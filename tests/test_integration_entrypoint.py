from __future__ import annotations

import ast
import builtins
import io
import json
import sys
import types
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from codex_usage.integration_snapshot import (
    IntegrationBusy,
    IntegrationInvalidSource,
    IntegrationSecureIOError,
    IntegrationUnavailable,
)

NOW = datetime(2026, 8, 15, 10, 5, tzinfo=UTC)
ARGV = ("integration-snapshot", "--schema", "1", "--format", "json")


@pytest.mark.parametrize("code", [True, 69.0, "69"])
def test_error_result_rejects_non_integer_error_codes(code):
    from codex_usage.integration_entrypoint import _error_result

    result = _error_result(code)

    assert result.exit_code == 69


def _environment(tmp_path: Path) -> dict[str, str]:
    data_home = tmp_path / "data"
    state_home = tmp_path / "state"
    (data_home / "codex-usage" / "current").mkdir(parents=True, mode=0o700)
    (state_home / "codex-usage" / "integration").mkdir(parents=True, mode=0o700)
    return {"XDG_DATA_HOME": str(data_home), "XDG_STATE_HOME": str(state_home)}


def _clock_counter():
    calls: list[None] = []

    def now() -> datetime:
        calls.append(None)
        return NOW

    return now, calls


def _payload() -> bytes:
    return b'{"accounts":[],"generated_at":"2026-08-15T10:05:00Z","schema_version":1}'


def _expected_entrypoint(tmp_path: Path) -> Path:
    path = tmp_path / "release" / "venv" / "lib" / "codex_usage" / "integration_entrypoint.py"
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_bytes(b"# synthetic entrypoint\n")
    path.chmod(0o600)
    return path


def test_execute_rejects_every_nonexact_argv_before_verifier_or_source(tmp_path):
    from codex_usage.integration_entrypoint import execute

    calls: list[str] = []
    result = execute(
        ("integration-snapshot", "--schema", "2", "--format", "json"),
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
        "1",
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

    class HeldLock:
        def __enter__(self):
            events.append("lock")
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append("unlock")
            return False

    monkeypatch.setattr(
        integration_entrypoint,
        "private_path_lock",
        lambda *args, **kwargs: HeldLock(),
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "read_current_usage_records",
        lambda _: events.append("read") or (),
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "build_schema1_document",
        lambda *args, **kwargs: events.append("build")
        or {"schema_version": 1, "generated_at": "2026-08-15T10:05:00Z", "accounts": []},
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "serialize_schema1_document",
        lambda _: events.append("serialize") or _payload(),
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "publish_schema1_cache",
        lambda payload, *, cache_path: events.append("publish"),
    )

    clock, clock_calls = _clock_counter()

    def verifier(state_home: Path, data_home: Path, expected: Path) -> None:
        events.append("verify")
        verifier_args.append((state_home, data_home, expected))

    result = integration_entrypoint.execute(
        ARGV,
        environ=_environment(tmp_path),
        clock=clock,
        expected_entrypoint_path=expected_entrypoint,
        verifier=verifier,
    )
    assert result == integration_entrypoint.CommandResult(0, _payload(), b"")
    assert events == ["lock", "verify", "read", "build", "serialize", "verify", "publish", "unlock"]
    assert len(clock_calls) == 1
    assert len(verifier_args) == 2
    assert verifier_args[0][2] is expected_entrypoint
    assert verifier_args[1][2] is expected_entrypoint


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


def test_execute_normalizes_clock_before_dst_cost_lookback(tmp_path, monkeypatch):
    from codex_usage import integration_entrypoint
    from codex_usage.history import HistoryStore, UsageSample
    from codex_usage.models import AccountUsage

    environ = _environment(tmp_path)
    paths = integration_entrypoint._runtime_paths(environ)
    now = datetime(
        2026,
        10,
        25,
        2,
        30,
        tzinfo=ZoneInfo("Europe/Berlin"),
        fold=1,
    )
    points = (
        (datetime(2026, 10, 24, 23, 30, tzinfo=UTC), 0),
        (datetime(2026, 10, 25, 0, 30, tzinfo=UTC), 20),
        (datetime(2026, 10, 25, 1, 0, tzinfo=UTC), 40),
        (datetime(2026, 10, 25, 1, 30, tzinfo=UTC), 50),
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
                    source="test",
                )
                for captured_at, used_percent in points
            )
        )
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=now.astimezone(UTC),
    )
    monkeypatch.setattr(
        integration_entrypoint,
        "read_current_usage_records",
        lambda _: (usage,),
    )

    result = integration_entrypoint.execute(
        ARGV,
        environ=environ,
        clock=lambda: now,
        expected_entrypoint_path=_expected_entrypoint(tmp_path),
        verifier=lambda *_: None,
    )

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    short = next(
        item
        for item in document["accounts"][0]["cost_windows"]
        if item["limit_window_seconds"] == 18_000
    )
    assert short["consumed_percentage_points"] == 30.0
    assert short["sample_count"] == 3


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
        cache_path=state_home / "codex-usage" / "integration" / "account-usage-v1.json",
        release_lock_target=state_home / "codex-usage" / "integration" / "producer-install",
    )


def test_cost_window_loader_reads_history_from_data_root(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from codex_usage.history import HistoryStore, UsageSample
    from codex_usage.integration_entrypoint import _load_cost_windows, _runtime_paths

    environ = _environment(tmp_path)
    paths = _runtime_paths(environ)
    data_history_path = Path(environ["XDG_DATA_HOME"]) / "codex-usage" / "usage-history.sqlite3"
    captured_before = NOW - timedelta(minutes=30)
    with HistoryStore(data_history_path) as store:
        store.record(
            UsageSample(
                account_id="alpha",
                pool="main",
                window_seconds=18_000,
                captured_at=captured_before,
                used_percent=10,
                source="test",
            )
        )
        store.record(
            UsageSample(
                account_id="alpha",
                pool="main",
                window_seconds=18_000,
                captured_at=NOW,
                used_percent=20,
                source="test",
            )
            )

    def unexpected_samples(*args, **kwargs):
        raise AssertionError("integration must use bounded history query")

    monkeypatch.setattr(HistoryStore, "samples", unexpected_samples)
    costs = _load_cost_windows(
        paths.history_path,
        (SimpleNamespace(account_id="alpha"),),
        NOW,
    )
    assert costs["alpha"][0].consumed_percentage_points == 10
    assert costs["alpha"][0].sample_count == 2


def test_cost_window_loader_includes_monthly_and_stored_custom_windows(tmp_path):
    from types import SimpleNamespace

    from codex_usage.history import HistoryStore, UsageSample
    from codex_usage.integration_entrypoint import _load_cost_windows

    history_path = tmp_path / "usage-history.sqlite3"
    with HistoryStore(history_path) as store:
        store.record_many(
            tuple(
                UsageSample(
                    account_id="alpha",
                    pool="main",
                    window_seconds=duration,
                    captured_at=captured_at,
                    used_percent=used_percent,
                    source="test",
                )
                for duration in (2_592_000, 123_456)
                for captured_at, used_percent in (
                    (NOW - timedelta(minutes=30), 10),
                    (NOW, 20),
                )
            )
        )

    costs = _load_cost_windows(
        history_path,
        (SimpleNamespace(account_id="alpha"),),
        NOW,
    )
    by_duration = {
        item.limit_window_seconds: item for item in costs["alpha"]
    }
    assert {18_000, 604_800, 2_592_000, 123_456} <= set(by_duration)
    assert by_duration[2_592_000].consumed_percentage_points == 10
    assert by_duration[123_456].consumed_percentage_points == 10


def test_cost_window_loader_rejects_out_of_range_lookback(tmp_path):
    from types import SimpleNamespace

    from codex_usage.history import HistoryStore
    from codex_usage.integration_entrypoint import _load_cost_windows

    history_path = tmp_path / "usage-history.sqlite3"
    with HistoryStore(history_path):
        pass

    with pytest.raises(ValueError, match="now is out of range"):
        _load_cost_windows(
            history_path,
            (SimpleNamespace(account_id="alpha"),),
            datetime.min.replace(tzinfo=UTC) + timedelta(seconds=3_599),
        )


def test_execute_does_not_publish_when_post_verifier_detects_drift(tmp_path, monkeypatch):
    from codex_usage import integration_entrypoint

    monkeypatch.setattr(integration_entrypoint, "read_current_usage_records", lambda _: ())
    monkeypatch.setattr(
        integration_entrypoint,
        "build_schema1_document",
        lambda *args, **kwargs: {
            "schema_version": 1,
            "generated_at": "2026-08-15T10:05:00Z",
            "accounts": [],
        },
    )
    monkeypatch.setattr(integration_entrypoint, "serialize_schema1_document", lambda _: _payload())
    monkeypatch.setattr(
        integration_entrypoint,
        "publish_schema1_cache",
        lambda *args, **kwargs: pytest.fail("cache publish"),
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

    fake_attestation = types.ModuleType("codex_usage.integration_attestation")

    class FakeAttestationUnavailable(Exception):
        pass

    def verify_active_release(**kwargs):
        raise FakeAttestationUnavailable("synthetic attestation marker")

    fake_attestation.IntegrationAttestationUnavailable = FakeAttestationUnavailable
    fake_attestation.verify_active_release = verify_active_release
    monkeypatch.setitem(sys.modules, "codex_usage.integration_attestation", fake_attestation)

    verifier = integration_entrypoint._default_verifier()
    with pytest.raises(IntegrationUnavailable) as error:
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

    class HeldLock:
        def __enter__(self):
            events.append("lock")
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append("unlock")
            return False

    monkeypatch.setattr(
        integration_entrypoint,
        "private_path_lock",
        lambda *args, **kwargs: HeldLock(),
    )
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
    assert events == ["lock", "verify", "unlock"]


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
