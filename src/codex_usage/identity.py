from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .extractor import JsonCandidate

MAX_BACKEND_ID_CHARS = 256


def backend_identity_from_payload(
    payload: Any,
) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    return (
        _identity_value(payload.get("user_id")),
        _identity_value(payload.get("account_id")),
    )


def backend_identity_from_candidates(
    candidates: Iterable[JsonCandidate],
) -> tuple[str | None, str | None]:
    user_id: str | None = None
    account_id: str | None = None
    for candidate in candidates:
        candidate_user_id, candidate_account_id = backend_identity_from_payload(
            candidate.payload
        )
        user_id = user_id or candidate_user_id
        account_id = account_id or candidate_account_id
        if user_id is not None and account_id is not None:
            break
    return user_id, account_id


def _identity_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value or len(value) > MAX_BACKEND_ID_CHARS:
        return None
    return value
