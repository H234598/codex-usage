from __future__ import annotations

import signal
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import codex_usage.history as history_module
import codex_usage.scheduler as scheduler_module
from codex_usage.app_server import AppServerUnavailableError
from codex_usage.config import AppConfig
from codex_usage.models import Account, AccountStatus, AccountUsage, LimitWindow, UsagePool
from codex_usage.scheduler import (
    _ambiguous_direct_accounts,
    _apply_watchdog_block,
    _block_state,
    _blocked_snapshot_matches_account,
    _blocked_until_active,
    _capture_is_too_far_in_future,
    _current_supersedes_blocked_snapshot,
    _fetch_one,
    _finite_number,
    _has_unexpired_window_reset_discontinuity,
    _has_usable_core_usage,
    _is_more_conservative_direct_usage,
    _pool_forces_watchdog_block,
    _raw_number,
    _remaining_percent,
    _should_persist_snapshot,
    _stabilize_authenticated_usage,
    _stabilize_main_pool,
    _usage_map_for_accounts,
    _valid_percent,
    _watch_core_resets_current,
    _watch_cycle_is_healthy,
    _watchdog_windows,
    _window_duration_seconds,
    _window_is_exhausted,
    fetch_all,
    watch,
    watchdog,
)


@pytest.fixture(autouse=True)
def _isolate_scheduler_xdg_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for variable, directory in (
        ("XDG_DATA_HOME", "xdg-data"),
        ("XDG_STATE_HOME", "xdg-state"),
        ("XDG_CONFIG_HOME", "xdg-config"),
        ("XDG_CACHE_HOME", "xdg-cache"),
    ):
        monkeypatch.setenv(variable, str(tmp_path / directory))


class _RaisingTimezone(tzinfo):
    def utcoffset(self, _value):
        raise RuntimeError("synthetic timezone marker")


class _RaisingComparisonDatetime(datetime):
    def __le__(self, _other):
        raise RuntimeError("synthetic comparison marker")

    def __gt__(self, _other):
        raise RuntimeError("synthetic comparison marker")


class _RaisingSubtractionDatetime(datetime):
    def __sub__(self, _other):
        raise RuntimeError("synthetic subtraction marker")


class _BrokenInt(int):
    def __lt__(self, _other):
        raise RuntimeError("synthetic scheduler integer comparison marker")

    def __add__(self, _other):
        raise RuntimeError("synthetic scheduler integer addition marker")


class _BrokenFloat(float):
    def __float__(self):
        raise RuntimeError("synthetic scheduler float conversion marker")


class _RaisingPool:
    @property
    def available(self):
        raise RuntimeError("synthetic pool marker")

    allowed = True
    limit_reached = False


class _RaisingEvidencePool:
    @property
    def availability_sources(self):
        raise RuntimeError("synthetic evidence marker")


def test_scheduler_numeric_helpers_reject_subclasses_before_operations():
    assert _finite_number(_BrokenFloat(50.0)) is None
    assert _finite_number(_BrokenInt(50)) is None
    assert _valid_percent(_BrokenFloat(50.0)) is None
    assert _valid_percent(_BrokenInt(50)) is None


def test_scheduler_reset_validation_uses_named_duration_for_numeric_subclass():
    captured_at = datetime(2026, 8, 23, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="scheduler",
        label="Scheduler",
        captured_at=captured_at,
        status=AccountStatus.OK,
        main=_usable_main(
            LimitWindow(
                name="5h",
                remaining=80,
                duration_seconds=_BrokenInt(18_000),
                reset_at=captured_at + timedelta(hours=1),
            )
        ),
    )

    assert _watch_core_resets_current(usage, now=captured_at) is True


class _RaisingWindow:
    has_invalid_usage_value = False
    used = None
    limit = None
    remaining = None
    percent = None

    @property
    def remaining_percent(self):
        raise RuntimeError("synthetic window marker")


def _usable_main(*windows, availability_sources=("usage",)):
    return UsagePool(
        key="main",
        display_name="Codex",
        windows=tuple(windows),
        availability_sources=availability_sources,
    )


def test_block_state_treats_failing_reset_timezone_as_unknown():
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 8, 16, 10, 0),
        main=_usable_main(
            LimitWindow(
                name="5h",
                remaining=0,
                limit=100,
                reset_at=datetime(2026, 8, 16, 11, 0, tzinfo=_RaisingTimezone()),
            )
        ),
        status=AccountStatus.OK,
    )

    blocked_until, reason = _block_state(
        usage,
        now=datetime(2026, 8, 16, 10, 0),
    )

    assert blocked_until is None
    assert reason == "usage limit reached: 5h; reset time unknown"


def test_block_state_treats_failing_reset_comparison_as_unknown():
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 8, 16, 10, 0),
        main=_usable_main(
            LimitWindow(
                name="5h",
                remaining=0,
                limit=100,
                reset_at=_RaisingComparisonDatetime(2026, 8, 16, 11, tzinfo=ZoneInfo("UTC")),
            )
        ),
        status=AccountStatus.OK,
    )

    blocked_until, reason = _block_state(
        usage,
        now=datetime(2026, 8, 16, 10, 0, tzinfo=ZoneInfo("UTC")),
    )

    assert blocked_until is None
    assert reason == "usage limit reached: 5h; reset time unknown"


def test_watch_core_resets_current_treats_failing_reset_comparison_as_invalid():
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 8, 16, 10, 0),
        main=_usable_main(
            LimitWindow(
                name="5h",
                remaining=50,
                reset_at=_RaisingComparisonDatetime(
                    2026, 8, 16, 11, tzinfo=ZoneInfo("UTC")
                ),
            )
        ),
        status=AccountStatus.OK,
    )

    assert _watch_core_resets_current(
        usage,
        now=datetime(2026, 8, 16, 10, 0, tzinfo=ZoneInfo("UTC")),
    ) is False


def test_blocked_until_active_treats_failing_comparison_as_inactive():
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 8, 16, 10, 0),
        status=AccountStatus.BLOCKED,
        blocked_until=_RaisingComparisonDatetime(
            2026, 8, 16, 11, tzinfo=ZoneInfo("UTC")
        ),
    )

    assert _blocked_until_active(
        usage,
        now=datetime(2026, 8, 16, 10, 0, tzinfo=ZoneInfo("UTC")),
    ) is False


def test_capture_future_treats_failing_comparison_as_too_far():
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=_RaisingComparisonDatetime(
            2026, 8, 16, 10, tzinfo=ZoneInfo("UTC")
        ),
    )

    assert _capture_is_too_far_in_future(
        usage,
        datetime(2026, 8, 16, 10, tzinfo=ZoneInfo("UTC")),
    ) is True


def test_current_supersedes_blocked_snapshot_treats_failing_comparison_as_stale(
    monkeypatch,
):
    account = Account(id="account", label="Account", profile_dir="/tmp/account")
    blocked_snapshot = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 8, 16, 10, 0, tzinfo=ZoneInfo("UTC")),
        status=AccountStatus.BLOCKED,
    )
    current = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=_RaisingComparisonDatetime(
            2026, 8, 16, 11, tzinfo=ZoneInfo("UTC")
        ),
        status=AccountStatus.OK,
    )
    monkeypatch.setattr(
        "codex_usage.scheduler._blocked_snapshot_matches_account",
        lambda *_args, **_kwargs: True,
    )

    assert _current_supersedes_blocked_snapshot(
        account,
        blocked_snapshot,
        current,
        auth_json_path=None,
        configured_backend="direct",
        authenticated_fetch=False,
    ) is False


def test_authenticated_stabilization_treats_failing_capture_subtraction_as_unusable():
    timezone = ZoneInfo("UTC")
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 8, 16, 10, 0, tzinfo=timezone),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user-account",
        backend_account_id="account-id",
    )
    current = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=_RaisingSubtractionDatetime(
            2026, 8, 16, 10, 1, tzinfo=timezone
        ),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user-account",
        backend_account_id="account-id",
    )

    assert _stabilize_authenticated_usage(
        current,
        previous,
        max_age_seconds=300,
    ) is current


def test_reset_discontinuity_treats_failing_datetime_comparison_as_unknown():
    timezone = ZoneInfo("UTC")

    assert _has_unexpired_window_reset_discontinuity(
        LimitWindow(
            name="5h",
            reset_at=_RaisingComparisonDatetime(
                2026, 8, 16, 12, 0, tzinfo=timezone
            ),
        ),
        LimitWindow(
            name="5h",
            reset_at=datetime(2026, 8, 16, 11, 0, tzinfo=timezone),
        ),
        reference_at=datetime(2026, 8, 16, 10, 0, tzinfo=timezone),
    ) is False


def test_conservative_direct_usage_treats_failing_reset_comparison_as_unknown():
    timezone = ZoneInfo("UTC")
    current = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 8, 16, 10, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            reset_at=_RaisingComparisonDatetime(
                2026, 8, 16, 12, 0, tzinfo=timezone
            ),
        ),
    )
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 8, 16, 10, 0, tzinfo=timezone),
        five_hour=LimitWindow(
            name="5h",
            reset_at=datetime(2026, 8, 16, 11, 0, tzinfo=timezone),
        ),
    )

    assert _is_more_conservative_direct_usage(current, previous) is False


def test_conservative_direct_usage_skips_windows_without_reset_metadata():
    now = datetime.now().astimezone()
    current = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=now,
        five_hour=LimitWindow(name="5h"),
    )
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=now,
        five_hour=LimitWindow(name="5h"),
    )

    assert _is_more_conservative_direct_usage(current, previous) is False


def test_pool_forces_watchdog_block_treats_failing_property_as_exhausted():
    assert _pool_forces_watchdog_block(_RaisingPool()) is True


def test_window_is_exhausted_treats_failing_remaining_property_as_exhausted():
    assert _window_is_exhausted(_RaisingWindow()) is True


def test_watch_cycle_health_treats_failing_pool_evidence_as_unhealthy():
    account = Account(
        id="account",
        label="Account",
        profile_dir="/tmp/account",
        backend="browser",
    )
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        backend_configured="browser",
        backend_used="browser",
        main=_RaisingEvidencePool(),
    )

    assert _watch_cycle_is_healthy([usage], [account]) is False


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


def test_fetch_all_rejects_account_iterators_over_config_cap(monkeypatch):
    monkeypatch.setattr(scheduler_module, "MAX_SCHEDULER_ACCOUNTS", 2)
    accounts = (
        Account(id=f"account-{index}", label=str(index), profile_dir=f"/tmp/{index}")
        for index in range(3)
    )

    with pytest.raises(ValueError, match="too many accounts"):
        fetch_all(AppConfig(accounts=()), accounts)


@pytest.mark.parametrize("accounts", [None, "invalid", [None], [object()]])
def test_fetch_all_rejects_invalid_account_records(accounts):
    with pytest.raises(ValueError, match="account records are invalid"):
        fetch_all(AppConfig(accounts=()), accounts)  # type: ignore[arg-type]


def test_fetch_all_rejects_account_with_non_string_id():
    account = Account(
        id=[],  # type: ignore[arg-type]
        label="Malformed ID",
        profile_dir="/tmp/malformed-id",
    )

    with pytest.raises(ValueError, match="account records are invalid"):
        fetch_all(AppConfig(accounts=()), (account,))


def test_fetch_all_keeps_malformed_account_auth_path_as_usage_error(monkeypatch):
    account = Account(
        id="malformed-auth",
        label="Malformed auth",
        profile_dir="/tmp/malformed-auth",
        auth_json_path=1,  # type: ignore[arg-type]
    )
    monkeypatch.setattr("codex_usage.scheduler.load_state_generation", lambda _id: 0)

    result = fetch_all(AppConfig(accounts=(account,)), (account,), direct=True)

    assert result[0].status == AccountStatus.LOGIN_REQUIRED
    assert result[0].error == "auth.json path is invalid"


@pytest.mark.parametrize("config", [None, [], "invalid", object()])
def test_fetch_all_rejects_invalid_config(config):
    with pytest.raises(ValueError, match="config is invalid"):
        fetch_all(config, ())  # type: ignore[arg-type]


@pytest.mark.parametrize("auth_json_path", [[], "invalid", 1, object()])
def test_scheduler_rejects_invalid_auth_json_path(auth_json_path):
    config = AppConfig(accounts=())
    with pytest.raises(ValueError, match="auth_json_path is invalid"):
        fetch_all(config, (), auth_json_path=auth_json_path)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="auth_json_path is invalid"):
        watch(
            config,
            (),
            output="json",
            auth_json_path=auth_json_path,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="auth_json_path is invalid"):
        watchdog(
            config,
            (),
            output="json",
            auth_json_path=auth_json_path,  # type: ignore[arg-type]
        )


def test_scheduler_rejects_unknown_auth_home():
    with pytest.raises(ValueError, match="auth_json_path is invalid"):
        scheduler_module._validated_auth_json_path(
            Path("~definitely-no-such-user-zzzz/auth.json")
        )


def test_shared_direct_auth_accounts_handles_unknown_override_home(tmp_path):
    accounts = [
        Account(id="alpha", label="Alpha", profile_dir=str(tmp_path / "alpha")),
        Account(id="beta", label="Beta", profile_dir=str(tmp_path / "beta")),
    ]

    assert scheduler_module._shared_direct_auth_accounts(
        accounts,
        auth_json_path=Path("~definitely-no-such-user-zzzz/auth.json"),
    ) == frozenset({"alpha", "beta"})


def test_shared_direct_auth_accounts_handles_unknown_account_home():
    accounts = [
        Account(
            id="alpha",
            label="Alpha",
            profile_dir="/tmp/alpha",
            auth_json_path="~definitely-no-such-user-zzzz/auth.json",
        ),
        Account(
            id="beta",
            label="Beta",
            profile_dir="/tmp/beta",
            auth_json_path="~definitely-no-such-user-zzzz/auth.json",
        ),
    ]

    assert scheduler_module._shared_direct_auth_accounts(accounts) == frozenset(
        {"alpha", "beta"}
    )


def test_fetch_all_rejects_oversized_config_account_iterators(monkeypatch):
    monkeypatch.setattr(scheduler_module, "MAX_SCHEDULER_ACCOUNTS", 2)
    accounts = (
        Account(id=f"configured-{index}", label=str(index), profile_dir=f"/tmp/{index}")
        for index in range(3)
    )

    with pytest.raises(ValueError, match="too many accounts"):
        fetch_all(AppConfig(accounts=accounts), ())


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


def test_fetch_all_allows_single_account_auth_override(monkeypatch):
    accounts = (
        Account(id="privat", label="Privat", profile_dir="/tmp/privat"),
        Account(id="work", label="Work", profile_dir="/tmp/work"),
    )
    override = Path("/tmp/override-auth.json")
    seen: dict[str, object] = {}
    monkeypatch.setattr("codex_usage.scheduler.load_state_generation", lambda _account_id: 0)

    def fake_fetch_direct(
        account,
        *,
        auth_json_path=None,
        reject_ambiguous_backend_identity=False,
    ):
        seen["account"] = account.id
        seen["auth_json_path"] = auth_json_path
        seen["reject_ambiguous_backend_identity"] = reject_ambiguous_backend_identity
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=datetime.now().astimezone(),
            main=_usable_main(LimitWindow(name="5h", remaining=97)),
        )

    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_direct", fake_fetch_direct)

    result = fetch_all(
        AppConfig(accounts=accounts),
        (accounts[0],),
        direct=True,
        auth_json_path=override,
    )

    assert result[0].status == AccountStatus.OK
    assert seen == {
        "account": "privat",
        "auth_json_path": override,
        "reject_ambiguous_backend_identity": False,
    }


def test_fetch_all_rejects_unhashable_backend_before_stabilization(monkeypatch):
    account = Account(id="account", label="Account", profile_dir="/tmp/account")
    malformed = AccountUsage(
        account_id=account.id,
        label=account.label,
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used=[],
    )
    monkeypatch.setattr("codex_usage.scheduler.load_state_generation", lambda _id: 0)
    monkeypatch.setattr("codex_usage.scheduler._fetch_one", lambda *_args, **_kwargs: malformed)

    result = fetch_all(AppConfig(accounts=(account,)), (account,))

    assert result[0].backend_used == []


def test_fetch_all_auth_override_forces_direct_fetch_even_without_direct_flag(
    monkeypatch,
):
    account = Account(
        id="browser",
        label="Browser",
        profile_dir="/tmp/browser",
        backend="browser",
    )
    override = Path("/tmp/override-auth.json")
    calls: list[tuple[str, str, object]] = []

    monkeypatch.setattr("codex_usage.scheduler.load_state_generation", lambda _account_id: 0)

    def fake_fetch_direct(
        selected,
        *,
        auth_json_path=None,
        reject_ambiguous_backend_identity=False,
    ):
        calls.append(("direct", selected.id, auth_json_path))
        return AccountUsage(
            account_id=selected.id,
            label=selected.label,
            captured_at=datetime.now().astimezone(),
            status=AccountStatus.OK,
            backend_configured="direct",
            backend_used="direct",
            main=_usable_main(LimitWindow(name="5h", remaining=97)),
        )

    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_direct", fake_fetch_direct)
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_account_usage",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("browser path used")),
    )

    result = fetch_all(
        AppConfig(accounts=(account,)),
        (account,),
        auth_json_path=override,
    )

    assert calls == [("direct", "browser", override)]
    assert result[0].backend_configured == "direct"
    assert result[0].backend_used == "direct"


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
            main=_usable_main(
                LimitWindow(name="5h", remaining=97),
                LimitWindow(name="weekly", remaining=55),
            ),
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
        usage.error == "direct auth source cannot be attributed to one account" for usage in result
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
            main=_usable_main(LimitWindow(name="5h", remaining=97)),
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
            main=_usable_main(LimitWindow(name="5h", remaining=97)),
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
            main=_usable_main(LimitWindow(name="5h", remaining=80)),
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


def test_fetch_all_invalidates_usage_when_state_changes_during_fetch(monkeypatch):
    account = Account(id="race", label="Race", profile_dir="/tmp/race")
    generations = iter((0, 1))
    usage = AccountUsage(
        account_id="race",
        label="Race",
        captured_at=datetime.now().astimezone(),
        main=_usable_main(
            LimitWindow(name="5h", remaining=97),
            LimitWindow(name="weekly", remaining=55),
        ),
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
    )

    monkeypatch.setattr(
        "codex_usage.scheduler.load_state_generation",
        lambda _account_id: next(generations),
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_account_usage_direct",
        lambda selected, *, auth_json_path=None: usage,
    )

    result = fetch_all(
        AppConfig(accounts=(account,)),
        (account,),
        direct=True,
    )

    assert result[0].status == AccountStatus.ERROR
    assert result[0].error == "account state changed during fetch"
    assert result[0].five_hour is None
    assert result[0].weekly is None
    assert result[0].cache_invalidated is True
    assert result[0].stale is True


def test_fetch_all_invalidates_usage_when_state_generation_read_fails_after_fetch(
    monkeypatch,
):
    account = Account(id="race", label="Race", profile_dir="/tmp/race")
    generation_reads = 0
    usage = AccountUsage(
        account_id="race",
        label="Race",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.ERROR,
        error="transport warning",
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
    )

    def fail_second_generation_read(_account_id):
        nonlocal generation_reads
        generation_reads += 1
        if generation_reads == 1:
            return 0
        raise OSError("generation unavailable")

    monkeypatch.setattr(
        "codex_usage.scheduler.load_state_generation",
        fail_second_generation_read,
    )
    monkeypatch.setattr("codex_usage.scheduler._fetch_one", lambda *_args, **_kwargs: usage)

    result = fetch_all(
        AppConfig(accounts=(account,)),
        (account,),
        direct=True,
    )

    assert result[0].status == AccountStatus.ERROR
    assert result[0].error == (
        "state generation failed after fetch: OSError; transport warning"
    )
    assert result[0].five_hour is None
    assert result[0].weekly is None
    assert result[0].stale is True
    assert result[0].cache_invalidated is True


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


@pytest.mark.parametrize("interval_seconds", (-1, 0, 59, 60.0, True, _BrokenInt(60)))
def test_watch_rejects_invalid_interval(interval_seconds):
    with pytest.raises(ValueError, match="interval_seconds"):
        watch(
            AppConfig(accounts=()),
            (),
            output="table",
            interval_seconds=interval_seconds,
        )


def test_fetch_all_rejects_numeric_subclass_config_interval():
    with pytest.raises(ValueError, match="interval_seconds"):
        fetch_all(
            AppConfig(accounts=(), interval_seconds=_BrokenInt(300)),
            (),
        )


@pytest.mark.parametrize("config", [None, [], object()])
def test_watch_rejects_invalid_config(config):
    with pytest.raises(ValueError, match="config is invalid"):
        watch(config, (), output="table", interval_seconds=60)  # type: ignore[arg-type]


@pytest.mark.parametrize("config", [None, [], object()])
def test_watchdog_rejects_invalid_config(config):
    with pytest.raises(ValueError, match="config is invalid"):
        watchdog(config, (), output="json")  # type: ignore[arg-type]


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

    assert sorted(calls) == sorted([("direct", "direct", None), ("browser", "browser", False)])
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


def test_browser_failure_keeps_browser_provenance(monkeypatch):
    account = Account(
        id="browser-failure",
        label="Browser failure",
        profile_dir="/tmp/browser-failure",
        backend="direct",
    )

    def fail_fetch(*_args, **_kwargs):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage", fail_fetch)
    monkeypatch.setattr("codex_usage.scheduler.load_state_generation", lambda _id: 0)

    result = fetch_all(AppConfig(accounts=(account,)), (account,), headed=True)

    assert result[0].status == AccountStatus.ERROR
    assert result[0].backend_configured == "direct"
    assert result[0].backend_used == "browser"
    assert result[0].cache_invalidated is True


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


def test_fetch_all_records_history_after_current_snapshot(monkeypatch):
    account = Account(id="alpha", label="Alpha", profile_dir="/tmp/alpha")
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=datetime.now().astimezone(),
        five_hour=LimitWindow(name="5h", remaining=75),
        weekly=LimitWindow(name="weekly", remaining=50),
        backend_used="direct",
    )
    events: list[str] = []
    monkeypatch.setattr("codex_usage.scheduler._fetch_one", lambda *_args, **_kwargs: usage)
    monkeypatch.setattr("codex_usage.scheduler.load_state_generation", lambda *_args: 0)
    monkeypatch.setattr("codex_usage.scheduler.load_usage_snapshot", lambda *_args: None)
    monkeypatch.setattr(
        "codex_usage.scheduler.save_current_usage",
        lambda *_args: events.append("current"),
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.save_usage_snapshot",
        lambda *_args: events.append("snapshot"),
    )
    monkeypatch.setattr("codex_usage.scheduler.account_lock", lambda _account_id: nullcontext())
    monkeypatch.setattr(
        "codex_usage.scheduler.backend_provenance_matches_configured",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.record_usage_samples_batch",
        lambda values: events.extend("history" for _value in values),
    )

    result = fetch_all(
        AppConfig(accounts=(account,)),
        (account,),
        save_snapshots=True,
    )

    assert result[0].account_id == "alpha"
    assert events == ["current", "snapshot", "history"]


def test_fetch_all_keeps_usage_when_history_batch_recording_fails(monkeypatch):
    account = Account(id="alpha", label="Alpha", profile_dir="/tmp/alpha")
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        five_hour=LimitWindow(name="5h", remaining=75),
        weekly=LimitWindow(name="weekly", remaining=50),
        backend_configured="direct",
        backend_used="direct",
    )
    health_events: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr("codex_usage.scheduler._fetch_one", lambda *_args, **_kwargs: usage)
    monkeypatch.setattr("codex_usage.scheduler.load_state_generation", lambda *_args: 0)
    monkeypatch.setattr("codex_usage.scheduler.load_usage_snapshot", lambda *_args: None)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda *_args: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda *_args: None)
    monkeypatch.setattr("codex_usage.scheduler.account_lock", lambda _account_id: nullcontext())
    monkeypatch.setattr(
        "codex_usage.scheduler.backend_provenance_matches_configured",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.record_usage_samples_batch",
        lambda _values: (_ for _ in ()).throw(OSError("history is read-only")),
    )
    monkeypatch.setattr(
        "codex_usage.scheduler._record_health",
        lambda component, event, **kwargs: health_events.append(
            (component, event, kwargs["account"], kwargs["error_class"])
        ),
    )

    result = fetch_all(
        AppConfig(accounts=(account,)),
        (account,),
        save_snapshots=True,
    )

    assert result[0] == usage
    assert health_events == [("history", "sample_save_failed", "alpha", "OSError")]


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
    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_direct", fake_fetch_direct)

    def fake_account_lock(account_id, **_kwargs):
        locks.append(account_id)
        return nullcontext()

    monkeypatch.setattr("codex_usage.scheduler.account_lock", fake_account_lock)

    result = fetch_all(AppConfig(accounts=accounts), accounts)

    assert [usage.account_id for usage in result] == ["first", "second"]
    assert calls == ["first", "second"]
    assert locks == ["__all_accounts__", "first", "second"]


def test_fetch_all_parallelizes_visible_browser_multi_account_polls(monkeypatch):
    accounts = (
        Account(id="first", label="First", profile_dir="/tmp/first"),
        Account(id="second", label="Second", profile_dir="/tmp/second"),
    )
    calls: list[tuple[str, bool]] = []
    locks: list[str] = []

    executor_workers: list[int] = []

    class FakeExecutor:
        def __init__(self, *, max_workers):
            executor_workers.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def map(self, function, values):
            return [function(value) for value in values]

    def fake_fetch_browser(account, _config, *, headed):
        calls.append((account.id, headed))
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=datetime(2026, 6, 8, 4, 30, tzinfo=ZoneInfo("Europe/Berlin")),
        )

    monkeypatch.setattr("codex_usage.scheduler.ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage", fake_fetch_browser)

    def fake_account_lock(account_id, **_kwargs):
        locks.append(account_id)
        return nullcontext()

    monkeypatch.setattr("codex_usage.scheduler.account_lock", fake_account_lock)

    result = fetch_all(AppConfig(accounts=accounts), accounts, headed=True)

    assert [usage.account_id for usage in result] == ["first", "second"]
    assert calls == [("first", True), ("second", True)]
    assert executor_workers == [2]
    assert locks == ["first", "second"]


def test_fetch_all_parallelizes_headless_browser_multi_account_polls(monkeypatch):
    accounts = (
        Account(id="first", label="First", profile_dir="/tmp/first"),
        Account(id="second", label="Second", profile_dir="/tmp/second"),
    )
    calls: list[tuple[str, bool]] = []
    locks: list[str] = []
    executor_workers: list[int] = []

    class FakeExecutor:
        def __init__(self, *, max_workers):
            executor_workers.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def map(self, function, values):
            return [function(value) for value in values]

    def fake_fetch_browser(account, _config, *, headed):
        calls.append((account.id, headed))
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=datetime(2026, 6, 8, 4, 30, tzinfo=ZoneInfo("Europe/Berlin")),
        )

    monkeypatch.setattr("codex_usage.scheduler.ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage", fake_fetch_browser)

    def fake_account_lock(account_id, **_kwargs):
        locks.append(account_id)
        return nullcontext()

    monkeypatch.setattr("codex_usage.scheduler.account_lock", fake_account_lock)

    result = fetch_all(AppConfig(accounts=accounts), accounts, headed=False)

    assert [usage.account_id for usage in result] == ["first", "second"]
    assert calls == [("first", False), ("second", False)]
    assert executor_workers == [2]
    assert locks == ["first", "second"]


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


def test_fetch_all_persists_browser_account_with_auth_json_path(monkeypatch, tmp_path):
    expected_history_path = tmp_path / "xdg-data" / "codex-usage" / "usage-history.sqlite3"
    opened_history_paths: list[Path] = []
    real_store_init = history_module.HistoryStore.__init__

    def guarded_store_init(store, path=None):
        real_store_init(store, path)
        opened_history_paths.append(store.path)
        if store.path != expected_history_path:
            raise AssertionError("scheduler test attempted non-hermetic history path")

    monkeypatch.setattr(history_module.HistoryStore, "__init__", guarded_store_init)
    account = Account(
        id="account",
        label="Account",
        profile_dir="/tmp/account",
        backend="browser",
        auth_json_path=str(tmp_path / "auth.json"),
    )
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user-account",
        backend_account_id="account-id",
        five_hour=LimitWindow(name="five_hour", remaining=11),
        weekly=LimitWindow(name="weekly", remaining=22),
    )
    saved_current = []
    saved_snapshots = []
    monkeypatch.setattr(
        "codex_usage.scheduler.fetch_account_usage_direct",
        lambda selected, auth_json_path=None, reject_ambiguous_backend_identity=False: usage,
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
        auth_json_path=Path(account.auth_json_path),
        save_snapshots=True,
    )

    assert result[0].backend_used == "direct"
    assert result[0].backend_configured == "direct"
    assert saved_current == ["account"]
    assert saved_snapshots == ["account"]
    assert opened_history_paths == [expected_history_path]


def test_watchdog_does_not_persist_explicit_backend_override(monkeypatch):
    account = Account(
        id="account",
        label="Account",
        profile_dir="/tmp/account",
        backend="direct",
    )
    now = datetime.now().astimezone()
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=now,
        status=AccountStatus.OK,
        backend_configured="app-server",
        backend_used="app-server",
        backend_user_id="user-account",
        backend_account_id="account-id",
        main=_usable_main(
            LimitWindow(name="five_hour", remaining=11, reset_at=now + timedelta(hours=5)),
            LimitWindow(name="weekly", remaining=22, reset_at=now + timedelta(days=6)),
        ),
        five_hour=LimitWindow(name="five_hour", remaining=11, reset_at=now + timedelta(hours=5)),
        weekly=LimitWindow(name="weekly", remaining=22, reset_at=now + timedelta(days=6)),
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


def test_watchdog_persists_browser_account_with_auth_json_path(monkeypatch, tmp_path):
    account = Account(
        id="account",
        label="Account",
        profile_dir="/tmp/account",
        backend="browser",
        auth_json_path=str(tmp_path / "auth.json"),
    )
    now = datetime.now().astimezone()
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=now,
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user-account",
        backend_account_id="account-id",
        main=_usable_main(
            LimitWindow(name="five_hour", remaining=11, reset_at=now + timedelta(hours=5)),
            LimitWindow(name="weekly", remaining=22, reset_at=now + timedelta(days=6)),
        ),
        five_hour=LimitWindow(name="five_hour", remaining=11, reset_at=now + timedelta(hours=5)),
        weekly=LimitWindow(name="weekly", remaining=22, reset_at=now + timedelta(days=6)),
    )
    saved_current = []
    saved_snapshots = []
    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: None,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", lambda *args, **kwargs: [usage])
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
        auth_json_path=tmp_path / "auth.json",
    )

    assert result == [usage]
    assert saved_current == ["account"]
    assert saved_snapshots == ["account"]


def test_watchdog_downgrades_ok_usage_without_current_reset_and_persists_partial(
    monkeypatch,
):
    account = Account(
        id="account",
        label="Account",
        profile_dir="/tmp/account",
    )
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        main=_usable_main(
            LimitWindow(name="five_hour", remaining=11),
            LimitWindow(name="weekly", remaining=22),
        ),
        five_hour=LimitWindow(name="five_hour", remaining=11, reset_at=None),
        weekly=LimitWindow(name="weekly", remaining=22, reset_at=None),
    )
    saved_current = []
    saved_snapshots = []
    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: None,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", lambda *args, **kwargs: [usage])
    monkeypatch.setattr(
        "codex_usage.scheduler.save_current_usage",
        lambda selected: saved_current.append(selected),
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.save_usage_snapshot",
        lambda selected: saved_snapshots.append(selected),
    )

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
    )

    assert result[0].status == AccountStatus.PARTIAL
    assert result[0].error == "missing usage limits; refresh required"
    assert result[0].five_hour is None
    assert result[0].weekly is None
    assert result[0].main is None
    assert saved_current and saved_current[0].status == AccountStatus.PARTIAL
    assert saved_snapshots
    assert all(item.status == AccountStatus.PARTIAL for item in saved_snapshots)


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


def test_authenticated_stabilization_rejects_unhashable_previous_fallback_reason():
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(name="5h", remaining=90),
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user-account",
        backend_account_id="account-id",
        fallback_reason=[],
        stale=True,
    )
    current = replace(
        previous,
        captured_at=datetime(2026, 7, 12, 0, 1, tzinfo=timezone),
        five_hour=replace(previous.five_hour, remaining=99),
        fallback_reason=None,
        stale=False,
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=300)

    assert result is current


def test_authenticated_stabilization_rejects_unhashable_previous_backend():
    timezone = ZoneInfo("Europe/Berlin")
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone),
        five_hour=LimitWindow(name="5h", remaining=90),
        backend_configured="direct",
        backend_used=[],
        backend_user_id="user-account",
        backend_account_id="account-id",
    )
    current = replace(
        previous,
        captured_at=datetime(2026, 7, 12, 0, 1, tzinfo=timezone),
        backend_used="direct",
        five_hour=replace(previous.five_hour, remaining=99),
    )

    result = _stabilize_authenticated_usage(current, previous, max_age_seconds=300)

    assert result is current


@pytest.mark.parametrize("malformed", [object(), [], {}])
def test_reset_discontinuity_guard_rejects_malformed_windows(malformed):
    captured_at = datetime.now().astimezone()

    assert (
        _has_unexpired_window_reset_discontinuity(
            malformed,
            malformed,
            reference_at=captured_at,
        )
        is False
    )


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
            raw=('"limit_window_seconds": 18000, "reset_after_seconds": 17000'),
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
            raw=('"limit_window_seconds": 18000, "reset_after_seconds": 17000'),
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
            raw=('"limit_window_seconds": 18000, "reset_after_seconds": 18001'),
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


def test_scheduler_accepts_named_dynamic_reset_without_duration():
    captured_at = datetime.now().astimezone()
    usage = AccountUsage(
        account_id="dynamic",
        label="Dynamic",
        captured_at=captured_at,
        status=AccountStatus.OK,
        main=_usable_main(
            LimitWindow(
                name="30d",
                remaining=80,
                reset_at=captured_at + timedelta(days=10),
            )
        ),
    )

    assert _watch_core_resets_current(usage) is True


@pytest.mark.parametrize(
    ("status", "backend_used"),
    [
        (AccountStatus.PARTIAL, []),
        (AccountStatus.PARTIAL, {}),
        ([], "direct"),
        ({}, "direct"),
    ],
)
def test_scheduler_does_not_persist_unhashable_snapshot_fields(status, backend_used):
    usage = AccountUsage(
        account_id="dynamic",
        label="Dynamic",
        captured_at=datetime.now().astimezone(),
        status=status,
        backend_used=backend_used,
    )

    assert _should_persist_snapshot(usage) is False


def test_scheduler_rejects_named_dynamic_reset_without_timestamp():
    usage = AccountUsage(
        account_id="dynamic",
        label="Dynamic",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        main=_usable_main(LimitWindow(name="30d", remaining=80)),
    )

    assert _watch_core_resets_current(usage) is False


@pytest.mark.parametrize("windows", [None, [None]])
def test_scheduler_rejects_malformed_core_reset_windows(windows):
    usage = AccountUsage(
        account_id="dynamic",
        label="Dynamic",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=windows,  # type: ignore[arg-type]
        ),
    )

    assert _watch_core_resets_current(usage) is False


def test_scheduler_stabilization_skips_malformed_main_pool():
    previous = AccountUsage(
        account_id="dynamic",
        label="Dynamic",
        captured_at=datetime.now().astimezone(),
        five_hour=LimitWindow(name="5h", remaining=90),
    )
    current = UsagePool(
        key="main",
        display_name="Codex",
        windows=None,  # type: ignore[arg-type]
    )

    assert (
        _stabilize_main_pool(
            current,
            previous,
            retain_five_hour=True,
            retain_weekly=False,
        )
        is current
    )


@pytest.mark.parametrize("windows", [None, [None]])
def test_scheduler_watchdog_ignores_malformed_main_windows(windows):
    usage = AccountUsage(
        account_id="dynamic",
        label="Dynamic",
        captured_at=datetime.now().astimezone(),
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=windows,  # type: ignore[arg-type]
        ),
    )

    assert _watchdog_windows(usage) == ()


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
        main=_usable_main(
            LimitWindow(name="5h", remaining=97, reset_at=now + timedelta(hours=5)),
            LimitWindow(name="weekly", remaining=55, reset_at=now + timedelta(days=6)),
        ),
        five_hour=LimitWindow(name="5h", remaining=97, reset_at=now + timedelta(hours=5)),
        weekly=LimitWindow(name="weekly", remaining=55, reset_at=now + timedelta(days=6)),
        backend_user_id="shared-user",
        backend_account_id="default-account",
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
    monkeypatch.setattr(
        "codex_usage.scheduler.datetime",
        type(
            "Clock",
            (datetime,),
            {"now": classmethod(lambda cls, tz=None: now.astimezone(tz) if tz else now)},
        ),
    )
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
        main=_usable_main(LimitWindow(name="5h", remaining=97, reset_at=now + timedelta(hours=5))),
        five_hour=LimitWindow(name="5h", remaining=97, reset_at=now + timedelta(hours=5)),
        backend_user_id="shared-user",
        backend_account_id="default-account",
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
        "codex_usage.scheduler.load_current_usage",
        lambda account_id, current_dir=None: None,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)
    monkeypatch.setattr(
        "codex_usage.scheduler.datetime",
        type(
            "Clock",
            (datetime,),
            {"now": classmethod(lambda cls, tz=None: now.astimezone(tz) if tz else now)},
        ),
    )

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
        direct=False,
    )

    assert fetched_accounts == ["blocked"]
    assert result == [fresh]


def test_watchdog_direct_rechecks_default_auth_identity_for_block(
    monkeypatch,
):
    account = Account(id="blocked", label="Blocked", profile_dir="/tmp/blocked")
    now = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
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
        main=_usable_main(LimitWindow(name="5h", remaining=97, reset_at=now + timedelta(hours=5))),
        five_hour=LimitWindow(name="5h", remaining=97, reset_at=now + timedelta(hours=5)),
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

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
            return value.astimezone(tz) if tz else value

    monkeypatch.setattr("codex_usage.scheduler.datetime", Clock)

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
            main=_usable_main(
                LimitWindow(name="5h", remaining=80, reset_at=now + timedelta(hours=5)),
                LimitWindow(
                    name="weekly",
                    remaining=80,
                    reset_at=now + timedelta(days=6),
                ),
            ),
            five_hour=LimitWindow(name="5h", remaining=80, reset_at=now + timedelta(hours=5)),
            weekly=LimitWindow(name="weekly", remaining=80, reset_at=now + timedelta(days=6)),
            backend_user_id="shared-user",
            backend_account_id="default-account",
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
        lambda account_id, snapshot_dir=None: blocked_snapshot if account_id == "broken" else None,
    )
    monkeypatch.setattr("codex_usage.scheduler.load_current_usage", lambda *args: None)
    monkeypatch.setattr(
        "codex_usage.scheduler.load_state_generation",
        fake_load_state_generation,
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)
    monkeypatch.setattr(
        "codex_usage.scheduler.datetime",
        type(
            "Clock",
            (datetime,),
            {"now": classmethod(lambda cls, tz=None: now.astimezone(tz) if tz else now)},
        ),
    )

    result = watchdog(
        AppConfig(accounts=accounts),
        accounts,
        output="json",
        direct=True,
    )

    assert seen_fetch_accounts == ["broken", "healthy"]
    assert [usage.status for usage in result] == [AccountStatus.OK, AccountStatus.OK]


def test_watchdog_refetches_block_when_state_generation_changes(monkeypatch):
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
        backend_used="browser",
        main=_usable_main(LimitWindow(name="5h", remaining=97, reset_at=now + timedelta(hours=5))),
        five_hour=LimitWindow(name="5h", remaining=97, reset_at=now + timedelta(hours=5)),
        backend_user_id="shared-user",
        backend_account_id="default-account",
    )
    seen_fetch_accounts: list[str] = []
    generations = iter((0, 1))

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
        selected = tuple(fetch_accounts)
        seen_fetch_accounts.extend(account.id for account in selected)
        return [fresh] if selected else []

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: blocked_snapshot,
    )
    monkeypatch.setattr("codex_usage.scheduler.load_current_usage", lambda *args: None)
    monkeypatch.setattr(
        "codex_usage.scheduler.load_state_generation",
        lambda account_id: next(generations),
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)
    monkeypatch.setattr(
        "codex_usage.scheduler.datetime",
        type(
            "Clock",
            (datetime,),
            {"now": classmethod(lambda cls, tz=None: now.astimezone(tz) if tz else now)},
        ),
    )

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
    )

    assert seen_fetch_accounts == ["blocked"]
    assert result == [fresh]


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
    now = datetime.now().astimezone()
    blocked_until = now + timedelta(hours=2)
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
        main=_usable_main(
            LimitWindow(name="5h", remaining=99, reset_at=now + timedelta(hours=5)),
            LimitWindow(name="weekly", remaining=98, reset_at=now + timedelta(days=6)),
        ),
        five_hour=LimitWindow(name="5h", remaining=99, reset_at=now + timedelta(hours=5)),
        weekly=LimitWindow(name="weekly", remaining=98, reset_at=now + timedelta(days=6)),
        backend_user_id="shared-user",
        backend_account_id="default-account",
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
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="shared-user",
        backend_account_id="default-account",
        main=_usable_main(
            LimitWindow(name="5h", remaining=99, reset_at=now + timedelta(hours=5)),
            LimitWindow(name="weekly", remaining=95, reset_at=now + timedelta(days=6)),
        ),
        five_hour=LimitWindow(name="5h", remaining=99, reset_at=now + timedelta(hours=5)),
        weekly=LimitWindow(name="weekly", remaining=95, reset_at=now + timedelta(days=6)),
    )

    class Clock(datetime):
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
        main=_usable_main(
            LimitWindow(name="5h", remaining=99, reset_at=now + timedelta(hours=5)),
            LimitWindow(name="weekly", remaining=98, reset_at=now + timedelta(days=6)),
        ),
        five_hour=LimitWindow(name="5h", remaining=99, reset_at=now + timedelta(hours=5)),
        weekly=LimitWindow(name="weekly", remaining=98, reset_at=now + timedelta(days=6)),
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
        main=_usable_main(
            LimitWindow(name="5h", remaining=97, reset_at=now + timedelta(hours=5)),
            LimitWindow(name="weekly", remaining=55, reset_at=now + timedelta(days=6)),
        ),
        five_hour=LimitWindow(name="5h", remaining=97, reset_at=now + timedelta(hours=5)),
        weekly=LimitWindow(name="weekly", remaining=55, reset_at=now + timedelta(days=6)),
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
        main=_usable_main(
            LimitWindow(
                name="5h", remaining=99, reset_at=datetime(2026, 6, 8, 9, 21, tzinfo=timezone)
            ),
            LimitWindow(
                name="weekly", remaining=98, reset_at=datetime(2026, 6, 14, 4, 21, tzinfo=timezone)
            ),
        ),
        five_hour=LimitWindow(
            name="5h", remaining=99, reset_at=datetime(2026, 6, 8, 9, 21, tzinfo=timezone)
        ),
        weekly=LimitWindow(
            name="weekly", remaining=98, reset_at=datetime(2026, 6, 14, 4, 21, tzinfo=timezone)
        ),
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
    monkeypatch.setattr(
        "codex_usage.scheduler.datetime",
        type(
            "Clock",
            (datetime,),
            {
                "now": classmethod(
                    lambda cls, tz=None: (
                        datetime(2026, 6, 8, 4, 22, tzinfo=timezone).astimezone(tz)
                        if tz
                        else datetime(2026, 6, 8, 4, 22, tzinfo=timezone)
                    )
                )
            },
        ),
    )

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
    )

    assert fetched_accounts == ["blocked"]
    assert result == [refreshed]


def test_watchdog_does_not_supersede_active_block_with_stale_current(monkeypatch):
    account = Account(id="blocked", label="Blocked", profile_dir="/tmp/blocked")
    timezone = ZoneInfo("Europe/Berlin")
    blocked_snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=timezone),
        status=AccountStatus.BLOCKED,
        blocked_until=datetime(2026, 6, 8, 10, 20, tzinfo=timezone),
        blocked_reason="usage limit reached: weekly",
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="shared-user",
        backend_account_id="default-account",
        five_hour=LimitWindow(
            name="5h", remaining=0, reset_at=datetime(2026, 6, 8, 9, 20, tzinfo=timezone)
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=0,
            reset_at=datetime(2026, 6, 8, 10, 20, tzinfo=timezone),
        ),
    )
    stale_current = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime(2026, 6, 8, 4, 21, tzinfo=timezone),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="shared-user",
        backend_account_id="default-account",
        stale=True,
        main=_usable_main(
            LimitWindow(
                name="5h", remaining=99, reset_at=datetime(2026, 6, 8, 9, 30, tzinfo=timezone)
            ),
            LimitWindow(
                name="weekly", remaining=95, reset_at=datetime(2026, 6, 8, 10, 30, tzinfo=timezone)
            ),
        ),
    )
    stale_fetches: list[str] = []

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: blocked_snapshot,
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.load_current_usage",
        lambda *args, **kwargs: stale_current,
    )

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
        stale_fetches.append([account.id for account in fetch_accounts])
        return [blocked_snapshot]

    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)
    monkeypatch.setattr(
        "codex_usage.scheduler.datetime",
        type(
            "Clock",
            (datetime,),
            {
                "now": classmethod(
                    lambda cls, tz=None: (
                        datetime(2026, 6, 8, 4, 20, tzinfo=timezone).astimezone(tz)
                        if tz
                        else datetime(2026, 6, 8, 4, 20, tzinfo=timezone)
                    )
                )
            },
        ),
    )

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
        direct=False,
    )

    assert result[0].status == AccountStatus.BLOCKED
    assert result[0].blocked_until == blocked_snapshot.blocked_until
    assert all(not fetch_accounts for fetch_accounts in stale_fetches)


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
        main=_usable_main(
            LimitWindow(
                name="5h", remaining=99, reset_at=datetime.now().astimezone() + timedelta(hours=5)
            ),
            LimitWindow(
                name="weekly",
                remaining=95,
                reset_at=datetime.now().astimezone() + timedelta(days=6),
            ),
        ),
        five_hour=LimitWindow(
            name="5h", remaining=99, reset_at=datetime.now().astimezone() + timedelta(hours=5)
        ),
        weekly=LimitWindow(
            name="weekly", remaining=95, reset_at=datetime.now().astimezone() + timedelta(days=6)
        ),
        backend_user_id="shared-user",
        backend_account_id="private-account",
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
        main=_usable_main(
            LimitWindow(
                name="5h", remaining=99, reset_at=datetime.now().astimezone() + timedelta(hours=5)
            ),
            LimitWindow(
                name="weekly",
                remaining=95,
                reset_at=datetime.now().astimezone() + timedelta(days=6),
            ),
        ),
        five_hour=LimitWindow(
            name="5h", remaining=99, reset_at=datetime.now().astimezone() + timedelta(hours=5)
        ),
        weekly=LimitWindow(
            name="weekly", remaining=95, reset_at=datetime.now().astimezone() + timedelta(days=6)
        ),
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
        main=_usable_main(
            LimitWindow(
                name="5h", remaining=99, reset_at=datetime.now().astimezone() + timedelta(hours=5)
            ),
            LimitWindow(
                name="weekly",
                remaining=95,
                reset_at=datetime.now().astimezone() + timedelta(days=6),
            ),
        ),
        five_hour=LimitWindow(
            name="5h", remaining=99, reset_at=datetime.now().astimezone() + timedelta(hours=5)
        ),
        weekly=LimitWindow(
            name="weekly", remaining=95, reset_at=datetime.now().astimezone() + timedelta(days=6)
        ),
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
        main=_usable_main(
            LimitWindow(
                name="5h", remaining=99, reset_at=datetime.now().astimezone() + timedelta(hours=5)
            ),
            LimitWindow(
                name="weekly",
                remaining=95,
                reset_at=datetime.now().astimezone() + timedelta(days=6),
            ),
        ),
        five_hour=LimitWindow(
            name="5h", remaining=99, reset_at=datetime.now().astimezone() + timedelta(hours=5)
        ),
        weekly=LimitWindow(
            name="weekly", remaining=95, reset_at=datetime.now().astimezone() + timedelta(days=6)
        ),
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
        main=_usable_main(
            LimitWindow(
                name="5h", remaining=99, reset_at=datetime.now().astimezone() + timedelta(hours=5)
            ),
            LimitWindow(
                name="weekly",
                remaining=95,
                reset_at=datetime.now().astimezone() + timedelta(days=6),
            ),
        ),
        five_hour=LimitWindow(
            name="5h", remaining=99, reset_at=datetime.now().astimezone() + timedelta(hours=5)
        ),
        weekly=LimitWindow(
            name="weekly", remaining=95, reset_at=datetime.now().astimezone() + timedelta(days=6)
        ),
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


def test_watchdog_auth_override_forces_direct_fetch_for_browser_block(
    tmp_path,
    monkeypatch,
):
    account = Account(
        id="blocked",
        label="Blocked",
        profile_dir=str(tmp_path / "profile"),
        backend="browser",
    )
    override = tmp_path / "override-auth.json"
    blocked_snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        status=AccountStatus.BLOCKED,
        blocked_until=datetime(2099, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_configured="browser",
        backend_used="browser",
        backend_user_id="shared-user",
        backend_account_id="account-new",
    )
    fresh_usage = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        main=_usable_main(
            LimitWindow(
                name="5h", remaining=99, reset_at=datetime.now().astimezone() + timedelta(hours=5)
            ),
            LimitWindow(
                name="weekly",
                remaining=95,
                reset_at=datetime.now().astimezone() + timedelta(days=6),
            ),
        ),
        five_hour=LimitWindow(
            name="5h", remaining=99, reset_at=datetime.now().astimezone() + timedelta(hours=5)
        ),
        weekly=LimitWindow(
            name="weekly", remaining=95, reset_at=datetime.now().astimezone() + timedelta(days=6)
        ),
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="shared-user",
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
        seen_fetch_accounts.extend(selected.id for selected in fetch_accounts)
        assert auth_json_path == override
        assert direct is False
        return [fresh_usage]

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: blocked_snapshot,
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.load_current_usage",
        lambda account_id, current_dir=None: None,
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_from_file",
        lambda path: ("shared-user", "account-new"),
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
        auth_json_path=override,
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
        main=_usable_main(
            LimitWindow(
                name="5h",
                remaining=0,
                reset_at=datetime(2026, 6, 8, 4, 21, 30, tzinfo=ZoneInfo("Europe/Berlin")),
            ),
            LimitWindow(
                name="weekly",
                remaining=99,
                reset_at=datetime(2026, 6, 14, 4, 20, 30, tzinfo=ZoneInfo("Europe/Berlin")),
            ),
        ),
        five_hour=LimitWindow(
            name="5h",
            remaining=0,
            reset_at=datetime(2026, 6, 8, 4, 21, 30, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=99,
            reset_at=datetime(2026, 6, 14, 4, 20, 30, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
    )
    clock_values = iter((before_fetch, after_fetch))

    class Clock(datetime):
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

    assert result[0].status == AccountStatus.BLOCKED
    assert result[0].blocked_reason is not None


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
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="shared-user",
        backend_account_id="default-account",
        main=_usable_main(
            LimitWindow(
                name="5h",
                remaining=99,
                reset_at=datetime(2026, 6, 8, 9, 21, tzinfo=ZoneInfo("Europe/Berlin")),
            ),
            LimitWindow(
                name="weekly",
                remaining=98,
                reset_at=datetime(2026, 6, 14, 4, 21, tzinfo=ZoneInfo("Europe/Berlin")),
            ),
        ),
        five_hour=LimitWindow(
            name="5h",
            remaining=99,
            reset_at=datetime(2026, 6, 8, 9, 21, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
        weekly=LimitWindow(
            name="weekly",
            remaining=98,
            reset_at=datetime(2026, 6, 14, 4, 21, tzinfo=ZoneInfo("Europe/Berlin")),
        ),
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
    load_current_calls: list[str] = []

    class Clock(datetime):
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

    def fake_load_current_usage(account_id, *args, **kwargs):
        load_current_calls.append(account_id)
        return None

    monkeypatch.setattr("codex_usage.scheduler.datetime", Clock)
    monkeypatch.setattr("codex_usage.scheduler.load_usage_snapshot", fake_load_usage_snapshot)
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        "codex_usage.scheduler.load_current_usage",
        fake_load_current_usage,
    )
    monkeypatch.setattr("codex_usage.scheduler.save_current_usage", lambda usage: None)
    monkeypatch.setattr("codex_usage.scheduler.save_usage_snapshot", lambda usage: None)

    result = watchdog(
        AppConfig(accounts=accounts),
        accounts,
        output="json",
    )

    assert seen_fetch_accounts == [["free"], ["blocked"]]
    assert load_current_calls == ["blocked"]
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

    assert (
        _window_is_exhausted(LimitWindow(name="5h", used=0, limit=100, remaining=100, percent=0))
        is False
    )


def test_window_exhaustion_prefers_absolute_usage_over_conflicting_remaining():
    from codex_usage.scheduler import _window_is_exhausted

    assert (
        _window_is_exhausted(
            LimitWindow(name="5h", used=100, limit=100, remaining=100, percent=100)
        )
        is True
    )


def test_window_exhaustion_interprets_remaining_against_absolute_limit():
    assert _window_is_exhausted(LimitWindow(name="weekly", limit=1000, remaining=101)) is False


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


def test_watchdog_rejects_malformed_legacy_window_without_crashing():
    usage = AccountUsage(
        account_id="malformed-legacy-window",
        label="Malformed legacy window",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        five_hour=[],  # type: ignore[arg-type]
    )

    rejected = _apply_watchdog_block(
        usage,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert rejected.status == AccountStatus.BLOCKED
    assert rejected.error == "usage limit reached: unknown; reset time unknown"


@pytest.mark.parametrize("main", [[], {}, "malformed"])
def test_scheduler_usable_core_usage_rejects_malformed_main(main):
    usage = AccountUsage(
        account_id="malformed-main",
        label="Malformed main",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        main=main,  # type: ignore[arg-type]
    )

    assert _has_usable_core_usage(usage) is False


def test_scheduler_usable_core_usage_rejects_malformed_legacy_window():
    usage = AccountUsage(
        account_id="malformed-legacy-window",
        label="Malformed legacy window",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        five_hour=object(),  # type: ignore[arg-type]
    )

    assert _has_usable_core_usage(usage) is False


@pytest.mark.parametrize("availability_sources", [("model_catalog",), ("legacy_fields",), (), None])
def test_watchdog_rejects_ok_main_usage_without_usable_usage_provenance(
    availability_sources,
):
    usage = AccountUsage(
        account_id="missing",
        label="Missing",
        captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(LimitWindow(name="weekly", remaining=80),),
            availability_sources=availability_sources,
        ),
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


@pytest.mark.parametrize(
    "available, availability_sources, expected",
    [
        (True, ("usage",), True),
        (True, ("usage", "model_catalog"), True),
        (True, ("model_catalog",), False),
        (True, (), False),
        (False, ("usage",), False),
    ],
)
def test_watch_cycle_health_requires_usage_provenance_for_main_pool(
    available,
    availability_sources,
    expected,
):
    account = Account(id="dynamic", label="Dynamic", profile_dir="/tmp/dynamic")
    captured_at = datetime.now(ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="dynamic",
        label="Dynamic",
        captured_at=captured_at,
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        main=UsagePool(
            key="main",
            display_name="Codex",
            available=available,
            windows=(
                LimitWindow(
                    name="weekly",
                    remaining=80,
                    reset_at=captured_at + timedelta(days=6),
                ),
            ),
            availability_sources=availability_sources,
        ),
    )

    assert _watch_cycle_is_healthy([usage], [account]) is expected


def test_watch_cycle_health_accepts_explicit_backend_override():
    account = Account(id="direct", label="Direct", profile_dir="/tmp/direct")
    captured_at = datetime.now(ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=captured_at,
        status=AccountStatus.OK,
        backend_configured="app-server",
        backend_used="app-server",
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(
                LimitWindow(
                    name="weekly",
                    remaining=80,
                    reset_at=captured_at + timedelta(days=6),
                ),
            ),
            availability_sources=("usage",),
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


def test_watch_cycle_health_auth_json_path_forces_direct_backend():
    account = Account(id="browser", label="Browser", profile_dir="/tmp/browser", backend="browser")
    captured_at = datetime.now(ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="browser",
        label="Browser",
        captured_at=captured_at,
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(
                LimitWindow(
                    name="5h",
                    remaining=97,
                    reset_at=captured_at + timedelta(hours=5),
                ),
            ),
            availability_sources=("usage",),
        ),
    )

    assert _watch_cycle_is_healthy(
        [usage],
        [account],
        auth_json_path=Path("/tmp/auth.json"),
    ) is True


def test_watch_cycle_health_auth_json_path_reduces_browser_account_fail_closed_without_match():
    account = Account(id="browser", label="Browser", profile_dir="/tmp/browser", backend="browser")
    captured_at = datetime.now(ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="browser",
        label="Browser",
        captured_at=captured_at,
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(
                LimitWindow(
                    name="5h",
                    remaining=97,
                    reset_at=captured_at + timedelta(hours=5),
                ),
            ),
            availability_sources=("usage",),
        ),
    )

    assert _watch_cycle_is_healthy([usage], [account]) is False


def test_watch_cycle_health_accepts_browser_usage_pool():
    account = Account(id="browser", label="Browser", profile_dir="/tmp/browser", backend="browser")
    captured_at = datetime.now(ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="browser",
        label="Browser",
        captured_at=captured_at,
        status=AccountStatus.OK,
        backend_configured="browser",
        backend_used="browser",
        backend_user_id="user-1",
        backend_account_id="account-1",
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(
                LimitWindow(name="5h", remaining=97, reset_at=captured_at + timedelta(hours=5)),
                LimitWindow(
                    name="weekly",
                    remaining=55,
                    reset_at=captured_at + timedelta(days=6),
                ),
            ),
            availability_sources=("usage", "browser"),
        ),
    )

    assert _watch_cycle_is_healthy([usage], [account]) is True


def test_watch_cycle_health_rejects_missing_core_reset_timestamp():
    account = Account(id="browser", label="Browser", profile_dir="/tmp/browser", backend="browser")
    usage = AccountUsage(
        account_id="browser",
        label="Browser",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
        status=AccountStatus.OK,
        backend_configured="browser",
        backend_used="browser",
        backend_user_id="user-1",
        backend_account_id="account-1",
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(
                LimitWindow(name="5h", remaining=97),
                LimitWindow(name="weekly", remaining=55),
            ),
            availability_sources=("usage", "browser"),
        ),
    )

    assert _watch_cycle_is_healthy([usage], [account]) is False


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


def test_watch_cycle_health_rejects_implausibly_future_core_reset():
    account = Account(id="direct", label="Direct", profile_dir="/tmp/direct")
    captured_at = datetime.now(ZoneInfo("Europe/Berlin"))
    usage = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=captured_at,
        status=AccountStatus.OK,
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(
                LimitWindow(
                    name="weekly",
                    remaining=80,
                    reset_at=captured_at + timedelta(days=8),
                ),
            ),
            availability_sources=("usage",),
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


def test_usage_validation_rejects_iterator_value_errors():
    account = Account(id="direct", label="Direct", profile_dir="/tmp/direct")
    usage = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
    )

    def broken_usages():
        yield usage
        raise ValueError("synthetic usage iterator marker")

    assert _usage_map_for_accounts(broken_usages(), [account]) is None
    assert _watch_cycle_is_healthy(broken_usages(), [account]) is False


def test_usage_validation_stops_after_expected_count_plus_overflow():
    account = Account(id="direct", label="Direct", profile_dir="/tmp/direct")
    usage = AccountUsage(
        account_id="direct",
        label="Direct",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
    )

    def overlong_usages():
        yield usage
        yield usage
        raise AssertionError("usage validation consumed beyond overflow point")

    assert _usage_map_for_accounts(overlong_usages(), [account]) is None
    assert _watch_cycle_is_healthy(overlong_usages(), [account]) is False


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


def test_app_server_fallback_preserves_ambiguous_identity_guard(monkeypatch):
    accounts = tuple(
        Account(
            id=account_id,
            label=account_id.title(),
            profile_dir=f"/tmp/{account_id}",
            auth_json_path=f"/tmp/{account_id}/auth.json",
            backend="app-server",
        )
        for account_id in ("work", "personal")
    )
    captured = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    reject_flags: dict[str, bool] = {}

    def unavailable(_selected):
        raise AppServerUnavailableError("unsupported")

    def direct(
        selected,
        auth_json_path=None,
        *,
        reject_ambiguous_backend_identity=False,
    ):
        reject_flags[selected.id] = reject_ambiguous_backend_identity
        return AccountUsage(
            account_id=selected.id,
            label=selected.label,
            captured_at=captured,
        )

    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_app_server", unavailable)
    monkeypatch.setattr("codex_usage.scheduler.fetch_account_usage_direct", direct)
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_for_account",
        lambda selected: ("shared-user", selected.id),
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_plan_type_for_account",
        lambda _selected: None,
    )
    monkeypatch.setattr("codex_usage.scheduler.account_lock", lambda _account_id: nullcontext())
    monkeypatch.setattr("codex_usage.scheduler.load_state_generation", lambda _account_id: 0)
    monkeypatch.setattr("codex_usage.scheduler.load_usage_snapshot", lambda _account_id: None)

    result = fetch_all(AppConfig(accounts=accounts), accounts)

    assert reject_flags == {"work": True, "personal": True}
    assert all(usage.backend_used == "direct" for usage in result)
    assert all(
        usage.fallback_reason == "app-server unavailable: unsupported"
        for usage in result
    )


def test_watchdog_refuses_user_only_authenticated_blocked_snapshot_match(tmp_path, monkeypatch):
    account = Account(
        id="blocked",
        label="Blocked",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )
    snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.BLOCKED,
        backend_configured="direct",
        backend_used="browser",
        backend_user_id="shared-user",
        blocked_until=datetime.now().astimezone() + timedelta(hours=2),
    )

    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_from_file",
        lambda path: ("shared-user", None),
    )

    assert _blocked_snapshot_matches_account(
        account,
        snapshot,
        auth_json_path=tmp_path / "auth.json",
        configured_backend="direct",
        authenticated_fetch=True,
    ) is False


def test_watchdog_refuses_user_only_snapshot_when_auth_has_account_id(
    tmp_path, monkeypatch
):
    account = Account(
        id="blocked",
        label="Blocked",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )
    snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.BLOCKED,
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="shared-user",
        blocked_until=datetime.now().astimezone() + timedelta(hours=2),
    )

    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_for_account",
        lambda selected: ("shared-user", "account-current"),
    )

    assert _blocked_snapshot_matches_account(
        account,
        snapshot,
        auth_json_path=None,
        configured_backend="direct",
        authenticated_fetch=True,
    ) is False


def test_watchdog_rejects_unhashable_snapshot_account_identity(tmp_path, monkeypatch):
    account = Account(
        id="blocked",
        label="Blocked",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )
    snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.BLOCKED,
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="shared-user",
        backend_account_id=["account-current"],
    )

    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_from_file",
        lambda path: ("shared-user", "account-current"),
    )

    assert _blocked_snapshot_matches_account(
        account,
        snapshot,
        auth_json_path=tmp_path / "auth.json",
        configured_backend="direct",
        authenticated_fetch=True,
    ) is False


def test_watchdog_refuses_user_only_browser_block_with_auth_json_override(tmp_path, monkeypatch):
    account = Account(
        id="blocked",
        label="Blocked",
        profile_dir=str(tmp_path / "profile"),
    )
    blocked_until = datetime.now().astimezone() + timedelta(hours=2)
    blocked_snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.BLOCKED,
        backend_configured="direct",
        backend_used="browser",
        backend_user_id="shared-user",
        blocked_until=blocked_until,
        five_hour=LimitWindow(
            name="5h",
            remaining=0,
            limit=100,
            used=100,
            percent=0,
            reset_at=blocked_until,
        ),
    )
    fresh_usage = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        main=_usable_main(
            LimitWindow(name="5h", remaining=99, reset_at=blocked_until + timedelta(hours=3)),
        ),
        five_hour=LimitWindow(name="5h", remaining=99, reset_at=blocked_until + timedelta(hours=3)),
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
        return [fresh_usage]

    monkeypatch.setattr(
        "codex_usage.scheduler.load_usage_snapshot",
        lambda account_id, snapshot_dir=None: blocked_snapshot,
    )
    monkeypatch.setattr(
        "codex_usage.scheduler.auth_identity_from_file",
        lambda path: ("shared-user", "account-current"),
    )
    monkeypatch.setattr("codex_usage.scheduler.fetch_all", fake_fetch_all)

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
        auth_json_path=tmp_path / "auth-override.json",
    )

    assert fetched_accounts == ["blocked"]
    assert result == [fresh_usage]


def test_watchdog_keeps_browser_account_match_without_auth_identity():
    account = Account(
        id="browser-account",
        label="Browser Account",
        profile_dir="/tmp/profile",
        backend="browser",
    )
    snapshot = AccountUsage(
        account_id="browser-account",
        label="Browser Account",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.BLOCKED,
        backend_configured="browser",
        backend_used="browser",
        backend_user_id="shared-user",
        blocked_until=datetime.now().astimezone() + timedelta(hours=2),
    )

    assert _blocked_snapshot_matches_account(
        account,
        snapshot,
        auth_json_path=None,
        configured_backend="browser",
        authenticated_fetch=False,
    ) is True


def test_scheduler_ambiguous_identity_skips_accounts_with_different_users(monkeypatch):
    accounts = [
        Account(
            id="one",
            label="One",
            profile_dir="/tmp/one",
            auth_json_path="/tmp/one-auth.json",
        ),
        Account(
            id="two",
            label="Two",
            profile_dir="/tmp/two",
            auth_json_path="/tmp/two-auth.json",
        ),
    ]
    identities = {"one": ("user-one", "account-one"), "two": ("user-two", "account-two")}
    monkeypatch.setattr(
        scheduler_module,
        "auth_identity_for_account",
        lambda account: identities[account.id],
    )
    monkeypatch.setattr(scheduler_module, "auth_plan_type_for_account", lambda _account: None)

    assert _ambiguous_direct_accounts(accounts) == frozenset()


def test_scheduler_shared_auth_path_falls_back_to_string_when_path_methods_fail(
    monkeypatch,
):
    account = Account(
        id="account",
        label="Account",
        profile_dir="/tmp/account",
        auth_json_path="/tmp/auth.json",
    )
    expand_calls = 0

    def expanduser(_path):
        nonlocal expand_calls
        expand_calls += 1
        if expand_calls == 2:
            raise RuntimeError("home unavailable")
        return _path

    monkeypatch.setattr(Path, "expanduser", expanduser)
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda _path, strict=False: (_ for _ in ()).throw(ValueError("resolve failed")),
    )

    assert scheduler_module._shared_direct_auth_accounts([account]) == frozenset()


def test_scheduler_unattributed_direct_error_keeps_previous_detail():
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now().astimezone(),
        error="transport warning",
    )

    rejected = scheduler_module._reject_unattributed_direct_usage(usage)

    assert rejected.status is AccountStatus.ERROR
    assert rejected.error == (
        "direct auth source cannot be attributed to one account; transport warning"
    )


def test_scheduler_stabilization_rejects_backend_transition_without_fallback_proof():
    captured = datetime(2026, 8, 23, 10, 0, tzinfo=ZoneInfo("UTC"))
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=captured,
        status=AccountStatus.OK,
        backend_configured="app-server",
        backend_used="app-server",
        backend_user_id="user",
        backend_account_id="account-id",
    )
    current = replace(
        previous,
        captured_at=captured + timedelta(minutes=1),
        backend_used="direct",
    )

    assert _stabilize_authenticated_usage(current, previous, max_age_seconds=300) is current


def test_scheduler_stabilization_rejects_capture_older_than_previous():
    captured = datetime(2026, 8, 23, 10, 0, tzinfo=ZoneInfo("UTC"))
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=captured,
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user",
        backend_account_id="account-id",
    )
    current = replace(previous, captured_at=captured + timedelta(hours=1))

    assert _stabilize_authenticated_usage(current, previous, max_age_seconds=300) is current


def test_scheduler_stabilization_does_not_reuse_confirmed_app_server_fallback():
    captured = datetime(2026, 8, 23, 10, 0, tzinfo=ZoneInfo("UTC"))
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=captured,
        status=AccountStatus.OK,
        backend_configured="app-server",
        backend_used="app-server",
        backend_user_id="user",
        backend_account_id="account-id",
        fallback_reason=scheduler_module.AUTHENTICATED_RESET_FALLBACK_REASON,
        stale=True,
    )
    current = replace(
        previous,
        captured_at=captured + timedelta(minutes=1),
        stale=False,
        five_hour=LimitWindow(name="5h", remaining=99),
    )

    assert _stabilize_authenticated_usage(current, previous, max_age_seconds=300) is current


@pytest.mark.parametrize("current", [None, object()])
def test_scheduler_stabilize_main_pool_rejects_non_pool(current):
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now().astimezone(),
    )

    assert _stabilize_main_pool(
        current,
        previous,
        retain_five_hour=True,
        retain_weekly=True,
    ) is current


def test_scheduler_stabilize_main_pool_appends_fallback_window_and_keeps_equal_pool():
    captured = datetime(2026, 8, 23, 10, 0, tzinfo=ZoneInfo("UTC"))
    previous_window = LimitWindow(name="5h", remaining=80)
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=captured,
        five_hour=previous_window,
        main=UsagePool(key="main", display_name="Codex", windows=()),
    )
    current = UsagePool(key="main", display_name="Codex", windows=())
    appended = _stabilize_main_pool(
        current,
        previous,
        retain_five_hour=True,
        retain_weekly=False,
    )
    assert appended.windows == (previous_window,)

    equal = UsagePool(key="main", display_name="Codex", windows=(previous_window,))
    assert _stabilize_main_pool(
        equal,
        previous,
        retain_five_hour=True,
        retain_weekly=False,
    ) is equal


def test_scheduler_stabilize_main_pool_skips_missing_fallback_window():
    previous = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now().astimezone(),
        main=UsagePool(key="main", display_name="Codex", windows=()),
    )
    current = UsagePool(key="main", display_name="Codex", windows=())

    assert _stabilize_main_pool(
        current,
        previous,
        retain_five_hour=True,
        retain_weekly=False,
    ) is current


def test_scheduler_reset_discontinuity_rejects_expired_unknown_and_absolute_resets():
    reference = datetime(2026, 8, 23, 10, 0, tzinfo=ZoneInfo("UTC"))
    expired = LimitWindow(name="5h", reset_at=reference)
    future_unknown = LimitWindow(name="unknown", reset_at=reference + timedelta(hours=1))
    assert (
        _has_unexpired_window_reset_discontinuity(
            expired,
            expired,
            reference_at=reference,
        )
        is False
    )
    assert (
        _has_unexpired_window_reset_discontinuity(
            future_unknown,
            future_unknown,
            reference_at=reference,
        )
        is False
    )
    absolute = replace(future_unknown, source="app-server")
    assert (
        _has_unexpired_window_reset_discontinuity(
            absolute,
            future_unknown,
            reference_at=reference,
        )
        is False
    )


def test_scheduler_reset_discontinuity_rejects_relative_metadata_and_countdown(monkeypatch):
    reference = datetime(2026, 8, 23, 10, 0, tzinfo=ZoneInfo("UTC"))
    malformed = LimitWindow(
        name="5h",
        reset_at=reference + timedelta(hours=1),
        raw='{"limit_window_seconds": 18000, "reset_after_seconds": "bad"}',
    )
    valid = replace(
        malformed,
        raw='{"limit_window_seconds": 18000, "reset_after_seconds": 17900}',
    )
    assert scheduler_module._has_relative_reset_metadata(malformed) is True
    assert scheduler_module._uses_relative_reset_time(valid) is True
    assert (
        _has_unexpired_window_reset_discontinuity(
            malformed,
            malformed,
            reference_at=reference,
        )
        is False
    )
    assert (
        _has_unexpired_window_reset_discontinuity(
            valid,
            valid,
            reference_at=reference,
        )
        is False
    )

    absolute = replace(
        valid,
        name="5h",
        source="app-server",
        raw=None,
    )
    assert (
        _has_unexpired_window_reset_discontinuity(
            absolute,
            valid,
            reference_at=reference,
        )
        is False
    )

    monkeypatch.setattr(
        scheduler_module,
        "_has_relative_reset_metadata",
        lambda _window: False,
    )
    assert (
        _has_unexpired_window_reset_discontinuity(
            valid,
            valid,
            reference_at=reference,
        )
        is False
    )


def test_scheduler_raw_number_and_duration_helpers_fail_closed(monkeypatch):
    assert _raw_number("{}", "limit_window_seconds") is None
    assert _window_duration_seconds(LimitWindow(name="5h", raw="{}")) is None

    monkeypatch.setattr(
        scheduler_module,
        "float",
        lambda _value: (_ for _ in ()).throw(ValueError("bad number")),
        raising=False,
    )
    assert _raw_number('{"limit_window_seconds": 18000}', "limit_window_seconds") is None
    assert _window_duration_seconds(
        LimitWindow(name="5h", raw='{"limit_window_seconds": 18000}')
    ) is None


def test_scheduler_should_retain_window_requires_two_percent_values():
    reference = datetime(2026, 8, 23, 10, 0, tzinfo=ZoneInfo("UTC"))
    current = SimpleNamespace(
        name="5h",
        raw=None,
        reset_at=reference + timedelta(hours=1),
        has_invalid_usage_value=False,
        used=None,
        limit=None,
        remaining=None,
        percent=None,
    )
    previous = SimpleNamespace(
        name="5h",
        raw=None,
        reset_at=reference + timedelta(hours=2),
        has_invalid_usage_value=False,
        used=None,
        limit=None,
        remaining=None,
        percent=None,
    )

    assert (
        scheduler_module._should_retain_previous_window(
            current,
            previous,
            reference_at=reference,
        )
        is False
    )


@pytest.mark.parametrize(
    ("current", "previous"),
    [
        (LimitWindow(name="5h"), LimitWindow(name="")),
        (
            LimitWindow(name="5h", raw='{"limit_window_seconds": 18000}'),
            LimitWindow(name="", raw='{"limit_window_seconds": 18000}'),
        ),
        (
            LimitWindow(name="5h", raw='{"limit_window_seconds": 18000}'),
            LimitWindow(name="weekly", raw='{"limit_window_seconds": 604800}'),
        ),
        (
            LimitWindow(name="5h", raw='{"limit_window_seconds": 604800}'),
            LimitWindow(name="5h", raw='{"limit_window_seconds": 18000}'),
        ),
    ],
)
def test_scheduler_window_duration_matching_rejects_identity_and_duration_mismatches(
    current,
    previous,
):
    assert scheduler_module._window_duration_matches(current, previous) is False


def test_scheduler_conservative_usage_skips_missing_windows():
    now = datetime.now().astimezone()
    current = AccountUsage(account_id="account", label="Account", captured_at=now)
    previous = AccountUsage(account_id="account", label="Account", captured_at=now)

    assert _is_more_conservative_direct_usage(current, previous) is False


def test_scheduler_remaining_percent_covers_invalid_and_absolute_fallbacks():
    def window(**values):
        fields = dict(
            has_invalid_usage_value=False,
            used=None,
            limit=None,
            remaining=None,
            percent=None,
        )
        fields.update(values)
        return SimpleNamespace(**fields)

    assert _remaining_percent(window(used="bad", limit=100)) is None
    assert _remaining_percent(window(used=-1, limit=100)) is None
    assert _remaining_percent(window(limit=0, remaining=0)) is None
    assert _remaining_percent(window(limit=100, remaining=50)) == 50
    assert _remaining_percent(window(limit=100, remaining=101)) is None
    assert _remaining_percent(window(remaining=101)) is None
    assert _remaining_percent(window(percent=50)) == 50


def test_scheduler_finite_number_maps_conversion_errors(monkeypatch):
    monkeypatch.setattr(
        scheduler_module,
        "float",
        lambda _value: (_ for _ in ()).throw(OverflowError("too large")),
        raising=False,
    )

    assert _finite_number(1) is None


def test_scheduler_usage_map_rejects_invalid_accounts_and_result_shapes():
    account = Account(id="account", label="Account", profile_dir="/tmp/account")
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now().astimezone(),
    )

    assert _usage_map_for_accounts([usage], [object()]) is None  # type: ignore[list-item]
    assert _usage_map_for_accounts([object()], [account]) is None  # type: ignore[list-item]
    duplicate_account = Account(
        id="account",
        label="Duplicate",
        profile_dir="/tmp/duplicate",
    )
    other_usage = replace(usage, account_id="other")
    assert _usage_map_for_accounts([usage, other_usage], [account, duplicate_account]) is None


def test_scheduler_usage_map_rejects_unhashable_result_identity():
    usage = AccountUsage(
        account_id=[],  # type: ignore[arg-type]
        label="Account",
        captured_at=datetime.now().astimezone(),
    )
    account = Account(id="account", label="Account", profile_dir="/tmp/account")

    assert _usage_map_for_accounts([usage], [account]) is None


def test_scheduler_watch_core_resets_legacy_windows_and_rejects_malformed_shapes():
    now = datetime(2026, 8, 23, 10, 0, tzinfo=ZoneInfo("UTC"))
    legacy = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=now,
        five_hour=LimitWindow(
            name="5h",
            remaining=80,
            reset_at=now + timedelta(hours=1),
        ),
    )
    assert _watch_core_resets_current(legacy, now=now) is True

    legacy_without_main = AccountUsage(
        account_id="legacy",
        label="Legacy",
        captured_at=now,
    )
    object.__setattr__(
        legacy_without_main,
        "five_hour",
        LimitWindow(name="5h", remaining=80, reset_at=now + timedelta(hours=1)),
    )
    assert _watch_core_resets_current(legacy_without_main, now=now) is True

    malformed_pool = replace(
        legacy,
        main=UsagePool(key="main", display_name="Codex", windows=(object(),)),  # type: ignore[arg-type]
    )
    assert _watch_core_resets_current(malformed_pool, now=now) is False

    naive = replace(
        legacy,
        main=None,
        five_hour=LimitWindow(name="5h", remaining=80, reset_at=datetime(2026, 8, 23, 11, 0)),
    )
    assert _watch_core_resets_current(naive, now=now) is False

    expired = replace(
        legacy,
        main=None,
        five_hour=LimitWindow(name="5h", remaining=80, reset_at=now),
    )
    assert _watch_core_resets_current(expired, now=now) is False


def test_scheduler_failure_usages_ignore_invalid_attempted_iterable():
    account = Account(id="account", label="Account", profile_dir="/tmp/account")

    failures = scheduler_module._watch_failure_usages(
        [account],
        object(),  # type: ignore[arg-type]
        error="fallback",
    )

    assert failures[0].error == "fallback"


def test_scheduler_watch_json_success_handles_keyboard_interrupt_and_signal_guards(
    monkeypatch,
    capsys,
):
    class StopAfterWait:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _delay):
            self.stopped = True
            return True

        def set(self):
            self.stopped = True

    signal_calls = []

    def signal_call(signum, handler):
        signal_calls.append((signum, handler))
        if len(signal_calls) > 2:
            raise OSError("restore failed")

    monkeypatch.setattr(scheduler_module, "Event", StopAfterWait)
    monkeypatch.setattr(scheduler_module, "signal", SimpleNamespace(
        SIGINT=signal.SIGINT,
        SIGTERM=signal.SIGTERM,
        getsignal=lambda _signum: "previous",
        signal=signal_call,
    ))
    monkeypatch.setattr(scheduler_module, "fetch_all", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(scheduler_module, "render_json", lambda _usages: "[]")

    watch(AppConfig(accounts=()), (), output="json", interval_seconds=60)
    signal_calls[0][1](None, None)

    assert capsys.readouterr().out == "[]\n"
    assert len(signal_calls) == 4


def test_scheduler_watch_skips_cycle_when_already_stopped(monkeypatch):
    class AlreadyStopped:
        def is_set(self):
            return True

        def wait(self, _delay):
            raise AssertionError("wait called after stop")

    signal_calls = []
    monkeypatch.setattr(scheduler_module, "Event", AlreadyStopped)
    monkeypatch.setattr(scheduler_module.signal, "getsignal", lambda _signum: "previous")
    monkeypatch.setattr(
        scheduler_module.signal,
        "signal",
        lambda signum, handler: signal_calls.append((signum, handler)),
    )
    monkeypatch.setattr(
        scheduler_module,
        "fetch_all",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fetch called after stop")),
    )

    watch(AppConfig(accounts=()), (), output="json", interval_seconds=60)

    assert len(signal_calls) == 4


def test_scheduler_watch_runs_again_after_non_stopping_wait(monkeypatch):
    class ContinueOnce:
        def __init__(self):
            self.wait_calls = 0

        def is_set(self):
            return self.wait_calls >= 2

        def wait(self, _delay):
            self.wait_calls += 1
            return self.wait_calls >= 2

        def set(self):
            return None

    event = ContinueOnce()
    fetch_calls = 0
    monkeypatch.setattr(scheduler_module, "Event", lambda: event)
    monkeypatch.setattr(scheduler_module.signal, "getsignal", lambda _signum: "previous")
    monkeypatch.setattr(scheduler_module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(scheduler_module, "render_json", lambda _usages: "[]")
    monkeypatch.setattr(scheduler_module, "_record_health", lambda *_args, **_kwargs: None)

    def fake_fetch_all(*_args, **_kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        return []

    monkeypatch.setattr(scheduler_module, "fetch_all", fake_fetch_all)

    watch(AppConfig(accounts=()), (), output="json", interval_seconds=60)

    assert fetch_calls == 2
    assert event.wait_calls == 2


def test_scheduler_watch_handles_signal_install_failure(monkeypatch):
    class StopAfterWait:
        def is_set(self):
            return False

        def wait(self, _delay):
            return True

        def set(self):
            return None

    monkeypatch.setattr(scheduler_module, "Event", StopAfterWait)
    monkeypatch.setattr(scheduler_module.signal, "getsignal", lambda _signum: "previous")
    monkeypatch.setattr(
        scheduler_module.signal,
        "signal",
        lambda *_args: (_ for _ in ()).throw(OSError("install failed")),
    )
    monkeypatch.setattr(scheduler_module, "fetch_all", lambda *_args, **_kwargs: [])

    watch(AppConfig(accounts=()), (), output="table", interval_seconds=60)


def test_scheduler_watch_handles_keyboard_interrupt(monkeypatch):
    class StopAfterInterrupt:
        def __init__(self):
            self.set_called = False

        def is_set(self):
            return False

        def wait(self, _delay):
            return True

        def set(self):
            self.set_called = True

    event = StopAfterInterrupt()
    monkeypatch.setattr(scheduler_module, "Event", lambda: event)
    monkeypatch.setattr(
        scheduler_module,
        "fetch_all",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(scheduler_module.signal, "getsignal", lambda _signum: "previous")
    monkeypatch.setattr(scheduler_module.signal, "signal", lambda *_args: None)

    watch(AppConfig(accounts=()), (), output="table", interval_seconds=60)

    assert event.set_called is True


def test_scheduler_watchdog_refetches_when_block_generation_read_fails(monkeypatch):
    account = Account(
        id="blocked",
        label="Blocked",
        profile_dir="/tmp/blocked",
        backend="direct",
    )
    snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.BLOCKED,
        backend_configured="direct",
        backend_used="direct",
        blocked_until=datetime.now().astimezone() + timedelta(hours=1),
        state_generation=1,
    )
    fetched: list[str] = []

    monkeypatch.setattr(scheduler_module, "load_usage_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(scheduler_module, "load_current_usage", lambda *_args: None)
    monkeypatch.setattr(scheduler_module, "_blocked_until_active", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        scheduler_module,
        "_blocked_snapshot_is_consistent",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        scheduler_module,
        "_blocked_snapshot_matches_account",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        scheduler_module,
        "_current_supersedes_blocked_snapshot",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        scheduler_module,
        "load_state_generation",
        lambda *_args: (_ for _ in ()).throw(ValueError("generation corrupt")),
    )

    def fake_fetch(_config, selected, **_kwargs):
        fetched.extend(account.id for account in selected)
        return []

    monkeypatch.setattr(scheduler_module, "fetch_all", fake_fetch)
    monkeypatch.setattr(scheduler_module, "save_current_usage", lambda *_args: None)
    monkeypatch.setattr(scheduler_module, "save_usage_snapshot", lambda *_args: None)

    watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
        direct=True,
    )

    assert fetched == ["blocked"]


def test_scheduler_watchdog_refetches_block_when_generation_changes(monkeypatch):
    account = Account(
        id="blocked",
        label="Blocked",
        profile_dir="/tmp/blocked",
        backend="direct",
    )
    snapshot = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.BLOCKED,
        backend_configured="direct",
        backend_used="direct",
        blocked_until=datetime.now().astimezone() + timedelta(hours=1),
    )
    fresh = AccountUsage(
        account_id="blocked",
        label="Blocked",
        captured_at=datetime.now().astimezone(),
        backend_configured="direct",
        backend_used="direct",
        main=_usable_main(
            LimitWindow(
                name="5h",
                remaining=80,
                reset_at=datetime.now().astimezone() + timedelta(hours=1),
            ),
            availability_sources=("usage",),
        ),
    )
    selected: list[list[str]] = []

    monkeypatch.setattr(scheduler_module, "load_usage_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(scheduler_module, "load_current_usage", lambda *_args: None)
    monkeypatch.setattr(scheduler_module, "_blocked_until_active", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        scheduler_module,
        "_blocked_snapshot_is_consistent",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        scheduler_module,
        "_blocked_snapshot_matches_account",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        scheduler_module,
        "_current_supersedes_blocked_snapshot",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(scheduler_module, "load_state_generation", lambda *_args: 7)
    monkeypatch.setattr(
        scheduler_module,
        "_blocked_snapshot_generation_is_current",
        lambda *_args: False,
    )

    def fake_fetch(_config, accounts, **_kwargs):
        chosen = [item.id for item in accounts]
        selected.append(chosen)
        return [fresh] if chosen else []

    monkeypatch.setattr(scheduler_module, "fetch_all", fake_fetch)
    monkeypatch.setattr(scheduler_module, "save_current_usage", lambda *_args: None)
    monkeypatch.setattr(scheduler_module, "save_usage_snapshot", lambda *_args: None)

    result = watchdog(AppConfig(accounts=(account,)), (account,), output="json")

    assert selected == [[], ["blocked"]]
    assert result == [fresh]


def test_scheduler_watchdog_skips_account_without_fetched_usage(monkeypatch):
    account = Account(id="missing", label="Missing", profile_dir="/tmp/missing")
    monkeypatch.setattr(scheduler_module, "load_usage_snapshot", lambda *_args: None)
    monkeypatch.setattr(scheduler_module, "load_current_usage", lambda *_args: None)
    monkeypatch.setattr(scheduler_module, "fetch_all", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(scheduler_module, "_usage_map_for_accounts", lambda *_args: {})

    assert watchdog(AppConfig(accounts=(account,)), (account,), output="json") == []


def test_scheduler_watchdog_contains_snapshot_save_failure(monkeypatch):
    account = Account(
        id="account",
        label="Account",
        profile_dir="/tmp/account",
        backend="direct",
    )
    captured = datetime.now().astimezone()
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=captured,
        backend_configured="direct",
        backend_used="direct",
        main=_usable_main(
            LimitWindow(name="5h", remaining=80, reset_at=captured + timedelta(hours=1)),
            availability_sources=("usage",),
        ),
    )
    monkeypatch.setattr(scheduler_module, "load_usage_snapshot", lambda *_args: None)
    monkeypatch.setattr(scheduler_module, "load_current_usage", lambda *_args: None)
    monkeypatch.setattr(scheduler_module, "fetch_all", lambda *_args, **_kwargs: [usage])
    monkeypatch.setattr(
        scheduler_module,
        "save_current_usage",
        lambda *_args: (_ for _ in ()).throw(OSError("read-only")),
    )

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
        direct=True,
    )

    assert result[0].status is AccountStatus.ERROR
    assert result[0].error == "snapshot save failed: OSError"


def test_scheduler_watchdog_skips_snapshot_for_non_persistable_usage(monkeypatch):
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
        status=AccountStatus.ERROR,
        error="backend unavailable",
        backend_configured="direct",
        backend_used="direct",
    )
    saved_current: list[AccountUsage] = []
    monkeypatch.setattr(scheduler_module, "load_usage_snapshot", lambda *_args: None)
    monkeypatch.setattr(scheduler_module, "load_current_usage", lambda *_args: None)
    monkeypatch.setattr(scheduler_module, "fetch_all", lambda *_args, **_kwargs: [usage])
    monkeypatch.setattr(scheduler_module, "save_current_usage", saved_current.append)
    monkeypatch.setattr(
        scheduler_module,
        "save_usage_snapshot",
        lambda *_args: (_ for _ in ()).throw(AssertionError("snapshot should be skipped")),
    )
    monkeypatch.setattr(scheduler_module, "_should_persist_snapshot", lambda _usage: False)

    result = watchdog(
        AppConfig(accounts=(account,)),
        (account,),
        output="json",
        direct=True,
    )

    assert result == [usage]
    assert saved_current == [usage]


def test_scheduler_blocked_snapshot_helpers_fail_closed(monkeypatch):
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.BLOCKED,
        blocked_until=datetime.now().astimezone() + timedelta(hours=1),
        five_hour=LimitWindow(name="5h", remaining=0),
        state_generation=1,
    )
    monkeypatch.setattr(
        scheduler_module,
        "_block_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad block")),
    )
    assert (
        scheduler_module._blocked_snapshot_is_consistent(
            usage,
            now=datetime.now().astimezone(),
        )
        is False
    )

    monkeypatch.setattr(
        scheduler_module,
        "load_state_generation",
        lambda *_args: (_ for _ in ()).throw(OSError("state unavailable")),
    )
    assert scheduler_module._blocked_snapshot_generation_is_current(usage, "account") is False

    assert _capture_is_too_far_in_future(None, datetime.now().astimezone()) is False


def test_scheduler_blocked_snapshot_matching_rejects_unbound_browser_and_missing_auth(
    monkeypatch,
):
    account = Account(id="browser", label="Browser", profile_dir="/tmp/browser")
    browser_snapshot = AccountUsage(
        account_id="browser",
        label="Browser",
        captured_at=datetime.now().astimezone(),
        backend_configured="direct",
        backend_used="browser",
        backend_account_id="account-id",
    )
    assert (
        _blocked_snapshot_matches_account(
            account,
            browser_snapshot,
            auth_json_path=None,
            configured_backend="direct",
            authenticated_fetch=False,
        )
        is False
    )

    monkeypatch.setattr(
        scheduler_module,
        "auth_identity_from_file",
        lambda _path: ("user", None),
    )
    assert (
        _blocked_snapshot_matches_account(
            account,
            replace(browser_snapshot, backend_used="direct"),
            auth_json_path=None,
            configured_backend="direct",
            authenticated_fetch=True,
        )
        is False
    )


def test_scheduler_blocked_snapshot_matching_uses_default_auth_identity(monkeypatch):
    account = Account(id="account", label="Account", profile_dir="/tmp/account")
    snapshot = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now().astimezone(),
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user",
        backend_account_id="account-id",
    )
    monkeypatch.setattr(
        scheduler_module,
        "auth_identity_from_file",
        lambda _path: ("user", "account-id"),
    )

    assert (
        _blocked_snapshot_matches_account(
            account,
            snapshot,
            auth_json_path=None,
            configured_backend="direct",
            authenticated_fetch=True,
        )
        is True
    )


def test_scheduler_effective_backend_returns_none_without_account():
    assert (
        scheduler_module._fetch_effective_backend(
            None,
            direct=False,
            backend_override=None,
            auth_json_path=None,
        )
        is None
    )


def test_scheduler_block_state_fails_closed_for_unavailable_pool_without_windows():
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now().astimezone(),
        main=UsagePool(
            key="main",
            display_name="Codex",
            available=False,
            windows=(),
        ),
    )

    assert _block_state(usage, now=datetime.now().astimezone()) == (
        None,
        "usage limit reached: main; reset time unknown",
    )


class _NeverEqualDatetime(datetime):
    def __eq__(self, _other):
        return False


def test_scheduler_block_state_handles_unmatched_active_window_name():
    reset_at = _NeverEqualDatetime(2099, 8, 23, 12, tzinfo=ZoneInfo("UTC"))
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime(2026, 8, 23, 10, tzinfo=ZoneInfo("UTC")),
        main=_usable_main(
            LimitWindow(name="5h", remaining=0, reset_at=reset_at),
        ),
    )

    blocked_until, reason = _block_state(
        usage,
        now=datetime(2026, 8, 23, 10, tzinfo=ZoneInfo("UTC")),
    )

    assert blocked_until is reset_at
    assert reason == "usage limit reached; release at 2099-08-23T12:00:00+00:00"


def test_scheduler_block_state_releases_expired_exhausted_window():
    now = datetime(2026, 8, 23, 10, tzinfo=ZoneInfo("UTC"))
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=now,
        main=_usable_main(
            LimitWindow(name="5h", remaining=0, reset_at=now),
        ),
    )

    assert _block_state(usage, now=now) == (None, None)


def test_scheduler_window_exhaustion_fail_closed_matrix():
    class RawWindow:
        has_invalid_usage_value = False
        used = None
        limit = None
        remaining = None
        percent = None
        remaining_percent = 50

    assert _window_is_exhausted(None) is False
    assert _window_is_exhausted(SimpleNamespace(
        has_invalid_usage_value=False,
        used="bad",
        limit=None,
        remaining=None,
        percent=None,
        remaining_percent=50,
    )) is True
    assert _window_is_exhausted(SimpleNamespace(
        has_invalid_usage_value=False,
        used=-1,
        limit=100,
        remaining=None,
        percent=None,
        remaining_percent=50,
    )) is True
    assert _window_is_exhausted(SimpleNamespace(
        has_invalid_usage_value=False,
        used=None,
        limit=0,
        remaining=None,
        percent=None,
        remaining_percent=50,
    )) is True
    assert _window_is_exhausted(SimpleNamespace(
        has_invalid_usage_value=False,
        used=None,
        limit=None,
        remaining=-1,
        percent=None,
        remaining_percent=50,
    )) is True
    assert _window_is_exhausted(SimpleNamespace(
        has_invalid_usage_value=False,
        used=None,
        limit=None,
        remaining=101,
        percent=50,
        remaining_percent=50,
    )) is False
    assert _window_is_exhausted(SimpleNamespace(
        has_invalid_usage_value=False,
        used=None,
        limit=None,
        remaining=101,
        percent=101,
        remaining_percent=50,
    )) is True
    assert _window_is_exhausted(RawWindow()) is True
