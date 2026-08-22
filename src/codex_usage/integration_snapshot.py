from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn, cast

from .consumption import MAX_FORECAST_SECONDS
from .history import MAX_HISTORY_SAMPLES, MAX_HISTORY_WINDOW_SECONDS
from .json_utils import loads_strict
from .models import AccountStatus, AccountUsage, LimitWindow, UsagePool
from .private_io import (
    assert_no_symlink_ancestors,
    private_path_lock,
    write_private_text,
)
from .state import load_current_usage
from .usage_resets import UsageResetState

_ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_ASCII_TOKEN_RE = re.compile(r"[!-~]{1,128}")
_KEY_RE = re.compile(r"[a-z][a-z0-9_]*")
_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_MAX_ACCOUNTS = 100
_MAX_DIRECTORY_ENTRIES = _MAX_ACCOUNTS * 8
_MAX_LIMITS_PER_ACCOUNT = 32
_MAX_COST_WINDOWS_PER_ACCOUNT = 64
_MAX_MODEL_POOLS_PER_ACCOUNT = 32
_WINDOW_NAME_SECONDS = {
    "5h": 18_000,
    "weekly": 604_800,
    "30d": 2_592_000,
}
_SCHEMA_STATUSES = frozenset(("ok", "partial", "error", "login_required", "unknown"))
_SECRET_NAMES = frozenset(
    (
        "token",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "apikey",
        "secret",
        "clientsecret",
        "password",
        "passphrase",
        "authorization",
        "cookie",
        "cookies",
        "session",
        "sessionid",
        "csrf",
        "devicecode",
        "auth",
        "authjson",
        "privatekey",
        "credential",
        "credentials",
        "credentialfingerprint",
        "email",
        "emailaddress",
        "responsebody",
        "raw",
        "rawoutput",
        "headers",
        "profile",
        "profilepath",
        "authjsonpath",
        "sourceurls",
        "backenduserid",
        "backendaccountid",
    )
)
_SECRET_SUFFIXES = ("token", "secret", "key", "cookie", "password", "path", "url", "header")
_JWT_RE = re.compile(
    r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{16,}$"
)
_PEM_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


class IntegrationSnapshotError(Exception):
    exit_code: int = 70


class IntegrationInvalidSource(IntegrationSnapshotError):
    exit_code = 65


class IntegrationUnavailable(IntegrationSnapshotError):
    exit_code = 69


class IntegrationSecureIOError(IntegrationSnapshotError):
    exit_code = 70


class IntegrationBusy(IntegrationSnapshotError):
    exit_code = 75


def _invalid() -> NoReturn:
    raise IntegrationInvalidSource()


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime):
        _invalid()
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            _invalid()
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except Exception:
        _invalid()


def _safe_account_filename(path: Path) -> str | None:
    try:
        item = path.lstat()
    except OSError:
        return None
    if (
        path.suffix != ".json"
        or not stat.S_ISREG(item.st_mode)
        or stat.S_IMODE(item.st_mode) != 0o600
        or item.st_nlink != 1
    ):
        return None
    account_id = path.stem
    if account_id in {".", ".."} or not _ACCOUNT_ID_RE.fullmatch(account_id):
        return None
    return account_id


def _directory_identity(path: Path) -> tuple[int, int, int]:
    try:
        assert_no_symlink_ancestors(path, label="current directory")
    except ValueError:
        raise IntegrationInvalidSource() from None
    except OSError:
        raise IntegrationUnavailable() from None
    try:
        item = path.lstat()
    except OSError:
        raise IntegrationUnavailable() from None
    if (
        not stat.S_ISDIR(item.st_mode)
        or stat.S_IMODE(item.st_mode) != 0o700
        or item.st_uid != os.getuid()
    ):
        raise IntegrationInvalidSource()
    return item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode)


def _source_file_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        item = path.lstat()
    except OSError:
        raise IntegrationInvalidSource() from None
    if (
        not stat.S_ISREG(item.st_mode)
        or stat.S_IMODE(item.st_mode) != 0o600
        or item.st_nlink != 1
        or item.st_uid != os.getuid()
    ):
        raise IntegrationInvalidSource()
    return item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode), item.st_size


def read_current_usage_records(current_dir: Path) -> tuple[AccountUsage, ...]:
    if not isinstance(current_dir, Path) or not current_dir.is_absolute():
        raise IntegrationInvalidSource()
    initial_identity = _directory_identity(current_dir)
    try:
        candidates: list[Path] = []
        entries_seen = 0
        for path in current_dir.iterdir():
            entries_seen += 1
            if entries_seen > _MAX_DIRECTORY_ENTRIES:
                raise IntegrationInvalidSource()
            if _is_transient_current_path(path):
                continue
            if len(candidates) >= _MAX_ACCOUNTS:
                raise IntegrationInvalidSource()
            candidates.append(path)
    except OSError:
        raise IntegrationUnavailable() from None
    if _directory_identity(current_dir) != initial_identity:
        raise IntegrationInvalidSource()

    account_paths: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for path in candidates:
        account_id = _safe_account_filename(path)
        if account_id is None or account_id in seen:
            raise IntegrationInvalidSource()
        seen.add(account_id)
        account_paths.append((account_id, path))
    account_paths.sort(key=lambda item: item[0])

    records: list[AccountUsage] = []
    for account_id, path in account_paths:
        if _directory_identity(current_dir) != initial_identity:
            raise IntegrationInvalidSource()
        before_file = _source_file_identity(path)
        try:
            usage = load_current_usage(account_id, current_dir)
        except Exception:
            raise IntegrationInvalidSource() from None
        if _directory_identity(current_dir) != initial_identity:
            raise IntegrationInvalidSource()
        if _source_file_identity(path) != before_file:
            raise IntegrationInvalidSource()
        if not isinstance(usage, AccountUsage) or usage.account_id != account_id:
            raise IntegrationInvalidSource()
        records.append(usage)
    if _directory_identity(current_dir) != initial_identity:
        raise IntegrationInvalidSource()
    return tuple(records)


def _is_transient_current_path(path: Path) -> bool:
    name = path.name
    return name.endswith(".json.lock") or (name.startswith(".") and ".json.tmp-" in name)


def _pool_windows(pool: UsagePool) -> list[dict[str, object]]:
    if not isinstance(pool.key, str) or not _ASCII_TOKEN_RE.fullmatch(pool.key[:64]):
        _invalid()
    if len(pool.key) > 64 or not isinstance(pool.windows, tuple):
        _invalid()
    if (
        not isinstance(pool.available, bool)
        or (pool.allowed is not None and not isinstance(pool.allowed, bool))
        or (pool.limit_reached is not None and not isinstance(pool.limit_reached, bool))
        or not isinstance(pool.availability_sources, tuple)
        or any(not isinstance(source, str) for source in pool.availability_sources)
    ):
        _invalid()
    if not pool.available:
        return []
    limits: list[dict[str, object]] = []
    for window in pool.windows:
        if not isinstance(window, LimitWindow):
            _invalid()
        if not window.has_known_identity:
            continue
        duration = window.duration_seconds
        if duration is None:
            duration = _WINDOW_NAME_SECONDS.get(window.name.strip().casefold())
        if (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or not 0 < duration <= MAX_HISTORY_WINDOW_SECONDS
        ):
            continue
        remaining = window.remaining_percent
        if (
            remaining is None
            or isinstance(remaining, bool)
            or not isinstance(remaining, (int, float))
        ):
            continue
        try:
            remaining = float(remaining)
        except (OverflowError, TypeError, ValueError):
            continue
        if not math.isfinite(remaining) or not 0 <= remaining <= 100:
            continue
        reset_at = None
        if window.reset_at is not None:
            if not isinstance(window.reset_at, datetime):
                continue
            try:
                reset_at = _utc_text(window.reset_at)
            except IntegrationInvalidSource:
                continue
        limits.append(
            {
                "pool": pool.key,
                "window_seconds": duration,
                "used_percent": 100.0 - remaining,
                "remaining_percent": remaining,
                **({"reset_at": reset_at} if reset_at is not None else {}),
            }
        )
    if len(limits) > _MAX_LIMITS_PER_ACCOUNT:
        _invalid()
    return limits


def _status_text(status: AccountStatus) -> str:
    if not isinstance(status, AccountStatus):
        _invalid()
    if status == AccountStatus.BLOCKED:
        return "error"
    if status.value not in {"ok", "partial", "error", "login_required"}:
        _invalid()
    return status.value


def _source_limits(usage: AccountUsage) -> list[dict[str, object]]:
    if (
        not isinstance(usage.models, tuple)
        or len(usage.models) > _MAX_MODEL_POOLS_PER_ACCOUNT
    ):
        _invalid()
    pools: list[UsagePool] = []
    if usage.main is not None:
        pools.append(usage.main)
    pools.extend(usage.models)
    seen: set[str] = set()
    limits: list[dict[str, object]] = []
    for pool in pools:
        if not isinstance(pool, UsagePool):
            _invalid()
        if not isinstance(pool.key, str):
            _invalid()
        if pool.key in seen:
            _invalid()
        seen.add(pool.key)
        limits.extend(_pool_windows(pool))
    if len(limits) > _MAX_LIMITS_PER_ACCOUNT:
        _invalid()
    identities = {
        (item["pool"], item["window_seconds"], item.get("reset_at"))
        for item in limits
    }
    if len(identities) != len(limits):
        _invalid()
    limits.sort(key=lambda item: (item["pool"], item["window_seconds"], item.get("reset_at", "")))
    return limits


def build_schema1_document(
    usages: tuple,
    *,
    generated_at: datetime,
    source_commit: str | None = None,
    cost_windows_by_account: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if not isinstance(usages, tuple) or len(usages) > _MAX_ACCOUNTS:
        _invalid()
    generated_text = _utc_text(generated_at)
    if source_commit is not None and (
        not isinstance(source_commit, str)
        or not source_commit.isascii()
        or not _ASCII_TOKEN_RE.fullmatch(source_commit)
    ):
        _invalid()
    if cost_windows_by_account is not None and not isinstance(cost_windows_by_account, Mapping):
        _invalid()

    accounts: list[dict[str, object]] = []
    seen: set[str] = set()
    for usage in usages:
        if not isinstance(usage, AccountUsage):
            _invalid()
        if not isinstance(usage.account_id, str) or not _ACCOUNT_ID_RE.fullmatch(usage.account_id):
            _invalid()
        if usage.account_id in seen:
            _invalid()
        if not isinstance(usage.stale, bool):
            _invalid()
        seen.add(usage.account_id)
        status_text = _status_text(usage.status)
        account: dict[str, object] = {
            "account_id": usage.account_id,
            "freshness": {
                "captured_at": _utc_text(usage.captured_at),
                "stale": usage.stale is True,
            },
            "status": status_text,
        }
        limits = _source_limits(usage)
        if status_text != "ok":
            limits = []
        if limits:
            account["limits"] = limits
        if cost_windows_by_account is not None and usage.account_id in cost_windows_by_account:
            raw_windows = cost_windows_by_account[usage.account_id]
            if (
                not isinstance(raw_windows, (tuple, list))
                or len(raw_windows) > _MAX_COST_WINDOWS_PER_ACCOUNT
            ):
                _invalid()
            cost_windows: list[object] = []
            for item in raw_windows:
                try:
                    converter = getattr(item, "as_dict", None)
                except Exception:
                    _invalid()
                if converter is None:
                    cost_windows.append(item)
                    continue
                if not callable(converter):
                    _invalid()
                try:
                    cost_windows.append(converter())
                except Exception:
                    _invalid()
            account["cost_windows"] = cost_windows
        if not isinstance(usage.usage_resets, UsageResetState):
            _invalid()
        account["usage_resets"] = usage.usage_resets.as_dict()
        accounts.append(account)
    accounts.sort(key=lambda account: cast(str, account["account_id"]))
    document: dict[str, object] = {
        "accounts": accounts,
        "generated_at": generated_text,
        "schema_version": 1,
    }
    if source_commit is not None:
        document["source_commit"] = source_commit
    return document


def _secret_key(key: str) -> bool:
    normalized = key.casefold().replace("_", "")
    return normalized in _SECRET_NAMES or normalized.endswith(_SECRET_SUFFIXES)


def _scan_secrets(value: object, *, depth: int = 0) -> None:
    if depth > 64:
        _invalid()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
                _invalid()
            if _secret_key(key):
                _invalid()
            _scan_secrets(nested, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _scan_secrets(nested, depth=depth + 1)
        return
    if isinstance(value, str):
        if (
            value.startswith("Bearer ")
            or _JWT_RE.fullmatch(value)
            or _PEM_PRIVATE_KEY_RE.search(value)
        ):
            _invalid()
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    _invalid()


def _canonical_timestamp(value: object) -> str:
    if not isinstance(value, str) or "T" not in value:
        _invalid()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        _invalid()
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _invalid()
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_token(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or len(value) < 1 or len(value) > maximum:
        _invalid()
    if not value.isascii() or not _ASCII_TOKEN_RE.fullmatch(value):
        _invalid()
    return value


def _canonical_percent(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid()
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        _invalid()
    if not math.isfinite(result) or not 0 <= result <= 100:
        _invalid()
    return result


def _canonical_cost(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid()
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        _invalid()
    if not math.isfinite(result) or not 0 <= result <= 10_000:
        _invalid()
    return result


def _canonical_int(value: object, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        _invalid()
    return value


def _canonical_limit(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid()
    if "pool" not in value or "window_seconds" not in value:
        _invalid()
    result: dict[str, object] = {
        "pool": _canonical_token(value["pool"], maximum=64),
        "window_seconds": _canonical_int(value["window_seconds"], maximum=2_592_000),
    }
    for name in ("used_percent", "remaining_percent"):
        if name in value:
            result[name] = _canonical_percent(value[name])
    if "reset_at" in value:
        result["reset_at"] = _canonical_timestamp(value["reset_at"])
    return result


def _canonical_cost_window(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _invalid()
    required = (
        "lookback_seconds",
        "pool",
        "limit_window_seconds",
        "consumed_percentage_points",
        "coverage",
        "sample_count",
    )
    if any(name not in value for name in required):
        _invalid()
    coverage = value["coverage"]
    if not isinstance(coverage, str) or coverage not in {
        "complete",
        "partial",
        "insufficient",
        "stale",
    }:
        _invalid()
    sample_count = value["sample_count"]
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or not 0 <= sample_count <= MAX_HISTORY_SAMPLES
    ):
        _invalid()
    estimate = value.get("estimated_seconds_to_exhaustion")
    if estimate is not None and (
        isinstance(estimate, bool)
        or not isinstance(estimate, int)
        or not 0 <= estimate <= MAX_FORECAST_SECONDS
    ):
        _invalid()
    result = {
        "consumed_percentage_points": _canonical_cost(value["consumed_percentage_points"]),
        "coverage": coverage,
        "limit_window_seconds": _canonical_int(value["limit_window_seconds"], maximum=2_592_000),
        "lookback_seconds": _canonical_int(value["lookback_seconds"], maximum=31_536_000),
        "pool": _canonical_token(value["pool"], maximum=64),
        "sample_count": sample_count,
    }
    if "estimated_seconds_to_exhaustion" in value:
        result["estimated_seconds_to_exhaustion"] = estimate
    if "baseline_used_percent" in value:
        baseline = value["baseline_used_percent"]
        result["baseline_used_percent"] = (
            None if baseline is None else _canonical_percent(baseline)
        )
    return result


def _canonical_document(document: object) -> dict[str, object]:
    if not isinstance(document, Mapping):
        _invalid()
    accounts_value = document.get("accounts")
    if not isinstance(accounts_value, list) or len(accounts_value) > _MAX_ACCOUNTS:
        _invalid()
    _scan_secrets(document)
    schema_version = document.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
        or "generated_at" not in document
    ):
        _invalid()
    accounts: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_account in accounts_value:
        if not isinstance(raw_account, Mapping):
            _invalid()
        if not all(name in raw_account for name in ("account_id", "status", "freshness")):
            _invalid()
        account_id = _canonical_token(raw_account["account_id"], maximum=64)
        if not _ACCOUNT_ID_RE.fullmatch(account_id) or account_id in seen:
            _invalid()
        status = raw_account["status"]
        if not isinstance(status, str) or status not in _SCHEMA_STATUSES:
            _invalid()
        freshness = raw_account["freshness"]
        if not isinstance(freshness, Mapping) or not all(
            name in freshness for name in ("captured_at", "stale")
        ):
            _invalid()
        if not isinstance(freshness["stale"], bool):
            _invalid()
        account: dict[str, object] = {
            "account_id": account_id,
            "freshness": {
                "captured_at": _canonical_timestamp(freshness["captured_at"]),
                "stale": freshness["stale"],
            },
            "status": status,
        }
        if "limits" in raw_account:
            limits = raw_account["limits"]
            if not isinstance(limits, list) or len(limits) > _MAX_LIMITS_PER_ACCOUNT:
                _invalid()
            canonical_limits = [_canonical_limit(limit) for limit in limits]
            identities = {
                (item["pool"], item["window_seconds"], item.get("reset_at"))
                for item in canonical_limits
            }
            if len(identities) != len(canonical_limits):
                _invalid()
            account["limits"] = sorted(
                canonical_limits,
                key=lambda item: (item["pool"], item["window_seconds"], item.get("reset_at", "")),
            )
        if "cost_windows" in raw_account:
            cost_windows = raw_account["cost_windows"]
            if (
                not isinstance(cost_windows, list)
                or len(cost_windows) > _MAX_COST_WINDOWS_PER_ACCOUNT
            ):
                _invalid()
            account["cost_windows"] = sorted(
                (_canonical_cost_window(item) for item in cost_windows),
                key=lambda item: (
                    item["pool"],
                    item["lookback_seconds"],
                    item["limit_window_seconds"],
                ),
            )
        if "usage_resets" in raw_account:
            resets = raw_account["usage_resets"]
            if not isinstance(resets, Mapping) or not all(
                name in resets for name in ("available", "known", "redeem_capability")
            ):
                _invalid()
            available = resets["available"]
            if not isinstance(resets["known"], bool) or not isinstance(
                resets["redeem_capability"], bool
            ):
                _invalid()
            if resets["known"]:
                if (
                    isinstance(available, bool)
                    or not isinstance(available, int)
                    or not 0 <= available <= 10_000
                ):
                    _invalid()
            elif available is not None:
                _invalid()
            account["usage_resets"] = {
                "available": available,
                "known": resets["known"],
                "redeem_capability": resets["redeem_capability"],
            }
        seen.add(account_id)
        accounts.append(account)
    result: dict[str, object] = {
        "accounts": sorted(
            accounts,
            key=lambda account: cast(str, account["account_id"]),
        ),
        "generated_at": _canonical_timestamp(document["generated_at"]),
        "schema_version": 1,
    }
    if "source_commit" in document:
        result["source_commit"] = _canonical_token(document["source_commit"], maximum=128)
    return result


def serialize_schema1_document(document: Mapping[str, object]) -> bytes:
    try:
        canonical = _canonical_document(document)
        payload = json.dumps(
            canonical,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except IntegrationInvalidSource:
        raise
    except Exception:
        raise IntegrationInvalidSource() from None
    if len(payload) > _MAX_DOCUMENT_BYTES:
        raise IntegrationInvalidSource()
    return payload


def _require_integration_directory(cache_path: Path) -> None:
    if not isinstance(cache_path, Path) or not cache_path.is_absolute():
        raise IntegrationSecureIOError()
    try:
        assert_no_symlink_ancestors(cache_path, label="integration cache")
        directory = cache_path.parent
        item = directory.lstat()
    except (OSError, ValueError):
        raise IntegrationSecureIOError() from None
    if (
        cache_path.name != "account-usage-v1.json"
        or directory.name != "integration"
        or not stat.S_ISDIR(item.st_mode)
        or stat.S_IMODE(item.st_mode) != 0o700
        or item.st_uid != os.getuid()
    ):
        raise IntegrationSecureIOError()


def _validate_existing_cache(cache_path: Path) -> None:
    try:
        item = cache_path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise IntegrationSecureIOError() from None
    if (
        not stat.S_ISREG(item.st_mode)
        or stat.S_IMODE(item.st_mode) != 0o600
        or item.st_nlink != 1
        or item.st_uid != os.getuid()
    ):
        raise IntegrationSecureIOError()


def publish_schema1_cache(payload: bytes, *, cache_path: Path) -> None:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_DOCUMENT_BYTES:
        raise IntegrationInvalidSource()
    try:
        parsed = loads_strict(payload)
        canonical = serialize_schema1_document(parsed)
    except IntegrationSnapshotError:
        raise
    except (TypeError, ValueError, UnicodeDecodeError, RecursionError):
        raise IntegrationInvalidSource() from None
    if canonical != payload:
        raise IntegrationInvalidSource()
    _require_integration_directory(cache_path)
    _validate_existing_cache(cache_path)
    try:
        with private_path_lock(
            cache_path,
            timeout_seconds=0,
            label="integration cache lock",
        ):
            _require_integration_directory(cache_path)
            _validate_existing_cache(cache_path)
            write_private_text(
                cache_path,
                payload.decode("utf-8"),
                label="integration cache",
                mode=0o600,
            )
    except TimeoutError:
        raise IntegrationBusy() from None
    except (OSError, TypeError, ValueError):
        raise IntegrationSecureIOError() from None
