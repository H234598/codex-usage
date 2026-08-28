from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from codex_usage.google_accounts import GoogleAccountsController, GoogleAccountsError
from codex_usage.masterjet_client import MasterjetClientError
from codex_usage.masterjet_contracts import (
    ControlOperation,
    GoogleControlAccount,
    GoogleOAuthTransactionV1,
    SecretIngressReceipt,
    SecretIngressSession,
)

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
REDIRECT_URI = "http://127.0.0.1:8765/oauth/callback"
AUTHORIZATION_URL = (
    "https://accounts.google.com/o/oauth2/v2/auth?"
    "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Foauth%2Fcallback"
)


def account(ref: str, generation: int) -> GoogleControlAccount:
    return GoogleControlAccount(
        ref=ref,
        label=ref.replace("-", " ").title(),
        enabled=True,
        subject_bound=True,
        oauth_state="ready",
        inventory_generation=generation,
        quota_state="fresh",
        project_count=0,
        billing_count=0,
        reload_state="ready",
    )


def operation(
    kind: str,
    *,
    operation_id: str = "plan-1",
    state: str = "planned",
    generation: int = 4,
    digest: str = DIGEST,
    expires_at: datetime = NOW + timedelta(minutes=5),
    resulting_generation: int | None = None,
) -> ControlOperation:
    return ControlOperation(
        id=operation_id,
        kind=kind,
        state=state,
        expected_generation=generation,
        resulting_generation=resulting_generation,
        plan_digest=digest,
        created_at=NOW - timedelta(minutes=1),
        expires_at=expires_at,
        completed_count=1 if state == "succeeded" else 0,
        failed_count=0,
        not_attempted_count=0 if state == "succeeded" else 1,
        reason_codes=(),
    )


class FakeControlClient:
    def __init__(self) -> None:
        self.accounts = (account("google-one", 4), account("google-two", 4))
        self.calls: list[tuple[str, object, int | None, str | None]] = []
        self.puts: list[tuple[str, bytes, int | None, str | None]] = []
        self.secret_views: list[bytearray] = []
        self.stored_plan = operation("google.provision.plan")
        self.stored_plan_account_ref = "google-one"
        self.authorization_url = AUTHORIZATION_URL

    def call(
        self,
        name: str,
        arguments: object,
        expected_generation: int | None = None,
        idempotency_key: str | None = None,
    ) -> object:
        self.calls.append((name, arguments, expected_generation, idempotency_key))
        if name == "google.accounts.list":
            return self.accounts
        if name == "google.oauth.begin":
            return GoogleOAuthTransactionV1(
                id="oauth-1",
                account_ref="google-one",
                authorization_url=self.authorization_url,
                expires_at=NOW + timedelta(minutes=5),
                generation=4,
            )
        if name == "google.oauth.complete":
            return operation(
                "google.oauth.complete",
                operation_id="oauth-complete-1",
                state="succeeded",
                resulting_generation=5,
            )
        if name == "google.inventory.refresh":
            return operation(
                "google.inventory.refresh",
                operation_id="refresh-1",
                state="succeeded",
                resulting_generation=5,
            )
        if name == "google.provision.plan":
            self.stored_plan = operation("google.provision.plan")
            self.stored_plan_account_ref = str(arguments["account_ref"])
            return self.stored_plan
        if name == "operations.get":
            if arguments != {
                "operation_id": self.stored_plan.id,
                "account_ref": self.stored_plan_account_ref,
            }:
                raise MasterjetClientError("control.plan_stale")
            return self.stored_plan
        if name == "google.provision.apply":
            return operation(
                "google.provision.apply",
                operation_id="apply-1",
                state="succeeded",
                resulting_generation=5,
            )
        if name == "google.oauth-client-import.plan":
            return operation("google.oauth-client-import.plan", operation_id="import-plan-1")
        if name == "secret.ingress.create":
            return SecretIngressSession(
                id="ingress-1",
                account_ref="google-one",
                plan_id="import-plan-1",
                expires_at=NOW + timedelta(minutes=2),
                expected_generation=4,
            )
        if name == "google.oauth-client-import.apply":
            return operation(
                "google.oauth-client-import.apply",
                operation_id="import-apply-1",
                state="succeeded",
                resulting_generation=5,
            )
        raise AssertionError(name)

    def put_secret(
        self,
        session_id: str,
        secret: bytearray,
        expected_generation: int | None = None,
        idempotency_key: str | None = None,
    ) -> object:
        self.secret_views.append(secret)
        self.puts.append((session_id, bytes(secret), expected_generation, idempotency_key))
        return SecretIngressReceipt(
            session_id=session_id,
            account_ref="google-one",
            state="consumed",
            generation=5,
        )


class FakeCallbackLease:
    def __init__(self, redirect_uri: str = REDIRECT_URI) -> None:
        self.redirect_uri = redirect_uri


class FakeCallbackProvider:
    def __init__(self, redirect_uri: str = REDIRECT_URI) -> None:
        self.lease = FakeCallbackLease(redirect_uri)
        self.acquired = False

    def acquire(self) -> FakeCallbackLease:
        self.acquired = True
        return self.lease


def controller(client: FakeControlClient, *, clock=lambda: NOW) -> GoogleAccountsController:
    return GoogleAccountsController(
        client,
        clock=clock,
        idempotency_key_factory=lambda: "idem-1",
        callback_provider=FakeCallbackProvider(),
    )


def test_list_keeps_google_accounts_separate() -> None:
    client = FakeControlClient()

    rows = controller(client).list()

    assert [row.ref for row in rows] == ["google-one", "google-two"]
    assert rows[0].inventory_generation == 4
    assert rows[1].inventory_generation == 4


def test_oauth_begin_and_complete_bind_account_generation_and_transaction() -> None:
    client = FakeControlClient()
    subject = controller(client)

    transaction = subject.oauth_begin("google-one", browser="firefox")
    completed = subject.oauth_complete(transaction)

    assert transaction.id == "oauth-1"
    assert transaction.account_ref == "google-one"
    assert completed.kind == "google.oauth.complete"
    assert client.calls[1] == (
        "google.oauth.begin",
        {
            "account_ref": "google-one",
            "browser": "firefox",
            "redirect_uri": REDIRECT_URI,
        },
        4,
        "idem-1",
    )
    assert client.calls[3] == (
        "google.oauth.complete",
        {"account_ref": "google-one", "transaction_id": "oauth-1"},
        4,
        "idem-1",
    )


def test_oauth_begin_without_bound_callback_provider_makes_no_request() -> None:
    client = FakeControlClient()
    subject = GoogleAccountsController(
        client, clock=lambda: NOW, idempotency_key_factory=lambda: "idem-1"
    )

    with pytest.raises(GoogleAccountsError, match=r"oauth\.callback_unavailable"):
        subject.oauth_begin("google-one", browser="firefox")

    assert client.calls == []


def test_oauth_begin_sends_exact_bound_redirect_from_acquired_lease() -> None:
    client = FakeControlClient()
    provider = FakeCallbackProvider()
    subject = GoogleAccountsController(
        client,
        clock=lambda: NOW,
        idempotency_key_factory=lambda: "idem-1",
        callback_provider=provider,
    )

    transaction = subject.oauth_begin("google-one", browser="firefox")

    assert transaction.authorization_url == AUTHORIZATION_URL
    assert client.calls[-1][1] == {
        "account_ref": "google-one",
        "browser": "firefox",
        "redirect_uri": REDIRECT_URI,
    }
    assert provider.acquired is True


@pytest.mark.parametrize(
    "authorization_url",
    [
        (
            "https://accounts.google.com/o/oauth2/v2/auth?"
            "redirect_uri=http%3A%2F%2F127.0.0.1%3A8766%2Foauth%2Fcallback"
        ),
        (
            "https://accounts.google.com/o/oauth2/v2/auth?"
            "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Fother"
        ),
        (
            "https://accounts.google.com/o/oauth2/v2/auth?"
            "redirect_uri=http%3A%2F%2F%5B%3A%3A1%5D%3A8765%2Foauth%2Fcallback"
        ),
    ],
)
def test_oauth_begin_rejects_callback_host_port_or_path_mismatch(
    authorization_url,
) -> None:
    client = FakeControlClient()
    client.authorization_url = authorization_url

    with pytest.raises(GoogleAccountsError, match=r"control\.response_invalid"):
        controller(client).oauth_begin("google-one", browser="firefox")

    assert not any(call[0] == "google.oauth.complete" for call in client.calls)


def test_provision_apply_reloads_plan_and_rejects_wrong_account_before_apply() -> None:
    client = FakeControlClient()
    subject = controller(client)
    plan = subject.provision_plan("google-one")
    restarted = controller(client)

    with pytest.raises(GoogleAccountsError, match=r"control\.plan_stale"):
        restarted.provision_apply(plan.plan_id, account_ref="google-two")

    assert [call[0] for call in client.calls].count("operations.get") == 1
    assert not any(call[0] == "google.provision.apply" for call in client.calls)


def test_provision_apply_reloads_and_binds_digest_after_restart() -> None:
    client = FakeControlClient()
    plan = controller(client).provision_plan("google-one")

    applied = controller(client).provision_apply(plan.plan_id, account_ref="google-one")

    assert applied.plan_digest == plan.plan_digest
    assert [call[0] for call in client.calls][-3:] == [
        "google.accounts.list",
        "operations.get",
        "google.provision.apply",
    ]
    assert client.calls[-2][1] == {
        "operation_id": "plan-1",
        "account_ref": "google-one",
    }
    assert client.calls[-1][1] == {"account_ref": "google-one", "plan_id": "plan-1"}
    assert client.calls[-1][2:] == (4, "idem-1")


def test_expired_reloaded_plan_never_reaches_apply() -> None:
    client = FakeControlClient()
    client.stored_plan = operation("google.provision.plan", expires_at=NOW - timedelta(seconds=1))

    with pytest.raises(GoogleAccountsError, match=r"control\.plan_stale"):
        controller(client).provision_apply("plan-1", account_ref="google-one")

    assert not any(call[0] == "google.provision.apply" for call in client.calls)


def test_inventory_refresh_uses_projection_generation_and_idempotency() -> None:
    client = FakeControlClient()

    result = controller(client).inventory_refresh("google-one")

    assert result.kind == "google.inventory.refresh"
    assert client.calls[-1] == (
        "google.inventory.refresh",
        {"account_ref": "google-one"},
        4,
        "idem-1",
    )


def test_oauth_client_import_is_plan_session_put_apply_and_zeroes_buffer(tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    source = root / "oauth-client.json"
    secret = b'{"client_secret":"never-project"}'
    source.write_bytes(secret)
    source.chmod(0o600)
    client = FakeControlClient()

    result = controller(client).import_oauth_client("google-one", source)

    assert result.status == "succeeded"
    assert client.puts == [("ingress-1", secret, 4, "idem-1")]
    assert bytes(client.secret_views[0]) == b"\0" * len(secret)
    names = [call[0] for call in client.calls]
    assert names == [
        "google.accounts.list",
        "google.oauth-client-import.plan",
        "secret.ingress.create",
        "google.oauth-client-import.apply",
    ]
    assert source.as_posix() not in repr(controller(client))
    assert secret.decode() not in repr(controller(client))


@pytest.mark.parametrize("state", ["partial", "failed", "blocked"])
def test_oauth_client_import_returns_terminal_receipt_failure_without_apply(
    tmp_path, state
) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    source = root / "oauth-client.json"
    source.write_text('{"client_secret":"private"}', encoding="utf-8")
    source.chmod(0o600)

    class TerminalReceiptClient(FakeControlClient):
        def put_secret(self, *args, **kwargs):
            receipt = super().put_secret(*args, **kwargs)
            return SecretIngressReceipt(
                session_id=receipt.session_id,
                account_ref=receipt.account_ref,
                state=state,
                generation=receipt.generation,
            )

    client = TerminalReceiptClient()

    result = controller(client).import_oauth_client("google-one", source)

    assert result.status == state
    assert not any(call[0] == "google.oauth-client-import.apply" for call in client.calls)


@pytest.mark.parametrize("state", ["partial", "failed", "blocked"])
def test_oauth_client_import_returns_terminal_apply_operation(tmp_path, state) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    source = root / "oauth-client.json"
    source.write_text('{"client_secret":"private"}', encoding="utf-8")
    source.chmod(0o600)

    class TerminalApplyClient(FakeControlClient):
        def call(self, name, arguments, expected_generation=None, idempotency_key=None):
            if name == "google.oauth-client-import.apply":
                self.calls.append((name, arguments, expected_generation, idempotency_key))
                return operation(
                    name,
                    operation_id="import-apply-1",
                    state=state,
                    resulting_generation=None,
                )
            return super().call(name, arguments, expected_generation, idempotency_key)

    result = controller(TerminalApplyClient()).import_oauth_client("google-one", source)

    assert result.status == state


def test_oauth_client_import_rejects_non_private_source_before_secret_put(tmp_path) -> None:
    root = tmp_path / "public"
    root.mkdir(mode=0o755)
    source = root / "oauth-client.json"
    source.write_text('{"client_secret":"private"}', encoding="utf-8")
    source.chmod(0o600)
    client = FakeControlClient()

    with pytest.raises(GoogleAccountsError, match=r"credential\.source_unavailable"):
        controller(client).import_oauth_client("google-one", source)

    assert client.puts == []


def test_oauth_client_import_zeroes_buffer_when_secret_put_fails(tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    source = root / "oauth-client.json"
    source.write_text('{"client_secret":"private"}', encoding="utf-8")
    source.chmod(0o600)

    class FailingPutClient(FakeControlClient):
        def put_secret(self, *args, **kwargs):
            super().put_secret(*args, **kwargs)
            raise GoogleAccountsError("credential.upload_expired")

    client = FailingPutClient()

    with pytest.raises(GoogleAccountsError, match=r"credential\.upload_expired"):
        controller(client).import_oauth_client("google-one", source)

    assert bytes(client.secret_views[0]) == b"\0" * source.stat().st_size
    assert not any(call[0] == "google.oauth-client-import.apply" for call in client.calls)


def test_oauth_client_import_rejects_hard_linked_source(tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    source = root / "oauth-client.json"
    source.write_text('{"client_secret":"private"}', encoding="utf-8")
    source.chmod(0o600)
    (root / "second-name.json").hardlink_to(source)
    client = FakeControlClient()

    with pytest.raises(GoogleAccountsError, match=r"credential\.source_unavailable"):
        controller(client).import_oauth_client("google-one", source)

    assert client.puts == []


def test_import_plan_expiring_after_read_causes_no_ingress_side_effect(tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    source = root / "oauth-client.json"
    source.write_text('{"client_secret":"private"}', encoding="utf-8")
    source.chmod(0o600)
    client = FakeControlClient()
    moments = iter((NOW, NOW + timedelta(minutes=10)))

    with pytest.raises(GoogleAccountsError, match=r"control\.plan_stale"):
        controller(client, clock=lambda: next(moments)).import_oauth_client("google-one", source)

    assert not any(call[0] == "secret.ingress.create" for call in client.calls)
    assert client.puts == []


def test_plan_expiry_is_rechecked_after_idempotency_key_before_apply() -> None:
    client = FakeControlClient()
    current = [NOW]

    def key() -> str:
        current[0] = NOW + timedelta(minutes=10)
        return "idem-1"

    subject = GoogleAccountsController(
        client, clock=lambda: current[0], idempotency_key_factory=key
    )

    with pytest.raises(GoogleAccountsError, match=r"control\.plan_stale"):
        subject.provision_apply("plan-1", account_ref="google-one")

    assert not any(call[0] == "google.provision.apply" for call in client.calls)


def test_oauth_expiry_is_rechecked_after_idempotency_key_before_complete() -> None:
    client = FakeControlClient()
    current = [NOW]
    transaction = GoogleOAuthTransactionV1(
        id="oauth-1",
        account_ref="google-one",
        authorization_url=AUTHORIZATION_URL,
        expires_at=NOW + timedelta(minutes=5),
        generation=4,
    )

    def key() -> str:
        current[0] = NOW + timedelta(minutes=10)
        return "idem-1"

    subject = GoogleAccountsController(
        client, clock=lambda: current[0], idempotency_key_factory=key
    )

    with pytest.raises(GoogleAccountsError, match=r"oauth\.transaction_expired"):
        subject.oauth_complete(transaction)

    assert not any(call[0] == "google.oauth.complete" for call in client.calls)
