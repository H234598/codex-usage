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
from pathlib import Path
from typing import ClassVar

import pytest

import codex_usage.masterjet_client as client_module
from codex_usage.config import MasterjetConnection
from codex_usage.masterjet_client import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_SECRET_BYTES,
    MasterjetClientError,
    MasterjetControlClient,
)


def google_accounts_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
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
                "reload_state": "current",
            }
        ],
    }


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


def blocking_resolver_worker(_host: str, _port: int, _sender: object) -> None:
    time.sleep(60)


def malformed_resolver_worker(_host: str, _port: int, sender: object) -> None:
    sender.send(None)
    sender.close()


def test_local_and_https_decode_same_projection(tmp_path, monkeypatch):
    encoded_response = json.dumps(google_accounts_payload()).encode()
    socket_path = tmp_path / "masterjet.sock"
    FakeHTTPSConnection.response = FakeHTTPResponse(encoded_response)
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with unix_server(socket_path, encoded_response):
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
    response = json.dumps(
        {
            "schema_version": 1,
            "id": "operation-1",
            "kind": "secret.ingress.put",
            "state": "succeeded",
            "expected_generation": 4,
            "resulting_generation": 5,
            "plan_digest": "sha256:" + "a" * 64,
            "created_at": "2026-08-28T12:00:00Z",
            "expires_at": "2026-08-28T12:01:00Z",
            "completed_count": 1,
            "failed_count": 0,
            "not_attempted_count": 0,
            "reason_codes": [],
        }
    ).encode()

    peer: list[tuple[int, int, int, socket.socket]] = []

    def attest(pid, uid, gid, connected_socket):
        peer.append((pid, uid, gid, connected_socket))
        return True

    with unix_server(socket_path, response) as capture:
        result = local_client(
            socket_path,
            local_attestation_verifier=attest,
            step_up_provider=lambda: "123456",
        ).call(
            "secret.ingress.put",
            secret,
            expected_generation=4,
            idempotency_key="idem-1",
        )

    assert result.state == "succeeded"
    assert peer[0][:3] == (os.getpid(), os.geteuid(), os.getegid())
    assert isinstance(peer[0][3], socket.socket)
    assert bytes(secret) not in capture["request"]
    assert capture["secrets"] == [bytes(secret), b"123456"]
    request = json.loads(capture["request"])
    assert request["arguments"] == {
        "secret_fd": 0,
        "secret_size": len(secret),
        "step_up_fd": 1,
        "step_up_size": 6,
    }


def test_https_secret_uses_bounded_raw_body_not_json(monkeypatch):
    secret = bytearray(b"oauth-client-json-private")
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps(
            {
                "schema_version": 1,
                "id": "operation-1",
                "kind": "secret.ingress.put",
                "state": "succeeded",
                "expected_generation": 4,
                "resulting_generation": 5,
                "plan_digest": "sha256:" + "a" * 64,
                "created_at": "2026-08-28T12:00:00Z",
                "expires_at": "2026-08-28T12:01:00Z",
                "completed_count": 1,
                "failed_count": 0,
                "not_attempted_count": 0,
                "reason_codes": [],
            }
        ).encode()
    )
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    https_client(
        bearer_provider=lambda: "remote-bearer",
        step_up_provider=lambda: "123456",
    ).call(
        "secret.ingress.put",
        secret,
        expected_generation=4,
        idempotency_key="idem-1",
    )

    method, target, body, headers = FakeHTTPSConnection.instances[0].requests[0]
    assert (method, target, body) == ("POST", "/control", bytes(secret))
    assert headers["Content-Type"] == "application/octet-stream"
    assert bytes(secret) not in target.encode()
    assert all(bytes(secret) not in value.encode() for value in headers.values())


def test_secret_larger_than_limit_is_rejected_before_connect(monkeypatch):
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_too_large"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "secret.ingress.put", b"x" * (MAX_SECRET_BYTES + 1)
        )

    assert FakeHTTPSConnection.instances == []


def test_secret_byte_subclass_is_rejected_before_connect(monkeypatch):
    class SecretBytes(bytes):
        pass

    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_invalid"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "secret.ingress.put",
            SecretBytes(b"private"),
        )

    assert FakeHTTPSConnection.instances == []


def test_https_uses_verified_tls_fixed_target_and_transient_auth_headers(monkeypatch):
    FakeHTTPSConnection.response = FakeHTTPResponse(
        json.dumps(
            {
                "schema_version": 1,
                "id": "operation-1",
                "kind": "google.oauth.begin",
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

    client = https_client(
        bearer_provider=lambda: "remote-bearer",
        step_up_provider=lambda: "123456",
    )
    client.call(
        "google.oauth.begin",
        {"account_ref": "google-1", "browser": "firefox"},
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
    assert (method, target) == ("POST", "/control")
    assert json.loads(body)["operation"] == "google.oauth.begin"
    assert headers["Authorization"] == "Bearer remote-bearer"
    assert headers["X-Masterjet-Step-Up"] == "123456"
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

    with unix_server(socket_path, json.dumps(problem).encode()):
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


def test_local_sensitive_json_operation_requires_step_up(tmp_path):
    socket_path = tmp_path / "masterjet.sock"
    response = json.dumps(google_accounts_payload()).encode()

    with unix_server(socket_path, response) as capture:
        with pytest.raises(MasterjetClientError, match=r"control\.step_up_required"):
            local_client(
                socket_path,
                local_attestation_verifier=lambda _pid, _uid, _gid, _socket: True,
            ).call(
                "google.oauth.begin",
                {"account_ref": "google-1", "browser": "firefox"},
                expected_generation=4,
                idempotency_key="idem-1",
            )

    assert capture.get("secrets", []) == []


def test_local_sensitive_json_operation_sends_step_up_only_by_fd(tmp_path):
    socket_path = tmp_path / "masterjet.sock"
    response = json.dumps(
        {
            "schema_version": 1,
            "id": "operation-1",
            "kind": "google.oauth.begin",
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

    with unix_server(socket_path, response) as capture:
        local_client(
            socket_path,
            local_attestation_verifier=lambda _pid, _uid, _gid, _socket: True,
            step_up_provider=lambda: "123456",
        ).call(
            "google.oauth.begin",
            {"account_ref": "google-1", "browser": "firefox"},
            expected_generation=4,
            idempotency_key="idem-1",
        )

    request = json.loads(capture["request"])
    assert request["arguments"] == {
        "account_ref": "google-1",
        "browser": "firefox",
        "step_up_fd": 0,
        "step_up_size": 6,
    }
    assert capture["secrets"] == [b"123456"]


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
        {"account_ref": "google-1", "plan_id": "plan-1"},
        expected_generation=4,
        idempotency_key="idem-1",
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
            local_client(socket_path, step_up_provider=step_up).call(
                "secret.ingress.put", b"private"
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
            ).call("secret.ingress.put", b"private")

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

    with pytest.raises(MasterjetClientError, match=r"control\.step_up_required"):
        https_client(
            bearer_provider=lambda: "remote-bearer",
            step_up_provider=lambda: value,
        ).call(
            "google.oauth.begin",
            {"account_ref": "google-1", "browser": "firefox"},
            expected_generation=4,
            idempotency_key="idem-1",
        )

    assert FakeHTTPSConnection.instances == []


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
            MasterjetControlClient(connection).call("google.accounts.list", {})

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
            ).call("secret.ingress.put", b"private")
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
def real_tls_server(tmp_path, *, wire_delay: float = 0):
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
    listener.listen(1)
    listener.settimeout(3)
    port = listener.getsockname()[1]
    finished = threading.Event()

    def serve():
        try:
            connection, _ = listener.accept()
            try:
                with server_context.wrap_socket(connection, server_side=True) as tls_socket:
                    request = bytearray()
                    while b"\r\n\r\n" not in request:
                        request.extend(tls_socket.recv(4096))
                    head, body = request.split(b"\r\n\r\n", 1)
                    lines = head.decode("ascii").split("\r\n")
                    headers = dict(line.split(": ", 1) for line in lines[1:])
                    length = int(headers["Content-Length"])
                    while len(body) < length:
                        body.extend(tls_socket.recv(length - len(body)))
                    capture["request_line"] = lines[0]
                    capture["headers"] = headers
                    capture["body"] = bytes(body)
                    response = json.dumps(google_accounts_payload()).encode()
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
        connection = MasterjetConnection(
            transport="https",
            endpoint=f"https://localhost:{port}/control",
            timeout_seconds=2,
        )

        result = MasterjetControlClient(
            connection,
            bearer_provider=lambda: "remote-bearer",
        ).call("google.accounts.list", {})

    assert result[0].ref == "google-1"
    assert capture["sni"] == "localhost"
    assert capture["request_line"] == "POST /control HTTP/1.1"
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


def test_second_secret_fd_failure_closes_first_fd(tmp_path, monkeypatch):
    socket_path = tmp_path / "masterjet.sock"
    response = json.dumps(google_accounts_payload()).encode()
    real_anonymous_secret = client_module._anonymous_secret
    opened = []

    def fail_second(secret):
        if opened:
            raise OSError("fixture failure")
        item = real_anonymous_secret(secret)
        opened.append(item)
        return item

    monkeypatch.setattr(client_module, "_anonymous_secret", fail_second)

    with unix_server(socket_path, response):
        with pytest.raises(MasterjetClientError, match=r"control\.transport_unavailable"):
            local_client(
                socket_path,
                local_attestation_verifier=lambda _pid, _uid, _gid, _socket: True,
                step_up_provider=lambda: "123456",
            ).call("secret.ingress.put", b"private")

    assert len(opened) == 1
    assert opened[0].closed is True


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
            ).call("secret.ingress.put", b"private")

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
