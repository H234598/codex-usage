from __future__ import annotations

import base64
import json
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from codex_usage.app_server import (
    AppServerProtocolError,
    AppServerUnavailableError,
    _LineReader,
    _missing_usage_limits_error,
    _should_refresh,
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


def _fake_codex(
    path: Path,
    requests_path: Path,
    *,
    reject_initial_account_read: bool = False,
    account_plan_type: str | None = None,
    account_email: str | None = None,
) -> str:
    reject_initial = str(reject_initial_account_read)
    plan_field = f", 'planType': {account_plan_type!r}" if account_plan_type else ""
    email_field = f", 'email': {account_email!r}" if account_email else ""
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
                    "account": {{"type": "chatgpt"{plan_field}{email_field}}},
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


def test_app_server_rejects_server_plan_mismatch(tmp_path):
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    _auth(auth_path, datetime.now(UTC) + timedelta(hours=1), plan_type="free")
    command = _fake_codex(
        tmp_path / "codex",
        tmp_path / "requests.json",
        account_plan_type="enterprise",
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


def test_app_server_rejects_server_email_mismatch(tmp_path):
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
        account_email="other@example.com",
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


def test_app_server_reports_missing_five_hour_window(
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

    assert usage.status == AccountStatus.PARTIAL
    assert usage.five_hour is None
    assert usage.weekly is not None and usage.weekly.remaining == 53
    assert usage.error == (
        "5h limit unavailable in app server response "
        "(plan plus; available window weekly)"
    )


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
