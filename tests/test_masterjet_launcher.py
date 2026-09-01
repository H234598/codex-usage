from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import codex_usage.config as config_module
import codex_usage.masterjet_launcher as launcher_module
from codex_usage.masterjet_launcher import (
    masterjet_launcher_argv,
    settings_masterjet_launcher_argv,
)

_REAL_SYSTEMD_GATE = launcher_module._verify_systemd_user_credentials


@pytest.fixture(autouse=True)
def _available_systemd_user_credentials(monkeypatch):
    monkeypatch.setattr(launcher_module, "_verify_systemd_user_credentials", lambda: None)


def _install_credentials(home: Path) -> tuple[Path, Path]:
    home.mkdir(mode=0o700)
    config = home / ".config"
    config.mkdir(mode=0o700)
    app_config = config / "codex-usage"
    app_config.mkdir(mode=0o700)
    directory = home / ".config" / "codex-usage" / "credentials"
    directory.mkdir(mode=0o700)
    for name, value in (
        ("masterjet-control-bearer.cred", b"encrypted-remote-bearer"),
        ("masterjet-local-attestation-key.cred", b"encrypted-attestation-key"),
    ):
        path = directory / name
        path.write_bytes(value)
        path.chmod(0o400)
    executable = home / ".local" / "bin" / "codex-usage"
    executable.parent.mkdir(parents=True, mode=0o700)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    executable.chmod(0o700)
    return directory, executable


def test_launcher_builds_transient_user_service_with_fixed_credentials(tmp_path: Path) -> None:
    home = tmp_path / "home"
    credentials, executable = _install_credentials(home)
    command = (os.fspath(executable), "masterjet", "openai-accounts", "--json")

    result = masterjet_launcher_argv(command, transport="https", home=home)

    assert result == (
        "/usr/bin/systemd-run",
        "--user",
        "--quiet",
        "--pipe",
        "--wait",
        "--collect",
        "--service-type=exec",
        "--property=UMask=0077",
        "--property=LoadCredentialEncrypted=masterjet-control-bearer:"
        + os.fspath(credentials / "masterjet-control-bearer.cred"),
        "--",
        *command,
    )


def test_launcher_fails_closed_for_missing_credential_or_unlisted_command(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    credentials, executable_path = _install_credentials(home)
    executable = os.fspath(executable_path)
    (credentials / "masterjet-control-bearer.cred").unlink()

    with pytest.raises(ValueError, match=r"control\.authentication_required"):
        masterjet_launcher_argv(
            (executable, "masterjet", "connection-test", "--json"),
            transport="https",
            home=home,
        )

    (credentials / "masterjet-control-bearer.cred").write_bytes(b"encrypted")
    (credentials / "masterjet-control-bearer.cred").chmod(0o400)
    with pytest.raises(ValueError, match=r"control\.request_invalid"):
        masterjet_launcher_argv(
            (executable, "shell", "--json"), transport="https", home=home
        )


def test_launcher_rejects_secret_source_symlink_and_ambient_credential_directory(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    credentials, executable_path = _install_credentials(home)
    target = credentials / "bearer-target"
    target.write_bytes(b"encrypted")
    target.chmod(0o400)
    bearer = credentials / "masterjet-control-bearer.cred"
    bearer.unlink()
    bearer.symlink_to(target)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/private/ambient-marker")
    executable = os.fspath(executable_path)

    with pytest.raises(ValueError, match=r"control\.authentication_required"):
        masterjet_launcher_argv(
            (executable, "google", "accounts", "--json"),
            transport="https",
            home=home,
        )


def test_local_launcher_loads_only_attestation_key(tmp_path: Path) -> None:
    home = tmp_path / "home"
    credentials, executable = _install_credentials(home)

    result = masterjet_launcher_argv(
        (os.fspath(executable), "google", "accounts", "--json"),
        transport="local",
        home=home,
    )

    properties = [value for value in result if value.startswith("--property=LoadCredential")]
    assert properties == [
        "--property=LoadCredentialEncrypted=masterjet-local-attestation-key:"
        + os.fspath(credentials / "masterjet-local-attestation-key.cred")
    ]


def test_settings_boundary_selects_nonsecret_config_transport(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    credentials, executable = _install_credentials(home)
    monkeypatch.setattr(launcher_module, "_account_home", lambda: home)
    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: SimpleNamespace(masterjet=SimpleNamespace(transport="https")),
    )
    command = (os.fspath(executable), "google", "accounts", "--json")

    result = settings_masterjet_launcher_argv(command)

    properties = [value for value in result if value.startswith("--property=LoadCredential")]
    assert properties == [
        "--property=LoadCredentialEncrypted=masterjet-control-bearer:"
        + os.fspath(credentials / "masterjet-control-bearer.cred")
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ("masterjet", "openai-accounts", "--json"),
        ("masterjet", "connection-test", "--json"),
        ("google", "accounts", "--json"),
        (
            "--step-up-stdin",
            "account",
            "auth-sync",
            "openai-one",
            "--format",
            "json",
        ),
        (
            "--step-up-stdin",
            "google",
            "oauth-begin",
            "google-one",
            "--browser",
            "firefox",
            "--json",
        ),
        (
            "--step-up-stdin",
            "google",
            "add",
            "google-one",
            "--oauth-client-json",
            "/private/oauth-client.json",
            "--json",
        ),
        ("--step-up-stdin", "google", "inventory-refresh", "google-one", "--json"),
        ("--step-up-stdin", "google", "provision-plan", "google-one", "--json"),
        (
            "--step-up-stdin",
            "google",
            "provision-apply",
            "google-one",
            "plan-one",
            "--plan-digest",
            "sha256:" + "a" * 64,
            "--confirm",
            "--json",
        ),
    ],
)
def test_launcher_allowlist_covers_settings_control_operations(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    home = tmp_path / "home"
    _credentials, executable = _install_credentials(home)

    result = masterjet_launcher_argv(
        (os.fspath(executable), *arguments), transport="https", home=home
    )

    assert result[-len(arguments) :] == arguments


def test_systemd_capability_gate_rejects_version_without_encrypted_credentials(
    monkeypatch,
) -> None:
    results = iter(
        (
            SimpleNamespace(returncode=0, stdout=b"systemd 246\n"),
            SimpleNamespace(returncode=0, stdout=b""),
        )
    )
    monkeypatch.setattr(launcher_module.subprocess, "run", lambda *_args, **_kwargs: next(results))
    _REAL_SYSTEMD_GATE.cache_clear()
    try:
        with pytest.raises(ValueError, match=r"control\.transport_unavailable"):
            _REAL_SYSTEMD_GATE()
    finally:
        _REAL_SYSTEMD_GATE.cache_clear()
