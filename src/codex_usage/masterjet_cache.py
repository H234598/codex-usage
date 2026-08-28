"""Private, bounded cache for validated redacted Masterjet projections."""

from __future__ import annotations

import json
import math
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from .masterjet_contracts import (
    ControlContractError,
    GoogleControlAccount,
    GoogleControlProject,
    GoogleControlProjectList,
    OpenAIControlAccount,
    parse_google_accounts,
    parse_google_projects,
    parse_openai_accounts,
)
from .private_io import (
    ensure_private_directory,
    open_verified_state_home,
    private_path_lock,
    read_private_bytes_at,
    write_private_text,
)

MAX_CONTROL_SNAPSHOT_BYTES: Final[int] = 1024 * 1024
_CACHE_NAME: Final[str] = "control-snapshot-v1.json"
_SCHEMA_VERSION: Final[int] = 1
_MAX_PROJECT_LISTS: Final[int] = 256
_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "control.cache_invalid",
        "control.cache_request_invalid",
        "control.cache_unavailable",
        "control.response_private",
    }
)


class ControlCacheError(ValueError):
    """Stable, code-only cache failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in _ERROR_CODES:
            raise TypeError("invalid control cache error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ControlSnapshot:
    openai_accounts: tuple[OpenAIControlAccount, ...] = ()
    google_accounts: tuple[GoogleControlAccount, ...] = ()
    google_projects: tuple[GoogleControlProjectList, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.openai_accounts) is not tuple
            or type(self.google_accounts) is not tuple
            or type(self.google_projects) is not tuple
            or not all(type(item) is OpenAIControlAccount for item in self.openai_accounts)
            or not all(type(item) is GoogleControlAccount for item in self.google_accounts)
            or not all(type(item) is GoogleControlProjectList for item in self.google_projects)
            or len(self.google_projects) > _MAX_PROJECT_LISTS
        ):
            raise ControlCacheError("control.response_private")
        project_accounts = tuple(item.account_ref for item in self.google_projects)
        if len(project_accounts) != len(set(project_accounts)):
            raise ControlCacheError("control.cache_request_invalid")


@dataclass(frozen=True, slots=True)
class CachedControlSnapshot:
    snapshot: ControlSnapshot
    observed_at: float
    stale: bool


class ControlSnapshotCache:
    """One private schema-v1 snapshot file under an explicit state root."""

    __slots__ = ("_clock", "_path", "_root")

    def __init__(self, state_root: Path) -> None:
        self._initialize(state_root, clock=time.time)

    @classmethod
    def for_test(
        cls,
        state_root: Path,
        *,
        clock: Callable[[], float] | None = None,
    ) -> ControlSnapshotCache:
        cache = cls.__new__(cls)
        cache._initialize(state_root, clock=clock if clock is not None else time.time)
        return cache

    def _initialize(self, state_root: Path, *, clock: Callable[[], float]) -> None:
        if (
            type(state_root) is not type(Path())
            or not state_root.is_absolute()
            or not callable(clock)
        ):
            raise ControlCacheError("control.cache_request_invalid")
        try:
            self._prepare_root(state_root)
        except (OSError, ValueError):
            raise ControlCacheError("control.cache_unavailable") from None
        self._root = state_root
        self._path = state_root / _CACHE_NAME
        self._clock = clock

    def save(self, snapshot: ControlSnapshot, *, observed_at: int | float) -> None:
        if type(snapshot) is not ControlSnapshot:
            raise ControlCacheError("control.response_private")
        observed = self._time_value(observed_at, "control.cache_request_invalid")
        encoded: str | None = None
        try:
            document = self._document(snapshot, observed)
            encoded = json.dumps(
                document,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except Exception:
            pass
        if encoded is None:
            raise ControlCacheError("control.response_private")
        encoded_bytes = encoded.encode("utf-8")
        if not encoded_bytes or len(encoded_bytes) > MAX_CONTROL_SNAPSHOT_BYTES:
            raise ControlCacheError("control.response_private")
        validated: tuple[ControlSnapshot, float] | None = None
        try:
            validated = self._decode(encoded_bytes)
        except ControlCacheError:
            pass
        if validated is None:
            raise ControlCacheError("control.response_private")

        try:
            with private_path_lock(self._path, label="Masterjet snapshot cache lock"):
                root_fd = open_verified_state_home(self._root)
                try:
                    try:
                        existing, _identity = read_private_bytes_at(
                            root_fd,
                            _CACHE_NAME,
                            maximum=MAX_CONTROL_SNAPSHOT_BYTES,
                            mode=0o600,
                        )
                    except FileNotFoundError:
                        existing = None
                    if existing is not None:
                        self._decode(existing)
                    write_private_text(
                        self._path,
                        encoded,
                        label="Masterjet snapshot cache",
                        mode=0o600,
                    )
                finally:
                    os.close(root_fd)
        except ControlCacheError:
            raise
        except (OSError, TimeoutError, ValueError):
            raise ControlCacheError("control.cache_unavailable") from None

    def load(self, *, max_age_seconds: int | float) -> CachedControlSnapshot:
        maximum_age = self._time_value(
            max_age_seconds,
            "control.cache_request_invalid",
        )
        try:
            now = self._time_value(self._clock(), "control.cache_invalid")
        except ControlCacheError:
            raise
        except BaseException:
            raise ControlCacheError("control.cache_unavailable") from None
        try:
            with private_path_lock(
                self._path,
                label="Masterjet snapshot cache lock",
                create=False,
            ):
                root_fd = open_verified_state_home(self._root)
                try:
                    raw, _identity = read_private_bytes_at(
                        root_fd,
                        _CACHE_NAME,
                        maximum=MAX_CONTROL_SNAPSHOT_BYTES,
                        mode=0o600,
                    )
                finally:
                    os.close(root_fd)
        except (FileNotFoundError, OSError, TimeoutError, ValueError):
            raise ControlCacheError("control.cache_unavailable") from None
        snapshot, observed = self._decode(raw)
        age = now - observed
        stale = age < 0 or not math.isfinite(age) or age > maximum_age
        return CachedControlSnapshot(snapshot=snapshot, observed_at=observed, stale=stale)

    @staticmethod
    def _prepare_root(state_root: Path) -> None:
        try:
            current = state_root.lstat()
        except FileNotFoundError:
            ensure_private_directory(state_root, label="Masterjet snapshot cache directory")
            current = state_root.lstat()
        if (
            not stat.S_ISDIR(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or current.st_uid != os.geteuid()
            or stat.S_IMODE(current.st_mode) != 0o700
        ):
            raise ValueError("unsafe cache directory")

    @classmethod
    def _decode(cls, raw: bytes) -> tuple[ControlSnapshot, float]:
        if type(raw) is not bytes or not raw or len(raw) > MAX_CONTROL_SNAPSHOT_BYTES:
            raise ControlCacheError("control.cache_invalid")
        result: tuple[ControlSnapshot, float] | None = None
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=cls._unique_object,
                parse_constant=cls._reject_constant,
            )
            if type(payload) is not dict or set(payload) != {
                "schema_version",
                "observed_at",
                "snapshot",
            }:
                raise ValueError("invalid document")
            if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
                raise ValueError("invalid schema")
            observed = cls._time_value(payload["observed_at"], "control.cache_invalid")
            data = payload["snapshot"]
            if type(data) is not dict or set(data) != {
                "google_accounts",
                "google_projects",
                "openai_accounts",
            }:
                raise ValueError("invalid snapshot")
            openai_accounts = parse_openai_accounts(
                {"schema_version": 1, "accounts": data["openai_accounts"]}
            )
            google_accounts = parse_google_accounts(
                {"schema_version": 1, "accounts": data["google_accounts"]}
            )
            projects_raw = data["google_projects"]
            if type(projects_raw) is not list or len(projects_raw) > _MAX_PROJECT_LISTS:
                raise ValueError("invalid projects")
            google_projects = tuple(parse_google_projects(item) for item in projects_raw)
            snapshot = ControlSnapshot(
                openai_accounts=openai_accounts,
                google_accounts=google_accounts,
                google_projects=google_projects,
            )
            result = snapshot, observed
        except (
            ControlCacheError,
            ControlContractError,
            json.JSONDecodeError,
            RecursionError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
        ):
            pass
        if result is None:
            raise ControlCacheError("control.cache_invalid")
        return result

    @classmethod
    def _document(cls, snapshot: ControlSnapshot, observed_at: float) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "observed_at": observed_at,
            "snapshot": {
                "openai_accounts": [cls._openai_account(item) for item in snapshot.openai_accounts],
                "google_accounts": [cls._google_account(item) for item in snapshot.google_accounts],
                "google_projects": [
                    cls._google_project_list(item) for item in snapshot.google_projects
                ],
            },
        }

    @staticmethod
    def _openai_account(item: OpenAIControlAccount) -> dict[str, object]:
        return {
            "ref": item.ref,
            "label": item.label,
            "enabled": item.enabled,
            "local_profile_ref": item.local_profile_ref,
            "source_host_ref": item.source_host_ref,
            "auth_state": item.auth_state,
            "access_expires_at": ControlSnapshotCache._timestamp(item.access_expires_at),
            "credential_generation": item.credential_generation,
            "vault_projection_state": item.vault_projection_state,
            "usage_state": item.usage_state,
        }

    @staticmethod
    def _google_account(item: GoogleControlAccount) -> dict[str, object]:
        return {
            "ref": item.ref,
            "label": item.label,
            "enabled": item.enabled,
            "subject_bound": item.subject_bound,
            "oauth_state": item.oauth_state,
            "inventory_generation": item.inventory_generation,
            "quota_state": item.quota_state,
            "project_count": item.project_count,
            "billing_count": item.billing_count,
            "reload_state": item.reload_state,
        }

    @staticmethod
    def _google_project_list(item: GoogleControlProjectList) -> dict[str, object]:
        return {
            "schema_version": 1,
            "account_ref": item.account_ref,
            "inventory_generation": item.inventory_generation,
            "projects": [
                ControlSnapshotCache._google_project(project) for project in item.projects
            ],
        }

    @staticmethod
    def _google_project(item: GoogleControlProject) -> dict[str, object]:
        return {
            "ref": item.ref,
            "project_name": item.project_name,
            "purpose": item.purpose,
            "key_name": item.key_name,
            "billing_ref": item.billing_ref,
            "status": item.status,
            "probe_state": item.probe_state,
            "quota_state": item.quota_state,
        }

    @staticmethod
    def _timestamp(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _time_value(value: object, error: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ControlCacheError(error)
        result: float | None = None
        try:
            result = float(value)
        except (OverflowError, TypeError, ValueError):
            pass
        if result is None or not math.isfinite(result) or result < 0:
            raise ControlCacheError(error)
        return result

    @staticmethod
    def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if type(key) is not str or key in result:
                raise ValueError("duplicate object key")
            result[key] = value
        return result

    @staticmethod
    def _reject_constant(_value: str) -> object:
        raise ValueError("non-finite JSON number")


def save_control_snapshot(
    state_root: Path,
    snapshot: ControlSnapshot,
    *,
    observed_at: int | float,
) -> None:
    ControlSnapshotCache(state_root).save(snapshot, observed_at=observed_at)


def load_control_snapshot(
    state_root: Path,
    max_age_seconds: int | float,
) -> CachedControlSnapshot:
    return ControlSnapshotCache(state_root).load(max_age_seconds=max_age_seconds)


__all__ = [
    "MAX_CONTROL_SNAPSHOT_BYTES",
    "CachedControlSnapshot",
    "ControlCacheError",
    "ControlSnapshot",
    "ControlSnapshotCache",
    "load_control_snapshot",
    "save_control_snapshot",
]
