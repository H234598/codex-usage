from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from .account_lock import AccountLockError, account_lock
from .masterjet_client import MasterjetClientError
from .masterjet_contracts import (
    ControlOperation,
    SecretIngressReceipt,
    SecretIngressSession,
)
from .models import Account
from .private_io import open_verified_state_home, read_private_bytes_at

MAX_AUTH_JSON_BYTES = 1_000_000


class AuthenticatedControlClient(Protocol):
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


class AuthSyncError(RuntimeError):
    """Redacted failure from canonical auth synchronization."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AuthSyncResult:
    account_ref: str
    generation: int
    status: str


def sync_account_auth(
    account: Account,
    client: AuthenticatedControlClient,
) -> AuthSyncResult:
    failure: str | None = None
    try:
        return _sync_account_auth(account, client)
    except MasterjetClientError as exc:
        failure = exc.code
    except (AccountLockError, OSError, TypeError, ValueError):
        failure = "credential.source_unavailable"
    except Exception:
        failure = "control.transport_unavailable"
    if failure is None:  # pragma: no cover - defensive control-flow assertion
        failure = "credential.source_unavailable"
    raise AuthSyncError(failure)


def _sync_account_auth(
    account: Account,
    client: AuthenticatedControlClient,
) -> AuthSyncResult:
    auth_path = _canonical_auth_path(account)
    with account_lock(account.id):
        plan = client.call(
            "openai.auth-sync.plan",
            {"account_ref": account.id},
            idempotency_key=_idempotency_key(),
        )
        if type(plan) is not ControlOperation:
            raise MasterjetClientError("control.response_invalid")
        plan = cast(ControlOperation, plan)
        if (
            plan.kind != "openai.auth-sync.plan"
            or plan.state != "planned"
            or plan.resulting_generation is not None
        ):
            raise MasterjetClientError("control.response_invalid")

        secret = _read_auth_json(auth_path)
        try:
            session = client.call(
                "secret.ingress.create",
                {
                    "account_ref": account.id,
                    "credential_type": "openai_auth_json",
                    "plan_id": plan.id,
                },
                expected_generation=plan.expected_generation,
                idempotency_key=_idempotency_key(),
            )
            if type(session) is not SecretIngressSession:
                raise MasterjetClientError("control.response_invalid")
            session = cast(SecretIngressSession, session)
            if (
                session.account_ref != account.id
                or session.plan_id != plan.id
                or session.expected_generation != plan.expected_generation
            ):
                raise MasterjetClientError("control.response_invalid")

            receipt = client.put_secret(
                session.id,
                secret,
                expected_generation=plan.expected_generation,
                idempotency_key=_idempotency_key(),
            )
            if type(receipt) is not SecretIngressReceipt:
                raise MasterjetClientError("control.response_invalid")
            receipt = cast(SecretIngressReceipt, receipt)
            if (
                receipt.session_id != session.id
                or receipt.account_ref != account.id
                or receipt.state != "consumed"
            ):
                raise MasterjetClientError("control.response_invalid")

            applied = client.call(
                "openai.auth-sync.apply",
                {"account_ref": account.id, "plan_id": plan.id},
                expected_generation=plan.expected_generation,
                idempotency_key=_idempotency_key(),
            )
            if type(applied) is not ControlOperation:
                raise MasterjetClientError("control.response_invalid")
            applied = cast(ControlOperation, applied)
            if (
                applied.kind != "openai.auth-sync.apply"
                or applied.state != "succeeded"
                or applied.expected_generation != plan.expected_generation
                or applied.resulting_generation != receipt.generation
            ):
                raise MasterjetClientError("control.response_invalid")
            return AuthSyncResult(
                account_ref=account.id,
                generation=receipt.generation,
                status=applied.state,
            )
        finally:
            _zero_secret(secret)


def _canonical_auth_path(account: Account) -> Path:
    if type(account) is not Account or type(account.profile_dir) is not str:
        raise ValueError("invalid account")
    if type(account.auth_json_path) is not str:
        raise ValueError("missing canonical auth path")
    profile = Path(account.profile_dir)
    auth = Path(account.auth_json_path)
    expected = profile / "codex-home" / "auth.json"
    if (
        not profile.is_absolute()
        or not auth.is_absolute()
        or any(part in {"", ".", ".."} for part in profile.parts[1:])
        or any(part in {"", ".", ".."} for part in auth.parts[1:])
        or os.fspath(auth) != os.fspath(expected)
    ):
        raise ValueError("auth path is not canonical")
    return auth


def _read_auth_json(auth_path: Path) -> bytearray:
    parent_fd = open_verified_state_home(auth_path.parent)
    try:
        payload, _identity = read_private_bytes_at(
            parent_fd,
            auth_path.name,
            maximum=MAX_AUTH_JSON_BYTES,
            mode=0o600,
        )
    finally:
        os.close(parent_fd)
    if not payload:
        raise ValueError("auth source is empty")
    secret = bytearray(payload)
    del payload
    return secret


def _idempotency_key() -> str:
    try:
        return str(uuid.uuid4())
    except Exception:
        raise MasterjetClientError("control.request_invalid") from None


def _zero_secret(secret: bytearray) -> None:
    for index in range(len(secret)):
        secret[index] = 0
