from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .account_lock import AccountLockError, account_lock
from .direct import (
    DirectAuthError,
    _extract_auth_details,
    auth_identity_from_payload,
    read_auth_json_file,
)
from .extractor import LOCAL_TZ
from .json_utils import loads_strict
from .models import Account
from .private_io import write_private_text

REACTIVATION_BROWSERS = ("auto", "vivaldi", "chromium", "firefox")
REACTIVATION_TIMEOUT_SECONDS = 600
OAUTH_PROFILE_MARKER = ".codex-usage-oauth-profile"
BROWSER_COMMANDS = {
    "vivaldi": ("vivaldi-stable", "vivaldi"),
    "chromium": ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"),
    "firefox": ("firefox",),
}


class ReactivationError(Exception):
    pass


def reactivate_account(
    account: Account,
    *,
    browser: str = "auto",
    timeout_seconds: int = REACTIVATION_TIMEOUT_SECONDS,
    codex_command: str | None = None,
    browser_helper: str | None = None,
) -> dict[str, Any]:
    try:
        with account_lock(account.id):
            return _reactivate_account_unlocked(
                account,
                browser=browser,
                timeout_seconds=timeout_seconds,
                codex_command=codex_command,
                browser_helper=browser_helper,
            )
    except AccountLockError as exc:
        raise ReactivationError(str(exc)) from exc


def _reactivate_account_unlocked(
    account: Account,
    *,
    browser: str,
    timeout_seconds: int,
    codex_command: str | None,
    browser_helper: str | None,
) -> dict[str, Any]:
    auth_path = _validate_auth_target(account)
    browser_kind, browser_executable = _select_browser(browser)
    profile_dir = _prepare_oauth_profile(account, browser_kind)
    codex = _resolve_executable(codex_command, "codex", label="codex command")
    helper = _resolve_executable(
        browser_helper,
        "codex-usage-browser",
        label="browser helper",
    )

    env = os.environ.copy()
    for name in ("CODEX_ACCESS_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY"):
        env.pop(name, None)
    env.update(
        {
            "CODEX_HOME": str(auth_path.parent),
            "BROWSER": helper,
            "CODEX_USAGE_BROWSER_EXECUTABLE": browser_executable,
            "CODEX_USAGE_BROWSER_KIND": browser_kind,
            "CODEX_USAGE_BROWSER_PROFILE": str(profile_dir),
        }
    )

    auth_backup = _capture_auth_backup(auth_path)
    expected_identity = _identity_from_auth_backup(auth_path, auth_backup)

    try:
        try:
            completed = subprocess.run(
                [codex, "login"],
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise ReactivationError(
                "login timed out; close the login browser and try again"
            ) from exc
        except OSError as exc:
            raise ReactivationError("could not start codex login") from exc

        if completed.returncode != 0:
            raise ReactivationError(
                f"codex login failed with exit code {completed.returncode}"
            )

        metadata = _validate_refreshed_auth(auth_path)
        _validate_refreshed_identity(auth_path, expected_identity)
    except Exception as exc:
        try:
            _restore_auth_backup(auth_path, auth_backup)
        except ReactivationError as restore_exc:
            raise restore_exc from exc
        if isinstance(exc, ReactivationError):
            raise
        raise ReactivationError("login failed unexpectedly") from exc

    return {
        "ok": True,
        "account": account.id,
        "label": account.label,
        "browser": browser_kind,
        "auth_updated": True,
        "auth_access_expires_at": metadata["auth_access_expires_at"].isoformat()
        if metadata["auth_access_expires_at"]
        else None,
    }


def _capture_auth_backup(path: Path) -> tuple[str, int] | None:
    if not path.exists():
        return None
    try:
        raw, file_stat = read_auth_json_file(path)
    except DirectAuthError as exc:
        raise ReactivationError("could not preserve previous auth.json") from exc
    if file_stat.st_nlink != 1:
        raise ReactivationError("auth.json must not be hard-linked")
    return raw, stat.S_IMODE(file_stat.st_mode)


def _identity_from_auth_backup(
    path: Path,
    backup: tuple[str, int] | None,
) -> tuple[str | None, str | None]:
    if backup is None:
        return None, None
    try:
        payload = loads_strict(backup[0])
        if not isinstance(payload, dict):
            return None, None
        return auth_identity_from_payload(payload, path=path)
    except (DirectAuthError, ValueError):
        return None, None


def _validate_refreshed_identity(
    path: Path,
    expected: tuple[str | None, str | None],
) -> None:
    expected_user_id, expected_account_id = expected
    if expected_user_id is None and expected_account_id is None:
        return
    try:
        raw, _ = read_auth_json_file(path)
        payload = loads_strict(raw)
        if not isinstance(payload, dict):
            raise ValueError("auth.json is not an object")
        actual_user_id, actual_account_id = auth_identity_from_payload(
            payload,
            path=path,
        )
    except (DirectAuthError, ValueError) as exc:
        raise ReactivationError(
            "login completed without a verifiable account identity"
        ) from exc

    if expected_user_id and actual_user_id and actual_user_id != expected_user_id:
        matches = False
    elif expected_account_id and actual_account_id:
        accepted_account_ids = {expected_account_id}
        if expected_user_id:
            accepted_account_ids.add(expected_user_id)
        matches = actual_account_id in accepted_account_ids
    else:
        matches = bool(expected_user_id and actual_user_id == expected_user_id)
    if not matches:
        raise ReactivationError("login completed for a different account")


def _restore_auth_backup(path: Path, backup: tuple[str, int] | None) -> None:
    try:
        if backup is not None:
            write_private_text(
                path,
                backup[0],
                label="auth.json restore",
                mode=backup[1],
            )
            return
        _assert_no_symlink_ancestors(path.parent, label="auth.json restore parent")
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("auth.json restore target must be a regular file")
        if path.exists() and path.stat().st_nlink != 1:
            raise ValueError("auth.json restore target must not be hard-linked")
        if path.exists():
            path.unlink()
    except (OSError, ValueError) as exc:
        raise ReactivationError("could not restore previous auth.json") from exc


def _validate_auth_target(account: Account) -> Path:
    if not account.auth_json_path:
        raise ReactivationError("account has no auth_json_path")
    path = Path(account.auth_json_path).expanduser()
    if path.name != "auth.json":
        raise ReactivationError("auth_json_path must point to auth.json")
    parent = path.parent
    _assert_no_symlink_ancestors(parent, label="auth_json_path parent")
    if parent.is_symlink() or not parent.is_dir():
        raise ReactivationError("auth_json_path parent must be a real directory")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ReactivationError("auth_json_path must be a regular file")
    return path


def _select_browser(requested: str) -> tuple[str, str]:
    if requested not in REACTIVATION_BROWSERS:
        raise ReactivationError(f"unsupported reactivation browser: {requested}")
    kinds = ("vivaldi", "chromium", "firefox") if requested == "auto" else (requested,)
    for kind in kinds:
        for command in BROWSER_COMMANDS[kind]:
            executable = shutil.which(command)
            if executable:
                return kind, executable
    raise ReactivationError(f"reactivation browser is not installed: {requested}")


def _prepare_oauth_profile(account: Account, browser_kind: str) -> Path:
    root = Path(account.profile_dir).expanduser()
    _prepare_real_private_directory(root, label="account profile directory")
    oauth_root = root / "oauth"
    _prepare_real_private_directory(oauth_root, label="OAuth profile root")
    profile = oauth_root / browser_kind
    _prepare_real_private_directory(profile, label="OAuth browser profile")
    marker = profile / OAUTH_PROFILE_MARKER
    write_private_text(
        marker,
        json.dumps({"account": account.id, "browser": browser_kind}) + "\n",
        label="OAuth profile marker",
    )
    return profile


def _prepare_real_private_directory(path: Path, *, label: str) -> None:
    _assert_no_symlink_ancestors(path, label=label)
    if path.is_symlink():
        raise ReactivationError(f"{label} must not be a symlink")
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise ReactivationError(f"could not create {label}") from exc
    if path.is_symlink() or not path.is_dir():
        raise ReactivationError(f"{label} must be a real directory")
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise ReactivationError(f"could not secure {label}") from exc


def _assert_no_symlink_ancestors(path: Path, *, label: str) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ReactivationError(f"{label} must not contain symlinks")
        if not current.exists():
            break


def _resolve_executable(explicit: str | None, fallback: str, *, label: str) -> str:
    executable = explicit or shutil.which(fallback)
    if not executable:
        raise ReactivationError(f"{label} was not found")
    path = Path(executable).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ReactivationError(f"{label} is not executable")
    return str(path)


def _validate_refreshed_auth(path: Path) -> dict[str, datetime | None]:
    try:
        raw, _ = read_auth_json_file(path)
        payload = loads_strict(raw)
    except (DirectAuthError, ValueError) as exc:
        raise ReactivationError("login completed without a valid auth.json") from exc
    if not isinstance(payload, dict):
        raise ReactivationError("login completed without a valid auth.json")
    try:
        _access_token, metadata = _extract_auth_details(payload, path=path)
    except DirectAuthError as exc:
        raise ReactivationError("login completed without a valid auth.json") from exc
    expiry = metadata.get("auth_access_expires_at")
    if expiry is not None and expiry <= datetime.now(tz=LOCAL_TZ):
        raise ReactivationError("login completed with an expired access token")
    return metadata
