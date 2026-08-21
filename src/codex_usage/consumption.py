from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice, pairwise

from .history import MAX_HISTORY_SAMPLES, UsageSample

_UNIT_SECONDS = {"minutes": 60, "hours": 3_600, "days": 86_400}
_UNIT_LIMITS = {"minutes": 1_440, "hours": 720, "days": 365}
MAX_CONSUMPTION_SAMPLES = MAX_HISTORY_SAMPLES
MAX_FORECAST_SECONDS = 31_536_000


@dataclass(frozen=True)
class ConsumptionWindow:
    lookback_seconds: int
    pool: str
    limit_window_seconds: int
    consumed_percentage_points: float
    coverage: str
    sample_count: int
    estimated_seconds_to_exhaustion: int | None = None
    baseline_used_percent: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "lookback_seconds": self.lookback_seconds,
            "pool": self.pool,
            "limit_window_seconds": self.limit_window_seconds,
            "consumed_percentage_points": self.consumed_percentage_points,
            "coverage": self.coverage,
            "sample_count": self.sample_count,
            "estimated_seconds_to_exhaustion": self.estimated_seconds_to_exhaustion,
            "baseline_used_percent": self.baseline_used_percent,
        }


def consumption_lookback_seconds(amount: int, unit: str) -> int:
    if not isinstance(unit, str) or unit not in _UNIT_SECONDS:
        raise ValueError("unit must be minutes, hours or days")
    if (
        isinstance(amount, bool)
        or not isinstance(amount, int)
        or not 0 < amount <= _UNIT_LIMITS[unit]
    ):
        raise ValueError(f"amount must be between 1 and {_UNIT_LIMITS[unit]}")
    return amount * _UNIT_SECONDS[unit]


def calculate_consumption(
    samples: Iterable[UsageSample],
    *,
    amount: int,
    unit: str,
    now: datetime,
    baseline_minutes: int | None = None,
    baseline_value_minutes: int | None = None,
    stale_after_seconds: int = 900,
    max_gap_seconds: int | None = None,
    smoothing: str | None = None,
) -> ConsumptionWindow:
    lookback_seconds = consumption_lookback_seconds(amount, unit)
    if baseline_minutes is not None and (
        isinstance(baseline_minutes, bool)
        or not isinstance(baseline_minutes, int)
        or not 0 <= baseline_minutes <= 9_999
    ):
        raise ValueError("baseline_minutes must be between 0 and 9999")
    if baseline_value_minutes is not None and (
        isinstance(baseline_value_minutes, bool)
        or not isinstance(baseline_value_minutes, int)
        or not 0 <= baseline_value_minutes <= 9_999
    ):
        raise ValueError("baseline_value_minutes must be between 0 and 9999")
    _require_aware(now)
    if (
        isinstance(stale_after_seconds, bool)
        or not isinstance(stale_after_seconds, int)
        or stale_after_seconds <= 0
    ):
        raise ValueError("stale_after_seconds must be positive")
    if max_gap_seconds is None:
        max_gap_seconds = 3_600
    if (
        isinstance(max_gap_seconds, bool)
        or not isinstance(max_gap_seconds, int)
        or max_gap_seconds <= 0
    ):
        raise ValueError("max_gap_seconds must be positive")
    if smoothing not in (None, "none"):
        if not isinstance(smoothing, str) or not smoothing.startswith("ema-"):
            raise ValueError("smoothing must be none or ema-5 through ema-640")
        try:
            smoothing_minutes = int(smoothing[4:])
        except ValueError as exc:
            raise ValueError("smoothing must be none or ema-5 through ema-640") from exc
        if smoothing_minutes not in {5, 10, 20, 40, 80, 160, 320, 640}:
            raise ValueError("smoothing must be none or ema-5 through ema-640")
    else:
        smoothing_minutes = None

    try:
        sample_list = tuple(islice(samples, MAX_CONSUMPTION_SAMPLES + 1))
    except TypeError:
        raise ValueError("samples are invalid") from None
    if len(sample_list) > MAX_CONSUMPTION_SAMPLES:
        raise ValueError("too many samples")
    if not sample_list:
        return ConsumptionWindow(
            lookback_seconds=lookback_seconds,
            pool="main",
            limit_window_seconds=0,
            consumed_percentage_points=0.0,
            coverage="insufficient",
            sample_count=0,
            estimated_seconds_to_exhaustion=None,
            baseline_used_percent=None,
        )
    ordered = sample_list
    previous_captured_at = None
    already_ordered = True
    for sample in sample_list:
        if not isinstance(sample, UsageSample):
            raise ValueError("samples are invalid")
        if (
            previous_captured_at is not None
            and sample.captured_at < previous_captured_at
        ):
            already_ordered = False
        previous_captured_at = sample.captured_at
    if not already_ordered:
        ordered = tuple(sorted(sample_list, key=lambda sample: sample.captured_at))
    first = ordered[0]
    account_id = first.account_id
    if any(sample.account_id != account_id for sample in ordered):
        raise ValueError("samples must use one account")
    pool = first.pool
    window_seconds = first.window_seconds
    if any(sample.pool != pool or sample.window_seconds != window_seconds for sample in ordered):
        raise ValueError("samples must use one pool and limit window")
    baseline_seconds = (
        lookback_seconds
        if baseline_minutes is None
        else baseline_minutes * 60
    )
    baseline_value_seconds = (
        None if baseline_value_minutes is None else baseline_value_minutes * 60
    )
    try:
        start = now - timedelta(seconds=baseline_seconds)
    except (OverflowError, ValueError) as exc:
        raise ValueError("now is out of range") from exc
    baseline = None
    baseline_value = None
    for sample in ordered:
        if sample.captured_at <= start:
            baseline = sample
        if baseline_value_seconds is not None and sample.captured_at <= now - timedelta(
            seconds=baseline_value_seconds
        ):
            baseline_value = sample
        if sample.captured_at > now:
            break
    observations = [sample for sample in ordered if start <= sample.captured_at <= now]
    if baseline is not None and (not observations or observations[0] != baseline):
        observations.insert(0, baseline)
    if len(observations) < 2:
        return ConsumptionWindow(
            lookback_seconds=lookback_seconds,
            pool=pool,
            limit_window_seconds=window_seconds,
            consumed_percentage_points=0.0,
            coverage="insufficient",
            sample_count=len(observations),
            estimated_seconds_to_exhaustion=None,
            baseline_used_percent=(
                float(baseline_value.used_percent)
                if baseline_value is not None
                else (float(baseline.used_percent) if baseline is not None else None)
            ),
        )

    partial = baseline is None or observations[0].captured_at > start
    stale = now - observations[-1].captured_at > timedelta(seconds=stale_after_seconds)
    consumed = 0.0
    for previous, current in pairwise(observations):
        gap = (current.captured_at - previous.captured_at).total_seconds()
        if gap > max_gap_seconds:
            partial = True
        delta = float(current.used_percent) - float(previous.used_percent)
        if delta >= 0:
            consumed += delta
            continue
        if _confirmed_reset(previous, current):
            consumed += float(current.used_percent)
            continue
        partial = True

    if stale:
        coverage = "stale"
    elif partial:
        coverage = "partial"
    else:
        coverage = "complete"
    estimate = None
    if coverage in {"complete", "partial"}:
        elapsed_seconds = (
            observations[-1].captured_at - observations[0].captured_at
        ).total_seconds()
        remaining_percent = max(0.0, 100.0 - float(observations[-1].used_percent))
        rate = consumed / elapsed_seconds if elapsed_seconds > 0 else 0.0
        if smoothing_minutes is not None:
            rate = _ema_rate(observations, smoothing_minutes * 60, max_gap_seconds)
        if remaining_percent == 0:
            estimate = 0
        elif rate > 0:
            forecast_seconds = remaining_percent / rate
            if math.isfinite(forecast_seconds):
                candidate = math.ceil(forecast_seconds)
                if candidate <= MAX_FORECAST_SECONDS:
                    estimate = candidate
    return ConsumptionWindow(
        lookback_seconds=lookback_seconds,
        pool=pool,
        limit_window_seconds=window_seconds,
        consumed_percentage_points=round(max(0.0, consumed), 6),
        coverage=coverage,
        sample_count=len(observations),
        estimated_seconds_to_exhaustion=estimate,
        baseline_used_percent=(
            float(baseline_value.used_percent)
            if baseline_value is not None
            else (float(baseline.used_percent) if baseline is not None else None)
        ),
    )


def _ema_rate(
    observations: list[UsageSample], time_constant_seconds: int, max_gap_seconds: int
) -> float:
    """Return a time-aware EMA of positive usage rate, preserving reset semantics."""
    ema = None
    previous = None
    for current in observations:
        if previous is None:
            previous = current
            continue
        gap = (current.captured_at - previous.captured_at).total_seconds()
        if gap <= 0 or gap > max_gap_seconds:
            previous = current
            continue
        delta = float(current.used_percent) - float(previous.used_percent)
        if delta < 0:
            delta = float(current.used_percent) if _confirmed_reset(previous, current) else 0.0
        instantaneous = delta / gap
        alpha = 1.0 - math.exp(-gap / float(time_constant_seconds))
        ema = instantaneous if ema is None else ema + alpha * (instantaneous - ema)
        previous = current
    return max(0.0, float(ema or 0.0))


def _confirmed_reset(previous: UsageSample, current: UsageSample) -> bool:
    generation_changed = (
        previous.reset_generation is not None
        and current.reset_generation is not None
        and previous.reset_generation != current.reset_generation
    )
    if current.reset_at is None:
        return generation_changed
    if current.reset_at <= previous.captured_at:
        return False
    return current.reset_at <= current.captured_at and current.reset_at != previous.reset_at


def _require_aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if value.tzinfo is not UTC:
        value.astimezone(UTC)
