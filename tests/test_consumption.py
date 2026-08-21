from datetime import UTC, datetime, timedelta, timezone

import pytest

import codex_usage.consumption as consumption_module
from codex_usage.consumption import calculate_consumption
from codex_usage.history import UsageSample

BASE = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)


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


def test_consumption_forecast_is_available_as_approximation_with_fresh_partial_data():
    result = calculate_consumption(
        [_sample(0, 20), _sample(30, 50)],
        amount=1,
        unit="hours",
        now=BASE + timedelta(minutes=30),
    )

    assert result.coverage == "partial"
    assert result.estimated_seconds_to_exhaustion == 3_000


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
        calculate_consumption([], amount=1, unit="weeks", now=BASE)


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
