from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest

import codex_usage.consumption as consumption_module
from codex_usage.consumption import (
    ConsumptionWindow,
    _confirmed_reset,
    calculate_consumption,
    consumption_lookback_seconds,
)
from codex_usage.history import UsageSample

BASE = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)


class _BrokenInt(int):
    def __gt__(self, _other):
        raise RuntimeError("synthetic consumption integer marker")

    def __le__(self, _other):
        raise RuntimeError("synthetic consumption integer marker")

    def __mul__(self, _other):
        raise RuntimeError("synthetic consumption integer marker")


def _sample(
    offset_minutes: int,
    used: float,
    *,
    account_id: str = "alpha",
    generation: str = "a",
    reset_at=None,
):
    return UsageSample(
        account_id=account_id,
        pool="main",
        window_seconds=18_000,
        captured_at=BASE + timedelta(minutes=offset_minutes),
        used_percent=used,
        reset_generation=generation,
        reset_at=reset_at,
        source="test",
    )


def test_consumption_lookback_seconds_converts_supported_units():
    assert consumption_lookback_seconds(5, "minutes") == 300
    assert consumption_lookback_seconds(2, "hours") == 7_200
    assert consumption_lookback_seconds(1, "days") == 86_400
    assert consumption_lookback_seconds(1, "weeks") == 604_800


def test_consumption_rejects_integer_subclasses_before_arithmetic():
    broken = _BrokenInt(1)

    with pytest.raises(ValueError, match="amount must be between"):
        consumption_lookback_seconds(broken, "hours")

    for name in (
        "baseline_minutes",
        "baseline_value_minutes",
        "stale_after_seconds",
        "max_gap_seconds",
    ):
        with pytest.raises(ValueError, match=name):
            calculate_consumption(
                [],
                amount=1,
                unit="hours",
                now=BASE,
                **{name: broken},
            )


def test_consumption_window_as_dict_preserves_optional_forecast_fields():
    window = ConsumptionWindow(
        lookback_seconds=3_600,
        pool="main",
        limit_window_seconds=18_000,
        consumed_percentage_points=12.5,
        coverage="partial",
        sample_count=2,
        estimated_seconds_to_exhaustion=4_200,
        baseline_used_percent=10.0,
    )

    assert window.as_dict() == {
        "lookback_seconds": 3_600,
        "pool": "main",
        "limit_window_seconds": 18_000,
        "consumed_percentage_points": 12.5,
        "coverage": "partial",
        "sample_count": 2,
        "estimated_seconds_to_exhaustion": 4_200,
        "baseline_used_percent": 10.0,
    }


def test_consumption_confirmed_reset_accepts_crossed_reset_window():
    previous_reset = BASE + timedelta(minutes=30)
    previous = _sample(0, 90, reset_at=previous_reset)
    current = _sample(60, 5, reset_at=BASE + timedelta(hours=2))

    assert _confirmed_reset(previous, current) is True


def test_consumption_sums_positive_deltas_as_percentage_points():
    result = calculate_consumption(
        [_sample(0, 10), _sample(30, 20), _sample(60, 35)],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=60),
    )
    assert result.consumed_percentage_points == 25.0
    assert result.coverage == "complete"
    assert result.sample_count == 3


def test_consumption_rejects_samples_from_multiple_accounts():
    with pytest.raises(ValueError, match="samples must use one account"):
        calculate_consumption(
            [
                _sample(-30, 80, account_id="beta"),
                _sample(0, 10),
                _sample(30, 50),
            ],
            amount=30,
            unit="minutes",
            now=BASE + timedelta(minutes=30),
        )


def test_consumption_estimates_seconds_until_limit_exhaustion():
    result = calculate_consumption(
        [_sample(-30, 20), _sample(0, 50)],
        amount=30,
        unit="minutes",
        now=BASE,
    )

    assert result.estimated_seconds_to_exhaustion == 3_000
    assert result.as_dict()["estimated_seconds_to_exhaustion"] == 3_000


def test_consumption_forecast_is_null_without_positive_rate_or_complete_data():
    zero = calculate_consumption(
        [_sample(0, 20), _sample(30, 20)],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=30),
    )
    stale = calculate_consumption(
        [_sample(0, 20), _sample(30, 50)],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=60),
        stale_after_seconds=10,
    )

    assert zero.estimated_seconds_to_exhaustion is None
    assert stale.estimated_seconds_to_exhaustion is None


def test_consumption_keeps_pool_when_all_samples_are_outside_window():
    sample = UsageSample(
        account_id="alpha",
        pool="credits",
        window_seconds=2_592_000,
        captured_at=BASE + timedelta(minutes=30),
        used_percent=20,
        source="test",
    )

    result = calculate_consumption(
        [sample],
        amount=1,
        unit="hours",
        now=BASE,
    )

    assert result.pool == "credits"
    assert result.limit_window_seconds == 2_592_000
    assert result.coverage == "insufficient"


def test_consumption_forecast_is_available_as_approximation_with_fresh_partial_data():
    result = calculate_consumption(
        [_sample(0, 20), _sample(30, 50)],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=30),
    )

    assert result.coverage == "partial"
    assert result.estimated_seconds_to_exhaustion == 3_000


def test_consumption_smoothing_uses_time_aware_ema_rate():
    unsmoothed = calculate_consumption(
        [_sample(0, 0), _sample(10, 10), _sample(20, 30)],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=20),
    )
    smoothed = calculate_consumption(
        [_sample(0, 0), _sample(10, 10), _sample(20, 30)],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=20),
        smoothing="ema-5",
    )

    assert smoothed.coverage == "partial"
    assert smoothed.estimated_seconds_to_exhaustion is not None
    assert unsmoothed.estimated_seconds_to_exhaustion is not None
    assert smoothed.estimated_seconds_to_exhaustion < unsmoothed.estimated_seconds_to_exhaustion


def test_consumption_returns_insufficient_window_for_empty_samples():
    result = calculate_consumption([], amount=1, unit="hours", now=BASE)

    assert result.coverage == "insufficient"
    assert result.sample_count == 0
    assert result.pool == "main"


def test_consumption_rejects_noncanonical_smoothing_suffix():
    with pytest.raises(ValueError, match="smoothing"):
        calculate_consumption(
            [_sample(0, 10), _sample(30, 20)],
            amount=1,
            unit="hours",
            now=BASE + timedelta(minutes=30),
            smoothing="ema-abc",
        )


def test_consumption_rejects_non_usage_sample_entry():
    with pytest.raises(ValueError, match="samples are invalid"):
        calculate_consumption(
            [object()],
            amount=1,
            unit="hours",
            now=BASE,
        )


def test_consumption_sorts_samples_that_arrive_out_of_order():
    result = calculate_consumption(
        [_sample(30, 20), _sample(0, 10)],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=30),
    )

    assert result.consumed_percentage_points == 10.0
    assert result.coverage == "partial"


def test_consumption_rejects_mixed_pool_or_limit_window():
    other_pool = UsageSample(
        account_id="alpha",
        pool="credits",
        window_seconds=18_000,
        captured_at=BASE + timedelta(minutes=30),
        used_percent=20,
        source="test",
    )
    with pytest.raises(ValueError, match="one pool and limit window"):
        calculate_consumption(
            [_sample(0, 10), other_pool],
            amount=1,
            unit="hours",
            now=BASE + timedelta(minutes=30),
        )


def test_consumption_inserts_baseline_before_window_observations():
    result = calculate_consumption(
        [_sample(-90, 10), _sample(-30, 20)],
        amount=1,
        unit="hours",
        now=BASE,
    )

    assert result.sample_count == 2
    assert result.consumed_percentage_points == 10.0


def test_consumption_marks_large_observation_gap_partial():
    result = calculate_consumption(
        [_sample(0, 10), _sample(120, 20)],
        amount=3,
        unit="hours",
        now=BASE + timedelta(minutes=120),
        max_gap_seconds=3_600,
    )

    assert result.coverage == "partial"


def test_consumption_ema_skips_duplicate_and_large_gaps():
    rate = consumption_module._ema_rate(
        [_sample(0, 10), _sample(0, 20), _sample(120, 30)],
        time_constant_seconds=300,
        max_gap_seconds=3_600,
    )

    assert rate == 0.0


def test_consumption_ema_clamps_negative_delta():
    rate = consumption_module._ema_rate(
        [_sample(0, 50), _sample(30, 40)],
        time_constant_seconds=300,
        max_gap_seconds=3_600,
    )

    assert rate == 0.0


def test_consumption_is_aware_rejects_naive_datetime():
    assert consumption_module._is_aware(datetime(2026, 8, 16, 10, 0)) is False


@pytest.mark.parametrize("smoothing", ["ema-05", "ema+5", "ema-0005"])
def test_consumption_rejects_noncanonical_smoothing_names(smoothing):
    with pytest.raises(ValueError, match="smoothing"):
        calculate_consumption(
            [_sample(0, 10), _sample(30, 20)],
            amount=1,
            unit="hours",
            now=BASE + timedelta(minutes=30),
            smoothing=smoothing,
        )


def test_consumption_accepts_large_stale_threshold_without_overflow():
    result = calculate_consumption(
        [_sample(0, 20), _sample(30, 50)],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=30),
        stale_after_seconds=10**15,
    )

    assert result.coverage == "partial"


def test_consumption_forecast_is_zero_when_limit_is_exhausted():
    result = calculate_consumption(
        [_sample(-30, 20), _sample(0, 100)],
        amount=30,
        unit="minutes",
        now=BASE,
    )

    assert result.estimated_seconds_to_exhaustion == 0


def test_consumption_omits_unrepresentable_forecast_without_raising():
    result = calculate_consumption(
        [
            _sample(0, 0.0),
            UsageSample(
                account_id="alpha",
                pool="main",
                window_seconds=18_000,
                captured_at=BASE + timedelta(seconds=1),
                used_percent=5e-324,
                source="test",
            ),
        ],
        amount=1,
        unit="hours",
        now=BASE + timedelta(seconds=1),
    )

    assert result.estimated_seconds_to_exhaustion is None


def test_consumption_handles_confirmed_reset_without_false_partial_status():
    reset = BASE + timedelta(minutes=45)
    result = calculate_consumption(
        [_sample(0, 90), _sample(30, 95), _sample(60, 5, generation="b", reset_at=reset)],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=60),
    )
    assert result.consumed_percentage_points == 10.0
    assert result.coverage == "complete"


def test_consumption_counts_new_cycle_when_reset_usage_exceeds_old_value():
    samples = [
        _sample(0, 90, generation="old", reset_at=BASE + timedelta(minutes=30)),
        _sample(60, 95, generation="new", reset_at=BASE + timedelta(hours=2)),
    ]
    result = calculate_consumption(
        samples,
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=60),
    )
    smoothed = calculate_consumption(
        samples,
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=60),
        smoothing="ema-5",
    )

    assert result.consumed_percentage_points == 95.0
    assert result.coverage == "complete"
    assert smoothed.consumed_percentage_points == 95.0
    assert smoothed.estimated_seconds_to_exhaustion == 190


def test_consumption_handles_future_next_reset_after_rollover():
    previous_reset = BASE + timedelta(minutes=30)
    current_reset = BASE + timedelta(hours=6)
    result = calculate_consumption(
        [
            _sample(0, 90, generation="old", reset_at=previous_reset),
            _sample(60, 5, generation="new", reset_at=current_reset),
        ],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=60),
    )

    assert result.consumed_percentage_points == 5.0
    assert result.coverage == "complete"


def test_consumption_keeps_generation_only_reset_evidence():
    result = calculate_consumption(
        [
            _sample(0, 90, generation="old", reset_at=None),
            _sample(30, 10, generation="new", reset_at=None),
        ],
        amount=30,
        unit="minutes",
        now=BASE + timedelta(minutes=30),
    )

    assert result.consumed_percentage_points == 10.0
    assert result.coverage == "complete"


def test_consumption_does_not_treat_future_reset_shift_as_completed_reset():
    first_reset = BASE + timedelta(hours=5)
    shifted_reset = first_reset + timedelta(minutes=5)

    result = calculate_consumption(
        [
            _sample(0, 60, generation="old", reset_at=first_reset),
            _sample(30, 20, generation="new", reset_at=shifted_reset),
            _sample(60, 25, generation="new", reset_at=shifted_reset),
        ],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=60),
    )

    assert result.consumed_percentage_points == 5.0
    assert result.coverage == "partial"


def test_consumption_does_not_invent_usage_for_ambiguous_drop():
    result = calculate_consumption(
        [_sample(0, 60), _sample(30, 20), _sample(60, 25)],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=60),
    )
    assert result.consumed_percentage_points == 5.0
    assert result.coverage == "partial"


def test_consumption_skips_zero_elapsed_duplicate_timestamp_delta():
    result = calculate_consumption(
        [_sample(0, 10), _sample(0, 90), _sample(30, 100)],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=30),
    )

    assert result.consumed_percentage_points == 10.0
    assert result.coverage == "partial"


def test_consumption_uses_baseline_before_window_and_marks_stale():
    result = calculate_consumption(
        [_sample(-90, 10), _sample(-30, 20), _sample(30, 30)],
        amount=2,
        unit="hours",
        now=BASE + timedelta(minutes=90),
        stale_after_seconds=30,
    )
    assert result.consumed_percentage_points == 10.0
    assert result.coverage == "stale"


def test_consumption_can_use_explicit_baseline_minutes():
    result = calculate_consumption(
        [_sample(0, 10), _sample(30, 20), _sample(60, 35)],
        amount=1,
        unit="hours",
        baseline_minutes=30,
        now=BASE + timedelta(minutes=60),
    )
    assert result.consumed_percentage_points == 15.0
    assert result.lookback_seconds == 3600


def test_consumption_baseline_value_does_not_replace_delta_window():
    result = calculate_consumption(
        [_sample(-120, 10), _sample(-60, 20), _sample(0, 35)],
        amount=1,
        unit="hours",
        baseline_value_minutes=120,
        now=BASE,
    )

    assert result.consumed_percentage_points == 15.0
    assert result.baseline_used_percent == 10.0


def test_consumption_rejects_invalid_period():
    with pytest.raises(ValueError, match="unit"):
        calculate_consumption([], amount=1, unit="fortnights", now=BASE)


@pytest.mark.parametrize("unit", [None, [], {}])
def test_consumption_rejects_non_string_period(unit):
    with pytest.raises(ValueError, match="unit"):
        calculate_consumption([], amount=1, unit=unit, now=BASE)


@pytest.mark.parametrize("samples", [None, 1, True, object()])
def test_consumption_rejects_non_iterable_samples(samples):
    with pytest.raises(ValueError, match="samples are invalid"):
        calculate_consumption(samples, amount=1, unit="hours", now=BASE)  # type: ignore[arg-type]


def test_consumption_rejects_capture_time_out_of_range_for_lookback():
    with pytest.raises(ValueError, match="now is out of range"):
        calculate_consumption(
            [_sample(0, 1)],
            amount=1,
            unit="hours",
            now=datetime.min.replace(tzinfo=UTC),
        )


def test_consumption_rejects_capture_time_out_of_range_for_baseline_value():
    with pytest.raises(ValueError, match="now is out of range"):
        calculate_consumption(
            [_sample(0, 1)],
            amount=1,
            unit="minutes",
            baseline_value_minutes=9_999,
            now=datetime.min.replace(tzinfo=UTC) + timedelta(seconds=60),
        )


def test_consumption_rejects_timestamp_normalization_overflow():
    with pytest.raises(ValueError, match="now is out of range"):
        calculate_consumption(
            [],
            amount=1,
            unit="hours",
            now=datetime.max.replace(tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "now",
    [
        datetime.min.replace(tzinfo=timezone(timedelta(hours=14))),
        datetime.max.replace(tzinfo=timezone(timedelta(hours=-14))),
    ],
)
def test_consumption_rejects_now_with_unrepresentable_timezone_conversion(now):
    with pytest.raises(ValueError, match="now is out of range"):
        calculate_consumption([], amount=1, unit="hours", now=now)


class _RaisingTimezone(tzinfo):
    def utcoffset(self, _value):
        raise RuntimeError("synthetic timezone marker")


class _BrokenNow(datetime):
    def __sub__(self, _other):
        return "not-a-datetime"

    def timestamp(self):
        return "not-a-timestamp"


def test_consumption_rejects_timezone_callbacks_that_raise():
    with pytest.raises(ValueError, match="timezone-aware"):
        calculate_consumption(
            [],
            amount=1,
            unit="hours",
            now=datetime(2026, 8, 16, 10, 0, tzinfo=_RaisingTimezone()),
        )


def test_consumption_normalizes_datetime_subclass_before_arithmetic():
    result = calculate_consumption(
        [_sample(-30, 10), _sample(0, 20)],
        amount=1,
        unit="hours",
        now=_BrokenNow(2026, 8, 16, 10, 0, tzinfo=UTC),
    )

    assert result.consumed_percentage_points == 10.0
    assert result.coverage == "partial"


def test_consumption_rejects_sample_iterators_over_cap(monkeypatch):
    monkeypatch.setattr(consumption_module, "MAX_CONSUMPTION_SAMPLES", 2)

    with pytest.raises(ValueError, match="too many samples"):
        calculate_consumption(
            (_sample(index, index) for index in range(3)),
            amount=1,
            unit="hours",
            now=BASE + timedelta(minutes=2),
        )


def test_consumption_does_not_sort_already_ordered_samples(monkeypatch):
    def unexpected_sort(*args, **kwargs):
        raise AssertionError("sorted history should not be copied")

    monkeypatch.setattr(consumption_module, "sorted", unexpected_sort, raising=False)

    result = calculate_consumption(
        (_sample(0, 10), _sample(30, 20)),
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=30),
    )

    assert result.consumed_percentage_points == 10.0
