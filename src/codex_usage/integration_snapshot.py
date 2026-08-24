from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import NoReturn, cast

from .consumption import (
    TRACKER_EVIDENCE_WINDOW_SECONDS,
    TrackerEvidence,
    calculate_tracker_evidence,
)
from .history import CREDIT_HISTORY_WINDOW_SECONDS, MAX_HISTORY_SAMPLES
from .json_utils import loads_strict
from .models import AccountStatus, AccountUsage, LimitWindow, UsagePool
from .private_io import (
    _recover_stale_rollback,
    assert_no_symlink_ancestors,
    private_path_lock,
    write_private_text,
)
from .state import load_current_usage

_ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_ASCII_TOKEN_RE = re.compile(r"[!-~]{1,128}")
_KEY_RE = re.compile(r"[a-z][a-z0-9_]*")
_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_MAX_ACCOUNTS = 100
_MAX_DIRECTORY_ENTRIES = _MAX_ACCOUNTS * 8
_MAX_LIMITS_PER_ACCOUNT = 32
_MAX_TRACKER_EVIDENCE_PER_ACCOUNT = 32
_MAX_TRACKER_SERIES = _MAX_ACCOUNTS * _MAX_TRACKER_EVIDENCE_PER_ACCOUNT
_MAX_MODEL_POOLS_PER_ACCOUNT = 32
_MAX_AVAILABILITY_SOURCES_PER_POOL = 32
_FRESHNESS_SECONDS = 900
_MAX_RATE_PERCENTAGE_POINTS_PER_SECOND = 100.0
_WINDOW_NAME_SECONDS = {
    "5h": 18_000,
    "weekly": 604_800,
    "30d": 2_592_000,
}
_WINDOW_SECONDS_NAME = {seconds: name for name, seconds in _WINDOW_NAME_SECONDS.items()}
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
_LOCAL_PATH_RE = re.compile(
    r"(?:^|[^A-Za-z0-9/])(?:/+|~/|[A-Za-z]:[\\/]|\\\\)"
)


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
    return name.endswith(".json.lock") or (
        name.startswith(".")
        and (
            ".json.tmp-" in name
            or ".json.rollback-" in name
            or name.endswith(".json.rollback")
        )
    )


def _pool_windows(pool: UsagePool) -> list[dict[str, object]]:
    if (
        type(pool.key) is not str
        or not _ASCII_TOKEN_RE.fullmatch(pool.key[:64])
        or _LOCAL_PATH_RE.search(pool.key)
    ):
        _invalid()
    if (
        len(pool.key) > 64
        or type(pool.windows) is not tuple
        or len(pool.windows) > _MAX_LIMITS_PER_ACCOUNT
    ):
        _invalid()
    if (
        not isinstance(pool.available, bool)
        or (pool.allowed is not None and not isinstance(pool.allowed, bool))
        or (pool.limit_reached is not None and not isinstance(pool.limit_reached, bool))
        or type(pool.availability_sources) is not tuple
        or len(pool.availability_sources) > _MAX_AVAILABILITY_SOURCES_PER_POOL
        or any(
            type(source) is not str
            or len(source) > 64
            or not _ASCII_TOKEN_RE.fullmatch(source)
            for source in pool.availability_sources
        )
    ):
        _invalid()
    if not pool.available:
        return []
    limits: list[dict[str, object]] = []
    for window in pool.windows:
        if not isinstance(window, LimitWindow):
            _invalid()
        try:
            if not window.has_known_identity:
                _invalid()
            duration = window.duration_seconds
            if duration is None:
                duration = _WINDOW_NAME_SECONDS.get(window.name.strip().casefold())
            remaining = window.remaining_percent
        except Exception:
            _invalid()
        if type(duration) is not int or duration not in TRACKER_EVIDENCE_WINDOW_SECONDS:
            _invalid()
        if (
            remaining is None
            or type(remaining) not in (int, float)
        ):
            _invalid()
        try:
            remaining = float(remaining)
        except (OverflowError, TypeError, ValueError):
            _invalid()
        if not math.isfinite(remaining) or not 0 <= remaining <= 100:
            _invalid()
        reset_at = None
        if window.reset_at is not None:
            if not isinstance(window.reset_at, datetime):
                _invalid()
            try:
                reset_at = _utc_text(window.reset_at)
            except IntegrationInvalidSource:
                _invalid()
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
        type(usage.models) is not tuple
        or len(usage.models) > _MAX_MODEL_POOLS_PER_ACCOUNT
    ):
        _invalid()
    pools: list[UsagePool] = []
    if usage.main is not None:
        pools.append(usage.main)
    pools.extend(usage.models)
    if usage.credits is not None:
        if not isinstance(usage.credits, LimitWindow):
            _invalid()
        credit_duration = usage.credits.duration_seconds
        if credit_duration is None:
            credit_duration = CREDIT_HISTORY_WINDOW_SECONDS
        if (
            type(credit_duration) is not int
            or credit_duration not in TRACKER_EVIDENCE_WINDOW_SECONDS
        ):
            _invalid()
        pools.append(
            UsagePool(
                key="credits",
                display_name="Credits",
                windows=(
                    replace(
                        usage.credits,
                        name=_WINDOW_SECONDS_NAME[credit_duration],
                        duration_seconds=credit_duration,
                    ),
                ),
                availability_sources=("usage",),
            )
        )
    seen: set[str] = set()
    limits: list[dict[str, object]] = []
    for pool in pools:
        if not isinstance(pool, UsagePool):
            _invalid()
        if type(pool.key) is not str:
            _invalid()
        if pool.key in seen:
            _invalid()
        seen.add(pool.key)
        limits.extend(_pool_windows(pool))
    if len(limits) > _MAX_LIMITS_PER_ACCOUNT:
        _invalid()
    identities = {
        (item["pool"], item["window_seconds"])
        for item in limits
    }
    if len(identities) != len(limits):
        _invalid()
    limits.sort(key=lambda item: (item["pool"], item["window_seconds"], item.get("reset_at", "")))
    return limits


def _tracker_document(evidence: TrackerEvidence) -> dict[str, object]:
    return {
        "coverage": evidence.coverage,
        "ema_time_constant_seconds": evidence.ema_time_constant_seconds,
        "first_sample_at": _utc_text(evidence.first_sample_at),
        "last_sample_at": _utc_text(evidence.last_sample_at),
        "limit_window_seconds": evidence.limit_window_seconds,
        "pool": evidence.pool,
        "projected_used_percent_at_reset": evidence.projected_used_percent_at_reset,
        "rate_percentage_points_per_second": evidence.rate_percentage_points_per_second,
        "reset_generation": evidence.reset_generation,
        "sample_count": evidence.sample_count,
    }


def _bounded_tracker_evidence(
    samples: object,
    *,
    generated_at: datetime,
) -> tuple[TrackerEvidence | None, tuple[object, ...]]:
    if isinstance(samples, (str, bytes, bytearray)):
        _invalid()
    try:
        bounded = tuple(islice(cast(Iterable[object], samples), MAX_HISTORY_SAMPLES + 1))
    except Exception:
        _invalid()
    if len(bounded) > MAX_HISTORY_SAMPLES:
        _invalid()
    try:
        evidence = calculate_tracker_evidence(bounded, now=generated_at)
    except (TypeError, ValueError):
        _invalid()
    return evidence, bounded


def build_schema2_document(
    usages: Iterable[object],
    *,
    generated_at: datetime,
    tracker_samples: Mapping[tuple[str, str, int], object] | None = None,
) -> dict[str, object]:
    if isinstance(usages, (str, bytes, bytearray)):
        _invalid()
    try:
        bounded_usages = tuple(islice(usages, _MAX_ACCOUNTS + 1))
    except Exception:
        _invalid()
    if len(bounded_usages) > _MAX_ACCOUNTS:
        _invalid()
    generated_text = _utc_text(generated_at)
    generated_utc = datetime.fromisoformat(generated_text.replace("Z", "+00:00"))
    if tracker_samples is not None and (
        type(tracker_samples) is not dict or len(tracker_samples) > _MAX_TRACKER_SERIES
    ):
        _invalid()
    remaining_samples = dict(tracker_samples or {})

    accounts: list[dict[str, object]] = []
    seen: set[str] = set()
    for usage in bounded_usages:
        if not isinstance(usage, AccountUsage):
            _invalid()
        if type(usage.account_id) is not str or not _ACCOUNT_ID_RE.fullmatch(usage.account_id):
            _invalid()
        if usage.account_id in seen or not isinstance(usage.stale, bool):
            _invalid()
        seen.add(usage.account_id)
        status_text = _status_text(usage.status)
        capture = (
            usage.values_captured_at
            if usage.values_captured_at is not None
            else usage.captured_at
        )
        captured_text = _utc_text(capture)
        captured_utc = datetime.fromisoformat(captured_text.replace("Z", "+00:00"))
        if captured_utc > generated_utc:
            _invalid()
        try:
            fresh_until_utc = captured_utc + timedelta(seconds=_FRESHNESS_SECONDS)
        except (OverflowError, ValueError):
            _invalid()
        limits = _source_limits(usage) if status_text in {"ok", "partial"} else []
        limit_by_identity = {
            (cast(str, limit["pool"]), cast(int, limit["window_seconds"])): limit
            for limit in limits
        }
        tracker_evidence: list[dict[str, object]] = []
        for pool, window_seconds in sorted(limit_by_identity):
            key = (usage.account_id, pool, window_seconds)
            if key not in remaining_samples:
                continue
            raw_samples = remaining_samples.pop(key)
            evidence, bounded_samples = _bounded_tracker_evidence(
                raw_samples,
                generated_at=generated_utc,
            )
            if evidence is None:
                continue
            limit = limit_by_identity[(pool, window_seconds)]
            if (
                any(
                    getattr(sample, "account_id", None) != usage.account_id
                    for sample in bounded_samples
                )
                or evidence.pool != pool
                or evidence.limit_window_seconds != window_seconds
            ):
                _invalid()
            reset_at = limit.get("reset_at")
            latest = bounded_samples[-1] if bounded_samples else None
            if (
                reset_at is None
                or latest is None
                or _utc_text(getattr(latest, "reset_at", None)) != reset_at
            ):
                _invalid()
            tracker_evidence.append(_tracker_document(evidence))
        accounts.append(
            {
                "account_id": usage.account_id,
                "freshness": {
                    "captured_at": captured_text,
                    "fresh_until": _utc_text(fresh_until_utc),
                    "stale": usage.stale or generated_utc > fresh_until_utc,
                },
                "limits": limits,
                "status": status_text,
                "tracker_evidence": tracker_evidence,
            }
        )
    if remaining_samples:
        _invalid()
    accounts.sort(key=lambda account: cast(str, account["account_id"]))
    return {
        "accounts": accounts,
        "generated_at": generated_text,
        "schema_version": 2,
    }


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
            or _LOCAL_PATH_RE.search(value)
        ):
            _invalid()
        return
    if value is None or type(value) in (bool, int, float):
        return
    _invalid()


def _canonical_timestamp(value: object) -> str:
    if type(value) is not str or len(value) > 64 or "T" not in value:
        _invalid()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        _invalid()
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _invalid()
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_token(value: object, *, maximum: int) -> str:
    if type(value) is not str or len(value) < 1 or len(value) > maximum:
        _invalid()
    if (
        not value.isascii()
        or not _ASCII_TOKEN_RE.fullmatch(value)
        or _LOCAL_PATH_RE.search(value)
    ):
        _invalid()
    return value


def _canonical_percent(value: object) -> float:
    if type(value) not in (int, float):
        _invalid()
    try:
        result = float(cast(int | float, value))
    except (OverflowError, TypeError, ValueError):
        _invalid()
    if not math.isfinite(result) or not 0 <= result <= 100:
        _invalid()
    return result



def _require_exact_fields(
    value: object,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    if type(value) is not dict:
        _invalid()
    mapping = cast(dict[object, object], value)
    if not len(required) <= len(mapping) <= len(required) + len(optional):
        _invalid()
    if any(type(key) is not str or len(key) > 64 for key in mapping):
        _invalid()
    keys = cast(set[str], set(mapping))
    if not required.issubset(keys) or not keys.issubset(required | optional):
        _invalid()
    return cast(dict[str, object], mapping)


def _canonical_rate(value: object) -> float:
    if type(value) not in (int, float):
        _invalid()
    try:
        result = float(cast(int | float, value))
    except (OverflowError, TypeError, ValueError):
        _invalid()
    if (
        not math.isfinite(result)
        or not 0 <= result <= _MAX_RATE_PERCENTAGE_POINTS_PER_SECOND
    ):
        _invalid()
    return result


def _canonical_limit_v2(value: object) -> dict[str, object]:
    mapping = _require_exact_fields(
        value,
        required=frozenset(
            ("pool", "window_seconds", "used_percent", "remaining_percent")
        ),
        optional=frozenset(("reset_at",)),
    )
    window_seconds = mapping["window_seconds"]
    if type(window_seconds) is not int or window_seconds not in TRACKER_EVIDENCE_WINDOW_SECONDS:
        _invalid()
    used_percent = _canonical_percent(mapping["used_percent"])
    remaining_percent = _canonical_percent(mapping["remaining_percent"])
    if not math.isclose(used_percent + remaining_percent, 100.0, abs_tol=1e-9):
        _invalid()
    result: dict[str, object] = {
        "pool": _canonical_token(mapping["pool"], maximum=64),
        "remaining_percent": remaining_percent,
        "used_percent": used_percent,
        "window_seconds": window_seconds,
    }
    if "reset_at" in mapping:
        result["reset_at"] = _canonical_timestamp(mapping["reset_at"])
    return result


def _canonical_tracker_evidence(value: object) -> dict[str, object]:
    mapping = _require_exact_fields(
        value,
        required=frozenset(
            (
                "coverage",
                "ema_time_constant_seconds",
                "first_sample_at",
                "last_sample_at",
                "limit_window_seconds",
                "pool",
                "projected_used_percent_at_reset",
                "rate_percentage_points_per_second",
                "reset_generation",
                "sample_count",
            )
        ),
    )
    coverage = mapping["coverage"]
    if type(coverage) is not str or coverage not in {
        "complete",
        "partial",
        "insufficient",
        "stale",
    }:
        _invalid()
    if (
        type(mapping["ema_time_constant_seconds"]) is not int
        or mapping["ema_time_constant_seconds"] != 3_600
    ):
        _invalid()
    window_seconds = mapping["limit_window_seconds"]
    if type(window_seconds) is not int or window_seconds not in TRACKER_EVIDENCE_WINDOW_SECONDS:
        _invalid()
    sample_count = mapping["sample_count"]
    if type(sample_count) is not int or not 1 <= sample_count <= MAX_HISTORY_SAMPLES:
        _invalid()
    if (coverage == "insufficient") != (sample_count == 1):
        _invalid()
    first_sample_at = _canonical_timestamp(mapping["first_sample_at"])
    last_sample_at = _canonical_timestamp(mapping["last_sample_at"])
    first_sample_time = datetime.fromisoformat(first_sample_at.replace("Z", "+00:00"))
    last_sample_time = datetime.fromisoformat(last_sample_at.replace("Z", "+00:00"))
    if (
        first_sample_time > last_sample_time
        or (sample_count == 1 and first_sample_time != last_sample_time)
        or (sample_count > 1 and first_sample_time >= last_sample_time)
    ):
        _invalid()
    return {
        "coverage": coverage,
        "ema_time_constant_seconds": 3_600,
        "first_sample_at": first_sample_at,
        "last_sample_at": last_sample_at,
        "limit_window_seconds": window_seconds,
        "pool": _canonical_token(mapping["pool"], maximum=64),
        "projected_used_percent_at_reset": _canonical_percent(
            mapping["projected_used_percent_at_reset"]
        ),
        "rate_percentage_points_per_second": _canonical_rate(
            mapping["rate_percentage_points_per_second"]
        ),
        "reset_generation": _canonical_token(mapping["reset_generation"], maximum=128),
        "sample_count": sample_count,
    }


def _canonical_document_v2(document: object) -> dict[str, object]:
    mapping = _require_exact_fields(
        document,
        required=frozenset(("accounts", "generated_at", "schema_version")),
    )
    if type(mapping["schema_version"]) is not int or mapping["schema_version"] != 2:
        _invalid()
    generated_at = _canonical_timestamp(mapping["generated_at"])
    accounts_value = mapping["accounts"]
    if type(accounts_value) is not list or len(accounts_value) > _MAX_ACCOUNTS:
        _invalid()
    accounts: list[dict[str, object]] = []
    account_ids: set[str] = set()
    for raw_account in accounts_value:
        source = _require_exact_fields(
            raw_account,
            required=frozenset(
                ("account_id", "freshness", "limits", "status", "tracker_evidence")
            ),
        )
        account_id = _canonical_token(source["account_id"], maximum=64)
        if not _ACCOUNT_ID_RE.fullmatch(account_id) or account_id in account_ids:
            _invalid()
        status = source["status"]
        if type(status) is not str or status not in _SCHEMA_STATUSES:
            _invalid()
        freshness = _require_exact_fields(
            source["freshness"],
            required=frozenset(("captured_at", "fresh_until", "stale")),
        )
        if type(freshness["stale"]) is not bool:
            _invalid()
        captured_at = _canonical_timestamp(freshness["captured_at"])
        fresh_until = _canonical_timestamp(freshness["fresh_until"])
        captured_time = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        fresh_until_time = datetime.fromisoformat(fresh_until.replace("Z", "+00:00"))
        generated_time = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if (
            captured_time > generated_time
            or fresh_until_time
            != captured_time + timedelta(seconds=_FRESHNESS_SECONDS)
        ):
            _invalid()
        if generated_time > fresh_until_time and freshness["stale"] is not True:
            _invalid()

        limits_value = source["limits"]
        if type(limits_value) is not list or len(limits_value) > _MAX_LIMITS_PER_ACCOUNT:
            _invalid()
        limits = [_canonical_limit_v2(limit) for limit in limits_value]
        limit_identities = {
            (item["pool"], item["window_seconds"])
            for item in limits
        }
        if len(limit_identities) != len(limits):
            _invalid()

        evidence_value = source["tracker_evidence"]
        if (
            type(evidence_value) is not list
            or len(evidence_value) > _MAX_TRACKER_EVIDENCE_PER_ACCOUNT
        ):
            _invalid()
        evidence = [_canonical_tracker_evidence(item) for item in evidence_value]
        evidence_identities = {
            (item["pool"], item["limit_window_seconds"])
            for item in evidence
        }
        if len(evidence_identities) != len(evidence):
            _invalid()
        limits_with_reset = {
            (item["pool"], item["window_seconds"]): datetime.fromisoformat(
                cast(str, item["reset_at"]).replace("Z", "+00:00")
            )
            for item in limits
            if "reset_at" in item
        }
        if not evidence_identities.issubset(limits_with_reset.keys()):
            _invalid()
        if status not in {"ok", "partial"} and (limits or evidence):
            _invalid()
        for item in evidence:
            last_sample_at = datetime.fromisoformat(
                cast(str, item["last_sample_at"]).replace("Z", "+00:00")
            )
            identity = (item["pool"], item["limit_window_seconds"])
            if (
                last_sample_at > captured_time
                or limits_with_reset[identity] <= last_sample_at
                or limits_with_reset[identity] <= generated_time
            ):
                _invalid()
        account_ids.add(account_id)
        accounts.append(
            {
                "account_id": account_id,
                "freshness": {
                    "captured_at": captured_at,
                    "fresh_until": fresh_until,
                    "stale": freshness["stale"],
                },
                "limits": sorted(
                    limits,
                    key=lambda item: (item["pool"], item["window_seconds"]),
                ),
                "status": status,
                "tracker_evidence": sorted(
                    evidence,
                    key=lambda item: (item["pool"], item["limit_window_seconds"]),
                ),
            }
        )
    result: dict[str, object] = {
        "accounts": sorted(accounts, key=lambda item: cast(str, item["account_id"])),
        "generated_at": generated_at,
        "schema_version": 2,
    }
    _scan_secrets(result)
    return result


def serialize_schema2_document(document: Mapping[str, object]) -> bytes:
    try:
        canonical = _canonical_document_v2(document)
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


def publish_schema2_cache(payload: bytes, *, cache_path: Path) -> None:
    if type(payload) is not bytes or not payload or len(payload) > _MAX_DOCUMENT_BYTES:
        raise IntegrationInvalidSource()
    try:
        parsed = loads_strict(payload)
        canonical = serialize_schema2_document(parsed)
    except IntegrationSnapshotError:
        raise
    except (TypeError, ValueError, UnicodeDecodeError, RecursionError):
        raise IntegrationInvalidSource() from None
    if canonical != payload:
        raise IntegrationInvalidSource()
    _require_integration_directory(cache_path)
    try:
        with private_path_lock(
            cache_path,
            timeout_seconds=0,
            label="integration cache lock",
        ):
            _require_integration_directory(cache_path)
            _recover_stale_rollback(
                cache_path,
                label="integration cache",
                required_target_mode=0o600,
            )
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
