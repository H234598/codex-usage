from __future__ import annotations

import fcntl
import getpass
import hmac
import json
import os
import secrets
import socket
import stat
import struct
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from .private_io import read_private_bytes_at

MASTERJET_BEARER_CREDENTIAL = "masterjet-control-bearer"
MASTERJET_ATTESTATION_CREDENTIAL = "masterjet-local-attestation-key"
MAX_BEARER_BYTES = 4096
MAX_STEP_UP_BYTES = 128
STEP_UP_SENTINEL = b"CODEX_USAGE_STEP_UP_REQUIRED\n"
_MAX_ATTESTATION_BYTES = 2048
_ATTESTATION_NONCE_BYTES = 32
_ATTESTATION_TRANSCRIPT_DOMAIN = b"codex-master/admin-socket/attestation/transcript\0"
_ATTESTATION_CLIENT_DOMAIN = b"codex-master/admin-socket/attestation/client-proof\0"
_ATTESTATION_SERVER_DOMAIN = b"codex-master/admin-socket/attestation/server-proof\0"


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


def local_attestation_verifier_from_systemd_credentials(
    *, environ: Mapping[str, str] | None = None
) -> Callable[[int, int, int, socket.socket], bool]:
    values = os.environ if environ is None else environ
    directory = values.get("CREDENTIALS_DIRECTORY")
    snapshot = Path(directory) if isinstance(directory, str) and directory else None

    def verify(pid: int, uid: int, gid: int, connection: socket.socket) -> bool:
        key = bytearray()
        server_nonce = bytearray()
        client_nonce = bytearray()
        transcript = bytearray()
        try:
            if snapshot is None or not snapshot.is_absolute():
                return False
            peer = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            if struct.unpack("3i", peer) != (pid, uid, gid):
                return False
            key = _read_attestation_key(snapshot)
            challenge = _receive_attestation_frame(connection)
            if set(challenge) != {
                "schema_version",
                "transport",
                "server_nonce",
                "server_pid",
                "server_uid",
                "server_gid",
            }:
                return False
            if challenge.get("schema_version") != 1 or type(
                challenge.get("schema_version")
            ) is not int:
                return False
            if challenge.get("transport") != "attestation.challenge":
                return False
            identity = (
                _attestation_uint(challenge.get("server_pid"), positive=True),
                _attestation_uint(challenge.get("server_uid")),
                _attestation_uint(challenge.get("server_gid")),
            )
            if identity != (pid, uid, gid):
                return False
            server_nonce = _attestation_bytes(challenge.get("server_nonce"))
            client_nonce = bytearray(secrets.token_bytes(_ATTESTATION_NONCE_BYTES))
            transcript = bytearray(
                _ATTESTATION_TRANSCRIPT_DOMAIN
                + struct.pack("!BIII", 1, pid, uid, gid)
                + bytes(server_nonce)
                + bytes(client_nonce)
            )
            client_proof = hmac.digest(
                key, _ATTESTATION_CLIENT_DOMAIN + bytes(transcript), "sha256"
            )
            _send_attestation_frame(
                connection,
                {
                    "schema_version": 1,
                    "transport": "attestation.response",
                    "client_nonce": client_nonce.hex(),
                    "proof": client_proof.hex(),
                },
            )
            accepted = _receive_attestation_frame(connection)
            if set(accepted) != {"schema_version", "transport", "proof"}:
                return False
            if (
                accepted.get("schema_version") != 1
                or type(accepted.get("schema_version")) is not int
                or accepted.get("transport") != "attestation.accepted"
            ):
                return False
            received = _attestation_bytes(accepted.get("proof"))
            expected = hmac.digest(
                key, _ATTESTATION_SERVER_DOMAIN + bytes(transcript), "sha256"
            )
            return hmac.compare_digest(received, expected)
        except Exception:
            return False
        finally:
            key[:] = b"\0" * len(key)
            server_nonce[:] = b"\0" * len(server_nonce)
            client_nonce[:] = b"\0" * len(client_nonce)
            transcript[:] = b"\0" * len(transcript)

    return verify


def bearer_provider_from_fd(fd: int) -> Callable[[], str]:
    if type(fd) is not int or fd < 0:
        raise MasterjetCredentialsError("credential unavailable")
    try:
        owned_fd = os.dup(fd)
        os.set_inheritable(owned_fd, False)
    except OSError as exc:
        raise MasterjetCredentialsError("credential unavailable") from exc
    return _one_shot(lambda: _read_bearer_from_fd(owned_fd))


def stdin_step_up_provider(
    stream: object, *, control_stream: object | None = None
) -> Callable[[], str]:
    def read() -> str:
        reader = getattr(stream, "read", None)
        if control_stream is None:
            output = getattr(sys.stderr, "buffer", sys.stderr)
        else:
            output = control_stream
        writer = getattr(output, "write", None)
        flush = getattr(output, "flush", None)
        if not callable(reader) or not callable(writer) or not callable(flush):
            raise MasterjetCredentialsError("step-up unavailable")
        try:
            writer(STEP_UP_SENTINEL)
            flush()
        except (OSError, TypeError, ValueError) as exc:
            raise MasterjetCredentialsError("step-up unavailable") from exc
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


def _read_attestation_key(directory: Path) -> bytearray:
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
        raw, _identity = read_private_bytes_at(
            fd,
            MASTERJET_ATTESTATION_CREDENTIAL,
            maximum=1024,
            mode=0o400,
        )
        payload = bytearray(raw)
        if len(payload) < 32:
            payload[:] = b"\0" * len(payload)
            raise MasterjetCredentialsError("credential unavailable")
        return payload
    except (OSError, ValueError) as exc:
        raise MasterjetCredentialsError("credential unavailable") from exc
    finally:
        os.close(fd)


def _receive_attestation_frame(connection: socket.socket) -> dict[str, object]:
    payload = bytearray()
    try:
        while b"\n" not in payload:
            chunk = connection.recv(1)
            if not chunk:
                raise MasterjetCredentialsError("attestation unavailable")
            payload.extend(chunk)
            if len(payload) > _MAX_ATTESTATION_BYTES:
                raise MasterjetCredentialsError("attestation unavailable")
        if payload[-1:] != b"\n" or payload.count(b"\n") != 1 or len(payload) == 1:
            raise MasterjetCredentialsError("attestation unavailable")
        value = json.loads(
            payload[:-1],
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        if type(value) is not dict:
            raise MasterjetCredentialsError("attestation unavailable")
        return value
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise MasterjetCredentialsError("attestation unavailable") from exc
    finally:
        payload[:] = b"\0" * len(payload)


def _send_attestation_frame(connection: socket.socket, value: dict[str, object]) -> None:
    payload = json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode("ascii") + b"\n"
    if len(payload) > _MAX_ATTESTATION_BYTES:
        raise MasterjetCredentialsError("attestation unavailable")
    connection.sendall(payload)


def _attestation_bytes(value: object) -> bytearray:
    if (
        type(value) is not str
        or len(value) != _ATTESTATION_NONCE_BYTES * 2
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise MasterjetCredentialsError("attestation unavailable")
    return bytearray.fromhex(value)


def _attestation_uint(value: object, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or not minimum <= value <= 2**32 - 1:
        raise MasterjetCredentialsError("attestation unavailable")
    return value


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError


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
