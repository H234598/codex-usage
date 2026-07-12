from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from .extractor import JsonCandidate

MAX_BACKEND_ID_CHARS = 256
MAX_BACKEND_PLAN_TYPE_CHARS = 64


def backend_identity_from_payload(
    payload: Any,
) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    return (
        _identity_value(payload.get("user_id")),
        _identity_value(payload.get("account_id")),
    )


def backend_plan_type_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    return _plan_type_value(payload.get("plan_type"))


def backend_identity_from_candidates(
    candidates: Iterable[JsonCandidate],
) -> tuple[str | None, str | None]:
    ordered_candidates = sorted(
        enumerate(candidates),
        key=lambda item: (_candidate_priority(item[1]), -item[0]),
    )
    for _candidate_index, candidate in ordered_candidates:
        identity = backend_identity_from_payload(candidate.payload)
        if identity == (None, None):
            continue
        return identity
    return None, None


def backend_plan_type_from_candidates(
    candidates: Iterable[JsonCandidate],
) -> str | None:
    ordered_candidates = sorted(
        enumerate(candidates),
        key=lambda item: (_candidate_priority(item[1]), -item[0]),
    )
    for _candidate_index, candidate in ordered_candidates:
        plan_type = backend_plan_type_from_payload(candidate.payload)
        if plan_type is not None:
            return plan_type
    return None


def _candidate_priority(candidate: JsonCandidate) -> int:
    path = urlsplit(candidate.url).path.rstrip("/").lower()
    if path == "/backend-api/wham/usage":
        return 0
    if path.startswith("/backend-api/wham/usage/"):
        return 1
    payload = candidate.payload
    if isinstance(payload, dict) and any(
        key in payload for key in ("rate_limit", "rateLimits", "rateLimitsByLimitId")
    ):
        return 1
    return 2


def _identity_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value or len(value) > MAX_BACKEND_ID_CHARS:
        return None
    return value


def _plan_type_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_BACKEND_PLAN_TYPE_CHARS:
        return None
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return None
    return value
