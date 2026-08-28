from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import time
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
    completed = object()
    controller.reauthenticate("BW_Work", callback=completed)
    controller.sync_auth("BW_Work", callback=completed)

    assert calls == [
        (
            (
                "/opt/codex-usage",
                "reactivate",
                "BW_Work",
                "--format",
                "json",
            ),
            None,
            completed,
        ),
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
            completed,
        ),
    ]


def test_openai_actions_keep_interactive_and_control_runners_separate() -> None:
    control_calls = []
    interactive_calls = []

    class Runner:
        def __init__(self, calls):
            self.calls = calls

        def submit(self, argv, *, stdin_data=None, callback=None):
            self.calls.append(tuple(argv))

    actions = _module().OpenAIActions(
        Runner(control_calls),
        reauth_runner=Runner(interactive_calls),
        executable="/opt/codex-usage",
    )

    actions.reauthenticate("BW_Work")
    actions.sync_auth("BW_Work")

    assert interactive_calls == [("/opt/codex-usage", "reactivate", "BW_Work", "--format", "json")]
    assert control_calls == [
        (
            "/opt/codex-usage",
            "account",
            "auth-sync",
            "BW_Work",
            "--format",
            "json",
        )
    ]


def test_reauthentication_has_bounded_interactive_timeout() -> None:
    assert _module().REAUTH_TIMEOUT_SECONDS == 15 * 60


def test_reauthentication_command_uses_real_json_cli_contract(
    tmp_path, monkeypatch, capsys
) -> None:
    from codex_usage import cli as cli_module
    from codex_usage.config import Account, AppConfig, save_config

    commands = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            commands.append(tuple(argv))

    _module().OpenAIActions(Runner(), executable="/opt/codex-usage").reauthenticate("work")
    config_path = tmp_path / "config.toml"
    save_config(
        AppConfig(
            accounts=(
                Account(
                    id="work",
                    label="Work",
                    profile_dir=str(tmp_path / "profile"),
                    auth_json_path=str(tmp_path / "profile" / "auth.json"),
                ),
            )
        ),
        config_path,
    )
    monkeypatch.setattr(
        cli_module,
        "reactivate_account",
        lambda selected, browser: {
            "ok": True,
            "account": selected.id,
            "browser": browser,
        },
    )

    assert cli_module.main(["--config", str(config_path), *commands[0][1:]]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["account"] == "work"
    assert payload["auth_sync_required"] is True


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


def test_masterjet_connection_cannot_create_second_settings_authority() -> None:
    writes = []

    with pytest.raises(RuntimeError, match="kanonische Codex-Usage-Konfiguration"):
        _module().save_masterjet_connection(
            lambda key, value: writes.append((key, value)),
            "local",
            "/run/user/4242/masterjet.sock",
        )

    assert writes == []


def test_masterjet_status_tests_only_canonical_codex_usage_config() -> None:
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append((tuple(argv), stdin_data, callback))

    completed = object()
    actions = _module().MasterjetConnectionActions(Runner(), executable="/opt/codex-usage")
    actions.test(callback=completed)

    assert calls == [
        (
            ("/opt/codex-usage", "masterjet", "connection-test", "--json"),
            None,
            completed,
        )
    ]


def test_masterjet_socket_default_uses_current_runtime_uid() -> None:
    module = _module()

    assert (
        module.default_masterjet_socket(environ={"XDG_RUNTIME_DIR": "/run/user/4242"}, uid=4242)
        == "/run/user/4242/masterjet.sock"
    )
    assert module.default_masterjet_socket(environ={}, uid=4242) == (
        "/run/user/4242/masterjet.sock"
    )

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["masterjet-connection"]["default"] == ""
    assert "/run/user/1000/masterjet.sock" not in SCHEMA.read_text(encoding="utf-8")


def _process_running(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return False
    return status.split()[2] != "Z"


@pytest.mark.parametrize(
    ("mode", "expected_ok"),
    [
        ("success", True),
        ("timeout", False),
        ("decode", False),
        ("output", False),
        ("error", False),
    ],
)
def test_bounded_runner_terminates_group_on_every_exit_path(
    tmp_path, mode: str, expected_ok: bool
) -> None:
    module = _module()
    pid_file = tmp_path / f"{mode}.pid"
    helper = """
import json
import os
import signal
import sys
import time

mode, pid_file = sys.argv[1:]
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
    os._exit(0)
with open(pid_file, "w", encoding="ascii") as stream:
    stream.write(str(child))
if mode == "success":
    print(json.dumps({"status": "ok"}), flush=True)
elif mode == "decode":
    print("not-json", flush=True)
elif mode == "output":
    print("x" * 1024, flush=True)
elif mode == "error":
    print(json.dumps({"code": "control.failed"}), flush=True)
    raise SystemExit(2)
else:
    time.sleep(30)
"""
    result = module.BoundedJsonRunner(
        timeout_seconds=0.3,
        max_output_bytes=128,
        dispatcher=lambda *args: args,
    )._run((sys.executable, "-c", helper, mode, str(pid_file)), None)
    child_pid = int(pid_file.read_text(encoding="ascii"))

    try:
        assert result.ok is expected_ok
        deadline = time.monotonic() + 2
        while _process_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _process_running(child_pid)
    finally:
        if _process_running(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_bounded_runner_passes_only_audited_gui_session_environment(monkeypatch) -> None:
    module = _module()
    allowed = {
        "DISPLAY": ":88",
        "WAYLAND_DISPLAY": "wayland-7",
        "XAUTHORITY": "/run/user/4242/Xauthority",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/4242/bus",
        "XDG_RUNTIME_DIR": "/run/user/4242",
        "LANG": "de_DE.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    blocked = {
        "OPENAI_API_KEY": "private-api-marker",
        "MASTERJET_BEARER": "private-bearer-marker",
        "TOTP_CODE": "739104",
        "UNRELATED_SETTING": "private-unrelated-marker",
    }
    for name, value in {**allowed, **blocked}.items():
        monkeypatch.setenv(name, value)
    names = [*allowed, *blocked]
    helper = (
        "import json,os,sys; "
        "print(json.dumps({name: os.environ.get(name) for name in sys.argv[1:]}))"
    )

    result = module.BoundedJsonRunner(dispatcher=lambda *args: args)._run(
        (sys.executable, "-c", helper, *names), None
    )

    assert result.ok is True
    assert result.payload == {**allowed, **dict.fromkeys(blocked)}


def test_masterjet_schema_has_no_bearer_or_totp_settings() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    assert "masterjet-connection" in schema
    assert not any("bearer" in key.casefold() or "totp" in key.casefold() for key in schema)


def test_openai_live_projection_refresh_uses_complete_bounded_command() -> None:
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append((tuple(argv), stdin_data, callback))

    callback = object()
    actions = _module().OpenAIActions(Runner(), executable="/opt/codex-usage")
    actions.refresh(callback=callback)

    assert calls == [
        (("/opt/codex-usage", "masterjet", "openai-accounts", "--json"), None, callback)
    ]


def test_live_projection_connection_actions_use_show_test_set_cli_contracts() -> None:
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append((tuple(argv), stdin_data, callback))

    actions = _module().MasterjetConnectionActions(
        Runner(), executable="/opt/codex-usage"
    )
    actions.show()
    actions.test()
    actions.set("https", "https://masterjet.example.test/control", 7)

    assert [call[0] for call in calls] == [
        ("/opt/codex-usage", "masterjet", "connection-show", "--json"),
        ("/opt/codex-usage", "masterjet", "connection-test", "--json"),
        (
            "/opt/codex-usage", "masterjet", "connection-set", "--transport", "https",
            "--endpoint", "https://masterjet.example.test/control",
            "--timeout-seconds", "7", "--json",
        ),
    ]
