from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

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


def test_account_navigation_contains_openai_google_and_pool_authority() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    layout = schema["layout"]
    account_pages = [
        page for page in layout["pages"] if layout[page]["title"].startswith("Accounts")
    ]

    assert account_pages == [
        "openai-accounts-page",
        "google-accounts-page",
        "pool-authority-accounts-page",
    ]
    assert layout["openai-accounts-page"]["title"] == "Accounts · OpenAI"
    assert layout["google-accounts-page"]["title"] == "Accounts · Google"
    assert layout["pool-authority-accounts-page"]["title"] == "Accounts · Pool Authority"
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


@pytest.mark.parametrize(
    ("target", "field", "invalid"),
    [
        ("stale", "stale", 0),
        ("remote", "enabled", 1),
        ("local", "auth_sync_required", "false"),
        ("local", "series-active", 1),
    ],
)
def test_openai_projection_rejects_non_boolean_state(target, field, invalid) -> None:
    model = _module().OpenAIAccountsModel()
    local = _local_accounts()
    remote = _masterjet_accounts()
    stale = False
    if target == "stale":
        stale = invalid
    elif target == "remote":
        remote[0][field] = invalid
    else:
        local[0][field] = invalid

    with pytest.raises(ValueError, match=field):
        model.render(local, remote, stale=stale)


def _page_buttons(module, widget):
    result = []
    if isinstance(widget, module.Gtk.Button):
        result.append(widget)
    if isinstance(widget, module.Gtk.Container):
        for child in widget.get_children():
            result.extend(_page_buttons(module, child))
    return result


def test_fresh_page_revokes_real_buttons_before_refresh_and_on_transport_failure() -> None:
    module = _module()
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append(tuple(argv))

    page = module.OpenAIAccountsPage(None, None, None)
    page._actions = module.OpenAIActions(Runner(), executable="/opt/codex-usage")
    page.render(_local_accounts(), _masterjet_accounts(), stale=False)
    old_buttons = _page_buttons(module, page._body)
    assert old_buttons and any(button.get_sensitive() for button in old_buttons)

    page._refresh()

    assert page.model.stale is True
    assert page.model.rows == ()
    assert page._body.get_children() == []
    assert page._actions.projection_ready is False
    page._reauth(old_buttons[0], "BW_Work")
    page._sync_auth(old_buttons[-1], "BW_Work")
    assert calls == [
        ("/opt/codex-usage", "masterjet", "openai-accounts", "--json")
    ]

    page._loaded(module.CommandResult(False, {}, "control.transport_unavailable"))
    assert page.model.stale is True
    assert page._body.get_children() == []
    assert page._actions.projection_ready is False


@pytest.mark.parametrize("newest_ok", [False, True])
def test_openai_refresh_accepts_only_newest_out_of_order_result(newest_ok) -> None:
    module = _module()
    callbacks = []

    class Runner:
        def submit(self, _argv, *, stdin_data=None, callback=None):
            callbacks.append(callback)

    page = module.OpenAIAccountsPage(None, None, None)
    page._actions = module.OpenAIActions(Runner(), executable="/opt/codex-usage")
    page._refresh()
    page._refresh()
    success = module.CommandResult(
        True,
        {
            "local_accounts": _local_accounts(),
            "accounts": _masterjet_accounts(),
            "stale": False,
        },
        "ok",
    )
    failure = module.CommandResult(False, None, "control.transport_unavailable")

    callbacks[1](success if newest_ok else failure)
    callbacks[0](failure if newest_ok else success)

    assert page.model.stale is not newest_ok
    assert page._actions.projection_ready is newest_ok
    assert page._status.get_text() == (
        "Aktuell"
        if newest_ok
        else "STALE · control.transport_unavailable · Mutationen gesperrt"
    )


def test_openai_destroy_ignores_pending_result_and_step_up_and_closes_runners(
    monkeypatch,
) -> None:
    module = _module()
    callbacks = []
    challenges = []

    class Runner:
        closed = False

        def submit(self, _argv, **options):
            callbacks.append(options.get("callback"))
            challenges.append(options.get("challenge_callback"))

        def close(self):
            self.closed = True

    runner = Runner()
    page = module.OpenAIAccountsPage(None, None, None)
    page._runner = runner
    page._reauth_runner = runner
    page._actions = module.OpenAIActions(
        runner,
        reauth_runner=runner,
        executable="/opt/codex-usage",
    )
    page.render(_local_accounts(), _masterjet_accounts(), stale=False)
    prompted = []
    monkeypatch.setattr(
        page,
        "prompt_step_up",
        lambda: prompted.append(True) or bytearray(b"739104"),
    )
    page._sync_auth(None, "BW_Work")
    status = page._status.get_text()

    page._on_destroy()
    callbacks[-1](module.CommandResult(True, {}, "ok"))

    assert challenges[-1]() is None
    assert prompted == []
    assert page._status.get_text() == status
    assert runner.closed is True


@pytest.mark.parametrize("page_kind", ["openai", "google"])
def test_destroy_during_step_up_wipes_result_and_returns_no_code(monkeypatch, page_kind) -> None:
    openai_module = _module()
    if page_kind == "openai":
        page = openai_module.OpenAIAccountsPage(None, None, None)
        actions = openai_module.OpenAIActions(openai_module.BoundedJsonRunner())
    else:
        google_path = APPLET_DIR / "google_accounts_page.py"
        spec = importlib.util.spec_from_file_location("google_accounts_page", google_path)
        assert spec is not None and spec.loader is not None
        google_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = google_module
        spec.loader.exec_module(google_module)
        page = google_module.GoogleAccountsPage(None, None, None)
        actions = google_module.GoogleActions(openai_module.BoundedJsonRunner())
    page._actions = actions
    actions.set_projection_ready(True)
    epoch = page._begin_request()
    code = bytearray(b"739104")

    def destroy_during_prompt():
        page._on_destroy()
        return code

    monkeypatch.setattr(page, "prompt_step_up", destroy_during_prompt)

    assert page._prompt_running_step_up(epoch) is None
    assert code == b""


def test_invalid_live_envelope_revokes_fresh_page_and_action_guard() -> None:
    module = _module()
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append(tuple(argv))

    page = module.OpenAIAccountsPage(None, None, None)
    page._actions = module.OpenAIActions(Runner(), executable="/opt/codex-usage")
    page.render(_local_accounts(), _masterjet_accounts(), stale=False)
    assert page._actions.projection_ready is True

    page._loaded(module.CommandResult(True, {"accounts": []}, "ok"))
    page._reauth(None, "BW_Work")
    page._sync_auth(None, "BW_Work")

    assert page.model.stale is True
    assert page.model.rows == ()
    assert page._body.get_children() == []
    assert page._actions.projection_ready is False
    assert calls == []


def test_openai_actions_use_only_bounded_own_cli_commands() -> None:
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append((tuple(argv), stdin_data, callback))

    controller = _module().OpenAIActions(Runner(), executable="/opt/codex-usage")
    controller.set_projection_ready(True)
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
                    "--step-up-stdin",
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
    actions.set_projection_ready(True)

    actions.reauthenticate("BW_Work")
    actions.sync_auth("BW_Work")

    assert interactive_calls == [("/opt/codex-usage", "reactivate", "BW_Work", "--format", "json")]
    assert control_calls == [
        (
                "/opt/codex-usage",
                "--step-up-stdin",
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

    actions = _module().OpenAIActions(Runner(), executable="/opt/codex-usage")
    actions.set_projection_ready(True)
    actions.reauthenticate("work")
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
    deadline = time.monotonic() + 2
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pid_file.exists()
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


def test_bounded_runner_routes_control_through_systemd_credential_launcher(
    monkeypatch,
) -> None:
    module = _module()
    calls = []

    def launch(argv):
        calls.append(argv)
        return (
            sys.executable,
            "-c",
            "import json; print(json.dumps({'transport': 'systemd'}))",
        )

    monkeypatch.setattr(module, "settings_masterjet_launcher_argv", launch)
    command = (
        "/home/operator/.local/bin/codex-usage",
        "google",
        "accounts",
        "--json",
    )

    result = module.BoundedJsonRunner(dispatcher=lambda *args: args)._run(command, None)

    assert calls == [command]
    assert result == module.CommandResult(True, {"transport": "systemd"}, "")


def test_bounded_runner_wipes_copied_stdin_when_thread_start_fails(monkeypatch) -> None:
    module = _module()
    events = []
    copies = []
    real_bytearray = bytearray

    class ObservedBytearray(bytearray):
        def __setitem__(self, key, value):
            if isinstance(key, slice):
                events.append(("wipe", bytes(value)))
            return super().__setitem__(key, value)

        def clear(self):
            events.append(("clear", bytes(self)))
            return super().clear()

    def observed_copy(value=b""):
        copy = ObservedBytearray(value)
        copies.append(copy)
        return copy

    class FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(module, "bytearray", observed_copy, raising=False)
    monkeypatch.setattr(module.threading, "Thread", FailingThread)
    source = real_bytearray(b"739104\n")

    with pytest.raises(RuntimeError, match="thread unavailable"):
        module.BoundedJsonRunner().submit(["/opt/codex-usage", "once"], stdin_data=source)

    assert source == b"739104\n"
    assert len(copies) == 1
    assert copies[0] == b""
    assert events == [
        ("wipe", b"\x00" * len(source)),
        ("clear", b"\x00" * len(source)),
    ]


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
    assert actions.projection_ready is False


def _install_totp_dialog(module, monkeypatch, value: str):
    entries = []

    class Entry:
        def __init__(self):
            self.visible = True
            self.purpose = None
            self.text = value
            entries.append(self)

        def set_visibility(self, visible):
            self.visible = visible

        def set_input_purpose(self, purpose):
            self.purpose = purpose

        def get_text(self):
            return self.text

        def set_text(self, text):
            self.text = text

    class Dialog:
        def __init__(self, **_kwargs):
            self.destroyed = False

        def add_buttons(self, *_args):
            return None

        def get_content_area(self):
            return SimpleNamespace(pack_start=lambda *_args: None)

        def show_all(self):
            return None

        def run(self):
            return 1

        def destroy(self):
            self.destroyed = True

    monkeypatch.setattr(
        module,
        "Gtk",
        SimpleNamespace(
            Dialog=Dialog,
            Entry=Entry,
            Window=type("Window", (), {}),
            DialogFlags=SimpleNamespace(MODAL=1),
            ResponseType=SimpleNamespace(CANCEL=0, OK=1),
            STOCK_CANCEL="cancel",
            STOCK_OK="ok",
            InputPurpose=SimpleNamespace(DIGITS="digits"),
        ),
    )
    return entries


def test_openai_page_prompt_step_up_is_hidden_and_wipes_entry(monkeypatch) -> None:
    module = _module()
    page = module.OpenAIAccountsPage(None, None, None)
    entries = _install_totp_dialog(module, monkeypatch, "739104")

    code = page.prompt_step_up()
    try:
        assert code == b"739104"
        assert entries[0].visible is False
        assert entries[0].purpose == "digits"
        assert entries[0].text == ""
    finally:
        assert code is not None
        code[:] = b"\x00" * len(code)
        code.clear()


def test_openai_page_does_not_restart_a_failed_process() -> None:
    module = _module()

    class Actions:
        projection_ready = True

    page = module.OpenAIAccountsPage(None, None, None)
    page._actions = Actions()
    page._operation_finished(module.CommandResult(False, None, "control.step_up_required"))

    assert page._status.get_text() == "Fehler: control.step_up_required"


def test_bounded_runner_accepts_one_fragmented_step_up_sentinel() -> None:
    module = _module()
    prompts = []
    sentinel = "CODEX_USAGE_STEP_UP_REQUIRED\n"
    script = (
        "import sys; "
        f"sys.stderr.write({sentinel[:12]!r}); sys.stderr.flush(); "
        f"sys.stderr.write({sentinel[12:]!r}); sys.stderr.flush(); "
        "assert sys.stdin.buffer.readline() == b'739104\\n'; "
        "sys.stdout.write('{\"ok\":true}')"
    )
    runner = module.BoundedJsonRunner(prompt_dispatcher=lambda callback: callback())

    result = runner._run(
        (sys.executable, "-c", script),
        None,
        lambda: prompts.append("prompt") or "739104",
    )

    assert result.ok is True
    assert prompts == ["prompt"]


def test_bounded_runner_accepts_two_sequential_step_up_challenges() -> None:
    module = _module()
    prompts = []
    sentinel = "CODEX_USAGE_STEP_UP_REQUIRED\n"
    script = (
        "import sys; "
        f"sys.stderr.write({sentinel!r}); sys.stderr.flush(); "
        "assert sys.stdin.buffer.readline() == b'739104\\n'; "
        f"sys.stderr.write({sentinel!r}); sys.stderr.flush(); "
        "assert sys.stdin.buffer.readline() == b'182736\\n'; "
        "sys.stdout.write('{\"ok\":true}')"
    )
    runner = module.BoundedJsonRunner(prompt_dispatcher=lambda callback: callback())
    codes = iter(("739104", "182736"))

    result = runner._run(
        (sys.executable, "-c", script),
        None,
        lambda: prompts.append("prompt") or next(codes),
    )

    assert result.ok is True
    assert prompts == ["prompt", "prompt"]


def test_bounded_runner_accepts_fragmented_second_challenge() -> None:
    module = _module()
    prompts = []
    sentinel = "CODEX_USAGE_STEP_UP_REQUIRED\n"
    script = (
        "import sys; "
        f"sys.stderr.write({sentinel!r}); sys.stderr.flush(); "
        "assert sys.stdin.buffer.readline() == b'739104\\n'; "
        f"sys.stderr.write({sentinel[:11]!r}); sys.stderr.flush(); "
        f"sys.stderr.write({sentinel[11:]!r}); sys.stderr.flush(); "
        "assert sys.stdin.buffer.readline() == b'739104\\n'; "
        "sys.stdout.write('{\"ok\":true}')"
    )
    runner = module.BoundedJsonRunner(prompt_dispatcher=lambda callback: callback())

    result = runner._run(
        (sys.executable, "-c", script),
        None,
        lambda: prompts.append("prompt") or "739104",
    )

    assert result.ok is True
    assert prompts == ["prompt", "prompt"]


@pytest.mark.parametrize(
    "control",
    [
        b"xCODEX_USAGE_STEP_UP_REQUIRED\n",
        b"CODEX_USAGE_STEP_UP_REQUIRED\nx",
        b"x" * 59,
    ],
)
def test_bounded_runner_rejects_noncanonical_or_oversize_control_without_prompt(
    control,
) -> None:
    module = _module()
    prompts = []
    script = f"import sys; sys.stderr.buffer.write({control!r}); sys.stderr.flush()"
    runner = module.BoundedJsonRunner(prompt_dispatcher=lambda callback: callback())

    result = runner._run(
        (sys.executable, "-c", script),
        None,
        lambda: prompts.append("prompt") or "739104",
    )

    assert result.ok is False
    assert prompts == []


@pytest.mark.parametrize("page_kind", ["openai", "google"])
@pytest.mark.parametrize("restore_projection", [False, True])
def test_running_step_up_revoked_during_prompt_writes_no_code_or_effect(
    tmp_path, monkeypatch, page_kind, restore_projection
) -> None:
    openai_module = _module()
    if page_kind == "openai":
        page = openai_module.OpenAIAccountsPage(None, None, None)
        actions = openai_module.OpenAIActions(openai_module.BoundedJsonRunner())
    else:
        google_path = APPLET_DIR / "google_accounts_page.py"
        spec = importlib.util.spec_from_file_location("google_accounts_page", google_path)
        assert spec is not None and spec.loader is not None
        google_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = google_module
        spec.loader.exec_module(google_module)
        page = google_module.GoogleAccountsPage(None, None, None)
        actions = google_module.GoogleActions(openai_module.BoundedJsonRunner())
    page._actions = actions
    actions.set_projection_ready(True)
    marker = tmp_path / f"{page_kind}-{restore_projection}-step-up-write"
    pid_file = tmp_path / f"{page_kind}-{restore_projection}-step-up-pid"

    def revoke_during_prompt():
        actions.set_projection_ready(False)
        if restore_projection:
            actions.set_projection_ready(True)
        return "739104"

    monkeypatch.setattr(type(page), "prompt_step_up", lambda _self: revoke_during_prompt())
    sentinel = "CODEX_USAGE_STEP_UP_REQUIRED\n"
    script = (
        "import os,pathlib,sys; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        f"sys.stderr.write({sentinel!r}); sys.stderr.flush(); "
        "code=sys.stdin.buffer.readline(); "
        f"pathlib.Path({str(marker)!r}).write_bytes(code) if code else None; "
        "sys.stdout.write('{\"ok\":true}')"
    )
    runner = openai_module.BoundedJsonRunner(
        timeout_seconds=1,
        prompt_dispatcher=lambda callback: callback(),
    )

    result = runner._run(
        (sys.executable, "-c", script),
        None,
        page._prompt_running_step_up,
    )

    assert result.ok is False
    assert actions.projection_ready is restore_projection
    assert not marker.exists()
    assert not _process_running(int(pid_file.read_text(encoding="ascii")))


@pytest.mark.parametrize("emit_sentinel", [False, True])
def test_bounded_runner_handles_child_exit_before_or_after_sentinel_without_late_prompt(
    tmp_path, emit_sentinel
) -> None:
    module = _module()
    queued = []
    prompts = []
    pid_file = tmp_path / f"child-exit-{emit_sentinel}.pid"
    sentinel_write = (
        "sys.stderr.write('CODEX_USAGE_STEP_UP_REQUIRED\\n'); sys.stderr.flush();"
        if emit_sentinel
        else ""
    )
    script = (
        "import os,pathlib,sys; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        f"{sentinel_write} "
    )
    runner = module.BoundedJsonRunner(
        timeout_seconds=2,
        prompt_dispatcher=lambda callback: queued.append(callback),
    )
    started = time.monotonic()

    result = runner._run(
        (sys.executable, "-c", script),
        None,
        lambda: prompts.append("prompt") or "739104",
    )
    elapsed = time.monotonic() - started
    for callback in queued:
        callback()

    assert result.ok is False
    assert elapsed < 1
    assert prompts == []
    assert not _process_running(int(pid_file.read_text(encoding="ascii")))


def test_bounded_runner_prompt_timeout_cancels_late_prompt_and_leaks_no_worker_or_child(
    tmp_path,
) -> None:
    module = _module()
    completed = []
    done = threading.Event()
    queued = []
    prompts = []
    pid_file = tmp_path / "prompt-timeout.pid"
    marker = tmp_path / "prompt-timeout-write"
    sentinel = "CODEX_USAGE_STEP_UP_REQUIRED\n"
    script = (
        "import os,pathlib,sys,time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        f"sys.stderr.write({sentinel!r}); sys.stderr.flush(); "
        "code=sys.stdin.buffer.readline(); "
        f"pathlib.Path({str(marker)!r}).write_bytes(code) if code else None; "
        "time.sleep(30)"
    )
    runner = module.BoundedJsonRunner(
        timeout_seconds=0.2,
        dispatcher=lambda callback, result: (completed.append(result), done.set()),
        prompt_dispatcher=lambda callback: queued.append(callback),
    )

    runner.submit(
        (sys.executable, "-c", script),
        callback=lambda _result: None,
        challenge_callback=lambda: prompts.append("prompt") or "739104",
    )

    assert done.wait(2)
    assert completed == [module.CommandResult(False, None, "control.transport_unavailable")]
    assert len(queued) == 1
    queued[0]()
    assert prompts == []
    assert not marker.exists()
    assert not _process_running(int(pid_file.read_text(encoding="ascii")))
    deadline = time.monotonic() + 1
    while any(thread.name == "codex-usage-control" for thread in threading.enumerate()):
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_bounded_runner_wipes_code_returned_after_prompt_timeout(tmp_path) -> None:
    module = _module()
    completed = threading.Event()
    prompt_started = threading.Event()
    release_prompt = threading.Event()
    prompt_threads = []
    code = bytearray(b"739104")
    pid_file = tmp_path / "running-prompt-timeout.pid"
    sentinel = "CODEX_USAGE_STEP_UP_REQUIRED\n"
    script = (
        "import os,pathlib,sys,time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        f"sys.stderr.write({sentinel!r}); sys.stderr.flush(); "
        "sys.stdin.buffer.readline(); time.sleep(30)"
    )

    def dispatch_prompt(callback):
        thread = threading.Thread(target=callback, name="test-gtk-step-up")
        prompt_threads.append(thread)
        thread.start()

    def provide_code():
        prompt_started.set()
        assert release_prompt.wait(2)
        return code

    runner = module.BoundedJsonRunner(
        timeout_seconds=0.2,
        dispatcher=lambda _callback, _result: completed.set(),
        prompt_dispatcher=dispatch_prompt,
    )
    runner.submit(
        (sys.executable, "-c", script),
        callback=lambda _result: None,
        challenge_callback=provide_code,
    )

    assert prompt_started.wait(1)
    assert completed.wait(2)
    release_prompt.set()
    prompt_threads[0].join(timeout=1)

    assert not prompt_threads[0].is_alive()
    assert code == b""
    assert not _process_running(int(pid_file.read_text(encoding="ascii")))


def test_bounded_runner_wipes_callback_bytearray_after_stdin_write() -> None:
    module = _module()
    code = bytearray(b"739104")
    sentinel = "CODEX_USAGE_STEP_UP_REQUIRED\n"
    script = (
        "import sys; "
        f"sys.stderr.write({sentinel!r}); sys.stderr.flush(); "
        "assert sys.stdin.buffer.readline() == b'739104\\n'; "
        "sys.stdout.write('{\"ok\":true}')"
    )
    runner = module.BoundedJsonRunner(prompt_dispatcher=lambda callback: callback())

    result = runner._run((sys.executable, "-c", script), None, lambda: code)

    assert result.ok is True
    assert code == b""


def test_bounded_runner_close_terminates_active_child_group(tmp_path) -> None:
    module = _module()
    pid_file = tmp_path / "closed-runner.pid"
    script = (
        "import os,pathlib,time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    runner = module.BoundedJsonRunner(timeout_seconds=60)
    runner.submit((sys.executable, "-c", script))
    deadline = time.monotonic() + 2
    while not pid_file.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)

    runner.close()

    pid = int(pid_file.read_text(encoding="ascii"))
    deadline = time.monotonic() + 2
    while _process_running(pid):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    with pytest.raises(RuntimeError, match="closed"):
        runner.submit((sys.executable, "-c", "pass"))


def test_bounded_runner_closes_all_child_pipes(monkeypatch) -> None:
    module = _module()
    processes = []
    real_popen = module.subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", recording_popen)
    sentinel = "CODEX_USAGE_STEP_UP_REQUIRED\n"
    script = (
        "import sys; "
        f"sys.stderr.write({sentinel!r}); sys.stderr.flush(); "
        "assert sys.stdin.buffer.readline() == b'739104\\n'; "
        "sys.stdout.write('{\"ok\":true}')"
    )

    result = module.BoundedJsonRunner(
        prompt_dispatcher=lambda callback: callback()
    )._run((sys.executable, "-c", script), None, lambda: "739104")

    assert result.ok is True
    assert len(processes) == 1
    assert processes[0].stdin is not None and processes[0].stdin.closed
    assert processes[0].stdout is not None and processes[0].stdout.closed
    assert processes[0].stderr is not None and processes[0].stderr.closed


def test_openai_action_guard_checks_current_projection_before_mutation_argv() -> None:
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append(tuple(argv))

    module = _module()
    actions = module.OpenAIActions(Runner(), executable="/opt/codex-usage")
    with pytest.raises(RuntimeError, match="STALE"):
        actions.reauthenticate("BW_Work")
    with pytest.raises(RuntimeError, match="STALE"):
        actions.sync_auth("BW_Work")

    actions.set_projection_ready(True)
    actions.refresh()
    with pytest.raises(RuntimeError, match="STALE"):
        actions.reauthenticate("BW_Work")

    assert calls == [
        ("/opt/codex-usage", "masterjet", "openai-accounts", "--json")
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
