from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from codex_usage import masterjet_contracts
from codex_usage.masterjet_contracts import (
    ControlContractError,
    ControlOperation,
    ControlProblem,
    GoogleControlAccount,
    GoogleControlProject,
    GoogleOAuthTransactionV1,
    OpenAIControlAccount,
    SecretIngressReceipt,
    SecretIngressSession,
    parse_control_operation,
    parse_control_problem,
    parse_google_accounts,
    parse_google_oauth_transaction,
    parse_google_project,
    parse_openai_accounts,
    parse_secret_ingress_receipt,
    parse_secret_ingress_session,
)


def valid_google_project() -> dict[str, object]:
    return {
        "ref": "the-hive-1",
        "project_name": "Amber Orchard",
        "purpose": "quota_probe",
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
        "subject_bound": True,
        "inventory_generation": 4,
        "project_count": 1,
        "billing_count": 1,
        "default_oauth_client_ref": "oauth-client-1",
        "oauth_client_availability": "available",
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


def valid_google_provision_plan() -> dict[str, object]:
    return valid_operation() | {
        "id": "plan-1",
        "kind": "google.provision.plan",
        "state": "planned",
        "account_ref": "google-1",
        "step_count": 5,
        "projects": [
            {"project_name": "Amber Orchard", "key_name": "Willow Meadow"},
            {"project_name": "Velvet Harbor", "key_name": "Silver Forest"},
        ],
    }


def valid_google_oauth_transaction() -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": "oauth-1",
        "account_ref": "google-1",
        "authorization_url": (
            "https://accounts.google.com/o/oauth2/v2/auth?"
            "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Foauth%2Fcallback"
            "&state=state-one"
        ),
        "expires_at": 1_777_463_500.0,
        "inventory_generation": 4,
    }


def valid_problem() -> dict[str, object]:
    return {
        "schema_version": 1,
        "code": "quota.provider_exhausted",
        "severity": "warning",
        "title": "Provider quota exhausted",
        "detail": "Provider quota is exhausted.",
        "effect": "Provisioning stopped.",
        "action": "Refresh quota evidence.",
        "retryable": True,
        "retry_after_seconds": 60,
        "correlation_id": "correlation-1",
        "occurred_at": "2026-08-28T12:00:00Z",
    }


def test_google_oauth_transaction_is_typed_redacted_and_immutable():
    transaction = parse_google_oauth_transaction(valid_google_oauth_transaction())

    assert transaction == GoogleOAuthTransactionV1(
        id="oauth-1",
        account_ref="google-1",
        authorization_url=(
            "https://accounts.google.com/o/oauth2/v2/auth?"
            "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Foauth%2Fcallback"
            "&state=state-one"
        ),
        expires_at=1_777_463_500.0,
        inventory_generation=4,
    )
    with pytest.raises(FrozenInstanceError):
        transaction.inventory_generation = 5  # type: ignore[misc]


@pytest.mark.parametrize(
    "field,value",
    [
        ("authorization_url", "http://accounts.google.com/o/oauth2/v2/auth"),
        ("authorization_url", "https://attacker.invalid/auth"),
        ("authorization_url", "https://user@accounts.google.com/auth"),
        ("authorization_url", "https://accounts.google.com/auth#private"),
        ("generation", True),
        ("account_ref", "ya29.private"),
        ("id", "GOCSPX-private"),
    ],
)
def test_google_oauth_transaction_rejects_untrusted_or_private_values(field, value):
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_oauth_transaction(valid_google_oauth_transaction() | {field: value})


@pytest.mark.parametrize(
    "authorization_url",
    [
        (
            "https://accounts.google.com/o/oauth2/v2/auth?client%5Fsecret=private&"
            "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Foauth%2Fcallback"
        ),
        (
            "https://accounts.google.com/o/oauth2/v2/auth?ClIeNt_SeCrEt=private&"
            "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Foauth%2Fcallback"
        ),
        (
            "https://accounts.google.com/o/oauth2/v2/auth?scope=openid&ScOpE=email&"
            "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Foauth%2Fcallback"
        ),
        (
            "https://accounts.google.com/o/oauth2/v2/auth?state=GOCSPX%2Dprivate&"
            "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Foauth%2Fcallback"
        ),
        (
            "https://accounts.google.com/o/oauth2/v2/auth?redirect_uri="
            "http%3A%2F%2Fattacker.invalid%2Fcallback"
        ),
        (
            "https://accounts.google.com/o/oauth2/v2/auth?redirect_uri="
            "http%3A%2F%2Fuser%40localhost%3A8765%2Fcallback"
        ),
        (
            "https://accounts.google.com/o/oauth2/v2/auth?redirect_uri="
            "http%3A%2F%2Flocalhost%3A8765%2Fcallback%3Fprivate%3D1"
        ),
        (
            "https://accounts.google.com/o/oauth2/v2/auth?unknown=value&"
            "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Foauth%2Fcallback"
        ),
        (
            "https://accounts.google.com/o/oauth2/auth?scope=openid&"
            "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Foauth%2Fcallback"
        ),
    ],
)
def test_google_oauth_transaction_rejects_encoded_or_ambiguous_auth_parameters(
    authorization_url,
):
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_oauth_transaction(
            valid_google_oauth_transaction()
            | {"authorization_url": authorization_url}
        )


def test_google_oauth_transaction_accepts_strict_loopback_callback():
    authorization_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?response_type=code"
        "&client_id=public-client.apps.googleusercontent.com"
        "&redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Fcallback"
        "&scope=openid&code_challenge=public-challenge"
        "&code_challenge_method=S256&state=public-state"
    )

    parsed = parse_google_oauth_transaction(
        valid_google_oauth_transaction() | {"authorization_url": authorization_url}
    )

    assert parsed.authorization_url == authorization_url


@pytest.mark.parametrize(
    "authorization_url",
    [
        (
            "https://accounts.google.com/o/oauth2/v2/auth?"
            "login_hint=owner%40example.test&"
            "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Foauth%2Fcallback"
        ),
        (
            "https://accounts.google.com/o/oauth2/v2/auth?"
            "redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Foauth%2Fcallback"
        ),
        "https://accounts.google.com/o/oauth2/v2/auth?scope=openid",
    ],
)
def test_google_oauth_transaction_rejects_login_hint_unbound_host_or_missing_callback(
    authorization_url,
):
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_oauth_transaction(
            valid_google_oauth_transaction()
            | {"authorization_url": authorization_url}
        )


def test_google_oauth_transaction_rejects_unknown_fields():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_oauth_transaction(valid_google_oauth_transaction() | {"state": "private"})


def valid_ingress_session() -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": "ingress-1",
        "account_ref": "openai-1",
        "state": "pending",
        "plan_digest": "sha256:" + "a" * 64,
        "expected_generation": 4,
        "expires_at": 1_777_463_500.0,
        "session_generation": 4,
    }


def valid_ingress_receipt() -> dict[str, object]:
    return {
        "schema_version": 1,
        "session_id": "ingress-1",
        "account_ref": "openai-1",
        "state": "consumed",
        "generation": 5,
    }


def test_secret_ingress_contracts_are_typed_immutable_and_redacted():
    session = parse_secret_ingress_session(valid_ingress_session())
    receipt = parse_secret_ingress_receipt(valid_ingress_receipt())

    assert session == SecretIngressSession(
        id="ingress-1",
        account_ref="openai-1",
        state="pending",
        plan_digest="sha256:" + "a" * 64,
        expected_generation=4,
        expires_at=1_777_463_500.0,
        session_generation=4,
    )
    assert receipt == SecretIngressReceipt(
        session_id="ingress-1",
        account_ref="openai-1",
        state="consumed",
        generation=5,
    )
    with pytest.raises(FrozenInstanceError):
        receipt.state = "pending"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (parse_secret_ingress_session, valid_ingress_session() | {"secret": "private"}),
        (parse_secret_ingress_receipt, valid_ingress_receipt() | {"secret": "private"}),
        (parse_secret_ingress_session, valid_ingress_session() | {"expected_generation": True}),
        (parse_secret_ingress_receipt, valid_ingress_receipt() | {"generation": -1}),
    ],
)
def test_secret_ingress_contracts_reject_unknown_or_invalid_fields(parser, payload):
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parser(payload)


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
        purpose="quota_probe",
        key_name="Willow Meadow",
        billing_ref="billing-1",
        status="active",
        probe_state="ready",
        quota_state="available",
    )
    with pytest.raises(FrozenInstanceError):
        project.status = "blocked"  # type: ignore[misc]


def test_full_plan_preview_is_typed_complete_and_redacted():
    plan = masterjet_contracts.parse_google_provision_plan(valid_google_provision_plan())

    assert plan == masterjet_contracts.GoogleProvisionPlanV1(
        id="plan-1",
        kind="google.provision.plan",
        state="planned",
        account_ref="google-1",
        expected_generation=4,
        resulting_generation=None,
        plan_digest="sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        created_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
        expires_at=datetime(2026, 8, 28, 12, 30, tzinfo=UTC),
        completed_count=2,
        failed_count=1,
        not_attempted_count=3,
        reason_codes=("quota.provider_exhausted",),
        step_count=5,
        projects=(
            masterjet_contracts.GoogleProvisionProjectV1("Amber Orchard", "Willow Meadow"),
            masterjet_contracts.GoogleProvisionProjectV1("Velvet Harbor", "Silver Forest"),
        ),
    )


@pytest.mark.parametrize(
    "private_field",
    ["project_id", "provider_id", "api_key", "client_secret", "access_token", "cookie"],
)
def test_full_plan_preview_rejects_private_project_fields(private_field):
    payload = valid_google_provision_plan()
    payload["projects"] = [payload["projects"][0] | {private_field: "private"}]

    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        masterjet_contracts.parse_google_provision_plan(payload)


def test_full_plan_preview_rejects_missing_name_pair_or_impossible_step_count():
    missing = valid_google_provision_plan()
    missing["projects"] = [{"project_name": "Amber Orchard"}]
    impossible = valid_google_provision_plan() | {"step_count": 1}

    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        masterjet_contracts.parse_google_provision_plan(missing)
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        masterjet_contracts.parse_google_provision_plan(impossible)


def test_google_project_rejects_names_that_break_visible_name_rule():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_project(valid_google_project() | {"project_name": "Amber 2"})


def test_google_project_rejects_non_utf8_name_as_contract_error():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_project(valid_google_project() | {"project_name": "\ud800"})


def test_google_project_accepts_visible_name_without_secret_value():
    project = parse_google_project(valid_google_project() | {"project_name": "Prompt Secret"})

    assert project.project_name == "Prompt Secret"


def test_account_labels_and_project_purpose_use_distinct_safe_contracts():
    accounts = parse_google_accounts(
        {
            "schema_version": 1,
            "accounts": [valid_google_account() | {"label": "Google account 01_BW"}],
        }
    )
    project = parse_google_project(valid_google_project())

    assert accounts[0].label == "Google account 01_BW"
    assert project.purpose == "quota_probe"


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
                "accounts": [valid_google_account() | {"project_count": 100_001}],
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

    assert projects.account_ref == "google-1"
    assert projects.inventory_generation == 4
    assert projects.projects == (parse_google_project(valid_google_project()),)


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


def test_google_project_list_rejects_unexpected_account_ref():
    from codex_usage import masterjet_contracts

    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        masterjet_contracts.parse_google_projects(
            {
                "schema_version": 1,
                "account_ref": "google-2",
                "inventory_generation": 4,
                "projects": [],
            },
            expected_account_ref="google-1",
        )


@pytest.mark.parametrize(
    "text",
    [
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
        "access_token=private",
        "ya29.A0ARrdaMabcdefghijk123456",
        "1//0abcdefghijklmnop",
        "GOCSPX-abcdefghijklmnop",
        "file:///home/teladi/.config/auth.json",
        r"\\server\share\oauth.json",
        "path=/home/teladi/.config/auth.json",
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


def test_control_operation_accepts_server_boundary_fixture():
    boundary_ref = "o" + "a" * 127
    operation = parse_control_operation(
        valid_operation()
        | {
            "id": boundary_ref,
            "expected_generation": 2**63 - 1,
            "resulting_generation": 2**63 - 1,
            "created_at": "9999-12-31T23:59:59.000000Z",
            "expires_at": "9999-12-31T23:59:59.999999Z",
            "completed_count": 100_000,
            "failed_count": 100_000,
            "not_attempted_count": 100_000,
        }
    )

    assert operation.id == boundary_ref
    assert operation.expected_generation == 2**63 - 1
    assert operation.completed_count == 100_000
    assert operation.expires_at.microsecond == 999_999


def test_operation_tokens_accept_server_grammar_at_128_bytes():
    token = "A._:-" + "x" * 123
    operation = parse_control_operation(
        valid_operation()
        | {
            "id": token,
            "kind": token,
            "reason_codes": [token],
        }
    )

    assert operation.id == token
    assert operation.kind == token
    assert operation.reason_codes == (token,)


def test_problem_correlation_id_accepts_server_token_grammar_at_128_bytes():
    token = "A._:-" + "x" * 123

    problem = parse_control_problem(valid_problem() | {"correlation_id": token})

    assert problem.correlation_id == token


@pytest.mark.parametrize("leading_character", [".", "_", ":", "-"])
def test_operation_id_rejects_leading_token_punctuation(leading_character):
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_operation(valid_operation() | {"id": f"{leading_character}operation"})


@pytest.mark.parametrize("leading_character", [".", "_", ":", "-"])
def test_hive_problem_correlation_id_rejects_leading_token_punctuation(
    leading_character,
):
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_problem(
            valid_problem() | {"correlation_id": f"{leading_character}correlation"}
        )


def test_control_operation_rejects_unknown_state():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_operation(valid_operation() | {"state": "waiting"})


def test_consumed_refs_and_tokens_reject_provider_secret_values():
    secret_ref = "sk-" + "a" * 40

    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_operation(valid_operation() | {"id": secret_ref})
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_problem(valid_problem() | {"correlation_id": secret_ref})
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_project(valid_google_project() | {"billing_ref": secret_ref})
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_openai_accounts(
            {
                "schema_version": 1,
                "accounts": [valid_openai_account() | {"local_profile_ref": secret_ref}],
            }
        )
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_operation(
            valid_operation() | {"reason_codes": ["ya29.abcdefghijk"]}
        )
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_operation(valid_operation() | {"id": "sk-..."})
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_operation(valid_operation() | {"reason_codes": ["ya29"]})


@pytest.mark.parametrize(
    "field, value",
    [
        ("completed_count", 100_001),
        ("failed_count", True),
        ("not_attempted_count", 100_001),
        ("expires_at", "2026-08-28T11:59:59Z"),
    ],
)
def test_control_operation_rejects_unsafe_count_and_time_values(field, value):
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_operation(valid_operation() | {field: value})


def test_control_operation_rejects_invalid_calendar_timestamp():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_operation(
            valid_operation()
            | {
                "created_at": "2026-02-30T00:00:00Z",
                "expires_at": "2026-02-30T00:30:00Z",
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
            subject_bound=True,
            inventory_generation=4,
            project_count=True,
            billing_count=1,
            default_oauth_client_ref="oauth-client-1",
            oauth_client_availability="available",
        ),
        lambda: GoogleControlProject(
            ref="the-hive-1",
            project_name="Amber Orchard",
            purpose="quota_probe",
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
            detail="Provider quota is exhausted.",
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


def test_control_problem_uses_local_credential_template():
    problem = parse_control_problem(
        valid_problem()
        | {
            "code": "credential.source_unavailable",
            "severity": "error",
            "title": "Credential source unavailable",
            "detail": "Token refresh required.",
            "effect": "Authentication is unavailable.",
            "action": "Restore the credential source.",
        }
    )

    assert problem.title == "Credential source unavailable"
    assert problem.detail == "Token refresh required."


def test_control_problem_rejects_remote_text_outside_local_template():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_problem(valid_problem() | {"detail": "Provider said limit 1"})


def test_control_problem_rejects_unknown_code():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_problem(valid_problem() | {"code": "provider.unknown"})


def test_control_problem_rejects_retry_delay_above_ui_limit():
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_problem(valid_problem() | {"retry_after_seconds": 86_401})


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), float("-inf")])
def test_schema_and_numeric_fields_reject_boolean_and_non_finite_values(value):
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_google_accounts({"schema_version": value, "accounts": []})
    with pytest.raises(ControlContractError, match=r"control\.response_invalid"):
        parse_control_operation(valid_operation() | {"completed_count": value})
