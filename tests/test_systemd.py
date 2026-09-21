from __future__ import annotations

from pathlib import Path

import codex_usage.private_io as private_io
import codex_usage.service as service_module
from codex_usage.config import AppConfig

EXPECTED_INTEGRATION_WATCHDOG_SYSTEMD_TIMEOUT_SECONDS = 270


def test_service_runs_dedicated_integration_watchdog_with_hardening():
    service = Path("systemd/codex-usage.service").read_text(encoding="utf-8")

    assert (
        'ExecStart="%h/.local/share/codex-usage-service-runtime-v2/current/bin/'
        'codex-usage-integration-watchdog-v2"'
    ) in service
    assert "--config" not in service
    assert "ms-playwright" not in service
    assert " codex-usage watchdog " not in service
    assert 'Environment="XDG_DATA_HOME=%h/.local/share"' in service
    assert 'Environment="XDG_STATE_HOME=%h/.local/state"' in service
    assert "Environment=PYTHONSAFEPATH=1" in service
    assert "Environment=PYTHONNOUSERSITE=1" in service
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in service
    assert "UnsetEnvironment=" in service
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONEXECUTABLE",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
    ):
        assert name in service
    assert 'ReadWritePaths="%h/.local/state/codex-usage/integration"' in service
    assert "ProtectClock=true" in service
    assert "ProtectHostname=true" in service
    assert "ProtectHome=read-only" in service
    assert "Type=oneshot" in service
    assert "NoNewPrivileges=true" in service
    assert "PrivateTmp=true" in service
    assert (
        f"TimeoutStartSec={EXPECTED_INTEGRATION_WATCHDOG_SYSTEMD_TIMEOUT_SECONDS}"
        in service
    )
    assert "RuntimeMaxSec=" not in service
    assert "TimeoutStopSec=15" in service
    assert "KillMode=mixed" in service
    assert "MemoryMax=1G" in service
    assert "TasksMax=256" in service
    assert "OOMPolicy=kill" in service
    assert "Restart=no" in service


def test_static_and_rendered_service_share_integration_watchdog_contract(
    tmp_path,
    monkeypatch,
):
    """Static sample and installer renderer must not drift on the unit contract."""
    home = tmp_path / "home"
    data_home = home / ".local" / "share"
    state_home = home / ".local" / "state"
    lock_root = state_home / "codex-usage" / "locks"
    watchdog = (
        data_home
        / "codex-usage-service-runtime-v2"
        / "current"
        / "bin"
        / "codex-usage-integration-watchdog-v2"
    )
    config_path = home / ".config" / "codex-usage" / "config.toml"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)

    static = Path("systemd/codex-usage.service").read_text(encoding="utf-8")
    rendered = service_module._render_service(
        AppConfig(accounts=()),
        watchdog,
        config_path,
    )
    static_normalized = static.replace("%h", str(home))

    assert static_normalized == rendered
