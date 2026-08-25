from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

pytest_plugins = ("test_integration_evidence",)


def _replace_active_json_inode_once(integration_fd: int) -> None:
    source_fd = os.open(
        "active.json",
        os.O_RDONLY | os.O_NOFOLLOW,
        dir_fd=integration_fd,
    )
    try:
        payload = os.read(source_fd, 128 * 1024 + 1)
    finally:
        os.close(source_fd)
    replacement = ".active-replacement"
    fd = os.open(
        replacement,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=integration_fd,
    )
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(replacement, "active.json", src_dir_fd=integration_fd, dst_dir_fd=integration_fd)


def test_verify_active_manifest_at_rejects_active_json_inode_swap(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, _verified = evidence_layout
    monkeypatch.setattr(
        integration_attestation,
        "_before_active_identity_recheck",
        _replace_active_json_inode_once,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )


def _swap_directory(path: Path) -> None:
    old = path.with_name(path.name + "-old")
    replacement = path.with_name(path.name + "-replacement")
    replacement.mkdir(mode=0o700)
    os.rename(path, old)
    os.rename(replacement, path)


def test_verify_active_manifest_at_rejects_state_home_swap(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, _verified = evidence_layout

    def swap_state(_integration_fd):
        _swap_directory(state_home)

    monkeypatch.setattr(
        integration_attestation,
        "_before_active_identity_recheck",
        swap_state,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )


def test_verify_active_manifest_at_rejects_integration_parent_swap(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, _verified = evidence_layout
    integration = state_home / "codex-usage" / "integration"

    def swap_integration(_integration_fd):
        _swap_directory(integration)

    monkeypatch.setattr(
        integration_attestation,
        "_before_active_identity_recheck",
        swap_integration,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )


def test_verify_active_manifest_at_rejects_release_directory_swap_after_hash(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, verified = evidence_layout
    release_dir = verified.active_release.release_dir
    replacement = release_dir.parent / ".release-replacement"
    old = release_dir.parent / ".release-old"
    shutil.copytree(release_dir, replacement, copy_function=shutil.copy2)

    def swap_release(_integration_fd):
        os.rename(release_dir, old)
        os.rename(replacement, release_dir)

    monkeypatch.setattr(
        integration_attestation,
        "_before_active_identity_recheck",
        swap_release,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )


def test_verify_active_manifest_at_rejects_release_file_swap_after_hash(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, verified = evidence_layout
    replacement = verified.active_release.release_dir.parent / ".entrypoint-replacement"
    shutil.copy2(entrypoint, replacement)

    def swap_entrypoint(_integration_fd):
        os.replace(replacement, entrypoint)

    monkeypatch.setattr(
        integration_attestation,
        "_before_active_identity_recheck",
        swap_entrypoint,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )


def test_verify_active_manifest_at_rejects_descendant_directory_rebinding(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, verified = evidence_layout
    release_parent = verified.active_release.release_dir.parent
    package_dir = entrypoint.parent
    replacement = release_parent / ".package-replacement"
    old = release_parent / ".package-old"
    shutil.copytree(package_dir, replacement, copy_function=shutil.copy2)
    walks = 0

    def swap_after_last_descendant_walk(_release_fd):
        nonlocal walks
        walks += 1
        if walks == 3:
            os.rename(package_dir, old)
            os.rename(replacement, package_dir)

    monkeypatch.setattr(
        integration_attestation,
        "_before_release_namespace_recheck",
        swap_after_last_descendant_walk,
        raising=False,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )
    assert walks == 3
