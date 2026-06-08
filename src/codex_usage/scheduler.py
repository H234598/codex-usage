from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .browser import fetch_account_usage
from .config import AppConfig
from .direct import fetch_account_usage_direct
from .models import Account, AccountStatus, AccountUsage
from .render import render_json, render_table
from .state import save_usage_snapshot


def fetch_all(
    config: AppConfig,
    accounts: Iterable[Account],
    *,
    headed: bool = False,
    direct: bool = False,
    auth_json_path: Path | None = None,
    save_snapshots: bool = False,
) -> list[AccountUsage]:
    usages = [
        _fetch_one(
            config,
            account,
            headed=headed,
            direct=direct,
            auth_json_path=auth_json_path,
        )
        for account in accounts
    ]
    if save_snapshots:
        for index, usage in enumerate(usages):
            if usage.error is None:
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
            save_snapshots=direct,
        )
        if output == "json":
            print(render_json(usages), flush=True)
        else:
            print("\033[2J\033[H", end="")
            print(render_table(usages), flush=True)
        time.sleep(interval)
