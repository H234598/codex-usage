from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from .integration_snapshot import (
    _ACCOUNT_ID_RE,
    IntegrationInvalidSource,
    _canonical_document_v2,
    _scan_secrets,
)
from .json_utils import loads_strict
from .private_io import IntegrationEvidenceInvalid

POOL_AUTHORITY_SOURCE_FILENAME = "pool-authority-source-v2.json"
POOL_AUTHORITY_FILENAME = "pool-authority-v2.json"
POOL_AUTHORITY_SOURCE_SCHEMA_VERSION = 2
POOL_AUTHORITY_SCHEMA_VERSION = 2
PRODUCER_VERSION = "0.6.537"
POOL_AUTHORITY_SOURCE_MAX_BYTES = 128 * 1024
POOL_AUTHORITY_MAX_BYTES = 256 * 1024
POOL_AUTHORITY_MAX_ENTRIES = 256
POOL_AUTHORITY_TTL = timedelta(minutes=15)

_SOURCE_FIELDS = frozenset(("authorities", "pool_authority_source_schema_version"))
_AUTHORITY_FIELDS = frozenset(
    (
        "account_id",
        "allowed_lifecycles",
        "allowed_model_families",
        "hive_available",
        "long_running_leadership_eligible",
        "persistent_leadership_eligible",
        "pool_id",
        "provider",
        "reasoning_maximum",
        "reasoning_minimum",
    )
)
_PROJECTION_FIELDS = frozenset(
    (
        "authorities",
        "expires_at",
        "generation_id",
        "issued_at",
        "pool_authority_schema_version",
        "producer_version",
        "release_id",
        "usage_binding_sha256",
        "usage_payload_sha256",
    )
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_GENERATION_ID_RE = re.compile(r"[0-9a-f]{32}")
_RELEASE_ID_RE = re.compile(r"0\.6\.537-[0-9a-f]{16}")
_POOL_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_PROVIDER_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_MODEL_FAMILY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_REASONING_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")
_LIFECYCLES = frozenset(("ephemeral", "session", "persistent"))


class PoolAuthorityInvalid(IntegrationEvidenceInvalid):
    pass


@dataclass(frozen=True)
class PoolAuthorityRequest:
    account_id: str
    pool_id: str
    provider: str
    model_family: str
    reasoning: str
    lifecycle: str
    require_persistent_leadership: bool
    require_long_running_leadership: bool


def _invalid() -> None:
    raise PoolAuthorityInvalid()


def _exact_object(value: object, fields: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict:
        _invalid()
    mapping = cast(dict[object, object], value)
    if len(mapping) != len(fields) or set(mapping) != fields:
        _invalid()
    if any(type(key) is not str for key in mapping):
        _invalid()
    return cast(dict[str, object], mapping)


def _timestamp(value: object) -> tuple[str, datetime]:
    if type(value) is not str or not 20 <= len(value) <= 32 or "T" not in value:
        _invalid()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        _invalid()
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _invalid()
    parsed = parsed.astimezone(UTC)
    canonical = parsed.isoformat().replace("+00:00", "Z")
    return canonical, parsed


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid()
    return value


def _generation_id(value: object) -> str:
    if type(value) is not str or _GENERATION_ID_RE.fullmatch(value) is None:
        _invalid()
    return value


def _release_id(value: object) -> str:
    if type(value) is not str or _RELEASE_ID_RE.fullmatch(value) is None:
        _invalid()
    return value


def _bounded_id(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _invalid()
    return value


def _closed_strings(
    value: object,
    *,
    pattern: re.Pattern[str] | None = None,
    allowed: frozenset[str] | None = None,
    maximum: int = 32,
) -> list[str]:
    if type(value) is not list or not 1 <= len(value) <= maximum:
        _invalid()
    values = cast(list[object], value)
    if any(type(item) is not str for item in values):
        _invalid()
    strings = cast(list[str], values)
    if len(set(strings)) != len(strings) or strings != sorted(strings):
        _invalid()
    if pattern is not None and any(pattern.fullmatch(item) is None for item in strings):
        _invalid()
    if allowed is not None and any(item not in allowed for item in strings):
        _invalid()
    return list(strings)


def _canonical_authority(value: object) -> dict[str, object]:
    authority = _exact_object(value, _AUTHORITY_FIELDS)
    account_id = authority["account_id"]
    if (
        type(account_id) is not str
        or account_id in {".", ".."}
        or _ACCOUNT_ID_RE.fullmatch(account_id) is None
    ):
        _invalid()
    minimum = authority["reasoning_minimum"]
    maximum = authority["reasoning_maximum"]
    if (
        type(minimum) is not str
        or type(maximum) is not str
        or minimum not in _REASONING_LEVELS
        or maximum not in _REASONING_LEVELS
        or _REASONING_LEVELS.index(minimum) > _REASONING_LEVELS.index(maximum)
    ):
        _invalid()
    for field in (
        "hive_available",
        "persistent_leadership_eligible",
        "long_running_leadership_eligible",
    ):
        if type(authority[field]) is not bool:
            _invalid()
    return {
        "account_id": account_id,
        "allowed_lifecycles": _closed_strings(
            authority["allowed_lifecycles"], allowed=_LIFECYCLES
        ),
        "allowed_model_families": _closed_strings(
            authority["allowed_model_families"], pattern=_MODEL_FAMILY_RE
        ),
        "hive_available": authority["hive_available"],
        "long_running_leadership_eligible": authority[
            "long_running_leadership_eligible"
        ],
        "persistent_leadership_eligible": authority[
            "persistent_leadership_eligible"
        ],
        "pool_id": _bounded_id(authority["pool_id"], _POOL_ID_RE),
        "provider": _bounded_id(authority["provider"], _PROVIDER_RE),
        "reasoning_maximum": maximum,
        "reasoning_minimum": minimum,
    }


def _canonical_authorities(value: object) -> list[dict[str, object]]:
    if type(value) is not list or len(value) > POOL_AUTHORITY_MAX_ENTRIES:
        _invalid()
    authorities = [_canonical_authority(item) for item in cast(list[object], value)]
    keys = [
        (item["account_id"], item["provider"], item["pool_id"])
        for item in authorities
    ]
    if len(set(keys)) != len(keys) or keys != sorted(keys):
        _invalid()
    return authorities


def _canonical_source(value: object) -> dict[str, object]:
    source = _exact_object(value, _SOURCE_FIELDS)
    if (
        type(source["pool_authority_source_schema_version"]) is not int
        or source["pool_authority_source_schema_version"]
        != POOL_AUTHORITY_SOURCE_SCHEMA_VERSION
    ):
        _invalid()
    canonical = {
        "authorities": _canonical_authorities(source["authorities"]),
        "pool_authority_source_schema_version": POOL_AUTHORITY_SOURCE_SCHEMA_VERSION,
    }
    try:
        _scan_secrets(canonical)
    except IntegrationInvalidSource as exc:
        raise PoolAuthorityInvalid() from exc
    return canonical


def _json_bytes(value: dict[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        _invalid()


def serialize_pool_authority_source(source: object) -> bytes:
    payload = _json_bytes(_canonical_source(source)) + b"\n"
    if not 1 <= len(payload) <= POOL_AUTHORITY_SOURCE_MAX_BYTES:
        _invalid()
    return payload


def parse_pool_authority_source(payload: bytes) -> dict[str, object]:
    if (
        type(payload) is not bytes
        or not 1 <= len(payload) <= POOL_AUTHORITY_SOURCE_MAX_BYTES
        or not payload.endswith(b"\n")
    ):
        _invalid()
    try:
        source = _canonical_source(loads_strict(payload))
    except PoolAuthorityInvalid:
        raise
    except (TypeError, ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise PoolAuthorityInvalid() from exc
    if serialize_pool_authority_source(source) != payload:
        _invalid()
    return source


def _canonical_projection(value: object) -> dict[str, object]:
    projection = _exact_object(value, _PROJECTION_FIELDS)
    if (
        type(projection["pool_authority_schema_version"]) is not int
        or projection["pool_authority_schema_version"] != POOL_AUTHORITY_SCHEMA_VERSION
        or projection["producer_version"] != PRODUCER_VERSION
    ):
        _invalid()
    issued_at, issued = _timestamp(projection["issued_at"])
    expires_at, expires = _timestamp(projection["expires_at"])
    if not issued < expires <= issued + POOL_AUTHORITY_TTL:
        _invalid()
    canonical = {
        "authorities": _canonical_authorities(projection["authorities"]),
        "expires_at": expires_at,
        "generation_id": _generation_id(projection["generation_id"]),
        "issued_at": issued_at,
        "pool_authority_schema_version": POOL_AUTHORITY_SCHEMA_VERSION,
        "producer_version": PRODUCER_VERSION,
        "release_id": _release_id(projection["release_id"]),
        "usage_binding_sha256": _digest(projection["usage_binding_sha256"]),
        "usage_payload_sha256": _digest(projection["usage_payload_sha256"]),
    }
    try:
        _scan_secrets(canonical)
    except IntegrationInvalidSource as exc:
        raise PoolAuthorityInvalid() from exc
    return canonical


def serialize_pool_authority_projection(projection: object) -> bytes:
    payload = _json_bytes(_canonical_projection(projection))
    if not 1 <= len(payload) <= POOL_AUTHORITY_MAX_BYTES:
        _invalid()
    return payload


def parse_pool_authority_projection(payload: bytes) -> dict[str, object]:
    if type(payload) is not bytes or not 1 <= len(payload) <= POOL_AUTHORITY_MAX_BYTES:
        _invalid()
    try:
        projection = _canonical_projection(loads_strict(payload))
    except PoolAuthorityInvalid:
        raise
    except (TypeError, ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise PoolAuthorityInvalid() from exc
    if serialize_pool_authority_projection(projection) != payload:
        _invalid()
    return projection


def build_pool_authority_projection(
    *,
    source: object,
    usage_document: object,
    generation_id: str,
    release_id: str,
    usage_payload_sha256: str,
    usage_binding_sha256: str,
) -> dict[str, object]:
    source_document = _canonical_source(source)
    try:
        usage = _canonical_document_v2(usage_document)
    except IntegrationInvalidSource as exc:
        raise PoolAuthorityInvalid() from exc
    issued_at, issued = _timestamp(usage["generated_at"])
    usage_accounts = cast(list[dict[str, object]], usage["accounts"])
    account_ids = {cast(str, item["account_id"]) for item in usage_accounts}
    source_accounts = {
        cast(str, item["account_id"])
        for item in cast(list[dict[str, object]], source_document["authorities"])
    }
    if source_accounts != account_ids:
        _invalid()
    expirations = [issued + POOL_AUTHORITY_TTL]
    account_usable: dict[str, bool] = {}
    for account in usage_accounts:
        freshness = cast(dict[str, object], account["freshness"])
        _captured_text, captured = _timestamp(freshness["captured_at"])
        _fresh_text, fresh_until = _timestamp(freshness["fresh_until"])
        usable = not (
            account["status"] != "ok"
            or freshness["stale"] is not False
            or captured > issued
            or fresh_until <= issued
        )
        account_usable[cast(str, account["account_id"])] = usable
        if usable:
            expirations.append(fresh_until)
    expires = min(expirations)
    authorities = []
    for source_authority in cast(
        list[dict[str, object]], source_document["authorities"]
    ):
        authority = dict(source_authority)
        if not account_usable[cast(str, authority["account_id"])]:
            authority["hive_available"] = False
            authority["persistent_leadership_eligible"] = False
            authority["long_running_leadership_eligible"] = False
        authorities.append(authority)
    projection = {
        "authorities": authorities,
        "expires_at": expires.isoformat().replace("+00:00", "Z"),
        "generation_id": _generation_id(generation_id),
        "issued_at": issued_at,
        "pool_authority_schema_version": POOL_AUTHORITY_SCHEMA_VERSION,
        "producer_version": PRODUCER_VERSION,
        "release_id": _release_id(release_id),
        "usage_binding_sha256": _digest(usage_binding_sha256),
        "usage_payload_sha256": _digest(usage_payload_sha256),
    }
    return _canonical_projection(projection)


def _valid_request(request: object) -> PoolAuthorityRequest:
    if type(request) is not PoolAuthorityRequest:
        _invalid()
    if (
        request.account_id in {".", ".."}
        or _ACCOUNT_ID_RE.fullmatch(request.account_id) is None
        or _POOL_ID_RE.fullmatch(request.pool_id) is None
        or _PROVIDER_RE.fullmatch(request.provider) is None
        or _MODEL_FAMILY_RE.fullmatch(request.model_family) is None
        or request.reasoning not in _REASONING_LEVELS
        or request.lifecycle not in _LIFECYCLES
        or type(request.require_persistent_leadership) is not bool
        or type(request.require_long_running_leadership) is not bool
    ):
        _invalid()
    return request


def evaluate_pool_authority(
    payload: bytes,
    request: PoolAuthorityRequest,
    *,
    now: datetime,
    expected_release_id: str,
    expected_generation_id: str,
    expected_usage_payload_sha256: str,
    expected_usage_binding_sha256: str,
) -> bool:
    """Return True only for one exact, fresh, cryptographically bound claim."""
    try:
        projection = parse_pool_authority_projection(payload)
        requested = _valid_request(request)
        if (
            type(now) is not datetime
            or now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or projection["release_id"] != _release_id(expected_release_id)
            or projection["generation_id"] != _generation_id(expected_generation_id)
            or projection["usage_payload_sha256"]
            != _digest(expected_usage_payload_sha256)
            or projection["usage_binding_sha256"]
            != _digest(expected_usage_binding_sha256)
        ):
            return False
        _issued_text, issued = _timestamp(projection["issued_at"])
        _expires_text, expires = _timestamp(projection["expires_at"])
        evaluated_at = now.astimezone(UTC)
        if evaluated_at < issued or evaluated_at >= expires:
            return False
        matches = [
            item
            for item in cast(list[dict[str, object]], projection["authorities"])
            if item["account_id"] == requested.account_id
            and item["pool_id"] == requested.pool_id
            and item["provider"] == requested.provider
        ]
        if len(matches) != 1:
            return False
        authority = matches[0]
        minimum = cast(str, authority["reasoning_minimum"])
        maximum = cast(str, authority["reasoning_maximum"])
        if (
            authority["hive_available"] is not True
            or requested.model_family not in authority["allowed_model_families"]
            or requested.lifecycle not in authority["allowed_lifecycles"]
            or not _REASONING_LEVELS.index(minimum)
            <= _REASONING_LEVELS.index(requested.reasoning)
            <= _REASONING_LEVELS.index(maximum)
            or (
                (requested.lifecycle == "persistent" or requested.require_persistent_leadership)
                and authority["persistent_leadership_eligible"] is not True
            )
            or (
                requested.require_long_running_leadership
                and authority["long_running_leadership_eligible"] is not True
            )
        ):
            return False
        return True
    except (PoolAuthorityInvalid, TypeError, ValueError, IndexError, KeyError):
        return False
