from __future__ import annotations

import signal
from contextlib import nullcontext
from datetime import datetime
from zoneinfo import ZoneInfo

from codex_usage.app_server import AppServerUnavailableError
from codex_usage.config import AppConfig
from codex_usage.models import Account, AccountStatus, AccountUsage, LimitWindow
from codex_usage.scheduler import fetch_all, watch, watchdog


def test_watch_backs_off_after_unexpected_cycle_error(monkeypatch, capsys):
    delays: list[int] = []
    health_events: list[tuple[str, str]] = []
    installed: list[tuple[int, object]] = []

    class StopAfterWait:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, delay):
            delays.append(delay)
            self.stopped = True
            return True

        def set(self):
            self.stopped = True

    monkeypatch.setattr("codex_usage.scheduler.Event", StopAfterWait)
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_all",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.record_health_event",
        lambda component, event, **kwargs: health_events.append((component, event)),
    )
    previous = {
        signal.SIGINT: "old-int",
        signal.SIGTERM: "old-term",
    }
    monkeypatch.setattr("codex_usage.scheduler.signal.getsignal", previous.__getitem__)
    monkeypatch.setattr(
        "codex_usage.scheduler.signal.signal",
        lambda signum, handler: installed.append((signum, handler)),
    )

    watch(AppConfig(accounts=()), (), output="table", interval_seconds=60)

    assert delays == [5]
    assert health_events == [("watch", "cycle_error")]
    assert "watch cycle failed" in capsys.readouterr().err
    assert [signum for signum, _ in installed] == [
        signal.SIGINT,
        signal.SIGTERM,
        signal.SIGINT,
        signal.SIGTERM,
    ]
    assert installed[2][1] == "old-int"
    assert installed[3][1] == "old-term"


def test_fetch_all_uses_direct_for_accounts_with_auth_and_browser_for_others(monkeypatch):
    accounts = (
        Account(
            id="direct",
            label="Direct",
            profile_dir="/tmp/direct",
            auth_json_path="/tmp/auth.json",
        ),
        Account(id="browser", label="Browser", profile_dir="/tmp/browser"),
    )
    now = datetime(2026, 6, 8, 4, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    direct_usage = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=now,
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
    )
    browser_usage = AccountUsage(
        account_id="browser",
        label="Browser",
        captured_at=now,
        five_hour=LimitWindow(name="5h", remaining=88),
        weekly=LimitWindow(name="weekly", remaining=44),
    )
    calls: list[tuple[str, str, object]] = []

    def fake_fetch_direct(account, *, auth_json_path=None):
        calls.append(("direct", account.id, auth_json_path))
        return direct_usage

    def fake_fetch_browser(account, config, *, headed):
        calls.append(("browser", account.id, headed))
        return browser_usage

    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_direct", fake_fetch_direct)
    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage", fake_fetch_browser)

    usages = fetch_all(AppConfig(accounts=accounts), accounts, headed=False, direct=False)

    assert sorted(calls) == sorted(
        [("direct", "direct", None), ("browser", "browser", False)]
    )
    assert [usage.account_id for usage in usages] == ["direct", "browser"]
    assert [usage.backend_used for usage in usages] == ["direct", "browser"]


def test_configured_app_server_without_auth_does_not_silently_use_browser(monkeypatch):
    account = Account(
        id="app-server",
        label="App Server",
        profile_dir="/tmp/app-server",
        backend="app-server",
    )
    usage = AccountUsage(
        account_id=account.id,
        label=account.label,
        captured_at=datetime(2026, 6, 8, 4, 30, tzinfo=ZoneInfo("Europe/Berlin")),
        status=AccountStatus.LOGIN_REQUIRED,
        backend_used="app-server",
    )

    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_account_usage_app_server",
        lambda selected: usage,
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_account_usage",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("browser used")),
    )
    monkeypatch.setattr("codex_usage.scheduler.account_lock", lambda account_id: nullcontext())

    result = fetch_all(AppConfig(accounts=(account,)), (account,))

    assert result == [usage]


def test_fetch_all_direct_saves_only_successful_snapshots(monkeypatch):
    accounts = (
        Account(id="ok", label="OK", profile_dir="/tmp/ok", auth_json_path="/tmp/ok-auth.json"),
        Account(
            id="broken",
            label="Broken",
            profile_dir="/tmp/broken",
            auth_json_path="/tmp/broken-auth.json",
        ),
    )
    captured_at = datetime(2026, 6, 8, 4, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    ok_usage = AccountUsage(
        account_id="ok",
        label="OK",
        captured_at=captured_at,
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
    )
    error_usage = AccountUsage(
        account_id="broken",
        label="Broken",
        captured_at=captured_at,
        status=AccountStatus.LOGIN_REQUIRED,
        error="direct auth failed",
    )
    by_account = {"ok": ok_usage, "broken": error_usage}
    saved: list[str] = []

    def fake_fetch_direct(account, *, auth_json_path=None):
        return by_account[account.id]

    def fake_save_usage_snapshot(usage):
        saved.append(usage.account_id)

    current: list[str] = []

    def fake_save_current_usage(usage):
        current.append(usage.account_id)

    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_direct", fake_fetch_direct)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", fake_save_usage_snapshot)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", fake_save_current_usage)

    usages = fetch_all(
        AppConfig(accounts=accounts),
        accounts,
        direct=True,
        save_snapshots=True,
    )

    assert [usage.account_id for usage in usages] == ["ok", "broken"]
    assert [usage.backend_used for usage in usages] == ["direct", "direct"]
    assert saved == ["ok"]
    assert sorted(current) == ["broken", "ok"]


def test_watchdog_skips_active_block_and_releases_after_reset(monkeypatch):
    accounts = (
        Account(id="blocked", label="Blocked", profile_dir="/tmp/blocked"),
        Account(id="ok", label="OK", profile_dir="/tmp/ok"),
    )
    now = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    blocked_snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=now,
        status=AccountStatus.BLOCKED,
        blocked_until=datetime(2099, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin")),
        blocked_reason="usage limit reached: weekly",
    )
    ok_usage = AccountUsage(
        account_id="ok",
        label="OK",
        captured_at=now,
        five_hour=LimitWindow(name="5h", remaining=97, reset_at=now),
        weekly=LimitWindow(name="weekly", remaining=55, reset_at=now),
    )
    fetched = [ok_usage]
    saved: list[str] = []
    seen_fetch_accounts: list[str] = []

    def fake_load_usage_snapshot(account_id, snapshot_dir=None):
        return blocked_snapshot if account_id == "blocked" else None

    def fake_fetch_all(
        config,
        fetch_accounts,
        *,
        headed,
        direct,
        backend_override,
        auth_json_path,
        save_snapshots,
    ):
        seen_fetch_accounts.extend(account.id for account in fetch_accounts)
        return fetched

    def fake_save_usage_snapshot(usage, snapshot_dir=None):
        saved.append(usage.account_id)
        return None

    monkeypatch.setattr("codex_usage.scheduler.load_usage_snapshot", fake_load_usage_snapshot)
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", fake_save_usage_snapshot)

    result = watchdog(
        AppConfig(accounts=accounts),
        accounts,
        output="table",
        direct=True,
    )

    assert seen_fetch_accounts == ["ok"]
    assert result[0].status == AccountStatus.BLOCKED
    assert result[0].blocked_until == blocked_snapshot.blocked_until
    assert result[1] == ok_usage
    assert saved == ["ok"]


def test_watchdog_blocks_exhausted_usage_and_persists_state(monkeypatch):
    accounts = (Account(id="blocked", label="Blocked", profile_dir="/tmp/blocked"),)
    now = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    exhausted_usage = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=now,
        five_hour=LimitWindow(
            name="5h",
            used=100,
            limit=100,
            remaining=0,
            percent=100,
            reset_at=datetime(2099, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
        weekly=LimitWindow(
            name="weekly",
            used=45,
            limit=1000,
            remaining=955,
            percent=4.5,
            reset_at=datetime(2099, 6, 10, 5, 5, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
    )
    saved: list[AccountUsage] = []

    def fake_load_usage_snapshot(account_id, snapshot_dir=None):
        return None

    def fake_fetch_all(
        config,
        fetch_accounts,
        *,
        headed,
        direct,
        backend_override,
        auth_json_path,
        save_snapshots,
    ):
        return [exhausted_usage]

    def fake_save_usage_snapshot(usage, snapshot_dir=None):
        saved.append(usage)
        return None

    monkeypatch.setattr("codex_usage.scheduler.load_usage_snapshot", fake_load_usage_snapshot)
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", fake_save_usage_snapshot)

    result = watchdog(
        AppConfig(accounts=accounts),
        accounts,
        output="json",
        direct=True,
    )

    assert result[0].status == AccountStatus.BLOCKED
    assert result[0].blocked_until == datetime(2099, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin"))
    assert result[0].blocked_reason is not None
    assert saved and saved[0].status == AccountStatus.BLOCKED


def test_window_exhaustion_percent_fallback_uses_remaining_semantics():
    from codex_usage.scheduler import _window_is_exhausted

    assert _window_is_exhausted(LimitWindow(name="5h", percent=0)) is True
    assert _window_is_exhausted(LimitWindow(name="5h", percent=100)) is False


def test_watchdog_blocks_until_latest_reset_when_multiple_windows_are_exhausted(monkeypatch):
    accounts = (Account(id="blocked", label="Blocked", profile_dir="/tmp/blocked"),)
    exhausted_usage = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        five_hour=LimitWindow(
            name="5h",
            used=100,
            limit=100,
            remaining=0,
            percent=100,
            reset_at=datetime(2099, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
        weekly=LimitWindow(
            name="weekly",
            used=1000,
            limit=1000,
            remaining=0,
            percent=100,
            reset_at=datetime(2099, 6, 10, 5, 5, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
    )

    def fake_load_usage_snapshot(account_id, snapshot_dir=None):
        return None

    def fake_fetch_all(
        config,
        fetch_accounts,
        *,
        headed,
        direct,
        backend_override,
        auth_json_path,
        save_snapshots,
    ):
        return [exhausted_usage]

    def fake_save_usage_snapshot(usage, snapshot_dir=None):
        return None

    monkeypatch.setattr("codex_usage.scheduler.load_usage_snapshot", fake_load_usage_snapshot)
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", fake_save_usage_snapshot)

    result = watchdog(
        AppConfig(accounts=accounts),
        accounts,
        output="table",
        direct=True,
    )

    assert result[0].status == AccountStatus.BLOCKED
    assert result[0].blocked_until == datetime(2099, 6, 10, 5, 5, tzinfo=ZoneInfo("Europe/Berlin"))


def test_app_server_falls_back_only_when_unavailable(monkeypatch):
    account = Account(
        id="work",
        label="Work",
        profile_dir="/tmp/work",
        auth_json_path="/tmp/work/auth.json",
        backend="app-server",
    )
    captured = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    direct_usage = AccountUsage(
        account_id="work",
        label="Work",
        captured_at=captured,
    )

    def unavailable(selected):
        raise AppServerUnavailableError("unsupported")

    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_app_server", unavailable)
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_account_usage_direct",
        lambda selected, auth_json_path=None: direct_usage,
    )
    monkeypatch.setattr("codex_usage.scheduler.account_lock", lambda account_id: nullcontext())

    result = fetch_all(AppConfig(accounts=(account,)), (account,))

    assert result[0].backend_used == "direct"
    assert result[0].fallback_reason == "unsupported"
