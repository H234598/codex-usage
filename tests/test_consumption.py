from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest

import codex_usage.consumption as consumption_module
from codex_usage.consumption import (
    ConsumptionWindow,
    _confirmed_reset,
    _reset_boundary_at,
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


def test_consumption_reset_boundary_prefers_provider_reset_time():
    previous_reset = BASE + timedelta(minutes=30)
    previous = _sample(0, 90, generation="old", reset_at=previous_reset)
    current = _sample(60, 5, generation="new", reset_at=BASE + timedelta(hours=2))

    assert _reset_boundary_at(previous, current) == previous_reset
    assert _reset_boundary_at(_sample(0, 70), _sample(30, 0)) == BASE + timedelta(minutes=30)
    assert _reset_boundary_at(
        _sample(0, 70, generation="old"),
        _sample(30, 5, generation="new"),
    ) == BASE + timedelta(minutes=30)


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


def test_consumption_accepts_explicit_none_smoothing_name():
    result = calculate_consumption(
        [_sample(0, 10), _sample(30, 20)],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=30),
        smoothing="none",
    )

    assert result.consumed_percentage_points == 10.0


def test_consumption_rejects_non_usage_sample_entry():
    with pytest.raises(ValueError, match="samples are invalid"):
        calculate_consumption(
            [object()],
            amount=1,
            unit="hours",
            now=BASE,
        )


def test_consumption_rejects_runtime_error_sample_iterator():
    def broken_samples():
        yield _sample(0, 10)
        raise RuntimeError("synthetic consumption iterator marker")

    with pytest.raises(ValueError, match="samples are invalid"):
        calculate_consumption(
            broken_samples(),
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


def test_consumption_omits_forecast_beyond_maximum():
    result = calculate_consumption(
        [_sample(0, 0.0), _sample(1, 1e-6)],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=1),
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
    assert result.consumed_percentage_points == 5.0
    assert result.coverage == "complete"


def test_consumption_resets_counter_at_planned_reset():
    reset = BASE + timedelta(minutes=45)
    result = calculate_consumption(
        [
            _sample(0, 50, generation="old", reset_at=reset),
            _sample(30, 70, generation="old", reset_at=reset),
            _sample(60, 5, generation="new", reset_at=BASE + timedelta(hours=2)),
        ],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=60),
    )

    assert result.consumed_percentage_points == 5.0
    assert result.coverage == "complete"


def test_consumption_resets_counter_when_limit_returns_to_full_without_metadata():
    result = calculate_consumption(
        [_sample(-30, 40), _sample(0, 50), _sample(30, 70), _sample(60, 0), _sample(90, 8)],
        amount=2,
        unit="hours",
        now=BASE + timedelta(minutes=90),
    )

    assert result.consumed_percentage_points == 8.0
    assert result.coverage == "complete"


def test_consumption_resets_counter_before_duplicate_timestamp_gap():
    result = calculate_consumption(
        [
            _sample(0, 10),
            _sample(30, 20),
            _sample(30, 0, generation="b"),
            _sample(60, 5, generation="b"),
        ],
        amount=2,
        unit="hours",
        now=BASE + timedelta(minutes=60),
    )

    assert result.consumed_percentage_points == 5.0


def test_consumption_smoothing_resets_ema_before_duplicate_timestamp_gap():
    result = calculate_consumption(
        [
            _sample(0, 10),
            _sample(30, 20),
            _sample(30, 0, generation="b"),
            _sample(60, 5, generation="b"),
        ],
        amount=2,
        unit="hours",
        now=BASE + timedelta(minutes=60),
        smoothing="ema-5",
    )

    assert result.estimated_seconds_to_exhaustion == 34_200


def test_consumption_resets_credit_counter_at_planned_reset():
    reset = BASE + timedelta(minutes=45)
    samples = [
        UsageSample(
            account_id="alpha",
            pool="credits",
            window_seconds=2_592_000,
            captured_at=BASE,
            used_percent=50,
            reset_generation="old",
            reset_at=reset,
            source="test",
        ),
        UsageSample(
            account_id="alpha",
            pool="credits",
            window_seconds=2_592_000,
            captured_at=BASE + timedelta(minutes=30),
            used_percent=80,
            reset_generation="old",
            reset_at=reset,
            source="test",
        ),
        UsageSample(
            account_id="alpha",
            pool="credits",
            window_seconds=2_592_000,
            captured_at=BASE + timedelta(minutes=60),
            used_percent=4,
            reset_generation="new",
            reset_at=BASE + timedelta(days=31),
            source="test",
        ),
    ]

    result = calculate_consumption(
        samples,
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=60),
    )

    assert result.pool == "credits"
    assert result.limit_window_seconds == 2_592_000
    assert result.consumed_percentage_points == 4.0
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
    assert smoothed.estimated_seconds_to_exhaustion == 95


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


class _RaisingTzinfoProperty(datetime):
    @property
    def tzinfo(self):
        raise RuntimeError("synthetic consumption tzinfo marker")


class _BrokenUnit(str):
    def __hash__(self):
        raise RuntimeError("synthetic consumption unit hash marker")


class _BrokenSmoothing(str):
    def startswith(self, _prefix):
        raise RuntimeError("synthetic consumption smoothing marker")


class _BrokenComparison(datetime):
    def __lt__(self, _other):
        raise RuntimeError("synthetic consumption comparison marker")


class _BrokenOrdering(datetime):
    def __le__(self, _other):
        raise RuntimeError("synthetic consumption ordering marker")


class _BrokenReset(datetime):
    def __gt__(self, _other):
        raise RuntimeError("synthetic consumption reset marker")


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


def test_consumption_rejects_datetime_tzinfo_property_hooks():
    with pytest.raises(ValueError, match="timezone-aware"):
        calculate_consumption(
            [],
            amount=1,
            unit="hours",
            now=_RaisingTzinfoProperty(2026, 8, 16, 10, tzinfo=UTC),
        )


def test_consumption_rejects_string_subclass_unit_and_smoothing():
    with pytest.raises(ValueError, match="unit"):
        consumption_lookback_seconds(1, _BrokenUnit("hours"))
    with pytest.raises(ValueError, match="smoothing"):
        calculate_consumption(
            [],
            amount=1,
            unit="hours",
            now=BASE,
            smoothing=_BrokenSmoothing("ema-5"),
        )


def test_consumption_rejects_sample_datetime_comparison_hooks():
    first = UsageSample(
        account_id="alpha",
        pool="main",
        window_seconds=18_000,
        captured_at=BASE,
        used_percent=10,
        source="test",
    )
    second = UsageSample(
        account_id="alpha",
        pool="main",
        window_seconds=18_000,
        captured_at=_BrokenComparison(2026, 8, 16, 10, 30, tzinfo=UTC),
        used_percent=20,
        source="test",
    )

    with pytest.raises(ValueError, match="samples are invalid"):
        calculate_consumption(
            [first, second],
            amount=1,
            unit="hours",
            now=BASE + timedelta(minutes=30),
        )


def test_consumption_rejects_sample_datetime_ordering_hooks():
    sample = UsageSample(
        account_id="alpha",
        pool="main",
        window_seconds=18_000,
        captured_at=_BrokenOrdering(2026, 8, 16, 10, tzinfo=UTC),
        used_percent=10,
        source="test",
    )

    with pytest.raises(ValueError, match="samples are invalid"):
        calculate_consumption(
            [sample],
            amount=1,
            unit="hours",
            now=BASE,
        )


def test_consumption_reset_detection_fails_closed_for_datetime_hooks():
    previous = _sample(0, 80, reset_at=_BrokenReset(2026, 8, 16, 10, 30, tzinfo=UTC))
    current = _sample(60, 10, reset_at=BASE + timedelta(hours=2))

    assert _confirmed_reset(previous, current) is False


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


class _BrokenSampleField(UsageSample):
    ready = False
    broken_field = ""

    def __getattribute__(self, name):
        if (
            name == object.__getattribute__(self, "broken_field")
            and object.__getattribute__(self, "ready")
        ):
            raise RuntimeError(f"synthetic {name} getter marker")
        return super().__getattribute__(name)


def _broken_sample(field):
    sample = _BrokenSampleField(
        account_id="alpha",
        pool="main",
        window_seconds=18_000,
        captured_at=BASE,
        used_percent=10,
        source="test",
    )
    object.__setattr__(sample, "broken_field", field)
    object.__setattr__(sample, "ready", True)
    return sample


@pytest.mark.parametrize(
    "field",
    ["account_id", "pool", "window_seconds", "used_percent", "captured_at"],
)
def test_consumption_rejects_sample_field_getter_hooks(field):
    with pytest.raises(ValueError, match="samples are invalid"):
        calculate_consumption(
            [_broken_sample(field)],
            amount=1,
            unit="hours",
            now=BASE,
        )


class _BrokenEqualityText(str):
    def __eq__(self, _other):
        raise RuntimeError("synthetic sample equality marker")

    def __ne__(self, _other):
        raise RuntimeError("synthetic sample equality marker")


class _BrokenEqualityInt(int):
    def __eq__(self, _other):
        raise RuntimeError("synthetic sample equality marker")

    def __ne__(self, _other):
        raise RuntimeError("synthetic sample equality marker")


def test_consumption_rejects_account_equality_hook():
    sample = _sample(0, 10)
    object.__setattr__(sample, "account_id", _BrokenEqualityText("alpha"))

    with pytest.raises(ValueError, match="samples are invalid"):
        calculate_consumption(
            [sample, _sample(30, 20)],
            amount=1,
            unit="hours",
            now=BASE + timedelta(minutes=30),
        )


def test_consumption_rejects_pool_equality_hook():
    sample = _sample(0, 10)
    object.__setattr__(sample, "pool", _BrokenEqualityText("main"))

    with pytest.raises(ValueError, match="samples are invalid"):
        calculate_consumption(
            [sample, _sample(30, 20)],
            amount=1,
            unit="hours",
            now=BASE + timedelta(minutes=30),
        )


def test_consumption_rejects_window_equality_hook():
    sample = _sample(0, 10)
    object.__setattr__(sample, "window_seconds", _BrokenEqualityInt(18_000))

    with pytest.raises(ValueError, match="samples are invalid"):
        calculate_consumption(
            [sample, _sample(30, 20)],
            amount=1,
            unit="hours",
            now=BASE + timedelta(minutes=30),
        )


def test_consumption_rejects_used_percent_float_hook():
    class BrokenFloat(float):
        def __float__(self):
            raise RuntimeError("synthetic used percent float marker")

    sample = _sample(0, 10)
    object.__setattr__(sample, "used_percent", BrokenFloat(10))

    with pytest.raises(ValueError, match="samples are invalid"):
        calculate_consumption(
            [sample, _sample(30, 20)],
            amount=1,
            unit="hours",
            now=BASE + timedelta(minutes=30),
        )


def test_consumption_rejects_stale_timestamp_arithmetic_hook():
    class BrokenTimestamp(datetime):
        def __sub__(self, _other):
            raise RuntimeError("synthetic stale timestamp marker")

    sample = _sample(30, 20)
    object.__setattr__(sample, "captured_at", BrokenTimestamp(2026, 8, 16, 10, 30, tzinfo=UTC))

    with pytest.raises(ValueError, match="samples are invalid"):
        calculate_consumption(
            [_sample(0, 10), sample],
            amount=1,
            unit="hours",
            now=BASE + timedelta(minutes=30),
        )


def test_consumption_rejects_invalid_mutated_used_percent():
    sample = _sample(0, 10)
    object.__setattr__(sample, "used_percent", float("nan"))

    with pytest.raises(ValueError, match="samples are invalid"):
        calculate_consumption(
            [sample],
            amount=1,
            unit="hours",
            now=BASE,
        )


def test_consumption_rejects_sort_key_hook():
    class BrokenSortDatetime(datetime):
        calls = 0

        def __lt__(self, _other):
            type(self).calls += 1
            if type(self).calls == 1:
                return True
            raise RuntimeError("synthetic sort key marker")

    first = _sample(0, 10)
    second = _sample(30, 20)
    object.__setattr__(second, "captured_at", BrokenSortDatetime(2026, 8, 16, 9, 30, tzinfo=UTC))

    with pytest.raises(ValueError, match="samples are invalid"):
        calculate_consumption(
            [first, second],
            amount=1,
            unit="hours",
            now=BASE + timedelta(minutes=30),
        )


def test_consumption_rejects_estimate_timestamp_hook():
    class BrokenLaterDatetime(datetime):
        calls = 0

        def __sub__(self, other):
            type(self).calls += 1
            if type(self).calls > 1:
                raise RuntimeError("synthetic estimate timestamp marker")
            return super().__sub__(other)

    first = _sample(0, 10)
    second = _sample(30, 20)
    object.__setattr__(second, "captured_at", BrokenLaterDatetime(2026, 8, 16, 10, 30, tzinfo=UTC))

    with pytest.raises(ValueError, match="samples are invalid"):
        calculate_consumption(
            [first, second],
            amount=1,
            unit="hours",
            now=BASE + timedelta(minutes=30),
        )


def test_consumption_aware_helper_rejects_non_datetime():
    assert consumption_module._is_aware(None) is False


@pytest.mark.parametrize(
    "field",
    [
        "lookback_seconds",
        "pool",
        "limit_window_seconds",
        "consumed_percentage_points",
        "coverage",
        "sample_count",
        "estimated_seconds_to_exhaustion",
        "baseline_used_percent",
    ],
)
def test_consumption_window_as_dict_fails_closed_for_property_hooks(field):
    class BrokenWindow(ConsumptionWindow):
        ready = False

        def __getattribute__(self, name):
            if object.__getattribute__(self, "ready") and name == field:
                raise RuntimeError(f"synthetic consumption window {field} hook")
            return super().__getattribute__(name)

    window = BrokenWindow(
        lookback_seconds=3_600,
        pool="main",
        limit_window_seconds=18_000,
        consumed_percentage_points=12.5,
        coverage="partial",
        sample_count=2,
        estimated_seconds_to_exhaustion=4_200,
        baseline_used_percent=10.0,
    )
    object.__setattr__(window, "ready", True)

    assert window.as_dict() == {}


def test_consumption_ema_fails_closed_for_timestamp_property_hook():
    class BrokenTimestamp(datetime):
        def __sub__(self, _other):
            raise RuntimeError("synthetic ema timestamp hook")

    previous = _sample(0, 10)
    current = _sample(30, 20)
    object.__setattr__(current, "captured_at", BrokenTimestamp(2026, 8, 16, 10, 30, tzinfo=UTC))

    assert consumption_module._ema_rate(
        [previous, current],
        time_constant_seconds=300,
        max_gap_seconds=3_600,
    ) == 0.0


def test_consumption_ema_fails_closed_for_used_percent_property_hook():
    class BrokenFloat(float):
        def __float__(self):
            raise RuntimeError("synthetic ema used percent hook")

    previous = _sample(0, 10)
    current = _sample(30, 20)
    object.__setattr__(current, "used_percent", BrokenFloat(20))

    assert consumption_module._ema_rate(
        [previous, current],
        time_constant_seconds=300,
        max_gap_seconds=3_600,
    ) == 0.0
