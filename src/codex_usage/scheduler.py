from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .browser import fetch_account_usage
from .config import AppConfig
from .direct import fetch_account_usage_direct
from .models import Account, AccountStatus, AccountUsage
from .render import render_json, render_table
from .state import load_usage_snapshot, save_usage_snapshot


def fetch_all(
    config: AppConfig,
    accounts: Iterable[Account],
    *,
    headed: bool = False,
    direct: bool = False,
    auth_json_path: Path | None = None,
    save_snapshots: bool = False,
) -> list[AccountUsage]:
    account_list = list(accounts)
    usages = [
        _fetch_one(
            config,
            account,
            headed=headed,
            direct=direct
            or _should_fetch_direct(account, headed=headed, auth_json_path=auth_json_path),
            auth_json_path=auth_json_path if (direct or auth_json_path is not None) else None,
        )
        for account in account_list
    ]
    if save_snapshots:
        for index, usage in enumerate(usages):
            if usage.status == AccountStatus.OK:
                try:
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
    auth_json_path: Path | None,
) -> AccountUsage:
    try:
        if direct:
            return fetch_account_usage_direct(account, auth_json_path=auth_json_path)
        return fetch_account_usage(account, config, headed=headed)
    except Exception as exc:
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=datetime.now().astimezone(),
            status=AccountStatus.ERROR,
            error=f"fetch failed: {type(exc).__name__}",
        )


def _should_fetch_direct(
    account: Account,
    *,
    headed: bool,
    auth_json_path: Path | None,
) -> bool:
    if headed:
        return False
    if auth_json_path is not None:
        return True
    return account.auth_json_path is not None


def watch(
    config: AppConfig,
    accounts: Iterable[Account],
    *,
    output: str,
    headed: bool = False,
    direct: bool = False,
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
            if usage.status in {AccountStatus.OK, AccountStatus.BLOCKED}:
                try:
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
    if window.percent is not None and window.percent >= 100:
        return True
    return False
