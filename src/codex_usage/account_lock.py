from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import default_state_dir
from .private_io import assert_no_symlink_ancestors

ACCOUNT_LOCK_TIMEOUT_SECONDS = 30


class AccountLockError(Exception):
    pass


@contextmanager
def account_lock(
    account_id: str,
    *,
    timeout_seconds: int = ACCOUNT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    if account_id in {".", ".."} or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,64}", account_id
    ):
        raise AccountLockError("invalid account id for lock")
    directory = default_state_dir() / "locks"
    _prepare_lock_directory(directory)
    path = directory / f"{account_id}.lock"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise AccountLockError("account lock must be a regular file")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise AccountLockError("account lock must be a regular file") from exc
        raise AccountLockError("could not open account lock") from exc
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise AccountLockError("account lock must be a private regular file")
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise AccountLockError("account operation is already running") from exc
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _prepare_lock_directory(path: Path) -> None:
    try:
        assert_no_symlink_ancestors(path, label="account lock directory")
    except ValueError as exc:
        raise AccountLockError(str(exc)) from exc
    if path.is_symlink():
        raise AccountLockError("account lock directory must not be a symlink")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise AccountLockError("account lock directory must be a real directory")
    path.chmod(0o700)
