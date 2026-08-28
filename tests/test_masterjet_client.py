from __future__ import annotations

import array
import json
import os
import socket
import ssl
import threading
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


@pytest.fixture(autouse=True)
def reset_fake_https() -> None:
    FakeHTTPSConnection.instances = []
    FakeHTTPSConnection.error = None
    FakeHTTPSConnection.response = FakeHTTPResponse(b"{}")


@contextmanager
def unix_server(
    socket_path: Path,
    response: bytes,
    *,
    append_newline: bool = True,
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
            ready.set()
            connection, _ = server.accept()
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
                connection.sendall(response + (b"\n" if append_newline else b""))
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
                client.call("fixture.large", {})
    else:
        FakeHTTPSConnection.response = FakeHTTPResponse(oversized)
        monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)
        with pytest.raises(MasterjetClientError, match=r"control\.response_too_large"):
            https_client(bearer_provider=lambda: "remote-bearer").call("fixture.large", {})


def test_request_larger_than_limit_is_rejected_before_https_connect(monkeypatch):
    monkeypatch.setattr(client_module.http.client, "HTTPSConnection", FakeHTTPSConnection)

    with pytest.raises(MasterjetClientError, match=r"control\.request_too_large"):
        https_client(bearer_provider=lambda: "remote-bearer").call(
            "fixture.echo", {"padding": "x" * MAX_REQUEST_BYTES}
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

    with unix_server(socket_path, response) as capture:
        result = local_client(socket_path).call(
            "secret.ingress.put",
            secret,
            expected_generation=4,
            idempotency_key="idem-1",
        )

    assert result.state == "succeeded"
    assert bytes(secret) not in capture["request"]
    assert capture["secrets"] == [bytes(secret)]
    request = json.loads(capture["request"])
    assert request["arguments"] == {"secret_fd": 0, "secret_size": len(secret)}


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


def test_https_uses_verified_tls_fixed_target_and_transient_auth_headers(monkeypatch):
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

    client = https_client(
        bearer_provider=lambda: "remote-bearer",
        step_up_provider=lambda: "123456",
    )
    client.call(
        "google.provision.apply",
        {"plan_id": "plan-1"},
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
    assert json.loads(body)["operation"] == "google.provision.apply"
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
            "fixture.echo",
            {"items": ({"access_token": "private"},)},
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
