from __future__ import annotations

import subprocess

import pytest

from codex_usage.config import AppConfig
from codex_usage.models import Account
from codex_usage.service import (
    ServiceError,
    managed_service_config_path,
    service_disable,
    service_enable,
    service_install,
    service_uninstall,
)


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
        if args[0] == "show":
            stdout = (
                "Result=success\n"
                "ExecMainStatus=0\n"
                "ExecMainCode=exited\n"
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
    assert "OnUnitActiveSec=420s" in timer
    assert oct(service_path.stat().st_mode & 0o777) == "0o600"
    assert ("enable", "codex-usage.timer") in calls
    assert ("restart", "codex-usage.timer") in calls
    assert result["installed"] is True
    assert result["enabled"] is True
    assert result["active"] is True
    assert result["service_result"] == "success"
    assert result["service_exit_status"] == "0"
    assert managed_service_config_path() == (
        tmp_path / "config" / "codex-usage" / "config.toml"
    ).absolute()


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

    def fake_systemctl(*args, check=True):
        calls.append(args)
        if args[0] == "disable" and check:
            raise ServiceError("systemctl disable failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    with pytest.raises(ServiceError, match="systemctl disable failed"):
        service_uninstall()

    assert service_path.exists()
    assert timer_path.exists()


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


def test_service_install_rejects_symlinked_config_home(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    config_home = tmp_path / "config"
    config_home.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    with pytest.raises(ServiceError, match="must not contain symlinks"):
        service_install(AppConfig(accounts=()), tmp_path / "config.toml")

    assert not (outside / "systemd" / "user").exists()
