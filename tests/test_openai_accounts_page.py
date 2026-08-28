from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APPLET_DIR = ROOT / "files" / "codex-usage@H234598"
SCHEMA = APPLET_DIR / "settings-schema.json"


def _module():
    sys.path.insert(0, str(APPLET_DIR))
    sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings")
    sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings/bin")
    path = APPLET_DIR / "openai_accounts_page.py"
    spec = importlib.util.spec_from_file_location("openai_accounts_page", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _local_accounts():
    return [
        {
            "account": "BW_Work",
            "label": "Work",
            "local_auth_state": "ready",
            "auth_sync_required": False,
            "series-active": True,
        }
    ]


def _masterjet_accounts():
    return [
        {
            "ref": "openai-work",
            "label": "Work",
            "enabled": True,
            "local_profile_ref": "BW_Work",
            "source_host_ref": "host-one",
            "auth_state": "ready",
            "access_expires_at": "2026-08-28T18:00:00Z",
            "credential_generation": 7,
            "vault_projection_state": "synced",
            "usage_state": "available",
        }
    ]


def test_account_navigation_contains_openai_and_google_only() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    layout = schema["layout"]
    account_pages = [
        page for page in layout["pages"] if layout[page]["title"].startswith("Accounts")
    ]

    assert account_pages == ["openai-accounts-page", "google-accounts-page"]
    assert layout["openai-accounts-page"]["title"] == "Accounts · OpenAI"
    assert layout["google-accounts-page"]["title"] == "Accounts · Google"
    assert "ollama-accounts-page" not in layout["pages"]


def test_openai_page_reuses_existing_account_table_without_moving_other_pages() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    layout = schema["layout"]

    assert "account-backends" in layout["openai-accounts-section"]["keys"]
    assert [page for page in layout["pages"] if "accounts" not in page] == [
        "general-page",
        "format-page",
        "forecast-page",
        "status-page",
        "routing-page",
        "alerts-page",
        "help-page",
    ]


def test_openai_page_shows_local_vault_expiry_and_hive_state() -> None:
    page = _module().OpenAIAccountsModel()

    page.render(_local_accounts(), _masterjet_accounts())

    row = page.row("BW_Work")
    assert row.local_auth_state == "ready"
    assert row.vault_projection_state == "synced"
    assert row.access_expires_at == "2026-08-28T18:00:00Z"
    assert row.credential_generation == 7
    assert row.hive_active is True


def test_openai_page_rejects_secret_fields_and_never_keeps_private_paths() -> None:
    page = _module().OpenAIAccountsModel()
    local = _local_accounts()
    local[0]["auth-json"] = "/private/profile/codex-home/auth.json"
    remote = _masterjet_accounts()
    remote[0]["access_token"] = "marker-secret"

    with pytest.raises(ValueError, match="private"):
        page.render(local, remote)

    assert "marker-secret" not in repr(page)
    assert "/private/profile" not in repr(page)


def test_stale_openai_page_disables_all_mutations() -> None:
    page = _module().OpenAIAccountsModel()

    page.render(_local_accounts(), _masterjet_accounts(), stale=True)

    row = page.row("BW_Work")
    assert row.reauth_enabled is False
    assert row.auth_sync_enabled is False
    assert row.disable_enabled is False


def test_openai_actions_use_only_bounded_own_cli_commands() -> None:
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append((tuple(argv), stdin_data, callback))

    controller = _module().OpenAIActions(Runner(), executable="/opt/codex-usage")
    controller.reauthenticate("BW_Work")
    controller.sync_auth("BW_Work")

    assert calls == [
        (("/opt/codex-usage", "reactivate", "BW_Work"), None, None),
        (
            (
                "/opt/codex-usage",
                "account",
                "auth-sync",
                "BW_Work",
                "--format",
                "json",
            ),
            None,
            None,
        ),
    ]


@pytest.mark.parametrize(
    ("transport", "endpoint"),
    [
        ("local", "/run/user/1000/masterjet.sock"),
        ("https", "https://masterjet.example.test/admin/v1"),
    ],
)
def test_masterjet_endpoint_validation_accepts_safe_endpoints(transport, endpoint) -> None:
    assert _module().validate_masterjet_endpoint(transport, endpoint) == endpoint


@pytest.mark.parametrize(
    ("transport", "endpoint"),
    [
        ("local", "relative.sock"),
        ("https", "http://masterjet.example.test"),
        ("https", "https://user:pass@masterjet.example.test"),
        ("https", "https://masterjet.example.test/path?token=value"),
    ],
)
def test_masterjet_endpoint_validation_rejects_before_settings_write(transport, endpoint) -> None:
    module = _module()
    writes = []

    with pytest.raises(ValueError):
        module.save_masterjet_connection(
            lambda key, value: writes.append((key, value)), transport, endpoint
        )

    assert writes == []


def test_masterjet_schema_has_no_bearer_or_totp_settings() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    assert "masterjet-connection" in schema
    assert not any("bearer" in key.casefold() or "totp" in key.casefold() for key in schema)
