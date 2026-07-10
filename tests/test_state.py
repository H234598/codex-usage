from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from codex_usage.models import AccountStatus, AccountUsage, LimitWindow
from codex_usage.state import (
    load_current_usage,
    load_usage_snapshot,
    merge_current_with_last_success,
    save_current_usage,
    save_usage_snapshot,
)


def test_load_usage_snapshot_ignores_invalid_json(tmp_path):
    (tmp_path / "privat.json").write_text("{not-json", encoding="utf-8")

    assert load_usage_snapshot("privat", tmp_path) is None


def test_load_usage_snapshot_ignores_symlink(tmp_path):
    target = tmp_path / "target.json"
    target.write_text(
        """
{
  "account": "privat",
  "label": "Privat",
  "captured_at": "2026-06-08T04:20:00+02:00",
  "status": "ok"
}
""",
        encoding="utf-8",
    )
    (tmp_path / "privat.json").symlink_to(target)

    assert load_usage_snapshot("privat", tmp_path) is None


def test_save_usage_snapshot_rejects_unsafe_account_id(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    usage = AccountUsage(
        account_id="../escape",
        label="Escape",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    with pytest.raises(ValueError, match="account id"):
        save_usage_snapshot(usage, snapshot_dir)

    assert not (tmp_path / "escape.json").exists()


def test_save_and_load_usage_snapshot_preserves_blocked_state(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    blocked_until = datetime(2026, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        status=AccountStatus.BLOCKED,
        error="usage limit reached",
        blocked_until=blocked_until,
        blocked_reason="usage limit reached: weekly",
        auth_last_refresh=datetime(2026, 7, 9, 23, 17, tzinfo=ZoneInfo("Europe/Berlin")),
        auth_access_expires_at=datetime(2026, 7, 19, 23, 17, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    path = save_usage_snapshot(usage, snapshot_dir)
    loaded = load_usage_snapshot("privat", snapshot_dir)

    assert path.name == "privat.json"
    assert loaded is not None
    assert loaded.status == AccountStatus.BLOCKED
    assert loaded.blocked_until == blocked_until
    assert loaded.blocked_reason == "usage limit reached: weekly"
    assert loaded.auth_last_refresh == datetime(
        2026, 7, 9, 23, 17, tzinfo=ZoneInfo("Europe/Berlin")
    )
    assert loaded.auth_access_expires_at == datetime(
        2026, 7, 19, 23, 17, tzinfo=ZoneInfo("Europe/Berlin")
    )


def test_current_status_keeps_last_success_values_separate(tmp_path):
    captured = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    current_dir = tmp_path / "current"
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        status=AccountStatus.LOGIN_REQUIRED,
        error="token expired",
        backend_configured="app-server",
        backend_used="app-server",
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        five_hour=LimitWindow(name="5h", remaining=70),
        weekly=LimitWindow(name="weekly", remaining=80),
    )

    save_current_usage(current, current_dir)
    loaded = load_current_usage("privat", current_dir)
    assert loaded is not None
    merged = merge_current_with_last_success(loaded, last_success)

    assert merged.status == AccountStatus.LOGIN_REQUIRED
    assert merged.five_hour == last_success.five_hour
    assert merged.values_captured_at == captured
    assert merged.stale is True
    assert merged.backend_used == "app-server"
