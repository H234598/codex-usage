from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .config import AppConfig
from .extractor import LOCAL_TZ
from .models import Account, AccountStatus, AccountUsage, LimitWindow
from .state import backend_provenance_matches_configured

ACCOUNT_CELL_MAX = 40
PATH_CELL_MAX = 80
STATUS_CELL_MAX = 40
VALUE_CELL_MAX = 28
AUTH_CELL_MAX = 28


def render_json(usages: Iterable[AccountUsage]) -> str:
    return json.dumps(
        [_safe_usage_for_display(usage).as_dict() for usage in usages],
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
            *_overview_usage_values(
                usage_by_account.get(account.id),
                expected_backend=account.backend,
            ),
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
        "Weitere Limits",
        "Spark",
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
            *_overview_usage_values(
                usages.get(account.id),
                expected_backend=account.backend,
            ),
        ]
        for account in sorted(accounts, key=lambda item: item.id)
    ]
    headers = [
        "Account",
        "5h Wert",
        "5h Reset",
        "Woche Wert",
        "Woche Reset",
        "Weitere Limits",
        "Spark",
        "Status",
    ]
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
        "Weitere Limits",
        "Spark",
        "Auth",
        "Status",
    ]
    data = []
    for usage in rows:
        safe_usage = _safe_usage_for_display(usage)
        data.append(
            [
                _cell(safe_usage.label, ACCOUNT_CELL_MAX),
                _usage_value(safe_usage.five_hour),
                _reset_value(safe_usage.five_hour),
                _usage_value(safe_usage.weekly),
                _reset_value(safe_usage.weekly),
                _extra_main_value(safe_usage),
                _spark_value(safe_usage),
                _auth_value(safe_usage),
                _status_value(safe_usage),
            ]
        )
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


def _overview_usage_values(
    usage: AccountUsage | None,
    *,
    expected_backend: str | None = None,
) -> list[str]:
    if usage is None:
        return ["-", "-", "-", "-", "-", "-", "-"]
    usage = _safe_usage_for_display(usage, expected_backend=expected_backend)
    return [
        _cell(_usage_value(usage.five_hour), VALUE_CELL_MAX),
        _reset_value(usage.five_hour),
        _cell(_usage_value(usage.weekly), VALUE_CELL_MAX),
        _reset_value(usage.weekly),
        _cell(_extra_main_value(usage), VALUE_CELL_MAX),
        _cell(_spark_value(usage), VALUE_CELL_MAX),
        _cell(_status_value(usage), STATUS_CELL_MAX),
    ]


def _safe_usage_for_display(
    usage: AccountUsage,
    *,
    expected_backend: str | None = None,
) -> AccountUsage:
    if not isinstance(usage.status, AccountStatus):
        return replace(
            usage,
            five_hour=None,
            weekly=None,
            main=None,
            models=(),
            error="invalid account status",
            status=AccountStatus.ERROR,
            values_captured_at=None,
            stale=True,
            cache_invalidated=True,
        )
    if usage.status in {AccountStatus.ERROR, AccountStatus.LOGIN_REQUIRED}:
        error = usage.error or (
            "login required"
            if usage.status == AccountStatus.LOGIN_REQUIRED
            else "usage error"
        )
        return replace(
            usage,
            five_hour=None,
            weekly=None,
            main=None,
            models=(),
            error=error,
            values_captured_at=None,
            stale=True,
            cache_invalidated=True,
        )
    if _usage_provenance_is_displayable(usage, expected_backend=expected_backend):
        return usage
    error = "incomplete usage backend provenance"
    if usage.error:
        error = f"{usage.error}; {error}"
    return replace(
        usage,
        five_hour=None,
        weekly=None,
        main=None,
        models=(),
        error=error,
        status=(
            AccountStatus.PARTIAL
            if usage.status == AccountStatus.OK
            else usage.status
        ),
        values_captured_at=None,
        stale=True,
        cache_invalidated=True,
    )


def _usage_provenance_is_displayable(
    usage: AccountUsage,
    *,
    expected_backend: str | None = None,
) -> bool:
    if usage.cache_invalidated:
        return False
    configured_backend = (
        expected_backend
        if expected_backend is not None
        else usage.backend_configured
    )
    if not isinstance(configured_backend, str) or not configured_backend:
        return False
    try:
        return backend_provenance_matches_configured(usage, configured_backend)
    except (AttributeError, TypeError, ValueError):
        return False


def _extra_main_value(usage: AccountUsage) -> str:
    if (
        usage.cache_invalidated
        or usage.main is None
        or usage.main.available is not True
        or not usage.main.has_valid_usage
    ):
        return "-"
    core_windows = {
        id(window)
        for window in (
            usage.main.window_for_duration(18_000),
            usage.main.window_for_duration(604_800),
        )
        if window is not None
    }
    values = [
        f"{window.name} {_usage_value(window)}"
        for window in usage.main.windows
        if id(window) not in core_windows
    ]
    return "; ".join(values) if values else "-"


def _spark_value(usage: AccountUsage) -> str:
    if usage.cache_invalidated:
        return "nicht verfügbar"
    pool = usage.model_pool("gpt-5.3-codex-spark")
    if pool is None or pool.available is not True:
        return "nicht verfügbar"
    if not isinstance(pool.windows, tuple):
        return "nicht verfügbar"
    if not pool.windows:
        return "erschöpft" if pool.exhausted else "verfügbar; Limit unbekannt"
    if not pool.has_valid_usage:
        return "nicht verfügbar"
    if pool.exhausted:
        return "erschöpft"
    values = []
    for window in pool.windows:
        value = f"{window.name} {_usage_value(window)}"
        if window.reset_at is not None:
            value += f" bis {_reset_value(window)}"
        values.append(value)
    return "; ".join(values)


def _usage_value(window: LimitWindow | None) -> str:
    if window is None:
        return "-"
    if window.has_invalid_usage_value:
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
    if remaining_percent is not None:
        parts.append(f"{_fmt_number(remaining_percent)}% verbleibend")
    if not parts and isinstance(window.raw, str) and window.raw:
        return _shorten(window.raw, 28)
    return "  ".join(parts) if parts else "-"


def _is_remaining_percent_window(window: LimitWindow) -> bool:
    if window.has_invalid_usage_value:
        return False
    remaining = _valid_percent(window.remaining)
    percent = _valid_percent(window.percent)
    if (
        remaining is None
        or percent is None
        or abs(remaining - percent) >= 0.01
    ):
        return False
    if _is_finite_number(window.used) and _is_finite_number(window.limit):
        used = float(window.used)
        limit = float(window.limit)
        if limit <= 0 or used < 0:
            return False
        derived = (limit - used) * 100 / limit
        return abs(remaining - derived) < 0.01
    return window.limit is None or (
        _is_finite_number(window.limit) and abs(float(window.limit) - 100) < 0.01
    )


def _remaining_percent(window: LimitWindow) -> float | None:
    if window.has_invalid_usage_value:
        return None
    if any(
        value is not None and not _is_finite_number(value)
        for value in (window.used, window.limit, window.remaining, window.percent)
    ):
        return None
    if window.percent is not None and _valid_percent(window.percent) is None:
        return None
    if window.limit is not None and float(window.limit) <= 0:
        return None
    if (
        window.limit is None
        and _is_finite_number(window.remaining)
        and _is_finite_number(window.percent)
    ):
        remaining_value = float(window.remaining)
        percent_value = float(window.percent)
        if 0 <= remaining_value <= 100 and abs(remaining_value - percent_value) >= 0.01:
            return None
    if (
        _is_finite_number(window.used)
        and _is_finite_number(window.limit)
    ):
        used = float(window.used)
        limit = float(window.limit)
        if used < 0:
            return None
        remaining = (limit - used) * 100 / limit
        return max(0.0, min(100.0, remaining))
    if _is_finite_number(window.remaining):
        if _is_finite_number(window.limit) and float(window.limit) > 0:
            remaining_value = float(window.remaining)
            if not 0 <= remaining_value <= float(window.limit):
                return None
            return max(
                0.0,
                min(100.0, remaining_value * 100 / float(window.limit)),
            )
        if _is_finite_number(window.percent):
            return _valid_percent(window.percent)
        if not 0 <= float(window.remaining) <= 100:
            return None
        return float(window.remaining)
    if _is_finite_number(window.percent):
        return _valid_percent(window.percent)
    return None


def _reset_value(window: LimitWindow | None) -> str:
    if window is None or window.reset_at is None:
        return "-"
    try:
        return window.reset_at.strftime("%d.%m.%Y %H:%M")
    except (AttributeError, OverflowError, TypeError, ValueError):
        return "-"


def _status_value(usage: AccountUsage) -> str:
    if usage.status == AccountStatus.BLOCKED:
        parts = ["blocked"]
        if usage.blocked_until is not None:
            try:
                blocked_until = usage.blocked_until.strftime("%d.%m.%Y %H:%M")
            except (AttributeError, OverflowError, TypeError, ValueError):
                blocked_until = None
            if blocked_until is not None:
                parts.append(f"bis {blocked_until}")
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
    try:
        expiry = usage.auth_access_expires_at
        if expiry is None:
            if usage.auth_last_refresh is None:
                return "-"
            return f"refresh {usage.auth_last_refresh.strftime('%d.%m.%Y %H:%M')}"
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            expiry = expiry.replace(tzinfo=LOCAL_TZ)
        stamp = expiry.strftime("%d.%m.%Y %H:%M")
        if expiry <= datetime.now(tz=LOCAL_TZ):
            return f"abgelaufen {stamp}"
        return f"bis {stamp}"
    except (AttributeError, OverflowError, TypeError, ValueError):
        return "-"


def _fmt_number(value: float) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return "-"
    if not math.isfinite(number):
        return "-"
    return str(int(number)) if number.is_integer() else f"{number:.2f}".rstrip("0").rstrip(".")


def _is_finite_number(value: float | None) -> bool:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _valid_percent(value: float | None) -> float | None:
    if not _is_finite_number(value):
        return None
    number = float(value)
    return number if 0 <= number <= 100 else None


def _cell(value: str, max_len: int) -> str:
    return _shorten(str(value), max_len)


def _shorten(value: str, max_len: int) -> str:
    clean = " ".join(value.split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"
