from __future__ import annotations

import ast
import builtins
import hashlib
import io
import json
import multiprocessing
import os
import queue
import shutil
import stat
import sys
import types
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

import pytest

from codex_usage.integration_snapshot import (
    IntegrationBusy,
    IntegrationInvalidSource,
    IntegrationSecureIOError,
    IntegrationUnavailable,
)

NOW = datetime(2026, 8, 15, 10, 5, tzinfo=UTC)
ARGV = ("integration-snapshot", "--schema", "2", "--format", "json")


def _private_source_copy_for_process_contract(destination_root: Path) -> Path:
    """Copy the exact live producer input set into a private test source root."""
    from codex_usage.integration_installer import SOURCE_MANIFEST_FILES

    project_root = Path(__file__).resolve().parents[1]
    source_root = destination_root / "source"
    source_root.mkdir(mode=0o700)
    source_root.chmod(0o700)
    for relative_text in SOURCE_MANIFEST_FILES:
        source = project_root / relative_text
        target = source_root / relative_text
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.parent.chmod(0o700)
        shutil.copyfile(source, target)
        target.chmod(0o600)
    return source_root


def _real_entrypoint_layout_for_process_contract(tmp_path: Path) -> tuple[
    dict[str, str], Path, Path
]:
    """Install an attested, fully real producer release in hermetic test roots."""
    from codex_usage.integration_installer import install_release

    data_home = tmp_path / "data"
    state_home = tmp_path / "state"
    temporary_root = tmp_path / "temporary"
    for path in (data_home, state_home, temporary_root):
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    (data_home / "codex-usage" / "current").mkdir(parents=True, mode=0o700)
    (data_home / "codex-usage").chmod(0o700)
    (data_home / "codex-usage" / "current").chmod(0o700)
    release = install_release(
        source_root=_private_source_copy_for_process_contract(tmp_path),
        state_home=state_home,
        data_home=data_home,
        python_executable=Path(sys.executable),
        temporary_root=temporary_root,
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
    return (
        {"XDG_DATA_HOME": str(data_home), "XDG_STATE_HOME": str(state_home)},
        release.entrypoint_path,
        state_home / "codex-usage" / "integration" / "current.json",
    )


def _run_real_entrypoint_in_process(
    environ: dict[str, str],
    expected_entrypoint: str,
    snapshot_seen: object | None,
    continue_after_snapshot: object | None,
    results: object,
) -> None:
    """Run the real entrypoint; only the deterministic test barrier is replaced."""
    from codex_usage import integration_entrypoint
    from codex_usage.integration_attestation import verify_active_manifest_at

    original_read = integration_entrypoint._read_current_source_snapshot
    first_read = True

    def read_current(path: Path):
        nonlocal first_read
        snapshot = original_read(path)
        if first_read and snapshot_seen is not None:
            first_read = False
            snapshot_seen.set()
            if continue_after_snapshot is None or not continue_after_snapshot.wait(15):
                raise RuntimeError("test producer barrier was not released")
        return snapshot

    integration_entrypoint._read_current_source_snapshot = read_current
    try:
        result = integration_entrypoint.execute(
            ARGV,
            environ=environ,
            clock=lambda: NOW,
            expected_entrypoint_path=Path(expected_entrypoint),
            verifier=lambda state_home, data_home, entrypoint: verify_active_manifest_at(
                state_home=state_home,
                data_home=data_home,
                expected_entrypoint_path=entrypoint,
            ),
        )
        results.put((result.exit_code, result.stderr.decode("ascii")))
    except BaseException as exc:
        results.put(("error", type(exc).__name__, str(exc)))


def _write_history_in_process(
    history_path: str,
    pointer_path: str,
    attempted: object,
    completed: object,
) -> None:
    """Exercise HistoryStore's real source-lock and SQLite/WAL write path."""
    from codex_usage.history import HistoryStore, UsageSample

    attempted.set()
    try:
        with HistoryStore(Path(history_path)) as store:
            pointer_visible_before_write = Path(pointer_path).is_file()
            count = store.record_many(
                (
                    UsageSample(
                        account_id="interleaving",
                        pool="main",
                        window_seconds=18_000,
                        captured_at=NOW,
                        used_percent=1,
                        source="two-process-test",
                    ),
                )
            )
        completed.put(("ok", count, pointer_visible_before_write))
    except BaseException as exc:
        completed.put(("error", type(exc).__name__, str(exc)))


def _write_history_then_hold_source_lock_in_process(
    history_path: str,
    held: object,
    release: object,
    completed: object,
) -> None:
    """Create real pre-entrypoint History drift while retaining the source lock."""
    from codex_usage.history import HistoryStore, UsageSample
    from codex_usage.source_lock import source_lock

    try:
        path = Path(history_path)
        with source_lock(path.parent, timeout_seconds=5):
            with HistoryStore(path) as store:
                count = store.record_many(
                    (
                        UsageSample(
                            account_id="prelock",
                            pool="main",
                            window_seconds=18_000,
                            captured_at=NOW,
                            used_percent=2,
                            source="two-process-test",
                        ),
                    )
                )
            held.set()
            if not release.wait(15):
                raise RuntimeError("test history-holder barrier was not released")
        completed.put(("ok", count))
    except BaseException as exc:
        completed.put(("error", type(exc).__name__, str(exc)))


def _join_process_or_fail(process: multiprocessing.Process, *, label: str) -> None:
    process.join(20)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail(f"{label} did not terminate")
    assert process.exitcode == 0, f"{label} exited {process.exitcode}"


@pytest.fixture(autouse=True)
def _stub_entrypoint_evidence_locks(monkeypatch):
    from codex_usage import integration_entrypoint

    real_lock_set = integration_entrypoint.evidence_lock_set

    @contextmanager
    def unlocked(**_kwargs):
        yield

    monkeypatch.setattr(integration_entrypoint, "evidence_lock_set", unlocked)
    return real_lock_set


@pytest.mark.parametrize("code", [True, 69.0, "69"])
def test_error_result_rejects_non_integer_error_codes(code):
    from codex_usage.integration_entrypoint import _error_result

    result = _error_result(code)

    assert result.exit_code == 69


def test_require_aware_utc_rejects_non_datetime():
    from codex_usage.integration_entrypoint import _require_aware_utc

    with pytest.raises(ValueError):
        _require_aware_utc("invalid")


def _environment(tmp_path: Path) -> dict[str, str]:
    data_home = tmp_path / "data"
    state_home = tmp_path / "state"
    (data_home / "codex-usage" / "current").mkdir(parents=True, mode=0o700)
    (state_home / "codex-usage" / "integration").mkdir(parents=True, mode=0o700)
    for path in (
        data_home,
        data_home / "codex-usage",
        data_home / "codex-usage/current",
        state_home,
        state_home / "codex-usage",
        state_home / "codex-usage/integration",
    ):
        path.chmod(0o700)
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
    return {"XDG_DATA_HOME": str(data_home), "XDG_STATE_HOME": str(state_home)}


def _clock_counter():
    calls: list[None] = []

    def now() -> datetime:
        calls.append(None)
        return NOW

    return now, calls


def _payload() -> bytes:
    return b'{"accounts":[],"generated_at":"2026-08-15T10:05:00Z","schema_version":2}'


def _source_snapshot(usages, *, spark_source_evidence: bool = False):
    from codex_usage.integration_snapshot import (
        CurrentSourceRecordBinding,
        CurrentSourceSnapshot,
    )
    from codex_usage.source_lock import SourceFileBinding, SourceRootIdentity

    file_binding = SourceFileBinding(
        device=1,
        inode=2,
        mode=0o600,
        uid=os.geteuid(),
        gid=os.getegid(),
        ctime_ns=1,
        mtime_ns=1,
        size_bytes=2,
        sha256="a" * 64,
    )
    return CurrentSourceSnapshot(
        usages=tuple(usages),
        current_directory=SourceRootIdentity(1, 2, 0o700, os.geteuid(), os.getegid()),
        records=tuple(
            CurrentSourceRecordBinding(
                account_id=usage.account_id,
                current_file=file_binding,
                state_generation=usage.state_generation or 0,
                state_generation_file=None,
                has_spark_source_evidence=spark_source_evidence,
            )
            for usage in usages
        ),
    )


def _spark_source_snapshot():
    from codex_usage.integration_snapshot import (
        CurrentSourceRecordBinding,
        CurrentSourceSnapshot,
    )
    from codex_usage.source_lock import SourceFileBinding, SourceRootIdentity

    file_binding = SourceFileBinding(
        device=1,
        inode=2,
        mode=0o600,
        uid=os.geteuid(),
        gid=os.getegid(),
        ctime_ns=1,
        mtime_ns=1,
        size_bytes=2,
        sha256="a" * 64,
    )
    return CurrentSourceSnapshot(
        usages=(),
        current_directory=SourceRootIdentity(1, 2, 0o700, os.geteuid(), os.getegid()),
        records=(
            CurrentSourceRecordBinding(
                account_id="alpha",
                current_file=file_binding,
                state_generation=0,
                state_generation_file=None,
                has_spark_source_evidence=True,
            ),
        ),
    )


def _expected_entrypoint(tmp_path: Path) -> Path:
    path = tmp_path / "release" / "venv" / "lib" / "codex_usage" / "integration_entrypoint.py"
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_bytes(b"# synthetic entrypoint\n")
    path.chmod(0o600)
    return path


def _verified_manifest(tmp_path: Path, entrypoint: Path | None = None):
    """Synthetic attested 0.6.538 manifest for entrypoint isolation tests."""
    from codex_usage.integration_attestation import ActiveRelease, VerifiedActiveManifest
    from codex_usage.private_io import FileIdentity

    entrypoint = entrypoint or _expected_entrypoint(tmp_path)
    active_bytes = b'{"release_id":"0.6.538-aaaaaaaaaaaaaaaa","version":"0.6.538"}'
    identity = FileIdentity(1, 2, 0o700)
    return VerifiedActiveManifest(
        active_release=ActiveRelease(
            version="0.6.538",
            release_dir=entrypoint.parents[3],
            launcher_path=entrypoint.parents[3] / "bin/codex-usage-integration",
            entrypoint_path=entrypoint,
            entrypoint_sha256="b" * 64,
            wheel_sha256="c" * 64,
            record_sha256="d" * 64,
            launcher_sha256="e" * 64,
            release_tree_sha256="f" * 64,
        ),
        release_id="0.6.538-aaaaaaaaaaaaaaaa",
        source_manifest_sha256="1" * 64,
        active_manifest_bytes=active_bytes,
        active_manifest_sha256=hashlib.sha256(active_bytes).hexdigest(),
        state_home_identity=identity,
        integration_parent_identity=FileIdentity(1, 3, 0o700),
        active_file_identity=FileIdentity(1, 4, 0o600),
    )


def test_execute_rejects_every_nonexact_argv_before_verifier_or_source(tmp_path):
    from codex_usage.integration_entrypoint import execute

    calls: list[str] = []
    result = execute(
        ("integration-snapshot", "--schema", "1", "--format", "json"),
        environ=_environment(tmp_path),
        clock=lambda: NOW,
        expected_entrypoint_path=_expected_entrypoint(tmp_path),
        verifier=lambda *_: calls.append("verify"),
    )
    assert result.exit_code == 64
    assert result.stdout == b""
    assert result.stderr == b"integration_snapshot_invalid_arguments\n"
    assert calls == []


def test_execute_rejects_spark_source_before_staging_or_publisher(tmp_path, monkeypatch):
    """D297 never normalizes Spark evidence into a V2 generation."""
    from codex_usage import integration_entrypoint
    from codex_usage.integration_entrypoint import execute

    environment = _environment(tmp_path)
    entrypoint = _expected_entrypoint(tmp_path)
    verified = _verified_manifest(tmp_path, entrypoint)
    calls: list[str] = []
    monkeypatch.setattr(
        integration_entrypoint,
        "_read_current_source_snapshot",
        lambda _path: _spark_source_snapshot(),
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "_publish_evidence_generation_locked",
        lambda *_args, **_kwargs: calls.append("publish"),
    )

    result = execute(
        ARGV,
        environ=environment,
        clock=lambda: NOW,
        expected_entrypoint_path=entrypoint,
        verifier=lambda *_args: verified,
    )

    assert result.exit_code == 65
    assert result.stderr == b"integration_snapshot_invalid_source\n"
    assert calls == []


def test_execute_rejects_selected_spark_history_before_publisher(tmp_path, monkeypatch):
    from codex_usage import integration_entrypoint
    from codex_usage.integration_entrypoint import (
        HistorySourceBinding,
        execute,
    )

    environment = _environment(tmp_path)
    entrypoint = _expected_entrypoint(tmp_path)
    verified = _verified_manifest(tmp_path, entrypoint)
    calls: list[str] = []
    monkeypatch.setattr(
        integration_entrypoint,
        "_read_current_source_snapshot",
        lambda _path: _source_snapshot(()),
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "_load_tracker_samples_with_binding",
        lambda *_args: (
            {},
            HistorySourceBinding(None, None, None, (), True),
        ),
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "_publish_evidence_generation_locked",
        lambda *_args, **_kwargs: calls.append("publish"),
    )

    result = execute(
        ARGV,
        environ=environment,
        clock=lambda: NOW,
        expected_entrypoint_path=entrypoint,
        verifier=lambda *_args: verified,
    )

    assert result.exit_code == 65
    assert result.stderr == b"integration_snapshot_invalid_source\n"
    assert calls == []


def test_execute_rejects_spark_that_appears_at_final_publisher_revalidation(
    tmp_path, monkeypatch
):
    """The final pre-pointer callback has the same RC65 fail-closed seam."""
    from codex_usage import integration_entrypoint
    from codex_usage.integration_entrypoint import execute

    environment = _environment(tmp_path)
    entrypoint = _expected_entrypoint(tmp_path)
    verified = _verified_manifest(tmp_path, entrypoint)
    snapshots = [_source_snapshot(()), _source_snapshot(()), _spark_source_snapshot()]
    calls: list[str] = []
    monkeypatch.setattr(
        integration_entrypoint,
        "_read_current_source_snapshot",
        lambda _path: snapshots.pop(0),
    )

    def publisher(*_args, **kwargs):
        calls.append("publisher")
        kwargs["source_input_revalidator"]()

    monkeypatch.setattr(
        integration_entrypoint,
        "_publish_evidence_generation_locked",
        publisher,
    )

    result = execute(
        ARGV,
        environ=environment,
        clock=lambda: NOW,
        expected_entrypoint_path=entrypoint,
        verifier=lambda *_args: verified,
    )

    assert result.exit_code == 65
    assert result.stderr == b"integration_snapshot_invalid_source\n"
    assert calls == ["publisher"]


@pytest.mark.parametrize("argv", [None, 1, object()])
def test_execute_rejects_non_sequence_argv(tmp_path, argv):
    from codex_usage.integration_entrypoint import execute

    result = execute(
        argv,  # type: ignore[arg-type]
        environ=_environment(tmp_path),
        clock=lambda: NOW,
        expected_entrypoint_path=_expected_entrypoint(tmp_path),
        verifier=lambda *_: pytest.fail("verifier"),
    )

    assert result == type(result)(64, b"", b"integration_snapshot_invalid_arguments\n")


def test_execute_rejects_string_subclass_argv_before_comparison(tmp_path):
    from codex_usage.integration_entrypoint import execute

    class BrokenStr(str):
        def __eq__(self, _other):
            raise RuntimeError("synthetic argv comparison marker")

    argv = (
        BrokenStr("integration-snapshot"),
        "--schema",
        "2",
        "--format",
        "json",
    )

    result = execute(
        argv,
        environ=_environment(tmp_path),
        clock=lambda: NOW,
        expected_entrypoint_path=_expected_entrypoint(tmp_path),
        verifier=lambda *_: pytest.fail("verifier"),
    )

    assert result == type(result)(64, b"", b"integration_snapshot_invalid_arguments\n")


def test_execute_rejects_argv_iterator_failure_before_source(tmp_path):
    from codex_usage.integration_entrypoint import execute

    class BrokenArgv:
        def __iter__(self):
            raise RuntimeError("synthetic argv iterator marker")

    result = execute(
        BrokenArgv(),  # type: ignore[arg-type]
        environ=_environment(tmp_path),
        clock=lambda: NOW,
        expected_entrypoint_path=_expected_entrypoint(tmp_path),
        verifier=lambda *_: pytest.fail("verifier"),
    )

    assert result == type(result)(64, b"", b"integration_snapshot_invalid_arguments\n")


def test_execute_verifies_before_and_after_then_publishes_once(tmp_path, monkeypatch):
    from codex_usage import integration_entrypoint

    events: list[str] = []
    verifier_args: list[tuple[Path, Path, Path]] = []
    expected_entrypoint = _expected_entrypoint(tmp_path)

    @contextmanager
    def locked(**_kwargs):
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    monkeypatch.setattr(integration_entrypoint, "evidence_lock_set", locked)

    monkeypatch.setattr(
        integration_entrypoint,
        "_read_current_source_snapshot",
        lambda _: events.append("read") or _source_snapshot(()),
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "build_schema2_document",
        lambda *args, **kwargs: events.append("build")
        or {"schema_version": 2, "generated_at": "2026-08-15T10:05:00Z", "accounts": []},
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "serialize_schema2_document",
        lambda _: events.append("serialize") or _payload(),
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "_publish_evidence_generation_locked",
        lambda payload, **kwargs: events.append("publish"),
    )

    clock, clock_calls = _clock_counter()

    verified = _verified_manifest(tmp_path, expected_entrypoint)

    def verifier(state_home: Path, data_home: Path, expected: Path):
        events.append("verify")
        verifier_args.append((state_home, data_home, expected))
        return verified

    result = integration_entrypoint.execute(
        ARGV,
        environ=_environment(tmp_path),
        clock=clock,
        expected_entrypoint_path=expected_entrypoint,
        verifier=verifier,
    )
    assert result == integration_entrypoint.CommandResult(0, _payload(), b"")
    assert events == [
        "lock-enter",
        "verify",
            "read",
            "build",
            "serialize",
            "read",
            "verify",
        "publish",
        "lock-exit",
    ]
    assert len(clock_calls) == 1
    assert len(verifier_args) == 2
    assert verifier_args[0][2] is expected_entrypoint
    assert verifier_args[1][2] is expected_entrypoint


def test_entrypoint_uses_release_then_current_exclusive_lock_set(
    tmp_path, monkeypatch, _stub_entrypoint_evidence_locks
):
    """Would fail if entrypoint publication bypassed ordered two-lock transaction."""
    from codex_usage import integration_entrypoint, integration_evidence, private_io
    from codex_usage.private_io import FileIdentity

    environ = _environment(tmp_path)
    state_home = Path(environ["XDG_STATE_HOME"])
    integration = state_home / "codex-usage/integration"
    (integration / "generations").mkdir(mode=0o700)
    active = integration / "active.json"
    active.write_bytes(b"staged-active")
    active.chmod(0o600)
    expected_entrypoint = _expected_entrypoint(tmp_path)
    staged = _verified_manifest(tmp_path, expected_entrypoint)
    state_item = state_home.lstat()
    integration_item = integration.lstat()
    active_item = active.lstat()
    staged = replace(
        staged,
        state_home_identity=FileIdentity(
            state_item.st_dev,
            state_item.st_ino,
            stat.S_IMODE(state_item.st_mode),
            state_item.st_gid,
        ),
        integration_parent_identity=FileIdentity(
            integration_item.st_dev,
            integration_item.st_ino,
            stat.S_IMODE(integration_item.st_mode),
            integration_item.st_gid,
        ),
        active_file_identity=FileIdentity(
            active_item.st_dev,
            active_item.st_ino,
            stat.S_IMODE(active_item.st_mode),
            active_item.st_gid,
        ),
    )
    lock_root = tmp_path / "lock-root"
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)
    private_io.ensure_private_directory(lock_root, label="test lock root")
    for target in (integration / "producer-install", integration / "current.json"):
        lock_path = lock_root / integration_evidence._evidence_lock_name(target)
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)

    monkeypatch.setattr(
        integration_entrypoint,
        "_read_current_source_snapshot",
        lambda _: _source_snapshot(()),
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "build_schema2_document",
        lambda *args, **kwargs: {
            "schema_version": 2,
            "generated_at": "2026-08-15T10:05:00Z",
            "accounts": [],
        },
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "_publish_evidence_generation_locked",
        lambda *_args, **_kwargs: None,
    )
    lock_calls: list[dict[str, object]] = []

    @contextmanager
    def observed_lock_set(**kwargs):
        lock_calls.append(kwargs)
        with _stub_entrypoint_evidence_locks(**kwargs):
            yield

    monkeypatch.setattr(integration_entrypoint, "evidence_lock_set", observed_lock_set)
    result = integration_entrypoint.execute(
        ARGV,
        environ=environ,
        clock=lambda: NOW,
        expected_entrypoint_path=expected_entrypoint,
        verifier=lambda *_: staged,
    )
    assert result.exit_code == 0
    assert lock_calls == [
        {
            "state_home": state_home,
            "release_mode": "exclusive",
            "current_mode": "exclusive",
            "timeout_seconds": 0,
            "create": False,
        }
    ]
    assert not (integration / "account-usage-v1.json").exists()


@pytest.mark.parametrize(
    ("error", "code", "token"),
    [
        (IntegrationInvalidSource(), 65, b"integration_snapshot_invalid_source\n"),
        (IntegrationUnavailable(), 69, b"integration_snapshot_unavailable\n"),
        (IntegrationSecureIOError(), 70, b"integration_snapshot_secure_io_failed\n"),
        (IntegrationBusy(), 75, b"integration_snapshot_busy\n"),
    ],
)
def test_execute_normalizes_known_failures_without_details(
    tmp_path,
    monkeypatch,
    error,
    code,
    token,
):
    from codex_usage import integration_entrypoint

    monkeypatch.setattr(
        integration_entrypoint,
        "_read_current_source_snapshot",
        lambda _: (_ for _ in ()).throw(error),
    )
    result = integration_entrypoint.execute(
        ARGV,
        environ=_environment(tmp_path),
        clock=lambda: NOW,
        expected_entrypoint_path=_expected_entrypoint(tmp_path),
        verifier=lambda *_: None,
    )
    assert result == integration_entrypoint.CommandResult(code, b"", token)
    assert b"tmp" not in result.stderr
    assert b"alpha" not in result.stderr


def test_execute_normalizes_broad_failures_without_details(tmp_path, monkeypatch):
    from codex_usage import integration_entrypoint

    monkeypatch.setattr(
        integration_entrypoint,
        "_read_current_source_snapshot",
        lambda _: (_ for _ in ()).throw(RuntimeError("tmp alpha secret marker")),
    )
    result = integration_entrypoint.execute(
        ARGV,
        environ=_environment(tmp_path),
        clock=lambda: NOW,
        expected_entrypoint_path=_expected_entrypoint(tmp_path),
        verifier=lambda *_: None,
    )
    assert result == integration_entrypoint.CommandResult(
        69,
        b"",
        b"integration_snapshot_unavailable\n",
    )
    assert b"tmp" not in result.stderr
    assert b"alpha" not in result.stderr
    assert b"secret" not in result.stderr


def test_execute_maps_busy_lock_to_retryable_error(tmp_path, monkeypatch):
    from codex_usage import integration_entrypoint
    from codex_usage.integration_evidence import IntegrationBusy as EvidenceBusy

    monkeypatch.setattr(
        integration_entrypoint,
        "evidence_lock_set",
        contextmanager(
            lambda **_kwargs: (_ for _ in ()).throw(EvidenceBusy())
        ),
    )

    result = integration_entrypoint.execute(
        ARGV,
        environ=_environment(tmp_path),
        clock=lambda: NOW,
        expected_entrypoint_path=_expected_entrypoint(tmp_path),
        verifier=lambda *_: _verified_manifest(tmp_path),
    )

    assert result == integration_entrypoint.CommandResult(
        75,
        b"",
        b"integration_snapshot_busy\n",
    )


@pytest.mark.parametrize(
    "timezone",
    [
        type(
            "NoOffsetTimezone",
            (tzinfo,),
            {"utcoffset": lambda self, value: None},
        ),
        type(
            "RaisingTimezone",
            (tzinfo,),
            {
                "utcoffset": lambda self, value: (_ for _ in ()).throw(
                    RuntimeError("synthetic timezone marker")
                )
            },
        ),
    ],
)
def test_execute_rejects_invalid_timezone_before_source_read(tmp_path, monkeypatch, timezone):
    from codex_usage import integration_entrypoint

    monkeypatch.setattr(
        integration_entrypoint,
        "_read_current_source_snapshot",
        lambda _: pytest.fail("source/read must not run"),
    )
    clock_calls: list[None] = []

    def clock() -> datetime:
        clock_calls.append(None)
        return datetime(2026, 8, 15, 10, 5, tzinfo=timezone())

    result = integration_entrypoint.execute(
        ARGV,
        environ=_environment(tmp_path),
        clock=clock,
        expected_entrypoint_path=_expected_entrypoint(tmp_path),
        verifier=lambda *_: None,
    )
    assert result == integration_entrypoint.CommandResult(
        70,
        b"",
        b"integration_snapshot_secure_io_failed\n",
    )
    assert len(clock_calls) == 1


def test_execute_rejects_clock_with_failing_astimezone_before_source_read(tmp_path, monkeypatch):
    from codex_usage import integration_entrypoint

    class BrokenDatetime(datetime):
        def astimezone(self, tz=None):
            raise RuntimeError("synthetic astimezone marker")

    monkeypatch.setattr(
        integration_entrypoint,
        "_read_current_source_snapshot",
        lambda _: pytest.fail("source/read must not run"),
    )
    result = integration_entrypoint.execute(
        ARGV,
        environ=_environment(tmp_path),
        clock=lambda: BrokenDatetime(2026, 8, 15, 10, 5, tzinfo=UTC),
        expected_entrypoint_path=_expected_entrypoint(tmp_path),
        verifier=lambda *_: None,
    )

    assert result == integration_entrypoint.CommandResult(
        70,
        b"",
        b"integration_snapshot_secure_io_failed\n",
    )


def test_execute_rejects_clock_with_failing_tzinfo_before_source_read(tmp_path, monkeypatch):
    from codex_usage import integration_entrypoint

    class BrokenDatetime(datetime):
        @property
        def tzinfo(self):
            raise RuntimeError("synthetic tzinfo marker")

    monkeypatch.setattr(
        integration_entrypoint,
        "_read_current_source_snapshot",
        lambda _: pytest.fail("source/read must not run"),
    )
    result = integration_entrypoint.execute(
        ARGV,
        environ=_environment(tmp_path),
        clock=lambda: BrokenDatetime(2026, 8, 15, 10, 5, tzinfo=UTC),
        expected_entrypoint_path=_expected_entrypoint(tmp_path),
        verifier=lambda *_: None,
    )

    assert result == integration_entrypoint.CommandResult(
        70,
        b"",
        b"integration_snapshot_secure_io_failed\n",
    )


def test_execute_exports_tracker_evidence_from_bounded_history_series(tmp_path, monkeypatch):
    from codex_usage import integration_entrypoint
    from codex_usage.history import HistoryStore, UsageSample
    from codex_usage.models import AccountUsage, LimitWindow

    environ = _environment(tmp_path)
    paths = integration_entrypoint._runtime_paths(environ)
    now = NOW
    reset_at = now + timedelta(hours=2)
    points = (
        (now - timedelta(minutes=30), 10),
        (now - timedelta(minutes=15), 20),
        (now, 30),
    )
    with HistoryStore(paths.history_path) as store:
        store.record_many(
            tuple(
                UsageSample(
                    account_id="alpha",
                    pool="main",
                    window_seconds=18_000,
                    captured_at=captured_at,
                    used_percent=used_percent,
                    reset_at=reset_at,
                    reset_generation=reset_at.isoformat(),
                    source="test",
                )
                for captured_at, used_percent in points
            )
        )
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=now,
        five_hour=LimitWindow(
            name="5h", remaining=70, reset_at=reset_at, duration_seconds=18_000
        ),
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "_read_current_source_snapshot",
        lambda _: _source_snapshot((usage,)),
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "_publish_evidence_generation_locked",
        lambda *args, **kwargs: None,
    )

    result = integration_entrypoint.execute(
        ARGV,
        environ=environ,
        clock=lambda: now,
        expected_entrypoint_path=_expected_entrypoint(tmp_path),
        verifier=lambda *_: _verified_manifest(tmp_path),
    )

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    evidence = document["accounts"][0]["tracker_evidence"]
    assert evidence[0]["pool"] == "main"
    assert evidence[0]["limit_window_seconds"] == 18_000
    assert evidence[0]["sample_count"] == 3
    assert evidence[0]["coverage"] == "complete"


def test_real_entrypoint_source_lock_serializes_history_writer_and_fails_closed_prelock(
    tmp_path: Path,
) -> None:
    """Would fail if a real SQLite/WAL HistoryStore write bypassed D297's source lock.

    The first process holds the actual entrypoint's outer lock after its real
    Current snapshot.  A second process uses the real HistoryStore API and
    must not write until that entrypoint has published.  Separately, a
    HistoryStore writer which already owns the source lock must make the
    entrypoint return its documented busy result without moving the pointer.
    """
    environ, expected_entrypoint, pointer_path = _real_entrypoint_layout_for_process_contract(
        tmp_path
    )
    context = multiprocessing.get_context("fork")
    snapshot_seen = context.Event()
    resume_entrypoint = context.Event()
    writer_attempted = context.Event()
    producer_results = context.Queue()
    writer_results = context.Queue()
    producer = context.Process(
        target=_run_real_entrypoint_in_process,
        args=(
            environ,
            str(expected_entrypoint),
            snapshot_seen,
            resume_entrypoint,
            producer_results,
        ),
    )
    writer = context.Process(
        target=_write_history_in_process,
        args=(
            str(Path(environ["XDG_DATA_HOME"]) / "codex-usage" / "usage-history.sqlite3"),
            str(pointer_path),
            writer_attempted,
            writer_results,
        ),
    )
    holder_release = context.Event()
    holder_held = context.Event()
    holder_results = context.Queue()
    holder: multiprocessing.Process | None = None
    blocked_results = context.Queue()
    blocked: multiprocessing.Process | None = None
    started: list[multiprocessing.Process] = []
    try:
        producer.start()
        started.append(producer)
        assert snapshot_seen.wait(15), "entrypoint never reached its real Current snapshot"

        writer.start()
        started.append(writer)
        assert writer_attempted.wait(10), "HistoryStore writer never attempted access"
        with pytest.raises(queue.Empty):
            writer_results.get(timeout=0.3)

        resume_entrypoint.set()
        _join_process_or_fail(producer, label="locked entrypoint")
        _join_process_or_fail(writer, label="blocked history writer")
        assert producer_results.get(timeout=2) == (0, "")
        assert writer_results.get(timeout=2) == ("ok", 1, True)
        pointer_before_prelock_writer = pointer_path.read_bytes()

        holder = context.Process(
            target=_write_history_then_hold_source_lock_in_process,
            args=(
                str(
                    Path(environ["XDG_DATA_HOME"])
                    / "codex-usage"
                    / "usage-history.sqlite3"
                ),
                holder_held,
                holder_release,
                holder_results,
            ),
        )
        holder.start()
        started.append(holder)
        assert holder_held.wait(15), "pre-lock HistoryStore writer did not own source lock"

        blocked = context.Process(
            target=_run_real_entrypoint_in_process,
            args=(
                environ,
                str(expected_entrypoint),
                None,
                None,
                blocked_results,
            ),
        )
        blocked.start()
        started.append(blocked)
        _join_process_or_fail(blocked, label="pre-lock entrypoint")
        assert blocked_results.get(timeout=2) == (
            75,
            "integration_snapshot_busy\n",
        )
        assert pointer_path.read_bytes() == pointer_before_prelock_writer

        holder_release.set()
        _join_process_or_fail(holder, label="pre-lock history writer")
        assert holder_results.get(timeout=2) == ("ok", 1)
    finally:
        resume_entrypoint.set()
        holder_release.set()
        for process in started:
            if process.is_alive():
                process.join(1)
            if process.is_alive():
                process.terminate()
                process.join(5)


def test_real_entrypoint_rejects_raw_current_disk_drift_before_pointer(
    tmp_path: Path, monkeypatch
) -> None:
    """Would fail if final source revalidation trusted a stale Current binding.

    This deliberately does not use a cooperating producer writer: the test
    replaces the on-disk Current record directly at the final pointer seam.
    It proves the independent hostile-drift defense; it is not a HistoryStore
    participant or an alternative to the two-process source-lock contract.
    """
    from codex_usage import integration_entrypoint, integration_evidence
    from codex_usage.integration_attestation import verify_active_manifest_at
    from codex_usage.models import AccountUsage
    from codex_usage.state import save_current_usage

    environ, expected_entrypoint, pointer_path = _real_entrypoint_layout_for_process_contract(
        tmp_path
    )
    current_dir = Path(environ["XDG_DATA_HOME"]) / "codex-usage" / "current"
    authority_source = (
        Path(environ["XDG_STATE_HOME"])
        / "codex-usage"
        / "integration"
        / "pool-authority-source-v2.json"
    )
    authority_source.write_bytes(
        (
            json.dumps(
                {
                    "authorities": [
                        {
                            "account_id": "alpha",
                            "allowed_lifecycles": [
                                "ephemeral",
                                "persistent",
                                "session",
                            ],
                            "allowed_model_families": ["sol"],
                            "hive_available": True,
                            "long_running_leadership_eligible": True,
                            "persistent_leadership_eligible": True,
                            "pool_id": "test",
                            "provider": "openai",
                            "reasoning_maximum": "max",
                            "reasoning_minimum": "low",
                        }
                    ],
                    "pool_authority_source_schema_version": 2,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
    )
    authority_source.chmod(0o600)
    original = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=NOW,
        backend_configured="direct",
        backend_used="direct",
    )
    current_file = save_current_usage(original, current_dir)

    def verifier(state_home: Path, data_home: Path, entrypoint: Path):
        return verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )

    baseline = integration_entrypoint.execute(
        ARGV,
        environ=environ,
        clock=lambda: NOW,
        expected_entrypoint_path=expected_entrypoint,
        verifier=verifier,
    )
    assert baseline.exit_code == 0
    old_pointer = pointer_path.read_bytes()
    old_item = current_file.stat()
    old_digest = hashlib.sha256(current_file.read_bytes()).hexdigest()

    replacement_dir = tmp_path / "hostile-current"
    replacement_dir.mkdir(mode=0o700)
    replacement_dir.chmod(0o700)
    replacement = save_current_usage(
        AccountUsage(
            account_id="alpha",
            label="Alpha changed by hostile raw writer",
            captured_at=NOW + timedelta(minutes=1),
            backend_configured="direct",
            backend_used="direct",
        ),
        replacement_dir,
    )
    replacement_digest = hashlib.sha256(replacement.read_bytes()).hexdigest()
    assert replacement_digest != old_digest
    replaced = False

    def raw_hostile_disk_drift(_state_home: Path, _integration_fd: int) -> None:
        nonlocal replaced
        if not replaced:
            os.replace(replacement, current_file)
            replaced = True

    monkeypatch.setattr(
        integration_evidence,
        "_before_publish_pointer_parent_recheck",
        raw_hostile_disk_drift,
    )
    result = integration_entrypoint.execute(
        ARGV,
        environ=environ,
        clock=lambda: NOW + timedelta(minutes=1),
        expected_entrypoint_path=expected_entrypoint,
        verifier=verifier,
    )

    assert replaced
    new_item = current_file.stat()
    assert (new_item.st_dev, new_item.st_ino) != (old_item.st_dev, old_item.st_ino)
    assert hashlib.sha256(current_file.read_bytes()).hexdigest() == replacement_digest
    assert result.exit_code == 70
    assert result.stderr == b"integration_snapshot_secure_io_failed\n"
    assert pointer_path.read_bytes() == old_pointer


def test_execute_rejects_missing_or_relative_xdg_roots_before_lock(tmp_path):
    from codex_usage.integration_entrypoint import execute

    for environ in (
        {},
        {"XDG_DATA_HOME": "relative", "XDG_STATE_HOME": str(tmp_path)},
        {"XDG_DATA_HOME": "", "XDG_STATE_HOME": str(tmp_path)},
    ):
        result = execute(
            ARGV,
            environ=environ,
            clock=lambda: NOW,
            expected_entrypoint_path=_expected_entrypoint(tmp_path),
            verifier=lambda *_: pytest.fail("verifier"),
        )
        assert result.exit_code == 70
        assert result.stdout == b""


def test_runtime_paths_use_only_the_two_absolute_xdg_roots(tmp_path):
    from codex_usage.integration_entrypoint import RuntimePaths, _runtime_paths

    environ = _environment(tmp_path)
    data_home = Path(environ["XDG_DATA_HOME"])
    state_home = Path(environ["XDG_STATE_HOME"])
    assert _runtime_paths(environ) == RuntimePaths(
        data_home=data_home,
        state_home=state_home,
        current_dir=data_home / "codex-usage" / "current",
        history_path=data_home / "codex-usage" / "usage-history.sqlite3",
        integration_dir=state_home / "codex-usage" / "integration",
    )


def test_execute_does_not_publish_when_post_verifier_detects_drift(tmp_path, monkeypatch):
    from codex_usage import integration_entrypoint

    monkeypatch.setattr(
        integration_entrypoint,
        "_read_current_source_snapshot",
        lambda _: _source_snapshot(()),
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "build_schema2_document",
        lambda *args, **kwargs: {
            "schema_version": 2,
            "generated_at": "2026-08-15T10:05:00Z",
            "accounts": [],
        },
    )
    monkeypatch.setattr(integration_entrypoint, "serialize_schema2_document", lambda _: _payload())
    monkeypatch.setattr(
        integration_entrypoint,
        "_publish_evidence_generation_locked",
        lambda *args, **kwargs: pytest.fail("evidence publish"),
    )
    calls = 0

    def post_drift_verifier(*_):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise IntegrationUnavailable()

    result = integration_entrypoint.execute(
        ARGV,
        environ=_environment(tmp_path),
        clock=lambda: NOW,
        expected_entrypoint_path=_expected_entrypoint(tmp_path),
        verifier=post_drift_verifier,
    )
    assert result == integration_entrypoint.CommandResult(
        69,
        b"",
        b"integration_snapshot_unavailable\n",
    )
    assert calls == 2


def test_entrypoint_module_has_no_general_cli_provider_or_installer_import():
    import codex_usage.integration_entrypoint as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {
        f"codex_usage.{node.module}" if node.level == 1 and node.module else node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported |= {
        f"codex_usage.{alias.name}"
        for node in ast.walk(tree)
        for alias in (
            node.names
            if isinstance(node, ast.ImportFrom) and node.level == 1 and not node.module
            else ()
        )
    }
    assert not imported & {
        "codex_usage.cli",
        "codex_usage.browser",
        "codex_usage.direct",
        "codex_usage.app_server",
        "codex_usage.oauth_browser",
        "codex_usage.scheduler",
        "codex_usage.bridge",
        "codex_usage.service",
        "codex_usage.integration_installer",
    }
    imported_roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots |= {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
    }
    assert not imported_roots & {"socket", "urllib", "http", "requests", "playwright"}


def test_default_verifier_maps_targeted_attestation_import_failure(monkeypatch):
    from codex_usage import integration_entrypoint

    real_import = builtins.__import__

    def import_without_attestation(name, globals=None, locals=None, fromlist=(), level=0):
        package = globals.get("__package__") if globals else None
        qualified = f"{package}.{name}" if level and package and name else name
        if qualified == "codex_usage.integration_attestation":
            raise ImportError("synthetic attestation marker")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_attestation)
    verifier = integration_entrypoint._default_verifier()
    with pytest.raises(IntegrationUnavailable) as error:
        verifier(Path("/tmp/state"), Path("/tmp/data"), Path("/tmp/entrypoint"))
    assert str(error.value) == ""
    assert "synthetic" not in repr(error.value)


def test_default_verifier_maps_attestation_unavailable_without_details(monkeypatch):
    from codex_usage import integration_entrypoint
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    fake_attestation = types.ModuleType("codex_usage.integration_attestation")

    def verify_active_manifest_at(**kwargs):
        raise IntegrationEvidenceUnavailable()

    fake_attestation.verify_active_manifest_at = verify_active_manifest_at
    monkeypatch.setitem(sys.modules, "codex_usage.integration_attestation", fake_attestation)

    verifier = integration_entrypoint._default_verifier()
    with pytest.raises(IntegrationEvidenceUnavailable) as error:
        verifier(Path("/tmp/state"), Path("/tmp/data"), Path("/tmp/entrypoint"))
    assert str(error.value) == ""
    assert "synthetic" not in repr(error.value)


def test_execute_rejects_old_executing_entrypoint_after_active_swap_before_lock(
    tmp_path,
    monkeypatch,
):
    from codex_usage import integration_entrypoint

    old_entrypoint = _expected_entrypoint(tmp_path)
    new_entrypoint = tmp_path / "new-release" / "codex_usage" / "integration_entrypoint.py"
    new_entrypoint.parent.mkdir(parents=True, mode=0o700)
    new_entrypoint.write_bytes(b"# new synthetic entrypoint\n")
    events: list[str] = []
    monkeypatch.setattr(
        integration_entrypoint,
        "_read_current_source_snapshot",
        lambda _: pytest.fail("source read"),
    )

    def swap_race_verifier(state_home: Path, data_home: Path, expected: Path) -> None:
        events.append("verify")
        assert expected is old_entrypoint
        active_manifest_entrypoint = new_entrypoint
        if expected != active_manifest_entrypoint:
            raise IntegrationUnavailable()

    result = integration_entrypoint.execute(
        ARGV,
        environ=_environment(tmp_path),
        clock=lambda: NOW,
        expected_entrypoint_path=old_entrypoint,
        verifier=swap_race_verifier,
    )
    assert result == integration_entrypoint.CommandResult(
        69,
        b"",
        b"integration_snapshot_unavailable\n",
    )
    assert events == ["verify"]


def test_main_writes_success_payload_to_binary_stdout(monkeypatch):
    from codex_usage import integration_entrypoint

    output = io.BytesIO()
    monkeypatch.setattr(
        integration_entrypoint,
        "execute",
        lambda *args, **kwargs: integration_entrypoint.CommandResult(0, _payload(), b""),
    )
    monkeypatch.setattr(sys, "stdout", type("Stdout", (), {"buffer": output})())
    assert integration_entrypoint.main(ARGV) == 0
    assert output.getvalue() == _payload()


def test_main_writes_error_payload_to_binary_stderr(monkeypatch):
    from codex_usage import integration_entrypoint

    output = io.BytesIO()
    monkeypatch.setattr(
        integration_entrypoint,
        "execute",
        lambda *args, **kwargs: integration_entrypoint.CommandResult(
            70, b"", b"integration_snapshot_secure_io_failed\n"
        ),
    )
    monkeypatch.setattr(sys, "stderr", type("Stderr", (), {"buffer": output})())

    assert integration_entrypoint.main(ARGV) == 70
    assert output.getvalue() == b"integration_snapshot_secure_io_failed\n"


def test_module_main_guard_executes(tmp_path, monkeypatch):
    import runpy

    environ = _environment(tmp_path)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["integration-entrypoint", *ARGV])

    with pytest.raises(SystemExit) as error:
        runpy.run_module("codex_usage.integration_entrypoint", run_name="__main__")

    assert isinstance(error.value.code, int)
