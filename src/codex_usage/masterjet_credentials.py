from __future__ import annotations

import fcntl
import getpass
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

from .private_io import read_private_bytes_at

MASTERJET_BEARER_CREDENTIAL = "masterjet-control-bearer"
MAX_BEARER_BYTES = 4096
MAX_STEP_UP_BYTES = 128


class MasterjetCredentialsError(RuntimeError):
    pass


def bearer_provider_from_systemd_credentials(
    *, environ: Mapping[str, str] | None = None
) -> Callable[[], str]:
    values = os.environ if environ is None else environ
    directory = values.get("CREDENTIALS_DIRECTORY")
    if not isinstance(directory, str) or not directory:
        return _one_shot(_credential_unavailable)
    snapshot = Path(directory)

    def read() -> str:
        return _read_bearer_from_directory(snapshot)

    return _one_shot(read)


def bearer_provider_from_fd(fd: int) -> Callable[[], str]:
    if type(fd) is not int or fd < 0:
        raise MasterjetCredentialsError("credential unavailable")
    try:
        owned_fd = os.dup(fd)
        os.set_inheritable(owned_fd, False)
    except OSError as exc:
        raise MasterjetCredentialsError("credential unavailable") from exc
    return _one_shot(lambda: _read_bearer_from_fd(owned_fd))


def stdin_step_up_provider(stream: object) -> Callable[[], str]:
    def read() -> str:
        reader = getattr(stream, "read", None)
        if not callable(reader):
            raise MasterjetCredentialsError("step-up unavailable")
        payload = bytearray(reader(MAX_STEP_UP_BYTES + 1))
        try:
            if len(payload) > MAX_STEP_UP_BYTES:
                raise MasterjetCredentialsError("step-up unavailable")
            return _step_up_from_bytes(payload)
        finally:
            payload[:] = b"\x00" * len(payload)
            payload.clear()

    return _one_shot(read)


def tty_step_up_provider() -> Callable[[], str]:
    def read() -> str:
        value = getpass.getpass("Masterjet TOTP: ")
        if not isinstance(value, str):
            raise MasterjetCredentialsError("step-up unavailable")
        payload = bytearray(value.encode("ascii"))
        try:
            return _step_up_from_bytes(payload)
        except UnicodeEncodeError as exc:
            raise MasterjetCredentialsError("step-up unavailable") from exc
        finally:
            payload[:] = b"\x00" * len(payload)
            payload.clear()

    return _one_shot(read)


def unavailable_step_up_provider() -> Callable[[], str]:
    def unavailable() -> str:
        raise MasterjetCredentialsError("step-up unavailable")

    return _one_shot(unavailable)


def _credential_unavailable() -> str:
    raise MasterjetCredentialsError("credential unavailable")


def _one_shot(reader: Callable[[], str]) -> Callable[[], str]:
    used = False

    def provide() -> str:
        nonlocal used
        if used:
            raise MasterjetCredentialsError("credential already used")
        used = True
        return reader()

    return provide


def _read_bearer_from_directory(directory: Path) -> str:
    if not directory.is_absolute():
        raise MasterjetCredentialsError("credential unavailable")
    try:
        fd = _open_private_directory(directory)
    except (OSError, ValueError) as exc:
        raise MasterjetCredentialsError("credential unavailable") from exc
    try:
        item = os.fstat(fd)
        if (
            not stat.S_ISDIR(item.st_mode)
            or item.st_uid != os.geteuid()
            or stat.S_IMODE(item.st_mode) & 0o077
        ):
            raise MasterjetCredentialsError("credential unavailable")
        payload, _identity = read_private_bytes_at(
            fd, MASTERJET_BEARER_CREDENTIAL, maximum=MAX_BEARER_BYTES, mode=0o400
        )
    except (OSError, ValueError) as exc:
        raise MasterjetCredentialsError("credential unavailable") from exc
    finally:
        os.close(fd)
    return _bearer_from_bytes(payload)


def _read_bearer_from_fd(fd: int) -> str:
    payload = bytearray()
    try:
        if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
            raise MasterjetCredentialsError("credential unavailable")
        initial = os.fstat(fd)
        _private_credential_stat(initial)
        payload.extend(os.pread(fd, MAX_BEARER_BYTES + 1, 0))
        final = os.fstat(fd)
        if _stable_stat(final) != _stable_stat(initial):
            raise MasterjetCredentialsError("credential unavailable")
    except OSError as exc:
        raise MasterjetCredentialsError("credential unavailable") from exc
    else:
        return _bearer_from_bytes(payload)
    finally:
        payload[:] = b"\x00" * len(payload)
        payload.clear()
        os.close(fd)


def _open_private_directory(directory: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    current = os.open("/", flags)
    try:
        for component in directory.parts[1:]:
            if component in {"", ".", ".."}:
                raise MasterjetCredentialsError("credential unavailable")
            next_fd = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def _stable_stat(item: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_uid,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _private_credential_stat(item: os.stat_result) -> None:
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_uid != os.geteuid()
        or item.st_nlink != 1
        or stat.S_IMODE(item.st_mode) != 0o400
        or item.st_size > MAX_BEARER_BYTES
    ):
        raise MasterjetCredentialsError("credential unavailable")


def _bearer_from_bytes(payload: bytes | bytearray) -> str:
    secret = bytearray(payload)
    try:
        if not secret or len(secret) > MAX_BEARER_BYTES:
            raise MasterjetCredentialsError("credential unavailable")
        value = secret.decode("ascii")
        if any(not 0x21 <= ord(character) <= 0x7E for character in value):
            raise MasterjetCredentialsError("credential unavailable")
        return value
    except UnicodeDecodeError as exc:
        raise MasterjetCredentialsError("credential unavailable") from exc
    finally:
        secret[:] = b"\x00" * len(secret)
        secret.clear()


def _step_up_from_bytes(payload: bytearray) -> str:
    if payload.endswith(b"\n"):
        del payload[-1:]
    if len(payload) not in {6, 7, 8} or not payload.isdigit():
        raise MasterjetCredentialsError("step-up unavailable")
    try:
        return payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise MasterjetCredentialsError("step-up unavailable") from exc
