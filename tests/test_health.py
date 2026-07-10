from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from codex_usage.health import MAX_HEALTH_EVENTS, clear_health, load_health, record_health_event


def test_health_is_bounded_and_redacts_invalid_account(tmp_path):
    path = tmp_path / "health.json"
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)

    for index in range(MAX_HEALTH_EVENTS + 12):
        record_health_event(
            "scheduler",
            "cycle_ok",
            account="valid-account",
            duration_ms=index,
            path=path,
            now=now,
        )
    record_health_event(
        "applet callback",
        "token=secret",
        account="token secret",
        error_class="ValueError: secret",
        path=path,
        now=now,
    )

    payload = load_health(path)
    serialized = path.read_text(encoding="utf-8")
    assert payload["event_count"] == MAX_HEALTH_EVENTS
    assert len(payload["events"]) == MAX_HEALTH_EVENTS
    assert len(serialized.encode("utf-8")) <= 256 * 1024
    assert "secret" not in serialized
    assert all("account" not in event for event in payload["events"][-1:])


def test_health_discards_old_events_and_can_be_cleared(tmp_path):
    path = tmp_path / "health.json"
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    record_health_event("watch", "old", path=path, now=now - timedelta(days=31))
    record_health_event("watch", "new", path=path, now=now)

    assert [event["event"] for event in load_health(path)["events"]] == ["new"]
    clear_health(path)
    assert load_health(path)["event_count"] == 0


def test_health_file_recovery_ignores_invalid_json(tmp_path):
    path = tmp_path / "health.json"
    path.write_text("{invalid", encoding="utf-8")

    record_health_event("watch", "recovered", path=path)

    payload = load_health(path)
    assert payload["event_count"] == 1
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
