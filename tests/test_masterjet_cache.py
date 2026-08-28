from __future__ import annotations

import json
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from codex_usage import masterjet_cache as cache_module
from codex_usage import private_io
from codex_usage.masterjet_cache import (
    MAX_CONTROL_SNAPSHOT_BYTES,
    ControlCacheError,
    ControlSnapshot,
    ControlSnapshotCache,
    load_control_snapshot,
    save_control_snapshot,
)
from codex_usage.masterjet_contracts import (
    GoogleControlAccount,
    GoogleControlProject,
    GoogleControlProjectList,
    OpenAIControlAccount,
)


class Clock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def valid_snapshot() -> ControlSnapshot:
    return ControlSnapshot(
        openai_accounts=(
            OpenAIControlAccount(
                ref="openai-one",
                label="OpenAI One",
                enabled=True,
                local_profile_ref="profile-one",
                source_host_ref="host-one",
                auth_state="ready",
                access_expires_at=datetime(2026, 8, 29, tzinfo=UTC),
                credential_generation=4,
                vault_projection_state="current",
                usage_state="available",
            ),
        ),
        google_accounts=(
            GoogleControlAccount(
                ref="google-one",
                label="Google One",
                enabled=True,
                subject_bound=True,
                oauth_state="ready",
                inventory_generation=7,
                quota_state="available",
                project_count=1,
                billing_count=0,
                reload_state="current",
            ),
        ),
        google_projects=(
            GoogleControlProjectList(
                schema_version=1,
                account_ref="google-one",
                inventory_generation=7,
                projects=(
                    GoogleControlProject(
                        ref="project-one",
                        project_name="Amber Harbor",
                        purpose="general",
                        key_name="Velvet Meadow",
                        billing_ref=None,
                        status="active",
                        probe_state="ready",
                        quota_state="available",
                    ),
                ),
            ),
        ),
    )


def cache_path(root: Path) -> Path:
    return root / "control-snapshot-v1.json"


def temp_path(root: Path) -> Path:
    return root / ".control-snapshot-v1.json.tmp"


def rollback_path(root: Path) -> Path:
    return root / ".control-snapshot-v1.json.rollback"


def test_old_snapshot_is_returned_as_stale(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path, clock=lambda: 1_000.0)
    cache.save(valid_snapshot(), observed_at=1.0)

    loaded = cache.load(max_age_seconds=60)

    assert loaded.snapshot == valid_snapshot()
    assert loaded.observed_at == 1.0
    assert loaded.stale is True


def test_cache_rejects_secret_marker(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)

    with pytest.raises(ControlCacheError, match=r"control\.response_private") as caught:
        cache.save({"token": "super-secret-marker"}, observed_at=1.0)  # type: ignore[arg-type]

    assert "super-secret-marker" not in str(caught.value)
    assert "super-secret-marker" not in repr(caught.value)
    assert not list(tmp_path.glob("*.json"))


def test_valid_contract_snapshot_round_trips_in_one_private_file(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path, clock=lambda: 5.0)
    snapshot = valid_snapshot()

    cache.save(snapshot, observed_at=4.0)

    loaded = cache.load(max_age_seconds=2)
    cache_files = list(tmp_path.glob("*.json"))
    assert loaded.snapshot == snapshot
    assert loaded.stale is False
    assert len(cache_files) == 1
    assert stat.S_IMODE(cache_files[0].stat().st_mode) == 0o600
    assert cache_files[0].stat().st_uid == os.geteuid()
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700


def test_module_api_reads_persisted_snapshot_in_new_cache_instance(tmp_path: Path) -> None:
    snapshot = valid_snapshot()

    save_control_snapshot(tmp_path, snapshot, observed_at=1.0)
    loaded = load_control_snapshot(tmp_path, max_age_seconds=60)

    assert loaded.snapshot == snapshot
    assert loaded.stale is True


def test_mutated_contract_projection_is_revalidated_before_write(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    snapshot = valid_snapshot()
    object.__setattr__(snapshot.openai_accounts[0], "label", "Bearer private-token")

    with pytest.raises(ControlCacheError, match=r"control\.response_private") as caught:
        cache.save(snapshot, observed_at=1.0)

    assert "private-token" not in repr(caught.value)
    assert not cache_path(tmp_path).exists()


@pytest.mark.parametrize(
    "observed_at",
    [-1.0, float("nan"), float("inf"), True, 10**1_000],
)
def test_invalid_observed_time_fails_with_code_only_error(
    tmp_path: Path,
    observed_at: object,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)

    with pytest.raises(ControlCacheError, match=r"control\.cache_request_invalid") as caught:
        cache.save(valid_snapshot(), observed_at=observed_at)  # type: ignore[arg-type]
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "max_age",
    [-1.0, float("nan"), float("inf"), True, 10**1_000],
)
def test_invalid_max_age_fails_with_code_only_error(
    tmp_path: Path,
    max_age: object,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)

    with pytest.raises(ControlCacheError, match=r"control\.cache_request_invalid") as caught:
        cache.load(max_age_seconds=max_age)  # type: ignore[arg-type]
    assert caught.value.__context__ is None


@pytest.mark.parametrize("clock_value", [float("nan"), float("inf"), -1.0, 10**1_000])
def test_invalid_clock_fails_closed_without_raw_error(
    tmp_path: Path,
    clock_value: object,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path, clock=lambda: clock_value)  # type: ignore[arg-type,return-value]
    cache.save(valid_snapshot(), observed_at=1.0)

    with pytest.raises(ControlCacheError, match=r"control\.cache_invalid") as caught:
        cache.load(max_age_seconds=60)
    assert caught.value.__context__ is None


def test_clock_rollback_returns_snapshot_as_stale(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path, clock=lambda: 99.0)
    snapshot = valid_snapshot()
    cache.save(snapshot, observed_at=100.0)

    loaded = cache.load(max_age_seconds=60)

    assert loaded.snapshot == snapshot
    assert loaded.stale is True


@pytest.mark.parametrize(
    "corrupt",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":1,"observed_at":NaN,"snapshot":{}}',
        b'{"schema_version":1',
        b'{"schema_version":2,"observed_at":1,"snapshot":{}}',
    ],
)
def test_duplicate_nonfinite_truncated_and_unknown_schema_fail_closed(
    tmp_path: Path,
    corrupt: bytes,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    cache_path(tmp_path).write_bytes(corrupt)
    cache_path(tmp_path).chmod(0o600)

    with pytest.raises(ControlCacheError, match=r"control\.cache_invalid") as caught:
        cache.load(max_age_seconds=60)
    assert caught.value.__context__ is None


def test_oversized_cache_fails_closed(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    cache_path(tmp_path).write_bytes(b"x" * (MAX_CONTROL_SNAPSHOT_BYTES + 1))
    cache_path(tmp_path).chmod(0o600)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable"):
        cache.load(max_age_seconds=60)


def test_corruption_is_not_overwritten_automatically(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    corrupt = b'{"schema_version":1'
    cache_path(tmp_path).write_bytes(corrupt)
    cache_path(tmp_path).chmod(0o600)

    with pytest.raises(ControlCacheError, match=r"control\.cache_invalid"):
        cache.save(valid_snapshot(), observed_at=2.0)

    assert cache_path(tmp_path).read_bytes() == corrupt


def test_failed_atomic_replace_preserves_existing_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    original = cache_path(tmp_path).read_bytes()

    def fail_replace(_source: object, destination: object) -> None:
        if destination == cache_path(tmp_path):
            raise OSError("injected replace failure")
        raise AssertionError("unexpected replace target")

    monkeypatch.setattr(private_io.os, "replace", fail_replace)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable") as caught:
        cache.save(valid_snapshot(), observed_at=2.0)

    assert "injected" not in repr(caught.value)
    assert cache_path(tmp_path).read_bytes() == original
    assert not list(tmp_path.glob(".control-snapshot-v1.json.*"))


def test_cache_file_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path.parent / f"{tmp_path.name}-outside"
    target.write_bytes(b"outside-marker")
    target.chmod(0o600)
    cache_path(tmp_path).symlink_to(target)
    cache = ControlSnapshotCache.for_test(tmp_path)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable"):
        cache.save(valid_snapshot(), observed_at=1.0)

    assert target.read_bytes() == b"outside-marker"


def test_cache_hardlink_and_wrong_mode_fail_closed(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    os.link(cache_path(tmp_path), tmp_path / "extra-link")

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable"):
        cache.load(max_age_seconds=60)

    (tmp_path / "extra-link").unlink()
    cache_path(tmp_path).chmod(0o640)
    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable"):
        cache.load(max_age_seconds=60)


def test_non_private_cache_root_is_rejected(tmp_path: Path) -> None:
    tmp_path.chmod(0o750)

    try:
        with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable"):
            ControlSnapshotCache.for_test(tmp_path)
    finally:
        tmp_path.chmod(0o700)


def test_unknown_persisted_fields_are_rejected(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    document = json.loads(cache_path(tmp_path).read_text())
    document["raw_server_text"] = "private diagnostic"
    cache_path(tmp_path).write_text(json.dumps(document))
    cache_path(tmp_path).chmod(0o600)

    with pytest.raises(ControlCacheError, match=r"control\.cache_invalid"):
        cache.load(max_age_seconds=60)


def test_cache_root_beneath_user_writable_parent_is_rejected(tmp_path: Path) -> None:
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o700)
    unsafe_parent.chmod(0o777)

    try:
        with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable"):
            ControlSnapshotCache.for_test(unsafe_parent / "cache")
    finally:
        unsafe_parent.chmod(0o700)


def test_non_normalized_cache_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "child" / ".." / "cache"

    with pytest.raises(ControlCacheError, match=r"control\.cache_request_invalid"):
        ControlSnapshotCache.for_test(root)

    assert not (tmp_path / "child").exists()


def test_publish_remains_bound_to_attested_root_during_namespace_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cache"
    displaced = tmp_path / "displaced-cache"
    cache = ControlSnapshotCache.for_test(root)
    cache.save(valid_snapshot(), observed_at=1.0)
    original_replace = cache_module.os.replace
    swapped = False

    def swap_root_then_replace(
        source: object,
        destination: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal swapped
        source_path = Path(str(source))
        destination_path = Path(str(destination))
        if not swapped and destination_path.name == cache_path(root).name:
            original_replace(root, displaced)
            root.mkdir(mode=0o700)
            cache_path(root).write_bytes(b"attacker-target")
            cache_path(root).chmod(0o600)
            if source_path.is_absolute():
                original_replace(displaced / source_path.name, root / source_path.name)
            swapped = True
        original_replace(source, destination, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cache_module.os, "replace", swap_root_then_replace)

    try:
        cache.save(valid_snapshot(), observed_at=2.0)
    except ControlCacheError:
        pass

    assert swapped is True
    assert cache_path(root).read_bytes() == b"attacker-target"


def test_temp_name_swap_after_fsync_is_rejected_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    original = cache_path(tmp_path).read_bytes()
    outside = tmp_path.parent / f"{tmp_path.name}-temp-swap"
    outside.write_bytes(b"outside")
    outside.chmod(0o600)
    original_fsync = cache_module.os.fsync
    swapped = False

    def swap_temp_after_fsync(fd: int) -> None:
        nonlocal swapped
        original_fsync(fd)
        if swapped:
            return
        descriptor_path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if descriptor_path.name.startswith(".control-snapshot-v1.json.tmp"):
            descriptor_path.unlink()
            descriptor_path.symlink_to(outside)
            swapped = True

    monkeypatch.setattr(cache_module.os, "fsync", swap_temp_after_fsync)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable"):
        cache.save(valid_snapshot(), observed_at=2.0)

    assert swapped is True
    assert cache_path(tmp_path).read_bytes() == original
    assert outside.read_bytes() == b"outside"


def test_target_swap_after_validation_blocks_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cache"
    cache = ControlSnapshotCache.for_test(root)
    original_fsync = cache_module.os.fsync
    swapped = False

    def swap_target_after_temp_fsync(fd: int) -> None:
        nonlocal swapped
        original_fsync(fd)
        if swapped:
            return
        descriptor_path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if descriptor_path.name.startswith(".control-snapshot-v1.json.tmp"):
            cache_path(root).write_bytes(b"attacker-target")
            cache_path(root).chmod(0o600)
            swapped = True

    monkeypatch.setattr(cache_module.os, "fsync", swap_target_after_temp_fsync)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable"):
        cache.save(valid_snapshot(), observed_at=2.0)

    assert swapped is True
    assert cache_path(root).read_bytes() == b"attacker-target"


def test_missing_target_publish_never_overwrites_newly_created_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    original_link = cache_module.os.link
    original_replace = cache_module.os.replace
    injected = False

    def inject_target() -> None:
        nonlocal injected
        if injected:
            return
        cache_path(tmp_path).write_bytes(b"newly-created-target")
        cache_path(tmp_path).chmod(0o600)
        injected = True

    def conflict_link(
        source: object,
        destination: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        if Path(str(destination)).name == cache_path(tmp_path).name:
            inject_target()
        original_link(source, destination, *args, **kwargs)  # type: ignore[arg-type]

    def conflict_replace(
        source: object,
        destination: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        if Path(str(destination)).name == cache_path(tmp_path).name:
            inject_target()
        original_replace(source, destination, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cache_module.os, "link", conflict_link)
    monkeypatch.setattr(cache_module.os, "replace", conflict_replace)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable"):
        cache.save(valid_snapshot(), observed_at=1.0)

    assert injected is True
    assert cache_path(tmp_path).read_bytes() == b"newly-created-target"


@pytest.mark.parametrize(
    "rollback_payload",
    [
        b'{"schema_version":1',
        b'{ "observed_at": 1.0, "schema_version": 1, "snapshot": '
        b'{"google_accounts": [], "google_projects": [], "openai_accounts": []}}',
    ],
)
def test_invalid_rollback_evidence_blocks_recovery_and_overwrite(
    tmp_path: Path,
    rollback_payload: bytes,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    rollback = rollback_path(tmp_path)
    rollback.write_bytes(rollback_payload)
    rollback.chmod(0o600)

    with pytest.raises(ControlCacheError, match=r"control\.cache_invalid"):
        cache.save(valid_snapshot(), observed_at=2.0)

    assert rollback.read_bytes() == rollback_payload
    assert not cache_path(tmp_path).exists()


def test_divergent_rollback_beside_target_blocks_overwrite(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    original = cache_path(tmp_path).read_bytes()
    other_root = tmp_path / "other"
    other_cache = ControlSnapshotCache.for_test(other_root)
    other_cache.save(valid_snapshot(), observed_at=9.0)
    rollback = rollback_path(tmp_path)
    rollback.write_bytes(cache_path(other_root).read_bytes())
    rollback.chmod(0o600)

    with pytest.raises(ControlCacheError, match=r"control\.cache_invalid"):
        cache.save(valid_snapshot(), observed_at=2.0)

    assert cache_path(tmp_path).read_bytes() == original
    assert rollback.exists()


def test_valid_rollback_is_recovered_before_new_publish(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    rollback = rollback_path(tmp_path)
    cache_path(tmp_path).replace(rollback)

    cache.save(valid_snapshot(), observed_at=2.0)

    assert cache.load(max_age_seconds=10**10).observed_at == 2.0
    assert not rollback.exists()


def test_rollback_recovery_never_overwrites_newly_created_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    rollback = rollback_path(tmp_path)
    cache_path(tmp_path).replace(rollback)
    original_link = cache_module.os.link
    injected = False

    def conflict_link(
        source: object,
        destination: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal injected
        if Path(str(destination)).name == cache_path(tmp_path).name and not injected:
            cache_path(tmp_path).write_bytes(b"newly-created-target")
            cache_path(tmp_path).chmod(0o600)
            injected = True
        original_link(source, destination, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cache_module.os, "link", conflict_link)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable"):
        cache.save(valid_snapshot(), observed_at=2.0)

    assert injected is True
    assert cache_path(tmp_path).read_bytes() == b"newly-created-target"
    assert rollback.exists()


def test_stale_deterministic_temp_is_removed_before_publish(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    stale = temp_path(tmp_path)
    stale.write_bytes(b"partial")
    stale.chmod(0o600)

    cache.save(valid_snapshot(), observed_at=1.0)

    assert not stale.exists()
    assert list(tmp_path.glob("*.json")) == [cache_path(tmp_path)]


def test_unsafe_stale_temp_blocks_publish(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    target = tmp_path.parent / f"{tmp_path.name}-stale-temp-target"
    target.write_bytes(b"outside")
    target.chmod(0o600)
    stale = temp_path(tmp_path)
    stale.symlink_to(target)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable"):
        cache.save(valid_snapshot(), observed_at=1.0)

    assert target.read_bytes() == b"outside"
    assert not cache_path(tmp_path).exists()


def test_load_cleans_safe_stale_temp_from_crashed_publish(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    stale = temp_path(tmp_path)
    stale.write_bytes(b"partial")
    stale.chmod(0o600)

    loaded = cache.load(max_age_seconds=10**10)

    assert loaded.observed_at == 1.0
    assert not stale.exists()


def test_load_completes_create_only_publish_after_link_crash(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    os.link(cache_path(tmp_path), temp_path(tmp_path))

    loaded = cache.load(max_age_seconds=10**10)

    assert loaded.observed_at == 1.0
    assert not temp_path(tmp_path).exists()
    assert cache_path(tmp_path).stat().st_nlink == 1


def test_load_rejects_unsafe_stale_temp(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    outside = tmp_path.parent / f"{tmp_path.name}-unsafe-load-temp"
    outside.write_bytes(b"outside")
    outside.chmod(0o600)
    stale = temp_path(tmp_path)
    stale.symlink_to(outside)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable"):
        cache.load(max_age_seconds=10**10)

    assert outside.read_bytes() == b"outside"


def test_similarly_named_file_is_never_scanned_or_deleted(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    unrelated = tmp_path / ".control-snapshot-v1.json.tmp-user-note"
    unrelated.write_bytes(b"keep")
    unrelated.chmod(0o600)

    cache.save(valid_snapshot(), observed_at=1.0)

    assert unrelated.read_bytes() == b"keep"


def test_load_completes_rollback_recovery_after_link_crash(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    rollback = rollback_path(tmp_path)
    cache_path(tmp_path).replace(rollback)
    os.link(rollback, cache_path(tmp_path))

    loaded = cache.load(max_age_seconds=10**10)

    assert loaded.observed_at == 1.0
    assert not rollback.exists()
    assert cache_path(tmp_path).stat().st_nlink == 1


def test_early_fchmod_failure_cleans_owned_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    original_fchmod = cache_module.os.fchmod

    def fail_temp_fchmod(fd: int, mode: int) -> None:
        descriptor = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if descriptor.name == temp_path(tmp_path).name:
            raise OSError("private fchmod diagnostic")
        original_fchmod(fd, mode)

    monkeypatch.setattr(cache_module.os, "fchmod", fail_temp_fchmod)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable") as caught:
        cache.save(valid_snapshot(), observed_at=1.0)

    assert "diagnostic" not in repr(caught.value)
    assert not temp_path(tmp_path).exists()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_baseexception_during_write_is_preserved_and_cleans_owned_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    original_write = cache_module.os.write
    original_close = cache_module.os.close

    def interrupt_temp_write(fd: int, payload: bytes) -> int:
        descriptor = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if descriptor.name == temp_path(tmp_path).name:
            raise interruption("primary interrupt")
        return original_write(fd, payload)

    def fail_temp_close(fd: int) -> None:
        descriptor = Path(os.readlink(f"/proc/self/fd/{fd}"))
        original_close(fd)
        if descriptor.name == temp_path(tmp_path).name:
            raise OSError("secondary close diagnostic")

    monkeypatch.setattr(cache_module.os, "write", interrupt_temp_write)
    monkeypatch.setattr(cache_module.os, "close", fail_temp_close)

    with pytest.raises(interruption, match="primary interrupt"):
        cache.save(valid_snapshot(), observed_at=1.0)

    assert not temp_path(tmp_path).exists()


def test_close_failure_does_not_skip_owned_temp_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    original_write = cache_module.os.write
    original_close = cache_module.os.close
    close_failed = False

    def fail_temp_write(fd: int, payload: bytes) -> int:
        descriptor = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if descriptor.name == temp_path(tmp_path).name:
            raise OSError("private write diagnostic")
        return original_write(fd, payload)

    def fail_temp_close(fd: int) -> None:
        nonlocal close_failed
        descriptor = Path(os.readlink(f"/proc/self/fd/{fd}"))
        original_close(fd)
        if descriptor.name == temp_path(tmp_path).name and not close_failed:
            close_failed = True
            raise OSError("private close diagnostic")

    monkeypatch.setattr(cache_module.os, "write", fail_temp_write)
    monkeypatch.setattr(cache_module.os, "close", fail_temp_close)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable") as caught:
        cache.save(valid_snapshot(), observed_at=1.0)

    assert close_failed is True
    assert "diagnostic" not in repr(caught.value)
    assert not temp_path(tmp_path).exists()


def test_constructor_root_close_failure_is_code_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cache"
    original_close = cache_module.os.close
    failed = False

    def fail_root_close(fd: int) -> None:
        nonlocal failed
        descriptor = Path(os.readlink(f"/proc/self/fd/{fd}"))
        original_close(fd)
        if descriptor == root and not failed:
            failed = True
            raise OSError("private root close diagnostic")

    monkeypatch.setattr(cache_module.os, "close", fail_root_close)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable") as caught:
        ControlSnapshotCache.for_test(root)

    assert failed is True
    assert "diagnostic" not in repr(caught.value)
    assert caught.value.__context__ is None


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("operation", ["init", "save", "load"])
def test_close_baseexception_is_exact_primary_across_public_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    interruption: type[BaseException],
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    if operation == "load":
        cache_path(tmp_path).replace(rollback_path(tmp_path))
    original_close = cache_module.os.close
    primary = interruption("close primary")
    later_closed: list[Path] = []
    failed = False
    target = (
        tmp_path
        if operation == "init"
        else temp_path(tmp_path)
        if operation == "save"
        else rollback_path(tmp_path)
    )

    def interrupt_owned_close(fd: int) -> None:
        nonlocal failed
        descriptor = Path(os.readlink(f"/proc/self/fd/{fd}"))
        original_close(fd)
        if not failed and descriptor == target:
            failed = True
            raise primary
        if failed:
            later_closed.append(descriptor)

    monkeypatch.setattr(cache_module.os, "close", interrupt_owned_close)

    with pytest.raises(interruption) as caught:
        if operation == "init":
            ControlSnapshotCache.for_test(tmp_path)
        elif operation == "save":
            cache.save(valid_snapshot(), observed_at=2.0)
        else:
            cache.load(max_age_seconds=10**10)

    assert caught.value is primary
    assert failed is True
    assert not temp_path(tmp_path).exists()
    if operation != "init":
        assert tmp_path in later_closed


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_close_helper_returns_exact_nonexception_primary(
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
) -> None:
    primary = interruption("close primary")
    monkeypatch.setattr(cache_module.os, "close", lambda _fd: (_ for _ in ()).throw(primary))

    assert ControlSnapshotCache._close_owned_fd(123, None) is primary


@pytest.mark.parametrize("operation", ["save", "load"])
def test_public_root_close_failure_is_code_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    original_close = cache_module.os.close
    failed = False

    def fail_root_close(fd: int) -> None:
        nonlocal failed
        descriptor = Path(os.readlink(f"/proc/self/fd/{fd}"))
        original_close(fd)
        if descriptor == tmp_path and not failed:
            failed = True
            raise OSError("private public-root close diagnostic")

    monkeypatch.setattr(cache_module.os, "close", fail_root_close)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable") as caught:
        if operation == "save":
            cache.save(valid_snapshot(), observed_at=2.0)
        else:
            cache.load(max_age_seconds=10**10)

    assert failed is True
    assert caught.value.__context__ is None


def test_root_traversal_close_failure_still_closes_next_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "nested" / "cache"
    original_close = cache_module.os.close
    failed = False
    closed_after_failure: list[Path] = []

    def fail_first_traversal_close(fd: int) -> None:
        nonlocal failed
        descriptor = Path(os.readlink(f"/proc/self/fd/{fd}"))
        original_close(fd)
        if failed:
            closed_after_failure.append(descriptor)
        elif descriptor == Path("/"):
            failed = True
            raise OSError("private traversal close diagnostic")

    monkeypatch.setattr(cache_module.os, "close", fail_first_traversal_close)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable") as caught:
        ControlSnapshotCache.for_test(root)

    assert caught.value.__context__ is None
    assert Path("/tmp") in closed_after_failure


def test_recovery_read_close_failure_is_code_only_and_root_is_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    cache_path(tmp_path).replace(rollback_path(tmp_path))
    original_close = cache_module.os.close
    failed = False
    root_closed = False

    def fail_rollback_close(fd: int) -> None:
        nonlocal failed, root_closed
        descriptor = Path(os.readlink(f"/proc/self/fd/{fd}"))
        original_close(fd)
        if descriptor == rollback_path(tmp_path) and not failed:
            failed = True
            raise OSError("private rollback close diagnostic")
        if descriptor == tmp_path:
            root_closed = True

    monkeypatch.setattr(cache_module.os, "close", fail_rollback_close)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable") as caught:
        cache.load(max_age_seconds=10**10)

    assert failed is True
    assert root_closed is True
    assert caught.value.__context__ is None
    assert rollback_path(tmp_path).exists()


@pytest.mark.parametrize(
    "unsafe_label",
    [
        "Cookie session=topsecret",
        "Authorization Basic dXNlcjpwYXNz",
        "api_key=topsecret",
        "api-key topsecret",
        "password=huntertwo",
        "passwd huntertwo",
        "token=topsecret",
        "secret topsecret",
        "header x-private-value",
        "upstream error private diagnostic",
        "setCookie=session-value",
        "sessionId=topsecret",
        "cookieHeader=session-value",
        "clientPassword=huntertwo",
        "pwd=huntertwo",
        "apikey=topsecret",
        "sessionid=topsecret",
        "setcookie=value",
        "cookieheader=value",
        "clientpassword=huntertwo",
        "clientsecret=topsecret",
        "accesstoken=topsecret",
        "refreshtoken=topsecret",
        "SeSsIoNiD=topsecret",
        "header apikey topsecret",
        "auth sessionid topsecret",
        "authorization clientsecret topsecret",
        "cookie setcookie topsecret",
        "error clientsecret topsecret",
        "diagnostic accesstoken topsecret",
        "header SeSsIoNiD topsecret",
        "auth ClIeNtSeCrEt topsecret",
        "error AcCeSsToKeN topsecret",
        "cookie SeTcOoKiE value",
        "diagnostic ClIeNtPaSsWoRd value",
    ],
)
def test_common_secret_header_cookie_and_error_markers_are_not_cached(
    tmp_path: Path,
    unsafe_label: str,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    snapshot = valid_snapshot()
    object.__setattr__(snapshot.openai_accounts[0], "label", unsafe_label)

    with pytest.raises(ControlCacheError, match=r"control\.response_private") as caught:
        cache.save(snapshot, observed_at=1.0)

    assert unsafe_label not in repr(caught.value)
    assert not cache_path(tmp_path).exists()


@pytest.mark.parametrize(
    "benign_label",
    ["Secret Garden", "Cookie Monster", "Error Recovery", "Session Work"],
)
def test_benign_visible_names_with_marker_words_remain_cacheable(
    tmp_path: Path,
    benign_label: str,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    snapshot = valid_snapshot()
    object.__setattr__(snapshot.openai_accounts[0], "label", benign_label)

    cache.save(snapshot, observed_at=1.0)

    assert cache.load(max_age_seconds=10**10).snapshot == snapshot


def test_semantically_valid_noncanonical_json_is_rejected(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    document = json.loads(cache_path(tmp_path).read_bytes())
    cache_path(tmp_path).write_text(json.dumps(document, indent=2))
    cache_path(tmp_path).chmod(0o600)

    with pytest.raises(ControlCacheError, match=r"control\.cache_invalid"):
        cache.load(max_age_seconds=60)


def test_excessive_json_depth_is_rejected_before_stdlib_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    cache_path(tmp_path).write_bytes(b"[" * 33 + b"0" + b"]" * 33)
    cache_path(tmp_path).chmod(0o600)

    def unexpected_decode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("stdlib decoder crossed application depth budget")

    monkeypatch.setattr(cache_module.json, "loads", unexpected_decode)

    with pytest.raises(ControlCacheError, match=r"control\.cache_invalid"):
        cache.load(max_age_seconds=60)


def test_oversized_integer_lexeme_is_rejected(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    raw = (
        b'{"observed_at":' + b"9" * 100 + b',"schema_version":1,"snapshot":{"google_accounts":[],'
        b'"google_projects":[],"openai_accounts":[]}}'
    )
    cache_path(tmp_path).write_bytes(raw)
    cache_path(tmp_path).chmod(0o600)

    with pytest.raises(ControlCacheError, match=r"control\.cache_invalid"):
        cache.load(max_age_seconds=60)


def test_age_is_measured_after_waiting_for_cache_lock(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    observed = time.time()
    cache.save(valid_snapshot(), observed_at=observed)
    started = threading.Event()

    def load_while_locked() -> object:
        started.set()
        return cache.load(max_age_seconds=0.05)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with private_io.private_path_lock(cache_path(tmp_path)):
            future = executor.submit(load_while_locked)
            assert started.wait(timeout=1)
            time.sleep(0.15)

        loaded = future.result(timeout=1)
    assert loaded.stale is True  # type: ignore[attr-defined]


def test_task9_offline_snapshot_is_read_only_and_stale_after_30_seconds(
    tmp_path: Path,
) -> None:
    clock = Clock(1_000.0)
    cache = ControlSnapshotCache.for_test(tmp_path, clock=clock)
    snapshot = valid_snapshot()
    cache.save(snapshot, observed_at=1_000.0)
    persisted = cache_path(tmp_path).read_bytes()

    clock.value = 1_030.0
    boundary = cache.load(max_age_seconds=30)
    clock.value = 1_030.001
    offline = cache.load(max_age_seconds=30)

    assert boundary.snapshot == snapshot
    assert boundary.stale is False
    assert offline.snapshot == snapshot
    assert offline.stale is True
    assert cache_path(tmp_path).read_bytes() == persisted
    assert b"task9-synthetic-auth" not in persisted
