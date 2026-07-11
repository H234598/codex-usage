from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import AppConfig, default_config_path, default_state_dir
from .private_io import read_private_text, write_private_text

SERVICE_NAME = "codex-usage.service"
TIMER_NAME = "codex-usage.timer"
MANAGED_MARKER = "X-Codex-Usage-Managed=true"
MAX_UNIT_BYTES = 100_000


class ServiceError(Exception):
    pass


def service_enable(config: AppConfig, config_path: Path | None = None) -> dict[str, Any]:
    result = service_install(config, config_path)
    _systemctl("enable", TIMER_NAME)
    _systemctl("restart", TIMER_NAME)
    return {**result, **service_status()}


def service_install(config: AppConfig, config_path: Path | None = None) -> dict[str, Any]:
    unit_dir = _unit_directory()
    _validate_existing_managed_units(unit_dir)
    executable = _resolve_codex_usage()
    config_file = (config_path or default_config_path()).expanduser().absolute()
    service_text = _render_service(config, executable, config_file)
    timer_text = _render_timer(config.interval_seconds)
    write_private_text(unit_dir / SERVICE_NAME, service_text, label="systemd service", mode=0o600)
    write_private_text(unit_dir / TIMER_NAME, timer_text, label="systemd timer", mode=0o600)
    _systemctl("daemon-reload")
    return {"installed": True, "service": SERVICE_NAME, "timer": TIMER_NAME}


def service_disable() -> dict[str, Any]:
    unit_dir = _unit_directory(create=False)
    if _require_complete_managed_units(unit_dir) is not None:
        _systemctl("disable", "--now", TIMER_NAME, check=False)
    return service_status()


def service_uninstall() -> dict[str, Any]:
    unit_dir = _unit_directory()
    paths = _require_complete_managed_units(unit_dir)
    if paths is None:
        return {"installed": False, "enabled": False, "active": False}
    service_disable()
    for path in paths:
        _validate_managed_unit(path)
        path.unlink()
    _systemctl("daemon-reload")
    return {"installed": False, "enabled": False, "active": False}


def service_status() -> dict[str, Any]:
    unit_dir = _unit_directory(create=False)
    service_path = unit_dir / SERVICE_NAME
    timer_path = unit_dir / TIMER_NAME
    installed = _is_managed_unit(service_path) and _is_managed_unit(timer_path)
    enabled = _systemctl_state("is-enabled", TIMER_NAME) == "enabled"
    active = _systemctl_state("is-active", TIMER_NAME) == "active"
    service_active = _systemctl_state("is-active", SERVICE_NAME) in {"active", "activating"}
    details = _systemctl_show(
        SERVICE_NAME,
        (
            "Result",
            "ExecMainStatus",
            "ExecMainCode",
            "ExecMainStartTimestamp",
            "ExecMainExitTimestamp",
        ),
    ) if installed else {}
    return {
        "installed": installed,
        "enabled": enabled,
        "active": active,
        "service_active": service_active,
        "service_result": details.get("Result", "unknown"),
        "service_exit_status": details.get("ExecMainStatus", "unknown"),
        "service_exit_code": details.get("ExecMainCode", "unknown"),
        "service_last_start": details.get("ExecMainStartTimestamp", ""),
        "service_last_exit": details.get("ExecMainExitTimestamp", ""),
        "service": SERVICE_NAME,
        "timer": TIMER_NAME,
    }


def managed_service_config_path() -> Path | None:
    service_path = _unit_directory(create=False) / SERVICE_NAME
    if not _is_managed_unit(service_path):
        return None
    try:
        text, _ = read_private_text(
            service_path,
            regular_label="systemd service",
            read_label="systemd service",
            max_bytes=MAX_UNIT_BYTES,
            too_large_label="systemd service",
            invalid_utf8_label="systemd service",
        )
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    for line in text.splitlines():
        if not line.startswith("ExecStart="):
            continue
        try:
            argv = shlex.split(line[len("ExecStart="):])
            config_index = argv.index("--config")
            return Path(argv[config_index + 1]).expanduser().absolute()
        except (ValueError, IndexError):
            return None
    return None


def _render_service(config: AppConfig, executable: Path, config_path: Path) -> str:
    state = default_state_dir().expanduser().absolute()
    config_root = config_path.parent
    writable = [config_root, state]
    cache = Path.home() / ".cache" / "ms-playwright"
    writable.append(cache)
    for account in config.accounts:
        profile = Path(account.profile_dir).expanduser().absolute()
        _validate_home_path(profile)
        writable.append(profile)
        if account.auth_json_path:
            parent = Path(account.auth_json_path).expanduser().absolute().parent
            _validate_home_path(parent)
            writable.append(parent)
    unique = sorted({str(path) for path in writable})
    read_write = "\n".join(f"ReadWritePaths={_unit_quote(path)}" for path in unique)
    exec_start = " ".join(
        _unit_quote(value)
        for value in (
            str(executable),
            "--config",
            str(config_path),
            "watchdog",
            "--format",
            "json",
        )
    )
    return f"""[Unit]
Description=Watch ChatGPT Codex usage analytics
Documentation=https://github.com/H234598/codex-usage
{MANAGED_MARKER}

[Service]
Type=simple
ExecStart={exec_start}
Environment=PYTHONUNBUFFERED=1
TimeoutStartSec=180
RuntimeMaxSec=180
TimeoutStopSec=15
KillMode=mixed
MemoryMax=1G
TasksMax=256
OOMPolicy=kill
Restart=no
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectClock=true
ProtectHostname=true
ProtectSystem=strict
ProtectHome=read-only
{read_write}
RestrictSUIDSGID=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
LockPersonality=true
SystemCallArchitectures=native
"""


def _render_timer(interval_seconds: int) -> str:
    return f"""[Unit]
Description=Run ChatGPT Codex usage poll periodically
Documentation=https://github.com/H234598/codex-usage
{MANAGED_MARKER}

[Timer]
OnBootSec=1min
OnUnitActiveSec={interval_seconds}s
AccuracySec=30s
Persistent=true
Unit={SERVICE_NAME}

[Install]
WantedBy=timers.target
"""


def _unit_directory(*, create: bool = True) -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    path = root / "systemd" / "user"
    if path.is_symlink():
        raise ServiceError("systemd user unit directory must not be a symlink")
    if create:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ServiceError("systemd user unit directory must be a real directory")
    if not path.exists():
        return path
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise ServiceError("could not secure systemd user unit directory") from exc
    return path


def _resolve_codex_usage() -> Path:
    executable = shutil.which("codex-usage")
    if not executable:
        raise ServiceError("codex-usage executable was not found")
    path = Path(executable).absolute()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ServiceError("codex-usage executable is not executable")
    return path


def _validate_home_path(path: Path) -> None:
    home = Path.home().resolve()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ServiceError("auth_json_path parent is unavailable") from exc
    if resolved != home and home not in resolved.parents:
        raise ServiceError("auth_json_path parent must stay inside the home directory")
    if (
        resolved != Path(os.path.abspath(path))
        or path.is_symlink()
        or not path.is_dir()
    ):
        raise ServiceError("auth_json_path parent must be a real directory")


def _unit_quote(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ServiceError("systemd unit value contains invalid characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = shutil.which("systemctl")
    if not command:
        raise ServiceError("systemctl was not found")
    try:
        completed = subprocess.run(
            [command, "--user", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError("systemctl command failed") from exc
    if check and completed.returncode != 0:
        raise ServiceError(f"systemctl {' '.join(args[:1])} failed")
    return completed


def _systemctl_state(command: str, unit: str) -> str:
    try:
        completed = _systemctl(command, unit, check=False)
    except ServiceError:
        return "unknown"
    return completed.stdout.strip().lower()


def _systemctl_show(unit: str, properties: tuple[str, ...]) -> dict[str, str]:
    args = ["show", unit]
    for property_name in properties:
        args.extend(["-p", property_name])
    try:
        completed = _systemctl(*args, check=False)
    except ServiceError:
        return {}
    result: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in properties:
            result[key] = value[:500]
    return result


def _is_managed_unit(path: Path) -> bool:
    try:
        _validate_managed_unit(path)
        return True
    except (OSError, ValueError, ServiceError):
        return False


def _validate_existing_managed_units(unit_dir: Path) -> None:
    for name in (SERVICE_NAME, TIMER_NAME):
        path = unit_dir / name
        if path.exists() or path.is_symlink():
            _validate_managed_unit(path)


def _require_complete_managed_units(unit_dir: Path) -> tuple[Path, Path] | None:
    paths = (unit_dir / SERVICE_NAME, unit_dir / TIMER_NAME)
    present = [path.exists() or path.is_symlink() for path in paths]
    if not any(present):
        return None
    _validate_existing_managed_units(unit_dir)
    if not all(present):
        raise ServiceError("managed service and timer must both exist")
    return paths


def _validate_managed_unit(path: Path) -> None:
    text, _ = read_private_text(
        path,
        regular_label="systemd unit",
        read_label="systemd unit",
        max_bytes=MAX_UNIT_BYTES,
    )
    if MANAGED_MARKER not in text.splitlines():
        raise ServiceError("refusing to modify an unmanaged systemd unit")


def render_service_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
