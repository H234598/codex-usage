from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path

from .browser import fetch_account_usage
from .config import AppConfig
from .direct import fetch_account_usage_direct
from .models import Account, AccountUsage
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
        fetch_account_usage_direct(account, auth_json_path=auth_json_path)
        if direct
        else fetch_account_usage(account, config, headed=headed)
        for account in accounts
    ]
    if save_snapshots:
        for usage in usages:
            save_usage_snapshot(usage)
    return usages


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
