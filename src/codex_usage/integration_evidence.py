from __future__ import annotations

import errno
import fcntl
import math
import os
import pwd
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from . import private_io
from .private_io import (
    IntegrationEvidenceError,
    IntegrationEvidenceInvalid,
    IntegrationEvidenceUnavailable,
)

_LOCK_MAX_BYTES = 4096
_EVIDENCE_LOCK_STATE = threading.local()


class IntegrationBusy(IntegrationEvidenceError):
    pass


def _evidence_lock_name(target: Path) -> str:
    if type(target) is not type(Path()) or not target.is_absolute():
        raise IntegrationEvidenceInvalid()
    return private_io._private_lock_name(target)


def _deadline(timeout_seconds: float) -> float:
    if type(timeout_seconds) not in (int, float):
        raise IntegrationEvidenceInvalid()
    try:
        seconds = float(timeout_seconds)
    except (OverflowError, TypeError, ValueError):
        raise IntegrationEvidenceInvalid() from None
    if not math.isfinite(seconds) or seconds < 0:
        raise IntegrationEvidenceInvalid()
    deadline = time.monotonic() + seconds
    if not math.isfinite(deadline):
        raise IntegrationEvidenceInvalid()
    return deadline


def _validate_directory(item: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(item.st_mode)
        or item.st_uid != os.getuid()
        or stat.S_IMODE(item.st_mode) != 0o700
    ):
        raise IntegrationEvidenceInvalid()


def _open_lock_root(*, create: bool) -> int:
    lock_root = private_io._private_lock_root()
    if create:
        try:
            private_io.ensure_private_directory(
                lock_root,
                label="integration evidence lock root",
            )
        except (OSError, ValueError) as exc:
            raise IntegrationEvidenceInvalid() from exc
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        passwd_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise IntegrationEvidenceUnavailable() from exc
    enforce_from = len(lock_root.parts) - 1
    if lock_root.parts[: len(passwd_home.parts)] == passwd_home.parts:
        enforce_from = len(passwd_home.parts) - 1
    fd = -1
    try:
        fd = os.open(lock_root.anchor, flags)
        for index, component in enumerate(lock_root.parts[1:], start=1):
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if index >= enforce_from:
                _validate_directory(os.fstat(fd))
        result = fd
        fd = -1
        return result
    except FileNotFoundError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    except IntegrationEvidenceError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise IntegrationEvidenceInvalid() from exc
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _open_lock_file(lock_root_fd: int, name: str, *, create: bool) -> int:
    flags = (
        os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    created = False
    try:
        try:
            fd = os.open(name, flags, dir_fd=lock_root_fd)
        except FileNotFoundError:
            if not create:
                raise IntegrationEvidenceUnavailable() from None
            fd = os.open(
                name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=lock_root_fd,
            )
            created = True
        if created:
            os.fchmod(fd, 0o600)
            os.fsync(lock_root_fd)
        item = os.fstat(fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_uid != os.getuid()
            or item.st_nlink != 1
            or stat.S_IMODE(item.st_mode) != 0o600
            or item.st_size > _LOCK_MAX_BYTES
        ):
            raise IntegrationEvidenceInvalid()
        return fd
    except IntegrationEvidenceError:
        if "fd" in locals():
            os.close(fd)
        raise
    except OSError as exc:
        if "fd" in locals():
            os.close(fd)
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise IntegrationEvidenceInvalid() from exc
        raise IntegrationEvidenceUnavailable() from exc


def _acquire_lock(fd: int, *, mode: str, deadline: float) -> None:
    operation = {"shared": fcntl.LOCK_SH, "exclusive": fcntl.LOCK_EX}[mode]
    before = os.fstat(fd)
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            break
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                raise IntegrationBusy() from exc
            time.sleep(0.05)
        except OSError as exc:
            raise IntegrationEvidenceUnavailable() from exc
    after = os.fstat(fd)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_mode != after.st_mode
        or before.st_uid != after.st_uid
        or before.st_nlink != after.st_nlink
        or after.st_size > _LOCK_MAX_BYTES
    ):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        raise IntegrationEvidenceInvalid()


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(fd)


def _verify_lock_target_parent(state_home: Path) -> None:
    state_fd = -1
    app_fd = -1
    integration_fd = -1
    try:
        state_fd = private_io.open_verified_state_home(state_home)
        app_fd = private_io.open_private_dir_at(state_fd, "codex-usage")
        integration_fd = private_io.open_private_dir_at(app_fd, "integration")
    except FileNotFoundError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    except OSError as exc:
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


@contextmanager
def evidence_lock_set(
    *,
    state_home: Path,
    release_mode: Literal["shared", "exclusive"],
    current_mode: Literal["shared", "exclusive"],
    timeout_seconds: float,
    create: bool,
) -> Iterator[None]:
    if type(state_home) is not type(Path()) or not state_home.is_absolute():
        raise IntegrationEvidenceInvalid()
    if release_mode not in {"shared", "exclusive"} or current_mode not in {
        "shared",
        "exclusive",
    }:
        raise IntegrationEvidenceInvalid()
    if type(create) is not bool:
        raise IntegrationEvidenceInvalid()
    _verify_lock_target_parent(state_home)
    deadline = _deadline(timeout_seconds)
    held = getattr(_EVIDENCE_LOCK_STATE, "held", None)
    if held is None:
        held = {}
        _EVIDENCE_LOCK_STATE.held = held
    if "current" in held and "release" not in held:
        raise IntegrationBusy()
    if held:
        if set(held) != {"release", "current"}:
            raise IntegrationBusy()
        if held["release"][0] != release_mode or held["current"][0] != current_mode:
            raise IntegrationBusy()
        held["release"] = (release_mode, held["release"][1] + 1)
        held["current"] = (current_mode, held["current"][1] + 1)
        try:
            yield
        finally:
            held["current"] = (current_mode, held["current"][1] - 1)
            held["release"] = (release_mode, held["release"][1] - 1)
        return

    integration = state_home / "codex-usage" / "integration"
    targets = (
        ("release", integration / "producer-install", release_mode),
        ("current", integration / "current.json", current_mode),
    )
    root_fd = _open_lock_root(create=create)
    acquired: list[tuple[str, int]] = []
    try:
        for logical_name, target, mode in targets:
            fd = _open_lock_file(
                root_fd,
                _evidence_lock_name(target),
                create=create,
            )
            try:
                _acquire_lock(fd, mode=mode, deadline=deadline)
            except Exception:
                os.close(fd)
                raise
            acquired.append((logical_name, fd))
            held[logical_name] = (mode, 1)
        try:
            yield
        finally:
            held.clear()
    finally:
        held.clear()
        for _, fd in reversed(acquired):
            _release_lock(fd)
        os.close(root_fd)
