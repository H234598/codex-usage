from __future__ import annotations

from contextlib import nullcontext

import pytest
from playwright.sync_api import Error as PlaywrightError

from codex_usage.browser import _prepare_profile, _profile_lock, fetch_account_usage
from codex_usage.config import AppConfig
from codex_usage.models import Account, LimitWindow


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
    monkeypatch.setattr(
        "codex_usage.browser.extract_windows",
        lambda **_kwargs: (
            LimitWindow(name="5h", remaining=97),
            LimitWindow(name="weekly", remaining=55),
        ),
    )
    monkeypatch.setattr(
        "codex_usage.browser.backend_identity_from_candidates",
        lambda _candidates: ("user-test", "user-test"),
    )
    monkeypatch.setattr(
        "codex_usage.browser.auth_identity_for_account",
        lambda _account: ("user-test", "account-uuid"),
    )

    usage = fetch_account_usage(account, AppConfig(accounts=(account,)))

    assert usage.status == "ok"
    assert usage.backend_user_id == "user-test"
    assert usage.backend_account_id == "account-uuid"
