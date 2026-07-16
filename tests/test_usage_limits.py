from __future__ import annotations

from datetime import datetime

import pytest

from codex_usage.extractor import LOCAL_TZ
from codex_usage.models import AccountUsage, LimitWindow, UsagePool
from codex_usage.usage_limits import (
    SPARK_MODEL,
    legacy_windows,
    merge_model_catalog,
    parse_app_server_usage_pools,
    parse_wham_usage_pools,
)

NOW = datetime(2026, 7, 16, 4, 0, tzinfo=LOCAL_TZ)


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


@pytest.mark.parametrize(
    ("reached_type", "expected"),
    [(True, True), (False, False), ("primary_window", True)],
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
