from __future__ import annotations

import json
from datetime import datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

import pytest

from codex_usage.bridge import MAX_INGEST_BYTES, bridge_token_for_account, load_latest_usages
from codex_usage.cli import main
from codex_usage.config import AppConfig, load_config
from codex_usage.models import Account, AccountStatus, AccountUsage, LimitWindow
from codex_usage.state import save_current_usage, save_usage_snapshot


def test_sync_managed_service_does_not_rebind_another_config(tmp_path, monkeypatch):
    calls = []
    configured = tmp_path / "configured.toml"
    requested = tmp_path / "requested.toml"
    monkeypatch.setattr("codex_usage.cli.service_status", lambda: {"installed": True})
    monkeypatch.setattr(
        "codex_usage.cli.managed_service_config_path",
        lambda: configured.absolute(),
    )
    monkeypatch.setattr(
        "codex_usage.cli.service_install",
        lambda *args: calls.append(args),
    )

    from codex_usage.cli import _sync_managed_service

    _sync_managed_service(object(), requested)

    assert calls == []


def test_root_help_lists_all_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0

    output = capsys.readouterr().out
    assert "Komplette Command-Line-Usage:" in output
    assert "Globale Optionen:" in output
    assert "Accounts:" in output
    assert "Login und Reaktivierung:" in output
    assert "Abruf und Ueberwachung:" in output
    assert "Analyse und Diagnose:" in output
    assert "Gespeicherte Werte und manuelle Aufnahme:" in output
    assert "Browser-Bridge:" in output
    assert "Beispiele:" in output
    assert "codex-usage account add ACCOUNT_ID" in output
    assert "--browser BROWSER" in output
    assert "codex-usage account list" not in output
    assert "codex-usage account overview" in output
    assert "--config-only" in output
    assert "codex-usage account backend ACCOUNT direct|app-server" in output
    assert "codex-usage account delete ACCOUNT" in output
    assert "codex-usage login ACCOUNT" in output
    assert "codex-usage once" in output
    assert "codex-usage watch" in output
    assert "codex-usage watchdog" in output
    assert "codex-usage health" in output
    assert "--direct" in output
    assert "--backend direct|app-server" in output
    assert "codex-usage probe ACCOUNT" in output
    assert "codex-usage diagnose ACCOUNT" in output
    assert "--auth-json PATH" in output
    assert "codex-usage ingest ACCOUNT" in output
    assert "codex-usage latest [--format table|json]" in output
    assert "codex-usage values [--account ACCOUNT]" in output
    assert "codex-usage bridge-snippet ACCOUNT" in output
    assert "codex-usage bridge-extension ACCOUNT" in output
    assert "codex-usage bridge-server" in output
    assert "--allow-remote" in output
    assert "codex-usage paths" in output
    assert (
        "Direct- und App-Server-Abrufe mit mehreren Accounts brauchen pro Account "
        "auth_json_path"
    ) in output
    assert "Ohne Override nutzt jeder Account seinen gespeicherten Abrufweg" in output
    assert "App-Server-Kontostatusabfragen starten keine Modellanfrage" in output
    assert "codex-usage values" in output
    assert "codex-usage watch" in output
    assert "codex-usage service enable" in output
    assert "codex-usage watchdog" in output


def test_health_command_records_reads_and_clears(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert main(
        [
            "health",
            "--format",
            "json",
            "--record-component",
            "watch",
            "--record-event",
            "cycle_error",
            "--account",
            "work",
            "--error-class",
            "ValueError",
            "--duration-ms",
            "12",
        ]
    ) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["event_count"] == 1
    assert recorded["events"][0]["account"] == "work"

    assert main(["health", "--clear", "--format", "json"]) == 0
    cleared = json.loads(capsys.readouterr().out)
    assert cleared["event_count"] == 0


def test_root_version_reports_package_version(capsys):
    for argv in (["--version"], ["--config", "/tmp/unused.toml", "--version"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)

        assert exc.value.code == 0
    assert capsys.readouterr().out == "codex-usage 0.6.259\ncodex-usage 0.6.259\n"


def test_root_without_subcommand_defaults_to_once(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    called = {}

    def fake_once(args):
        called["account_ids"] = args.account_ids
        called["format"] = args.format
        called["direct"] = args.direct
        return 0

    monkeypatch.setattr("codex_usage.cli._cmd_once", fake_once)

    assert main(["--config", str(config_path), "--format", "json"]) == 0

    assert called == {
        "account_ids": None,
        "format": "json",
        "direct": False,
    }


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

    def fake_fetch_all(
        config,
        accounts,
        *,
        headed,
        direct,
        backend_override,
        auth_json_path,
        save_snapshots,
    ):
        called["accounts"] = [account.id for account in accounts]
        called["headed"] = headed
        called["direct"] = direct
        called["backend_override"] = backend_override
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
        "backend_override": None,
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
        backend_override,
        auth_json_path,
        interval_seconds,
    ):
        called["accounts"] = [account.id for account in accounts]
        called["output"] = output
        called["headed"] = headed
        called["direct"] = direct
        called["backend_override"] = backend_override
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
        "backend_override": None,
        "auth_json_path": auth_path,
        "interval_seconds": 300,
    }


def test_watch_without_account_selects_all_accounts_and_defers_mode_to_scheduler(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.toml"
    called = {}

    def fake_watch(
        config,
        accounts,
        *,
        output,
        headed,
        direct,
        backend_override,
        auth_json_path,
        interval_seconds,
    ):
        called["accounts"] = [account.id for account in accounts]
        called["output"] = output
        called["headed"] = headed
        called["direct"] = direct
        called["backend_override"] = backend_override
        called["auth_json_path"] = auth_json_path
        called["interval_seconds"] = interval_seconds

    monkeypatch.setattr("codex_usage.cli.watch", fake_watch)

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

    assert main(["--config", str(config_path), "watch"]) == 0

    assert called == {
        "accounts": ["privat", "work"],
        "output": "table",
        "headed": False,
        "direct": False,
        "backend_override": None,
        "auth_json_path": None,
        "interval_seconds": None,
    }


def test_watchdog_routes_through_watchdog_scheduler(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    called = {}

    def fake_watchdog(
        config,
        accounts,
        *,
        output,
        headed,
        direct,
        backend_override,
        auth_json_path,
    ):
        called["accounts"] = [account.id for account in accounts]
        called["output"] = output
        called["headed"] = headed
        called["direct"] = direct
        called["backend_override"] = backend_override
        called["auth_json_path"] = auth_json_path
        return []

    monkeypatch.setattr("codex_usage.cli.watchdog", fake_watchdog)

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
    capsys.readouterr()

    assert main(["--config", str(config_path), "watchdog", "--format", "json"]) == 0

    assert called == {
        "accounts": ["privat"],
        "output": "json",
        "headed": False,
        "direct": False,
        "backend_override": None,
        "auth_json_path": None,
    }


def test_bridge_server_rejects_remote_host_without_explicit_opt_in(
    tmp_path,
    monkeypatch,
    capsys,
):
    config_path = tmp_path / "config.toml"
    called = {}

    def fake_run_bridge_server(config, *, host, port, config_path):
        called["host"] = host
        called["port"] = port

    monkeypatch.setattr("codex_usage.cli.run_bridge_server", fake_run_bridge_server)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "bridge-server",
                "--host",
                "0.0.0.0",
            ]
        )
        == 1
    )

    assert called == {}
    assert "--allow-remote" in capsys.readouterr().err


def test_bridge_server_allows_remote_host_with_explicit_opt_in(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config.toml"
    called = {}

    def fake_run_bridge_server(config, *, host, port, config_path):
        called["host"] = host
        called["port"] = port

    monkeypatch.setattr("codex_usage.cli.run_bridge_server", fake_run_bridge_server)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "bridge-server",
                "--host",
                "0.0.0.0",
                "--allow-remote",
            ]
        )
        == 0
    )

    assert called == {"host": "0.0.0.0", "port": 8765}


def test_bridge_server_allows_loopback_host_without_opt_in(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config.toml"
    called = {}

    def fake_run_bridge_server(config, *, host, port, config_path):
        called["host"] = host
        called["port"] = port

    monkeypatch.setattr("codex_usage.cli.run_bridge_server", fake_run_bridge_server)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "bridge-server",
                "--host",
                "::1",
                "--port",
                "9999",
            ]
        )
        == 0
    )

    assert called == {"host": "::1", "port": 9999}


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


@pytest.mark.parametrize(
    "extra_args",
    (
        ("--direct",),
        ("--auth-json", "auth.json"),
        ("--backend", "app-server"),
    ),
)
def test_headed_rejects_non_browser_fetch_overrides(tmp_path, capsys, extra_args):
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--config",
                str(config_path),
                "once",
                "--headed",
                *extra_args,
            ]
        )
        == 1
    )

    assert "cannot be combined" in capsys.readouterr().err


def test_account_overview_shows_config_and_accounts(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    profile_dir = tmp_path / "profile"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

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
    assert "5h Wert" in output
    assert "Woche Wert" in output


def test_account_overview_config_only_skips_live_fetch(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    assert main(
        [
            "--config",
            str(config_path),
            "account",
            "add",
            "privat",
            "--label",
            "BW_Privat",
        ]
    ) == 0
    capsys.readouterr()

    def fail_live_fetch(*_args, **_kwargs):
        raise AssertionError("config-only overview must not fetch usage")

    monkeypatch.setattr("codex_usage.cli._load_overview_usages", fail_live_fetch)
    assert main(
        [
            "--config",
            str(config_path),
            "account",
            "overview",
            "--format",
            "json",
            "--config-only",
        ]
    ) == 0

    account = json.loads(capsys.readouterr().out)["accounts"][0]
    assert account["id"] == "privat"
    assert account["backend"] == "direct"
    assert account["usage"] is None


def test_account_overview_shows_live_direct_values(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    auth_path = tmp_path / "auth.json"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    def fake_fetch_all(config, accounts, **kwargs):
        account = next(iter(accounts))
        assert account.id == "privat"
        return [
            AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=datetime(2026, 6, 8, 3, 30, tzinfo=ZoneInfo("Europe/Berlin")),
                auth_last_refresh=datetime(
                    2026, 7, 9, 23, 17, tzinfo=ZoneInfo("Europe/Berlin")
                ),
                auth_access_expires_at=datetime(
                    2026, 7, 19, 23, 17, tzinfo=ZoneInfo("Europe/Berlin")
                ),
                five_hour=LimitWindow(
                    name="5h",
                    used=3,
                    limit=100,
                    remaining=97,
                    percent=97,
                    reset_at=datetime(2026, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin")),
                ),
                weekly=LimitWindow(
                    name="weekly",
                    used=45,
                    limit=100,
                    remaining=55,
                    percent=55,
                    reset_at=datetime(2026, 6, 10, 5, 5, tzinfo=ZoneInfo("Europe/Berlin")),
                ),
            )
        ]

    monkeypatch.setattr("codex_usage.cli.fetch_all", fake_fetch_all)

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
                "--auth-json",
                str(auth_path),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["--config", str(config_path), "account", "overview"]) == 0

    output = capsys.readouterr().out
    assert "97% verbleibend" in output
    assert "55% verbleibend" in output
    assert "08.06.2026 06:50" in output
    assert "10.06.2026 05:05" in output
    assert "bis 19.07.2026 23:17" in output
    assert "ok" in output


def test_account_overview_json_shows_live_values(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    def fake_fetch_all(config, accounts, **kwargs):
        account = next(iter(accounts))
        captured_at = datetime(2026, 6, 8, 3, 30, tzinfo=ZoneInfo("Europe/Berlin"))
        return [
            AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=captured_at,
                five_hour=LimitWindow(
                    name="5h",
                    used=3,
                    limit=100,
                    remaining=97,
                    percent=97,
                    reset_at=datetime(
                        2026, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin")
                    ),
                ),
                weekly=LimitWindow(
                    name="weekly",
                    used=45,
                    limit=100,
                    remaining=55,
                    percent=55,
                    reset_at=datetime(
                        2026, 6, 10, 5, 5, tzinfo=ZoneInfo("Europe/Berlin")
                    ),
                ),
            )
        ]

    monkeypatch.setattr("codex_usage.cli.fetch_all", fake_fetch_all)
    assert main(
        [
            "--config",
            str(config_path),
            "account",
            "add",
            "privat",
            "--label",
            "BW_Privat",
        ]
    ) == 0
    capsys.readouterr()

    assert main(
        ["--config", str(config_path), "account", "overview", "--format", "json"]
    ) == 0

    account = json.loads(capsys.readouterr().out)["accounts"][0]
    assert account["usage"]["status"] == "ok"
    assert account["usage"]["five_hour"]["remaining"] == 97
    assert account["usage"]["weekly"]["remaining"] == 55
    assert account["usage"]["five_hour"]["reset_at"] == "2026-06-08T06:50:00+02:00"


def test_values_shows_compact_live_values_for_all_accounts(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    def fake_fetch_all(config, accounts, **kwargs):
        return [
            AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=datetime(2026, 6, 8, 3, 30, tzinfo=ZoneInfo("Europe/Berlin")),
                five_hour=LimitWindow(
                    name="5h",
                    used=3,
                    limit=100,
                    remaining=97,
                    percent=97,
                    reset_at=datetime(2026, 6, 8, 6, 50, tzinfo=ZoneInfo("Europe/Berlin")),
                ),
                weekly=LimitWindow(
                    name="weekly",
                    used=45,
                    limit=100,
                    remaining=55,
                    percent=55,
                    reset_at=datetime(2026, 6, 10, 5, 5, tzinfo=ZoneInfo("Europe/Berlin")),
                ),
            )
            for account in accounts
        ]

    monkeypatch.setattr("codex_usage.cli.fetch_all", fake_fetch_all)

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
                "--label",
                "BW_Work",
                "--auth-json",
                str(tmp_path / "work-auth.json"),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["--config", str(config_path), "values"]) == 0

    output = capsys.readouterr().out
    assert "Account" in output
    assert "BW_Privat" in output
    assert "BW_Work" in output
    assert output.count("97% verbleibend") == 2
    assert "Stand:" not in output
    assert "Profil" not in output


def test_ingest_and_latest_show_manual_snapshot(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reset = (datetime.now().astimezone() + timedelta(days=1)).strftime(
        "%d.%m.%Y %H:%M"
    )
    body = f"""
    5 Stunden Nutzungsgrenze 42 / 100 Zurücksetzungen {reset}
    Wöchentliches Nutzungslimit 310 / 1000 Zurücksetzungen {reset}
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
    assert "58% verbleibend" in output
    assert "310 / 1000" in output
    assert "69% verbleibend" in output

    assert main(["--config", str(config_path), "latest"]) == 0
    latest = capsys.readouterr().out
    assert "58% verbleibend" in latest
    assert "310 / 1000" in latest
    assert "69% verbleibend" in latest


def test_latest_marks_old_current_values_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
    )
    config = AppConfig(accounts=(account,), interval_seconds=300)
    save_current_usage(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime.now().astimezone() - timedelta(minutes=7),
            five_hour=LimitWindow(name="5h", remaining=97),
            status=AccountStatus.OK,
        )
    )

    usages = load_latest_usages(config)

    assert len(usages) == 1
    assert usages[0].stale is True


def test_latest_does_not_show_cached_window_after_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
    )
    config = AppConfig(accounts=(account,), interval_seconds=300)
    captured = datetime.now().astimezone()
    save_current_usage(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=captured,
            status=AccountStatus.OK,
            backend_used="direct",
            five_hour=LimitWindow(
                name="5h",
                remaining=38,
                reset_at=captured - timedelta(seconds=1),
            ),
            weekly=LimitWindow(
                name="weekly",
                remaining=72,
                reset_at=captured + timedelta(hours=1),
            ),
        )
    )

    usages = load_latest_usages(config)

    assert len(usages) == 1
    assert usages[0].five_hour is None
    assert usages[0].weekly is not None
    assert usages[0].weekly.remaining == 72
    assert usages[0].status == AccountStatus.PARTIAL
    assert usages[0].stale is True


def test_latest_rejects_cached_authenticated_backend_override(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    account = Account(
        id="privat",
        label="Privat",
        profile_dir=str(tmp_path / "profile"),
        backend="direct",
    )
    config = AppConfig(accounts=(account,), interval_seconds=300)
    captured = datetime.now().astimezone()
    direct = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured - timedelta(minutes=1),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        backend_user_id="user",
        backend_account_id="account",
        five_hour=LimitWindow(name="5h", remaining=77),
    )
    override = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=captured,
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="app-server",
        backend_user_id="user",
        backend_account_id="account",
        five_hour=LimitWindow(name="5h", remaining=11),
    )
    save_usage_snapshot(direct)
    save_current_usage(override)

    usages = load_latest_usages(config)

    assert len(usages) == 1
    assert usages[0].backend_used == "direct"
    assert usages[0].five_hour is not None
    assert usages[0].five_hour.remaining == 77


def test_ingest_file_rejects_oversized_payload_before_saving(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    payload_path = tmp_path / "too-large.txt"
    payload_path.write_text("x" * (MAX_INGEST_BYTES + 1), encoding="utf-8")

    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--config",
                str(config_path),
                "ingest",
                "privat",
                "--file",
                str(payload_path),
            ]
        )
        == 1
    )

    assert "ingest payload too large" in capsys.readouterr().err
    assert not (tmp_path / "data" / "codex-usage" / "snapshots" / "privat.json").exists()


def test_ingest_stdin_rejects_oversized_payload_before_saving(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()

    import sys

    old_stdin = sys.stdin
    try:
        sys.stdin = StringIO("x" * (MAX_INGEST_BYTES + 1))
        assert main(["--config", str(config_path), "ingest", "privat", "--stdin"]) == 1
    finally:
        sys.stdin = old_stdin

    assert "ingest payload too large" in capsys.readouterr().err
    assert not (tmp_path / "data" / "codex-usage" / "snapshots" / "privat.json").exists()


def test_bridge_snippet_command_normalizes_label_to_account_id(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
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
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "--config",
                str(config_path),
                "bridge-snippet",
                "BW_Privat",
                "--port",
                "8765",
                "--interval",
                "300",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert 'const account = "privat";' in output
    assert "BW_Privat" not in output
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


def test_account_delete_revokes_bridge_token_before_same_id_is_readded(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"

    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    first = bridge_token_for_account("privat")
    old_usage = AccountUsage(
        account_id="privat",
        label="Old",
        captured_at=datetime.now().astimezone(),
        five_hour=LimitWindow(name="5h", remaining=12),
        weekly=LimitWindow(name="weekly", remaining=34),
    )
    save_current_usage(old_usage)
    save_usage_snapshot(old_usage)
    debug_dir = tmp_path / "data" / "codex-usage" / "debug"
    debug_dir.mkdir(parents=True, mode=0o700)
    (debug_dir / "privat-last-ingest.json").write_text("{}", encoding="utf-8")

    assert main(["--config", str(config_path), "account", "delete", "privat"]) == 0
    capsys.readouterr()

    token_path = tmp_path / "data" / "codex-usage" / "bridge-tokens" / "privat.token"
    assert not token_path.exists()
    assert not (tmp_path / "data" / "codex-usage" / "current" / "privat.json").exists()
    assert not (tmp_path / "data" / "codex-usage" / "snapshots" / "privat.json").exists()
    assert not (debug_dir / "privat-last-ingest.json").exists()

    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    second = bridge_token_for_account("privat")
    assert second != first
    assert load_latest_usages(load_config(config_path)) == []


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


def test_account_delete_rejects_symlink_profile_and_keeps_config(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    target = tmp_path / "target-profile"
    target.mkdir()
    profile_link = tmp_path / "profile-link"
    profile_link.symlink_to(target, target_is_directory=True)
    config_path.write_text(
        f"""
[[accounts]]
id = "privat"
label = "BW_Privat"
profile_dir = "{profile_link}"
browser = "firefox"
""",
        encoding="utf-8",
    )

    assert (
        main(["--config", str(config_path), "account", "delete", "privat", "--delete-profile"])
        == 1
    )

    assert profile_link.is_symlink()
    assert target.is_dir()
    assert "privat" in config_path.read_text(encoding="utf-8")
    assert "symlink" in capsys.readouterr().err


def test_account_backend_updates_config_and_json_overview(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "add",
                "privat",
                "--label",
                "Privat",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "backend",
                "privat",
                "app-server",
                "--format",
                "json",
            ]
        )
        == 0
    )
    changed = json.loads(capsys.readouterr().out)
    assert changed["backend"] == "app-server"

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "overview",
                "--format",
                "json",
            ]
    )
    == 0
    )
    overview = json.loads(capsys.readouterr().out)
    account = overview["accounts"][0]
    assert account["id"] == "privat"
    assert account["label"] == "Privat"
    assert account["browser"] == "firefox"
    assert account["backend"] == "app-server"
    assert account["backend_used"] == "app-server"
    assert account["fallback_reason"] is None
    assert account["usage"]["status"] == "login_required"
    assert account["usage"]["five_hour"] is None


def test_backend_override_rejects_conflicting_direct_flag(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--config",
                str(config_path),
                "once",
                "--direct",
                "--backend",
                "app-server",
            ]
        )
        == 1
    )
    assert "cannot be combined" in capsys.readouterr().err
