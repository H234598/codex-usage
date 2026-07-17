from __future__ import annotations

import signal
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from codex_usage.app_server import AppServerUnavailableError
from codex_usage.config import AppConfig
from codex_usage.models import Account, AccountStatus, AccountUsage, LimitWindow, UsagePool
from codex_usage.scheduler import (
    _ambiguous_direct_accounts,
    _apply_watchdog_block,
    _fetch_one,
    _is_more_conservative_direct_usage,
    _raw_number,
    _remaining_percent,
    _stabilize_authenticated_usage,
    _usage_map_for_accounts,
    _watch_cycle_is_healthy,
    _window_duration_seconds,
    _window_is_exhausted,
    fetch_all,
    watch,
    watchdog,
)


def test_ambiguous_direct_accounts_detects_shared_users_with_distinct_accounts(
    monkeypatch,
):
    accounts = [
        Account(
            id="privat",
            label="Privat",
            profile_dir="/tmp/privat",
            auth_json_path="/tmp/privat-auth.json",
        ),
        Account(
            id="work",
            label="Work",
            profile_dir="/tmp/work",
            auth_json_path="/tmp/work-auth.json",
        ),
    ]
    identities = {
        "privat": ("shared-user", "free-account"),
        "work": ("shared-user", "enterprise-account"),
    }
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_for_account",
        lambda account: identities[account.id],
    )

    assert _ambiguous_direct_accounts(accounts) == frozenset({"privat", "work"})


def test_ambiguous_direct_accounts_allows_local_aliases_of_same_account(monkeypatch):
    accounts = [
        Account(
            id="primary",
            label="Primary",
            profile_dir="/tmp/primary",
            auth_json_path="/tmp/primary-auth.json",
        ),
        Account(
            id="alias",
            label="Alias",
            profile_dir="/tmp/alias",
            auth_json_path="/tmp/alias-auth.json",
        ),
    ]
    identities = {
        "primary": ("shared-user", "same-account"),
        "alias": ("shared-user", "same-account"),
    }
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_for_account",
        lambda account: identities[account.id],
    )

    assert _ambiguous_direct_accounts(accounts) == frozenset()


def test_ambiguous_direct_accounts_allows_shared_users_with_distinct_plans(
    monkeypatch,
):
    accounts = [
        Account(
            id="privat",
            label="Privat",
            profile_dir="/tmp/privat",
            auth_json_path="/tmp/privat-auth.json",
        ),
        Account(
            id="work",
            label="Work",
            profile_dir="/tmp/work",
            auth_json_path="/tmp/work-auth.json",
        ),
    ]
    identities = {
        "privat": ("shared-user", "free-account"),
        "work": ("shared-user", "enterprise-account"),
    }
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_for_account",
        lambda account: identities[account.id],
    )
    plans = {"privat": "free", "work": "enterprise"}
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_plan_type_for_account",
        lambda account: plans[account.id],
    )

    assert _ambiguous_direct_accounts(accounts) == frozenset()


def test_ambiguous_direct_accounts_normalizes_plan_aliases(monkeypatch):
    accounts = [
        Account(
            id="pro",
            label="Pro",
            profile_dir="/tmp/pro",
            auth_json_path="/tmp/pro-auth.json",
        ),
        Account(
            id="plus",
            label="Plus",
            profile_dir="/tmp/plus",
            auth_json_path="/tmp/plus-auth.json",
        ),
    ]
    identities = {
        "pro": ("shared-user", "pro-account"),
        "plus": ("shared-user", "plus-account"),
    }
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_for_account",
        lambda account: identities[account.id],
    )
    plans = {"pro": "pro", "plus": "plus"}
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_plan_type_for_account",
        lambda account: plans[account.id],
    )

    assert _ambiguous_direct_accounts(accounts) == frozenset({"pro", "plus"})


def test_ambiguous_direct_accounts_rejects_missing_account_id_for_shared_user(
    monkeypatch,
):
    accounts = [
        Account(
            id="privat",
            label="Privat",
            profile_dir="/tmp/privat",
            auth_json_path="/tmp/privat-auth.json",
        ),
        Account(
            id="work",
            label="Work",
            profile_dir="/tmp/work",
            auth_json_path="/tmp/work-auth.json",
        ),
    ]
    identities = {
        "privat": ("shared-user", None),
        "work": ("shared-user", "work-account"),
    }
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_for_account",
        lambda account: identities[account.id],
    )

    assert _ambiguous_direct_accounts(accounts) == frozenset({"privat", "work"})


def test_fetch_all_passes_ambiguous_identity_guard_to_direct_reader(monkeypatch):
    accounts = (
        Account(
            id="privat",
            label="Privat",
            profile_dir="/tmp/privat",
            auth_json_path="/tmp/privat-auth.json",
        ),
        Account(
            id="work",
            label="Work",
            profile_dir="/tmp/work",
            auth_json_path="/tmp/work-auth.json",
        ),
    )
    flags: dict[str, bool] = {}
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_for_account",
        lambda account: ("shared-user", f"{account.id}-account"),
    )

    def fake_fetch_direct(
        account,
        *,
        auth_json_path=None,
        reject_ambiguous_backend_identity=False,
    ):
        flags[account.id] = reject_ambiguous_backend_identity
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=datetime.now().astimezone(),
        )

    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_direct", fake_fetch_direct)

    fetch_all(AppConfig(accounts=accounts), accounts)

    assert flags == {"privat": True, "work": True}


def test_fetch_all_keeps_ambiguity_guard_for_selected_account(monkeypatch):
    accounts = (
        Account(
            id="privat",
            label="Privat",
            profile_dir="/tmp/privat",
            auth_json_path="/tmp/privat-auth.json",
        ),
        Account(
            id="work",
            label="Work",
            profile_dir="/tmp/work",
            auth_json_path="/tmp/work-auth.json",
        ),
    )
    flags: dict[str, bool] = {}
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_for_account",
        lambda account: ("shared-user", f"{account.id}-account"),
    )

    def fake_fetch_direct(
        account,
        *,
        auth_json_path=None,
        reject_ambiguous_backend_identity=False,
    ):
        flags[account.id] = reject_ambiguous_backend_identity
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=datetime.now().astimezone(),
        )

    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_direct", fake_fetch_direct)

    fetch_all(AppConfig(accounts=accounts), (accounts[0],))

    assert flags == {"privat": True}


def test_fetch_all_marks_unidentified_auth_accounts_ambiguous(monkeypatch):
    accounts = (
        Account(
            id="privat",
            label="Privat",
            profile_dir="/tmp/privat",
            auth_json_path="/tmp/privat-auth.json",
        ),
        Account(
            id="work",
            label="Work",
            profile_dir="/tmp/work",
            auth_json_path="/tmp/work-auth.json",
        ),
    )
    flags: dict[str, bool] = {}
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_for_account",
        lambda _account: (None, None),
    )

    def fake_fetch_direct(
        account,
        *,
        auth_json_path=None,
        reject_ambiguous_backend_identity=False,
    ):
        flags[account.id] = reject_ambiguous_backend_identity
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=datetime.now().astimezone(),
        )

    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_direct", fake_fetch_direct)

    fetch_all(AppConfig(accounts=accounts), accounts)

    assert flags == {"privat": True, "work": True}


def test_ambiguous_direct_accounts_rejects_shared_default_auth_source():
    accounts = [
        Account(id="privat", label="Privat", profile_dir="/tmp/privat"),
        Account(id="work", label="Work", profile_dir="/tmp/work"),
    ]

    assert _ambiguous_direct_accounts(accounts) == frozenset({"privat", "work"})


def test_fetch_all_rejects_shared_default_auth_source(monkeypatch):
    accounts = (
        Account(id="privat", label="Privat", profile_dir="/tmp/privat"),
        Account(id="work", label="Work", profile_dir="/tmp/work"),
    )
    flags: dict[str, bool] = {}

    monkeypatch.setattr("codex_usage.scheduler.load_state_generation", lambda _account_id: 0)

    def fake_fetch_direct(
        account,
        *,
        auth_json_path=None,
        reject_ambiguous_backend_identity=False,
    ):
        flags[account.id] = reject_ambiguous_backend_identity
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=datetime.now().astimezone(),
            five_hour=LimitWindow(name="5h", remaining=97),
            weekly=LimitWindow(name="weekly", remaining=55),
        )

    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_direct", fake_fetch_direct)

    result = fetch_all(AppConfig(accounts=accounts), accounts, direct=True)

    assert flags == {"privat": True, "work": True}
    assert [usage.status for usage in result] == [AccountStatus.ERROR, AccountStatus.ERROR]
    assert all(usage.five_hour is None for usage in result)
    assert all(usage.weekly is None for usage in result)
    assert all(usage.cache_invalidated for usage in result)
    assert all(
        usage.error == "direct auth source cannot be attributed to one account"
        for usage in result
    )


def test_fetch_all_allows_single_default_auth_source(monkeypatch):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/privat")
    flags: list[bool] = []

    monkeypatch.setattr("codex_usage.scheduler.load_state_generation", lambda _account_id: 0)

    def fake_fetch_direct(
        selected,
        *,
        auth_json_path=None,
        reject_ambiguous_backend_identity=False,
    ):
        flags.append(reject_ambiguous_backend_identity)
        return AccountUsage(
            account_id=selected.id,
            label=selected.label,
            captured_at=datetime.now().astimezone(),
            five_hour=LimitWindow(name="5h", remaining=97),
        )

    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_direct", fake_fetch_direct)

    result = fetch_all(AppConfig(accounts=(account,)), (account,), direct=True)

    assert flags == [False]
    assert result[0].status == AccountStatus.OK
    assert result[0].five_hour is not None


def test_fetch_all_rejects_only_unattributed_account_in_mixed_direct_config(
    tmp_path,
    monkeypatch,
):
    accounts = (
        Account(id="default", label="Default", profile_dir="/tmp/default"),
        Account(
            id="explicit",
            label="Explicit",
            profile_dir="/tmp/explicit",
            auth_json_path=str(tmp_path / "explicit-auth.json"),
        ),
    )

    monkeypatch.setattr("codex_usage.scheduler.load_state_generation", lambda _account_id: 0)

    def fake_fetch_direct(
        account,
        *,
        auth_json_path=None,
        reject_ambiguous_backend_identity=False,
    ):
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=datetime.now().astimezone(),
            five_hour=LimitWindow(name="5h", remaining=97),
        )

    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_direct", fake_fetch_direct)

    result = fetch_all(AppConfig(accounts=accounts), accounts, direct=True)
    by_id = {usage.account_id: usage for usage in result}

    assert by_id["default"].status == AccountStatus.ERROR
    assert by_id["default"].five_hour is None
    assert by_id["explicit"].status == AccountStatus.OK
    assert by_id["explicit"].five_hour is not None


def test_fetch_all_contains_state_generation_failure_per_account(monkeypatch):
    accounts = (
        Account(id="broken", label="Broken", profile_dir="/tmp/broken"),
        Account(id="healthy", label="Healthy", profile_dir="/tmp/healthy"),
    )
    fetched: list[str] = []

    def fake_load_state_generation(account_id):
        if account_id == "broken":
            raise ValueError("invalid generation")
        return 0

    def fake_fetch_one(config, account, **kwargs):
        fetched.append(account.id)
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=datetime.now().astimezone(),
            backend_configured="browser",
            backend_used="browser",
            five_hour=LimitWindow(name="5h", remaining=80),
        )

    monkeypatch.setattr(
        "codex_usage.scheduler.load_state_generation",
        fake_load_state_generation,
    )
    monkeypatch.setattr("codex_usage.scheduler._fetch_one", fake_fetch_one)

    result = fetch_all(
        AppConfig(accounts=accounts),
        accounts,
        headed=True,
    )

    assert fetched == ["healthy"]
    assert result[0].status == AccountStatus.ERROR
    assert result[0].error == "state generation failed: ValueError"
    assert result[0].cache_invalidated is True
    assert result[1].status == AccountStatus.OK


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


@pytest.mark.parametrize("interval_seconds", (-1, 0, 59, 60.0, True))
def test_watch_rejects_invalid_interval(interval_seconds):
    with pytest.raises(ValueError, match="interval_seconds"):
        watch(
            AppConfig(accounts=()),
            (),
            output="table",
            interval_seconds=interval_seconds,
        )


def test_watch_marks_unusable_usage_as_cycle_error(monkeypatch, capsys):
    delays: list[int] = []
    health_events: list[tuple[str, str]] = []

    class StopAfterWait:
        def is_set(self):
            return False

        def wait(self, delay):
            delays.append(delay)
            return True

        def set(self):
            return None

    account = Account(id="privat", label="Privat", profile_dir="/tmp/privat")
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.LOGIN_REQUIRED,
        error="401",
    )
    monkeypatch.setattr("codex_usage.scheduler.Event", StopAfterWait)
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_all",
        lambda *args, **kwargs: [usage],
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.record_health_event",
        lambda component, event, **kwargs: health_events.append((component, event)),
    )

    watch(
        AppConfig(accounts=(account,)),
        (account,),
        output="table",
        interval_seconds=60,
    )

    assert delays == [5]
    assert health_events == [("watch", "cycle_error")]
    assert "watch cycle failed" in capsys.readouterr().err


def test_watch_clears_unusable_values_from_failure_output(monkeypatch, capsys):
    class StopAfterWait:
        def is_set(self):
            return False

        def wait(self, _delay):
            return True

        def set(self):
            return None

    account = Account(id="privat", label="Privat", profile_dir="/tmp/privat")
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime.now().astimezone(),
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
        stale=True,
        error="old values retained",
    )
    monkeypatch.setattr("codex_usage.scheduler.Event", StopAfterWait)
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_all",
        lambda *args, **kwargs: [usage],
    )

    watch(
        AppConfig(accounts=(account,)),
        (account,),
        output="table",
        interval_seconds=60,
    )

    captured = capsys.readouterr()
    assert "97%" not in captured.out
    assert "55%" not in captured.out
    assert "error: old values retained" in captured.out


def test_watch_failure_json_contains_no_values(monkeypatch, capsys):
    class StopAfterWait:
        def is_set(self):
            return False

        def wait(self, _delay):
            return True

        def set(self):
            return None

    account = Account(id="privat", label="Privat", profile_dir="/tmp/privat")
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime.now().astimezone(),
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
        status=AccountStatus.PARTIAL,
        error="usage limits not found",
    )
    monkeypatch.setattr("codex_usage.scheduler.Event", StopAfterWait)
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_all",
        lambda *args, **kwargs: [usage],
    )

    watch(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
        interval_seconds=60,
    )

    payload = capsys.readouterr().out
    assert '"five_hour": null' in payload
    assert '"weekly": null' in payload
    assert '"status": "error"' in payload
    assert '"remaining": 97' not in payload
    assert '"remaining": 55' not in payload


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


def test_fetch_all_invalidates_cache_after_unexpected_fetch_failure(monkeypatch):
    account = Account(
        id="broken",
        label="Broken",
        profile_dir="/tmp/broken",
        auth_json_path="/tmp/broken-auth.json",
        backend="direct",
    )

    def fail_fetch(*_args, **_kwargs):
        raise RuntimeError("backend crashed")

    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_direct", fail_fetch)

    result = fetch_all(AppConfig(accounts=(account,)), (account,), direct=True)

    assert len(result) == 1
    assert result[0].status == AccountStatus.ERROR
    assert result[0].backend_used == "direct"
    assert result[0].cache_invalidated is True
    assert result[0].five_hour is None
    assert result[0].weekly is None


def test_fetch_one_rejects_malformed_backend_override_fail_closed():
    account = Account(
        id="account",
        label="Account",
        profile_dir="/tmp/account",
        backend="direct",
    )

    result = _fetch_one(
        AppConfig(accounts=(account,)),
        account,
        headed=False,
        direct=False,
        backend_override=[],
        auth_json_path=None,
    )

    assert result.status == AccountStatus.ERROR
    assert result.error == "fetch failed: ValueError"
    assert result.backend_used is None
    assert result.cache_invalidated is True
    assert result.five_hour is None
    assert result.weekly is None


def test_fetch_all_expires_reset_windows_before_return(monkeypatch):
    account = Account(id="expired", label="Expired", profile_dir="/tmp/expired")
    now = datetime.now().astimezone()
    usage = AccountUsage(
        account_id="expired",
        label="Expired",
        captured_at=now,
        five_hour=LimitWindow(
            name="5h",
            remaining=97,
            reset_at=now - timedelta(seconds=1),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=55,
            reset_at=now + timedelta(days=6),
        ),
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_account_usage",
        lambda *_args, **_kwargs: usage,
    )

    result = fetch_all(
        AppConfig(accounts=(account,)),
        (account,),
        headed=True,
    )

    assert result[0].status == AccountStatus.PARTIAL
    assert result[0].stale is True
    assert result[0].five_hour is None
    assert result[0].weekly is not None
    assert result[0].weekly.remaining == 55
    assert result[0].main is not None
    assert [window.name for window in result[0].main.windows] == ["weekly"]


def test_direct_override_failure_keeps_direct_provenance(monkeypatch):
    account = Account(
        id="broken",
        label="Broken",
        profile_dir="/tmp/broken",
        backend="browser",
    )

    def fail_fetch(*_args, **_kwargs):
        raise RuntimeError("backend crashed")

    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_direct", fail_fetch)

    result = fetch_all(AppConfig(accounts=(account,)), (account,), direct=True)

    assert result[0].status == AccountStatus.ERROR
    assert result[0].backend_configured == "browser"
    assert result[0].backend_used == "direct"


def test_fetch_all_clears_values_after_snapshot_save_failure(monkeypatch):
    account = Account(id="broken", label="Broken", profile_dir="/tmp/broken")
    usage = AccountUsage(
        account_id="broken",
        label="Broken",
        captured_at=datetime.now().astimezone(),
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
    )

    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_account_usage",
        lambda *_args, **_kwargs: usage,
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.save_current_usage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read-only")),
    )

    result = fetch_all(
        AppConfig(accounts=(account,)),
        (account,),
        headed=True,
        save_snapshots=True,
    )

    assert result[0].status == AccountStatus.ERROR
    assert result[0].error == "snapshot save failed: OSError"
    assert result[0].cache_invalidated is True
    assert result[0].five_hour is None
    assert result[0].weekly is None
    assert result[0].main is None
    assert result[0].models == ()


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
        backend_configured="app-server",
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


def test_fetch_all_direct_saves_authenticated_partial_snapshots(monkeypatch):
    accounts = (
        Account(id="ok", label="OK", profile_dir="/tmp/ok", auth_json_path="/tmp/ok-auth.json"),
        Account(
            id="partial",
            label="Partial",
            profile_dir="/tmp/partial",
            auth_json_path="/tmp/partial-auth.json",
        ),
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
    partial_usage = AccountUsage(
        account_id="partial",
        label="Partial",
        captured_at=captured_at,
        status=AccountStatus.PARTIAL,
        error="usage limits not found",
    )
    error_usage = AccountUsage(
        account_id="broken",
        label="Broken",
        captured_at=captured_at,
        status=AccountStatus.LOGIN_REQUIRED,
        error="direct auth failed",
    )
    by_account = {"ok": ok_usage, "partial": partial_usage, "broken": error_usage}
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

    assert [usage.account_id for usage in usages] == ["ok", "partial", "broken"]
    assert [usage.backend_used for usage in usages] == ["direct", "direct", "direct"]
    assert saved == ["ok", "partial"]
    assert sorted(current) == ["broken", "ok", "partial"]


def test_fetch_all_persists_accounts_inside_shared_lock(monkeypatch):
    accounts = (
        Account(id="one", label="One", profile_dir="/tmp/one"),
        Account(id="two", label="Two", profile_dir="/tmp/two"),
    )
    shared_lock_active = False
    save_states: list[tuple[str, bool]] = []

    class TrackingLock:
        def __init__(self, account_id: str):
            self.account_id = account_id

        def __enter__(self):
            nonlocal shared_lock_active
            if self.account_id == "__all_accounts__":
                shared_lock_active = True
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            nonlocal shared_lock_active
            if self.account_id == "__all_accounts__":
                shared_lock_active = False
            return False

    def fake_fetch(account, config, *, headed):
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=datetime.now().astimezone(),
            five_hour=LimitWindow(name="5h", remaining=80),
            weekly=LimitWindow(name="weekly", remaining=60),
        )

    monkeypatch.setattr("codex_usage.scheduler.account_lock", TrackingLock)
    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage", fake_fetch)
    monkeypatch.setattr(
        "codex_usage.scheduler.save_current_usage",
        lambda usage: save_states.append((usage.account_id, shared_lock_active)),
    )
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    result = fetch_all(
        AppConfig(accounts=accounts),
        accounts,
        headed=True,
        save_snapshots=True,
    )

    assert [usage.account_id for usage in result] == ["one", "two"]
    assert save_states == [("one", True), ("two", True)]


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
        backend_configured="direct",
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
        backend_configured="direct",
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
        backend_configured="direct",
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
        backend_configured="direct",
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


def test_fetch_all_does_not_stabilize_direct_against_unproven_app_server_snapshot(
    monkeypatch,
):
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
            reset_at=datetime(2026, 7, 12, 4, 40, 41, tzinfo=timezone),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=89,
            reset_at=datetime(2026, 7, 18, 8, 2, 42, tzinfo=timezone),
        ),
        backend_used="app-server",
        backend_user_id="user-account",
        backend_account_id="account-id",
    )
    current = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 7, 12, 0, 1, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            remaining=99,
            reset_at=datetime(2026, 7, 12, 4, 41, 59, tzinfo=timezone),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=99,
            reset_at=datetime(2026, 7, 18, 8, 30, 25, tzinfo=timezone),
        ),
        backend_used="direct",
        backend_user_id="user-account",
        backend_account_id="account-id",
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_account_usage_direct",
        lambda selected, *, auth_json_path=None: current,
    )
    monkeypatch.setattr("codex_usage.scheduler.load_usage_snapshot", lambda account_id: previous)

    result = fetch_all(AppConfig(accounts=(account,)), (account,))

    assert result[0].five_hour is not None
    assert result[0].five_hour.remaining == 99
    assert result[0].weekly is not None
    assert result[0].weekly.remaining == 99
    assert result[0].stale is False


def test_fetch_all_does_not_persist_explicit_backend_override(monkeypatch):
    account = Account(
        id="account",
        label="Account",
        profile_dir="/tmp/account",
        backend="direct",
        auth_json_path="/tmp/account-auth.json",
    )
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="app-server",
        backend_user_id="user-account",
        backend_account_id="account-id",
        five_hour=LimitWindow(name="five_hour", remaining=11),
        weekly=LimitWindow(name="weekly", remaining=22),
    )
    saved_current = []
    saved_snapshots = []
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_account_usage_app_server",
        lambda selected: usage,
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.save_current_usage",
        lambda selected: saved_current.append(selected.account_id),
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.save_usage_snapshot",
        lambda selected: saved_snapshots.append(selected.account_id),
    )

    result = fetch_all(
        AppConfig(accounts=(account,)),
        (account,),
        backend_override="app-server",
        save_snapshots=True,
    )

    assert result[0].backend_used == "app-server"
    assert result[0].backend_configured == "app-server"
    assert saved_current == []
    assert saved_snapshots == []


def test_watchdog_does_not_persist_explicit_backend_override(monkeypatch):
    account = Account(
        id="account",
        label="Account",
        profile_dir="/tmp/account",
        backend="direct",
    )
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        backend_configured="app-server",
        backend_used="app-server",
        backend_user_id="user-account",
        backend_account_id="account-id",
        five_hour=LimitWindow(name="five_hour", remaining=11),
        weekly=LimitWindow(name="weekly", remaining=22),
    )
    saved_current = []
    saved_snapshots = []
    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: None,
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_all",
        lambda *args, **kwargs: [usage],
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.save_current_usage",
        lambda selected: saved_current.append(selected.account_id),
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.save_usage_snapshot",
        lambda selected: saved_snapshots.append(selected.account_id),
    )

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
        backend_override="app-server",
    )

    assert result == [usage]
    assert saved_current == []
    assert saved_snapshots == []


def test_authenticated_reset_fallback_is_applied_per_window():
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            remaining=90,
            reset_at=datetime(2026, 7, 12, 4, 40, 41, tzinfo=timezone),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=80,
            reset_at=datetime(2026, 7, 18, 8, 2, 42, tzinfo=timezone),
        ),
        backend_configured="app-server",
        backend_used="direct",
        backend_user_id="user-account",
        backend_account_id="account-id",
    )
    current = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 7, 12, 0, 1, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            remaining=99,
            reset_at=datetime(2026, 7, 12, 4, 42, 0, tzinfo=timezone),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=70,
            reset_at=datetime(2026, 7, 18, 8, 2, 42, tzinfo=timezone),
        ),
        backend_configured="app-server",
        backend_used="app-server",
        backend_user_id="user-account",
        backend_account_id="account-id",
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=300)

    assert result.five_hour is not None and result.five_hour.remaining == 90
    assert result.weekly is not None and result.weekly.remaining == 70
    assert result.main is not None
    assert [window.remaining for window in result.main.windows] == [90, 70]
    assert result.stale is True


def test_authenticated_stabilization_rejects_previous_without_configured_backend():
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(name="5h", remaining=90),
        backend_used="direct",
        backend_user_id="user-account",
        backend_account_id="account-id",
    )
    current = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 7, 12, 0, 1, tzinfo=timezone),
        five_hour=LimitWindow(name="5h", remaining=99),
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user-account",
        backend_account_id="account-id",
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=300)

    assert result is current


def test_authenticated_app_server_absolute_reset_is_not_replaced_by_old_value():
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            used=90,
            limit=100,
            remaining=10,
            percent=10,
            reset_at=datetime(2026, 7, 12, 5, 0, tzinfo=timezone),
            source="app-server",
        ),
        weekly=LimitWindow(
            name="weekly",
            used=10,
            limit=100,
            remaining=90,
            percent=90,
            reset_at=datetime(2026, 7, 18, 0, 0, tzinfo=timezone),
            source="app-server",
        ),
        backend_used="app-server",
        backend_user_id="user-account",
        backend_account_id="account-id",
    )
    current = replace(
        previous,
        captured_at=datetime(2026, 7, 12, 0, 1, tzinfo=timezone),
        five_hour=replace(
            previous.five_hour,
            used=0,
            remaining=100,
            percent=100,
            reset_at=datetime(2026, 7, 12, 10, 0, tzinfo=timezone),
        ),
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=300)

    assert result is current
    assert result.five_hour is not None and result.five_hour.remaining == 100
    assert result.stale is False


def test_authenticated_app_server_fallback_is_not_reused_after_confirmation():
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            remaining=90,
            reset_at=datetime(2026, 7, 12, 4, 40, 41, tzinfo=timezone),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=80,
            reset_at=datetime(2026, 7, 18, 8, 2, 42, tzinfo=timezone),
        ),
        backend_used="app-server",
        backend_user_id="user-account",
        backend_account_id="account-id",
        fallback_reason="previous authenticated limits retained after reset transition",
        stale=True,
    )
    current = replace(
        previous,
        captured_at=datetime(2026, 7, 12, 0, 1, tzinfo=timezone),
        five_hour=replace(
            previous.five_hour,
            remaining=100,
            reset_at=datetime(2026, 7, 12, 9, 42, 0, tzinfo=timezone),
        ),
        fallback_reason=None,
        stale=False,
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=300)

    assert result is current
    assert result.five_hour is not None and result.five_hour.remaining == 100
    assert result.stale is False


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
        backend_configured="direct",
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
        backend_configured="direct",
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


def test_authenticated_stabilization_ignores_relative_reset_time_drift():
    timezone = ZoneInfo("Europe/Berlin")
    previous_captured = datetime(2026, 7, 12, 4, 10, tzinfo=timezone)
    current_captured = datetime(2026, 7, 12, 4, 13, tzinfo=timezone)
    previous = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=previous_captured,
        five_hour=LimitWindow(
            name="5h",
            used=2,
            limit=100,
            remaining=98,
            percent=98,
            reset_at=previous_captured + timedelta(hours=5),
            raw=(
                '$.rate_limit.primary_window {"used_percent": 2, '
                '"limit_window_seconds": 18000, "reset_after_seconds": 18000}'
            ),
        ),
        weekly=LimitWindow(name="weekly", remaining=49),
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
    )
    current = replace(
        previous,
        captured_at=current_captured,
        five_hour=replace(
            previous.five_hour,
            used=1,
            remaining=99,
            percent=99,
            reset_at=current_captured + timedelta(hours=5),
            raw=(
                '$.rate_limit.primary_window {"used_percent": 1, '
                '"limit_window_seconds": 18000, "reset_after_seconds": 18000}'
            ),
        ),
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=360)

    assert result is current
    assert result.five_hour is not None
    assert result.five_hour.remaining == 99
    assert result.stale is False


def test_authenticated_stabilization_ignores_a_running_relative_countdown():
    timezone = ZoneInfo("Europe/Berlin")
    previous_captured = datetime(2026, 7, 12, 4, 10, tzinfo=timezone)
    current_captured = datetime(2026, 7, 12, 4, 13, tzinfo=timezone)
    previous = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=previous_captured,
        five_hour=LimitWindow(
            name="5h",
            remaining=98,
            reset_at=previous_captured + timedelta(seconds=17_000),
            raw=(
                '"limit_window_seconds": 18000, "reset_after_seconds": 17000'
            ),
        ),
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
    )
    current = replace(
        previous,
        captured_at=current_captured,
        five_hour=replace(
            previous.five_hour,
            remaining=99,
            reset_at=current_captured + timedelta(seconds=17_000),
            raw=(
                '"limit_window_seconds": 18000, "reset_after_seconds": 17000'
            ),
        ),
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=360)

    assert result is current
    assert result.five_hour is not None
    assert result.five_hour.remaining == 99
    assert result.stale is False


def test_authenticated_stabilization_rejects_malformed_relative_reset_metadata():
    timezone = ZoneInfo("Europe/Berlin")
    previous_captured = datetime(2026, 7, 12, 4, 10, tzinfo=timezone)
    current_captured = datetime(2026, 7, 12, 4, 13, tzinfo=timezone)
    previous = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=previous_captured,
        five_hour=LimitWindow(
            name="5h",
            remaining=50,
            reset_at=previous_captured + timedelta(hours=5),
            raw=(
                '"limit_window_seconds": 18000, "reset_after_seconds": 18001'
            ),
        ),
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
    )
    current = replace(
        previous,
        captured_at=current_captured,
        five_hour=replace(
            previous.five_hour,
            remaining=100,
            reset_at=current_captured + timedelta(hours=6),
        ),
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=360)

    assert result is current
    assert result.five_hour is not None
    assert result.five_hour.remaining == 100
    assert result.stale is False


def test_authenticated_stabilization_rejects_incomparable_reset_timestamps():
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime(2026, 7, 12, 4, 10, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            remaining=50,
            reset_at=datetime(2026, 7, 12, 9, 10),
        ),
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
    )
    current = replace(
        previous,
        captured_at=datetime(2026, 7, 12, 4, 13, tzinfo=timezone),
        five_hour=replace(
            previous.five_hour,
            remaining=99,
            reset_at=datetime(2026, 7, 12, 10, 13, tzinfo=timezone),
        ),
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=360)

    assert result is current
    assert result.five_hour is not None and result.five_hour.remaining == 99


def test_authenticated_stabilization_accepts_reset_with_dynamic_absolute_timestamp():
    timezone = ZoneInfo("Europe/Berlin")
    previous_captured = datetime(2026, 7, 12, 4, 10, tzinfo=timezone)
    current_captured = datetime(2026, 7, 12, 4, 13, tzinfo=timezone)
    previous = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=previous_captured,
        five_hour=LimitWindow(
            name="5h",
            used=5,
            limit=100,
            remaining=95,
            percent=95,
            reset_at=previous_captured + timedelta(hours=5),
            raw=(
                '{"used_percent": 5, "limit_window_seconds": 18000, '
                '"reset_after_seconds": 18000, "reset_at": 1783860000}'
            ),
        ),
        weekly=LimitWindow(name="weekly", remaining=49),
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
    )
    current = replace(
        previous,
        captured_at=current_captured,
        five_hour=replace(
            previous.five_hour,
            used=0,
            remaining=100,
            percent=100,
            reset_at=current_captured + timedelta(hours=5),
            raw=(
                '{"used_percent": 0, "limit_window_seconds": 18000, '
                '"reset_after_seconds": 18000, "reset_at": 1783860180}'
            ),
        ),
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=360)

    assert result is current
    assert result.five_hour is not None
    assert result.five_hour.remaining == 100
    assert result.stale is False


def test_authenticated_stabilization_does_not_restore_reset_only_current_window():
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            remaining=90,
            reset_at=datetime(2026, 7, 12, 5, 0, tzinfo=timezone),
        ),
        weekly=LimitWindow(name="weekly", remaining=80),
        status=AccountStatus.OK,
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
    )
    current = replace(
        previous,
        captured_at=datetime(2026, 7, 12, 0, 1, tzinfo=timezone),
        status=AccountStatus.PARTIAL,
        five_hour=LimitWindow(
            name="5h",
            reset_at=datetime(2026, 7, 12, 10, 0, tzinfo=timezone),
        ),
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=300)

    assert result is current
    assert result.five_hour is not None
    assert result.five_hour.remaining is None
    assert result.stale is False


def test_authenticated_stabilization_rejects_a_different_window_duration():
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            used=5,
            limit=100,
            remaining=95,
            percent=95,
            reset_at=datetime(2026, 7, 13, 0, 0, tzinfo=timezone),
            raw='{"limit_window_seconds": 2592000}',
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
            used=1,
            remaining=99,
            percent=99,
            reset_at=datetime(2026, 7, 17, 0, 0, tzinfo=timezone),
            raw='{"limit_window_seconds": 18000}',
        ),
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=300)

    assert result is current
    assert result.five_hour is not None
    assert result.five_hour.remaining == 99
    assert result.stale is False


def test_authenticated_stabilization_rejects_unknown_window_duration():
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="",
            remaining=95,
            reset_at=datetime(2026, 7, 12, 5, 0, tzinfo=timezone),
            raw='{"limit_window_seconds": 2592000}',
        ),
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
    )
    current = replace(
        previous,
        captured_at=datetime(2026, 7, 12, 0, 1, tzinfo=timezone),
        five_hour=replace(
            previous.five_hour,
            remaining=99,
            reset_at=datetime(2026, 7, 13, 5, 0, tzinfo=timezone),
        ),
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=300)

    assert result is current
    assert result.five_hour is not None
    assert result.five_hour.remaining == 99
    assert result.stale is False


def test_authenticated_stabilization_rejects_implausibly_future_reset():
    timezone = ZoneInfo("Europe/Berlin")
    captured_at = datetime(2026, 7, 12, 0, 0, tzinfo=timezone)
    previous = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=captured_at,
        five_hour=LimitWindow(
            name="5h",
            remaining=95,
            reset_at=captured_at + timedelta(hours=5),
        ),
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
    )
    current = replace(
        previous,
        captured_at=captured_at + timedelta(minutes=1),
        five_hour=replace(
            previous.five_hour,
            remaining=99,
            reset_at=captured_at + timedelta(days=10),
        ),
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=300)

    assert result is current
    assert result.five_hour is not None
    assert result.five_hour.remaining == 99
    assert result.stale is False


def test_authenticated_stabilization_rejects_a_different_known_window_kind():
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            remaining=95,
            percent=95,
            reset_at=datetime(2026, 7, 13, 0, 0, tzinfo=timezone),
        ),
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
    )
    current = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime(2026, 7, 12, 0, 1, tzinfo=timezone),
        five_hour=LimitWindow(
            name="weekly",
            remaining=99,
            percent=99,
            reset_at=datetime(2026, 7, 17, 0, 0, tzinfo=timezone),
        ),
        backend_used="direct",
        backend_user_id="user-direct",
        backend_account_id="account-direct",
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=300)

    assert result is current
    assert result.five_hour is not None
    assert result.five_hour.name == "weekly"
    assert result.five_hour.remaining == 99
    assert result.stale is False


def test_authenticated_stabilization_rejects_unclassified_window_transition():
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="",
            remaining=95,
            reset_at=datetime(2026, 7, 12, 5, 0, tzinfo=timezone),
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
            remaining=99,
            reset_at=datetime(2026, 7, 12, 5, 2, tzinfo=timezone),
        ),
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=300)

    assert result is current
    assert result.five_hour is not None
    assert result.five_hour.remaining == 99
    assert result.stale is False


def test_scheduler_remaining_percent_prefers_absolute_usage_values():
    window = LimitWindow(
        name="5h",
        used=8,
        limit=40,
        remaining=32,
        percent=20,
    )

    assert _remaining_percent(window) == 80


def test_scheduler_numeric_overflow_is_treated_as_missing():
    huge = 10**309
    window = LimitWindow(
        name="5h",
        used=huge,
        limit=100,
        raw=f'{{"limit_window_seconds": {huge}}}',
    )

    assert _remaining_percent(window) is None
    assert _raw_number(window.raw, "limit_window_seconds") is None
    assert _window_duration_seconds(window) is None


def test_scheduler_ignores_oversized_finite_raw_window_duration():
    window = LimitWindow(
        name="5h",
        raw='{"limit_window_seconds": 315360001}',
    )

    assert _window_duration_seconds(window) is None


@pytest.mark.parametrize(
    "window",
    (
        LimitWindow(name="5h", used=0, limit=0),
        LimitWindow(name="5h", used=10, limit=-1),
    ),
)
def test_scheduler_blocks_on_non_positive_absolute_limit(window):
    assert _window_is_exhausted(window) is True


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (LimitWindow(name="5h", used=120, limit=100), 0),
        (LimitWindow(name="5h", used=-20, limit=100), None),
        (LimitWindow(name="5h", remaining=120), None),
        (LimitWindow(name="5h", remaining=-20), None),
        (LimitWindow(name="5h", remaining=-20, percent=97), None),
        (LimitWindow(name="5h", remaining=120, limit=100), None),
        (LimitWindow(name="5h", percent=120), None),
        (LimitWindow(name="5h", percent=-20), None),
        (LimitWindow(name="5h", percent=True), None),
        (LimitWindow(name="5h", remaining="97"), None),
        (LimitWindow(name="5h", percent=float("nan")), None),
        (LimitWindow(name="5h", percent=float("inf")), None),
    ],
)
def test_scheduler_remaining_percent_fails_closed_for_malformed_values(window, expected):
    assert _remaining_percent(window) == expected


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
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="shared-user",
        backend_account_id="default-account",
        blocked_until=datetime(2099, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin")),
        blocked_reason="usage limit reached: weekly",
    )
    ok_usage = AccountUsage(
        account_id="ok",
        label="OK",
        captured_at=now,
        backend_configured="direct",
        backend_used="direct",
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
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_from_file",
        lambda path: ("shared-user", "default-account"),
    )
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


def test_watchdog_direct_refetches_browser_block_without_account_auth(monkeypatch):
    account = Account(id="blocked", label="Blocked", profile_dir="/tmp/blocked")
    now = datetime.now().astimezone()
    blocked_snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=now,
        status=AccountStatus.BLOCKED,
        backend_configured="direct",
        backend_used="browser",
        blocked_until=now + timedelta(hours=2),
    )
    fresh = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=now,
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", remaining=97),
    )
    fetched_accounts: list[str] = []

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
        fetched_accounts.extend(selected.id for selected in fetch_accounts)
        return [fresh]

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: blocked_snapshot,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
        direct=True,
    )

    assert fetched_accounts == ["blocked"]
    assert result == [fresh]


def test_watchdog_direct_rechecks_default_auth_identity_for_block(
    monkeypatch,
):
    account = Account(id="blocked", label="Blocked", profile_dir="/tmp/blocked")
    now = datetime.now().astimezone()
    blocked_snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=now,
        status=AccountStatus.BLOCKED,
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user-old",
        backend_account_id="account-old",
        blocked_until=now + timedelta(hours=2),
    )
    fresh = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=now,
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user-new",
        backend_account_id="account-new",
        five_hour=LimitWindow(name="5h", remaining=97),
    )
    fetched_accounts: list[str] = []

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
        fetched_accounts.extend(selected.id for selected in fetch_accounts)
        return [fresh]

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: blocked_snapshot,
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_from_file",
        lambda path: ("user-new", "account-new"),
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
        direct=True,
    )

    assert fetched_accounts == ["blocked"]
    assert result == [fresh]


def test_watchdog_contains_state_generation_failure_for_blocked_snapshot(monkeypatch):
    accounts = (
        Account(id="broken", label="Broken", profile_dir="/tmp/broken"),
        Account(id="healthy", label="Healthy", profile_dir="/tmp/healthy"),
    )
    now = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    blocked_snapshot = AccountUsage(
        account_id="broken",
        label="Broken",
        captured_at=now,
        status=AccountStatus.BLOCKED,
        backend_configured="direct",
        backend_used="direct",
        blocked_until=datetime(2099, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin")),
        blocked_reason="usage limit reached: weekly",
    )
    fresh = [
        AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=now,
            backend_configured="direct",
            backend_used="direct",
            five_hour=LimitWindow(name="5h", remaining=80),
        )
        for account in accounts
    ]
    seen_fetch_accounts: list[str] = []

    def fake_load_state_generation(account_id):
        if account_id == "broken":
            raise ValueError("invalid generation")
        return 0

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
        return fresh

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: (
            blocked_snapshot if account_id == "broken" else None
        ),
    )
    monkeypatch.setattr("codex_usage.scheduler.load_current_usage", lambda *args: None)
    monkeypatch.setattr(
        "codex_usage.scheduler.load_state_generation",
        fake_load_state_generation,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    result = watchdog(
        AppConfig(accounts=accounts),
        accounts,
        output="json",
        direct=True,
    )

    assert seen_fetch_accounts == ["broken", "healthy"]
    assert [usage.status for usage in result] == [AccountStatus.OK, AccountStatus.OK]


def test_watchdog_contains_unexpected_fetch_failure_per_account(monkeypatch):
    accounts = (
        Account(id="broken", label="Broken", profile_dir="/tmp/broken"),
        Account(id="healthy", label="Healthy", profile_dir="/tmp/healthy"),
    )

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: None,
    )
    monkeypatch.setattr("codex_usage.scheduler.load_current_usage", lambda *args: None)
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_all",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("backend crashed")),
    )

    result = watchdog(
        AppConfig(accounts=accounts),
        accounts,
        output="json",
    )

    assert [usage.account_id for usage in result] == ["broken", "healthy"]
    assert [usage.status for usage in result] == [
        AccountStatus.ERROR,
        AccountStatus.ERROR,
    ]
    assert [usage.error for usage in result] == [
        "watchdog fetch failed: RuntimeError",
        "watchdog fetch failed: RuntimeError",
    ]
    assert all(usage.cache_invalidated for usage in result)


def test_watchdog_refetches_block_with_inconsistent_limit_windows(monkeypatch):
    account = Account(id="blocked", label="Blocked", profile_dir="/tmp/blocked")
    now = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    blocked_until = datetime(2099, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin"))
    blocked_snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=now,
        status=AccountStatus.BLOCKED,
        blocked_until=blocked_until,
        five_hour=LimitWindow(
            name="5h",
            remaining=50,
            reset_at=blocked_until,
        ),
    )
    fresh = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=now,
        status=AccountStatus.OK,
        five_hour=LimitWindow(name="5h", remaining=99),
        weekly=LimitWindow(name="weekly", remaining=98),
    )
    fetched_accounts: list[str] = []

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
        fetched_accounts.extend(account.id for account in fetch_accounts)
        return [fresh]

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: blocked_snapshot,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
    )

    assert fetched_accounts == ["blocked"]
    assert result == [fresh]


def test_watchdog_replaces_identity_mismatch_with_fail_closed_error(monkeypatch):
    account = Account(id="account", label="Account", profile_dir="/tmp/account")
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
    )

    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_all",
        lambda *_args, **_kwargs: [usage, usage],
    )
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda *_args: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda *_args: None)

    result = watchdog(AppConfig(accounts=(account,)), (account,), output="json")

    assert len(result) == 1
    assert result[0].status is AccountStatus.ERROR
    assert result[0].cache_invalidated is True
    assert result[0].five_hour is None
    assert result[0].weekly is None


def test_watchdog_uses_dst_aware_local_timezone(monkeypatch):
    account = Account(id="account", label="Account", profile_dir="/tmp/account")
    local_tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 7, 13, 1, 0, tzinfo=local_tz)
    timezone_calls = []
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=now,
        status=AccountStatus.OK,
        five_hour=LimitWindow(name="5h", remaining=99),
        weekly=LimitWindow(name="weekly", remaining=95),
    )

    class Clock:
        @classmethod
        def now(cls, tz=None):
            timezone_calls.append(tz)
            return now.astimezone(tz) if tz is not None else now

    monkeypatch.setattr("codex_usage.scheduler.LOCAL_TZ", local_tz)
    monkeypatch.setattr("codex_usage.scheduler.datetime", Clock)
    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: None,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", lambda *args, **kwargs: [usage])
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    assert watchdog(AppConfig(accounts=(account,)), (account,), output="json") == [usage]
    assert timezone_calls
    assert timezone_calls == [local_tz, local_tz]


def test_watchdog_refetches_far_future_blocked_snapshot(monkeypatch):
    account = Account(id="blocked", label="Blocked", profile_dir="/tmp/blocked")
    now = datetime.now().astimezone()
    blocked_snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=now + timedelta(hours=1),
        status=AccountStatus.BLOCKED,
        blocked_until=now + timedelta(hours=2),
        backend_used="browser",
    )
    fresh = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=now,
        status=AccountStatus.OK,
        five_hour=LimitWindow(name="5h", remaining=99),
        weekly=LimitWindow(name="weekly", remaining=98),
        backend_used="browser",
    )
    fetched_accounts: list[str] = []

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
        fetched_accounts.extend(account.id for account in fetch_accounts)
        return [fresh]

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: blocked_snapshot,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
    )

    assert fetched_accounts == ["blocked"]
    assert result == [fresh]


def test_watchdog_refetches_browser_block_for_authenticated_direct_account(
    tmp_path,
    monkeypatch,
):
    account = Account(
        id="blocked",
        label="Blocked",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )
    now = datetime.now().astimezone()
    blocked_snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=now,
        status=AccountStatus.BLOCKED,
        blocked_until=now + timedelta(hours=2),
        backend_configured="direct",
        backend_used="browser",
        backend_user_id="shared-user",
        # A browser alias can contain the shared user ID instead of the
        # account-specific ID, so identity matching alone cannot make it safe.
        backend_account_id="shared-user",
    )
    fresh = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=now,
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="shared-user",
        backend_account_id="private-account",
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
    )
    fetched_accounts: list[str] = []

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
        fetched_accounts.extend(selected.id for selected in fetch_accounts)
        return [fresh]

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: blocked_snapshot,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
        direct=True,
    )

    assert fetched_accounts == ["blocked"]
    assert result == [fresh]


def test_watchdog_refetches_when_newer_current_supersedes_blocked_snapshot(monkeypatch):
    account = Account(id="blocked", label="Blocked", profile_dir="/tmp/blocked")
    timezone = ZoneInfo("Europe/Berlin")
    blocked_snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=timezone),
        status=AccountStatus.BLOCKED,
        blocked_until=datetime(2099, 6, 8, 6, 50, tzinfo=timezone),
        blocked_reason="usage limit reached: weekly",
        backend_configured="direct",
        backend_used="direct",
    )
    current = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime(2026, 6, 8, 4, 21, tzinfo=timezone),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", remaining=99),
        weekly=LimitWindow(name="weekly", remaining=98),
    )
    refreshed = replace(current, captured_at=datetime(2026, 6, 8, 4, 22, tzinfo=timezone))
    fetched_accounts: list[str] = []

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
        fetched_accounts.extend(account.id for account in fetch_accounts)
        return [refreshed]

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: blocked_snapshot,
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.load_current_usage",
        lambda account_id, current_dir=None: current,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
    )

    assert fetched_accounts == ["blocked"]
    assert result == [refreshed]


@pytest.mark.parametrize(
    ("account_backend", "direct", "backend_override", "snapshot_backend"),
    [
        ("app-server", False, None, "direct"),
        ("direct", False, "app-server", "direct"),
        ("app-server", True, None, "app-server"),
    ],
)
def test_watchdog_refetches_blocked_snapshot_after_backend_change(
    account_backend,
    direct,
    backend_override,
    snapshot_backend,
    monkeypatch,
):
    account = Account(
        id="blocked",
        label="Blocked",
        profile_dir="/tmp/blocked",
        backend=account_backend,
    )
    blocked_snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        status=AccountStatus.BLOCKED,
        blocked_until=datetime(2099, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin")),
        blocked_reason="usage limit reached: weekly",
        backend_configured=snapshot_backend,
        backend_used=snapshot_backend,
    )
    fresh_usage = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        five_hour=LimitWindow(name="5h", remaining=99),
        weekly=LimitWindow(name="weekly", remaining=95),
        backend_configured="app-server",
        backend_used="app-server",
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
        seen_fetch_accounts.extend(selected.id for selected in fetch_accounts)
        return [fresh_usage]

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: blocked_snapshot,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
        direct=direct,
        backend_override=backend_override,
    )

    assert seen_fetch_accounts == ["blocked"]
    assert result == [fresh_usage]


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


def test_watchdog_refetches_block_when_shared_account_user_changes(tmp_path, monkeypatch):
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
        backend_account_id="shared-account",
    )
    fresh_usage = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        five_hour=LimitWindow(name="5h", remaining=99),
        weekly=LimitWindow(name="weekly", remaining=95),
        backend_user_id="user-new",
        backend_account_id="shared-account",
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
        seen_fetch_accounts.extend(selected.id for selected in fetch_accounts)
        return [fresh_usage]

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: blocked_snapshot,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_for_account",
        lambda selected: ("user-new", "shared-account"),
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


def test_watchdog_refetches_legacy_block_without_account_identity(
    tmp_path,
    monkeypatch,
):
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
        backend_user_id="shared-user",
    )
    fresh_usage = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        five_hour=LimitWindow(name="5h", remaining=99),
        weekly=LimitWindow(name="weekly", remaining=95),
        backend_user_id="shared-user",
        backend_account_id="new-account",
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
        seen_fetch_accounts.extend(selected.id for selected in fetch_accounts)
        return [fresh_usage]

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: blocked_snapshot,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_for_account",
        lambda selected: ("shared-user", "new-account"),
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
        five_hour=LimitWindow(name="5h", remaining=99),
        weekly=LimitWindow(name="weekly", remaining=95),
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
        backend_configured="direct",
        backend_used="direct",
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
        def now(cls, tz=None):
            value = next(clock_values)
            return value.astimezone(tz) if tz is not None else value

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
        backend_configured="direct",
        backend_used="direct",
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
        def now(cls, tz=None):
            value = next(clock_values)
            return value.astimezone(tz) if tz is not None else value

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


def test_window_exhaustion_blocks_invalid_usage_even_with_safe_percent():
    from codex_usage.scheduler import _window_is_exhausted

    assert _window_is_exhausted(LimitWindow(name="5h", used=-1, percent=97)) is True


@pytest.mark.parametrize(
    "window",
    (
        LimitWindow(name="5h", remaining=-20),
        LimitWindow(name="5h", remaining=120),
        LimitWindow(name="5h", remaining=50, percent=90),
        LimitWindow(name="5h", percent=-1),
        LimitWindow(name="5h", percent=120),
        LimitWindow(name="5h", percent=True),
    ),
)
def test_window_exhaustion_blocks_out_of_range_percent_values(window):
    from codex_usage.scheduler import _window_is_exhausted

    assert _window_is_exhausted(window) is True


def test_scheduler_remaining_percent_prefers_percent_without_denominator():
    assert _remaining_percent(LimitWindow(name="5h", remaining=690, percent=69)) == 69
    assert _remaining_percent(LimitWindow(name="5h", remaining=690)) is None


def test_scheduler_remaining_percent_rejects_conflicting_denominatorless_values():
    assert _remaining_percent(LimitWindow(name="5h", remaining=50, percent=50)) == 50
    assert _remaining_percent(LimitWindow(name="5h", remaining=50, percent=90)) is None


def test_window_exhaustion_prefers_remaining_over_usage_percent():
    from codex_usage.scheduler import _window_is_exhausted

    assert _window_is_exhausted(
        LimitWindow(name="5h", used=0, limit=100, remaining=100, percent=0)
    ) is False


def test_window_exhaustion_prefers_absolute_usage_over_conflicting_remaining():
    from codex_usage.scheduler import _window_is_exhausted

    assert _window_is_exhausted(
        LimitWindow(name="5h", used=100, limit=100, remaining=100, percent=100)
    ) is True


def test_window_exhaustion_interprets_remaining_against_absolute_limit():
    assert _window_is_exhausted(
        LimitWindow(name="weekly", limit=1000, remaining=101)
    ) is False


def test_window_exhaustion_treats_missing_usage_as_exhausted():
    assert _window_is_exhausted(LimitWindow(name="5h")) is True


def test_watchdog_blocks_exhausted_window_without_usable_reset_time():
    usage = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        five_hour=LimitWindow(name="5h", remaining=0),
        weekly=LimitWindow(name="weekly", remaining=99),
    )

    blocked = _apply_watchdog_block(
        usage,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert blocked.status == AccountStatus.BLOCKED
    assert blocked.blocked_until is None
    assert blocked.blocked_reason == "usage limit reached: 5h; reset time unknown"


@pytest.mark.parametrize("window_name", [None, 1, ""])
def test_watchdog_malformed_window_name_fails_closed(window_name):
    reset_at = datetime(2099, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="malformed",
        label="Malformed",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(LimitWindow(name=window_name, remaining=0, reset_at=reset_at),),
        ),
    )

    blocked = _apply_watchdog_block(
        usage,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert blocked.status == AccountStatus.BLOCKED
    assert blocked.blocked_until == reset_at
    assert blocked.blocked_reason == (
        "usage limit reached: unknown; release at 2099-06-08T06:50:00+02:00"
    )


def test_watchdog_blocks_unavailable_pool_even_with_remaining_usage():
    reset_at = datetime(2099, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="unavailable",
        label="Unavailable",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        main=UsagePool(
            key="main",
            display_name="Codex",
            available=False,
            windows=(LimitWindow(name="weekly", remaining=80, reset_at=reset_at),),
        ),
    )

    blocked = _apply_watchdog_block(
        usage,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert blocked.status == AccountStatus.BLOCKED
    assert blocked.blocked_until == reset_at


@pytest.mark.parametrize(
    "main",
    [None, UsagePool(key="main", display_name="Codex", windows=())],
)
def test_watchdog_rejects_ok_usage_without_core_limits(main):
    usage = AccountUsage(
        account_id="missing",
        label="Missing",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        main=main,
    )

    rejected = _apply_watchdog_block(
        usage,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert rejected.status == AccountStatus.PARTIAL
    assert rejected.error == "missing usage limits; refresh required"
    assert rejected.five_hour is None
    assert rejected.weekly is None
    assert rejected.main is None
    assert rejected.cache_invalidated is True
    assert rejected.stale is True


@pytest.mark.parametrize("available, expected", [(True, True), (False, False)])
def test_watch_cycle_health_validates_dynamic_main_pool(available, expected):
    account = Account(id="dynamic", label="Dynamic", profile_dir="/tmp/dynamic")
    usage = AccountUsage(
        account_id="dynamic",
        label="Dynamic",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        main=UsagePool(
            key="main",
            display_name="Codex",
            available=available,
            windows=(LimitWindow(name="weekly", remaining=80),),
        ),
    )

    assert _watch_cycle_is_healthy([usage], [account]) is expected


def test_watch_cycle_health_accepts_explicit_backend_override():
    account = Account(id="direct", label="Direct", profile_dir="/tmp/direct")
    usage = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
        status=AccountStatus.OK,
        backend_configured="app-server",
        backend_used="app-server",
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(LimitWindow(name="weekly", remaining=80),),
        ),
    )

    assert (
        _watch_cycle_is_healthy(
            [usage],
            [account],
            backend_override="app-server",
        )
        is True
    )


def test_watch_cycle_health_rejects_missing_backend_provenance():
    account = Account(id="direct", label="Direct", profile_dir="/tmp/direct")
    usage = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
        status=AccountStatus.OK,
        five_hour=LimitWindow(name="5h", remaining=80),
    )

    assert _watch_cycle_is_healthy([usage], (item for item in [account])) is False


def test_watch_cycle_health_rejects_expired_core_reset():
    account = Account(id="direct", label="Direct", profile_dir="/tmp/direct")
    usage = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
        status=AccountStatus.OK,
        five_hour=LimitWindow(
            name="5h",
            remaining=80,
            reset_at=datetime.now(ZoneInfo("Europe/Berlin")) - timedelta(seconds=1),
        ),
        backend_configured="direct",
        backend_used="direct",
    )

    assert _watch_cycle_is_healthy([usage], [account]) is False


def test_watch_cycle_health_rejects_duplicate_usage_ids():
    account = Account(id="direct", label="Direct", profile_dir="/tmp/direct")
    usage = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
        status=AccountStatus.OK,
        five_hour=LimitWindow(name="5h", remaining=80),
        backend_configured="direct",
        backend_used="direct",
    )

    assert _watch_cycle_is_healthy([usage, usage], [account, account]) is False


def test_usage_map_rejects_missing_or_duplicate_results():
    account = Account(id="direct", label="Direct", profile_dir="/tmp/direct")
    usage = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
    )

    assert _usage_map_for_accounts([], [account]) is None
    assert _usage_map_for_accounts([usage, usage], [account, account]) is None


def test_watchdog_blocks_exhausted_dynamic_main_window():
    reset_at = datetime(2099, 6, 10, 5, 5, tzinfo=ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(LimitWindow(name="weekly", remaining=0, reset_at=reset_at),),
        ),
    )

    blocked = _apply_watchdog_block(
        usage,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert blocked.status == AccountStatus.BLOCKED
    assert blocked.blocked_until == reset_at
    assert blocked.blocked_reason == (
        f"usage limit reached: weekly; release at {reset_at.isoformat()}"
    )


@pytest.mark.parametrize("field, value", [("allowed", False), ("limit_reached", True)])
def test_watchdog_blocks_main_pool_flags_even_with_remaining_value(field, value):
    reset_at = datetime(2099, 6, 10, 5, 5, tzinfo=ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(LimitWindow(name="weekly", remaining=50, reset_at=reset_at),),
            **{field: value},
        ),
    )

    blocked = _apply_watchdog_block(
        usage,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert blocked.status == AccountStatus.BLOCKED
    assert blocked.blocked_until == reset_at
    assert blocked.blocked_reason == (
        f"usage limit reached: weekly; release at {reset_at.isoformat()}"
    )


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
    assert result[0].fallback_reason == "app-server unavailable: unsupported"
