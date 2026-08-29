from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from codex_usage.config import MasterjetConnection
from codex_usage.masterjet_client import (
    MasterjetClientError,
    _encode_request,
    _UnixTransport,
)
from codex_usage.masterjet_credentials import (
    MASTERJET_ATTESTATION_CREDENTIAL,
    local_attestation_verifier_from_systemd_credentials,
)

ADMIN_ROOT = Path("/home/teladi/.codex-worktrees/codex-master/admin-control-20260828")
sys.path.insert(0, str(ADMIN_ROOT / "src"))
sys.path.insert(0, str(ADMIN_ROOT / "tests"))

from codex_master.admin_socket import AdminSocketServer, UnixPeerCredentials  # noqa: E402
from test_admin_socket import PRINCIPAL, _Hosts, _SecretIngress, _service  # noqa: E402


def _credential_directory(tmp_path: Path, secret: bytes) -> tuple[Path, int]:
    directory = tmp_path / "credentials"
    directory.mkdir(mode=0o700)
    key = directory / MASTERJET_ATTESTATION_CREDENTIAL
    key.write_bytes(secret)
    key.chmod(0o400)
    return directory, os.open(key, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)


def _running_admin_socket(tmp_path: Path, key_fd: int) -> AdminSocketServer:
    service = _service(_SecretIngress(), _Hosts())

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
