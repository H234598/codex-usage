import json
from datetime import UTC, datetime

import pytest

from codex_usage.models import AccountStatus, AccountUsage, LimitWindow, UsagePool


class _BrokenDuration(int):
    def __le__(self, _other):
        raise RuntimeError("synthetic duration comparison marker")

    def __mod__(self, _other):
        raise RuntimeError("synthetic duration marker")


class _BrokenFloat(float):
    def __float__(self):
        raise RuntimeError("synthetic number marker")


class _TrustedMalformedWindow(LimitWindow):
    @property
    def has_known_identity(self):
        return True


class _TrustedUsageWindow(LimitWindow):
    @property
    def has_invalid_usage_value(self):
        return False


@pytest.mark.parametrize("duration", [True, 1.0, "1", 0, -1, None])
def test_window_for_duration_rejects_invalid_duration_types(duration):
    pool = UsagePool(
        key="custom",
        display_name="Custom",
        windows=(LimitWindow(name="1s", duration_seconds=1, remaining=1),),
    )

    assert pool.window_for_duration(duration) is None


def test_window_for_duration_rejects_integer_subclass_before_comparison():
    pool = UsagePool(
        key="custom",
        display_name="Custom",
        windows=(LimitWindow(name="1s", duration_seconds=1, remaining=1),),
    )

    assert pool.window_for_duration(_BrokenDuration(1)) is None


def test_window_for_duration_resolves_duration_and_named_identities():
    by_duration = LimitWindow(
        name="1s", duration_seconds=1, remaining=1
    )
    by_name = LimitWindow(name="weekly", remaining=1)
    pool = UsagePool(key="custom", display_name="Custom", windows=(by_duration, by_name))

    assert pool.window_for_duration(1) is by_duration
    assert pool.window_for_duration(604_800) is by_name


def test_usage_pool_identity_validation_fails_closed_for_unhashable_duration():
    pool = UsagePool(
        key="custom",
        display_name="Custom",
        windows=(
            _TrustedMalformedWindow(name="weekly", duration_seconds=[], remaining=97),
        ),
    )

    assert pool.has_valid_usage is False


def test_account_usage_model_pool_requires_one_exact_key_match():
    pool = UsagePool(key="spark", display_name="Spark")
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        models=(pool,),
    )

    assert usage.model_pool("spark") is pool
    assert usage.model_pool("SPARK") is None
    assert usage.model_pool(" spark ") is None


def test_account_usage_model_pool_fails_closed_for_malformed_or_ambiguous_catalog():
    malformed = UsagePool(key=[], display_name="Malformed")  # type: ignore[arg-type]
    ambiguous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        models=(
            UsagePool(key="spark", display_name="Spark"),
            UsagePool(key="SPARK", display_name="Spark duplicate"),
        ),
    )
    invalid = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        models=(malformed, UsagePool(key="valid", display_name="Valid")),
    )

    assert ambiguous.model_pool("spark") is None
    assert invalid.model_pool("valid") is None


@pytest.mark.parametrize("model", [None, [], {}, True, ""])
def test_account_usage_model_pool_rejects_invalid_model_input(model):
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
    )

    assert usage.model_pool(model) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (LimitWindow(name="weekly", remaining=97), True),
        (LimitWindow(name="", duration_seconds=604800, remaining=97), True),
        (LimitWindow(name="weekly", duration_seconds=604800), True),
        (LimitWindow(name="weekly", duration_seconds=18000), False),
        (LimitWindow(name="unknown", remaining=97), False),
        (LimitWindow(name="weekly", duration_seconds=True, remaining=97), False),
        (LimitWindow(name=42), False),
    ],
)
def test_limit_window_known_identity_is_strict(window, expected):
    assert window.has_known_identity is expected


@pytest.mark.parametrize(
    ("duration", "name"), [(86_400, "1d"), (3_600, "1h"), (61, "61s")]
)
def test_limit_window_known_identity_accepts_canonical_dynamic_names(duration, name):
    assert LimitWindow(name=name, duration_seconds=duration).has_known_identity is True


def test_limit_window_known_identity_rejects_integer_subclass_duration():
    window = LimitWindow(name="custom", duration_seconds=_BrokenDuration(61))

    assert window.has_known_identity is False


def test_limit_window_is_complete_requires_usage_limit_and_reset():
    complete = LimitWindow(
        name="weekly",
        used=3,
        limit=100,
        reset_at=datetime.now(UTC),
    )

    assert complete.is_complete is True
    assert LimitWindow(name="weekly", used=3, limit=100).is_complete is False


@pytest.mark.parametrize(
    "window",
    [
        LimitWindow(name="weekly", used=-1, limit=100),
        LimitWindow(name="weekly", remaining=-1),
        LimitWindow(name="weekly", percent=101),
        LimitWindow(name="weekly", remaining=120, limit=100),
        LimitWindow(name="weekly", limit=0),
        LimitWindow(name="weekly", remaining=120),
    ],
)
def test_limit_window_invalid_usage_values_fail_closed(window):
    assert window.has_invalid_usage_value is True
    assert window.has_usage_value is False


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (LimitWindow(name="weekly", used=25, limit=100), 75.0),
        (LimitWindow(name="weekly", remaining=50, limit=100), 50.0),
        (LimitWindow(name="weekly", remaining=50, percent=50), 50.0),
        (LimitWindow(name="weekly", remaining=50, percent=60), None),
        (LimitWindow(name="weekly", percent=75), 75.0),
    ],
)
def test_limit_window_remaining_percent_uses_valid_sources(window, expected):
    assert window.remaining_percent == expected


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (_TrustedUsageWindow(name="weekly", used=float("nan")), None),
        (_TrustedUsageWindow(name="weekly", used=10, limit=0), None),
        (_TrustedUsageWindow(name="weekly", used=100, limit=100), 0.0),
        (_TrustedUsageWindow(name="weekly", remaining=120, limit=100), None),
        (_TrustedUsageWindow(name="weekly"), None),
    ],
)
def test_limit_window_remaining_percent_defensive_guards(window, expected):
    assert window.remaining_percent == expected


def test_limit_window_numeric_subclasses_fail_closed_and_stay_json_safe():
    window = LimitWindow(
        name="weekly",
        duration_seconds=_BrokenDuration(604_800),
        used=_BrokenFloat(10),
        limit=100,
    )
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        five_hour=window,
    )

    assert window.has_invalid_usage_value is True
    assert window.remaining_percent is None
    payload = usage.as_dict()
    json.dumps(payload, allow_nan=False)
    assert payload["five_hour"]["duration_seconds"] is None
    assert payload["five_hour"]["used"] is None


def test_account_usage_as_dict_skips_unhashable_model_pool_keys():
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        status=AccountStatus.OK,
        models=(
            UsagePool(key=[], display_name="malformed"),
            UsagePool(key="valid", display_name="Valid"),
        ),
    )

    payload = usage.as_dict()

    assert tuple(payload["models"]) == ("valid",)


def test_account_usage_as_dict_skips_ambiguous_model_pool_keys():
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        status=AccountStatus.OK,
        models=(
            UsagePool(key="spark", display_name="Spark"),
            UsagePool(key="SPARK", display_name="Duplicate Spark"),
            UsagePool(key="valid", display_name="Valid"),
        ),
    )

    payload = usage.as_dict()

    assert tuple(payload["models"]) == ("valid",)


@pytest.mark.parametrize("status", [[], {}, "ok"])
def test_account_usage_as_dict_normalizes_invalid_status(status):
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        status=status,
    )

    payload = usage.as_dict()

    assert payload["status"] == "error"
    assert payload["stale"] is True
    assert payload["cache_invalidated"] is True


def test_account_usage_as_dict_handles_invalid_optional_containers():
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        source_urls=None,
        usage_resets=None,
    )

    payload = usage.as_dict()

    assert payload["source_urls"] == []
    assert payload["usage_resets"] == {
        "available": None,
        "known": False,
        "redeem_capability": False,
    }


def test_account_usage_as_dict_keeps_malformed_window_values_json_safe():
    malformed = datetime.now(UTC)
    window = LimitWindow(
        name=malformed,  # type: ignore[arg-type]
        used=malformed,  # type: ignore[arg-type]
        reset_at=[],  # type: ignore[arg-type]
    )
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=[],  # type: ignore[arg-type]
        five_hour=window,
        blocked_until={},  # type: ignore[arg-type]
        auth_last_refresh=[],  # type: ignore[arg-type]
        auth_access_expires_at={},  # type: ignore[arg-type]
        auth_id_expires_at="invalid",  # type: ignore[arg-type]
        values_captured_at=[],  # type: ignore[arg-type]
    )

    payload = usage.as_dict()

    json.dumps(payload, allow_nan=False)
    assert payload["captured_at"] is None
    assert payload["five_hour"]["name"] is None
    assert payload["five_hour"]["used"] is None
    assert payload["five_hour"]["reset_at"] is None


def test_account_usage_as_dict_ignores_datetime_subclass_serialization_failure():
    class BrokenDatetime(datetime):
        def isoformat(self, *args, **kwargs):
            raise RuntimeError("synthetic isoformat marker")

    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=BrokenDatetime(2026, 8, 16, 10, tzinfo=UTC),
    )

    payload = usage.as_dict()

    assert payload["captured_at"] is None


@pytest.mark.parametrize(
    ("five_hour", "weekly", "expected_windows"),
    [
        ([], None, ()),
        (["malformed"], LimitWindow(name="weekly", remaining=80), ("weekly",)),
    ],
)
def test_account_usage_skips_malformed_legacy_windows(
    five_hour, weekly, expected_windows
):
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        five_hour=five_hour,  # type: ignore[arg-type]
        weekly=weekly,
    )

    if expected_windows:
        assert usage.main is not None
        assert tuple(window.name for window in usage.main.windows) == expected_windows
    else:
        assert usage.main is None
