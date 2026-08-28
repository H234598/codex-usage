"""Private, bounded cache for validated redacted Masterjet projections."""

from __future__ import annotations

import errno
import json
import math
import os
import stat
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
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
    private_path_lock,
    read_private_bytes_at,
)

MAX_CONTROL_SNAPSHOT_BYTES: Final[int] = 1024 * 1024
_CACHE_NAME: Final[str] = "control-snapshot-v1.json"
_SCHEMA_VERSION: Final[int] = 1
_MAX_PROJECT_LISTS: Final[int] = 256
_MAX_JSON_DEPTH: Final[int] = 16
_MAX_INTEGER_LEXEME_BYTES: Final[int] = 20
_MAX_FLOAT_LEXEME_BYTES: Final[int] = 64
_MAX_TIME_VALUE: Final[float] = float(2**53 - 1)
_MAX_DIRECTORY_ENTRIES: Final[int] = 4096
_TEMP_NAME: Final[str] = f".{_CACHE_NAME}.tmp"
_LEGACY_TEMP_PREFIX: Final[str] = f".{_CACHE_NAME}.tmp-"
_ROLLBACK_PREFIX: Final[str] = f".{_CACHE_NAME}.rollback-"
_PRIVATE_TEXT_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "apikey",
        "authorization",
        "bearer",
        "cookie",
        "error",
        "exception",
        "failed",
        "failure",
        "header",
        "passwd",
        "password",
        "session",
        "secret",
        "token",
        "traceback",
    }
)
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
            or any(part in {"", ".", ".."} for part in state_root.parts[1:])
            or not callable(clock)
        ):
            raise ControlCacheError("control.cache_request_invalid")
        root_fd = -1
        try:
            root_fd = self._open_cache_root(state_root, create=True)
        except (OSError, ValueError):
            raise ControlCacheError("control.cache_unavailable") from None
        finally:
            if root_fd >= 0:
                os.close(root_fd)
        self._root = state_root
        self._path = state_root / _CACHE_NAME
        self._clock = clock

    def save(self, snapshot: ControlSnapshot, *, observed_at: int | float) -> None:
        if type(snapshot) is not ControlSnapshot:
            raise ControlCacheError("control.response_private")
        observed = self._time_value(observed_at, "control.cache_request_invalid")
        encoded: bytes | None = None
        try:
            encoded = self._encode(snapshot, observed)
        except Exception:
            pass
        if encoded is None:
            raise ControlCacheError("control.response_private")
        if not encoded or len(encoded) > MAX_CONTROL_SNAPSHOT_BYTES:
            raise ControlCacheError("control.response_private")
        validated: tuple[ControlSnapshot, float] | None = None
        try:
            validated = self._decode(encoded)
        except ControlCacheError:
            pass
        if validated is None:
            raise ControlCacheError("control.response_private")

        try:
            with private_path_lock(self._path, label="Masterjet snapshot cache lock"):
                root_fd = self._open_cache_root(self._root, create=False)
                try:
                    self._prepare_publish_state(root_fd)
                    existing = self._read_existing(root_fd)
                    if existing is not None:
                        self._decode(existing[0])
                    self._publish(root_fd, encoded, existing)
                finally:
                    os.close(root_fd)
        except ControlCacheError:
            raise
        except Exception:
            raise ControlCacheError("control.cache_unavailable") from None

    def load(self, *, max_age_seconds: int | float) -> CachedControlSnapshot:
        maximum_age = self._time_value(
            max_age_seconds,
            "control.cache_request_invalid",
        )
        try:
            with private_path_lock(
                self._path,
                label="Masterjet snapshot cache lock",
                create=False,
            ):
                root_fd = self._open_cache_root(self._root, create=False)
                try:
                    self._prepare_publish_state(root_fd)
                    raw, _identity = read_private_bytes_at(
                        root_fd,
                        _CACHE_NAME,
                        maximum=MAX_CONTROL_SNAPSHOT_BYTES,
                        mode=0o600,
                    )
                finally:
                    os.close(root_fd)
        except ControlCacheError:
            raise
        except (FileNotFoundError, OSError, TimeoutError, ValueError):
            raise ControlCacheError("control.cache_unavailable") from None
        snapshot, observed = self._decode(raw)
        try:
            now = self._time_value(self._clock(), "control.cache_invalid")
        except ControlCacheError:
            raise
        except BaseException:
            raise ControlCacheError("control.cache_unavailable") from None
        age = now - observed
        stale = age < 0 or not math.isfinite(age) or age > maximum_age
        return CachedControlSnapshot(snapshot=snapshot, observed_at=observed, stale=stale)

    @staticmethod
    def _open_cache_root(state_root: Path, *, create: bool) -> int:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        current_fd = os.open(state_root.anchor, flags)
        try:
            ControlSnapshotCache._require_trusted_parent(os.fstat(current_fd))
            components = state_root.parts[1:]
            if not components:
                raise ValueError("cache root cannot be filesystem root")
            for index, component in enumerate(components):
                final = index == len(components) - 1
                created = False
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current_fd)
                        created = True
                    except FileExistsError:
                        pass
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
                if created:
                    os.fchmod(current_fd, 0o700)
                item = os.fstat(current_fd)
                if final:
                    ControlSnapshotCache._require_private_root(item)
                else:
                    ControlSnapshotCache._require_trusted_parent(item)
            result = current_fd
            current_fd = -1
            return result
        finally:
            if current_fd >= 0:
                os.close(current_fd)

    @staticmethod
    def _require_trusted_parent(item: os.stat_result) -> None:
        owner = item.st_uid
        mode = stat.S_IMODE(item.st_mode)
        sticky_root_directory = owner == 0 and bool(item.st_mode & stat.S_ISVTX)
        if (
            not stat.S_ISDIR(item.st_mode)
            or owner not in {0, os.geteuid()}
            or (mode & 0o022 and not sticky_root_directory)
        ):
            raise ValueError("cache parent directory is untrusted")

    @staticmethod
    def _require_private_root(item: os.stat_result) -> None:
        if (
            not stat.S_ISDIR(item.st_mode)
            or item.st_uid != os.geteuid()
            or stat.S_IMODE(item.st_mode) != 0o700
        ):
            raise ValueError("cache root directory is untrusted")

    @classmethod
    def _prepare_publish_state(cls, root_fd: int) -> None:
        rollback_names: list[str] = []
        temp_names: list[str] = []
        with os.scandir(root_fd) as entries:
            for count, entry in enumerate(entries, start=1):
                if count > _MAX_DIRECTORY_ENTRIES:
                    raise ValueError("cache directory exceeds entry budget")
                name = entry.name
                if type(name) is not str:
                    raise ValueError("cache artifact name is invalid")
                if name == _TEMP_NAME or name.startswith(_LEGACY_TEMP_PREFIX):
                    temp_names.append(name)
                elif name.startswith(_ROLLBACK_PREFIX):
                    rollback_names.append(name)
        for name in temp_names:
            cls._attest_named_file(root_fd, name)
            os.unlink(name, dir_fd=root_fd)
        if temp_names:
            cls._fsync_directory(root_fd)
        if len(rollback_names) > 1:
            raise ControlCacheError("control.cache_invalid")
        if rollback_names:
            cls._recover_rollback(root_fd, rollback_names[0])

    @classmethod
    def _recover_rollback(cls, root_fd: int, name: str) -> None:
        rollback_raw, rollback_identity = cls._read_named(root_fd, name)
        cls._decode(rollback_raw)
        existing = cls._read_existing(root_fd)
        if existing is not None:
            existing_raw, _existing_identity = existing
            cls._decode(existing_raw)
            if existing_raw != rollback_raw:
                raise ControlCacheError("control.cache_invalid")
            if cls._read_named(root_fd, name) != (rollback_raw, rollback_identity):
                raise ValueError("cache rollback changed before cleanup")
            os.unlink(name, dir_fd=root_fd)
            cls._fsync_directory(root_fd)
            return
        if cls._read_named(root_fd, name) != (rollback_raw, rollback_identity):
            raise ValueError("cache rollback changed before recovery")
        try:
            os.stat(_CACHE_NAME, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("cache target appeared during recovery")
        os.replace(
            name,
            _CACHE_NAME,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        cls._fsync_directory(root_fd)
        recovered_raw, _recovered_identity = cls._read_named(root_fd, _CACHE_NAME)
        if recovered_raw != rollback_raw:
            raise ControlCacheError("control.cache_invalid")

    @classmethod
    def _publish(
        cls,
        root_fd: int,
        encoded: bytes,
        existing: tuple[bytes, tuple[int, ...]] | None,
    ) -> None:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        temp_fd = -1
        temp_identity: tuple[int, ...] | None = None
        published = False
        try:
            temp_fd = os.open(_TEMP_NAME, flags, 0o600, dir_fd=root_fd)
            os.fchmod(temp_fd, 0o600)
            offset = 0
            while offset < len(encoded):
                written = os.write(temp_fd, encoded[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "short cache write")
                offset += written
            os.fsync(temp_fd)
            temp_identity = cls._private_file_identity(
                os.fstat(temp_fd),
                expected_size=len(encoded),
            )
            cls._fsync_directory(root_fd)
            named_raw, named_identity = cls._read_named(root_fd, _TEMP_NAME)
            if named_raw != encoded or named_identity != temp_identity:
                raise ValueError("cache temporary identity changed")
            cls._recheck_target(root_fd, existing)
            os.replace(
                _TEMP_NAME,
                _CACHE_NAME,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
            published = True
            published_raw, published_identity = cls._read_named(root_fd, _CACHE_NAME)
            if published_raw != encoded or published_identity[:5] != temp_identity[:5]:
                raise ValueError("published cache identity changed")
            cls._fsync_directory(root_fd)
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            if not published and temp_identity is not None:
                cls._remove_matching_temp(root_fd, temp_identity)

    @classmethod
    def _recheck_target(
        cls,
        root_fd: int,
        existing: tuple[bytes, tuple[int, ...]] | None,
    ) -> None:
        if existing is None:
            try:
                os.stat(_CACHE_NAME, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise ValueError("cache target appeared before publish")
        current = cls._read_named(root_fd, _CACHE_NAME)
        if current != existing:
            raise ValueError("cache target changed before publish")

    @classmethod
    def _remove_matching_temp(cls, root_fd: int, identity: tuple[int, ...]) -> None:
        try:
            current = cls._attest_named_file(root_fd, _TEMP_NAME)
        except (FileNotFoundError, OSError, ValueError):
            return
        if current != identity:
            return
        try:
            os.unlink(_TEMP_NAME, dir_fd=root_fd)
            cls._fsync_directory(root_fd)
        except OSError:
            return

    @classmethod
    def _read_existing(
        cls,
        root_fd: int,
    ) -> tuple[bytes, tuple[int, ...]] | None:
        try:
            return cls._read_named(root_fd, _CACHE_NAME)
        except FileNotFoundError:
            return None

    @classmethod
    def _read_named(cls, root_fd: int, name: str) -> tuple[bytes, tuple[int, ...]]:
        raw, opened_identity = read_private_bytes_at(
            root_fd,
            name,
            maximum=MAX_CONTROL_SNAPSHOT_BYTES,
            mode=0o600,
        )
        identity = cls._attest_named_file(root_fd, name)
        if identity[:3] != (
            opened_identity.device,
            opened_identity.inode,
            opened_identity.mode,
        ):
            raise ValueError("cache file identity changed after read")
        return raw, identity

    @classmethod
    def _attest_named_file(cls, root_fd: int, name: str) -> tuple[int, ...]:
        item = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        return cls._private_file_identity(item)

    @staticmethod
    def _private_file_identity(
        item: os.stat_result,
        *,
        expected_size: int | None = None,
    ) -> tuple[int, ...]:
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_uid != os.geteuid()
            or item.st_nlink != 1
            or stat.S_IMODE(item.st_mode) != 0o600
            or item.st_size > MAX_CONTROL_SNAPSHOT_BYTES
            or (expected_size is not None and item.st_size != expected_size)
        ):
            raise ValueError("cache file metadata is invalid")
        return (
            item.st_dev,
            item.st_ino,
            stat.S_IMODE(item.st_mode),
            item.st_uid,
            item.st_nlink,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )

    @staticmethod
    def _fsync_directory(root_fd: int) -> None:
        try:
            os.fsync(root_fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise

    @classmethod
    def _decode(cls, raw: bytes) -> tuple[ControlSnapshot, float]:
        if type(raw) is not bytes or not raw or len(raw) > MAX_CONTROL_SNAPSHOT_BYTES:
            raise ControlCacheError("control.cache_invalid")
        result: tuple[ControlSnapshot, float] | None = None
        try:
            cls._require_json_depth(raw)
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=cls._unique_object,
                parse_constant=cls._reject_constant,
                parse_float=cls._parse_float,
                parse_int=cls._parse_integer,
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
            if raw == cls._encode(snapshot, observed):
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
    def _encode(cls, snapshot: ControlSnapshot, observed_at: float) -> bytes:
        document = cls._document(snapshot, observed_at)
        return json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

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
            "label": ControlSnapshotCache._public_text(item.label),
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
            "label": ControlSnapshotCache._public_text(item.label),
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
            "project_name": ControlSnapshotCache._public_text(item.project_name),
            "purpose": item.purpose,
            "key_name": ControlSnapshotCache._public_text(item.key_name),
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
        if result is None or not math.isfinite(result) or result < 0 or result > _MAX_TIME_VALUE:
            raise ControlCacheError(error)
        return result

    @staticmethod
    def _public_text(value: str) -> str:
        if type(value) is not str:
            raise ValueError("cache display text is invalid")
        normalized = unicodedata.normalize("NFKC", value).casefold()
        tokens: list[str] = []
        token: list[str] = []
        for character in normalized:
            if character.isalnum():
                token.append(character)
            elif token:
                tokens.append("".join(token))
                token.clear()
        if token:
            tokens.append("".join(token))
        if any(item in _PRIVATE_TEXT_MARKERS for item in tokens) or any(
            left == "api" and right == "key" for left, right in pairwise(tokens)
        ):
            raise ValueError("cache display text is private")
        return value

    @staticmethod
    def _require_json_depth(raw: bytes) -> None:
        depth = 0
        in_string = False
        escaped = False
        for value in raw:
            if in_string:
                if escaped:
                    escaped = False
                elif value == 0x5C:
                    escaped = True
                elif value == 0x22:
                    in_string = False
                continue
            if value == 0x22:
                in_string = True
            elif value in (0x5B, 0x7B):
                depth += 1
                if depth > _MAX_JSON_DEPTH:
                    raise ValueError("cache JSON depth exceeded")
            elif value in (0x5D, 0x7D):
                depth -= 1
                if depth < 0:
                    raise ValueError("cache JSON nesting is invalid")
        if in_string or depth != 0:
            raise ValueError("cache JSON nesting is incomplete")

    @staticmethod
    def _parse_integer(value: str) -> int:
        if type(value) is not str or len(value.encode("ascii")) > _MAX_INTEGER_LEXEME_BYTES:
            raise ValueError("cache integer exceeds budget")
        return int(value)

    @staticmethod
    def _parse_float(value: str) -> float:
        if type(value) is not str or len(value.encode("ascii")) > _MAX_FLOAT_LEXEME_BYTES:
            raise ValueError("cache float exceeds budget")
        result = float(value)
        if not math.isfinite(result) or abs(result) > _MAX_TIME_VALUE:
            raise ValueError("cache float exceeds value budget")
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
