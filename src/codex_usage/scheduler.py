from __future__ import annotations

import math
import re
import signal
import sys
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

from .account_lock import account_lock
from .app_server import AppServerUnavailableError, fetch_account_usage_app_server
from .browser import fetch_account_usage
from .config import AppConfig
from .direct import (
    DirectAuthError,
    _normalized_plan_type,
    auth_identity_changed,
    auth_identity_for_account,
    auth_identity_from_file,
    auth_plan_type_for_account,
    fetch_account_usage_direct,
)
from .health import record_health_event
from .models import Account, AccountStatus, AccountUsage
from .render import render_json, render_table
from .state import (
    backend_identity_matches,
    backend_provenance_matches,
    backend_provenance_matches_configured,
    load_current_usage,
    load_state_generation,
    load_usage_snapshot,
    save_current_usage,
    save_usage_snapshot,
)

AUTHENTICATED_BACKENDS = frozenset(("direct", "app-server"))
MAX_CAPTURE_FUTURE_SECONDS = 5 * 60
DIRECT_RESET_DISCONTINUITY_SECONDS = 30
WINDOW_DURATIONS = {"five_hour": 18_000, "weekly": 604_800}
RAW_NUMBER_PATTERN = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
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
    ambiguous_direct_accounts = _ambiguous_direct_accounts(account_list)
    serial_fetch_required = _serial_fetch_required(
        account_list,
        headed=headed,
        direct=direct,
        backend_override=backend_override,
    )

    def fetch(account: Account) -> AccountUsage:
        state_generation = load_state_generation(account.id)
        usage = _fetch_one(
            config,
            account,
            headed=headed,
            direct=direct,
            backend_override=backend_override,
            auth_json_path=auth_json_path if (direct or auth_json_path is not None) else None,
            global_lock_held=serial_fetch_required,
            reject_ambiguous_backend_identity=account.id in ambiguous_direct_accounts,
        )
        usage = replace(usage, state_generation=state_generation)
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
        with account_lock("__all_accounts__"):
            accounts_by_id = {account.id: account for account in account_list}
            for index, usage in enumerate(usages):
                account = accounts_by_id.get(usage.account_id)
                if (
                    account is None
                    or not backend_provenance_matches_configured(usage, account.backend)
                ):
                    continue
                try:
                    save_current_usage(usage)
                    if _should_persist_snapshot(usage):
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


def _ambiguous_direct_accounts(accounts: list[Account]) -> frozenset[str]:
    identities: list[tuple[str, str, str | None, str | None]] = []
    for account in accounts:
        if not account.auth_json_path:
            continue
        try:
            user_id, account_id = auth_identity_for_account(account)
            plan_type = auth_plan_type_for_account(account)
        except DirectAuthError:
            continue
        if not user_id:
            continue
        identities.append((account.id, user_id, account_id, plan_type))
    ambiguous: set[str] = set()
    for index, (local_id, user_id, account_id, plan_type) in enumerate(identities):
        for other_local_id, other_user_id, other_account_id, other_plan_type in identities[
            index + 1 :
        ]:
            if user_id != other_user_id:
                continue
            if account_id and other_account_id and account_id == other_account_id:
                continue
            if not account_id or not other_account_id:
                ambiguous.update((local_id, other_local_id))
                continue
            plans_are_ambiguous = (
                plan_type is None
                or other_plan_type is None
                or _normalized_plan_type(plan_type) == _normalized_plan_type(other_plan_type)
            )
            if plans_are_ambiguous:
                ambiguous.update((local_id, other_local_id))
    return frozenset(ambiguous)


def _fetch_one(
    config: AppConfig,
    account: Account,
    *,
    headed: bool,
    direct: bool,
    backend_override: str | None,
    auth_json_path: Path | None,
    global_lock_held: bool = False,
    reject_ambiguous_backend_identity: bool = False,
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
                        direct_kwargs = {"auth_json_path": auth_json_path}
                        if reject_ambiguous_backend_identity:
                            direct_kwargs["reject_ambiguous_backend_identity"] = True
                        usage = fetch_account_usage_direct(account, **direct_kwargs)
                        return replace(
                            usage,
                            backend_configured=account.backend,
                            backend_used="direct",
                            fallback_reason=" ".join(str(exc).split())[:500],
                        )
                direct_kwargs = {"auth_json_path": auth_json_path}
                if reject_ambiguous_backend_identity:
                    direct_kwargs["reject_ambiguous_backend_identity"] = True
                usage = fetch_account_usage_direct(account, **direct_kwargs)
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
        usage.backend_used == "direct"
        and previous.backend_used == "app-server"
        and not backend_provenance_matches(usage, previous)
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
    if (
        usage.backend_used == "app-server"
        and previous.backend_used == "app-server"
        and previous.fallback_reason == AUTHENTICATED_RESET_FALLBACK_REASON
    ):
        # An app-server value that repeats after one guarded transition is
        # evidence for the new window, not another reason to preserve the
        # already-stale fallback indefinitely.
        return usage
    retain_five_hour = _should_retain_previous_window(
        usage.five_hour,
        previous.five_hour,
        reference_at=usage.captured_at,
    )
    retain_weekly = _should_retain_previous_window(
        usage.weekly,
        previous.weekly,
        reference_at=usage.captured_at,
    )
    if not retain_five_hour and not retain_weekly:
        return usage
    return replace(
        usage,
        label=usage.label,
        captured_at=usage.captured_at,
        five_hour=previous.five_hour if retain_five_hour else usage.five_hour,
        weekly=previous.weekly if retain_weekly else usage.weekly,
        auth_last_refresh=usage.auth_last_refresh,
        auth_access_expires_at=usage.auth_access_expires_at,
        auth_id_expires_at=usage.auth_id_expires_at,
        backend_configured=usage.backend_configured,
        backend_used=usage.backend_used,
        fallback_reason=AUTHENTICATED_RESET_FALLBACK_REASON,
        values_captured_at=previous.values_captured_at or previous.captured_at,
        stale=True,
    )


def _has_unexpired_window_reset_discontinuity(
    current: Any,
    previous: Any,
    *,
    reference_at: datetime,
) -> bool:
    if (
        current is None
        or previous is None
        or current.reset_at is None
        or previous.reset_at is None
    ):
        return False
    if previous.reset_at <= reference_at or current.reset_at <= reference_at:
        return False
    if _uses_relative_reset_time(current) or _uses_relative_reset_time(previous):
        return False
    return (
        abs((current.reset_at - previous.reset_at).total_seconds())
        > DIRECT_RESET_DISCONTINUITY_SECONDS
    )


def _uses_relative_reset_time(window: Any) -> bool:
    """The direct endpoint estimates untouched-window resets from the poll time."""
    raw = getattr(window, "raw", None)
    if not isinstance(raw, str):
        return False
    limit_window = _raw_number(raw, "limit_window_seconds")
    reset_after = _raw_number(raw, "reset_after_seconds")
    return (
        limit_window is not None
        and reset_after is not None
        and 0 <= reset_after <= limit_window
    )


def _raw_number(raw: str, field: str) -> float | None:
    match = re.search(
        rf'"{re.escape(field)}"\s*:\s*({RAW_NUMBER_PATTERN})',
        raw,
    )
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _should_retain_previous_window(
    current: Any,
    previous: Any,
    *,
    reference_at: datetime,
) -> bool:
    if not _window_duration_matches(current, previous):
        return False
    if not _has_unexpired_window_reset_discontinuity(
        current,
        previous,
        reference_at=reference_at,
    ):
        return False
    current_remaining = _remaining_percent(current)
    previous_remaining = _remaining_percent(previous)
    if current_remaining is not None and previous_remaining is not None:
        return current_remaining > previous_remaining
    return False


def _window_duration_matches(current: Any, previous: Any) -> bool:
    current_kind = _window_kind(current)
    previous_kind = _window_kind(previous)
    current_duration = _window_duration_seconds(current)
    previous_duration = _window_duration_seconds(previous)
    if (
        (current_kind is None and current_duration is None)
        or (previous_kind is None and previous_duration is None)
    ):
        return False
    if bool(current_kind) != bool(previous_kind):
        return False
    if current_kind and previous_kind and current_kind != previous_kind:
        return False
    expected_duration = WINDOW_DURATIONS.get(current_kind or previous_kind or "")
    if expected_duration is not None and any(
        duration is not None and duration != expected_duration
        for duration in (current_duration, previous_duration)
    ):
        return False
    return (
        current_duration is None
        or previous_duration is None
        or current_duration == previous_duration
    )


def _window_kind(window: Any) -> str | None:
    name = getattr(window, "name", None)
    if not isinstance(name, str):
        return None
    normalized = re.sub(r"[-\s]+", "_", name.strip().casefold())
    if normalized in {"5h", "5_hour", "five_hour"}:
        return "five_hour"
    if normalized in {"w", "week", "weekly"}:
        return "weekly"
    return None


def _window_duration_seconds(window: Any) -> int | None:
    raw = getattr(window, "raw", None)
    if not isinstance(raw, str):
        return None
    match = re.search(
        r'"limit_window_seconds"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        raw,
    )
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0 or not value.is_integer():
        return None
    return int(value)


def _is_more_conservative_direct_usage(
    current: AccountUsage,
    previous: AccountUsage,
) -> bool:
    decisions: list[bool] = []
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
            decisions.append(current_remaining <= previous_remaining)
            continue
        if (
            current_window.reset_at is not None
            and previous_window.reset_at is not None
        ):
            decisions.append(current_window.reset_at <= previous_window.reset_at)
    return bool(decisions) and all(decisions)


def _remaining_percent(window) -> float | None:
    if window.used is not None and window.limit is not None and window.limit > 0:
        remaining = (float(window.limit) - float(window.used)) * 100 / float(window.limit)
        return _clamp_percent(remaining)
    if window.remaining is not None:
        if window.limit is not None and window.limit > 0:
            remaining = float(window.remaining) * 100 / float(window.limit)
            return _clamp_percent(remaining)
        remaining = float(window.remaining)
        return _clamp_percent(remaining)
    if window.percent is not None:
        percent = float(window.percent)
        return _clamp_percent(percent)
    return None


def _clamp_percent(value: float) -> float | None:
    if not math.isfinite(value):
        return None
    return max(0.0, min(100.0, value))


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
    effective_backend = "direct" if direct else None
    if effective_backend is None:
        effective_backend = backend_override
    blocked_snapshots: dict[str, AccountUsage] = {}
    fetch_accounts: list[Account] = []
    for account in account_list:
        snapshot = load_usage_snapshot(account.id)
        if (
            snapshot is not None
            and not _capture_is_too_far_in_future(snapshot, now)
            and _blocked_until_active(snapshot, now=now)
            and _blocked_snapshot_matches_account(
                account,
                snapshot,
                auth_json_path=auth_json_path,
                configured_backend=effective_backend or account.backend,
            )
            and not _current_supersedes_blocked_snapshot(
                account,
                snapshot,
                load_current_usage(account.id),
                auth_json_path=auth_json_path,
                configured_backend=effective_backend or account.backend,
            )
        ):
            blocked_snapshots[account.id] = replace(
                snapshot,
                state_generation=load_state_generation(account.id),
            )
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
    with account_lock("__all_accounts__"):
        for account in account_list:
            usage = blocked_snapshots.get(account.id) or fetched_by_id.get(account.id)
            if usage is None:
                continue
            if account.id not in blocked_snapshots:
                usage = _apply_watchdog_block(usage, now=evaluation_now)
                try:
                    save_current_usage(usage)
                    if _should_persist_snapshot(usage):
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


def _current_supersedes_blocked_snapshot(
    account: Account,
    blocked_snapshot: AccountUsage,
    current: AccountUsage | None,
    *,
    auth_json_path: Path | None,
    configured_backend: str,
) -> bool:
    if current is None or current.status == AccountStatus.BLOCKED:
        return False
    if not _blocked_snapshot_matches_account(
        account,
        current,
        auth_json_path=auth_json_path,
        configured_backend=configured_backend,
    ):
        return False
    try:
        return current.captured_at > blocked_snapshot.captured_at
    except TypeError:
        return False


def _blocked_until_active(usage: AccountUsage, *, now: datetime) -> bool:
    return bool(
        usage.status == AccountStatus.BLOCKED
        and usage.blocked_until is not None
        and usage.blocked_until > now
    )


def _capture_is_too_far_in_future(
    usage: AccountUsage | None,
    reference_at: datetime,
) -> bool:
    if usage is None:
        return False
    try:
        return usage.captured_at > reference_at + timedelta(seconds=MAX_CAPTURE_FUTURE_SECONDS)
    except (TypeError, ValueError, OverflowError):
        return True


def _blocked_snapshot_matches_account(
    account: Account,
    snapshot: AccountUsage,
    *,
    auth_json_path: Path | None,
    configured_backend: str,
) -> bool:
    if not backend_provenance_matches_configured(snapshot, configured_backend):
        return False
    try:
        if auth_json_path is not None:
            auth_user_id, auth_account_id = auth_identity_from_file(auth_json_path)
        elif account.auth_json_path:
            auth_user_id, auth_account_id = auth_identity_for_account(account)
        else:
            return True
    except DirectAuthError:
        return False
    if snapshot.backend_account_id:
        if not auth_account_id:
            return False
        if auth_identity_changed(
            before_user_id=snapshot.backend_user_id,
            before_account_id=snapshot.backend_account_id,
            after_user_id=auth_user_id,
            after_account_id=auth_account_id,
        ):
            return False
        return snapshot.backend_account_id in {auth_account_id, auth_user_id}
    if snapshot.backend_user_id:
        # A user ID alone cannot distinguish two accounts sharing that user.
        # Reuse is safe only when the current auth has no account ID either.
        return auth_account_id is None and snapshot.backend_user_id == auth_user_id
    return False


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
    if window.used is not None and window.limit is not None:
        # Absolute usage is authoritative when a stale remaining field
        # conflicts with it.
        return window.used >= window.limit
    if window.remaining is not None:
        return window.remaining <= 0
    if window.percent is not None and window.percent <= 0:
        return True
    return False


def _should_persist_snapshot(usage: AccountUsage) -> bool:
    if usage.status in {AccountStatus.OK, AccountStatus.BLOCKED}:
        return True
    return (
        usage.status == AccountStatus.PARTIAL
        and usage.backend_used in AUTHENTICATED_BACKENDS
    )
