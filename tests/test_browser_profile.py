from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Error as PlaywrightError

from codex_usage.browser import (
    _format_datetime,
    _prepare_profile,
    _profile_lock,
    fetch_account_usage,
)
from codex_usage.config import AppConfig
from codex_usage.models import Account, LimitWindow


def test_browser_diagnostic_datetime_uses_dst_aware_local_timezone(monkeypatch):
    berlin = ZoneInfo("Europe/Berlin")
    value = datetime(2026, 1, 15, 0, 15, tzinfo=ZoneInfo("UTC"))

    monkeypatch.setattr("codex_usage.browser.LOCAL_TZ", berlin)

    assert _format_datetime(value) == "2026-01-15T01:15:00+01:00"


def test_prepare_profile_rejects_symlink_root_without_marking_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    profile_link = tmp_path / "profile"
    profile_link.symlink_to(target, target_is_directory=True)
    account = Account(id="privat", label="Privat", profile_dir=str(profile_link))

    with pytest.raises(ValueError, match="profile directory"):
        _prepare_profile(account)

    assert not (target / ".codex-usage-profile").exists()


def test_prepare_profile_rejects_symlink_browser_dir_without_marking_target(tmp_path):
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    target = tmp_path / "firefox-target"
    target.mkdir()
    (profile_root / "firefox").symlink_to(target, target_is_directory=True)
    account = Account(id="privat", label="Privat", profile_dir=str(profile_root))

    with pytest.raises(ValueError, match="browser profile directory"):
        _prepare_profile(account)

    assert not (target / ".codex-usage-browser-profile").exists()


def test_prepare_profile_rejects_symlink_root_marker_without_overwriting_target(tmp_path):
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    target = tmp_path / "outside-marker"
    target.write_text("keep", encoding="utf-8")
    (profile_root / ".codex-usage-profile").symlink_to(target)
    account = Account(id="privat", label="Privat", profile_dir=str(profile_root))

    with pytest.raises(ValueError, match="profile marker path"):
        _prepare_profile(account)

    assert target.read_text(encoding="utf-8") == "keep"


def test_prepare_profile_rejects_symlink_browser_marker_without_overwriting_target(tmp_path):
    browser_dir = tmp_path / "profile" / "firefox"
    browser_dir.mkdir(parents=True)
    target = tmp_path / "outside-browser-marker"
    target.write_text("keep", encoding="utf-8")
    (browser_dir / ".codex-usage-browser-profile").symlink_to(target)
    account = Account(id="privat", label="Privat", profile_dir=str(tmp_path / "profile"))

    with pytest.raises(ValueError, match="browser profile marker path"):
        _prepare_profile(account)

    assert target.read_text(encoding="utf-8") == "keep"


def test_profile_lock_rejects_symlink_lock_without_overwriting_target(tmp_path):
    profile_dir = tmp_path / "profile" / "firefox"
    profile_dir.mkdir(parents=True)
    target = tmp_path / "outside-lock"
    target.write_text("keep", encoding="utf-8")
    (profile_dir / ".codex-usage.lock").symlink_to(target)

    with pytest.raises(ValueError, match="profile lock path"):
        with _profile_lock(profile_dir):
            pass

    assert target.read_text(encoding="utf-8") == "keep"


def test_fetch_closes_context_when_navigation_fails(tmp_path, monkeypatch):
    account = Account(id="privat", label="Privat", profile_dir=str(tmp_path / "profile"))
    context_state = {"closed": False}

    class FakePage:
        def on(self, *_args):
            return None

        def goto(self, *_args, **_kwargs):
            raise PlaywrightError("navigation failed")

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            context_state["closed"] = True

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    context = FakeContext()
    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: context,
    )

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status.value == "error"
    assert context_state["closed"] is True


def test_fetch_canonicalizes_browser_identity_from_configured_auth(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )

    class FakeLocator:
        def inner_text(self, *, timeout):
            return "5-hour usage limit 97% Weekly usage limit 55%"

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, *_args):
            return None

        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, *_args):
            return FakeLocator()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    extract_kwargs = {}

    def fake_extract_windows(**kwargs):
        extract_kwargs.update(kwargs)
        return (
            LimitWindow(name="5h", remaining=97),
            LimitWindow(name="weekly", remaining=55),
        )

    monkeypatch.setattr("codex_usage.browser.extract_windows", fake_extract_windows)
    monkeypatch.setattr(
        "codex_usage.browser.backend_identity_from_candidates",
        lambda _candidates: ("user-test", "user-test"),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: ("user-test", "account-uuid"),
    )
    monkeypatch.setattr("codex_usage.browser.auth_plan_type_for_account", lambda _account: None)

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status == "ok"
    assert usage.backend_user_id == "user-test"
    assert usage.backend_account_id == "account-uuid"
    assert extract_kwargs["now"] == usage.captured_at


def test_fetch_does_not_merge_dom_values_with_authenticated_json_usage(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )

    class FakeResponse:
        def __init__(self):
            self.url = "https://chatgpt.com/backend-api/wham/usage"
            self.headers = {"content-type": "application/json"}

        def finished(self):
            return None

        def text(self):
            return (
                '{"user_id":"user-test","account_id":"account-uuid",'
                '"rate_limit":{"primary_window":{"used_percent":3,'
                '"limit_window_seconds":18000}}}'
            )

    class FakeLocator:
        def inner_text(self, *, timeout):
            return "5-hour limit 97% remaining Weekly limit 55% remaining"

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, event, callback):
            if event == "response":
                callback(FakeResponse())

        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, *_args):
            return FakeLocator()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: ("user-test", "account-uuid"),
    )
    monkeypatch.setattr("codex_usage.browser.auth_plan_type_for_account", lambda _account: None)

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status == "partial"
    assert usage.five_hour is not None and usage.five_hour.remaining == 97
    assert usage.weekly is None


def test_fetch_reports_missing_paid_five_hour_window_from_json(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )

    class FakeResponse:
        url = "https://chatgpt.com/backend-api/wham/usage"
        headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}

        def finished(self):
            return None

        def text(self):
            return (
                '{"user_id":"user-test","account_id":"account-uuid",'
                '"plan_type":"pro","rate_limit":{"primary_window":'
                '{"used_percent":10,"limit_window_seconds":604800,'
                '"reset_at":"2026-07-19T20:59:30+02:00"},'
                '"secondary_window":null}}'
            )

    class FakeLocator:
        def inner_text(self, *, timeout):
            return "Codex analytics page"

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, event, callback):
            if event == "response":
                callback(FakeResponse())

        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, *_args):
            return FakeLocator()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: ("user-test", "account-uuid"),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_plan_type_for_account",
        lambda _account: "pro",
    )

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status == "partial"
    assert usage.five_hour is None
    assert usage.weekly is not None and usage.weekly.remaining == 90
    assert usage.error is None


def test_fetch_rejects_browser_auth_identity_changed_during_request(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )

    class FakeLocator:
        def inner_text(self, *, timeout):
            return "5-hour usage limit 97% Weekly usage limit 55%"

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, *_args):
            return None

        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, *_args):
            return FakeLocator()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    monkeypatch.setattr(
        "codex_usage.browser.extract_windows",
        lambda **_kwargs: (
            LimitWindow(name="5h", remaining=97),
            LimitWindow(name="weekly", remaining=55),
        ),
    )
    monkeypatch.setattr(
        "codex_usage.browser.backend_identity_from_candidates",
        lambda _candidates: ("old-user", "old-account"),
    )
    identities = iter(
        [("old-user", "old-account"), ("new-user", "new-account")]
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: next(identities),
    )
    monkeypatch.setattr("codex_usage.browser.auth_plan_type_for_account", lambda _account: None)

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status == "login_required"
    assert usage.error == "auth.json identity changed during browser request"


def test_fetch_accepts_browser_pro_plus_plan_alias_transition(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )

    class FakeLocator:
        def inner_text(self, *, timeout):
            return "5-hour usage limit 97% Weekly usage limit 55%"

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, *_args):
            return None

        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, *_args):
            return FakeLocator()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: ("user-test", "account-uuid"),
    )
    plan_types = iter(("pro", "plus"))
    monkeypatch.setattr(
        "codex_usage.browser.auth_plan_type_for_account",
        lambda _account: next(plan_types),
    )
    monkeypatch.setattr(
        "codex_usage.browser.backend_identity_from_candidates",
        lambda _candidates: ("user-test", "account-uuid"),
    )
    monkeypatch.setattr(
        "codex_usage.browser.extract_windows",
        lambda **_kwargs: (
            LimitWindow(name="5h", remaining=97),
            LimitWindow(name="weekly", remaining=55),
        ),
    )

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status == "ok"
    assert usage.error is None
    assert usage.five_hour is not None and usage.five_hour.remaining == 97
    assert usage.weekly is not None and usage.weekly.remaining == 55


def test_fetch_rejects_browser_values_without_backend_identity(tmp_path, monkeypatch):
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "auth.json"),
    )

    class FakeLocator:
        def inner_text(self, *, timeout):
            return "5-hour usage limit 97% Weekly usage limit 55%"

    class FakePage:
        url = "https://chatgpt.com/codex/cloud/settings/analytics"

        def on(self, *_args):
            return None

        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def locator(self, *_args):
            return FakeLocator()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "codex_usage.browser._prepare_profile",
        lambda _account: tmp_path / "profile",
    )
    monkeypatch.setattr("codex_usage.browser._profile_lock", lambda _profile: nullcontext())
    monkeypatch.setattr("codex_usage.browser.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        "codex_usage.browser._launch_persistent_context",
        lambda *_args, **_kwargs: FakeContext(),
    )
    monkeypatch.setattr(
        "codex_usage.browser.extract_windows",
        lambda **_kwargs: (
            LimitWindow(name="5h", remaining=97),
            LimitWindow(name="weekly", remaining=55),
        ),
    )
    monkeypatch.setattr(
        "codex_usage.browser.backend_identity_from_candidates",
        lambda _candidates: (None, None),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: ("user-test", "account-uuid"),
    )
    monkeypatch.setattr("codex_usage.browser.auth_plan_type_for_account", lambda _account: None)

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status == "error"
    assert usage.error == "backend response has no account identity"
