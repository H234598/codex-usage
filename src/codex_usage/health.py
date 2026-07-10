from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import default_state_dir
from .json_utils import loads_strict
from .private_io import private_path_lock, read_private_text, write_private_text

HEALTH_FILENAME = "health.json"
HEALTH_VERSION = 1
MAX_HEALTH_EVENTS = 128
MAX_HEALTH_BYTES = 256 * 1024
HEALTH_RETENTION = timedelta(days=30)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def default_health_path() -> Path:
    return default_state_dir() / HEALTH_FILENAME


def record_health_event(
    component: str,
    event: str,
    *,
    account: str | None = None,
    duration_ms: int | None = None,
    error_class: str | None = None,
    path: Path | None = None,
    now: datetime | None = None,
) -> None:
    health_path = path or default_health_path()
    _prepare_health_directory(health_path.parent)
    current_time = now or datetime.now(UTC)
    entry: dict[str, Any] = {
        "at": current_time.astimezone(UTC).isoformat(),
        "component": _safe_token(component, "unknown"),
        "event": _safe_token(event, "unknown"),
    }
    if account and _TOKEN_RE.fullmatch(account):
        entry["account"] = account
    if (
        duration_ms is not None
        and isinstance(duration_ms, int)
        and not isinstance(duration_ms, bool)
    ):
        entry["duration_ms"] = max(0, min(duration_ms, 86_400_000))
    if error_class:
        entry["error_class"] = _safe_token(error_class, "Error")

    with private_path_lock(health_path, label="health lock"):
        events = _read_events(health_path)
        events.append(entry)
        events = _trim_events(events, current_time)
        _write_events(health_path, events)


def load_health(path: Path | None = None) -> dict[str, Any]:
    health_path = path or default_health_path()
    events = _read_events(health_path)
    counts: dict[str, int] = {}
    for event in events:
        key = f"{event['component']}:{event['event']}"
        counts[key] = counts.get(key, 0) + 1
    return {
        "version": HEALTH_VERSION,
        "event_count": len(events),
        "event_counts": counts,
        "events": events,
    }


def clear_health(path: Path | None = None) -> None:
    health_path = path or default_health_path()
    _prepare_health_directory(health_path.parent)
    with private_path_lock(health_path, label="health lock"):
        _write_events(health_path, [])


def _prepare_health_directory(directory: Path) -> None:
    if directory.is_symlink():
        raise ValueError(f"health directory must not be a symlink: {directory}")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"health directory must be a real directory: {directory}")
    try:
        directory.chmod(0o700)
    except OSError:
        pass


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        text, _ = read_private_text(
            path,
            regular_label="health path",
            read_label="health file",
            max_bytes=MAX_HEALTH_BYTES,
            too_large_label="health file",
            invalid_utf8_label="health file",
        )
        payload = loads_strict(text)
        if not isinstance(payload, dict) or payload.get("version") != HEALTH_VERSION:
            return []
        raw_events = payload.get("events")
        if not isinstance(raw_events, list):
            return []
        return [event for event in raw_events if _valid_event(event)][-MAX_HEALTH_EVENTS:]
    except (OSError, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        return []


def _valid_event(event: object) -> bool:
    if not isinstance(event, dict):
        return False
    if not isinstance(event.get("at"), str):
        return False
    if not isinstance(event.get("component"), str) or not _TOKEN_RE.fullmatch(event["component"]):
        return False
    if not isinstance(event.get("event"), str) or not _TOKEN_RE.fullmatch(event["event"]):
        return False
    if "account" in event and (
        not isinstance(event["account"], str) or not _TOKEN_RE.fullmatch(event["account"])
    ):
        return False
    if "duration_ms" in event and (
        isinstance(event["duration_ms"], bool)
        or not isinstance(event["duration_ms"], int)
        or not 0 <= event["duration_ms"] <= 86_400_000
    ):
        return False
    if "error_class" in event and (
        not isinstance(event["error_class"], str) or not _TOKEN_RE.fullmatch(event["error_class"])
    ):
        return False
    try:
        datetime.fromisoformat(event["at"])
    except ValueError:
        return False
    return True


def _trim_events(events: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    cutoff = now.astimezone(UTC) - HEALTH_RETENTION
    kept: list[dict[str, Any]] = []
    for event in events:
        try:
            timestamp = datetime.fromisoformat(event["at"])
        except (KeyError, TypeError, ValueError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        if timestamp >= cutoff:
            kept.append(event)
    return kept[-MAX_HEALTH_EVENTS:]


def _write_events(path: Path, events: list[dict[str, Any]]) -> None:
    while True:
        payload = json.dumps(
            {"version": HEALTH_VERSION, "events": events},
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        if len(payload.encode("utf-8")) <= MAX_HEALTH_BYTES or not events:
            write_private_text(path, payload, label="health path")
            return
        events = events[1:]


def _safe_token(value: str, fallback: str) -> str:
    candidate = str(value).strip()
    return candidate[:64] if _TOKEN_RE.fullmatch(candidate) else fallback
