from __future__ import annotations

import errno
import fcntl
import glob
import hashlib
import math
import os
import pwd
import secrets
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

PRIVATE_LOCK_TIMEOUT_SECONDS = 30
_PATH_TYPE = type(Path())
_MAX_STALE_ROLLBACKS = 1
_MAX_PRIVATE_ROLLBACK_BYTES = 64 * 1024 * 1024
_PRIVATE_LOCK_STATE = threading.local()


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int


class IntegrationEvidenceError(Exception):
    pass


class IntegrationEvidenceInvalid(IntegrationEvidenceError):
    pass


class IntegrationEvidenceUnavailable(IntegrationEvidenceError):
    pass


def _private_lock_root() -> Path:
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ValueError("cannot determine private lock home") from exc
    if not home.is_absolute():
        raise ValueError("private lock home must be absolute")
    return home / ".local" / "state" / "codex-usage" / "locks"


def _private_lock_path(path: Path) -> Path:
    root = _private_lock_root()
    ensure_private_directory(root, label="private lock directory")
    return root / _private_lock_name(path)


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


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _regular_open_flags(*, writable: bool = False) -> int:
    flags = os.O_WRONLY if writable else os.O_RDONLY
    return (
        flags
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


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
    return FileIdentity(item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode))


def _require_private_directory_fd(fd: int) -> FileIdentity:
    try:
        item = os.fstat(fd)
    except OSError as exc:
        raise ValueError("private directory descriptor is invalid") from exc
    if (
        not stat.S_ISDIR(item.st_mode)
        or item.st_uid != os.getuid()
        or stat.S_IMODE(item.st_mode) != 0o700
    ):
        raise ValueError("private directory descriptor is invalid")
    return _directory_identity(item)


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
        for component in state_home.parts[1:]:
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError("state home contains an unsafe component") from exc
                raise
            os.close(current_fd)
            current_fd = next_fd
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
        or item.st_uid != os.getuid()
        or item.st_nlink != 1
        or stat.S_IMODE(item.st_mode) != mode
        or item.st_size > maximum
    ):
        raise ValueError("private file descriptor is invalid")
    return FileIdentity(item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode))


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
                    and candidate.st_uid == os.getuid()
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
        or candidate_stat.st_uid != os.getuid()
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
    if not stat.S_ISREG(target_stat.st_mode) or target_stat.st_uid != os.getuid():
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
            or opened_stat.st_uid != os.getuid()
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
            or rollback_stat.st_uid != os.getuid()
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
                or item.st_uid != os.getuid()
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
        if target_stat.st_nlink != 1 or target_stat.st_uid != os.getuid():
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
) -> Iterator[None]:
    path = _require_path(path, label=label)
    if created_lock_files is not None and not isinstance(created_lock_files, list):
        raise ValueError("created_lock_files is invalid")
    deadline = _lock_deadline(timeout_seconds)
    parent = path.parent
    assert_no_symlink_ancestors(parent, label=label)
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError(f"{label} parent must be a real directory: {parent}")
    lock_path = _private_lock_path(path)
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise ValueError(f"{label} must be a regular file: {lock_path}")
    lock_key = Path(os.path.abspath(lock_path))
    held_lock_paths = getattr(_PRIVATE_LOCK_STATE, "paths", None)
    if held_lock_paths is None:
        held_lock_paths = set()
        _PRIVATE_LOCK_STATE.paths = held_lock_paths
    if lock_key in held_lock_paths:
        yield
        return
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    lock_created = False
    try:
        if created_lock_files is None:
            fd = os.open(lock_path, flags | os.O_CREAT, 0o600)
        else:
            try:
                fd = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                lock_created = True
            except FileExistsError:
                fd = os.open(lock_path, flags)
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
        if lock_created:
            created_lock_files.append((lock_path, file_stat.st_dev, file_stat.st_ino))
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{label} is already in use") from exc
                time.sleep(0.05)
        held_lock_paths.add(lock_key)
        try:
            yield
        finally:
            held_lock_paths.remove(lock_key)
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
