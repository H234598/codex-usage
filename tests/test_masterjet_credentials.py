from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

import codex_usage.masterjet_credentials as credentials_module
from codex_usage.masterjet_credentials import (
    MasterjetCredentialsError,
    bearer_provider_from_fd,
    bearer_provider_from_systemd_credentials,
    stdin_step_up_provider,
)


def _credential(tmp_path: Path, payload: bytes = b"remote-bearer") -> Path:
    directory = tmp_path / "credentials"
    directory.mkdir(mode=0o700)
    path = directory / "masterjet-control-bearer"
    path.write_bytes(payload)
    path.chmod(0o400)
    return path


def test_systemd_bearer_is_bounded_private_and_one_shot(tmp_path: Path) -> None:
    path = _credential(tmp_path)

    provider = bearer_provider_from_systemd_credentials(
        environ={"CREDENTIALS_DIRECTORY": str(path.parent)}
    )

    assert provider() == "remote-bearer"
    with pytest.raises(MasterjetCredentialsError):
        provider()


@pytest.mark.parametrize("case", ["symlink", "hardlink", "mode", "oversize"])
def test_systemd_bearer_rejects_unsafe_credential_files(tmp_path: Path, case: str) -> None:
    path = _credential(tmp_path)
    if case == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"remote-bearer")
        target.chmod(0o400)
        path.unlink()
        path.symlink_to(target)
    elif case == "hardlink":
        os.link(path, tmp_path / "linked")
    elif case == "mode":
        path.chmod(0o440)
    else:
        path.chmod(0o600)
        path.write_bytes(b"x" * 4097)
        path.chmod(0o400)

    provider = bearer_provider_from_systemd_credentials(
        environ={"CREDENTIALS_DIRECTORY": str(path.parent)}
    )

    with pytest.raises(MasterjetCredentialsError):
        provider()


def test_injected_bearer_fd_is_private_and_one_shot(tmp_path: Path) -> None:
    path = _credential(tmp_path)
    fd = os.open(path, os.O_RDONLY)
    try:
        provider = bearer_provider_from_fd(fd)
        assert provider() == "remote-bearer"
        with pytest.raises(MasterjetCredentialsError):
            provider()
    finally:
        os.close(fd)


def test_systemd_bearer_rejects_wrong_owner(tmp_path: Path, monkeypatch) -> None:
    path = _credential(tmp_path)
    actual_uid = os.geteuid()
    monkeypatch.setattr(credentials_module.os, "geteuid", lambda: actual_uid + 1)
    provider = bearer_provider_from_systemd_credentials(
        environ={"CREDENTIALS_DIRECTORY": str(path.parent)}
    )

    with pytest.raises(MasterjetCredentialsError):
        provider()


def test_step_up_stdin_is_not_read_until_challenge_and_is_one_shot() -> None:
    stream = io.BytesIO(b"739104\n")
    provider = stdin_step_up_provider(stream)

    assert stream.tell() == 0
    assert provider() == "739104"
    with pytest.raises(MasterjetCredentialsError):
        provider()
