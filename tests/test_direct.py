from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest

from codex_usage.direct import (
    MAX_AUTH_JSON_BYTES,
    DirectAuthError,
    DirectFetchError,
    _fetch_stable_wham_usage,
    _jwt_expiry,
    _select_stable_wham_usage,
    auth_identity_changed,
    auth_identity_from_payload,
    canonical_backend_identity,
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


def test_auth_identity_rejects_changed_user_with_same_account():
    assert auth_identity_changed(
        before_user_id="old-user",
        before_account_id="shared-account",
        after_user_id="new-user",
        after_account_id="shared-account",
    ) is True
    assert auth_identity_changed(
        before_user_id="same-user",
        before_account_id="shared-account",
        after_user_id="same-user",
        after_account_id="shared-account",
    ) is False
    assert auth_identity_changed(
        before_user_id=None,
        before_account_id="shared-account",
        after_user_id="new-user",
        after_account_id="shared-account",
    ) is True
    assert auth_identity_changed(
        before_user_id="old-user",
        before_account_id="shared-account",
        after_user_id=None,
        after_account_id="shared-account",
    ) is True


def test_canonical_backend_identity_rejects_foreign_account_without_auth_account_id():
    with pytest.raises(ValueError, match="backend response belongs to a different account"):
        canonical_backend_identity(
            "shared-user",
            "foreign-account",
            auth_user_id="shared-user",
            auth_account_id=None,
            require_backend_identity=True,
        )


def test_canonical_backend_identity_rejects_ambiguous_shared_user_account():
    with pytest.raises(ValueError, match="ambiguous account identity"):
        canonical_backend_identity(
            "shared-user",
            "shared-user",
            auth_user_id="shared-user",
            auth_account_id="enterprise-account",
            auth_plan_type="enterprise",
            backend_plan_type="enterprise",
            require_backend_identity=True,
            reject_ambiguous_backend_identity=True,
        )


def test_fetch_account_usage_direct_uses_auth_json_access_token(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "secret-access-token",
                    "id_token": _jwt_with_claims(
                        {
                            "https://api.openai.com/auth": {
                                "chatgpt_user_id": "user-test",
                                "chatgpt_plan_type": "plus",
                            }
                        }
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
                    "plan_type": "pro",
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


def test_select_stable_wham_usage_does_not_choose_empty_majority():
    complete = {
        "user_id": "user-test",
        "account_id": "account-test",
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
    }
    empty = {
        "user_id": "user-test",
        "account_id": "account-test",
        "rate_limit": {},
    }

    with pytest.raises(DirectFetchError, match="inconsistent"):
        _select_stable_wham_usage([complete, empty, empty])


def test_select_stable_wham_usage_does_not_choose_reset_only_majority():
    complete = {
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
    reset_only = {
        "rate_limit": {
            "primary_window": {
                "limit_window_seconds": 18000,
                "reset_at": 1780894250,
            },
            "secondary_window": {
                "limit_window_seconds": 604800,
                "reset_at": 1781060750,
            },
        }
    }

    with pytest.raises(DirectFetchError, match="inconsistent"):
        _select_stable_wham_usage([complete, reset_only, reset_only])


def test_fetch_stable_wham_usage_groups_dynamic_reset_buckets(monkeypatch):
    responses = iter(
        (
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "limit_window_seconds": 18000,
                        "reset_at": 1783829134,
                        "reset_after_seconds": 18000,
                    },
                    "secondary_window": {
                        "used_percent": 0,
                        "limit_window_seconds": 604800,
                        "reset_at": 1784415934,
                        "reset_after_seconds": 604800,
                    },
                }
            },
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "limit_window_seconds": 18000,
                        "reset_at": 1783829134,
                        "reset_after_seconds": 18000,
                    },
                    "secondary_window": {
                        "used_percent": 51,
                        "limit_window_seconds": 604800,
                        "reset_at": 1784280925,
                        "reset_after_seconds": 469832,
                    },
                }
            },
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "limit_window_seconds": 18000,
                        "reset_at": 1783829135,
                        "reset_after_seconds": 18000,
                    },
                    "secondary_window": {
                        "used_percent": 51,
                        "limit_window_seconds": 604800,
                        "reset_at": 1784280925,
                        "reset_after_seconds": 469832,
                    },
                }
            },
        )
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    payload = _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)

    assert payload["rate_limit"]["primary_window"]["used_percent"] == 1
    assert payload["rate_limit"]["secondary_window"]["used_percent"] == 51


def test_fetch_stable_wham_usage_keeps_backend_identities_in_separate_groups(
    monkeypatch,
):
    def response(account_id: str) -> dict:
        return {
            "user_id": "shared-user",
            "account_id": account_id,
            "rate_limit": {
                "primary_window": {
                    "used_percent": 55,
                    "limit_window_seconds": 18000,
                    "reset_at": 1783829134,
                },
                "secondary_window": {
                    "used_percent": 10,
                    "limit_window_seconds": 604800,
                    "reset_at": 1784415934,
                },
            },
        }

    responses = iter(
        (response("foreign-account"), response("expected-account"), response("expected-account"))
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    payload = _fetch_stable_wham_usage("token", account_id="expected-account", timeout_seconds=1)

    assert payload["account_id"] == "expected-account"


def test_fetch_stable_wham_usage_tolerates_decreasing_relative_reset_after(
    monkeypatch,
):
    responses = iter(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 1,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": reset_after,
                    "reset_at": 1783829134,
                },
                "secondary_window": {
                    "used_percent": 51,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 469832,
                    "reset_at": 1784280925,
                },
            }
        }
        for reset_after in (18000, 17999, 17998)
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    payload = _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)

    assert payload["rate_limit"]["primary_window"]["used_percent"] == 1
    assert payload["rate_limit"]["secondary_window"]["used_percent"] == 51


def test_fetch_stable_wham_usage_accepts_progressive_relative_reset_without_absolute_reset(
    monkeypatch,
):
    responses = iter(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": used,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": reset_after,
                },
                "secondary_window": {
                    "used_percent": 51,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 469832 - index,
                },
            }
        }
        for index, (used, reset_after) in enumerate(
            ((3, 13665), (4, 13664), (5, 13663))
        )
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    payload = _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)

    assert payload["rate_limit"]["primary_window"]["used_percent"] == 5


def test_fetch_stable_wham_usage_keeps_latest_monotonic_progress(monkeypatch):
    def response(used: int) -> dict:
        return {
            "rate_limit": {
                "primary_window": {
                    "used_percent": used,
                    "limit_window_seconds": 18000,
                    "reset_at": 1783829134,
                },
                "secondary_window": {
                    "used_percent": 13,
                    "limit_window_seconds": 604800,
                    "reset_at": 1784354562,
                },
            }
        }

    responses = iter((response(22), response(22), response(23)))
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    payload = _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)

    assert payload["rate_limit"]["primary_window"]["used_percent"] == 23


def test_fetch_stable_wham_usage_accepts_quorum_latest_large_progress(monkeypatch):
    def response(used: int) -> dict:
        return {
            "user_id": "same-user",
            "account_id": "same-account",
            "rate_limit": {
                "primary_window": {
                    "used_percent": used,
                    "limit_window_seconds": 18000,
                    "reset_at": 1783829134,
                },
                "secondary_window": {
                    "used_percent": 13,
                    "limit_window_seconds": 604800,
                    "reset_at": 1784354562,
                },
            },
        }

    responses = iter((response(10), response(10), response(30)))
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    payload = _fetch_stable_wham_usage("token", account_id="same-account", timeout_seconds=1)

    assert payload["rate_limit"]["primary_window"]["used_percent"] == 30


def test_fetch_stable_wham_usage_rejects_large_progress_without_quorum(monkeypatch):
    def response(used: int) -> dict:
        return {
            "user_id": "same-user",
            "account_id": "same-account",
            "rate_limit": {
                "primary_window": {
                    "used_percent": used,
                    "limit_window_seconds": 18000,
                    "reset_at": 1783829134,
                },
                "secondary_window": {
                    "used_percent": 13,
                    "limit_window_seconds": 604800,
                    "reset_at": 1784354562,
                },
            },
        }

    responses = iter((response(10), response(30), response(40)))
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(DirectFetchError, match="inconsistent"):
        _fetch_stable_wham_usage("token", account_id="same-account", timeout_seconds=1)


def test_fetch_stable_wham_usage_rejects_missing_quorum(monkeypatch):
    responses = iter(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": used,
                    "limit_window_seconds": 18000,
                    "reset_at": 1780894250 + index * 600,
                }
            }
        }
        for index, used in enumerate((3, 4, 5))
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(DirectFetchError, match="inconsistent"):
        _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)


def test_fetch_stable_wham_usage_rejects_reset_identity_regression(monkeypatch):
    def response(used: int, reset_at: int) -> dict:
        return {
            "rate_limit": {
                "primary_window": {
                    "used_percent": used,
                    "limit_window_seconds": 18000,
                    "reset_at": reset_at,
                }
            }
        }

    responses = iter(
        (
            response(6, 1783824119),
            response(6, 1783824119),
            response(48, 1783824041),
        )
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(DirectFetchError, match="inconsistent"):
        _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)


def test_fetch_stable_wham_usage_retries_transient_reset_regression(monkeypatch):
    def response(used: int, reset_at: int) -> dict:
        return {
            "rate_limit": {
                "primary_window": {
                    "used_percent": used,
                    "limit_window_seconds": 18000,
                    "reset_at": reset_at,
                },
                "secondary_window": {
                    "used_percent": 51 if used > 1 else 1,
                    "limit_window_seconds": 604800,
                    "reset_at": 1784280925 if used > 1 else 1784281140,
                },
            }
        }

    responses = iter(
        (
            response(51, 1784280925),
            response(51, 1784280925),
            response(1, 1784281140),
            response(1, 1784281140),
            response(1, 1784281140),
            response(1, 1784281140),
        )
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )
    monkeypatch.setattr("codex_usage.direct.time.sleep", lambda _seconds: None)

    payload = _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)

    assert payload["rate_limit"]["primary_window"]["used_percent"] == 1
    assert payload["rate_limit"]["secondary_window"]["used_percent"] == 1


def test_fetch_stable_wham_usage_rejects_usage_regression_with_fixed_reset(
    monkeypatch,
):
    def response(used: int) -> dict:
        return {
            "rate_limit": {
                "primary_window": {
                    "used_percent": used,
                    "limit_window_seconds": 18000,
                    "reset_at": 1783824041,
                }
            }
        }

    responses = iter((response(54), response(54), response(1)))
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(DirectFetchError, match="inconsistent"):
        _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)


def test_fetch_stable_wham_usage_accepts_latest_relative_reset_transition(
    monkeypatch,
):
    def response(used: int, reset_after: int, reset_at: int) -> dict:
        return {
            "user_id": "user-test",
            "account_id": "account-test",
            "rate_limit": {
                "primary_window": {
                    "used_percent": used,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": reset_after,
                    "reset_at": reset_at,
                },
                "secondary_window": {
                    "used_percent": 10,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 604800,
                    "reset_at": 1784415934,
                },
            },
        }

    responses = iter(
        (
            response(5, 120, 1783860000),
            response(5, 118, 1783860000),
            response(0, 18000, 1783860180),
        )
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    payload = _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)

    assert payload["rate_limit"]["primary_window"]["used_percent"] == 0


def test_fetch_stable_wham_usage_accepts_reset_when_usage_percent_is_unchanged(
    monkeypatch,
):
    def response(reset_after: int, reset_at: int) -> dict:
        return {
            "user_id": "user-test",
            "account_id": "account-test",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 0,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": reset_after,
                    "reset_at": reset_at,
                },
                "secondary_window": {
                    "used_percent": 10,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 604800,
                    "reset_at": 1784415934,
                },
            },
        }

    responses = iter(
        (
            response(5, 1783860000),
            response(5, 1783860000),
            response(18000, 1783878000),
        )
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    payload = _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)

    primary = payload["rate_limit"]["primary_window"]
    assert primary["used_percent"] == 0
    assert primary["reset_after_seconds"] == 18000
    assert primary["reset_at"] == 1783878000


def test_fetch_stable_wham_usage_accepts_fixed_reset_after_quorum(
    monkeypatch,
):
    def response(used: int) -> dict:
        return {
            "rate_limit": {
                "primary_window": {
                    "used_percent": used,
                    "limit_window_seconds": 18000,
                    "reset_at": 1783824041,
                }
            }
        }

    responses = iter((response(54), response(1), response(1)))
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    payload = _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)

    assert payload["rate_limit"]["primary_window"]["used_percent"] == 1


def test_fetch_stable_wham_usage_accepts_progressive_same_window(monkeypatch):
    responses = iter(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": used,
                    "limit_window_seconds": 18000,
                    "reset_at": 1780894250,
                }
            }
        }
        for used in (3, 4, 5)
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    payload = _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)

    assert payload["rate_limit"]["primary_window"]["used_percent"] == 5


def test_fetch_stable_wham_usage_accepts_dynamic_reset_progression(monkeypatch):
    responses = iter(
        {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 1,
                    "limit_window_seconds": 18000,
                    "reset_at": 1780894250 + index * 6,
                    "reset_after_seconds": 18000,
                }
            }
        }
        for index in range(3)
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    payload = _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)

    assert payload["rate_limit"]["primary_window"]["used_percent"] == 1
    assert payload["rate_limit"]["primary_window"]["reset_at"] == 1780894250


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
    assert usage.backend_user_id is None
    assert usage.backend_account_id is None


def test_fetch_account_usage_direct_retries_after_rotated_auth_token(
    tmp_path,
    monkeypatch,
):
    auth_path = tmp_path / "auth.json"
    old_token = "old-access-token"
    new_token = "new-access-token"

    def write_auth(token: str) -> None:
        auth_path.write_text(
            json.dumps(
                {
                    "tokens": {
                        "access_token": token,
                        "id_token": _jwt_with_claims(
                            {"https://api.openai.com/auth": {"chatgpt_user_id": "user-a"}}
                        ),
                        "account_id": "account-a",
                    }
                }
            ),
            encoding="utf-8",
        )
        auth_path.chmod(0o600)

    write_auth(old_token)
    calls: list[str] = []

    def fake_fetch(token: str, **_kwargs):
        calls.append(token)
        if token == old_token:
            write_auth(new_token)
            raise DirectAuthError("direct auth failed: HTTP 401")
        return {
            "rate_limit": {
                "primary_window": {"used_percent": 3, "limit_window_seconds": 18000},
                "secondary_window": {"used_percent": 45, "limit_window_seconds": 604800},
            },
            "user_id": "user-a",
            "account_id": "account-a",
        }

    monkeypatch.setattr("codex_usage.direct._fetch_stable_wham_usage", fake_fetch)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert calls == [old_token, new_token]
    assert usage.status == AccountStatus.OK
    assert usage.five_hour is not None and usage.five_hour.remaining == 97
    assert usage.weekly is not None and usage.weekly.remaining == 55


def test_fetch_account_usage_direct_does_not_retry_unchanged_auth_after_401(
    tmp_path,
    monkeypatch,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"tokens": {"access_token": "same-token", "account_id": "account-a"}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    calls = []

    def fake_fetch(token: str, **_kwargs):
        calls.append(token)
        raise DirectAuthError("direct auth failed: HTTP 401")

    monkeypatch.setattr("codex_usage.direct._fetch_stable_wham_usage", fake_fetch)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert calls == ["same-token"]
    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "direct auth failed: HTTP 401"


def test_fetch_account_usage_direct_does_not_retry_expired_rotated_auth(
    tmp_path,
    monkeypatch,
):
    auth_path = tmp_path / "auth.json"
    expired_token = _jwt_with_exp(int(datetime.now(tz=UTC).timestamp()) - 60)
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "old-token",
                    "id_token": _jwt_with_claims(
                        {"https://api.openai.com/auth": {"chatgpt_user_id": "user-a"}}
                    ),
                    "account_id": "account-a",
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    calls = []

    def fake_fetch(token: str, **_kwargs):
        calls.append(token)
        auth_path.write_text(
            json.dumps(
                {
                    "tokens": {
                        "access_token": expired_token,
                        "id_token": _jwt_with_claims(
                            {"https://api.openai.com/auth": {"chatgpt_user_id": "user-a"}}
                        ),
                        "account_id": "account-a",
                    }
                }
            ),
            encoding="utf-8",
        )
        auth_path.chmod(0o600)
        raise DirectAuthError("direct auth failed: HTTP 403")

    monkeypatch.setattr("codex_usage.direct._fetch_stable_wham_usage", fake_fetch)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert calls == ["old-token"]
    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "direct auth failed: HTTP 403"


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


@pytest.mark.parametrize("plan_type", ["enterprise", None])
def test_fetch_account_usage_direct_rejects_shared_user_response_with_different_plan(
    tmp_path,
    monkeypatch,
    plan_type,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "secret-access-token",
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
                            "used_percent": 5,
                            "limit_window_seconds": 2_592_000,
                        }
                    },
                    "user_id": "shared-user",
                    "account_id": "shared-user",
                    "plan_type": plan_type,
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


def test_fetch_account_usage_direct_explains_unsupported_plan_window(tmp_path, monkeypatch):
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
                    "plan_type": "free",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 5,
                            "limit_window_seconds": 2_592_000,
                        },
                        "secondary_window": None,
                    },
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
    assert usage.five_hour is None
    assert usage.weekly is None
    assert usage.error == (
        "requested 5h/weekly limits unavailable in direct response "
        "(plan free; backend window 2592000s)"
    )


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
    assert usage.backend_user_id == "user-test"
    assert usage.backend_account_id == "server-account"


def test_fetch_account_usage_direct_keeps_auth_identity_on_transient_io_error(
    tmp_path, monkeypatch
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

    def fake_urlopen(request, *, timeout):
        raise OSError("temporary network failure")

    monkeypatch.setattr("codex_usage.direct.urlopen", fake_urlopen)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.ERROR
    assert usage.error == "direct fetch failed: I/O error"
    assert usage.backend_user_id == "user-test"
    assert usage.backend_account_id == "server-account"


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
