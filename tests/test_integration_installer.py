from __future__ import annotations

import ast
import base64
import contextlib
import errno
import hashlib
import importlib.util
import json
import marshal
import math
import multiprocessing
import os
import py_compile
import queue
import re
import runpy
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
import tomllib
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "install_integration_producer.py"
BOOTSTRAP_PROCESS_TIMEOUT_SECONDS = 30

TEST_TRUSTED_CORE_MODULE_FILES = (
    "__init__.py",
    "account_lock.py",
    "config.py",
    "consumption.py",
    "extractor.py",
    "integration_attestation.py",
    "integration_evidence.py",
    "integration_entrypoint.py",
    "integration_pool_authority.py",
    "integration_snapshot.py",
    "integration_timeout_contract.py",
    "integration_watchdog.py",
    "json_utils.py",
    "models.py",
    "history.py",
    "pool_authority_owner.py",
    "private_io.py",
    "source_lock.py",
    "state_maintenance.py",
    "state.py",
    "usage_limits.py",
    "usage_resets.py",
)
TEST_PRODUCER_RELEASE_MODULE_FILES = (
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
    "src/codex_usage/pool_authority_owner.py",
    "src/codex_usage/private_io.py",
    "src/codex_usage/source_lock.py",
    "src/codex_usage/state_maintenance.py",
    "src/codex_usage/state.py",
    "src/codex_usage/usage_limits.py",
    "src/codex_usage/usage_resets.py",
)
TEST_SOURCE_MANIFEST_FILES = (
    "pyproject.toml",
    *TEST_PRODUCER_RELEASE_MODULE_FILES,
)


def test_installer_source_manifest_uses_producer_boundary_without_controller_modules():
    """Would fail if controller-only modules leaked into the Producer source set."""
    from codex_usage import integration_attestation, integration_installer

    assert set(integration_installer.SOURCE_MANIFEST_FILES) == set(
        TEST_SOURCE_MANIFEST_FILES
    )
    assert tuple(integration_attestation.TRUSTED_CORE_MODULES) == (
        TEST_TRUSTED_CORE_MODULE_FILES
    )
    assert tuple(integration_attestation.PRODUCER_RELEASE_MODULES) == tuple(
        path.removeprefix("src/codex_usage/")
        for path in TEST_PRODUCER_RELEASE_MODULE_FILES
    )
    assert "integration_watchdog.py" not in integration_attestation.PRODUCER_RELEASE_MODULES
    assert (
        "integration_timeout_contract.py"
        not in integration_attestation.PRODUCER_RELEASE_MODULES
    )


class _BrokenInt(int):
    def __gt__(self, _other):
        raise RuntimeError("synthetic installer integer comparison marker")

    def __le__(self, _other):
        raise RuntimeError("synthetic installer integer comparison marker")


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


def _set_fixture_source_release_version(source_root: Path, version: str) -> None:
    """Give a historical installer fixture matching distribution metadata bytes.

    The installer deliberately rejects a source tree whose distribution metadata
    disagrees with the release identity being exercised.  Historical tests must
    therefore change these two authoritative version declarations together;
    they must not weaken the production source-version check.
    """
    assert re.fullmatch(r"0\.6\.\d+", version)
    for relative in ("pyproject.toml", "src/codex_usage/__init__.py"):
        path = source_root / relative
        original = path.read_text(encoding="utf-8")
        rewritten, substitutions = re.subn(r"0\.6\.\d+", version, original)
        assert substitutions == 1
        path.write_text(rewritten, encoding="utf-8")
        path.chmod(0o600)


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


@contextlib.contextmanager
def _loaded_public_prepare_namespace(repo_root: Path):
    """Load only a fixture's public preparer and restore import process state."""
    original_path = tuple(sys.path)
    original_meta_path = tuple(sys.meta_path)
    original_modules = dict(sys.modules)
    try:
        for name in tuple(sys.modules):
            if name == "codex_usage" or name.startswith("codex_usage."):
                del sys.modules[name]
        yield runpy.run_path(
            str(repo_root / "scripts" / SCRIPT_PATH.name),
            run_name="_codex_usage_prepare_source_test_",
        )
    finally:
        sys.path[:] = original_path
        sys.meta_path[:] = original_meta_path
        for name in tuple(sys.modules):
            if name not in original_modules:
                del sys.modules[name]
        sys.modules.update(original_modules)


def _prepare_source_tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    """Bind a hostile-rebind fixture's namespace, identity, modes, and bytes."""
    rows: list[tuple[object, ...]] = []

    def visit(path: Path, relative: str) -> None:
        item = path.lstat()
        base = (
            relative,
            stat.S_IFMT(item.st_mode),
            item.st_dev,
            item.st_ino,
            stat.S_IMODE(item.st_mode),
            item.st_nlink,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if stat.S_ISDIR(item.st_mode):
            rows.append((*base, None, None))
            for child in sorted(path.iterdir(), key=lambda candidate: candidate.name):
                visit(child, f"{relative}/{child.name}")
        elif stat.S_ISREG(item.st_mode):
            rows.append((*base, path.read_bytes(), None))
        elif stat.S_ISLNK(item.st_mode):
            rows.append((*base, None, str(path.readlink())))
        else:
            rows.append((*base, None, None))

    visit(root, ".")
    return tuple(rows)


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    data_home = tmp_path / "data"
    state_home = tmp_path / "state"
    temporary_root = tmp_path / "temporary"
    for path in (data_home, state_home, temporary_root):
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    return data_home, state_home, temporary_root


def _write_synthetic_schema1_active(*, state_home: Path, data_home: Path) -> bytes:
    from codex_usage.integration_attestation import _release_tree_sha256
    from codex_usage.private_io import write_private_text

    integration = state_home / "codex-usage" / "integration"
    releases = integration / "releases"
    source_digest = "1" * 64
    release_id = f"0.6.532-{source_digest[:16]}"
    release_dir = releases / release_id
    site_packages = release_dir / "venv/lib/python3.11/site-packages"
    dist_info = site_packages / "codex_usage_integration_producer-0.6.532.dist-info"
    package = site_packages / "codex_usage"
    launcher_path = release_dir / "venv/bin/codex-usage"
    entrypoint_path = package / "integration_entrypoint.py"
    wheel_path = release_dir / "producer.whl"
    record_path = dist_info / "RECORD"
    metadata_path = dist_info / "METADATA"
    for directory in (
        state_home / "codex-usage",
        integration,
        releases,
        release_dir,
        release_dir / "venv",
        release_dir / "venv/bin",
        release_dir / "venv/lib",
        release_dir / "venv/lib/python3.11",
        site_packages,
        package,
        dist_info,
    ):
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)

    entrypoint_payload = b"# synthetic schema-1 entrypoint\n"
    wheel_payload = b"synthetic schema-1 wheel\n"
    launcher_payload = (
        b"#!/bin/sh\nexec /nonexistent/python -B -I -m "
        b"codex_usage.integration_entrypoint \"$@\"\n"
    )
    metadata_payload = (
        b"Metadata-Version: 2.4\n"
        b"Name: codex-usage-integration-producer\n"
        b"Version: 0.6.532\n"
    )

    def record_digest(payload: bytes) -> str:
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
        return "sha256=" + digest.decode("ascii").rstrip("=")

    metadata_relative = metadata_path.relative_to(site_packages).as_posix()
    entrypoint_relative = entrypoint_path.relative_to(site_packages).as_posix()
    record_relative = record_path.relative_to(site_packages).as_posix()
    record_payload = (
        f"{entrypoint_relative},{record_digest(entrypoint_payload)},{len(entrypoint_payload)}\n"
        f"{metadata_relative},{record_digest(metadata_payload)},{len(metadata_payload)}\n"
        f"{record_relative},,\n"
    ).encode()
    for path, payload, mode in (
        (entrypoint_path, entrypoint_payload, 0o600),
        (wheel_path, wheel_payload, 0o600),
        (launcher_path, launcher_payload, 0o700),
        (metadata_path, metadata_payload, 0o600),
        (record_path, record_payload, 0o600),
    ):
        path.write_bytes(payload)
        path.chmod(mode)

    manifest = {
        "schema_version": 1,
        "version": "0.6.532",
        "release_id": release_id,
        "source_manifest_sha256": source_digest,
        "state_home": str(state_home),
        "data_home": str(data_home),
        "release_dir": str(release_dir),
        "launcher_path": str(launcher_path),
        "entrypoint_path": str(entrypoint_path),
        "wheel_path": str(wheel_path),
        "record_path": str(record_path),
        "entrypoint_sha256": hashlib.sha256(entrypoint_payload).hexdigest(),
        "wheel_sha256": hashlib.sha256(wheel_payload).hexdigest(),
        "record_sha256": hashlib.sha256(record_payload).hexdigest(),
        "launcher_sha256": hashlib.sha256(launcher_payload).hexdigest(),
        "release_tree_sha256": _release_tree_sha256(release_dir=release_dir),
    }
    active_text = json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    write_private_text(
        integration / "active.json",
        active_text,
        label="synthetic schema-1 active manifest",
        mode=0o600,
    )
    return active_text.encode("utf-8")


def _mutate_manifest_fields(
    manifest: dict[str, object],
    mutation: str,
) -> dict[str, object]:
    mutated = dict(manifest)
    if mutation == "unknown":
        mutated["future_extension"] = "bounded"
    elif mutation == "secret-like":
        mutated["access_token"] = "synthetic-only"
    elif mutation == "missing":
        mutated.pop("launcher_sha256")
    else:  # pragma: no cover - parametrization is closed
        raise AssertionError(mutation)
    return mutated


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


def test_create_private_directory_secures_mode_without_path_chmod(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    target = parent / "target"
    parent_identity = integration_installer._directory_identity(parent)
    original_chmod = Path.chmod

    def reject_target_chmod(path, mode):
        if path == target:
            pytest.fail("private directory requires directory-FD mode changes")
        return original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", reject_target_chmod)
    identity = integration_installer._create_private_directory(target, parent_identity)

    assert identity == integration_installer._directory_identity(target)
    assert stat.S_IMODE(target.lstat().st_mode) == 0o700
    assert not target.is_symlink()


def test_create_private_directory_binds_creation_to_parent_descriptor(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    target = parent / "target"
    old_parent = tmp_path / "parent-old"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    parent_identity = integration_installer._directory_identity(parent)
    original_mkdir = os.mkdir
    swapped = False

    def swap_before_mkdir(candidate, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if candidate == "target" and dir_fd is not None and not swapped:
            parent.rename(old_parent)
            outside.rename(parent)
            swapped = True
        if dir_fd is None:
            return original_mkdir(candidate, mode)
        return original_mkdir(candidate, mode, dir_fd=dir_fd)

    monkeypatch.setattr(integration_installer.os, "mkdir", swap_before_mkdir)
    identity = integration_installer._create_private_directory(target, parent_identity)

    assert swapped
    assert not target.exists()
    assert (old_parent / "target").is_dir()
    assert identity == integration_installer._directory_identity(old_parent / "target")


def test_require_private_dir_binds_creation_to_parent_descriptor(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    target = parent / "target"
    old_parent = tmp_path / "parent-old"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    parent_identity = integration_installer._directory_identity(parent)
    original_mkdir = os.mkdir
    swapped = False

    def swap_before_mkdir(candidate, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if candidate == "target" and dir_fd is not None and not swapped:
            parent.rename(old_parent)
            outside.rename(parent)
            swapped = True
        if dir_fd is None:
            return original_mkdir(candidate, mode)
        return original_mkdir(candidate, mode, dir_fd=dir_fd)

    monkeypatch.setattr(integration_installer.os, "mkdir", swap_before_mkdir)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._require_private_dir(
            target,
            None,
            True,
            parent_identity=parent_identity,
        )

    assert swapped
    assert not (parent / "target").exists()
    assert (old_parent / "target").is_dir()


def test_require_private_dir_rejects_same_name_target_after_parent_swap(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    target = parent / "target"
    old_parent = tmp_path / "parent-old"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (outside / "target").mkdir(mode=0o700)
    parent_identity = integration_installer._directory_identity(parent)
    original_mkdir = os.mkdir
    swapped = False

    def swap_before_mkdir(candidate, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if candidate == "target" and dir_fd is not None and not swapped:
            parent.rename(old_parent)
            outside.rename(parent)
            swapped = True
        if dir_fd is None:
            return original_mkdir(candidate, mode)
        return original_mkdir(candidate, mode, dir_fd=dir_fd)

    monkeypatch.setattr(integration_installer.os, "mkdir", swap_before_mkdir)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._require_private_dir(
            target,
            None,
            True,
            parent_identity=parent_identity,
        )

    assert swapped
    assert (parent / "target").is_dir()
    assert (old_parent / "target").is_dir()


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


def _patch_release_identity(
    monkeypatch,
    version: str,
    *,
    source_root: Path | None = None,
) -> None:
    from codex_usage import integration_attestation, integration_installer

    current_version = integration_installer.RELEASE_VERSION
    if source_root is not None:
        _set_fixture_source_release_version(source_root, version)
    generated_pyproject = integration_installer._GENERATED_PYPROJECT.replace(
        f'version = "{current_version}"',
        f'version = "{version}"',
    )
    monkeypatch.setattr(integration_installer, "RELEASE_VERSION", version)
    monkeypatch.setattr(
        integration_installer,
        "DIST_INFO_PREFIX",
        f"codex_usage_integration_producer-{version}.dist-info",
    )
    monkeypatch.setattr(
        integration_installer,
        "EXPECTED_WHEEL_NAME",
        f"codex_usage_integration_producer-{version}-py3-none-any.whl",
    )
    monkeypatch.setattr(
        integration_installer,
        "_WHEEL_BUILDER_CODE",
        integration_installer._WHEEL_BUILDER_CODE.replace(
            f"codex_usage_integration_producer-{current_version}-py3-none-any.whl",
            f"codex_usage_integration_producer-{version}-py3-none-any.whl",
        ),
    )
    monkeypatch.setattr(
        integration_installer,
        "_GENERATED_PYPROJECT",
        generated_pyproject,
    )
    monkeypatch.setattr(integration_attestation, "_EXPECTED_VERSION", version)
    monkeypatch.setattr(
        integration_attestation,
        "_DIST_INFO_PREFIX",
        f"codex_usage_integration_producer-{version}.dist-info",
    )


def captured_builder_environment() -> dict[str, str]:
    from codex_usage.integration_installer import _sanitized_build_environment

    return _sanitized_build_environment()


def captured_python_argv(tmp_path: Path) -> tuple[str, ...]:
    from codex_usage import integration_installer

    capture = tmp_path / "captured-python-argv"
    executable = tmp_path / "capture-python"
    executable.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$CAPTURE_PATH\"\n"
        "printf '%s\\n' "
        "'{\"backend\":\"setuptools.command.bdist_wheel.bdist_wheel\","
        "\"setuptools\":\"77.0\"}'\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    environment = integration_installer._sanitized_build_environment()
    environment["CAPTURE_PATH"] = str(capture)
    result = integration_installer._run_builder_preflight(
        python_executable=executable,
        environment=environment,
    )
    assert result.returncode == 0
    return tuple(capture.read_text(encoding="utf-8").splitlines())


def install_verified_06540_source(tmp_path: Path):
    from codex_usage import integration_attestation, integration_installer

    tmp_path.mkdir(mode=0o700)
    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    with pytest.MonkeyPatch.context() as context:
        _patch_release_identity(
            context,
            integration_attestation._PREVIOUS_SCHEMA2_VERSION,
            source_root=source_root,
        )
        previous = integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    assert previous.version == integration_attestation._PREVIOUS_SCHEMA2_VERSION
    _set_fixture_source_release_version(source_root, "0.6.542")
    return integration_installer.install_release(
        source_root=source_root,
        state_home=state_home,
        data_home=data_home,
        python_executable=Path(sys.executable),
        temporary_root=temporary_root,
    )


def verify_compromised_06535_runtime(tmp_path: Path):
    from codex_usage import integration_attestation, integration_installer

    tmp_path.mkdir(mode=0o700)
    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    with pytest.MonkeyPatch.context() as context:
        _patch_release_identity(context, "0.6.535", source_root=source_root)
        from codex_usage import integration_attestation

        context.setattr(
            integration_attestation,
            "_PREVIOUS_SCHEMA2_VERSION",
            "0.6.534",
        )
        context.setattr(
            integration_attestation,
            "_PREVIOUS_SCHEMA2_DIST_INFO_PREFIX",
            "codex_usage_integration_producer-0.6.534.dist-info",
        )
        compromised = integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    pycache = compromised.release_dir / "venv/lib/__pycache__"
    pycache.mkdir(mode=0o700)
    (pycache / "compromised.pyc").write_bytes(b"compromised")
    return integration_attestation.verify_active_release(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=compromised.entrypoint_path,
    )


def verified_lock_targets(state_home: Path) -> set[str]:
    from codex_usage import integration_evidence, private_io

    integration = state_home / "codex-usage" / "integration"
    lock_root = private_io._private_lock_root()
    root_item = lock_root.lstat()
    assert stat.S_ISDIR(root_item.st_mode)
    assert root_item.st_uid == os.getuid()
    assert stat.S_IMODE(root_item.st_mode) == 0o700
    verified: set[str] = set()
    for logical_name in (
        "producer-install",
        "current.json",
        integration_evidence.POOL_AUTHORITY_SOURCE_FILENAME,
    ):
        lock_name = integration_evidence._evidence_lock_name(integration / logical_name)
        item = (lock_root / lock_name).lstat()
        assert stat.S_ISREG(item.st_mode)
        assert item.st_uid == os.getuid()
        assert stat.S_IMODE(item.st_mode) == 0o600
        assert item.st_nlink == 1
        assert 0 <= item.st_size <= 4096
        verified.add(logical_name)
    return verified


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


def test_copy_source_project_removes_pyproject_without_path_unlink(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    source_root = _temporary_source_copy(tmp_path)
    build_root = tmp_path / "build"
    build_root.mkdir(mode=0o700)
    build_identity = integration_installer._directory_identity(build_root)
    original_unlink = Path.unlink

    def reject_pyproject_unlink(path, *args, **kwargs):
        if path == build_root / "pyproject.toml":
            pytest.fail("generated pyproject cleanup requires parent-FD unlink")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", reject_pyproject_unlink)
    integration_installer._copy_source_into_project(
        source_root,
        build_root,
        build_identity=build_identity,
    )

    assert (build_root / "pyproject.toml").read_text(encoding="utf-8") == (
        integration_installer._GENERATED_PYPROJECT
    )


def test_copy_source_project_rejects_build_replacement_before_pyproject_cleanup(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    source_root = _temporary_source_copy(tmp_path)
    build_root = tmp_path / "build"
    build_root.mkdir(mode=0o700)
    build_identity = integration_installer._directory_identity(build_root)
    old_build = tmp_path / "build-old"
    original_copy = integration_installer._copy_regular
    replaced = False

    def replace_after_manifest(source, target, **kwargs):
        nonlocal replaced
        result = original_copy(source, target, **kwargs)
        if target == build_root / Path(integration_installer.SOURCE_MANIFEST_FILES[-1]):
            build_root.rename(old_build)
            build_root.mkdir(mode=0o700)
            foreign = build_root / "pyproject.toml"
            foreign.write_bytes(b"foreign")
            foreign.chmod(0o600)
            replaced = True
        return result

    monkeypatch.setattr(integration_installer, "_copy_regular", replace_after_manifest)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._copy_source_into_project(
            source_root,
            build_root,
            build_identity=build_identity,
        )

    assert replaced
    assert (build_root / "pyproject.toml").read_bytes() == b"foreign"
    assert (old_build / "pyproject.toml").exists()


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


def test_remove_activation_files_rejects_bin_parent_swap(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    venv_root = tmp_path / "venv"
    bin_dir = venv_root / "bin"
    bin_dir.mkdir(parents=True, mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    outside_activation = outside / "activate"
    outside_activation.write_text("keep", encoding="utf-8")
    original_iterdir = Path.iterdir
    original_open = os.open
    swapped = False

    def swap_bin():
        nonlocal swapped
        if swapped:
            return
        bin_dir.rmdir()
        bin_dir.symlink_to(outside, target_is_directory=True)
        swapped = True

    def swap_before_iterdir(path):
        if path == bin_dir:
            swap_bin()
        return original_iterdir(path)

    def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == "bin" and dir_fd is not None:
            swap_bin()
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(Path, "iterdir", swap_before_iterdir)
    monkeypatch.setattr(integration_installer.os, "open", swap_before_open)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._remove_activation_files(venv_root)

    assert swapped
    assert outside_activation.exists()


def test_remove_activation_files_rejects_replaced_entry_before_unlink(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(mode=0o700)
    activation = bin_dir / "activate"
    activation.write_text("owned", encoding="utf-8")
    replaced = False
    original_stat = os.stat

    def replace_before_stat(name, *args, **kwargs):
        nonlocal replaced
        if name == "activate" and kwargs.get("dir_fd") is not None and not replaced:
            activation.unlink()
            activation.write_text("foreign", encoding="utf-8")
            replaced = True
        return original_stat(name, *args, **kwargs)

    monkeypatch.setattr(integration_installer.os, "stat", replace_before_stat)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._remove_activation_files(tmp_path)

    assert replaced
    assert activation.read_text(encoding="utf-8") == "foreign"


def test_remove_activation_files_rejects_replaced_lib64_before_unlink(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    (tmp_path / "bin").mkdir(mode=0o700)
    lib64 = tmp_path / "lib64"
    lib64.symlink_to("owned-target")
    replacement = tmp_path / "foreign-target"
    replacement.mkdir(mode=0o700)
    calls = 0
    original_stat = os.stat

    def replace_before_lib64_unlink(name, *args, **kwargs):
        nonlocal calls
        if name == "lib64" and kwargs.get("dir_fd") is not None:
            calls += 1
            if calls == 2:
                lib64.unlink()
                lib64.symlink_to(replacement.name)
        return original_stat(name, *args, **kwargs)

    monkeypatch.setattr(integration_installer.os, "stat", replace_before_lib64_unlink)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._remove_activation_files(tmp_path)

    assert calls == 2
    assert lib64.is_symlink()
    assert lib64.resolve() == replacement


def test_remove_activation_files_rejects_foreign_owned_entry(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(mode=0o700)
    activation = bin_dir / "activate"
    activation.write_text("owned", encoding="utf-8")
    item = activation.lstat()
    foreign = SimpleNamespace(
        st_dev=item.st_dev,
        st_ino=item.st_ino,
        st_uid=os.getuid() + 1,
        st_mode=item.st_mode,
        st_nlink=item.st_nlink,
    )

    class ForeignEntry:
        name = "activate"

        def stat(self, *, follow_symlinks=False):
            return foreign

    class ForeignScan:
        def __enter__(self):
            return iter((ForeignEntry(),))

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    original_stat = integration_installer.os.stat
    original_unlink = integration_installer.os.unlink
    unlinked = False

    def foreign_scandir(fd):
        return ForeignScan()

    def foreign_stat(name, *args, **kwargs):
        if name == "activate" and kwargs.get("dir_fd") is not None:
            return foreign
        return original_stat(name, *args, **kwargs)

    def record_unlink(name, *args, **kwargs):
        nonlocal unlinked
        if name == "activate" and kwargs.get("dir_fd") is not None:
            unlinked = True
            return None
        return original_unlink(name, *args, **kwargs)

    monkeypatch.setattr(integration_installer.os, "scandir", foreign_scandir)
    monkeypatch.setattr(integration_installer.os, "stat", foreign_stat)
    monkeypatch.setattr(integration_installer.os, "unlink", record_unlink)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._remove_activation_files(tmp_path)

    assert not unlinked
    assert activation.read_text(encoding="utf-8") == "owned"


def test_remove_activation_files_rejects_foreign_owned_lib64(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    (tmp_path / "bin").mkdir(mode=0o700)
    target = tmp_path / "lib64-target"
    target.mkdir(mode=0o700)
    lib64 = tmp_path / "lib64"
    lib64.symlink_to(target.name)
    item = lib64.lstat()
    foreign = SimpleNamespace(
        st_dev=item.st_dev,
        st_ino=item.st_ino,
        st_uid=os.getuid() + 1,
        st_mode=item.st_mode,
        st_nlink=item.st_nlink,
    )
    original_stat = integration_installer.os.stat
    original_unlink = integration_installer.os.unlink
    unlinked = False

    def foreign_stat(name, *args, **kwargs):
        if name == "lib64" and kwargs.get("dir_fd") is not None:
            return foreign
        return original_stat(name, *args, **kwargs)

    def record_unlink(name, *args, **kwargs):
        nonlocal unlinked
        if name == "lib64" and kwargs.get("dir_fd") is not None:
            unlinked = True
            return None
        return original_unlink(name, *args, **kwargs)

    monkeypatch.setattr(integration_installer.os, "stat", foreign_stat)
    monkeypatch.setattr(integration_installer.os, "unlink", record_unlink)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._remove_activation_files(tmp_path)

    assert not unlinked
    assert lib64.is_symlink()
    assert lib64.resolve() == target


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


def test_release_version_is_06542_across_project_surfaces():
    from codex_usage import (
        __version__,
        integration_attestation,
        integration_installer,
        integration_pool_authority,
        service,
    )

    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    applet = json.loads(
        (PROJECT_ROOT / "files/codex-usage@H234598/metadata.json").read_text(
            encoding="utf-8"
        )
    )

    assert integration_installer.RELEASE_VERSION == "0.6.542"
    assert project["project"]["version"] == "0.6.542"
    assert __version__ == "0.6.542"
    assert applet["version"] == "0.6.542"
    assert applet["comments"] == "Version: 0.6.542"
    assert integration_attestation._EXPECTED_VERSION == "0.6.542"
    assert integration_attestation._DIST_INFO_PREFIX == (
        "codex_usage_integration_producer-0.6.542.dist-info"
    )
    assert integration_attestation._CORE_DIST_INFO_PREFIX == "codex_usage-0.6.542.dist-info"
    assert integration_attestation._PREVIOUS_SCHEMA2_VERSION == "0.6.541"
    assert integration_attestation._PREVIOUS_SCHEMA2_DIST_INFO_PREFIX == (
        "codex_usage_integration_producer-0.6.541.dist-info"
    )
    assert integration_pool_authority.PRODUCER_VERSION == "0.6.542"
    assert service.EXPECTED_DISTRIBUTION_VERSION == "0.6.542"


def test_runtime_rejects_compromised_06535_but_installer_upgrades_verified_06540(
    tmp_path,
):
    from codex_usage.integration_attestation import IntegrationAttestationUnavailable

    assert install_verified_06540_source(tmp_path / "verified-06540").version == "0.6.542"
    with pytest.raises(IntegrationAttestationUnavailable):
        verify_compromised_06535_runtime(tmp_path / "compromised-06535")


def _write_runtime_bytecode(
    release_dir: Path,
    *,
    invalidation_mode: py_compile.PycInvalidationMode = (
        py_compile.PycInvalidationMode.TIMESTAMP
    ),
) -> Path:
    source = next(
        release_dir.glob(
            "venv/lib/python*/site-packages/codex_usage/integration_entrypoint.py"
        )
    )
    cache_path = Path(importlib.util.cache_from_source(str(source)))
    py_compile.compile(
        str(source),
        cfile=str(cache_path),
        doraise=True,
        invalidation_mode=invalidation_mode,
    )
    cache_path.parent.chmod(0o755)
    cache_path.chmod(0o600)
    return cache_path


@pytest.mark.parametrize(
    "invalidation_mode",
    (
        py_compile.PycInvalidationMode.TIMESTAMP,
        py_compile.PycInvalidationMode.CHECKED_HASH,
    ),
)
def test_install_cutover_accepts_expected_runtime_bytecode_in_06536_release(
    tmp_path,
    invalidation_mode,
):
    from codex_usage import integration_attestation, integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    with pytest.MonkeyPatch.context() as context:
        _patch_release_identity(
            context,
            integration_attestation._PREVIOUS_SCHEMA2_VERSION,
            source_root=source_root,
        )
        previous = integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    _set_fixture_source_release_version(source_root, "0.6.542")
    cache_path = _write_runtime_bytecode(
        previous.release_dir,
        invalidation_mode=invalidation_mode,
    )

    installed = integration_installer.install_release(
        source_root=source_root,
        state_home=state_home,
        data_home=data_home,
        python_executable=Path(sys.executable),
        temporary_root=temporary_root,
    )

    assert installed.version == "0.6.542"
    assert cache_path.is_file()
    current_cache = _write_runtime_bytecode(installed.release_dir)
    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation.verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=installed.entrypoint_path,
        )
    assert current_cache.is_file()


@pytest.mark.parametrize(
    "addition",
    (
        "unknown-cache-name",
        "foreign-release-file",
        "invalid-pyc-flags",
        "pyc-header-source-mismatch",
        "pyc-payload-tampering",
        "attested-source-tampering",
    ),
)
def test_install_cutover_rejects_unexpected_addition_beside_06536_runtime_bytecode(
    tmp_path,
    addition,
):
    from codex_usage import integration_attestation, integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    with pytest.MonkeyPatch.context() as context:
        _patch_release_identity(
            context,
            integration_attestation._PREVIOUS_SCHEMA2_VERSION,
            source_root=source_root,
        )
        previous = integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    _set_fixture_source_release_version(source_root, "0.6.542")
    cache_path = _write_runtime_bytecode(previous.release_dir)
    if addition == "unknown-cache-name":
        unexpected = cache_path.with_name("foreign.cpython-999.pyc")
        unexpected.write_bytes(cache_path.read_bytes())
    elif addition == "foreign-release-file":
        unexpected = previous.release_dir / "foreign-runtime-artifact"
        unexpected.write_bytes(b"foreign")
    elif addition == "invalid-pyc-flags":
        unexpected = cache_path
        payload = bytearray(unexpected.read_bytes())
        payload[4:8] = (2).to_bytes(4, "little")
        unexpected.write_bytes(payload)
    elif addition == "pyc-header-source-mismatch":
        unexpected = cache_path
        payload = bytearray(unexpected.read_bytes())
        payload[8] ^= 1
        unexpected.write_bytes(payload)
    elif addition == "pyc-payload-tampering":
        unexpected = cache_path
        payload = bytearray(unexpected.read_bytes())
        payload[-1] ^= 1
        unexpected.write_bytes(payload)
    else:
        unexpected = cache_path.parent.parent / "integration_entrypoint.py"
        unexpected.write_bytes(unexpected.read_bytes() + b"\n")
    unexpected.chmod(0o600)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    active_before = active_path.read_bytes()

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )

    assert active_path.read_bytes() == active_before
    assert unexpected.is_file()
    assert not (integration / "previous.json").exists()


def test_06536_bytecode_validation_rejects_source_inode_swap_after_tree_scan(
    tmp_path,
    monkeypatch,
):
    from codex_usage import integration_attestation, integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    with pytest.MonkeyPatch.context() as context:
        _patch_release_identity(
            context,
            integration_attestation._PREVIOUS_SCHEMA2_VERSION,
            source_root=source_root,
        )
        previous = integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    _set_fixture_source_release_version(source_root, "0.6.542")
    cache_path = _write_runtime_bytecode(previous.release_dir)
    source_path = cache_path.parent.parent / "integration_entrypoint.py"
    staged = tmp_path / "staged-race"
    staged.mkdir(mode=0o700)
    malicious_source = staged / source_path.name
    malicious_source.write_bytes(source_path.read_bytes() + b"\n# raced source\n")
    malicious_source.chmod(0o600)
    malicious_cache = staged / cache_path.name
    py_compile.compile(
        str(malicious_source),
        cfile=str(malicious_cache),
        dfile=str(source_path),
        doraise=True,
    )
    malicious_cache.chmod(0o600)
    held_original_source = staged / "held-original.py"
    original_inode = source_path.stat().st_ino
    raced = False
    original_scandir = integration_attestation.os.scandir

    class OrderedPackageScan:
        def __init__(self, candidate):
            self._candidate = candidate
            self._scan = None

        def __enter__(self):
            self._scan = original_scandir(self._candidate)
            entries = list(self._scan.__enter__())
            try:
                scanned_path = Path(os.readlink(f"/proc/self/fd/{self._candidate}"))
            except (OSError, TypeError):
                scanned_path = None
            if scanned_path == source_path.parent:
                entries.sort(key=lambda entry: entry.name == "__pycache__")
            return iter(entries)

        def __exit__(self, *args):
            assert self._scan is not None
            return self._scan.__exit__(*args)

    monkeypatch.setattr(integration_attestation.os, "scandir", OrderedPackageScan)

    def replace_source_and_bytecode(package_path):
        nonlocal raced
        assert package_path == source_path.parent
        if raced:
            return
        raced = True
        os.replace(source_path, held_original_source)
        os.replace(malicious_source, source_path)
        os.replace(malicious_cache, cache_path)

    monkeypatch.setattr(
        integration_attestation,
        "_before_expected_runtime_bytecode_validation",
        replace_source_and_bytecode,
    )
    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation._release_tree_sha256(
            release_dir=previous.release_dir,
            expected_runtime_bytecode_root=(
                "./venv/lib/"
                f"{source_path.parents[2].name}/site-packages/codex_usage"
            ),
        )

    assert raced
    assert source_path.stat().st_ino != original_inode


def test_06536_bytecode_validator_binds_pyc_to_held_original_source_fd(
    tmp_path,
):
    from codex_usage import integration_attestation

    package_path = tmp_path / "codex_usage"
    package_path.mkdir(mode=0o700)
    source_name = "integration_entrypoint.py"
    canonical_source = package_path / source_name
    canonical_source.write_bytes(b"VALUE = 'replacement'\n")
    canonical_source.chmod(0o600)
    held_source = tmp_path / "held-original.py"
    held_source.write_bytes(b"VALUE = 'original'\n")
    held_source.chmod(0o600)
    cache_path = Path(importlib.util.cache_from_source(str(canonical_source)))
    py_compile.compile(
        str(canonical_source),
        cfile=str(cache_path),
        dfile=str(canonical_source),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
    )
    cache_path.parent.chmod(0o755)
    cache_path.chmod(0o600)
    source_fd = os.open(held_source, os.O_RDONLY | os.O_NOFOLLOW)
    cache_fd = os.open(
        cache_path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        source_item = os.fstat(source_fd)
        cache_item = os.fstat(cache_fd)

        with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
            integration_attestation._validate_expected_runtime_bytecode(
                package_entries=((source_name, source_fd, source_item),),
                cache_fd=cache_fd,
                cache_item=cache_item,
                python_directory=(
                    f"python{sys.version_info.major}.{sys.version_info.minor}"
                ),
                package_path=package_path,
            )

        assert os.read(source_fd, source_item.st_size) == held_source.read_bytes()
        assert canonical_source.read_bytes() != held_source.read_bytes()
    finally:
        os.close(cache_fd)
        os.close(source_fd)


def _compromised_06535_with_previous_06534(tmp_path: Path):
    from codex_usage import integration_installer
    from codex_usage.private_io import write_private_text

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    with pytest.MonkeyPatch.context() as context:
        _patch_release_identity(context, "0.6.534", source_root=source_root)
        previous = integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    integration = state_home / "codex-usage" / "integration"
    historical_previous = (integration / "active.json").read_bytes()
    with pytest.MonkeyPatch.context() as context:
        _patch_release_identity(context, "0.6.535", source_root=source_root)
        from codex_usage import integration_attestation

        context.setattr(
            integration_attestation,
            "_PREVIOUS_SCHEMA2_VERSION",
            "0.6.534",
        )
        context.setattr(
            integration_attestation,
            "_PREVIOUS_SCHEMA2_DIST_INFO_PREFIX",
            "codex_usage_integration_producer-0.6.534.dist-info",
        )
        compromised = integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    pycache = compromised.release_dir / "venv/lib/__pycache__"
    pycache.mkdir(mode=0o700)
    (pycache / "compromised.pyc").write_bytes(b"compromised")
    # Historical malformed-marker tests need an inert predecessor artifact.
    # D299's real 0.6.542 install path never creates or consumes it.
    write_private_text(
        integration / "previous.json",
        historical_previous.decode("utf-8"),
        label="historical predecessor fixture",
        mode=0o600,
    )
    _set_fixture_source_release_version(source_root, "0.6.542")
    assert json.loads((integration / "previous.json").read_bytes())["version"] == "0.6.534"
    return (
        previous,
        compromised,
        source_root,
        data_home,
        state_home,
        temporary_root,
    )


def test_installer_never_recovers_compromised_06535_from_previous_release(
    tmp_path,
):
    from codex_usage import integration_attestation, integration_installer

    (
        previous,
        compromised,
        source_root,
        data_home,
        state_home,
        temporary_root,
    ) = _compromised_06535_with_previous_06534(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    previous_path = integration / "previous.json"
    previous_before = previous_path.read_bytes()
    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation._verify_manifest(
            manifest_path=integration / "active.json",
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=None,
        )

    active_before = (integration / "active.json").read_bytes()
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )

    assert (integration / "active.json").read_bytes() == active_before
    assert previous_path.read_bytes() == previous_before
    assert json.loads(previous_before)["release_id"] == previous.release_dir.name
    assert json.loads(previous_before)["release_id"] != compromised.release_dir.name


_COMPROMISED_06535_MARKER_MUTATIONS = (
    "extra-field",
    "missing-field",
    "schema-version-value",
    "schema-version-float",
    "version-value",
    "version-type",
    "state-home-value",
    "state-home-type",
    "data-home-value",
    "data-home-type",
    "source-digest-value",
    "source-digest-type",
    "source-digest-uppercase",
    "source-digest-short",
    "release-id-value",
    "release-id-type",
    "release-dir-type",
    "release-dir-path",
    "launcher-path-type",
    "launcher-path-name",
    "entrypoint-path-type",
    "entrypoint-path-name",
    "entrypoint-path-python-version",
    "record-path-type",
    "record-path-version",
    "record-path-python-version",
    "wheel-path-type",
    "wheel-path-name",
    "entrypoint-digest-type",
    "entrypoint-digest-uppercase",
    "launcher-digest-type",
    "launcher-digest-uppercase",
    "record-digest-type",
    "record-digest-uppercase",
    "wheel-digest-type",
    "wheel-digest-uppercase",
    "release-tree-digest-type",
    "release-tree-digest-uppercase",
)


def _mutate_compromised_06535_marker(
    manifest: dict[str, object],
    mutation: str,
) -> None:
    release_dir = Path(str(manifest["release_dir"]))
    site_packages = Path(str(manifest["record_path"])).parent.parent
    digest_field = mutation.removesuffix("-type").removesuffix("-uppercase")
    digest_fields = {
        "entrypoint-digest": "entrypoint_sha256",
        "launcher-digest": "launcher_sha256",
        "record-digest": "record_sha256",
        "wheel-digest": "wheel_sha256",
        "release-tree-digest": "release_tree_sha256",
    }
    if mutation == "extra-field":
        manifest["untrusted_recovery_hint"] = True
    elif mutation == "missing-field":
        del manifest["launcher_sha256"]
    elif mutation == "schema-version-value":
        manifest["schema_version"] = 1
    elif mutation == "schema-version-float":
        manifest["schema_version"] = 2.0
    elif mutation == "version-value":
        manifest["version"] = "0.6.534"
    elif mutation == "version-type":
        manifest["version"] = 535
    elif mutation == "state-home-value":
        manifest["state_home"] = str(Path(str(manifest["state_home"])).parent)
    elif mutation == "state-home-type":
        manifest["state_home"] = 1
    elif mutation == "data-home-value":
        manifest["data_home"] = str(Path(str(manifest["data_home"])).parent)
    elif mutation == "data-home-type":
        manifest["data_home"] = 1
    elif mutation == "source-digest-value":
        manifest["source_manifest_sha256"] = "0" * 64
    elif mutation == "source-digest-type":
        manifest["source_manifest_sha256"] = 1
    elif mutation == "source-digest-uppercase":
        manifest["source_manifest_sha256"] = "A" * 64
    elif mutation == "source-digest-short":
        manifest["source_manifest_sha256"] = "a" * 63
    elif mutation == "release-id-value":
        manifest["release_id"] = "0.6.535-0000000000000000"
    elif mutation == "release-id-type":
        manifest["release_id"] = 1
    elif mutation == "release-dir-type":
        manifest["release_dir"] = 1
    elif mutation == "release-dir-path":
        manifest["release_dir"] = str(release_dir.parent / "0.6.535-wrong")
    elif mutation == "launcher-path-type":
        manifest["launcher_path"] = 1
    elif mutation == "launcher-path-name":
        manifest["launcher_path"] = str(release_dir / "venv/bin/not-codex-usage")
    elif mutation == "entrypoint-path-type":
        manifest["entrypoint_path"] = 1
    elif mutation == "entrypoint-path-name":
        manifest["entrypoint_path"] = str(
            site_packages / "codex_usage/not-integration-entrypoint.py"
        )
    elif mutation == "entrypoint-path-python-version":
        manifest["entrypoint_path"] = str(
            release_dir
            / "venv/lib/not-python/site-packages/codex_usage/integration_entrypoint.py"
        )
    elif mutation == "record-path-type":
        manifest["record_path"] = 1
    elif mutation == "record-path-version":
        manifest["record_path"] = str(
            site_packages
            / "codex_usage_integration_producer-0.6.536.dist-info/RECORD"
        )
    elif mutation == "record-path-python-version":
        manifest["record_path"] = str(
            release_dir
            / "venv/lib/not-python/site-packages/"
            "codex_usage_integration_producer-0.6.535.dist-info/RECORD"
        )
    elif mutation == "wheel-path-type":
        manifest["wheel_path"] = 1
    elif mutation == "wheel-path-name":
        manifest["wheel_path"] = str(release_dir / "not-producer.whl")
    elif digest_field in digest_fields:
        manifest[digest_fields[digest_field]] = (
            1 if mutation.endswith("-type") else "A" * 64
        )
    else:  # pragma: no cover - parameter table is closed above
        raise AssertionError(mutation)


@pytest.mark.parametrize("mutation", _COMPROMISED_06535_MARKER_MUTATIONS)
def test_installer_recovery_rejects_malformed_compromised_active_marker_without_mutation(
    tmp_path,
    mutation,
):
    from codex_usage import integration_installer
    from codex_usage.private_io import write_private_text

    (
        _,
        _,
        source_root,
        data_home,
        state_home,
        temporary_root,
    ) = _compromised_06535_with_previous_06534(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    previous_path = integration / "previous.json"
    manifest = json.loads(active_path.read_bytes())
    _mutate_compromised_06535_marker(manifest, mutation)
    write_private_text(
        active_path,
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        label="malformed compromised active integration manifest",
        mode=0o600,
    )
    active_before = active_path.read_bytes()
    previous_before = previous_path.read_bytes()
    releases_before = {path.name for path in (integration / "releases").iterdir()}

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )

    assert active_path.read_bytes() == active_before
    assert previous_path.read_bytes() == previous_before
    assert {path.name for path in (integration / "releases").iterdir()} == releases_before


@pytest.mark.parametrize("mutation", ["unknown-field", "forbidden-pyc"])
def test_installer_recovery_rejects_invalid_previous_without_mutation(
    tmp_path,
    mutation,
):
    from codex_usage import integration_installer
    from codex_usage.private_io import write_private_text

    (
        previous,
        _,
        source_root,
        data_home,
        state_home,
        temporary_root,
    ) = _compromised_06535_with_previous_06534(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    previous_path = integration / "previous.json"
    if mutation == "unknown-field":
        manifest = json.loads(previous_path.read_bytes())
        manifest["untrusted_upgrade_fallback"] = True
        write_private_text(
            previous_path,
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            label="malicious previous integration manifest",
            mode=0o600,
        )
    else:
        pycache = previous.release_dir / "venv/lib/__pycache__"
        pycache.mkdir(mode=0o700)
        (pycache / "previous.pyc").write_bytes(b"forbidden")
    active_before = active_path.read_bytes()
    previous_before = previous_path.read_bytes()
    releases_before = {
        path.name for path in (integration / "releases").iterdir()
    }

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )

    assert active_path.read_bytes() == active_before
    assert previous_path.read_bytes() == previous_before
    assert {
        path.name for path in (integration / "releases").iterdir()
    } == releases_before


@pytest.mark.parametrize("mutation", ["version-06533", "schema1"])
def test_installer_recovery_never_uses_legacy_active_as_fallback(
    tmp_path,
    mutation,
):
    from codex_usage import integration_installer
    from codex_usage.private_io import write_private_text

    (
        _,
        _,
        source_root,
        data_home,
        state_home,
        temporary_root,
    ) = _compromised_06535_with_previous_06534(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    previous_path = integration / "previous.json"
    manifest = json.loads(active_path.read_bytes())
    if mutation == "version-06533":
        manifest["version"] = "0.6.533"
    else:
        manifest["schema_version"] = 1
    write_private_text(
        active_path,
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        label="legacy active integration manifest",
        mode=0o600,
    )
    active_before = active_path.read_bytes()
    previous_before = previous_path.read_bytes()

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )

    assert active_path.read_bytes() == active_before
    assert previous_path.read_bytes() == previous_before


def test_installer_recovery_build_failure_preserves_active_and_previous(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    (
        _,
        _,
        source_root,
        data_home,
        state_home,
        temporary_root,
    ) = _compromised_06535_with_previous_06534(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    previous_path = integration / "previous.json"
    active_before = active_path.read_bytes()
    previous_before = previous_path.read_bytes()

    def fail_build(**_kwargs):
        raise OSError("synthetic recovery build failure")

    monkeypatch.setattr(integration_installer, "_build_verified_wheel", fail_build)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )

    assert active_path.read_bytes() == active_before
    assert previous_path.read_bytes() == previous_before


def test_release_tree_rejects_pyc_and_builder_invokes_python_with_b_and_env(tmp_path):
    from codex_usage.integration_attestation import (
        IntegrationAttestationUnavailable,
        _release_tree_sha256,
    )

    release_tree = tmp_path / "release-tree"
    pycache = release_tree / "__pycache__"
    pycache.mkdir(mode=0o700, parents=True)
    (pycache / "payload.pyc").write_bytes(b"bytecode")

    with pytest.raises(IntegrationAttestationUnavailable):
        _release_tree_sha256(release_dir=release_tree)
    assert captured_builder_environment()["PYTHONDONTWRITEBYTECODE"] == "1"
    assert captured_python_argv(tmp_path)[:2] == ("-B", "-I")


def test_installer_bootstraps_exact_evidence_lock_root_and_authority_inode(tmp_path):
    _, _, state_home = _install(tmp_path)

    assert verified_lock_targets(state_home) == {
        "producer-install",
        "current.json",
        "pool-authority-source-v2.json",
    }


def test_install_creates_attested_private_active_release(tmp_path):
    release, data_home, state_home = _install(tmp_path)
    from codex_usage.integration_attestation import verify_active_release

    assert release.version == "0.6.542"
    assert release.release_dir.name.startswith("0.6.542-")
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
    assert active["schema_version"] == 2
    assert active["version"] == "0.6.542"
    assert active["release_id"] == release.release_dir.name
    assert Path(active["record_path"]).parent.name == (
        "codex_usage_integration_producer-0.6.542.dist-info"
    )
    assert active["launcher_sha256"] == release.launcher_sha256
    assert active["release_tree_sha256"] == release.release_tree_sha256
    integration = state_home / "codex-usage" / "integration"
    assert stat.S_IMODE(integration.lstat().st_mode) == 0o700
    assert stat.S_IMODE((integration / "active.json").lstat().st_mode) == 0o600
    assert not list(release.release_dir.rglob("*.json"))
    assert not list((tmp_path / "temporary").rglob("candidate-*.json"))


def test_d297_rejects_public_active_release_rollback(tmp_path):
    """D297 retains only transaction-local precommit restoration."""
    _release, data_home, state_home = _install(tmp_path)
    from codex_usage import integration_installer

    active = state_home / "codex-usage" / "integration" / "active.json"
    before = active.read_bytes()
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.rollback_active_release(
            state_home=state_home,
            data_home=data_home,
        )
    assert active.read_bytes() == before


@pytest.mark.parametrize(
    ("residue_name", "residue_kind"),
    (
        (".evidence-v1-cutover-current-" + "a" * 32, "file"),
        (".evidence-v1-cutover-generations-" + "a" * 32, "directory"),
        (".evidence-v1-cutover-current-" + "b" * 32, "symlink"),
        (".evidence-v1-cutover-unknown", "file"),
        (".evidence-v1-cutover-broken", "directory"),
        (".evidence-v1-cutover-malformed", "symlink"),
    ),
)
def test_d297_rejects_all_v1_cutover_residues_before_recovery_or_mutation(
    tmp_path,
    monkeypatch,
    residue_name,
    residue_kind,
):
    """V1 residue is an input violation, never a recoverable installer state."""
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    integration_installer.install_release(
        source_root=source_root,
        state_home=state_home,
        data_home=data_home,
        python_executable=Path(sys.executable),
        temporary_root=temporary_root,
    )
    integration = state_home / "codex-usage" / "integration"
    active = integration / "active.json"
    current = integration / "current.json"
    generations = integration / "generations"
    current.write_bytes(b"must-remain-an-untouched-current-fixture")
    current.chmod(0o600)
    residue = integration / residue_name
    if residue_kind == "file":
        residue.write_bytes(b"valid-looking-v1-residue")
        residue.chmod(0o600)
    elif residue_kind == "directory":
        residue.mkdir(mode=0o700)
        residue.chmod(0o700)
    else:
        residue.symlink_to("current.json")

    def snapshot(path: Path):
        item = path.lstat()
        payload = path.read_bytes() if stat.S_ISREG(item.st_mode) else None
        target = path.readlink() if stat.S_ISLNK(item.st_mode) else None
        return (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_nlink,
            payload,
            target,
        )

    active_before = snapshot(active)
    current_before = snapshot(current)
    generations_before = (snapshot(generations), _foreign_tree_digest(root=generations))
    residue_before = snapshot(residue)

    def reject_recovery(**_kwargs):
        pytest.fail("D297 must reject V1 residue before any recovery")

    monkeypatch.setattr(
        integration_installer,
        "_recover_active_transactions",
        reject_recovery,
    )
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )

    assert snapshot(active) == active_before
    assert snapshot(current) == current_before
    assert (snapshot(generations), _foreign_tree_digest(root=generations)) == generations_before
    assert snapshot(residue) == residue_before


def _install_attested_d300_predecessor(tmp_path: Path):
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    with pytest.MonkeyPatch.context() as context:
        _patch_release_identity(context, "0.6.541", source_root=source_root)
        predecessor = integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    return predecessor, source_root, data_home, state_home, temporary_root


def test_d300_cutover_accepts_only_attested_06541_predecessor_and_preserves_v2_contract(
    tmp_path,
):
    from codex_usage import integration_installer

    predecessor, source_root, data_home, state_home, temporary_root = (
        _install_attested_d300_predecessor(tmp_path)
    )
    assert predecessor.version == "0.6.541"
    _set_fixture_source_release_version(source_root, "0.6.542")
    installed = integration_installer.install_release(
        source_root=source_root,
        state_home=state_home,
        data_home=data_home,
        python_executable=Path(sys.executable),
        temporary_root=temporary_root,
    )

    integration = state_home / "codex-usage" / "integration"
    assert installed.version == "0.6.542"
    assert not (integration / "previous.json").exists()
    assert not (integration / "current.json").exists()


def test_d300_rejects_attested_06540_predecessor_without_mutation(
    tmp_path,
    monkeypatch,
):
    """D300 accepts no compatibility predecessor older than 0.6.541."""
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    with pytest.MonkeyPatch.context() as context:
        _patch_release_identity(context, "0.6.540", source_root=source_root)
        predecessor = integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    assert predecessor.version == "0.6.540"
    _set_fixture_source_release_version(source_root, "0.6.542")
    integration = state_home / "codex-usage" / "integration"
    active = integration / "active.json"
    before = active.read_bytes()
    active_identity = (active.lstat().st_dev, active.lstat().st_ino)
    generations = integration / "generations"
    generations_identity = (generations.lstat().st_dev, generations.lstat().st_ino)

    def reject_recovery(**_kwargs):
        pytest.fail("D300 must reject a 0.6.540 active before any recovery")

    monkeypatch.setattr(
        integration_installer,
        "_recover_active_transactions",
        reject_recovery,
    )

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )

    assert active.read_bytes() == before
    assert (active.lstat().st_dev, active.lstat().st_ino) == active_identity
    assert (generations.lstat().st_dev, generations.lstat().st_ino) == generations_identity
    assert not (integration / "previous.json").exists()
    assert not (integration / "current.json").exists()


def test_d300_rejects_attested_06539_predecessor_without_mutation(
    tmp_path,
    monkeypatch,
):
    """The new 0.6.542 release has no 0.6.539 compatibility cutover."""
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    with pytest.MonkeyPatch.context() as context:
        _patch_release_identity(context, "0.6.539", source_root=source_root)
        predecessor = integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    assert predecessor.version == "0.6.539"
    _set_fixture_source_release_version(source_root, "0.6.542")
    integration = state_home / "codex-usage" / "integration"
    active = integration / "active.json"
    before = active.read_bytes()
    active_identity = (active.lstat().st_dev, active.lstat().st_ino)

    def reject_recovery(**_kwargs):
        pytest.fail("D300 must reject a 0.6.539 active before any recovery")

    monkeypatch.setattr(
        integration_installer,
        "_recover_active_transactions",
        reject_recovery,
    )

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )

    assert active.read_bytes() == before
    assert (active.lstat().st_dev, active.lstat().st_ino) == active_identity
    assert not (integration / "previous.json").exists()
    assert not (integration / "current.json").exists()


def test_d300_cutover_preserves_existing_v2_namespace(tmp_path):
    from codex_usage import integration_installer

    _predecessor, source_root, data_home, state_home, temporary_root = (
        _install_attested_d300_predecessor(tmp_path)
    )
    _set_fixture_source_release_version(source_root, "0.6.542")
    integration = state_home / "codex-usage" / "integration"
    generation = integration / "generations" / ("a" * 32)
    generation.mkdir(mode=0o700)
    generation.chmod(0o700)
    before = _foreign_tree_digest(root=integration / "generations")

    installed = integration_installer.install_release(
        source_root=source_root,
        state_home=state_home,
        data_home=data_home,
        python_executable=Path(sys.executable),
        temporary_root=temporary_root,
    )

    assert installed.version == "0.6.542"
    assert _foreign_tree_digest(root=integration / "generations") == before


def test_install_secures_generated_venv_directories_without_path_chmod(
    tmp_path, monkeypatch
):
    original_chmod = Path.chmod

    def reject_generated_directory_chmod(path, mode):
        if path.name in {"venv", "site-packages"}:
            pytest.fail("generated venv directories require directory-FD mode changes")
        return original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", reject_generated_directory_chmod)
    release, _, _ = _install(tmp_path)

    venv_root = release.release_dir / "venv"
    site_packages = next(venv_root.glob("lib/python*/site-packages"))
    assert stat.S_IMODE(venv_root.lstat().st_mode) == 0o700
    assert stat.S_IMODE(site_packages.lstat().st_mode) == 0o700
    assert not venv_root.is_symlink()
    assert not site_packages.is_symlink()


def test_find_site_packages_rejects_python_directory_swap_before_open(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    venv_root = tmp_path / "venv"
    lib = venv_root / "lib"
    python_dir = lib / "python3.14"
    site_packages = python_dir / "site-packages"
    venv_root.mkdir(mode=0o700)
    lib.mkdir(mode=0o755)
    python_dir.mkdir(mode=0o755)
    site_packages.mkdir(mode=0o755)
    venv_root.chmod(0o700)
    lib.chmod(0o755)
    python_dir.chmod(0o755)
    site_packages.chmod(0o755)
    outside = tmp_path / "outside"
    (outside / "site-packages").mkdir(mode=0o755, parents=True)
    outside.chmod(0o755)
    old_python = lib / "python-old"
    venv_identity = integration_installer._directory_identity(venv_root)
    original_open = os.open
    swapped = False

    def swap_before_python_open(candidate, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if candidate == python_dir.name and dir_fd is not None and not swapped:
            python_dir.rename(old_python)
            outside.rename(python_dir)
            swapped = True
        if dir_fd is None:
            return original_open(candidate, flags, mode)
        return original_open(candidate, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(integration_installer.os, "open", swap_before_python_open)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._find_site_packages(venv_root, venv_identity)
    assert swapped
    assert (old_python / "site-packages").is_dir()


@pytest.mark.parametrize("schema_version", [1, True, 2.0, "2"])
def test_attestation_requires_exact_integer_schema_version(tmp_path, schema_version):
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
    assert manifest["schema_version"] == 2
    manifest["schema_version"] = schema_version
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


@pytest.mark.parametrize("mutation", ["unknown", "secret-like", "missing"])
def test_active_manifest_requires_exact_canonical_fields(tmp_path, mutation):
    from codex_usage.integration_attestation import (
        IntegrationAttestationUnavailable,
        verify_active_release,
    )

    release, data_home, state_home = _install(tmp_path)
    active_path = state_home / "codex-usage" / "integration" / "active.json"
    manifest = json.loads(active_path.read_text(encoding="utf-8"))
    mutated = _mutate_manifest_fields(manifest, mutation)
    active_path.write_text(
        json.dumps(mutated, sort_keys=True, separators=(",", ":")) + "\n",
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


@pytest.mark.parametrize(
    "value",
    ["+" + "a" * 63, " " + "a" * 63, "A" * 64],
)
def test_attestation_manifest_hash_requires_lowercase_hex(value):
    from codex_usage import integration_attestation

    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation._valid_hash(value)


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


def _exit_after_pointer_temp_create_or_fsync(
    integration_text: str,
    index: int,
    crash_point: str,
) -> None:
    integration_fd = os.open(
        integration_text,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    name = f".tmp-current.json-{index:032x}"
    fd = os.open(
        name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=integration_fd,
    )
    if crash_point == "create":
        os._exit(77)
    if crash_point != "fsync":
        os._exit(92)
    if os.write(fd, b"{}") != 2:
        os._exit(91)
    os.fsync(fd)
    os._exit(77)


def _leave_pointer_temp_crash_debris(
    integration: Path,
    count: int,
    crash_point: str,
) -> None:
    context = multiprocessing.get_context("fork")
    for index in range(count):
        process = context.Process(
            target=_exit_after_pointer_temp_create_or_fsync,
            args=(str(integration), index, crash_point),
        )
        process.start()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        assert process.exitcode == 77


def _try_current_lock(state_home_text: str, result) -> None:
    from codex_usage.private_io import private_path_lock

    current = Path(state_home_text) / "codex-usage/integration/current.json"
    try:
        with private_path_lock(
            current,
            timeout_seconds=0,
            label="test current lock",
            create=False,
        ):
            result.put("acquired")
    except TimeoutError:
        result.put("busy")


def _prepared_active_transaction(tmp_path: Path, operation: str) -> SimpleNamespace:
    from codex_usage import integration_installer
    from codex_usage.private_io import write_private_text

    release, data_home, state_home = _install(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    previous_path = integration / "previous.json"
    active_before = active_path.read_bytes()
    write_private_text(
        previous_path,
        active_before.decode("utf-8") + "\n",
        label="test previous manifest",
        mode=0o600,
    )
    previous_before = previous_path.read_bytes()

    if operation == "install":
        second_root = tmp_path / "atomic-install"
        second_root.mkdir(mode=0o700)
        second_source = _temporary_source_copy(second_root)
        second_entrypoint = second_source / "src/codex_usage/integration_snapshot.py"
        second_entrypoint.write_bytes(
            second_entrypoint.read_bytes() + b"\n# atomic release\n"
        )
        second_temporary = second_root / "temporary"
        second_temporary.mkdir(mode=0o700)

        def run():
            return integration_installer.install_release(
                source_root=second_source,
                state_home=state_home,
                data_home=data_home,
                python_executable=Path(sys.executable),
                temporary_root=second_temporary,
            )

    elif operation == "rollback":

        def run():
            return integration_installer.rollback_active_release(
                state_home=state_home,
                data_home=data_home,
            )

    else:
        raise AssertionError("invalid transaction operation")

    return SimpleNamespace(
        release=release,
        data_home=data_home,
        state_home=state_home,
        integration=integration,
        active_path=active_path,
        previous_path=previous_path,
        active_before=active_before,
        previous_before=previous_before,
        run=run,
    )


@pytest.mark.parametrize("operation", ("install",))
@pytest.mark.parametrize("crash_point", ("create", "fsync"))
def test_install_and_rollback_recover_sixty_one_pointer_temp_crashes_before_scan(
    tmp_path,
    monkeypatch,
    operation,
    crash_point,
):
    """Would fail if 61 real pointer crashes still wedged root scan at entry 65."""
    from codex_usage import integration_installer

    prepared = _prepared_active_transaction(tmp_path, operation)
    _leave_pointer_temp_crash_debris(prepared.integration, 61, crash_point)
    assert len(list(os.scandir(prepared.integration))) == 65
    assert len(
        [
            entry
            for entry in os.scandir(prepared.integration)
            if entry.name.startswith(".tmp-current.json-")
        ]
    ) == 61
    assert {
        entry.stat(follow_symlinks=False).st_size
        for entry in os.scandir(prepared.integration)
        if entry.name.startswith(".tmp-current.json-")
    } == {0 if crash_point == "create" else 2}
    real_scan = integration_installer._active_transaction_artifacts
    observed: list[tuple[int, int]] = []

    def track_scan(**kwargs):
        entries = list(os.scandir(kwargs["parent_fd"]))
        observed.append(
            (
                len(entries),
                sum(
                    entry.name.startswith(".tmp-current.json-")
                    for entry in entries
                ),
            )
        )
        return real_scan(**kwargs)

    monkeypatch.setattr(
        integration_installer,
        "_active_transaction_artifacts",
        track_scan,
    )

    prepared.run()

    assert observed == [(4, 0)]
    assert not any(
        entry.name.startswith(".tmp-current.json-")
        for entry in os.scandir(prepared.integration)
    )


@pytest.mark.parametrize("operation", ("install",))
def test_install_and_rollback_hold_current_exclusive_before_recovery_scan(
    tmp_path,
    monkeypatch,
    operation,
):
    """Would fail if recovery ran under Release EX without Current EX."""
    from codex_usage import integration_installer

    prepared = _prepared_active_transaction(tmp_path, operation)
    real_recover = integration_installer._recover_active_transactions
    observed: list[str] = []

    def probe_current_lock(**kwargs):
        context = multiprocessing.get_context("spawn")
        result = context.Queue()
        process = context.Process(
            target=_try_current_lock,
            args=(str(prepared.state_home), result),
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
        return real_recover(**kwargs)

    monkeypatch.setattr(
        integration_installer,
        "_recover_active_transactions",
        probe_current_lock,
    )

    prepared.run()

    assert observed == ["busy"]


def _write_active_transaction_artifact(path: Path, payload: bytes = b"stale") -> None:
    path.write_bytes(payload)
    path.chmod(0o600)


def _write_bound_stale_transaction_artifact(
    *,
    integration: Path,
    active_path: Path,
    index: int,
    payload: bytes = b"stale",
) -> Path:
    source = integration / f"candidate-source-{index}"
    _write_active_transaction_artifact(source, payload)
    candidate = source.stat()
    prior = active_path.stat()
    artifact = integration / (
        f".active.json.publish-install-{5000 + index}-{index:016x}-"
        f"c{candidate.st_dev:x}-{candidate.st_ino:x}-"
        f"p{prior.st_dev:x}-{prior.st_ino:x}"
    )
    source.rename(artifact)
    return artifact


def _write_forged_bound_transaction_artifact(
    *,
    integration: Path,
    active_path: Path,
    transaction_kind: str,
    payload: bytes,
) -> tuple[Path, tuple[int, int]]:
    source = integration / "foreign-artifact-source"
    _write_active_transaction_artifact(source, payload)
    forged = source.stat()
    active = active_path.stat()
    artifact = integration / (
        f".active.json.publish-{transaction_kind}-6000-fedcba9876543210-"
        f"c{forged.st_dev:x}-{forged.st_ino:x}-"
        f"p{active.st_dev:x}-{active.st_ino:x}"
    )
    source.rename(artifact)
    return artifact, (forged.st_dev, forged.st_ino)


def test_final_install_attestation_failure_restores_active_and_preserves_previous(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer
    from codex_usage.integration_attestation import IntegrationAttestationUnavailable
    from codex_usage.private_io import write_private_text

    release, data_home, state_home = _install(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    previous_path = integration / "previous.json"
    active_before = active_path.read_bytes()
    write_private_text(
        previous_path,
        active_before.decode("utf-8") + "\n",
        label="test previous manifest",
        mode=0o600,
    )
    previous_before = previous_path.read_bytes()
    second_root = tmp_path / "post-swap-install"
    second_root.mkdir(mode=0o700)
    second_source = _temporary_source_copy(second_root)
    second_entrypoint = second_source / "src/codex_usage/integration_snapshot.py"
    second_entrypoint.write_bytes(second_entrypoint.read_bytes() + b"\n# second release\n")
    second_temporary = second_root / "temporary"
    second_temporary.mkdir(mode=0o700)
    original_verify = integration_installer._verify_manifest
    active_verifications = 0

    def fail_final_active_attestation(*args, **kwargs):
        nonlocal active_verifications
        if kwargs["manifest_path"] == active_path:
            active_verifications += 1
            if active_verifications == 2:
                raise IntegrationAttestationUnavailable()
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(
        integration_installer,
        "_verify_manifest",
        fail_final_active_attestation,
    )
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=second_source,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=second_temporary,
        )

    assert active_verifications == 2
    assert active_path.read_bytes() == active_before
    assert previous_path.read_bytes() == previous_before
    assert original_verify(
        manifest_path=active_path,
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=release.entrypoint_path,
    ) == release


def test_final_rollback_attestation_failure_restores_active_and_preserves_previous(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer
    from codex_usage.integration_attestation import IntegrationAttestationUnavailable
    from codex_usage.private_io import write_private_text

    release, data_home, state_home = _install(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    previous_path = integration / "previous.json"
    active_before = active_path.read_bytes()
    write_private_text(
        previous_path,
        active_before.decode("utf-8") + "\n",
        label="test previous manifest",
        mode=0o600,
    )
    previous_before = previous_path.read_bytes()
    original_verify = integration_installer._verify_manifest

    def fail_final_active_attestation(*args, **kwargs):
        if kwargs["manifest_path"] == active_path:
            raise IntegrationAttestationUnavailable()
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(
        integration_installer,
        "_verify_manifest",
        fail_final_active_attestation,
    )
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.rollback_active_release(
            state_home=state_home,
            data_home=data_home,
        )

    assert active_path.read_bytes() == active_before
    assert previous_path.read_bytes() == previous_before
    assert original_verify(
        manifest_path=active_path,
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=release.entrypoint_path,
    ) == release


def test_d297_public_rollback_does_not_start_restore_transaction(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer
    from codex_usage.private_io import write_private_text

    _, data_home, state_home = _install(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    previous_path = integration / "previous.json"
    active_before = active_path.read_bytes()
    write_private_text(
        previous_path,
        active_before.decode("utf-8") + "\n",
        label="test previous manifest",
        mode=0o600,
    )
    previous_before = previous_path.read_bytes()
    monkeypatch.setattr(
        integration_installer,
        "_rollback_active_publish",
        lambda *_args, **_kwargs: pytest.fail("public rollback must not restore"),
    )
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.rollback_active_release(
            state_home=state_home,
            data_home=data_home,
        )

    assert active_path.read_bytes() == active_before
    assert previous_path.read_bytes() == previous_before


@pytest.mark.parametrize("operation", ["install"])
@pytest.mark.parametrize("capture_fault", ["oserror", "mode_drift"])
def test_post_swap_identity_capture_failure_restores_active_and_preserves_previous(
    tmp_path, monkeypatch, operation, capture_fault
):
    from codex_usage import integration_installer
    from codex_usage.integration_attestation import verify_active_release
    from codex_usage.private_io import write_private_text

    release, data_home, state_home = _install(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    previous_path = integration / "previous.json"
    active_before = active_path.read_bytes()
    write_private_text(
        previous_path,
        active_before.decode("utf-8") + "\n",
        label="test previous manifest",
        mode=0o600,
    )
    previous_before = previous_path.read_bytes()

    if operation == "install":
        second_root = tmp_path / f"capture-{capture_fault}-install"
        second_root.mkdir(mode=0o700)
        second_source = _temporary_source_copy(second_root)
        second_entrypoint = second_source / "src/codex_usage/integration_snapshot.py"
        second_entrypoint.write_bytes(
            second_entrypoint.read_bytes() + b"\n# identity capture release\n"
        )
        second_temporary = second_root / "temporary"
        second_temporary.mkdir(mode=0o700)

        def run_operation():
            return integration_installer.install_release(
                source_root=second_source,
                state_home=state_home,
                data_home=data_home,
                python_executable=Path(sys.executable),
                temporary_root=second_temporary,
            )

    else:

        def run_operation():
            return integration_installer.rollback_active_release(
                state_home=state_home,
                data_home=data_home,
            )

    original_file_identity = integration_installer._file_identity
    fault_injected = False

    def fail_published_identity_capture(path):
        nonlocal fault_injected
        if (
            path == active_path
            and active_path.read_bytes() != active_before
            and not fault_injected
        ):
            fault_injected = True
            if capture_fault == "oserror":
                raise OSError("synthetic active identity capture failure")
            active_path.chmod(0o644)
        return original_file_identity(path)

    monkeypatch.setattr(
        integration_installer,
        "_file_identity",
        fail_published_identity_capture,
    )
    expected_error = (
        integration_installer.IntegrationInstallError
        if capture_fault == "oserror"
        else integration_installer.IntegrationCleanupError
    )
    with pytest.raises(expected_error):
        run_operation()

    assert fault_injected
    assert previous_path.read_bytes() == previous_before
    if capture_fault == "oserror":
        assert active_path.read_bytes() == active_before
        assert stat.S_IMODE(active_path.stat().st_mode) == 0o600
        assert verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.entrypoint_path,
        ) == release
    else:
        assert active_path.read_bytes() != active_before
        assert stat.S_IMODE(active_path.stat().st_mode) == 0o644


@pytest.mark.parametrize("operation", ["install"])
def test_post_swap_active_inode_replacement_is_preserved_as_cleanup_evidence(
    tmp_path, monkeypatch, operation
):
    from codex_usage import integration_installer
    from codex_usage.integration_attestation import IntegrationAttestationUnavailable
    from codex_usage.private_io import write_private_text

    _, data_home, state_home = _install(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    previous_path = integration / "previous.json"
    active_before = active_path.read_bytes()
    write_private_text(
        previous_path,
        active_before.decode("utf-8") + "\n",
        label="test previous manifest",
        mode=0o600,
    )
    previous_before = previous_path.read_bytes()

    if operation == "install":
        second_root = tmp_path / "inode-race-install"
        second_root.mkdir(mode=0o700)
        second_source = _temporary_source_copy(second_root)
        second_entrypoint = second_source / "src/codex_usage/integration_snapshot.py"
        second_entrypoint.write_bytes(
            second_entrypoint.read_bytes() + b"\n# inode race release\n"
        )
        second_temporary = second_root / "temporary"
        second_temporary.mkdir(mode=0o700)

        def run_operation():
            return integration_installer.install_release(
                source_root=second_source,
                state_home=state_home,
                data_home=data_home,
                python_executable=Path(sys.executable),
                temporary_root=second_temporary,
            )

    else:

        def run_operation():
            return integration_installer.rollback_active_release(
                state_home=state_home,
                data_home=data_home,
            )

    original_verify = integration_installer._verify_manifest
    replacement_injected = False
    published_inode: tuple[int, int] | None = None
    replacement_inode: tuple[int, int] | None = None
    raced_active: str | None = None

    def replace_active_after_identity_capture(*args, **kwargs):
        nonlocal replacement_injected, published_inode, replacement_inode, raced_active
        if (
            kwargs["manifest_path"] == active_path
            and active_path.read_bytes() != active_before
            and not replacement_injected
        ):
            replacement_injected = True
            raced_active = active_path.read_text(encoding="utf-8")
            published_stat = active_path.stat()
            published_inode = (published_stat.st_dev, published_stat.st_ino)
            write_private_text(
                active_path,
                raced_active,
                label="synthetic raced active manifest",
                mode=0o600,
            )
            replacement_stat = active_path.stat()
            replacement_inode = (replacement_stat.st_dev, replacement_stat.st_ino)
            raise IntegrationAttestationUnavailable()
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(
        integration_installer,
        "_verify_manifest",
        replace_active_after_identity_capture,
    )
    with pytest.raises(integration_installer.IntegrationCleanupError):
        run_operation()

    assert replacement_injected
    assert published_inode is not None
    assert replacement_inode is not None
    assert replacement_inode != published_inode
    final_stat = active_path.stat()
    assert (final_stat.st_dev, final_stat.st_ino) == replacement_inode
    assert raced_active is not None
    assert active_path.read_text(encoding="utf-8") == raced_active
    assert previous_path.read_bytes() == previous_before


@pytest.mark.parametrize("operation", ["install"])
def test_restore_boundary_inode_replacement_is_never_overwritten(
    tmp_path, monkeypatch, operation
):
    from codex_usage import integration_installer
    from codex_usage.integration_attestation import IntegrationAttestationUnavailable
    from codex_usage.private_io import write_private_text

    _, data_home, state_home = _install(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    previous_path = integration / "previous.json"
    active_before = active_path.read_bytes()
    write_private_text(
        previous_path,
        active_before.decode("utf-8") + "\n",
        label="test previous manifest",
        mode=0o600,
    )
    previous_before = previous_path.read_bytes()

    if operation == "install":
        second_root = tmp_path / "restore-boundary-install"
        second_root.mkdir(mode=0o700)
        second_source = _temporary_source_copy(second_root)
        second_entrypoint = second_source / "src/codex_usage/integration_snapshot.py"
        second_entrypoint.write_bytes(
            second_entrypoint.read_bytes() + b"\n# restore boundary release\n"
        )
        second_temporary = second_root / "temporary"
        second_temporary.mkdir(mode=0o700)

        def run_operation():
            return integration_installer.install_release(
                source_root=second_source,
                state_home=state_home,
                data_home=data_home,
                python_executable=Path(sys.executable),
                temporary_root=second_temporary,
            )

    else:

        def run_operation():
            return integration_installer.rollback_active_release(
                state_home=state_home,
                data_home=data_home,
            )

    original_verify = integration_installer._verify_manifest
    original_matches = integration_installer._provisional_matches
    raced_active = '{"synthetic_restore_boundary_race":true}\n'
    replacement_inode: tuple[int, int] | None = None
    attestation_failed = False
    replacement_injected = False

    def fail_final_active_attestation(*args, **kwargs):
        nonlocal attestation_failed
        if (
            kwargs["manifest_path"] == active_path
            and active_path.read_bytes() != active_before
            and not attestation_failed
        ):
            attestation_failed = True
            raise IntegrationAttestationUnavailable()
        return original_verify(*args, **kwargs)

    def replace_immediately_after_restore_predicate(
        path, identity, parent_identity, *, directory
    ):
        nonlocal replacement_injected, replacement_inode
        result = original_matches(
            path,
            identity,
            parent_identity,
            directory=directory,
        )
        if (
            path == active_path
            and result
            and attestation_failed
            and not replacement_injected
        ):
            replacement_injected = True
            write_private_text(
                active_path,
                raced_active,
                label="synthetic restore-boundary active manifest",
                mode=0o600,
            )
            replacement_stat = active_path.stat()
            replacement_inode = (replacement_stat.st_dev, replacement_stat.st_ino)
        return result

    monkeypatch.setattr(
        integration_installer,
        "_verify_manifest",
        fail_final_active_attestation,
    )
    monkeypatch.setattr(
        integration_installer,
        "_provisional_matches",
        replace_immediately_after_restore_predicate,
    )
    with pytest.raises(integration_installer.IntegrationCleanupError):
        run_operation()

    assert attestation_failed
    assert replacement_injected
    assert replacement_inode is not None
    final_stat = active_path.stat()
    assert (final_stat.st_dev, final_stat.st_ino) == replacement_inode
    assert active_path.read_text(encoding="utf-8") == raced_active
    assert previous_path.read_bytes() == previous_before


@pytest.mark.parametrize("operation", ["install"])
def test_capture_failure_never_adopts_byte_identical_replacement(
    tmp_path, monkeypatch, operation
):
    from codex_usage import integration_installer
    from codex_usage.private_io import write_private_text

    _, data_home, state_home = _install(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    previous_path = integration / "previous.json"
    active_before = active_path.read_bytes()
    write_private_text(
        previous_path,
        active_before.decode("utf-8") + "\n",
        label="test previous manifest",
        mode=0o600,
    )
    previous_before = previous_path.read_bytes()

    if operation == "install":
        second_root = tmp_path / "identical-capture-race-install"
        second_root.mkdir(mode=0o700)
        second_source = _temporary_source_copy(second_root)
        second_entrypoint = second_source / "src/codex_usage/integration_snapshot.py"
        second_entrypoint.write_bytes(
            second_entrypoint.read_bytes() + b"\n# identical capture race release\n"
        )
        second_temporary = second_root / "temporary"
        second_temporary.mkdir(mode=0o700)

        def run_operation():
            return integration_installer.install_release(
                source_root=second_source,
                state_home=state_home,
                data_home=data_home,
                python_executable=Path(sys.executable),
                temporary_root=second_temporary,
            )

    else:

        def run_operation():
            return integration_installer.rollback_active_release(
                state_home=state_home,
                data_home=data_home,
            )

    original_file_identity = integration_installer._file_identity
    replacement_inode: tuple[int, int] | None = None
    replacement_text: str | None = None
    replacement_injected = False

    def replace_with_identical_bytes_during_capture(path):
        nonlocal replacement_injected, replacement_inode, replacement_text
        if (
            path == active_path
            and active_path.read_bytes() != active_before
            and not replacement_injected
        ):
            replacement_injected = True
            replacement_text = active_path.read_text(encoding="utf-8")
            write_private_text(
                active_path,
                replacement_text,
                label="synthetic byte-identical active replacement",
                mode=0o600,
            )
            replacement_stat = active_path.stat()
            replacement_inode = (replacement_stat.st_dev, replacement_stat.st_ino)
            raise OSError("synthetic capture failure after identical replacement")
        return original_file_identity(path)

    monkeypatch.setattr(
        integration_installer,
        "_file_identity",
        replace_with_identical_bytes_during_capture,
    )
    with pytest.raises(integration_installer.IntegrationCleanupError):
        run_operation()

    assert replacement_injected
    assert replacement_inode is not None
    final_stat = active_path.stat()
    assert (final_stat.st_dev, final_stat.st_ino) == replacement_inode
    assert replacement_text is not None
    assert active_path.read_text(encoding="utf-8") == replacement_text
    assert previous_path.read_bytes() == previous_before


def test_rename_exchange_swaps_existing_entries(tmp_path):
    from codex_usage import integration_installer

    parent = tmp_path / "integration"
    parent.mkdir(mode=0o700)
    left = parent / "left"
    right = parent / "right"
    _write_active_transaction_artifact(left, b"left")
    _write_active_transaction_artifact(right, b"right")
    exchange = getattr(integration_installer, "_rename_exchange", None)
    assert exchange is not None
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        exchange(left.name, right.name, parent_fd)
    finally:
        os.close(parent_fd)

    assert left.read_bytes() == b"right"
    assert right.read_bytes() == b"left"


@pytest.mark.parametrize("operation", ["install"])
def test_second_exchange_failure_keeps_active_and_prior_evidence(
    tmp_path, monkeypatch, operation
):
    from codex_usage import integration_installer
    from codex_usage.integration_attestation import IntegrationAttestationUnavailable

    prepared = _prepared_active_transaction(tmp_path, operation)
    original_verify = integration_installer._verify_manifest
    attestation_failed = False

    def fail_final_active_attestation(*args, **kwargs):
        nonlocal attestation_failed
        if (
            kwargs["manifest_path"] == prepared.active_path
            and prepared.active_path.read_bytes() != prepared.active_before
            and not attestation_failed
        ):
            attestation_failed = True
            raise IntegrationAttestationUnavailable()
        return original_verify(*args, **kwargs)

    original_exchange = getattr(integration_installer, "_rename_exchange", None)
    exchange_calls = 0

    def fail_second_exchange(source_name, target_name, parent_fd):
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 2:
            raise OSError(errno.EIO, "synthetic second exchange failure")
        assert original_exchange is not None
        return original_exchange(source_name, target_name, parent_fd)

    monkeypatch.setattr(
        integration_installer,
        "_verify_manifest",
        fail_final_active_attestation,
    )
    monkeypatch.setattr(
        integration_installer,
        "_rename_exchange",
        fail_second_exchange,
        raising=False,
    )
    with pytest.raises(integration_installer.IntegrationCleanupError):
        prepared.run()

    assert attestation_failed
    assert exchange_calls == 2
    assert prepared.active_path.is_file() and not prepared.active_path.is_symlink()
    assert prepared.previous_path.read_bytes() == prepared.previous_before
    evidence = [
        path
        for path in prepared.integration.iterdir()
        if path.name.startswith(".active.json.publish-")
    ]
    assert len(evidence) == 1
    assert evidence[0].read_bytes() == prepared.active_before


@pytest.mark.parametrize("operation", ["install"])
def test_publish_exchange_fsync_failure_atomically_restores_active(
    tmp_path, monkeypatch, operation
):
    from codex_usage import integration_installer

    prepared = _prepared_active_transaction(tmp_path, operation)
    integration_stat = prepared.integration.stat()
    original_exchange = getattr(integration_installer, "_rename_exchange", None)
    original_fsync = integration_installer.os.fsync
    exchange_calls = 0
    fsync_failed = False

    def count_exchange(source_name, target_name, parent_fd):
        nonlocal exchange_calls
        exchange_calls += 1
        assert original_exchange is not None
        return original_exchange(source_name, target_name, parent_fd)

    def fail_first_post_exchange_fsync(fd):
        nonlocal fsync_failed
        item = os.fstat(fd)
        if (
            exchange_calls == 1
            and not fsync_failed
            and stat.S_ISDIR(item.st_mode)
            and (item.st_dev, item.st_ino)
            == (integration_stat.st_dev, integration_stat.st_ino)
        ):
            fsync_failed = True
            raise OSError(errno.EIO, "synthetic publish fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(
        integration_installer,
        "_rename_exchange",
        count_exchange,
        raising=False,
    )
    monkeypatch.setattr(integration_installer.os, "fsync", fail_first_post_exchange_fsync)
    with pytest.raises(integration_installer.IntegrationInstallError):
        prepared.run()

    assert fsync_failed
    assert exchange_calls == 2
    assert prepared.active_path.read_bytes() == prepared.active_before
    assert prepared.previous_path.read_bytes() == prepared.previous_before


@pytest.mark.parametrize("operation", ["install"])
def test_install_and_rollback_close_active_publish_after_baseexception(
    tmp_path,
    monkeypatch,
    operation,
):
    """Would fail if BaseException left active.json swapped with transaction evidence."""
    from codex_usage import integration_installer

    prepared = _prepared_active_transaction(tmp_path, operation)
    interrupted = False

    def interrupt_active_validation(**_kwargs) -> None:
        nonlocal interrupted
        interrupted = True
        raise KeyboardInterrupt("active publish validation interrupted")

    monkeypatch.setattr(
        integration_installer,
        "_validate_active_publish",
        interrupt_active_validation,
    )

    with pytest.raises(KeyboardInterrupt, match="active publish validation interrupted"):
        prepared.run()

    assert interrupted
    assert prepared.active_path.read_bytes() == prepared.active_before
    assert prepared.previous_path.read_bytes() == prepared.previous_before
    assert not [
        path
        for path in prepared.integration.iterdir()
        if path.name.startswith(".active.json.publish-")
    ]


@pytest.mark.parametrize("transaction_kind", ["install", "rollback"])
def test_begin_active_publish_rolls_back_exchange_after_baseexception(
    tmp_path,
    monkeypatch,
    transaction_kind,
):
    """Would fail if BaseException inside the active swap left transaction debris."""
    from codex_usage import integration_installer

    prepared = _prepared_active_transaction(tmp_path, transaction_kind)
    integration_identity = integration_installer._directory_identity(
        prepared.integration
    )
    prior_identity = integration_installer._provisional_from_stat(
        prepared.active_path.stat()
    )
    original_entry_matches = integration_installer._entry_matches_at
    interrupted = False

    def interrupt_after_active_swap(parent_fd, name, identity):
        nonlocal interrupted
        matched = original_entry_matches(parent_fd, name, identity)
        if (
            name == prepared.active_path.name
            and matched
            and prepared.active_path.read_bytes() != prepared.active_before
        ):
            interrupted = True
            raise KeyboardInterrupt("active swap interrupted")
        return matched

    monkeypatch.setattr(
        integration_installer,
        "_entry_matches_at",
        interrupt_after_active_swap,
    )

    with pytest.raises(KeyboardInterrupt, match="active swap interrupted"):
        integration_installer._begin_active_publish(
            active_path=prepared.active_path,
            published_text=prepared.active_before.decode("utf-8") + "\n",
            prior_identity=prior_identity,
            integration_identity=integration_identity,
            transaction_kind=transaction_kind,
        )

    assert interrupted
    assert prepared.active_path.read_bytes() == prepared.active_before
    assert not [
        path
        for path in prepared.integration.iterdir()
        if path.name.startswith(".active.json.publish-")
    ]


@pytest.mark.parametrize("operation", ["install"])
def test_commit_cleanup_failure_returns_success_with_bounded_evidence(
    tmp_path, monkeypatch, operation
):
    from codex_usage import integration_installer

    prepared = _prepared_active_transaction(tmp_path, operation)
    original_cleanup = integration_installer._cleanup_provisional
    cleanup_failed = False

    def retain_transaction_evidence(path, identity, parent_identity, *, directory):
        nonlocal cleanup_failed
        if (
            path.parent == prepared.integration
            and path.name.startswith(
                (".active.json.publish-", ".active.json.prior-")
            )
        ):
            cleanup_failed = True
            return False
        return original_cleanup(
            path,
            identity,
            parent_identity,
            directory=directory,
        )

    monkeypatch.setattr(
        integration_installer,
        "_cleanup_provisional",
        retain_transaction_evidence,
    )
    result = prepared.run()

    assert result.version == "0.6.542"
    assert cleanup_failed
    assert prepared.active_path.is_file()
    # D297 never creates a public active-release rollback path.  A pre-existing
    # historical marker is not rewritten as an implicit compatibility route.
    assert prepared.previous_path.read_bytes() == prepared.previous_before
    evidence = [
        path
        for path in prepared.integration.iterdir()
        if path.name.startswith(
            (".active.json.publish-", ".active.json.prior-")
        )
    ]
    assert len(evidence) == 1


def test_next_operation_fails_closed_on_unproven_post_exchange_artifact(tmp_path):
    from codex_usage import integration_installer

    _, data_home, state_home = _install(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    previous_path = integration / "previous.json"
    active_before = active_path.read_bytes()
    active_stat = active_path.stat()
    integration_identity = integration_installer._directory_identity(integration)
    integration_installer._begin_active_publish(
        active_path=active_path,
        published_text=active_before.decode("utf-8") + "\n",
        prior_identity=integration_installer._provisional_from_stat(active_stat),
        integration_identity=integration_identity,
    )
    active_after_publish = active_path.read_bytes()
    artifacts = [
        path
        for path in integration.iterdir()
        if path.name.startswith(".active.json.publish-")
    ]
    assert active_after_publish != active_before
    assert not previous_path.exists()
    assert len(artifacts) == 1
    artifact = artifacts[0]
    artifact_before = artifact.read_bytes()
    artifact_stat = artifact.stat()

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.rollback_active_release(
            state_home=state_home,
            data_home=data_home,
        )

    assert active_path.read_bytes() == active_after_publish
    assert not previous_path.exists()
    assert artifact.read_bytes() == artifact_before
    current_artifact = artifact.stat()
    assert (current_artifact.st_dev, current_artifact.st_ino) == (
        artifact_stat.st_dev,
        artifact_stat.st_ino,
    )


def test_startup_recovery_rejects_eight_unproven_publish_artifacts(tmp_path):
    from codex_usage import integration_installer

    prepared = _prepared_active_transaction(tmp_path, "rollback")
    artifacts = []
    for index in range(8):
        artifact = _write_bound_stale_transaction_artifact(
            integration=prepared.integration,
            active_path=prepared.active_path,
            index=index,
        )
        artifacts.append(artifact)

    with pytest.raises(integration_installer.IntegrationInstallError):
        prepared.run()

    assert prepared.active_path.read_bytes() == prepared.active_before
    assert prepared.previous_path.read_bytes() == prepared.previous_before
    assert all(artifact.read_bytes() == b"stale" for artifact in artifacts)


@pytest.mark.parametrize("transaction_kind", ["install", "rollback"])
@pytest.mark.parametrize("payload_kind", ["foreign", "copied-valid-manifest"])
def test_startup_recovery_preserves_same_owner_forged_bound_artifact(
    tmp_path,
    transaction_kind,
    payload_kind,
):
    from codex_usage import integration_installer

    prepared = _prepared_active_transaction(tmp_path, "rollback")
    payload = (
        b"foreign transaction evidence"
        if payload_kind == "foreign"
        else prepared.active_before
    )
    artifact, artifact_identity = _write_forged_bound_transaction_artifact(
        integration=prepared.integration,
        active_path=prepared.active_path,
        transaction_kind=transaction_kind,
        payload=payload,
    )
    forged_stat = artifact.stat()
    assert stat.S_ISREG(forged_stat.st_mode)
    assert stat.S_IMODE(forged_stat.st_mode) == 0o600
    assert forged_stat.st_uid == os.getuid()
    assert forged_stat.st_nlink == 1
    assert (forged_stat.st_dev, forged_stat.st_ino) == artifact_identity
    bounded_error = None

    try:
        prepared.run()
    except integration_installer.IntegrationInstallError as error:
        bounded_error = error

    assert artifact.exists(), "same-owner forged bound artifact was deleted"
    current_artifact = artifact.stat()
    assert (current_artifact.st_dev, current_artifact.st_ino) == artifact_identity
    assert artifact.read_bytes() == payload
    assert prepared.active_path.read_bytes() == prepared.active_before
    assert prepared.previous_path.read_bytes() == prepared.previous_before
    assert bounded_error is not None


def test_startup_recovery_rejects_ninth_artifact_without_deleting_evidence(
    tmp_path,
):
    from codex_usage import integration_installer

    prepared = _prepared_active_transaction(tmp_path, "rollback")
    artifacts = []
    for index in range(9):
        artifact = _write_bound_stale_transaction_artifact(
            integration=prepared.integration,
            active_path=prepared.active_path,
            index=100 + index,
        )
        artifacts.append(artifact)

    with pytest.raises(integration_installer.IntegrationInstallError):
        prepared.run()

    assert prepared.active_path.read_bytes() == prepared.active_before
    assert prepared.previous_path.read_bytes() == prepared.previous_before
    assert all(artifact.read_bytes() == b"stale" for artifact in artifacts)


@pytest.mark.parametrize(
    "artifact_shape",
    [
        "raw-valid",
        "mode",
        "hardlink",
        "oversize",
        "symlink",
        "directory",
        "legacy-publish",
        "legacy-prior",
        "legacy-failed",
    ],
)
def test_startup_recovery_preserves_ambiguous_or_foreign_artifact(
    tmp_path, artifact_shape
):
    from codex_usage import integration_installer

    prepared = _prepared_active_transaction(tmp_path, "rollback")
    if artifact_shape.startswith("legacy-"):
        label = artifact_shape.removeprefix("legacy-")
        artifact = prepared.integration / (
            f".active.json.{label}-3000-0123456789abcdef"
        )
        _write_active_transaction_artifact(artifact, prepared.active_before)
    else:
        artifact = prepared.integration / (
            ".active.json.publish-new-3000-0123456789abcdef"
        )
        if artifact_shape == "directory":
            artifact.mkdir(mode=0o700)
        elif artifact_shape == "symlink":
            foreign = prepared.integration / "foreign-artifact-target"
            _write_active_transaction_artifact(foreign, b"foreign")
            artifact.symlink_to(foreign.name)
        else:
            payload = b"x" * (128 * 1024 + 1) if artifact_shape == "oversize" else b"stale"
            _write_active_transaction_artifact(artifact, payload)
            if artifact_shape == "mode":
                artifact.chmod(0o640)
            elif artifact_shape == "hardlink":
                os.link(artifact, prepared.integration / "foreign-hardlink")

    with pytest.raises(integration_installer.IntegrationInstallError):
        prepared.run()

    assert prepared.active_path.read_bytes() == prepared.active_before
    assert prepared.previous_path.read_bytes() == prepared.previous_before
    assert artifact.exists() or artifact.is_symlink()


def test_startup_recovery_preserves_bound_artifact_identity(tmp_path):
    from codex_usage import integration_installer

    prepared = _prepared_active_transaction(tmp_path, "rollback")
    artifact = _write_bound_stale_transaction_artifact(
        integration=prepared.integration,
        active_path=prepared.active_path,
        index=200,
        payload=b"owned",
    )
    artifact_stat = artifact.stat()

    with pytest.raises(integration_installer.IntegrationInstallError):
        prepared.run()

    current = artifact.stat()
    assert (current.st_dev, current.st_ino) == (
        artifact_stat.st_dev,
        artifact_stat.st_ino,
    )
    assert artifact.read_bytes() == b"owned"
    assert prepared.active_path.read_bytes() == prepared.active_before
    assert prepared.previous_path.read_bytes() == prepared.previous_before


@pytest.mark.parametrize("operation", ["install"])
def test_failed_publish_to_initially_absent_active_keeps_active_present(
    tmp_path, monkeypatch, operation
):
    from codex_usage import integration_installer
    from codex_usage.integration_attestation import IntegrationAttestationUnavailable
    from codex_usage.private_io import write_private_text

    if operation == "install":
        data_home, state_home, temporary_root = _roots(tmp_path)
        initial_root = tmp_path / "initial-install"
        initial_root.mkdir(mode=0o700)
        source = _temporary_source_copy(initial_root)
        active_path = state_home / "codex-usage/integration/active.json"
        previous_path = state_home / "codex-usage/integration/previous.json"

        def run():
            return integration_installer.install_release(
                source_root=source,
                state_home=state_home,
                data_home=data_home,
                python_executable=Path(sys.executable),
                temporary_root=temporary_root,
            )

    else:
        _, data_home, state_home = _install(tmp_path)
        integration = state_home / "codex-usage/integration"
        active_path = integration / "active.json"
        previous_path = integration / "previous.json"
        active_text = active_path.read_text(encoding="utf-8")
        write_private_text(
            previous_path,
            active_text + "\n",
            label="test previous manifest",
            mode=0o600,
        )
        active_path.unlink()

        def run():
            return integration_installer.rollback_active_release(
                state_home=state_home,
                data_home=data_home,
            )

    assert not active_path.exists()
    original_verify = integration_installer._verify_manifest
    final_attestation_failed = False

    def fail_final_attestation(*args, **kwargs):
        nonlocal final_attestation_failed
        if kwargs["manifest_path"] == active_path:
            final_attestation_failed = True
            raise IntegrationAttestationUnavailable()
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(
        integration_installer,
        "_verify_manifest",
        fail_final_attestation,
    )
    with pytest.raises(integration_installer.IntegrationCleanupError):
        run()

    assert final_attestation_failed
    assert active_path.is_file() and not active_path.is_symlink()
    assert original_verify(
        manifest_path=active_path,
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=None,
    ).version == "0.6.542"
    if operation == "install":
        assert not previous_path.exists()


def test_install_cutover_rejects_schema1_upgrade_source(
    tmp_path,
    monkeypatch,
):
    from codex_usage.integration_attestation import (
        IntegrationAttestationUnavailable,
        verify_active_release,
    )
    from codex_usage.integration_installer import (
        IntegrationInstallError,
        install_release,
        rollback_active_release,
    )

    data_home, state_home, temporary_root = _roots(tmp_path)
    schema1_active = _write_synthetic_schema1_active(
        state_home=state_home,
        data_home=data_home,
    )
    schema1_entrypoint = Path(
        json.loads(schema1_active)["entrypoint_path"]
    )
    with pytest.raises(IntegrationAttestationUnavailable):
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=schema1_entrypoint,
        )

    def reject_recovery(**_kwargs):
        pytest.fail("D297 must reject a schema-1 active before any recovery")

    monkeypatch.setattr(
        sys.modules[install_release.__module__],
        "_recover_active_transactions",
        reject_recovery,
    )

    with pytest.raises(IntegrationInstallError):
        install_release(
            source_root=_temporary_source_copy(tmp_path),
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    assert active_path.read_bytes() == schema1_active
    assert not (integration / "previous.json").exists()
    assert not (integration / "current.json").exists()
    assert not (integration / "generations").exists()
    with pytest.raises(IntegrationInstallError):
        rollback_active_release(state_home=state_home, data_home=data_home)
    assert active_path.read_bytes() == schema1_active
    assert not (integration / "current.json").exists()
    assert not (integration / "generations").exists()


def test_install_cutover_rejects_attested_schema2_06533_upgrade_source(
    tmp_path,
    monkeypatch,
):
    from codex_usage import integration_attestation, integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    with monkeypatch.context() as old_release_context:
        _patch_release_identity(
            old_release_context,
            "0.6.533",
            source_root=source_root,
        )
        old_release = integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )

    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    old_active = active_path.read_bytes()
    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation.verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=old_release.entrypoint_path,
        )

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    assert active_path.read_bytes() == old_active
    assert not (integration / "previous.json").exists()
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.rollback_active_release(
            state_home=state_home,
            data_home=data_home,
        )
    assert active_path.read_bytes() == old_active


@pytest.mark.parametrize("mutation", ["unknown", "secret-like", "missing"])
def test_legacy_upgrade_manifest_requires_exact_canonical_fields(
    tmp_path,
    mutation,
):
    from codex_usage.integration_installer import IntegrationInstallError, install_release

    data_home, state_home, temporary_root = _roots(tmp_path)
    _write_synthetic_schema1_active(state_home=state_home, data_home=data_home)
    active_path = state_home / "codex-usage" / "integration" / "active.json"
    manifest = json.loads(active_path.read_text(encoding="utf-8"))
    mutated = _mutate_manifest_fields(manifest, mutation)
    active_path.write_text(
        json.dumps(mutated, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    before = active_path.read_bytes()

    with pytest.raises(IntegrationInstallError):
        install_release(
            source_root=_temporary_source_copy(tmp_path),
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    assert active_path.read_bytes() == before


def test_public_rollback_rejects_prior_manifest_without_mutation(tmp_path):
    from codex_usage.integration_installer import (
        IntegrationInstallError,
        rollback_active_release,
    )
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
    active_before = active_path.read_bytes()
    previous_path = integration / "previous.json"
    previous_before = previous_path.read_bytes()

    with pytest.raises(IntegrationInstallError):
        rollback_active_release(state_home=state_home, data_home=data_home)

    assert active_path.read_bytes() == active_before
    assert previous_path.read_bytes() == previous_before
    assert first.release_dir.is_dir()


@pytest.mark.parametrize("missing", ["producer-lock", "lock-root"])
def test_rollback_missing_lock_namespace_fails_without_recreating_or_mutating(
    tmp_path, monkeypatch, missing
):
    from codex_usage import integration_evidence, integration_installer, private_io
    from codex_usage.private_io import write_private_text

    lock_root = tmp_path / "installer-lock-root"
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)
    _, data_home, state_home = _install(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    previous_path = integration / "previous.json"
    active_before = active_path.read_bytes()
    write_private_text(
        previous_path,
        active_before.decode("utf-8"),
        label="synthetic previous manifest",
        mode=0o600,
    )
    previous_before = previous_path.read_bytes()
    producer_lock = lock_root / integration_evidence._evidence_lock_name(
        integration / "producer-install"
    )
    if missing == "producer-lock":
        producer_lock.unlink()
        missing_path = producer_lock
    else:
        missing_path = tmp_path / "missing-lock-root"
        monkeypatch.setattr(private_io, "_private_lock_root", lambda: missing_path)

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.rollback_active_release(
            state_home=state_home,
            data_home=data_home,
        )

    assert not missing_path.exists()
    assert active_path.read_bytes() == active_before
    assert previous_path.read_bytes() == previous_before


def test_rollback_rejects_schema1_previous_without_changing_schema2_active(tmp_path):
    from codex_usage.integration_attestation import verify_active_release
    from codex_usage.integration_installer import (
        IntegrationInstallError,
        rollback_active_release,
    )
    from codex_usage.private_io import write_private_text

    release, data_home, state_home = _install(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    active_before = active_path.read_bytes()
    previous = json.loads(active_before)
    assert previous["schema_version"] == 2
    previous["schema_version"] = 1
    write_private_text(
        integration / "previous.json",
        json.dumps(previous, sort_keys=True, separators=(",", ":")) + "\n",
        label="synthetic schema-1 previous manifest",
        mode=0o600,
    )

    with pytest.raises(IntegrationInstallError):
        rollback_active_release(state_home=state_home, data_home=data_home)

    assert active_path.read_bytes() == active_before
    assert (
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.entrypoint_path,
        )
        == release
    )


@pytest.mark.parametrize("mutation", ["unknown", "secret-like", "missing"])
def test_rollback_previous_manifest_requires_exact_canonical_fields(
    tmp_path,
    mutation,
):
    from codex_usage.integration_installer import (
        IntegrationInstallError,
        rollback_active_release,
    )
    from codex_usage.private_io import write_private_text

    _, data_home, state_home = _install(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    active_path = integration / "active.json"
    manifest = json.loads(active_path.read_text(encoding="utf-8"))
    mutated = _mutate_manifest_fields(manifest, mutation)
    write_private_text(
        integration / "previous.json",
        json.dumps(mutated, sort_keys=True, separators=(",", ":")) + "\n",
        label="mutated previous manifest",
        mode=0o600,
    )
    active_before = active_path.read_bytes()

    with pytest.raises(IntegrationInstallError):
        rollback_active_release(state_home=state_home, data_home=data_home)
    assert active_path.read_bytes() == active_before


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
    assert "PYTHONDONTWRITEBYTECODE=1" in launcher
    assert str(data_home) in launcher
    assert str(state_home) in launcher
    assert "codex_usage.cli" not in launcher
    assert "PYTHONPATH" in launcher
    assert launcher.splitlines()[0] == "#!/bin/sh"


def test_attestation_requires_launcher_bytecode_environment(tmp_path):
    from codex_usage import integration_attestation
    from codex_usage.private_io import write_private_text

    release, data_home, state_home = _install(tmp_path)
    launcher = release.launcher_path
    launcher.write_text(
        launcher.read_text(encoding="utf-8").replace(
            " PYTHONDONTWRITEBYTECODE=1",
            "",
        ),
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    active_path = state_home / "codex-usage" / "integration" / "active.json"
    manifest = json.loads(active_path.read_text(encoding="utf-8"))
    manifest["launcher_sha256"] = hashlib.sha256(launcher.read_bytes()).hexdigest()
    manifest["release_tree_sha256"] = integration_attestation._release_tree_sha256(
        release_dir=release.release_dir
    )
    write_private_text(
        active_path,
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        label="launcher without bytecode environment",
        mode=0o600,
    )

    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation.verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.entrypoint_path,
        )


def test_install_release_restores_umask_when_venv_create_fails(tmp_path, monkeypatch):
    """Would fail if isolated venv builder failure leaked the parent umask."""
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    real_run_builder_bounded = integration_installer._run_builder_bounded

    def fail_venv_builder(command, **kwargs):
        if "venv.EnvBuilder" not in command[3]:
            return real_run_builder_bounded(command, **kwargs)
        assert command[0].startswith("/proc/self/fd/")
        assert int(command[0].rsplit("/", 1)[1]) in kwargs["pass_fds"]
        assert command[1] == "-I"
        assert "os.umask(0o077)" in command[3]
        assert kwargs["cwd"] == Path(command[-1]).parent
        return subprocess.CompletedProcess(command, 1)

    previous = os.umask(0o027)
    os.umask(previous)
    monkeypatch.setattr(
        integration_installer,
        "_run_builder_bounded",
        fail_venv_builder,
    )
    try:
        with pytest.raises(integration_installer.IntegrationInstallError):
            integration_installer.install_release(
                source_root=_temporary_source_copy(tmp_path),
                state_home=state_home,
                data_home=data_home,
                python_executable=Path(sys.executable),
                temporary_root=temporary_root,
            )
        restored = os.umask(0o022)
        os.umask(restored)
    finally:
        os.umask(previous)

    assert restored == previous


def test_private_virtualenv_uses_isolated_child_umask_contract(tmp_path, monkeypatch):
    """Would fail if venv creation still changed umask in the parent process."""
    from codex_usage import integration_installer

    venv_root = tmp_path / "venv"
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def create_venv_in_child(command, **kwargs):
        calls.append((tuple(command), kwargs))
        assert command[0].startswith("/proc/self/fd/")
        assert int(command[0].rsplit("/", 1)[1]) in kwargs["pass_fds"]
        Path(command[-1]).mkdir(mode=0o700)
        return subprocess.CompletedProcess(command, 0)

    previous = os.umask(0o027)
    os.umask(previous)
    monkeypatch.setattr(
        integration_installer,
        "_run_builder_bounded",
        create_venv_in_child,
    )
    try:
        integration_installer._create_private_virtualenv(venv_root)
        restored = os.umask(0o022)
        os.umask(restored)
    finally:
        os.umask(previous)

    assert restored == previous
    assert venv_root.is_dir()
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[0].startswith("/proc/self/fd/")
    assert command[1] == "-I"
    assert command[2] == "-c"
    assert "os.umask(0o077)" in command[3]
    assert command[-1] == str(venv_root)
    assert kwargs["env"] == integration_installer._sanitized_build_environment()
    assert kwargs["cwd"] == venv_root.parent
    assert kwargs["timeout_seconds"] == integration_installer.BUILDER_VENV_TIMEOUT_SECONDS


def test_private_virtualenv_propagates_builder_cleanup_error(tmp_path, monkeypatch):
    """Would fail if the virtualenv wrapper degraded a builder cleanup error."""
    from codex_usage import integration_installer

    cleanup_error = integration_installer.IntegrationCleanupError(
        "synthetic virtualenv builder cleanup failure"
    )

    def fail_builder(*_args, **_kwargs):
        raise cleanup_error

    monkeypatch.setattr(integration_installer, "_run_builder_bounded", fail_builder)

    with pytest.raises(
        integration_installer.IntegrationCleanupError,
        match="synthetic virtualenv builder cleanup failure",
    ) as exc_info:
        integration_installer._create_private_virtualenv(tmp_path / "venv")

    assert exc_info.value is cleanup_error


def test_private_virtualenv_umask_is_process_isolated_from_foreign_thread(
    tmp_path,
    monkeypatch,
):
    """Would fail if venv creation changed the parent process umask."""
    import threading

    from codex_usage import integration_installer

    venv_root = tmp_path / "venv"
    builder_active = tmp_path / "builder-active"
    builder_release = tmp_path / "builder-release"

    def blocking_child_builder(command, **kwargs):
        assert command[0].startswith("/proc/self/fd/")
        assert int(command[0].rsplit("/", 1)[1]) in kwargs["pass_fds"]
        builder_active.write_text("active", encoding="utf-8")
        deadline = time.monotonic() + 5
        while not builder_release.exists():
            if time.monotonic() > deadline:
                return subprocess.CompletedProcess(command, 1)
            time.sleep(0.01)
        Path(command[-1]).mkdir(mode=0o700)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(
        integration_installer,
        "_run_builder_bounded",
        blocking_child_builder,
    )
    previous = os.umask(0o027)
    os.umask(previous)
    errors: list[BaseException] = []

    def create_virtualenv() -> None:
        try:
            integration_installer._create_private_virtualenv(venv_root)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=create_virtualenv)
    try:
        worker.start()
        deadline = time.monotonic() + 5
        while not builder_active.exists():
            if time.monotonic() > deadline:
                raise TimeoutError("synthetic venv builder did not start")
            time.sleep(0.01)
        observed = os.umask(0o022)
        os.umask(observed)
        builder_release.write_text("release", encoding="utf-8")
        worker.join(timeout=5)
    finally:
        builder_release.write_text("release", encoding="utf-8")
        worker.join(timeout=5)
        os.umask(previous)

    assert not worker.is_alive()
    assert errors == []
    assert observed == previous
    assert venv_root.is_dir()


def test_launcher_rejects_schema1_active_manifest_without_repair(tmp_path):
    from codex_usage.private_io import write_private_text

    release, data_home, state_home = _install(tmp_path)
    _write_launcher_state(data_home, state_home)
    active_path = state_home / "codex-usage" / "integration" / "active.json"
    manifest = json.loads(active_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    manifest["schema_version"] = 1
    write_private_text(
        active_path,
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        label="synthetic schema-1 active manifest",
        mode=0o600,
    )
    before = active_path.read_bytes()

    completed = subprocess.run(
        [
            str(release.launcher_path),
            "integration-snapshot",
            "--schema",
            "2",
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

    assert completed.returncode == 69
    assert completed.stdout == ""
    assert completed.stderr == "integration_snapshot_unavailable\n"
    assert active_path.read_bytes() == before


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


@pytest.mark.parametrize(
    ("field", "relative", "mode"),
    [
        ("launcher_path", Path("venv/bin/alternate-launcher"), 0o700),
        ("wheel_path", Path("alternate-producer.whl"), 0o600),
    ],
)
def test_attestation_rejects_manifest_path_drift_even_when_hashes_match(
    tmp_path,
    field,
    relative,
    mode,
):
    from codex_usage import integration_attestation
    from codex_usage.integration_attestation import (
        IntegrationAttestationUnavailable,
        verify_active_release,
    )
    from codex_usage.private_io import write_private_text

    release, data_home, state_home = _install(tmp_path)
    active_path = state_home / "codex-usage" / "integration" / "active.json"
    manifest = json.loads(active_path.read_text(encoding="utf-8"))
    original_path = Path(manifest[field])
    alternate = release.release_dir / relative
    alternate.write_bytes(original_path.read_bytes())
    alternate.chmod(mode)
    manifest[field] = str(alternate)
    manifest["release_tree_sha256"] = integration_attestation._release_tree_sha256(
        release_dir=release.release_dir
    )
    write_private_text(
        active_path,
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        label="mutated active path manifest",
        mode=0o600,
    )
    before = active_path.read_bytes()

    with pytest.raises(IntegrationAttestationUnavailable):
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.entrypoint_path,
        )
    assert active_path.read_bytes() == before


def test_attestation_rejects_entrypoint_and_dist_info_outside_canonical_site_packages(
    tmp_path,
):
    from codex_usage import integration_attestation
    from codex_usage.integration_attestation import (
        IntegrationAttestationUnavailable,
        verify_active_release,
    )
    from codex_usage.private_io import write_private_text

    release, data_home, state_home = _install(tmp_path)
    active_path = state_home / "codex-usage" / "integration" / "active.json"
    manifest = json.loads(active_path.read_text(encoding="utf-8"))
    original_record = Path(manifest["record_path"])
    original_site_packages = original_record.parent.parent
    alternate_site_packages = release.release_dir / "venv/alternate/site-packages"
    shutil.copytree(original_site_packages, alternate_site_packages)
    alternate_entrypoint = alternate_site_packages / "codex_usage/integration_entrypoint.py"
    alternate_record = alternate_site_packages / original_record.parent.name / "RECORD"
    manifest["entrypoint_path"] = str(alternate_entrypoint)
    manifest["record_path"] = str(alternate_record)
    manifest["release_tree_sha256"] = integration_attestation._release_tree_sha256(
        release_dir=release.release_dir
    )
    write_private_text(
        active_path,
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        label="mutated entrypoint and dist-info manifest",
        mode=0o600,
    )
    before = active_path.read_bytes()

    with pytest.raises(IntegrationAttestationUnavailable):
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=alternate_entrypoint,
        )
    assert active_path.read_bytes() == before


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
    assert str(observed["argv"][0]).startswith("/proc/self/fd/")
    assert observed["argv"][1:3] == ("-B", "-I")
    assert "--no-index" not in observed["argv"]
    assert observed["env"]["PIP_NO_INDEX"] == "1"
    assert observed["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "PYTHONPATH" not in observed["env"]
    assert observed["timeout"] == 120
    assert observed["stdout"] == subprocess.DEVNULL
    assert observed["stderr"] == subprocess.DEVNULL
    assert observed["cwd"] == tmp_path
    assert observed["start_new_session"] is True
    assert killed == [4321]


def test_verified_wheel_propagates_builder_cleanup_error(tmp_path, monkeypatch):
    """Would fail if the wheel wrapper degraded a builder cleanup error."""
    from codex_usage import integration_installer

    build_root = tmp_path / "build"
    wheel_dir = tmp_path / "wheel"
    build_root.mkdir(mode=0o700)
    wheel_dir.mkdir(mode=0o700)
    cleanup_error = integration_installer.IntegrationCleanupError(
        "synthetic wheel builder cleanup failure"
    )

    def fail_builder(*_args, **_kwargs):
        raise cleanup_error

    monkeypatch.setattr(
        integration_installer,
        "_require_offline_builder",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(integration_installer, "_run_builder_bounded", fail_builder)

    with pytest.raises(
        integration_installer.IntegrationCleanupError,
        match="synthetic wheel builder cleanup failure",
    ) as exc_info:
        integration_installer._build_verified_wheel(
            python_executable=Path(sys.executable),
            environment=integration_installer._sanitized_build_environment(),
            build_root=build_root,
            wheel_dir=wheel_dir,
        )

    assert exc_info.value is cleanup_error


def test_installer_preflight_cleanup_rejects_boolean_pid(monkeypatch):
    from codex_usage import integration_installer

    calls = []

    class FakeProcess:
        pid = True

        def kill(self):
            calls.append("kill")

        def wait(self, timeout=None):
            calls.append(("wait", timeout))

    monkeypatch.setattr(
        integration_installer.os,
        "killpg",
        lambda pid, signum: calls.append(("killpg", pid, signum)),
    )

    integration_installer._terminate_preflight_process(FakeProcess())

    assert calls == ["kill", ("wait", 1)]


def test_installer_preflight_cleanup_rejects_numeric_subclass_pid(monkeypatch):
    from codex_usage import integration_installer

    calls = []

    class FakeProcess:
        pid = _BrokenInt(4321)

        def kill(self):
            calls.append("kill")

        def wait(self, timeout=None):
            calls.append(("wait", timeout))

    monkeypatch.setattr(
        integration_installer.os,
        "killpg",
        lambda pid, signum: calls.append(("killpg", pid, signum)),
    )

    integration_installer._terminate_preflight_process(FakeProcess())

    assert calls == ["kill", ("wait", 1)]


def test_installer_builder_rejects_boolean_process_pid(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    calls = []

    class FakeProcess:
        pid = True

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(
        integration_installer.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        integration_installer.os,
        "getpgid",
        lambda pid: calls.append(("getpgid", pid)) or 4321,
    )
    monkeypatch.setattr(
        integration_installer,
        "_kill_process_group",
        lambda process_group_id: calls.append(("killpg", process_group_id)),
    )

    result = integration_installer._run_builder_bounded(
        ["builder"], env={}, cwd=tmp_path
    )

    assert result.returncode == 0
    assert calls == [("wait", integration_installer.BUILDER_WHEEL_TIMEOUT_SECONDS)]


def test_installer_builder_rejects_numeric_subclass_process_pid(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    calls = []

    class FakeProcess:
        pid = _BrokenInt(4321)

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(
        integration_installer.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        integration_installer.os,
        "getpgid",
        lambda pid: pytest.fail("numeric subclass PID must not reach getpgid"),
    )

    result = integration_installer._run_builder_bounded(
        ["builder"], env={}, cwd=tmp_path
    )

    assert result.returncode == 0
    assert calls == [("wait", integration_installer.BUILDER_WHEEL_TIMEOUT_SECONDS)]


def test_installer_group_cleanup_rejects_boolean_id(monkeypatch):
    from codex_usage import integration_installer

    calls = []
    monkeypatch.setattr(
        integration_installer.os,
        "killpg",
        lambda process_group_id, signum: calls.append((process_group_id, signum)),
    )

    integration_installer._kill_process_group(True)

    assert calls == []


def test_installer_group_cleanup_rejects_numeric_subclass_id(monkeypatch):
    from codex_usage import integration_installer

    calls = []
    monkeypatch.setattr(
        integration_installer.os,
        "killpg",
        lambda process_group_id, signum: calls.append((process_group_id, signum)),
    )

    integration_installer._kill_process_group(_BrokenInt(4321))

    assert calls == []


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


def test_installer_builder_normal_exit_requires_process_group_gone(
    tmp_path,
    monkeypatch,
):
    """Would fail if a normal leader exit returned while descendants survived."""
    from codex_usage import integration_installer

    calls: list[tuple[str, object]] = []

    class FakeProcess:
        pid = 4321

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            return 0

        def poll(self):
            calls.append(("poll", None))
            return 0

    def fake_killpg(process_group_id, signum):
        calls.append(("killpg", (process_group_id, signum)))
        if signum == 0:
            return None
        return None

    monkeypatch.setattr(
        integration_installer.subprocess,
        "Popen",
        lambda *_args, **_kwargs: FakeProcess(),
    )
    monkeypatch.setattr(integration_installer.os, "getpgid", lambda _pid: 4321)
    monkeypatch.setattr(integration_installer.os, "killpg", fake_killpg)
    monkeypatch.setattr(
        integration_installer,
        "BUILDER_CLEANUP_WAIT_SECONDS",
        0,
        raising=False,
    )

    with pytest.raises(integration_installer.IntegrationCleanupError):
        integration_installer._run_builder_bounded(["builder"], env={}, cwd=tmp_path)

    assert ("killpg", (4321, 0)) in calls


def test_installer_builder_timeout_reports_live_leader_cleanup_failures(
    tmp_path,
    monkeypatch,
):
    """Would fail if timeout cleanup swallowed kill/wait errors for a live leader."""
    from codex_usage import integration_installer

    calls: list[object] = []

    class FakeProcess:
        pid = 4321

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            if timeout == integration_installer.BUILDER_WHEEL_TIMEOUT_SECONDS:
                raise subprocess.TimeoutExpired(["builder"], timeout)
            raise OSError("leader wait failed")

        def poll(self):
            calls.append(("poll", None))
            return None

        def kill(self):
            calls.append("kill")
            raise OSError("leader kill failed")

    def fake_killpg(process_group_id, signum):
        calls.append(("killpg", process_group_id, signum))
        raise OSError(f"group signal {signum} failed")

    monkeypatch.setattr(
        integration_installer.subprocess,
        "Popen",
        lambda *_args, **_kwargs: FakeProcess(),
    )
    monkeypatch.setattr(integration_installer.os, "getpgid", lambda _pid: 4321)
    monkeypatch.setattr(integration_installer.os, "killpg", fake_killpg)

    with pytest.raises(integration_installer.IntegrationCleanupError) as exc_info:
        integration_installer._run_builder_bounded(["builder"], env={}, cwd=tmp_path)

    def leaves(error: BaseException) -> list[BaseException]:
        if isinstance(error, BaseExceptionGroup):
            flattened: list[BaseException] = []
            for nested in error.exceptions:
                flattened.extend(leaves(nested))
            return flattened
        return [error]

    flattened = leaves(exc_info.value.__cause__)
    assert any(isinstance(error, subprocess.TimeoutExpired) for error in flattened)
    assert sorted(
        str(error)
        for error in flattened
        if isinstance(error, OSError)
    ) == [
        "group signal 0 failed",
        "group signal 0 failed",
        "group signal 15 failed",
        "group signal 9 failed",
        "leader kill failed",
        "leader wait failed",
    ]
    assert "kill" in calls
    assert ("wait", 1) in calls


def test_installer_builder_cleanup_reaped_leader_without_group_id_uses_pid_group(
    monkeypatch,
):
    """Would fail if already-reaped leaders skipped bounded process-group cleanup."""
    from codex_usage import integration_installer

    calls: list[object] = []

    class FakeProcess:
        pid = 4321

        def poll(self):
            calls.append(("poll", None))
            return 0

        def kill(self):
            calls.append("kill")

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            return 0

    def fake_killpg(process_group_id, signum):
        calls.append(("killpg", process_group_id, signum))
        if signum == 0:
            raise ProcessLookupError()

    monkeypatch.setattr(integration_installer.os, "killpg", fake_killpg)

    errors = integration_installer._collect_builder_process_cleanup_errors(
        FakeProcess(),
        None,
    )

    assert errors == []
    assert ("killpg", 4321, signal.SIGTERM) in calls
    assert ("killpg", 4321, 0) in calls
    assert ("wait", 1) in calls
    assert "kill" not in calls


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


def test_install_cleanup_attempts_all_artifacts_and_preserves_primary_error(
    tmp_path,
    monkeypatch,
):
    """Would fail if transaction cleanup stopped after the first cleanup exception."""
    from codex_usage import integration_installer

    data_home, state_home, temporary_root = _roots(tmp_path)
    source = _temporary_source_copy(tmp_path)
    original_rename = integration_installer._rename_owned_directory
    cleanup_attempts: list[str] = []

    def fail_final_release_rename(source_path, destination_path, *args, **kwargs):
        if Path(destination_path).name.startswith(
            f"{integration_installer.RELEASE_VERSION}-"
        ):
            raise integration_installer.IntegrationInstallError("synthetic primary failure")
        return original_rename(source_path, destination_path, *args, **kwargs)

    def fail_candidate_cleanup(path, *_args):
        if Path(path).name.startswith("candidate-"):
            cleanup_attempts.append("candidate")
            raise OSError("synthetic candidate cleanup failure")
        return True

    def fail_directory_cleanup(path, *_args):
        name = Path(path).name
        if name.startswith("producer-build-"):
            cleanup_attempts.append("build")
            raise OSError("synthetic build cleanup failure")
        if name.startswith("producer-wheel-"):
            cleanup_attempts.append("wheel")
            raise OSError("synthetic wheel cleanup failure")
        if ".staging-" in name:
            cleanup_attempts.append("staging")
            raise OSError("synthetic staging cleanup failure")
        return True

    monkeypatch.setattr(
        integration_installer,
        "_rename_owned_directory",
        fail_final_release_rename,
    )
    monkeypatch.setattr(
        integration_installer,
        "_cleanup_owned_file",
        fail_candidate_cleanup,
    )
    monkeypatch.setattr(
        integration_installer,
        "_cleanup_owned_directory",
        fail_directory_cleanup,
    )

    with pytest.raises(integration_installer.IntegrationCleanupError) as error:
        integration_installer.install_release(
            source_root=source,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )

    assert sorted(cleanup_attempts) == ["build", "candidate", "staging", "wheel"]
    leaves = integration_installer._base_exception_leaves([error.value.__cause__])
    assert any("synthetic primary failure" in str(leaf) for leaf in leaves)
    assert sorted(
        str(leaf)
        for leaf in leaves
        if isinstance(leaf, OSError) and "synthetic" in str(leaf)
    ) == [
        "synthetic build cleanup failure",
        "synthetic candidate cleanup failure",
        "synthetic staging cleanup failure",
        "synthetic wheel cleanup failure",
    ]


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


def _write_pool_authority_source(state_home: Path, account_ids) -> None:
    from codex_usage.private_io import write_private_text

    authorities = [
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
        for account_id in sorted(account_ids)
    ]
    write_private_text(
        state_home / "codex-usage/integration/pool-authority-source-v2.json",
        json.dumps(
            {
                "authorities": authorities,
                "pool_authority_source_schema_version": 2,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        label="synthetic pool authority source",
        mode=0o600,
    )


def _write_launcher_state(data_home: Path, state_home: Path) -> None:
    from codex_usage.models import AccountUsage, LimitWindow, UsagePool
    from codex_usage.private_io import write_private_text

    usage = AccountUsage(
        account_id="alpha",
        label="never-exported-label",
        captured_at=datetime.now(UTC),
        main=UsagePool(
            key="main",
            display_name="Synthetic",
            windows=(
                LimitWindow(
                    name="5h",
                    percent=75.0,
                    duration_seconds=18_000,
                ),
            ),
            availability_sources=("usage",),
        ),
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
    _write_pool_authority_source(state_home, ("alpha",))


def test_temporary_launcher_emits_schema2_from_temporary_state(tmp_path):
    release, data_home, state_home = _install(tmp_path)
    _write_launcher_state(data_home, state_home)
    completed = subprocess.run(
        [
            str(release.launcher_path),
            "integration-snapshot",
            "--schema",
            "2",
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
    assert payload["schema_version"] == 2
    assert "never-exported-label" not in completed.stdout
    assert "backend" not in completed.stdout
    assert completed.stderr == ""
    integration = state_home / "codex-usage" / "integration"
    pointer = json.loads((integration / "current.json").read_bytes())
    generation = integration / "generations" / pointer["current_generation_id"]
    assert (generation / "account-usage-v2.json").read_bytes() == completed.stdout.encode()
    assert (generation / "account-usage-v2.binding.json").is_file()
    assert (generation / "pool-authority-v2.json").is_file()
    assert (generation / "source-inputs-v2.json").is_file()
    assert {path.name for path in generation.iterdir()} == {
        "account-usage-v2.json",
        "account-usage-v2.binding.json",
        "pool-authority-v2.json",
        "source-inputs-v2.json",
    }
    assert not (integration / "account-usage-v1.json").exists()


def test_installed_launcher_omits_real_absolute_credit_without_blocking_accounts(tmp_path):
    from test_app_server import _auth, _fake_codex

    from codex_usage.app_server import fetch_account_usage_app_server
    from codex_usage.history import usage_samples_from_usage
    from codex_usage.integration_evidence import read_current_evidence
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        build_schema2_document,
        read_current_usage_records,
        serialize_schema2_document,
    )
    from codex_usage.models import Account, LimitWindow
    from codex_usage.state import save_current_usage

    release, data_home, state_home = _install(tmp_path)
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    _auth(auth_path, datetime.now(UTC) + timedelta(hours=1))
    scalar_values = {
        "absolute-a": "0",
        "absolute-b": "12",
        "absolute-c": "80",
        "absolute-d": "100",
        "absolute-e": "100.01",
        "absolute-f": "794",
    }
    current_dir = data_home / "codex-usage" / "current"
    fetched_by_id = {}
    for account_id, source_value in scalar_values.items():
        command = _fake_codex(
            tmp_path / f"codex-{account_id}",
            tmp_path / f"app-server-requests-{account_id}.json",
            account_credits=source_value,
            model_id="sol",
        )
        fetched = fetch_account_usage_app_server(
            Account(
                id=account_id,
                label="Absolute credit",
                profile_dir=str(tmp_path / f"profile-{account_id}"),
                auth_json_path=str(auth_path),
                backend="app-server",
            ),
            codex_command=command,
        )
        assert fetched.main is not None
        assert len(fetched.main.windows) == 2
        assert fetched.credits is not None
        assert fetched.credits.remaining == float(source_value)
        assert fetched.credits.limit is None
        assert fetched.credits.percent is None
        fetched_by_id[account_id] = fetched
        save_current_usage(fetched, current_dir)
    save_current_usage(
        replace(
            fetched_by_id["absolute-f"],
            account_id="percent-credit",
            label="Percent credit",
            credits=LimitWindow(name="credits", percent=80.0),
        ),
        current_dir,
    )
    near_limit = math.nextafter(1.0, 0.0)
    near_remaining = 1.0 - near_limit
    near_percent = near_remaining * 100.0
    boundary_command = _fake_codex(
        tmp_path / "codex-near-endpoint-credit",
        tmp_path / "app-server-requests-near-endpoint-credit.json",
        account_credits_payload={
            "used": near_limit,
            "remaining": near_remaining,
            "limit": 1.0,
            "percent": near_percent,
        },
        model_id="sol",
    )
    boundary = fetch_account_usage_app_server(
        Account(
            id="near-endpoint-credit",
            label="Near endpoint credit",
            profile_dir=str(tmp_path / "profile-near-endpoint-credit"),
            auth_json_path=str(auth_path),
            backend="app-server",
        ),
        codex_command=boundary_command,
    )
    assert boundary.credits == LimitWindow(
        name="credits",
        used=near_limit,
        remaining=near_remaining,
        limit=1.0,
        percent=near_percent,
        source="json:credits",
    )
    save_current_usage(boundary, current_dir)
    offset_reset_command = _fake_codex(
        tmp_path / "codex-offset-reset-credit",
        tmp_path / "app-server-requests-offset-reset-credit.json",
        account_credits_payload={
            "percent": 80,
            "reset_at": "2026-09-01T00:00:00Z",
            "resetAt": "2026-09-01T02:00:00+02:00",
        },
        model_id="sol",
    )
    offset_reset = fetch_account_usage_app_server(
        Account(
            id="offset-reset-credit",
            label="Offset reset credit",
            profile_dir=str(tmp_path / "profile-offset-reset-credit"),
            auth_json_path=str(auth_path),
            backend="app-server",
        ),
        codex_command=offset_reset_command,
    )
    assert offset_reset.credits == LimitWindow(
        name="credits",
        percent=80,
        reset_at=datetime(2026, 9, 1, tzinfo=UTC),
        source="json:credits",
    )
    save_current_usage(offset_reset, current_dir)
    _write_pool_authority_source(
        state_home,
        set(scalar_values)
        | {"near-endpoint-credit", "offset-reset-credit", "percent-credit"},
    )
    roundtrip = read_current_usage_records(current_dir)
    roundtrip_by_id = {usage.account_id: usage for usage in roundtrip}
    for account_id, source_value in scalar_values.items():
        credit = roundtrip_by_id[account_id].credits
        assert credit is not None
        assert credit.remaining == float(source_value)
        assert not any(
            sample.pool == "credits"
            for sample in usage_samples_from_usage(roundtrip_by_id[account_id])
        )
    boundary_roundtrip = roundtrip_by_id["near-endpoint-credit"]
    assert boundary_roundtrip.credits == boundary.credits
    assert [
        sample.used_percent
        for sample in usage_samples_from_usage(boundary_roundtrip)
        if sample.pool == "credits"
    ] == [pytest.approx(100.0 - near_percent)]
    offset_reset_roundtrip = roundtrip_by_id["offset-reset-credit"]
    assert offset_reset_roundtrip.credits == offset_reset.credits
    assert [
        sample.reset_generation
        for sample in usage_samples_from_usage(offset_reset_roundtrip)
        if sample.pool == "credits"
    ] == ["2026-09-01T00:00:00+00:00"]

    direct_error = None
    direct_document = None
    try:
        direct_document = build_schema2_document(
            roundtrip,
            generated_at=datetime.now(UTC),
        )
    except IntegrationInvalidSource as exc:
        direct_error = type(exc).__name__

    completed = subprocess.run(
        [
            str(release.launcher_path),
            "integration-snapshot",
            "--schema",
            "2",
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

    assert (direct_error, completed.returncode, completed.stderr) == (None, 0, "")
    assert direct_document is not None
    direct_payload = serialize_schema2_document(direct_document).decode("utf-8")
    for payload_text in (direct_payload, completed.stdout):
        payload = json.loads(payload_text)
        accounts = {account["account_id"]: account for account in payload["accounts"]}
        assert set(accounts) == set(scalar_values) | {
            "near-endpoint-credit",
            "offset-reset-credit",
            "percent-credit",
        }
        for account_id in scalar_values:
            assert {
                limit["window_seconds"]
                for limit in accounts[account_id]["limits"]
                if limit["pool"] == "main"
            } == {18_000, 604_800}
            assert not any(
                limit["pool"] == "credits"
                for limit in accounts[account_id]["limits"]
            )
        assert [
            limit
            for limit in accounts["percent-credit"]["limits"]
            if limit["pool"] == "credits"
        ] == [
            {
                "pool": "credits",
                "remaining_percent": 80.0,
                "used_percent": 20.0,
                "window_seconds": 2_592_000,
            }
        ]
        assert [
            limit
            for limit in accounts["offset-reset-credit"]["limits"]
            if limit["pool"] == "credits"
        ] == [
            {
                "pool": "credits",
                "remaining_percent": 80.0,
                "reset_at": "2026-09-01T00:00:00Z",
                "used_percent": 20.0,
                "window_seconds": 2_592_000,
            }
        ]
        assert [
            limit
            for limit in accounts["near-endpoint-credit"]["limits"]
            if limit["pool"] == "credits"
        ] == [
            {
                "pool": "credits",
                "remaining_percent": near_percent,
                "used_percent": 100.0 - near_percent,
                "window_seconds": 2_592_000,
            }
        ]
        assert all(
            evidence["pool"] != "credits"
            for account in accounts.values()
            for evidence in account["tracker_evidence"]
        )
        assert '"remaining":' not in payload_text
        assert '"limit":' not in payload_text
        assert all(
            value != 794.0
            for limit in accounts["absolute-f"]["limits"]
            for value in limit.values()
            if type(value) in (int, float)
        )

    evidence, status = read_current_evidence(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=release.entrypoint_path,
        now=datetime.now(UTC),
    )
    assert evidence == json.loads(completed.stdout)
    assert status == "partial"


def test_installed_launcher_invalid_real_credit_does_not_commit_current(tmp_path):
    from test_app_server import _auth, _fake_codex

    from codex_usage.app_server import fetch_account_usage_app_server
    from codex_usage.integration_evidence import read_current_evidence
    from codex_usage.integration_snapshot import read_current_usage_records
    from codex_usage.models import Account, LimitWindow
    from codex_usage.scheduler import _apply_watchdog_block
    from codex_usage.state import save_current_usage

    release, data_home, state_home = _install(tmp_path)
    auth_home = tmp_path / "codex-home-invalid"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    _auth(auth_path, datetime.now(UTC) + timedelta(hours=1))
    current_dir = data_home / "codex-usage" / "current"
    healthy_command = _fake_codex(
        tmp_path / "codex-healthy",
        tmp_path / "app-server-requests-healthy.json",
        model_id="sol",
    )
    healthy = fetch_account_usage_app_server(
        Account(
            id="healthy",
            label="Healthy",
            profile_dir=str(tmp_path / "profile-healthy"),
            auth_json_path=str(auth_path),
            backend="app-server",
        ),
        codex_command=healthy_command,
    )
    save_current_usage(healthy, current_dir)
    _write_pool_authority_source(state_home, ("healthy",))

    argv = [
        str(release.launcher_path),
        "integration-snapshot",
        "--schema",
        "2",
        "--format",
        "json",
    ]
    baseline = subprocess.run(
        argv,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert (baseline.returncode, baseline.stderr) == (0, "")
    integration = state_home / "codex-usage" / "integration"
    current_path = integration / "current.json"
    current_before = current_path.read_bytes()
    generations = integration / "generations"
    generations_before = {path.name for path in generations.iterdir()}

    conflict_command = _fake_codex(
        tmp_path / "codex-invalid-cross-source",
        tmp_path / "app-server-requests-invalid-cross-source.json",
        account_credits_payload={"percent": 80},
        rate_limits_extra_payload={"creditBalance": {"percent": 70}},
        model_id="sol",
    )
    fetched_conflict = fetch_account_usage_app_server(
        Account(
            id="invalid-credit",
            label="Invalid credit",
            profile_dir=str(tmp_path / "profile-invalid"),
            auth_json_path=str(auth_path),
            backend="app-server",
        ),
        codex_command=conflict_command,
    )
    assert fetched_conflict.credits == LimitWindow(
        name="credits",
        source="invalid:credits",
    )
    save_current_usage(fetched_conflict, current_dir)
    conflict_roundtrip = {
        usage.account_id: usage
        for usage in read_current_usage_records(current_dir)
    }
    assert conflict_roundtrip["invalid-credit"].credits == LimitWindow(
        name="credits",
        source="invalid:credits",
    )
    conflict_state = json.loads(
        (current_dir / "invalid-credit.json").read_bytes()
    )
    assert conflict_state["credits"] == {
        "duration_seconds": None,
        "limit": None,
        "name": "credits",
        "percent": None,
        "raw": None,
        "remaining": None,
        "reset_at": None,
        "source": "invalid:credits",
        "used": None,
    }

    conflict_completed = subprocess.run(
        argv,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert (
        conflict_completed.returncode,
        conflict_completed.stdout,
        conflict_completed.stderr,
    ) == (65, "", "integration_snapshot_invalid_source\n")
    assert current_path.read_bytes() == current_before
    assert {path.name for path in generations.iterdir()} == generations_before

    invalid_sources = (
        ("negative", "-1", None),
        ("nan", "NaN", None),
        ("infinite", "Inf", None),
        ("unparseable", "not-a-number", None),
        (
            "secondary-negative-alias",
            None,
            {"remaining": 0.0, "available": -math.ulp(0.0)},
        ),
        (
            "secondary-percent-above-hundred",
            None,
            {
                "percent": 100.0,
                "remaining_percent": math.nextafter(100.0, math.inf),
            },
        ),
        (
            "secondary-zero-limit",
            None,
            {"used": 0.0, "limit": math.ulp(0.0), "total": 0.0},
        ),
        (
            "conflicting-reset-aliases",
            None,
            {
                "percent": 80,
                "reset_at": "2026-09-01T00:00:00Z",
                "resetAt": "2026-10-01T00:00:00Z",
            },
        ),
        (
            "malformed-plus-valid-reset-aliases",
            None,
            {
                "percent": 80,
                "reset_at": "not-a-timestamp",
                "resetAt": "2026-09-01T00:00:00Z",
            },
        ),
        ("pair-used-limit", None, {"used": 101, "limit": 100}),
        ("pair-used-percent", None, {"used": 20, "percent": 70}),
        (
            "pair-used-remaining-without-denominator",
            None,
            {"used": 20, "remaining": 80},
        ),
        (
            "triple-used-limit-remaining",
            None,
            {"used": 20, "limit": 100, "remaining": 70},
        ),
        (
            "triple-used-limit-percent",
            None,
            {"used": 20, "limit": 100, "percent": 70},
        ),
        (
            "triple-remaining-limit-percent",
            None,
            {"remaining": 80, "limit": 100, "percent": 70},
        ),
        (
            "triple-without-limit",
            None,
            {"used": 20, "remaining": 70, "percent": 70},
        ),
        (
            "quad",
            None,
            {"used": 20, "limit": 100, "remaining": 70, "percent": 70},
        ),
    )
    for index, (case, source_value, source_payload) in enumerate(invalid_sources):
        command = _fake_codex(
            tmp_path / f"codex-invalid-{index}-{case}",
            tmp_path / f"app-server-requests-invalid-{index}-{case}.json",
            account_credits=source_value,
            account_credits_payload=source_payload,
        )
        fetched = fetch_account_usage_app_server(
            Account(
                id="invalid-credit",
                label="Invalid credit",
                profile_dir=str(tmp_path / "profile-invalid"),
                auth_json_path=str(auth_path),
                backend="app-server",
            ),
            codex_command=command,
        )
        assert fetched.status.value == "ok"
        assert fetched.main is not None and len(fetched.main.windows) == 2
        assert fetched.credits == LimitWindow(
            name="credits",
            source="invalid:credits",
        )
        save_current_usage(fetched, current_dir)
        roundtrip = {
            usage.account_id: usage
            for usage in read_current_usage_records(current_dir)
        }
        assert roundtrip["invalid-credit"].credits == LimitWindow(
            name="credits",
            source="invalid:credits",
        )
        state_payload = json.loads(
            (current_dir / "invalid-credit.json").read_bytes()
        )
        assert state_payload["credits"] == {
            "duration_seconds": None,
            "limit": None,
            "name": "credits",
            "percent": None,
            "raw": None,
            "remaining": None,
            "reset_at": None,
            "source": "invalid:credits",
            "used": None,
        }

        completed = subprocess.run(
            argv,
            env={"PATH": "/usr/bin:/bin"},
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

        assert (completed.returncode, completed.stdout, completed.stderr) == (
            65,
            "",
            "integration_snapshot_invalid_source\n",
        )
        assert current_path.read_bytes() == current_before
        assert {path.name for path in generations.iterdir()} == generations_before

    raw_pair = replace(
        healthy,
        account_id="invalid-credit",
        label="Invalid cached credit",
        captured_at=datetime.now(UTC),
        credits=LimitWindow(name="credits", used=20, remaining=80),
    )
    save_current_usage(raw_pair, current_dir)
    raw_pair_roundtrip = {
        usage.account_id: usage for usage in read_current_usage_records(current_dir)
    }["invalid-credit"]
    assert raw_pair_roundtrip.credits == LimitWindow(
        name="credits",
        used=20,
        remaining=80,
    )

    raw_pair_completed = subprocess.run(
        argv,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert (
        raw_pair_completed.returncode,
        raw_pair_completed.stdout,
        raw_pair_completed.stderr,
    ) == (65, "", "integration_snapshot_invalid_source\n")
    assert current_path.read_bytes() == current_before
    assert {path.name for path in generations.iterdir()} == generations_before

    blocked_command = _fake_codex(
        tmp_path / "codex-invalid-blocked",
        tmp_path / "app-server-requests-invalid-blocked.json",
        account_credits="-1",
        primary_used_percent=100,
        primary_resets_at=4_102_444_800,
    )
    fetched_blocked = fetch_account_usage_app_server(
        Account(
            id="invalid-credit",
            label="Invalid blocked credit",
            profile_dir=str(tmp_path / "profile-invalid-blocked"),
            auth_json_path=str(auth_path),
            backend="app-server",
        ),
        codex_command=blocked_command,
    )
    blocked = _apply_watchdog_block(
        fetched_blocked,
        now=fetched_blocked.captured_at,
    )
    assert blocked.status.value == "blocked"
    assert blocked.credits == LimitWindow(name="credits", source="invalid:credits")
    save_current_usage(blocked, current_dir)
    blocked_roundtrip = {
        usage.account_id: usage for usage in read_current_usage_records(current_dir)
    }["invalid-credit"]
    assert blocked_roundtrip.status.value == "blocked"
    assert blocked_roundtrip.credits == LimitWindow(
        name="credits",
        source="invalid:credits",
    )

    blocked_completed = subprocess.run(
        argv,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert (
        blocked_completed.returncode,
        blocked_completed.stdout,
        blocked_completed.stderr,
    ) == (65, "", "integration_snapshot_invalid_source\n")
    assert current_path.read_bytes() == current_before
    assert {path.name for path in generations.iterdir()} == generations_before

    evidence, status = read_current_evidence(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=release.entrypoint_path,
        now=datetime.now(UTC),
    )
    assert evidence == json.loads(baseline.stdout)
    assert status == "partial"


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
    _write_launcher_state(data_home, state_home)
    active = state_home / "codex-usage" / "integration" / "active.json"
    before_tree = _release_tree_sha256(release_dir=release.release_dir)
    before_active = active.read_bytes()
    completed = subprocess.run(
        [
            str(release.launcher_path),
            "integration-snapshot",
            "--schema",
            "2",
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
    from codex_usage import integration_installer

    _safe_extract_wheel = integration_installer._safe_extract_wheel

    wheel = tmp_path / "candidate.whl"
    destination = tmp_path / "destination"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("codex_usage/ok.py", b"x")
    destination.mkdir(mode=0o700)
    identities = _safe_extract_wheel(
        wheel_path=wheel,
        destination=destination,
        record_rows={"codex_usage/ok.py": (hashlib.sha256(b"x").hexdigest(), 1)},
    )
    target = destination / "codex_usage" / "ok.py"
    assert target.read_bytes() == b"x"
    assert stat.S_IMODE(target.lstat().st_mode) == 0o600
    assert _tree_bytes(destination) == (("codex_usage/ok.py", 0o600, b"x"),)
    assert identities["codex_usage/ok.py"] == (
        integration_installer._directory_identity(target.parent),
        integration_installer._file_identity(target),
    )


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


def test_copy_regular_binds_target_to_parent_descriptor(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    source = tmp_path / "source"
    source.write_bytes(b"source")
    source.chmod(0o600)
    parent = tmp_path / "destination"
    parent.mkdir(mode=0o700)
    target = parent / "target"
    old_parent = tmp_path / "destination-old"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    original_open = os.open
    swapped = False

    def swap_before_target_open(candidate, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if candidate == "target" and dir_fd is not None and not swapped:
            parent.rename(old_parent)
            outside.rename(parent)
            swapped = True
        if dir_fd is None:
            return original_open(candidate, flags, mode)
        return original_open(candidate, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(integration_installer.os, "open", swap_before_target_open)
    identity = integration_installer._copy_regular(source, target)

    assert swapped
    assert identity == integration_installer._FileIdentity(
        old_parent.stat().st_dev,
        (old_parent / "target").stat().st_ino,
        0o600,
        (old_parent / "target").stat().st_gid,
    )
    assert (old_parent / "target").read_bytes() == b"source"
    assert not (parent / "target").exists()


def test_copy_regular_rejects_replaced_source_before_read(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    source = tmp_path / "source"
    source.write_bytes(b"owned")
    source.chmod(0o600)
    parent = tmp_path / "destination"
    parent.mkdir(mode=0o700)
    target = parent / "target"
    original_read = integration_installer._read_nofollow
    replaced = False

    def replace_before_read(path, **kwargs):
        nonlocal replaced
        if path == source and not replaced:
            source.unlink()
            source.write_bytes(b"foreign")
            source.chmod(0o600)
            replaced = True
        return original_read(path, **kwargs)

    monkeypatch.setattr(integration_installer, "_read_nofollow", replace_before_read)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._copy_regular(source, target)

    assert replaced
    assert source.read_bytes() == b"foreign"
    assert not target.exists()


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


def test_safe_extract_rejects_parent_symlink_before_target_open(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    wheel = tmp_path / "candidate.whl"
    destination = tmp_path / "destination"
    destination.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("codex_usage/ok.py", b"x")
    parent = destination / "codex_usage"
    old_parent = destination / "codex_usage-old"
    target = parent / "ok.py"
    original_open = os.open
    swapped = False

    def swap_before_target_open(candidate, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if (
            (candidate == target or (candidate == "codex_usage" and dir_fd is not None))
            and not swapped
        ):
            parent.rename(old_parent)
            parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        if dir_fd is None:
            return original_open(candidate, flags, mode)
        return original_open(candidate, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(integration_installer.os, "open", swap_before_target_open)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._safe_extract_wheel(
            wheel_path=wheel,
            destination=destination,
            record_rows={"codex_usage/ok.py": (hashlib.sha256(b"x").hexdigest(), 1)},
        )

    assert swapped
    assert not (outside / "ok.py").exists()
    assert (old_parent / "ok.py").exists() is False


def test_safe_extract_rejects_parent_directory_swap_before_target_open(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    wheel = tmp_path / "candidate.whl"
    destination = tmp_path / "destination"
    destination.mkdir(mode=0o700)
    parent = destination / "codex_usage"
    parent.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    old_parent = tmp_path / "codex_usage-old"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("codex_usage/ok.py", b"x")
    original_open = os.open
    swapped = False

    def swap_before_parent_open(candidate, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if candidate == "codex_usage" and dir_fd is not None and not swapped:
            parent.rename(old_parent)
            outside.rename(parent)
            swapped = True
        if dir_fd is None:
            return original_open(candidate, flags, mode)
        return original_open(candidate, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(integration_installer.os, "open", swap_before_parent_open)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._safe_extract_wheel(
            wheel_path=wheel,
            destination=destination,
            record_rows={"codex_usage/ok.py": (hashlib.sha256(b"x").hexdigest(), 1)},
        )

    assert swapped
    assert not (parent / "ok.py").exists()
    assert not (old_parent / "ok.py").exists()


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


def test_write_exclusive_rejects_parent_swap_before_target_open(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    target = parent / "target"
    old_parent = tmp_path / "parent-old"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    original_open = os.open
    swapped = False

    def swap_before_target_open(candidate, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if candidate == "target" and dir_fd is not None and not swapped:
            parent.rename(old_parent)
            outside.rename(parent)
            swapped = True
        if dir_fd is None:
            return original_open(candidate, flags, mode)
        return original_open(candidate, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(integration_installer.os, "open", swap_before_target_open)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._write_exclusive(target, b"payload", mode=0o600)

    assert swapped
    assert not (parent / "target").exists()
    assert (old_parent / "target").exists()
    assert (old_parent / "target").read_bytes() == b""


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
        integration_identity.gid,
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

    def swap_mode_before_open(candidate, flags, *args, **kwargs):
        nonlocal swapped
        if (
            (
                candidate == path
                or (candidate == path.name and kwargs.get("dir_fd") is not None)
            )
            and not swapped
        ):
            swapped = True
            path.chmod(0o644)
        return original_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(integration_attestation.os, "open", swap_mode_before_open)

    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation._file_bytes(path, mode=0o600)


def test_attestation_reader_binds_file_to_parent_descriptor(tmp_path, monkeypatch):
    from codex_usage import integration_attestation

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    path = parent / "payload"
    path.write_bytes(b"owned")
    path.chmod(0o600)
    old_parent = tmp_path / "parent-old"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    foreign = outside / "payload"
    foreign.write_bytes(b"foreign")
    foreign.chmod(0o600)
    original_open = os.open
    swapped = False

    def swap_before_target_open(candidate, flags, *args, dir_fd=None):
        nonlocal swapped
        if candidate == "payload" and dir_fd is not None and not swapped:
            parent.rename(old_parent)
            outside.rename(parent)
            swapped = True
        if dir_fd is None:
            return original_open(candidate, flags, *args)
        return original_open(candidate, flags, *args, dir_fd=dir_fd)

    monkeypatch.setattr(integration_attestation.os, "open", swap_before_target_open)
    assert integration_attestation._file_bytes(path, mode=0o600) == b"owned"
    assert swapped
    assert (parent / "payload").read_bytes() == b"foreign"
    assert (old_parent / "payload").read_bytes() == b"owned"


def test_attestation_tree_rejects_child_directory_swap_before_open(
    tmp_path, monkeypatch
):
    from codex_usage import integration_attestation

    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    nested = release / "nested"
    nested.mkdir(mode=0o700)
    payload = nested / "payload"
    payload.write_bytes(b"owned")
    payload.chmod(0o600)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    foreign = outside / "foreign"
    foreign.write_bytes(b"foreign")
    foreign.chmod(0o600)
    old_nested = tmp_path / "nested-old"
    original_open = os.open
    swapped = False

    def swap_before_child_open(candidate, flags, *args, dir_fd=None):
        nonlocal swapped
        if candidate == "nested" and dir_fd is not None and not swapped:
            nested.rename(old_nested)
            outside.rename(nested)
            swapped = True
        if dir_fd is None:
            return original_open(candidate, flags, *args)
        return original_open(candidate, flags, *args, dir_fd=dir_fd)

    monkeypatch.setattr(integration_attestation.os, "open", swap_before_child_open)
    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation._release_tree_sha256(release_dir=release)
    assert swapped
    assert (old_nested / "payload").read_bytes() == b"owned"
    assert not (old_nested / "foreign").exists()


def test_attestation_tree_rejects_foreign_owned_child_directory(tmp_path, monkeypatch):
    from codex_usage import integration_attestation

    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    nested = release / "nested"
    nested.mkdir(mode=0o700)
    payload = nested / "payload"
    payload.write_bytes(b"owned")
    payload.chmod(0o600)
    original_fstat = integration_attestation.os.fstat
    nested_inode = nested.stat().st_ino

    def foreign_nested_fstat(fd):
        item = original_fstat(fd)
        if stat.S_ISDIR(item.st_mode) and item.st_ino == nested_inode:
            return SimpleNamespace(
                st_dev=item.st_dev,
                st_ino=item.st_ino,
                st_mode=item.st_mode,
                st_uid=os.getuid() + 1,
            )
        return item

    monkeypatch.setattr(integration_attestation.os, "fstat", foreign_nested_fstat)

    with pytest.raises(integration_attestation.IntegrationAttestationUnavailable):
        integration_attestation._release_tree_sha256(release_dir=release)


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


def test_attestation_record_digest_requires_canonical_urlsafe_base64():
    from codex_usage import integration_attestation

    canonical = base64.urlsafe_b64encode(hashlib.sha256(b"x").digest()).decode().rstrip(
        "="
    )

    assert integration_attestation._record_digest("sha256=" + canonical, b"x")
    for suffix in ("=", "!!"):
        assert not integration_attestation._record_digest(
            "sha256=" + canonical + suffix,
            b"x",
        )


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


def test_postwalk_release_rejects_child_directory_swap_before_fd_open(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    nested = release / "nested"
    nested.mkdir(mode=0o700)
    (nested / "inside.txt").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (outside / "foreign.txt").write_bytes(b"foreign")
    moved = tmp_path / "nested-original"
    original_open = os.open
    swapped = False

    def swap_before_child_open(candidate, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if candidate == "nested" and dir_fd is not None and not swapped:
            nested.rename(moved)
            outside.rename(nested)
            swapped = True
        return original_open(candidate, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(integration_installer.os, "open", swap_before_child_open)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._postwalk_release(release)
    assert swapped


def test_postwalk_release_rejects_foreign_owned_file(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    payload = release / "payload"
    payload.write_bytes(b"foreign")
    item = payload.lstat()
    foreign = SimpleNamespace(
        st_dev=item.st_dev,
        st_ino=item.st_ino,
        st_uid=os.getuid() + 1,
        st_gid=item.st_gid,
        st_mode=item.st_mode,
        st_nlink=item.st_nlink,
    )

    class ForeignEntry:
        name = "payload"

        def stat(self, *, follow_symlinks=False):
            return foreign

    class ForeignScan:
        def __enter__(self):
            return iter((ForeignEntry(),))

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr(
        integration_installer.os,
        "scandir",
        lambda directory_fd: ForeignScan(),
    )
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._postwalk_release(release)

    assert payload.read_bytes() == b"foreign"


def test_postwalk_release_rejects_foreign_owned_root(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    item = release.lstat()
    foreign = SimpleNamespace(
        st_dev=item.st_dev,
        st_ino=item.st_ino,
        st_uid=os.getuid() + 1,
        st_gid=item.st_gid,
        st_mode=item.st_mode,
        st_nlink=item.st_nlink,
    )
    original_fstat = integration_installer.os.fstat
    calls = 0

    def foreign_root_fstat(fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            return foreign
        return original_fstat(fd)

    monkeypatch.setattr(integration_installer.os, "fstat", foreign_root_fstat)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._postwalk_release(release)

    assert calls == 1


def test_installer_reader_rejects_oversized_file_before_materializing(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    path = tmp_path / "installer-file"
    path.write_bytes(b"xxxxx")
    path.chmod(0o600)
    monkeypatch.setattr(integration_installer, "MAX_INSTALL_FILE_BYTES", 4, raising=False)

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._read_nofollow(path)


def test_installer_reader_rejects_size_drift_after_open(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    path = tmp_path / "installer-file"
    path.write_bytes(b"payload")
    path.chmod(0o600)
    original_fstat = integration_installer.os.fstat

    def report_drifted_size(fd):
        item = original_fstat(fd)
        return SimpleNamespace(
            st_mode=item.st_mode,
            st_nlink=item.st_nlink,
            st_uid=item.st_uid,
            st_size=item.st_size - 1,
        )

    monkeypatch.setattr(integration_installer.os, "fstat", report_drifted_size)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._read_nofollow(path)


def test_wheel_member_reader_rejects_header_size_drift():
    from codex_usage import integration_installer

    class FakeSource:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size):
            return b"payload" if size > 1 else b""

    class FakeArchive:
        def open(self, info, mode):
            return FakeSource()

    info = SimpleNamespace(file_size=len(b"payload") + 1)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._read_bounded_wheel_member(FakeArchive(), info)


def test_wheel_member_reader_rejects_numeric_subclass_size():
    from codex_usage import integration_installer

    class FakeSource:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size):
            return b"payload" if size > 1 else b""

    class FakeArchive:
        def open(self, info, mode):
            return FakeSource()

    info = SimpleNamespace(file_size=_BrokenInt(len(b"payload") + 1))
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._read_bounded_wheel_member(FakeArchive(), info)


def test_installer_reader_rejects_foreign_owner(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    path = tmp_path / "installer-file"
    path.write_bytes(b"payload")
    path.chmod(0o600)
    monkeypatch.setattr(integration_installer.os, "getuid", lambda: 2**31 - 1)

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._read_nofollow(path)


def test_installer_reader_rejects_replaced_file_after_parent_open(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    parent = tmp_path / "wheel-parent"
    parent.mkdir(mode=0o700)
    path = parent / "candidate.whl"
    path.write_bytes(b"original")
    path.chmod(0o600)
    parent_identity = integration_installer._directory_identity(parent)
    file_identity = integration_installer._file_identity(path)
    original_open = os.open
    replaced = False

    def replace_before_file_open(candidate, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if candidate == path.name and dir_fd is not None and not replaced:
            path.unlink()
            path.write_bytes(b"foreign")
            path.chmod(0o600)
            replaced = True
        if dir_fd is None:
            return original_open(candidate, flags, mode)
        return original_open(candidate, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(integration_installer.os, "open", replace_before_file_open)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._read_nofollow(
            path,
            expected_parent_identity=parent_identity,
            expected_file_identity=file_identity,
        )

    assert replaced
    assert path.read_bytes() == b"foreign"


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


def test_two_valid_releases_keep_new_runtime_when_public_rollback_is_rejected(tmp_path):
    from codex_usage.integration_attestation import (
        IntegrationAttestationUnavailable,
        verify_active_release,
    )
    from codex_usage.integration_installer import (
        IntegrationInstallError,
        install_release,
        rollback_active_release,
    )

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
    active_path = state_home / "codex-usage" / "integration" / "active.json"
    active_before = active_path.read_bytes()
    with pytest.raises(IntegrationInstallError):
        rollback_active_release(state_home=state_home, data_home=data_home)
    assert active_path.read_bytes() == active_before
    assert (
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=second.entrypoint_path,
        )
        == second
    )


def test_runtime_wheel_import_closure_is_exact_and_utc_precedes_python_import(tmp_path):
    release, _, _ = _install(tmp_path)
    from codex_usage import integration_attestation, integration_installer

    allowed = {
        f"codex_usage/{name}" for name in integration_attestation.PRODUCER_RELEASE_MODULES
    }
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
    assert "codex_usage/integration_watchdog.py" not in package_members
    assert "codex_usage/integration_timeout_contract.py" not in package_members
    launcher = release.launcher_path.read_text(encoding="utf-8")
    assert launcher.index("TZ=UTC") < launcher.index("exec ")


def test_runtime_import_gate_rejects_missing_producer_dependency():
    """Would fail if the Producer module list could omit an imported dependency."""
    from codex_usage import integration_attestation, integration_installer

    modules = {
        f"codex_usage/{module_name}": (
            PROJECT_ROOT / "src" / "codex_usage" / module_name
        ).read_bytes()
        for module_name in integration_attestation.PRODUCER_RELEASE_MODULES
        if module_name != "integration_snapshot.py"
    }

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._validate_runtime_import_closure(modules)


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
        holder_go.wait()
        with private_path_lock(
            integration / "producer-install",
            timeout_seconds=0,
            label="integration producer lock",
        ):
            holder_locked.set()
            release.wait()
        queue.put(("holder-released",))
        return
    holder_locked.wait()
    try:
        with private_path_lock(
            integration / "producer-install",
            timeout_seconds=0,
            label="integration producer lock",
        ):
            queue.put(("unexpected-second-lock",))
    except TimeoutError:
        queue.put(("busy",))


def _process_message(result_queue, processes, deadline):
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("installer test processes did not make bounded progress")
        try:
            return result_queue.get(timeout=min(remaining, 0.1))
        except queue.Empty:
            if all(process.exitcode is not None for process in processes):
                raise AssertionError(
                    "installer test processes exited before reporting: "
                    + ", ".join(str(process.exitcode) for process in processes)
                ) from None


def test_process_message_fails_immediately_after_children_exit():
    class EmptyQueue:
        def get(self, *, timeout):
            raise queue.Empty

    process = SimpleNamespace(exitcode=17)
    with pytest.raises(AssertionError, match="exited before reporting: 17"):
        _process_message(
            EmptyQueue(),
            (process,),
            time.monotonic() + BOOTSTRAP_PROCESS_TIMEOUT_SECONDS,
        )


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
    processes = (holder, contender)
    started = []
    deadline = time.monotonic() + BOOTSTRAP_PROCESS_TIMEOUT_SECONDS
    try:
        holder.start()
        started.append(holder)
        contender.start()
        started.append(contender)
        first = _process_message(queue, processes, deadline)
        second = _process_message(queue, processes, deadline)
        assert first[0] == second[0] == "booted"
        assert first[1:] == second[1:]
        assert sorted(path.name for path in state_home.iterdir()) == ["codex-usage"]
        holder_go.set()
        assert holder_locked.wait(max(0, deadline - time.monotonic()))
        assert _process_message(queue, processes, deadline) == ("busy",)
        release.set()
        assert _process_message(queue, processes, deadline) == ("holder-released",)
        for process in processes:
            process.join(max(0, deadline - time.monotonic()))
        assert holder.exitcode == contender.exitcode == 0
    finally:
        holder_go.set()
        release.set()
        for process in started:
            if process.is_alive():
                process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)
        queue.close()
        queue.join_thread()


def test_installer_script_has_narrow_parser_and_no_general_cli_import(tmp_path):
    repo_root = _temporary_bootstrap_repo(tmp_path)

    completed = _run_bootstrap_help(repo_root)

    assert completed.returncode == 0
    assert completed.stderr == ""
    for option in (
        "--rollback",
        "--prepare-source",
        "--source-root",
        "--state-home",
        "--data-home",
        "--python",
        "--temporary-root",
    ):
        assert option in completed.stdout
    rejected = subprocess.run(
        [
            sys.executable,
            "-B",
            str(repo_root / "scripts" / SCRIPT_PATH.name),
            "--rollback",
            "--state-home",
            "/tmp/state",
            "--data-home",
            "/tmp/data",
            "--python",
            "/tmp/python",
        ],
        cwd=repo_root,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=BOOTSTRAP_PROCESS_TIMEOUT_SECONDS,
    )
    assert rejected.returncode == 64
    assert rejected.stderr == "integration_producer_unavailable\n"
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "codex_usage.cli" not in source


@pytest.mark.parametrize(
    ("checkout_mode", "source_file_mode"),
    ((0o700, 0o600), (0o755, 0o644)),
    ids=("secure-0077", "normal-0022"),
)
def test_public_prepare_source_normalizes_checkout_before_real_install(
    tmp_path, pytestconfig, checkout_mode, source_file_mode
):
    """Would fail if a real 0077 or 0022 checkout cannot be prepared once."""
    repo_root = _temporary_bootstrap_repo(tmp_path)
    for path in (
        repo_root,
        repo_root / "scripts",
        repo_root / "src",
        repo_root / "src/codex_usage",
    ):
        path.chmod(checkout_mode)
    for path in repo_root.rglob("*"):
        if path.is_file():
            path.chmod(source_file_mode)
    untouched = repo_root / "unrelated-private-note"
    untouched.write_text("do-not-normalize", encoding="utf-8")
    untouched.chmod(source_file_mode)
    data_home, state_home, temporary_root = _roots(tmp_path)
    command = [
        sys.executable,
        "-B",
        str(repo_root / "scripts" / SCRIPT_PATH.name),
        "--prepare-source",
        "--source-root",
        str(repo_root),
    ]

    prepared = subprocess.run(
        command,
        cwd=repo_root,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=BOOTSTRAP_PROCESS_TIMEOUT_SECONDS,
    )

    assert prepared.returncode == 0, prepared.stderr
    assert prepared.stdout == "integration_producer_source_prepared\n"
    assert prepared.stderr == ""
    assert stat.S_IMODE(repo_root.lstat().st_mode) == 0o700
    for relative in (*TEST_SOURCE_MANIFEST_FILES, f"scripts/{SCRIPT_PATH.name}"):
        source = repo_root / relative
        assert source.is_file() and not source.is_symlink()
        assert source.stat().st_nlink == 1
        assert stat.S_IMODE(source.stat().st_mode) == 0o644
    assert stat.S_IMODE(untouched.stat().st_mode) == source_file_mode
    assert not (state_home / "codex-usage" / "integration" / "active.json").exists()

    from codex_usage import private_io

    lock_root = private_io._private_lock_root()
    production_lock_root = pytestconfig._private_lock_production_root
    bwrap_item = Path("/usr/bin/bwrap").stat()
    trusted_bwrap_fds = []
    for candidate in Path("/proc/self/fd").iterdir():
        try:
            item = candidate.stat()
        except OSError:
            continue
        if (item.st_dev, item.st_ino) == (bwrap_item.st_dev, bwrap_item.st_ino):
            trusted_bwrap_fds.append(candidate)
    assert len(trusted_bwrap_fds) == 1
    installed = subprocess.run(
        [
            str(trusted_bwrap_fds[0]),
            "--bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--dev-bind",
            "/dev/null",
            "/dev/null",
            "--bind",
            str(lock_root),
            str(production_lock_root),
            "--",
            sys.executable,
            "-B",
            str(repo_root / "scripts" / SCRIPT_PATH.name),
            "--source-root",
            str(repo_root),
            "--state-home",
            str(state_home),
            "--data-home",
            str(data_home),
            "--python",
            sys.executable,
            "--temporary-root",
            str(temporary_root),
        ],
        cwd=repo_root,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=BOOTSTRAP_PROCESS_TIMEOUT_SECONDS,
        close_fds=False,
    )

    assert installed.returncode == 0, installed.stderr
    assert installed.stdout == "integration_producer_install_ok\n"
    assert installed.stderr == ""
    manifest = json.loads(
        (state_home / "codex-usage" / "integration" / "active.json").read_bytes()
    )
    assert manifest["version"] == "0.6.542"
    assert re.fullmatch(r"0\.6\.542-[0-9a-f]{16}", manifest["release_id"])


@pytest.mark.parametrize(
    "mutation",
    ("symlink", "hardlink", "unknown-root", "root-mode-0750", "root-mode-0777"),
)
def test_public_prepare_source_rejects_hostile_or_unknown_input_without_mutation(
    tmp_path, mutation
):
    """Would fail if preparation normalized a link, foreign root, or partial closure."""
    repo_root = _temporary_bootstrap_repo(tmp_path)
    for path in (
        repo_root,
        repo_root / "scripts",
        repo_root / "src",
        repo_root / "src/codex_usage",
    ):
        path.chmod(0o700)
    for path in repo_root.rglob("*"):
        if path.is_file():
            path.chmod(0o600)
    target = repo_root / "src/codex_usage/history.py"
    initial_target_mode = stat.S_IMODE(target.stat().st_mode)
    source_root = repo_root
    if mutation == "symlink":
        outside = tmp_path / "outside.py"
        outside.write_text("outside", encoding="utf-8")
        outside.chmod(0o600)
        target.unlink()
        target.symlink_to(outside)
    elif mutation == "hardlink":
        linked = repo_root / "source-linked.py"
        os.link(target, linked)
    elif mutation == "root-mode-0750":
        repo_root.chmod(0o750)
    elif mutation == "root-mode-0777":
        repo_root.chmod(0o777)
    else:
        source_root = tmp_path / "unknown-root"
        source_root.mkdir(mode=0o700)
    initial_root_mode = stat.S_IMODE(repo_root.stat().st_mode)

    rejected = subprocess.run(
        [
            sys.executable,
            "-B",
            str(repo_root / "scripts" / SCRIPT_PATH.name),
            "--prepare-source",
            "--source-root",
            str(source_root),
        ],
        cwd=repo_root,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=BOOTSTRAP_PROCESS_TIMEOUT_SECONDS,
    )

    assert rejected.returncode == 69
    assert rejected.stdout == ""
    assert rejected.stderr == "integration_producer_source_prepare_rejected\n"
    assert stat.S_IMODE(repo_root.stat().st_mode) == initial_root_mode
    if mutation == "symlink":
        assert target.is_symlink()
    else:
        assert stat.S_IMODE(target.stat().st_mode) == initial_target_mode


@pytest.mark.parametrize(
    ("rebind_after_stage", "expected_stages"),
    (("root", ("root",)), ("pyproject.toml", ("root", "pyproject.toml"))),
    ids=("before-root-mode", "before-first-closure-file-mode"),
)
def test_public_prepare_source_rejects_rebind_before_any_following_mode_mutation(
    tmp_path, monkeypatch, capsys, rebind_after_stage, expected_stages
):
    """Would fail if a rebind after validation could chmod either source tree."""
    repo_root = _temporary_bootstrap_repo(tmp_path)
    script = repo_root / "scripts" / SCRIPT_PATH.name
    monkeypatch.setattr(sys, "argv", [str(script)])
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    with _loaded_public_prepare_namespace(repo_root) as public_prepare:
        for path in (
            repo_root,
            repo_root / "scripts",
            repo_root / "src",
            repo_root / "src/codex_usage",
        ):
            path.chmod(0o700)
        for path in repo_root.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        bound_root = tmp_path / f"bound-{rebind_after_stage}"
        snapshots: dict[str, tuple[tuple[object, ...], ...]] = {}
        observed_stages: list[str] = []

        def rebind_after_validation(stage: str) -> None:
            observed_stages.append(stage)
            if stage != rebind_after_stage:
                return
            assert not snapshots
            repo_root.rename(bound_root)
            repo_root.mkdir(mode=0o700)
            hostile_note = repo_root / "hostile-note"
            hostile_note.write_bytes(b"hostile-root-must-not-change")
            hostile_note.chmod(0o600)
            snapshots["bound"] = _prepare_source_tree_snapshot(bound_root)
            snapshots["hostile"] = _prepare_source_tree_snapshot(repo_root)

        monkeypatch.setitem(
            public_prepare["_prepare_source_root"].__globals__,
            "_prepare_after_closure_validation",
            rebind_after_validation,
        )

        result = public_prepare["main"](
            ["--prepare-source", "--source-root", str(repo_root)]
        )

    captured = capsys.readouterr()
    assert result == 69
    assert captured.out == ""
    assert captured.err == "integration_producer_source_prepare_rejected\n"
    assert observed_stages == list(expected_stages)
    assert _prepare_source_tree_snapshot(bound_root) == snapshots["bound"]
    assert _prepare_source_tree_snapshot(repo_root) == snapshots["hostile"]


def test_installer_script_bootstraps_repo_source_ahead_of_ambient_package(
    tmp_path, pytestconfig
):
    from codex_usage import private_io

    source_root = _temporary_source_copy(tmp_path)
    bootstrap_parent = tmp_path / "bootstrap"
    bootstrap_parent.mkdir(mode=0o700)
    repo_root = _temporary_bootstrap_repo(bootstrap_parent)
    data_home, state_home, temporary_root = _roots(tmp_path)
    lock_root = private_io._private_lock_root()
    production_lock_root = pytestconfig._private_lock_production_root
    # The script directory precedes the bootstrapper's explicit source-root
    # insertion.  This is a real ambient package attack without PYTHONPATH.
    ambient_package = repo_root / "scripts" / "codex_usage"
    ambient_package.mkdir(parents=True)
    (ambient_package / "__init__.py").write_text("", encoding="utf-8")
    (ambient_package / "integration_installer.py").write_text(
        """\
from pathlib import Path

class IntegrationCleanupError(Exception):
    pass

class IntegrationInstallError(Exception):
    pass

def install_release(**kwargs):
    Path(kwargs["state_home"], "ambient-imported").write_text("0.6.532")

def rollback_active_release(**kwargs):
    Path(kwargs["state_home"], "ambient-imported").write_text("0.6.532")
""",
        encoding="utf-8",
    )

    bwrap_item = Path("/usr/bin/bwrap").stat()
    trusted_bwrap_fds = []
    for candidate in Path("/proc/self/fd").iterdir():
        try:
            item = candidate.stat()
        except OSError:
            continue
        if (item.st_dev, item.st_ino) == (bwrap_item.st_dev, bwrap_item.st_ino):
            trusted_bwrap_fds.append(candidate)
    assert len(trusted_bwrap_fds) == 1

    completed = subprocess.run(
        [
            str(trusted_bwrap_fds[0]),
            "--bind",
            "/",
            "/",
            "--dev",
            "/dev",
            # The already attested session bwrap FD avoids a second outer user
            # namespace; bind the actual device that subprocess.DEVNULL opens.
            "--dev-bind",
            "/dev/null",
            "/dev/null",
            "--bind",
            str(lock_root),
            str(production_lock_root),
            "--",
            sys.executable,
            "-B",
            str(repo_root / "scripts" / SCRIPT_PATH.name),
            "--source-root",
            str(source_root),
            "--state-home",
            str(state_home),
            "--data-home",
            str(data_home),
            "--python",
            sys.executable,
            "--temporary-root",
            str(temporary_root),
        ],
        cwd=tmp_path,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
            capture_output=True,
            text=True,
            timeout=30,
            # The session fixture made exactly this bwrap FD inheritable.
            close_fds=False,
    )

    assert not (state_home / "ambient-imported").exists()
    assert completed.returncode == 0
    assert completed.stdout == "integration_producer_install_ok\n"
    assert completed.stderr == ""
    active = json.loads(
        (state_home / "codex-usage" / "integration" / "active.json").read_bytes()
    )
    assert active["version"] == "0.6.542"


def test_installer_script_rejects_symlinked_entrypoint_before_ambient_import(tmp_path):
    # The linked script's own directory is an ambient import location.
    ambient_package = tmp_path / "codex_usage"
    ambient_package.mkdir(parents=True)
    marker = tmp_path / "ambient-imported"
    (ambient_package / "__init__.py").write_text("", encoding="utf-8")
    (ambient_package / "integration_installer.py").write_text(
        """\
import os
from pathlib import Path

Path(os.environ["AMBIENT_IMPORT_MARKER"]).write_text("imported")

class IntegrationCleanupError(Exception):
    pass

class IntegrationInstallError(Exception):
    pass

def install_release(**kwargs):
    pass

def rollback_active_release(**kwargs):
    pass
""",
        encoding="utf-8",
    )
    linked_script = tmp_path / SCRIPT_PATH.name
    linked_script.symlink_to(SCRIPT_PATH)

    completed = subprocess.run(
        [sys.executable, "-B", str(linked_script), "--help"],
        cwd=tmp_path,
        env={
            "AMBIENT_IMPORT_MARKER": str(marker),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 69
    assert completed.stdout == ""
    assert completed.stderr == "integration_producer_unavailable\n"
    assert not marker.exists()


_BOOTSTRAP_TRANSITIVE_MODULES = tuple(
    Path(relative).name
    for relative in TEST_SOURCE_MANIFEST_FILES
    if relative.startswith("src/codex_usage/")
    and relative != "src/codex_usage/__init__.py"
)


def _temporary_bootstrap_repo(destination_root: Path) -> Path:
    repo_root = _temporary_source_copy(destination_root)
    for path in repo_root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    scripts = repo_root / "scripts"
    scripts.mkdir(mode=0o755)
    shutil.copyfile(SCRIPT_PATH, scripts / SCRIPT_PATH.name)
    shutil.copyfile(
        PROJECT_ROOT / "src/codex_usage/integration_installer.py",
        repo_root / "src/codex_usage/integration_installer.py",
    )
    (scripts / SCRIPT_PATH.name).chmod(0o644)
    (repo_root / "src/codex_usage/integration_installer.py").chmod(0o644)
    return repo_root


def _run_bootstrap_help(
    repo_root: Path,
    *,
    environment: dict[str, str] | None = None,
    include_bytecode_environment: bool = True,
    prefix: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    child_environment = {"PATH": os.environ.get("PATH", "")}
    if include_bytecode_environment:
        child_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    child_environment.update(environment or {})
    return subprocess.run(
        [
            *prefix,
            sys.executable,
            "-B",
            str(repo_root / "scripts" / SCRIPT_PATH.name),
            "--help",
        ],
        cwd=repo_root,
        env=child_environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_installer_script_rejects_preloaded_foreign_installer_before_call(tmp_path):
    marker = tmp_path / "ambient-called"
    wrapper = """\
import os
from pathlib import Path
import runpy
import sys
import types

package = types.ModuleType("codex_usage")
package.__file__ = "/foreign/codex_usage/__init__.py"
package.__path__ = ["/foreign/codex_usage"]
installer = types.ModuleType("codex_usage.integration_installer")
installer.__file__ = "/foreign/codex_usage/integration_installer.py"
installer.IntegrationCleanupError = type("IntegrationCleanupError", (Exception,), {})
installer.IntegrationInstallError = type("IntegrationInstallError", (Exception,), {})

def ambient_install(**_kwargs):
    Path(os.environ["AMBIENT_CALL_MARKER"]).write_text("called")

installer.install_release = ambient_install
installer.rollback_active_release = ambient_install
sys.modules["codex_usage"] = package
sys.modules["codex_usage.integration_installer"] = installer
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            wrapper,
            str(SCRIPT_PATH),
            "--source-root",
            str(tmp_path / "source"),
            "--state-home",
            str(tmp_path / "state"),
            "--data-home",
            str(tmp_path / "data"),
            "--python",
            sys.executable,
            "--temporary-root",
            str(tmp_path / "temporary"),
        ],
        cwd=tmp_path,
        env={
            "AMBIENT_CALL_MARKER": str(marker),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 69
    assert completed.stdout == ""
    assert completed.stderr == "integration_producer_unavailable\n"
    assert not marker.exists()


def test_installer_script_rejects_preloaded_installer_spoofing_local_origins(tmp_path):
    repo_root = _temporary_bootstrap_repo(tmp_path)
    marker = tmp_path / "spoofed-called"
    wrapper = """\
import os
from pathlib import Path
import runpy
import sys
import types

script = Path(sys.argv[1])
package_root = script.parents[1] / "src" / "codex_usage"
package = types.ModuleType("codex_usage")
package.__file__ = str(package_root / "__init__.py")
package.__path__ = [str(package_root)]
installer = types.ModuleType("codex_usage.integration_installer")
installer.__file__ = str(package_root / "integration_installer.py")
installer.IntegrationCleanupError = type("IntegrationCleanupError", (Exception,), {})
installer.IntegrationInstallError = type("IntegrationInstallError", (Exception,), {})

def spoofed_install(**_kwargs):
    Path(os.environ["SPOOFED_CALL_MARKER"]).write_text("called")

installer.install_release = spoofed_install
installer.rollback_active_release = spoofed_install
sys.modules["codex_usage"] = package
sys.modules["codex_usage.integration_installer"] = installer
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            wrapper,
            str(repo_root / "scripts" / SCRIPT_PATH.name),
            "--source-root",
            str(tmp_path / "source-root"),
            "--state-home",
            str(tmp_path / "state"),
            "--data-home",
            str(tmp_path / "data"),
            "--python",
            sys.executable,
            "--temporary-root",
            str(tmp_path / "temporary"),
        ],
        cwd=repo_root,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SPOOFED_CALL_MARKER": str(marker),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 69
    assert completed.stdout == ""
    assert completed.stderr == "integration_producer_unavailable\n"
    assert not marker.exists()


def test_installer_script_rejects_any_preloaded_package_namespace_key(tmp_path):
    repo_root = _temporary_bootstrap_repo(tmp_path)
    wrapper = """\
import runpy
import sys
import types

sys.modules["codex_usage.unrelated"] = types.ModuleType("codex_usage.unrelated")
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            wrapper,
            str(repo_root / "scripts" / SCRIPT_PATH.name),
            "--help",
        ],
        cwd=repo_root,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 69
    assert completed.stdout == ""
    assert completed.stderr == "integration_producer_unavailable\n"


def test_installer_script_imports_never_reach_hostile_meta_path_finder(tmp_path):
    repo_root = _temporary_bootstrap_repo(tmp_path)
    marker = tmp_path / "meta-finder-executed"
    wrapper = """\
import importlib.machinery
import os
from pathlib import Path
import runpy
import sys

script = Path(sys.argv[1])
package_root = script.parents[1] / "src" / "codex_usage"

class HostileLoader:
    def __init__(self, fullname, source_path):
        self.fullname = fullname
        self.source_path = source_path

    def create_module(self, _spec):
        return None

    def exec_module(self, module):
        Path(os.environ["META_FINDER_MARKER"]).write_text("executed")
        module.__file__ = str(self.source_path)
        if self.fullname == "codex_usage.integration_installer":
            module.IntegrationCleanupError = type("IntegrationCleanupError", (Exception,), {})
            module.IntegrationInstallError = type("IntegrationInstallError", (Exception,), {})
            module.install_release = lambda **_kwargs: None
            module.rollback_active_release = lambda **_kwargs: None

class HostileFinder:
    def find_spec(self, fullname, _path=None, _target=None):
        if fullname != "codex_usage" and not fullname.startswith("codex_usage."):
            return None
        source_path = package_root / (
            "__init__.py" if fullname == "codex_usage" else fullname.rsplit(".", 1)[1] + ".py"
        )
        loader = HostileLoader(fullname, source_path)
        spec = importlib.machinery.ModuleSpec(
            fullname,
            loader,
            origin=str(source_path),
            is_package=fullname == "codex_usage",
        )
        spec.has_location = True
        if fullname == "codex_usage":
            spec.submodule_search_locations = [str(package_root)]
        return spec

sys.meta_path.insert(0, HostileFinder())
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            wrapper,
            str(repo_root / "scripts" / SCRIPT_PATH.name),
            "--help",
        ],
        cwd=repo_root,
        env={
            "META_FINDER_MARKER": str(marker),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert not marker.exists()


def test_installer_script_keeps_exact_loader_guard_for_complete_closure(tmp_path):
    repo_root = _temporary_bootstrap_repo(tmp_path)
    marker = tmp_path / "restored-meta-finder-reached"
    wrapper = """\
import hashlib
import importlib
import os
from pathlib import Path
import runpy
import sys

class AmbientFinder:
    def find_spec(self, fullname, _path=None, _target=None):
        if fullname == "codex_usage" or fullname.startswith("codex_usage."):
            Path(os.environ["RESTORED_META_FINDER_MARKER"]).write_text("reached")
        return None

ambient = AmbientFinder()
sys.meta_path.insert(0, ambient)
namespace = runpy.run_path(sys.argv[1], run_name="synthetic_installer")
finder = namespace["_SOURCE_FINDER"]
expected = namespace["_EXPECTED_MODULES"]
loader_type = namespace["_ValidatedSourceLoader"]
assert sys.meta_path[0] is finder
assert sys.meta_path[1] is ambient
for fullname, (expected_path, _identity) in expected.items():
    module = sys.modules[fullname]
    loader = module.__spec__.loader
    assert type(loader) is loader_type
    assert module.__loader__ is loader
    assert loader is finder.loader_for(fullname)
    assert loader.path == str(expected_path)
    assert loader._digest == hashlib.sha256(loader._payload).digest()
try:
    importlib.import_module("codex_usage.undeclared")
except ImportError:
    pass
else:
    raise AssertionError("undeclared package import escaped trusted guard")
assert not Path(os.environ["RESTORED_META_FINDER_MARKER"]).exists()
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            wrapper,
            str(repo_root / "scripts" / SCRIPT_PATH.name),
        ],
        cwd=repo_root,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "RESTORED_META_FINDER_MARKER": str(marker),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert not marker.exists()


@pytest.mark.parametrize("cache_kind", ("directory", "loose-pyc"))
def test_installer_script_rejects_source_bytecode_cache_without_deleting_it(
    tmp_path, cache_kind
):
    repo_root = _temporary_bootstrap_repo(tmp_path)
    package_root = repo_root / "src" / "codex_usage"
    if cache_kind == "directory":
        cache_path = package_root / "__pycache__"
        cache_path.mkdir(mode=0o755)
    else:
        cache_path = package_root / "private_io.pyc"
        cache_path.write_bytes(b"attacker-bytecode")
        cache_path.chmod(0o644)

    completed = _run_bootstrap_help(repo_root)

    assert completed.returncode == 69
    assert completed.stdout == ""
    assert completed.stderr == "integration_producer_unavailable\n"
    assert cache_path.exists()


def test_installer_script_never_executes_valid_timestamp_bytecode(tmp_path):
    repo_root = _temporary_bootstrap_repo(tmp_path)
    marker = tmp_path / "bytecode-executed"
    source = repo_root / "src" / "codex_usage" / "integration_attestation.py"
    source_stat = source.stat()
    attacker = compile(
        "from pathlib import Path\n"
        "import os\n"
        'Path(os.environ["BYTECODE_EXECUTION_MARKER"]).write_text("executed")\n',
        str(source),
        "exec",
    )
    bytecode = (
        importlib.util.MAGIC_NUMBER
        + struct.pack(
            "<III",
            0,
            int(source_stat.st_mtime) & 0xFFFFFFFF,
            source_stat.st_size & 0xFFFFFFFF,
        )
        + marshal.dumps(attacker)
    )
    cache_path = Path(importlib.util.cache_from_source(str(source)))
    cache_path.parent.mkdir(mode=0o755)
    cache_path.write_bytes(bytecode)
    cache_path.chmod(0o644)

    completed = _run_bootstrap_help(
        repo_root,
        environment={"BYTECODE_EXECUTION_MARKER": str(marker)},
    )

    assert completed.returncode == 69
    assert completed.stdout == ""
    assert completed.stderr == "integration_producer_unavailable\n"
    assert not marker.exists()
    assert cache_path.exists()


def test_installer_script_requires_bytecode_environment_even_with_dash_b(tmp_path):
    repo_root = _temporary_bootstrap_repo(tmp_path)

    completed = _run_bootstrap_help(
        repo_root,
        include_bytecode_environment=False,
    )

    assert completed.returncode == 69
    assert completed.stdout == ""
    assert completed.stderr == "integration_producer_unavailable\n"


def test_installer_script_rejects_external_pycache_prefix(tmp_path):
    repo_root = _temporary_bootstrap_repo(tmp_path)
    external_cache = tmp_path / "external-pycache"

    completed = _run_bootstrap_help(
        repo_root,
        environment={"PYTHONPYCACHEPREFIX": str(external_cache)},
    )

    assert completed.returncode == 69
    assert completed.stdout == ""
    assert completed.stderr == "integration_producer_unavailable\n"
    assert not external_cache.exists()


@pytest.mark.parametrize("module_name", _BOOTSTRAP_TRANSITIVE_MODULES)
def test_installer_script_rejects_symlinked_import_closure_before_execution(
    tmp_path, module_name
):
    repo_root = _temporary_bootstrap_repo(tmp_path)
    marker = tmp_path / "symlink-executed"
    foreign = tmp_path / f"foreign-{module_name}"
    foreign.write_text(
        "from pathlib import Path\n"
        "import os\n"
        'Path(os.environ["SYMLINK_EXECUTION_MARKER"]).write_text("executed")\n',
        encoding="utf-8",
    )
    target = repo_root / "src" / "codex_usage" / module_name
    target.unlink()
    target.symlink_to(foreign)

    completed = _run_bootstrap_help(
        repo_root,
        environment={"SYMLINK_EXECUTION_MARKER": str(marker)},
    )

    assert completed.returncode == 69
    assert completed.stdout == ""
    assert completed.stderr == "integration_producer_unavailable\n"
    assert not marker.exists()


def test_installer_script_rejects_world_writable_import_closure(tmp_path):
    repo_root = _temporary_bootstrap_repo(tmp_path)
    (repo_root / "src/codex_usage/private_io.py").chmod(0o666)

    completed = _run_bootstrap_help(repo_root)

    assert completed.returncode == 69
    assert completed.stdout == ""
    assert completed.stderr == "integration_producer_unavailable\n"


def test_installer_script_rejects_foreign_uid_import_closure(tmp_path, pytestconfig):
    from codex_usage import private_io

    repo_root = _temporary_bootstrap_repo(tmp_path)
    root_owned_source = Path("/usr/lib64/python3.14/__future__.py")
    root_owned_stat = root_owned_source.stat()
    assert root_owned_stat.st_uid != os.geteuid()
    assert stat.S_IMODE(root_owned_stat.st_mode) == 0o644
    target = repo_root / "src/codex_usage/integration_entrypoint.py"
    completed = _run_bootstrap_help(
        repo_root,
        prefix=(
            "/usr/bin/bwrap",
            "--bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--ro-bind",
            str(root_owned_source),
            str(target),
            "--bind",
            str(private_io._private_lock_root()),
            str(pytestconfig._private_lock_production_root),
            "--",
        ),
    )

    assert completed.returncode == 69
    assert completed.stdout == ""
    assert completed.stderr == "integration_producer_unavailable\n"


def test_candidate_manifest_is_single_final_only_write_with_real_treehash(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    candidate_writes: list[dict[str, object]] = []
    rename_snapshots: list[dict[str, object]] = []
    original_write = integration_installer._write_exclusive
    original_rename = integration_installer._rename_noreplace

    def capture_write(path, payload, **kwargs):
        if Path(path).name.startswith("candidate-"):
            candidate_writes.append(json.loads(payload))
        return original_write(path, payload, **kwargs)

    def capture_rename(source_name, target_name, parent_fd):
        if ".staging-" in source_name:
            candidates = sorted(tmp_path.rglob("candidate-*.json"))
            assert len(candidates) == 1
            rename_snapshots.append(json.loads(candidates[0].read_text(encoding="utf-8")))
        return original_rename(source_name, target_name, parent_fd)

    monkeypatch.setattr(integration_installer, "_write_exclusive", capture_write)
    monkeypatch.setattr(integration_installer, "_rename_noreplace", capture_rename)
    release, _, _ = _install(tmp_path)

    assert len(candidate_writes) == 1
    assert len(rename_snapshots) == 1
    candidate = rename_snapshots[0]
    for key in ("release_dir", "launcher_path", "entrypoint_path", "wheel_path", "record_path"):
        assert str(release.release_dir) in candidate[key]
        assert ".staging-" not in candidate[key]
        assert "temporary" not in candidate[key]
    assert candidate["release_tree_sha256"] == release.release_tree_sha256


@pytest.mark.parametrize("mutation", ["unknown", "secret-like", "missing"])
def test_candidate_manifest_requires_exact_canonical_fields(
    tmp_path,
    monkeypatch,
    mutation,
):
    from codex_usage import integration_installer

    original_manifest = integration_installer._manifest
    original_rename = integration_installer._rename_noreplace
    staging_renames = []

    def mutated_manifest(**kwargs):
        return _mutate_manifest_fields(original_manifest(**kwargs), mutation)

    def capture_rename(source_name, target_name, parent_fd):
        if ".staging-" in source_name:
            staging_renames.append((source_name, target_name))
        return original_rename(source_name, target_name, parent_fd)

    monkeypatch.setattr(integration_installer, "_manifest", mutated_manifest)
    monkeypatch.setattr(integration_installer, "_rename_noreplace", capture_rename)
    with pytest.raises(integration_installer.IntegrationInstallError):
        _install(tmp_path)
    assert staging_renames == []


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

        def replace_build(source, build, **kwargs):
            result = original_copy(source, build, **kwargs)
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
    original_unlink = os.unlink

    def fail_candidate_unlink(path, *args, **kwargs):
        if Path(path).name.startswith("candidate-"):
            raise OSError("synthetic candidate cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(integration_installer.os, "unlink", fail_candidate_unlink)
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
    original_mkdir = os.mkdir
    build_created = False

    def fail_wheel_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal build_created
        name = Path(path).name
        if name.startswith("producer-build-"):
            if dir_fd is None:
                result = original_mkdir(path, mode)
            else:
                result = original_mkdir(path, mode, dir_fd=dir_fd)
            build_created = True
            return result
        if name.startswith("producer-wheel-"):
            assert build_created
            raise OSError("synthetic wheel create failure")
        if dir_fd is None:
            return original_mkdir(path, mode)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(integration_installer.os, "mkdir", fail_wheel_mkdir)
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
def test_post_create_mode_change_failure_cleans_only_new_target(
    tmp_path, monkeypatch, kind
):
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
            not fired
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


def test_owned_file_cleanup_rejects_parent_swap_before_unlink(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    target = parent / "candidate.json"
    target.write_bytes(b"owned")
    target.chmod(0o600)
    parent_identity = integration_installer._directory_identity(parent)
    identity = integration_installer._file_identity(target)
    old_parent = tmp_path / "parent-old"
    original_unlink = Path.unlink
    original_open = os.open
    swapped = False

    def swap_parent():
        nonlocal swapped
        if swapped:
            return
        parent.rename(old_parent)
        parent.mkdir(mode=0o700)
        foreign = parent / target.name
        foreign.write_bytes(b"foreign")
        foreign.chmod(0o600)
        swapped = True

    def swap_before_unlink(path, *args, **kwargs):
        if path == target:
            swap_parent()
        return original_unlink(path, *args, **kwargs)

    def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == parent and dir_fd is None:
            swap_parent()
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(Path, "unlink", swap_before_unlink)
    monkeypatch.setattr(integration_installer.os, "open", swap_before_open)
    assert (
        integration_installer._cleanup_owned_file(target, identity, parent_identity)
        is False
    )

    assert swapped
    assert (parent / target.name).read_bytes() == b"foreign"
    assert (old_parent / target.name).read_bytes() == b"owned"


def test_owned_file_cleanup_rejects_replaced_entry_before_unlink(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    target = parent / "candidate.json"
    target.write_bytes(b"owned")
    target.chmod(0o600)
    parent_identity = integration_installer._directory_identity(parent)
    identity = integration_installer._file_identity(target)
    calls = 0
    original_stat = os.stat

    def replace_before_unlink(name, *args, **kwargs):
        nonlocal calls
        if name == target.name and kwargs.get("dir_fd") is not None:
            calls += 1
            if calls == 2:
                target.unlink()
                target.write_bytes(b"foreign")
                target.chmod(0o600)
        return original_stat(name, *args, **kwargs)

    monkeypatch.setattr(integration_installer.os, "stat", replace_before_unlink)
    assert integration_installer._cleanup_owned_file(
        target, identity, parent_identity
    ) is False

    assert calls == 2
    assert target.read_bytes() == b"foreign"


def test_owned_file_cleanup_rejects_group_owner_drift_before_unlink(
    tmp_path,
    monkeypatch,
):
    """Would fail if installer file identity ignored gid changes."""
    from codex_usage import integration_installer

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    target = parent / "target"
    target.write_bytes(b"payload")
    target.chmod(0o600)
    parent_identity = integration_installer._directory_identity(parent)
    identity = integration_installer._file_identity(target)
    real_stat = integration_installer.os.stat

    def stat_with_group_owner_drift(path, *args, **kwargs):
        item = real_stat(path, *args, **kwargs)
        if kwargs.get("dir_fd") is not None and path == target.name:
            values = list(item)
            values[5] = item.st_gid + 1
            return os.stat_result(values)
        return item

    monkeypatch.setattr(integration_installer.os, "stat", stat_with_group_owner_drift)

    assert not integration_installer._cleanup_owned_file(
        target,
        identity,
        parent_identity,
    )
    assert target.exists()


def test_provisional_directory_cleanup_rejects_parent_swap_before_rmdir(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    target = parent / "staging"
    target.mkdir(mode=0o700)
    parent_identity = integration_installer._directory_identity(parent)
    identity = integration_installer._provisional_path_identity(target, directory=True)
    old_parent = tmp_path / "parent-old"
    original_rmdir = Path.rmdir
    original_open = os.open
    swapped = False

    def swap_parent():
        nonlocal swapped
        if swapped:
            return
        parent.rename(old_parent)
        parent.mkdir(mode=0o700)
        (parent / target.name).mkdir(mode=0o700)
        swapped = True

    def swap_before_rmdir(path, *args, **kwargs):
        if path == target:
            swap_parent()
        return original_rmdir(path, *args, **kwargs)

    def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == parent and dir_fd is None:
            swap_parent()
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(Path, "rmdir", swap_before_rmdir)
    monkeypatch.setattr(integration_installer.os, "open", swap_before_open)
    assert (
        integration_installer._cleanup_provisional(
            target,
            identity,
            parent_identity,
            directory=True,
        )
        is False
    )

    assert swapped
    assert (parent / target.name).is_dir()
    assert (old_parent / target.name).is_dir()


def test_owned_directory_rename_rejects_parent_swap_before_rename(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    parent = tmp_path / "releases"
    parent.mkdir(mode=0o700)
    staging = parent / "staging"
    staging.mkdir(mode=0o700)
    (staging / "owned-marker").write_bytes(b"owned")
    final = parent / "final"
    parent_identity = integration_installer._directory_identity(parent)
    staging_identity = integration_installer._directory_identity(staging)
    old_parent = tmp_path / "releases-old"
    original_rename = Path.rename
    original_open = os.open
    swapped = False

    def swap_parent():
        nonlocal swapped
        if swapped:
            return
        parent.rename(old_parent)
        parent.mkdir(mode=0o700)
        foreign_staging = parent / staging.name
        foreign_staging.mkdir(mode=0o700)
        (foreign_staging / "foreign-marker").write_bytes(b"foreign")
        swapped = True

    def swap_before_rename(source, target):
        if source == staging:
            swap_parent()
        return original_rename(source, target)

    def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == parent and dir_fd is None:
            swap_parent()
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(Path, "rename", swap_before_rename)
    monkeypatch.setattr(integration_installer.os, "open", swap_before_open)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._rename_owned_directory(
            staging,
            final,
            parent_identity,
            staging_identity,
        )

    assert swapped
    assert (parent / staging.name / "foreign-marker").read_bytes() == b"foreign"
    assert (old_parent / staging.name / "owned-marker").read_bytes() == b"owned"


def test_rename_noreplace_preserves_existing_target(tmp_path):
    from codex_usage import integration_installer

    parent = tmp_path / "releases"
    parent.mkdir(mode=0o700)
    source = parent / "staging"
    source.mkdir(mode=0o700)
    (source / "owned-marker").write_bytes(b"owned")
    target = parent / "final"
    target.mkdir(mode=0o700)
    (target / "foreign-marker").write_bytes(b"foreign")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_fd = os.open(parent, flags)
    try:
        with pytest.raises(OSError) as error:
            integration_installer._rename_noreplace("staging", "final", parent_fd)
    finally:
        os.close(parent_fd)

    assert error.value.errno == errno.EEXIST
    assert (source / "owned-marker").read_bytes() == b"owned"
    assert (target / "foreign-marker").read_bytes() == b"foreign"


def test_owned_directory_rename_does_not_overwrite_target_created_at_rename(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    parent = tmp_path / "releases"
    parent.mkdir(mode=0o700)
    source = parent / "staging"
    source.mkdir(mode=0o700)
    (source / "owned-marker").write_bytes(b"owned")
    target = parent / "final"
    parent_identity = integration_installer._directory_identity(parent)
    source_identity = integration_installer._directory_identity(source)
    original_rename = integration_installer._rename_noreplace
    created = False

    def create_target_then_rename(source_name, target_name, parent_fd):
        nonlocal created
        target.mkdir(mode=0o700)
        (target / "foreign-marker").write_bytes(b"foreign")
        created = True
        return original_rename(source_name, target_name, parent_fd)

    monkeypatch.setattr(
        integration_installer, "_rename_noreplace", create_target_then_rename
    )
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._rename_owned_directory(
            source,
            target,
            parent_identity,
            source_identity,
        )

    assert created
    assert (source / "owned-marker").read_bytes() == b"owned"
    assert (target / "foreign-marker").read_bytes() == b"foreign"


def test_owned_directory_rename_rejects_replaced_source_before_rename(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    parent = tmp_path / "releases"
    parent.mkdir(mode=0o700)
    source = parent / "staging"
    source.mkdir(mode=0o700)
    (source / "owned-marker").write_bytes(b"owned")
    target = parent / "final"
    parent_identity = integration_installer._directory_identity(parent)
    source_identity = integration_installer._directory_identity(source)
    old_source = parent / "staging-old"
    calls = 0
    original_stat = os.stat

    def replace_before_rename(name, *args, **kwargs):
        nonlocal calls
        if name == source.name and kwargs.get("dir_fd") is not None:
            calls += 1
            if calls == 2:
                source.rename(old_source)
                source.mkdir(mode=0o700)
                (source / "foreign-marker").write_bytes(b"foreign")
        return original_stat(name, *args, **kwargs)

    monkeypatch.setattr(integration_installer.os, "stat", replace_before_rename)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._rename_owned_directory(
            source,
            target,
            parent_identity,
            source_identity,
        )

    assert calls == 2
    assert (old_source / "owned-marker").read_bytes() == b"owned"
    assert (source / "foreign-marker").read_bytes() == b"foreign"
    assert not target.exists()


def test_owned_directory_cleanup_rejects_parent_swap_before_rmtree(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    parent = tmp_path / "temporary"
    parent.mkdir(mode=0o700)
    target = parent / "producer-build"
    target.mkdir(mode=0o700)
    (target / "owned-marker").write_bytes(b"owned")
    parent_identity = integration_installer._directory_identity(parent)
    identity = integration_installer._directory_identity(target)
    old_parent = tmp_path / "temporary-old"
    original_rmtree = integration_installer.shutil.rmtree
    original_open = os.open
    swapped = False

    def swap_parent():
        nonlocal swapped
        if swapped:
            return
        parent.rename(old_parent)
        parent.mkdir(mode=0o700)
        foreign = parent / target.name
        foreign.mkdir(mode=0o700)
        (foreign / "foreign-marker").write_bytes(b"foreign")
        swapped = True

    def swap_before_rmtree(path, *args, **kwargs):
        if Path(path) == target:
            swap_parent()
        return original_rmtree(path, *args, **kwargs)

    def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == parent and dir_fd is None:
            swap_parent()
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(integration_installer.shutil, "rmtree", swap_before_rmtree)
    monkeypatch.setattr(integration_installer.os, "open", swap_before_open)
    assert (
        integration_installer._cleanup_owned_directory(
            target,
            identity,
            parent_identity,
        )
        is False
    )

    assert swapped
    assert (parent / target.name / "foreign-marker").read_bytes() == b"foreign"
    assert (old_parent / target.name / "owned-marker").read_bytes() == b"owned"


def test_owned_directory_cleanup_rejects_foreign_owner(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    parent = tmp_path / "temporary"
    parent.mkdir(mode=0o700)
    target = parent / "producer-build"
    target.mkdir(mode=0o700)
    parent_identity = integration_installer._directory_identity(parent)
    identity = integration_installer._directory_identity(target)
    item = target.lstat()
    foreign = SimpleNamespace(
        st_dev=item.st_dev,
        st_ino=item.st_ino,
        st_uid=os.getuid() + 1,
        st_mode=item.st_mode,
        st_nlink=item.st_nlink,
    )
    original_stat = integration_installer.os.stat
    original_rmtree = integration_installer.shutil.rmtree
    removed = False

    def foreign_stat(name, *args, **kwargs):
        if name == target.name and kwargs.get("dir_fd") is not None:
            return foreign
        return original_stat(name, *args, **kwargs)

    def record_rmtree(*args, **kwargs):
        nonlocal removed
        removed = True
        return original_rmtree(*args, **kwargs)

    monkeypatch.setattr(integration_installer.os, "stat", foreign_stat)
    monkeypatch.setattr(integration_installer.shutil, "rmtree", record_rmtree)
    assert (
        integration_installer._remove_owned_entry(
            target,
            identity,
            parent_identity,
            directory=True,
            recursive=True,
        )
        is False
    )

    assert not removed
    assert target.is_dir()


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


def test_exclusive_write_rejects_candidate_replaced_after_final_revalidation(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    candidate = parent / "candidate.json"
    old_candidate = parent / "candidate-old"
    marker = b"foreign-candidate-marker"
    original_rebased = integration_installer._provisional_rebased
    calls = 0

    def replace_after_final_revalidation(path, identity, parent_identity, *, directory):
        nonlocal calls
        result = original_rebased(
            path,
            identity,
            parent_identity,
            directory=directory,
        )
        if path == candidate:
            calls += 1
            if calls == 2:
                candidate.rename(old_candidate)
                candidate.write_bytes(marker)
                candidate.chmod(0o600)
        return result

    monkeypatch.setattr(
        integration_installer,
        "_provisional_rebased",
        replace_after_final_revalidation,
    )
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._write_exclusive(
            candidate,
            b"candidate-payload",
            mode=0o600,
        )

    assert calls >= 3
    assert candidate.read_bytes() == marker
    assert old_candidate.read_bytes() == b"candidate-payload"


def test_write_launcher_binds_parent_identity_before_write(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    parent = tmp_path / "bin"
    parent.mkdir(mode=0o700)
    launcher = parent / "codex-usage"
    old_parent = tmp_path / "bin-old"
    foreign_parent = tmp_path / "bin-foreign"
    foreign_parent.mkdir(mode=0o700)
    parent_identity = integration_installer._directory_identity(parent)
    original_write = integration_installer._write_exclusive
    swapped = False

    def swap_before_write(path, payload, **kwargs):
        nonlocal swapped
        parent.rename(old_parent)
        foreign_parent.rename(parent)
        swapped = True
        return original_write(path, payload, **kwargs)

    monkeypatch.setattr(integration_installer, "_write_exclusive", swap_before_write)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._write_launcher(
            path=launcher,
            final_release_dir=tmp_path / "release",
            data_home=tmp_path / "data",
            state_home=tmp_path / "state",
            parent_identity=parent_identity,
        )

    assert swapped
    assert not (parent / launcher.name).exists()
    assert not (old_parent / launcher.name).exists()


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
    original_rename = integration_installer._rename_noreplace
    original_read_manifest = integration_installer._read_manifest

    def tamper_before_seam(candidate_path):
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        candidate["release_dir"] = str(candidate_path.parent)
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        return original_read_manifest(candidate_path)

    def capture_rename(source_name, target_name, parent_fd):
        rename_calls.append((Path(source_name), Path(target_name)))
        return original_rename(source_name, target_name, parent_fd)

    monkeypatch.setattr(integration_installer, "_read_manifest", tamper_before_seam)
    monkeypatch.setattr(integration_installer, "_rename_noreplace", capture_rename)
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


def test_public_rollback_fails_before_bootstrap_write_or_attestation(tmp_path, monkeypatch):
    from codex_usage import integration_installer
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
    active_before = active_path.read_bytes()
    previous_path = integration / "previous.json"
    previous_before = previous_path.read_bytes()
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
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer.rollback_active_release(
            state_home=state_home,
            data_home=data_home,
        )
    assert events == []
    assert active_path.read_bytes() == active_before
    assert previous_path.read_bytes() == previous_before
    assert first.release_dir.is_dir()


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


def test_python_executable_owner_allows_only_bounded_unmapped_root_overflow(
    monkeypatch,
):
    """Would fail if the outer user namespace accepted arbitrary nobody-owned Python."""
    from codex_usage import integration_installer

    monkeypatch.setattr(integration_installer.os, "getuid", lambda: 1000)
    monkeypatch.setattr(integration_installer, "_kernel_overflow_uid", lambda: 65534)

    def fake_lstat(path):
        normalized = Path(path)
        if normalized in {Path("/usr"), Path("/usr/bin")}:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o755,
                st_uid=65534,
            )
        raise FileNotFoundError(path)

    monkeypatch.setattr(integration_installer.Path, "lstat", fake_lstat)

    assert integration_installer._python_executable_owner_is_allowed(
        Path("/usr/bin/python3.14"),
        65534,
    )
    assert integration_installer._python_executable_owner_is_allowed(
        Path("/usr/bin/python3.14"),
        0,
    )
    assert integration_installer._python_executable_owner_is_allowed(
        Path("/tmp/user-python"),
        1000,
    )
    assert not integration_installer._python_executable_owner_is_allowed(
        Path("/tmp/python"),
        65534,
    )
    assert not integration_installer._python_executable_owner_is_allowed(
        Path("/usr/local/bin/python"),
        65534,
    )


def test_python_executable_owner_rejects_overflow_when_system_root_is_mapped(
    monkeypatch,
):
    """Would fail if normal-host nobody ownership was treated as root-owned Python."""
    from codex_usage import integration_installer

    monkeypatch.setattr(integration_installer.os, "getuid", lambda: 1000)
    monkeypatch.setattr(integration_installer, "_kernel_overflow_uid", lambda: 65534)

    def fake_lstat(path):
        normalized = Path(path)
        if normalized in {Path("/usr"), Path("/usr/bin")}:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o755,
                st_uid=0,
            )
        raise FileNotFoundError(path)

    monkeypatch.setattr(integration_installer.Path, "lstat", fake_lstat)

    assert not integration_installer._python_executable_owner_is_allowed(
        Path("/usr/bin/python3.14"),
        65534,
    )


def test_kernel_overflow_uid_reader_accepts_only_bounded_decimal(
    tmp_path,
    monkeypatch,
):
    """Would fail if malformed overflow-uid state could widen interpreter owners."""
    from codex_usage import integration_installer

    source = tmp_path / "overflowuid"
    monkeypatch.setattr(integration_installer, "_KERNEL_OVERFLOW_UID_PATH", source)

    source.write_text("65534\n", encoding="ascii")
    assert integration_installer._kernel_overflow_uid() == 65534

    source.write_text("not-a-uid\n", encoding="ascii")
    assert integration_installer._kernel_overflow_uid() == -1

    source.write_text(str(2**32) + "\n", encoding="ascii")
    assert integration_installer._kernel_overflow_uid() == -1


def test_interpreter_resolver_rejects_target_replacement_before_return(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer

    target = tmp_path / "python-target"
    target.write_bytes(b"owned executable")
    target.chmod(0o700)
    old_target = tmp_path / "python-target-old"
    replaced = False
    original_access = os.access

    def replace_before_access(path, mode):
        nonlocal replaced
        if path == target and not replaced:
            target.rename(old_target)
            target.write_bytes(b"foreign executable")
            target.chmod(0o700)
            replaced = True
        return original_access(path, mode)

    monkeypatch.setattr(integration_installer.os, "access", replace_before_access)
    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._resolve_python_executable(target)

    assert replaced
    assert old_target.read_bytes() == b"owned executable"
    assert target.read_bytes() == b"foreign executable"


def test_build_verified_wheel_rejects_python_swap_after_resolve_before_spawn(
    tmp_path,
    monkeypatch,
):
    """Would fail if a resolved interpreter identity was not carried to spawn."""
    from codex_usage import integration_installer

    target = tmp_path / "python-target"
    target.write_bytes(b"owned executable")
    target.chmod(0o700)
    resolved = integration_installer._resolve_python_executable(target)
    old_target = tmp_path / "python-target-old"
    target.rename(old_target)
    target.write_bytes(b"replacement executable")
    target.chmod(0o700)
    spawned: list[tuple[str, ...]] = []

    class FakeProcess:
        pid = 4321

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

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
    monkeypatch.setattr(
        integration_installer.subprocess,
        "Popen",
        lambda argv, **_kwargs: spawned.append(tuple(argv)) or FakeProcess(),
    )

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._build_verified_wheel(
            build_root=tmp_path,
            python_executable=resolved,
            wheel_dir=tmp_path / "wheel",
            environment=integration_installer._sanitized_build_environment(),
        )

    assert spawned == []
    assert old_target.read_bytes() == b"owned executable"
    assert target.read_bytes() == b"replacement executable"


def test_python_spawn_revalidation_rejects_in_place_byte_change(
    tmp_path,
):
    """Would fail if Python binding omitted executable content and timestamps."""
    from codex_usage import integration_installer

    target = tmp_path / "python-target"
    target.write_bytes(b"owned executable")
    target.chmod(0o700)
    resolved = integration_installer._resolve_python_executable(target)
    target.write_bytes(b"evil executable!!")
    target.chmod(0o700)

    with pytest.raises(integration_installer.IntegrationInstallError):
        integration_installer._revalidate_python_executable_for_spawn(resolved)


def test_builder_preflight_execs_fd_bound_python_after_spawn_race(
    tmp_path,
    monkeypatch,
):
    """Would fail if Popen reopened a revalidated Python executable by path."""
    from codex_usage import integration_installer

    target = tmp_path / "python-target"
    target.write_bytes(b"owned executable")
    target.chmod(0o700)
    resolved = integration_installer._resolve_python_executable(target)
    old_target = tmp_path / "python-target-old"
    spawned: list[tuple[tuple[str, ...], tuple[int, ...]]] = []

    class FakeProcess:
        pid = 4321

        def __init__(self) -> None:
            read_fd, write_fd = os.pipe()
            os.write(
                write_fd,
                b'{"backend":"setuptools.command.bdist_wheel.bdist_wheel",'
                b'"setuptools":"80.10.2"}\n',
            )
            os.close(write_fd)
            self.stdout = os.fdopen(read_fd, "rb")

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            return None

    def race_path_before_spawn(argv, **kwargs):
        target.rename(old_target)
        target.write_bytes(b"replacement executable")
        target.chmod(0o700)
        spawned.append((tuple(argv), tuple(kwargs.get("pass_fds", ()))))
        return FakeProcess()

    monkeypatch.setattr(integration_installer.subprocess, "Popen", race_path_before_spawn)

    result = integration_installer._run_builder_preflight(
        python_executable=resolved,
        environment=integration_installer._sanitized_build_environment(),
    )

    assert result.returncode == 0
    assert spawned
    argv, pass_fds = spawned[0]
    assert argv[0].startswith("/proc/self/fd/")
    assert int(argv[0].rsplit("/", 1)[1]) in pass_fds
    assert old_target.read_bytes() == b"owned executable"
    assert target.read_bytes() == b"replacement executable"


def test_create_private_virtualenv_execs_fd_bound_python_after_path_shadow(
    tmp_path,
    monkeypatch,
):
    """Would fail if venv creation reopened sys.executable by path at spawn."""
    from codex_usage import integration_installer as module

    python_path = tmp_path / "python"
    detached_python = tmp_path / "python-detached"
    marker = tmp_path / "venv-python-marker"
    venv_root = tmp_path / "venv"
    real_python = shlex.quote(sys.executable)
    python_path.write_text(
        "#!/bin/sh\n"
        "printf 'trusted\\n' > \"$CODEX_USAGE_SPAWN_MARKER\"\n"
        f"exec {real_python} \"$@\"\n",
        encoding="utf-8",
    )
    python_path.chmod(0o700)
    environment = module._sanitized_build_environment()
    environment["CODEX_USAGE_SPAWN_MARKER"] = str(marker)
    monkeypatch.setattr(module.sys, "executable", str(python_path))
    monkeypatch.setattr(module, "_sanitized_build_environment", lambda: dict(environment))
    swapped = False

    def shadow_python(_python_fd_path: str) -> None:
        nonlocal swapped
        python_path.rename(detached_python)
        python_path.write_text(
            "#!/bin/sh\n"
            "printf 'shadow\\n' > \"$CODEX_USAGE_SPAWN_MARKER\"\n"
            "exit 0\n",
            encoding="utf-8",
        )
        python_path.chmod(0o700)
        swapped = True

    monkeypatch.setattr(
        module,
        "_before_private_virtualenv_builder_exec",
        shadow_python,
        raising=False,
    )

    module._create_private_virtualenv(venv_root)

    assert swapped
    assert marker.read_text(encoding="utf-8") == "trusted\n"
    assert (venv_root / "pyvenv.cfg").is_file()
    assert detached_python.read_text(encoding="utf-8").startswith("#!/bin/sh")


def test_build_verified_wheel_execs_fd_bound_python_after_path_shadow(
    tmp_path,
    monkeypatch,
):
    """Would fail if wheel build reopened the validated interpreter path."""
    from codex_usage import integration_installer as module

    python_path = tmp_path / "python"
    detached_python = tmp_path / "python-detached"
    marker = tmp_path / "wheel-python-marker"
    build_root = tmp_path / "build"
    wheel_dir = tmp_path / "wheel"
    build_root.mkdir(mode=0o700)
    build_root.chmod(0o700)
    expected_wheel = module.EXPECTED_WHEEL_NAME
    python_path.write_text(
        "#!/bin/sh\n"
        "printf 'trusted\\n' > \"$CODEX_USAGE_SPAWN_MARKER\"\n"
        "wheel_dir=''\n"
        "previous=''\n"
        "for argument in \"$@\"; do\n"
        "  if [ \"$previous\" = '--wheel-dir' ]; then wheel_dir=\"$argument\"; fi\n"
        "  previous=\"$argument\"\n"
        "done\n"
        "mkdir -p \"$wheel_dir\"\n"
        f"printf 'wheel\\n' > \"$wheel_dir/{expected_wheel}\"\n"
        f"chmod 600 \"$wheel_dir/{expected_wheel}\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    python_path.chmod(0o700)
    resolved = module._resolve_python_executable(python_path)
    environment = module._sanitized_build_environment()
    environment["CODEX_USAGE_SPAWN_MARKER"] = str(marker)
    monkeypatch.setattr(module, "_require_offline_builder", lambda **_kwargs: None)
    swapped = False

    def shadow_python(_python_fd_path: str) -> None:
        nonlocal swapped
        python_path.rename(detached_python)
        python_path.write_text(
            "#!/bin/sh\n"
            "printf 'shadow\\n' > \"$CODEX_USAGE_SPAWN_MARKER\"\n"
            "wheel_dir=''\n"
            "previous=''\n"
            "for argument in \"$@\"; do\n"
            "  if [ \"$previous\" = '--wheel-dir' ]; then wheel_dir=\"$argument\"; fi\n"
            "  previous=\"$argument\"\n"
            "done\n"
            "mkdir -p \"$wheel_dir\"\n"
            f"printf 'wheel\\n' > \"$wheel_dir/{expected_wheel}\"\n"
            f"chmod 600 \"$wheel_dir/{expected_wheel}\"\n"
            "exit 0\n",
            encoding="utf-8",
        )
        python_path.chmod(0o700)
        swapped = True

    monkeypatch.setattr(
        module,
        "_before_wheel_builder_exec",
        shadow_python,
        raising=False,
    )

    wheel_path, identity = module._build_verified_wheel(
        python_executable=resolved,
        environment=environment,
        build_root=build_root,
        wheel_dir=wheel_dir,
    )

    assert swapped
    assert marker.read_text(encoding="utf-8") == "trusted\n"
    assert wheel_path == wheel_dir / expected_wheel
    assert identity.permissions == 0o600
    assert detached_python.read_text(encoding="utf-8").startswith("#!/bin/sh")


def test_install_revalidates_temporary_root_and_child_identities(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    _, _, temporary_root = _roots(tmp_path)
    seen: list[tuple[Path, object]] = []
    original_require = integration_installer._require_private_dir

    def require(path, expected, create, **kwargs):
        path = Path(path)
        if path == temporary_root or path.parent == temporary_root or ".staging-" in path.name:
            seen.append((path, expected))
        return original_require(path, expected, create, **kwargs)

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
        ("release_id", "0.6.537-ffffffffffffffff"),
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


def test_builder_scans_wheel_directory_by_descriptor(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    build_root = tmp_path / "build"
    wheel_dir = tmp_path / "wheel"
    build_root.mkdir(mode=0o700)
    wheel_dir.mkdir(mode=0o700)
    wheel_identity = integration_installer._directory_identity(wheel_dir)
    monkeypatch.setattr(integration_installer, "_require_offline_builder", lambda **_: None)

    def fake_builder(command, *, env, cwd, pass_fds=()):
        del env, cwd, pass_fds
        (wheel_dir / integration_installer.EXPECTED_WHEEL_NAME).write_bytes(b"wheel")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(integration_installer, "_run_builder_bounded", fake_builder)
    original_iterdir = Path.iterdir

    def reject_wheel_path_iterdir(path):
        if path == wheel_dir:
            pytest.fail("wheel scan requires an opened directory descriptor")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", reject_wheel_path_iterdir)
    wheel_path, wheel_file_identity = integration_installer._build_verified_wheel(
        python_executable=Path(sys.executable),
        environment=integration_installer._sanitized_build_environment(),
        build_root=build_root,
        wheel_dir=wheel_dir,
        wheel_identity=wheel_identity,
    )
    assert wheel_path == wheel_dir / integration_installer.EXPECTED_WHEEL_NAME
    wheel_item = wheel_path.lstat()
    assert wheel_file_identity == integration_installer._FileIdentity(
        wheel_item.st_dev,
        wheel_item.st_ino,
        stat.S_IMODE(wheel_item.st_mode),
        wheel_item.st_gid,
    )


def test_builder_rejects_wrong_wheel_basename_before_release_use(tmp_path, monkeypatch):
    from codex_usage import integration_installer

    build_root = tmp_path / "build"
    wheel_dir = tmp_path / "wheel"
    build_root.mkdir(mode=0o700)
    wheel_dir.mkdir(mode=0o700)

    monkeypatch.setattr(integration_installer, "_require_offline_builder", lambda **_: None)

    def fake_builder(command, *, env, cwd, pass_fds=()):
        del env, cwd, pass_fds
        (wheel_dir / "wrong-name-0.6.537-py3-none-any.whl").write_bytes(b"wheel")
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
    _write_launcher_state(data_home, state_home)
    completed = subprocess.run(
        [
            str(release.launcher_path),
            "integration-snapshot",
            "--schema",
            "2",
            "--format",
            "json",
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "CODEX_USAGE_MARKER": "secret-marker",
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
    assert json.loads(completed.stdout)["schema_version"] == 2
    assert completed.stderr == ""


def test_installer_parser_errors_are_data_sparse(tmp_path):
    repo_root = _temporary_bootstrap_repo(tmp_path)

    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(repo_root / "scripts" / SCRIPT_PATH.name),
            "--unknown",
            "secret-marker",
        ],
        cwd=repo_root,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 64
    assert completed.stdout == ""
    assert completed.stderr == "integration_producer_unavailable\n"
    assert "secret-marker" not in completed.stderr


def test_installer_entrypoint_maps_cleanup_error_to_70(tmp_path, monkeypatch):
    import conftest as test_conftest

    monkeypatch.setattr(subprocess, "Popen", test_conftest._REAL_SUBPROCESS_POPEN)
    repo_root = _temporary_bootstrap_repo(tmp_path)
    wrapper = """\
import runpy
import sys

namespace = runpy.run_path(sys.argv[1], run_name="synthetic_installer")
cleanup_error = namespace["IntegrationCleanupError"]("synthetic mapper cleanup")

def fail_install(**_kwargs):
    raise cleanup_error

main = namespace["main"]
main.__globals__["install_release"] = fail_install
result = main([
    "--source-root", "/tmp/source",
    "--state-home", "/tmp/state",
    "--data-home", "/tmp/data",
    "--python", "/usr/bin/python",
    "--temporary-root", "/tmp/temporary",
])
print(result)
"""

    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            wrapper,
            str(repo_root / "scripts" / SCRIPT_PATH.name),
        ],
        cwd=repo_root,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0
    assert completed.stdout == "70\n"
    assert completed.stderr == "integration_producer_cleanup_failed\n"


def test_installer_cleanup_errors_have_distinct_data_sparse_result(tmp_path):
    repo_root = _temporary_bootstrap_repo(tmp_path)
    installer_path = repo_root / "src/codex_usage/integration_installer.py"
    installer_path.write_text(
        installer_path.read_text(encoding="utf-8")
        + "\ndef install_release(**_kwargs):\n"
        + "    raise IntegrationCleanupError()\n",
        encoding="utf-8",
    )
    installer_path.chmod(0o644)

    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(repo_root / "scripts" / SCRIPT_PATH.name),
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
        ],
        cwd=repo_root,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 70
    assert completed.stdout == ""
    assert completed.stderr == "integration_producer_cleanup_failed\n"


def test_attestation_private_path_guards_reject_missing_and_wrong_types(tmp_path):
    from codex_usage import integration_attestation as module

    missing = tmp_path / "missing"
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._private_regular(missing, mode=0o600)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._private_directory(missing)

    directory = tmp_path / "directory"
    directory.mkdir(mode=0o700)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._private_regular(directory, mode=0o600)
    file_path = tmp_path / "file"
    file_path.write_bytes(b"x")
    file_path.chmod(0o600)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._private_directory(file_path)


@pytest.mark.parametrize("value", [None, "", "relative", "/tmp/a//b", "/tmp/a\x00b"])
def test_attestation_absolute_path_and_containment_guards(value, tmp_path):
    from codex_usage import integration_attestation as module

    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._absolute_path(value)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._contained(tmp_path / "outside", tmp_path / "root")
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._contained(tmp_path / "root" / "bad\\name", tmp_path / "root")


def test_attestation_file_reader_rejects_parent_and_opened_identity_changes(
    tmp_path, monkeypatch
):
    from codex_usage import integration_attestation as module

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    path = parent / "payload"
    path.write_bytes(b"payload")
    path.chmod(0o600)
    original_lstat = Path.lstat

    def parent_not_directory(candidate):
        if candidate == parent:
            return SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600,
                st_uid=os.getuid(),
                st_dev=1,
                st_ino=1,
            )
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", parent_not_directory)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._file_bytes(path, mode=0o600)

    monkeypatch.setattr(Path, "lstat", original_lstat)
    original_fstat = module.os.fstat
    calls = 0

    def mismatched_parent(candidate_fd):
        nonlocal calls
        calls += 1
        item = original_fstat(candidate_fd)
        if calls == 1:
            return SimpleNamespace(
                st_mode=item.st_mode,
                st_uid=item.st_uid,
                st_dev=item.st_dev,
                st_ino=item.st_ino + 1,
            )
        return item

    monkeypatch.setattr(module.os, "fstat", mismatched_parent)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._file_bytes(path, mode=0o600)


def test_attestation_file_reader_rejects_oversized_opened_file(tmp_path, monkeypatch):
    from codex_usage import integration_attestation as module

    path = tmp_path / "payload"
    path.write_bytes(b"x")
    path.chmod(0o600)
    original_fstat = module.os.fstat
    calls = 0

    def oversized_file(candidate_fd):
        nonlocal calls
        calls += 1
        item = original_fstat(candidate_fd)
        if calls == 2:
            return SimpleNamespace(
                st_mode=item.st_mode,
                st_uid=item.st_uid,
                st_nlink=item.st_nlink,
                st_dev=item.st_dev,
                st_ino=item.st_ino,
                st_size=module.MAX_ATTESTATION_FILE_BYTES + 1,
            )
        return item

    monkeypatch.setattr(module.os, "fstat", oversized_file)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._file_bytes(path, mode=0o600)


def test_attestation_file_reader_rejects_oversized_read_payload(tmp_path, monkeypatch):
    from codex_usage import integration_attestation as module

    path = tmp_path / "payload"
    path.write_bytes(b"x")
    path.chmod(0o600)
    original_fdopen = module.os.fdopen
    fd_holder = {"fd": -1}

    class _OversizedHandle:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            os.close(fd_holder["fd"])

        def read(self, _size):
            return b"xx"

    def fdopen(candidate_fd, *args, **kwargs):
        fd_holder["fd"] = candidate_fd
        return _OversizedHandle()

    monkeypatch.setattr(module.os, "fdopen", fdopen)
    monkeypatch.setattr(module, "MAX_ATTESTATION_FILE_BYTES", 1)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._file_bytes(path, mode=0o600)
    monkeypatch.setattr(module.os, "fdopen", original_fdopen)


def test_attestation_file_reader_maps_open_error(tmp_path, monkeypatch):
    from codex_usage import integration_attestation as module

    path = tmp_path / "payload"
    path.write_bytes(b"x")
    path.chmod(0o600)
    original_open = module.os.open

    def fail_file_open(candidate, flags, *args, **kwargs):
        if candidate == path.name and kwargs.get("dir_fd") is not None:
            raise OSError("synthetic open marker")
        return original_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", fail_file_open)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._file_bytes(path, mode=0o600)


def test_attestation_tree_rejects_invalid_root_and_child_entries(tmp_path):
    from codex_usage import integration_attestation as module

    missing = tmp_path / "missing"
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._release_tree_rows(release_dir=missing)

    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    (release / "bad\\name").write_bytes(b"x")
    (release / "bad\\name").chmod(0o600)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._release_tree_rows(release_dir=release)

    target = release / "target"
    target.write_bytes(b"x")
    target.chmod(0o600)
    (release / "link").symlink_to(target)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._release_tree_rows(release_dir=release)


def test_attestation_tree_rejects_hardlink_and_file_size_limits(tmp_path, monkeypatch):
    from codex_usage import integration_attestation as module

    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    target = release / "target"
    target.write_bytes(b"x")
    target.chmod(0o600)
    hardlink = release / "hardlink"
    hardlink.hardlink_to(target)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._release_tree_rows(release_dir=release)

    hardlink.unlink()
    monkeypatch.setattr(module, "MAX_ATTESTATION_FILE_BYTES", 1)
    target.write_bytes(b"xx")
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._release_tree_rows(release_dir=release)


def test_attestation_tree_rejects_payload_growth_after_descriptor_read(
    tmp_path, monkeypatch
):
    from codex_usage import integration_attestation as module

    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    target = release / "target"
    target.write_bytes(b"x")
    target.chmod(0o600)
    monkeypatch.setattr(module, "MAX_RELEASE_TREE_BYTES", 1)
    monkeypatch.setattr(module, "_read_nofollow_fd", lambda _fd: b"xx")

    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._release_tree_rows(release_dir=release)


def test_attestation_tree_closes_child_fd_when_sorting_fails(tmp_path, monkeypatch):
    from codex_usage import integration_attestation as module

    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    child = release / "child"
    child.write_bytes(b"x")
    child.chmod(0o600)
    second = release / "second"
    second.write_bytes(b"y")
    second.chmod(0o600)
    original_scandir = module.os.scandir

    class _UnsortableName(str):
        def __lt__(self, _other):
            raise RuntimeError("synthetic sorting marker")

    class _Entry:
        def __init__(self, name, target):
            self.name = _UnsortableName(name)
            self.target = target

        def stat(self, *, follow_symlinks):
            assert follow_symlinks is False
            return self.target.lstat()

    class _Scan:
        def __enter__(self):
            return iter((_Entry("child", child), _Entry("second", second)))

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(module.os, "scandir", lambda _fd: _Scan())
    with pytest.raises(RuntimeError, match="synthetic sorting marker"):
        module._release_tree_rows(release_dir=release)
    monkeypatch.setattr(module.os, "scandir", original_scandir)


def test_attestation_tree_catches_reader_failure_and_closes_root_fd(
    tmp_path, monkeypatch
):
    from codex_usage import integration_attestation as module

    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    target = release / "target"
    target.write_bytes(b"x")
    target.chmod(0o600)
    monkeypatch.setattr(
        module,
        "_read_nofollow_fd",
        lambda _fd: (_ for _ in ()).throw(OSError("synthetic reader marker")),
    )

    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._release_tree_rows(release_dir=release)


def test_attestation_tree_rejects_unsupported_opened_entry_type(tmp_path, monkeypatch):
    from codex_usage import integration_attestation as module

    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    child = release / "child"
    child.write_bytes(b"x")
    child.chmod(0o600)
    original_isdir = module.stat.S_ISDIR
    original_isreg = module.stat.S_ISREG
    child_mode = child.stat().st_mode
    regular_calls = 0

    def fake_isdir(mode):
        return original_isdir(mode)

    def fake_isreg(mode):
        nonlocal regular_calls
        if mode == child_mode:
            regular_calls += 1
            return regular_calls == 1
        return original_isreg(mode)

    monkeypatch.setattr(module.stat, "S_ISDIR", fake_isdir)
    monkeypatch.setattr(module.stat, "S_ISREG", fake_isreg)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._release_tree_rows(release_dir=release)


def test_attestation_tree_closes_root_fd_when_root_stat_is_invalid(tmp_path, monkeypatch):
    from codex_usage import integration_attestation as module

    release = tmp_path / "release"
    release.write_bytes(b"not a directory")
    release.chmod(0o600)
    original_open = module.os.open
    original_close = module.os.close
    closed: list[int] = []

    def open_root(candidate, flags, *args, **kwargs):
        if candidate == release:
            return original_open(candidate, os.O_RDONLY)
        return original_open(candidate, flags, *args, **kwargs)

    def close_traced(fd):
        closed.append(fd)
        original_close(fd)
        raise OSError(errno.EIO, "close trace")

    monkeypatch.setattr(module.os, "open", open_root)
    monkeypatch.setattr(module.os, "close", close_traced)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._release_tree_rows(release_dir=release)
    assert closed


def test_attestation_read_nofollow_fd_rejects_invalid_and_oversized_payload(
    tmp_path, monkeypatch
):
    from codex_usage import integration_attestation as module

    directory = tmp_path / "directory"
    directory.mkdir(mode=0o700)
    fd = os.open(directory, os.O_RDONLY)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._read_nofollow_fd(fd)

    payload = tmp_path / "payload"
    payload.write_bytes(b"x")
    payload.chmod(0o600)
    fd = os.open(payload, os.O_RDONLY)
    original_fdopen = module.os.fdopen

    class _OversizedHandle:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            os.close(fd)

        def read(self, _size):
            return b"xx"

    monkeypatch.setattr(module.os, "fdopen", lambda *_args, **_kwargs: _OversizedHandle())
    monkeypatch.setattr(module, "MAX_ATTESTATION_FILE_BYTES", 1)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._read_nofollow_fd(fd)
    monkeypatch.setattr(module.os, "fdopen", original_fdopen)


def test_attestation_read_nofollow_fd_maps_fstat_error(tmp_path, monkeypatch):
    from codex_usage import integration_attestation as module

    payload = tmp_path / "payload"
    payload.write_bytes(b"x")
    payload.chmod(0o600)
    fd = os.open(payload, os.O_RDONLY)
    monkeypatch.setattr(
        module.os,
        "fstat",
        lambda _fd: (_ for _ in ()).throw(OSError("synthetic fstat marker")),
    )
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._read_nofollow_fd(fd)


def test_attestation_read_nofollow_bytes_rejects_parent_and_opened_identity(
    tmp_path, monkeypatch
):
    from codex_usage import integration_attestation as module

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    path = parent / "payload"
    path.write_bytes(b"x")
    path.chmod(0o600)
    original_lstat = Path.lstat

    def parent_not_directory(candidate):
        if candidate == parent:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_uid=os.getuid())
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", parent_not_directory)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._read_nofollow_bytes(path)
    monkeypatch.setattr(Path, "lstat", original_lstat)

    original_fstat = module.os.fstat
    calls = 0

    def mismatched_parent(candidate_fd):
        nonlocal calls
        calls += 1
        item = original_fstat(candidate_fd)
        if calls == 1:
            return SimpleNamespace(
                st_mode=item.st_mode,
                st_uid=item.st_uid,
                st_dev=item.st_dev,
                st_ino=item.st_ino + 1,
            )
        return item

    monkeypatch.setattr(module.os, "fstat", mismatched_parent)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._read_nofollow_bytes(path)


def test_attestation_read_nofollow_bytes_rejects_file_identity_and_read_size(
    tmp_path, monkeypatch
):
    from codex_usage import integration_attestation as module

    path = tmp_path / "payload"
    path.write_bytes(b"x")
    path.chmod(0o600)
    identity = path.stat()
    wrong_identity = SimpleNamespace(
        st_dev=identity.st_dev,
        st_ino=identity.st_ino + 1,
        st_mode=identity.st_mode,
    )
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._read_nofollow_bytes(path, expected_file_identity=wrong_identity)

    fd_holder = {"fd": -1}
    original_fdopen = module.os.fdopen

    class _OversizedHandle:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            os.close(fd_holder["fd"])

        def read(self, _size):
            return b"xx"

    def fdopen(candidate_fd, *args, **kwargs):
        fd_holder["fd"] = candidate_fd
        return _OversizedHandle()

    monkeypatch.setattr(module.os, "fdopen", fdopen)
    monkeypatch.setattr(module, "MAX_ATTESTATION_FILE_BYTES", 1)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._read_nofollow_bytes(path)
    monkeypatch.setattr(module.os, "fdopen", original_fdopen)


def test_attestation_read_nofollow_bytes_maps_open_error(tmp_path, monkeypatch):
    from codex_usage import integration_attestation as module

    path = tmp_path / "payload"
    path.write_bytes(b"x")
    path.chmod(0o600)
    original_open = module.os.open

    def fail_file_open(candidate, flags, *args, **kwargs):
        if candidate == path.name and kwargs.get("dir_fd") is not None:
            raise OSError("synthetic open marker")
        return original_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", fail_file_open)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._read_nofollow_bytes(path)


def test_attestation_release_tree_sha256_maps_unexpected_errors(monkeypatch, tmp_path):
    from codex_usage import integration_attestation as module

    monkeypatch.setattr(
        module,
        "_release_tree_rows",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic tree marker")),
    )
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._release_tree_sha256(release_dir=tmp_path)


def test_attestation_manifest_reader_rejects_io_mode_json_and_shape(tmp_path):
    from codex_usage import integration_attestation as module

    missing = tmp_path / "missing.json"
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._read_manifest(missing)

    path = tmp_path / "manifest.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._read_manifest(path)
    path.chmod(0o600)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._read_manifest(path)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._read_manifest(path)


@pytest.mark.parametrize("value", [None, "", 1])
def test_attestation_manifest_string_requires_nonempty_text(value):
    from codex_usage import integration_attestation as module

    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._manifest_string({"field": value}, "field")


def test_attestation_manifest_string_returns_valid_value():
    from codex_usage import integration_attestation as module

    assert module._manifest_string({"field": "value"}, "field") == "value"


def test_attestation_record_digest_rejects_prefix_and_decode_errors(monkeypatch):
    from codex_usage import integration_attestation as module

    assert module._record_digest("invalid", b"x") is False
    monkeypatch.setattr(
        module.base64,
        "urlsafe_b64decode",
        lambda _value: (_ for _ in ()).throw(module.binascii.Error("synthetic decode marker")),
    )
    assert module._record_digest("sha256=" + "A" * 43, b"x") is False


def test_attestation_record_rows_rejects_invalid_path_and_digest_rows(tmp_path):
    from codex_usage import integration_attestation as module

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
    record.write_text("../bad,,\n", encoding="utf-8")
    record.chmod(0o600)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._record_rows(record, release)

    record.write_text("dist-info/RECORD,,1\n", encoding="utf-8")
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._record_rows(record, release)
    record.write_text("codex_usage/ok.py,sha256=" + "A" * 43 + ",1\n", encoding="utf-8")
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._record_rows(record, release)


@pytest.mark.parametrize("payload", [b"\xff", b""])
def test_attestation_record_rows_rejects_invalid_or_empty_csv(tmp_path, payload):
    from codex_usage import integration_attestation as module

    release = tmp_path / "release"
    dist_info = release / "site-packages" / "dist-info"
    dist_info.mkdir(mode=0o700, parents=True)
    release.chmod(0o700)
    record = dist_info / "RECORD"
    record.write_bytes(payload)
    record.chmod(0o600)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._record_rows(record, release)


def test_attestation_record_rows_requires_record_self_row(tmp_path):
    from codex_usage import integration_attestation as module

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
    record.write_text(f"codex_usage/ok.py,sha256={digest},1\n", encoding="utf-8")
    record.chmod(0o600)

    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._record_rows(record, release)


def test_attestation_manifest_rejects_version_and_home_mismatch(tmp_path, monkeypatch):
    from codex_usage import integration_attestation as module

    state_home = tmp_path / "state"
    data_home = tmp_path / "data"
    manifest_path = tmp_path / "active.json"
    monkeypatch.setattr(
        module,
        "_read_manifest",
        lambda _path: {"schema_version": 2, "version": "wrong"},
    )
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._verify_manifest(
            manifest_path=manifest_path,
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=None,
        )

    monkeypatch.setattr(
        module,
        "_read_manifest",
        lambda _path: {
            "schema_version": 2,
            "version": module._EXPECTED_VERSION,
            "source_manifest_sha256": "a" * 64,
            "state_home": str(tmp_path / "other-state"),
            "data_home": str(data_home),
        },
    )
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module._verify_manifest(
            manifest_path=manifest_path,
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=None,
        )


def test_attestation_verify_maps_unexpected_exception(monkeypatch, tmp_path):
    from codex_usage import integration_attestation as module

    def fail_verify(**_kwargs):
        raise RuntimeError("synthetic verifier marker")

    monkeypatch.setattr(module, "_verify_manifest", fail_verify)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module.verify_active_release(
            state_home=tmp_path / "state",
            data_home=tmp_path / "data",
            expected_entrypoint_path=tmp_path / "entrypoint",
        )


def test_attestation_verify_rejects_expected_entrypoint_type_and_path(tmp_path):
    from codex_usage.integration_attestation import (
        IntegrationAttestationUnavailable,
        verify_active_release,
    )

    release, data_home, state_home = _install(tmp_path)
    with pytest.raises(IntegrationAttestationUnavailable):
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path="not-a-path",  # type: ignore[arg-type]
        )
    with pytest.raises(IntegrationAttestationUnavailable):
        verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.release_dir / "wrong.py",
        )


def test_attestation_verify_rejects_payload_digest_and_launcher_contract(tmp_path):
    from codex_usage import integration_attestation as module
    from codex_usage.private_io import write_private_text

    release, data_home, state_home = _install(tmp_path)
    active_path = state_home / "codex-usage" / "integration" / "active.json"
    manifest = json.loads(active_path.read_text(encoding="utf-8"))
    manifest["entrypoint_sha256"] = "0" * 64
    write_private_text(
        active_path,
        json.dumps(manifest, sort_keys=True),
        label="mutated active manifest",
        mode=0o600,
    )
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module.verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.entrypoint_path,
        )


def test_attestation_verify_rejects_launcher_without_entrypoint_contract(tmp_path):
    from codex_usage import integration_attestation as module
    from codex_usage.private_io import write_private_text

    release, data_home, state_home = _install(tmp_path)
    release.launcher_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    release.launcher_path.chmod(0o700)
    active_path = state_home / "codex-usage" / "integration" / "active.json"
    manifest = json.loads(active_path.read_text(encoding="utf-8"))
    manifest["launcher_sha256"] = hashlib.sha256(
        release.launcher_path.read_bytes()
    ).hexdigest()
    write_private_text(
        active_path,
        json.dumps(manifest, sort_keys=True),
        label="mutated launcher manifest",
        mode=0o600,
    )

    with pytest.raises(module.IntegrationAttestationUnavailable):
        module.verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.entrypoint_path,
        )


def test_installer_low_level_identity_and_path_guards(tmp_path, monkeypatch):
    from codex_usage import integration_installer as module

    with pytest.raises(ValueError, match="invalid wheel validation reason"):
        module._WheelMemberValidationError("invalid")
    for value in (Path("relative"), "not-a-path", Path("\x00invalid")):
        with pytest.raises(module.IntegrationInstallError):
            module._absolute(value)  # type: ignore[arg-type]

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    regular = parent / "regular"
    regular.write_bytes(b"owned")
    regular.chmod(0o600)
    wrong_mode = parent / "wrong-mode"
    wrong_mode.write_bytes(b"owned")
    wrong_mode.chmod(0o644)
    directory = parent / "directory"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)

    failing = tmp_path / "failing"
    original_lstat = Path.lstat

    def fail_lstat(path):
        if path == failing:
            raise OSError("lstat")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_lstat)
    with pytest.raises(module.IntegrationInstallError):
        module._no_symlink_ancestors(failing)
    with pytest.raises(module.IntegrationInstallError):
        module._identity(failing)
    with pytest.raises(module.IntegrationInstallError):
        module._provisional_path_identity(failing, directory=False)
    monkeypatch.setattr(Path, "lstat", original_lstat)

    with pytest.raises(module.IntegrationInstallError):
        module._identity(regular)
    with pytest.raises(module.IntegrationInstallError):
        module._directory_identity(regular)
    with pytest.raises(module.IntegrationInstallError):
        module._directory_identity(tmp_path / "missing")
    assert module._file_identity(regular).permissions == 0o600
    with pytest.raises(module.IntegrationInstallError):
        module._file_identity(wrong_mode)
    with pytest.raises(module.IntegrationInstallError):
        module._provisional_path_identity(regular, directory=True)
    with pytest.raises(module.IntegrationInstallError):
        module._provisional_path_identity(directory, directory=False)
    symlink = parent / "symlink"
    symlink.symlink_to(regular)
    with pytest.raises(module.IntegrationInstallError):
        module._provisional_path_identity(symlink, directory=False)

    with pytest.raises(module.IntegrationInstallError):
        module._provisional_fd_identity(-1)
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(module.IntegrationInstallError):
            module._provisional_fd_identity(directory_fd)
    finally:
        os.close(directory_fd)

    parent_identity = module._directory_identity(parent)
    regular_identity = module._provisional_path_identity(regular, directory=False)
    assert module._provisional_rebased(
        regular,
        regular_identity,
        parent_identity,
        directory=False,
    ) == regular_identity
    assert module._provisional_rebased(
        symlink,
        regular_identity,
        parent_identity,
        directory=False,
    ) is None
    assert module._provisional_rebased(
        regular,
        regular_identity,
        parent_identity,
        directory=True,
    ) is None
    assert module._provisional_rebased(
        directory,
        regular_identity,
        parent_identity,
        directory=False,
    ) is None


def test_installer_cleanup_and_rename_guards(tmp_path, monkeypatch):
    from codex_usage import integration_installer as module

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    parent_identity = module._directory_identity(parent)
    module._no_symlink_ancestors(
        SimpleNamespace(anchor=tmp_path.anchor, parts=(tmp_path.anchor, ".", "missing"))
    )

    failing = parent / "failing"
    original_lstat = Path.lstat

    def fail_file_lstat(path):
        if path == failing:
            raise ValueError("lstat")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_file_lstat)
    with pytest.raises(module.IntegrationInstallError):
        module._file_identity(failing)
    monkeypatch.setattr(Path, "lstat", original_lstat)

    regular = parent / "regular"
    regular.write_bytes(b"owned")
    regular.chmod(0o600)
    regular_identity = module._file_identity(regular)
    provisional = module._provisional_path_identity(regular, directory=False)

    with monkeypatch.context() as context:
        context.setattr(
            module,
            "_directory_identity",
            lambda _path: (_ for _ in ()).throw(OSError("rebased")),
        )
        assert (
            module._provisional_rebased(
                regular,
                provisional,
                parent_identity,
                directory=False,
            )
            is None
        )

    with monkeypatch.context() as context:
        context.setattr(
            module,
            "_no_symlink_ancestors",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                module.IntegrationInstallError()
            ),
        )
        assert not module._owned_file_matches(regular, regular_identity, parent_identity)
        assert not module._owned_directory_matches(
            parent,
            parent_identity,
            parent_identity,
        )

    wrong_parent = module._DirectoryIdentity(
        parent_identity.device,
        parent_identity.inode,
        0o701,
        parent_identity.gid,
    )
    assert not module._remove_owned_entry(
        regular,
        regular_identity,
        wrong_parent,
        directory=False,
    )
    assert not module._remove_owned_entry(
        regular,
        regular_identity,
        parent_identity,
        directory=True,
    )
    directory_entry = parent / "directory-entry"
    directory_entry.mkdir(mode=0o700)
    directory_identity = module._identity(directory_entry)
    assert not module._remove_owned_entry(
        directory_entry,
        directory_identity,
        parent_identity,
        directory=False,
    )
    wrong_provisional = module._ProvisionalIdentity(
        provisional.device,
        provisional.inode + 1,
        provisional.uid,
        provisional.file_type,
        provisional.permissions,
        provisional.gid,
    )
    assert not module._remove_owned_entry(
        regular,
        wrong_provisional,
        parent_identity,
        directory=False,
    )
    wrong_directory = module._DirectoryIdentity(
        regular_identity.device,
        regular_identity.inode,
        regular_identity.permissions + 1,
        regular_identity.gid,
    )
    assert not module._remove_owned_entry(
        regular,
        wrong_directory,
        parent_identity,
        directory=False,
    )
    wrong_file = module._FileIdentity(
        regular_identity.device,
        regular_identity.inode + 1,
        regular_identity.permissions,
        regular_identity.gid,
    )
    assert not module._remove_owned_entry(
        regular,
        wrong_file,
        parent_identity,
        directory=False,
    )

    original_stat = module.os.stat
    stat_calls = 0

    def fail_second_stat(path, *args, **kwargs):
        nonlocal stat_calls
        stat_calls += 1
        if stat_calls == 2:
            raise OSError("replacement")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "stat", fail_second_stat)
    assert not module._remove_owned_entry(
        regular,
        regular_identity,
        parent_identity,
        directory=False,
    )
    monkeypatch.setattr(module.os, "stat", original_stat)

    with monkeypatch.context() as context:
        context.setattr(module.os, "unlink", lambda *_args, **_kwargs: None)
        assert not module._remove_owned_entry(
            regular,
            regular_identity,
            parent_identity,
            directory=False,
        )
    with monkeypatch.context() as context:
        def fail_open(*_args, **_kwargs):
            raise OSError("open")

        context.setattr(module.os, "open", fail_open)
        assert not module._remove_owned_entry(
            regular,
            regular_identity,
            parent_identity,
            directory=False,
        )

    for attribute in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
        path = parent / f"without-{attribute}"
        path.write_bytes(b"owned")
        path.chmod(0o600)
        identity = module._file_identity(path)
        with monkeypatch.context() as context:
            context.delattr(module.os, attribute, raising=False)
            assert module._remove_owned_entry(
                path,
                identity,
                parent_identity,
                directory=False,
            )

    source = parent / "source"
    source.mkdir(mode=0o700)
    source_identity = module._identity(source)
    target = parent / "target"
    with pytest.raises(module.IntegrationInstallError):
        module._rename_owned_directory(
            source,
            tmp_path / "other" / "target",
            parent_identity,
            source_identity,
        )
    with monkeypatch.context() as context:
        context.setattr(module.sys, "platform", "darwin")
        with pytest.raises(module.IntegrationInstallError):
            module._rename_noreplace("source", "target", -1)
    with monkeypatch.context() as context:
        context.setattr(module.ctypes, "CDLL", lambda *_args, **_kwargs: SimpleNamespace())
        with pytest.raises(module.IntegrationInstallError):
            module._rename_noreplace("source", "target", -1)
    with monkeypatch.context() as context:
        def fail_cdll(*_args, **_kwargs):
            raise OSError("renameat2")

        context.setattr(module.ctypes, "CDLL", fail_cdll)
        with pytest.raises(module.IntegrationInstallError):
            module._rename_noreplace("source", "target", -1)
    with monkeypatch.context() as context:
        def fail_rename(*_args, **_kwargs):
            return 1

        context.setattr(
            module.ctypes,
            "CDLL",
            lambda *_args, **_kwargs: SimpleNamespace(renameat2=fail_rename),
        )
        with pytest.raises(OSError):
            module._rename_noreplace("source", "target", -1)

    wrong_source = module._DirectoryIdentity(
        source_identity.device,
        source_identity.inode + 1,
        source_identity.permissions,
        source_identity.gid,
    )
    with pytest.raises(module.IntegrationInstallError):
        module._rename_owned_directory(source, target, parent_identity, wrong_source)

    def rename_and_replace(_source_name, _target_name, _parent_fd):
        source.rename(target)
        target.rmdir()
        target.mkdir(mode=0o700)

    with monkeypatch.context() as context:
        context.setattr(module, "_rename_noreplace", rename_and_replace)
        with pytest.raises(module.IntegrationInstallError):
            module._rename_owned_directory(source, target, parent_identity, source_identity)

    flags_source = parent / "flags-source"
    flags_source.mkdir(mode=0o700)
    flags_target = parent / "flags-target"
    flags_identity = module._identity(flags_source)
    with monkeypatch.context() as context:
        context.delattr(module.os, "O_DIRECTORY", raising=False)
        context.delattr(module.os, "O_NOFOLLOW", raising=False)
        context.delattr(module.os, "O_CLOEXEC", raising=False)
        module._rename_owned_directory(
            flags_source,
            flags_target,
            parent_identity,
            flags_identity,
        )
    assert flags_target.is_dir()

    with monkeypatch.context() as context:
        context.setattr(
            module,
            "_no_symlink_ancestors",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                module.IntegrationInstallError()
            ),
        )
        with pytest.raises(module.IntegrationInstallError):
            module._rename_owned_directory(
                flags_target,
                parent / "never-target",
                parent_identity,
                flags_identity,
            )


def test_installer_private_directory_and_bootstrap_guards(tmp_path, monkeypatch):
    from codex_usage import integration_installer as module

    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    parent_identity = module._directory_identity(parent)

    target = parent / "target"
    wrong_parent = module._DirectoryIdentity(
        parent_identity.device,
        parent_identity.inode,
        0o701,
        parent_identity.gid,
    )
    with pytest.raises(module.IntegrationInstallError):
        module._create_private_directory(target, wrong_parent)

    with monkeypatch.context() as context:
        target = parent / "invalid-child"
        original_fstat = module.os.fstat
        calls = 0

        def regular_child(fd):
            nonlocal calls
            calls += 1
            item = original_fstat(fd)
            if calls == 2:
                values = list(item)
                values[0] = stat.S_IFREG | 0o600
                return os.stat_result(values)
            return item

        context.setattr(module.os, "fstat", regular_child)
        with pytest.raises(module.IntegrationInstallError):
            module._create_private_directory(target, parent_identity)
        assert target.is_dir()

    with monkeypatch.context() as context:
        target = parent / "bad-final"
        original_fstat = module.os.fstat
        calls = 0

        def wrong_final(fd):
            nonlocal calls
            calls += 1
            item = original_fstat(fd)
            if calls == 3:
                values = list(item)
                values[0] = stat.S_IFDIR | 0o755
                return os.stat_result(values)
            return item

        context.setattr(module.os, "fstat", wrong_final)
        with pytest.raises(module.IntegrationInstallError):
            module._create_private_directory(target, parent_identity)
        assert not target.exists()

    with monkeypatch.context() as context:
        target = parent / "bad-parent-final"
        original_fstat = module.os.fstat
        calls = 0

        def wrong_parent_final(fd):
            nonlocal calls
            calls += 1
            item = original_fstat(fd)
            if calls == 4:
                values = list(item)
                values[0] = stat.S_IFDIR | 0o701
                return os.stat_result(values)
            return item

        context.setattr(module.os, "fstat", wrong_parent_final)
        with pytest.raises(module.IntegrationInstallError):
            module._create_private_directory(target, parent_identity)
        assert not target.exists()

    with monkeypatch.context() as context:
        target = parent / "integration-error"

        def fail_fchmod(*_args, **_kwargs):
            raise module.IntegrationInstallError()

        context.setattr(module.os, "fchmod", fail_fchmod)
        with pytest.raises(module.IntegrationInstallError):
            module._create_private_directory(target, parent_identity)
        assert not target.exists()

    with monkeypatch.context() as context:
        target = parent / "cleanup-error"

        def fail_fchmod(*_args, **_kwargs):
            raise OSError("chmod")

        context.setattr(module.os, "fchmod", fail_fchmod)

        def fail_cleanup(*_args, **_kwargs):
            return False

        context.setattr(module, "_cleanup_provisional_after_failure", fail_cleanup)
        with pytest.raises(module.IntegrationInstallError):
            module._create_private_directory(target, parent_identity)

    with monkeypatch.context() as context:
        context.delattr(module.os, "O_DIRECTORY", raising=False)
        context.delattr(module.os, "O_NOFOLLOW", raising=False)
        context.delattr(module.os, "O_CLOEXEC", raising=False)
        target = parent / "without-flags"
        module._create_private_directory(target, parent_identity)
        assert target.is_dir()

    with monkeypatch.context() as context:
        def fail_open(*_args, **_kwargs):
            raise OSError("open")

        context.setattr(module.os, "open", fail_open)
        with pytest.raises(module.IntegrationInstallError):
            module._create_private_directory(parent / "open-error", parent_identity)

    with pytest.raises(module.IntegrationInstallError):
        module._require_private_dir(target, wrong_parent, False)

    create_target = parent / "create-without-parent-identity"
    module._require_private_dir(create_target, None, True)
    assert create_target.is_dir()

    race_target = parent / "race-target"
    race_target.mkdir(mode=0o700)
    original_stat = module.os.stat
    first_stat = True

    def report_missing_once(name, *args, **kwargs):
        nonlocal first_stat
        if first_stat and name == race_target.name:
            first_stat = False
            raise FileNotFoundError(name)
        return original_stat(name, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(module.os, "stat", report_missing_once)

        def report_exists(*_args, **_kwargs):
            raise FileExistsError("race")

        context.setattr(module.os, "mkdir", report_exists)
        assert module._require_private_dir(race_target, None, True).permissions == 0o700

    with monkeypatch.context() as context:
        context.setattr(module, "_require_private_dir", lambda *_args, **_kwargs: wrong_parent)
        with pytest.raises(module.IntegrationInstallError):
            module._revalidate_bootstrap(tmp_path, parent_identity, parent_identity)

    create_wrong_parent = parent / "create-wrong-parent"
    with pytest.raises(module.IntegrationInstallError):
        module._require_private_dir(
            create_wrong_parent,
            None,
            True,
            parent_identity=wrong_parent,
        )

    with monkeypatch.context() as context:
        context.delattr(module.os, "O_DIRECTORY", raising=False)
        context.delattr(module.os, "O_NOFOLLOW", raising=False)
        context.delattr(module.os, "O_CLOEXEC", raising=False)
        no_flag_target = parent / "require-without-flags"
        module._require_private_dir(no_flag_target, None, True)

    with monkeypatch.context() as context:
        def fail_open(*_args, **_kwargs):
            raise OSError("open")

        context.setattr(module.os, "open", fail_open)
        with pytest.raises(module.IntegrationInstallError):
            module._require_private_dir(parent / "require-open-error", None, True)

    with monkeypatch.context() as context:
        original_fstat = module.os.fstat
        calls = 0

        def wrong_final_parent(fd):
            nonlocal calls
            calls += 1
            item = original_fstat(fd)
            if calls == 2:
                values = list(item)
                values[0] = stat.S_IFDIR | 0o701
                return os.stat_result(values)
            return item

        context.setattr(module.os, "fstat", wrong_final_parent)
        with pytest.raises(module.IntegrationInstallError):
            module._require_private_dir(
                parent / "require-final-race",
                None,
                True,
                parent_identity=parent_identity,
            )


def test_installer_copy_reader_resolver_and_builder_guards(tmp_path, monkeypatch):
    from codex_usage import integration_installer as module

    source = tmp_path / "source"
    source.write_bytes(b"source")
    source.chmod(0o600)
    parent = tmp_path / "destination"
    parent.mkdir(mode=0o700)
    parent_identity = module._directory_identity(parent)

    with monkeypatch.context() as context:
        original_lstat = Path.lstat

        def fail_source_lstat(path):
            if path == source:
                raise OSError("source lstat")
            return original_lstat(path)

        context.setattr(Path, "lstat", fail_source_lstat)
        with pytest.raises(module.IntegrationInstallError):
            module._copy_regular(source, parent / "source-lstat-error")

    with pytest.raises(module.IntegrationInstallError):
        module._copy_regular(parent, parent / "directory-target")

    with monkeypatch.context() as context:
        original_identity = module._directory_identity

        def wrong_target_parent(path):
            if path == parent:
                return module._DirectoryIdentity(
                    parent_identity.device,
                    parent_identity.inode,
                    0o701,
                    parent_identity.gid,
                )
            return original_identity(path)

        context.setattr(module, "_directory_identity", wrong_target_parent)
        with pytest.raises(module.IntegrationInstallError):
            module._copy_regular(source, parent / "wrong-parent")

    with monkeypatch.context() as context:
        target = parent / "wrong-output"
        original_fstat = module.os.fstat

        def wrong_output_mode(fd):
            item = original_fstat(fd)
            try:
                descriptor_path = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                descriptor_path = ""
            if descriptor_path == str(target):
                values = list(item)
                values[0] = stat.S_IFREG | 0o644
                return os.stat_result(values)
            return item

        context.setattr(module.os, "fstat", wrong_output_mode)
        with pytest.raises(module.IntegrationInstallError):
            module._copy_regular(source, target)
        assert not target.exists()

    with monkeypatch.context() as context:
        target = parent / "read-error"

        def fail_read(*_args, **_kwargs):
            raise OSError("read")

        context.setattr(module, "_read_nofollow", fail_read)
        with pytest.raises(module.IntegrationInstallError):
            module._copy_regular(source, target)
        assert not target.exists()

    with monkeypatch.context() as context:
        target = parent / "fdopen-error"

        def fail_fdopen(*_args, **_kwargs):
            raise OSError("fdopen")

        context.setattr(module.os, "fdopen", fail_fdopen)
        with pytest.raises(module.IntegrationInstallError):
            module._copy_regular(source, target)
        assert not target.exists()

    for attribute in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
        target = parent / f"without-{attribute}"
        with monkeypatch.context() as context:
            context.delattr(module.os, attribute, raising=False)
            assert module._copy_regular(source, target).permissions == 0o600

    file_path = parent / "reader"
    file_path.write_bytes(b"payload")
    file_path.chmod(0o600)
    file_identity = module._file_identity(file_path)
    with monkeypatch.context() as context:
        context.delattr(module.os, "O_DIRECTORY", raising=False)
        context.delattr(module.os, "O_NOFOLLOW", raising=False)
        context.delattr(module.os, "O_CLOEXEC", raising=False)
        assert module._read_nofollow(
            file_path,
            expected_parent_identity=parent_identity,
            expected_file_identity=file_identity,
        ) == b"payload"

    wrong_parent = module._DirectoryIdentity(
        parent_identity.device,
        parent_identity.inode,
        0o701,
        parent_identity.gid,
    )
    with pytest.raises(module.IntegrationInstallError):
        module._read_nofollow(file_path, expected_parent_identity=wrong_parent)
    with monkeypatch.context() as context:
        def fail_open(*_args, **_kwargs):
            raise OSError("open")

        context.setattr(module.os, "open", fail_open)
        with pytest.raises(module.IntegrationInstallError):
            module._read_nofollow(file_path)

    executable = tmp_path / "python"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    original_lstat = Path.lstat
    lstat_calls = 0

    def fail_final_lstat(path):
        nonlocal lstat_calls
        if path == executable:
            lstat_calls += 1
            if lstat_calls == 2:
                raise OSError("final lstat")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_final_lstat)
    with pytest.raises(module.IntegrationInstallError):
        module._resolve_python_executable(executable)
    monkeypatch.setattr(Path, "lstat", original_lstat)

    synthetic = tmp_path / "synthetic"
    synthetic.mkdir(mode=0o700)
    destination_root = tmp_path / "copy-destination"
    destination_root.mkdir(mode=0o700)
    with monkeypatch.context() as context:
        context.setattr(module, "PROJECT_ROOT", synthetic)
        context.setattr(module, "SOURCE_MANIFEST_FILES", ("missing.py",))
        with pytest.raises(module.IntegrationInstallError):
            module._temporary_source_copy(destination_root)

    synthetic_file = synthetic / "one.py"
    synthetic_file.write_bytes(b"one")
    with monkeypatch.context() as context:
        context.setattr(module, "PROJECT_ROOT", synthetic)
        context.setattr(module, "SOURCE_MANIFEST_FILES", ("one.py",))
        context.setattr(module, "_postwalk_release", lambda *_args, **_kwargs: set())
        with pytest.raises(module.IntegrationInstallError):
            module._temporary_source_copy(destination_root)

    for value in ("'", "\n", "\r", "\x00"):
        with pytest.raises(module.IntegrationInstallError):
            module._shell_single_quote(value)

    class BrokenProcess:
        pid = 123

        def kill(self):
            raise OSError("kill")

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(["builder"], timeout)

    with monkeypatch.context() as context:
        def fail_killpg(*_args, **_kwargs):
            raise OSError("killpg")

        context.setattr(module.os, "killpg", fail_killpg)
        module._terminate_preflight_process(BrokenProcess())

    def make_preflight_result(payload):
        def run(**_kwargs):
            return subprocess.CompletedProcess(["python"], 0, payload, "")

        return run

    for stdout in (
        "[]\n",
        '{"backend":"setuptools.command.bdist_wheel.bdist_wheel",'
        '"setuptools":80}\n',
        '{"backend":"setuptools.command.bdist_wheel.bdist_wheel",'
        '"setuptools":"broken.version"}\n',
    ):
        with monkeypatch.context() as context:
            context.setattr(module, "_run_builder_preflight", make_preflight_result(stdout))
            with pytest.raises(module.IntegrationInstallError):
                module._require_offline_builder(
                    python_executable=Path(sys.executable),
                    environment={},
                )


def test_installer_preflight_deadline_and_cleanup_guards(monkeypatch):
    from codex_usage import integration_installer as module

    class FakeProcess:
        def __init__(self, stdout, *, poll_value=None, wait_error=None):
            self.pid = None
            self.stdout = stdout
            self.poll_value = poll_value
            self.wait_error = wait_error
            self.killed = False

        def kill(self):
            self.killed = True

        def poll(self):
            return self.poll_value

        def wait(self, timeout=None):
            if self.wait_error is not None:
                raise self.wait_error
            return 0

    class FakeStream:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeSelector:
        def __init__(self, *, mode, stream):
            self.mode = mode
            self.stream = stream
            self.registered = False

        def register(self, stream, _events):
            self.registered = stream

        def unregister(self, _stream):
            self.registered = False

        def get_map(self):
            return {1: object()} if self.registered else {}

        def select(self, _timeout):
            if self.mode == "empty":
                return []
            if self.mode == "error":
                raise RuntimeError("selector")
            return [(SimpleNamespace(fileobj=self.stream), 1)]

        def close(self):
            pass

    def run_case(process, selector_mode, monotonic_values):
        values = iter(monotonic_values)
        with monkeypatch.context() as context:
            context.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: process)
            context.setattr(
                module.selectors,
                "DefaultSelector",
                lambda: FakeSelector(mode=selector_mode, stream=process.stdout),
            )
            context.setattr(module.time, "monotonic", lambda: next(values))
            return module._run_builder_preflight(
                python_executable=Path("/usr/bin/python"),
                environment={},
            )

    with monkeypatch.context() as context:
        process = FakeProcess(None)
        context.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: process)
        with pytest.raises(OSError, match="stdout unavailable"):
            module._run_builder_preflight(
                python_executable=Path("/usr/bin/python"),
                environment={},
            )

    stream = FakeStream()
    process = FakeProcess(stream)
    with pytest.raises(subprocess.TimeoutExpired):
        run_case(process, "ready", (0.0, module.BUILDER_PREFLIGHT_TIMEOUT_SECONDS + 1.0))
    stream = FakeStream()
    process = FakeProcess(stream)
    with pytest.raises(subprocess.TimeoutExpired):
        run_case(process, "empty", (0.0, 0.0))

    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    stream_file = os.fdopen(read_fd, "rb")
    process = FakeProcess(
        stream_file,
        wait_error=subprocess.TimeoutExpired(["builder"], 1),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run_case(process, "ready", (0.0, 0.0, 0.0))

    stream = FakeStream()
    process = FakeProcess(stream, poll_value=None)
    with pytest.raises(RuntimeError, match="selector"):
        run_case(process, "error", (0.0, 1.0))


def test_installer_builder_import_and_record_guards(tmp_path, monkeypatch):
    from codex_usage import integration_installer as module

    class FakeBuilderProcess:
        pid = 123

        def wait(self, timeout=None):
            raise RuntimeError("builder")

        def poll(self):
            return 1

    killed: list[int] = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: FakeBuilderProcess())
    monkeypatch.setattr(module.os, "getpgid", lambda _pid: 456)
    monkeypatch.setattr(module, "_kill_process_group", killed.append)
    with pytest.raises(module.IntegrationCleanupError) as exc_info:
        module._run_builder_bounded(["builder"], env={}, cwd=tmp_path)
    assert killed == [456]
    assert isinstance(exc_info.value.__cause__, BaseExceptionGroup)
    assert [
        type(error).__name__ for error in exc_info.value.__cause__.exceptions
    ] == ["RuntimeError", "RuntimeError"]

    class NoGroupProcess:
        pid = True

        def wait(self, timeout=None):
            raise RuntimeError("builder without group")

        def poll(self):
            return 1

    with monkeypatch.context() as context:
        context.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: NoGroupProcess())
        with pytest.raises(module.IntegrationCleanupError) as no_group_exc:
            module._run_builder_bounded(["builder"], env={}, cwd=tmp_path)
    assert isinstance(no_group_exc.value.__cause__, BaseExceptionGroup)
    assert [
        type(error).__name__ for error in no_group_exc.value.__cause__.exceptions
    ] == ["RuntimeError", "RuntimeError"]

    assert module._resolve_local_import_targets(node=ast.parse("import os").body[0]) == frozenset()
    assert module._resolve_local_import_targets(
        node=ast.parse("import codex_usage").body[0]
    ) == frozenset({"__init__.py"})
    assert module._resolve_local_import_targets(node=ast.parse("x = 1").body[0]) == frozenset()
    assert module._resolve_local_import_targets(
        node=ast.parse("from os import path").body[0]
    ) == frozenset()
    assert module._resolve_local_import_targets(
        node=ast.parse("from . import config").body[0]
    ) == frozenset({"config.py"})
    assert module._resolve_local_import_targets(
        node=ast.parse("from .models import AccountUsage").body[0]
    ) == frozenset({"models.py"})
    assert module._resolve_local_import_targets(
        node=ast.parse("from codex_usage import config").body[0]
    ) == frozenset({"config.py"})
    with pytest.raises(module.IntegrationInstallError):
        module._resolve_local_import_targets(node=ast.parse("import codex_usage.a.b").body[0])
    with pytest.raises(module.IntegrationInstallError):
        module._resolve_local_import_targets(node=ast.parse("from ... import config").body[0])
    with pytest.raises(module.IntegrationInstallError):
        module._resolve_local_import_targets(
            node=ast.parse("from codex_usage.a.b import x").body[0]
        )
    with pytest.raises(module.IntegrationInstallError):
        module._resolve_local_import_targets(node=ast.parse("from . import *").body[0])

    with pytest.raises(module.IntegrationInstallError):
        module._validate_runtime_import_closure({"foreign.py": b"pass"})
    with pytest.raises(module.IntegrationInstallError):
        module._validate_runtime_import_closure({"codex_usage/config.py": b"\xff"})
    with pytest.raises(module.IntegrationInstallError):
        module._validate_runtime_import_closure(
            {"codex_usage/config.py": b"from codex_usage import state\n"}
        )
    with pytest.raises(module.IntegrationInstallError):
        module._validate_runtime_import_closure(
            {
                "codex_usage/config.py": b"from codex_usage import state\n",
                "codex_usage/models.py": b"pass\n",
            }
        )
    module._validate_runtime_import_closure(
        {"codex_usage/config.py": b"from codex_usage import state\n"},
        require_available=False,
    )

    with pytest.raises(module.IntegrationInstallError):
        module._read_bounded_wheel_member(
            SimpleNamespace(),
            SimpleNamespace(file_size=module.MAX_INSTALL_FILE_BYTES + 1),
        )
    invalid_records = (
        b"one,two\n",
        b"one,sha256=x,1\none,sha256=y,1\n",
        b"/absolute,,\n",
        b"a/../b,,\n",
        b"a,sha256=x,nope\n",
        b"\xff\n",
    )
    for payload in invalid_records:
        with pytest.raises(module.IntegrationInstallError):
            module._parse_record(payload)


def test_installer_safe_extract_inner_guards(tmp_path, monkeypatch):
    from codex_usage import integration_installer as module

    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("codex_usage/ok.py", b"x")
    rows = {"codex_usage/ok.py": (hashlib.sha256(b"x").hexdigest(), 1)}

    destination = tmp_path / "missing-record"
    destination.mkdir(mode=0o700)
    with pytest.raises(module._WheelMemberValidationError) as error:
        module._safe_extract_wheel(
            wheel_path=wheel,
            destination=destination,
            record_rows={},
        )
    assert error.value.reason == "record_mismatch"

    destination = tmp_path / "bad-record"
    destination.mkdir(mode=0o700)
    with pytest.raises(module._WheelMemberValidationError) as error:
        module._safe_extract_wheel(
            wheel_path=wheel,
            destination=destination,
            record_rows={"codex_usage/ok.py": ("sha256=bad", 1)},
        )
    assert error.value.reason == "record_mismatch"

    destination = tmp_path / "bad-zip"
    destination.mkdir(mode=0o700)
    bad_wheel = tmp_path / "bad.whl"
    bad_wheel.write_bytes(b"not a zip")
    with pytest.raises(module.IntegrationInstallError):
        module._safe_extract_wheel(
            wheel_path=bad_wheel,
            destination=destination,
            record_rows={},
        )

    destination = tmp_path / "wrong-destination"
    destination.mkdir(mode=0o700)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    with pytest.raises(module.IntegrationInstallError):
        module._safe_extract_wheel(
            wheel_path=wheel,
            destination=destination,
            record_rows=rows,
            destination_identity=module._directory_identity(other),
        )

    destination = tmp_path / "missing-parent"
    destination.mkdir(mode=0o700)
    original_stat = module.os.stat
    parent_stat_calls = 0

    def disappear_on_second_pass(name, *args, **kwargs):
        nonlocal parent_stat_calls
        if name == "codex_usage":
            parent_stat_calls += 1
            if parent_stat_calls in {1, 3}:
                raise FileNotFoundError(name)
        return original_stat(name, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(module.os, "stat", disappear_on_second_pass)
        with pytest.raises(module.IntegrationInstallError):
            module._safe_extract_wheel(
                wheel_path=wheel,
                destination=destination,
                record_rows=rows,
            )


def test_installer_safe_extract_and_wheel_details_validation_guards(tmp_path, monkeypatch):
    from codex_usage import integration_installer as module

    duplicate = tmp_path / "duplicate.whl"
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("codex_usage/ok.py", b"x")
        with pytest.warns(UserWarning):
            archive.writestr("codex_usage/ok.py", b"y")
    with pytest.raises(module._WheelMemberValidationError) as error:
        module._wheel_details(duplicate)
    assert error.value.reason == "duplicate_member"

    mismatched = tmp_path / "mismatched.whl"
    with zipfile.ZipFile(mismatched, "w") as archive:
        archive.writestr("codex_usage/ok.py", b"x")
    with pytest.raises(module.IntegrationInstallError):
        module._wheel_details(mismatched)

    installed_root = tmp_path / "installed"
    installed_root.mkdir(mode=0o700)
    release, _, _ = _install(installed_root)
    wheel = release.release_dir / "producer.whl"
    original_parse = module._parse_record
    with monkeypatch.context() as context:
        context.setattr(module, "_parse_record", lambda _payload: {})
        with pytest.raises(module.IntegrationInstallError):
            module._wheel_details(wheel)

    original_read = module._read_bounded_wheel_member

    def bad_metadata(archive, info):
        if info.filename.endswith("METADATA"):
            return b"invalid metadata"
        return original_read(archive, info)

    with monkeypatch.context() as context:
        context.setattr(module, "_read_bounded_wheel_member", bad_metadata)
        with pytest.raises(module.IntegrationInstallError):
            module._wheel_details(wheel)

    def bad_record_self(payload):
        rows = original_parse(payload)
        record_name = f"{module.DIST_INFO_PREFIX}/RECORD"
        rows[record_name] = ("sha256=bad", -1)
        return rows

    with monkeypatch.context() as context:
        context.setattr(module, "_parse_record", bad_record_self)
        with pytest.raises(module.IntegrationInstallError):
            module._wheel_details(wheel)

    def bad_record_digest(payload):
        rows = original_parse(payload)
        name = next(name for name in rows if not name.endswith("/RECORD"))
        _, size = rows[name]
        rows[name] = ("sha256=bad", size)
        return rows

    with monkeypatch.context() as context:
        context.setattr(module, "_parse_record", bad_record_digest)
        with pytest.raises(module.IntegrationInstallError):
            module._wheel_details(wheel)

    invalid = tmp_path / "invalid-details.whl"
    invalid.write_bytes(b"invalid")
    with pytest.raises(module.IntegrationInstallError):
        module._wheel_details(invalid)

    wheel = tmp_path / "candidate-inner.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("codex_usage/ok.py", b"x")
    rows = {"codex_usage/ok.py": (hashlib.sha256(b"x").hexdigest(), 1)}

    destination = tmp_path / "mkdir-race"
    destination.mkdir(mode=0o700)
    original_stat = module.os.stat
    first_parent_stat = True

    def missing_parent_once(name, *args, **kwargs):
        nonlocal first_parent_stat
        if name == "codex_usage" and first_parent_stat:
            first_parent_stat = False
            raise FileNotFoundError(name)
        return original_stat(name, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(module.os, "stat", missing_parent_once)

        def mkdir_race(*_args, **_kwargs):
            raise FileExistsError("mkdir race")

        context.setattr(module.os, "mkdir", mkdir_race)
        with pytest.raises(module.IntegrationInstallError):
            module._safe_extract_wheel(
                wheel_path=wheel,
                destination=destination,
                record_rows=rows,
            )

    destination = tmp_path / "bad-parent-identity"
    destination.mkdir(mode=0o700)
    original_stat = module.os.stat
    first_parent_stat = True

    def wrong_parent_stat(name, *args, **kwargs):
        nonlocal first_parent_stat
        if name == "codex_usage" and first_parent_stat:
            first_parent_stat = False
            raise FileNotFoundError(name)
        if name == "codex_usage":
            item = original_stat(name, *args, **kwargs)
            values = list(item)
            values[0] = stat.S_IFDIR | 0o701
            return os.stat_result(values)
        return original_stat(name, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(module.os, "stat", wrong_parent_stat)
        with pytest.raises(module.IntegrationInstallError):
            module._safe_extract_wheel(
                wheel_path=wheel,
                destination=destination,
                record_rows=rows,
            )


def test_installer_postwalk_activation_and_safe_extract_remaining_guards(
    tmp_path, monkeypatch
):
    from codex_usage import integration_installer as module

    for attribute in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
        root = tmp_path / f"postwalk-{attribute}"
        root.mkdir(mode=0o700)
        with monkeypatch.context() as context:
            context.delattr(module.os, attribute, raising=False)
            assert module._postwalk_release(root) == set()

    limited = tmp_path / "limited"
    limited.mkdir(mode=0o700)
    with monkeypatch.context() as context:
        context.setattr(module, "MAX_RELEASE_TREE_ENTRIES", 0)
        with pytest.raises(module.IntegrationInstallError):
            module._postwalk_release(limited)

    hardlinked = tmp_path / "hardlinked"
    hardlinked.mkdir(mode=0o700)
    payload = hardlinked / "payload"
    payload.write_bytes(b"x")
    payload_link = hardlinked / "payload-link"
    payload_link.hardlink_to(payload)
    with pytest.raises(module.IntegrationInstallError):
        module._postwalk_release(hardlinked)

    for name in ("__pycache__", "payload.pyc"):
        root = tmp_path / f"forbidden-{name.replace('.', '-') }"
        root.mkdir(mode=0o700)
        target = root / name
        if name == "__pycache__":
            target.mkdir(mode=0o700)
        else:
            target.write_bytes(b"x")
        with pytest.raises(module.IntegrationInstallError):
            module._postwalk_release(root)

    root = tmp_path / "postwalk-open-error"
    root.mkdir(mode=0o700)
    with monkeypatch.context() as context:
        def fail_open(*_args, **_kwargs):
            raise OSError("open")

        context.setattr(module.os, "open", fail_open)
        with pytest.raises(module.IntegrationInstallError):
            module._postwalk_release(root)

    wheel = tmp_path / "remaining-candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("codex_usage/ok.py", b"x")
    rows = {"codex_usage/ok.py": (hashlib.sha256(b"x").hexdigest(), 1)}

    venv = tmp_path / "activation"
    (venv / "bin").mkdir(parents=True, mode=0o700)
    for attribute in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
        with monkeypatch.context() as context:
            context.delattr(module.os, attribute, raising=False)
            module._remove_activation_files(venv)

    not_directory = tmp_path / "not-directory"
    not_directory.write_bytes(b"x")
    with monkeypatch.context() as context:
        context.delattr(module.os, "O_DIRECTORY", raising=False)
        with pytest.raises(module.IntegrationInstallError):
            module._remove_activation_files(not_directory)

    wrong_bin = tmp_path / "wrong-bin"
    wrong_bin.mkdir(mode=0o700)
    (wrong_bin / "bin").write_bytes(b"x")
    with monkeypatch.context() as context:
        context.delattr(module.os, "O_DIRECTORY", raising=False)
        with pytest.raises(module.IntegrationInstallError):
            module._remove_activation_files(wrong_bin)

    directory_entry = tmp_path / "directory-entry"
    (directory_entry / "bin" / "activate").mkdir(parents=True, mode=0o700)
    module._remove_activation_files(directory_entry)
    assert (directory_entry / "bin" / "activate").is_dir()

    stat_error = tmp_path / "activation-stat-error"
    (stat_error / "bin").mkdir(parents=True, mode=0o700)
    (stat_error / "bin" / "activate").write_text("x", encoding="utf-8")
    original_stat = module.os.stat

    def fail_activation_stat(name, *args, **kwargs):
        if name == "activate" and kwargs.get("dir_fd") is not None:
            raise OSError("stat")
        return original_stat(name, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(module.os, "stat", fail_activation_stat)
        with pytest.raises(module.IntegrationInstallError):
            module._remove_activation_files(stat_error)

    lib64_error = tmp_path / "lib64-stat-error"
    (lib64_error / "bin").mkdir(parents=True, mode=0o700)
    (lib64_error / "lib64").symlink_to("missing-target")
    lib64_calls = 0

    def fail_second_lib64_stat(name, *args, **kwargs):
        nonlocal lib64_calls
        if name == "lib64" and kwargs.get("dir_fd") is not None:
            lib64_calls += 1
            if lib64_calls == 2:
                raise OSError("lib64 stat")
        return original_stat(name, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(module.os, "stat", fail_second_lib64_stat)
        with pytest.raises(module.IntegrationInstallError):
            module._remove_activation_files(lib64_error)

    ordinary_lib64 = tmp_path / "ordinary-lib64"
    (ordinary_lib64 / "bin").mkdir(parents=True, mode=0o700)
    (ordinary_lib64 / "lib64").mkdir(mode=0o700)
    module._remove_activation_files(ordinary_lib64)

    open_error = tmp_path / "activation-open-error"
    (open_error / "bin").mkdir(parents=True, mode=0o700)
    with monkeypatch.context() as context:
        def fail_venv_open(*_args, **_kwargs):
            raise OSError("open")

        context.setattr(module.os, "open", fail_venv_open)
        with pytest.raises(module.IntegrationInstallError):
            module._remove_activation_files(open_error)

    destination = tmp_path / "child-identity"
    destination.mkdir(mode=0o700)
    original_fstat = module.os.fstat

    def wrong_child_identity(fd):
        item = original_fstat(fd)
        try:
            descriptor_path = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            descriptor_path = ""
        if descriptor_path == str(destination / "codex_usage"):
            values = list(item)
            values[0] = stat.S_IFDIR | 0o701
            return os.stat_result(values)
        return item

    with monkeypatch.context() as context:
        context.setattr(module.os, "fstat", wrong_child_identity)
        with pytest.raises(module.IntegrationInstallError):
            module._safe_extract_wheel(
                wheel_path=wheel,
                destination=destination,
                record_rows=rows,
            )

    destination = tmp_path / "preexisting-target"
    (destination / "codex_usage").mkdir(parents=True, mode=0o700)
    (destination / "codex_usage" / "ok.py").write_bytes(b"old")
    with pytest.raises(module._WheelMemberValidationError) as error:
        module._safe_extract_wheel(
            wheel_path=wheel,
            destination=destination,
            record_rows=rows,
        )
    assert error.value.reason == "duplicate_member"

    destination = tmp_path / "bad-parent-mode"
    (destination / "codex_usage").mkdir(parents=True, mode=0o755)
    (destination / "codex_usage").chmod(0o755)
    with pytest.raises(module.IntegrationInstallError):
        module._safe_extract_wheel(
            wheel_path=wheel,
            destination=destination,
            record_rows=rows,
        )

    destination = tmp_path / "open-race"
    destination.mkdir(mode=0o700)
    original_open = module.os.open

    def target_open_race(name, flags, mode=0o777, *, dir_fd=None):
        if name == "ok.py" and dir_fd is not None:
            raise FileExistsError("target race")
        if dir_fd is None:
            return original_open(name, flags, mode)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as context:
        context.setattr(module.os, "open", target_open_race)
        with pytest.raises(module._WheelMemberValidationError) as error:
            module._safe_extract_wheel(
                wheel_path=wheel,
                destination=destination,
                record_rows=rows,
            )
    assert error.value.reason == "duplicate_member"

    destination = tmp_path / "bad-output"
    destination.mkdir(mode=0o700)
    original_fstat = module.os.fstat

    def wrong_output_identity(fd):
        item = original_fstat(fd)
        try:
            descriptor_path = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            descriptor_path = ""
        if descriptor_path == str(destination / "codex_usage" / "ok.py"):
            values = list(item)
            values[0] = stat.S_IFREG | 0o644
            return os.stat_result(values)
        return item

    with monkeypatch.context() as context:
        context.setattr(module.os, "fstat", wrong_output_identity)
        with pytest.raises(module.IntegrationInstallError):
            module._safe_extract_wheel(
                wheel_path=wheel,
                destination=destination,
                record_rows=rows,
            )

    destination = tmp_path / "fdopen-race"
    destination.mkdir(mode=0o700)
    with monkeypatch.context() as context:
        original_fdopen = module.os.fdopen

        def fail_fdopen(fd, *args, **kwargs):
            try:
                descriptor_path = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                descriptor_path = ""
            if descriptor_path == str(destination / "codex_usage" / "ok.py"):
                raise OSError("fdopen")
            return original_fdopen(fd, *args, **kwargs)

        context.setattr(module.os, "fdopen", fail_fdopen)
        with pytest.raises(module.IntegrationInstallError):
            module._safe_extract_wheel(
                wheel_path=wheel,
                destination=destination,
                record_rows=rows,
            )

    destination = tmp_path / "open-failure"
    destination.mkdir(mode=0o700)
    with monkeypatch.context() as context:
        def fail_destination_open(name, *_args, **_kwargs):
            if name == destination:
                raise OSError("destination")
            return original_open(name, *_args, **_kwargs)

        context.setattr(module.os, "open", fail_destination_open)
        with pytest.raises(module.IntegrationInstallError):
            module._safe_extract_wheel(
                wheel_path=wheel,
                destination=destination,
                record_rows=rows,
            )

    for attribute in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
        destination = tmp_path / f"without-{attribute}"
        destination.mkdir(mode=0o700)
        with monkeypatch.context() as context:
            context.delattr(module.os, attribute, raising=False)
            module._safe_extract_wheel(
                wheel_path=wheel,
                destination=destination,
                record_rows=rows,
            )


def test_installer_find_site_packages_remaining_guards(tmp_path, monkeypatch):
    from codex_usage import integration_installer as module

    def make_venv(name, *, lib=True, python=True, site=True):
        root = tmp_path / name
        root.mkdir(mode=0o700)
        if lib:
            lib_root = root / "lib"
            lib_root.mkdir(mode=0o700)
            if python:
                python_root = lib_root / "python3.14"
                python_root.mkdir(mode=0o700)
                if site:
                    (python_root / "site-packages").mkdir(mode=0o700)
        return root

    valid = make_venv("site-valid")
    (valid / "lib" / "other").mkdir(mode=0o700)
    (valid / "lib" / "python3.14" / "other").mkdir(mode=0o700)
    identity = module._directory_identity(valid)
    site_path, site_identity = module._find_site_packages(valid, identity)
    assert site_path.name == "site-packages"
    assert site_identity == module._directory_identity(site_path)

    for attribute in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
        root = make_venv(f"site-without-{attribute}")
        with monkeypatch.context() as context:
            context.delattr(module.os, attribute, raising=False)
            module._find_site_packages(root, module._directory_identity(root))

    wrong_identity = module._DirectoryIdentity(
        identity.device,
        identity.inode,
        identity.permissions + 1,
        identity.gid,
    )
    with pytest.raises(module.IntegrationInstallError):
        module._find_site_packages(valid, wrong_identity)

    missing_lib = make_venv("site-missing-lib", lib=False)
    with pytest.raises(module.IntegrationInstallError):
        module._find_site_packages(missing_lib, module._directory_identity(missing_lib))

    invalid_lib = tmp_path / "site-invalid-lib"
    invalid_lib.mkdir(mode=0o700)
    (invalid_lib / "lib").write_bytes(b"not a directory")
    with pytest.raises(module.IntegrationInstallError):
        module._find_site_packages(invalid_lib, module._directory_identity(invalid_lib))

    opened_lib = make_venv("site-opened-lib")
    original_fstat = module.os.fstat

    def wrong_lib_identity(fd):
        item = original_fstat(fd)
        try:
            descriptor_path = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            descriptor_path = ""
        if descriptor_path == str(opened_lib / "lib"):
            values = list(item)
            values[0] = stat.S_IFDIR | 0o701
            return os.stat_result(values)
        return item

    with monkeypatch.context() as context:
        context.setattr(module.os, "fstat", wrong_lib_identity)
        with pytest.raises(module.IntegrationInstallError):
            module._find_site_packages(
                opened_lib,
                module._directory_identity(opened_lib),
            )

    invalid_python = make_venv("site-invalid-python", python=False)
    (invalid_python / "lib" / "python3.14").write_bytes(b"not a directory")
    with pytest.raises(module.IntegrationInstallError):
        module._find_site_packages(
            invalid_python,
            module._directory_identity(invalid_python),
        )

    invalid_site = make_venv("site-invalid-site", site=False)
    (invalid_site / "lib" / "python3.14" / "site-packages").write_bytes(b"file")
    with pytest.raises(module.IntegrationInstallError):
        module._find_site_packages(
            invalid_site,
            module._directory_identity(invalid_site),
        )

    opened_site = make_venv("site-opened-site")
    original_fstat = module.os.fstat

    def wrong_site_identity(fd):
        item = original_fstat(fd)
        try:
            descriptor_path = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            descriptor_path = ""
        if descriptor_path == str(opened_site / "lib" / "python3.14" / "site-packages"):
            values = list(item)
            values[0] = stat.S_IFDIR | 0o701
            return os.stat_result(values)
        return item

    with monkeypatch.context() as context:
        context.setattr(module.os, "fstat", wrong_site_identity)
        with pytest.raises(module.IntegrationInstallError):
            module._find_site_packages(
                opened_site,
                module._directory_identity(opened_site),
            )

    final_site = make_venv("site-final-site")
    original_fstat = module.os.fstat
    site_fstats = 0

    def wrong_final_site(fd):
        nonlocal site_fstats
        item = original_fstat(fd)
        try:
            descriptor_path = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            descriptor_path = ""
        if descriptor_path == str(final_site / "lib" / "python3.14" / "site-packages"):
            site_fstats += 1
            if site_fstats == 2:
                values = list(item)
                values[0] = stat.S_IFDIR | 0o701
                return os.stat_result(values)
        return item

    with monkeypatch.context() as context:
        context.setattr(module.os, "fstat", wrong_final_site)
        with pytest.raises(module.IntegrationInstallError):
            module._find_site_packages(
                final_site,
                module._directory_identity(final_site),
            )

    no_candidates = make_venv("site-no-candidates", python=False)
    (no_candidates / "lib" / "helper").mkdir(mode=0o700)
    with pytest.raises(module.IntegrationInstallError):
        module._find_site_packages(
            no_candidates,
            module._directory_identity(no_candidates),
        )

    for stage in ("lib", "python", "site"):
        root = make_venv(f"site-open-{stage}")
        with monkeypatch.context() as context:
            original_open = module.os.open

            def make_fail_stage_open(stage_name, fallback):
                def fail_stage_open(name, flags, mode=0o777, *, dir_fd=None):
                    if (
                        (stage_name == "lib" and name == "lib" and dir_fd is not None)
                        or (
                            stage_name == "python"
                            and name == "python3.14"
                            and dir_fd is not None
                        )
                        or (
                            stage_name == "site"
                            and name == "site-packages"
                            and dir_fd is not None
                        )
                    ):
                        raise OSError("stage open")
                    if dir_fd is None:
                        return fallback(name, flags, mode)
                    return fallback(name, flags, mode, dir_fd=dir_fd)

                return fail_stage_open

            context.setattr(module.os, "open", make_fail_stage_open(stage, original_open))
            with pytest.raises(module.IntegrationInstallError):
                module._find_site_packages(root, module._directory_identity(root))

    open_error = make_venv("site-open-error")
    with monkeypatch.context() as context:
        def fail_open(*_args, **_kwargs):
            raise OSError("open")

        context.setattr(module.os, "open", fail_open)
        with pytest.raises(module.IntegrationInstallError):
            module._find_site_packages(open_error, module._directory_identity(open_error))


def test_installer_write_exclusive_remaining_guards(tmp_path, monkeypatch):
    from codex_usage import integration_installer as module

    parent = tmp_path / "write-parent"
    parent.mkdir(mode=0o700)
    parent_identity = module._directory_identity(parent)

    for attribute in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
        target = parent / f"without-{attribute}"
        with monkeypatch.context() as context:
            context.delattr(module.os, attribute, raising=False)
            assert module._write_exclusive(target, b"payload", mode=0o600).permissions == 0o600

    target = parent / "wrong-parent-fd"
    original_fstat = module.os.fstat

    def wrong_parent_fd(fd):
        item = original_fstat(fd)
        try:
            descriptor_path = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            descriptor_path = ""
        if descriptor_path == str(parent):
            values = list(item)
            values[0] = stat.S_IFDIR | 0o701
            return os.stat_result(values)
        return item

    with monkeypatch.context() as context:
        context.setattr(module.os, "fstat", wrong_parent_fd)
        with pytest.raises(module.IntegrationInstallError):
            module._write_exclusive(
                target,
                b"payload",
                mode=0o600,
                parent_identity=parent_identity,
            )

    target = parent / "zero-write"
    with monkeypatch.context() as context:
        context.setattr(module.os, "write", lambda *_args, **_kwargs: 0)
        with pytest.raises(module.IntegrationInstallError):
            module._write_exclusive(target, b"payload", mode=0o600)
        assert not target.exists()

    target = parent / "no-final-rebase"
    original_rebased = module._provisional_rebased
    rebase_calls = 0

    def fail_final_rebase(*args, **kwargs):
        nonlocal rebase_calls
        rebase_calls += 1
        if rebase_calls == 2:
            return None
        return original_rebased(*args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(module, "_provisional_rebased", fail_final_rebase)
        with pytest.raises(module.IntegrationInstallError):
            module._write_exclusive(target, b"payload", mode=0o600)

    target = parent / "wrong-final-fstat"
    original_fstat = module.os.fstat

    def wrong_final_fstat(fd):
        item = original_fstat(fd)
        try:
            descriptor_path = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            descriptor_path = ""
        if descriptor_path == str(target):
            values = list(item)
            values[0] = stat.S_IFREG | 0o644
            return os.stat_result(values)
        return item

    with monkeypatch.context() as context:
        context.setattr(module.os, "fstat", wrong_final_fstat)
        with pytest.raises(module.IntegrationInstallError):
            module._write_exclusive(target, b"payload", mode=0o600)

    target = parent / "open-error"
    with monkeypatch.context() as context:
        def fail_open(*_args, **_kwargs):
            raise OSError("open")

        context.setattr(module.os, "open", fail_open)
        with pytest.raises(module.IntegrationInstallError):
            module._write_exclusive(target, b"payload", mode=0o600)

    target = parent / "close-error"
    close_calls = 0
    original_close = module.os.close

    def fail_first_close(fd):
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise OSError("close")
        return original_close(fd)

    with monkeypatch.context() as context:
        context.setattr(module.os, "close", fail_first_close)
        assert module._write_exclusive(target, b"payload", mode=0o600).permissions == 0o600

    target = parent / "parent-close-error"
    close_calls = 0

    def fail_second_close(fd):
        nonlocal close_calls
        close_calls += 1
        if close_calls == 2:
            raise OSError("parent close")
        return original_close(fd)

    with monkeypatch.context() as context:
        context.setattr(module.os, "close", fail_second_close)
        assert module._write_exclusive(target, b"payload", mode=0o600).permissions == 0o600


def test_installer_build_wheel_directory_guards(tmp_path, monkeypatch):
    from codex_usage import integration_installer as module

    build_root = tmp_path / "build"
    build_root.mkdir(mode=0o700)
    wheel_parent = tmp_path / "wheel-parent"
    wheel_parent.mkdir(mode=0o700)

    monkeypatch.setattr(module, "_require_offline_builder", lambda **_kwargs: None)

    wheel_dir = wheel_parent / "wheel"

    def run_and_create(command, *, env, cwd, pass_fds=()):
        del command, env, cwd, pass_fds
        wheel_dir.mkdir(mode=0o700, exist_ok=True)
        wheel = wheel_dir / module.EXPECTED_WHEEL_NAME
        wheel.write_bytes(b"wheel")
        wheel.chmod(0o600)
        return subprocess.CompletedProcess(["builder"], 0)

    monkeypatch.setattr(module, "_run_builder_bounded", run_and_create)
    with monkeypatch.context() as context:
        context.delattr(module.os, "O_DIRECTORY", raising=False)
        context.delattr(module.os, "O_NOFOLLOW", raising=False)
        context.delattr(module.os, "O_CLOEXEC", raising=False)
        path, identity = module._build_verified_wheel(
            python_executable=Path(sys.executable),
            environment={},
            build_root=build_root,
            wheel_dir=wheel_dir,
        )
        assert path.name == module.EXPECTED_WHEEL_NAME
        assert identity.permissions == 0o600

    invalid_identity_dir = wheel_parent / "invalid-identity"

    def run_invalid_identity(command, *, env, cwd, pass_fds=()):
        del command, env, cwd, pass_fds
        invalid_identity_dir.mkdir(mode=0o700, exist_ok=True)
        wheel = invalid_identity_dir / module.EXPECTED_WHEEL_NAME
        wheel.write_bytes(b"wheel")
        wheel.chmod(0o600)
        return subprocess.CompletedProcess(["builder"], 0)

    original_fstat = module.os.fstat

    def wrong_wheel_identity(fd):
        item = original_fstat(fd)
        try:
            descriptor_path = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            descriptor_path = ""
        if descriptor_path == str(invalid_identity_dir):
            values = list(item)
            values[0] = stat.S_IFDIR | 0o701
            return os.stat_result(values)
        return item

    with monkeypatch.context() as context:
        context.setattr(module, "_run_builder_bounded", run_invalid_identity)
        context.setattr(module.os, "fstat", wrong_wheel_identity)
        with pytest.raises(module.IntegrationInstallError):
            module._build_verified_wheel(
                python_executable=Path(sys.executable),
                environment={},
                build_root=build_root,
                wheel_dir=invalid_identity_dir,
            )

    scan_error_dir = wheel_parent / "scan-error"

    def run_scan_error(command, *, env, cwd, pass_fds=()):
        del command, env, cwd, pass_fds
        scan_error_dir.mkdir(mode=0o700, exist_ok=True)
        return subprocess.CompletedProcess(["builder"], 0)

    with monkeypatch.context() as context:
        context.setattr(module, "_run_builder_bounded", run_scan_error)

        def fail_scandir(*_args, **_kwargs):
            raise OSError("scandir")

        context.setattr(module.os, "scandir", fail_scandir)
        with pytest.raises(module.IntegrationInstallError):
            module._build_verified_wheel(
                python_executable=Path(sys.executable),
                environment={},
                build_root=build_root,
                wheel_dir=scan_error_dir,
            )

    no_open_dir = wheel_parent / "no-open"
    with monkeypatch.context() as context:
        context.setattr(module, "_run_builder_bounded", run_scan_error)
        context.setattr(
            module,
            "_no_symlink_ancestors",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                module.IntegrationInstallError()
            ),
        )
        with pytest.raises(module.IntegrationInstallError):
            module._build_verified_wheel(
                python_executable=Path(sys.executable),
                environment={},
                build_root=build_root,
                wheel_dir=no_open_dir,
            )


def test_installer_release_entry_guards_and_public_wrapper(tmp_path, monkeypatch):
    from codex_usage import integration_installer as module

    source_root = _temporary_source_copy(tmp_path)
    build_root = tmp_path / "build"
    build_root.mkdir(mode=0o700)
    build_identity = module._directory_identity(build_root)
    wrong_identity = module._DirectoryIdentity(
        build_identity.device,
        build_identity.inode,
        build_identity.permissions + 1,
        build_identity.gid,
    )
    with pytest.raises(module.IntegrationInstallError):
        module._copy_source_into_project(
            source_root,
            build_root,
            build_identity=wrong_identity,
        )

    guard_root = tmp_path / "version-guard"
    guard_root.mkdir(mode=0o700)
    data_home, state_home, temporary_root = _roots(guard_root)
    bad_source_root = _temporary_source_copy(guard_root)
    pyproject = bad_source_root / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            'version = "0.6.542"',
            'version = "0.0.0"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(module.IntegrationInstallError):
        module.install_release(
            source_root=bad_source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )

    with monkeypatch.context() as context:
        def fail_install(**_kwargs):
            raise RuntimeError("wrapper")

        context.setattr(module, "_install_release", fail_install)
        with pytest.raises(module.IntegrationInstallError):
            module.install_release(
                source_root=source_root,
                state_home=tmp_path,
                data_home=tmp_path,
                python_executable=Path(sys.executable),
                temporary_root=tmp_path,
            )


@pytest.mark.parametrize("drift_call", [3, 4])
def test_installer_source_drift_guards_at_build_seams(tmp_path, monkeypatch, drift_call):
    from codex_usage import integration_installer as module

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    original_rehash = module._rehash_source_manifest
    calls = 0

    def drift_at_seam(path):
        nonlocal calls
        calls += 1
        result = original_rehash(path)
        if calls == drift_call:
            return {"changed.py": "0" * 64}
        return result

    monkeypatch.setattr(module, "_rehash_source_manifest", drift_at_seam)
    with pytest.raises(module.IntegrationInstallError):
        module.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )
    assert calls >= drift_call


def test_installer_candidate_read_failure_guard(tmp_path, monkeypatch):
    from codex_usage import integration_attestation
    from codex_usage import integration_installer as module

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    original_read_manifest = module._read_manifest

    def fail_candidate_manifest(path):
        if path.name.startswith("candidate-"):
            raise integration_attestation.IntegrationAttestationUnavailable()
        return original_read_manifest(path)

    monkeypatch.setattr(module, "_read_manifest", fail_candidate_manifest)
    with pytest.raises(module.IntegrationInstallError):
        module.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )


@pytest.mark.parametrize("failure", ["tree", "wheel"])
def test_installer_post_build_failure_guards(tmp_path, monkeypatch, failure):
    from codex_usage import integration_installer as module

    data_home, state_home, temporary_root = _roots(tmp_path)
    source_root = _temporary_source_copy(tmp_path)
    if failure == "tree":
        original_tree_hash = module._release_tree_sha256
        calls = 0

        def fail_final_tree_hash(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original_tree_hash(*args, **kwargs)
            if calls == 2:
                return "0" * 64
            return result

        monkeypatch.setattr(module, "_release_tree_sha256", fail_final_tree_hash)
    else:
        monkeypatch.setattr(
            module,
            "_wheel_details",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("wheel details")),
        )
    with pytest.raises(module.IntegrationInstallError):
        module.install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=Path(sys.executable),
            temporary_root=temporary_root,
        )


def test_installer_active_manifest_and_rollback_guards(tmp_path, monkeypatch):
    from codex_usage import integration_installer as module
    from codex_usage.private_io import write_private_text

    first, data_home, state_home = _install(tmp_path)
    integration = state_home / "codex-usage" / "integration"
    previous = integration / module.PREVIOUS_NAME
    active = integration / module.ACTIVE_NAME
    write_private_text(
        previous,
        active.read_text(encoding="utf-8"),
        label="test previous manifest",
        mode=0o600,
    )

    original_read = module.read_private_text

    second_root = tmp_path / "second-install"
    second_root.mkdir(mode=0o700)
    second_source = _temporary_source_copy(second_root)
    second_entrypoint = second_source / "src/codex_usage/integration_snapshot.py"
    second_entrypoint.write_bytes(second_entrypoint.read_bytes() + b"\n# second release\n")
    second_temporary = second_root / "temporary"
    second_temporary.mkdir(mode=0o700)

    def bad_active_mode(path, **kwargs):
        text, item = original_read(path, **kwargs)
        if path == active:
            values = list(item)
            values[0] = stat.S_IFREG | 0o644
            return text, os.stat_result(values)
        return text, item

    with monkeypatch.context() as context:
        context.setattr(module, "read_private_text", bad_active_mode)
        with pytest.raises(module.IntegrationInstallError):
            module.install_release(
                source_root=second_source,
                state_home=state_home,
                data_home=data_home,
                python_executable=Path(sys.executable),
                temporary_root=second_temporary,
            )

    def bad_previous_mode(path, **kwargs):
        text, item = original_read(path, **kwargs)
        if path == previous:
            values = list(item)
            values[0] = stat.S_IFREG | 0o644
            return text, os.stat_result(values)
        return text, item

    with monkeypatch.context() as context:
        context.setattr(module, "read_private_text", bad_previous_mode)
        with pytest.raises(module.IntegrationInstallError):
            module.rollback_active_release(state_home=state_home, data_home=data_home)

    with monkeypatch.context() as context:
        context.setattr(
            module,
            "_require_private_dir",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                module.IntegrationInstallError()
            ),
        )
        with pytest.raises(module.IntegrationInstallError):
            module.rollback_active_release(state_home=state_home, data_home=data_home)

    with monkeypatch.context() as context:
        context.setattr(
            module,
            "_verify_manifest",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("verify")),
        )
        with pytest.raises(module.IntegrationInstallError):
            module.rollback_active_release(state_home=state_home, data_home=data_home)

    assert first.release_dir.is_dir()


def test_installer_finally_false_edges(tmp_path, monkeypatch):
    from codex_usage import integration_installer as module

    parent = tmp_path / "finally-parent"
    parent.mkdir(mode=0o700)
    parent_identity = module._directory_identity(parent)
    original_open = module.os.open

    source = parent / "source"
    source.mkdir(mode=0o700)
    source_identity = module._identity(source)
    target = parent / "target"

    def fail_parent_open(path, *args, **kwargs):
        if path == parent:
            raise OSError("parent open")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(module.os, "open", fail_parent_open)
        with pytest.raises(module.IntegrationInstallError):
            module._rename_owned_directory(source, target, parent_identity, source_identity)

    with monkeypatch.context() as context:
        context.setattr(module.os, "open", fail_parent_open)
        with pytest.raises(module.IntegrationInstallError):
            module._create_private_directory(parent / "create-failure", parent_identity)

    with monkeypatch.context() as context:
        context.setattr(module.os, "open", fail_parent_open)
        with pytest.raises(module.IntegrationInstallError):
            module._require_private_dir(
                parent / "require-failure",
                None,
                True,
                parent_identity=parent_identity,
            )

    source_file = parent / "source-file"
    source_file.write_bytes(b"source")
    source_file.chmod(0o600)
    original_lstat = Path.lstat

    def fail_source_lstat(path):
        if path == source_file:
            raise OSError("source lstat")
        return original_lstat(path)

    with monkeypatch.context() as context:
        context.setattr(Path, "lstat", fail_source_lstat)
        with pytest.raises(module.IntegrationInstallError):
            module._copy_regular(source_file, parent / "copy-failure")

    build_root = tmp_path / "finally-build"
    build_root.mkdir(mode=0o700)
    wheel_parent = tmp_path / "finally-wheel-parent"
    wheel_parent.mkdir(mode=0o700)
    wheel_dir = wheel_parent / "wheel"
    original_no_symlink = module._no_symlink_ancestors

    def fail_wheel_ancestors(path):
        if path == wheel_dir:
            raise module.IntegrationInstallError()
        return original_no_symlink(path)

    with monkeypatch.context() as context:
        context.setattr(module, "_require_offline_builder", lambda **_kwargs: None)
        context.setattr(
            module,
            "_run_builder_bounded",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(["builder"], 0),
        )
        context.setattr(module, "_no_symlink_ancestors", fail_wheel_ancestors)
        with pytest.raises(module.IntegrationInstallError):
            module._build_verified_wheel(
                python_executable=Path(sys.executable),
                environment={},
                build_root=build_root,
                wheel_dir=wheel_dir,
            )

    wheel = tmp_path / "finally-candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("codex_usage/ok.py", b"x")
    destination = tmp_path / "finally-destination"
    destination.mkdir(mode=0o700)
    rows = {"codex_usage/ok.py": (hashlib.sha256(b"x").hexdigest(), 1)}

    def fail_destination_open(path, *args, **kwargs):
        if path == destination:
            raise OSError("destination open")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(module.os, "open", fail_destination_open)
        with pytest.raises(module.IntegrationInstallError):
            module._safe_extract_wheel(
                wheel_path=wheel,
                destination=destination,
                record_rows=rows,
            )

    venv = tmp_path / "finally-venv"
    (venv / "bin").mkdir(parents=True, mode=0o700)

    def fail_venv_open(path, *args, **kwargs):
        if path == venv:
            raise OSError("venv open")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(module.os, "open", fail_venv_open)
        with pytest.raises(module.IntegrationInstallError):
            module._remove_activation_files(venv)

    site_venv = tmp_path / "finally-site-venv"
    (site_venv / "lib" / "python3.14" / "site-packages").mkdir(
        parents=True,
        mode=0o700,
    )

    def fail_lib_open(name, flags, mode=0o777, *, dir_fd=None):
        if name == "lib" and dir_fd is not None:
            raise OSError("lib open")
        if dir_fd is None:
            return original_open(name, flags, mode)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as context:
        context.setattr(module.os, "open", fail_lib_open)
        with pytest.raises(module.IntegrationInstallError):
            module._find_site_packages(
                site_venv,
                module._directory_identity(site_venv),
            )

    with monkeypatch.context() as context:
        context.setattr(module.os, "open", fail_parent_open)
        with pytest.raises(module.IntegrationInstallError):
            module._write_exclusive(parent / "exclusive-failure", b"x", mode=0o600)


def test_attestation_verify_maps_metadata_read_and_content_failures(tmp_path, monkeypatch):
    from codex_usage import integration_attestation as module

    release, data_home, state_home = _install(tmp_path)
    original_read = module._read_nofollow_bytes
    metadata_calls = 0

    def fail_metadata(path, **kwargs):
        nonlocal metadata_calls
        if path.name == "METADATA":
            metadata_calls += 1
            if metadata_calls == 2:
                raise module.IntegrationAttestationUnavailable()
        return original_read(path, **kwargs)

    monkeypatch.setattr(module, "_read_nofollow_bytes", fail_metadata)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module.verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.entrypoint_path,
        )

    monkeypatch.setattr(module, "_read_nofollow_bytes", original_read)
    metadata_calls = 0

    def bad_metadata(path, **kwargs):
        nonlocal metadata_calls
        if path.name == "METADATA":
            metadata_calls += 1
            if metadata_calls == 2:
                return b"Name: wrong\nVersion: wrong\n"
        return original_read(path, **kwargs)

    monkeypatch.setattr(module, "_read_nofollow_bytes", bad_metadata)
    with pytest.raises(module.IntegrationAttestationUnavailable):
        module.verify_active_release(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=release.entrypoint_path,
        )
