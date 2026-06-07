from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .models import LimitWindow

LOCAL_TZ = datetime.now().astimezone().tzinfo or ZoneInfo("UTC")

FIVE_HOUR_LABELS = (
    "5 stunden nutzungsgrenze",
    "5-hour usage limit",
    "5 hour usage limit",
    "five hour",
)
WEEKLY_LABELS = (
    "woechentliches nutzungslimit",
    "wöchentliches nutzungslimit",
    "weekly usage limit",
    "week usage",
)


@dataclass(frozen=True)
class JsonCandidate:
    url: str
    payload: Any


def extract_windows(
    *,
    body_text: str,
    json_candidates: Iterable[JsonCandidate] = (),
    now: datetime | None = None,
) -> tuple[LimitWindow | None, LimitWindow | None]:
    captured_at = now or datetime.now(tz=LOCAL_TZ)
    candidates = list(json_candidates)

    five = _extract_json_window(candidates, "five_hour", captured_at)
    weekly = _extract_json_window(candidates, "weekly", captured_at)

    if five is None:
        five = _extract_text_window(
            body_text,
            name="5h",
            labels=FIVE_HOUR_LABELS,
            captured_at=captured_at,
        )
    if weekly is None:
        weekly = _extract_text_window(
            body_text,
            name="weekly",
            labels=WEEKLY_LABELS,
            captured_at=captured_at,
        )
    return five, weekly


def load_json_candidate(url: str, payload_text: str) -> JsonCandidate | None:
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return None
    return JsonCandidate(url=url, payload=payload)


def _extract_json_window(
    candidates: list[JsonCandidate],
    target: str,
    captured_at: datetime,
) -> LimitWindow | None:
    matches: list[tuple[str, str, dict[str, Any], str]] = []
    for candidate in candidates:
        for path, obj in _walk_dicts(candidate.payload):
            haystack = f"{path} {json.dumps(obj, ensure_ascii=False, default=str)}".lower()
            if target == "five_hour" and not _looks_like_five_hour(haystack):
                continue
            if target == "weekly" and not _looks_like_weekly(haystack):
                continue
            if any(word in haystack for word in ("limit", "usage", "nutzung", "reset")):
                matches.append((candidate.url, path, obj, haystack))

    matches.sort(
        key=lambda item: (
            _target_rank(item[1], item[3], target),
            len(_flatten_mapping(item[2])),
        )
    )
    for url, _path, obj, haystack in matches:
        window = _window_from_mapping(
            obj,
            name="5h" if target == "five_hour" else "weekly",
            captured_at=captured_at,
            source=f"json:{url}",
            raw=haystack[:500],
        )
        if window is not None:
            return window
    return None


def _target_rank(path: str, haystack: str, target: str) -> int:
    compact = f"{path} {haystack}".lower()
    if target == "five_hour":
        if any(marker in compact for marker in ("five_hour", "5_hour", "five-hour", "5-hour")):
            return 0
    if target == "weekly" and any(marker in compact for marker in ("weekly", "week", "woche")):
        return 0
    return 1 if path != "$" else 2


def _window_from_mapping(
    obj: dict[str, Any],
    *,
    name: str,
    captured_at: datetime,
    source: str,
    raw: str,
) -> LimitWindow | None:
    flat = _flatten_mapping(obj)
    used = _pick_number(flat, ("used", "usage", "current", "consumed", "num_used"))
    limit = _pick_number(flat, ("limit", "max", "quota", "total", "capacity"))
    remaining = _pick_number(flat, ("remaining", "left", "available"))
    percent = _pick_number(flat, ("percent", "percentage", "ratio"))
    reset_at = _pick_datetime(flat, ("reset", "reset_at", "resets_at", "next_reset"), captured_at)

    if percent is not None and 0 <= percent <= 1:
        percent *= 100
    if remaining is None and used is not None and limit is not None:
        remaining = max(limit - used, 0)
    if percent is None and used is not None and limit:
        percent = used / limit * 100

    if all(value is None for value in (used, limit, remaining, percent, reset_at)):
        return None

    return LimitWindow(
        name=name,
        used=used,
        limit=limit,
        remaining=remaining,
        percent=percent,
        reset_at=reset_at,
        raw=raw,
        source=source,
    )


def _extract_text_window(
    body_text: str,
    *,
    name: str,
    labels: tuple[str, ...],
    captured_at: datetime,
) -> LimitWindow | None:
    text = _normalize_ws(body_text)
    lower = text.lower()
    start = -1
    for label in labels:
        start = lower.find(label)
        if start >= 0:
            break
    if start < 0:
        return None

    chunk = text[start : start + 1500]
    used, limit = _extract_used_limit(chunk)
    percent = _extract_percent(chunk)
    remaining = _extract_remaining(chunk)
    reset_at = _extract_reset_at(chunk, captured_at)

    if remaining is None and used is not None and limit is not None:
        remaining = max(limit - used, 0)
    if percent is None and used is not None and limit:
        percent = used / limit * 100

    return LimitWindow(
        name=name,
        used=used,
        limit=limit,
        remaining=remaining,
        percent=percent,
        reset_at=reset_at,
        raw=chunk[:500],
        source="dom-text",
    )


def _extract_used_limit(text: str) -> tuple[float | None, float | None]:
    patterns = (
        r"(?P<used>\d+(?:[.,]\d+)?)\s*/\s*(?P<limit>\d+(?:[.,]\d+)?)",
        r"(?P<used>\d+(?:[.,]\d+)?)\s+(?:von|of)\s+(?P<limit>\d+(?:[.,]\d+)?)",
        r"(?:used|genutzt|verbraucht)\D{0,40}(?P<used>\d+(?:[.,]\d+)?)\D{0,20}"
        r"(?:limit|max|grenze)\D{0,40}(?P<limit>\d+(?:[.,]\d+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _parse_number(match.group("used")), _parse_number(match.group("limit"))
    return None, None


def _extract_percent(text: str) -> float | None:
    match = re.search(r"(?P<percent>\d+(?:[.,]\d+)?)\s*%", text)
    return _parse_number(match.group("percent")) if match else None


def _extract_remaining(text: str) -> float | None:
    match = re.search(
        r"(?:remaining|left|verbleibend|uebrig|übrig)\D{0,40}(?P<remaining>\d+(?:[.,]\d+)?)",
        text,
        flags=re.IGNORECASE,
    )
    return _parse_number(match.group("remaining")) if match else None


def _extract_reset_at(text: str, captured_at: datetime) -> datetime | None:
    reset_patterns = (
        r"(?:zuruecksetzungen|zurücksetzungen|zuruecksetzung|zurücksetzung|reset(?:s|ting)?"
        r"|wird zurueckgesetzt|wird zurückgesetzt)\D{0,80}"
        r"(?P<date>\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2})",
        r"(?P<date>\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2})",
        r"(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)",
    )
    for pattern in reset_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        raw = match.groupdict().get("date") or match.groupdict().get("iso")
        parsed = _parse_datetime(raw, captured_at)
        if parsed:
            return parsed
    return None


def _pick_number(flat: dict[str, Any], hints: tuple[str, ...]) -> float | None:
    for key, value in flat.items():
        lower = key.lower().rsplit(".", 1)[-1]
        if any(hint in lower for hint in hints):
            number = _coerce_number(value)
            if number is not None:
                return number
    return None


def _pick_datetime(
    flat: dict[str, Any],
    hints: tuple[str, ...],
    captured_at: datetime,
) -> datetime | None:
    for key, value in flat.items():
        lower = key.lower().rsplit(".", 1)[-1]
        if any(hint in lower for hint in hints):
            parsed = _parse_datetime(value, captured_at)
            if parsed:
                return parsed
    return None


def _parse_datetime(value: Any, captured_at: datetime) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=captured_at.tzinfo)
        except (OSError, ValueError):
            return None
    raw = str(value).strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y, %H:%M"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=captured_at.tzinfo)
    try:
        iso = raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=captured_at.tzinfo)
    return parsed.astimezone(captured_at.tzinfo)


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return _parse_number(str(value))


def _parse_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    cleaned = raw.strip().replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _flatten_mapping(obj: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(_flatten_mapping(value, path))
        else:
            flat[path] = value
    return flat


def _walk_dicts(value: Any, path: str = "$") -> Iterable[tuple[str, dict[str, Any]]]:
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from _walk_dicts(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_dicts(child, f"{path}[{index}]")


def _normalize_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _looks_like_five_hour(value: str) -> bool:
    return (
        any(label in value for label in FIVE_HOUR_LABELS)
        or any(word in value for word in ("five_hour", "five-hour", "5_hour", "5-hour"))
        or bool(re.search(r"\b5\b.{0,20}(hour|stunden)", value))
    )


def _looks_like_weekly(value: str) -> bool:
    return any(label in value for label in WEEKLY_LABELS) or any(
        word in value for word in ("weekly", "week_limit", "woche", "woechentlich", "wöchentlich")
    )
