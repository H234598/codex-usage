from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import json
import multiprocessing
import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "install_integration_producer.py"

TEST_SOURCE_MANIFEST_FILES = (
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


def _temporary_source_copy(destination_root: Path) -> Path:
    destination = destination_root / "source"
    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    for relative_text in TEST_SOURCE_MANIFEST_FILES:
        relative = Path(relative_text)
        source = PROJECT_ROOT / relative
        assert source.is_file() and not source.is_symlink()
        target = destination / relative
        parent = destination
        for part in relative.parts[:-1]:
            parent = parent / part
            parent.mkdir(mode=0o700, exist_ok=True)
            parent.chmod(0o700)
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source_file:
            source_stat = os.fstat(source_file.fileno())
            assert stat.S_ISREG(source_stat.st_mode)
            payload = source_file.read()
        with os.fdopen(
            os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600),
            "wb",
        ) as target_file:
            target_file.write(payload)
    assert {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    } == set(TEST_SOURCE_MANIFEST_FILES)
    return destination


def _foreign_tree_digest(*, root: Path) -> str:
    rows: list[bytes] = []

    def visit(path: Path, relative: str) -> None:
        item = path.lstat()
        mode = stat.S_IMODE(item.st_mode)
        if stat.S_ISDIR(item.st_mode):
            rows.append(f"D {relative}\0{mode:04o}\n".encode())
            for child in sorted(path.iterdir(), key=lambda value: value.name):
                visit(child, f"{relative}/{child.name}")
        elif stat.S_ISREG(item.st_mode):
            rows.append(
                f"F {relative}\0{mode:04o}\0{item.st_size}\0".encode()
                + hashlib.sha256(path.read_bytes()).hexdigest().encode()
                + b"\n"
            )
        elif stat.S_ISLNK(item.st_mode):
            rows.append(f"L {relative}\0{mode:04o}\0{path.readlink()}\n".encode())
        else:
            rows.append(f"X {relative}\0{mode:04o}\n".encode())

    visit(root, root.name)
    return hashlib.sha256(b"".join(rows)).hexdigest()


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    data_home = tmp_path / "data"
    state_home = tmp_path / "state"
    temporary_root = tmp_path / "temporary"
    for path in (data_home, state_home, temporary_root):
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    return data_home, state_home, temporary_root


def test_no_symlink_ancestors_scans_after_missing_segment(tmp_path):
    from codex_usage import integration_installer

    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._no_symlink_ancestors(
            tmp_path / "missing" / ".." / "redirected" / "target"
        )


def _tree_bytes(root: Path) -> tuple[tuple[str, int, bytes], ...]:
    return tuple(
        (
            path.relative_to(root).as_posix(),
            stat.S_IMODE(path.lstat().st_mode),
            path.read_bytes(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def _install(tmp_path: Path):
    from codex_usage.integration_installer import install_release

    data_home, state_home, temporary_root = _roots(tmp_path)
    return (
        install_release(
            source_root=_temporary_source_copy(tmp_path),
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        ),
        data_home,
        state_home,
    )


def test_temporary_source_copy_has_exact_manifest_and_no_untracked_input(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    synthetic_root = tmp_path / "synthetic-project"
    synthetic_root.mkdir(mode=0o700)
    for relative_text in TEST_SOURCE_MANIFEST_FILES:
        target = synthetic_root / relative_text
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(relative_text.encode("utf-8"))
    (synthetic_root / "untracked-secret-marker").write_bytes(b"must-not-copy")
    output_root = tmp_path / "output"
    output_root.mkdir(mode=0o700)
    monkeypatch.setattr(integration_installer, "PROJECT_ROOT", synthetic_root)
    copied = integration_installer._temporary_source_copy(output_root)
    copied_files = {
        path.relative_to(copied).as_posix()
        for path in copied.rglob("*")
        if path.is_file()
    }
    assert copied_files == set(TEST_SOURCE_MANIFEST_FILES)
    assert not (copied / "untracked-secret-marker").exists()


def test_temporary_source_copy_rejects_untracked_symlink_in_existing_destination(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    synthetic_root = tmp_path / "synthetic-project"
    synthetic_root.mkdir(mode=0o700)
    for relative_text in TEST_SOURCE_MANIFEST_FILES:
        target = synthetic_root / relative_text
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(relative_text.encode("utf-8"))
    output_root = tmp_path / "output"
    output_root.mkdir(mode=0o700)
    destination = output_root / "source"
    destination.mkdir(mode=0o700)
    outside = tmp_path / "outside-secret"
    outside.write_bytes(b"must-not-enter-source-tree")
    (destination / "untracked-link").symlink_to(outside)
    monkeypatch.setattr(integration_installer, "PROJECT_ROOT", synthetic_root)

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._temporary_source_copy(output_root)


def test_remove_activation_files_removes_any_python3_minor_entry(tmp_path):
    from codex_usage import integration_installer

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("activate", "python3", "python3.11", "python3.14", "python"):
        (bin_dir / name).write_text("placeholder", encoding="utf-8")

    integration_installer._remove_activation_files(tmp_path)

    assert not (bin_dir / "activate").exists()
    assert not (bin_dir / "python3").exists()
    assert not (bin_dir / "python3.11").exists()
    assert not (bin_dir / "python3.14").exists()
    assert (bin_dir / "python").exists()


def test_foreign_tree_digest_detects_same_size_bytes_and_symlink_target(tmp_path):
    root = tmp_path / "foreign"
    root.mkdir(mode=0o700)
    payload = root / "payload"
    payload.write_bytes(b"aa")
    first = _foreign_tree_digest(root=root)
    payload.write_bytes(b"bb")
    assert _foreign_tree_digest(root=root) != first
    target_a, target_b = root / "target-a", root / "target-b"
    target_a.write_bytes(b"a")
    target_b.write_bytes(b"b")
    link = root / "foreign-link"
    link.symlink_to(target_a.name)
    linked_first = _foreign_tree_digest(root=root)
    link.unlink()
    link.symlink_to(target_b.name)
    assert _foreign_tree_digest(root=root) != linked_first


def test_release_version_is_06532_in_project_and_package():
    assert 'version = "0.6.532"' in (PROJECT_ROOT / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert '__version__ = "0.6.532"' in (
        PROJECT_ROOT / "src" / "codex_usage" / "__init__.py"
    ).read_text(encoding="utf-8")


def test_install_creates_attested_private_active_release(tmp_path):
    release, data_home, state_home = _install(tmp_path)
    from codex_usage.integration_attestation import verify_active_release

    assert release.version == "0.6.532"
    assert release.launcher_path.name == "codex-usage"
    assert stat.S_IMODE(release.launcher_path.lstat().st_mode) == 0o700
    verified = verify_active_release(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=release.entrypoint_path,
    )
    assert verified == release
    active = json.loads(
        (state_home / "codex-usage" / "integration" / "active.json").read_text(
            encoding="utf-8"
        )
    )
    assert active["version"] == "0.6.532"
    assert active["launcher_sha256"] == release.launcher_sha256
    assert active["release_tree_sha256"] == release.release_tree_sha256
    integration = state_home / "codex-usage" / "integration"
    assert stat.S_IMODE(integration.lstat().st_mode) == 0o700
    assert stat.S_IMODE((integration / "active.json").lstat().st_mode) == 0o600
    assert not list(release.release_dir.rglob("*.json"))
    assert not list((tmp_path / "temporary").rglob("candidate-*.json"))


def test_attestation_requires_exact_integer_schema_version(tmp_path):
    from codex_usage.integration_attestation import (
        IntegrationAttestationUnavailable,
        verify_active_release,
    )

    release, data_home, state_home = _install(tmp_path)
    active_path = state_home / "codex-usage" / "integration" / "active.json"
    assert (
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.entrypoint_path,
        )
        == release
    )
    manifest = json.loads(active_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = True
    active_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    before = active_path.read_bytes()
    with pytest.raises(IntegrationAttestationUnavailable):
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.entrypoint_path,
        )
    assert active_path.read_bytes() == before


def test_source_drift_before_active_swap_keeps_prior_active_release(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    release, data_home, state_home = _install(tmp_path)
    second_root = tmp_path / "second"
    second_root.mkdir(mode=0o700)
    source = _temporary_source_copy(second_root)
    temporary_root = second_root / "temporary"
    temporary_root.mkdir(mode=0o700)
    original_rehash = integration_installer._rehash_source_manifest
    calls = 0

    def drift_on_second_hash(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_rehash(*args, **kwargs)
        return {"changed.py": "0" * 64}

    monkeypatch.setattr(integration_installer, "_rehash_source_manifest", drift_on_second_hash)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    assert calls >= 2
    from codex_usage.integration_attestation import verify_active_release

    assert (
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.entrypoint_path,
        )
        == release
    )


def test_rollback_revalidates_prior_manifest_and_swaps_only_active_json(tmp_path):
    from codex_usage.integration_attestation import verify_active_release
    from codex_usage.integration_installer import rollback_active_release
    from codex_usage.private_io import write_private_text

    first, data_home, state_home = _install(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    write_private_text(
        integration / "previous.json",
        active_path.read_text(encoding="utf-8"),
        label="synthetic previous manifest",
        mode=0o600,
    )
    write_private_text(
        active_path,
        '{"schema_version":1,"version":"broken"}',
        label="synthetic broken active manifest",
        mode=0o600,
    )
    rolled_back = rollback_active_release(state_home=state_home, data_home=data_home)
    assert rolled_back == first
    assert (
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=first.entrypoint_path,
        )
        == first
    )


def test_generated_wheel_has_no_general_cli_or_forbidden_modules(tmp_path):
    release, _, _ = _install(tmp_path)
    with zipfile.ZipFile(release.release_dir / "producer.whl") as wheel:
        names = set(wheel.namelist())
    assert "codex_usage/cli.py" not in names
    assert not any(name.startswith("codex_usage/browser") for name in names)
    assert "codex_usage/integration_entrypoint.py" in names
    assert "codex_usage/integration_attestation.py" in names
    assert "codex_usage/integration_installer.py" not in names


def test_launcher_uses_isolated_python_and_fixed_environment(tmp_path):
    release, data_home, state_home = _install(tmp_path)
    launcher = release.launcher_path.read_text(encoding="utf-8")
    assert " -B -I -m codex_usage.integration_entrypoint" in launcher
    assert str(data_home) in launcher
    assert str(state_home) in launcher
    assert "codex_usage.cli" not in launcher
    assert "PYTHONPATH" in launcher
    assert launcher.splitlines()[0] == "#!/bin/sh"


def test_verify_rejects_record_or_launcher_drift_without_active_repair(tmp_path):
    release, data_home, state_home = _install(tmp_path)
    active_path = state_home / "codex-usage" / "integration" / "active.json"
    old = active_path.read_bytes()
    release.launcher_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    from codex_usage.integration_attestation import (
        IntegrationAttestationUnavailable,
        verify_active_release,
    )

    with pytest.raises(IntegrationAttestationUnavailable):
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.entrypoint_path,
        )
    assert active_path.read_bytes() == old


def test_installer_build_subprocess_is_no_index_and_sanitized(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    observed: dict[str, object] = {}

    class FakeProcess:
        pid = 4321

        def wait(self, timeout=None):
            observed["timeout"] = timeout
            return 1

        def poll(self):
            return 1

    def fake_verified_wheel_popen(argv, **kwargs):
        observed["argv"] = tuple(argv)
        observed["env"] = dict(kwargs["env"])
        observed["cwd"] = kwargs["cwd"]
        observed["stdout"] = kwargs["stdout"]
        observed["stderr"] = kwargs["stderr"]
        observed["start_new_session"] = kwargs["start_new_session"]
        return FakeProcess()

    killed: list[int] = []
    monkeypatch.setattr(
        integration_installer,
        "_kill_process_group",
        lambda process_group_id: killed.append(process_group_id),
    )
    monkeypatch.setattr(
        integration_installer,
        "_run_builder_preflight",
        lambda **kwargs: subprocess.CompletedProcess(
            [
                str(kwargs["python_executable"]),
                "-I",
                "-c",
                integration_installer._BUILDER_PREFLIGHT_CODE,
            ],
            0,
            '{"backend":"setuptools.command.bdist_wheel.bdist_wheel",'
            '"setuptools":"80.10.2"}\n',
            "",
        ),
    )
    monkeypatch.setattr(integration_installer.subprocess, "Popen", fake_verified_wheel_popen)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._build_verified_wheel(
            build_root=tmp_path,
            python_executable=Path(sys.executable),
            wheel_dir=tmp_path / "wheel",
            environment=integration_installer._sanitized_build_environment(),
        )
    assert "--no-index" in observed["argv"]
    assert observed["env"]["PIP_NO_INDEX"] == "1"
    assert "PYTHONPATH" not in observed["env"]
    assert observed["timeout"] == 120
    assert observed["stdout"] == subprocess.DEVNULL
    assert observed["stderr"] == subprocess.DEVNULL
    assert observed["cwd"] == tmp_path
    assert observed["start_new_session"] is True
    assert killed == [4321]


def test_installer_builder_timeout_kills_descendants(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    child_pid_path = tmp_path / "builder-child.pid"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "sleep 30 &\n"
        "echo $! > \"$BUILDER_CHILD_PID\"\n"
        "wait\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    environment = integration_installer._sanitized_build_environment()
    environment["BUILDER_CHILD_PID"] = str(child_pid_path)
    monkeypatch.setattr(
        integration_installer,
        "_run_builder_preflight",
        lambda **kwargs: subprocess.CompletedProcess(
            [str(kwargs["python_executable"])],
            0,
            '{"backend":"setuptools.command.bdist_wheel.bdist_wheel",'
            '"setuptools":"80.10.2"}\n',
            "",
        ),
    )
    monkeypatch.setattr(integration_installer, "BUILDER_WHEEL_TIMEOUT_SECONDS", 1)
    build_root = tmp_path / "build"
    build_root.mkdir(mode=0o700)
    build_root.chmod(0o700)

    child_pid = None
    try:
        with pytest.raises(integration_installer.IntegrationInstallError):
            integration_installer._build_verified_wheel(
                python_executable=fake_python,
                environment=environment,
                build_root=build_root,
                wheel_dir=tmp_path / "wheel",
            )
        pid_deadline = time.monotonic() + 2
        while time.monotonic() < pid_deadline and not child_pid_path.exists():
            time.sleep(0.01)
        if not child_pid_path.exists():
            pytest.fail("builder child never reported its pid")
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        exit_deadline = time.monotonic() + 2
        while time.monotonic() < exit_deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            pytest.fail("builder timeout left descendant process running")
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass


def test_installer_successful_builder_kills_descendants(tmp_path):
    from codex_usage import integration_installer

    child_pid_path = tmp_path / "builder-child.pid"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "sleep 30 &\n"
        "echo $! > \"$BUILDER_CHILD_PID\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    environment = integration_installer._sanitized_build_environment()
    environment["BUILDER_CHILD_PID"] = str(child_pid_path)

    result = integration_installer._run_builder_bounded(
        [str(fake_python)],
        env=environment,
        cwd=tmp_path,
    )
    assert result.returncode == 0

    pid_deadline = time.monotonic() + 2
    while time.monotonic() < pid_deadline and not child_pid_path.exists():
        time.sleep(0.01)
    if not child_pid_path.exists():
        pytest.fail("builder child never reported its pid")
    child_pid = int(child_pid_path.read_text(encoding="ascii"))
    exit_deadline = time.monotonic() + 2
    while time.monotonic() < exit_deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("successful builder left descendant process running")


def test_install_cleanup_failure_preserves_original_error_as_cause(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source = _temporary_source_copy(tmp_path)

    def fail_build(**_kwargs):
        raise integration_installer.IntegrationInstallError()

    monkeypatch.setattr(integration_installer, "_build_verified_wheel", fail_build)
    monkeypatch.setattr(
        integration_installer,
        "_cleanup_owned_directory",
        lambda *_args: False,
    )
    with pytest.raises(integration_installer.IntegrationCleanupError) as error:
        integration_installer.install_release(
            source_root=source,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    assert isinstance(error.value.__cause__, integration_installer.IntegrationInstallError)


def test_builder_preflight_has_bounded_timeout_and_streams_only_json(monkeypatch):
    from codex_usage import integration_installer

    calls: list[dict[str, object]] = []

    def fake_preflight_run(**kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(
            [
                str(kwargs["python_executable"]),
                "-I",
                "-c",
                integration_installer._BUILDER_PREFLIGHT_CODE,
            ],
            0,
            '{"backend":"setuptools.command.bdist_wheel.bdist_wheel",'
            '"setuptools":"80.10.2"}\n',
            "",
        )

    monkeypatch.setattr(integration_installer, "_run_builder_preflight", fake_preflight_run)
    integration_installer._require_offline_builder(
        python_executable=Path(sys.executable),
        environment=integration_installer._sanitized_build_environment(),
    )

    assert calls[0]["python_executable"] == Path(sys.executable)
    assert calls[0]["environment"]["PIP_NO_INDEX"] == "1"
    assert integration_installer.BUILDER_PREFLIGHT_TIMEOUT_SECONDS == 30
    assert integration_installer.BUILDER_PREFLIGHT_MAX_OUTPUT_BYTES == 64 * 1024


def test_builder_preflight_rejects_oversized_output_before_process_finishes(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    marker = tmp_path / "finished"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        f"{sys.executable} -c \"import os, pathlib, sys, time; "
        f"sys.stdout.write('x' * "
        f"({integration_installer.BUILDER_PREFLIGHT_MAX_OUTPUT_BYTES} + 1)); "
        "sys.stdout.flush(); time.sleep(0.3); "
        "pathlib.Path(os.environ['BUILDER_MARKER']).touch()\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    environment = integration_installer._sanitized_build_environment()
    environment["BUILDER_MARKER"] = str(marker)

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._require_offline_builder(
            python_executable=fake_python,
            environment=environment,
        )
    assert not marker.exists()


def _write_launcher_state(data_home: Path) -> None:
    from codex_usage.models import AccountUsage
    from codex_usage.private_io import write_private_text

    usage = AccountUsage(
        account_id="alpha",
        label="never-exported-label",
        captured_at=datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
        backend_configured="direct",
        backend_used="direct",
    )
    current = data_home / "codex-usage" / "current"
    current.mkdir(parents=True, mode=0o700)
    current.chmod(0o700)
    body = usage.as_dict() | {"state_generation": 0}
    write_private_text(
        current / "alpha.json",
        json.dumps(body),
        label="synthetic launcher state",
    )


def test_temporary_launcher_emits_schema1_from_temporary_state(tmp_path):
    release, data_home, state_home = _install(tmp_path)
    _write_launcher_state(data_home)
    completed = subprocess.run(
        [
            str(release.launcher_path),
            "integration-snapshot",
            "--schema",
            "1",
            "--format",
            "json",
        ],
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == 1
    assert "never-exported-label" not in completed.stdout
    assert "backend" not in completed.stdout
    assert completed.stderr == ""
    assert (state_home / "codex-usage" / "integration" / "account-usage-v1.json").is_file()


def test_installer_module_has_no_network_import():
    import codex_usage.integration_installer as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        for alias in (node.names if isinstance(node, ast.Import) else ())
    }
    imported |= {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imported & {"socket", "urllib", "http", "requests"}


def test_attestation_module_has_no_installer_or_mutation_capability_import():
    import codex_usage.integration_attestation as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported |= {
        f"codex_usage.{node.module}" if node.level == 1 else node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imported & {
        "subprocess",
        "venv",
        "zipfile",
        "shutil",
        "codex_usage.integration_installer",
        "codex_usage.cli",
    }
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "write_private_text" not in source
    assert "os.replace" not in source


def test_temporary_launcher_keeps_final_release_tree_and_attestation_unchanged(tmp_path):
    from codex_usage.integration_attestation import _release_tree_sha256

    release, data_home, state_home = _install(tmp_path)
    _write_launcher_state(data_home)
    active = state_home / "codex-usage" / "integration" / "active.json"
    before_tree = _release_tree_sha256(release_dir=release.release_dir)
    before_active = active.read_bytes()
    completed = subprocess.run(
        [
            str(release.launcher_path),
            "integration-snapshot",
            "--schema",
            "1",
            "--format",
            "json",
        ],
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0
    assert not list(release.release_dir.rglob("__pycache__"))
    assert not list(release.release_dir.rglob("*.pyc"))
    assert _release_tree_sha256(release_dir=release.release_dir) == before_tree
    assert active.read_bytes() == before_active


@pytest.mark.parametrize(
    "relative",
    [Path("venv/bin/python"), Path("venv/pyvenv.cfg"), Path("extra-regular-file")],
)
def test_runtime_rejects_release_tree_drift_without_repair(tmp_path, relative):
    from codex_usage.integration_attestation import (
        IntegrationAttestationUnavailable,
        verify_active_release,
    )

    release, data_home, state_home = _install(tmp_path)
    active = state_home / "codex-usage" / "integration" / "active.json"
    before = active.read_bytes()
    target = release.release_dir / relative
    if relative.name == "extra-regular-file":
        target.write_bytes(b"extra")
    else:
        target.write_bytes(target.read_bytes() + b"x")
    with pytest.raises(IntegrationAttestationUnavailable):
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.entrypoint_path,
        )
    assert active.read_bytes() == before


def test_installer_cleans_only_its_temporary_build_children(tmp_path):
    from codex_usage.integration_installer import install_release

    data_home, state_home, temporary_root = _roots(tmp_path)
    preserved = temporary_root / "caller-preserved"
    preserved.mkdir(mode=0o700)
    preserved_marker = preserved / "marker"
    preserved_marker.write_bytes(b"keep")
    install_release(
        source_root=_temporary_source_copy(tmp_path),
        state_home=state_home,
        data_home=data_home,
        python_executable=Path(sys.executable),
        temporary_root=temporary_root,
    )
    assert preserved_marker.read_bytes() == b"keep"
    assert not list(temporary_root.rglob("__pycache__"))
    assert not list(temporary_root.rglob("*.pyc"))


REGULAR_ATTR = (stat.S_IFREG | 0o600) << 16


@pytest.mark.parametrize(
    "member_name, external_attr, reason",
    [
        ("codex_usage/a.py", REGULAR_ATTR, "duplicate_member"),
        ("/absolute.py", REGULAR_ATTR, "unsafe_path"),
        ("../escape.py", REGULAR_ATTR, "unsafe_path"),
        ("a\\b.py", REGULAR_ATTR, "unsafe_path"),
        ("a/../escape.py", REGULAR_ATTR, "unsafe_path"),
        ("codex_usage/link.py", (stat.S_IFLNK | 0o777) << 16, "symlink_member"),
        ("codex_usage/fifo", (stat.S_IFIFO | 0o600) << 16, "nonregular_member"),
    ],
)
def test_safe_extract_rejects_each_member_class_without_destination_change(
    tmp_path,
    member_name,
    external_attr,
    reason,
):
    from codex_usage.integration_installer import (
        _safe_extract_wheel,
        _WheelMemberValidationError,
    )

    wheel = tmp_path / "candidate.whl"
    destination = tmp_path / "destination"
    with zipfile.ZipFile(wheel, "w") as archive:
        first = zipfile.ZipInfo(member_name)
        first.external_attr = external_attr
        archive.writestr(first, b"x")
        if member_name == "codex_usage/a.py":
            with pytest.warns(UserWarning):
                archive.writestr("codex_usage/a.py", b"duplicate")
    destination.mkdir(mode=0o700)
    before = _tree_bytes(destination)
    rows = {member_name: (hashlib.sha256(b"x").hexdigest(), 1)}
    with pytest.raises(_WheelMemberValidationError) as error:
        _safe_extract_wheel(
            wheel_path=wheel,
            destination=destination,
            record_rows=rows,
        )
    assert error.value.reason == reason
    assert _tree_bytes(destination) == before


def test_safe_extract_accepts_matching_regular_member(tmp_path):
    from codex_usage.integration_installer import _safe_extract_wheel

    wheel = tmp_path / "candidate.whl"
    destination = tmp_path / "destination"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("codex_usage/ok.py", b"x")
    destination.mkdir(mode=0o700)
    _safe_extract_wheel(
        wheel_path=wheel,
        destination=destination,
        record_rows={"codex_usage/ok.py": (hashlib.sha256(b"x").hexdigest(), 1)},
    )
    target = destination / "codex_usage" / "ok.py"
    assert target.read_bytes() == b"x"
    assert stat.S_IMODE(target.lstat().st_mode) == 0o600
    assert _tree_bytes(destination) == (("codex_usage/ok.py", 0o600, b"x"),)


def test_copy_regular_binds_mode_change_to_open_file(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    source = tmp_path / "source"
    source.write_bytes(b"source")
    source.chmod(0o600)
    parent = tmp_path / "destination"
    parent.mkdir(mode=0o700)
    target = parent / "target"
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    outside.chmod(0o644)
    original_chmod = Path.chmod

    def replace_target_before_path_chmod(path, mode):
        if path == target:
            target.unlink()
            target.symlink_to(outside)
        return original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", replace_target_before_path_chmod)
    integration_installer._copy_regular(source, target)

    assert target.is_file() and not target.is_symlink()
    assert stat.S_IMODE(target.lstat().st_mode) == 0o600
    assert stat.S_IMODE(outside.lstat().st_mode) == 0o644


def test_safe_extract_binds_mode_change_to_open_file(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    wheel = tmp_path / "candidate.whl"
    destination = tmp_path / "destination"
    destination.mkdir(mode=0o700)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("codex_usage/ok.py", b"x")
    target = destination / "codex_usage" / "ok.py"
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    outside.chmod(0o644)
    original_chmod = Path.chmod

    def replace_target_before_path_chmod(path, mode):
        if path == target:
            target.unlink()
            target.symlink_to(outside)
        return original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", replace_target_before_path_chmod)
    integration_installer._safe_extract_wheel(
        wheel_path=wheel,
        destination=destination,
        record_rows={"codex_usage/ok.py": (hashlib.sha256(b"x").hexdigest(), 1)},
    )

    assert target.is_file() and not target.is_symlink()
    assert stat.S_IMODE(target.lstat().st_mode) == 0o600
    assert stat.S_IMODE(outside.lstat().st_mode) == 0o644


def test_write_exclusive_binds_mode_change_to_open_file(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    target = parent / "target"
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    outside.chmod(0o644)
    original_chmod = Path.chmod

    def replace_target_before_path_chmod(path, mode):
        if path == target:
            target.unlink()
            target.symlink_to(outside)
        return original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", replace_target_before_path_chmod)
    identity = integration_installer._write_exclusive(target, b"payload", mode=0o600)

    assert identity.permissions == 0o600
    assert target.is_file() and not target.is_symlink()
    assert target.read_bytes() == b"payload"
    assert stat.S_IMODE(outside.lstat().st_mode) == 0o644


def test_safe_extract_rejects_oversized_member_before_materializing(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    wheel = tmp_path / "candidate.whl"
    destination = tmp_path / "destination"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("codex_usage/oversized.py", b"xxxxx")
    destination.mkdir(mode=0o700)
    monkeypatch.setattr(integration_installer, "MAX_INSTALL_FILE_BYTES", 4)

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._safe_extract_wheel(
            wheel_path=wheel,
            destination=destination,
            record_rows={
                "codex_usage/oversized.py": (hashlib.sha256(b"xxxxx").hexdigest(), 5)
            },
        )
    assert not (destination / "codex_usage" / "oversized.py").exists()


@pytest.mark.parametrize("parser", ["details", "extract"])
def test_wheel_parsers_reject_oversized_archive_before_zip_open(tmp_path, monkeypatch, parser):
    from codex_usage import integration_installer

    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"xxxxx")
    monkeypatch.setattr(integration_installer, "MAX_INSTALL_FILE_BYTES", 4)

    def unexpected_zip_open(*args, **kwargs):
        raise AssertionError("oversized archive must be rejected before ZipFile")

    monkeypatch.setattr(integration_installer.zipfile, "ZipFile", unexpected_zip_open)
    with pytest.raises(integration_installer.IntegrationInstallError):
        if parser == "details":
            integration_installer._wheel_details(wheel)
        else:
            integration_installer._safe_extract_wheel(
                wheel_path=wheel,
                destination=tmp_path / "destination",
                record_rows={},
            )


def test_bounded_wheel_infos_rejects_too_many_members_before_iteration(monkeypatch):
    from codex_usage import integration_installer

    class _IterationBomb(list):
        def __iter__(self):
            for index, value in enumerate(super().__iter__()):
                if index >= 1:
                    raise AssertionError("bounded wheel reader iterated after limit")
                yield value

    class _Archive:
        def infolist(self):
            return _IterationBomb([object(), object()])

    monkeypatch.setattr(integration_installer, "MAX_RELEASE_TREE_ENTRIES", 1)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._bounded_wheel_infos(_Archive())


def test_bootstrap_creates_only_two_private_children_and_rejects_identity_drift(
    tmp_path,
    monkeypatch,
):
    from codex_usage import integration_installer

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    app_identity, integration_identity = integration_installer._bootstrap_integration_dir(
        state_home
    )
    assert stat.S_IMODE((state_home / "codex-usage").lstat().st_mode) == 0o700
    assert (
        stat.S_IMODE((state_home / "codex-usage" / "integration").lstat().st_mode)
        == 0o700
    )
    assert (state_home / "codex-usage").lstat().st_nlink >= 2
    assert (state_home / "codex-usage" / "integration").lstat().st_nlink >= 2
    assert app_identity.permissions == integration_identity.permissions == 0o700
    assert sorted(path.name for path in state_home.iterdir()) == ["codex-usage"]
    changed = type(integration_identity)(
        integration_identity.device,
        integration_identity.inode + 1,
        integration_identity.permissions,
    )
    calls = iter((app_identity, integration_identity, app_identity, changed))
    monkeypatch.setattr(
        integration_installer,
        "_require_private_dir",
        lambda *args, **kwargs: next(calls),
    )
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._revalidate_bootstrap(
            state_home,
            app_identity,
            integration_identity,
        )


def test_offline_builder_preflight_requires_local_setuptools_backend(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    observed: list[tuple[str, ...]] = []

    def fake_preflight_success_run(**kwargs):
        observed.append(
            (
                str(kwargs["python_executable"]),
                "-I",
                "-c",
                integration_installer._BUILDER_PREFLIGHT_CODE,
            )
        )
        return subprocess.CompletedProcess(
            observed[-1],
            0,
            '{"backend":"setuptools.command.bdist_wheel.bdist_wheel",'
            '"setuptools":"80.10.2"}\n',
            "",
        )

    monkeypatch.setattr(integration_installer, "_run_builder_preflight", fake_preflight_success_run)
    integration_installer._require_offline_builder(
        python_executable=Path(sys.executable),
        environment=integration_installer._sanitized_build_environment(),
    )
    assert observed == [
        (str(Path(sys.executable)), "-I", "-c", integration_installer._BUILDER_PREFLIGHT_CODE)
    ]


@pytest.mark.parametrize(
    "returncode, stdout",
    [
        (1, ""),
        (0, ""),
        (0, "not-json\n"),
        (
            0,
            '{"backend":"setuptools.command.bdist_wheel.bdist_wheel",'
            '"setuptools":"80.10.2"}\nextra\n',
        ),
        (0, '{"backend":"setuptools.command.bdist_wheel.bdist_wheel","setuptools":"76.9.0"}\n'),
        (0, '{"backend":"other.backend","setuptools":"80.10.2"}\n'),
    ],
)
def test_offline_builder_rejects_before_pip_wheel(monkeypatch, returncode, stdout):
    from codex_usage import integration_installer

    observed: list[tuple[str, ...]] = []

    def fake_preflight_failure_run(**kwargs):
        argv = (
            str(kwargs["python_executable"]),
            "-I",
            "-c",
            integration_installer._BUILDER_PREFLIGHT_CODE,
        )
        observed.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(integration_installer, "_run_builder_preflight", fake_preflight_failure_run)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._build_verified_wheel(
            python_executable=Path(sys.executable),
            environment=integration_installer._sanitized_build_environment(),
            build_root=Path("/tmp/build-not-created"),
            wheel_dir=Path("/tmp/wheel-not-created"),
        )
    assert observed == [
        (str(Path(sys.executable)), "-I", "-c", integration_installer._BUILDER_PREFLIGHT_CODE)
    ]


def test_attestation_reader_rejects_oversized_release_file(tmp_path):
    from codex_usage import integration_attestation

    path = tmp_path / "release-file"
    path.write_bytes(b"x" * (integration_attestation.MAX_ATTESTATION_FILE_BYTES + 1))
    path.chmod(0o600)

    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation._file_bytes(path, mode=0o600)


def test_attestation_reader_rechecks_private_mode_after_open(tmp_path, monkeypatch):
    from codex_usage import integration_attestation

    path = tmp_path / "release-file"
    path.write_bytes(b"private payload")
    path.chmod(0o600)
    original_open = integration_attestation.os.open
    swapped = False

    def swap_mode_before_open(candidate, flags, *args):
        nonlocal swapped
        if candidate == path and not swapped:
            swapped = True
            path.chmod(0o644)
        return original_open(candidate, flags, *args)

    monkeypatch.setattr(integration_attestation.os, "open", swap_mode_before_open)

    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation._file_bytes(path, mode=0o600)


def test_attestation_tree_rejects_aggregate_bytes_limit(tmp_path, monkeypatch):
    from codex_usage import integration_attestation

    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    payload = release / "payload"
    payload.write_bytes(b"xx")
    payload.chmod(0o600)
    monkeypatch.setattr(integration_attestation, "MAX_RELEASE_TREE_BYTES", 1, raising=False)

    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation._release_tree_sha256(release_dir=release)


def test_attestation_tree_rejects_entry_limit(tmp_path, monkeypatch):
    from codex_usage import integration_attestation

    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    payload = release / "payload"
    payload.write_bytes(b"x")
    payload.chmod(0o600)
    monkeypatch.setattr(integration_attestation, "MAX_RELEASE_TREE_ENTRIES", 1, raising=False)

    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation._release_tree_sha256(release_dir=release)


def test_installer_record_parser_rejects_oversized_csv_before_materializing(
    monkeypatch,
):
    from codex_usage import integration_installer

    monkeypatch.setattr(integration_installer, "MAX_RELEASE_TREE_ENTRIES", 2, raising=False)
    payload = b"a,,\nb,,\nc,,\n"

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._parse_record(payload)


def test_installer_record_parser_does_not_split_all_lines():
    from codex_usage import integration_installer

    class NoSplitText(str):
        def splitlines(self, *args, **kwargs):
            raise AssertionError("RECORD parser must stream CSV lines")

    class NoSplitBytes(bytes):
        def decode(self, encoding="utf-8", errors="strict"):
            return NoSplitText(super().decode(encoding, errors))

    payload = NoSplitBytes(b"a,,\nb,,\n")

    assert integration_installer._parse_record(payload) == {
        "a": ("", -1),
        "b": ("", -1),
    }


def test_installer_record_parser_rejects_oversized_file_size_without_raising():
    from codex_usage import integration_installer

    payload = f"a,sha256=x,{'9' * 5000}\n".encode()

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._parse_record(payload)


def test_attestation_record_parser_rejects_oversized_csv_before_materializing(
    tmp_path, monkeypatch
):
    from codex_usage import integration_attestation

    release = tmp_path / "release"
    site_packages = release / "site-packages"
    dist_info = site_packages / "dist-info"
    dist_info.mkdir(mode=0o700, parents=True)
    release.chmod(0o700)
    site_packages.chmod(0o700)
    record = dist_info / "RECORD"
    other = dist_info / "OTHER"
    other.write_bytes(b"x")
    other.chmod(0o600)
    record.write_bytes(b"dist-info/RECORD,,\ndist-info/OTHER,,\nthird,,\n")
    record.chmod(0o600)
    monkeypatch.setattr(integration_attestation, "MAX_RELEASE_TREE_ENTRIES", 2, raising=False)

    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation._record_rows(record, release)


def test_attestation_record_parser_rejects_duplicate_paths(tmp_path):
    from codex_usage import integration_attestation

    release = tmp_path / "release"
    site_packages = release / "site-packages"
    dist_info = site_packages / "dist-info"
    target_dir = site_packages / "codex_usage"
    dist_info.mkdir(mode=0o700, parents=True)
    target_dir.mkdir(mode=0o700)
    release.chmod(0o700)
    site_packages.chmod(0o700)
    target = target_dir / "ok.py"
    target.write_bytes(b"x")
    target.chmod(0o600)
    digest = base64.urlsafe_b64encode(hashlib.sha256(b"x").digest()).decode().rstrip("=")
    record = dist_info / "RECORD"
    record.write_text(
        "dist-info/RECORD,,\n"
        f"codex_usage/ok.py,sha256={digest},1\n"
        f"codex_usage/ok.py,sha256={digest},1\n",
        encoding="utf-8",
    )
    record.chmod(0o600)

    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation._record_rows(record, release)


def test_attestation_record_parser_rejects_oversized_file_size_without_raising(tmp_path):
    from codex_usage import integration_attestation

    release = tmp_path / "release"
    site_packages = release / "site-packages"
    dist_info = site_packages / "dist-info"
    target_dir = site_packages / "codex_usage"
    dist_info.mkdir(mode=0o700, parents=True)
    target_dir.mkdir(mode=0o700)
    release.chmod(0o700)
    site_packages.chmod(0o700)
    target = target_dir / "ok.py"
    target.write_bytes(b"x")
    target.chmod(0o600)
    digest = base64.urlsafe_b64encode(hashlib.sha256(b"x").digest()).decode().rstrip("=")
    record = dist_info / "RECORD"
    record.write_text(
        "dist-info/RECORD,,\n"
        f"codex_usage/ok.py,sha256={digest},{'9' * 5000}\n",
        encoding="utf-8",
    )
    record.chmod(0o600)

    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation._record_rows(record, release)


def test_attestation_record_parser_rejects_missing_digest_for_existing_target(tmp_path):
    from codex_usage import integration_attestation

    release = tmp_path / "release"
    site_packages = release / "site-packages"
    dist_info = site_packages / "dist-info"
    target_dir = site_packages / "codex_usage"
    dist_info.mkdir(mode=0o700, parents=True)
    target_dir.mkdir(mode=0o700)
    release.chmod(0o700)
    site_packages.chmod(0o700)
    target = target_dir / "ok.py"
    target.write_bytes(b"x")
    target.chmod(0o600)
    record = dist_info / "RECORD"
    record.write_text(
        "dist-info/RECORD,,\n"
        "codex_usage/ok.py,,\n",
        encoding="utf-8",
    )
    record.chmod(0o600)

    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation._record_rows(record, release)


def test_attestation_record_parser_enforces_row_limit_when_targets_exist(
    tmp_path, monkeypatch
):
    from codex_usage import integration_attestation

    release = tmp_path / "release"
    site_packages = release / "site-packages"
    dist_info = site_packages / "dist-info"
    target_dir = site_packages / "codex_usage"
    dist_info.mkdir(mode=0o700, parents=True)
    target_dir.mkdir(mode=0o700)
    release.chmod(0o700)
    site_packages.chmod(0o700)
    rows = ["dist-info/RECORD,,"]
    for name in ("one.py", "two.py"):
        target = target_dir / name
        target.write_bytes(b"x")
        target.chmod(0o600)
        digest = base64.urlsafe_b64encode(hashlib.sha256(b"x").digest()).decode().rstrip("=")
        rows.append(f"codex_usage/{name},sha256={digest},1")
    record = dist_info / "RECORD"
    record.write_text("\n".join(rows) + "\n", encoding="utf-8")
    record.chmod(0o600)
    monkeypatch.setattr(integration_attestation, "MAX_RELEASE_TREE_ENTRIES", 2)

    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation._record_rows(record, release)


def test_postwalk_release_rejects_entry_limit_before_unbounded_rglob(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    (release / "payload").write_bytes(b"x")
    monkeypatch.setattr(integration_installer, "MAX_RELEASE_TREE_ENTRIES", 1, raising=False)

    def unbounded_rglob(self, pattern):
        yield self / "payload"
        raise AssertionError("postwalk must not enumerate with unbounded rglob")

    monkeypatch.setattr(Path, "rglob", unbounded_rglob)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._postwalk_release(release)


def test_installer_reader_rejects_oversized_file_before_materializing(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    path = tmp_path / "installer-file"
    path.write_bytes(b"xxxxx")
    path.chmod(0o600)
    monkeypatch.setattr(integration_installer, "MAX_INSTALL_FILE_BYTES", 4, raising=False)

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._read_nofollow(path)


def test_installer_reader_rejects_foreign_owner(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    path = tmp_path / "installer-file"
    path.write_bytes(b"payload")
    path.chmod(0o600)
    monkeypatch.setattr(integration_installer.os, "getuid", lambda: 2**31 - 1)

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._read_nofollow(path)


def test_final_release_collision_is_immutable_and_staging_never_leaks_into_manifest_or_launcher(
    tmp_path,
):
    from codex_usage import integration_installer

    release, data_home, state_home = _install(tmp_path)
    manifest = json.loads(
        (state_home / "codex-usage" / "integration" / "active.json").read_text(
            encoding="utf-8"
        )
    )
    manifest_text = json.dumps(manifest, sort_keys=True)
    launcher_text = release.launcher_path.read_text(encoding="utf-8")
    assert ".staging-" not in manifest_text
    assert ".staging-" not in launcher_text
    before_active = (state_home / "codex-usage" / "integration" / "active.json").read_bytes()
    repeat_root = tmp_path / "repeat"
    repeat_root.mkdir(mode=0o700)
    repeat_temporary = repeat_root / "temporary"
    repeat_temporary.mkdir(mode=0o700)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=_temporary_source_copy(repeat_root),
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=repeat_temporary,
        )
    assert (
        state_home / "codex-usage" / "integration" / "active.json"
    ).read_bytes() == before_active
    assert release.release_dir.is_dir()


def test_two_valid_releases_bind_runtime_to_executing_entrypoint_and_rollback(tmp_path):
    from codex_usage.integration_attestation import (
        IntegrationAttestationUnavailable,
        verify_active_release,
    )
    from codex_usage.integration_installer import install_release, rollback_active_release

    first, data_home, state_home = _install(tmp_path)
    source_root = tmp_path / "source-b-root"
    temporary_root = tmp_path / "temporary-b-root"
    source_root.mkdir(mode=0o700)
    temporary_root.mkdir(mode=0o700)
    second_source = _temporary_source_copy(source_root)
    second_entrypoint = second_source / "src/codex_usage/integration_snapshot.py"
    second_entrypoint.write_bytes(second_entrypoint.read_bytes() + b"\n# distinct test release\n")
    second = install_release(
        source_root=second_source,
        state_home=state_home,
        data_home=data_home,
        python_executable=Path(sys.executable),
        temporary_root=temporary_root,
    )
    assert second.release_dir != first.release_dir
    assert (
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=second.entrypoint_path,
        )
        == second
    )
    with pytest.raises(IntegrationAttestationUnavailable):
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=first.entrypoint_path,
        )
    assert rollback_active_release(state_home=state_home, data_home=data_home) == first
    assert (
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=first.entrypoint_path,
        )
        == first
    )


def test_runtime_wheel_import_closure_is_exact_and_utc_precedes_python_import(tmp_path):
    release, _, _ = _install(tmp_path)
    from codex_usage import integration_installer

    allowed = {f"codex_usage/{name}" for name in integration_installer.SOURCE_MODULES}
    allowed_dist_info = {
        f"{integration_installer.DIST_INFO_PREFIX}/{name}"
        for name in integration_installer.DIST_INFO_FILES
    }
    with zipfile.ZipFile(release.release_dir / "producer.whl") as wheel:
        names = set(wheel.namelist())
        package_members = {name for name in names if name.startswith("codex_usage/")}
        assert package_members == allowed
        assert names - package_members == allowed_dist_info
        integration_installer._validate_runtime_import_closure(
            {name: wheel.read(name) for name in package_members}
        )
    launcher = release.launcher_path.read_text(encoding="utf-8")
    assert launcher.index("TZ=UTC") < launcher.index("exec ")


@pytest.mark.parametrize(
    "source",
    [
        "import codex_usage.integration_snapshot\n",
        "from codex_usage import integration_snapshot\n",
        "from codex_usage.integration_snapshot import build_schema1_payload\n",
        "from . import integration_snapshot\n",
        "from .integration_snapshot import build_schema1_payload\n",
    ],
)
def test_runtime_import_gate_accepts_each_declared_local_form(source):
    from codex_usage.integration_installer import _validate_runtime_import_closure

    _validate_runtime_import_closure(
        {"codex_usage/probe.py": source.encode("utf-8")},
        require_available=False,
    )


def test_runtime_import_gate_rejects_absolute_importfrom_installer():
    from codex_usage.integration_installer import (
        IntegrationInstallError,
        _validate_runtime_import_closure,
    )

    with pytest.raises(IntegrationInstallError):
        _validate_runtime_import_closure(
            {"codex_usage/probe.py": b"from codex_usage import integration_installer\n"},
            require_available=False,
        )


def _bootstrap_child(state_home_text, holder, holder_go, holder_locked, release, queue):
    from codex_usage.integration_installer import _bootstrap_integration_dir
    from codex_usage.private_io import private_path_lock

    state_home = Path(state_home_text)
    app_identity, integration_identity = _bootstrap_integration_dir(state_home)
    assert app_identity.permissions == integration_identity.permissions == 0o700
    integration = state_home / "codex-usage" / "integration"
    stat_result = integration.lstat()
    queue.put(
        ("booted", str(integration), stat_result.st_dev, stat_result.st_ino, stat_result.st_mode)
    )
    if holder:
        assert holder_go.wait(5)
        with private_path_lock(
            integration / "producer-install",
            timeout_seconds=0,
            label="integration producer lock",
        ):
            holder_locked.set()
            assert release.wait(5)
        queue.put(("holder-released",))
        return
    assert holder_locked.wait(5)
    try:
        with private_path_lock(
            integration / "producer-install",
            timeout_seconds=0,
            label="integration producer lock",
        ):
            queue.put(("unexpected-second-lock",))
    except TimeoutError:
        queue.put(("busy",))


def test_first_install_bootstrap_converges_then_uses_one_zero_time_lock(tmp_path):
    context = multiprocessing.get_context("spawn")
    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    holder_go, holder_locked, release = (context.Event() for _ in range(3))
    queue = context.Queue()
    holder = context.Process(
        target=_bootstrap_child,
        args=(str(state_home), True, holder_go, holder_locked, release, queue),
    )
    contender = context.Process(
        target=_bootstrap_child,
        args=(str(state_home), False, holder_go, holder_locked, release, queue),
    )
    holder.start()
    contender.start()
    first, second = queue.get(timeout=5), queue.get(timeout=5)
    assert first[0] == second[0] == "booted"
    assert first[1:] == second[1:]
    assert sorted(path.name for path in state_home.iterdir()) == ["codex-usage"]
    holder_go.set()
    assert holder_locked.wait(5)
    assert queue.get(timeout=5) == ("busy",)
    release.set()
    assert queue.get(timeout=5) == ("holder-released",)
    holder.join(5)
    contender.join(5)
    assert holder.exitcode == contender.exitcode == 0


def test_installer_script_has_narrow_parser_and_no_general_cli_import():
    spec = importlib.util.spec_from_file_location("synthetic_installer_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parser = module._parser()
    parsed = parser.parse_args(
        [
            "--source-root",
            "/tmp/source",
            "--state-home",
            "/tmp/state",
            "--data-home",
            "/tmp/data",
            "--python",
            "/usr/bin/python3",
            "--temporary-root",
            "/tmp/temporary",
        ]
    )
    assert parsed.source_root == "/tmp/source"
    assert parsed.rollback is False
    with pytest.raises(module._InstallerArgumentError):
        parser.parse_args(
            [
                "--rollback",
                "--state-home",
                "/tmp/state",
                "--data-home",
                "/tmp/data",
                "--python",
                "/tmp/python",
            ]
        )
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "codex_usage.cli" not in source


def test_candidate_manifest_is_single_final_only_write_with_real_treehash(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    candidate_writes: list[dict[str, object]] = []
    rename_snapshots: list[dict[str, object]] = []
    original_write = integration_installer._write_exclusive
    original_rename = Path.rename

    def capture_write(path, payload, **kwargs):
        if Path(path).name.startswith("candidate-"):
            candidate_writes.append(json.loads(payload))
        return original_write(path, payload, **kwargs)

    def capture_rename(source, target):
        if ".staging-" in source.name:
            candidates = sorted(tmp_path.rglob("candidate-*.json"))
            assert len(candidates) == 1
            rename_snapshots.append(json.loads(candidates[0].read_text(encoding="utf-8")))
        return original_rename(source, target)

    monkeypatch.setattr(integration_installer, "_write_exclusive", capture_write)
    monkeypatch.setattr(Path, "rename", capture_rename)
    release, _, _ = _install(tmp_path)

    assert len(candidate_writes) == 1
    assert len(rename_snapshots) == 1
    candidate = rename_snapshots[0]
    for key in ("release_dir", "launcher_path", "entrypoint_path", "wheel_path", "record_path"):
        assert str(release.release_dir) in candidate[key]
        assert ".staging-" not in candidate[key]
        assert "temporary" not in candidate[key]
    assert candidate["release_tree_sha256"] == release.release_tree_sha256


def test_preexisting_candidate_is_exclusive_and_untouched(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    original_manifest = integration_installer._manifest
    marker = b"preexisting-candidate-marker\n"
    candidate_path: Path | None = None

    def precreate_candidate(**kwargs):
        nonlocal candidate_path
        candidate = original_manifest(**kwargs)
        candidate_path = temporary_root / f"candidate-{candidate['release_id']}.json"
        candidate_path.write_bytes(marker)
        candidate_path.chmod(0o600)
        return candidate

    monkeypatch.setattr(integration_installer, "_manifest", precreate_candidate)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    assert candidate_path is not None
    assert candidate_path.read_bytes() == marker
    assert stat.S_IMODE(candidate_path.lstat().st_mode) == 0o600


@pytest.mark.parametrize("replaced_kind", ["build", "wheel", "staging"])
def test_cleanup_does_not_delete_replaced_owned_child(tmp_path, monkeypatch, replaced_kind):
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    marker_path: Path | None = None

    def replace_with_foreign_marker(path: Path) -> None:
        nonlocal marker_path
        shutil.rmtree(path)
        path.mkdir(mode=0o700)
        marker_path = path / "foreign-marker"
        marker_path.write_bytes(replaced_kind.encode("ascii"))

    if replaced_kind == "build":
        original_copy = integration_installer._copy_source_into_project

        def replace_build(source, build):
            result = original_copy(source, build)
            replace_with_foreign_marker(build)
            return result

        monkeypatch.setattr(
            integration_installer, "_copy_source_into_project", replace_build
        )
    elif replaced_kind == "wheel":
        original_build = integration_installer._build_verified_wheel

        def replace_wheel(*, wheel_dir, **kwargs):
            result = original_build(wheel_dir=wheel_dir, **kwargs)
            replace_with_foreign_marker(wheel_dir)
            return result

        monkeypatch.setattr(
            integration_installer, "_build_verified_wheel", replace_wheel
        )
    else:
        original_copy_regular = integration_installer._copy_regular

        def replace_staging(source, target, **kwargs):
            result = original_copy_regular(source, target, **kwargs)
            if target.name == "producer.whl":
                replace_with_foreign_marker(target.parent)
            return result

        monkeypatch.setattr(
            integration_installer, "_copy_regular", replace_staging
        )

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    assert marker_path is not None
    assert marker_path.read_bytes() == replaced_kind.encode("ascii")


def test_candidate_cleanup_oserror_prevents_success(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    original_unlink = Path.unlink

    def fail_candidate_unlink(path, *args, **kwargs):
        if path.name.startswith("candidate-"):
            raise OSError("synthetic candidate cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_candidate_unlink)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=_temporary_source_copy(tmp_path),
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )


def test_owned_child_cleanup_oserror_prevents_success(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    original_rmtree = integration_installer.shutil.rmtree

    def fail_build_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith("producer-build-"):
            raise OSError("synthetic build cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(integration_installer.shutil, "rmtree", fail_build_cleanup)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=_temporary_source_copy(tmp_path),
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )


def test_success_leaves_no_candidate_build_or_wheel_artifacts(tmp_path):
    _install(tmp_path)
    temporary_root = tmp_path / "temporary"
    assert not list(temporary_root.glob("candidate-*.json"))
    assert not list(temporary_root.glob("producer-build-*"))
    assert not list(temporary_root.glob("producer-wheel-*"))


def test_wheel_create_failure_cleans_already_owned_build(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    original_mkdir = Path.mkdir
    build_created = False

    def fail_wheel_mkdir(path, *args, **kwargs):
        nonlocal build_created
        if path.name.startswith("producer-build-"):
            result = original_mkdir(path, *args, **kwargs)
            build_created = True
            return result
        if path.name.startswith("producer-wheel-"):
            assert build_created
            raise OSError("synthetic wheel create failure")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_wheel_mkdir)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    assert not list(temporary_root.glob("producer-build-*"))
    assert not list(temporary_root.glob("producer-wheel-*"))


def test_preexisting_wheel_target_is_untouched_and_build_is_cleaned(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    token = "fixr3-preexisting-wheel"
    foreign_wheel = temporary_root / f"producer-wheel-{token}"
    foreign_wheel.mkdir(mode=0o700)
    marker = foreign_wheel / "foreign-wheel-marker"
    marker.write_bytes(b"foreign-wheel-bytes")
    before = marker.read_bytes()
    monkeypatch.setattr(integration_installer.secrets, "token_hex", lambda _: token)

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    assert marker.read_bytes() == before
    assert foreign_wheel.is_dir()
    assert not list(temporary_root.glob("producer-build-*"))


@pytest.mark.parametrize("kind", ["staging", "build", "wheel", "candidate"])
def test_post_create_chmod_failure_cleans_only_new_target(tmp_path, monkeypatch, kind):
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    original_chmod = Path.chmod
    original_fchmod = os.fchmod
    fired = False

    def is_target(path: Path) -> bool:
        if kind == "staging":
            return path.name.startswith(".") and ".staging-" in path.name
        return path.name.startswith(f"{kind if kind == 'candidate' else 'producer-' + kind}-")

    def fail_target_chmod(path, mode):
        nonlocal fired
        if not fired and is_target(path):
            fired = True
            raise OSError("synthetic post-create chmod failure")
        return original_chmod(path, mode)

    def fail_target_fchmod(fd, mode):
        nonlocal fired
        try:
            path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            path = None
        if (
            kind == "candidate"
            and not fired
            and path is not None
            and is_target(path)
        ):
            fired = True
            raise OSError("synthetic post-create chmod failure")
        return original_fchmod(fd, mode)

    monkeypatch.setattr(Path, "chmod", fail_target_chmod)
    monkeypatch.setattr(os, "fchmod", fail_target_fchmod)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )

    assert fired
    assert not list(temporary_root.glob("candidate-*.json"))
    assert not list(temporary_root.glob("producer-build-*"))
    assert not list(temporary_root.glob("producer-wheel-*"))
    assert not list(
        (state_home / "codex-usage" / "integration" / "releases").glob(".*.staging-*")
    )


def test_cleanup_does_not_delete_replaced_candidate_inode(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    original_cleanup = integration_installer._cleanup_owned_file
    replacement: Path | None = None

    def replace_candidate(path, identity, parent_identity):
        nonlocal replacement
        if path.name.startswith("candidate-"):
            path.unlink()
            path.write_bytes(b"foreign-candidate-marker")
            path.chmod(0o600)
            replacement = path
        return original_cleanup(path, identity, parent_identity)

    monkeypatch.setattr(integration_installer, "_cleanup_owned_file", replace_candidate)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=_temporary_source_copy(tmp_path),
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )

    assert replacement is not None
    assert replacement.read_bytes() == b"foreign-candidate-marker"
    assert stat.S_IMODE(replacement.lstat().st_mode) == 0o600


def test_exclusive_write_cleans_candidate_when_parent_revalidation_fails_after_open(
    tmp_path,
    monkeypatch,
):
    from codex_usage import integration_installer

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    candidate = parent / "candidate.json"
    original_identity = integration_installer._directory_identity
    original_open = integration_installer.os.open
    opened = False
    parent_revalidation_failed = False

    def mark_open(*args, **kwargs):
        nonlocal opened
        fd = original_open(*args, **kwargs)
        opened = True
        return fd

    def fail_once_after_open(path):
        nonlocal parent_revalidation_failed
        if path == parent and opened and not parent_revalidation_failed:
            parent_revalidation_failed = True
            raise OSError("synthetic parent revalidation failure")
        return original_identity(path)

    monkeypatch.setattr(integration_installer.os, "open", mark_open)
    monkeypatch.setattr(integration_installer, "_directory_identity", fail_once_after_open)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._write_exclusive(
            candidate,
            b"candidate-payload",
            mode=0o600,
        )

    assert opened
    assert parent_revalidation_failed
    assert not candidate.exists()


def test_exclusive_write_keeps_replaced_parent_and_candidate_marker(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    candidate = parent / "candidate.json"
    old_parent = tmp_path / "parent-old"
    marker = b"foreign-parent-marker"
    original_identity = integration_installer._directory_identity
    original_open = integration_installer.os.open
    original_write = integration_installer.os.write
    opened = False
    parent_replaced = False
    write_called = False

    def mark_open(*args, **kwargs):
        nonlocal opened
        fd = original_open(*args, **kwargs)
        opened = True
        return fd

    def mark_write(*args, **kwargs):
        nonlocal write_called
        write_called = True
        return original_write(*args, **kwargs)

    def replace_parent_after_open(path):
        nonlocal parent_replaced
        if path == parent and opened and not parent_replaced:
            parent_replaced = True
            parent.rename(old_parent)
            parent.mkdir(mode=0o700)
            parent.chmod(0o700)
            (parent / "foreign-marker").write_bytes(marker)
        return original_identity(path)

    monkeypatch.setattr(integration_installer.os, "open", mark_open)
    monkeypatch.setattr(integration_installer.os, "write", mark_write)
    monkeypatch.setattr(integration_installer, "_directory_identity", replace_parent_after_open)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._write_exclusive(
            candidate,
            b"candidate-payload",
            mode=0o600,
        )

    assert opened
    assert parent_replaced
    assert not write_called
    assert (parent / "foreign-marker").read_bytes() == marker
    assert (old_parent / candidate.name).read_bytes() == b""


def test_exclusive_write_keeps_replaced_candidate_inode_marker(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    candidate = parent / "candidate.json"
    marker = b"foreign-candidate-marker"
    original_identity = integration_installer._directory_identity
    original_open = integration_installer.os.open
    original_write = integration_installer.os.write
    opened = False
    candidate_replaced = False
    write_called = False

    def mark_open(*args, **kwargs):
        nonlocal opened
        fd = original_open(*args, **kwargs)
        opened = True
        return fd

    def mark_write(*args, **kwargs):
        nonlocal write_called
        write_called = True
        return original_write(*args, **kwargs)

    def replace_candidate_after_open(path):
        nonlocal candidate_replaced
        if path == parent and opened and not candidate_replaced:
            candidate_replaced = True
            candidate.unlink()
            candidate.write_bytes(marker)
            candidate.chmod(0o600)
        return original_identity(path)

    monkeypatch.setattr(integration_installer.os, "open", mark_open)
    monkeypatch.setattr(integration_installer.os, "write", mark_write)
    monkeypatch.setattr(integration_installer, "_directory_identity", replace_candidate_after_open)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._write_exclusive(
            candidate,
            b"candidate-payload",
            mode=0o600,
        )

    assert opened
    assert candidate_replaced
    assert not write_called
    assert candidate.read_bytes() == marker
    assert stat.S_IMODE(candidate.lstat().st_mode) == 0o600


def test_candidate_call_binds_saved_temporary_identity(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    expected_parent = integration_installer._identity(temporary_root)
    original_write = integration_installer._write_exclusive
    candidate_parents = []

    def capture_candidate_parent(path, payload, **kwargs):
        if path.name.startswith("candidate-"):
            candidate_parents.append(kwargs.get("parent_identity"))
        return original_write(path, payload, **kwargs)

    monkeypatch.setattr(integration_installer, "_write_exclusive", capture_candidate_parent)
    integration_installer.install_release(
        source_root=source_root,
        state_home=state_home,
        data_home=data_home,
        python_executable=Path(sys.executable),
        temporary_root=temporary_root,
    )

    assert candidate_parents == [expected_parent]


def test_candidate_manifest_is_checked_before_rename(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    rename_calls: list[tuple[Path, Path]] = []
    original_rename = Path.rename
    original_read_manifest = integration_installer._read_manifest

    def tamper_before_seam(candidate_path):
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        candidate["release_dir"] = str(candidate_path.parent)
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        return original_read_manifest(candidate_path)

    def capture_rename(source, target):
        rename_calls.append((source, target))
        return original_rename(source, target)

    monkeypatch.setattr(integration_installer, "_read_manifest", tamper_before_seam)
    monkeypatch.setattr(Path, "rename", capture_rename)
    with pytest.raises(integration_installer.IntegrationInstallError):
        _install(tmp_path)
    assert not rename_calls


def _assert_identity_before_events(events: list[str]) -> None:
    assert events
    for index, event in enumerate(events):
        if event in {"write", "attest"}:
            assert index > 0 and events[index - 1] == "revalidate"


def test_install_revalidates_bootstrap_before_every_write_and_attestation(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    events: list[str] = []
    original_revalidate = integration_installer._revalidate_bootstrap
    original_write = integration_installer.write_private_text
    original_attest = integration_installer._verify_manifest

    def revalidate(*args, **kwargs):
        events.append("revalidate")
        return original_revalidate(*args, **kwargs)

    def write(path, text, **kwargs):
        events.append("write")
        return original_write(path, text, **kwargs)

    def attest(*args, **kwargs):
        events.append("attest")
        return original_attest(*args, **kwargs)

    monkeypatch.setattr(integration_installer, "_revalidate_bootstrap", revalidate)
    monkeypatch.setattr(integration_installer, "write_private_text", write)
    monkeypatch.setattr(integration_installer, "_verify_manifest", attest)
    _install(tmp_path)
    _assert_identity_before_events(events)


def test_rollback_revalidates_bootstrap_before_write_and_final_attestation(tmp_path, monkeypatch):
    from codex_usage import integration_installer
    from codex_usage.private_io import write_private_text

    first, _, state_home = _install(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    write_private_text(
        integration / "previous.json",
        active_path.read_text(encoding="utf-8"),
        label="synthetic previous manifest",
        mode=0o600,
    )
    write_private_text(
        active_path,
        '{"schema_version":1,"version":"broken"}',
        label="synthetic broken active manifest",
        mode=0o600,
    )
    events: list[str] = []
    original_revalidate = integration_installer._revalidate_bootstrap
    original_write = integration_installer.write_private_text
    original_attest = integration_installer._verify_manifest

    def revalidate(*args, **kwargs):
        events.append("revalidate")
        return original_revalidate(*args, **kwargs)

    def write(path, text, **kwargs):
        events.append("write")
        return original_write(path, text, **kwargs)

    def attest(*args, **kwargs):
        events.append("attest")
        return original_attest(*args, **kwargs)

    monkeypatch.setattr(integration_installer, "_revalidate_bootstrap", revalidate)
    monkeypatch.setattr(integration_installer, "write_private_text", write)
    monkeypatch.setattr(integration_installer, "_verify_manifest", attest)
    assert integration_installer.rollback_active_release(
        state_home=state_home,
        data_home=tmp_path / "data",
    ) == first
    _assert_identity_before_events(events)


def test_temporary_source_copy_rejects_descendant_symlink_escape(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    (project / "pyproject.toml").write_text("synthetic", encoding="utf-8")
    for relative_text in TEST_SOURCE_MANIFEST_FILES:
        if relative_text == "pyproject.toml":
            continue
        target = outside / relative_text
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(relative_text.encode("utf-8"))
    (project / "src").symlink_to(outside / "src", target_is_directory=True)
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    monkeypatch.setattr(integration_installer, "PROJECT_ROOT", project)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._temporary_source_copy(output)


def test_interpreter_resolver_accepts_final_symlink_and_rejects_bad_targets(tmp_path):
    from codex_usage import integration_installer

    target = tmp_path / "python-target"
    target.write_bytes(b"synthetic executable")
    target.chmod(0o700)
    link = tmp_path / "python"
    link.symlink_to(target)
    assert integration_installer._resolve_python_executable(link) == target

    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing")
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._resolve_python_executable(dangling)

    directory = tmp_path / "directory"
    directory.mkdir(mode=0o700)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._resolve_python_executable(directory)


def test_install_revalidates_temporary_root_and_child_identities(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    _, _, temporary_root = _roots(tmp_path)
    seen: list[tuple[Path, object]] = []
    original_require = integration_installer._require_private_dir

    def require(path, expected, create):
        path = Path(path)
        if path == temporary_root or path.parent == temporary_root or ".staging-" in path.name:
            seen.append((path, expected))
        return original_require(path, expected, create)

    monkeypatch.setattr(integration_installer, "_require_private_dir", require)
    integration_installer.install_release(
        source_root=_temporary_source_copy(tmp_path),
        state_home=tmp_path / "state",
        data_home=tmp_path / "data",
        python_executable=Path(sys.executable),
        temporary_root=temporary_root,
    )
    assert any(path == temporary_root and expected is not None for path, expected in seen)
    assert any(path.parent == temporary_root and expected is not None for path, expected in seen)
    assert any(".staging-" in path.name and expected is not None for path, expected in seen)


def test_record_must_bind_nonempty_entrypoint_row(tmp_path):
    from codex_usage import integration_attestation
    from codex_usage.integration_attestation import (
        IntegrationAttestationUnavailable,
        verify_active_release,
    )
    from codex_usage.private_io import write_private_text

    release, data_home, state_home = _install(tmp_path)
    active_path = state_home / "codex-usage" / "integration" / "active.json"
    manifest = json.loads(active_path.read_text(encoding="utf-8"))
    record_path = Path(manifest["record_path"])
    record_path.write_text(
        "".join(
            line
            for line in record_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.startswith("codex_usage/integration_entrypoint.py,")
        ),
        encoding="utf-8",
    )
    manifest["record_sha256"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
    manifest["release_tree_sha256"] = integration_attestation._release_tree_sha256(
        release_dir=release.release_dir
    )
    write_private_text(
        active_path,
        json.dumps(manifest, sort_keys=True),
        label="mutated active manifest",
        mode=0o600,
    )
    with pytest.raises(IntegrationAttestationUnavailable):
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.entrypoint_path,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("release_id", "0.6.532-ffffffffffffffff"),
        ("source_manifest_sha256", "f" * 64),
    ],
)
def test_manifest_release_id_and_source_digest_bind_final_path(tmp_path, field, value):
    from codex_usage.integration_attestation import (
        IntegrationAttestationUnavailable,
        verify_active_release,
    )
    from codex_usage.private_io import write_private_text

    release, data_home, state_home = _install(tmp_path)
    active_path = state_home / "codex-usage" / "integration" / "active.json"
    manifest = json.loads(active_path.read_text(encoding="utf-8"))
    manifest[field] = value
    write_private_text(
        active_path,
        json.dumps(manifest, sort_keys=True),
        label="mutated active manifest",
        mode=0o600,
    )
    with pytest.raises(IntegrationAttestationUnavailable):
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.entrypoint_path,
        )


def test_builder_rejects_wrong_wheel_basename_before_release_use(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    build_root = tmp_path / "build"
    wheel_dir = tmp_path / "wheel"
    build_root.mkdir(mode=0o700)
    wheel_dir.mkdir(mode=0o700)

    monkeypatch.setattr(integration_installer, "_require_offline_builder", lambda **_: None)

    def fake_builder(command, *, env, cwd):
        (wheel_dir / "wrong-name-0.6.532-py3-none-any.whl").write_bytes(b"wheel")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(integration_installer, "_run_builder_bounded", fake_builder)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._build_verified_wheel(
            python_executable=Path(sys.executable),
            environment=integration_installer._sanitized_build_environment(),
            build_root=build_root,
            wheel_dir=wheel_dir,
        )


def test_builder_rejects_wheel_directory_entry_limit(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    build_root = tmp_path / "build"
    wheel_dir = tmp_path / "wheel"
    build_root.mkdir(mode=0o700)
    wheel_dir.mkdir(mode=0o700)
    (wheel_dir / integration_installer.EXPECTED_WHEEL_NAME).write_bytes(b"wheel")
    (wheel_dir / "unrelated-output").write_bytes(b"output")
    monkeypatch.setattr(integration_installer, "MAX_RELEASE_TREE_ENTRIES", 1)
    monkeypatch.setattr(integration_installer, "_require_offline_builder", lambda **_: None)
    monkeypatch.setattr(
        integration_installer,
        "_run_builder_bounded",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0),
    )

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._build_verified_wheel(
            python_executable=Path(sys.executable),
            environment=integration_installer._sanitized_build_environment(),
            build_root=build_root,
            wheel_dir=wheel_dir,
        )


def test_launcher_drops_marker_environment_before_runtime(tmp_path):
    from codex_usage.integration_installer import install_release

    source_root_parent = tmp_path / "source-parent"
    source_root_parent.mkdir(mode=0o700)
    source = _temporary_source_copy(source_root_parent)
    entrypoint = source / "src/codex_usage/integration_entrypoint.py"
    source_text = entrypoint.read_text(encoding="utf-8")
    source_text = source_text.replace(
        "def main(argv: Sequence[str] | None = None) -> int:\n",
        "def main(argv: Sequence[str] | None = None) -> int:\n"
        "    if os.environ.get(\"CODEX_USAGE_MARKER\") == \"secret-marker\":\n"
        "        raise SystemExit(91)\n",
    )
    entrypoint.write_text(source_text, encoding="utf-8")
    data_home, state_home, temporary_root = _roots(tmp_path)
    release = install_release(
        source_root=source,
        state_home=state_home,
        data_home=data_home,
        python_executable=Path(sys.executable),
        temporary_root=temporary_root,
    )
    _write_launcher_state(data_home)
    completed = subprocess.run(
        [
            str(release.launcher_path),
            "integration-snapshot",
            "--schema",
            "1",
            "--format",
            "json",
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "CODEX_USAGE_MARKER": "secret-marker",
            "PYTHONPATH": str(tmp_path),
            "OPENAI_API_KEY": "secret-marker",
            "HTTP_PROXY": "http://secret.invalid",
        },
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["schema_version"] == 1
    assert completed.stderr == ""


def test_installer_parser_errors_are_data_sparse(capsys):
    spec = importlib.util.spec_from_file_location("synthetic_installer_parser", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    result = module.main(["--unknown", "secret-marker"])
    captured = capsys.readouterr()
    assert result == 64
    assert captured.out == ""
    assert captured.err == "integration_producer_unavailable\n"
    assert "secret-marker" not in captured.err


def test_installer_cleanup_errors_have_distinct_data_sparse_result(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("synthetic_installer_cleanup", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def fail_install(**_kwargs):
        raise module.IntegrationCleanupError()

    monkeypatch.setattr(module, "install_release", fail_install)
    result = module.main(
        [
            "--source-root",
            str(tmp_path / "source"),
            "--state-home",
            str(tmp_path / "state"),
            "--data-home",
            str(tmp_path / "data"),
            "--python",
            str(tmp_path / "python"),
            "--temporary-root",
            str(tmp_path / "temporary"),
        ]
    )
    captured = capsys.readouterr()
    assert result == 70
    assert captured.out == ""
    assert captured.err == "integration_producer_cleanup_failed\n"
