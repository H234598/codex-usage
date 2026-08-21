from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from codex_usage.models import AccountStatus, AccountUsage, LimitWindow, UsagePool
from codex_usage.routing import (
    MAIN_MODEL,
    SPARK_HEALTH_MAX_AGE_SECONDS,
    effective_credit_limits,
    effective_paid_overage,
    evaluate_routing,
    load_policy,
    set_credit_limits,
    set_policy_rule,
)
from codex_usage.usage_limits import SPARK_MODEL

NOW = datetime(2026, 7, 16, 4, 0, tzinfo=UTC)


def _window(name: str, remaining: float, duration: int) -> LimitWindow:
    return LimitWindow(
        name=name,
        remaining=remaining,
        percent=remaining,
        duration_seconds=duration,
    )


def _usage(
    *,
    main_windows: tuple[LimitWindow, ...] = (),
    main_sources: tuple[str, ...] = ("usage",),
    spark: UsagePool | None = None,
    captured_at: datetime = NOW,
    stale: bool = False,
    backend_account_id: str = "backend-private",
) -> AccountUsage:
    return AccountUsage(
        account_id="private",
        label="Private",
        captured_at=captured_at,
        status=AccountStatus.OK,
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=main_windows,
            availability_sources=main_sources,
        )
        if main_windows
        else None,
        models=(spark,) if spark else (),
        stale=stale,
        backend_account_id=backend_account_id,
        backend_configured="direct",
        backend_used="direct",
    )


def test_routing_prefers_spark_with_weekly_only_limit():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(_window("weekly", 99, 604800),),
        available=True,
        availability_sources=("usage",),
    )

    result = evaluate_routing(
        _usage(main_windows=(_window("weekly", 80, 604800),), spark=spark),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "healthy",
            "reason": "test",
            "checked_at": NOW.isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "spark"
    assert result["model"] == SPARK_MODEL
    assert result["usage_state"] == "known"


def test_routing_fails_closed_for_error_with_ok_status():
    usage = replace(
        _usage(main_windows=(_window("weekly", 80, 604800),)),
        error="backend warning",
    )

    result = evaluate_routing(
        usage,
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "usage_error"
    assert result["usage_state"] == "unknown"


def test_routing_keeps_valid_spark_for_partial_main_usage():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(_window("weekly", 99, 604800),),
        available=True,
        availability_sources=("usage",),
    )
    usage = replace(
        _usage(main_windows=(_window("weekly", 80, 604800),), spark=spark),
        status=AccountStatus.PARTIAL,
        error="5h limit unavailable",
    )

    result = evaluate_routing(
        usage,
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "healthy",
            "reason": "test",
            "checked_at": NOW.isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "spark"


@pytest.mark.parametrize(
    "availability_sources, expected_decision",
    [
        (("usage",), "main"),
        (("usage", "model_catalog"), "main"),
        (("model_catalog",), "blocked"),
        ((), "blocked"),
    ],
)
def test_routing_requires_usage_provenance_for_main_pool(
    availability_sources: tuple[str, ...],
    expected_decision: str,
):
    result = evaluate_routing(
        _usage(
            main_windows=(_window("weekly", 80, 604800),),
            main_sources=availability_sources,
        ),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == expected_decision
    if expected_decision == "blocked":
        assert result["reason"] == "main_limit_unknown"
        assert result["usage_state"] == "unknown"
    else:
        assert result["model"] == MAIN_MODEL
        assert result["usage_state"] == "known"


def test_routing_does_not_select_catalog_only_spark_without_usage():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        available=True,
        availability_sources=("model_catalog",),
    )

    result = evaluate_routing(
        _usage(main_windows=(_window("weekly", 80, 604800),), spark=spark),
        role="exploriererin",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "healthy",
            "reason": "test",
            "checked_at": NOW.isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_usage_unknown"
    assert result["usage_state"] == "known"


def test_routing_does_not_select_catalog_only_spark_with_lookalike_windows():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(_window("weekly", 99, 604800),),
        available=True,
        availability_sources=("model_catalog",),
    )

    result = evaluate_routing(
        _usage(main_windows=(_window("weekly", 80, 604800),), spark=spark),
        role="explorierin",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "healthy",
            "reason": "test",
            "checked_at": NOW.isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_usage_unknown"


@pytest.mark.parametrize("timestamp_field", ["captured_at", "values_captured_at"])
def test_routing_fails_closed_for_naive_usage_timestamps(timestamp_field):
    usage = _usage(main_windows=(_window("weekly", 80, 604800),))
    usage = replace(usage, **{timestamp_field: NOW.replace(tzinfo=None)})

    result = evaluate_routing(
        usage,
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "usage_timestamp_invalid"
    assert result["model"] is None


def test_routing_fails_closed_for_non_enum_usage_status():
    usage = replace(
        _usage(main_windows=(_window("weekly", 80, 604800),)),
        status="ok",
    )

    result = evaluate_routing(
        usage,
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "usage_status_invalid"
    assert result["model"] is None


@pytest.mark.parametrize("field", ["stale", "cache_invalidated"])
def test_routing_fails_closed_for_non_boolean_usage_metadata(field):
    usage = replace(
        _usage(main_windows=(_window("weekly", 80, 604800),)),
        **{field: 0},
    )

    result = evaluate_routing(
        usage,
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "usage_metadata_invalid"
    assert result["model"] is None


def test_routing_does_not_select_spark_when_usage_window_has_no_value():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(LimitWindow(name="weekly", duration_seconds=604800),),
        available=True,
        availability_sources=("usage",),
    )

    result = evaluate_routing(
        _usage(main_windows=(_window("weekly", 80, 604800),), spark=spark),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "healthy",
            "reason": "test",
            "checked_at": NOW.isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_usage_unknown"


def test_routing_fails_closed_when_spark_health_is_unknown():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(_window("weekly", 99, 604800),),
        available=True,
    )

    result = evaluate_routing(
        _usage(main_windows=(_window("weekly", 80, 604800),), spark=spark),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_health_unverified"
    assert result["spark_health"]["state"] == "unknown"


def test_routing_blocks_spark_after_expired_spark_health_failure():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(_window("weekly", 99, 604800),),
        available=True,
    )

    result = evaluate_routing(
        _usage(main_windows=(_window("weekly", 80, 604800),), spark=spark),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "failed",
            "reason": "spark_turn_timeout",
            "checked_at": (NOW - timedelta(seconds=SPARK_HEALTH_MAX_AGE_SECONDS + 1)).isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_health_failed"
    assert result["model"] == MAIN_MODEL


def test_routing_treats_failed_spark_health_with_invalid_checked_at_as_unverified():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(_window("weekly", 99, 604800),),
        available=True,
    )

    result = evaluate_routing(
        _usage(main_windows=(_window("weekly", 80, 604800),), spark=spark),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "failed",
            "reason": "spark_turn_timeout",
            "checked_at": "not-a-timestamp",
            "stale": False,
        },
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_health_unverified"


def test_routing_treats_failed_spark_health_with_non_boolean_stale_as_unverified():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(_window("weekly", 99, 604800),),
        available=True,
    )

    result = evaluate_routing(
        _usage(main_windows=(_window("weekly", 80, 604800),), spark=spark),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "failed",
            "reason": "spark_turn_timeout",
            "checked_at": NOW.isoformat(),
            "stale": "false",
        },
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_health_unverified"


def test_spark_health_failure_aging_does_not_invalidate_failed_state():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(_window("weekly", 99, 604800),),
        available=True,
    )

    result = evaluate_routing(
        _usage(
            main_windows=(_window("weekly", 5, 604800),),
            spark=spark,
            captured_at=NOW - timedelta(minutes=1),
        ),
        role="arbeitsbiene",
        paid_overage_allowed=True,
        now=NOW,
        spark_health={
            "state": "failed",
            "reason": "spark_turn_timeout",
            "checked_at": (NOW - timedelta(days=30)).isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "credits"
    assert result["reason"] == "paid_overage_explicitly_allowed"


def test_routing_fails_closed_for_stale_spark_health():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(_window("weekly", 99, 604800),),
        available=True,
    )

    result = evaluate_routing(
        _usage(main_windows=(_window("weekly", 80, 604800),), spark=spark),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "healthy",
            "reason": "successful_spark_turn",
            "checked_at": NOW.isoformat(),
            "stale": True,
        },
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_health_stale"


def test_routing_blocks_spark_for_future_spark_health_timestamp():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(_window("weekly", 99, 604800),),
        available=True,
        availability_sources=("usage",),
    )

    result = evaluate_routing(
        _usage(main_windows=(_window("weekly", 80, 604800),), spark=spark),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "healthy",
            "reason": "successful_spark_turn",
            "checked_at": (NOW + timedelta(seconds=299)).isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_health_unverified"


def test_routing_fails_closed_for_expired_limit_reset():
    expired = replace(
        _window("weekly", 99, 604800),
        reset_at=NOW - timedelta(seconds=1),
    )
    result = evaluate_routing(
        _usage(main_windows=(expired,)),
        role="arbeitsbiene",
        paid_overage_allowed=True,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "main_limit_unknown"


def test_routing_fails_closed_for_resetless_window_after_duration():
    result = evaluate_routing(
        _usage(
            main_windows=(_window("5h", 80, 18000),),
            captured_at=NOW - timedelta(hours=6),
        ),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        max_age_seconds=86_400,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "usage_stale"


def test_routing_fails_closed_for_expired_resetless_spark_window():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(_window("5h", 99, 18000),),
        available=True,
        availability_sources=("usage",),
    )
    result = evaluate_routing(
        _usage(
            main_windows=(_window("30d", 80, 2_592_000),),
            spark=spark,
            captured_at=NOW - timedelta(hours=6),
        ),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        max_age_seconds=86_400,
        spark_health={
            "state": "healthy",
            "reason": "test",
            "checked_at": NOW.isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "usage_stale"


def test_routing_fails_closed_for_reset_too_far_in_future():
    future = replace(
        _window("weekly", 99, 604800),
        reset_at=NOW + timedelta(days=365),
    )

    result = evaluate_routing(
        _usage(main_windows=(future,)),
        role="arbeitsbiene",
        paid_overage_allowed=True,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "main_limit_unknown"


def test_routing_fails_closed_for_non_tuple_main_windows():
    window = _window("weekly", 90, 604800)
    usage = _usage(main_windows=(window,))
    malformed_main = replace(usage.main, windows=[window])
    usage = replace(usage, main=malformed_main)

    result = evaluate_routing(
        usage,
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "main_limit_unknown"


def test_routing_fails_closed_for_malformed_main_pool():
    result = evaluate_routing(
        replace(
            _usage(main_windows=(_window("weekly", 90, 604800),)),
            main=object(),
        ),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "main_limit_unknown"


def test_routing_fails_closed_for_noncanonical_main_pool_key():
    usage = _usage(main_windows=(_window("weekly", 90, 604800),))
    malformed_main = replace(usage.main, key=SPARK_MODEL)

    result = evaluate_routing(
        replace(usage, main=malformed_main),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "main_limit_unknown"


@pytest.mark.parametrize(
    "spark_health",
    [
        {"state": "healthy", "stale": False},
        {
            "state": "healthy",
            "stale": False,
            "checked_at": (NOW - timedelta(hours=2)).isoformat(),
        },
        "healthy",
    ],
)
def test_routing_fails_closed_for_invalid_supplied_spark_health(spark_health):
    result = evaluate_routing(
        _usage(
            main_windows=(_window("weekly", 80, 604800),),
            spark=UsagePool(
                key=SPARK_MODEL,
                display_name="Spark",
                windows=(_window("weekly", 99, 604800),),
                available=True,
            ),
        ),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health=spark_health,
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_health_unverified"


def test_routing_uses_main_when_spark_is_exhausted_and_all_main_windows_are_safe():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(_window("weekly", 0, 604800),),
        available=True,
        limit_reached=True,
    )

    result = evaluate_routing(
        _usage(
            main_windows=(
                _window("5h", 11, 18000),
                _window("weekly", 49, 604800),
            ),
            spark=spark,
        ),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == "main"
    assert result["model"] == "gpt-5.4-mini"


def test_routing_accepts_weekly_or_30_day_main_without_five_hour_window():
    for window in (
        _window("weekly", 11, 604800),
        _window("30d", 11, 2592000),
    ):
        result = evaluate_routing(
            _usage(main_windows=(window,)),
            role="arbeitsbiene",
            paid_overage_allowed=False,
            now=NOW,
        )
        assert result["decision"] == "main"


def test_routing_accepts_canonical_weekly_window_name():
    result = evaluate_routing(
        _usage(main_windows=(_window("7d", 90, 604800),)),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == "main"


def test_routing_blocks_main_when_window_identity_is_unknown():
    result = evaluate_routing(
        _usage(
            main_windows=(LimitWindow(name="unknown", remaining=80),),
        ),
        role="arbeitsbiene",
        paid_overage_allowed=True,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "main_limit_unknown"


def test_routing_blocks_main_when_window_duration_is_unsupported():
    result = evaluate_routing(
        _usage(
            main_windows=(_window("unknown", 80, 3600),),
        ),
        role="arbeitsbiene",
        paid_overage_allowed=True,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "main_limit_unknown"


def test_routing_blocks_main_when_window_name_and_duration_conflict():
    result = evaluate_routing(
        _usage(
            main_windows=(_window("weekly", 80, 18000),),
        ),
        role="arbeitsbiene",
        paid_overage_allowed=True,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "main_limit_unknown"


def test_routing_does_not_select_spark_when_window_identity_is_unknown():
    result = evaluate_routing(
        _usage(
            main_windows=(_window("weekly", 80, 604800),),
            spark=UsagePool(
                key=SPARK_MODEL,
                display_name="Spark",
                windows=(LimitWindow(name="unknown", remaining=99),),
                available=True,
            ),
        ),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "healthy",
            "reason": "test",
            "checked_at": NOW.isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_usage_unknown"


def test_routing_does_not_select_spark_for_unsupported_window_duration():
    result = evaluate_routing(
        _usage(
            main_windows=(_window("weekly", 80, 604800),),
            spark=UsagePool(
                key=SPARK_MODEL,
                display_name="Spark",
                windows=(_window("unknown", 99, 3600),),
                available=True,
            ),
        ),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "healthy",
            "reason": "test",
            "checked_at": NOW.isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_usage_unknown"


def test_routing_does_not_select_spark_when_window_names_collide():
    result = evaluate_routing(
        _usage(
            main_windows=(_window("weekly", 80, 604800),),
            spark=UsagePool(
                key=SPARK_MODEL,
                display_name="Spark",
                windows=(
                    _window("", 99, 18000),
                    _window("", 88, 604800),
                ),
                available=True,
                availability_sources=("usage",),
            ),
        ),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "healthy",
            "reason": "test",
            "checked_at": NOW.isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_usage_unknown"


def test_routing_blocks_at_exact_threshold_without_paid_override():
    result = evaluate_routing(
        _usage(
            main_windows=(
                _window("5h", 10, 18000),
                _window("weekly", 90, 604800),
            )
        ),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "main_limit_at_or_below_threshold"


def test_routing_rejects_truthy_non_boolean_paid_policy():
    with pytest.raises(ValueError, match="paid_overage_allowed must be a boolean"):
        evaluate_routing(
            _usage(main_windows=(_window("weekly", 5, 604800),)),
            role="arbeitsbiene",
            paid_overage_allowed="false",
            now=NOW,
        )


@pytest.mark.parametrize("max_age", [float("nan"), float("inf"), 600.0, "600"])
def test_routing_rejects_non_integer_or_non_finite_age_limit(max_age):
    with pytest.raises(
        ValueError,
        match="max_age_seconds must be a finite integer of at least 60",
    ):
        evaluate_routing(
            _usage(main_windows=(_window("weekly", 80, 604800),)),
            role="arbeitsbiene",
            paid_overage_allowed=False,
            now=NOW,
            max_age_seconds=max_age,
        )


@pytest.mark.parametrize("remaining", [float("nan"), -1, 101])
def test_routing_treats_invalid_main_percent_as_unknown(remaining):
    result = evaluate_routing(
        _usage(main_windows=(_window("weekly", remaining, 604800),)),
        role="arbeitsbiene",
        paid_overage_allowed=True,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "main_limit_unknown"


def test_routing_rejects_invalid_remaining_with_percent():
    result = evaluate_routing(
        _usage(
            main_windows=(
                LimitWindow(
                    name="weekly",
                    remaining=-1,
                    percent=97,
                    duration_seconds=604800,
                ),
            )
        ),
        role="arbeitsbiene",
        paid_overage_allowed=True,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "main_limit_unknown"


@pytest.mark.parametrize("flag", ["allowed", "limit_reached", "available"])
def test_routing_fails_closed_for_invalid_main_pool_flags(flag):
    main = UsagePool(
        key="main",
        display_name="Codex",
        windows=(_window("weekly", 80, 604800),),
        availability_sources=("usage",),
        **{flag: "false"},
    )

    result = evaluate_routing(
        AccountUsage(
            account_id="private",
            label="Private",
            captured_at=NOW,
            status=AccountStatus.OK,
            main=main,
            backend_account_id="backend-private",
            backend_configured="direct",
            backend_used="direct",
        ),
        role="arbeitsbiene",
        paid_overage_allowed=True,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "main_limit_unknown"


@pytest.mark.parametrize(
    ("backend_configured", "backend_used"),
    [(None, "direct"), ("direct", None), ("app-server", "direct")],
)
def test_routing_fails_closed_for_invalid_backend_provenance(
    backend_configured, backend_used
):
    usage = replace(
        _usage(main_windows=(_window("weekly", 90, 604800),)),
        backend_configured=backend_configured,
        backend_used=backend_used,
    )

    result = evaluate_routing(
        usage,
        role="arbeitsbiene",
        paid_overage_allowed=True,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "backend_provenance_invalid"


@pytest.mark.parametrize(
    ("backend_user_id", "backend_account_id"),
    [
        (None, None),
        (0, "backend-private"),
        ("backend\nuser", None),
        (" ", "backend-private"),
        ("u" * 257, "backend-private"),
    ],
)
def test_routing_fails_closed_for_invalid_authenticated_backend_identity(
    backend_user_id, backend_account_id
):
    usage = replace(
        _usage(main_windows=(_window("weekly", 80, 604800),)),
        backend_user_id=backend_user_id,
        backend_account_id=backend_account_id,
    )

    result = evaluate_routing(
        usage,
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "backend_identity_invalid"


def test_routing_allows_browser_provenance_without_backend_identity():
    usage = replace(
        _usage(main_windows=(_window("weekly", 80, 604800),)),
        backend_configured="browser",
        backend_used="browser",
        backend_account_id=None,
    )

    result = evaluate_routing(
        usage,
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == "main"


@pytest.mark.parametrize("flag", ["allowed", "limit_reached", "available"])
def test_routing_fails_closed_for_invalid_spark_pool_flags(flag):
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(_window("weekly", 99, 604800),),
        **{flag: "false"},
    )

    result = evaluate_routing(
        _usage(main_windows=(_window("weekly", 80, 604800),), spark=spark),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "healthy",
            "reason": "test",
            "checked_at": NOW.isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_usage_invalid"


def test_routing_does_not_select_spark_for_invalid_percent():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(_window("weekly", 101, 604800),),
        available=True,
    )

    result = evaluate_routing(
        _usage(main_windows=(_window("weekly", 80, 604800),), spark=spark),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "healthy",
            "reason": "successful_spark_turn",
            "checked_at": NOW.isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_usage_invalid"


def test_routing_does_not_select_ambiguous_spark_pool():
    spark = UsagePool(
        key=SPARK_MODEL,
        display_name="Spark",
        windows=(_window("weekly", 99, 604800),),
        available=True,
    )
    duplicate = replace(spark, key=SPARK_MODEL.upper())

    result = evaluate_routing(
        replace(
            _usage(main_windows=(_window("weekly", 80, 604800),)),
            models=(spark, duplicate),
        ),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "healthy",
            "reason": "successful_spark_turn",
            "checked_at": NOW.isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "main"


def test_routing_rejects_duplicate_main_window_identity():
    low = _window("weekly", 5, 604800)
    high = _window("w", 90, 604800)

    result = evaluate_routing(
        _usage(main_windows=(low, high)),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "main_limit_unknown"


def test_routing_rejects_colliding_main_window_names():
    result = evaluate_routing(
        _usage(
            main_windows=(
                _window("", 5, 18000),
                _window("", 90, 604800),
            ),
        ),
        role="arbeitsbiene",
        paid_overage_allowed=True,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "main_limit_unknown"


def test_routing_rejects_non_string_window_name_with_duration():
    result = evaluate_routing(
        _usage(
            main_windows=(
                LimitWindow(
                    name=None,
                    remaining=90,
                    duration_seconds=604800,
                ),
            ),
        ),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "main_limit_unknown"


def test_routing_does_not_select_noncanonical_spark_pool():
    spark = UsagePool(
        key=SPARK_MODEL.upper(),
        display_name="Spark",
        windows=(_window("weekly", 99, 604800),),
        available=True,
    )

    result = evaluate_routing(
        replace(
            _usage(main_windows=(_window("weekly", 80, 604800),)),
            models=(spark,),
        ),
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
        spark_health={
            "state": "healthy",
            "reason": "successful_spark_turn",
            "checked_at": NOW.isoformat(),
            "stale": False,
        },
    )

    assert result["decision"] == "main"


def test_policy_rejects_truthy_non_boolean_rule(tmp_path):
    with pytest.raises(ValueError, match="policy value must be a boolean or None"):
        set_policy_rule("global", None, "false", path=tmp_path / "routing-policy.json")


@pytest.mark.parametrize("scope", [None, 1, []])
def test_policy_rejects_malformed_scope(scope, tmp_path):
    with pytest.raises(
        ValueError,
        match="policy scope must be global, account, group, agent or job",
    ):
        set_policy_rule(scope, None, False, path=tmp_path / "routing-policy.json")


@pytest.mark.parametrize("role", [None, 1, [], {}, "   "])
def test_routing_rejects_invalid_role_before_decision(role):
    with pytest.raises(ValueError, match="role must be a non-empty string"):
        evaluate_routing(
            _usage(main_windows=(_window("weekly", 90, 604800),)),
            role=role,
            paid_overage_allowed=False,
            now=NOW,
        )


def test_routing_allows_credits_only_for_known_low_main_limit():
    low = evaluate_routing(
        _usage(main_windows=(_window("weekly", 5, 604800),)),
        role="arbeitsbiene",
        paid_overage_allowed=True,
        now=NOW,
    )
    unknown = evaluate_routing(
        _usage(),
        role="arbeitsbiene",
        paid_overage_allowed=True,
        now=NOW,
    )

    assert low["decision"] == "credits"
    assert unknown["decision"] == "blocked"
    assert unknown["reason"] == "main_limit_unknown"


def test_routing_blocks_credits_for_partial_usage():
    usage = replace(
        _usage(main_windows=(_window("weekly", 5, 604800),)),
        status=AccountStatus.PARTIAL,
        error="5h limit unavailable",
    )

    result = evaluate_routing(
        usage,
        role="arbeitsbiene",
        paid_overage_allowed=True,
        now=NOW,
    )

    assert result["decision"] == "blocked"
    assert result["reason"] == "usage_incomplete"
    assert result["model"] is None


def test_routing_fails_closed_for_stale_usage_and_exempts_teamleitung():
    usage = _usage(
        main_windows=(_window("weekly", 90, 604800),),
        captured_at=NOW - timedelta(minutes=11),
    )

    blocked = evaluate_routing(
        usage,
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=NOW,
    )
    exempt = evaluate_routing(
        usage,
        role="teamleiterin",
        paid_overage_allowed=False,
        now=NOW,
    )

    assert blocked["reason"] == "usage_too_old"
    assert exempt["decision"] == "unchanged"


def test_paid_overage_policy_precedence_and_inherit(tmp_path):
    path = tmp_path / "routing-policy.json"
    set_policy_rule("global", None, True, path=path)
    set_policy_rule("account", "private", False, path=path)
    set_policy_rule("agent", "a1", True, path=path)
    set_policy_rule("group", "frontend", False, path=path)
    set_policy_rule("job", "job-42", True, path=path)
    policy = load_policy(path)

    assert effective_paid_overage(
        policy,
        account="private",
        group="frontend",
        agent="a1",
        job="job-42",
    ) == (True, "job:job-42")
    set_policy_rule("job", "job-42", None, path=path)
    policy = load_policy(path)
    assert effective_paid_overage(
        policy,
        account="private",
        group="frontend",
        agent="a1",
    ) == (False, "group:frontend")
    assert path.stat().st_mode & 0o777 == 0o600


def test_scoped_credit_limits_override_global_per_dimension(tmp_path):
    path = tmp_path / "routing-policy.json"
    set_credit_limits({"hourly": 10, "weekly": 100, "monthly": 1000}, path=path)
    set_credit_limits(
        {"hourly": 2, "weekly": None, "monthly": 0},
        scope="account", identifier="private", path=path,
    )
    policy = load_policy(path)

    assert policy["credit_limits"] == {"hourly": 10.0, "weekly": 100.0, "monthly": 1000.0}
    assert effective_credit_limits(policy, account="private") == (
        {"hourly": 2.0, "weekly": 100.0, "monthly": 1000.0},
        "account:private",
    )


def test_scoped_credit_limits_use_specific_scope_first(tmp_path):
    path = tmp_path / "routing-policy.json"
    set_credit_limits({"hourly": 10}, path=path)
    set_credit_limits({"hourly": 8}, scope="account", identifier="private", path=path)
    set_credit_limits({"hourly": 4}, scope="job", identifier="build", path=path)
    policy = load_policy(path)

    assert effective_credit_limits(
        policy, account="private", job="build"
    ) == ({"hourly": 4.0, "weekly": None, "monthly": None}, "job:build")


def test_scoped_credit_limit_zero_removes_override(tmp_path):
    path = tmp_path / "routing-policy.json"
    set_credit_limits({"hourly": 2}, scope="account", identifier="private", path=path)
    set_credit_limits(
        {"hourly": 0, "weekly": 0, "monthly": 0},
        scope="account", identifier="private", path=path,
    )
    policy = load_policy(path)
    assert policy["credit_limit_overrides"]["account"] == {}
