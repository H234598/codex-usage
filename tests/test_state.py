from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from codex_usage.models import AccountStatus, AccountUsage
from codex_usage.state import load_usage_snapshot, save_usage_snapshot


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
