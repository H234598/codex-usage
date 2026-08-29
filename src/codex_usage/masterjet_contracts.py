from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn
from urllib.parse import parse_qsl, urlsplit

_SCHEMA_VERSION = 1
_MAX_ACCOUNTS = 256
_MAX_PROJECTS = 256
_MAX_REASON_CODES = 64
_MAX_NAME_BYTES = 256
_MAX_LABEL_BYTES = 256
_MAX_PURPOSE_BYTES = 128
_MAX_GENERATION = 2**63 - 1
_MAX_COUNT = 100_000
_MAX_RETRY_SECONDS = 86_400
_MAX_URL_BYTES = 2_048
_REF_RE = re.compile(r"^[a-z][a-z0-9-]{0,127}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PURPOSE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_LABEL_RE = re.compile(r'^[^{}\[\]"`:\\]+$')
_VISIBLE_NAME_RE = re.compile(r"^[A-Za-z]+(?:[ -][A-Za-z]+)*$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_PLAN_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_SECRET_VALUE_RE = re.compile(
    r"(?:\bAIza[A-Za-z0-9_-]*|\bya29(?:\.[A-Za-z0-9._-]*)?\b|"
    r"\b1//|\bGOCSPX-|\beyJ[A-Za-z0-9_-]{20,}|\bsk-|"
    r"(?:^|\s)Bearer\s+[A-Za-z0-9._-]{8,}|"
    r"\b(?:access_token|refresh_token|client_secret)\s*[=:]\s*\S+)",
    re.IGNORECASE,
)
_OAUTH_QUERY_NAMES = frozenset(
    {
        "access_type",
        "client_id",
        "code_challenge",
        "code_challenge_method",
        "hd",
        "include_granted_scopes",
        "prompt",
        "redirect_uri",
        "response_type",
        "scope",
        "state",
    }
)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_])(?:file://|/(?:[^\s]*)|\\\\[^\s]+|[A-Za-z]:[\\/])"
)
_OPERATION_STATES = frozenset(
    {"planned", "queued", "running", "partial", "succeeded", "failed", "blocked"}
)
_PROBLEM_TEMPLATES = {
    "control.step_up_required": (
        "warning",
        "Step-up required",
        "Additional authentication is required.",
        "Operation is paused.",
        "Complete step-up authentication.",
    ),
    "credential.source_unavailable": (
        "error",
        "Credential source unavailable",
        "Token refresh required.",
        "Authentication is unavailable.",
        "Restore the credential source.",
    ),
    "credential.upload_expired": (
        "error",
        "Credential upload expired",
        "Credential upload has expired.",
        "Authentication is unavailable.",
        "Start a new credential upload.",
    ),
    "credential.generation_conflict": (
        "warning",
        "Credential generation conflict",
        "Credential generation has changed.",
        "Synchronization stopped.",
        "Refresh credential status.",
    ),
    "oauth.transaction_expired": (
        "warning",
        "OAuth transaction expired",
        "OAuth transaction has expired.",
        "Authorization is unavailable.",
        "Start OAuth again.",
    ),
    "oauth.identity_mismatch": (
        "error",
        "OAuth identity mismatch",
        "OAuth identity does not match the selected account.",
        "Authorization is blocked.",
        "Use the selected account.",
    ),
    "quota.evidence_stale": (
        "warning",
        "Quota evidence stale",
        "Quota evidence is stale.",
        "Provisioning is blocked.",
        "Refresh quota evidence.",
    ),
    "quota.provider_exhausted": (
        "warning",
        "Provider quota exhausted",
        "Provider quota is exhausted.",
        "Provisioning stopped.",
        "Refresh quota evidence.",
    ),
    "control.plan_stale": (
        "warning",
        "Control plan stale",
        "Control plan is stale.",
        "Mutation is blocked.",
        "Create a new plan.",
    ),
    "control.operation_partial": (
        "warning",
        "Control operation partial",
        "Control operation completed partially.",
        "Some changes were not applied.",
        "Review operation status.",
    ),
    "resource.host_unreachable": (
        "error",
        "Resource host unreachable",
        "Resource host is unreachable.",
        "Operation is unavailable.",
        "Check host availability.",
    ),
    "resource.target_path_invalid": (
        "error",
        "Resource target path invalid",
        "Resource target path is invalid.",
        "Operation is blocked.",
        "Correct the target path.",
    ),
    "resource.cgroup_profile_invalid": (
        "error",
        "Resource cgroup profile invalid",
        "Resource cgroup profile is invalid.",
        "Operation is blocked.",
        "Correct the cgroup profile.",
    ),
    "provider.model_unavailable": (
        "warning",
        "Provider model unavailable",
        "Provider model is unavailable.",
        "Operation is blocked.",
        "Select an available model.",
    ),
    "authority.scope_denied": (
        "error",
        "Authority scope denied",
        "Authority scope is denied.",
        "Operation is denied.",
        "Request required scope.",
    ),
}


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
        _label(self.label, "label")
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
    subject_bound: bool
    inventory_generation: int
    project_count: int
    billing_count: int
    default_oauth_client_ref: str | None
    oauth_client_availability: str

    def __post_init__(self) -> None:
        _ref(self.ref, "ref")
        _label(self.label, "label")
        _bool(self.subject_bound, "subject_bound")
        _generation(self.inventory_generation, "inventory_generation")
        _count(self.project_count, "project_count", _MAX_COUNT)
        _count(self.billing_count, "billing_count", _MAX_COUNT)
        _optional_ref(self.default_oauth_client_ref, "default_oauth_client_ref")
        availability = _code(self.oauth_client_availability, "oauth_client_availability")
        if availability not in {
            "available",
            "missing",
            "ambiguous",
            "revoked",
            "stale",
            "unavailable",
        }:
            _invalid("oauth_client_availability")
        if (availability == "available") != (self.default_oauth_client_ref is not None):
            _invalid("default_oauth_client_ref")


@dataclass(frozen=True, slots=True)
class GoogleOAuthTransactionV1:
    id: str
    account_ref: str
    authorization_url: str
    expires_at: float
    inventory_generation: int

    def __post_init__(self) -> None:
        _token(self.id, "id")
        _ref(self.account_ref, "account_ref")
        _authorization_url(self.authorization_url)
        _epoch_seconds(self.expires_at, "expires_at")
        _generation(self.inventory_generation, "inventory_generation")


@dataclass(frozen=True, slots=True)
class GoogleOAuthReceipt:
    account_ref: str
    subject_bound: bool
    refresh_token_stored: bool

    def __post_init__(self) -> None:
        _ref(self.account_ref, "account_ref")
        _bool(self.subject_bound, "subject_bound")
        _bool(self.refresh_token_stored, "refresh_token_stored")


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
        _purpose(self.purpose, "purpose")
        _visible_name(self.key_name, "key_name")
        _optional_ref(self.billing_ref, "billing_ref")
        _code(self.status, "status")
        _code(self.probe_state, "probe_state")
        _code(self.quota_state, "quota_state")


@dataclass(frozen=True, slots=True)
class GoogleControlProjectList:
    schema_version: int
    account_ref: str
    inventory_generation: int
    projects: tuple[GoogleControlProject, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            _invalid("schema_version")
        _ref(self.account_ref, "account_ref")
        _generation(self.inventory_generation, "inventory_generation")
        if type(self.projects) is not tuple or len(self.projects) > _MAX_PROJECTS:
            _invalid("projects")
        if not all(type(project) is GoogleControlProject for project in self.projects):
            _invalid("projects")
        _unique_refs(self.projects)


@dataclass(frozen=True, slots=True)
class GoogleProvisionProjectV1:
    project_name: str
    key_name: str

    def __post_init__(self) -> None:
        _visible_name(self.project_name, "project_name")
        _visible_name(self.key_name, "key_name")


@dataclass(frozen=True, slots=True)
class GoogleProvisionPlanV1:
    id: str
    kind: str
    state: str
    account_ref: str
    expected_generation: int
    resulting_generation: int | None
    plan_digest: str
    created_at: datetime
    expires_at: datetime
    completed_count: int
    failed_count: int
    not_attempted_count: int
    reason_codes: tuple[str, ...]
    step_count: int
    projects: tuple[GoogleProvisionProjectV1, ...]

    def __post_init__(self) -> None:
        _token(self.id, "id")
        if _token(self.kind, "kind") != "google.provision.plan":
            _invalid("kind")
        if _code(self.state, "state") != "planned":
            _invalid("state")
        _ref(self.account_ref, "account_ref")
        _generation(self.expected_generation, "expected_generation")
        _optional_generation(self.resulting_generation, "resulting_generation")
        _plan_digest(self.plan_digest)
        _timestamp_value(self.created_at, "created_at")
        _timestamp_value(self.expires_at, "expires_at")
        _count(self.completed_count, "completed_count", _MAX_COUNT)
        _count(self.failed_count, "failed_count", _MAX_COUNT)
        _count(self.not_attempted_count, "not_attempted_count", _MAX_COUNT)
        _reason_codes(self.reason_codes)
        _count(self.step_count, "step_count", _MAX_COUNT)
        if (
            self.resulting_generation is not None
            or self.expires_at <= self.created_at
            or type(self.projects) is not tuple
            or len(self.projects) > _MAX_PROJECTS
            or not all(type(item) is GoogleProvisionProjectV1 for item in self.projects)
            or self.step_count < len(self.projects)
        ):
            _invalid("plan")
        pairs = tuple((item.project_name, item.key_name) for item in self.projects)
        if len(pairs) != len(set(pairs)):
            _invalid("projects")


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
        _token(self.id, "id")
        _token(self.kind, "kind")
        if _code(self.state, "state") not in _OPERATION_STATES:
            _invalid("state")
        _generation(self.expected_generation, "expected_generation")
        _optional_generation(self.resulting_generation, "resulting_generation")
        _plan_digest(self.plan_digest)
        _timestamp_value(self.created_at, "created_at")
        _timestamp_value(self.expires_at, "expires_at")
        _count(self.completed_count, "completed_count", _MAX_COUNT)
        _count(self.failed_count, "failed_count", _MAX_COUNT)
        _count(self.not_attempted_count, "not_attempted_count", _MAX_COUNT)
        _reason_codes(self.reason_codes)
        if self.expires_at <= self.created_at:
            _invalid("expires_at")


@dataclass(frozen=True, slots=True)
class SecretIngressSession:
    id: str
    account_ref: str
    state: str
    plan_digest: str
    expected_generation: int
    expires_at: float
    session_generation: int

    def __post_init__(self) -> None:
        _token(self.id, "id")
        _ref(self.account_ref, "account_ref")
        _code(self.state, "state")
        _plan_digest(self.plan_digest)
        _generation(self.expected_generation, "expected_generation")
        _epoch_seconds(self.expires_at, "expires_at")
        _generation(self.session_generation, "session_generation")


@dataclass(frozen=True, slots=True)
class SecretIngressReceipt:
    session_id: str
    account_ref: str
    state: str
    generation: int

    def __post_init__(self) -> None:
        _token(self.session_id, "session_id")
        _ref(self.account_ref, "account_ref")
        _code(self.state, "state")
        _generation(self.generation, "generation")


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
        template = _problem_template(self.code)
        if (
            self.severity,
            self.title,
            self.detail,
            self.effect,
            self.action,
        ) != template:
            _invalid("problem")
        _bool(self.retryable, "retryable")
        _optional_retry_seconds(self.retry_after_seconds, "retry_after_seconds")
        _token(self.correlation_id, "correlation_id")
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


def parse_google_oauth_transaction(payload: object) -> GoogleOAuthTransactionV1:
    data = _document(
        payload,
        {
            "schema_version",
            "id",
            "account_ref",
            "authorization_url",
            "expires_at",
            "inventory_generation",
        },
    )
    return GoogleOAuthTransactionV1(
        id=_token(data["id"], "id"),
        account_ref=_ref(data["account_ref"], "account_ref"),
        authorization_url=_authorization_url(data["authorization_url"]),
        expires_at=_epoch_seconds(data["expires_at"], "expires_at"),
        inventory_generation=_generation(
            data["inventory_generation"], "inventory_generation"
        ),
    )


def parse_google_oauth_receipt(payload: object) -> GoogleOAuthReceipt:
    data = _document(
        payload,
        {"schema_version", "account_ref", "subject_bound", "refresh_token_stored"},
    )
    return GoogleOAuthReceipt(
        account_ref=_ref(data["account_ref"], "account_ref"),
        subject_bound=_bool(data["subject_bound"], "subject_bound"),
        refresh_token_stored=_bool(
            data["refresh_token_stored"], "refresh_token_stored"
        ),
    )


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
        purpose=_purpose(data["purpose"], "purpose"),
        key_name=_visible_name(data["key_name"], "key_name"),
        billing_ref=_optional_ref(data["billing_ref"], "billing_ref"),
        status=_code(data["status"], "status"),
        probe_state=_code(data["probe_state"], "probe_state"),
        quota_state=_code(data["quota_state"], "quota_state"),
    )


def parse_google_projects(
    payload: object,
    *,
    expected_account_ref: str | None = None,
) -> GoogleControlProjectList:
    data = _document(
        payload,
        {"schema_version", "account_ref", "inventory_generation", "projects"},
    )
    account_ref = _ref(data["account_ref"], "account_ref")
    if expected_account_ref is not None and account_ref != _ref(
        expected_account_ref, "expected_account_ref"
    ):
        _invalid("account_ref")
    inventory_generation = _generation(data["inventory_generation"], "inventory_generation")
    projects = _list(data["projects"], "projects", _MAX_PROJECTS)
    return GoogleControlProjectList(
        schema_version=_SCHEMA_VERSION,
        account_ref=account_ref,
        inventory_generation=inventory_generation,
        projects=tuple(parse_google_project(project) for project in projects),
    )


def parse_google_provision_plan(payload: object) -> GoogleProvisionPlanV1:
    data = _document(
        payload,
        {
            "schema_version",
            "id",
            "kind",
            "state",
            "account_ref",
            "expected_generation",
            "resulting_generation",
            "plan_digest",
            "created_at",
            "expires_at",
            "completed_count",
            "failed_count",
            "not_attempted_count",
            "reason_codes",
            "step_count",
            "projects",
        },
    )
    raw_projects = _list(data["projects"], "projects", _MAX_PROJECTS)
    projects = tuple(_parse_google_provision_project(item) for item in raw_projects)
    return GoogleProvisionPlanV1(
        id=_token(data["id"], "id"),
        kind=_token(data["kind"], "kind"),
        state=_code(data["state"], "state"),
        account_ref=_ref(data["account_ref"], "account_ref"),
        expected_generation=_generation(data["expected_generation"], "expected_generation"),
        resulting_generation=_optional_generation(
            data["resulting_generation"], "resulting_generation"
        ),
        plan_digest=_plan_digest(data["plan_digest"]),
        created_at=_timestamp(data["created_at"], "created_at"),
        expires_at=_timestamp(data["expires_at"], "expires_at"),
        completed_count=_count(data["completed_count"], "completed_count", _MAX_COUNT),
        failed_count=_count(data["failed_count"], "failed_count", _MAX_COUNT),
        not_attempted_count=_count(data["not_attempted_count"], "not_attempted_count", _MAX_COUNT),
        reason_codes=_parse_reason_codes(data["reason_codes"]),
        step_count=_count(data["step_count"], "step_count", _MAX_COUNT),
        projects=projects,
    )


def _parse_google_provision_project(payload: object) -> GoogleProvisionProjectV1:
    data = _mapping(payload, {"project_name", "key_name"})
    return GoogleProvisionProjectV1(
        project_name=_visible_name(data["project_name"], "project_name"),
        key_name=_visible_name(data["key_name"], "key_name"),
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
        id=_token(data["id"], "id"),
        kind=_token(data["kind"], "kind"),
        state=state,
        expected_generation=_generation(data["expected_generation"], "expected_generation"),
        resulting_generation=_optional_generation(
            data["resulting_generation"], "resulting_generation"
        ),
        plan_digest=_plan_digest(data["plan_digest"]),
        created_at=_timestamp(data["created_at"], "created_at"),
        expires_at=_timestamp(data["expires_at"], "expires_at"),
        completed_count=_count(data["completed_count"], "completed_count", _MAX_COUNT),
        failed_count=_count(data["failed_count"], "failed_count", _MAX_COUNT),
        not_attempted_count=_count(data["not_attempted_count"], "not_attempted_count", _MAX_COUNT),
        reason_codes=_parse_reason_codes(data["reason_codes"]),
    )
    return operation


def parse_secret_ingress_session(payload: object) -> SecretIngressSession:
    data = _document(
        payload,
        {
            "schema_version",
            "id",
            "account_ref",
            "state",
            "plan_digest",
            "expected_generation",
            "expires_at",
            "session_generation",
        },
    )
    return SecretIngressSession(
        id=_token(data["id"], "id"),
        account_ref=_ref(data["account_ref"], "account_ref"),
        state=_code(data["state"], "state"),
        plan_digest=_plan_digest(data["plan_digest"]),
        expected_generation=_generation(data["expected_generation"], "expected_generation"),
        expires_at=_epoch_seconds(data["expires_at"], "expires_at"),
        session_generation=_generation(data["session_generation"], "session_generation"),
    )


def parse_secret_ingress_receipt(payload: object) -> SecretIngressReceipt:
    data = _document(
        payload,
        {"schema_version", "session_id", "account_ref", "state", "generation"},
    )
    return SecretIngressReceipt(
        session_id=_token(data["session_id"], "session_id"),
        account_ref=_ref(data["account_ref"], "account_ref"),
        state=_code(data["state"], "state"),
        generation=_generation(data["generation"], "generation"),
    )


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
    code = _code(data["code"], "code")
    template = _problem_template(code)
    remote_text = (
        data["severity"],
        data["title"],
        data["detail"],
        data["effect"],
        data["action"],
    )
    if any(type(value) is not str for value in remote_text) or remote_text != template:
        _invalid("problem")
    return ControlProblem(
        code=code,
        severity=template[0],
        title=template[1],
        detail=template[2],
        effect=template[3],
        action=template[4],
        retryable=_bool(data["retryable"], "retryable"),
        retry_after_seconds=_optional_retry_seconds(
            data["retry_after_seconds"], "retry_after_seconds"
        ),
        correlation_id=_token(data["correlation_id"], "correlation_id"),
        occurred_at=_timestamp(data["occurred_at"], "occurred_at"),
    )


def _parse_google_account(payload: object) -> GoogleControlAccount:
    data = _mapping(
        payload,
        {
            "ref",
            "label",
            "subject_bound",
            "inventory_generation",
            "project_count",
            "billing_count",
            "default_oauth_client_ref",
            "oauth_client_availability",
        },
    )
    return GoogleControlAccount(
        ref=_ref(data["ref"], "ref"),
        label=_label(data["label"], "label"),
        subject_bound=_bool(data["subject_bound"], "subject_bound"),
        inventory_generation=_generation(data["inventory_generation"], "inventory_generation"),
        project_count=_count(data["project_count"], "project_count", _MAX_COUNT),
        billing_count=_count(data["billing_count"], "billing_count", _MAX_COUNT),
        default_oauth_client_ref=_optional_ref(
            data["default_oauth_client_ref"], "default_oauth_client_ref"
        ),
        oauth_client_availability=_code(
            data["oauth_client_availability"], "oauth_client_availability"
        ),
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
        label=_label(data["label"], "label"),
        enabled=_bool(data["enabled"], "enabled"),
        local_profile_ref=_ref(data["local_profile_ref"], "local_profile_ref"),
        source_host_ref=_ref(data["source_host_ref"], "source_host_ref"),
        auth_state=_code(data["auth_state"], "auth_state"),
        access_expires_at=_optional_timestamp(data["access_expires_at"], "access_expires_at"),
        credential_generation=_generation(data["credential_generation"], "credential_generation"),
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
    if type(value) is not str or _private_value(value) or not _REF_RE.fullmatch(value):
        _invalid(field)
    return value


def _optional_ref(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _ref(value, field)


def _code(value: object, field: str) -> str:
    return _token(value, field)


def _token(value: object, field: str) -> str:
    if type(value) is not str or _private_value(value) or not _TOKEN_RE.fullmatch(value):
        _invalid(field)
    return value


def _visible_name(value: object, field: str) -> str:
    _safe_text(value, field, _MAX_NAME_BYTES)
    assert isinstance(value, str)
    if not _VISIBLE_NAME_RE.fullmatch(value):
        _invalid(field)
    return value


def _label(value: object, field: str) -> str:
    _safe_text(value, field, _MAX_LABEL_BYTES)
    assert isinstance(value, str)
    if not _LABEL_RE.fullmatch(value):
        _invalid(field)
    return value


def _purpose(value: object, field: str) -> str:
    _safe_text(value, field, _MAX_PURPOSE_BYTES)
    assert isinstance(value, str)
    if not _PURPOSE_RE.fullmatch(value):
        _invalid(field)
    return value


def _safe_text(value: object, field: str, maximum: int) -> str:
    if type(value) is not str or not value or _private_value(value):
        _invalid(field)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ControlContractError("control.response_invalid") from exc
    if len(encoded) > maximum or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
    ):
        _invalid(field)
    return value


def _authorization_url(value: object) -> str:
    if type(value) is not str or not value:
        _invalid("authorization_url")
    try:
        encoded = value.encode("ascii")
        parsed = urlsplit(value)
        port = parsed.port
    except (UnicodeError, ValueError):
        _invalid("authorization_url")
    if (
        len(encoded) > _MAX_URL_BYTES
        or any(not 0x21 <= byte <= 0x7E for byte in encoded)
        or parsed.scheme != "https"
        or parsed.hostname != "accounts.google.com"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/o/oauth2/v2/auth"
        or parsed.fragment
    ):
        _invalid("authorization_url")
    try:
        query = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=32,
        )
    except (UnicodeError, ValueError):
        _invalid("authorization_url")
    seen: set[str] = set()
    redirect_uri: str | None = None
    for name, query_value in query:
        normalized_name = name.casefold()
        if normalized_name not in _OAUTH_QUERY_NAMES or normalized_name in seen:
            _invalid("authorization_url")
        seen.add(normalized_name)
        try:
            query_bytes = query_value.encode("ascii")
        except UnicodeError:
            _invalid("authorization_url")
        if (
            len(query_bytes) > 1_024
            or any(not 0x20 <= byte <= 0x7E for byte in query_bytes)
            or _SECRET_VALUE_RE.search(query_value)
        ):
            _invalid("authorization_url")
        if normalized_name == "redirect_uri":
            redirect_uri = validate_google_oauth_redirect_uri(query_value)
    if redirect_uri is None:
        _invalid("authorization_url")
    return value


def validate_google_oauth_redirect_uri(value: object) -> str:
    if type(value) is not str or not value:
        _invalid("redirect_uri")
    try:
        encoded = value.encode("ascii")
        parsed = urlsplit(value)
        port = parsed.port
    except (UnicodeError, ValueError):
        _invalid("redirect_uri")
    host = parsed.hostname
    canonical_host = f"[{host}]" if host == "::1" else host
    canonical = (
        f"http://{canonical_host}:{port}{parsed.path}"
        if canonical_host is not None and port is not None
        else ""
    )
    if (
        len(encoded) > _MAX_URL_BYTES
        or any(not 0x21 <= byte <= 0x7E for byte in encoded)
        or parsed.scheme != "http"
        or host not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65_535
        or not parsed.path.startswith("/")
        or parsed.path == "/"
        or "//" in parsed.path
        or "%" in parsed.path
        or any(part in {".", ".."} for part in parsed.path.split("/"))
        or parsed.query
        or parsed.fragment
        or value != canonical
    ):
        _invalid("redirect_uri")
    return value


def google_oauth_redirect_uri(authorization_url: object) -> str:
    value = _authorization_url(authorization_url)
    try:
        query = parse_qsl(
            urlsplit(value).query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=32,
        )
    except (UnicodeError, ValueError):
        _invalid("authorization_url")
    matches = [item for name, item in query if name.casefold() == "redirect_uri"]
    if len(matches) != 1:
        _invalid("authorization_url")
    return validate_google_oauth_redirect_uri(matches[0])


def google_oauth_state(authorization_url: object) -> str:
    value = _authorization_url(authorization_url)
    try:
        query = parse_qsl(
            urlsplit(value).query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=32,
        )
    except (UnicodeError, ValueError):
        _invalid("authorization_url")
    matches = [item for name, item in query if name.casefold() == "state"]
    if len(matches) != 1:
        _invalid("authorization_url")
    return _token(matches[0], "state")


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        _invalid(field)
    return value


def _generation(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_GENERATION:
        _invalid(field)
    return value


def _epoch_seconds(value: object, field: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
        _invalid(field)
    return float(value)


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
        timestamp = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise ControlContractError("control.response_invalid") from exc
    _timestamp_value(timestamp, field)
    return timestamp


def _optional_timestamp_value(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    return _timestamp_value(value, field)


def _timestamp_value(value: object, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is not UTC:
        _invalid(field)
    return value


def _problem_template(code: object) -> tuple[str, str, str, str, str]:
    if type(code) is not str or code not in _PROBLEM_TEMPLATES:
        _invalid("code")
    return _PROBLEM_TEMPLATES[code]


def _private_value(value: str) -> bool:
    return bool(_SECRET_VALUE_RE.search(value) or _ABSOLUTE_PATH_RE.search(value))


def _invalid(_field: str) -> NoReturn:
    raise ControlContractError("control.response_invalid")
