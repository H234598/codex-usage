from __future__ import annotations

import importlib.util
import json
import os
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
from codex_master_test_source import codex_master_test_source

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
                "inventory_generation": 4,
                "project_count": 2,
                "billing_count": 1,
                "default_oauth_client_ref": "oauth-client-one",
                "oauth_client_availability": "available",
                "oauth_state": "ready",
                "quota_state": "ready",
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
def _task9_unix_control_server(socket_path: Path, attestation_key_fd: int):
    with codex_master_test_source(require_tests=False):
        try:
            from codex_master.admin_socket import _server_attestation
        except ModuleNotFoundError as exc:
            if exc.name != "codex_master.admin_socket":
                raise
            pytest.skip(
                "codex-master admin_socket unavailable; set CODEX_MASTER_ROOT "
                "to a compatible checkout"
            )

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
                    _server_attestation(connection, attestation_key_fd)
                    raw = bytearray()
                    while b"\n" not in raw:
                        chunk = connection.recv(65_536)
                        if not chunk:
                            break
                        raw.extend(chunk)
                    request = json.loads(raw.split(b"\n", 1)[0])
                    requests.append(request)
                    result = response_for(request)
                    assert result.pop("schema_version") == 1
                    response = json.dumps(
                        {"schema_version": 1, "ok": True, "result": result},
                        separators=(",", ":"),
                    ).encode()
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
    tmp_path: Path,
    *,
    google_challenge_operation: str | None = None,
    openai_challenge_operation: str | None = None,
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
    ingress_accounts = {}
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
            arguments = None
        else:
            envelope = json.loads(request["body"])
            operation = envelope["operation"]
            arguments = envelope["arguments"]
        request["operation"] = operation
        family = (
            "google"
            if operation.startswith("google.")
            or (
                operation == "secret.ingress.create"
                and arguments["account_ref"].startswith("google-")
            )
            else "openai"
        )
        challenge_operation = (
            google_challenge_operation if family == "google" else openai_challenge_operation
        )
        challenge_key = (family, operation)
        if operation == challenge_operation and challenge_key not in challenged_operations:
            challenged_operations.add(challenge_key)
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
        if operation == "openai.auth.plan":
            return operation_base | {
                "id": "plan-1",
                "kind": operation,
                "state": "planned",
                "resulting_generation": None,
                "completed_count": 0,
                "not_attempted_count": 1,
            }
        if operation == "google.oauth-client-import.plan":
            return {
                "schema_version": 1,
                "id": "google-import-plan-1",
                "account_ref": "google-one",
                "expected_generation": 4,
                "expires_at": now.timestamp() + 1_800,
                "plan_digest": operation_base["plan_digest"],
            }
        if operation == "secret.ingress.create":
            ingress_accounts["ingress-1"] = arguments["account_ref"]
            return {
                "schema_version": 1,
                "id": "ingress-1",
                "account_ref": arguments["account_ref"],
                "state": "pending",
                "plan_digest": envelope["plan_digest"],
                "expected_generation": 4,
                "expires_at": now.timestamp() + 900,
                "session_generation": 4,
            }
        if operation == "secret.ingress.put":
            return {
                "schema_version": 1,
                "session_id": "ingress-1",
                "account_ref": ingress_accounts["ingress-1"],
                "state": "consumed",
                "generation": 5,
            }
        if operation == "openai.auth.apply":
            return operation_base | {
                "id": "apply-1",
                "kind": operation,
                "state": "succeeded",
                "resulting_generation": 5,
                "completed_count": 1,
                "not_attempted_count": 0,
            }
        if operation == "google.oauth-client-import.apply":
            return {
                "schema_version": 1,
                "account_ref": "google-one",
                "client_ref": "oauth-client-one",
                "display_name": "Task9 OAuth Client",
                "inventory_generation": 5,
                "client_digest": "sha256:" + "b" * 64,
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
                            chunk = tls_socket.recv(4096)
                            if not chunk:
                                return
                            raw.extend(chunk)
                        head, body = raw.split(b"\r\n\r\n", 1)
                        lines = head.decode("ascii").split("\r\n")
                        headers = dict(line.split(": ", 1) for line in lines[1:])
                        length = int(headers["Content-Length"])
                        while len(body) < length:
                            chunk = tls_socket.recv(length - len(body))
                            if not chunk:
                                return
                            body.extend(chunk)
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
                        tls_socket.shutdown(socket.SHUT_RDWR)
                        tls_socket.close()
        finally:
            listener.close()
            finished.set()

    thread = threading.Thread(target=serve, name="task9-https-control", daemon=True)
    thread.start()
    try:
        yield port, certificate, requests
    finally:
        stopped.set()
        wake = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            wake.connect(("127.0.0.1", port))
        except OSError:
            pass
        finally:
            wake.close()
        assert finished.wait(2)
        thread.join(timeout=0)


def test_google_widget_renders_account_cards_status_and_project_table() -> None:
    page = _module().GoogleAccountsModel()

    page.render(_payload())

    card = page.card("google-one")
    assert card.enabled is True
    assert card.default_oauth_client_ref == "oauth-client-one"
    assert card.oauth_client_availability == "available"
    assert card.oauth_state == "ready"
    assert card.quota_state == "ready"
    assert card.reload_state == "ready"
    assert card.inventory_generation == 4
    assert card.oauth_enabled is True
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


@pytest.mark.parametrize("newest_ok", [False, True])
def test_google_refresh_revokes_immediately_and_accepts_only_newest_result(newest_ok) -> None:
    module = _module()
    callbacks = []

    class Runner:
        def submit(self, _argv, **options):
            callbacks.append(options.get("callback"))

    page = module.GoogleAccountsPage(None, None, None)
    page._actions = module.GoogleActions(Runner(), executable="/opt/codex-usage")
    page.render(_payload())
    assert page._actions.projection_ready is True

    page._refresh()
    assert page.model.stale is True
    assert page._actions.projection_ready is False
    page._refresh()
    success = module.CommandResult(True, _payload(), "ok")
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


def test_google_destroy_ignores_pending_result_and_step_up_and_closes_runners(
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
    page = module.GoogleAccountsPage(None, None, None)
    page._runner = runner
    page._oauth_runner = runner
    page._actions = module.GoogleActions(
        runner,
        oauth_runner=runner,
        executable="/opt/codex-usage",
    )
    page.render(_payload())
    prompted = []
    monkeypatch.setattr(
        page,
        "prompt_step_up",
        lambda: prompted.append(True) or bytearray(b"739104"),
    )
    page._inventory(None, "google-one")
    status = page._status.get_text()

    page._on_destroy()
    callbacks[-1](module.CommandResult(True, {}, "ok"))

    assert challenges[-1]() is None
    assert prompted == []
    assert page._status.get_text() == status
    assert runner.closed is True


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
    [("subject_bound", "true")],
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


def test_oauth_requires_fresh_available_server_projected_client_ref() -> None:
    model = _module().GoogleAccountsModel()
    payload = _payload()
    payload["accounts"][0]["default_oauth_client_ref"] = None
    payload["accounts"][0]["oauth_client_availability"] = "missing"

    model.render(payload)

    assert model.card("google-one").oauth_enabled is False


def test_disabled_google_account_blocks_every_mutation_even_when_fresh() -> None:
    page = _module().GoogleAccountsModel()
    payload = _payload(stale=False)
    payload["accounts"][0]["enabled"] = False

    page.render(payload)

    card = page.card("google-one")
    assert card.add_enabled is False
    assert card.oauth_enabled is False
    assert card.inventory_enabled is False
    assert card.plan_enabled is False
    assert card.apply_enabled is False


@pytest.mark.parametrize(
    "missing_field",
    ["enabled", "oauth_state", "quota_state", "reload_state"],
)
def test_google_account_status_fields_are_required(missing_field) -> None:
    payload = _payload()
    del payload["accounts"][0][missing_field]
    model = _module().GoogleAccountsModel()

    with pytest.raises(ValueError, match="Google account"):
        model.render(payload)

    assert model.cards == ()


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("enabled", "true"),
        ("oauth_state", ""),
        ("quota_state", "x" * 65),
        ("reload_state", None),
    ],
)
def test_google_account_status_fields_are_bounded(field, invalid_value) -> None:
    payload = _payload()
    payload["accounts"][0][field] = invalid_value
    model = _module().GoogleAccountsModel()

    with pytest.raises(ValueError, match=field):
        model.render(payload)

    assert model.cards == ()


def test_invalid_google_status_projection_clears_previous_cards() -> None:
    payload = _payload()
    invalid = _payload()
    invalid["accounts"][0]["reload_state"] = ""
    model = _module().GoogleAccountsModel()
    model.render(payload)

    with pytest.raises(ValueError, match="reload_state"):
        model.render(invalid)

    assert model.stale is True
    assert model.details_available is False
    assert model.cards == ()


def test_google_page_summary_shows_projected_status_fields() -> None:
    module = _module()
    page = module.GoogleAccountsPage(None, None, None)

    page.render(_payload())

    labels = [
        child.get_label()
        for frame in page._body.get_children()
        for child in frame.get_child().get_children()
        if isinstance(child, module.Gtk.Label)
    ]
    assert any(
        "Aktiv ja" in label
        and "OAuth ready" in label
        and "Quota ready" in label
        and "Reload ready" in label
        for label in labels
    )


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


def test_oauth_uses_dedicated_browser_timeout_runner() -> None:
    calls = []

    class Runner:
        def __init__(self, name):
            self.name = name

        def submit(self, argv, **options):
            calls.append((self.name, tuple(argv), options))

    actions = _module().GoogleActions(
        Runner("short"),
        oauth_runner=Runner("oauth"),
        executable="/opt/codex-usage",
    )
    actions.set_projection_ready(True)
    actions.oauth_begin("google-one", browser="firefox")

    assert calls == [
        (
            "oauth",
            (
                "/opt/codex-usage",
                "google",
                "oauth-begin",
                "google-one",
                "--browser",
                "firefox",
                "--json",
            ),
            {"stdin_data": None, "callback": None},
        )
    ]


def test_google_page_actions_use_running_challenge_callbacks(monkeypatch) -> None:
    module = _module()
    page = module.GoogleAccountsPage(None, None, None)
    pending = {}
    callbacks = []

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
        projection_version = 0

        def oauth_begin(self, _account, *, browser, callback, challenge_callback):
            assert browser == "firefox"
            pending["oauth"] = callback
            callbacks.append(challenge_callback)

        def import_oauth_client(self, _account, _source, *, callback, challenge_callback):
            pending["import"] = callback
            callbacks.append(challenge_callback)

        def inventory_refresh(self, _account, *, callback, challenge_callback):
            pending["inventory"] = callback
            callbacks.append(challenge_callback)

        def provision_plan(self, _account, *, callback, challenge_callback):
            pending["plan"] = callback
            callbacks.append(challenge_callback)

        def apply(self, _preview, *, callback, challenge_callback):
            pending["apply"] = callback
            callbacks.append(challenge_callback)
            return True

    page._actions = Actions()

    def assert_prompted() -> None:
        code = callbacks[-1]()
        assert code == bytearray(b"739104")
        code[:] = b"\x00" * len(code)
        code.clear()

    page._oauth_begin(None, "google-one")
    assert_prompted()
    page.choose_oauth_client("google-one")
    assert_prompted()
    page._inventory(None, "google-one")
    assert_prompted()
    page._plan(None, "google-one")
    assert_prompted()
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
    assert_prompted()
    assert len(callbacks) == 5


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


@pytest.mark.parametrize(
    ("challenge_operation", "openai_operations", "secret_operations"),
    [
        (
            "secret.ingress.create",
            ["openai.accounts.list", "openai.auth.plan", "openai.auth.apply"],
            ["secret.ingress.create", "secret.ingress.create", "secret.ingress.put"],
        ),
        (
            "openai.auth.apply",
            [
                "openai.accounts.list",
                "openai.auth.plan",
                "openai.auth.apply",
                "openai.auth.apply",
            ],
            ["secret.ingress.create", "secret.ingress.put"],
        ),
    ],
)
def test_https_auth_sync_challenge_resumes_running_process(
    tmp_path, monkeypatch, challenge_operation, openai_operations, secret_operations
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
    oauth_directory = home / "oauth-client"
    oauth_directory.mkdir(mode=0o700)
    raw_oauth_client = b'{"client_secret":"google-runner-secret-marker"}'
    oauth_client_path = oauth_directory / "client.json"
    oauth_client_path.write_bytes(raw_oauth_client)
    oauth_client_path.chmod(0o600)
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir(mode=0o700)
    bearer = "runner-system-bearer-marker"
    credential = credential_directory / "masterjet-control-bearer"
    credential.write_text(bearer, encoding="ascii")
    credential.chmod(0o400)
    diagnostics = tmp_path / "runner-diagnostics.jsonl"
    completed = queue.Queue()
    submissions = []
    prompts = []

    with _task9_https_control_server(
        tmp_path,
        google_challenge_operation="secret.ingress.create",
        openai_challenge_operation=challenge_operation,
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
                        prompt_dispatcher=lambda prompt: prompt(),
                    )

            def submit(self, argv, *, stdin_data=None, callback=None, challenge_callback=None):
                submissions.append(
                    (tuple(argv), None if stdin_data is None else bytes(stdin_data))
                )
                return super().submit(
                    argv,
                    stdin_data=stdin_data,
                    callback=callback,
                    challenge_callback=challenge_callback,
                )

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
        openai_page = openai_module.OpenAIAccountsPage(None, None, None)
        openai_page._actions = openai_module.OpenAIActions(
            runner, reauth_runner=runner, executable=str(executable)
        )
        openai_page._actions.set_projection_ready(True)
        class StepUpEntry:
            def set_visibility(self, _visible):
                return None

            def set_input_purpose(self, _purpose):
                return None

            def get_text(self):
                prompts.append("prompt")
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
        monkeypatch.setattr(openai_module, "Gtk", google_module.Gtk)
        google_page._inventory(None, "google-one")
        google_result = dispatch_one()

        assert google_result.ok is True
        assert google_page._status.get_text() == "Operation abgeschlossen"
        assert len(submissions) == 1

        google_page._actions.import_oauth_client(
            "google-one",
            oauth_client_path,
            callback=google_page._operation_finished,
            challenge_callback=google_page._prompt_running_step_up,
        )
        google_import_result = dispatch_one()
        assert google_import_result.ok is True
        assert google_import_result.payload == {
            "account_ref": "google-one",
            "generation": 5,
            "status": "succeeded",
            "ok": True,
        }
        assert len(submissions) == 2

        openai_page._sync_auth(None, "openai-one")
        openai_result = dispatch_one()
        assert openai_result.ok is True
        assert openai_page._status.get_text() == "Operation abgeschlossen"
        assert len(submissions) == 3

    assert prompts == ["prompt", "prompt", "prompt"]
    assert [stdin for _argv, stdin in submissions] == [None, None, None]
    assert submissions[0][0][1:] == (
        "--step-up-stdin", "google", "inventory-refresh", "google-one", "--json"
    )
    assert submissions[1][0][1:] == (
        "--step-up-stdin",
        "google",
        "add",
        "google-one",
        "--oauth-client-json",
        str(oauth_client_path),
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
    assert raw_oauth_client.decode("ascii") not in diagnostics.read_text(encoding="utf-8")
    assert all(
        request["headers"]["Authorization"] == f"Bearer {bearer}" for request in requests
    )
    google_requests = [
        request for request in requests if request["operation"] == "google.accounts.list"
    ]
    assert [request["headers"].get("X-Masterjet-Step-Up") for request in google_requests] == [
        None,
        "739104",
        None,
    ]
    google_create_requests = [
        request
        for request in requests
        if request["operation"] == "secret.ingress.create"
        and json.loads(request["body"])["arguments"]["account_ref"] == "google-one"
    ]
    assert len(google_create_requests) == 2
    assert google_create_requests[0]["body"] == google_create_requests[1]["body"]
    assert [
        request["headers"].get("X-Masterjet-Step-Up")
        for request in google_create_requests
    ] == [None, "739104"]
    google_create_envelopes = [json.loads(request["body"]) for request in google_create_requests]
    assert {item["expected_generation"] for item in google_create_envelopes} == {4}
    assert len({item["idempotency_key"] for item in google_create_envelopes}) == 1
    openai_requests = [
        request for request in requests if request["operation"] == "openai.accounts.list"
    ]
    assert [request["headers"].get("X-Masterjet-Step-Up") for request in openai_requests] == [None]
    assert [
        request["operation"] for request in requests if request["operation"].startswith("openai.")
    ] == openai_operations
    assert [
        request["operation"] for request in requests if request["operation"].startswith("secret.")
    ] == [
        "secret.ingress.create",
        "secret.ingress.create",
        "secret.ingress.put",
        *secret_operations,
    ]
    assert [request["body"] for request in requests if request["method"] == "PUT"] == [
        raw_oauth_client,
        raw_auth,
    ]


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
    credential_directory = tmp_path / "credentials"
    credential_directory.mkdir(mode=0o700)
    attestation_key = credential_directory / "masterjet-local-attestation-key"
    attestation_key.write_bytes(b"k" * 32)
    attestation_key.chmod(0o400)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credential_directory))
    attestation_key_fd = os.open(
        attestation_key,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
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

    try:
        with _task9_unix_control_server(
            socket_path, attestation_key_fd
        ) as (requests, stop_endpoint):
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
                    "projects": [
                        {"project_name": "Amber Meadow", "key_name": "Quiet River"}
                    ],
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
    finally:
        os.close(attestation_key_fd)

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
