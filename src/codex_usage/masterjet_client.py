from __future__ import annotations

import array
import atexit
import http.client
import ipaddress
import json
import multiprocessing
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
from urllib.parse import quote, urlsplit

from .config import MasterjetConnection
from .masterjet_contracts import (
    ControlContractError,
    ControlProblem,
    parse_control_operation,
    parse_control_problem,
    parse_google_accounts,
    parse_google_projects,
    parse_openai_accounts,
    parse_secret_ingress_receipt,
    parse_secret_ingress_session,
)
from .private_io import assert_no_symlink_ancestors

MAX_REQUEST_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 1_000_000
MAX_SECRET_BYTES = 10_000_000

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IDEMPOTENCY_RE = re.compile(
    r"^(?:idem-[A-Za-z0-9][A-Za-z0-9._:-]{0,122}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_INVALID_JSON = object()
_OPERATION_ARGUMENT_FIELDS = {
    "google.accounts.list": ({}, frozenset()),
    "google.projects.list": ({"account_ref": "token"}, frozenset()),
    "google.oauth.begin": (
        {"account_ref": "token", "browser": "token"},
        frozenset(),
    ),
    "google.oauth.complete": (
        {"account_ref": "token", "transaction_id": "token"},
        frozenset(),
    ),
    "google.inventory.refresh": ({"account_ref": "token"}, frozenset()),
    "google.provision.plan": ({"account_ref": "token"}, frozenset()),
    "google.provision.apply": (
        {"account_ref": "token", "plan_id": "token"},
        frozenset(),
    ),
    "google.billing.plan": (
        {
            "account_ref": "token",
            "billing_ref": "token",
            "project_refs": "token_list",
        },
        frozenset(),
    ),
    "google.billing.apply": (
        {"account_ref": "token", "plan_id": "token"},
        frozenset(),
    ),
    "openai.accounts.list": ({}, frozenset()),
    "openai.auth-sync.plan": ({"account_ref": "token"}, frozenset()),
    "openai.auth-sync.apply": (
        {"account_ref": "token", "plan_id": "token"},
        frozenset(),
    ),
    "secret.ingress.create": (
        {"account_ref": "token", "credential_type": "token", "plan_id": "token"},
        frozenset(),
    ),
}
_SENSITIVE_OPERATIONS = frozenset(
    {
        "google.oauth.begin",
        "google.oauth.complete",
        "google.billing.plan",
        "google.billing.apply",
        "openai.auth-sync.plan",
        "openai.auth-sync.apply",
        "secret.ingress.create",
        "secret.ingress.put",
    }
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
        if operation == "secret.ingress.put":
            raise MasterjetClientError("control.request_invalid")
        request, secret = _encode_request(
            operation,
            arguments,
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
        )
        return self._request(
            operation,
            arguments,
            request,
            secret,
            expected_generation,
            idempotency_key,
            secret_session_id=None,
        )

    def put_secret(
        self,
        session_id: str,
        secret: bytes | bytearray | memoryview,
        expected_generation: int | None = None,
        idempotency_key: str | None = None,
    ) -> object:
        session_id = _token(session_id, "control.request_invalid")
        request, secret_view = _encode_request(
            "secret.ingress.put",
            secret,
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            secret_session_id=session_id,
        )
        return self._request(
            "secret.ingress.put",
            {"session_id": session_id},
            request,
            secret_view,
            expected_generation,
            idempotency_key,
            secret_session_id=session_id,
        )

    def _request(
        self,
        operation: str,
        arguments: object,
        request: bytes,
        secret: memoryview | None,
        expected_generation: int | None,
        idempotency_key: str | None,
        *,
        secret_session_id: str | None,
    ) -> object:
        if self._connection.transport == "local":
            response = _UnixTransport(
                self._connection,
                step_up_provider=self._step_up_provider,
                attestation_verifier=self._local_attestation_verifier,
            ).request(operation, request, secret)
        elif self._connection.transport == "https":
            response = _HttpsTransport(
                self._connection,
                bearer_provider=self._bearer_provider,
                step_up_provider=self._step_up_provider,
            ).request(
                operation,
                request,
                secret,
                expected_generation,
                idempotency_key,
                secret_session_id,
            )
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

    def request(
        self,
        operation: str,
        request: bytes,
        secret: memoryview | None,
    ) -> tuple[int, bytes]:
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
                if not _is_sensitive(operation):
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
                    step_up_index = 1 if secret is not None else 0
                    request = _add_step_up_fd(request, len(step_up), step_up_index)
                    if secret is not None:
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
        secret_session_id: str | None,
    ) -> tuple[int, bytes]:
        deadline = _Deadline(_timeout(self._connection))
        connection: http.client.HTTPSConnection | None = None
        result: tuple[int, bytes] | None = None
        client_error: MasterjetClientError | None = None
        failure_code: str | None = None
        try:
            host, port, target = _https_endpoint(self._connection)
            deadline.remaining()
            bearer = _provider_value(
                self._bearer_provider,
                "control.authentication_required",
                maximum=4096,
            )
            deadline.remaining()
            headers = {
                "Authorization": f"Bearer {bearer}",
                "Cache-Control": "no-store",
                "Content-Type": "application/json",
            }
            body = request
            if secret is not None:
                if secret_session_id is None:
                    raise MasterjetClientError("control.request_invalid")
                body = secret.tobytes()
                target = (
                    "/admin/v1/secret-ingress-sessions/"
                    + quote(secret_session_id, safe="")
                )
                headers.update(
                    _secret_request_headers(
                        operation,
                        expected_generation,
                        idempotency_key,
                    )
                )
                headers["Content-Type"] = "application/octet-stream"
            if _is_sensitive(operation):
                headers["X-Masterjet-Step-Up"] = _provider_value(
                    self._step_up_provider,
                    "control.step_up_required",
                    maximum=128,
                )
                deadline.remaining()
            context = ssl.create_default_context()
            deadline.remaining()
            if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
                raise MasterjetClientError("control.tls_required")
            connection = _open_https_connection(host, port, context, deadline)
            with _DeadlineGuard(deadline, lambda: _abort_http(connection)):
                connection.request(
                    "PUT" if secret is not None else "POST",
                    target,
                    body=body,
                    headers=headers,
                )
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
            if connection is not None:
                _close_http(connection)
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
    secret_session_id: object = None,
) -> tuple[bytes, memoryview | None]:
    operation = _operation(operation)
    if expected_generation is not None and (
        type(expected_generation) is not int or not 0 <= expected_generation <= 2**63 - 1
    ):
        raise MasterjetClientError("control.request_invalid")
    if idempotency_key is not None:
        idempotency_key = _idempotency_key(idempotency_key)

    secret = _secret_view(arguments)
    encoded_arguments: object
    if secret is None:
        if operation in _SECRET_INGRESS_OPERATIONS or not _operation_arguments_valid(
            operation,
            arguments,
        ):
            raise MasterjetClientError("control.request_invalid")
        encoded_arguments = arguments
    else:
        if operation not in _SECRET_INGRESS_OPERATIONS or secret_session_id is None:
            raise MasterjetClientError("control.request_invalid")
        session_id = _token(secret_session_id, "control.request_invalid")
        if secret.nbytes > MAX_SECRET_BYTES:
            raise MasterjetClientError("control.request_too_large")
        encoded_arguments = {
            "session_id": session_id,
            "secret_fd": 0,
            "secret_size": secret.nbytes,
        }

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
        if operation == "secret.ingress.create":
            session = parse_secret_ingress_session(payload)
            if (
                type(arguments) is not dict
                or session.account_ref != arguments.get("account_ref")
                or session.plan_id != arguments.get("plan_id")
            ):
                raise MasterjetClientError("control.response_invalid")
            return session
        if operation == "secret.ingress.put":
            receipt = parse_secret_ingress_receipt(payload)
            if type(arguments) is not dict or receipt.session_id != arguments.get(
                "session_id"
            ):
                raise MasterjetClientError("control.response_invalid")
            return receipt
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


def _add_step_up_fd(request: bytes, size: int, index: int) -> bytes:
    document = json.loads(request)
    arguments = document["arguments"]
    arguments["step_up_fd"] = index
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


def _close_http(connection: http.client.HTTPSConnection) -> None:
    try:
        connection.close()
    except (OSError, http.client.HTTPException, ValueError):
        pass


def _resolve_worker(host: str, port: int, sender: object) -> None:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        normalized = [
            (int(family), int(kind), int(protocol), str(canonical_name), tuple(address))
            for family, kind, protocol, canonical_name, address in addresses[:32]
        ]
        message = (True, normalized)
    except BaseException:
        message = (False, ())
    try:
        sender.send(message)
    except BaseException:
        pass
    finally:
        try:
            sender.close()
        except BaseException:
            pass


_RESOLVER_WORKER = _resolve_worker


class _ResolverProcessReaper:
    """Bound resolver workers and retain them until a successful later join."""

    def __init__(self, maximum: int = 32) -> None:
        self._maximum = maximum
        self._reset_after_fork()

    def _reset_after_fork(self) -> None:
        self._owner_pid = os.getpid()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._pending: dict[int, object] = {}
        self._reserved = 0
        self._thread: threading.Thread | None = None

    def reserve(self) -> bool:
        if self._owner_pid != os.getpid():
            self._reset_after_fork()
        with self._lock:
            if self._reserved >= self._maximum:
                return False
            if self._thread is None:
                thread = threading.Thread(
                    target=self._run,
                    name="masterjet-resolver-reaper",
                    daemon=True,
                )
                try:
                    thread.start()
                except BaseException:
                    return False
                self._thread = thread
            self._reserved += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._reserved:
                self._reserved -= 1

    def defer(self, process: object) -> None:
        with self._lock:
            self._pending[id(process)] = process
            self._wake.set()

    def _run(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            while True:
                with self._lock:
                    pending = tuple(self._pending.items())
                if not pending:
                    break
                for key, process in pending:
                    if not self._reap_once(process):
                        continue
                    with self._lock:
                        if self._pending.get(key) is process:
                            del self._pending[key]
                            self._reserved -= 1
                with self._lock:
                    if not self._pending:
                        break
                self._wake.wait(0.05)
                self._wake.clear()

    @staticmethod
    def _reap_once(process: object) -> bool:
        _try_process_call(process, "kill")
        joined = _try_process_call(process, "join", timeout=0.05)
        alive, checked = _process_alive(process)
        if not joined or not checked or alive:
            return False
        _try_process_call(process, "close")
        return True

    def shutdown(self) -> None:
        try:
            with self._lock:
                pending = tuple(self._pending.values())
        except BaseException:
            return
        for process in pending:
            self._reap_once(process)


_RESOLVER_REAPER = _ResolverProcessReaper()
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_RESOLVER_REAPER._reset_after_fork)
atexit.register(_RESOLVER_REAPER.shutdown)


def _resolve_host(host: str, port: int, deadline: _Deadline) -> list[tuple[object, ...]]:
    if not _RESOLVER_REAPER.reserve():
        raise MasterjetClientError("control.transport_unavailable")
    receiver = None
    sender = None
    process = None
    result: list[tuple[object, ...]] | None = None
    failure: str | None = None
    try:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_RESOLVER_WORKER,
            args=(host, port, sender),
            daemon=True,
        )
        process.start()
        sender.close()
        if not receiver.poll(deadline.remaining()):
            failure = "control.timeout"
        else:
            succeeded, addresses = receiver.recv()
            deadline.remaining()
            if succeeded is True and _resolved_addresses_valid(addresses, port):
                result = addresses
            else:
                failure = "control.transport_unavailable"
    except BaseException:
        failure = _resolver_failure_code(deadline)
    finally:
        cleanup_succeeded = _cleanup_resolver(process, receiver, sender)
    if not cleanup_succeeded and failure != "control.timeout":
        failure = "control.transport_unavailable"
    if failure is not None or result is None:
        raise MasterjetClientError(failure or "control.transport_unavailable")
    return result


def _resolver_failure_code(deadline: _Deadline) -> str:
    try:
        if deadline.expired():
            return "control.timeout"
    except BaseException:
        pass
    return "control.transport_unavailable"


def _cleanup_resolver(process: object, receiver: object, sender: object) -> bool:
    succeeded = True
    for pipe in (receiver, sender):
        if pipe is None:
            continue
        try:
            pipe.close()
        except BaseException:
            succeeded = False
    if process is None:
        _RESOLVER_REAPER.release()
    else:
        stopped, needs_reap = _stop_process(process)
        succeeded = stopped and succeeded
        if needs_reap:
            _RESOLVER_REAPER.defer(process)
        else:
            _RESOLVER_REAPER.release()
    return succeeded


def _stop_process(process: object) -> tuple[bool, bool]:
    succeeded = True
    try:
        pid = process.pid
    except BaseException:
        pid = 1
        succeeded = False
    if pid is not None:
        joined = _try_process_call(process, "join", timeout=0)
        succeeded = joined and succeeded
        alive, checked = _process_alive(process)
        succeeded = checked and succeeded
        if alive:
            succeeded = _try_process_call(process, "terminate") and succeeded
            joined = _try_process_call(process, "join", timeout=0.1)
            succeeded = joined and succeeded
            alive, checked = _process_alive(process)
            succeeded = checked and succeeded
        if alive:
            succeeded = _try_process_call(process, "kill") and succeeded
            joined = _try_process_call(process, "join", timeout=0.1)
            succeeded = joined and succeeded
            alive, checked = _process_alive(process)
            succeeded = checked and succeeded
        if alive or not checked or not joined:
            return False, True
    succeeded = _try_process_call(process, "close") and succeeded
    return succeeded, False


def _try_process_call(process: object, method: str, **kwargs: object) -> bool:
    try:
        getattr(process, method)(**kwargs)
        return True
    except BaseException:
        return False


def _process_alive(process: object) -> tuple[bool, bool]:
    try:
        return process.is_alive() is True, True
    except BaseException:
        return True, False


def _resolved_addresses_valid(value: object, requested_port: int) -> bool:
    if type(value) is not list or not 1 <= len(value) <= 32:
        return False
    for item in value:
        if type(item) is not tuple or len(item) != 5:
            return False
        family, kind, protocol, canonical_name, address = item
        if (
            type(family) is not int
            or type(kind) is not int
            or type(protocol) is not int
            or type(canonical_name) is not str
            or type(address) is not tuple
            or family not in {socket.AF_INET, socket.AF_INET6}
            or kind != socket.SOCK_STREAM
            or protocol not in {0, socket.IPPROTO_TCP}
            or not _sockaddr_valid(family, address, requested_port)
        ):
            return False
    return True


def _sockaddr_valid(family: int, address: tuple[object, ...], requested_port: int) -> bool:
    if family == socket.AF_INET:
        if len(address) != 2:
            return False
    elif len(address) != 4:
        return False
    host, port, *ipv6_tail = address
    if type(host) is not str or type(port) is not int or port != requested_port:
        return False
    try:
        numeric = ipaddress.ip_address(host)
    except ValueError:
        return False
    if family == socket.AF_INET:
        return numeric.version == 4
    flowinfo, scope_id = ipv6_tail
    return (
        numeric.version == 6
        and "%" not in host
        and type(flowinfo) is int
        and 0 <= flowinfo <= 0xFFFFF
        and type(scope_id) is int
        and 0 <= scope_id <= 0xFFFFFFFF
    )


def _open_https_connection(
    host: str,
    port: int | None,
    context: ssl.SSLContext,
    deadline: _Deadline,
) -> http.client.HTTPSConnection:
    constructor_failure: str | None = None
    try:
        connection = http.client.HTTPSConnection(
            host,
            port,
            timeout=deadline.remaining(),
            context=context,
        )
    except (http.client.InvalidURL, UnicodeError, ValueError):
        constructor_failure = "control.endpoint_invalid"
    except (OSError, http.client.HTTPException):
        constructor_failure = "control.transport_unavailable"
    if constructor_failure is not None:
        raise MasterjetClientError(constructor_failure)

    try:
        resolved_port = 443 if port is None else port
        addresses = _resolve_host(host, resolved_port, deadline)
        for family, kind, protocol, _canonical_name, address in addresses:
            raw_socket: socket.socket | None = None
            tls_socket: socket.socket | None = None
            try:
                raw_socket = socket.socket(family, kind, protocol)
                raw_socket.settimeout(deadline.remaining())
                raw_socket.connect(address)
                deadline.remaining()
                tls_socket = context.wrap_socket(raw_socket, server_hostname=host)
                raw_socket = None
                tls_socket.settimeout(deadline.remaining())
                connection.sock = tls_socket
                tls_socket = None
                return connection
            except TimeoutError:
                if deadline.expired():
                    raise MasterjetClientError("control.timeout") from None
            except (OSError, UnicodeError, ValueError):
                if deadline.expired():
                    raise MasterjetClientError("control.timeout") from None
            finally:
                if tls_socket is not None:
                    _abort_socket(tls_socket)
                if raw_socket is not None:
                    _abort_socket(raw_socket)
        raise MasterjetClientError("control.transport_unavailable")
    except MasterjetClientError:
        _close_http(connection)
        raise


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
    if type(connection.endpoint) is not str or any(
        not 0x21 <= ord(character) <= 0x7E for character in connection.endpoint
    ):
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
    host = parsed.hostname
    target = parsed.path or "/"
    if (
        not _host_valid(host)
        or (port is not None and not 1 <= port <= 65535)
        or any(ord(character) <= 0x20 for character in target)
    ):
        raise MasterjetClientError("control.endpoint_invalid")
    return host, port, target


def _parse_https_endpoint(value: str):
    try:
        parsed = urlsplit(value)
        return parsed, parsed.port
    except ValueError:
        return None


def _host_valid(host: str) -> bool:
    if "%" in host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return (
        _HOST_RE.fullmatch(host) is not None
        and all(
            label and len(label) <= 63 and not label.startswith("-") and not label.endswith("-")
            for label in host.split(".")
        )
    )


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
    if type(value) not in {bytes, bytearray, memoryview}:
        return None
    view = memoryview(value)
    if view.ndim != 1 or not view.c_contiguous:
        raise MasterjetClientError("control.request_invalid")
    return view.cast("B")


def _operation_arguments_valid(operation: str, arguments: object) -> bool:
    contract = _OPERATION_ARGUMENT_FIELDS.get(operation)
    if contract is None or type(arguments) is not dict:
        return False
    fields, optional = contract
    if set(arguments) - fields.keys() or fields.keys() - set(arguments) - optional:
        return False
    try:
        return all(_argument_value_valid(arguments[name], kind) for name, kind in fields.items())
    except (TypeError, ValueError, RecursionError):
        return False


def _argument_value_valid(value: object, kind: str) -> bool:
    if kind == "token":
        return type(value) is str and _TOKEN_RE.fullmatch(value) is not None
    if kind == "token_list":
        return (
            type(value) is list
            and 1 <= len(value) <= 256
            and all(
                type(item) is str and _TOKEN_RE.fullmatch(item) is not None
                for item in value
            )
            and len(set(value)) == len(value)
        )
    return False


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


def _operation(value: object) -> str:
    operation = _token(value, "control.request_invalid")
    if operation not in _OPERATION_ARGUMENT_FIELDS and operation not in _SECRET_INGRESS_OPERATIONS:
        raise MasterjetClientError("control.request_invalid")
    return operation


def _idempotency_key(value: object) -> str:
    if type(value) is not str or _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise MasterjetClientError("control.request_invalid")
    return value


def _timeout(connection: MasterjetConnection) -> int:
    if type(connection.timeout_seconds) is not int or connection.timeout_seconds < 1:
        raise MasterjetClientError("control.endpoint_invalid")
    return connection.timeout_seconds


def _is_sensitive(operation: str) -> bool:
    return operation in _SENSITIVE_OPERATIONS
