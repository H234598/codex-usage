from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from codex_usage.masterjet_client import _encode_request
from codex_usage.masterjet_contracts import parse_google_accounts

ADMIN_ROOT = Path("/home/teladi/.codex-worktrees/codex-master/admin-control-20260828")
sys.path.insert(0, str(ADMIN_ROOT / "src"))
sys.path.insert(0, str(ADMIN_ROOT / "tests"))

from codex_master.admin_contracts import AdminRequestV1, parse_admin_request  # noqa: E402
from test_admin_service import principal, service_at  # noqa: E402

DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("operation", "arguments", "generation", "idempotency_key", "plan_digest"),
    [
        ("hosts.list", {}, None, None, None),
        ("openai.accounts.list", {}, None, None, None),
        ("google.accounts.list", {}, None, None, None),
        ("google.projects.list", {"account_ref": "google-one"}, None, None, None),
        (
            "operations.get",
            {"account_ref": "google-one", "operation_id": "operation-one"},
            None,
            None,
            None,
        ),
        ("openai.auth.plan", {"account_ref": "openai-one"}, 4, "idem-openai-plan", None),
        (
            "openai.auth.apply",
            {"account_ref": "openai-one"},
            4,
            "idem-openai-apply",
            DIGEST,
        ),
        (
            "secret.ingress.create",
            {
                "account_ref": "openai-one",
                "credential_kind": "openai.auth-json",
            },
            4,
            "idem-ingress",
            DIGEST,
        ),
        (
            "google.oauth.begin",
            {
                "account_ref": "google-one",
                "oauth_client_ref": "client-one",
                "redirect_uri": "http://127.0.0.1:8765/callback",
                "scope_profile": "inventory_readonly",
            },
            4,
            "idem-oauth-begin",
            None,
        ),
        (
            "google.oauth.complete",
            {
                "account_ref": "google-one",
                "transaction_id": "transaction-one",
                "redirect_uri": "http://127.0.0.1:8765/callback",
                "state": "state-one",
            },
            4,
            None,
            None,
        ),
        (
            "google.oauth-client-import.plan",
            {"account_ref": "google-one"},
            4,
            "idem-client-plan",
            None,
        ),
        (
            "google.oauth-client-import.apply",
            {"account_ref": "google-one"},
            4,
            "idem-client-apply",
            DIGEST,
        ),
        ("google.inventory.refresh", {}, 4, "idem-inventory", None),
        (
            "google.provision.plan",
            {"account_ref": "google-one"},
            4,
            "idem-provision-plan",
            None,
        ),
        (
            "google.provision.apply",
            {"account_ref": "google-one"},
            4,
            "idem-provision-apply",
            DIGEST,
        ),
        (
            "google.billing.plan",
            {
                "account_ref": "google-one",
                "project_ref": "project-one",
                "billing_ref": "billing-one",
            },
            4,
            "idem-billing-plan",
            None,
        ),
        (
            "google.billing.apply",
            {
                "account_ref": "google-one",
                "project_ref": "project-one",
                "billing_ref": "billing-one",
                "plan_id": "billing-plan-one",
            },
            4,
            "idem-billing-apply",
            DIGEST,
        ),
    ],
)
def test_usage_request_is_accepted_by_real_admin_v1_parser(
    operation: str,
    arguments: dict[str, object],
    generation: int | None,
    idempotency_key: str | None,
    plan_digest: str | None,
) -> None:
    encoded, secret = _encode_request(
        operation,
        arguments,
        expected_generation=generation,
        idempotency_key=idempotency_key,
        plan_digest=plan_digest,
    )

    assert secret is None
    parsed = parse_admin_request(json.loads(encoded))
    assert type(parsed) is AdminRequestV1
    assert parsed.operation == operation
    assert dict(parsed.arguments) == arguments
    assert parsed.expected_generation == generation
    assert parsed.idempotency_key == idempotency_key
    assert parsed.plan_digest == plan_digest


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("openai.auth.apply", {"account_ref": "openai-one", "plan_id": "plan-one"}),
        (
            "secret.ingress.create",
            {
                "account_ref": "openai-one",
                "credential_kind": "openai.auth-json",
                "plan_id": "plan-one",
            },
        ),
        (
            "google.oauth-client-import.apply",
            {"account_ref": "google-one", "plan_id": "plan-one"},
        ),
        (
            "google.provision.apply",
            {"account_ref": "google-one", "plan_id": "plan-one"},
        ),
    ],
)
def test_usage_never_emits_legacy_plan_id(
    operation: str, arguments: dict[str, str]
) -> None:
    with pytest.raises(Exception, match=r"control\.request_invalid"):
        _encode_request(
            operation,
            arguments,
            expected_generation=4,
            idempotency_key="idem-openai-apply",
            plan_digest=DIGEST,
        )


def test_usage_parses_real_admin_default_oauth_client_projection() -> None:
    service, _owners = service_at()
    payload = service.query(principal("fleet.read"), "google.accounts.list", {})

    accounts = parse_google_accounts({"schema_version": 1, **payload})

    assert len(accounts) == 1
    assert accounts[0].default_oauth_client_ref == "oauth-client-opaque"
    assert accounts[0].oauth_client_availability == "available"
