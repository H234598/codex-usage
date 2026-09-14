from __future__ import annotations

import hashlib
import inspect
import json
import multiprocessing.spawn
import multiprocessing.util
import os
import posixpath
import shlex
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

import codex_usage.private_io as private_io

_REAL_SCANDIR = os.scandir
_REAL_SUBPROCESS_POPEN = subprocess.Popen
_REAL_LSTAT = os.lstat
_REAL_OPEN = os.open
_REAL_FSTAT = os.fstat
_REAL_STAT = os.stat
_REAL_LISTDIR = os.listdir
_REAL_READLINK = os.readlink
_REAL_READ = os.read
_REAL_CLOSE = os.close
_REAL_SYSTEM = os.system
_REAL_POPEN = os.popen
_REAL_POSIX_SPAWN = getattr(os, "posix_spawn", None)
_REAL_POSIX_SPAWNP = getattr(os, "posix_spawnp", None)
_REAL_EXECV = os.execv
_REAL_EXECVE = os.execve
_REAL_EXECVP = os.execvp
_REAL_EXECVPE = os.execvpe
_REAL_SPAWNV = os.spawnv
_REAL_SPAWNVE = os.spawnve
_REAL_SPAWNVP = os.spawnvp
_REAL_SPAWNVPE = os.spawnvpe
_LOCK_GUARD_MAX_REGULAR_BYTES = 4096
_BWRAP_ARGS_FD_MAX_BYTES = 65536
_BWRAP_PAIR_MOUNT_OPTIONS = frozenset(
    (
        "--bind",
        "--bind-try",
        "--dev-bind",
        "--dev-bind-try",
        "--ro-bind",
        "--ro-bind-try",
    )
)
_BWRAP_FD_DESTINATION_MOUNT_OPTIONS = frozenset(
    (
        "--bind-data",
        "--bind-fd",
        "--file",
        "--ro-bind-data",
        "--ro-bind-fd",
    )
)
_BWRAP_TRIPLE_DESTINATION_MOUNT_OPTIONS = frozenset(("--overlay",))
_BWRAP_DESTINATION_MOUNT_OPTIONS = frozenset(
    (
        "--dev",
        "--dir",
        "--mqueue",
        "--proc",
        "--remount-ro",
        "--ro-overlay",
        "--tmp-overlay",
        "--tmpfs",
    )
)
_BWRAP_ENV_LOCK_ROOT_NAMES = frozenset(("HOME", "XDG_STATE_HOME"))
_FORBIDDEN_DYNAMIC_LOADER_ENV_NAMES = frozenset(
    (
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "LD_AUDIT",
        "LD_BIND_NOW",
        "LD_DEBUG",
        "LD_LIBRARY_PATH",
        "LD_ORIGIN_PATH",
        "LD_PRELOAD",
        "PYTHONEXECUTABLE",
    )
)
_FORBIDDEN_PYTHON_LOADER_ENV_NAMES = frozenset(
    (
        "PYTHONEXECUTABLE",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSAFEPATH",
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
    )
)
_FORBIDDEN_LOADER_ENV_NAMES = (
    _FORBIDDEN_DYNAMIC_LOADER_ENV_NAMES | _FORBIDDEN_PYTHON_LOADER_ENV_NAMES
)
_BWRAP_NO_ARGUMENT_OPTIONS = frozenset(
    (
        "--as-pid-1",
        "--assert-userns-disabled",
        "--clearenv",
        "--die-with-parent",
        "--disable-userns",
        "--help",
        "--level-prefix",
        "--new-session",
        "--share-net",
        "--unshare-all",
        "--unshare-cgroup",
        "--unshare-cgroup-try",
        "--unshare-ipc",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-user",
        "--unshare-user-try",
        "--unshare-uts",
        "--version",
    )
)
_BWRAP_SINGLE_ARGUMENT_OPTIONS = frozenset(
    (
        "--add-seccomp-fd",
        "--argv0",
        "--block-fd",
        "--cap-add",
        "--cap-drop",
        "--chdir",
        "--exec-label",
        "--file-label",
        "--gid",
        "--hostname",
        "--info-fd",
        "--json-status-fd",
        "--perms",
        "--pidns",
        "--seccomp",
        "--size",
        "--sync-fd",
        "--uid",
        "--userns",
        "--userns-block-fd",
        "--userns2",
    )
)
_BWRAP_SINGLE_PATH_TOUCH_OPTIONS = frozenset(("--lock-file",))
_BWRAP_DOUBLE_PATH_TOUCH_OPTIONS = frozenset(("--chmod",))
_BWRAP_EXECUTABLE_PATH = "/usr/bin/bwrap"
_BWRAP_OVERFLOW_UID = 65534
_BWRAP_EXECUTABLE_MAX_BYTES = 16 * 1024 * 1024
_OUTER_LOCK_PROOF_FORMAT = "codex-usage-cycle31-outer-lock-isolation-v1"
_OUTER_LOCK_PROOF_NAME = ".cycle31-outer-proof.json"
_OUTER_LOCK_PROOF_MAX_BYTES = 16 * 1024
_OUTER_LOCK_PRODUCTION_ROOT = Path("/home/teladi/.local/state/codex-usage/locks")
_OUTER_LOCK_PROOF_ENV_NAMES = (
    "CODEX_USAGE_TEST_OUTER_LOCK_PARENT_MNT_NS",
    "CODEX_USAGE_TEST_OUTER_LOCK_PARENT_USER_NS",
    "CODEX_USAGE_TEST_OUTER_LOCK_PRODUCTION_ROOT",
    "CODEX_USAGE_TEST_OUTER_LOCK_PROOF",
    "CODEX_USAGE_TEST_OUTER_LOCK_PROOF_SHA256",
    "CODEX_USAGE_TEST_OUTER_LOCK_SYNTHETIC_ROOT",
)
_OUTER_LOCK_REQUIRED_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "PYTHONSAFEPATH": "1",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
}
_PROC_SELF_UID_MAP_MAX_BYTES = 4096
_PROC_SELF_UID_MAP = Path("/proc/self/uid_map")
_PROC_SELF_MOUNTINFO = Path("/proc/self/mountinfo")
_PROC_SELF_NAMESPACE_PATHS = ("/proc/self/ns/user", "/proc/self/ns/mnt")
_POPEN_SIGNATURE = inspect.signature(subprocess.Popen)
_POPEN_POSITIONAL_PARAMETER_NAMES = tuple(
    name
    for name, parameter in _POPEN_SIGNATURE.parameters.items()
    if parameter.kind
    in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
)


@dataclass(frozen=True)
class _BwrapOverflowOwnerBinding:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str
    namespaces: tuple[str, ...]


@dataclass(frozen=True)
class _OuterLockIsolationProof:
    production_root: Path
    synthetic_root: Path
    proof_path: Path
    proof_sha256: str
    parent_namespaces: tuple[str, str]
    current_namespaces: tuple[str, str]
    synthetic_root_identity: _LockEntrySnapshot
    hidden_production_root_identity: dict[str, object]
    proof_identity: _LockEntrySnapshot


@dataclass(frozen=True)
class _LockIsolationSyscalls:
    scandir: Callable[..., object]
    lstat: Callable[..., os.stat_result]
    open: Callable[..., int]
    fstat: Callable[..., os.stat_result]
    stat: Callable[..., os.stat_result]
    listdir: Callable[..., object]
    readlink: Callable[..., str]
    read: Callable[..., bytes]
    close: Callable[[int], None]


def _lock_isolation_syscalls_from_module() -> _LockIsolationSyscalls:
    return _LockIsolationSyscalls(
        scandir=_REAL_SCANDIR,
        lstat=_REAL_LSTAT,
        open=_REAL_OPEN,
        fstat=_REAL_FSTAT,
        stat=_REAL_STAT,
        listdir=_REAL_LISTDIR,
        readlink=_REAL_READLINK,
        read=_REAL_READ,
        close=_REAL_CLOSE,
    )


@dataclass(frozen=True)
class _LockEntrySnapshot:
    name: str
    file_type: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    mtime_ns: int | None
    ctime_ns: int | None
    symlink_target: str | None = None
    content_sha256: str | None = None


@dataclass(frozen=True)
class _LockNamespaceSnapshot:
    root: _LockEntrySnapshot | None
    root_identities: tuple[_LockEntrySnapshot, ...]
    entries: tuple[_LockEntrySnapshot, ...]
    lock_root: Path | None = None
    include_times: bool = True
    approval_hash: str = ""
    approval_report: private_io.PrivateLockNamespaceReport | None = None


@dataclass
class _LockNamespaceGuard:
    production_root: Path
    _component_names: tuple[str, ...]
    _component_fds: tuple[int, ...]
    _syscalls: _LockIsolationSyscalls
    before: _LockNamespaceSnapshot
    allow_root_time_drift: bool = False
    _closed: bool = False

    def assert_unchanged(self, *, label: str) -> None:
        if self._closed:
            raise AssertionError(f"{label} guard is already closed")
        _assert_open_lock_namespace_guard_unchanged(self, label=label)

    def close(self) -> None:
        if self._closed:
            return
        errors: list[OSError] = []
        for fd in reversed(self._component_fds):
            try:
                self._syscalls.close(fd)
            except OSError as exc:
                errors.append(exc)
        self._closed = True
        if not errors:
            return
        if len(errors) == 1:
            raise errors[0]
        raise ExceptionGroup("could not close lock isolation guard descriptors", errors)


def _lock_names(root: Path) -> frozenset[str]:
    try:
        with _REAL_SCANDIR(root) as entries:
            return frozenset(entry.name for entry in entries)
    except FileNotFoundError:
        return frozenset()


def _lock_file_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _user_owned_directory_open_flags() -> int:
    return _directory_open_flags() | getattr(os, "O_NOATIME", 0)


def _regular_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOATIME", 0)
    )


def _regular_content_sha256_at(
    root_fd: int,
    name: str,
    expected: os.stat_result,
    *,
    syscalls: _LockIsolationSyscalls,
) -> str | None:
    if not stat.S_ISREG(expected.st_mode):
        return None
    if expected.st_size > _LOCK_GUARD_MAX_REGULAR_BYTES:
        raise AssertionError("lock isolation regular entry exceeds byte budget")
    fd = -1
    try:
        fd = syscalls.open(name, _regular_open_flags(), dir_fd=root_fd)
        initial = syscalls.fstat(fd)
        if (
            initial.st_dev != expected.st_dev
            or initial.st_ino != expected.st_ino
            or initial.st_mode != expected.st_mode
            or initial.st_uid != expected.st_uid
            or initial.st_gid != expected.st_gid
            or initial.st_nlink != expected.st_nlink
            or initial.st_size != expected.st_size
            or initial.st_mtime_ns != expected.st_mtime_ns
            or initial.st_ctime_ns != expected.st_ctime_ns
        ):
            raise AssertionError("lock isolation regular entry changed during open")
        payload = bytearray()
        while len(payload) <= _LOCK_GUARD_MAX_REGULAR_BYTES:
            chunk = syscalls.read(
                fd,
                min(65_536, _LOCK_GUARD_MAX_REGULAR_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _LOCK_GUARD_MAX_REGULAR_BYTES:
            raise AssertionError("lock isolation regular entry exceeds byte budget")
        final = syscalls.fstat(fd)
        if (
            final.st_dev != initial.st_dev
            or final.st_ino != initial.st_ino
            or final.st_mode != initial.st_mode
            or final.st_uid != initial.st_uid
            or final.st_gid != initial.st_gid
            or final.st_nlink != initial.st_nlink
            or final.st_size != initial.st_size
            or final.st_mtime_ns != initial.st_mtime_ns
            or final.st_ctime_ns != initial.st_ctime_ns
        ):
            raise AssertionError("lock isolation regular entry changed during read")
        return hashlib.sha256(bytes(payload)).hexdigest()
    finally:
        if fd >= 0:
            syscalls.close(fd)


def _entry_snapshot_from_stat(
    item: os.stat_result,
    *,
    name: str,
    include_times: bool = True,
    symlink_target: str | None = None,
    content_sha256: str | None = None,
) -> _LockEntrySnapshot:
    return _LockEntrySnapshot(
        name=name,
        file_type=_lock_file_type(item.st_mode),
        device=item.st_dev,
        inode=item.st_ino,
        mode=item.st_mode,
        uid=item.st_uid,
        gid=item.st_gid,
        nlink=item.st_nlink,
        size=item.st_size,
        mtime_ns=item.st_mtime_ns if include_times else None,
        ctime_ns=item.st_ctime_ns if include_times else None,
        symlink_target=symlink_target,
        content_sha256=content_sha256,
    )


def _private_lock_snapshot_from_entry(
    entry: _LockEntrySnapshot,
) -> private_io.PrivateLockSnapshot:
    if entry.mtime_ns is None or entry.ctime_ns is None:
        raise AssertionError("lock isolation approval requires timestamp evidence")
    return private_io.PrivateLockSnapshot(
        file_type=entry.file_type,
        device=entry.device,
        inode=entry.inode,
        mode=entry.mode,
        uid=entry.uid,
        gid=entry.gid,
        nlink=entry.nlink,
        size=entry.size,
        mtime_ns=entry.mtime_ns,
        ctime_ns=entry.ctime_ns,
        symlink_target=entry.symlink_target,
        content_sha256=entry.content_sha256,
    )


def _private_lock_report_from_guard_snapshot(
    *,
    lock_root: Path,
    root_snapshot: _LockEntrySnapshot,
    entries: tuple[_LockEntrySnapshot, ...],
) -> private_io.PrivateLockNamespaceReport:
    def valid_private_lock_snapshot(snapshot: private_io.PrivateLockSnapshot) -> bool:
        return (
            snapshot.file_type == "regular"
            and snapshot.uid == root_snapshot.uid
            and stat.S_IMODE(snapshot.mode) == 0o600
            and snapshot.nlink == 1
            and snapshot.size <= private_io._PRIVATE_LOCK_MAX_BYTES
        )

    def valid_empty_private_lock_snapshot(
        snapshot: private_io.PrivateLockSnapshot,
    ) -> bool:
        return valid_private_lock_snapshot(snapshot) and snapshot.size == 0

    root = _private_lock_snapshot_from_entry(root_snapshot)
    by_name = {
        entry.name: _private_lock_snapshot_from_entry(entry) for entry in entries
    }
    issues: list[private_io.PrivateLockNamespaceIssue] = []
    for entry in entries:
        snapshot = by_name[entry.name]
        if (
            private_io._CANONICAL_LOCK_NAME_RE.fullmatch(entry.name)
            and valid_private_lock_snapshot(snapshot)
        ):
            continue
        moved = private_io._MOVED_LOCK_NAME_RE.fullmatch(entry.name)
        if moved is not None and valid_empty_private_lock_snapshot(snapshot):
            canonical_sibling_name = moved.group(1)
            sibling = by_name.get(canonical_sibling_name)
            if sibling is not None and valid_private_lock_snapshot(sibling):
                issues.append(
                    private_io.PrivateLockNamespaceIssue(
                        name=entry.name,
                        reason="approved-empty-moved-lock",
                        snapshot=snapshot,
                        canonical_sibling_name=canonical_sibling_name,
                    )
                )
                continue
        issues.append(
            private_io.PrivateLockNamespaceIssue(
                name=entry.name,
                reason="unsupported-private-lock-residue",
                snapshot=snapshot,
            )
        )
    return private_io.PrivateLockNamespaceReport(
        lock_root=lock_root,
        root_snapshot=root,
        issues=tuple(issues),
    )


def _lock_namespace_approval_payload(
    snapshot: _LockNamespaceSnapshot,
) -> dict[str, object]:
    def entry_payload(entry: _LockEntrySnapshot) -> dict[str, object]:
        return {
            "content_sha256": entry.content_sha256,
            "ctime_ns": entry.ctime_ns,
            "device": entry.device,
            "file_type": entry.file_type,
            "gid": entry.gid,
            "inode": entry.inode,
            "mode": entry.mode,
            "mtime_ns": entry.mtime_ns,
            "name": entry.name,
            "nlink": entry.nlink,
            "size": entry.size,
            "symlink_target": entry.symlink_target,
            "uid": entry.uid,
        }

    return {
        "entries": [entry_payload(entry) for entry in snapshot.entries],
        "include_times": snapshot.include_times,
        "root": None if snapshot.root is None else entry_payload(snapshot.root),
        "root_identities": [
            entry_payload(identity) for identity in snapshot.root_identities
        ],
    }


def _lock_namespace_approval_hash(snapshot: _LockNamespaceSnapshot) -> str:
    if snapshot.approval_report is not None:
        return private_io.private_lock_namespace_approval_hash(
            snapshot.approval_report,
        )
    if snapshot.root is not None and snapshot.lock_root is not None:
        raise AssertionError("lock isolation approval requires productive evidence")
    canonical = json.dumps(
        _lock_namespace_approval_payload(snapshot),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _with_lock_namespace_approval(
    snapshot: _LockNamespaceSnapshot,
) -> _LockNamespaceSnapshot:
    return _LockNamespaceSnapshot(
        root=snapshot.root,
        root_identities=snapshot.root_identities,
        entries=snapshot.entries,
        lock_root=snapshot.lock_root,
        include_times=snapshot.include_times,
        approval_report=snapshot.approval_report,
        approval_hash=_lock_namespace_approval_hash(snapshot),
    )


def _bounded_lock_namespace_entry_names(
    root_fd: int,
    *,
    syscalls: _LockIsolationSyscalls,
) -> tuple[str, ...]:
    names: list[str] = []
    with syscalls.scandir(root_fd) as entries:
        for entry in entries:
            if len(names) >= private_io._PRIVATE_LOCK_NAMESPACE_MAX_ENTRIES:
                raise AssertionError("lock isolation namespace has too many entries")
            names.append(entry.name)
    return tuple(sorted(names))


def _open_lock_namespace_components(
    root: Path,
    *,
    include_times: bool,
    syscalls: _LockIsolationSyscalls,
) -> tuple[tuple[str, ...], tuple[int, ...], tuple[_LockEntrySnapshot, ...]]:
    absolute = root if root.is_absolute() else Path.cwd() / root
    if any(part in {"", ".", ".."} for part in absolute.parts[1:]):
        raise ValueError("lock isolation root must be normalized")
    flags = _directory_open_flags()
    component_names: list[str] = [absolute.anchor]
    component_fds: list[int] = []
    identities: list[_LockEntrySnapshot] = []
    try:
        current_fd = syscalls.open(absolute.anchor, flags)
        component_fds.append(current_fd)
        identities.append(
            _entry_snapshot_from_stat(
                syscalls.fstat(current_fd),
                name=absolute.anchor,
                include_times=include_times,
            )
        )
        components = absolute.parts[1:]
        for index, component in enumerate(components):
            open_flags = (
                _user_owned_directory_open_flags()
                if index == len(components) - 1
                else flags
            )
            next_fd = syscalls.open(component, open_flags, dir_fd=current_fd)
            component_names.append(component)
            component_fds.append(next_fd)
            current_fd = next_fd
            identities.append(
                _entry_snapshot_from_stat(
                    syscalls.fstat(current_fd),
                    name=component,
                    include_times=include_times,
                )
            )
        return tuple(component_names), tuple(component_fds), tuple(identities)
    except Exception:
        for fd in reversed(component_fds):
            try:
                syscalls.close(fd)
            except OSError:
                pass
        raise


def _snapshot_open_lock_namespace(
    *,
    lock_root: Path,
    root_fd: int,
    root_identities: tuple[_LockEntrySnapshot, ...],
    include_times: bool,
    syscalls: _LockIsolationSyscalls,
) -> _LockNamespaceSnapshot:
    root_snapshot = _entry_snapshot_from_stat(
        syscalls.fstat(root_fd),
        name=".",
        include_times=include_times,
    )
    normalized_identities = (
        (*root_identities[:-1], root_snapshot)
        if root_identities
        else (root_snapshot,)
    )
    entries: list[_LockEntrySnapshot] = []
    for name in _bounded_lock_namespace_entry_names(root_fd, syscalls=syscalls):
        item = syscalls.stat(name, dir_fd=root_fd, follow_symlinks=False)
        symlink_target = (
            syscalls.readlink(name, dir_fd=root_fd)
            if stat.S_ISLNK(item.st_mode)
            else None
        )
        content_sha256 = _regular_content_sha256_at(
            root_fd,
            name,
            item,
            syscalls=syscalls,
        )
        entries.append(
            _entry_snapshot_from_stat(
                item,
                name=name,
                include_times=include_times,
                symlink_target=symlink_target,
                content_sha256=content_sha256,
            )
        )
    report = _private_lock_report_from_guard_snapshot(
        lock_root=lock_root,
        root_snapshot=root_snapshot,
        entries=tuple(entries),
    )
    return _with_lock_namespace_approval(
        _LockNamespaceSnapshot(
            root=root_snapshot,
            root_identities=normalized_identities,
            entries=tuple(entries),
            lock_root=lock_root,
            include_times=include_times,
            approval_report=report,
        )
    )


def _open_lock_namespace_guard(
    root: Path,
    *,
    label: str,
    include_times: bool = True,
    allow_root_time_drift: bool = False,
) -> _LockNamespaceGuard:
    del label
    syscalls = _lock_isolation_syscalls_from_module()
    component_names, component_fds, root_identities = _open_lock_namespace_components(
        root,
        include_times=include_times,
        syscalls=syscalls,
    )
    lock_root = root if root.is_absolute() else Path.cwd() / root
    snapshot = _snapshot_open_lock_namespace(
        lock_root=lock_root,
        root_fd=component_fds[-1],
        root_identities=root_identities,
        include_times=include_times,
        syscalls=syscalls,
    )
    return _LockNamespaceGuard(
        production_root=lock_root,
        _component_names=component_names,
        _component_fds=component_fds,
        _syscalls=syscalls,
        before=snapshot,
        allow_root_time_drift=allow_root_time_drift,
    )


def _lock_namespace_snapshot(
    root: Path,
    *,
    include_times: bool = True,
) -> _LockNamespaceSnapshot:
    guard: _LockNamespaceGuard | None = None
    try:
        guard = _open_lock_namespace_guard(
            root,
            label="lock isolation root",
            include_times=include_times,
        )
    except FileNotFoundError:
        return _with_lock_namespace_approval(
            _LockNamespaceSnapshot(
                root=None,
                root_identities=(),
                entries=(),
                lock_root=root if root.is_absolute() else Path.cwd() / root,
                include_times=include_times,
                approval_report=None,
            )
        )
    try:
        return guard.before
    finally:
        guard.close()


def _assert_snapshot_approval(snapshot: _LockNamespaceSnapshot, *, label: str) -> None:
    if snapshot.approval_hash != _lock_namespace_approval_hash(snapshot):
        raise AssertionError(f"{label} approval hash changed")


def _entry_identity_without_times(entry: _LockEntrySnapshot) -> tuple[object, ...]:
    return (
        entry.name,
        entry.file_type,
        entry.device,
        entry.inode,
        entry.mode,
        entry.uid,
        entry.gid,
        entry.nlink,
        entry.size,
        entry.symlink_target,
        entry.content_sha256,
    )


def _same_entry_except_times(
    left: _LockEntrySnapshot,
    right: _LockEntrySnapshot,
) -> bool:
    return _entry_identity_without_times(left) == _entry_identity_without_times(right)


def _raise_snapshot_changed(
    *,
    label: str,
    before: _LockNamespaceSnapshot,
    after: _LockNamespaceSnapshot,
) -> None:
    before_names = {entry.name for entry in before.entries}
    after_names = {entry.name for entry in after.entries}
    added = sorted(after_names - before_names)
    deleted = sorted(before_names - after_names)
    changed = sorted(
        entry.name
        for entry in after.entries
        if entry.name in before_names
        and entry
        != next(previous for previous in before.entries if previous.name == entry.name)
    )
    root_changed = before.root != after.root or before.root_identities != (
        after.root_identities
    )
    if root_changed:
        before_roots = (() if before.root is None else (before.root,)) + (
            before.root_identities
        )
        after_roots = (() if after.root is None else (after.root,)) + (
            after.root_identities
        )
        root_time_only = len(before_roots) == len(after_roots) and all(
            _same_entry_except_times(left, right)
            for left, right in zip(before_roots, after_roots, strict=True)
        )
        if root_time_only and not added and not deleted and not changed:
            raise AssertionError(
                f"{label}: external host lock namespace drift; "
                f"added=[]; deleted=[]; changed=['.']"
            )
        changed.insert(0, ".")
    if added or deleted or any(name != "." for name in changed):
        raise AssertionError(
            f"{label}: external host lock namespace drift; "
            f"added={added[:8]}; deleted={deleted[:8]}; changed={changed[:8]}"
        )
    raise AssertionError(
        f"{label} changed; added={added[:8]}; "
        f"deleted={deleted[:8]}; changed={changed[:8]}"
    )


def _assert_open_lock_namespace_guard_unchanged(
    guard: _LockNamespaceGuard,
    *,
    label: str,
) -> None:
    before = guard.before
    syscalls = guard._syscalls
    _assert_snapshot_approval(before, label=label)
    if len(guard._component_fds) != len(before.root_identities):
        raise AssertionError(f"{label} root changed")
    for index, fd in enumerate(guard._component_fds):
        expected = before.root_identities[index]
        current = _entry_snapshot_from_stat(
            syscalls.fstat(fd),
            name=expected.name,
            include_times=before.include_times,
        )
        if current != expected and not (
            guard.allow_root_time_drift
            and _same_entry_except_times(expected, current)
        ):
            raise AssertionError(
                f"{label}: external host lock namespace drift; changed=['{expected.name}']"
            )
        if index == 0:
            continue
        named_fd = -1
        try:
            named_fd = syscalls.open(
                guard._component_names[index],
                _directory_open_flags(),
                dir_fd=guard._component_fds[index - 1],
            )
            named = _entry_snapshot_from_stat(
                syscalls.fstat(named_fd),
                name=expected.name,
                include_times=before.include_times,
            )
            if named != expected and not (
                guard.allow_root_time_drift
                and _same_entry_except_times(expected, named)
            ):
                raise AssertionError(
                    f"{label}: external host lock namespace drift; changed=['{expected.name}']"
                )
        except FileNotFoundError as exc:
            raise AssertionError(f"{label}: external host lock namespace rebind") from exc
        finally:
            if named_fd >= 0:
                syscalls.close(named_fd)
    after = _snapshot_open_lock_namespace(
        lock_root=guard.production_root,
        root_fd=guard._component_fds[-1],
        root_identities=before.root_identities,
        include_times=before.include_times,
        syscalls=syscalls,
    )
    if after != before:
        if guard.allow_root_time_drift:
            before_names = {entry.name for entry in before.entries}
            after_names = {entry.name for entry in after.entries}
            added = after_names - before_names
            deleted = before_names - after_names
            changed = {
                entry.name
                for entry in after.entries
                if entry.name in before_names
                and entry
                != next(
                    previous for previous in before.entries if previous.name == entry.name
                )
            }
            before_roots = (() if before.root is None else (before.root,)) + (
                before.root_identities
            )
            after_roots = (() if after.root is None else (after.root,)) + (
                after.root_identities
            )
            root_time_only = len(before_roots) == len(after_roots) and all(
                _same_entry_except_times(left, right)
                for left, right in zip(before_roots, after_roots, strict=True)
            )
            if root_time_only and not added and not deleted and not changed:
                return
        _raise_snapshot_changed(label=label, before=before, after=after)


def _assert_lock_namespace_unchanged(
    root: Path,
    before: _LockNamespaceSnapshot,
    *,
    label: str,
) -> None:
    _assert_snapshot_approval(before, label=label)
    after = _lock_namespace_snapshot(root, include_times=before.include_times)
    if after != before:
        if before.root is not None and after.root is None:
            raise AssertionError(f"{label} root changed")
        _raise_snapshot_changed(label=label, before=before, after=after)


def _path_basename(value: object) -> str:
    try:
        raw = os.fspath(value)
    except TypeError:
        return ""
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    return Path(raw).name


def _token_text(value: object) -> str:
    try:
        raw = os.fspath(value)
    except TypeError:
        return ""
    if isinstance(raw, bytes):
        return os.fsdecode(raw)
    return str(raw)


def _bwrap_fd_number(bwrap_fd_path: str) -> int | None:
    prefix = "/proc/self/fd/"
    if not bwrap_fd_path.startswith(prefix):
        return None
    try:
        return int(bwrap_fd_path[len(prefix) :])
    except ValueError:
        return None


def _read_bwrap_args_fd(
    fd_token: str,
    *,
    bwrap_fd_path: str,
    seen_fds: frozenset[int],
) -> tuple[str, ...]:
    try:
        fd = int(fd_token, 10)
    except ValueError as exc:
        raise ValueError("bwrap --args FD is invalid") from exc
    if fd < 0:
        raise ValueError("bwrap --args FD is invalid")
    bwrap_fd = _bwrap_fd_number(bwrap_fd_path)
    if fd == bwrap_fd or fd in seen_fds:
        raise ValueError("bwrap --args FD reuse is invalid")
    try:
        item = _REAL_FSTAT(fd)
    except OSError as exc:
        raise ValueError("bwrap --args FD is unreadable") from exc
    if not stat.S_ISREG(item.st_mode):
        raise ValueError("bwrap --args FD must be a regular file")
    if item.st_size > _BWRAP_ARGS_FD_MAX_BYTES:
        raise ValueError("bwrap --args FD is too large")
    try:
        raw = os.pread(fd, _BWRAP_ARGS_FD_MAX_BYTES + 1, 0)
    except OSError as exc:
        raise ValueError("bwrap --args FD is unreadable") from exc
    if len(raw) > _BWRAP_ARGS_FD_MAX_BYTES:
        raise ValueError("bwrap --args FD is too large")
    if not raw:
        return ()
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    tokens: list[str] = []
    for part in parts:
        if part == b"":
            raise ValueError("bwrap --args FD contains an empty argument")
        try:
            token = part.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("bwrap --args FD is malformed") from exc
        if token == "--":
            raise ValueError("bwrap --args FD must not contain command boundary")
        tokens.append(token)
    return tuple(tokens)


def _expand_bwrap_args_fds(
    prefix: tuple[str, ...],
    *,
    bwrap_fd_path: str,
    seen_fds: frozenset[int] = frozenset(),
) -> tuple[str, ...]:
    expanded: list[str] = []
    index = 0
    while index < len(prefix):
        token = prefix[index]
        if token != "--args":
            expanded.append(token)
            index += 1
            continue
        if not _require_bwrap_arity(prefix, index, 1):
            raise ValueError("bwrap --args option arity is invalid")
        fd_token = prefix[index + 1]
        hidden = _read_bwrap_args_fd(
            fd_token,
            bwrap_fd_path=bwrap_fd_path,
            seen_fds=seen_fds,
        )
        hidden_fd = int(fd_token, 10)
        expanded.extend(
            _expand_bwrap_args_fds(
                hidden,
                bwrap_fd_path=bwrap_fd_path,
                seen_fds=seen_fds | frozenset((hidden_fd,)),
            )
        )
        index += 2
    return tuple(expanded)


def _contains_forbidden_loader_env(
    env: object,
    *,
    allow_python_loader_env: bool = False,
) -> bool:
    if env is None:
        return False
    if not isinstance(env, Mapping):
        return False
    forbidden = (
        _FORBIDDEN_DYNAMIC_LOADER_ENV_NAMES
        if allow_python_loader_env
        else _FORBIDDEN_LOADER_ENV_NAMES
    )
    for name, value in env.items():
        text = _token_text(name)
        if text not in forbidden:
            continue
        if text == "PYTHONSAFEPATH" and _token_text(value) == "1":
            continue
        return True
    return False


def _sanitized_process_env() -> dict[str, str] | None:
    if not os.environ:
        return None
    return {
        key: value
        for key, value in os.environ.items()
        if key not in _FORBIDDEN_LOADER_ENV_NAMES
    }


def _process_fd_path_number(value: object) -> int | None:
    text = _token_text(value)
    prefix = "/proc/self/fd/"
    if not text.startswith(prefix):
        return None
    fd_text = text[len(prefix) :]
    try:
        fd = int(fd_text, 10)
    except ValueError:
        return None
    if fd_text != str(fd) or fd < 0:
        return None
    return fd


def _validate_regular_executable_fd(fd: int, *, bwrap_pass_fd: int) -> None:
    if fd == bwrap_pass_fd:
        raise ValueError("lock isolation subprocess pass_fds is not allowed")
    try:
        item = _REAL_FSTAT(fd)
    except OSError as exc:
        raise ValueError("lock isolation subprocess pass_fds is not allowed") from exc
    mode = stat.S_IMODE(item.st_mode)
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_nlink < 1
        or not _bwrap_owner_is_trusted(item)
        or mode & 0o022
    ):
        raise ValueError("lock isolation subprocess pass_fds is not allowed")


def _allowed_executable_pass_fds(
    *,
    args: object,
    tokens: tuple[object, ...],
    executable: object,
    shell: bool,
    bwrap_fd_path: str,
    bwrap_pass_fd: int,
) -> tuple[int, ...]:
    if shell:
        return ()
    if tokens and _token_text(tokens[0]) == bwrap_fd_path:
        return ()
    executable_path = executable if executable is not None else (tokens[0] if tokens else args)
    fd = _process_fd_path_number(executable_path)
    if fd is None:
        return ()
    _validate_regular_executable_fd(fd, bwrap_pass_fd=bwrap_pass_fd)
    return (fd,)


def _validate_lock_isolation_process_kwargs(
    kwargs: dict[str, object],
    *,
    allowed_pass_fds: tuple[int, ...] = (),
    allow_python_loader_env: bool = False,
) -> tuple[int, ...]:
    if kwargs.get("preexec_fn") is not None:
        raise ValueError("lock isolation subprocess preexec_fn is not allowed")
    pass_fds = tuple(kwargs.get("pass_fds") or ())
    if any(type(fd) is not int for fd in pass_fds):
        raise ValueError("lock isolation subprocess pass_fds is not allowed")
    if any(fd not in allowed_pass_fds for fd in pass_fds):
        raise ValueError("lock isolation subprocess pass_fds is not allowed")
    if _contains_forbidden_loader_env(
        kwargs.get("env"),
        allow_python_loader_env=allow_python_loader_env,
    ):
        raise ValueError("lock isolation subprocess environment is not allowed")
    return pass_fds


def _explicit_bwrap_command(tokens: tuple[object, ...]) -> bool:
    return bool(tokens) and _path_basename(tokens[0]) == "bwrap"


def _prepare_lock_isolation_process_env(kwargs: dict[str, object]) -> None:
    if "env" not in kwargs or kwargs["env"] is None:
        sanitized = _sanitized_process_env()
        if sanitized is not None:
            kwargs["env"] = sanitized


def _validate_lock_isolation_posix_spawn_kwargs(kwargs: dict[str, object]) -> None:
    if kwargs.get("file_actions"):
        raise ValueError("lock isolation posix_spawn file_actions are not allowed")


def _validate_lock_isolation_env(env: object) -> None:
    if _contains_forbidden_loader_env(env):
        raise ValueError("lock isolation process environment is not allowed")


def _merge_pass_fds(existing: object, bwrap_pass_fd: int) -> tuple[int, ...]:
    merged: list[int] = []
    if existing is not None:
        for fd in tuple(existing):  # type: ignore[arg-type]
            if type(fd) is int and fd not in merged:
                merged.append(fd)
    if bwrap_pass_fd not in merged:
        merged.append(bwrap_pass_fd)
    return tuple(merged)


def _bwrap_prefix(
    *,
    production_root: Path,
    test_root: Path,
    bwrap_fd_path: str,
) -> tuple[object, ...]:
    return (
        bwrap_fd_path,
        "--bind",
        "/",
        "/",
        "--bind",
        str(test_root),
        str(production_root),
        "--",
    )


def _bwrap_prefix_with_argv0(
    *,
    production_root: Path,
    test_root: Path,
    bwrap_fd_path: str,
    argv0: object | None = None,
) -> tuple[object, ...]:
    prefix = _bwrap_prefix(
        production_root=production_root,
        test_root=test_root,
        bwrap_fd_path=bwrap_fd_path,
    )
    if argv0 is None:
        return prefix
    return (*prefix[:-1], "--argv0", os.fspath(argv0), prefix[-1])


def _normalized_mount_path(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith("/"):
        return None
    normalized = posixpath.normpath(value)
    return "/" if normalized in {"", "."} else normalized


def _contains_path(candidate: str | None, path: Path) -> bool:
    if candidate is None:
        return False
    candidate_path = _normalized_mount_path(candidate)
    path_text = _normalized_mount_path(str(path))
    if candidate_path is None or path_text is None:
        return False
    if candidate_path == path_text:
        return True
    candidate_prefix = candidate_path.rstrip("/") + "/"
    path_prefix = path_text.rstrip("/") + "/"
    return candidate_path.startswith(path_prefix) or path_text.startswith(
        candidate_prefix,
    )


def _same_mount_path(candidate: str | None, path: Path) -> bool:
    candidate_path = _normalized_mount_path(candidate)
    path_text = _normalized_mount_path(str(path))
    return candidate_path is not None and candidate_path == path_text


def _relative_mount_path(parent: str, child: str) -> str | None:
    if parent == child:
        return ""
    prefix = parent.rstrip("/") + "/"
    if child.startswith(prefix):
        return child[len(prefix) :]
    return None


def _bwrap_mount_path_contains(parent: str, child: str) -> bool:
    return _relative_mount_path(parent, child) is not None


def _clear_bwrap_aliases_at_or_below(aliases: dict[str, str], path: str) -> None:
    for alias in tuple(aliases):
        if _bwrap_mount_path_contains(path, alias):
            del aliases[alias]


def _resolve_bwrap_namespace_path(path: str, aliases: dict[str, str]) -> str:
    resolved = path
    seen: set[str] = set()
    for _ in range(len(aliases) + 1):
        if resolved in seen:
            _raise_bwrap_production_root_error("symlink alias cycle is invalid")
        seen.add(resolved)
        matching_aliases = [
            alias
            for alias in aliases
            if resolved == alias or resolved.startswith(alias.rstrip("/") + "/")
        ]
        if not matching_aliases:
            return resolved
        alias = max(matching_aliases, key=len)
        tail = resolved[len(alias) :].lstrip("/")
        target = aliases[alias]
        resolved = target if not tail else f"{target.rstrip('/')}/{tail}"
        resolved = _normalized_mount_path(resolved) or resolved
    _raise_bwrap_production_root_error("symlink alias depth is invalid")


def _resolve_bwrap_symlink_target(
    source: str | None,
    destination: str,
    aliases: dict[str, str],
    *,
    production_root: Path,
    test_root: Path,
) -> str | None:
    if source is None:
        return None
    absolute_source = _normalized_mount_path(source)
    if absolute_source is not None:
        return _resolve_bwrap_namespace_path(absolute_source, aliases)
    parent = posixpath.dirname(destination) or "/"
    relative_target = _normalized_mount_path(posixpath.join(parent, source))
    if relative_target is None:
        _raise_bwrap_production_root_error("relative symlink alias target is invalid")
    resolved = _resolve_bwrap_namespace_path(relative_target, aliases)
    if _contains_path(resolved, production_root) or _contains_path(resolved, test_root):
        return resolved
    if any(_bwrap_mount_path_contains(alias, resolved) for alias in aliases):
        return resolved
    _raise_bwrap_production_root_error("relative symlink alias target is unknown")


def _bwrap_bind_source_is_test_root(
    source: str | None,
    destination: str,
    *,
    production_root: Path,
    test_root: Path,
) -> bool:
    source_path = _normalized_mount_path(source)
    destination_path = _normalized_mount_path(destination)
    production_path = _normalized_mount_path(str(production_root))
    test_path = _normalized_mount_path(str(test_root))
    if (
        source_path is None
        or destination_path is None
        or production_path is None
        or test_path is None
    ):
        return False
    relative = _relative_mount_path(production_path, destination_path)
    if relative is None:
        return False
    expected_source = test_path if relative == "" else f"{test_path.rstrip('/')}/{relative}"
    return source_path in {test_path, expected_source}


def _require_bwrap_arity(
    prefix: tuple[str, ...],
    index: int,
    count: int,
) -> bool:
    if index + count >= len(prefix):
        return False
    return True


def _raise_bwrap_production_root_error(message: str) -> None:
    raise ValueError(f"bwrap lock isolation must not expose the production root: {message}")


def _reject_bwrap_path_alias(value: str | None) -> None:
    if value is None:
        return
    normalized = _normalized_mount_path(value)
    if normalized is None:
        return
    try:
        private_io.assert_no_symlink_ancestors(
            Path(normalized),
            label="bwrap lock isolation path",
        )
    except ValueError as exc:
        _raise_bwrap_production_root_error(str(exc))


def _bwrap_value_exposes_lock_root(value: str | None, *, production_root: Path) -> bool:
    _reject_bwrap_path_alias(value)
    return _contains_path(value, production_root)


def _bwrap_prefix_allows_existing_isolation(
    prefix: tuple[str, ...],
    *,
    production_root: Path,
    test_root: Path,
) -> bool:
    root_mount_is_safe = False
    child_mounts: dict[str, bool] = {}
    final_env: dict[str, str | None] = {}
    aliases: dict[str, str] = {}
    saw_lock_root_mount = False
    unsafe_specific_lock_root_touch = False

    def record_mount(
        operation: str,
        source: str | None,
        destination: str | None,
    ) -> None:
        nonlocal root_mount_is_safe, saw_lock_root_mount, unsafe_specific_lock_root_touch
        _reject_bwrap_path_alias(source)
        _reject_bwrap_path_alias(destination)
        destination_path = _normalized_mount_path(destination)
        production_path = _normalized_mount_path(str(production_root))
        if destination_path is None or production_path is None:
            return
        resolved_destination = _resolve_bwrap_namespace_path(destination_path, aliases)
        relative = _relative_mount_path(production_path, resolved_destination)
        destination_covers_root = (
            resolved_destination == production_path
            or production_path.startswith(resolved_destination.rstrip("/") + "/")
        )
        destination_is_child = relative not in {None, ""}
        if not destination_covers_root and not destination_is_child:
            return
        saw_lock_root_mount = True
        source_is_safe = operation in {"--bind", "--ro-bind"} and (
            _bwrap_bind_source_is_test_root(
                source,
                resolved_destination,
                production_root=production_root,
                test_root=test_root,
            )
        )
        if relative is not None and not source_is_safe:
            unsafe_specific_lock_root_touch = True
        if destination_covers_root:
            root_mount_is_safe = (
                resolved_destination == production_path and source_is_safe
            )
            if root_mount_is_safe:
                child_mounts.clear()
            return
        child_mounts[resolved_destination] = source_is_safe

    def record_symlink(source: str | None, destination: str | None) -> None:
        _reject_bwrap_path_alias(source)
        _reject_bwrap_path_alias(destination)
        destination_path = _normalized_mount_path(destination)
        if destination_path is None:
            return
        record_mount("--symlink", None, destination_path)
        _clear_bwrap_aliases_at_or_below(aliases, destination_path)
        source_path = _resolve_bwrap_symlink_target(
            source,
            destination_path,
            aliases,
            production_root=production_root,
            test_root=test_root,
        )
        if source_path is not None:
            aliases[destination_path] = source_path

    def forget_destination_aliases(destination: str | None) -> None:
        destination_path = _normalized_mount_path(destination)
        if destination_path is not None:
            _clear_bwrap_aliases_at_or_below(aliases, destination_path)

    index = 1
    while index < len(prefix):
        token = prefix[index]
        if token in _BWRAP_PAIR_MOUNT_OPTIONS:
            if not _require_bwrap_arity(prefix, index, 2):
                _raise_bwrap_production_root_error("option arity is invalid")
            source = prefix[index + 1]
            destination = prefix[index + 2]
            record_mount(token, source, destination)
            forget_destination_aliases(destination)
            index += 3
            continue
        if token in _BWRAP_FD_DESTINATION_MOUNT_OPTIONS:
            if not _require_bwrap_arity(prefix, index, 2):
                _raise_bwrap_production_root_error("option arity is invalid")
            destination = prefix[index + 2]
            record_mount(token, None, destination)
            forget_destination_aliases(destination)
            index += 3
            continue
        if token == "--symlink":
            if not _require_bwrap_arity(prefix, index, 2):
                _raise_bwrap_production_root_error("option arity is invalid")
            source = prefix[index + 1]
            destination = prefix[index + 2]
            record_symlink(source, destination)
            index += 3
            continue
        if token in _BWRAP_TRIPLE_DESTINATION_MOUNT_OPTIONS:
            if not _require_bwrap_arity(prefix, index, 3):
                _raise_bwrap_production_root_error("option arity is invalid")
            destination = prefix[index + 3]
            record_mount(token, None, destination)
            forget_destination_aliases(destination)
            index += 4
            continue
        if token == "--overlay-src":
            if not _require_bwrap_arity(prefix, index, 1):
                _raise_bwrap_production_root_error("option arity is invalid")
            index += 2
            continue
        if token in _BWRAP_DESTINATION_MOUNT_OPTIONS:
            if not _require_bwrap_arity(prefix, index, 1):
                _raise_bwrap_production_root_error("option arity is invalid")
            destination = prefix[index + 1]
            record_mount(token, None, destination)
            forget_destination_aliases(destination)
            index += 2
            continue
        if token == "--setenv":
            if index + 2 >= len(prefix):
                _raise_bwrap_production_root_error("option arity is invalid")
            name = prefix[index + 1]
            if name in _BWRAP_ENV_LOCK_ROOT_NAMES:
                final_env[name] = prefix[index + 2]
            index += 3
            continue
        if token == "--unsetenv":
            if index + 1 >= len(prefix):
                _raise_bwrap_production_root_error("option arity is invalid")
            name = prefix[index + 1]
            if name in _BWRAP_ENV_LOCK_ROOT_NAMES:
                final_env[name] = None
            index += 2
            continue
        if token in _BWRAP_SINGLE_PATH_TOUCH_OPTIONS:
            if not _require_bwrap_arity(prefix, index, 1):
                _raise_bwrap_production_root_error("option arity is invalid")
            destination = prefix[index + 1]
            record_mount(token, None, destination)
            forget_destination_aliases(destination)
            index += 2
            continue
        if token in _BWRAP_DOUBLE_PATH_TOUCH_OPTIONS:
            if not _require_bwrap_arity(prefix, index, 2):
                _raise_bwrap_production_root_error("option arity is invalid")
            destination = prefix[index + 2]
            record_mount(token, None, destination)
            forget_destination_aliases(destination)
            index += 3
            continue
        if token in _BWRAP_NO_ARGUMENT_OPTIONS:
            index += 1
            continue
        if token in _BWRAP_SINGLE_ARGUMENT_OPTIONS:
            if not _require_bwrap_arity(prefix, index, 1):
                _raise_bwrap_production_root_error("option arity is invalid")
            index += 2
            continue
        if token.startswith("--"):
            _raise_bwrap_production_root_error(f"unknown bwrap option {token!r}")
        _raise_bwrap_production_root_error(f"unexpected bwrap operand {token!r}")
    for value in final_env.values():
        if _bwrap_value_exposes_lock_root(value, production_root=production_root):
            raise ValueError("bwrap lock isolation must not expose the production root")
    if unsafe_specific_lock_root_touch:
        raise ValueError("bwrap lock isolation must not expose the production root")
    if not root_mount_is_safe:
        if saw_lock_root_mount:
            raise ValueError("bwrap lock isolation must not expose the production root")
        return False
    if child_mounts and all(child_mounts.values()):
        return True
    if not child_mounts:
        return True
    raise ValueError("bwrap lock isolation must not expose the production root")


def _already_binds_test_lock_root(
    tokens: tuple[object, ...],
    *,
    production_root: Path,
    test_root: Path,
    bwrap_fd_path: str,
) -> bool:
    if not tokens:
        return False
    first_token = _token_text(tokens[0])
    trusted_bwrap_fd = first_token == bwrap_fd_path
    if not trusted_bwrap_fd and posixpath.basename(first_token) != "bwrap":
        return False
    command_boundary = tokens.index("--") if "--" in tokens else len(tokens)
    raw_prefix = tuple(_token_text(token) for token in tokens[:command_boundary])
    has_args_fd = "--args" in raw_prefix
    try:
        prefix = _expand_bwrap_args_fds(
            raw_prefix,
            bwrap_fd_path=bwrap_fd_path,
        )
        isolation_is_safe = _bwrap_prefix_allows_existing_isolation(
            prefix,
            production_root=production_root,
            test_root=test_root,
        )
        return trusted_bwrap_fd and isolation_is_safe
    except ValueError as exc:
        if has_args_fd:
            raise ValueError(f"bwrap --args FD content is invalid: {exc}") from exc
        raise


def _wrap_codex_usage_subprocess_args(
    args,
    popen_kwargs,
    *,
    production_root: Path,
    test_root: Path,
    bwrap_fd_path: str,
    bwrap_pass_fd: int,
):
    kwargs = dict(popen_kwargs)
    tokens = tuple(args) if isinstance(args, (list, tuple)) else ()
    executable = kwargs.get("executable")
    shell = bool(kwargs.get("shell"))
    allowed_pass_fds = _allowed_executable_pass_fds(
        args=args,
        tokens=tokens,
        executable=executable,
        shell=shell,
        bwrap_fd_path=bwrap_fd_path,
        bwrap_pass_fd=bwrap_pass_fd,
    )
    pass_fds = _validate_lock_isolation_process_kwargs(
        kwargs,
        allowed_pass_fds=allowed_pass_fds,
        allow_python_loader_env=not shell
        and executable is None
        and _explicit_bwrap_command(tokens),
    )
    if (
        not shell
        and executable is None
        and _already_binds_test_lock_root(
            tokens,
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path=bwrap_fd_path,
        )
    ):
        return args, popen_kwargs
    _prepare_lock_isolation_process_env(kwargs)
    if shell:
        command_tail = ("/bin/sh", "-c", _token_text(args))
        kwargs["shell"] = False
        kwargs.pop("executable", None)
    elif executable is not None:
        command_tail = (os.fspath(executable), *tokens[1:])
        kwargs["executable"] = bwrap_fd_path
    elif tokens:
        command_tail = tuple(os.fspath(token) for token in tokens)
    else:
        command_tail = (os.fspath(args),)
        kwargs.pop("executable", None)
    kwargs["pass_fds"] = (*pass_fds, bwrap_pass_fd)
    kwargs["close_fds"] = True
    return (
        (
            *_bwrap_prefix_with_argv0(
                production_root=production_root,
                test_root=test_root,
                bwrap_fd_path=bwrap_fd_path,
                argv0=(tokens[0] if tokens else args) if executable is not None else None,
            ),
            *command_tail,
        ),
        kwargs,
    )


def _wrap_codex_usage_spawnv_args(
    path,
    args,
    passfds,
    *,
    production_root: Path,
    test_root: Path,
    bwrap_fd_path: str,
    bwrap_pass_fd: int,
):
    tokens = tuple(args)
    spawn_passfds = tuple(passfds or ())
    allowed_passfds = _validate_resource_tracker_passfds(
        path,
        tokens,
        spawn_passfds,
        bwrap_pass_fd=bwrap_pass_fd,
    )
    if allowed_passfds is None:
        allowed_passfds = _validate_spawn_main_passfds(
            path,
            tokens,
            spawn_passfds,
            bwrap_pass_fd=bwrap_pass_fd,
        )
    if allowed_passfds is None:
        raise ValueError("lock isolation spawn pass_fds is not allowed")
    use_bytes = isinstance(path, bytes)

    def convert(value: object):
        text = os.fspath(value)
        if use_bytes:
            return os.fsencode(text)
        return os.fsdecode(text) if isinstance(text, bytes) else text

    wrapped_args = (
        *(
            convert(value)
            for value in _bwrap_prefix(
                production_root=production_root,
                test_root=test_root,
                bwrap_fd_path=bwrap_fd_path,
            )
        ),
        *(convert(value) for value in args),
    )
    return (
        convert(bwrap_fd_path),
        wrapped_args,
        (bwrap_pass_fd, *allowed_passfds),
    )


def _same_executable_path(left: object, right: object) -> bool:
    try:
        left_text = os.fsdecode(os.fspath(left))
        right_text = os.fsdecode(os.fspath(right))
    except (TypeError, ValueError):
        return False
    if left_text != right_text:
        return False
    try:
        left_item = _REAL_STAT(left_text, follow_symlinks=True)
        right_item = _REAL_STAT(right_text, follow_symlinks=True)
    except OSError:
        return False
    return (left_item.st_dev, left_item.st_ino) == (right_item.st_dev, right_item.st_ino)


def _resource_tracker_main_fd(argv_code: str) -> int | None:
    prefix = "from multiprocessing.resource_tracker import main;main("
    suffix = ")"
    if not argv_code.startswith(prefix) or not argv_code.endswith(suffix):
        return None
    fd_text = argv_code[len(prefix) : -len(suffix)]
    try:
        fd = int(fd_text, 10)
    except ValueError:
        return None
    if fd_text != str(fd) or fd < 0:
        return None
    return fd


def _current_stderr_fd() -> int | None:
    try:
        fd = sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    return fd if type(fd) is int and fd >= 0 else None


def _validate_resource_tracker_passfds(
    path: object,
    args: tuple[object, ...],
    passfds: tuple[int, ...],
    *,
    bwrap_pass_fd: int,
) -> tuple[int, ...] | None:
    if not passfds:
        return ()
    if _contains_forbidden_loader_env(os.environ):
        raise ValueError("lock isolation resource_tracker environment is not allowed")
    if any(type(fd) is not int or fd < 0 for fd in passfds):
        return None
    if len(frozenset(passfds)) != len(passfds):
        return None
    if bwrap_pass_fd in passfds:
        return None
    expected_executable = multiprocessing.spawn.get_executable()
    if not _same_executable_path(path, expected_executable):
        return None
    if len(args) < 4 or not _same_executable_path(args[0], expected_executable):
        return None
    interpreter_flags = tuple(multiprocessing.util._args_from_interpreter_flags())
    if tuple(_token_text(value) for value in args[1:-2]) != interpreter_flags:
        return None
    if _token_text(args[-2]) != "-c":
        return None
    tracker_fd = _resource_tracker_main_fd(_token_text(args[-1]))
    if tracker_fd is None:
        return None
    stderr_fd = _current_stderr_fd()
    allowed_fds = {tracker_fd}
    if stderr_fd is not None:
        allowed_fds.add(stderr_fd)
    if set(passfds) != allowed_fds:
        return None
    try:
        tracker_item = _REAL_FSTAT(tracker_fd)
        if stderr_fd is not None:
            _REAL_FSTAT(stderr_fd)
    except OSError:
        return None
    if not stat.S_ISFIFO(tracker_item.st_mode):
        return None
    return passfds


def _spawn_main_fds(argv_code: str) -> tuple[int, int] | None:
    prefix = "from multiprocessing.spawn import spawn_main; spawn_main("
    suffix = ")"
    if not argv_code.startswith(prefix) or not argv_code.endswith(suffix):
        return None
    values: dict[str, int] = {}
    for raw_part in argv_code[len(prefix) : -len(suffix)].split(", "):
        name, separator, value_text = raw_part.partition("=")
        if separator != "=" or name not in {"tracker_fd", "pipe_handle"}:
            return None
        try:
            value = int(value_text, 10)
        except ValueError:
            return None
        if value_text != str(value) or value < 0:
            return None
        values[name] = value
    if set(values) != {"tracker_fd", "pipe_handle"}:
        return None
    return values["tracker_fd"], values["pipe_handle"]


def _validate_spawn_main_passfds(
    path: object,
    args: tuple[object, ...],
    passfds: tuple[int, ...],
    *,
    bwrap_pass_fd: int,
) -> tuple[int, ...] | None:
    if not passfds:
        return ()
    if _contains_forbidden_loader_env(os.environ):
        raise ValueError("lock isolation spawn_main environment is not allowed")
    if any(type(fd) is not int or fd < 0 for fd in passfds):
        return None
    if len(frozenset(passfds)) != len(passfds):
        return None
    if bwrap_pass_fd in passfds:
        return None
    expected_executable = multiprocessing.spawn.get_executable()
    if not _same_executable_path(path, expected_executable):
        return None
    if len(args) < 5 or not _same_executable_path(args[0], expected_executable):
        return None
    if _token_text(args[-1]) != "--multiprocessing-fork":
        return None
    interpreter_flags = tuple(multiprocessing.util._args_from_interpreter_flags())
    if tuple(_token_text(value) for value in args[1:-3]) != interpreter_flags:
        return None
    if _token_text(args[-3]) != "-c":
        return None
    spawn_fds = _spawn_main_fds(_token_text(args[-2]))
    if spawn_fds is None:
        return None
    tracker_fd, pipe_handle = spawn_fds
    if tracker_fd not in passfds or pipe_handle not in passfds:
        return None
    try:
        fd_stats = tuple((fd, _REAL_FSTAT(fd)) for fd in passfds)
    except OSError:
        return None
    if not all(
        stat.S_ISFIFO(item.st_mode)
        or stat.S_ISSOCK(item.st_mode)
        or stat.S_ISCHR(item.st_mode)
        for _fd, item in fd_stats
    ):
        return None
    return passfds


def _wrap_lock_isolation_exec_args(
    path,
    args,
    *,
    production_root: Path,
    test_root: Path,
    bwrap_fd_path: str,
):
    use_bytes = isinstance(path, bytes)

    def convert(value: object):
        text = os.fspath(value)
        if use_bytes:
            return os.fsencode(text)
        return os.fsdecode(text) if isinstance(text, bytes) else text

    wrapped_args = (
        *(
            convert(value)
            for value in _bwrap_prefix(
                production_root=production_root,
                test_root=test_root,
                bwrap_fd_path=bwrap_fd_path,
            )
        ),
        *(convert(value) for value in args),
    )
    return convert(bwrap_fd_path), wrapped_args


def _wrap_lock_isolation_shell_command(
    command: object,
    *,
    production_root: Path,
    test_root: Path,
    bwrap_fd_path: str,
) -> str:
    return shlex.join(
        (
            *_bwrap_prefix(
                production_root=production_root,
                test_root=test_root,
                bwrap_fd_path=bwrap_fd_path,
            ),
            "/bin/sh",
            "-c",
            _token_text(command),
        )
    )


def _namespace_identities() -> tuple[str, ...]:
    identities: list[str] = []
    for path in _PROC_SELF_NAMESPACE_PATHS:
        try:
            identities.append(_REAL_READLINK(path))
        except OSError:
            return ()
    return tuple(identities)


def _path_is_on_procfs_mount(path: Path) -> bool:
    try:
        target = path.resolve(strict=True)
        raw_mountinfo = _PROC_SELF_MOUNTINFO.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    best_mount_point = ""
    best_fstype = ""
    for line in raw_mountinfo.splitlines():
        prefix, separator, suffix = line.partition(" - ")
        if not separator:
            return False
        prefix_fields = prefix.split()
        suffix_fields = suffix.split()
        if len(prefix_fields) < 5 or len(suffix_fields) < 1:
            return False
        mount_point = _mountinfo_unescape(prefix_fields[4])
        if not mount_point.startswith("/"):
            return False
        if str(target) == mount_point or str(target).startswith(
            mount_point.rstrip("/") + "/"
        ):
            if len(mount_point) > len(best_mount_point):
                best_mount_point = mount_point
                best_fstype = suffix_fields[0]
    return best_fstype == "proc"


def _same_proc_uid_map_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_mode == right.st_mode
        and left.st_uid == right.st_uid
        and left.st_gid == right.st_gid
        and left.st_nlink == right.st_nlink
        and left.st_size == right.st_size
    )


def _read_proc_self_uid_map() -> bytes | None:
    if _PROC_SELF_UID_MAP != Path("/proc/self/uid_map"):
        return None
    if not _path_is_on_procfs_mount(_PROC_SELF_UID_MAP):
        return None
    fd = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = _REAL_OPEN(_PROC_SELF_UID_MAP, flags)
        opened = _REAL_FSTAT(fd)
        named = _REAL_STAT(_PROC_SELF_UID_MAP, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not _same_proc_uid_map_identity(opened, named)
        ):
            return None
        chunks: list[bytes] = []
        total = 0
        while total <= _PROC_SELF_UID_MAP_MAX_BYTES:
            chunk = _REAL_READ(
                fd,
                min(4096, _PROC_SELF_UID_MAP_MAX_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > _PROC_SELF_UID_MAP_MAX_BYTES:
            return None
        final = _REAL_FSTAT(fd)
        if not _same_proc_uid_map_identity(opened, final):
            return None
        return b"".join(chunks)
    except (OSError, ValueError):
        return None
    finally:
        if fd >= 0:
            _REAL_CLOSE(fd)


def _parse_host_root_overflow_uid_map(raw_uid_map: bytes) -> bool:
    if not raw_uid_map or not raw_uid_map.endswith(b"\n"):
        return False
    try:
        text = raw_uid_map.decode("ascii")
    except UnicodeError:
        return False
    lines = text.splitlines()
    if len(lines) != 1:
        return False
    line = lines[0]
    parts = line.split()
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        return False
    inside_uid, outside_uid, length = (int(part, 10) for part in parts)
    if inside_uid < 0 or outside_uid < 0 or length <= 0:
        return False
    if inside_uid > 2**32 - 1 or outside_uid > 2**32 - 1 or length > 2**32:
        return False
    return not (outside_uid <= 0 < outside_uid + length)


def _bwrap_binding_matches(
    item: os.stat_result,
    binding: _BwrapOverflowOwnerBinding,
) -> bool:
    return (
        item.st_dev == binding.device
        and item.st_ino == binding.inode
        and item.st_mode == binding.mode
        and item.st_uid == binding.uid
        and item.st_gid == binding.gid
        and item.st_nlink == binding.nlink
        and item.st_size == binding.size
        and item.st_mtime_ns == binding.mtime_ns
        and item.st_ctime_ns == binding.ctime_ns
        and binding.uid == _BWRAP_OVERFLOW_UID
        and bool(binding.sha256)
        and binding.namespaces == _namespace_identities()
    )


def _uid_map_treats_host_root_as_overflow(
    *,
    bwrap_binding: _BwrapOverflowOwnerBinding | None = None,
) -> bool:
    if bwrap_binding is None or not bwrap_binding.namespaces:
        return False
    if bwrap_binding.namespaces != _namespace_identities():
        return False
    raw_uid_map = _read_proc_self_uid_map()
    if raw_uid_map is None:
        return False
    if bwrap_binding.namespaces != _namespace_identities():
        return False
    if not _parse_host_root_overflow_uid_map(raw_uid_map):
        return False
    fd = -1
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(_BWRAP_EXECUTABLE_PATH, flags)
        current = os.fstat(fd)
        named = os.stat(_BWRAP_EXECUTABLE_PATH, follow_symlinks=False)
        if not _same_bwrap_identity(current, named):
            return False
        if not _bwrap_binding_matches(current, bwrap_binding):
            return False
        if current.st_size < 0 or current.st_size > _BWRAP_EXECUTABLE_MAX_BYTES:
            return False
        digest = hashlib.sha256()
        total = 0
        while total <= _BWRAP_EXECUTABLE_MAX_BYTES:
            chunk = os.read(
                fd,
                min(65_536, _BWRAP_EXECUTABLE_MAX_BYTES + 1 - total),
            )
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        if total != current.st_size or total > _BWRAP_EXECUTABLE_MAX_BYTES:
            return False
        final = os.fstat(fd)
        if not _same_bwrap_identity(current, final):
            return False
        return digest.hexdigest() == bwrap_binding.sha256
    except OSError:
        return False
    finally:
        if fd >= 0:
            os.close(fd)


def _bwrap_owner_is_trusted(
    item: os.stat_result,
    *,
    bwrap_binding: _BwrapOverflowOwnerBinding | None = None,
) -> bool:
    if item.st_uid in {0, os.geteuid()}:
        return True
    if item.st_uid != _BWRAP_OVERFLOW_UID or bwrap_binding is None:
        return False
    if not _bwrap_binding_matches(item, bwrap_binding):
        return False
    return _uid_map_treats_host_root_as_overflow(bwrap_binding=bwrap_binding)


def _bwrap_overflow_owner_binding(
    item: os.stat_result,
    *,
    digest: str,
) -> _BwrapOverflowOwnerBinding:
    return _BwrapOverflowOwnerBinding(
        device=item.st_dev,
        inode=item.st_ino,
        mode=item.st_mode,
        uid=item.st_uid,
        gid=item.st_gid,
        nlink=item.st_nlink,
        size=item.st_size,
        mtime_ns=item.st_mtime_ns,
        ctime_ns=item.st_ctime_ns,
        sha256=digest,
        namespaces=_namespace_identities(),
    )


def _same_bwrap_identity(left: os.stat_result, right: os.stat_result) -> bool:
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


def _validate_bwrap_executable_identity(
    item: os.stat_result,
    *,
    bwrap_binding: _BwrapOverflowOwnerBinding | None = None,
) -> None:
    mode = stat.S_IMODE(item.st_mode)
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_nlink < 1
        or not _bwrap_owner_is_trusted(item, bwrap_binding=bwrap_binding)
        or mode & 0o022
    ):
        raise ValueError("bwrap executable identity is invalid")


def _bwrap_fd_sha256(fd: int, expected: os.stat_result) -> str:
    if expected.st_size < 0 or expected.st_size > _BWRAP_EXECUTABLE_MAX_BYTES:
        raise ValueError("bwrap executable identity is invalid")
    digest = hashlib.sha256()
    remaining_budget = _BWRAP_EXECUTABLE_MAX_BYTES + 1
    total = 0
    while remaining_budget > 0:
        chunk = _REAL_READ(fd, min(65_536, remaining_budget))
        if not chunk:
            break
        total += len(chunk)
        remaining_budget -= len(chunk)
        if total > _BWRAP_EXECUTABLE_MAX_BYTES:
            raise ValueError("bwrap executable identity is invalid")
        digest.update(chunk)
    if total != expected.st_size:
        raise ValueError("bwrap executable identity is invalid")
    final = _REAL_FSTAT(fd)
    if not _same_bwrap_identity(expected, final):
        raise ValueError("bwrap executable identity is invalid")
    return digest.hexdigest()


def _open_bwrap_fd() -> int:
    if os.environ.get("CODEX_USAGE_TEST_FORBID_INNER_BWRAP") == "1":
        raise RuntimeError("inner bwrap invocation forbidden by outer proof harness")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = _REAL_OPEN(_BWRAP_EXECUTABLE_PATH, flags)
    mirror_fd = -1
    try:
        item = _REAL_FSTAT(fd)
        digest = _bwrap_fd_sha256(fd, item)
        binding = _bwrap_overflow_owner_binding(item, digest=digest)
        _validate_bwrap_executable_identity(item, bwrap_binding=binding)
        named_item = _REAL_STAT(_BWRAP_EXECUTABLE_PATH, follow_symlinks=False)
        if not _same_bwrap_identity(item, named_item):
            raise ValueError("bwrap executable identity is invalid")
        mirror_fd = _REAL_OPEN(_BWRAP_EXECUTABLE_PATH, flags)
        if mirror_fd == fd:
            raise ValueError("bwrap executable identity is invalid")
        mirror_item = _REAL_FSTAT(mirror_fd)
        mirror_digest = _bwrap_fd_sha256(mirror_fd, mirror_item)
        mirror_binding = _bwrap_overflow_owner_binding(mirror_item, digest=mirror_digest)
        _validate_bwrap_executable_identity(
            mirror_item,
            bwrap_binding=mirror_binding,
        )
        if not _same_bwrap_identity(item, mirror_item):
            raise ValueError("bwrap executable identity is invalid")
        if digest != mirror_digest:
            raise ValueError("bwrap executable identity is invalid")
        _REAL_CLOSE(mirror_fd)
        mirror_fd = -1
        return fd
    except Exception:
        if mirror_fd >= 0:
            _REAL_CLOSE(mirror_fd)
        _REAL_CLOSE(fd)
        raise


def _bwrap_lock_isolation_available(
    *,
    production_root: Path,
    test_root: Path,
    bwrap_fd_path: str,
    bwrap_pass_fd: int,
) -> bool:
    try:
        result = subprocess.run(
            [
                bwrap_fd_path,
                "--bind",
                "/",
                "/",
                "--bind",
                str(test_root),
                str(production_root),
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            env={},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(bwrap_pass_fd,),
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return False
    return result.returncode == 0


def _require_bwrap_lock_isolation_available(available: bool) -> None:
    if available:
        return
    raise RuntimeError(
        "nested bwrap lock isolation is unavailable; release evidence blocked"
    )


def _mountinfo_unescape(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _outer_lock_fail(message: str) -> None:
    raise RuntimeError(f"outer lock isolation proof {message}")


def _outer_lock_path(value: object, *, label: str) -> Path:
    if type(value) is not str or not value or "\x00" in value:
        _outer_lock_fail(f"{label} is invalid")
    path = Path(value)
    if (
        not path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        _outer_lock_fail(f"{label} is invalid")
    return path


def _outer_lock_snapshot_payload(
    snapshot: _LockEntrySnapshot,
    *,
    include_times: bool = True,
) -> dict[str, object]:
    payload = {
        "device": snapshot.device,
        "file_type": snapshot.file_type,
        "gid": snapshot.gid,
        "inode": snapshot.inode,
        "mode": snapshot.mode,
        "nlink": snapshot.nlink,
        "size": snapshot.size,
        "uid": snapshot.uid,
    }
    if include_times:
        payload["ctime_ns"] = snapshot.ctime_ns
        payload["mtime_ns"] = snapshot.mtime_ns
    return payload


def _outer_lock_stable_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        _outer_lock_fail("payload identity is invalid")
    return {
        key: payload.get(key)
        for key in (
            "device",
            "file_type",
            "gid",
            "inode",
            "mode",
            "nlink",
            "size",
            "uid",
        )
    }


def _outer_lock_snapshot(path: Path, *, name: str) -> _LockEntrySnapshot:
    try:
        item = _REAL_STAT(path, follow_symlinks=False)
    except OSError as exc:
        _outer_lock_fail(f"{name} is unavailable: {exc}")
    return _entry_snapshot_from_stat(item, name=name)


def _require_outer_directory(snapshot: _LockEntrySnapshot, *, label: str) -> None:
    if snapshot.file_type != "directory":
        _outer_lock_fail(f"{label} must be a directory")
    if snapshot.uid != os.geteuid():
        _outer_lock_fail(f"{label} owner is invalid")
    if stat.S_IMODE(snapshot.mode) != 0o700:
        _outer_lock_fail(f"{label} mode is invalid")


def _same_outer_identity(
    left: _LockEntrySnapshot,
    right: _LockEntrySnapshot,
) -> bool:
    return _outer_lock_snapshot_payload(
        left,
        include_times=False,
    ) == _outer_lock_snapshot_payload(right, include_times=False)


def _read_outer_proof_file(
    proof_path: Path,
    *,
    expected_sha256: str,
) -> tuple[bytes, _LockEntrySnapshot]:
    if (
        type(expected_sha256) is not str
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        _outer_lock_fail("sha is invalid")
    fd = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = _REAL_OPEN(proof_path, flags)
        opened = _REAL_FSTAT(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or opened.st_size < 0
            or opened.st_size > _OUTER_LOCK_PROOF_MAX_BYTES
        ):
            _outer_lock_fail("file identity is invalid")
        named = _REAL_STAT(proof_path, follow_symlinks=False)
        if not _same_bwrap_identity(opened, named):
            _outer_lock_fail("file changed before read")
        payload = bytearray()
        while len(payload) <= _OUTER_LOCK_PROOF_MAX_BYTES:
            chunk = _REAL_READ(
                fd,
                min(65_536, _OUTER_LOCK_PROOF_MAX_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _OUTER_LOCK_PROOF_MAX_BYTES:
            _outer_lock_fail("file is too large")
        final = _REAL_FSTAT(fd)
        if not _same_bwrap_identity(opened, final):
            _outer_lock_fail("file changed during read")
        raw = bytes(payload)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            _outer_lock_fail("sha mismatch")
        return raw, _entry_snapshot_from_stat(opened, name=proof_path.name)
    except RuntimeError:
        raise
    except OSError as exc:
        _outer_lock_fail(f"file is unavailable: {exc}")
    finally:
        if fd >= 0:
            _REAL_CLOSE(fd)


def _current_outer_namespaces() -> tuple[str, str]:
    try:
        return (
            _REAL_READLINK("/proc/self/ns/user"),
            _REAL_READLINK("/proc/self/ns/mnt"),
        )
    except OSError as exc:
        _outer_lock_fail(f"namespace identity is unavailable: {exc}")


def _expected_outer_mount_root(synthetic_root: Path) -> str:
    try:
        return "/" + synthetic_root.relative_to("/tmp").as_posix()
    except ValueError:
        _outer_lock_fail("synthetic root is invalid")


def _verify_outer_mountinfo(
    *,
    production_root: Path,
    synthetic_root: Path,
) -> None:
    expected_root = _expected_outer_mount_root(synthetic_root)
    try:
        raw_mountinfo = _PROC_SELF_MOUNTINFO.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _outer_lock_fail(f"mountinfo is unavailable: {exc}")
    for line in raw_mountinfo.splitlines():
        prefix, separator, suffix = line.partition(" - ")
        if not separator:
            _outer_lock_fail("mountinfo is malformed")
        prefix_fields = prefix.split()
        suffix_fields = suffix.split()
        if len(prefix_fields) < 6 or len(suffix_fields) < 1:
            _outer_lock_fail("mountinfo is malformed")
        mount_root = _mountinfo_unescape(prefix_fields[3])
        mount_point = _mountinfo_unescape(prefix_fields[4])
        if mount_point != str(production_root):
            continue
        options = frozenset(prefix_fields[5].split(","))
        if mount_root != expected_root:
            _outer_lock_fail("mount source is invalid")
        if suffix_fields[0] != "tmpfs" or not {"rw", "nosuid", "nodev"} <= options:
            _outer_lock_fail("mountinfo does not match the canonical outer mount")
        return
    _outer_lock_fail("mountinfo does not contain the literal production root")


def _attest_outer_lock_isolation_from_environment(
    production_root: Path,
    *,
    environ: Mapping[str, str],
) -> _OuterLockIsolationProof | None:
    present = [name for name in _OUTER_LOCK_PROOF_ENV_NAMES if environ.get(name)]
    if not present:
        return None
    if len(present) != len(_OUTER_LOCK_PROOF_ENV_NAMES):
        _outer_lock_fail("environment is incomplete")
    for name, expected in _OUTER_LOCK_REQUIRED_ENVIRONMENT.items():
        if environ.get(name) != expected:
            _outer_lock_fail(f"{name} is invalid")
    selected_production_root = _outer_lock_path(
        environ["CODEX_USAGE_TEST_OUTER_LOCK_PRODUCTION_ROOT"],
        label="production root",
    )
    if production_root != selected_production_root:
        _outer_lock_fail("literal production root does not match caller")
    if selected_production_root != _OUTER_LOCK_PRODUCTION_ROOT:
        _outer_lock_fail("literal production root is invalid")
    synthetic_root = _outer_lock_path(
        environ["CODEX_USAGE_TEST_OUTER_LOCK_SYNTHETIC_ROOT"],
        label="synthetic root",
    )
    if (
        synthetic_root == selected_production_root
        or synthetic_root.name != "synthetic-lock-root"
        or synthetic_root.parent.parent != Path("/tmp")
        or not synthetic_root.parent.name.startswith("cycle31-release-review.")
    ):
        _outer_lock_fail("synthetic root is invalid")
    proof_path = _outer_lock_path(
        environ["CODEX_USAGE_TEST_OUTER_LOCK_PROOF"],
        label="proof path",
    )
    if proof_path.name != _OUTER_LOCK_PROOF_NAME or proof_path.parent != synthetic_root.parent:
        _outer_lock_fail("proof path is invalid")
    if proof_path == selected_production_root / _OUTER_LOCK_PROOF_NAME:
        _outer_lock_fail("proof path must stay outside the lock namespace")

    parent_namespaces = (
        environ["CODEX_USAGE_TEST_OUTER_LOCK_PARENT_USER_NS"],
        environ["CODEX_USAGE_TEST_OUTER_LOCK_PARENT_MNT_NS"],
    )
    if any(
        type(namespace) is not str or not namespace.startswith(prefix)
        for namespace, prefix in zip(
            parent_namespaces,
            ("user:[", "mnt:["),
            strict=True,
        )
    ):
        _outer_lock_fail("parent namespace identity is invalid")
    current_namespaces = _current_outer_namespaces()
    if current_namespaces[1] == parent_namespaces[1]:
        _outer_lock_fail("mount namespace did not change")
    if current_namespaces[0] == parent_namespaces[0]:
        _outer_lock_fail("user namespace did not change")

    production_snapshot = _outer_lock_snapshot(
        selected_production_root,
        name="production root",
    )
    synthetic_snapshot = _outer_lock_snapshot(synthetic_root, name="synthetic root")
    _require_outer_directory(production_snapshot, label="production root")
    _require_outer_directory(synthetic_snapshot, label="synthetic root")
    if not _same_outer_identity(production_snapshot, synthetic_snapshot):
        _outer_lock_fail("synthetic root binding changed")
    _verify_outer_mountinfo(
        production_root=selected_production_root,
        synthetic_root=synthetic_root,
    )
    raw_proof, proof_snapshot = _read_outer_proof_file(
        proof_path,
        expected_sha256=environ["CODEX_USAGE_TEST_OUTER_LOCK_PROOF_SHA256"],
    )
    try:
        payload = json.loads(raw_proof)
    except json.JSONDecodeError as exc:
        _outer_lock_fail(f"payload is invalid: {exc}")
    if not isinstance(payload, dict):
        _outer_lock_fail("payload is invalid")
    if payload.get("format") != _OUTER_LOCK_PROOF_FORMAT:
        _outer_lock_fail("payload is invalid")
    if payload.get("literal_production_root") != str(selected_production_root):
        _outer_lock_fail("literal production root is invalid")
    if payload.get("synthetic_root") != str(synthetic_root):
        _outer_lock_fail("synthetic root is invalid")
    if payload.get("proof_path") != str(proof_path):
        _outer_lock_fail("proof path is invalid")
    if payload.get("parent_user_namespace") != parent_namespaces[0]:
        _outer_lock_fail("parent namespace identity is invalid")
    if payload.get("parent_mnt_namespace") != parent_namespaces[1]:
        _outer_lock_fail("parent namespace identity is invalid")
    if _outer_lock_stable_payload(payload.get("synthetic_root_identity")) != (
        _outer_lock_snapshot_payload(synthetic_snapshot, include_times=False)
    ):
        _outer_lock_fail("synthetic root changed")
    hidden_production_root_identity = payload.get("hidden_production_root_identity")
    if not isinstance(hidden_production_root_identity, dict):
        _outer_lock_fail("host product root identity is invalid")
    if _outer_lock_stable_payload(hidden_production_root_identity) == (
        _outer_lock_snapshot_payload(production_snapshot, include_times=False)
    ):
        _outer_lock_fail("host product root remains visible")
    if _current_outer_namespaces() != current_namespaces:
        _outer_lock_fail("namespace changed after proof")
    return _OuterLockIsolationProof(
        production_root=selected_production_root,
        synthetic_root=synthetic_root,
        proof_path=proof_path,
        proof_sha256=environ["CODEX_USAGE_TEST_OUTER_LOCK_PROOF_SHA256"],
        parent_namespaces=parent_namespaces,
        current_namespaces=current_namespaces,
        synthetic_root_identity=synthetic_snapshot,
        hidden_production_root_identity=hidden_production_root_identity,
        proof_identity=proof_snapshot,
    )


@pytest.fixture(scope="session", autouse=True)
def isolate_private_lock_root(tmp_path_factory, request):
    """Keep persistent test locks out of the invoking user's product root."""
    production_root = private_io._private_lock_root()
    outer_proof = _attest_outer_lock_isolation_from_environment(
        production_root,
        environ=os.environ,
    )
    request.config._private_lock_outer_isolation_proof = outer_proof
    request.config._private_lock_production_root = production_root
    session_guard = None
    if outer_proof is None:
        session_guard = _open_lock_namespace_guard(
            production_root,
            label="tests wrote persistent locks into product root",
            allow_root_time_drift=True,
        )
    request.config._private_lock_session_guard = session_guard
    if outer_proof is not None:
        test_root = outer_proof.synthetic_root
    else:
        test_root = tmp_path_factory.mktemp("private-lock-root")
        test_root.chmod(0o700)
    patch = pytest.MonkeyPatch()
    patch.setattr(private_io, "_private_lock_root", lambda: test_root)
    if outer_proof is not None:

        def verify_outer_isolation():
            patch.undo()

        request.addfinalizer(verify_outer_isolation)
        return

    real_popen = subprocess.Popen
    bwrap_fd = _open_bwrap_fd()
    os.set_inheritable(bwrap_fd, True)
    bwrap_fd_path = f"/proc/self/fd/{bwrap_fd}"
    if not _bwrap_lock_isolation_available(
        production_root=production_root,
        test_root=test_root,
        bwrap_fd_path=bwrap_fd_path,
        bwrap_pass_fd=bwrap_fd,
    ):
        _REAL_CLOSE(bwrap_fd)
        _require_bwrap_lock_isolation_available(False)

    def isolated_popen(*popen_args, **popen_kwargs):
        try:
            bound = _POPEN_SIGNATURE.bind(*popen_args, **popen_kwargs)
        except TypeError as exc:
            raise ValueError(
                "lock isolation subprocess arguments are invalid",
            ) from exc
        if len(popen_args) > len(_POPEN_POSITIONAL_PARAMETER_NAMES):
            raise ValueError("lock isolation subprocess positional arguments are invalid")
        supplied_positional_names = _POPEN_POSITIONAL_PARAMETER_NAMES[
            1:len(popen_args)
        ]
        unsafe_names = tuple(
            name for name in supplied_positional_names if name != "bufsize"
        )
        if unsafe_names:
            raise ValueError(
                "lock isolation subprocess positional "
                f"{unsafe_names[0]} is not allowed"
            )
        bound.apply_defaults()
        bound_arguments = dict(bound.arguments)
        args = bound_arguments.pop("args")
        wrapped_args, wrapped_kwargs = _wrap_codex_usage_subprocess_args(
            args,
            bound_arguments,
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path=bwrap_fd_path,
            bwrap_pass_fd=bwrap_fd,
        )
        return real_popen(wrapped_args, **wrapped_kwargs)

    patch.setattr(subprocess, "Popen", isolated_popen)

    def isolated_system(command):
        return _REAL_SYSTEM(
            _wrap_lock_isolation_shell_command(
                command,
                production_root=production_root,
                test_root=test_root,
                bwrap_fd_path=bwrap_fd_path,
            )
        )

    patch.setattr(os, "system", isolated_system)

    def isolated_popen_shell(command, mode="r", buffering=-1):
        return _REAL_POPEN(
            _wrap_lock_isolation_shell_command(
                command,
                production_root=production_root,
                test_root=test_root,
                bwrap_fd_path=bwrap_fd_path,
            ),
            mode,
            buffering,
        )

    patch.setattr(os, "popen", isolated_popen_shell)

    if _REAL_POSIX_SPAWN is not None:

        def isolated_posix_spawn(path, args, env, *spawn_args, **spawn_kwargs):
            _validate_lock_isolation_env(env)
            _validate_lock_isolation_posix_spawn_kwargs(spawn_kwargs)
            wrapped_path, wrapped_args = _wrap_lock_isolation_exec_args(
                path,
                args,
                production_root=production_root,
                test_root=test_root,
                bwrap_fd_path=bwrap_fd_path,
            )
            return _REAL_POSIX_SPAWN(
                wrapped_path,
                wrapped_args,
                env,
                *spawn_args,
                **spawn_kwargs,
            )

        patch.setattr(os, "posix_spawn", isolated_posix_spawn)

    if _REAL_POSIX_SPAWNP is not None:

        def isolated_posix_spawnp(path, args, env, *spawn_args, **spawn_kwargs):
            _validate_lock_isolation_env(env)
            _validate_lock_isolation_posix_spawn_kwargs(spawn_kwargs)
            wrapped_path, wrapped_args = _wrap_lock_isolation_exec_args(
                path,
                args,
                production_root=production_root,
                test_root=test_root,
                bwrap_fd_path=bwrap_fd_path,
            )
            return _REAL_POSIX_SPAWNP(
                wrapped_path,
                wrapped_args,
                env,
                *spawn_args,
                **spawn_kwargs,
            )

        patch.setattr(os, "posix_spawnp", isolated_posix_spawnp)

    def isolated_execv(path, args):
        wrapped_path, wrapped_args = _wrap_lock_isolation_exec_args(
            path,
            args,
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path=bwrap_fd_path,
        )
        return _REAL_EXECV(wrapped_path, wrapped_args)

    patch.setattr(os, "execv", isolated_execv)

    def isolated_execve(path, args, env):
        _validate_lock_isolation_env(env)
        wrapped_path, wrapped_args = _wrap_lock_isolation_exec_args(
            path,
            args,
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path=bwrap_fd_path,
        )
        return _REAL_EXECVE(wrapped_path, wrapped_args, env)

    patch.setattr(os, "execve", isolated_execve)

    def isolated_execvp(path, args):
        wrapped_path, wrapped_args = _wrap_lock_isolation_exec_args(
            path,
            args,
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path=bwrap_fd_path,
        )
        return _REAL_EXECVP(wrapped_path, wrapped_args)

    patch.setattr(os, "execvp", isolated_execvp)

    def isolated_execvpe(path, args, env):
        _validate_lock_isolation_env(env)
        wrapped_path, wrapped_args = _wrap_lock_isolation_exec_args(
            path,
            args,
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path=bwrap_fd_path,
        )
        return _REAL_EXECVPE(wrapped_path, wrapped_args, env)

    patch.setattr(os, "execvpe", isolated_execvpe)

    def isolated_spawnv(mode, path, args):
        wrapped_path, wrapped_args = _wrap_lock_isolation_exec_args(
            path,
            args,
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path=bwrap_fd_path,
        )
        return _REAL_SPAWNV(mode, wrapped_path, wrapped_args)

    patch.setattr(os, "spawnv", isolated_spawnv)

    def isolated_spawnve(mode, path, args, env):
        _validate_lock_isolation_env(env)
        wrapped_path, wrapped_args = _wrap_lock_isolation_exec_args(
            path,
            args,
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path=bwrap_fd_path,
        )
        return _REAL_SPAWNVE(mode, wrapped_path, wrapped_args, env)

    patch.setattr(os, "spawnve", isolated_spawnve)

    def isolated_spawnvp(mode, path, args):
        wrapped_path, wrapped_args = _wrap_lock_isolation_exec_args(
            path,
            args,
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path=bwrap_fd_path,
        )
        return _REAL_SPAWNVP(mode, wrapped_path, wrapped_args)

    patch.setattr(os, "spawnvp", isolated_spawnvp)

    def isolated_spawnvpe(mode, path, args, env):
        _validate_lock_isolation_env(env)
        wrapped_path, wrapped_args = _wrap_lock_isolation_exec_args(
            path,
            args,
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path=bwrap_fd_path,
        )
        return _REAL_SPAWNVPE(mode, wrapped_path, wrapped_args, env)

    patch.setattr(os, "spawnvpe", isolated_spawnvpe)

    real_spawnv_passfds = multiprocessing.util.spawnv_passfds

    def isolated_spawnv_passfds(path, args, passfds):
        wrapped_path, wrapped_args, wrapped_passfds = _wrap_codex_usage_spawnv_args(
            path,
            args,
            passfds,
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path=bwrap_fd_path,
            bwrap_pass_fd=bwrap_fd,
        )
        return real_spawnv_passfds(wrapped_path, wrapped_args, wrapped_passfds)

    patch.setattr(multiprocessing.util, "spawnv_passfds", isolated_spawnv_passfds)

    def verify_isolation():
        patch.undo()
        try:
            session_guard.assert_unchanged(
                label="tests wrote persistent locks into product root",
            )
        finally:
            session_guard.close()
            _REAL_CLOSE(bwrap_fd)

    request.addfinalizer(verify_isolation)


@pytest.fixture(autouse=True)
def verify_test_lock_isolation(request):
    if request.config._private_lock_outer_isolation_proof is not None:
        return
    production_root = request.config._private_lock_production_root
    guard = _open_lock_namespace_guard(
        production_root,
        label="test wrote persistent locks into product root",
        allow_root_time_drift=True,
    )
    request.config._private_lock_current_test_guard = guard

    def verify_test():
        try:
            guard.assert_unchanged(
                label="test wrote persistent locks into product root",
            )
        finally:
            guard.close()
            if getattr(request.config, "_private_lock_current_test_guard", None) is guard:
                delattr(request.config, "_private_lock_current_test_guard")

    request.addfinalizer(verify_test)


@pytest.fixture(autouse=True)
def isolate_cli_data_home(tmp_path_factory, monkeypatch, request):
    """Keep CLI tests from writing into the invoking user's data directory."""
    module_name = request.module.__name__.rsplit(".", 1)[-1]
    if module_name in {"test_cli", "test_history_cli", "test_profile_cli"}:
        root = tmp_path_factory.mktemp("cli-xdg")
        monkeypatch.setenv("XDG_DATA_HOME", str(root / "data"))
        monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
