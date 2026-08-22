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
from itertools import islice
from pathlib import Path
from threading import Event
from typing import Any, cast

from .account_lock import account_lock
from .app_server import AppServerUnavailableError, fetch_account_usage_app_server
from .browser import fetch_account_usage
from .config import MAX_CONFIG_ACCOUNTS, AppConfig
from .direct import (
    DirectAuthError,
    _normalized_plan_type,
    auth_identity_changed,
    auth_identity_for_account,
    auth_identity_from_file,
    auth_plan_type_for_account,
    default_auth_json_path,
    fetch_account_usage_direct,
)
from .extractor import LOCAL_TZ
from .health import record_health_event
from .history import record_usage_samples_batch
from .models import Account, AccountStatus, AccountUsage, LimitWindow, UsagePool
from .render import render_json, render_table
from .routing import _pool_has_usage_evidence
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

DATETIME_TYPE = datetime
AUTHENTICATED_BACKENDS = frozenset(("direct", "app-server"))
MAX_SCHEDULER_ACCOUNTS = MAX_CONFIG_ACCOUNTS
MAX_CAPTURE_FUTURE_SECONDS = 5 * 60
RESET_FUTURE_SKEW_SECONDS = 5 * 60
DIRECT_RESET_DISCONTINUITY_SECONDS = 30
WINDOW_DURATIONS = {
    "five_hour": 18_000,
    "weekly": 604_800,
    "thirty_day": 2_592_000,
}
LEGACY_WINDOW_DURATIONS = frozenset((18_000, 604_800))
RAW_NUMBER_PATTERN = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
LEGACY_DIRECT_RESET_FALLBACK_REASON = "previous direct limits retained after reset transition"
AUTHENTICATED_RESET_FALLBACK_REASON = (
    "previous authenticated limits retained after reset transition"
)
REUSABLE_RESET_FALLBACK_REASONS = frozenset(
    (LEGACY_DIRECT_RESET_FALLBACK_REASON, AUTHENTICATED_RESET_FALLBACK_REASON)
)


def _bounded_account_list(accounts: Iterable[Account]) -> list[Account]:
    try:
        account_list = list(islice(accounts, MAX_SCHEDULER_ACCOUNTS + 1))
    except TypeError as exc:
        raise ValueError("account records are invalid") from exc
    if len(account_list) > MAX_SCHEDULER_ACCOUNTS:
        raise ValueError("too many accounts")
    if any(
        not isinstance(account, Account) or not isinstance(account.id, str)
        for account in account_list
    ):
        raise ValueError("account records are invalid")
    return account_list


def _validated_auth_json_path(auth_json_path: Path | None) -> Path | None:
    if auth_json_path is not None and not isinstance(auth_json_path, Path):
        raise ValueError("auth_json_path is invalid")
    if auth_json_path is None:
        return None
    try:
        return auth_json_path.expanduser()
    except RuntimeError as exc:
        raise ValueError("auth_json_path is invalid") from exc


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
    if not isinstance(config, AppConfig):
        raise ValueError("config is invalid")
    auth_json_path = _validated_auth_json_path(auth_json_path)
    account_list = _bounded_account_list(accounts)
    # A single-account command must not bypass ambiguity detection by selecting
    # only one row from a configuration that contains a shared user identity.
    configured_accounts = _bounded_account_list(config.accounts)
    identity_scope = account_list if auth_json_path is not None else configured_accounts
    ambiguous_direct_accounts = _ambiguous_direct_accounts(
        identity_scope,
        auth_json_path=auth_json_path,
    )
    unattributed_direct_accounts = (
        _unattributed_direct_accounts(identity_scope)
        if auth_json_path is None
        else frozenset()
    )
    shared_direct_auth_accounts = _shared_direct_auth_accounts(
        identity_scope,
        auth_json_path=auth_json_path,
    )
    serial_fetch_required = _serial_fetch_required(
        account_list,
        headed=headed,
        direct=direct,
        backend_override=backend_override,
        auth_json_path=auth_json_path,
    )

    def fetch(account: Account) -> AccountUsage:
        try:
            state_generation = load_state_generation(account.id)
        except Exception as exc:
            return AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=datetime.now(tz=LOCAL_TZ),
                status=AccountStatus.ERROR,
                error=f"state generation failed: {type(exc).__name__}",
                backend_configured=account.backend,
                cache_invalidated=True,
            )
        usage = _fetch_one(
            config,
            account,
            headed=headed,
            direct=direct,
            backend_override=backend_override,
            auth_json_path=auth_json_path,
            global_lock_held=serial_fetch_required,
            reject_ambiguous_backend_identity=account.id in ambiguous_direct_accounts,
        )
        try:
            current_state_generation = load_state_generation(account.id)
        except Exception as exc:
            return _usage_after_state_generation_failure(
                usage,
                error=f"state generation failed after fetch: {type(exc).__name__}",
            )
        if current_state_generation != state_generation:
            return _usage_after_state_generation_failure(
                usage,
                error="account state changed during fetch",
            )
        if usage.backend_used == "direct" and (
            account.id in shared_direct_auth_accounts
            or account.id in unattributed_direct_accounts
        ):
            usage = _reject_unattributed_direct_usage(usage)
        usage = replace(usage, state_generation=state_generation)
        # A successful transport response can still contain a reset timestamp
        # that was already expired when captured. Such values are not usage.
        usage = expire_reset_windows(
            usage,
            reference_at=usage.captured_at,
        )
        if (
            usage.status != AccountStatus.OK
            or not isinstance(usage.backend_used, str)
            or usage.backend_used not in AUTHENTICATED_BACKENDS
        ):
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
            history_candidates: list[AccountUsage] = []
            for index, usage in enumerate(usages):
                account = accounts_by_id.get(usage.account_id)
                effective_backend = _fetch_effective_backend(
                    account,
                    direct=direct,
                    backend_override=backend_override,
                    auth_json_path=auth_json_path,
                )
                if (
                    account is None
                    or (
                        backend_override is not None
                        and backend_override != account.backend
                    )
                    or not isinstance(effective_backend, str)
                    or not backend_provenance_matches_configured(
                        usage, effective_backend
                    )
                ):
                    continue
                try:
                    save_current_usage(usage)
                    if _should_persist_snapshot(usage):
                        save_usage_snapshot(usage)
                    history_candidates.append(usage)
                except Exception as exc:
                    usages[index] = _usage_after_snapshot_failure(
                        usage,
                        error=f"snapshot save failed: {type(exc).__name__}",
                    )
            if history_candidates:
                try:
                    record_usage_samples_batch(tuple(history_candidates))
                except Exception as exc:
                    for usage in history_candidates:
                        _record_health(
                            "history",
                            "sample_save_failed",
                            account=usage.account_id,
                            error_class=type(exc).__name__,
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
    auth_json_path: Path | None,
) -> bool:
    # Browser requests use isolated persistent profiles. Authenticated requests
    # still need the global lock because provider buckets can overlap.
    if len(accounts) <= 1 or headed:
        return False
    if direct or backend_override is not None or auth_json_path is not None:
        return True
    return any(
        account.backend == "app-server" or account.auth_json_path is not None
        for account in accounts
    )


def _ambiguous_direct_accounts(
    accounts: list[Account],
    *,
    auth_json_path: Path | None = None,
) -> frozenset[str]:
    ambiguous = set(_shared_direct_auth_accounts(accounts, auth_json_path=auth_json_path))
    if auth_json_path is None and _unattributed_direct_accounts(accounts):
        # A default auth.json has no local account binding. Keep direct
        # responses guarded when another configured account exists.
        ambiguous.update(_unattributed_direct_accounts(accounts))
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


def _unattributed_direct_accounts(accounts: list[Account]) -> frozenset[str]:
    if len(accounts) <= 1:
        return frozenset()
    return frozenset(account.id for account in accounts if not account.auth_json_path)


def _shared_direct_auth_accounts(
    accounts: list[Account],
    *,
    auth_json_path: Path | None = None,
) -> frozenset[str]:
    sources: dict[str, list[str]] = {}
    for account in accounts:
        raw_path = account.auth_json_path
        if (
            auth_json_path is None
            and raw_path not in (None, "")
            and not isinstance(raw_path, str)
        ):
            # Keep malformed account records inside per-account error handling.
            source_key = f"<invalid-auth-json:{account.id}>"
        else:
            path = (
                auth_json_path
                if auth_json_path is not None
                else Path(raw_path)
                if raw_path
                else default_auth_json_path()
            )
            try:
                source_key = str(path.expanduser().resolve(strict=False))
            except (OSError, TypeError, ValueError):
                try:
                    source_key = str(path.expanduser())
                except RuntimeError:
                    source_key = str(path)
            except RuntimeError:
                source_key = str(path)
        sources.setdefault(source_key, []).append(account.id)
    return frozenset(
        account_id
        for source_accounts in sources.values()
        if len(source_accounts) > 1
        for account_id in source_accounts
    )


def _usage_after_state_generation_failure(
    usage: AccountUsage,
    *,
    error: str,
) -> AccountUsage:
    if usage.error:
        error = f"{error}; {usage.error}"
    return replace(
        usage,
        five_hour=None,
        weekly=None,
        main=None,
        models=(),
        status=AccountStatus.ERROR,
        error=error,
        values_captured_at=None,
        stale=True,
        cache_invalidated=True,
    )


def _reject_unattributed_direct_usage(usage: AccountUsage) -> AccountUsage:
    error = "direct auth source cannot be attributed to one account"
    if usage.error:
        error = f"{error}; {usage.error}"
    return replace(
        usage,
        five_hour=None,
        weekly=None,
        main=None,
        models=(),
        status=AccountStatus.ERROR,
        error=error,
        values_captured_at=None,
        stale=True,
        cache_invalidated=True,
    )


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
        effective_backend = "direct" if (direct or auth_json_path is not None) else (
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
            or auth_json_path is not None
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
                        if reject_ambiguous_backend_identity:
                            usage = fetch_account_usage_direct(
                                account,
                                auth_json_path=auth_json_path,
                                reject_ambiguous_backend_identity=True,
                            )
                        else:
                            usage = fetch_account_usage_direct(
                                account,
                                auth_json_path=auth_json_path,
                            )
                        fallback_detail = re.sub(r"\s+", " ", str(exc)).strip()
                        return replace(
                            usage,
                            backend_configured=effective_backend,
                            backend_used="direct",
                            fallback_reason=(
                                f"{APP_SERVER_FALLBACK_REASON_PREFIX}{fallback_detail}"
                            )[:500],
                        )
                if reject_ambiguous_backend_identity:
                    usage = fetch_account_usage_direct(
                        account,
                        auth_json_path=auth_json_path,
                        reject_ambiguous_backend_identity=True,
                    )
                else:
                    usage = fetch_account_usage_direct(
                        account,
                        auth_json_path=auth_json_path,
                    )
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
        or not isinstance(previous.backend_used, str)
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
    if previous.stale and (
        not isinstance(previous.fallback_reason, str)
        or previous.fallback_reason not in REUSABLE_RESET_FALLBACK_REASONS
    ):
        return usage
    try:
        age_seconds = (usage.captured_at - previous.captured_at).total_seconds()
    except Exception:
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
    if current is None or not isinstance(current, UsagePool):
        return current
    if not isinstance(current.windows, tuple):
        return current
    current_windows = list(current.windows)
    previous_windows = (
        tuple(previous.main.windows)
        if (
            isinstance(previous.main, UsagePool)
            and isinstance(previous.main.windows, tuple)
        )
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
    try:
        current_reset_at = getattr(current, "reset_at", None)
        previous_reset_at = getattr(previous, "reset_at", None)
        if (
            current is None
            or previous is None
            or current_reset_at is None
            or previous_reset_at is None
        ):
            return False
        if previous_reset_at <= reference_at or current_reset_at <= reference_at:
            return False
        for window, reset_at in (
            (previous, previous_reset_at),
            (current, current_reset_at),
        ):
            duration = _window_duration_seconds(window)
            if duration is None:
                duration = WINDOW_DURATIONS.get(_window_kind(window) or "")
            if duration is None:
                return False
            if reset_at > reference_at + timedelta(
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
            abs((current_reset_at - previous_reset_at).total_seconds())
            > DIRECT_RESET_DISCONTINUITY_SECONDS
        )
    except Exception:
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
            current_duration in LEGACY_WINDOW_DURATIONS
            and previous_duration in LEGACY_WINDOW_DURATIONS
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
    if normalized in {"30d", "30_day", "month", "monthly"}:
        return "thirty_day"
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
            except Exception:
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
            if 0 <= remaining_value <= 100 and abs(remaining_value - percent) >= 0.01:
                return None
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
    auth_json_path: Path | None = None,
) -> bool:
    account_list = _bounded_account_list(accounts)
    results = list(islice(usages, len(account_list) + 1))
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
            configured_backend = _fetch_effective_backend(
                account,
                direct=direct,
                backend_override=backend_override,
                auth_json_path=auth_json_path,
            )
            if account is None or not isinstance(configured_backend, str) or not (
                usage.backend_configured == configured_backend
                and usage.backend_used in AUTHENTICATED_BACKENDS | {"browser"}
                and backend_provenance_matches_configured(
                    usage, configured_backend
                )
            ):
                return False
            if usage.main is not None and not _pool_has_usage_evidence(usage.main):
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
    try:
        account_list = _bounded_account_list(accounts)
    except (TypeError, ValueError):
        return None
    results = list(islice(usages, len(account_list) + 1))
    expected = tuple(account.id for account in account_list)
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
        return (
            isinstance(usage.main, UsagePool)
            and usage.main.has_valid_usage
            and _pool_has_usage_evidence(usage.main)
        )
    return any(
        isinstance(window, LimitWindow) and window.has_usage_value
        for window in (usage.five_hour, usage.weekly)
    )


def _watch_core_resets_current(
    usage: AccountUsage,
    *,
    now: datetime | None = None,
) -> bool:
    if now is None:
        now = datetime.now(tz=LOCAL_TZ)
    if usage.main is not None:
        if not isinstance(usage.main, UsagePool) or not isinstance(usage.main.windows, tuple):
            return False
        windows = usage.main.windows
    else:
        windows = tuple(
            window
            for window in (usage.five_hour, usage.weekly)
            if isinstance(window, LimitWindow)
        )
    if any(not isinstance(window, LimitWindow) for window in windows):
        return False
    for window in windows:
        reset_at = window.reset_at
        if reset_at is None:
            return False
        if not isinstance(reset_at, DATETIME_TYPE) or reset_at.tzinfo is None:
            return False
        try:
            if reset_at <= now:
                return False
            duration = getattr(window, "duration_seconds", None)
            if (
                not isinstance(duration, int)
                or isinstance(duration, bool)
                or duration <= 0
                or duration > MAX_WINDOW_SECONDS
            ):
                duration = WINDOW_DURATIONS.get(_window_kind(window) or "")
            if duration is None or reset_at > now + timedelta(
                seconds=duration + RESET_FUTURE_SKEW_SECONDS
            ):
                return False
        except Exception:
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
        detail = re.sub(r"\s+", " ", str(detail)).strip()[:240] or "watch cycle failed"
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
    if not isinstance(config, AppConfig):
        raise ValueError("config is invalid")
    auth_json_path = _validated_auth_json_path(auth_json_path)
    interval = config.interval_seconds if interval_seconds is None else interval_seconds
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or interval < 60
    ):
        raise ValueError("interval_seconds must be a finite integer of at least 60")
    account_list = _bounded_account_list(accounts)
    stop_event = Event()
    previous_handlers: dict[int, Any] = {}

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
                    auth_json_path=auth_json_path,
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
                message = re.sub(r"\s+", " ", str(exc)).strip()[:240] or type(exc).__name__
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
        for previous_signum, previous_handler in previous_handlers.items():
            try:
                signal.signal(previous_signum, previous_handler)
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
    if not isinstance(config, AppConfig):
        raise ValueError("config is invalid")
    auth_json_path = _validated_auth_json_path(auth_json_path)
    now = datetime.now(tz=LOCAL_TZ)
    account_list = _bounded_account_list(accounts)
    effective_backend = "direct" if (direct or auth_json_path is not None) else None
    if effective_backend is None:
        effective_backend = backend_override
    blocked_snapshots: dict[str, AccountUsage] = {}
    fetch_accounts: list[Account] = []

    def fetch_watchdog_accounts(selected_accounts: Iterable[Account]) -> list[AccountUsage]:
        selected = tuple(selected_accounts)
        try:
            return fetch_all(
                config,
                selected,
                headed=headed,
                direct=direct,
                backend_override=backend_override,
                auth_json_path=auth_json_path,
                save_snapshots=False,
            )
        except Exception as exc:
            return _watch_failure_usages(
                selected,
                None,
                error=f"watchdog fetch failed: {type(exc).__name__}",
            )

    for account in account_list:
        snapshot = load_usage_snapshot(account.id)
        authenticated_fetch = _watchdog_uses_authenticated_fetch(
            account,
            direct=direct,
            backend_override=backend_override,
            auth_json_path=auth_json_path,
        )
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
                authenticated_fetch=authenticated_fetch,
            )
            and not _current_supersedes_blocked_snapshot(
                account,
                snapshot,
                load_current_usage(account.id),
                auth_json_path=auth_json_path,
                configured_backend=effective_backend or account.backend,
                authenticated_fetch=authenticated_fetch,
            )
        ):
            try:
                state_generation = load_state_generation(account.id)
            except Exception:
                # A corrupt generation invalidates the cached block. Let the
                # normal per-account fetch path fail closed if it cannot read
                # the generation either.
                fetch_accounts.append(account)
                continue
            blocked_snapshots[account.id] = replace(
                snapshot,
                state_generation=state_generation,
            )
            continue
        fetch_accounts.append(account)

    fetched = fetch_watchdog_accounts(fetch_accounts)
    evaluation_now = datetime.now(tz=LOCAL_TZ)
    expired_blocked_accounts = [
        account
        for account in account_list
        if account.id in blocked_snapshots
        and not _blocked_until_active(blocked_snapshots[account.id], now=evaluation_now)
    ]
    if expired_blocked_accounts:
        fetched.extend(
            fetch_watchdog_accounts(expired_blocked_accounts)
        )
        for account in expired_blocked_accounts:
            blocked_snapshots.pop(account.id, None)
        evaluation_now = datetime.now(tz=LOCAL_TZ)
    changed_blocked_accounts = [
        account
        for account in account_list
        if account.id in blocked_snapshots
        and not _blocked_snapshot_generation_is_current(
            blocked_snapshots[account.id],
            account.id,
        )
    ]
    if changed_blocked_accounts:
        fetched.extend(
            fetch_watchdog_accounts(changed_blocked_accounts)
        )
        for account in changed_blocked_accounts:
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
                effective_backend = _fetch_effective_backend(
                    account,
                    direct=direct,
                    backend_override=backend_override,
                    auth_json_path=auth_json_path,
                )
                if (
                    not isinstance(effective_backend, str)
                    or (
                        backend_override is not None
                        and backend_override != account.backend
                    )
                    or not backend_provenance_matches_configured(
                        usage, effective_backend
                    )
                ):
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
    authenticated_fetch: bool,
) -> bool:
    if current is None or current.status == AccountStatus.BLOCKED:
        return False
    if current.stale:
        return False
    if not _blocked_snapshot_matches_account(
        account,
        current,
        auth_json_path=auth_json_path,
        configured_backend=configured_backend,
        authenticated_fetch=authenticated_fetch,
    ):
        return False
    try:
        return current.captured_at > blocked_snapshot.captured_at
    except Exception:
        return False


def _blocked_until_active(usage: AccountUsage, *, now: datetime) -> bool:
    try:
        return bool(
            usage.status == AccountStatus.BLOCKED
            and usage.blocked_until is not None
            and usage.blocked_until > now
        )
    except Exception:
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


def _blocked_snapshot_generation_is_current(
    usage: AccountUsage,
    account_id: str,
) -> bool:
    try:
        return (
            usage.state_generation is not None
            and load_state_generation(account_id) == usage.state_generation
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _capture_is_too_far_in_future(
    usage: AccountUsage | None,
    reference_at: datetime,
) -> bool:
    if usage is None:
        return False
    try:
        return usage.captured_at > reference_at + timedelta(seconds=MAX_CAPTURE_FUTURE_SECONDS)
    except Exception:
        return True


def _blocked_snapshot_matches_account(
    account: Account,
    snapshot: AccountUsage,
    *,
    auth_json_path: Path | None,
    configured_backend: str,
    authenticated_fetch: bool,
) -> bool:
    if not backend_provenance_matches_configured(snapshot, configured_backend):
        return False
    if snapshot.backend_used == "browser" and configured_backend in AUTHENTICATED_BACKENDS:
        # Browser blocks are only reusable for authenticated backends when the
        # account identity can be resolved and verified.
        if (
            auth_json_path is None
            and not account.auth_json_path
            and not authenticated_fetch
        ):
            return False
    try:
        if auth_json_path is not None:
            auth_user_id, auth_account_id = auth_identity_from_file(auth_json_path)
        elif account.auth_json_path:
            auth_user_id, auth_account_id = auth_identity_for_account(account)
        elif authenticated_fetch:
            auth_user_id, auth_account_id = auth_identity_from_file(
                default_auth_json_path()
            )
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
        # A current account ID cannot bind a snapshot that has no account ID;
        # another account under the same user could have produced it.
        if snapshot.backend_used == "browser" and configured_backend in AUTHENTICATED_BACKENDS:
            return False
        return (
            auth_account_id is None
            and snapshot.backend_user_id == auth_user_id
        )
    return False


def _watchdog_uses_authenticated_fetch(
    account: Account,
    *,
    direct: bool,
    backend_override: str | None,
    auth_json_path: Path | None,
) -> bool:
    return bool(
        direct
        or auth_json_path is not None
        or backend_override is not None
        or account.backend == "app-server"
        or account.auth_json_path
    )


def _fetch_effective_backend(
    account: Account | None,
    *,
    direct: bool,
    backend_override: str | None,
    auth_json_path: Path | None,
) -> str | None:
    if direct or auth_json_path is not None:
        return "direct"
    if account is None:
        return None
    return backend_override if backend_override is not None else account.backend


def _apply_watchdog_block(usage: AccountUsage, *, now: datetime) -> AccountUsage:
    blocked_until, blocked_reason = _block_state(usage, now=now)
    if blocked_until is not None or blocked_reason is not None:
        return replace(
            usage,
            status=AccountStatus.BLOCKED,
            error=blocked_reason,
            blocked_until=blocked_until,
            blocked_reason=blocked_reason,
        )
    if usage.status == AccountStatus.OK and (
        not _has_usable_core_usage(usage)
        or not _watch_core_resets_current(usage, now=now)
    ):
        return replace(
            usage,
            five_hour=None,
            weekly=None,
            main=None,
            models=(),
            status=AccountStatus.PARTIAL,
            error="missing usage limits; refresh required",
            values_captured_at=None,
            stale=True,
            cache_invalidated=True,
        )
    return usage


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
        if reset_at is None:
            window_name = getattr(window, "name", None)
            unknown_reset_names.append(
                window_name if isinstance(window_name, str) and window_name.strip() else "unknown"
            )
            continue
        reset_at = cast(datetime, reset_at)
        try:
            reset_timezone = reset_at.tzinfo
            reset_offset = reset_at.utcoffset()
        except Exception:
            reset_timezone = None
            reset_offset = None
        window_name = getattr(window, "name", None)
        if not isinstance(window_name, str) or not window_name.strip():
            window_name = "unknown"
        if reset_timezone is None or reset_offset is None:
            unknown_reset_names.append(window_name)
            continue
        saturated_windows.append((reset_at, window_name))
    if unknown_reset_names:
        names = ", ".join(unknown_reset_names)
        return None, f"usage limit reached: {names}; reset time unknown"
    if not saturated_windows:
        return None, None
    try:
        blocked_until, _window_name = max(saturated_windows, key=lambda item: item[0])
        active_names = ", ".join(
            name for reset_at, name in saturated_windows if reset_at == blocked_until
        )
        release_at = blocked_until.isoformat()
        if blocked_until <= now:
            return None, None
    except Exception:
        names = ", ".join(name for _reset_at, name in saturated_windows)
        return None, f"usage limit reached: {names or 'unknown'}; reset time unknown"
    if active_names:
        reason = f"usage limit reached: {active_names}; release at {release_at}"
    else:
        reason = f"usage limit reached; release at {release_at}"
    return blocked_until, reason


def _pool_forces_watchdog_block(pool: Any) -> bool:
    if pool is None:
        return False
    try:
        return bool(
            pool.available is not True
            or pool.allowed is False
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
    except Exception:
        return True


def _watchdog_windows(usage: AccountUsage) -> tuple[Any, ...]:
    if isinstance(usage.main, UsagePool):
        if not isinstance(usage.main.windows, tuple):
            return ()
        if usage.main.windows:
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
    except Exception:
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
        if limit is not None and limit > 0:
            # A positive limit makes remaining an absolute counter, even when
            # its value is greater than 100.
            return remaining > limit or remaining <= 0
        if remaining <= 100:
            return remaining <= 0
        if percent is not None and 0 <= percent <= 100:
            return percent <= 0
        return True
    if percent is not None:
        return not 0 <= percent <= 100 or percent <= 0
    return True


def _should_persist_snapshot(usage: AccountUsage) -> bool:
    if isinstance(usage.status, AccountStatus) and usage.status in {
        AccountStatus.OK,
        AccountStatus.BLOCKED,
    }:
        return True
    return (
        isinstance(usage.status, AccountStatus)
        and
        usage.status == AccountStatus.PARTIAL
        and isinstance(usage.backend_used, str)
        and usage.backend_used in AUTHENTICATED_BACKENDS
    )
