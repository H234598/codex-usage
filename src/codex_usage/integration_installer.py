from __future__ import annotations

import ast
import base64
import csv
import hashlib
import io
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import venv
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .integration_attestation import (
    MAX_ATTESTATION_FILE_BYTES,
    MAX_RELEASE_TREE_ENTRIES,
    ActiveRelease,
    IntegrationAttestationUnavailable,
    _read_manifest,
    _release_tree_sha256,
    _verify_manifest,
)
from .private_io import (
    ensure_private_directory,
    private_path_lock,
    read_private_text,
    write_private_text,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RELEASE_VERSION = "0.6.532"
PRODUCER_DISTRIBUTION = "codex_usage_integration_producer"
SOURCE_MODULES = (
    "__init__.py",
    "account_lock.py",
    "config.py",
    "consumption.py",
    "extractor.py",
    "integration_attestation.py",
    "integration_entrypoint.py",
    "integration_snapshot.py",
    "json_utils.py",
    "models.py",
    "history.py",
    "private_io.py",
    "state.py",
    "usage_limits.py",
    "usage_resets.py",
)
SOURCE_MANIFEST_FILES = (
    "pyproject.toml",
    *(f"src/codex_usage/{name}" for name in SOURCE_MODULES),
)
ACTIVE_NAME = "active.json"
PREVIOUS_NAME = "previous.json"
RELEASE_LOCK_STEM = "producer-install"
DIST_INFO_PREFIX = "codex_usage_integration_producer-0.6.532.dist-info"
DIST_INFO_FILES = frozenset({"METADATA", "WHEEL", "RECORD", "top_level.txt"})
EXPECTED_WHEEL_NAME = "codex_usage_integration_producer-0.6.532-py3-none-any.whl"
BUILDER_PREFLIGHT_TIMEOUT_SECONDS = 30
BUILDER_PREFLIGHT_MAX_OUTPUT_BYTES = 64 * 1024
BUILDER_WHEEL_TIMEOUT_SECONDS = 120
MAX_INSTALL_FILE_BYTES = MAX_ATTESTATION_FILE_BYTES
_BUILDER_PREFLIGHT_CODE = (
    "import json, setuptools\n"
    "from setuptools.command.bdist_wheel import bdist_wheel\n"
    "parts=tuple(int(part) for part in setuptools.__version__.split('.')[:2])\n"
    "assert parts >= (77, 0) and bdist_wheel.__name__ == 'bdist_wheel'\n"
    "print(json.dumps({'backend':'setuptools.command.bdist_wheel.bdist_wheel',"
    "'setuptools':setuptools.__version__}, sort_keys=True))\n"
)
_GENERATED_PYPROJECT = """[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = "codex-usage-integration-producer"
version = "0.6.532"
requires-python = ">=3.11"
dependencies = []

[tool.setuptools.packages.find]
where = ["src"]
"""
_FORBIDDEN_RUNTIME_MODULES = frozenset(
    {
        "cli",
        "browser",
        "direct",
        "app_server",
        "oauth_browser",
        "scheduler",
        "bridge",
        "service",
        "integration_installer",
    }
)


class IntegrationInstallError(Exception):
    pass


class IntegrationCleanupError(IntegrationInstallError):
    pass


@dataclass(frozen=True)
class _DirectoryIdentity:
    device: int
    inode: int
    permissions: int


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    permissions: int


@dataclass(frozen=True)
class _ProvisionalIdentity:
    device: int
    inode: int
    uid: int
    file_type: int
    permissions: int


class _WheelMemberValidationError(IntegrationInstallError):
    def __init__(self, reason: str):
        if reason not in {
            "duplicate_member",
            "unsafe_path",
            "symlink_member",
            "nonregular_member",
            "record_mismatch",
        }:
            raise ValueError("invalid wheel validation reason")
        self.reason = reason
        super().__init__(reason)


def _fail() -> None:
    raise IntegrationInstallError()


def _absolute(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or "\x00" in str(path):
        _fail()
    return path


def _no_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if part in {"", "."}:
            continue
        current /= part
        try:
            item = current.lstat()
        except FileNotFoundError:
            break
        except (OSError, ValueError):
            _fail()
        if stat.S_ISLNK(item.st_mode):
            _fail()


def _identity(path: Path) -> _DirectoryIdentity:
    try:
        item = path.lstat()
    except (OSError, ValueError):
        _fail()
    if (
        not stat.S_ISDIR(item.st_mode)
        or stat.S_ISLNK(item.st_mode)
        or item.st_uid != os.getuid()
        or stat.S_IMODE(item.st_mode) != 0o700
    ):
        _fail()
    return _DirectoryIdentity(item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode))


def _directory_identity(path: Path) -> _DirectoryIdentity:
    try:
        _no_symlink_ancestors(path)
        item = path.lstat()
    except (OSError, ValueError):
        _fail()
    if not stat.S_ISDIR(item.st_mode) or item.st_uid != os.getuid():
        _fail()
    return _DirectoryIdentity(item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode))


def _file_identity_for_mode(path: Path, mode: int) -> _FileIdentity:
    try:
        _no_symlink_ancestors(path.parent)
        item = path.lstat()
    except (OSError, ValueError):
        _fail()
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_nlink != 1
        or item.st_uid != os.getuid()
        or stat.S_IMODE(item.st_mode) != mode
    ):
        _fail()
    return _FileIdentity(item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode))


def _file_identity(path: Path) -> _FileIdentity:
    return _file_identity_for_mode(path, 0o600)


def _provisional_from_stat(item: os.stat_result) -> _ProvisionalIdentity:
    return _ProvisionalIdentity(
        item.st_dev,
        item.st_ino,
        item.st_uid,
        stat.S_IFMT(item.st_mode),
        stat.S_IMODE(item.st_mode),
    )


def _provisional_path_identity(path: Path, *, directory: bool) -> _ProvisionalIdentity:
    try:
        _no_symlink_ancestors(path.parent)
        item = path.lstat()
    except (OSError, ValueError):
        _fail()
    if item.st_uid != os.getuid() or stat.S_ISLNK(item.st_mode):
        _fail()
    if directory:
        if not stat.S_ISDIR(item.st_mode):
            _fail()
    elif not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
        _fail()
    return _provisional_from_stat(item)


def _provisional_fd_identity(fd: int) -> _ProvisionalIdentity:
    try:
        item = os.fstat(fd)
    except OSError:
        _fail()
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_nlink != 1
        or item.st_uid != os.getuid()
    ):
        _fail()
    return _provisional_from_stat(item)


def _provisional_rebased(
    path: Path,
    identity: _ProvisionalIdentity,
    parent_identity: _DirectoryIdentity,
    *,
    directory: bool,
) -> _ProvisionalIdentity | None:
    try:
        _no_symlink_ancestors(path.parent)
        if _directory_identity(path.parent) != parent_identity:
            return None
        item = path.lstat()
        if item.st_uid != os.getuid() or stat.S_ISLNK(item.st_mode):
            return None
        if directory:
            if not stat.S_ISDIR(item.st_mode):
                return None
        elif not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
            return None
        current = _provisional_from_stat(item)
        if (
            current.device != identity.device
            or current.inode != identity.inode
            or current.uid != identity.uid
            or current.file_type != identity.file_type
        ):
            return None
        return current
    except (IntegrationInstallError, OSError, ValueError):
        return None


def _provisional_matches(
    path: Path,
    identity: _ProvisionalIdentity,
    parent_identity: _DirectoryIdentity,
    *,
    directory: bool,
) -> bool:
    return _provisional_rebased(
        path,
        identity,
        parent_identity,
        directory=directory,
    ) == identity


def _cleanup_provisional(
    path: Path,
    identity: _ProvisionalIdentity,
    parent_identity: _DirectoryIdentity,
    *,
    directory: bool,
) -> bool:
    if not _provisional_matches(
        path,
        identity,
        parent_identity,
        directory=directory,
    ):
        return False
    try:
        if directory:
            path.rmdir()
        else:
            path.unlink()
    except (OSError, ValueError):
        return False
    return not path.exists() and not path.is_symlink()


def _cleanup_provisional_after_failure(
    path: Path,
    identity: _ProvisionalIdentity,
    parent_identity: _DirectoryIdentity,
    *,
    directory: bool,
) -> bool:
    current = _provisional_rebased(
        path,
        identity,
        parent_identity,
        directory=directory,
    )
    return _cleanup_provisional(
        path,
        current or identity,
        parent_identity,
        directory=directory,
    )


def _create_private_directory(
    path: Path,
    parent_identity: _DirectoryIdentity,
) -> _DirectoryIdentity:
    path = _absolute(path)
    _no_symlink_ancestors(path.parent)
    if path.exists() or path.is_symlink():
        _fail()
    provisional: _ProvisionalIdentity | None = None
    try:
        if _identity(path.parent) != parent_identity:
            _fail()
        path.mkdir(mode=0o700)
        provisional = _provisional_path_identity(path, directory=True)
        if _identity(path.parent) != parent_identity:
            _fail()
        path.chmod(0o700)
        final_provisional = _provisional_rebased(
            path,
            provisional,
            parent_identity,
            directory=True,
        )
        if final_provisional is None:
            _fail()
        final = _require_private_dir(path, None, False)
        if (
            _identity(path.parent) != parent_identity
            or final.device != final_provisional.device
            or final.inode != final_provisional.inode
        ):
            _fail()
        return final
    except IntegrationInstallError:
        if provisional is not None:
            _cleanup_provisional_after_failure(
                path,
                provisional,
                parent_identity,
                directory=True,
            )
        raise
    except (OSError, ValueError):
        if provisional is not None and not _cleanup_provisional_after_failure(
            path,
            provisional,
            parent_identity,
            directory=True,
        ):
            _fail()
        _fail()


def _owned_file_matches(
    path: Path,
    identity: _FileIdentity,
    parent_identity: _DirectoryIdentity,
) -> bool:
    try:
        _no_symlink_ancestors(path.parent)
        return _identity(path.parent) == parent_identity and _file_identity(path) == identity
    except IntegrationInstallError:
        return False


def _owned_directory_matches(
    path: Path,
    identity: _DirectoryIdentity,
    parent_identity: _DirectoryIdentity,
) -> bool:
    try:
        _no_symlink_ancestors(path.parent)
        return _identity(path.parent) == parent_identity and _identity(path) == identity
    except IntegrationInstallError:
        return False


def _cleanup_owned_file(
    path: Path,
    identity: _FileIdentity,
    parent_identity: _DirectoryIdentity,
) -> bool:
    if not _owned_file_matches(path, identity, parent_identity):
        return False
    try:
        path.unlink()
    except (OSError, ValueError):
        return False
    return not path.exists() and not path.is_symlink()


def _cleanup_owned_directory(
    path: Path,
    identity: _DirectoryIdentity,
    parent_identity: _DirectoryIdentity,
) -> bool:
    if not _owned_directory_matches(path, identity, parent_identity):
        return False
    try:
        shutil.rmtree(path)
    except (OSError, ValueError):
        return False
    return not path.exists() and not path.is_symlink()


def _require_private_dir(
    path: Path,
    expected: _DirectoryIdentity | None,
    create: bool,
) -> _DirectoryIdentity:
    path = _absolute(path)
    _no_symlink_ancestors(path.parent)
    if create and not path.exists() and not path.is_symlink():
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except (OSError, ValueError):
            _fail()
    identity = _identity(path)
    if expected is not None and identity != expected:
        _fail()
    return identity


def _bootstrap_integration_dir(
    state_home: Path,
) -> tuple[_DirectoryIdentity, _DirectoryIdentity]:
    state_home = _absolute(state_home)
    _require_private_dir(state_home, None, False)
    app_dir = state_home / "codex-usage"
    integration_dir = app_dir / "integration"
    app_identity = _require_private_dir(app_dir, None, True)
    integration_identity = _require_private_dir(integration_dir, None, True)
    return app_identity, integration_identity


def _revalidate_bootstrap(
    state_home: Path,
    app_identity: _DirectoryIdentity,
    integration_identity: _DirectoryIdentity,
) -> None:
    app_dir = state_home / "codex-usage"
    integration_dir = app_dir / "integration"
    for _ in range(2):
        if _require_private_dir(app_dir, app_identity, False) != app_identity:
            _fail()
        if (
            _require_private_dir(integration_dir, integration_identity, False)
            != integration_identity
        ):
            _fail()


def _copy_regular(source: Path, target: Path, *, mode: int = 0o600) -> None:
    fd = -1
    try:
        _no_symlink_ancestors(source.parent)
        source_stat = source.lstat()
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
            _fail()
        ensure_private_directory(target.parent, label="integration target directory")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target, flags, mode)
        destination = os.fdopen(fd, "wb")
        fd = -1
        with destination:
            destination.write(_read_nofollow(source))
        target.chmod(mode)
    except IntegrationInstallError:
        raise
    except (OSError, ValueError):
        _fail()
    finally:
        if fd >= 0:
            os.close(fd)


def _read_nofollow(path: Path) -> bytes:
    fd = -1
    try:
        _no_symlink_ancestors(path.parent)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        item = os.fstat(fd)
        if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
            _fail()
        if item.st_size > MAX_INSTALL_FILE_BYTES:
            _fail()
        with os.fdopen(fd, "rb") as source:
            fd = -1
            payload = source.read(MAX_INSTALL_FILE_BYTES + 1)
            if len(payload) > MAX_INSTALL_FILE_BYTES:
                _fail()
            return payload
    except IntegrationInstallError:
        raise
    except (OSError, ValueError):
        _fail()
    finally:
        if fd >= 0:
            os.close(fd)


def _resolve_python_executable(path: Path) -> Path:
    path = _absolute(path)
    _no_symlink_ancestors(path.parent)
    try:
        resolved = path.resolve(strict=True)
        _no_symlink_ancestors(resolved.parent)
        item = resolved.lstat()
    except (OSError, RuntimeError, ValueError):
        _fail()
    if not stat.S_ISREG(item.st_mode) or not os.access(resolved, os.X_OK):
        _fail()
    return resolved


def _temporary_source_copy(destination_root: Path) -> Path:
    destination_root = _absolute(destination_root)
    _require_private_dir(destination_root, None, False)
    destination = destination_root / "source"
    _require_private_dir(destination, None, True)
    for relative_text in SOURCE_MANIFEST_FILES:
        relative = Path(relative_text)
        source = PROJECT_ROOT / relative
        if not source.is_file() or source.is_symlink():
            _fail()
        _copy_regular(source, destination / relative)
    files = _postwalk_release(destination)
    if files != set(SOURCE_MANIFEST_FILES):
        _fail()
    return destination


def _rehash_source_manifest(source_root: Path) -> dict[str, str]:
    source_root = _absolute(source_root)
    _require_private_dir(source_root, None, False)
    result: dict[str, str] = {}
    for relative_text in SOURCE_MANIFEST_FILES:
        path = source_root / relative_text
        payload = _read_nofollow(path)
        result[relative_text] = hashlib.sha256(payload).hexdigest()
    return result


def _source_digest(manifest: Mapping[str, str]) -> str:
    rows = b"".join(
        f"{relative}\0{manifest[relative]}\n".encode("ascii")
        for relative in sorted(manifest)
    )
    return hashlib.sha256(rows).hexdigest()


def _shell_single_quote(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value or "'" in value:
        _fail()
    return "'" + value + "'"


def _sanitized_build_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "PIP_NO_INDEX": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PIP_CONFIG_FILE": "/dev/null",
        "PYTHONNOUSERSITE": "1",
    }


def _terminate_preflight_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ValueError):
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _kill_process_group(process_group_id: int) -> None:
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_builder_preflight(
    *,
    python_executable: Path,
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    command = [str(python_executable), "-I", "-c", _BUILDER_PREFLIGHT_CODE]
    process = subprocess.Popen(
        command,
        env=dict(environment),
        cwd=None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    stream = process.stdout
    if stream is None:
        _terminate_preflight_process(process)
        raise OSError("builder preflight stdout unavailable")
    selector = selectors.DefaultSelector()
    output = bytearray()
    deadline = time.monotonic() + BUILDER_PREFLIGHT_TIMEOUT_SECONDS
    try:
        selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_preflight_process(process)
                raise subprocess.TimeoutExpired(command, BUILDER_PREFLIGHT_TIMEOUT_SECONDS)
            ready = selector.select(remaining)
            if not ready:
                _terminate_preflight_process(process)
                raise subprocess.TimeoutExpired(command, BUILDER_PREFLIGHT_TIMEOUT_SECONDS)
            for key, _ in ready:
                chunk = os.read(
                    key.fileobj.fileno(),
                    min(8192, BUILDER_PREFLIGHT_MAX_OUTPUT_BYTES + 1 - len(output)),
                )
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > BUILDER_PREFLIGHT_MAX_OUTPUT_BYTES:
                    _terminate_preflight_process(process)
                    raise IntegrationInstallError()
        try:
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _terminate_preflight_process(process)
            raise
        return subprocess.CompletedProcess(
            command,
            returncode,
            bytes(output).decode("utf-8", "replace"),
            "",
        )
    except BaseException:
        if process.poll() is None:
            _terminate_preflight_process(process)
        raise
    finally:
        selector.close()
        stream.close()


def _require_offline_builder(
    *,
    python_executable: Path,
    environment: Mapping[str, str],
) -> None:
    try:
        result = _run_builder_preflight(
            python_executable=python_executable,
            environment=environment,
        )
    except Exception:
        _fail()
    if result.returncode != 0 or not isinstance(result.stdout, str):
        _fail()
    lines = result.stdout.splitlines()
    if len(lines) != 1:
        _fail()
    try:
        payload = json.loads(lines[0])
    except (TypeError, ValueError):
        _fail()
    if not isinstance(payload, dict):
        _fail()
    if payload.get("backend") != "setuptools.command.bdist_wheel.bdist_wheel":
        _fail()
    version = payload.get("setuptools")
    if not isinstance(version, str):
        _fail()
    try:
        parts = tuple(int(part) for part in version.split(".")[:2])
    except ValueError:
        _fail()
    if len(parts) != 2 or parts < (77, 0):
        _fail()


def _build_verified_wheel(
    *,
    python_executable: Path,
    environment: Mapping[str, str],
    build_root: Path,
    wheel_dir: Path,
) -> Path:
    _require_offline_builder(
        python_executable=python_executable,
        environment=environment,
    )
    _require_private_dir(build_root, None, False)
    _require_private_dir(wheel_dir.parent, None, False)
    _require_private_dir(wheel_dir, None, True)
    command = [
        str(python_executable),
        "-I",
        "-m",
        "pip",
        "wheel",
        "--no-deps",
        "--no-build-isolation",
        "--no-index",
        "--no-cache-dir",
        "--wheel-dir",
        str(wheel_dir),
        str(build_root),
    ]
    try:
        result = _run_builder_bounded(
            command,
            env=dict(environment),
            cwd=build_root,
        )
    except Exception:
        _fail()
    if result.returncode != 0:
        _fail()
    wheels: list[Path] = []
    entries_seen = 0
    for path in wheel_dir.iterdir():
        entries_seen += 1
        if entries_seen > MAX_RELEASE_TREE_ENTRIES:
            _fail()
        if path.is_file() and path.name.endswith(".whl") and not path.is_symlink():
            wheels.append(path)
    wheels.sort()
    if len(wheels) != 1 or wheels[0].name != EXPECTED_WHEEL_NAME:
        _fail()
    return wheels[0]


def _run_builder_bounded(
    command: list[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[bytes]:
    process: subprocess.Popen[bytes] | None = None
    process_group_id: int | None = None
    try:
        process = subprocess.Popen(
            command,
            env=dict(env),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        try:
            process_group_id = os.getpgid(process.pid)
        except OSError:
            process_group_id = process.pid
        returncode = process.wait(timeout=BUILDER_WHEEL_TIMEOUT_SECONDS)
        if process_group_id is not None:
            _kill_process_group(process_group_id)
    except BaseException:
        if process is not None and process.poll() is None:
            _terminate_preflight_process(process)
        elif process_group_id is not None:
            _kill_process_group(process_group_id)
        raise
    return subprocess.CompletedProcess(command, returncode)


def _resolve_local_import_targets(*, node: ast.AST) -> frozenset[str]:
    targets: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "codex_usage":
                targets.add("__init__.py")
            elif alias.name.startswith("codex_usage."):
                suffix = alias.name[len("codex_usage.") :]
                if "." in suffix or not suffix:
                    _fail()
                targets.add(suffix + ".py")
            else:
                continue
        return frozenset(targets)
    if not isinstance(node, ast.ImportFrom):
        return frozenset()
    if node.level not in {0, 1}:
        _fail()
    if node.level == 1:
        base = node.module or ""
    else:
        base = node.module or ""
        if not base.startswith("codex_usage"):
            return frozenset()
        if base == "codex_usage":
            base = ""
        else:
            base = base[len("codex_usage.") :]
    if base and "." in base:
        _fail()
    if base:
        targets.add(base + ".py")
    else:
        for alias in node.names:
            if alias.name == "*":
                _fail()
            targets.add(alias.name + ".py")
    return frozenset(targets)


def _validate_runtime_import_closure(
    modules: Mapping[str, bytes], *, require_available: bool = True
) -> None:
    available = {
        name[len("codex_usage/") : -3]
        for name in modules
        if name.startswith("codex_usage/") and name.endswith(".py")
    }
    declared = {name[:-3] for name in SOURCE_MODULES}
    for path_text, payload in modules.items():
        if not path_text.startswith("codex_usage/") or not path_text.endswith(".py"):
            _fail()
        try:
            tree = ast.parse(payload.decode("utf-8"), filename=path_text)
        except (UnicodeDecodeError, SyntaxError):
            _fail()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            targets = _resolve_local_import_targets(node=node)
            for target in targets:
                flat = target[:-3] if target.endswith(".py") else target
                if flat not in declared or flat in _FORBIDDEN_RUNTIME_MODULES:
                    _fail()
                if require_available and available and flat not in available:
                    _fail()


def _record_hash(payload: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode("ascii")
    return "sha256=" + encoded.rstrip("=")


def _record_hash_matches(payload: bytes, digest: str) -> bool:
    return digest in {
        _record_hash(payload),
        hashlib.sha256(payload).hexdigest(),
    }


def _read_bounded_wheel_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if not isinstance(info.file_size, int) or info.file_size < 0:
        _fail()
    if info.file_size > MAX_INSTALL_FILE_BYTES:
        _fail()
    with archive.open(info, "r") as source:
        payload = source.read(MAX_INSTALL_FILE_BYTES + 1)
        if len(payload) > MAX_INSTALL_FILE_BYTES or source.read(1):
            _fail()
    return payload


def _bounded_wheel_infos(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_RELEASE_TREE_ENTRIES:
        _fail()
    return infos


def _parse_record(record_payload: bytes) -> dict[str, tuple[str, int]]:
    result: dict[str, tuple[str, int]] = {}
    try:
        reader = csv.reader(io.StringIO(record_payload.decode("utf-8")))
        rows_seen = 0
        for row in reader:
            rows_seen += 1
            if rows_seen > MAX_RELEASE_TREE_ENTRIES:
                _fail()
            if len(row) != 3 or row[0] in result:
                _fail()
            path_text, digest, size_text = row
            if not path_text or "\\" in path_text or path_text.startswith("/"):
                _fail()
            if any(part in {"", ".", ".."} for part in path_text.split("/")):
                _fail()
            if digest or size_text:
                if not digest or not size_text.isdecimal():
                    _fail()
                result[path_text] = (digest, int(size_text))
            else:
                result[path_text] = ("", -1)
    except (UnicodeDecodeError, csv.Error):
        _fail()
    return result


def _safe_extract_wheel(
    *,
    wheel_path: Path,
    destination: Path,
    record_rows: Mapping[str, tuple[str, int]],
) -> None:
    pending: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    try:
        wheel_payload = _read_nofollow(wheel_path)
        with zipfile.ZipFile(io.BytesIO(wheel_payload), "r") as archive:
            infos = _bounded_wheel_infos(archive)
            for info in infos:
                name = info.filename
                if name in seen:
                    raise _WheelMemberValidationError("duplicate_member")
                seen.add(name)
                if (
                    not name
                    or "\x00" in name
                    or "\\" in name
                    or name.startswith("/")
                    or name.endswith("/")
                ):
                    raise _WheelMemberValidationError("unsafe_path")
                parts = PurePosixPath(name).parts
                if any(part in {"", ".", ".."} for part in parts) or "/".join(parts) != name:
                    raise _WheelMemberValidationError("unsafe_path")
                mode = (info.external_attr >> 16) & 0o177777
                file_type = stat.S_IFMT(mode)
                if file_type == stat.S_IFLNK:
                    raise _WheelMemberValidationError("symlink_member")
                if file_type not in {0, stat.S_IFREG}:
                    raise _WheelMemberValidationError("nonregular_member")
                if name not in record_rows:
                    raise _WheelMemberValidationError("record_mismatch")
                payload = _read_bounded_wheel_member(archive, info)
                digest, size = record_rows[name]
                record_self_row = name.endswith("/RECORD") and not digest and size == -1
                if not record_self_row and (
                    not digest
                    or size < 0
                    or not _record_hash_matches(payload, digest)
                    or len(payload) != size
                ):
                    raise _WheelMemberValidationError("record_mismatch")
                pending.append((name, payload))
    except _WheelMemberValidationError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, KeyError):
        raise IntegrationInstallError() from None
    try:
        _require_private_dir(destination, None, False)
        for name, _ in pending:
            target = destination / Path(*name.split("/"))
            parent = target.parent
            if not parent.exists():
                ensure_private_directory(parent, label="integration wheel directory")
            _require_private_dir(parent, None, False)
            if target.exists() or target.is_symlink():
                raise _WheelMemberValidationError("duplicate_member")
        for name, payload in pending:
            target = destination / Path(*name.split("/"))
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target, flags, 0o600)
            try:
                handle = os.fdopen(fd, "wb")
                fd = -1
                with handle:
                    handle.write(payload)
            finally:
                if fd >= 0:
                    os.close(fd)
            target.chmod(0o600)
    except _WheelMemberValidationError:
        raise
    except (OSError, ValueError):
        raise IntegrationInstallError() from None


def _wheel_details(wheel_path: Path) -> tuple[dict[str, bytes], dict[str, tuple[str, int]]]:
    try:
        wheel_payload = _read_nofollow(wheel_path)
        with zipfile.ZipFile(io.BytesIO(wheel_payload), "r") as archive:
            infos = _bounded_wheel_infos(archive)
            names = tuple(info.filename for info in infos)
            if len(names) != len(set(names)):
                raise _WheelMemberValidationError("duplicate_member")
            expected_package = {f"codex_usage/{name}" for name in SOURCE_MODULES}
            expected_dist = {f"{DIST_INFO_PREFIX}/{name}" for name in DIST_INFO_FILES}
            if set(names) != expected_package | expected_dist:
                _fail()
            record_payload = _read_bounded_wheel_member(
                archive,
                archive.getinfo(f"{DIST_INFO_PREFIX}/RECORD"),
            )
            record_rows = _parse_record(record_payload)
            if set(record_rows) != set(names):
                _fail()
            package = {
                name: _read_bounded_wheel_member(archive, archive.getinfo(name))
                for name in expected_package
            }
            metadata = _read_bounded_wheel_member(
                archive,
                archive.getinfo(f"{DIST_INFO_PREFIX}/METADATA"),
            ).decode("utf-8")
            wheel_metadata = _read_bounded_wheel_member(
                archive,
                archive.getinfo(f"{DIST_INFO_PREFIX}/WHEEL"),
            ).decode("utf-8")
            top_level = _read_bounded_wheel_member(
                archive,
                archive.getinfo(f"{DIST_INFO_PREFIX}/top_level.txt"),
            ).decode("utf-8")
            if (
                f"Name: {PRODUCER_DISTRIBUTION.replace('_', '-')}\n" not in metadata
                or f"Version: {RELEASE_VERSION}\n" not in metadata
                or "Wheel-Version:" not in wheel_metadata
                or top_level.strip() != "codex_usage"
            ):
                _fail()
            for name in names:
                payload = _read_bounded_wheel_member(archive, archive.getinfo(name))
                digest, size = record_rows[name]
                if name.endswith("/RECORD"):
                    if digest or size != -1:
                        _fail()
                elif not digest or size != len(payload) or _record_hash(payload) != digest:
                    _fail()
            _validate_runtime_import_closure(package)
            return package, record_rows
    except _WheelMemberValidationError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, KeyError):
        _fail()


def _postwalk_release(root: Path) -> set[str]:
    try:
        root_stat = root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            _fail()
        entries_seen = 1
        if entries_seen > MAX_RELEASE_TREE_ENTRIES:
            _fail()
        files: set[str] = set()
        pending = [root]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                child_names = []
                for entry in entries:
                    if entries_seen >= MAX_RELEASE_TREE_ENTRIES:
                        _fail()
                    entries_seen += 1
                    child_names.append(entry.name)
            for name in child_names:
                path = directory / name
                item = path.lstat()
                if stat.S_ISLNK(item.st_mode) or not (
                    stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode)
                ):
                    _fail()
                if stat.S_ISREG(item.st_mode) and item.st_nlink != 1:
                    _fail()
                if path.name == "__pycache__" or path.suffix == ".pyc":
                    _fail()
                if stat.S_ISDIR(item.st_mode):
                    pending.append(path)
                else:
                    files.add(path.relative_to(root).as_posix())
        return files
    except IntegrationInstallError:
        raise
    except (OSError, ValueError):
        _fail()


def _remove_activation_files(venv_root: Path) -> None:
    bin_dir = venv_root / "bin"
    for path in bin_dir.iterdir():
        if path.name.startswith("activate") or path.name == "Activate.ps1" or re.fullmatch(
            r"python3(?:\.\d+)?", path.name
        ):
            if path.is_symlink() or path.is_file():
                path.unlink()
    lib64 = venv_root / "lib64"
    if lib64.is_symlink():
        lib64.unlink()


def _write_exclusive(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    parent_identity: _DirectoryIdentity | None = None,
) -> _FileIdentity:
    _no_symlink_ancestors(path.parent)
    if path.exists() or path.is_symlink():
        _fail()
    if parent_identity is None:
        parent_identity = _directory_identity(path.parent)
    elif _directory_identity(path.parent) != parent_identity:
        _fail()
    fd = -1
    provisional: _ProvisionalIdentity | None = None
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        provisional = _provisional_fd_identity(fd)
        if _directory_identity(path.parent) != parent_identity:
            _fail()
        if (
            _provisional_rebased(
                path,
                provisional,
                parent_identity,
                directory=False,
            )
            is None
        ):
            _fail()
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                _fail()
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        path.chmod(mode)
        final_provisional = _provisional_rebased(
            path,
            provisional,
            parent_identity,
            directory=False,
        )
        if final_provisional is None:
            _fail()
        final = _file_identity_for_mode(path, mode)
        return final
    except IntegrationInstallError:
        if provisional is not None:
            _cleanup_provisional_after_failure(
                path,
                provisional,
                parent_identity,
                directory=False,
            )
        raise
    except (OSError, ValueError):
        if provisional is not None:
            _cleanup_provisional_after_failure(
                path,
                provisional,
                parent_identity,
                directory=False,
            )
        _fail()
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _write_launcher(
    *,
    path: Path,
    final_release_dir: Path,
    data_home: Path,
    state_home: Path,
) -> None:
    interpreter = final_release_dir / "venv" / "bin" / "python"
    quoted_data = _shell_single_quote(str(data_home))
    quoted_state = _shell_single_quote(str(state_home))
    quoted_interpreter = _shell_single_quote(str(interpreter))
    payload = (
        "#!/bin/sh\n"
        "unset PYTHONPATH PYTHONHOME VIRTUAL_ENV PIP_CONFIG_FILE PIP_INDEX_URL "
        "PIP_EXTRA_INDEX_URL PIP_TRUSTED_HOST OPENAI_API_KEY GEMINI_API_KEY "
        "HTTP_PROXY HTTPS_PROXY ALL_PROXY\n"
        "export PATH='/usr/bin:/bin'\n"
        "export LC_ALL='C.UTF-8'\n"
        "export LANG='C.UTF-8'\n"
        "export TZ=UTC\n"
        f"exec /usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 LANG=C.UTF-8 "
        f"TZ=UTC XDG_DATA_HOME={quoted_data} XDG_STATE_HOME={quoted_state} "
        f"{quoted_interpreter} -B -I -m codex_usage.integration_entrypoint \"$@\"\n"
    ).encode()
    _write_exclusive(path, payload, mode=0o700)


def _manifest(
    *,
    state_home: Path,
    data_home: Path,
    release_dir: Path,
    launcher_path: Path,
    entrypoint_path: Path,
    wheel_path: Path,
    record_path: Path,
    source_digest: str,
    entrypoint_sha256: str,
    wheel_sha256: str,
    record_sha256: str,
    launcher_sha256: str,
    release_tree_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "version": RELEASE_VERSION,
        "release_id": release_dir.name,
        "source_manifest_sha256": source_digest,
        "state_home": str(state_home),
        "data_home": str(data_home),
        "release_dir": str(release_dir),
        "launcher_path": str(launcher_path),
        "entrypoint_path": str(entrypoint_path),
        "wheel_path": str(wheel_path),
        "record_path": str(record_path),
        "entrypoint_sha256": entrypoint_sha256,
        "wheel_sha256": wheel_sha256,
        "record_sha256": record_sha256,
        "launcher_sha256": launcher_sha256,
        "release_tree_sha256": release_tree_sha256,
    }


def _manifest_text(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"


def _copy_source_into_project(source_root: Path, build_root: Path) -> None:
    for relative_text in SOURCE_MANIFEST_FILES:
        _copy_regular(source_root / relative_text, build_root / relative_text)
    pyproject = build_root / "pyproject.toml"
    pyproject.unlink()
    _write_exclusive(pyproject, _GENERATED_PYPROJECT.encode("utf-8"), mode=0o600)


def _install_release(
    *,
    source_root: Path,
    state_home: Path,
    data_home: Path,
    python_executable: Path,
    temporary_root: Path,
) -> ActiveRelease:
    source_root = _absolute(source_root)
    state_home = _absolute(state_home)
    data_home = _absolute(data_home)
    python_executable = _resolve_python_executable(python_executable)
    temporary_root = _absolute(temporary_root)
    _require_private_dir(source_root, None, False)
    _require_private_dir(state_home, None, False)
    _require_private_dir(data_home, None, False)
    temporary_identity = _require_private_dir(temporary_root, None, False)
    pyproject = _read_nofollow(source_root / "pyproject.toml").decode("utf-8")
    init_text = _read_nofollow(source_root / "src/codex_usage/__init__.py").decode("utf-8")
    if 'version = "0.6.532"' not in pyproject or '__version__ = "0.6.532"' not in init_text:
        _fail()
    source_manifest = _rehash_source_manifest(source_root)
    source_manifest_digest = _source_digest(source_manifest)
    release_id = f"{RELEASE_VERSION}-{source_manifest_digest[:16]}"

    app_identity, integration_identity = _bootstrap_integration_dir(state_home)
    integration = state_home / "codex-usage" / "integration"
    environment = _sanitized_build_environment()
    staging: Path | None = None
    staging_identity: _DirectoryIdentity | None = None
    staging_parent_identity: _DirectoryIdentity | None = None
    build_root: Path | None = None
    wheel_root: Path | None = None
    build_identity: _DirectoryIdentity | None = None
    build_parent_identity: _DirectoryIdentity | None = None
    wheel_identity: _DirectoryIdentity | None = None
    wheel_parent_identity: _DirectoryIdentity | None = None
    candidate_path = temporary_root / f"candidate-{release_id}.json"
    candidate_identity: _FileIdentity | None = None
    candidate_parent_identity: _DirectoryIdentity | None = None
    final_release_dir: Path | None = None
    final_renamed = False
    try:
        with private_path_lock(
            integration / RELEASE_LOCK_STEM,
            timeout_seconds=0,
            label="integration producer lock",
        ):
            _revalidate_bootstrap(state_home, app_identity, integration_identity)
            _require_private_dir(temporary_root, temporary_identity, False)
            releases = integration / "releases"
            releases_identity = _require_private_dir(releases, None, True)
            _revalidate_bootstrap(state_home, app_identity, integration_identity)
            _require_private_dir(temporary_root, temporary_identity, False)
            _require_private_dir(releases, releases_identity, False)
            final_release_dir = releases / release_id
            if _rehash_source_manifest(source_root) != source_manifest:
                _fail()
            if final_release_dir.exists() or final_release_dir.is_symlink():
                _fail()
            token = secrets.token_hex(8)
            staging = releases / f".{release_id}.staging-{token}"
            if staging.exists() or staging.is_symlink():
                _fail()
            staging_identity = _create_private_directory(staging, releases_identity)
            staging_parent_identity = releases_identity
            build_root = temporary_root / f"producer-build-{token}"
            wheel_root = temporary_root / f"producer-wheel-{token}"
            build_identity = _create_private_directory(build_root, temporary_identity)
            build_parent_identity = temporary_identity
            wheel_identity = _create_private_directory(wheel_root, temporary_identity)
            wheel_parent_identity = temporary_identity
            _require_private_dir(temporary_root, temporary_identity, False)
            _require_private_dir(staging, staging_identity, False)
            _require_private_dir(build_root, build_identity, False)
            _require_private_dir(wheel_root, wheel_identity, False)
            _copy_source_into_project(source_root, build_root)
            _require_private_dir(build_root, build_identity, False)
            _require_private_dir(temporary_root, temporary_identity, False)
            wheel_path = _build_verified_wheel(
                python_executable=python_executable,
                environment=environment,
                build_root=build_root,
                wheel_dir=wheel_root,
            )
            _require_private_dir(temporary_root, temporary_identity, False)
            _require_private_dir(build_root, build_identity, False)
            _require_private_dir(wheel_root, wheel_identity, False)
            if _rehash_source_manifest(source_root) != source_manifest:
                _fail()
            package, record_rows = _wheel_details(wheel_path)
            _ = package
            staged_wheel = staging / "producer.whl"
            _require_private_dir(staging, staging_identity, False)
            _copy_regular(wheel_path, staged_wheel)
            _require_private_dir(staging, staging_identity, False)
            venv_root = staging / "venv"
            venv.EnvBuilder(
                system_site_packages=False,
                clear=False,
                symlinks=False,
                with_pip=False,
            ).create(venv_root)
            venv_root.chmod(0o700)
            _require_private_dir(staging, staging_identity, False)
            _remove_activation_files(venv_root)
            site_packages = next(venv_root.glob("lib/python*/site-packages"), None)
            if site_packages is None:
                _fail()
            site_packages.chmod(0o700)
            _safe_extract_wheel(
                wheel_path=staged_wheel,
                destination=site_packages,
                record_rows=record_rows,
            )
            _require_private_dir(staging, staging_identity, False)
            entrypoint_path = site_packages / "codex_usage" / "integration_entrypoint.py"
            record_path = site_packages / DIST_INFO_PREFIX / "RECORD"
            launcher_path = venv_root / "bin" / "codex-usage"
            _write_launcher(
                path=launcher_path,
                final_release_dir=final_release_dir,
                data_home=data_home,
                state_home=state_home,
            )
            _postwalk_release(staging)
            _require_private_dir(staging, staging_identity, False)
            if _rehash_source_manifest(source_root) != source_manifest:
                _fail()
            entrypoint_payload = _read_nofollow(entrypoint_path)
            wheel_payload = _read_nofollow(staged_wheel)
            record_payload = _read_nofollow(record_path)
            launcher_payload = _read_nofollow(launcher_path)
            _require_private_dir(staging, staging_identity, False)
            tree_hash = _release_tree_sha256(release_dir=staging)
            entrypoint_hash = hashlib.sha256(entrypoint_payload).hexdigest()
            wheel_hash = hashlib.sha256(wheel_payload).hexdigest()
            record_hash = hashlib.sha256(record_payload).hexdigest()
            launcher_hash = hashlib.sha256(launcher_payload).hexdigest()
            entrypoint_relative = entrypoint_path.relative_to(staging)
            record_relative = record_path.relative_to(staging)
            launcher_relative = launcher_path.relative_to(staging)
            final_entrypoint_path = final_release_dir / entrypoint_relative
            final_record_path = final_release_dir / record_relative
            final_launcher_path = final_release_dir / launcher_relative
            final_wheel_path = final_release_dir / "producer.whl"
            candidate = _manifest(
                state_home=state_home,
                data_home=data_home,
                release_dir=final_release_dir,
                launcher_path=final_launcher_path,
                entrypoint_path=final_entrypoint_path,
                wheel_path=final_wheel_path,
                record_path=final_record_path,
                source_digest=source_manifest_digest,
                entrypoint_sha256=entrypoint_hash,
                wheel_sha256=wheel_hash,
                record_sha256=record_hash,
                launcher_sha256=launcher_hash,
                release_tree_sha256=tree_hash,
            )
            _revalidate_bootstrap(state_home, app_identity, integration_identity)
            _require_private_dir(temporary_root, temporary_identity, False)
            _require_private_dir(staging, staging_identity, False)
            candidate_identity = _write_exclusive(
                candidate_path,
                _manifest_text(candidate).encode("utf-8"),
                mode=0o600,
                parent_identity=temporary_identity,
            )
            candidate_parent_identity = temporary_identity
            _revalidate_bootstrap(state_home, app_identity, integration_identity)
            _require_private_dir(releases, releases_identity, False)
            _require_private_dir(staging, staging_identity, False)
            if final_release_dir.exists() or final_release_dir.is_symlink():
                _fail()
            try:
                candidate_at_seam = _read_manifest(candidate_path)
            except IntegrationAttestationUnavailable:
                _fail()
            if candidate_at_seam != candidate:
                _fail()
            staging.rename(final_release_dir)
            final_renamed = True
            _require_private_dir(final_release_dir, staging_identity, False)
            _revalidate_bootstrap(state_home, app_identity, integration_identity)
            actual_tree_hash = _release_tree_sha256(release_dir=final_release_dir)
            if actual_tree_hash != tree_hash:
                _fail()
            _revalidate_bootstrap(state_home, app_identity, integration_identity)
            verified = _verify_manifest(
                manifest_path=candidate_path,
                state_home=state_home,
                data_home=data_home,
                expected_entrypoint_path=final_entrypoint_path,
            )
            active_path = integration / ACTIVE_NAME
            if active_path.exists() or active_path.is_symlink():
                active_text, active_stat = read_private_text(
                    active_path,
                    regular_label="active manifest",
                    read_label="active manifest",
                    max_bytes=128 * 1024,
                )
                if active_stat.st_nlink != 1 or stat.S_IMODE(active_stat.st_mode) != 0o600:
                    _fail()
                _revalidate_bootstrap(state_home, app_identity, integration_identity)
                _verify_manifest(
                    manifest_path=active_path,
                    state_home=state_home,
                    data_home=data_home,
                    expected_entrypoint_path=None,
                )
                _revalidate_bootstrap(state_home, app_identity, integration_identity)
                write_private_text(
                    integration / PREVIOUS_NAME,
                    active_text,
                    label="previous integration manifest",
                    mode=0o600,
                )
            _revalidate_bootstrap(state_home, app_identity, integration_identity)
            write_private_text(
                active_path,
                _manifest_text(candidate),
                label="active integration manifest",
                mode=0o600,
            )
            _revalidate_bootstrap(state_home, app_identity, integration_identity)
            _verify_manifest(
                manifest_path=active_path,
                state_home=state_home,
                data_home=data_home,
                expected_entrypoint_path=None,
            )
            return verified
    except IntegrationInstallError:
        raise
    except (IntegrationAttestationUnavailable, OSError, ValueError, RuntimeError):
        _fail()
    finally:
        active_error = sys.exc_info()[1]
        cleanup_failed = False
        if candidate_identity is not None and candidate_parent_identity is not None:
            if not _cleanup_owned_file(
                candidate_path,
                candidate_identity,
                candidate_parent_identity,
            ):
                cleanup_failed = True
        if (
            build_root is not None
            and build_identity is not None
            and build_parent_identity is not None
        ):
            if not _cleanup_owned_directory(
                build_root,
                build_identity,
                build_parent_identity,
            ):
                cleanup_failed = True
        if (
            wheel_root is not None
            and wheel_identity is not None
            and wheel_parent_identity is not None
        ):
            if not _cleanup_owned_directory(
                wheel_root,
                wheel_identity,
                wheel_parent_identity,
            ):
                cleanup_failed = True
        if (
            staging is not None
            and staging_identity is not None
            and staging_parent_identity is not None
            and not final_renamed
            and not _cleanup_owned_directory(
                staging,
                staging_identity,
                staging_parent_identity,
            )
        ):
            cleanup_failed = True
        if cleanup_failed:
            if active_error is not None:
                raise IntegrationCleanupError() from active_error
            raise IntegrationCleanupError()


def install_release(
    *,
    source_root: Path,
    state_home: Path,
    data_home: Path,
    python_executable: Path,
    temporary_root: Path,
) -> ActiveRelease:
    try:
        return _install_release(
            source_root=source_root,
            state_home=state_home,
            data_home=data_home,
            python_executable=python_executable,
            temporary_root=temporary_root,
        )
    except IntegrationInstallError:
        raise
    except Exception:
        raise IntegrationInstallError() from None


def rollback_active_release(*, state_home: Path, data_home: Path) -> ActiveRelease:
    try:
        state_home = _absolute(state_home)
        data_home = _absolute(data_home)
        _require_private_dir(state_home, None, False)
        _require_private_dir(data_home, None, False)
        app_identity, integration_identity = _bootstrap_integration_dir(state_home)
        integration = state_home / "codex-usage" / "integration"
        with private_path_lock(
            integration / RELEASE_LOCK_STEM,
            timeout_seconds=0,
            label="integration producer lock",
        ):
            _revalidate_bootstrap(state_home, app_identity, integration_identity)
            previous = integration / PREVIOUS_NAME
            previous_text, previous_stat = read_private_text(
                previous,
                regular_label="previous integration manifest",
                read_label="previous integration manifest",
                max_bytes=128 * 1024,
            )
            if previous_stat.st_nlink != 1 or stat.S_IMODE(previous_stat.st_mode) != 0o600:
                _fail()
            _revalidate_bootstrap(state_home, app_identity, integration_identity)
            release = _verify_manifest(
                manifest_path=previous,
                state_home=state_home,
                data_home=data_home,
                expected_entrypoint_path=None,
            )
            _revalidate_bootstrap(state_home, app_identity, integration_identity)
            write_private_text(
                integration / ACTIVE_NAME,
                previous_text,
                label="active integration manifest",
                mode=0o600,
            )
            _revalidate_bootstrap(state_home, app_identity, integration_identity)
            _verify_manifest(
                manifest_path=integration / ACTIVE_NAME,
                state_home=state_home,
                data_home=data_home,
                expected_entrypoint_path=None,
            )
            return release
    except IntegrationInstallError:
        raise
    except Exception:
        raise IntegrationInstallError() from None
