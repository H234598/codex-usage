from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .models import Account
from .private_io import (
    assert_no_symlink_ancestors,
    private_path_lock,
    read_private_text,
    write_private_text,
)

APP_NAME = "codex-usage"
SUPPORTED_BROWSERS = ("firefox", "chromium")
SUPPORTED_BACKENDS = ("direct", "app-server")
MAX_CONFIG_BYTES = 1_000_000
MAX_CONFIG_ACCOUNTS = 100
MAX_CONFIG_LABEL_CHARS = 256
MAX_CONFIG_PATH_CHARS = 4096
MAX_CONFIG_URL_CHARS = 2048


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
        if config_path.is_symlink():
            raise ValueError(f"config path must be a regular file: {config_path}")
        return AppConfig(accounts=())

    data = tomllib.loads(_read_config_text(config_path))
    raw_accounts = data.get("accounts", [])
    if not isinstance(raw_accounts, list):
        raise ValueError("accounts must be a list of TOML tables")
    if len(raw_accounts) > MAX_CONFIG_ACCOUNTS:
        raise ValueError(
            f"accounts must contain at most {MAX_CONFIG_ACCOUNTS} entries"
        )
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
    config = AppConfig(
        accounts=accounts,
        interval_seconds=interval,
        analytics_url=analytics_url,
        headless=headless,
    )
    _validate_config(config)
    return config


def _read_config_text(config_path: Path) -> str:
    text, _ = read_private_text(
        config_path,
        regular_label="config path",
        read_label="config file",
        max_bytes=MAX_CONFIG_BYTES,
        too_large_label="config file",
        invalid_utf8_label="config file",
    )
    return text


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    _validate_config(config)
    config_path = path or default_config_path()
    _prepare_config_directory(config_path.parent)
    with private_path_lock(config_path, label="config lock"):
        _save_config_unlocked(config, config_path)
    return config_path


def _save_config_unlocked(config: AppConfig, config_path: Path) -> None:
    text = _to_toml(config)
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError(f"config file too large; max {MAX_CONFIG_BYTES} bytes")
    write_private_text(config_path, text, label="config path")


def _prepare_config_directory(config_dir: Path) -> None:
    assert_no_symlink_ancestors(config_dir, label="config directory")
    if config_dir.is_symlink():
        raise ValueError(f"config directory must not be a symlink: {config_dir}")
    config_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    if config_dir.is_symlink() or not config_dir.is_dir():
        raise ValueError(f"config directory is not a real directory: {config_dir}")
    try:
        config_dir.chmod(0o700)
    except OSError:
        pass


def add_or_update_account(
    account_id: str,
    label: str | None = None,
    profile_dir: str | None = None,
    browser: str | None = None,
    auth_json_path: str | None = None,
    backend: str | None = None,
    path: Path | None = None,
) -> tuple[AppConfig, Account]:
    _validate_account_id(account_id)
    if browser is not None:
        _validate_browser(browser)
    if backend is not None:
        _validate_backend(backend)
    config_path = path or default_config_path()
    _prepare_config_directory(config_path.parent)
    with private_path_lock(config_path, label="config lock"):
        config = load_config(config_path)
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
            backend=backend or (existing.backend if existing else "direct"),
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
        _validate_config(updated)
        _save_config_unlocked(updated, config_path)
    return updated, account


def remove_account(account_ref: str, path: Path | None = None) -> tuple[AppConfig, Account]:
    config_path = path or default_config_path()
    _prepare_config_directory(config_path.parent)
    with private_path_lock(config_path, label="config lock"):
        config = load_config(config_path)
        account = resolve_account(config, account_ref)
        updated = AppConfig(
            accounts=tuple(item for item in config.accounts if item.id != account.id),
            interval_seconds=config.interval_seconds,
            analytics_url=config.analytics_url,
            headless=config.headless,
        )
        _validate_config(updated)
        _save_config_unlocked(updated, config_path)
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
    backend = str(item.get("backend") or "direct")
    _validate_backend(backend)
    return Account(
        id=account_id,
        label=label,
        profile_dir=profile_dir,
        browser=browser,
        auth_json_path=str(auth_json_path) if auth_json_path else None,
        backend=backend,
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
    assert_no_symlink_ancestors(path, label="profile dir")
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
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        raise ValueError(f"profile marker must be a regular file: {marker}")
    if not marker.exists():
        write_private_text(
            marker,
            "codex-usage persistent browser profile\n",
            label="profile marker",
        )
    return path


def _validate_unique_accounts(accounts: tuple[Account, ...]) -> None:
    seen: set[str] = set()
    ids = {account.id for account in accounts}
    for account in accounts:
        if account.id in seen:
            raise ValueError(f"duplicate account id: {account.id}")
        if account.label in ids and account.label != account.id:
            raise ValueError(
                f"account label conflicts with another account id: {account.label}"
            )
        seen.add(account.id)


def _normalized_config_path(value: str) -> str:
    try:
        return os.path.normcase(str(Path(value).expanduser().resolve(strict=False)))
    except OSError:
        return os.path.normcase(os.path.abspath(os.path.expanduser(value)))


def _validate_unique_account_resources(accounts: tuple[Account, ...]) -> None:
    profile_paths: dict[str, str] = {}
    auth_paths: dict[str, str] = {}
    for account in accounts:
        profile_key = _normalized_config_path(account.profile_dir)
        previous_profile = profile_paths.get(profile_key)
        if previous_profile is not None:
            raise ValueError(
                f"duplicate profile_dir for accounts {previous_profile} and {account.id}"
            )
        profile_paths[profile_key] = account.id

        if account.auth_json_path is None:
            continue
        auth_key = _normalized_config_path(account.auth_json_path)
        previous_auth = auth_paths.get(auth_key)
        if previous_auth is not None:
            raise ValueError(
                f"duplicate auth_json_path for accounts {previous_auth} and {account.id}"
            )
        auth_paths[auth_key] = account.id


def _validate_config(config: AppConfig) -> None:
    if not isinstance(config, AppConfig):
        raise ValueError("config must be an AppConfig")
    if not isinstance(config.accounts, tuple):
        raise ValueError("accounts must be a tuple of Account entries")
    if len(config.accounts) > MAX_CONFIG_ACCOUNTS:
        raise ValueError(
            f"accounts must contain at most {MAX_CONFIG_ACCOUNTS} entries"
        )

    interval = _strict_int(config.interval_seconds, "interval_seconds")
    if interval < 60:
        raise ValueError("interval_seconds must be at least 60")
    if not isinstance(config.analytics_url, str):
        raise ValueError("analytics_url must be an https://chatgpt.com URL")
    _validate_text_field(config.analytics_url, "analytics_url", MAX_CONFIG_URL_CHARS)
    _validate_analytics_url(config.analytics_url)
    _strict_bool(config.headless, "headless")

    for account in config.accounts:
        _validate_account(account)
    _validate_unique_accounts(config.accounts)
    _validate_unique_account_resources(config.accounts)


def _validate_account(account: object) -> None:
    if not isinstance(account, Account):
        raise ValueError("account entry must be Account")
    if not isinstance(account.id, str):
        raise ValueError("account id must be a string")
    _validate_account_id(account.id)
    _validate_text_field(account.label, "account label", MAX_CONFIG_LABEL_CHARS)
    _validate_text_field(account.profile_dir, "profile_dir", MAX_CONFIG_PATH_CHARS)
    _validate_browser(account.browser)
    _validate_backend(account.backend)
    if account.auth_json_path is not None:
        _validate_text_field(
            account.auth_json_path,
            "auth_json_path",
            MAX_CONFIG_PATH_CHARS,
        )


def _validate_text_field(value: object, name: str, max_chars: int) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")
    if len(value) > max_chars:
        raise ValueError(f"{name} must be at most {max_chars} characters")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL bytes")


def _validate_analytics_url(url: str) -> None:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("analytics_url must be an https://chatgpt.com URL") from exc
    if (
        parts.scheme != "https"
        or parts.hostname != "chatgpt.com"
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("analytics_url must be an https://chatgpt.com URL")
    if parts.path.rstrip("/") != "/codex/cloud/settings/analytics":
        raise ValueError("analytics_url must point to /codex/cloud/settings/analytics")


def _validate_browser(browser: str) -> None:
    if browser not in SUPPORTED_BROWSERS:
        choices = ", ".join(SUPPORTED_BROWSERS)
        raise ValueError(f"browser must be one of: {choices}")


def _validate_backend(backend: str) -> None:
    if backend not in SUPPORTED_BACKENDS:
        choices = ", ".join(SUPPORTED_BACKENDS)
        raise ValueError(f"backend must be one of: {choices}")


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
                f"backend = {_quote(account.backend)}",
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
