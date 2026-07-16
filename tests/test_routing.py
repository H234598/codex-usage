from __future__ import annotations

from datetime import UTC, datetime, timedelta

from codex_usage.models import AccountStatus, AccountUsage, LimitWindow, UsagePool
from codex_usage.routing import (
    effective_paid_overage,
    evaluate_routing,
    load_policy,
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
            availability_sources=("usage",),
        )
        if main_windows
        else None,
        models=(spark,) if spark else (),
        stale=stale,
        backend_account_id=backend_account_id,
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


def test_routing_prefers_catalog_only_spark_but_marks_usage_unknown():
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

    assert result["decision"] == "spark"
    assert result["usage_state"] == "unknown"


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


def test_routing_fails_closed_after_spark_health_failure():
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
            "stale": False,
        },
    )

    assert result["decision"] == "main"
    assert result["reason"] == "spark_health_failed"


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
