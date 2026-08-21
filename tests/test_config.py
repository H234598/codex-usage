from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

import codex_usage.config as config_module
from codex_usage.account_lock import AccountLockError, account_lock
from codex_usage.bridge import load_latest_usages
from codex_usage.config import (
    MAX_CONFIG_BYTES,
    AppConfig,
    add_or_update_account,
    load_config,
    remove_account,
    resolve_account,
    restore_account,
    save_config,
)
from codex_usage.models import Account, AccountUsage, LimitWindow
from codex_usage.state import load_current_usage, save_current_usage, save_usage_snapshot


def test_relative_xdg_data_home_is_ignored_for_default_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("XDG_DATA_HOME", "relative-data")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))

    _, account = add_or_update_account(
        "new-account",
        path=tmp_path / "config.toml",
    )

    expected = (
        tmp_path
        / "home"
        / ".local"
        / "share"
        / "codex-usage"
        / "profiles"
        / "new-account"
    )
    assert account.profile_dir == str(expected)
    assert expected.is_dir()


def test_test_home_moves_auth_and_initializes_file_store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    calls = []
    monkeypatch.setattr(
        config_module.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )
    source = tmp_path / "incoming" / "auth.json"
    source.parent.mkdir()
    source.write_text('{"tokens": {}}\n', encoding="utf-8")

    _, account = add_or_update_account(
        "test-account",
        auth_json_path=str(source),
        test_home=True,
        path=tmp_path / "config.toml",
    )

    profile = home / ".codex-test" / "test-account"
    target = profile / "codex-home" / "auth.json"
    assert account.profile_dir == str(profile)
    assert account.auth_json_path == str(target)
    assert not source.exists()
    assert target.read_text(encoding="utf-8") == '{"tokens": {}}\n'
    assert (profile / "codex-home" / "config.toml").read_text(encoding="utf-8") == (
        'cli_auth_credentials_store = "file"\n'
    )
    assert calls[0][0] == ["codex", "--help"]
    assert calls[0][1]["env"]["CODEX_HOME"] == str(profile / "codex-home")


def test_test_home_state_cleanup_failure_restores_auth_and_profile(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(config_module.subprocess, "run", lambda *_args, **_kwargs: None)
    source = tmp_path / "incoming" / "auth.json"
    source.parent.mkdir()
    source.write_text('{"tokens": {}}\n', encoding="utf-8")
    config_path = tmp_path / "config.toml"

    def fail_cleanup(*_args):
        raise OSError("state cleanup failed")

    monkeypatch.setattr("codex_usage.state.remove_account_state", fail_cleanup)

    with pytest.raises(OSError, match="state cleanup failed"):
        add_or_update_account(
            "test-account",
            auth_json_path=str(source),
            test_home=True,
            path=config_path,
        )

    assert source.read_text(encoding="utf-8") == '{"tokens": {}}\n'
    assert not (home / ".codex-test" / "test-account").exists()
    assert load_config(config_path).accounts == ()


def test_internal_all_accounts_lock_name_is_not_a_valid_account_id():
    with pytest.raises(ValueError, match="reserved"):
        config_module._validate_account_id("__all_accounts__")


def test_account_paths_expand_tilde_to_absolute_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"

    _, account = add_or_update_account(
        "tilde",
        profile_dir="~/profile",
        auth_json_path="~/auth.json",
        path=config_path,
    )

    expected_profile = str(home / "profile")
    expected_auth = str(home / "auth.json")
    assert account.profile_dir == expected_profile
    assert account.auth_json_path == expected_auth
    loaded = load_config(config_path)
    assert loaded.accounts[0].profile_dir == expected_profile
    assert loaded.accounts[0].auth_json_path == expected_auth


def test_account_paths_accept_local_file_uris_from_applet(tmp_path):
    config_path = tmp_path / "config.toml"
    profile_path = tmp_path / "profiles" / "uri account"
    auth_path = tmp_path / "auth files" / "auth.json"

    _, account = add_or_update_account(
        "uri-account",
        profile_dir=profile_path.as_uri(),
        auth_json_path=auth_path.as_uri(),
        path=config_path,
    )

    assert account.profile_dir == str(profile_path)
    assert account.auth_json_path == str(auth_path)
    assert profile_path.is_dir()


def test_account_paths_accept_localhost_file_uri(tmp_path):
    profile_path = tmp_path / "profile"
    local_uri = profile_path.as_uri().replace("file:///", "file://localhost/", 1)

    assert config_module._absolute_account_path(local_uri, "profile_dir") == str(
        profile_path
    )


def test_account_paths_reject_decoded_nul_in_local_file_uri():
    with pytest.raises(ValueError, match="local file URI"):
        config_module._absolute_account_path(
            "file:///tmp/profile%00escape",
            "profile_dir",
        )


def test_account_paths_reject_native_nul_path():
    with pytest.raises(ValueError, match="absolute path"):
        config_module._absolute_account_path(
            "/tmp/profile\x00escape",
            "profile_dir",
        )


@pytest.mark.parametrize(
    "value",
    (
        "file://server/tmp/profile",
        "file://",
        "file://localhost",
        "https://example.test/profile",
    ),
)
def test_account_paths_reject_non_local_file_uris(value):
    with pytest.raises(ValueError, match="local file URI"):
        config_module._absolute_account_path(value, "profile_dir")


def test_loading_legacy_tilde_account_paths_returns_absolute_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[[accounts]]
id = "legacy"
profile_dir = "~/profile"
auth_json_path = "~/auth.json"
""",
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    account = load_config(config_path).accounts[0]

    assert account.profile_dir == str(home / "profile")
    assert account.auth_json_path == str(home / "auth.json")


def test_load_config_rejects_group_readable_file(tmp_path):
    config_path = tmp_path / "config.toml"
    add_or_update_account("work", path=config_path)
    config_path.chmod(0o640)

    with pytest.raises(ValueError, match="config file permissions"):
        load_config(config_path)


def test_load_config_rejects_hard_linked_file(tmp_path):
    config_path = tmp_path / "config.toml"
    add_or_update_account("work", path=config_path)
    os.link(config_path, tmp_path / "config-copy.toml")

    assert config_path.stat().st_nlink == 2
    with pytest.raises(ValueError, match="config file must not be hard-linked"):
        load_config(config_path)


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


def test_add_account_rejects_case_variant_active_series_conflict(tmp_path):
    config_path = tmp_path / "config.toml"
    add_or_update_account(
        "one",
        series="A",
        series_active=True,
        path=config_path,
    )

    with pytest.raises(ValueError, match="series conflict"):
        add_or_update_account(
            "two",
            series="a",
            series_active=True,
            path=config_path,
        )


def test_add_account_rejects_dot_segments(tmp_path):
    with pytest.raises(ValueError):
        add_or_update_account(".", path=tmp_path / "config.toml")
    with pytest.raises(ValueError):
        add_or_update_account("..", path=tmp_path / "config.toml")


def test_add_account_rejects_home_as_profile_without_chmod_or_marker(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = tmp_path / "config-dir" / "config.toml"
    tmp_path.chmod(0o755)

    with pytest.raises(ValueError, match="protected"):
        add_or_update_account("unsafe", profile_dir=str(tmp_path), path=config_path)

    assert oct(tmp_path.stat().st_mode & 0o777) == "0o755"
    assert not (tmp_path / ".codex-usage-profile").exists()
    assert not config_path.exists()


def test_add_account_rejects_home_as_config_directory_without_chmod(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    tmp_path.chmod(0o755)

    with pytest.raises(ValueError, match="protected"):
        add_or_update_account("unsafe", path=config_path)

    assert oct(tmp_path.stat().st_mode & 0o777) == "0o755"
    assert not config_path.exists()


def test_config_directory_rejects_existing_shared_directory_without_chmod(tmp_path):
    config_dir = tmp_path / "shared"
    config_dir.mkdir()
    config_dir.chmod(0o755)

    with pytest.raises(ValueError, match="private config directory"):
        config_module._prepare_config_directory(config_dir)

    assert oct(config_dir.stat().st_mode & 0o777) == "0o755"


def test_add_account_fails_closed_when_config_directory_cannot_be_secured(
    tmp_path, monkeypatch
):
    config_dir = tmp_path / "config-dir"
    config_path = config_dir / "config.toml"
    original_chmod = config_module.Path.chmod

    def fail_config_chmod(path, mode):
        if path == config_dir:
            raise OSError("simulated config chmod failure")
        return original_chmod(path, mode)

    monkeypatch.setattr(config_module.Path, "chmod", fail_config_chmod)

    with pytest.raises(ValueError, match="secure config directory"):
        add_or_update_account("work", path=config_path)

    assert not config_path.exists()


def test_add_account_fails_closed_when_profile_directory_cannot_be_secured(
    tmp_path, monkeypatch
):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    config_path = tmp_path / "config" / "config.toml"
    original_chmod = config_module.Path.chmod

    def fail_profile_chmod(path, mode):
        if path == profile_dir:
            raise OSError("simulated profile chmod failure")
        return original_chmod(path, mode)

    monkeypatch.setattr(config_module.Path, "chmod", fail_profile_chmod)

    with pytest.raises(ValueError, match="secure profile directory"):
        add_or_update_account(
            "work",
            profile_dir=str(profile_dir),
            path=config_path,
        )

    assert not config_path.exists()
    assert not (profile_dir / ".codex-usage-profile").exists()


def test_profile_directory_rollback_stops_at_first_unexpected_entry(
    tmp_path, monkeypatch
):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    marker = profile_dir / ".codex-usage-profile"
    unexpected = profile_dir / "unexpected"
    original_iterdir = config_module.Path.iterdir

    def bounded_iterdir(path):
        if path != profile_dir:
            return original_iterdir(path)

        def entries():
            yield marker
            yield unexpected
            raise AssertionError("rollback directory scan was unbounded")

        return entries()

    monkeypatch.setattr(config_module.Path, "iterdir", bounded_iterdir)

    with pytest.raises(ValueError, match="unexpected data"):
        config_module._remove_created_profile_dir(profile_dir)


def test_failed_account_add_does_not_create_profile_before_config_validation(tmp_path):
    config_path = tmp_path / "config.toml"
    first_profile = tmp_path / "first-profile"
    second_profile = tmp_path / "second-profile"
    auth_path = tmp_path / "auth.json"
    add_or_update_account(
        "one",
        profile_dir=str(first_profile),
        auth_json_path=str(auth_path),
        path=config_path,
    )

    with pytest.raises(ValueError, match="duplicate auth_json_path"):
        add_or_update_account(
            "two",
            profile_dir=str(second_profile),
            auth_json_path=str(auth_path),
            path=config_path,
        )

    assert not second_profile.exists()
    assert load_config(config_path).accounts[0].id == "one"


def test_account_update_config_save_failure_removes_only_new_profile(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    old_profile = tmp_path / "old-profile"
    new_profile = tmp_path / "new-profile"
    add_or_update_account(
        "same",
        profile_dir=str(old_profile),
        path=config_path,
    )

    def fail_save(*_args, **_kwargs):
        raise OSError("config save failed")

    monkeypatch.setattr(config_module, "_save_config_unlocked", fail_save)

    with pytest.raises(OSError, match="config save failed"):
        add_or_update_account(
            "same",
            profile_dir=str(new_profile),
            path=config_path,
        )

    assert not new_profile.exists()
    assert old_profile.is_dir()
    assert load_config(config_path).accounts[0].profile_dir == str(old_profile)


def test_account_update_state_cleanup_failure_removes_only_new_profile(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.toml"
    old_profile = tmp_path / "old-profile"
    new_profile = tmp_path / "new-profile"
    add_or_update_account(
        "same",
        profile_dir=str(old_profile),
        path=config_path,
    )

    def fail_cleanup(*_args, **_kwargs):
        raise OSError("state cleanup failed")

    monkeypatch.setattr("codex_usage.state.remove_account_state", fail_cleanup)

    with pytest.raises(OSError, match="state cleanup failed"):
        add_or_update_account(
            "same",
            profile_dir=str(new_profile),
            path=config_path,
        )

    assert not new_profile.exists()
    assert old_profile.is_dir()
    assert load_config(config_path).accounts[0].profile_dir == str(old_profile)


def test_profile_rollback_removes_new_empty_ancestors(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    profile_dir = tmp_path / "new-root" / "nested" / "profile"

    def fail_save(*_args, **_kwargs):
        raise OSError("config save failed")

    monkeypatch.setattr(config_module, "_save_config_unlocked", fail_save)

    with pytest.raises(OSError, match="config save failed"):
        add_or_update_account(
            "nested",
            profile_dir=str(profile_dir),
            path=config_path,
        )

    assert not profile_dir.exists()
    assert not profile_dir.parent.exists()
    assert not profile_dir.parent.parent.exists()


def test_profile_setup_failure_removes_partially_created_ancestors(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    profile_dir = tmp_path / "partial-root" / "nested" / "profile"
    original_ensure = config_module.ensure_private_directory

    def fail_after_profile_setup(path, *, label, **kwargs):
        result = original_ensure(path, label=label, **kwargs)
        if label == "profile dir":
            raise OSError("profile setup failed after creation")
        return result

    monkeypatch.setattr(config_module, "ensure_private_directory", fail_after_profile_setup)

    with pytest.raises(OSError, match="profile setup failed after creation"):
        add_or_update_account(
            "partial",
            profile_dir=str(profile_dir),
            path=config_path,
        )

    assert not profile_dir.exists()
    assert not profile_dir.parent.exists()
    assert not profile_dir.parent.parent.exists()


def test_profile_rollback_does_not_delete_new_content(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    old_profile = tmp_path / "old-profile"
    new_profile = tmp_path / "new-profile"
    add_or_update_account(
        "same",
        profile_dir=str(old_profile),
        path=config_path,
    )

    def fail_save(*_args, **_kwargs):
        (new_profile / "concurrent-data").write_text("keep", encoding="utf-8")
        raise OSError("config save failed")

    monkeypatch.setattr(config_module, "_save_config_unlocked", fail_save)

    with pytest.raises(ExceptionGroup) as exc:
        add_or_update_account(
            "same",
            profile_dir=str(new_profile),
            path=config_path,
        )

    assert str(exc.value.exceptions[0]) == "config save failed"
    assert (new_profile / "concurrent-data").read_text(encoding="utf-8") == "keep"


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
    legacy.chmod(0o600)
    assert load_config(legacy).accounts[0].backend == "direct"


def test_config_round_trip_reactivation_browser(tmp_path):
    config_path = tmp_path / "config.toml"
    _, account = add_or_update_account(
        "privat",
        reactivation_browser="vivaldi",
        path=config_path,
    )

    loaded = load_config(config_path)

    assert account.reactivation_browser == "vivaldi"
    assert loaded.accounts[0].reactivation_browser == "vivaldi"
    assert 'reactivation_browser = "vivaldi"' in config_path.read_text(encoding="utf-8")


def test_config_defaults_reactivation_browser_for_legacy_account(tmp_path):
    config_path = tmp_path / "legacy.toml"
    config_path.write_text('[[accounts]]\nid = "legacy"\n', encoding="utf-8")
    config_path.chmod(0o600)

    assert load_config(config_path).accounts[0].reactivation_browser == "auto"


def test_config_rejects_unknown_reactivation_browser(tmp_path):
    with pytest.raises(ValueError, match="reactivation browser must be one of"):
        add_or_update_account(
            "privat",
            reactivation_browser="netscape",
            path=tmp_path / "config.toml",
        )


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


def test_reconfiguring_account_clears_old_usage_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    auth_a = tmp_path / "auth-a.json"
    auth_b = tmp_path / "auth-b.json"
    add_or_update_account("same", auth_json_path=str(auth_a), path=config_path)
    usage = AccountUsage(
        account_id="same",
        label="Same",
        captured_at=datetime.now(UTC),
        five_hour=LimitWindow(name="5h", remaining=12),
        weekly=LimitWindow(name="weekly", remaining=34),
        backend_used="direct",
        backend_user_id="user-a",
        backend_account_id="account-a",
    )
    save_current_usage(usage)
    save_usage_snapshot(usage)

    add_or_update_account("same", auth_json_path=str(auth_b), path=config_path)

    assert load_current_usage("same") is None
    assert (tmp_path / "data" / "codex-usage" / "snapshots" / "same.json").exists() is False
    assert load_latest_usages(load_config(config_path)) == []


def test_reconfiguring_account_keeps_state_when_config_save_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    add_or_update_account("same", path=config_path)
    usage = AccountUsage(
        account_id="same",
        label="Same",
        captured_at=datetime.now(UTC),
        five_hour=LimitWindow(name="5h", remaining=12),
        weekly=LimitWindow(name="weekly", remaining=34),
    )
    save_current_usage(usage)

    def fail_save(*_args, **_kwargs):
        raise OSError("config save failed")

    monkeypatch.setattr(config_module, "_save_config_unlocked", fail_save)

    with pytest.raises(OSError, match="config save failed"):
        add_or_update_account("same", label="Changed", path=config_path)

    assert load_current_usage("same") is not None


def test_reconfiguring_account_rolls_back_config_when_state_cleanup_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    add_or_update_account("same", label="Old", path=config_path)

    def fail_cleanup(*_args, **_kwargs):
        raise OSError("state cleanup failed")

    monkeypatch.setattr("codex_usage.state.remove_account_state", fail_cleanup)

    with pytest.raises(OSError, match="state cleanup failed"):
        add_or_update_account("same", label="New", path=config_path)

    assert load_config(config_path).accounts[0].label == "Old"


def test_account_update_holds_all_accounts_lock_during_state_cleanup(
    tmp_path,
):
    config_path = tmp_path / "config.toml"

    def assert_transaction_lock(_config):
        with pytest.raises(AccountLockError):
            with account_lock("__all_accounts__", timeout_seconds=0):
                pass

    add_or_update_account(
        "same",
        label="New",
        path=config_path,
        before_state_cleanup=assert_transaction_lock,
    )


def test_reconfiguring_account_aggregates_multiple_rollback_failures(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    add_or_update_account("same", label="Old", path=config_path)

    def fail_state_cleanup(*_args, **_kwargs):
        raise OSError("state cleanup failed")

    monkeypatch.setattr("codex_usage.state.remove_account_state", fail_state_cleanup)
    original_save = config_module._save_config_unlocked
    save_attempts = 0

    def fail_config_rollback(config, path):
        nonlocal save_attempts
        save_attempts += 1
        if save_attempts == 1:
            return original_save(config, path)
        raise OSError("config rollback failed")

    monkeypatch.setattr(config_module, "_save_config_unlocked", fail_config_rollback)

    def fail_service_rollback(*_args, **_kwargs):
        raise OSError("service rollback failed")

    with pytest.raises(ExceptionGroup) as exc:
        add_or_update_account(
            "same",
            label="New",
            path=config_path,
            before_state_cleanup=lambda _config: None,
            rollback_callback=fail_service_rollback,
        )

    assert "config rollback" in str(exc.value)
    assert "service rollback" in str(exc.value)
    assert [str(error) for error in exc.value.exceptions] == [
        "state cleanup failed",
        "config rollback failed",
        "service rollback failed",
    ]


def test_reconfiguring_account_rolls_back_service_if_callback_fails(
    tmp_path,
):
    config_path = tmp_path / "config.toml"
    add_or_update_account("same", label="Old", path=config_path)
    events = []

    def fail_after_external_change(_config):
        events.append("service changed")
        raise OSError("service update failed after write")

    def restore_external_change(_config):
        events.append("service restored")

    with pytest.raises(OSError, match="service update failed after write"):
        add_or_update_account(
            "same",
            label="New",
            path=config_path,
            before_state_cleanup=fail_after_external_change,
            rollback_callback=restore_external_change,
        )

    assert events == ["service changed", "service restored"]
    assert load_config(config_path).accounts[0].label == "Old"


def test_unchanged_account_update_keeps_usage_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    add_or_update_account("same", path=config_path)
    usage = AccountUsage(
        account_id="same",
        label="same",
        captured_at=datetime.now(UTC),
        five_hour=LimitWindow(name="5h", remaining=12),
        weekly=LimitWindow(name="weekly", remaining=34),
    )
    save_current_usage(usage)

    add_or_update_account("same", path=config_path)

    assert load_current_usage("same") is not None


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
    config_path.chmod(0o600)

    with pytest.raises(ValueError):
        load_config(config_path)


@pytest.mark.parametrize(
    "field",
    ("id", "label", "profile_dir", "browser", "auth_json_path", "backend"),
)
def test_load_config_rejects_non_string_account_fields(tmp_path, field):
    config_path = tmp_path / "config.toml"
    account_id = "" if field == "id" else 'id = "privat"\n'
    config_path.write_text(
        f"""
[[accounts]]
{account_id}{field} = 123
""",
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    with pytest.raises(ValueError):
        load_config(config_path)


def test_load_config_rejects_non_string_analytics_url(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("analytics_url = 123\n", encoding="utf-8")
    config_path.chmod(0o600)

    with pytest.raises(ValueError, match="analytics_url"):
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
    config_path.chmod(0o600)

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
    config_path.chmod(0o600)

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
    config_path.chmod(0o600)

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
    config_path.chmod(0o600)

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


@pytest.mark.parametrize("field", ("profile_dir", "auth_json_path"))
def test_save_config_rejects_relative_account_paths(tmp_path, field):
    values = {
        "id": "one",
        "label": "One",
        "profile_dir": str(tmp_path / "profiles" / "one"),
    }
    values[field] = "relative-path"
    account = Account(**values)

    with pytest.raises(ValueError, match=f"{field} must be an absolute path"):
        save_config(AppConfig(accounts=(account,)), tmp_path / "config.toml")


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


def test_restore_account_rejects_different_parallel_readd(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    add_or_update_account(
        "same",
        label="Old",
        profile_dir=str(tmp_path / "old-profile"),
        path=config_path,
    )
    _, removed = remove_account("same", path=config_path)
    add_or_update_account(
        "same",
        label="New",
        profile_dir=str(tmp_path / "new-profile"),
        path=config_path,
    )

    with pytest.raises(ValueError, match="different settings"):
        restore_account(removed, path=config_path, index=0)

    assert load_config(config_path).accounts[0].label == "New"


def test_remove_account_rejects_different_parallel_update(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    add_or_update_account("same", label="Old", path=config_path)
    _, expected = remove_account("same", path=config_path)
    add_or_update_account("same", label="New", path=config_path)

    with pytest.raises(ValueError, match="changed before removal"):
        remove_account("same", path=config_path, expected=expected)

    assert load_config(config_path).accounts[0].label == "New"


def test_readding_removed_account_clears_previous_usage_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    add_or_update_account("same", label="Old", path=config_path)
    save_current_usage(
        AccountUsage(
            account_id="same",
            label="Old",
            captured_at=datetime.now(UTC),
            five_hour=LimitWindow(name="5h", remaining=12),
            weekly=LimitWindow(name="weekly", remaining=34),
        )
    )

    remove_account("same", path=config_path)
    add_or_update_account("same", label="New", path=config_path)

    assert load_current_usage("same") is None
