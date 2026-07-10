from __future__ import annotations

import subprocess

import pytest

from codex_usage.config import AppConfig
from codex_usage.models import Account
from codex_usage.service import ServiceError, service_enable, service_uninstall


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
    assert ("enable", "--now", "codex-usage.timer") in calls
    assert result["installed"] is True
    assert result["enabled"] is True
    assert result["active"] is True
    assert result["service_result"] == "success"
    assert result["service_exit_status"] == "0"


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
    monkeypatch.setattr(
        "codex_usage.service._systemctl",
        lambda *args, check=True: subprocess.CompletedProcess(args, 0, "", ""),
    )

    with pytest.raises(ServiceError, match="unmanaged"):
        service_uninstall()

    assert service_path.exists()
    assert timer_path.exists()
