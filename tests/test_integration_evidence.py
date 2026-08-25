from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

_SOURCE_FILES = (
    "pyproject.toml",
    "src/codex_usage/__init__.py",
    "src/codex_usage/account_lock.py",
    "src/codex_usage/config.py",
    "src/codex_usage/consumption.py",
    "src/codex_usage/extractor.py",
    "src/codex_usage/integration_attestation.py",
    "src/codex_usage/integration_entrypoint.py",
    "src/codex_usage/integration_snapshot.py",
    "src/codex_usage/json_utils.py",
    "src/codex_usage/models.py",
    "src/codex_usage/history.py",
    "src/codex_usage/private_io.py",
    "src/codex_usage/state.py",
    "src/codex_usage/usage_limits.py",
    "src/codex_usage/usage_resets.py",
)


def _source_copy(tmp_path: Path) -> Path:
    project_root = Path(__file__).resolve().parents[1]
    source_root = tmp_path / "source"
    source_root.mkdir(mode=0o700)
    for relative_text in _SOURCE_FILES:
        source = project_root / relative_text
        destination = source_root / relative_text
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.parent.chmod(0o700)
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
    return source_root


def _bootstrap_test_lock_inodes(state_home: Path) -> None:
    from codex_usage import integration_evidence, private_io

    lock_root = private_io._private_lock_root()
    private_io.ensure_private_directory(lock_root, label="test evidence lock root")
    for target in (
        state_home / "codex-usage" / "integration" / "producer-install",
        state_home / "codex-usage" / "integration" / "current.json",
    ):
        lock_path = lock_root / integration_evidence._evidence_lock_name(target)
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            item = lock_path.lstat()
            assert stat.S_ISREG(item.st_mode)
            assert item.st_uid == os.getuid()
            assert stat.S_IMODE(item.st_mode) == 0o600
            assert item.st_nlink == 1
            assert item.st_size == 0
            continue
        os.close(fd)


@pytest.fixture
def evidence_layout(tmp_path):
    from codex_usage.integration_attestation import verify_active_manifest_at
    from codex_usage.integration_installer import install_release

    state_home = tmp_path / "state"
    data_home = tmp_path / "data"
    temporary_root = tmp_path / "temporary"
    for path in (state_home, data_home, temporary_root):
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    release = install_release(
        source_root=_source_copy(tmp_path),
        state_home=state_home,
        data_home=data_home,
        python_executable=Path(sys.executable),
        temporary_root=temporary_root,
    )
    _bootstrap_test_lock_inodes(state_home)
    payload = (
        json.dumps(
            {
                "accounts": [],
                "generated_at": "2026-08-25T10:00:00Z",
                "schema_version": 2,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    verified = verify_active_manifest_at(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=release.entrypoint_path,
    )
    return state_home, data_home, release.entrypoint_path, payload, verified


def test_fd_private_io_round_trip_and_identity(tmp_path):
    from codex_usage.private_io import (
        open_private_dir_at,
        open_verified_state_home,
        read_private_bytes_at,
        write_private_bytes_at,
    )

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    child = state_home / "child"
    child.mkdir(mode=0o700)
    state_fd = open_verified_state_home(state_home)
    child_fd = -1
    try:
        child_fd = open_private_dir_at(state_fd, "child")
        written = write_private_bytes_at(child_fd, "value.json", b"{}\n", mode=0o600)
        payload, read = read_private_bytes_at(
            child_fd,
            "value.json",
            maximum=3,
            mode=0o600,
        )
    finally:
        if child_fd >= 0:
            os.close(child_fd)
        os.close(state_fd)
    assert payload == b"{}\n"
    assert read == written
    item = (child / "value.json").lstat()
    assert stat.S_IMODE(item.st_mode) == 0o600


def test_fd_private_write_cleanup_does_not_unlink_replacement(tmp_path, monkeypatch):
    from codex_usage import private_io

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    child = state_home / "child"
    child.mkdir(mode=0o700)
    state_fd = private_io.open_verified_state_home(state_home)
    child_fd = private_io.open_private_dir_at(state_fd, "child")
    value = child / "value.json"
    old = child / "owned-old.json"
    swapped = False

    def swap_then_fail(_fd):
        nonlocal swapped
        if not swapped:
            swapped = True
            os.rename(value, old)
            value.write_bytes(b"replacement\n")
            value.chmod(0o600)
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(private_io.os, "fsync", swap_then_fail)
    try:
        with pytest.raises(OSError, match="synthetic fsync failure"):
            private_io.write_private_bytes_at(
                child_fd,
                "value.json",
                b"owned\n",
                mode=0o600,
            )
    finally:
        os.close(child_fd)
        os.close(state_fd)
    assert old.read_bytes() == b"owned\n"
    assert value.read_bytes() == b"replacement\n"


def test_verify_active_manifest_at_hashes_exact_active_bytes(evidence_layout):
    state_home, _data_home, _entrypoint, _payload, verified = evidence_layout
    active = state_home / "codex-usage" / "integration" / "active.json"
    assert verified.active_manifest_bytes == active.read_bytes()
    assert verified.active_manifest_sha256 == hashlib.sha256(active.read_bytes()).hexdigest()


def test_binding_requires_exact_ten_fields_and_32kib_limit():
    """Would fail if binding parser accepted missing, extra, or oversized bytes."""
    from codex_usage import integration_evidence
    from codex_usage.integration_evidence import EvidenceBinding
    from codex_usage.private_io import IntegrationEvidenceInvalid

    binding = EvidenceBinding(
        active_manifest_sha256="a" * 64,
        binding_schema_version=1,
        generation_id="b" * 32,
        payload_filename="account-usage-v2.json",
        payload_sha256="c" * 64,
        payload_size_bytes=64,
        published_at="2026-08-25T10:00:00Z",
        producer_version="0.6.536",
        release_id="0.6.536-" + "d" * 16,
        source_manifest_sha256="e" * 64,
    )

    assert integration_evidence.parse_binding(
        integration_evidence.serialize_binding(binding)
    ) == binding
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.parse_binding(b"{" + b"x" * 32768 + b"}")


def test_pointer_rejects_half_previous_pair_and_equal_generations():
    """Would fail if pointer parser allowed unusable rollback state."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    pointer = {
        "current_binding_sha256": "a" * 64,
        "current_generation_id": "b" * 32,
        "pointer_schema_version": 1,
        "previous_binding_sha256": None,
        "previous_generation_id": None,
    }
    pointer["previous_generation_id"] = "c" * 32
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.parse_pointer(
            json.dumps(pointer, sort_keys=True, separators=(",", ":")).encode("ascii")
        )
    pointer["previous_binding_sha256"] = "d" * 64
    pointer["previous_generation_id"] = pointer["current_generation_id"]
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.parse_pointer(
            json.dumps(pointer, sort_keys=True, separators=(",", ":")).encode("ascii")
        )


def test_v2_payload_rejects_2097153_bytes():
    """Would fail if payload boundary admitted more than two MiB."""
    from codex_usage import integration_evidence
    from codex_usage.integration_snapshot import IntegrationInvalidSource

    with pytest.raises(IntegrationInvalidSource):
        integration_evidence.validate_v2_payload_bytes(b"x" * 2_097_153)


def test_v2_contract_allows_only_three_windows():
    """Would fail if a non-approved quota window entered V2 validation."""
    from codex_usage import integration_evidence

    assert integration_evidence.ALLOWED_WINDOW_SECONDS == frozenset(
        (18_000, 604_800, 2_592_000)
    )


def test_pointer_positional_constructor_round_trips_canonical_fields():
    """Would fail if positional interface swapped digest and generation values."""
    from codex_usage import integration_evidence
    from codex_usage.integration_evidence import EvidencePointer

    pointer = EvidencePointer(
        "b" * 32,
        "a" * 64,
        1,
        "c" * 32,
        "d" * 64,
    )

    assert integration_evidence.parse_pointer(
        integration_evidence.serialize_pointer(pointer)
    ) == pointer


def test_v2_contract_exposes_private_exact_window_allowlist():
    """Would fail if implementation drifted from shared private contract constant."""
    from codex_usage import integration_evidence

    assert integration_evidence._ALLOWED_WINDOW_SECONDS == frozenset(
        (18_000, 604_800, 2_592_000)
    )
