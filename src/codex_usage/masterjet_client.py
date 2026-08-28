from __future__ import annotations

import array
import http.client
import json
import math
import os
import re
import socket
import ssl
import stat
import struct
import tempfile
import threading
import time
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
from .private_io import assert_no_symlink_ancestors

MAX_REQUEST_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 1_000_000
MAX_SECRET_BYTES = 10_000_000

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PRIVATE_KEY_RE = re.compile(
    r"(?:access|refresh)?token|clientsecret|apikey|password|credential|secret",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(?:\bAIza[A-Za-z0-9_-]*|\bya29(?:\.[A-Za-z0-9._-]*)?\b|"
    r"\b1//|\bGOCSPX-|\beyJ[A-Za-z0-9_-]{20,}|\bsk-|"
    r"(?:^|\s)Bearer\s+[A-Za-z0-9._-]{8,}|"
    r"\b(?:access_token|refresh_token|client_secret)\s*[=:]\s*\S+)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_])(?:file://|/(?:[^\s]*)|\\\\[^\s]+|[A-Za-z]:[\\/])"
)
_MAX_JSON_DEPTH = 32
_INVALID_JSON = object()
_SENSITIVE_PREFIXES = (
    "google.oauth.",
    "google.provision.",
    "google.billing.",
    "openai.auth-sync.",
    "openai.auth_sync.",
    "secret.ingress.",
)
_SECRET_INGRESS_OPERATIONS = frozenset({"secret.ingress.put"})


class MasterjetClientError(RuntimeError):
    """Bounded, non-sensitive failure at the Masterjet client boundary."""

    def __init__(self, code: str, *, problem: ControlProblem | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.problem = problem


class _Deadline:
    def __init__(self, seconds: int) -> None:
        self._expires_at = time.monotonic() + seconds

    def remaining(self) -> float:
        remaining = self._expires_at - time.monotonic()
        if remaining <= 0:
            raise MasterjetClientError("control.timeout")
        return remaining

    def expired(self) -> bool:
        return time.monotonic() >= self._expires_at


class _DeadlineGuard:
    def __init__(self, deadline: _Deadline, abort: Callable[[], None]) -> None:
        self._timer = threading.Timer(deadline.remaining(), abort)
        self._timer.daemon = True

    def __enter__(self) -> None:
        self._timer.start()

    def __exit__(self, _kind: object, _value: object, _traceback: object) -> None:
        self._timer.cancel()


class MasterjetControlClient:
    __slots__ = (
        "_bearer_provider",
        "_connection",
        "_local_attestation_verifier",
        "_step_up_provider",
    )

    def __init__(
        self,
        connection: MasterjetConnection,
        *,
        bearer_provider: Callable[[], str] | None = None,
        step_up_provider: Callable[[], str] | None = None,
        local_attestation_verifier: (
            Callable[[int, int, int, socket.socket], bool] | None
        ) = None,
    ) -> None:
        if type(connection) is not MasterjetConnection:
            raise TypeError("connection must be a MasterjetConnection")
        self._connection = connection
        self._bearer_provider = bearer_provider
        self._step_up_provider = step_up_provider
        self._local_attestation_verifier = local_attestation_verifier

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
            response = _UnixTransport(
                self._connection,
                step_up_provider=self._step_up_provider,
                attestation_verifier=self._local_attestation_verifier,
            ).request(request, secret)
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
    def __init__(
        self,
        connection: MasterjetConnection,
        *,
        step_up_provider: Callable[[], str] | None,
        attestation_verifier: Callable[[int, int, int, socket.socket], bool] | None,
    ) -> None:
        self._connection = connection
        self._step_up_provider = step_up_provider
        self._attestation_verifier = attestation_verifier

    def request(self, request: bytes, secret: memoryview | None) -> tuple[int, bytes]:
        endpoint = _local_endpoint(self._connection)
        before = _socket_identity(endpoint)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        secret_files = []
        deadline = _Deadline(_timeout(self._connection))
        try:
            with _DeadlineGuard(deadline, lambda: _abort_socket(sock)):
                _set_socket_deadline(sock, deadline)
                sock.connect(str(endpoint))
                deadline.remaining()
                after = _socket_identity(endpoint)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise MasterjetClientError("control.endpoint_invalid")
                peer = _verify_peer(sock, expected_uid=before.st_uid)
                if secret is None:
                    _set_socket_deadline(sock, deadline)
                    sock.sendall(request + b"\n")
                else:
                    _set_socket_deadline(sock, deadline)
                    _attest_local_transport(self._attestation_verifier, peer, sock)
                    deadline.remaining()
                    step_up = _provider_value(
                        self._step_up_provider,
                        "control.step_up_required",
                        maximum=128,
                    ).encode("ascii")
                    request = _add_step_up_fd(request, len(step_up))
                    secret_files.append(_anonymous_secret(secret))
                    secret_files.append(_anonymous_secret(memoryview(step_up)))
                    descriptor = array.array("i", [item.fileno() for item in secret_files])
                    framed = request + b"\n"
                    _set_socket_deadline(sock, deadline)
                    sent = sock.sendmsg(
                        [framed],
                        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptor)],
                    )
                    if sent < len(framed):
                        _set_socket_deadline(sock, deadline)
                        sock.sendall(framed[sent:])
                return 200, _read_json_line(sock, deadline)
        except MasterjetClientError as exc:
            if deadline.expired():
                raise MasterjetClientError("control.timeout") from None
            raise exc from None
        except TimeoutError:
            raise MasterjetClientError("control.timeout") from None
        except OSError:
            if deadline.expired():
                raise MasterjetClientError("control.timeout") from None
            raise MasterjetClientError("control.transport_unavailable") from None
        finally:
            for secret_file in secret_files:
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
        bearer = _provider_value(
            self._bearer_provider,
            "control.authentication_required",
            maximum=4096,
        )
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
                self._step_up_provider,
                "control.step_up_required",
                maximum=128,
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
        deadline = _Deadline(_timeout(self._connection))
        result: tuple[int, bytes] | None = None
        client_error: MasterjetClientError | None = None
        failure_code: str | None = None
        try:
            with _DeadlineGuard(deadline, lambda: _abort_http(connection)):
                connection.request("POST", target, body=body, headers=headers)
                deadline.remaining()
                _set_http_deadline(connection, deadline)
                response = connection.getresponse()
                deadline.remaining()
                if 300 <= response.status < 400:
                    raise MasterjetClientError("control.redirect_rejected")
                content_type = response.getheader("Content-Type", "") or ""
                if content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise MasterjetClientError("control.response_invalid")
                result = (
                    response.status,
                    _read_bounded_response(
                        response,
                        deadline=deadline,
                        connection=connection,
                    ),
                )
        except MasterjetClientError as exc:
            if deadline.expired():
                failure_code = "control.timeout"
            else:
                client_error = exc
        except TimeoutError:
            failure_code = "control.timeout"
        except (OSError, http.client.HTTPException, UnicodeError, ValueError):
            if deadline.expired():
                failure_code = "control.timeout"
            else:
                failure_code = "control.transport_unavailable"
        finally:
            connection.close()
        if failure_code is not None:
            raise MasterjetClientError(failure_code)
        if client_error is not None:
            raise client_error
        if result is None:  # pragma: no cover - defensive control-flow assertion
            raise MasterjetClientError("control.transport_unavailable")
        return result


def _encode_request(
    operation: object,
    arguments: object,
    *,
    expected_generation: object,
    idempotency_key: object,
) -> tuple[bytes, memoryview | None]:
    operation = _request_token(operation)
    if expected_generation is not None and (
        type(expected_generation) is not int or not 0 <= expected_generation <= 2**63 - 1
    ):
        raise MasterjetClientError("control.request_invalid")
    if idempotency_key is not None:
        idempotency_key = _request_token(idempotency_key)

    secret = _secret_view(arguments)
    encoded_arguments: object
    if secret is None:
        if not _request_arguments_valid(arguments):
            raise MasterjetClientError("control.request_invalid")
        encoded_arguments = arguments
    else:
        if operation not in _SECRET_INGRESS_OPERATIONS:
            raise MasterjetClientError("control.request_invalid")
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
    payload = _load_json(body)
    if payload is _INVALID_JSON:
        raise MasterjetClientError("control.response_invalid")
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
    if (
        type(connection.endpoint) is not str
        or not connection.endpoint
        or "\0" in connection.endpoint
    ):
        raise MasterjetClientError("control.endpoint_invalid")
    endpoint = Path(connection.endpoint)
    if not endpoint.is_absolute() or any(part in {".", ".."} for part in endpoint.parts):
        raise MasterjetClientError("control.endpoint_invalid")
    try:
        assert_no_symlink_ancestors(endpoint, label="masterjet socket")
        parent = endpoint.parent.lstat()
    except (OSError, RuntimeError, ValueError):
        raise MasterjetClientError("control.endpoint_invalid") from None
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise MasterjetClientError("control.endpoint_invalid")
    return endpoint


def _socket_identity(endpoint: Path) -> os.stat_result:
    try:
        identity = endpoint.lstat()
    except (OSError, ValueError):
        raise MasterjetClientError("control.transport_unavailable") from None
    if (
        not stat.S_ISSOCK(identity.st_mode)
        or identity.st_uid != os.geteuid()
        or stat.S_IMODE(identity.st_mode) & 0o077
    ):
        raise MasterjetClientError("control.endpoint_invalid")
    return identity


def _verify_peer(sock: socket.socket, *, expected_uid: int) -> tuple[int, int, int]:
    if not hasattr(socket, "SO_PEERCRED"):
        raise MasterjetClientError("control.endpoint_invalid")
    try:
        credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", credentials)
    except (OSError, struct.error):
        raise MasterjetClientError("control.endpoint_invalid") from None
    if uid != expected_uid or uid != os.geteuid():
        raise MasterjetClientError("control.endpoint_invalid")
    return pid, uid, gid


def _attest_local_transport(
    verifier: Callable[[int, int, int, socket.socket], bool] | None,
    peer: tuple[int, int, int],
    sock: socket.socket,
) -> None:
    if verifier is None:
        raise MasterjetClientError("control.attestation_required")
    if not _invoke_attestation(verifier, peer, sock):
        raise MasterjetClientError("control.attestation_required")


def _invoke_attestation(
    verifier: Callable[[int, int, int, socket.socket], bool],
    peer: tuple[int, int, int],
    sock: socket.socket,
) -> bool:
    try:
        return verifier(*peer, sock) is True
    except Exception:
        return False


def _add_step_up_fd(request: bytes, size: int) -> bytes:
    document = json.loads(request)
    arguments = document["arguments"]
    arguments["step_up_fd"] = 1
    arguments["step_up_size"] = size
    encoded = json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise MasterjetClientError("control.request_too_large")
    return encoded


def _read_json_line(sock: socket.socket, deadline: _Deadline) -> bytes:
    received = bytearray()
    while True:
        _set_socket_deadline(sock, deadline)
        chunk = sock.recv(min(65_536, MAX_RESPONSE_BYTES + 1 - len(received)))
        deadline.remaining()
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


def _read_bounded_response(
    response: http.client.HTTPResponse,
    *,
    deadline: _Deadline,
    connection: http.client.HTTPSConnection,
) -> bytes:
    content_length = response.getheader("Content-Length")
    if content_length is not None:
        try:
            declared = int(content_length, 10)
        except ValueError:
            raise MasterjetClientError("control.response_invalid") from None
        if declared < 0:
            raise MasterjetClientError("control.response_invalid")
        if declared > MAX_RESPONSE_BYTES:
            raise MasterjetClientError("control.response_too_large")
    body = bytearray()
    read = getattr(response, "read1", response.read)
    while True:
        _set_http_deadline(connection, deadline)
        chunk = read(min(65_536, MAX_RESPONSE_BYTES + 1 - len(body)))
        deadline.remaining()
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise MasterjetClientError("control.response_too_large")


def _set_socket_deadline(sock: socket.socket, deadline: _Deadline) -> None:
    sock.settimeout(deadline.remaining())


def _set_http_deadline(connection: http.client.HTTPSConnection, deadline: _Deadline) -> None:
    sock = getattr(connection, "sock", None)
    if sock is not None:
        sock.settimeout(deadline.remaining())


def _abort_socket(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _abort_http(connection: http.client.HTTPSConnection) -> None:
    sock = getattr(connection, "sock", None)
    if sock is not None:
        _abort_socket(sock)


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
    if type(connection.endpoint) is not str or "\0" in connection.endpoint:
        raise MasterjetClientError("control.endpoint_invalid")
    parsed_endpoint = _parse_https_endpoint(connection.endpoint)
    if parsed_endpoint is None:
        raise MasterjetClientError("control.endpoint_invalid")
    parsed, port = parsed_endpoint
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


def _parse_https_endpoint(value: str):
    try:
        parsed = urlsplit(value)
        return parsed, parsed.port
    except ValueError:
        return None


def _provider_value(
    provider: Callable[[], str] | None,
    error: str,
    *,
    maximum: int,
) -> str:
    if provider is None:
        raise MasterjetClientError(error)
    value = _invoke_provider(provider)
    if value is None:
        raise MasterjetClientError(error)
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise MasterjetClientError(error)
    return value


def _invoke_provider(provider: Callable[[], str]) -> object | None:
    try:
        return provider()
    except Exception:
        return None


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


def _validate_json_value(
    value: object,
    *,
    depth: int,
    ancestors: set[int],
    require_mapping: bool = False,
) -> None:
    if depth > _MAX_JSON_DEPTH or (require_mapping and type(value) is not dict):
        raise ValueError
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if not -(2**63) <= value <= 2**63 - 1:
            raise ValueError
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError
        return
    if type(value) is str:
        _safe_request_string(value)
        return
    if type(value) not in {dict, list}:
        raise TypeError
    identity = id(value)
    if identity in ancestors:
        raise ValueError
    ancestors.add(identity)
    try:
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError
                normalized_key = re.sub(r"[^A-Za-z0-9]", "", key)
                if _PRIVATE_KEY_RE.search(normalized_key):
                    raise ValueError
                _safe_request_string(key)
                _validate_json_value(item, depth=depth + 1, ancestors=ancestors)
        else:
            for item in value:
                _validate_json_value(item, depth=depth + 1, ancestors=ancestors)
    finally:
        ancestors.remove(identity)


def _request_arguments_valid(arguments: object) -> bool:
    try:
        _validate_json_value(arguments, depth=0, ancestors=set(), require_mapping=True)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def _safe_request_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError from None
    if _SECRET_VALUE_RE.search(value) or _ABSOLUTE_PATH_RE.search(value):
        raise ValueError
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _load_json(body: bytes) -> object:
    try:
        return json.loads(
            body,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        return _INVALID_JSON


def _reject_json_constant(_value: str) -> object:
    raise ValueError


def _token(value: object, error: str) -> str:
    if type(value) is not str or not _TOKEN_RE.fullmatch(value):
        raise MasterjetClientError(error)
    return value


def _request_token(value: object) -> str:
    token = _token(value, "control.request_invalid")
    try:
        _safe_request_string(token)
    except ValueError:
        raise MasterjetClientError("control.request_invalid") from None
    return token


def _timeout(connection: MasterjetConnection) -> int:
    if type(connection.timeout_seconds) is not int or connection.timeout_seconds < 1:
        raise MasterjetClientError("control.endpoint_invalid")
    return connection.timeout_seconds


def _is_sensitive(operation: str) -> bool:
    return operation.startswith(_SENSITIVE_PREFIXES)
