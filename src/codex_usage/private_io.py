from __future__ import annotations

import errno
import fcntl
import math
import os
import secrets
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PRIVATE_LOCK_TIMEOUT_SECONDS = 30
_PATH_TYPE = type(Path())


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
        or item.st_uid != os.getuid()
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
        if not stat.S_ISDIR(item.st_mode) or item.st_uid != os.getuid():
            raise ValueError(f"{label} must be a private user-owned directory: {path}")
        os.fchmod(fd, 0o700)
    finally:
        if fd >= 0:  # pragma: no branch - exception unwind
            os.close(fd)


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
    while not current.exists():
        if current.is_symlink():
            raise ValueError(f"{label} must not be a symlink: {current}")
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise ValueError(f"{label} has no usable directory parent: {raw_path}")
        current = parent
    if current.is_symlink() or not current.is_dir():
        raise ValueError(f"{label} must be a real directory: {current}")

    for candidate in reversed(missing):
        created = False
        try:
            candidate.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        if created and created_paths is not None:
            item = candidate.lstat()
            created_paths.append((candidate, item.st_dev, item.st_ino))
        _require_private_directory(candidate, label=label)
        _chmod_private_directory(candidate, label=label)

    _require_private_directory(raw_path, label=label)
    _chmod_private_directory(raw_path, label=label)
    return raw_path


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
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.getuid():
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


def write_private_text(
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
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"{label} must be a regular file: {path}")
    target_stat = None
    if path.exists():
        target_stat = path.stat()
        if target_stat.st_nlink != 1 or target_stat.st_uid != os.getuid():
            raise ValueError(f"{label} must be a private user-owned file: {path}")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError(f"{label} parent must be a real directory: {parent}")

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
            or file_stat.st_uid != os.getuid()
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
                os.link(path, rollback, follow_symlinks=False)
                rollback_exists = True
                current_stat = path.lstat()
                rollback_stat = rollback.lstat()
                if (
                    not stat.S_ISREG(current_stat.st_mode)
                    or current_stat.st_dev != target_stat.st_dev
                    or current_stat.st_ino != target_stat.st_ino
                    or rollback_stat.st_dev != target_stat.st_dev
                    or rollback_stat.st_ino != target_stat.st_ino
                    or current_stat.st_nlink != 2
                    or rollback_stat.st_nlink != 2
                    or current_stat.st_uid != os.getuid()
                    or rollback_stat.st_uid != os.getuid()
                ):
                    raise ValueError(f"{label} changed before replacement")
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
) -> Iterator[None]:
    path = _require_path(path, label=label)
    deadline = _lock_deadline(timeout_seconds)
    parent = path.parent
    assert_no_symlink_ancestors(parent, label=label)
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError(f"{label} parent must be a real directory: {parent}")
    lock_path = parent / (path.name + ".lock")
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise ValueError(f"{label} must be a regular file: {lock_path}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise ValueError(f"{label} must be a regular file: {lock_path}") from exc
        raise
    try:
        file_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
            or file_stat.st_uid != os.getuid()
        ):
            raise ValueError(f"{label} must be a private regular file: {lock_path}")
        os.fchmod(fd, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{label} is already in use") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


def _require_path(path: object, *, label: str) -> Path:
    if type(path) is not _PATH_TYPE:
        raise ValueError(f"{label} path is invalid")
    return path
