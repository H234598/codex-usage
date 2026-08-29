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
    directory.mkdir(mode=0o700, parents=True)
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


def test_step_up_stdin_is_not_read_until_each_same_process_challenge() -> None:
    stream = io.BytesIO(b"739104\n182736\n")
    provider = stdin_step_up_provider(stream)

    assert stream.tell() == 0
    assert provider() == "739104"
    assert provider() == "182736"


def test_systemd_credential_directory_is_snapshotted_at_provider_creation(tmp_path: Path) -> None:
    first = _credential(tmp_path / "first", b"first-bearer")
    second = _credential(tmp_path / "second", b"second-bearer")
    environment = {"CREDENTIALS_DIRECTORY": str(first.parent)}
    provider = bearer_provider_from_systemd_credentials(environ=environment)
    environment["CREDENTIALS_DIRECTORY"] = str(second.parent)

    assert provider() == "first-bearer"


def test_injected_fd_owns_duplicate_after_original_close_and_reuse(tmp_path: Path) -> None:
    first = _credential(tmp_path / "first", b"first-bearer")
    second = _credential(tmp_path / "second", b"second-bearer")
    original = os.open(first, os.O_RDONLY)
    provider = bearer_provider_from_fd(original)
    os.close(original)
    replacement = os.open(second, os.O_RDONLY)
    try:
        assert provider() == "first-bearer"
    finally:
        os.close(replacement)


def test_injected_fd_provider_preserves_caller_offset_and_closes_duplicate(tmp_path: Path, monkeypatch) -> None:
    path = _credential(tmp_path, b"remote-bearer")
    original = os.open(path, os.O_RDONLY)
    duplicate = []
    real_dup = credentials_module.os.dup

    def remember(fd: int) -> int:
        value = real_dup(fd)
        duplicate.append(value)
        return value

    monkeypatch.setattr(credentials_module.os, "dup", remember)
    os.lseek(original, 3, os.SEEK_SET)
    provider = bearer_provider_from_fd(original)
    try:
        assert provider() == "remote-bearer"
        assert os.lseek(original, 0, os.SEEK_CUR) == 3
        with pytest.raises(OSError):
            os.fstat(duplicate[0])
    finally:
        os.close(original)


def test_step_up_newline_is_removed_in_place() -> None:
    payload = bytearray(b"739104\n")

    assert credentials_module._step_up_from_bytes(payload) == "739104"
    assert payload == b"739104"


def test_systemd_credential_read_stays_bound_to_open_ancestor_during_swap(
    tmp_path: Path, monkeypatch
) -> None:
    active = tmp_path / "active"
    trusted = _credential(active, b"trusted-bearer")
    attacker = tmp_path / "attacker"
    _credential(attacker, b"attacker-bearer")
    parked = tmp_path / "parked"
    provider = bearer_provider_from_systemd_credentials(
        environ={"CREDENTIALS_DIRECTORY": str(trusted.parent)}
    )
    real_open = credentials_module.os.open
    swapped = False

    def swap_before_credential_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and (
            (path == "credentials" and dir_fd is not None)
            or Path(path) == trusted.parent
        ):
            active.rename(parked)
            active.symlink_to(attacker, target_is_directory=True)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(credentials_module.os, "open", swap_before_credential_open)

    assert provider() == "trusted-bearer"
    assert swapped is True


def test_injected_fd_final_stat_mutation_wipes_payload_and_closes_duplicate(
    tmp_path: Path, monkeypatch
) -> None:
    path = _credential(tmp_path, b"remote-bearer")
    original = os.open(path, os.O_RDONLY)
    duplicate = []
    observed = []
    real_dup = credentials_module.os.dup
    real_pread = credentials_module.os.pread

    class ObservedBytearray(bytearray):
        def __setitem__(self, key, value):
            if isinstance(key, slice):
                observed.append(("wipe", bytes(value)))
            return super().__setitem__(key, value)

        def clear(self):
            observed.append(("clear", bytes(self)))
            return super().clear()

    def remember_dup(fd: int) -> int:
        value = real_dup(fd)
        duplicate.append(value)
        return value

    def mutate_after_read(fd: int, maximum: int, offset: int) -> bytes:
        payload = real_pread(fd, maximum, offset)
        path.chmod(0o600)
        path.write_bytes(b"changed-bearer")
        path.chmod(0o400)
        return payload

    monkeypatch.setattr(credentials_module.os, "dup", remember_dup)
    monkeypatch.setattr(credentials_module.os, "pread", mutate_after_read)
    monkeypatch.setattr(credentials_module, "bytearray", ObservedBytearray, raising=False)
    provider = bearer_provider_from_fd(original)
    try:
        with pytest.raises(MasterjetCredentialsError, match="credential unavailable"):
            provider()
        assert observed == [
            ("wipe", b"\x00" * len(b"remote-bearer")),
            ("clear", b"\x00" * len(b"remote-bearer")),
        ]
        with pytest.raises(OSError):
            os.fstat(duplicate[0])
        assert os.fstat(original).st_ino == path.stat().st_ino
    finally:
        os.close(original)
