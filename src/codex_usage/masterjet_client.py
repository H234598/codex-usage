from __future__ import annotations

import array
import http.client
import json
import os
import re
import socket
import ssl
import stat
import struct
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from .config import MasterjetConnection
from .masterjet_contracts import (
    ControlContractError,
    ControlProblem,
    parse_control_operation,
    parse_control_problem,
    parse_google_accounts,
    parse_google_projects,
    parse_openai_accounts,
)

MAX_REQUEST_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 1_000_000
MAX_SECRET_BYTES = 10_000_000

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PRIVATE_KEY_RE = re.compile(
    r"(?:^|_)(?:access_token|refresh_token|client_secret|api_key|password|secret)(?:$|_)",
    re.IGNORECASE,
)
_SENSITIVE_PREFIXES = (
    "google.oauth.",
    "google.provision.",
    "google.billing.",
    "openai.auth-sync.",
    "openai.auth_sync.",
    "secret.ingress.",
)


class MasterjetClientError(RuntimeError):
    """Bounded, non-sensitive failure at the Masterjet client boundary."""

    def __init__(self, code: str, *, problem: ControlProblem | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.problem = problem


class MasterjetControlClient:
    __slots__ = ("_bearer_provider", "_connection", "_step_up_provider")

    def __init__(
        self,
        connection: MasterjetConnection,
        *,
        bearer_provider: Callable[[], str] | None = None,
        step_up_provider: Callable[[], str] | None = None,
    ) -> None:
        if type(connection) is not MasterjetConnection:
            raise TypeError("connection must be a MasterjetConnection")
        self._connection = connection
        self._bearer_provider = bearer_provider
        self._step_up_provider = step_up_provider

    def __repr__(self) -> str:
        return f"{type(self).__name__}(transport={self._connection.transport!r})"

    def call(
        self,
        operation: str,
        arguments: object,
        expected_generation: int | None = None,
        idempotency_key: str | None = None,
    ) -> object:
        request, secret = _encode_request(
            operation,
            arguments,
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
        )
        if self._connection.transport == "local":
            response = _UnixTransport(self._connection).request(request, secret)
        elif self._connection.transport == "https":
            response = _HttpsTransport(
                self._connection,
                bearer_provider=self._bearer_provider,
                step_up_provider=self._step_up_provider,
            ).request(operation, request, secret, expected_generation, idempotency_key)
        else:
            raise MasterjetClientError("control.endpoint_invalid")
        return _decode_response(operation, arguments, response)


class _UnixTransport:
    def __init__(self, connection: MasterjetConnection) -> None:
        self._connection = connection

    def request(self, request: bytes, secret: memoryview | None) -> tuple[int, bytes]:
        endpoint = _local_endpoint(self._connection)
        before = _socket_identity(endpoint)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        secret_file = None
        try:
            sock.settimeout(_timeout(self._connection))
            sock.connect(str(endpoint))
            after = _socket_identity(endpoint)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise MasterjetClientError("control.endpoint_invalid")
            _verify_peer(sock, expected_uid=before.st_uid)
            if secret is None:
                sock.sendall(request + b"\n")
            else:
                secret_file = _anonymous_secret(secret)
                descriptor = array.array("i", [secret_file.fileno()])
                framed = request + b"\n"
                sent = sock.sendmsg(
                    [framed],
                    [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptor)],
                )
                if sent < len(framed):
                    sock.sendall(framed[sent:])
            return 200, _read_json_line(sock)
        except MasterjetClientError:
            raise
        except TimeoutError:
            raise MasterjetClientError("control.timeout") from None
        except OSError:
            raise MasterjetClientError("control.transport_unavailable") from None
        finally:
            if secret_file is not None:
                secret_file.close()
            sock.close()


class _HttpsTransport:
    def __init__(
        self,
        connection: MasterjetConnection,
        *,
        bearer_provider: Callable[[], str] | None,
        step_up_provider: Callable[[], str] | None,
    ) -> None:
        self._connection = connection
        self._bearer_provider = bearer_provider
        self._step_up_provider = step_up_provider

    def request(
        self,
        operation: str,
        request: bytes,
        secret: memoryview | None,
        expected_generation: int | None,
        idempotency_key: str | None,
    ) -> tuple[int, bytes]:
        host, port, target = _https_endpoint(self._connection)
        bearer = _provider_value(self._bearer_provider, "control.authentication_required")
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
        }
        body = request
        if secret is not None:
            body = secret.tobytes()
            headers.update(_secret_request_headers(operation, expected_generation, idempotency_key))
            headers["Content-Type"] = "application/octet-stream"
        if _is_sensitive(operation):
            headers["X-Masterjet-Step-Up"] = _provider_value(
                self._step_up_provider, "control.step_up_required"
            )
        context = ssl.create_default_context()
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise MasterjetClientError("control.tls_required")
        connection = http.client.HTTPSConnection(
            host,
            port,
            timeout=_timeout(self._connection),
            context=context,
        )
        try:
            connection.request("POST", target, body=body, headers=headers)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise MasterjetClientError("control.redirect_rejected")
            content_type = response.getheader("Content-Type", "") or ""
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise MasterjetClientError("control.response_invalid")
            return response.status, _read_bounded_response(response)
        except MasterjetClientError:
            raise
        except TimeoutError:
            raise MasterjetClientError("control.timeout") from None
        except (OSError, http.client.HTTPException, ssl.SSLError):
            raise MasterjetClientError("control.transport_unavailable") from None
        finally:
            connection.close()


def _encode_request(
    operation: object,
    arguments: object,
    *,
    expected_generation: object,
    idempotency_key: object,
) -> tuple[bytes, memoryview | None]:
    operation = _token(operation, "control.request_invalid")
    if expected_generation is not None and (
        type(expected_generation) is not int or not 0 <= expected_generation <= 2**63 - 1
    ):
        raise MasterjetClientError("control.request_invalid")
    if idempotency_key is not None:
        idempotency_key = _token(idempotency_key, "control.request_invalid")

    secret = _secret_view(arguments)
    encoded_arguments: object
    if secret is None:
        if type(arguments) is not dict or _contains_private_argument(arguments):
            raise MasterjetClientError("control.request_invalid")
        encoded_arguments = arguments
    else:
        if secret.nbytes > MAX_SECRET_BYTES:
            raise MasterjetClientError("control.request_too_large")
        encoded_arguments = {"secret_fd": 0, "secret_size": secret.nbytes}

    document: dict[str, object] = {
        "schema_version": 1,
        "operation": operation,
        "arguments": encoded_arguments,
    }
    if expected_generation is not None:
        document["expected_generation"] = expected_generation
    if idempotency_key is not None:
        document["idempotency_key"] = idempotency_key
    try:
        encoded = json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError):
        raise MasterjetClientError("control.request_invalid") from None
    if len(encoded) > MAX_REQUEST_BYTES:
        raise MasterjetClientError("control.request_too_large")
    return encoded, secret


def _decode_response(operation: str, arguments: object, response: tuple[int, bytes]) -> object:
    status, body = response
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MasterjetClientError("control.response_invalid") from None
    try:
        if type(payload) is dict and "code" in payload:
            problem = parse_control_problem(payload)
            raise MasterjetClientError(problem.code, problem=problem)
        if not 200 <= status < 300:
            problem = parse_control_problem(payload)
            raise MasterjetClientError(problem.code, problem=problem)
        if operation == "google.accounts.list":
            return parse_google_accounts(payload)
        if operation == "openai.accounts.list":
            return parse_openai_accounts(payload)
        if operation == "google.projects.list":
            account_ref = arguments.get("account_ref") if type(arguments) is dict else None
            return parse_google_projects(payload, expected_account_ref=account_ref)
        return parse_control_operation(payload)
    except ControlContractError:
        raise MasterjetClientError("control.response_invalid") from None


def _local_endpoint(connection: MasterjetConnection) -> Path:
    if not connection.endpoint:
        raise MasterjetClientError("control.endpoint_invalid")
    endpoint = Path(connection.endpoint)
    if not endpoint.is_absolute():
        raise MasterjetClientError("control.endpoint_invalid")
    return endpoint


def _socket_identity(endpoint: Path) -> os.stat_result:
    try:
        identity = endpoint.lstat()
    except OSError:
        raise MasterjetClientError("control.transport_unavailable") from None
    if (
        not stat.S_ISSOCK(identity.st_mode)
        or identity.st_uid != os.geteuid()
        or stat.S_IMODE(identity.st_mode) & 0o077
    ):
        raise MasterjetClientError("control.endpoint_invalid")
    return identity


def _verify_peer(sock: socket.socket, *, expected_uid: int) -> None:
    if not hasattr(socket, "SO_PEERCRED"):
        raise MasterjetClientError("control.endpoint_invalid")
    try:
        credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", credentials)
    except (OSError, struct.error):
        raise MasterjetClientError("control.endpoint_invalid") from None
    if uid != expected_uid or uid != os.geteuid():
        raise MasterjetClientError("control.endpoint_invalid")


def _read_json_line(sock: socket.socket) -> bytes:
    received = bytearray()
    while True:
        chunk = sock.recv(min(65_536, MAX_RESPONSE_BYTES + 1 - len(received)))
        if not chunk:
            raise MasterjetClientError("control.response_invalid")
        received.extend(chunk)
        newline = received.find(b"\n")
        if newline >= 0:
            if newline > MAX_RESPONSE_BYTES:
                raise MasterjetClientError("control.response_too_large")
            if received[newline + 1 :]:
                raise MasterjetClientError("control.response_invalid")
            return bytes(received[:newline])
        if len(received) > MAX_RESPONSE_BYTES:
            raise MasterjetClientError("control.response_too_large")


def _read_bounded_response(response: http.client.HTTPResponse) -> bytes:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise MasterjetClientError("control.response_too_large")
    return body


def _anonymous_secret(secret: memoryview):
    if hasattr(os, "memfd_create"):
        fd = os.memfd_create("codex-usage-masterjet-secret", os.MFD_CLOEXEC)
        secret_file = os.fdopen(fd, "w+b", closefd=True)
    else:
        secret_file = tempfile.TemporaryFile(mode="w+b")
    secret_file.write(secret)
    secret_file.flush()
    secret_file.seek(0)
    return secret_file


def _https_endpoint(connection: MasterjetConnection) -> tuple[str, int | None, str]:
    try:
        parsed = urlsplit(connection.endpoint)
        port = parsed.port
    except ValueError:
        raise MasterjetClientError("control.endpoint_invalid") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise MasterjetClientError("control.endpoint_invalid")
    return parsed.hostname, port, parsed.path or "/"


def _provider_value(provider: Callable[[], str] | None, error: str) -> str:
    if provider is None:
        raise MasterjetClientError(error)
    try:
        value = provider()
    except Exception:
        raise MasterjetClientError(error) from None
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8", errors="ignore")) > 4096
        or any(character in value for character in "\r\n\0")
    ):
        raise MasterjetClientError(error)
    return value


def _secret_request_headers(
    operation: str,
    expected_generation: int | None,
    idempotency_key: str | None,
) -> dict[str, str]:
    headers = {"X-Masterjet-Operation": operation}
    if expected_generation is not None:
        headers["X-Masterjet-Expected-Generation"] = str(expected_generation)
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _secret_view(value: object) -> memoryview | None:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return None
    view = memoryview(value)
    if view.ndim != 1 or not view.c_contiguous:
        raise MasterjetClientError("control.request_invalid")
    return view.cast("B")


def _contains_private_argument(value: object) -> bool:
    if type(value) is dict:
        return any(
            type(key) is not str
            or _PRIVATE_KEY_RE.search(key)
            or _contains_private_argument(item)
            for key, item in value.items()
        )
    if type(value) in {list, tuple}:
        return any(_contains_private_argument(item) for item in value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return True
    return False


def _token(value: object, error: str) -> str:
    if type(value) is not str or not _TOKEN_RE.fullmatch(value):
        raise MasterjetClientError(error)
    return value


def _timeout(connection: MasterjetConnection) -> int:
    if type(connection.timeout_seconds) is not int or connection.timeout_seconds < 1:
        raise MasterjetClientError("control.endpoint_invalid")
    return connection.timeout_seconds


def _is_sensitive(operation: str) -> bool:
    return operation.startswith(_SENSITIVE_PREFIXES)
