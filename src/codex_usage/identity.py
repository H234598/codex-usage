from __future__ import annotations

from collections.abc import Iterable
from itertools import islice
from typing import Any
from urllib.parse import urlsplit

from .extractor import MAX_JSON_CANDIDATES, JsonCandidate

MAX_BACKEND_ID_CHARS = 256
MAX_BACKEND_PLAN_TYPE_CHARS = 64


def backend_identity_from_payload(
    payload: Any,
) -> tuple[str | None, str | None]:
    if type(payload) is not dict:
        return None, None
    return (
        _identity_value(payload.get("user_id"), field="user_id"),
        _identity_value(payload.get("account_id"), field="account_id"),
    )


def select_identity_consistent_candidates(
    candidates: Iterable[JsonCandidate],
    *,
    auth_user_id: str | None,
    auth_account_id: str | None,
) -> list[JsonCandidate]:
    """Keep structured responses from one backend account together."""
    auth_user_id = _auth_identity_value(auth_user_id, field="auth_user_id")
    auth_account_id = _auth_identity_value(auth_account_id, field="auth_account_id")
    candidate_list = _usable_candidates(candidates)
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
            if auth_account_id:
                # A shared user ID cannot distinguish two configured accounts.
                # Do not mix a user-only limit response into an account-ID
                # authenticated response, even when no foreign account ID was
                # captured in the same browser batch.
                return True
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
        matching_indices = [
            index
            for index, (group_identity, _grouped_candidates) in enumerate(groups)
            if _identities_compatible(identity, group_identity)
        ]
        if not matching_indices:
            groups.append((identity, [candidate]))
            continue
        merged_identity = identity
        merged_candidates = [candidate]
        for index in reversed(matching_indices):
            group_identity, grouped_candidates = groups.pop(index)
            merged_identity = (
                merged_identity[0] or group_identity[0],
                merged_identity[1] or group_identity[1],
            )
            merged_candidates[0:0] = grouped_candidates
        groups.insert(
            matching_indices[0],
            (merged_identity, merged_candidates),
        )
    if skipped_ambiguous_partial and not (auth_user_id or auth_account_id):
        raise ValueError("backend response contains multiple backend accounts")
    if not groups:
        if skipped_ambiguous_partial and (auth_user_id or auth_account_id):
            return []
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
        (identity, grouped_candidates)
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
    if auth_account_id:
        exact_groups = [
            grouped_candidates
            for identity, grouped_candidates in matching_groups
            if identity[1] == auth_account_id
        ]
        if len(exact_groups) == 1:
            return exact_groups[0]
        if exact_groups:
            raise ValueError("backend response does not identify one account")
    if len(matching_groups) > 1:
        raise ValueError("backend response does not identify one account")
    return matching_groups[0][1]


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
    if any(
        value is not None and type(value) is not str
        for value in (
            backend_user_id,
            backend_account_id,
            auth_user_id,
            auth_account_id,
        )
    ):
        return False
    if backend_account_id and auth_account_id:
        if backend_account_id == auth_account_id:
            return (
                not backend_user_id
                or not auth_user_id
                or backend_user_id == auth_user_id
            )
        if backend_account_id == auth_user_id:
            return not backend_user_id or backend_user_id == auth_user_id
        return False
    if backend_account_id and auth_user_id:
        return (
            backend_account_id == auth_user_id
            and (not backend_user_id or backend_user_id == auth_user_id)
        )
    if auth_user_id and backend_user_id and backend_user_id != auth_user_id:
        return False
    return True


def backend_plan_type_from_payload(payload: Any) -> str | None:
    if type(payload) is not dict:
        return None
    return _plan_type_value(payload.get("plan_type"))


def backend_identity_from_candidates(
    candidates: Iterable[JsonCandidate],
) -> tuple[str | None, str | None]:
    candidate_list = _usable_candidates(candidates)
    ordered_candidates = sorted(
        (
            (index, candidate)
            for index, candidate in enumerate(candidate_list)
        ),
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
    candidate_list = _usable_candidates(candidates)
    ordered_candidates = sorted(
        (
            (index, candidate)
            for index, candidate in enumerate(candidate_list)
        ),
        key=lambda item: (_candidate_priority(item[1]), -item[0]),
    )
    for _candidate_index, candidate in ordered_candidates:
        plan_type = backend_plan_type_from_payload(candidate.payload)
        if plan_type is not None:
            return plan_type
    return None


def _candidate_priority(candidate: JsonCandidate) -> int:
    try:
        path = urlsplit(candidate.url).path.rstrip("/").lower()
    except Exception:
        return 2
    if path == "/backend-api/wham/usage":
        return 0
    if path.startswith("/backend-api/wham/usage/"):
        return 1
    payload = candidate.payload
    if type(payload) is dict and any(
        key in payload for key in ("rate_limit", "rateLimits", "rateLimitsByLimitId")
    ):
        return 1
    return 2


def _candidate_is_usable(candidate: Any) -> bool:
    if (
        type(candidate) is not JsonCandidate
        or type(candidate.url) is not str
        or not candidate.url.strip()
    ):
        return False
    try:
        urlsplit(candidate.url)
    except (TypeError, ValueError):
        return False
    return True


def _usable_candidates(candidates: Iterable[JsonCandidate]) -> list[JsonCandidate]:
    try:
        bounded = list(islice(candidates, MAX_JSON_CANDIDATES + 1))
    except Exception:
        return []
    if len(bounded) > MAX_JSON_CANDIDATES:
        return []
    return [candidate for candidate in bounded if _candidate_is_usable(candidate)]


def _identity_value(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(f"backend response {field} is invalid")
    if not value or len(value) > MAX_BACKEND_ID_CHARS or any(
        char.isspace() or ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
        for char in value
    ):
        raise ValueError(f"backend response {field} is invalid")
    return value


def _auth_identity_value(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > MAX_BACKEND_ID_CHARS or any(
        char.isspace() or ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
        for char in value
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _plan_type_value(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > MAX_BACKEND_PLAN_TYPE_CHARS:
        raise ValueError("backend response plan_type is invalid")
    if any(
        char.isspace() or ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
        for char in value
    ):
        raise ValueError("backend response plan_type is invalid")
    return value
