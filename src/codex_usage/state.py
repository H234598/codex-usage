from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import default_state_dir
from .models import AccountStatus, AccountUsage, LimitWindow


def default_snapshot_dir() -> Path:
    return default_state_dir() / "snapshots"


def save_usage_snapshot(usage: AccountUsage, snapshot_dir: Path | None = None) -> Path:
    directory = snapshot_dir or default_snapshot_dir()
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / f"{usage.account_id}.json"
    path.write_text(json.dumps(usage.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def load_usage_snapshot(account_id: str, snapshot_dir: Path | None = None) -> AccountUsage | None:
    path = (snapshot_dir or default_snapshot_dir()) / f"{account_id}.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return usage_from_dict(payload)


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
