from __future__ import annotations

import time
from collections.abc import Iterable

from .browser import fetch_account_usage
from .config import AppConfig
from .models import Account, AccountUsage
from .render import render_json, render_table


def fetch_all(
    config: AppConfig,
    accounts: Iterable[Account],
    *,
    headed: bool = False,
) -> list[AccountUsage]:
    return [fetch_account_usage(account, config, headed=headed) for account in accounts]


def watch(
    config: AppConfig,
    accounts: Iterable[Account],
    *,
    output: str,
    headed: bool = False,
    interval_seconds: int | None = None,
) -> None:
    interval = interval_seconds or config.interval_seconds
    account_list = list(accounts)
    while True:
        usages = fetch_all(config, account_list, headed=headed)
        if output == "json":
            print(render_json(usages), flush=True)
        else:
            print("\033[2J\033[H", end="")
            print(render_table(usages), flush=True)
        time.sleep(interval)
