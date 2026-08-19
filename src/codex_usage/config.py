from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tomllib
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .models import Account
from .private_io import (
    assert_no_symlink_ancestors,
    ensure_private_directory,
    private_path_lock,
    read_private_text,
    write_private_text,
)

APP_NAME = "codex-usage"
SUPPORTED_BROWSERS = ("firefox", "chromium")
SUPPORTED_REACTIVATION_BROWSERS = ("auto", "vivaldi", "chromium", "firefox")
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
    root = _xdg_root("XDG_CONFIG_HOME", Path.home() / ".config")
    return root / APP_NAME / "config.toml"


def default_state_dir() -> Path:
    root = _xdg_root("XDG_DATA_HOME", Path.home() / ".local" / "share")
    return root / APP_NAME


def _xdg_root(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate
    return fallback


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
    analytics_url = data.get(
        "analytics_url", "https://chatgpt.com/codex/cloud/settings/analytics"
    )
    if not isinstance(analytics_url, str):
        raise ValueError("analytics_url must be an https://chatgpt.com URL")
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
    text, file_stat = read_private_text(
        config_path,
        regular_label="config path",
        read_label="config file",
        max_bytes=MAX_CONFIG_BYTES,
        too_large_label="config file",
        invalid_utf8_label="config file",
    )
    if file_stat.st_nlink != 1:
        raise ValueError("config file must not be hard-linked")
    if file_stat.st_mode & 0o077:
        raise ValueError("config file permissions must be 0600")
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
    try:
        resolved = config_dir.expanduser().resolve(strict=False)
        home = Path.home().resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("config directory cannot be resolved") from exc
    if resolved in {
        Path("/").resolve(),
        home,
        home / ".config",
        home / ".local",
        home / ".local" / "share",
    }:
        raise ValueError(f"config directory must not be a protected directory: {config_dir}")
    assert_no_symlink_ancestors(config_dir, label="config directory")
    if config_dir.is_symlink():
        raise ValueError(f"config directory must not be a symlink: {config_dir}")
    if config_dir.exists() and config_dir.is_dir():
        mode = stat.S_IMODE(config_dir.stat().st_mode)
        if mode & 0o077:
            raise ValueError(
                f"private config directory must not be group/world accessible: {config_dir}"
            )
    try:
        ensure_private_directory(config_dir, label="config directory")
    except OSError as exc:
        raise ValueError("could not secure config directory") from exc


def add_or_update_account(
    account_id: str,
    label: str | None = None,
    profile_dir: str | None = None,
    browser: str | None = None,
    auth_json_path: str | None = None,
    backend: str | None = None,
    reactivation_browser: str | None = None,
    clear_auth_json: bool = False,
    test_home: bool = False,
    path: Path | None = None,
    before_state_cleanup: Callable[[AppConfig], None] | None = None,
    rollback_callback: Callable[[AppConfig], None] | None = None,
    _all_accounts_lock_held: bool = False,
) -> tuple[AppConfig, Account]:
    _validate_account_id(account_id)
    if browser is not None:
        _validate_browser(browser)
    if backend is not None:
        _validate_backend(backend)
    if reactivation_browser is not None:
        _validate_reactivation_browser(reactivation_browser)
    if not isinstance(test_home, bool):
        raise ValueError("test_home must be boolean")
    if clear_auth_json and auth_json_path is not None:
        raise ValueError("clear_auth_json cannot be combined with auth_json_path")
    if clear_auth_json and test_home:
        raise ValueError("clear_auth_json cannot be combined with test_home")
    if not isinstance(_all_accounts_lock_held, bool):
        raise ValueError("_all_accounts_lock_held must be boolean")
    config_path = path or default_config_path()
    _prepare_config_directory(config_path.parent)
    from .account_lock import account_lock

    account_guard = (
        nullcontext()
        if _all_accounts_lock_held
        else account_lock("__all_accounts__")
    )
    with account_guard, private_path_lock(
        config_path, label="config lock"
    ):
        config = load_config(config_path)
        existing = next((item for item in config.accounts if item.id == account_id), None)
        selected_profile_dir = profile_dir or (
            str(_test_profile_root(account_id))
            if test_home and existing is None
            else (existing.profile_dir if existing else str(_default_profile_root(account_id)))
        )
        selected_auth_json_path = (
            None
            if clear_auth_json
            else auth_json_path
            if auth_json_path is not None
            else (existing.auth_json_path if existing else None)
        )
        source_auth_json = (
            Path(selected_auth_json_path).expanduser()
            if test_home and selected_auth_json_path
            else None
        )
        canonical_auth_json = (
            str(Path(selected_profile_dir).expanduser() / "codex-home" / "auth.json")
            if test_home
            else selected_auth_json_path
        )
        account = Account(
            id=account_id,
            label=label or (existing.label if existing else account_id),
            profile_dir=_absolute_account_path(selected_profile_dir, "profile_dir"),
            browser=browser or (existing.browser if existing else "firefox"),
            auth_json_path=(
                _absolute_account_path(canonical_auth_json, "auth_json_path")
                if canonical_auth_json not in (None, "")
                else canonical_auth_json
            ),
            backend=backend or (existing.backend if existing else "direct"),
            reactivation_browser=reactivation_browser
            or (existing.reactivation_browser if existing else "auto"),
        )

        accounts = [item for item in config.accounts if item.id != account_id]
        accounts.append(account)
        updated = AppConfig(
            accounts=tuple(accounts),
            interval_seconds=config.interval_seconds,
            analytics_url=config.analytics_url,
            headless=config.headless,
        )
        _validate_account(account)
        _validate_config(updated)
        profile_path, profile_created, profile_created_directories = _prepare_profile_dir(
            account.profile_dir
        )
        moved_auth_json: tuple[Path, Path] | None = None
        state_changed = existing is None or existing != account
        try:
            if source_auth_json is not None:
                _integrate_test_home_auth(source_auth_json, Path(account.auth_json_path))
                moved_auth_json = (source_auth_json, Path(account.auth_json_path))
            if test_home:
                _prepare_test_codex_home(Path(account.profile_dir) / "codex-home")
            _save_config_unlocked(updated, config_path)
        except Exception as original_error:
            if moved_auth_json is not None:
                source, target = moved_auth_json
                try:
                    if target.is_file() and not source.exists():
                        source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        shutil.move(str(target), str(source))
                except OSError:
                    pass
            if profile_created:
                try:
                    _cleanup_created_profile_directories(
                        profile_path,
                        profile_created_directories,
                    )
                except Exception as cleanup_error:
                    raise ExceptionGroup(
                        "account update rollback failed: profile cleanup",
                        [original_error, cleanup_error],
                    ) from None
            raise
        callback_started = False
        try:
            if before_state_cleanup is not None:
                callback_started = True
                before_state_cleanup(updated)
            if state_changed:
                # A re-added or changed account must not inherit values from another
                # configuration generation under the same local account ID.
                from .state import remove_account_state

                remove_account_state(account.id)
        except Exception as original_error:
            rollback_errors: list[tuple[str, Exception]] = []
            try:
                _save_config_unlocked(config, config_path)
            except Exception as exc:
                rollback_errors.append(("config rollback", exc))
            if callback_started and rollback_callback is not None:
                try:
                    rollback_callback(config)
                except Exception as exc:
                    rollback_errors.append(("service rollback", exc))
            if profile_created:
                try:
                    _cleanup_created_profile_directories(
                        profile_path,
                        profile_created_directories,
                    )
                except Exception as exc:
                    rollback_errors.append(("profile rollback", exc))
            if len(rollback_errors) == 1:
                label, rollback_error = rollback_errors[0]
                raise ValueError(
                    f"could not roll back account configuration ({label})"
                ) from rollback_error
            if rollback_errors:
                labels = ", ".join(label for label, _ in rollback_errors)
                raise ExceptionGroup(
                    f"account update rollback failed: primary operation, {labels}",
                    [original_error, *(error for _, error in rollback_errors)],
                ) from None
            raise original_error
    return updated, account


def remove_account(
    account_ref: str,
    path: Path | None = None,
    *,
    expected: Account | None = None,
) -> tuple[AppConfig, Account]:
    config_path = path or default_config_path()
    _prepare_config_directory(config_path.parent)
    with private_path_lock(config_path, label="config lock"):
        config = load_config(config_path)
        account = resolve_account(config, account_ref)
        if expected is not None and account != expected:
            raise ValueError(f"account {account.id} changed before removal")
        updated = AppConfig(
            accounts=tuple(item for item in config.accounts if item.id != account.id),
            interval_seconds=config.interval_seconds,
            analytics_url=config.analytics_url,
            headless=config.headless,
        )
        _validate_config(updated)
        _save_config_unlocked(updated, config_path)
    return updated, account


def restore_account(
    account: Account,
    path: Path | None = None,
    *,
    index: int | None = None,
    expected: Account | None = None,
) -> AppConfig:
    config_path = path or default_config_path()
    _prepare_config_directory(config_path.parent)
    with private_path_lock(config_path, label="config lock"):
        config = load_config(config_path)
        existing_index = next(
            (position for position, item in enumerate(config.accounts) if item.id == account.id),
            None,
        )
        existing = (
            config.accounts[existing_index]
            if existing_index is not None
            else None
        )
        if existing is not None:
            if existing == account:
                return config
            if expected is None or existing != expected:
                raise ValueError(
                    f"account {account.id} was recreated with different settings"
                )
            accounts = list(config.accounts)
            accounts[existing_index] = account
        else:
            accounts = list(config.accounts)
            insert_at = len(accounts) if index is None else max(0, min(index, len(accounts)))
            accounts.insert(insert_at, account)
        restored = AppConfig(
            accounts=tuple(accounts),
            interval_seconds=config.interval_seconds,
            analytics_url=config.analytics_url,
            headless=config.headless,
        )
        _validate_config(restored)
        _save_config_unlocked(restored, config_path)
    return restored


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
    raw_account_id = item.get("id", "")
    if not isinstance(raw_account_id, str):
        raise ValueError("account id must be a string")
    account_id = raw_account_id.strip()
    _validate_account_id(account_id)
    raw_label = item.get("label")
    if raw_label in (None, ""):
        label = account_id
    elif not isinstance(raw_label, str):
        raise ValueError("account label must be a string")
    else:
        label = raw_label
    raw_profile_dir = item.get("profile_dir")
    if raw_profile_dir in (None, ""):
        profile_dir = str(_default_profile_root(account_id))
    elif not isinstance(raw_profile_dir, str):
        raise ValueError("profile_dir must be a string")
    else:
        profile_dir = raw_profile_dir
    profile_dir = _absolute_account_path(profile_dir, "profile_dir")
    raw_browser = item.get("browser")
    if raw_browser in (None, ""):
        browser = "firefox"
    elif not isinstance(raw_browser, str):
        raise ValueError("browser must be a string")
    else:
        browser = raw_browser
    _validate_browser(browser)
    auth_json_path = item.get("auth_json_path")
    if auth_json_path == "":
        auth_json_path = None
    elif auth_json_path is not None and not isinstance(auth_json_path, str):
        raise ValueError("auth_json_path must be a string")
    if auth_json_path is not None:
        auth_json_path = _absolute_account_path(auth_json_path, "auth_json_path")
    raw_backend = item.get("backend")
    if raw_backend in (None, ""):
        backend = "direct"
    elif not isinstance(raw_backend, str):
        raise ValueError("backend must be a string")
    else:
        backend = raw_backend
    _validate_backend(backend)
    raw_reactivation_browser = item.get("reactivation_browser")
    if raw_reactivation_browser in (None, ""):
        reactivation_browser = "auto"
    elif not isinstance(raw_reactivation_browser, str):
        raise ValueError("reactivation_browser must be a string")
    else:
        reactivation_browser = raw_reactivation_browser
    _validate_reactivation_browser(reactivation_browser)
    return Account(
        id=account_id,
        label=label,
        profile_dir=profile_dir,
        browser=browser,
        auth_json_path=auth_json_path,
        backend=backend,
        reactivation_browser=reactivation_browser,
    )


def _validate_account_id(account_id: str) -> None:
    if account_id == "__all_accounts__":
        raise ValueError("account id is reserved for internal coordination")
    if account_id in {".", ".."} or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,64}", account_id
    ):
        raise ValueError("account id must be 1-64 chars: letters, digits, underscore, dot, dash")


def _safe_profile_name(account_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", account_id)


def _default_profile_root(account_id: str) -> Path:
    return default_state_dir() / "profiles" / _safe_profile_name(account_id)


def _test_profile_root(account_id: str) -> Path:
    return Path.home() / ".codex-test" / _safe_profile_name(account_id)


def _integrate_test_home_auth(source: Path, target: Path) -> None:
    source = source.expanduser().absolute()
    target = target.expanduser().absolute()
    if source == target:
        return
    assert_no_symlink_ancestors(source, label="test auth source")
    if source.is_symlink() or not source.is_file():
        raise ValueError("test auth source must be a regular file")
    if target.exists() or target.is_symlink():
        raise ValueError(f"test auth target already exists: {target}")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    shutil.move(str(source), str(target))
    target.chmod(0o600)


def _prepare_test_codex_home(codex_home: Path) -> None:
    ensure_private_directory(codex_home, label="test CODEX_HOME")
    config_path = codex_home / "config.toml"
    if config_path.is_symlink() or (config_path.exists() and not config_path.is_file()):
        raise ValueError("test CODEX_HOME config must be a regular file")
    if not config_path.exists():
        write_private_text(
            config_path,
            'cli_auth_credentials_store = "file"\n',
            label="test CODEX_HOME config",
        )
    else:
        existing, _ = read_private_text(
            config_path,
            regular_label="test CODEX_HOME config",
            read_label="test CODEX_HOME config",
            max_bytes=64 * 1024,
        )
        if 'cli_auth_credentials_store = "file"' not in existing:
            write_private_text(
                config_path,
                existing.rstrip() + '\ncli_auth_credentials_store = "file"\n',
                label="test CODEX_HOME config",
                replace_existing=True,
            )
    try:
        subprocess.run(
            ["codex", "--help"],
            env={**os.environ, "CODEX_HOME": str(codex_home)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("could not start Codex in test CODEX_HOME") from exc


def _absolute_account_path(value: str, name: str) -> str:
    raw_value = value
    parsed = urlsplit(value)
    if parsed.scheme:
        if parsed.scheme.lower() != "file":
            raise ValueError(f"{name} must be a local file URI")
        if parsed.netloc not in ("", "localhost"):
            raise ValueError(f"{name} must be a local file URI")
        if not parsed.path or not parsed.path.startswith("/"):
            raise ValueError(f"{name} must be a local file URI")
        if parsed.query or parsed.fragment:
            raise ValueError(f"{name} must be a local file URI")
        raw_value = unquote(parsed.path)
        if "\x00" in raw_value:
            raise ValueError(f"{name} must be a local file URI")
    if "\x00" in raw_value:
        raise ValueError(f"{name} must be an absolute path")
    path = Path(raw_value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return str(path)


def _remove_created_profile_dir(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"new profile directory is not a real directory: {path}")
    marker = path / ".codex-usage-profile"
    for entry in path.iterdir():
        if entry != marker:
            raise ValueError(f"new profile directory contains unexpected data: {path}")
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        raise ValueError(f"new profile marker is not a regular file: {marker}")
    if marker.exists():
        marker.unlink()
    path.rmdir()


def _cleanup_created_profile_directories(
    profile_path: Path,
    created_directories: list[tuple[Path, int, int]],
) -> None:
    final_created = next(
        (item for item in created_directories if item[0] == profile_path),
        None,
    )
    if final_created is not None and (profile_path.exists() or profile_path.is_symlink()):
        _assert_created_directory_identity(profile_path, final_created)
        _remove_created_profile_dir(profile_path)
    for directory, device, inode in reversed(created_directories):
        if directory == profile_path:
            continue
        if not directory.exists():
            if directory.is_symlink():
                raise ValueError(f"created profile directory became a symlink: {directory}")
            continue
        _assert_created_directory_identity(directory, (directory, device, inode))
        directory.rmdir()


def _assert_created_directory_identity(
    path: Path,
    expected: tuple[Path, int, int],
) -> None:
    item = path.lstat()
    if (
        item.st_dev != expected[1]
        or item.st_ino != expected[2]
        or stat.S_ISLNK(item.st_mode)
        or not stat.S_ISDIR(item.st_mode)
    ):
        raise ValueError(f"created profile directory changed: {path}")


def _prepare_profile_dir(
    profile_dir: str,
) -> tuple[Path, bool, list[tuple[Path, int, int]]]:
    path = _validate_profile_path(profile_dir)
    assert_no_symlink_ancestors(path, label="profile dir")
    if path.is_symlink():
        raise ValueError(f"profile dir must not be a symlink: {path}")
    created = False
    created_directories: list[tuple[Path, int, int]] = []
    try:
        if not path.exists():
            ensure_private_directory(
                path,
                label="profile dir",
                created_paths=created_directories,
            )
            created = True
        if path.is_symlink():
            raise ValueError(f"profile dir must not be a symlink: {path}")
        if not path.is_dir():
            raise ValueError(f"profile path is not a directory: {path}")
        try:
            path.chmod(0o700)
        except OSError as exc:
            raise ValueError("could not secure profile directory") from exc
        marker = path / ".codex-usage-profile"
        if marker.is_symlink() or (marker.exists() and not marker.is_file()):
            raise ValueError(f"profile marker must be a regular file: {marker}")
        if not marker.exists():
            write_private_text(
                marker,
                "codex-usage persistent browser profile\n",
                label="profile marker",
            )
        return path, created, created_directories
    except Exception as primary_error:
        if not created_directories:
            raise
        try:
            _cleanup_created_profile_directories(path, created_directories)
        except Exception as cleanup_error:
            raise ExceptionGroup(
                "profile directory setup rollback failed",
                [primary_error, cleanup_error],
            ) from None
        raise


def _validate_profile_path(profile_dir: str) -> Path:
    path = Path(profile_dir).expanduser()
    try:
        resolved = path.resolve(strict=False)
        home = Path.home().resolve()
        state = default_state_dir().expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("profile dir cannot be resolved") from exc
    protected = {
        Path("/").resolve(),
        home,
        home / ".config",
        home / ".local",
        home / ".local" / "share",
        state,
        state / "profiles",
    }
    if resolved in protected:
        raise ValueError(f"profile dir must not be a protected directory: {path}")
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
    profile_path = _validate_profile_path(account.profile_dir)
    if not profile_path.is_absolute():
        raise ValueError("profile_dir must be an absolute path")
    _validate_browser(account.browser)
    _validate_backend(account.backend)
    _validate_reactivation_browser(account.reactivation_browser)
    if account.auth_json_path is not None:
        _validate_text_field(
            account.auth_json_path,
            "auth_json_path",
            MAX_CONFIG_PATH_CHARS,
        )
        if not Path(account.auth_json_path).expanduser().is_absolute():
            raise ValueError("auth_json_path must be an absolute path")


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


def _validate_reactivation_browser(browser: str) -> None:
    if browser not in SUPPORTED_REACTIVATION_BROWSERS:
        choices = ", ".join(SUPPORTED_REACTIVATION_BROWSERS)
        raise ValueError(f"reactivation browser must be one of: {choices}")


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
                f"reactivation_browser = {_quote(account.reactivation_browser)}",
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
