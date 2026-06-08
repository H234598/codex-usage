from __future__ import annotations

import pytest

from codex_usage.cli import main


def test_root_help_lists_all_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0

    output = capsys.readouterr().out
    assert "Komplette Command-Line-Usage:" in output
    assert "Globale Optionen:" in output
    assert "Accounts:" in output
    assert "Browser-Modus:" in output
    assert "Direct-Modus ohne Browser:" in output
    assert "Analyse und Diagnose:" in output
    assert "Manuelle Aufnahme und Ausgabe:" in output
    assert "Browser-Bridge:" in output
    assert "Beispiele:" in output
    assert "codex-usage account add ACCOUNT_ID" in output
    assert "--browser BROWSER" in output
    assert "codex-usage account list" not in output
    assert "codex-usage account overview" in output
    assert "codex-usage account delete ACCOUNT" in output
    assert "codex-usage login ACCOUNT" in output
    assert "codex-usage once" in output
    assert "codex-usage watch" in output
    assert "--direct" in output
    assert "codex-usage probe ACCOUNT" in output
    assert "codex-usage diagnose ACCOUNT" in output
    assert "--auth-json PATH" in output
    assert "codex-usage ingest ACCOUNT" in output
    assert "codex-usage latest [--format table|json]" in output
    assert "codex-usage bridge-snippet ACCOUNT" in output
    assert "codex-usage bridge-extension ACCOUNT" in output
    assert "codex-usage bridge-server" in output
    assert "codex-usage paths" in output
    assert "Direct-Modus mit mehreren Accounts braucht pro Account auth_json_path" in output
    assert "codex-usage watch --direct" in output


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


def test_account_add_accepts_auth_json(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    auth_path = tmp_path / "auth.json"

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "add",
                "privat",
                "--auth-json",
                str(auth_path),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert f"Auth JSON: {auth_path}" in output
    assert f'auth_json_path = "{auth_path}"' in config_path.read_text(encoding="utf-8")


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


def test_diagnose_accepts_unique_label(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    called = {}

    def fake_diagnose(account, config, *, headed, screenshot_dir, auth_json_path):
        called["account_id"] = account.id
        called["label"] = account.label
        called["headed"] = headed
        called["screenshot_dir"] = str(screenshot_dir)
        called["auth_json_path"] = str(auth_json_path)
        return {"account": account.id, "detected": "cloudflare"}

    monkeypatch.setattr("codex_usage.cli.diagnose_account", fake_diagnose)

    assert (
        main(["--config", str(config_path), "account", "add", "privat", "--label", "BW_Privat"])
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "--config",
                str(config_path),
                "diagnose",
                "BW_Privat",
                "--headed",
                "--screenshot",
                "--save-dir",
                str(tmp_path / "shots"),
                "--auth-json",
                str(tmp_path / "auth.json"),
            ]
        )
        == 0
    )

    assert called == {
        "account_id": "privat",
        "label": "BW_Privat",
        "headed": True,
        "screenshot_dir": str(tmp_path / "shots"),
        "auth_json_path": str(tmp_path / "auth.json"),
    }
    assert '"detected": "cloudflare"' in capsys.readouterr().out


def test_once_direct_passes_auth_json_and_saves_snapshots(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    auth_path = tmp_path / "auth.json"
    called = {}

    def fake_fetch_all(config, accounts, *, headed, direct, auth_json_path, save_snapshots):
        called["accounts"] = [account.id for account in accounts]
        called["headed"] = headed
        called["direct"] = direct
        called["auth_json_path"] = auth_json_path
        called["save_snapshots"] = save_snapshots
        return []

    monkeypatch.setattr("codex_usage.cli.fetch_all", fake_fetch_all)

    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--config",
                str(config_path),
                "once",
                "--direct",
                "--auth-json",
                str(auth_path),
            ]
        )
        == 0
    )

    assert called == {
        "accounts": ["privat"],
        "headed": False,
        "direct": True,
        "auth_json_path": auth_path,
        "save_snapshots": True,
    }


def test_watch_direct_passes_auth_json(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    auth_path = tmp_path / "auth.json"
    called = {}

    def fake_watch(
        config,
        accounts,
        *,
        output,
        headed,
        direct,
        auth_json_path,
        interval_seconds,
    ):
        called["accounts"] = [account.id for account in accounts]
        called["output"] = output
        called["headed"] = headed
        called["direct"] = direct
        called["auth_json_path"] = auth_json_path
        called["interval_seconds"] = interval_seconds

    monkeypatch.setattr("codex_usage.cli.watch", fake_watch)

    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--config",
                str(config_path),
                "watch",
                "--direct",
                "--auth-json",
                str(auth_path),
                "--interval",
                "300",
            ]
        )
        == 0
    )

    assert called == {
        "accounts": ["privat"],
        "output": "table",
        "headed": False,
        "direct": True,
        "auth_json_path": auth_path,
        "interval_seconds": 300,
    }


def test_direct_rejects_multiple_accounts_without_per_account_auth_json(tmp_path, capsys):
    config_path = tmp_path / "config.toml"

    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    assert main(["--config", str(config_path), "account", "add", "work"]) == 0
    capsys.readouterr()

    assert main(["--config", str(config_path), "once", "--direct"]) == 1

    assert "requires per-account --auth-json" in capsys.readouterr().err


def test_direct_rejects_global_auth_json_for_multiple_accounts(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    auth_path = tmp_path / "auth.json"

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "add",
                "privat",
                "--auth-json",
                str(tmp_path / "privat-auth.json"),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "add",
                "work",
                "--auth-json",
                str(tmp_path / "work-auth.json"),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(["--config", str(config_path), "once", "--direct", "--auth-json", str(auth_path)])
        == 1
    )

    assert "can only override direct auth for one selected account" in capsys.readouterr().err


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


def test_ingest_and_latest_show_manual_snapshot(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    body = """
    5 Stunden Nutzungsgrenze 42 / 100 Zurücksetzungen 08.06.2026 04:26
    Wöchentliches Nutzungslimit 310 / 1000 Zurücksetzungen 14.06.2026 04:26
    """

    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()

    import sys
    from io import StringIO

    old_stdin = sys.stdin
    try:
        sys.stdin = StringIO(body)
        assert main(["--config", str(config_path), "ingest", "privat", "--stdin"]) == 0
    finally:
        sys.stdin = old_stdin

    output = capsys.readouterr().out
    assert "42 / 100" in output
    assert "310 / 1000" in output

    assert main(["--config", str(config_path), "latest"]) == 0
    latest = capsys.readouterr().out
    assert "42 / 100" in latest
    assert "310 / 1000" in latest


def test_bridge_snippet_command_prints_javascript(capsys):
    assert main(["bridge-snippet", "BW_Privat", "--port", "8765", "--interval", "300"]) == 0

    output = capsys.readouterr().out
    assert "BW_Privat" in output
    assert "http://127.0.0.1:8765/ingest" in output
    assert "setInterval" in output


def test_bridge_extension_command_writes_extension(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    output_dir = tmp_path / "extension"

    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--config",
                str(config_path),
                "bridge-extension",
                "privat",
                "--output",
                str(output_dir),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Extension erzeugt:" in output
    assert (output_dir / "manifest.json").is_file()
    assert (output_dir / "background.js").is_file()
    assert (output_dir / "content.js").is_file()


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
