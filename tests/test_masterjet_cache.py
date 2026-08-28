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
    rollback = tmp_path / ".control-snapshot-v1.json.rollback-crashed"
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
    rollback = tmp_path / ".control-snapshot-v1.json.rollback-crashed"
    rollback.write_bytes(cache_path(other_root).read_bytes())
    rollback.chmod(0o600)

    with pytest.raises(ControlCacheError, match=r"control\.cache_invalid"):
        cache.save(valid_snapshot(), observed_at=2.0)

    assert cache_path(tmp_path).read_bytes() == original
    assert rollback.exists()


def test_valid_rollback_is_recovered_before_new_publish(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    rollback = tmp_path / ".control-snapshot-v1.json.rollback-crashed"
    cache_path(tmp_path).replace(rollback)

    cache.save(valid_snapshot(), observed_at=2.0)

    assert cache.load(max_age_seconds=10**10).observed_at == 2.0
    assert not rollback.exists()


def test_stale_legacy_temps_are_removed_before_publish(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    stale = [
        tmp_path / ".control-snapshot-v1.json.tmp-first",
        tmp_path / ".control-snapshot-v1.json.tmp-second",
    ]
    for path in stale:
        path.write_bytes(b"partial")
        path.chmod(0o600)

    cache.save(valid_snapshot(), observed_at=1.0)

    assert not any(path.exists() for path in stale)
    assert list(tmp_path.glob("*.json")) == [cache_path(tmp_path)]


def test_unsafe_stale_temp_blocks_publish(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    target = tmp_path.parent / f"{tmp_path.name}-stale-temp-target"
    target.write_bytes(b"outside")
    target.chmod(0o600)
    stale = tmp_path / ".control-snapshot-v1.json.tmp-crashed"
    stale.symlink_to(target)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable"):
        cache.save(valid_snapshot(), observed_at=1.0)

    assert target.read_bytes() == b"outside"
    assert not cache_path(tmp_path).exists()


def test_load_cleans_safe_stale_temp_from_crashed_publish(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    stale = tmp_path / ".control-snapshot-v1.json.tmp-crashed"
    stale.write_bytes(b"partial")
    stale.chmod(0o600)

    loaded = cache.load(max_age_seconds=10**10)

    assert loaded.observed_at == 1.0
    assert not stale.exists()


def test_load_rejects_unsafe_stale_temp(tmp_path: Path) -> None:
    cache = ControlSnapshotCache.for_test(tmp_path)
    cache.save(valid_snapshot(), observed_at=1.0)
    outside = tmp_path.parent / f"{tmp_path.name}-unsafe-load-temp"
    outside.write_bytes(b"outside")
    outside.chmod(0o600)
    stale = tmp_path / ".control-snapshot-v1.json.tmp-crashed"
    stale.symlink_to(outside)

    with pytest.raises(ControlCacheError, match=r"control\.cache_unavailable"):
        cache.load(max_age_seconds=10**10)

    assert outside.read_bytes() == b"outside"


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
