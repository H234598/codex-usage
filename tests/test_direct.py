from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from urllib.request import HTTPRedirectHandler
from zoneinfo import ZoneInfo

import pytest

import codex_usage.direct as direct_module
from codex_usage.direct import (
    MAX_AUTH_JSON_BYTES,
    DirectAuthError,
    DirectFetchError,
    _credit_window,
    _current_jwt_claims,
    _extract_auth_details,
    _fetch_stable_wham_usage,
    _fetch_wham_usage,
    _is_identity_attribution_error,
    _is_spark_limit_response,
    _jwt_expiry,
    _redact_url,
    _response_content_type,
    _select_stable_wham_usage,
    _signature_number,
    auth_email_from_file,
    auth_email_from_payload,
    auth_identity_changed,
    auth_identity_for_account,
    auth_identity_from_file,
    auth_identity_from_payload,
    auth_metadata_from_payload,
    auth_plan_type_for_account,
    auth_plan_type_from_file,
    auth_plan_type_from_payload,
    canonical_backend_identity,
    fetch_account_usage_direct,
    read_auth_json_file,
    validate_auth_json_file,
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


def _jwt_with_raw_payload(payload: bytes) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    encoded_payload = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    return f"{header}.{encoded_payload}.signature"


class _BrokenInt(int):
    def __lt__(self, _other):
        raise RuntimeError("synthetic direct integer comparison marker")

    def __le__(self, _other):
        raise RuntimeError("synthetic direct integer comparison marker")

    def __gt__(self, _other):
        raise RuntimeError("synthetic direct integer comparison marker")


class _BrokenFloat(float):
    def __float__(self):
        raise RuntimeError("synthetic direct float conversion marker")


def test_default_auth_json_path_uses_codex_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    assert direct_module.default_auth_json_path() == home / ".codex" / "auth.json"


def test_expanded_auth_path_expands_current_user_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    assert direct_module._expanded_auth_path(Path("~/.codex/auth.json")) == (
        home / ".codex" / "auth.json"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://user:secret@chatgpt.com/path?token=value#fragment",
            "https://chatgpt.com/path",
        ),
        (
            "https://user:secret@[2001:db8::1]:8443/path?token=value",
            "https://[2001:db8::1]:8443/path",
        ),
    ],
)
def test_direct_redact_url_removes_userinfo(url, expected):
    assert _redact_url(url) == expected


def test_direct_redact_url_rejects_invalid_port():
    assert _redact_url("https://user:secret@chatgpt.com:invalid/path") == ""


def test_direct_response_content_type_falls_back_for_malformed_headers():
    class Response:
        headers: ClassVar[list[object]] = []

    assert _response_content_type(Response()) == ""

    class LegacyResponse:
        headers: ClassVar[list[object]] = []

        def getheader(self, name):
            return "application/json" if name == "content-type" else None

    assert _response_content_type(LegacyResponse()) == "application/json"


def test_direct_response_content_type_rejects_header_property_hooks():
    class Response:
        @property
        def headers(self):
            raise RuntimeError("synthetic response headers marker")

    assert _response_content_type(Response()) == ""


def test_direct_response_final_url_rejects_getter_hooks():
    class Response:
        @property
        def geturl(self):
            raise RuntimeError("synthetic response URL getter marker")

    assert direct_module._response_final_url(Response(), "https://chatgpt.com/") == ""


def test_direct_response_final_url_rejects_string_subclass_results():
    class BrokenStr(str):
        pass

    class Response:
        def geturl(self):
            return BrokenStr("https://chatgpt.com/")

    assert direct_module._response_final_url(Response(), "https://chatgpt.com/") == ""

    class FallbackResponse:
        url = BrokenStr("https://chatgpt.com/")

    assert direct_module._response_final_url(
        FallbackResponse(), "https://chatgpt.com/"
    ) == ""


def test_direct_trusted_response_url_rejects_string_subclasses():
    class BrokenStr(str):
        pass

    assert not direct_module._is_trusted_wham_response_url(
        BrokenStr("https://chatgpt.com/backend-api/wham/usage")
    )


def test_parse_iso_datetime_rejects_string_subclass_hooks():
    class BrokenStr(str):
        def strip(self):
            raise RuntimeError("synthetic datetime marker")

    assert direct_module._parse_iso_datetime(BrokenStr("2026-08-23T12:00:00Z")) is None


@pytest.mark.parametrize("account", [None, [], "invalid", 1, True, object()])
def test_direct_fetch_rejects_non_account_input(account):
    with pytest.raises(ValueError, match="account is invalid"):
        fetch_account_usage_direct(account)  # type: ignore[arg-type]


def test_direct_deadline_returns_monotonic_deadline(monkeypatch):
    monkeypatch.setattr(direct_module.time, "monotonic", lambda: 100.0)

    assert direct_module._direct_deadline(2) == 102.0


def test_remaining_direct_timeout_returns_positive_remainder(monkeypatch):
    monkeypatch.setattr(direct_module.time, "monotonic", lambda: 100.0)

    assert direct_module._remaining_direct_timeout(102.0) == 2.0


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (Path("/proc/self/fd/42"), 42),
        (Path("/proc/self/fd/0"), 0),
        (Path("/proc/self/fd"), None),
        (Path("/tmp/auth.json"), None),
    ],
)
def test_proc_self_fd_parses_only_canonical_fd_paths(path, expected):
    assert direct_module._proc_self_fd(path) == expected


def test_open_auth_json_fd_duplicates_inherited_regular_fd(tmp_path):
    path = tmp_path / "auth.json"
    path.write_bytes(b"auth-payload")
    source_fd = os.open(path, os.O_RDONLY)
    duplicate_fd = -1
    try:
        duplicate_fd = direct_module._open_auth_json_fd(Path(f"/proc/self/fd/{source_fd}"))
        assert os.read(duplicate_fd, 64) == b"auth-payload"
    finally:
        if duplicate_fd >= 0:
            os.close(duplicate_fd)
        os.close(source_fd)


@pytest.mark.parametrize("auth_json_path", [1, {}, object()])
def test_direct_fetch_rejects_invalid_auth_json_path_type(auth_json_path):
    account = Account(
        id="work",
        label="Work",
        profile_dir="/tmp/work",
        auth_json_path=auth_json_path,  # type: ignore[arg-type]
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "auth.json path is invalid"


def test_direct_resolve_auth_path_rejects_string_subclass_hooks():
    class BrokenStr(str):
        def __eq__(self, _other):
            raise RuntimeError("synthetic auth path marker")

    account = Account(
        id="work",
        label="Work",
        profile_dir="/tmp/work",
        auth_json_path=BrokenStr("/tmp/auth.json"),
    )
    with pytest.raises(DirectAuthError, match=r"auth\.json path is invalid"):
        direct_module._resolve_auth_json_path(account, None)


def test_direct_resolve_auth_override_rejects_path_subclasses():
    class BrokenPath(type(Path())):
        pass

    account = Account(
        id="work",
        label="Work",
        profile_dir="/tmp/work",
    )
    with pytest.raises(DirectAuthError, match=r"auth\.json path is invalid"):
        direct_module._resolve_auth_json_path(
            account, BrokenPath("/tmp/auth.json")
        )


def test_direct_require_auth_path_rejects_path_subclasses():
    class BrokenPath(type(Path())):
        pass

    with pytest.raises(DirectAuthError, match=r"auth\.json path is invalid"):
        direct_module._require_auth_path(BrokenPath("/tmp/auth.json"))


def test_direct_fetch_rejects_unknown_auth_home(tmp_path):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "work"),
        auth_json_path="~definitely-no-such-user-zzzz/auth.json",
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "auth.json path is invalid"


def test_direct_fetch_rejects_unknown_auth_override_home(tmp_path):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "work"),
    )

    usage = fetch_account_usage_direct(
        account,
        auth_json_path=Path("~definitely-no-such-user-zzzz/auth.json"),
    )

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "auth.json path is invalid"


@pytest.mark.parametrize("auth_json_path", [1, {}, object()])
def test_direct_fetch_rejects_invalid_auth_override_type(auth_json_path):
    account = Account(id="work", label="Work", profile_dir="/tmp/work")

    usage = fetch_account_usage_direct(
        account,
        auth_json_path=auth_json_path,  # type: ignore[arg-type]
    )

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "auth.json path is invalid"


@pytest.mark.parametrize(
    "value",
    (
        pytest.param(10**10000, id="huge-int"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param(float("nan"), id="nan"),
    ),
)
def test_signature_number_rejects_non_finite_values_without_raising(value):
    assert _signature_number(value) is None


def test_signature_reset_buckets_timestamp_by_five_seconds():
    assert direct_module._signature_reset(12) == 2


def test_usage_response_completeness_counts_two_valid_windows():
    payload = {
        "user_id": "user-test",
        "account_id": "account-test",
        "rate_limit": {
            "primary_window": {
                "limit_window_seconds": 18_000,
                "used_percent": 10,
            },
            "secondary_window": {
                "limit_window_seconds": 604_800,
                "used_percent": 20,
            },
        },
    }

    assert direct_module._usage_response_completeness(payload) == 2


def test_usage_response_progresses_with_small_monotonic_delta():
    def response(used: int) -> dict:
        return {
            "user_id": "user-test",
            "account_id": "account-test",
            "rate_limit": {
                "primary_window": {
                    "limit_window_seconds": 18_000,
                    "used_percent": used,
                    "reset_at": 1_000,
                }
            },
        }

    assert direct_module._usage_response_progresses(
        [response(1), response(2)]
    ) is True


def test_latest_response_progresses_beyond_stable_group():
    def response(used: int) -> dict:
        return {
            "user_id": "user-test",
            "account_id": "account-test",
            "rate_limit": {
                "primary_window": {
                    "limit_window_seconds": 18_000,
                    "used_percent": used,
                    "reset_at": 1_000,
                }
            },
        }

    stable = response(1)
    latest = response(2)

    assert direct_module._latest_response_progresses_beyond_group(
        [stable, latest], [(0, stable)]
    ) is True


def test_signature_flag_rejects_string_hooks():
    class BrokenFlag:
        def __str__(self):
            raise RuntimeError("synthetic signature flag marker")

    assert direct_module._signature_flag(BrokenFlag()) == (
        "invalid",
        "BrokenFlag",
        "<unprintable>",
    )


def test_usage_window_signature_rejects_dict_subclass_hooks():
    class BrokenDict(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic usage signature marker")

    assert direct_module._usage_window_signature(BrokenDict()) is None


def test_supported_window_durations_rejects_mapping_subclass_hooks():
    class BrokenDict(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic supported duration marker")

    assert direct_module._supported_window_durations(
        {"rate_limit": BrokenDict()}
    ) == set()
    assert direct_module._supported_window_durations(
        {"rate_limit": {"primary_window": BrokenDict()}}
    ) == set()


def test_usage_response_signature_rejects_rate_limit_dict_subclass_hooks():
    class BrokenRateLimit(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic response signature marker")

    signature = direct_module._usage_response_signature(
        {"rate_limit": BrokenRateLimit()}
    )

    assert signature[1] == (None, None)


def test_rate_limit_window_rejects_mapping_subclass_hooks():
    class BrokenDict(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic rate limit window marker")

    assert direct_module._rate_limit_window(
        {"rate_limit": BrokenDict()}, "primary_window"
    ) is None
    assert direct_module._rate_limit_window(
        {"rate_limit": {"primary_window": BrokenDict()}}, "primary_window"
    ) is None


def test_main_limit_signature_rejects_dict_subclass_hooks():
    class BrokenDict(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic main signature marker")

    assert direct_module._main_limit_signature({"rate_limit": BrokenDict()}) == (
        "invalid-rate-limit",
    )


def test_spark_limit_signature_rejects_list_subclass_hooks():
    class BrokenList(list):
        def __iter__(self):
            raise RuntimeError("synthetic spark signature marker")

    assert direct_module._spark_limit_signature(
        {"additional_rate_limits": BrokenList([{}])}
    ) is None


def test_spark_limit_signature_rejects_item_dict_subclass_hooks():
    class BrokenItem(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic spark item marker")

    assert direct_module._spark_limit_signature(
        {"additional_rate_limits": [BrokenItem()]}
    ) == ("present-no-spark",)


def test_spark_limit_signature_rejects_rate_limit_dict_subclass_hooks():
    class BrokenRateLimit(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic spark rate limit marker")

    assert direct_module._spark_limit_signature(
        {
            "additional_rate_limits": [
                {
                    "limit_name": "GPT-5.3-Codex-Spark",
                    "metered_feature": "codex_bengalfox",
                    "rate_limit": BrokenRateLimit(),
                }
            ]
        }
    ) == ("invalid",)


def test_direct_numeric_boundaries_reject_subclasses_before_operations(tmp_path, monkeypatch):
    broken_int = _BrokenInt(200)
    broken_float = _BrokenFloat(1.0)

    assert direct_module._signature_number(broken_int) is None
    assert direct_module._signature_number(broken_float) is None
    with pytest.raises(DirectFetchError, match="positive finite"):
        direct_module._direct_deadline(broken_float)
    with pytest.raises(DirectFetchError, match="timed out"):
        direct_module._remaining_direct_timeout(broken_float)
    assert direct_module._missing_usage_limits_error(
        {
            "rate_limit": {
                "primary_window": {
                    "limit_window_seconds": broken_float,
                    "used_percent": broken_float,
                }
            }
        },
        None,
    ) == "usage limits not found in direct response"
    assert direct_module._credit_window(
        {"credits": broken_float},
        datetime(2026, 7, 16, 4, 0, tzinfo=UTC),
    ) is None

    monkeypatch.setattr(
        direct_module,
        "_jwt_claims",
        lambda _token: {"exp": broken_float},
    )
    assert direct_module._jwt_expiry("token") is None
    assert direct_module._current_jwt_claims("token") is None
    with pytest.raises(DirectAuthError, match="access_token expiry is invalid"):
        direct_module._validate_access_token_expiry("token", path=tmp_path / "auth.json")


def test_validate_access_token_expiry_rejects_dict_subclass_results(
    tmp_path, monkeypatch
):
    class BrokenClaims(dict):
        def __contains__(self, _key):
            raise RuntimeError("synthetic access expiry claims marker")

    monkeypatch.setattr(
        direct_module,
        "_jwt_claims",
        lambda _token: BrokenClaims(),
    )

    assert (
        direct_module._validate_access_token_expiry(
            "token",
            path=tmp_path / "auth.json",
        )
        is None
    )


def test_missing_usage_limits_error_rejects_plan_type_string_subclass_hooks():
    class BrokenStr(str):
        def __bool__(self):
            raise RuntimeError("synthetic missing limits plan marker")

    error = direct_module._missing_usage_limits_error(
        {
            "rate_limit": {
                "primary_window": {
                    "limit_window_seconds": 18_000,
                    "used_percent": 20,
                }
            }
        },
        BrokenStr("plus"),
    )

    assert "(plan unknown; available window 5h)" in error


def test_missing_usage_limits_error_rejects_mapping_subclass_hooks():
    class BrokenDict(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic missing limits mapping marker")

    expected = "usage limits not found in direct response"
    assert (
        direct_module._missing_usage_limits_error(
            {"rate_limit": BrokenDict()},
            None,
        )
        == expected
    )
    assert (
        direct_module._missing_usage_limits_error(
            {"rate_limit": {"primary_window": BrokenDict()}},
            None,
        )
        == expected
    )


def test_credit_window_extracts_nested_absolute_balance():
    window = _credit_window(
        {
            "account": {
                "credits": {
                    "has_credits": True,
                    "unlimited": False,
                    "balance": "794",
                }
            }
        },
        datetime(2026, 7, 16, 4, 0, tzinfo=UTC),
    )

    assert window is not None
    assert window.name == "credits"
    assert window.remaining == 794
    assert window.source == "json:credits"


def test_credit_window_rejects_credit_dict_subclass_hooks():
    class BrokenCredits(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic credit mapping marker")

    assert (
        _credit_window(
            {"credits": BrokenCredits()},
            datetime(2026, 7, 16, 4, 0, tzinfo=UTC),
        )
        is None
    )


def test_credit_window_rejects_nested_source_mapping_hooks():
    class BrokenMapping(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic nested credit get marker")

        def values(self):
            raise RuntimeError("synthetic nested credit values marker")

    assert (
        _credit_window(
            {"rateLimits": BrokenMapping()},
            datetime(2026, 7, 16, 4, 0, tzinfo=UTC),
        )
        is None
    )
    assert (
        _credit_window(
            {"rateLimitsByLimitId": BrokenMapping()},
            datetime(2026, 7, 16, 4, 0, tzinfo=UTC),
        )
        is None
    )
    assert (
        _credit_window(
            {"account": BrokenMapping()},
            datetime(2026, 7, 16, 4, 0, tzinfo=UTC),
        )
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"credits": True},
        {"credits": -1},
        {"credits": {"balance": "not-a-number"}},
        {"credits": {"remaining": 101, "limit": 100}},
        pytest.param({"credits": 10**10_000}, id="huge-scalar"),
        pytest.param({"credits": {"balance": 10**10_000}}, id="huge-nested"),
    ],
)
def test_credit_window_rejects_invalid_balances(payload):
    assert (
        _credit_window(
            payload,
            datetime(2026, 7, 16, 4, 0, tzinfo=UTC),
        )
        is None
    )


@pytest.mark.parametrize("status", [None, True, "200", 199, 300, _BrokenInt(200)])
def test_fetch_wham_usage_rejects_invalid_http_status(status, monkeypatch):
    class FakeResponse:
        status = 200

        def __init__(self):
            self.status = status
            self.headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit):
            return b"{}"

    monkeypatch.setattr(
        "codex_usage.direct.urlopen",
        lambda request, *, timeout: FakeResponse(),
    )

    with pytest.raises(DirectFetchError, match="invalid HTTP status"):
        _fetch_wham_usage("token", account_id=None, timeout_seconds=1)


def test_direct_credentials_are_not_forwarded_to_redirect_target(monkeypatch):
    class FakeResponse:
        status = 200

        def __init__(self):
            self.headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit):
            return b"{}"

    def fake_urlopen(request, *, timeout):
        redirected = HTTPRedirectHandler().redirect_request(
            request,
            None,
            302,
            "found",
            {},
            "https://attacker.example/collect",
        )
        assert request.get_header("Authorization") == "Bearer secret"
        assert request.get_header("Chatgpt-account-id") == "account"
        assert redirected is not None
        assert redirected.get_header("Authorization") is None
        assert redirected.get_header("Chatgpt-account-id") is None
        return FakeResponse()

    monkeypatch.setattr("codex_usage.direct.urlopen", fake_urlopen)

    assert _fetch_wham_usage(
        "secret",
        account_id="account",
        timeout_seconds=1,
    ) == {}


def test_direct_rejects_foreign_final_response_url(monkeypatch):
    class FakeResponse:
        status = 200
        headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}
        url = "https://attacker.example/collect"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def geturl(self):
            return self.url

        def read(self, _limit):
            return b"{}"

    monkeypatch.setattr(
        "codex_usage.direct.urlopen",
        lambda _request, *, timeout: FakeResponse(),
    )

    with pytest.raises(DirectFetchError, match="response URL is untrusted"):
        _fetch_wham_usage("secret", account_id=None, timeout_seconds=1)


def test_fetch_wham_usage_rejects_payload_dict_subclass(monkeypatch):
    class BrokenPayload(dict):
        pass

    class FakeResponse:
        status = 200
        headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit):
            return b"{}"

    monkeypatch.setattr(
        "codex_usage.direct.urlopen",
        lambda _request, *, timeout: FakeResponse(),
    )
    monkeypatch.setattr(
        direct_module,
        "loads_strict",
        lambda _raw: BrokenPayload(),
    )

    with pytest.raises(DirectFetchError, match="response is not a JSON object"):
        _fetch_wham_usage("token", account_id=None, timeout_seconds=1)


def test_jwt_expiry_ignores_non_object_payloads():
    for claims in ([], None, "not-an-object"):
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).rstrip(b"=")
        token = f"e30.{payload.decode('ascii')}.signature"

        assert _jwt_expiry(token) is None


@pytest.mark.parametrize("expiry", [True, "not-a-number", float("inf")])
def test_jwt_claims_reject_invalid_exp_types(expiry):
    token = _jwt_with_claims({"exp": expiry, "sub": "account"})

    assert _jwt_expiry(token) is None
    assert _current_jwt_claims(token) is None


@pytest.mark.parametrize("expiry", [True, "not-a-number", []])
def test_auth_details_rejects_invalid_access_token_expiry(tmp_path, expiry):
    token = _jwt_with_claims({"exp": expiry})

    with pytest.raises(DirectAuthError, match="access_token expiry is invalid"):
        _extract_auth_details(
            {"tokens": {"access_token": token}},
            path=tmp_path / "auth.json",
        )


def test_auth_details_rejects_tokens_dict_subclass_hooks(tmp_path):
    class BrokenTokens(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic auth details mapping marker")

    with pytest.raises(DirectAuthError, match=r"auth\.json has no tokens object"):
        _extract_auth_details(
            {"tokens": BrokenTokens()},
            path=tmp_path / "auth.json",
        )


def test_auth_details_rejects_payload_dict_subclass_hooks(tmp_path):
    class BrokenPayload(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic auth details payload marker")

    with pytest.raises(DirectAuthError, match=r"auth\.json has no tokens object"):
        _extract_auth_details(
            BrokenPayload(),
            path=tmp_path / "auth.json",
        )


def test_auth_details_rejects_access_token_string_subclass_hooks(tmp_path):
    class BrokenStr(str):
        def __len__(self):
            raise RuntimeError("synthetic access token marker")

    with pytest.raises(DirectAuthError, match=r"auth\.json has no access_token"):
        _extract_auth_details(
            {"tokens": {"access_token": BrokenStr("token")}},
            path=tmp_path / "auth.json",
        )


def test_validate_access_token_expiry_rejects_string_subclass_hooks(tmp_path):
    class BrokenStr(str):
        def split(self, _separator):
            raise RuntimeError("synthetic JWT token marker")

    with pytest.raises(DirectAuthError, match="access_token expiry is invalid"):
        direct_module._validate_access_token_expiry(
            BrokenStr("token"), path=tmp_path / "auth.json"
        )


def test_jwt_claims_rejects_string_subclass_hooks():
    class BrokenStr(str):
        def split(self, _separator):
            raise RuntimeError("synthetic JWT claims marker")

    assert direct_module._jwt_claims(BrokenStr("token")) is None


def test_jwt_claims_rejects_dict_subclass_results(monkeypatch):
    class BrokenClaims(dict):
        pass

    monkeypatch.setattr(direct_module, "loads_strict", lambda _raw: BrokenClaims())

    assert direct_module._jwt_claims(_jwt_with_claims({})) is None


def test_current_jwt_claims_rejects_dict_subclass_results(monkeypatch):
    class BrokenClaims(dict):
        pass

    monkeypatch.setattr(direct_module, "_jwt_claims", lambda _token: BrokenClaims())

    assert _current_jwt_claims("token") is None


def test_jwt_expiry_rejects_dict_subclass_results(monkeypatch):
    class BrokenClaims(dict):
        pass

    monkeypatch.setattr(
        direct_module,
        "_jwt_claims",
        lambda _token: BrokenClaims({"exp": 1_700_000_000}),
    )

    assert direct_module._jwt_expiry("token") is None


def test_jwt_claims_reject_extra_segments():
    token = _jwt_with_claims({"sub": "account"}) + ".extra"

    assert _current_jwt_claims(token) is None


def test_jwt_claims_reject_nonstandard_json_constants():
    payload = base64.urlsafe_b64encode(b'{"sub":NaN}').rstrip(b"=").decode("ascii")
    token = f"e30.{payload}.signature"

    assert _current_jwt_claims(token) is None


def test_jwt_claims_reject_duplicate_keys():
    token = _jwt_with_raw_payload(b'{"sub":"first","sub":"second"}')

    assert _current_jwt_claims(token) is None


@pytest.mark.parametrize("token", [" secret-access-token", "secret-access-token "])
def test_auth_details_reject_whitespace_wrapped_access_token(tmp_path, token):
    with pytest.raises(DirectAuthError, match="invalid characters"):
        _extract_auth_details(
            {"tokens": {"access_token": token}},
            path=tmp_path / "auth.json",
        )


def test_jwt_expiry_uses_dst_aware_local_zone(monkeypatch):
    berlin = ZoneInfo("Europe/Berlin")
    expected = datetime(2026, 10, 26, 0, 15, tzinfo=berlin)
    monkeypatch.setattr("codex_usage.direct.LOCAL_TZ", berlin)

    expiry = _jwt_expiry(_jwt_with_exp(int(expected.timestamp())))

    assert expiry == expected


def test_access_token_expired_rejects_datetime_subclass_hooks():
    class BrokenDateTime(datetime):
        def __le__(self, _other):
            raise RuntimeError("synthetic token expiry marker")

    assert direct_module._is_access_token_expired(
        BrokenDateTime(2026, 1, 1, tzinfo=UTC),
        now=datetime(2026, 1, 2, tzinfo=UTC),
    ) is True


def test_expired_auth_error_rejects_datetime_subclass_hooks():
    class BrokenDateTime(datetime):
        def astimezone(self, _tz=None):
            raise RuntimeError("synthetic expired auth marker")

    assert direct_module._expired_auth_error(
        "account",
        BrokenDateTime(2026, 1, 1, tzinfo=UTC),
    ) == "auth.json access_token expired; run `codex-usage reactivate account`"


def test_has_usage_values_rejects_window_property_hooks():
    class BrokenWindow:
        @property
        def has_usage_value(self):
            raise RuntimeError("synthetic usage window marker")

    assert direct_module._has_usage_values(BrokenWindow(), object()) is False


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


def test_strict_auth_identity_values_extracts_requested_claims(tmp_path):
    assert direct_module._strict_auth_identity_values(
        {"chatgpt_user_id": "user-1", "other": "ignored"},
        ("chatgpt_user_id", "chatgpt_account_id"),
        path=tmp_path / "auth.json",
    ) == ["user-1"]


@pytest.mark.parametrize("payload", [None, [], "invalid", 1, True, object()])
def test_auth_payload_helpers_ignore_non_object_payloads(tmp_path, payload):
    path = tmp_path / "auth.json"

    assert auth_identity_from_payload(payload, path=path) == (None, None)  # type: ignore[arg-type]
    assert auth_email_from_payload(payload, path=path) is None  # type: ignore[arg-type]
    assert auth_plan_type_from_payload(payload, path=path) is None  # type: ignore[arg-type]
    assert auth_metadata_from_payload(payload) == {  # type: ignore[arg-type]
        "auth_last_refresh": None,
        "auth_access_expires_at": None,
        "auth_id_expires_at": None,
    }


def test_auth_account_id_rejects_tokens_dict_subclass_hooks(tmp_path):
    class BrokenTokens(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic auth account mapping marker")

    assert direct_module._auth_account_id_from_payload(
        {"tokens": BrokenTokens()},
        path=tmp_path / "auth.json",
    ) is None


def test_auth_account_id_rejects_payload_dict_subclass_hooks(tmp_path):
    class BrokenPayload(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic auth account payload marker")

    assert direct_module._auth_account_id_from_payload(
        BrokenPayload(),
        path=tmp_path / "auth.json",
    ) is None


def test_auth_email_rejects_tokens_dict_subclass_hooks(tmp_path):
    class BrokenTokens(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic auth email mapping marker")

    assert auth_email_from_payload(
        {"tokens": BrokenTokens()},
        path=tmp_path / "auth.json",
    ) is None


def test_auth_email_rejects_payload_dict_subclass_hooks(tmp_path):
    class BrokenPayload(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic auth email payload marker")

    assert auth_email_from_payload(
        BrokenPayload(),
        path=tmp_path / "auth.json",
    ) is None


def test_auth_email_rejects_claims_dict_subclass_hooks(tmp_path, monkeypatch):
    class BrokenClaims(dict):
        def __contains__(self, _key):
            raise RuntimeError("synthetic auth email claims marker")

    monkeypatch.setattr(
        direct_module,
        "_current_jwt_claims",
        lambda _token: BrokenClaims(),
    )

    assert auth_email_from_payload(
        {"tokens": {"id_token": "token"}},
        path=tmp_path / "auth.json",
    ) is None


def test_auth_plan_type_rejects_tokens_dict_subclass_hooks(tmp_path):
    class BrokenTokens(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic auth plan mapping marker")

    assert auth_plan_type_from_payload(
        {"tokens": BrokenTokens()},
        path=tmp_path / "auth.json",
    ) is None


def test_auth_plan_type_rejects_payload_dict_subclass_hooks(tmp_path):
    class BrokenPayload(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic auth plan payload marker")

    assert auth_plan_type_from_payload(
        BrokenPayload(),
        path=tmp_path / "auth.json",
    ) is None


def test_auth_plan_type_rejects_claims_dict_subclass_hooks(tmp_path, monkeypatch):
    class BrokenClaims(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic auth plan claims marker")

    monkeypatch.setattr(
        direct_module,
        "_current_jwt_claims",
        lambda _token: BrokenClaims(),
    )

    assert auth_plan_type_from_payload(
        {"tokens": {"id_token": "token"}},
        path=tmp_path / "auth.json",
    ) is None


def test_auth_plan_type_rejects_nested_auth_claims_dict_subclass_hooks(
    tmp_path, monkeypatch
):
    class BrokenAuthClaims(dict):
        def __contains__(self, _key):
            raise RuntimeError("synthetic nested auth plan claims marker")

    monkeypatch.setattr(
        direct_module,
        "_current_jwt_claims",
        lambda _token: {
            "https://api.openai.com/auth": BrokenAuthClaims(),
        },
    )

    with pytest.raises(DirectAuthError, match="token auth claims are invalid"):
        auth_plan_type_from_payload(
            {"tokens": {"id_token": "token"}},
            path=tmp_path / "auth.json",
        )


def test_auth_identity_rejects_tokens_dict_subclass_hooks(tmp_path):
    class BrokenTokens(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic auth identity mapping marker")

    assert auth_identity_from_payload(
        {"tokens": BrokenTokens()},
        path=tmp_path / "auth.json",
    ) == (None, None)


def test_auth_identity_rejects_claims_dict_subclass_hooks(tmp_path, monkeypatch):
    class BrokenClaims(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic auth identity claims marker")

    monkeypatch.setattr(
        direct_module,
        "_current_jwt_claims",
        lambda _token: BrokenClaims(),
    )

    assert auth_identity_from_payload(
        {"tokens": {"id_token": "token"}},
        path=tmp_path / "auth.json",
    ) == (None, None)


def test_auth_identity_rejects_nested_auth_claims_dict_subclass_hooks(
    tmp_path, monkeypatch
):
    class BrokenAuthClaims(dict):
        def __contains__(self, _key):
            raise RuntimeError("synthetic nested auth claims marker")

    monkeypatch.setattr(
        direct_module,
        "_current_jwt_claims",
        lambda _token: {
            "https://api.openai.com/auth": BrokenAuthClaims(),
        },
    )

    with pytest.raises(DirectAuthError, match="token auth claims are invalid"):
        auth_identity_from_payload(
            {"tokens": {"id_token": "token"}},
            path=tmp_path / "auth.json",
        )


def test_auth_metadata_rejects_tokens_dict_subclass_hooks():
    class BrokenTokens(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic auth metadata mapping marker")

    assert auth_metadata_from_payload({"tokens": BrokenTokens()}) == {
        "auth_last_refresh": None,
        "auth_access_expires_at": None,
        "auth_id_expires_at": None,
    }


def test_auth_metadata_rejects_payload_dict_subclass_hooks():
    class BrokenPayload(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic auth metadata payload marker")

    assert auth_metadata_from_payload(BrokenPayload()) == {
        "auth_last_refresh": None,
        "auth_access_expires_at": None,
        "auth_id_expires_at": None,
    }


def test_auth_identity_rejects_payload_dict_subclass_hooks(tmp_path):
    class BrokenPayload(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic auth identity payload marker")

    assert auth_identity_from_payload(
        BrokenPayload(),
        path=tmp_path / "auth.json",
    ) == (None, None)


@pytest.mark.parametrize(
    "claims",
    [
        {"https://api.openai.com/auth": {"chatgpt_user_id": ["not-an-id"]}},
        {
            "https://api.openai.com/auth": {
                "chatgpt_user_id": "user-a",
                "user_id": "user-b",
            }
        },
        {"https://api.openai.com/auth": "not-an-object"},
    ],
)
def test_auth_identity_rejects_malformed_or_conflicting_claims(tmp_path, claims):
    path = tmp_path / "auth.json"
    payload = {
        "tokens": {
            "access_token": _jwt_with_claims(claims),
            "account_id": "account-uuid",
        }
    }

    with pytest.raises(
        DirectAuthError,
        match=r"identity claim|auth claims|identities disagree",
    ):
        auth_identity_from_payload(payload, path=path)


def test_safe_auth_identity_rejects_string_subclass_hooks():
    class BrokenStr(str):
        def __len__(self):
            raise RuntimeError("synthetic auth identity marker")

    assert direct_module._safe_auth_identity(BrokenStr("user")) is None


def test_safe_auth_plan_type_rejects_string_subclass_hooks():
    class BrokenStr(str):
        def __len__(self):
            raise RuntimeError("synthetic auth plan marker")

    assert direct_module._safe_auth_plan_type(BrokenStr("plus")) is None


def test_auth_plan_type_changed_rejects_string_subclass_hooks():
    class BrokenStr(str):
        def __ne__(self, _other):
            raise RuntimeError("synthetic auth plan comparison marker")

    assert direct_module._auth_plan_type_changed(BrokenStr("plus"), None) is True


def test_normalized_plan_type_rejects_string_subclass_hooks():
    class BrokenStr(str):
        def strip(self):
            raise RuntimeError("synthetic auth plan normalization marker")

    assert direct_module._normalized_plan_type(BrokenStr("plus")) == ""


def test_auth_identity_rejects_account_id_string_subclass_hooks(tmp_path):
    class BrokenStr(str):
        def __len__(self):
            raise RuntimeError("synthetic auth account marker")

    with pytest.raises(DirectAuthError, match=r"auth\.json account_id is invalid"):
        auth_identity_from_payload(
            {"tokens": {"account_id": BrokenStr("account")}},
            path=tmp_path / "auth.json",
        )


def test_auth_identity_ignores_expired_id_token_when_access_token_is_current(tmp_path):
    path = tmp_path / "auth.json"
    payload = {
        "tokens": {
            "id_token": _jwt_with_claims(
                {
                    "exp": int(datetime.now(tz=UTC).timestamp()) - 60,
                    "email": "old@example.test",
                    "https://api.openai.com/auth": {
                        "chatgpt_user_id": "old-user",
                        "chatgpt_account_id": "old-account",
                        "chatgpt_plan_type": "free",
                    },
                }
            ),
            "access_token": _jwt_with_claims(
                {
                    "exp": int(datetime.now(tz=UTC).timestamp()) + 3600,
                    "email": "current@example.test",
                    "https://api.openai.com/auth": {
                        "chatgpt_user_id": "current-user",
                        "chatgpt_account_id": "current-account",
                        "chatgpt_plan_type": "plus",
                    },
                }
            ),
            "account_id": "current-account",
        }
    }

    assert auth_identity_from_payload(payload, path=path) == (
        "current-user",
        "current-account",
    )


@pytest.mark.parametrize(
    "claims",
    [
        {"https://api.openai.com/auth": {"chatgpt_plan_type": []}},
        {"https://api.openai.com/auth": {"chatgpt_plan_type": "free"}},
    ],
)
def test_auth_plan_type_rejects_invalid_claims(tmp_path, claims):
    path = tmp_path / "auth.json"
    token = _jwt_with_claims(claims)
    payload = {"tokens": {"access_token": token}}

    if claims["https://api.openai.com/auth"].get("chatgpt_plan_type") == "free":
        assert auth_plan_type_from_payload(payload, path=path) == "free"
    else:
        with pytest.raises(DirectAuthError, match="plan type is invalid"):
            auth_plan_type_from_payload(payload, path=path)


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
    assert auth_identity_changed(
        before_user_id="old-user",
        before_account_id=None,
        after_user_id="new-user",
        after_account_id=None,
    ) is True


def test_auth_identity_changed_rejects_string_subclass_hooks():
    class BrokenStr(str):
        def __bool__(self):
            raise RuntimeError("synthetic identity marker")

    assert auth_identity_changed(
        before_user_id=None,
        before_account_id=BrokenStr("shared-account"),
        after_user_id=None,
        after_account_id=None,
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


def test_canonical_backend_identity_rejects_foreign_user_on_shared_user_alias():
    with pytest.raises(ValueError, match="backend response belongs to a different account"):
        canonical_backend_identity(
            "foreign-user",
            "shared-user",
            auth_user_id="shared-user",
            auth_account_id="real-account",
            auth_plan_type="plus",
            backend_plan_type="plus",
            require_backend_identity=True,
        )


def test_canonical_backend_identity_rejects_foreign_user_on_exact_account():
    with pytest.raises(ValueError, match="backend response belongs to a different account"):
        canonical_backend_identity(
            "foreign-user",
            "real-account",
            auth_user_id="real-user",
            auth_account_id="real-account",
            require_backend_identity=True,
        )


def test_canonical_backend_identity_rejects_shared_user_without_auth_account_id():
    with pytest.raises(ValueError, match="ambiguous account identity"):
        canonical_backend_identity(
            "shared-user",
            "shared-user",
            auth_user_id="shared-user",
            auth_account_id=None,
            require_backend_identity=True,
            reject_ambiguous_backend_identity=True,
        )


def test_canonical_backend_identity_rejects_user_only_response_with_auth_account_id():
    with pytest.raises(ValueError, match="ambiguous account identity"):
        canonical_backend_identity(
            "shared-user",
            None,
            auth_user_id="shared-user",
            auth_account_id="real-account",
            require_backend_identity=True,
            require_backend_account_id=True,
        )


def test_canonical_backend_identity_rejects_identity_free_response_without_auth_ids():
    with pytest.raises(ValueError, match="backend response has no account identity"):
        canonical_backend_identity(
            None,
            None,
            auth_user_id=None,
            auth_account_id=None,
            require_backend_identity=True,
        )


@pytest.mark.parametrize(
    "field, value",
    (
        ("backend_user_id", 1),
        ("backend_account_id", []),
        ("auth_user_id", {}),
        ("auth_account_id", " "),
        ("auth_plan_type", 1),
        ("backend_plan_type", []),
    ),
)
def test_canonical_backend_identity_rejects_malformed_fields(field, value):
    arguments = {
        "backend_user_id": None,
        "backend_account_id": None,
        "auth_user_id": None,
        "auth_account_id": None,
    }
    arguments[field] = value
    with pytest.raises(ValueError, match=f"{field} is invalid"):
        canonical_backend_identity(**arguments)


def test_canonical_backend_identity_rejects_string_subclass_hooks():
    class BrokenStr(str):
        def __len__(self):
            raise RuntimeError("synthetic canonical identity marker")

    with pytest.raises(ValueError, match="auth_user_id is invalid"):
        canonical_backend_identity(
            "user",
            "account",
            auth_user_id=BrokenStr("user"),
            auth_account_id="account",
        )


def test_canonical_backend_identity_rejects_bool_flag_hooks():
    class BrokenBool:
        def __bool__(self):
            raise RuntimeError("synthetic canonical flag marker")

    with pytest.raises(ValueError, match="require_backend_identity is invalid"):
        canonical_backend_identity(
            None,
            None,
            auth_user_id=None,
            auth_account_id=None,
            require_backend_identity=BrokenBool(),  # type: ignore[arg-type]
        )


def test_response_identity_match_rejects_string_subclass_hooks():
    class BrokenStr(str):
        def __bool__(self):
            raise RuntimeError("synthetic response identity marker")

    assert direct_module._response_identity_matches_auth(
        backend_user_id=None,
        backend_account_id=BrokenStr("account"),
        auth_user_id=None,
        auth_account_id=None,
    ) is False


@pytest.mark.parametrize(
    "error",
    (
        "backend response has no account identity",
        "backend response has ambiguous account identity",
        "backend response belongs to a different account",
        "backend response contains multiple backend accounts",
        "backend response does not identify one account",
    ),
)
def test_identity_attribution_errors_invalidate_cached_values(error):
    assert _is_identity_attribution_error(error) is True


def test_retryable_auth_error_rejects_exception_subclass_hooks():
    class BrokenAuthError(DirectAuthError):
        def __str__(self):
            raise RuntimeError("synthetic auth error marker")

    assert direct_module._is_retryable_direct_auth_error(BrokenAuthError("401")) is False


def test_identity_attribution_error_rejects_string_subclass_hooks():
    class BrokenStr(str):
        def __hash__(self):
            raise RuntimeError("synthetic identity error marker")

    assert _is_identity_attribution_error(
        BrokenStr("backend response has no account identity")
    ) is False


@pytest.mark.parametrize(
    "error",
    (
        "direct fetch failed: HTTP 500",
        "direct fetch failed: network error",
        "direct fetch failed: I/O error",
        "direct response limits were inconsistent across samples",
    ),
)
def test_transient_direct_errors_do_not_invalidate_cached_values(error):
    assert _is_identity_attribution_error(error) is False


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
        status = 200

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

    timeout = captured.pop("timeout")
    assert captured == {
        "url": "https://chatgpt.com/backend-api/wham/usage",
        "authorization": "Bearer secret-access-token",
        "account_id": "server-account",
    }
    assert 0 < timeout <= 7
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


def test_fetch_account_usage_direct_rejects_identity_free_response(
    tmp_path,
    monkeypatch,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"tokens": {"access_token": "opaque-token"}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    monkeypatch.setattr(
        "codex_usage.direct._fetch_stable_wham_usage",
        lambda *_args, **_kwargs: {
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
        },
    )
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(
        account,
        reject_ambiguous_backend_identity=True,
    )

    assert usage.status == AccountStatus.ERROR
    assert usage.error == "backend response has no account identity"
    assert usage.cache_invalidated is True


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


@pytest.mark.parametrize("value", [[], "malformed", 42, None])
def test_select_stable_wham_usage_rejects_malformed_main_limit_structure(value):
    response = {
        "user_id": "user-test",
        "account_id": "account-test",
        "rate_limit": value,
    }

    with pytest.raises(DirectFetchError, match="main limits were malformed"):
        _select_stable_wham_usage([response, response, response])


def test_select_stable_wham_usage_rejects_main_limit_dict_subclass():
    class BrokenRateLimit(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic main malformed marker")

    response = {
        "user_id": "user-test",
        "account_id": "account-test",
        "rate_limit": BrokenRateLimit(),
    }

    with pytest.raises(DirectFetchError, match="main limits were malformed"):
        _select_stable_wham_usage([response, response, response])


def test_select_stable_wham_usage_rejects_conflicting_partial_windows():
    def response(duration: int, used: int) -> dict:
        return {
            "user_id": "user-test",
            "account_id": "account-test",
            "rate_limit": {
                "primary_window": {
                    "used_percent": used,
                    "limit_window_seconds": duration,
                }
            },
        }

    with pytest.raises(DirectFetchError, match="inconsistent"):
        _select_stable_wham_usage(
            [response(18_000, 3), response(604_800, 45), response(18_000, 3)]
        )


def test_conflicting_partial_windows_detects_dropped_latest_window():
    def response(*, include_week: bool) -> dict:
        rate_limit = {
            "primary_window": {
                "limit_window_seconds": 18_000,
                "used_percent": 3,
            }
        }
        if include_week:
            rate_limit["secondary_window"] = {
                "limit_window_seconds": 604_800,
                "used_percent": 45,
            }
        return {
            "user_id": "user-test",
            "account_id": "account-test",
            "rate_limit": rate_limit,
        }

    complete = response(include_week=True)
    latest_partial = response(include_week=False)

    assert direct_module._has_conflicting_partial_windows(
        [(0, complete)],
        {("complete",): [(0, complete)]},
        latest_payload=latest_partial,
    ) is True


def test_select_stable_wham_usage_rejects_conflicting_spark_windows():
    def response(used: int) -> dict:
        return {
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
            "additional_rate_limits": [
                {
                    "limit_name": "GPT-5.3-Codex-Spark",
                    "metered_feature": "codex_bengalfox",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": used,
                            "limit_window_seconds": 604_800,
                        }
                    },
                }
            ],
        }

    with pytest.raises(DirectFetchError, match="Spark limits were inconsistent"):
        _select_stable_wham_usage([response(1), response(1), response(99)])


def test_conflicting_spark_limits_detects_signature_mismatch():
    def response(used: int) -> dict:
        return {
            "additional_rate_limits": [
                {
                    "limit_name": "GPT-5.3-Codex-Spark",
                    "metered_feature": "codex_bengalfox",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": used,
                            "limit_window_seconds": 604_800,
                        }
                    },
                }
            ]
        }

    assert direct_module._has_conflicting_spark_limits(
        [response(1), response(99)]
    ) is True


@pytest.mark.parametrize(
    ("spark_index", "other_index"),
    [(100, 101), (101, 100)],
)
def test_select_stable_wham_usage_detects_spark_conflicts_past_first_hundred_items(
    spark_index, other_index
):
    def response(used: int, spark_position: int) -> dict:
        additional_rate_limits = [
            {
                "limit_name": "not-spark",
                "metered_feature": "other-feature",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "limit_window_seconds": 604800,
                    }
                },
            }
            for _ in range(spark_position)
        ]
        additional_rate_limits.append(
            {
                "limit_name": "GPT-5.3-Codex-Spark",
                "metered_feature": "codex_bengalfox",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": used,
                        "limit_window_seconds": 604800,
                    }
                },
            }
        )
        return {
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
            "additional_rate_limits": additional_rate_limits,
        }

    with pytest.raises(DirectFetchError, match="Spark limits were inconsistent"):
        _select_stable_wham_usage(
            [response(1, spark_index), response(99, other_index), response(1, spark_index)]
        )


@pytest.mark.parametrize("value", [{}, "malformed", 42])
def test_select_stable_wham_usage_rejects_malformed_spark_limit_structure(value):
    response = {
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
        "additional_rate_limits": value,
    }

    with pytest.raises(DirectFetchError, match="Spark limits were malformed"):
        _select_stable_wham_usage([response, response, response])


def test_select_stable_wham_usage_rejects_spark_limit_list_subclass():
    class BrokenAdditionalLimits(list):
        def __iter__(self):
            raise RuntimeError("synthetic Spark malformed marker")

    response = {
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
        "additional_rate_limits": BrokenAdditionalLimits(),
    }

    with pytest.raises(DirectFetchError, match="Spark limits were malformed"):
        _select_stable_wham_usage([response, response, response])


def test_select_stable_wham_usage_rejects_mixed_sparse_spark_limit_forms():
    def response(**overrides: object) -> dict:
        base = {
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
        base.update(overrides)
        return base

    no_spark_list = response(
        additional_rate_limits=[
            {
                "limit_name": "not-spark",
                "metered_feature": "other-feature",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "limit_window_seconds": 604_800,
                    }
                },
            }
        ]
    )

    with pytest.raises(DirectFetchError, match="Spark limits were inconsistent"):
        _select_stable_wham_usage(
            [
                response(),
                response(additional_rate_limits=None),
                response(additional_rate_limits=[]),
                no_spark_list,
            ]
        )


def test_select_stable_wham_usage_accepts_consistent_non_spark_additional_limits():
    response = {
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
        "additional_rate_limits": [
            {
                "limit_name": "not-spark",
                "metered_feature": "other-feature",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "limit_window_seconds": 604_800,
                    }
                },
            }
        ],
    }

    selected = _select_stable_wham_usage([response, response, response])

    assert selected["additional_rate_limits"] == response["additional_rate_limits"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("limit_name", {"value": "GPT-5.3-Codex-Spark"}), ("metered_feature", ["codex_bengalfox"])],
)
def test_select_stable_wham_ignores_non_string_spark_identifiers(field, value):
    response = {
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
        "additional_rate_limits": [
            {
                "limit_name": "unrelated",
                "metered_feature": "unrelated",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "limit_window_seconds": 604_800,
                    }
                },
            }
        ],
    }
    response["additional_rate_limits"][0][field] = value

    selected = _select_stable_wham_usage([response, response, response])

    assert selected["additional_rate_limits"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("limit_name", " GPT-5.3-Codex-Spark"),
        ("metered_feature", "codex_bengalfox "),
    ],
)
def test_spark_response_identifier_is_not_normalized(field, value):
    values = {
        "limit_name": "unrelated",
        "metered_feature": "unrelated",
    }
    values[field] = value

    assert _is_spark_limit_response(
        values["limit_name"], values["metered_feature"]
    ) is False


def test_spark_response_identifier_rejects_string_subclass_hooks():
    class BrokenStr(str):
        def __iter__(self):
            raise RuntimeError("synthetic spark identifier marker")

    assert _is_spark_limit_response(BrokenStr("GPT-5.3-Codex-Spark"), "other") is False


def test_normalized_response_identifier_casefolds_native_string():
    assert direct_module._normalized_response_identifier(
        "GPT-5.3-Codex-Spark"
    ) == "gpt-5.3-codex-spark"


@pytest.mark.parametrize("field", ["allowed", "limit_reached"])
def test_select_stable_wham_usage_rejects_conflicting_main_limit_flags(field):
    def response(value: bool) -> dict:
        return {
            "user_id": "user-test",
            "account_id": "account-test",
            "rate_limit": {
                field: value,
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

    with pytest.raises(DirectFetchError, match="main limit flags were inconsistent"):
        _select_stable_wham_usage([response(False), response(False), response(True)])


def test_conflicting_main_limit_flags_detects_signature_mismatch():
    def response(value: bool) -> dict:
        return {"rate_limit": {"allowed": value, "limit_reached": False}}

    assert direct_module._has_conflicting_main_limit_flags(
        [response(False), response(True)]
    ) is True


def test_malformed_main_limit_structure_detects_non_dict_rate_limit():
    assert direct_module._has_malformed_main_limit_structure(
        [{"rate_limit": []}]
    ) is True


def test_malformed_spark_limit_structure_detects_non_list_additional_limits():
    assert direct_module._has_malformed_spark_limit_structure(
        [{"additional_rate_limits": {}}]
    ) is True


def test_select_stable_wham_usage_rejects_newer_partial_after_complete_quorum():
    complete = {
        "user_id": "user-test",
        "account_id": "account-test",
        "rate_limit": {
            "primary_window": {
                "used_percent": 3,
                "limit_window_seconds": 18_000,
                "reset_at": 1780894250,
            },
            "secondary_window": {
                "used_percent": 45,
                "limit_window_seconds": 604_800,
                "reset_at": 1781060750,
            },
        },
    }
    partial = {
        "user_id": "user-test",
        "account_id": "account-test",
        "rate_limit": {
            "secondary_window": {
                "used_percent": 46,
                "limit_window_seconds": 604_800,
                "reset_at": 1781060750,
            },
        },
    }

    with pytest.raises(DirectFetchError, match="inconsistent"):
        _select_stable_wham_usage([complete, complete, partial])


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


def test_select_stable_wham_usage_does_not_choose_unsupported_window_majority():
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
    unsupported = {
        "rate_limit": {
            "primary_window": {
                "used_percent": 5,
                "limit_window_seconds": 2592000,
                "reset_at": 1780894250,
            },
            "secondary_window": {
                "used_percent": 10,
                "limit_window_seconds": 2592000,
                "reset_at": 1781060750,
            },
        }
    }

    with pytest.raises(DirectFetchError, match="inconsistent"):
        _select_stable_wham_usage([complete, unsupported, unsupported])


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


def test_fetch_stable_wham_usage_uses_one_aggregate_deadline(monkeypatch):
    now = [100.0]
    request_timeouts: list[float] = []

    monkeypatch.setattr("codex_usage.direct.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "codex_usage.direct.time.sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    def fake_fetch(_token, *, account_id, timeout_seconds):
        request_timeouts.append(timeout_seconds)
        now[0] += 4.0
        return {}

    def reject(_payloads):
        raise DirectFetchError("inconsistent")

    monkeypatch.setattr("codex_usage.direct._fetch_wham_usage", fake_fetch)
    monkeypatch.setattr("codex_usage.direct._select_stable_wham_usage", reject)

    with pytest.raises(DirectFetchError, match="direct fetch timed out"):
        _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=10)

    assert request_timeouts == pytest.approx([10.0, 6.0, 2.0])


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
def test_fetch_stable_wham_usage_rejects_invalid_timeout(timeout_seconds, monkeypatch):
    def fail_network(*_args, **_kwargs):
        raise AssertionError("network must not be reached")

    monkeypatch.setattr("codex_usage.direct._fetch_wham_usage", fail_network)

    with pytest.raises(DirectFetchError, match="positive finite"):
        _fetch_stable_wham_usage(
            "token",
            account_id=None,
            timeout_seconds=timeout_seconds,
        )


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


def test_fetch_stable_wham_usage_accepts_latest_absolute_reset_transition(
    monkeypatch,
):
    def response(used: int, reset_at: int) -> dict:
        return {
            "user_id": "user-test",
            "account_id": "account-test",
            "rate_limit": {
                "primary_window": {
                    "used_percent": used,
                    "limit_window_seconds": 18000,
                    "reset_at": reset_at,
                },
                "secondary_window": {
                    "used_percent": 10,
                    "limit_window_seconds": 604800,
                    "reset_at": 1784415934,
                },
            },
        }

    responses = iter(
        (
            response(50, 1783860000),
            response(50, 1783860000),
            response(0, 1783878000),
        )
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    payload = _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)

    assert payload["rate_limit"]["primary_window"]["used_percent"] == 0
    assert payload["rate_limit"]["primary_window"]["reset_at"] == 1783878000


def test_fetch_stable_wham_usage_rejects_reset_with_missing_counterpart(
    monkeypatch,
):
    def response(primary_used: int, primary_after: int, *, include_secondary: bool) -> dict:
        return {
            "user_id": "user-test",
            "account_id": "account-test",
            "rate_limit": {
                "primary_window": {
                    "used_percent": primary_used,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": primary_after,
                    "reset_at": 1783860000 if primary_after < 18000 else 1783878000,
                },
                "secondary_window": (
                    {
                        "used_percent": 10,
                        "limit_window_seconds": 604800,
                        "reset_after_seconds": 604800,
                        "reset_at": 1784415934,
                    }
                    if include_secondary
                    else None
                ),
            },
        }

    responses = iter(
        (
            response(5, 120, include_secondary=True),
            response(5, 118, include_secondary=True),
            response(0, 18000, include_secondary=False),
        )
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(DirectFetchError, match="inconsistent"):
        _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)


def test_fetch_stable_wham_usage_rejects_reset_with_large_counterpart_jump(
    monkeypatch,
):
    def response(primary_used: int, primary_after: int, secondary_used: int) -> dict:
        return {
            "user_id": "user-test",
            "account_id": "account-test",
            "rate_limit": {
                "primary_window": {
                    "used_percent": primary_used,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": primary_after,
                    "reset_at": 1783860000 if primary_after < 18000 else 1783878000,
                },
                "secondary_window": {
                    "used_percent": secondary_used,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 604800,
                    "reset_at": 1784415934,
                },
            },
        }

    responses = iter(
        (
            response(5, 120, 10),
            response(5, 118, 10),
            response(0, 18000, 80),
        )
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_wham_usage",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(DirectFetchError, match="inconsistent"):
        _fetch_stable_wham_usage("token", account_id=None, timeout_seconds=1)


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


def test_fetch_account_usage_direct_rejects_auth_identity_disappearing_user_after_401(
    tmp_path, monkeypatch
):
    auth_path = tmp_path / "auth.json"
    old_token = "old-access-token"
    new_token = "new-access-token"

    def write_auth(token: str, *, include_user: bool) -> None:
        payload = {
            "tokens": {
                "access_token": token,
                "id_token": _jwt_with_claims(
                    {
                        "https://api.openai.com/auth": {
                            "chatgpt_user_id": "user-a" if include_user else "",
                            "chatgpt_plan_type": "pro",
                        }
                    }
                ),
                "account_id": "account-a",
            }
        }
        if not include_user:
            payload["tokens"].pop("id_token")
        auth_path.write_text(json.dumps(payload), encoding="utf-8")
        auth_path.chmod(0o600)

    def fake_fetch(token: str, **_kwargs):
        if token == old_token:
            write_auth(new_token, include_user=False)
            raise DirectAuthError("direct auth failed: HTTP 401")
        raise AssertionError("usage fetch should not happen after failed identity checks")

    write_auth(old_token, include_user=True)
    monkeypatch.setattr("codex_usage.direct._fetch_stable_wham_usage", fake_fetch)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error == "auth.json identity changed during usage request"


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


def test_fetch_account_usage_direct_keeps_deadline_after_rotated_auth_token(
    tmp_path,
    monkeypatch,
):
    auth_path = tmp_path / "auth.json"
    old_token = "old-access-token"
    new_token = "new-access-token"
    now = [100.0]
    request_timeouts: list[float] = []

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

    complete_payload = {
        "rate_limit": {
            "primary_window": {"used_percent": 3, "limit_window_seconds": 18000},
            "secondary_window": {"used_percent": 45, "limit_window_seconds": 604800},
        },
        "user_id": "user-a",
        "account_id": "account-a",
    }

    def fake_fetch(token: str, *, account_id, timeout_seconds):
        request_timeouts.append(timeout_seconds)
        if token == old_token:
            write_auth(new_token)
            now[0] += 9.0
            raise DirectAuthError("direct auth failed: HTTP 401")
        return complete_payload

    write_auth(old_token)
    monkeypatch.setattr("codex_usage.direct.time.monotonic", lambda: now[0])
    monkeypatch.setattr("codex_usage.direct._fetch_wham_usage", fake_fetch)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account, timeout_seconds=10)

    assert usage.status == AccountStatus.OK
    assert request_timeouts == pytest.approx([10.0, 1.0, 1.0, 1.0])


def test_fetch_account_usage_direct_retries_after_rotated_auth_token_with_suffix_error_message(
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
            raise DirectAuthError("direct auth failed: HTTP 401 (token expired)")
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
    assert usage.cache_invalidated is True


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
        status = 200

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
    assert usage.cache_invalidated is True


def test_fetch_account_usage_direct_rejects_same_account_with_different_user_id(
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
        status = 200

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

    assert usage.status == AccountStatus.ERROR
    assert usage.error == "backend response belongs to a different account"
    assert usage.cache_invalidated is True


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
        status = 200

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


def test_fetch_account_usage_direct_accepts_shared_user_alias_with_matching_plan(
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
        status = 200

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
                    "plan_type": "free",
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
    assert usage.error is None
    assert usage.main is not None
    assert usage.main.windows[0].name == "30d"
    assert usage.backend_user_id == "shared-user"
    assert usage.backend_account_id == "free-account"


@pytest.mark.parametrize("account_id", ["account\nforged", " account ", " ", 42])
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
        status = 200

        def __init__(self):
            self.headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit):
            return json.dumps(
                {
                    "user_id": "user-test",
                    "account_id": "account-test",
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


def test_fetch_account_usage_direct_keeps_model_specific_spark_limit_separate(
    tmp_path, monkeypatch
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"tokens": {"access_token": "secret-access-token"}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    payload = {
        "user_id": "user-nufker",
        "account_id": "user-nufker",
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {
                "used_percent": 20,
                "limit_window_seconds": 604800,
                "reset_at": 1784487570,
            },
            "secondary_window": None,
        },
        "additional_rate_limits": [
            {
                "limit_name": "GPT-5.3-Codex-Spark",
                "metered_feature": "codex_bengalfox",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "limit_window_seconds": 604800,
                        "reset_at": 1784497193,
                    },
                    "secondary_window": None,
                },
            }
        ],
    }

    class FakeResponse:
        status = 200

        def __init__(self):
            self.headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr("codex_usage.direct.urlopen", lambda request, *, timeout: FakeResponse())
    account = Account(
        id="nufker",
        label="Nufker",
        profile_dir="/tmp/profile",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.OK
    assert usage.five_hour is None
    assert usage.weekly is not None
    assert usage.weekly.used == 20
    assert usage.weekly.remaining == 80
    assert usage.error is None
    assert usage.main is not None
    assert usage.main.windows[0].remaining == 80
    spark = usage.model_pool("gpt-5.3-codex-spark")
    assert spark is not None
    assert spark.windows[0].remaining == 99


def test_fetch_account_usage_direct_supports_plan_specific_30_day_window(
    tmp_path, monkeypatch
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"tokens": {"access_token": "secret-access-token"}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    class FakeResponse:
        status = 200

        def __init__(self):
            self.headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit):
            return json.dumps(
                {
                    "user_id": "user-test",
                    "account_id": "account-test",
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

    assert usage.status == AccountStatus.OK
    assert usage.five_hour is None
    assert usage.weekly is None
    assert usage.error is None
    assert usage.main is not None
    assert usage.main.windows[0].name == "30d"
    assert usage.main.windows[0].remaining == 95


def test_fetch_account_usage_direct_ignores_overflowing_window_duration(
    tmp_path,
    monkeypatch,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"tokens": {"access_token": "secret-access-token"}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit):
            return json.dumps(
                {
                    "user_id": "user-test",
                    "account_id": "account-test",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 5,
                            "limit_window_seconds": 10**309,
                        }
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
    assert usage.five_hour is None
    assert usage.weekly is None
    assert usage.error == "usage limits not found in direct response"


def test_fetch_account_usage_direct_reports_single_available_window(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"tokens": {"access_token": "secret-access-token"}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    class FakeResponse:
        status = 200

        def __init__(self):
            self.headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit):
            return json.dumps(
                {
                    "user_id": "user-test",
                    "account_id": "account-test",
                    "plan_type": "pro",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 47,
                            "limit_window_seconds": 604800,
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

    assert usage.status == AccountStatus.OK
    assert usage.five_hour is None
    assert usage.weekly is not None and usage.weekly.remaining == 53
    assert usage.error is None
    assert usage.main is not None
    assert usage.main.windows[0].remaining == 53


def test_fetch_account_usage_direct_marks_malformed_main_slot_partial(
    tmp_path, monkeypatch
):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )
    payload = {
        "user_id": "user-test",
        "account_id": "account-test",
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": "malformed",
            "secondary_window": {
                "used_percent": 20,
                "limit_window_seconds": 604800,
            },
        },
    }
    auth_result = ("token", {}, "user-test", "account-test", "pro")
    monkeypatch.setattr(
        "codex_usage.direct._load_auth_token_and_metadata",
        lambda _path: auth_result,
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_stable_wham_usage",
        lambda *_args, **_kwargs: payload,
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.PARTIAL
    assert usage.main is not None
    assert usage.main.available is False
    assert usage.weekly is not None and usage.weekly.remaining == 80


def test_fetch_account_usage_direct_marks_reset_only_main_slot_partial(
    tmp_path, monkeypatch
):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )
    payload = {
        "user_id": "user-test",
        "account_id": "account-test",
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {"limit_window_seconds": 18000},
            "secondary_window": {
                "used_percent": 20,
                "limit_window_seconds": 604800,
            },
        },
    }
    auth_result = ("token", {}, "user-test", "account-test", "pro")
    monkeypatch.setattr(
        "codex_usage.direct._load_auth_token_and_metadata",
        lambda _path: auth_result,
    )
    monkeypatch.setattr(
        "codex_usage.direct._fetch_stable_wham_usage",
        lambda *_args, **_kwargs: payload,
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.PARTIAL
    assert usage.main is not None
    assert usage.main.available is True
    assert usage.main.windows[0].has_usage_value is False
    assert usage.weekly is not None and usage.weekly.remaining == 80


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


def test_fetch_account_usage_direct_rejects_symlink_auth_parent(tmp_path, monkeypatch):
    target_dir = tmp_path / "target-auth"
    target_dir.mkdir()
    auth_path = target_dir / "auth.json"
    auth_path.write_text(
        json.dumps({"tokens": {"access_token": "secret-access-token"}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    linked_parent = tmp_path / "linked-auth"
    linked_parent.symlink_to(target_dir, target_is_directory=True)

    def fake_urlopen(request, *, timeout):
        raise AssertionError("network must not be reached for symlink auth parent")

    monkeypatch.setattr("codex_usage.direct.urlopen", fake_urlopen)
    account = Account(
        id="privat",
        label="Privat",
        profile_dir="/tmp/profile",
        auth_json_path=str(linked_parent / "auth.json"),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.LOGIN_REQUIRED
    assert usage.error is not None
    assert "auth.json parent" in usage.error
    assert "symlink ancestors" in usage.error
    assert "secret-access-token" not in usage.error


def test_auth_json_helpers_accept_inherited_regular_fd(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text('{"tokens": {"access_token": "token"}}', encoding="utf-8")
    auth_path.chmod(0o600)
    fd = os.open(auth_path, os.O_RDONLY)
    try:
        proc_path = Path(f"/proc/self/fd/{fd}")
        raw, file_stat = read_auth_json_file(proc_path)
        raw_again, _ = read_auth_json_file(proc_path)
        validated = validate_auth_json_file(proc_path)
    finally:
        os.close(fd)

    assert raw == '{"tokens": {"access_token": "token"}}'
    assert raw_again == raw
    assert file_stat.st_ino == validated.st_ino


def test_validate_auth_json_stat_accepts_secure_regular_file(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text('{"tokens": {"access_token": "token"}}', encoding="utf-8")
    auth_path.chmod(0o600)

    direct_module._validate_auth_json_stat(auth_path, auth_path.stat())


def test_auth_identity_from_file_rejects_payload_dict_subclass(
    tmp_path, monkeypatch
):
    class BrokenPayload(dict):
        pass

    path = tmp_path / "auth.json"
    monkeypatch.setattr(
        direct_module,
        "read_auth_json_file",
        lambda _path: ("{}", None),
    )
    monkeypatch.setattr(
        direct_module,
        "loads_strict",
        lambda _raw: BrokenPayload(),
    )

    with pytest.raises(DirectAuthError, match=r"invalid auth\.json structure"):
        auth_identity_from_file(path)


def test_load_auth_token_and_metadata_rejects_payload_dict_subclass(
    tmp_path, monkeypatch
):
    class BrokenPayload(dict):
        pass

    path = tmp_path / "auth.json"
    monkeypatch.setattr(
        direct_module,
        "read_auth_json_file",
        lambda _path: ("{}", None),
    )
    monkeypatch.setattr(
        direct_module,
        "loads_strict",
        lambda _raw: BrokenPayload(),
    )

    with pytest.raises(DirectAuthError, match=r"invalid auth\.json structure"):
        direct_module._load_auth_token_and_metadata(path)


def test_auth_email_from_file_rejects_payload_dict_subclass(tmp_path, monkeypatch):
    class BrokenPayload(dict):
        pass

    path = tmp_path / "auth.json"
    monkeypatch.setattr(
        direct_module,
        "read_auth_json_file",
        lambda _path: ("{}", None),
    )
    monkeypatch.setattr(
        direct_module,
        "loads_strict",
        lambda _raw: BrokenPayload(),
    )

    with pytest.raises(DirectAuthError, match=r"invalid auth\.json structure"):
        auth_email_from_file(path)


def test_auth_plan_type_from_file_rejects_payload_dict_subclass(
    tmp_path, monkeypatch
):
    class BrokenPayload(dict):
        pass

    path = tmp_path / "auth.json"
    monkeypatch.setattr(
        direct_module,
        "read_auth_json_file",
        lambda _path: ("{}", None),
    )
    monkeypatch.setattr(
        direct_module,
        "loads_strict",
        lambda _raw: BrokenPayload(),
    )

    with pytest.raises(DirectAuthError, match=r"invalid auth\.json structure"):
        auth_plan_type_from_file(path)


@pytest.mark.parametrize("path", [None, [], "invalid", 1, False, object()])
def test_auth_file_helpers_reject_non_path(path):
    for helper in (
        auth_identity_from_file,
        auth_email_from_file,
        auth_plan_type_from_file,
        read_auth_json_file,
        validate_auth_json_file,
    ):
        with pytest.raises(DirectAuthError, match=r"auth\.json path is invalid"):
            helper(path)  # type: ignore[arg-type]


def test_auth_file_helpers_reject_unknown_user_home():
    path = Path("~definitely-no-such-user-zzzz/auth.json")
    for helper in (auth_identity_from_file, auth_email_from_file, auth_plan_type_from_file):
        with pytest.raises(DirectAuthError, match=r"auth\.json path is invalid"):
            helper(path)


@pytest.mark.parametrize("account", [None, [], 1, object()])
def test_auth_account_helpers_reject_non_account(account):
    for helper in (auth_identity_for_account, auth_plan_type_for_account):
        with pytest.raises(DirectAuthError, match="account is invalid"):
            helper(account)  # type: ignore[arg-type]


def test_auth_account_helpers_reject_auth_path_string_subclass_hooks():
    class BrokenStr(str):
        def __bool__(self):
            raise RuntimeError("synthetic account auth path marker")

    account = Account(
        id="work",
        label="Work",
        profile_dir="/tmp/work",
        auth_json_path=BrokenStr("/tmp/auth.json"),
    )
    for helper in (auth_identity_for_account, auth_plan_type_for_account):
        with pytest.raises(DirectAuthError, match=r"auth\.json path is invalid"):
            helper(account)


def test_auth_json_helpers_reject_hard_linked_file(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        '{"tokens": {"access_token": "token"}}',
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    os.link(auth_path, tmp_path / "auth-copy.json")

    assert auth_path.stat().st_nlink == 2
    with pytest.raises(DirectAuthError, match="hard-linked"):
        read_auth_json_file(auth_path)
    with pytest.raises(DirectAuthError, match="hard-linked"):
        validate_auth_json_file(auth_path)


def test_auth_json_helpers_reject_inherited_non_regular_fd():
    read_fd, write_fd = os.pipe()
    try:
        proc_path = Path(f"/proc/self/fd/{read_fd}")
        with pytest.raises(DirectAuthError, match=r"auth\.json is not a regular file"):
            read_auth_json_file(proc_path)
        with pytest.raises(DirectAuthError, match=r"auth\.json is not a regular file"):
            validate_auth_json_file(proc_path)
    finally:
        os.close(read_fd)
        os.close(write_fd)


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
        status = 200

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
    assert usage.cache_invalidated is True


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
