from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypeVar, cast

from .masterjet_client import MasterjetClientError
from .masterjet_contracts import (
    ControlContractError,
    ControlOperation,
    GoogleControlAccount,
    GoogleControlProject,
    GoogleControlProjectList,
    GoogleOAuthTransactionV1,
    GoogleProvisionPlanV1,
    GoogleProvisionProjectV1,
    SecretIngressReceipt,
    SecretIngressSession,
    google_oauth_redirect_uri,
    validate_google_oauth_redirect_uri,
)
from .private_io import open_verified_state_home, read_private_bytes_at

MAX_OAUTH_CLIENT_JSON_BYTES = 1_000_000
_CALLBACK_CLOSE_RETRY_DELAYS = (1.0, 2.0)
_CALLBACK_CLOSE_MAX_ATTEMPTS = len(_CALLBACK_CLOSE_RETRY_DELAYS) + 1
_T = TypeVar("_T")
_PATH_TYPE = type(Path())


class AuthenticatedGoogleClient(Protocol):
    def call(
        self,
        operation: str,
        arguments: object,
        expected_generation: int | None = None,
        idempotency_key: str | None = None,
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

    def close(self) -> None: ...


class GoogleOAuthCallbackProvider(Protocol):
    def acquire(self) -> GoogleOAuthCallbackLease: ...


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
        "_terminal_oauth_result",
        "_terminal_oauth_transaction",
    )

    def __init__(
        self,
        client: AuthenticatedGoogleClient,
        *,
        clock: Callable[[], datetime] | None = None,
        idempotency_key_factory: Callable[[], str] | None = None,
        callback_provider: GoogleOAuthCallbackProvider | None = None,
        callback_timer_factory: Callable[[float, Callable[[], None]], _CallbackTimer] | None = None,
    ) -> None:
        self._client = client
        self._clock = clock if clock is not None else _utc_now
        self._idempotency_key_factory = (
            idempotency_key_factory if idempotency_key_factory is not None else _uuid_key
        )
        self._callback_provider = callback_provider
        self._callback_timer_factory = (
            callback_timer_factory if callback_timer_factory is not None else _new_callback_timer
        )
        self._callback_lease: GoogleOAuthCallbackLease | None = None
        self._callback_transaction: GoogleOAuthTransactionV1 | None = None
        self._callback_timer: _CallbackTimer | None = None
        self._callback_close_attempts = 0
        self._callback_cleanup_failed = False
        self._terminal_oauth_transaction: GoogleOAuthTransactionV1 | None = None
        self._terminal_oauth_result: ControlOperation | None = None
        self._callback_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def list(self) -> tuple[GoogleControlAccount, ...]:
        return self._guard(self._list)

    def account_details(self) -> tuple[GoogleAccountDetails, ...]:
        return self._guard(self._account_details)

    def oauth_begin(self, account_ref: str, *, browser: str) -> GoogleOAuthTransactionV1:
        return self._guard(lambda: self._oauth_begin(account_ref, browser))

    def oauth_complete(self, transaction: GoogleOAuthTransactionV1) -> ControlOperation:
        return self._guard(lambda: self._oauth_complete(transaction))

    def oauth_cancel(self, transaction: GoogleOAuthTransactionV1) -> None:
        self._guard(lambda: self._oauth_cancel(transaction))

    def inventory_refresh(self, account_ref: str) -> ControlOperation:
        return self._guard(lambda: self._inventory_refresh(account_ref))

    def provision_plan(self, account_ref: str) -> GoogleProvisionPlan:
        return self._guard(lambda: self._provision_plan(account_ref))

    def provision_apply(self, plan_id: str, *, account_ref: str) -> ControlOperation:
        return self._guard(lambda: self._provision_apply(plan_id, account_ref))

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

    def _oauth_begin(self, account_ref: str, browser: str) -> GoogleOAuthTransactionV1:
        with self._callback_lock:
            if self._callback_cleanup_failed:
                raise MasterjetClientError("oauth.callback_cleanup_failed")
            if self._callback_lease is not None:
                raise MasterjetClientError("oauth.callback_active")
            provider = self._callback_provider
            if provider is None:
                raise MasterjetClientError("oauth.callback_unavailable")
            lease: GoogleOAuthCallbackLease | None = None
            try:
                lease = provider.acquire()
                self._callback_lease = lease
                self._callback_close_attempts = 0
                self._terminal_oauth_transaction = None
                self._terminal_oauth_result = None
                transaction = self._begin_with_callback(
                    lease, account_ref=account_ref, browser=browser
                )
                self._callback_transaction = transaction
                self._schedule_callback_timer(
                    lease,
                    transaction,
                    max(
                        0.0,
                        (transaction.expires_at - _clock_value(self._clock)).total_seconds(),
                    ),
                )
            except BaseException:
                if lease is not None and lease is self._callback_lease:
                    self._close_active_callback()
                raise
            return transaction

    def _begin_with_callback(
        self,
        lease: GoogleOAuthCallbackLease,
        *,
        account_ref: str,
        browser: str,
    ) -> GoogleOAuthTransactionV1:
        redirect_uri = validate_google_oauth_redirect_uri(lease.redirect_uri)
        account = self._account(account_ref)
        result = self._client.call(
            "google.oauth.begin",
            {
                "account_ref": account.ref,
                "browser": browser,
                "redirect_uri": redirect_uri,
            },
            expected_generation=account.inventory_generation,
            idempotency_key=self._idempotency_key(),
        )
        if type(result) is not GoogleOAuthTransactionV1:
            raise MasterjetClientError("control.response_invalid")
        transaction = cast(GoogleOAuthTransactionV1, result)
        if (
            transaction.account_ref != account.ref
            or transaction.generation != account.inventory_generation
        ):
            raise MasterjetClientError("control.response_invalid")
        try:
            transaction_redirect_uri = google_oauth_redirect_uri(transaction.authorization_url)
        except ControlContractError:
            raise MasterjetClientError("control.response_invalid") from None
        if transaction_redirect_uri != redirect_uri:
            raise MasterjetClientError("control.response_invalid")
        _require_unexpired(transaction.expires_at, self._clock, "oauth.transaction_expired")
        return transaction

    def _oauth_complete(self, transaction: GoogleOAuthTransactionV1) -> ControlOperation:
        if type(transaction) is not GoogleOAuthTransactionV1:
            raise MasterjetClientError("control.request_invalid")
        with self._callback_lock:
            if (
                transaction == self._terminal_oauth_transaction
                and self._terminal_oauth_result is not None
            ):
                return self._terminal_oauth_result
            self._require_mutation_allowed()
            if transaction != self._callback_transaction:
                raise MasterjetClientError("control.request_invalid")
            try:
                account = self._account(transaction.account_ref)
                if account.inventory_generation != transaction.generation:
                    raise MasterjetClientError("credential.generation_conflict")
                idempotency_key = self._idempotency_key()
                _require_unexpired(transaction.expires_at, self._clock, "oauth.transaction_expired")
                result = self._client.call(
                    "google.oauth.complete",
                    {"account_ref": account.ref, "transaction_id": transaction.id},
                    expected_generation=transaction.generation,
                    idempotency_key=idempotency_key,
                )
                completed = _require_operation(
                    result,
                    kind="google.oauth.complete",
                    expected_generation=transaction.generation,
                )
            except BaseException:
                self._close_active_callback()
                raise
            self._terminal_oauth_transaction = transaction
            self._terminal_oauth_result = completed
            try:
                self._close_active_callback()
            except MasterjetClientError:
                pass
            return completed

    def _oauth_cancel(self, transaction: GoogleOAuthTransactionV1) -> None:
        if type(transaction) is not GoogleOAuthTransactionV1:
            raise MasterjetClientError("control.request_invalid")
        with self._callback_lock:
            self._require_mutation_allowed()
            if transaction != self._callback_transaction:
                raise MasterjetClientError("control.request_invalid")
            self._close_active_callback()

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

    def _provision_apply(self, plan_id: str, account_ref: str) -> ControlOperation:
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
        plan = _require_plan(
            raw_plan,
            kind="google.oauth-client-import.plan",
            expected_generation=account.inventory_generation,
            clock=self._clock,
        )
        secret = _read_oauth_client_json(source)
        try:
            session_key = self._idempotency_key()
            _require_unexpired(plan.expires_at, self._clock, "control.plan_stale")
            raw_session = self._client.call(
                "secret.ingress.create",
                {
                    "account_ref": account.ref,
                    "credential_type": "google_oauth_client_json",
                    "plan_id": plan.id,
                },
                expected_generation=plan.expected_generation,
                idempotency_key=session_key,
            )
            if type(raw_session) is not SecretIngressSession:
                raise MasterjetClientError("control.response_invalid")
            session = raw_session
            if (
                session.account_ref != account.ref
                or session.plan_id != plan.id
                or session.expected_generation != plan.expected_generation
            ):
                raise MasterjetClientError("control.response_invalid")
            put_key = self._idempotency_key()
            now = _clock_value(self._clock)
            if session.expires_at <= now:
                raise MasterjetClientError("credential.upload_expired")
            if plan.expires_at <= now:
                raise MasterjetClientError("control.plan_stale")
            raw_receipt = self._client.put_secret(
                session.id,
                secret,
                expected_generation=plan.expected_generation,
                idempotency_key=put_key,
            )
            if type(raw_receipt) is not SecretIngressReceipt:
                raise MasterjetClientError("control.response_invalid")
            receipt = raw_receipt
            if receipt.session_id != session.id or receipt.account_ref != account.ref:
                raise MasterjetClientError("control.response_invalid")
            if receipt.state in {"partial", "failed", "blocked"}:
                return GoogleOAuthClientImportResult(
                    account_ref=account.ref,
                    generation=receipt.generation,
                    status=receipt.state,
                )
            if receipt.state != "consumed" or receipt.generation <= plan.expected_generation:
                raise MasterjetClientError("control.response_invalid")
            apply_key = self._idempotency_key()
            _require_unexpired(plan.expires_at, self._clock, "control.plan_stale")
            applied = _require_operation(
                self._client.call(
                    "google.oauth-client-import.apply",
                    {"account_ref": account.ref, "plan_id": plan.id},
                    expected_generation=plan.expected_generation,
                    idempotency_key=apply_key,
                ),
                kind="google.oauth-client-import.apply",
                expected_generation=plan.expected_generation,
            )
            if applied.plan_digest != plan.plan_digest:
                raise MasterjetClientError("control.response_invalid")
            if applied.state in {"partial", "failed", "blocked"}:
                return GoogleOAuthClientImportResult(
                    account_ref=account.ref,
                    generation=receipt.generation,
                    status=applied.state,
                )
            if applied.state != "succeeded" or applied.resulting_generation != receipt.generation:
                raise MasterjetClientError("control.response_invalid")
            return GoogleOAuthClientImportResult(
                account_ref=account.ref,
                generation=receipt.generation,
                status=applied.state,
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
