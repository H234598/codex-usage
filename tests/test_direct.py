from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest

from codex_usage.direct import (
    MAX_AUTH_JSON_BYTES,
    DirectAuthError,
    _jwt_expiry,
    auth_identity_from_payload,
    fetch_account_usage_direct,
)
from codex_usage.models import Account, AccountStatus


def _jwt_with_exp(expiry: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode("utf-8")).rstrip(
        b"="
    ).decode()
    return f"{header}.{payload}.signature"


def _jwt_with_claims(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


def test_jwt_expiry_ignores_non_object_payloads():
    for claims in ([], None, "not-an-object"):
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).rstrip(b"=")
        token = f"e30.{payload.decode('ascii')}.signature"

        assert _jwt_expiry(token) is None


def test_auth_identity_rejects_conflicting_id_and_access_tokens(tmp_path):
    path = tmp_path / "auth.json"
    payload = {
        "tokens": {
            "id_token": _jwt_with_claims(
                {"https://api.openai.com/auth": {"chatgpt_user_id": "old-user"}}
            ),
            "access_token": _jwt_with_claims(
                {"https://api.openai.com/auth": {"chatgpt_user_id": "new-user"}}
            ),
            "account_id": "account-uuid",
        }
    }

    with pytest.raises(DirectAuthError, match="token identities disagree"):
        auth_identity_from_payload(payload, path=path)


def test_fetch_account_usage_direct_uses_auth_json_access_token(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "secret-access-token",
                    "id_token": _jwt_with_claims(
                        {"https://api.openai.com/auth": {"chatgpt_user_id": "user-test"}}
                    ),
                    "account_id": "server-account",
                }
            }
        ),
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
                    },
                    "user_id": "user-test",
                    "account_id": "user-test",
                }
            ).encode("utf-8")

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["account_id"] = request.get_header("Chatgpt-account-id")
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
        "account_id": "server-account",
        "timeout": 7,
    }
    assert usage.status == AccountStatus.OK
    assert usage.five_hour is not None
    assert usage.five_hour.remaining == 97
    assert usage.weekly is not None
    assert usage.weekly.remaining == 55
    assert usage.backend_user_id == "user-test"
    assert usage.backend_account_id == "server-account"


def test_fetch_account_usage_direct_prefers_majority_response(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "secret-access-token",
                    "id_token": _jwt_with_claims(
                        {"https://api.openai.com/auth": {"chatgpt_user_id": "user-test"}}
                    ),
                    "account_id": "server-account",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    stable = {
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
        },
        "user_id": "user-test",
        "account_id": "server-account",
    }
    transient = {
        **stable,
        "rate_limit": {
            "primary_window": {
                "used_percent": 80,
                "limit_window_seconds": 18000,
                "reset_at": 1780894850,
            },
            "secondary_window": {
                "used_percent": 90,
                "limit_window_seconds": 604800,
                "reset_at": 1781061350,
            },
        },
    }
    responses = iter((transient, stable, stable))
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.OK
    assert usage.five_hour is not None
    assert usage.five_hour.remaining == 97
    assert usage.weekly is not None
    assert usage.weekly.remaining == 55


def test_fetch_account_usage_direct_rejects_auth_identity_changed_during_request(
    tmp_path, monkeypatch
):
    auth_path = tmp_path / "auth.json"

    def write_auth(user_id: str, account_id: str) -> None:
        auth_path.write_text(
            json.dumps(
                {
                    "tokens": {
                        "access_token": _jwt_with_claims(
                            {"https://api.openai.com/auth": {"chatgpt_user_id": user_id}}
                        ),
                        "id_token": _jwt_with_claims(
                            {"https://api.openai.com/auth": {"chatgpt_user_id": user_id}}
                        ),
                        "account_id": account_id,
                    }
                }
            ),
            encoding="utf-8",
        )
        auth_path.chmod(0o600)

    write_auth("old-user", "old-account")

    def fake_fetch(*_args, **_kwargs):
        write_auth("new-user", "new-account")
        return {
            "user_id": "old-user",
            "account_id": "old-account",
            "rate_limit": {
                "primary_window": {"used_percent": 3, "limit_window_seconds": 18000},
                "secondary_window": {"used_percent": 45, "limit_window_seconds": 604800},
            },
        }

    monkeypatch.setattr("codex_usage.direct._fetch_wham_usage", fake_fetch)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "auth.json identity changed during usage request"


def test_fetch_account_usage_direct_rejects_response_from_different_account(
    tmp_path,
    monkeypatch,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "secret-access-token",
                    "id_token": _jwt_with_claims(
                        {"https://api.openai.com/auth": {"chatgpt_user_id": "user-test"}}
                    ),
                    "account_id": "server-account",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit):
            return json.dumps(
                {
                    "rate_limit": {},
                    "user_id": "user-test",
                    "account_id": "other-account",
                }
            ).encode("utf-8")

    monkeypatch.setattr(
        "codex_usage.direct.urlopen",
        lambda request, *, timeout: FakeResponse(),
    )
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.ERROR
    assert usage.error == "backend response belongs to a different account"


def test_fetch_account_usage_direct_accepts_same_account_with_different_user_id(
    tmp_path,
    monkeypatch,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "secret-access-token",
                    "id_token": _jwt_with_claims(
                        {"https://api.openai.com/auth": {"chatgpt_user_id": "auth-user"}}
                    ),
                    "account_id": "server-account",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

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
                            "limit_window_seconds": 18_000,
                        },
                        "secondary_window": {
                            "used_percent": 45,
                            "limit_window_seconds": 604_800,
                        },
                    },
                    "user_id": "response-user",
                    "account_id": "server-account",
                }
            ).encode("utf-8")

    monkeypatch.setattr(
        "codex_usage.direct.urlopen",
        lambda request, *, timeout: FakeResponse(),
    )
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.OK
    assert usage.backend_user_id == "auth-user"
    assert usage.backend_account_id == "server-account"


@pytest.mark.parametrize("account_id", ["account\nforged", " ", 42])
def test_fetch_account_usage_direct_rejects_invalid_auth_account_id(
    tmp_path,
    monkeypatch,
    account_id,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "secret-access-token",
                    "account_id": account_id,
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    def fake_urlopen(request, *, timeout):
        raise AssertionError("network must not be reached for invalid account id")

    monkeypatch.setattr("codex_usage.direct.urlopen", fake_urlopen)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == f"auth.json account_id is invalid: {auth_path}"


def test_fetch_account_usage_direct_marks_reset_only_windows_partial(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"tokens": {"access_token": "secret-access-token"}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    class FakeResponse:
        def __init__(self):
            self.headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit):
            return json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {
                            "limit_window_seconds": 18000,
                            "reset_at": 1893456000,
                        },
                        "secondary_window": {
                            "limit_window_seconds": 604800,
                            "reset_at": 1893456000,
                        },
                    }
                }
            ).encode("utf-8")

    monkeypatch.setattr("codex_usage.direct.urlopen", lambda request, *, timeout: FakeResponse())
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.PARTIAL
    assert usage.error == "usage limits not found in direct response"


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
