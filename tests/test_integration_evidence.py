from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from dataclasses import replace
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
        if relative_text == "src/codex_usage/integration_entrypoint.py":
            destination.write_text(
                "from __future__ import annotations\n\n"
                "def main(argv=None):\n"
                "    return 0\n",
                encoding="utf-8",
            )
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


def _staged_06536_manifest(verified, active_path: Path):
    """Task-3-only adapter; Task 6 replaces this with real 0.6.536 install."""
    from codex_usage.integration_attestation import VerifiedActiveManifest
    from codex_usage.private_io import FileIdentity

    active = json.loads(active_path.read_bytes())
    active["version"] = "0.6.536"
    active["release_id"] = "0.6.536-" + active["release_id"].split("-", 1)[1]
    active_bytes = (
        json.dumps(active, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    active_item = active_path.lstat()
    state_item = active_path.parents[2].lstat()
    integration_item = active_path.parent.lstat()
    return VerifiedActiveManifest(
        active_release=replace(verified.active_release, version="0.6.536"),
        release_id=active["release_id"],
        source_manifest_sha256=active["source_manifest_sha256"],
        active_manifest_bytes=active_bytes,
        active_manifest_sha256=hashlib.sha256(active_bytes).hexdigest(),
        state_home_identity=FileIdentity(
            state_item.st_dev, state_item.st_ino, stat.S_IMODE(state_item.st_mode)
        ),
        integration_parent_identity=FileIdentity(
            integration_item.st_dev,
            integration_item.st_ino,
            stat.S_IMODE(integration_item.st_mode),
        ),
        active_file_identity=FileIdentity(
            active_item.st_dev, active_item.st_ino, stat.S_IMODE(active_item.st_mode)
        ),
    )


@pytest.fixture
def evidence_layout(tmp_path):
    from codex_usage.integration_attestation import verify_active_manifest_at
    from codex_usage.integration_installer import install_release
    from codex_usage.integration_snapshot import serialize_schema2_document

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
    verified = verify_active_manifest_at(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=release.entrypoint_path,
    )
    _bootstrap_test_lock_inodes(state_home)
    payload = serialize_schema2_document(
        {
            "accounts": [],
            "generated_at": "2026-08-25T10:00:00Z",
            "schema_version": 2,
        }
    )
    return state_home, data_home, release.entrypoint_path, payload, verified


@pytest.fixture
def staged_evidence_layout(evidence_layout, monkeypatch):
    from codex_usage import integration_evidence

    state_home, data_home, entrypoint, payload, verified = evidence_layout
    integration = state_home / "codex-usage" / "integration"
    (integration / "generations").mkdir(mode=0o700)
    active_path = integration / "active.json"
    staged = _staged_06536_manifest(verified, active_path)

    def staged_reverify(*, state_home, data_home, expected_entrypoint_path):
        assert expected_entrypoint_path == entrypoint
        return _staged_06536_manifest(verified, state_home / "codex-usage/integration/active.json")

    monkeypatch.setattr(
        integration_evidence,
        "_verify_active_manifest_for_publish",
        staged_reverify,
        raising=False,
    )
    return state_home, data_home, entrypoint, payload, staged


@pytest.fixture
def published_evidence_layout(staged_evidence_layout):
    from codex_usage import integration_evidence

    state_home, data_home, entrypoint, payload, verified = staged_evidence_layout
    integration_evidence.publish_evidence_generation(
        payload,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )
    current = state_home / "codex-usage/integration/current.json"
    return state_home, data_home, entrypoint, payload, verified, current.read_bytes()


def replace_active_json_inode_after_payload_build(state_home, _data_home, _verified):
    active = state_home / "codex-usage/integration/active.json"
    document = json.loads(active.read_bytes())
    document["source_manifest_sha256"] = "f" * 64
    replacement = active.with_name("active.replacement.json")
    replacement.write_bytes(
        (
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    )
    replacement.chmod(0o600)
    os.replace(replacement, active)


def _replace_named_file(parent_fd: int, name: str, payload: bytes) -> None:
    old_name = f"old-{name}"
    os.rename(name, old_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def test_publish_creates_immutable_generation_then_one_current_pointer(
    staged_evidence_layout,
):
    """Would fail if publish skipped generation durability or emitted split pointers."""
    from codex_usage import integration_evidence

    state_home, data_home, _entrypoint, payload_bytes, verified = staged_evidence_layout
    pointer = integration_evidence.publish_evidence_generation(
        payload_bytes,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )
    generation = (
        state_home
        / "codex-usage/integration/generations"
        / pointer.current_generation_id
    )
    assert generation.is_dir()
    assert (generation / "account-usage-v2.json").read_bytes() == payload_bytes
    assert pointer.previous_generation_id is None
    assert integration_evidence.parse_pointer(
        (state_home / "codex-usage/integration/current.json").read_bytes()
    ) == pointer


def test_publish_does_not_swap_current_when_second_active_digest_changes(
    published_evidence_layout, monkeypatch
):
    """Would fail if publish trusted stale pre-build active attestation."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, _entrypoint, payload_bytes, verified, current_bytes = (
        published_evidence_layout
    )
    monkeypatch.setattr(
        integration_evidence,
        "_before_publish_active_reverify",
        replace_active_json_inode_after_payload_build,
    )
    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_evidence.publish_evidence_generation(
            payload_bytes,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified,
        )
    assert (state_home / "codex-usage/integration/current.json").read_bytes() == current_bytes


def test_publish_rejects_current_pointer_parent_swap(
    published_evidence_layout, monkeypatch
):
    """Would fail if pointer rename escaped captured integration parent FD."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, _entrypoint, payload, verified, current_bytes = (
        published_evidence_layout
    )
    integration = state_home / "codex-usage/integration"

    def swap_parent(_state_home, _integration_fd):
        old = integration.with_name("integration-old")
        os.rename(integration, old)
        integration.mkdir(mode=0o700)
        shutil.copyfile(old / "active.json", integration / "active.json")
        (integration / "active.json").chmod(0o600)
        shutil.copyfile(old / "current.json", integration / "current.json")
        (integration / "current.json").chmod(0o600)
        (integration / "generations").mkdir(mode=0o700)

    monkeypatch.setattr(
        integration_evidence, "_before_publish_pointer_parent_recheck", swap_parent
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.publish_evidence_generation(
            payload,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified,
        )
    assert (integration / "current.json").read_bytes() == current_bytes


def test_publish_rejects_generation_directory_swap(
    published_evidence_layout, monkeypatch
):
    """Would fail if immutable generation name could be rebound before Current."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, _entrypoint, payload, verified, current_bytes = (
        published_evidence_layout
    )

    def swap_generation(generations_fd, generation_id, _generation_fd):
        old_name = f".old-{generation_id}"
        os.rename(
            generation_id,
            old_name,
            src_dir_fd=generations_fd,
            dst_dir_fd=generations_fd,
        )
        os.mkdir(generation_id, mode=0o700, dir_fd=generations_fd)

    monkeypatch.setattr(
        integration_evidence, "_before_publish_generation_recheck", swap_generation
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.publish_evidence_generation(
            payload,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified,
        )
    assert (state_home / "codex-usage/integration/current.json").read_bytes() == current_bytes


@pytest.mark.parametrize(
    ("hook_name", "target_name"),
    [
        ("_before_publish_payload_recheck", "account-usage-v2.json"),
        ("_before_publish_binding_recheck", "account-usage-v2.binding.json"),
    ],
)
def test_publish_rejects_staged_file_inode_swap(
    published_evidence_layout, monkeypatch, hook_name, target_name
):
    """Would fail if staged file name could rebind after validated descriptor open."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, _entrypoint, payload, verified, current_bytes = (
        published_evidence_layout
    )

    def swap_file(parent_fd, name, held_fd):
        assert name == target_name
        os.lseek(held_fd, 0, os.SEEK_SET)
        original = os.read(held_fd, 2_097_153)
        _replace_named_file(parent_fd, name, original)

    monkeypatch.setattr(integration_evidence, hook_name, swap_file)
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.publish_evidence_generation(
            payload,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified,
        )
    assert (state_home / "codex-usage/integration/current.json").read_bytes() == current_bytes


def test_publish_rejects_payload_inode_swap(published_evidence_layout, monkeypatch):
    """Named compatibility case for payload inode race."""
    test_publish_rejects_staged_file_inode_swap(
        published_evidence_layout,
        monkeypatch,
        "_before_publish_payload_recheck",
        "account-usage-v2.json",
    )


def test_publish_rejects_binding_inode_swap(published_evidence_layout, monkeypatch):
    """Named compatibility case for Binding inode race."""
    test_publish_rejects_staged_file_inode_swap(
        published_evidence_layout,
        monkeypatch,
        "_before_publish_binding_recheck",
        "account-usage-v2.binding.json",
    )


def test_publish_runs_recovery_before_staging(staged_evidence_layout, monkeypatch):
    """Would fail if old crash debris was inspected after new staging mutation."""
    from codex_usage import integration_evidence

    state_home, data_home, _entrypoint, payload, verified = staged_evidence_layout
    events: list[str] = []
    real_recover = integration_evidence.recover_evidence_staging

    def recover(*, generations_fd):
        events.append("recover")
        return real_recover(generations_fd=generations_fd)

    monkeypatch.setattr(integration_evidence, "recover_evidence_staging", recover)
    monkeypatch.setattr(
        integration_evidence,
        "_before_publish_staging",
        lambda: events.append("stage"),
    )
    integration_evidence.publish_evidence_generation(
        payload,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )
    assert events[:2] == ["recover", "stage"]


def test_publish_rejects_seventeenth_staging_directory(staged_evidence_layout):
    """Would fail if recovery enumerated attacker-controlled staging unboundedly."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, _entrypoint, payload, verified = staged_evidence_layout
    generations = state_home / "codex-usage/integration/generations"
    for index in range(17):
        (generations / f".tmp-{index:032x}").mkdir(mode=0o700)
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.publish_evidence_generation(
            payload,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified,
        )
    assert not (state_home / "codex-usage/integration/current.json").exists()


def test_publish_never_creates_v1_cache(staged_evidence_layout):
    """Would fail if immutable publish retained legacy cache side effect."""
    from codex_usage import integration_evidence

    state_home, data_home, _entrypoint, payload, verified = staged_evidence_layout
    integration_evidence.publish_evidence_generation(
        payload,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )
    assert not (state_home / "codex-usage/integration/account-usage-v1.json").exists()


def test_publish_pointer_parent_fsync_failure_returns_committed_pointer(
    published_evidence_layout,
    monkeypatch,
):
    """Would fail if post-rename durability error made committed Current retryable."""
    from codex_usage import integration_evidence

    state_home, data_home, _entrypoint, payload, verified, old_current = (
        published_evidence_layout
    )
    current = state_home / "codex-usage/integration/current.json"
    real_fsync = integration_evidence.os.fsync

    def fail_only_after_pointer_commit(fd):
        if current.read_bytes() != old_current:
            raise OSError("synthetic post-commit pointer parent fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(integration_evidence.os, "fsync", fail_only_after_pointer_commit)
    pointer = integration_evidence.publish_evidence_generation(
        payload,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )
    assert current.read_bytes() != old_current
    assert integration_evidence.parse_pointer(current.read_bytes()) == pointer


def test_publish_active_rotation_after_commit_skips_gc_and_returns_pointer(
    published_evidence_layout,
    monkeypatch,
):
    """Would fail if post-commit release rotation looked like an uncommitted publish."""
    from codex_usage import integration_evidence

    state_home, data_home, _entrypoint, payload, verified, old_current = (
        published_evidence_layout
    )
    current = state_home / "codex-usage/integration/current.json"
    real_reverify = integration_evidence._verify_active_manifest_for_publish
    calls = 0
    gc_calls: list[None] = []

    def rotate_before_postcommit_reverify(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            replace_active_json_inode_after_payload_build(
                state_home,
                data_home,
                verified,
            )
        return real_reverify(**kwargs)

    monkeypatch.setattr(
        integration_evidence,
        "_verify_active_manifest_for_publish",
        rotate_before_postcommit_reverify,
    )
    monkeypatch.setattr(
        integration_evidence,
        "gc_evidence_generations",
        lambda **kwargs: gc_calls.append(None),
    )
    pointer = integration_evidence.publish_evidence_generation(
        payload,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )
    assert current.read_bytes() != old_current
    assert integration_evidence.parse_pointer(current.read_bytes()) == pointer
    assert gc_calls == []


def test_publish_gc_failure_after_commit_returns_committed_pointer(
    published_evidence_layout,
    monkeypatch,
):
    """Would fail if deferred retention failure invited duplicate publication retry."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, _entrypoint, payload, verified, old_current = (
        published_evidence_layout
    )
    current = state_home / "codex-usage/integration/current.json"
    monkeypatch.setattr(
        integration_evidence,
        "gc_evidence_generations",
        lambda **kwargs: (_ for _ in ()).throw(IntegrationEvidenceUnavailable()),
    )
    pointer = integration_evidence.publish_evidence_generation(
        payload,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )
    assert current.read_bytes() != old_current
    assert integration_evidence.parse_pointer(current.read_bytes()) == pointer


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
