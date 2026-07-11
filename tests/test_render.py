from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from codex_usage.models import Account, AccountStatus, AccountUsage, LimitWindow
from codex_usage.render import render_account_values, render_json, render_table


def test_render_table_contains_values():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        auth_last_refresh=datetime(2026, 7, 9, 23, 17, tzinfo=ZoneInfo("Europe/Berlin")),
        auth_access_expires_at=datetime(2026, 7, 19, 23, 17, tzinfo=ZoneInfo("Europe/Berlin")),
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
    assert "bis 19.07.2026 23:17" in rendered


def test_render_table_labels_remaining_percent_windows():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
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


def test_render_account_values_is_compact_and_includes_missing_accounts():
    accounts = (
        Account(id="privat", label="Privat", profile_dir="/tmp/privat"),
        Account(id="work", label="Work", profile_dir="/tmp/work"),
    )
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
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


def test_render_json_is_machine_readable():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    rendered = render_json([usage])

    assert '"account": "privat"' in rendered
    assert '"status": "ok"' in rendered


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
