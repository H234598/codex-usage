from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from codex_master_test_source import codex_master_test_source

import codex_usage.masterjet_client as client_module
from codex_usage.config import MasterjetConnection
from codex_usage.masterjet_client import (
    MasterjetClientError,
    MasterjetControlClient,
    _encode_request,
    _UnixTransport,
)
from codex_usage.masterjet_credentials import (
    MASTERJET_ATTESTATION_CREDENTIAL,
    local_attestation_verifier_from_systemd_credentials,
)

with codex_master_test_source(require_tests=True, module_level=True):
    from codex_master.admin_service import SecretIngressSessionV1
    from codex_master.admin_socket import AdminSocketServer, UnixPeerCredentials
    from test_admin_socket import PRINCIPAL, _Hosts, _SecretIngress, _service

PLAN_DIGEST = "sha256:" + "a" * 64


class _CreatingSecretIngress(_SecretIngress):
    def create_session(self, **_values: object) -> SecretIngressSessionV1:
        return SecretIngressSessionV1(
            "ingress-one",
            "openai-one",
            "pending",
            PLAN_DIGEST,
            0,
            1_777_463_500.0,
            0,
        )


def _credential_directory(tmp_path: Path, secret: bytes) -> tuple[Path, int]:
    directory = tmp_path / "credentials"
    directory.mkdir(mode=0o700)
    key = directory / MASTERJET_ATTESTATION_CREDENTIAL
    key.write_bytes(secret)
    key.chmod(0o400)
    return directory, os.open(key, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)


def _running_admin_socket(
    tmp_path: Path, key_fd: int, *, ingress: _SecretIngress | None = None
) -> AdminSocketServer:
    service = _service(ingress or _SecretIngress(), _Hosts())

    def authorize(peer: UnixPeerCredentials):
        assert (peer.pid, peer.uid, peer.gid) == (os.getpid(), os.getuid(), os.getgid())
        return PRINCIPAL

    server = AdminSocketServer(
        tmp_path / "socket" / "admin.sock",
        service,
        authorize,
        attestation_key_fd=key_fd,
    )
    server.start()
    return server


def _read_request(endpoint: Path, verifier):
    request, secret = _encode_request(
        "hosts.list",
        {},
        expected_generation=None,
        idempotency_key=None,
    )
    assert secret is None
    return _UnixTransport(
        MasterjetConnection("local", os.fspath(endpoint), 2),
        step_up_provider=None,
        attestation_verifier=verifier,
    ).request("hosts.list", request, None)


def test_real_admin_socket_read_uses_systemd_credential_mutual_hmac(tmp_path: Path) -> None:
    directory, key_fd = _credential_directory(tmp_path, b"k" * 32)
    server = _running_admin_socket(tmp_path, key_fd)
    try:
        verifier = local_attestation_verifier_from_systemd_credentials(
            environ={"CREDENTIALS_DIRECTORY": os.fspath(directory)}
        )
        status, body = _read_request(server.path, verifier)
    finally:
        server.close()
        os.close(key_fd)

    assert status == 200
    assert json.loads(body)["hosts"][0]["ref"] == "worker-one"


def test_real_admin_socket_wrong_attestation_key_fails_closed(tmp_path: Path) -> None:
    _directory, key_fd = _credential_directory(tmp_path, b"s" * 32)
    wrong = tmp_path / "wrong"
    wrong.mkdir(mode=0o700)
    wrong_key = wrong / MASTERJET_ATTESTATION_CREDENTIAL
    wrong_key.write_bytes(b"w" * 32)
    wrong_key.chmod(0o400)
    server = _running_admin_socket(tmp_path, key_fd)
    try:
        verifier = local_attestation_verifier_from_systemd_credentials(
            environ={"CREDENTIALS_DIRECTORY": os.fspath(wrong)}
        )
        with pytest.raises(MasterjetClientError, match=r"control\.attestation_required"):
            _read_request(server.path, verifier)
    finally:
        server.close()
        os.close(key_fd)


def test_missing_attestation_credential_fails_before_request_bytes(tmp_path: Path) -> None:
    directory, key_fd = _credential_directory(tmp_path, b"s" * 32)
    missing = tmp_path / "missing"
    missing.mkdir(mode=0o700)
    server = _running_admin_socket(tmp_path, key_fd)
    try:
        verifier = local_attestation_verifier_from_systemd_credentials(
            environ={"CREDENTIALS_DIRECTORY": os.fspath(missing)}
        )
        with pytest.raises(MasterjetClientError, match=r"control\.attestation_required"):
            _read_request(server.path, verifier)
    finally:
        server.close()
        os.close(key_fd)
    assert directory.exists()


def test_real_admin_socket_raw_put_sends_one_fd_after_attestation_and_wipes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, key_fd = _credential_directory(tmp_path, b"s" * 32)
    ingress = _SecretIngress()
    server = _running_admin_socket(tmp_path, key_fd, ingress=ingress)
    secret = bytearray(b"private-auth-json")
    original = client_module.tempfile.NamedTemporaryFile
    temporary_path = tmp_path / "secret-upload"

    def persistent_secret_file(*, mode: str):
        return original(mode=mode, dir=tmp_path, prefix="secret-upload", delete=False)

    monkeypatch.setattr(client_module.tempfile, "NamedTemporaryFile", persistent_secret_file)
    try:
        client = MasterjetControlClient(
            MasterjetConnection("local", os.fspath(server.path), 2),
            local_attestation_verifier=(
                local_attestation_verifier_from_systemd_credentials(
                    environ={"CREDENTIALS_DIRECTORY": os.fspath(directory)}
                )
            ),
        )
        receipt = client.put_secret(
            "ingress-one",
            secret,
            expected_generation=0,
            idempotency_key="idem-secret-put",
        )
        temporary_path = next(tmp_path.glob("secret-upload*"))
    finally:
        server.close()
        os.close(key_fd)
        secret[:] = b"\0" * len(secret)

    assert receipt.session_id == "ingress-one"
    assert ingress.put_calls == 1
    assert ingress.received == b"private-auth-json"
    assert temporary_path.read_bytes() == b"\0" * len(b"private-auth-json")


def test_real_admin_socket_regular_mutation_sends_no_secret_fd(tmp_path: Path) -> None:
    directory, key_fd = _credential_directory(tmp_path, b"s" * 32)
    server = _running_admin_socket(tmp_path, key_fd, ingress=_CreatingSecretIngress())
    try:
        client = MasterjetControlClient(
            MasterjetConnection("local", os.fspath(server.path), 2),
            local_attestation_verifier=(
                local_attestation_verifier_from_systemd_credentials(
                    environ={"CREDENTIALS_DIRECTORY": os.fspath(directory)}
                )
            ),
        )
        session = client.call(
            "secret.ingress.create",
            {
                "account_ref": "openai-one",
                "credential_kind": "openai.auth-json",
            },
            expected_generation=0,
            idempotency_key="idem-create",
            plan_digest=PLAN_DIGEST,
        )
    finally:
        server.close()
        os.close(key_fd)

    assert session.id == "ingress-one"
