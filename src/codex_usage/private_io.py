from __future__ import annotations

import ctypes
import errno
import fcntl
import glob
import hashlib
import json
import math
import os
import pwd
import re
import secrets
import stat
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

PRIVATE_LOCK_TIMEOUT_SECONDS = 30
_PRIVATE_LOCK_MAX_BYTES = 4096
_PRIVATE_LOCK_NAMESPACE_MAX_ENTRIES = 65_536
_RENAME_NOREPLACE = 1
_PATH_TYPE = type(Path())
_MAX_STALE_ROLLBACKS = 1
_MAX_PRIVATE_ROLLBACK_BYTES = 64 * 1024 * 1024
_PRIVATE_LOCK_STATE = threading.local()
_CANONICAL_LOCK_NAME_RE = re.compile(r"[0-9a-f]{64}\.lock")
_MOVED_LOCK_NAME_RE = re.compile(r"([0-9a-f]{64}\.lock)\.moved(?:-nested)?")
_OPERATOR_APPROVED_LOCK_RESIDUE_REASONS = frozenset(
    (
        "operator-approved-empty-directory-lock",
        "operator-approved-dangling-symlink-lock",
    )
)


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    gid: int = -1
    uid: int = -1
    ctime_ns: int = -1


@dataclass(frozen=True)
class _DirectoryAncestorIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    ctime_ns: int


class IntegrationEvidenceError(Exception):
    pass


class IntegrationEvidenceInvalid(IntegrationEvidenceError):
    pass


class IntegrationEvidenceUnavailable(IntegrationEvidenceError):
    pass


@dataclass(frozen=True)
class PrivateLockSnapshot:
    file_type: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int
    symlink_target: str | None = None
    content_sha256: str | None = None


@dataclass(frozen=True)
class PrivateLockResidueApproval:
    name: str
    reason: str
    snapshot: PrivateLockSnapshot


@dataclass(frozen=True)
class PrivateLockNamespaceIssue:
    name: str
    reason: str
    snapshot: PrivateLockSnapshot
    canonical_sibling_name: str | None = None


@dataclass(frozen=True)
class PrivateLockNamespaceReport:
    lock_root: Path
    root_snapshot: PrivateLockSnapshot
    issues: tuple[PrivateLockNamespaceIssue, ...]


@dataclass(frozen=True)
class PrivateLockQuarantineEntry:
    original_name: str
    quarantine_name: str
    file_type: str
    snapshot: PrivateLockSnapshot


@dataclass(frozen=True)
class PrivateLockQuarantineResult:
    lock_root: Path
    lock_root_snapshot: PrivateLockSnapshot
    quarantine_root: Path
    quarantine_root_snapshot: PrivateLockSnapshot
    approval_hash: str
    quarantined: tuple[PrivateLockQuarantineEntry, ...]


class PrivateLockPartialCommitError(ValueError):
    def __init__(
        self,
        *,
        primary_error: BaseException,
        lock_root_snapshot: PrivateLockSnapshot | None,
        quarantine_root_snapshot: PrivateLockSnapshot | None,
        committed: tuple[PrivateLockQuarantineEntry, ...],
        unmoved: tuple[PrivateLockNamespaceIssue, ...],
        unverified: tuple[PrivateLockNamespaceIssue, ...] = (),
    ) -> None:
        self.primary_error = primary_error
        self.lock_root_snapshot = lock_root_snapshot
        self.quarantine_root_snapshot = quarantine_root_snapshot
        self.committed = committed
        self.unmoved = unmoved
        self.unverified = unverified
        committed_names = ",".join(entry.original_name for entry in committed) or "-"
        unmoved_names = ",".join(issue.name for issue in unmoved) or "-"
        unverified_names = ",".join(issue.name for issue in unverified) or "-"
        super().__init__(
            "private lock quarantine partially committed: "
            f"committed={committed_names}; unmoved={unmoved_names}; "
            f"unverified={unverified_names}; "
            f"primary={primary_error}"
        )


@dataclass(frozen=True)
class _HeldPrivatePathLock:
    root_identities: tuple[FileIdentity, ...]
    lock_identity: tuple[int, ...]


def _private_lock_root() -> Path:
    return _private_lock_root_from_passwd()


def _private_lock_root_from_passwd() -> Path:
    try:
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ValueError("cannot determine private lock home") from exc
    if not home.is_absolute():
        raise ValueError("private lock home must be absolute")
    return home / ".local" / "state" / "codex-usage" / "locks"


def _private_lock_path(path: Path) -> Path:
    root = _private_lock_root()
    ensure_private_directory(root, label="private lock directory")
    return root / _private_lock_name(path)


def _open_or_create_private_lock_root(
    lock_root: Path,
) -> tuple[int, tuple[FileIdentity, ...]]:
    try:
        return _open_existing_private_lock_root(lock_root)
    except FileNotFoundError:
        return _create_private_lock_root_under_pinned_parent(lock_root)


def _create_private_lock_root_under_pinned_parent(
    lock_root: Path,
) -> tuple[int, tuple[FileIdentity, ...]]:
    lock_root = _require_path(lock_root, label="private lock directory")
    if (
        not lock_root.is_absolute()
        or any(part in {"", ".", ".."} for part in lock_root.parts[1:])
    ):
        raise ValueError("private lock directory must be an absolute normalized path")
    root_name = _safe_component(lock_root.name)
    parent = lock_root.parent
    ensure_private_directory(parent, label="private lock parent directory")
    parent_fd = -1
    root_fd = -1
    path_fd = -1
    temp_name = _safe_component(
        f".{root_name}.create-{os.getpid()}-{secrets.token_hex(16)}"
    )
    temp_created = False
    published = False
    try:
        parent_fd, _parent_identities = _open_existing_private_lock_root(
            parent,
            validate_namespace=False,
        )
        parent_identity = _require_private_directory_fd(parent_fd)
        try:
            os.mkdir(temp_name, 0o700, dir_fd=parent_fd)
            temp_created = True
        except FileExistsError as exc:
            raise ValueError("private lock namespace cannot be validated") from exc
        root_fd = os.open(temp_name, _directory_open_flags(), dir_fd=parent_fd)
        root_identity = _require_private_directory_fd(root_fd)
        _raise_for_private_lock_namespace_issues(lock_root, root_fd)
        parent_identity = _refresh_private_directory_identity_after_mutation(
            (parent_identity,),
            parent_fd,
            label="private lock parent directory",
        )[-1]
        try:
            _rename_private_lock_residue_no_replace(
                source_fd=parent_fd,
                source_name=temp_name,
                destination_fd=parent_fd,
                destination_name=root_name,
            )
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                _remove_created_private_directory_at(parent_fd, temp_name)
                temp_created = False
                os.close(root_fd)
                root_fd = -1
                try:
                    return _open_existing_private_lock_root(lock_root)
                except ValueError as value_error:
                    raise ValueError(
                        "private lock namespace cannot be validated"
                    ) from value_error
            raise
        published = True
        root_identity = _refresh_private_directory_identity_after_mutation(
            (root_identity,),
            root_fd,
            label="private lock namespace",
        )[-1]
        path_fd, path_identities = _open_existing_private_lock_root(lock_root)
        path_identity = _require_private_directory_fd(path_fd)
        if (
            path_identity != root_identity
            or not path_identities
            or path_identities[-1] != root_identity
        ):
            raise ValueError("private lock namespace cannot be validated")
        _refresh_private_directory_identity_after_mutation(
            (parent_identity,),
            parent_fd,
            label="private lock parent directory",
        )
        result = root_fd
        root_fd = -1
        return result, path_identities
    except Exception as exc:
        if temp_created and not published:
            try:
                _remove_created_private_directory_at(parent_fd, temp_name)
            except Exception as cleanup_error:
                raise ValueError(
                    "private lock namespace cannot be validated"
                ) from cleanup_error
        raise ValueError("private lock namespace cannot be validated") from exc
    finally:
        if path_fd >= 0:
            os.close(path_fd)
        if root_fd >= 0:
            os.close(root_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _remove_created_private_directory_at(parent_fd: int, name: str) -> None:
    if parent_fd < 0:
        return
    component = _safe_component(name)
    fd = -1
    try:
        fd = os.open(component, _directory_open_flags(), dir_fd=parent_fd)
        _require_private_directory_fd(fd)
    finally:
        if fd >= 0:
            os.close(fd)
    os.rmdir(component, dir_fd=parent_fd)


def _private_lock_name(path: Path) -> str:
    absolute = os.path.abspath(path)
    digest = hashlib.sha256(os.fsencode(absolute)).hexdigest()
    return f"{digest}.lock"


def _lock_deadline(timeout_seconds: int | float) -> float:
    error = "lock timeout must be a non-negative finite number"
    if type(timeout_seconds) not in (int, float):
        raise ValueError(error)
    try:
        seconds = float(timeout_seconds)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(error) from None
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(error)
    deadline = time.monotonic() + seconds
    if not math.isfinite(deadline):
        raise ValueError(error)
    return deadline


def _require_private_directory(path: Path, *, label: str) -> None:
    try:
        item = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} must be a real directory: {path}") from exc
    if (
        not stat.S_ISDIR(item.st_mode)
        or stat.S_ISLNK(item.st_mode)
        or item.st_uid != os.geteuid()
    ):
        raise ValueError(f"{label} must be a private user-owned directory: {path}")


def _chmod_private_directory(path: Path, *, label: str) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = -1
    try:
        fd = os.open(path, flags)
        item = os.fstat(fd)
        if not stat.S_ISDIR(item.st_mode) or item.st_uid != os.geteuid():
            raise ValueError(f"{label} must be a private user-owned directory: {path}")
        os.fchmod(fd, 0o700)
    finally:
        if fd >= 0:  # pragma: no branch - exception unwind
            os.close(fd)


def _verify_private_directory_path_identity(
    path: Path,
    *,
    label: str,
    expected: FileIdentity | None = None,
    secure: bool = False,
) -> FileIdentity:
    fd, identity = _open_private_directory_path_identity(
        path,
        label=label,
        expected=expected,
        secure=secure,
    )
    try:
        return identity
    finally:
        os.close(fd)


def _open_private_directory_path_chain(
    path: Path,
    *,
    label: str,
    expected: FileIdentity | None = None,
    secure: bool = False,
    allow_public_leaf: bool = False,
) -> tuple[list[int], tuple[FileIdentity, ...]]:
    raw_path = _require_path(path, label=label)
    absolute = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
    if (
        not absolute.is_absolute()
        or any(part in {"", ".", ".."} for part in absolute.parts[1:])
    ):
        raise ValueError(f"{label} must be an absolute normalized path: {path}")
    flags = _directory_open_flags()
    fds: list[int] = []
    try:
        try:
            fds.append(os.open(absolute.anchor, flags))
            for component in absolute.parts[1:]:
                fds.append(os.open(_safe_component(component), flags, dir_fd=fds[-1]))
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ValueError(f"{label} contains an unsafe component") from exc
            raise ValueError(
                f"{label} must be a private user-owned directory: {path}"
            ) from exc
        if not fds:
            raise ValueError(f"{label} must be a real directory: {path}")
        item = os.fstat(fds[-1])
        if not stat.S_ISDIR(item.st_mode) or item.st_uid != os.geteuid():
            raise ValueError(f"{label} must be a private user-owned directory: {path}")
        mode = stat.S_IMODE(item.st_mode)
        if mode & 0o022 and not allow_public_leaf:
            raise ValueError(f"{label} must be a private user-owned directory: {path}")
        if expected is not None and (
            item.st_dev != expected.device
            or item.st_ino != expected.inode
        ):
            raise ValueError(f"{label} changed before final validation")
        if secure and mode != 0o700:
            raise ValueError(f"{label} must be a private user-owned directory: {path}")
        identity = _directory_identity(item)
        if expected is not None:
            if identity.device != expected.device or identity.inode != expected.inode:
                raise ValueError(f"{label} changed before final validation")
            if not secure and identity.mode != expected.mode:
                raise ValueError(f"{label} changed before final validation")
        identities = tuple(_directory_identity(os.fstat(fd)) for fd in fds)
        result_fds = fds
        fds = []
        return result_fds, identities
    except ValueError:
        raise
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _open_private_directory_path_identity(
    path: Path,
    *,
    label: str,
    expected: FileIdentity | None = None,
    secure: bool = False,
    allow_public_leaf: bool = False,
) -> tuple[int, FileIdentity]:
    fds, identities = _open_private_directory_path_chain(
        path,
        label=label,
        expected=expected,
        secure=secure,
        allow_public_leaf=allow_public_leaf,
    )
    try:
        result_fd = fds.pop()
        return result_fd, identities[-1]
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _verify_private_directory_path_chain_binding(
    path: Path,
    *,
    label: str,
    expected: tuple[FileIdentity, ...],
) -> None:
    fds: list[int] = []
    try:
        fds, identities = _open_private_directory_path_chain(
            path,
            label=label,
            expected=expected[-1],
        )
        if identities != expected:
            raise ValueError(f"{label} changed before final validation")
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _before_private_directory_final_recheck(_path: Path) -> None:
    return None


def assert_no_symlink_ancestors(path: Path, *, label: str) -> None:
    raw_path = _require_path(path, label=label)
    absolute = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        if part == ".":
            continue
        if part == "..":
            current = current.parent
            continue
        current /= part
        if current.is_symlink():
            raise ValueError(f"{label} must not contain symlink ancestors: {current}")


def ensure_private_directory(
    path: Path,
    *,
    label: str,
    created_paths: list[tuple[Path, int, int]] | None = None,
) -> Path:
    """Create private directory path without weakening existing parents."""
    if created_paths is not None and not isinstance(created_paths, list):
        raise ValueError("created_paths is invalid")
    raw_path = _require_path(path, label=label)
    assert_no_symlink_ancestors(raw_path, label=label)
    if raw_path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {raw_path}")
    absolute = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
    try:
        protected = {Path("/").resolve(), Path.home().resolve()}
        normalized = absolute.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} cannot be resolved safely: {raw_path}") from exc
    if normalized in protected:
        raise ValueError(f"{label} must not be a protected directory: {raw_path}")

    missing: list[Path] = []
    current = raw_path
    while True:
        try:
            current_item = current.lstat()
        except AttributeError as exc:
            if current.is_symlink():
                raise ValueError(f"{label} must not be a symlink: {current}") from exc
            parent = current.parent
            if parent == current:
                raise ValueError(
                    f"{label} has no usable directory parent: {raw_path}"
                ) from exc
            if not current.exists():
                missing.append(current)
                current = parent
                continue
            raise ValueError(f"{label} must be a real directory: {current}") from exc
        except FileNotFoundError:
            missing.append(current)
            parent = current.parent
            if parent == current:
                raise ValueError(
                    f"{label} has no usable directory parent: {raw_path}"
                ) from None
            current = parent
            continue
        except OSError as exc:
            raise ValueError(f"{label} must be a real directory: {current}") from exc
        if stat.S_ISLNK(current_item.st_mode) or not stat.S_ISDIR(current_item.st_mode):
            raise ValueError(f"{label} must be a real directory: {current}")
        break

    held_fds: list[int] = []
    held_identities: list[FileIdentity] = []
    child_fd = -1
    try:
        if missing:
            held_fds, existing_identities = _open_private_directory_path_chain(
                current,
                label=label,
                expected=_directory_identity(current_item),
                allow_public_leaf=True,
            )
            held_identities.extend(existing_identities)
            for candidate in reversed(missing):
                component = _safe_component(candidate.name)
                created = False
                try:
                    os.mkdir(component, 0o700, dir_fd=held_fds[-1])
                    created = True
                except FileExistsError:
                    pass
                try:
                    child_fd = os.open(
                        component,
                        _directory_open_flags(),
                        dir_fd=held_fds[-1],
                    )
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise ValueError(f"{label} contains an unsafe component") from exc
                    raise
                child_item = os.fstat(child_fd)
                if (
                    not stat.S_ISDIR(child_item.st_mode)
                    or child_item.st_uid != os.geteuid()
                ):
                    raise ValueError(
                        f"{label} must be a private user-owned directory: {candidate}"
                    )
                if created:
                    os.fchmod(child_fd, 0o700)
                    secured_child = os.fstat(child_fd)
                    if (
                        not stat.S_ISDIR(secured_child.st_mode)
                        or secured_child.st_uid != os.geteuid()
                        or stat.S_IMODE(secured_child.st_mode) != 0o700
                        or secured_child.st_dev != child_item.st_dev
                        or secured_child.st_ino != child_item.st_ino
                    ):
                        raise ValueError(
                            f"{label} must be a private user-owned directory: {candidate}"
                        )
                    child_item = secured_child
                elif stat.S_IMODE(child_item.st_mode) != 0o700:
                    raise ValueError(
                        f"{label} must be a private user-owned directory: {candidate}"
                    )
                if created and created_paths is not None:
                    created_paths.append((candidate, child_item.st_dev, child_item.st_ino))
                parent_before = held_identities[-1]
                parent_after = _directory_identity(os.fstat(held_fds[-1]))
                if (
                    parent_after.device != parent_before.device
                    or parent_after.inode != parent_before.inode
                    or parent_after.mode != parent_before.mode
                    or parent_after.uid != parent_before.uid
                    or parent_after.gid != parent_before.gid
                ):
                    raise ValueError(f"{label} changed while creating child directory")
                if created or parent_after != parent_before:
                    held_identities[-1] = parent_after
                held_identities.append(_directory_identity(child_item))
                held_fds.append(child_fd)
                child_fd = -1
        else:
            held_fds, existing_identities = _open_private_directory_path_chain(
                raw_path,
                label=label,
                expected=_directory_identity(current_item),
                secure=True,
            )
            held_identities.extend(existing_identities)

        _before_private_directory_final_recheck(raw_path)
        _verify_private_directory_path_chain_binding(
            raw_path,
            label=label,
            expected=tuple(held_identities),
        )
        return raw_path
    finally:
        if child_fd >= 0:
            os.close(child_fd)
        for fd in reversed(held_fds):
            os.close(fd)


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _regular_open_flags(*, writable: bool = False, noatime: bool = False) -> int:
    flags = os.O_WRONLY if writable else os.O_RDONLY
    result = (
        flags
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if noatime:
        result |= getattr(os, "O_NOATIME", 0)
    return result


def _safe_component(name: object) -> str:
    if (
        type(name) is not str
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise ValueError("private path component is invalid")
    return name


def _directory_identity(item: os.stat_result) -> FileIdentity:
    return FileIdentity(
        item.st_dev,
        item.st_ino,
        stat.S_IMODE(item.st_mode),
        gid=item.st_gid,
        uid=item.st_uid,
        ctime_ns=item.st_ctime_ns,
    )


def _directory_ancestor_identity(item: os.stat_result) -> _DirectoryAncestorIdentity:
    return _DirectoryAncestorIdentity(
        device=item.st_dev,
        inode=item.st_ino,
        mode=stat.S_IMODE(item.st_mode),
        uid=item.st_uid,
        gid=item.st_gid,
        ctime_ns=item.st_ctime_ns,
    )


def _require_private_directory_fd(fd: int) -> FileIdentity:
    try:
        item = os.fstat(fd)
    except OSError as exc:
        raise ValueError("private directory descriptor is invalid") from exc
    if (
        not stat.S_ISDIR(item.st_mode)
        or item.st_uid != os.geteuid()
        or stat.S_IMODE(item.st_mode) != 0o700
    ):
        raise ValueError("private directory descriptor is invalid")
    return _directory_identity(item)


def _require_trusted_directory_fd(fd: int, *, root_uid: int) -> FileIdentity:
    try:
        item = os.fstat(fd)
    except OSError as exc:
        raise ValueError("trusted directory descriptor is invalid") from exc
    mode = stat.S_IMODE(item.st_mode)
    root_sticky = item.st_uid == root_uid and bool(item.st_mode & stat.S_ISVTX)
    if not stat.S_ISDIR(item.st_mode):
        raise ValueError("trusted directory descriptor is not a directory")
    if item.st_uid not in {root_uid, os.geteuid()}:
        raise ValueError("trusted directory descriptor has an invalid owner")
    if bool(mode & 0o022) and not root_sticky:
        raise ValueError("trusted directory descriptor has an invalid mode")
    return _directory_identity(item)


def _open_existing_private_lock_root(
    lock_root: Path,
    *,
    validate_namespace: bool = True,
) -> tuple[int, tuple[FileIdentity, ...]]:
    lock_root = _require_path(lock_root, label="private lock directory")
    if (
        not lock_root.is_absolute()
        or any(part in {"", ".", ".."} for part in lock_root.parts[1:])
    ):
        raise ValueError("private lock directory must be an absolute normalized path")
    try:
        passwd_home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ValueError("cannot determine private lock home") from exc
    enforce_from = len(lock_root.parts) - 1
    if lock_root.parts[: len(passwd_home.parts)] == passwd_home.parts:
        enforce_from = len(passwd_home.parts) - 1
    flags = _directory_open_flags()
    current_fd = os.open(lock_root.anchor, flags)
    identities: list[FileIdentity] = []
    try:
        for index, component in enumerate(lock_root.parts[1:], start=1):
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError("private lock directory is unsafe") from exc
                raise
            os.close(current_fd)
            current_fd = next_fd
            if index >= enforce_from:
                identities.append(_require_private_directory_fd(current_fd))
        if validate_namespace:
            _raise_for_private_lock_namespace_issues(lock_root, current_fd)
        result = current_fd
        current_fd = -1
        return result, tuple(identities)
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _private_lock_file_identity(item: os.stat_result) -> tuple[int, ...]:
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_uid != os.geteuid()
        or stat.S_IMODE(item.st_mode) != 0o600
        or item.st_nlink != 1
        or item.st_size > _PRIVATE_LOCK_MAX_BYTES
    ):
        raise ValueError("private lock file is invalid")
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_uid,
        item.st_nlink,
        item.st_size,
    )


def _private_lock_file_type(item: os.stat_result) -> str:
    if stat.S_ISREG(item.st_mode):
        return "regular"
    if stat.S_ISDIR(item.st_mode):
        return "directory"
    if stat.S_ISLNK(item.st_mode):
        return "symlink"
    return "other"


def _private_lock_regular_content_sha256_at(
    root_fd: int,
    name: str,
    expected: os.stat_result,
) -> str | None:
    if not stat.S_ISREG(expected.st_mode) or expected.st_size > _PRIVATE_LOCK_MAX_BYTES:
        return None
    fd = -1
    try:
        fd = os.open(
            _safe_component(name),
            _regular_open_flags(noatime=True),
            dir_fd=root_fd,
        )
        initial = os.fstat(fd)
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
            raise ValueError("private lock file changed during scan")
        payload = bytearray()
        while len(payload) <= _PRIVATE_LOCK_MAX_BYTES:
            chunk = os.read(
                fd,
                min(65_536, _PRIVATE_LOCK_MAX_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _PRIVATE_LOCK_MAX_BYTES:
            return None
        final = os.fstat(fd)
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
            raise ValueError("private lock file changed during scan")
        return hashlib.sha256(bytes(payload)).hexdigest()
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return None
        raise ValueError("private lock namespace cannot be scanned") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _private_lock_snapshot_at(root_fd: int, name: str) -> PrivateLockSnapshot:
    component = _safe_component(name)
    try:
        item = os.stat(component, dir_fd=root_fd, follow_symlinks=False)
        target = (
            os.readlink(component, dir_fd=root_fd)
            if stat.S_ISLNK(item.st_mode)
            else None
        )
        content_sha256 = _private_lock_regular_content_sha256_at(
            root_fd,
            component,
            item,
        )
    except OSError as exc:
        raise ValueError("private lock namespace cannot be scanned") from exc
    return PrivateLockSnapshot(
        file_type=_private_lock_file_type(item),
        device=item.st_dev,
        inode=item.st_ino,
        mode=item.st_mode,
        uid=item.st_uid,
        gid=item.st_gid,
        nlink=item.st_nlink,
        size=item.st_size,
        mtime_ns=item.st_mtime_ns,
        ctime_ns=item.st_ctime_ns,
        symlink_target=target,
        content_sha256=content_sha256,
    )


def _snapshot_from_stat(item: os.stat_result) -> PrivateLockSnapshot:
    return PrivateLockSnapshot(
        file_type=_private_lock_file_type(item),
        device=item.st_dev,
        inode=item.st_ino,
        mode=item.st_mode,
        uid=item.st_uid,
        gid=item.st_gid,
        nlink=item.st_nlink,
        size=item.st_size,
        mtime_ns=item.st_mtime_ns,
        ctime_ns=item.st_ctime_ns,
        symlink_target=None,
        content_sha256=None,
    )


def _private_lock_snapshot_dict(snapshot: PrivateLockSnapshot) -> dict[str, object]:
    return {
        "ctime_ns": snapshot.ctime_ns,
        "content_sha256": snapshot.content_sha256,
        "device": snapshot.device,
        "file_type": snapshot.file_type,
        "gid": snapshot.gid,
        "inode": snapshot.inode,
        "mode": snapshot.mode,
        "mtime_ns": snapshot.mtime_ns,
        "nlink": snapshot.nlink,
        "size": snapshot.size,
        "symlink_target": snapshot.symlink_target,
        "uid": snapshot.uid,
    }


def _private_lock_issue_dict(issue: PrivateLockNamespaceIssue) -> dict[str, object]:
    return {
        "canonical_sibling_name": issue.canonical_sibling_name,
        "name": issue.name,
        "reason": issue.reason,
        "snapshot": _private_lock_snapshot_dict(issue.snapshot),
    }


def _normalize_private_lock_residue_approvals(
    approved_residues: Iterable[PrivateLockResidueApproval] | None,
) -> dict[str, PrivateLockResidueApproval]:
    if approved_residues is None:
        return {}
    try:
        iterator = iter(approved_residues)
    except TypeError as exc:
        raise ValueError("private lock residue approvals are invalid") from exc
    approvals: dict[str, PrivateLockResidueApproval] = {}
    for approval in iterator:
        if type(approval) is not PrivateLockResidueApproval:
            raise ValueError("private lock residue approval is invalid")
        try:
            name = _safe_component(approval.name)
        except ValueError as exc:
            raise ValueError("private lock residue approval name is invalid") from exc
        if name != approval.name:
            raise ValueError("private lock residue approval name is invalid")
        if approval.reason not in _OPERATOR_APPROVED_LOCK_RESIDUE_REASONS:
            raise ValueError("private lock residue approval reason is invalid")
        if type(approval.snapshot) is not PrivateLockSnapshot:
            raise ValueError("private lock residue approval snapshot is invalid")
        if name in approvals:
            raise ValueError("private lock residue approval is duplicated")
        approvals[name] = approval
    return approvals


def _valid_private_lock_snapshot(snapshot: PrivateLockSnapshot) -> bool:
    return (
        snapshot.file_type == "regular"
        and snapshot.uid == os.geteuid()
        and stat.S_IMODE(snapshot.mode) == 0o600
        and snapshot.nlink == 1
        and snapshot.size <= _PRIVATE_LOCK_MAX_BYTES
    )


def _valid_empty_private_lock_snapshot(snapshot: PrivateLockSnapshot) -> bool:
    return _valid_private_lock_snapshot(snapshot) and snapshot.size == 0


def _directory_empty_at(
    parent_fd: int,
    name: str,
    *,
    allowed_modes: frozenset[int],
) -> bool:
    fd = -1
    try:
        fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
        item = os.fstat(fd)
        if (
            not stat.S_ISDIR(item.st_mode)
            or item.st_uid != os.geteuid()
            or stat.S_IMODE(item.st_mode) not in allowed_modes
        ):
            return False
        with os.scandir(fd) as entries:
            return next(entries, None) is None
    except OSError:
        return False
    finally:
        if fd >= 0:
            os.close(fd)


def _operator_approved_directory_residue_at(
    root_fd: int,
    name: str,
    snapshot: PrivateLockSnapshot,
) -> bool:
    return (
        snapshot.file_type == "directory"
        and snapshot.uid == os.geteuid()
        and stat.S_IMODE(snapshot.mode) in {0o700, 0o755}
        and _directory_empty_at(
            root_fd,
            name,
            allowed_modes=frozenset((0o700, 0o755)),
        )
    )


def _operator_approved_dangling_symlink_residue_at(
    root_fd: int,
    name: str,
    snapshot: PrivateLockSnapshot,
) -> bool:
    if (
        snapshot.file_type != "symlink"
        or snapshot.uid != os.geteuid()
        or snapshot.nlink != 1
        or not isinstance(snapshot.symlink_target, str)
    ):
        return False
    try:
        os.stat(name, dir_fd=root_fd)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _operator_approved_private_lock_issue_at(
    root_fd: int,
    name: str,
    snapshot: PrivateLockSnapshot,
    approval: PrivateLockResidueApproval,
) -> PrivateLockNamespaceIssue:
    if not _CANONICAL_LOCK_NAME_RE.fullmatch(name):
        raise ValueError("private lock residue approval requires a canonical name")
    if approval.snapshot != snapshot:
        raise ValueError("private lock residue approval does not match current snapshot")
    if approval.reason == "operator-approved-empty-directory-lock":
        if _operator_approved_directory_residue_at(root_fd, name, snapshot):
            return PrivateLockNamespaceIssue(
                name=name,
                reason=approval.reason,
                snapshot=snapshot,
            )
    elif approval.reason == "operator-approved-dangling-symlink-lock":
        if _operator_approved_dangling_symlink_residue_at(root_fd, name, snapshot):
            return PrivateLockNamespaceIssue(
                name=name,
                reason=approval.reason,
                snapshot=snapshot,
            )
    raise ValueError("private lock residue approval does not match residue type")


def _classify_private_lock_issue_at(
    root_fd: int,
    name: str,
    snapshot: PrivateLockSnapshot,
    *,
    approved_residue: PrivateLockResidueApproval | None = None,
) -> PrivateLockNamespaceIssue | None:
    if approved_residue is not None and not _CANONICAL_LOCK_NAME_RE.fullmatch(name):
        raise ValueError("private lock residue approval requires a canonical name")
    if (
        _CANONICAL_LOCK_NAME_RE.fullmatch(name)
        and _valid_private_lock_snapshot(snapshot)
    ):
        if approved_residue is not None:
            raise ValueError("private lock residue approval does not match a residue")
        return None
    moved = _MOVED_LOCK_NAME_RE.fullmatch(name)
    if moved is not None and _valid_empty_private_lock_snapshot(snapshot):
        if approved_residue is not None:
            raise ValueError("private lock residue approval requires a canonical name")
        canonical_sibling_name = moved.group(1)
        try:
            sibling = _private_lock_snapshot_at(root_fd, canonical_sibling_name)
        except ValueError:
            sibling = None
        if sibling is not None and _valid_private_lock_snapshot(sibling):
            return PrivateLockNamespaceIssue(
                name=name,
                reason="approved-empty-moved-lock",
                snapshot=snapshot,
                canonical_sibling_name=canonical_sibling_name,
            )
    if _CANONICAL_LOCK_NAME_RE.fullmatch(name):
        if approved_residue is not None:
            return _operator_approved_private_lock_issue_at(
                root_fd,
                name,
                snapshot,
                approved_residue,
            )
    return PrivateLockNamespaceIssue(
        name=name,
        reason="unsupported-private-lock-residue",
        snapshot=snapshot,
    )


def _scan_private_lock_namespace_fd(
    lock_root: Path,
    root_fd: int,
    *,
    approved_residues: Iterable[PrivateLockResidueApproval] | None = None,
) -> PrivateLockNamespaceReport:
    residue_approvals = _normalize_private_lock_residue_approvals(approved_residues)
    try:
        root_snapshot = _snapshot_from_stat(os.fstat(root_fd))
        if (
            root_snapshot.file_type != "directory"
            or root_snapshot.uid != os.geteuid()
            or stat.S_IMODE(root_snapshot.mode) != 0o700
        ):
            raise ValueError("private lock namespace root is invalid")
        names = _bounded_private_lock_namespace_names(root_fd)
    except OSError as exc:
        raise ValueError("private lock namespace cannot be scanned") from exc
    snapshots = tuple(
        (name, _private_lock_snapshot_at(root_fd, name))
        for name in names
    )
    issues: list[PrivateLockNamespaceIssue] = []
    consumed_approvals: set[str] = set()
    for name, snapshot in snapshots:
        approved_residue = residue_approvals.get(name)
        issue = _classify_private_lock_issue_at(
            root_fd,
            name,
            snapshot,
            approved_residue=approved_residue,
        )
        if approved_residue is not None:
            consumed_approvals.add(name)
        if issue is not None:
            issues.append(issue)
    if residue_approvals.keys() - consumed_approvals:
        raise ValueError("private lock residue approval did not match current namespace")
    return PrivateLockNamespaceReport(
        lock_root=lock_root,
        root_snapshot=root_snapshot,
        issues=tuple(issues),
    )


def _raise_for_private_lock_namespace_issues(
    lock_root: Path,
    root_fd: int,
) -> None:
    report = _scan_private_lock_namespace_fd(lock_root, root_fd)
    if report.issues:
        raise ValueError("private lock namespace contains noncanonical entries")


def _bounded_private_lock_namespace_names(root_fd: int) -> tuple[str, ...]:
    names: list[str] = []
    try:
        with os.scandir(root_fd) as entries:
            for entry in entries:
                if len(names) >= _PRIVATE_LOCK_NAMESPACE_MAX_ENTRIES:
                    raise ValueError("private lock namespace contains too many entries")
                names.append(_safe_component(entry.name))
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("private lock namespace cannot be scanned") from exc
    return tuple(sorted(names))


def scan_private_lock_namespace(
    lock_root: Path | None = None,
    *,
    approved_residues: Iterable[PrivateLockResidueApproval] | None = None,
) -> PrivateLockNamespaceReport:
    selected_root = _private_lock_root() if lock_root is None else lock_root
    root_fd = -1
    try:
        root_fd, _root_identities = _open_existing_private_lock_root(
            selected_root,
            validate_namespace=False,
        )
        return _scan_private_lock_namespace_fd(
            selected_root,
            root_fd,
            approved_residues=approved_residues,
        )
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def _require_private_lock_quarantine_root(
    report: PrivateLockNamespaceReport,
    quarantine_root: Path,
) -> Path:
    if not isinstance(quarantine_root, Path):
        raise ValueError("private lock quarantine path is invalid")
    if (
        not quarantine_root.is_absolute()
        or any(part in {"", ".", ".."} for part in quarantine_root.parts[1:])
        or quarantine_root == report.lock_root
        or report.lock_root in quarantine_root.parents
        or quarantine_root.parent != report.lock_root.parent
    ):
        raise ValueError("private lock quarantine must be outside the lock root")
    return quarantine_root


def _private_lock_quarantine_targets(
    report: PrivateLockNamespaceReport,
    quarantine_root: Path,
) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "original_name": issue.name,
            "quarantine_name": _safe_quarantine_name(issue),
            "quarantine_path": str(quarantine_root / _safe_quarantine_name(issue)),
        }
        for issue in report.issues
    )


def private_lock_namespace_approval_hash(
    report: PrivateLockNamespaceReport,
    *,
    quarantine_root: Path | None = None,
) -> str:
    if type(report) is not PrivateLockNamespaceReport:
        raise ValueError("private lock namespace report is invalid")
    payload = {
        "issues": [_private_lock_issue_dict(issue) for issue in report.issues],
        "lock_root": str(report.lock_root),
        "root_snapshot": _private_lock_snapshot_dict(report.root_snapshot),
    }
    if quarantine_root is not None:
        selected_quarantine_root = _require_private_lock_quarantine_root(
            report,
            quarantine_root,
        )
        payload["quarantine_root"] = str(selected_quarantine_root)
        payload["quarantine_targets"] = _private_lock_quarantine_targets(
            report,
            selected_quarantine_root,
        )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _safe_quarantine_name(issue: PrivateLockNamespaceIssue) -> str:
    seed = json.dumps(
        _private_lock_issue_dict(issue),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"{issue.name}.quarantine-{hashlib.sha256(seed).hexdigest()[:32]}"


def _before_private_lock_reconcile_commit(
    _original_name: str,
    _quarantine_name: str,
) -> None:
    return None


def _before_private_lock_reconcile_rename(
    _original_name: str,
    _quarantine_name: str,
) -> None:
    return None


def _before_private_lock_reconcile_return(
    _root_fd: int,
    _quarantine_fd: int,
    _entries: list[PrivateLockQuarantineEntry],
) -> None:
    return None


def _close_private_lock_reconcile_descriptors(*fds: int) -> None:
    close_errors: list[BaseException] = []
    for fd in fds:
        if fd < 0:
            continue
        try:
            os.close(fd)
        except BaseException as exc:
            close_errors.append(exc)
    if not close_errors:
        return
    if len(close_errors) == 1:
        raise close_errors[0]
    group_type = (
        ExceptionGroup
        if all(isinstance(error, Exception) for error in close_errors)
        else BaseExceptionGroup
    )
    raise group_type("could not close private lock reconcile descriptors", close_errors)


def _base_exception_leaves(errors: Iterable[BaseException]) -> list[BaseException]:
    leaves: list[BaseException] = []
    for error in errors:
        if isinstance(error, BaseExceptionGroup):
            leaves.extend(_base_exception_leaves(error.exceptions))
        else:
            leaves.append(error)
    return leaves


def _raise_private_lock_cleanup_errors(
    label: str,
    primary_error: BaseException | None,
    cleanup_errors: list[BaseException],
) -> None:
    if not cleanup_errors:
        return
    errors = _base_exception_leaves(
        [*cleanup_errors]
        if primary_error is None
        else [primary_error, *cleanup_errors]
    )
    if len(errors) == 1:
        raise errors[0]
    message = f"{label} cleanup failed"
    if all(isinstance(error, Exception) for error in errors):
        raise ExceptionGroup(message, errors) from primary_error
    raise BaseExceptionGroup(message, errors) from primary_error


def _fsync_directory_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
            return
        raise


def _renameat2_syscall_number() -> int:
    machine = os.uname().machine
    numbers = {
        "aarch64": 276,
        "arm64": 276,
        "x86_64": 316,
    }
    try:
        return numbers[machine]
    except KeyError as exc:
        raise OSError(
            errno.ENOSYS,
            f"renameat2 unsupported on {machine}",
        ) from exc


def _rename_private_lock_residue_no_replace(
    *,
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> None:
    source_component = os.fsencode(_safe_component(source_name))
    destination_component = os.fsencode(_safe_component(destination_name))
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            source_fd,
            source_component,
            destination_fd,
            destination_component,
            _RENAME_NOREPLACE,
        )
    else:
        syscall = libc.syscall
        syscall.restype = ctypes.c_long
        result = syscall(
            ctypes.c_long(_renameat2_syscall_number()),
            ctypes.c_int(source_fd),
            ctypes.c_char_p(source_component),
            ctypes.c_int(destination_fd),
            ctypes.c_char_p(destination_component),
            ctypes.c_uint(_RENAME_NOREPLACE),
        )
    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise OSError(
            error_number,
            os.strerror(error_number),
            os.fsdecode(destination_component),
        )


def _revalidate_private_lock_issue_at(
    root_fd: int,
    issue: PrivateLockNamespaceIssue,
) -> None:
    snapshot = _private_lock_snapshot_at(root_fd, issue.name)
    if snapshot != issue.snapshot:
        raise ValueError("private lock residue changed before quarantine")
    if issue.reason == "unsupported-private-lock-residue":
        raise ValueError("private lock residue is not approved for quarantine")
    approved_residue = (
        PrivateLockResidueApproval(
            name=issue.name,
            reason=issue.reason,
            snapshot=issue.snapshot,
        )
        if issue.reason in _OPERATOR_APPROVED_LOCK_RESIDUE_REASONS
        else None
    )
    classified = _classify_private_lock_issue_at(
        root_fd,
        issue.name,
        snapshot,
        approved_residue=approved_residue,
    )
    if classified != issue:
        raise ValueError("private lock residue is not approved for quarantine")


def _operator_approvals_from_private_lock_report(
    report: PrivateLockNamespaceReport,
) -> tuple[PrivateLockResidueApproval, ...]:
    return tuple(
        PrivateLockResidueApproval(
            name=issue.name,
            reason=issue.reason,
            snapshot=issue.snapshot,
        )
        for issue in report.issues
        if issue.reason in _OPERATOR_APPROVED_LOCK_RESIDUE_REASONS
    )


def _same_quarantined_lock_snapshot(
    current: PrivateLockSnapshot,
    approved: PrivateLockSnapshot,
) -> bool:
    return (
        current.file_type == approved.file_type
        and current.device == approved.device
        and current.inode == approved.inode
        and current.mode == approved.mode
        and current.uid == approved.uid
        and current.gid == approved.gid
        and current.nlink == approved.nlink
        and current.size == approved.size
        and current.mtime_ns == approved.mtime_ns
        and current.symlink_target == approved.symlink_target
        and current.content_sha256 == approved.content_sha256
    )


def _exact_private_lock_issue_at(root_fd: int, issue: PrivateLockNamespaceIssue) -> bool:
    try:
        return _private_lock_snapshot_at(root_fd, issue.name) == issue.snapshot
    except ValueError:
        return False


def _exact_quarantine_entry_at(
    quarantine_fd: int,
    issue: PrivateLockNamespaceIssue,
    quarantine_name: str,
) -> PrivateLockQuarantineEntry | None:
    try:
        current = _private_lock_snapshot_at(quarantine_fd, quarantine_name)
    except ValueError:
        return None
    if not _same_quarantined_lock_snapshot(current, issue.snapshot):
        return None
    return PrivateLockQuarantineEntry(
        original_name=issue.name,
        quarantine_name=quarantine_name,
        file_type=issue.snapshot.file_type,
        snapshot=current,
    )


def _verify_private_lock_quarantine_entries(
    quarantine_fd: int,
    entries: list[PrivateLockQuarantineEntry],
) -> None:
    for entry in entries:
        snapshot = _private_lock_snapshot_at(quarantine_fd, entry.quarantine_name)
        if snapshot != entry.snapshot:
            raise ValueError("private lock quarantine snapshot changed")


def _validate_private_lock_quarantine_root_binding(
    *,
    quarantine_parent_fd: int,
    quarantine_root: Path,
    quarantine_fd: int,
    quarantine_identities: tuple[FileIdentity, ...],
) -> None:
    if quarantine_parent_fd < 0 or quarantine_fd < 0 or not quarantine_identities:
        raise ValueError("private lock quarantine root is unavailable")
    component = _safe_component(quarantine_root.name)
    named_fd = -1
    try:
        _require_private_directory_fd(quarantine_parent_fd)
        held_identity = _require_private_directory_fd(quarantine_fd)
        named_fd = os.open(
            component,
            _directory_open_flags(),
            dir_fd=quarantine_parent_fd,
        )
        named_identity = _require_private_directory_fd(named_fd)
        if named_identity != held_identity or quarantine_identities[-1] != held_identity:
            raise ValueError("private lock quarantine root changed")
    except ValueError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError("private lock quarantine root is unsafe") from exc
        raise ValueError("private lock quarantine root changed") from exc
    finally:
        if named_fd >= 0:
            os.close(named_fd)


def _refresh_private_directory_identity_after_mutation(
    identities: tuple[FileIdentity, ...],
    fd: int,
    *,
    label: str,
) -> tuple[FileIdentity, ...]:
    if fd < 0 or not identities:
        raise ValueError(f"{label} is unavailable")
    previous = identities[-1]
    current = _require_private_directory_fd(fd)
    if (
        current.device != previous.device
        or current.inode != previous.inode
        or current.mode != previous.mode
        or current.uid != previous.uid
        or current.gid != previous.gid
    ):
        raise ValueError(f"{label} changed")
    return (*identities[:-1], current)


def _open_private_lock_quarantine_ancestor_chain(
    quarantine_root: Path,
) -> tuple[list[int], tuple[_DirectoryAncestorIdentity, ...]]:
    parent = _require_path(
        quarantine_root.parent,
        label="private lock quarantine parent",
    )
    absolute = parent if parent.is_absolute() else Path.cwd() / parent
    if (
        not absolute.is_absolute()
        or any(part in {"", ".", ".."} for part in absolute.parts[1:])
    ):
        raise ValueError("private lock quarantine parent must be an absolute path")
    flags = _directory_open_flags()
    current_fd = -1
    next_fd = -1
    fds: list[int] = []
    identities: list[_DirectoryAncestorIdentity] = []
    try:
        current_fd = os.open(absolute.anchor, flags)
        item = os.fstat(current_fd)
        if not stat.S_ISDIR(item.st_mode):
            raise ValueError("private lock quarantine ancestor is not a directory")
        identities.append(_directory_ancestor_identity(item))
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(
                    _safe_component(component),
                    flags,
                    dir_fd=current_fd,
                )
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError(
                        "private lock quarantine ancestor is unsafe"
                    ) from exc
                raise ValueError(
                    "private lock quarantine ancestor changed"
                ) from exc
            item = os.fstat(next_fd)
            if not stat.S_ISDIR(item.st_mode):
                raise ValueError("private lock quarantine ancestor is not a directory")
            identities.append(_directory_ancestor_identity(item))
            fds.append(current_fd)
            current_fd = next_fd
            next_fd = -1
        fds.append(current_fd)
        current_fd = -1
        result_fds = fds
        fds = []
        return result_fds, tuple(identities)
    finally:
        if next_fd >= 0:
            os.close(next_fd)
        if current_fd >= 0:
            os.close(current_fd)
        for fd in reversed(fds):
            os.close(fd)


def _private_lock_quarantine_ancestor_identities(
    quarantine_root: Path,
) -> tuple[_DirectoryAncestorIdentity, ...]:
    fds: list[int] = []
    try:
        fds, identities = _open_private_lock_quarantine_ancestor_chain(
            quarantine_root
        )
        return identities
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _validate_held_private_lock_quarantine_ancestor_chain(
    *,
    quarantine_ancestor_fds: tuple[int, ...],
    quarantine_ancestor_identities: tuple[_DirectoryAncestorIdentity, ...],
) -> None:
    if len(quarantine_ancestor_fds) != len(quarantine_ancestor_identities):
        raise ValueError("private lock quarantine ancestor binding is unavailable")
    for fd, expected in zip(
        quarantine_ancestor_fds,
        quarantine_ancestor_identities,
        strict=True,
    ):
        try:
            current = _directory_ancestor_identity(os.fstat(fd))
        except OSError as exc:
            raise ValueError("private lock quarantine ancestor changed") from exc
        if current != expected:
            raise ValueError("private lock quarantine ancestor changed")


def _validate_private_lock_quarantine_ancestor_binding(
    *,
    quarantine_root: Path,
    quarantine_ancestor_identities: tuple[_DirectoryAncestorIdentity, ...],
    quarantine_ancestor_fds: tuple[int, ...] = (),
) -> None:
    if not quarantine_ancestor_identities:
        raise ValueError("private lock quarantine ancestor binding is unavailable")
    if quarantine_ancestor_fds:
        _validate_held_private_lock_quarantine_ancestor_chain(
            quarantine_ancestor_fds=quarantine_ancestor_fds,
            quarantine_ancestor_identities=quarantine_ancestor_identities,
        )
    try:
        current = _private_lock_quarantine_ancestor_identities(quarantine_root)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("private lock quarantine ancestor changed") from exc
    if current != quarantine_ancestor_identities:
        raise ValueError("private lock quarantine ancestor changed")


def _private_lock_quarantine_root_bound(
    *,
    quarantine_parent_fd: int,
    quarantine_root: Path,
    quarantine_fd: int,
    quarantine_identities: tuple[FileIdentity, ...],
    quarantine_ancestor_identities: tuple[_DirectoryAncestorIdentity, ...],
    quarantine_ancestor_fds: tuple[int, ...],
) -> bool:
    try:
        _validate_private_lock_quarantine_ancestor_binding(
            quarantine_root=quarantine_root,
            quarantine_ancestor_identities=quarantine_ancestor_identities,
            quarantine_ancestor_fds=quarantine_ancestor_fds,
        )
        _validate_private_lock_quarantine_root_binding(
            quarantine_parent_fd=quarantine_parent_fd,
            quarantine_root=quarantine_root,
            quarantine_fd=quarantine_fd,
            quarantine_identities=quarantine_identities,
        )
    except (OSError, ValueError):
        return False
    return True


def _private_lock_quarantine_ancestor_bound(
    *,
    quarantine_root: Path,
    quarantine_ancestor_identities: tuple[_DirectoryAncestorIdentity, ...],
    quarantine_ancestor_fds: tuple[int, ...],
) -> bool:
    try:
        _validate_private_lock_quarantine_ancestor_binding(
            quarantine_root=quarantine_root,
            quarantine_ancestor_identities=quarantine_ancestor_identities,
            quarantine_ancestor_fds=quarantine_ancestor_fds,
        )
    except (OSError, ValueError):
        return False
    return True


def _same_private_lock_quarantine_ancestor_path_identity(
    current: _DirectoryAncestorIdentity,
    expected: _DirectoryAncestorIdentity,
) -> bool:
    return (
        current.device == expected.device
        and current.inode == expected.inode
        and current.mode == expected.mode
        and current.uid == expected.uid
        and current.gid == expected.gid
    )


def _private_lock_quarantine_ancestor_path_still_bound_for_partial_error(
    *,
    quarantine_root: Path,
    quarantine_ancestor_identities: tuple[_DirectoryAncestorIdentity, ...],
) -> bool:
    if not quarantine_ancestor_identities:
        return False
    try:
        current_identities = _private_lock_quarantine_ancestor_identities(
            quarantine_root
        )
    except (OSError, ValueError):
        return False
    if len(current_identities) != len(quarantine_ancestor_identities):
        return False
    return all(
        _same_private_lock_quarantine_ancestor_path_identity(current, expected)
        for current, expected in zip(
            current_identities,
            quarantine_ancestor_identities,
            strict=True,
        )
    )


def _validate_private_lock_root_path_binding(
    path: Path,
    held_fd: int,
    *,
    label: str,
) -> None:
    fresh_fd = -1
    try:
        fresh_fd, _identities = _open_existing_private_lock_root(
            path,
            validate_namespace=False,
        )
        if _require_private_directory_fd(fresh_fd) != _require_private_directory_fd(
            held_fd
        ):
            raise ValueError(f"{label} changed before return")
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"{label} changed before return") from exc
    finally:
        if fresh_fd >= 0:
            os.close(fresh_fd)


def _partial_lock_quarantine_error(
    *,
    primary_error: BaseException,
    moved: list[tuple[PrivateLockNamespaceIssue, str]],
    entries: list[PrivateLockQuarantineEntry],
    issues: tuple[PrivateLockNamespaceIssue, ...],
    root_fd: int,
    quarantine_fd: int,
    quarantine_root: Path,
    quarantine_ancestor_identities: tuple[_DirectoryAncestorIdentity, ...],
    quarantine_ancestor_bound: bool,
    quarantine_path_bound: bool,
) -> PrivateLockPartialCommitError:
    committed_by_name: dict[str, PrivateLockQuarantineEntry] = {}
    unverified: list[PrivateLockNamespaceIssue] = []
    lock_root_snapshot = None
    quarantine_root_snapshot = None
    if root_fd >= 0:
        try:
            lock_root_snapshot = _snapshot_from_stat(os.fstat(root_fd))
        except OSError:
            lock_root_snapshot = None
    quarantine_path_still_named_by_same_ancestors = (
        quarantine_ancestor_bound
        or _private_lock_quarantine_ancestor_path_still_bound_for_partial_error(
            quarantine_root=quarantine_root,
            quarantine_ancestor_identities=quarantine_ancestor_identities,
        )
    )
    if quarantine_fd >= 0 and quarantine_path_still_named_by_same_ancestors:
        try:
            quarantine_root_snapshot = _snapshot_from_stat(os.fstat(quarantine_fd))
        except OSError:
            quarantine_root_snapshot = None
    if quarantine_path_bound:
        for entry in entries:
            if quarantine_fd < 0:
                continue
            try:
                current = _private_lock_snapshot_at(quarantine_fd, entry.quarantine_name)
            except ValueError:
                continue
            if current == entry.snapshot:
                committed_by_name[entry.original_name] = entry
    for issue, quarantine_name in moved:
        if issue.name in committed_by_name:
            continue
        if quarantine_path_bound and quarantine_fd >= 0:
            exact = _exact_quarantine_entry_at(quarantine_fd, issue, quarantine_name)
            if exact is not None:
                committed_by_name[issue.name] = exact
                continue
        unverified.append(issue)
    moved_names = frozenset(issue.name for issue, _ in moved)
    unmoved: list[PrivateLockNamespaceIssue] = []
    for issue in issues:
        if issue.name in moved_names:
            continue
        if root_fd >= 0 and _exact_private_lock_issue_at(root_fd, issue):
            unmoved.append(issue)
        else:
            unverified.append(issue)
    return PrivateLockPartialCommitError(
        primary_error=primary_error,
        lock_root_snapshot=lock_root_snapshot,
        quarantine_root_snapshot=quarantine_root_snapshot,
        committed=tuple(
            committed_by_name[issue.name]
            for issue, _ in moved
            if issue.name in committed_by_name
        ),
        unmoved=tuple(unmoved),
        unverified=tuple(unverified),
    )


def quarantine_private_lock_residues(
    report: PrivateLockNamespaceReport,
    *,
    quarantine_root: Path,
    approval_hash: str,
) -> PrivateLockQuarantineResult:
    if type(report) is not PrivateLockNamespaceReport:
        raise ValueError("private lock namespace report is invalid")
    quarantine_root = _require_private_lock_quarantine_root(report, quarantine_root)
    if approval_hash != private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    ):
        raise ValueError("private lock namespace approval hash does not match")
    root_fd = -1
    quarantine_fd = -1
    quarantine_parent_fd = -1
    quarantine_identities: tuple[FileIdentity, ...] = ()
    quarantine_ancestor_fds: list[int] = []
    quarantine_ancestor_identities: tuple[_DirectoryAncestorIdentity, ...] = ()
    moved: list[tuple[PrivateLockNamespaceIssue, str]] = []
    entries: list[PrivateLockQuarantineEntry] = []
    pending_error: BaseException | None = None

    def validate_quarantine_ancestor_binding() -> None:
        _validate_private_lock_quarantine_ancestor_binding(
            quarantine_root=quarantine_root,
            quarantine_ancestor_identities=quarantine_ancestor_identities,
            quarantine_ancestor_fds=tuple(quarantine_ancestor_fds),
        )

    try:
        root_fd, _root_identities = _open_existing_private_lock_root(
            report.lock_root,
            validate_namespace=False,
        )
        for issue in report.issues:
            _revalidate_private_lock_issue_at(root_fd, issue)
        report_approvals = _operator_approvals_from_private_lock_report(report)
        current_report = _scan_private_lock_namespace_fd(
            report.lock_root,
            root_fd,
            approved_residues=report_approvals,
        )
        if current_report != report:
            raise ValueError("private lock namespace changed before quarantine")
        ensure_private_directory(
            quarantine_root,
            label="private lock quarantine directory",
        )
        quarantine_parent_fd, _quarantine_parent_identity = (
            _open_private_directory_path_identity(
                quarantine_root.parent,
                label="private lock quarantine parent",
                secure=True,
            )
        )
        _fsync_directory_fd(quarantine_parent_fd)
        quarantine_fd, quarantine_identities = _open_existing_private_lock_root(
            quarantine_root,
            validate_namespace=False,
        )
        quarantine_ancestor_fds, quarantine_ancestor_identities = (
            _open_private_lock_quarantine_ancestor_chain(
                quarantine_root
            )
        )
        validate_quarantine_ancestor_binding()
        _validate_private_lock_quarantine_root_binding(
            quarantine_parent_fd=quarantine_parent_fd,
            quarantine_root=quarantine_root,
            quarantine_fd=quarantine_fd,
            quarantine_identities=quarantine_identities,
        )
        for issue in report.issues:
            _revalidate_private_lock_issue_at(root_fd, issue)
            quarantine_name = _safe_quarantine_name(issue)
            try:
                _before_private_lock_reconcile_rename(issue.name, quarantine_name)
                _rename_private_lock_residue_no_replace(
                    source_fd=root_fd,
                    source_name=issue.name,
                    destination_fd=quarantine_fd,
                    destination_name=quarantine_name,
                )
                quarantine_identities = (
                    _refresh_private_directory_identity_after_mutation(
                        quarantine_identities,
                        quarantine_fd,
                        label="private lock quarantine root",
                    )
                )
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    raise ValueError(
                        "private lock quarantine target already exists"
                    ) from exc
                raise
            moved.append((issue, quarantine_name))
            validate_quarantine_ancestor_binding()
            current = _private_lock_snapshot_at(quarantine_fd, quarantine_name)
            if not _same_quarantined_lock_snapshot(current, issue.snapshot):
                raise ValueError("private lock quarantine snapshot changed")
            _before_private_lock_reconcile_commit(issue.name, quarantine_name)
            validate_quarantine_ancestor_binding()
            current = _private_lock_snapshot_at(quarantine_fd, quarantine_name)
            if not _same_quarantined_lock_snapshot(current, issue.snapshot):
                raise ValueError("private lock quarantine snapshot changed")
            _fsync_directory_fd(quarantine_fd)
            validate_quarantine_ancestor_binding()
            _fsync_directory_fd(root_fd)
            validate_quarantine_ancestor_binding()
            current = _private_lock_snapshot_at(quarantine_fd, quarantine_name)
            if not _same_quarantined_lock_snapshot(current, issue.snapshot):
                raise ValueError("private lock quarantine snapshot changed")
            entries.append(
                PrivateLockQuarantineEntry(
                    original_name=issue.name,
                    quarantine_name=quarantine_name,
                    file_type=issue.snapshot.file_type,
                    snapshot=current,
                )
            )
        report_after = _scan_private_lock_namespace_fd(report.lock_root, root_fd)
        if report_after.issues:
            raise ValueError("private lock namespace still contains residues")
        _fsync_directory_fd(quarantine_parent_fd)
        validate_quarantine_ancestor_binding()
        _validate_private_lock_quarantine_root_binding(
            quarantine_parent_fd=quarantine_parent_fd,
            quarantine_root=quarantine_root,
            quarantine_fd=quarantine_fd,
            quarantine_identities=quarantine_identities,
        )
        lock_root_snapshot = _snapshot_from_stat(os.fstat(root_fd))
        quarantine_root_snapshot = _snapshot_from_stat(os.fstat(quarantine_fd))
        _verify_private_lock_quarantine_entries(quarantine_fd, entries)
        _before_private_lock_reconcile_return(root_fd, quarantine_fd, entries)
        _validate_private_lock_root_path_binding(
            report.lock_root,
            root_fd,
            label="private lock root",
        )
        _validate_private_lock_root_path_binding(
            quarantine_root,
            quarantine_fd,
            label="private lock quarantine root",
        )
        validate_quarantine_ancestor_binding()
        _validate_private_lock_quarantine_root_binding(
            quarantine_parent_fd=quarantine_parent_fd,
            quarantine_root=quarantine_root,
            quarantine_fd=quarantine_fd,
            quarantine_identities=quarantine_identities,
        )
        if _snapshot_from_stat(os.fstat(root_fd)) != lock_root_snapshot:
            raise ValueError("private lock root changed before quarantine return")
        if _snapshot_from_stat(os.fstat(quarantine_fd)) != quarantine_root_snapshot:
            raise ValueError("private lock quarantine root changed before return")
        _verify_private_lock_quarantine_entries(quarantine_fd, entries)
        return PrivateLockQuarantineResult(
            lock_root=report.lock_root,
            lock_root_snapshot=lock_root_snapshot,
            quarantine_root=quarantine_root,
            quarantine_root_snapshot=quarantine_root_snapshot,
            approval_hash=approval_hash,
            quarantined=tuple(entries),
        )
    except BaseException as exc:
        if moved:
            pending_error = _partial_lock_quarantine_error(
                primary_error=exc,
                moved=moved,
                entries=entries,
                issues=report.issues,
                root_fd=root_fd,
                quarantine_fd=quarantine_fd,
                quarantine_root=quarantine_root,
                quarantine_ancestor_identities=quarantine_ancestor_identities,
                quarantine_ancestor_bound=_private_lock_quarantine_ancestor_bound(
                    quarantine_root=quarantine_root,
                    quarantine_ancestor_identities=quarantine_ancestor_identities,
                    quarantine_ancestor_fds=tuple(quarantine_ancestor_fds),
                ),
                quarantine_path_bound=_private_lock_quarantine_root_bound(
                    quarantine_parent_fd=quarantine_parent_fd,
                    quarantine_root=quarantine_root,
                    quarantine_fd=quarantine_fd,
                    quarantine_identities=quarantine_identities,
                    quarantine_ancestor_identities=quarantine_ancestor_identities,
                    quarantine_ancestor_fds=tuple(quarantine_ancestor_fds),
                ),
            )
            raise pending_error from exc
        pending_error = exc
        raise
    finally:
        active_unwind = sys.exc_info()[0] is not None
        active_error = pending_error if pending_error is not None else (
            sys.exc_info()[1] if active_unwind else None
        )
        cleanup_errors: list[BaseException] = []
        try:
            _close_private_lock_reconcile_descriptors(
                quarantine_parent_fd,
                quarantine_fd,
                root_fd,
                *reversed(quarantine_ancestor_fds),
            )
        except BaseException as close_error:
            cleanup_errors.append(close_error)
        _raise_private_lock_cleanup_errors(
            "private lock reconcile",
            active_error if active_unwind else None,
            cleanup_errors,
        )


def _open_existing_private_lock_file(
    root_fd: int,
    name: str,
    *,
    label: str,
) -> tuple[int, tuple[int, ...]]:
    component = _safe_component(name)
    flags = (
        os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(component, flags, dir_fd=root_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise ValueError(f"{label} must be a regular file") from exc
        raise
    try:
        identity = _private_lock_file_identity(os.fstat(fd))
        result = fd
        fd = -1
        return result, identity
    finally:
        if fd >= 0:
            os.close(fd)


def _revalidate_existing_private_lock(
    *,
    lock_root: Path,
    root_identities: tuple[FileIdentity, ...],
    lock_name: str,
    lock_fd: int,
    lock_identity: tuple[int, ...],
    label: str,
) -> None:
    if _private_lock_file_identity(os.fstat(lock_fd)) != lock_identity:
        raise ValueError(f"{label} changed while locking")
    fresh_root_fd = -1
    fresh_lock_fd = -1
    try:
        fresh_root_fd, fresh_root_identities = _open_existing_private_lock_root(
            lock_root
        )
        if fresh_root_identities != root_identities:
            raise ValueError(f"{label} namespace changed while locking")
        fresh_lock_fd, fresh_lock_identity = _open_existing_private_lock_file(
            fresh_root_fd,
            lock_name,
            label=label,
        )
        if fresh_lock_identity != lock_identity:
            raise ValueError(f"{label} changed while locking")
    finally:
        if fresh_lock_fd >= 0:
            os.close(fresh_lock_fd)
        if fresh_root_fd >= 0:
            os.close(fresh_root_fd)


def _revalidate_held_private_lock_siblings(
    *,
    lock_root: Path,
    root_identities: tuple[FileIdentity, ...],
    held_lock_identities: dict[Path, _HeldPrivatePathLock],
    lock_key: Path,
    label: str,
) -> None:
    root_fd = -1
    sibling_fd = -1
    try:
        root_fd, fresh_root_identities = _open_existing_private_lock_root(lock_root)
        if fresh_root_identities != root_identities:
            raise ValueError(f"{label} namespace changed while locking")
        for held_key, active_lock in held_lock_identities.items():
            if held_key.parent != lock_key.parent:
                continue
            if type(active_lock) is not _HeldPrivatePathLock:
                raise ValueError(f"{label} changed while locking")
            sibling_fd, sibling_identity = _open_existing_private_lock_file(
                root_fd,
                held_key.name,
                label=label,
            )
            if sibling_identity != active_lock.lock_identity:
                raise ValueError(f"{label} changed while locking")
            os.close(sibling_fd)
            sibling_fd = -1
    finally:
        if sibling_fd >= 0:
            os.close(sibling_fd)
        if root_fd >= 0:
            os.close(root_fd)


def open_verified_state_home(state_home: Path) -> int:
    state_home = _require_path(state_home, label="state home")
    if (
        not state_home.is_absolute()
        or any(part in {"", ".", ".."} for part in state_home.parts[1:])
    ):
        raise ValueError("state home must be an absolute normalized path")
    flags = _directory_open_flags()
    current_fd = os.open(state_home.anchor, flags)
    try:
        root_uid = os.fstat(current_fd).st_uid
        _require_trusted_directory_fd(current_fd, root_uid=root_uid)
        components = state_home.parts[1:]
        for index, component in enumerate(components):
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError("state home contains an unsafe component") from exc
                raise
            os.close(current_fd)
            current_fd = next_fd
            if index == len(components) - 1:
                _require_private_directory_fd(current_fd)
            else:
                _require_trusted_directory_fd(current_fd, root_uid=root_uid)
        if not components:
            _require_private_directory_fd(current_fd)
        result = current_fd
        current_fd = -1
        return result
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def open_private_dir_at(parent_fd: int, name: str) -> int:
    _require_private_directory_fd(parent_fd)
    component = _safe_component(name)
    try:
        fd = os.open(component, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError("private directory is unsafe") from exc
        raise
    try:
        _require_private_directory_fd(fd)
        result = fd
        fd = -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def _require_private_file_stat(
    item: os.stat_result,
    *,
    maximum: int,
    mode: int,
) -> FileIdentity:
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_uid != os.geteuid()
        or item.st_nlink != 1
        or stat.S_IMODE(item.st_mode) != mode
        or item.st_size > maximum
    ):
        raise ValueError("private file descriptor is invalid")
    return FileIdentity(
        item.st_dev,
        item.st_ino,
        stat.S_IMODE(item.st_mode),
        gid=item.st_gid,
        uid=item.st_uid,
        ctime_ns=item.st_ctime_ns,
    )


def read_private_bytes_at(
    parent_fd: int,
    name: str,
    *,
    maximum: int,
    mode: int,
) -> tuple[bytes, FileIdentity]:
    _require_private_directory_fd(parent_fd)
    component = _safe_component(name)
    if type(maximum) is not int or maximum < 0:
        raise ValueError("private byte budget is invalid")
    if type(mode) is not int or mode < 0 or mode & ~0o700:
        raise ValueError("private file mode is invalid")
    fd = os.open(component, _regular_open_flags(), dir_fd=parent_fd)
    try:
        initial = os.fstat(fd)
        identity = _require_private_file_stat(initial, maximum=maximum, mode=mode)
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(fd, min(65_536, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > maximum:
            raise ValueError("private file exceeds byte budget")
        final = os.fstat(fd)
        if (
            _require_private_file_stat(final, maximum=maximum, mode=mode) != identity
            or final.st_size != initial.st_size
            or final.st_mtime_ns != initial.st_mtime_ns
            or final.st_ctime_ns != initial.st_ctime_ns
        ):
            raise ValueError("private file changed during read")
        return bytes(payload), identity
    finally:
        os.close(fd)


def write_private_bytes_at(
    parent_fd: int,
    name: str,
    payload: bytes,
    *,
    mode: int,
) -> FileIdentity:
    _require_private_directory_fd(parent_fd)
    component = _safe_component(name)
    if type(payload) is not bytes:
        raise ValueError("private payload is invalid")
    if type(mode) is not int or mode < 0 or mode & ~0o700:
        raise ValueError("private file mode is invalid")
    flags = _regular_open_flags(writable=True) | os.O_CREAT | os.O_EXCL
    fd = -1
    created = False
    created_identity: tuple[int, int] | None = None
    try:
        fd = os.open(component, flags, mode, dir_fd=parent_fd)
        created = True
        opened = os.fstat(fd)
        created_identity = (opened.st_dev, opened.st_ino)
        os.fchmod(fd, mode)
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError(errno.EIO, "short private write")
            offset += written
        os.fsync(fd)
        item = os.fstat(fd)
        identity = _require_private_file_stat(
            item,
            maximum=len(payload),
            mode=mode,
        )
        if item.st_size != len(payload):
            raise ValueError("private file size is invalid")
        os.fsync(parent_fd)
        created = False
        return identity
    finally:
        if fd >= 0:
            os.close(fd)
        if created:
            try:
                candidate = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    stat.S_ISREG(candidate.st_mode)
                    and candidate.st_uid == os.geteuid()
                    and candidate.st_nlink == 1
                    and (candidate.st_dev, candidate.st_ino) == created_identity
                ):
                    os.unlink(component, dir_fd=parent_fd)
                    os.fsync(parent_fd)
            except OSError:
                pass


def read_private_text(
    path: Path,
    *,
    regular_label: str,
    read_label: str,
    max_bytes: int,
    too_large_label: str | None = None,
    invalid_utf8_label: str | None = None,
) -> tuple[str, os.stat_result]:
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError(f"{read_label} max_bytes is invalid")
    path = _require_path(path, label=regular_label)
    assert_no_symlink_ancestors(path, label=regular_label)
    if path.is_symlink():
        raise ValueError(f"{regular_label} must be a regular file: {path}")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK

    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise ValueError(f"{regular_label} must be a regular file: {path}") from exc
        raise ValueError(f"cannot read {read_label}: {path}") from exc

    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.geteuid():
            raise ValueError(f"{regular_label} must be a regular file: {path}")
        if file_stat.st_size > max_bytes:
            raise ValueError(
                f"{too_large_label or read_label} too large; max {max_bytes} bytes"
            )
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(max_bytes + 1)
    except OSError as exc:
        raise ValueError(f"cannot read {read_label}: {path}") from exc
    finally:
        if fd >= 0:
            os.close(fd)

    if len(raw) > max_bytes:
        raise ValueError(
            f"{too_large_label or read_label} too large; max {max_bytes} bytes"
        )
    try:
        return raw.decode("utf-8"), file_stat
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{invalid_utf8_label or read_label} is not valid UTF-8: {path}"
        ) from exc


def _rollback_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.rollback")


def _recover_stale_rollback(
    path: Path,
    *,
    label: str,
    required_target_mode: int | None = None,
) -> Path | None:
    rollback = _rollback_path(path)
    legacy_pattern = f".{glob.escape(path.name)}.rollback-*"
    stale = []
    try:
        rollback.lstat()
    except FileNotFoundError:
        pass
    else:
        stale.append(rollback)
    stale.extend(islice(path.parent.glob(legacy_pattern), _MAX_STALE_ROLLBACKS + 1))
    if len(stale) > _MAX_STALE_ROLLBACKS:
        raise ValueError(f"too many stale {label} rollback files")
    if not stale:
        return None

    candidate = stale[0]
    candidate_stat = candidate.lstat()
    if (
        not stat.S_ISREG(candidate_stat.st_mode)
        or candidate_stat.st_uid != os.geteuid()
        or stat.S_IMODE(candidate_stat.st_mode) & ~0o700
    ):
        raise ValueError(f"stale {label} rollback must be a private user-owned file")
    try:
        target_stat = path.lstat()
    except FileNotFoundError:
        if candidate_stat.st_nlink != 1:
            raise ValueError(f"stale {label} rollback identity is invalid") from None
        os.replace(candidate, path)
        _fsync_directory(path.parent)
        return None
    if not stat.S_ISREG(target_stat.st_mode) or target_stat.st_uid != os.geteuid():
        raise ValueError(f"{label} must be a private user-owned file: {path}")
    same_inode = (
        target_stat.st_dev == candidate_stat.st_dev
        and target_stat.st_ino == candidate_stat.st_ino
    )
    if same_inode:
        if target_stat.st_nlink != 2 or candidate_stat.st_nlink != 2:
            raise ValueError(f"stale {label} rollback identity is invalid")
        candidate.unlink()
        _fsync_directory(path.parent)
        return None
    if target_stat.st_nlink != 1 or candidate_stat.st_nlink != 1:
        raise ValueError(f"stale {label} rollback identity is invalid")
    if required_target_mode is not None and (
        stat.S_IMODE(target_stat.st_mode) != required_target_mode
    ):
        return candidate
    return candidate


def _copy_private_file(
    source: Path,
    destination: Path,
    *,
    source_stat: os.stat_result,
    label: str,
    mode: int,
) -> None:
    if source_stat.st_size > _MAX_PRIVATE_ROLLBACK_BYTES:
        raise ValueError(f"{label} is too large for rollback")
    read_flags = os.O_RDONLY
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for flag_name in ("O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK"):
        flag = getattr(os, flag_name, 0)
        read_flags |= flag
        write_flags |= flag
    source_fd = -1
    destination_fd = -1
    try:
        source_fd = os.open(source, read_flags)
        opened_stat = os.fstat(source_fd)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_dev != source_stat.st_dev
            or opened_stat.st_ino != source_stat.st_ino
            or opened_stat.st_nlink != 1
            or opened_stat.st_uid != os.geteuid()
            or opened_stat.st_mode != source_stat.st_mode
        ):
            raise ValueError(f"{label} changed before rollback copy")
        destination_fd = os.open(
            destination,
            write_flags,
            mode,
        )
        os.fchmod(destination_fd, mode)
        rollback_stat = os.fstat(destination_fd)
        if (
            not stat.S_ISREG(rollback_stat.st_mode)
            or rollback_stat.st_nlink != 1
            or rollback_stat.st_uid != os.geteuid()
            or stat.S_IMODE(rollback_stat.st_mode) != mode
        ):
            raise ValueError(f"rollback {label} is not a private regular file")
        total = 0
        while True:
            chunk = os.read(
                source_fd,
                min(65_536, _MAX_PRIVATE_ROLLBACK_BYTES - total + 1),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_PRIVATE_ROLLBACK_BYTES:
                raise ValueError(f"{label} is too large for rollback")
            offset = 0
            while offset < len(chunk):
                written = os.write(destination_fd, chunk[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, f"short rollback write for {label}")
                offset += written
        os.fsync(destination_fd)
        final_stat = os.fstat(source_fd)
        current_stat = source.lstat()
        for item in (final_stat, current_stat):
            if (
                not stat.S_ISREG(item.st_mode)
                or item.st_dev != source_stat.st_dev
                or item.st_ino != source_stat.st_ino
                or item.st_nlink != 1
                or item.st_uid != os.geteuid()
                or item.st_mode != source_stat.st_mode
                or item.st_size != source_stat.st_size
                or item.st_mtime_ns != source_stat.st_mtime_ns
            ):
                raise ValueError(f"{label} changed during rollback copy")
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if source_fd >= 0:
            os.close(source_fd)


def _write_private_text_locked(
    path: Path,
    text: str,
    *,
    label: str,
    mode: int = 0o600,
    replace_existing: bool = True,
) -> None:
    if type(text) is not str:
        raise ValueError(f"{label} text is invalid")
    path = _require_path(path, label=label)
    if (
        type(mode) is not int
        or mode < 0
        or mode & ~0o700
    ):
        raise ValueError(f"{label} mode must be private")
    assert_no_symlink_ancestors(path, label=label)
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError(f"{label} parent must be a real directory: {parent}")
    stale_rollback = (
        _recover_stale_rollback(path, label=label) if replace_existing else None
    )
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"{label} must be a regular file: {path}")
    target_stat = None
    if path.exists():
        target_stat = path.lstat()
        if target_stat.st_nlink != 1 or target_stat.st_uid != os.geteuid():
            raise ValueError(f"{label} must be a private user-owned file: {path}")
    if stale_rollback is not None:
        stale_rollback.unlink()
        _fsync_directory(parent)
    encoded = text.encode("utf-8")
    temporary = parent / (
        "." + path.name + ".tmp-" + str(os.getpid()) + "-" + secrets.token_hex(8)
    )
    rollback = parent / (
        "." + path.name + ".rollback-" + str(os.getpid()) + "-" + secrets.token_hex(8)
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK

    fd = -1
    replaced = False
    rollback_exists = False
    try:
        fd = os.open(temporary, flags, mode)
        file_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
            or file_stat.st_uid != os.geteuid()
        ):
            raise ValueError(f"temporary {label} is not a private regular file")
        os.fchmod(fd, mode)
        offset = 0
        while offset < len(encoded):
            written = os.write(fd, encoded[offset:])
            if written <= 0:
                raise OSError(errno.EIO, f"short write for {label}")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = -1
        if replace_existing:
            if target_stat is not None:
                rollback_exists = True
                _copy_private_file(
                    path,
                    rollback,
                    source_stat=target_stat,
                    label=label,
                    mode=mode,
                )
            _fsync_directory(parent)
            os.replace(temporary, path)
            replaced = True
            try:
                _fsync_directory(parent)
                if rollback_exists:
                    rollback.unlink()
                    rollback_exists = False
            except OSError as publish_error:
                try:
                    if rollback_exists:
                        os.replace(rollback, path)
                        rollback_exists = False
                    else:
                        path.unlink()
                except OSError as rollback_error:
                    raise OSError(errno.EIO, f"could not roll back {label}") from rollback_error
                try:
                    _fsync_directory(parent)
                except OSError:
                    pass
                raise publish_error
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise ValueError(f"{label} must not overwrite existing file: {path}") from exc
            replaced = True
            try:
                temporary.unlink()
            except OSError as unlink_error:
                try:
                    path.unlink()
                except OSError as rollback_error:
                    raise ExceptionGroup(
                        f"could not roll back create-only {label}",
                        [unlink_error, rollback_error],
                    ) from None
                replaced = False
                raise
            _fsync_directory(parent)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise ValueError(f"{label} must be a regular file: {path}") from exc
        raise
    finally:
        if fd >= 0:
            os.close(fd)
        if not replaced:
            try:
                temporary.unlink()
            except OSError:
                pass
        if rollback_exists:
            try:
                rollback.unlink()
            except OSError:
                pass


def write_private_text(
    path: Path,
    text: str,
    *,
    label: str,
    mode: int = 0o600,
    replace_existing: bool = True,
    created_lock_files: list[tuple[Path, int, int]] | None = None,
) -> None:
    if type(text) is not str:
        raise ValueError(f"{label} text is invalid")
    path = _require_path(path, label=label)
    if type(mode) is not int or mode < 0 or mode & ~0o700:
        raise ValueError(f"{label} mode must be private")
    assert_no_symlink_ancestors(path, label=label)
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError(f"{label} parent must be a real directory: {parent}")
    with private_path_lock(
        path,
        label=f"{label} write lock",
        created_lock_files=created_lock_files,
    ):
        _write_private_text_locked(
            path,
            text,
            label=label,
            mode=mode,
            replace_existing=replace_existing,
        )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
            return
        raise
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def private_path_lock(
    path: Path,
    *,
    timeout_seconds: int | float = PRIVATE_LOCK_TIMEOUT_SECONDS,
    label: str = "private lock",
    created_lock_files: list[tuple[Path, int, int]] | None = None,
    create: bool = True,
) -> Iterator[None]:
    path = _require_path(path, label=label)
    if created_lock_files is not None and not isinstance(created_lock_files, list):
        raise ValueError("created_lock_files is invalid")
    if type(create) is not bool:
        raise ValueError("create is invalid")
    deadline = _lock_deadline(timeout_seconds)
    parent = path.parent
    assert_no_symlink_ancestors(parent, label=label)
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError(f"{label} parent must be a real directory: {parent}")
    lock_root = _private_lock_root()
    lock_name = _private_lock_name(path)
    lock_path = lock_root / lock_name
    held_lock_identities = getattr(_PRIVATE_LOCK_STATE, "identities", None)
    if held_lock_identities is None:
        held_lock_identities = {}
        _PRIVATE_LOCK_STATE.identities = held_lock_identities
    lock_key = Path(os.path.abspath(lock_path))
    held_lock = held_lock_identities.get(lock_key)
    if create and held_lock is not None:
        if type(held_lock) is not _HeldPrivatePathLock:
            raise ValueError(f"{label} changed while locking")
        root_fd = -1
        lock_fd = -1
        pending_error: BaseException | None = None
        try:
            root_fd, root_identities = _open_existing_private_lock_root(lock_root)
            if root_identities != held_lock.root_identities:
                raise ValueError(f"{label} namespace changed while locking")
            lock_fd, lock_identity = _open_existing_private_lock_file(
                root_fd,
                lock_name,
                label=label,
            )
            if lock_identity != held_lock.lock_identity:
                raise ValueError(f"{label} changed while locking")
        except OSError as exc:
            pending_error = ValueError("private lock namespace cannot be validated")
            raise pending_error from exc
        except BaseException as exc:
            pending_error = exc
            raise
        finally:
            active_unwind = sys.exc_info()[0] is not None
            active_error = pending_error if pending_error is not None else (
                sys.exc_info()[1] if active_unwind else None
            )
            cleanup_errors: list[BaseException] = []
            try:
                _close_private_lock_reconcile_descriptors(lock_fd, root_fd)
            except BaseException as close_error:
                cleanup_errors.append(close_error)
            _raise_private_lock_cleanup_errors(
                label,
                active_error if active_unwind else None,
                cleanup_errors,
            )
        primary_error: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            _raise_private_lock_cleanup_errors(label, primary_error, [])
        return
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    lock_created = False
    root_fd = -1
    root_identities: tuple[FileIdentity, ...] = ()
    existing_lock_identity: tuple[int, ...] | None = None
    fd = -1
    try:
        if create:
            root_fd, root_identities = _open_or_create_private_lock_root(lock_root)
            try:
                try:
                    fd = os.open(
                        lock_name,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=root_fd,
                    )
                    lock_created = True
                except FileExistsError:
                    fd, existing_lock_identity = _open_existing_private_lock_file(
                        root_fd,
                        lock_name,
                        label=label,
                    )
            except Exception:
                if root_fd >= 0:
                    os.close(root_fd)
                    root_fd = -1
                raise
        else:
            root_fd, root_identities = _open_existing_private_lock_root(lock_root)
            try:
                fd, existing_lock_identity = _open_existing_private_lock_file(
                    root_fd,
                    lock_name,
                    label=label,
                )
            except Exception:
                os.close(root_fd)
                root_fd = -1
                raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise ValueError(f"{label} must be a regular file: {lock_path}") from exc
        raise
    try:
        file_stat = os.fstat(fd)
        if create:
            if lock_created:
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or file_stat.st_nlink != 1
                    or file_stat.st_uid != os.geteuid()
                ):
                    raise ValueError(
                        f"{label} must be a private regular file: {lock_path}"
                    )
                if stat.S_IMODE(file_stat.st_mode) != 0o600:
                    os.fchmod(fd, 0o600)
                file_stat = os.fstat(fd)
                lock_identity = _private_lock_file_identity(file_stat)
            elif existing_lock_identity is None:
                raise ValueError(f"{label} identity is unavailable")
            else:
                lock_identity = existing_lock_identity
        elif existing_lock_identity is None:  # pragma: no cover - local invariant
            raise ValueError(f"{label} identity is unavailable")
        else:
            lock_identity = existing_lock_identity
        if lock_created and created_lock_files is not None:
            created_lock_files.append((lock_path, file_stat.st_dev, file_stat.st_ino))
        if lock_created:
            root_identities = _refresh_private_directory_identity_after_mutation(
                root_identities,
                root_fd,
                label="private lock namespace",
            )
        held_lock = held_lock_identities.get(lock_key)
        if held_lock is not None:
            if type(held_lock) is not _HeldPrivatePathLock:
                raise ValueError(f"{label} changed while locking")
            if create or held_lock.lock_identity != lock_identity:
                raise ValueError(f"{label} changed while locking")
            if held_lock.root_identities != root_identities:
                raise ValueError(f"{label} namespace changed while locking")
            _revalidate_existing_private_lock(
                lock_root=lock_root,
                root_identities=root_identities,
                lock_name=lock_name,
                lock_fd=fd,
                lock_identity=lock_identity,
                label=label,
            )
            yield
            return
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{label} is already in use") from exc
                time.sleep(0.05)
        _revalidate_existing_private_lock(
            lock_root=lock_root,
            root_identities=root_identities,
            lock_name=lock_name,
            lock_fd=fd,
            lock_identity=lock_identity,
            label=label,
        )
        if lock_created:
            _revalidate_held_private_lock_siblings(
                lock_root=lock_root,
                root_identities=root_identities,
                held_lock_identities=held_lock_identities,
                lock_key=lock_key,
                label=label,
            )
            refreshed_held_locks: dict[Path, _HeldPrivatePathLock] = {}
            for held_key, active_lock in held_lock_identities.items():
                if held_key.parent != lock_key.parent:
                    continue
                if type(active_lock) is not _HeldPrivatePathLock:
                    raise ValueError(f"{label} changed while locking")
                refreshed_held_locks[held_key] = _HeldPrivatePathLock(
                    root_identities=root_identities,
                    lock_identity=active_lock.lock_identity,
                )
            held_lock_identities.update(refreshed_held_locks)
        held_lock_identities[lock_key] = _HeldPrivatePathLock(
            root_identities=root_identities,
            lock_identity=lock_identity,
        )
        primary_error: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            del held_lock_identities[lock_key]
            cleanup_errors: list[BaseException] = []
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            except BaseException as exc:
                cleanup_errors.append(exc)
            for close_fd in (fd, root_fd):
                if close_fd < 0:
                    continue
                try:
                    os.close(close_fd)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            fd = -1
            root_fd = -1
            _raise_private_lock_cleanup_errors(
                label,
                primary_error,
                cleanup_errors,
            )
    finally:
        cleanup_errors = []
        for close_fd in (fd, root_fd):
            if close_fd < 0:
                continue
            try:
                os.close(close_fd)
            except BaseException as exc:
                cleanup_errors.append(exc)
        _raise_private_lock_cleanup_errors(label, None, cleanup_errors)


def _require_path(path: object, *, label: str) -> Path:
    if type(path) is not _PATH_TYPE:
        raise ValueError(f"{label} path is invalid")
    return path
