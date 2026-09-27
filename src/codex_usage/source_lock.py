from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .private_io import assert_no_symlink_ancestors, ensure_private_directory

_PATH_TYPE = type(Path())
_SOURCE_LOCK_NAME = ".source-lock-v2"
_SOURCE_LOCK_STATE = threading.local()
SOURCE_LOCK_OPERATIONS = frozenset((
    "root_ancestors", "lock_create_open", "lock_existing_open", "lock_initial_fstat",
    "lock_fchmod", "lock_postchmod_fstat", "lock_initial_lstat", "lock_helper_error_close",
    "lock_acquire", "lock_recheck_fstat", "lock_recheck_lstat", "root_recheck_ancestors",
    "lock_entry_cleanup_close", "unrecognized",
))
_ERRNO_CATEGORIES = {
    errno.EROFS: "read_only_fs", errno.EACCES: "permission", errno.EPERM: "permission",
    errno.ENOENT: "missing", errno.ENOTDIR: "path_structure", errno.ELOOP: "path_structure",
    errno.EISDIR: "path_structure", errno.ENOSPC: "storage_limit", errno.EDQUOT: "storage_limit",
    errno.EMFILE: "descriptor_limit", errno.ENFILE: "descriptor_limit",
    errno.ENOLCK: "lock_resource", errno.EBADF: "invalid_descriptor", errno.EINTR: "interrupted",
    errno.EIO: "io", errno.ENOSYS: "unsupported", errno.EOPNOTSUPP: "unsupported",
}
SOURCE_LOCK_ERRNO_CATEGORIES = frozenset((*_ERRNO_CATEGORIES.values(), "other"))


@dataclass
class SourceLockObservation:
    """Invocation-local attribution, matched by exception identity, never by text."""

    error: BaseException | None = None
    operation: str = "unrecognized"

    def tokens(self, error: BaseException) -> tuple[str, str]:
        operation = self.operation if self.error is error else "unrecognized"
        if type(operation) is not str or operation not in SOURCE_LOCK_OPERATIONS:
            operation = "unrecognized"
        # Read the built-in slot directly: custom exception properties are not diagnostics.
        number = OSError.errno.__get__(error) if issubclass(type(error), OSError) else None
        category = _ERRNO_CATEGORIES.get(number, "other") if type(number) is int else "other"
        return operation, category


@contextmanager
def observe_source_lock() -> Iterator[SourceLockObservation]:
    previous = getattr(_SOURCE_LOCK_STATE, "observation", None)
    observation = SourceLockObservation()
    _SOURCE_LOCK_STATE.observation = observation
    try:
        yield observation
    finally:
        _SOURCE_LOCK_STATE.observation = previous


def _lock_operation(operation, function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except OSError as error:
        observation = getattr(_SOURCE_LOCK_STATE, "observation", None)
        if type(observation) is SourceLockObservation:
            observation.error = error
            observation.operation = operation
        raise


@dataclass(frozen=True)
class SourceRootIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int

    def to_contract(self) -> dict[str, int]:
        return {
            "device": self.device,
            "gid": self.gid,
            "inode": self.inode,
            "mode": self.mode,
            "uid": self.uid,
        }


@dataclass(frozen=True)
class SourceLockBinding:
    root: Path
    identity: SourceRootIdentity

    def revalidate(self) -> None:
        if _source_root_identity(self.root, operation="root_recheck_ancestors") != self.identity:
            raise ValueError("source root changed while locked")


@dataclass(frozen=True)
class SourceFileBinding:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    ctime_ns: int
    mtime_ns: int
    size_bytes: int
    sha256: str

    def to_contract(self) -> dict[str, int | str]:
        return {
            "ctime_ns": self.ctime_ns,
            "device": self.device,
            "gid": self.gid,
            "inode": self.inode,
            "mode": self.mode,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "uid": self.uid,
        }


def _source_file_stat(item: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_uid != os.geteuid()
        or item.st_nlink != 1
        or stat.S_IMODE(item.st_mode) != 0o600
    ):
        raise ValueError("source file must be a private regular file")
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_uid,
        item.st_gid,
        item.st_ctime_ns,
        item.st_mtime_ns,
    )


def capture_private_source_file(
    path: Path,
    *,
    maximum: int,
) -> tuple[bytes, SourceFileBinding]:
    """Read one source input once and bind its content to its visible inode."""
    if type(path) is not _PATH_TYPE or not path.is_absolute():
        raise ValueError("source file path is invalid")
    if type(maximum) is not int or maximum < 0:
        raise ValueError("source file maximum is invalid")
    assert_no_symlink_ancestors(path, label="source file")
    flags = os.O_RDONLY
    for flag_name in ("O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK"):
        flags |= getattr(os, flag_name, 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise ValueError("source file must be a private regular file") from exc
        raise
    try:
        initial = os.fstat(fd)
        identity = _source_file_stat(initial)
        if initial.st_size > maximum:
            raise ValueError("source file exceeds maximum size")
        payload = bytearray()
        while len(payload) < initial.st_size:
            chunk = os.read(fd, min(65_536, initial.st_size - len(payload)))
            if not chunk:
                raise ValueError("source file changed while reading")
            payload.extend(chunk)
        final = os.fstat(fd)
        visible = path.lstat()
        if (
            _source_file_stat(final) != identity
            or final.st_size != initial.st_size
            or _source_file_stat(visible) != identity
            or visible.st_size != initial.st_size
        ):
            raise ValueError("source file changed while reading")
        raw = bytes(payload)
        return raw, SourceFileBinding(
            device=identity[0],
            inode=identity[1],
            mode=stat.S_IMODE(identity[2]),
            uid=identity[3],
            gid=identity[4],
            ctime_ns=identity[5],
            mtime_ns=identity[6],
            size_bytes=initial.st_size,
            sha256=hashlib.sha256(raw).hexdigest(),
        )
    finally:
        os.close(fd)


def _source_root_identity(root: Path, *, operation: str = "root_ancestors") -> SourceRootIdentity:
    if type(root) is not _PATH_TYPE or not root.is_absolute():
        raise ValueError("source root is invalid")
    _lock_operation(operation, assert_no_symlink_ancestors, root, label="source root")
    try:
        item = root.lstat()
    except OSError as exc:
        raise ValueError("source root is unavailable") from exc
    if (
        not stat.S_ISDIR(item.st_mode)
        or stat.S_IMODE(item.st_mode) != 0o700
        or item.st_uid != os.geteuid()
    ):
        raise ValueError("source root must be a private real directory")
    return SourceRootIdentity(
        item.st_dev,
        item.st_ino,
        stat.S_IMODE(item.st_mode),
        item.st_uid,
        item.st_gid,
    )


def capture_private_source_directory(path: Path) -> SourceRootIdentity:
    return _source_root_identity(path)


def _source_lock_fd(
    root: Path,
    *,
    create: bool,
) -> tuple[int, tuple[int, int, int, int, int]]:
    path = root / _SOURCE_LOCK_NAME
    flags = os.O_RDWR
    for flag_name in ("O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK"):
        flags |= getattr(os, flag_name, 0)
    try:
        if create:
            try:
                fd = _lock_operation("lock_create_open", os.open, path,
                                     flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                fd = _lock_operation("lock_existing_open", os.open, path, flags)
        else:
            fd = _lock_operation("lock_existing_open", os.open, path, flags)
    except OSError as exc:
        if exc.errno == errno.ENOENT and not create:
            raise ValueError("source lock is unavailable") from exc
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise ValueError("source lock must be a private regular file") from exc
        raise
    try:
        item = _lock_operation("lock_initial_fstat", os.fstat, fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_uid != os.geteuid()
            or item.st_nlink != 1
        ):
            raise ValueError("source lock must be a private regular file")
        if not create and stat.S_IMODE(item.st_mode) != 0o600:
            raise ValueError("source lock must be a private regular file")
        if stat.S_IMODE(item.st_mode) != 0o600:
            _lock_operation("lock_fchmod", os.fchmod, fd, 0o600)
            item = _lock_operation("lock_postchmod_fstat", os.fstat, fd)
        identity = (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_uid,
            item.st_gid,
        )
        visible = _lock_operation("lock_initial_lstat", path.lstat)
        if (
            not stat.S_ISREG(visible.st_mode)
            or visible.st_nlink != 1
            or visible.st_uid != os.geteuid()
            or stat.S_IMODE(visible.st_mode) != 0o600
            or (visible.st_dev, visible.st_ino) != identity[:2]
        ):
            raise ValueError("source lock must be a private regular file")
        return fd, identity
    except BaseException:
        _lock_operation("lock_helper_error_close", os.close, fd)
        raise


def _revalidate_source_lock(
    root: Path,
    fd: int,
    expected: tuple[int, int, int, int, int],
) -> None:
    item = _lock_operation("lock_recheck_fstat", os.fstat, fd)
    visible = _lock_operation("lock_recheck_lstat", (root / _SOURCE_LOCK_NAME).lstat)
    current = (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_uid,
        item.st_gid,
    )
    if (
        current != expected
        or not stat.S_ISREG(item.st_mode)
        or item.st_nlink != 1
        or not stat.S_ISREG(visible.st_mode)
        or visible.st_nlink != 1
        or (visible.st_dev, visible.st_ino) != expected[:2]
        or visible.st_uid != expected[3]
        or visible.st_gid != expected[4]
        or visible.st_mode != expected[2]
    ):
        raise ValueError("source lock changed while locked")


def _acquire_source_lock(fd: int, timeout_seconds: int | float) -> None:
    if type(timeout_seconds) not in (int, float) or timeout_seconds < 0:
        raise ValueError("source lock timeout is invalid")
    deadline = time.monotonic() + float(timeout_seconds)
    while True:
        try:
            _lock_operation("lock_acquire", fcntl.flock, fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError("source lock is already in use") from exc
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


@contextmanager
def source_lock(
    root: Path,
    *,
    timeout_seconds: int | float = 30,
    create_root: bool = False,
    create_lock: bool = True,
) -> Iterator[SourceLockBinding]:
    """Serialize trusted source access without creating a requested absent lock."""
    if type(create_root) is not bool:
        raise ValueError("source lock create_root is invalid")
    if type(create_lock) is not bool:
        raise ValueError("source lock create_lock is invalid")
    if create_root and not create_lock:
        raise ValueError("source lock cannot create root without creating a lock")
    if type(root) is not _PATH_TYPE or not root.is_absolute():
        raise ValueError("source root is invalid")
    if create_root:
        try:
            root.lstat()
        except FileNotFoundError:
            ensure_private_directory(root, label="source root")
    identity = _source_root_identity(root)
    held = getattr(_SOURCE_LOCK_STATE, "held", None)
    if held is None:
        held = {}
        _SOURCE_LOCK_STATE.held = held
    key = Path(os.path.abspath(root))
    existing = held.get(key)
    if existing is not None:
        if type(existing) is not SourceLockBinding or existing.identity != identity:
            raise ValueError("source root changed while locked")
        existing.revalidate()
        try:
            yield existing
        finally:
            existing.revalidate()
        return
    fd = -1
    primary_error: BaseException | None = None
    try:
        fd, lock_identity = _source_lock_fd(root, create=create_lock)
        _acquire_source_lock(fd, timeout_seconds)
        _revalidate_source_lock(root, fd, lock_identity)
        binding = SourceLockBinding(root=root, identity=identity)
        binding.revalidate()
        held[key] = binding
        try:
            yield binding
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            held.pop(key, None)
            binding.revalidate()
            _revalidate_source_lock(root, fd, lock_identity)
    finally:
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                _lock_operation("lock_entry_cleanup_close", os.close, fd)
            except OSError:
                if primary_error is None:
                    raise


def _source_lock_is_held(root: Path) -> bool:
    """Return whether this thread already owns the exact bound Source lock."""
    identity = _source_root_identity(root)
    held = getattr(_SOURCE_LOCK_STATE, "held", None)
    if held is None:
        return False
    key = Path(os.path.abspath(root))
    existing = held.get(key)
    if existing is None:
        return False
    if type(existing) is not SourceLockBinding or existing.identity != identity:
        raise ValueError("source root changed while locked")
    existing.revalidate()
    return True


def source_root_for_state_directory(directory: Path) -> Path:
    if type(directory) is not _PATH_TYPE or not directory.is_absolute():
        raise ValueError("state directory is invalid")
    return directory.parent


def source_root_for_history_path(path: Path) -> Path:
    if type(path) is not _PATH_TYPE or not path.is_absolute():
        raise ValueError("history path is invalid")
    return path.parent
