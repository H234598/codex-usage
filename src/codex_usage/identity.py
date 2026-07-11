from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

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
    partial: tuple[str | None, str | None] = (None, None)
    ordered_candidates = sorted(
        enumerate(candidates),
        key=lambda item: (_candidate_priority(item[1]), -item[0]),
    )
    for _candidate_index, candidate in ordered_candidates:
        identity = backend_identity_from_payload(candidate.payload)
        if identity == (None, None):
            continue
        if identity[0] is not None and identity[1] is not None:
            return identity
        if partial == (None, None):
            partial = identity
    return partial


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
