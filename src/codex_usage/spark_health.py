from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import default_state_dir
from .identity import MAX_BACKEND_ID_CHARS
from .json_utils import loads_strict
from .private_io import (
    ensure_private_directory,
    private_path_lock,
    read_private_text,
    write_private_text,
)

SPARK_HEALTH_FILENAME = "spark-health.json"
SPARK_HEALTH_VERSION = 1
SPARK_HEALTH_MAX_BYTES = 128 * 1024
SPARK_HEALTH_MAX_RECORDS = 256
SPARK_HEALTH_MAX_AGE_SECONDS = 3600
SPARK_HEALTH_STATES = ("healthy", "failed")
_IDENTIFIER_RE = re.compile(
    rf"^[A-Za-z0-9_.:@+/-]{{1,{MAX_BACKEND_ID_CHARS}}}$"
)


def default_spark_health_path() -> Path:
    return default_state_dir() / SPARK_HEALTH_FILENAME


def spark_health_status(
    backend_account_id: str | None,
    *,
    path: Path | None = None,
    now: datetime | None = None,
    max_age_seconds: int = SPARK_HEALTH_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    checked_at = now if now is not None else datetime.now(UTC)
    if (
        not isinstance(max_age_seconds, int)
        or isinstance(max_age_seconds, bool)
        or max_age_seconds < 60
    ):
        raise ValueError("max_age_seconds must be at least 60")
    if not _aware_datetime(checked_at):
        return _unknown_health("invalid_health_clock")
    if not isinstance(backend_account_id, str) or not _IDENTIFIER_RE.fullmatch(backend_account_id):
        return _unknown_health("missing_backend_account_id")
    records = _load_records(_spark_health_path(path))
    record = records.get(_health_key(backend_account_id))
    if not isinstance(record, dict):
        return _unknown_health("no_successful_spark_turn")
    state = record.get("state")
    timestamp = _parse_timestamp(record.get("checked_at"))
    if state == "failed":
        return {
            "state": "failed",
            "reason": _safe_reason(record.get("reason")) or "spark_turn_failed",
            "checked_at": timestamp.isoformat() if timestamp else None,
            "stale": False,
        }
    if state != "healthy" or timestamp is None:
        return _unknown_health("invalid_spark_health_record")
    try:
        age = (checked_at.astimezone(UTC) - timestamp.astimezone(UTC)).total_seconds()
    except (OverflowError, TypeError, ValueError):
        return _unknown_health("invalid_spark_health_record")
    if age < -300 or age > max_age_seconds:
        return {
            "state": "unknown",
            "reason": "spark_health_stale",
            "checked_at": timestamp.isoformat(),
            "stale": True,
        }
    return {
        "state": "healthy",
        "reason": "successful_spark_turn",
        "checked_at": timestamp.isoformat(),
        "stale": False,
    }


def set_spark_health(
    backend_account_id: str,
    state: str,
    *,
    reason: str | None = None,
    path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(backend_account_id, str) or not _IDENTIFIER_RE.fullmatch(backend_account_id):
        raise ValueError("backend_account_id is invalid")
    if state not in SPARK_HEALTH_STATES:
        raise ValueError("spark health state must be healthy or failed")
    current_time = now if now is not None else datetime.now(UTC)
    if not _aware_datetime(current_time):
        raise ValueError("spark health timestamp must be timezone-aware")
    health_path = _spark_health_path(path)
    _prepare_health_directory(health_path.parent)
    try:
        checked_at = current_time.astimezone(UTC).isoformat()
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("spark health timestamp is out of range") from exc
    record = {
        "state": state,
        "checked_at": checked_at,
        "reason": _safe_reason(reason) or (
            "successful_spark_turn" if state == "healthy" else "spark_turn_failed"
        ),
    }
    with private_path_lock(health_path, label="spark health lock"):
        records = _load_records(health_path)
        record_key = _health_key(backend_account_id)
        records.pop(record_key, None)
        records[record_key] = record
        records = dict(list(records.items())[-SPARK_HEALTH_MAX_RECORDS:])
        payload = json.dumps(
            {"version": SPARK_HEALTH_VERSION, "records": records},
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ) + "\n"
        if len(payload.encode("utf-8")) > SPARK_HEALTH_MAX_BYTES:
            raise ValueError("spark health file is too large")
        write_private_text(health_path, payload, label="spark health path")
    return {"state": state, "reason": record["reason"], "checked_at": record["checked_at"]}


def _health_key(backend_account_id: str) -> str:
    return hashlib.sha256(backend_account_id.encode("utf-8")).hexdigest()


def _load_records(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        if path.is_symlink():
            raise ValueError("spark health path must be a regular file")
        return {}
    try:
        text, file_stat = read_private_text(
            path,
            regular_label="spark health path",
            read_label="spark health file",
            max_bytes=SPARK_HEALTH_MAX_BYTES,
        )
        if file_stat.st_nlink != 1 or file_stat.st_mode & 0o077:
            return {}
        payload = loads_strict(text)
    except (OSError, RecursionError, TypeError, ValueError, UnicodeDecodeError):
        return {}
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload["version"] != SPARK_HEALTH_VERSION
    ):
        return {}
    raw_records = payload.get("records")
    if not isinstance(raw_records, dict):
        return {}
    if len(raw_records) > SPARK_HEALTH_MAX_RECORDS:
        return {}
    return {
        key: value
        for key, value in raw_records.items()
        if isinstance(key, str)
        and re.fullmatch(r"[0-9a-f]{64}", key)
        and isinstance(value, dict)
        and value.get("state") in SPARK_HEALTH_STATES
        and isinstance(value.get("checked_at"), str)
    }


def _prepare_health_directory(directory: Path) -> None:
    ensure_private_directory(directory, label="spark health directory")


def _spark_health_path(path: object | None) -> Path:
    if path is None:
        return default_spark_health_path()
    if not isinstance(path, Path):
        raise ValueError("spark health path is invalid")
    return path


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _aware_datetime(value: Any) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    try:
        return value.utcoffset() is not None
    except (OverflowError, TypeError, ValueError):
        return False


def _safe_reason(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:120] if re.fullmatch(r"[A-Za-z0-9_.:@+/\- ]{1,120}", cleaned) else None


def _unknown_health(reason: str) -> dict[str, Any]:
    return {"state": "unknown", "reason": reason, "checked_at": None, "stale": False}
