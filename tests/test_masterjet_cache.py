from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
