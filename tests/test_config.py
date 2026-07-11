from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from codex_usage.config import (
    MAX_CONFIG_BYTES,
    AppConfig,
    add_or_update_account,
    load_config,
    remove_account,
    resolve_account,
    save_config,
)
from codex_usage.models import Account


def test_concurrent_account_updates_keep_each_valid_account(tmp_path):
    config_path = tmp_path / "config.toml"

    def add(account_id):
        add_or_update_account(
            account_id,
            label=account_id.upper(),
            profile_dir=str(tmp_path / "profiles" / account_id),
            path=config_path,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(add, ("one", "two", "three", "four")))

    config = load_config(config_path)
    assert {account.id for account in config.accounts} == {"one", "two", "three", "four"}


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


def test_config_round_trip_backend_and_legacy_default(tmp_path):
    config_path = tmp_path / "config.toml"
    add_or_update_account("privat", backend="app-server", path=config_path)

    loaded = load_config(config_path)

    assert loaded.accounts[0].backend == "app-server"
    assert 'backend = "app-server"' in config_path.read_text(encoding="utf-8")

    legacy = tmp_path / "legacy.toml"
    legacy.write_text('[[accounts]]\nid = "legacy"\n', encoding="utf-8")
    assert load_config(legacy).accounts[0].backend == "direct"


def test_config_rejects_unknown_backend(tmp_path):
    with pytest.raises(ValueError, match="backend must be one of"):
        add_or_update_account(
            "privat",
            backend="mystery",
            path=tmp_path / "config.toml",
        )


def test_config_round_trip_auth_json_path(tmp_path):
    config_path = tmp_path / "config.toml"
    auth_path = tmp_path / "auth.json"
    add_or_update_account("privat", auth_json_path=str(auth_path), path=config_path)

    loaded = load_config(config_path)

    assert loaded.accounts[0].auth_json_path == str(auth_path)
    assert f'auth_json_path = "{auth_path}"' in config_path.read_text(encoding="utf-8")


def test_add_account_rejects_symlink_profile_dir_without_marking_target(tmp_path):
    config_path = tmp_path / "config.toml"
    target = tmp_path / "target-profile"
    target.mkdir()
    profile_link = tmp_path / "profile-link"
    profile_link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        add_or_update_account("privat", profile_dir=str(profile_link), path=config_path)

    assert not config_path.exists()
    assert not (target / ".codex-usage-profile").exists()


def test_add_account_rejects_symlinked_config_home(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    config_home = tmp_path / "config-home"
    config_home.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    with pytest.raises(ValueError, match="symlink ancestors"):
        add_or_update_account("privat")

    assert not (outside / "codex-usage").exists()


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


def test_load_config_rejects_symlink_config_file(tmp_path):
    target = tmp_path / "outside.toml"
    target.write_text("interval_seconds = 300\n", encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.symlink_to(target)

    with pytest.raises(ValueError, match="config path"):
        load_config(config_path)


def test_load_config_rejects_oversized_config_file(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(" " * (MAX_CONFIG_BYTES + 1), encoding="utf-8")

    with pytest.raises(ValueError, match="config file too large"):
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


def test_load_config_rejects_label_matching_another_account_id(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[[accounts]]
id = "privat"
label = "work"
profile_dir = "/tmp/privat"

[[accounts]]
id = "work"
label = "Work"
profile_dir = "/tmp/work"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="label conflicts with another account id"):
        load_config(config_path)


def test_save_config_rejects_shared_profile_directory(tmp_path):
    profile = tmp_path / "profiles" / "shared"
    config = AppConfig(
        accounts=(
            Account(id="one", label="One", profile_dir=str(profile)),
            Account(
                id="two",
                label="Two",
                profile_dir=str(profile / ".." / "shared"),
            ),
        )
    )

    with pytest.raises(ValueError, match="duplicate profile_dir"):
        save_config(config, tmp_path / "config.toml")


def test_save_config_rejects_shared_auth_json_path(tmp_path):
    auth_path = tmp_path / "auth.json"
    config = AppConfig(
        accounts=(
            Account(
                id="one",
                label="One",
                profile_dir=str(tmp_path / "profiles" / "one"),
                auth_json_path=str(auth_path),
            ),
            Account(
                id="two",
                label="Two",
                profile_dir=str(tmp_path / "profiles" / "two"),
                auth_json_path=str(auth_path.parent / "." / auth_path.name),
            ),
        )
    )

    with pytest.raises(ValueError, match="duplicate auth_json_path"):
        save_config(config, tmp_path / "config.toml")


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


def test_save_config_rejects_symlink_config_file_without_overwriting_target(tmp_path):
    target = tmp_path / "outside.toml"
    target.write_text("keep", encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.symlink_to(target)

    with pytest.raises(ValueError, match="config path"):
        save_config(AppConfig(accounts=()), config_path)

    assert target.read_text(encoding="utf-8") == "keep"


def test_save_config_rejects_symlink_config_directory_without_writing_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    config_dir = tmp_path / "config"
    config_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="config directory"):
        save_config(AppConfig(accounts=()), config_dir / "config.toml")

    assert not (outside / "config.toml").exists()


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
