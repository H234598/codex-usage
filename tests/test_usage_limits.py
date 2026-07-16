from __future__ import annotations

from datetime import datetime

from codex_usage.extractor import LOCAL_TZ
from codex_usage.models import AccountUsage
from codex_usage.usage_limits import (
    SPARK_MODEL,
    legacy_windows,
    parse_app_server_usage_pools,
    parse_wham_usage_pools,
)

NOW = datetime(2026, 7, 16, 4, 0, tzinfo=LOCAL_TZ)


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


def test_model_catalog_marks_spark_available_when_usage_bucket_is_absent():
    _, models = parse_app_server_usage_pools(
        {"rateLimits": {}},
        captured_at=NOW,
        model_ids=(SPARK_MODEL,),
    )

    assert len(models) == 1
    assert models[0].key == SPARK_MODEL
    assert models[0].available is True
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
