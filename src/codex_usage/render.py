from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from .config import AppConfig
from .models import AccountUsage, LimitWindow


def render_json(usages: Iterable[AccountUsage]) -> str:
    return json.dumps([usage.as_dict() for usage in usages], ensure_ascii=False, indent=2)


def render_account_overview(config: AppConfig, config_path: Path) -> str:
    rows = [
        [
            account.id,
            account.label,
            _profile_state(account.profile_dir),
            str(Path(account.profile_dir).expanduser()),
        ]
        for account in sorted(config.accounts, key=lambda item: item.id)
    ]
    headers = ["ID", "Label", "Profil", "Pfad"]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows)) if rows else len(header)
        for index, header in enumerate(headers)
    ]
    lines = [
        "Account-Uebersicht",
        "",
        f"Config: {config_path}",
        f"Accounts: {len(config.accounts)}",
        f"Intervall: {config.interval_seconds}s",
        f"Headless: {'ja' if config.headless else 'nein'}",
        f"Analytics: {config.analytics_url}",
        "",
    ]
    if not rows:
        lines.append("Keine Accounts konfiguriert.")
        return "\n".join(lines)
    lines.append(_format_row(headers, widths))
    lines.append("  ".join("-" * width for width in widths))
    lines.extend(_format_row(row, widths) for row in rows)
    return "\n".join(lines)


def render_table(usages: Iterable[AccountUsage]) -> str:
    rows = list(usages)
    now = datetime.now().astimezone().strftime("%d.%m.%Y %H:%M")
    headers = ["Account", "5h genutzt", "5h Reset", "Woche genutzt", "Woche Reset", "Status"]
    data = [
        [
            usage.label,
            _usage_value(usage.five_hour),
            _reset_value(usage.five_hour),
            _usage_value(usage.weekly),
            _reset_value(usage.weekly),
            _status_value(usage),
        ]
        for usage in rows
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in data)) if data else len(header)
        for index, header in enumerate(headers)
    ]
    lines = [f"Stand: {now}", ""]
    lines.append(_format_row(headers, widths))
    lines.append("  ".join("-" * width for width in widths))
    lines.extend(_format_row(row, widths) for row in data)
    return "\n".join(lines)


def _format_row(row: list[str], widths: list[int]) -> str:
    return "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))


def _profile_state(profile_dir: str) -> str:
    path = Path(profile_dir).expanduser()
    if path.is_dir():
        return "vorhanden"
    if path.exists():
        return "kein Ordner"
    return "fehlt"


def _usage_value(window: LimitWindow | None) -> str:
    if window is None:
        return "-"
    parts: list[str] = []
    if window.used is not None and window.limit is not None:
        parts.append(f"{_fmt_number(window.used)} / {_fmt_number(window.limit)}")
    elif window.used is not None:
        parts.append(f"{_fmt_number(window.used)} genutzt")
    elif window.limit is not None:
        parts.append(f"Limit {_fmt_number(window.limit)}")
    if window.percent is not None:
        parts.append(f"{window.percent:.0f}%")
    if not parts and window.raw:
        return _shorten(window.raw, 28)
    return "  ".join(parts) if parts else "-"


def _reset_value(window: LimitWindow | None) -> str:
    if window is None or window.reset_at is None:
        return "-"
    return window.reset_at.astimezone().strftime("%d.%m.%Y %H:%M")


def _status_value(usage: AccountUsage) -> str:
    if usage.error:
        return f"{usage.status.value}: {_shorten(usage.error, 30)}"
    return usage.status.value


def _fmt_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def _shorten(value: str, max_len: int) -> str:
    clean = " ".join(value.split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"
