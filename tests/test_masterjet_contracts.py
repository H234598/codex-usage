from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from codex_usage.masterjet_contracts import (
    ControlContractError,
    GoogleControlProject,
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


def test_google_accounts_reject_unknown_nested_fields():
    account = valid_google_account() | {"oauth_client_json": "private"}

    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_accounts({"schema_version": 1, "accounts": [account]})


def test_google_accounts_reject_count_above_local_safety_limit():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_accounts(
            {"schema_version": 1, "accounts": [valid_google_account()] * 257}
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


def test_control_operation_parses_bounded_timestamp_and_reason_codes():
    operation = parse_control_operation(valid_operation())

    assert operation.state == "partial"
    assert operation.reason_codes == ("quota.provider_exhausted",)
    assert operation.expires_at == datetime(2026, 8, 28, 12, 30, tzinfo=UTC)


def test_control_operation_rejects_unknown_state():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_operation(valid_operation() | {"state": "waiting"})


def test_control_problem_parses_only_safe_structured_error():
    problem = parse_control_problem(valid_problem())

    assert problem.code == "quota.provider_exhausted"
    assert problem.retry_after_seconds == 60
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_problem(valid_problem() | {"provider_id": "private"})
