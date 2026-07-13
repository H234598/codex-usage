from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

from .config import AppConfig
from .extractor import LOCAL_TZ
from .models import Account, AccountStatus, AccountUsage, LimitWindow

ACCOUNT_CELL_MAX = 40
PATH_CELL_MAX = 80
STATUS_CELL_MAX = 40
VALUE_CELL_MAX = 28
AUTH_CELL_MAX = 28


def render_json(usages: Iterable[AccountUsage]) -> str:
    return json.dumps(
        [usage.as_dict() for usage in usages],
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )


def render_account_overview(
    config: AppConfig,
    config_path: Path,
    usages: Mapping[str, AccountUsage] | None = None,
) -> str:
    usage_by_account = usages or {}
    rows = [
        [
            _cell(account.id, 64),
            _cell(account.label, ACCOUNT_CELL_MAX),
            _cell(account.browser, 16),
            _cell(account.backend, 16),
            _auth_state(account.auth_json_path),
            _auth_value(usage_by_account.get(account.id)),
            *_overview_usage_values(usage_by_account.get(account.id)),
            _profile_state(account.profile_dir),
            _cell(str(Path(account.profile_dir).expanduser()), PATH_CELL_MAX),
        ]
        for account in sorted(config.accounts, key=lambda item: item.id)
    ]
    headers = [
        "ID",
        "Label",
        "Browser",
        "Backend",
        "Auth JSON",
        "Auth",
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
            _cell(account.label, ACCOUNT_CELL_MAX),
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
    now = datetime.now(tz=LOCAL_TZ).strftime("%d.%m.%Y %H:%M")
    headers = [
        "Account",
        "5h Wert",
        "5h Reset",
        "Woche Wert",
        "Woche Reset",
        "Auth",
        "Status",
    ]
    data = [
        [
            _cell(usage.label, ACCOUNT_CELL_MAX),
            _usage_value(usage.five_hour),
            _reset_value(usage.five_hour),
            _usage_value(usage.weekly),
            _reset_value(usage.weekly),
            _auth_value(usage),
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
        _cell(_usage_value(usage.five_hour), VALUE_CELL_MAX),
        _reset_value(usage.five_hour),
        _cell(_usage_value(usage.weekly), VALUE_CELL_MAX),
        _reset_value(usage.weekly),
        _cell(_status_value(usage), STATUS_CELL_MAX),
    ]


def _usage_value(window: LimitWindow | None) -> str:
    if window is None:
        return "-"
    if _is_remaining_percent_window(window):
        return f"{window.remaining:.0f}% verbleibend"
    parts: list[str] = []
    if window.used is not None and window.limit is not None:
        used = _fmt_number(window.used)
        limit = _fmt_number(window.limit)
        if used != "-" and limit != "-":
            parts.append(f"{used} / {limit}")
    elif window.used is not None:
        used = _fmt_number(window.used)
        if used != "-":
            parts.append(f"{used} genutzt")
    elif window.limit is not None and window.remaining is None:
        limit = _fmt_number(window.limit)
        if limit != "-":
            parts.append(f"Limit {limit}")
    remaining_percent = _remaining_percent(window)
    if remaining_percent is not None and (
        window.used is not None or window.remaining is not None
    ):
        parts.append(f"{_fmt_number(remaining_percent)}% verbleibend")
    elif window.percent is not None:
        percent = _fmt_number(window.percent)
        if percent != "-":
            parts.append(f"{percent}%")
    if not parts and window.raw:
        return _shorten(window.raw, 28)
    return "  ".join(parts) if parts else "-"


def _is_remaining_percent_window(window: LimitWindow) -> bool:
    if not (
        _is_finite_number(window.remaining)
        and _is_finite_number(window.percent)
        and abs(float(window.remaining) - float(window.percent)) < 0.01
    ):
        return False
    if _is_finite_number(window.used) and _is_finite_number(window.limit):
        if float(window.limit) <= 0:
            return False
        derived = (float(window.limit) - float(window.used)) * 100 / float(window.limit)
        return abs(float(window.remaining) - derived) < 0.01
    return window.limit is None or (
        _is_finite_number(window.limit) and abs(float(window.limit) - 100) < 0.01
    )


def _remaining_percent(window: LimitWindow) -> float | None:
    if (
        _is_finite_number(window.used)
        and _is_finite_number(window.limit)
        and float(window.limit) > 0
    ):
        remaining = (float(window.limit) - float(window.used)) * 100 / float(window.limit)
        return max(0.0, min(100.0, remaining))
    if _is_finite_number(window.remaining):
        if _is_finite_number(window.limit) and float(window.limit) > 0:
            return max(
                0.0,
                min(100.0, float(window.remaining) * 100 / float(window.limit)),
            )
        return max(0.0, min(100.0, float(window.remaining)))
    return None


def _reset_value(window: LimitWindow | None) -> str:
    if window is None or window.reset_at is None:
        return "-"
    return window.reset_at.strftime("%d.%m.%Y %H:%M")


def _status_value(usage: AccountUsage) -> str:
    if usage.status == AccountStatus.BLOCKED:
        parts = ["blocked"]
        if usage.blocked_until is not None:
            parts.append(f"bis {usage.blocked_until.strftime('%d.%m.%Y %H:%M')}")
        if usage.blocked_reason:
            parts.append(f": {_shorten(usage.blocked_reason, 30)}")
        status = " ".join(parts)
    elif usage.error:
        status = f"{usage.status.value}: {_shorten(usage.error, 30)}"
    else:
        status = usage.status.value
    if usage.stale:
        status += " (gespeichert)"
    return status


def _auth_value(usage: AccountUsage | None) -> str:
    if usage is None:
        return "-"
    expiry = usage.auth_access_expires_at
    if expiry is None:
        if usage.auth_last_refresh is None:
            return "-"
        return f"refresh {usage.auth_last_refresh.strftime('%d.%m.%Y %H:%M')}"
    stamp = expiry.strftime("%d.%m.%Y %H:%M")
    if expiry <= datetime.now(tz=LOCAL_TZ):
        return f"abgelaufen {stamp}"
    return f"bis {stamp}"


def _fmt_number(value: float) -> str:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return "-"
    if not math.isfinite(number):
        return "-"
    return str(int(number)) if number.is_integer() else f"{number:.2f}".rstrip("0").rstrip(".")


def _is_finite_number(value: float | None) -> bool:
    if value is None:
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _cell(value: str, max_len: int) -> str:
    return _shorten(str(value), max_len)


def _shorten(value: str, max_len: int) -> str:
    clean = " ".join(value.split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"
