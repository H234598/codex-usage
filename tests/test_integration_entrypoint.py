from __future__ import annotations

import ast
import builtins
import hashlib
import io
import json
import os
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
    return {"XDG_DATA_HOME": str(data_home), "XDG_STATE_HOME": str(state_home)}


def _clock_counter():
    calls: list[None] = []

    def now() -> datetime:
        calls.append(None)
        return NOW

    return now, calls


def _payload() -> bytes:
    return b'{"accounts":[],"generated_at":"2026-08-15T10:05:00Z","schema_version":2}'


def _expected_entrypoint(tmp_path: Path) -> Path:
    path = tmp_path / "release" / "venv" / "lib" / "codex_usage" / "integration_entrypoint.py"
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_bytes(b"# synthetic entrypoint\n")
    path.chmod(0o600)
    return path


def _verified_manifest(tmp_path: Path, entrypoint: Path | None = None):
    """Task-3-only staged 0.6.536 manifest; real cutover belongs to Task 6."""
    from codex_usage.integration_attestation import ActiveRelease, VerifiedActiveManifest
    from codex_usage.private_io import FileIdentity

    entrypoint = entrypoint or _expected_entrypoint(tmp_path)
    active_bytes = b'{"release_id":"0.6.536-aaaaaaaaaaaaaaaa","version":"0.6.536"}'
    identity = FileIdentity(1, 2, 0o700)
    return VerifiedActiveManifest(
        active_release=ActiveRelease(
            version="0.6.536",
            release_dir=entrypoint.parents[3],
            launcher_path=entrypoint.parents[3] / "bin/codex-usage-integration",
            entrypoint_path=entrypoint,
            entrypoint_sha256="b" * 64,
            wheel_sha256="c" * 64,
            record_sha256="d" * 64,
            launcher_sha256="e" * 64,
            release_tree_sha256="f" * 64,
        ),
        release_id="0.6.536-aaaaaaaaaaaaaaaa",
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
        "read_current_usage_records",
        lambda _: events.append("read") or (),
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
    lock_root = private_io._private_lock_root()
    private_io.ensure_private_directory(lock_root, label="test lock root")
    for target in (integration / "producer-install", integration / "current.json"):
        lock_path = lock_root / integration_evidence._evidence_lock_name(target)
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)

    monkeypatch.setattr(integration_entrypoint, "read_current_usage_records", lambda _: ())
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
        integration_evidence,
        "_verify_active_manifest_for_publish",
        lambda **kwargs: staged,
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
        "read_current_usage_records",
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
        "read_current_usage_records",
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
        "read_current_usage_records",
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
        "read_current_usage_records",
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
        "read_current_usage_records",
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
        "read_current_usage_records",
        lambda _: (usage,),
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

    monkeypatch.setattr(integration_entrypoint, "read_current_usage_records", lambda _: ())
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
        "read_current_usage_records",
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
