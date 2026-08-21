from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from itertools import islice
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .json_utils import loads_strict
from .models import LimitWindow


def _system_local_timezone():
    configured = os.environ.get("TZ", "").strip()
    if configured.startswith(":"):
        configured = configured[1:]
    candidates = [configured] if configured else []
    try:
        localtime = os.path.realpath("/etc/localtime")
    except OSError:
        localtime = ""
    marker = "/zoneinfo/"
    if marker in localtime:
        candidates.append(localtime.split(marker, 1)[1])
    for candidate in candidates:
        if not candidate or candidate.startswith("/") or ".." in candidate.split("/"):
            continue
        try:
            return ZoneInfo(candidate)
        except (KeyError, ValueError, ZoneInfoNotFoundError):
            continue
    return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")


LOCAL_TZ = _system_local_timezone()
MAX_JSON_WALK_DEPTH = 24
MAX_JSON_WALK_ITEMS = 1000
MAX_JSON_FLATTEN_FIELDS = 2000
MAX_JSON_WINDOW_MATCHES = 2000
MAX_JSON_CANDIDATES = 50
MAX_TEXT_LABEL_OFFSETS = 256
MAX_PROGRESS_PARSER_ENTRIES = 1000
PERCENT_COMPLEMENT_TOLERANCE = 0.01
RELATIVE_RESET_HINTS = (
    "reset_after_seconds",
    "resetafterseconds",
    "reset_after",
    "resetafter",
    "reset_seconds",
    "resetseconds",
    "reset_in_seconds",
    "resetinseconds",
    "seconds_until_reset",
    "secondsuntilreset",
    "reset_duration",
    "resetduration",
)

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
    text_sources: Iterable[tuple[str, str]] | None = None,
    now: datetime | None = None,
) -> tuple[LimitWindow | None, LimitWindow | None]:
    captured_at = now if isinstance(now, datetime) else datetime.now(tz=LOCAL_TZ)
    normalized_body = body_text if isinstance(body_text, str) else ""
    try:
        candidates = list(islice(json_candidates, MAX_JSON_CANDIDATES + 1))
    except TypeError:
        candidates = []
    if len(candidates) > MAX_JSON_CANDIDATES:
        candidates = []
    sources: tuple[tuple[str, str], ...]
    if text_sources is None:
        sources = (("dom-text", normalized_body),)
    else:
        normalized_sources: list[tuple[str, str]] = []
        try:
            source_items = iter(text_sources)
        except TypeError:
            source_items = iter(())
        for item in source_items:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                continue
            source, text = item
            if isinstance(source, str) and isinstance(text, str) and text.strip():
                normalized_sources.append((source, text))
        sources = tuple(normalized_sources)

    five_json = _extract_json_window(candidates, "five_hour", captured_at)
    weekly_json = _extract_json_window(candidates, "weekly", captured_at)
    five_text = _extract_text_windows(
        sources,
        name="5h",
        labels=FIVE_HOUR_LABELS,
        stop_labels=WEEKLY_LABELS,
        captured_at=captured_at,
    )
    weekly_text = _extract_text_windows(
        sources,
        name="weekly",
        labels=WEEKLY_LABELS,
        stop_labels=FIVE_HOUR_LABELS,
        captured_at=captured_at,
    )

    five = _merge_window_sources(five_json, five_text)
    weekly = _merge_window_sources(weekly_json, weekly_text)
    return five, weekly


def _extract_text_windows(
    sources: tuple[tuple[str, str], ...],
    *,
    name: str,
    labels: tuple[str, ...],
    stop_labels: tuple[str, ...],
    captured_at: datetime,
) -> LimitWindow | None:
    candidate_pairs = [
        (source_index, _extract_text_window(
            text,
            name=name,
            labels=labels,
            stop_labels=stop_labels,
            captured_at=captured_at,
            source=source,
        ))
        for source_index, (source, text) in enumerate(sources)
    ]
    candidates = [
        (source_index, window)
        for source_index, window in candidate_pairs
        if isinstance(window, LimitWindow)
    ]
    usage_candidates = [item for item in candidates if item[1].has_usage_value]
    if usage_candidates:
        selected = _select_text_usage_candidate(usage_candidates)
        reset_candidates = [
            item[1]
            for item in candidates
            if not item[1].has_usage_value and item[1].reset_at is not None
        ]
        if selected.reset_at is None and reset_candidates:
            reset_source = min(
                reset_candidates,
                key=lambda item: (
                    _text_source_priority(item.source),
                    item.reset_at is None,
                ),
            )
            selected = replace(
                selected,
                reset_at=reset_source.reset_at,
                source=_merge_window_source_names(selected, reset_source),
            )
        return selected
    if candidates:
        return min(
            (item[1] for item in candidates),
            key=lambda item: (
                item.reset_at is None,
                _text_source_priority(item.source),
            ),
        )
    return None


def _select_text_usage_candidate(
    candidates: list[tuple[int, LimitWindow]],
) -> LimitWindow:
    primary = [
        item
        for item in candidates
        if _text_source_priority(item[1].source) == 0
    ]
    if primary:
        selected = min(primary, key=lambda item: item[0])[1]
        html_progress = [
            item
            for item in candidates
            if item[1].source == "htmlText"
            and _extract_progress_width_percent(item[1].raw or "") is not None
        ]
        if html_progress:
            selected_progress = min(html_progress, key=lambda item: item[0])[1]
            if (
                selected.used is None
                and selected.limit is None
                and _is_authoritative_html_progress(selected_progress)
            ):
                return selected_progress
        if _text_window_strength(selected) == 2:
            if html_progress:
                return min(html_progress, key=lambda item: item[0])[1]
        return selected

    html_progress = [
        item
        for item in candidates
        if item[1].source == "htmlText"
        and _extract_progress_width_percent(item[1].raw or "") is not None
    ]
    if html_progress:
        return min(html_progress, key=lambda item: item[0])[1]
    return min(
        candidates,
        key=lambda item: (
            -_text_window_strength(item[1]),
            _text_source_priority(item[1].source),
            item[0],
        ),
    )[1]


def _is_authoritative_html_progress(window: LimitWindow) -> bool:
    raw = window.raw or ""
    return bool(
        re.search(r"transition-\[width\]", raw, flags=re.IGNORECASE)
        or re.search(r"role\s*=\s*['\"]progressbar['\"]", raw, flags=re.IGNORECASE)
        or re.search(
            r"class\s*=\s*['\"][^'\"]*\bprogress(?:bar)?\b",
            raw,
            flags=re.IGNORECASE,
        )
    )


def _text_source_priority(source: str) -> int:
    return {
        "bodyText": 0,
        "body_text": 0,
        "text": 0,
        "innerText": 0,
        "dom-text": 0,
        "htmlText": 1,
        "accessibilityText": 2,
        "svgText": 3,
        "domText": 4,
        "textContent": 4,
    }.get(source, 5)


def _text_window_strength(window: LimitWindow) -> int:
    raw = window.raw or ""
    if window.used is not None and window.limit is not None:
        return 5
    if _extract_remaining(raw) is not None:
        return 5
    if _extract_progress_width_percent(raw) is not None:
        return 4
    if _extract_used_percent(raw) is not None:
        return 2
    return 3


def _merge_window_sources(
    primary: LimitWindow | None,
    secondary: LimitWindow | None,
) -> LimitWindow | None:
    """Prefer structured data while filling a missing reset from the DOM."""
    if primary is None:
        return secondary
    if secondary is None:
        return primary

    if _json_window_has_usage_metadata(primary):
        # A structured response that mentioned usage but yielded no valid
        # value must not be replaced by a weaker DOM fallback.
        return primary

    if primary.has_usage_value:
        if primary.reset_at is None and secondary.reset_at is not None:
            return replace(
                primary,
                reset_at=secondary.reset_at,
                source=_merge_window_source_names(primary, secondary),
            )
        return primary

    if secondary.has_usage_value:
        if primary.reset_at is not None:
            return replace(
                secondary,
                reset_at=primary.reset_at,
                source=_merge_window_source_names(secondary, primary),
            )
        return secondary

    return primary if primary.reset_at is not None else secondary


def _json_window_has_usage_metadata(window: LimitWindow) -> bool:
    if not window.source.casefold().startswith("json:"):
        return False
    return re.search(
        r"\"(?:used|usage|consumed|remaining|left|available|percent|percentage|ratio)"
        r"\"\s*:",
        window.raw or "",
        flags=re.IGNORECASE,
    ) is not None


def _merge_window_source_names(first: LimitWindow, second: LimitWindow) -> str:
    names: list[str] = []
    for source in (first.source, second.source):
        if source and source not in names:
            names.append(source)
    return "+".join(names) or "unknown"


def _candidate_url_is_usable(url: Any) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        urlsplit(url)
    except (TypeError, ValueError):
        return False
    return True


def load_json_candidate(url: str, payload_text: str) -> JsonCandidate | None:
    if not _candidate_url_is_usable(url) or not isinstance(payload_text, str):
        return None
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
    matches: list[tuple[str, str, dict[str, Any], str, int]] = []
    wham_window_counts: dict[int, int] = {}
    wham_main_window_counts: dict[int, int] = {}
    reset_only: list[tuple[int, int, int, bool, LimitWindow]] = []
    usage_windows: list[tuple[int, int, int, bool, LimitWindow]] = []
    generic_window_counts: dict[tuple[int, int], int] = {}
    for candidate_index, candidate in enumerate(candidates):
        if (
            not isinstance(candidate, JsonCandidate)
            or not _candidate_url_is_usable(candidate.url)
        ):
            continue
        candidate_priority = _wham_candidate_priority(candidate.url)
        blocks_additional = _main_wham_target_blocks_additional(candidate.payload, target)
        for path, obj in _walk_dicts(candidate.payload):
            if blocks_additional and "additional_rate_limits" in path.lower():
                continue
            obj_preview = _json_preview(obj)
            is_rate_limit_path = _path_has_component(path, "rate_limit")
            wham_window = (
                _window_from_wham_rate_limit_mapping(
                    obj,
                    target=target,
                    captured_at=captured_at,
                    source=f"json:{candidate.url}",
                    raw=f"{path} {obj_preview}"[:500],
                )
                if is_rate_limit_path
                else None
            )
            if wham_window is not None:
                wham_window_counts[candidate_index] = (
                    wham_window_counts.get(candidate_index, 0) + 1
                )
                if "additional_rate_limits" not in path.casefold():
                    wham_main_window_counts[candidate_index] = (
                        wham_main_window_counts.get(candidate_index, 0) + 1
                    )
                if wham_window.has_usage_value:
                    usage_windows.append(
                        (
                            candidate_priority,
                            _wham_window_path_priority(path, target),
                            -candidate_index,
                            wham_window.reset_at is None,
                            wham_window,
                        )
                    )
                else:
                    reset_only.append(
                        (
                            candidate_priority,
                            _wham_window_path_priority(path, target),
                            -candidate_index,
                            wham_window.reset_at is None,
                            wham_window,
                        )
                    )
                continue
            haystack = f"{path} {obj_preview}".lower()
            has_target_scope = (
                _has_window_scope(path, target)
                or _has_window_scope(obj_preview, target)
                or _has_direct_structural_window(obj, target)
            )
            if "limit_window_seconds" in obj and not is_rate_limit_path:
                # A duration alone is not enough to identify a usage bucket.
                # Keep explicitly scoped generic windows eligible, but never
                # promote unrelated metadata by traversal order.
                if not has_target_scope:
                    continue
            if target == "five_hour" and not _looks_like_five_hour(haystack):
                if not has_target_scope:
                    continue
            if target == "weekly" and not _looks_like_weekly(haystack):
                if not has_target_scope:
                    continue
            has_usage_context = any(
                word in haystack for word in ("limit", "usage", "nutzung", "reset")
            )
            if has_usage_context or has_target_scope:
                if len(matches) >= MAX_JSON_WINDOW_MATCHES:
                    return None
                matches.append((candidate.url, path, obj, haystack, candidate_index))

    if any(
        main_count > 1
        or (
            main_count == 0
            and wham_window_counts.get(candidate_index, 0) > 1
        )
        for candidate_index, main_count in wham_main_window_counts.items()
    ) or any(
        candidate_index not in wham_main_window_counts and count > 1
        for candidate_index, count in wham_window_counts.items()
    ):
        # A single response cannot identify which duplicate rate-limit bucket
        # belongs to this target. Never choose one by traversal order.
        return None

    if usage_windows:
        # A newer usage value is authoritative; an older reset timestamp must
        # not make the parser display stale consumption data.
        usage_windows.sort(key=lambda item: item[:4])
        return usage_windows[0][4]

    ranked_windows: list[tuple[int, int, int, int, int, LimitWindow]] = []
    for url, _path, obj, haystack, candidate_index in matches:
        window = _window_from_mapping(
            obj,
            name="5h" if target == "five_hour" else "weekly",
            captured_at=captured_at,
            source=f"json:{url}",
            raw=haystack[:500],
            target=target,
            path=_path,
        )
        if window is not None:
            target_rank = _target_rank(_path, haystack, target)
            if not _is_structural_window_path(_path):
                count_key = (candidate_index, target_rank)
                generic_window_counts[count_key] = (
                    generic_window_counts.get(count_key, 0) + 1
                )
            if window.has_usage_value:
                ranked_windows.append(
                    (
                        target_rank,
                        0,
                        -candidate_index,
                        0 if window.reset_at is not None else 1,
                        len(_flatten_mapping(obj)),
                        window,
                    )
                )
            else:
                reset_only.append(
                    (
                        _wham_candidate_priority(url),
                        _target_rank(_path, haystack, target),
                        -candidate_index,
                        window.reset_at is None,
                        window,
                    )
                )
    if any(count > 1 for count in generic_window_counts.values()):
        # A generic response with multiple equally scoped windows cannot
        # identify which value belongs to this target. Never use traversal
        # order as an implicit authority.
        return None
    if ranked_windows:
        ranked_windows.sort(key=lambda item: item[:5])
        return ranked_windows[0][5]
    if reset_only:
        reset_only.sort(key=lambda item: item[:4])
        return reset_only[0][4]
    return None


def _wham_candidate_priority(url: str) -> int:
    path = urlsplit(url).path.rstrip("/").lower()
    if path == "/backend-api/wham/usage":
        return 0
    if path.startswith("/backend-api/wham/usage/"):
        return 1
    return 2


def _main_wham_target_blocks_additional(payload: Any, target: str) -> bool:
    if not isinstance(payload, dict):
        return False
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        # Model-specific buckets must never become legacy 5h/weekly values
        # when response has no identifiable main bucket.
        return "additional_rate_limits" in payload
    expected_key = "primary_window" if target == "five_hour" else "secondary_window"
    window = rate_limit.get(expected_key)
    if not isinstance(window, dict):
        return True
    duration = _coerce_number(window.get("limit_window_seconds"))
    expected_duration = 18_000 if target == "five_hour" else 604_800
    if duration != expected_duration:
        return True
    # A present main bucket with only reset metadata is not permission to use
    # a model-specific additional bucket as its usage value.
    return not any(
        (
            _coerce_percent(window.get(key)) is not None
            or _normalize_ratio_value(window.get(key)) is not None
        )
        for key in (
            "used_percent",
            "used_percentage",
            "usage_percent",
            "usage_percentage",
            "consumed_percent",
            "consumed_percentage",
            "remaining_percent",
            "remaining_percentage",
            "available_percent",
            "available_percentage",
            "left_percent",
            "left_percentage",
            "used_ratio",
            "usage_ratio",
            "consumed_ratio",
            "remaining_ratio",
            "available_ratio",
            "left_ratio",
        )
    )


def _wham_window_path_priority(path: str, target: str) -> int:
    lower = path.lower()
    expected = "primary_window" if target == "five_hour" else "secondary_window"
    is_additional = "additional_rate_limits" in lower
    if not is_additional and lower.endswith(f".rate_limit.{expected}"):
        return 0
    if not is_additional and expected in lower:
        return 1
    if is_additional:
        return 2
    return 3


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

    flat = _flatten_mapping(obj)
    absolute_used = _pick_number(
        flat,
        ("used", "usage", "current", "consumed", "num_used"),
        exclude_suffixes=(
            "_percent",
            "_percentage",
            "_ratio",
            "_seconds",
            "_minutes",
            "_hours",
        ),
    )
    absolute_remaining = _pick_number(
        flat,
        ("remaining", "left", "available"),
        exclude_suffixes=("_percent", "_percentage", "_ratio"),
    )
    invalid_absolute_counter = _has_invalid_number(
        flat,
        ("used", "usage", "current", "consumed", "num_used"),
        exclude_suffixes=(
            "_percent",
            "_percentage",
            "_ratio",
            "_seconds",
            "_minutes",
            "_hours",
        ),
        converter=_coerce_number,
    ) or _has_invalid_number(
        flat,
        ("remaining", "left", "available"),
        exclude_suffixes=("_percent", "_percentage", "_ratio"),
        converter=_coerce_number,
    )
    invalid_absolute_counter = invalid_absolute_counter or (
        (absolute_used is not None and absolute_used < 0)
        or (absolute_remaining is not None and absolute_remaining < 0)
    )
    used_percent_hints = (
        "used_percent",
        "used_percentage",
        "usage_percent",
        "usage_percentage",
        "consumed_percent",
        "consumed_percentage",
    )
    used_ratio_hints = ("used_ratio", "usage_ratio", "consumed_ratio")
    used_percent_aliases = _matching_number_values(
        flat, used_percent_hints, _coerce_percent
    )
    used_ratio_aliases = _matching_number_values(
        flat, used_ratio_hints, _normalize_ratio_value
    )
    used_percent = _coerce_percent(_pick_number(flat, used_percent_hints))
    used_ratio = _normalize_ratio_value(_pick_number(flat, used_ratio_hints))
    if not _values_are_consistent((*used_percent_aliases, *used_ratio_aliases)):
        used_percent = None
        used_ratio = None
    elif used_percent is None:
        used_percent = used_ratio

    remaining_percent_hints = (
        "remaining_percent",
        "remaining_percentage",
        "available_percent",
        "available_percentage",
        "left_percent",
        "left_percentage",
    )
    remaining_ratio_hints = ("remaining_ratio", "available_ratio", "left_ratio")
    remaining_percent_aliases = _matching_number_values(
        flat, remaining_percent_hints, _coerce_percent
    )
    remaining_ratio_aliases = _matching_number_values(
        flat, remaining_ratio_hints, _normalize_ratio_value
    )
    remaining_percent = _coerce_percent(
        _pick_number(flat, remaining_percent_hints)
    )
    remaining_ratio = _normalize_ratio_value(
        _pick_number(flat, remaining_ratio_hints)
    )
    if not _values_are_consistent(
        (*remaining_percent_aliases, *remaining_ratio_aliases)
    ):
        remaining_percent = None
        remaining_ratio = None
    elif remaining_percent is None:
        remaining_percent = remaining_ratio
    invalid_percentage_field = _has_invalid_number(
        flat, used_percent_hints, converter=_coerce_percent
    ) or _has_invalid_number(
        flat, used_ratio_hints, converter=_normalize_ratio_value
    ) or _has_invalid_number(
        flat, remaining_percent_hints, converter=_coerce_percent
    ) or _has_invalid_number(
        flat, remaining_ratio_hints, converter=_normalize_ratio_value
    )
    reset_at = _parse_datetime(obj.get("reset_at"), captured_at)
    if reset_at is None:
        reset_at = _pick_datetime(
            flat, ("reset", "reset_at", "resets_at", "next_reset"), captured_at
        )
    if reset_at is None:
        reset_after = _pick_number(flat, RELATIVE_RESET_HINTS)
        reset_at = _relative_reset_at(reset_after, captured_at)
    if invalid_absolute_counter or invalid_percentage_field:
        # A contradictory absolute counter invalidates percentage usage from
        # this WHAM object; retain only independent reset metadata.
        if reset_at is None:
            return None
        return LimitWindow(
            name="5h" if target == "five_hour" else "weekly",
            reset_at=reset_at,
            raw=raw,
            source=source,
        )
    if used_percent is None and remaining_percent is None and reset_at is None:
        return None
    if not _percentages_are_complementary(used_percent, remaining_percent):
        # A contradictory pair cannot be attributed to either the consumed or
        # remaining side without inventing a value. Keep only reset metadata.
        used_percent = None
        remaining_percent = None

    remaining = (
        max(100 - used_percent, 0)
        if used_percent is not None
        else remaining_percent
    )
    return LimitWindow(
        name="5h" if target == "five_hour" else "weekly",
        used=used_percent,
        limit=100 if used_percent is not None else None,
        remaining=remaining,
        percent=remaining,
        reset_at=reset_at,
        raw=raw,
        source=source,
    )


def _target_rank(path: str, haystack: str, target: str) -> int:
    path_lower = path.lower()
    markers = (
        ("five_hour", "5_hour", "five-hour", "5-hour")
        if target == "five_hour"
        else ("weekly", "week", "woche")
    )
    if any(marker in path_lower for marker in markers):
        return 0
    return 2 if path == "$" else 1


def _window_from_mapping(
    obj: dict[str, Any],
    *,
    name: str,
    captured_at: datetime,
    source: str,
    raw: str,
    target: str | None = None,
    path: str | None = None,
) -> LimitWindow | None:
    if target in {"five_hour", "weekly"} and path:
        lower_path = path.casefold()
        structural_key = (
            "primary_window" if target == "five_hour" else "secondary_window"
        )
        opposite_key = (
            "secondary_window" if target == "five_hour" else "primary_window"
        )
        if _path_has_component(lower_path, opposite_key):
            return None
        if _path_has_component(lower_path, structural_key):
            if not lower_path.endswith(f".{structural_key}"):
                return None
            duration = obj.get("limit_window_seconds")
            if duration is not None:
                expected_duration = 18_000 if target == "five_hour" else 604_800
                if _coerce_number(duration) != expected_duration:
                    return None
            return _window_from_mapping(
                obj,
                name=name,
                captured_at=captured_at,
                source=source,
                raw=raw,
                target=target,
                path=None,
            )
    if target in {"five_hour", "weekly"}:
        # Some backend responses expose one or both buckets without
        # durations. The generic field picker must not let the first bucket
        # win for both target slots; use only the structural bucket belonging
        # to the requested slot. WHAM responses with explicit durations are
        # handled by _window_from_wham_rate_limit_mapping above.
        structural_keys = ("primary_window", "secondary_window")
        if any(isinstance(obj.get(key), dict) for key in structural_keys):
            target_key = (
                "primary_window" if target == "five_hour" else "secondary_window"
            )
            if not isinstance(obj.get(target_key), dict):
                return None
            target_window = obj[target_key]
            duration = target_window.get("limit_window_seconds")
            if duration is not None:
                expected_duration = 18_000 if target == "five_hour" else 604_800
                if _coerce_number(duration) != expected_duration:
                    return None
            obj = target_window
    flat = _flatten_mapping(obj)
    if target in {"five_hour", "weekly"}:
        opposite = "weekly" if target == "five_hour" else "five_hour"
        opposite_structural_key = (
            "secondary_window" if target == "five_hour" else "primary_window"
        )
        flat = {
            key: value
            for key, value in flat.items()
            if not _has_window_scope(key, opposite)
            and not _path_has_component(key.casefold(), opposite_structural_key)
        }
        target_structural_key = (
            "primary_window" if target == "five_hour" else "secondary_window"
        )
        target_scoped_keys = {
            key
            for key in flat
            if _has_window_scope(key, target)
            or _path_has_component(key.casefold(), target_structural_key)
        }
        if target_scoped_keys:
            flat = {
                key: value
                for key, value in flat.items()
                if key in target_scoped_keys
            }
    used_percent_hints = (
        "used_percent",
        "used_percentage",
        "usage_percent",
        "usage_percentage",
        "consumed_percent",
        "consumed_percentage",
    )
    used_ratio_hints = ("used_ratio", "usage_ratio", "consumed_ratio")
    used_percent_aliases = _matching_number_values(
        flat, used_percent_hints, _coerce_percent
    )
    used_ratio_aliases = _matching_number_values(
        flat, used_ratio_hints, _normalize_ratio_value
    )
    used_percent = _coerce_percent(_pick_number(flat, used_percent_hints))
    used_ratio = _normalize_ratio_value(_pick_number(flat, used_ratio_hints))
    used_percent_conflict = not _values_are_consistent(
        (*used_percent_aliases, *used_ratio_aliases)
    )
    if used_percent_conflict:
        used_percent = None
        used_ratio = None
    elif used_percent is None:
        used_percent = used_ratio
    used = _pick_number(
        flat,
        ("used", "usage", "current", "consumed", "num_used"),
        exclude_suffixes=(
            "_percent",
            "_percentage",
            "_ratio",
            "_seconds",
            "_minutes",
            "_hours",
        ),
    )
    limit = _pick_number(
        flat,
        ("limit", "max", "quota", "total", "capacity"),
        exclude_suffixes=("_seconds", "_minutes", "_hours"),
    )
    invalid_absolute_limit_field = _has_invalid_number(
        flat,
        ("limit", "max", "quota", "total", "capacity"),
        exclude_suffixes=("_seconds", "_minutes", "_hours"),
        converter=_coerce_number,
    )
    remaining_percent_hints = (
        "remaining_percent",
        "remaining_percentage",
        "available_percent",
        "available_percentage",
        "left_percent",
        "left_percentage",
    )
    remaining_ratio_hints = ("remaining_ratio", "available_ratio", "left_ratio")
    remaining_percent_aliases = _matching_number_values(
        flat, remaining_percent_hints, _coerce_percent
    )
    remaining_ratio_aliases = _matching_number_values(
        flat, remaining_ratio_hints, _normalize_ratio_value
    )
    remaining_percent = _coerce_percent(
        _pick_number(flat, remaining_percent_hints)
    )
    remaining_ratio = _normalize_ratio_value(
        _pick_number(flat, remaining_ratio_hints)
    )
    remaining_percent_conflict = not _values_are_consistent(
        (*remaining_percent_aliases, *remaining_ratio_aliases)
    )
    if remaining_percent_conflict:
        remaining_percent = None
        remaining_ratio = None
    elif remaining_percent is None:
        remaining_percent = remaining_ratio
    remaining = _pick_number(
        flat,
        ("remaining", "left", "available"),
        exclude_suffixes=("_percent", "_percentage", "_ratio"),
    )
    percent_hints = ("percent", "percentage")
    ratio_hints = ("ratio",)
    percent_aliases = _matching_number_values(flat, percent_hints, _coerce_percent)
    ratio_aliases = _matching_number_values(
        flat, ratio_hints, _normalize_ratio_value
    )
    percent = _coerce_percent(
        _pick_number(
            flat,
            percent_hints,
            exclude_suffixes=("_percent", "_percentage", "_ratio"),
        )
    )
    ratio = _normalize_ratio_value(
        _pick_number(
            flat,
            ratio_hints,
            exclude_suffixes=("_percent", "_percentage", "_ratio"),
        )
    )
    if not _values_are_consistent((*percent_aliases, *ratio_aliases)):
        percent = None
        ratio = None
    elif percent is None:
        percent = ratio
    invalid_percentage_field = _has_invalid_number(
        flat, used_percent_hints, converter=_coerce_percent
    ) or _has_invalid_number(
        flat, used_ratio_hints, converter=_normalize_ratio_value
    ) or _has_invalid_number(
        flat, remaining_percent_hints, converter=_coerce_percent
    ) or _has_invalid_number(
        flat, remaining_ratio_hints, converter=_normalize_ratio_value
    ) or _has_invalid_number(
        flat,
        percent_hints,
        exclude_suffixes=("_percent", "_percentage", "_ratio"),
        converter=_coerce_percent,
    ) or _has_invalid_number(
        flat,
        ratio_hints,
        exclude_suffixes=("_percent", "_percentage", "_ratio"),
        converter=_normalize_ratio_value,
    )
    reset_at = _pick_datetime(
        flat, ("reset", "reset_at", "resets_at", "next_reset"), captured_at
    )
    if reset_at is None:
        reset_after = _pick_number(flat, RELATIVE_RESET_HINTS)
        reset_at = _relative_reset_at(reset_after, captured_at)

    has_explicit_remaining_percent = (
        remaining_percent is not None or remaining_ratio is not None
    )
    explicit_remaining_percent = (
        remaining_percent if remaining_percent is not None else remaining_ratio
    )
    remaining_percentage_values = tuple(
        value
        for value in (explicit_remaining_percent, percent)
        if value is not None
    )
    if not (
        all(
            _percentages_are_complementary(used_percent, value)
            for value in remaining_percentage_values
        )
        and all(
            abs(first - second) <= PERCENT_COMPLEMENT_TOLERANCE
            for first in remaining_percentage_values
            for second in remaining_percentage_values
        )
    ):
        # Keep generic JSON consistent with WHAM: conflicting percentage fields
        # must not produce different values depending on the selected source.
        used_percent = None
        remaining_percent = None
        remaining_ratio = None
        percent = None
        has_explicit_remaining_percent = False

    ambiguous_remaining_conflict = (
        limit is None
        and remaining is not None
        and 0 <= remaining <= 100
        and (
            (
                percent is not None
                and abs(remaining - percent) > PERCENT_COMPLEMENT_TOLERANCE
            )
            or (
                used_percent is not None
                and not _percentages_are_complementary(used_percent, remaining)
            )
            or (
                explicit_remaining_percent is not None
                and abs(remaining - explicit_remaining_percent)
                > PERCENT_COMPLEMENT_TOLERANCE
            )
        )
    )
    if ambiguous_remaining_conflict:
        # A denominator-less counter in percentage range cannot be reconciled
        # with a different percentage source. Keep reset metadata only.
        used_percent = None
        remaining = None
        remaining_percent = None
        remaining_ratio = None
        percent = None
        ratio = None
        has_explicit_remaining_percent = False
        explicit_remaining_percent = None

    invalid_absolute_limit = used is not None and limit is not None and limit <= 0
    invalid_absolute_usage_field = _has_invalid_number(
        flat,
        ("used", "usage", "current", "consumed", "num_used"),
        exclude_suffixes=(
            "_percent",
            "_percentage",
            "_ratio",
            "_seconds",
            "_minutes",
            "_hours",
        ),
        converter=_coerce_number,
    )
    invalid_absolute_usage = (
        invalid_absolute_usage_field
        or (used is not None and used < 0)
    )
    invalid_absolute_remaining_field = _has_invalid_number(
        flat,
        ("remaining", "left", "available"),
        exclude_suffixes=("_percent", "_percentage", "_ratio"),
        converter=_coerce_number,
    )
    invalid_absolute_remaining = remaining is not None and (
        remaining < 0
        or (limit is not None and limit > 0 and remaining > limit)
        or (
            limit is None
            and remaining > 100
            and used_percent is None
            and remaining_percent is None
            and remaining_ratio is None
        )
    )
    invalid_absolute_remaining = (
        invalid_absolute_remaining_field or invalid_absolute_remaining
    )
    if invalid_absolute_limit_field:
        # A malformed denominator makes every usage value in this object
        # unverifiable; keep only independent reset metadata.
        used = None
        limit = None
        remaining = None
        used_percent = None
        remaining_percent = None
        remaining_ratio = None
        percent = None
        ratio = None
        has_explicit_remaining_percent = False
        explicit_remaining_percent = None
    if invalid_absolute_usage:
        # A contradictory absolute counter invalidates every usage value from
        # this object. Keep only independent limit/reset metadata.
        used = None
        remaining = None
        used_percent = None
        remaining_percent = None
        remaining_ratio = None
        percent = None
        ratio = None
        has_explicit_remaining_percent = False
        explicit_remaining_percent = None
    elif invalid_absolute_remaining:
        # A negative or out-of-range absolute remaining counter must not be
        # masked by a separate optimistic percentage from the same object.
        used = None
        used_percent = None
        remaining_percent = None
        remaining_ratio = None
        percent = None
        ratio = None
        has_explicit_remaining_percent = False
        explicit_remaining_percent = None
        if remaining is not None and remaining < 0 and limit is not None and limit > 0:
            remaining = 0
            percent = 0
        else:
            remaining = None
    has_valid_absolute_usage = (
        used is not None
        and limit is not None
        and limit > 0
    ) or (
        remaining is not None
        and limit is not None
        and limit > 0
        and 0 <= remaining <= limit
    )
    if invalid_percentage_field and not has_valid_absolute_usage:
        used_percent = None
        remaining_percent = None
        remaining_ratio = None
        percent = None
        ratio = None
        has_explicit_remaining_percent = False
        explicit_remaining_percent = None
    if invalid_absolute_limit:
        # A non-positive absolute denominator cannot produce a valid usage
        # value. An unqualified `remaining` field is ambiguous here and must
        # not be mistaken for a percentage; explicit percentage fields below
        # remain available as their own source.
        used = None
        limit = None
        remaining = None
    elif limit is not None and limit <= 0:
        # A standalone non-positive limit is not a usable denominator either.
        # Drop it and any unqualified counter; explicit percentage fields are
        # still handled below.
        limit = None
        remaining = None
    if remaining is not None and remaining < 0:
        remaining = (
            None
            if has_explicit_remaining_percent or percent is not None
            else 0
        )
    if (
        used_percent is not None
        and used is None
        and limit is None
        and not has_explicit_remaining_percent
    ):
        # An unqualified `remaining` field may be an absolute count. Without a
        # denominator, the explicit usage percentage is the only safe source.
        remaining = None
    if limit is None:
        if has_explicit_remaining_percent:
            remaining = explicit_remaining_percent
        elif percent is not None:
            # The standalone percentage is authoritative over an ambiguous
            # unqualified `remaining` counter when no denominator exists.
            remaining = None
    if used is not None and limit is not None:
        remaining = max(limit - used, 0)
        if limit > 0:
            # `percent` is the remaining percentage across WHAM, app-server,
            # rendering, and applet consumers. Absolute usage is authoritative
            # over conflicting percentage fields, so derive it from used/limit.
            percent = max(0, min(100, 100 - (used / limit * 100)))
        else:
            percent = None
    else:
        if remaining is None:
            if has_explicit_remaining_percent:
                remaining_value = (
                    remaining_percent
                    if remaining_percent is not None
                    else remaining_ratio
                )
                remaining = (
                    remaining_value * limit / 100
                    if remaining_value is not None and limit is not None and limit > 0
                    else remaining_value
                )
            elif used_percent is not None:
                available_percent = max(100 - used_percent, 0)
                remaining = (
                    available_percent * limit / 100
                    if limit is not None and limit > 0
                    else available_percent
                )
        if remaining is not None and limit is not None and limit > 0:
            percent = remaining / limit * 100
        elif has_explicit_remaining_percent and remaining is not None:
            percent = remaining
        elif used_percent is not None and used is None and remaining is not None:
            percent = remaining
    if percent is not None and not 0 <= percent <= 100:
        percent = None
    if (
        limit is None
        and remaining is not None
        and percent is None
        and not has_explicit_remaining_percent
        and not 0 <= remaining <= 100
    ):
        # Never let an absolute, denominator-less count become a clamped
        # 100% remaining value in the renderer.
        remaining = None

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
    source: str = "dom-text",
) -> LimitWindow | None:
    text = _normalize_ws(body_text)
    lower = text.lower()
    reset_only: list[tuple[int, LimitWindow]] = []
    usage_windows: list[tuple[int, LimitWindow]] = []
    label_offsets = _label_offsets(lower, labels)
    if source == "htmlText":
        hidden_ranges = _extract_hidden_html_ranges(text)
        if hidden_ranges is None:
            return None
        label_offsets = [
            start
            for start in label_offsets
            if not any(begin <= start < end for begin, end in hidden_ranges)
        ]
    for start in label_offsets:
        end = _next_label_offset(lower, start + 1, stop_labels)
        chunk_end = min(start + 1500, end) if end is not None else start + 1500
        chunk = text[start:chunk_end]
        used, limit = _extract_used_limit(chunk)
        progress_percent = _extract_progress_width_percent(chunk)
        percent = _extract_percent(chunk)
        remaining = _extract_remaining(chunk)
        used_percent = _extract_used_percent(chunk)
        reset_at = _extract_reset_at(chunk, captured_at)
        invalid_absolute_usage = used is not None and used < 0
        invalid_absolute_remaining = remaining is not None and (
            remaining < 0
            or (limit is not None and limit > 0 and remaining > limit)
            or (limit is None and remaining > 100 and used_percent is None)
        )

        if used is not None and limit is not None and limit <= 0:
            used = None
            limit = None
            # Without a valid absolute denominator, an unqualified text
            # value such as `50 remaining` is not safe to interpret as 50%.
            # Explicit `%` and progress-bar values are processed separately.
            remaining = None

        if invalid_absolute_usage or invalid_absolute_remaining:
            # Do not let a contradictory absolute value fall back to a
            # positive text percentage or rendered progress bar.
            used = None
            remaining = None
            percent = None
            used_percent = None
            progress_percent = None

        if used is not None and limit is not None:
            remaining = max(limit - used, 0)
            percent = (
                max(0, min(100, 100 - (used / limit * 100)))
                if limit > 0
                else None
            )
        elif source == "htmlText" and progress_percent is not None:
            # The rendered bar is the visual source of truth for HTML. A
            # hidden text clone later in the same HTML fragment must not
            # replace it; absolute used/limit values remain authoritative.
            remaining = progress_percent
            percent = progress_percent
        elif remaining is None and progress_percent is not None:
            # A rendered progress bar is more specific than generic text such
            # as a hidden or stale `100% used` label in the same DOM chunk.
            remaining = progress_percent
            percent = progress_percent
        elif remaining is None and used_percent is not None:
            remaining = max(100 - used_percent, 0)

        if (
            used is None
            and limit is None
            and used_percent is not None
            and progress_percent is None
        ):
            # A text `remaining` value can be an absolute count. Keep a
            # complementary percentage when present; otherwise derive it from
            # the explicit used percentage.
            if remaining is None or not _percentages_are_complementary(
                used_percent,
                remaining,
            ):
                remaining = max(100 - used_percent, 0)
            percent = remaining

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
        if (
            limit is None
            and remaining is not None
            and percent is None
            and not 0 <= remaining <= 100
        ):
            # Without a denominator this absolute count cannot be rendered as
            # a percentage; preserve only the reset metadata.
            remaining = None

        window = LimitWindow(
            name=name,
            used=used,
            limit=limit,
            remaining=remaining,
            percent=percent,
            reset_at=reset_at,
            raw=chunk[:500],
            source=source,
        )
        if window.has_usage_value:
            usage_windows.append((start, window))
            continue
        reset_only.append((start, window))
    if usage_windows:
        if source == "htmlText":
            progress_windows = [
                item
                for item in usage_windows
                if _extract_progress_width_percent(item[1].raw or "") is not None
            ]
            if progress_windows:
                progress_windows.sort(key=lambda item: (-item[0], item[1].reset_at is None))
                return progress_windows[0][1]
        # A later DOM occurrence carries the freshest rendered usage value;
        # an older reset timestamp must not make stale consumption win.
        usage_windows.sort(
            key=lambda item: (-item[0], item[1].reset_at is None)
        )
        return usage_windows[0][1]
    if reset_only:
        reset_only.sort(key=lambda item: (-item[0], item[1].reset_at is None))
        return reset_only[0][1]
    return None


def _label_offsets(text: str, labels: tuple[str, ...]) -> list[int]:
    offsets: set[int] = set()
    for label in labels:
        for match in re.finditer(_label_pattern(label), text):
            offsets.add(match.start())
            if len(offsets) > MAX_TEXT_LABEL_OFFSETS:
                return []
    return sorted(offsets)


def _next_label_offset(text: str, start: int, labels: tuple[str, ...]) -> int | None:
    next_offset: int | None = None
    for label in labels:
        match = re.compile(_label_pattern(label)).search(text, start)
        if match is not None and (
            next_offset is None or match.start() < next_offset
        ):
            next_offset = match.start()
    return next_offset


def _label_pattern(label: str) -> str:
    # Underscores remain valid inside JSON field names; letters and digits do
    # not, so a 5h marker cannot match the tail of 15h or 25h.
    return rf"(?<![^\W_]){re.escape(label)}(?![^\W_])"


def _extract_used_limit(text: str) -> tuple[float | None, float | None]:
    patterns = (
        r"(?P<used>[+-]?\d+(?:[.,]\d+)?)\s*/\s*(?P<limit>[+-]?\d+(?:[.,]\d+)?)",
        r"(?P<used>[+-]?\d+(?:[.,]\d+)?)\s+(?:von|of)\s+(?P<limit>[+-]?\d+(?:[.,]\d+)?)",
        r"(?:used|genutzt|verbraucht)\D{0,40}(?P<used>[+-]?\d+(?:[.,]\d+)?)\D{0,20}"
        r"(?:limit|max|grenze)\D{0,40}(?P<limit>[+-]?\d+(?:[.,]\d+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _parse_number(match.group("used")), _parse_number(match.group("limit"))
    return None, None


def _extract_percent(text: str) -> float | None:
    match = re.search(r"(?P<percent>[+-]?\d+(?:[.,]\d+)?)\s*%", text)
    return _parse_percent(match.group("percent")) if match else None


def _extract_used_percent(text: str) -> float | None:
    patterns = (
        r"(?P<used>[+-]?\d+(?:[.,]\d+)?)\s*%\s*(?:used|genutzt|verbraucht)",
        r"(?:used|genutzt|verbraucht)\D{0,10}(?P<used>[+-]?\d+(?:[.,]\d+)?)\s*%",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _parse_percent(match.group("used"))
    return None


def _extract_progress_width_percent(text: str) -> float | None:
    parser = _ProgressWidthParser(text)
    try:
        parser.feed(text)
        parser.close()
    except (AssertionError, RuntimeError, ValueError):
        pass
    if parser.overflowed:
        return None
    if parser.visible_candidates:
        best_rank = min(item[0] for item in parser.visible_candidates)
        best = [
            item for item in parser.visible_candidates if item[0] == best_rank
        ]
        values = {item[2] for item in best}
        if len(values) > 1:
            return None
        return best[0][2]
    if parser.candidates:
        return None

    # Keep single-value compatibility for captures that contain a style
    # fragment but no parseable start tag. Multiple values lack provenance.
    matches = list(
        islice(
            re.finditer(
                r"\bwidth\s*:\s*(?P<percent>\d+(?:[.,]\d+)?)\s*%",
                text,
                flags=re.IGNORECASE,
            ),
            MAX_PROGRESS_PARSER_ENTRIES + 1,
        )
    )
    if len(matches) > MAX_PROGRESS_PARSER_ENTRIES:
        return None
    if matches:
        fallback_values: list[float] = []
        for match in matches:
            percent = _parse_percent(match.group("percent"))
            if percent is None:
                return None
            fallback_values.append(percent)
        return (
            fallback_values[0]
            if len(set(fallback_values)) == 1
            else None
        )
    return None


def _extract_hidden_html_ranges(
    text: str,
) -> tuple[tuple[int, int], ...] | None:
    parser = _ProgressWidthParser(text)
    try:
        parser.feed(text)
        parser.close()
        parser.finish(len(text))
    except (AssertionError, RuntimeError, ValueError):
        pass
    if parser.overflowed:
        return None
    return tuple(parser.hidden_ranges)


class _ProgressWidthParser(HTMLParser):
    def __init__(self, source_text: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[tuple[int, int, float]] = []
        self.visible_candidates: list[tuple[int, int, float]] = []
        self.hidden_ranges: list[tuple[int, int]] = []
        self._hidden_stack: list[tuple[str, bool, int]] = []
        self._open_tag_indices: dict[str, list[int]] = {}
        self._hidden_ancestor_count = 0
        self.overflowed = False
        self._source_text = source_text
        self._line_number = 1
        self._line_start = 0
        self._line_scan_offset = 0

    def _absolute_position(self) -> int:
        line, column = self.getpos()
        while self._line_number < line:
            newline = self._source_text.find("\n", self._line_scan_offset)
            if newline < 0:
                break
            self._line_number += 1
            self._line_start = newline + 1
            self._line_scan_offset = self._line_start
        if line == self._line_number:
            return self._line_start + column
        return column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.overflowed:
            return
        values = {name.casefold(): value or "" for name, value in attrs}
        own_hidden = _is_hidden_progress_element(values, values.get("class", "").casefold())
        inherited_hidden = self._hidden_ancestor_count > 0
        self._record_width(attrs, hidden=inherited_hidden or own_hidden)
        if tag.casefold() not in {
            "area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr",
        }:
            if len(self._hidden_stack) >= MAX_PROGRESS_PARSER_ENTRIES:
                self.overflowed = True
                return
            normalized = tag.casefold()
            stack_index = len(self._hidden_stack)
            self._hidden_stack.append((normalized, own_hidden, self._absolute_position()))
            self._open_tag_indices.setdefault(normalized, []).append(stack_index)
            if own_hidden:
                self._hidden_ancestor_count += 1

    def handle_endtag(self, tag: str) -> None:
        if self.overflowed:
            return
        normalized = tag.casefold()
        indices = self._open_tag_indices.get(normalized)
        if not indices:
            return
        index = indices[-1]
        end = self._absolute_position()
        for stack_index in range(len(self._hidden_stack) - 1, index - 1, -1):
            open_tag = self._hidden_stack[stack_index][0]
            open_indices = self._open_tag_indices[open_tag]
            open_indices.pop()
            if not open_indices:
                del self._open_tag_indices[open_tag]
        for _tag, own_hidden, start in self._hidden_stack[index:]:
            if own_hidden:
                if len(self.hidden_ranges) >= MAX_PROGRESS_PARSER_ENTRIES:
                    self.overflowed = True
                    continue
                self.hidden_ranges.append((start, end))
                self._hidden_ancestor_count -= 1
        del self._hidden_stack[index:]

    def finish(self, end: int) -> None:
        if self.overflowed:
            self._hidden_stack.clear()
            self._open_tag_indices.clear()
            self._hidden_ancestor_count = 0
            return
        for _tag, own_hidden, start in self._hidden_stack:
            if own_hidden:
                if len(self.hidden_ranges) >= MAX_PROGRESS_PARSER_ENTRIES:
                    self.overflowed = True
                    break
                self.hidden_ranges.append((start, end))
        self._hidden_stack.clear()
        self._open_tag_indices.clear()
        self._hidden_ancestor_count = 0

    def handle_startendtag(
        self,
        _tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if self.overflowed:
            return
        values = {name.casefold(): value or "" for name, value in attrs}
        hidden = self._hidden_ancestor_count > 0
        hidden = hidden or _is_hidden_progress_element(
            values,
            values.get("class", "").casefold(),
        )
        self._record_width(attrs, hidden=hidden)

    def _record_width(
        self,
        attrs: list[tuple[str, str | None]],
        *,
        hidden: bool = False,
    ) -> None:
        if len(self.candidates) >= MAX_PROGRESS_PARSER_ENTRIES:
            self.overflowed = True
            return
        values = {name.casefold(): value or "" for name, value in attrs}
        match = re.search(
            r"(?:^|;)\s*width\s*:\s*(?P<percent>\d+(?:[.,]\d+)?)\s*%",
            values.get("style", ""),
            flags=re.IGNORECASE,
        )
        if match is None:
            return
        percent = _parse_percent(match.group("percent"))
        if percent is None:
            return
        classes = values.get("class", "").casefold()
        rank = 0
        is_hidden = hidden or _is_hidden_progress_element(values, classes)
        if is_hidden:
            # React can retain a hidden previous render in the serialized DOM.
            # A hidden bar must never replace the visible account value.
            rank += 100
        if "transition-[width]" in classes:
            rank -= 4
        if "rounded-full" in classes:
            rank -= 2
        if "bg-[#22c55e]" in classes or "progress" in classes:
            rank -= 1
        if values.get("role", "").casefold() == "progressbar":
            rank -= 2
        candidate = (rank, len(self.candidates), percent)
        self.candidates.append(candidate)
        if not is_hidden:
            self.visible_candidates.append(candidate)


def _is_hidden_progress_element(
    attrs: dict[str, str],
    classes: str,
) -> bool:
    if "hidden" in attrs:
        return True
    if attrs.get("aria-hidden", "").casefold() == "true":
        return True
    if re.search(
        r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)",
        attrs.get("style", ""),
        flags=re.IGNORECASE,
    ):
        return True
    return bool(
        re.search(
            r"(?:^|\s)(?:hidden|invisible|sr-only)(?:$|\s)",
            classes,
        )
    )


def _extract_remaining(text: str) -> float | None:
    patterns = (
        r"(?P<remaining>[+-]?\d+(?:[.,]\d+)?)\s*%?\s*"
        r"(?:remaining|left|verbleibend|uebrig|übrig)",
        r"(?:remaining|left|verbleibend|uebrig|übrig)\s+"
        r"(?P<remaining>[+-]?\d+(?:[.,]\d+)?)",
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


def _matching_number_values(
    flat: dict[str, Any],
    hints: tuple[str, ...],
    converter: Callable[[Any], float | None],
) -> tuple[float, ...]:
    values: list[float] = []
    for key, value in flat.items():
        lower = key.lower().rsplit(".", 1)[-1]
        if lower not in hints:
            continue
        number = converter(value)
        if number is not None:
            values.append(number)
    return tuple(values)


def _has_invalid_number(
    flat: dict[str, Any],
    hints: tuple[str, ...],
    converter: Callable[[Any], float | None],
    *,
    exclude_suffixes: tuple[str, ...] = (),
) -> bool:
    for key, value in flat.items():
        if value is None:
            continue
        lower = key.lower().rsplit(".", 1)[-1]
        if lower.endswith(exclude_suffixes):
            continue
        if any(lower == hint or hint in lower for hint in hints):
            if converter(value) is None:
                return True
    return False


def _pick_datetime(
    flat: dict[str, Any],
    hints: tuple[str, ...],
    captured_at: datetime,
) -> datetime | None:
    for key, value in flat.items():
        lower = key.lower().rsplit(".", 1)[-1]
        if _is_relative_reset_key(lower):
            continue
        if lower in hints:
            parsed = _parse_datetime(value, captured_at)
            if parsed:
                return parsed
    for key, value in flat.items():
        lower = key.lower().rsplit(".", 1)[-1]
        if _is_relative_reset_key(lower):
            continue
        if any(hint in lower for hint in hints):
            parsed = _parse_datetime(value, captured_at)
            if parsed:
                return parsed
    return None


def _is_relative_reset_key(key: str) -> bool:
    return any(hint in key for hint in RELATIVE_RESET_HINTS)


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
            return datetime.fromtimestamp(timestamp, tz=_display_timezone(captured_at))
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y, %H:%M"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=_display_timezone(captured_at))
    try:
        iso = raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    try:
        display_timezone = _display_timezone(captured_at)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=display_timezone)
        return parsed.astimezone(display_timezone)
    except (OSError, OverflowError, ValueError):
        return None


def _relative_reset_at(seconds: float | None, captured_at: datetime) -> datetime | None:
    if seconds is None or seconds < 0 or not math.isfinite(seconds):
        return None
    try:
        if captured_at.tzinfo is None:
            return captured_at + timedelta(seconds=seconds)
        target = captured_at.astimezone(UTC) + timedelta(seconds=seconds)
        return target.astimezone(_display_timezone(captured_at))
    except (OverflowError, TypeError, ValueError):
        return None


def _parse_time_today_or_next(raw: str, captured_at: datetime) -> datetime | None:
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    local_capture = (
        captured_at
        if captured_at.tzinfo is None
        else captured_at.astimezone(_display_timezone(captured_at))
    )
    parsed = local_capture.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if parsed < local_capture:
        try:
            parsed += timedelta(days=1)
        except (OverflowError, ValueError):
            return None
    return parsed


def _display_timezone(captured_at: datetime):
    timezone = captured_at.tzinfo
    if timezone is None:
        return None
    if isinstance(timezone, ZoneInfo):
        return timezone
    if captured_at.utcoffset() == timedelta(0):
        return timezone
    return LOCAL_TZ


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _finite_float(value)
    return _parse_number(value) if isinstance(value, str) else None


def _coerce_percent(value: Any) -> float | None:
    number = _coerce_number(value)
    return number if number is not None and 0 <= number <= 100 else None


def _percentages_are_complementary(
    used_percent: float | None,
    remaining_percent: float | None,
) -> bool:
    if used_percent is None or remaining_percent is None:
        return True
    return (
        abs((100 - used_percent) - remaining_percent)
        <= PERCENT_COMPLEMENT_TOLERANCE
    )


def _values_are_consistent(values: Iterable[float]) -> bool:
    values = tuple(values)
    return all(
        abs(first - second) <= PERCENT_COMPLEMENT_TOLERANCE
        for first in values
        for second in values
    )


def _normalize_ratio_value(value: Any) -> float | None:
    number = _coerce_number(value)
    if number is None:
        return None
    if 0 <= number <= 1:
        number *= 100
    return number if 0 <= number <= 100 else None


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
    try:
        coerced = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
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
        any(_contains_label(value, label) for label in FIVE_HOUR_LABELS)
        or any(
            _contains_label(value, word)
            for word in (
                "five_hour",
                "five-hour",
                "5_hour",
                "5-hour",
                "5h",
            )
        )
        or bool(re.search(r"\b5\s*h\b", value))
        or bool(re.search(r"\b5\b.{0,20}(hour|hours|stunden)", value))
    )


def _has_window_scope(value: str, target: str) -> bool:
    markers = (
        (
            "five_hour",
            "five-hour",
            "5_hour",
            "5-hour",
            "5h",
            "five hour",
        )
        if target == "five_hour"
        else (
            "weekly",
            "week",
            "woche",
            "wochen",
            "woechentlich",
            "wöchentlich",
        )
    )
    return any(_contains_label(value.casefold(), marker) for marker in markers)


def _path_has_component(path: str, component: str) -> bool:
    return re.search(
        rf"(?:^|\.){re.escape(component)}(?:\.|\[|$)",
        path,
    ) is not None


def _has_direct_structural_window(value: Any, target: str) -> bool:
    if not isinstance(value, dict):
        return False
    key = "primary_window" if target == "five_hour" else "secondary_window"
    return isinstance(value.get(key), dict)


def _is_structural_window_path(path: str) -> bool:
    return re.search(
        r"(?:^|\.)[^.\[]*window(?:_config)?(?:\.|\[|$)",
        path,
    ) is not None


def _looks_like_weekly(value: str) -> bool:
    return any(_contains_label(value, label) for label in WEEKLY_LABELS) or any(
        _contains_label(value, word)
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


def _contains_label(value: str, label: str) -> bool:
    return re.search(_label_pattern(label), value) is not None
