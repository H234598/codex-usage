from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from codex_usage.models import AccountStatus, AccountUsage, LimitWindow
from codex_usage.state import (
    backend_provenance_matches,
    backend_provenance_matches_configured,
    expire_reset_windows,
    load_current_usage,
    load_state_generation,
    load_usage_snapshot,
    merge_current_with_last_success,
    remove_account_state,
    save_current_usage,
    save_usage_snapshot,
)


def test_backend_provenance_rejects_explicit_cross_backend_cache_data():
    direct = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        backend_configured="direct",
        backend_used="direct",
    )
    override = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        backend_configured="direct",
        backend_used="app-server",
    )

    assert backend_provenance_matches_configured(override, "direct") is False
    assert backend_provenance_matches(direct, override) is False


def test_backend_provenance_rejects_unproven_cross_backend_fallback():
    direct = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        backend_used="direct",
        fallback_reason="arbitrary stale marker",
    )
    app_server = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        backend_used="app-server",
    )

    assert backend_provenance_matches_configured(direct, "app-server") is False
    assert backend_provenance_matches(direct, app_server) is False


def test_backend_provenance_accepts_explicit_direct_fallback_from_app_server():
    direct = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        backend_configured="app-server",
        backend_used="direct",
        fallback_reason="installed Codex does not support rate-limit RPC",
    )
    app_server = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        backend_used="app-server",
    )

    assert backend_provenance_matches_configured(direct, "app-server") is True
    assert backend_provenance_matches(direct, app_server) is True


def test_merge_rejects_unproven_cross_backend_cache_values():
    captured = datetime(2026, 7, 12, 9, 40, tzinfo=ZoneInfo("Europe/Berlin"))
    current = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=captured,
        status=AccountStatus.OK,
        backend_used="direct",
        backend_user_id="user-account",
        backend_account_id="account-id",
        five_hour=LimitWindow(name="5h", remaining=80),
    )
    last_success = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=captured - timedelta(minutes=5),
        status=AccountStatus.OK,
        backend_used="app-server",
        backend_user_id="user-account",
        backend_account_id="account-id",
        five_hour=LimitWindow(name="5h", remaining=70),
        weekly=LimitWindow(name="weekly", remaining=60),
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged is current
    assert merged.weekly is None


def test_expire_reset_windows_drops_only_expired_cached_values():
    reference_at = datetime(2026, 7, 12, 9, 40, tzinfo=ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=reference_at,
        status=AccountStatus.OK,
        five_hour=LimitWindow(
            name="5h",
            remaining=38,
            reset_at=reference_at - timedelta(seconds=1),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=72,
            reset_at=reference_at + timedelta(hours=1),
        ),
    )

    expired = expire_reset_windows(usage, reference_at=reference_at)

    assert expired.five_hour is None
    assert expired.weekly is usage.weekly
    assert expired.status == AccountStatus.PARTIAL
    assert expired.stale is True
    assert expired.error == "cached limit window expired: 5h; refresh required"


def test_expire_reset_windows_clears_expired_blocked_state():
    reference_at = datetime(2026, 7, 12, 9, 40, tzinfo=ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=reference_at - timedelta(minutes=1),
        status=AccountStatus.BLOCKED,
        error="usage limit reached: 5h, weekly",
        blocked_until=reference_at - timedelta(seconds=1),
        blocked_reason="usage limit reached: 5h, weekly",
        five_hour=LimitWindow(
            name="5h",
            remaining=0,
            reset_at=reference_at - timedelta(minutes=2),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=0,
            reset_at=reference_at - timedelta(minutes=1),
        ),
    )

    expired = expire_reset_windows(usage, reference_at=reference_at)

    assert expired.status == AccountStatus.PARTIAL
    assert expired.blocked_until is None
    assert expired.blocked_reason is None
    assert expired.error == "cached limit window expired: 5h, weekly; refresh required"
    assert expired.five_hour is None
    assert expired.weekly is None
    assert expired.stale is True


def test_load_usage_snapshot_ignores_invalid_json(tmp_path):
    (tmp_path / "privat.json").write_text("{not-json", encoding="utf-8")

    assert load_usage_snapshot("privat", tmp_path) is None


def test_remove_account_state_deletes_current_snapshot_and_debug(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime.now(UTC),
        five_hour=LimitWindow(name="5h", remaining=12),
        weekly=LimitWindow(name="weekly", remaining=34),
    )
    save_current_usage(usage)
    save_usage_snapshot(usage)
    debug_dir = tmp_path / "data" / "codex-usage" / "debug"
    debug_dir.mkdir(parents=True, mode=0o700)
    (debug_dir / "privat-last-ingest.json").write_text("{}", encoding="utf-8")

    remove_account_state("privat")

    assert not (tmp_path / "data" / "codex-usage" / "current" / "privat.json").exists()
    assert not (tmp_path / "data" / "codex-usage" / "snapshots" / "privat.json").exists()
    assert not (debug_dir / "privat-last-ingest.json").exists()


def test_stale_state_generation_cannot_recreate_removed_account_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    captured_at = datetime(2026, 7, 12, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    generation = load_state_generation("race")
    usage = AccountUsage(
        account_id="race",
        label="Race",
        captured_at=captured_at,
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
        state_generation=generation,
    )
    save_current_usage(usage)
    save_usage_snapshot(usage)

    remove_account_state("race")

    assert load_state_generation("race") == generation + 1
    save_current_usage(usage)
    save_usage_snapshot(usage)
    assert load_current_usage("race") is None
    assert load_usage_snapshot("race") is None

    fresh = replace(
        usage,
        captured_at=captured_at + timedelta(minutes=5),
        state_generation=load_state_generation("race"),
    )
    save_current_usage(fresh)
    save_usage_snapshot(fresh)
    assert load_current_usage("race") == fresh
    assert load_usage_snapshot("race") == fresh


@pytest.mark.parametrize("malformed_window", ([], "not-an-object", 42))
def test_load_usage_snapshot_ignores_malformed_window_shape(tmp_path, malformed_window):
    payload = {
        "account": "partial",
        "label": "Partial",
        "captured_at": "2026-06-08T04:20:00+02:00",
        "status": "partial",
        "five_hour": malformed_window,
        "weekly": {"name": "weekly", "remaining": 55},
    }
    (tmp_path / "partial.json").write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_usage_snapshot("partial", tmp_path)

    assert loaded is not None
    assert loaded.five_hour is None
    assert loaded.weekly is not None
    assert loaded.weekly.remaining == 55


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


def test_save_usage_snapshot_rejects_symlinked_data_home(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    data_home = tmp_path / "data-home"
    data_home.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    with pytest.raises(ValueError, match="symlink ancestors"):
        save_usage_snapshot(usage)

    assert not (outside / "codex-usage").exists()


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


def test_load_legacy_snapshot_localizes_naive_datetimes(tmp_path):
    payload = {
        "account": "legacy",
        "label": "Legacy",
        "captured_at": "2099-06-08T04:20:00",
        "status": "blocked",
        "blocked_until": "2099-06-08T06:50:00",
        "five_hour": {
            "name": "5h",
            "remaining": 0,
            "reset_at": "2099-06-08T05:05:00",
        },
        "auth_last_refresh": "2099-06-07T23:17:00",
    }
    (tmp_path / "legacy.json").write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_usage_snapshot("legacy", tmp_path)

    assert loaded is not None
    assert loaded.captured_at.tzinfo is not None
    assert loaded.captured_at.utcoffset() is not None
    assert loaded.blocked_until is not None
    assert loaded.blocked_until.tzinfo is not None
    assert loaded.five_hour is not None
    assert loaded.five_hour.reset_at is not None
    assert loaded.five_hour.reset_at.tzinfo is not None
    assert loaded.auth_last_refresh is not None
    assert loaded.auth_last_refresh.tzinfo is not None


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


def test_merge_current_with_last_success_fills_missing_window():
    captured = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        status=AccountStatus.PARTIAL,
        five_hour=LimitWindow(name="5h", remaining=97),
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        weekly=LimitWindow(name="weekly", remaining=55),
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged.five_hour == current.five_hour
    assert merged.weekly == last_success.weekly
    assert merged.values_captured_at == captured
    assert merged.stale is True


def test_merge_drops_cached_windows_after_their_reset():
    timezone = ZoneInfo("Europe/Berlin")
    captured = datetime(2026, 6, 8, 16, 0, tzinfo=timezone)
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        status=AccountStatus.PARTIAL,
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 15, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            remaining=97,
            reset_at=datetime(2026, 6, 8, 15, 30, tzinfo=timezone),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=55,
            reset_at=datetime(2026, 6, 9, 15, 30, tzinfo=timezone),
        ),
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged.five_hour is None
    assert merged.weekly is not None
    assert merged.weekly.remaining == 55
    assert merged.values_captured_at == last_success.captured_at
    assert merged.stale is True


def test_authoritative_empty_direct_limits_do_not_restore_old_values():
    captured = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        status=AccountStatus.PARTIAL,
        backend_used="direct",
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged == current


@pytest.mark.parametrize("backend", ("direct", "app-server"))
def test_partial_authenticated_limits_do_not_restore_missing_window(backend):
    captured = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        weekly=LimitWindow(name="weekly", remaining=55),
        status=AccountStatus.PARTIAL,
        backend_used=backend,
        backend_user_id="user-privat",
        backend_account_id="account-privat",
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=70),
        backend_used=backend,
        backend_user_id="user-privat",
        backend_account_id="account-privat",
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged.five_hour is None
    assert merged.weekly == current.weekly
    assert merged.stale is False


def test_merge_rejects_identified_current_data_from_unknown_cached_account():
    captured = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        status=AccountStatus.PARTIAL,
        backend_used="direct",
        backend_user_id="user-current",
        backend_account_id="account-current",
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged.five_hour is None
    assert merged.weekly is None
    assert merged.stale is False


def test_merge_rejects_different_identified_backend_account():
    captured = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        status=AccountStatus.PARTIAL,
        backend_user_id="user-shared",
        backend_account_id="account-current",
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
        backend_user_id="user-shared",
        backend_account_id="account-other",
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged.five_hour is None
    assert merged.weekly is None


def test_merge_accepts_same_account_id_when_backend_user_id_format_differs():
    captured = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        status=AccountStatus.PARTIAL,
        backend_user_id="api-user",
        backend_account_id="account-uuid",
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
        backend_user_id=None,
        backend_account_id="account-uuid",
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged.five_hour == last_success.five_hour
    assert merged.weekly == last_success.weekly


def test_merge_rejects_same_account_id_with_conflicting_backend_users():
    captured = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        status=AccountStatus.PARTIAL,
        backend_user_id="user-current",
        backend_account_id="account-shared",
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
        backend_user_id="user-previous",
        backend_account_id="account-shared",
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged == current


def test_merge_current_with_last_success_preserves_usage_under_reset_only_window():
    timezone = ZoneInfo("Europe/Berlin")
    captured = datetime(2026, 6, 8, 4, 20, tzinfo=timezone)
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        status=AccountStatus.PARTIAL,
        five_hour=LimitWindow(
            name="5h",
            reset_at=datetime(2026, 6, 8, 16, 0, tzinfo=timezone),
        ),
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        five_hour=LimitWindow(
            name="5h",
            remaining=97,
            reset_at=datetime(2026, 6, 8, 15, 0, tzinfo=timezone),
        ),
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged.five_hour is not None
    assert merged.five_hour.remaining == 97
    assert merged.five_hour.reset_at == datetime(2026, 6, 8, 16, 0, tzinfo=timezone)
    assert merged.values_captured_at == captured
    assert merged.stale is True


def test_merge_does_not_reuse_reset_from_a_different_window_duration():
    timezone = ZoneInfo("Europe/Berlin")
    captured = datetime(2026, 7, 12, 4, 20, tzinfo=timezone)
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        status=AccountStatus.PARTIAL,
        backend_used="direct",
        five_hour=LimitWindow(
            name="5h",
            used=20,
            limit=100,
            remaining=80,
            raw=(
                '$.rate_limit.primary_window {"used_percent": 20, '
                '"limit_window_seconds": 18000}'
            ),
        ),
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured - timedelta(minutes=5),
        five_hour=LimitWindow(
            name="5h",
            used=5,
            limit=100,
            remaining=95,
            reset_at=captured + timedelta(days=30),
            raw=(
                '$.rate_limit.primary_window {"used_percent": 5, '
                '"limit_window_seconds": 2592000}'
            ),
        ),
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged.five_hour == current.five_hour
    assert merged.five_hour is not None
    assert merged.five_hour.reset_at is None
    assert merged.stale is False


def test_merge_rejects_known_window_kind_against_unknown_current_kind():
    timezone = ZoneInfo("Europe/Berlin")
    captured = datetime(2026, 7, 12, 4, 20, tzinfo=timezone)
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        status=AccountStatus.PARTIAL,
        five_hour=LimitWindow(
            name="",
            reset_at=captured + timedelta(days=7),
        ),
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured - timedelta(minutes=5),
        five_hour=LimitWindow(
            name="5h",
            remaining=95,
            reset_at=captured + timedelta(hours=5),
            raw='{"limit_window_seconds": 18000}',
        ),
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged is current
    assert merged.five_hour is not None
    assert merged.five_hour.remaining is None


def test_merge_does_not_restore_known_other_duration_into_browser_window():
    timezone = ZoneInfo("Europe/Berlin")
    captured = datetime(2026, 7, 12, 4, 20, tzinfo=timezone)
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        status=AccountStatus.PARTIAL,
        backend_used="browser",
        backend_user_id="user-privat",
        backend_account_id="account-privat",
        five_hour=LimitWindow(
            name="5h",
            reset_at=captured + timedelta(hours=5),
            source="dom-text",
        ),
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured - timedelta(minutes=5),
        status=AccountStatus.OK,
        backend_used="direct",
        backend_user_id="user-privat",
        backend_account_id="account-privat",
        five_hour=LimitWindow(
            name="5h",
            used=5,
            limit=100,
            remaining=95,
            percent=95,
            reset_at=captured + timedelta(days=30),
            raw='{"limit_window_seconds": 2592000}',
            source="json",
        ),
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged is current
    assert merged.five_hour == current.five_hour
    assert merged.five_hour is not None
    assert merged.five_hour.used is None
    assert merged.five_hour.reset_at == captured + timedelta(hours=5)


def test_merge_current_usage_does_not_restore_expired_reset_time():
    timezone = ZoneInfo("Europe/Berlin")
    captured = datetime(2026, 6, 8, 16, 0, tzinfo=timezone)
    current_window = LimitWindow(name="5h", remaining=80)
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        status=AccountStatus.PARTIAL,
        five_hour=current_window,
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 15, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            remaining=97,
            reset_at=datetime(2026, 6, 8, 15, 30, tzinfo=timezone),
        ),
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged.five_hour == current_window
    assert merged.five_hour.reset_at is None
    assert merged.stale is False


def test_save_usage_snapshot_preserves_values_when_partial_snapshot_arrives(tmp_path):
    timezone = ZoneInfo("Europe/Berlin")
    snapshot_dir = tmp_path / "snapshots"
    previous_capture = datetime(2026, 6, 8, 4, 20, tzinfo=timezone)
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=previous_capture,
            five_hour=LimitWindow(
                name="5h",
                remaining=97,
                reset_at=datetime(2026, 6, 8, 16, 0, tzinfo=timezone),
            ),
            weekly=LimitWindow(
                name="weekly",
                remaining=55,
                reset_at=datetime(2026, 6, 14, 16, 0, tzinfo=timezone),
            ),
        ),
        snapshot_dir,
    )

    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime(2026, 6, 8, 4, 25, tzinfo=timezone),
            status=AccountStatus.PARTIAL,
            five_hour=LimitWindow(
                name="5h",
                reset_at=datetime(2026, 6, 8, 16, 5, tzinfo=timezone),
            ),
            error="usage limits not found",
        ),
        snapshot_dir,
    )

    loaded = load_usage_snapshot("privat", snapshot_dir)

    assert loaded is not None
    assert loaded.status == AccountStatus.PARTIAL
    assert loaded.stale is True
    assert loaded.five_hour is not None
    assert loaded.five_hour.remaining == 97
    assert loaded.five_hour.reset_at == datetime(2026, 6, 8, 16, 5, tzinfo=timezone)
    assert loaded.weekly is not None
    assert loaded.weekly.remaining == 55
    assert loaded.captured_at == datetime(2026, 6, 8, 4, 25, tzinfo=timezone)
    assert loaded.values_captured_at == previous_capture


def test_save_usage_snapshot_preserves_reset_when_usage_arrives_without_reset(tmp_path):
    timezone = ZoneInfo("Europe/Berlin")
    snapshot_dir = tmp_path / "snapshots"
    previous_reset = datetime(2026, 6, 8, 16, 0, tzinfo=timezone)
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=timezone),
            five_hour=LimitWindow(name="5h", remaining=97, reset_at=previous_reset),
        ),
        snapshot_dir,
    )

    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime(2026, 6, 8, 4, 25, tzinfo=timezone),
            status=AccountStatus.PARTIAL,
            five_hour=LimitWindow(name="5h", remaining=80),
            error="reset time missing",
        ),
        snapshot_dir,
    )

    loaded = load_usage_snapshot("privat", snapshot_dir)

    assert loaded is not None
    assert loaded.five_hour is not None
    assert loaded.five_hour.remaining == 80
    assert loaded.five_hour.reset_at == previous_reset
    assert loaded.stale is True


def test_merge_current_with_newer_success_prefers_success_snapshot():
    timezone = ZoneInfo("Europe/Berlin")
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 7, 11, 2, 0, tzinfo=timezone),
        status=AccountStatus.PARTIAL,
        five_hour=LimitWindow(name="5h", remaining=1),
        weekly=LimitWindow(name="weekly", remaining=2),
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 7, 11, 3, 0, tzinfo=timezone),
        status=AccountStatus.OK,
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged == last_success


def test_merge_current_with_newer_partial_snapshot_keeps_current_windows():
    timezone = ZoneInfo("Europe/Berlin")
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 7, 11, 2, 0, tzinfo=timezone),
        status=AccountStatus.PARTIAL,
        backend_used="browser",
        five_hour=LimitWindow(name="5h", remaining=42),
    )
    last_success = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 7, 11, 3, 0, tzinfo=timezone),
        status=AccountStatus.PARTIAL,
        backend_used="browser",
        weekly=LimitWindow(name="weekly", remaining=61),
    )

    merged = merge_current_with_last_success(current, last_success)

    assert merged.captured_at == last_success.captured_at
    assert merged.five_hour == current.five_hour
    assert merged.weekly == last_success.weekly
    assert merged.values_captured_at == current.captured_at
    assert merged.stale is True


@pytest.mark.parametrize("backend", ("direct", "app-server"))
def test_merge_newer_authenticated_partial_does_not_restore_missing_windows(backend):
    timezone = ZoneInfo("Europe/Berlin")
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 7, 11, 2, 0, tzinfo=timezone),
        status=AccountStatus.OK,
        backend_used=backend,
        backend_user_id="user-privat",
        backend_account_id="account-privat",
        five_hour=LimitWindow(name="5h", remaining=42),
        weekly=LimitWindow(name="weekly", remaining=61),
    )
    newer = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 7, 11, 3, 0, tzinfo=timezone),
        status=AccountStatus.PARTIAL,
        error="weekly limit unavailable",
        backend_used=backend,
        backend_user_id="user-privat",
        backend_account_id="account-privat",
        weekly=LimitWindow(name="weekly", remaining=59),
    )

    merged = merge_current_with_last_success(current, newer)

    assert merged == newer


def test_save_current_usage_does_not_overwrite_newer_capture(tmp_path):
    current_dir = tmp_path / "current"
    newer = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 5, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )
    older = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 6, 8, 4, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    save_current_usage(newer, current_dir)
    save_current_usage(older, current_dir)

    loaded = load_current_usage("privat", current_dir)
    assert loaded is not None
    assert loaded.captured_at == newer.captured_at


def test_save_current_usage_normalizes_naive_capture_before_order_check(tmp_path):
    current_dir = tmp_path / "current"
    newer = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2099, 1, 2, 12, tzinfo=UTC),
        weekly=LimitWindow(name="weekly", remaining=55),
    )
    older_naive = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2099, 1, 1, 0),
    )

    save_current_usage(newer, current_dir)
    save_current_usage(older_naive, current_dir)

    loaded = load_current_usage("privat", current_dir)
    assert loaded is not None
    assert loaded.captured_at == newer.captured_at
    assert loaded.weekly == newer.weekly


def test_concurrent_current_writes_keep_the_newest_capture(tmp_path):
    current_dir = tmp_path / "current"
    captures = [
        datetime(2026, 6, 8, hour, tzinfo=ZoneInfo("Europe/Berlin"))
        for hour in (1, 2, 3, 4, 5)
    ]

    def save(captured_at):
        save_current_usage(
            AccountUsage(account_id="privat", label="Privat", captured_at=captured_at),
            current_dir,
        )

    with ThreadPoolExecutor(max_workers=len(captures)) as executor:
        list(executor.map(save, captures))

    loaded = load_current_usage("privat", current_dir)
    assert loaded is not None
    assert loaded.captured_at == max(captures)
