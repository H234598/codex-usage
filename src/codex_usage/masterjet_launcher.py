from __future__ import annotations

import os
import pwd
import re
import stat
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Literal

from .private_io import assert_no_symlink_ancestors

MasterjetTransport = Literal["local", "https"]

_SYSTEMD_RUN = Path("/usr/bin/systemd-run")
_SYSTEMCTL = Path("/usr/bin/systemctl")
_CREDENTIALS_RELATIVE = Path(".config/codex-usage/credentials")
_CREDENTIAL_PROFILE = {
    "https": ("masterjet-control-bearer", "masterjet-control-bearer.cred"),
    "local": (
        "masterjet-local-attestation-key",
        "masterjet-local-attestation-key.cred",
    ),
}
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_ARGUMENTS = 24
_MAX_ARGUMENT_BYTES = 4096
_MAX_COMMAND_BYTES = 16 * 1024
_MAX_ENCRYPTED_CREDENTIAL_BYTES = 64 * 1024


def masterjet_launcher_argv(
    argv: tuple[str, ...],
    *,
    transport: MasterjetTransport,
    home: Path | None = None,
) -> tuple[str, ...]:
    if transport not in _CREDENTIAL_PROFILE:
        raise ValueError("control.endpoint_invalid")
    selected_home = _account_home() if home is None else home
    if not isinstance(selected_home, Path) or not selected_home.is_absolute():
        raise ValueError("control.authentication_required")
    executable = selected_home / ".local" / "bin" / "codex-usage"
    _validate_executable(executable, expected_uid=os.geteuid())
    _validate_command(argv, executable)
    name, filename = _CREDENTIAL_PROFILE[transport]
    directory = selected_home / _CREDENTIALS_RELATIVE
    _validate_private_path(directory, selected_home)
    source = directory / filename
    _validate_encrypted_credential(source)
    _verify_systemd_user_credentials()
    return (
        os.fspath(_SYSTEMD_RUN),
        "--user",
        "--quiet",
        "--pipe",
        "--wait",
        "--collect",
        "--service-type=exec",
        "--property=UMask=0077",
        f"--property=LoadCredentialEncrypted={name}:{source}",
        "--",
        *argv,
    )


def settings_masterjet_launcher_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    home = _account_home()
    executable = os.fspath(home / ".local" / "bin" / "codex-usage")
    if not argv or argv[0] != executable or not _authenticated_settings_command(argv[1:]):
        return argv
    from .config import load_config

    transport = load_config().masterjet.transport
    if transport not in _CREDENTIAL_PROFILE:
        raise ValueError("control.endpoint_invalid")
    return masterjet_launcher_argv(argv, transport=transport, home=home)


def _account_home() -> Path:
    try:
        value = pwd.getpwuid(os.geteuid()).pw_dir
    except (KeyError, OSError):
        raise ValueError("control.authentication_required") from None
    if type(value) is not str or not value or "\0" in value:
        raise ValueError("control.authentication_required")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("control.authentication_required")
    return path


@lru_cache(maxsize=1)
def _verify_systemd_user_credentials() -> None:
    _validate_executable(_SYSTEMD_RUN, expected_uid=None)
    _validate_executable(_SYSTEMCTL, expected_uid=None)
    environment = {
        "HOME": os.fspath(_account_home()),
        "LANG": "C",
        "PATH": "/usr/bin:/bin",
    }
    for name in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
        value = os.environ.get(name)
        if isinstance(value, str) and value and "\0" not in value:
            environment[name] = value
    try:
        version = subprocess.run(
            (os.fspath(_SYSTEMD_RUN), "--version"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            check=False,
            timeout=2,
        )
        first_line = version.stdout[:256].decode("ascii").splitlines()[0].split()
        manager = subprocess.run(
            (os.fspath(_SYSTEMCTL), "--user", "show-environment"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            check=False,
            timeout=2,
        )
    except (IndexError, OSError, subprocess.SubprocessError, UnicodeError):
        raise ValueError("control.transport_unavailable") from None
    if (
        version.returncode != 0
        or first_line[:1] != ["systemd"]
        or len(first_line) < 2
        or not first_line[1].isdigit()
        or int(first_line[1]) < 247
        or manager.returncode != 0
    ):
        raise ValueError("control.transport_unavailable")


def _validate_private_path(path: Path, home: Path) -> None:
    try:
        assert_no_symlink_ancestors(path, label="masterjet credential directory")
        home_item = home.lstat()
        if (
            not stat.S_ISDIR(home_item.st_mode)
            or home_item.st_uid != os.geteuid()
            or stat.S_IMODE(home_item.st_mode) != 0o700
        ):
            raise ValueError
        relative = path.relative_to(home)
        current = home
        for component in relative.parts:
            current /= component
            item = current.lstat()
            if (
                not stat.S_ISDIR(item.st_mode)
                or item.st_uid != os.geteuid()
                or stat.S_IMODE(item.st_mode) != 0o700
            ):
                raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise ValueError("control.authentication_required") from None


def _validate_encrypted_credential(path: Path) -> None:
    try:
        item = path.lstat()
    except OSError:
        raise ValueError("control.authentication_required") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(item.st_mode)
        or item.st_uid != os.geteuid()
        or item.st_nlink != 1
        or stat.S_IMODE(item.st_mode) not in {0o400, 0o600}
        or not 1 <= item.st_size <= _MAX_ENCRYPTED_CREDENTIAL_BYTES
    ):
        raise ValueError("control.authentication_required")


def _validate_executable(path: Path, *, expected_uid: int | None) -> None:
    try:
        item = path.lstat()
    except OSError:
        raise ValueError("control.transport_unavailable") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(item.st_mode)
        or (expected_uid is not None and item.st_uid != expected_uid)
        or item.st_nlink != 1
        or not item.st_mode & stat.S_IXUSR
    ):
        raise ValueError("control.transport_unavailable")


def _validate_command(argv: tuple[str, ...], executable: Path) -> None:
    if (
        type(argv) is not tuple
        or not 1 <= len(argv) <= _MAX_ARGUMENTS
        or any(
            type(value) is not str
            or not value
            or "\0" in value
            or len(value.encode("utf-8")) > _MAX_ARGUMENT_BYTES
            for value in argv
        )
        or sum(len(value.encode("utf-8")) + 1 for value in argv) > _MAX_COMMAND_BYTES
        or argv[0] != os.fspath(executable)
        or not _allowed_cli_arguments(argv[1:])
    ):
        raise ValueError("control.request_invalid")


def _allowed_cli_arguments(arguments: tuple[str, ...]) -> bool:
    step_up = arguments[:1] == ("--step-up-stdin",)
    values = arguments[1:] if step_up else arguments
    if values in {
        ("masterjet", "openai-accounts", "--json"),
        ("masterjet", "connection-test", "--json"),
        ("masterjet", "connection-show", "--json"),
        ("google", "accounts", "--json"),
    }:
        return not step_up or values == ("google", "accounts", "--json")
    if len(values) == 5 and values[:2] == ("account", "auth-sync"):
        return step_up and _token(values[2]) and values[3:] == ("--format", "json")
    if len(values) == 6 and values[:2] == ("google", "oauth-begin"):
        return (
            step_up
            and _token(values[2])
            and values[3] == "--browser"
            and values[4] in {"firefox", "vivaldi", "chromium"}
            and values[5] == "--json"
        )
    if len(values) == 6 and values[:2] == ("google", "add"):
        try:
            source = Path(values[4])
        except (RuntimeError, ValueError):
            return False
        return (
            step_up
            and _token(values[2])
            and values[3] == "--oauth-client-json"
            and source.is_absolute()
            and values[5] == "--json"
        )
    if (
        len(values) == 4
        and values[0] == "google"
        and values[1] in {"inventory-refresh", "provision-plan"}
    ):
        return step_up and _token(values[2]) and values[3] == "--json"
    if len(values) == 8 and values[:2] == ("google", "provision-apply"):
        return (
            step_up
            and _token(values[2])
            and _token(values[3])
            and values[4] == "--plan-digest"
            and _DIGEST.fullmatch(values[5]) is not None
            and values[6:] == ("--confirm", "--json")
        )
    if len(values) == 9 and values[:2] == ("masterjet", "connection-set"):
        return (
            not step_up
            and values[2] == "--transport"
            and values[3] in _CREDENTIAL_PROFILE
            and values[4] == "--endpoint"
            and len(values[5]) <= 2048
            and values[6] == "--timeout-seconds"
            and values[7].isdigit()
            and 1 <= int(values[7]) <= 300
            and values[8] == "--json"
        )
    return False


def _authenticated_settings_command(arguments: tuple[str, ...]) -> bool:
    values = arguments[1:] if arguments[:1] == ("--step-up-stdin",) else arguments
    return (
        values[:2] in {
            ("masterjet", "openai-accounts"),
            ("masterjet", "connection-test"),
            ("account", "auth-sync"),
        }
        or values[:1] == ("google",)
    )


def _token(value: str) -> bool:
    return _TOKEN.fullmatch(value) is not None
