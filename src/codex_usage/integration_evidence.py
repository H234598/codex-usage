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
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import private_io
from .private_io import (
    FileIdentity,
    IntegrationEvidenceError,
    IntegrationEvidenceInvalid,
    IntegrationEvidenceUnavailable,
)

_LOCK_MAX_BYTES = 4096
_EVIDENCE_LOCK_STATE = threading.local()


@dataclass
class _HeldEvidenceLocks:
    state_identity: FileIdentity
    integration_identity: FileIdentity
    lock_root_identity: FileIdentity
    release_name: str
    release_identity: FileIdentity
    current_name: str
    current_identity: FileIdentity
    release_mode: str
    current_mode: str
    depth: int = 1


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


def _fd_identity(fd: int) -> FileIdentity:
    item = os.fstat(fd)
    return FileIdentity(item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode))


def _verify_lock_target_parent(state_home: Path) -> tuple[FileIdentity, FileIdentity]:
    state_fd = -1
    app_fd = -1
    integration_fd = -1
    try:
        state_fd = private_io.open_verified_state_home(state_home)
        state_identity = _fd_identity(state_fd)
        app_fd = private_io.open_private_dir_at(state_fd, "codex-usage")
        integration_fd = private_io.open_private_dir_at(app_fd, "integration")
        integration_identity = _fd_identity(integration_fd)
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
    return state_identity, integration_identity


def _matches_held_lock_set(
    held_set: _HeldEvidenceLocks,
    *,
    state_identity: FileIdentity,
    integration_identity: FileIdentity,
    release_name: str,
    current_name: str,
    create: bool,
) -> bool:
    if (
        held_set.state_identity != state_identity
        or held_set.integration_identity != integration_identity
        or held_set.release_name != release_name
        or held_set.current_name != current_name
    ):
        return False
    root_fd = _open_lock_root(create=create)
    release_fd = -1
    current_fd = -1
    try:
        if _fd_identity(root_fd) != held_set.lock_root_identity:
            return False
        release_fd = _open_lock_file(root_fd, release_name, create=create)
        if _fd_identity(release_fd) != held_set.release_identity:
            return False
        current_fd = _open_lock_file(root_fd, current_name, create=create)
        return _fd_identity(current_fd) == held_set.current_identity
    finally:
        if current_fd >= 0:
            os.close(current_fd)
        if release_fd >= 0:
            os.close(release_fd)
        os.close(root_fd)


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
    state_identity, integration_identity = _verify_lock_target_parent(state_home)
    deadline = _deadline(timeout_seconds)
    order_probe = getattr(_EVIDENCE_LOCK_STATE, "held", None)
    if order_probe is not None and "current" in order_probe and "release" not in order_probe:
        raise IntegrationBusy()

    integration = state_home / "codex-usage" / "integration"
    release_name = _evidence_lock_name(integration / "producer-install")
    current_name = _evidence_lock_name(integration / "current.json")
    held_sets = getattr(_EVIDENCE_LOCK_STATE, "sets", None)
    if held_sets is None:
        held_sets = []
        _EVIDENCE_LOCK_STATE.sets = held_sets
    for held_set in held_sets:
        if not _matches_held_lock_set(
            held_set,
            state_identity=state_identity,
            integration_identity=integration_identity,
            release_name=release_name,
            current_name=current_name,
            create=create,
        ):
            continue
        if (
            held_set.release_mode != release_mode
            or held_set.current_mode != current_mode
        ):
            raise IntegrationBusy()
        held_set.depth += 1
        try:
            yield
        finally:
            held_set.depth -= 1
        return

    targets = (
        ("release", release_name, release_mode),
        ("current", current_name, current_mode),
    )
    root_fd = _open_lock_root(create=create)
    lock_root_identity = _fd_identity(root_fd)
    acquired: list[tuple[str, int]] = []
    acquired_identities: dict[str, FileIdentity] = {}
    held_set = None
    try:
        for logical_name, lock_name, mode in targets:
            fd = _open_lock_file(
                root_fd,
                lock_name,
                create=create,
            )
            try:
                _acquire_lock(fd, mode=mode, deadline=deadline)
            except Exception:
                os.close(fd)
                raise
            acquired.append((logical_name, fd))
            acquired_identities[logical_name] = _fd_identity(fd)
        held_set = _HeldEvidenceLocks(
            state_identity=state_identity,
            integration_identity=integration_identity,
            lock_root_identity=lock_root_identity,
            release_name=release_name,
            release_identity=acquired_identities["release"],
            current_name=current_name,
            current_identity=acquired_identities["current"],
            release_mode=release_mode,
            current_mode=current_mode,
        )
        held_sets.append(held_set)
        try:
            yield
        finally:
            held_sets.remove(held_set)
    finally:
        if held_set is not None and held_set in held_sets:
            held_sets.remove(held_set)
        for _, fd in reversed(acquired):
            _release_lock(fd)
        os.close(root_fd)
