from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .json_utils import loads_strict
from .models import LimitWindow

LOCAL_TZ = datetime.now().astimezone().tzinfo or ZoneInfo("UTC")
MAX_JSON_WALK_DEPTH = 24
MAX_JSON_WALK_ITEMS = 1000
MAX_JSON_FLATTEN_FIELDS = 2000

FIVE_HOUR_LABELS = (
    "5 stunden nutzungsgrenze",
    "5 stunden limit",
    "5-stunden nutzungsgrenze",
    "5-stunden-nutzungsgrenze",
    "5-stunden limit",
    "5-stunden-limit",
    "5-hour usage limit",
    "5-hour limit",
    "5 hour usage limit",
    "5 hour limit",
    "5 hours usage limit",
    "5 hours limit",
    "5h usage limit",
    "5h limit",
    "five hour",
    "five-hour",
)
WEEKLY_LABELS = (
    "woechentliches nutzungslimit",
    "wöchentliches nutzungslimit",
    "woechentliches limit",
    "wöchentliches limit",
    "wochenlimit",
    "wochen limit",
    "weekly usage limit",
    "weekly limit",
    "weekly usage",
    "week usage",
    "week limit",
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
            stop_labels=WEEKLY_LABELS,
            captured_at=captured_at,
        )
    if weekly is None:
        weekly = _extract_text_window(
            body_text,
            name="weekly",
            labels=WEEKLY_LABELS,
            stop_labels=FIVE_HOUR_LABELS,
            captured_at=captured_at,
        )
    return five, weekly


def load_json_candidate(url: str, payload_text: str) -> JsonCandidate | None:
    try:
        payload = loads_strict(payload_text)
    except ValueError:
        return None
    return JsonCandidate(url=url, payload=payload)


def _extract_json_window(
    candidates: list[JsonCandidate],
    target: str,
    captured_at: datetime,
) -> LimitWindow | None:
    matches: list[tuple[str, str, dict[str, Any], str]] = []
    reset_only: LimitWindow | None = None
    for candidate in candidates:
        for path, obj in _walk_dicts(candidate.payload):
            obj_preview = _json_preview(obj)
            wham_window = _window_from_wham_rate_limit_mapping(
                obj,
                target=target,
                captured_at=captured_at,
                source=f"json:{candidate.url}",
                raw=f"{path} {obj_preview}"[:500],
            )
            if wham_window is not None:
                if wham_window.has_usage_value:
                    return wham_window
                if reset_only is None:
                    reset_only = wham_window
                continue
            haystack = f"{path} {obj_preview}".lower()
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
            if window.has_usage_value:
                return window
            if reset_only is None:
                reset_only = window
    return reset_only


def _window_from_wham_rate_limit_mapping(
    obj: dict[str, Any],
    *,
    target: str,
    captured_at: datetime,
    source: str,
    raw: str,
) -> LimitWindow | None:
    window_seconds = _coerce_number(obj.get("limit_window_seconds"))
    if target == "five_hour" and window_seconds != 18_000:
        return None
    if target == "weekly" and window_seconds != 604_800:
        return None

    used_percent = _coerce_percent(obj.get("used_percent"))
    reset_at = _parse_datetime(obj.get("reset_at"), captured_at)
    if used_percent is None and reset_at is None:
        return None

    remaining_percent = max(100 - used_percent, 0) if used_percent is not None else None
    return LimitWindow(
        name="5h" if target == "five_hour" else "weekly",
        used=used_percent,
        limit=100 if used_percent is not None else None,
        remaining=remaining_percent,
        percent=remaining_percent,
        reset_at=reset_at,
        raw=raw,
        source=source,
    )


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
    used_percent = _pick_number(
        flat,
        ("used_percent", "usage_percent", "consumed_percent"),
    )
    used = _pick_number(
        flat,
        ("used", "usage", "current", "consumed", "num_used"),
        exclude_suffixes=("_percent",),
    )
    limit = _pick_number(flat, ("limit", "max", "quota", "total", "capacity"))
    remaining = _pick_number(flat, ("remaining", "left", "available"))
    percent = _pick_number(flat, ("percent", "percentage", "ratio"))
    reset_at = _pick_datetime(flat, ("reset", "reset_at", "resets_at", "next_reset"), captured_at)

    if percent is not None and 0 <= percent <= 1:
        percent *= 100
    if used_percent is not None and 0 <= used_percent <= 1:
        used_percent *= 100
    if used_percent is not None and not 0 <= used_percent <= 100:
        used_percent = None
    if remaining is None and used is not None and limit is not None:
        remaining = max(limit - used, 0)
    if used_percent is not None and used is None:
        if remaining is None:
            remaining = max(100 - used_percent, 0)
        percent = remaining
    if percent is None and used is not None and limit:
        percent = used / limit * 100
    if percent is not None and not 0 <= percent <= 100:
        percent = None

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
    stop_labels: tuple[str, ...],
    captured_at: datetime,
) -> LimitWindow | None:
    text = _normalize_ws(body_text)
    lower = text.lower()
    reset_only: LimitWindow | None = None
    for start in _label_offsets(lower, labels):
        end = _next_label_offset(lower, start + 1, stop_labels)
        chunk_end = min(start + 1500, end) if end is not None else start + 1500
        chunk = text[start:chunk_end]
        used, limit = _extract_used_limit(chunk)
        progress_percent = _extract_progress_width_percent(chunk)
        percent = _extract_percent(chunk)
        remaining = _extract_remaining(chunk)
        used_percent = _extract_used_percent(chunk)
        reset_at = _extract_reset_at(chunk, captured_at)

        if remaining is None and used_percent is not None:
            remaining = max(100 - used_percent, 0)
        if percent is None and progress_percent is not None:
            percent = progress_percent
        if remaining is None and progress_percent is not None:
            remaining = progress_percent

        if (
            remaining is not None
            and used is None
            and limit is None
            and 0 <= remaining <= 100
        ):
            percent = remaining

        if all(value is None for value in (used, limit, remaining, percent, reset_at)):
            continue

        if remaining is None and used is not None and limit is not None:
            remaining = max(limit - used, 0)
        if percent is None and used is not None and limit:
            percent = used / limit * 100

        window = LimitWindow(
            name=name,
            used=used,
            limit=limit,
            remaining=remaining,
            percent=percent,
            reset_at=reset_at,
            raw=chunk[:500],
            source="dom-text",
        )
        if window.has_usage_value:
            return window
        if reset_only is None:
            reset_only = window
    return reset_only


def _label_offsets(text: str, labels: tuple[str, ...]) -> list[int]:
    offsets: set[int] = set()
    for label in labels:
        start = 0
        while True:
            index = text.find(label, start)
            if index < 0:
                break
            offsets.add(index)
            start = index + max(len(label), 1)
    return sorted(offsets)


def _next_label_offset(text: str, start: int, labels: tuple[str, ...]) -> int | None:
    offsets = [index for label in labels if (index := text.find(label, start)) >= 0]
    return min(offsets) if offsets else None


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
    return _parse_percent(match.group("percent")) if match else None


def _extract_used_percent(text: str) -> float | None:
    patterns = (
        r"(?P<used>\d+(?:[.,]\d+)?)\s*%\s*(?:used|genutzt|verbraucht)",
        r"(?:used|genutzt|verbraucht)\D{0,10}(?P<used>\d+(?:[.,]\d+)?)\s*%",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _parse_percent(match.group("used"))
    return None


def _extract_progress_width_percent(text: str) -> float | None:
    patterns = (
        r"\bstyle=[\"'][^\"']*\bwidth\s*:\s*(?P<percent>\d+(?:[.,]\d+)?)\s*%",
        r"\bwidth\s*:\s*(?P<percent>\d+(?:[.,]\d+)?)\s*%",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _parse_percent(match.group("percent"))
    return None


def _extract_remaining(text: str) -> float | None:
    patterns = (
        r"(?P<remaining>\d+(?:[.,]\d+)?)\s*%?\s*"
        r"(?:remaining|left|verbleibend|uebrig|übrig)",
        r"(?:remaining|left|verbleibend|uebrig|übrig)\s+"
        r"(?P<remaining>\d+(?:[.,]\d+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _parse_number(match.group("remaining"))
    return None


def _extract_reset_at(text: str, captured_at: datetime) -> datetime | None:
    reset_patterns = (
        r"(?:zuruecksetzungen|zurücksetzungen|zuruecksetzung|zurücksetzung|reset(?:s|ting)?"
        r"|wird zurueckgesetzt|wird zurückgesetzt)\D{0,80}"
        r"(?P<date>\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2})",
        r"(?:zuruecksetzungen|zurücksetzungen|zuruecksetzung|zurücksetzung|reset(?:s|ting)?"
        r"|wird zurueckgesetzt|wird zurückgesetzt)\D{0,80}"
        r"(?P<time>\d{1,2}:\d{2})",
        r"(?P<date>\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2})",
        r"(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)",
    )
    for pattern in reset_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        groups = match.groupdict()
        raw = groups.get("date") or groups.get("iso")
        if groups.get("time"):
            parsed = _parse_time_today_or_next(groups["time"], captured_at)
            if parsed:
                return parsed
        parsed = _parse_datetime(raw, captured_at)
        if parsed:
            return parsed
    return None


def _pick_number(
    flat: dict[str, Any],
    hints: tuple[str, ...],
    *,
    exclude_suffixes: tuple[str, ...] = (),
) -> float | None:
    for hint in hints:
        for key, value in flat.items():
            lower = key.lower().rsplit(".", 1)[-1]
            if lower.endswith(exclude_suffixes):
                continue
            if lower == hint:
                number = _coerce_number(value)
                if number is not None:
                    return number
    for hint in hints:
        for key, value in flat.items():
            lower = key.lower().rsplit(".", 1)[-1]
            if lower.endswith(exclude_suffixes):
                continue
            if hint in lower:
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
        if "reset_after" in lower:
            continue
        if lower in hints:
            parsed = _parse_datetime(value, captured_at)
            if parsed:
                return parsed
    for key, value in flat.items():
        lower = key.lower().rsplit(".", 1)[-1]
        if "reset_after" in lower:
            continue
        if any(hint in lower for hint in hints):
            parsed = _parse_datetime(value, captured_at)
            if parsed:
                return parsed
    return None


def _parse_datetime(value: Any, captured_at: datetime) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    timestamp_value: int | float | None = None
    if isinstance(value, (int, float)):
        timestamp_value = value
    elif isinstance(value, str):
        numeric_text = value.strip()
        is_compact_iso_date = len(numeric_text) == 8 and numeric_text.isdigit()
        if not is_compact_iso_date and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", numeric_text):
            try:
                timestamp_value = float(numeric_text)
            except (OverflowError, ValueError):
                return None
    if timestamp_value is not None:
        try:
            timestamp = float(timestamp_value)
        except (OverflowError, ValueError):
            return None
        if not math.isfinite(timestamp):
            return None
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=captured_at.tzinfo)
        except (OSError, OverflowError, ValueError):
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
    try:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=captured_at.tzinfo)
        return parsed.astimezone(captured_at.tzinfo)
    except (OSError, OverflowError, ValueError):
        return None


def _parse_time_today_or_next(raw: str, captured_at: datetime) -> datetime | None:
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    parsed = captured_at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if parsed < captured_at:
        try:
            parsed += timedelta(days=1)
        except (OverflowError, ValueError):
            return None
    return parsed


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _finite_float(value)
    return _parse_number(str(value))


def _coerce_percent(value: Any) -> float | None:
    number = _coerce_number(value)
    return number if number is not None and 0 <= number <= 100 else None


def _parse_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    cleaned = raw.strip().replace(",", ".")
    try:
        return _finite_float(float(cleaned))
    except ValueError:
        return None


def _parse_percent(raw: str | None) -> float | None:
    number = _parse_number(raw)
    return number if number is not None and 0 <= number <= 100 else None


def _finite_float(value: float) -> float | None:
    coerced = float(value)
    return coerced if math.isfinite(coerced) else None


def _json_preview(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str, allow_nan=False)
    except (TypeError, ValueError):
        return type(obj).__name__


def _flatten_mapping(
    obj: dict[str, Any],
    prefix: str = "",
    *,
    depth: int = 0,
    max_fields: int = MAX_JSON_FLATTEN_FIELDS,
) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    if depth >= MAX_JSON_WALK_DEPTH:
        return flat
    for index, (key, value) in enumerate(obj.items()):
        if index >= MAX_JSON_WALK_ITEMS or len(flat) >= max_fields:
            break
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            for child_key, child_value in _flatten_mapping(
                value,
                path,
                depth=depth + 1,
                max_fields=max_fields - len(flat),
            ).items():
                flat[child_key] = child_value
                if len(flat) >= max_fields:
                    break
        else:
            flat[path] = value
    return flat


def _walk_dicts(
    value: Any,
    path: str = "$",
    *,
    depth: int = 0,
) -> Iterable[tuple[str, dict[str, Any]]]:
    if depth > MAX_JSON_WALK_DEPTH:
        return
    if isinstance(value, dict):
        yield path, value
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_JSON_WALK_ITEMS:
                break
            yield from _walk_dicts(child, f"{path}.{key}", depth=depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if index >= MAX_JSON_WALK_ITEMS:
                break
            yield from _walk_dicts(child, f"{path}[{index}]", depth=depth + 1)


def _normalize_ws(value: str) -> str:
    value = (
        value.replace("\u00a0", " ")
        .replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    return re.sub(r"\s+", " ", value).strip()


def _looks_like_five_hour(value: str) -> bool:
    return (
        any(label in value for label in FIVE_HOUR_LABELS)
        or any(word in value for word in ("five_hour", "five-hour", "5_hour", "5-hour", "5h"))
        or bool(re.search(r"\b5\s*h\b", value))
        or bool(re.search(r"\b5\b.{0,20}(hour|hours|stunden)", value))
    )


def _looks_like_weekly(value: str) -> bool:
    return any(label in value for label in WEEKLY_LABELS) or any(
        word
        in value
        for word in (
            "weekly",
            "week_limit",
            "week-limit",
            "week limit",
            "woche",
            "wochenlimit",
            "woechentlich",
            "wöchentlich",
        )
    )
