from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import codex_usage.state_maintenance as maintenance_module
from codex_usage import private_io
from codex_usage.account_lock import AccountLockError, account_lock, state_maintenance_lock
from codex_usage.cli import main
from codex_usage.config import AppConfig, PoolAuthorityOwner, load_config, save_config
from codex_usage.integration_snapshot import read_current_usage_records
from codex_usage.models import Account, AccountUsage, LimitWindow
from codex_usage.pool_authority_owner import save_pool_authority_owner
from codex_usage.private_io import ensure_private_directory
from codex_usage.state import load_current_usage, save_current_usage, save_usage_snapshot
from codex_usage.state_maintenance import quarantine_unconfigured_usage_state

_D296_IDS = (
    "BW_Nufker",
    "BW_Privat",
    "BW_Work",
    "Birthe_Privat",
    "GPT1",
    "RH_Privat",
)
_FOREIGN_IDS = ("account", "blocked", "broken", "ok")


def _account(account_id: str, root: Path) -> Account:
    return Account(
        id=account_id,
        label=account_id,
        profile_dir=str(root / "profiles" / account_id),
        backend="direct",
    )


def _authority(account_id: str) -> PoolAuthorityOwner:
    return PoolAuthorityOwner(
        account_id=account_id,
        pool_id="openai",
        provider="openai",
        hive_available=True,
        allowed_model_families=("luna", "sol", "terra"),
        reasoning_minimum="low",
        reasoning_maximum="max",
        allowed_lifecycles=("ephemeral", "persistent", "session"),
        persistent_leadership_eligible=True,
        long_running_leadership_eligible=True,
    )


def _usage(account_id: str) -> AccountUsage:
    return AccountUsage(
        account_id=account_id,
        label=account_id,
        captured_at=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", remaining=50),
    )


def _write_private(path: Path, value: str = "{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def _prepared_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    data_home = tmp_path / "data"
    state_home = tmp_path / "state"
    config_path = tmp_path / "config" / "config.toml"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    save_config(
        AppConfig(accounts=tuple(_account(item, tmp_path) for item in _D296_IDS)),
        config_path,
    )
    save_pool_authority_owner(
        tuple(_authority(item) for item in _D296_IDS),
        expected_generation=0,
        config_path=config_path,
        state_home=state_home,
    )
    for account_id in (*_D296_IDS, *_FOREIGN_IDS):
        save_current_usage(_usage(account_id))
        save_usage_snapshot(_usage(account_id))
        root = data_home / "codex-usage"
        _write_private(root / "debug" / f"{account_id}-last-ingest.json")
        generation = '{"account":"' + account_id + '","generation":0}'
        _write_private(root / "generations" / f"{account_id}.json", generation)
        _write_private(root / "locks" / f"{account_id}.lock", "")
    return config_path, data_home, state_home


def _names(directory: Path, suffix: str) -> tuple[str, ...]:
    return tuple(sorted(path.name.removesuffix(suffix) for path in directory.iterdir()))


def _artifact_bytes(root: Path) -> dict[Path, bytes]:
    return {
        path: path.read_bytes()
        for directory in (
            root / "current",
            root / "snapshots",
            root / "debug",
            root / "generations",
        )
        for path in directory.iterdir()
    } | {
        path: path.read_bytes()
        for path in (root / "locks").iterdir()
        if path.name != "__state_maintenance__.lock"
    }


def _bound_file_bytes(path: Path) -> tuple[bytes, tuple[int, int, int, int, int, int, int, int]]:
    item = path.lstat()
    return path.read_bytes(), (
        item.st_dev,
        item.st_ino,
        stat.S_IMODE(item.st_mode),
        item.st_uid,
        item.st_gid,
        item.st_nlink,
        item.st_size,
        item.st_ctime_ns,
    )


def _namespace_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    """Capture every controlled State entry's name, identity, and bytes."""
    entries: list[tuple[object, ...]] = []
    paths = (root, *sorted(root.rglob("*"), key=lambda path: str(path)))
    for path in paths:
        item = path.lstat()
        relative = "." if path == root else str(path.relative_to(root))
        payload = path.read_bytes() if stat.S_ISREG(item.st_mode) else None
        entries.append(
            (
                relative,
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_uid,
                item.st_gid,
                item.st_nlink,
                item.st_size,
                item.st_ctime_ns,
                item.st_mtime_ns,
                payload,
            )
        )
    return tuple(entries)


def _maintenance_input_snapshot(
    *,
    config_path: Path,
    data_home: Path,
    state_home: Path,
) -> tuple[object, ...]:
    source = state_home / "codex-usage" / "integration" / "pool-authority-source-v2.json"
    return (
        _namespace_snapshot(data_home / "codex-usage"),
        _bound_file_bytes(config_path),
        _bound_file_bytes(source),
    )


def _run_crashing_apply(
    *,
    config_path: Path,
    data_home: Path,
    state_home: Path,
    stage: str,
    pytestconfig: pytest.Config,
) -> None:
    scripts = {
        "before_manifest": """
original = maintenance._write_manifest
def crash(*args, **kwargs):
    os._exit(71)
maintenance._write_manifest = crash
""",
        "after_move_before_manifest": """
original = maintenance._rename_no_replace
def crash(source, destination, *, label):
    original(source, destination, label=label)
    if label == "state maintenance artifact quarantine":
        os._exit(72)
maintenance._rename_no_replace = crash
""",
        "before_commit_rename": """
original = maintenance._rename_no_replace
def crash(source, destination, *, label):
    if label == "state maintenance transaction publication":
        os._exit(73)
    original(source, destination, label=label)
maintenance._rename_no_replace = crash
""",
        "after_commit_rename": """
original = maintenance._rename_no_replace
def crash(source, destination, *, label):
    original(source, destination, label=label)
    if label == "state maintenance transaction publication":
        os._exit(74)
maintenance._rename_no_replace = crash
""",
    }
    script = "\n".join(
        (
            "import os",
            "import sys",
            "from pathlib import Path",
            "import codex_usage.state_maintenance as maintenance",
            scripts[stage],
            "maintenance.quarantine_unconfigured_usage_state(",
            "    config_path=Path(sys.argv[1]), apply=True,",
            ")",
        )
    )
    environment = os.environ | {
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
    }
    lock_root = private_io._private_lock_root()
    isolation_prefix = [
        "/usr/bin/bwrap",
        "--bind",
        "/",
        "/",
        "--bind",
        str(lock_root),
        str(pytestconfig._private_lock_production_root),
        "--",
    ]
    result = subprocess.run(
        [*isolation_prefix, sys.executable, "-c", script, str(config_path)],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == {
        "before_manifest": 71,
        "after_move_before_manifest": 72,
        "before_commit_rename": 73,
        "after_commit_rename": 74,
    }[stage], result.stderr


def _run_crashing_recovery_cleanup(
    *,
    config_path: Path,
    data_home: Path,
    state_home: Path,
    pytestconfig: pytest.Config,
) -> None:
    script = """
import os
import sys
from pathlib import Path
import codex_usage.state_maintenance as maintenance

original_unlink = Path.unlink
def crash_after_manifest_unlink(path, *args, **kwargs):
    original_unlink(path, *args, **kwargs)
    if path.name == maintenance._MANIFEST_NAME and path.parent.name.startswith(".pending-"):
        os._exit(75)
Path.unlink = crash_after_manifest_unlink
maintenance.quarantine_unconfigured_usage_state(config_path=Path(sys.argv[1]), apply=True)
"""
    environment = os.environ | {
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
    }
    lock_root = private_io._private_lock_root()
    isolation_prefix = [
        "/usr/bin/bwrap",
        "--bind",
        "/",
        "/",
        "--bind",
        str(lock_root),
        str(pytestconfig._private_lock_production_root),
        "--",
    ]
    result = subprocess.run(
        [*isolation_prefix, sys.executable, "-c", script, str(config_path)],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 75, result.stderr


def _start_cross_process_lock_holder(
    *,
    data_home: Path,
    state_home: Path,
    lock_kind: str,
    pytestconfig: pytest.Config,
) -> subprocess.Popen[str]:
    scripts = {
        "maintenance": """
from codex_usage.account_lock import state_maintenance_lock
with state_maintenance_lock(timeout_seconds=5):
    print("locked", flush=True)
    __import__("sys").stdin.read()
""",
        "account": """
from codex_usage.account_lock import account_lock
with account_lock("account", timeout_seconds=5):
    print("locked", flush=True)
    __import__("sys").stdin.read()
""",
    }
    environment = os.environ | {
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
    }
    lock_root = private_io._private_lock_root()
    isolation_prefix = [
        "/usr/bin/bwrap",
        "--bind",
        "/",
        "/",
        "--bind",
        str(lock_root),
        str(pytestconfig._private_lock_production_root),
        "--",
    ]
    process = subprocess.Popen(
        [*isolation_prefix, sys.executable, "-c", scripts[lock_kind]],
        cwd=Path(__file__).parents[1],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "locked"
    return process


def _stop_cross_process_lock_holder(process: subprocess.Popen[str]) -> None:
    try:
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def _start_cross_process_source_lock_holder(
    *,
    source_root: Path,
    pytestconfig: pytest.Config,
) -> subprocess.Popen[str]:
    script = """
import sys
from pathlib import Path
from codex_usage.source_lock import source_lock

with source_lock(Path(sys.argv[1]), timeout_seconds=5):
    print("locked", flush=True)
    sys.stdin.read()
"""
    environment = os.environ | {
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
    }
    lock_root = private_io._private_lock_root()
    isolation_prefix = [
        "/usr/bin/bwrap",
        "--bind",
        "/",
        "/",
        "--bind",
        str(lock_root),
        str(pytestconfig._private_lock_production_root),
        "--",
    ]
    process = subprocess.Popen(
        [*isolation_prefix, sys.executable, "-c", script, str(source_root)],
        cwd=Path(__file__).parents[1],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "locked"
    return process


def _start_config_update_before_source_seam(
    *,
    config_path: Path,
    data_home: Path,
    state_home: Path,
    pytestconfig: pytest.Config,
) -> subprocess.Popen[str]:
    script = """
import os
import sys
from pathlib import Path
from codex_usage.config import add_or_update_account
from codex_usage.source_lock import source_lock

def wait_before_source(_config):
    print("account-and-config-held", flush=True)
    if sys.stdin.readline() != "continue\\n":
        raise RuntimeError("config update was not released")
    with source_lock(Path(os.environ["XDG_DATA_HOME"]) / "codex-usage", timeout_seconds=5):
        pass

try:
    add_or_update_account(
        "BW_Nufker",
        label="Updated",
        path=Path(sys.argv[1]),
        before_state_cleanup=wait_before_source,
    )
except BaseException as exc:
    print(f"error:{type(exc).__name__}:{exc}", flush=True)
    raise
print("ok", flush=True)
"""
    environment = os.environ | {
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
    }
    lock_root = private_io._private_lock_root()
    isolation_prefix = [
        "/usr/bin/bwrap",
        "--bind",
        "/",
        "/",
        "--bind",
        str(lock_root),
        str(pytestconfig._private_lock_production_root),
        "--",
    ]
    process = subprocess.Popen(
        [*isolation_prefix, sys.executable, "-c", script, str(config_path)],
        cwd=Path(__file__).parents[1],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "account-and-config-held"
    return process


def _start_maintenance_apply(
    *,
    config_path: Path,
    data_home: Path,
    state_home: Path,
    pytestconfig: pytest.Config,
) -> subprocess.Popen[str]:
    script = """
import sys
from pathlib import Path
from codex_usage.state_maintenance import quarantine_unconfigured_usage_state

try:
    report = quarantine_unconfigured_usage_state(
        config_path=Path(sys.argv[1]),
        data_home=Path(sys.argv[2]),
        state_home=Path(sys.argv[3]),
        apply=True,
    )
except BaseException as exc:
    print(f"error:{type(exc).__name__}:{exc}", flush=True)
else:
    print(f"ok:{','.join(report.quarantined_account_ids)}", flush=True)
"""
    environment = os.environ | {
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
    }
    lock_root = private_io._private_lock_root()
    isolation_prefix = [
        "/usr/bin/bwrap",
        "--bind",
        "/",
        "/",
        "--bind",
        str(lock_root),
        str(pytestconfig._private_lock_production_root),
        "--",
    ]
    return subprocess.Popen(
        [
            *isolation_prefix,
            sys.executable,
            "-c",
            script,
            str(config_path),
            str(data_home),
            str(state_home),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop_process(process: subprocess.Popen[str]) -> None:
    try:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("payload", ("", "historical current lock sidecar\n"))
def test_dry_run_ignores_safe_historical_current_lock_sidecars_for_every_d296_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    """Would fail if a valid Producer-style current lock blocked maintenance."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    current = data_home / "codex-usage" / "current"
    sidecars = tuple(current / f"{account_id}.json.lock" for account_id in _D296_IDS)
    for sidecar in sidecars:
        _write_private(sidecar, payload)

    report = quarantine_unconfigured_usage_state(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        apply=False,
    )

    assert report.applied is False
    assert report.quarantined_account_ids == tuple(sorted(_FOREIGN_IDS))
    assert tuple(sidecar.read_text(encoding="utf-8") for sidecar in sidecars) == (
        payload,
    ) * len(sidecars)


def test_dry_run_rejects_a_symlinked_historical_current_lock_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if a Producer-style suffix bypassed no-follow validation."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    current = data_home / "codex-usage" / "current"
    sidecar = current / "BW_Nufker.json.lock"
    outside = tmp_path / "outside-lock"
    _write_private(outside, "outside")
    sidecar.symlink_to(outside)

    with pytest.raises(ValueError, match="current lock sidecar"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=False,
        )

    assert sidecar.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside"


def test_dry_run_rejects_a_hardlinked_historical_current_lock_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if a hardlinked historical sidecar became transient."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    current = data_home / "codex-usage" / "current"
    sidecar = current / "BW_Nufker.json.lock"
    outside = tmp_path / "outside-lock"
    _write_private(outside, "outside")
    os.link(outside, sidecar)

    with pytest.raises(ValueError, match="current lock sidecar"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=False,
        )

    assert sidecar.stat().st_nlink == 2
    assert outside.read_text(encoding="utf-8") == "outside"


def test_dry_run_rejects_a_wrong_mode_historical_current_lock_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if an owner file with a non-private mode were ignored."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    sidecar = data_home / "codex-usage" / "current" / "BW_Nufker.json.lock"
    _write_private(sidecar, "historical")
    sidecar.chmod(0o640)

    with pytest.raises(ValueError, match="current lock sidecar"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=False,
        )

    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o640


def test_dry_run_rejects_a_foreign_owner_historical_current_lock_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if a foreign UID in the sidecar evidence were ignored."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    sidecar = data_home / "codex-usage" / "current" / "BW_Nufker.json.lock"
    _write_private(sidecar, "historical")
    original_read_private_text = maintenance_module.read_private_text

    def read_with_foreign_owner(path: Path, **kwargs: object) -> tuple[str, os.stat_result]:
        text, item = original_read_private_text(path, **kwargs)
        if path == sidecar:
            fields = list(item)
            fields[stat.ST_UID] = item.st_uid + 1
            return text, os.stat_result(fields)
        return text, item

    monkeypatch.setattr(maintenance_module, "read_private_text", read_with_foreign_owner)

    with pytest.raises(ValueError, match="current lock sidecar"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=False,
        )


def test_dry_run_rejects_a_rebound_historical_current_lock_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if a verified sidecar could be swapped before scan completion."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    current = data_home / "codex-usage" / "current"
    sidecar = current / "BW_Nufker.json.lock"
    _write_private(sidecar, "first")
    original_identity = maintenance_module._private_identity
    rebound = False

    def capture_then_rebind(path: Path, *, label: str) -> maintenance_module._Identity:
        nonlocal rebound
        identity = original_identity(path, label=label)
        if path == sidecar and not rebound:
            rebound = True
            replacement = current / ".replacement"
            _write_private(replacement, "second")
            replacement.replace(sidecar)
        return identity

    monkeypatch.setattr(maintenance_module, "_private_identity", capture_then_rebind)

    with pytest.raises(ValueError, match="current directory changed while scanning"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=False,
        )

    assert rebound is True
    assert sidecar.read_text(encoding="utf-8") == "second"


@pytest.mark.parametrize("drift", ("hardlink", "in_place", "mode"))
def test_dry_run_rejects_post_scan_drift_of_historical_current_lock_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    """Ignored sidecars stay identity-bound through the final verification."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    sidecar = data_home / "codex-usage" / "current" / "BW_Nufker.json.lock"
    _write_private(sidecar, "first")
    real_verify_authority = maintenance_module._verify_authority_snapshot
    drifted = False

    def drift_after_scan(*args: object) -> None:
        nonlocal drifted
        real_verify_authority(*args)
        if drifted:
            return
        drifted = True
        if drift == "hardlink":
            os.link(sidecar, tmp_path / "rebound-lock")
        elif drift == "in_place":
            sidecar.write_text("other", encoding="utf-8")
        elif drift == "mode":
            sidecar.chmod(0o640)
        else:  # pragma: no cover - closed parametrization
            raise AssertionError(drift)

    monkeypatch.setattr(
        maintenance_module, "_verify_authority_snapshot", drift_after_scan
    )

    with pytest.raises(ValueError):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=False,
        )

    assert drifted is True


def test_apply_ignores_safe_historical_current_lock_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The D299 exception neither blocks nor quarantines a safe Producer sidecar."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    current = data_home / "codex-usage" / "current"
    sidecars = tuple(current / f"{account_id}.json.lock" for account_id in _D296_IDS)
    for sidecar in sidecars:
        _write_private(sidecar, "historical")

    report = quarantine_unconfigured_usage_state(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        apply=True,
    )

    assert report.applied is True
    assert report.quarantined_account_ids == tuple(sorted(_FOREIGN_IDS))
    assert tuple(sidecar.read_text(encoding="utf-8") for sidecar in sidecars) == (
        "historical",
    ) * len(sidecars)


@pytest.mark.parametrize("drift", ("hardlink", "in_place", "mode"))
@pytest.mark.parametrize("phase", ("after_pending_manifest", "after_authority_snapshot"))
def test_apply_rejects_historical_current_lock_sidecar_drift_before_any_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    phase: str,
) -> None:
    """Safe sidecars remain bound from the pending journal through every move."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    sidecar = root / "current" / "BW_Nufker.json.lock"
    _write_private(sidecar, "first")
    drifted = False

    def introduce_drift() -> None:
        nonlocal drifted
        assert drifted is False
        drifted = True
        if drift == "hardlink":
            os.link(sidecar, tmp_path / "rebound-lock")
        elif drift == "in_place":
            sidecar.write_text("other", encoding="utf-8")
        elif drift == "mode":
            sidecar.chmod(0o640)
        else:  # pragma: no cover - closed parametrization
            raise AssertionError(drift)

    if phase == "after_pending_manifest":
        real_write_manifest = maintenance_module._write_manifest
        writes = 0

        def write_then_drift(*args: object) -> None:
            nonlocal writes
            real_write_manifest(*args)
            writes += 1
            if writes == 1:
                introduce_drift()

        monkeypatch.setattr(maintenance_module, "_write_manifest", write_then_drift)
    elif phase == "after_authority_snapshot":
        real_verify = maintenance_module._verify_authority_snapshot_for_apply

        def verify_then_drift(*args: object) -> None:
            real_verify(*args)
            introduce_drift()

        monkeypatch.setattr(
            maintenance_module,
            "_verify_authority_snapshot_for_apply",
            verify_then_drift,
        )
    else:  # pragma: no cover - closed parametrization
        raise AssertionError(phase)

    with pytest.raises(ValueError):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=True,
        )

    assert drifted is True
    assert (root / "current" / "account.json").is_file()
    assert (root / "snapshots" / "account.json").is_file()
    quarantine = root / "maintenance-quarantine-v1"
    assert not quarantine.exists() or not tuple(quarantine.glob("transaction-*"))


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        (b"\xff", "current lock sidecar"),
        (b"x" * (maintenance_module._MAX_STATE_ARTIFACT_BYTES + 1), "current lock sidecar"),
    ),
    ids=("invalid-utf8", "max-bytes"),
)
def test_dry_run_rejects_nontext_or_unbounded_historical_current_lock_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes, expected: str
) -> None:
    """The transient exception keeps the Producer's bounded text-file boundary."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    sidecar = data_home / "codex-usage" / "current" / "BW_Nufker.json.lock"
    sidecar.write_bytes(payload)
    sidecar.chmod(0o600)

    with pytest.raises(ValueError, match=expected):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=False,
        )


@pytest.mark.parametrize(
    "name",
    ("unknown", "BW_Nufker.json.lock.bak", ".json.lock"),
)
def test_dry_run_keeps_arbitrary_current_names_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Would fail if the suffix rule admitted names beyond exact sidecars."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    entry = data_home / "codex-usage" / "current" / name
    _write_private(entry, "unknown")

    with pytest.raises(ValueError, match="current contains a nontransient unknown entry"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=False,
        )

    assert entry.read_text(encoding="utf-8") == "unknown"


@pytest.mark.parametrize("kind", ("snapshots", "debug", "generations"))
def test_dry_run_does_not_extend_current_lock_sidecar_semantics_to_other_namespaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Would fail if the current-only exception relaxed another State namespace."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    entry = data_home / "codex-usage" / kind / "BW_Nufker.json.lock"
    _write_private(entry, "historical")

    with pytest.raises(ValueError, match=f"{kind} contains a nontransient unknown entry"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=False,
        )


def test_dry_run_does_not_ignore_a_current_style_sidecar_in_the_lock_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if current-only sidecar handling leaked into locks."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    entry = data_home / "codex-usage" / "locks" / "BW_Nufker.json.lock"
    _write_private(entry, "historical")

    report = quarantine_unconfigured_usage_state(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        apply=False,
    )

    assert "BW_Nufker.json" in report.quarantined_account_ids
    assert entry.read_text(encoding="utf-8") == "historical"


def test_dry_run_keeps_current_entry_bound_before_ignoring_safe_lock_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if ignored sidecars could evade the current namespace bound."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    current = data_home / "codex-usage" / "current"
    for index in range(maintenance_module._MAX_ARTIFACTS - len(tuple(current.iterdir())) + 1):
        _write_private(current / f"legacy-{index}.json.lock", "")

    with pytest.raises(ValueError, match="current directory has too many entries"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=False,
        )


def test_quarantine_unconfigured_usage_state_dry_run_is_canonical_and_does_not_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    before = _artifact_bytes(root)
    source = state_home / "codex-usage" / "integration" / "pool-authority-source-v2.json"
    config_before = _bound_file_bytes(config_path)
    source_before = _bound_file_bytes(source)
    input_before = _maintenance_input_snapshot(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
    )

    report = quarantine_unconfigured_usage_state(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        apply=False,
    )

    assert report.applied is False
    assert report.configured_account_ids == tuple(sorted(_D296_IDS))
    assert report.quarantined_account_ids == tuple(sorted(_FOREIGN_IDS))
    assert report.audit_json == report.audit_json.encode("utf-8").decode("utf-8")
    assert report.quarantine_path is None
    assert {path: path.read_bytes() for path in before} == before
    assert _bound_file_bytes(config_path) == config_before
    assert _bound_file_bytes(source) == source_before
    assert _maintenance_input_snapshot(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
    ) == input_before
    assert not (root / "maintenance-quarantine-v1").exists()
    assert stat.S_IMODE(source.stat().st_mode) == 0o600


def test_dry_run_without_a_source_lock_fails_closed_without_mutating_any_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-create dry-run must not reintroduce the absent Source lock."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    (root / ".source-lock-v2").unlink()
    before = _maintenance_input_snapshot(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
    )

    with pytest.raises(ValueError, match="source lock is unavailable"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=False,
        )

    assert _maintenance_input_snapshot(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
    ) == before


def test_dry_run_rejects_a_nonprivate_existing_source_lock_without_repairing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-create dry-run must not chmod an otherwise valid visible lock inode."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    lock_file = root / ".source-lock-v2"
    lock_file.chmod(0o644)
    before = _maintenance_input_snapshot(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
    )

    with pytest.raises(ValueError, match="source lock must be a private regular file"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=False,
        )

    assert _maintenance_input_snapshot(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
    ) == before


def test_quarantine_unconfigured_usage_state_moves_complete_foreign_bundles_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    source = state_home / "codex-usage" / "integration" / "pool-authority-source-v2.json"
    config_before = _bound_file_bytes(config_path)
    source_before = _bound_file_bytes(source)

    report = quarantine_unconfigured_usage_state(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        apply=True,
    )

    assert report.applied is True
    assert _bound_file_bytes(config_path) == config_before
    assert _bound_file_bytes(source) == source_before
    assert report.quarantined_account_ids == tuple(sorted(_FOREIGN_IDS))
    assert report.artifact_count == len(_FOREIGN_IDS) * 5
    assert report.quarantine_path is not None
    assert _names(root / "current", ".json") == tuple(sorted(_D296_IDS))
    assert _names(root / "snapshots", ".json") == tuple(sorted(_D296_IDS))
    assert _names(root / "debug", "-last-ingest.json") == tuple(sorted(_D296_IDS))
    assert _names(root / "generations", ".json") == tuple(sorted(_D296_IDS))
    assert {
        path.name.removesuffix(".lock")
        for path in (root / "locks").iterdir()
        if path.name != "__all_accounts__.lock"
    } == set(_D296_IDS)
    assert tuple(item.account_id for item in read_current_usage_records(root / "current")) == tuple(
        sorted(_D296_IDS)
    )
    for kind, suffix in (
        ("current", ".json"),
        ("snapshots", ".json"),
        ("debug", "-last-ingest.json"),
        ("generations", ".json"),
        ("locks", ".lock"),
    ):
        assert _names(report.quarantine_path / kind, suffix) == tuple(sorted(_FOREIGN_IDS))
    audit_path = report.quarantine_path / "audit.json"
    assert audit_path.read_text(encoding="utf-8") == report.audit_json
    assert stat.S_IMODE(audit_path.stat().st_mode) == 0o600


def test_quarantine_unconfigured_usage_state_rejects_invalid_owner_source_without_state_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    source = state_home / "codex-usage" / "integration" / "pool-authority-source-v2.json"
    before = _artifact_bytes(root)
    maintenance_lock = root / ".state-maintenance.lock"
    maintenance_lock_before = maintenance_lock.read_bytes()
    _write_private(source, "{not-json}\n")

    with pytest.raises(ValueError, match="owner source is invalid"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=True,
        )

    assert {path: path.read_bytes() for path in before} == before
    assert not (root / "maintenance-quarantine-v1").exists()
    assert maintenance_lock.read_bytes() == maintenance_lock_before


def test_quarantine_unconfigured_usage_state_rolls_back_every_artifact_on_staging_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    before = _artifact_bytes(root)
    original_rename = maintenance_module._rename_no_replace

    def fail_one_snapshot(source: Path, destination: Path, *, label: str) -> None:
        if source == root / "snapshots" / "account.json":
            raise OSError("synthetic quarantine staging failure")
        original_rename(source, destination, label=label)

    monkeypatch.setattr(maintenance_module, "_rename_no_replace", fail_one_snapshot)

    with pytest.raises(OSError, match="synthetic quarantine staging failure"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=True,
        )

    assert {path: path.read_bytes() for path in before} == before
    quarantine_root = root / "maintenance-quarantine-v1"
    assert not tuple(quarantine_root.iterdir())


def test_quarantine_unconfigured_usage_state_rejects_owner_toctou_before_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    source = state_home / "codex-usage" / "integration" / "pool-authority-source-v2.json"
    before = _artifact_bytes(root)

    def replace_source() -> None:
        _write_private(source, "{not-json}\n")

    monkeypatch.setattr("codex_usage.state_maintenance._before_quarantine_mutation", replace_source)

    with pytest.raises(ValueError, match="owner source is invalid"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=True,
        )

    assert {path: path.read_bytes() for path in before} == before


def test_cli_state_maintenance_exposes_only_explicit_dry_run_and_apply_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, data_home, _state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"

    exit_code = main(
        [
            "--config",
            str(config_path),
            "state-maintenance",
            "quarantine-unconfigured",
            "--dry-run",
            "--format",
            "json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["applied"] is False
    assert payload["quarantine_path"] is None
    assert payload["quarantined_account_ids"] == sorted(_FOREIGN_IDS)
    assert _names(root / "current", ".json") == tuple(sorted((*_D296_IDS, *_FOREIGN_IDS)))

    exit_code = main(
        [
            "--config",
            str(config_path),
            "state-maintenance",
            "quarantine-unconfigured",
            "--apply",
            "--format",
            "json",
        ]
    )

    applied_payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert applied_payload["applied"] is True
    assert applied_payload["quarantine_path"] is not None
    assert _names(root / "current", ".json") == tuple(sorted(_D296_IDS))


def test_quarantine_unconfigured_usage_state_rejects_unsafe_lock_without_any_bundle_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    unsafe_lock = root / "locks" / "account.lock"
    unsafe_lock.unlink()
    outside = tmp_path / "outside.lock"
    outside.write_text("outside", encoding="utf-8")
    unsafe_lock.symlink_to(outside)
    before_current = {
        path: path.read_bytes() for path in (root / "current").iterdir()
    }

    with pytest.raises(ValueError, match="symlink"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=True,
        )

    assert {path: path.read_bytes() for path in before_current} == before_current
    assert unsafe_lock.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside"
    assert not (root / "maintenance-quarantine-v1").exists()


def test_apply_recovers_identity_bound_pending_quarantine_before_new_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    authority = maintenance_module._load_authority_snapshot(config_path, state_home)
    artifacts = maintenance_module._scan_artifacts(
        root, authority.configured_account_ids
    ).artifacts
    quarantine_root = root / "maintenance-quarantine-v1"
    ensure_private_directory(quarantine_root, label="test quarantine root")
    pending = quarantine_root / ".pending-11111111111111111111111111111111"
    pending.mkdir(mode=0o700)
    moved = artifacts[0]
    # The durable progress record still names no moved artifact when a process
    # dies immediately after its first rename.
    maintenance_module._write_manifest(pending, authority, artifacts, [])
    destination = pending / moved.relative_path
    ensure_private_directory(destination.parent, label="test quarantine artifact directory")
    moved.source.rename(destination)

    report = quarantine_unconfigured_usage_state(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        apply=True,
    )

    assert report.applied is True
    assert report.quarantine_path is not None
    assert not (quarantine_root / ".pending-11111111111111111111111111111111").exists()
    assert _names(root / "current", ".json") == tuple(sorted(_D296_IDS))
    assert _names(report.quarantine_path / "current", ".json") == tuple(sorted(_FOREIGN_IDS))


def test_apply_recovers_a_fully_staged_pending_quarantine_before_reporting_no_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    authority = maintenance_module._load_authority_snapshot(config_path, state_home)
    artifacts = maintenance_module._scan_artifacts(
        root, authority.configured_account_ids
    ).artifacts
    quarantine_root = root / "maintenance-quarantine-v1"
    ensure_private_directory(quarantine_root, label="test quarantine root")
    pending = quarantine_root / ".pending-22222222222222222222222222222222"
    pending.mkdir(mode=0o700)
    maintenance_module._write_manifest(pending, authority, artifacts, list(artifacts))
    for artifact in artifacts:
        destination = pending / artifact.relative_path
        ensure_private_directory(destination.parent, label="test quarantine artifact directory")
        artifact.source.rename(destination)
    pending_audit = maintenance_module._report(
        authority,
        artifacts,
        applied=True,
        quarantine_path=None,
    )
    _write_private(pending / "audit.json", pending_audit.audit_json)

    report = quarantine_unconfigured_usage_state(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        apply=True,
    )

    assert report.applied is True
    assert report.quarantined_account_ids == tuple(sorted(_FOREIGN_IDS))
    assert report.quarantine_path is not None
    assert not pending.exists()
    assert _names(root / "current", ".json") == tuple(sorted(_D296_IDS))
    assert _names(report.quarantine_path / "current", ".json") == tuple(sorted(_FOREIGN_IDS))


def test_apply_recovers_an_empty_pending_directory_left_before_manifest_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process death after mkdir must not strand an artifact-free journal."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    quarantine_root = root / "maintenance-quarantine-v1"
    ensure_private_directory(quarantine_root, label="test quarantine root")
    pending = quarantine_root / ".pending-0123456789abcdef0123456789abcdef"
    pending.mkdir(mode=0o700)

    report = quarantine_unconfigured_usage_state(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        apply=True,
    )

    assert report.applied is True
    assert report.quarantined_account_ids == tuple(sorted(_FOREIGN_IDS))
    assert report.quarantine_path is not None
    assert not pending.exists()
    assert _names(root / "current", ".json") == tuple(sorted(_D296_IDS))


def test_apply_rejects_nonempty_pending_directory_without_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    quarantine_root = root / "maintenance-quarantine-v1"
    ensure_private_directory(quarantine_root, label="test quarantine root")
    pending = quarantine_root / ".pending-fedcba9876543210fedcba9876543210"
    pending.mkdir(mode=0o700)
    _write_private(pending / "unexpected", "do not delete")

    with pytest.raises(ValueError):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=True,
        )

    assert (pending / "unexpected").read_text(encoding="utf-8") == "do not delete"
    assert _names(root / "current", ".json") == tuple(sorted((*_D296_IDS, *_FOREIGN_IDS)))


def test_apply_rejects_an_empty_foreign_pending_directory_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    quarantine_root = root / "maintenance-quarantine-v1"
    ensure_private_directory(quarantine_root, label="test quarantine root")
    pending = quarantine_root / ".pending-foreign"
    pending.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="pending transaction name is invalid"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=True,
        )

    assert pending.is_dir()
    assert _names(root / "current", ".json") == tuple(sorted((*_D296_IDS, *_FOREIGN_IDS)))


def test_apply_does_not_move_state_when_new_journal_parent_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    quarantine_root = root / "maintenance-quarantine-v1"
    before = _artifact_bytes(root)
    original_fsync = maintenance_module._fsync_directory_fd
    failed = False

    def fail_first_quarantine_parent_fsync(fd: int) -> None:
        nonlocal failed
        if not failed and Path(os.readlink(f"/proc/self/fd/{fd}")) == quarantine_root:
            failed = True
            raise OSError("synthetic journal parent fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(
        maintenance_module,
        "_fsync_directory_fd",
        fail_first_quarantine_parent_fsync,
    )

    with pytest.raises(OSError, match="synthetic journal parent fsync failure"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=True,
        )

    assert failed is True
    assert _artifact_bytes(root) == before
    assert not tuple(quarantine_root.iterdir())


def test_first_apply_fsyncs_state_root_before_creating_any_pending_journal_or_moving_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A power-loss barrier must cover the first quarantine-root creation."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    quarantine_root = root / "maintenance-quarantine-v1"
    before = _artifact_bytes(root)
    assert not quarantine_root.exists()
    original_fsync = maintenance_module._fsync_directory_fd
    failed = False

    def fail_first_state_root_fsync(fd: int) -> None:
        nonlocal failed
        if not failed and Path(os.readlink(f"/proc/self/fd/{fd}")) == root:
            failed = True
            raise OSError("synthetic state root fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(
        maintenance_module,
        "_fsync_directory_fd",
        fail_first_state_root_fsync,
    )

    with pytest.raises(OSError, match="synthetic state root fsync failure"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=True,
        )

    assert failed is True
    assert _artifact_bytes(root) == before
    assert quarantine_root.is_dir()
    assert stat.S_IMODE(quarantine_root.stat().st_mode) == 0o700
    assert not tuple(quarantine_root.iterdir())


def test_retry_fsyncs_state_root_again_before_pending_or_state_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed first durability barrier must not authorize an unfynced retry."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    quarantine_root = root / "maintenance-quarantine-v1"
    before = _artifact_bytes(root)
    original_fsync = maintenance_module._fsync_directory_fd
    state_root_fsync_attempts = 0

    def fail_first_state_root_fsync(fd: int) -> None:
        nonlocal state_root_fsync_attempts
        if Path(os.readlink(f"/proc/self/fd/{fd}")) == root:
            state_root_fsync_attempts += 1
            if state_root_fsync_attempts == 1:
                raise OSError("synthetic first state root fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(
        maintenance_module,
        "_fsync_directory_fd",
        fail_first_state_root_fsync,
    )

    with pytest.raises(OSError, match="synthetic first state root fsync failure"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=True,
        )

    assert _artifact_bytes(root) == before
    assert quarantine_root.is_dir()
    assert not tuple(quarantine_root.iterdir())

    report = quarantine_unconfigured_usage_state(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        apply=True,
    )

    assert state_root_fsync_attempts == 2
    assert report.applied is True
    assert report.quarantined_account_ids == tuple(sorted(_FOREIGN_IDS))
    assert _names(root / "current", ".json") == tuple(sorted(_D296_IDS))


def test_empty_pending_recovery_fsyncs_its_parent_after_journal_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    quarantine_root = root / "maintenance-quarantine-v1"
    ensure_private_directory(quarantine_root, label="test quarantine root")
    pending = quarantine_root / ".pending-0123456789abcdef0123456789abcdef"
    pending.mkdir(mode=0o700)
    original_fsync = maintenance_module._fsync_directory_fd
    parent_fsyncs = 0

    def count_quarantine_parent_fsync(fd: int) -> None:
        nonlocal parent_fsyncs
        if Path(os.readlink(f"/proc/self/fd/{fd}")) == quarantine_root:
            parent_fsyncs += 1
        original_fsync(fd)

    monkeypatch.setattr(maintenance_module, "_fsync_directory_fd", count_quarantine_parent_fsync)
    report = quarantine_unconfigured_usage_state(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        apply=True,
    )

    assert report.applied is True
    assert not pending.exists()
    # recovery removal, fresh pending creation, and commit publication each
    # require a durable mutation of the quarantine parent.
    assert parent_fsyncs >= 3


@pytest.mark.parametrize(
    "stage",
    (
        "before_manifest",
        "after_move_before_manifest",
        "before_commit_rename",
    ),
)
def test_subprocess_crash_before_commit_recovers_the_pending_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pytestconfig: pytest.Config, stage: str
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"

    _run_crashing_apply(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        stage=stage,
        pytestconfig=pytestconfig,
    )

    report = quarantine_unconfigured_usage_state(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        apply=True,
    )

    assert report.applied is True
    assert report.quarantined_account_ids == tuple(sorted(_FOREIGN_IDS))
    assert report.quarantine_path is not None
    assert _names(root / "current", ".json") == tuple(sorted(_D296_IDS))
    assert not tuple((root / "maintenance-quarantine-v1").glob(".pending-*"))


def test_subprocess_crash_after_commit_rename_leaves_the_completed_transaction_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pytestconfig: pytest.Config
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"

    _run_crashing_apply(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        stage="after_commit_rename",
        pytestconfig=pytestconfig,
    )

    report = quarantine_unconfigured_usage_state(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        apply=True,
    )

    completed = tuple((root / "maintenance-quarantine-v1").glob("transaction-*"))
    assert report.applied is True
    assert report.quarantined_account_ids == ()
    assert len(completed) == 1
    assert _names(root / "current", ".json") == tuple(sorted(_D296_IDS))


def test_subprocess_crash_during_pending_cleanup_recovers_the_empty_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pytestconfig: pytest.Config
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    _run_crashing_apply(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        stage="before_commit_rename",
        pytestconfig=pytestconfig,
    )

    _run_crashing_recovery_cleanup(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        pytestconfig=pytestconfig,
    )

    report = quarantine_unconfigured_usage_state(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        apply=True,
    )

    assert report.applied is True
    assert report.quarantined_account_ids == tuple(sorted(_FOREIGN_IDS))
    assert report.quarantine_path is not None
    assert not tuple((root / "maintenance-quarantine-v1").glob(".pending-*"))


def test_cross_process_maintenance_barrier_blocks_new_account_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pytestconfig: pytest.Config
) -> None:
    _config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    holder = _start_cross_process_lock_holder(
        data_home=data_home,
        state_home=state_home,
        lock_kind="maintenance",
        pytestconfig=pytestconfig,
    )
    try:
        with pytest.raises(AccountLockError, match="already running"):
            with account_lock("account", timeout_seconds=0):
                pass
    finally:
        _stop_cross_process_lock_holder(holder)


def test_cross_process_account_lock_blocks_new_maintenance_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pytestconfig: pytest.Config
) -> None:
    _config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    holder = _start_cross_process_lock_holder(
        data_home=data_home,
        state_home=state_home,
        lock_kind="account",
        pytestconfig=pytestconfig,
    )
    try:
        with pytest.raises(AccountLockError, match="already running"):
            with state_maintenance_lock(timeout_seconds=0):
                pass
    finally:
        _stop_cross_process_lock_holder(holder)


@pytest.mark.parametrize("apply", (False, True))
def test_maintenance_fails_closed_while_a_global_source_writer_holds_the_state_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pytestconfig: pytest.Config,
    apply: bool,
) -> None:
    """Removing source-lock participation lets maintenance race a source writer."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    before = _artifact_bytes(root)
    holder = _start_cross_process_source_lock_holder(
        source_root=root,
        pytestconfig=pytestconfig,
    )
    try:
        with pytest.raises(TimeoutError, match="source lock is already in use"):
            quarantine_unconfigured_usage_state(
                config_path=config_path,
                data_home=data_home,
                state_home=state_home,
                apply=apply,
            )
        assert _artifact_bytes(root) == before
        assert not (root / "maintenance-quarantine-v1").exists()
    finally:
        _stop_cross_process_lock_holder(holder)


def test_config_state_cleanup_and_maintenance_have_a_single_source_first_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pytestconfig: pytest.Config,
) -> None:
    """A Config callback cannot hold Account while waiting for Maintenance's Source."""
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    config_update = _start_config_update_before_source_seam(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        pytestconfig=pytestconfig,
    )
    maintenance = _start_maintenance_apply(
        config_path=config_path,
        data_home=data_home,
        state_home=state_home,
        pytestconfig=pytestconfig,
    )
    try:
        assert maintenance.wait(timeout=3) == 0
        assert maintenance.stdout is not None
        assert maintenance.stdout.read().strip() == (
            "error:TimeoutError:source lock is already in use"
        )

        assert config_update.stdin is not None
        config_update.stdin.write("continue\n")
        config_update.stdin.flush()
        assert config_update.wait(timeout=5) == 0
        assert config_update.stdout is not None
        assert config_update.stdout.read().strip() == "ok"

        report = quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=True,
        )
    finally:
        _stop_process(maintenance)
        _stop_process(config_update)

    assert load_config(config_path).accounts[0].label == "Updated"
    assert load_current_usage("BW_Nufker") is None
    assert report.quarantined_account_ids == tuple(sorted(_FOREIGN_IDS))
    assert _names(root / "current", ".json") == tuple(
        item for item in sorted(_D296_IDS) if item != "BW_Nufker"
    )


def test_apply_rejects_pending_journal_that_targets_a_configured_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, data_home, state_home = _prepared_state(tmp_path, monkeypatch)
    root = data_home / "codex-usage"
    authority = maintenance_module._load_authority_snapshot(config_path, state_home)
    source = root / "current" / "BW_Nufker.json"
    crafted = maintenance_module._Artifact(
        account_id="BW_Nufker",
        kind="current",
        source=source,
        relative_path="current/BW_Nufker.json",
        identity=maintenance_module._private_identity(source, label="test artifact"),
    )
    quarantine_root = root / "maintenance-quarantine-v1"
    ensure_private_directory(quarantine_root, label="test quarantine root")
    pending = quarantine_root / ".pending-33333333333333333333333333333333"
    pending.mkdir(mode=0o700)
    maintenance_module._write_manifest(pending, authority, (crafted,), [crafted])
    destination = pending / crafted.relative_path
    ensure_private_directory(destination.parent, label="test quarantine artifact directory")
    source.rename(destination)

    with pytest.raises(ValueError, match="manifest is invalid"):
        quarantine_unconfigured_usage_state(
            config_path=config_path,
            data_home=data_home,
            state_home=state_home,
            apply=True,
        )

    assert not source.exists()
    assert destination.exists()


def test_quarantine_rename_refuses_to_replace_an_existing_destination(tmp_path: Path) -> None:
    source_parent = tmp_path / "source"
    destination_parent = tmp_path / "destination"
    for directory in (source_parent, destination_parent):
        directory.mkdir(mode=0o700)
    source = source_parent / "entry.json"
    destination = destination_parent / "entry.json"
    _write_private(source, "source")
    _write_private(destination, "destination")

    with pytest.raises(ValueError, match="destination already exists"):
        maintenance_module._rename_no_replace(
            source,
            destination,
            label="test no-replace",
        )

    assert source.read_text(encoding="utf-8") == "source"
    assert destination.read_text(encoding="utf-8") == "destination"
