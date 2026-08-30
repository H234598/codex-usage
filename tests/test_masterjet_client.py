from __future__ import annotations

import array
import http.client
import json
import multiprocessing
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
from masterjet_resolver_workers import (
    blocking_resolver_worker,
    hostname_resolver_worker,
    malformed_resolver_worker,
    wrong_family_resolver_worker,
    wrong_port_resolver_worker,
    wrong_protocol_resolver_worker,
    wrong_shape_resolver_worker,
)

import codex_usage.masterjet_client as client_module
import codex_usage.masterjet_contracts as contracts_module
import codex_usage.masterjet_credentials as credentials_module
from codex_usage.config import MasterjetConnection
from codex_usage.google_accounts import GoogleAccountsController
from codex_usage.masterjet_auth_sync import sync_account_auth
from codex_usage.masterjet_client import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_SECRET_BYTES,
    MasterjetClientError,
    MasterjetControlClient,
)
from codex_usage.masterjet_credentials import (
    bearer_provider_from_fd,
    bearer_provider_from_systemd_credentials,
)
from codex_usage.models import Account

REDIRECT_URI = "http://127.0.0.1:8765/oauth/callback"
AUTHORIZATION_URL = (
    "https://accounts.google.com/o/oauth2/v2/auth?"
    "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Foauth%2Fcallback"
    "&state=state-one"
)


def google_accounts_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "registry_generation": 9,
        "accounts": [
            {
                "ref": "google-1",
                "label": "Google account 01_BW",
                "enabled": True,
                "subject_bound": True,
                "oauth_state": "ready",
                "inventory_generation": 4,
                "quota_state": "available",
                "project_count": 12,
                "billing_count": 1,
                "reload_state": "idle",
                "default_oauth_client_ref": "oauth-client-1",
                "oauth_client_availability": "available",
            }
        ],
    }


def step_up_problem_payload() -> dict[str, object]:
    severity, title, detail, effect, action = contracts_module._problem_template(
        "control.step_up_required"
    )
    return {
        "schema_version": 1,
        "code": "control.step_up_required",
        "severity": severity,
        "title": title,
        "detail": detail,
        "effect": effect,
        "action": action,
        "retryable": False,
        "retry_after_seconds": None,
        "correlation_id": "corr-1",
        "occurred_at": "2026-08-28T12:00:00Z",
    }


def ingress_receipt_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "session_id": "ingress-1",
        "account_ref": "openai-1",
        "state": "consumed",
        "generation": 5,
    }


def ingress_session_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": "ingress-1",
        "account_ref": "openai-1",
        "state": "pending",
        "plan_digest": "sha256:" + "a" * 64,
        "expected_generation": 4,
        "expires_at": 1_787_919_300.0,
        "session_generation": 4,
    }


def google_oauth_transaction_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": "oauth-1",
        "account_ref": "google-1",
        "authorization_url": AUTHORIZATION_URL,
        "expires_at": 1_777_463_500.0,
        "inventory_generation": 4,
    }


def unix_success(payload: dict[str, object]) -> bytes:
    result = dict(payload)
    assert result.pop("schema_version") == 1
    return json.dumps({"schema_version": 1, "ok": True, "result": result}).encode()


def unix_problem(payload: dict[str, object]) -> bytes:
    return json.dumps({"schema_version": 1, "ok": False, "problem": payload}).encode()


class FakeHTTPResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._offset = 0
        self._headers = {"content-type": content_type, **(headers or {})}

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name.lower(), default)

    def read(self, amount: int | None = None) -> bytes:
        if amount is None:
            amount = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk


class FakeHTTPSConnection:
    response = FakeHTTPResponse(b"{}")
    error: Exception | None = None
    instances: ClassVar[list[FakeHTTPSConnection]] = []

    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []
        type(self).instances.append(self)

    def request(
        self,
        method: str,
        url: str,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        self.requests.append((method, url, body, headers))
        if type(self).error is not None:
            raise type(self).error

    def getresponse(self) -> FakeHTTPResponse:
        return type(self).response

    def close(self) -> None:
        pass


_REAL_OPEN_HTTPS_CONNECTION = client_module._open_https_connection


def open_fake_https_connection(host, port, context, deadline):
    return FakeHTTPSConnection(
        host,
        port,
        timeout=round(deadline.remaining()),
        context=context,
    )


@pytest.fixture(autouse=True)
def reset_fake_https(monkeypatch) -> None:
    FakeHTTPSConnection.instances = []
    FakeHTTPSConnection.error = None
    FakeHTTPSConnection.response = FakeHTTPResponse(b"{}")
    monkeypatch.setattr(client_module, "_open_https_connection", open_fake_https_connection)


@contextmanager
def unix_server(
    socket_path: Path,
    response: bytes,
    *,
    append_newline: bool = True,
    response_delay: float = 0,
):
    capture: dict[str, object] = {}
    ready = threading.Event()
    finished = threading.Event()

    def serve() -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(socket_path))
            socket_path.chmod(0o600)
            server.listen(1)
            server.settimeout(1)
            ready.set()
            try:
                connection, _ = server.accept()
            except TimeoutError:
                capture["accepted"] = False
                return
            capture["accepted"] = True
            with connection:
                chunks: list[bytes] = []
                received_fds: list[int] = []
                while not chunks or b"\n" not in b"".join(chunks):
                    chunk, ancillary, _flags, _address = connection.recvmsg(
                        65_536,
                        socket.CMSG_SPACE(16 * array.array("i").itemsize),
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    for level, kind, data in ancillary:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            fds = array.array("i")
                            fds.frombytes(data[: len(data) - (len(data) % fds.itemsize)])
                            received_fds.extend(fds)
                capture["request"] = b"".join(chunks)
                secrets = []
                for fd in received_fds:
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        secrets.append(os.read(fd, MAX_SECRET_BYTES + 1))
                    finally:
                        os.close(fd)
                capture["secrets"] = secrets
                try:
                    framed_response = response + (b"\n" if append_newline else b"")
                    if response_delay:
                        for byte in framed_response:
                            connection.sendall(bytes([byte]))
                            time.sleep(response_delay)
                    else:
                        connection.sendall(framed_response)
                except BrokenPipeError:
                    pass
        finally:
            server.close()
            finished.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(2)
    try:
        yield capture
    finally:
        assert finished.wait(2)
        thread.join(timeout=0)


def local_client(socket_path: Path, **kwargs: object) -> MasterjetControlClient:
    kwargs.setdefault("local_attestation_verifier", lambda *_args: True)
    return MasterjetControlClient(
        MasterjetConnection(transport="local", endpoint=str(socket_path), timeout_seconds=2),
        **kwargs,
    )


def https_client(**kwargs: object) -> MasterjetControlClient:
    return MasterjetControlClient(
        MasterjetConnection(
            transport="https",
            endpoint="https://masterjet.example.test:8443/control",
            timeout_seconds=7,
        ),
        **kwargs,
    )


def _task9_transport_and_auth_selfcheck(tmp_path, monkeypatch):
    encoded_google = unix_success(google_accounts_payload())
    socket_path = tmp_path / "masterjet.sock"
    with unix_server(socket_path, encoded_google):
        local_google = local_client(socket_path).call("google.accounts.list", {})

    operation_base = {
        "schema_version": 1,
        "expected_generation": 4,
        "plan_digest": "sha256:" + "a" * 64,
        "created_at": "2026-08-28T12:00:00Z",
        "expires_at": "2026-08-28T12:30:00Z",
        "failed_count": 0,
        "reason_codes": [],
    }
    responses = [
        google_accounts_payload(),
        {
            "schema_version": 1,
            "accounts": [
                {
                    "ref": "openai-remote",
                    "label": "OpenAI synthetic",
                    "enabled": True,
                    "local_profile_ref": "profile-1",
                    "source_host_ref": "host-1",
                    "auth_state": "ready",
                    "access_expires_at": None,
                    "credential_generation": 4,
                    "vault_projection_state": "current",
                    "usage_state": "fresh",
                }
            ],
        },
        operation_base
        | {
            "id": "plan-1",
            "kind": "openai.auth.plan",
            "state": "planned",
            "resulting_generation": None,
            "completed_count": 0,
            "not_attempted_count": 1,
        },
        ingress_session_payload()
        | {
            "account_ref": "openai-remote",
            "plan_digest": operation_base["plan_digest"],
        },
        {
            "schema_version": 1,
            "session_id": "ingress-1",
            "account_ref": "openai-remote",
            "state": "consumed",
            "generation": 5,
        },
        operation_base
        | {
            "id": "apply-1",
            "kind": "openai.auth.apply",
            "state": "succeeded",
            "resulting_generation": 5,
            "completed_count": 1,
            "not_attempted_count": 0,
        },
    ]

    original_default_context = ssl.create_default_context
    with real_tls_server(tmp_path, responses=responses) as (port, certificate, capture):
        monkeypatch.setattr(client_module, "_open_https_connection", _REAL_OPEN_HTTPS_CONNECTION)
        monkeypatch.setattr(
            client_module.ssl,
            "create_default_context",
            lambda: original_default_context(cafile=certificate),
        )
        remote = MasterjetControlClient(
            MasterjetConnection(
                transport="https",
                endpoint=f"https://localhost:{port}/control",
                timeout_seconds=5,
            ),
            bearer_provider=lambda: "task9-system-credential",
            step_up_provider=lambda: "123456",
        )
        https_google = remote.call("google.accounts.list", {})

        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
        profile = tmp_path / "profile"
        codex_home = profile / "codex-home"
        codex_home.mkdir(parents=True, mode=0o700)
        codex_home.chmod(0o700)
        auth_path = codex_home / "auth.json"
        secret = b'{"tokens":"task9-synthetic-auth"}'
        auth_path.write_bytes(secret)
        auth_path.chmod(0o600)
        account = Account(
            id="profile-1",
            label="OpenAI synthetic",
            profile_dir=str(profile),
            auth_json_path=str(auth_path),
        )
        synced = sync_account_auth(
            account,
            remote,
            clock=lambda: datetime(2026, 8, 28, 12, 5, tzinfo=UTC),
        )

    requests = capture["requests"]
    assert isinstance(requests, list)
    json_requests = b"".join(
        request["body"]
        for request in requests
        if request["headers"].get("Content-Type") == "application/json"
    )
    header_bytes = repr([request["headers"] for request in requests]).encode()
    ingress_headers = next(
        request["headers"] for request in requests if request["method"] == "PUT"
    )
    return {
        "local_google": local_google,
        "https_google": https_google,
        "google_generation": https_google[0].inventory_generation,
        "auth_sync": (synced.account_ref, synced.generation, synced.status),
        "secret_ingress_content_type": ingress_headers["Content-Type"],
        "secret_bytes": secret,
        "json_requests": json_requests,
        "headers": header_bytes,
        "environment": repr(dict(os.environ)).encode(),
        "https_server_request_lines": [request["request_line"] for request in requests],
    }


def test_real_https_step_up_challenge_retries_exactly_once(tmp_path, monkeypatch):
    calls = 0

    def step_up() -> str:
        nonlocal calls
        calls += 1
        return "123456"

    original_default_context = ssl.create_default_context
    with real_tls_server(
        tmp_path, responses=[step_up_problem_payload(), google_accounts_payload()]
    ) as (port, certificate, capture):
        monkeypatch.setattr(client_module, "_open_https_connection", _REAL_OPEN_HTTPS_CONNECTION)
        monkeypatch.setattr(
            client_module.ssl,
            "create_default_context",
            lambda: original_default_context(cafile=certificate),
        )
        result = MasterjetControlClient(
            MasterjetConnection(
                transport="https", endpoint=f"https://localhost:{port}/control", timeout_seconds=5
            ),
            bearer_provider=lambda: "remote-bearer",
            step_up_provider=step_up,
        ).call("google.accounts.list", {})

    assert len(result) == 1
    assert calls == 1
    requests = capture["requests"]
    assert len(requests) == 2
    assert "X-Masterjet-Step-Up" not in requests[0]["headers"]
    assert requests[1]["headers"]["X-Masterjet-Step-Up"] == "123456"


def test_real_https_second_step_up_challenge_stops_after_one_retry(tmp_path, monkeypatch):
    calls = 0

    def step_up() -> str:
        nonlocal calls
        calls += 1
        return "123456"

    original_default_context = ssl.create_default_context
    with real_tls_server(
        tmp_path, responses=[step_up_problem_payload(), step_up_problem_payload()]
    ) as (port, certificate, capture):
        monkeypatch.setattr(client_module, "_open_https_connection", _REAL_OPEN_HTTPS_CONNECTION)
        monkeypatch.setattr(
            client_module.ssl,
            "create_default_context",
            lambda: original_default_context(cafile=certificate),
        )
        client = MasterjetControlClient(
            MasterjetConnection(
                transport="https", endpoint=f"https://localhost:{port}/control", timeout_seconds=5
            ),
            bearer_provider=lambda: "remote-bearer",
            step_up_provider=step_up,
        )
        with pytest.raises(MasterjetClientError, match=r"control\.step_up_required"):
            client.call("google.accounts.list", {})

    assert calls == 1
    assert len(capture["requests"]) == 2


def test_https_client_caches_one_shot_bearer_across_multiple_requests(monkeypatch):
    responses = iter(
        [
            FakeHTTPResponse(json.dumps(google_accounts_payload()).encode()),
            FakeHTTPResponse(json.dumps(google_accounts_payload()).encode()),
        ]
    )
    calls = 0

    class SequencedConnection(FakeHTTPSConnection):
        def getresponse(self):
            return next(responses)

    def bearer() -> str:
        nonlocal calls
        calls += 1
        return "remote-bearer"

    monkeypatch.setattr(
        client_module,
        "_open_https_connection",
        lambda *args: SequencedConnection(args[0]),
    )
    client = https_client(bearer_provider=bearer)
    client.call("google.accounts.list", {})
    client.call("google.accounts.list", {})

    assert calls == 1


def test_google_account_details_over_real_https_reads_system_credential_once(
    tmp_path, monkeypatch
):
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir(mode=0o700)
    credential = credential_directory / "masterjet-control-bearer"
    credential.write_bytes(b"google-system-bearer")
    credential.chmod(0o400)
    account_payload = google_accounts_payload()
    account_payload["accounts"][0]["project_count"] = 1
    projects_payload = {
        "schema_version": 1,
        "account_ref": "google-1",
        "inventory_generation": 4,
        "projects": [
            {
                "ref": "project-1",
                "project_name": "Amber Meadow",
                "purpose": "quota_probe",
                "key_name": "Quiet River",
                "billing_ref": None,
                "status": "ready",
                "probe_state": "ready",
                "quota_state": "available",
            }
        ],
    }
    reads = 0
    real_read = credentials_module._read_bearer_from_directory

    def count_read(directory):
        nonlocal reads
        reads += 1
        return real_read(directory)

    monkeypatch.setattr(credentials_module, "_read_bearer_from_directory", count_read)
    provider = bearer_provider_from_systemd_credentials(
        environ={"CREDENTIALS_DIRECTORY": str(credential_directory)}
    )
    original_default_context = ssl.create_default_context
    with real_tls_server(
        tmp_path, responses=[account_payload, projects_payload]
    ) as (port, certificate, capture):
        monkeypatch.setattr(client_module, "_open_https_connection", _REAL_OPEN_HTTPS_CONNECTION)
        monkeypatch.setattr(
            client_module.ssl,
            "create_default_context",
            lambda: original_default_context(cafile=certificate),
        )
        client = MasterjetControlClient(
            MasterjetConnection(
                transport="https",
                endpoint=f"https://localhost:{port}/control",
                timeout_seconds=5,
            ),
            bearer_provider=provider,
        )

        details = GoogleAccountsController(client).account_details()

    assert reads == 1
    assert details[0].account.ref == "google-1"
    assert [project.ref for project in details[0].projects] == ["project-1"]
    assert [request["request_line"] for request in capture["requests"]] == [
        "GET /admin/v1/google/accounts HTTP/1.1",
        "GET /admin/v1/google/accounts/google-1 HTTP/1.1",
    ]
    assert all(
        request["headers"]["Authorization"] == "Bearer google-system-bearer"
        for request in capture["requests"]
    )


def test_complete_openai_auth_sync_over_real_https_reads_fd_once_and_raw_secret_only_in_put(
    tmp_path, monkeypatch
):
    operation_base = {
        "schema_version": 1,
        "expected_generation": 4,
        "plan_digest": "sha256:" + "a" * 64,
        "created_at": "2026-08-28T12:00:00Z",
        "expires_at": "2026-08-28T12:30:00Z",
        "failed_count": 0,
        "reason_codes": [],
    }
    responses = [
        {
            "schema_version": 1,
            "accounts": [
                {
                    "ref": "openai-remote",
                    "label": "OpenAI synthetic",
                    "enabled": True,
                    "local_profile_ref": "profile-1",
                    "source_host_ref": "host-1",
                    "auth_state": "ready",
                    "access_expires_at": None,
                    "credential_generation": 4,
                    "vault_projection_state": "current",
                    "usage_state": "fresh",
                }
            ],
        },
        operation_base
        | {
            "id": "plan-1",
            "kind": "openai.auth.plan",
            "state": "planned",
            "resulting_generation": None,
            "completed_count": 0,
            "not_attempted_count": 1,
        },
        ingress_session_payload()
        | {
            "account_ref": "openai-remote",
            "plan_digest": operation_base["plan_digest"],
        },
        {
            "schema_version": 1,
            "session_id": "ingress-1",
            "account_ref": "openai-remote",
            "state": "consumed",
            "generation": 5,
        },
        operation_base
        | {
            "id": "apply-1",
            "kind": "openai.auth.apply",
            "state": "succeeded",
            "resulting_generation": 5,
            "completed_count": 1,
            "not_attempted_count": 0,
        },
    ]
    credential = tmp_path / "fd-credential"
    credential.write_bytes(b"openai-fd-bearer")
    credential.chmod(0o400)
    original_fd = os.open(credential, os.O_RDONLY)
    provider = bearer_provider_from_fd(original_fd)
    os.close(original_fd)
    reads = 0
    real_read = credentials_module._read_bearer_from_fd

    def count_read(fd):
        nonlocal reads
        reads += 1
        return real_read(fd)

    monkeypatch.setattr(credentials_module, "_read_bearer_from_fd", count_read)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    profile = tmp_path / "profile"
    auth_directory = profile / "codex-home"
    auth_directory.mkdir(parents=True, mode=0o700)
    raw_secret = b'{"tokens":"raw-openai-auth-marker"}'
    auth_path = auth_directory / "auth.json"
    auth_path.write_bytes(raw_secret)
    auth_path.chmod(0o600)
    account = Account(
        id="profile-1",
        label="OpenAI synthetic",
        profile_dir=str(profile),
        auth_json_path=str(auth_path),
    )
    original_default_context = ssl.create_default_context
    with real_tls_server(tmp_path, responses=responses) as (port, certificate, capture):
        monkeypatch.setattr(client_module, "_open_https_connection", _REAL_OPEN_HTTPS_CONNECTION)
        monkeypatch.setattr(
            client_module.ssl,
            "create_default_context",
            lambda: original_default_context(cafile=certificate),
        )
        client = MasterjetControlClient(
            MasterjetConnection(
                transport="https",
                endpoint=f"https://localhost:{port}/control",
                timeout_seconds=5,
            ),
            bearer_provider=provider,
        )

        result = sync_account_auth(
            account,
            client,
            clock=lambda: datetime(2026, 8, 28, 12, 5, tzinfo=UTC),
        )

    requests = capture["requests"]
    assert reads == 1
    assert (result.account_ref, result.generation, result.status) == (
        "openai-remote",
        5,
        "succeeded",
    )
    assert [request["method"] for request in requests] == ["GET", "POST", "POST", "PUT", "POST"]
    assert all(
        request["headers"]["Authorization"] == "Bearer openai-fd-bearer"
        for request in requests
    )
    assert requests[3]["body"] == raw_secret
    assert all(raw_secret not in request["body"] for request in requests[:3] + requests[4:])
    assert raw_secret not in repr([request["headers"] for request in requests]).encode()


class ExplodingConnectSocket:
    def settimeout(self, _timeout):
        pass

    def connect(self, _address):
        raise AssertionError("resolver response reached parent connect")

    def shutdown(self, _how):
        pass

    def close(self):
        pass


class ResolverPipe:
    def __init__(self, events, *, poll_error=None, close_error=None):
        self.events = events
        self.poll_error = poll_error
        self.close_error = close_error

    def poll(self, _timeout):
        self.events.append("poll")
        if self.poll_error is not None:
            raise self.poll_error
        return False

    def close(self):
        self.events.append("pipe.close")
        if self.close_error is not None:
            raise self.close_error


class ResolverProcess:
    pid = 42

    def __init__(self, events, *, failing_method=None):
        self.events = events
        self.alive = True
        self.failing_method = failing_method

    def start(self):
        self.events.append("start")

    def join(self, timeout):
        self.events.append(("join", timeout))
        if self.failing_method == "join":
            raise ValueError("private-resolver-state")

    def is_alive(self):
        self.events.append("is_alive")
        return self.alive

    def terminate(self):
        self.events.append("terminate")
        if self.failing_method == "terminate":
            raise ValueError("private-resolver-state")

    def kill(self):
        self.events.append("kill")
        self.alive = False
        if self.failing_method == "kill":
            raise ValueError("private-resolver-state")

    def close(self):
        self.events.append("process.close")
        if self.failing_method == "close":
            raise ValueError("private-resolver-state")


class DelayedExitResolverProcess(ResolverProcess):
    def __init__(self, events) -> None:
        super().__init__(events)
        self.allow_exit = threading.Event()
        self.reaped = threading.Event()

    def join(self, timeout):
        self.events.append(("join", timeout))
        if self.allow_exit.is_set():
            self.alive = False
            self.reaped.set()

    def kill(self):
        self.events.append("kill")

    def close(self):
        self.events.append("process.close")
        if self.alive:
            raise ValueError("process still running")


class DelayedRealProcess:
    def __init__(self, process) -> None:
        self.process = process
        self.reaped = threading.Event()

    @property
    def pid(self):
        return self.process.pid

    def start(self):
        self.process.start()

    def join(self, timeout):
        self.process.join(timeout)
        if not self.process.is_alive():
            self.reaped.set()

    def is_alive(self):
        return self.process.is_alive()

    def terminate(self):
        pass

    def kill(self):
        pass

    def close(self):
        self.process.close()


class ResolverContext:
    def __init__(self, receiver, sender, process):
        self.receiver = receiver
        self.sender = sender
        self.process = process

    def Pipe(self, *, duplex):
        assert duplex is False
        return self.receiver, self.sender

    def Process(self, **_kwargs):
        return self.process


class ResolverWorkerSender:
    def __init__(self):
        self.messages = []
        self.closed = False

    def send(self, message):
        self.messages.append(message)

    def close(self):
        self.closed = True


class OwnedRawSocket:
    def __init__(self):
        self.close_calls = 0

    def settimeout(self, _timeout):
        pass

    def connect(self, _address):
        pass

    def shutdown(self, _how):
        pass

    def close(self):
        self.close_calls += 1


class OwnedTLSSocket:
    def __init__(self, *, fail_timeout: bool):
        self.fail_timeout = fail_timeout
        self.close_calls = 0

    def settimeout(self, _timeout):
        if self.fail_timeout:
            raise ValueError("private-tls-state")

    def shutdown(self, _how):
        pass

    def close(self):
        self.close_calls += 1


class OwnershipTLSContext:
    check_hostname = True
    verify_mode = ssl.CERT_REQUIRED

    def __init__(self, tls_socket):
        self.tls_socket = tls_socket

    def wrap_socket(self, _raw_socket, *, server_hostname):
        assert server_hostname == "masterjet.example.test"
        return self.tls_socket


class FailingSocketAssignmentConnection:
    def __init__(self, *_args, **_kwargs):
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None:
            raise ValueError("private-connection-state")
        self._sock = value

    def close(self):
        pass


def test_local_and_https_decode_same_projection(tmp_path, monkeypatch):
    encoded_response = json.dumps(google_accounts_payload()).encode()
    socket_path = tmp_path / "masterjet.sock"
    FakeHTTPSConnection.response = FakeHTTPResponse(encoded_response)
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with unix_server(socket_path, unix_success(google_accounts_payload())):
        local_result = local_client(socket_path).call("google.accounts.list", {})
    https_result = https_client(bearer_provider=lambda: "remote-bearer").call(
        "google.accounts.list", {}
    )

    assert local_result == https_result
    assert local_result[0].ref == "google-1"


@pytest.mark.parametrize("transport", ["local", "https"])
def test_response_larger_than_limit_is_rejected(tmp_path, monkeypatch, transport):
    oversized = b" " * (MAX_RESPONSE_BYTES + 1)
    socket_path = tmp_path / "masterjet.sock"
    if transport == "local":
        with unix_server(socket_path, oversized):
            client = local_client(socket_path)
            with pytest.raises(MasterjetClientError, match=r"control\.response_too_large"):
                client.call("google.accounts.list", {})
    else:
        FakeHTTPSConnection.response = FakeHTTPResponse(oversized)
        monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)
        with pytest.raises(MasterjetClientError, match=r"control\.response_too_large"):
            https_client(bearer_provider=lambda: "remote-bearer").call(
                "google.accounts.list", {}
            )


def test_request_larger_than_limit_is_rejected_before_https_connect(monkeypatch):
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.projects.list",
            {"account_ref": "google-1", "padding": "x" * MAX_REQUEST_BYTES},
        )

    assert FakeHTTPSConnection.instances == []


def test_local_secret_uses_scm_rights_and_never_enters_json(tmp_path):
    secret = bytearray(b"super-private-auth-json")
    socket_path = tmp_path / "masterjet.sock"
    response = unix_success(ingress_receipt_payload())

    peer: list[tuple[int, int, int, socket.socket]] = []

    def attest(pid, uid, gid, connected_socket):
        peer.append((pid, uid, gid, connected_socket))
        return True

    with unix_server(socket_path, response) as capture:
        result = local_client(
            socket_path,
            local_attestation_verifier=attest,
            step_up_provider=lambda: "123456",
        ).put_secret(
            "ingress-1",
            secret,
            expected_generation=4,
            idempotency_key="idem-1",
        )

    assert result.state == "consumed"
    assert peer[0][:3] == (os.getpid(), os.geteuid(), os.getegid())
    assert isinstance(peer[0][3], socket.socket)
    assert bytes(secret) not in capture["request"]
    assert capture["secrets"] == [bytes(secret)]
    request = json.loads(capture["request"])
    assert request == {
        "schema_version": 1,
        "transport": "secret.put",
        "session_id": "ingress-1",
        "expected_generation": 4,
        "idempotency_key": "idem-1",
    }


def test_https_secret_uses_bounded_raw_body_not_json(monkeypatch):
    secret = bytearray(b"oauth-client-json-private")
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps(ingress_receipt_payload()).encode()
    )
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    https_client(
        bearer_provider=lambda: "remote-bearer",
        step_up_provider=lambda: "123456",
    ).put_secret(
        "ingress-1",
        secret,
        expected_generation=4,
        idempotency_key="idem-1",
    )

    method, target, body, headers = FakeHTTPSConnection.instances[0].requests[0]
    assert (method, target, body) == (
        "PUT",
        "/admin/v1/secret-ingress-sessions/ingress-1",
        bytes(secret),
    )
    assert headers["Content-Type"] == "application/octet-stream"
    assert bytes(secret) not in target.encode()
    assert all(bytes(secret) not in value.encode() for value in headers.values())


def test_https_secret_uses_encoded_session_id_on_fixed_admin_route(monkeypatch):
    receipt = ingress_receipt_payload()
    receipt["session_id"] = "ingress:one"
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps(receipt).encode()
    )
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    https_client(
        bearer_provider=lambda: "remote-bearer",
        step_up_provider=lambda: "123456",
    ).put_secret(
        "ingress:one",
        b"private",
        expected_generation=4,
        idempotency_key="idem-1",
    )

    method, target, _body, _headers = FakeHTTPSConnection.instances[0].requests[0]
    assert (method, target) == (
        "PUT",
        "/admin/v1/secret-ingress-sessions/ingress%3Aone",
    )


@pytest.mark.parametrize(
    ("operation", "arguments", "expected", "extra"),
    [
        ("hosts.list", {}, ("GET", "/admin/v1/hosts"), {}),
        ("openai.accounts.list", {}, ("GET", "/admin/v1/openai/accounts"), {}),
        (
            "openai.auth.plan",
            {"account_ref": "openai-1"},
            ("POST", "/admin/v1/openai/accounts/openai-1/auth-sync-plans"),
            {"expected_generation": 4, "idempotency_key": "idem-openai-plan"},
        ),
        (
            "openai.auth.apply",
            {"account_ref": "openai-1"},
            (
                "POST",
                "/admin/v1/openai/accounts/openai-1/auth-sync-plans/"
                + "sha256%3A"
                + "a" * 64
                + "/apply",
            ),
            {
                "expected_generation": 4,
                "idempotency_key": "idem-openai-apply",
                "plan_digest": "sha256:" + "a" * 64,
            },
        ),
        (
            "secret.ingress.create",
            {"account_ref": "openai-1", "credential_kind": "openai.auth-json"},
            ("POST", "/admin/v1/secret-ingress-sessions"),
            {
                "expected_generation": 4,
                "idempotency_key": "idem-ingress-create",
                "plan_digest": "sha256:" + "a" * 64,
            },
        ),
        ("google.accounts.list", {}, ("GET", "/admin/v1/google/accounts"), {}),
        (
            "google.projects.list",
            {"account_ref": "google-1"},
            ("GET", "/admin/v1/google/accounts/google-1"),
            {},
        ),
        (
            "operations.get",
            {"account_ref": "google-1", "operation_id": "operation-1"},
            ("GET", "/admin/v1/operations/operation-1"),
            {},
        ),
        (
            "google.oauth.begin",
            {
                "account_ref": "google-1",
                "oauth_client_ref": "oauth-client-1",
                "redirect_uri": REDIRECT_URI,
                "scope_profile": "default",
            },
            ("POST", "/admin/v1/google/oauth-transactions"),
            {"expected_generation": 4, "idempotency_key": "idem-oauth-begin"},
        ),
        (
            "google.oauth.complete",
            {
                "account_ref": "google-1",
                "transaction_id": "transaction-1",
                "redirect_uri": REDIRECT_URI,
                "state": "state-1",
            },
            ("POST", "/admin/v1/google/oauth-transactions/transaction-1/complete"),
            {"expected_generation": 4},
        ),
        (
            "google.oauth-client-import.plan",
            {"account_ref": "google-1"},
            ("POST", "/admin/v1/google/oauth-client-import-plans"),
            {"expected_generation": 4, "idempotency_key": "idem-oauth-import-plan"},
        ),
        (
            "google.oauth-client-import.apply",
            {"account_ref": "google-1"},
            (
                "POST",
                "/admin/v1/google/oauth-client-import-plans/"
                + "sha256%3A"
                + "a" * 64
                + "/apply",
            ),
            {
                "expected_generation": 4,
                "idempotency_key": "idem-oauth-import-apply",
                "plan_digest": "sha256:" + "a" * 64,
            },
        ),
        (
            "google.inventory.refresh",
            {},
            ("POST", "/admin/v1/google/inventory-refreshes"),
            {"expected_generation": 4, "idempotency_key": "idem-inventory-refresh"},
        ),
        (
            "google.provision.plan",
            {"account_ref": "google-1"},
            ("POST", "/admin/v1/google/provision-plans"),
            {"expected_generation": 4, "idempotency_key": "idem-provision-plan"},
        ),
        (
            "google.provision.apply",
            {"account_ref": "google-1"},
            (
                "POST",
                "/admin/v1/google/provision-plans/"
                + "sha256%3A"
                + "a" * 64
                + "/apply",
            ),
            {
                "expected_generation": 4,
                "idempotency_key": "idem-provision-apply",
                "plan_digest": "sha256:" + "a" * 64,
            },
        ),
        (
            "google.billing.plan",
            {
                "account_ref": "google-1",
                "project_ref": "project-1",
                "billing_ref": "billing-1",
            },
            ("POST", "/admin/v1/google/billing-bind-plans"),
            {"expected_generation": 4, "idempotency_key": "idem-billing-plan"},
        ),
        (
            "google.billing.apply",
            {
                "account_ref": "google-1",
                "project_ref": "project-1",
                "billing_ref": "billing-1",
                "plan_id": "billing-plan-1",
            },
            ("POST", "/admin/v1/google/billing-bind-plans/billing-plan-1/apply"),
            {
                "expected_generation": 4,
                "idempotency_key": "idem-billing-apply",
                "plan_digest": "sha256:" + "a" * 64,
            },
        ),
        ("ollama.models.list", {}, ("GET", "/admin/v1/ollama/models"), {}),
        ("ollama.instances.list", {}, ("GET", "/admin/v1/ollama/instances"), {}),
        (
            "ollama.instance.plan",
            {
                "ref": "instance-one",
                "label": "Instance One",
                "host_ref": "control-host",
                "ollama_executable": "/usr/bin/ollama",
                "models_directory": "/var/lib/ollama",
                "selected_model_refs": ["model-one"],
                "allowed_cpus": "0-1",
                "cpu_quota_percent": 200,
                "cpu_weight": 100,
            },
            ("POST", "/admin/v1/ollama/instance-plans"),
            {"expected_generation": 4, "idempotency_key": "idem-ollama-plan"},
        ),
        (
            "ollama.instance.apply",
            {"plan_id": "ollama-plan-1"},
            ("POST", "/admin/v1/ollama/instance-plans/ollama-plan-1/apply"),
            {
                "expected_generation": 4,
                "idempotency_key": "idem-ollama-apply",
                "plan_digest": "sha256:" + "a" * 64,
            },
        ),
        (
            "ollama.instance.probe",
            {"instance_ref": "ollama-instance-1"},
            ("POST", "/admin/v1/ollama/instances/ollama-instance-1/probe"),
            {"expected_generation": 4, "idempotency_key": "idem-ollama-probe"},
        ),
    ],
)
def test_https_documented_operations_use_fixed_routes_without_get_body(
    monkeypatch, operation, arguments, expected, extra
):
    FakeHTTPSConnection.response = FakeHTTPResponse(b"{}")
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.response_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            operation, arguments, **extra
        )

    method, target, body, headers = FakeHTTPSConnection.instances[0].requests[0]
    assert (method, target) == expected
    if method == "GET":
        assert body == b""
        assert "Content-Type" not in headers
    else:
        assert json.loads(body)["operation"] == operation
        assert headers["Content-Type"] == "application/json"
    assert "?" not in target
    assert "/admin/v1" != target


def test_https_route_segments_percent_encode_opaque_refs_without_query(monkeypatch):
    FakeHTTPSConnection.response = FakeHTTPResponse(b"{}")
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.response_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "operations.get",
            {"account_ref": "google:one", "operation_id": "plan:one"},
        )

    method, target, body, _headers = FakeHTTPSConnection.instances[0].requests[0]
    assert (method, body) == ("GET", b"")
    assert target == "/admin/v1/operations/plan%3Aone"
    assert "?" not in target


def test_secret_larger_than_limit_is_rejected_before_connect(monkeypatch):
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_too_large"):
        https_client(bearer_provider=lambda: "remote-bearer").put_secret(
            "ingress-1",
            b"x" * (MAX_SECRET_BYTES + 1),
            expected_generation=4,
            idempotency_key="idem-1",
        )

    assert FakeHTTPSConnection.instances == []


def test_secret_byte_subclass_is_rejected_before_connect(monkeypatch):
    class SecretBytes(bytes):
        pass

    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").put_secret(
            "ingress-1",
            SecretBytes(b"private"),
        )

    assert FakeHTTPSConnection.instances == []


def test_unbound_generic_secret_put_is_rejected_before_connect(monkeypatch):
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "secret.ingress.put", b"private"
        )

    assert FakeHTTPSConnection.instances == []


def test_secret_ingress_create_returns_bound_typed_session(monkeypatch):
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps(ingress_session_payload()).encode()
    )

    session = https_client(
        bearer_provider=lambda: "remote-bearer",
        step_up_provider=lambda: "123456",
    ).call(
        "secret.ingress.create",
        {
            "account_ref": "openai-1",
            "credential_kind": "openai.auth-json",
        },
        expected_generation=4,
        idempotency_key="idem-1",
        plan_digest="sha256:" + "a" * 64,
    )

    assert session.id == "ingress-1"
    assert session.plan_digest == "sha256:" + "a" * 64
    assert session.session_generation == 4


def test_secret_ingress_receipt_for_another_session_is_rejected(monkeypatch):
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps(ingress_receipt_payload() | {"session_id": "ingress-2"}).encode()
    )

    with pytest.raises(MasterjetClientError, match=r"control\.response_invalid"):
        https_client(
            bearer_provider=lambda: "remote-bearer",
            step_up_provider=lambda: "123456",
        ).put_secret(
            "ingress-1",
            b"private",
            expected_generation=4,
            idempotency_key="idem-secret-put",
        )


def test_oauth_begin_decodes_bound_typed_transaction(monkeypatch):
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps(google_oauth_transaction_payload()).encode()
    )
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    result = https_client(
        bearer_provider=lambda: "remote-bearer",
        step_up_provider=lambda: "123456",
    ).call(
        "google.oauth.begin",
        {
            "account_ref": "google-1",
            "oauth_client_ref": "oauth-client-1",
            "redirect_uri": REDIRECT_URI,
            "scope_profile": "default",
        },
        expected_generation=4,
        idempotency_key="idem-1",
    )

    assert result.id == "oauth-1"
    assert result.account_ref == "google-1"
    assert result.inventory_generation == 4


def test_oauth_client_import_plan_and_receipt_decode_canonical_wire(monkeypatch):
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)
    client = https_client(
        bearer_provider=lambda: "remote-bearer",
        step_up_provider=lambda: "123456",
    )
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps(
            {
                "schema_version": 1,
                "id": "oauth-import-one",
                "account_ref": "google-1",
                "expected_generation": 4,
                "expires_at": 1_788_000_000.0,
                "plan_digest": "sha256:" + "a" * 64,
            }
        ).encode()
    )
    plan = client.call(
        "google.oauth-client-import.plan",
        {"account_ref": "google-1"},
        expected_generation=4,
        idempotency_key="idem-plan",
    )
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps(
            {
                "schema_version": 1,
                "account_ref": "google-1",
                "client_ref": "client-one",
                "display_name": "Quiet Client",
                "inventory_generation": 4,
                "client_digest": "sha256:" + "b" * 64,
            }
        ).encode()
    )
    receipt = client.call(
        "google.oauth-client-import.apply",
        {"account_ref": "google-1"},
        expected_generation=4,
        idempotency_key="idem-apply",
        plan_digest=plan.plan_digest,
    )

    assert plan.id == "oauth-import-one"
    assert receipt.client_ref == "client-one"
    assert receipt.inventory_generation == 4


def test_google_account_add_uses_fixed_post_route_and_binds_receipt(monkeypatch):
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps(
            {
                "schema_version": 1,
                "account": {"ref": "google-3", "generation": 10},
            }
        ).encode()
    )

    receipt = https_client(bearer_provider=lambda: "remote-bearer").call(
        "google.accounts.add",
        {"account_ref": "google-3", "label": "Google Three"},
        expected_generation=9,
        idempotency_key="idem-add",
    )

    assert receipt.account_ref == "google-3"
    assert receipt.resulting_generation == 10
    assert FakeHTTPSConnection.instances[-1].requests[-1][0] == "POST"
    assert FakeHTTPSConnection.instances[-1].requests[-1][1] == "/admin/v1/google/accounts"


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "account": {"ref": "google-other", "generation": 10}},
        {"schema_version": 1, "account": {"ref": "google-3", "generation": 11}},
    ],
)
def test_google_account_add_rejects_unbound_receipt(monkeypatch, payload):
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)
    FakeHTTPSConnection.response = FakeHTTPResponse(json.dumps(payload).encode())

    with pytest.raises(MasterjetClientError, match=r"control\.response_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.add",
            {"account_ref": "google-3", "label": "Google Three"},
            expected_generation=9,
            idempotency_key="idem-add",
        )


def test_oauth_begin_rejects_transaction_for_another_account(monkeypatch):
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps(
            google_oauth_transaction_payload() | {"account_ref": "google-2"}
        ).encode()
    )
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.response_invalid"):
        https_client(
            bearer_provider=lambda: "remote-bearer",
            step_up_provider=lambda: "123456",
        ).call(
            "google.oauth.begin",
            {
                "account_ref": "google-1",
                "oauth_client_ref": "oauth-client-1",
                "redirect_uri": REDIRECT_URI,
                "scope_profile": "default",
            },
            expected_generation=4,
            idempotency_key="idem-1",
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"account_ref": "google-1", "browser": "firefox"},
        {
            "account_ref": "google-1",
            "browser": "firefox",
            "redirect_uri": "http://localhost:8765/oauth/callback",
        },
        {
            "account_ref": "google-1",
            "browser": "firefox",
            "redirect_uri": "http://127.0.0.1/oauth/callback",
        },
    ],
)
def test_oauth_begin_rejects_missing_or_unbound_redirect_before_transport(arguments):
    with pytest.raises(MasterjetClientError, match=r"control\.request_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.oauth.begin",
            arguments,
            expected_generation=4,
            idempotency_key="idem-1",
        )

    assert FakeHTTPSConnection.instances == []


def test_operations_get_binds_returned_operation_id(monkeypatch):
    payload = {
        "schema_version": 1,
        "id": "plan-2",
        "kind": "google.provision.plan",
        "state": "planned",
        "expected_generation": 4,
        "resulting_generation": None,
        "plan_digest": "sha256:" + "a" * 64,
        "created_at": "2026-08-28T12:00:00Z",
        "expires_at": "2026-08-28T12:05:00Z",
        "completed_count": 0,
        "failed_count": 0,
        "not_attempted_count": 1,
        "reason_codes": [],
    }
    FakeHTTPSConnection.response = FakeHTTPResponse(json.dumps(payload).encode())
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.response_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "operations.get",
            {"operation_id": "plan-1", "account_ref": "google-1"},
        )


@pytest.mark.parametrize(
    "operation_name,arguments",
    [
        (
            "operations.get",
            {"operation_id": "plan-1", "account_ref": "google-1"},
        ),
        ("google.oauth-client-import.plan", {"account_ref": "google-1"}),
    ],
)
def test_task6_specified_operations_are_accepted_by_request_contract(
    monkeypatch, operation_name, arguments
):
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)
    FakeHTTPSConnection.response = FakeHTTPResponse(b"{}")

    with pytest.raises(MasterjetClientError, match=r"control\.response_invalid"):
        https_client(
            bearer_provider=lambda: "remote-bearer",
            step_up_provider=lambda: "123456",
        ).call(
            operation_name,
            arguments,
            **(
                {}
                if operation_name == "operations.get"
                else {"expected_generation": 4, "idempotency_key": "idem-1"}
            ),
        )

    assert len(FakeHTTPSConnection.instances) == 1


def test_operations_get_accepts_operation_id_without_account_route_ref(monkeypatch):
    FakeHTTPSConnection.response = FakeHTTPResponse(b"{}")
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.response_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "operations.get", {"operation_id": "plan-1"}
        )

    method, target, body, _headers = FakeHTTPSConnection.instances[0].requests[0]
    assert (method, target, body) == ("GET", "/admin/v1/operations/plan-1", b"")


def test_operations_get_without_account_ref_accepts_matching_provision_plan(monkeypatch):
    payload = {
        "schema_version": 1,
        "id": "plan-1",
        "account_ref": "google-1",
        "kind": "google.provision.plan",
        "state": "planned",
        "expected_generation": 4,
        "resulting_generation": None,
        "plan_digest": "sha256:" + "a" * 64,
        "created_at": "2026-08-28T12:00:00Z",
        "expires_at": "2026-08-28T12:05:00Z",
        "completed_count": 0,
        "failed_count": 0,
        "not_attempted_count": 1,
        "reason_codes": [],
        "step_count": 1,
        "projects": [{"project_name": "Amber Orchard", "key_name": "Willow Meadow"}],
    }
    FakeHTTPSConnection.response = FakeHTTPResponse(json.dumps(payload).encode())
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    result = https_client(bearer_provider=lambda: "remote-bearer").call(
        "operations.get", {"operation_id": "plan-1"}
    )

    assert result.id == "plan-1"
    assert result.account_ref == "google-1"
    method, target, body, _headers = FakeHTTPSConnection.instances[0].requests[0]
    assert (method, target, body) == ("GET", "/admin/v1/operations/plan-1", b"")


def test_https_uses_verified_tls_fixed_target_and_transient_auth_headers(monkeypatch):
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps(google_oauth_transaction_payload()).encode()
    )
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    client = https_client(
        bearer_provider=lambda: "remote-bearer",
        step_up_provider=lambda: "123456",
    )
    client.call(
        "google.oauth.begin",
        {
            "account_ref": "google-1",
            "oauth_client_ref": "oauth-client-1",
            "redirect_uri": REDIRECT_URI,
            "scope_profile": "default",
        },
        expected_generation=4,
        idempotency_key="idem-1",
    )

    connection = FakeHTTPSConnection.instances[0]
    method, target, body, headers = connection.requests[0]
    assert (connection.host, connection.port, connection.timeout) == (
        "masterjet.example.test",
        8443,
        7,
    )
    assert connection.context is not None
    assert connection.context.check_hostname is True
    assert connection.context.verify_mode == ssl.CERT_REQUIRED
    assert (method, target) == (
        "POST",
        "/admin/v1/google/oauth-transactions",
    )
    request = json.loads(body)
    assert request["operation"] == "google.oauth.begin"
    assert request["arguments"]["redirect_uri"] == REDIRECT_URI
    assert headers["Authorization"] == "Bearer remote-bearer"
    assert "X-Masterjet-Step-Up" not in headers
    assert headers["Cache-Control"] == "no-store"
    assert headers["Content-Type"] == "application/json"
    assert "remote-bearer" not in repr(client)
    assert "123456" not in repr(client)


def test_https_redirect_is_rejected_without_following_location(monkeypatch):
    FakeHTTPSConnection.response = FakeHTTPResponse(
        b"",
        status=307,
        content_type="text/plain",
        headers={"location": "https://attacker.example.test/collect"},
    )
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.redirect_rejected"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )

    assert len(FakeHTTPSConnection.instances) == 1
    assert len(FakeHTTPSConnection.instances[0].requests) == 1


def test_https_timeout_is_mapped_without_leaking_bearer(monkeypatch):
    FakeHTTPSConnection.error = TimeoutError("remote-bearer should stay private")
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.timeout") as caught:
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )

    assert "remote-bearer" not in str(caught.value)


def test_local_endpoint_must_be_private_socket(tmp_path):
    endpoint = tmp_path / "not-a-socket"
    endpoint.write_text("not a socket", encoding="utf-8")
    endpoint.chmod(0o600)

    with pytest.raises(MasterjetClientError, match=r"control\.endpoint_invalid"):
        local_client(endpoint).call("google.accounts.list", {})


def test_malformed_projection_is_rejected_by_endpoint_parser(monkeypatch):
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps({"schema_version": 1, "accounts": [], "access_token": "private"}).encode()
    )
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.response_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )


def test_openai_accounts_endpoint_uses_openai_projection_parser(monkeypatch):
    payload = {
        "schema_version": 1,
        "accounts": [
            {
                "ref": "openai-1",
                "label": "OpenAI account 01",
                "enabled": True,
                "local_profile_ref": "profile-1",
                "source_host_ref": "host-1",
                "auth_state": "ready",
                "access_expires_at": "2026-08-28T12:00:00Z",
                "credential_generation": 8,
                "vault_projection_state": "current",
                "usage_state": "fresh",
            }
        ],
    }
    FakeHTTPSConnection.response = FakeHTTPResponse(json.dumps(payload).encode())
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    result = https_client(bearer_provider=lambda: "remote-bearer").call(
        "openai.accounts.list", {}
    )

    assert result[0].local_profile_ref == "profile-1"


def test_google_projects_endpoint_binds_parser_to_requested_account(monkeypatch):
    payload = {
        "schema_version": 1,
        "account_ref": "google-1",
        "inventory_generation": 4,
        "projects": [
            {
                "ref": "project-1",
                "project_name": "Amber Orchard",
                "purpose": "quota_probe",
                "key_name": "Quota Probe",
                "billing_ref": None,
                "status": "ready",
                "probe_state": "ready",
                "quota_state": "available",
            }
        ],
    }
    FakeHTTPSConnection.response = FakeHTTPResponse(json.dumps(payload).encode())
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    result = https_client(bearer_provider=lambda: "remote-bearer").call(
        "google.projects.list", {"account_ref": "google-1"}
    )

    assert result.account_ref == "google-1"
    assert result.projects[0].ref == "project-1"


def test_local_problem_uses_same_canonical_problem_contract(tmp_path):
    problem = {
        "schema_version": 1,
        "code": "authority.scope_denied",
        "severity": "error",
        "title": "Authority scope denied",
        "detail": "Authority scope is denied.",
        "effect": "Operation is denied.",
        "action": "Request required scope.",
        "retryable": False,
        "retry_after_seconds": None,
        "correlation_id": "correlation-1",
        "occurred_at": "2026-08-28T12:00:00Z",
    }
    socket_path = tmp_path / "masterjet.sock"

    with unix_server(socket_path, unix_problem(problem)):
        with pytest.raises(MasterjetClientError, match=r"authority\.scope_denied") as caught:
            local_client(socket_path).call("google.accounts.list", {})

    assert caught.value.problem is not None
    assert caught.value.problem.title == "Authority scope denied"


def test_nested_secret_field_is_rejected_before_connect(monkeypatch):
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.projects.list",
            {"account_ref": "google-1", "items": ({"access_token": "private"},)},
        )

    assert FakeHTTPSConnection.instances == []


def test_client_repr_never_echoes_unvalidated_endpoint_credentials():
    client = MasterjetControlClient(
        MasterjetConnection(
            transport="https",
            endpoint="https://user:private@example.test/control",
            timeout_seconds=7,
        )
    )

    assert "user" not in repr(client)
    assert "private" not in repr(client)


@pytest.mark.parametrize(
    "arguments",
    [
        {"token": "sk-private"},
        {"credential": "ya29.private"},
        {"apiKey": "ordinary-looking-value"},
        {"nested": [{"clientSecret": "ordinary-looking-value"}]},
    ],
)
def test_secret_shaped_keys_and_values_never_reach_json(monkeypatch, arguments):
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.projects.list", {"account_ref": "google-1", **arguments}
        )

    assert FakeHTTPSConnection.instances == []


@pytest.mark.parametrize(
    ("operation", "idempotency_key"),
    [
        ("sk-private", None),
        ("google.accounts.list", "ya29.private"),
    ],
)
def test_secret_shaped_control_tokens_are_rejected(monkeypatch, operation, idempotency_key):
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            operation,
            {},
            idempotency_key=idempotency_key,
        )

    assert FakeHTTPSConnection.instances == []


def test_json_container_subclasses_are_rejected_before_transport(monkeypatch):
    class Arguments(dict):
        pass

    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.projects.list", Arguments(account_ref="google-1")
        )

    assert FakeHTTPSConnection.instances == []


def test_cyclic_request_is_mapped_before_transport(monkeypatch):
    cycle: list[object] = []
    cycle.append(cycle)
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.projects.list", {"account_ref": cycle}
        )

    assert FakeHTTPSConnection.instances == []


def test_excessively_deep_request_is_rejected_before_transport(monkeypatch):
    nested: object = "leaf"
    for _ in range(40):
        nested = [nested]
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.projects.list", {"account_ref": nested}
        )

    assert FakeHTTPSConnection.instances == []


def test_duplicate_json_response_keys_are_rejected(monkeypatch):
    body = (
        b'{"schema_version":1,'
        b'"accounts":[{"access_token":"sk-private"}],'
        b'"accounts":[]}'
    )
    FakeHTTPSConnection.response = FakeHTTPResponse(body)
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.response_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )


def test_excessively_deep_json_response_is_mapped(monkeypatch):
    FakeHTTPSConnection.response = FakeHTTPResponse(b"[" * 2_000 + b"0" + b"]" * 2_000)
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.response_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )


def test_secret_payload_is_rejected_for_unknown_operation_before_connect(monkeypatch):
    provider_called = False

    def step_up():
        nonlocal provider_called
        provider_called = True
        return "123456"

    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_invalid"):
        https_client(
            bearer_provider=lambda: "remote-bearer",
            step_up_provider=step_up,
        ).call("fixture.echo", b"private")

    assert provider_called is False
    assert FakeHTTPSConnection.instances == []


def test_unknown_operation_with_neutral_secret_shape_is_rejected_before_auth(monkeypatch):
    bearer_called = False

    def bearer():
        nonlocal bearer_called
        bearer_called = True
        return "remote-bearer"

    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_invalid"):
        https_client(bearer_provider=bearer).call(
            "fixture.opaque",
            {"payload": "pvt_0123456789abcdef"},
        )

    assert bearer_called is False
    assert FakeHTTPSConnection.instances == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"account_ref": "google-1", "payload": "ordinary-value"},
        {"account_ref": 7},
        {"account_ref": "google-1", "metadata": {"payload": "pvt-private"}},
    ],
)
def test_operation_contract_rejects_unknown_fields_and_wrong_types(monkeypatch, arguments):
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.projects.list",
            arguments,
        )

    assert FakeHTTPSConnection.instances == []


def test_provision_operation_is_not_in_step_up_allowlist(monkeypatch):
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps(
            {
                "schema_version": 1,
                "id": "operation-1",
                "kind": "google.provision.apply",
                "state": "queued",
                "expected_generation": 4,
                "resulting_generation": None,
                "plan_digest": "sha256:" + "a" * 64,
                "created_at": "2026-08-28T12:00:00Z",
                "expires_at": "2026-08-28T12:01:00Z",
                "completed_count": 0,
                "failed_count": 0,
                "not_attempted_count": 1,
                "reason_codes": [],
            }
        ).encode()
    )
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    result = https_client(bearer_provider=lambda: "remote-bearer").call(
        "google.provision.apply",
        {"account_ref": "google-1"},
        expected_generation=4,
        idempotency_key="idem-1",
        plan_digest="sha256:" + "a" * 64,
    )

    assert result.kind == "google.provision.apply"
    assert "X-Masterjet-Step-Up" not in FakeHTTPSConnection.instances[0].requests[0][3]


def test_local_secret_requires_confirmed_transport_attestation(tmp_path):
    socket_path = tmp_path / "masterjet.sock"
    response = json.dumps(google_accounts_payload()).encode()
    step_up_called = False

    def step_up():
        nonlocal step_up_called
        step_up_called = True
        return "123456"

    with unix_server(socket_path, response) as capture:
        with pytest.raises(MasterjetClientError, match=r"control\.attestation_required"):
            local_client(
                socket_path,
                local_attestation_verifier=None,
                step_up_provider=step_up,
            ).put_secret(
                "ingress-1",
                b"private",
                expected_generation=4,
                idempotency_key="idem-1",
            )

    assert step_up_called is False
    assert capture.get("secrets", []) == []


def test_rejected_transport_attestation_prevents_fd_send(tmp_path):
    socket_path = tmp_path / "masterjet.sock"
    response = json.dumps(google_accounts_payload()).encode()
    step_up_called = False

    def step_up():
        nonlocal step_up_called
        step_up_called = True
        return "123456"

    with unix_server(socket_path, response) as capture:
        with pytest.raises(MasterjetClientError, match=r"control\.attestation_required"):
            local_client(
                socket_path,
                local_attestation_verifier=lambda _pid, _uid, _gid, _socket: False,
                step_up_provider=step_up,
            ).put_secret(
                "ingress-1",
                b"private",
                expected_generation=4,
                idempotency_key="idem-1",
            )

    assert step_up_called is False
    assert capture.get("secrets", []) == []


@pytest.mark.parametrize("value", ["has space", "has\ttab", "tök", "\ud800"])
def test_bearer_rejects_non_ascii_header_values_before_request(monkeypatch, value):
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.authentication_required"):
        https_client(bearer_provider=lambda: value).call("google.accounts.list", {})

    assert FakeHTTPSConnection.instances == []


@pytest.mark.parametrize("value", ["12 3456", "12\t3456", "tötp", "\ud800"])
def test_step_up_rejects_non_ascii_header_values_before_request(monkeypatch, value):
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps(step_up_problem_payload()).encode()
    )

    with pytest.raises(MasterjetClientError, match=r"control\.step_up_required"):
        https_client(
            bearer_provider=lambda: "remote-bearer",
            step_up_provider=lambda: value,
        ).call(
            "google.oauth.begin",
            {
                "account_ref": "google-1",
                "oauth_client_ref": "oauth-client-1",
                "redirect_uri": REDIRECT_URI,
                "scope_profile": "default",
            },
            expected_generation=4,
            idempotency_key="idem-1",
        )

    assert len(FakeHTTPSConnection.instances) == 1
    assert "X-Masterjet-Step-Up" not in FakeHTTPSConnection.instances[0].requests[0][3]


def test_local_endpoint_rejects_embedded_nul():
    client = MasterjetControlClient(
        MasterjetConnection(
            transport="local",
            endpoint="/tmp/masterjet\0.sock",
            timeout_seconds=2,
        )
    )

    with pytest.raises(MasterjetClientError, match=r"control\.endpoint_invalid"):
        client.call("google.accounts.list", {})


def test_local_endpoint_rejects_symlink_ancestor(tmp_path):
    socket_dir = tmp_path / "real"
    socket_dir.mkdir(mode=0o700)
    socket_path = socket_dir / "masterjet.sock"
    alias = tmp_path / "alias"
    alias.symlink_to(socket_dir, target_is_directory=True)

    with unix_server(socket_path, json.dumps(google_accounts_payload()).encode()) as capture:
        with pytest.raises(MasterjetClientError, match=r"control\.endpoint_invalid"):
            local_client(alias / "masterjet.sock").call("google.accounts.list", {})

    assert capture["accepted"] is False


def test_local_slow_drip_cannot_extend_end_to_end_deadline(tmp_path):
    socket_path = tmp_path / "masterjet.sock"
    connection = MasterjetConnection(
        transport="local",
        endpoint=str(socket_path),
        timeout_seconds=1,
    )
    started = time.monotonic()

    with unix_server(
        socket_path,
        b'{"padding":"xxxxxxxx"}',
        response_delay=0.1,
    ):
        with pytest.raises(MasterjetClientError, match=r"control\.timeout"):
            MasterjetControlClient(
                connection,
                local_attestation_verifier=lambda *_args: True,
            ).call("google.accounts.list", {})

    assert time.monotonic() - started < 1.6


def test_swap_and_restore_socket_fails_attestation_before_fd_send(tmp_path, monkeypatch):
    endpoint = tmp_path / "masterjet.sock"
    parked = tmp_path / "masterjet.original.sock"
    legitimate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    legitimate.bind(str(endpoint))
    endpoint.chmod(0o600)
    legitimate.listen(1)
    original_identity = endpoint.lstat()
    real_identity = client_module._socket_identity
    malicious: socket.socket | None = None
    malicious_finished = threading.Event()
    capture: dict[str, object] = {}
    identity_calls = 0

    def malicious_server(server):
        try:
            connection, _ = server.accept()
            with connection:
                capture["handshake"] = connection.recv(64)
                connection.sendall(b"BAD\n")
                _data, ancillary, _flags, _address = connection.recvmsg(
                    65_536,
                    socket.CMSG_SPACE(4 * array.array("i").itemsize),
                )
                capture["fd_messages"] = [
                    item for item in ancillary if item[1] == socket.SCM_RIGHTS
                ]
        finally:
            malicious_finished.set()

    def swapped_identity(path):
        nonlocal identity_calls, malicious
        identity_calls += 1
        if identity_calls == 1:
            identity = real_identity(path)
            endpoint.rename(parked)
            malicious = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            malicious.bind(str(endpoint))
            endpoint.chmod(0o600)
            malicious.listen(1)
            threading.Thread(
                target=malicious_server,
                args=(malicious,),
                daemon=True,
            ).start()
            return identity
        endpoint.unlink()
        parked.rename(endpoint)
        return real_identity(path)

    def attest(_pid, _uid, _gid, connected_socket):
        connected_socket.sendall(b"ATTEST\n")
        return connected_socket.recv(4) == b"OK\n"

    monkeypatch.setattr(client_module, "_socket_identity", swapped_identity)
    try:
        with pytest.raises(MasterjetClientError, match=r"control\.attestation_required"):
            local_client(
                endpoint,
                local_attestation_verifier=attest,
                step_up_provider=lambda: "123456",
            ).put_secret(
                "ingress-1",
                b"private",
                expected_generation=4,
                idempotency_key="idem-1",
            )
        assert malicious_finished.wait(2)
        assert capture["handshake"] == b"ATTEST\n"
        assert capture["fd_messages"] == []
        restored = endpoint.lstat()
        assert (restored.st_dev, restored.st_ino) == (
            original_identity.st_dev,
            original_identity.st_ino,
        )
    finally:
        legitimate.close()
        if malicious is not None:
            malicious.close()


@contextmanager
def real_tls_server(tmp_path, *, wire_delay: float = 0, responses=None):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl unavailable for stdlib TLS fixture")
    certificate = tmp_path / "server.crt"
    private_key = tmp_path / "server.key"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ],
        check=True,
        capture_output=True,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate, private_key)
    capture: dict[str, object] = {}
    server_context.sni_callback = lambda _socket, name, _context: capture.update(sni=name)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    scripted_responses = responses or [google_accounts_payload()]
    listener.listen(len(scripted_responses))
    listener.settimeout(3)
    port = listener.getsockname()[1]
    finished = threading.Event()

    def serve():
        try:
            capture["requests"] = []
            for scripted_response in scripted_responses:
                connection, _ = listener.accept()
                try:
                    with server_context.wrap_socket(connection, server_side=True) as tls_socket:
                        request = bytearray()
                        while b"\r\n\r\n" not in request:
                            chunk = tls_socket.recv(4096)
                            if not chunk:
                                return
                            request.extend(chunk)
                        head, body = request.split(b"\r\n\r\n", 1)
                        lines = head.decode("ascii").split("\r\n")
                        headers = dict(line.split(": ", 1) for line in lines[1:])
                        length = int(headers.get("Content-Length", "0"))
                        while len(body) < length:
                            chunk = tls_socket.recv(length - len(body))
                            if not chunk:
                                return
                            body.extend(chunk)
                        request_capture = {
                            "method": lines[0].split(" ", 1)[0],
                            "request_line": lines[0],
                            "headers": headers,
                            "body": bytes(body),
                        }
                        capture["requests"].append(request_capture)
                        capture.update(request_capture)
                        response = json.dumps(scripted_response).encode()
                        wire_response = (
                            b"HTTP/1.1 200 OK\r\n"
                            b"Content-Type: application/json\r\n"
                            + f"Content-Length: {len(response)}\r\n".encode()
                            + b"Connection: close\r\n\r\n"
                            + response
                        )
                        if wire_delay:
                            for byte in wire_response:
                                tls_socket.sendall(bytes([byte]))
                                time.sleep(wire_delay)
                        else:
                            tls_socket.sendall(wire_response)
                        tls_socket.shutdown(socket.SHUT_RDWR)
                        tls_socket.close()
                except (OSError, ssl.SSLError) as exc:
                    capture["tls_error"] = type(exc).__name__
        finally:
            listener.close()
            finished.set()

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield port, certificate, capture
    finally:
        assert finished.wait(4)


def test_real_https_verifies_tls_sni_host_and_content_length(tmp_path, monkeypatch):
    original_default_context = ssl.create_default_context
    original_getaddrinfo = socket.getaddrinfo
    parent_pid = os.getpid()

    def child_only_getaddrinfo(*args, **kwargs):
        if os.getpid() == parent_pid:
            raise AssertionError("parent process attempted name resolution")
        return original_getaddrinfo(*args, **kwargs)

    with real_tls_server(tmp_path) as (port, certificate, capture):
        monkeypatch.setattr(
            client_module,
            "_open_https_connection",
            _REAL_OPEN_HTTPS_CONNECTION,
        )
        monkeypatch.setattr(
            client_module.ssl,
            "create_default_context",
            lambda: original_default_context(cafile=certificate),
        )
        monkeypatch.setattr(client_module.socket, "getaddrinfo", child_only_getaddrinfo)
        connection = MasterjetConnection(
            transport="https",
            endpoint=f"https://localhost:{port}/control",
            timeout_seconds=5,
        )

        result = MasterjetControlClient(
            connection,
            bearer_provider=lambda: "remote-bearer",
        ).call("google.accounts.list", {})

    assert result[0].ref == "google-1"
    assert capture["sni"] == "localhost"
    assert capture["request_line"] == "GET /admin/v1/google/accounts HTTP/1.1"
    headers = capture["headers"]
    assert headers["Host"] == f"localhost:{port}"
    assert int(headers["Content-Length"]) == len(capture["body"])


def test_real_https_rejects_certificate_hostname_mismatch(tmp_path, monkeypatch):
    original_default_context = ssl.create_default_context
    with real_tls_server(tmp_path) as (port, certificate, _capture):
        monkeypatch.setattr(
            client_module,
            "_open_https_connection",
            _REAL_OPEN_HTTPS_CONNECTION,
        )
        monkeypatch.setattr(
            client_module.ssl,
            "create_default_context",
            lambda: original_default_context(cafile=certificate),
        )
        connection = MasterjetConnection(
            transport="https",
            endpoint=f"https://127.0.0.1:{port}/control",
            timeout_seconds=2,
        )

        with pytest.raises(MasterjetClientError, match=r"control\.transport_unavailable"):
            MasterjetControlClient(
                connection,
                bearer_provider=lambda: "remote-bearer",
            ).call("google.accounts.list", {})


def test_provider_exception_is_not_retained_as_client_error_context(monkeypatch):
    def bearer():
        raise ValueError("sk-private")

    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.authentication_required") as caught:
        https_client(bearer_provider=bearer).call("google.accounts.list", {})

    assert caught.value.__context__ is None
    assert "sk-private" not in repr(caught.value)


def test_header_encoding_exception_is_not_retained_as_client_error_context(monkeypatch):
    private = "private-☃"
    FakeHTTPSConnection.error = UnicodeEncodeError(
        "latin-1",
        private,
        len(private) - 1,
        len(private),
        "ordinal not in range",
    )
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.transport_unavailable") as caught:
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )

    assert caught.value.__context__ is None
    assert private not in repr(caught.value)


def test_malformed_secret_response_is_not_retained_as_error_context(monkeypatch):
    private = "sk-private"
    FakeHTTPSConnection.response = FakeHTTPResponse(
        f'{{"access_token":"{private}"'.encode()
    )
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.response_invalid") as caught:
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )

    assert caught.value.__context__ is None
    assert private not in repr(caught.value)


def test_https_endpoint_rejects_embedded_nul_before_authentication():
    bearer_called = False

    def bearer():
        nonlocal bearer_called
        bearer_called = True
        return "remote-bearer"

    client = MasterjetControlClient(
        MasterjetConnection(
            transport="https",
            endpoint="https://masterjet.example.test/control\0hidden",
            timeout_seconds=2,
        ),
        bearer_provider=bearer,
    )

    with pytest.raises(MasterjetClientError, match=r"control\.endpoint_invalid"):
        client.call("google.accounts.list", {})

    assert bearer_called is False


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://masterjet bad.example/control",
        "https://masterjet\tbad.example/control",
        "https://masterjet.example/control\r\nignored",
        "https://mästerjet.example/control",
    ],
)
def test_https_endpoint_rejects_non_ascii_or_whitespace_before_authentication(endpoint):
    bearer_called = False

    def bearer():
        nonlocal bearer_called
        bearer_called = True
        return "remote-bearer"

    client = MasterjetControlClient(
        MasterjetConnection(
            transport="https",
            endpoint=endpoint,
            timeout_seconds=2,
        ),
        bearer_provider=bearer,
    )

    with pytest.raises(MasterjetClientError, match=r"control\.endpoint_invalid") as caught:
        client.call("google.accounts.list", {})

    assert bearer_called is False
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://[fe80::1%eth0]/control",
        "https://[fe80::1%25eth0]/control",
    ],
)
def test_https_endpoint_rejects_scoped_ipv6_before_authentication(endpoint):
    bearer_called = False

    def bearer():
        nonlocal bearer_called
        bearer_called = True
        return "remote-bearer"

    client = MasterjetControlClient(
        MasterjetConnection(
            transport="https",
            endpoint=endpoint,
            timeout_seconds=2,
        ),
        bearer_provider=bearer,
    )

    with pytest.raises(MasterjetClientError, match=r"control\.endpoint_invalid"):
        client.call("google.accounts.list", {})

    assert bearer_called is False


def test_https_endpoint_rejects_port_zero_before_authentication():
    bearer_called = False

    def bearer():
        nonlocal bearer_called
        bearer_called = True
        return "remote-bearer"

    client = MasterjetControlClient(
        MasterjetConnection(
            transport="https",
            endpoint="https://masterjet.example:0/control",
            timeout_seconds=2,
        ),
        bearer_provider=bearer,
    )

    with pytest.raises(MasterjetClientError, match=r"control\.endpoint_invalid"):
        client.call("google.accounts.list", {})

    assert bearer_called is False


def test_https_connection_constructor_failure_is_sanitized(monkeypatch):
    private_host = "private-host"

    class FailingConnection:
        def __init__(self, *_args, **_kwargs):
            raise http.client.InvalidURL(private_host)

    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FailingConnection)
    monkeypatch.setattr(
        client_module,
        "_open_https_connection",
        _REAL_OPEN_HTTPS_CONNECTION,
    )

    with pytest.raises(MasterjetClientError, match=r"control\.endpoint_invalid") as caught:
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )

    assert caught.value.__context__ is None
    assert private_host not in repr(caught.value)


def test_https_name_resolution_obeys_total_deadline(monkeypatch):
    monkeypatch.setattr(client_module, "_RESOLVER_WORKER", blocking_resolver_worker, raising=False)
    monkeypatch.setattr(
        client_module,
        "_open_https_connection",
        _REAL_OPEN_HTTPS_CONNECTION,
    )
    connection = MasterjetConnection(
        transport="https",
        endpoint="https://localhost:9/control",
        timeout_seconds=1,
    )
    client = MasterjetControlClient(
        connection,
        bearer_provider=lambda: "remote-bearer",
    )
    children_before = {child.pid for child in multiprocessing.active_children()}

    started = time.monotonic()
    with pytest.raises(MasterjetClientError, match=r"control\.timeout"):
        client.call("google.accounts.list", {})
    elapsed = time.monotonic() - started

    assert elapsed < 1.6
    assert {child.pid for child in multiprocessing.active_children()} == children_before


def test_malformed_resolver_result_is_sanitized(monkeypatch):
    monkeypatch.setattr(client_module, "_RESOLVER_WORKER", malformed_resolver_worker)
    monkeypatch.setattr(
        client_module,
        "_open_https_connection",
        _REAL_OPEN_HTTPS_CONNECTION,
    )
    connection = MasterjetConnection(
        transport="https",
        endpoint="https://localhost:9/control",
        timeout_seconds=2,
    )

    with pytest.raises(
        MasterjetClientError,
        match=r"control\.transport_unavailable",
    ) as caught:
        MasterjetControlClient(
            connection,
            bearer_provider=lambda: "remote-bearer",
        ).call("google.accounts.list", {})

    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "worker",
    [
        hostname_resolver_worker,
        wrong_port_resolver_worker,
        wrong_family_resolver_worker,
        wrong_shape_resolver_worker,
        wrong_protocol_resolver_worker,
    ],
)
def test_resolver_result_must_match_numeric_tcp_sockaddr(monkeypatch, worker):
    monkeypatch.setattr(client_module, "_RESOLVER_WORKER", worker)
    monkeypatch.setattr(
        client_module,
        "_open_https_connection",
        _REAL_OPEN_HTTPS_CONNECTION,
    )
    monkeypatch.setattr(
        client_module.socket,
        "socket",
        lambda *_args, **_kwargs: ExplodingConnectSocket(),
    )
    connection = MasterjetConnection(
        transport="https",
        endpoint="https://localhost:9/control",
        timeout_seconds=2,
    )

    with pytest.raises(
        MasterjetClientError,
        match=r"control\.transport_unavailable",
    ) as caught:
        MasterjetControlClient(
            connection,
            bearer_provider=lambda: "remote-bearer",
        ).call("google.accounts.list", {})

    assert caught.value.__context__ is None


@pytest.mark.parametrize("control_exception", [KeyboardInterrupt, SystemExit])
def test_resolver_control_exception_propagates_after_process_reap(
    monkeypatch, control_exception
):
    events = []
    interrupt = control_exception("stop-now")
    receiver = ResolverPipe(
        events,
        poll_error=interrupt,
    )
    sender = ResolverPipe(events)
    process = ResolverProcess(events)
    context = ResolverContext(receiver, sender, process)
    monkeypatch.setattr(client_module.multiprocessing, "get_context", lambda _kind: context)
    monkeypatch.setattr(
        client_module,
        "_open_https_connection",
        _REAL_OPEN_HTTPS_CONNECTION,
    )

    with pytest.raises(control_exception) as caught:
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )

    assert caught.value is interrupt
    assert events.index("terminate") < events.index("kill") < events.index("process.close")


def test_resolver_worker_exception_is_contained_and_pipe_is_closed(monkeypatch):
    sender = ResolverWorkerSender()

    def fail_resolution(*_args, **_kwargs):
        raise ValueError("private-resolver-state")

    monkeypatch.setattr(client_module.socket, "getaddrinfo", fail_resolution)

    client_module._resolve_worker("masterjet.example.test", 443, sender)

    assert sender.messages == [(False, ())]
    assert sender.closed is True


@pytest.mark.parametrize("control_exception", [KeyboardInterrupt, SystemExit])
def test_resolver_worker_control_exception_propagates(monkeypatch, control_exception):
    sender = ResolverWorkerSender()
    interrupt = control_exception("stop-now")

    def fail_resolution(*_args, **_kwargs):
        raise interrupt

    monkeypatch.setattr(client_module.socket, "getaddrinfo", fail_resolution)

    with pytest.raises(control_exception) as caught:
        client_module._resolve_worker("masterjet.example.test", 443, sender)

    assert caught.value is interrupt
    assert sender.closed is True


def test_resolver_pipe_cleanup_error_does_not_skip_process_reap(monkeypatch):
    events = []
    receiver = ResolverPipe(
        events,
        close_error=ValueError("private-resolver-state"),
    )
    sender = ResolverPipe(events)
    process = ResolverProcess(events)
    context = ResolverContext(receiver, sender, process)
    monkeypatch.setattr(client_module.multiprocessing, "get_context", lambda _kind: context)
    monkeypatch.setattr(
        client_module,
        "_open_https_connection",
        _REAL_OPEN_HTTPS_CONNECTION,
    )

    with pytest.raises(MasterjetClientError, match=r"control\.timeout") as caught:
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )

    assert caught.value.__context__ is None
    assert events.index("kill") < events.index("process.close")


@pytest.mark.parametrize("failing_method", ["join", "terminate", "kill", "close"])
def test_resolver_process_cleanup_errors_are_sanitized_after_kill(
    monkeypatch,
    failing_method,
):
    events = []
    receiver = ResolverPipe(
        events,
        poll_error=ValueError("private-resolver-state"),
    )
    sender = ResolverPipe(events)
    process = ResolverProcess(events, failing_method=failing_method)
    context = ResolverContext(receiver, sender, process)
    monkeypatch.setattr(client_module.multiprocessing, "get_context", lambda _kind: context)
    monkeypatch.setattr(
        client_module,
        "_open_https_connection",
        _REAL_OPEN_HTTPS_CONNECTION,
    )

    with pytest.raises(
        MasterjetClientError,
        match=r"control\.transport_unavailable",
    ) as caught:
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )

    assert caught.value.__context__ is None
    assert process.alive is False


def test_resolver_alive_after_kill_is_reaped_later_without_blocking_call(monkeypatch):
    events = []
    receiver = ResolverPipe(events)
    sender = ResolverPipe(events)
    process = DelayedExitResolverProcess(events)
    context = ResolverContext(receiver, sender, process)
    monkeypatch.setattr(client_module.multiprocessing, "get_context", lambda _kind: context)
    monkeypatch.setattr(
        client_module,
        "_open_https_connection",
        _REAL_OPEN_HTTPS_CONNECTION,
    )

    started = time.monotonic()
    with pytest.raises(MasterjetClientError, match=r"control\.timeout"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )
    elapsed = time.monotonic() - started

    assert elapsed < 0.6
    assert process.alive is True
    process.allow_exit.set()
    assert process.reaped.wait(2)
    assert process.alive is False
    assert "process.close" in events


def test_real_resolver_process_that_outlives_kill_window_is_reaped_later(monkeypatch):
    children_before = {child.pid for child in multiprocessing.active_children()}
    real_process = multiprocessing.get_context("spawn").Process(
        target=time.sleep,
        args=(0.5,),
        daemon=True,
    )
    process = DelayedRealProcess(real_process)
    events = []
    context = ResolverContext(ResolverPipe(events), ResolverPipe(events), process)
    monkeypatch.setattr(client_module.multiprocessing, "get_context", lambda _kind: context)
    monkeypatch.setattr(
        client_module,
        "_open_https_connection",
        _REAL_OPEN_HTTPS_CONNECTION,
    )

    with pytest.raises(MasterjetClientError, match=r"control\.timeout"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )

    assert process.reaped.wait(3)
    assert {child.pid for child in multiprocessing.active_children()} == children_before


def test_tls_socket_is_closed_when_post_wrap_timeout_setup_fails(monkeypatch):
    raw_socket = OwnedRawSocket()
    tls_socket = OwnedTLSSocket(fail_timeout=True)
    tls_context = OwnershipTLSContext(tls_socket)
    monkeypatch.setattr(
        client_module,
        "_open_https_connection",
        _REAL_OPEN_HTTPS_CONNECTION,
    )
    monkeypatch.setattr(client_module.ssl, "create_default_context", lambda: tls_context)
    monkeypatch.setattr(
        client_module,
        "_resolve_host",
        lambda *_args: [(2, 1, 6, "", ("127.0.0.1", 8443))],
    )
    monkeypatch.setattr(client_module.socket, "socket", lambda *_args: raw_socket)

    with pytest.raises(
        MasterjetClientError,
        match=r"control\.transport_unavailable",
    ) as caught:
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )

    assert caught.value.__context__ is None
    assert tls_socket.close_calls == 1


def test_tls_socket_is_closed_when_connection_ownership_transfer_fails(monkeypatch):
    raw_socket = OwnedRawSocket()
    tls_socket = OwnedTLSSocket(fail_timeout=False)
    tls_context = OwnershipTLSContext(tls_socket)
    monkeypatch.setattr(
        client_module,
        "_open_https_connection",
        _REAL_OPEN_HTTPS_CONNECTION,
    )
    monkeypatch.setattr(client_module.ssl, "create_default_context", lambda: tls_context)
    monkeypatch.setattr(
        client_module,
        "_resolve_host",
        lambda *_args: [(2, 1, 6, "", ("127.0.0.1", 8443))],
    )
    monkeypatch.setattr(client_module.socket, "socket", lambda *_args: raw_socket)
    monkeypatch.setattr(
        client_module.http.client,
        "HTTPSConnection",
        FailingSocketAssignmentConnection,
    )

    with pytest.raises(
        MasterjetClientError,
        match=r"control\.transport_unavailable",
    ) as caught:
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )

    assert caught.value.__context__ is None
    assert tls_socket.close_calls == 1


def test_local_endpoint_rejects_group_traversable_parent(tmp_path):
    socket_path = tmp_path / "masterjet.sock"
    try:
        with unix_server(socket_path, json.dumps(google_accounts_payload()).encode()) as capture:
            tmp_path.chmod(0o750)
            with pytest.raises(MasterjetClientError, match=r"control\.endpoint_invalid"):
                local_client(socket_path).call("google.accounts.list", {})
        assert capture["accepted"] is False
    finally:
        tmp_path.chmod(0o700)


def test_real_https_slow_headers_cannot_extend_end_to_end_deadline(tmp_path, monkeypatch):
    original_default_context = ssl.create_default_context
    with real_tls_server(tmp_path, wire_delay=0.05) as (port, certificate, _capture):
        monkeypatch.setattr(
            client_module,
            "_open_https_connection",
            _REAL_OPEN_HTTPS_CONNECTION,
        )
        monkeypatch.setattr(
            client_module.ssl,
            "create_default_context",
            lambda: original_default_context(cafile=certificate),
        )
        connection = MasterjetConnection(
            transport="https",
            endpoint=f"https://localhost:{port}/control",
            timeout_seconds=1,
        )

        started = time.monotonic()
        with pytest.raises(MasterjetClientError, match=r"control\.timeout"):
            MasterjetControlClient(
                connection,
                bearer_provider=lambda: "remote-bearer",
            ).call("google.accounts.list", {})
        elapsed = time.monotonic() - started

    assert elapsed < 1.6


def test_https_problem_uses_canonical_local_template(monkeypatch):
    problem = {
        "schema_version": 1,
        "code": "authority.scope_denied",
        "severity": "error",
        "title": "Authority scope denied",
        "detail": "Authority scope is denied.",
        "effect": "Operation is denied.",
        "action": "Request required scope.",
        "retryable": False,
        "retry_after_seconds": None,
        "correlation_id": "correlation-1",
        "occurred_at": "2026-08-28T12:00:00Z",
    }
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps(problem).encode(),
        status=403,
    )
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"authority\.scope_denied") as caught:
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "google.accounts.list", {}
        )

    assert caught.value.problem is not None
    assert caught.value.problem.detail == "Authority scope is denied."


def test_attestation_exception_is_not_retained_as_client_error_context(tmp_path):
    socket_path = tmp_path / "masterjet.sock"

    def attest(_pid, _uid, _gid, _socket):
        raise ValueError("sk-private")

    with unix_server(socket_path, json.dumps(google_accounts_payload()).encode()):
        with pytest.raises(
            MasterjetClientError,
            match=r"control\.attestation_required",
        ) as caught:
            local_client(
                socket_path,
                local_attestation_verifier=attest,
                step_up_provider=lambda: "123456",
            ).put_secret(
                "ingress-1",
                b"private",
                expected_generation=4,
                idempotency_key="idem-1",
            )

    assert caught.value.__context__ is None
    assert "sk-private" not in repr(caught.value)


def test_invalid_https_port_is_not_retained_as_client_error_context():
    client = MasterjetControlClient(
        MasterjetConnection(
            transport="https",
            endpoint="https://masterjet.example.test:sk-private/control",
            timeout_seconds=2,
        ),
        bearer_provider=lambda: "remote-bearer",
    )

    with pytest.raises(MasterjetClientError, match=r"control\.endpoint_invalid") as caught:
        client.call("google.accounts.list", {})

    assert caught.value.__context__ is None
    assert "sk-private" not in repr(caught.value)


def test_full_plan_preview_dispatches_to_typed_redacted_contract(monkeypatch):
    payload = {
        "schema_version": 1,
        "id": "plan-1",
        "kind": "google.provision.plan",
        "state": "planned",
        "account_ref": "google-1",
        "expected_generation": 4,
        "resulting_generation": None,
        "plan_digest": "sha256:" + "a" * 64,
        "created_at": "2026-08-28T12:00:00Z",
        "expires_at": "2026-08-28T12:05:00Z",
        "completed_count": 0,
        "failed_count": 0,
        "not_attempted_count": 2,
        "reason_codes": [],
        "step_count": 4,
        "projects": [
            {"project_name": "Amber Orchard", "key_name": "Willow Meadow"},
            {"project_name": "Velvet Harbor", "key_name": "Silver Forest"},
        ],
    }
    FakeHTTPSConnection.response = FakeHTTPResponse(json.dumps(payload).encode())
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    result = https_client(bearer_provider=lambda: "remote-bearer").call(
        "google.provision.plan",
        {"account_ref": "google-1"},
        expected_generation=4,
        idempotency_key="idem-1",
    )

    assert result.account_ref == "google-1"
    assert result.step_count == 4
    assert [(row.project_name, row.key_name) for row in result.projects] == [
        ("Amber Orchard", "Willow Meadow"),
        ("Velvet Harbor", "Silver Forest"),
    ]


def test_task9_selfcheck_covers_both_transports_and_synthetic_auth(tmp_path, monkeypatch):
    result = _task9_transport_and_auth_selfcheck(tmp_path, monkeypatch)

    assert result.get("https_server_request_lines") == [
        "GET /admin/v1/google/accounts HTTP/1.1",
        "GET /admin/v1/openai/accounts HTTP/1.1",
        "POST /admin/v1/openai/accounts/openai-remote/auth-sync-plans HTTP/1.1",
        "POST /admin/v1/secret-ingress-sessions HTTP/1.1",
        "PUT /admin/v1/secret-ingress-sessions/ingress-1 HTTP/1.1",
        "POST /admin/v1/openai/accounts/openai-remote/auth-sync-plans/"
        + "sha256%3A"
        + "a" * 64
        + "/apply HTTP/1.1",
    ]
    assert result["local_google"] == result["https_google"]
    assert result["google_generation"] == 4
    assert result["auth_sync"] == ("openai-remote", 5, "succeeded")
    assert result["secret_ingress_content_type"] == "application/octet-stream"
    assert result["secret_bytes"] not in result["json_requests"]
    assert result["secret_bytes"] not in result["headers"]
    assert result["secret_bytes"] not in result["environment"]
