from __future__ import annotations

import ast
import base64
import csv
import errno
import hashlib
import importlib.metadata as importlib_metadata
import json
import os
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePath, PurePosixPath
from typing import IO, Any, cast

from . import private_io
from .config import (
    AppConfig,
    _validate_config,
    _xdg_root,
    default_config_path,
    default_state_dir,
)
from .integration_timeout_contract import INTEGRATION_WATCHDOG_SYSTEMD_TIMEOUT_SECONDS
from .private_io import (
    ensure_private_directory,
    private_path_lock,
    read_private_text,
    write_private_text,
)

SERVICE_NAME = "codex-usage.service"
TIMER_NAME = "codex-usage.timer"
INTEGRATION_WATCHDOG_EXECUTABLE_NAME = "codex-usage-integration-watchdog"
MANAGED_MARKER = "X-Codex-Usage-Managed=true"
MAX_UNIT_BYTES = 100_000
SYSTEMCTL_OUTPUT_MAX_BYTES = 64 * 1024
SYSTEMCTL_TIMEOUT_SECONDS = 30
SERVICE_OPERATION_LOCK_NAME = ".codex-usage-operation"
SERVICE_OPERATION_LOCK_TIMEOUT_SECONDS = 30
EXECUTABLE_SCRIPT_MAX_BYTES = 128 * 1024
INTERPRETER_MAX_BYTES = 128 * 1024 * 1024
PACKAGE_FILE_MAX_BYTES = 1024 * 1024
PACKAGE_RECORD_MAX_BYTES = 2 * 1024 * 1024
MAX_DISTRIBUTION_FILES = 4096
EXPECTED_DISTRIBUTION_NAME = "codex-usage"
EXPECTED_DISTRIBUTION_VERSION = "0.6.537"
SERVICE_RUNTIME_UNSET_ENVIRONMENT_NAMES = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONUSERBASE",
    "PYTHONSTARTUP",
    "PYTHONINSPECT",
    "PYTHONEXECUTABLE",
    "PYTHONBREAKPOINT",
    "PYTHONDEBUG",
    "PYTHONVERBOSE",
    "PYTHONCASEOK",
    "PYTHONPYCACHEPREFIX",
    "PYTHONPLATLIBDIR",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "LD_DEBUG",
    "LD_ORIGIN_PATH",
    "LD_PROFILE",
    "LD_USE_LOAD_BIAS",
    "DYLD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
)
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


class ServicePartialInstallError(ServiceError):
    def __init__(self, operation: str, partial_units: tuple[Path, ...]) -> None:
        self.operation = operation
        self.partial_units = partial_units
        units = ", ".join(str(path) for path in partial_units)
        super().__init__(
            f"could not roll back {operation}; partial systemd unit state remains: {units}"
        )


@dataclass(frozen=True)
class _RegularFileBinding:
    path: Path
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int
    payload: bytes
    sha256: str


@dataclass(frozen=True)
class _DistributionBinding:
    version: str
    metadata: _RegularFileBinding
    record: _RegularFileBinding
    modules: tuple[_RegularFileBinding, ...]


@dataclass(frozen=True)
class _DirectoryBinding:
    path: Path
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _ExecutableBinding:
    path: Path
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int
    payload: bytes
    sha256: str
    interpreter: _RegularFileBinding
    expected_module: str
    distribution: _DistributionBinding | None = None


_RESOLVED_INTEGRATION_WATCHDOG_BINDINGS: dict[
    Path, tuple[_ExecutableBinding, _ExecutableBinding]
] = {}


def _raise_service_rollback_error(
    operation: str,
    primary_error: BaseException,
    rollback_errors: list[BaseException],
    *,
    partial_units: tuple[Path, ...] = (),
) -> None:
    errors = [primary_error, *rollback_errors]
    message = f"{operation} rollback failed: primary operation, rollback steps"
    if all(isinstance(error, Exception) for error in errors):
        causes: BaseException = ExceptionGroup(
            message,
            cast("list[Exception]", errors),
        )
    else:
        causes = BaseExceptionGroup(message, errors)
    if partial_units:
        raise ServicePartialInstallError(operation, partial_units) from causes
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
        _before_service_unit_write()
        _revalidate_integration_watchdog_for_unit_write(executable)
        write_private_text(paths[0], service_text, label="systemd service", mode=0o600)
        _revalidate_integration_watchdog_for_unit_write(executable)
        write_private_text(paths[1], timer_text, label="systemd timer", mode=0o600)
        _revalidate_integration_watchdog_for_unit_write(executable)
        reload_attempted = True
        _systemctl("daemon-reload")
        _revalidate_integration_watchdog_for_unit_write(executable)
    except BaseException as primary_error:
        rollback_errors: list[BaseException] = []
        try:
            _restore_unit_snapshot(previous)
        except BaseException as rollback_error:
            rollback_errors.append(rollback_error)
        partial_units: list[Path] = []
        for path, expected in previous.items():
            try:
                current = _read_unit_snapshot(path)
            except BaseException as inspection_error:
                rollback_errors.append(inspection_error)
                partial_units.append(path)
                continue
            if current != expected:
                partial_units.append(path)
        if reload_attempted:
            try:
                _systemctl("daemon-reload")
            except BaseException as rollback_error:
                rollback_errors.append(rollback_error)
        if rollback_errors or partial_units:
            _raise_service_rollback_error(
                "service installation",
                primary_error,
                rollback_errors,
                partial_units=tuple(partial_units),
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
    data_root = _xdg_root("XDG_DATA_HOME", Path.home() / ".local" / "share")
    state_root = _xdg_root("XDG_STATE_HOME", Path.home() / ".local" / "state")
    state = default_state_dir().expanduser().absolute()
    integration_state = state_root / "codex-usage" / "integration"
    lock_root = private_io._private_lock_root()
    # watchdog reads config; only state, browser cache, profiles and auth
    # refresh targets need write access.
    writable = [state, integration_state, lock_root]
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
    integration_watchdog = executable.with_name(INTEGRATION_WATCHDOG_EXECUTABLE_NAME)
    exec_start = " ".join(
        _unit_quote(value)
        for value in (
            str(integration_watchdog),
            "--config",
            str(config_path),
        )
    )
    return f"""[Unit]
Description=Watch ChatGPT Codex usage analytics
Documentation=https://github.com/H234598/codex-usage
{MANAGED_MARKER}

[Service]
Type=oneshot
ExecStart={exec_start}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONSAFEPATH=1
Environment=PYTHONNOUSERSITE=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment={_unit_quote(f"XDG_DATA_HOME={data_root}")}
Environment={_unit_quote(f"XDG_STATE_HOME={state_root}")}
UnsetEnvironment={" ".join(SERVICE_RUNTIME_UNSET_ENVIRONMENT_NAMES)}
TimeoutStartSec={INTEGRATION_WATCHDOG_SYSTEMD_TIMEOUT_SECONDS}
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

[Install]
WantedBy=default.target
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
    _assert_no_symlink_ancestors(path.parent)
    if create:
        _ensure_systemd_unit_directory(path)
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ServiceError("systemd user unit directory must be a real directory")
    if not path.exists():
        return path
    if not create:
        try:
            _ensure_systemd_unit_directory(path)
        except (OSError, ValueError) as exc:
            raise ServiceError("could not secure systemd user unit directory") from exc
    return path


def _ensure_systemd_unit_directory(path: Path) -> None:
    try:
        ensure_private_directory(path, label="systemd user unit directory")
        return
    except ValueError:
        if not path.exists() or path.is_symlink() or not path.is_dir():
            raise
    private_io._chmod_private_directory(path, label="systemd user unit directory")
    ensure_private_directory(path, label="systemd user unit directory")


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
    codex_binding = _read_bound_console_script(
        path,
        expected_module="codex_usage.cli",
        label="codex-usage executable",
    )
    wrapper = _resolve_integration_watchdog_executable(path)
    codex_binding, wrapper = _bind_console_scripts_to_distribution(
        codex_binding,
        wrapper,
    )
    _RESOLVED_INTEGRATION_WATCHDOG_BINDINGS[path] = (codex_binding, wrapper)
    return path


def _resolve_integration_watchdog_executable(
    codex_usage_executable: Path,
) -> _ExecutableBinding:
    expected = codex_usage_executable.with_name(
        INTEGRATION_WATCHDOG_EXECUTABLE_NAME
    ).absolute()
    resolved = shutil.which(INTEGRATION_WATCHDOG_EXECUTABLE_NAME)
    if not resolved:
        raise ServiceError("integration watchdog executable was not found")
    path = Path(resolved).absolute()
    if path != expected:
        raise ServiceError(
            "integration watchdog executable must be installed beside codex-usage"
        )
    return _read_bound_console_script(
        path,
        expected_module="codex_usage.integration_watchdog",
        label="integration watchdog executable",
    )


def _normalize_distribution_name(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _normalized_path(path: object) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _pep376_sha256(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode("ascii")
    return "sha256=" + digest.rstrip("=")


def _read_bound_regular_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    executable: bool = False,
    single_link: bool = False,
) -> _RegularFileBinding:
    _assert_no_symlink_ancestors(path.parent)
    fd = -1
    try:
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        initial = os.fstat(fd)
        mode = stat.S_IMODE(initial.st_mode)
        if not stat.S_ISREG(initial.st_mode):
            raise ServiceError(f"{label} must be a regular executable")
        if initial.st_uid not in {0, os.geteuid()}:
            raise ServiceError(f"{label} owner is not trusted")
        if single_link and initial.st_nlink != 1:
            raise ServiceError(f"{label} must be single-linked")
        if mode & 0o022:
            raise ServiceError(f"{label} is writable by an untrusted account")
        if executable and not mode & 0o100:
            raise ServiceError(f"{label} is not executable")
        if initial.st_size <= 0 or initial.st_size > max_bytes:
            raise ServiceError(f"{label} size is invalid")
        payload = bytearray()
        while len(payload) <= max_bytes:
            chunk = os.read(fd, min(65_536, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > max_bytes:
            raise ServiceError(f"{label} size is invalid")
        final = os.fstat(fd)
        if (
            final.st_dev != initial.st_dev
            or final.st_ino != initial.st_ino
            or final.st_mode != initial.st_mode
            or final.st_uid != initial.st_uid
            or final.st_gid != initial.st_gid
            or final.st_nlink != initial.st_nlink
            or final.st_size != initial.st_size
            or final.st_mtime_ns != initial.st_mtime_ns
            or final.st_ctime_ns != initial.st_ctime_ns
        ):
            raise ServiceError(f"{label} changed during read")
    except ServiceError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO, errno.ENOTDIR):
            raise ServiceError(f"{label} must be a regular executable") from exc
        raise ServiceError(f"{label} is unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    payload_bytes = bytes(payload)
    return _RegularFileBinding(
        path=path,
        device=initial.st_dev,
        inode=initial.st_ino,
        mode=initial.st_mode,
        uid=initial.st_uid,
        gid=initial.st_gid,
        nlink=initial.st_nlink,
        size=initial.st_size,
        mtime_ns=initial.st_mtime_ns,
        ctime_ns=initial.st_ctime_ns,
        payload=payload_bytes,
        sha256=hashlib.sha256(payload_bytes).hexdigest(),
    )


def _read_bound_regular_file_at(
    parent_fd: int,
    name: str,
    path: Path,
    *,
    label: str,
    max_bytes: int,
) -> _RegularFileBinding:
    fd = -1
    try:
        fd = os.open(
            _safe_unit_component(name),
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        initial = os.fstat(fd)
        mode = stat.S_IMODE(initial.st_mode)
        if not stat.S_ISREG(initial.st_mode):
            raise ServiceError(f"{label} must be a regular executable")
        if initial.st_uid not in {0, os.geteuid()}:
            raise ServiceError(f"{label} owner is not trusted")
        if mode & 0o022:
            raise ServiceError(f"{label} is writable by an untrusted account")
        if initial.st_size <= 0 or initial.st_size > max_bytes:
            raise ServiceError(f"{label} size is invalid")
        payload = bytearray()
        while len(payload) <= max_bytes:
            chunk = os.read(fd, min(65_536, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > max_bytes:
            raise ServiceError(f"{label} size is invalid")
        final = os.fstat(fd)
        if (
            final.st_dev != initial.st_dev
            or final.st_ino != initial.st_ino
            or final.st_mode != initial.st_mode
            or final.st_uid != initial.st_uid
            or final.st_gid != initial.st_gid
            or final.st_nlink != initial.st_nlink
            or final.st_size != initial.st_size
            or final.st_mtime_ns != initial.st_mtime_ns
            or final.st_ctime_ns != initial.st_ctime_ns
        ):
            raise ServiceError(f"{label} changed during attestation")
    except ServiceError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO, errno.ENOTDIR):
            raise ServiceError(f"{label} must be a regular executable") from exc
        raise ServiceError(f"{label} is unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    payload_bytes = bytes(payload)
    return _RegularFileBinding(
        path=path,
        device=initial.st_dev,
        inode=initial.st_ino,
        mode=initial.st_mode,
        uid=initial.st_uid,
        gid=initial.st_gid,
        nlink=initial.st_nlink,
        size=initial.st_size,
        mtime_ns=initial.st_mtime_ns,
        ctime_ns=initial.st_ctime_ns,
        payload=payload_bytes,
        sha256=hashlib.sha256(payload_bytes).hexdigest(),
    )


def _directory_binding_from_fd(path: Path, fd: int, *, label: str) -> _DirectoryBinding:
    item = os.fstat(fd)
    mode = stat.S_IMODE(item.st_mode)
    if not stat.S_ISDIR(item.st_mode):
        raise ServiceError(f"{label} is unavailable")
    if item.st_uid not in {0, os.geteuid()}:
        raise ServiceError(f"{label} owner is not trusted")
    if mode & 0o022:
        raise ServiceError(f"{label} is writable by an untrusted account")
    if item.st_nlink < 1:
        raise ServiceError(f"{label} link count is invalid")
    return _DirectoryBinding(
        path=path,
        device=item.st_dev,
        inode=item.st_ino,
        mode=item.st_mode,
        uid=item.st_uid,
        gid=item.st_gid,
        nlink=item.st_nlink,
        mtime_ns=item.st_mtime_ns,
        ctime_ns=item.st_ctime_ns,
    )


def _read_bound_directory(path: Path, *, label: str) -> _DirectoryBinding:
    _assert_no_symlink_ancestors(path)
    fd = -1
    try:
        fd = os.open(path, private_io._directory_open_flags())
        binding = _directory_binding_from_fd(path, fd, label=label)
        final = _directory_binding_from_fd(path, fd, label=label)
        if final != binding:
            raise ServiceError(f"{label} changed during attestation")
        return binding
    except ServiceError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ServiceError(f"{label} is unsafe") from exc
        raise ServiceError(f"{label} is unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _console_script_interpreter(
    payload: bytes,
    *,
    label: str,
) -> _RegularFileBinding:
    first_line = payload.splitlines()[0] if payload.splitlines() else b""
    if not first_line.startswith(b"#!"):
        raise ServiceError(f"{label} has unexpected entry point")
    try:
        tokens = shlex.split(first_line[2:].decode("utf-8", "strict").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise ServiceError(f"{label} interpreter is invalid") from exc
    if len(tokens) != 1:
        raise ServiceError(f"{label} interpreter is invalid")
    actual = _normalized_path(tokens[0])
    expected = _normalized_path(sys.executable)
    if actual != expected:
        raise ServiceError(f"{label} interpreter is not trusted")
    try:
        interpreter_path = expected.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ServiceError(f"{label} interpreter is unavailable") from exc
    return _read_bound_regular_file(
        interpreter_path,
        label=f"{label} interpreter",
        max_bytes=INTERPRETER_MAX_BYTES,
        executable=True,
        single_link=True,
    )


def _console_script_imports_module(payload: bytes, expected_module: str) -> bool:
    try:
        text = payload.decode("utf-8", "strict")
        tree = ast.parse(text)
    except (SyntaxError, UnicodeDecodeError):
        return False
    statements = tuple(tree.body)
    if statements and isinstance(statements[0], ast.Expr) and isinstance(
        statements[0].value,
        ast.Constant,
    ) and isinstance(statements[0].value.value, str):
        statements = statements[1:]
    sys_import_seen = False
    expected_import_seen = False
    main_call_seen = False
    for statement in statements:
        if (
            isinstance(statement, ast.Import)
            and len(statement.names) == 1
            and statement.names[0].name == "sys"
            and statement.names[0].asname is None
        ):
            if sys_import_seen or expected_import_seen or main_call_seen:
                return False
            sys_import_seen = True
            continue
        if _is_expected_console_main_import(statement, expected_module):
            if expected_import_seen or main_call_seen:
                return False
            expected_import_seen = True
            continue
        if _binds_console_main(statement):
            return False
        if _is_console_script_main_guard(statement):
            if main_call_seen or not expected_import_seen:
                return False
            main_call_seen = _console_main_guard_calls_main(statement)
            continue
        return False
    return expected_import_seen and main_call_seen


def _is_expected_console_main_import(
    statement: ast.stmt,
    expected_module: str,
) -> bool:
    return (
        isinstance(statement, ast.ImportFrom)
        and statement.level == 0
        and statement.module == expected_module
        and len(statement.names) == 1
        and statement.names[0].name == "main"
        and statement.names[0].asname is None
    )


def _binds_console_main(statement: ast.stmt) -> bool:
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return statement.name == "main"
    if isinstance(statement, ast.ImportFrom):
        return any((alias.asname or alias.name) == "main" for alias in statement.names)
    if isinstance(statement, ast.Import):
        return any(
            (alias.asname or alias.name.partition(".")[0]) == "main"
            for alias in statement.names
        )
    if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets: list[ast.expr] = []
        if isinstance(statement, ast.Assign):
            targets.extend(statement.targets)
        else:
            targets.append(statement.target)
        return any(_target_binds_name(target, "main") for target in targets)
    return False


def _target_binds_name(target: ast.expr, name: str) -> bool:
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_target_binds_name(item, name) for item in target.elts)
    return False


def _is_console_script_main_guard(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.If):
        return False
    test = statement.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    left, comparator = test.left, test.comparators[0]
    if not isinstance(test.ops[0], ast.Eq):
        return False
    return (
        isinstance(left, ast.Name)
        and left.id == "__name__"
        and isinstance(comparator, ast.Constant)
        and comparator.value == "__main__"
    )


def _console_main_guard_calls_main(statement: ast.If) -> bool:
    if statement.orelse:
        return False
    body = statement.body
    if len(body) == 2:
        if not _is_pip_261_argv0_normalization(body[0]):
            return False
        body = body[1:]
    if len(body) != 1:
        return False
    guarded = body[0]
    if isinstance(guarded, ast.Expr) and isinstance(guarded.value, ast.Call):
        return _is_allowed_console_main_exit_call(guarded.value)
    return _is_allowed_console_main_system_exit_raise(guarded)


def _is_pip_261_argv0_normalization(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        return False
    if not _is_sys_argv_zero(statement.targets[0]):
        return False
    value = statement.value
    return (
        isinstance(value, ast.Call)
        and not value.keywords
        and len(value.args) == 1
        and isinstance(value.args[0], ast.Constant)
        and type(value.args[0].value) is str
        and value.args[0].value == ".exe"
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "removesuffix"
        and _is_sys_argv_zero(value.func.value)
    )


def _is_sys_argv_zero(expression: ast.expr) -> bool:
    return (
        isinstance(expression, ast.Subscript)
        and isinstance(expression.value, ast.Attribute)
        and isinstance(expression.value.value, ast.Name)
        and expression.value.value.id == "sys"
        and expression.value.attr == "argv"
        and isinstance(expression.slice, ast.Constant)
        and type(expression.slice.value) is int
        and expression.slice.value == 0
    )


def _is_allowed_console_main_exit_call(call: ast.Call) -> bool:
    return _is_direct_console_main_call(call) or _is_sys_exit_console_main_call(call)


def _is_allowed_console_main_system_exit_raise(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Raise)
        and statement.cause is None
        and isinstance(statement.exc, ast.Call)
        and isinstance(statement.exc.func, ast.Name)
        and statement.exc.func.id == "SystemExit"
        and not statement.exc.keywords
        and len(statement.exc.args) == 1
        and isinstance(statement.exc.args[0], ast.Call)
        and _is_direct_console_main_call(statement.exc.args[0])
    )


def _is_direct_console_main_call(call: ast.Call) -> bool:
    if isinstance(call.func, ast.Name) and call.func.id == "main":
        return not call.args and not call.keywords
    return False


def _is_sys_exit_console_main_call(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "exit"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "sys"
        and not call.keywords
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Call)
        and _is_direct_console_main_call(call.args[0])
    )


def _read_bound_console_script(
    path: Path,
    *,
    expected_module: str,
    label: str,
) -> _ExecutableBinding:
    binding = _read_bound_regular_file(
        path,
        label=label,
        max_bytes=EXECUTABLE_SCRIPT_MAX_BYTES,
        executable=True,
        single_link=True,
    )
    interpreter = _console_script_interpreter(binding.payload, label=label)
    if not _console_script_imports_module(binding.payload, expected_module):
        raise ServiceError(f"{label} has unexpected entry point")
    return _ExecutableBinding(
        path=path,
        device=binding.device,
        inode=binding.inode,
        mode=binding.mode,
        uid=binding.uid,
        gid=binding.gid,
        nlink=binding.nlink,
        size=binding.size,
        mtime_ns=binding.mtime_ns,
        ctime_ns=binding.ctime_ns,
        payload=binding.payload,
        sha256=binding.sha256,
        interpreter=interpreter,
        expected_module=expected_module,
    )


def _distribution_file_path(distribution: object, relative_path: str) -> Path:
    try:
        located = distribution.locate_file(relative_path)
    except Exception as exc:
        raise ServiceError("codex-usage distribution RECORD is unavailable") from exc
    return _normalized_path(located)


def _distribution_site_packages(distribution: object) -> _DirectoryBinding:
    site_packages = _distribution_file_path(distribution, "")
    return _read_bound_directory(site_packages, label="codex-usage distribution root")


def _scan_dist_info_roots(
    distribution: object,
    site_packages: Path,
    *,
    site_packages_binding: _DirectoryBinding | None = None,
) -> tuple[str, str, _RegularFileBinding, _RegularFileBinding]:
    flags = private_io._directory_open_flags()
    root_fd = -1
    candidates: list[tuple[str, _RegularFileBinding, _RegularFileBinding]] = []
    try:
        root_fd = os.open(site_packages, flags)
        if site_packages_binding is not None and _directory_binding_from_fd(
            site_packages,
            root_fd,
            label="codex-usage distribution root",
        ) != site_packages_binding:
            raise ServiceError("codex-usage distribution root changed during attestation")
        with os.scandir(root_fd) as names:
            for index, entry in enumerate(names):
                if index >= MAX_DISTRIBUTION_FILES:
                    raise ServiceError("codex-usage distribution RECORD is too large")
                name = entry.name
                if not name.endswith(".dist-info"):
                    continue
                dist_fd = -1
                try:
                    dist_fd = os.open(_safe_unit_component(name), flags, dir_fd=root_fd)
                    dist_item = os.fstat(dist_fd)
                    if not stat.S_ISDIR(dist_item.st_mode):
                        raise ServiceError("codex-usage dist-info root is unavailable")
                    metadata_relative = f"{name}/METADATA"
                    record_relative = f"{name}/RECORD"
                    metadata = _read_bound_regular_file_at(
                        dist_fd,
                        "METADATA",
                        site_packages / metadata_relative,
                        label="codex-usage METADATA",
                        max_bytes=PACKAGE_FILE_MAX_BYTES,
                    )
                    headers = _metadata_headers(metadata.payload)
                    if (
                        _normalize_distribution_name(headers.get("name", ""))
                        != EXPECTED_DISTRIBUTION_NAME
                        or headers.get("version") != EXPECTED_DISTRIBUTION_VERSION
                    ):
                        continue
                    record = _read_bound_regular_file_at(
                        dist_fd,
                        "RECORD",
                        site_packages / record_relative,
                        label="codex-usage RECORD",
                        max_bytes=PACKAGE_RECORD_MAX_BYTES,
                    )
                    _parse_record(record.payload)
                    candidates.append((name, metadata, record))
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise ServiceError("codex-usage dist-info root is unsafe") from exc
                    raise ServiceError("codex-usage dist-info root is unavailable") from exc
                finally:
                    if dist_fd >= 0:
                        os.close(dist_fd)
        if site_packages_binding is not None and _directory_binding_from_fd(
            site_packages,
            root_fd,
            label="codex-usage distribution root",
        ) != site_packages_binding:
            raise ServiceError("codex-usage distribution root changed during attestation")
    except ServiceError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ServiceError("codex-usage distribution root is unsafe") from exc
        raise ServiceError("codex-usage distribution root is unavailable") from exc
    finally:
        if root_fd >= 0:
            os.close(root_fd)
    if len(candidates) != 1:
        raise ServiceError("codex-usage distribution dist-info root is unavailable")
    root, metadata, record = sorted(candidates, key=lambda item: item[0])[0]
    return f"{root}/METADATA", f"{root}/RECORD", metadata, record


def _safe_unit_component(name: object) -> str:
    if (
        type(name) is not str
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise ServiceError("codex-usage distribution path is invalid")
    return name


def _distribution_relative_for_path(
    distribution: object,
    files: tuple[str, ...],
    path: Path,
    *,
    label: str,
) -> str:
    target = _normalized_path(path)
    matches = [
        file
        for file in files
        if not PurePath(file).parent.name.endswith(".dist-info")
        and _distribution_file_path(distribution, file) == target
    ]
    if len(matches) != 1:
        raise ServiceError(f"codex-usage distribution RECORD does not uniquely bind {label}")
    return matches[0]


def _metadata_headers(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ServiceError("codex-usage METADATA is invalid") from exc
    headers: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            break
        key, separator, value = line.partition(":")
        if not separator:
            continue
        normalized = key.strip().lower()
        if normalized in headers:
            raise ServiceError("codex-usage METADATA has duplicate fields")
        headers[normalized] = value.strip()
    return headers


def _parse_record(payload: bytes) -> dict[str, tuple[str, str]]:
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ServiceError("codex-usage distribution RECORD is invalid") from exc
    entries: dict[str, tuple[str, str]] = {}
    for row in csv.reader(text.splitlines()):
        if len(row) != 3 or not row[0]:
            raise ServiceError("codex-usage distribution RECORD is invalid")
        _validate_record_relative_path(row[0])
        if row[0] in entries:
            raise ServiceError("codex-usage distribution RECORD has duplicate paths")
        entries[row[0]] = (row[1], row[2])
    return entries


def _validate_record_relative_path(relative_path: str) -> None:
    if "\x00" in relative_path:
        raise ServiceError("codex-usage distribution RECORD is invalid")
    path = PurePosixPath(relative_path)
    if path.is_absolute() or str(path) != relative_path:
        raise ServiceError("codex-usage distribution RECORD is invalid")
    parts = path.parts
    if not parts or any(part in {"", "."} for part in parts):
        raise ServiceError("codex-usage distribution RECORD is invalid")
    if ".." not in parts:
        return
    allowed_scripts = {
        ("..", "..", "..", "bin", "codex-usage"),
        ("..", "..", "..", "bin", INTEGRATION_WATCHDOG_EXECUTABLE_NAME),
    }
    if parts not in allowed_scripts:
        raise ServiceError("codex-usage distribution RECORD is invalid")


def _verify_record_selfrow(
    record_entries: dict[str, tuple[str, str]],
    record_relative: str,
) -> None:
    if record_entries.get(record_relative) != ("", ""):
        raise ServiceError("codex-usage distribution RECORD is missing RECORD")


def _verify_recorded_file(
    record_entries: dict[str, tuple[str, str]],
    relative_path: str,
    binding: _RegularFileBinding | _ExecutableBinding,
    *,
    label: str,
) -> None:
    entry = record_entries.get(relative_path)
    if entry is None:
        raise ServiceError(f"codex-usage distribution RECORD is missing {label}")
    expected_hash, expected_size = entry
    if expected_hash != _pep376_sha256(binding.payload) or expected_size != str(binding.size):
        raise ServiceError(f"codex-usage distribution RECORD does not match {label}")


def _read_distribution_file(
    distribution: object,
    relative_path: str,
    *,
    label: str,
    max_bytes: int = PACKAGE_FILE_MAX_BYTES,
) -> _RegularFileBinding:
    return _read_bound_regular_file(
        _distribution_file_path(distribution, relative_path),
        label=label,
        max_bytes=max_bytes,
    )


def _bind_console_scripts_to_distribution(
    codex_usage: _ExecutableBinding,
    integration_watchdog: _ExecutableBinding,
) -> tuple[_ExecutableBinding, _ExecutableBinding]:
    try:
        distribution = importlib_metadata.distribution(EXPECTED_DISTRIBUTION_NAME)
    except Exception as exc:
        raise ServiceError("codex-usage distribution is unavailable") from exc
    name = str(getattr(distribution, "metadata", {}).get("Name", ""))
    version = str(getattr(distribution, "version", ""))
    if _normalize_distribution_name(name) != EXPECTED_DISTRIBUTION_NAME:
        raise ServiceError("codex-usage distribution identity is invalid")
    if version != EXPECTED_DISTRIBUTION_VERSION:
        raise ServiceError(
            f"codex-usage distribution must be version {EXPECTED_DISTRIBUTION_VERSION}"
        )
    site_packages_binding = _distribution_site_packages(distribution)
    site_packages = site_packages_binding.path
    metadata_relative, record_relative, metadata, record = _scan_dist_info_roots(
        distribution,
        site_packages,
        site_packages_binding=site_packages_binding,
    )
    headers = _metadata_headers(metadata.payload)
    if _normalize_distribution_name(headers.get("name", "")) != EXPECTED_DISTRIBUTION_NAME:
        raise ServiceError("codex-usage METADATA identity is invalid")
    if headers.get("version") != EXPECTED_DISTRIBUTION_VERSION:
        raise ServiceError(
            f"codex-usage METADATA must be version {EXPECTED_DISTRIBUTION_VERSION}"
        )
    record_entries = _parse_record(record.payload)
    _verify_record_selfrow(record_entries, record_relative)
    files = tuple(record_entries)
    _verify_recorded_file(
        record_entries,
        metadata_relative,
        metadata,
        label="METADATA",
    )
    codex_relative = _distribution_relative_for_path(
        distribution,
        files,
        codex_usage.path,
        label="codex-usage executable",
    )
    watchdog_relative = _distribution_relative_for_path(
        distribution,
        files,
        integration_watchdog.path,
        label="integration watchdog executable",
    )
    _verify_recorded_file(
        record_entries,
        codex_relative,
        codex_usage,
        label="codex-usage executable",
    )
    _verify_recorded_file(
        record_entries,
        watchdog_relative,
        integration_watchdog,
        label="integration watchdog executable",
    )
    modules: list[_RegularFileBinding] = []
    for module in sorted({codex_usage.expected_module, integration_watchdog.expected_module}):
        module_relative = module.replace(".", "/") + ".py"
        if module_relative not in files:
            raise ServiceError(f"codex-usage distribution RECORD is missing {module}")
        module_binding = _read_distribution_file(
            distribution,
            module_relative,
            label=f"{module} module",
        )
        _verify_recorded_file(
            record_entries,
            module_relative,
            module_binding,
            label=f"{module} module",
        )
        modules.append(module_binding)
    distribution_binding = _DistributionBinding(
        version=EXPECTED_DISTRIBUTION_VERSION,
        metadata=metadata,
        record=record,
        modules=tuple(modules),
    )
    return (
        replace(codex_usage, distribution=distribution_binding),
        replace(integration_watchdog, distribution=distribution_binding),
    )


def _revalidate_integration_watchdog_for_unit_write(codex_usage_executable: Path) -> None:
    expected = _RESOLVED_INTEGRATION_WATCHDOG_BINDINGS.get(codex_usage_executable)
    if expected is None:
        raise ServiceError("integration watchdog executable was not resolved before unit write")
    try:
        codex_usage = _read_bound_console_script(
            expected[0].path,
            expected_module="codex_usage.cli",
            label="codex-usage executable",
        )
        integration_watchdog = _read_bound_console_script(
            expected[1].path,
            expected_module="codex_usage.integration_watchdog",
            label="integration watchdog executable",
        )
        current = _bind_console_scripts_to_distribution(
            codex_usage,
            integration_watchdog,
        )
    except ServiceError as exc:
        raise ServiceError(
            "integration watchdog executable changed before unit write"
        ) from exc
    if current != expected:
        raise ServiceError("integration watchdog executable changed before unit write")


def _before_service_unit_write() -> None:
    return None


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
    errors: list[BaseException] = []
    for path, text in previous.items():
        try:
            if text is None:
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise ServiceError(f"cannot remove unexpected systemd unit: {path}")
                path.unlink(missing_ok=True)
                continue
            label = "systemd service" if path.name == SERVICE_NAME else "systemd timer"
            write_private_text(path, text, label=label, mode=0o600)
        except BaseException as exc:
            errors.append(exc)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        if all(isinstance(error, Exception) for error in errors):
            raise ExceptionGroup(
                "systemd unit snapshot restore failed",
                cast("list[Exception]", errors),
            )
        raise BaseExceptionGroup("systemd unit snapshot restore failed", errors)


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
