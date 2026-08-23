from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest

import codex_usage.health as health_module
from codex_usage.health import (
    HEALTH_RETENTION,
    MAX_HEALTH_EVENTS,
    clear_health,
    load_health,
    record_health_event,
)


class _RaisingTimezone(tzinfo):
    def utcoffset(self, _value):
        raise RuntimeError("synthetic timezone marker")


class _ForeignClock(datetime):
    def astimezone(self, _tz=UTC):
        return "not-a-datetime"


class _BrokenInt(int):
    def __ge__(self, _other):
        raise RuntimeError("synthetic health integer comparison marker")


def test_default_health_path_uses_default_state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(health_module, "default_state_dir", lambda: tmp_path)

    assert health_module.default_health_path() == tmp_path / "health.json"
    assert health_module._health_path(None) == tmp_path / "health.json"

    def __le__(self, _other):
        raise RuntimeError("synthetic health integer comparison marker")

    def __lt__(self, _other):
        raise RuntimeError("synthetic health integer comparison marker")


def test_health_is_bounded_and_redacts_invalid_account(tmp_path):
    path = tmp_path / "health.json"
    now = datetime.now(UTC)

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


def test_health_rejects_duration_integer_subclasses_before_arithmetic(tmp_path):
    broken = _BrokenInt(100)
    path = tmp_path / "health.json"

    record_health_event("watch", "cycle_ok", duration_ms=broken, path=path)

    assert "duration_ms" not in load_health(path)["events"][0]
    assert health_module._valid_event(
        {
            "at": datetime.now(UTC).isoformat(),
            "component": "watch",
            "event": "cycle_ok",
            "duration_ms": broken,
        }
    ) is False


@pytest.mark.parametrize("account", [[], {}, 42])
def test_health_redacts_non_string_account(tmp_path, account):
    path = tmp_path / "health.json"

    record_health_event("scheduler", "cycle_ok", account=account, path=path)

    assert "account" not in load_health(path)["events"][0]


@pytest.mark.parametrize(
    "now",
    [
        [],
        {},
        "invalid",
        1,
        True,
        object(),
        datetime(2026, 8, 16, 10, 0),
        datetime.min.replace(tzinfo=timezone(timedelta(hours=14))),
        datetime.max.replace(tzinfo=timezone(-timedelta(hours=14))),
        datetime(2026, 8, 16, 10, 0, tzinfo=_RaisingTimezone()),
    ],
)
def test_health_uses_safe_clock_for_malformed_now(tmp_path, now):
    path = tmp_path / "health.json"

    record_health_event("scheduler", "cycle_ok", path=path, now=now)  # type: ignore[arg-type]

    assert load_health(path)["event_count"] == 1


def test_health_uses_safe_clock_when_datetime_returns_foreign_value(tmp_path):
    path = tmp_path / "health.json"

    record_health_event(
        "scheduler",
        "cycle_ok",
        path=path,
        now=_ForeignClock(2026, 8, 16, 10, 0, tzinfo=UTC),
    )

    assert load_health(path)["event_count"] == 1


@pytest.mark.parametrize(
    ("field", "fallback"),
    [("component", "unknown"), ("event", "unknown"), ("error_class", "Error")],
)
def test_health_falls_back_for_unstringifiable_tokens(tmp_path, field, fallback):
    class BrokenToken:
        def __str__(self):
            raise RuntimeError("synthetic token failure")

    path = tmp_path / "health.json"
    values = {"component": "scheduler", "event": "cycle_ok"}
    values[field] = BrokenToken()
    record_health_event(path=path, **values)  # type: ignore[arg-type]

    assert load_health(path)["events"][0][field] == fallback


@pytest.mark.parametrize("path", [[], "invalid", 1, False, object()])
def test_health_rejects_non_path(path):
    with pytest.raises(ValueError, match="health path is invalid"):
        record_health_event("scheduler", "cycle_ok", path=path)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="health path is invalid"):
        load_health(path)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="health path is invalid"):
        clear_health(path)  # type: ignore[arg-type]


def test_health_discards_old_events_and_can_be_cleared(tmp_path):
    path = tmp_path / "health.json"
    now = datetime.now(UTC)
    record_health_event("watch", "old", path=path, now=now - timedelta(days=31))
    record_health_event("watch", "new", path=path, now=now)

    assert [event["event"] for event in load_health(path)["events"]] == ["new"]
    clear_health(path)
    assert load_health(path)["event_count"] == 0


def test_health_load_excludes_expired_events_without_a_new_write(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "health.json"
    recorded_at = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    read_at = recorded_at + HEALTH_RETENTION + timedelta(seconds=1)
    record_health_event("watch", "old", path=path, now=recorded_at)
    persisted = path.read_bytes()

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return read_at.replace(tzinfo=None)
            return read_at.astimezone(tz)

    monkeypatch.setattr(health_module, "datetime", FrozenDateTime)

    payload = load_health(path)

    assert payload == {
        "version": 1,
        "event_count": 0,
        "event_counts": {},
        "events": [],
    }
    assert path.read_bytes() == persisted


def test_health_read_stops_after_valid_tail(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "events": [{"index": index} for index in range(MAX_HEALTH_EVENTS + 1)],
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    def valid_event(event):
        if event["index"] == 0:
            raise AssertionError("health parser validated beyond retained tail")
        return True

    monkeypatch.setattr(health_module, "_valid_event", valid_event)

    events = health_module._read_events(path)

    assert [event["index"] for event in events] == list(range(1, MAX_HEALTH_EVENTS + 1))


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_health_requires_strict_version(tmp_path, version):
    path = tmp_path / "health.json"
    path.write_text(
        json.dumps(
            {
                "version": version,
                "events": [
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "component": "watch",
                        "event": "cycle_ok",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    assert load_health(path)["event_count"] == 0


def test_health_file_recovery_ignores_invalid_json(tmp_path):
    path = tmp_path / "health.json"
    path.write_text("{invalid", encoding="utf-8")

    record_health_event("watch", "recovered", path=path)

    payload = load_health(path)
    assert payload["event_count"] == 1
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_health_load_ignores_group_readable_file(tmp_path):
    path = tmp_path / "health.json"
    record_health_event("watch", "private", path=path)
    path.chmod(0o640)

    assert load_health(path)["event_count"] == 0


def test_health_load_ignores_hard_linked_file(tmp_path):
    path = tmp_path / "health.json"
    linked = tmp_path / "health-copy.json"
    record_health_event("watch", "private", path=path)
    os.link(path, linked)

    assert path.stat().st_nlink == 2
    assert load_health(path)["event_count"] == 0


def test_health_read_rejects_non_list_events(tmp_path):
    path = tmp_path / "health.json"
    path.write_text(json.dumps({"version": 1, "events": {}}), encoding="utf-8")
    path.chmod(0o600)

    assert load_health(path)["event_count"] == 0


def test_health_read_recovers_from_private_io_error(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(
        health_module,
        "read_private_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read blocked")),
    )

    assert load_health(path)["event_count"] == 0


@pytest.mark.parametrize(
    "event",
    [
        None,
        {},
        {"at": None, "component": "watch", "event": "cycle_ok"},
        {"at": "2026-08-16T10:00:00+00:00", "component": "bad token", "event": "cycle_ok"},
        {"at": "2026-08-16T10:00:00+00:00", "component": "watch", "event": "bad token"},
        {
            "at": "2026-08-16T10:00:00+00:00",
            "component": "watch",
            "event": "cycle_ok",
            "account": "bad token",
        },
        {
            "at": "2026-08-16T10:00:00+00:00",
            "component": "watch",
            "event": "cycle_ok",
            "error_class": "bad token",
        },
        {"at": "not-a-timestamp", "component": "watch", "event": "cycle_ok"},
    ],
)
def test_health_valid_event_rejects_malformed_fields(event):
    assert health_module._valid_event(event) is False


def test_health_trim_skips_malformed_event_and_normalizes_naive_timestamp():
    now = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    naive = {
        "at": "2026-08-16T09:00:00",
        "component": "watch",
        "event": "cycle_ok",
    }

    assert health_module._trim_events([{}, naive], now) == [naive]


def test_health_write_discards_oldest_events_until_size_fits(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    monkeypatch.setattr(health_module, "MAX_HEALTH_BYTES", 1)

    health_module._write_events(
        path,
        [{
            "at": datetime.now(UTC).isoformat(),
            "component": "watch",
            "event": "cycle_ok",
        }],
    )

    assert load_health(path)["event_count"] == 0


def test_health_write_fails_when_directory_chmod_fails(tmp_path, monkeypatch):
    path = tmp_path / "health-dir" / "health.json"

    def fail_chmod(_fd, _mode):
        raise PermissionError("health directory chmod blocked")

    monkeypatch.setattr("codex_usage.private_io.os.fchmod", fail_chmod)

    with pytest.raises(PermissionError, match="health directory chmod blocked"):
        record_health_event("watch", "blocked", path=path)

    assert not path.exists()
