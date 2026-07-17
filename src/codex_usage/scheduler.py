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
from .extractor import LOCAL_TZ
from .health import record_health_event
from .models import Account, AccountStatus, AccountUsage
from .render import render_json, render_table
from .state import (
    APP_SERVER_FALLBACK_REASON_PREFIX,
    _backend_provenance_is_complete,
    backend_identity_matches,
    backend_provenance_matches,
    backend_provenance_matches_configured,
    expire_reset_windows,
    load_current_usage,
    load_state_generation,
    load_usage_snapshot,
    save_current_usage,
    save_usage_snapshot,
)
from .usage_limits import MAX_WINDOW_SECONDS

AUTHENTICATED_BACKENDS = frozenset(("direct", "app-server"))
MAX_CAPTURE_FUTURE_SECONDS = 5 * 60
RESET_FUTURE_SKEW_SECONDS = 5 * 60
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
    # A single-account command must not bypass ambiguity detection by selecting
    # only one row from a configuration that contains a shared user identity.
    ambiguous_direct_accounts = _ambiguous_direct_accounts(list(config.accounts))
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
        # A successful transport response can still contain a reset timestamp
        # that was already expired when captured. Such values are not usage.
        usage = expire_reset_windows(
            usage,
            reference_at=usage.captured_at,
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
                    usages[index] = _usage_after_snapshot_failure(
                        usage,
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
    configured_auth_account_ids = {
        account.id for account in accounts if account.auth_json_path
    }
    identities: list[tuple[str, str, str | None, str | None]] = []
    identity_lookup_failed: set[str] = set()
    for account in accounts:
        if not account.auth_json_path:
            continue
        try:
            user_id, account_id = auth_identity_for_account(account)
        except DirectAuthError:
            identity_lookup_failed.add(account.id)
            continue
        if not user_id:
            continue
        try:
            plan_type = auth_plan_type_for_account(account)
        except DirectAuthError:
            plan_type = None
        identities.append((account.id, user_id, account_id, plan_type))
    ambiguous: set[str] = set()
    identity_account_ids = {account_id for account_id, *_ in identities}
    unidentified_account_ids = (
        configured_auth_account_ids - identity_account_ids - identity_lookup_failed
    )
    if len(configured_auth_account_ids) > 1 and unidentified_account_ids:
        # An opaque, identity-less auth file cannot prove which backend account
        # a direct response belongs to when multiple auth accounts are loaded.
        ambiguous.update(configured_auth_account_ids)
    for index, (local_id, user_id, account_id, plan_type) in enumerate(identities):
        for (
            other_local_id,
            other_user_id,
            other_account_id,
            other_plan_type,
        ) in identities[index + 1 :]:
            if user_id != other_user_id:
                continue
            if account_id and other_account_id and account_id == other_account_id:
                continue
            if not account_id or not other_account_id:
                ambiguous.update((local_id, other_local_id))
                continue
            if _plan_types_distinguish(plan_type, other_plan_type):
                # Personal WHAM responses can echo the shared user ID as
                # account_id. A matching backend plan, checked later against
                # this token, distinguishes it from a same-user account with
                # a different plan. Same-plan or incomplete identities remain
                # fail-closed.
                continue
            ambiguous.update((local_id, other_local_id))
    return frozenset(ambiguous)


def _plan_types_distinguish(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return _normalized_plan_type(left) != _normalized_plan_type(right)


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
    backend: object = None
    try:
        effective_backend = "direct" if direct else (
            backend_override if backend_override is not None else account.backend
        )
        if (
            not isinstance(effective_backend, str)
            or effective_backend not in AUTHENTICATED_BACKENDS
        ):
            raise ValueError("invalid backend selection")
        backend = effective_backend
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
                        usage = fetch_account_usage_app_server(account)
                        return replace(
                            usage,
                            backend_configured=effective_backend,
                            backend_used="app-server",
                        )
                    except AppServerUnavailableError as exc:
                        direct_kwargs = {"auth_json_path": auth_json_path}
                        if reject_ambiguous_backend_identity:
                            direct_kwargs["reject_ambiguous_backend_identity"] = True
                        usage = fetch_account_usage_direct(account, **direct_kwargs)
                        fallback_detail = " ".join(str(exc).split())
                        return replace(
                            usage,
                            backend_configured=effective_backend,
                            backend_used="direct",
                            fallback_reason=(
                                f"{APP_SERVER_FALLBACK_REASON_PREFIX}{fallback_detail}"
                            )[:500],
                        )
                direct_kwargs = {"auth_json_path": auth_json_path}
                if reject_ambiguous_backend_identity:
                    direct_kwargs["reject_ambiguous_backend_identity"] = True
                usage = fetch_account_usage_direct(account, **direct_kwargs)
                return replace(
                    usage,
                    backend_configured=effective_backend,
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
                backend_configured=effective_backend,
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
            captured_at=datetime.now(tz=LOCAL_TZ),
            status=AccountStatus.ERROR,
            error=f"fetch failed: {type(exc).__name__}",
            backend_configured=account.backend,
            backend_used=(
                backend
                if isinstance(backend, str) and backend in AUTHENTICATED_BACKENDS
                else None
            ),
            cache_invalidated=True,
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
        or not _backend_provenance_is_complete(usage)
        or not _backend_provenance_is_complete(previous)
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
    stabilized_main = _stabilize_main_pool(
        usage.main,
        previous,
        retain_five_hour=retain_five_hour,
        retain_weekly=retain_weekly,
    )
    return replace(
        usage,
        label=usage.label,
        captured_at=usage.captured_at,
        five_hour=previous.five_hour if retain_five_hour else usage.five_hour,
        weekly=previous.weekly if retain_weekly else usage.weekly,
        main=stabilized_main,
        auth_last_refresh=usage.auth_last_refresh,
        auth_access_expires_at=usage.auth_access_expires_at,
        auth_id_expires_at=usage.auth_id_expires_at,
        backend_configured=usage.backend_configured,
        backend_used=usage.backend_used,
        fallback_reason=AUTHENTICATED_RESET_FALLBACK_REASON,
        values_captured_at=previous.values_captured_at or previous.captured_at,
        stale=True,
    )


def _stabilize_main_pool(
    current: Any,
    previous: AccountUsage,
    *,
    retain_five_hour: bool,
    retain_weekly: bool,
) -> Any:
    if current is None:
        return None
    current_windows = list(current.windows)
    previous_windows = (
        tuple(previous.main.windows)
        if previous.main is not None
        else ()
    )
    replacements = (
        ("five_hour", retain_five_hour, 18_000),
        ("weekly", retain_weekly, 604_800),
    )
    for kind, retain, duration in replacements:
        if not retain:
            continue
        previous_window = next(
            (
                window
                for window in previous_windows
                if _window_kind(window) == kind
                or getattr(window, "duration_seconds", None) == duration
            ),
            None,
        )
        if previous_window is None:
            previous_window = (
                previous.five_hour
                if kind == "five_hour"
                else previous.weekly
            )
        if previous_window is None:
            continue
        current_index = next(
            (
                index
                for index, window in enumerate(current_windows)
                if _window_kind(window) == kind
                or getattr(window, "duration_seconds", None) == duration
            ),
            None,
        )
        if current_index is None:
            current_windows.append(previous_window)
        else:
            current_windows[current_index] = previous_window
    if tuple(current_windows) == current.windows:
        return current
    return replace(current, windows=tuple(current_windows))


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
    try:
        if previous.reset_at <= reference_at or current.reset_at <= reference_at:
            return False
        for window in (previous, current):
            duration = _window_duration_seconds(window)
            if duration is None:
                duration = WINDOW_DURATIONS.get(_window_kind(window) or "")
            if duration is None:
                return False
            if window.reset_at > reference_at + timedelta(
                seconds=duration + RESET_FUTURE_SKEW_SECONDS
            ):
                return False
        if _uses_absolute_reset_time(current) or _uses_absolute_reset_time(previous):
            return False
        if _has_relative_reset_metadata(current) or _has_relative_reset_metadata(previous):
            # A relative countdown is the authoritative reset signal for direct
            # responses. If its metadata is malformed, never reinterpret the
            # accompanying timestamp as an absolute reset and retain old values.
            return False
        if _uses_relative_reset_time(current) or _uses_relative_reset_time(previous):
            return False
        return (
            abs((current.reset_at - previous.reset_at).total_seconds())
            > DIRECT_RESET_DISCONTINUITY_SECONDS
        )
    except (AttributeError, OverflowError, TypeError, ValueError):
        # Unknown reset ordering must never retain older, possibly exhausted,
        # values.
        return False


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


def _has_relative_reset_metadata(window: Any) -> bool:
    raw = getattr(window, "raw", None)
    if not isinstance(raw, str):
        return False
    return re.search(
        r'"(?:reset_after_seconds|resetafterseconds|reset_after|resetafter)"\s*:',
        raw,
    ) is not None


def _uses_absolute_reset_time(window: Any) -> bool:
    return getattr(window, "source", None) == "app-server"


def _raw_number(raw: str, field: str) -> float | None:
    match = re.search(
        rf'"{re.escape(field)}"\s*:\s*({RAW_NUMBER_PATTERN})',
        raw,
    )
    if match is None:
        return None
    try:
        number = float(match.group(1))
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


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
    if not current_kind and not previous_kind:
        # Raw duration alone may identify a supported legacy window. An
        # unknown duration must never authorize reuse of an older value.
        return (
            current_duration in WINDOW_DURATIONS.values()
            and previous_duration in WINDOW_DURATIONS.values()
            and current_duration == previous_duration
        )
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
    except (OverflowError, ValueError):
        return None
    if (
        not math.isfinite(value)
        or value <= 0
        or value > MAX_WINDOW_SECONDS
        or not value.is_integer()
    ):
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
            try:
                decisions.append(current_window.reset_at <= previous_window.reset_at)
            except (AttributeError, OverflowError, TypeError, ValueError):
                return False
    return bool(decisions) and all(decisions)


def _remaining_percent(window) -> float | None:
    if getattr(window, "has_invalid_usage_value", False):
        return None
    raw_used = getattr(window, "used", None)
    raw_limit = getattr(window, "limit", None)
    raw_remaining = getattr(window, "remaining", None)
    raw_percent = getattr(window, "percent", None)
    used = _finite_number(window.used)
    limit = _finite_number(window.limit)
    remaining_value = _finite_number(window.remaining)
    percent = _finite_number(window.percent)
    if any(
        raw is not None and parsed is None
        for raw, parsed in (
            (raw_used, used),
            (raw_limit, limit),
            (raw_remaining, remaining_value),
            (raw_percent, percent),
        )
    ):
        return None
    if used is not None and limit is not None and limit > 0:
        if used < 0:
            return None
        if used >= limit:
            return 0.0
        remaining = (limit - used) * 100 / limit
        return _valid_percent(remaining)
    if limit is not None and limit <= 0:
        return None
    if remaining_value is not None:
        if limit is not None and limit > 0:
            if not 0 <= remaining_value <= limit:
                return None
            remaining = remaining_value * 100 / limit
            return _valid_percent(remaining)
        if percent is not None:
            return _valid_percent(percent)
        if not 0 <= remaining_value <= 100:
            return None
        return remaining_value
    if percent is not None:
        return _valid_percent(percent)
    return None


def _finite_number(value) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _valid_percent(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return value if 0 <= value <= 100 else None


def _watch_cycle_is_healthy(
    usages: Iterable[AccountUsage],
    accounts: Iterable[Account],
    *,
    direct: bool = False,
    backend_override: str | None = None,
) -> bool:
    results = list(usages)
    account_list = list(accounts)
    expected = tuple(account.id for account in account_list)
    if _usage_map_for_accounts(results, account_list) is None:
        return False
    if not expected:
        return True
    try:
        accounts_by_id = {account.id: account for account in account_list}
        for usage in results:
            if (
                not isinstance(usage, AccountUsage)
                or usage.status is not AccountStatus.OK
                or usage.error is not None
                or usage.stale is not False
                or usage.cache_invalidated is not False
            ):
                return False
            account = accounts_by_id.get(usage.account_id)
            configured_backend = "direct" if direct else backend_override
            if configured_backend is None and account is not None:
                configured_backend = account.backend
            if account is None or not (
                usage.backend_configured == configured_backend
                and usage.backend_used in AUTHENTICATED_BACKENDS | {"browser"}
                and backend_provenance_matches_configured(
                    usage, configured_backend
                )
            ):
                return False
            if not _has_usable_core_usage(usage) or not _watch_core_resets_current(
                usage
            ):
                return False
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _usage_map_for_accounts(
    usages: Iterable[AccountUsage], accounts: Iterable[Account]
) -> dict[str, AccountUsage] | None:
    results = list(usages)
    expected = tuple(account.id for account in accounts)
    if len(results) != len(expected):
        return None
    try:
        if not all(isinstance(usage, AccountUsage) for usage in results):
            return None
        result_ids = tuple(usage.account_id for usage in results)
        if len(set(result_ids)) != len(result_ids):
            return None
        if len(set(expected)) != len(expected) or set(result_ids) != set(expected):
            return None
        return {usage.account_id: usage for usage in results}
    except (AttributeError, TypeError, ValueError):
        return None


def _has_usable_core_usage(usage: AccountUsage) -> bool:
    if usage.main is not None:
        return usage.main.has_valid_usage
    return any(
        window is not None and window.has_usage_value
        for window in (usage.five_hour, usage.weekly)
    )


def _watch_core_resets_current(usage: AccountUsage) -> bool:
    now = datetime.now(tz=LOCAL_TZ)
    windows = (
        usage.main.windows
        if usage.main is not None
        else tuple(
            window
            for window in (usage.five_hour, usage.weekly)
            if window is not None
        )
    )
    for window in windows:
        reset_at = window.reset_at
        if reset_at is None:
            continue
        if not isinstance(reset_at, datetime) or reset_at.tzinfo is None:
            return False
        try:
            if reset_at <= now:
                return False
        except (OverflowError, TypeError, ValueError):
            return False
    return True


def _watch_failure_usages(
    accounts: Iterable[Account],
    attempted: Iterable[AccountUsage] | None,
    *,
    error: str,
) -> list[AccountUsage]:
    try:
        attempted_by_id = {
            usage.account_id: usage
            for usage in (attempted or ())
            if isinstance(usage, AccountUsage)
        }
    except (AttributeError, TypeError, ValueError):
        attempted_by_id = {}
    captured_at = datetime.now(tz=LOCAL_TZ)
    failures: list[AccountUsage] = []
    for account in accounts:
        attempted_usage = attempted_by_id.get(account.id)
        detail = (
            attempted_usage.error
            if attempted_usage is not None and attempted_usage.error
            else error
        )
        detail = " ".join(str(detail).split())[:240] or "watch cycle failed"
        failures.append(
            AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=captured_at,
                status=AccountStatus.ERROR,
                error=detail,
                backend_configured=account.backend,
                cache_invalidated=True,
            )
        )
    return failures


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
    interval = config.interval_seconds if interval_seconds is None else interval_seconds
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or interval < 60
    ):
        raise ValueError("interval_seconds must be a finite integer of at least 60")
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
            usages: list[AccountUsage] = []
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
                if not _watch_cycle_is_healthy(
                    usages,
                    account_list,
                    direct=direct,
                    backend_override=backend_override,
                ):
                    raise RuntimeError("watch cycle returned unusable usage")
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
                failure_usages = _watch_failure_usages(
                    account_list,
                    usages,
                    error=f"watch cycle failed: {message}",
                )
                if output == "json":
                    print(render_json(failure_usages), flush=True)
                else:
                    print("\033[2J\033[H", end="")
                    print(render_table(failure_usages), flush=True)
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
    now = datetime.now(tz=LOCAL_TZ)
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
            and _blocked_snapshot_is_consistent(snapshot, now=now)
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
    evaluation_now = datetime.now(tz=LOCAL_TZ)
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
        evaluation_now = datetime.now(tz=LOCAL_TZ)
    fetched_accounts = tuple(
        account for account in account_list if account.id not in blocked_snapshots
    )
    fetched_by_id = _usage_map_for_accounts(fetched, fetched_accounts)
    if fetched_by_id is None:
        failures = _watch_failure_usages(
            fetched_accounts,
            fetched,
            error="watchdog fetch result identity mismatch",
        )
        fetched_by_id = {usage.account_id: usage for usage in failures}

    usages: list[AccountUsage] = []
    with account_lock("__all_accounts__"):
        for account in account_list:
            usage = blocked_snapshots.get(account.id) or fetched_by_id.get(account.id)
            if usage is None:
                continue
            if account.id not in blocked_snapshots:
                usage = _apply_watchdog_block(usage, now=evaluation_now)
                if not backend_provenance_matches_configured(usage, account.backend):
                    usages.append(usage)
                    continue
                try:
                    save_current_usage(usage)
                    if _should_persist_snapshot(usage):
                        save_usage_snapshot(usage)
                except Exception as exc:
                    usage = _usage_after_snapshot_failure(
                        usage,
                        error=f"snapshot save failed: {type(exc).__name__}",
                    )
            usages.append(usage)

    if output == "json":
        print(render_json(usages), flush=True)
    else:
        print(render_table(usages), flush=True)
    return usages


def _usage_after_snapshot_failure(usage: AccountUsage, *, error: str) -> AccountUsage:
    return replace(
        usage,
        five_hour=None,
        weekly=None,
        main=None,
        models=(),
        values_captured_at=None,
        status=AccountStatus.ERROR,
        error=error,
        cache_invalidated=True,
    )


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
    except (AttributeError, OverflowError, TypeError, ValueError):
        return False


def _blocked_until_active(usage: AccountUsage, *, now: datetime) -> bool:
    try:
        return bool(
            usage.status == AccountStatus.BLOCKED
            and usage.blocked_until is not None
            and usage.blocked_until > now
        )
    except (AttributeError, OverflowError, TypeError, ValueError):
        return False


def _blocked_snapshot_is_consistent(usage: AccountUsage, *, now: datetime) -> bool:
    """Validate stored block metadata without breaking legacy empty snapshots."""
    if not _watchdog_windows(usage):
        return True
    try:
        blocked_until, _reason = _block_state(usage, now=now)
        return blocked_until is not None and blocked_until == usage.blocked_until
    except (AttributeError, OverflowError, TypeError, ValueError):
        return False


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
    if (
        snapshot.backend_used == "browser"
        and (
            configured_backend == "app-server"
            or (
                configured_backend == "direct"
                and (auth_json_path is not None or account.auth_json_path is not None)
            )
        )
    ):
        # A browser block belongs to whichever account the browser cookies had
        # active. It is not safe to attribute it to an authenticated account,
        # especially when multiple accounts share a user ID.
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
    if blocked_until is None and blocked_reason is None:
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
    unknown_reset_names: list[str] = []
    watchdog_windows = _watchdog_windows(usage)
    pool_forces_block = _pool_forces_watchdog_block(usage.main)
    if pool_forces_block and not watchdog_windows:
        return None, "usage limit reached: main; reset time unknown"
    for window in watchdog_windows:
        if window is None or (not pool_forces_block and not _window_is_exhausted(window)):
            continue
        reset_at = getattr(window, "reset_at", None)
        try:
            reset_timezone = reset_at.tzinfo
            reset_offset = reset_at.utcoffset()
        except (AttributeError, OverflowError, TypeError, ValueError):
            reset_timezone = None
            reset_offset = None
        if reset_timezone is None or reset_offset is None:
            unknown_reset_names.append(window.name)
            continue
        saturated_windows.append((reset_at, window.name))
    if unknown_reset_names:
        names = ", ".join(unknown_reset_names)
        return None, f"usage limit reached: {names}; reset time unknown"
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


def _pool_forces_watchdog_block(pool: Any) -> bool:
    if pool is None:
        return False
    try:
        return bool(
            pool.allowed is False
            or pool.limit_reached is True
            or (
                pool.allowed is not None
                and not isinstance(pool.allowed, bool)
            )
            or (
                pool.limit_reached is not None
                and not isinstance(pool.limit_reached, bool)
            )
        )
    except (AttributeError, TypeError, ValueError):
        return True


def _watchdog_windows(usage: AccountUsage) -> tuple[Any, ...]:
    if usage.main is not None and usage.main.windows:
        return usage.main.windows
    return tuple(
        window
        for window in (usage.five_hour, usage.weekly)
        if window is not None
    )


def _window_is_exhausted(window: Any) -> bool:
    if window is None:
        return False
    if getattr(window, "has_invalid_usage_value", False):
        return True
    raw_used = getattr(window, "used", None)
    raw_limit = getattr(window, "limit", None)
    raw_remaining = getattr(window, "remaining", None)
    raw_percent = getattr(window, "percent", None)
    used = _finite_number(raw_used)
    limit = _finite_number(raw_limit)
    remaining = _finite_number(raw_remaining)
    percent = _finite_number(raw_percent)
    if any(
        raw is not None and parsed is None
        for raw, parsed in (
            (raw_used, used),
            (raw_limit, limit),
            (raw_remaining, remaining),
            (raw_percent, percent),
        )
    ):
        return True
    try:
        remaining_percent = window.remaining_percent
    except (AttributeError, TypeError, ValueError, OverflowError):
        return True
    if remaining_percent is None:
        return True
    if used is not None and limit is not None:
        if limit <= 0 or used < 0:
            return True
        # Absolute usage is authoritative when a stale remaining field
        # conflicts with it.
        return used >= limit
    if limit is not None and limit <= 0:
        return True
    if remaining is not None:
        if remaining < 0:
            return True
        if remaining <= 100:
            return remaining <= 0
        if percent is not None and 0 <= percent <= 100:
            return percent <= 0
        return True
    if percent is not None:
        return not 0 <= percent <= 100 or percent <= 0
    return True


def _should_persist_snapshot(usage: AccountUsage) -> bool:
    if usage.status in {AccountStatus.OK, AccountStatus.BLOCKED}:
        return True
    return (
        usage.status == AccountStatus.PARTIAL
        and usage.backend_used in AUTHENTICATED_BACKENDS
    )
