from __future__ import annotations

from datetime import datetime, tzinfo

import pytest

import codex_usage.usage_limits as usage_limits_module
from codex_usage.extractor import LOCAL_TZ
from codex_usage.models import AccountUsage, LimitWindow, UsagePool
from codex_usage.usage_limits import (
    SPARK_METERED_FEATURE,
    SPARK_MODEL,
    _window_identities_are_unique,
    legacy_windows,
    merge_model_catalog,
    parse_app_server_usage_pools,
    parse_wham_usage_pools,
)

NOW = datetime(2026, 7, 16, 4, 0, tzinfo=LOCAL_TZ)


class _RaisingTimezone(tzinfo):
    def utcoffset(self, _value):
        raise RuntimeError("synthetic timezone marker")


class _BrokenInt(int):
    def __mul__(self, _other):
        return "not-an-int"

    def __mod__(self, _other):
        return "not-an-int"

    def __float__(self):
        raise AssertionError("numeric subclass conversion must not run")


@pytest.mark.parametrize("payload", [None, [], "invalid", 42, True])
def test_usage_pool_parsers_fail_closed_for_non_object_payload(payload):
    assert parse_wham_usage_pools(
        payload,
        captured_at=NOW,
        source="test",
    ) == (None, ())
    assert parse_app_server_usage_pools(
        payload,
        captured_at=NOW,
    ) == (None, ())


@pytest.mark.parametrize("captured_at", [None, [], "invalid", 42, True, object()])
def test_usage_pool_parsers_fail_closed_for_invalid_capture_time(captured_at):
    wham_payload = {
        "rate_limit": {
            "primary_window": {"reset_after_seconds": 60},
        }
    }
    app_server_payload = {
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {"resetsAt": 1},
            }
        }
    }

    assert parse_wham_usage_pools(
        wham_payload,
        captured_at=captured_at,
        source="test",
    ) == (None, ())  # type: ignore[arg-type]
    assert parse_app_server_usage_pools(
        app_server_payload,
        captured_at=captured_at,
    ) == (None, ())  # type: ignore[arg-type]


def test_legacy_windows_returns_empty_slots_without_main_pool():
    assert legacy_windows(None) == (None, None)


def test_usage_limit_private_helpers_cover_window_and_identity_contracts():
    wham_pool = usage_limits_module._wham_pool(
        key="main",
        display_name="Codex",
        rate_limit={
            "primary_window": {
                "limit_window_seconds": 18_000,
                "used_percent": 1,
            }
        },
        metered_feature=None,
        captured_at=NOW,
        source="test",
    )
    app_server_pool = usage_limits_module._app_server_pool(
        key="main",
        display_name="Codex",
        snapshot={"primary": {"usedPercent": 1, "windowDurationMins": 300}},
        metered_feature=None,
        captured_at=NOW,
        source="test",
    )
    assert wham_pool is not None and wham_pool.available is True
    assert app_server_pool is not None and app_server_pool.available is True

    wham_window = usage_limits_module._wham_window(
        {"limit_window_seconds": 18_000, "used_percent": 1},
        captured_at=NOW,
        source="test",
    )
    app_server_window = usage_limits_module._app_server_window(
        {"usedPercent": 1, "windowDurationMins": 300},
        captured_at=NOW,
        source="test",
    )
    assert wham_window is not None and wham_window.name == "5h"
    assert app_server_window is not None and app_server_window.name == "5h"
    assert usage_limits_module._wham_window(
        {"limit_window_seconds": 0}, captured_at=NOW, source="test"
    ) is None
    assert usage_limits_module._app_server_window(
        {"windowDurationMins": 0}, captured_at=NOW, source="test"
    ) is None

    assert usage_limits_module._window(18_000, 1, None, source="test").remaining == 99
    assert usage_limits_module._window_name(None) == "unknown"
    assert usage_limits_module._window_name(86_400) == "1d"
    assert usage_limits_module._window_name(3_600) == "1h"
    assert usage_limits_module._window_name(61) == "61s"
    assert usage_limits_module._reset_at(None, 60, captured_at=NOW) is not None
    assert usage_limits_module._reset_at(None, -1, captured_at=NOW) is None

    assert usage_limits_module._is_spark_limit(SPARK_MODEL, None) is True
    assert usage_limits_module._is_spark_limit(None, SPARK_METERED_FEATURE) is True
    assert usage_limits_module._is_spark_limit(" GPT-5.3-Codex-Spark", None) is False
    assert usage_limits_module._normalized("Spark") == "spark"
    assert usage_limits_module._normalized("bad value") == ""
    assert usage_limits_module._positive_int(18_000) == 18_000
    assert usage_limits_module._positive_int(0) is None
    assert usage_limits_module._nonnegative_int(0) == 0
    assert usage_limits_module._nonnegative_int(-1) is None
    assert usage_limits_module._strict_int(1) == 1
    assert usage_limits_module._strict_int(True) is None
    assert usage_limits_module._optional_bool(False) is False
    assert usage_limits_module._optional_bool(0) is None
    assert usage_limits_module._unique(("a", "a", "b")) == ("a", "b")


def test_percent_rejects_numeric_subclass_before_float_conversion():
    assert usage_limits_module._percent(_BrokenInt(50)) is None


def test_wham_parser_drops_overflowing_relative_reset_time():
    main, _ = parse_wham_usage_pools(
        {
            "rate_limit": {
                "primary_window": {
                    "limit_window_seconds": 18_000,
                    "used_percent": 1,
                    "reset_after_seconds": 60,
                }
            }
        },
        captured_at=datetime.max.replace(tzinfo=NOW.tzinfo),
        source="test",
    )

    assert main is not None
    assert main.windows[0].reset_at is None


def test_wham_parser_drops_relative_reset_with_failing_timezone_callback():
    main, _ = parse_wham_usage_pools(
        {
            "rate_limit": {
                "primary_window": {
                    "limit_window_seconds": 18_000,
                    "used_percent": 1,
                    "reset_after_seconds": 60,
                }
            }
        },
        captured_at=datetime(2026, 8, 16, 10, 0, tzinfo=_RaisingTimezone()),
        source="test",
    )

    assert main is not None
    assert main.windows[0].reset_at is None


def test_app_server_parser_rejects_integer_subclass_window_duration():
    main, models = parse_app_server_usage_pools(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "windowDurationMins": _BrokenInt(300),
                        "usedPercent": 1,
                    }
                }
            }
        },
        captured_at=NOW,
    )

    assert main is None
    assert models == ()


@pytest.mark.parametrize("pools", [None, 1, True, object()])
def test_merge_model_catalog_fails_closed_for_non_iterable_pools(pools):
    assert merge_model_catalog(pools, ()) == ()  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (LimitWindow(name="5h", remaining=690, limit=1000), 69.0),
        (LimitWindow(name="5h", remaining=float("nan"), limit=100), None),
        (LimitWindow(name="5h", remaining=float("inf"), limit=100), None),
        (LimitWindow(name="5h", remaining=50, limit=float("inf")), None),
        (LimitWindow(name="5h", remaining=120, limit=100), None),
        (LimitWindow(name="5h", percent=float("nan")), None),
        (LimitWindow(name="5h", percent=101), None),
    ],
)
def test_remaining_percent_fails_closed_for_invalid_values(window, expected):
    assert window.remaining_percent == expected


def test_remaining_percent_prefers_absolute_usage_over_percent():
    window = LimitWindow(
        name="5h",
        used=100,
        limit=100,
        remaining=100,
        percent=100,
    )

    assert window.remaining_percent == 0


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (LimitWindow(name="5h", remaining=97), True),
        (LimitWindow(name="5h", percent=97), True),
        (LimitWindow(name="5h", remaining=690), False),
        (LimitWindow(name="5h", remaining=690, percent=97), True),
        (LimitWindow(name="5h", remaining=55, percent=69), False),
        (LimitWindow(name="5h", used=-1, limit=100, remaining=80), False),
        (LimitWindow(name="5h", percent=float("nan")), False),
    ],
)
def test_has_usage_value_requires_verified_remaining_percentage(window, expected):
    assert window.has_usage_value is expected


def test_invalid_remaining_cannot_override_percent():
    window = LimitWindow(name="5h", remaining=-1, percent=97)

    assert window.has_invalid_usage_value is True
    assert window.remaining_percent is None


def test_usage_pool_exhaustion_prefers_absolute_usage_over_percent():
    pool = UsagePool(
        key="main",
        display_name="Codex",
        windows=(
            LimitWindow(
                name="5h",
                used=100,
                limit=100,
                remaining=100,
                percent=100,
            ),
        ),
    )

    assert pool.exhausted is True


def test_usage_pool_treats_invalid_window_as_unusable():
    pool = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(LimitWindow(name="weekly", remaining=-1, percent=97),),
    )

    assert pool.exhausted is True


def test_usage_pool_exhaustion_fails_closed_for_usage_reset_only_window():
    pool = UsagePool(
        key="main",
        display_name="Codex",
        windows=(LimitWindow(name="weekly", reset_at=NOW),),
        availability_sources=("usage",),
    )

    assert pool.has_valid_usage is False
    assert pool.exhausted is True


def test_usage_pool_exhaustion_fails_closed_for_usage_window_missing_verified_usage():
    pool = UsagePool(
        key="main",
        display_name="Codex",
        windows=(LimitWindow(name="weekly"),),
        availability_sources=("usage",),
    )

    assert pool.has_valid_usage is False
    assert pool.exhausted is True


@pytest.mark.parametrize("field", ["available", "allowed", "limit_reached"])
def test_usage_pool_treats_invalid_control_flag_as_unusable(field):
    values = {field: "false"}
    pool = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(LimitWindow(name="weekly", remaining=97, percent=97),),
        **values,
    )

    assert pool.exhausted is True


@pytest.mark.parametrize(
    ("available", "allowed", "limit_reached", "expected"),
    [
        (True, None, None, True),
        (True, False, None, True),
        (True, None, True, True),
        (False, None, None, False),
        (True, "false", None, False),
        (True, None, "false", False),
    ],
)
def test_usage_pool_validity_requires_strict_controls(
    available, allowed, limit_reached, expected
):
    pool = UsagePool(
        key="main",
        display_name="Codex",
        windows=(LimitWindow(name="weekly", remaining=97, percent=97),),
        available=available,
        allowed=allowed,
        limit_reached=limit_reached,
    )

    assert pool.has_valid_usage is expected


@pytest.mark.parametrize("name", ["", "unknown", "Limit", "garbage"])
def test_usage_pool_validity_requires_known_window_identity(name):
    pool = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(LimitWindow(name=name, remaining=97),),
    )

    assert pool.has_valid_usage is False


def test_usage_pool_duration_proves_window_identity_without_name():
    pool = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(LimitWindow(name="", remaining=97, duration_seconds=604800),),
    )

    assert pool.has_valid_usage is True


def test_usage_pool_rejects_unknown_window_identity_as_unavailable():
    pool = UsagePool(
        key="main",
        display_name="Codex",
        windows=(LimitWindow(name="unknown", remaining=97),),
        availability_sources=("usage",),
    )

    assert pool.has_valid_usage is False
    assert pool.exhausted is True


def test_usage_pool_rejects_name_and_duration_alias_collision():
    pool = UsagePool(
        key="main",
        display_name="Codex",
        windows=(
            LimitWindow(name="weekly", remaining=97),
            LimitWindow(name="", duration_seconds=604800, remaining=90),
        ),
        availability_sources=("usage",),
    )

    assert _window_identities_are_unique(pool.windows) is False
    assert pool.has_valid_usage is False


def test_wham_marks_missing_window_duration_unavailable():
    main, _ = parse_wham_usage_pools(
        {
            "rate_limit": {
                "primary_window": {"used_percent": 10},
            }
        },
        captured_at=NOW,
        source="wham",
    )

    assert main is not None
    assert main.available is False
    assert main.has_valid_usage is False


@pytest.mark.parametrize(
    ("name", "duration"),
    [("5h", 604800), ("garbage", 604800), ("weekly", 18000)],
)
def test_usage_pool_rejects_conflicting_window_identity(name, duration):
    pool = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(LimitWindow(name=name, remaining=97, duration_seconds=duration),),
    )

    assert pool.has_valid_usage is False


def test_usage_pool_rejects_duplicate_window_identity_aliases():
    pool = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(
            LimitWindow(name="weekly", remaining=97),
            LimitWindow(name="w", remaining=90),
        ),
    )

    assert pool.has_valid_usage is False
    assert pool.exhausted is True
    assert pool.window_for_duration(604800) is None


def test_usage_pool_lookup_rejects_malformed_window_container():
    pool = UsagePool(
        key="main",
        display_name="Codex",
        windows=[
            LimitWindow(name="weekly", remaining=90, duration_seconds=604800),
        ],
    )

    assert pool.has_valid_usage is False
    assert pool.window_for_duration(604800) is None


def test_usage_pool_accepts_canonical_dynamic_window_identity():
    pool = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(LimitWindow(name="10d", remaining=97, duration_seconds=864000),),
    )

    assert pool.has_valid_usage is True


def test_empty_catalog_pool_is_unknown_not_exhausted():
    pool = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        available=True,
        availability_sources=("model_catalog",),
    )

    assert pool.has_valid_usage is False
    assert pool.exhausted is False


def test_catalog_only_pool_without_verified_usage_remains_unknown_not_exhausted():
    pool = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(LimitWindow(name="weekly", reset_at=NOW),),
        available=True,
        availability_sources=("model_catalog",),
    )

    assert pool.has_valid_usage is False
    assert pool.exhausted is False


def test_wham_keeps_main_and_spark_weekly_limits_separate():
    main, models = parse_wham_usage_pools(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 20,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 600,
                }
            },
            "additional_rate_limits": [
                {
                    "limit_name": "GPT-5.3-Codex-Spark",
                    "metered_feature": "codex_bengalfox",
                    "rate_limit": {
                        "allowed": True,
                        "limit_reached": False,
                        "primary_window": {
                            "used_percent": 1,
                            "limit_window_seconds": 604800,
                            "reset_after_seconds": 900,
                        },
                    },
                }
            ],
        },
        captured_at=NOW,
        source="wham",
    )

    assert main is not None
    assert main.windows[0].name == "weekly"
    assert main.windows[0].remaining == 80
    assert legacy_windows(main) == (None, main.windows[0])
    assert len(models) == 1
    assert models[0].key == SPARK_MODEL
    assert models[0].windows[0].remaining == 99
    assert models[0].allowed is True
    assert models[0].exhausted is False


@pytest.mark.parametrize("spark_index", [100, 101])
def test_wham_recognizes_spark_entries_past_first_hundred_items(spark_index):
    additional_rate_limits = [
        {
            "limit_name": "not-spark",
            "metered_feature": "other-feature",
            "rate_limit": {"primary_window": {"used_percent": 1}},
        }
        for _ in range(spark_index)
    ]
    additional_rate_limits.append(
        {
            "limit_name": "GPT-5.3-Codex-Spark",
            "metered_feature": "codex_bengalfox",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 1,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 900,
                },
            },
        }
    )

    main, models = parse_wham_usage_pools(
        {"additional_rate_limits": additional_rate_limits},
        captured_at=NOW,
        source="wham",
    )

    assert main is None
    assert len(models) == 1
    assert models[0].key == SPARK_MODEL
    assert models[0].windows[0].remaining == 99
    assert models[0].allowed is True


def test_wham_supports_30_day_main_window_without_inventing_5h_or_weekly():
    main, models = parse_wham_usage_pools(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 5,
                    "limit_window_seconds": 2592000,
                    "reset_at": 1786759661,
                }
            }
        },
        captured_at=NOW,
        source="wham",
    )

    assert models == ()
    assert main is not None
    assert main.windows[0].name == "30d"
    assert main.windows[0].duration_seconds == 2592000
    assert legacy_windows(main) == (None, None)


def test_wham_rejects_string_usage_percent():
    main, _ = parse_wham_usage_pools(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": "5",
                    "limit_window_seconds": 604800,
                }
            }
        },
        captured_at=NOW,
        source="wham",
    )

    assert main is not None
    assert main.windows[0].remaining is None
    assert main.windows[0].remaining_percent is None


def test_wham_disables_pool_when_one_present_window_is_malformed():
    main, _ = parse_wham_usage_pools(
        {
            "rate_limit": {
                "primary_window": "malformed",
                "secondary_window": {
                    "used_percent": 20,
                    "limit_window_seconds": 604800,
                },
            }
        },
        captured_at=NOW,
        source="wham",
    )

    assert main is not None
    assert main.available is False
    assert main.windows[0].name == "weekly"
    assert main.exhausted is True


def test_wham_disables_pool_with_duplicate_window_identity():
    main, _ = parse_wham_usage_pools(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 10,
                    "limit_window_seconds": 604800,
                },
                "secondary_window": {
                    "used_percent": 20,
                    "limit_window_seconds": 604800,
                },
            }
        },
        captured_at=NOW,
        source="wham",
    )

    assert main is not None
    assert main.available is False
    assert main.exhausted is True


def test_wham_disables_control_only_pool():
    main, _ = parse_wham_usage_pools(
        {"rate_limit": {"allowed": True, "limit_reached": False}},
        captured_at=NOW,
        source="wham",
    )

    assert main is not None
    assert main.available is False
    assert main.exhausted is True


def test_wham_ignores_unrelated_additional_rate_limit():
    _, models = parse_wham_usage_pools(
        {
            "additional_rate_limits": [
                {
                    "limit_name": "Some Other Model",
                    "metered_feature": "other_meter",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 10,
                            "limit_window_seconds": 604800,
                        }
                    },
                }
            ]
        },
        captured_at=NOW,
        source="wham",
    )

    assert models == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [("limit_name", {"value": SPARK_MODEL}), ("metered_feature", [SPARK_METERED_FEATURE])],
)
def test_wham_ignores_non_string_spark_identifiers(field, value):
    item = {
        "limit_name": "unrelated",
        "metered_feature": "unrelated",
        "rate_limit": {
            "primary_window": {
                "used_percent": 10,
                "limit_window_seconds": 604800,
            }
        },
    }
    item[field] = value

    _, models = parse_wham_usage_pools(
        {"additional_rate_limits": [item]},
        captured_at=NOW,
        source="wham",
    )

    assert models == ()


@pytest.mark.parametrize("field", ["allowed", "limit_reached"])
def test_wham_disables_spark_pool_with_invalid_control_flag(field):
    _, models = parse_wham_usage_pools(
        {
            "additional_rate_limits": [
                {
                    "limit_name": SPARK_MODEL,
                    "rate_limit": {
                        field: "false",
                        "primary_window": {
                            "used_percent": 1,
                            "limit_window_seconds": 604800,
                        },
                    },
                }
            ]
        },
        captured_at=NOW,
        source="wham",
    )

    assert len(models) == 1
    assert models[0].available is False
    assert models[0].exhausted is True


def test_wham_disables_spark_pool_with_conflicting_duplicate_entries():
    _, models = parse_wham_usage_pools(
        {
            "additional_rate_limits": [
                {
                    "limit_name": SPARK_MODEL,
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 1,
                            "limit_window_seconds": 604800,
                        }
                    },
                },
                {
                    "metered_feature": "codex_bengalfox",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 100,
                            "limit_window_seconds": 604800,
                        }
                    },
                },
            ]
        },
        captured_at=NOW,
        source="wham",
    )

    assert len(models) == 1
    assert models[0].available is False
    assert models[0].exhausted is True


def test_wham_disables_valid_spark_pool_when_duplicate_entry_is_invalid():
    _, models = parse_wham_usage_pools(
        {
            "additional_rate_limits": [
                {
                    "limit_name": SPARK_MODEL,
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 1,
                            "limit_window_seconds": 604800,
                        }
                    },
                },
                {
                    "metered_feature": SPARK_METERED_FEATURE,
                    "rate_limit": {},
                },
            ]
        },
        captured_at=NOW,
        source="wham",
    )

    assert len(models) == 1
    assert models[0].available is False
    assert models[0].exhausted is True


def test_wham_materializes_unavailable_spark_for_only_invalid_entry():
    _, models = parse_wham_usage_pools(
        {
            "additional_rate_limits": [
                {
                    "limit_name": SPARK_MODEL,
                    "rate_limit": {},
                }
            ]
        },
        captured_at=NOW,
        source="wham",
    )

    assert len(models) == 1
    assert models[0].key == SPARK_MODEL
    assert models[0].available is False
    assert models[0].windows == ()


def test_wham_keeps_only_first_identical_spark_duplicate():
    item = {
        "limit_name": SPARK_MODEL,
        "rate_limit": {
            "primary_window": {
                "used_percent": 1,
                "limit_window_seconds": 604800,
            }
        },
    }

    _, models = parse_wham_usage_pools(
        {"additional_rate_limits": [item] * 1000},
        captured_at=NOW,
        source="wham",
    )

    assert len(models) == 1
    assert models[0].available is True


def test_model_catalog_cannot_reenable_disabled_usage_pool():
    disabled = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        available=False,
        availability_sources=("usage",),
    )

    models = merge_model_catalog((disabled,), (SPARK_MODEL,))

    assert models[0].available is False
    assert models[0].availability_sources == ("usage", "model_catalog")


def test_model_catalog_does_not_normalize_spark_identity():
    assert merge_model_catalog((), (f"{SPARK_MODEL} ",)) == ()
    assert merge_model_catalog((), (SPARK_MODEL.upper(),)) == ()


def test_model_catalog_bounds_arbitrary_iterators():
    def overlong_catalog():
        for index in range(101):
            yield SPARK_MODEL if index == 0 else f"model-{index}"
        raise AssertionError("model catalog iterator was consumed past its cap")

    assert merge_model_catalog((), overlong_catalog()) == ()


def test_model_catalog_bounds_pool_iterators(monkeypatch):
    monkeypatch.setattr(usage_limits_module, "MAX_MODEL_CATALOG_IDS", 2)
    consumed = []

    def overlong_pools():
        for index in range(5):
            consumed.append(index)
            yield UsagePool(key=f"model-{index}", display_name="Model")

    assert merge_model_catalog(overlong_pools(), ()) == ()
    assert consumed == [0, 1, 2]


@pytest.mark.parametrize(
    "model_ids",
    [None, 42.0, {SPARK_MODEL: True}, [SPARK_MODEL, 42]],
)
def test_app_server_ignores_malformed_model_catalog(model_ids):
    _, models = parse_app_server_usage_pools(
        {"rateLimits": {}},
        captured_at=NOW,
        model_ids=model_ids,
    )

    assert models == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("limit_name", f" {SPARK_MODEL}"),
        ("metered_feature", f"{SPARK_METERED_FEATURE} "),
    ],
)
def test_wham_does_not_normalize_spark_limit_identity(field, value):
    item = {
        "limit_name": "unrelated",
        "metered_feature": "unrelated",
        "rate_limit": {
            "primary_window": {
                "used_percent": 10,
                "limit_window_seconds": 604800,
            }
        },
    }
    item[field] = value

    _, models = parse_wham_usage_pools(
        {"additional_rate_limits": [item]},
        captured_at=NOW,
        source="wham",
    )

    assert models == ()


def test_app_server_parses_dynamic_main_and_spark_buckets():
    main, models = parse_app_server_usage_pools(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "usedPercent": 2,
                        "windowDurationMins": 300,
                        "resetsAt": 1784185662,
                    },
                    "secondary": {
                        "usedPercent": 51,
                        "windowDurationMins": 10080,
                        "resetsAt": 1784280925,
                    },
                },
                "codex_bengalfox": {
                    "limitId": "codex_bengalfox",
                    "limitName": "GPT-5.3-Codex-Spark",
                    "primary": {
                        "usedPercent": 0,
                        "windowDurationMins": 10080,
                        "resetsAt": 1784772460,
                    },
                    "secondary": None,
                },
            }
        },
        captured_at=NOW,
        model_ids=(SPARK_MODEL,),
    )

    assert main is not None
    assert [window.name for window in main.windows] == ["5h", "weekly"]
    assert [window.remaining for window in main.windows] == [98, 49]
    assert all(window.reset_at is not None for window in main.windows)
    assert len(models) == 1
    assert models[0].availability_sources == ("usage", "model_catalog")
    assert models[0].windows[0].name == "weekly"
    assert models[0].windows[0].reset_at is not None


def test_app_server_main_slots_infer_missing_window_durations():
    main, _ = parse_app_server_usage_pools(
        {
            "rateLimits": {
                "primary": {"usedPercent": 2},
                "secondary": {"usedPercent": 51},
            }
        },
        captured_at=NOW,
    )

    assert main is not None
    assert [window.name for window in main.windows] == ["5h", "weekly"]
    assert [window.remaining for window in main.windows] == [98, 49]
    assert main.has_valid_usage is True


def test_app_server_ignores_missing_main_duration_when_other_duration_is_invalid():
    main, _ = parse_app_server_usage_pools(
        {
            "rateLimits": {
                "primary": {"usedPercent": 2},
                "secondary": {"usedPercent": 51, "windowDurationMins": "bad"},
            }
        },
        captured_at=NOW,
    )

    assert main is None


def test_app_server_keeps_explicit_weekly_only_bucket_without_duplicate_inference():
    main, _ = parse_app_server_usage_pools(
        {
            "rateLimits": {
                "primary": {"usedPercent": 12, "windowDurationMins": 10080},
            }
        },
        captured_at=NOW,
    )

    assert main is not None
    assert [window.name for window in main.windows] == ["weekly"]
    assert main.has_valid_usage is True


def test_app_server_ignores_unclassified_duplicate_bucket():
    main, _ = parse_app_server_usage_pools(
        {
            "rateLimits": {
                "primary": {"usedPercent": 12},
                "secondary": {"usedPercent": 20, "windowDurationMins": 300},
            }
        },
        captured_at=NOW,
    )

    assert main is not None
    assert [window.name for window in main.windows] == ["5h"]
    assert main.has_valid_usage is True


def test_app_server_does_not_infer_missing_duration_for_spark_bucket():
    _, models = parse_app_server_usage_pools(
        {
            "rateLimitsByLimitId": {
                SPARK_METERED_FEATURE: {
                    "primary": {"usedPercent": 2},
                }
            }
        },
        captured_at=NOW,
    )

    assert len(models) == 1
    assert models[0].windows[0].name == "unknown"
    assert models[0].has_valid_usage is False


@pytest.mark.parametrize(
    ("codex_bucket", "expected_primary"),
    [
        ({"primary": {"usedPercent": 1, "windowDurationMins": 300}}, 99),
        ({}, 91),
        ({"primary": None}, 91),
    ],
)
def test_app_server_merges_partial_codex_bucket_with_top_level_windows(
    codex_bucket, expected_primary
):
    main, _ = parse_app_server_usage_pools(
        {
            "rateLimits": {
                "primary": {"usedPercent": 9, "windowDurationMins": 300},
                "secondary": {"usedPercent": 40, "windowDurationMins": 10080},
            },
            "rateLimitsByLimitId": {"codex": codex_bucket},
        },
        captured_at=NOW,
    )

    assert main is not None
    assert [window.name for window in main.windows] == ["5h", "weekly"]
    assert [window.remaining for window in main.windows] == [expected_primary, 60]
    assert main.has_valid_usage is True


def test_app_server_does_not_infer_nested_window_over_unsupported_top_level():
    main, _ = parse_app_server_usage_pools(
        {
            "rateLimits": {
                "primary": {"usedPercent": 90, "windowDurationMins": 43200},
                "secondary": {"usedPercent": 40, "windowDurationMins": 10080},
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 1},
                    "secondary": {"usedPercent": 2, "windowDurationMins": 10080},
                }
            },
        },
        captured_at=NOW,
    )

    assert main is not None
    assert [window.name for window in main.windows] == ["30d", "weekly"]
    assert [window.remaining for window in main.windows] == [10, 98]
    assert main.has_valid_usage is True


def test_app_server_retains_top_level_windows_when_nested_codex_primary_is_malformed():
    main, _ = parse_app_server_usage_pools(
        {
            "rateLimits": {
                "primary": {"usedPercent": 9, "windowDurationMins": 300},
                "secondary": {"usedPercent": 40, "windowDurationMins": 10080},
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": "malformed",
                    "secondary": None,
                }
            },
        },
        captured_at=NOW,
    )

    assert main is not None
    assert [window.name for window in main.windows] == ["5h", "weekly"]
    assert [window.remaining for window in main.windows] == [91, 60]
    assert main.available is False
    assert main.has_valid_usage is False
    assert main.exhausted is True


def test_app_server_materializes_unavailable_main_when_only_nested_window_is_malformed():
    main, _ = parse_app_server_usage_pools(
        {
            "rateLimits": {"primary": "malformed"},
            "rateLimitsByLimitId": {"codex": {"primary": "malformed"}},
        },
        captured_at=NOW,
    )

    assert main is not None
    assert main.available is False
    assert main.windows == ()


def test_app_server_disables_main_when_codex_bucket_has_wrong_shape():
    main, _ = parse_app_server_usage_pools(
        {
            "rateLimits": {
                "primary": {"usedPercent": 2, "windowDurationMins": 300}
            },
            "rateLimitsByLimitId": {"codex": "malformed"},
        },
        captured_at=NOW,
    )

    assert main is not None
    assert main.available is False
    assert main.windows == ()


@pytest.mark.parametrize("value", [[], "malformed", 42])
def test_app_server_disables_main_when_limit_map_has_wrong_shape(value):
    main, _ = parse_app_server_usage_pools(
        {
            "rateLimits": {
                "primary": {"usedPercent": 2, "windowDurationMins": 300},
                "secondary": {"usedPercent": 3, "windowDurationMins": 10080},
            },
            "rateLimitsByLimitId": value,
        },
        captured_at=NOW,
    )

    assert main is not None
    assert main.available is False
    assert main.has_valid_usage is False


def test_app_server_disables_pool_when_one_present_window_is_malformed():
    main, _ = parse_app_server_usage_pools(
        {
            "rateLimits": {
                "primary": "malformed",
                "secondary": {
                    "usedPercent": 20,
                    "windowDurationMins": 10080,
                },
            }
        },
        captured_at=NOW,
    )

    assert main is not None
    assert main.available is False
    assert main.windows[0].name == "weekly"
    assert main.exhausted is True


def test_app_server_disables_pool_with_duplicate_window_identity():
    main, _ = parse_app_server_usage_pools(
        {
            "rateLimits": {
                "primary": {"usedPercent": 10, "windowDurationMins": 10080},
                "secondary": {"usedPercent": 20, "windowDurationMins": 10080},
            }
        },
        captured_at=NOW,
    )

    assert main is not None
    assert main.available is False
    assert main.exhausted is True


def test_app_server_disables_control_only_pool():
    main, _ = parse_app_server_usage_pools(
        {
            "rateLimitsByLimitId": {
                "codex": {"rateLimitReachedType": False},
            }
        },
        captured_at=NOW,
    )

    assert main is not None
    assert main.available is False
    assert main.exhausted is True


def test_app_server_rejects_string_usage_percent():
    main, _ = parse_app_server_usage_pools(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "usedPercent": "2",
                        "windowDurationMins": 300,
                    }
                }
            }
        },
        captured_at=NOW,
    )

    assert main is not None
    assert main.windows[0].remaining is None
    assert main.windows[0].remaining_percent is None


def test_app_server_disables_spark_with_conflicting_duplicate_buckets():
    _, models = parse_app_server_usage_pools(
        {
            "rateLimitsByLimitId": {
                "spark_a": {
                    "limitId": "codex_bengalfox",
                    "primary": {
                        "usedPercent": 1,
                        "windowDurationMins": 10080,
                    },
                },
                "spark_b": {
                    "limitName": "GPT-5.3-Codex-Spark",
                    "primary": {
                        "usedPercent": 100,
                        "windowDurationMins": 10080,
                    },
                },
            }
        },
        captured_at=NOW,
    )

    assert len(models) == 1
    assert models[0].available is False
    assert models[0].exhausted is True


def test_app_server_keeps_only_first_identical_spark_duplicate():
    bucket = {
        "limitId": "codex_bengalfox",
        "primary": {
            "usedPercent": 1,
            "windowDurationMins": 10080,
        },
    }

    _, models = parse_app_server_usage_pools(
        {"rateLimitsByLimitId": {f"spark-{index}": bucket for index in range(1000)}},
        captured_at=NOW,
    )

    assert len(models) == 1
    assert models[0].available is True


def test_app_server_disables_spark_when_exact_bucket_is_malformed():
    _, models = parse_app_server_usage_pools(
        {
            "rateLimitsByLimitId": {
                "codex_bengalfox": "malformed",
                "spark_alias": {
                    "limitName": "GPT-5.3-Codex-Spark",
                    "primary": {
                        "usedPercent": 1,
                        "windowDurationMins": 10080,
                    },
                },
            }
        },
        captured_at=NOW,
    )

    assert len(models) == 1
    assert models[0].available is False
    assert models[0].exhausted is True


def test_app_server_materializes_unavailable_spark_for_only_invalid_dict_bucket():
    _, models = parse_app_server_usage_pools(
        {"rateLimitsByLimitId": {SPARK_METERED_FEATURE: {}}},
        captured_at=NOW,
    )

    assert len(models) == 1
    assert models[0].key == SPARK_MODEL
    assert models[0].available is False
    assert models[0].windows == ()


def test_app_server_rejects_explicit_window_duration_above_maximum():
    main, _ = parse_app_server_usage_pools(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "usedPercent": 7,
                        "windowDurationMins": (10 * 365 * 24 * 60) + 1,
                    }
                }
            }
        },
        captured_at=NOW,
    )

    assert main is None


@pytest.mark.parametrize(
    ("reached_type", "expected"),
    [
        (True, True),
        (False, False),
        ("rate_limit_reached", True),
        ("workspace_owner_credits_depleted", True),
        ("workspace_member_credits_depleted", True),
        ("workspace_owner_usage_limit_reached", True),
        ("workspace_member_usage_limit_reached", True),
        ("primary_window", True),
        ("secondary_window", True),
    ],
)
def test_app_server_preserves_limit_reached_flag(reached_type, expected):
    _, models = parse_app_server_usage_pools(
        {
            "rateLimitsByLimitId": {
                "codex_bengalfox": {
                    "primary": {
                        "usedPercent": 1,
                        "windowDurationMins": 10080,
                    },
                    "rateLimitReachedType": reached_type,
                }
            }
        },
        captured_at=NOW,
    )

    assert len(models) == 1
    assert models[0].limit_reached is expected
    assert models[0].exhausted is expected


def test_app_server_rejects_unknown_limit_reached_type():
    _, models = parse_app_server_usage_pools(
        {
            "rateLimitsByLimitId": {
                "codex_bengalfox": {
                    "primary": {
                        "usedPercent": 1,
                        "windowDurationMins": 10080,
                    },
                    "rateLimitReachedType": "not_a_backend_enum",
                }
            }
        },
        captured_at=NOW,
    )

    assert len(models) == 1
    assert models[0].available is False
    assert models[0].limit_reached is None
    assert models[0].exhausted is True


def test_app_server_disables_pool_with_invalid_limit_reached_flag():
    _, models = parse_app_server_usage_pools(
        {
            "rateLimitsByLimitId": {
                "codex_bengalfox": {
                    "primary": {
                        "usedPercent": 1,
                        "windowDurationMins": 10080,
                    },
                    "rateLimitReachedType": {"value": "false"},
                }
            }
        },
        captured_at=NOW,
    )

    assert len(models) == 1
    assert models[0].available is False
    assert models[0].exhausted is True


@pytest.mark.parametrize("reached_type", ["", "   "])
def test_app_server_disables_pool_with_empty_limit_reached_flag(reached_type):
    _, models = parse_app_server_usage_pools(
        {
            "rateLimitsByLimitId": {
                "codex_bengalfox": {
                    "primary": {
                        "usedPercent": 1,
                        "windowDurationMins": 10080,
                    },
                    "rateLimitReachedType": reached_type,
                }
            }
        },
        captured_at=NOW,
    )

    assert len(models) == 1
    assert models[0].available is False
    assert models[0].exhausted is True


def test_model_catalog_does_not_mark_spark_available_without_usage_bucket():
    _, models = parse_app_server_usage_pools(
        {"rateLimits": {}},
        captured_at=NOW,
        model_ids=(SPARK_MODEL,),
    )

    assert len(models) == 1
    assert models[0].key == SPARK_MODEL
    assert models[0].available is False
    assert models[0].exhausted is True
    assert models[0].windows == ()
    assert models[0].availability_sources == ("model_catalog",)


def test_account_usage_serializes_dynamic_pools_without_breaking_legacy_fields():
    main, models = parse_wham_usage_pools(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 40,
                    "limit_window_seconds": 604800,
                }
            },
            "additional_rate_limits": [
                {
                    "metered_feature": "codex_bengalfox",
                    "rate_limit": {
                        "limit_reached": True,
                        "primary_window": {
                            "used_percent": 100,
                            "limit_window_seconds": 604800,
                        },
                    },
                }
            ],
        },
        captured_at=NOW,
        source="wham",
    )
    _, weekly = legacy_windows(main)
    usage = AccountUsage(
        account_id="private",
        label="Private",
        captured_at=NOW,
        weekly=weekly,
        main=main,
        models=models,
    )

    payload = usage.as_dict()

    assert payload["weekly"]["duration_seconds"] == 604800
    assert payload["main"]["windows"][0]["remaining"] == 60
    assert payload["models"][SPARK_MODEL]["exhausted"] is True
