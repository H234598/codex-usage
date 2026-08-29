from __future__ import annotations

import hashlib
import os
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypeVar

from .google_oauth_loopback import GoogleOAuthLoopbackError
from .masterjet_client import MasterjetClientError
from .masterjet_contracts import (
    ControlContractError,
    ControlOperation,
    GoogleControlAccount,
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
    google_oauth_redirect_uri,
    google_oauth_state,
    validate_google_oauth_redirect_uri,
)
from .private_io import open_verified_state_home, read_private_bytes_at

MAX_OAUTH_CLIENT_JSON_BYTES = 1_000_000
_CALLBACK_CLOSE_RETRY_DELAYS = (1.0, 2.0)
_CALLBACK_CLOSE_MAX_ATTEMPTS = len(_CALLBACK_CLOSE_RETRY_DELAYS) + 1
_T = TypeVar("_T")
_PATH_TYPE = type(Path())
_PLAN_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


class AuthenticatedGoogleClient(Protocol):
    def call(
        self,
        operation: str,
        arguments: object,
        expected_generation: int | None = None,
        idempotency_key: str | None = None,
        plan_digest: str | None = None,
    ) -> object: ...

    def put_secret(
        self,
        session_id: str,
        secret: bytes | bytearray | memoryview,
        expected_generation: int | None = None,
        idempotency_key: str | None = None,
    ) -> object: ...


class GoogleOAuthCallbackLease(Protocol):
    @property
    def redirect_uri(self) -> str: ...

    @property
    def launch_uri(self) -> str: ...

    def prepare_authorization(self, authorization_url: str) -> None: ...

    def receive(self, *, expected_state: str, timeout_seconds: float) -> bytearray: ...

    def close(self) -> None: ...


class GoogleOAuthCallbackProvider(Protocol):
    def acquire(self) -> GoogleOAuthCallbackLease: ...


class GoogleOAuthBrowserLease(Protocol):
    def close(self) -> None: ...


class _CallbackTimer(Protocol):
    def start(self) -> None: ...

    def cancel(self) -> None: ...


class GoogleAccountsError(RuntimeError):
    """Redacted Google control-flow failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class GoogleProvisionPlan:
    account_ref: str
    plan_id: str
    expected_generation: int
    plan_digest: str
    expires_at: datetime
    step_count: int
    projects: tuple[GoogleProvisionProjectV1, ...]


@dataclass(frozen=True, slots=True)
class GoogleAccountDetails:
    account: GoogleControlAccount
    projects: tuple[GoogleControlProject, ...]

    def __post_init__(self) -> None:
        if (
            type(self.account) is not GoogleControlAccount
            or type(self.projects) is not tuple
            or not all(type(item) is GoogleControlProject for item in self.projects)
            or len(self.projects) != self.account.project_count
        ):
            raise ValueError("control.response_invalid")


@dataclass(frozen=True, slots=True)
class GoogleOAuthClientImportResult:
    account_ref: str
    generation: int
    status: str


class GoogleAccountsController:
    __slots__ = (
        "_browser_opener",
        "_callback_cleanup_failed",
        "_callback_close_attempts",
        "_callback_lease",
        "_callback_lock",
        "_callback_provider",
        "_callback_timer",
        "_callback_timer_factory",
        "_callback_transaction",
        "_client",
        "_clock",
        "_idempotency_key_factory",
    )

    def __init__(
        self,
        client: AuthenticatedGoogleClient,
        *,
        clock: Callable[[], datetime] | None = None,
        idempotency_key_factory: Callable[[], str] | None = None,
        callback_provider: GoogleOAuthCallbackProvider | None = None,
        callback_timer_factory: Callable[[float, Callable[[], None]], _CallbackTimer] | None = None,
        browser_opener: (
            Callable[[str, str, str], GoogleOAuthBrowserLease] | None
        ) = None,
    ) -> None:
        self._client = client
        self._clock = clock if clock is not None else _utc_now
        self._idempotency_key_factory = (
            idempotency_key_factory if idempotency_key_factory is not None else _uuid_key
        )
        self._callback_provider = callback_provider
        self._browser_opener = browser_opener
        self._callback_timer_factory = (
            callback_timer_factory if callback_timer_factory is not None else _new_callback_timer
        )
        self._callback_lease: GoogleOAuthCallbackLease | None = None
        self._callback_transaction: GoogleOAuthTransactionV1 | None = None
        self._callback_timer: _CallbackTimer | None = None
        self._callback_close_attempts = 0
        self._callback_cleanup_failed = False
        self._callback_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def list(self) -> tuple[GoogleControlAccount, ...]:
        return self._guard(self._list)

    def account_details(self) -> tuple[GoogleAccountDetails, ...]:
        return self._guard(self._account_details)

    def oauth_authorize(self, account_ref: str, *, browser: str) -> GoogleOAuthReceipt:
        return self._guard(lambda: self._oauth_authorize(account_ref, browser))

    def inventory_refresh(self, account_ref: str) -> ControlOperation:
        return self._guard(lambda: self._inventory_refresh(account_ref))

    def provision_plan(self, account_ref: str) -> GoogleProvisionPlan:
        return self._guard(lambda: self._provision_plan(account_ref))

    def provision_apply(
        self, plan_id: str, *, account_ref: str, plan_digest: str
    ) -> ControlOperation:
        return self._guard(lambda: self._provision_apply(plan_id, account_ref, plan_digest))

    def import_oauth_client(self, account_ref: str, source: Path) -> GoogleOAuthClientImportResult:
        try:
            return self._import_oauth_client(account_ref, source)
        except GoogleAccountsError:
            raise
        except MasterjetClientError as exc:
            raise GoogleAccountsError(exc.code) from None
        except (OSError, TypeError, ValueError):
            raise GoogleAccountsError("credential.source_unavailable") from None
        except Exception:
            raise GoogleAccountsError("control.transport_unavailable") from None

    def _guard(self, action: Callable[[], _T]) -> _T:
        try:
            return action()
        except GoogleAccountsError:
            raise
        except MasterjetClientError as exc:
            raise GoogleAccountsError(exc.code) from None
        except Exception:
            raise GoogleAccountsError("control.transport_unavailable") from None

    def _list(self) -> tuple[GoogleControlAccount, ...]:
        rows = self._client.call("google.accounts.list", {})
        if type(rows) is not tuple or any(type(row) is not GoogleControlAccount for row in rows):
            raise MasterjetClientError("control.response_invalid")
        return rows

    def _account(self, account_ref: str) -> GoogleControlAccount:
        rows = self._list()
        matches = [row for row in rows if row.ref == account_ref]
        if len(matches) != 1:
            raise MasterjetClientError("control.response_invalid")
        return matches[0]

    def _account_details(self) -> tuple[GoogleAccountDetails, ...]:
        accounts = self._list()
        result: list[GoogleAccountDetails] = []
        for account in accounts:
            value = self._client.call("google.projects.list", {"account_ref": account.ref})
            if (
                type(value) is not GoogleControlProjectList
                or value.account_ref != account.ref
                or value.inventory_generation != account.inventory_generation
                or len(value.projects) != account.project_count
            ):
                raise MasterjetClientError("control.response_invalid")
            result.append(GoogleAccountDetails(account=account, projects=value.projects))
        return tuple(result)

    def _oauth_authorize(self, account_ref: str, browser: str) -> GoogleOAuthReceipt:
        with self._callback_lock:
            self._require_mutation_allowed()
            if self._callback_lease is not None:
                raise MasterjetClientError("oauth.callback_active")
            provider = self._callback_provider
            opener = self._browser_opener
            if provider is None or opener is None:
                raise MasterjetClientError("oauth.callback_unavailable")
            lease: GoogleOAuthCallbackLease | None = None
            browser_lease: GoogleOAuthBrowserLease | None = None
            code: bytearray | None = None
            try:
                lease = provider.acquire()
                self._callback_lease = lease
                self._callback_close_attempts = 0
                redirect_uri = validate_google_oauth_redirect_uri(lease.redirect_uri)
                account = self._account(account_ref)
                if (
                    account.oauth_client_availability != "available"
                    or account.default_oauth_client_ref is None
                ):
                    raise MasterjetClientError("oauth.client_unavailable")
                transaction = self._client.call(
                    "google.oauth.begin",
                    {
                        "account_ref": account.ref,
                        "oauth_client_ref": account.default_oauth_client_ref,
                        "redirect_uri": redirect_uri,
                        "scope_profile": "inventory_readonly",
                    },
                    expected_generation=account.inventory_generation,
                    idempotency_key=self._idempotency_key(),
                )
                if type(transaction) is not GoogleOAuthTransactionV1:
                    raise MasterjetClientError("control.response_invalid")
                if (
                    transaction.account_ref != account.ref
                    or transaction.inventory_generation != account.inventory_generation
                    or google_oauth_redirect_uri(transaction.authorization_url) != redirect_uri
                ):
                    raise MasterjetClientError("control.response_invalid")
                state = google_oauth_state(transaction.authorization_url)
                timeout = transaction.expires_at - _clock_epoch(self._clock)
                if timeout <= 0:
                    raise MasterjetClientError("oauth.transaction_expired")
                self._callback_transaction = transaction
                self._schedule_callback_timer(lease, transaction, timeout)
                try:
                    lease.prepare_authorization(transaction.authorization_url)
                    browser_lease = opener(browser, account.ref, lease.launch_uri)
                except BaseException:
                    raise MasterjetClientError("oauth.browser_unavailable") from None
                try:
                    code = lease.receive(expected_state=state, timeout_seconds=timeout)
                except GoogleOAuthLoopbackError as error:
                    raise MasterjetClientError(error.code) from None
                digest = _oauth_flow_digest(
                    account.ref,
                    transaction.id,
                    redirect_uri,
                    state,
                    transaction.inventory_generation,
                )
                create_key = self._idempotency_key()
                if transaction.expires_at <= _clock_epoch(self._clock):
                    raise MasterjetClientError("oauth.transaction_expired")
                session = self._client.call(
                    "secret.ingress.create",
                    {
                        "account_ref": account.ref,
                        "credential_kind": "google-oauth-code",
                        "transaction_id": transaction.id,
                    },
                    expected_generation=transaction.inventory_generation,
                    idempotency_key=create_key,
                    plan_digest=digest,
                )
                if (
                    type(session) is not SecretIngressSession
                    or session.account_ref != account.ref
                    or session.plan_digest != digest
                    or session.expected_generation != transaction.inventory_generation
                    or session.session_generation != transaction.inventory_generation
                ):
                    raise MasterjetClientError("control.response_invalid")
                if session.expires_at <= _clock_epoch(self._clock):
                    raise MasterjetClientError("credential.upload_expired")
                put_key = self._idempotency_key()
                now = _clock_epoch(self._clock)
                if transaction.expires_at <= now:
                    raise MasterjetClientError("oauth.transaction_expired")
                if session.expires_at <= now:
                    raise MasterjetClientError("credential.upload_expired")
                receipt = self._client.put_secret(
                    session.id,
                    code,
                    expected_generation=session.session_generation,
                    idempotency_key=put_key,
                )
                if (
                    type(receipt) is not SecretIngressReceipt
                    or receipt.session_id != session.id
                    or receipt.account_ref != account.ref
                    or receipt.state != "consumed"
                ):
                    raise MasterjetClientError("control.response_invalid")
                if transaction.expires_at <= _clock_epoch(self._clock):
                    raise MasterjetClientError("oauth.transaction_expired")
                completed = self._client.call(
                    "google.oauth.complete",
                    {
                        "account_ref": account.ref,
                        "transaction_id": transaction.id,
                        "redirect_uri": redirect_uri,
                        "state": state,
                    },
                    expected_generation=session.session_generation,
                )
                if (
                    type(completed) is not GoogleOAuthReceipt
                    or completed.account_ref != account.ref
                    or not completed.subject_bound
                    or not completed.refresh_token_stored
                ):
                    raise MasterjetClientError("control.response_invalid")
                return completed
            except ControlContractError:
                raise MasterjetClientError("control.response_invalid") from None
            finally:
                if code is not None:
                    _zero_secret(code)
                if browser_lease is not None:
                    try:
                        browser_lease.close()
                    except BaseException:
                        pass
                if lease is not None and lease is self._callback_lease:
                    try:
                        self._close_active_callback()
                    except MasterjetClientError:
                        pass

    def _close_active_callback(self) -> None:
        lease = self._callback_lease
        if lease is None:
            return
        if self._callback_cleanup_failed:
            raise MasterjetClientError("oauth.callback_cleanup_failed")
        self._callback_close_attempts += 1
        try:
            _close_callback_lease(lease)
        except MasterjetClientError:
            if self._callback_close_attempts >= _CALLBACK_CLOSE_MAX_ATTEMPTS:
                self._quarantine_callback()
                raise MasterjetClientError("oauth.callback_cleanup_failed") from None
            try:
                self._schedule_callback_timer(
                    lease,
                    self._callback_transaction,
                    _CALLBACK_CLOSE_RETRY_DELAYS[self._callback_close_attempts - 1],
                )
            except MasterjetClientError:
                self._close_callback_synchronously()
            raise
        self._release_callback()

    def _close_callback_synchronously(self) -> None:
        lease = self._callback_lease
        while lease is not None and self._callback_close_attempts < _CALLBACK_CLOSE_MAX_ATTEMPTS:
            self._callback_close_attempts += 1
            try:
                _close_callback_lease(lease)
            except MasterjetClientError:
                continue
            self._release_callback()
            return
        self._quarantine_callback()
        raise MasterjetClientError("oauth.callback_cleanup_failed") from None

    def _release_callback(self) -> None:
        timer = self._callback_timer
        self._callback_lease = None
        self._callback_transaction = None
        self._callback_timer = None
        self._callback_close_attempts = 0
        self._callback_cleanup_failed = False
        if timer is not None:
            try:
                timer.cancel()
            except BaseException:
                pass

    def _quarantine_callback(self) -> None:
        timer = self._callback_timer
        self._callback_timer = None
        self._callback_cleanup_failed = True
        if timer is not None:
            try:
                timer.cancel()
            except BaseException:
                pass

    def _require_mutation_allowed(self) -> None:
        if self._callback_cleanup_failed:
            raise MasterjetClientError("oauth.callback_cleanup_failed")
        if self._callback_close_attempts:
            raise MasterjetClientError("oauth.callback_unavailable")

    def _schedule_callback_timer(
        self,
        lease: GoogleOAuthCallbackLease,
        transaction: GoogleOAuthTransactionV1 | None,
        delay: float,
    ) -> None:
        current = self._callback_timer
        timer_ref: list[_CallbackTimer] = []

        def expire() -> None:
            self._expire_callback(lease, transaction, timer_ref[0])

        timer: _CallbackTimer | None = None
        try:
            timer = self._callback_timer_factory(delay, expire)
            timer_ref.append(timer)
            self._callback_timer = timer
            timer.start()
        except BaseException:
            self._callback_timer = current
            if timer is not None:
                try:
                    timer.cancel()
                except BaseException:
                    pass
            raise MasterjetClientError("oauth.callback_unavailable") from None
        if current is not None:
            try:
                current.cancel()
            except BaseException:
                pass

    def _expire_callback(
        self,
        lease: GoogleOAuthCallbackLease,
        transaction: GoogleOAuthTransactionV1 | None,
        timer: _CallbackTimer,
    ) -> None:
        with self._callback_lock:
            if (
                timer is not self._callback_timer
                or lease is not self._callback_lease
                or transaction != self._callback_transaction
            ):
                return
            self._callback_timer = None
            try:
                self._close_active_callback()
            except MasterjetClientError:
                pass

    def _inventory_refresh(self, account_ref: str) -> ControlOperation:
        self._require_mutation_allowed()
        account = self._account(account_ref)
        result = self._client.call(
            "google.inventory.refresh",
            {"account_ref": account.ref},
            expected_generation=account.inventory_generation,
            idempotency_key=self._idempotency_key(),
        )
        return _require_operation(
            result,
            kind="google.inventory.refresh",
            expected_generation=account.inventory_generation,
        )

    def _provision_plan(self, account_ref: str) -> GoogleProvisionPlan:
        self._require_mutation_allowed()
        account = self._account(account_ref)
        result = self._client.call(
            "google.provision.plan",
            {"account_ref": account.ref},
            expected_generation=account.inventory_generation,
            idempotency_key=self._idempotency_key(),
        )
        plan = _require_provision_plan(result, account=account, clock=self._clock)
        return GoogleProvisionPlan(
            account_ref=account.ref,
            plan_id=plan.id,
            expected_generation=plan.expected_generation,
            plan_digest=plan.plan_digest,
            expires_at=plan.expires_at,
            step_count=plan.step_count,
            projects=plan.projects,
        )

    def _provision_apply(
        self, plan_id: str, account_ref: str, expected_plan_digest: str
    ) -> ControlOperation:
        expected_plan_digest = validate_google_plan_digest(expected_plan_digest)
        self._require_mutation_allowed()
        account = self._account(account_ref)
        fetched = self._client.call(
            "operations.get",
            {"operation_id": plan_id, "account_ref": account.ref},
        )
        plan = _require_provision_plan(
            fetched,
            account=account,
            clock=self._clock,
            generation_code="control.plan_stale",
        )
        if plan.id != plan_id:
            raise MasterjetClientError("control.response_invalid")
        if plan.plan_digest != expected_plan_digest:
            raise MasterjetClientError("control.plan_stale")
        idempotency_key = self._idempotency_key()
        _require_unexpired(plan.expires_at, self._clock, "control.plan_stale")
        result = self._client.call(
            "google.provision.apply",
            {"account_ref": account.ref, "plan_id": plan.id},
            expected_generation=plan.expected_generation,
            idempotency_key=idempotency_key,
        )
        applied = _require_operation(
            result,
            kind="google.provision.apply",
            expected_generation=plan.expected_generation,
        )
        if applied.plan_digest != plan.plan_digest:
            raise MasterjetClientError("control.response_invalid")
        return applied

    def _import_oauth_client(self, account_ref: str, source: Path) -> GoogleOAuthClientImportResult:
        self._require_mutation_allowed()
        account = self._account(account_ref)
        raw_plan = self._client.call(
            "google.oauth-client-import.plan",
            {"account_ref": account.ref},
            expected_generation=account.inventory_generation,
            idempotency_key=self._idempotency_key(),
        )
        if (
            type(raw_plan) is not GoogleOAuthClientImportPlanV1
            or raw_plan.account_ref != account.ref
            or raw_plan.expected_generation != account.inventory_generation
        ):
            raise MasterjetClientError("control.response_invalid")
        plan = raw_plan
        if plan.expires_at <= _clock_epoch(self._clock):
            raise MasterjetClientError("control.plan_stale")
        secret = _read_oauth_client_json(source)
        try:
            session_key = self._idempotency_key()
            if plan.expires_at <= _clock_epoch(self._clock):
                raise MasterjetClientError("control.plan_stale")
            raw_session = self._client.call(
                "secret.ingress.create",
                {
                    "account_ref": account.ref,
                    "credential_kind": "google.oauth-client",
                },
                expected_generation=plan.expected_generation,
                idempotency_key=session_key,
                plan_digest=plan.plan_digest,
            )
            if type(raw_session) is not SecretIngressSession:
                raise MasterjetClientError("control.response_invalid")
            session = raw_session
            if (
                session.account_ref != account.ref
                or session.plan_digest != plan.plan_digest
                or session.expected_generation != plan.expected_generation
                or session.session_generation != plan.expected_generation
            ):
                raise MasterjetClientError("control.response_invalid")
            put_key = self._idempotency_key()
            now = _clock_epoch(self._clock)
            if session.expires_at <= now:
                raise MasterjetClientError("credential.upload_expired")
            if plan.expires_at <= now:
                raise MasterjetClientError("control.plan_stale")
            raw_receipt = self._client.put_secret(
                session.id,
                secret,
                expected_generation=session.session_generation,
                idempotency_key=put_key,
            )
            if type(raw_receipt) is not SecretIngressReceipt:
                raise MasterjetClientError("control.response_invalid")
            receipt = raw_receipt
            if receipt.session_id != session.id or receipt.account_ref != account.ref:
                raise MasterjetClientError("control.response_invalid")
            if receipt.state != "consumed":
                raise MasterjetClientError("control.response_invalid")
            apply_key = self._idempotency_key()
            if plan.expires_at <= _clock_epoch(self._clock):
                raise MasterjetClientError("control.plan_stale")
            applied = self._client.call(
                "google.oauth-client-import.apply",
                {"account_ref": account.ref},
                expected_generation=session.session_generation,
                idempotency_key=apply_key,
                plan_digest=plan.plan_digest,
            )
            if (
                type(applied) is not GoogleOAuthClientImportReceiptV1
                or applied.account_ref != account.ref
            ):
                raise MasterjetClientError("control.response_invalid")
            return GoogleOAuthClientImportResult(
                account_ref=account.ref,
                generation=applied.inventory_generation,
                status="succeeded",
            )
        finally:
            _zero_secret(secret)

    def _idempotency_key(self) -> str:
        try:
            value = self._idempotency_key_factory()
        except Exception:
            raise MasterjetClientError("control.request_invalid") from None
        if type(value) is not str:
            raise MasterjetClientError("control.request_invalid")
        return value


def _read_oauth_client_json(source: Path) -> bytearray:
    if (
        type(source) is not _PATH_TYPE
        or not source.is_absolute()
        or any(part in {"", ".", ".."} for part in source.parts[1:])
    ):
        raise ValueError("oauth client source is invalid")
    parent_fd = open_verified_state_home(source.parent)
    try:
        payload, _identity = read_private_bytes_at(
            parent_fd,
            source.name,
            maximum=MAX_OAUTH_CLIENT_JSON_BYTES,
            mode=0o600,
        )
    finally:
        os.close(parent_fd)
    if not payload:
        raise ValueError("oauth client source is empty")
    secret = bytearray(payload)
    del payload
    return secret


def validate_google_plan_digest(value: object) -> str:
    if type(value) is not str or _PLAN_DIGEST_RE.fullmatch(value) is None:
        raise GoogleAccountsError("control.response_invalid")
    return value


def _require_plan(
    value: object,
    *,
    kind: str,
    expected_generation: int,
    clock: Callable[[], datetime],
    generation_code: str = "control.response_invalid",
) -> ControlOperation:
    if type(value) is not ControlOperation:
        raise MasterjetClientError("control.response_invalid")
    operation = value
    if operation.kind != kind:
        raise MasterjetClientError("control.response_invalid")
    if operation.expected_generation != expected_generation:
        raise MasterjetClientError(generation_code)
    if operation.state != "planned" or operation.resulting_generation is not None:
        raise MasterjetClientError("control.response_invalid")
    _require_unexpired(operation.expires_at, clock, "control.plan_stale")
    return operation


def _require_provision_plan(
    value: object,
    *,
    account: GoogleControlAccount,
    clock: Callable[[], datetime],
    generation_code: str = "control.response_invalid",
) -> GoogleProvisionPlanV1:
    if type(value) is not GoogleProvisionPlanV1:
        raise MasterjetClientError("control.response_invalid")
    plan = value
    if plan.account_ref != account.ref:
        raise MasterjetClientError("control.response_invalid")
    if plan.expected_generation != account.inventory_generation:
        raise MasterjetClientError(generation_code)
    _require_unexpired(plan.expires_at, clock, "control.plan_stale")
    return plan


def _require_operation(value: object, *, kind: str, expected_generation: int) -> ControlOperation:
    if type(value) is not ControlOperation:
        raise MasterjetClientError("control.response_invalid")
    operation = value
    if operation.kind != kind or operation.expected_generation != expected_generation:
        raise MasterjetClientError("control.response_invalid")
    return operation


def _clock_value(clock: Callable[[], datetime]) -> datetime:
    try:
        now = clock()
    except Exception:
        raise MasterjetClientError("control.response_invalid") from None
    if type(now) is not datetime or now.tzinfo is not UTC:
        raise MasterjetClientError("control.response_invalid")
    return now


def _clock_epoch(clock: Callable[[], datetime]) -> float:
    return _clock_value(clock).timestamp()


def _oauth_flow_digest(
    account_ref: str,
    transaction_id: str,
    redirect_uri: str,
    state: str,
    generation: int,
) -> str:
    payload = "\0".join(
        (account_ref, transaction_id, redirect_uri, state, str(generation))
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _require_unexpired(expires_at: datetime, clock: Callable[[], datetime], code: str) -> None:
    if expires_at <= _clock_value(clock):
        raise MasterjetClientError(code)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_callback_timer(delay: float, callback: Callable[[], None]) -> _CallbackTimer:
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    return timer


def _uuid_key() -> str:
    try:
        return str(uuid.uuid4())
    except Exception:
        raise MasterjetClientError("control.request_invalid") from None


def _zero_secret(secret: bytearray) -> None:
    for index in range(len(secret)):
        secret[index] = 0


def _close_callback_lease(lease: GoogleOAuthCallbackLease) -> None:
    try:
        lease.close()
    except BaseException:
        raise MasterjetClientError("oauth.callback_unavailable") from None
