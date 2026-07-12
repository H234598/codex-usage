from __future__ import annotations

import base64
import json
import shutil
import subprocess
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pytest

from codex_usage.bridge import (
    _make_handler,
    bridge_token_for_account,
    bridge_token_matches,
    ingest_and_save,
    load_latest_usages,
    render_bridge_snippet,
    revoke_bridge_token,
    save_bridge_debug_payload,
    usage_from_ingest_payload,
    write_bridge_extension,
)
from codex_usage.config import AppConfig, add_or_update_account, save_config
from codex_usage.models import Account, AccountStatus, AccountUsage, LimitWindow
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
    assert usage.error is not None
    assert "missing page text" in usage.error
    assert "ready=complete" in usage.error
    assert "textLength=0" in usage.error


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
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
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
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
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
                    "bodyText": json.dumps({"user_id": "user-test"}),
                }
            ],
            "api_responses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json",
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
                    "bodyText": json.dumps({"user_id": "user-test"}),
                }
            ],
        },
    )

    assert usage.status == AccountStatus.PARTIAL
    assert usage.five_hour is None
    assert usage.weekly is None


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
                    "bodyText": json.dumps({"detail": "Unauthorized"}),
                },
                {
                    "source": "content-probe",
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json",
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


def test_usage_from_ingest_payload_prefers_latest_response_across_sources():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")

    def response(source: str, five_hour: int, weekly: int, sequence: int) -> dict[str, object]:
        return {
            "source": source,
            "url": "https://chatgpt.com/backend-api/wham/usage",
            "status": 200,
            "contentType": "application/json",
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


def test_usage_from_ingest_payload_canonicalizes_personal_account_identity(tmp_path):
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

    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json",
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

    assert usage.backend_user_id == "user-test"
    assert usage.backend_account_id == "account-uuid"


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

    with pytest.raises(ValueError, match="ambiguous backend account identity"):
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
                "apiResponses": [
                    {
                        "url": "https://chatgpt.com/backend-api/wham/usage",
                        "status": 200,
                        "contentType": "application/json",
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
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json",
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
    assert '"Authorization": "Bearer " + token' in snippet
    assert token in snippet


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
    assert "document.body.innerText" in content
    assert "sanitizedCodexUsageRoot" in content
    assert "script, style, link, meta, noscript, template" in content
    assert "sanitizedRoot.outerHTML" in content
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
    assert "requestSequence" in page_hook
    assert "/backend-api/wham/" in page_hook
    assert 'source: "page-fetch"' in page_hook


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
        "apiResponses": [
            {
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "status": 200,
                "contentType": "application/json",
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

    def post(headers=None):
        request = Request(endpoint, data=body, method="POST", headers=headers or {})
        return urlopen(request, timeout=5)

    try:
        with pytest.raises(HTTPError) as missing:
            post()
        assert missing.value.code == 401
        with pytest.raises(HTTPError) as wrong:
            post({"Authorization": "Bearer wrong-token"})
        assert wrong.value.code == 401
        with post({"Authorization": f"Bearer {token}"}) as response:
            assert response.status == 200
            assert json.loads(response.read())["status"] == "ok"
        assert bridge_token_matches(account.id, token) is True
        assert revoke_bridge_token(account.id) is True
        with pytest.raises(HTTPError) as revoked:
            post({"Authorization": f"Bearer {token}"})
        assert revoked.value.code == 401
        replacement = bridge_token_for_account(account.id)
        assert replacement != token
        with post({"Authorization": f"Bearer {replacement}"}) as response:
            assert response.status == 200
            assert json.loads(response.read())["status"] == "ok"
        options = Request(
            endpoint,
            method="OPTIONS",
            headers={
                "Origin": "https://chatgpt.com",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        with urlopen(options, timeout=5) as response:
            assert response.status == 204
            assert "Authorization" in response.headers["Access-Control-Allow-Headers"]
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
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json",
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
            headers={"Authorization": f"Bearer {second_token}"},
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
const document = {
  title: "Codex",
  readyState: "complete",
  body: { innerText: text },
  documentElement: {
    cloneNode() {
      return {
        textContent: text,
        outerHTML: "<html><body>Codex</body></html>",
        querySelectorAll() { return { forEach() {} }; }
      };
    }
  },
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
vm.runInNewContext(source, sandbox);
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
const document = {
  title: "Codex",
  readyState: "complete",
  body: { innerText: text },
  documentElement: {
    cloneNode() {
      return {
        textContent: text,
        outerHTML: "<html><body>Codex</body></html>",
        querySelectorAll() { return { forEach() {} }; }
      };
    }
  },
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
const sandbox = {
  window: { addEventListener() {} },
  document: {
    title: "Codex",
    readyState: "complete",
    body: { innerText: text },
    documentElement: {
      cloneNode() {
        return {
          textContent: text,
          outerHTML: "<html><body>Codex</body></html>",
          querySelectorAll() { return { forEach() {} }; }
        };
      }
    },
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
  documentElement: {
    cloneNode() {
      return {
        textContent: document.body.innerText,
        outerHTML: "<html><body>Codex</body></html>",
        querySelectorAll() { return { forEach() {} }; }
      };
    }
  },
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
vm.runInNewContext(source, sandbox);
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
