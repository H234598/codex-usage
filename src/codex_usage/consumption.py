from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice, pairwise
from typing import TypeGuard

from .history import MAX_HISTORY_SAMPLES, UsageSample

_UNIT_SECONDS = {"minutes": 60, "hours": 3_600, "days": 86_400, "weeks": 604_800}
_UNIT_LIMITS = {"minutes": 1_440, "hours": 720, "days": 365, "weeks": 365}
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
        try:
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
        except Exception:
            return {}


def consumption_lookback_seconds(amount: int, unit: str) -> int:
    if type(unit) is not str or unit not in _UNIT_SECONDS:
        raise ValueError("unit must be minutes, hours, days or weeks")
    if type(amount) is not int or not 0 < amount <= _UNIT_LIMITS[unit]:
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
        type(baseline_minutes) is not int
        or not 0 <= baseline_minutes <= 9_999
    ):
        raise ValueError("baseline_minutes must be between 0 and 9999")
    if baseline_value_minutes is not None and (
        type(baseline_value_minutes) is not int
        or not 0 <= baseline_value_minutes <= 9_999
    ):
        raise ValueError("baseline_value_minutes must be between 0 and 9999")
    _require_aware(now)
    try:
        now = datetime.fromtimestamp(datetime.timestamp(now), tz=UTC)
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise ValueError("now is out of range") from exc
    if (
        type(stale_after_seconds) is not int
        or stale_after_seconds <= 0
    ):
        raise ValueError("stale_after_seconds must be positive")
    if max_gap_seconds is None:
        max_gap_seconds = 3_600
    if (
        type(max_gap_seconds) is not int
        or max_gap_seconds <= 0
    ):
        raise ValueError("max_gap_seconds must be positive")
    if smoothing is None:
        smoothing_minutes = None
    elif type(smoothing) is not str:
        raise ValueError("smoothing must be none or ema-5 through ema-640")
    elif smoothing == "none":
        smoothing_minutes = None
    else:
        if not smoothing.startswith("ema-"):
            raise ValueError("smoothing must be none or ema-5 through ema-640")
        try:
            smoothing_minutes = int(smoothing[4:])
        except ValueError as exc:
            raise ValueError("smoothing must be none or ema-5 through ema-640") from exc
        if (
            smoothing != f"ema-{smoothing_minutes}"
            or smoothing_minutes not in {5, 10, 20, 40, 80, 160, 320, 640}
        ):
            raise ValueError("smoothing must be none or ema-5 through ema-640")
    try:
        sample_list = tuple(islice(samples, MAX_CONSUMPTION_SAMPLES + 1))
    except Exception as exc:
        raise ValueError("samples are invalid") from exc
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
        try:
            used_percent = float(sample.used_percent)
            if not math.isfinite(used_percent) or not 0 <= used_percent <= 100:
                raise ValueError("used_percent is invalid")
            sample_captured_at = sample.captured_at
            _ = (sample.account_id, sample.pool, sample.window_seconds)
        except Exception as exc:
            raise ValueError("samples are invalid") from exc
        if previous_captured_at is not None:
            try:
                out_of_order = sample_captured_at < previous_captured_at
            except Exception as exc:
                raise ValueError("samples are invalid") from exc
        else:
            out_of_order = False
        if out_of_order:
            already_ordered = False
        previous_captured_at = sample_captured_at
    if not already_ordered:
        try:
            ordered = tuple(sorted(sample_list, key=lambda sample: sample.captured_at))
        except Exception as exc:
            raise ValueError("samples are invalid") from exc
    first = ordered[0]
    try:
        account_id = first.account_id
        if any(sample.account_id != account_id for sample in ordered):
            raise ValueError("samples must use one account")
        pool = first.pool
        window_seconds = first.window_seconds
        if any(
            sample.pool != pool or sample.window_seconds != window_seconds
            for sample in ordered
        ):
            raise ValueError("samples must use one pool and limit window")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("samples are invalid") from exc
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
        baseline_value_start = (
            None
            if baseline_value_seconds is None
            else now - timedelta(seconds=baseline_value_seconds)
        )
    except (OverflowError, ValueError) as exc:
        raise ValueError("now is out of range") from exc
    baseline = None
    baseline_value = None
    try:
        for sample in ordered:
            if sample.captured_at <= start:
                baseline = sample
            if (
                baseline_value_start is not None
                and sample.captured_at <= baseline_value_start
            ):
                baseline_value = sample
            if sample.captured_at > now:
                break
        observations = [
            sample for sample in ordered if start <= sample.captured_at <= now
        ]
    except Exception as exc:
        raise ValueError("samples are invalid") from exc
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
    stale = (
        now - observations[-1].captured_at
    ).total_seconds() > stale_after_seconds
    consumed = 0.0
    cycle_start = observations[0].captured_at
    for previous, current in pairwise(observations):
        try:
            gap = (current.captured_at - previous.captured_at).total_seconds()
            if gap > max_gap_seconds:
                partial = True
            reset_boundary = _reset_boundary_at(previous, current)
            if reset_boundary is not None:
                # A reset starts a new limit cycle.  Keep only usage from
                # that cycle; otherwise an old cycle inflates Tokendelta.
                consumed = float(current.used_percent)
                cycle_start = reset_boundary
                if gap <= 0:
                    partial = True
                continue
            if gap <= 0:
                partial = True
                continue
            delta = float(current.used_percent) - float(previous.used_percent)
            if delta >= 0:
                consumed += delta
                continue
            partial = True
        except Exception as exc:
            raise ValueError("samples are invalid") from exc

    if stale:
        coverage = "stale"
    elif partial:
        coverage = "partial"
    else:
        coverage = "complete"
    estimate = None
    if coverage in {"complete", "partial"}:
        try:
            elapsed_seconds = (
                observations[-1].captured_at - cycle_start
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
        except Exception as exc:
            raise ValueError("samples are invalid") from exc
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
    try:
        return _ema_rate_impl(observations, time_constant_seconds, max_gap_seconds)
    except Exception:
        return 0.0


def _ema_rate_impl(
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
        boundary = _reset_boundary_at(previous, current)
        if boundary is not None:
            # Drop EMA history at cycle boundary as well as raw counter.
            if current.captured_at <= boundary:
                ema = None
            else:
                reset_gap = (current.captured_at - boundary).total_seconds()
                ema = float(current.used_percent) / reset_gap
            previous = current
            continue
        if gap <= 0 or gap > max_gap_seconds:
            previous = current
            continue
        delta = float(current.used_percent) - float(previous.used_percent)
        if delta < 0:
            delta = 0.0
        instantaneous = delta / gap
        alpha = 1.0 - math.exp(-gap / float(time_constant_seconds))
        ema = instantaneous if ema is None else ema + alpha * (instantaneous - ema)
        previous = current
    return max(0.0, float(ema or 0.0))


def _confirmed_reset(previous: UsageSample, current: UsageSample) -> bool:
    return _reset_boundary_at(previous, current) is not None


def _reset_boundary_at(
    previous: UsageSample, current: UsageSample
) -> datetime | None:
    try:
        previous_used = float(previous.used_percent)
        current_used = float(current.used_percent)
        if previous_used > 0 and current_used == 0:
            # Some providers omit reset metadata.  A positive usage counter
            # returning to zero is the observable reset boundary.
            return current.captured_at
        generation_changed = (
            previous.reset_generation is not None
            and current.reset_generation is not None
            and previous.reset_generation != current.reset_generation
        )
        if current.reset_at is None:
            return current.captured_at if generation_changed else None
        if (
            previous.reset_at is not None
            and previous.reset_at > previous.captured_at
            and previous.reset_at <= current.captured_at
        ):
            # Providers may keep reporting the same scheduled timestamp for
            # the first sample after rollover.  Crossing that timestamp is
            # enough evidence to start a fresh counter cycle.
            return previous.reset_at
        if (
            previous.captured_at < current.reset_at <= current.captured_at
            and current.reset_at != previous.reset_at
        ):
            return current.reset_at
        return None
    except Exception:
        return None


def _require_aware(value: datetime) -> None:
    if not _is_aware(value):
        raise ValueError("now must be timezone-aware")
    if value.tzinfo is not UTC:
        try:
            value.astimezone(UTC)
        except Exception as exc:
            raise ValueError("now is out of range") from exc


def _is_aware(value: object) -> TypeGuard[datetime]:
    if not isinstance(value, datetime):
        return False
    try:
        if value.tzinfo is None:
            return False
        return value.utcoffset() is not None
    except Exception:
        return False
