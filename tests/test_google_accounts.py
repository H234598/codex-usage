from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from codex_usage.google_accounts import GoogleAccountsController, GoogleAccountsError
from codex_usage.masterjet_client import MasterjetClientError
from codex_usage.masterjet_contracts import (
    ControlOperation,
    GoogleAccountAddReceiptV1,
    GoogleBillingPlanV1,
    GoogleBillingReceiptV1,
    GoogleControlAccount,
    GoogleControlAccountList,
    GoogleControlProject,
    GoogleControlProjectList,
    GoogleOAuthClientImportPlanV1,
    GoogleOAuthClientImportReceiptV1,
    GoogleOAuthReceipt,
    GoogleOAuthTransactionV1,
    GoogleProvisionPlanV1,
    GoogleProvisionProjectV1,
    SecretIngressReceipt,
    SecretIngressSession,
)

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
REDIRECT_URI = "http://127.0.0.1:8765/oauth/callback"
AUTHORIZATION_URL = (
    "https://accounts.google.com/o/oauth2/v2/auth?"
    "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Foauth%2Fcallback"
    "&state=state-one"
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
        default_oauth_client_ref="oauth-client-1",
        oauth_client_availability="available",
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


def provision_plan(
    *,
    generation: int = 4,
    expires_at: datetime = NOW + timedelta(minutes=5),
) -> GoogleProvisionPlanV1:
    return GoogleProvisionPlanV1(
        id="plan-1",
        kind="google.provision.plan",
        state="planned",
        account_ref="google-one",
        expected_generation=generation,
        resulting_generation=None,
        plan_digest=DIGEST,
        created_at=NOW - timedelta(minutes=1),
        expires_at=expires_at,
        completed_count=0,
        failed_count=0,
        not_attempted_count=1,
        reason_codes=(),
        step_count=1,
        projects=(GoogleProvisionProjectV1("Amber Orchard", "Willow Meadow"),),
    )


class FakeControlClient:
    def __init__(self) -> None:
        self.accounts = (account("google-one", 4), account("google-two", 4))
        self.calls: list[tuple[str, object, int | None, str | None]] = []
        self.plan_digests: list[str | None] = []
        self.puts: list[tuple[str, bytes, int | None, str | None]] = []
        self.secret_views: list[bytearray] = []
        self.stored_plan = provision_plan()
        self.stored_plan_account_ref = "google-one"
        self.authorization_url = AUTHORIZATION_URL
        self.oauth_expires_at = NOW + timedelta(minutes=5)

    def call(
        self,
        name: str,
        arguments: object,
        expected_generation: int | None = None,
        idempotency_key: str | None = None,
        plan_digest: str | None = None,
    ) -> object:
        self.calls.append((name, arguments, expected_generation, idempotency_key))
        self.plan_digests.append(plan_digest)
        if name == "google.accounts.list":
            return self.accounts
        if name == "google.accounts.add":
            return GoogleAccountAddReceiptV1(str(arguments["account_ref"]), 9)
        if name == "google.oauth.begin":
            return GoogleOAuthTransactionV1(
                id="oauth-1",
                account_ref="google-one",
                authorization_url=self.authorization_url,
                expires_at=self.oauth_expires_at.timestamp(),
                inventory_generation=4,
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
            self.stored_plan = provision_plan()
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
            return GoogleOAuthClientImportPlanV1(
                "import-plan-1",
                "google-one",
                4,
                (NOW + timedelta(minutes=5)).timestamp(),
                DIGEST,
            )
        if name == "secret.ingress.create":
            return SecretIngressSession(
                "ingress-1",
                "google-one",
                "pending",
                DIGEST,
                4,
                (NOW + timedelta(minutes=2)).timestamp(),
                4,
            )
        if name == "google.oauth-client-import.apply":
            return GoogleOAuthClientImportReceiptV1(
                "google-one",
                "client-one",
                "Quiet Client",
                4,
                DIGEST,
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


class BillingControlClient:
    def __init__(self) -> None:
        self.accounts = (
            replace(account("google-one", 4), project_count=2, billing_count=1),
            replace(account("google-two", 4), project_count=1, billing_count=1),
        )
        self.projects = {
            "google-one": (
                GoogleControlProject(
                    "project-one", "Amber Orchard", "quota_probe", "Willow Meadow", None,
                    "active", "ready", "fresh",
                ),
                GoogleControlProject(
                    "project-two", "Velvet Harbor", "quota_probe", "Silver Forest", "billing-one",
                    "active", "ready", "fresh",
                ),
            ),
            "google-two": (
                GoogleControlProject(
                    "project-foreign",
                    "Golden Meadow",
                    "quota_probe",
                    "Autumn Grove",
                    "billing-two",
                    "active", "ready", "fresh",
                ),
            ),
        }
        self.calls: list[tuple[str, object, int | None, str | None, str | None]] = []

    def call(
        self,
        name: str,
        arguments: object,
        expected_generation: int | None = None,
        idempotency_key: str | None = None,
        plan_digest: str | None = None,
    ) -> object:
        self.calls.append((name, arguments, expected_generation, idempotency_key, plan_digest))
        if name == "google.accounts.list":
            return self.accounts
        if name == "google.projects.list":
            account_ref = str(arguments["account_ref"])
            return GoogleControlProjectList(
                schema_version=1,
                account_ref=account_ref,
                inventory_generation=4,
                projects=self.projects[account_ref],
            )
        if name == "google.billing.plan":
            return GoogleBillingPlanV1(
                id="billing-plan-one",
                account_ref="google-one",
                inventory_generation=4,
                snapshot_fingerprint="b" * 64,
                project_ref="project-one",
                billing_ref="billing-one",
                plan_digest=DIGEST,
                created_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(minutes=5),
            )
        if name == "google.billing.apply":
            return GoogleBillingReceiptV1(
                plan_id="billing-plan-one",
                state="succeeded",
                attempted=1,
                completed=1,
                failed=0,
                not_attempted=0,
                reason_code="billing.binding_created",
            )
        raise AssertionError(name)


class AuthorizationClient(FakeControlClient):
    def __init__(self) -> None:
        super().__init__()

    def call(
        self,
        name: str,
        arguments: object,
        expected_generation: int | None = None,
        idempotency_key: str | None = None,
        plan_digest: str | None = None,
    ) -> object:
        self.calls.append((name, arguments, expected_generation, idempotency_key))
        self.plan_digests.append(plan_digest)
        if name == "google.accounts.list":
            return self.accounts
        if name == "google.oauth.begin":
            return GoogleOAuthTransactionV1(
                "oauth-1",
                "google-one",
                AUTHORIZATION_URL,
                (NOW + timedelta(minutes=5)).timestamp(),
                4,
            )
        if name == "secret.ingress.create":
            assert plan_digest is not None
            return SecretIngressSession(
                "ingress-1",
                "google-one",
                "pending",
                plan_digest,
                4,
                (NOW + timedelta(minutes=2)).timestamp(),
                4,
            )
        if name == "google.oauth.complete":
            return GoogleOAuthReceipt("google-one", True, True)
        raise AssertionError(name)


class FakeCallbackLease:
    def __init__(self, redirect_uri: str = REDIRECT_URI, *, close_failures: int = 0) -> None:
        self.redirect_uri = redirect_uri
        self.close_count = 0
        self.close_failures = close_failures

    def close(self) -> None:
        self.close_count += 1
        if self.close_count <= self.close_failures:
            raise OSError("close failed")


class AuthorizationCallbackLease(FakeCallbackLease):
    def __init__(self) -> None:
        super().__init__(REDIRECT_URI)
        self.code = bytearray(b"private-oauth-code")
        self.launch_uri = "http://127.0.0.1:8765/oauth/start/nonsecret"

    def prepare_authorization(self, authorization_url: str) -> None:
        assert authorization_url == AUTHORIZATION_URL

    def receive(self, *, expected_state: str, timeout_seconds: float) -> bytearray:
        assert expected_state == "state-one"
        assert timeout_seconds == pytest.approx(300.0)
        return self.code


class AuthorizationCallbackProvider:
    def __init__(self) -> None:
        self.lease = AuthorizationCallbackLease()

    def acquire(self) -> AuthorizationCallbackLease:
        return self.lease


class BrowserLease:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def controller(client: FakeControlClient, *, clock=lambda: NOW) -> GoogleAccountsController:
    return GoogleAccountsController(
        client,
        clock=clock,
        idempotency_key_factory=lambda: "idem-1",
    )


def test_fake_control_client_oauth_begin_uses_current_transaction_contract() -> None:
    transaction = FakeControlClient().call("google.oauth.begin", {})

    assert transaction == GoogleOAuthTransactionV1(
        id="oauth-1",
        account_ref="google-one",
        authorization_url=AUTHORIZATION_URL,
        expires_at=(NOW + timedelta(minutes=5)).timestamp(),
        inventory_generation=4,
    )


def test_list_keeps_google_accounts_separate() -> None:
    client = AuthorizationClient()

    rows = controller(client).list()

    assert [row.ref for row in rows] == ["google-one", "google-two"]
    assert rows[0].inventory_generation == 4
    assert rows[1].inventory_generation == 4


def test_register_account_uses_fresh_registry_generation_and_binds_receipt() -> None:
    client = FakeControlClient()
    client.accounts = GoogleControlAccountList(
        accounts=(account("google-one", 4),),
        registry_generation=8,
    )

    result = controller(client).register_account("google-two", "Google Two")

    assert result.account_ref == "google-two"
    assert result.generation == 9
    assert result.status == "succeeded"
    assert client.calls == [
        ("google.accounts.list", {}, None, None),
        (
            "google.accounts.add",
            {"account_ref": "google-two", "label": "Google Two"},
            8,
            "idem-1",
        ),
    ]


def test_register_account_fails_closed_without_registry_generation() -> None:
    client = FakeControlClient()

    with pytest.raises(GoogleAccountsError, match=r"control\.response_invalid"):
        controller(client).register_account("google-three", "Google Three")

    assert [call[0] for call in client.calls] == ["google.accounts.list"]


def test_register_account_rejects_unbound_resulting_generation() -> None:
    class WrongGenerationClient(FakeControlClient):
        def call(
            self,
            name,
            arguments,
            expected_generation=None,
            idempotency_key=None,
            plan_digest=None,
        ):
            if name == "google.accounts.list":
                self.calls.append((name, arguments, expected_generation, idempotency_key))
                self.plan_digests.append(plan_digest)
                return GoogleControlAccountList(accounts=(), registry_generation=8)
            if name == "google.accounts.add":
                self.calls.append((name, arguments, expected_generation, idempotency_key))
                self.plan_digests.append(plan_digest)
                return GoogleAccountAddReceiptV1("google-three", 10)
            return super().call(
                name, arguments, expected_generation, idempotency_key, plan_digest
            )

    with pytest.raises(GoogleAccountsError, match=r"control\.response_invalid"):
        controller(WrongGenerationClient()).register_account("google-three", "Google Three")


def test_account_details_cover_exact_accounts_generations_and_project_counts() -> None:
    client = FakeControlClient()
    client.accounts = (
        account("google-one", 4),
        account("google-two", 7),
    )
    client.accounts = (replace(client.accounts[0], project_count=1), client.accounts[1])
    project = GoogleControlProject(
        ref="hive-one",
        project_name="Amber Orchard",
        purpose="quota_probe",
        key_name="Willow Meadow",
        billing_ref=None,
        status="ready",
        probe_state="ready",
        quota_state="available",
    )
    original_call = client.call

    def call(name, arguments, expected_generation=None, idempotency_key=None):
        if name == "google.projects.list":
            ref = arguments["account_ref"]
            generation = 4 if ref == "google-one" else 7
            return GoogleControlProjectList(
                schema_version=1,
                account_ref=ref,
                inventory_generation=generation,
                projects=(project,) if ref == "google-one" else (),
            )
        return original_call(name, arguments, expected_generation, idempotency_key)

    client.call = call

    details = controller(client).account_details()

    assert tuple(row.account.ref for row in details) == ("google-one", "google-two")
    assert details[0].projects == (project,)
    assert details[1].projects == ()


def test_account_details_reject_generation_or_count_mismatch() -> None:
    client = FakeControlClient()
    original_call = client.call

    def call(name, arguments, expected_generation=None, idempotency_key=None):
        if name == "google.projects.list":
            return GoogleControlProjectList(
                schema_version=1,
                account_ref=arguments["account_ref"],
                inventory_generation=99,
                projects=(),
            )
        return original_call(name, arguments, expected_generation, idempotency_key)

    client.call = call

    with pytest.raises(GoogleAccountsError, match=r"control\.response_invalid"):
        controller(client).account_details()


def test_oauth_authorize_uses_fresh_default_client_and_raw_code_ingress() -> None:
    client = AuthorizationClient()
    provider = AuthorizationCallbackProvider()
    browser = BrowserLease()
    opened: list[tuple[str, str, str]] = []

    def open_browser(browser_name: str, account_ref: str, url: str) -> BrowserLease:
        opened.append((browser_name, account_ref, url))
        return browser

    subject = GoogleAccountsController(
        client,
        clock=lambda: NOW,
        idempotency_key_factory=iter(
            ("idem-begin", "idem-create", "idem-put")
        ).__next__,
        callback_provider=provider,
        browser_opener=open_browser,
    )

    receipt = subject.oauth_authorize("google-one", browser="firefox")

    assert receipt == GoogleOAuthReceipt("google-one", True, True)
    assert opened == [
        ("firefox", "google-one", "http://127.0.0.1:8765/oauth/start/nonsecret")
    ]
    assert provider.lease.code == bytearray(len(b"private-oauth-code"))
    assert provider.lease.close_count == 1
    assert browser.closed is True
    assert client.calls[1][:3] == (
        "google.oauth.begin",
        {
            "account_ref": "google-one",
            "oauth_client_ref": "oauth-client-1",
            "redirect_uri": REDIRECT_URI,
            "scope_profile": "inventory_readonly",
        },
        4,
    )
    assert client.calls[-1] == (
        "google.oauth.complete",
        {
            "account_ref": "google-one",
            "transaction_id": "oauth-1",
            "redirect_uri": REDIRECT_URI,
            "state": "state-one",
        },
        4,
        None,
    )
    assert client.puts[0][1] == b"private-oauth-code"


@pytest.mark.parametrize("control_exception", [KeyboardInterrupt, SystemExit])
def test_oauth_authorize_propagates_browser_control_exception_after_cleanup(
    control_exception,
) -> None:
    client = AuthorizationClient()
    provider = AuthorizationCallbackProvider()
    interrupt = control_exception("stop-now")
    subject = GoogleAccountsController(
        client,
        clock=lambda: NOW,
        idempotency_key_factory=lambda: "idem-one",
        callback_provider=provider,
        browser_opener=lambda *_args: (_ for _ in ()).throw(interrupt),
    )

    with pytest.raises(control_exception) as caught:
        subject.oauth_authorize("google-one", browser="firefox")

    assert caught.value is interrupt
    assert provider.lease.close_count == 1


@pytest.mark.parametrize("availability", ["missing", "ambiguous", "stale"])
def test_oauth_authorize_requires_fresh_available_projected_client(availability) -> None:
    client = AuthorizationClient()
    client.accounts = (
        replace(
            client.accounts[0],
            default_oauth_client_ref=None,
            oauth_client_availability=availability,
        ),
    )
    provider = AuthorizationCallbackProvider()
    browser = BrowserLease()
    subject = GoogleAccountsController(
        client,
        clock=lambda: NOW,
        idempotency_key_factory=lambda: "idem-one",
        callback_provider=provider,
        browser_opener=lambda *_args: browser,
    )

    with pytest.raises(GoogleAccountsError, match=r"oauth\.client_unavailable"):
        subject.oauth_authorize("google-one", browser="firefox")

    assert not any(call[0] == "google.oauth.begin" for call in client.calls)
    assert provider.lease.close_count == 1
    assert browser.closed is False


def test_oauth_authorize_rechecks_transaction_expiry_after_idempotency_key() -> None:
    client = AuthorizationClient()
    provider = AuthorizationCallbackProvider()
    browser = BrowserLease()
    current = [NOW]
    key_calls = [0]

    def key() -> str:
        key_calls[0] += 1
        if key_calls[0] == 2:
            current[0] = NOW + timedelta(minutes=10)
        return f"idem-{key_calls[0]}"

    subject = GoogleAccountsController(
        client,
        clock=lambda: current[0],
        idempotency_key_factory=key,
        callback_provider=provider,
        browser_opener=lambda *_args: browser,
    )

    with pytest.raises(GoogleAccountsError, match=r"oauth\.transaction_expired"):
        subject.oauth_authorize("google-one", browser="firefox")

    assert not any(call[0] == "secret.ingress.create" for call in client.calls)
    assert provider.lease.code == bytearray(len(b"private-oauth-code"))
    assert provider.lease.close_count == 1
    assert browser.closed is True


def test_provision_apply_reloads_plan_and_rejects_wrong_account_before_apply() -> None:
    client = FakeControlClient()
    subject = controller(client)
    plan = subject.provision_plan("google-one")
    restarted = controller(client)

    with pytest.raises(GoogleAccountsError, match=r"control\.plan_stale"):
        restarted.provision_apply(
            plan.plan_id, account_ref="google-two", plan_digest=plan.plan_digest
        )

    assert [call[0] for call in client.calls].count("operations.get") == 1
    assert not any(call[0] == "google.provision.apply" for call in client.calls)


def test_provision_apply_reloads_and_binds_digest_after_restart() -> None:
    client = FakeControlClient()
    plan = controller(client).provision_plan("google-one")

    applied = controller(client).provision_apply(
        plan.plan_id, account_ref="google-one", plan_digest=plan.plan_digest
    )

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
    assert client.calls[-1][1] == {"account_ref": "google-one"}
    assert client.calls[-1][2:] == (4, "idem-1")
    assert client.plan_digests[-1] == plan.plan_digest


def test_expired_reloaded_plan_never_reaches_apply() -> None:
    client = FakeControlClient()
    client.stored_plan = provision_plan(expires_at=NOW - timedelta(seconds=1))

    with pytest.raises(GoogleAccountsError, match=r"control\.plan_stale"):
        controller(client).provision_apply(
            "plan-1", account_ref="google-one", plan_digest=DIGEST
        )

    assert not any(call[0] == "google.provision.apply" for call in client.calls)


def test_inventory_refresh_uses_projection_generation_and_idempotency() -> None:
    client = FakeControlClient()

    result = controller(client).inventory_refresh("google-one")

    assert result.kind == "google.inventory.refresh"
    assert client.calls[-1] == (
        "google.inventory.refresh",
        {},
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
    assert client.calls[2][1:] == (
        {
            "account_ref": "google-one",
            "credential_kind": "google.oauth-client",
        },
        4,
        "idem-1",
    )
    assert client.calls[3][1:] == ({"account_ref": "google-one"}, 4, "idem-1")
    assert client.plan_digests == [None, None, DIGEST, DIGEST]
    assert source.as_posix() not in repr(controller(client))
    assert secret.decode() not in repr(controller(client))


def test_oauth_client_import_rejects_noncanonical_upload_receipt_without_apply(
    tmp_path,
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
                state="failed",
                generation=receipt.generation,
            )

    client = TerminalReceiptClient()

    with pytest.raises(GoogleAccountsError, match=r"control\.response_invalid"):
        controller(client).import_oauth_client("google-one", source)

    assert not any(call[0] == "google.oauth-client-import.apply" for call in client.calls)


def test_oauth_client_import_rejects_legacy_apply_operation(tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    source = root / "oauth-client.json"
    source.write_text('{"client_secret":"private"}', encoding="utf-8")
    source.chmod(0o600)

    class TerminalApplyClient(FakeControlClient):
        def call(
            self,
            name,
            arguments,
            expected_generation=None,
            idempotency_key=None,
            plan_digest=None,
        ):
            if name == "google.oauth-client-import.apply":
                self.calls.append((name, arguments, expected_generation, idempotency_key))
                return operation(
                    name,
                    operation_id="import-apply-1",
                    state="failed",
                    resulting_generation=None,
                )
            return super().call(
                name,
                arguments,
                expected_generation,
                idempotency_key,
                plan_digest,
            )

    with pytest.raises(GoogleAccountsError, match=r"control\.response_invalid"):
        controller(TerminalApplyClient()).import_oauth_client("google-one", source)


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
        subject.provision_apply("plan-1", account_ref="google-one", plan_digest=DIGEST)

    assert not any(call[0] == "google.provision.apply" for call in client.calls)


def test_provision_apply_rejects_preview_digest_mismatch_before_apply() -> None:
    client = FakeControlClient()

    with pytest.raises(GoogleAccountsError, match=r"control\.plan_stale"):
        controller(client).provision_apply(
            "plan-1",
            account_ref="google-one",
            plan_digest="sha256:" + "b" * 64,
        )

    assert not any(call[0] == "google.provision.apply" for call in client.calls)


def test_billing_plan_rejects_billing_ref_from_another_google_account_before_request() -> None:
    client = BillingControlClient()

    with pytest.raises(GoogleAccountsError, match=r"control\.response_invalid"):
        controller(client).billing_plan("google-one", "project-one", "billing-two")

    assert not any(call[0] == "google.billing.plan" for call in client.calls)


def test_billing_plan_and_apply_preserve_plan_idempotency_and_generation() -> None:
    client = BillingControlClient()
    subject = controller(client)

    plan = subject.billing_plan("google-one", "project-one", "billing-one")
    receipt = subject.billing_apply(
        plan.plan_id,
        account_ref=plan.account_ref,
        project_ref=plan.project_ref,
        billing_ref=plan.billing_ref,
        expected_generation=plan.expected_generation,
        plan_digest=plan.plan_digest,
        idempotency_key=plan.idempotency_key,
    )

    assert plan.expected_generation == 4
    assert plan.idempotency_key == "idem-1"
    assert receipt.plan_id == plan.plan_id
    assert client.calls[-1] == (
        "google.billing.apply",
        {
            "account_ref": "google-one",
            "project_ref": "project-one",
            "billing_ref": "billing-one",
            "plan_id": "billing-plan-one",
        },
        4,
        "idem-1",
        DIGEST,
    )
