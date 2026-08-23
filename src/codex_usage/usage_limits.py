from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import islice
from typing import Any

from .extractor import LOCAL_TZ
from .models import LimitWindow, UsagePool

MAIN_POOL_KEY = "main"
SPARK_MODEL = "gpt-5.3-codex-spark"
SPARK_METERED_FEATURE = "codex_bengalfox"
FIVE_HOUR_SECONDS = 18_000
WEEKLY_SECONDS = 604_800
MAX_WINDOW_SECONDS = 10 * 365 * 24 * 60 * 60
MAX_MODEL_CATALOG_IDS = 100
APP_SERVER_LIMIT_REACHED_TYPES = frozenset(
    {
        "rate_limit_reached",
        "workspace_owner_credits_depleted",
        "workspace_member_credits_depleted",
        "workspace_owner_usage_limit_reached",
        "workspace_member_usage_limit_reached",
        # Older app-server builds exposed the reached window instead.
        "primary_window",
        "secondary_window",
    }
)


def parse_wham_usage_pools(
    payload: dict[str, Any],
    *,
    captured_at: datetime,
    source: str,
) -> tuple[UsagePool | None, tuple[UsagePool, ...]]:
    if not isinstance(payload, dict) or not isinstance(captured_at, datetime):
        return None, ()
    main = _wham_pool(
        key=MAIN_POOL_KEY,
        display_name="Codex",
        rate_limit=payload.get("rate_limit"),
        metered_feature=None,
        captured_at=captured_at,
        source=source,
    )
    models: list[UsagePool] = []
    spark_pool: UsagePool | None = None
    invalid_spark_entry = False
    conflicting_spark_entry = False
    additional = payload.get("additional_rate_limits")
    if isinstance(additional, list):
        for item in additional:
            if not isinstance(item, dict) or not _is_spark_limit(
                item.get("limit_name"), item.get("metered_feature")
            ):
                continue
            pool = _wham_pool(
                key=SPARK_MODEL,
                display_name="GPT-5.3-Codex-Spark",
                rate_limit=item.get("rate_limit"),
                metered_feature=SPARK_METERED_FEATURE,
                captured_at=captured_at,
                source=source,
            )
            if pool is None:
                invalid_spark_entry = True
                continue
            if spark_pool is None:
                spark_pool = pool
            elif pool != spark_pool:
                conflicting_spark_entry = True
    if spark_pool is not None:
        if invalid_spark_entry or conflicting_spark_entry:
            spark_pool = replace(spark_pool, available=False)
        models.append(spark_pool)
    elif invalid_spark_entry:
        models.append(
            UsagePool(
                key=SPARK_MODEL,
                display_name="GPT-5.3-Codex-Spark",
                available=False,
                metered_feature=SPARK_METERED_FEATURE,
                availability_sources=("usage",),
            )
        )
    return main, tuple(models)


def parse_app_server_usage_pools(
    payload: dict[str, Any],
    *,
    captured_at: datetime,
    model_ids: Iterable[str] = (),
    source: str = "app-server",
) -> tuple[UsagePool | None, tuple[UsagePool, ...]]:
    if not isinstance(payload, dict) or not isinstance(captured_at, datetime):
        return None, ()
    raw_by_id = payload.get("rateLimitsByLimitId")
    malformed_by_id = raw_by_id is not None and not isinstance(raw_by_id, dict)
    by_id = raw_by_id if isinstance(raw_by_id, dict) else {}
    raw_main_payload = by_id.get("codex")
    malformed_main_bucket = "codex" in by_id and not isinstance(raw_main_payload, dict)
    main_payload = by_id.get("codex")
    malformed_main_window = False
    if isinstance(main_payload, dict):
        top_level_payload = payload.get("rateLimits")
        if isinstance(top_level_payload, dict):
            merged_payload = dict(top_level_payload)
            for key, value in main_payload.items():
                if key in {"primary", "secondary"}:
                    if value is None:
                        continue
                    if (
                        isinstance(value, dict)
                        and value.get("windowDurationMins") is None
                    ):
                        top_level_window = top_level_payload.get(key)
                        top_level_minutes = (
                            _strict_int(top_level_window.get("windowDurationMins"))
                            if isinstance(top_level_window, dict)
                            else None
                        )
                        if (
                            isinstance(top_level_window, dict)
                            and top_level_window.get("windowDurationMins") is not None
                            and top_level_minutes
                            not in {FIVE_HOUR_SECONDS // 60, WEEKLY_SECONDS // 60}
                        ):
                            # A duration-less nested bucket cannot reclassify
                            # an explicit unsupported top-level window.
                            continue
                    if not isinstance(value, dict) or _app_server_window(
                        value, captured_at=captured_at, source=source
                    ) is None:
                        malformed_main_window = True
                        continue
                merged_payload[key] = value
            main_payload = merged_payload
    if not isinstance(main_payload, dict):
        main_payload = payload.get("rateLimits")
    main: UsagePool | None
    if malformed_by_id or malformed_main_bucket:
        main = UsagePool(
            key=MAIN_POOL_KEY,
            display_name="Codex",
            available=False,
            availability_sources=("usage",),
        )
    else:
        main = _app_server_pool(
            key=MAIN_POOL_KEY,
            display_name="Codex",
            snapshot=main_payload,
            metered_feature=None,
            captured_at=captured_at,
            source=source,
        )
    if malformed_main_window:
        if main is None:
            main = UsagePool(
                key=MAIN_POOL_KEY,
                display_name="Codex",
                available=False,
                availability_sources=("usage",),
            )
        else:
            main = replace(main, available=False)

    invalid_spark_entry = False
    spark_pool: UsagePool | None = None
    conflicting_spark_entry = False

    def remember_spark_payload(spark_payload: dict[str, Any]) -> None:
        nonlocal conflicting_spark_entry, invalid_spark_entry, spark_pool
        spark = _app_server_pool(
            key=SPARK_MODEL,
            display_name="GPT-5.3-Codex-Spark",
            snapshot=spark_payload,
            metered_feature=SPARK_METERED_FEATURE,
            captured_at=captured_at,
            source=source,
        )
        if spark is None:
            invalid_spark_entry = True
        elif spark_pool is None:
            spark_pool = spark
        elif spark != spark_pool:
            conflicting_spark_entry = True

    exact_spark_payload = by_id.get(SPARK_METERED_FEATURE)
    if SPARK_METERED_FEATURE in by_id and not isinstance(exact_spark_payload, dict):
        invalid_spark_entry = True
    elif isinstance(exact_spark_payload, dict):
        remember_spark_payload(exact_spark_payload)
    for value in by_id.values():
        if (
            isinstance(value, dict)
            and value is not exact_spark_payload
            and _is_spark_limit(value.get("limitName"), value.get("limitId"))
        ):
            remember_spark_payload(value)

    models: tuple[UsagePool, ...] = ()
    if spark_pool is not None:
        if invalid_spark_entry or conflicting_spark_entry:
            spark_pool = replace(spark_pool, available=False)
        models = (spark_pool,)
    elif invalid_spark_entry:
        models = (
            UsagePool(
                key=SPARK_MODEL,
                display_name="GPT-5.3-Codex-Spark",
                available=False,
                metered_feature=SPARK_METERED_FEATURE,
                availability_sources=("usage",),
            ),
        )
    return main, merge_model_catalog(models, model_ids)


def merge_model_catalog(
    pools: Iterable[UsagePool], model_ids: Iterable[str]
) -> tuple[UsagePool, ...]:
    try:
        pool_values = tuple(islice(pools, MAX_MODEL_CATALOG_IDS + 1))
    except (TypeError, ValueError):
        return ()
    if len(pool_values) > MAX_MODEL_CATALOG_IDS or any(
        not isinstance(pool, UsagePool) for pool in pool_values
    ):
        return ()
    result = list(pool_values)
    if isinstance(model_ids, (str, bytes, bytearray, Mapping)):
        model_id_values: tuple[Any, ...] = ()
    else:
        try:
            model_id_values = tuple(islice(model_ids, MAX_MODEL_CATALOG_IDS + 1))
        except (TypeError, ValueError):
            model_id_values = ()
        if len(model_id_values) > MAX_MODEL_CATALOG_IDS:
            model_id_values = ()
    if any(not isinstance(value, str) for value in model_id_values):
        model_id_values = ()
    spark_in_catalog = SPARK_MODEL in model_id_values
    spark_index = next(
        (index for index, pool in enumerate(result) if pool.key == SPARK_MODEL),
        None,
    )
    if spark_in_catalog and spark_index is None:
        result.append(
            UsagePool(
                key=SPARK_MODEL,
                display_name="GPT-5.3-Codex-Spark",
                # Catalog entitlement alone is not usage evidence.
                available=False,
                metered_feature=SPARK_METERED_FEATURE,
                availability_sources=("model_catalog",),
            )
        )
    elif spark_in_catalog and spark_index is not None:
        pool = result[spark_index]
        result[spark_index] = replace(
            pool,
            availability_sources=_unique(
                (*pool.availability_sources, "model_catalog")
            ),
        )
    return tuple(result)


def legacy_windows(
    main: UsagePool | None,
) -> tuple[LimitWindow | None, LimitWindow | None]:
    if main is None:
        return None, None
    return (
        main.window_for_duration(FIVE_HOUR_SECONDS),
        main.window_for_duration(WEEKLY_SECONDS),
    )


def _wham_pool(
    *,
    key: str,
    display_name: str,
    rate_limit: Any,
    metered_feature: str | None,
    captured_at: datetime,
    source: str,
) -> UsagePool | None:
    if not isinstance(rate_limit, dict):
        return None
    raw_windows = tuple(rate_limit.get(slot) for slot in ("primary_window", "secondary_window"))
    parsed_windows = tuple(
        _wham_window(raw, captured_at=captured_at, source=source)
        if raw is not None
        else None
        for raw in raw_windows
    )
    windows = tuple(window for window in parsed_windows if window is not None)
    window_shape_valid = all(
        raw is None or window is not None
        for raw, window in zip(raw_windows, parsed_windows, strict=True)
    )
    window_identity_valid = bool(windows) and _window_identities_are_unique(windows)
    raw_allowed = rate_limit.get("allowed")
    raw_limit_reached = rate_limit.get("limit_reached")
    allowed = _optional_bool(raw_allowed)
    limit_reached = _optional_bool(raw_limit_reached)
    control_flags_valid = all(
        value is None or isinstance(value, bool)
        for value in (raw_allowed, raw_limit_reached)
    )
    if (
        not windows
        and raw_allowed is None
        and raw_limit_reached is None
    ):
        return None
    return UsagePool(
        key=key,
        display_name=display_name,
        windows=windows,
        available=control_flags_valid and window_shape_valid and window_identity_valid,
        allowed=allowed,
        limit_reached=limit_reached,
        metered_feature=metered_feature,
        availability_sources=("usage",),
    )


def _app_server_pool(
    *,
    key: str,
    display_name: str,
    snapshot: Any,
    metered_feature: str | None,
    captured_at: datetime,
    source: str,
) -> UsagePool | None:
    if not isinstance(snapshot, dict):
        return None
    raw_windows = tuple(snapshot.get(slot) for slot in ("primary", "secondary"))
    fallback_durations: list[int | None] = list(
        (FIVE_HOUR_SECONDS, WEEKLY_SECONDS)
        if key == MAIN_POOL_KEY
        else (None, None)
    )
    if key == MAIN_POOL_KEY:
        ignored_missing_duration = [False, False]
        for index, fallback_duration in enumerate(fallback_durations):
            current = raw_windows[index]
            other = raw_windows[1 - index]
            if not isinstance(current, dict) or current.get("windowDurationMins") is not None:
                continue
            if not isinstance(other, dict) or other.get("windowDurationMins") is None:
                continue
            other_minutes = _strict_int(other.get("windowDurationMins"))
            other_duration = (
                other_minutes * 60 if other_minutes is not None else None
            )
            if other_duration not in {FIVE_HOUR_SECONDS, WEEKLY_SECONDS}:
                fallback_durations[index] = None
            elif other_duration == fallback_duration:
                # The explicit bucket already identifies this slot's window;
                # inferring the same duration would create a duplicate identity.
                fallback_durations[index] = None
            if fallback_durations[index] is None:
                ignored_missing_duration[index] = True
    else:
        ignored_missing_duration = [False, False]
    parsed_windows = tuple(
        _app_server_window(
            raw,
            captured_at=captured_at,
            source=source,
            fallback_duration_seconds=fallback_duration,
        )
        if raw is not None and not ignored_missing_duration[index]
        else None
        for index, (raw, fallback_duration) in enumerate(zip(
            raw_windows,
            fallback_durations,
            strict=True,
        ))
    )
    windows = tuple(window for window in parsed_windows if window is not None)
    window_shape_valid = all(
        raw is None or window is not None or ignored_missing_duration[index]
        for index, (raw, window) in enumerate(zip(raw_windows, parsed_windows, strict=True))
    )
    window_identity_valid = bool(windows) and _window_identities_are_unique(windows)
    raw_limit_reached = snapshot.get("rateLimitReachedType")
    limit_reached: bool | None
    control_flag_valid: bool
    if isinstance(raw_limit_reached, bool):
        limit_reached = raw_limit_reached
        control_flag_valid = True
    elif isinstance(raw_limit_reached, str):
        control_flag_valid = raw_limit_reached in APP_SERVER_LIMIT_REACHED_TYPES
        limit_reached = True if control_flag_valid else None
    else:
        limit_reached = None
        control_flag_valid = raw_limit_reached is None
    if not windows and raw_limit_reached is None:
        return None
    return UsagePool(
        key=key,
        display_name=display_name,
        windows=windows,
        available=control_flag_valid and window_shape_valid and window_identity_valid,
        limit_reached=limit_reached,
        metered_feature=metered_feature,
        availability_sources=("usage",),
    )


def _wham_window(
    value: Any, *, captured_at: datetime, source: str
) -> LimitWindow | None:
    if not isinstance(value, dict):
        return None
    duration = _positive_int(value.get("limit_window_seconds"))
    if "limit_window_seconds" in value and duration is None:
        return None
    used = _percent(value.get("used_percent"))
    reset_at = _reset_at(
        value.get("reset_at"),
        value.get("reset_after_seconds"),
        captured_at=captured_at,
    )
    if duration is None and used is None and reset_at is None:
        return None
    return _window(duration, used, reset_at, source=source)


def _app_server_window(
    value: Any,
    *,
    captured_at: datetime,
    source: str,
    fallback_duration_seconds: int | None = None,
) -> LimitWindow | None:
    if not isinstance(value, dict):
        return None
    raw_duration_minutes = value.get("windowDurationMins")
    duration_minutes = _strict_int(raw_duration_minutes)
    if raw_duration_minutes is not None and (
        duration_minutes is None
        or duration_minutes <= 0
        or duration_minutes * 60 > MAX_WINDOW_SECONDS
    ):
        return None
    duration = (
        duration_minutes * 60
        if duration_minutes is not None
        else fallback_duration_seconds
    )
    used = _percent(value.get("usedPercent"))
    reset_at = _reset_at(value.get("resetsAt"), None, captured_at=captured_at)
    if duration is None and used is None and reset_at is None:
        return None
    return _window(duration, used, reset_at, source=source)


def _window(
    duration: int | None,
    used: float | None,
    reset_at: datetime | None,
    *,
    source: str,
) -> LimitWindow:
    remaining = 100.0 - used if used is not None else None
    return LimitWindow(
        name=_window_name(duration),
        used=used,
        limit=100.0 if used is not None else None,
        remaining=remaining,
        percent=remaining,
        reset_at=reset_at,
        source=source,
        duration_seconds=duration,
    )


def _window_name(duration: int | None) -> str:
    if duration == FIVE_HOUR_SECONDS:
        return "5h"
    if duration == WEEKLY_SECONDS:
        return "weekly"
    if duration is None:
        return "unknown"
    if duration % 86_400 == 0:
        return f"{duration // 86_400}d"
    if duration % 3_600 == 0:
        return f"{duration // 3_600}h"
    return f"{duration}s"


def _window_identities_are_unique(windows: tuple[LimitWindow, ...]) -> bool:
    identities: list[int] = []
    for window in windows:
        if not isinstance(window, LimitWindow) or not window.has_known_identity:
            return False
        if window.duration_seconds is not None:
            identities.append(window.duration_seconds)
            continue
        if not isinstance(window.name, str):
            return False
        duration = {
            "5h": FIVE_HOUR_SECONDS,
            "5_hour": FIVE_HOUR_SECONDS,
            "five_hour": FIVE_HOUR_SECONDS,
            "w": WEEKLY_SECONDS,
            "week": WEEKLY_SECONDS,
            "weekly": WEEKLY_SECONDS,
            "30d": 2_592_000,
            "30_day": 2_592_000,
            "month": 2_592_000,
            "monthly": 2_592_000,
        }.get(window.name.strip().casefold())
        if duration is None:
            return False
        identities.append(duration)
    return len(identities) == len(set(identities))


def _reset_at(
    absolute: Any, relative: Any, *, captured_at: datetime
) -> datetime | None:
    epoch = _strict_int(absolute)
    if epoch is not None and epoch > 0:
        try:
            return datetime.fromtimestamp(epoch, tz=UTC).astimezone(LOCAL_TZ)
        except (OSError, OverflowError, ValueError):
            pass
    after = _nonnegative_int(relative)
    if after is None or after > MAX_WINDOW_SECONDS:
        return None
    try:
        return captured_at.astimezone(LOCAL_TZ) + timedelta(seconds=after)
    except Exception:
        return None


def _is_spark_limit(name: Any, metered_feature: Any) -> bool:
    return _normalized(name) == SPARK_MODEL or _normalized(
        metered_feature
    ) == SPARK_METERED_FEATURE


def _normalized(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    if any(
        char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F
        for char in value
    ):
        return ""
    return value.casefold()


def _percent(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if 0 <= number <= 100 else None


def _positive_int(value: Any) -> int | None:
    number = _strict_int(value)
    if number is None or not 0 < number <= MAX_WINDOW_SECONDS:
        return None
    return number


def _nonnegative_int(value: Any) -> int | None:
    number = _strict_int(value)
    return number if number is not None and number >= 0 else None


def _strict_int(value: Any) -> int | None:
    return value if type(value) is int else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
