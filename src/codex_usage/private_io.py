from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


def read_private_text(
    path: Path,
    *,
    regular_label: str,
    read_label: str,
    max_bytes: int,
    too_large_label: str | None = None,
    invalid_utf8_label: str | None = None,
) -> tuple[str, os.stat_result]:
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
        if not stat.S_ISREG(file_stat.st_mode):
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


def write_private_text(path: Path, text: str, *, label: str, mode: int = 0o600) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"{label} must be a regular file: {path}")

    flags = os.O_WRONLY | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK

    try:
        fd = os.open(path, flags, mode)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise ValueError(f"{label} must be a regular file: {path}") from exc
        raise

    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"{label} must be a regular file: {path}")
        if file_stat.st_nlink != 1:
            raise ValueError(f"{label} must not be hard-linked: {path}")
        os.fchmod(fd, mode)
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
    finally:
        if fd >= 0:
            os.close(fd)
