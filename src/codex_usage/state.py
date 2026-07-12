from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

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


def default_snapshot_dir() -> Path:
    return default_state_dir() / "snapshots"


def default_current_dir() -> Path:
    return default_state_dir() / "current"


def save_usage_snapshot(usage: AccountUsage, snapshot_dir: Path | None = None) -> Path:
    return _save_usage(
        usage,
        snapshot_dir or default_snapshot_dir(),
        preserve_existing_values=True,
    )


def save_current_usage(usage: AccountUsage, current_dir: Path | None = None) -> Path:
    return _save_usage(usage, current_dir or default_current_dir())


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
    with private_path_lock(path, label="snapshot lock"):
        existing = _load_usage(usage.account_id, directory)
        if existing is not None:
            try:
                if existing.captured_at > usage.captured_at:
                    return path
            except TypeError:
                pass
            if (
                preserve_existing_values
                and not _authoritative_empty_limits(usage)
                and backend_identity_matches(usage, existing)
            ):
                usage = merge_current_with_last_success(usage, existing)
        text = json.dumps(usage.as_dict(), ensure_ascii=False, indent=2, allow_nan=False)
        if len(text.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError(f"snapshot file too large; max {MAX_SNAPSHOT_BYTES} bytes")
        write_private_text(path, text, label="snapshot path")
    return path


def load_usage_snapshot(account_id: str, snapshot_dir: Path | None = None) -> AccountUsage | None:
    return _load_usage(account_id, snapshot_dir or default_snapshot_dir())


def load_current_usage(account_id: str, current_dir: Path | None = None) -> AccountUsage | None:
    return _load_usage(account_id, current_dir or default_current_dir())


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
        return usage_from_dict(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


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
    try:
        if last_success.captured_at > current.captured_at:
            return last_success
    except TypeError:
        pass
    five_hour = _merge_window_with_last_success(
        current.five_hour,
        last_success.five_hour,
        reference_at=current.captured_at,
    )
    weekly = _merge_window_with_last_success(
        current.weekly,
        last_success.weekly,
        reference_at=current.captured_at,
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
) -> LimitWindow | None:
    if current is None:
        return None if _window_reset_expired(last_success, reference_at) else last_success
    if last_success is None:
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
    if payload is None:
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
