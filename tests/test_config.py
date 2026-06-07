from __future__ import annotations

import pytest

from codex_usage.config import (
    AppConfig,
    add_or_update_account,
    load_config,
    remove_account,
    resolve_account,
    save_config,
)
from codex_usage.models import Account


def test_add_account_rejects_dot_segments(tmp_path):
    with pytest.raises(ValueError):
        add_or_update_account(".", path=tmp_path / "config.toml")
    with pytest.raises(ValueError):
        add_or_update_account("..", path=tmp_path / "config.toml")


def test_config_round_trip_quotes_and_newlines(tmp_path):
    config_path = tmp_path / "config.toml"
    _, account = add_or_update_account(
        "privat",
        label='Privat "Main"\nAccount',
        path=config_path,
    )

    loaded = load_config(config_path)

    assert loaded.accounts == (account,)
    assert loaded.accounts[0].label == 'Privat "Main"\nAccount'
    assert loaded.accounts[0].browser == "firefox"


def test_config_round_trip_browser(tmp_path):
    config_path = tmp_path / "config.toml"
    add_or_update_account("privat", browser="chromium", path=config_path)

    loaded = load_config(config_path)

    assert loaded.accounts[0].browser == "chromium"


def test_config_rejects_unknown_browser(tmp_path):
    with pytest.raises(ValueError, match="browser must be one of"):
        add_or_update_account("privat", browser="netscape", path=tmp_path / "config.toml")


def test_load_config_rejects_loose_types(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
interval_seconds = 300.5
headless = "false"

[[accounts]]
id = "privat"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_config(config_path)


def test_load_config_rejects_external_analytics_url(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
interval_seconds = 300
analytics_url = "https://example.com/codex/cloud/settings/analytics"

[[accounts]]
id = "privat"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_config(config_path)


def test_load_config_rejects_duplicate_accounts(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[[accounts]]
id = "privat"

[[accounts]]
id = "privat"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_config(config_path)


def test_save_config_sets_private_file_mode(tmp_path):
    config_path = tmp_path / "config.toml"
    save_config(
        AppConfig(
            accounts=(Account(id="privat", label="Privat", profile_dir="/tmp/profile"),),
            interval_seconds=300,
            analytics_url="https://chatgpt.com/codex/cloud/settings/analytics",
            headless=True,
        ),
        config_path,
    )

    assert oct(config_path.stat().st_mode & 0o777) == "0o600"


def test_resolve_account_accepts_id_or_unique_label(tmp_path):
    config_path = tmp_path / "config.toml"
    add_or_update_account("privat", label="BW_Privat", path=config_path)
    config = load_config(config_path)

    assert resolve_account(config, "privat").id == "privat"
    assert resolve_account(config, "BW_Privat").id == "privat"


def test_resolve_account_rejects_ambiguous_label():
    config = AppConfig(
        accounts=(
            Account(id="privat", label="BW", profile_dir="/tmp/privat"),
            Account(id="arbeit", label="BW", profile_dir="/tmp/arbeit"),
        )
    )

    with pytest.raises(KeyError, match="ambiguous account label"):
        resolve_account(config, "BW")


def test_remove_account_accepts_unique_label_and_keeps_profile(tmp_path):
    config_path = tmp_path / "config.toml"
    profile_dir = tmp_path / "profile"
    add_or_update_account(
        "privat",
        label="BW_Privat",
        profile_dir=str(profile_dir),
        path=config_path,
    )

    updated, removed = remove_account("BW_Privat", path=config_path)

    assert removed.id == "privat"
    assert updated.accounts == ()
    assert load_config(config_path).accounts == ()
    assert profile_dir.is_dir()
    assert (profile_dir / ".codex-usage-profile").is_file()
