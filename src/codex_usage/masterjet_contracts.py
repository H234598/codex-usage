from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_SCHEMA_VERSION = 1
_MAX_ACCOUNTS = 256
_MAX_PROJECTS = 256
_MAX_REASON_CODES = 64
_MAX_LABEL_BYTES = 256
_MAX_PURPOSE_BYTES = 256
_MAX_NAME_BYTES = 256
_MAX_TITLE_BYTES = 256
_MAX_DETAIL_BYTES = 2_048
_MAX_EFFECT_BYTES = 512
_MAX_ACTION_BYTES = 512
_MAX_GENERATION = 2**31 - 1
_MAX_OPERATION_COUNT = 10_000
_MAX_RETRY_SECONDS = 86_400
_MIN_TIMESTAMP_YEAR = 2000
_MAX_TIMESTAMP_YEAR = 2100
_MAX_OPERATION_LIFETIME = timedelta(days=1)
_REF_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63}){0,7}$")
_VISIBLE_NAME_RE = re.compile(r"^[A-Za-z]+(?:[ -][A-Za-z]+)*$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PLAN_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_PRIVATE_TEXT_RE = re.compile(
    r"(?:\b(?:bearer|access[_ -]?token|refresh[_ -]?token|id[_ -]?token|"
    r"api[_ -]?key|client[_ -]?(?:secret|id)|authorization|cookie|password|secret|"
    r"token|credential|prompt|"
    r"raw[ _-]?output|provider[ _-]?(?:response|output)|response[ _-]?body)\b|"
    r"\b(?:AIza[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,})\b|"
    r"file:|(?:^|[\s\"'(])/(?:[^\s]*)|\\\\|[A-Za-z]:[\\/]|```|^\s*[\[{])",
    re.IGNORECASE,
)
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
    local_profile_ref: str
    source_host_ref: str
    auth_state: str
    access_expires_at: datetime | None
    credential_generation: int
    vault_projection_state: str
    usage_state: str

    def __post_init__(self) -> None:
        _ref(self.ref, "ref")
        _text(self.label, "label", _MAX_LABEL_BYTES)
        _bool(self.enabled, "enabled")
        _ref(self.local_profile_ref, "local_profile_ref")
        _ref(self.source_host_ref, "source_host_ref")
        _code(self.auth_state, "auth_state")
        _optional_timestamp_value(self.access_expires_at, "access_expires_at")
        _generation(self.credential_generation, "credential_generation")
        _code(self.vault_projection_state, "vault_projection_state")
        _code(self.usage_state, "usage_state")


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

    def __post_init__(self) -> None:
        _ref(self.ref, "ref")
        _text(self.label, "label", _MAX_LABEL_BYTES)
        _bool(self.enabled, "enabled")
        _bool(self.subject_bound, "subject_bound")
        _code(self.oauth_state, "oauth_state")
        _generation(self.inventory_generation, "inventory_generation")
        _code(self.quota_state, "quota_state")
        _count(self.project_count, "project_count", _MAX_PROJECTS)
        _count(self.billing_count, "billing_count", _MAX_PROJECTS)
        _code(self.reload_state, "reload_state")


@dataclass(frozen=True, slots=True)
class GoogleControlProject:
    ref: str
    project_name: str
    purpose: str
    key_name: str
    billing_ref: str | None
    status: str
    probe_state: str
    quota_state: str

    def __post_init__(self) -> None:
        _ref(self.ref, "ref")
        _visible_name(self.project_name, "project_name")
        _text(self.purpose, "purpose", _MAX_PURPOSE_BYTES)
        _visible_name(self.key_name, "key_name")
        _optional_ref(self.billing_ref, "billing_ref")
        _code(self.status, "status")
        _code(self.probe_state, "probe_state")
        _code(self.quota_state, "quota_state")


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

    def __post_init__(self) -> None:
        _ref(self.id, "id")
        _code(self.kind, "kind")
        if _code(self.state, "state") not in _OPERATION_STATES:
            _invalid("state")
        _generation(self.expected_generation, "expected_generation")
        _optional_generation(self.resulting_generation, "resulting_generation")
        _plan_digest(self.plan_digest)
        _timestamp_value(self.created_at, "created_at")
        _timestamp_value(self.expires_at, "expires_at")
        _count(self.completed_count, "completed_count", _MAX_OPERATION_COUNT)
        _count(self.failed_count, "failed_count", _MAX_OPERATION_COUNT)
        _count(self.not_attempted_count, "not_attempted_count", _MAX_OPERATION_COUNT)
        _reason_codes(self.reason_codes)
        if (
            self.expires_at <= self.created_at
            or self.expires_at - self.created_at > _MAX_OPERATION_LIFETIME
        ):
            _invalid("expires_at")


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

    def __post_init__(self) -> None:
        _code(self.code, "code")
        if _code(self.severity, "severity") not in _PROBLEM_SEVERITIES:
            _invalid("severity")
        _text(self.title, "title", _MAX_TITLE_BYTES)
        _text(self.detail, "detail", _MAX_DETAIL_BYTES)
        _text(self.effect, "effect", _MAX_EFFECT_BYTES)
        _text(self.action, "action", _MAX_ACTION_BYTES)
        _bool(self.retryable, "retryable")
        _optional_retry_seconds(self.retry_after_seconds, "retry_after_seconds")
        _ref(self.correlation_id, "correlation_id")
        _timestamp_value(self.occurred_at, "occurred_at")


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
            "purpose",
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
        purpose=_text(data["purpose"], "purpose", _MAX_PURPOSE_BYTES),
        key_name=_visible_name(data["key_name"], "key_name"),
        billing_ref=_optional_ref(data["billing_ref"], "billing_ref"),
        status=_code(data["status"], "status"),
        probe_state=_code(data["probe_state"], "probe_state"),
        quota_state=_code(data["quota_state"], "quota_state"),
    )


def parse_google_projects(payload: object) -> tuple[GoogleControlProject, ...]:
    data = _document(
        payload,
        {"schema_version", "account_ref", "inventory_generation", "projects"},
    )
    _ref(data["account_ref"], "account_ref")
    _generation(data["inventory_generation"], "inventory_generation")
    projects = _list(data["projects"], "projects", _MAX_PROJECTS)
    result = tuple(parse_google_project(project) for project in projects)
    _unique_refs(result)
    return result


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
        expected_generation=_generation(data["expected_generation"], "expected_generation"),
        resulting_generation=_optional_generation(
            data["resulting_generation"], "resulting_generation"
        ),
        plan_digest=_plan_digest(data["plan_digest"]),
        created_at=_timestamp(data["created_at"], "created_at"),
        expires_at=_timestamp(data["expires_at"], "expires_at"),
        completed_count=_count(data["completed_count"], "completed_count", _MAX_OPERATION_COUNT),
        failed_count=_count(data["failed_count"], "failed_count", _MAX_OPERATION_COUNT),
        not_attempted_count=_count(
            data["not_attempted_count"], "not_attempted_count", _MAX_OPERATION_COUNT
        ),
        reason_codes=_parse_reason_codes(data["reason_codes"]),
    )
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
        title=_text(data["title"], "title", _MAX_TITLE_BYTES),
        detail=_text(data["detail"], "detail", _MAX_DETAIL_BYTES),
        effect=_text(data["effect"], "effect", _MAX_EFFECT_BYTES),
        action=_text(data["action"], "action", _MAX_ACTION_BYTES),
        retryable=_bool(data["retryable"], "retryable"),
        retry_after_seconds=_optional_retry_seconds(
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
        label=_text(data["label"], "label", _MAX_LABEL_BYTES),
        enabled=_bool(data["enabled"], "enabled"),
        subject_bound=_bool(data["subject_bound"], "subject_bound"),
        oauth_state=_code(data["oauth_state"], "oauth_state"),
        inventory_generation=_generation(
            data["inventory_generation"], "inventory_generation"
        ),
        quota_state=_code(data["quota_state"], "quota_state"),
        project_count=_count(data["project_count"], "project_count", _MAX_PROJECTS),
        billing_count=_count(data["billing_count"], "billing_count", _MAX_PROJECTS),
        reload_state=_code(data["reload_state"], "reload_state"),
    )


def _parse_openai_account(payload: object) -> OpenAIControlAccount:
    data = _mapping(
        payload,
        {
            "ref",
            "label",
            "enabled",
            "local_profile_ref",
            "source_host_ref",
            "auth_state",
            "access_expires_at",
            "credential_generation",
            "vault_projection_state",
            "usage_state",
        },
    )
    return OpenAIControlAccount(
        ref=_ref(data["ref"], "ref"),
        label=_text(data["label"], "label", _MAX_LABEL_BYTES),
        enabled=_bool(data["enabled"], "enabled"),
        local_profile_ref=_ref(data["local_profile_ref"], "local_profile_ref"),
        source_host_ref=_ref(data["source_host_ref"], "source_host_ref"),
        auth_state=_code(data["auth_state"], "auth_state"),
        access_expires_at=_optional_timestamp(data["access_expires_at"], "access_expires_at"),
        credential_generation=_generation(
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
    items: tuple[OpenAIControlAccount, ...]
    | tuple[GoogleControlAccount, ...]
    | tuple[GoogleControlProject, ...],
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
    if type(value) is not str:
        _invalid(field)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ControlContractError("control.response_invalid") from exc
    if (
        len(encoded) > _MAX_NAME_BYTES
        or _unsafe_text(value)
        or not _VISIBLE_NAME_RE.fullmatch(value)
    ):
        _invalid(field)
    return value


def _text(value: object, field: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or _unsafe_text(value)
    ):
        _invalid(field)
    try:
        if len(value.encode("utf-8")) > maximum:
            _invalid(field)
    except UnicodeEncodeError as exc:
        raise ControlContractError("control.response_invalid") from exc
    return value


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        _invalid(field)
    return value


def _generation(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_GENERATION:
        _invalid(field)
    return value


def _optional_generation(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _generation(value, field)


def _count(value: object, field: str, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        _invalid(field)
    return value


def _optional_retry_seconds(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _count(value, field, _MAX_RETRY_SECONDS)


def _plan_digest(value: object) -> str:
    if type(value) is not str or not _PLAN_DIGEST_RE.fullmatch(value):
        _invalid("plan_digest")
    return value


def _parse_reason_codes(value: object) -> tuple[str, ...]:
    codes = _list(value, "reason_codes", _MAX_REASON_CODES)
    return _reason_codes(tuple(codes))


def _reason_codes(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > _MAX_REASON_CODES:
        _invalid("reason_codes")
    result = tuple(_code(code, "reason_codes") for code in value)
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
        timestamp = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ControlContractError("control.response_invalid") from exc
    _timestamp_value(timestamp, field)
    return timestamp


def _optional_timestamp_value(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    return _timestamp_value(value, field)


def _timestamp_value(value: object, field: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is not UTC
        or not _MIN_TIMESTAMP_YEAR <= value.year <= _MAX_TIMESTAMP_YEAR
    ):
        _invalid(field)
    return value


def _unsafe_text(value: str) -> bool:
    if _PRIVATE_TEXT_RE.search(value):
        return True
    return any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value)


def _invalid(_field: str) -> None:
    raise ControlContractError("control.response_invalid")
