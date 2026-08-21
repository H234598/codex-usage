from __future__ import annotations

import base64
import json
import queue
import signal
import socket
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import codex_usage.app_server as app_server_module
from codex_usage.app_server import (
    AppServerAuthError,
    AppServerFetchError,
    AppServerProtocolError,
    AppServerUnavailableError,
    _auth_email_changed,
    _LineReader,
    _missing_usage_limits_error,
    _resolve_codex,
    _response_for,
    _send,
    _should_refresh,
    _StderrReader,
    _stop_process,
    _window,
    _windows_from_response,
    fetch_account_usage_app_server,
)
from codex_usage.models import Account, AccountStatus, LimitWindow


def _jwt(
    expiry: datetime,
    *,
    plan_type: str | None = None,
    email: str | None = None,
) -> str:
    payload = {"exp": int(expiry.timestamp())}
    if plan_type is not None:
        payload["https://api.openai.com/auth"] = {"chatgpt_plan_type": plan_type}
    if email is not None:
        payload["email"] = email
    payload = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).rstrip(b"=")
    return f"e30.{payload.decode()}.signature"


def _auth(
    path: Path,
    expiry: datetime,
    account_id: str = "account-test",
    *,
    plan_type: str | None = None,
    email: str | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": _jwt(expiry, plan_type=plan_type, email=email),
                    "id_token": _jwt(expiry, plan_type=plan_type, email=email),
                    "refresh_token": "refresh-test",
                    "account_id": account_id,
                },
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_app_server_symlink_check_rejects_dotdot_bypass(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AppServerAuthError, match="must not contain symlinks"):
        app_server_module._assert_no_symlink_ancestors(redirected / ".." / "target")


def test_app_server_symlink_check_scans_after_missing_segment(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AppServerAuthError, match="must not contain symlinks"):
        app_server_module._assert_no_symlink_ancestors(
            tmp_path / "missing" / ".." / "redirected" / "target"
        )


def _fake_codex(
    path: Path,
    requests_path: Path,
    *,
    reject_initial_account_read: bool = False,
    account_plan_type: str | None = None,
    account_email: str | None = None,
    account_credits: str | None = None,
    model_id: str = "gpt-5.3-codex-spark",
) -> str:
    reject_initial = str(reject_initial_account_read)
    plan_field = f", 'planType': {account_plan_type!r}" if account_plan_type else ""
    email_field = f", 'email': {account_email!r}" if account_email else ""
    credits_field = (
        f", 'credits': {{'has_credits': True, 'unlimited': False, "
        f"'balance': {account_credits!r}}}"
        if account_credits is not None
        else ""
    )
    source = f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

requests = []
for line in sys.stdin:
    message = json.loads(line)
    requests.append(message)
    Path({str(requests_path)!r}).write_text(json.dumps(requests), encoding="utf-8")
    method = message.get("method")
    if method == "initialize":
        print(json.dumps({{"id": message["id"], "result": {{}}}}), flush=True)
    elif method == "account/read":
        if {reject_initial} and not message.get("params", {{}}).get("refreshToken"):
            response = {{
                "id": message["id"],
                "error": {{"code": 401, "message": "unauthorized"}},
            }}
        else:
            response = {{
                "id": message["id"],
                "result": {{
                    "account": {{"type": "chatgpt"{plan_field}{email_field}{credits_field}}},
                    "requiresOpenaiAuth": True,
                }},
            }}
        print(json.dumps(response), flush=True)
    elif method == "account/rateLimits/read":
        response = {{
            "id": message["id"],
            "result": {{
                "rateLimits": {{
                    "primary": {{
                        "usedPercent": 17,
                        "windowDurationMins": 300,
                        "resetsAt": 1780000000,
                    }},
                    "secondary": {{
                        "usedPercent": 42,
                        "windowDurationMins": 10080,
                        "resetsAt": 1780500000,
                    }},
                }}
            }},
        }}
        print(json.dumps(response), flush=True)
    elif method == "model/list":
        print(json.dumps({{
            "id": message["id"],
            "result": {{"data": [{{
                "id": {model_id!r},
                "model": {model_id!r},
            }}]}},
        }}), flush=True)
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)
    return str(path)


def test_app_server_send_honors_deadline_when_stdin_is_full():
    sender, receiver = socket.socketpair()
    sender.setblocking(False)
    try:
        while True:
            sender.send(b"x" * 65_536)
    except BlockingIOError:
        pass

    try:
        with pytest.raises(AppServerFetchError, match="timed out"):
            _send(
                SimpleNamespace(stdin=sender),
                {"method": "initialize", "id": 1},
                deadline=time.monotonic() + 0.05,
            )
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize(
    "timeout_seconds",
    (
        pytest.param(True, id="bool"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
        pytest.param("1", id="string"),
        pytest.param(10**10_000, id="huge-int"),
    ),
)
def test_app_server_rejects_invalid_timeout_before_process_start(
    tmp_path, monkeypatch, timeout_seconds
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    command = tmp_path / "codex"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o700)

    def fail_start(*_args, **_kwargs):
        pytest.fail("app server process must not start")

    monkeypatch.setattr(app_server_module, "_start_app_server", fail_start)

    with pytest.raises(AppServerFetchError, match="positive finite"):
        app_server_module._read_rate_limits(
            codex_home,
            refresh=False,
            timeout_seconds=timeout_seconds,
            codex_command=str(command),
            expected_plan_type=None,
            expected_email=None,
        )


def test_app_server_fetch_uses_only_account_methods(tmp_path):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    _auth(
        auth_path,
        datetime.now(UTC) + timedelta(hours=1),
        email="same@example.com",
    )
    requests_path = tmp_path / "requests.json"
    command = _fake_codex(
        tmp_path / "codex",
        requests_path,
        account_email="same@example.com",
    )
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
        backend="app-server",
    )

    usage = fetch_account_usage_app_server(account, codex_command=command)

    assert usage.status == AccountStatus.OK
    assert usage.backend_used == "app-server"
    assert usage.backend_account_id == "account-test"
    assert usage.five_hour is not None and usage.five_hour.remaining == 83
    assert usage.weekly is not None and usage.weekly.remaining == 58
    spark = usage.model_pool("gpt-5.3-codex-spark")
    assert spark is not None
    assert spark.windows == ()
    assert spark.availability_sources == ("model_catalog",)
    methods = [item["method"] for item in json.loads(requests_path.read_text())]
    assert methods == [
        "initialize",
        "initialized",
        "account/read",
        "account/rateLimits/read",
        "model/list",
    ]
    assert not any(method.startswith(("thread/", "turn/")) for method in methods)


def test_app_server_preserves_absolute_account_credits(tmp_path):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    _auth(auth_path, datetime.now(UTC) + timedelta(hours=1))
    command = _fake_codex(
        tmp_path / "codex",
        tmp_path / "requests.json",
        account_credits="794",
    )
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
        backend="app-server",
    )

    usage = fetch_account_usage_app_server(account, codex_command=command)

    assert usage.credits is not None
    assert usage.credits.remaining == 794


def test_app_server_rejects_normalized_model_identity(tmp_path):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    _auth(auth_path, datetime.now(UTC) + timedelta(hours=1))
    command = _fake_codex(
        tmp_path / "codex",
        tmp_path / "requests.json",
        model_id="gpt-5.3-codex-spark ",
    )
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
        backend="app-server",
    )

    usage = fetch_account_usage_app_server(account, codex_command=command)

    assert usage.status == AccountStatus.ERROR
    assert usage.error == "app server model id is invalid"
    assert usage.five_hour is None
    assert usage.weekly is None


def test_app_server_requests_refresh_for_expiring_token(tmp_path):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    _auth(auth_path, datetime.now(UTC) + timedelta(minutes=5))
    requests_path = tmp_path / "requests.json"
    command = _fake_codex(tmp_path / "codex", requests_path)
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
        backend="app-server",
    )

    usage = fetch_account_usage_app_server(account, codex_command=command)

    assert usage.status == AccountStatus.OK
    requests = json.loads(requests_path.read_text())
    account_read = next(item for item in requests if item["method"] == "account/read")
    assert account_read["params"]["refreshToken"] is True


def test_app_server_refreshes_when_initial_account_read_is_unauthorized(tmp_path):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    _auth(auth_path, datetime.now(UTC) + timedelta(hours=1))
    requests_path = tmp_path / "requests.json"
    command = _fake_codex(
        tmp_path / "codex",
        requests_path,
        reject_initial_account_read=True,
    )
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
        backend="app-server",
    )

    usage = fetch_account_usage_app_server(account, codex_command=command)

    assert usage.status == AccountStatus.OK
    requests = json.loads(requests_path.read_text())
    account_reads = [item for item in requests if item["method"] == "account/read"]
    assert [item["params"]["refreshToken"] for item in account_reads] == [False, True]


def test_app_server_rejects_auth_identity_changed_during_rate_limit_read(
    tmp_path, monkeypatch
):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    expiry = datetime.now(UTC) + timedelta(hours=1)
    _auth(auth_path, expiry, account_id="old-account")
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
        backend="app-server",
    )

    def mutate_auth(*_args, **_kwargs):
        _auth(auth_path, expiry, account_id="new-account")
        return {
            "rateLimits": {
                "primary": {"usedPercent": 17, "windowDurationMins": 300},
                "secondary": {"usedPercent": 42, "windowDurationMins": 10080},
            }
        }

    monkeypatch.setattr("codex_usage.app_server._read_rate_limits", mutate_auth)

    usage = fetch_account_usage_app_server(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "auth.json identity changed during rate-limit request"
    assert usage.cache_invalidated is True


def test_app_server_protocol_failure_invalidates_cache(monkeypatch, tmp_path):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
        backend="app-server",
    )
    monkeypatch.setattr(
        "codex_usage.app_server._auth_context",
        lambda _account: (tmp_path / "auth.json", {}, "user", "account", "free", None),
    )
    monkeypatch.setattr(
        "codex_usage.app_server._read_rate_limits",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AppServerProtocolError("malformed app-server response")
        ),
    )

    usage = fetch_account_usage_app_server(account)

    assert usage.status == AccountStatus.ERROR
    assert usage.error == "malformed app-server response"
    assert usage.cache_invalidated is True


def test_app_server_rejects_auth_plan_change_during_rate_limit_read(
    tmp_path, monkeypatch
):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    expiry = datetime.now(UTC) + timedelta(hours=1)
    _auth(auth_path, expiry, account_id="same-account", plan_type="free")
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
        backend="app-server",
    )

    def mutate_auth(*_args, **_kwargs):
        _auth(auth_path, expiry, account_id="same-account", plan_type="enterprise")
        return {
            "rateLimits": {
                "primary": {"usedPercent": 17, "windowDurationMins": 300},
                "secondary": {"usedPercent": 42, "windowDurationMins": 10080},
            }
        }

    monkeypatch.setattr("codex_usage.app_server._read_rate_limits", mutate_auth)

    usage = fetch_account_usage_app_server(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "auth.json plan type changed during rate-limit request"


def test_app_server_rejects_missing_auth_plan_after_rate_limit_read(
    tmp_path, monkeypatch
):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    expiry = datetime.now(UTC) + timedelta(hours=1)
    _auth(auth_path, expiry, account_id="same-account", plan_type="free")
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
        backend="app-server",
    )

    def mutate_auth(*_args, **_kwargs):
        _auth(auth_path, expiry, account_id="same-account")
        return {
            "rateLimits": {
                "primary": {"usedPercent": 17, "windowDurationMins": 300},
                "secondary": {"usedPercent": 42, "windowDurationMins": 10080},
            }
        }

    monkeypatch.setattr("codex_usage.app_server._read_rate_limits", mutate_auth)

    usage = fetch_account_usage_app_server(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "auth.json plan type changed during rate-limit request"


def test_app_server_requires_configured_auth_json(tmp_path):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        backend="app-server",
    )

    usage = fetch_account_usage_app_server(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "account has no auth_json_path"


def test_app_server_rejects_nonstandard_auth_json_filename(tmp_path):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "work-auth.json"
    _auth(auth_path, datetime.now(UTC) + timedelta(hours=1))
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
        backend="app-server",
    )

    usage = fetch_account_usage_app_server(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "app-server requires auth_json_path filename auth.json"


@pytest.mark.parametrize("account_plan_type", ["enterprise", " free "])
def test_app_server_rejects_server_plan_mismatch(tmp_path, account_plan_type):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    _auth(auth_path, datetime.now(UTC) + timedelta(hours=1), plan_type="free")
    command = _fake_codex(
        tmp_path / "codex",
        tmp_path / "requests.json",
        account_plan_type=account_plan_type,
    )
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
        backend="app-server",
    )

    usage = fetch_account_usage_app_server(account, codex_command=command)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "Codex app server plan type differs from auth.json"


@pytest.mark.parametrize("account_email", ["other@example.com", " expected@example.com"])
def test_app_server_rejects_server_email_mismatch(tmp_path, account_email):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    _auth(
        auth_path,
        datetime.now(UTC) + timedelta(hours=1),
        email="expected@example.com",
    )
    command = _fake_codex(
        tmp_path / "codex",
        tmp_path / "requests.json",
        account_email=account_email,
    )
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
        backend="app-server",
    )

    usage = fetch_account_usage_app_server(account, codex_command=command)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "Codex app server email differs from auth.json"


def test_app_server_rejects_auth_without_account_identity(tmp_path):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": _jwt(datetime.now(UTC) + timedelta(hours=1)),
                    "id_token": _jwt(datetime.now(UTC) + timedelta(hours=1)),
                    "refresh_token": "refresh-test",
                },
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
        backend="app-server",
    )

    usage = fetch_account_usage_app_server(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "auth.json has no account identity"


def test_app_server_rejects_invalid_access_token_expiry(tmp_path):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    invalid_payload = base64.urlsafe_b64encode(
        json.dumps({"exp": "not-a-number"}).encode()
    ).rstrip(b"=")
    invalid_token = f"e30.{invalid_payload.decode()}.signature"
    _auth(auth_path, datetime.now(UTC) + timedelta(hours=1))
    payload = json.loads(auth_path.read_text(encoding="utf-8"))
    payload["tokens"]["access_token"] = invalid_token
    auth_path.write_text(json.dumps(payload), encoding="utf-8")
    auth_path.chmod(0o600)
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
        backend="app-server",
    )

    usage = fetch_account_usage_app_server(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == f"auth.json access_token expiry is invalid: {auth_path}"
    assert usage.cache_invalidated is True


def test_response_for_rejects_non_integer_request_id_aliases():
    reader = type("Reader", (), {})()
    reader.items = queue.Queue()
    reader.items.put(b'{"id":true,"result":{}}')

    with pytest.raises(AppServerFetchError) as error:
        _response_for(
            reader,
            1,
            deadline=time.monotonic() + 0.01,
            stderr_reader=object(),
        )

    assert "timed out" in str(error.value)


def test_response_for_rejects_explicit_null_error():
    reader = type("Reader", (), {})()
    reader.items = queue.Queue()
    reader.items.put(b'{"id":1,"result":{},"error":null}')

    with pytest.raises(AppServerProtocolError) as error:
        _response_for(
            reader,
            1,
            deadline=time.monotonic() + 0.01,
            stderr_reader=object(),
        )

    assert "invalid error" in str(error.value)


def test_app_server_missing_command_is_compatibility_failure(tmp_path):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    _auth(auth_path, datetime.now(UTC) + timedelta(hours=1))
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
        backend="app-server",
    )

    with pytest.raises(AppServerUnavailableError):
        fetch_account_usage_app_server(account, codex_command=str(tmp_path / "missing"))


@pytest.mark.parametrize("explicit", ["", " ", [], False])
def test_resolve_codex_rejects_explicit_invalid_values(explicit, tmp_path, monkeypatch):
    fallback = tmp_path / "codex"
    fallback.write_text("#!/bin/sh\n", encoding="utf-8")
    fallback.chmod(0o700)
    monkeypatch.setattr(app_server_module.shutil, "which", lambda _name: str(fallback))

    with pytest.raises(AppServerUnavailableError, match="codex command is invalid"):
        _resolve_codex(explicit)


def test_app_server_rejects_symlinked_codex_home(tmp_path):
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    auth_path = real_home / "auth.json"
    _auth(auth_path, datetime.now(UTC) + timedelta(hours=1))
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(linked_home / "auth.json"),
        backend="app-server",
    )

    usage = fetch_account_usage_app_server(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "CODEX_HOME must not contain symlinks"


def test_window_mapping_prefers_codex_limit_bucket():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {},
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 1, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 2, "windowDurationMins": 10080},
                }
            },
        }
    )

    assert five is not None and five.used == 1
    assert weekly is not None and weekly.used == 2


def test_window_reset_timestamp_uses_dst_aware_local_zone(monkeypatch):
    berlin = ZoneInfo("Europe/Berlin")
    monkeypatch.setattr("codex_usage.app_server.LOCAL_TZ", berlin)
    expected = datetime(2026, 10, 26, 0, 15, tzinfo=berlin)

    window = _window(
        "five_hour",
        {"usedPercent": 1, "resetsAt": int(expected.timestamp())},
    )

    assert window.reset_at == expected


def test_window_mapping_merges_partial_codex_bucket_with_top_level_snapshot():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {"usedPercent": 9, "windowDurationMins": 300},
                "secondary": {"usedPercent": 4, "windowDurationMins": 10080},
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 1, "windowDurationMins": 300},
                }
            },
        }
    )

    assert five is not None and five.used == 1
    assert weekly is not None and weekly.used == 4


def test_window_mapping_keeps_complete_top_level_bucket_over_partial_codex_bucket():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {"usedPercent": 9, "windowDurationMins": 300},
                "secondary": {"usedPercent": 4, "windowDurationMins": 10080},
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 1},
                    "secondary": {"usedPercent": 2, "windowDurationMins": 10080},
                }
            },
        }
    )

    assert five is not None and five.used == 9
    assert weekly is not None and weekly.used == 2


def test_window_mapping_does_not_infer_partial_codex_over_unsupported_top_level():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {"usedPercent": 90, "windowDurationMins": 43_200},
                "secondary": {"usedPercent": 40, "windowDurationMins": 10_080},
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 1},
                    "secondary": {"usedPercent": 2, "windowDurationMins": 10_080},
                }
            },
        }
    )

    assert five is None
    assert weekly is not None and weekly.used == 2


@pytest.mark.parametrize(
    ("primary", "secondary"),
    (
        (
            {"usedPercent": 7, "windowDurationMins": 300},
            {"usedPercent": 18, "windowDurationMins": "invalid"},
        ),
        (
            {"usedPercent": 7, "windowDurationMins": "invalid"},
            {"usedPercent": 18, "windowDurationMins": 10_080},
        ),
    ),
)
def test_window_mapping_does_not_infer_explicit_invalid_duration(primary, secondary):
    five, weekly = _windows_from_response(
        {"rateLimits": {"primary": primary, "secondary": secondary}}
    )

    assert five is None if primary["windowDurationMins"] == "invalid" else five is not None
    assert weekly is None if secondary["windowDurationMins"] == "invalid" else weekly is not None


def test_window_mapping_rejects_two_explicit_invalid_durations():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {"usedPercent": 7, "windowDurationMins": "invalid"},
                "secondary": {"usedPercent": 18, "windowDurationMins": "invalid"},
            }
        }
    )

    assert five is None
    assert weekly is None


def test_window_mapping_keeps_complete_top_level_over_incomplete_codex_bucket():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {"usedPercent": 9, "windowDurationMins": 300},
                "secondary": {"usedPercent": 4, "windowDurationMins": 10_080},
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"windowDurationMins": 300},
                    "secondary": {
                        "usedPercent": 2,
                        "windowDurationMins": 10_080,
                    },
                }
            },
        }
    )

    assert five is not None and five.used == 9
    assert weekly is not None and weekly.used == 2


def test_window_mapping_does_not_let_unsupported_codex_bucket_hide_top_level_window():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {"usedPercent": 9, "windowDurationMins": 300},
                "secondary": {"usedPercent": 4, "windowDurationMins": 10080},
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 1, "windowDurationMins": 43_200},
                    "secondary": {"usedPercent": 2, "windowDurationMins": 10080},
                }
            },
        }
    )

    assert five is not None and five.used == 9
    assert weekly is not None and weekly.used == 2


def test_window_mapping_ignores_invalid_codex_duration_without_top_level_fallback():
    five, weekly = _windows_from_response(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 1, "windowDurationMins": "300"},
                    "secondary": {"usedPercent": 2, "windowDurationMins": 10080},
                }
            }
        }
    )

    assert five is None
    assert weekly is not None and weekly.used == 2


@pytest.mark.parametrize("value", [[], "malformed", 42])
def test_window_mapping_rejects_malformed_limit_map(value):
    with pytest.raises(
        AppServerProtocolError,
        match="rateLimitsByLimitId is not an object",
    ):
        _windows_from_response(
            {
                "rateLimits": {
                    "primary": {"usedPercent": 1, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 2, "windowDurationMins": 10080},
                },
                "rateLimitsByLimitId": value,
            }
        )


def test_window_mapping_keeps_weekly_only_bucket_as_weekly():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "secondary": {
                    "usedPercent": 12,
                    "windowDurationMins": 10080,
                }
            }
        }
    )

    assert five is None
    assert weekly is not None and weekly.used == 12


def test_window_mapping_keeps_single_secondary_without_duration_as_weekly():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "secondary": {
                    "usedPercent": 12,
                }
            }
        }
    )

    assert five is None
    assert weekly is not None and weekly.used == 12


def test_window_mapping_keeps_unknown_duration_for_known_primary_bucket():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {
                    "usedPercent": 7,
                    "windowDurationMins": 300,
                },
                "secondary": {
                    "usedPercent": 18,
                },
            }
        }
    )

    assert five is not None and five.used == 7
    assert weekly is not None and weekly.used == 18


def test_window_mapping_keeps_unknown_duration_for_known_secondary_bucket():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {
                    "usedPercent": 7,
                },
                "secondary": {
                    "usedPercent": 18,
                    "windowDurationMins": 10080,
                },
            }
        }
    )

    assert five is not None and five.used == 7
    assert weekly is not None and weekly.used == 18


def test_window_mapping_rejects_unsupported_single_duration():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {
                    "usedPercent": 5,
                    "windowDurationMins": 43_200,
                    "resetsAt": 1786342835,
                }
            }
        }
    )

    assert five is None
    assert weekly is None


def test_window_mapping_does_not_label_unsupported_duration_as_weekly():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {
                    "usedPercent": 1,
                    "windowDurationMins": 300,
                    "resetsAt": 1783769000,
                },
                "secondary": {
                    "usedPercent": 5,
                    "windowDurationMins": 43_200,
                    "resetsAt": 1786342835,
                },
            }
        }
    )

    assert five is not None and five.used == 1
    assert weekly is None


def test_window_mapping_does_not_infer_weekly_after_unsupported_primary_duration():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {
                    "usedPercent": 5,
                    "windowDurationMins": 43_200,
                },
                "secondary": {
                    "usedPercent": 18,
                },
            }
        }
    )

    assert five is None
    assert weekly is None


def test_window_mapping_rejects_duplicate_known_durations():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {
                    "usedPercent": 7,
                    "windowDurationMins": 300,
                },
                "secondary": {
                    "usedPercent": 18,
                    "windowDurationMins": 300,
                },
            }
        }
    )

    assert five is None
    assert weekly is None


def test_window_mapping_keeps_valid_window_when_other_used_percent_is_invalid():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {"usedPercent": 7, "windowDurationMins": 300},
                "secondary": {
                    "usedPercent": "invalid",
                    "windowDurationMins": 10080,
                },
            }
        }
    )

    assert five is not None and five.used == 7
    assert weekly is None


def test_window_mapping_keeps_usage_when_reset_timestamp_is_unusable():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {
                    "usedPercent": 7,
                    "windowDurationMins": 300,
                    "resetsAt": 10**100,
                },
                "secondary": {
                    "usedPercent": 18,
                    "windowDurationMins": 10080,
                },
            }
        }
    )

    assert five is not None and five.used == 7 and five.reset_at is None
    assert weekly is not None and weekly.used == 18


def test_window_mapping_falls_back_when_codex_bucket_is_empty():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {"usedPercent": 3, "windowDurationMins": 300},
                "secondary": {"usedPercent": 4, "windowDurationMins": 10080},
            },
            "rateLimitsByLimitId": {"codex": {}},
        }
    )

    assert five is not None and five.used == 3
    assert weekly is not None and weekly.used == 4


def test_app_server_missing_window_error_identifies_available_weekly_limit():
    five, weekly = _windows_from_response(
        {
            "rateLimits": {
                "primary": {"usedPercent": 47, "windowDurationMins": 10080},
                "secondary": None,
            }
        }
    )

    assert _missing_usage_limits_error(
        {"rateLimits": {"primary": {"usedPercent": 47, "windowDurationMins": 10080}}},
        "pro",
        five,
        weekly,
    ) == "5h limit unavailable in app server response (plan plus; available window weekly)"


def test_app_server_accepts_missing_five_hour_compatibility_window(
    tmp_path,
    monkeypatch,
):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    _auth(
        auth_path,
        datetime.now(UTC) + timedelta(hours=1),
        plan_type="pro",
    )
    command = _fake_codex(
        tmp_path / "codex",
        tmp_path / "requests.json",
        account_plan_type="pro",
    )
    monkeypatch.setattr(
        "codex_usage.app_server._windows_from_response",
        lambda payload: (
            None,
            LimitWindow(
                name="weekly",
                used=47,
                limit=100,
                remaining=53,
                percent=53,
            ),
        ),
    )
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
        backend="app-server",
    )

    usage = fetch_account_usage_app_server(account, codex_command=command)

    assert usage.status == AccountStatus.OK
    assert usage.five_hour is None
    assert usage.weekly is not None and usage.weekly.remaining == 53
    assert usage.error is None
    assert usage.main is not None


def test_app_server_accepts_weekly_only_bucket_without_duration(tmp_path, monkeypatch):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
        backend="app-server",
    )
    auth_context = (tmp_path / "auth.json", {}, "user-test", "account-test", "pro", None)
    monkeypatch.setattr(
        "codex_usage.app_server._auth_context",
        lambda _account: auth_context,
    )
    monkeypatch.setattr(
        "codex_usage.app_server._read_rate_limits",
        lambda *_args, **_kwargs: {
            "rateLimits": {"secondary": {"usedPercent": 47}},
        },
    )

    usage = fetch_account_usage_app_server(account)

    assert usage.status == AccountStatus.OK
    assert usage.five_hour is None
    assert usage.weekly is not None and usage.weekly.remaining == 53
    assert usage.main is not None and usage.main.has_valid_usage is True


def test_app_server_marks_malformed_main_slot_partial(tmp_path, monkeypatch):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
        backend="app-server",
    )
    payload = {
        "rateLimits": {
            "primary": "malformed",
            "secondary": {
                "usedPercent": 20,
                "windowDurationMins": 10080,
            },
        }
    }
    auth_context = (tmp_path / "auth.json", {}, "user-test", "account-test", "pro", None)
    monkeypatch.setattr(
        "codex_usage.app_server._auth_context",
        lambda _account: auth_context,
    )
    monkeypatch.setattr(
        "codex_usage.app_server._read_rate_limits",
        lambda *_args, **_kwargs: payload,
    )

    usage = fetch_account_usage_app_server(account)

    assert usage.status == AccountStatus.PARTIAL
    assert usage.main is not None
    assert usage.main.available is False
    assert usage.weekly is not None and usage.weekly.remaining == 80


def test_app_server_marks_reset_only_main_slot_partial(tmp_path, monkeypatch):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
        backend="app-server",
    )
    payload = {
        "rateLimits": {
            "primary": {"windowDurationMins": 300},
            "secondary": {
                "usedPercent": 20,
                "windowDurationMins": 10080,
            },
        }
    }
    auth_context = (tmp_path / "auth.json", {}, "user-test", "account-test", "pro", None)
    monkeypatch.setattr(
        "codex_usage.app_server._auth_context",
        lambda _account: auth_context,
    )
    monkeypatch.setattr(
        "codex_usage.app_server._read_rate_limits",
        lambda *_args, **_kwargs: payload,
    )

    usage = fetch_account_usage_app_server(account)

    assert usage.status == AccountStatus.PARTIAL
    assert usage.main is not None
    assert usage.main.available is True
    assert usage.main.windows[0].has_usage_value is False
    assert usage.weekly is not None and usage.weekly.remaining == 80


def test_app_server_missing_window_error_reports_unsupported_duration():
    payload = {
        "rateLimits": {
            "primary": {"usedPercent": 5, "windowDurationMins": 43200},
        }
    }
    five, weekly = _windows_from_response(payload)

    assert _missing_usage_limits_error(payload, "free", five, weekly) == (
        "requested 5h/weekly limits unavailable in app server response "
        "(plan free; backend window 43200m)"
    )


def test_refresh_window_is_fifteen_minutes():
    now = datetime.now(UTC)
    assert _should_refresh(now + timedelta(minutes=14), now=now) is True
    assert _should_refresh(now + timedelta(minutes=16), now=now) is False


@pytest.mark.parametrize(
    ("before", "after", "changed"),
    [
        ("user@example.test", None, True),
        (None, "user@example.test", True),
        ("user@example.test", "user@example.test", False),
        ("user@example.test", " user@example.test", True),
        ("user@example.test", "user@example.test ", True),
        ("user@example.test", "other@example.test", True),
    ],
)
def test_auth_email_change_rejects_missing_or_different_email(before, after, changed):
    assert _auth_email_changed(before, after) is changed


def test_stop_process_terminates_isolated_process_group(monkeypatch):
    calls = []

    class FakeProcess:
        pid = 1234
        stdin = None

        def poll(self):
            return None

        def wait(self, timeout):
            calls.append(("wait", timeout))

        def terminate(self):
            raise AssertionError("process fallback must not be used")

    monkeypatch.setattr(
        "codex_usage.app_server.os.killpg",
        lambda pid, signum: calls.append((pid, signum)),
    )

    _stop_process(FakeProcess())

    assert calls == [(1234, signal.SIGTERM), ("wait", 2)]


def test_stop_process_signals_group_after_parent_exit(monkeypatch):
    calls = []

    class FakeProcess:
        pid = 1234
        stdin = None

        def poll(self):
            return 0

        def terminate(self):
            raise AssertionError("exited parent must not use process fallback")

    monkeypatch.setattr(
        "codex_usage.app_server.os.killpg",
        lambda pid, signum: calls.append((pid, signum)),
    )

    _stop_process(FakeProcess())

    assert calls == [(1234, signal.SIGTERM)]


def test_stop_process_ignores_exit_races():
    class FakeProcess:
        stdin = None

        def poll(self):
            return None

        def terminate(self):
            raise ProcessLookupError

    _stop_process(FakeProcess())


def test_reader_cleanup_closes_streams_and_joins_reader_threads(monkeypatch):
    class BlockingStream:
        def __init__(self):
            self.started = Event()
            self.release = Event()
            self.closed = False

        def readline(self, _limit):
            self.started.set()
            self.release.wait(timeout=2)
            return b""

        def read(self, _limit):
            self.started.set()
            self.release.wait(timeout=2)
            return b""

        def close(self):
            self.closed = True
            self.release.set()

    class FakeStdin:
        def close(self):
            return None

    stdout = BlockingStream()
    stderr = BlockingStream()
    line_reader = _LineReader(stdout)
    stderr_reader = _StderrReader(stderr)
    line_reader.start()
    stderr_reader.start()
    assert stdout.started.wait(timeout=2)
    assert stderr.started.wait(timeout=2)

    class FakeProcess:
        pid = 1234
        stdin = FakeStdin()

        def __init__(self):
            self.stdout = stdout
            self.stderr = stderr

        def poll(self):
            return 0

    monkeypatch.setattr("codex_usage.app_server.os.killpg", lambda *_args: None)
    process = FakeProcess()
    try:
        _stop_process(process, readers=(line_reader, stderr_reader))
    finally:
        stdout.close()
        stderr.close()
        line_reader.join(timeout=2)
        stderr_reader.join(timeout=2)

    assert stdout.closed is True
    assert stderr.closed is True
    assert not line_reader.is_alive()
    assert not stderr_reader.is_alive()


def test_line_reader_does_not_block_on_full_message_queue():
    class FakeStream:
        def readline(self, _limit):
            return b"second\n"

    reader = _LineReader(FakeStream())
    for _ in range(reader.items.maxsize):
        reader.items.put(b"first\n")
    reader.run()

    items = [reader.items.get_nowait() for _ in range(reader.items.qsize())]
    errors = [item for item in items if isinstance(item, AppServerProtocolError)]
    assert errors
    assert "too many pending messages" in str(errors[0])


def test_line_reader_keeps_oversize_error_when_queue_is_full():
    class FakeStream:
        def readline(self, _limit):
            return b"x" * (2_000_000 + 1)

    reader = _LineReader(FakeStream())
    for _ in range(reader.items.maxsize):
        reader.items.put(b"first\n")
    reader.run()

    items = [reader.items.get_nowait() for _ in range(reader.items.qsize())]
    errors = [item for item in items if isinstance(item, AppServerProtocolError)]
    assert errors
    assert "response is too large" in str(errors[0])


def test_line_reader_reports_closed_pipe_errors():
    class ClosedStream:
        def readline(self, _limit):
            raise ValueError("I/O operation on closed file")

    reader = _LineReader(ClosedStream())
    reader.run()

    item = reader.items.get_nowait()
    assert isinstance(item, AppServerProtocolError)
    assert "could not read" in str(item)
