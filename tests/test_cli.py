from __future__ import annotations

import pytest

from codex_usage.cli import main


def test_root_help_lists_all_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0

    output = capsys.readouterr().out
    assert "codex-usage account add ACCOUNT_ID" in output
    assert "--browser BROWSER" in output
    assert "codex-usage account list" not in output
    assert "codex-usage account overview" in output
    assert "codex-usage account delete ACCOUNT" in output
    assert "codex-usage login ACCOUNT" in output
    assert "codex-usage once" in output
    assert "codex-usage watch" in output
    assert "codex-usage probe ACCOUNT" in output
    assert "codex-usage paths" in output


def test_account_add_prints_login_id_hint(tmp_path, capsys):
    config_path = tmp_path / "config.toml"

    assert main(["--config", str(config_path), "account", "add", "privat", "--label", "BW"]) == 0

    output = capsys.readouterr().out
    assert "Account gespeichert: privat (BW)" in output
    assert "Browser: firefox" in output
    assert "Login: codex-usage login privat" in output


def test_account_add_accepts_browser(tmp_path, capsys):
    config_path = tmp_path / "config.toml"

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "add",
                "privat",
                "--browser",
                "chromium",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Browser: chromium" in output


def test_account_list_is_removed(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["account", "list"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_login_accepts_unique_label(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    called = {}

    def fake_login(account, config):
        called["account_id"] = account.id
        called["label"] = account.label
        called["url"] = config.analytics_url

    monkeypatch.setattr("codex_usage.cli.login_account", fake_login)

    assert (
        main(["--config", str(config_path), "account", "add", "privat", "--label", "BW_Privat"])
        == 0
    )
    assert main(["--config", str(config_path), "login", "BW_Privat"]) == 0

    assert called == {
        "account_id": "privat",
        "label": "BW_Privat",
        "url": "https://chatgpt.com/codex/cloud/settings/analytics",
    }


def test_account_overview_shows_config_and_accounts(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    profile_dir = tmp_path / "profile"

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "add",
                "privat",
                "--label",
                "BW_Privat",
                "--profile-dir",
                str(profile_dir),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["--config", str(config_path), "account", "overview"]) == 0

    output = capsys.readouterr().out
    assert "Account-Uebersicht" in output
    assert "Accounts: 1" in output
    assert "privat" in output
    assert "BW_Privat" in output
    assert "firefox" in output
    assert "vorhanden" in output


def test_account_delete_removes_config_but_keeps_profile_by_default(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    profile_dir = tmp_path / "profile"

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "add",
                "privat",
                "--label",
                "BW_Privat",
                "--profile-dir",
                str(profile_dir),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["--config", str(config_path), "account", "delete", "BW_Privat"]) == 0

    output = capsys.readouterr().out
    assert "Account geloescht: privat (BW_Privat)" in output
    assert "Profil behalten:" in output
    assert profile_dir.is_dir()

    assert main(["--config", str(config_path), "account", "overview"]) == 0
    assert "Accounts: 0" in capsys.readouterr().out


def test_account_delete_can_delete_marked_profile(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    profile_dir = tmp_path / "profile"

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "add",
                "privat",
                "--profile-dir",
                str(profile_dir),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(["--config", str(config_path), "account", "delete", "privat", "--delete-profile"])
        == 0
    )

    output = capsys.readouterr().out
    assert "Profil: geloescht" in output
    assert not profile_dir.exists()
