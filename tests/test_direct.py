from __future__ import annotations

import json

from codex_usage.direct import fetch_account_usage_direct
from codex_usage.models import Account, AccountStatus


def test_fetch_account_usage_direct_uses_auth_json_access_token(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"tokens": {"access_token": "secret-access-token"}}),
        encoding="utf-8",
    )
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit):
            return json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 3,
                            "limit_window_seconds": 18000,
                            "reset_at": 1780894250,
                        },
                        "secondary_window": {
                            "used_percent": 45,
                            "limit_window_seconds": 604800,
                            "reset_at": 1781060750,
                        },
                    }
                }
            ).encode("utf-8")

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("codex_usage.direct.urlopen", fake_urlopen)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account, timeout_seconds=7)

    assert captured == {
        "url": "https://chatgpt.com/backend-api/wham/usage",
        "authorization": "Bearer secret-access-token",
        "timeout": 7,
    }
    assert usage.status == AccountStatus.OK
    assert usage.five_hour is not None
    assert usage.five_hour.remaining == 97
    assert usage.weekly is not None
    assert usage.weekly.remaining == 55


def test_fetch_account_usage_direct_reports_missing_auth_json(tmp_path):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(tmp_path / "missing.json"),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error is not None
    assert "cannot read auth.json" in usage.error
