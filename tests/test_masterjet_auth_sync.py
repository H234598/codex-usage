from __future__ import annotations

from datetime import UTC, datetime

import pytest

from codex_usage.masterjet_auth_sync import (
    MAX_AUTH_JSON_BYTES,
    AuthSyncError,
    sync_account_auth,
)
from codex_usage.masterjet_client import MasterjetClientError
from codex_usage.masterjet_contracts import (
    ControlOperation,
    SecretIngressReceipt,
    SecretIngressSession,
)
from codex_usage.models import Account


def operation(*, kind: str, state: str, resulting_generation: int | None) -> ControlOperation:
    return ControlOperation(
        id="plan-1" if state == "planned" else "apply-1",
        kind=kind,
        state=state,
        expected_generation=4,
        resulting_generation=resulting_generation,
        plan_digest="sha256:" + "a" * 64,
        created_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
        expires_at=datetime(2026, 8, 28, 12, 30, tzinfo=UTC),
        completed_count=1 if state == "succeeded" else 0,
        failed_count=0,
        not_attempted_count=0 if state == "succeeded" else 1,
        reason_codes=(),
    )


class AuthenticatedClient:
    def __init__(self, *, fail_put: bool = False) -> None:
        self.calls: list[tuple[str, object, int | None, str | None]] = []
        self.secret_views: list[bytearray] = []
        self.secret_at_put: bytes | None = None
        self.fail_put = fail_put

    def call(
        self,
        operation_name: str,
        arguments: object,
        expected_generation: int | None = None,
        idempotency_key: str | None = None,
    ) -> object:
        self.calls.append((operation_name, arguments, expected_generation, idempotency_key))
        if operation_name == "openai.auth-sync.plan":
            return operation(
                kind="openai.auth-sync.plan",
                state="planned",
                resulting_generation=None,
            )
        if operation_name == "secret.ingress.create":
            return SecretIngressSession(
                id="ingress-1",
                account_ref="openai-1",
                plan_id="plan-1",
                expires_at=datetime(2026, 8, 28, 12, 1, tzinfo=UTC),
                expected_generation=4,
            )
        assert operation_name == "openai.auth-sync.apply"
        return operation(
            kind="openai.auth-sync.apply",
            state="succeeded",
            resulting_generation=5,
        )

    def put_secret(
        self,
        session_id: str,
        secret: bytearray,
        expected_generation: int | None = None,
        idempotency_key: str | None = None,
    ) -> SecretIngressReceipt:
        assert session_id == "ingress-1"
        assert expected_generation == 4
        assert idempotency_key
        self.secret_views.append(secret)
        self.secret_at_put = bytes(secret)
        if self.fail_put:
            raise MasterjetClientError("credential.upload_expired")
        return SecretIngressReceipt(
            session_id="ingress-1",
            account_ref="openai-1",
            state="consumed",
            generation=5,
        )


def canonical_account(tmp_path, secret: bytes = b'{"tokens":"top-secret"}') -> Account:
    profile = tmp_path / "profile"
    codex_home = profile / "codex-home"
    codex_home.mkdir(parents=True, mode=0o700)
    codex_home.chmod(0o700)
    auth = codex_home / "auth.json"
    auth.write_bytes(secret)
    auth.chmod(0o600)
    return Account(
        id="openai-1",
        label="OpenAI",
        profile_dir=str(profile),
        auth_json_path=str(auth),
    )


def test_auth_sync_binds_canonical_source_session_plan_and_apply(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    account = canonical_account(tmp_path)
    client = AuthenticatedClient()

    result = sync_account_auth(account, client)

    assert result.account_ref == "openai-1"
    assert result.generation == 5
    assert result.status == "succeeded"
    assert client.secret_at_put == b'{"tokens":"top-secret"}'
    assert client.calls[0][0:3] == (
        "openai.auth-sync.plan",
        {"account_ref": "openai-1"},
        None,
    )
    assert client.calls[1][0:3] == (
        "secret.ingress.create",
        {
            "account_ref": "openai-1",
            "credential_type": "openai_auth_json",
            "plan_id": "plan-1",
        },
        4,
    )
    assert client.calls[2][0:3] == (
        "openai.auth-sync.apply",
        {"account_ref": "openai-1", "plan_id": "plan-1"},
        4,
    )
    assert all(client.calls[index][3] for index in range(3))
    assert all(byte == 0 for byte in client.secret_views[0])
    combined = repr(result) + repr(client.calls)
    assert "top-secret" not in combined
    assert str(account.auth_json_path) not in combined


@pytest.mark.parametrize("variant", ["alternate", "relative", "missing"])
def test_auth_sync_rejects_noncanonical_auth_path_before_remote_call(
    tmp_path, monkeypatch, variant
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    account = canonical_account(tmp_path)
    if variant == "alternate":
        account = Account(**(account.__dict__ | {"auth_json_path": str(tmp_path / "auth.json")}))
    elif variant == "relative":
        account = Account(**(account.__dict__ | {"auth_json_path": "profile/codex-home/auth.json"}))
    else:
        account = Account(**(account.__dict__ | {"auth_json_path": None}))
    client = AuthenticatedClient()

    with pytest.raises(AuthSyncError, match=r"credential\.source_unavailable") as caught:
        sync_account_auth(account, client)

    assert client.calls == []
    assert caught.value.__context__ is None


def test_auth_sync_rejects_unsafe_auth_file_and_does_not_touch_other_account(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    secret = b'{"tokens":"top-secret"}'
    account = canonical_account(tmp_path, secret)
    other = canonical_account(tmp_path / "other", b"other-secret")
    auth = tmp_path / "profile" / "codex-home" / "auth.json"
    auth.chmod(0o644)
    client = AuthenticatedClient()

    with pytest.raises(AuthSyncError, match=r"credential\.source_unavailable") as caught:
        sync_account_auth(account, client)

    assert client.calls[0][0] == "openai.auth-sync.plan"
    assert len(client.calls) == 1
    other_auth = tmp_path / "other" / "profile" / "codex-home" / "auth.json"
    assert other_auth.read_bytes() == b"other-secret"
    assert "top-secret" not in repr(caught.value)
    assert other.id == "openai-1"


@pytest.mark.parametrize("variant", ["symlink", "hardlink", "oversize", "empty"])
def test_auth_sync_private_source_boundaries_fail_closed(tmp_path, monkeypatch, variant):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    account = canonical_account(tmp_path)
    auth = tmp_path / "profile" / "codex-home" / "auth.json"
    if variant == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"private")
        target.chmod(0o600)
        auth.unlink()
        auth.symlink_to(target)
    elif variant == "hardlink":
        auth.with_name("second-link").hardlink_to(auth)
    elif variant == "oversize":
        auth.write_bytes(b"x" * (MAX_AUTH_JSON_BYTES + 1))
    else:
        auth.write_bytes(b"")
    client = AuthenticatedClient()

    with pytest.raises(AuthSyncError, match=r"credential\.source_unavailable"):
        sync_account_auth(account, client)

    assert [call[0] for call in client.calls] == ["openai.auth-sync.plan"]


def test_auth_sync_maps_put_failure_without_secret_or_exception_context(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    account = canonical_account(tmp_path)
    client = AuthenticatedClient(fail_put=True)

    with pytest.raises(AuthSyncError, match=r"credential\.upload_expired") as caught:
        sync_account_auth(account, client)

    assert caught.value.__context__ is None
    assert "top-secret" not in repr(caught.value)
    assert all(byte == 0 for byte in client.secret_views[0])


def test_auth_sync_sanitizes_unexpected_authenticated_client_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    account = canonical_account(tmp_path)

    class ExplodingClient(AuthenticatedClient):
        def call(self, *_args, **_kwargs):
            raise RuntimeError("top-secret")

    with pytest.raises(AuthSyncError, match=r"control\.transport_unavailable") as caught:
        sync_account_auth(account, ExplodingClient())

    assert caught.value.__context__ is None
    assert "top-secret" not in repr(caught.value)
