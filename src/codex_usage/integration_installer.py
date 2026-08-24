from __future__ import annotations

import ast
import base64
import csv
import ctypes
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
from typing import IO, NoReturn, cast

from .integration_attestation import (
    MAX_ATTESTATION_FILE_BYTES,
    MAX_RELEASE_TREE_ENTRIES,
    ActiveRelease,
    IntegrationAttestationUnavailable,
    _read_manifest,
    _release_tree_sha256,
    _verify_legacy_manifest_for_upgrade,
    _verify_manifest,
)
from .private_io import (
    ensure_private_directory,
    private_path_lock,
    read_private_text,
    write_private_text,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RELEASE_VERSION = "0.6.533"
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
DIST_INFO_PREFIX = "codex_usage_integration_producer-0.6.533.dist-info"
DIST_INFO_FILES = frozenset({"METADATA", "WHEEL", "RECORD", "top_level.txt"})
EXPECTED_WHEEL_NAME = "codex_usage_integration_producer-0.6.533-py3-none-any.whl"
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
version = "0.6.533"
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


def _fail() -> NoReturn:
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
        if part == "..":
            current = current.parent
            continue
        current /= part
        try:
            item = current.lstat()
        except FileNotFoundError:
            continue
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
    return _remove_owned_entry(
        path,
        identity,
        parent_identity,
        directory=directory,
    )


def _remove_owned_entry(
    path: Path,
    identity: _DirectoryIdentity | _FileIdentity | _ProvisionalIdentity,
    parent_identity: _DirectoryIdentity,
    *,
    directory: bool,
    recursive: bool = False,
) -> bool:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    parent_fd = -1
    try:
        _no_symlink_ancestors(path.parent)
        parent_fd = os.open(path.parent, flags)
        parent_item = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_item.st_mode)
            or parent_item.st_uid != os.getuid()
            or _DirectoryIdentity(
                parent_item.st_dev,
                parent_item.st_ino,
                stat.S_IMODE(parent_item.st_mode),
            )
            != parent_identity
        ):
            return False
        item = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if directory:
            if not stat.S_ISDIR(item.st_mode) or item.st_uid != os.getuid():
                return False
        elif not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
            return False
        if isinstance(identity, _ProvisionalIdentity):
            if _provisional_from_stat(item) != identity:
                return False
        elif isinstance(identity, _DirectoryIdentity):
            if (
                _DirectoryIdentity(
                    item.st_dev,
                    item.st_ino,
                    stat.S_IMODE(item.st_mode),
                )
                != identity
            ):
                return False
        elif (
            item.st_uid != os.getuid()
            or _FileIdentity(item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode))
            != identity
        ):
            return False
        try:
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return False
        if (
            _provisional_from_stat(current) != _provisional_from_stat(item)
            or (not directory and current.st_nlink != item.st_nlink)
        ):
            return False
        if directory:
            if recursive:
                shutil.rmtree(path.name, dir_fd=parent_fd)
            else:
                os.rmdir(path.name, dir_fd=parent_fd)
        else:
            os.unlink(path.name, dir_fd=parent_fd)
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    except (OSError, ValueError):
        return False
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _rename_noreplace(source_name: str, target_name: str, parent_fd: int) -> None:
    if sys.platform != "linux":
        _fail()
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError):
        _fail()
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            parent_fd,
            os.fsencode(source_name),
            parent_fd,
            os.fsencode(target_name),
            1,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _rename_owned_directory(
    source: Path,
    target: Path,
    parent_identity: _DirectoryIdentity,
    source_identity: _DirectoryIdentity,
) -> None:
    if source.parent != target.parent:
        _fail()
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    parent_fd = -1
    try:
        _no_symlink_ancestors(source.parent)
        parent_fd = os.open(source.parent, flags)
        parent_item = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_item.st_mode)
            or parent_item.st_uid != os.getuid()
            or _DirectoryIdentity(
                parent_item.st_dev,
                parent_item.st_ino,
                stat.S_IMODE(parent_item.st_mode),
            )
            != parent_identity
        ):
            _fail()
        source_item = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(source_item.st_mode)
            or source_item.st_uid != os.getuid()
            or _DirectoryIdentity(
                source_item.st_dev,
                source_item.st_ino,
                stat.S_IMODE(source_item.st_mode),
            )
            != source_identity
        ):
            _fail()
        try:
            os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _fail()
        current_source = os.stat(
            source.name, dir_fd=parent_fd, follow_symlinks=False
        )
        if _provisional_from_stat(current_source) != _provisional_from_stat(
            source_item
        ):
            _fail()
        _rename_noreplace(source.name, target.name, parent_fd)
        final_item = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(final_item.st_mode)
            or final_item.st_uid != os.getuid()
            or _DirectoryIdentity(
                final_item.st_dev,
                final_item.st_ino,
                stat.S_IMODE(final_item.st_mode),
            )
            != source_identity
        ):
            _fail()
    except IntegrationInstallError:
        raise
    except (OSError, ValueError):
        _fail()
    finally:
        if parent_fd >= 0:  # pragma: no branch - sentinel remains -1 on unwind
            os.close(parent_fd)


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
    provisional: _ProvisionalIdentity | None = None
    parent_fd = -1
    child_fd = -1
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        parent_fd = os.open(path.parent, flags)
        parent_item = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_item.st_mode)
            or parent_item.st_uid != os.getuid()
            or _DirectoryIdentity(
                parent_item.st_dev,
                parent_item.st_ino,
                stat.S_IMODE(parent_item.st_mode),
            )
            != parent_identity
        ):
            _fail()
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _fail()
        os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
        child_fd = os.open(path.name, flags, dir_fd=parent_fd)
        child_item = os.fstat(child_fd)
        provisional = _provisional_from_stat(child_item)
        if not stat.S_ISDIR(child_item.st_mode) or child_item.st_uid != os.getuid():
            _fail()
        os.fchmod(child_fd, 0o700)
        final_item = os.fstat(child_fd)
        final = _DirectoryIdentity(
            final_item.st_dev,
            final_item.st_ino,
            stat.S_IMODE(final_item.st_mode),
        )
        if (
            not stat.S_ISDIR(final_item.st_mode)
            or final_item.st_uid != os.getuid()
            or final.permissions != 0o700
            or final.device != provisional.device
            or final.inode != provisional.inode
        ):
            _fail()
        parent_final = os.fstat(parent_fd)
        if _DirectoryIdentity(
            parent_final.st_dev,
            parent_final.st_ino,
            stat.S_IMODE(parent_final.st_mode),
        ) != parent_identity:
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
    finally:
        if child_fd >= 0:
            os.close(child_fd)
        if parent_fd >= 0:  # pragma: no branch - sentinel remains -1 on unwind
            os.close(parent_fd)


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
    return _remove_owned_entry(
        path,
        identity,
        parent_identity,
        directory=False,
    )


def _cleanup_owned_directory(
    path: Path,
    identity: _DirectoryIdentity,
    parent_identity: _DirectoryIdentity,
) -> bool:
    if not _owned_directory_matches(path, identity, parent_identity):
        return False
    return _remove_owned_entry(
        path,
        identity,
        parent_identity,
        directory=True,
        recursive=True,
    )


def _require_private_dir(
    path: Path,
    expected: _DirectoryIdentity | None,
    create: bool,
    *,
    parent_identity: _DirectoryIdentity | None = None,
) -> _DirectoryIdentity:
    path = _absolute(path)
    _no_symlink_ancestors(path.parent)
    if create:
        parent_fd = -1
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if parent_identity is None:
                parent_identity = _directory_identity(path.parent)
            parent_fd = os.open(path.parent, flags)
            parent_item = os.fstat(parent_fd)
            if (
                not stat.S_ISDIR(parent_item.st_mode)
                or parent_item.st_uid != os.getuid()
                or _DirectoryIdentity(
                    parent_item.st_dev,
                    parent_item.st_ino,
                    stat.S_IMODE(parent_item.st_mode),
                )
                != parent_identity
            ):
                _fail()
            try:
                os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            parent_final = os.fstat(parent_fd)
            if _DirectoryIdentity(
                parent_final.st_dev,
                parent_final.st_ino,
                stat.S_IMODE(parent_final.st_mode),
            ) != parent_identity:
                _fail()
            if _directory_identity(path.parent) != parent_identity:
                _fail()
        except (OSError, ValueError):
            _fail()
        finally:
            if parent_fd >= 0:  # pragma: no branch - sentinel remains -1 on unwind
                os.close(parent_fd)
    identity = _identity(path)
    if expected is not None and identity != expected:
        _fail()
    return identity


def _bootstrap_integration_dir(
    state_home: Path,
) -> tuple[_DirectoryIdentity, _DirectoryIdentity]:
    state_home = _absolute(state_home)
    state_identity = _require_private_dir(state_home, None, False)
    app_dir = state_home / "codex-usage"
    integration_dir = app_dir / "integration"
    app_identity = _require_private_dir(
        app_dir,
        None,
        True,
        parent_identity=state_identity,
    )
    integration_identity = _require_private_dir(
        integration_dir,
        None,
        True,
        parent_identity=app_identity,
    )
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


def _restore_active_manifest(
    *,
    active_path: Path,
    active_text: str,
    expected_published_identity: _ProvisionalIdentity,
    state_home: Path,
    app_identity: _DirectoryIdentity,
    integration_identity: _DirectoryIdentity,
) -> None:
    _revalidate_bootstrap(state_home, app_identity, integration_identity)
    if not _provisional_matches(
        active_path,
        expected_published_identity,
        integration_identity,
        directory=False,
    ):
        _fail()
    write_private_text(
        active_path,
        active_text,
        label="active integration manifest",
        mode=0o600,
    )
    _revalidate_bootstrap(state_home, app_identity, integration_identity)
    restored_text, restored_stat = read_private_text(
        active_path,
        regular_label="active manifest",
        read_label="active manifest",
        max_bytes=128 * 1024,
    )
    if (
        restored_text != active_text
        or restored_stat.st_nlink != 1
        or stat.S_IMODE(restored_stat.st_mode) != 0o600
    ):
        _fail()


def _published_active_identity(
    *,
    active_path: Path,
    published_text: str,
    integration_identity: _DirectoryIdentity,
) -> _ProvisionalIdentity:
    strict_identity = _file_identity(active_path)
    current_text, current_stat = read_private_text(
        active_path,
        regular_label="active manifest",
        read_label="active manifest",
        max_bytes=128 * 1024,
    )
    identity = _provisional_from_stat(current_stat)
    if (
        current_text != published_text
        or identity.device != strict_identity.device
        or identity.inode != strict_identity.inode
        or identity.permissions != strict_identity.permissions
        or not _provisional_matches(
            active_path,
            identity,
            integration_identity,
            directory=False,
        )
    ):
        _fail()
    return identity


def _recover_uncaptured_active_identity(
    *,
    active_path: Path,
    published_text: str,
    state_home: Path,
    app_identity: _DirectoryIdentity,
    integration_identity: _DirectoryIdentity,
) -> _ProvisionalIdentity:
    _revalidate_bootstrap(state_home, app_identity, integration_identity)
    current_text, current_stat = read_private_text(
        active_path,
        regular_label="active manifest",
        read_label="active manifest",
        max_bytes=128 * 1024,
    )
    identity = _provisional_from_stat(current_stat)
    if current_text != published_text or not _provisional_matches(
        active_path,
        identity,
        integration_identity,
        directory=False,
    ):
        _fail()
    return identity


def _copy_regular(
    source: Path,
    target: Path,
    *,
    mode: int = 0o600,
    source_parent_identity: _DirectoryIdentity | None = None,
    source_identity: _FileIdentity | None = None,
) -> _FileIdentity:
    fd = -1
    parent_fd = -1
    target_parent_identity: _DirectoryIdentity | None = None
    provisional: _ProvisionalIdentity | None = None
    try:
        _no_symlink_ancestors(source.parent)
        source_stat = source.lstat()
        if (
            not stat.S_ISREG(source_stat.st_mode)
            or source_stat.st_nlink != 1
            or (
                source_identity is not None
                and _FileIdentity(
                    source_stat.st_dev,
                    source_stat.st_ino,
                    stat.S_IMODE(source_stat.st_mode),
                )
                != source_identity
            )
        ):
            _fail()
        expected_source_identity = source_identity or _FileIdentity(
            source_stat.st_dev,
            source_stat.st_ino,
            stat.S_IMODE(source_stat.st_mode),
        )
        ensure_private_directory(target.parent, label="integration target directory")
        target_parent_identity = _directory_identity(target.parent)
        parent_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            parent_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            parent_flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            parent_flags |= os.O_CLOEXEC
        parent_fd = os.open(target.parent, parent_flags)
        parent_item = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_item.st_mode)
            or parent_item.st_uid != os.getuid()
            or _DirectoryIdentity(
                parent_item.st_dev,
                parent_item.st_ino,
                stat.S_IMODE(parent_item.st_mode),
            )
            != target_parent_identity
        ):
            _fail()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target.name, flags, mode, dir_fd=parent_fd)
        provisional = _provisional_fd_identity(fd)
        destination = os.fdopen(fd, "wb")
        fd = -1
        with destination:
            destination.write(
                _read_nofollow(
                    source,
                    expected_parent_identity=source_parent_identity,
                    expected_file_identity=expected_source_identity,
                )
            )
            os.fchmod(destination.fileno(), mode)
            item = os.fstat(destination.fileno())
            if (
                not stat.S_ISREG(item.st_mode)
                or item.st_nlink != 1
                or item.st_uid != os.getuid()
                or stat.S_IMODE(item.st_mode) != mode
            ):
                _fail()
            return _FileIdentity(item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode))
    except IntegrationInstallError:
        if provisional is not None and target_parent_identity is not None:
            _cleanup_provisional_after_failure(
                target,
                provisional,
                target_parent_identity,
                directory=False,
            )
        raise
    except (OSError, ValueError):
        if provisional is not None and target_parent_identity is not None:
            _cleanup_provisional_after_failure(
                target,
                provisional,
                target_parent_identity,
                directory=False,
            )
        _fail()
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:  # pragma: no branch - sentinel remains -1 on unwind
            os.close(parent_fd)


def _read_nofollow(
    path: Path,
    *,
    expected_parent_identity: _DirectoryIdentity | None = None,
    expected_file_identity: _FileIdentity | None = None,
) -> bytes:
    fd = -1
    parent_fd = -1
    try:
        _no_symlink_ancestors(path.parent)
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if expected_parent_identity is None and expected_file_identity is None:
            fd = os.open(path, file_flags)
        else:
            parent_flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                parent_flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                parent_flags |= os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                parent_flags |= os.O_CLOEXEC
            parent_fd = os.open(path.parent, parent_flags)
            parent_item = os.fstat(parent_fd)
            if (
                not stat.S_ISDIR(parent_item.st_mode)
                or parent_item.st_uid != os.getuid()
                or (
                    expected_parent_identity is not None
                    and _DirectoryIdentity(
                        parent_item.st_dev,
                        parent_item.st_ino,
                        stat.S_IMODE(parent_item.st_mode),
                    )
                    != expected_parent_identity
                )
            ):
                _fail()
            fd = os.open(path.name, file_flags, dir_fd=parent_fd)
        item = os.fstat(fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or item.st_uid != os.getuid()
        ):
            _fail()
        if expected_file_identity is not None and (
            _FileIdentity(item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode))
            != expected_file_identity
        ):
            _fail()
        if item.st_size > MAX_INSTALL_FILE_BYTES:
            _fail()
        with os.fdopen(fd, "rb") as source:
            fd = -1
            payload = source.read(MAX_INSTALL_FILE_BYTES + 1)
            if (
                len(payload) > MAX_INSTALL_FILE_BYTES
                or len(payload) != item.st_size
            ):
                _fail()
            return payload
    except IntegrationInstallError:
        raise
    except (OSError, ValueError):
        _fail()
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)


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
    try:
        final_item = resolved.lstat()
    except (OSError, ValueError):
        _fail()
    if (
        _provisional_from_stat(final_item) != _provisional_from_stat(item)
        or final_item.st_nlink != item.st_nlink
    ):
        _fail()
    return resolved


def _temporary_source_copy(destination_root: Path) -> Path:
    destination_root = _absolute(destination_root)
    destination_root_identity = _require_private_dir(destination_root, None, False)
    destination = destination_root / "source"
    _require_private_dir(
        destination,
        None,
        True,
        parent_identity=destination_root_identity,
    )
    for relative_text in SOURCE_MANIFEST_FILES:
        relative = Path(relative_text)
        source = PROJECT_ROOT / relative
        if not source.is_file() or source.is_symlink():
            _fail()
        _copy_regular(source, destination / relative)
    files = _postwalk_release(
        destination,
        root_identity=_directory_identity(destination),
    )
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
    pid = getattr(process, "pid", None)
    if type(pid) is int and pid > 0:
        try:
            os.killpg(pid, signal.SIGKILL)
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
    if type(process_group_id) is not int or process_group_id <= 0:
        return
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
                stream = cast(IO[bytes], key.fileobj)
                chunk = os.read(
                    stream.fileno(),
                    min(8192, BUILDER_PREFLIGHT_MAX_OUTPUT_BYTES + 1 - len(output)),
                )
                if not chunk:
                    selector.unregister(stream)
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
    wheel_identity: _DirectoryIdentity | None = None,
) -> tuple[Path, _FileIdentity]:
    _require_offline_builder(
        python_executable=python_executable,
        environment=environment,
    )
    _require_private_dir(build_root, None, False)
    wheel_parent_identity = _require_private_dir(wheel_dir.parent, None, False)
    wheel_identity = _require_private_dir(
        wheel_dir,
        wheel_identity,
        True,
        parent_identity=wheel_parent_identity,
    )
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
    wheels: list[tuple[Path, _FileIdentity]] = []
    entries_seen = 0
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    wheel_fd = -1
    try:
        _no_symlink_ancestors(wheel_dir)
        wheel_fd = os.open(wheel_dir, flags)
        wheel_item = os.fstat(wheel_fd)
        if (
            not stat.S_ISDIR(wheel_item.st_mode)
            or wheel_item.st_uid != os.getuid()
            or _DirectoryIdentity(
                wheel_item.st_dev,
                wheel_item.st_ino,
                stat.S_IMODE(wheel_item.st_mode),
            )
            != wheel_identity
        ):
            _fail()
        with os.scandir(wheel_fd) as entries:
            for entry in entries:
                entries_seen += 1
                if entries_seen > MAX_RELEASE_TREE_ENTRIES:
                    _fail()
                item = entry.stat(follow_symlinks=False)
                if (
                    stat.S_ISREG(item.st_mode)
                    and item.st_nlink == 1
                    and entry.name.endswith(".whl")
                ):
                    wheels.append(
                        (
                            wheel_dir / entry.name,
                            _FileIdentity(
                                item.st_dev,
                                item.st_ino,
                                stat.S_IMODE(item.st_mode),
                            ),
                        )
                    )
    except IntegrationInstallError:
        raise
    except (OSError, ValueError):
        _fail()
    finally:
        if wheel_fd >= 0:  # pragma: no branch - sentinel remains -1 on unwind
            os.close(wheel_fd)
    wheels.sort(key=lambda candidate: candidate[0])
    if len(wheels) != 1 or wheels[0][0].name != EXPECTED_WHEEL_NAME:
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
        pid = getattr(process, "pid", None)
        if type(pid) is int and pid > 0:
            try:
                process_group_id = os.getpgid(pid)
            except OSError:
                process_group_id = pid
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
    if type(info.file_size) is not int or info.file_size < 0:
        _fail()
    if info.file_size > MAX_INSTALL_FILE_BYTES:
        _fail()
    with archive.open(info, "r") as source:
        payload = source.read(MAX_INSTALL_FILE_BYTES + 1)
        if (
            len(payload) > MAX_INSTALL_FILE_BYTES
            or len(payload) != info.file_size
            or source.read(1)
        ):
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
                try:
                    size = int(size_text)
                except (OverflowError, ValueError):
                    _fail()
                result[path_text] = (digest, size)
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
    wheel_parent_identity: _DirectoryIdentity | None = None,
    wheel_file_identity: _FileIdentity | None = None,
    destination_identity: _DirectoryIdentity | None = None,
) -> dict[str, tuple[_DirectoryIdentity, _FileIdentity]]:
    pending: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    try:
        wheel_payload = _read_nofollow(
            wheel_path,
            expected_parent_identity=wheel_parent_identity,
            expected_file_identity=wheel_file_identity,
        )
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
        if destination_identity is None:
            destination_identity = _directory_identity(destination)
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= os.O_CLOEXEC
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(
            os, "O_NOFOLLOW", 0
        )
        destination_fd = -1
        directory_identities: dict[str, _DirectoryIdentity] = {
            "": destination_identity,
        }
        extracted_identities: dict[str, tuple[_DirectoryIdentity, _FileIdentity]] = {}
        try:
            _no_symlink_ancestors(destination)
            destination_fd = os.open(destination, directory_flags)
            destination_item = os.fstat(destination_fd)
            if (
                not stat.S_ISDIR(destination_item.st_mode)
                or destination_item.st_uid != os.getuid()
                or _DirectoryIdentity(
                    destination_item.st_dev,
                    destination_item.st_ino,
                    stat.S_IMODE(destination_item.st_mode),
                )
                != destination_identity
            ):
                _fail()
            def open_parent(parts: tuple[str, ...], *, create: bool) -> int:
                parent_fd = os.dup(destination_fd)
                result_fd = -1
                relative = ""
                try:
                    for part in parts:
                        key = f"{relative}/{part}" if relative else part
                        expected = directory_identities.get(key)
                        try:
                            child_item = os.stat(
                                part,
                                dir_fd=parent_fd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            if not create or expected is not None:
                                _fail()
                            try:
                                os.mkdir(part, mode=0o700, dir_fd=parent_fd)
                            except FileExistsError:
                                _fail()
                            child_item = os.stat(
                                part,
                                dir_fd=parent_fd,
                                follow_symlinks=False,
                            )
                        current = _DirectoryIdentity(
                            child_item.st_dev,
                            child_item.st_ino,
                            stat.S_IMODE(child_item.st_mode),
                        )
                        if (
                            not stat.S_ISDIR(child_item.st_mode)
                            or child_item.st_uid != os.getuid()
                            or stat.S_IMODE(child_item.st_mode) != 0o700
                            or (expected is not None and current != expected)
                        ):
                            _fail()
                        child_fd = -1
                        try:
                            child_fd = os.open(
                                part,
                                directory_flags,
                                dir_fd=parent_fd,
                            )
                            opened_item = os.fstat(child_fd)
                            opened_identity = _DirectoryIdentity(
                                opened_item.st_dev,
                                opened_item.st_ino,
                                stat.S_IMODE(opened_item.st_mode),
                            )
                            if (
                                not stat.S_ISDIR(opened_item.st_mode)
                                or opened_item.st_uid != os.getuid()
                                or opened_identity != current
                            ):
                                _fail()
                            directory_identities[key] = current
                            old_parent_fd = parent_fd
                            parent_fd = child_fd
                            child_fd = -1
                        finally:
                            if child_fd >= 0:
                                os.close(child_fd)
                        os.close(old_parent_fd)
                        relative = key
                    result_fd = parent_fd
                    parent_fd = -1
                    return result_fd
                finally:
                    if parent_fd >= 0:
                        os.close(parent_fd)

            for name, _ in pending:
                target_parts = tuple(name.split("/"))
                parent_fd = open_parent(target_parts[:-1], create=True)
                try:
                    try:
                        os.stat(
                            target_parts[-1],
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        raise _WheelMemberValidationError("duplicate_member")
                finally:
                    os.close(parent_fd)

            for name, payload in pending:
                target_parts = tuple(name.split("/"))
                parent_fd = open_parent(target_parts[:-1], create=False)
                fd = -1
                try:
                    try:
                        fd = os.open(
                            target_parts[-1],
                            file_flags,
                            0o600,
                            dir_fd=parent_fd,
                        )
                    except FileExistsError:
                        raise _WheelMemberValidationError("duplicate_member") from None
                    with os.fdopen(fd, "wb") as handle:
                        fd = -1
                        handle.write(payload)
                        os.fchmod(handle.fileno(), 0o600)
                        item = os.fstat(handle.fileno())
                        if (
                            not stat.S_ISREG(item.st_mode)
                            or item.st_nlink != 1
                            or item.st_uid != os.getuid()
                            or stat.S_IMODE(item.st_mode) != 0o600
                        ):
                            _fail()
                        parent_name = "/".join(target_parts[:-1])
                        extracted_identities[name] = (
                            directory_identities[parent_name],
                            _FileIdentity(
                                item.st_dev,
                                item.st_ino,
                                stat.S_IMODE(item.st_mode),
                            ),
                        )
                finally:
                    if fd >= 0:
                        os.close(fd)
                    os.close(parent_fd)
        finally:
            if destination_fd >= 0:  # pragma: no branch - sentinel remains -1 on unwind
                os.close(destination_fd)
        return extracted_identities
    except _WheelMemberValidationError:
        raise
    except (OSError, ValueError):
        raise IntegrationInstallError() from None


def _wheel_details(
    wheel_path: Path,
    *,
    parent_identity: _DirectoryIdentity | None = None,
    file_identity: _FileIdentity | None = None,
) -> tuple[dict[str, bytes], dict[str, tuple[str, int]]]:
    try:
        wheel_payload = _read_nofollow(
            wheel_path,
            expected_parent_identity=parent_identity,
            expected_file_identity=file_identity,
        )
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


def _postwalk_release(
    root: Path,
    *,
    root_identity: _DirectoryIdentity | None = None,
) -> set[str]:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    root_fd = -1
    pending: list[tuple[int, str]] = []
    try:
        _no_symlink_ancestors(root)
        root_fd = os.open(root, flags)
        root_stat = os.fstat(root_fd)
        current_root_identity = _DirectoryIdentity(
            root_stat.st_dev,
            root_stat.st_ino,
            stat.S_IMODE(root_stat.st_mode),
        )
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or stat.S_ISLNK(root_stat.st_mode)
            or root_stat.st_uid != os.getuid()
            or (
                root_identity is not None
                and current_root_identity != root_identity
            )
        ):
            _fail()
        entries_seen = 1
        if entries_seen > MAX_RELEASE_TREE_ENTRIES:
            _fail()
        files: set[str] = set()
        pending.append((root_fd, ""))
        root_fd = -1
        while pending:
            directory_fd, relative_directory = pending.pop()
            try:
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        if entries_seen >= MAX_RELEASE_TREE_ENTRIES:
                            _fail()
                        entries_seen += 1
                        item = entry.stat(follow_symlinks=False)
                        name = entry.name
                        relative = (
                            f"{relative_directory}/{name}"
                            if relative_directory
                            else name
                        )
                        if stat.S_ISLNK(item.st_mode) or not (
                            stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode)
                        ):
                            _fail()
                        if item.st_uid != os.getuid():
                            _fail()
                        if stat.S_ISREG(item.st_mode) and item.st_nlink != 1:
                            _fail()
                        if name == "__pycache__" or Path(name).suffix == ".pyc":
                            _fail()
                        if stat.S_ISDIR(item.st_mode):
                            child_fd = os.open(name, flags, dir_fd=directory_fd)
                            child_stat = os.fstat(child_fd)
                            if (
                                not stat.S_ISDIR(child_stat.st_mode)
                                or child_stat.st_uid != os.getuid()
                                or child_stat.st_dev != item.st_dev
                                or child_stat.st_ino != item.st_ino
                            ):
                                os.close(child_fd)
                                _fail()
                            pending.append((child_fd, relative))
                        else:
                            files.add(relative)
            finally:
                os.close(directory_fd)
        return files
    except IntegrationInstallError:
        raise
    except (OSError, ValueError):
        _fail()
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        for directory_fd, _ in pending:
            os.close(directory_fd)


def _remove_activation_files(venv_root: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    venv_fd = -1
    bin_fd = -1
    try:
        _no_symlink_ancestors(venv_root)
        venv_fd = os.open(venv_root, flags)
        venv_item = os.fstat(venv_fd)
        if not stat.S_ISDIR(venv_item.st_mode) or venv_item.st_uid != os.getuid():
            _fail()
        bin_fd = os.open("bin", flags, dir_fd=venv_fd)
        bin_item = os.fstat(bin_fd)
        if not stat.S_ISDIR(bin_item.st_mode) or bin_item.st_uid != os.getuid():
            _fail()
        with os.scandir(bin_fd) as entries:
            removable: list[tuple[str, _ProvisionalIdentity, int]] = []
            for entry in entries:
                if not (
                    entry.name.startswith("activate")
                    or entry.name == "Activate.ps1"
                    or re.fullmatch(r"python3(?:\.\d+)?", entry.name)
                ):
                    continue
                item = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(item.st_mode) or stat.S_ISREG(item.st_mode):
                    if item.st_uid != os.getuid():
                        _fail()
                    removable.append(
                        (entry.name, _provisional_from_stat(item), item.st_nlink)
                    )
        for name, expected, expected_nlink in removable:
            try:
                current = os.stat(name, dir_fd=bin_fd, follow_symlinks=False)
            except OSError:
                _fail()
            if (
                _provisional_from_stat(current) != expected
                or current.st_nlink != expected_nlink
            ):
                _fail()
            os.unlink(name, dir_fd=bin_fd)
        try:
            lib64_item = os.stat("lib64", dir_fd=venv_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(lib64_item.st_mode):
                if lib64_item.st_uid != os.getuid():
                    _fail()
                expected_lib64 = _provisional_from_stat(lib64_item)
                try:
                    current_lib64 = os.stat(
                        "lib64", dir_fd=venv_fd, follow_symlinks=False
                    )
                except OSError:
                    _fail()
                if (
                    _provisional_from_stat(current_lib64) != expected_lib64
                    or current_lib64.st_nlink != lib64_item.st_nlink
                ):
                    _fail()
                os.unlink("lib64", dir_fd=venv_fd)
    except IntegrationInstallError:
        raise
    except (OSError, ValueError):
        _fail()
    finally:
        if bin_fd >= 0:
            os.close(bin_fd)
        if venv_fd >= 0:  # pragma: no branch - sentinel remains -1 on unwind
            os.close(venv_fd)


def _find_site_packages(
    venv_root: Path,
    venv_identity: _DirectoryIdentity,
) -> tuple[Path, _DirectoryIdentity]:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    venv_fd = -1
    lib_fd = -1
    try:
        _no_symlink_ancestors(venv_root)
        venv_fd = os.open(venv_root, flags)
        venv_item = os.fstat(venv_fd)
        if (
            not stat.S_ISDIR(venv_item.st_mode)
            or venv_item.st_uid != os.getuid()
            or _DirectoryIdentity(
                venv_item.st_dev,
                venv_item.st_ino,
                stat.S_IMODE(venv_item.st_mode),
            )
            != venv_identity
        ):
            _fail()
        lib_item: os.stat_result | None = None
        with os.scandir(venv_fd) as entries:
            for entry in entries:
                if entry.name == "lib":
                    lib_item = entry.stat(follow_symlinks=False)
                    break
        if (
            lib_item is None
            or not stat.S_ISDIR(lib_item.st_mode)
            or stat.S_ISLNK(lib_item.st_mode)
            or lib_item.st_uid != os.getuid()
        ):
            _fail()
        lib_identity = _DirectoryIdentity(
            lib_item.st_dev,
            lib_item.st_ino,
            stat.S_IMODE(lib_item.st_mode),
        )
        lib_fd = os.open("lib", flags, dir_fd=venv_fd)
        opened_lib = os.fstat(lib_fd)
        if (
            not stat.S_ISDIR(opened_lib.st_mode)
            or opened_lib.st_uid != os.getuid()
            or _DirectoryIdentity(
                opened_lib.st_dev,
                opened_lib.st_ino,
                stat.S_IMODE(opened_lib.st_mode),
            )
            != lib_identity
        ):
            _fail()
        candidates: list[tuple[str, _DirectoryIdentity]] = []
        with os.scandir(lib_fd) as python_entries:
            for python_entry in python_entries:
                if not python_entry.name.startswith("python"):
                    continue
                python_item = python_entry.stat(follow_symlinks=False)
                if (
                    not stat.S_ISDIR(python_item.st_mode)
                    or stat.S_ISLNK(python_item.st_mode)
                    or python_item.st_uid != os.getuid()
                ):
                    _fail()
                python_identity = _DirectoryIdentity(
                    python_item.st_dev,
                    python_item.st_ino,
                    stat.S_IMODE(python_item.st_mode),
                )
                python_fd = -1
                try:
                    python_fd = os.open(
                        python_entry.name,
                        flags,
                        dir_fd=lib_fd,
                    )
                    opened_python = os.fstat(python_fd)
                    if (
                        not stat.S_ISDIR(opened_python.st_mode)
                        or opened_python.st_uid != os.getuid()
                        or _DirectoryIdentity(
                            opened_python.st_dev,
                            opened_python.st_ino,
                            stat.S_IMODE(opened_python.st_mode),
                        )
                        != python_identity
                    ):
                        _fail()
                    with os.scandir(python_fd) as site_entries:
                        for site_entry in site_entries:
                            if site_entry.name != "site-packages":
                                continue
                            site_item = site_entry.stat(follow_symlinks=False)
                            if (
                                not stat.S_ISDIR(site_item.st_mode)
                                or stat.S_ISLNK(site_item.st_mode)
                                or site_item.st_uid != os.getuid()
                            ):
                                _fail()
                            site_identity = _DirectoryIdentity(
                                site_item.st_dev,
                                site_item.st_ino,
                                stat.S_IMODE(site_item.st_mode),
                            )
                            site_fd = -1
                            try:
                                site_fd = os.open(
                                    site_entry.name,
                                    flags,
                                    dir_fd=python_fd,
                                )
                                opened_site = os.fstat(site_fd)
                                if (
                                    not stat.S_ISDIR(opened_site.st_mode)
                                    or opened_site.st_uid != os.getuid()
                                    or _DirectoryIdentity(
                                        opened_site.st_dev,
                                        opened_site.st_ino,
                                        stat.S_IMODE(opened_site.st_mode),
                                    )
                                    != site_identity
                                ):
                                    _fail()
                                os.fchmod(site_fd, 0o700)
                                final_site = os.fstat(site_fd)
                                if (
                                    not stat.S_ISDIR(final_site.st_mode)
                                    or final_site.st_uid != os.getuid()
                                    or stat.S_IMODE(final_site.st_mode) != 0o700
                                ):
                                    _fail()
                                candidates.append(
                                    (
                                        python_entry.name,
                                        _DirectoryIdentity(
                                            final_site.st_dev,
                                            final_site.st_ino,
                                            stat.S_IMODE(final_site.st_mode),
                                        ),
                                    )
                                )
                            finally:
                                if site_fd >= 0:  # pragma: no branch - exception unwind
                                    os.close(site_fd)
                finally:
                    if python_fd >= 0:  # pragma: no branch - sentinel remains -1 on unwind
                        os.close(python_fd)
        if not candidates:
            _fail()
        python_name, site_identity = sorted(candidates)[0]
        return venv_root / "lib" / python_name / "site-packages", site_identity
    except IntegrationInstallError:
        raise
    except (OSError, ValueError):
        _fail()
    finally:
        if lib_fd >= 0:
            os.close(lib_fd)
        if venv_fd >= 0:  # pragma: no branch - sentinel remains -1 on unwind
            os.close(venv_fd)


def _write_exclusive(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    parent_identity: _DirectoryIdentity | None = None,
) -> _FileIdentity:
    _no_symlink_ancestors(path.parent)
    if parent_identity is None:
        parent_identity = _directory_identity(path.parent)
    elif _directory_identity(path.parent) != parent_identity:
        _fail()
    fd = -1
    parent_fd = -1
    provisional: _ProvisionalIdentity | None = None
    try:
        parent_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            parent_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            parent_flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            parent_flags |= os.O_CLOEXEC
        parent_fd = os.open(path.parent, parent_flags)
        parent_item = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_item.st_mode)
            or parent_item.st_uid != os.getuid()
            or _DirectoryIdentity(
                parent_item.st_dev,
                parent_item.st_ino,
                stat.S_IMODE(parent_item.st_mode),
            )
            != parent_identity
        ):
            _fail()
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _fail()
        fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent_fd,
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
        os.fchmod(fd, mode)
        os.fsync(fd)
        final_provisional = _provisional_rebased(
            path,
            provisional,
            parent_identity,
            directory=False,
        )
        if final_provisional is None:
            _fail()
        final_item = os.fstat(fd)
        if (
            not stat.S_ISREG(final_item.st_mode)
            or final_item.st_nlink != 1
            or final_item.st_uid != os.getuid()
            or stat.S_IMODE(final_item.st_mode) != mode
            or final_item.st_dev != provisional.device
            or final_item.st_ino != provisional.inode
        ):
            _fail()
        if (
            _provisional_rebased(
                path,
                provisional,
                parent_identity,
                directory=False,
            )
            != final_provisional
        ):
            _fail()
        return _FileIdentity(
            final_item.st_dev,
            final_item.st_ino,
            stat.S_IMODE(final_item.st_mode),
        )
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
        if parent_fd >= 0:  # pragma: no branch - sentinel remains -1 on unwind
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _write_launcher(
    *,
    path: Path,
    final_release_dir: Path,
    data_home: Path,
    state_home: Path,
    parent_identity: _DirectoryIdentity | None = None,
) -> _FileIdentity:
    if parent_identity is None:
        parent_identity = _directory_identity(path.parent)
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
    return _write_exclusive(
        path,
        payload,
        mode=0o700,
        parent_identity=parent_identity,
    )


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
        "schema_version": 2,
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


def _copy_source_into_project(
    source_root: Path,
    build_root: Path,
    *,
    build_identity: _DirectoryIdentity | None = None,
) -> None:
    expected_build_identity = build_identity or _directory_identity(build_root)
    if _directory_identity(build_root) != expected_build_identity:
        _fail()
    for relative_text in SOURCE_MANIFEST_FILES:
        _copy_regular(source_root / relative_text, build_root / relative_text)
    pyproject = build_root / "pyproject.toml"
    pyproject_identity = _file_identity(pyproject)
    if not _remove_owned_entry(
        pyproject,
        pyproject_identity,
        expected_build_identity,
        directory=False,
    ):
        _fail()
    _write_exclusive(
        pyproject,
        _GENERATED_PYPROJECT.encode("utf-8"),
        mode=0o600,
        parent_identity=expected_build_identity,
    )


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
    if 'version = "0.6.533"' not in pyproject or '__version__ = "0.6.533"' not in init_text:
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
            releases_identity = _require_private_dir(
                releases,
                None,
                True,
                parent_identity=integration_identity,
            )
            _revalidate_bootstrap(state_home, app_identity, integration_identity)
            _require_private_dir(temporary_root, temporary_identity, False)
            _require_private_dir(releases, releases_identity, False)
            final_release_dir = releases / release_id
            if _rehash_source_manifest(source_root) != source_manifest:
                _fail()
            token = secrets.token_hex(8)
            staging = releases / f".{release_id}.staging-{token}"
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
            _copy_source_into_project(
                source_root,
                build_root,
                build_identity=build_identity,
            )
            _require_private_dir(build_root, build_identity, False)
            _require_private_dir(temporary_root, temporary_identity, False)
            wheel_path, wheel_file_identity = _build_verified_wheel(
                python_executable=python_executable,
                environment=environment,
                build_root=build_root,
                wheel_dir=wheel_root,
                wheel_identity=wheel_identity,
            )
            _require_private_dir(temporary_root, temporary_identity, False)
            _require_private_dir(build_root, build_identity, False)
            _require_private_dir(wheel_root, wheel_identity, False)
            if _rehash_source_manifest(source_root) != source_manifest:
                _fail()
            package, record_rows = _wheel_details(
                wheel_path,
                parent_identity=wheel_identity,
                file_identity=wheel_file_identity,
            )
            _ = package
            staged_wheel = staging / "producer.whl"
            _require_private_dir(staging, staging_identity, False)
            staged_wheel_identity = _copy_regular(
                wheel_path,
                staged_wheel,
                source_parent_identity=wheel_identity,
                source_identity=wheel_file_identity,
            )
            _require_private_dir(staging, staging_identity, False)
            venv_root = staging / "venv"
            venv.EnvBuilder(
                system_site_packages=False,
                clear=False,
                symlinks=False,
                with_pip=False,
            ).create(venv_root)
            ensure_private_directory(venv_root, label="integration venv directory")
            venv_root_identity = _require_private_dir(venv_root, None, False)
            _require_private_dir(staging, staging_identity, False)
            _remove_activation_files(venv_root)
            site_packages, site_packages_identity = _find_site_packages(
                venv_root,
                venv_root_identity,
            )
            extracted_identities = _safe_extract_wheel(
                wheel_path=staged_wheel,
                destination=site_packages,
                record_rows=record_rows,
                wheel_parent_identity=staging_identity,
                wheel_file_identity=staged_wheel_identity,
                destination_identity=site_packages_identity,
            )
            _require_private_dir(staging, staging_identity, False)
            entrypoint_path = site_packages / "codex_usage" / "integration_entrypoint.py"
            record_path = site_packages / DIST_INFO_PREFIX / "RECORD"
            entrypoint_parent_identity, entrypoint_identity = extracted_identities[
                "codex_usage/integration_entrypoint.py"
            ]
            record_parent_identity, record_identity = extracted_identities[
                f"{DIST_INFO_PREFIX}/RECORD"
            ]
            launcher_path = venv_root / "bin" / "codex-usage"
            launcher_identity = _write_launcher(
                path=launcher_path,
                final_release_dir=final_release_dir,
                data_home=data_home,
                state_home=state_home,
            )
            _postwalk_release(staging, root_identity=staging_identity)
            _require_private_dir(staging, staging_identity, False)
            if _rehash_source_manifest(source_root) != source_manifest:
                _fail()
            entrypoint_payload = _read_nofollow(
                entrypoint_path,
                expected_parent_identity=entrypoint_parent_identity,
                expected_file_identity=entrypoint_identity,
            )
            wheel_payload = _read_nofollow(
                staged_wheel,
                expected_parent_identity=staging_identity,
                expected_file_identity=staged_wheel_identity,
            )
            record_payload = _read_nofollow(
                record_path,
                expected_parent_identity=record_parent_identity,
                expected_file_identity=record_identity,
            )
            launcher_payload = _read_nofollow(
                launcher_path,
                expected_file_identity=launcher_identity,
            )
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
            try:
                candidate_at_seam = _read_manifest(candidate_path)
            except IntegrationAttestationUnavailable:
                _fail()
            if candidate_at_seam != candidate:
                _fail()
            _rename_owned_directory(
                staging,
                final_release_dir,
                releases_identity,
                staging_identity,
            )
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
            active_text: str | None = None
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
                try:
                    _verify_manifest(
                        manifest_path=active_path,
                        state_home=state_home,
                        data_home=data_home,
                        expected_entrypoint_path=None,
                    )
                except IntegrationAttestationUnavailable:
                    _verify_legacy_manifest_for_upgrade(
                        manifest_path=active_path,
                        state_home=state_home,
                        data_home=data_home,
                    )
            published_text = _manifest_text(candidate)
            _revalidate_bootstrap(state_home, app_identity, integration_identity)
            write_private_text(
                active_path,
                published_text,
                label="active integration manifest",
                mode=0o600,
            )
            published_identity: _ProvisionalIdentity | None = None
            try:
                published_identity = _published_active_identity(
                    active_path=active_path,
                    published_text=published_text,
                    integration_identity=integration_identity,
                )
                _revalidate_bootstrap(state_home, app_identity, integration_identity)
                _verify_manifest(
                    manifest_path=active_path,
                    state_home=state_home,
                    data_home=data_home,
                    expected_entrypoint_path=None,
                )
                if active_text is not None:
                    _revalidate_bootstrap(
                        state_home,
                        app_identity,
                        integration_identity,
                    )
                    write_private_text(
                        integration / PREVIOUS_NAME,
                        active_text,
                        label="previous integration manifest",
                        mode=0o600,
                    )
            except Exception as publish_error:
                try:
                    if published_identity is None:
                        published_identity = _recover_uncaptured_active_identity(
                            active_path=active_path,
                            published_text=published_text,
                            state_home=state_home,
                            app_identity=app_identity,
                            integration_identity=integration_identity,
                        )
                    if active_text is None:
                        _revalidate_bootstrap(
                            state_home,
                            app_identity,
                            integration_identity,
                        )
                        if not _cleanup_provisional(
                            active_path,
                            published_identity,
                            integration_identity,
                            directory=False,
                        ):
                            _fail()
                    else:
                        _restore_active_manifest(
                            active_path=active_path,
                            active_text=active_text,
                            expected_published_identity=published_identity,
                            state_home=state_home,
                            app_identity=app_identity,
                            integration_identity=integration_identity,
                        )
                except Exception as restore_error:
                    raise IntegrationCleanupError() from restore_error
                raise publish_error
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
            active = integration / ACTIVE_NAME
            active_text: str | None = None
            if active.exists() or active.is_symlink():
                active_text, active_stat = read_private_text(
                    active,
                    regular_label="active manifest",
                    read_label="active manifest",
                    max_bytes=128 * 1024,
                )
                if (
                    active_stat.st_nlink != 1
                    or stat.S_IMODE(active_stat.st_mode) != 0o600
                ):
                    _fail()
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
                active,
                previous_text,
                label="active integration manifest",
                mode=0o600,
            )
            published_identity: _ProvisionalIdentity | None = None
            try:
                published_identity = _published_active_identity(
                    active_path=active,
                    published_text=previous_text,
                    integration_identity=integration_identity,
                )
                _revalidate_bootstrap(state_home, app_identity, integration_identity)
                _verify_manifest(
                    manifest_path=active,
                    state_home=state_home,
                    data_home=data_home,
                    expected_entrypoint_path=None,
                )
            except Exception as publish_error:
                try:
                    if published_identity is None:
                        published_identity = _recover_uncaptured_active_identity(
                            active_path=active,
                            published_text=previous_text,
                            state_home=state_home,
                            app_identity=app_identity,
                            integration_identity=integration_identity,
                        )
                    if active_text is None:
                        _revalidate_bootstrap(
                            state_home,
                            app_identity,
                            integration_identity,
                        )
                        if not _cleanup_provisional(
                            active,
                            published_identity,
                            integration_identity,
                            directory=False,
                        ):
                            _fail()
                    else:
                        _restore_active_manifest(
                            active_path=active,
                            active_text=active_text,
                            expected_published_identity=published_identity,
                            state_home=state_home,
                            app_identity=app_identity,
                            integration_identity=integration_identity,
                        )
                except Exception as restore_error:
                    raise IntegrationCleanupError() from restore_error
                raise publish_error
            return release
    except IntegrationInstallError:
        raise
    except Exception:
        raise IntegrationInstallError() from None
