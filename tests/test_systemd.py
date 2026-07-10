from __future__ import annotations

from pathlib import Path


def test_watchdog_service_runs_watchdog_command_with_hardening():
    service = Path("systemd/codex-usage.service").read_text(encoding="utf-8")

    assert "ExecStart=%h/.local/bin/codex-usage watchdog --format table" in service
    assert "ProtectClock=true" in service
    assert "ProtectHostname=true" in service
    assert "ProtectHome=read-only" in service
    assert "Type=simple" in service
    assert "NoNewPrivileges=true" in service
    assert "PrivateTmp=true" in service
    assert "RuntimeMaxSec=180" in service
    assert "TimeoutStopSec=15" in service
    assert "KillMode=mixed" in service
    assert "MemoryMax=1G" in service
    assert "TasksMax=256" in service
    assert "OOMPolicy=kill" in service
    assert "Restart=no" in service
