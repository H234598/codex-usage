from __future__ import annotations

import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .account_lock import account_lock
from .app_server import AppServerUnavailableError, fetch_account_usage_app_server
from .browser import fetch_account_usage
from .config import AppConfig
from .direct import fetch_account_usage_direct
from .models import Account, AccountStatus, AccountUsage
from .render import render_json, render_table
from .state import load_usage_snapshot, save_current_usage, save_usage_snapshot


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

    def fetch(account: Account) -> AccountUsage:
        return _fetch_one(
            config,
            account,
            headed=headed,
            direct=direct,
            backend_override=backend_override,
            auth_json_path=auth_json_path if (direct or auth_json_path is not None) else None,
        )

    if len(account_list) > 1:
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
    return usages


def _fetch_one(
    config: AppConfig,
    account: Account,
    *,
    headed: bool,
    direct: bool,
    backend_override: str | None,
    auth_json_path: Path | None,
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
            with account_lock(account.id):
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
        usage = fetch_account_usage(account, config, headed=headed)
        return replace(
            usage,
            backend_configured=account.backend,
            backend_used="browser",
        )
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
    while True:
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
        time.sleep(interval)


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
        if snapshot is not None and _blocked_until_active(snapshot, now=now):
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
    fetched_by_id = {usage.account_id: usage for usage in fetched}

    usages: list[AccountUsage] = []
    for account in account_list:
        usage = blocked_snapshots.get(account.id) or fetched_by_id.get(account.id)
        if usage is None:
            continue
        if account.id not in blocked_snapshots:
            usage = _apply_watchdog_block(usage, now=now)
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
    if window.remaining is not None and window.remaining <= 0:
        return True
    if window.used is not None and window.limit is not None and window.used >= window.limit:
        return True
    if window.percent is not None and window.percent <= 0:
        return True
    return False
