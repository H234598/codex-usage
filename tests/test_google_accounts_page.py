from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APPLET_DIR = ROOT / "files" / "codex-usage@H234598"


def _module():
    sys.path.insert(0, str(APPLET_DIR))
    sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings")
    sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings/bin")
    path = APPLET_DIR / "google_accounts_page.py"
    spec = importlib.util.spec_from_file_location("google_accounts_page", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _payload(*, stale: bool = False):
    return {
        "stale": stale,
        "accounts": [
            {
                "ref": "google-one",
                "label": "Google One",
                "enabled": True,
                "subject_bound": True,
                "oauth_state": "ready",
                "inventory_generation": 4,
                "quota_state": "fresh",
                "project_count": 2,
                "billing_count": 1,
                "reload_state": "ready",
            }
        ],
        "projects": {
            "google-one": [
                {
                    "ref": "hive-one",
                    "project_name": "Amber Meadow",
                    "purpose": "hive",
                    "key_name": "Quiet River",
                    "billing_ref": "billing-one",
                    "status": "active",
                    "probe_state": "ready",
                    "quota_state": "ready",
                },
                {
                    "ref": "hive-two",
                    "project_name": "Velvet Orchard",
                    "purpose": "hive",
                    "key_name": "Silver Fern",
                    "billing_ref": None,
                    "status": "blocked",
                    "probe_state": "blocked",
                    "quota_state": "exhausted",
                },
            ]
        },
    }


def test_google_widget_renders_account_cards_status_and_project_table() -> None:
    page = _module().GoogleAccountsModel()

    page.render(_payload())

    card = page.card("google-one")
    assert card.oauth_state == "ready"
    assert card.inventory_generation == 4
    assert card.quota_state == "fresh"
    assert [row.project_name for row in card.projects] == [
        "Amber Meadow",
        "Velvet Orchard",
    ]
    assert card.projects[0].key_name == "Quiet River"
    assert card.projects[1].status == "blocked"


def test_accounts_only_cli_payload_marks_project_details_unavailable() -> None:
    model = _module().GoogleAccountsModel()
    payload = _payload()

    model.render(payload["accounts"])

    assert model.details_available is False
    assert model.card("google-one").projects == ()


def test_google_widget_never_persists_secret_or_provider_id_fields() -> None:
    page = _module().GoogleAccountsModel()
    payload = _payload()
    payload["projects"]["google-one"][0]["project_id"] = "private-provider-id"

    with pytest.raises(ValueError, match="private"):
        page.render(payload)

    assert "private-provider-id" not in repr(page)
    assert "client_secret" not in repr(page).casefold()


def test_stale_google_page_disables_every_mutation() -> None:
    page = _module().GoogleAccountsModel()

    page.render(_payload(stale=True))

    card = page.card("google-one")
    assert card.add_enabled is False
    assert card.oauth_enabled is False
    assert card.inventory_enabled is False
    assert card.plan_enabled is False
    assert card.apply_enabled is False


def test_plan_preview_shows_every_name_and_step_count() -> None:
    page = _module().GoogleAccountsModel()
    preview = page.preview_plan(
        {
            "account_ref": "google-one",
            "plan_id": "plan-one",
            "expected_generation": 4,
            "expires_at": "2026-08-28T18:00:00Z",
            "step_count": 5,
            "projects": [
                {"project_name": "Amber Meadow", "key_name": "Quiet River"},
                {"project_name": "Velvet Orchard", "key_name": "Silver Fern"},
            ],
        }
    )

    assert preview.step_count == 5
    assert preview.names == (
        ("Amber Meadow", "Quiet River"),
        ("Velvet Orchard", "Silver Fern"),
    )


def test_apply_runs_only_after_visible_confirmation() -> None:
    module = _module()
    preview = module.GoogleAccountsModel().preview_plan(
        {
            "account_ref": "google-one",
            "plan_id": "plan-one",
            "expected_generation": 4,
            "expires_at": "2026-08-28T18:00:00Z",
            "step_count": 1,
            "projects": [{"project_name": "Amber Meadow", "key_name": "Quiet River"}],
        }
    )
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append((tuple(argv), stdin_data, callback))

    declined = module.GoogleActions(
        Runner(), executable="/opt/codex-usage", confirm=lambda _preview: False
    )
    assert declined.apply(preview) is False
    assert calls == []

    accepted = module.GoogleActions(
        Runner(), executable="/opt/codex-usage", confirm=lambda _preview: True
    )
    assert accepted.apply(preview) is True
    assert calls == [
        (
            (
                "/opt/codex-usage",
                "google",
                "provision-apply",
                "google-one",
                "plan-one",
                "--confirm",
                "--json",
            ),
            None,
            None,
        )
    ]


def test_oauth_filechooser_passes_only_path_to_private_cli_opening(tmp_path) -> None:
    source = tmp_path / "oauth-client.json"
    source.write_text('{"client_secret":"marker-secret"}', encoding="utf-8")
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append((tuple(argv), stdin_data, callback))

    actions = _module().GoogleActions(Runner(), executable="/opt/codex-usage")
    actions.import_oauth_client("google-one", source)

    assert calls == [
        (
            (
                "/opt/codex-usage",
                "google",
                "add",
                "google-one",
                "--oauth-client-json",
                str(source),
                "--json",
            ),
            None,
            None,
        )
    ]
    assert "marker-secret" not in repr(actions)
    assert "marker-secret" not in repr(calls)


def test_totp_is_transient_stdin_not_argv_env_model() -> None:
    marker = "739104"
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append((tuple(argv), bytes(stdin_data), callback))

    actions = _module().GoogleActions(Runner(), executable="/opt/codex-usage")
    actions.with_step_up(
        ["/opt/codex-usage", "google", "inventory-refresh", "google-one", "--json"],
        lambda: marker,
    )

    argv, stdin_data, _callback = calls[0]
    assert marker not in " ".join(argv)
    assert stdin_data == b"739104\n"
    assert marker not in repr(actions)


def test_google_actions_use_own_cli_and_never_masterjet_binary() -> None:
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append(tuple(argv))

    actions = _module().GoogleActions(Runner(), executable="/opt/codex-usage")
    actions.refresh_accounts()
    actions.oauth_begin("google-one", browser="firefox")
    actions.inventory_refresh("google-one")
    actions.provision_plan("google-one")

    assert all(argv[0] == "/opt/codex-usage" for argv in calls)
    assert all("codex-master-mcp" not in " ".join(argv) for argv in calls)


def test_stale_actions_allow_read_only_refresh_but_block_every_mutation(tmp_path) -> None:
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append(tuple(argv))

    module = _module()
    actions = module.GoogleActions(Runner(), executable="/opt/codex-usage")
    actions.set_stale(True)
    actions.refresh_accounts()

    with pytest.raises(RuntimeError, match="STALE"):
        actions.inventory_refresh("google-one")
    with pytest.raises(RuntimeError, match="STALE"):
        actions.oauth_begin("google-one", browser="firefox")
    with pytest.raises(RuntimeError, match="STALE"):
        actions.provision_plan("google-one")
    with pytest.raises(RuntimeError, match="STALE"):
        actions.import_oauth_client("google-one", tmp_path / "client.json")

    assert calls == [("/opt/codex-usage", "google", "accounts", "--json")]
