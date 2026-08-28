from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

_SCHEMA_VERSION = 1
_MAX_ACCOUNTS = 256
_MAX_REASON_CODES = 64
_MAX_TEXT_CHARS = 512
_MAX_DETAIL_CHARS = 2_048
_MAX_GENERATION = 2**63 - 1
_REF_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63}){0,7}$")
_VISIBLE_NAME_RE = re.compile(r"^[A-Za-z]+(?:[ -][A-Za-z]+)*$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PLAN_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_OPERATION_STATES = frozenset(
    {"planned", "queued", "running", "partial", "succeeded", "failed", "blocked"}
)
_PROBLEM_SEVERITIES = frozenset({"info", "warning", "error", "critical"})


class ControlContractError(ValueError):
    """Response violates Codex Usage's redacted control contract."""


@dataclass(frozen=True, slots=True)
class OpenAIControlAccount:
    ref: str
    label: str
    enabled: bool
    auth_state: str
    access_expires_at: datetime | None
    credential_generation: int
    vault_projection_state: str
    usage_state: str


@dataclass(frozen=True, slots=True)
class GoogleControlAccount:
    ref: str
    label: str
    enabled: bool
    subject_bound: bool
    oauth_state: str
    inventory_generation: int
    quota_state: str
    project_count: int
    billing_count: int
    reload_state: str


@dataclass(frozen=True, slots=True)
class GoogleControlProject:
    ref: str
    project_name: str
    key_name: str
    billing_ref: str | None
    status: str
    probe_state: str
    quota_state: str


@dataclass(frozen=True, slots=True)
class ControlOperation:
    id: str
    kind: str
    state: str
    expected_generation: int
    resulting_generation: int | None
    plan_digest: str
    created_at: datetime
    expires_at: datetime
    completed_count: int
    failed_count: int
    not_attempted_count: int
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ControlProblem:
    code: str
    severity: str
    title: str
    detail: str
    effect: str
    action: str
    retryable: bool
    retry_after_seconds: int | None
    correlation_id: str
    occurred_at: datetime


def parse_google_accounts(payload: object) -> tuple[GoogleControlAccount, ...]:
    data = _document(payload, {"schema_version", "accounts"})
    accounts = _list(data["accounts"], "accounts", _MAX_ACCOUNTS)
    result = tuple(_parse_google_account(account) for account in accounts)
    _unique_refs(result)
    return result


def parse_openai_accounts(payload: object) -> tuple[OpenAIControlAccount, ...]:
    data = _document(payload, {"schema_version", "accounts"})
    accounts = _list(data["accounts"], "accounts", _MAX_ACCOUNTS)
    result = tuple(_parse_openai_account(account) for account in accounts)
    _unique_refs(result)
    return result


def parse_google_project(payload: object) -> GoogleControlProject:
    data = _mapping(
        payload,
        {
            "ref",
            "project_name",
            "key_name",
            "billing_ref",
            "status",
            "probe_state",
            "quota_state",
        },
    )
    return GoogleControlProject(
        ref=_ref(data["ref"], "ref"),
        project_name=_visible_name(data["project_name"], "project_name"),
        key_name=_visible_name(data["key_name"], "key_name"),
        billing_ref=_optional_ref(data["billing_ref"], "billing_ref"),
        status=_code(data["status"], "status"),
        probe_state=_code(data["probe_state"], "probe_state"),
        quota_state=_code(data["quota_state"], "quota_state"),
    )


def parse_control_operation(payload: object) -> ControlOperation:
    data = _document(
        payload,
        {
            "schema_version",
            "id",
            "kind",
            "state",
            "expected_generation",
            "resulting_generation",
            "plan_digest",
            "created_at",
            "expires_at",
            "completed_count",
            "failed_count",
            "not_attempted_count",
            "reason_codes",
        },
    )
    state = _code(data["state"], "state")
    if state not in _OPERATION_STATES:
        _invalid("state")
    operation = ControlOperation(
        id=_ref(data["id"], "id"),
        kind=_code(data["kind"], "kind"),
        state=state,
        expected_generation=_non_negative_int(data["expected_generation"], "expected_generation"),
        resulting_generation=_optional_non_negative_int(
            data["resulting_generation"], "resulting_generation"
        ),
        plan_digest=_plan_digest(data["plan_digest"]),
        created_at=_timestamp(data["created_at"], "created_at"),
        expires_at=_timestamp(data["expires_at"], "expires_at"),
        completed_count=_non_negative_int(data["completed_count"], "completed_count"),
        failed_count=_non_negative_int(data["failed_count"], "failed_count"),
        not_attempted_count=_non_negative_int(
            data["not_attempted_count"], "not_attempted_count"
        ),
        reason_codes=_reason_codes(data["reason_codes"]),
    )
    if operation.expires_at <= operation.created_at:
        _invalid("expires_at")
    return operation


def parse_control_problem(payload: object) -> ControlProblem:
    data = _document(
        payload,
        {
            "schema_version",
            "code",
            "severity",
            "title",
            "detail",
            "effect",
            "action",
            "retryable",
            "retry_after_seconds",
            "correlation_id",
            "occurred_at",
        },
    )
    severity = _code(data["severity"], "severity")
    if severity not in _PROBLEM_SEVERITIES:
        _invalid("severity")
    return ControlProblem(
        code=_code(data["code"], "code"),
        severity=severity,
        title=_text(data["title"], "title"),
        detail=_text(data["detail"], "detail", _MAX_DETAIL_CHARS),
        effect=_text(data["effect"], "effect"),
        action=_text(data["action"], "action"),
        retryable=_bool(data["retryable"], "retryable"),
        retry_after_seconds=_optional_non_negative_int(
            data["retry_after_seconds"], "retry_after_seconds"
        ),
        correlation_id=_ref(data["correlation_id"], "correlation_id"),
        occurred_at=_timestamp(data["occurred_at"], "occurred_at"),
    )


def _parse_google_account(payload: object) -> GoogleControlAccount:
    data = _mapping(
        payload,
        {
            "ref",
            "label",
            "enabled",
            "subject_bound",
            "oauth_state",
            "inventory_generation",
            "quota_state",
            "project_count",
            "billing_count",
            "reload_state",
        },
    )
    return GoogleControlAccount(
        ref=_ref(data["ref"], "ref"),
        label=_text(data["label"], "label"),
        enabled=_bool(data["enabled"], "enabled"),
        subject_bound=_bool(data["subject_bound"], "subject_bound"),
        oauth_state=_code(data["oauth_state"], "oauth_state"),
        inventory_generation=_non_negative_int(
            data["inventory_generation"], "inventory_generation"
        ),
        quota_state=_code(data["quota_state"], "quota_state"),
        project_count=_non_negative_int(data["project_count"], "project_count"),
        billing_count=_non_negative_int(data["billing_count"], "billing_count"),
        reload_state=_code(data["reload_state"], "reload_state"),
    )


def _parse_openai_account(payload: object) -> OpenAIControlAccount:
    data = _mapping(
        payload,
        {
            "ref",
            "label",
            "enabled",
            "auth_state",
            "access_expires_at",
            "credential_generation",
            "vault_projection_state",
            "usage_state",
        },
    )
    return OpenAIControlAccount(
        ref=_ref(data["ref"], "ref"),
        label=_text(data["label"], "label"),
        enabled=_bool(data["enabled"], "enabled"),
        auth_state=_code(data["auth_state"], "auth_state"),
        access_expires_at=_optional_timestamp(data["access_expires_at"], "access_expires_at"),
        credential_generation=_non_negative_int(
            data["credential_generation"], "credential_generation"
        ),
        vault_projection_state=_code(data["vault_projection_state"], "vault_projection_state"),
        usage_state=_code(data["usage_state"], "usage_state"),
    )


def _document(payload: object, fields: set[str]) -> dict[str, object]:
    data = _mapping(payload, fields)
    schema_version = data["schema_version"]
    if type(schema_version) is not int:
        _invalid("schema_version")
    if schema_version != _SCHEMA_VERSION:
        raise ControlContractError("control.schema_unsupported")
    return data


def _mapping(payload: object, fields: set[str]) -> dict[str, object]:
    if type(payload) is not dict or set(payload) != fields:
        _invalid("fields")
    return payload


def _list(value: object, field: str, maximum: int) -> list[object]:
    if type(value) is not list or len(value) > maximum:
        _invalid(field)
    return value


def _unique_refs(
    items: tuple[OpenAIControlAccount, ...] | tuple[GoogleControlAccount, ...],
) -> None:
    if len({item.ref for item in items}) != len(items):
        _invalid("ref")


def _ref(value: object, field: str) -> str:
    if type(value) is not str or not _REF_RE.fullmatch(value):
        _invalid(field)
    return value


def _optional_ref(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _ref(value, field)


def _code(value: object, field: str) -> str:
    if type(value) is not str or not _CODE_RE.fullmatch(value):
        _invalid(field)
    return value


def _visible_name(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) > _MAX_TEXT_CHARS
        or not _VISIBLE_NAME_RE.fullmatch(value)
    ):
        _invalid(field)
    return value


def _text(value: object, field: str, maximum: int = _MAX_TEXT_CHARS) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        _invalid(field)
    return value


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        _invalid(field)
    return value


def _non_negative_int(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_GENERATION:
        _invalid(field)
    return value


def _optional_non_negative_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value, field)


def _plan_digest(value: object) -> str:
    if type(value) is not str or not _PLAN_DIGEST_RE.fullmatch(value):
        _invalid("plan_digest")
    return value


def _reason_codes(value: object) -> tuple[str, ...]:
    codes = _list(value, "reason_codes", _MAX_REASON_CODES)
    result = tuple(_code(code, "reason_codes") for code in codes)
    if len(set(result)) != len(result):
        _invalid("reason_codes")
    return result


def _optional_timestamp(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    return _timestamp(value, field)


def _timestamp(value: object, field: str) -> datetime:
    if type(value) is not str or not _TIMESTAMP_RE.fullmatch(value):
        _invalid(field)
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ControlContractError("control.response_invalid") from exc


def _invalid(_field: str) -> None:
    raise ControlContractError("control.response_invalid")
