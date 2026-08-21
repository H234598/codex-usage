from __future__ import annotations

import base64
import json
import shutil
import ssl
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pytest

import codex_usage.bridge as bridge_module
from codex_usage.bridge import (
    _authenticated_snapshot_supersedes_browser_current,
    _BoundedThreadingHTTPServer,
    _bridge_host_requires_tls,
    _browser_payload_is_covered_by_authenticated_state,
    _json_candidates_from_payload,
    _make_handler,
    _newest_known_usage,
    _parse_captured_at,
    _redact_url,
    _safe_context_value,
    _safe_excerpt,
    _sanitize_debug_number,
    _tls_context,
    bridge_token_for_account,
    bridge_token_matches,
    ingest_and_save,
    load_latest_usages,
    render_bridge_snippet,
    revoke_bridge_token,
    run_bridge_server,
    save_bridge_debug_payload,
    usage_from_ingest_payload,
    write_bridge_extension,
)
from codex_usage.config import AppConfig, add_or_update_account, save_config
from codex_usage.models import Account, AccountStatus, AccountUsage, LimitWindow, UsagePool
from codex_usage.routing import evaluate_routing
from codex_usage.state import (
    load_current_usage,
    load_state_generation,
    load_usage_snapshot,
    remove_account_state,
    save_current_usage,
    save_usage_snapshot,
)


def _jwt_with_claims(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


def test_parse_captured_at_uses_dst_aware_local_zone(monkeypatch):
    berlin = ZoneInfo("Europe/Berlin")
    monkeypatch.setattr("codex_usage.bridge.LOCAL_TZ", berlin)
    expected = datetime(2026, 1, 15, 0, 15, tzinfo=berlin)

    assert _parse_captured_at("2026-01-15T00:15:00") == expected
    assert _parse_captured_at("2026-01-14T23:15:00Z") == expected


def test_bridge_text_sanitizers_normalize_and_bound_whitespace():
    assert _safe_excerpt("\n  alpha\t beta  ", limit=9) == "alpha ..."
    assert _safe_context_value("alpha\n beta gamma", limit=11) == "alpha be..."


def test_bridge_server_rejects_connections_when_slots_are_exhausted(tmp_path, monkeypatch):
    monkeypatch.setattr("codex_usage.bridge.BRIDGE_MAX_CONNECTIONS", 1)
    handler = _make_handler(AppConfig(accounts=()), tmp_path / "snapshots", {})
    server = _BoundedThreadingHTTPServer(("127.0.0.1", 0), handler)

    class FakeRequest:
        closed = False

        def close(self):
            self.closed = True

    request = FakeRequest()
    assert server._connection_slots.acquire(blocking=False) is True
    try:
        server.process_request(request, ("127.0.0.1", 0))
        assert request.closed is True
    finally:
        server._connection_slots.release()
        server.server_close()


def test_bridge_tls_wraps_each_accepted_connection_with_handshake_timeout(
    tmp_path, monkeypatch
):
    handler = _make_handler(AppConfig(accounts=()), tmp_path / "snapshots", {})
    raw_socket = type(
        "RawSocket",
        (),
        {
            "settimeout": lambda self, value: setattr(self, "timeout", value),
            "close": lambda self: setattr(self, "closed", True),
        },
    )()
    wrapped_socket = type(
        "WrappedSocket",
        (),
        {
            "do_handshake": lambda self: setattr(self, "handshaken", True),
            "settimeout": lambda self, value: setattr(self, "timeout", value),
            "close": lambda self: setattr(self, "closed", True),
        },
    )()

    class FakeTLSContext:
        def wrap_socket(self, sock, *, server_side, do_handshake_on_connect):
            assert sock is raw_socket
            assert server_side is True
            assert do_handshake_on_connect is False
            return wrapped_socket

    monkeypatch.setattr(
        ThreadingHTTPServer,
        "get_request",
        lambda _server: (raw_socket, ("127.0.0.1", 1)),
    )
    server = _BoundedThreadingHTTPServer(
        ("127.0.0.1", 0),
        handler,
        tls_context=FakeTLSContext(),
    )
    try:
        request, address = server.get_request()
    finally:
        server.server_close()

    assert request is wrapped_socket
    assert address == ("127.0.0.1", 1)
    assert getattr(wrapped_socket, "handshaken", False) is False
    assert wrapped_socket.timeout == bridge_module.BRIDGE_REQUEST_TIMEOUT_SECONDS


def test_bridge_tls_handshake_runs_in_bounded_worker(tmp_path):
    handler = _make_handler(AppConfig(accounts=()), tmp_path / "snapshots", {})
    server = _BoundedThreadingHTTPServer(
        ("127.0.0.1", 0),
        handler,
        tls_context=object(),
    )
    events = []
    request = type(
        "WrappedSocket",
        (),
        {
            "do_handshake": lambda self: events.append("handshake"),
            "close": lambda self: events.append("close"),
        },
    )()
    server.finish_request = lambda current, address: events.append("handler")
    server.shutdown_request = lambda current: events.append("shutdown")
    assert server._connection_slots.acquire(blocking=False)
    try:
        server.process_request_thread(request, ("127.0.0.1", 1))
    finally:
        server.server_close()
    assert events == ["handshake", "handler", "shutdown"]
    assert server._connection_slots.acquire(blocking=False)
    server._connection_slots.release()


def test_bridge_tls_handshake_failure_skips_handler_and_releases_slot(tmp_path):
    handler = _make_handler(AppConfig(accounts=()), tmp_path / "snapshots", {})
    server = _BoundedThreadingHTTPServer(
        ("127.0.0.1", 0),
        handler,
        tls_context=object(),
    )
    events = []

    def fail_handshake(_self):
        events.append("handshake")
        raise ssl.SSLError("bad client hello")

    request = type(
        "WrappedSocket",
        (),
        {
            "do_handshake": fail_handshake,
            "close": lambda self: events.append("close"),
        },
    )()
    server.finish_request = lambda current, address: events.append("handler")
    server.shutdown_request = lambda current: events.append("shutdown")
    assert server._connection_slots.acquire(blocking=False)
    try:
        server.process_request_thread(request, ("127.0.0.1", 1))
    finally:
        server.server_close()
    assert events == ["handshake", "close"]
    assert server._connection_slots.acquire(blocking=False)
    server._connection_slots.release()


def test_tls_context_requires_matching_private_material(tmp_path):
    assert _tls_context(None, None) is None
    with pytest.raises(ValueError, match="both certificate and key"):
        _tls_context(tmp_path / "cert.pem", None)

    certificate = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    certificate.write_text("certificate", encoding="utf-8")
    key.write_text("private key", encoding="utf-8")
    key.chmod(0o644)

    with pytest.raises(ValueError, match="permissions too broad"):
        _tls_context(certificate, key)


def test_bridge_server_rejects_plaintext_non_loopback_before_bind(monkeypatch):
    monkeypatch.setattr(
        "codex_usage.bridge._BoundedThreadingHTTPServer",
        lambda *_args, **_kwargs: pytest.fail("server must not bind"),
    )

    assert _bridge_host_requires_tls("127.0.0.1") is False
    assert _bridge_host_requires_tls("localhost") is False
    assert _bridge_host_requires_tls("0.0.0.0") is True

    with pytest.raises(ValueError, match="require TLS"):
        run_bridge_server(AppConfig(accounts=()), host="0.0.0.0", port=8765)


@pytest.mark.parametrize("config", [None, [], object()])
def test_bridge_server_rejects_invalid_config_before_bind(config):
    with pytest.raises(ValueError, match="config is invalid"):
        run_bridge_server(config, host="127.0.0.1", port=8765)  # type: ignore[arg-type]


@pytest.mark.parametrize("host", [None, [], 1, object()])
def test_bridge_server_rejects_invalid_host_before_bind(host):
    with pytest.raises(ValueError, match="bridge host is invalid"):
        run_bridge_server(AppConfig(accounts=()), host=host, port=8765)  # type: ignore[arg-type]


@pytest.mark.parametrize("port", [None, [], "8765", 0, 65536, True, object()])
def test_bridge_server_rejects_invalid_port_before_bind(port):
    with pytest.raises(ValueError, match="bridge port is invalid"):
        run_bridge_server(AppConfig(accounts=()), host="127.0.0.1", port=port)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("parameter", "value", "message"),
    [
        ("snapshot_dir", "invalid", "snapshot directory"),
        ("config_path", "invalid", "config path"),
        ("tls_cert", "invalid", "TLS certificate path"),
        ("tls_key", "invalid", "TLS key path"),
    ],
)
def test_bridge_server_rejects_invalid_paths_before_bind(parameter, value, message):
    with pytest.raises(ValueError, match=message):
        run_bridge_server(
            AppConfig(accounts=()),
            host="127.0.0.1",
            port=8765,
            **{parameter: value},  # type: ignore[arg-type]
        )


def test_parse_captured_at_strict_mode_rejects_ambiguous_values():
    with pytest.raises(ValueError, match="timezone"):
        _parse_captured_at("2026-01-15T00:15:00", strict=True)
    with pytest.raises(ValueError, match="required"):
        _parse_captured_at(None, strict=True)
    with pytest.raises(ValueError, match="invalid"):
        _parse_captured_at("not-a-timestamp", strict=True)


@pytest.mark.parametrize("url", [None, [], {}, "http://[malformed"])
def test_redact_url_rejects_malformed_external_values(url):
    assert _redact_url(url) == ""


def test_redact_url_removes_userinfo_and_rejects_invalid_port():
    assert (
        _redact_url("https://user:secret@example.test:8443/path?token=value#fragment")
        == "https://example.test:8443/path"
    )
    assert _redact_url("https://example.test:invalid/path") == ""


@pytest.mark.parametrize(
    "value",
    [float("inf"), float("-inf"), float("nan"), "9" * 5000],
)
def test_debug_number_sanitizer_rejects_non_finite_and_unbounded_values(value):
    assert _sanitize_debug_number(value) is None


@pytest.mark.parametrize("captured_at", [0, False, [], {}])
def test_parse_captured_at_rejects_present_non_string_values(captured_at):
    with pytest.raises(ValueError, match="ISO-8601"):
        _parse_captured_at(captured_at)


@pytest.mark.parametrize(
    "captured_at",
    [None, "", "not-a-timestamp", "2026-01-15T00:15:00", "2099-01-15T00:15:00Z"],
)
def test_ingest_rejects_invalid_capture_timestamp_before_saving(tmp_path, captured_at):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
            backend_configured="direct",
            backend_used="direct",
            five_hour=LimitWindow(name="5h", remaining=97),
            weekly=LimitWindow(name="weekly", remaining=55),
            backend_user_id="browser-user",
            backend_account_id="browser-account",
        ),
        snapshot_dir,
    )
    payload = {
        "bodyText": "5-hour limit 42 / 100 Weekly limit 310 / 1000",
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "browser-user",
                        "account_id": "browser-account",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 42,
                                "limit_window_seconds": 18000,
                            },
                            "secondary_window": {
                                "used_percent": 31,
                                "limit_window_seconds": 604800,
                            },
                        },
                    }
                ),
            }
        ],
    }
    if captured_at is not None:
        payload["capturedAt"] = captured_at

    with pytest.raises(ValueError, match="capture timestamp"):
        ingest_and_save(
            config,
            "privat",
            payload,
            snapshot_dir,
            require_backend_identity=True,
        )

    saved = load_usage_snapshot("privat", snapshot_dir)
    assert saved is not None
    assert saved.five_hour is not None and saved.five_hour.remaining == 97


def test_ingest_rejects_conflicting_capture_timestamp_fields(tmp_path):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    config = AppConfig(accounts=(account,))
    payload = {
        "capturedAt": "2026-01-15T00:15:00Z",
        "captured_at": "2026-01-15T01:15:00Z",
        "bodyText": "5-hour limit 42 / 100 Weekly limit 310 / 1000",
    }

    with pytest.raises(ValueError, match="conflicting capture timestamps"):
        ingest_and_save(config, "privat", payload, tmp_path / "snapshots")


def test_bridge_rejects_incomparable_known_timestamps():
    current = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 1, 15, 0, 15),
    )
    snapshot = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 1, 15, 0, 15, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    with pytest.raises(ValueError, match="not comparable"):
        _newest_known_usage(current, snapshot)


def test_bridge_blocks_browser_when_authenticated_timestamp_is_incomparable():
    browser = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 1, 15, 0, 15, tzinfo=ZoneInfo("Europe/Berlin")),
        backend_used="browser",
        backend_user_id="user",
        backend_account_id="account",
    )
    authenticated = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime(2026, 1, 15, 0, 15),
        status=AccountStatus.PARTIAL,
        backend_used="direct",
        backend_user_id="user",
        backend_account_id="account",
    )

    assert _browser_payload_is_covered_by_authenticated_state(
        AppConfig(accounts=()), browser, authenticated
    )
    assert _authenticated_snapshot_supersedes_browser_current(
        browser,
        authenticated,
        300,
    )


@pytest.mark.parametrize("field", ["backend_used", "status"])
def test_bridge_rejects_unhashable_authenticated_fields(field):
    browser = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
        backend_used="browser",
        backend_user_id="user",
        backend_account_id="account",
    )
    authenticated = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
        status=AccountStatus.PARTIAL,
        backend_used="direct",
        backend_user_id="user",
        backend_account_id="account",
        weekly=LimitWindow(name="weekly", remaining=80),
    )
    malformed = replace(authenticated, **{field: []})

    assert _browser_payload_is_covered_by_authenticated_state(
        AppConfig(accounts=()), browser, malformed
    ) is False
    assert _authenticated_snapshot_supersedes_browser_current(
        browser,
        malformed,
        300,
    ) is False


def test_latest_default_cache_uses_shared_account_lock(monkeypatch):
    events: list[tuple[str, str]] = []

    class TrackingLock:
        def __enter__(self):
            events.append(("enter", self.account_id))
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            events.append(("exit", self.account_id))
            return False

        def __init__(self, account_id: str):
            self.account_id = account_id

    monkeypatch.setattr(
        "codex_usage.bridge.account_lock",
        lambda account_id: TrackingLock(account_id),
    )

    assert load_latest_usages(AppConfig(accounts=())) == []
    assert events == [
        ("enter", "__all_accounts__"),
        ("exit", "__all_accounts__"),
    ]


@pytest.mark.parametrize("config", [None, [], {}, object()])
def test_load_latest_usages_rejects_non_config(config):
    with pytest.raises(ValueError, match="config is invalid"):
        load_latest_usages(config)  # type: ignore[arg-type]


@pytest.mark.parametrize("snapshot_dir", [[], "invalid", 1, True, object()])
def test_load_latest_usages_rejects_non_path(snapshot_dir):
    with pytest.raises(ValueError, match="snapshot directory is invalid"):
        load_latest_usages(AppConfig(accounts=()), snapshot_dir)  # type: ignore[arg-type]


def test_usage_from_ingest_payload_extracts_visible_values():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics?secret=1",
            "capturedAt": "2026-06-08T04:20:00+02:00",
            "bodyText": """
            5 Stunden Nutzungsgrenze
            42 / 100 genutzt
            Zurücksetzungen 08.06.2026 04:26
            Wöchentliches Nutzungslimit
            310 / 1000 genutzt
            Zurücksetzungen 14.06.2026 04:26
            """,
        },
    )

    assert usage.status == AccountStatus.OK
    assert usage.source_urls == ("https://chatgpt.com/codex/cloud/settings/analytics",)
    assert usage.five_hour is not None
    assert usage.five_hour.used == 42
    assert usage.weekly is not None
    assert usage.weekly.limit == 1000
    assert usage.main is not None
    assert usage.main.availability_sources == ("usage", "browser")
    decision = evaluate_routing(
        usage,
        role="arbeitsbiene",
        paid_overage_allowed=False,
        now=datetime(2026, 6, 8, 4, 21, tzinfo=ZoneInfo("Europe/Berlin")),
    )
    assert decision["decision"] == "main"


@pytest.mark.parametrize("payload", [None, [], "invalid", 1, True, object()])
def test_usage_from_ingest_payload_rejects_non_object_payload(payload):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")

    with pytest.raises(ValueError, match="ingest payload must be an object"):
        usage_from_ingest_payload(account, payload)  # type: ignore[arg-type]


@pytest.mark.parametrize("account", [None, [], "invalid", 1, True, object()])
def test_usage_from_ingest_payload_rejects_non_account(account):
    with pytest.raises(ValueError, match="account is invalid"):
        usage_from_ingest_payload(account, {})  # type: ignore[arg-type]


def test_usage_from_ingest_payload_reports_empty_text_context():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "title": "Codex",
            "readyState": "complete",
            "textLength": 0,
            "bodyText": "",
        },
    )

    assert usage.status == AccountStatus.PARTIAL
    assert usage.cache_invalidated is True
    assert usage.error is not None
    assert "missing page text" in usage.error
    assert "ready=complete" in usage.error
    assert "textLength=0" in usage.error


@pytest.mark.parametrize(
    "truncated_fields",
    [
        {"bodyText": True},
        {"bodyText": "true"},
        [],
        None,
    ],
)
def test_usage_from_ingest_payload_ignores_truncated_text_fields(truncated_fields):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")

    usage = usage_from_ingest_payload(
        account,
        {
            "capturedAt": "2026-06-08T04:20:00+02:00",
            "bodyText": (
                "5 Stunden Nutzungsgrenze 97% verbleibend "
                "Wöchentliches Nutzungslimit 55% verbleibend"
            ),
            "truncatedFields": truncated_fields,
        },
    )

    assert usage.status == AccountStatus.PARTIAL
    assert usage.five_hour is None
    assert usage.weekly is None


def test_ingest_rejects_unidentified_browser_payload_before_saving(tmp_path):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"

    with pytest.raises(ValueError, match="no backend account identity"):
        ingest_and_save(
            config,
            "privat",
            {
                "url": "https://chatgpt.com/codex/cloud/settings/analytics",
                "bodyText": (
                    "5-hour usage limit 97% remaining "
                    "Weekly usage limit 55% remaining"
                ),
            },
            snapshot_dir,
            require_backend_identity=True,
        )

    assert load_usage_snapshot("privat", snapshot_dir) is None


def test_ingest_rejects_first_browser_identity_before_saving(tmp_path):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    payload = {
        "url": "https://chatgpt.com/codex/cloud/settings/analytics",
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "wrong-browser-user",
                        "account_id": "wrong-browser-account",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 3,
                                "limit_window_seconds": 18000,
                            },
                            "secondary_window": {
                                "used_percent": 45,
                                "limit_window_seconds": 604800,
                            },
                        },
                    }
                ),
            }
        ],
    }

    with pytest.raises(ValueError, match="identity is not initialized"):
        ingest_and_save(
            config,
            "privat",
            payload,
            snapshot_dir,
            require_backend_identity=True,
        )

    assert load_usage_snapshot("privat", snapshot_dir) is None


def test_ingest_accepts_matching_initialized_browser_identity(tmp_path):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
            five_hour=LimitWindow(name="5h", remaining=97),
            weekly=LimitWindow(name="weekly", remaining=55),
            backend_user_id="browser-user",
            backend_account_id="browser-account",
        ),
        snapshot_dir,
    )
    payload = {
        "url": "https://chatgpt.com/codex/cloud/settings/analytics",
        "capturedAt": "2026-06-08T04:25:00+02:00",
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "browser-user",
                        "account_id": "browser-account",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 4,
                                "limit_window_seconds": 18000,
                            },
                            "secondary_window": {
                                "used_percent": 46,
                                "limit_window_seconds": 604800,
                            },
                        },
                    }
                ),
            }
        ],
    }

    usage, _path = ingest_and_save(
        config,
        "privat",
        payload,
        snapshot_dir,
        require_backend_identity=True,
    )

    assert usage.five_hour is not None and usage.five_hour.used == 4
    assert usage.weekly is not None and usage.weekly.used == 46


def test_ingest_clears_browser_cache_after_backend_identity_switch(tmp_path):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        backend="browser",
    )
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    captured = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    known = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        status=AccountStatus.OK,
        backend_configured="browser",
        backend_used="browser",
        backend_user_id="old-browser-user",
        backend_account_id="old-browser-account",
        five_hour=LimitWindow(name="5h", remaining=80),
        weekly=LimitWindow(name="weekly", remaining=60),
    )
    save_usage_snapshot(known, snapshot_dir)
    save_current_usage(known, snapshot_dir.parent / "current")
    payload = {
        "url": "https://chatgpt.com/codex/cloud/settings/analytics",
        "capturedAt": "2026-06-08T04:25:00+02:00",
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "new-browser-user",
                        "account_id": "new-browser-account",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 20,
                                "limit_window_seconds": 18000,
                            },
                            "secondary_window": {
                                "used_percent": 40,
                                "limit_window_seconds": 604800,
                            },
                        },
                    }
                ),
            }
        ],
    }

    with pytest.raises(ValueError, match="different backend account"):
        ingest_and_save(
            config,
            "privat",
            payload,
            snapshot_dir,
            require_backend_identity=True,
        )

    for saved in (
        load_usage_snapshot("privat", snapshot_dir),
        load_current_usage("privat", snapshot_dir.parent / "current"),
    ):
        assert saved is not None
        assert saved.status == AccountStatus.PARTIAL
        assert saved.cache_invalidated is True
        assert saved.five_hour is None
        assert saved.weekly is None
        assert saved.error == "cached browser usage discarded after backend identity changed"


def test_ingest_marks_browser_provenance_and_does_not_restore_direct_window(
    tmp_path,
):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    captured = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=captured,
            status=AccountStatus.OK,
            backend_configured="direct",
            backend_used="direct",
            backend_user_id="browser-user",
            backend_account_id="browser-account",
            five_hour=LimitWindow(name="5h", remaining=80),
            weekly=LimitWindow(name="weekly", remaining=60),
        ),
        snapshot_dir,
    )
    payload = {
        "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "capturedAt": "2026-06-08T04:30:00+02:00",
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "browser-user",
                        "account_id": "browser-account",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 20,
                                "limit_window_seconds": 18000,
                            }
                        },
                    }
                ),
            }
        ],
    }

    usage, _path = ingest_and_save(
        config,
        "privat",
        payload,
        snapshot_dir,
        require_backend_identity=True,
    )

    saved = load_current_usage("privat", snapshot_dir.parent / "current")
    assert usage.backend_configured == "direct"
    assert usage.backend_used == "browser"
    assert saved is not None
    assert saved.backend_used == "browser"
    assert saved.five_hour is not None and saved.five_hour.remaining == 80
    assert saved.weekly is None


def test_ingest_rejects_browser_payload_over_recent_authenticated_current(tmp_path):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    captured = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    save_current_usage(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=captured,
            status=AccountStatus.OK,
            backend_configured="direct",
            backend_used="direct",
            backend_user_id="browser-user",
            backend_account_id="browser-account",
            five_hour=LimitWindow(name="5h", remaining=80),
            weekly=LimitWindow(name="weekly", remaining=60),
        ),
        snapshot_dir.parent / "current",
    )
    payload = {
        "url": "https://chatgpt.com/codex/cloud/settings/analytics",
        "capturedAt": "2026-06-08T04:25:00+02:00",
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "browser-user",
                        "account_id": "browser-account",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 20,
                                "limit_window_seconds": 18000,
                            }
                        },
                    }
                ),
            }
        ],
    }

    with pytest.raises(ValueError, match="current authenticated state"):
        ingest_and_save(
            config,
            "privat",
            payload,
            snapshot_dir,
            require_backend_identity=True,
        )

    saved = load_current_usage("privat", snapshot_dir.parent / "current")
    assert saved is not None
    assert saved.backend_used == "direct"
    assert saved.five_hour is not None and saved.five_hour.remaining == 80
    assert saved.weekly is not None and saved.weekly.remaining == 60


def test_ingest_rejects_browser_payload_when_current_error_has_recent_auth_snapshot(
    tmp_path,
):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    captured = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=captured,
            status=AccountStatus.OK,
            backend_configured="direct",
            backend_used="direct",
            backend_user_id="browser-user",
            backend_account_id="browser-account",
            five_hour=LimitWindow(name="5h", remaining=80),
            weekly=LimitWindow(name="weekly", remaining=60),
        ),
        snapshot_dir,
    )
    save_current_usage(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime(2026, 6, 8, 4, 21, tzinfo=ZoneInfo("Europe/Berlin")),
            status=AccountStatus.ERROR,
            error="direct fetch failed",
            backend_configured="direct",
            backend_used="direct",
            backend_user_id="browser-user",
            backend_account_id="browser-account",
        ),
        snapshot_dir.parent / "current",
    )
    payload = {
        "url": "https://chatgpt.com/codex/cloud/settings/analytics",
        "capturedAt": "2026-06-08T04:22:00+02:00",
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "browser-user",
                        "account_id": "browser-account",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 35,
                                "limit_window_seconds": 18000,
                            },
                            "secondary_window": {
                                "used_percent": 55,
                                "limit_window_seconds": 604800,
                            },
                        },
                    }
                ),
            }
        ],
    }

    with pytest.raises(ValueError, match="current authenticated state"):
        ingest_and_save(
            config,
            "privat",
            payload,
            snapshot_dir,
            require_backend_identity=True,
        )

    saved_snapshot = load_usage_snapshot("privat", snapshot_dir)
    saved_current = load_current_usage("privat", snapshot_dir.parent / "current")
    assert saved_snapshot is not None
    assert saved_snapshot.backend_used == "direct"
    assert saved_snapshot.five_hour is not None
    assert saved_snapshot.five_hour.remaining == 80
    assert saved_current is not None
    assert saved_current.status == AccountStatus.ERROR


def test_ingest_uses_newer_current_identity_than_old_snapshot(tmp_path):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
            five_hour=LimitWindow(name="5h", remaining=97),
            weekly=LimitWindow(name="weekly", remaining=55),
            backend_user_id="old-user",
            backend_account_id="old-account",
        ),
        snapshot_dir,
    )
    save_current_usage(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime(2026, 6, 8, 4, 30, tzinfo=ZoneInfo("Europe/Berlin")),
            five_hour=LimitWindow(name="5h", remaining=96),
            weekly=LimitWindow(name="weekly", remaining=54),
            backend_user_id="new-user",
            backend_account_id="new-account",
        ),
        snapshot_dir.parent / "current",
    )
    payload = {
        "url": "https://chatgpt.com/codex/cloud/settings/analytics",
        "capturedAt": "2026-06-08T04:35:00+02:00",
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "new-user",
                        "account_id": "new-account",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 5,
                                "limit_window_seconds": 18000,
                            },
                            "secondary_window": {
                                "used_percent": 47,
                                "limit_window_seconds": 604800,
                            },
                        },
                    }
                ),
            }
        ],
    }

    usage, _path = ingest_and_save(
        config,
        "privat",
        payload,
        snapshot_dir,
        require_backend_identity=True,
    )

    assert usage.backend_account_id == "new-account"


def test_ingest_rejects_payload_older_than_newer_current_state(tmp_path):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    timezone = ZoneInfo("Europe/Berlin")
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=timezone),
            five_hour=LimitWindow(name="5h", remaining=97),
            weekly=LimitWindow(name="weekly", remaining=55),
            backend_user_id="browser-user",
            backend_account_id="browser-account",
        ),
        snapshot_dir,
    )
    save_current_usage(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime(2026, 6, 8, 4, 40, tzinfo=timezone),
            five_hour=LimitWindow(name="5h", remaining=80),
            weekly=LimitWindow(name="weekly", remaining=40),
            backend_user_id="browser-user",
            backend_account_id="browser-account",
        ),
        snapshot_dir.parent / "current",
    )

    with pytest.raises(ValueError, match="older than known state"):
        ingest_and_save(
            config,
            "privat",
            {
                "capturedAt": "2026-06-08T04:30:00+02:00",
                "apiResponses": [
                    {
                        "url": "https://chatgpt.com/backend-api/wham/usage",
                        "status": 200,
                        "contentType": "application/json",
                        "ok": True,
                        "truncated": False,
                        "bodyText": json.dumps(
                            {
                                "user_id": "browser-user",
                                "account_id": "browser-account",
                                "rate_limit": {
                                    "primary_window": {
                                        "used_percent": 10,
                                        "limit_window_seconds": 18000,
                                    },
                                    "secondary_window": {
                                        "used_percent": 20,
                                        "limit_window_seconds": 604800,
                                    },
                                },
                            }
                        ),
                    }
                ],
            },
            snapshot_dir,
        )

    stored_snapshot = load_usage_snapshot("privat", snapshot_dir)
    stored_current = load_current_usage("privat", snapshot_dir.parent / "current")
    assert stored_snapshot is not None
    assert stored_current is not None
    assert stored_snapshot.captured_at == datetime(2026, 6, 8, 4, 20, tzinfo=timezone)
    assert stored_current.captured_at == datetime(2026, 6, 8, 4, 40, tzinfo=timezone)


def test_bridge_revalidates_auth_identity_before_saving(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "access-token",
                    "id_token": _jwt_with_claims(
                        {"https://api.openai.com/auth": {"chatgpt_user_id": "new-user"}}
                    ),
                    "account_id": "new-account",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    identities = iter(
        (
            ("old-user", "old-account"),
            ("new-user", "new-account"),
        )
    )
    monkeypatch.setattr(
        "codex_usage.bridge.auth_identity_for_account",
        lambda _account: next(identities),
    )
    payload = {
        "url": "https://chatgpt.com/codex/cloud/settings/analytics",
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "old-user",
                        "account_id": "old-account",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 3,
                                "limit_window_seconds": 18000,
                            },
                            "secondary_window": {
                                "used_percent": 45,
                                "limit_window_seconds": 604800,
                            },
                        },
                    }
                ),
            }
        ],
    }

    with pytest.raises(ValueError, match="different backend account"):
        ingest_and_save(
            config,
            "privat",
            payload,
            snapshot_dir,
            require_backend_identity=True,
        )

    assert load_usage_snapshot("privat", snapshot_dir) is None


def test_latest_rejects_cached_values_after_auth_identity_changes(tmp_path):
    auth_path = tmp_path / "auth.json"

    def write_auth(user_id: str, account_id: str) -> None:
        auth_path.write_text(
            json.dumps(
                {
                    "tokens": {
                        "id_token": _jwt_with_claims(
                            {
                                "https://api.openai.com/auth": {
                                    "chatgpt_user_id": user_id,
                                }
                            }
                        ),
                        "account_id": account_id,
                    }
                }
            ),
            encoding="utf-8",
        )
        auth_path.chmod(0o600)

    write_auth("old-user", "old-account")
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
    )
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    cached = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime.now().astimezone(),
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", remaining=12),
        weekly=LimitWindow(name="weekly", remaining=34),
        backend_user_id="old-user",
        backend_account_id="old-account",
    )
    save_usage_snapshot(cached, snapshot_dir)
    save_current_usage(cached, snapshot_dir.parent / "current")

    matching = load_latest_usages(config, snapshot_dir)
    assert len(matching) == 1
    assert matching[0].five_hour is not None
    assert matching[0].five_hour.remaining == 12

    write_auth("new-user", "new-account")

    invalidated = load_latest_usages(config, snapshot_dir)
    assert len(invalidated) == 1
    assert invalidated[0].cache_invalidated is True
    assert invalidated[0].five_hour is None
    assert invalidated[0].weekly is None


def test_latest_rejects_identity_free_dynamic_cached_values(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": _jwt_with_claims(
                        {
                            "https://api.openai.com/auth": {
                                "chatgpt_user_id": "current-user",
                            }
                        }
                    ),
                    "account_id": "current-account",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        backend="direct",
        auth_json_path=str(auth_path),
    )
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime.now().astimezone(),
            status=AccountStatus.OK,
            backend_configured="direct",
            backend_used="direct",
            main=UsagePool(
                key="main",
                display_name="Codex",
                windows=(LimitWindow(name="weekly", remaining=72),),
            ),
        ),
        snapshot_dir,
    )

    result = load_latest_usages(config, snapshot_dir)

    assert len(result) == 1
    assert result[0].cache_invalidated is True
    assert result[0].five_hour is None
    assert result[0].weekly is None
    assert result[0].main is None
    assert result[0].models == ()


def test_latest_discards_foreign_browser_values_with_shared_user_id(tmp_path):
    auth_path = tmp_path / "privat-auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": _jwt_with_claims(
                        {
                            "https://api.openai.com/auth": {
                                "chatgpt_user_id": "shared-user",
                                "chatgpt_plan_type": "free",
                            }
                        }
                    ),
                    "account_id": "private-account",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        backend="direct",
        auth_json_path=str(auth_path),
    )
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    foreign_browser = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime.now().astimezone(),
        status=AccountStatus.OK,
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
        backend_configured="direct",
        backend_used="browser",
        backend_user_id="shared-user",
        backend_account_id="enterprise-account",
    )
    save_usage_snapshot(foreign_browser, snapshot_dir)
    save_current_usage(foreign_browser, snapshot_dir.parent / "current")

    latest = load_latest_usages(config, snapshot_dir)

    assert len(latest) == 1
    assert latest[0].cache_invalidated is True
    assert latest[0].backend_used == "direct"
    assert latest[0].five_hour is None
    assert latest[0].weekly is None


def test_latest_rejects_cache_when_auth_identity_changes_during_read(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    cached = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime.now().astimezone(),
        backend_configured="direct",
        backend_used="direct",
        five_hour=LimitWindow(name="5h", remaining=12),
        weekly=LimitWindow(name="weekly", remaining=34),
        backend_user_id="old-user",
        backend_account_id="old-account",
    )
    save_usage_snapshot(cached, snapshot_dir)
    save_current_usage(cached, snapshot_dir.parent / "current")
    identities = iter(
        (
            ("old-user", "old-account"),
            ("new-user", "new-account"),
        )
    )
    monkeypatch.setattr(
        "codex_usage.bridge.auth_identity_for_account",
        lambda _account: next(identities),
    )

    invalidated = load_latest_usages(config, snapshot_dir)

    assert len(invalidated) == 1
    assert invalidated[0].cache_invalidated is True
    assert invalidated[0].five_hour is None
    assert invalidated[0].weekly is None


@pytest.mark.parametrize(
    ("snapshot_age", "expected_backend", "expected_remaining"),
    [
        (timedelta(seconds=30), "direct", 80),
        (timedelta(minutes=10), "browser", 10),
    ],
)
def test_latest_does_not_let_legacy_browser_current_hide_auth_snapshot(
    tmp_path,
    snapshot_age,
    expected_backend,
    expected_remaining,
):
    now = datetime.now().astimezone()
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        backend="direct",
    )
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    authenticated = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=now - snapshot_age,
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user-privat",
        backend_account_id="account-privat",
        five_hour=LimitWindow(name="5h", remaining=80),
        weekly=LimitWindow(name="weekly", remaining=60),
    )
    browser = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=now,
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="browser",
        backend_user_id="user-privat",
        backend_account_id="account-privat",
        five_hour=LimitWindow(name="5h", remaining=10),
        weekly=LimitWindow(name="weekly", remaining=20),
    )
    save_usage_snapshot(authenticated, snapshot_dir)
    save_current_usage(browser, snapshot_dir.parent / "current")

    result = load_latest_usages(config, snapshot_dir)

    assert len(result) == 1
    assert result[0].backend_used == expected_backend
    assert result[0].five_hour is not None
    assert result[0].five_hour.remaining == expected_remaining


def test_latest_prefers_fresh_authenticated_block_over_browser_current(tmp_path):
    now = datetime.now().astimezone()
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        backend="direct",
    )
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    blocked = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=now - timedelta(seconds=30),
        status=AccountStatus.BLOCKED,
        error="usage limit reached",
        blocked_until=now + timedelta(hours=1),
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user-privat",
        backend_account_id="account-privat",
    )
    browser = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=now,
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="browser",
        backend_user_id="user-privat",
        backend_account_id="account-privat",
        five_hour=LimitWindow(name="5h", remaining=10),
        weekly=LimitWindow(name="weekly", remaining=20),
    )
    save_usage_snapshot(blocked, snapshot_dir)
    save_current_usage(browser, snapshot_dir.parent / "current")

    result = load_latest_usages(config, snapshot_dir)

    assert len(result) == 1
    assert result[0].backend_used == "direct"
    assert result[0].status == AccountStatus.BLOCKED
    assert result[0].five_hour is None
    assert result[0].weekly is None


def test_latest_rejects_browser_cache_for_authenticated_direct_account(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": _jwt_with_claims(
                        {
                            "https://api.openai.com/auth": {
                                "chatgpt_user_id": "user-privat",
                            }
                        }
                    ),
                    "account_id": "account-privat",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        backend="direct",
        auth_json_path=str(auth_path),
    )
    config = AppConfig(accounts=(account,))
    now = datetime.now().astimezone()
    browser = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=now,
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="browser",
        backend_user_id="user-privat",
        backend_account_id="account-privat",
        five_hour=LimitWindow(name="5h", remaining=10),
        weekly=LimitWindow(name="weekly", remaining=20),
    )
    snapshot_dir = tmp_path / "snapshots"
    save_usage_snapshot(browser, snapshot_dir)
    save_current_usage(browser, snapshot_dir.parent / "current")

    result = load_latest_usages(config, snapshot_dir)

    assert len(result) == 1
    assert result[0].backend_used == "direct"
    assert result[0].cache_invalidated is True
    assert result[0].five_hour is None
    assert result[0].weekly is None
    assert result[0].error == "cached browser usage ignored for configured direct backend"


@pytest.mark.parametrize("snapshot_window", ["weekly", None])
def test_latest_does_not_let_browser_current_hide_fresh_authenticated_partial(
    tmp_path,
    snapshot_window,
):
    now = datetime.now().astimezone()
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        backend="direct",
    )
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    authenticated = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=now - timedelta(seconds=30),
        status=AccountStatus.PARTIAL,
        error="weekly limit unavailable" if snapshot_window else "requested limits unavailable",
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user-privat",
        backend_account_id="account-privat",
        weekly=(
            LimitWindow(name="weekly", remaining=83)
            if snapshot_window == "weekly"
            else None
        ),
    )
    browser = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=now,
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="browser",
        backend_user_id="user-privat",
        backend_account_id="account-privat",
        five_hour=LimitWindow(name="5h", remaining=97),
        weekly=LimitWindow(name="weekly", remaining=55),
    )
    save_usage_snapshot(authenticated, snapshot_dir)
    save_current_usage(browser, snapshot_dir.parent / "current")

    result = load_latest_usages(config, snapshot_dir)

    assert len(result) == 1
    assert result[0].backend_used == "direct"
    assert result[0].five_hour is None
    if snapshot_window == "weekly":
        assert result[0].weekly is not None
        assert result[0].weekly.remaining == 83
    else:
        assert result[0].weekly is None


def test_latest_accepts_fresh_authenticated_partial_with_restored_reset(
    tmp_path,
):
    now = datetime.now().astimezone()
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        backend="direct",
    )
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    identity = {
        "backend_configured": "direct",
        "backend_used": "direct",
        "backend_user_id": "user-privat",
        "backend_account_id": "account-privat",
    }
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=now - timedelta(minutes=1),
            status=AccountStatus.OK,
            five_hour=LimitWindow(
                name="5h",
                remaining=80,
                reset_at=now + timedelta(hours=4),
            ),
            weekly=LimitWindow(
                name="weekly",
                remaining=60,
                reset_at=now + timedelta(days=6),
            ),
            **identity,
        ),
        snapshot_dir,
    )
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=now - timedelta(seconds=30),
            status=AccountStatus.PARTIAL,
            error="5h limit unavailable",
            weekly=LimitWindow(name="weekly", remaining=59),
            **identity,
        ),
        snapshot_dir,
    )
    cached = load_usage_snapshot("privat", snapshot_dir)
    assert cached is not None and cached.stale is True
    save_current_usage(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=now,
            status=AccountStatus.OK,
            backend_configured="direct",
            backend_used="browser",
            backend_user_id="user-privat",
            backend_account_id="account-privat",
            five_hour=LimitWindow(name="5h", remaining=97),
            weekly=LimitWindow(name="weekly", remaining=55),
        ),
        snapshot_dir.parent / "current",
    )

    result = load_latest_usages(config, snapshot_dir)

    assert len(result) == 1
    assert result[0].backend_used == "direct"
    assert result[0].five_hour is None
    assert result[0].weekly is not None
    assert result[0].weekly.remaining == 59


def test_latest_keeps_browser_when_authenticated_partial_values_are_stale(
    tmp_path,
):
    now = datetime.now().astimezone()
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        backend="direct",
    )
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    identity = {
        "backend_configured": "direct",
        "backend_used": "direct",
        "backend_user_id": "user-privat",
        "backend_account_id": "account-privat",
    }
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=now - timedelta(minutes=10),
            status=AccountStatus.OK,
            weekly=LimitWindow(
                name="weekly",
                remaining=60,
                reset_at=now + timedelta(days=6),
            ),
            **identity,
        ),
        snapshot_dir,
    )
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=now,
            status=AccountStatus.PARTIAL,
            error="5h limit unavailable",
            weekly=LimitWindow(name="weekly", remaining=59),
            **identity,
        ),
        snapshot_dir,
    )
    cached = load_usage_snapshot("privat", snapshot_dir)
    assert cached is not None and cached.stale is True
    save_current_usage(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=now,
            status=AccountStatus.OK,
            backend_configured="direct",
            backend_used="browser",
            backend_user_id="user-privat",
            backend_account_id="account-privat",
            five_hour=LimitWindow(name="5h", remaining=97),
            weekly=LimitWindow(name="weekly", remaining=55),
        ),
        snapshot_dir.parent / "current",
    )

    result = load_latest_usages(config, snapshot_dir)

    assert len(result) == 1
    assert result[0].backend_used == "browser"
    assert result[0].five_hour is not None
    assert result[0].five_hour.remaining == 97
    assert result[0].weekly is not None
    assert result[0].weekly.remaining == 55


def test_latest_discards_far_future_current_and_keeps_valid_snapshot(tmp_path):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        backend="browser",
    )
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    captured_at = datetime.now().astimezone()
    valid = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured_at,
        status=AccountStatus.OK,
        backend_configured="browser",
        backend_used="browser",
        five_hour=LimitWindow(name="5h", remaining=80),
        weekly=LimitWindow(name="weekly", remaining=60),
    )
    future = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured_at + timedelta(hours=1),
        status=AccountStatus.OK,
        backend_configured="browser",
        backend_used="browser",
        five_hour=LimitWindow(name="5h", remaining=1),
        weekly=LimitWindow(name="weekly", remaining=2),
    )

    save_usage_snapshot(valid, snapshot_dir)
    save_current_usage(future, snapshot_dir.parent / "current")

    result = load_latest_usages(config, snapshot_dir)

    assert len(result) == 1
    assert result[0].captured_at == valid.captured_at
    assert result[0].five_hour is not None
    assert result[0].five_hour.remaining == 80


def test_latest_uses_current_account_label_for_cached_usage(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Old Label",
            captured_at=datetime.now().astimezone(),
            backend_configured="direct",
            backend_used="direct",
            five_hour=LimitWindow(name="5h", remaining=80),
            weekly=LimitWindow(name="weekly", remaining=60),
        ),
        snapshot_dir,
    )
    config = AppConfig(
        accounts=(
            Account(
                id="privat",
                label="New Label",
                profile_dir=str(tmp_path / "profile"),
            ),
        )
    )

    result = load_latest_usages(config, snapshot_dir)

    assert len(result) == 1
    assert result[0].label == "New Label"
    assert result[0].weekly is not None
    assert result[0].weekly.remaining == 60


def test_latest_rejects_cached_usage_without_backend_provenance(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime.now().astimezone(),
            status=AccountStatus.OK,
            five_hour=LimitWindow(name="5h", remaining=80),
            weekly=LimitWindow(name="weekly", remaining=60),
        ),
        snapshot_dir,
    )
    config = AppConfig(
        accounts=(
            Account(
                id="privat",
                label="Privat",
                profile_dir=str(tmp_path / "profile"),
            ),
        )
    )

    assert load_latest_usages(config, snapshot_dir) == []


def test_usage_from_ingest_payload_clamps_far_future_capture_time():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    before = datetime.now().astimezone()
    usage = usage_from_ingest_payload(
        account,
        {
            "capturedAt": (before + timedelta(hours=1)).isoformat(),
            "bodyText": "".join(
                (
                    "5-hour limit 42 / 100 Reset 08.06.2026 04:26 ",
                    "Weekly limit 310 / 1000 Reset 14.06.2026 04:26",
                )
            ),
        },
    )
    after = datetime.now().astimezone()

    assert before <= usage.captured_at <= after


def test_usage_from_ingest_payload_marks_reset_only_windows_partial():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "bodyText": """
            5 Stunden Nutzungsgrenze
            Zurücksetzungen 08.06.2026 04:26
            Wöchentliches Nutzungslimit
            Zurücksetzungen 14.06.2026 04:26
            """,
        },
    )

    assert usage.status == AccountStatus.PARTIAL


def test_usage_from_ingest_payload_marks_limit_only_windows_partial():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "capturedAt": "2026-07-12T11:00:00+02:00",
            "jsonResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/other",
                    "status": 200,
                    "bodyText": json.dumps(
                        {
                            "five_hour": {
                                "limit": 100,
                                "reset_at": "2026-07-12T14:00:00+02:00",
                            },
                            "weekly": {
                                "limit": 100,
                                "reset_at": "2026-07-18T08:00:00+02:00",
                            },
                        }
                    ),
                }
            ],
        },
    )

    assert usage.status == AccountStatus.PARTIAL


def test_usage_from_ingest_payload_uses_full_dom_payload_fields():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "bodyText": "Codex analytics",
            "accessibilityText": """
            5-hour limit 42 / 100 Reset 08.06.2026 04:26
            Weekly limit 310 / 1000 Reset 14.06.2026 04:26
            """,
        },
    )

    assert usage.status == AccountStatus.OK
    assert usage.five_hour is not None
    assert usage.five_hour.used == 42
    assert usage.weekly is not None
    assert usage.weekly.limit == 1000


def test_usage_from_ingest_payload_prefers_visible_values_over_stale_dom_fields():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "bodyText": (
                "5-hour limit 97% remaining Reset 12.07.2026 19:00 "
                "Weekly limit 55% remaining Reset 18.07.2026 08:00"
            ),
            "domText": (
                "5-hour limit 20% remaining Reset 12.07.2026 18:00 "
                "Weekly limit 10% remaining Reset 18.07.2026 07:00"
            ),
        },
    )

    assert usage.five_hour is not None and usage.five_hour.remaining == 97
    assert usage.weekly is not None and usage.weekly.remaining == 55
    assert usage.five_hour.source == "bodyText"


def test_usage_from_ingest_payload_extracts_api_responses():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "bodyText": "Codex analytics",
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage?secret=1",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": json.dumps(
                        {
                            "user_id": "user-test",
                            "account_id": "account-test",
                            "five_hour_usage_limit": {
                                "used": 42,
                                "limit": 100,
                                "reset_at": "2026-06-08T04:26:00+02:00",
                            },
                            "weekly_usage_limit": {
                                "used": 310,
                                "limit": 1000,
                                "reset_at": "2026-06-14T04:26:00+02:00",
                            },
                        }
                    ),
                }
            ],
        },
    )

    assert usage.status == AccountStatus.OK
    assert usage.source_urls == (
        "https://chatgpt.com/backend-api/wham/usage",
        "https://chatgpt.com/codex/cloud/settings/analytics",
    )
    assert usage.five_hour is not None
    assert usage.five_hour.used == 42
    assert usage.weekly is not None
    assert usage.weekly.limit == 1000
    assert usage.backend_user_id == "user-test"
    assert usage.backend_account_id == "account-test"


@pytest.mark.parametrize(
    "response_fields",
    (
        {"status": "403"},
        {"status": 302},
        {"status": "304"},
        {"ok": False},
        {"ok": "false"},
        {"truncated": "true"},
    ),
)
def test_usage_from_ingest_payload_ignores_failed_api_response_status_variants(
    response_fields,
):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    response = {
        "url": "https://chatgpt.com/backend-api/wham/usage",
        "status": 200,
        "contentType": "application/json",
        "ok": True,
        "truncated": False,
        "bodyText": json.dumps(
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 3,
                        "limit_window_seconds": 18_000,
                    },
                    "secondary_window": {
                        "used_percent": 45,
                        "limit_window_seconds": 604_800,
                    },
                }
            }
        ),
        **response_fields,
    }

    usage = usage_from_ingest_payload(account, {"apiResponses": [response]})

    assert usage.status == AccountStatus.PARTIAL
    assert usage.cache_invalidated is True
    assert usage.five_hour is None
    assert usage.weekly is None


@pytest.mark.parametrize("missing_field", ("status", "ok", "truncated", "contentType"))
def test_usage_from_ingest_payload_rejects_missing_api_response_metadata(missing_field):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    response = {
        "url": "https://chatgpt.com/backend-api/wham/usage",
        "status": 200,
        "ok": True,
        "contentType": "application/json",
        "truncated": False,
        "bodyText": json.dumps(
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 3,
                        "limit_window_seconds": 18_000,
                    },
                    "secondary_window": {
                        "used_percent": 45,
                        "limit_window_seconds": 604_800,
                    },
                }
            }
        ),
    }
    response.pop(missing_field)

    usage = usage_from_ingest_payload(account, {"apiResponses": [response]})

    assert usage.status == AccountStatus.PARTIAL
    assert usage.cache_invalidated is True
    assert usage.five_hour is None
    assert usage.weekly is None


@pytest.mark.parametrize(
    "response_fields",
    (
        {"status": 1},
        {"status": "200"},
        {"status": "ok"},
        {"ok": 1},
        {"ok": "true"},
        {"truncated": 1},
        {"truncated": "false"},
        {"truncated": "no"},
        {"contentType": ["application/json"]},
        {"contentType": None},
        {"url": ["https://chatgpt.com/backend-api/wham/usage"]},
    ),
)
def test_usage_from_ingest_payload_rejects_invalid_api_response_metadata(
    response_fields,
):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    response = {
        "url": "https://chatgpt.com/backend-api/wham/usage",
        "status": 200,
        "contentType": "application/json",
        "ok": True,
        "truncated": False,
        "bodyText": json.dumps(
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 3,
                        "limit_window_seconds": 18_000,
                    },
                    "secondary_window": {
                        "used_percent": 45,
                        "limit_window_seconds": 604_800,
                    },
                }
            }
        ),
        **response_fields,
    }

    usage = usage_from_ingest_payload(account, {"apiResponses": [response]})

    assert usage.status == AccountStatus.PARTIAL
    assert usage.cache_invalidated is True
    assert usage.five_hour is None
    assert usage.weekly is None


def test_usage_from_ingest_payload_rejects_invalid_api_response_body_alias():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    valid_body = json.dumps(
        {
            "user_id": "user-test",
            "account_id": "account-test",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 3,
                    "limit_window_seconds": 18_000,
                },
                "secondary_window": {
                    "used_percent": 45,
                    "limit_window_seconds": 604_800,
                },
            },
        }
    )

    usage = usage_from_ingest_payload(
        account,
        {
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": [],
                    "body": valid_body,
                }
            ]
        },
    )

    assert usage.status == AccountStatus.PARTIAL
    assert usage.cache_invalidated is True
    assert usage.five_hour is None
    assert usage.weekly is None


def test_usage_from_ingest_payload_reports_missing_paid_five_hour_window():
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path="/tmp/auth.json",
    )
    payload = {
        "capturedAt": "2026-07-13T03:40:00+02:00",
        "url": "https://chatgpt.com/codex/cloud/settings/analytics",
        "apiResponses": [
            {
                "source": "page-fetch",
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "user-test",
                        "account_id": "account-test",
                        "plan_type": "pro",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 10,
                                "limit_window_seconds": 604800,
                                "reset_at": "2026-07-19T20:59:30+02:00",
                            },
                            "secondary_window": None,
                        },
                    }
                ),
            }
        ],
    }

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            "codex_usage.bridge.auth_identity_for_account",
            lambda _account: ("user-test", "account-test"),
        )
        monkeypatch.setattr(
            "codex_usage.bridge.auth_plan_type_for_account",
            lambda _account: "pro",
        )
        usage = usage_from_ingest_payload(account, payload)
    finally:
        monkeypatch.undo()

    assert usage.status == AccountStatus.PARTIAL
    assert usage.five_hour is None
    assert usage.weekly is not None and usage.weekly.remaining == 90
    assert usage.cache_invalidated is False
    assert usage.error.startswith("missing page text")


def test_usage_from_ingest_payload_merges_both_api_response_field_names():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "bodyText": "Codex analytics",
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/settings/user",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": json.dumps({"user_id": "user-test"}),
                }
            ],
            "api_responses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": json.dumps(
                        {
                            "user_id": "user-test",
                            "account_id": "account-test",
                            "rate_limit": {
                                "primary_window": {
                                    "used_percent": 3,
                                    "limit_window_seconds": 18000,
                                },
                                "secondary_window": {
                                    "used_percent": 45,
                                    "limit_window_seconds": 604800,
                                },
                            },
                        }
                    ),
                }
            ],
        },
    )

    assert usage.status == AccountStatus.OK
    assert usage.five_hour is not None and usage.five_hour.remaining == 97
    assert usage.weekly is not None and usage.weekly.remaining == 55


def test_usage_from_ingest_payload_does_not_mix_identity_json_with_dom_values(
    tmp_path,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": _jwt_with_claims(
                        {
                            "https://api.openai.com/auth": {
                                "chatgpt_user_id": "user-test",
                                "chatgpt_account_id": "account-test",
                            }
                        }
                    )
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
    )

    usage = usage_from_ingest_payload(
        account,
        {
            "bodyText": (
                "5-hour limit 42 / 100 Reset 12.07.2026 16:00 "
                "Weekly limit 310 / 1000 Reset 18.07.2026 08:00"
            ),
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/settings/user",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": json.dumps({"user_id": "user-test"}),
                }
            ],
        },
    )

    assert usage.status == AccountStatus.PARTIAL
    assert usage.five_hour is None
    assert usage.weekly is None


def test_usage_from_ingest_payload_uses_dom_for_confirmed_identity_without_limits(
    tmp_path,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": _jwt_with_claims(
                        {
                            "https://api.openai.com/auth": {
                                "chatgpt_user_id": "user-test",
                                "chatgpt_account_id": "account-test",
                            }
                        }
                    ),
                    "account_id": "account-test",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
    )

    usage = usage_from_ingest_payload(
        account,
        {
            "bodyText": (
                "5-hour limit 97% remaining Reset 12.07.2026 16:00 "
                "Weekly limit 55% remaining Reset 18.07.2026 08:00"
            ),
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/settings/user",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": json.dumps(
                        {"user_id": "user-test", "account_id": "account-test"}
                    ),
                }
            ],
        },
    )

    assert usage.status == AccountStatus.OK
    assert usage.backend_account_id == "account-test"
    assert usage.five_hour is not None and usage.five_hour.remaining == 97
    assert usage.weekly is not None and usage.weekly.remaining == 55


def test_usage_from_ingest_payload_fills_missing_window_from_dom_for_confirmed_identity(
    tmp_path,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": _jwt_with_claims(
                        {
                            "https://api.openai.com/auth": {
                                "chatgpt_user_id": "user-test",
                                "chatgpt_account_id": "account-test",
                            }
                        }
                    ),
                    "account_id": "account-test",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
    )

    usage = usage_from_ingest_payload(
        account,
        {
            "bodyText": (
                "5-hour limit 97% remaining Reset 12.07.2026 16:00 "
                "Weekly limit 55% remaining Reset 18.07.2026 08:00"
            ),
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": json.dumps(
                        {
                            "user_id": "user-test",
                            "account_id": "account-test",
                            "rate_limit": {
                                "primary_window": {
                                    "used_percent": 45,
                                    "limit_window_seconds": 604800,
                                },
                                "secondary_window": None,
                            },
                        }
                    ),
                }
            ],
        },
    )

    assert usage.status == AccountStatus.OK
    assert usage.five_hour is not None and usage.five_hour.remaining == 97
    assert usage.weekly is not None and usage.weekly.remaining == 55
    assert usage.weekly.used == 45


def test_usage_from_ingest_payload_rejects_user_id_as_account_id_for_dom_fallback(
    tmp_path,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": _jwt_with_claims(
                        {
                            "https://api.openai.com/auth": {
                                "chatgpt_user_id": "user-test",
                                "chatgpt_account_id": "account-test",
                            }
                        }
                    ),
                    "account_id": "account-test",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
    )

    with pytest.raises(ValueError, match="ambiguous account identity"):
        usage_from_ingest_payload(
            account,
            {
                "bodyText": (
                    "5-hour limit 97% remaining Reset 12.07.2026 16:00 "
                    "Weekly limit 55% remaining Reset 18.07.2026 08:00"
                ),
                "apiResponses": [
                    {
                        "url": "https://chatgpt.com/backend-api/wham/usage",
                        "status": 200,
                        "contentType": "application/json",
                        "ok": True,
                        "truncated": False,
                        "bodyText": json.dumps(
                            {
                                "user_id": "user-test",
                                "account_id": "user-test",
                                "rate_limit": {
                                    "primary_window": None,
                                    "secondary_window": {
                                        "used_percent": 17,
                                        "limit_window_seconds": 604800,
                                    },
                                },
                            }
                        ),
                    }
                ],
            },
        )


def test_ingest_rejects_limit_values_without_backend_account_id(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": _jwt_with_claims(
                        {
                            "https://api.openai.com/auth": {
                                "chatgpt_user_id": "shared-user",
                                "chatgpt_account_id": "account-a",
                            }
                        }
                    ),
                    "account_id": "account-a",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
    )
    config = AppConfig(accounts=(account,))
    payload = {
        "bodyText": "5-hour limit 97% remaining Weekly limit 55% remaining",
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "shared-user",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 3,
                                "limit_window_seconds": 18000,
                            },
                            "secondary_window": {
                                "used_percent": 45,
                                "limit_window_seconds": 604800,
                            },
                        },
                    }
                ),
            }
        ],
    }

    with pytest.raises(ValueError, match="ambiguous account identity"):
        ingest_and_save(
            config,
            "privat",
            payload,
            tmp_path / "snapshots",
            require_backend_identity=True,
        )

    assert load_usage_snapshot("privat", tmp_path / "snapshots") is None


def test_usage_from_ingest_payload_keeps_probe_after_failed_page_hook_response():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "bodyText": "Codex analytics",
            "apiResponses": [
                {
                    "source": "page-fetch",
                    "requestSequence": 7,
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 401,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": json.dumps({"detail": "Unauthorized"}),
                },
                {
                    "source": "content-probe",
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": json.dumps(
                        {
                            "user_id": "user-test",
                            "account_id": "account-test",
                            "rate_limit": {
                                "primary_window": {
                                    "used_percent": 3,
                                    "limit_window_seconds": 18000,
                                },
                                "secondary_window": {
                                    "used_percent": 45,
                                    "limit_window_seconds": 604800,
                                },
                            },
                        }
                    ),
                },
            ],
        },
    )

    assert usage.status == AccountStatus.OK
    assert usage.five_hour is not None and usage.five_hour.remaining == 97
    assert usage.weekly is not None and usage.weekly.remaining == 55


def test_usage_from_ingest_payload_ignores_truncated_json_api_responses():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "bodyText": "Codex analytics",
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": True,
                    "bodyText": json.dumps(
                        {
                            "user_id": "user-test",
                            "account_id": "account-test",
                            "rate_limit": {
                                "primary_window": {
                                    "used_percent": 3,
                                    "limit_window_seconds": 18000,
                                },
                                "secondary_window": {
                                    "used_percent": 45,
                                    "limit_window_seconds": 604800,
                                },
                            },
                        }
                    ),
                }
            ],
        },
    )

    assert usage.status == AccountStatus.PARTIAL
    assert usage.five_hour is None
    assert usage.weekly is None


def test_usage_from_ingest_payload_prefers_latest_response_for_endpoint():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")

    def response(five_hour: int, weekly: int, sequence: int) -> dict[str, object]:
        return {
            "url": "https://chatgpt.com/backend-api/wham/usage?cache=refresh",
            "status": 200,
            "contentType": "application/json",
            "ok": True,
            "truncated": False,
            "requestSequence": sequence,
            "bodyText": json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": five_hour,
                            "limit_window_seconds": 18000,
                        },
                        "secondary_window": {
                            "used_percent": weekly,
                            "limit_window_seconds": 604800,
                        },
                    }
                }
            ),
        }

    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "bodyText": "Codex analytics",
            "apiResponses": [response(20, 60, 2), response(3, 45, 1)],
        },
    )

    assert usage.status == AccountStatus.OK
    assert usage.five_hour is not None
    assert usage.five_hour.used == 20
    assert usage.weekly is not None
    assert usage.weekly.used == 60


def test_usage_from_ingest_payload_rejects_conflicting_equal_sequence_responses():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")

    def response(five_hour: int) -> dict[str, object]:
        return {
            "url": "https://chatgpt.com/backend-api/wham/usage",
            "status": 200,
            "contentType": "application/json",
            "ok": True,
            "truncated": False,
            "requestSequence": 1,
            "bodyText": json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": five_hour,
                            "limit_window_seconds": 18_000,
                        },
                        "secondary_window": {
                            "used_percent": 45,
                            "limit_window_seconds": 604_800,
                        },
                    }
                }
            ),
        }

    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "bodyText": "Codex analytics",
            "apiResponses": [response(3), response(45)],
        },
    )

    assert usage.status == AccountStatus.PARTIAL
    assert usage.cache_invalidated is True
    assert usage.five_hour is None
    assert usage.weekly is None


def test_json_candidates_keep_each_response_url():
    body = json.dumps({"usage": {}})
    candidates = _json_candidates_from_payload(
        {
            "apiResponses": [
                {
                    "url": "https://example.test/first",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": body,
                },
                {
                    "url": "https://example.test/second",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": body,
                },
            ]
        }
    )

    assert [candidate.url for candidate in candidates] == [
        "https://example.test/first",
        "https://example.test/second",
    ]


def test_json_candidates_reject_oversized_combined_response_collection():
    responses = [
        {
            "url": f"https://example.test/usage-{index}",
            "status": 200,
            "contentType": "application/json",
            "ok": True,
            "truncated": False,
            "bodyText": json.dumps({"usage": {}}),
        }
        for index in range(bridge_module.MAX_BRIDGE_API_RESPONSES + 1)
    ]

    assert _json_candidates_from_payload(
        {
            "apiResponses": responses[: bridge_module.MAX_BRIDGE_API_RESPONSES // 2],
            "api_responses": responses[bridge_module.MAX_BRIDGE_API_RESPONSES // 2 :],
        }
    ) == []


@pytest.mark.parametrize(
    "request_sequence",
    (None, True, False, -1, 1.5, "1", "", "invalid", [], {}),
)
def test_json_candidates_reject_malformed_request_sequence(request_sequence):
    candidates = _json_candidates_from_payload(
        {
            "apiResponses": [
                {
                    "url": "https://example.test/usage",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "requestSequence": request_sequence,
                    "bodyText": json.dumps({"usage": {}}),
                }
            ]
        }
    )

    assert candidates == []


@pytest.mark.parametrize("source", [None, False, 0, [], {}])
def test_json_candidates_reject_malformed_response_source(source):
    candidates = _json_candidates_from_payload(
        {
            "apiResponses": [
                {
                    "source": source,
                    "url": "https://example.test/usage",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": json.dumps({"usage": {}}),
                }
            ]
        }
    )

    assert candidates == []


def test_json_candidates_reject_unknown_response_source():
    candidates = _json_candidates_from_payload(
        {
            "apiResponses": [
                {
                    "source": "untrusted-hook",
                    "url": "https://example.test/usage",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": json.dumps({"usage": {}}),
                }
            ]
        }
    )

    assert candidates == []


def test_usage_from_ingest_payload_prefers_latest_response_across_sources():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")

    def response(source: str, five_hour: int, weekly: int, sequence: int) -> dict[str, object]:
        return {
            "source": source,
            "url": "https://chatgpt.com/backend-api/wham/usage",
            "status": 200,
            "contentType": "application/json",
            "ok": True,
            "truncated": False,
            "requestSequence": sequence,
            "bodyText": json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": five_hour,
                            "limit_window_seconds": 18000,
                        },
                        "secondary_window": {
                            "used_percent": weekly,
                            "limit_window_seconds": 604800,
                        },
                    }
                }
            ),
        }

    responses = [
        response("page-fetch", 20, 60, 11),
        response("page-hook", 3, 45, 10),
    ]
    for api_responses in (responses, list(reversed(responses))):
        usage = usage_from_ingest_payload(
            account,
            {
                "url": "https://chatgpt.com/codex/cloud/settings/analytics",
                "bodyText": "Codex analytics",
                "apiResponses": api_responses,
            },
        )

        assert usage.status == AccountStatus.OK
        assert usage.five_hour is not None
        assert usage.five_hour.used == 20
        assert usage.weekly is not None
        assert usage.weekly.used == 60


def test_usage_from_ingest_payload_rejects_mixed_backend_identities():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    payload = {
        "apiResponses": [
            {
                "source": "page-fetch",
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "user-a",
                        "account_id": "account-a",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 3,
                                "limit_window_seconds": 18000,
                            }
                        },
                    }
                ),
            },
            {
                "source": "content-probe",
                "url": "https://chatgpt.com/backend-api/wham/usage/daily-token-usage-breakdown",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "user-b",
                        "account_id": "account-b",
                        "rate_limit": {
                            "secondary_window": {
                                "used_percent": 45,
                                "limit_window_seconds": 604800,
                            }
                        },
                    }
                ),
            },
        ]
    }

    with pytest.raises(ValueError, match="multiple backend accounts"):
        usage_from_ingest_payload(account, payload)


def test_usage_from_ingest_payload_rejects_disjoint_partial_identities():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    payload = {
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "user-a",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 3,
                                "limit_window_seconds": 18000,
                            }
                        },
                    }
                ),
            },
            {
                "url": "https://chatgpt.com/backend-api/wham/usage/daily-token-usage-breakdown",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "account_id": "account-b",
                        "rate_limit": {
                            "secondary_window": {
                                "used_percent": 45,
                                "limit_window_seconds": 604800,
                            }
                        },
                    }
                ),
            },
        ]
    }

    with pytest.raises(ValueError, match="multiple backend accounts"):
        usage_from_ingest_payload(account, payload)


def test_usage_from_ingest_payload_prefers_configured_identity_when_candidates_mix(
    tmp_path,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": _jwt_with_claims(
                        {
                            "https://api.openai.com/auth": {
                                "chatgpt_user_id": "user-a",
                                "chatgpt_account_id": "account-a",
                            }
                        }
                    ),
                    "account_id": "account-a",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )
    payload = {
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "user-a",
                        "account_id": "account-a",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 3,
                                "limit_window_seconds": 18000,
                            },
                            "secondary_window": {
                                "used_percent": 45,
                                "limit_window_seconds": 604800,
                            },
                        },
                    }
                ),
            },
            {
                "url": "https://chatgpt.com/backend-api/wham/usage/daily-token-usage-breakdown",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "user-b",
                        "account_id": "account-b",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 90,
                                "limit_window_seconds": 18000,
                            },
                            "secondary_window": {
                                "used_percent": 95,
                                "limit_window_seconds": 604800,
                            },
                        },
                    }
                ),
            },
        ]
    }

    usage = usage_from_ingest_payload(account, payload)

    assert usage.status == AccountStatus.OK
    assert usage.backend_account_id == "account-a"
    assert usage.five_hour is not None and usage.five_hour.remaining == 97
    assert usage.weekly is not None and usage.weekly.remaining == 55


def test_usage_from_ingest_payload_drops_old_success_after_latest_failed_response():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "bodyText": "Codex analytics",
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": json.dumps(
                        {
                            "rate_limit": {
                                "primary_window": {
                                    "used_percent": 3,
                                    "limit_window_seconds": 18000,
                                },
                                "secondary_window": {
                                    "used_percent": 45,
                                    "limit_window_seconds": 604800,
                                },
                            }
                        }
                    ),
                },
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 403,
                    "contentType": "text/html",
                    "bodyText": "Just a moment...",
                },
            ],
        },
    )

    assert usage.status == AccountStatus.PARTIAL
    assert usage.five_hour is None
    assert usage.weekly is None
    assert usage.error is not None
    assert "usage limits not found" in usage.error


def test_usage_from_ingest_payload_rejects_ambiguous_personal_account_identity(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "access-token",
                    "id_token": _jwt_with_claims(
                        {
                            "https://api.openai.com/auth": {
                                "chatgpt_user_id": "user-test",
                                "chatgpt_plan_type": "free",
                            }
                        }
                    ),
                    "account_id": "account-uuid",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
    )

    with pytest.raises(ValueError, match="ambiguous account identity"):
        usage_from_ingest_payload(
            account,
            {
                "url": "https://chatgpt.com/codex/cloud/settings/analytics",
                "apiResponses": [
                    {
                        "url": "https://chatgpt.com/backend-api/wham/usage",
                        "status": 200,
                        "contentType": "application/json",
                        "ok": True,
                        "truncated": False,
                        "bodyText": json.dumps(
                            {
                                "user_id": "user-test",
                                "account_id": "user-test",
                                "plan_type": "free",
                                "rate_limit": {
                                    "primary_window": {
                                        "used_percent": 3,
                                        "limit_window_seconds": 18000,
                                    },
                                    "secondary_window": {
                                        "used_percent": 45,
                                        "limit_window_seconds": 604800,
                                    },
                                },
                            }
                        ),
                    }
                ],
            },
        )


def test_ingest_rejects_ambiguous_shared_user_browser_identity(tmp_path):
    def write_auth(path, account_id):
        path.write_text(
            json.dumps(
                {
                    "tokens": {
                        "id_token": _jwt_with_claims(
                            {
                                "https://api.openai.com/auth": {
                                    "chatgpt_user_id": "shared-user",
                                    "chatgpt_plan_type": "free",
                                }
                            }
                        ),
                        "account_id": account_id,
                    }
                }
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)

    privat_auth = tmp_path / "privat-auth.json"
    work_auth = tmp_path / "work-auth.json"
    write_auth(privat_auth, "free-account")
    write_auth(work_auth, "work-account")
    privat = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "privat-profile"),
        auth_json_path=str(privat_auth),
    )
    work = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "work-profile"),
        auth_json_path=str(work_auth),
    )
    config = AppConfig(accounts=(privat, work))
    payload = {
        "url": "https://chatgpt.com/codex/cloud/settings/analytics",
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "shared-user",
                        "account_id": "shared-user",
                        "plan_type": "free",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 3,
                                "limit_window_seconds": 18000,
                            },
                            "secondary_window": {
                                "used_percent": 45,
                                "limit_window_seconds": 604800,
                            },
                        },
                    }
                ),
            }
        ],
    }

    with pytest.raises(ValueError, match="ambiguous account identity"):
        ingest_and_save(
            config,
            "privat",
            payload,
            tmp_path / "snapshots",
            require_backend_identity=True,
        )

    assert load_usage_snapshot("privat", tmp_path / "snapshots") is None


def test_ingest_rejects_browser_identity_known_for_another_configured_account(tmp_path):
    work_auth = tmp_path / "work-auth.json"
    work_auth.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": _jwt_with_claims(
                        {
                            "https://api.openai.com/auth": {
                                "chatgpt_user_id": "shared-user",
                                "chatgpt_plan_type": "enterprise",
                            }
                        }
                    ),
                    "account_id": "work-account",
                }
            }
        ),
        encoding="utf-8",
    )
    work_auth.chmod(0o600)
    privat = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "privat-profile"),
    )
    work = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "work-profile"),
        auth_json_path=str(work_auth),
    )
    config = AppConfig(accounts=(privat, work))
    payload = {
        "url": "https://chatgpt.com/codex/cloud/settings/analytics",
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "shared-user",
                        "account_id": "work-account",
                        "plan_type": "enterprise",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 3,
                                "limit_window_seconds": 18000,
                            },
                            "secondary_window": {
                                "used_percent": 45,
                                "limit_window_seconds": 604800,
                            },
                        },
                    }
                ),
            }
        ],
    }

    with pytest.raises(ValueError, match="different configured account"):
        ingest_and_save(
            config,
            "privat",
            payload,
            tmp_path / "snapshots",
            require_backend_identity=True,
        )

    assert load_usage_snapshot("privat", tmp_path / "snapshots") is None


def test_usage_from_ingest_payload_rejects_shared_user_response_with_different_plan(
    tmp_path,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "access-token",
                    "id_token": _jwt_with_claims(
                        {
                            "https://api.openai.com/auth": {
                                "chatgpt_user_id": "shared-user",
                                "chatgpt_plan_type": "free",
                            }
                        }
                    ),
                    "account_id": "free-account",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
    )

    with pytest.raises(ValueError, match="different account"):
        usage_from_ingest_payload(
            account,
            {
                "apiResponses": [
                    {
                        "url": "https://chatgpt.com/backend-api/wham/usage",
                        "status": 200,
                        "contentType": "application/json",
                        "ok": True,
                        "truncated": False,
                        "bodyText": json.dumps(
                            {
                                "user_id": "shared-user",
                                "account_id": "shared-user",
                                "plan_type": "enterprise",
                                "rate_limit": {
                                    "primary_window": {
                                        "used_percent": 5,
                                        "limit_window_seconds": 2_592_000,
                                    }
                                },
                            }
                        ),
                    }
                ]
            },
        )


def test_usage_from_ingest_payload_rejects_mismatched_auth_account(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "access-token",
                    "id_token": _jwt_with_claims(
                        {"https://api.openai.com/auth": {"chatgpt_user_id": "user-test"}}
                    ),
                    "account_id": "account-uuid",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
    )

    with pytest.raises(ValueError, match="different account"):
        usage_from_ingest_payload(
            account,
            {
                "apiResponses": [
                    {
                        "url": "https://chatgpt.com/backend-api/wham/usage",
                        "status": 200,
                        "contentType": "application/json",
                        "ok": True,
                        "truncated": False,
                        "bodyText": json.dumps(
                            {
                                "user_id": "user-test",
                                "account_id": "other-account",
                            }
                        ),
                    }
                ]
            },
        )


def test_usage_from_ingest_payload_rejects_auth_values_without_backend_identity(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "access-token",
                    "id_token": _jwt_with_claims(
                        {"https://api.openai.com/auth": {"chatgpt_user_id": "user-test"}}
                    ),
                    "account_id": "account-uuid",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
    )

    with pytest.raises(ValueError, match="no account identity"):
        usage_from_ingest_payload(
            account,
            {
                "bodyText": (
                    "5-hour usage limit 97% remaining "
                    "Weekly usage limit 55% remaining"
                )
            },
        )


def test_ingest_rejects_payload_from_different_backend_account(tmp_path):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
            five_hour=LimitWindow(name="5h", remaining=97),
            weekly=LimitWindow(name="weekly", remaining=55),
            backend_user_id="user-shared",
            backend_account_id="account-current",
        ),
        snapshot_dir,
    )

    with pytest.raises(ValueError, match="different backend account"):
        ingest_and_save(
            config,
            "privat",
            {
                "url": "https://chatgpt.com/codex/cloud/settings/analytics",
                "capturedAt": "2026-06-08T04:25:00+02:00",
                "apiResponses": [
                    {
                        "url": "https://chatgpt.com/backend-api/wham/usage",
                        "status": 200,
                        "contentType": "application/json",
                        "ok": True,
                        "truncated": False,
                        "bodyText": json.dumps(
                            {
                                "user_id": "user-shared",
                                "account_id": "account-other",
                                "rate_limit": {
                                    "primary_window": {
                                        "used_percent": 3,
                                        "limit_window_seconds": 18000,
                                    },
                                    "secondary_window": {
                                        "used_percent": 45,
                                        "limit_window_seconds": 604800,
                                    },
                                },
                            }
                        ),
                    }
                ],
            },
            snapshot_dir,
        )


def test_ingest_accepts_new_authenticated_account_after_snapshot_switch(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "access-token",
                    "id_token": _jwt_with_claims(
                        {"https://api.openai.com/auth": {"chatgpt_user_id": "user-test"}}
                    ),
                    "account_id": "account-new",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )
    config = AppConfig(accounts=(account,))
    snapshot_dir = tmp_path / "snapshots"
    save_usage_snapshot(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
            five_hour=LimitWindow(name="5h", remaining=97),
            weekly=LimitWindow(name="weekly", remaining=55),
            backend_user_id="user-old",
            backend_account_id="account-old",
        ),
        snapshot_dir,
    )

    usage, _path = ingest_and_save(
        config,
        "privat",
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "capturedAt": "2026-06-08T04:25:00+02:00",
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": json.dumps(
                        {
                            "user_id": "user-test",
                            "account_id": "account-new",
                            "rate_limit": {
                                "primary_window": {
                                    "used_percent": 3,
                                    "limit_window_seconds": 18000,
                                },
                                "secondary_window": {
                                    "used_percent": 45,
                                    "limit_window_seconds": 604800,
                                },
                            },
                        }
                    ),
                }
            ],
        },
        snapshot_dir,
    )

    assert usage.backend_user_id == "user-test"
    assert usage.backend_account_id == "account-new"
    saved = load_usage_snapshot("privat", snapshot_dir)
    assert saved is not None
    assert saved.backend_account_id == "account-new"
    assert saved.five_hour is not None and saved.five_hour.used == 3


def test_usage_from_ingest_payload_ignores_failed_html_api_responses():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "bodyText": "Codex analytics",
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 404,
                    "contentType": "text/html",
                    "bodyText": (
                        "<html><body>marketing 97 55 five_hour_usage_limit "
                        "weekly_usage_limit</body></html>"
                    ),
                },
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json; charset=utf-8",
                    "ok": True,
                    "truncated": False,
                    "bodyText": json.dumps(
                        {
                            "five_hour_usage_limit": {
                                "used": 97,
                                "limit": 100,
                                "reset_at": "2026-06-08T04:26:00+02:00",
                            },
                            "weekly_usage_limit": {
                                "used": 55,
                                "limit": 1000,
                                "reset_at": "2026-06-14T04:26:00+02:00",
                            },
                        }
                    ),
                },
            ],
        },
    )

    assert usage.status == AccountStatus.OK
    assert usage.five_hour is not None
    assert usage.five_hour.used == 97
    assert usage.weekly is not None
    assert usage.weekly.used == 55


def test_usage_from_ingest_payload_reports_search_excerpt():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics?secret=1",
            "title": "Codex",
            "readyState": "complete",
            "textLength": 123,
            "bodyText": "Codex analytics without the expected limit labels",
        },
    )

    assert usage.status == AccountStatus.PARTIAL
    assert usage.error is not None
    assert "usage limits not found" in usage.error
    assert "https://chatgpt.com/codex/cloud/settings/analytics" in usage.error
    assert "secret=1" not in usage.error
    assert 'excerpt="Codex analytics without the expected limit labels"' in usage.error


@pytest.mark.parametrize("account_ref", [None, [], 1, "bad/id", object()])
def test_write_bridge_extension_rejects_invalid_account_ref(tmp_path, account_ref):
    with pytest.raises(ValueError, match="account id"):
        write_bridge_extension(
            account_ref,  # type: ignore[arg-type]
            tmp_path / "extension",
            endpoint="http://127.0.0.1:8765/ingest",
            interval_seconds=300,
            token="A" * 32,
        )


def test_write_bridge_extension_rejects_invalid_output_dir(tmp_path):
    with pytest.raises(ValueError, match="extension output directory"):
        write_bridge_extension(
            "privat",
            [],  # type: ignore[arg-type]
            endpoint="http://127.0.0.1:8765/ingest",
            interval_seconds=300,
            token="A" * 32,
        )


@pytest.mark.parametrize("account_id", [None, [], 1, object()])
def test_save_bridge_debug_payload_rejects_non_string_account_id(tmp_path, account_id):
    with pytest.raises(ValueError, match="account id"):
        save_bridge_debug_payload(account_id, {}, tmp_path / "snapshots")  # type: ignore[arg-type]


@pytest.mark.parametrize("payload", [None, [], "invalid", 1, object()])
def test_save_bridge_debug_payload_rejects_non_object_payload(tmp_path, payload):
    with pytest.raises(ValueError, match="debug payload"):
        save_bridge_debug_payload("privat", payload, tmp_path / "snapshots")  # type: ignore[arg-type]


@pytest.mark.parametrize("snapshot_dir", [[], "invalid", 1, object()])
def test_save_bridge_debug_payload_rejects_non_path(tmp_path, snapshot_dir):
    with pytest.raises(ValueError, match="snapshot directory"):
        save_bridge_debug_payload("privat", {}, snapshot_dir)  # type: ignore[arg-type]


def test_save_bridge_debug_payload_redacts_url_and_locks_file(tmp_path):
    path = save_bridge_debug_payload(
        "BW/Privat",
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics?secret=1",
            "bodyText": "user@example.test 1 / 2",
            "htmlText": (
                "<html><script>"
                '"accessToken":"aaa.bbb.ccc","sessionToken":"ddd.eee.fff"'
                "</script><body>debug</body></html>"
            ),
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage?secret=1",
                    "bodyText": (
                        '{"accessToken":"aaa.bbb.ccc","email":"user@example.test",'
                        '"user_id":"user-secret","account_id":"account-secret"}'
                    ),
                }
            ],
        },
        tmp_path / "snapshots",
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "BW_Privat-last-ingest.json"
    assert payload["url"] == "https://chatgpt.com/codex/cloud/settings/analytics"
    assert payload["bodyText"] == "[redacted.email] 1 / 2"
    assert "accessToken" not in payload["htmlText"]
    assert "sessionToken" not in payload["htmlText"]
    assert "<script>[redacted]</script>" in payload["htmlText"]
    assert "<body>debug</body>" in payload["htmlText"]
    assert payload["apiResponses"][0]["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert "aaa.bbb.ccc" not in payload["apiResponses"][0]["bodyText"]
    assert "user@example.test" not in payload["apiResponses"][0]["bodyText"]
    assert "user-secret" not in payload["apiResponses"][0]["bodyText"]
    assert "account-secret" not in payload["apiResponses"][0]["bodyText"]
    assert path.stat().st_mode & 0o077 == 0


def test_stale_bridge_debug_payload_cannot_recreate_removed_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    payload = {"bodyText": "stale debug"}
    generation = load_state_generation("race")
    save_bridge_debug_payload("race", payload, state_generation=generation)

    remove_account_state("race")

    stale_path = save_bridge_debug_payload(
        "race",
        payload,
        state_generation=generation,
    )
    assert not stale_path.exists()

    fresh_path = save_bridge_debug_payload(
        "race",
        payload,
        state_generation=load_state_generation("race"),
    )
    assert fresh_path.exists()


def test_save_bridge_debug_payload_rejects_symlink_debug_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    debug_link = tmp_path / "debug"
    debug_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="debug directory"):
        save_bridge_debug_payload(
            "privat",
            {"bodyText": "user@example.test"},
            tmp_path / "snapshots",
        )

    assert not (outside / "privat-last-ingest.json").exists()


def test_save_bridge_debug_payload_fails_closed_when_directory_cannot_be_secured(
    tmp_path, monkeypatch
):
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    debug_dir = tmp_path / "debug"
    original_chmod = bridge_module.Path.chmod

    def fail_debug_chmod(path, mode):
        if path == debug_dir:
            raise OSError("simulated debug chmod failure")
        return original_chmod(path, mode)

    monkeypatch.setattr(bridge_module.Path, "chmod", fail_debug_chmod)

    with pytest.raises(ValueError, match="secure debug directory"):
        save_bridge_debug_payload("privat", {"bodyText": "debug"}, snapshot_dir)

    assert not (debug_dir / "privat-last-ingest.json").exists()


def test_save_bridge_debug_payload_rejects_symlink_debug_file(tmp_path):
    debug_dir = tmp_path / "debug"
    debug_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    (debug_dir / "privat-last-ingest.json").symlink_to(outside)

    with pytest.raises(ValueError, match="debug path"):
        save_bridge_debug_payload(
            "privat",
            {"bodyText": "user@example.test"},
            tmp_path / "snapshots",
        )

    assert outside.read_text(encoding="utf-8") == "keep"


def test_render_bridge_snippet_contains_account_endpoint_and_interval():
    token = bridge_token_for_account("BW_Privat")
    snippet = render_bridge_snippet(
        "BW_Privat",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )

    assert '"BW_Privat"' in snippet
    assert '"http://127.0.0.1:8765/ingest"' in snippet
    assert "setInterval" in snippet
    assert "300000" in snippet
    assert '"X-Codex-Usage-Account": account' in snippet
    assert '"Authorization": "Bearer " + token' in snippet
    assert token in snippet
    assert "htmlText" in snippet
    assert "accessibilityText" in snippet
    assert "sendInFlight" in snippet
    assert "codex-usage bridge failed" in snippet
    assert "Array.from(node.childNodes" not in snippet
    assert "Array.from(node.attributes" not in snippet
    assert "Array.from(document.querySelectorAll" not in snippet


@pytest.mark.parametrize("account_ref", [None, [], 1, "bad/id", object()])
def test_render_bridge_snippet_rejects_invalid_account_ref(account_ref):
    with pytest.raises(ValueError, match="account id"):
        render_bridge_snippet(
            account_ref,  # type: ignore[arg-type]
            endpoint="http://127.0.0.1:8765/ingest",
            interval_seconds=300,
            token="A" * 32,
        )


@pytest.mark.parametrize(
    "endpoint",
    [None, [], 1, object(), "ftp://127.0.0.1:8765/ingest", "http://127.0.0.1:8765/ingest?x=1"],
)
def test_render_bridge_snippet_rejects_invalid_endpoint(endpoint):
    with pytest.raises(ValueError, match="endpoint"):
        render_bridge_snippet(
            "privat",
            endpoint=endpoint,  # type: ignore[arg-type]
            interval_seconds=300,
            token="A" * 32,
        )


@pytest.mark.parametrize("interval_seconds", [None, [], "300", True, 59, object()])
def test_render_bridge_snippet_rejects_invalid_interval(interval_seconds):
    with pytest.raises(ValueError, match="interval"):
        render_bridge_snippet(
            "privat",
            endpoint="http://127.0.0.1:8765/ingest",
            interval_seconds=interval_seconds,  # type: ignore[arg-type]
            token="A" * 32,
        )


def test_render_bridge_snippet_sends_dom_fields_and_handles_fetch_failure(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    snippet = render_bridge_snippet(
        "BW_Privat",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    snippet_path = tmp_path / "snippet.js"
    snippet_path.write_text(snippet, encoding="utf-8")
    harness = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const requests = [];
let warnings = 0;
const textNode = (value) => ({ nodeType: 3, nodeValue: value, childNodes: [] });
const element = (tagName, attributes, childNodes) => ({
  nodeType: 1,
  tagName,
  attributes: Object.entries(attributes).map(([name, value]) => ({ name, value })),
  childNodes
});
const document = {
  title: "Codex",
  readyState: "complete",
  body: { innerText: "5 Stunden Nutzungsgrenze 97% verbleibend" },
  documentElement: element("html", {}, [
    element("body", {}, [
      element("div", { style: "width: 97%" }, [
        textNode("5 Stunden Nutzungsgrenze 97% verbleibend")
      ])
    ])
  ]),
  querySelectorAll(selector) {
    if (selector.includes("[aria-label]")) {
      return [{
        getAttribute(name) {
          return name === "aria-label" ? "x".repeat(2000100) : null;
        }
      }];
    }
    return [];
  }
};
const sandbox = {
  document,
  location: { href: "https://chatgpt.com/codex/cloud/settings/analytics" },
  fetch: async (_url, options) => {
    requests.push(JSON.parse(options.body));
    throw new Error("CSP blocked");
  },
  console: {
    log() {},
    warn(message) {
      if (String(message).includes("codex-usage bridge failed")) {
        warnings += 1;
      }
    }
  },
  Date,
  JSON,
  String,
  Array,
  Promise,
  setInterval() { return 1; },
  setTimeout,
  clearTimeout
};
const boundsGuard = `
Array.prototype.map = function() { throw new Error("unbounded map collector used"); };
Array.prototype.flatMap = function() { throw new Error("unbounded flatMap collector used"); };
Object.getPrototypeOf([]).map = Array.prototype.map;
Object.getPrototypeOf([]).flatMap = Array.prototype.flatMap;
`;
vm.runInNewContext(boundsGuard + source, sandbox);
setTimeout(() => {
  if (
    requests.length !== 1
    || !requests[0].htmlText.includes("width: 97%")
    || !requests[0].htmlText.includes("5 Stunden Nutzungsgrenze")
        || requests[0].accessibilityText.length > 2000000
        || requests[0].accessibilityText.length === 0
        || requests[0].truncatedFields.accessibilityText !== true
        || warnings !== 1
      ) {
        throw new Error(JSON.stringify({ requests, warnings }));
      }
  process.exit(0);
}, 100);
'''

    result = subprocess.run(
        [node, "-e", harness, str(snippet_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_render_bridge_snippet_preserves_metadata_when_payload_fallback_runs(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    snippet = render_bridge_snippet(
        "BW_Privat",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    snippet = snippet.replace(
        "  void sendCodexUsage();\n  setInterval",
        "  globalThis.fitPayloadForTest = fitPayload;\n"
        "  if (false) void sendCodexUsage();\n"
        "  setInterval",
    )
    snippet_path = tmp_path / "snippet.js"
    snippet_path.write_text(snippet, encoding="utf-8")
    harness = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const sandbox = {
  TextEncoder,
  setInterval() { return 1; },
  clearInterval() {},
  globalThis: null
};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const payload = sandbox.fitPayloadForTest({
  account: "BW_Privat",
  url: "u".repeat(500000),
  title: "t".repeat(2000),
  capturedAt: "2026-08-16T10:00:00.000Z",
  readyState: "complete",
  noise: new Array(5000000).fill(0)
});
if (
  payload.account !== "BW_Privat"
  || payload.url.length !== 500000
  || payload.title.length !== 2000
  || payload.capturedAt !== "2026-08-16T10:00:00.000Z"
  || payload.readyState !== "complete"
) {
  throw new Error(JSON.stringify(payload));
}
process.exit(0);
'''
    result = subprocess.run(
        [node, "-e", harness, str(snippet_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_render_bridge_snippet_reports_field_limit_truncation(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    snippet = render_bridge_snippet(
        "BW_Privat",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    snippet = snippet.replace(
        "  void sendCodexUsage();\n  setInterval",
        "  globalThis.boundedVisibleTextForTest = boundedVisibleText;\n"
        "  globalThis.boundedDomCaptureForTest = boundedDomCapture;\n"
        "  if (false) void sendCodexUsage();\n"
        "  setInterval",
    )
    snippet_path = tmp_path / "snippet.js"
    snippet_path.write_text(snippet, encoding="utf-8")
    harness = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const textNode = (value) => ({ nodeType: 3, nodeValue: value, childNodes: [] });
const element = (tagName, childNodes) => ({
  nodeType: 1,
  tagName,
  childNodes,
  attributes: [],
  hidden: false
});
const sandbox = {
  TextEncoder,
  getComputedStyle() { return {}; },
  setInterval() { return 1; },
  clearInterval() {},
  globalThis: null
};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const visible = sandbox.boundedVisibleTextForTest(
  element("div", [textNode("x".repeat(1999999))])
);
const boundaryVisible = sandbox.boundedVisibleTextForTest(
  element("span", [textNode("x".repeat(2000000)), textNode("later")])
);
const dom = sandbox.boundedDomCaptureForTest(
  element("body", [textNode("x".repeat(1999994))])
);
if (!visible.truncated || !boundaryVisible.truncated || !dom.htmlTruncated) {
  throw new Error(JSON.stringify({
    visibleTruncated: visible.truncated,
    visibleLength: visible.text.length,
    domTextTruncated: dom.textTruncated,
    domHtmlTruncated: dom.htmlTruncated,
    domHtmlLength: dom.html.length
  }));
}
process.exit(0);
'''
    result = subprocess.run(
        [node, "-e", harness, str(snippet_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_generated_content_preserves_metadata_when_payload_fallback_runs(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    content_path = output / "content.js"
    source = content_path.read_text(encoding="utf-8").replace(
        "\nstartCodexUsageBridge();",
        "\nglobalThis.fitCodexUsagePayloadForTest = fitCodexUsagePayload;\n"
        "if (false) startCodexUsageBridge();",
    )
    content_path.write_text(source, encoding="utf-8")
    harness = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const sandbox = {
  TextEncoder,
  window: { addEventListener() {} },
  globalThis: null
};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const payload = sandbox.fitCodexUsagePayloadForTest({
  account: "BW_Privat",
  url: "https://chatgpt.com/codex/cloud/settings/analytics",
  title: "Codex",
  capturedAt: "2026-08-16T10:00:00.000Z",
  readyState: "complete",
  apiResponses: [],
  noise: new Array(5000000).fill(0)
});
if (
  payload.account !== "BW_Privat"
  || payload.url !== "https://chatgpt.com/codex/cloud/settings/analytics"
  || payload.title !== "Codex"
  || payload.capturedAt !== "2026-08-16T10:00:00.000Z"
  || payload.readyState !== "complete"
) {
  throw new Error(JSON.stringify(payload));
}
process.exit(0);
'''
    result = subprocess.run(
        [node, "-e", harness, str(content_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_generated_content_marks_boundary_truncation_with_remaining_nodes(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    content_path = output / "content.js"
    source = content_path.read_text(encoding="utf-8").replace(
        "\nstartCodexUsageBridge();",
        "\nglobalThis.boundedVisibleTextForTest = boundedCodexUsageVisibleText;\n"
        "if (false) startCodexUsageBridge();",
    )
    content_path.write_text(source, encoding="utf-8")
    harness = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const textNode = (value) => ({ nodeType: 3, nodeValue: value, childNodes: [] });
const element = (tagName, childNodes) => ({
  nodeType: 1,
  tagName,
  childNodes,
  attributes: [],
  hidden: false
});
const sandbox = {
  TextEncoder,
  window: { addEventListener() {} },
  getComputedStyle() { return {}; },
  globalThis: null
};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const result = sandbox.boundedVisibleTextForTest(
  element("span", [textNode("x".repeat(2000000)), textNode("later")])
);
if (!result.truncated || result.text.length !== 2000000) {
  throw new Error(JSON.stringify(result));
}
process.exit(0);
'''
    result = subprocess.run(
        [node, "-e", harness, str(content_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_render_bridge_snippet_bounds_streaming_and_legacy_ack(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    snippet = render_bridge_snippet(
        "BW_Privat",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    snippet_path = tmp_path / "snippet.js"
    snippet_path.write_text(snippet, encoding="utf-8")
    harness = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const logs = [];
let intervalCallback = null;
let fetchIndex = 0;
let maxDecoderInput = 0;
class TrackingTextDecoder {
  constructor() { this.decoder = new TextDecoder(); }
  decode(value, options) {
    maxDecoderInput = Math.max(maxDecoderInput, value ? value.length : 0);
    return this.decoder.decode(value, options);
  }
}
const makeStreamingResponse = () => {
    const bytes = new TextEncoder().encode("a".repeat(4095) + "Ä" + "z".repeat(5000000));
    const split = bytes.indexOf(0xc3) + 1;
    const chunks = [bytes.slice(0, split), bytes.slice(split)];
    let cancelled = false;
  return {
    status: 200,
    body: {
      getReader() {
        return {
          async read() {
            if (!chunks.length) {
              return { done: true, value: undefined };
            }
            return { done: false, value: chunks.shift() };
          },
          async cancel() { cancelled = true; }
        };
      }
    },
    text: async () => "fallback".repeat(2000),
    wasCancelled() { return cancelled; }
  };
};
const first = makeStreamingResponse();
const document = {
  title: "Codex",
  readyState: "complete",
  body: { nodeType: 1, tagName: "body", attributes: [], childNodes: [] },
  documentElement: {
    nodeType: 1,
    tagName: "html",
    attributes: [],
    childNodes: []
  },
  querySelectorAll() { return []; }
};
const sandbox = {
  document,
  location: { href: "https://chatgpt.com/codex/cloud/settings/analytics" },
  fetch: async () => {
    fetchIndex += 1;
    return fetchIndex === 1
      ? first
      : { status: 413, text: async () => "x".repeat(5000) };
  },
  console: {
    log(...args) { logs.push(args); },
    warn() {}
  },
  Date,
  JSON,
  String,
  Object,
  Array,
  Promise,
  TextDecoder: TrackingTextDecoder,
  TextEncoder,
  Uint8Array,
  setInterval(callback) { intervalCallback = callback; return 1; },
  clearInterval() {}
};
vm.runInNewContext(source, sandbox);
setTimeout(() => {
  if (typeof intervalCallback !== "function") {
    throw new Error("interval callback was not installed");
  }
  intervalCallback();
}, 20);
setTimeout(() => {
  const firstText = logs[0] && logs[0][2];
  const secondText = logs[1] && logs[1][2];
  if (
    fetchIndex !== 2
    || typeof firstText !== "string"
    || firstText.length !== 4096
    || !firstText.endsWith("Ä")
    || !first.wasCancelled()
    || typeof secondText !== "string"
    || secondText.length !== 4096
    || secondText !== "x".repeat(4096)
    || maxDecoderInput > 65536
  ) {
    throw new Error(JSON.stringify({
      fetchIndex, logs, cancelled: first.wasCancelled(), maxDecoderInput
    }));
  }
  process.exit(0);
}, 100);
'''

    result = subprocess.run(
        [node, "-e", harness, str(snippet_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_render_bridge_snippet_keeps_aggregate_payload_under_ingest_limit(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    snippet = render_bridge_snippet(
        "BW_Privat",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    snippet_path = tmp_path / "snippet.js"
    snippet_path.write_text(snippet, encoding="utf-8")
    harness = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const requests = [];
const huge = "ä".repeat(2000100);
const textNode = (value) => ({ nodeType: 3, nodeValue: value, childNodes: [] });
const element = (tagName, childNodes) => ({
  nodeType: 1, tagName, attributes: [], childNodes
});
const body = element("body", [textNode(huge)]);
const document = {
  title: "Codex",
  readyState: "complete",
  body,
  documentElement: element("html", [body]),
  querySelectorAll(selector) {
    if (selector.includes("[aria-label]")) {
      return [{ getAttribute() { return huge; } }];
    }
    if (selector.includes("svg text")) {
      return [{ textContent: huge }];
    }
    return [];
  }
};
const sandbox = {
  document,
  location: { href: "https://chatgpt.com/codex/cloud/settings/analytics" },
  fetch: async (_url, options) => {
    requests.push(options.body);
    throw new Error("CSP blocked");
  },
  console: { log() {}, warn() {} },
  Date,
  JSON,
  String,
  Array,
  Set,
  Promise,
  setInterval() { return 1; },
  setTimeout,
  clearTimeout
};
vm.runInNewContext(source, sandbox);
setTimeout(() => {
  if (requests.length !== 1 || Buffer.byteLength(requests[0], "utf8") >= 10000000) {
    throw new Error(JSON.stringify({ length: requests[0] && requests[0].length }));
  }
  process.exit(0);
}, 100);
'''

    result = subprocess.run(
        [node, "-e", harness, str(snippet_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_write_bridge_extension_creates_vivaldi_compatible_files(tmp_path):
    token = bridge_token_for_account("BW_Privat")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    background = (output / "background.js").read_text(encoding="utf-8")
    content = (output / "content.js").read_text(encoding="utf-8")
    page_hook = (output / "page-hook.js").read_text(encoding="utf-8")

    assert manifest["manifest_version"] == 3
    assert "https://chatgpt.com/*" in manifest["host_permissions"]
    assert "http://127.0.0.1:8765/*" in manifest["host_permissions"]
    assert manifest["content_scripts"][0]["matches"] == [
        "https://chatgpt.com/codex/cloud/settings/analytics*"
    ]
    assert manifest["content_scripts"][0]["run_at"] == "document_start"
    assert manifest["content_scripts"][0]["js"] == ["content.js"]
    assert manifest["content_scripts"][1]["matches"] == [
        "https://chatgpt.com/codex/cloud/settings/analytics*"
    ]
    assert manifest["content_scripts"][1]["run_at"] == "document_start"
    assert manifest["content_scripts"][1]["world"] == "MAIN"
    assert manifest["content_scripts"][1]["js"] == ["page-hook.js"]
    assert "fetch(ENDPOINT" in background
    assert f'const TOKEN = "{token}";' in background
    assert '"X-Codex-Usage-Account": ACCOUNT' in background
    assert '"Authorization": "Bearer " + TOKEN' in background
    assert "chrome.runtime.sendMessage" in content
    assert "/backend-api/wham/usage" in content
    assert '"/wham/usage"' not in content
    assert "fetchCodexUsageApis" in content
    assert "apiResponses" in content
    assert 'credentials: "include"' in content
    assert "looksLikeCodexUsageJson" in content
    assert "bodyExcerpt" in content
    assert "stopCodexUsageBridge" in content
    assert "extension context invalidated" in content
    assert "codexUsageIntervalId = setInterval" in content
    assert "codexUsageCapturedApiResponses" in content
    assert "codexUsageApiResponseKey" in content
    assert "requestSequence" in content
    assert "window.addEventListener(\"message\"" in content
    assert "const pageRefreshSucceeded = await requestCodexUsagePageRefresh()" in content
    assert (
        "const probeResponses = pageRefreshSucceeded ? [] : await fetchCodexUsageApis()"
        in content
    )
    assert "boundedCodexUsageVisibleText" in content
    assert "boundedCodexUsageDomCapture" in content
    assert '"script", "style", "link", "meta", "noscript", "template"' in content
    assert "root.html" in content
    assert "collectCodexUsageAttributeText" in content
    assert "collectCodexUsageSvgText" in content
    assert "fieldLengths" in content
    assert "truncatedFields" in content
    assert "visibleTextLength" in content
    assert "CODEX_USAGE_READY_TIMEOUT_MS = 60000" in content
    assert "htmlText" in content
    assert "MutationObserver" in content
    assert "readyState" in content
    assert "textLength" in content
    assert "BW_Privat" in content
    assert "300000" in content
    assert "window.fetch" in page_hook
    assert "response.clone()" in page_hook
    assert "window.postMessage" in page_hook
    assert "codexUsageApiResponses" in page_hook
    assert "codexUsageApiResponseKey" in page_hook
    assert "CODEX_USAGE_CAPTURED_API_MAX_CHARS" in page_hook
    assert "requestSequence" in page_hook
    assert "/backend-api/wham/" in page_hook
    assert 'source: "page-fetch"' in page_hook


def test_bridge_extension_transaction_stays_inside_output_directory(tmp_path, monkeypatch):
    output_dir = tmp_path / "extension"
    recorded = {}
    original_temporary_directory = bridge_module.tempfile.TemporaryDirectory

    def temporary_directory(*args, **kwargs):
        recorded["dir"] = kwargs.get("dir")
        return original_temporary_directory(*args, **kwargs)

    monkeypatch.setattr(
        bridge_module.tempfile,
        "TemporaryDirectory",
        temporary_directory,
    )

    write_bridge_extension(
        "BW_Privat",
        output_dir,
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )

    assert Path(recorded["dir"]) == output_dir


def test_generated_background_bounds_streaming_and_legacy_ack_responses(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
let listener = null;
let fetchIndex = 0;
const responses = [];
let maxDecoderInput = 0;
class TrackingTextDecoder {
  constructor() { this.decoder = new TextDecoder(); }
  decode(value, options) {
    maxDecoderInput = Math.max(maxDecoderInput, value ? value.length : 0);
    return this.decoder.decode(value, options);
  }
}
const makeStreamingResponse = () => {
  const bytes = new TextEncoder().encode("a".repeat(4095) + "Ä" + "z".repeat(5000000));
  const split = bytes.indexOf(0xc3) + 1;
  const chunks = [bytes.slice(0, split), bytes.slice(split)];
  let cancelled = false;
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        return {
          async read() {
            if (!chunks.length) {
              return { done: true, value: undefined };
            }
            return { done: false, value: chunks.shift() };
          },
          async cancel() { cancelled = true; }
        };
      }
    },
    text() { throw new Error("stream reader must be used"); },
    wasCancelled() { return cancelled; }
  };
};
const runtime = {
  onMessage: {
    addListener(callback) { listener = callback; }
  }
};
const first = makeStreamingResponse();
const sandbox = {
  chrome: { runtime },
  fetch: async () => {
    fetchIndex += 1;
    return fetchIndex === 1
      ? first
      : {
          ok: false,
          status: 413,
          text: async () => "x".repeat(5000)
        };
  },
  TextDecoder: TrackingTextDecoder,
  TextEncoder,
  Uint8Array,
  Promise,
  JSON,
  String,
  Object,
  Array,
  Error,
  console
};
vm.runInNewContext(source, sandbox);
function invoke() {
  listener({ type: "codexUsageIngest", payload: {} }, {}, (response) => {
    responses.push(response);
    if (responses.length === 1) {
      invoke();
      return;
    }
    if (
      responses[0].text.length !== 4096
      || !responses[0].text.endsWith("Ä")
      || !first.wasCancelled()
      || responses[1].text.length !== 4096
      || responses[1].text !== "x".repeat(4096)
      || maxDecoderInput > 65536
    ) {
      throw new Error(JSON.stringify({ responses }));
    }
    process.exit(0);
  });
}
invoke();
setTimeout(() => process.exit(1), 1000);
"""
    result = subprocess.run(
        [node, "-e", harness, str(output / "background.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_write_bridge_extension_does_not_leave_partial_files_when_staging_fails(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "extension"
    output_dir.mkdir()
    old_files = {
        "manifest.json": "old manifest",
        "background.js": "old background",
        "content.js": "old content",
        "page-hook.js": "old page hook",
    }
    for filename, content in old_files.items():
        (output_dir / filename).write_text(content, encoding="utf-8")
    unrelated = output_dir / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    original_write = bridge_module._write_private_text

    def fail_content(path, content, *, label):
        if path.name == "content.js":
            raise OSError("simulated content generation failure")
        return original_write(path, content, label=label)

    monkeypatch.setattr(bridge_module, "_write_private_text", fail_content)

    with pytest.raises(OSError, match="simulated content generation failure"):
        write_bridge_extension(
            "BW_Privat",
            output_dir,
            endpoint="http://127.0.0.1:8765/ingest",
            interval_seconds=300,
            token="A" * 43,
        )

    for filename, content in old_files.items():
        assert (output_dir / filename).read_text(encoding="utf-8") == content
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_write_bridge_extension_serializes_output_transaction(tmp_path, monkeypatch):
    output_dir = tmp_path / "extension"
    lock_events = []

    class FakeLock:
        def __enter__(self):
            lock_events.append("enter")

        def __exit__(self, exc_type, exc_value, traceback):
            lock_events.append("exit")

    def fake_lock(path, **kwargs):
        assert path == output_dir / ".codex-usage-write"
        assert kwargs["label"] == "bridge extension output lock"
        return FakeLock()

    original_write = bridge_module._write_private_text

    def observe_stage_write(path, content, *, label):
        assert lock_events == ["enter"]
        return original_write(path, content, label=label)

    monkeypatch.setattr(bridge_module, "private_path_lock", fake_lock)
    monkeypatch.setattr(bridge_module, "_write_private_text", observe_stage_write)

    assert write_bridge_extension(
        "BW_Privat",
        output_dir,
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
        token="A" * 43,
    ) == output_dir
    assert lock_events == ["enter", "exit"]


def test_write_bridge_extension_rolls_back_when_commit_fails(tmp_path, monkeypatch):
    output_dir = tmp_path / "extension"
    output_dir.mkdir()
    old_files = {
        "manifest.json": "old manifest",
        "background.js": "old background",
        "content.js": "old content",
        "page-hook.js": "old page hook",
    }
    for filename, content in old_files.items():
        (output_dir / filename).write_text(content, encoding="utf-8")
    original_replace = Path.replace

    def fail_page_hook_commit(source, target):
        if source.parent.name == "stage" and target.name == "page-hook.js":
            raise OSError("simulated page hook commit failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_page_hook_commit)

    with pytest.raises(OSError, match="simulated page hook commit failure"):
        write_bridge_extension(
            "BW_Privat",
            output_dir,
            endpoint="http://127.0.0.1:8765/ingest",
            interval_seconds=300,
            token="A" * 43,
        )

    for filename, content in old_files.items():
        assert (output_dir / filename).read_text(encoding="utf-8") == content


def test_http_bridge_rejects_invalid_auth_before_reading_body(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
    )
    config = AppConfig(accounts=(account,))
    token = bridge_token_for_account(account.id)
    handler = _make_handler(config, tmp_path / "snapshots", {account.id: token})
    read_calls = []
    close_flags = []
    original_setup = handler.setup
    original_send_json = handler._send_json

    def send_json_with_close_probe(instance, status, payload):
        close_flags.append(instance.close_connection)
        return original_send_json(instance, status, payload)

    handler._send_json = send_json_with_close_probe

    def setup_with_read_probe(instance):
        original_setup(instance)
        stream = instance.rfile

        class ReadProbe:
            def read(self, *args, **kwargs):
                read_calls.append(args)
                return stream.read(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(stream, name)

        instance.rfile = ReadProbe()

    handler.setup = setup_with_read_probe
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection(server.server_address[0], server.server_address[1], timeout=5)
        try:
            connection.putrequest("POST", "/ingest")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "10000000")
            connection.putheader("Origin", "https://chatgpt.com")
            connection.putheader("X-Codex-Usage-Account", account.id)
            connection.putheader("Authorization", "Bearer invalid-token")
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == 401
            response.read()
        finally:
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert read_calls == []
    assert close_flags == [True]


def test_http_bridge_requires_the_account_token(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": _jwt_with_claims(
                        {
                            "https://api.openai.com/auth": {
                                "chatgpt_user_id": "user-test",
                                "chatgpt_account_id": "account-test",
                            }
                        }
                    ),
                    "account_id": "account-test",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
    )
    config = AppConfig(accounts=(account,))
    token = bridge_token_for_account(account.id)
    handler = _make_handler(
        config,
        tmp_path / "snapshots",
        {account.id: token},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/ingest"
    payload = {
        "account": account.id,
        "capturedAt": "2026-06-08T04:25:00+02:00",
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
                "ok": True,
                "truncated": False,
                "bodyText": json.dumps(
                    {
                        "user_id": "user-test",
                        "account_id": "account-test",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 3,
                                "limit_window_seconds": 18000,
                            },
                            "secondary_window": {
                                "used_percent": 45,
                                "limit_window_seconds": 604800,
                            },
                        },
                    }
                ),
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")

    def post(headers=None, request_body=body):
        request = Request(endpoint, data=request_body, method="POST", headers=headers or {})
        return urlopen(request, timeout=5)

    try:
        with pytest.raises(HTTPError) as missing:
            post()
        assert missing.value.code == 403
        with pytest.raises(HTTPError) as wrong:
            post(
                {
                    "Origin": "https://chatgpt.com",
                    "X-Codex-Usage-Account": account.id,
                    "Authorization": "Bearer wrong-token",
                }
            )
        assert wrong.value.code == 401
        with pytest.raises(HTTPError) as outside_origin:
            post(
                {
                    "Origin": "https://evil.example",
                    "X-Codex-Usage-Account": account.id,
                    "Authorization": f"Bearer {token}",
                }
            )
        assert outside_origin.value.code == 403
        with post(
            {
                "Origin": "https://chatgpt.com",
                "X-Codex-Usage-Account": account.id,
                "Authorization": f"Bearer {token}",
            }
        ) as response:
            assert response.status == 200
            assert json.loads(response.read())["status"] == "ok"
        mismatched_payload = dict(payload, account="other")
        with pytest.raises(HTTPError) as mismatch:
            post(
                {
                    "Origin": "https://chatgpt.com",
                    "X-Codex-Usage-Account": account.id,
                    "Authorization": f"Bearer {token}",
                },
                json.dumps(mismatched_payload).encode("utf-8"),
            )
        assert mismatch.value.code == 400
        assert bridge_token_matches(account.id, token) is True
        assert revoke_bridge_token(account.id) is True
        with pytest.raises(HTTPError) as revoked:
            post(
                {
                    "Origin": "https://chatgpt.com",
                    "X-Codex-Usage-Account": account.id,
                    "Authorization": f"Bearer {token}",
                }
            )
        assert revoked.value.code == 401
        replacement = bridge_token_for_account(account.id)
        assert replacement != token
        with post(
            {
                "Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
                "X-Codex-Usage-Account": account.id,
                "Authorization": f"Bearer {replacement}",
            }
        ) as response:
            assert response.status == 200
            assert json.loads(response.read())["status"] == "ok"
        connection = HTTPConnection(server.server_address[0], server.server_address[1], timeout=5)
        try:
            connection.putrequest("POST", "/ingest")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(body)))
            connection.putheader("Origin", "https://chatgpt.com")
            connection.putheader("X-Codex-Usage-Account", account.id)
            connection.putheader("Authorization", f"Bearer {replacement}")
            connection.putheader("Authorization", f"Bearer {replacement}")
            connection.endheaders(body)
            duplicate_auth = connection.getresponse()
            assert duplicate_auth.status == 401
            duplicate_auth.read()
        finally:
            connection.close()
        connection = HTTPConnection(server.server_address[0], server.server_address[1], timeout=5)
        try:
            connection.putrequest("POST", "/ingest")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "9" * 100)
            connection.putheader("Origin", "https://chatgpt.com")
            connection.putheader("X-Codex-Usage-Account", account.id)
            connection.putheader("Authorization", f"Bearer {replacement}")
            connection.endheaders()
            oversized_length = connection.getresponse()
            assert oversized_length.status == 413
            oversized_length.read()
        finally:
            connection.close()
        options = Request(
            endpoint,
            method="OPTIONS",
            headers={
                "Origin": "https://chatgpt.com",
                "Access-Control-Request-Headers": "authorization, x-codex-usage-account",
            },
        )
        with urlopen(options, timeout=5) as response:
            assert response.status == 204
            assert "Authorization" in response.headers["Access-Control-Allow-Headers"]
            assert "X-Codex-Usage-Account" in response.headers["Access-Control-Allow-Headers"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    token_path = tmp_path / "data" / "codex-usage" / "bridge-tokens" / "privat.token"
    assert token_path.read_text(encoding="utf-8").strip() == replacement
    assert token_path.stat().st_mode & 0o077 == 0


def test_http_bridge_accepts_account_added_after_server_start(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    def write_auth(path, user_id, account_id):
        path.write_text(
            json.dumps(
                {
                    "tokens": {
                        "id_token": _jwt_with_claims(
                            {
                                "https://api.openai.com/auth": {
                                    "chatgpt_user_id": user_id,
                                    "chatgpt_account_id": account_id,
                                }
                            }
                        ),
                        "account_id": account_id,
                    }
                }
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)

    first_auth = tmp_path / "first-auth.json"
    second_auth = tmp_path / "second-auth.json"
    write_auth(first_auth, "first-user", "first-account")
    write_auth(second_auth, "second-user", "second-account")
    first = Account(
        id="first",
        label="First",
        profile_dir=str(tmp_path / "first-profile"),
        auth_json_path=str(first_auth),
    )
    config_path = tmp_path / "config.toml"
    config = AppConfig(accounts=(first,))
    save_config(config, config_path)
    first_token = bridge_token_for_account(first.id)
    handler = _make_handler(
        config,
        tmp_path / "snapshots",
        {first.id: first_token},
        config_path=config_path,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/ingest"

    try:
        add_or_update_account(
            "second",
            label="Second",
            profile_dir=str(tmp_path / "second-profile"),
            auth_json_path=str(second_auth),
            path=config_path,
        )
        second_token = bridge_token_for_account("second")
        payload = {
            "account": "second",
            "capturedAt": "2026-06-08T04:25:00+02:00",
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": json.dumps(
                        {
                            "user_id": "second-user",
                            "account_id": "second-account",
                            "rate_limit": {
                                "primary_window": {
                                    "used_percent": 3,
                                    "limit_window_seconds": 18000,
                                },
                                "secondary_window": {
                                    "used_percent": 45,
                                    "limit_window_seconds": 604800,
                                },
                            },
                        }
                    ),
                }
            ],
        }
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Origin": "https://chatgpt.com",
                "X-Codex-Usage-Account": "second",
                "Authorization": f"Bearer {second_token}",
            },
        )
        with urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read())["account"] == "second"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert load_usage_snapshot("second", tmp_path / "snapshots") is not None


@pytest.mark.parametrize("mutation", ["permissions", "hardlink"])
def test_bridge_token_rejects_non_private_token_file(tmp_path, monkeypatch, mutation):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    token_dir = tmp_path / "data" / "codex-usage" / "bridge-tokens"
    token_dir.mkdir(parents=True)
    token_path = token_dir / "privat.token"
    token_path.write_text("A" * 43 + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    if mutation == "permissions":
        token_path.chmod(0o644)
    else:
        (token_dir / "other.token").hardlink_to(token_path)

    with pytest.raises(ValueError, match="permissions"):
        bridge_token_for_account("privat")


def test_bridge_token_rejects_noncanonical_token_file_whitespace(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    token = bridge_token_for_account("privat")
    token_path = tmp_path / "data" / "codex-usage" / "bridge-tokens" / "privat.token"
    token_path.write_text(token + " \n", encoding="utf-8")

    assert bridge_token_matches("privat", token) is False
    with pytest.raises(ValueError, match="invalid bridge token"):
        bridge_token_for_account("privat")


def test_revoke_bridge_token_forces_new_token_after_account_readd(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    first = bridge_token_for_account("privat")
    token_path = tmp_path / "data" / "codex-usage" / "bridge-tokens" / "privat.token"
    assert token_path.is_file()

    assert revoke_bridge_token("privat") is True
    assert not token_path.exists()
    assert revoke_bridge_token("privat") is False

    second = bridge_token_for_account("privat")
    assert second != first
    assert token_path.read_text(encoding="utf-8").strip() == second


def test_generated_content_refreshes_page_usage_before_ingest(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const messages = [];
const refreshRequests = [];
const probeFetches = [];
let messageHandler = null;
const pageWindow = {
  addEventListener(type, callback) {
    if (type === "message") {
      messageHandler = callback;
    }
  },
  postMessage(message) {
    if (!message || message.type !== "codexUsageRefresh") {
      return;
    }
    refreshRequests.push(message);
    setTimeout(() => {
      messageHandler({
        source: pageWindow,
        data: {
          type: "codexUsageApiResponses",
          requestId: message.requestId,
          responses: [{
            source: "page-fetch",
            requestSequence: 1,
            url: "https://chatgpt.com/backend-api/wham/usage",
            status: 200,
            ok: true,
            contentType: "application/json",
            truncated: false,
            bodyText: JSON.stringify({
              rate_limit: {
                primary_window: { used_percent: 20, limit_window_seconds: 18000 },
                secondary_window: { used_percent: 60, limit_window_seconds: 604800 }
              }
            })
          }]
        }
      });
    }, 0);
  }
};
const text = "Codex analytics page text with enough content";
const textNode = (value) => ({ nodeType: 3, nodeValue: value, childNodes: [] });
const element = (tagName, childNodes) => ({
  nodeType: 1, tagName, attributes: [], childNodes
});
const document = {
  title: "Codex",
  readyState: "complete",
  body: { innerText: text },
  documentElement: element("html", [element("body", [textNode(text)])]),
  querySelectorAll() { return []; }
};
const runtime = {
  id: "test-extension",
  lastError: null,
  sendMessage(message, callback) {
    messages.push(message);
    callback({ ok: true });
  }
};
const sandbox = {
  window: pageWindow,
  document,
  chrome: { runtime },
  location: {
    href: "https://chatgpt.com/codex/cloud/settings/analytics",
    origin: "https://chatgpt.com"
  },
  fetch: async (url) => {
    probeFetches.push(url);
    return {
      headers: { get() { return "application/json"; } },
      text: async () => JSON.stringify({ detail: "probe should not run" })
    };
  },
  Date,
  JSON,
  Map,
  Array,
  Number,
  String,
  Object,
  Promise,
  URL,
  console,
  setInterval() { return 1; },
  clearInterval() {},
  setTimeout,
  clearTimeout
};
const boundsGuard = `
Array.prototype.map = function() { throw new Error("unbounded map collector used"); };
Array.prototype.flatMap = function() { throw new Error("unbounded flatMap collector used"); };
Object.getPrototypeOf([]).map = Array.prototype.map;
Object.getPrototypeOf([]).flatMap = Array.prototype.flatMap;
`;
vm.runInNewContext(boundsGuard + source, sandbox);
setTimeout(() => {
  const ingest = messages.find((message) => message.type === "codexUsageIngest");
  const usage = ingest && ingest.payload.apiResponses.find(
    (item) => item.url.endsWith("/backend-api/wham/usage")
  );
  const body = usage && JSON.parse(usage.bodyText);
  if (
    refreshRequests.length !== 1
    || probeFetches.length !== 0
    || !usage
    || body.rate_limit.primary_window.used_percent !== 20
  ) {
    throw new Error(JSON.stringify({ messages, refreshRequests, probeFetches }));
  }
  process.exit(0);
}, 700);
"""

    result = subprocess.run(
        [node, "-e", harness, str(output / "content.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_generated_content_enforces_api_response_aggregate_budget(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const messages = [];
let messageHandler = null;
const huge = "x".repeat(1100000);
const responses = [
  {
    source: "page-fetch",
    requestSequence: 1,
    url: "https://chatgpt.com/backend-api/wham/usage",
    status: 200,
    ok: true,
    contentType: "application/json",
    truncated: false,
    bodyText: JSON.stringify({ rate_limit: { primary_window: { used_percent: 10 } } })
  },
  ...Array.from({ length: 4 }, (_, index) => ({
    source: "page-fetch",
    requestSequence: index + 2,
    url: `https://chatgpt.com/backend-api/wham/other-${index}`,
    status: 200,
    ok: true,
    contentType: "application/json",
    truncated: false,
    bodyText: huge
  }))
];
const pageWindow = {
  addEventListener(type, callback) {
    if (type === "message") {
      messageHandler = callback;
    }
  },
  postMessage(message) {
    if (!message || message.type !== "codexUsageRefresh") {
      return;
    }
    setTimeout(() => messageHandler({
      source: pageWindow,
      data: {
        type: "codexUsageApiResponses",
        requestId: message.requestId,
        responses
      }
    }), 0);
  }
};
const bodyText = "ä".repeat(2000000);
const domText = "ß".repeat(2000000);
const attributeText = "ç".repeat(2000000);
const svgText = "€".repeat(2000000);
const textNode = (value) => ({ nodeType: 3, nodeValue: value, childNodes: [] });
const element = (tagName, childNodes) => ({
  nodeType: 1, tagName, childNodes, hidden: false, attributes: []
});
const body = element("body", [textNode(domText)]);
const document = {
  title: "Codex",
  readyState: "complete",
  body: { innerText: bodyText },
  documentElement: element("html", [body]),
  querySelectorAll(selector) {
    if (selector.includes("[aria-label]")) {
      return [{ getAttribute(name) { return name === "aria-label" ? attributeText : ""; } }];
    }
    if (selector.includes("svg text")) {
      return [{ textContent: svgText }];
    }
    return [];
  }
};
const runtime = {
  id: "test-extension",
  lastError: null,
  sendMessage(message, callback) {
    messages.push(message);
    callback({ ok: true });
  }
};
const sandbox = {
  window: pageWindow,
  document,
  chrome: { runtime },
  location: {
    href: "https://chatgpt.com/codex/cloud/settings/analytics",
    origin: "https://chatgpt.com"
  },
  console: { log() {}, warn() {} },
  Date,
  JSON,
  Map,
  Set,
  Array,
  Number,
  String,
  Object,
  Promise,
  URL,
  TextEncoder,
  MutationObserver: class { observe() {} disconnect() {} },
  setInterval() { return 1; },
  clearInterval() {},
  setTimeout,
  clearTimeout
};
vm.runInNewContext(source, sandbox);
setTimeout(() => {
  const ingest = messages.find((message) => message.type === "codexUsageIngest");
  const captured = ingest && ingest.payload.apiResponses;
  const total = captured && captured.reduce(
    (sum, item) => sum + String(item.bodyText || "").length,
    0
  );
  const serialized = ingest && JSON.stringify(ingest.payload);
  const serializedBytes = serialized && new TextEncoder().encode(serialized).length;
  if (
    !captured
    || total > 4000000
    || !captured.some((item) => item.url.endsWith("/wham/usage"))
    || !ingest.payload.htmlText.includes("ß")
    || !serialized
    || serializedBytes >= 9500000
  ) {
      throw new Error(JSON.stringify({
        captured: captured && captured.length,
        total,
        serializedBytes,
      }));
  }
  process.exit(0);
}, 100);
"""

    result = subprocess.run(
        [node, "-e", harness, str(output / "content.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_generated_content_bounds_streaming_api_responses(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const messages = [];
const refreshRequests = [];
let messageHandler = null;
let cancelled = 0;
const encoder = new TextEncoder();
const prefix = '{"rate_limit":{"label":"Ä","padding":"';
const boundaryPadding = "x".repeat(65536 - encoder.encode(prefix).length - 1);
const oversized = encoder.encode(
  prefix + boundaryPadding + "Ä" + "x".repeat(2000100) + '"}}'
);
let maxDecoderInput = 0;
class TrackingTextDecoder {
  constructor() { this.decoder = new TextDecoder(); }
  decode(value, options) {
    maxDecoderInput = Math.max(maxDecoderInput, value ? value.length : 0);
    return this.decoder.decode(value, options);
  }
}
function streamingResponse() {
  let offset = 0;
  return {
    status: 200,
    ok: true,
    headers: { get() { return "application/json"; } },
    body: {
      getReader() {
        return {
          async read() {
            if (offset >= oversized.length) {
              return { done: true, value: undefined };
            }
            const chunkSize = oversized.length;
            const end = Math.min(offset + chunkSize, oversized.length);
            const value = oversized.slice(offset, end);
            offset = end;
            return { done: false, value };
          },
          async cancel() { cancelled += 1; }
        };
      }
    }
  };
}
const pageWindow = {
  addEventListener(type, callback) {
    if (type === "message") {
      messageHandler = callback;
    }
  },
  postMessage(message) {
    refreshRequests.push(message);
    setTimeout(() => messageHandler({
      source: pageWindow,
      data: {
        type: "codexUsageApiResponses",
        requestId: message.requestId,
        responses: []
      }
    }), 0);
  }
};
const text = "Codex analytics page text with enough content";
const textNode = (value) => ({ nodeType: 3, nodeValue: value, childNodes: [] });
const element = (tagName, childNodes) => ({
  nodeType: 1, tagName, attributes: [], childNodes
});
const document = {
  title: "Codex",
  readyState: "complete",
  body: { innerText: text },
  documentElement: element("html", [element("body", [textNode(text)])]),
  querySelectorAll() { return []; }
};
const runtime = {
  id: "test-extension",
  lastError: null,
  sendMessage(message, callback) {
    messages.push(message);
    callback({ ok: true });
  }
};
const sandbox = {
  window: pageWindow,
  document,
  chrome: { runtime },
  location: {
    href: "https://chatgpt.com/codex/cloud/settings/analytics",
    origin: "https://chatgpt.com"
  },
  fetch: async () => streamingResponse(),
  console,
  Date,
  JSON,
  Map,
  Set,
  Array,
  Number,
  String,
  Object,
  Promise,
  URL,
  TextDecoder: TrackingTextDecoder,
  setInterval() { return 1; },
  clearInterval() {},
  setTimeout,
  clearTimeout
};
vm.runInNewContext(source, sandbox);
setTimeout(() => {
  const ingest = messages.find((message) => message.type === "codexUsageIngest");
  const responses = ingest && ingest.payload.apiResponses;
  const oversizedResponse = responses && responses.find(
    (item) => item && item.truncated === true
  );
  if (
    refreshRequests.length !== 1
    || !oversizedResponse
    || oversizedResponse.bodyText !== ""
    || !oversizedResponse.bodyExcerpt.includes("Ä")
    || cancelled !== 4
    || maxDecoderInput > 65536
  ) {
    throw new Error(JSON.stringify({ messages, refreshRequests, cancelled, maxDecoderInput }));
  }
  process.exit(0);
}, 1000);
"""

    result = subprocess.run(
        [node, "-e", harness, str(output / "content.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_generated_content_compacts_truncated_api_responses_before_ingest(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const messages = [];
const refreshRequests = [];
const probeFetches = [];
let messageHandler = null;
const huge = "x".repeat(2000000);
const truncatedResponses = Array.from({ length: 8 }, (_, index) => ({
  source: "page-fetch",
  requestSequence: index + 1,
  url: `https://chatgpt.com/backend-api/wham/other-${index}`,
  status: 200,
  ok: true,
  contentType: "application/json",
  truncated: true,
  bodyText: huge
}));
truncatedResponses.push({
  source: "page-fetch",
  requestSequence: 99,
  url: "https://chatgpt.com/backend-api/wham/usage",
  status: 200,
  ok: true,
  contentType: "application/json",
  truncated: false,
  bodyText: JSON.stringify({ rate_limit: { primary_window: { used_percent: 20 } } })
});
const pageWindow = {
  addEventListener(type, callback) {
    if (type === "message") {
      messageHandler = callback;
    }
  },
  postMessage(message) {
    refreshRequests.push(message);
    setTimeout(() => messageHandler({
      source: pageWindow,
      data: {
        type: "codexUsageApiResponses",
        requestId: message.requestId,
        responses: truncatedResponses
      }
    }), 0);
  }
};
const text = "Codex analytics page text with enough content";
const textNode = (value) => ({ nodeType: 3, nodeValue: value, childNodes: [] });
const element = (tagName, childNodes) => ({
  nodeType: 1, tagName, attributes: [], childNodes
});
const body = element("body", [textNode(text)]);
const document = {
  title: "Codex",
  readyState: "complete",
  body,
  documentElement: element("html", [body]),
  querySelectorAll() { return []; }
};
const runtime = {
  id: "test-extension",
  lastError: null,
  sendMessage(message, callback) {
    messages.push(message);
    callback({ ok: true });
  }
};
const sandbox = {
  window: pageWindow,
  document,
  chrome: { runtime },
  location: {
    href: "https://chatgpt.com/codex/cloud/settings/analytics",
    origin: "https://chatgpt.com"
  },
  fetch: async (url) => {
    probeFetches.push(url);
    return { headers: { get() { return "application/json"; } }, text: async () => "{}" };
  },
  console,
  Date,
  JSON,
  Map,
  Set,
  Array,
  Number,
  String,
  Object,
  Promise,
  URL,
  setInterval() { return 1; },
  clearInterval() {},
  setTimeout,
  clearTimeout
};
vm.runInNewContext(source, sandbox);
setTimeout(() => {
  const ingest = messages.find((message) => message.type === "codexUsageIngest");
  const responses = ingest && ingest.payload.apiResponses;
  const compacted = responses && responses.find((item) => item.truncated === true);
  const serialized = ingest && JSON.stringify(ingest.payload);
  if (
    refreshRequests.length !== 1
    || probeFetches.length !== 0
    || !compacted
    || compacted.bodyText !== ""
    || compacted.bodyExcerpt.length > 500
    || !serialized
    || Buffer.byteLength(serialized, "utf8") >= 10000000
  ) {
    throw new Error(JSON.stringify({ messages, refreshRequests, probeFetches }));
  }
  process.exit(0);
}, 700);
"""

    result = subprocess.run(
        [node, "-e", harness, str(output / "content.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_generated_content_rejects_incomplete_main_response_metadata(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const messages = [];
const refreshRequests = [];
const probeFetches = [];
let messageHandler = null;
const pageWindow = {
  addEventListener(type, callback) {
    if (type === "message") {
      messageHandler = callback;
    }
  },
  postMessage(message) {
    if (!message || message.type !== "codexUsageRefresh") {
      return;
    }
    refreshRequests.push(message);
    setTimeout(() => {
      messageHandler({
        source: pageWindow,
        data: {
          type: "codexUsageApiResponses",
          requestId: message.requestId,
          responses: [{
            source: "page-fetch",
            requestSequence: 1,
            url: "https://chatgpt.com/backend-api/wham/usage",
            status: 200,
            contentType: "application/json",
            bodyText: JSON.stringify({
              rate_limit: {
                primary_window: { used_percent: 20, limit_window_seconds: 18000 },
                secondary_window: { used_percent: 60, limit_window_seconds: 604800 }
              }
            })
          }]
        }
      });
    }, 0);
  }
};
const text = "Codex analytics page text with enough content";
const textNode = (value) => ({ nodeType: 3, nodeValue: value, childNodes: [] });
const element = (tagName, childNodes) => ({
  nodeType: 1, tagName, attributes: [], childNodes
});
const document = {
  title: "Codex",
  readyState: "complete",
  body: { innerText: text },
  documentElement: element("html", [element("body", [textNode(text)])]),
  querySelectorAll() { return []; }
};
const runtime = {
  id: "test-extension",
  lastError: null,
  sendMessage(message, callback) {
    messages.push(message);
    callback({ ok: true });
  }
};
const sandbox = {
  window: pageWindow,
  document,
  chrome: { runtime },
  location: {
    href: "https://chatgpt.com/codex/cloud/settings/analytics",
    origin: "https://chatgpt.com"
  },
  fetch: async (url) => {
    probeFetches.push(url);
    return {
      headers: { get() { return "application/json"; } },
      text: async () => JSON.stringify({ detail: "fallback probe" })
    };
  },
  Date,
  JSON,
  Map,
  Array,
  Number,
  String,
  Object,
  Promise,
  URL,
  console,
  setInterval() { return 1; },
  clearInterval() {},
  setTimeout,
  clearTimeout
};
vm.runInNewContext(source, sandbox);
setTimeout(() => {
  const ingest = messages.find((message) => message.type === "codexUsageIngest");
  if (refreshRequests.length !== 1 || probeFetches.length !== 4 || !ingest) {
    throw new Error(JSON.stringify({ messages, refreshRequests, probeFetches }));
  }
  process.exit(0);
}, 700);
"""

    result = subprocess.run(
        [node, "-e", harness, str(output / "content.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_generated_content_serializes_overlapping_sends(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const messages = [];
const probeFetches = [];
let messageHandler = null;
let intervalCallback = null;
let releaseFirst = null;
let refreshCount = 0;
let activeRefreshes = 0;
let maximumActiveRefreshes = 0;
const pageWindow = {
  addEventListener(type, callback) {
    if (type === "message") {
      messageHandler = callback;
    }
  },
  postMessage(message) {
    if (!message || message.type !== "codexUsageRefresh") {
      return;
    }
    refreshCount += 1;
    activeRefreshes += 1;
    maximumActiveRefreshes = Math.max(maximumActiveRefreshes, activeRefreshes);
    const respond = () => {
      setTimeout(() => {
        messageHandler({
          source: pageWindow,
          data: {
            type: "codexUsageApiResponses",
            requestId: message.requestId,
            responses: [{
              source: "page-fetch",
              requestSequence: refreshCount,
              url: "https://chatgpt.com/backend-api/wham/usage",
              status: 200,
              ok: true,
              contentType: "application/json",
              truncated: false,
              bodyText: JSON.stringify({
                rate_limit: {
                  primary_window: { used_percent: 20, limit_window_seconds: 18000 },
                  secondary_window: { used_percent: 60, limit_window_seconds: 604800 }
                }
              })
            }]
          }
        });
        activeRefreshes -= 1;
      }, 0);
    };
    if (refreshCount === 1) {
      releaseFirst = respond;
    } else {
      respond();
    }
  }
};
const text = "Codex analytics page text with enough content";
const textNode = (value) => ({ nodeType: 3, nodeValue: value, childNodes: [] });
const element = (tagName, childNodes) => ({
  nodeType: 1, tagName, attributes: [], childNodes
});
const document = {
  title: "Codex",
  readyState: "complete",
  body: { innerText: text },
  documentElement: element("html", [element("body", [textNode(text)])]),
  querySelectorAll() { return []; }
};
const runtime = {
  id: "test-extension",
  lastError: null,
  sendMessage(message, callback) {
    messages.push(message);
    callback({ ok: true });
  }
};
const sandbox = {
  window: pageWindow,
  document,
  chrome: { runtime },
  location: {
    href: "https://chatgpt.com/codex/cloud/settings/analytics",
    origin: "https://chatgpt.com"
  },
  fetch: async (url) => {
    probeFetches.push(url);
    return {
      headers: { get() { return "application/json"; } },
      text: async () => JSON.stringify({ detail: "probe should not run" })
    };
  },
  Date,
  JSON,
  Map,
  Array,
  Number,
  String,
  Object,
  Promise,
  URL,
  console,
  setInterval(callback) {
    intervalCallback = callback;
    return 1;
  },
  clearInterval() {},
  setTimeout,
  clearTimeout
};
vm.runInNewContext(source, sandbox);
setTimeout(() => {
  if (!intervalCallback || !releaseFirst) {
    throw new Error(JSON.stringify({ refreshCount, intervalCallback, releaseFirst }));
  }
  intervalCallback();
  releaseFirst();
}, 10);
setTimeout(() => {
  if (
    refreshCount !== 2
    || maximumActiveRefreshes !== 1
    || probeFetches.length !== 0
    || messages.filter((message) => message.type === "codexUsageIngest").length < 2
  ) {
    throw new Error(JSON.stringify({
      messages,
      refreshCount,
      maximumActiveRefreshes,
      probeFetches
    }));
  }
  process.exit(0);
}, 800);
"""

    result = subprocess.run(
        [node, "-e", harness, str(output / "content.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_generated_extension_handles_invalidated_runtime_callback(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
process.on("uncaughtException", (error) => {
  console.error(error);
  process.exitCode = 1;
});
const runtime = {
  id: "test-extension",
  sendMessage(_message, callback) {
    Promise.resolve().then(() => callback({}));
  }
};
Object.defineProperty(runtime, "lastError", {
  get() {
    throw new Error("Extension context invalidated");
  }
});
const text = "Codex analytics page text with enough content";
const textNode = (value) => ({ nodeType: 3, nodeValue: value, childNodes: [] });
const element = (tagName, childNodes) => ({
  nodeType: 1, tagName, attributes: [], childNodes
});
const sandbox = {
  window: { addEventListener() {} },
  document: {
    title: "Codex",
    readyState: "complete",
    body: { innerText: text },
    documentElement: element("html", [element("body", [textNode(text)])]),
    querySelectorAll() { return []; }
      },
      chrome: { runtime },
      location: {
        href: "https://chatgpt.com/codex/cloud/settings/analytics",
        origin: "https://chatgpt.com"
      },
      console,
  Date,
  JSON,
  Map,
  Array,
  Number,
  String,
  Object,
  Promise,
  URL,
  setInterval() { return 1; },
  clearInterval() {},
  setTimeout,
  clearTimeout,
  fetch: async () => ({
    headers: { get() { return "text/plain"; } },
    text: async () => ""
  })
};
vm.runInNewContext(source, sandbox);
setTimeout(() => process.exit(process.exitCode || 0), 40);
"""

    result = subprocess.run(
        [node, "-e", harness, str(output / "content.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_generated_content_reprobes_after_failed_main_usage_response(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const messages = [];
const fetched = [];
let messageHandler = null;
let observerCallback = null;
const textNode = (value) => ({ nodeType: 3, nodeValue: value, childNodes: [] });
const element = (tagName, childNodes) => ({
  nodeType: 1, tagName, attributes: [], childNodes
});
const pageWindow = {
  addEventListener(type, callback) {
    if (type === "message") {
      messageHandler = callback;
    }
  }
};
const document = {
  title: "Codex",
  readyState: "loading",
  body: { innerText: "" },
  documentElement: element("html", [element("body", [textNode("")])]),
  querySelectorAll() { return []; }
};
class MutationObserver {
  constructor(callback) { observerCallback = callback; }
  observe() {}
  disconnect() {}
}
const sandbox = {
  window: pageWindow,
  document,
  MutationObserver,
  chrome: {
    runtime: {
      id: "test-extension",
      lastError: null,
      sendMessage(message, callback) {
        messages.push(message);
        callback({ ok: true });
      }
    }
  },
  location: {
    href: "https://chatgpt.com/codex/cloud/settings/analytics",
    origin: "https://chatgpt.com"
  },
  fetch: async (url) => {
    fetched.push(url);
    return {
      headers: { get() { return "application/json"; } },
      text: async () => JSON.stringify({
        rate_limit: {
          primary_window: { used_percent: 3, limit_window_seconds: 18000 },
          secondary_window: { used_percent: 45, limit_window_seconds: 604800 }
        }
      })
    };
  },
  Date,
  JSON,
  URL,
  String,
  Number,
  Object,
  Array,
  Promise,
  console,
  setInterval() { return 1; },
  clearInterval() {},
  setTimeout,
  clearTimeout
};
vm.runInNewContext(source, sandbox);
if (!messageHandler || !observerCallback) {
  throw new Error("content script did not initialize");
}
messageHandler({
  source: pageWindow,
  data: {
    type: "codexUsageApiResponses",
    responses: [{
      source: "page-fetch",
      url: "https://chatgpt.com/backend-api/wham/settings/user",
      requestSequence: 1,
      status: 200,
      contentType: "application/json",
      bodyText: "{}"
    }]
  }
});
document.body.innerText = "Codex analytics page text with enough content";
observerCallback();
messageHandler({
  source: pageWindow,
  data: {
    type: "codexUsageApiResponses",
    responses: [{
      source: "page-fetch",
      url: "https://chatgpt.com/backend-api/wham/usage",
      requestSequence: 2,
      status: 401,
      contentType: "application/json",
      bodyText: JSON.stringify({ detail: "Unauthorized" })
    }]
  }
});
setTimeout(() => {
  const payload = messages[1] && messages[1].payload;
  if (fetched.length !== 8 || messages.length < 2 || !payload || !payload.apiResponses.some(
    (item) => item.url.endsWith("/backend-api/wham/usage")
  )) {
    throw new Error(JSON.stringify({ fetched, messages }));
  }
  process.exit(0);
}, 700);
"""

    result = subprocess.run(
        [node, "-e", harness, str(output / "content.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_generated_page_hook_replaces_stale_endpoint_response(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const messages = [];
let fetchCount = 0;
const window = {
  addEventListener() {},
  fetch: async () => ({
    clone() {
      const bodyText = JSON.stringify({ value: fetchCount++ === 0 ? "old" : "new" });
      return {
        status: 200,
        headers: { get() { return "application/json"; } },
        text: async () => bodyText
      };
    }
  }),
  postMessage(message) {
    messages.push(message);
  }
};
const sandbox = {
  window,
  location: { origin: "https://chatgpt.com" },
  URL,
  String,
  Object,
  Array,
  Promise,
  JSON,
  console,
  setInterval() { return 1; },
  clearInterval() {},
  setTimeout,
  clearTimeout
};
const boundsGuard = `
Array.prototype.map = function() { throw new Error("unbounded map collector used"); };
Array.prototype.flatMap = function() { throw new Error("unbounded flatMap collector used"); };
Object.getPrototypeOf([]).map = Array.prototype.map;
Object.getPrototypeOf([]).flatMap = Array.prototype.flatMap;
`;
vm.runInNewContext(boundsGuard + source, sandbox);
async function run() {
  await window.fetch("https://chatgpt.com/backend-api/wham/usage");
  await new Promise((resolve) => setTimeout(resolve, 0));
  await window.fetch("https://chatgpt.com/backend-api/wham/usage");
  await new Promise((resolve) => setTimeout(resolve, 20));
  const responses = messages.at(-1)?.responses || [];
  if (responses.length !== 1 || JSON.parse(responses[0].bodyText).value !== "new") {
    throw new Error(JSON.stringify({ messages, responses }));
  }
}
run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
setTimeout(() => process.exit(process.exitCode || 0), 100);
"""

    result = subprocess.run(
        [node, "-e", harness, str(output / "page-hook.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_generated_page_hook_bounds_streaming_response_clone(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const messages = [];
let cancelled = 0;
const encoder = new TextEncoder();
const prefix = '{"rate_limit":{"label":"Ä","padding":"';
const boundaryPadding = "x".repeat(65536 - encoder.encode(prefix).length - 1);
const oversized = encoder.encode(
  prefix + boundaryPadding + "Ä" + "x".repeat(2000100) + '"}}'
);
let maxDecoderInput = 0;
class TrackingTextDecoder {
  constructor() { this.decoder = new TextDecoder(); }
  decode(value, options) {
    maxDecoderInput = Math.max(maxDecoderInput, value ? value.length : 0);
    return this.decoder.decode(value, options);
  }
}
function makeResponse() {
  return {
    clone() {
      let offset = 0;
      return {
        status: 200,
        headers: { get() { return "application/json"; } },
        body: {
          getReader() {
            return {
              async read() {
                if (offset >= oversized.length) {
                  return { done: true, value: undefined };
                }
                const chunkSize = oversized.length;
                const end = Math.min(offset + chunkSize, oversized.length);
                const value = oversized.slice(offset, end);
                offset = end;
                return { done: false, value };
              },
              async cancel() { cancelled += 1; }
            };
          }
        }
      };
    }
  };
}
const window = {
  addEventListener() {},
  fetch: async () => makeResponse(),
  postMessage(message) {
    messages.push(message);
  }
};
const sandbox = {
  window,
  location: { origin: "https://chatgpt.com" },
  Number,
  String,
  Object,
  Array,
  Promise,
  JSON,
  URL,
  TextDecoder: TrackingTextDecoder,
  console,
  setInterval() { return 1; },
  clearInterval() {},
  setTimeout,
  clearTimeout
};
vm.runInNewContext(source, sandbox);
async function run() {
  await window.fetch("https://chatgpt.com/backend-api/wham/usage");
  await new Promise((resolve) => setTimeout(resolve, 100));
  const responses = messages.at(-1)?.responses || [];
  if (
    responses.length !== 1
    || responses[0].truncated !== true
    || responses[0].bodyText !== ""
    || !responses[0].bodyExcerpt.includes("Ä")
    || cancelled !== 1
    || maxDecoderInput > 65536
  ) {
    throw new Error(JSON.stringify({ messages, responses, cancelled, maxDecoderInput }));
  }
}
run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
setTimeout(() => process.exit(process.exitCode || 0), 300);
"""

    result = subprocess.run(
        [node, "-e", harness, str(output / "page-hook.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_generated_page_hook_enforces_api_response_aggregate_budget(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const messages = [];
const huge = "x".repeat(1100000);
function makeResponse(url) {
  const bodyText = url.endsWith("/usage")
    ? JSON.stringify({ rate_limit: { primary_window: { used_percent: 10 } } })
    : huge;
  return {
    clone() {
      return {
        status: 200,
        headers: { get() { return "application/json"; } },
        text: async () => bodyText
      };
    }
  };
}
const window = {
  addEventListener() {},
  fetch: async (url) => makeResponse(url),
  postMessage(message) { messages.push(message); }
};
const sandbox = {
  window,
  location: { origin: "https://chatgpt.com" },
  Number,
  String,
  Object,
  Array,
  Promise,
  JSON,
  URL,
  TextDecoder,
  console,
  setInterval() { return 1; },
  clearInterval() {},
  setTimeout,
  clearTimeout
};
vm.runInNewContext(source, sandbox);
async function run() {
  await window.fetch("https://chatgpt.com/backend-api/wham/usage");
  for (let index = 0; index < 4; index += 1) {
    await window.fetch(`https://chatgpt.com/backend-api/wham/other-${index}`);
  }
  await new Promise((resolve) => setTimeout(resolve, 100));
  const responses = messages.at(-1)?.responses || [];
  const total = responses.reduce(
    (sum, item) => sum + String(item.bodyText || "").length,
    0
  );
  if (
    total > 4000000
    || !responses.some((item) => item.url.endsWith("/wham/usage"))
  ) {
    throw new Error(JSON.stringify({ responses: responses.length, total }));
  }
}
run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
setTimeout(() => process.exit(process.exitCode || 0), 500);
"""

    result = subprocess.run(
        [node, "-e", harness, str(output / "page-hook.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_generated_page_hook_ignores_late_older_endpoint_response(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const messages = [];
let fetchCount = 0;
let releaseFirst;
const firstGate = new Promise((resolve) => { releaseFirst = resolve; });
function makeResponse(value) {
  return {
    clone() {
      return {
        status: 200,
        headers: { get() { return "application/json"; } },
        text: async () => JSON.stringify({ value })
      };
    }
  };
}
const window = {
  addEventListener() {},
  fetch: async () => {
    const call = fetchCount++;
    if (call === 0) {
      await firstGate;
    }
    return makeResponse(call === 0 ? "old" : "new");
  },
  postMessage(message) {
    messages.push(message);
  }
};
const sandbox = {
  window,
  location: { origin: "https://chatgpt.com" },
  Number,
  String,
  Object,
  Array,
  Promise,
  JSON,
  URL,
  console,
  setInterval() { return 1; },
  clearInterval() {},
  setTimeout,
  clearTimeout
};
vm.runInNewContext(source, sandbox);
async function run() {
  const first = window.fetch("https://chatgpt.com/backend-api/wham/usage");
  await new Promise((resolve) => setTimeout(resolve, 0));
  const second = window.fetch("https://chatgpt.com/backend-api/wham/usage");
  await second;
  await new Promise((resolve) => setTimeout(resolve, 0));
  releaseFirst();
  await first;
  await new Promise((resolve) => setTimeout(resolve, 20));
  const responses = messages.at(-1)?.responses || [];
  if (responses.length !== 1 || JSON.parse(responses[0].bodyText).value !== "new") {
    throw new Error(JSON.stringify({ messages, responses }));
  }
}
run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
setTimeout(() => process.exit(process.exitCode || 0), 100);
"""

    result = subprocess.run(
        [node, "-e", harness, str(output / "page-hook.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_generated_page_hook_discards_late_response_from_previous_refresh_epoch(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const messages = [];
let messageHandler = null;
let fetchCount = 0;
let releaseLateOld;
let releaseFresh;
const lateOldGate = new Promise((resolve) => { releaseLateOld = resolve; });
const freshGate = new Promise((resolve) => { releaseFresh = resolve; });
function makeResponse(value) {
  return {
    status: 200,
    ok: true,
    clone() {
      return {
        status: 200,
        ok: true,
        headers: { get() { return "application/json"; } },
        text: async () => JSON.stringify({ value })
      };
    }
  };
}
const window = {
  addEventListener(type, callback) {
    if (type === "message") {
      messageHandler = callback;
    }
  },
  fetch: async () => {
    const call = fetchCount++;
    if (call === 1) {
      await lateOldGate;
      return makeResponse("late-old");
    }
    if (call === 2) {
      await freshGate;
      return makeResponse("fresh");
    }
    return makeResponse("baseline");
  },
  postMessage(message) {
    messages.push(message);
  }
};
const sandbox = {
  window,
  location: { origin: "https://chatgpt.com" },
  Number,
  String,
  Object,
  Array,
  Promise,
  JSON,
  URL,
  console,
  setInterval() { return 1; },
  clearInterval() {},
  setTimeout,
  clearTimeout
};
vm.runInNewContext(source, sandbox);
if (!messageHandler) {
  throw new Error("page hook did not install its message handler");
}
async function waitForTasks(milliseconds = 20) {
  await new Promise((resolve) => setTimeout(resolve, milliseconds));
}
async function run() {
  messageHandler({
    source: window,
    data: { type: "codexUsageRefresh", requestId: "refresh-1" }
  });
  await waitForTasks();
  const afterBaseline = messages.length;

  const lateOld = window.fetch("https://chatgpt.com/backend-api/wham/usage");
  await waitForTasks(0);
  messageHandler({
    source: window,
    data: { type: "codexUsageRefresh", requestId: "refresh-2" }
  });
  await waitForTasks(0);
  releaseLateOld();
  await lateOld;
  await waitForTasks();

  const lateMessages = messages.slice(afterBaseline);
  if (lateMessages.some((message) => (message.responses || []).some(
    (item) => item.url.endsWith("/backend-api/wham/usage")
      && JSON.parse(item.bodyText || "{}").value === "late-old"
  ))) {
    throw new Error(JSON.stringify({ messages, lateMessages }));
  }

  releaseFresh();
  await waitForTasks();
  const freshMessage = messages.find((message) => message.requestId === "refresh-2");
  const freshUsage = freshMessage && freshMessage.responses.find(
    (item) => item.url.endsWith("/backend-api/wham/usage")
  );
  if (!freshUsage || JSON.parse(freshUsage.bodyText).value !== "fresh") {
    throw new Error(JSON.stringify({ messages, freshMessage, freshUsage }));
  }
}
run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
setTimeout(() => process.exit(process.exitCode || 0), 200);
"""

    result = subprocess.run(
        [node, "-e", harness, str(output / "page-hook.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_generated_page_hook_answers_refresh_request_with_fresh_usage(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const messages = [];
let messageHandler = null;
const window = {
  addEventListener(type, callback) {
    if (type === "message") {
      messageHandler = callback;
    }
  },
  fetch: async () => ({
    clone() {
      return {
        status: 200,
        ok: true,
        headers: { get() { return "application/json"; } },
        text: async () => JSON.stringify({
          rate_limit: {
            primary_window: { used_percent: 22, limit_window_seconds: 18000 },
            secondary_window: { used_percent: 62, limit_window_seconds: 604800 }
          }
        })
      };
    }
  }),
  postMessage(message) {
    messages.push(message);
  }
};
const sandbox = {
  window,
  location: { origin: "https://chatgpt.com" },
  Number,
  String,
  Object,
  Array,
  Promise,
  JSON,
  URL,
  console,
  setInterval() { return 1; },
  clearInterval() {},
  setTimeout,
  clearTimeout
};
vm.runInNewContext(source, sandbox);
if (!messageHandler) {
  throw new Error("page hook did not install its message handler");
}
messageHandler({
  source: window,
  data: { type: "codexUsageRefresh", requestId: "refresh-1" }
});
setTimeout(() => {
  const message = messages.find((item) => item.requestId === "refresh-1");
  const usage = message && message.responses.find(
    (item) => item.url.endsWith("/backend-api/wham/usage")
  );
  const body = usage && JSON.parse(usage.bodyText);
  if (!message || !usage || body.rate_limit.primary_window.used_percent !== 22) {
    throw new Error(JSON.stringify({ messages }));
  }
  process.exit(0);
}, 50);
"""

    result = subprocess.run(
        [node, "-e", harness, str(output / "page-hook.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr


def test_write_bridge_extension_rejects_symlink_output_dir(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    output_link = tmp_path / "extension"
    output_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="extension output directory"):
        write_bridge_extension(
            "BW_Privat",
            output_link,
            endpoint="http://127.0.0.1:8765/ingest",
            interval_seconds=300,
        )

    assert not (outside / "manifest.json").exists()


def test_write_bridge_extension_fails_closed_when_output_directory_cannot_be_secured(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "extension"
    original_chmod = bridge_module.Path.chmod

    def fail_output_chmod(path, mode):
        if path == output_dir:
            raise OSError("simulated extension chmod failure")
        return original_chmod(path, mode)

    monkeypatch.setattr(bridge_module.Path, "chmod", fail_output_chmod)

    with pytest.raises(ValueError, match="secure extension output directory"):
        write_bridge_extension(
            "BW_Privat",
            output_dir,
            endpoint="http://127.0.0.1:8765/ingest",
            interval_seconds=300,
            token="A" * 43,
        )

    assert not (output_dir / "manifest.json").exists()


def test_write_bridge_extension_rejects_symlink_output_file(tmp_path):
    output_dir = tmp_path / "extension"
    output_dir.mkdir()
    outside = tmp_path / "outside.js"
    outside.write_text("keep", encoding="utf-8")
    (output_dir / "content.js").symlink_to(outside)

    with pytest.raises(ValueError, match="extension output path"):
        write_bridge_extension(
            "BW_Privat",
            output_dir,
            endpoint="http://127.0.0.1:8765/ingest",
            interval_seconds=300,
        )

    assert outside.read_text(encoding="utf-8") == "keep"
