from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import stat
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
    "src/codex_usage/integration_evidence.py",
    "src/codex_usage/integration_entrypoint.py",
    "src/codex_usage/integration_pool_authority.py",
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
    authority_source = (
        state_home
        / "codex-usage"
        / "integration"
        / "pool-authority-source-v2.json"
    )
    authority_source.write_bytes(
        b'{"authorities":[],"pool_authority_source_schema_version":2}\n'
    )
    authority_source.chmod(0o600)
    payload = serialize_schema2_document(
        {
            "accounts": [],
            "generated_at": "2026-08-25T10:00:00Z",
            "schema_version": 2,
        }
    )
    return state_home, data_home, release.entrypoint_path, payload, verified


@pytest.fixture
def staged_evidence_layout(evidence_layout):
    return evidence_layout


def _install_distinct_active_release(
    tmp_path: Path,
    *,
    state_home: Path,
    data_home: Path,
):
    from codex_usage.integration_attestation import verify_active_manifest_at
    from codex_usage.integration_installer import install_release

    source_parent = tmp_path / "release-b-source"
    temporary_root = tmp_path / "release-b-temporary"
    source_parent.mkdir(mode=0o700)
    temporary_root.mkdir(mode=0o700)
    source_root = _source_copy(source_parent)
    distinct_source = source_root / "src/codex_usage/integration_snapshot.py"
    distinct_source.write_bytes(
        distinct_source.read_bytes() + b"\n# distinct evidence rotation release\n"
    )
    release = install_release(
        source_root=source_root,
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
    return release, verified


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


def _create_complete_generations(
    state_home: Path,
    data_home: Path,
    verified_active_manifest,
    count: int,
):
    from codex_usage import integration_evidence, private_io
    from codex_usage.integration_snapshot import serialize_schema2_document

    del data_home
    integration = state_home / "codex-usage/integration"
    generations = integration / "generations"
    generations_fd = os.open(
        generations,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    generation_ids: list[str] = []
    binding_digests: list[str] = []
    try:
        for index in range(count):
            generation_id = f"{index:032x}"
            generation_ids.append(generation_id)
            os.mkdir(generation_id, mode=0o700, dir_fd=generations_fd)
            generation_fd = private_io.open_private_dir_at(
                generations_fd,
                generation_id,
            )
            try:
                published_at = "2026-08-25T10:00:00Z"
                payload = serialize_schema2_document(
                    {
                        "accounts": [],
                        "generated_at": published_at,
                        "schema_version": 2,
                    }
                )
                binding = integration_evidence.EvidenceBinding(
                    active_manifest_sha256=(
                        verified_active_manifest.active_manifest_sha256
                    ),
                    binding_schema_version=2,
                    generation_id=generation_id,
                    payload_filename="account-usage-v2.json",
                    payload_sha256=hashlib.sha256(payload).hexdigest(),
                    payload_size_bytes=len(payload),
                    published_at=published_at,
                    producer_version="0.6.537",
                    release_id=verified_active_manifest.release_id,
                    source_manifest_sha256=(
                        verified_active_manifest.source_manifest_sha256
                    ),
                    usage_binding_schema_version=2,
                    pool_authority_filename="pool-authority-v2.json",
                    pool_authority_sha256="0" * 64,
                    pool_authority_size_bytes=1,
                )
                from codex_usage.integration_pool_authority import (
                    build_pool_authority_projection,
                    serialize_pool_authority_projection,
                )

                pool_authority_bytes = serialize_pool_authority_projection(
                    build_pool_authority_projection(
                        source={
                            "authorities": [],
                            "pool_authority_source_schema_version": 2,
                        },
                        usage_document=json.loads(payload),
                        generation_id=generation_id,
                        release_id=verified_active_manifest.release_id,
                        usage_payload_sha256=hashlib.sha256(payload).hexdigest(),
                        usage_binding_sha256=hashlib.sha256(
                            integration_evidence.serialize_usage_binding(binding)
                        ).hexdigest(),
                    )
                )
                binding = replace(
                    binding,
                    pool_authority_sha256=hashlib.sha256(
                        pool_authority_bytes
                    ).hexdigest(),
                    pool_authority_size_bytes=len(pool_authority_bytes),
                )
                binding_bytes = integration_evidence.serialize_binding(binding)
                assert integration_evidence.parse_binding(binding_bytes) == binding
                private_io.write_private_bytes_at(
                    generation_fd,
                    "account-usage-v2.json",
                    payload,
                    mode=0o600,
                )
                private_io.write_private_bytes_at(
                    generation_fd,
                    "pool-authority-v2.json",
                    pool_authority_bytes,
                    mode=0o600,
                )
                private_io.write_private_bytes_at(
                    generation_fd,
                    "account-usage-v2.binding.json",
                    binding_bytes,
                    mode=0o600,
                )
                os.fsync(generation_fd)
                binding_digests.append(hashlib.sha256(binding_bytes).hexdigest())
            finally:
                os.close(generation_fd)
        os.fsync(generations_fd)
    finally:
        os.close(generations_fd)

    pointer = integration_evidence.EvidencePointer(
        generation_ids[-1],
        binding_digests[-1],
        1,
        generation_ids[-2],
        binding_digests[-2],
    )
    integration_fd = os.open(
        integration,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        private_io.write_private_bytes_at(
            integration_fd,
            "current.json",
            integration_evidence.serialize_pointer(pointer),
            mode=0o600,
        )
        os.fsync(integration_fd)
    finally:
        os.close(integration_fd)
    return pointer


def create_257_complete_generations(
    state_home: Path,
    data_home: Path,
    verified_active_manifest,
):
    return _create_complete_generations(
        state_home,
        data_home,
        verified_active_manifest,
        257,
    )


def create_258_complete_generations(
    state_home: Path,
    data_home: Path,
    verified_active_manifest,
):
    return _create_complete_generations(
        state_home,
        data_home,
        verified_active_manifest,
        258,
    )


def count_complete_generation_directories(state_home: Path) -> int:
    generations = state_home / "codex-usage/integration/generations"
    return sum(
        1
        for entry in os.scandir(generations)
        if not entry.name.startswith(".tmp-") and entry.is_dir(follow_symlinks=False)
    )


def create_seventeen_staging_directories(evidence_layout) -> int:
    state_home, _data_home, _entrypoint, _payload, _verified = evidence_layout
    generations = state_home / "codex-usage/integration/generations"
    generations_fd = os.open(
        generations,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    for index in range(17):
        os.mkdir(f".tmp-{index:032x}", mode=0o700, dir_fd=generations_fd)
    return generations_fd


def _write_pointer_temp(
    integration: Path,
    index: int,
    *,
    payload: bytes = b"{}",
) -> Path:
    name = f".tmp-current.json-{index:032x}"
    integration_fd = os.open(
        integration,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    fd = -1
    try:
        fd = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=integration_fd,
        )
        assert os.write(fd, payload) == len(payload)
        os.fsync(fd)
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(integration_fd)
    return integration / name


def _try_shared_evidence_lock(state_home_text: str, result) -> None:
    from codex_usage.integration_evidence import IntegrationBusy, evidence_lock_set

    try:
        with evidence_lock_set(
            state_home=Path(state_home_text),
            release_mode="shared",
            current_mode="shared",
            timeout_seconds=0,
            create=False,
        ):
            result.put("acquired")
    except IntegrationBusy:
        result.put("busy")


def _rewrite_complete_generation(
    state_home: Path,
    generation_id: str,
    *,
    published_at: str | None = None,
    historical_trust: bool = False,
) -> None:
    from codex_usage import integration_evidence

    generation = (
        state_home / "codex-usage/integration/generations" / generation_id
    )
    payload_path = generation / "account-usage-v2.json"
    payload = payload_path.read_bytes()
    binding_path = generation / "account-usage-v2.binding.json"
    binding = integration_evidence.parse_binding(binding_path.read_bytes())
    if published_at is not None:
        document = json.loads(payload)
        document["generated_at"] = published_at
        payload = integration_evidence.serialize_schema2_document(document)
        _rewrite_reader_file(generation, "account-usage-v2.json", payload)
        binding = replace(
            binding,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            payload_size_bytes=len(payload),
            published_at=published_at,
        )
    if historical_trust:
        binding = replace(
            binding,
            active_manifest_sha256="c" * 64,
            release_id="0.6.537-" + "d" * 16,
            source_manifest_sha256="e" * 64,
        )
    from codex_usage.integration_pool_authority import (
        parse_pool_authority_projection,
        serialize_pool_authority_projection,
    )

    authority_path = generation / "pool-authority-v2.json"
    authority = parse_pool_authority_projection(authority_path.read_bytes())
    authority["issued_at"] = binding.published_at
    authority["expires_at"] = (
        datetime.fromisoformat(binding.published_at.replace("Z", "+00:00"))
        + timedelta(minutes=15)
    ).isoformat().replace("+00:00", "Z")
    authority["release_id"] = binding.release_id
    authority["usage_payload_sha256"] = binding.payload_sha256
    authority["usage_binding_sha256"] = hashlib.sha256(
        integration_evidence.serialize_usage_binding(binding)
    ).hexdigest()
    authority_bytes = serialize_pool_authority_projection(authority)
    _rewrite_reader_file(generation, "pool-authority-v2.json", authority_bytes)
    binding = replace(
        binding,
        pool_authority_sha256=hashlib.sha256(authority_bytes).hexdigest(),
        pool_authority_size_bytes=len(authority_bytes),
    )
    _rewrite_reader_file(
        generation,
        "account-usage-v2.binding.json",
        integration_evidence.serialize_binding(binding),
    )


def test_rollback_swaps_current_and_previous_in_one_pointer_rename(
    staged_evidence_layout, monkeypatch
):
    """Would fail if rollback changed generations instead of swapping one pointer."""
    from codex_usage import integration_evidence

    state_home, data_home, _entrypoint, payload_bytes, verified = (
        staged_evidence_layout
    )
    integration_evidence.publish_evidence_generation(
        payload_bytes,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )
    second = integration_evidence.publish_evidence_generation(
        payload_bytes,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )
    real_replace = os.replace
    current_replaces: list[tuple[str, str]] = []

    def track_current_replace(src, dst, *args, **kwargs):
        if dst == "current.json":
            current_replaces.append((src, dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(integration_evidence.os, "replace", track_current_replace)

    after = integration_evidence.rollback_current_evidence(
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )

    assert after.current_generation_id == second.previous_generation_id
    assert after.previous_generation_id == second.current_generation_id
    assert integration_evidence.parse_pointer(
        (state_home / "codex-usage/integration/current.json").read_bytes()
    ) == after
    assert len(current_replaces) == 1
    assert current_replaces[0][1] == "current.json"
    assert count_complete_generation_directories(state_home) == 2


def test_gc_scans_257_complete_generations_then_retains_256(
    staged_evidence_layout,
):
    """Would fail if GC skipped the 257 boundary or deleted a protected generation."""
    from codex_usage import integration_evidence

    state_home, data_home, _entrypoint, _payload, verified = staged_evidence_layout
    pointer = create_257_complete_generations(state_home, data_home, verified)

    integration_evidence.gc_evidence_generations(
        state_home=state_home,
        data_home=data_home,
        pointer=pointer,
        verified_active_manifest=verified,
    )

    assert count_complete_generation_directories(state_home) == 256
    generations = state_home / "codex-usage/integration/generations"
    assert not (generations / f"{0:032x}").exists()
    assert (generations / pointer.current_generation_id).is_dir()
    assert (generations / pointer.previous_generation_id).is_dir()


def test_publish_from_256_prunes_before_commit_and_stays_at_256(
    staged_evidence_layout,
):
    """Would fail if successful publish exposed a post-commit 257th generation."""
    from codex_usage import integration_evidence

    state_home, data_home, _entrypoint, payload, verified = staged_evidence_layout
    old_pointer = _create_complete_generations(
        state_home,
        data_home,
        verified,
        256,
    )

    pointer = integration_evidence.publish_evidence_generation(
        payload,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )

    assert count_complete_generation_directories(state_home) == 256
    assert pointer.current_generation_id != old_pointer.current_generation_id
    assert pointer.previous_generation_id == old_pointer.current_generation_id


def test_publish_from_257_prunes_before_pointer_and_never_commits_258(
    staged_evidence_layout, monkeypatch
):
    """Would fail if a publish could cross the explicit 258 invalid boundary."""
    from codex_usage import integration_evidence

    state_home, data_home, _entrypoint, payload, verified = staged_evidence_layout
    _create_complete_generations(state_home, data_home, verified, 257)
    real_replace = integration_evidence._atomic_replace_current
    counts_at_commit: list[int] = []

    def replace_current(integration_fd: int, pointer_bytes: bytes, **kwargs) -> None:
        counts_at_commit.append(count_complete_generation_directories(state_home))
        assert counts_at_commit[-1] == 256
        real_replace(integration_fd, pointer_bytes, **kwargs)

    monkeypatch.setattr(
        integration_evidence,
        "_atomic_replace_current",
        replace_current,
    )

    integration_evidence.publish_evidence_generation(
        payload,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )

    assert counts_at_commit == [256]
    assert count_complete_generation_directories(state_home) == 256


def test_publish_retention_holds_exclusive_locks_through_commit(
    staged_evidence_layout, monkeypatch
):
    """Would fail if a cooperative reader could interleave after retention."""
    from codex_usage import integration_evidence

    state_home, data_home, _entrypoint, payload, verified = staged_evidence_layout
    _create_complete_generations(state_home, data_home, verified, 256)
    context = multiprocessing.get_context("spawn")
    observed: list[str] = []

    def probe_lock() -> None:
        result = context.Queue()
        process = context.Process(
            target=_try_shared_evidence_lock,
            args=(str(state_home), result),
        )
        process.start()
        try:
            observed.append(result.get(timeout=10))
        finally:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        assert process.exitcode == 0

    monkeypatch.setattr(
        integration_evidence,
        "_before_publish_retention_reclaim",
        probe_lock,
    )

    integration_evidence.publish_evidence_generation(
        payload,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )

    assert observed == ["busy"]


def test_gc_orders_fractional_timestamps_by_utc_instant_then_generation_id(
    staged_evidence_layout,
):
    """Would fail if canonical timestamps were sorted as text instead of instants."""
    from codex_usage import integration_evidence

    state_home, data_home, _entrypoint, _payload, verified = staged_evidence_layout
    pointer = create_257_complete_generations(state_home, data_home, verified)
    fractional_later = f"{0:032x}"
    whole_second_older = f"{1:032x}"
    _rewrite_complete_generation(
        state_home,
        fractional_later,
        published_at="2026-08-25T10:00:00.9Z",
    )
    _rewrite_complete_generation(
        state_home,
        whole_second_older,
        published_at="2026-08-25T10:00:00Z",
    )

    integration_evidence.gc_evidence_generations(
        state_home=state_home,
        data_home=data_home,
        pointer=pointer,
        verified_active_manifest=verified,
    )

    generations = state_home / "codex-usage/integration/generations"
    assert (generations / fractional_later).is_dir()
    assert not (generations / whole_second_older).exists()


def test_gc_reclaims_valid_history_from_prior_active_manifest(
    staged_evidence_layout,
):
    """Would fail if GC required historical evidence to match current Active."""
    from codex_usage import integration_evidence

    state_home, data_home, _entrypoint, _payload, verified = staged_evidence_layout
    pointer = create_257_complete_generations(state_home, data_home, verified)
    historical = f"{0:032x}"
    _rewrite_complete_generation(
        state_home,
        historical,
        historical_trust=True,
    )

    integration_evidence.gc_evidence_generations(
        state_home=state_home,
        data_home=data_home,
        pointer=pointer,
        verified_active_manifest=verified,
    )

    assert count_complete_generation_directories(state_home) == 256
    assert not (
        state_home / "codex-usage/integration/generations" / historical
    ).exists()


def test_active_release_rotation_keeps_publication_reader_and_gc_live(
    staged_evidence_layout,
    tmp_path,
):
    """Historical A stays integral while strict Current advances under active B."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint_a, payload_a, verified_a = (
        staged_evidence_layout
    )
    pointer_a = integration_evidence.publish_evidence_generation(
        payload_a,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified_a,
    )
    release_b, verified_b = _install_distinct_active_release(
        tmp_path,
        state_home=state_home,
        data_home=data_home,
    )
    assert verified_b.release_id != verified_a.release_id
    assert (
        integration_evidence.read_current_evidence(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release_b.entrypoint_path,
            now=datetime(2026, 8, 25, 10, 1, tzinfo=UTC),
        )[1]
        == "invalid"
    )

    integration_evidence.gc_evidence_generations(
        state_home=state_home,
        data_home=data_home,
        pointer=pointer_a,
        verified_active_manifest=verified_b,
    )
    payload_b1 = integration_evidence.serialize_schema2_document(
        {
            "accounts": [],
            "generated_at": "2026-08-25T10:01:00Z",
            "schema_version": 2,
        }
    )
    pointer_b1 = integration_evidence.publish_evidence_generation(
        payload_b1,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified_b,
    )
    assert pointer_b1.previous_generation_id == pointer_a.current_generation_id
    assert (
        integration_evidence.read_current_evidence(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release_b.entrypoint_path,
            now=datetime(2026, 8, 25, 10, 1, tzinfo=UTC),
        )[1]
        == "complete"
    )
    integration_evidence.gc_evidence_generations(
        state_home=state_home,
        data_home=data_home,
        pointer=pointer_b1,
        verified_active_manifest=verified_b,
    )

    payload_b2 = integration_evidence.serialize_schema2_document(
        {
            "accounts": [],
            "generated_at": "2026-08-25T10:02:00Z",
            "schema_version": 2,
        }
    )
    pointer_b2 = integration_evidence.publish_evidence_generation(
        payload_b2,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified_b,
    )
    assert pointer_b2.previous_generation_id == pointer_b1.current_generation_id
    assert (
        integration_evidence.read_current_evidence(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release_b.entrypoint_path,
            now=datetime(2026, 8, 25, 10, 2, tzinfo=UTC),
        )[1]
        == "complete"
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.publish_evidence_generation(
            payload_b1,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified_b,
        )
    assert integration_evidence.parse_pointer(
        (state_home / "codex-usage/integration/current.json").read_bytes()
    ) == pointer_b2
    assert entrypoint_a != release_b.entrypoint_path


@pytest.mark.parametrize("mutation", ("malformed_binding", "payload_hash"))
def test_rotated_publisher_and_gc_reject_malformed_historical_current(
    staged_evidence_layout,
    tmp_path,
    mutation,
):
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, _entrypoint, payload, verified_a = (
        staged_evidence_layout
    )
    pointer_a = integration_evidence.publish_evidence_generation(
        payload,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified_a,
    )
    _release_b, verified_b = _install_distinct_active_release(
        tmp_path,
        state_home=state_home,
        data_home=data_home,
    )
    generation = (
        state_home
        / "codex-usage/integration/generations"
        / pointer_a.current_generation_id
    )
    if mutation == "malformed_binding":
        _rewrite_reader_file(
            generation,
            "account-usage-v2.binding.json",
            b"{}",
        )
    else:
        _rewrite_reader_file(
            generation,
            "account-usage-v2.json",
            payload + b"\n",
        )
    current = state_home / "codex-usage/integration/current.json"
    before = current.read_bytes()

    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.gc_evidence_generations(
            state_home=state_home,
            data_home=data_home,
            pointer=pointer_a,
            verified_active_manifest=verified_b,
        )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.publish_evidence_generation(
            payload,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified_b,
        )
    assert current.read_bytes() == before


def test_rollback_rejects_previous_from_prior_active_release(
    staged_evidence_layout,
    tmp_path,
):
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, _entrypoint, payload_a, verified_a = (
        staged_evidence_layout
    )
    pointer_a = integration_evidence.publish_evidence_generation(
        payload_a,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified_a,
    )
    _release_b, verified_b = _install_distinct_active_release(
        tmp_path,
        state_home=state_home,
        data_home=data_home,
    )
    payload_b = integration_evidence.serialize_schema2_document(
        {
            "accounts": [],
            "generated_at": "2026-08-25T10:01:00Z",
            "schema_version": 2,
        }
    )
    pointer_b = integration_evidence.publish_evidence_generation(
        payload_b,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified_b,
    )
    assert pointer_b.previous_generation_id == pointer_a.current_generation_id
    current = state_home / "codex-usage/integration/current.json"
    before = current.read_bytes()

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_evidence.rollback_current_evidence(
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified_b,
        )
    assert current.read_bytes() == before


def test_gc_never_unlinks_inside_final_generation_namespace(
    staged_evidence_layout,
    monkeypatch,
):
    """Would fail if GC damaged an immutable final generation before rename."""
    from codex_usage import integration_evidence

    state_home, data_home, _entrypoint, _payload, verified = staged_evidence_layout
    pointer = create_257_complete_generations(state_home, data_home, verified)
    real_unlink = os.unlink
    unlink_parents: list[str] = []

    def record_unlink_parent(name, *args, dir_fd=None, **kwargs):
        unlink_parents.append(_crash_fd_name(dir_fd))
        return real_unlink(name, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(
        integration_evidence.os,
        "unlink",
        record_unlink_parent,
    )
    integration_evidence.gc_evidence_generations(
        state_home=state_home,
        data_home=data_home,
        pointer=pointer,
        verified_active_manifest=verified,
    )

    assert unlink_parents
    assert all(parent.startswith(".tmp-") for parent in unlink_parents)


@pytest.mark.parametrize("interruption", ("after_rename", "after_first_unlink"))
def test_gc_recovery_cleans_interrupted_temporary_victim(
    staged_evidence_layout,
    monkeypatch,
    interruption,
):
    """Would fail if interrupted GC left final damage or unrecoverable debris."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, _entrypoint, _payload, verified = staged_evidence_layout
    pointer = create_257_complete_generations(state_home, data_home, verified)
    generations = state_home / "codex-usage/integration/generations"
    victim_id = f"{0:032x}"
    temporary = generations / f".tmp-{victim_id}"
    injected = False
    real_fsync = os.fsync
    real_unlink = os.unlink

    def fail_after_victim_rename(fd):
        nonlocal injected
        if (
            not injected
            and _crash_fd_name(fd) == "generations"
            and temporary.is_dir()
        ):
            injected = True
            raise OSError("synthetic crash after victim rename")
        return real_fsync(fd)

    def fail_after_first_temporary_unlink(name, *args, dir_fd=None, **kwargs):
        nonlocal injected
        result = real_unlink(name, *args, dir_fd=dir_fd, **kwargs)
        if not injected and _crash_fd_name(dir_fd).startswith(".tmp-"):
            injected = True
            raise OSError("synthetic crash after first temporary unlink")
        return result

    if interruption == "after_rename":
        monkeypatch.setattr(
            integration_evidence.os,
            "fsync",
            fail_after_victim_rename,
        )
    else:
        monkeypatch.setattr(
            integration_evidence.os,
            "unlink",
            fail_after_first_temporary_unlink,
        )

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_evidence.gc_evidence_generations(
            state_home=state_home,
            data_home=data_home,
            pointer=pointer,
            verified_active_manifest=verified,
        )

    assert injected
    assert temporary.is_dir()
    assert count_complete_generation_directories(state_home) == 256

    integration_evidence.gc_evidence_generations(
        state_home=state_home,
        data_home=data_home,
        pointer=pointer,
        verified_active_manifest=verified,
    )

    assert not temporary.exists()
    assert count_complete_generation_directories(state_home) == 256


def test_gc_rejects_temporary_victim_name_collision_before_rename(
    staged_evidence_layout,
    monkeypatch,
):
    """Would fail if GC replaced or ignored an occupied recovery namespace."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, _entrypoint, _payload, verified = staged_evidence_layout
    pointer = create_257_complete_generations(state_home, data_home, verified)
    victim_id = f"{0:032x}"
    real_inspect = integration_evidence._inspect_complete_generation_names

    def inspect_then_collide(*, generations_fd, names):
        generations = real_inspect(generations_fd=generations_fd, names=names)
        os.mkdir(f".tmp-{victim_id}", mode=0o700, dir_fd=generations_fd)
        return generations

    monkeypatch.setattr(
        integration_evidence,
        "_inspect_complete_generation_names",
        inspect_then_collide,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.gc_evidence_generations(
            state_home=state_home,
            data_home=data_home,
            pointer=pointer,
            verified_active_manifest=verified,
        )

    generations = state_home / "codex-usage/integration/generations"
    assert (generations / victim_id).is_dir()
    assert (generations / f".tmp-{victim_id}").is_dir()
    assert count_complete_generation_directories(state_home) == 257


def test_rollback_rejects_invalid_previous_without_pointer_change(
    staged_evidence_layout,
):
    """Would fail if rollback promoted a Previous generation before full validation."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, _entrypoint, payload, verified = staged_evidence_layout
    integration_evidence.publish_evidence_generation(
        payload,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )
    pointer = integration_evidence.publish_evidence_generation(
        payload,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )
    current = state_home / "codex-usage/integration/current.json"
    current_bytes = current.read_bytes()
    assert pointer.previous_generation_id is not None
    previous = (
        state_home
        / "codex-usage/integration/generations"
        / pointer.previous_generation_id
    )
    _rewrite_reader_file(previous, "account-usage-v2.json", b"{}")

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_evidence.rollback_current_evidence(
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified,
        )

    assert current.read_bytes() == current_bytes


def test_gc_rejects_258_complete_generations_without_deletion(
    staged_evidence_layout,
):
    """Would fail if GC mutated state after crossing its bounded scan limit."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, _entrypoint, _payload, verified = staged_evidence_layout
    pointer = create_258_complete_generations(state_home, data_home, verified)

    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.gc_evidence_generations(
            state_home=state_home,
            data_home=data_home,
            pointer=pointer,
            verified_active_manifest=verified,
        )

    assert count_complete_generation_directories(state_home) == 258


@pytest.mark.parametrize("scenario", ("seventeenth", "unsafe"))
def test_recovery_rejects_seventeenth_or_unsafe_staging_directory(
    staged_evidence_layout, scenario
):
    """Would fail if recovery enumerated or removed an over-limit staging set."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, _data_home, _entrypoint, _payload, _verified = (
        staged_evidence_layout
    )
    if scenario == "seventeenth":
        generations_fd = create_seventeen_staging_directories(
            staged_evidence_layout
        )
    else:
        generations = state_home / "codex-usage/integration/generations"
        generations_fd = os.open(
            generations,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        staging_name = f".tmp-{0:032x}"
        os.mkdir(staging_name, mode=0o700, dir_fd=generations_fd)
        staging_fd = os.open(
            staging_name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            dir_fd=generations_fd,
        )
        try:
            os.symlink("outside", "account-usage-v2.json", dir_fd=staging_fd)
        finally:
            os.close(staging_fd)
    os.close(generations_fd)
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.recover_evidence_staging(state_home=state_home)


def test_namespace_scan_stops_at_258th_complete_entry(tmp_path, monkeypatch):
    """Would fail if namespace classification consumed an unbounded iterator."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    namespace = tmp_path / "generations"
    namespace.mkdir(mode=0o700)
    namespace_fd = os.open(
        namespace,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    consumed = 0

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    class Entries:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            nonlocal consumed
            for index in range(100_000):
                consumed += 1
                yield Entry(f"{index:032x}")

    monkeypatch.setattr(integration_evidence.os, "scandir", lambda _fd: Entries())
    try:
        with pytest.raises(IntegrationEvidenceInvalid):
            integration_evidence._scan_generation_namespace(namespace_fd)
    finally:
        os.close(namespace_fd)

    assert consumed == 258


def test_foreign_generation_namespace_entry_blocks_publish(
    staged_evidence_layout,
):
    """Would fail if publisher ignored a foreign namespace entry before commit."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, _entrypoint, payload, verified = staged_evidence_layout
    generations = state_home / "codex-usage/integration/generations"
    (generations / "foreign").mkdir(mode=0o700)

    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.publish_evidence_generation(
            payload,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified,
        )

    assert not (state_home / "codex-usage/integration/current.json").exists()


def test_recovery_rechecks_each_name_before_unlink(
    staged_evidence_layout,
    monkeypatch,
):
    """Would fail if recovery unlinked a replacement installed after prior unlink."""
    from codex_usage import integration_evidence, private_io
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, _data_home, _entrypoint, _payload, _verified = staged_evidence_layout
    generations = state_home / "codex-usage/integration/generations"
    generations_fd = os.open(
        generations,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    staging_name = f".tmp-{0:032x}"
    os.mkdir(staging_name, mode=0o700, dir_fd=generations_fd)
    staging_fd = private_io.open_private_dir_at(generations_fd, staging_name)
    contents = {
        "account-usage-v2.json": b"payload",
        "account-usage-v2.binding.json": b"binding",
    }
    try:
        for name, payload in contents.items():
            private_io.write_private_bytes_at(staging_fd, name, payload, mode=0o600)
    finally:
        os.close(staging_fd)

    real_unlink = os.unlink
    replacement_name: list[str] = []

    def replace_other_after_first_unlink(name, *args, dir_fd=None, **kwargs):
        result = real_unlink(name, *args, dir_fd=dir_fd, **kwargs)
        if not replacement_name:
            other = next(candidate for candidate in contents if candidate != name)
            _replace_named_file(dir_fd, other, contents[other])
            replacement_name.append(other)
        return result

    monkeypatch.setattr(
        integration_evidence.os,
        "unlink",
        replace_other_after_first_unlink,
    )
    os.close(generations_fd)
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.recover_evidence_staging(state_home=state_home)
    assert replacement_name
    assert (generations / staging_name / replacement_name[0]).exists()


@pytest.mark.parametrize(
    "shape",
    (
        "directory",
        "symlink",
        "hardlink",
        "oversize",
        "wrong-mode",
        "malformed-short",
        "malformed-uppercase",
    ),
)
def test_pointer_temp_recovery_rejects_unsafe_artifact_without_deleting_it(
    staged_evidence_layout,
    shape,
):
    """Would fail if root recovery deleted a non-private pointer temp."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, _data_home, _entrypoint, _payload, _verified = staged_evidence_layout
    integration = state_home / "codex-usage/integration"
    if shape == "malformed-short":
        name = f".tmp-current.json-{0:031x}"
    elif shape == "malformed-uppercase":
        name = f".tmp-current.json-{'A' * 32}"
    else:
        name = f".tmp-current.json-{0:032x}"
    artifact = integration / name
    if shape == "directory":
        artifact.mkdir(mode=0o700)
    elif shape == "symlink":
        artifact.symlink_to("current.json")
    elif shape == "hardlink":
        source = integration / "pointer-temp-hardlink-source"
        source.write_bytes(b"")
        source.chmod(0o600)
        os.link(source, artifact)
    else:
        payload = b"x" * 4097 if shape == "oversize" else b""
        artifact.write_bytes(payload)
        artifact.chmod(0o644 if shape == "wrong-mode" else 0o600)

    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.recover_evidence_staging(state_home=state_home)

    assert artifact.exists() or artifact.is_symlink()


@pytest.mark.parametrize("size", (0, 1, 4096))
def test_pointer_temp_recovery_accepts_private_size_boundaries(
    staged_evidence_layout,
    size,
):
    """Would fail if recovery rejected a safe boundary-sized pointer temp."""
    from codex_usage import integration_evidence

    state_home, _data_home, _entrypoint, _payload, _verified = staged_evidence_layout
    integration = state_home / "codex-usage/integration"
    artifact = _write_pointer_temp(integration, size, payload=b"x" * size)

    integration_evidence.recover_evidence_staging(state_home=state_home)

    assert not artifact.exists()


def test_pointer_temp_recovery_rejects_sixty_fifth_empty_artifact_without_deletion(
    staged_evidence_layout,
):
    """Would fail if empty crash debris bypassed the 64-artifact cap."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, _data_home, _entrypoint, _payload, _verified = staged_evidence_layout
    integration = state_home / "codex-usage/integration"
    artifacts = [
        _write_pointer_temp(integration, index, payload=b"") for index in range(65)
    ]

    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.recover_evidence_staging(state_home=state_home)

    assert all(artifact.read_bytes() == b"" for artifact in artifacts)


def test_pointer_temp_root_scan_stops_at_129th_entry(tmp_path, monkeypatch):
    """Would fail if root recovery consumed an unbounded directory iterator."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    integration = tmp_path / "integration"
    integration.mkdir(mode=0o700)
    integration_fd = os.open(
        integration,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    consumed = 0

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    class Entries:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            nonlocal consumed
            for index in range(100_000):
                consumed += 1
                yield Entry(f"foreign-{index}")

    monkeypatch.setattr(integration_evidence.os, "scandir", lambda _fd: Entries())
    try:
        with pytest.raises(IntegrationEvidenceInvalid):
            integration_evidence._scan_integration_recovery_namespace(
                integration_fd
            )
    finally:
        os.close(integration_fd)

    assert consumed == 129


def test_pointer_temp_recovery_rechecks_name_identity_before_each_unlink(
    staged_evidence_layout,
    monkeypatch,
):
    """Would fail if cleanup unlinked a replacement after its initial scan."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, _data_home, _entrypoint, _payload, _verified = staged_evidence_layout
    integration = state_home / "codex-usage/integration"
    artifacts = [_write_pointer_temp(integration, index) for index in range(2)]
    real_unlink = integration_evidence.os.unlink
    replaced: list[Path] = []

    def replace_second_after_first_unlink(name, *args, dir_fd=None, **kwargs):
        result = real_unlink(name, *args, dir_fd=dir_fd, **kwargs)
        if not replaced:
            second = artifacts[1]
            real_unlink(second)
            second.write_bytes(b"{}")
            second.chmod(0o600)
            replaced.append(second)
        return result

    monkeypatch.setattr(
        integration_evidence.os,
        "unlink",
        replace_second_after_first_unlink,
    )

    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.recover_evidence_staging(state_home=state_home)

    assert replaced == [artifacts[1]]
    assert artifacts[1].read_bytes() == b"{}"


def test_pointer_temp_cleanup_interruption_is_recoverable(
    staged_evidence_layout,
    monkeypatch,
):
    """Would fail if partial cleanup poisoned the next public recovery run."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, _data_home, _entrypoint, _payload, _verified = staged_evidence_layout
    integration = state_home / "codex-usage/integration"
    artifacts = [_write_pointer_temp(integration, index) for index in range(2)]
    real_unlink = integration_evidence.os.unlink
    interrupted = False

    def interrupt_after_first_unlink(name, *args, dir_fd=None, **kwargs):
        nonlocal interrupted
        result = real_unlink(name, *args, dir_fd=dir_fd, **kwargs)
        if not interrupted:
            interrupted = True
            raise OSError("simulated cleanup interruption")
        return result

    monkeypatch.setattr(
        integration_evidence.os,
        "unlink",
        interrupt_after_first_unlink,
    )
    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_evidence.recover_evidence_staging(state_home=state_home)

    monkeypatch.setattr(integration_evidence.os, "unlink", real_unlink)
    integration_evidence.recover_evidence_staging(state_home=state_home)

    assert not any(artifact.exists() for artifact in artifacts)


def test_pointer_temp_recovery_fsyncs_integration_directory(
    staged_evidence_layout,
    monkeypatch,
):
    """Would fail if cleanup returned before persisting root namespace changes."""
    from codex_usage import integration_evidence

    state_home, _data_home, _entrypoint, _payload, _verified = staged_evidence_layout
    integration = state_home / "codex-usage/integration"
    artifact = _write_pointer_temp(integration, 0)
    real_fsync = integration_evidence.os.fsync
    fsynced_names: list[str] = []

    def track_fsync(fd):
        fsynced_names.append(_crash_fd_name(fd))
        return real_fsync(fd)

    monkeypatch.setattr(integration_evidence.os, "fsync", track_fsync)
    integration_evidence.recover_evidence_staging(state_home=state_home)

    assert not artifact.exists()
    assert "integration" in fsynced_names


def test_public_recovery_does_not_delete_while_other_process_holds_locks(
    staged_evidence_layout,
):
    """Would fail if public cleanup bypassed cooperative Release/Current locks."""
    from codex_usage import integration_evidence

    state_home, _data_home, _entrypoint, _payload, _verified = staged_evidence_layout
    integration = state_home / "codex-usage/integration"
    artifact = _write_pointer_temp(integration, 0)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_exclusive_evidence_locks,
        args=(str(state_home), ready, release),
    )
    process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(integration_evidence.IntegrationBusy):
            integration_evidence.recover_evidence_staging(state_home=state_home)
        assert artifact.read_bytes() == b"{}"
    finally:
        release.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
    assert process.exitcode == 0

    integration_evidence.recover_evidence_staging(state_home=state_home)
    assert not artifact.exists()


def _crash_fd_name(fd: int) -> str:
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}")).name
    except OSError:
        return ""


def _publish_until_crash(
    scenario: str,
    state_home: str,
    data_home: str,
    payload: bytes,
    verified,
    ready,
    proceed,
) -> None:
    from codex_usage import integration_evidence, private_io
    from codex_usage.private_io import IntegrationEvidenceError

    real_open = os.open
    real_write = os.write
    real_fsync = os.fsync
    real_rename = os.rename
    real_replace = os.replace

    def wait_then_exit_after(operation):
        ready.set()
        if not proceed.wait(10):
            os._exit(91)
        operation()
        os._exit(77)

    def open_file(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if (
            scenario == "pointer_temp_create_before_write"
            and type(path) is str
            and integration_evidence._POINTER_STAGING_RE.fullmatch(path) is not None
            and flags & os.O_CREAT
            and flags & os.O_EXCL
        ):
            ready.set()
            os._exit(77)
        return fd

    def write(fd, data):
        name = _crash_fd_name(fd)
        marker = {
            "payload_write_before_fsync": ".tmp-account-usage-v2.json-",
            "binding_write_before_fsync": ".tmp-account-usage-v2.binding.json-",
            "pointer_temp_short_write_cleanup": ".tmp-current.json-",
        }.get(scenario)
        if marker is not None and name.startswith(marker):
            ready.set()
            if not proceed.wait(10):
                os._exit(91)
            return 0
        return real_write(fd, data)

    def fsync(fd):
        name = _crash_fd_name(fd)
        file_marker = {
            "payload_fsync": ".tmp-account-usage-v2.json-",
            "binding_fsync": ".tmp-account-usage-v2.binding.json-",
            "pointer_temp_fsync": ".tmp-current.json-",
        }.get(scenario)
        if file_marker is not None and name.startswith(file_marker):
            wait_then_exit_after(lambda: real_fsync(fd))
        if (
            scenario == "staging_fsync"
            and name.startswith(".tmp-")
            and sys._getframe(1).f_code.co_name
            == "_publish_evidence_generation_locked"
        ):
            wait_then_exit_after(lambda: real_fsync(fd))
        if scenario == "generations_fsync" and name == "generations":
            wait_then_exit_after(lambda: real_fsync(fd))
        if scenario == "integration_fsync" and name == "integration":
            current = Path(state_home) / "codex-usage/integration/current.json"
            pointer = integration_evidence.parse_pointer(current.read_bytes())
            if pointer.previous_generation_id is not None:
                wait_then_exit_after(lambda: real_fsync(fd))
        return real_fsync(fd)

    def rename(src, dst, *args, **kwargs):
        if (
            scenario == "generation_rename"
            and type(src) is str
            and src.startswith(".tmp-")
            and type(dst) is str
            and integration_evidence._GENERATION_ID_RE.fullmatch(dst) is not None
        ):
            wait_then_exit_after(lambda: real_rename(src, dst, *args, **kwargs))
        return real_rename(src, dst, *args, **kwargs)

    def replace(src, dst, *args, **kwargs):
        if scenario == "pointer_rename" and dst == "current.json":
            wait_then_exit_after(lambda: real_replace(src, dst, *args, **kwargs))
        return real_replace(src, dst, *args, **kwargs)

    os.open = open_file
    os.write = write
    os.fsync = fsync
    os.rename = rename
    os.replace = replace
    private_io.os.open = open_file
    private_io.os.write = write
    private_io.os.fsync = fsync
    integration_evidence.os.fsync = fsync
    integration_evidence.os.rename = rename
    integration_evidence.os.replace = replace
    try:
        integration_evidence.publish_evidence_generation(
            payload,
            state_home=Path(state_home),
            data_home=Path(data_home),
            verified_active_manifest=verified,
        )
    except IntegrationEvidenceError:
        if scenario.endswith("write_before_fsync") or scenario == (
            "pointer_temp_short_write_cleanup"
        ):
            os._exit(0)
        os._exit(92)
    os._exit(93)


def _recover_and_read_after_crash(
    published_evidence_layout,
    scenario: str,
) -> None:
    from codex_usage import integration_evidence

    state_home, data_home, entrypoint, payload, verified, old_current = (
        published_evidence_layout
    )
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    proceed = context.Event()
    child = context.Process(
        target=_publish_until_crash,
        args=(
            scenario,
            str(state_home),
            str(data_home),
            payload,
            verified,
            ready,
            proceed,
        ),
    )
    child.start()
    assert ready.wait(10)
    proceed.set()
    child.join(10)
    normal_cleanup = scenario.endswith("write_before_fsync") or scenario == (
        "pointer_temp_short_write_cleanup"
    )
    assert child.exitcode == (0 if normal_cleanup else 77)

    generations = state_home / "codex-usage/integration/generations"
    if scenario == "staging_fsync":
        debris = [
            entry
            for entry in os.scandir(generations)
            if entry.name.startswith(".tmp-")
        ]
        assert len(debris) == 1
        assert {
            entry.name for entry in os.scandir(generations / debris[0].name)
        } == {
            "account-usage-v2.json",
            "account-usage-v2.binding.json",
            "pool-authority-v2.json",
        }
    integration = state_home / "codex-usage/integration"
    pointer_debris = [
        entry
        for entry in os.scandir(integration)
        if entry.name.startswith(".tmp-current.json-")
    ]
    if scenario in {"pointer_temp_create_before_write", "pointer_temp_fsync"}:
        assert len(pointer_debris) == 1
    if scenario == "pointer_temp_create_before_write":
        item = pointer_debris[0].stat(follow_symlinks=False)
        assert stat.S_ISREG(item.st_mode)
        assert stat.S_IMODE(item.st_mode) == 0o600
        assert item.st_uid == os.geteuid()
        assert item.st_nlink == 1
        assert item.st_size == 0

    integration_evidence.recover_evidence_staging(state_home=state_home)
    if scenario == "pointer_temp_create_before_write":
        integration_evidence.recover_evidence_staging(state_home=state_home)

    assert not any(
        entry.name.startswith(".tmp-current.json-")
        for entry in os.scandir(integration)
    )
    if scenario == "staging_fsync":
        assert not any(
            entry.name.startswith(".tmp-") for entry in os.scandir(generations)
        )

    document, status = integration_evidence.read_current_evidence(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=entrypoint,
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert status == "complete"
    assert document["schema_version"] == 2
    current = state_home / "codex-usage/integration/current.json"
    if scenario == "pointer_temp_create_before_write":
        assert current.read_bytes() == old_current
    current_pointer = integration_evidence.parse_pointer(current.read_bytes())
    old_pointer = integration_evidence.parse_pointer(old_current)
    if scenario in {"pointer_rename", "integration_fsync"}:
        assert current_pointer.previous_generation_id == old_pointer.current_generation_id
    else:
        assert current_pointer == old_pointer
    if scenario == "pointer_temp_create_before_write":
        published = integration_evidence.publish_evidence_generation(
            payload,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified,
        )
        assert published.previous_generation_id == old_pointer.current_generation_id


class TestCrashRecovery:
    def test_recovery_after_payload_write_before_fsync(
        self, published_evidence_layout
    ):
        _recover_and_read_after_crash(
            published_evidence_layout,
            "payload_write_before_fsync",
        )

    def test_recovery_after_payload_fsync(self, published_evidence_layout):
        _recover_and_read_after_crash(published_evidence_layout, "payload_fsync")

    def test_recovery_after_binding_write_before_fsync(
        self, published_evidence_layout
    ):
        _recover_and_read_after_crash(
            published_evidence_layout,
            "binding_write_before_fsync",
        )

    def test_recovery_after_binding_fsync(self, published_evidence_layout):
        _recover_and_read_after_crash(published_evidence_layout, "binding_fsync")

    def test_recovery_after_staging_fsync(self, published_evidence_layout):
        _recover_and_read_after_crash(published_evidence_layout, "staging_fsync")

    def test_recovery_after_generation_rename(self, published_evidence_layout):
        _recover_and_read_after_crash(published_evidence_layout, "generation_rename")

    def test_recovery_after_generations_fsync(self, published_evidence_layout):
        _recover_and_read_after_crash(published_evidence_layout, "generations_fsync")

    def test_pointer_temp_short_write_uses_normal_cleanup(
        self, published_evidence_layout
    ):
        _recover_and_read_after_crash(
            published_evidence_layout,
            "pointer_temp_short_write_cleanup",
        )

    def test_recovery_after_pointer_temp_create_before_write(
        self, published_evidence_layout
    ):
        _recover_and_read_after_crash(
            published_evidence_layout,
            "pointer_temp_create_before_write",
        )

    def test_recovery_after_pointer_temp_fsync(self, published_evidence_layout):
        _recover_and_read_after_crash(
            published_evidence_layout,
            "pointer_temp_fsync",
        )

    def test_recovery_after_pointer_rename(self, published_evidence_layout):
        _recover_and_read_after_crash(published_evidence_layout, "pointer_rename")

    def test_recovery_after_integration_fsync(self, published_evidence_layout):
        _recover_and_read_after_crash(published_evidence_layout, "integration_fsync")


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
    assert {path.name for path in generation.iterdir()} == {
        "account-usage-v2.json",
        "account-usage-v2.binding.json",
        "pool-authority-v2.json",
    }
    assert (generation / "account-usage-v2.json").read_bytes() == payload_bytes
    binding_bytes = (generation / "account-usage-v2.binding.json").read_bytes()
    binding = integration_evidence.parse_binding(binding_bytes)
    from codex_usage.integration_pool_authority import parse_pool_authority_projection

    authority_bytes = (generation / "pool-authority-v2.json").read_bytes()
    authority = parse_pool_authority_projection(authority_bytes)
    assert authority["generation_id"] == pointer.current_generation_id
    assert authority["release_id"] == verified.release_id
    assert authority["usage_payload_sha256"] == hashlib.sha256(payload_bytes).hexdigest()
    assert authority["usage_binding_sha256"] == hashlib.sha256(
        integration_evidence.serialize_usage_binding(binding)
    ).hexdigest()
    assert binding.pool_authority_sha256 == hashlib.sha256(authority_bytes).hexdigest()
    assert pointer.current_binding_sha256 == hashlib.sha256(binding_bytes).hexdigest()
    assert pointer.previous_generation_id is None
    assert integration_evidence.parse_pointer(
        (state_home / "codex-usage/integration/current.json").read_bytes()
    ) == pointer


def test_publish_missing_or_partial_authority_source_never_commits_current(
    staged_evidence_layout,
):
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceError

    state_home, data_home, _entrypoint, payload_bytes, verified = staged_evidence_layout
    integration = state_home / "codex-usage/integration"
    source = integration / "pool-authority-source-v2.json"
    source.unlink()
    with pytest.raises(IntegrationEvidenceError):
        integration_evidence.publish_evidence_generation(
            payload_bytes,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified,
        )
    assert not (integration / "current.json").exists()

    source.write_bytes(
        b'{"authorities":[{"account_id":"unknown"}],'
        b'"pool_authority_source_schema_version":2}\n'
    )
    source.chmod(0o600)
    with pytest.raises(IntegrationEvidenceError):
        integration_evidence.publish_evidence_generation(
            payload_bytes,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified,
        )
    assert not (integration / "current.json").exists()


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


def test_publish_rejects_generations_parent_swap_before_current_commit(
    published_evidence_layout, monkeypatch
):
    """Would fail if Current could bind a generation in a detached parent FD."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, _entrypoint, payload, verified, current_bytes = (
        published_evidence_layout
    )
    integration = state_home / "codex-usage/integration"
    generations = integration / "generations"

    def swap_generations(_state_home, _integration_fd):
        os.rename(generations, integration / "generations-old")
        generations.mkdir(mode=0o700)

    monkeypatch.setattr(
        integration_evidence,
        "_before_publish_pointer_parent_recheck",
        swap_generations,
    )

    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.publish_evidence_generation(
            payload,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified,
        )

    assert (integration / "current.json").read_bytes() == current_bytes
    assert list(generations.iterdir()) == []


def test_publish_rebinds_generations_after_pointer_temp_validation(
    published_evidence_layout, monkeypatch
):
    """Would fail if generations could rebind between pointer check and rename."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, _entrypoint, payload, verified, current_bytes = (
        published_evidence_layout
    )
    integration = state_home / "codex-usage/integration"
    generations = integration / "generations"
    real_verify = integration_evidence._verify_named_file
    swapped = False

    def verify_then_swap(parent_fd, name, expected, **kwargs):
        nonlocal swapped
        result = real_verify(parent_fd, name, expected, **kwargs)
        if not swapped and name.startswith(".tmp-current.json-"):
            swapped = True
            os.rename(generations, integration / "generations-old")
            generations.mkdir(mode=0o700)
        return result

    monkeypatch.setattr(
        integration_evidence,
        "_verify_named_file",
        verify_then_swap,
    )

    with pytest.raises(IntegrationEvidenceInvalid):
        integration_evidence.publish_evidence_generation(
            payload,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified,
        )

    assert swapped
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
        ("_before_publish_pool_authority_recheck", "pool-authority-v2.json"),
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
    real_recover = integration_evidence._recover_evidence_staging_from_namespace

    def recover(*, generations_fd, namespace):
        events.append("recover")
        return real_recover(
            generations_fd=generations_fd,
            namespace=namespace,
        )

    monkeypatch.setattr(
        integration_evidence,
        "_recover_evidence_staging_from_namespace",
        recover,
    )
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


def test_older_concurrent_invocation_cannot_replace_newer_current(
    published_evidence_layout,
):
    """Would fail if an older invocation could publish after a newer one."""
    from codex_usage import integration_evidence
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, _entrypoint, _payload, verified, _old_current = (
        published_evidence_layout
    )
    newer_payload = integration_evidence.serialize_schema2_document(
        {
            "accounts": [],
            "generated_at": "2026-08-25T10:02:00Z",
            "schema_version": 2,
        }
    )
    older_payload = integration_evidence.serialize_schema2_document(
        {
            "accounts": [],
            "generated_at": "2026-08-25T10:01:00Z",
            "schema_version": 2,
        }
    )
    older_started = threading.Event()
    release_older = threading.Event()
    older_errors: list[type[BaseException]] = []

    def older_invocation() -> None:
        older_started.set()
        release_older.wait(timeout=10)
        try:
            integration_evidence.publish_evidence_generation(
                older_payload,
                state_home=state_home,
                data_home=data_home,
                verified_active_manifest=verified,
            )
        except BaseException as exc:
            older_errors.append(type(exc))

    thread = threading.Thread(target=older_invocation)
    thread.start()
    assert older_started.wait(timeout=10)
    newer_pointer = integration_evidence.publish_evidence_generation(
        newer_payload,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )
    release_older.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert older_errors == [IntegrationEvidenceInvalid]
    current = integration_evidence.parse_pointer(
        (state_home / "codex-usage/integration/current.json").read_bytes()
    )
    assert current == newer_pointer
    generation = (
        state_home
        / "codex-usage/integration/generations"
        / current.current_generation_id
    )
    assert json.loads((generation / "account-usage-v2.json").read_bytes())[
        "generated_at"
    ] == "2026-08-25T10:02:00Z"


def test_publish_teardown_failures_return_committed_pointer_and_attempt_all_cleanup(
    published_evidence_layout,
    monkeypatch,
):
    """Would fail if one teardown failure stopped cleanup or masked commit."""
    from codex_usage import integration_evidence

    state_home, data_home, _entrypoint, payload, verified, old_current = (
        published_evidence_layout
    )
    current = state_home / "codex-usage/integration/current.json"
    real_close = integration_evidence.os.close
    real_flock = integration_evidence.fcntl.flock
    close_attempts: list[int] = []
    unlock_attempts: list[int] = []
    failed_close_fd: int | None = None
    failed_unlock = False

    def fail_first_postcommit_close(fd):
        nonlocal failed_close_fd
        if current.read_bytes() != old_current:
            close_attempts.append(fd)
            if failed_close_fd is None:
                failed_close_fd = fd
                raise OSError("synthetic post-commit close failure")
        return real_close(fd)

    def fail_first_postcommit_unlock(fd, operation):
        nonlocal failed_unlock
        if current.read_bytes() != old_current and operation == integration_evidence.fcntl.LOCK_UN:
            unlock_attempts.append(fd)
            if not failed_unlock:
                failed_unlock = True
                raise OSError("synthetic post-commit unlock failure")
        return real_flock(fd, operation)

    monkeypatch.setattr(integration_evidence.os, "close", fail_first_postcommit_close)
    monkeypatch.setattr(integration_evidence.fcntl, "flock", fail_first_postcommit_unlock)
    try:
        pointer = integration_evidence.publish_evidence_generation(
            payload,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified,
        )
    finally:
        if failed_close_fd is not None:
            try:
                real_close(failed_close_fd)
            except OSError:
                pass
    assert current.read_bytes() != old_current
    assert integration_evidence.parse_pointer(current.read_bytes()) == pointer
    assert len(close_attempts) >= 8
    assert len(unlock_attempts) == 2


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


def test_binding_requires_exact_nested_fields_and_32kib_limit():
    """Would fail if binding parser accepted missing, extra, or oversized bytes."""
    from codex_usage import integration_evidence
    from codex_usage.integration_evidence import EvidenceBinding
    from codex_usage.private_io import IntegrationEvidenceInvalid

    binding = EvidenceBinding(
        active_manifest_sha256="a" * 64,
        binding_schema_version=2,
        generation_id="b" * 32,
        payload_filename="account-usage-v2.json",
        payload_sha256="c" * 64,
        payload_size_bytes=64,
        published_at="2026-08-25T10:00:00Z",
        producer_version="0.6.537",
        release_id="0.6.537-" + "d" * 16,
        source_manifest_sha256="e" * 64,
        usage_binding_schema_version=2,
        pool_authority_filename="pool-authority-v2.json",
        pool_authority_sha256="f" * 64,
        pool_authority_size_bytes=64,
    )

    binding_bytes = integration_evidence.serialize_binding(binding)
    assert integration_evidence.parse_binding(binding_bytes) == binding
    canonical = json.loads(binding_bytes)
    variants = []
    extra_outer = json.loads(binding_bytes)
    extra_outer["future"] = True
    variants.append(extra_outer)
    missing_outer = json.loads(binding_bytes)
    del missing_outer["pool_authority_sha256"]
    variants.append(missing_outer)
    extra_usage = json.loads(binding_bytes)
    extra_usage["usage_binding"]["future"] = True
    variants.append(extra_usage)
    missing_usage = json.loads(binding_bytes)
    del missing_usage["usage_binding"]["payload_sha256"]
    variants.append(missing_usage)
    legacy_outer = dict(canonical["usage_binding"])
    legacy_outer["binding_schema_version"] = 1
    del legacy_outer["usage_binding_schema_version"]
    variants.append(legacy_outer)
    for value in variants:
        with pytest.raises(IntegrationEvidenceInvalid):
            integration_evidence.parse_binding(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            )
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


def _complete_reader_account() -> dict[str, object]:
    return {
        "account_id": "account-1",
        "freshness": {
            "captured_at": "2026-08-25T10:00:00Z",
            "fresh_until": "2026-08-25T10:15:00Z",
            "stale": False,
        },
        "limits": [
            {
                "pool": "main",
                "remaining_percent": 90.0,
                "reset_at": "2026-08-25T12:00:00Z",
                "used_percent": 10.0,
                "window_seconds": 18_000,
            }
        ],
        "status": "ok",
        "tracker_evidence": [
            {
                "coverage": "complete",
                "ema_time_constant_seconds": 3_600,
                "first_sample_at": "2026-08-25T09:45:00Z",
                "last_sample_at": "2026-08-25T10:00:00Z",
                "limit_window_seconds": 18_000,
                "pool": "main",
                "projected_used_percent_at_reset": 11.0,
                "rate_percentage_points_per_second": 0.1,
                "reset_generation": "reset-1",
                "sample_count": 2,
            }
        ],
    }


def test_atomic_publish_keeps_fresh_account_authority_when_peer_is_partial(
    staged_evidence_layout,
):
    from codex_usage import integration_evidence
    from codex_usage.integration_pool_authority import (
        PoolAuthorityRequest,
        evaluate_pool_authority,
    )

    state_home, data_home, entrypoint, _payload, verified = staged_evidence_layout
    healthy = _complete_reader_account()
    partial = json.loads(json.dumps(healthy))
    partial["account_id"] = "account-2"
    partial["status"] = "partial"
    payload = integration_evidence.serialize_schema2_document(
        {
            "accounts": [healthy, partial],
            "generated_at": "2026-08-25T10:00:00Z",
            "schema_version": 2,
        }
    )
    authorities = []
    for account_id in ("account-1", "account-2"):
        authorities.append(
            {
                "account_id": account_id,
                "allowed_lifecycles": ["persistent"],
                "allowed_model_families": ["sol"],
                "hive_available": True,
                "long_running_leadership_eligible": True,
                "persistent_leadership_eligible": True,
                "pool_id": "synthetic-primary",
                "provider": "openai",
                "reasoning_maximum": "max",
                "reasoning_minimum": "medium",
            }
        )
    authority_source = (
        state_home
        / "codex-usage"
        / "integration"
        / "pool-authority-source-v2.json"
    )
    authority_source.write_bytes(
        (
            json.dumps(
                {
                    "authorities": authorities,
                    "pool_authority_source_schema_version": 2,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    )
    authority_source.chmod(0o600)
    pointer = integration_evidence.publish_evidence_generation(
        payload,
        state_home=state_home,
        data_home=data_home,
        verified_active_manifest=verified,
    )
    bundle, status = integration_evidence.read_current_generation_bundle(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=entrypoint,
        now=datetime(2026, 8, 25, 10, 5, tzinfo=UTC),
    )
    assert status == "partial"
    assert bundle is not None
    assert pointer.current_generation_id == bundle.binding.generation_id
    assert {
        account["account_id"] for account in bundle.usage["accounts"]
    } == {
        authority["account_id"]
        for authority in bundle.pool_authority["authorities"]
    } == {"account-1", "account-2"}
    decision_arguments = {
        "now": datetime(2026, 8, 25, 10, 5, tzinfo=UTC),
        "expected_release_id": bundle.binding.release_id,
        "expected_generation_id": bundle.binding.generation_id,
        "expected_usage_payload_sha256": bundle.binding.payload_sha256,
        "expected_usage_binding_sha256": hashlib.sha256(
            integration_evidence.serialize_usage_binding(bundle.binding)
        ).hexdigest(),
    }
    request = PoolAuthorityRequest(
        account_id="account-1",
        pool_id="synthetic-primary",
        provider="openai",
        model_family="sol",
        reasoning="max",
        lifecycle="persistent",
        require_persistent_leadership=True,
        require_long_running_leadership=True,
    )
    authority_bytes = integration_evidence.serialize_pool_authority_projection(
        bundle.pool_authority
    )
    assert evaluate_pool_authority(
        authority_bytes,
        request,
        **decision_arguments,
    )
    assert not evaluate_pool_authority(
        authority_bytes,
        replace(request, account_id="account-2"),
        **decision_arguments,
    )


def _rewrite_reader_file(parent: Path, name: str, payload: bytes) -> None:
    fd = os.open(parent / name, os.O_WRONLY | os.O_TRUNC)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def mutate_evidence_layout(state_home: Path, mutation: str) -> None:
    """Mutate one contract layer while retaining valid private FS metadata."""
    from codex_usage import integration_evidence

    integration = state_home / "codex-usage" / "integration"
    pointer_path = integration / "current.json"
    pointer = integration_evidence.parse_pointer(pointer_path.read_bytes())
    generation = integration / "generations" / pointer.current_generation_id
    payload_path = generation / "account-usage-v2.json"
    binding_path = generation / "account-usage-v2.binding.json"
    if mutation == "missing_binding":
        binding_path.unlink()
        return
    binding = integration_evidence.parse_binding(binding_path.read_bytes())
    if mutation == "payload_hash":
        binding = replace(binding, payload_sha256="f" * 64)
    else:
        document = json.loads(payload_path.read_bytes())
        document["accounts"] = [_complete_reader_account()]
        account = document["accounts"][0]
        if mutation == "freshness_stale":
            account["freshness"]["stale"] = True
        elif mutation == "fresh_until_expired":
            account["freshness"] = {
                "captured_at": "2026-08-25T09:00:00Z",
                "fresh_until": "2026-08-25T09:15:00Z",
                "stale": True,
            }
            account["tracker_evidence"][0]["last_sample_at"] = "2026-08-25T09:00:00Z"
            account["tracker_evidence"][0]["first_sample_at"] = "2026-08-25T08:45:00Z"
            account["tracker_evidence"][0]["coverage"] = "stale"
        elif mutation == "partial_status":
            account["status"] = "partial"
        elif mutation == "missing_complete_trend":
            account["tracker_evidence"][0]["coverage"] = "partial"
        else:
            raise AssertionError(f"unexpected mutation: {mutation}")
        payload = integration_evidence.serialize_schema2_document(document)
        _rewrite_reader_file(generation, "account-usage-v2.json", payload)
        binding = replace(
            binding,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            payload_size_bytes=len(payload),
        )
        from codex_usage.integration_pool_authority import (
            parse_pool_authority_projection,
            serialize_pool_authority_projection,
        )

        authority_path = generation / "pool-authority-v2.json"
        authority = parse_pool_authority_projection(authority_path.read_bytes())
        authority["authorities"] = [
            {
                "account_id": "account-1",
                "allowed_lifecycles": ["persistent"],
                "allowed_model_families": ["sol"],
                "hive_available": True,
                "long_running_leadership_eligible": True,
                "persistent_leadership_eligible": True,
                "pool_id": "synthetic-primary",
                "provider": "openai",
                "reasoning_maximum": "max",
                "reasoning_minimum": "medium",
            }
        ]
        authority["usage_payload_sha256"] = binding.payload_sha256
        authority["usage_binding_sha256"] = hashlib.sha256(
            integration_evidence.serialize_usage_binding(binding)
        ).hexdigest()
        authority_bytes = serialize_pool_authority_projection(authority)
        _rewrite_reader_file(generation, authority_path.name, authority_bytes)
        binding = replace(
            binding,
            pool_authority_sha256=hashlib.sha256(authority_bytes).hexdigest(),
            pool_authority_size_bytes=len(authority_bytes),
        )
    binding_bytes = integration_evidence.serialize_binding(binding)
    _rewrite_reader_file(generation, "account-usage-v2.binding.json", binding_bytes)
    pointer = replace(
        pointer,
        current_binding_sha256=hashlib.sha256(binding_bytes).hexdigest(),
    )
    _rewrite_reader_file(
        integration,
        "current.json",
        integration_evidence.serialize_pointer(pointer),
    )


def test_reader_requires_active_binding_payload_and_pointer_hash_chain(
    published_evidence_layout,
):
    """Would fail if reader accepted an unbound or unattested payload."""
    from codex_usage import integration_evidence

    state_home, data_home, entrypoint, _payload, _verified, _current_bytes = (
        published_evidence_layout
    )
    document, status = integration_evidence.read_current_evidence(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=entrypoint,
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert status == "complete"
    assert document["schema_version"] == 2
    bundle, bundle_status = integration_evidence.read_current_generation_bundle(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=entrypoint,
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert bundle_status == "complete"
    assert bundle is not None
    assert bundle.usage == document
    assert bundle.pool_authority["pool_authority_schema_version"] == 2
    assert bundle.binding.binding_schema_version == 2


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_authority",
        "authority_release_mismatch",
        "authority_generation_mismatch",
        "authority_usage_digest_tamper",
        "authority_usage_binding_tamper",
    ),
)
def test_reader_fails_closed_on_pool_authority_bundle_tampering(
    published_evidence_layout,
    mutation,
):
    from codex_usage import integration_evidence
    from codex_usage.integration_pool_authority import (
        parse_pool_authority_projection,
        serialize_pool_authority_projection,
    )

    state_home, data_home, entrypoint, _payload, _verified, _current_bytes = (
        published_evidence_layout
    )
    integration = state_home / "codex-usage/integration"
    pointer_path = integration / "current.json"
    pointer = integration_evidence.parse_pointer(pointer_path.read_bytes())
    generation = integration / "generations" / pointer.current_generation_id
    authority_path = generation / "pool-authority-v2.json"
    if mutation == "missing_authority":
        authority_path.unlink()
        expected_status = "unavailable"
    else:
        authority = parse_pool_authority_projection(authority_path.read_bytes())
        if mutation == "authority_release_mismatch":
            authority["release_id"] = "0.6.537-" + "d" * 16
        elif mutation == "authority_generation_mismatch":
            authority["generation_id"] = "e" * 32
        elif mutation == "authority_usage_digest_tamper":
            authority["usage_payload_sha256"] = "f" * 64
        else:
            authority["usage_binding_sha256"] = "f" * 64
        authority_bytes = serialize_pool_authority_projection(authority)
        _rewrite_reader_file(generation, "pool-authority-v2.json", authority_bytes)
        binding_path = generation / "account-usage-v2.binding.json"
        binding = replace(
            integration_evidence.parse_binding(binding_path.read_bytes()),
            pool_authority_sha256=hashlib.sha256(authority_bytes).hexdigest(),
            pool_authority_size_bytes=len(authority_bytes),
        )
        binding_bytes = integration_evidence.serialize_binding(binding)
        _rewrite_reader_file(generation, binding_path.name, binding_bytes)
        _rewrite_reader_file(
            integration,
            "current.json",
            integration_evidence.serialize_pointer(
                replace(
                    pointer,
                    current_binding_sha256=hashlib.sha256(binding_bytes).hexdigest(),
                )
            ),
        )
        expected_status = "invalid"
    bundle, status = integration_evidence.read_current_generation_bundle(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=entrypoint,
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert bundle is None
    assert status == expected_status


@pytest.mark.parametrize(
    ("mutation", "status"),
    [
        ("missing_binding", "unavailable"),
        ("payload_hash", "invalid"),
        ("freshness_stale", "stale"),
        ("fresh_until_expired", "stale"),
        ("partial_status", "partial"),
        ("missing_complete_trend", "partial"),
    ],
)
def test_reader_classification_priority(published_evidence_layout, mutation, status):
    """Would fail if invalid/stale/partial precedence changed consumer authority."""
    from codex_usage import integration_evidence

    state_home, data_home, entrypoint, _payload, _verified, _current_bytes = (
        published_evidence_layout
    )
    mutate_evidence_layout(state_home, mutation)
    assert (
        integration_evidence.read_current_evidence(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
            now=datetime(2026, 8, 25, tzinfo=UTC),
        )[1]
        == status
    )


def test_reader_missing_current_pointer_is_unavailable_without_document(
    published_evidence_layout,
):
    """Would fail if reader fell back to prior or unauthenticated payload."""
    from codex_usage import integration_evidence

    state_home, data_home, entrypoint, _payload, _verified, _current_bytes = (
        published_evidence_layout
    )
    (state_home / "codex-usage/integration/current.json").unlink()
    document, status = integration_evidence.read_current_evidence(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=entrypoint,
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert document == {}
    assert status == "unavailable"


def _reader_swap_hook(parent_fd: int, name: str, held_fd: int) -> None:
    payload = os.pread(held_fd, os.fstat(held_fd).st_size, 0)
    _replace_named_file(parent_fd, name, payload)


def test_reader_rejects_current_pointer_inode_swap(published_evidence_layout, monkeypatch):
    """Would fail if Current inode changed between reader fstats."""
    from codex_usage import integration_evidence

    state_home, data_home, entrypoint, _payload, _verified, _current_bytes = (
        published_evidence_layout
    )
    monkeypatch.setattr(
        integration_evidence,
        "_before_reader_current_recheck",
        _reader_swap_hook,
        raising=False,
    )
    document, status = integration_evidence.read_current_evidence(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=entrypoint,
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert document == {}
    assert status in {"unavailable", "invalid"}


def test_reader_rejects_current_pointer_parent_swap(
    published_evidence_layout, monkeypatch
):
    """Would fail if reader escaped captured Current parent directory."""
    from codex_usage import integration_evidence

    state_home, data_home, entrypoint, _payload, _verified, _current_bytes = (
        published_evidence_layout
    )
    integration = state_home / "codex-usage/integration"

    def swap_parent(_state_home, _integration_fd):
        old = integration.with_name("integration-reader-old")
        os.rename(integration, old)
        shutil.copytree(old, integration)
        for directory in (integration, integration / "generations"):
            directory.chmod(0o700)
        for path in integration.rglob("*.json"):
            path.chmod(0o600)

    monkeypatch.setattr(
        integration_evidence,
        "_before_reader_pointer_parent_recheck",
        swap_parent,
        raising=False,
    )
    document, status = integration_evidence.read_current_evidence(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=entrypoint,
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert document == {}
    assert status in {"unavailable", "invalid"}


def test_reader_rejects_generation_directory_swap(
    published_evidence_layout, monkeypatch
):
    """Would fail if generation name rebound while reader held its FD."""
    from codex_usage import integration_evidence

    state_home, data_home, entrypoint, _payload, _verified, _current_bytes = (
        published_evidence_layout
    )

    def swap_generation(generations_fd, generation_id, _generation_fd):
        old_name = f"reader-old-{generation_id}"
        os.rename(
            generation_id,
            old_name,
            src_dir_fd=generations_fd,
            dst_dir_fd=generations_fd,
        )
        os.mkdir(generation_id, mode=0o700, dir_fd=generations_fd)

    monkeypatch.setattr(
        integration_evidence,
        "_before_reader_generation_recheck",
        swap_generation,
        raising=False,
    )
    document, status = integration_evidence.read_current_evidence(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=entrypoint,
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert document == {}
    assert status in {"unavailable", "invalid"}


def _assert_reader_rejects_file_inode_swap(
    published_evidence_layout, monkeypatch, hook_name: str
) -> None:
    """Exercise the real reader with one named file-recheck hook."""
    from codex_usage import integration_evidence

    state_home, data_home, entrypoint, _payload, _verified, _current_bytes = (
        published_evidence_layout
    )
    monkeypatch.setattr(
        integration_evidence, hook_name, _reader_swap_hook, raising=False
    )
    document, status = integration_evidence.read_current_evidence(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=entrypoint,
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert document == {}
    assert status in {"unavailable", "invalid"}


def test_reader_rejects_payload_inode_swap(published_evidence_layout, monkeypatch):
    """Would fail if payload inode changed between reader fstats."""
    _assert_reader_rejects_file_inode_swap(
        published_evidence_layout,
        monkeypatch,
        "_before_reader_payload_recheck",
    )


def test_reader_rejects_binding_inode_swap(published_evidence_layout, monkeypatch):
    """Would fail if Binding inode changed between reader fstats."""
    _assert_reader_rejects_file_inode_swap(
        published_evidence_layout,
        monkeypatch,
        "_before_reader_binding_recheck",
    )


def test_reader_rejects_pool_authority_inode_swap(
    published_evidence_layout, monkeypatch
):
    _assert_reader_rejects_file_inode_swap(
        published_evidence_layout,
        monkeypatch,
        "_before_reader_pool_authority_recheck",
    )


def _late_reader_file_swap(generations_fd: int, generation_id: str, name: str) -> None:
    fd = os.open(
        generation_id,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        dir_fd=generations_fd,
    )
    try:
        payload_fd = os.open(name, os.O_RDONLY, dir_fd=fd)
        try:
            payload = os.read(payload_fd, os.fstat(payload_fd).st_size)
        finally:
            os.close(payload_fd)
        _replace_named_file(fd, name, payload)
    finally:
        os.close(fd)


def _assert_reader_rejects_late_file_swap(
    published_evidence_layout, monkeypatch, name: str, *, previous: bool = False
) -> None:
    """Would fail if final reader phase trusted first file validation."""
    from codex_usage import integration_evidence

    state_home, data_home, entrypoint, payload, verified, _current_bytes = (
        published_evidence_layout
    )
    if previous:
        integration_evidence.publish_evidence_generation(
            payload,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified,
        )

    def late_swap(generations_fd, pointer):
        generation_id = (
            pointer.previous_generation_id
            if previous
            else pointer.current_generation_id
        )
        assert generation_id is not None
        _late_reader_file_swap(generations_fd, generation_id, name)

    monkeypatch.setattr(
        integration_evidence,
        "_before_reader_final_revalidate",
        late_swap,
        raising=False,
    )
    document, status = integration_evidence.read_current_evidence(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=entrypoint,
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert document == {}
    assert status in {"unavailable", "invalid"}


def test_reader_rejects_late_payload_inode_swap(
    published_evidence_layout, monkeypatch
):
    _assert_reader_rejects_late_file_swap(
        published_evidence_layout,
        monkeypatch,
        "account-usage-v2.json",
    )


def test_reader_rejects_late_binding_inode_swap(
    published_evidence_layout, monkeypatch
):
    _assert_reader_rejects_late_file_swap(
        published_evidence_layout,
        monkeypatch,
        "account-usage-v2.binding.json",
    )


def test_reader_rejects_late_pool_authority_inode_swap(
    published_evidence_layout, monkeypatch
):
    _assert_reader_rejects_late_file_swap(
        published_evidence_layout,
        monkeypatch,
        "pool-authority-v2.json",
    )


def test_reader_rejects_late_previous_binding_inode_swap(
    published_evidence_layout, monkeypatch
):
    _assert_reader_rejects_late_file_swap(
        published_evidence_layout,
        monkeypatch,
        "account-usage-v2.binding.json",
        previous=True,
    )


def _hold_exclusive_evidence_locks(state_home: str, ready, release) -> None:
    from codex_usage import integration_evidence

    with integration_evidence.evidence_lock_set(
        state_home=Path(state_home),
        release_mode="exclusive",
        current_mode="exclusive",
        timeout_seconds=5,
        create=False,
    ):
        ready.set()
        release.wait(10)


def test_reader_reports_busy_while_child_holds_exclusive_evidence_locks(
    published_evidence_layout,
):
    """Would fail if an EX-held evidence namespace looked unavailable."""
    from codex_usage import integration_evidence

    state_home, data_home, entrypoint, _payload, _verified, _current_bytes = (
        published_evidence_layout
    )
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    child = context.Process(
        target=_hold_exclusive_evidence_locks,
        args=(str(state_home), ready, release),
    )
    child.start()
    try:
        assert ready.wait(5)
        assert integration_evidence.read_current_evidence(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
            now=datetime(2026, 8, 25, tzinfo=UTC),
        ) == ({}, "busy")
    finally:
        release.set()
        child.join(10)
    assert child.exitcode == 0


def test_reader_fresh_until_expiration_is_stale_after_deadline(
    published_evidence_layout,
):
    """Would fail if fresh_until deadline were ignored or treated inclusively."""
    from codex_usage import integration_evidence

    state_home, data_home, entrypoint, _payload, _verified, _current_bytes = (
        published_evidence_layout
    )
    mutate_evidence_layout(state_home, "partial_status")
    pointer = integration_evidence.parse_pointer(
        (state_home / "codex-usage/integration/current.json").read_bytes()
    )
    generation = state_home / "codex-usage/integration/generations" / pointer.current_generation_id
    payload_path = generation / "account-usage-v2.json"
    document = json.loads(payload_path.read_bytes())
    document["accounts"][0]["status"] = "ok"
    payload = integration_evidence.serialize_schema2_document(document)
    _rewrite_reader_file(generation, "account-usage-v2.json", payload)
    binding_path = generation / "account-usage-v2.binding.json"
    binding = integration_evidence.parse_binding(binding_path.read_bytes())
    binding = replace(
        binding,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        payload_size_bytes=len(payload),
    )
    from codex_usage.integration_pool_authority import (
        parse_pool_authority_projection,
        serialize_pool_authority_projection,
    )

    authority_path = generation / "pool-authority-v2.json"
    authority = parse_pool_authority_projection(authority_path.read_bytes())
    authority["usage_payload_sha256"] = binding.payload_sha256
    authority["usage_binding_sha256"] = hashlib.sha256(
        integration_evidence.serialize_usage_binding(binding)
    ).hexdigest()
    authority_bytes = serialize_pool_authority_projection(authority)
    _rewrite_reader_file(generation, authority_path.name, authority_bytes)
    binding = replace(
        binding,
        pool_authority_sha256=hashlib.sha256(authority_bytes).hexdigest(),
        pool_authority_size_bytes=len(authority_bytes),
    )
    binding_bytes = integration_evidence.serialize_binding(binding)
    _rewrite_reader_file(generation, "account-usage-v2.binding.json", binding_bytes)
    _rewrite_reader_file(
        state_home / "codex-usage/integration",
        "current.json",
        integration_evidence.serialize_pointer(
            replace(
                pointer,
                current_binding_sha256=hashlib.sha256(binding_bytes).hexdigest(),
            )
        ),
    )
    assert integration_evidence.read_current_evidence(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=entrypoint,
        now=datetime(2026, 8, 25, 10, 15, 1, tzinfo=UTC),
    )[1] == "stale"
