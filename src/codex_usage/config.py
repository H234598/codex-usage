from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .models import Account

APP_NAME = "codex-usage"
SUPPORTED_BROWSERS = ("firefox", "chromium")


@dataclass(frozen=True)
class AppConfig:
    accounts: tuple[Account, ...]
    interval_seconds: int = 300
    analytics_url: str = "https://chatgpt.com/codex/cloud/settings/analytics"
    headless: bool = True


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / APP_NAME / "config.toml"


def default_state_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / APP_NAME


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or default_config_path()
    if not config_path.exists():
        return AppConfig(accounts=())

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    raw_accounts = data.get("accounts", [])
    if not isinstance(raw_accounts, list):
        raise ValueError("accounts must be a list of TOML tables")
    accounts = tuple(_account_from_data(item) for item in raw_accounts)
    _validate_unique_accounts(accounts)
    interval = _strict_int(data.get("interval_seconds", 300), "interval_seconds")
    if interval < 60:
        raise ValueError("interval_seconds must be at least 60")
    analytics_url = str(
        data.get("analytics_url", "https://chatgpt.com/codex/cloud/settings/analytics")
    )
    _validate_analytics_url(analytics_url)
    headless = _strict_bool(data.get("headless", True), "headless")
    return AppConfig(
        accounts=accounts,
        interval_seconds=interval,
        analytics_url=analytics_url,
        headless=headless,
    )


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    config_path.write_text(_to_toml(config), encoding="utf-8")
    try:
        config_path.chmod(0o600)
    except OSError:
        pass
    return config_path


def add_or_update_account(
    account_id: str,
    label: str | None = None,
    profile_dir: str | None = None,
    browser: str | None = None,
    auth_json_path: str | None = None,
    path: Path | None = None,
) -> tuple[AppConfig, Account]:
    _validate_account_id(account_id)
    if browser is not None:
        _validate_browser(browser)
    config = load_config(path)
    existing = next((item for item in config.accounts if item.id == account_id), None)
    account = Account(
        id=account_id,
        label=label or (existing.label if existing else account_id),
        profile_dir=profile_dir
        or (existing.profile_dir if existing else str(_default_profile_root(account_id))),
        browser=browser or (existing.browser if existing else "firefox"),
        auth_json_path=auth_json_path
        if auth_json_path is not None
        else (existing.auth_json_path if existing else None),
    )

    accounts = [item for item in config.accounts if item.id != account_id]
    accounts.append(account)
    updated = AppConfig(
        accounts=tuple(accounts),
        interval_seconds=config.interval_seconds,
        analytics_url=config.analytics_url,
        headless=config.headless,
    )
    _prepare_profile_dir(account.profile_dir)
    save_config(updated, path)
    return updated, account


def remove_account(account_ref: str, path: Path | None = None) -> tuple[AppConfig, Account]:
    config = load_config(path)
    account = resolve_account(config, account_ref)
    updated = AppConfig(
        accounts=tuple(item for item in config.accounts if item.id != account.id),
        interval_seconds=config.interval_seconds,
        analytics_url=config.analytics_url,
        headless=config.headless,
    )
    save_config(updated, path)
    return updated, account


def get_account(config: AppConfig, account_id: str) -> Account:
    for account in config.accounts:
        if account.id == account_id:
            return account
    raise KeyError(f"unknown account: {account_id}")


def resolve_account(config: AppConfig, account_ref: str) -> Account:
    for account in config.accounts:
        if account.id == account_ref:
            return account

    matches = [account for account in config.accounts if account.label == account_ref]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = ", ".join(account.id for account in matches)
        raise KeyError(f"ambiguous account label: {account_ref}; matching ids: {ids}")

    available = ", ".join(f"{account.id} ({account.label})" for account in config.accounts)
    detail = f"; available accounts: {available}" if available else ""
    raise KeyError(f"unknown account: {account_ref}{detail}")


def _account_from_data(item: object) -> Account:
    if not isinstance(item, dict):
        raise ValueError("account entry must be a TOML table")
    account_id = str(item.get("id", "")).strip()
    _validate_account_id(account_id)
    label = str(item.get("label") or account_id)
    profile_dir = str(item.get("profile_dir") or _default_profile_root(account_id))
    browser = str(item.get("browser") or "firefox")
    _validate_browser(browser)
    auth_json_path = item.get("auth_json_path")
    return Account(
        id=account_id,
        label=label,
        profile_dir=profile_dir,
        browser=browser,
        auth_json_path=str(auth_json_path) if auth_json_path else None,
    )


def _validate_account_id(account_id: str) -> None:
    if account_id in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", account_id):
        raise ValueError("account id must be 1-64 chars: letters, digits, underscore, dot, dash")


def _safe_profile_name(account_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", account_id)


def _default_profile_root(account_id: str) -> Path:
    return default_state_dir() / "profiles" / _safe_profile_name(account_id)


def _prepare_profile_dir(profile_dir: str) -> Path:
    path = Path(profile_dir).expanduser()
    if path.is_symlink():
        raise ValueError(f"profile dir must not be a symlink: {path}")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"profile dir must not be a symlink: {path}")
    if not path.is_dir():
        raise ValueError(f"profile path is not a directory: {path}")
    try:
        path.chmod(0o700)
    except OSError:
        pass
    marker = path / ".codex-usage-profile"
    if marker.is_symlink():
        raise ValueError(f"profile marker must not be a symlink: {marker}")
    if not marker.exists():
        marker.write_text("codex-usage persistent browser profile\n", encoding="utf-8")
        try:
            marker.chmod(0o600)
        except OSError:
            pass
    return path


def _validate_unique_accounts(accounts: tuple[Account, ...]) -> None:
    seen: set[str] = set()
    for account in accounts:
        if account.id in seen:
            raise ValueError(f"duplicate account id: {account.id}")
        seen.add(account.id)


def _validate_analytics_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "chatgpt.com":
        raise ValueError("analytics_url must be an https://chatgpt.com URL")
    if not parts.path.startswith("/codex/cloud/settings/analytics"):
        raise ValueError("analytics_url must point to /codex/cloud/settings/analytics")


def _validate_browser(browser: str) -> None:
    if browser not in SUPPORTED_BROWSERS:
        choices = ", ".join(SUPPORTED_BROWSERS)
        raise ValueError(f"browser must be one of: {choices}")


def _strict_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _to_toml(config: AppConfig) -> str:
    lines = [
        f"interval_seconds = {config.interval_seconds}",
        f"analytics_url = {_quote(config.analytics_url)}",
        f"headless = {'true' if config.headless else 'false'}",
        "",
    ]
    for account in sorted(config.accounts, key=lambda item: item.id):
        lines.extend(
            [
                "[[accounts]]",
                f"id = {_quote(account.id)}",
                f"label = {_quote(account.label)}",
                f"profile_dir = {_quote(account.profile_dir)}",
                f"browser = {_quote(account.browser)}",
                *(
                    [f"auth_json_path = {_quote(account.auth_json_path)}"]
                    if account.auth_json_path
                    else []
                ),
                "",
            ]
        )
    return "\n".join(lines)


def _quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    escaped = "".join(
        char if ord(char) >= 0x20 else f"\\u{ord(char):04x}" for char in escaped
    )
    return f'"{escaped}"'
