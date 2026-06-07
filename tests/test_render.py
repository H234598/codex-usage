from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from codex_usage.models import AccountUsage, LimitWindow
from codex_usage.render import render_json, render_table


def test_render_table_contains_values():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
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


def test_render_json_is_machine_readable():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    rendered = render_json([usage])

    assert '"account": "privat"' in rendered
    assert '"status": "ok"' in rendered
