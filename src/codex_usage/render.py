from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

from .config import AppConfig
from .models import Account, AccountUsage, LimitWindow


def render_json(usages: Iterable[AccountUsage]) -> str:
    return json.dumps([usage.as_dict() for usage in usages], ensure_ascii=False, indent=2)


def render_account_overview(
    config: AppConfig,
    config_path: Path,
    usages: Mapping[str, AccountUsage] | None = None,
) -> str:
    usage_by_account = usages or {}
    rows = [
        [
            account.id,
            account.label,
            account.browser,
            _auth_state(account.auth_json_path),
            *_overview_usage_values(usage_by_account.get(account.id)),
            _profile_state(account.profile_dir),
            str(Path(account.profile_dir).expanduser()),
        ]
        for account in sorted(config.accounts, key=lambda item: item.id)
    ]
    headers = [
        "ID",
        "Label",
        "Browser",
        "Auth JSON",
        "5h Wert",
        "5h Reset",
        "Woche Wert",
        "Woche Reset",
        "Status",
        "Profil",
        "Pfad",
    ]
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


def render_account_values(
    accounts: Iterable[Account],
    usages: Mapping[str, AccountUsage],
) -> str:
    rows = [
        [
            account.label,
            *_overview_usage_values(usages.get(account.id)),
        ]
        for account in sorted(accounts, key=lambda item: item.id)
    ]
    headers = ["Account", "5h Wert", "5h Reset", "Woche Wert", "Woche Reset", "Status"]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows)) if rows else len(header)
        for index, header in enumerate(headers)
    ]
    lines = [_format_row(headers, widths), "  ".join("-" * width for width in widths)]
    lines.extend(_format_row(row, widths) for row in rows)
    return "\n".join(lines)


def render_table(usages: Iterable[AccountUsage]) -> str:
    rows = list(usages)
    now = datetime.now().astimezone().strftime("%d.%m.%Y %H:%M")
    headers = ["Account", "5h Wert", "5h Reset", "Woche Wert", "Woche Reset", "Status"]
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


def _auth_state(auth_json_path: str | None) -> str:
    if not auth_json_path:
        return "-"
    path = Path(auth_json_path).expanduser()
    if path.is_file():
        return "vorhanden"
    if path.exists():
        return "keine Datei"
    return "fehlt"


def _overview_usage_values(usage: AccountUsage | None) -> list[str]:
    if usage is None:
        return ["-", "-", "-", "-", "-"]
    return [
        _usage_value(usage.five_hour),
        _reset_value(usage.five_hour),
        _usage_value(usage.weekly),
        _reset_value(usage.weekly),
        _status_value(usage),
    ]


def _usage_value(window: LimitWindow | None) -> str:
    if window is None:
        return "-"
    if _is_remaining_percent_window(window):
        return f"{window.remaining:.0f}% verbleibend"
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


def _is_remaining_percent_window(window: LimitWindow) -> bool:
    return (
        window.remaining is not None
        and window.percent is not None
        and abs(window.remaining - window.percent) < 0.01
        and (window.limit is None or abs(window.limit - 100) < 0.01)
    )


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
