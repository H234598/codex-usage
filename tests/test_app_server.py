from __future__ import annotations

import base64
import json
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codex_usage.app_server import (
    AppServerProtocolError,
    AppServerUnavailableError,
    _LineReader,
    _should_refresh,
    _stop_process,
    _windows_from_response,
    fetch_account_usage_app_server,
)
from codex_usage.models import Account, AccountStatus


def _jwt(expiry: datetime) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(expiry.timestamp())}).encode()
    ).rstrip(b"=")
    return f"e30.{payload.decode()}.signature"


def _auth(path: Path, expiry: datetime, account_id: str = "account-test") -> None:
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": _jwt(expiry),
                    "id_token": _jwt(expiry),
                    "refresh_token": "refresh-test",
                    "account_id": account_id,
                },
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _fake_codex(
    path: Path,
    requests_path: Path,
    *,
    reject_initial_account_read: bool = False,
) -> str:
    reject_initial = str(reject_initial_account_read)
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
                    "account": {{"type": "chatgpt"}},
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
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)
    return str(path)


def test_app_server_fetch_uses_only_account_methods(tmp_path):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    _auth(auth_path, datetime.now(UTC) + timedelta(hours=1))
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
    assert usage.backend_used == "app-server"
    assert usage.backend_account_id == "account-test"
    assert usage.five_hour is not None and usage.five_hour.remaining == 83
    assert usage.weekly is not None and usage.weekly.remaining == 58
    methods = [item["method"] for item in json.loads(requests_path.read_text())]
    assert methods == [
        "initialize",
        "initialized",
        "account/read",
        "account/rateLimits/read",
    ]
    assert not any(method.startswith(("thread/", "turn/")) for method in methods)


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


def test_refresh_window_is_fifteen_minutes():
    now = datetime.now(UTC)
    assert _should_refresh(now + timedelta(minutes=14), now=now) is True
    assert _should_refresh(now + timedelta(minutes=16), now=now) is False


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
