from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from codex_usage.config import MAX_CONFIG_ACCOUNTS
from codex_usage.models import Account, AccountStatus, AccountUsage, LimitWindow, UsagePool
from codex_usage.render import (
    _auth_value,
    _extra_main_value,
    _fmt_number,
    _is_finite_number,
    _is_remaining_percent_window,
    _remaining_percent,
    _safe_usage_for_display,
    _usage_value,
    render_account_values,
    render_json,
    render_table,
)


def test_render_table_contains_values(monkeypatch):
    now = datetime(2026, 7, 10, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    class Clock:
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now

    monkeypatch.setattr("codex_usage.render.datetime", Clock)

    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        auth_last_refresh=datetime(2026, 7, 9, 23, 17, tzinfo=ZoneInfo("Europe/Berlin")),
        auth_access_expires_at=datetime(2099, 7, 19, 23, 17, tzinfo=ZoneInfo("Europe/Berlin")),
        five_hour=LimitWindow(
            name="5h",
            used=42,
            limit=100,
            percent=42,
            reset_at=datetime(2026, 6, 8, 4, 26, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
        weekly=LimitWindow(
            name="weekly",
            used=310,
            limit=1000,
            percent=31,
            reset_at=datetime(2026, 6, 14, 4, 26, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
    )

    rendered = render_table([usage])

    assert "Privat" in rendered
    assert "42 / 100" in rendered
    assert "08.06.2026 04:26" in rendered
    assert "310 / 1000" in rendered
    assert "58% verbleibend" in rendered
    assert "69% verbleibend" in rendered
    assert "Auth" in rendered
    assert "bis 19.07.2099 23:17" in rendered


def test_render_hides_values_without_backend_provenance():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
    )

    rendered = render_table([usage])

    assert "97% verbleibend" not in rendered
    assert "55% verbleibend" not in rendered
    assert "incomplete usage backend prov" in rendered


def test_render_hides_values_for_invalid_expected_backend():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", remaining=97),
    )

    safe = _safe_usage_for_display(usage, expected_backend="")

    assert safe.five_hour is None
    assert safe.cache_invalidated is True
    assert safe.stale is True


def test_render_clears_login_required_values():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        status=AccountStatus.LOGIN_REQUIRED,
        error="token expired",
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
    )

    table = render_table([usage])
    payload = json.loads(render_json([usage]))[0]

    assert "97% verbleibend" not in table
    assert "55% verbleibend" not in table
    assert payload["five_hour"] is None
    assert payload["weekly"] is None
    assert payload["main"] is None
    assert payload["models"] == {}
    assert payload["stale"] is True
    assert payload["cache_invalidated"] is True


def test_render_clears_values_for_invalid_status():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        status="ok",
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
    )

    safe = _safe_usage_for_display(usage)
    table = render_table([usage])
    payload = json.loads(render_json([usage]))[0]

    assert safe.status == AccountStatus.ERROR
    assert safe.five_hour is None
    assert safe.weekly is None
    assert safe.stale is True
    assert safe.cache_invalidated is True
    assert "97% verbleibend" not in table
    assert "55% verbleibend" not in table
    assert payload["five_hour"] is None
    assert payload["weekly"] is None
    assert payload["status"] == "error"


def test_render_clears_values_for_error_status_without_cache_flag():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        status=AccountStatus.ERROR,
        error="backend failed",
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
    )

    table = render_table([usage])
    payload = json.loads(render_json([usage]))[0]

    assert "97% verbleibend" not in table
    assert "55% verbleibend" not in table
    assert payload["five_hour"] is None
    assert payload["weekly"] is None
    assert payload["main"] is None
    assert payload["models"] == {}
    assert payload["stale"] is True
    assert payload["cache_invalidated"] is True


def test_render_uses_dst_aware_local_timezone(monkeypatch):
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 1, 15, 0, 15, tzinfo=berlin)
    calls = []

    class Clock:
        @classmethod
        def now(cls, tz=None):
            calls.append(tz)
            return now.astimezone(tz) if tz is not None else now

    monkeypatch.setattr("codex_usage.render.LOCAL_TZ", berlin)
    monkeypatch.setattr("codex_usage.render.datetime", Clock)

    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=now,
        auth_access_expires_at=datetime(2026, 1, 15, 1, 15, tzinfo=berlin),
    )

    assert "Stand: 15.01.2026 00:15" in render_table([usage])
    assert _auth_value(usage) == "bis 15.01.2026 01:15"
    assert calls == [berlin, berlin, berlin]


def test_render_auth_value_localizes_naive_expiry():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 1, 15, tzinfo=ZoneInfo("Europe/Berlin")),
        auth_access_expires_at=datetime(2099, 1, 1, 12, 0),
    )

    assert _auth_value(usage) == "bis 01.01.2099 12:00"


def test_render_table_labels_remaining_percent_windows():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(
            name="5h",
            used=3,
            limit=100,
            remaining=97,
            percent=97,
            reset_at=datetime(2026, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
        weekly=LimitWindow(
            name="weekly",
            used=45,
            limit=100,
            remaining=55,
            percent=55,
            reset_at=datetime(2026, 6, 10, 5, 5, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
    )

    rendered = render_table([usage])

    assert "97% verbleibend" in rendered
    assert "55% verbleibend" in rendered
    assert "08.06.2026 06:50" in rendered
    assert "10.06.2026 05:05" in rendered


def test_render_table_prefers_absolute_usage_over_conflicting_remaining_fields():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(
            name="5h",
            used=0,
            limit=100,
            remaining=0,
            percent=0,
        ),
    )

    rendered = render_table([usage])

    assert "0 / 100" in rendered
    assert "0 / 100  100% verbleibend" in rendered


def test_render_ignores_integer_overflow_without_hiding_other_window():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", used=10**309, limit=100),
        weekly=LimitWindow(name="weekly", used=10, limit=100),
    )

    rendered = render_table([usage])

    assert _fmt_number(10**309) == "-"
    assert _is_finite_number(10**309) is False
    assert _remaining_percent(usage.five_hour) is None
    assert "90% verbleibend" in rendered


def test_render_table_converts_absolute_remaining_to_percent():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", limit=1000, remaining=690),
    )

    rendered = render_table([usage])

    assert "69% verbleibend" in rendered
    assert "Limit 1000" not in rendered


def test_render_prefers_percent_when_remaining_has_no_absolute_limit():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", remaining=690, percent=69),
    )

    rendered = render_table([usage])

    assert "69% verbleibend" in rendered
    assert "690% verbleibend" not in rendered


def test_render_fails_closed_for_conflicting_remaining_and_percent_without_limit():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", remaining=55, percent=97),
    )

    rendered = render_table([usage])

    assert _remaining_percent(usage.five_hour) is None
    assert "97% verbleibend" not in rendered
    assert "55% verbleibend" not in rendered


def test_render_rejects_denominatorless_absolute_remaining():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        five_hour=LimitWindow(name="5h", remaining=690),
    )

    rendered = render_table([usage])

    assert "690% verbleibend" not in rendered
    assert "100% verbleibend" not in rendered


def test_render_fails_closed_for_invalid_remaining_percent_values():
    for window in (
        LimitWindow(name="5h", remaining=101, percent=101),
        LimitWindow(name="5h", remaining=-1, percent=97),
        LimitWindow(name="5h", remaining=120, limit=100),
        LimitWindow(name="5h", percent=True),
        LimitWindow(name="5h", remaining=float("nan"), limit=100),
        LimitWindow(name="5h", percent=float("inf")),
    ):
        assert _remaining_percent(window) is None
        assert _is_remaining_percent_window(window) is False


@pytest.mark.parametrize("raw", [True, 42, object()])
def test_render_fails_closed_for_non_string_raw_value(raw):
    window = LimitWindow(name="5h", raw=raw)

    assert _usage_value(window) == "-"


def test_render_hides_percent_when_another_usage_field_is_invalid():
    window = LimitWindow(
        name="5h",
        used="bad",
        limit=100,
        remaining=97,
        percent=97,
    )

    assert _usage_value(window) == "-"
    assert _is_remaining_percent_window(window) is False


def test_render_does_not_format_boolean_as_number():
    assert _fmt_number(True) == "-"
    assert _fmt_number("97") == "-"
    assert _is_finite_number(True) is False
    assert _is_finite_number("97") is False
    assert _remaining_percent(LimitWindow(name="5h", used=True, limit=100)) is None


def test_render_table_treats_percent_only_window_as_remaining_percent():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", percent=97),
    )

    rendered = render_table([usage])

    assert "97% verbleibend" in rendered


def test_render_account_values_is_compact_and_includes_missing_accounts():
    accounts = (
        Account(id="privat", label="Privat", profile_dir="/tmp/privat"),
        Account(id="work", label="Work", profile_dir="/tmp/work"),
    )
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(
            name="5h",
            used=3,
            limit=100,
            remaining=97,
            percent=97,
            reset_at=datetime(2026, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
        weekly=LimitWindow(
            name="weekly",
            used=45,
            limit=100,
            remaining=55,
            percent=55,
            reset_at=datetime(2026, 6, 10, 5, 5, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
    )

    rendered = render_account_values(accounts, {"privat": usage})

    assert "Account" in rendered
    assert "Privat" in rendered
    assert "97% verbleibend" in rendered
    assert "55% verbleibend" in rendered
    assert "Work" in rendered
    assert "Stand:" not in rendered


def test_render_account_values_rejects_account_iterators_over_account_cap():
    with pytest.raises(ValueError, match="too many account records"):
        render_account_values(
            (
                Account(
                    id=f"account-{index}",
                    label="Account",
                    profile_dir=f"/tmp/account-{index}",
                )
                for index in range(MAX_CONFIG_ACCOUNTS + 1)
            ),
            {},
        )


def test_render_table_includes_dynamic_main_and_spark_limits():
    reset = datetime(2026, 7, 23, 4, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="private",
        label="Private",
        captured_at=reset,
        backend_configured="direct",
        backend_used="direct",
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(
                LimitWindow(
                    name="30d",
                    remaining=95,
                    percent=95,
                    duration_seconds=2592000,
                ),
            ),
        ),
        models=(
            UsagePool(
                key="gpt-5.3-codex-spark",
                display_name="Spark",
                windows=(
                    LimitWindow(
                        name="weekly",
                        remaining=100,
                        percent=100,
                        reset_at=reset,
                        duration_seconds=604800,
                    ),
                ),
            ),
        ),
    )

    rendered = render_table([usage])

    assert "Weitere Limits" in rendered
    assert "30d 95% verbleibend" in rendered
    assert "Spark" in rendered
    assert "weekly 100% verbleibend bis 23.07.2026 04:00" in rendered


def test_render_table_shows_available_spark_without_known_limit():
    usage = AccountUsage(
        account_id="private",
        label="Private",
        captured_at=datetime(2026, 7, 23, 4, 0, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", remaining=90),
        models=(
            UsagePool(
                key="gpt-5.3-codex-spark",
                display_name="Spark",
                available=True,
                availability_sources=("model_catalog",),
            ),
        ),
    )

    rendered = render_table([usage])

    assert "verfügbar; Limit unbekannt" in rendered


def test_extra_main_value_uses_name_only_core_window_identity():
    usage = AccountUsage(
        account_id="private",
        label="Private",
        captured_at=datetime(2026, 7, 23, 4, 0, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(
                LimitWindow(name="weekly", remaining=80, percent=80),
                LimitWindow(name="30d", remaining=95, percent=95, duration_seconds=2592000),
            ),
        ),
    )

    assert _extra_main_value(usage) == "30d 95% verbleibend"


@pytest.mark.parametrize(
    "main",
    [[], UsagePool(key="main", display_name="Codex", windows=None)],  # type: ignore[arg-type]
)
def test_render_table_hides_malformed_main_pool(main):
    usage = AccountUsage(
        account_id="private",
        label="Private",
        captured_at=datetime(2026, 7, 23, 4, 0, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        main=main,
    )

    rendered = render_table([usage])

    assert "Weitere Limits" in rendered
    assert _extra_main_value(usage) == "-"
    assert "AttributeError" not in rendered


def test_render_table_hides_unavailable_dynamic_pools():
    usage = AccountUsage(
        account_id="private",
        label="Private",
        captured_at=datetime(2026, 7, 23, 4, 0, tzinfo=ZoneInfo("Europe/Berlin")),
        main=UsagePool(
            key="main",
            display_name="Codex",
            available=False,
            windows=(LimitWindow(name="30d", remaining=95),),
        ),
        models=(
            UsagePool(
                key="gpt-5.3-codex-spark",
                display_name="Spark",
                available=True,
                windows=(LimitWindow(name="weekly", remaining=100),),
            ),
        ),
        cache_invalidated=True,
    )

    rendered = render_table([usage])

    assert "30d 95% verbleibend" not in rendered
    assert "weekly 100% verbleibend" not in rendered
    assert "nicht verfügbar" in rendered


def test_render_table_hides_dynamic_pools_without_window_identity():
    usage = AccountUsage(
        account_id="private",
        label="Private",
        captured_at=datetime(2026, 7, 23, 4, 0, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(LimitWindow(name="unknown", remaining=95),),
        ),
        models=(
            UsagePool(
                key="gpt-5.3-codex-spark",
                display_name="Spark",
                windows=(LimitWindow(name="", remaining=90),),
            ),
        ),
    )

    rendered = render_table([usage])

    assert "95% verbleibend" not in rendered
    assert "90% verbleibend" not in rendered
    assert "nicht verfügbar" in rendered


def test_render_json_is_machine_readable():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
    )

    rendered = render_json([usage])

    assert '"account": "privat"' in rendered
    assert '"status": "ok"' in rendered


def test_render_rejects_usage_iterators_over_account_cap():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )
    with pytest.raises(ValueError, match="too many usage records"):
        render_table(usage for _ in range(MAX_CONFIG_ACCOUNTS + 1))
    with pytest.raises(ValueError, match="too many usage records"):
        render_json(usage for _ in range(MAX_CONFIG_ACCOUNTS + 1))


def test_render_table_shows_blocked_state():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        status=AccountStatus.BLOCKED,
        blocked_until=datetime(2026, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin")),
        blocked_reason="usage limit reached: weekly",
    )

    rendered = render_table([usage])

    assert "blocked bis 08.06.2026 06:50" in rendered
    assert "usage limit reached" in rendered


def test_render_table_ignores_invalid_blocked_until():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        status=AccountStatus.BLOCKED,
        blocked_until="invalid",
        blocked_reason="usage limit reached",
    )

    rendered = render_table([usage])

    assert "blocked : usage limit reached" in rendered


def test_render_table_ignores_invalid_reset_at():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", remaining=50, reset_at="invalid"),
    )

    rendered = render_table([usage])

    assert "50% verbleibend" in rendered
    assert "invalid" not in rendered


def test_render_table_marks_stale_values_as_saved():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="direct",
        backend_used="direct",
        stale=True,
    )

    rendered = render_table([usage])

    assert "ok (gespeichert)" in rendered
