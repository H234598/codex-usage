from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from codex_usage.masterjet_contracts import (
    ControlContractError,
    ControlOperation,
    ControlProblem,
    GoogleControlAccount,
    GoogleControlProject,
    OpenAIControlAccount,
    parse_control_operation,
    parse_control_problem,
    parse_google_accounts,
    parse_google_project,
    parse_openai_accounts,
)


def valid_google_project() -> dict[str, object]:
    return {
        "ref": "the-hive-1",
        "project_name": "Amber Orchard",
        "purpose": "Primary usage",
        "key_name": "Willow Meadow",
        "billing_ref": "billing-1",
        "status": "active",
        "probe_state": "ready",
        "quota_state": "available",
    }


def valid_google_account() -> dict[str, object]:
    return {
        "ref": "google-1",
        "label": "Google primary",
        "enabled": True,
        "subject_bound": True,
        "oauth_state": "ready",
        "inventory_generation": 4,
        "quota_state": "available",
        "project_count": 1,
        "billing_count": 1,
        "reload_state": "current",
    }


def valid_openai_account() -> dict[str, object]:
    return {
        "ref": "openai-1",
        "label": "OpenAI primary",
        "enabled": True,
        "local_profile_ref": "profile-1",
        "source_host_ref": "host-1",
        "auth_state": "ready",
        "access_expires_at": "2026-08-28T12:00:00Z",
        "credential_generation": 8,
        "vault_projection_state": "current",
        "usage_state": "fresh",
    }


def valid_operation() -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": "operation-1",
        "kind": "google.provision.apply",
        "state": "partial",
        "expected_generation": 4,
        "resulting_generation": None,
        "plan_digest": "sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        "created_at": "2026-08-28T12:00:00Z",
        "expires_at": "2026-08-28T12:30:00Z",
        "completed_count": 2,
        "failed_count": 1,
        "not_attempted_count": 3,
        "reason_codes": ["quota.provider_exhausted"],
    }


def valid_problem() -> dict[str, object]:
    return {
        "schema_version": 1,
        "code": "quota.provider_exhausted",
        "severity": "warning",
        "title": "Provider quota exhausted",
        "detail": "Try again after quota refresh.",
        "effect": "Provisioning stopped.",
        "action": "Refresh quota evidence.",
        "retryable": True,
        "retry_after_seconds": 60,
        "correlation_id": "correlation-1",
        "occurred_at": "2026-08-28T12:00:00Z",
    }


def test_google_projection_rejects_provider_secret_fields():
    payload = valid_google_project() | {"project_id": "private"}

    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_project(payload)


def test_unknown_major_schema_is_rejected():
    with pytest.raises(ControlContractError, match=r"control\.schema_unsupported"):
        parse_google_accounts({"schema_version": 2, "accounts": []})


def test_google_project_is_immutable_redacted_projection():
    project = parse_google_project(valid_google_project())

    assert project == GoogleControlProject(
        ref="the-hive-1",
        project_name="Amber Orchard",
        purpose="Primary usage",
        key_name="Willow Meadow",
        billing_ref="billing-1",
        status="active",
        probe_state="ready",
        quota_state="available",
    )
    with pytest.raises(FrozenInstanceError):
        project.status = "blocked"  # type: ignore[misc]


def test_google_project_rejects_names_that_break_visible_name_rule():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_project(valid_google_project() | {"project_name": "Amber 2"})


def test_google_project_rejects_non_utf8_name_as_contract_error():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_project(valid_google_project() | {"project_name": "\ud800"})


def test_google_project_rejects_secret_marker_in_visible_name():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_project(valid_google_project() | {"project_name": "Prompt Secret"})


def test_google_accounts_reject_unknown_nested_fields():
    account = valid_google_account() | {"oauth_client_json": "private"}

    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_accounts({"schema_version": 1, "accounts": [account]})


def test_google_accounts_reject_count_above_local_safety_limit():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_accounts(
            {"schema_version": 1, "accounts": [valid_google_account()] * 257}
        )


def test_google_accounts_reject_project_count_above_local_safety_limit():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_accounts(
            {
                "schema_version": 1,
                "accounts": [valid_google_account() | {"project_count": 257}],
            }
        )


def test_openai_accounts_return_only_redacted_fields():
    accounts = parse_openai_accounts(
        {"schema_version": 1, "accounts": [valid_openai_account()]}
    )

    assert accounts[0].ref == "openai-1"
    assert accounts[0].access_expires_at == datetime(2026, 8, 28, 12, tzinfo=UTC)
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_openai_accounts(
            {
                "schema_version": 1,
                "accounts": [valid_openai_account() | {"access_token": "private"}],
            }
        )


def test_openai_accounts_reject_non_utc_timestamp():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_openai_accounts(
            {
                "schema_version": 1,
                "accounts": [
                    valid_openai_account()
                    | {"access_expires_at": "2026-08-28T14:00:00+02:00"}
                ],
            }
        )


def test_google_project_list_accepts_versioned_complete_projection():
    from codex_usage import masterjet_contracts

    projects = masterjet_contracts.parse_google_projects(
        {
            "schema_version": 1,
            "account_ref": "google-1",
            "inventory_generation": 4,
            "projects": [valid_google_project()],
        }
    )

    assert projects == (parse_google_project(valid_google_project()),)


def test_google_project_list_rejects_duplicate_refs():
    from codex_usage import masterjet_contracts

    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        masterjet_contracts.parse_google_projects(
            {
                "schema_version": 1,
                "account_ref": "google-1",
                "inventory_generation": 4,
                "projects": [valid_google_project(), valid_google_project()],
            }
        )


def test_google_project_list_rejects_unknown_top_level_field():
    from codex_usage import masterjet_contracts

    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        masterjet_contracts.parse_google_projects(
            {
                "schema_version": 1,
                "account_ref": "google-1",
                "inventory_generation": 4,
                "projects": [],
                "credential": "private",
            }
        )


def test_google_project_list_rejects_count_above_local_safety_limit():
    from codex_usage import masterjet_contracts

    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        masterjet_contracts.parse_google_projects(
            {
                "schema_version": 1,
                "account_ref": "google-1",
                "inventory_generation": 4,
                "projects": [valid_google_project()] * 257,
            }
        )


@pytest.mark.parametrize(
    "text",
    [
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
        "access_token=private",
        "file:///home/teladi/.config/auth.json",
        r"\\server\share\oauth.json",
        "Provider raw output: {\"credential\": \"private\"}",
        "Prompt: summarize this secret",
        "AIza" + "A" * 35,
        '{"credential":"private"}',
    ],
)
def test_text_projection_rejects_secret_path_and_provider_output_markers(text):
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_accounts(
            {"schema_version": 1, "accounts": [valid_google_account() | {"label": text}]}
        )


@pytest.mark.parametrize("text", ["\ud800", "name\u007f", "name\u0085", "name\u202e", "name\u200b"])
def test_text_projection_rejects_non_utf8_and_ui_control_characters(text):
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_accounts(
            {"schema_version": 1, "accounts": [valid_google_account() | {"label": text}]}
        )


def test_text_projection_enforces_utf8_byte_budget():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_accounts(
            {
                "schema_version": 1,
                "accounts": [valid_google_account() | {"label": "é" * 129}],
            }
        )


def test_control_operation_parses_bounded_timestamp_and_reason_codes():
    operation = parse_control_operation(valid_operation())

    assert operation.state == "partial"
    assert operation.reason_codes == ("quota.provider_exhausted",)
    assert operation.expires_at == datetime(2026, 8, 28, 12, 30, tzinfo=UTC)


def test_control_operation_accepts_complete_spec_v1_fixture():
    operation = parse_control_operation(valid_operation())

    assert operation.id == "operation-1"
    assert operation.completed_count == 2
    assert operation.not_attempted_count == 3


def test_control_operation_rejects_unknown_state():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_operation(valid_operation() | {"state": "waiting"})


@pytest.mark.parametrize(
    "field, value",
    [
        ("completed_count", 10_001),
        ("failed_count", True),
        ("not_attempted_count", 10_001),
        ("expires_at", "2026-08-30T12:00:01Z"),
    ],
)
def test_control_operation_rejects_unsafe_count_and_time_values(field, value):
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_operation(valid_operation() | {field: value})


def test_control_operation_rejects_timestamp_outside_ui_range():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_operation(
            valid_operation()
            | {
                "created_at": "9999-12-31T00:00:00Z",
                "expires_at": "9999-12-31T00:30:00Z",
            }
        )


def test_control_operation_constructor_rejects_mutable_reason_codes():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        ControlOperation(
            id="operation-1",
            kind="google.provision.apply",
            state="partial",
            expected_generation=4,
            resulting_generation=None,
            plan_digest="sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
            created_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
            expires_at=datetime(2026, 8, 28, 12, 30, tzinfo=UTC),
            completed_count=2,
            failed_count=1,
            not_attempted_count=3,
            reason_codes=["quota.provider_exhausted"],  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: OpenAIControlAccount(
            ref="openai-1",
            label="OpenAI primary",
            enabled=True,
            local_profile_ref="profile-1",
            source_host_ref="host-1",
            auth_state="ready",
            access_expires_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
            credential_generation=True,
            vault_projection_state="current",
            usage_state="fresh",
        ),
        lambda: GoogleControlAccount(
            ref="google-1",
            label="Google primary",
            enabled=True,
            subject_bound=True,
            oauth_state="ready",
            inventory_generation=4,
            quota_state="available",
            project_count=True,
            billing_count=1,
            reload_state="current",
        ),
        lambda: GoogleControlProject(
            ref="the-hive-1",
            project_name="Amber Orchard",
            purpose="Primary usage",
            key_name="Willow Meadow",
            billing_ref="billing-1",
            status=True,
            probe_state="ready",
            quota_state="available",
        ),
        lambda: ControlProblem(
            code="quota.provider_exhausted",
            severity="warning",
            title="Provider quota exhausted",
            detail="Try again after quota refresh.",
            effect="Provisioning stopped.",
            action="Refresh quota evidence.",
            retryable=True,
            retry_after_seconds=True,
            correlation_id="correlation-1",
            occurred_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
        ),
    ],
)
def test_direct_dto_constructors_reject_invalid_types(factory):
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        factory()


def test_control_problem_parses_only_safe_structured_error():
    problem = parse_control_problem(valid_problem())

    assert problem.code == "quota.provider_exhausted"
    assert problem.retry_after_seconds == 60
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_problem(valid_problem() | {"provider_id": "private"})


def test_control_problem_accepts_complete_spec_v1_fixture():
    problem = parse_control_problem(valid_problem())

    assert problem.title == "Provider quota exhausted"
    assert problem.occurred_at == datetime(2026, 8, 28, 12, tzinfo=UTC)


def test_control_problem_rejects_retry_delay_above_ui_limit():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_problem(valid_problem() | {"retry_after_seconds": 86_401})
