from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .reactivate import OAUTH_PROFILE_MARKER

ALLOWED_BROWSER_KINDS = ("vivaldi", "chromium", "firefox")
PASSTHROUGH_ENV_NAMES = {
    "DBUS_SESSION_BUS_ADDRESS",
    "DESKTOP_SESSION",
    "DISPLAY",
    "HOME",
    "LANG",
    "PATH",
    "SHELL",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_CURRENT_DESKTOP",
    "XDG_DATA_DIRS",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("error: expected exactly one login URL", file=sys.stderr)
        return 2
    try:
        url = _validate_login_url(args[0])
        executable, browser_kind, profile = _browser_configuration()
        command = _browser_command(executable, browser_kind, profile, url)
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=_browser_environment(),
        )
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _validate_login_url(value: str) -> str:
    if len(value) > 8192:
        raise ValueError("login URL is too long")
    parts = urlsplit(value)
    hostname = (parts.hostname or "").lower()
    allowed_host = hostname in {"openai.com", "chatgpt.com"} or hostname.endswith(
        (".openai.com", ".chatgpt.com")
    )
    if (
        parts.scheme != "https"
        or not allowed_host
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("refusing non-OpenAI login URL")
    return value


def _browser_configuration() -> tuple[str, str, Path]:
    executable = os.environ.get("CODEX_USAGE_BROWSER_EXECUTABLE", "")
    browser_kind = os.environ.get("CODEX_USAGE_BROWSER_KIND", "")
    profile_value = os.environ.get("CODEX_USAGE_BROWSER_PROFILE", "")
    if browser_kind not in ALLOWED_BROWSER_KINDS:
        raise ValueError("invalid isolated browser kind")
    executable_path = Path(executable)
    if not executable_path.is_absolute() or not executable_path.is_file():
        raise ValueError("invalid isolated browser executable")
    if not os.access(executable_path, os.X_OK):
        raise ValueError("isolated browser is not executable")
    profile = Path(profile_value)
    marker = profile / OAUTH_PROFILE_MARKER
    if (
        not profile.is_absolute()
        or profile.is_symlink()
        or not profile.is_dir()
        or marker.is_symlink()
        or not marker.is_file()
    ):
        raise ValueError("invalid isolated browser profile")
    return str(executable_path), browser_kind, profile


def _browser_command(
    executable: str,
    browser_kind: str,
    profile: Path,
    url: str,
) -> list[str]:
    if browser_kind == "firefox":
        return [executable, "-no-remote", "-profile", str(profile), "-new-window", url]
    return [
        executable,
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        url,
    ]


def _browser_environment() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in PASSTHROUGH_ENV_NAMES or key.startswith("LC_")
    }
    env.pop("CODEX_HOME", None)
    env.pop("BROWSER", None)
    return env


if __name__ == "__main__":
    raise SystemExit(main())
