from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterator
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
_SNAPSHOT_FINGERPRINT_RE = re.compile(r"^[a-f0-9]{64}$")
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
_TERMINAL_OPERATION_STATES = frozenset({"partial", "succeeded", "failed", "blocked"})
_PUBLIC_AGENT_REASON_CODES = frozenset(
    {
        "host.unreachable",
        "host.identity_mismatch",
        "host.generation_stale",
        "host.lease_expired",
        "host.capability_mismatch",
        "host.probe_failed",
        "host.operation_unknown",
        "resource.host_response_invalid",
        "resource.host_unreachable",
        "control.plan_stale",
    }
)
_OPERATION_STATUS_FIELDS = {
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
    "result_kind",
    "result",
}
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
    enabled: bool
    subject_bound: bool
    oauth_state: str
    inventory_generation: int
    quota_state: str
    project_count: int
    billing_count: int
    billing_refs: tuple[str, ...]
    reload_state: str
    default_oauth_client_ref: str | None
    oauth_client_availability: str

    def __post_init__(self) -> None:
        _ref(self.ref, "ref")
        _label(self.label, "label")
        _bool(self.enabled, "enabled")
        _bool(self.subject_bound, "subject_bound")
        _code(self.oauth_state, "oauth_state")
        _generation(self.inventory_generation, "inventory_generation")
        _code(self.quota_state, "quota_state")
        _count(self.project_count, "project_count", _MAX_COUNT)
        _count(self.billing_count, "billing_count", _MAX_COUNT)
        if type(self.billing_refs) is not tuple or len(self.billing_refs) > _MAX_COUNT:
            _invalid("billing_refs")
        refs = tuple(_ref(item, "billing_refs") for item in self.billing_refs)
        if len(refs) != self.billing_count or len(refs) != len(set(refs)):
            _invalid("billing_refs")
        _code(self.reload_state, "reload_state")
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
class GoogleControlAccountList:
    accounts: tuple[GoogleControlAccount, ...]
    registry_generation: int

    def __post_init__(self) -> None:
        if type(self.accounts) is not tuple or not all(
            type(account) is GoogleControlAccount for account in self.accounts
        ):
            _invalid("accounts")
        _unique_refs(self.accounts)
        _generation(self.registry_generation, "registry_generation")

    def __iter__(self) -> Iterator[GoogleControlAccount]:
        return iter(self.accounts)

    def __len__(self) -> int:
        return len(self.accounts)

    def __getitem__(self, index: int) -> GoogleControlAccount:
        return self.accounts[index]


@dataclass(frozen=True, slots=True)
class GoogleAccountAddReceiptV1:
    account_ref: str
    resulting_generation: int

    def __post_init__(self) -> None:
        _ref(self.account_ref, "account_ref")
        _generation(self.resulting_generation, "resulting_generation")


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
class GoogleOAuthClientImportPlanV1:
    id: str
    account_ref: str
    expected_generation: int
    expires_at: float
    plan_digest: str

    def __post_init__(self) -> None:
        _token(self.id, "id")
        _ref(self.account_ref, "account_ref")
        _generation(self.expected_generation, "expected_generation")
        _epoch_seconds(self.expires_at, "expires_at")
        _plan_digest(self.plan_digest)


@dataclass(frozen=True, slots=True)
class GoogleOAuthClientImportReceiptV1:
    account_ref: str
    client_ref: str
    display_name: str
    inventory_generation: int
    client_digest: str

    def __post_init__(self) -> None:
        _ref(self.account_ref, "account_ref")
        _token(self.client_ref, "client_ref")
        _label(self.display_name, "display_name")
        _generation(self.inventory_generation, "inventory_generation")
        _plan_digest(self.client_digest)


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
class GoogleBillingPlanV1:
    id: str
    account_ref: str
    inventory_generation: int
    snapshot_fingerprint: str
    project_ref: str
    billing_ref: str
    plan_digest: str
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _token(self.id, "id")
        _ref(self.account_ref, "account_ref")
        _generation(self.inventory_generation, "inventory_generation")
        _snapshot_fingerprint(self.snapshot_fingerprint)
        _ref(self.project_ref, "project_ref")
        _ref(self.billing_ref, "billing_ref")
        _plan_digest(self.plan_digest)
        _timestamp_value(self.created_at, "created_at")
        _timestamp_value(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            _invalid("expires_at")


@dataclass(frozen=True, slots=True)
class GoogleBillingReceiptV1:
    plan_id: str
    state: str
    attempted: int
    completed: int
    failed: int
    not_attempted: int
    reason_code: str

    def __post_init__(self) -> None:
        _token(self.plan_id, "plan_id")
        if _code(self.state, "state") not in {"succeeded", "partial"}:
            _invalid("state")
        _count(self.attempted, "attempted", _MAX_COUNT)
        _count(self.completed, "completed", _MAX_COUNT)
        _count(self.failed, "failed", _MAX_COUNT)
        _count(self.not_attempted, "not_attempted", _MAX_COUNT)
        _code(self.reason_code, "reason_code")
        if self.attempted != self.completed + self.failed:
            _invalid("attempted")
        if self.state == "succeeded" and self.failed != 0:
            _invalid("state")


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
class HostProbeResultV1:
    kernel_class: str
    architecture_class: str
    cpu_count: int
    memory_class: str
    cgroup_v2: bool
    systemd: bool
    load_class: str
    pressure_class: str
    ollama_capability: bool
    observed_at: datetime
    agent_generation: int
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.kernel_class not in {"linux", "other"}:
            _invalid("kernel_class")
        if self.architecture_class not in {"x86_64", "arm64", "other"}:
            _invalid("architecture_class")
        _count(self.cpu_count, "cpu_count", 2**31 - 1)
        if self.cpu_count == 0 or self.memory_class not in {
            "under-8-gib",
            "8-31-gib",
            "32-127-gib",
            "128-plus-gib",
        }:
            _invalid("memory_class")
        for field in ("cgroup_v2", "systemd", "ollama_capability"):
            _bool(getattr(self, field), field)
        if self.load_class not in {"idle", "busy", "saturated"}:
            _invalid("load_class")
        if self.pressure_class not in {"none", "low", "elevated"}:
            _invalid("pressure_class")
        _timestamp_value(self.observed_at, "observed_at")
        if (
            type(self.agent_generation) is not int
            or not 1 <= self.agent_generation <= _MAX_GENERATION
        ):
            _invalid("agent_generation")
        _plan_digest(self.evidence_digest)


@dataclass(frozen=True, slots=True)
class OllamaPlanResultV1:
    plan_ref: str

    def __post_init__(self) -> None:
        _token(self.plan_ref, "plan_ref")


@dataclass(frozen=True, slots=True)
class OllamaApplyResultV1:
    instance_ref: str
    generation: int

    def __post_init__(self) -> None:
        _token(self.instance_ref, "instance_ref")
        _generation(self.generation, "generation")


@dataclass(frozen=True, slots=True)
class OllamaProbeResultV1:
    ready: bool
    reason_codes: tuple[str, ...]
    process_running: bool
    cgroup_member: bool
    loopback_endpoint_reachable: bool
    available_model_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in (
            "ready",
            "process_running",
            "cgroup_member",
            "loopback_endpoint_reachable",
        ):
            _bool(getattr(self, field), field)
        _public_agent_reason_codes(self.reason_codes)
        if type(self.available_model_ids) is not tuple or len(self.available_model_ids) > 64:
            _invalid("available_model_ids")
        if len(set(self.available_model_ids)) != len(self.available_model_ids):
            _invalid("available_model_ids")
        for model_id in self.available_model_ids:
            _token(model_id, "available_model_ids")


@dataclass(frozen=True, slots=True)
class OllamaStopResultV1:
    stopped: bool

    def __post_init__(self) -> None:
        _bool(self.stopped, "stopped")


@dataclass(frozen=True, slots=True)
class ControlOperationStatusV1:
    operation: ControlOperation
    result_kind: str | None
    result: (
        HostProbeResultV1
        | OllamaPlanResultV1
        | OllamaApplyResultV1
        | OllamaProbeResultV1
        | OllamaStopResultV1
        | None
    )

    def __post_init__(self) -> None:
        if type(self.operation) is not ControlOperation:
            _invalid("operation")
        if self.result_kind is None or self.result is None:
            if self.result_kind is not None or self.result is not None:
                _invalid("result")
            return
        expected = {
            "host.probe": HostProbeResultV1,
            "ollama.instance.plan": OllamaPlanResultV1,
            "ollama.instance.apply": OllamaApplyResultV1,
            "ollama.instance.probe": OllamaProbeResultV1,
            "ollama.instance.stop": OllamaStopResultV1,
        }.get(self.result_kind)
        if (
            expected is None
            or type(self.result) is not expected
            or self.operation.state not in _TERMINAL_OPERATION_STATES
            or any(code not in _PUBLIC_AGENT_REASON_CODES for code in self.operation.reason_codes)
        ):
            _invalid("result")


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


def parse_google_account_list(payload: object) -> GoogleControlAccountList:
    data = _document(payload, {"schema_version", "accounts", "registry_generation"})
    accounts = _list(data["accounts"], "accounts", _MAX_ACCOUNTS)
    return GoogleControlAccountList(
        accounts=tuple(_parse_google_account(account) for account in accounts),
        registry_generation=_generation(data["registry_generation"], "registry_generation"),
    )


def parse_google_account_add_receipt(payload: object) -> GoogleAccountAddReceiptV1:
    data = _document(payload, {"schema_version", "account"})
    account = _mapping(data["account"], {"ref", "generation"})
    return GoogleAccountAddReceiptV1(
        account_ref=_ref(account["ref"], "account_ref"),
        resulting_generation=_generation(account["generation"], "resulting_generation"),
    )


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


def parse_google_oauth_client_import_plan(
    payload: object,
) -> GoogleOAuthClientImportPlanV1:
    data = _document(
        payload,
        {
            "schema_version",
            "id",
            "account_ref",
            "expected_generation",
            "expires_at",
            "plan_digest",
        },
    )
    return GoogleOAuthClientImportPlanV1(
        id=_token(data["id"], "id"),
        account_ref=_ref(data["account_ref"], "account_ref"),
        expected_generation=_generation(
            data["expected_generation"], "expected_generation"
        ),
        expires_at=_epoch_seconds(data["expires_at"], "expires_at"),
        plan_digest=_plan_digest(data["plan_digest"]),
    )


def parse_google_oauth_client_import_receipt(
    payload: object,
) -> GoogleOAuthClientImportReceiptV1:
    data = _document(
        payload,
        {
            "schema_version",
            "account_ref",
            "client_ref",
            "display_name",
            "inventory_generation",
            "client_digest",
        },
    )
    return GoogleOAuthClientImportReceiptV1(
        account_ref=_ref(data["account_ref"], "account_ref"),
        client_ref=_token(data["client_ref"], "client_ref"),
        display_name=_label(data["display_name"], "display_name"),
        inventory_generation=_generation(
            data["inventory_generation"], "inventory_generation"
        ),
        client_digest=_plan_digest(data["client_digest"]),
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


def parse_google_billing_plan(payload: object) -> GoogleBillingPlanV1:
    data = _mapping(
        payload,
        {
            "id",
            "account_ref",
            "inventory_generation",
            "snapshot_fingerprint",
            "project_ref",
            "billing_ref",
            "plan_digest",
            "created_at",
            "expires_at",
        },
    )
    return GoogleBillingPlanV1(
        id=_token(data["id"], "id"),
        account_ref=_ref(data["account_ref"], "account_ref"),
        inventory_generation=_generation(data["inventory_generation"], "inventory_generation"),
        snapshot_fingerprint=_snapshot_fingerprint(data["snapshot_fingerprint"]),
        project_ref=_ref(data["project_ref"], "project_ref"),
        billing_ref=_ref(data["billing_ref"], "billing_ref"),
        plan_digest=_plan_digest(data["plan_digest"]),
        created_at=_timestamp(data["created_at"], "created_at"),
        expires_at=_timestamp(data["expires_at"], "expires_at"),
    )


def parse_google_billing_receipt(payload: object) -> GoogleBillingReceiptV1:
    data = _mapping(
        payload,
        {
            "plan_id",
            "state",
            "attempted",
            "completed",
            "failed",
            "not_attempted",
            "reason_code",
        },
    )
    return GoogleBillingReceiptV1(
        plan_id=_token(data["plan_id"], "plan_id"),
        state=_code(data["state"], "state"),
        attempted=_count(data["attempted"], "attempted", _MAX_COUNT),
        completed=_count(data["completed"], "completed", _MAX_COUNT),
        failed=_count(data["failed"], "failed", _MAX_COUNT),
        not_attempted=_count(data["not_attempted"], "not_attempted", _MAX_COUNT),
        reason_code=_code(data["reason_code"], "reason_code"),
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


def parse_operation_status(payload: object) -> ControlOperationStatusV1:
    """Parse the exact public ``operations.get`` envelope."""

    data = _document(payload, _OPERATION_STATUS_FIELDS)
    operation = parse_control_operation(
        {field: data[field] for field in _OPERATION_STATUS_FIELDS - {"result_kind", "result"}}
    )
    result_kind = data["result_kind"]
    result_payload = data["result"]
    if result_kind is None and result_payload is None:
        return ControlOperationStatusV1(operation, None, None)
    if type(result_kind) is not str or result_payload is None:
        _invalid("result")
    result = _parse_agent_result(result_kind, result_payload)
    return ControlOperationStatusV1(operation, result_kind, result)


def _parse_agent_result(
    result_kind: str, payload: object
) -> (
    HostProbeResultV1
    | OllamaPlanResultV1
    | OllamaApplyResultV1
    | OllamaProbeResultV1
    | OllamaStopResultV1
):
    if result_kind == "host.probe":
        data = _mapping(
            payload,
            {
                "kernel_class",
                "architecture_class",
                "cpu_count",
                "memory_class",
                "cgroup_v2",
                "systemd",
                "load_class",
                "pressure_class",
                "ollama_capability",
                "observed_at",
                "agent_generation",
                "evidence_digest",
            },
        )
        observed_at = data["observed_at"]
        if type(observed_at) is not str or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
            observed_at,
        ):
            _invalid("observed_at")
        result = HostProbeResultV1(
            kernel_class=data["kernel_class"],  # type: ignore[arg-type]
            architecture_class=data["architecture_class"],  # type: ignore[arg-type]
            cpu_count=data["cpu_count"],  # type: ignore[arg-type]
            memory_class=data["memory_class"],  # type: ignore[arg-type]
            cgroup_v2=data["cgroup_v2"],  # type: ignore[arg-type]
            systemd=data["systemd"],  # type: ignore[arg-type]
            load_class=data["load_class"],  # type: ignore[arg-type]
            pressure_class=data["pressure_class"],  # type: ignore[arg-type]
            ollama_capability=data["ollama_capability"],  # type: ignore[arg-type]
            observed_at=_timestamp(observed_at, "observed_at"),
            agent_generation=data["agent_generation"],  # type: ignore[arg-type]
            evidence_digest=data["evidence_digest"],  # type: ignore[arg-type]
        )
        evidence = {
            "kernel_class": data["kernel_class"],
            "architecture_class": data["architecture_class"],
            "cpu_count": data["cpu_count"],
            "memory_class": data["memory_class"],
            "cgroup_v2": data["cgroup_v2"],
            "systemd": data["systemd"],
            "load_class": data["load_class"],
            "pressure_class": data["pressure_class"],
            "ollama_capability": data["ollama_capability"],
            "observed_at": observed_at,
            "agent_generation": data["agent_generation"],
        }
        digest = "sha256:" + hashlib.sha256(
            json.dumps(
                evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("ascii")
        ).hexdigest()
        if result.evidence_digest != digest:
            _invalid("evidence_digest")
        return result
    if result_kind == "ollama.instance.plan":
        data = _mapping(payload, {"plan_ref"})
        return OllamaPlanResultV1(data["plan_ref"])  # type: ignore[arg-type]
    if result_kind == "ollama.instance.apply":
        data = _mapping(payload, {"instance_ref", "generation"})
        return OllamaApplyResultV1(
            data["instance_ref"], data["generation"]  # type: ignore[arg-type]
        )
    if result_kind == "ollama.instance.probe":
        data = _mapping(
            payload,
            {
                "ready",
                "reason_codes",
                "process_running",
                "cgroup_member",
                "loopback_endpoint_reachable",
                "available_model_ids",
            },
        )
        model_ids = _list(data["available_model_ids"], "available_model_ids", 64)
        return OllamaProbeResultV1(
            ready=data["ready"],  # type: ignore[arg-type]
            reason_codes=_public_agent_reason_codes(
                tuple(_list(data["reason_codes"], "reason_codes", _MAX_REASON_CODES))
            ),
            process_running=data["process_running"],  # type: ignore[arg-type]
            cgroup_member=data["cgroup_member"],  # type: ignore[arg-type]
            loopback_endpoint_reachable=data["loopback_endpoint_reachable"],  # type: ignore[arg-type]
            available_model_ids=tuple(model_ids),  # type: ignore[arg-type]
        )
    if result_kind == "ollama.instance.stop":
        data = _mapping(payload, {"stopped"})
        return OllamaStopResultV1(data["stopped"])  # type: ignore[arg-type]
    _invalid("result_kind")


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
            "enabled",
            "subject_bound",
            "oauth_state",
            "inventory_generation",
            "quota_state",
            "project_count",
            "billing_count",
            "billing_refs",
            "reload_state",
            "default_oauth_client_ref",
            "oauth_client_availability",
        },
    )
    return GoogleControlAccount(
        ref=_ref(data["ref"], "ref"),
        label=_label(data["label"], "label"),
        enabled=_bool(data["enabled"], "enabled"),
        subject_bound=_bool(data["subject_bound"], "subject_bound"),
        oauth_state=_code(data["oauth_state"], "oauth_state"),
        inventory_generation=_generation(data["inventory_generation"], "inventory_generation"),
        quota_state=_code(data["quota_state"], "quota_state"),
        project_count=_count(data["project_count"], "project_count", _MAX_COUNT),
        billing_count=_count(data["billing_count"], "billing_count", _MAX_COUNT),
        billing_refs=tuple(
            _ref(item, "billing_refs")
            for item in _list(data["billing_refs"], "billing_refs", _MAX_COUNT)
        ),
        reload_state=_code(data["reload_state"], "reload_state"),
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


def _snapshot_fingerprint(value: object) -> str:
    if type(value) is not str or not _SNAPSHOT_FINGERPRINT_RE.fullmatch(value):
        _invalid("snapshot_fingerprint")
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


def _public_agent_reason_codes(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > _MAX_REASON_CODES:
        _invalid("reason_codes")
    result = tuple(_code(code, "reason_codes") for code in value)
    if (
        len(set(result)) != len(result)
        or any(code not in _PUBLIC_AGENT_REASON_CODES for code in result)
    ):
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
