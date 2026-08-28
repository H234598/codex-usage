from __future__ import annotations

import importlib.util
import json
import queue
import shutil
import site
import socket
import ssl
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_usage.config import AppConfig, MasterjetConnection, save_config
from codex_usage.models import Account

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


def _openai_payload():
    return {
        "schema_version": 1,
        "accounts": [
            {
                "ref": "openai-remote",
                "label": "OpenAI One",
                "enabled": True,
                "local_profile_ref": "openai-one",
                "source_host_ref": "host-one",
                "auth_state": "ready",
                "access_expires_at": None,
                "credential_generation": 4,
                "vault_projection_state": "current",
                "usage_state": "fresh",
            }
        ],
    }


@contextmanager
def _task9_unix_control_server(socket_path: Path):
    ready = threading.Event()
    stopped = threading.Event()
    finished = threading.Event()
    requests = []

    def response_for(request):
        operation = request["operation"]
        if operation == "openai.accounts.list":
            return _openai_payload()
        if operation == "google.accounts.list":
            payload = _payload()
            return {"schema_version": 1, "accounts": payload["accounts"]}
        if operation == "google.projects.list":
            payload = _payload()
            return {
                "schema_version": 1,
                "account_ref": "google-one",
                "inventory_generation": 4,
                "projects": payload["projects"]["google-one"],
            }
        raise AssertionError(f"unexpected operation: {operation}")

    def serve():
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(socket_path))
            socket_path.chmod(0o600)
            server.listen(4)
            ready.set()
            while not stopped.is_set():
                connection, _ = server.accept()
                with connection:
                    if stopped.is_set():
                        break
                    raw = bytearray()
                    while b"\n" not in raw:
                        chunk = connection.recv(65_536)
                        if not chunk:
                            break
                        raw.extend(chunk)
                    request = json.loads(raw.split(b"\n", 1)[0])
                    requests.append(request)
                    response = json.dumps(response_for(request), separators=(",", ":")).encode()
                    connection.sendall(response + b"\n")
        finally:
            server.close()
            finished.set()

    thread = threading.Thread(target=serve, name="task9-unix-control", daemon=True)
    thread.start()
    assert ready.wait(2)

    def stop():
        stopped.set()
        wake = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            wake.connect(str(socket_path))
        finally:
            wake.close()
        assert finished.wait(2)

    try:
        yield requests, stop
    finally:
        if not stopped.is_set():
            stop()
        thread.join(timeout=0)


@contextmanager
def _task9_https_control_server(
    tmp_path: Path, *, openai_challenge_operation: str | None = None
):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl unavailable for stdlib TLS fixture")
    certificate = tmp_path / "runner-server.crt"
    private_key = tmp_path / "runner-server.key"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.settimeout(0.1)
    port = listener.getsockname()[1]
    stopped = threading.Event()
    finished = threading.Event()
    requests = []
    authenticated = {"google": False, "openai": False}
    challenged_operations = set()
    now = datetime.now(UTC)

    def timestamp(offset: timedelta) -> str:
        return (now + offset).isoformat().replace("+00:00", "Z")

    operation_base = {
        "schema_version": 1,
        "expected_generation": 4,
        "plan_digest": "sha256:" + "a" * 64,
        "created_at": timestamp(timedelta(minutes=-1)),
        "expires_at": timestamp(timedelta(minutes=30)),
        "failed_count": 0,
        "reason_codes": [],
    }
    challenge = {
        "schema_version": 1,
        "code": "control.step_up_required",
        "severity": "warning",
        "title": "Step-up required",
        "detail": "Additional authentication is required.",
        "effect": "Operation is paused.",
        "action": "Complete step-up authentication.",
        "retryable": False,
        "retry_after_seconds": None,
        "correlation_id": "runner-correlation",
        "occurred_at": timestamp(timedelta()),
    }

    def response_for(request):
        if request["method"] == "PUT":
            operation = "secret.ingress.put"
        else:
            operation = json.loads(request["body"])["operation"]
        request["operation"] = operation
        family = "google" if operation.startswith("google.") else "openai"
        if operation == openai_challenge_operation and operation not in challenged_operations:
            challenged_operations.add(operation)
            return challenge
        if family == "openai" and openai_challenge_operation is not None:
            authenticated[family] = True
        if operation in {"google.accounts.list", "openai.accounts.list"} and not authenticated[
            family
        ]:
            step_up = request["headers"].get("X-Masterjet-Step-Up")
            if step_up == "739104":
                authenticated[family] = True
            else:
                return challenge
        if operation == "google.accounts.list":
            return {"schema_version": 1, "accounts": _payload()["accounts"]}
        if operation == "google.inventory.refresh":
            return operation_base | {
                "id": "inventory-1",
                "kind": operation,
                "state": "succeeded",
                "resulting_generation": 5,
                "completed_count": 1,
                "not_attempted_count": 0,
            }
        if operation == "openai.accounts.list":
            return _openai_payload()
        if operation == "openai.auth-sync.plan":
            return operation_base | {
                "id": "plan-1",
                "kind": operation,
                "state": "planned",
                "resulting_generation": None,
                "completed_count": 0,
                "not_attempted_count": 1,
            }
        if operation == "secret.ingress.create":
            return {
                "schema_version": 1,
                "id": "ingress-1",
                "account_ref": "openai-remote",
                "plan_id": "plan-1",
                "expires_at": timestamp(timedelta(minutes=15)),
                "expected_generation": 4,
            }
        if operation == "secret.ingress.put":
            return {
                "schema_version": 1,
                "session_id": "ingress-1",
                "account_ref": "openai-remote",
                "state": "consumed",
                "generation": 5,
            }
        if operation == "openai.auth-sync.apply":
            return operation_base | {
                "id": "apply-1",
                "kind": operation,
                "state": "succeeded",
                "resulting_generation": 5,
                "completed_count": 1,
                "not_attempted_count": 0,
            }
        raise AssertionError(f"unexpected operation: {operation}")

    def serve() -> None:
        try:
            while not stopped.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                with connection:
                    with context.wrap_socket(connection, server_side=True) as tls_socket:
                        raw = bytearray()
                        while b"\r\n\r\n" not in raw:
                            raw.extend(tls_socket.recv(4096))
                        head, body = raw.split(b"\r\n\r\n", 1)
                        lines = head.decode("ascii").split("\r\n")
                        headers = dict(line.split(": ", 1) for line in lines[1:])
                        length = int(headers["Content-Length"])
                        while len(body) < length:
                            body.extend(tls_socket.recv(length - len(body)))
                        request = {
                            "method": lines[0].split(" ", 1)[0],
                            "target": lines[0].split(" ")[1],
                            "headers": headers,
                            "body": bytes(body),
                        }
                        response = json.dumps(response_for(request), separators=(",", ":")).encode()
                        requests.append(request)
                        tls_socket.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                            + f"Content-Length: {len(response)}\r\n".encode()
                            + b"Connection: close\r\n\r\n"
                            + response
                        )
        finally:
            listener.close()
            finished.set()

    thread = threading.Thread(target=serve, name="task9-https-control", daemon=True)
    thread.start()
    try:
        yield port, certificate, requests
    finally:
        stopped.set()
        assert finished.wait(2)
        thread.join(timeout=0)


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
    assert model.stale is True
    assert model.card("google-one").projects == ()
    assert model.card("google-one").plan_enabled is False


def test_google_model_starts_unknown_and_fail_closed() -> None:
    model = _module().GoogleAccountsModel()

    assert model.stale is True
    assert model.details_available is False
    assert model.cards == ()

    model.render(_payload())
    model.fail_closed()

    assert model.stale is True
    assert model.details_available is False
    assert model.cards == ()


@pytest.mark.parametrize(
    "invalid_stale",
    [None, 0, 1, "false", {}, []],
    ids=["none", "zero", "one", "string", "mapping", "list"],
)
def test_non_boolean_stale_projection_is_rejected_fail_closed(invalid_stale) -> None:
    module = _module()
    payload = _payload()
    payload["stale"] = invalid_stale
    model = module.GoogleAccountsModel()
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append(tuple(argv))

    actions = module.GoogleActions(Runner())

    with pytest.raises(ValueError, match="stale"):
        model.render(payload)
    with pytest.raises(RuntimeError, match="STALE"):
        actions.provision_plan("google-one")

    assert model.stale is True
    assert model.cards == ()
    assert calls == []


def test_missing_stale_projection_is_rejected_fail_closed() -> None:
    payload = _payload()
    del payload["stale"]
    model = _module().GoogleAccountsModel()

    with pytest.raises(ValueError):
        model.render(payload)

    assert model.stale is True
    assert model.cards == ()


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [("enabled", 1), ("subject_bound", "true")],
)
def test_non_boolean_account_projection_is_rejected(field, invalid_value) -> None:
    payload = _payload()
    payload["accounts"][0][field] = invalid_value
    model = _module().GoogleAccountsModel()

    with pytest.raises(ValueError, match=field):
        model.render(payload)

    assert model.stale is True
    assert model.cards == ()


@pytest.mark.parametrize(
    "mutation",
    ["missing-owner", "zero-missing", "too-few", "too-many"],
)
def test_incomplete_project_projection_is_rejected(mutation) -> None:
    payload = _payload()
    if mutation == "missing-owner":
        payload["projects"] = {}
    elif mutation == "zero-missing":
        payload["accounts"][0]["project_count"] = 0
        payload["projects"] = {}
    elif mutation == "too-few":
        payload["projects"]["google-one"].pop()
    else:
        payload["accounts"][0]["project_count"] = 1
    model = _module().GoogleAccountsModel()

    with pytest.raises(ValueError, match="project"):
        model.render(payload)

    assert model.stale is True
    assert model.details_available is False
    assert model.cards == ()


def test_zero_project_count_requires_explicit_empty_projection() -> None:
    payload = _payload()
    payload["accounts"][0]["project_count"] = 0
    payload["projects"]["google-one"] = []
    model = _module().GoogleAccountsModel()

    model.render(payload)

    card = model.card("google-one")
    assert card.projects == ()
    assert card.project_count == 0
    assert card.plan_enabled is True


def test_duplicate_and_foreign_project_refs_are_rejected() -> None:
    duplicate = _payload()
    duplicate["projects"]["google-one"][1]["ref"] = "hive-one"
    foreign = _payload()
    foreign["projects"]["google-foreign"] = []
    module = _module()

    with pytest.raises(ValueError, match="duplicate Google project"):
        module.GoogleAccountsModel().render(duplicate)
    with pytest.raises(ValueError, match="project owner"):
        module.GoogleAccountsModel().render(foreign)


def test_incomplete_projection_revokes_all_mutations_without_argv(tmp_path) -> None:
    module = _module()
    model = module.GoogleAccountsModel()
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append(tuple(argv))

    actions = module.GoogleActions(Runner())
    model.render(_payload())
    actions.set_projection_ready(True)
    preview = model.preview_plan(
        {
            "account_ref": "google-one",
            "plan_id": "plan-one",
            "expected_generation": 4,
            "plan_digest": "sha256:" + "a" * 64,
            "expires_at": "2026-08-28T18:00:00Z",
            "step_count": 1,
            "projects": [{"project_name": "Amber Meadow", "key_name": "Quiet River"}],
        }
    )
    invalid = _payload()
    invalid["projects"] = {}

    with pytest.raises(ValueError, match="project"):
        model.render(invalid)
    model.fail_closed()
    actions.set_projection_ready(False)

    operations = (
        lambda: actions.import_oauth_client("google-one", tmp_path / "oauth.json"),
        lambda: actions.oauth_begin("google-one", browser="firefox"),
        lambda: actions.inventory_refresh("google-one"),
        lambda: actions.provision_plan("google-one"),
        lambda: actions.apply(preview),
    )
    for operation in operations:
        with pytest.raises(RuntimeError, match="STALE"):
            operation()

    assert model.stale is True
    assert model.cards == ()
    assert calls == []


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
            "plan_digest": "sha256:" + "a" * 64,
            "expires_at": "2026-08-28T18:00:00Z",
            "step_count": 5,
            "projects": [
                {"project_name": "Amber Meadow", "key_name": "Quiet River"},
                {"project_name": "Velvet Orchard", "key_name": "Silver Fern"},
            ],
        }
    )

    assert preview.step_count == 5
    assert preview.plan_digest == "sha256:" + "a" * 64
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
            "plan_digest": "sha256:" + "a" * 64,
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
    declined.set_projection_ready(True)
    assert declined.apply(preview) is False
    assert calls == []

    accepted = module.GoogleActions(
        Runner(), executable="/opt/codex-usage", confirm=lambda _preview: True
    )
    accepted.set_projection_ready(True)
    assert accepted.apply(preview) is True
    assert calls == [
        (
            (
                "/opt/codex-usage",
                "google",
                "provision-apply",
                "google-one",
                "plan-one",
                "--plan-digest",
                "sha256:" + "a" * 64,
                "--confirm",
                "--json",
            ),
            None,
            None,
        )
    ]


def test_apply_stale_race_is_rejected_after_confirmation() -> None:
    module = _module()
    preview = module.GoogleAccountsModel().preview_plan(
        {
            "account_ref": "google-one",
            "plan_id": "plan-one",
            "expected_generation": 4,
            "plan_digest": "sha256:" + "a" * 64,
            "expires_at": "2026-08-28T18:00:00Z",
            "step_count": 1,
            "projects": [{"project_name": "Amber Meadow", "key_name": "Quiet River"}],
        }
    )
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append(tuple(argv))

    actions = None

    def confirm(_preview):
        actions.set_projection_ready(False)
        return True

    actions = module.GoogleActions(Runner(), confirm=confirm)
    actions.set_projection_ready(True)

    assert actions.apply(preview) is False
    assert calls == []


@pytest.mark.parametrize(
    "payload_change",
    [
        lambda payload: payload.pop("plan_digest"),
        lambda payload: payload.__setitem__("plan_digest", "sha256:not-a-digest"),
    ],
    ids=["missing", "invalid"],
)
def test_plan_preview_requires_valid_digest(payload_change) -> None:
    payload = {
        "account_ref": "google-one",
        "plan_id": "plan-one",
        "expected_generation": 4,
        "plan_digest": "sha256:" + "a" * 64,
        "expires_at": "2026-08-28T18:00:00Z",
        "step_count": 1,
        "projects": [{"project_name": "Amber Meadow", "key_name": "Quiet River"}],
    }
    payload_change(payload)

    with pytest.raises(ValueError, match=r"digest|incomplete|Google plan fields"):
        _module().GoogleAccountsModel().preview_plan(payload)


def test_oauth_filechooser_passes_only_path_to_private_cli_opening(tmp_path) -> None:
    source = tmp_path / "oauth-client.json"
    source.write_text('{"client_secret":"marker-secret"}', encoding="utf-8")
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append((tuple(argv), stdin_data, callback))

    actions = _module().GoogleActions(Runner(), executable="/opt/codex-usage")
    actions.set_projection_ready(True)
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
    actions.set_projection_ready(True)
    actions.with_step_up(
        ["/opt/codex-usage", "google", "inventory-refresh", "google-one", "--json"],
        lambda: marker,
    )

    argv, stdin_data, _callback = calls[0]
    assert "--step-up-stdin" in argv
    assert marker not in " ".join(argv)
    assert stdin_data == b"739104\n"
    assert marker not in repr(actions)


def test_google_step_up_checks_stale_before_prompting_or_submitting() -> None:
    module = _module()
    calls = []
    prompted = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append((tuple(argv), stdin_data, callback))

    actions = module.GoogleActions(Runner(), executable="/opt/codex-usage")

    with pytest.raises(RuntimeError, match="STALE"):
        actions.with_step_up(
            ["/opt/codex-usage", "google", "inventory-refresh", "google-one", "--json"],
            lambda: prompted.append(True) or "739104",
        )

    assert prompted == []
    assert calls == []


def test_google_page_binds_every_sensitive_action_to_its_original_argv(monkeypatch) -> None:
    module = _module()
    page = module.GoogleAccountsPage(None, None, None)
    pending = {}
    retries = []

    class Entry:
        def set_visibility(self, _visible):
            return None

        def set_input_purpose(self, _purpose):
            return None

        def get_text(self):
            return "739104"

        def set_text(self, _text):
            return None

    class Dialog:
        def __init__(self, **_kwargs):
            pass

        def add_buttons(self, *_args):
            return None

        def get_content_area(self):
            return type("Area", (), {"pack_start": lambda *_args: None})()

        def show_all(self):
            return None

        def run(self):
            return 1

        def destroy(self):
            return None

    class FileChooserDialog(Dialog):
        def get_filename(self):
            return "/tmp/oauth-client.json"

    monkeypatch.setattr(
        module,
        "Gtk",
        type(
            "FakeGtk",
            (),
            {
                "Dialog": Dialog,
                "Entry": Entry,
                "FileChooserDialog": FileChooserDialog,
                "Window": type("Window", (), {}),
                "DialogFlags": type("Flags", (), {"MODAL": 1}),
                "ResponseType": type("Response", (), {"CANCEL": 0, "OK": 1}),
                "FileChooserAction": type("Action", (), {"OPEN": 1}),
                "InputPurpose": type("Purpose", (), {"DIGITS": 1}),
                "STOCK_CANCEL": "cancel",
                "STOCK_OK": "ok",
                "STOCK_OPEN": "open",
            },
        ),
    )

    class Actions:
        _executable = "/opt/codex-usage"
        projection_ready = True

        def oauth_begin(self, _account, *, browser, callback):
            assert browser == "firefox"
            pending["oauth"] = callback

        def import_oauth_client(self, _account, _source, *, callback):
            pending["import"] = callback

        def inventory_refresh(self, _account, *, callback):
            pending["inventory"] = callback

        def provision_plan(self, _account, *, callback):
            pending["plan"] = callback

        def apply(self, _preview, *, callback):
            pending["apply"] = callback
            return True

        def with_step_up(self, argv, provider, *, callback=None):
            retries.append((tuple(argv), provider(), callback))

    page._actions = Actions()
    challenge = module.CommandResult(False, None, "control.step_up_required", True)
    page._oauth_begin(None, "google-one")
    pending["oauth"](challenge)
    page.choose_oauth_client("google-one")
    pending["import"](challenge)
    page._inventory(None, "google-one")
    pending["inventory"](challenge)
    page._plan(None, "google-one")
    pending["plan"](challenge)
    page._plan_loaded(
        module.CommandResult(
            True,
            {
                "account_ref": "google-one",
                "plan_id": "plan-one",
                "expected_generation": 4,
                "plan_digest": "sha256:" + "a" * 64,
                "expires_at": "2026-08-28T18:00:00Z",
                "step_count": 1,
                "projects": [{"project_name": "Amber Meadow", "key_name": "Quiet River"}],
            },
            "",
        )
    )
    pending["apply"](challenge)

    assert [item[0] for item in retries] == [
        (
            "/opt/codex-usage",
            "google",
            "oauth-begin",
            "google-one",
            "--browser",
            "firefox",
            "--json",
        ),
        (
            "/opt/codex-usage",
            "google",
            "add",
            "google-one",
            "--oauth-client-json",
            "/tmp/oauth-client.json",
            "--json",
        ),
        ("/opt/codex-usage", "google", "inventory-refresh", "google-one", "--json"),
        ("/opt/codex-usage", "google", "provision-plan", "google-one", "--json"),
        (
            "/opt/codex-usage",
            "google",
            "provision-apply",
            "google-one",
            "plan-one",
            "--plan-digest",
            "sha256:" + "a" * 64,
            "--confirm",
            "--json",
        ),
    ]
    for _argv, _code, callback in tuple(retries):
        callback(challenge)
    assert len(retries) == 5


def test_google_actions_use_own_cli_and_never_masterjet_binary() -> None:
    calls = []

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            calls.append(tuple(argv))

    actions = _module().GoogleActions(Runner(), executable="/opt/codex-usage")
    actions.refresh_accounts()
    actions.set_projection_ready(True)
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


def test_live_projection_failure_revokes_previous_google_mutations() -> None:
    module = _module()
    model = module.GoogleAccountsModel()
    model.render(_payload())

    class Runner:
        def submit(self, argv, *, stdin_data=None, callback=None):
            raise AssertionError("mutation escaped revoked projection")

    actions = module.GoogleActions(Runner())
    actions.set_projection_ready(True)
    model.fail_closed()
    actions.set_projection_ready(False)

    with pytest.raises(RuntimeError, match="STALE"):
        actions.provision_plan("google-one")
    assert model.cards == ()


def test_https_page_late_auth_sync_challenge_fails_closed_without_replaying_stages(
    tmp_path, monkeypatch
) -> None:
    google_module = _module()
    openai_module = sys.modules["openai_accounts_page"]
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    profile = home / "profile"
    auth_directory = profile / "codex-home"
    auth_directory.mkdir(parents=True, mode=0o700)
    raw_auth = b'{"tokens":"runner-raw-auth-marker"}'
    auth_path = auth_directory / "auth.json"
    auth_path.write_bytes(raw_auth)
    auth_path.chmod(0o600)
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir(mode=0o700)
    bearer = "runner-system-bearer-marker"
    credential = credential_directory / "masterjet-control-bearer"
    credential.write_text(bearer, encoding="ascii")
    credential.chmod(0o400)
    diagnostics = tmp_path / "runner-diagnostics.jsonl"
    completed = queue.Queue()
    submissions = []

    with _task9_https_control_server(
        tmp_path, openai_challenge_operation="openai.auth-sync.apply"
    ) as (port, certificate, requests):
        config_path = home / ".config" / "codex-usage" / "config.toml"
        save_config(
            AppConfig(
                accounts=(
                    Account(
                        id="openai-one",
                        label="OpenAI One",
                        profile_dir=str(profile),
                        auth_json_path=str(auth_path),
                        auth_sync_required=True,
                    ),
                ),
                masterjet=MasterjetConnection(
                    transport="https",
                    endpoint=f"https://localhost:{port}/control",
                    timeout_seconds=5,
                ),
            ),
            config_path,
        )
        executable = tmp_path / "codex-usage-task9-https"
        private_lock_root = tmp_path / "runner-private-locks"
        user_site = site.getusersitepackages()
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, ssl, sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {user_site!r})\n"
            f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
            "import codex_usage.private_io as private_io\n"
            f"private_io._private_lock_root = lambda: Path({str(private_lock_root)!r})\n"
            "from codex_usage.cli import main\n"
            "if __name__ == '__main__':\n"
            f"    with Path({str(diagnostics)!r}).open('a', encoding='utf-8') as stream:\n"
            "        stream.write(\n"
            "            json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}) + '\\n'\n"
            "        )\n"
            "    real_default_context = ssl.create_default_context\n"
            "    ssl.create_default_context = lambda *args, **kwargs: "
            f"real_default_context(cafile={str(certificate)!r})\n"
            "    raise SystemExit(main())\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credential_directory))
        monkeypatch.setenv("MASTERJET_BEARER", bearer)
        monkeypatch.setenv("TOTP_CODE", "739104")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

        class Runner(openai_module.BoundedJsonRunner):
            def __init__(self):
                super().__init__(
                    timeout_seconds=15,
                    dispatcher=lambda callback, result: completed.put((callback, result)),
                )

            def submit(self, argv, *, stdin_data=None, callback=None):
                submissions.append(
                    (tuple(argv), None if stdin_data is None else bytes(stdin_data))
                )
                return super().submit(argv, stdin_data=stdin_data, callback=callback)

        runner = Runner()

        def dispatch_one():
            callback, result = completed.get(timeout=20)
            callback(result)
            return result

        google_page = google_module.GoogleAccountsPage(None, None, None)
        google_page._actions = google_module.GoogleActions(
            runner, executable=str(executable), confirm=lambda _preview: True
        )
        google_page._actions.set_projection_ready(True)
        class StepUpEntry:
            def set_visibility(self, _visible):
                return None

            def set_input_purpose(self, _purpose):
                return None

            def get_text(self):
                return "739104"

            def set_text(self, _text):
                return None

        class StepUpDialog:
            def __init__(self, **_kwargs):
                pass

            def add_buttons(self, *_args):
                return None

            def get_content_area(self):
                return SimpleNamespace(pack_start=lambda *_args: None)

            def show_all(self):
                return None

            def run(self):
                return 1

            def destroy(self):
                return None

        monkeypatch.setattr(
            google_module,
            "Gtk",
            SimpleNamespace(
                Dialog=StepUpDialog,
                Entry=StepUpEntry,
                Window=type("Window", (), {}),
                DialogFlags=SimpleNamespace(MODAL=1),
                ResponseType=SimpleNamespace(CANCEL=0, OK=1),
                STOCK_CANCEL="cancel",
                STOCK_OK="ok",
                InputPurpose=SimpleNamespace(DIGITS="digits"),
            ),
        )
        google_page._inventory(None, "google-one")
        google_challenge = dispatch_one()
        assert google_challenge.code == "control.step_up_required", (
            google_challenge,
            requests,
            diagnostics.read_text(encoding="utf-8"),
        )
        assert len(submissions) == 2
        google_result = dispatch_one()

        assert google_result.ok is True
        assert google_page._status.get_text() == "Operation abgeschlossen"
        google_calls = len(submissions)
        google_page._operation_finished(
            google_module.CommandResult(False, None, "control.step_up_required"),
            argv=list(submissions[0][0]),
            retried=True,
        )
        google_page._actions.set_projection_ready(False)
        google_page._operation_finished(
            google_module.CommandResult(False, None, "control.step_up_required"),
            argv=list(submissions[0][0]),
        )
        assert len(submissions) == google_calls

        openai_page = openai_module.OpenAIAccountsPage(None, None, None)
        openai_page._actions = openai_module.OpenAIActions(
            runner, reauth_runner=runner, executable=str(executable)
        )
        openai_page._actions.set_projection_ready(True)
        openai_page._sync_auth(None, "openai-one")
        openai_challenge = dispatch_one()
        assert openai_challenge.code == "control.step_up_required"
        assert openai_challenge.step_up_retry_safe is False
        assert len(submissions) == 3
        assert openai_page._status.get_text() == "Fehler: control.step_up_required"
        openai_calls = len(submissions)
        openai_page._operation_finished(
            openai_module.CommandResult(False, None, "control.step_up_required"),
            argv=list(submissions[2][0]),
            retried=True,
        )
        openai_page._actions.set_projection_ready(False)
        openai_page._operation_finished(
            openai_module.CommandResult(False, None, "control.step_up_required"),
            argv=list(submissions[2][0]),
        )
        assert len(submissions) == openai_calls

    assert [stdin for _argv, stdin in submissions] == [None, b"739104\n", None]
    assert submissions[1][0][1:] == (
        "--step-up-stdin",
        "google",
        "inventory-refresh",
        "google-one",
        "--json",
    )
    assert all(bearer not in " ".join(argv) for argv, _stdin in submissions)
    assert all("739104" not in " ".join(argv) for argv, _stdin in submissions)
    observed = [json.loads(line) for line in diagnostics.read_text(encoding="utf-8").splitlines()]
    assert len(observed) == 3
    assert all(
        item["env"].get("CREDENTIALS_DIRECTORY") == str(credential_directory)
        for item in observed
    )
    assert all("MASTERJET_BEARER" not in item["env"] for item in observed)
    assert all("TOTP_CODE" not in item["env"] for item in observed)
    assert bearer not in diagnostics.read_text(encoding="utf-8")
    assert "739104" not in diagnostics.read_text(encoding="utf-8")
    assert raw_auth.decode("ascii") not in diagnostics.read_text(encoding="utf-8")
    assert all(
        request["headers"]["Authorization"] == f"Bearer {bearer}" for request in requests
    )
    google_requests = [
        request for request in requests if request["operation"] == "google.accounts.list"
    ]
    assert [request["headers"].get("X-Masterjet-Step-Up") for request in google_requests] == [
        None,
        None,
        "739104",
    ]
    openai_requests = [
        request for request in requests if request["operation"] == "openai.accounts.list"
    ]
    assert [request["headers"].get("X-Masterjet-Step-Up") for request in openai_requests] == [None]
    assert [
        request["operation"] for request in requests if request["operation"].startswith("openai.")
    ] == [
        "openai.accounts.list",
        "openai.auth-sync.plan",
        "openai.auth-sync.apply",
    ]
    assert [
        request["operation"] for request in requests if request["operation"].startswith("secret.")
    ] == [
        "secret.ingress.create",
        "secret.ingress.put",
    ]
    assert [request["body"] for request in requests if request["method"] == "PUT"] == [raw_auth]


def test_task9_remote_outage_fail_closes_both_pages_and_all_account_writes(
    tmp_path,
    monkeypatch,
) -> None:
    google_module = _module()
    openai_module = sys.modules["openai_accounts_page"]
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    profile = home / "profile"
    auth_dir = profile / "codex-home"
    auth_dir.mkdir(parents=True, mode=0o700)
    auth_path = auth_dir / "auth.json"
    auth_secret = b'{"tokens":{"account_id":"task9-private-marker"}}'
    auth_path.write_bytes(auth_secret)
    auth_path.chmod(0o600)
    socket_path = tmp_path / "masterjet.sock"
    config_path = home / ".config" / "codex-usage" / "config.toml"
    save_config(
        AppConfig(
            accounts=(
                Account(
                    id="openai-one",
                    label="OpenAI One",
                    profile_dir=str(profile),
                    auth_json_path=str(auth_path),
                    series="A",
                    series_active=True,
                    auth_sync_required=True,
                ),
            ),
            masterjet=MasterjetConnection(
                transport="local", endpoint=str(socket_path), timeout_seconds=2
            ),
        ),
        config_path,
    )
    executable = tmp_path / "codex-usage-task9"
    user_site = site.getusersitepackages()
    lock_root = tmp_path / "private-locks"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {user_site!r})\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "import codex_usage.private_io as private_io\n"
        f"private_io._private_lock_root = lambda: Path({str(lock_root)!r})\n"
        "from codex_usage.cli import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    completed = queue.Queue()
    argv_seen = []

    class Runner(openai_module.BoundedJsonRunner):
        def __init__(self):
            super().__init__(
                timeout_seconds=5,
                dispatcher=lambda callback, result: completed.put((callback, result)),
            )

        def submit(self, argv, *, stdin_data=None, callback=None):
            argv_seen.append(tuple(argv))
            return super().submit(argv, stdin_data=stdin_data, callback=callback)

    runner = Runner()
    openai_page = openai_module.OpenAIAccountsPage(None, None, None)
    openai_page._actions = openai_module.OpenAIActions(
        runner, reauth_runner=runner, executable=str(executable)
    )
    google_page = google_module.GoogleAccountsPage(None, None, None)
    google_page._actions = google_module.GoogleActions(
        runner, executable=str(executable), confirm=lambda _preview: True
    )

    def dispatch_one():
        callback, result = completed.get(timeout=8)
        callback(result)
        return result

    with _task9_unix_control_server(socket_path) as (requests, stop_endpoint):
        openai_page._refresh()
        assert dispatch_one().ok is True
        google_page._refresh()
        assert dispatch_one().ok is True
        preview = google_page.model.preview_plan(
            {
                "account_ref": "google-one",
                "plan_id": "plan-one",
                "expected_generation": 4,
                "plan_digest": "sha256:" + "a" * 64,
                "expires_at": "2026-08-28T18:00:00Z",
                "step_count": 1,
                "projects": [{"project_name": "Amber Meadow", "key_name": "Quiet River"}],
            }
        )
        assert openai_page.model.stale is False
        assert google_page.model.stale is False
        assert openai_page._actions.projection_ready is True
        assert google_page._actions.projection_ready is True
        assert [request["operation"] for request in requests] == [
            "openai.accounts.list",
            "google.accounts.list",
            "google.projects.list",
        ]
        stop_endpoint()

        openai_page._refresh()
        assert dispatch_one().payload["stale"] is True
        google_page._refresh()
        assert dispatch_one().payload["stale"] is True

    calls_before_blocked_writes = len(argv_seen)

    blocked = (
        lambda: google_page._actions.import_oauth_client(
            "google-one", tmp_path / "oauth-client.json"
        ),
        lambda: google_page._actions.oauth_begin("google-one", browser="firefox"),
        lambda: google_page._actions.inventory_refresh("google-one"),
        lambda: google_page._actions.provision_plan("google-one"),
        lambda: google_page._actions.apply(preview),
        lambda: openai_page._actions.reauthenticate("openai-one"),
        lambda: openai_page._actions.sync_auth("openai-one"),
    )
    for write in blocked:
        with pytest.raises(RuntimeError, match="STALE"):
            write()

    cache_path = home / ".local" / "share" / "codex-usage" / "control-snapshot-v1.json"
    assert cache_path.is_file()
    assert auth_secret not in cache_path.read_bytes()
    assert openai_page.model.stale is True
    assert google_page.model.stale is True
    assert "STALE" in openai_page._status.get_text()
    assert "STALE" in google_page._status.get_text()
    assert len(argv_seen) == calls_before_blocked_writes
    assert auth_secret.decode() not in repr(argv_seen)
