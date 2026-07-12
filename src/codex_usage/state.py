from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .account_lock import account_lock
from .config import default_state_dir
from .json_utils import loads_strict
from .models import AccountStatus, AccountUsage, LimitWindow
from .private_io import (
    assert_no_symlink_ancestors,
    private_path_lock,
    read_private_text,
    write_private_text,
)

MAX_SNAPSHOT_BYTES = 1_000_000
SNAPSHOT_ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MAX_SNAPSHOT_TEXT = 500
MAX_SNAPSHOT_URLS = 20
MAX_STATE_GENERATION_BYTES = 4096
AUTHENTICATED_BACKENDS = frozenset(("direct", "app-server"))
WINDOW_DURATIONS = {"five_hour": 18_000, "weekly": 604_800}
KNOWN_FALLBACK_REASONS = frozenset(
    (
        "previous direct limits retained after reset transition",
        "previous authenticated limits retained after reset transition",
    )
)


def backend_provenance_matches_configured(
    usage: AccountUsage,
    configured_backend: str,
) -> bool:
    """Reject authenticated cache data produced by an explicit other backend."""
    if usage.backend_configured and usage.backend_configured != configured_backend:
        return False
    if usage.backend_used not in AUTHENTICATED_BACKENDS:
        return True
    if usage.backend_used == configured_backend:
        return True
    return configured_backend == "app-server" and _has_backend_fallback_proof(usage)


def backend_provenance_matches(left: AccountUsage, right: AccountUsage) -> bool:
    """Avoid merging values across authenticated backends without fallback proof."""
    if (
        left.backend_configured
        and right.backend_configured
        and left.backend_configured != right.backend_configured
    ):
        return False
    left_backend = left.backend_used
    right_backend = right.backend_used
    if "browser" in {left_backend, right_backend}:
        return left_backend == right_backend == "browser"
    if left_backend not in AUTHENTICATED_BACKENDS or right_backend not in AUTHENTICATED_BACKENDS:
        return True
    if left_backend == right_backend:
        return True
    return _has_backend_fallback_proof(left) or _has_backend_fallback_proof(right)


def _has_backend_fallback_proof(usage: AccountUsage) -> bool:
    if usage.backend_used not in AUTHENTICATED_BACKENDS:
        return False
    if usage.fallback_reason in KNOWN_FALLBACK_REASONS:
        return True
    return bool(
        usage.backend_used == "direct"
        and usage.backend_configured == "app-server"
        and usage.fallback_reason
    )


def default_snapshot_dir() -> Path:
    return default_state_dir() / "snapshots"


def default_current_dir() -> Path:
    return default_state_dir() / "current"


def load_state_generation(
    account_id: str,
    directory: Path | None = None,
) -> int:
    _validate_snapshot_account_id(account_id)
    with account_state_lock(account_id):
        return _load_state_generation_unlocked(account_id, directory)


@contextmanager
def account_state_lock(account_id: str) -> Iterator[None]:
    _validate_snapshot_account_id(account_id)
    with account_lock(account_id):
        yield


def _load_state_generation_unlocked(
    account_id: str,
    directory: Path | None = None,
) -> int:
    _validate_snapshot_account_id(account_id)
    generation_path = _state_generation_path(
        account_id,
        directory or default_snapshot_dir(),
    )
    return _read_state_generation(generation_path, account_id)


def save_usage_snapshot(usage: AccountUsage, snapshot_dir: Path | None = None) -> Path:
    _validate_snapshot_account_id(usage.account_id)
    directory = snapshot_dir or default_snapshot_dir()
    assert_no_symlink_ancestors(directory, label="snapshot directory")
    with account_state_lock(usage.account_id):
        return _save_usage(usage, directory, preserve_existing_values=True)


def save_current_usage(usage: AccountUsage, current_dir: Path | None = None) -> Path:
    _validate_snapshot_account_id(usage.account_id)
    directory = current_dir or default_current_dir()
    assert_no_symlink_ancestors(directory, label="snapshot directory")
    with account_state_lock(usage.account_id):
        return _save_usage(usage, directory)


def _save_usage(
    usage: AccountUsage,
    directory: Path,
    *,
    preserve_existing_values: bool = False,
) -> Path:
    _validate_snapshot_account_id(usage.account_id)
    usage = replace(usage, captured_at=_saved_datetime(usage.captured_at))
    assert_no_symlink_ancestors(directory, label="snapshot directory")
    if directory.is_symlink():
        raise ValueError(f"snapshot directory must not be a symlink: {directory}")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"snapshot directory is not a real directory: {directory}")
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    path = directory / f"{usage.account_id}.json"
    current_generation = _read_state_generation(
        _state_generation_path(usage.account_id, directory),
        usage.account_id,
    )
    if usage.state_generation is not None and usage.state_generation != current_generation:
        return path
    if usage.state_generation is None:
        usage = replace(usage, state_generation=current_generation)
    with private_path_lock(path, label="snapshot lock"):
        existing = _load_usage(usage.account_id, directory)
        if existing is not None:
            try:
                if existing.captured_at > usage.captured_at:
                    return path
            except TypeError:
                pass
            if _equal_capture_prefers_existing(existing, usage):
                return path
            if (
                preserve_existing_values
                and not _authoritative_empty_limits(usage)
                and backend_identity_matches(usage, existing)
                and backend_provenance_matches(usage, existing)
            ):
                usage = merge_current_with_last_success(usage, existing)
        payload = usage.as_dict()
        payload["state_generation"] = usage.state_generation
        text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
        if len(text.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError(f"snapshot file too large; max {MAX_SNAPSHOT_BYTES} bytes")
        write_private_text(path, text, label="snapshot path")
    return path


def _equal_capture_prefers_existing(
    existing: AccountUsage,
    incoming: AccountUsage,
) -> bool:
    try:
        if existing.captured_at != incoming.captured_at:
            return False
    except TypeError:
        return False
    existing_priority = _backend_capture_priority(existing)
    incoming_priority = _backend_capture_priority(incoming)
    if existing_priority != incoming_priority:
        return existing_priority > incoming_priority
    if existing.backend_used == incoming.backend_used:
        return False
    return not backend_provenance_matches(existing, incoming)


def _backend_capture_priority(usage: AccountUsage) -> int:
    if usage.backend_used == "browser":
        return 0
    if usage.backend_used in AUTHENTICATED_BACKENDS:
        if usage.backend_configured == usage.backend_used:
            return 2
        return 1
    return -1


def load_usage_snapshot(account_id: str, snapshot_dir: Path | None = None) -> AccountUsage | None:
    return _load_usage(account_id, snapshot_dir or default_snapshot_dir())


def load_current_usage(account_id: str, current_dir: Path | None = None) -> AccountUsage | None:
    return _load_usage(account_id, current_dir or default_current_dir())


def remove_account_state(account_id: str) -> None:
    _validate_snapshot_account_id(account_id)
    with account_state_lock(account_id):
        # Invalidate first so an interrupted cleanup cannot leave the old
        # generation valid for an in-flight writer.
        _increment_state_generation(account_id, default_state_dir())
        targets = (
            (default_snapshot_dir(), f"{account_id}.json", "snapshot path"),
            (default_current_dir(), f"{account_id}.json", "current path"),
            (
                default_state_dir() / "debug",
                f"{account_id}-last-ingest.json",
                "debug path",
            ),
        )
        for directory, filename, label in targets:
            assert_no_symlink_ancestors(directory, label=f"{label} directory")
            if not directory.exists() and not directory.is_symlink():
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError(f"{label} directory must be a real directory: {directory}")
            path = directory / filename
            with private_path_lock(path, label=f"{label} lock"):
                if path.is_dir() and not path.is_symlink():
                    raise ValueError(f"{label} must be a regular file: {path}")
                if path.exists() or path.is_symlink():
                    path.unlink()


def _state_generation_path(account_id: str, directory: Path) -> Path:
    return directory.parent / "generations" / f"{account_id}.json"


def _read_state_generation(path: Path, account_id: str) -> int:
    assert_no_symlink_ancestors(path, label="state generation")
    if not path.exists():
        if path.is_symlink():
            raise ValueError(f"state generation must be a regular file: {path}")
        return 0
    text, _ = read_private_text(
        path,
        regular_label="state generation",
        read_label="state generation",
        max_bytes=MAX_STATE_GENERATION_BYTES,
        too_large_label="state generation",
        invalid_utf8_label="state generation",
    )
    payload = loads_strict(text)
    if not isinstance(payload, dict) or payload.get("account") != account_id:
        raise ValueError(f"state generation account mismatch: {path}")
    generation = payload.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError(f"invalid state generation: {path}")
    return generation


def _increment_state_generation(account_id: str, state_dir: Path) -> int:
    directory = state_dir / "generations"
    assert_no_symlink_ancestors(directory, label="state generation directory")
    if directory.is_symlink():
        raise ValueError(f"state generation directory must not be a symlink: {directory}")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"state generation directory must be a real directory: {directory}")
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    path = directory / f"{account_id}.json"
    generation = _read_state_generation(path, account_id) + 1
    text = json.dumps(
        {"account": account_id, "generation": generation},
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )
    write_private_text(path, text, label="state generation")
    return generation


def _load_usage(account_id: str, directory: Path) -> AccountUsage | None:
    try:
        _validate_snapshot_account_id(account_id)
    except ValueError:
        return None
    path = directory / f"{account_id}.json"
    if not path.exists():
        return None
    try:
        text, _ = read_private_text(
            path,
            regular_label="snapshot path",
            read_label="snapshot file",
            max_bytes=MAX_SNAPSHOT_BYTES,
            too_large_label="snapshot file",
            invalid_utf8_label="snapshot file",
        )
        payload = loads_strict(text)
        if not isinstance(payload, dict):
            return None
        snapshot_account = payload.get("account")
        if not isinstance(snapshot_account, str) or snapshot_account != account_id:
            return None
        usage = usage_from_dict(payload)
        generation = _read_state_generation(
            _state_generation_path(account_id, directory),
            account_id,
        )
        if usage.state_generation is None:
            return usage if generation == 0 else None
        if usage.state_generation != generation:
            return None
        return usage
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def expire_reset_windows(
    usage: AccountUsage,
    *,
    reference_at: datetime,
) -> AccountUsage:
    expired_names: list[str] = []
    five_hour = usage.five_hour
    weekly = usage.weekly
    if _window_reset_expired(five_hour, reference_at):
        expired_names.append("5h")
        five_hour = None
    if _window_reset_expired(weekly, reference_at):
        expired_names.append("weekly")
        weekly = None
    try:
        blocked_until_expired = (
            usage.status == AccountStatus.BLOCKED
            and usage.blocked_until is not None
            and usage.blocked_until <= reference_at
        )
    except TypeError:
        blocked_until_expired = False
    clear_expired_block = (
        usage.status == AccountStatus.BLOCKED
        and not five_hour
        and not weekly
        and (usage.blocked_until is None or blocked_until_expired)
    )
    if not expired_names and not clear_expired_block:
        return usage

    if expired_names:
        names = ", ".join(expired_names)
        error = f"cached limit window expired: {names}; refresh required"
    else:
        error = "cached blocked state expired; refresh required"
    status = usage.status
    blocked_until = usage.blocked_until
    blocked_reason = usage.blocked_reason
    if status == AccountStatus.OK:
        status = AccountStatus.PARTIAL
    elif clear_expired_block:
        status = AccountStatus.PARTIAL
        blocked_until = None
        blocked_reason = None
    return replace(
        usage,
        five_hour=five_hour,
        weekly=weekly,
        status=status,
        error=error,
        blocked_until=blocked_until,
        blocked_reason=blocked_reason,
        stale=True,
    )


def usage_from_dict(payload: dict[str, Any]) -> AccountUsage:
    return AccountUsage(
        account_id=_snapshot_text(payload["account"], limit=64),
        label=_snapshot_text(payload.get("label") or payload["account"], limit=120),
        captured_at=_snapshot_datetime(payload["captured_at"]),
        five_hour=_window_from_dict(payload.get("five_hour")),
        weekly=_window_from_dict(payload.get("weekly")),
        status=AccountStatus(str(payload.get("status", "ok"))),
        error=_optional_snapshot_text(payload.get("error"), limit=MAX_SNAPSHOT_TEXT),
        blocked_until=_optional_datetime(payload.get("blocked_until")),
        blocked_reason=_optional_snapshot_text(
            payload.get("blocked_reason"),
            limit=MAX_SNAPSHOT_TEXT,
        ),
        auth_last_refresh=_optional_datetime(payload.get("auth_last_refresh")),
        auth_access_expires_at=_optional_datetime(payload.get("auth_access_expires_at")),
        auth_id_expires_at=_optional_datetime(payload.get("auth_id_expires_at")),
        source_urls=_snapshot_source_urls(payload.get("source_urls")),
        backend_configured=_optional_snapshot_text(
            payload.get("backend_configured"), limit=40
        ),
        backend_used=_optional_snapshot_text(payload.get("backend_used"), limit=40),
        backend_user_id=_optional_snapshot_text(payload.get("backend_user_id"), limit=256),
        backend_account_id=_optional_snapshot_text(
            payload.get("backend_account_id"), limit=256
        ),
        fallback_reason=_optional_snapshot_text(
            payload.get("fallback_reason"), limit=MAX_SNAPSHOT_TEXT
        ),
        values_captured_at=_optional_datetime(payload.get("values_captured_at")),
        stale=payload.get("stale") is True,
        cache_invalidated=payload.get("cache_invalidated") is True,
        state_generation=_optional_state_generation(payload.get("state_generation")),
    )


def merge_current_with_last_success(
    current: AccountUsage,
    last_success: AccountUsage | None,
) -> AccountUsage:
    if last_success is None:
        return current
    if _authoritative_empty_limits(current):
        return current
    if not backend_identity_matches(current, last_success):
        return current
    if not backend_provenance_matches(current, last_success):
        return current
    try:
        if last_success.captured_at > current.captured_at:
            if _has_complete_usage_windows(last_success):
                return last_success
            return _merge_newer_partial_usage(current, last_success)
    except TypeError:
        pass
    preserve_missing_window_values = _allow_missing_window_restore(current)
    five_hour = _merge_window_with_last_success(
        current.five_hour,
        last_success.five_hour,
        reference_at=current.captured_at,
        expected_kind="five_hour",
        preserve_missing_value=preserve_missing_window_values,
    )
    weekly = _merge_window_with_last_success(
        current.weekly,
        last_success.weekly,
        reference_at=current.captured_at,
        expected_kind="weekly",
        preserve_missing_value=preserve_missing_window_values,
    )
    if five_hour is current.five_hour and weekly is current.weekly:
        return current
    return replace(
        current,
        five_hour=five_hour,
        weekly=weekly,
        values_captured_at=last_success.values_captured_at or last_success.captured_at,
        stale=True,
    )


def _has_complete_usage_windows(usage: AccountUsage) -> bool:
    return bool(
        usage.five_hour is not None
        and usage.weekly is not None
        and usage.five_hour.has_usage_value
        and usage.weekly.has_usage_value
    )


def _merge_newer_partial_usage(
    older: AccountUsage,
    newer: AccountUsage,
) -> AccountUsage:
    preserve_missing_window_values = _allow_missing_window_restore(newer)
    five_hour = _merge_window_with_last_success(
        newer.five_hour,
        older.five_hour,
        reference_at=newer.captured_at,
        expected_kind="five_hour",
        preserve_missing_value=preserve_missing_window_values,
    )
    weekly = _merge_window_with_last_success(
        newer.weekly,
        older.weekly,
        reference_at=newer.captured_at,
        expected_kind="weekly",
        preserve_missing_value=preserve_missing_window_values,
    )
    if five_hour is newer.five_hour and weekly is newer.weekly:
        return newer
    return replace(
        newer,
        five_hour=five_hour,
        weekly=weekly,
        values_captured_at=older.values_captured_at or older.captured_at,
        stale=True,
    )


def _allow_missing_window_restore(usage: AccountUsage) -> bool:
    return not (
        usage.status == AccountStatus.PARTIAL
        and usage.backend_used in AUTHENTICATED_BACKENDS
    )


def _authoritative_empty_limits(usage: AccountUsage) -> bool:
    return (
        usage.status == AccountStatus.PARTIAL
        and usage.five_hour is None
        and usage.weekly is None
        and usage.backend_used in {"direct", "app-server"}
    )


def _merge_window_with_last_success(
    current: LimitWindow | None,
    last_success: LimitWindow | None,
    *,
    reference_at: datetime,
    expected_kind: str | None = None,
    preserve_missing_value: bool = True,
) -> LimitWindow | None:
    if not _window_matches_expected_kind(current, expected_kind):
        return current
    if not _window_matches_expected_kind(last_success, expected_kind):
        return current
    if current is None:
        return (
            last_success
            if preserve_missing_value and not _window_reset_expired(last_success, reference_at)
            else None
        )
    if last_success is None:
        return current
    if not _window_duration_matches(current, last_success):
        return current
    if not preserve_missing_value and not current.has_usage_value:
        return current
    if not current.has_usage_value and _window_reset_expired(current, reference_at):
        # A newer reset-only observation can prove that the old window ended.
        return current
    if current.has_usage_value:
        if current.reset_at is None and last_success.reset_at is not None:
            if _window_reset_expired(last_success, reference_at):
                return current
            return replace(current, reset_at=last_success.reset_at)
        return current
    if _window_reset_expired(last_success, reference_at):
        return current
    if current.reset_at is None:
        return last_success
    return replace(last_success, reset_at=current.reset_at)


def _window_matches_expected_kind(
    window: LimitWindow | None,
    expected_kind: str | None,
) -> bool:
    if window is None or expected_kind is None:
        return True
    kind = _window_kind(window)
    if kind is not None and kind != expected_kind:
        return False
    duration = _window_duration_seconds(window)
    if kind is None and duration is None:
        return False
    expected_duration = WINDOW_DURATIONS.get(expected_kind)
    return (
        expected_duration is None
        or duration is None
        or duration == expected_duration
    )


def _window_duration_matches(
    current: LimitWindow,
    last_success: LimitWindow,
) -> bool:
    current_kind = _window_kind(current)
    previous_kind = _window_kind(last_success)
    if bool(current_kind) != bool(previous_kind):
        return False
    if current_kind and previous_kind and current_kind != previous_kind:
        return False
    current_duration = _window_duration_seconds(current)
    previous_duration = _window_duration_seconds(last_success)
    expected_duration = WINDOW_DURATIONS.get(current_kind or previous_kind or "")
    if expected_duration is not None and any(
        duration is not None and duration != expected_duration
        for duration in (current_duration, previous_duration)
    ):
        return False
    return (
        current_duration is None
        or previous_duration is None
        or current_duration == previous_duration
    )


def _window_kind(window: LimitWindow | None) -> str | None:
    name = getattr(window, "name", None)
    if not isinstance(name, str):
        return None
    normalized = re.sub(r"[-\s]+", "_", name.strip().casefold())
    if normalized in {"5h", "5_hour", "five_hour"}:
        return "five_hour"
    if normalized in {"w", "week", "weekly"}:
        return "weekly"
    return None


def _window_duration_seconds(window: LimitWindow | None) -> int | None:
    raw = getattr(window, "raw", None)
    if not isinstance(raw, str):
        return None
    match = re.search(
        r'"limit_window_seconds"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        raw,
    )
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0 or not value.is_integer():
        return None
    return int(value)


def _window_reset_expired(window: LimitWindow | None, reference_at: datetime) -> bool:
    if window is None or window.reset_at is None:
        return False
    try:
        return window.reset_at <= reference_at
    except TypeError:
        return False


def backend_identity_matches(left: AccountUsage, right: AccountUsage) -> bool:
    left_account_id = left.backend_account_id
    right_account_id = right.backend_account_id
    if bool(left_account_id) != bool(right_account_id):
        return False
    if left_account_id:
        if left_account_id != right_account_id:
            return False
        if left.backend_user_id and right.backend_user_id:
            return left.backend_user_id == right.backend_user_id
        return True

    return left.backend_user_id == right.backend_user_id


def _window_from_dict(payload: dict[str, Any] | None) -> LimitWindow | None:
    if not isinstance(payload, dict):
        return None
    reset_at = payload.get("reset_at")
    return LimitWindow(
        name=_snapshot_text(payload.get("name") or "", limit=40),
        used=_optional_float(payload.get("used")),
        limit=_optional_float(payload.get("limit")),
        remaining=_optional_float(payload.get("remaining")),
        percent=_optional_float(payload.get("percent")),
        reset_at=_snapshot_datetime(reset_at) if reset_at else None,
        raw=_optional_snapshot_text(payload.get("raw"), limit=MAX_SNAPSHOT_TEXT),
        source=_snapshot_text(payload.get("source") or "unknown", limit=120),
    )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return None
    return coerced if math.isfinite(coerced) else None


def _optional_state_generation(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _optional_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _snapshot_datetime(value)
    except ValueError:
        return None


def _snapshot_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.astimezone()
    return parsed


def _saved_datetime(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("captured_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.astimezone()
    return value


def _snapshot_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _optional_snapshot_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return _snapshot_text(value, limit=limit)


def _snapshot_source_urls(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    urls: list[str] = []
    for item in value[:MAX_SNAPSHOT_URLS]:
        if isinstance(item, str):
            urls.append(_snapshot_text(item, limit=300))
    return tuple(urls)


def _validate_snapshot_account_id(account_id: str) -> None:
    if account_id in {".", ".."} or not SNAPSHOT_ACCOUNT_ID_RE.fullmatch(account_id):
        raise ValueError("account id must be 1-64 chars: letters, digits, underscore, dot, dash")
