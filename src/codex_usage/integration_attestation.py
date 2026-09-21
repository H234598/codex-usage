from __future__ import annotations

import base64
import binascii
import csv
import email.parser
import email.policy
import errno
import hashlib
import importlib.util
import io
import json
import marshal
import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .json_utils import loads_strict
from .private_io import (
    FileIdentity,
    IntegrationEvidenceInvalid,
    IntegrationEvidenceUnavailable,
    assert_no_symlink_ancestors,
    open_private_dir_at,
    open_verified_state_home,
    read_private_bytes_at,
    read_private_text,
)

_MANIFEST_MAX_BYTES = 128 * 1024
MAX_ATTESTATION_FILE_BYTES = 4 * 1024 * 1024
MAX_RELEASE_TREE_ENTRIES = 4096
MAX_RELEASE_TREE_BYTES = 128 * 1024 * 1024
_DIST_INFO_PREFIX = "codex_usage_integration_producer-0.6.541.dist-info"
_EXPECTED_VERSION = "0.6.541"
_EXPECTED_DISTRIBUTION = "codex-usage-integration-producer"
_EXPECTED_CORE_DISTRIBUTION = "codex-usage"
_CORE_DIST_INFO_PREFIX = "codex_usage-0.6.541.dist-info"
TRUSTED_CORE_MODULES = (
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
PRODUCER_RELEASE_MODULES = tuple(
    module
    for module in TRUSTED_CORE_MODULES
    if module not in {"integration_timeout_contract.py", "integration_watchdog.py"}
)
TRUSTED_PRODUCER_CORE_MODULES = TRUSTED_CORE_MODULES
TRUSTED_PRODUCER_CORE_RECORD_RELATIVES = tuple(
    f"codex_usage/{name}" for name in TRUSTED_CORE_MODULES
)
PRODUCER_RELEASE_RECORD_RELATIVES = tuple(
    f"codex_usage/{name}" for name in PRODUCER_RELEASE_MODULES
)
RUNTIME_SELF_ATTESTED_CORE_MODULES = (
    "codex_usage",
    "codex_usage.integration_attestation",
    "codex_usage.integration_entrypoint",
    "codex_usage.integration_timeout_contract",
    "codex_usage.integration_watchdog",
    "codex_usage.json_utils",
    "codex_usage.private_io",
)
_PRODUCER_DIST_INFO_RECORD_RELATIVES = (
    f"{_DIST_INFO_PREFIX}/METADATA",
    f"{_DIST_INFO_PREFIX}/WHEEL",
    f"{_DIST_INFO_PREFIX}/RECORD",
    f"{_DIST_INFO_PREFIX}/top_level.txt",
)
MAX_RUNTIME_INTERPRETER_BYTES = 128 * 1024 * 1024
_PREVIOUS_SCHEMA2_DIST_INFO_PREFIX = "codex_usage_integration_producer-0.6.540.dist-info"
_PREVIOUS_SCHEMA2_VERSION = "0.6.540"
_CURRENT_SCHEMA2_MANIFEST_FIELDS = frozenset(
    {
        "data_home",
        "entrypoint_path",
        "entrypoint_sha256",
        "launcher_path",
        "launcher_sha256",
        "record_path",
        "record_sha256",
        "release_dir",
        "release_id",
        "release_tree_sha256",
        "schema_version",
        "source_manifest_sha256",
        "state_home",
        "version",
        "wheel_path",
        "wheel_sha256",
    }
)
_PREVIOUS_SCHEMA2_MANIFEST_FIELDS = frozenset(
    {
        "data_home",
        "entrypoint_path",
        "entrypoint_sha256",
        "launcher_path",
        "launcher_sha256",
        "record_path",
        "record_sha256",
        "release_dir",
        "release_id",
        "release_tree_sha256",
        "schema_version",
        "source_manifest_sha256",
        "state_home",
        "version",
        "wheel_path",
        "wheel_sha256",
    }
)
class IntegrationAttestationUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ActiveRelease:
    version: str
    release_dir: Path
    launcher_path: Path
    entrypoint_path: Path
    entrypoint_sha256: str
    wheel_sha256: str
    record_sha256: str
    launcher_sha256: str
    release_tree_sha256: str


@dataclass(frozen=True)
class VerifiedActiveManifest:
    active_release: ActiveRelease
    release_id: str
    source_manifest_sha256: str
    active_manifest_bytes: bytes
    active_manifest_sha256: str
    state_home_identity: FileIdentity
    integration_parent_identity: FileIdentity
    active_file_identity: FileIdentity


@dataclass(frozen=True)
class _ReleaseTreeEvidence:
    releases_identity: FileIdentity
    entries: tuple[_ReleaseEntryEvidence, ...]
    rows: tuple[bytes, ...]


@dataclass(frozen=True)
class _ReleaseEntryEvidence:
    relative: str
    is_directory: bool
    identity: FileIdentity
    uid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _TrustedDirectoryIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    ctime_ns: int


@dataclass(frozen=True)
class _TrustedCoreProvenanceEvidence:
    dist_info_path: Path
    dist_info_identity: _TrustedDirectoryIdentity
    metadata_identity: tuple[int, ...]
    metadata_payload: bytes
    record_identity: tuple[int, ...]
    record_payload: bytes
    trusted_modules: tuple[_CoreModuleEvidence, ...]
    active_modules: tuple[_CoreModuleEvidence, ...]


@dataclass(frozen=True)
class _CoreModuleEvidence:
    relative: str
    identity: tuple[int, ...]
    payload_sha256: str
    size: int
    payload: bytes


@dataclass(frozen=True)
class _RuntimeInterpreterEvidence:
    path: Path
    identity: tuple[int, ...]


def _before_release_namespace_recheck(_release_fd: int) -> None:
    return None


def _before_expected_runtime_bytecode_validation(_package_path: Path) -> None:
    return None


def _before_trusted_entrypoint_recheck(_trusted_entrypoint_path: Path) -> None:
    return None


def _before_runtime_self_attestation_recheck(_trusted_entrypoint_path: Path) -> None:
    return None


def _unavailable() -> IntegrationAttestationUnavailable:
    return IntegrationAttestationUnavailable()


def _private_regular(path: Path, *, mode: int) -> os.stat_result:
    try:
        item = path.lstat()
    except (OSError, ValueError):
        raise _unavailable() from None
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_nlink != 1
        or stat.S_IMODE(item.st_mode) != mode
        or item.st_uid != os.getuid()
    ):
        raise _unavailable()
    return item


def _private_directory(path: Path, *, mode: int = 0o700) -> os.stat_result:
    try:
        item = path.lstat()
    except (OSError, ValueError):
        raise _unavailable() from None
    if (
        not stat.S_ISDIR(item.st_mode)
        or stat.S_IMODE(item.st_mode) != mode
        or item.st_uid != os.getuid()
    ):
        raise _unavailable()
    return item


def _file_bytes(path: Path, *, mode: int) -> bytes:
    initial_item = _private_regular(path, mode=mode)
    if initial_item.st_size > MAX_ATTESTATION_FILE_BYTES:
        raise _unavailable()
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    parent_fd = -1
    try:
        parent_item = path.parent.lstat()
        if not stat.S_ISDIR(parent_item.st_mode) or parent_item.st_uid != os.getuid():
            raise _unavailable()
        parent_fd = os.open(path.parent, directory_flags)
        opened_parent = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or opened_parent.st_uid != os.getuid()
            or opened_parent.st_dev != parent_item.st_dev
            or opened_parent.st_ino != parent_item.st_ino
        ):
            raise _unavailable()
        fd = os.open(path.name, file_flags, dir_fd=parent_fd)
        item = os.fstat(fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or item.st_uid != os.getuid()
            or stat.S_IMODE(item.st_mode) != mode
            or item.st_dev != initial_item.st_dev
            or item.st_ino != initial_item.st_ino
        ):
            raise _unavailable()
        if item.st_size > MAX_ATTESTATION_FILE_BYTES:
            raise _unavailable()
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            payload = handle.read(MAX_ATTESTATION_FILE_BYTES + 1)
            if len(payload) > MAX_ATTESTATION_FILE_BYTES:
                raise _unavailable()
            return payload
    except (OSError, ValueError):
        raise _unavailable() from None
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _same_stat_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_mode == right.st_mode
        and left.st_uid == right.st_uid
        and left.st_gid == right.st_gid
        and left.st_nlink == right.st_nlink
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _stable_file_identity(item: os.stat_result) -> tuple[int, ...]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_uid,
        item.st_gid,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _valid_hash(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise _unavailable()
    return value


def _absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _unavailable()
    path = Path(value)
    if not path.is_absolute() or str(path) != value:
        raise _unavailable()
    return path


def _contained(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise _unavailable() from None
    if not relative.parts or any(
        part in {"", ".", ".."} or "\\" in part for part in relative.parts
    ):
        raise _unavailable()


def _validate_expected_runtime_bytecode(
    *,
    package_entries: tuple[tuple[str, int, os.stat_result], ...],
    cache_fd: int,
    cache_item: os.stat_result,
    python_directory: str,
    package_path: Path,
) -> int:
    _before_expected_runtime_bytecode_validation(package_path)
    cache_tag = "cpython-" + python_directory.removeprefix("python").replace(".", "")
    expected_sources: dict[str, tuple[bytes, os.stat_result, Path]] = {}
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    for name, source_fd, item in package_entries:
        if not (stat.S_ISREG(item.st_mode) and name.endswith(".py")):
            continue
        opened = os.fstat(source_fd)
        source_payload = bytearray()
        os.lseek(source_fd, 0, os.SEEK_SET)
        while len(source_payload) <= MAX_ATTESTATION_FILE_BYTES:
            chunk = os.read(
                source_fd,
                min(
                    65_536,
                    MAX_ATTESTATION_FILE_BYTES + 1 - len(source_payload),
                ),
            )
            if not chunk:
                break
            source_payload.extend(chunk)
        final = os.fstat(source_fd)
        os.lseek(source_fd, 0, os.SEEK_SET)
        if (
            item.st_uid != os.getuid()
            or item.st_nlink != 1
            or stat.S_IMODE(item.st_mode) != 0o600
            or opened.st_dev != item.st_dev
            or opened.st_ino != item.st_ino
            or opened.st_mode != item.st_mode
            or opened.st_uid != item.st_uid
            or opened.st_nlink != item.st_nlink
            or opened.st_size != item.st_size
            or opened.st_mtime_ns != item.st_mtime_ns
            or opened.st_ctime_ns != item.st_ctime_ns
            or final.st_dev != opened.st_dev
            or final.st_ino != opened.st_ino
            or final.st_mode != opened.st_mode
            or final.st_uid != opened.st_uid
            or final.st_nlink != opened.st_nlink
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
            or len(source_payload) > MAX_ATTESTATION_FILE_BYTES
        ):
            raise _unavailable()
        expected_sources[f"{name[:-3]}.{cache_tag}.pyc"] = (
            bytes(source_payload),
            opened,
            package_path / name,
        )
    if (
        cache_item.st_uid != os.getuid()
        or stat.S_IMODE(cache_item.st_mode) & 0o022
    ):
        raise _unavailable()
    initial_cache = os.fstat(cache_fd)
    if (
        initial_cache.st_dev != cache_item.st_dev
        or initial_cache.st_ino != cache_item.st_ino
        or initial_cache.st_mode != cache_item.st_mode
        or initial_cache.st_uid != cache_item.st_uid
    ):
        raise _unavailable()
    count = 0
    with os.scandir(cache_fd) as entries:
        for entry in entries:
            count += 1
            source = expected_sources.get(entry.name)
            if count > len(expected_sources) or source is None:
                raise _unavailable()
            initial = entry.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(initial.st_mode)
                or initial.st_uid != os.getuid()
                or initial.st_nlink != 1
                or stat.S_IMODE(initial.st_mode) != 0o600
                or initial.st_size < 16
                or initial.st_size > MAX_ATTESTATION_FILE_BYTES
            ):
                raise _unavailable()
            fd = -1
            try:
                fd = os.open(entry.name, file_flags, dir_fd=cache_fd)
                opened = os.fstat(fd)
                payload = bytearray()
                while len(payload) <= MAX_ATTESTATION_FILE_BYTES:
                    chunk = os.read(
                        fd,
                        min(
                            65_536,
                            MAX_ATTESTATION_FILE_BYTES + 1 - len(payload),
                        ),
                    )
                    if not chunk:
                        break
                    payload.extend(chunk)
                final = os.fstat(fd)
                source_payload, source_item, source_path = source
                flags = int.from_bytes(payload[4:8], "little")
                if flags == 0:
                    expected_header_data = (
                        (int(source_item.st_mtime) & 0xFFFFFFFF).to_bytes(4, "little")
                        + (len(source_payload) & 0xFFFFFFFF).to_bytes(4, "little")
                    )
                elif flags in {1, 3}:
                    expected_header_data = importlib.util.source_hash(source_payload)
                else:
                    raise _unavailable()
                try:
                    expected_code = compile(
                        source_payload,
                        str(source_path),
                        "exec",
                        dont_inherit=True,
                        optimize=0,
                    )
                except (MemoryError, OverflowError, SyntaxError, ValueError):
                    raise _unavailable() from None
                if (
                    opened.st_dev != initial.st_dev
                    or opened.st_ino != initial.st_ino
                    or opened.st_mode != initial.st_mode
                    or opened.st_uid != initial.st_uid
                    or opened.st_nlink != initial.st_nlink
                    or opened.st_size != initial.st_size
                    or opened.st_mtime_ns != initial.st_mtime_ns
                    or opened.st_ctime_ns != initial.st_ctime_ns
                    or final.st_dev != opened.st_dev
                    or final.st_ino != opened.st_ino
                    or final.st_mode != opened.st_mode
                    or final.st_uid != opened.st_uid
                    or final.st_nlink != opened.st_nlink
                    or final.st_size != opened.st_size
                    or final.st_mtime_ns != opened.st_mtime_ns
                    or final.st_ctime_ns != opened.st_ctime_ns
                    or len(payload) > MAX_ATTESTATION_FILE_BYTES
                    or payload[:4] != importlib.util.MAGIC_NUMBER
                    or payload[8:16] != expected_header_data
                    or payload[16:] != marshal.dumps(expected_code)
                ):
                    raise _unavailable()
            finally:
                if fd >= 0:
                    os.close(fd)
    final_cache = os.fstat(cache_fd)
    if (
        final_cache.st_dev != initial_cache.st_dev
        or final_cache.st_ino != initial_cache.st_ino
        or final_cache.st_mode != initial_cache.st_mode
        or final_cache.st_uid != initial_cache.st_uid
        or final_cache.st_mtime_ns != initial_cache.st_mtime_ns
        or final_cache.st_ctime_ns != initial_cache.st_ctime_ns
    ):
        raise _unavailable()
    return count


def _release_tree_rows(
    *,
    release_dir: Path,
    expected_runtime_bytecode_root: str | None = None,
) -> list[bytes]:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    root_fd = -1
    try:
        root_fd = os.open(release_dir, directory_flags)
        root_stat = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or stat.S_ISLNK(root_stat.st_mode)
            or root_stat.st_uid != os.getuid()
        ):
            raise _unavailable()
    except (OSError, ValueError, IntegrationAttestationUnavailable):
        if root_fd >= 0:
            try:
                os.close(root_fd)
            except OSError:
                pass
            root_fd = -1
        raise _unavailable() from None
    rows: list[bytes] = []
    entries_seen = 1
    file_bytes = 0
    stack: list[tuple[int, str, os.stat_result]] = [(root_fd, ".", root_stat)]
    root_fd = -1
    try:
        while stack:
            directory_fd, relative, item = stack.pop()
            try:
                mode = stat.S_IMODE(item.st_mode)
                if stat.S_ISDIR(item.st_mode):
                    rows.append(f"D {relative}\0{mode:04o}\n".encode())
                    children: list[tuple[str, int, os.stat_result]] = []
                    expected_cache_fd = -1
                    expected_cache_item: os.stat_result | None = None
                    try:
                        with os.scandir(directory_fd) as entries:
                            for entry in entries:
                                if entries_seen >= MAX_RELEASE_TREE_ENTRIES:
                                    raise _unavailable()
                                entries_seen += 1
                                name = entry.name
                                child_item = entry.stat(follow_symlinks=False)
                                is_expected_cache = (
                                    expected_runtime_bytecode_root is not None
                                    and relative == expected_runtime_bytecode_root
                                    and stat.S_ISDIR(child_item.st_mode)
                                    and name == "__pycache__"
                                )
                                if (
                                    not name
                                    or name in {".", ".."}
                                    or "\\" in name
                                    or (
                                        stat.S_ISDIR(child_item.st_mode)
                                        and name == "__pycache__"
                                        and not is_expected_cache
                                    )
                                    or (
                                        stat.S_ISREG(child_item.st_mode)
                                        and name.endswith(".pyc")
                                    )
                                ):
                                    raise _unavailable()
                                if stat.S_ISLNK(child_item.st_mode) or not (
                                    stat.S_ISDIR(child_item.st_mode)
                                    or stat.S_ISREG(child_item.st_mode)
                                ) or child_item.st_uid != os.getuid():
                                    raise _unavailable()
                                child_flags = (
                                    directory_flags
                                    if stat.S_ISDIR(child_item.st_mode)
                                    else file_flags
                                )
                                child_fd = -1
                                try:
                                    child_fd = os.open(
                                        name,
                                        child_flags,
                                        dir_fd=directory_fd,
                                    )
                                    opened_item = os.fstat(child_fd)
                                    if (
                                        stat.S_IFMT(opened_item.st_mode)
                                        != stat.S_IFMT(child_item.st_mode)
                                        or opened_item.st_dev != child_item.st_dev
                                        or opened_item.st_ino != child_item.st_ino
                                        or opened_item.st_uid != os.getuid()
                                    ):
                                        raise _unavailable()
                                    if is_expected_cache:
                                        if expected_cache_fd >= 0:
                                            raise _unavailable()
                                        expected_cache_fd = child_fd
                                        expected_cache_item = opened_item
                                        child_fd = -1
                                        continue
                                    children.append((name, child_fd, opened_item))
                                    child_fd = -1
                                finally:
                                    if child_fd >= 0:
                                        os.close(child_fd)
                        if expected_cache_fd >= 0:
                            if expected_cache_item is None:
                                raise _unavailable()
                            entries_seen += _validate_expected_runtime_bytecode(
                                package_entries=tuple(children),
                                cache_fd=expected_cache_fd,
                                cache_item=expected_cache_item,
                                python_directory=Path(
                                    expected_runtime_bytecode_root
                                ).parts[2],
                                package_path=release_dir
                                / expected_runtime_bytecode_root.removeprefix("./"),
                            )
                            if entries_seen > MAX_RELEASE_TREE_ENTRIES:
                                raise _unavailable()
                            final_directory = os.fstat(directory_fd)
                            if (
                                final_directory.st_dev != item.st_dev
                                or final_directory.st_ino != item.st_ino
                                or final_directory.st_mode != item.st_mode
                                or final_directory.st_uid != item.st_uid
                                or final_directory.st_nlink != item.st_nlink
                                or final_directory.st_size != item.st_size
                                or final_directory.st_mtime_ns != item.st_mtime_ns
                                or final_directory.st_ctime_ns != item.st_ctime_ns
                            ):
                                raise _unavailable()
                            os.close(expected_cache_fd)
                            expected_cache_fd = -1
                        children.sort(key=lambda child: child[0], reverse=True)
                        stack.extend(
                            (
                                child_fd,
                                f"{relative}/{name}",
                                child_item,
                            )
                            for name, child_fd, child_item in children
                        )
                        children.clear()
                    finally:
                        if expected_cache_fd >= 0:
                            os.close(expected_cache_fd)
                        for _, child_fd, _ in children:
                            os.close(child_fd)
                    continue
                if stat.S_ISREG(item.st_mode):
                    if item.st_nlink != 1:
                        raise _unavailable()
                    if item.st_size > MAX_ATTESTATION_FILE_BYTES:
                        raise _unavailable()
                    if file_bytes + item.st_size > MAX_RELEASE_TREE_BYTES:
                        raise _unavailable()
                    file_fd = directory_fd
                    directory_fd = -1
                    payload = _read_nofollow_fd(file_fd)
                    file_bytes += len(payload)
                    if file_bytes > MAX_RELEASE_TREE_BYTES:
                        raise _unavailable()
                    rows.append(
                        f"F {relative}\0{mode:04o}\0{len(payload)}\0".encode()
                        + _sha256_bytes(payload).encode("ascii")
                        + b"\n"
                    )
                    continue
                raise _unavailable()
            finally:
                if directory_fd >= 0:
                    os.close(directory_fd)
        return rows
    except (OSError, ValueError):
        raise _unavailable() from None
    finally:
        for directory_fd, _, _ in stack:
            os.close(directory_fd)


def _release_entry_evidence(
    relative: str,
    item: os.stat_result,
) -> _ReleaseEntryEvidence:
    return _ReleaseEntryEvidence(
        relative=relative,
        is_directory=stat.S_ISDIR(item.st_mode),
        identity=FileIdentity(
            item.st_dev,
            item.st_ino,
            stat.S_IMODE(item.st_mode),
            gid=item.st_gid,
            uid=item.st_uid,
            ctime_ns=item.st_ctime_ns,
        ),
        uid=item.st_uid,
        nlink=item.st_nlink,
        size=item.st_size,
        mtime_ns=item.st_mtime_ns,
        ctime_ns=item.st_ctime_ns,
    )


def _scan_release_tree_at(
    release_anchor_fd: int,
) -> tuple[list[_ReleaseEntryEvidence], list[bytes]]:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    stack: list[tuple[int, str, os.stat_result]] = []
    try:
        root_fd = os.open(".", directory_flags, dir_fd=release_anchor_fd)
        stack.append((root_fd, ".", os.fstat(root_fd)))
        rows: list[bytes] = []
        entries: list[_ReleaseEntryEvidence] = []
        entries_seen = 1
        file_bytes = 0
        while stack:
            item_fd, relative, initial = stack.pop()
            try:
                mode = stat.S_IMODE(initial.st_mode)
                entries.append(_release_entry_evidence(relative, initial))
                if stat.S_ISDIR(initial.st_mode):
                    if initial.st_uid != os.getuid():
                        raise _unavailable()
                    rows.append(f"D {relative}\0{mode:04o}\n".encode())
                    children: list[tuple[str, int, os.stat_result]] = []
                    try:
                        with os.scandir(item_fd) as directory_entries:
                            for entry in directory_entries:
                                if entries_seen >= MAX_RELEASE_TREE_ENTRIES:
                                    raise _unavailable()
                                entries_seen += 1
                                name = entry.name
                                child_initial = entry.stat(follow_symlinks=False)
                                if (
                                    not name
                                    or name in {".", ".."}
                                    or "/" in name
                                    or "\\" in name
                                    or "\x00" in name
                                    or (
                                        stat.S_ISDIR(child_initial.st_mode)
                                        and name == "__pycache__"
                                    )
                                    or (
                                        stat.S_ISREG(child_initial.st_mode)
                                        and name.endswith(".pyc")
                                    )
                                ):
                                    raise _unavailable()
                                if stat.S_ISDIR(child_initial.st_mode):
                                    child_flags = directory_flags
                                elif stat.S_ISREG(child_initial.st_mode):
                                    child_flags = file_flags
                                else:
                                    raise _unavailable()
                                child_fd = -1
                                try:
                                    child_fd = os.open(
                                        name,
                                        child_flags,
                                        dir_fd=item_fd,
                                    )
                                    opened = os.fstat(child_fd)
                                    if (
                                        stat.S_IFMT(opened.st_mode)
                                        != stat.S_IFMT(child_initial.st_mode)
                                        or opened.st_dev != child_initial.st_dev
                                        or opened.st_ino != child_initial.st_ino
                                        or opened.st_uid != os.getuid()
                                        or opened.st_mode != child_initial.st_mode
                                        or (
                                            stat.S_ISREG(opened.st_mode)
                                            and (
                                                opened.st_nlink != 1
                                                or opened.st_size
                                                > MAX_ATTESTATION_FILE_BYTES
                                            )
                                        )
                                    ):
                                        raise _unavailable()
                                    children.append((name, child_fd, opened))
                                    child_fd = -1
                                finally:
                                    if child_fd >= 0:
                                        os.close(child_fd)
                        children.sort(key=lambda child: child[0], reverse=True)
                        stack.extend(
                            (
                                child_fd,
                                f"{relative}/{name}",
                                child_item,
                            )
                            for name, child_fd, child_item in children
                        )
                        children.clear()
                    finally:
                        for _, child_fd, _ in children:
                            os.close(child_fd)
                    final = os.fstat(item_fd)
                    if (
                        final.st_dev != initial.st_dev
                        or final.st_ino != initial.st_ino
                        or final.st_mode != initial.st_mode
                        or final.st_uid != initial.st_uid
                        or final.st_nlink != initial.st_nlink
                        or final.st_size != initial.st_size
                        or final.st_mtime_ns != initial.st_mtime_ns
                        or final.st_ctime_ns != initial.st_ctime_ns
                    ):
                        raise _unavailable()
                    continue
                if not stat.S_ISREG(initial.st_mode):
                    raise _unavailable()
                if (
                    initial.st_uid != os.getuid()
                    or initial.st_nlink != 1
                    or initial.st_size > MAX_ATTESTATION_FILE_BYTES
                    or file_bytes + initial.st_size > MAX_RELEASE_TREE_BYTES
                ):
                    raise _unavailable()
                payload = bytearray()
                while len(payload) <= MAX_ATTESTATION_FILE_BYTES:
                    chunk = os.read(
                        item_fd,
                        min(
                            65_536,
                            MAX_ATTESTATION_FILE_BYTES + 1 - len(payload),
                        ),
                    )
                    if not chunk:
                        break
                    payload.extend(chunk)
                if len(payload) > MAX_ATTESTATION_FILE_BYTES:
                    raise _unavailable()
                final = os.fstat(item_fd)
                if (
                    final.st_dev != initial.st_dev
                    or final.st_ino != initial.st_ino
                    or final.st_mode != initial.st_mode
                    or final.st_uid != initial.st_uid
                    or final.st_nlink != initial.st_nlink
                    or final.st_size != initial.st_size
                    or final.st_mtime_ns != initial.st_mtime_ns
                    or final.st_ctime_ns != initial.st_ctime_ns
                ):
                    raise _unavailable()
                file_bytes += len(payload)
                if file_bytes > MAX_RELEASE_TREE_BYTES:
                    raise _unavailable()
                rows.append(
                    f"F {relative}\0{mode:04o}\0{len(payload)}\0".encode()
                    + _sha256_bytes(bytes(payload)).encode("ascii")
                    + b"\n"
                )
            finally:
                os.close(item_fd)
        return entries, rows
    except IntegrationAttestationUnavailable:
        raise
    except (OSError, ValueError):
        raise _unavailable() from None
    finally:
        for item_fd, _, _ in stack:
            os.close(item_fd)


def _release_tree_evidence_at(
    *,
    integration_fd: int,
    release_id: str,
) -> _ReleaseTreeEvidence:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    releases_fd = -1
    release_fd = -1
    release_anchor_fd = -1
    try:
        releases_fd = open_private_dir_at(integration_fd, "releases")
        releases_item = os.fstat(releases_fd)
        releases_identity = _fd_identity(releases_fd)
        release_fd = open_private_dir_at(releases_fd, release_id)
        release_item = os.fstat(release_fd)
        release_anchor_fd = os.open(".", directory_flags, dir_fd=release_fd)
        entries, rows = _scan_release_tree_at(release_anchor_fd)
        _before_release_namespace_recheck(release_anchor_fd)
        _verify_release_namespace_at(
            integration_fd=integration_fd,
            releases_identity=releases_identity,
            release_id=release_id,
            release_identity=FileIdentity(
                release_item.st_dev,
                release_item.st_ino,
                stat.S_IMODE(release_item.st_mode),
                gid=release_item.st_gid,
                uid=release_item.st_uid,
                ctime_ns=release_item.st_ctime_ns,
            ),
            release_anchor_fd=release_anchor_fd,
            entries=entries,
        )
        repeated_entries, repeated_rows = _scan_release_tree_at(release_anchor_fd)
        if repeated_entries != entries or repeated_rows != rows:
            raise _unavailable()
        _verify_release_namespace_at(
            integration_fd=integration_fd,
            releases_identity=releases_identity,
            release_id=release_id,
            release_identity=FileIdentity(
                release_item.st_dev,
                release_item.st_ino,
                stat.S_IMODE(release_item.st_mode),
                gid=release_item.st_gid,
                uid=release_item.st_uid,
                ctime_ns=release_item.st_ctime_ns,
            ),
            release_anchor_fd=release_anchor_fd,
            entries=repeated_entries,
        )
        current_releases = os.fstat(releases_fd)
        if (
            current_releases.st_dev != releases_item.st_dev
            or current_releases.st_ino != releases_item.st_ino
            or current_releases.st_mode != releases_item.st_mode
            or current_releases.st_uid != releases_item.st_uid
        ):
            raise _unavailable()
        return _ReleaseTreeEvidence(
            releases_identity=releases_identity,
            entries=tuple(entries),
            rows=tuple(rows),
        )
    except IntegrationAttestationUnavailable:
        raise
    except (OSError, ValueError):
        raise _unavailable() from None
    finally:
        if release_fd >= 0:
            os.close(release_fd)
        if release_anchor_fd >= 0:
            os.close(release_anchor_fd)
        if releases_fd >= 0:
            os.close(releases_fd)


def _verify_release_entry_at(
    *,
    release_anchor_fd: int,
    expected: _ReleaseEntryEvidence,
) -> None:
    relative = expected.relative
    if relative == ".":
        item = os.fstat(release_anchor_fd)
    else:
        if not relative.startswith("./"):
            raise _unavailable()
        components = relative[2:].split("/")
        if not components or any(
            not component
            or component in {".", ".."}
            or "\\" in component
            or "\x00" in component
            for component in components
        ):
            raise _unavailable()
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        current_fd = os.dup(release_anchor_fd)
        try:
            for index, component in enumerate(components):
                final = index == len(components) - 1
                flags = (
                    file_flags
                    if final and not expected.is_directory
                    else directory_flags
                )
                next_fd = os.open(component, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            item = os.fstat(current_fd)
        finally:
            os.close(current_fd)
    identity = FileIdentity(
        item.st_dev,
        item.st_ino,
        stat.S_IMODE(item.st_mode),
        gid=item.st_gid,
        uid=item.st_uid,
        ctime_ns=item.st_ctime_ns,
    )
    if (
        identity != expected.identity
        or item.st_uid != expected.uid
        or item.st_nlink != expected.nlink
        or item.st_size != expected.size
        or item.st_mtime_ns != expected.mtime_ns
        or item.st_ctime_ns != expected.ctime_ns
        or item.st_uid != os.getuid()
        or (expected.is_directory and not stat.S_ISDIR(item.st_mode))
        or (
            not expected.is_directory
            and (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1)
        )
    ):
        raise _unavailable()


def _verify_release_namespace_at(
    *,
    integration_fd: int,
    releases_identity: FileIdentity,
    release_id: str,
    release_identity: FileIdentity,
    release_anchor_fd: int,
    entries: list[_ReleaseEntryEvidence],
) -> None:
    bound_releases_fd = -1
    bound_release_fd = -1
    try:
        bound_releases_fd = open_private_dir_at(integration_fd, "releases")
        if _fd_identity(bound_releases_fd) != releases_identity:
            raise _unavailable()
        bound_release_fd = open_private_dir_at(bound_releases_fd, release_id)
        if _fd_identity(bound_release_fd) != release_identity:
            raise _unavailable()
        for entry in entries:
            _verify_release_entry_at(
                release_anchor_fd=release_anchor_fd,
                expected=entry,
            )
    finally:
        if bound_release_fd >= 0:
            os.close(bound_release_fd)
        if bound_releases_fd >= 0:
            os.close(bound_releases_fd)


def _read_nofollow_fd(fd: int) -> bytes:
    try:
        item = os.fstat(fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or item.st_uid != os.getuid()
            or item.st_size > MAX_ATTESTATION_FILE_BYTES
        ):
            raise _unavailable()
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            payload = handle.read(MAX_ATTESTATION_FILE_BYTES + 1)
            if len(payload) > MAX_ATTESTATION_FILE_BYTES:
                raise _unavailable()
            return payload
    except (OSError, ValueError):
        raise _unavailable() from None
    finally:
        if fd >= 0:
            os.close(fd)


def _read_nofollow_bytes(
    path: Path,
    *,
    expected_file_identity: os.stat_result | None = None,
) -> bytes:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    parent_fd = -1
    try:
        parent_item = path.parent.lstat()
        if not stat.S_ISDIR(parent_item.st_mode) or parent_item.st_uid != os.getuid():
            raise _unavailable()
        parent_fd = os.open(path.parent, directory_flags)
        opened_parent = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or opened_parent.st_uid != os.getuid()
            or opened_parent.st_dev != parent_item.st_dev
            or opened_parent.st_ino != parent_item.st_ino
        ):
            raise _unavailable()
        fd = os.open(path.name, file_flags, dir_fd=parent_fd)
        item = os.fstat(fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or item.st_uid != os.getuid()
            or item.st_size > MAX_ATTESTATION_FILE_BYTES
            or (
                expected_file_identity is not None
                and (
                    item.st_dev != expected_file_identity.st_dev
                    or item.st_ino != expected_file_identity.st_ino
                    or stat.S_IMODE(item.st_mode)
                    != stat.S_IMODE(expected_file_identity.st_mode)
                )
            )
        ):
            raise _unavailable()
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            payload = handle.read(MAX_ATTESTATION_FILE_BYTES + 1)
            if len(payload) > MAX_ATTESTATION_FILE_BYTES:
                raise _unavailable()
            return payload
    except (OSError, ValueError):
        raise _unavailable() from None
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _release_tree_sha256(
    *,
    release_dir: Path,
    expected_runtime_bytecode_root: str | None = None,
) -> str:
    try:
        rows = (
            _release_tree_rows(release_dir=release_dir)
            if expected_runtime_bytecode_root is None
            else _release_tree_rows(
                release_dir=release_dir,
                expected_runtime_bytecode_root=expected_runtime_bytecode_root,
            )
        )
        return hashlib.sha256(b"".join(rows)).hexdigest()
    except IntegrationAttestationUnavailable:
        raise
    except Exception:
        raise _unavailable() from None


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        text, item = read_private_text(
            path,
            regular_label="integration manifest",
            read_label="integration manifest",
            max_bytes=_MANIFEST_MAX_BYTES,
            too_large_label="integration manifest",
            invalid_utf8_label="integration manifest",
        )
    except Exception:
        raise _unavailable() from None
    if item.st_nlink != 1 or stat.S_IMODE(item.st_mode) != 0o600:
        raise _unavailable()
    try:
        value = loads_strict(text)
    except Exception:
        raise _unavailable() from None
    if not isinstance(value, dict):
        raise _unavailable()
    return value


def _manifest_from_canonical_bytes(payload: bytes) -> dict[str, object]:
    try:
        text = payload.decode("utf-8")
        value = loads_strict(text)
    except (UnicodeDecodeError, ValueError):
        raise _unavailable() from None
    if not isinstance(value, dict):
        raise _unavailable()
    canonical = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if canonical != payload:
        raise _unavailable()
    return value


def _require_manifest_fields(
    manifest: dict[str, object],
    *,
    expected_fields: frozenset[str],
) -> dict[str, object]:
    if set(manifest) != expected_fields:
        raise _unavailable()
    return manifest


def _manifest_string(manifest: Mapping[str, object], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value:
        raise _unavailable()
    return value


def _record_digest(value: str, payload: bytes) -> bool:
    if type(value) is not str or not value.startswith("sha256="):
        return False
    encoded = value[7:]
    if len(encoded) != 43 or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in encoded
    ):
        return False
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, binascii.Error):
        return False
    expected = hashlib.sha256(payload).digest()
    return (
        decoded == expected
        and encoded == base64.urlsafe_b64encode(expected).decode("ascii").rstrip("=")
    )


def _record_digest_text(payload: bytes) -> str:
    return (
        "sha256="
        + base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
        .decode("ascii")
        .rstrip("=")
    )


def _metadata_header(payload: bytes, name: str) -> str:
    try:
        message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
            payload
        )
    except Exception:
        raise _unavailable() from None
    values = message.get_all(name)
    if values is None or len(values) != 1:
        raise _unavailable()
    value = values[0]
    if not isinstance(value, str) or not value:
        raise _unavailable()
    return value


def _record_rows(record_path: Path, release_dir: Path) -> dict[str, tuple[str, int]]:
    payload = _file_bytes(record_path, mode=0o600)
    site_packages = record_path.parent.parent
    seen: set[str] = set()
    validated: dict[str, tuple[str, int]] = {}
    try:
        reader = csv.reader(io.StringIO(payload.decode("utf-8")))
        rows_seen = 0
        for row in reader:
            rows_seen += 1
            if rows_seen > MAX_RELEASE_TREE_ENTRIES:
                raise _unavailable()
            if len(row) != 3 or row[0] in seen:
                raise _unavailable()
            relative_text, digest, size_text = row
            seen.add(relative_text)
            if (
                not relative_text
                or relative_text.startswith("/")
                or "\\" in relative_text
                or "\x00" in relative_text
                or any(part in {"", ".", ".."} for part in relative_text.split("/"))
            ):
                raise _unavailable()
            target = site_packages / relative_text
            _contained(target, release_dir)
            item = _private_regular(target, mode=0o600)
            target_payload = _read_nofollow_bytes(
                target,
                expected_file_identity=item,
            )
            if digest or size_text:
                if not digest or not size_text or not size_text.isdecimal():
                    raise _unavailable()
                try:
                    size = int(size_text)
                except (OverflowError, ValueError):
                    raise _unavailable() from None
                if not _record_digest(digest, target_payload) or size != item.st_size:
                    raise _unavailable()
                validated[relative_text] = (digest, item.st_size)
            elif target != record_path:
                raise _unavailable()
            else:
                validated[relative_text] = ("", -1)
    except (UnicodeDecodeError, csv.Error):
        raise _unavailable() from None
    if not seen:
        raise _unavailable()
    if str(record_path.relative_to(site_packages).as_posix()) not in seen:
        raise _unavailable()
    return validated


def _verify_manifest_contract(
    *,
    manifest_path: Path,
    state_home: Path,
    data_home: Path,
    expected_entrypoint_path: Path | None,
    expected_schema_version: int,
    expected_version: str,
    expected_dist_info_prefix: str,
    expected_fields: frozenset[str],
    require_bytecode_environment: bool = True,
    allow_expected_runtime_bytecode: bool = False,
    manifest_payload: bytes | None = None,
) -> ActiveRelease:
    manifest = _require_manifest_fields(
        (
            _read_manifest(manifest_path)
            if manifest_payload is None
            else _manifest_from_canonical_bytes(manifest_payload)
        ),
        expected_fields=expected_fields,
    )
    schema_version = manifest.get("schema_version")
    if type(schema_version) is not int or schema_version != expected_schema_version:
        raise _unavailable()
    if manifest.get("version") != expected_version:
        raise _unavailable()
    source_manifest_digest = _valid_hash(manifest.get("source_manifest_sha256"))
    manifest_state = _absolute_path(manifest.get("state_home"))
    manifest_data = _absolute_path(manifest.get("data_home"))
    if manifest_state != state_home or manifest_data != data_home:
        raise _unavailable()
    _private_directory(state_home)
    _private_directory(data_home)
    integration = state_home / "codex-usage" / "integration"
    _private_directory(state_home / "codex-usage")
    _private_directory(integration)

    release_dir = _absolute_path(manifest.get("release_dir"))
    releases_dir = integration / "releases"
    _private_directory(releases_dir)
    _contained(release_dir, releases_dir)
    _private_directory(release_dir)
    release_id = f"{expected_version}-{source_manifest_digest[:16]}"
    if (
        release_dir.parent != releases_dir
        or release_dir.name != release_id
        or manifest.get("release_id") != release_id
    ):
        raise _unavailable()

    launcher_path = _absolute_path(manifest.get("launcher_path"))
    entrypoint_path = _absolute_path(manifest.get("entrypoint_path"))
    wheel_path = _absolute_path(manifest.get("wheel_path"))
    record_path = _absolute_path(manifest.get("record_path"))
    for path in (launcher_path, entrypoint_path, wheel_path, record_path):
        _contained(path, release_dir)
    site_packages = record_path.parent.parent
    try:
        site_packages_parts = site_packages.relative_to(release_dir).parts
    except ValueError:
        raise _unavailable() from None
    python_directory = site_packages_parts[2] if len(site_packages_parts) == 4 else ""
    if (
        len(site_packages_parts) != 4
        or site_packages_parts[:2] != ("venv", "lib")
        or site_packages_parts[3] != "site-packages"
        or not python_directory.startswith("python3.")
        or not python_directory.removeprefix("python3.").isdecimal()
        or launcher_path != release_dir / "venv" / "bin" / "codex-usage"
        or wheel_path != release_dir / "producer.whl"
        or record_path
        != site_packages / expected_dist_info_prefix / "RECORD"
        or entrypoint_path
        != site_packages / "codex_usage" / "integration_entrypoint.py"
    ):
        raise _unavailable()
    if expected_entrypoint_path is not None:
        if not isinstance(expected_entrypoint_path, Path):
            raise _unavailable()
        if (
            not expected_entrypoint_path.is_absolute()
            or expected_entrypoint_path != entrypoint_path
        ):
            raise _unavailable()

    entrypoint_payload = _file_bytes(entrypoint_path, mode=0o600)
    wheel_payload = _file_bytes(wheel_path, mode=0o600)
    record_payload = _file_bytes(record_path, mode=0o600)
    launcher_payload = _file_bytes(launcher_path, mode=0o700)
    entrypoint_hash = _valid_hash(manifest.get("entrypoint_sha256"))
    wheel_hash = _valid_hash(manifest.get("wheel_sha256"))
    record_hash = _valid_hash(manifest.get("record_sha256"))
    launcher_hash = _valid_hash(manifest.get("launcher_sha256"))
    tree_hash = _valid_hash(manifest.get("release_tree_sha256"))
    if (
        _sha256_bytes(entrypoint_payload) != entrypoint_hash
        or _sha256_bytes(wheel_payload) != wheel_hash
        or _sha256_bytes(record_payload) != record_hash
        or _sha256_bytes(launcher_payload) != launcher_hash
    ):
        raise _unavailable()
    if (
        b" -B -I -m codex_usage.integration_entrypoint" not in launcher_payload
        or (
            require_bytecode_environment
            and b" PYTHONDONTWRITEBYTECODE=1 XDG_DATA_HOME=" not in launcher_payload
        )
    ):
        raise _unavailable()
    record_rows = _record_rows(record_path, release_dir)
    entrypoint_relative = entrypoint_path.relative_to(record_path.parent.parent).as_posix()
    entrypoint_record = record_rows.get(entrypoint_relative)
    if (
        entrypoint_record is None
        or not entrypoint_record[0]
        or entrypoint_record[1] != len(entrypoint_payload)
        or not _record_digest(entrypoint_record[0], entrypoint_payload)
    ):
        raise _unavailable()
    try:
        metadata_path = record_path.parent / "METADATA"
        metadata_payload = _read_nofollow_bytes(metadata_path)
    except IntegrationAttestationUnavailable:
        raise _unavailable() from None
    if (
        _metadata_header(metadata_payload, "Version") != expected_version
        or _metadata_header(metadata_payload, "Name") != _EXPECTED_DISTRIBUTION
    ):
        raise _unavailable()
    expected_runtime_bytecode_root = (
        f"./venv/lib/{python_directory}/site-packages/codex_usage"
        if allow_expected_runtime_bytecode
        else None
    )
    if (
        _release_tree_sha256(
            release_dir=release_dir,
            expected_runtime_bytecode_root=expected_runtime_bytecode_root,
        )
        != tree_hash
    ):
        raise _unavailable()
    return ActiveRelease(
        version=expected_version,
        release_dir=release_dir,
        launcher_path=launcher_path,
        entrypoint_path=entrypoint_path,
        entrypoint_sha256=entrypoint_hash,
        wheel_sha256=wheel_hash,
        record_sha256=record_hash,
        launcher_sha256=launcher_hash,
        release_tree_sha256=tree_hash,
    )


def _verify_manifest(
    *,
    manifest_path: Path,
    state_home: Path,
    data_home: Path,
    expected_entrypoint_path: Path | None,
    manifest_payload: bytes | None = None,
) -> ActiveRelease:
    return _verify_manifest_contract(
        manifest_path=manifest_path,
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=expected_entrypoint_path,
        expected_schema_version=2,
        expected_version=_EXPECTED_VERSION,
        expected_dist_info_prefix=_DIST_INFO_PREFIX,
        expected_fields=_CURRENT_SCHEMA2_MANIFEST_FIELDS,
        manifest_payload=manifest_payload,
    )


def _active_entrypoint_candidate_from_active_manifest(
    *,
    state_home: Path,
    data_home: Path,
) -> Path:
    state_fd = -1
    app_fd = -1
    integration_fd = -1
    try:
        state_fd = open_verified_state_home(state_home)
        app_fd = open_private_dir_at(state_fd, "codex-usage")
        integration_fd = open_private_dir_at(app_fd, "integration")
        payload, _identity = read_private_bytes_at(
            integration_fd,
            "active.json",
            maximum=_MANIFEST_MAX_BYTES,
            mode=0o600,
        )
        manifest = _manifest_from_canonical_bytes(payload)
        if _absolute_path(manifest.get("state_home")) != state_home:
            raise _unavailable()
        if _absolute_path(manifest.get("data_home")) != data_home:
            raise _unavailable()
        return _absolute_path(manifest.get("entrypoint_path"))
    except FileNotFoundError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    except (OSError, IntegrationAttestationUnavailable) as exc:
        raise IntegrationEvidenceUnavailable() from exc
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    finally:
        if integration_fd >= 0:
            os.close(integration_fd)
        if app_fd >= 0:
            os.close(app_fd)
        if state_fd >= 0:
            os.close(state_fd)


def _trusted_entrypoint_bytes_and_stat(path: Path) -> tuple[bytes, os.stat_result]:
    if not isinstance(path, Path):
        raise IntegrationEvidenceUnavailable()
    if (
        not path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts[1:])
        or "\x00" in str(path)
    ):
        raise IntegrationEvidenceUnavailable()
    try:
        assert_no_symlink_ancestors(path, label="trusted integration entrypoint")
        initial = path.lstat()
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_uid != os.geteuid()
            or initial.st_nlink != 1
            or stat.S_IMODE(initial.st_mode) != 0o644
            or initial.st_size > MAX_ATTESTATION_FILE_BYTES
        ):
            raise IntegrationEvidenceUnavailable()
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        parent_fd = -1
        fd = -1
        try:
            parent = path.parent.lstat()
            if (
                not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.geteuid()
                or bool(stat.S_IMODE(parent.st_mode) & 0o022)
            ):
                raise IntegrationEvidenceUnavailable()
            parent_fd = os.open(path.parent, directory_flags)
            opened_parent = os.fstat(parent_fd)
            if (
                not stat.S_ISDIR(opened_parent.st_mode)
                or opened_parent.st_uid != os.geteuid()
                or opened_parent.st_dev != parent.st_dev
                or opened_parent.st_ino != parent.st_ino
                or opened_parent.st_mode != parent.st_mode
                or bool(stat.S_IMODE(opened_parent.st_mode) & 0o022)
            ):
                raise IntegrationEvidenceUnavailable()
            fd = os.open(path.name, file_flags, dir_fd=parent_fd)
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not _same_stat_identity(opened, initial)
                or stat.S_IMODE(opened.st_mode) != 0o644
            ):
                raise IntegrationEvidenceUnavailable()
            payload = bytearray()
            while len(payload) <= MAX_ATTESTATION_FILE_BYTES:
                chunk = os.read(
                    fd,
                    min(65_536, MAX_ATTESTATION_FILE_BYTES + 1 - len(payload)),
                )
                if not chunk:
                    break
                payload.extend(chunk)
            final = os.fstat(fd)
            if len(payload) > MAX_ATTESTATION_FILE_BYTES:
                raise IntegrationEvidenceUnavailable()
            if not _same_stat_identity(final, opened):
                raise IntegrationEvidenceUnavailable()
            return bytes(payload), opened
        finally:
            if fd >= 0:
                os.close(fd)
            if parent_fd >= 0:
                os.close(parent_fd)
    except IntegrationEvidenceUnavailable:
        raise
    except IntegrationAttestationUnavailable as exc:
        raise IntegrationEvidenceUnavailable() from exc
    except (OSError, ValueError) as exc:
        raise IntegrationEvidenceUnavailable() from exc


def _trusted_file_bytes_at(
    directory_fd: int,
    name: str,
    *,
    mode: int,
) -> tuple[bytes, os.stat_result]:
    if (
        type(name) is not str
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise IntegrationEvidenceUnavailable()
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = -1
    try:
        fd = os.open(name, file_flags, dir_fd=directory_fd)
        initial = os.fstat(fd)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_uid != os.geteuid()
            or initial.st_nlink != 1
            or stat.S_IMODE(initial.st_mode) != mode
            or initial.st_size > MAX_ATTESTATION_FILE_BYTES
        ):
            raise IntegrationEvidenceUnavailable()
        payload = bytearray()
        while len(payload) <= MAX_ATTESTATION_FILE_BYTES:
            chunk = os.read(
                fd,
                min(65_536, MAX_ATTESTATION_FILE_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        final = os.fstat(fd)
        if len(payload) > MAX_ATTESTATION_FILE_BYTES:
            raise IntegrationEvidenceUnavailable()
        if not _same_stat_identity(final, initial):
            raise IntegrationEvidenceUnavailable()
        return bytes(payload), initial
    except IntegrationEvidenceUnavailable:
        raise
    except IntegrationAttestationUnavailable as exc:
        raise IntegrationEvidenceUnavailable() from exc
    except (OSError, ValueError) as exc:
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _trusted_directory_fd_at(parent_fd: int, name: str) -> tuple[int, os.stat_result]:
    if (
        type(name) is not str
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise IntegrationEvidenceUnavailable()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
        item = os.fstat(fd)
        mode = stat.S_IMODE(item.st_mode)
        if (
            not stat.S_ISDIR(item.st_mode)
            or item.st_uid != os.geteuid()
            or bool(mode & 0o022)
        ):
            raise IntegrationEvidenceUnavailable()
        result = fd
        fd = -1
        return result, item
    except IntegrationEvidenceUnavailable:
        raise
    except (OSError, ValueError) as exc:
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _trusted_directory_identity(item: os.stat_result) -> _TrustedDirectoryIdentity:
    return _TrustedDirectoryIdentity(
        device=item.st_dev,
        inode=item.st_ino,
        mode=stat.S_IMODE(item.st_mode),
        uid=item.st_uid,
        gid=item.st_gid,
        ctime_ns=item.st_ctime_ns,
    )


def _require_trusted_directory_identity_fd(
    fd: int,
    *,
    root_uid: int,
) -> _TrustedDirectoryIdentity:
    try:
        item = os.fstat(fd)
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    mode = stat.S_IMODE(item.st_mode)
    root_sticky = item.st_uid == root_uid and bool(item.st_mode & stat.S_ISVTX)
    if (
        not stat.S_ISDIR(item.st_mode)
        or item.st_uid not in {root_uid, os.geteuid()}
        or (bool(mode & 0o022) and not root_sticky)
    ):
        raise IntegrationEvidenceUnavailable()
    return _trusted_directory_identity(item)


def _trusted_entrypoint_ancestor_identities(
    path: Path,
) -> tuple[_TrustedDirectoryIdentity, ...]:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts[1:])
        or "\x00" in str(path)
    ):
        raise IntegrationEvidenceUnavailable()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    current_fd = -1
    try:
        current_fd = os.open(path.anchor, flags)
        root_uid = os.fstat(current_fd).st_uid
        identities = [_require_trusted_directory_identity_fd(current_fd, root_uid=root_uid)]
        for component in path.parent.parts[1:]:
            if (
                not component
                or component in {".", ".."}
                or "/" in component
                or "\\" in component
                or "\x00" in component
            ):
                raise IntegrationEvidenceUnavailable()
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise IntegrationEvidenceUnavailable() from exc
                raise
            os.close(current_fd)
            current_fd = next_fd
            identities.append(
                _require_trusted_directory_identity_fd(
                    current_fd,
                    root_uid=root_uid,
                )
            )
        return tuple(identities)
    except IntegrationEvidenceUnavailable:
        raise
    except (OSError, ValueError) as exc:
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _trusted_core_record_rows(payload: bytes) -> dict[str, tuple[str, int]]:
    rows: dict[str, tuple[str, int]] = {}
    try:
        reader = csv.reader(io.StringIO(payload.decode("utf-8")))
        rows_seen = 0
        for row in reader:
            rows_seen += 1
            if rows_seen > MAX_RELEASE_TREE_ENTRIES:
                raise _unavailable()
            if len(row) != 3:
                raise _unavailable()
            relative_text, digest, size_text = row
            if (
                relative_text in rows
                or not relative_text
                or relative_text.startswith("/")
                or "\\" in relative_text
                or "\x00" in relative_text
                or any(part in {"", ".", ".."} for part in relative_text.split("/"))
            ):
                raise _unavailable()
            if digest or size_text:
                if not digest or not size_text.isdecimal():
                    raise _unavailable()
                try:
                    size = int(size_text)
                except (OverflowError, ValueError):
                    raise _unavailable() from None
                rows[relative_text] = (digest, size)
            else:
                rows[relative_text] = ("", -1)
    except (UnicodeDecodeError, csv.Error):
        raise _unavailable() from None
    if not rows:
        raise _unavailable()
    return rows


def _require_core_record_row(
    rows: Mapping[str, tuple[str, int]],
    relative: str,
    payload: bytes,
) -> None:
    row = rows.get(relative)
    if (
        row is None
        or row[0] != _record_digest_text(payload)
        or row[1] != len(payload)
    ):
        raise IntegrationEvidenceUnavailable()


def _trusted_core_provenance_evidence(
    trusted_entrypoint_path: Path,
    trusted_entrypoint_payload: bytes,
) -> _TrustedCoreProvenanceEvidence:
    if (
        trusted_entrypoint_path.name != "integration_entrypoint.py"
        or trusted_entrypoint_path.parent.name != "codex_usage"
    ):
        raise IntegrationEvidenceUnavailable()
    site_packages = trusted_entrypoint_path.parent.parent
    assert_no_symlink_ancestors(site_packages, label="trusted core site-packages")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    site_fd = -1
    selected: tuple[Path, _TrustedDirectoryIdentity, bytes, os.stat_result] | None = None
    codex_usage_dist_infos = 0
    try:
        site_fd = os.open(site_packages, flags)
        site_item = os.fstat(site_fd)
        if (
            not stat.S_ISDIR(site_item.st_mode)
            or site_item.st_uid != os.geteuid()
            or bool(stat.S_IMODE(site_item.st_mode) & 0o022)
        ):
            raise IntegrationEvidenceUnavailable()
        entries_seen = 0
        with os.scandir(site_fd) as entries:
            for entry in entries:
                entries_seen += 1
                if entries_seen > MAX_RELEASE_TREE_ENTRIES:
                    raise IntegrationEvidenceUnavailable()
                name = entry.name
                if not (
                    isinstance(name, str)
                    and name.startswith("codex_usage-")
                    and name.endswith(".dist-info")
                ):
                    continue
                dist_fd = -1
                try:
                    dist_fd, dist_item = _trusted_directory_fd_at(site_fd, name)
                    metadata_payload, metadata_item = _trusted_file_bytes_at(
                        dist_fd,
                        "METADATA",
                        mode=0o644,
                    )
                    if _metadata_header(metadata_payload, "Name") != (
                        _EXPECTED_CORE_DISTRIBUTION
                    ):
                        continue
                    codex_usage_dist_infos += 1
                    if name == _CORE_DIST_INFO_PREFIX:
                        selected = (
                            site_packages / name,
                            _trusted_directory_identity(dist_item),
                            metadata_payload,
                            metadata_item,
                        )
                finally:
                    if dist_fd >= 0:
                        os.close(dist_fd)
        if codex_usage_dist_infos != 1 or selected is None:
            raise IntegrationEvidenceUnavailable()
        dist_info_path, dist_identity, metadata_payload, metadata_item = selected
        dist_fd, dist_item = _trusted_directory_fd_at(site_fd, dist_info_path.name)
        try:
            if _trusted_directory_identity(dist_item) != dist_identity:
                raise IntegrationEvidenceUnavailable()
            metadata_payload, metadata_item = _trusted_file_bytes_at(
                dist_fd,
                "METADATA",
                mode=0o644,
            )
            if (
                _metadata_header(metadata_payload, "Name")
                != _EXPECTED_CORE_DISTRIBUTION
                or _metadata_header(metadata_payload, "Version") != _EXPECTED_VERSION
            ):
                raise IntegrationEvidenceUnavailable()
            record_payload, record_item = _trusted_file_bytes_at(
                dist_fd,
                "RECORD",
                mode=0o644,
            )
            rows = _trusted_core_record_rows(record_payload)
            entrypoint_relative = trusted_entrypoint_path.relative_to(
                site_packages
            ).as_posix()
            metadata_relative = (dist_info_path / "METADATA").relative_to(
                site_packages
            ).as_posix()
            record_relative = (dist_info_path / "RECORD").relative_to(
                site_packages
            ).as_posix()
            _require_core_record_row(
                rows,
                entrypoint_relative,
                trusted_entrypoint_payload,
            )
            _require_core_record_row(rows, metadata_relative, metadata_payload)
            if rows.get(record_relative) != ("", -1):
                raise IntegrationEvidenceUnavailable()
            package_fd = -1
            try:
                package_fd, _package_item = _trusted_directory_fd_at(
                    site_fd,
                    "codex_usage",
                )
                trusted_modules: list[_CoreModuleEvidence] = []
                for module_name in TRUSTED_CORE_MODULES:
                    relative = f"codex_usage/{module_name}"
                    payload, item = _trusted_file_bytes_at(
                        package_fd,
                        module_name,
                        mode=0o644,
                    )
                    _require_core_record_row(rows, relative, payload)
                    trusted_modules.append(
                        _CoreModuleEvidence(
                            relative=relative,
                            identity=_stable_file_identity(item),
                            payload_sha256=_sha256_bytes(payload),
                            size=len(payload),
                            payload=payload,
                        )
                    )
            finally:
                if package_fd >= 0:
                    os.close(package_fd)
            return _TrustedCoreProvenanceEvidence(
                dist_info_path=dist_info_path,
                dist_info_identity=dist_identity,
                metadata_identity=_stable_file_identity(metadata_item),
                metadata_payload=metadata_payload,
                record_identity=_stable_file_identity(record_item),
                record_payload=record_payload,
                trusted_modules=tuple(trusted_modules),
                active_modules=(),
            )
        finally:
            os.close(dist_fd)
    except IntegrationEvidenceUnavailable:
        raise
    except IntegrationAttestationUnavailable as exc:
        raise IntegrationEvidenceUnavailable() from exc
    except (OSError, ValueError) as exc:
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        if site_fd >= 0:
            os.close(site_fd)


def _active_release_core_module_evidence(
    verified: VerifiedActiveManifest,
    trusted_modules: tuple[_CoreModuleEvidence, ...],
) -> tuple[_CoreModuleEvidence, ...]:
    trusted_by_relative = {module.relative: module for module in trusted_modules}
    if set(trusted_by_relative) != set(TRUSTED_PRODUCER_CORE_RECORD_RELATIVES):
        raise IntegrationEvidenceUnavailable()
    site_packages = verified.active_release.entrypoint_path.parent.parent
    package_path = site_packages / "codex_usage"
    record_path = site_packages / _DIST_INFO_PREFIX / "RECORD"
    if verified.active_release.entrypoint_path != package_path / "integration_entrypoint.py":
        raise IntegrationEvidenceUnavailable()
    _contained(package_path, verified.active_release.release_dir)
    _contained(record_path, verified.active_release.release_dir)
    record_rows = _record_rows(record_path, verified.active_release.release_dir)
    if set(record_rows) != (
        set(PRODUCER_RELEASE_RECORD_RELATIVES)
        | set(_PRODUCER_DIST_INFO_RECORD_RELATIVES)
    ):
        raise IntegrationEvidenceUnavailable()

    expected_names = set(PRODUCER_RELEASE_MODULES)
    package_initial = _private_directory(package_path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    package_fd = -1
    try:
        package_fd = os.open(package_path, flags)
        package_opened = os.fstat(package_fd)
        if not _same_stat_identity(package_opened, package_initial):
            raise IntegrationEvidenceUnavailable()
        seen: set[str] = set()
        modules: list[_CoreModuleEvidence] = []
        with os.scandir(package_fd) as entries:
            for entry in entries:
                name = entry.name
                if (
                    type(name) is not str
                    or name not in expected_names
                    or name in seen
                    or len(seen) >= len(expected_names)
                ):
                    raise IntegrationEvidenceUnavailable()
                seen.add(name)
                relative = f"codex_usage/{name}"
                payload, item = _trusted_file_bytes_at(
                    package_fd,
                    name,
                    mode=0o600,
                )
                _require_core_record_row(record_rows, relative, payload)
                trusted = trusted_by_relative[relative]
                if payload != trusted.payload:
                    raise IntegrationEvidenceUnavailable()
                modules.append(
                    _CoreModuleEvidence(
                        relative=relative,
                        identity=_stable_file_identity(item),
                        payload_sha256=_sha256_bytes(payload),
                        size=len(payload),
                        payload=payload,
                    )
                )
        if seen != expected_names:
            raise IntegrationEvidenceUnavailable()
        if not _same_stat_identity(os.fstat(package_fd), package_opened):
            raise IntegrationEvidenceUnavailable()
        modules.sort(key=lambda module: module.relative)
        return tuple(modules)
    except IntegrationEvidenceUnavailable:
        raise
    except IntegrationAttestationUnavailable as exc:
        raise IntegrationEvidenceUnavailable() from exc
    except (OSError, ValueError) as exc:
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        if package_fd >= 0:
            os.close(package_fd)


def _runtime_module_relative(module_name: str) -> str:
    if module_name == "codex_usage":
        return "codex_usage/__init__.py"
    prefix = "codex_usage."
    if (
        type(module_name) is not str
        or not module_name.startswith(prefix)
        or module_name.count(".") != 1
    ):
        raise IntegrationEvidenceUnavailable()
    leaf = module_name.removeprefix(prefix)
    if (
        not leaf
        or not leaf.isidentifier()
        or "/" in leaf
        or "\\" in leaf
        or "\x00" in leaf
    ):
        raise IntegrationEvidenceUnavailable()
    return f"codex_usage/{leaf}.py"


def _runtime_interpreter_evidence(
    interpreter_path: Path | None,
) -> _RuntimeInterpreterEvidence:
    selected = Path(sys.executable) if interpreter_path is None else interpreter_path
    if (
        not isinstance(selected, Path)
        or not selected.is_absolute()
        or any(part in {"", ".", ".."} for part in selected.parts[1:])
        or "\x00" in str(selected)
    ):
        raise IntegrationEvidenceUnavailable()
    try:
        resolved = selected.resolve(strict=True)
        if (
            not resolved.is_absolute()
            or any(part in {"", ".", ".."} for part in resolved.parts[1:])
            or "\x00" in str(resolved)
        ):
            raise IntegrationEvidenceUnavailable()
        assert_no_symlink_ancestors(resolved, label="runtime interpreter")
        initial = resolved.lstat()
        mode = stat.S_IMODE(initial.st_mode)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_uid not in {0, os.geteuid()}
            or bool(mode & 0o022)
            or not bool(mode & 0o111)
            or initial.st_size <= 0
            or initial.st_size > MAX_RUNTIME_INTERPRETER_BYTES
        ):
            raise IntegrationEvidenceUnavailable()
        final = resolved.lstat()
        if not _same_stat_identity(final, initial):
            raise IntegrationEvidenceUnavailable()
        return _RuntimeInterpreterEvidence(
            path=resolved,
            identity=_stable_file_identity(final),
        )
    except IntegrationEvidenceUnavailable:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise IntegrationEvidenceUnavailable() from exc


def _runtime_core_module_evidence(
    *,
    trusted_entrypoint_path: Path,
    trusted_modules: tuple[_CoreModuleEvidence, ...],
    module_names: tuple[str, ...],
) -> tuple[_CoreModuleEvidence, ...]:
    if (
        type(module_names) is not tuple
        or not module_names
        or len(module_names) > len(TRUSTED_CORE_MODULES)
        or len(set(module_names)) != len(module_names)
    ):
        raise IntegrationEvidenceUnavailable()
    trusted_by_relative = {module.relative: module for module in trusted_modules}
    site_packages = trusted_entrypoint_path.parent.parent
    modules: list[_CoreModuleEvidence] = []
    for module_name in module_names:
        relative = _runtime_module_relative(module_name)
        trusted = trusted_by_relative.get(relative)
        module = sys.modules.get(module_name)
        if trusted is None or module is None:
            raise IntegrationEvidenceUnavailable()
        module_file = getattr(module, "__file__", None)
        module_spec = getattr(module, "__spec__", None)
        spec_origin = None if module_spec is None else getattr(module_spec, "origin", None)
        if type(module_file) is not str or type(spec_origin) is not str:
            raise IntegrationEvidenceUnavailable()
        if module_file != spec_origin:
            raise IntegrationEvidenceUnavailable()
        module_path = Path(module_file)
        if (
            not module_path.is_absolute()
            or any(part in {"", ".", ".."} for part in module_path.parts[1:])
            or "\x00" in str(module_path)
            or module_path != site_packages / relative
        ):
            raise IntegrationEvidenceUnavailable()
        initial = _private_regular(module_path, mode=0o644)
        payload = _read_nofollow_bytes(
            module_path,
            expected_file_identity=initial,
        )
        final = _private_regular(module_path, mode=0o644)
        if (
            not _same_stat_identity(final, initial)
            or payload != trusted.payload
            or _sha256_bytes(payload) != trusted.payload_sha256
            or len(payload) != trusted.size
        ):
            raise IntegrationEvidenceUnavailable()
        modules.append(
            _CoreModuleEvidence(
                relative=relative,
                identity=_stable_file_identity(final),
                payload_sha256=_sha256_bytes(payload),
                size=len(payload),
                payload=payload,
            )
        )
    modules.sort(key=lambda module: module.relative)
    return tuple(modules)


def _verify_trusted_core_provenance(
    trusted_entrypoint_path: Path,
    verified: VerifiedActiveManifest,
    trusted_entrypoint_payload: bytes,
    *,
    expected: _TrustedCoreProvenanceEvidence | None = None,
) -> _TrustedCoreProvenanceEvidence:
    if (
        verified.active_release.version != _EXPECTED_VERSION
        or not verified.release_id.startswith(f"{_EXPECTED_VERSION}-")
    ):
        raise IntegrationEvidenceUnavailable()
    evidence = _trusted_core_provenance_evidence(
        trusted_entrypoint_path,
        trusted_entrypoint_payload,
    )
    evidence = _TrustedCoreProvenanceEvidence(
        dist_info_path=evidence.dist_info_path,
        dist_info_identity=evidence.dist_info_identity,
        metadata_identity=evidence.metadata_identity,
        metadata_payload=evidence.metadata_payload,
        record_identity=evidence.record_identity,
        record_payload=evidence.record_payload,
        trusted_modules=evidence.trusted_modules,
        active_modules=_active_release_core_module_evidence(
            verified,
            evidence.trusted_modules,
        ),
    )
    if expected is not None and evidence != expected:
        raise IntegrationEvidenceUnavailable()
    return evidence


def verify_runtime_self_attestation(
    *,
    trusted_entrypoint_path: Path,
    verified: VerifiedActiveManifest,
    module_names: tuple[str, ...] = RUNTIME_SELF_ATTESTED_CORE_MODULES,
    interpreter_path: Path | None = None,
) -> None:
    interpreter = _runtime_interpreter_evidence(interpreter_path)
    trusted_payload, trusted_stat = _trusted_entrypoint_bytes_and_stat(
        trusted_entrypoint_path,
    )
    provenance = _verify_trusted_core_provenance(
        trusted_entrypoint_path,
        verified,
        trusted_payload,
    )
    runtime_modules = _runtime_core_module_evidence(
        trusted_entrypoint_path=trusted_entrypoint_path,
        trusted_modules=provenance.trusted_modules,
        module_names=module_names,
    )
    _before_runtime_self_attestation_recheck(trusted_entrypoint_path)
    try:
        repeated_interpreter = _runtime_interpreter_evidence(interpreter_path)
        repeated_trusted_payload, repeated_trusted_stat = (
            _trusted_entrypoint_bytes_and_stat(trusted_entrypoint_path)
        )
        repeated_provenance = _verify_trusted_core_provenance(
            trusted_entrypoint_path,
            verified,
            repeated_trusted_payload,
            expected=provenance,
        )
        repeated_runtime_modules = _runtime_core_module_evidence(
            trusted_entrypoint_path=trusted_entrypoint_path,
            trusted_modules=repeated_provenance.trusted_modules,
            module_names=module_names,
        )
    except (IntegrationAttestationUnavailable, IntegrationEvidenceUnavailable) as exc:
        raise IntegrationEvidenceInvalid() from exc
    if (
        repeated_interpreter != interpreter
        or repeated_trusted_payload != trusted_payload
        or not _same_stat_identity(repeated_trusted_stat, trusted_stat)
        or repeated_runtime_modules != runtime_modules
    ):
        raise IntegrationEvidenceInvalid()


def verify_active_manifest_against_trusted_entrypoint(
    *,
    state_home: Path,
    data_home: Path,
    trusted_entrypoint_path: Path,
) -> VerifiedActiveManifest:
    candidate_entrypoint = _active_entrypoint_candidate_from_active_manifest(
        state_home=state_home,
        data_home=data_home,
    )
    first = verify_active_manifest_at(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=candidate_entrypoint,
    )
    trusted_ancestors = _trusted_entrypoint_ancestor_identities(
        trusted_entrypoint_path,
    )
    trusted_payload, trusted_stat = _trusted_entrypoint_bytes_and_stat(
        trusted_entrypoint_path,
    )
    provenance = _verify_trusted_core_provenance(
        trusted_entrypoint_path,
        first,
        trusted_payload,
    )
    release_payload = _file_bytes(first.active_release.entrypoint_path, mode=0o600)
    if (
        trusted_payload != release_payload
        or _sha256_bytes(release_payload) != first.active_release.entrypoint_sha256
    ):
        raise IntegrationEvidenceUnavailable()
    _before_trusted_entrypoint_recheck(trusted_entrypoint_path)
    try:
        repeated_trusted_ancestors = _trusted_entrypoint_ancestor_identities(
            trusted_entrypoint_path,
        )
        repeated_trusted_payload, repeated_trusted_stat = (
            _trusted_entrypoint_bytes_and_stat(trusted_entrypoint_path)
        )
    except IntegrationEvidenceUnavailable as exc:
        raise IntegrationEvidenceInvalid() from exc
    if (
        repeated_trusted_ancestors != trusted_ancestors
        or repeated_trusted_payload != trusted_payload
        or not _same_stat_identity(repeated_trusted_stat, trusted_stat)
    ):
        raise IntegrationEvidenceInvalid()
    second = verify_active_manifest_at(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=candidate_entrypoint,
    )
    if first != second:
        raise IntegrationEvidenceInvalid()
    try:
        _verify_trusted_core_provenance(
            trusted_entrypoint_path,
            second,
            repeated_trusted_payload,
            expected=provenance,
        )
    except IntegrationEvidenceUnavailable as exc:
        raise IntegrationEvidenceInvalid() from exc
    return second


def _verify_previous_schema2_manifest_for_upgrade(
    *,
    manifest_path: Path,
    state_home: Path,
    data_home: Path,
    manifest_payload: bytes | None = None,
) -> ActiveRelease:
    return _verify_manifest_contract(
        manifest_path=manifest_path,
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=None,
        expected_schema_version=2,
        expected_version=_PREVIOUS_SCHEMA2_VERSION,
        expected_dist_info_prefix=_PREVIOUS_SCHEMA2_DIST_INFO_PREFIX,
        expected_fields=_PREVIOUS_SCHEMA2_MANIFEST_FIELDS,
        require_bytecode_environment=False,
        allow_expected_runtime_bytecode=True,
        manifest_payload=manifest_payload,
    )


def verify_active_release(
    *,
    state_home: Path,
    data_home: Path,
    expected_entrypoint_path: Path,
) -> ActiveRelease:
    try:
        return _verify_manifest(
            manifest_path=state_home / "codex-usage" / "integration" / "active.json",
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=expected_entrypoint_path,
        )
    except IntegrationAttestationUnavailable:
        raise
    except Exception:
        raise _unavailable() from None


def _before_active_identity_recheck(_integration_fd: int) -> None:
    return None


def _fd_identity(fd: int) -> FileIdentity:
    item = os.fstat(fd)
    return FileIdentity(
        item.st_dev,
        item.st_ino,
        stat.S_IMODE(item.st_mode),
        gid=item.st_gid,
        uid=item.st_uid,
        ctime_ns=item.st_ctime_ns,
    )


def verify_active_manifest_at(
    *,
    state_home: Path,
    data_home: Path,
    expected_entrypoint_path: Path,
) -> VerifiedActiveManifest:
    state_fd = -1
    app_fd = -1
    integration_fd = -1
    try:
        try:
            state_fd = open_verified_state_home(state_home)
            state_identity = _fd_identity(state_fd)
            app_fd = open_private_dir_at(state_fd, "codex-usage")
            integration_fd = open_private_dir_at(app_fd, "integration")
            integration_identity = _fd_identity(integration_fd)
            active_payload, active_identity = read_private_bytes_at(
                integration_fd,
                "active.json",
                maximum=_MANIFEST_MAX_BYTES,
                mode=0o600,
            )
        except FileNotFoundError as exc:
            raise IntegrationEvidenceUnavailable() from exc
        except OSError as exc:
            raise IntegrationEvidenceUnavailable() from exc
        except ValueError as exc:
            raise IntegrationEvidenceInvalid() from exc

        try:
            active_release = _verify_manifest_contract(
                manifest_path=(
                    state_home / "codex-usage" / "integration" / "active.json"
                ),
                state_home=state_home,
                data_home=data_home,
                expected_entrypoint_path=expected_entrypoint_path,
                expected_schema_version=2,
                expected_version=_EXPECTED_VERSION,
                expected_dist_info_prefix=_DIST_INFO_PREFIX,
                expected_fields=_CURRENT_SCHEMA2_MANIFEST_FIELDS,
                manifest_payload=active_payload,
            )
            manifest = _manifest_from_canonical_bytes(active_payload)
            release_id = _manifest_string(manifest, "release_id")
            source_manifest_sha256 = _valid_hash(
                manifest.get("source_manifest_sha256")
            )
            release_evidence = _release_tree_evidence_at(
                integration_fd=integration_fd,
                release_id=release_id,
            )
            if (
                hashlib.sha256(b"".join(release_evidence.rows)).hexdigest()
                != active_release.release_tree_sha256
            ):
                raise _unavailable()
        except IntegrationAttestationUnavailable as exc:
            raise IntegrationEvidenceUnavailable() from exc
        except Exception as exc:
            raise IntegrationEvidenceUnavailable() from exc

        try:
            _before_active_identity_recheck(integration_fd)
            repeated_payload, repeated_identity = read_private_bytes_at(
                integration_fd,
                "active.json",
                maximum=_MANIFEST_MAX_BYTES,
                mode=0o600,
            )
            if (
                _fd_identity(state_fd) != state_identity
                or _fd_identity(integration_fd) != integration_identity
                or repeated_identity != active_identity
                or repeated_payload != active_payload
            ):
                raise IntegrationEvidenceInvalid()
            if (
                _release_tree_evidence_at(
                    integration_fd=integration_fd,
                    release_id=release_id,
                )
                != release_evidence
            ):
                raise IntegrationEvidenceInvalid()

            fresh_state_fd = open_verified_state_home(state_home)
            fresh_app_fd = -1
            fresh_integration_fd = -1
            try:
                fresh_app_fd = open_private_dir_at(fresh_state_fd, "codex-usage")
                fresh_integration_fd = open_private_dir_at(
                    fresh_app_fd,
                    "integration",
                )
                fresh_payload, fresh_active_identity = read_private_bytes_at(
                    fresh_integration_fd,
                    "active.json",
                    maximum=_MANIFEST_MAX_BYTES,
                    mode=0o600,
                )
                if (
                    _fd_identity(fresh_state_fd) != state_identity
                    or _fd_identity(fresh_integration_fd) != integration_identity
                    or fresh_active_identity != active_identity
                    or fresh_payload != active_payload
                    or _release_tree_evidence_at(
                        integration_fd=fresh_integration_fd,
                        release_id=release_id,
                    )
                    != release_evidence
                ):
                    raise IntegrationEvidenceInvalid()
            finally:
                if fresh_integration_fd >= 0:
                    os.close(fresh_integration_fd)
                if fresh_app_fd >= 0:
                    os.close(fresh_app_fd)
                os.close(fresh_state_fd)
        except IntegrationEvidenceInvalid:
            raise
        except Exception as exc:
            raise IntegrationEvidenceInvalid() from exc

        return VerifiedActiveManifest(
            active_release=active_release,
            release_id=release_id,
            source_manifest_sha256=source_manifest_sha256,
            active_manifest_bytes=active_payload,
            active_manifest_sha256=_sha256_bytes(active_payload),
            state_home_identity=state_identity,
            integration_parent_identity=integration_identity,
            active_file_identity=active_identity,
        )
    finally:
        if integration_fd >= 0:
            os.close(integration_fd)
        if app_fd >= 0:
            os.close(app_fd)
        if state_fd >= 0:
            os.close(state_fd)
