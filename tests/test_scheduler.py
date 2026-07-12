from __future__ import annotations

import signal
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from codex_usage.app_server import AppServerUnavailableError
from codex_usage.config import AppConfig
from codex_usage.models import Account, AccountStatus, AccountUsage, LimitWindow
from codex_usage.scheduler import (
    _is_more_conservative_direct_usage,
    _remaining_percent,
    fetch_all,
    watch,
    watchdog,
)


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


def test_watch_subtracts_successful_cycle_duration_from_interval(monkeypatch):
    delays: list[float] = []

    class StopAfterWait:
        def is_set(self):
            return False

        def wait(self, delay):
            delays.append(delay)
            return True

        def set(self):
            return None

    monotonic_values = iter((100.0, 112.5))
    monkeypatch.setattr("codex_usage.scheduler.Event", StopAfterWait)
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", lambda *args, **kwargs: [])
    monkeypatch.setattr("codex_usage.scheduler.time.monotonic", lambda: next(monotonic_values))

    watch(AppConfig(accounts=()), (), output="table", interval_seconds=60)

    assert delays == [47.5]


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


def test_fetch_all_serializes_authenticated_multi_account_polls(monkeypatch):
    accounts = (
        Account(
            id="first",
            label="First",
            profile_dir="/tmp/first",
            auth_json_path="/tmp/first-auth.json",
        ),
        Account(
            id="second",
            label="Second",
            profile_dir="/tmp/second",
            auth_json_path="/tmp/second-auth.json",
        ),
    )
    calls: list[str] = []
    locks: list[str] = []

    def fail_if_parallel(**_kwargs):
        raise AssertionError("authenticated account polls must be serialized")

    def fake_fetch_direct(account, *, auth_json_path=None):
        calls.append(account.id)
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=datetime(2026, 6, 8, 4, 30, tzinfo=ZoneInfo("Europe/Berlin")),
        )

    monkeypatch.setattr("codex_usage.scheduler.ThreadPoolExecutor", fail_if_parallel)
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_account_usage_direct", fake_fetch_direct
    )
    def fake_account_lock(account_id, **_kwargs):
        locks.append(account_id)
        return nullcontext()

    monkeypatch.setattr("codex_usage.scheduler.account_lock", fake_account_lock)

    result = fetch_all(AppConfig(accounts=accounts), accounts)

    assert [usage.account_id for usage in result] == ["first", "second"]
    assert calls == ["first", "second"]
    assert locks == ["__all_accounts__", "first", "second"]


def test_fetch_all_serializes_visible_browser_multi_account_polls(monkeypatch):
    accounts = (
        Account(id="first", label="First", profile_dir="/tmp/first"),
        Account(id="second", label="Second", profile_dir="/tmp/second"),
    )
    calls: list[tuple[str, bool]] = []
    locks: list[str] = []

    def fail_if_parallel(**_kwargs):
        raise AssertionError("visible browser account polls must be serialized")

    def fake_fetch_browser(account, _config, *, headed):
        calls.append((account.id, headed))
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=datetime(2026, 6, 8, 4, 30, tzinfo=ZoneInfo("Europe/Berlin")),
        )

    monkeypatch.setattr("codex_usage.scheduler.ThreadPoolExecutor", fail_if_parallel)
    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage", fake_fetch_browser)

    def fake_account_lock(account_id, **_kwargs):
        locks.append(account_id)
        return nullcontext()

    monkeypatch.setattr("codex_usage.scheduler.account_lock", fake_account_lock)

    result = fetch_all(AppConfig(accounts=accounts), accounts, headed=True)

    assert [usage.account_id for usage in result] == ["first", "second"]
    assert calls == [("first", True), ("second", True)]
    assert locks == ["__all_accounts__", "first", "second"]


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


def test_fetch_all_retains_direct_values_across_future_reset_jump(monkeypatch):
    account = Account(
        id="direct",
        label="Direct",
        profile_dir="/tmp/direct",
        auth_json_path="/tmp/direct-auth.json",
    )
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            used=9,
            limit=100,
            remaining=91,
            percent=91,
            reset_at=datetime(2026, 7, 12, 4, 40, 41, tzinfo=timezone),
        ),
        weekly=LimitWindow(
            name="weekly",
            used=11,
            limit=100,
            remaining=89,
            percent=89,
            reset_at=datetime(2026, 7, 18, 8, 2, 42, tzinfo=timezone),
        ),
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
        fallback_reason="previous direct limits retained after reset transition",
        stale=True,
    )
    current = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime(2026, 7, 12, 0, 1, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            used=1,
            limit=100,
            remaining=99,
            percent=99,
            reset_at=datetime(2026, 7, 12, 4, 41, 59, tzinfo=timezone),
        ),
        weekly=LimitWindow(
            name="weekly",
            used=1,
            limit=100,
            remaining=99,
            percent=99,
            reset_at=datetime(2026, 7, 18, 8, 30, 25, tzinfo=timezone),
        ),
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_account_usage_direct",
        lambda selected, *, auth_json_path=None: current,
    )
    monkeypatch.setattr("codex_usage.scheduler.load_usage_snapshot", lambda account_id: previous)

    result = fetch_all(AppConfig(accounts=(account,)), (account,))

    assert result[0].five_hour is not None
    assert result[0].five_hour.remaining == 91
    assert result[0].weekly is not None
    assert result[0].weekly.remaining == 89
    assert result[0].captured_at == current.captured_at
    assert result[0].stale is True


def test_fetch_all_stabilizes_app_server_against_direct_snapshot(monkeypatch):
    account = Account(
        id="account",
        label="Account",
        profile_dir="/tmp/account",
        auth_json_path="/tmp/account-auth.json",
    )
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            remaining=91,
            percent=91,
            reset_at=datetime(2026, 7, 12, 4, 40, 41, tzinfo=timezone),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=89,
            percent=89,
            reset_at=datetime(2026, 7, 18, 8, 2, 42, tzinfo=timezone),
        ),
        backend_used="direct",
        backend_user_id="user-account",
        backend_account_id="account-id",
    )
    current = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 7, 12, 0, 1, tzinfo=timezone),
        five_hour=LimitWindow(
            name="five_hour",
            remaining=99,
            percent=99,
            reset_at=datetime(2026, 7, 12, 4, 41, 59, tzinfo=timezone),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=99,
            percent=99,
            reset_at=datetime(2026, 7, 18, 8, 30, 25, tzinfo=timezone),
        ),
        backend_used="app-server",
        backend_user_id="user-account",
        backend_account_id="account-id",
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_account_usage_app_server",
        lambda selected: current,
    )
    monkeypatch.setattr("codex_usage.scheduler.load_usage_snapshot", lambda account_id: previous)

    result = fetch_all(
        AppConfig(accounts=(account,)),
        (account,),
        backend_override="app-server",
    )

    assert result[0].backend_used == "app-server"
    assert result[0].five_hour is not None
    assert result[0].five_hour.remaining == 91
    assert result[0].weekly is not None
    assert result[0].weekly.remaining == 89
    assert result[0].stale is True


def test_fetch_all_reuses_direct_reset_fallback_on_next_poll(monkeypatch):
    account = Account(
        id="direct",
        label="Direct",
        profile_dir="/tmp/direct",
        auth_json_path="/tmp/direct-auth.json",
    )
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            remaining=91,
            percent=91,
            reset_at=datetime(2026, 7, 12, 4, 40, 41, tzinfo=timezone),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=89,
            percent=89,
            reset_at=datetime(2026, 7, 18, 8, 2, 42, tzinfo=timezone),
        ),
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
    )
    inconsistent = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime(2026, 7, 12, 0, 1, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            remaining=99,
            percent=99,
            reset_at=datetime(2026, 7, 12, 4, 41, 59, tzinfo=timezone),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=99,
            percent=99,
            reset_at=datetime(2026, 7, 18, 8, 30, 25, tzinfo=timezone),
        ),
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
    )
    snapshots = [previous]
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_account_usage_direct",
        lambda selected, *, auth_json_path=None: inconsistent,
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id: snapshots[0],
    )

    first = fetch_all(AppConfig(accounts=(account,)), (account,))[0]
    snapshots[0] = first
    second = fetch_all(AppConfig(accounts=(account,)), (account,))[0]

    assert first.stale is True
    assert second.stale is True
    assert second.five_hour is not None
    assert second.five_hour.remaining == 91
    assert second.weekly is not None
    assert second.weekly.remaining == 89


def test_fetch_all_accepts_more_conservative_direct_reset_transition(monkeypatch):
    account = Account(
        id="direct",
        label="Direct",
        profile_dir="/tmp/direct",
        auth_json_path="/tmp/direct-auth.json",
    )
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            remaining=99,
            percent=99,
            reset_at=datetime(2026, 7, 12, 4, 41, 59, tzinfo=timezone),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=99,
            percent=99,
            reset_at=datetime(2026, 7, 18, 8, 30, 25, tzinfo=timezone),
        ),
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
    )
    current = replace(
        previous,
        captured_at=datetime(2026, 7, 12, 0, 1, tzinfo=timezone),
        five_hour=replace(
            previous.five_hour,
            remaining=91,
            percent=91,
            reset_at=datetime(2026, 7, 12, 4, 40, 41, tzinfo=timezone),
        ),
        weekly=replace(
            previous.weekly,
            remaining=89,
            percent=89,
            reset_at=datetime(2026, 7, 18, 8, 2, 42, tzinfo=timezone),
        ),
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_account_usage_direct",
        lambda selected, *, auth_json_path=None: current,
    )
    monkeypatch.setattr("codex_usage.scheduler.load_usage_snapshot", lambda account_id: previous)

    result = fetch_all(AppConfig(accounts=(account,)), (account,))

    assert result[0].five_hour is not None
    assert result[0].five_hour.remaining == 91
    assert result[0].stale is False


def test_direct_reset_guard_rejects_earlier_reset_with_more_remaining():
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            remaining=91,
            percent=91,
            reset_at=datetime(2026, 7, 12, 4, 40, 41, tzinfo=timezone),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=89,
            percent=89,
            reset_at=datetime(2026, 7, 18, 8, 2, 42, tzinfo=timezone),
        ),
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
    )
    current = replace(
        previous,
        five_hour=replace(
            previous.five_hour,
            remaining=100,
            percent=100,
            reset_at=datetime(2026, 7, 12, 4, 39, 41, tzinfo=timezone),
        ),
    )

    assert _is_more_conservative_direct_usage(current, previous) is False


def test_scheduler_remaining_percent_prefers_absolute_usage_values():
    window = LimitWindow(
        name="5h",
        used=8,
        limit=40,
        remaining=32,
        percent=20,
    )

    assert _remaining_percent(window) == 80


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


def test_watchdog_refetches_block_after_auth_identity_changes(tmp_path, monkeypatch):
    account = Account(
        id="blocked",
        label="Blocked",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )
    blocked_snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        status=AccountStatus.BLOCKED,
        blocked_until=datetime(2099, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_user_id="user-old",
        backend_account_id="account-old",
    )
    fresh_usage = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        five_hour=LimitWindow(name="5h", remaining=99),
        weekly=LimitWindow(name="weekly", remaining=95),
        backend_user_id="user-new",
        backend_account_id="account-new",
    )
    seen_fetch_accounts: list[str] = []

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
        return [fresh_usage]

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: blocked_snapshot,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_for_account",
        lambda selected: ("user-new", "account-new"),
    )
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
        direct=True,
    )

    assert seen_fetch_accounts == ["blocked"]
    assert result == [fresh_usage]


def test_watchdog_override_auth_identity_releases_old_block(tmp_path, monkeypatch):
    account = Account(
        id="blocked",
        label="Blocked",
        profile_dir=str(tmp_path / "profile"),
    )
    blocked_snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        status=AccountStatus.BLOCKED,
        blocked_until=datetime(2099, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_account_id="account-old",
    )
    fresh_usage = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        backend_account_id="account-new",
    )
    seen_fetch_accounts: list[str] = []

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
        return [fresh_usage]

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: blocked_snapshot,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_from_file",
        lambda path: ("user-new", "account-new"),
    )
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
        direct=True,
        auth_json_path=tmp_path / "override-auth.json",
    )

    assert seen_fetch_accounts == ["blocked"]
    assert result == [fresh_usage]


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


def test_watchdog_does_not_block_when_reset_expires_during_fetch(monkeypatch):
    account = Account(id="free", label="Free", profile_dir="/tmp/free")
    before_fetch = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    after_fetch = datetime(2026, 6, 8, 4, 21, tzinfo=ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="free",
        label="Free",
        captured_at=after_fetch,
        five_hour=LimitWindow(
            name="5h",
            remaining=0,
            reset_at=datetime(2026, 6, 8, 4, 20, 30, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
        weekly=LimitWindow(name="weekly", remaining=99),
    )
    clock_values = iter((before_fetch, after_fetch))

    class Clock:
        @classmethod
        def now(cls):
            return next(clock_values)

    monkeypatch.setattr("codex_usage.scheduler.datetime", Clock)
    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: None,
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_all",
        lambda *args, **kwargs: [usage],
    )
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
    )

    assert result[0].status == AccountStatus.OK
    assert result[0].blocked_until is None


def test_watchdog_refetches_blocked_account_when_reset_expires_during_other_fetch(
    monkeypatch,
):
    accounts = (
        Account(id="blocked", label="Blocked", profile_dir="/tmp/blocked"),
        Account(id="free", label="Free", profile_dir="/tmp/free"),
    )
    blocked_snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime(2026, 6, 8, 4, 19, tzinfo=ZoneInfo("Europe/Berlin")),
        status=AccountStatus.BLOCKED,
        blocked_until=datetime(2026, 6, 8, 4, 20, 30, tzinfo=ZoneInfo("Europe/Berlin")),
    )
    refreshed_blocked = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime(2026, 6, 8, 4, 21, tzinfo=ZoneInfo("Europe/Berlin")),
        five_hour=LimitWindow(name="5h", remaining=99),
        weekly=LimitWindow(name="weekly", remaining=98),
    )
    free_usage = AccountUsage(
        account_id="free",
        label="Free",
        captured_at=datetime(2026, 6, 8, 4, 21, tzinfo=ZoneInfo("Europe/Berlin")),
    )
    clock_values = iter(
        (
            datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
            datetime(2026, 6, 8, 4, 21, tzinfo=ZoneInfo("Europe/Berlin")),
            datetime(2026, 6, 8, 4, 21, 5, tzinfo=ZoneInfo("Europe/Berlin")),
        )
    )
    seen_fetch_accounts: list[list[str]] = []

    class Clock:
        @classmethod
        def now(cls):
            return next(clock_values)

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
        selected = [account.id for account in fetch_accounts]
        seen_fetch_accounts.append(selected)
        if selected == ["free"]:
            return [free_usage]
        assert selected == ["blocked"]
        return [refreshed_blocked]

    monkeypatch.setattr("codex_usage.scheduler.datetime", Clock)
    monkeypatch.setattr("codex_usage.scheduler.load_usage_snapshot", fake_load_usage_snapshot)
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    result = watchdog(
        AppConfig(accounts=accounts),
        accounts,
        output="json",
    )

    assert seen_fetch_accounts == [["free"], ["blocked"]]
    assert [usage.account_id for usage in result] == ["blocked", "free"]
    assert result[0] == refreshed_blocked
    assert result[0].status == AccountStatus.OK


def test_window_exhaustion_percent_fallback_uses_remaining_semantics():
    from codex_usage.scheduler import _window_is_exhausted

    assert _window_is_exhausted(LimitWindow(name="5h", percent=0)) is True
    assert _window_is_exhausted(LimitWindow(name="5h", percent=100)) is False


def test_window_exhaustion_prefers_remaining_over_usage_percent():
    from codex_usage.scheduler import _window_is_exhausted

    assert _window_is_exhausted(
        LimitWindow(name="5h", used=0, limit=100, remaining=100, percent=0)
    ) is False


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
