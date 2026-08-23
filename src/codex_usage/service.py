from __future__ import annotations

import json
import os
import selectors
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, cast

from .config import (
    AppConfig,
    _validate_config,
    _xdg_root,
    default_config_path,
    default_state_dir,
)
from .private_io import (
    ensure_private_directory,
    private_path_lock,
    read_private_text,
    write_private_text,
)

SERVICE_NAME = "codex-usage.service"
TIMER_NAME = "codex-usage.timer"
MANAGED_MARKER = "X-Codex-Usage-Managed=true"
MAX_UNIT_BYTES = 100_000
SYSTEMCTL_OUTPUT_MAX_BYTES = 64 * 1024
SYSTEMCTL_TIMEOUT_SECONDS = 30
SERVICE_OPERATION_LOCK_NAME = ".codex-usage-operation"
SERVICE_OPERATION_LOCK_TIMEOUT_SECONDS = 30
EXEC_MAIN_CODE_NAMES = {
    "1": "exited",
    "2": "killed",
    "3": "dumped",
    "4": "trapped",
    "5": "stopped",
    "6": "continued",
}


class ServiceError(Exception):
    pass


def _raise_service_rollback_error(
    operation: str,
    primary_error: Exception,
    rollback_errors: list[Exception],
) -> None:
    causes = ExceptionGroup(
        f"{operation} rollback failed: primary operation, rollback steps",
        [primary_error, *rollback_errors],
    )
    raise ServiceError(f"could not roll back {operation}") from causes


@contextmanager
def _service_operation_lock() -> Iterator[None]:
    unit_dir = _unit_directory()
    with private_path_lock(
        unit_dir / SERVICE_OPERATION_LOCK_NAME,
        timeout_seconds=SERVICE_OPERATION_LOCK_TIMEOUT_SECONDS,
        label="systemd service lock",
    ):
        yield


def service_enable(config: AppConfig, config_path: Path | None = None) -> dict[str, Any]:
    _validate_config(config)
    selected_config_path = _select_service_config_path(config_path)
    with _service_operation_lock():
        return _service_enable_unlocked(config, selected_config_path)


def _service_enable_unlocked(
    config: AppConfig, config_path: Path | None = None
) -> dict[str, Any]:
    unit_dir = _unit_directory()
    _validate_existing_managed_units(unit_dir)
    paths = (unit_dir / SERVICE_NAME, unit_dir / TIMER_NAME)
    previous = {
        path: _read_unit_snapshot(path)
        for path in paths
    }
    activation = _systemd_activation_snapshot()
    result = _service_install_unlocked(config, config_path)
    enable_started = False
    enable_completed = False
    try:
        enable_started = True
        _systemctl("enable", TIMER_NAME)
        enable_completed = True
        _systemctl("restart", TIMER_NAME)
    except Exception as primary_error:
        rollback_errors: list[Exception] = []
        if activation[0] == "not-found" and enable_started:
            if enable_completed:
                try:
                    _restore_systemd_activation(activation)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            try:
                _cleanup_managed_timer_enable_link()
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
        try:
            _restore_unit_snapshot(previous)
        except Exception as rollback_error:
            rollback_errors.append(rollback_error)
        try:
            _systemctl("daemon-reload")
        except Exception as rollback_error:
            rollback_errors.append(rollback_error)
        if activation[0] != "not-found":
            try:
                _restore_systemd_activation(activation)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
        if rollback_errors:
            _raise_service_rollback_error(
                "service activation", primary_error, rollback_errors
            )
        raise primary_error
    return {**result, **service_status()}


def service_install(config: AppConfig, config_path: Path | None = None) -> dict[str, Any]:
    _validate_config(config)
    selected_config_path = _select_service_config_path(config_path)
    with _service_operation_lock():
        return _service_install_unlocked(config, selected_config_path)


def _service_install_unlocked(
    config: AppConfig, config_path: Path | None = None
) -> dict[str, Any]:
    unit_dir = _unit_directory()
    _validate_existing_managed_units(unit_dir)
    executable = _resolve_codex_usage()
    config_file = _select_service_config_path(config_path).expanduser().absolute()
    service_text = _render_service(config, executable, config_file)
    timer_text = _render_timer(config.interval_seconds)
    paths = (unit_dir / SERVICE_NAME, unit_dir / TIMER_NAME)
    previous = {
        path: _read_unit_snapshot(path)
        for path in paths
    }
    reload_attempted = False
    try:
        write_private_text(paths[0], service_text, label="systemd service", mode=0o600)
        write_private_text(paths[1], timer_text, label="systemd timer", mode=0o600)
        reload_attempted = True
        _systemctl("daemon-reload")
    except Exception as primary_error:
        rollback_errors: list[Exception] = []
        try:
            _restore_unit_snapshot(previous)
        except Exception as rollback_error:
            rollback_errors.append(rollback_error)
        if reload_attempted:
            try:
                _systemctl("daemon-reload")
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
        if rollback_errors:
            _raise_service_rollback_error(
                "service installation", primary_error, rollback_errors
            )
        raise primary_error
    return {"installed": True, "service": SERVICE_NAME, "timer": TIMER_NAME}


def _select_service_config_path(path: object | None) -> Path:
    if path is not None and not isinstance(path, Path):
        raise ValueError("config path must be a Path")
    selected = default_config_path() if path is None else path
    try:
        return selected.expanduser()
    except RuntimeError as exc:
        raise ValueError("config path cannot be resolved") from exc


def service_disable() -> dict[str, Any]:
    with _service_operation_lock():
        return _service_disable_unlocked()


def _service_disable_unlocked() -> dict[str, Any]:
    unit_dir = _unit_directory(create=False)
    if _require_complete_managed_units(unit_dir) is not None:
        _systemctl("disable", "--now", TIMER_NAME)
    return service_status()


def service_uninstall() -> dict[str, Any]:
    with _service_operation_lock():
        return _service_uninstall_unlocked()


def _service_uninstall_unlocked() -> dict[str, Any]:
    unit_dir = _unit_directory()
    paths = _require_complete_managed_units(unit_dir)
    if paths is None:
        return {"installed": False, "enabled": False, "active": False}
    previous = {
        path: _read_unit_snapshot(path)
        for path in paths
    }
    activation = _systemd_activation_snapshot()
    try:
        _service_disable_unlocked()
        for path in paths:
            _validate_managed_unit(path)
            path.unlink()
        _systemctl("daemon-reload")
    except Exception as primary_error:
        rollback_errors: list[Exception] = []
        try:
            _restore_unit_snapshot(previous)
        except Exception as rollback_error:
            rollback_errors.append(rollback_error)
        try:
            _systemctl("daemon-reload")
        except Exception as rollback_error:
            rollback_errors.append(rollback_error)
        try:
            _restore_systemd_activation(activation)
        except Exception as rollback_error:
            rollback_errors.append(rollback_error)
        if rollback_errors:
            _raise_service_rollback_error(
                "service uninstallation", primary_error, rollback_errors
            )
        raise primary_error
    return {"installed": False, "enabled": False, "active": False}


def service_status() -> dict[str, Any]:
    unit_dir = _unit_directory(create=False)
    service_path = unit_dir / SERVICE_NAME
    timer_path = unit_dir / TIMER_NAME
    installed = _is_managed_unit(service_path) and _is_managed_unit(timer_path)
    if installed:
        enabled = _systemctl_state("is-enabled", TIMER_NAME) == "enabled"
        timer_active = _systemctl_state("is-active", TIMER_NAME) == "active"
        service_active = _systemctl_state("is-active", SERVICE_NAME) in {
            "active",
            "activating",
        }
        timer_details = _systemctl_show(
            TIMER_NAME,
            (
                "SubState",
                "NextElapseUSecMonotonic",
                "NextElapseUSecRealtime",
            ),
        )
        timer_substate = timer_details.get("SubState", "unknown").strip().lower()
        timer_next_elapse = (
            timer_details.get("NextElapseUSecMonotonic")
            or timer_details.get("NextElapseUSecRealtime")
            or ""
        ).strip().lower()
        timer_scheduled = timer_substate == "waiting" and timer_next_elapse not in {
            "",
            "0",
            "infinity",
            "n/a",
        }
        active = timer_active and (service_active or timer_scheduled)
        details = _systemctl_show(
            SERVICE_NAME,
            (
                "Result",
                "ExecMainStatus",
                "ExecMainCode",
                "ExecMainStartTimestamp",
                "ExecMainExitTimestamp",
            ),
        )
    else:
        enabled = False
        active = False
        service_active = False
        timer_scheduled = False
        timer_substate = "unknown"
        details = {}
    return {
        "installed": installed,
        "enabled": enabled,
        "active": active,
        "service_active": service_active,
        "timer_scheduled": timer_scheduled,
        "timer_substate": timer_substate,
        "service_result": details.get("Result", "unknown"),
        "service_exit_status": details.get("ExecMainStatus", "unknown"),
        "service_exit_code": _normalize_exec_main_code(details.get("ExecMainCode")),
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
            return Path(argv[config_index + 1].replace("%%", "%")).expanduser().absolute()
        except (IndexError, RuntimeError, ValueError):
            return None
    return None


def _render_service(config: AppConfig, executable: Path, config_path: Path) -> str:
    state = default_state_dir().expanduser().absolute()
    # watchdog reads config; only state, browser cache, profiles and auth
    # refresh targets need write access.
    writable = [state]
    cache = Path.home() / ".cache" / "ms-playwright"
    writable.append(cache)
    for account in config.accounts:
        profile = Path(account.profile_dir).expanduser().absolute()
        _validate_home_path(profile)
        _reject_home_write_path(profile, label="profile directory")
        writable.append(profile)
        if account.auth_json_path:
            parent = Path(account.auth_json_path).expanduser().absolute().parent
            _validate_home_path(parent)
            _reject_home_write_path(parent, label="auth.json parent")
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
OnActiveSec=1min
OnUnitActiveSec={interval_seconds}s
AccuracySec=30s
Persistent=true
Unit={SERVICE_NAME}

[Install]
WantedBy=timers.target
"""


def _unit_directory(*, create: bool = True) -> Path:
    root = _xdg_root("XDG_CONFIG_HOME", Path.home() / ".config")
    path = root / "systemd" / "user"
    _assert_no_symlink_ancestors(path)
    if create:
        ensure_private_directory(path, label="systemd user unit directory")
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ServiceError("systemd user unit directory must be a real directory")
    if not path.exists():
        return path
    if not create:
        try:
            ensure_private_directory(path, label="systemd user unit directory")
        except (OSError, ValueError) as exc:
            raise ServiceError("could not secure systemd user unit directory") from exc
    return path


def _assert_no_symlink_ancestors(path: Path) -> None:
    raw_path = Path(path)
    absolute = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        if part == ".":
            continue
        if part == "..":
            current = current.parent
            continue
        current /= part
        if current.is_symlink():
            raise ServiceError("systemd user unit path must not contain symlinks")


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


def _reject_home_write_path(path: Path, *, label: str) -> None:
    try:
        resolved = path.resolve(strict=False)
        home = Path.home().resolve()
    except (OSError, RuntimeError) as exc:
        raise ServiceError(f"{label} cannot be resolved") from exc
    if resolved == home:
        raise ServiceError(f"{label} must not be the home directory")


def _unit_quote(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ServiceError("systemd unit value contains invalid characters")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped.replace("%", "%%") + '"'


def _terminate_systemctl_process(process: subprocess.Popen[bytes]) -> None:
    pid = getattr(process, "pid", None)
    if type(pid) is int and pid > 0:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (OSError, ValueError):
            pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_systemctl_bounded(command: list[str]) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
    )
    stdout = process.stdout
    stderr = process.stderr
    if stdout is None or stderr is None:
        _terminate_systemctl_process(process)
        raise OSError("systemctl output pipe unavailable")
    streams = {stdout: bytearray(), stderr: bytearray()}
    selector = selectors.DefaultSelector()
    total = 0
    deadline = time.monotonic() + SYSTEMCTL_TIMEOUT_SECONDS
    try:
        selector.register(stdout, selectors.EVENT_READ)
        selector.register(stderr, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_systemctl_process(process)
                raise subprocess.TimeoutExpired(command, SYSTEMCTL_TIMEOUT_SECONDS)
            ready = selector.select(remaining)
            if not ready:
                _terminate_systemctl_process(process)
                raise subprocess.TimeoutExpired(command, SYSTEMCTL_TIMEOUT_SECONDS)
            for key, _ in ready:
                stream = cast(IO[bytes], key.fileobj)
                chunk = os.read(stream.fileno(), min(8192, SYSTEMCTL_OUTPUT_MAX_BYTES + 1 - total))
                if not chunk:
                    selector.unregister(stream)
                    continue
                total += len(chunk)
                if total > SYSTEMCTL_OUTPUT_MAX_BYTES:
                    _terminate_systemctl_process(process)
                    raise ServiceError("systemctl output exceeded configured limit")
                streams[stream].extend(chunk)
        try:
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _terminate_systemctl_process(process)
            raise
        return subprocess.CompletedProcess(
            command,
            returncode,
            bytes(streams[stdout]).decode("utf-8", "replace"),
            bytes(streams[stderr]).decode("utf-8", "replace"),
        )
    except BaseException:
        if process.poll() is None:
            _terminate_systemctl_process(process)
        raise
    finally:
        selector.close()
        stdout.close()
        stderr.close()


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = shutil.which("systemctl")
    if not command:
        raise ServiceError("systemctl was not found")
    argv = [command, "--user", *args]
    try:
        completed = _run_systemctl_bounded(argv)
    except (OSError, subprocess.TimeoutExpired, ServiceError) as exc:
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


def _systemd_activation_snapshot() -> tuple[str, str]:
    return (
        _systemctl_state("is-enabled", TIMER_NAME),
        _systemctl_state("is-active", TIMER_NAME),
    )


def _restore_systemd_activation(snapshot: tuple[str, str]) -> None:
    enabled, active = snapshot
    errors: list[Exception] = []
    enabled_command = {
        "enabled": "enable",
        "disabled": "disable",
        "not-found": "disable",
    }.get(enabled)
    if enabled_command is not None:
        try:
            _systemctl(enabled_command, TIMER_NAME)
        except Exception as exc:
            errors.append(exc)
    else:
        errors.append(ServiceError(f"cannot restore systemd enabled state: {enabled}"))
    active_command = {"active": "start", "inactive": "stop"}.get(active)
    if active_command is not None:
        try:
            _systemctl(active_command, TIMER_NAME)
        except Exception as exc:
            errors.append(exc)
    else:
        errors.append(ServiceError(f"cannot restore systemd active state: {active}"))
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise ExceptionGroup("systemd activation restore failed", errors)


def _cleanup_managed_timer_enable_link() -> None:
    unit_dir = _unit_directory(create=False)
    wants_dir = unit_dir / "timers.target.wants"
    _assert_no_symlink_ancestors(wants_dir)
    if not wants_dir.exists():
        if wants_dir.is_symlink():
            raise ServiceError("systemd timer wants directory must not be a symlink")
        return
    if not wants_dir.is_dir():
        raise ServiceError("systemd timer wants path must be a directory")
    link = wants_dir / TIMER_NAME
    if not (link.exists() or link.is_symlink()):
        return
    if not link.is_symlink():
        raise ServiceError("refusing to remove a non-symlink systemd enable path")
    try:
        target = link.resolve(strict=False)
        expected = (unit_dir / TIMER_NAME).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ServiceError("could not resolve systemd enable link") from exc
    if target != expected:
        raise ServiceError("refusing to remove a foreign systemd enable link")
    link.unlink()


def _normalize_exec_main_code(value: str | None) -> str:
    code = str(value or "unknown").strip()
    return EXEC_MAIN_CODE_NAMES.get(code, code or "unknown")


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


def _read_unit_snapshot(path: Path) -> str | None:
    if not (path.exists() or path.is_symlink()):
        return None
    text, _ = read_private_text(
        path,
        regular_label="systemd unit",
        read_label="systemd unit",
        max_bytes=MAX_UNIT_BYTES,
    )
    return text


def _restore_unit_snapshot(previous: dict[Path, str | None]) -> None:
    errors: list[Exception] = []
    for path, text in previous.items():
        try:
            if text is None:
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise ServiceError(f"cannot remove unexpected systemd unit: {path}")
                path.unlink(missing_ok=True)
                continue
            label = "systemd service" if path.name == SERVICE_NAME else "systemd timer"
            write_private_text(path, text, label=label, mode=0o600)
        except Exception as exc:
            errors.append(exc)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise ExceptionGroup("systemd unit snapshot restore failed", errors)


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
