from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .extractor import LOCAL_TZ
from .models import LimitWindow, UsagePool

MAIN_POOL_KEY = "main"
SPARK_MODEL = "gpt-5.3-codex-spark"
SPARK_METERED_FEATURE = "codex_bengalfox"
FIVE_HOUR_SECONDS = 18_000
WEEKLY_SECONDS = 604_800
MAX_WINDOW_SECONDS = 10 * 365 * 24 * 60 * 60


def parse_wham_usage_pools(
    payload: dict[str, Any],
    *,
    captured_at: datetime,
    source: str,
) -> tuple[UsagePool | None, tuple[UsagePool, ...]]:
    main = _wham_pool(
        key=MAIN_POOL_KEY,
        display_name="Codex",
        rate_limit=payload.get("rate_limit"),
        metered_feature=None,
        captured_at=captured_at,
        source=source,
    )
    models: list[UsagePool] = []
    additional = payload.get("additional_rate_limits")
    if isinstance(additional, list):
        for item in additional[:100]:
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
            if pool is not None:
                models.append(pool)
                break
    return main, tuple(models)


def parse_app_server_usage_pools(
    payload: dict[str, Any],
    *,
    captured_at: datetime,
    model_ids: Iterable[str] = (),
    source: str = "app-server",
) -> tuple[UsagePool | None, tuple[UsagePool, ...]]:
    by_id = payload.get("rateLimitsByLimitId")
    by_id = by_id if isinstance(by_id, dict) else {}
    main_payload = by_id.get("codex")
    if not isinstance(main_payload, dict):
        main_payload = payload.get("rateLimits")
    main = _app_server_pool(
        key=MAIN_POOL_KEY,
        display_name="Codex",
        snapshot=main_payload,
        metered_feature=None,
        captured_at=captured_at,
        source=source,
    )

    spark_payload = by_id.get(SPARK_METERED_FEATURE)
    if not isinstance(spark_payload, dict):
        spark_payload = next(
            (
                value
                for value in by_id.values()
                if isinstance(value, dict)
                and _is_spark_limit(value.get("limitName"), value.get("limitId"))
            ),
            None,
        )
    models: tuple[UsagePool, ...] = ()
    if isinstance(spark_payload, dict):
        spark = _app_server_pool(
            key=SPARK_MODEL,
            display_name="GPT-5.3-Codex-Spark",
            snapshot=spark_payload,
            metered_feature=SPARK_METERED_FEATURE,
            captured_at=captured_at,
            source=source,
        )
        if spark is not None:
            models = (spark,)
    return main, merge_model_catalog(models, model_ids)


def merge_model_catalog(
    pools: Iterable[UsagePool], model_ids: Iterable[str]
) -> tuple[UsagePool, ...]:
    result = list(pools)
    spark_in_catalog = any(_normalized(value) == SPARK_MODEL for value in model_ids)
    spark_index = next(
        (index for index, pool in enumerate(result) if pool.key == SPARK_MODEL),
        None,
    )
    if spark_in_catalog and spark_index is None:
        result.append(
            UsagePool(
                key=SPARK_MODEL,
                display_name="GPT-5.3-Codex-Spark",
                available=True,
                metered_feature=SPARK_METERED_FEATURE,
                availability_sources=("model_catalog",),
            )
        )
    elif spark_in_catalog and spark_index is not None:
        pool = result[spark_index]
        result[spark_index] = replace(
            pool,
            available=True,
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
    windows = tuple(
        window
        for slot in ("primary_window", "secondary_window")
        if (
            window := _wham_window(
                rate_limit.get(slot), captured_at=captured_at, source=source
            )
        )
        is not None
    )
    allowed = _optional_bool(rate_limit.get("allowed"))
    limit_reached = _optional_bool(rate_limit.get("limit_reached"))
    if not windows and allowed is None and limit_reached is None:
        return None
    return UsagePool(
        key=key,
        display_name=display_name,
        windows=windows,
        available=True,
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
    windows = tuple(
        window
        for slot in ("primary", "secondary")
        if (
            window := _app_server_window(
                snapshot.get(slot), captured_at=captured_at, source=source
            )
        )
        is not None
    )
    reached_type = snapshot.get("rateLimitReachedType")
    limit_reached = bool(reached_type) if isinstance(reached_type, str) else None
    if not windows and limit_reached is None:
        return None
    return UsagePool(
        key=key,
        display_name=display_name,
        windows=windows,
        available=True,
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
    value: Any, *, captured_at: datetime, source: str
) -> LimitWindow | None:
    if not isinstance(value, dict):
        return None
    duration_minutes = _positive_int(value.get("windowDurationMins"))
    if "windowDurationMins" in value and duration_minutes is None:
        return None
    duration = duration_minutes * 60 if duration_minutes is not None else None
    if duration is not None and duration > MAX_WINDOW_SECONDS:
        duration = None
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
    return captured_at.astimezone(LOCAL_TZ) + timedelta(seconds=after)


def _is_spark_limit(name: Any, metered_feature: Any) -> bool:
    return _normalized(name) == SPARK_MODEL or _normalized(
        metered_feature
    ) == SPARK_METERED_FEATURE


def _normalized(value: Any) -> str:
    return str(value or "").strip().casefold()


def _percent(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
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
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
