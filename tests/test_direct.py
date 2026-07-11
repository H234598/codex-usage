from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

from codex_usage.direct import MAX_AUTH_JSON_BYTES, _jwt_expiry, fetch_account_usage_direct
from codex_usage.models import Account, AccountStatus


def _jwt_with_exp(expiry: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode("utf-8")).rstrip(
        b"="
    ).decode()
    return f"{header}.{payload}.signature"


def test_jwt_expiry_ignores_non_object_payloads():
    for claims in ([], None, "not-an-object"):
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).rstrip(b"=")
        token = f"e30.{payload.decode('ascii')}.signature"

        assert _jwt_expiry(token) is None


def test_fetch_account_usage_direct_uses_auth_json_access_token(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"tokens": {"access_token": "secret-access-token"}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
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


def test_fetch_account_usage_direct_rejects_broad_auth_json_permissions(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"tokens": {"access_token": "secret-access-token"}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o644)

    def fake_urlopen(request, *, timeout):
        raise AssertionError("network must not be reached with broad auth permissions")

    monkeypatch.setattr("codex_usage.direct.urlopen", fake_urlopen)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error is not None
    assert "permissions too broad" in usage.error
    assert "secret-access-token" not in usage.error


def test_fetch_account_usage_direct_rejects_symlink_auth_json(tmp_path, monkeypatch):
    target = tmp_path / "target-auth.json"
    target.write_text(
        json.dumps({"tokens": {"access_token": "secret-access-token"}}),
        encoding="utf-8",
    )
    target.chmod(0o600)
    auth_path = tmp_path / "auth.json"
    auth_path.symlink_to(target)

    def fake_urlopen(request, *, timeout):
        raise AssertionError("network must not be reached with symlink auth")

    monkeypatch.setattr("codex_usage.direct.urlopen", fake_urlopen)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error is not None
    assert "auth.json is not a regular file" in usage.error
    assert "secret-access-token" not in usage.error


def test_fetch_account_usage_direct_rejects_oversized_auth_json(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(" " * (MAX_AUTH_JSON_BYTES + 1), encoding="utf-8")
    auth_path.chmod(0o600)

    def fake_urlopen(request, *, timeout):
        raise AssertionError("network must not be reached with oversized auth")

    monkeypatch.setattr("codex_usage.direct.urlopen", fake_urlopen)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error is not None
    assert "auth.json too large" in usage.error


def test_fetch_account_usage_direct_rejects_non_json_content_type(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"tokens": {"access_token": "secret-access-token"}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    class FakeResponse:
        def __init__(self):
            self.headers = {"content-type": "text/html; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit):
            return json.dumps({"rate_limit": {}}).encode("utf-8")

    def fake_urlopen(request, *, timeout):
        return FakeResponse()

    monkeypatch.setattr("codex_usage.direct.urlopen", fake_urlopen)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.ERROR
    assert usage.error == "direct response is not JSON content"


def test_fetch_account_usage_direct_marks_expired_auth_before_network(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    expired_at = int(datetime.now(tz=UTC).timestamp()) - 3600
    auth_path.write_text(
        json.dumps({"tokens": {"access_token": _jwt_with_exp(expired_at)}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    def fake_urlopen(request, *, timeout):
        raise AssertionError("network must not be reached for expired auth")

    monkeypatch.setattr("codex_usage.direct.urlopen", fake_urlopen)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error is not None
    assert "expired" in usage.error
    assert "reactivate privat" in usage.error


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
