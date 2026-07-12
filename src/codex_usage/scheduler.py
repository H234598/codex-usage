from __future__ import annotations

import signal
import sys
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any

from .account_lock import account_lock
from .app_server import AppServerUnavailableError, fetch_account_usage_app_server
from .browser import fetch_account_usage
from .config import AppConfig
from .direct import (
    DirectAuthError,
    auth_identity_for_account,
    auth_identity_from_file,
    fetch_account_usage_direct,
)
from .health import record_health_event
from .models import Account, AccountStatus, AccountUsage
from .render import render_json, render_table
from .state import (
    backend_identity_matches,
    load_usage_snapshot,
    save_current_usage,
    save_usage_snapshot,
)

AUTHENTICATED_BACKENDS = frozenset(("direct", "app-server"))
DIRECT_RESET_DISCONTINUITY_SECONDS = 30
LEGACY_DIRECT_RESET_FALLBACK_REASON = "previous direct limits retained after reset transition"
AUTHENTICATED_RESET_FALLBACK_REASON = (
    "previous authenticated limits retained after reset transition"
)
REUSABLE_RESET_FALLBACK_REASONS = frozenset(
    (LEGACY_DIRECT_RESET_FALLBACK_REASON, AUTHENTICATED_RESET_FALLBACK_REASON)
)


def fetch_all(
    config: AppConfig,
    accounts: Iterable[Account],
    *,
    headed: bool = False,
    direct: bool = False,
    backend_override: str | None = None,
    auth_json_path: Path | None = None,
    save_snapshots: bool = False,
) -> list[AccountUsage]:
    account_list = list(accounts)
    serial_fetch_required = _serial_fetch_required(
        account_list,
        headed=headed,
        direct=direct,
        backend_override=backend_override,
    )

    def fetch(account: Account) -> AccountUsage:
        usage = _fetch_one(
            config,
            account,
            headed=headed,
            direct=direct,
            backend_override=backend_override,
            auth_json_path=auth_json_path if (direct or auth_json_path is not None) else None,
            global_lock_held=serial_fetch_required,
        )
        if usage.status != AccountStatus.OK or usage.backend_used not in AUTHENTICATED_BACKENDS:
            return usage
        previous = load_usage_snapshot(account.id)
        return _stabilize_authenticated_usage(
            usage,
            previous,
            max_age_seconds=max(int(config.interval_seconds), 60) + 60,
        )

    if serial_fetch_required:
        # The authenticated usage endpoints can return a shared/cached bucket
        # when multiple account requests overlap. Keep the whole poll cycle
        # exclusive, including separate codex-usage processes.
        with account_lock("__all_accounts__"):
            usages = [fetch(account) for account in account_list]
    elif len(account_list) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(account_list))) as executor:
            usages = list(executor.map(fetch, account_list))
    else:
        usages = [fetch(account) for account in account_list]
    if save_snapshots:
        for index, usage in enumerate(usages):
            try:
                save_current_usage(usage)
                if usage.status == AccountStatus.OK:
                    save_usage_snapshot(usage)
            except Exception as exc:
                usages[index] = replace(
                    usage,
                    status=AccountStatus.ERROR,
                    error=f"snapshot save failed: {type(exc).__name__}",
                )
    for usage in usages:
        if usage.status == AccountStatus.ERROR:
            _record_health(
                "scheduler",
                "account_error",
                account=usage.account_id,
                error_class="UsageError",
            )
    return usages


def _serial_fetch_required(
    accounts: list[Account],
    *,
    headed: bool,
    direct: bool,
    backend_override: str | None,
) -> bool:
    return len(accounts) > 1


def _fetch_one(
    config: AppConfig,
    account: Account,
    *,
    headed: bool,
    direct: bool,
    backend_override: str | None,
    auth_json_path: Path | None,
    global_lock_held: bool = False,
) -> AccountUsage:
    try:
        backend = "direct" if direct else (backend_override or account.backend)
        use_auth_backend = (
            direct
            or backend == "app-server"
            or backend_override is not None
            or account.auth_json_path is not None
        )
        if not headed and use_auth_backend:
            def fetch_authenticated() -> AccountUsage:
                if backend == "app-server":
                    try:
                        return fetch_account_usage_app_server(account)
                    except AppServerUnavailableError as exc:
                        usage = fetch_account_usage_direct(account, auth_json_path=auth_json_path)
                        return replace(
                            usage,
                            backend_configured=account.backend,
                            backend_used="direct",
                            fallback_reason=" ".join(str(exc).split())[:500],
                        )
                usage = fetch_account_usage_direct(account, auth_json_path=auth_json_path)
                return replace(
                    usage,
                    backend_configured=account.backend,
                    backend_used="direct",
                )
            def fetch_with_account_lock() -> AccountUsage:
                with account_lock(account.id):
                    return fetch_authenticated()

            if global_lock_held:
                return fetch_with_account_lock()
            with account_lock("__all_accounts__"):
                return fetch_with_account_lock()
        def fetch_browser() -> AccountUsage:
            usage = fetch_account_usage(account, config, headed=headed)
            return replace(
                usage,
                backend_configured=account.backend,
                backend_used="browser",
            )

        if global_lock_held:
            with account_lock(account.id):
                return fetch_browser()
        with account_lock("__all_accounts__"):
            with account_lock(account.id):
                return fetch_browser()
    except Exception as exc:
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=datetime.now().astimezone(),
            status=AccountStatus.ERROR,
            error=f"fetch failed: {type(exc).__name__}",
            backend_configured=account.backend,
            backend_used=backend_override or account.backend,
        )


def _stabilize_authenticated_usage(
    usage: AccountUsage,
    previous: AccountUsage | None,
    *,
    max_age_seconds: int,
) -> AccountUsage:
    if (
        previous is None
        or previous.status != AccountStatus.OK
        or previous.backend_used not in AUTHENTICATED_BACKENDS
        or not backend_identity_matches(usage, previous)
    ):
        return usage
    if (
        previous.stale
        and previous.fallback_reason not in REUSABLE_RESET_FALLBACK_REASONS
    ):
        return usage
    try:
        age_seconds = (usage.captured_at - previous.captured_at).total_seconds()
    except (TypeError, AttributeError):
        return usage
    if age_seconds < 0 or age_seconds > max_age_seconds:
        return usage
    if not _has_unexpired_direct_reset_discontinuity(usage, previous):
        return usage
    if _is_more_conservative_direct_usage(usage, previous):
        return usage
    return replace(
        previous,
        label=usage.label,
        captured_at=usage.captured_at,
        auth_last_refresh=usage.auth_last_refresh,
        auth_access_expires_at=usage.auth_access_expires_at,
        auth_id_expires_at=usage.auth_id_expires_at,
        backend_configured=usage.backend_configured,
        backend_used=usage.backend_used,
        fallback_reason=AUTHENTICATED_RESET_FALLBACK_REASON,
        values_captured_at=previous.values_captured_at or previous.captured_at,
        stale=True,
    )


def _has_unexpired_direct_reset_discontinuity(
    current: AccountUsage,
    previous: AccountUsage,
) -> bool:
    for current_window, previous_window in (
        (current.five_hour, previous.five_hour),
        (current.weekly, previous.weekly),
    ):
        if (
            current_window is None
            or previous_window is None
            or current_window.reset_at is None
            or previous_window.reset_at is None
        ):
            continue
        if previous_window.reset_at <= current.captured_at:
            continue
        if current_window.reset_at <= current.captured_at:
            continue
        if (
            abs((current_window.reset_at - previous_window.reset_at).total_seconds())
            > DIRECT_RESET_DISCONTINUITY_SECONDS
        ):
            return True
    return False


def _is_more_conservative_direct_usage(
    current: AccountUsage,
    previous: AccountUsage,
) -> bool:
    compared = False
    for current_window, previous_window in (
        (current.five_hour, previous.five_hour),
        (current.weekly, previous.weekly),
    ):
        if current_window is None or previous_window is None:
            continue
        current_remaining = _remaining_percent(current_window)
        previous_remaining = _remaining_percent(previous_window)
        if (
            current_remaining is not None
            and previous_remaining is not None
        ):
            compared = True
            if current_remaining > previous_remaining:
                return False
            continue
        if (
            current_window.reset_at is not None
            and previous_window.reset_at is not None
        ):
            compared = True
            if current_window.reset_at > previous_window.reset_at:
                return False
    return compared


def _remaining_percent(window) -> float | None:
    if window.used is not None and window.limit is not None and window.limit > 0:
        return (float(window.limit) - float(window.used)) * 100 / float(window.limit)
    if window.remaining is not None:
        if window.limit is not None and window.limit > 0:
            return float(window.remaining) * 100 / float(window.limit)
        return float(window.remaining)
    if window.percent is not None:
        return float(window.percent)
    return None


def watch(
    config: AppConfig,
    accounts: Iterable[Account],
    *,
    output: str,
    headed: bool = False,
    direct: bool = False,
    backend_override: str | None = None,
    auth_json_path: Path | None = None,
    interval_seconds: int | None = None,
) -> None:
    interval = interval_seconds or config.interval_seconds
    account_list = list(accounts)
    stop_event = Event()
    previous_handlers: dict[int, object] = {}

    def stop(_signum, _frame) -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, stop)
        except (OSError, RuntimeError, ValueError):
            pass

    consecutive_failures = 0
    try:
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                usages = fetch_all(
                    config,
                    account_list,
                    headed=headed,
                    direct=direct,
                    backend_override=backend_override,
                    auth_json_path=auth_json_path,
                    save_snapshots=True,
                )
                if output == "json":
                    print(render_json(usages), flush=True)
                else:
                    print("\033[2J\033[H", end="")
                    print(render_table(usages), flush=True)
                elapsed = max(time.monotonic() - started, 0.0)
                _record_health(
                    "watch",
                    "cycle_ok",
                    duration_ms=int(elapsed * 1000),
                )
                consecutive_failures = 0
                delay = max(interval - elapsed, 0.0)
            except KeyboardInterrupt:
                stop_event.set()
                break
            except Exception as exc:
                consecutive_failures += 1
                _record_health(
                    "watch",
                    "cycle_error",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_class=type(exc).__name__,
                )
                message = " ".join(str(exc).split())[:240] or type(exc).__name__
                print(f"Fehler: watch cycle failed: {message}", file=sys.stderr, flush=True)
                delay = min(interval, 5 * (2 ** min(consecutive_failures - 1, 6)))
            if stop_event.wait(delay):
                break
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, RuntimeError, ValueError):
                pass


def _record_health(component: str, event: str, **kwargs) -> None:
    try:
        record_health_event(component, event, **kwargs)
    except Exception:
        pass


def watchdog(
    config: AppConfig,
    accounts: Iterable[Account],
    *,
    output: str,
    headed: bool = False,
    direct: bool = False,
    backend_override: str | None = None,
    auth_json_path: Path | None = None,
) -> list[AccountUsage]:
    now = datetime.now().astimezone()
    account_list = list(accounts)
    blocked_snapshots: dict[str, AccountUsage] = {}
    fetch_accounts: list[Account] = []
    for account in account_list:
        snapshot = load_usage_snapshot(account.id)
        if (
            snapshot is not None
            and _blocked_until_active(snapshot, now=now)
            and _blocked_snapshot_matches_account(
                account,
                snapshot,
                auth_json_path=auth_json_path,
            )
        ):
            blocked_snapshots[account.id] = snapshot
            continue
        fetch_accounts.append(account)

    fetched = fetch_all(
        config,
        fetch_accounts,
        headed=headed,
        direct=direct,
        backend_override=backend_override,
        auth_json_path=auth_json_path,
        save_snapshots=False,
    )
    evaluation_now = datetime.now().astimezone()
    expired_blocked_accounts = [
        account
        for account in account_list
        if account.id in blocked_snapshots
        and not _blocked_until_active(blocked_snapshots[account.id], now=evaluation_now)
    ]
    if expired_blocked_accounts:
        fetched.extend(
            fetch_all(
                config,
                expired_blocked_accounts,
                headed=headed,
                direct=direct,
                backend_override=backend_override,
                auth_json_path=auth_json_path,
                save_snapshots=False,
            )
        )
        for account in expired_blocked_accounts:
            blocked_snapshots.pop(account.id, None)
        evaluation_now = datetime.now().astimezone()
    fetched_by_id = {usage.account_id: usage for usage in fetched}

    usages: list[AccountUsage] = []
    for account in account_list:
        usage = blocked_snapshots.get(account.id) or fetched_by_id.get(account.id)
        if usage is None:
            continue
        if account.id not in blocked_snapshots:
            usage = _apply_watchdog_block(usage, now=evaluation_now)
            try:
                save_current_usage(usage)
                if usage.status in {AccountStatus.OK, AccountStatus.BLOCKED}:
                    save_usage_snapshot(usage)
            except Exception as exc:
                usage = replace(
                    usage,
                    status=AccountStatus.ERROR,
                    error=f"snapshot save failed: {type(exc).__name__}",
                    blocked_until=usage.blocked_until,
                    blocked_reason=usage.blocked_reason,
                )
        usages.append(usage)

    if output == "json":
        print(render_json(usages), flush=True)
    else:
        print(render_table(usages), flush=True)
    return usages


def _blocked_until_active(usage: AccountUsage, *, now: datetime) -> bool:
    return bool(
        usage.status == AccountStatus.BLOCKED
        and usage.blocked_until is not None
        and usage.blocked_until > now
    )


def _blocked_snapshot_matches_account(
    account: Account,
    snapshot: AccountUsage,
    *,
    auth_json_path: Path | None,
) -> bool:
    try:
        if auth_json_path is not None:
            auth_user_id, auth_account_id = auth_identity_from_file(auth_json_path)
        elif account.auth_json_path:
            auth_user_id, auth_account_id = auth_identity_for_account(account)
        else:
            return True
    except DirectAuthError:
        return False
    identities = {value for value in (auth_user_id, auth_account_id) if value}
    snapshot_identity = snapshot.backend_account_id or snapshot.backend_user_id
    return bool(snapshot_identity and snapshot_identity in identities)


def _apply_watchdog_block(usage: AccountUsage, *, now: datetime) -> AccountUsage:
    blocked_until, blocked_reason = _block_state(usage, now=now)
    if blocked_until is None:
        return usage
    return replace(
        usage,
        status=AccountStatus.BLOCKED,
        error=blocked_reason,
        blocked_until=blocked_until,
        blocked_reason=blocked_reason,
    )


def _block_state(usage: AccountUsage, *, now: datetime) -> tuple[datetime | None, str | None]:
    saturated_windows: list[tuple[datetime, str]] = []
    for window in (usage.five_hour, usage.weekly):
        if window is None or window.reset_at is None:
            continue
        if _window_is_exhausted(window):
            saturated_windows.append((window.reset_at, window.name))
    if not saturated_windows:
        return None, None
    blocked_until, _window_name = max(saturated_windows, key=lambda item: item[0])
    active_names = ", ".join(
        name for reset_at, name in saturated_windows if reset_at == blocked_until
    )
    if active_names:
        reason = f"usage limit reached: {active_names}; release at {blocked_until.isoformat()}"
    else:
        reason = f"usage limit reached; release at {blocked_until.isoformat()}"
    if blocked_until <= now:
        return None, None
    return blocked_until, reason


def _window_is_exhausted(window: Any) -> bool:
    if window is None:
        return False
    if window.remaining is not None:
        return window.remaining <= 0
    if window.used is not None and window.limit is not None and window.used >= window.limit:
        return True
    if window.percent is not None and window.percent <= 0:
        return True
    return False
