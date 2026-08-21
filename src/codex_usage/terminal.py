from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from .models import Account
from .profile_layout import layout_for_account


class TerminalError(Exception):
    """Raised when an account terminal cannot be started safely."""


TERMINAL_CANDIDATES = (
    "ghostty",
    "gnome-terminal",
    "konsole",
    "xfce4-terminal",
    "mate-terminal",
    "x-terminal-emulator",
    "kitty",
    "alacritty",
    "wezterm",
    "foot",
    "xterm",
)


def start_account_terminal(
    account: Account,
    *,
    terminal: str | None = None,
    codex_command: str | None = None,
) -> dict[str, Any]:
    """Start Codex in a new terminal using the account's canonical CODEX_HOME."""
    if not isinstance(account, Account):
        raise TerminalError("account is invalid")

    layout = layout_for_account(account)
    if not layout.profile_dir.is_dir():
        raise TerminalError(f"account profile directory does not exist: {layout.profile_dir}")
    _validate_auth_json(layout.auth_json)
    codex = _resolve_executable(codex_command, "codex", label="codex command")
    terminal_path, terminal_kind = _resolve_terminal(terminal)
    argv = _terminal_argv(
        terminal_path,
        terminal_kind,
        profile_dir=layout.profile_dir,
        codex=codex,
    )
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(layout.codex_home)
    try:
        subprocess.Popen(
            argv,
            cwd=str(layout.profile_dir),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise TerminalError("could not start terminal for account") from exc
    return {
        "ok": True,
        "account": account.id,
        "label": account.label,
        "profile_dir": str(layout.profile_dir),
        "codex_home": str(layout.codex_home),
        "terminal": terminal_path,
    }


def _validate_auth_json(path: Path) -> None:
    try:
        item = path.lstat()
    except FileNotFoundError as exc:
        raise TerminalError(f"canonical auth.json is missing: {path}") from exc
    except OSError as exc:
        raise TerminalError(f"canonical auth.json cannot be inspected: {path}") from exc
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
        raise TerminalError(f"canonical auth.json must be a regular file: {path}")


def _resolve_executable(explicit: str | None, fallback: str, *, label: str) -> str:
    candidate = fallback if explicit is None else explicit
    if not isinstance(candidate, str) or not candidate or candidate != candidate.strip():
        raise TerminalError(f"{label} is invalid")
    resolved = shutil.which(candidate)
    if not resolved:
        raise TerminalError(f"{label} was not found: {candidate}")
    return resolved


def _resolve_terminal(explicit: str | None = None) -> tuple[str, str]:
    candidates = (
        (explicit,)
        if explicit is not None
        else (*TERMINAL_CANDIDATES, os.environ.get("TERMINAL"))
    )
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate or candidate != candidate.strip():
            continue
        resolved = shutil.which(candidate)
        if not resolved:
            continue
        kind = Path(resolved).resolve().name.lower()
        if kind.endswith(".wrapper"):
            kind = Path(resolved).resolve().stem.lower()
        return resolved, kind
    raise TerminalError("no supported terminal emulator was found")


def _terminal_argv(
    terminal: str,
    kind: str,
    *,
    profile_dir: Path,
    codex: str,
) -> list[str]:
    profile = str(profile_dir)
    if kind in {"gnome-terminal", "mate-terminal"}:
        return [terminal, "--working-directory", profile, "--", codex]
    if kind == "konsole":
        return [terminal, "--workdir", profile, "-e", codex]
    if kind == "xfce4-terminal":
        return [terminal, "--working-directory", profile, "--command", codex]
    if kind == "ghostty":
        return [terminal, "--working-directory", profile, "-e", codex]
    if kind == "kitty":
        return [terminal, "--directory", profile, codex]
    if kind == "alacritty":
        return [terminal, "--working-directory", profile, "-e", codex]
    if kind == "wezterm":
        return [terminal, "start", "--cwd", profile, "--", codex]
    if kind == "foot":
        return [terminal, "--working-directory", profile, codex]
    return [terminal, "-e", codex]
