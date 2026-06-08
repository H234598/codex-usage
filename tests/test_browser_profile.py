from __future__ import annotations

import pytest

from codex_usage.browser import _prepare_profile, _profile_lock
from codex_usage.models import Account


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
