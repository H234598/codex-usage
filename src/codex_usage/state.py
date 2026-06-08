from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import default_state_dir
from .models import AccountStatus, AccountUsage, LimitWindow
from .private_io import read_private_text, write_private_text

MAX_SNAPSHOT_BYTES = 1_000_000
SNAPSHOT_ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def default_snapshot_dir() -> Path:
    return default_state_dir() / "snapshots"


def save_usage_snapshot(usage: AccountUsage, snapshot_dir: Path | None = None) -> Path:
    _validate_snapshot_account_id(usage.account_id)
    directory = snapshot_dir or default_snapshot_dir()
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
    write_private_text(
        path,
        json.dumps(usage.as_dict(), ensure_ascii=False, indent=2),
        label="snapshot path",
    )
    return path


def load_usage_snapshot(account_id: str, snapshot_dir: Path | None = None) -> AccountUsage | None:
    try:
        _validate_snapshot_account_id(account_id)
    except ValueError:
        return None
    path = (snapshot_dir or default_snapshot_dir()) / f"{account_id}.json"
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
        payload = json.loads(text)
        if not isinstance(payload, dict):
            return None
        return usage_from_dict(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def usage_from_dict(payload: dict[str, Any]) -> AccountUsage:
    return AccountUsage(
        account_id=str(payload["account"]),
        label=str(payload.get("label") or payload["account"]),
        captured_at=datetime.fromisoformat(str(payload["captured_at"])),
        five_hour=_window_from_dict(payload.get("five_hour")),
        weekly=_window_from_dict(payload.get("weekly")),
        status=AccountStatus(str(payload.get("status", "ok"))),
        error=payload.get("error"),
        source_urls=tuple(str(item) for item in payload.get("source_urls", [])),
    )


def _window_from_dict(payload: dict[str, Any] | None) -> LimitWindow | None:
    if payload is None:
        return None
    reset_at = payload.get("reset_at")
    return LimitWindow(
        name=str(payload.get("name") or ""),
        used=_optional_float(payload.get("used")),
        limit=_optional_float(payload.get("limit")),
        remaining=_optional_float(payload.get("remaining")),
        percent=_optional_float(payload.get("percent")),
        reset_at=datetime.fromisoformat(reset_at) if reset_at else None,
        raw=payload.get("raw"),
        source=str(payload.get("source") or "unknown"),
    )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _validate_snapshot_account_id(account_id: str) -> None:
    if account_id in {".", ".."} or not SNAPSHOT_ACCOUNT_ID_RE.fullmatch(account_id):
        raise ValueError("account id must be 1-64 chars: letters, digits, underscore, dot, dash")
