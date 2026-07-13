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


def select_identity_consistent_candidates(
    candidates: Iterable[JsonCandidate],
    *,
    auth_user_id: str | None,
    auth_account_id: str | None,
) -> list[JsonCandidate]:
    """Keep structured responses from one backend account together."""
    candidate_list = list(candidates)
    account_ids_by_user: dict[str, set[str]] = {}
    user_ids_by_account: dict[str, set[str]] = {}
    known_account_ids: set[str] = set()
    for candidate in candidate_list:
        user_id, account_id = backend_identity_from_payload(candidate.payload)
        if account_id:
            known_account_ids.add(account_id)
        if user_id and account_id:
            account_ids_by_user.setdefault(user_id, set()).add(account_id)
            user_ids_by_account.setdefault(account_id, set()).add(user_id)

    def is_ambiguous_partial_identity(identity: tuple[str | None, str | None]) -> bool:
        user_id, account_id = identity
        if user_id and not account_id:
            user_account_ids = account_ids_by_user.get(user_id, set())
            return (
                len(user_account_ids) > 1
                or (
                    bool(known_account_ids - user_account_ids)
                    if user_account_ids
                    else bool(known_account_ids)
                    and (
                        auth_account_id is None
                        or any(account != auth_account_id for account in known_account_ids)
                    )
                )
            )
        if account_id and not user_id:
            return len(user_ids_by_account.get(account_id, set())) > 1
        return False

    groups: list[
        tuple[tuple[str | None, str | None], list[JsonCandidate]]
    ] = []
    skipped_ambiguous_partial = False
    for candidate in candidate_list:
        identity = backend_identity_from_payload(candidate.payload)
        if identity == (None, None):
            continue
        if is_ambiguous_partial_identity(identity):
            skipped_ambiguous_partial = True
            continue
        for index, (group_identity, grouped_candidates) in enumerate(groups):
            if not _identities_compatible(identity, group_identity):
                continue
            groups[index] = (
                (
                    identity[0] or group_identity[0],
                    identity[1] or group_identity[1],
                ),
                [*grouped_candidates, candidate],
            )
            break
        else:
            groups.append((identity, [candidate]))
    if skipped_ambiguous_partial and not (auth_user_id or auth_account_id):
        raise ValueError("backend response contains multiple backend accounts")
    if not groups:
        return candidate_list
    if len(groups) == 1:
        if (auth_user_id or auth_account_id) and not _response_identity_matches_auth(
            backend_user_id=groups[0][0][0],
            backend_account_id=groups[0][0][1],
            auth_user_id=auth_user_id,
            auth_account_id=auth_account_id,
        ):
            raise ValueError("backend response belongs to a different account")
        return groups[0][1]
    if not (auth_user_id or auth_account_id):
        raise ValueError("backend response contains multiple backend accounts")

    matching_groups = [
        grouped_candidates
        for identity, grouped_candidates in groups
        if _response_identity_matches_auth(
            backend_user_id=identity[0],
            backend_account_id=identity[1],
            auth_user_id=auth_user_id,
            auth_account_id=auth_account_id,
        )
    ]
    if len(matching_groups) == 0:
        raise ValueError("backend response belongs to a different account")
    if len(matching_groups) > 1:
        raise ValueError("backend response does not identify one account")
    return matching_groups[0]


def _identities_compatible(
    left: tuple[str | None, str | None],
    right: tuple[str | None, str | None],
) -> bool:
    shared_field = False
    for left_value, right_value in zip(left, right, strict=True):
        if left_value is None or right_value is None:
            continue
        shared_field = True
        if left_value != right_value:
            return False
    return shared_field


def _response_identity_matches_auth(
    *,
    backend_user_id: str | None,
    backend_account_id: str | None,
    auth_user_id: str | None,
    auth_account_id: str | None,
) -> bool:
    if backend_account_id and auth_account_id:
        accepted_account_ids = {auth_account_id}
        if auth_user_id:
            accepted_account_ids.add(auth_user_id)
        return backend_account_id in accepted_account_ids
    if backend_account_id and auth_user_id:
        return backend_account_id == auth_user_id
    if auth_user_id and backend_user_id and backend_user_id != auth_user_id:
        return False
    return True


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
