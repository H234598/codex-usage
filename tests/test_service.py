from __future__ import annotations

import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import codex_usage.service as service_module
from codex_usage.config import AppConfig
from codex_usage.models import Account
from codex_usage.service import (
    SERVICE_NAME,
    TIMER_NAME,
    ServiceError,
    _terminate_systemctl_process,
    _unit_directory,
    managed_service_config_path,
    service_disable,
    service_enable,
    service_install,
    service_status,
    service_uninstall,
)


class _BrokenInt(int):
    def __gt__(self, _other):
        raise RuntimeError("synthetic service PID comparison marker")


def test_relative_xdg_config_home_uses_default_unit_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")

    unit_directory = _unit_directory()

    assert unit_directory == home / ".config" / "systemd" / "user"
    assert unit_directory.is_dir()
    assert not (cwd / "relative-config").exists()


@pytest.mark.parametrize("operation", (service_install, service_enable))
@pytest.mark.parametrize("config_path", ("", False, 0, [], {}))
def test_service_rejects_invalid_config_path_before_side_effects(
    tmp_path, monkeypatch, operation, config_path
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    with pytest.raises(ValueError, match="config path must be a Path"):
        operation(AppConfig(accounts=()), config_path)

    assert not (tmp_path / "config").exists()


@pytest.mark.parametrize("operation", (service_install, service_enable))
def test_service_rejects_unknown_config_home_before_side_effects(
    tmp_path, monkeypatch, operation
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    with pytest.raises(ValueError, match="config path cannot be resolved"):
        operation(
            AppConfig(accounts=()),
            Path("~definitely-no-such-user-zzzz/config.toml"),
        )

    assert not (tmp_path / "config").exists()


def test_managed_service_config_path_ignores_unknown_user_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = _unit_directory()
    service_path = unit_dir / SERVICE_NAME
    service_path.write_text(
        "[Service]\n"
        f"{service_module.MANAGED_MARKER}\n"
        'ExecStart=/usr/bin/codex-usage --config "~definitely-no-such-user-zzzz/config.toml"\n',
        encoding="utf-8",
    )
    service_path.chmod(0o600)

    assert managed_service_config_path() is None


@pytest.mark.parametrize("operation", (service_install, service_enable))
@pytest.mark.parametrize("interval", ("60\nExecStart=bad", True, 59, 300.5, None, []))
def test_service_rejects_invalid_config_before_side_effects(
    tmp_path, monkeypatch, operation, interval
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)
    monkeypatch.setattr(
        "codex_usage.service._systemctl",
        lambda *args, check=True: subprocess.CompletedProcess(args, 0, "", ""),
    )

    with pytest.raises(ValueError, match="interval_seconds"):
        operation(AppConfig(accounts=(), interval_seconds=interval), tmp_path / "config.toml")

    assert not (tmp_path / "config").exists()


def test_service_symlink_check_rejects_dotdot_bypass(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ServiceError, match="must not contain symlinks"):
        service_module._assert_no_symlink_ancestors(redirected / ".." / "target")


def test_service_symlink_check_scans_after_missing_segment(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ServiceError, match="must not contain symlinks"):
        service_module._assert_no_symlink_ancestors(
            tmp_path / "missing" / ".." / "redirected" / "target"
        )


def test_systemctl_rejects_oversized_output_before_process_finishes(tmp_path, monkeypatch):
    marker = tmp_path / "finished"
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        "#!/bin/sh\n"
        f"{shlex.quote(sys.executable)} -c \"import os, pathlib, sys, time; "
        f"sys.stdout.write('x' * ({service_module.SYSTEMCTL_OUTPUT_MAX_BYTES} + 1)); "
        "sys.stdout.flush(); time.sleep(2); "
        "pathlib.Path(os.environ['SYSTEMCTL_MARKER']).touch()\"\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o700)
    monkeypatch.setattr(service_module.shutil, "which", lambda name: str(fake_systemctl))
    monkeypatch.setenv("SYSTEMCTL_MARKER", str(marker))

    with pytest.raises(ServiceError, match="systemctl command failed"):
        service_module._systemctl("show", "codex-usage.service", check=False)
    time.sleep(2.2)
    assert not marker.exists()


def test_systemctl_cleanup_rejects_boolean_pid(monkeypatch):
    calls = []

    class FakeProcess:
        pid = True

        def kill(self):
            calls.append("kill")

        def wait(self, timeout=None):
            calls.append(("wait", timeout))

    monkeypatch.setattr(
        "codex_usage.service.os.killpg",
        lambda pid, signum: calls.append(("killpg", pid, signum)),
    )

    _terminate_systemctl_process(FakeProcess())

    assert calls == ["kill", ("wait", 1)]


def test_systemctl_cleanup_rejects_numeric_subclass_pid(monkeypatch):
    calls = []

    class FakeProcess:
        pid = _BrokenInt(1234)

        def kill(self):
            calls.append("kill")

        def wait(self, timeout=None):
            calls.append(("wait", timeout))

    monkeypatch.setattr(
        "codex_usage.service.os.killpg",
        lambda pid, signum: calls.append(("killpg", pid, signum)),
    )

    _terminate_systemctl_process(FakeProcess())

    assert calls == ["kill", ("wait", 1)]


def test_service_enable_renders_private_hardened_units(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    auth_home = tmp_path / "agent"
    auth_home.mkdir()
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(profile_dir),
        auth_json_path=str(auth_home / "auth.json"),
        backend="app-server",
    )
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)

    def fake_systemctl(*args, check=True):
        calls.append(args)
        stdout = ""
        if args[0] == "is-enabled":
            stdout = "enabled\n"
        if args[0] == "is-active" and args[1].endswith("timer"):
            stdout = "active\n"
        if args[0] == "show" and args[1].endswith("timer"):
            stdout = (
                "SubState=waiting\n"
                "NextElapseUSecMonotonic=15h\n"
                "NextElapseUSecRealtime=\n"
            )
        elif args[0] == "show":
            stdout = (
                "Result=success\n"
                "ExecMainStatus=0\n"
                "ExecMainCode=1\n"
                "ExecMainStartTimestamp=now\n"
                "ExecMainExitTimestamp=later\n"
            )
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    result = service_enable(
        AppConfig(accounts=(account,), interval_seconds=420),
        tmp_path / "config" / "codex-usage" / "config.toml",
    )

    service_path = tmp_path / "config" / "systemd" / "user" / "codex-usage.service"
    timer_path = tmp_path / "config" / "systemd" / "user" / "codex-usage.timer"
    service = service_path.read_text(encoding="utf-8")
    timer = timer_path.read_text(encoding="utf-8")
    assert "ExecStart=" in service
    assert "Type=simple" in service
    assert "watchdog" in service
    assert "ProtectSystem=strict" in service
    assert "RuntimeMaxSec=180" in service
    assert "TimeoutStopSec=15" in service
    assert "KillMode=mixed" in service
    assert "MemoryMax=1G" in service
    assert "TasksMax=256" in service
    assert "OOMPolicy=kill" in service
    assert "Restart=no" in service
    assert f'ReadWritePaths="{profile_dir}"' in service
    assert f'ReadWritePaths="{auth_home}"' in service
    assert f'ReadWritePaths="{tmp_path / "config" / "codex-usage"}"' not in service
    assert "OnActiveSec=1min" in timer
    assert "OnBootSec=" not in timer
    assert "OnUnitActiveSec=420s" in timer
    assert oct(service_path.stat().st_mode & 0o777) == "0o600"
    assert ("enable", "codex-usage.timer") in calls
    assert ("restart", "codex-usage.timer") in calls
    assert result["installed"] is True
    assert result["enabled"] is True
    assert result["active"] is True
    assert result["timer_scheduled"] is True
    assert result["timer_substate"] == "waiting"
    assert result["service_result"] == "success"
    assert result["service_exit_status"] == "0"
    assert result["service_exit_code"] == "exited"
    assert managed_service_config_path() == (
        tmp_path / "config" / "codex-usage" / "config.toml"
    ).absolute()


def test_service_enable_removes_new_units_when_activation_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)
    calls: list[tuple[str, ...]] = []

    def fail_enable(*args, check=True):
        calls.append(args)
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 4, "not-found\n", "")
        if args[0] == "is-active":
            return subprocess.CompletedProcess(args, 0, "inactive\n", "")
        if args[0] == "enable":
            raise ServiceError("systemctl enable failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_enable)

    with pytest.raises(ServiceError, match="systemctl enable failed"):
        service_enable(AppConfig(accounts=()), tmp_path / "config.toml")

    unit_dir = tmp_path / "config" / "systemd" / "user"
    assert not (unit_dir / "codex-usage.service").exists()
    assert not (unit_dir / "codex-usage.timer").exists()
    assert ("disable", TIMER_NAME) not in calls


def test_service_enable_cleans_partial_enable_after_command_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("codex_usage.service._service_install_unlocked", lambda *_args: {})
    monkeypatch.setattr(
        "codex_usage.service._systemd_activation_snapshot",
        lambda: ("not-found", "inactive"),
    )
    cleaned = []

    def fail_after_enable_started(*args, check=True):
        if args == ("enable", TIMER_NAME):
            raise ServiceError("systemctl enable failed after link creation")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_after_enable_started)
    monkeypatch.setattr(
        "codex_usage.service._cleanup_managed_timer_enable_link",
        lambda: cleaned.append(True),
    )

    with pytest.raises(ServiceError, match="after link creation"):
        service_enable(AppConfig(accounts=()), tmp_path / "config.toml")

    assert cleaned == [True]


def test_service_install_serializes_concurrent_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)
    first_reload_entered = threading.Event()
    second_reload_entered = threading.Event()
    release_first_reload = threading.Event()
    reload_count = 0
    reload_lock = threading.Lock()

    def block_first_reload(*args, check=True):
        nonlocal reload_count
        if args == ("daemon-reload",):
            with reload_lock:
                reload_count += 1
                current_reload = reload_count
            if current_reload == 1:
                first_reload_entered.set()
                if not release_first_reload.wait(2):
                    raise AssertionError("first reload was not released")
            elif current_reload == 2:
                second_reload_entered.set()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", block_first_reload)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            service_install,
            AppConfig(accounts=(), interval_seconds=300),
            tmp_path / "config.toml",
        )
        assert first_reload_entered.wait(1)
        second = executor.submit(
            service_install,
            AppConfig(accounts=(), interval_seconds=600),
            tmp_path / "config.toml",
        )
        try:
            assert not second_reload_entered.wait(0.2)
        finally:
            release_first_reload.set()
        assert first.result() == {
            "installed": True,
            "service": SERVICE_NAME,
            "timer": TIMER_NAME,
        }
        assert second.result() == {
            "installed": True,
            "service": SERVICE_NAME,
            "timer": TIMER_NAME,
        }


def test_service_operation_lock_has_bounded_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("codex_usage.service.SERVICE_OPERATION_LOCK_TIMEOUT_SECONDS", 0)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    lock_path = unit_dir / service_module.SERVICE_OPERATION_LOCK_NAME

    def try_lock():
        with service_module._service_operation_lock():
            return True

    with service_module.private_path_lock(lock_path, timeout_seconds=0, label="held lock"):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(try_lock)
            with pytest.raises(TimeoutError, match="already in use"):
                future.result()


def test_service_enable_removes_first_install_enable_link_after_restart_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    wants_dir = unit_dir / "timers.target.wants"
    timer_path = unit_dir / TIMER_NAME
    calls: list[tuple[str, ...]] = []

    def fail_restart(*args, check=True):
        calls.append(args)
        if args == ("is-enabled", TIMER_NAME):
            return subprocess.CompletedProcess(args, 4, "not-found\n", "")
        if args == ("is-active", TIMER_NAME):
            return subprocess.CompletedProcess(args, 3, "inactive\n", "")
        if args == ("enable", TIMER_NAME):
            wants_dir.mkdir(parents=True, exist_ok=True)
            (wants_dir / TIMER_NAME).symlink_to(timer_path)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == ("disable", TIMER_NAME):
            (wants_dir / TIMER_NAME).unlink(missing_ok=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == ("restart", TIMER_NAME):
            raise ServiceError("systemctl restart failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_restart)

    with pytest.raises(ServiceError, match="systemctl restart failed"):
        service_enable(AppConfig(accounts=()), tmp_path / "config.toml")

    assert ("disable", TIMER_NAME) in calls
    assert not (wants_dir / TIMER_NAME).is_symlink()


def test_service_enable_removes_enable_link_when_disable_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    wants_dir = unit_dir / "timers.target.wants"
    timer_path = unit_dir / TIMER_NAME

    def fail_disable(*args, check=True):
        if args == ("is-enabled", TIMER_NAME):
            return subprocess.CompletedProcess(args, 4, "not-found\n", "")
        if args == ("is-active", TIMER_NAME):
            return subprocess.CompletedProcess(args, 3, "inactive\n", "")
        if args == ("enable", TIMER_NAME):
            wants_dir.mkdir(parents=True, exist_ok=True)
            (wants_dir / TIMER_NAME).symlink_to(timer_path)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == ("disable", TIMER_NAME):
            raise ServiceError("systemctl disable failed")
        if args == ("restart", TIMER_NAME):
            raise ServiceError("systemctl restart failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_disable)

    with pytest.raises(ServiceError) as exc:
        service_enable(AppConfig(accounts=()), tmp_path / "config.toml")

    assert isinstance(exc.value.__cause__, ExceptionGroup)
    assert any(str(error) == "systemctl disable failed" for error in exc.value.__cause__.exceptions)
    assert not (wants_dir / TIMER_NAME).is_symlink()


def test_cleanup_managed_timer_link_refuses_foreign_target(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    wants_dir = unit_dir / "timers.target.wants"
    wants_dir.mkdir(parents=True)
    foreign = tmp_path / "foreign.timer"
    foreign.write_text("foreign\n", encoding="utf-8")
    link = wants_dir / TIMER_NAME
    link.symlink_to(foreign)

    with pytest.raises(ServiceError, match="foreign systemd enable link"):
        service_module._cleanup_managed_timer_enable_link()

    assert link.is_symlink()


def test_cleanup_managed_timer_link_wraps_runtime_resolution_errors(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    wants_dir = unit_dir / "timers.target.wants"
    wants_dir.mkdir(parents=True)
    timer_path = unit_dir / TIMER_NAME
    timer_path.write_text("managed\n", encoding="utf-8")
    link = wants_dir / TIMER_NAME
    link.symlink_to(timer_path)
    original_resolve = service_module.Path.resolve

    def fail_link_resolution(path, strict=False):
        if path == link:
            raise RuntimeError("synthetic symlink resolution failure")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(service_module.Path, "resolve", fail_link_resolution)

    with pytest.raises(ServiceError, match="could not resolve systemd enable link"):
        service_module._cleanup_managed_timer_enable_link()

    assert link.is_symlink()


def test_cleanup_managed_timer_link_refuses_regular_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    wants_dir = tmp_path / "config" / "systemd" / "user" / "timers.target.wants"
    wants_dir.mkdir(parents=True)
    link = wants_dir / TIMER_NAME
    link.write_text("foreign\n", encoding="utf-8")

    with pytest.raises(ServiceError, match="non-symlink"):
        service_module._cleanup_managed_timer_enable_link()

    assert link.is_file()


def test_cleanup_managed_timer_link_refuses_symlinked_wants_directory(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (unit_dir / "timers.target.wants").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ServiceError, match="must not contain symlinks"):
        service_module._cleanup_managed_timer_enable_link()


def test_service_enable_restores_previous_units_when_restart_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    old_service = "old service\nX-Codex-Usage-Managed=true\n"
    old_timer = "old timer\nX-Codex-Usage-Managed=true\n"
    service_path.write_text(old_service, encoding="utf-8")
    timer_path.write_text(old_timer, encoding="utf-8")
    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)
    restart_attempts = 0

    def fail_restart(*args, check=True):
        nonlocal restart_attempts
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 0, "enabled\n", "")
        if args[0] == "is-active":
            return subprocess.CompletedProcess(args, 0, "active\n", "")
        if args[0] == "restart":
            restart_attempts += 1
            if restart_attempts == 1:
                raise ServiceError("systemctl restart failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_restart)

    with pytest.raises(ServiceError, match="systemctl restart failed"):
        service_enable(AppConfig(accounts=()), tmp_path / "config.toml")

    assert service_path.read_text(encoding="utf-8") == old_service
    assert timer_path.read_text(encoding="utf-8") == old_timer


def test_service_enable_restores_disabled_inactive_state_after_partial_enable(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name in (SERVICE_NAME, TIMER_NAME):
        (unit_dir / name).write_text(
            "old unit\nX-Codex-Usage-Managed=true\n",
            encoding="utf-8",
        )
    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)
    calls: list[tuple[str, ...]] = []

    def fail_restart(*args, check=True):
        calls.append(args)
        if args == ("is-enabled", TIMER_NAME):
            return subprocess.CompletedProcess(args, 0, "disabled\n", "")
        if args == ("is-active", TIMER_NAME):
            return subprocess.CompletedProcess(args, 0, "inactive\n", "")
        if args == ("restart", TIMER_NAME):
            raise ServiceError("systemctl restart failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_restart)

    with pytest.raises(ServiceError, match="systemctl restart failed"):
        service_enable(AppConfig(accounts=()), tmp_path / "config.toml")

    assert ("disable", TIMER_NAME) in calls
    assert ("stop", TIMER_NAME) in calls


def test_restore_systemd_activation_attempts_both_steps_after_failure(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fail_systemctl(*args, check=True):
        calls.append(args)
        raise OSError(f"{args[0]} failed")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_systemctl)

    with pytest.raises(ExceptionGroup) as exc:
        service_module._restore_systemd_activation(("disabled", "inactive"))

    assert calls == [("disable", TIMER_NAME), ("stop", TIMER_NAME)]
    assert [str(error) for error in exc.value.exceptions] == [
        "disable failed",
        "stop failed",
    ]


def test_restore_systemd_activation_reports_unknown_states(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def record_systemctl(*args, check=True):
        calls.append(args)

    monkeypatch.setattr("codex_usage.service._systemctl", record_systemctl)

    with pytest.raises(ExceptionGroup) as exc:
        service_module._restore_systemd_activation(("masked", "failed"))

    assert calls == []
    assert [str(error) for error in exc.value.exceptions] == [
        "cannot restore systemd enabled state: masked",
        "cannot restore systemd active state: failed",
    ]


def test_reject_home_write_path_wraps_runtime_resolution_errors(monkeypatch):
    def fail_resolve(_path, strict=False):
        raise RuntimeError("synthetic resolution failure")

    monkeypatch.setattr(service_module.Path, "resolve", fail_resolve)

    with pytest.raises(ServiceError, match="profile cannot be resolved"):
        service_module._reject_home_write_path(Path("/tmp/profile"), label="profile")


@pytest.mark.parametrize("field", ("profile", "auth"))
def test_service_rejects_home_as_account_writable_path(tmp_path, monkeypatch, field):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    auth_home = tmp_path / "agent"
    auth_home.mkdir()
    auth_json_path = (
        tmp_path / "auth.json" if field == "auth" else auth_home / "auth.json"
    )
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path if field == "profile" else profile_dir),
        auth_json_path=str(auth_json_path),
        backend="app-server",
    )

    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)

    with pytest.raises(
        (ServiceError, ValueError), match=r"home directory|protected directory"
    ):
        service_install(
            AppConfig(accounts=(account,)),
            tmp_path / "config" / "codex-usage" / "config.toml",
        )


def test_service_units_escape_percent_specifiers_and_restore_config_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    executable = tmp_path / "bin" / "codex%usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    profile_dir = tmp_path / "profile%user"
    profile_dir.mkdir()
    auth_home = tmp_path / "agent%user"
    auth_home.mkdir()
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(profile_dir),
        auth_json_path=str(auth_home / "auth.json"),
        backend="direct",
    )

    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)

    def fake_systemctl(*args, check=True):
        stdout = ""
        if args[0] == "is-enabled":
            stdout = "enabled\n"
        if args[0] == "is-active" and args[1].endswith("timer"):
            stdout = "active\n"
        if args[0] == "show" and args[1].endswith("timer"):
            stdout = (
                "SubState=waiting\n"
                "NextElapseUSecMonotonic=15h\n"
                "NextElapseUSecRealtime=\n"
            )
        elif args[0] == "show":
            stdout = "Result=success\nExecMainStatus=0\nExecMainCode=1\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)
    config_path = tmp_path / "config" / "codex%usage" / "config.toml"

    service_enable(AppConfig(accounts=(account,), interval_seconds=300), config_path)

    service_path = tmp_path / "config" / "systemd" / "user" / "codex-usage.service"
    service = service_path.read_text(encoding="utf-8")
    assert "%%" in service
    assert managed_service_config_path() == config_path.absolute()


def test_service_uninstall_refuses_unmanaged_unit(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    service_path.write_text("[Service]\nType=oneshot\n", encoding="utf-8")
    timer_path.write_text("[Timer]\n", encoding="utf-8")
    service_path.chmod(0o600)
    timer_path.chmod(0o600)
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    with pytest.raises(ServiceError, match="unmanaged"):
        service_uninstall()

    assert service_path.exists()
    assert timer_path.exists()
    assert calls == []


def test_service_uninstall_does_not_stop_foreign_unit_without_managed_files(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "active\n", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    assert service_uninstall() == {"installed": False, "enabled": False, "active": False}
    assert calls == []


def test_service_status_hides_foreign_state_without_managed_units(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "active\n", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    result = service_status()

    assert result["installed"] is False
    assert result["enabled"] is False
    assert result["active"] is False
    assert result["service_active"] is False
    assert calls == []


def test_service_status_rejects_elapsed_timer_without_next_run(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name in ("codex-usage.service", "codex-usage.timer"):
        (unit_dir / name).write_text(
            "[Unit]\nX-Codex-Usage-Managed=true\n",
            encoding="utf-8",
        )

    def fake_systemctl(*args, check=True):
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 0, "enabled\n", "")
        if args[0] == "is-active" and args[1].endswith("timer"):
            return subprocess.CompletedProcess(args, 0, "active\n", "")
        if args[0] == "is-active":
            return subprocess.CompletedProcess(args, 0, "inactive\n", "")
        if args[0] == "show" and args[1].endswith("timer"):
            return subprocess.CompletedProcess(
                args,
                0,
                "SubState=elapsed\nNextElapseUSecMonotonic=infinity\nNextElapseUSecRealtime=\n",
                "",
            )
        return subprocess.CompletedProcess(
            args,
            0,
            "Result=success\nExecMainStatus=0\nExecMainCode=1\n",
            "",
        )

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    result = service_status()

    assert result["active"] is False
    assert result["timer_scheduled"] is False
    assert result["timer_substate"] == "elapsed"


def test_service_disable_refuses_unmanaged_unit_without_stopping_it(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "codex-usage.timer").write_text("[Timer]\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "active\n", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    with pytest.raises(ServiceError, match="unmanaged"):
        service_disable()
    assert calls == []


def test_service_disable_skips_mutation_without_managed_units(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "active\n", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    result = service_disable()

    assert result["installed"] is False
    assert all(args[:1] != ("disable",) for args in calls)


def test_service_uninstall_keeps_units_when_disable_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    service_path.write_text("X-Codex-Usage-Managed=true\n", encoding="utf-8")
    timer_path.write_text("X-Codex-Usage-Managed=true\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    disable_attempts = 0

    def fake_systemctl(*args, check=True):
        nonlocal disable_attempts
        calls.append(args)
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 0, "disabled\n", "")
        if args[0] == "is-active":
            return subprocess.CompletedProcess(args, 0, "inactive\n", "")
        if args[0] == "disable" and check:
            disable_attempts += 1
            if disable_attempts == 1:
                raise ServiceError("systemctl disable failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    with pytest.raises(ServiceError, match="systemctl disable failed"):
        service_uninstall()

    assert service_path.exists()
    assert timer_path.exists()


def test_service_uninstall_uses_unlocked_disable_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name in (SERVICE_NAME, TIMER_NAME):
        (unit_dir / name).write_text(
            "X-Codex-Usage-Managed=true\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "codex_usage.service._service_disable_unlocked", lambda: {}
    )
    monkeypatch.setattr(
        "codex_usage.service.service_disable",
        lambda: pytest.fail("nested public service_disable call"),
    )
    monkeypatch.setattr(
        "codex_usage.service._systemd_activation_snapshot",
        lambda: ("disabled", "inactive"),
    )
    monkeypatch.setattr(
        "codex_usage.service._systemctl",
        lambda *args, check=True: subprocess.CompletedProcess(args, 0, "", ""),
    )

    assert service_uninstall() == {
        "installed": False,
        "enabled": False,
        "active": False,
    }


def test_service_uninstall_restores_units_when_timer_delete_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    old_service = "old service\nX-Codex-Usage-Managed=true\n"
    old_timer = "old timer\nX-Codex-Usage-Managed=true\n"
    service_path.write_text(old_service, encoding="utf-8")
    timer_path.write_text(old_timer, encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args, check=True):
        calls.append(args)
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 0, "enabled\n", "")
        if args[0] == "is-active":
            return subprocess.CompletedProcess(args, 0, "active\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)
    original_unlink = Path.unlink

    def fail_timer_unlink(path, *args, **kwargs):
        if path == timer_path:
            raise OSError("simulated timer delete failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_timer_unlink)

    with pytest.raises(OSError, match="simulated timer delete failure"):
        service_uninstall()

    assert service_path.read_text(encoding="utf-8") == old_service
    assert timer_path.read_text(encoding="utf-8") == old_timer
    assert ("enable", "codex-usage.timer") in calls
    assert ("start", "codex-usage.timer") in calls


def test_service_install_refuses_unmanaged_unit_without_overwriting(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    service_path.write_text("[Service]\nType=oneshot\n", encoding="utf-8")
    timer_path.write_text(
        "[Unit]\nX-Codex-Usage-Managed=true\n",
        encoding="utf-8",
    )
    service_path.chmod(0o600)
    timer_path.chmod(0o600)
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)
    monkeypatch.setattr(
        "codex_usage.service._systemctl",
        lambda *args, check=True: subprocess.CompletedProcess(args, 0, "", ""),
    )

    with pytest.raises(ServiceError, match="unmanaged"):
        service_install(AppConfig(accounts=(), interval_seconds=300), tmp_path / "config.toml")

    assert service_path.read_text(encoding="utf-8") == "[Service]\nType=oneshot\n"
    assert timer_path.read_text(encoding="utf-8") == "[Unit]\nX-Codex-Usage-Managed=true\n"


def test_service_install_rolls_back_new_units_when_timer_write_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)
    original_write = service_module.write_private_text

    def fail_timer_write(path, text, *, label, mode=0o600):
        if path.name == "codex-usage.timer":
            raise OSError("simulated timer write failure")
        return original_write(path, text, label=label, mode=mode)

    monkeypatch.setattr("codex_usage.service.write_private_text", fail_timer_write)

    with pytest.raises(OSError, match="simulated timer write failure"):
        service_install(AppConfig(accounts=(), interval_seconds=300), tmp_path / "config.toml")

    unit_dir = tmp_path / "config" / "systemd" / "user"
    assert not (unit_dir / "codex-usage.service").exists()
    assert not (unit_dir / "codex-usage.timer").exists()


def test_service_install_restores_existing_units_when_timer_write_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    old_service = "old service\nX-Codex-Usage-Managed=true\n"
    old_timer = "old timer\nX-Codex-Usage-Managed=true\n"
    service_path.write_text(old_service, encoding="utf-8")
    timer_path.write_text(old_timer, encoding="utf-8")
    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)
    original_write = service_module.write_private_text
    timer_attempts = 0

    def fail_after_timer_write(path, text, *, label, mode=0o600):
        nonlocal timer_attempts
        original_write(path, text, label=label, mode=mode)
        if path.name == "codex-usage.timer" and timer_attempts == 0:
            timer_attempts += 1
            raise OSError("simulated timer fsync failure")

    monkeypatch.setattr("codex_usage.service.write_private_text", fail_after_timer_write)

    with pytest.raises(OSError, match="simulated timer fsync failure"):
        service_install(AppConfig(accounts=(), interval_seconds=300), tmp_path / "config.toml")

    assert service_path.read_text(encoding="utf-8") == old_service
    assert timer_path.read_text(encoding="utf-8") == old_timer


def test_service_install_aggregates_install_and_restore_failures(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    service_path.write_text("old service\nX-Codex-Usage-Managed=true\n", encoding="utf-8")
    timer_path.write_text("old timer\nX-Codex-Usage-Managed=true\n", encoding="utf-8")
    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)
    original_write = service_module.write_private_text
    timer_attempts = 0

    def fail_timer_write(path, text, *, label, mode=0o600):
        nonlocal timer_attempts
        if path.name == "codex-usage.timer":
            timer_attempts += 1
            if timer_attempts == 1:
                raise OSError("timer write failed")
            raise OSError("timer restore failed")
        return original_write(path, text, label=label, mode=mode)

    monkeypatch.setattr("codex_usage.service.write_private_text", fail_timer_write)

    with pytest.raises(ServiceError) as exc:
        service_install(AppConfig(accounts=(), interval_seconds=300), tmp_path / "config.toml")

    assert isinstance(exc.value.__cause__, ExceptionGroup)
    assert [str(error) for error in exc.value.__cause__.exceptions] == [
        "timer write failed",
        "timer restore failed",
    ]


def test_service_install_preserves_both_unit_restore_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    service_path.write_text("old service\nX-Codex-Usage-Managed=true\n", encoding="utf-8")
    timer_path.write_text("old timer\nX-Codex-Usage-Managed=true\n", encoding="utf-8")
    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)
    original_write = service_module.write_private_text
    service_writes = 0
    timer_writes = 0

    def fail_both_restores(path, text, *, label, mode=0o600):
        nonlocal service_writes, timer_writes
        if path == service_path:
            service_writes += 1
            if service_writes > 1:
                raise OSError("service restore failed")
        if path == timer_path:
            timer_writes += 1
            if timer_writes == 1:
                raise OSError("timer write failed")
            raise OSError("timer restore failed")
        return original_write(path, text, label=label, mode=mode)

    monkeypatch.setattr("codex_usage.service.write_private_text", fail_both_restores)

    with pytest.raises(ServiceError) as exc:
        service_install(AppConfig(accounts=(), interval_seconds=300), tmp_path / "config.toml")

    rollback_errors = exc.value.__cause__.exceptions
    assert [str(error) for error in rollback_errors[0:1]] == ["timer write failed"]
    assert isinstance(rollback_errors[1], ExceptionGroup)
    assert [str(error) for error in rollback_errors[1].exceptions] == [
        "service restore failed",
        "timer restore failed",
    ]


def test_service_enable_aggregates_all_activation_rollback_failures(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("codex_usage.service._service_install_unlocked", lambda *_args: {})
    monkeypatch.setattr(
        "codex_usage.service.service_install",
        lambda *_args: pytest.fail("nested public service_install call"),
    )
    monkeypatch.setattr(
        "codex_usage.service._systemd_activation_snapshot",
        lambda: ("enabled", "active"),
    )

    def fail_systemctl(*args, check=True):
        if args == ("enable", TIMER_NAME):
            raise OSError("activation enable failed")
        raise OSError(f"rollback systemctl failed: {args[0]}")

    def fail_unit_restore(*_args):
        raise OSError("unit restore failed")

    def fail_activation_restore(*_args):
        raise OSError("activation restore failed")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_systemctl)
    monkeypatch.setattr("codex_usage.service._restore_unit_snapshot", fail_unit_restore)
    monkeypatch.setattr(
        "codex_usage.service._restore_systemd_activation", fail_activation_restore
    )

    with pytest.raises(ServiceError) as exc:
        service_enable(AppConfig(accounts=()), tmp_path / "config.toml")

    assert isinstance(exc.value.__cause__, ExceptionGroup)
    assert [str(error) for error in exc.value.__cause__.exceptions] == [
        "activation enable failed",
        "unit restore failed",
        "rollback systemctl failed: daemon-reload",
        "activation restore failed",
    ]


def test_service_uninstall_aggregates_all_rollback_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name in ("codex-usage.service", "codex-usage.timer"):
        (unit_dir / name).write_text(
            "X-Codex-Usage-Managed=true\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "codex_usage.service._systemd_activation_snapshot",
        lambda: ("enabled", "active"),
    )

    def fail_systemctl(*args, check=True):
        if args == ("disable", "--now", TIMER_NAME):
            raise OSError("service disable failed")
        raise OSError(f"rollback systemctl failed: {args[0]}")

    def fail_unit_restore(*_args):
        raise OSError("unit restore failed")

    def fail_activation_restore(*_args):
        raise OSError("activation restore failed")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_systemctl)
    monkeypatch.setattr("codex_usage.service._restore_unit_snapshot", fail_unit_restore)
    monkeypatch.setattr(
        "codex_usage.service._restore_systemd_activation", fail_activation_restore
    )

    with pytest.raises(ServiceError) as exc:
        service_uninstall()

    assert isinstance(exc.value.__cause__, ExceptionGroup)
    assert [str(error) for error in exc.value.__cause__.exceptions] == [
        "service disable failed",
        "unit restore failed",
        "rollback systemctl failed: daemon-reload",
        "activation restore failed",
    ]


def test_service_install_reloads_systemd_after_daemon_reload_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    old_service = "old service\nX-Codex-Usage-Managed=true\n"
    old_timer = "old timer\nX-Codex-Usage-Managed=true\n"
    service_path.write_text(old_service, encoding="utf-8")
    timer_path.write_text(old_timer, encoding="utf-8")
    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)
    reload_calls = 0

    def fail_once(*args, check=True):
        nonlocal reload_calls
        if args == ("daemon-reload",):
            reload_calls += 1
            if reload_calls == 1:
                raise ServiceError("systemctl daemon-reload failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_once)

    with pytest.raises(ServiceError, match="systemctl daemon-reload failed"):
        service_install(AppConfig(accounts=(), interval_seconds=300), tmp_path / "config.toml")

    assert reload_calls == 2
    assert service_path.read_text(encoding="utf-8") == old_service
    assert timer_path.read_text(encoding="utf-8") == old_timer


def test_service_install_restricts_existing_unit_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit_dir.chmod(0o755)
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)
    monkeypatch.setattr(
        "codex_usage.service._systemctl",
        lambda *args, check=True: subprocess.CompletedProcess(args, 0, "", ""),
    )

    service_install(AppConfig(accounts=()), tmp_path / "config.toml")

    assert oct(unit_dir.stat().st_mode & 0o777) == "0o700"


def test_unit_directory_binds_mode_change_to_existing_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, mode=0o755)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    original_chmod = Path.chmod

    def replace_target_before_path_chmod(path, mode):
        if path == unit_dir:
            unit_dir.rmdir()
            unit_dir.symlink_to(outside, target_is_directory=True)
        return original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", replace_target_before_path_chmod)
    _unit_directory(create=False)

    assert unit_dir.is_dir() and not unit_dir.is_symlink()
    assert unit_dir.stat().st_mode & 0o777 == 0o700
    assert outside.stat().st_mode & 0o777 == 0o755


def test_service_install_rejects_symlinked_config_home(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    config_home = tmp_path / "config"
    config_home.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    with pytest.raises(ServiceError, match="must not contain symlinks"):
        service_install(AppConfig(accounts=()), tmp_path / "config.toml")

    assert not (outside / "systemd" / "user").exists()
