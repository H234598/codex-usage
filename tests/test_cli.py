from __future__ import annotations

import base64
import json
import runpy
import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import codex_usage.cli as cli_module
import codex_usage.config as config_module
from codex_usage import __version__
from codex_usage.account_lock import AccountLockError, account_lock
from codex_usage.bridge import MAX_INGEST_BYTES, bridge_token_for_account, load_latest_usages
from codex_usage.browser import _profile_browser_dir, _profile_lock
from codex_usage.cli import (
    _all_usage_results_valid,
    _is_successful_usage,
    _load_overview_usages,
    _overview_usage_json,
    _policy_decision_exit_code,
    _select_accounts,
    _usage_for_policy,
    main,
)
from codex_usage.config import (
    AppConfig,
    MasterjetConnection,
    add_or_update_account,
    load_config,
    save_config,
)
from codex_usage.masterjet_cache import CachedControlSnapshot, ControlSnapshot
from codex_usage.masterjet_contracts import OpenAIControlAccount
from codex_usage.models import Account, AccountStatus, AccountUsage, LimitWindow, UsagePool
from codex_usage.spark_health import set_spark_health
from codex_usage.state import load_current_usage, save_current_usage, save_usage_snapshot


class _BrokenInt(int):
    def __ge__(self, _other):
        raise RuntimeError("synthetic CLI integer comparison marker")

    def __le__(self, _other):
        raise RuntimeError("synthetic CLI integer comparison marker")

    def __lt__(self, _other):
        raise RuntimeError("synthetic CLI integer comparison marker")


def test_cli_numeric_boundaries_reject_subclasses(tmp_path, monkeypatch):
    broken = _BrokenInt(300)

    with pytest.raises(ValueError, match="port"):
        cli_module._validate_port(broken)
    with pytest.raises(ValueError, match="interval"):
        cli_module._validate_min_interval(broken)
    with pytest.raises(ValueError, match="port"):
        cli_module._bridge_endpoint(None, broken)

    history_args = SimpleNamespace(
        dry_run=True,
        apply=False,
        days=broken,
        before=None,
        path=tmp_path / "history.sqlite3",
        format="json",
    )
    with pytest.raises(ValueError, match="days"):
        cli_module._cmd_history_prune(history_args)

    monkeypatch.setattr(cli_module, "load_config", lambda _path: object())
    monkeypatch.setattr(cli_module, "_select_accounts", lambda *_args: ())
    monkeypatch.setattr(cli_module, "_validate_fetch_mode_flags", lambda _args: None)
    watch_args = SimpleNamespace(
        config=None,
        account_ids=(),
        interval=broken,
        direct=False,
        auth_json=None,
        backend=None,
        headed=False,
        format="table",
    )
    with pytest.raises(ValueError, match="interval"):
        cli_module._cmd_watch(watch_args)


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
    assert "[--tag TAG] [--clear-auth-json]" in output
    assert "[--series SERIES]" in output
    assert "[--series-active|--no-series-active]" in output
    assert "codex-usage account list" not in output
    assert "codex-usage account overview" in output
    assert "--config-only" in output
    assert "codex-usage account backend ACCOUNT direct|app-server" in output
    assert "codex-usage account delete ACCOUNT" in output
    assert (
        "codex-usage account manage ACCOUNT [--browser auto|vivaldi|chromium|firefox]"
        in output
    )
    assert "codex-usage account terminal ACCOUNT" in output
    assert (
        "codex-usage google add ACCOUNT --oauth-client-json PATH --json" in output
    )
    assert "--format table|json" in output
    assert "codex-usage profile jobs [--account ACCOUNT] [--json]" in output
    assert "codex-usage profile job-status JOB_ID [--json]" in output
    assert "codex-usage profile cancel JOB_ID [--json]" in output
    assert "codex-usage profile device-login --account ACCOUNT [--codex-bin PATH]" in output
    assert "[--timeout SEKUNDEN]" in output
    assert "codex-usage login ACCOUNT" in output
    assert "codex-usage once" in output
    assert "codex-usage watch" in output
    assert "codex-usage watchdog" in output
    assert "codex-usage policy evaluate [ACCOUNT|--auth-json PATH]" in output
    assert "[--max-age SEKUNDEN] [--format json]" in output
    assert "codex-usage policy set account|group|agent|job" in output
    assert "codex-usage policy set-limits [--hourly N] [--weekly N] [--monthly N]" in output
    assert "codex-usage policy status" in output
    assert "codex-usage health" in output
    assert "--direct" in output
    assert "--backend direct|app-server" in output
    assert "codex-usage probe ACCOUNT" in output
    assert "codex-usage diagnose ACCOUNT" in output
    assert "--auth-json PATH" in output
    assert "codex-usage ingest ACCOUNT" in output
    assert "codex-usage latest [--format table|json]" in output
    assert "codex-usage values [--account ACCOUNT]" in output
    assert "[--baseline-minutes N] [--baseline-value-minutes N]" in output
    assert "[--smoothing none|ema-5|ema-10|ema-20|ema-40|" in output
    assert "ema-80|ema-160|ema-320|ema-640]" in output
    assert "[--pool POOL] [--limit-window short|weekly|monthly|spark|all]" in output
    assert "[--path PATH] [--now ISO] [--format table|json] [--json]" in output
    assert "history query --account ACCOUNT --pool POOL --window-seconds SECONDS" in output
    assert "[--since ISO] [--until ISO] [--path PATH]" in output
    assert "history prune [--before ISO|--days N] (--dry-run|--apply)" in output
    assert "integration-snapshot" not in output
    assert "profile create" in output
    assert output.count("[--tag TAG]") >= 2
    assert output.count("[--series SERIES]") >= 2
    assert output.count("[--series-active|--no-series-active]") >= 2
    assert "codex-usage bridge-snippet ACCOUNT" in output
    assert "codex-usage bridge-extension ACCOUNT" in output
    assert "codex-usage bridge-server" in output
    assert "--allow-remote" in output
    assert "--tls-cert" in output
    assert "--tls-key" in output
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


@pytest.mark.parametrize("argv", [(), "once", [1], ["once", 1]])
def test_cli_rejects_malformed_explicit_argv(argv, capsys):
    assert main(argv) == 2  # type: ignore[arg-type]
    assert "argv is invalid" in capsys.readouterr().err


@pytest.mark.parametrize(
    "now",
    [
        "0001-01-01T00:00:00Z",
        "0001-01-01T00:00:00+14:00",
        "9999-12-31T23:59:59-14:00",
    ],
)
def test_consumption_rejects_now_out_of_range(tmp_path, capsys, now):
    assert (
        main(
            [
                "consumption",
                "--account",
                "alpha",
                "--amount",
                "1",
                "--unit",
                "hours",
                "--now",
                now,
                "--path",
                str(tmp_path / "history.sqlite3"),
            ]
        )
        == 1
    )

    assert "now is out of range" in capsys.readouterr().err


def test_account_add_json_returns_all_editable_fields(tmp_path, capsys):
    assert main(
        [
            "--config",
            str(tmp_path / "config.toml"),
            "account",
            "add",
            "privat",
            "--label",
            "Privat",
            "--profile-dir",
            str(tmp_path / "profile"),
            "--browser",
            "chromium",
            "--reactivation-browser",
            "firefox",
            "--backend",
            "app-server",
            "--format",
            "json",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["account"]["label"] == "Privat"
    assert payload["account"]["browser"] == "chromium"
    assert payload["account"]["reactivation_browser"] == "firefox"
    assert payload["account"]["backend"] == "app-server"
    assert payload["account"]["auth_json_path"] is None


def test_account_add_rolls_back_when_managed_service_sync_fails(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr("codex_usage.cli.service_status", lambda: {"installed": True})
    monkeypatch.setattr(
        "codex_usage.cli.managed_service_config_path",
        lambda: config_path.absolute(),
    )

    def fail_service_sync(*_args, **_kwargs):
        raise OSError("service sync failed")

    monkeypatch.setattr("codex_usage.cli.service_install", fail_service_sync)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "add",
                "privat",
            ]
        )
        == 1
    )
    capsys.readouterr()

    assert load_config(config_path).accounts == ()


def test_account_add_syncs_managed_service_with_updated_config(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    synced = []
    monkeypatch.setattr("codex_usage.cli.service_status", lambda: {"installed": True})
    monkeypatch.setattr(
        "codex_usage.cli.managed_service_config_path",
        lambda: config_path.absolute(),
    )
    monkeypatch.setattr(
        "codex_usage.cli.service_install",
        lambda config, path: synced.append((config, path)),
    )

    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()

    assert len(synced) == 1
    assert synced[0][0].accounts[0].id == "privat"
    assert synced[0][1] == config_path


def test_account_update_rolls_back_when_managed_service_sync_fails(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
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
                "Old",
            ]
        )
        == 0
    )
    capsys.readouterr()
    monkeypatch.setattr("codex_usage.cli.service_status", lambda: {"installed": True})
    monkeypatch.setattr(
        "codex_usage.cli.managed_service_config_path",
        lambda: config_path.absolute(),
    )
    def fail_service_sync(*_args, **_kwargs):
        raise OSError("service sync failed")

    monkeypatch.setattr("codex_usage.cli.service_install", fail_service_sync)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "add",
                "privat",
                "--label",
                "New",
            ]
        )
        == 1
    )
    capsys.readouterr()

    assert load_config(config_path).accounts[0].label == "Old"


def test_account_update_service_sync_rollback_keeps_previous_usage_state(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
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
                "Old",
            ]
        )
        == 0
    )
    capsys.readouterr()
    save_current_usage(
        AccountUsage(
            account_id="privat",
            label="Old",
            captured_at=datetime.now().astimezone(),
            five_hour=LimitWindow(name="5h", remaining=12),
            weekly=LimitWindow(name="weekly", remaining=34),
        )
    )
    monkeypatch.setattr("codex_usage.cli.service_status", lambda: {"installed": True})
    monkeypatch.setattr(
        "codex_usage.cli.managed_service_config_path",
        lambda: config_path.absolute(),
    )

    def fail_service_sync(*_args, **_kwargs):
        raise OSError("service sync failed")

    monkeypatch.setattr("codex_usage.cli.service_install", fail_service_sync)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "add",
                "privat",
                "--label",
                "New",
            ]
        )
        == 1
    )
    capsys.readouterr()

    assert load_current_usage("privat") is not None


def test_account_update_state_failure_rolls_back_managed_service_unit(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    monkeypatch.setattr("codex_usage.cli.service_status", lambda: {"installed": True})
    monkeypatch.setattr(
        "codex_usage.cli.managed_service_config_path",
        lambda: config_path.absolute(),
    )
    synced = []
    monkeypatch.setattr(
        "codex_usage.cli.service_install",
        lambda config, path: synced.append((config, path)),
    )

    def fail_state_cleanup(*_args, **_kwargs):
        raise OSError("state cleanup failed")

    monkeypatch.setattr("codex_usage.state.remove_account_state", fail_state_cleanup)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "add",
                "privat",
                "--label",
                "New",
            ]
        )
        == 1
    )
    capsys.readouterr()

    assert [config.accounts[0].label for config, _ in synced] == ["New", "privat"]
    assert load_config(config_path).accounts[0].label == "privat"


def test_account_overview_json_exposes_paths_and_reactivation_browser(
    tmp_path, capsys
):
    config_path = tmp_path / "config.toml"
    auth_path = tmp_path / "auth.json"
    assert main(
        [
            "--config",
            str(config_path),
            "account",
            "add",
            "privat",
            "--profile-dir",
            str(tmp_path / "profile"),
            "--auth-json",
            str(auth_path),
            "--reactivation-browser",
            "vivaldi",
        ]
    ) == 0
    capsys.readouterr()

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

    item = json.loads(capsys.readouterr().out)["accounts"][0]
    assert item["profile_dir"] == str(tmp_path / "profile")
    assert item["auth_json_path"] == str(auth_path)
    assert item["reactivation_browser"] == "vivaldi"


def test_account_add_clear_auth_json(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    assert main(
        [
            "--config",
            str(config_path),
            "account",
            "add",
            "privat",
            "--auth-json",
            str(tmp_path / "auth.json"),
        ]
    ) == 0
    capsys.readouterr()

    assert main(
        [
            "--config",
            str(config_path),
            "account",
            "add",
            "privat",
            "--clear-auth-json",
            "--format",
            "json",
        ]
    ) == 0

    assert json.loads(capsys.readouterr().out)["account"]["auth_json_path"] is None


@pytest.mark.parametrize("available, expected", [(True, True), (False, False)])
def test_successful_usage_validates_dynamic_main_pool(available, expected):
    usage = AccountUsage(
        account_id="private",
        label="Private",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        main=UsagePool(
            key="main",
            display_name="Codex",
            available=available,
            windows=(LimitWindow(name="weekly", remaining=80),),
        ),
    )

    assert _is_successful_usage(usage) is expected


@pytest.mark.parametrize("field", ["allowed", "limit_reached"])
def test_successful_usage_rejects_invalid_dynamic_main_flags(field):
    usage = AccountUsage(
        account_id="private",
        label="Private",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(LimitWindow(name="weekly", remaining=80),),
            **{field: "false"},
        ),
    )

    assert _is_successful_usage(usage) is False


def test_successful_usage_requires_complete_backend_provenance():
    usage = AccountUsage(
        account_id="private",
        label="Private",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
        status=AccountStatus.OK,
        main=UsagePool(
            key="main",
            display_name="Codex",
            available=True,
            windows=(LimitWindow(name="weekly", remaining=80),),
        ),
    )

    assert _is_successful_usage(usage) is False


@pytest.mark.parametrize("field", ["backend_configured", "backend_used"])
def test_successful_usage_rejects_unhashable_backend_provenance(field):
    usage = AccountUsage(
        account_id="private",
        label="Private",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
        main=UsagePool(
            key="main",
            display_name="Codex",
            available=True,
            windows=(LimitWindow(name="weekly", remaining=80),),
        ),
    )
    object.__setattr__(usage, field, [])

    assert _is_successful_usage(usage) is False


def test_policy_usage_rejects_unhashable_backend_without_raising(monkeypatch):
    account = Account(
        id="private",
        label="Private",
        profile_dir="/tmp/private",
        backend="direct",
    )
    malformed = AccountUsage(
        account_id=account.id,
        label=account.label,
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used=[],
    )
    monkeypatch.setattr(cli_module, "load_current_usage", lambda _account_id: malformed)
    monkeypatch.setattr(cli_module, "load_usage_snapshot", lambda _account_id: None)

    result = _usage_for_policy(account)

    assert result.status is AccountStatus.ERROR
    assert result.error == "usage backend provenance mismatch"


def test_policy_commands_are_machine_readable_and_use_saved_usage(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    configured_auth = tmp_path / "configured-auth.json"
    agent_auth = tmp_path / "agent-auth.json"
    for auth_path in (configured_auth, agent_auth):
        auth_path.write_text(
            json.dumps({"tokens": {"account_id": "backend-private"}}),
            encoding="utf-8",
        )
        auth_path.chmod(0o600)
    assert main(
        [
            "--config",
            str(config_path),
            "account",
            "add",
            "private",
            "--auth-json",
            str(configured_auth),
        ]
    ) == 0
    capsys.readouterr()
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    save_current_usage(
        AccountUsage(
            account_id="private",
            label="Private",
            captured_at=now,
            status=AccountStatus.OK,
            main=UsagePool(
                key="main",
                display_name="Codex",
                windows=(
                    LimitWindow(
                        name="weekly",
                        remaining=80,
                        percent=80,
                        duration_seconds=604800,
                    ),
                ),
            ),
            backend_configured="direct",
            backend_used="direct",
            models=(
                UsagePool(
                    key="gpt-5.3-codex-spark",
                    display_name="Spark",
                    windows=(
                        LimitWindow(
                            name="weekly",
                            remaining=90,
                            percent=90,
                            duration_seconds=604800,
                        ),
                    ),
                    available=True,
                    availability_sources=("usage", "model_catalog"),
                ),
            ),
            backend_account_id="backend-private",
        )
    )
    set_spark_health("backend-private", "healthy")

    assert main(["policy", "set", "global", "deny"]) == 0
    policy = json.loads(capsys.readouterr().out)
    assert policy["global"] is False
    assert main(
        [
            "--config",
            str(config_path),
            "policy",
            "evaluate",
            "private",
            "--role",
            "arbeitsbiene",
        ]
    ) == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["schema_version"] == 1
    assert decision["decision"] == "spark"
    assert decision["model"] == "gpt-5.3-codex-spark"
    assert decision["paid_overage_allowed"] is False

    assert main(
        [
            "--config",
            str(config_path),
            "policy",
            "evaluate",
            "--auth-json",
            str(agent_auth),
            "--role",
            "arbeitsbiene",
        ]
    ) == 0
    auth_decision = json.loads(capsys.readouterr().out)
    assert auth_decision["account"] == "private"
    assert auth_decision["backend_account_id"] == "backend-private"

    assert main(["--config", str(config_path), "policy", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["schema_version"] == 1
    assert status["policy"]["global"] is False
    assert status["decisions"]["private"]["decision"] == "spark"


def test_policy_evaluate_returns_failure_for_blocked_decision(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "private"]) == 0
    capsys.readouterr()
    save_current_usage(
        AccountUsage(
            account_id="private",
            label="Private",
            captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
            status=AccountStatus.OK,
            main=UsagePool(
                key="main",
                display_name="Codex",
                windows=(LimitWindow(name="weekly", remaining=5),),
            ),
            backend_configured="direct",
            backend_used="direct",
        )
    )

    assert main(
        [
            "--config",
            str(config_path),
            "policy",
            "evaluate",
            "private",
            "--role",
            "arbeitsbiene",
        ]
    ) == 2
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "blocked"


def test_policy_status_returns_failure_without_accounts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"

    assert main(["--config", str(config_path), "policy", "status"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["decisions"] == {}


@pytest.mark.parametrize("decision", [[], {}])
def test_policy_decision_exit_code_rejects_unhashable_decision(decision):
    assert _policy_decision_exit_code({"decision": decision}) == 2


def test_policy_fails_closed_for_cached_backend_mismatch(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "private"]) == 0
    capsys.readouterr()
    save_current_usage(
        AccountUsage(
            account_id="private",
            label="Private",
            captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
            status=AccountStatus.OK,
            main=UsagePool(
                key="main",
                display_name="Codex",
                windows=(LimitWindow(name="weekly", remaining=80),),
            ),
            backend_configured="direct",
            backend_used="app-server",
        )
    )

    assert (
        main(
            [
                "--config",
                str(config_path),
                "policy",
                "evaluate",
                "private",
                "--role",
                "arbeitsbiene",
            ]
        )
        == 2
    )
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "blocked"
    assert decision["reason"] == "cache_invalidated"


def test_policy_fails_closed_for_invalid_cached_percent_with_remaining(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "private"]) == 0
    capsys.readouterr()
    save_current_usage(
        AccountUsage(
            account_id="private",
            label="Private",
            captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
            status=AccountStatus.OK,
            five_hour=LimitWindow(name="5h", remaining=97, percent=101),
            backend_configured="direct",
            backend_used="direct",
            backend_account_id="backend-private",
        )
    )

    assert (
        main(
            [
                "--config",
                str(config_path),
                "policy",
                "evaluate",
                "private",
                "--role",
                "arbeitsbiene",
            ]
        )
        == 2
    )
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "blocked"
    assert decision["reason"] == "usage_stale"


def test_policy_does_not_relabel_identity_free_auth_cache(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"tokens": {"account_id": "backend-private"}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "add",
                "private",
                "--auth-json",
                str(auth_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    save_current_usage(
        AccountUsage(
            account_id="private",
            label="Private",
            captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
            status=AccountStatus.OK,
            main=UsagePool(
                key="main",
                display_name="Codex",
                windows=(LimitWindow(name="weekly", remaining=80),),
            ),
            backend_configured="direct",
            backend_used="direct",
        )
    )

    assert (
        main(
            [
                "--config",
                str(config_path),
                "policy",
                "evaluate",
                "--auth-json",
                str(auth_path),
                "--role",
                "arbeitsbiene",
            ]
        )
        == 2
    )
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "blocked"
    assert decision["reason"] == "cache_invalidated"


def test_policy_set_requires_identifier_for_non_global_scope(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert main(["policy", "set", "job", "allow"]) == 1

    assert "--id is required" in capsys.readouterr().err


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
        assert capsys.readouterr().out == f"codex-usage {__version__}\n"


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


def test_global_step_up_stdin_flag_survives_root_command_normalization() -> None:
    argv = ["--step-up-stdin", "account", "auth-sync", "openai-one", "--format", "json"]

    assert cli_module._default_root_command(argv) == argv
    parsed = cli_module._build_parser().parse_args(argv)
    assert parsed.step_up_stdin is True
    assert parsed.command == "account"
    assert parsed.account_command == "auth-sync"


def test_select_accounts_rejects_duplicate_refs():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/privat")
    config = AppConfig(accounts=(account,))

    with pytest.raises(ValueError, match="duplicate account selection"):
        _select_accounts(config, ["privat", "Privat"])


def test_all_usage_results_rejects_duplicate_ids():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
        status=AccountStatus.OK,
        backend_configured="direct",
        backend_used="direct",
    )

    assert not _all_usage_results_valid(
        [usage, usage],
        ["privat", "privat"],
        predicate=lambda _: True,
    )


def test_load_overview_rejects_duplicate_usage_ids(monkeypatch):
    account = Account(id="privat", label="Privat", profile_dir="/tmp/privat")
    config = AppConfig(accounts=(account,))
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=datetime.now(ZoneInfo("Europe/Berlin")),
        status=AccountStatus.ERROR,
    )
    monkeypatch.setattr(
        "codex_usage.cli.fetch_all",
        lambda *_args, **_kwargs: [usage, usage],
    )

    with pytest.raises(ValueError, match="usage result identity mismatch"):
        _load_overview_usages(config)


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


def test_login_accepts_unique_label_and_marks_projection_sync_required(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.toml"
    called = {}

    def fake_login(account, config):
        called["account_id"] = account.id
        called["label"] = account.label
        called["url"] = config.analytics_url

    monkeypatch.setattr("codex_usage.cli.login_account", fake_login)
    monkeypatch.setattr(
        cli_module,
        "sync_account_auth",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must remain explicit")),
    )

    assert (
        main(["--config", str(config_path), "account", "add", "privat", "--label", "BW_Privat"])
        == 0
    )
    assert main(["--config", str(config_path), "login", "BW_Privat"]) == 0

    assert "sync_required" in capsys.readouterr().out
    restarted = load_config(config_path).accounts[0]
    assert restarted.auth_sync_required is True
    assert restarted.auth_sync_generation == 1
    assert called == {
        "account_id": "privat",
        "label": "BW_Privat",
        "url": "https://chatgpt.com/codex/cloud/settings/analytics",
    }


def test_reactivate_success_persists_sync_required_without_upload(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.toml"
    add_or_update_account("openai-1", path=config_path)
    monkeypatch.setattr(
        cli_module,
        "reactivate_account",
        lambda *_args, **kwargs: {"ok": True, "browser": kwargs["browser"]},
    )
    monkeypatch.setattr(
        cli_module,
        "sync_account_auth",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must remain explicit")),
    )

    assert (
        main(
            [
                "--config",
                str(config_path),
                "reactivate",
                "openai-1",
                "--browser",
                "firefox",
            ]
        )
        == 0
    )

    restarted = load_config(config_path).accounts[0]
    assert restarted.auth_sync_required is True
    assert restarted.auth_sync_generation == 1
    assert "sync_required" in capsys.readouterr().out


def test_account_auth_sync_uses_resolved_account_and_authenticated_client(
    monkeypatch, capsys
):
    account = Account(
        id="openai-1",
        label="OpenAI",
        profile_dir="/private/profile",
        auth_json_path="/private/profile/codex-home/auth.json",
    )
    config = SimpleNamespace(masterjet=object())
    client = object()
    calls = []
    persisted = []
    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "resolve_account", lambda *_args: account)
    monkeypatch.setattr(cli_module, "_new_masterjet_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(
        cli_module,
        "compare_and_clear_account_auth_sync_required",
        lambda snapshot, **kwargs: persisted.append((snapshot, kwargs)) or True,
    )
    monkeypatch.setattr(
        cli_module,
        "sync_account_auth",
        lambda selected, authenticated: calls.append((selected, authenticated))
        or SimpleNamespace(account_ref="openai-1", generation=5, status="succeeded"),
    )

    assert main(["account", "auth-sync", "OpenAI", "--format", "json"]) == 0

    assert calls == [(account, client)]
    assert persisted == [
        (account, {"path": None})
    ]
    assert json.loads(capsys.readouterr().out) == {
        "account_ref": "openai-1",
        "generation": 5,
        "status": "succeeded",
    }


def test_account_auth_sync_success_clears_persisted_sync_required(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.toml"
    add_or_update_account("openai-1", path=config_path)
    config_module.mark_account_auth_sync_required("openai-1", path=config_path)
    monkeypatch.setattr(cli_module, "MasterjetControlClient", lambda _connection: object())
    monkeypatch.setattr(
        cli_module,
        "sync_account_auth",
        lambda *_args: SimpleNamespace(
            account_ref="openai-1", generation=5, status="succeeded"
        ),
    )

    assert main(["--config", str(config_path), "account", "auth-sync", "openai-1"]) == 0

    restarted = load_config(config_path).accounts[0]
    assert restarted.auth_sync_required is False
    assert restarted.auth_sync_generation == 1
    assert "succeeded" in capsys.readouterr().out


def test_account_auth_sync_old_completion_cannot_clear_newer_reauth_and_retry_can(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.toml"
    add_or_update_account("openai-1", path=config_path)
    first = config_module.mark_account_auth_sync_required(
        "openai-1", path=config_path
    )
    assert first.auth_sync_generation == 1
    monkeypatch.setattr(cli_module, "MasterjetControlClient", lambda _connection: object())

    def sync_with_concurrent_reauth(account, _client):
        assert account.auth_sync_generation == 1
        config_module.mark_account_auth_sync_required(
            account.id, path=config_path
        )
        return SimpleNamespace(
            account_ref="remote-openai-7",
            generation=5,
            status="succeeded",
        )

    monkeypatch.setattr(cli_module, "sync_account_auth", sync_with_concurrent_reauth)

    assert main(["--config", str(config_path), "account", "auth-sync", "openai-1"]) == 0
    raced = load_config(config_path).accounts[0]
    assert raced.auth_sync_required is True
    assert raced.auth_sync_generation == 2

    monkeypatch.setattr(
        cli_module,
        "sync_account_auth",
        lambda *_args: SimpleNamespace(
            account_ref="remote-openai-7",
            generation=6,
            status="succeeded",
        ),
    )
    assert main(["--config", str(config_path), "account", "auth-sync", "openai-1"]) == 0
    retried = load_config(config_path).accounts[0]
    assert retried.auth_sync_required is False
    assert retried.auth_sync_generation == 2
    assert "remote-openai-7" in capsys.readouterr().out


def test_account_auth_sync_has_no_secret_path_or_provider_argv(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(cli_module, "sync_account_auth", lambda *_args: called.append(True))

    with pytest.raises(SystemExit) as caught:
        main(["account", "auth-sync", "openai-1", "--auth-json", "top-secret"])

    assert caught.value.code == 2
    assert called == []
    assert "top-secret" not in capsys.readouterr().out


def test_account_auth_sync_without_productive_providers_fails_closed(
    monkeypatch, capsys
):
    account = Account(
        id="openai-1",
        label="OpenAI",
        profile_dir="/private/profile",
        auth_json_path="/private/profile/codex-home/auth.json",
    )
    config = SimpleNamespace(masterjet=object())
    client = object()
    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "resolve_account", lambda *_args: account)
    monkeypatch.setattr(cli_module, "_new_masterjet_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(
        cli_module,
        "sync_account_auth",
        lambda *_args: (_ for _ in ()).throw(
            cli_module.AuthSyncError("control.authentication_required")
        ),
    )

    assert main(["account", "auth-sync", "openai-1"]) == 2
    assert capsys.readouterr().err.strip() == "Fehler: control.authentication_required"


def test_account_auth_sync_json_error_is_redacted_for_bounded_runner(monkeypatch, capsys):
    account = Account(
        id="openai-1",
        label="OpenAI",
        profile_dir="/private/profile",
        auth_json_path="/private/profile/codex-home/auth.json",
    )
    config = SimpleNamespace(masterjet=object())
    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "resolve_account", lambda *_args: account)
    monkeypatch.setattr(
        cli_module,
        "_new_masterjet_client",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        cli_module,
        "sync_account_auth",
        lambda *_args: (_ for _ in ()).throw(
            cli_module.AuthSyncError("control.step_up_required")
        ),
    )

    assert main(["account", "auth-sync", "openai-1", "--format", "json"]) == 2

    output = capsys.readouterr()
    assert json.loads(output.out) == {"ok": False, "code": "control.step_up_required"}
    assert output.err == ""


def test_google_provision_apply_requires_confirm_before_config_or_request(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        cli_module,
        "load_config",
        lambda _path: (_ for _ in ()).throw(AssertionError("request started")),
    )

    assert main(["google", "provision-apply", "google-one", "plan-1", "--json"]) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "ok": False,
        "code": "confirmation_required",
    }
    assert captured.err == ""


def test_google_cli_fixed_commands_forward_only_redacted_values(monkeypatch, capsys):
    calls = []

    class Controller:
        def account_details(self):
            calls.append(("list",))
            account = SimpleNamespace(
                ref="google-one",
                label="Google One",
                enabled=True,
                subject_bound=True,
                oauth_state="ready",
                inventory_generation=4,
                quota_state="fresh",
                project_count=1,
                billing_count=0,
                reload_state="ready",
            )
            project = SimpleNamespace(
                ref="hive-one", project_name="Amber Orchard", purpose="quota_probe",
                key_name="Willow Meadow", billing_ref=None, status="ready",
                probe_state="ready", quota_state="available",
            )
            return (SimpleNamespace(account=account, projects=(project,)),)

        def oauth_begin(self, account_ref, *, browser):
            calls.append(("oauth_begin", account_ref, browser))
            return SimpleNamespace(
                id="oauth-1",
                account_ref=account_ref,
                authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
                expires_at=datetime(2026, 8, 28, 12, 5, tzinfo=ZoneInfo("UTC")),
                generation=4,
            )

        def import_oauth_client(self, account_ref, path):
            calls.append(("import", account_ref, path))
            return SimpleNamespace(account_ref=account_ref, generation=5, status="succeeded")

        def inventory_refresh(self, account_ref):
            calls.append(("inventory", account_ref))
            return SimpleNamespace(
                id="refresh-1",
                kind="google.inventory.refresh",
                state="succeeded",
                expected_generation=4,
                resulting_generation=5,
                plan_digest="sha256:" + "a" * 64,
                expires_at=datetime(2026, 8, 28, 12, 5, tzinfo=ZoneInfo("UTC")),
            )

        def provision_plan(self, account_ref):
            calls.append(("plan", account_ref))
            return SimpleNamespace(
                account_ref=account_ref,
                plan_id="plan-1",
                expected_generation=4,
                plan_digest="sha256:" + "a" * 64,
                expires_at=datetime(2026, 8, 28, 12, 5, tzinfo=ZoneInfo("UTC")),
                step_count=1,
                projects=(
                    SimpleNamespace(project_name="Amber Orchard", key_name="Willow Meadow"),
                ),
            )

        def provision_apply(self, plan_id, *, account_ref, plan_digest):
            calls.append(("apply", account_ref, plan_id, plan_digest))
            return SimpleNamespace(
                id="apply-1",
                kind="google.provision.apply",
                state="succeeded",
                expected_generation=4,
                resulting_generation=5,
                plan_digest="sha256:" + "a" * 64,
                expires_at=datetime(2026, 8, 28, 12, 5, tzinfo=ZoneInfo("UTC")),
            )

    monkeypatch.setattr(cli_module, "_new_google_controller", lambda _path: Controller())
    monkeypatch.setattr(cli_module, "_save_google_projection", lambda _details: None)
    monkeypatch.setattr(
        cli_module, "_new_google_oauth_controller", lambda _path: Controller()
    )

    assert main(["google", "accounts", "--json"]) == 0
    assert (
        main(
            [
                "google",
                "oauth-begin",
                "google-one",
                "--browser",
                "firefox",
                "--json",
            ]
        )
        == 0
    )
    assert main(["google", "inventory-refresh", "google-one", "--json"]) == 0
    assert main(["google", "provision-plan", "google-one", "--json"]) == 0
    assert (
        main(
            [
                "google",
                "provision-apply",
                "google-one",
                "plan-1",
                "--plan-digest",
                "sha256:" + "a" * 64,
                "--confirm",
                "--json",
            ]
        )
        == 0
    )

    assert calls == [
        ("list",),
        ("oauth_begin", "google-one", "firefox"),
        ("inventory", "google-one"),
        ("plan", "google-one"),
        ("apply", "google-one", "plan-1", "sha256:" + "a" * 64),
    ]
    output = capsys.readouterr().out
    assert "access_token" not in output
    assert "client_secret" not in output


def test_google_provision_apply_missing_digest_fails_before_controller(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        cli_module,
        "_new_google_controller",
        lambda _path: (_ for _ in ()).throw(AssertionError("controller started")),
    )

    assert main(
        [
            "google",
            "provision-apply",
            "google-one",
            "plan-1",
            "--confirm",
            "--json",
        ]
    ) == 2

    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "code": "control.response_invalid",
    }


def test_google_add_keeps_oauth_client_path_local(monkeypatch, tmp_path, capsys):
    source = tmp_path / "oauth-client.json"
    source.write_text("private", encoding="utf-8")
    seen = []

    class Controller:
        def import_oauth_client(self, account_ref, path):
            seen.append(("import", account_ref, path))
            return SimpleNamespace(account_ref=account_ref, generation=5, status="succeeded")

    monkeypatch.setattr(cli_module, "_new_google_controller", lambda _path: Controller())

    assert (
        main(
            [
                "google",
                "add",
                "google-one",
                "--oauth-client-json",
                str(source),
                "--json",
            ]
        )
        == 0
    )

    assert seen == [("import", "google-one", source)]
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "account_ref": "google-one",
        "generation": 5,
        "status": "succeeded",
        "ok": True,
    }
    assert str(source) not in repr(output)


def test_google_add_rejects_browser_before_controller(monkeypatch, tmp_path, capsys):
    source = tmp_path / "oauth-client.json"
    source.write_text("private", encoding="utf-8")
    monkeypatch.setattr(
        cli_module,
        "_new_google_controller",
        lambda _path: (_ for _ in ()).throw(AssertionError("controller constructed")),
    )

    with pytest.raises(SystemExit) as caught:
        main(
            [
                "google",
                "add",
                "google-one",
                "--oauth-client-json",
                str(source),
                "--browser",
                "firefox",
            ]
        )

    assert caught.value.code == 2
    assert "unrecognized arguments: --browser firefox" in capsys.readouterr().err


@pytest.mark.parametrize("state", ["partial", "failed", "blocked"])
def test_google_operation_terminal_failure_is_nonzero_structured_and_redacted(
    monkeypatch, capsys, state
):
    operation = SimpleNamespace(
        id="refresh-1",
        kind="google.inventory.refresh",
        state=state,
        expected_generation=4,
        resulting_generation=None,
        plan_digest="sha256:" + "a" * 64,
        expires_at=datetime(2026, 8, 28, 12, 5, tzinfo=ZoneInfo("UTC")),
    )
    controller = SimpleNamespace(inventory_refresh=lambda _account: operation)
    monkeypatch.setattr(cli_module, "_new_google_controller", lambda _path: controller)

    assert main(["google", "inventory-refresh", "google-one", "--json"]) == 2

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["code"] == f"control.operation_{state}"
    assert output["state"] == state
    assert "secret" not in json.dumps(output).casefold()


@pytest.mark.parametrize("state", ["partial", "failed", "blocked"])
def test_google_add_terminal_receipt_failure_is_nonzero_structured(
    monkeypatch, tmp_path, capsys, state
):
    source = tmp_path / "oauth-client.json"
    source.write_text("private", encoding="utf-8")
    result = SimpleNamespace(account_ref="google-one", generation=4, status=state)
    controller = SimpleNamespace(import_oauth_client=lambda *_args: result)
    monkeypatch.setattr(cli_module, "_new_google_controller", lambda _path: controller)

    assert (
        main(
            [
                "google",
                "add",
                "google-one",
                "--oauth-client-json",
                str(source),
                "--json",
            ]
        )
        == 2
    )

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "account_ref": "google-one",
        "generation": 4,
        "status": state,
        "ok": False,
        "code": f"control.operation_{state}",
    }


def test_google_json_flag_selects_json_instead_of_human_output(monkeypatch, capsys):
    row = SimpleNamespace(
        ref="google-one",
        label="Google One",
        enabled=True,
        subject_bound=True,
        oauth_state="ready",
        inventory_generation=4,
        quota_state="fresh",
        project_count=1,
        billing_count=0,
        reload_state="ready",
    )
    project = SimpleNamespace(
        ref="hive-one", project_name="Amber Orchard", purpose="quota_probe",
        key_name="Willow Meadow", billing_ref=None, status="ready",
        probe_state="ready", quota_state="available",
    )
    controller = SimpleNamespace(
        account_details=lambda: (SimpleNamespace(account=row, projects=(project,)),)
    )
    monkeypatch.setattr(cli_module, "_new_google_controller", lambda _path: controller)
    monkeypatch.setattr(cli_module, "_save_google_projection", lambda _details: None)

    assert main(["google", "accounts"]) == 0
    human = capsys.readouterr().out
    assert human.startswith("REF")
    with pytest.raises(json.JSONDecodeError):
        json.loads(human)

    assert main(["google", "accounts", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["accounts"][0]["ref"] == "google-one"


def test_google_cli_rejects_secret_quota_and_provider_identifier_options(capsys):
    for option in ("--token", "--secret", "--quota-remaining", "--provider-id"):
        with pytest.raises(SystemExit) as caught:
            main(["google", "accounts", option])
        assert caught.value.code == 2

    capsys.readouterr()


def test_google_production_cli_sanitizes_client_construction_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_module,
        "MasterjetControlClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("client constructed")),
    )

    assert main(["google", "accounts", "--json"]) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "ok": False,
        "code": "control.transport_unavailable",
    }
    assert captured.err == ""


def test_google_oauth_begin_without_productive_callback_is_json_fail_closed(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        cli_module,
        "MasterjetControlClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("client constructed")
        ),
    )

    assert (
        main(
            [
                "google",
                "oauth-begin",
                "google-one",
                "--browser",
                "firefox",
                "--json",
            ]
        )
        == 2
    )

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "ok": False,
        "code": "oauth.callback_unavailable",
    }
    assert captured.err == ""


@pytest.mark.parametrize(
    "command",
    [
        ["google", "accounts", "--json"],
        ["google", "inventory-refresh", "google-one", "--json"],
        ["google", "provision-plan", "google-one", "--json"],
    ],
)
def test_google_json_sanitizes_unexpected_controller_or_transport_error(
    monkeypatch, capsys, command
):
    monkeypatch.setattr(
        cli_module,
        "_new_google_controller",
        lambda _path: (_ for _ in ()).throw(RuntimeError("Bearer topsecret")),
    )

    assert main(command) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "ok": False,
        "code": "control.transport_unavailable",
    }
    assert captured.err == ""
    assert "topsecret" not in captured.out


def test_masterjet_status_reports_invalid_default_endpoint_fail_closed(capsys):
    assert main(["masterjet", "status", "--json"]) == 2

    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "code": "control.endpoint_invalid",
    }


def test_masterjet_openai_routing_options_uses_control_client(monkeypatch, capsys):
    config = AppConfig(
        accounts=(
            Account(
                id="profile-one",
                label="OpenAI One",
                profile_dir="/private/profile-one",
                series="A",
            ),
        )
    )
    remote = OpenAIControlAccount(
        ref="openai-one",
        label="OpenAI One",
        enabled=True,
        local_profile_ref="profile-one",
        source_host_ref="host-one",
        auth_state="ready",
        access_expires_at=None,
        credential_generation=4,
        vault_projection_state="current",
        usage_state="available",
    )
    calls = []

    class _Client:
        def call(self, operation, arguments):
            calls.append((operation, arguments))
            return (remote,)

    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "MasterjetControlClient", lambda _connection: _Client())

    assert main(["masterjet", "openai-routing-options", "--json"]) == 0
    assert calls == [("openai.accounts.list", {})]
    assert json.loads(capsys.readouterr().out) == {
        "stale": False,
        "series": [
            {"prefix": "A", "enabled": True, "provider": "openai_chatgpt"}
        ]
    }


def test_masterjet_openai_routing_options_uses_fresh_control_cache_on_outage(
    tmp_path, monkeypatch, capsys
):
    config = AppConfig(
        accounts=(
            Account(
                id="profile-one",
                label="OpenAI One",
                profile_dir="/private/profile-one",
                series="A",
            ),
        )
    )
    remote = OpenAIControlAccount(
        ref="openai-one",
        label="OpenAI One",
        enabled=True,
        local_profile_ref="profile-one",
        source_host_ref="host-one",
        auth_state="ready",
        access_expires_at=None,
        credential_generation=4,
        vault_projection_state="current",
        usage_state="available",
    )
    cached = CachedControlSnapshot(
        snapshot=ControlSnapshot(openai_accounts=(remote,)),
        observed_at=1.0,
        stale=False,
    )

    class _UnavailableClient:
        def call(self, _operation, _arguments):
            raise cli_module.MasterjetClientError("control.transport_unavailable")

    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(
        cli_module,
        "MasterjetControlClient",
        lambda _connection: _UnavailableClient(),
    )
    monkeypatch.setattr(cli_module, "default_state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        cli_module,
        "load_control_snapshot",
        lambda _root, _max_age: cached,
        raising=False,
    )

    assert main(["masterjet", "openai-routing-options", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "stale": True,
        "series": [
            {"prefix": "A", "enabled": True, "provider": "openai_chatgpt"}
        ]
    }


def test_masterjet_openai_routing_options_rejects_stale_control_cache(
    tmp_path, monkeypatch, capsys
):
    cached = CachedControlSnapshot(
        snapshot=ControlSnapshot(),
        observed_at=1.0,
        stale=True,
    )

    class _UnavailableClient:
        def call(self, _operation, _arguments):
            raise cli_module.MasterjetClientError("control.transport_unavailable")

    monkeypatch.setattr(cli_module, "load_config", lambda _path: AppConfig(accounts=()))
    monkeypatch.setattr(
        cli_module,
        "MasterjetControlClient",
        lambda _connection: _UnavailableClient(),
    )
    monkeypatch.setattr(cli_module, "default_state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        cli_module,
        "load_control_snapshot",
        lambda _root, _max_age: cached,
    )

    assert main(["masterjet", "openai-routing-options", "--json"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "code": "control.cache_unavailable",
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
        return [
            AccountUsage(
                account_id="privat",
                label="privat",
                captured_at=datetime.now().astimezone(),
                backend_configured="direct",
                backend_used="direct",
                five_hour=LimitWindow(name="5h", remaining=97),
            )
        ]

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


def test_once_rejects_override_when_configured_auth_identity_is_unavailable(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.toml"
    configured_auth = tmp_path / "configured-auth.json"
    override_auth = tmp_path / "override-auth.json"
    override_auth.write_text(
        json.dumps(
            {
                "tokens": {
                    "account_id": "account-override",
                    "id_token": "header.payload.signature",
                }
            }
        ),
        encoding="utf-8",
    )

    def fail_fetch_all(*_args, **_kwargs):
        raise AssertionError("fetch must not run without configured auth binding")

    monkeypatch.setattr("codex_usage.cli.fetch_all", fail_fetch_all)
    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "add",
                "privat",
                "--auth-json",
                str(configured_auth),
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
                "once",
                "--direct",
                "--auth-json",
                str(override_auth),
            ]
        )
        == 1
    )
    assert "configured auth.json identity unavailable" in capsys.readouterr().err


def test_once_accepts_matching_auth_json_override_for_configured_account(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.toml"
    configured_auth = tmp_path / "configured-auth.json"
    override_auth = tmp_path / "override-auth.json"
    auth_payload = json.dumps({"tokens": {"account_id": "account-private"}})
    configured_auth.write_text(auth_payload, encoding="utf-8")
    override_auth.write_text(auth_payload, encoding="utf-8")
    configured_auth.chmod(0o600)
    override_auth.chmod(0o600)
    called = {}

    def fake_fetch_all(config, accounts, **kwargs):
        called["account"] = [account.id for account in accounts]
        called["auth_json_path"] = kwargs["auth_json_path"]
        return [
            AccountUsage(
                account_id="privat",
                label="Privat",
                captured_at=datetime.now().astimezone(),
                backend_configured="direct",
                backend_used="direct",
                five_hour=LimitWindow(name="5h", remaining=97),
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
                "--auth-json",
                str(configured_auth),
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
                "once",
                "--direct",
                "--auth-json",
                str(override_auth),
            ]
        )
        == 0
    )
    assert called == {"account": ["privat"], "auth_json_path": override_auth}


def test_once_fails_closed_for_empty_fetch_result(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    monkeypatch.setattr("codex_usage.cli.fetch_all", lambda *args, **kwargs: [])

    assert main(["--config", str(config_path), "once"]) == 2


def test_once_fails_closed_for_stale_ok_usage(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    usage = AccountUsage(
        account_id="privat",
        label="privat",
        captured_at=datetime.now().astimezone(),
        five_hour=LimitWindow(name="5h", remaining=97),
        stale=True,
    )
    monkeypatch.setattr("codex_usage.cli.fetch_all", lambda *args, **kwargs: [usage])

    assert main(["--config", str(config_path), "once"]) == 2


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

    assert main(["--config", str(config_path), "watchdog", "--format", "json"]) == 2

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

    def fake_run_bridge_server(config, *, host, port, config_path, tls_cert, tls_key):
        called["host"] = host
        called["port"] = port
        called["tls_cert"] = tls_cert
        called["tls_key"] = tls_key

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
                "--tls-cert",
                str(tmp_path / "cert.pem"),
                "--tls-key",
                str(tmp_path / "key.pem"),
            ]
        )
        == 0
    )

    assert called == {
        "host": "0.0.0.0",
        "port": 8765,
        "tls_cert": tmp_path / "cert.pem",
        "tls_key": tmp_path / "key.pem",
    }


def test_bridge_server_rejects_remote_host_without_tls(
    tmp_path,
    monkeypatch,
    capsys,
):
    config_path = tmp_path / "config.toml"
    called = {}

    def fake_run_bridge_server(*_args, **_kwargs):
        called["started"] = True

    monkeypatch.setattr("codex_usage.cli.run_bridge_server", fake_run_bridge_server)

    assert main([
        "--config",
        str(config_path),
        "bridge-server",
        "--host",
        "0.0.0.0",
        "--allow-remote",
    ]) == 1

    assert called == {}
    assert "tls" in capsys.readouterr().err.lower()


def test_bridge_server_allows_loopback_host_without_opt_in(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config.toml"
    called = {}

    def fake_run_bridge_server(config, *, host, port, config_path, tls_cert, tls_key):
        called["host"] = host
        called["port"] = port
        called["tls_cert"] = tls_cert
        called["tls_key"] = tls_key

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

    assert called == {
        "host": "::1",
        "port": 9999,
        "tls_cert": None,
        "tls_key": None,
    }


def test_direct_rejects_multiple_accounts_without_per_account_auth_json(tmp_path, capsys):
    config_path = tmp_path / "config.toml"

    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    assert main(["--config", str(config_path), "account", "add", "work"]) == 0
    capsys.readouterr()

    assert main(["--config", str(config_path), "once", "--direct"]) == 1

    assert "requires per-account --auth-json" in capsys.readouterr().err


@pytest.mark.parametrize("command", ("once", "watch", "watchdog"))
def test_backend_direct_rejects_multiple_accounts_without_per_account_auth_json(
    tmp_path, monkeypatch, capsys, command
):
    config_path = tmp_path / "config.toml"

    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    assert main(["--config", str(config_path), "account", "add", "work"]) == 0
    capsys.readouterr()

    if command == "once":
        monkeypatch.setattr(
            "codex_usage.cli.fetch_all",
            lambda *args, **kwargs: pytest.fail("direct fetch started before validation"),
        )
    elif command == "watch":
        monkeypatch.setattr(
            "codex_usage.cli.watch",
            lambda *args, **kwargs: pytest.fail("watch started before validation"),
        )
    else:
        monkeypatch.setattr(
            "codex_usage.cli.watchdog",
            lambda *args, **kwargs: pytest.fail("watchdog started before validation"),
        )

    assert (
        main(["--config", str(config_path), command, "--backend", "direct"])
        == 1
    )

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


def test_direct_rejects_auth_json_override_from_another_account(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.toml"
    account_auth = tmp_path / "selected-auth.json"
    override_auth = tmp_path / "override-auth.json"
    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "add",
                "privat",
                "--auth-json",
                str(account_auth),
            ]
        )
        == 0
    )
    capsys.readouterr()
    monkeypatch.setattr(
        "codex_usage.cli.auth_identity_for_account",
        lambda _account: ("selected-user", "selected-account"),
    )
    monkeypatch.setattr(
        "codex_usage.cli.auth_identity_from_file",
        lambda _path: ("other-user", "other-account"),
    )
    monkeypatch.setattr(
        "codex_usage.cli.fetch_all",
        lambda *args, **kwargs: pytest.fail("foreign override reached fetch"),
    )

    assert (
        main(
            [
                "--config",
                str(config_path),
                "once",
                "--account",
                "privat",
                "--direct",
                "--auth-json",
                str(override_auth),
            ]
        )
        == 1
    )

    assert "identity does not match the selected account" in capsys.readouterr().err


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

    assert main(["--config", str(config_path), "account", "overview"]) == 2

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
    now = datetime(2026, 7, 10, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    class Clock:
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now

    monkeypatch.setattr("codex_usage.render.datetime", Clock)

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
                    2099, 7, 19, 23, 17, tzinfo=ZoneInfo("Europe/Berlin")
                ),
                backend_configured="direct",
                backend_used="direct",
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
    assert "bis 19.07.2099 23:17" in output
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
                backend_configured="direct",
                backend_used="direct",
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


def test_account_overview_json_hides_login_required_values(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    def fake_fetch_all(config, accounts, **kwargs):
        account = next(iter(accounts))
        return [
            AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=datetime(2026, 6, 8, 3, 30, tzinfo=ZoneInfo("Europe/Berlin")),
                backend_configured="direct",
                backend_used="direct",
                status=AccountStatus.LOGIN_REQUIRED,
                five_hour=LimitWindow(name="5h", remaining=97),
                weekly=LimitWindow(name="weekly", remaining=55),
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
        ]
    ) == 0
    capsys.readouterr()

    assert main(
        ["--config", str(config_path), "account", "overview", "--format", "json"]
    ) == 2

    usage = json.loads(capsys.readouterr().out)["accounts"][0]["usage"]
    assert usage["status"] == "login_required"
    assert usage["five_hour"] is None
    assert usage["weekly"] is None


def test_overview_usage_json_uses_safe_serialized_metadata():
    usage = AccountUsage(
        account_id="privat",
        label="Privat",
        captured_at=[],  # type: ignore[arg-type]
        backend_configured="direct",
        backend_used="direct",
    )

    payload = _overview_usage_json(usage, expected_backend="direct")

    assert payload is not None
    assert payload["captured_at"] is None
    assert payload["status"] == "ok"
    assert payload["stale"] is False


def test_values_shows_compact_live_values_for_all_accounts(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    def fake_fetch_all(config, accounts, **kwargs):
        return [
            AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=datetime(2026, 6, 8, 3, 30, tzinfo=ZoneInfo("Europe/Berlin")),
                backend_configured="direct",
                backend_used="direct",
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
    reset = (datetime.now().astimezone() + timedelta(hours=4)).strftime(
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


def test_latest_fails_closed_when_account_snapshot_is_missing(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"

    assert main(["--config", str(config_path), "account", "add", "private"]) == 0
    capsys.readouterr()

    assert main(["--config", str(config_path), "latest"]) == 2
    assert "Keine Snapshots vorhanden." in capsys.readouterr().out


def test_ingest_binds_shared_user_browser_payload_to_selected_account(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    shared_user = "shared-user"

    def write_auth(path, account_id):
        header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=")
        claims = json.dumps(
            {"https://api.openai.com/auth": {"chatgpt_user_id": shared_user}}
        ).encode("utf-8")
        encoded_claims = base64.urlsafe_b64encode(claims).rstrip(b"=")
        token = f"{header.decode()}.{encoded_claims.decode()}.signature"
        path.write_text(
            json.dumps(
                {
                    "tokens": {
                        "access_token": token,
                        "id_token": token,
                        "account_id": account_id,
                    }
                }
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)

    privat_auth = tmp_path / "privat-auth.json"
    work_auth = tmp_path / "work-auth.json"
    write_auth(privat_auth, "privat-account")
    write_auth(work_auth, "work-account")
    assert main(
        [
            "--config",
            str(config_path),
            "account",
            "add",
            "privat",
            "--auth-json",
            str(privat_auth),
        ]
    ) == 0
    capsys.readouterr()
    assert main(
        [
            "--config",
            str(config_path),
            "account",
            "add",
            "work",
            "--auth-json",
            str(work_auth),
        ]
    ) == 0
    capsys.readouterr()

    payload = {
        "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json",
                    "ok": True,
                    "truncated": False,
                    "bodyText": json.dumps(
                    {
                        "user_id": shared_user,
                        "account_id": shared_user,
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 3,
                                "limit_window_seconds": 18000,
                            },
                            "secondary_window": {
                                "used_percent": 45,
                                "limit_window_seconds": 604800,
                            },
                        },
                    }
                ),
            }
        ]
    }
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))

    assert main(["--config", str(config_path), "ingest", "privat", "--stdin"]) == 1
    assert "ambiguous account identity" in capsys.readouterr().err
    assert load_latest_usages(load_config(config_path))[0:] == []


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
            backend_configured="direct",
            backend_used="direct",
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
            backend_configured="direct",
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


def test_bridge_snippet_command_accepts_absolute_https_endpoint(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()

    assert main(
        [
            "--config",
            str(config_path),
            "bridge-snippet",
            "privat",
            "--endpoint",
            "https://bridge.example.test:8765/ingest",
        ]
    ) == 0

    assert "https://bridge.example.test:8765/ingest" in capsys.readouterr().out


def test_bridge_snippet_command_rejects_malformed_endpoint(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()

    assert main(
        [
            "--config",
            str(config_path),
            "bridge-snippet",
            "privat",
            "--endpoint",
            "https://bridge.example.test:0/ingest",
        ]
    ) == 1

    assert "absolute HTTP(S) URL" in capsys.readouterr().err


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


def test_account_delete_supports_structured_json_output(tmp_path, capsys):
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
                "account",
                "delete",
                "privat",
                "--format",
                "json",
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "account": "privat",
        "label": "BW_Privat",
        "profile_deleted": False,
    }


def test_account_delete_rejects_parallel_replacement(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    add_or_update_account(
        "same",
        label="Old",
        profile_dir=str(tmp_path / "old-profile"),
        path=config_path,
    )
    original_remove = cli_module.remove_account

    def replace_then_remove(account_ref, *, path=None, **kwargs):
        save_config(
            AppConfig(
                accounts=(
                    Account(
                        id="same",
                        label="New",
                        profile_dir=str(tmp_path / "new-profile"),
                    ),
                ),
                interval_seconds=300,
            ),
            path,
        )
        return original_remove(account_ref, path=path, **kwargs)

    monkeypatch.setattr(cli_module, "remove_account", replace_then_remove)

    assert main(["--config", str(config_path), "account", "delete", "same"]) == 1
    capsys.readouterr()

    assert load_config(config_path).accounts[0].label == "New"


def test_account_delete_holds_all_accounts_lock_during_cleanup(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()

    def assert_transaction_lock(_account_id, **_kwargs):
        with pytest.raises(AccountLockError):
            with account_lock("__all_accounts__", timeout_seconds=0):
                pass

    monkeypatch.setattr(cli_module, "remove_account_state", assert_transaction_lock)
    monkeypatch.setattr(cli_module, "revoke_bridge_token", lambda _account_id: None)

    assert main(["--config", str(config_path), "account", "delete", "privat"]) == 0
    capsys.readouterr()


def test_account_delete_serializes_reactivation_account_lock(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    profile_dir = tmp_path / "profile"
    assert main(
        [
            "--config", str(config_path), "account", "add", "privat",
            "--profile-dir", str(profile_dir),
        ]
    ) == 0
    capsys.readouterr()

    def reactivation_attempt(*_args, **_kwargs):
        with account_lock("privat", timeout_seconds=0):
            pass
        return "geloescht"

    monkeypatch.setattr(cli_module, "_delete_profile_dir", reactivation_attempt)
    monkeypatch.setattr(cli_module, "revoke_bridge_token", lambda _account_id: None)

    assert main(
        [
            "--config", str(config_path), "account", "delete", "privat",
            "--delete-profile",
        ]
    ) == 1
    assert "already running" in capsys.readouterr().err
    assert load_config(config_path).accounts[0].id == "privat"
    assert profile_dir.is_dir()


def test_account_delete_keeps_config_when_profile_cleanup_fails(
    tmp_path, monkeypatch, capsys
):
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

    def fail_profile_cleanup(*_args, **_kwargs):
        raise OSError("profile cleanup failed")

    monkeypatch.setattr("codex_usage.cli._delete_profile_dir", fail_profile_cleanup)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "delete",
                "privat",
                "--delete-profile",
            ]
        )
        == 1
    )
    capsys.readouterr()

    assert load_config(config_path).accounts[0].id == "privat"
    assert profile_dir.is_dir()


def test_profile_delete_uses_normalized_path_for_lock_and_quarantine(
    tmp_path, monkeypatch
):
    profile_root = tmp_path / "profiles"
    profile_root.mkdir()
    alias_parent = profile_root / "alias-parent"
    alias_parent.mkdir()
    profile_dir = profile_root / "privat"
    profile_dir.mkdir(mode=0o700)
    (profile_dir / ".codex-usage-profile").write_text("marker\n", encoding="utf-8")
    alias_path = alias_parent / ".." / "privat"
    seen = []
    original_lock_targets = cli_module._profile_delete_lock_targets

    def capture_lock_targets(path, *, browser):
        seen.append(path)
        return original_lock_targets(path, browser=browser)

    monkeypatch.setattr(
        cli_module,
        "_profile_delete_lock_targets",
        capture_lock_targets,
    )

    assert cli_module._delete_profile_dir(
        alias_path,
        browser="firefox",
        force=False,
    ) == "geloescht"

    assert seen == [profile_dir.resolve()]
    assert not profile_dir.exists()


def test_account_delete_profile_failure_preserves_state_and_bridge_token(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
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
    token = bridge_token_for_account("privat")
    save_current_usage(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime.now().astimezone(),
            five_hour=LimitWindow(name="5h", remaining=12),
            weekly=LimitWindow(name="weekly", remaining=34),
        )
    )
    state_path = tmp_path / "data" / "codex-usage" / "current" / "privat.json"

    def fail_profile_cleanup(*_args, **_kwargs):
        raise OSError("profile cleanup failed")

    monkeypatch.setattr("codex_usage.cli._delete_profile_dir", fail_profile_cleanup)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "delete",
                "privat",
                "--delete-profile",
            ]
        )
        == 1
    )
    capsys.readouterr()

    assert state_path.exists()
    assert bridge_token_for_account("privat") == token


def test_account_delete_keeps_config_when_config_delete_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
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
    token = bridge_token_for_account("privat")
    save_current_usage(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime.now().astimezone(),
            five_hour=LimitWindow(name="5h", remaining=12),
            weekly=LimitWindow(name="weekly", remaining=34),
        )
    )
    def fail_remove(*_args, **_kwargs):
        raise OSError("config delete failed")

    monkeypatch.setattr("codex_usage.cli.remove_account", fail_remove)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "delete",
                "privat",
                "--delete-profile",
            ]
        )
        == 1
    )
    capsys.readouterr()
    assert load_config(config_path).accounts[0].id == "privat"
    assert (tmp_path / "data" / "codex-usage" / "current" / "privat.json").is_file()
    assert bridge_token_for_account("privat") == token
    assert profile_dir.is_dir()


def test_account_delete_syncs_managed_service_with_updated_config(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    synced = []
    monkeypatch.setattr("codex_usage.cli.service_status", lambda: {"installed": True})
    monkeypatch.setattr(
        "codex_usage.cli.managed_service_config_path",
        lambda: config_path.absolute(),
    )
    monkeypatch.setattr(
        "codex_usage.cli.service_install",
        lambda config, path: synced.append((config, path)),
    )

    assert main(["--config", str(config_path), "account", "delete", "privat"]) == 0
    capsys.readouterr()

    assert len(synced) == 1
    assert synced[0][0].accounts == ()
    assert synced[0][1] == config_path
    assert load_config(config_path).accounts == ()


def test_account_delete_cleanup_failure_rolls_back_managed_service_config(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    synced = []
    monkeypatch.setattr("codex_usage.cli.service_status", lambda: {"installed": True})
    monkeypatch.setattr(
        "codex_usage.cli.managed_service_config_path",
        lambda: config_path.absolute(),
    )
    monkeypatch.setattr(
        "codex_usage.cli.service_install",
        lambda config, path: synced.append((config, path)),
    )
    def fail_state_cleanup(_account_id, **_kwargs):
        raise OSError("state cleanup failed")

    monkeypatch.setattr("codex_usage.cli.remove_account_state", fail_state_cleanup)
    monkeypatch.setattr("codex_usage.cli.revoke_bridge_token", lambda _account_id: None)

    assert main(["--config", str(config_path), "account", "delete", "privat"]) == 1
    capsys.readouterr()

    assert load_config(config_path).accounts[0].id == "privat"
    assert [config.accounts for config, _path in synced] == [(), load_config(config_path).accounts]


def test_account_delete_state_failure_restores_profile_when_profile_delete_requested(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
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
    profile_dir.mkdir(exist_ok=True)
    (profile_dir / ".codex-usage-profile").write_text("marker\n", encoding="utf-8")

    def fail_state_cleanup(_account_id, **_kwargs):
        raise OSError("state cleanup failed")

    monkeypatch.setattr("codex_usage.cli.remove_account_state", fail_state_cleanup)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "delete",
                "privat",
                "--delete-profile",
            ]
        )
        == 1
    )
    capsys.readouterr()

    assert load_config(config_path).accounts[0].id == "privat"
    assert profile_dir.is_dir()
    assert (profile_dir / ".codex-usage-profile").read_text(encoding="utf-8") == "marker\n"


def test_account_delete_cancels_active_profile_jobs_before_cleanup(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    cancelled = []
    job = {"job_id": "job-" + "a" * 32, "status": "running"}

    monkeypatch.setattr("codex_usage.cli.list_profile_jobs", lambda account_id: [job])
    monkeypatch.setattr(
        "codex_usage.cli.cancel_profile_job",
        lambda job_id: cancelled.append(job_id) or {"job_id": job_id, "status": "cancel_requested"},
    )
    monkeypatch.setattr(
        "codex_usage.cli.profile_job_status",
        lambda job_id: {"job_id": job_id, "status": "cancelled", "ok": False},
    )

    assert main(["--config", str(config_path), "account", "delete", "privat"]) == 0

    assert cancelled == [job["job_id"]]


def test_account_delete_aborts_when_profile_job_does_not_stop(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    job_id = "job-" + "b" * 32
    clock = iter((0.0, 31.0))

    monkeypatch.setattr(
        "codex_usage.cli.list_profile_jobs",
        lambda account_id: [{"job_id": job_id, "status": "running"}],
    )
    monkeypatch.setattr("codex_usage.cli.cancel_profile_job", lambda value: {"job_id": value})
    monkeypatch.setattr(
        "codex_usage.cli.profile_job_status",
        lambda value: {"job_id": value, "status": "running"},
    )
    monkeypatch.setattr("codex_usage.cli.time.monotonic", lambda: next(clock))

    assert main(["--config", str(config_path), "account", "delete", "privat"]) == 1
    capsys.readouterr()

    assert load_config(config_path).accounts[0].id == "privat"


def test_account_delete_profile_commit_failure_restores_profile(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
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
    (profile_dir / ".codex-usage-profile").write_text("marker\n", encoding="utf-8")

    def fail_profile_commit(*_args, **_kwargs):
        raise OSError("profile commit failed")

    monkeypatch.setattr("codex_usage.cli.shutil.rmtree", fail_profile_commit)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "delete",
                "privat",
                "--delete-profile",
            ]
        )
        == 1
    )
    capsys.readouterr()

    assert load_config(config_path).accounts[0].id == "privat"
    assert profile_dir.is_dir()
    assert (profile_dir / ".codex-usage-profile").read_text(encoding="utf-8") == "marker\n"


def test_account_delete_does_not_restore_partially_deleted_profile(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
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
    marker = profile_dir / ".codex-usage-profile"
    marker.write_text("marker\n", encoding="utf-8")
    original_rmtree = cli_module.shutil.rmtree
    rmtree_calls: list[tuple[str, bool]] = []

    def partially_fail_profile_commit(path, *args, onerror=None, **kwargs):
        rmtree_calls.append((Path(path).name, onerror is not None))
        if not Path(path).name.startswith(".profile.delete-"):
            return original_rmtree(path, *args, onerror=onerror, **kwargs)
        quarantined_marker = Path(path) / marker.name
        quarantined_marker.unlink()
        error = OSError("partial profile commit failed")
        if onerror is None:
            raise error
        onerror(Path.unlink, str(quarantined_marker), (OSError, error, None))

    monkeypatch.setattr(
        "codex_usage.cli.shutil.rmtree",
        partially_fail_profile_commit,
    )

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "delete",
                "privat",
                "--delete-profile",
            ]
        )
        == 1
    )
    error = capsys.readouterr().err

    assert rmtree_calls, error
    assert rmtree_calls == [(rmtree_calls[0][0], True)]
    assert rmtree_calls[0][0].startswith(".profile.delete-")
    assert load_config(config_path).accounts[0].id == "privat"
    assert not profile_dir.exists()
    quarantines = list(tmp_path.glob(".profile.delete-*"))
    assert len(quarantines) == 1
    assert quarantines[0].is_dir()
    assert "profile deletion rollback failed" in error


def test_account_delete_rolls_back_config_when_managed_service_sync_fails(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    monkeypatch.setattr("codex_usage.cli.service_status", lambda: {"installed": True})
    monkeypatch.setattr(
        "codex_usage.cli.managed_service_config_path",
        lambda: config_path.absolute(),
    )

    def fail_service_sync(*_args, **_kwargs):
        raise OSError("service sync failed")

    monkeypatch.setattr("codex_usage.cli.service_install", fail_service_sync)

    assert main(["--config", str(config_path), "account", "delete", "privat"]) == 1
    capsys.readouterr()

    assert load_config(config_path).accounts[0].id == "privat"


def test_account_delete_service_sync_failure_preserves_all_account_data(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    profile_dir = tmp_path / "profile"

    assert main(
        [
            "--config",
            str(config_path),
            "account",
            "add",
            "privat",
            "--profile-dir",
            str(profile_dir),
        ]
    ) == 0
    capsys.readouterr()
    token = bridge_token_for_account("privat")
    state_dir = tmp_path / "data" / "codex-usage"
    current_path = state_dir / "current" / "privat.json"
    snapshot_path = state_dir / "snapshots" / "privat.json"
    current_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text("current\n", encoding="utf-8")
    snapshot_path.write_text("snapshot\n", encoding="utf-8")

    monkeypatch.setattr("codex_usage.cli.service_status", lambda: {"installed": True})
    monkeypatch.setattr(
        "codex_usage.cli.managed_service_config_path",
        lambda: config_path.absolute(),
    )

    def fail_service_sync(*_args, **_kwargs):
        raise OSError("service sync failed")

    monkeypatch.setattr("codex_usage.cli.service_install", fail_service_sync)

    assert main(
        [
            "--config",
            str(config_path),
            "account",
            "delete",
            "privat",
            "--delete-profile",
        ]
    ) == 1
    capsys.readouterr()

    assert load_config(config_path).accounts[0].id == "privat"
    assert current_path.read_text(encoding="utf-8") == "current\n"
    assert snapshot_path.read_text(encoding="utf-8") == "snapshot\n"
    assert bridge_token_for_account("privat") == token
    assert profile_dir.is_dir()


def test_account_delete_revokes_token_when_state_cleanup_fails(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    revoked = []

    def fail_state_cleanup(*_args, **_kwargs):
        raise OSError("state cleanup failed")

    monkeypatch.setattr("codex_usage.cli.remove_account_state", fail_state_cleanup)
    monkeypatch.setattr(
        "codex_usage.cli.revoke_bridge_token",
        lambda account_id: revoked.append(account_id),
    )

    assert main(["--config", str(config_path), "account", "delete", "privat"]) == 1
    assert revoked == ["privat"]
    assert load_config(config_path).accounts[0].id == "privat"


def test_account_delete_token_failure_restores_staged_state(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    save_current_usage(
        AccountUsage(
            account_id="privat",
            label="Privat",
            captured_at=datetime.now().astimezone(),
            five_hour=LimitWindow(name="5h", remaining=12),
        )
    )
    state_path = tmp_path / "data" / "codex-usage" / "current" / "privat.json"
    before = state_path.read_bytes()

    def fail_token_revoke(_account_id):
        raise OSError("token revoke failed")

    monkeypatch.setattr(cli_module, "revoke_bridge_token", fail_token_revoke)

    assert main(["--config", str(config_path), "account", "delete", "privat"]) == 1
    capsys.readouterr()

    assert load_config(config_path).accounts[0].id == "privat"
    assert state_path.read_bytes() == before


def test_account_delete_preserves_state_error_when_token_revoke_also_fails(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.toml"
    assert main(["--config", str(config_path), "account", "add", "privat"]) == 0
    capsys.readouterr()
    revoked = []

    def fail_state_cleanup(*_args, **_kwargs):
        raise OSError("state cleanup failed")

    def fail_token_revoke(account_id):
        revoked.append(account_id)
        raise OSError("token revoke failed")

    monkeypatch.setattr("codex_usage.cli.remove_account_state", fail_state_cleanup)
    monkeypatch.setattr("codex_usage.cli.revoke_bridge_token", fail_token_revoke)

    assert main(["--config", str(config_path), "account", "delete", "privat"]) == 1
    error = capsys.readouterr().err
    assert "state cleanup failed" in error
    assert "token revoke failed" not in error
    assert revoked == ["privat"]


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


def test_account_delete_refuses_profile_removal_while_browser_lock_held(
    tmp_path, capsys
):
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
                "--browser",
                "chromium",
            ]
        )
        == 0
    )
    capsys.readouterr()
    browser_profile = profile_dir / _profile_browser_dir("chromium")
    browser_profile.mkdir()

    with _profile_lock(browser_profile):
        assert (
            main(
                [
                    "--config",
                    str(config_path),
                    "account",
                    "delete",
                    "privat",
                    "--delete-profile",
                ]
            )
            == 1
        )
        assert "profile is already in use" in capsys.readouterr().err
        assert profile_dir.is_dir()
        assert load_config(config_path).accounts[0].id == "privat"

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "delete",
                "privat",
                "--delete-profile",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert not profile_dir.exists()
    assert list(profile_dir.parent.glob(".*.codex-usage.lock"))


def test_account_delete_oauth_profile_lock_refuses_removal_while_held(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
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
                "--browser",
                "chromium",
            ]
        )
        == 0
    )
    capsys.readouterr()
    oauth_profile = profile_dir / "oauth" / "chromium"
    oauth_profile.mkdir(parents=True)

    with _profile_lock(oauth_profile):
        assert (
            main(
                [
                    "--config",
                    str(config_path),
                    "account",
                    "delete",
                    "privat",
                    "--delete-profile",
                ]
            )
            == 1
        )
        assert "profile is already in use" in capsys.readouterr().err
        assert profile_dir.is_dir()
        assert load_config(config_path).accounts[0].id == "privat"

    assert (
        main(
            [
                "--config",
                str(config_path),
                "account",
                "delete",
                "privat",
                "--delete-profile",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert not profile_dir.exists()
    assert list(profile_dir.parent.glob(".*.codex-usage.lock"))


def test_profile_delete_rejects_too_many_oauth_entries(tmp_path, monkeypatch):
    profile_dir = tmp_path / "profile"
    oauth_root = profile_dir / "oauth"
    (oauth_root / "chromium").mkdir(parents=True)
    (oauth_root / "firefox").mkdir()
    monkeypatch.setattr(cli_module, "MAX_PROFILE_OAUTH_ENTRIES", 1)

    with pytest.raises(ValueError, match="too many OAuth browser profiles"):
        cli_module._profile_delete_lock_targets(profile_dir, browser="chromium")


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
    config_path.chmod(0o600)

    assert (
        main(["--config", str(config_path), "account", "delete", "privat", "--delete-profile"])
        == 1
    )

    assert profile_link.is_symlink()
    assert target.is_dir()
    assert "privat" in config_path.read_text(encoding="utf-8")
    assert "symlink" in capsys.readouterr().err


def test_account_delete_rejects_symlink_profile_ancestor_and_keeps_config(
    tmp_path, capsys
):
    config_path = tmp_path / "config.toml"
    target_parent = tmp_path / "target-parent"
    target_profile = target_parent / "profile"
    target_profile.mkdir(parents=True)
    (target_profile / ".codex-usage-profile").write_text("marker\n", encoding="utf-8")
    profile_parent_link = tmp_path / "profile-parent-link"
    profile_parent_link.symlink_to(target_parent, target_is_directory=True)
    profile_path = profile_parent_link / "profile"
    config_path.write_text(
        "\n".join(
            (
                "[[accounts]]",
                'id = "privat"',
                'label = "BW_Privat"',
                f'profile_dir = "{profile_path}"',
                'browser = "firefox"',
            )
        )
        + "\n",
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    assert (
        main(["--config", str(config_path), "account", "delete", "privat", "--delete-profile"])
        == 1
    )

    assert target_profile.is_dir()
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
        == 2
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


@pytest.mark.parametrize("error", (KeyboardInterrupt(), KeyError(), KeyError("bad key")))
def test_main_maps_interrupt_and_key_errors(monkeypatch, error, capsys):
    class Parser:
        def parse_args(self, _argv):
            def fail(_args):
                raise error

            return SimpleNamespace(func=fail)

    monkeypatch.setattr(cli_module, "_build_parser", lambda: Parser())

    result = main(["once"])

    assert result == (130 if isinstance(error, KeyboardInterrupt) else 1)
    if isinstance(error, KeyError):
        assert "Fehler:" in capsys.readouterr().err


def test_account_backend_table_output(monkeypatch, capsys):
    current = Account(id="alpha", label="Alpha", profile_dir="/tmp/alpha")
    updated = Account(
        id=current.id,
        label=current.label,
        profile_dir=current.profile_dir,
        backend="app-server",
    )
    monkeypatch.setattr(cli_module, "load_config", lambda _path: object())
    monkeypatch.setattr(cli_module, "resolve_account", lambda *_args: current)
    monkeypatch.setattr(
        cli_module,
        "add_or_update_account",
        lambda *_args, **_kwargs: (object(), updated),
    )

    result = cli_module._cmd_account_backend(
        SimpleNamespace(
            config=None,
            account="alpha",
            backend="app-server",
            format="table",
        )
    )

    assert result == 0
    assert "Abrufweg gespeichert: alpha (Alpha) -> app-server" in capsys.readouterr().out


def test_cancel_account_profile_jobs_rejects_invalid_id_and_times_out(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "list_profile_jobs",
        lambda _account: [{"job_id": 1, "status": "running"}],
    )
    with pytest.raises(ValueError, match="profile job id"):
        cli_module._cancel_account_profile_jobs("alpha")

    monkeypatch.setattr(
        cli_module,
        "list_profile_jobs",
        lambda _account: [{"job_id": "job-1", "status": "running"}],
    )
    monkeypatch.setattr(cli_module, "cancel_profile_job", lambda _job_id: None)
    monkeypatch.setattr(cli_module, "profile_job_status", lambda _job_id: {"status": "running"})
    monkeypatch.setattr(cli_module, "ACCOUNT_DELETE_PROFILE_JOB_TIMEOUT_SECONDS", 1)
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(cli_module.time, "monotonic", lambda: next(clock))

    with pytest.raises(ValueError, match="did not stop"):
        cli_module._cancel_account_profile_jobs("alpha")

    monkeypatch.setattr(
        cli_module,
        "profile_job_status",
        lambda _job_id: {"status": "completed"},
    )
    clock = iter((0.0, 0.0, 0.0))
    monkeypatch.setattr(cli_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(cli_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(cli_module, "ACCOUNT_DELETE_PROFILE_JOB_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(
        cli_module,
        "list_profile_jobs",
        lambda _account: [{"job_id": "job-2", "status": "running"}],
    )
    cli_module._cancel_account_profile_jobs("alpha")


def test_history_commands_cover_table_query_and_prune(monkeypatch, capsys):
    sample = SimpleNamespace(
        account_id="alpha",
        pool="main",
        window_seconds=18_000,
        captured_at=datetime(2026, 8, 16, 10, tzinfo=ZoneInfo("Europe/Berlin")),
        used_percent=12.5,
        reset_at=None,
        reset_generation=None,
        source="test",
    )

    class Store:
        def __init__(self, _path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def status(self):
            return {"path": "/tmp/history.sqlite3", "sample_count": 1}

        def samples(self, *_args, **_kwargs):
            return [sample]

        def prune(self, _before, *, dry_run):
            return 2 if dry_run else 1

    monkeypatch.setattr(cli_module, "HistoryStore", Store)
    assert cli_module._cmd_history_status(SimpleNamespace(path=Path("history"), format="table")) == 0
    assert "Samples: 1" in capsys.readouterr().out

    query_args = SimpleNamespace(
        path=Path("history"),
        account="alpha",
        pool="main",
        window_seconds=18_000,
        since="2026-08-15T10:00:00Z",
        until="2026-08-17T10:00:00+00:00",
        format="table",
    )
    assert cli_module._cmd_history_query(query_args) == 0
    assert "12.500%" in capsys.readouterr().out

    for dry_run, expected in ((True, "würden entfernt"), (False, "entfernt")):
        prune_args = SimpleNamespace(
            path=Path("history"),
            dry_run=dry_run,
            apply=not dry_run,
            days=30,
            before=None,
            format="table",
        )
        assert cli_module._cmd_history_prune(prune_args) == 0
        assert expected in capsys.readouterr().out

    with pytest.raises(ValueError, match="exactly one"):
        cli_module._cmd_history_prune(
            SimpleNamespace(
                path=Path("history"),
                dry_run=False,
                apply=False,
                days=30,
                before=None,
                format="json",
            )
        )


def test_parse_history_datetime_rejects_invalid_timezone_and_overflow():
    for value in (None, "not-a-date", "2026-08-16T10:00:00"):
        with pytest.raises(ValueError):
            cli_module._parse_history_datetime(value, "value")  # type: ignore[arg-type]


def test_consumption_command_covers_all_windows_baselines_and_table(monkeypatch, capsys):
    class Store:
        def __init__(self, _path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def consumption_window_seconds(self, *_args, **_kwargs):
            return (604800,)

        def samples_for_consumption(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(cli_module, "HistoryStore", Store)
    monkeypatch.setattr(cli_module, "consumption_lookback_seconds", lambda *_args: 3600)
    monkeypatch.setattr(
        cli_module,
        "calculate_consumption",
        lambda *_args, **_kwargs: cli_module.ConsumptionWindow(
            lookback_seconds=3600,
            pool="main",
            limit_window_seconds=0,
            consumed_percentage_points=1.5,
            coverage="partial",
            sample_count=1,
            estimated_seconds_to_exhaustion=None,
            baseline_used_percent=None,
        ),
    )
    args = SimpleNamespace(
        now="2026-08-16T10:00:00Z",
        limit_window="all",
        account="alpha",
        pool="main",
        amount=1,
        unit="hours",
        baseline_minutes=10,
        baseline_value_minutes=5,
        smoothing=1,
        path=Path("history"),
        format="table",
    )

    assert cli_module._cmd_consumption(args) == 0
    output = capsys.readouterr().out
    assert "main/18000s" in output
    assert "main/604800s" in output

    for field in ("baseline_minutes", "baseline_value_minutes"):
        invalid = SimpleNamespace(**vars(args))
        setattr(invalid, field, 10_000)
        with pytest.raises(ValueError, match="between 0 and 9999"):
            cli_module._cmd_consumption(invalid)


def test_history_json_output_and_profile_create_optional_fields(monkeypatch, capsys):
    class Store:
        def __init__(self, _path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def samples(self, *_args, **_kwargs):
            return []

        def prune(self, *_args, **_kwargs):
            return 0

    monkeypatch.setattr(cli_module, "HistoryStore", Store)
    assert cli_module._cmd_history_query(
        SimpleNamespace(
            path=Path("history"), account="alpha", pool=None, window_seconds=None,
            since=None, until=None, format="json",
        )
    ) == 0
    assert json.loads(capsys.readouterr().out)["samples"] == []
    assert cli_module._cmd_history_prune(
        SimpleNamespace(
            path=Path("history"), dry_run=True, apply=False, days=30,
            before="2026-08-16T10:00:00Z", format="json",
        )
    ) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True

    captured = {}
    monkeypatch.setattr(cli_module, "account_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(
        cli_module,
        "create_profile_job",
        lambda **kwargs: captured.update(kwargs) or {"ok": True},
    )
    assert cli_module._cmd_profile_create(
        SimpleNamespace(
            account_id="new", label="New", browser="firefox", backend="direct",
            profile_dir=None, reactivation_browser="auto", expected_backend_account_id=None,
            config=None, json_events=False, tag="tag", series="pool", series_active=True,
        )
    ) == 0
    capsys.readouterr()
    assert captured["tag"] == "tag"
    assert captured["series"] == "pool"
    assert captured["series_active"] is True


def test_reactivation_and_account_handlers_print_error_tables(monkeypatch, capsys):
    account = Account(id="alpha", label="Alpha", profile_dir="/tmp/alpha")
    monkeypatch.setattr(cli_module, "load_config", lambda _path: object())
    monkeypatch.setattr(cli_module, "resolve_account", lambda *_args: account)
    monkeypatch.setattr(
        cli_module,
        "reactivate_account",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(cli_module.ReactivationError("bad")),
    )
    assert cli_module._cmd_reactivate(
        SimpleNamespace(config=None, account="alpha", browser="auto", format="table")
    ) == 2
    assert "Reaktivierung fehlgeschlagen" in capsys.readouterr().out
    monkeypatch.setattr(
        cli_module,
        "open_account_in_reactivation_browser",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(cli_module.ReactivationError("bad")),
    )
    assert cli_module._cmd_account_manage(
        SimpleNamespace(config=None, account="alpha", browser=None, format="table")
    ) == 2
    assert "konnte nicht geöffnet" in capsys.readouterr().out
    monkeypatch.setattr(
        cli_module,
        "start_account_terminal",
        lambda _account: (_ for _ in ()).throw(cli_module.TerminalError("bad")),
    )
    assert cli_module._cmd_account_terminal(
        SimpleNamespace(config=None, account="alpha", format="table")
    ) == 2
    assert "konnte nicht gestartet" in capsys.readouterr().out


def test_probe_diagnose_latest_health_service_and_paths(monkeypatch, capsys, tmp_path):
    account = Account(id="alpha", label="Alpha", profile_dir="/tmp/alpha")
    config = SimpleNamespace(accounts=(account,))
    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "resolve_account", lambda *_args: account)
    monkeypatch.setattr(cli_module, "probe_account", lambda *_args, **kwargs: {"headed": kwargs["headed"]})
    assert cli_module._cmd_probe(
        SimpleNamespace(config=None, account="alpha", headless=True, save_dir=None)
    ) == 0
    assert json.loads(capsys.readouterr().out)["headed"] is False

    called = {}
    monkeypatch.setattr(
        cli_module,
        "diagnose_account",
        lambda *_args, **kwargs: called.update(kwargs) or {"ok": True},
    )
    assert cli_module._cmd_diagnose(
        SimpleNamespace(config=None, account="alpha", headed=True, screenshot=True, save_dir=None, auth_json=None)
    ) == 0
    assert called["screenshot_dir"] == Path("diagnose-output")
    assert called["auth_json_path"] is None
    capsys.readouterr()
    monkeypatch.setattr(cli_module, "diagnose_account", lambda *_args, **_kwargs: {"error": "bad"})
    assert cli_module._cmd_diagnose(
        SimpleNamespace(config=None, account="alpha", headed=False, screenshot=False, save_dir=tmp_path, auth_json=None)
    ) == 2
    capsys.readouterr()

    monkeypatch.setattr(cli_module, "load_latest_usages", lambda _config: [])
    assert cli_module._cmd_latest(SimpleNamespace(config=None, format="table")) == 2
    assert "Keine Snapshots" in capsys.readouterr().out
    monkeypatch.setattr(cli_module, "render_json", lambda _usages: "{}")
    monkeypatch.setattr(cli_module, "load_latest_usages", lambda _config: [object()])
    monkeypatch.setattr(cli_module, "_all_usage_results_valid", lambda *_args, **_kwargs: True)
    assert cli_module._cmd_latest(SimpleNamespace(config=None, format="json")) == 0
    assert capsys.readouterr().out.strip() == "{}"

    monkeypatch.setattr(
        cli_module,
        "load_health",
        lambda: {"event_count": 1, "event_counts": {"watch:cycle": 1}},
    )
    assert cli_module._cmd_health(
        SimpleNamespace(
            record_component=None, record_event=None, clear=False, account=None,
            duration_ms=None, error_class=None, format="table",
        )
    ) == 0
    assert "watch:cycle: 1" in capsys.readouterr().out
    with pytest.raises(ValueError, match="requires component"):
        cli_module._cmd_health(
            SimpleNamespace(
                record_component="watch", record_event=None, clear=False, account=None,
                duration_ms=None, error_class=None, format="json",
            )
        )

    service_calls = []
    monkeypatch.setattr(cli_module, "service_status", lambda: {"installed": True, "enabled": False, "active": True})
    monkeypatch.setattr(cli_module, "service_disable", lambda: service_calls.append("disable") or {"installed": True})
    monkeypatch.setattr(cli_module, "service_uninstall", lambda: service_calls.append("uninstall") or {"installed": False})
    monkeypatch.setattr(cli_module, "service_install", lambda *_args: service_calls.append("install") or {"installed": True})
    monkeypatch.setattr(cli_module, "service_enable", lambda *_args: service_calls.append("enable") or {"installed": True})
    monkeypatch.setattr(cli_module, "render_service_json", lambda result: json.dumps(result))
    for action in ("status", "disable", "uninstall", "install", "enable"):
        args = SimpleNamespace(action=action, config=None, format="json")
        assert cli_module._cmd_service(args) == 0
        capsys.readouterr()
    assert service_calls == ["disable", "uninstall", "install", "enable"]
    assert cli_module._cmd_paths(SimpleNamespace(config=tmp_path / "config.toml")) == 0
    assert "config:" in capsys.readouterr().out


def test_bridge_server_validation_and_service_sync_guards(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli_module, "load_config", lambda _path: object())
    monkeypatch.setattr(cli_module, "run_bridge_server", lambda *_args, **_kwargs: None)
    base = dict(
        config=tmp_path / "config.toml", port=8787, host="127.0.0.1", allow_remote=False,
        tls_cert=None, tls_key=None,
    )
    with pytest.raises(ValueError, match="TLS requires"):
        cli_module._cmd_bridge_server(SimpleNamespace(**{**base, "tls_cert": Path("cert")}))
    with pytest.raises(ValueError, match="remote bridge"):
        cli_module._cmd_bridge_server(SimpleNamespace(**{**base, "host": "0.0.0.0", "allow_remote": True}))
    assert cli_module._cmd_bridge_server(
        SimpleNamespace(**{**base, "host": "0.0.0.0", "allow_remote": True, "tls_cert": Path("cert"), "tls_key": Path("key")})
    ) == 0

    monkeypatch.setattr(cli_module, "service_status", lambda: {"installed": False})
    assert cli_module._managed_service_sync_required(tmp_path / "config.toml") is False
    monkeypatch.setattr(cli_module, "service_status", lambda: (_ for _ in ()).throw(OSError("unknown")))
    assert cli_module._managed_service_sync_required(tmp_path / "config.toml") is True
    monkeypatch.setattr(cli_module, "service_status", lambda: {"installed": True})
    monkeypatch.setattr(cli_module, "managed_service_config_path", lambda: tmp_path / "other.toml")
    assert cli_module._managed_service_sync_required(tmp_path / "config.toml") is False
    monkeypatch.setattr(cli_module, "managed_service_config_path", lambda: tmp_path / "config.toml")
    with pytest.raises(OSError):
        monkeypatch.setattr(cli_module, "service_install", lambda *_args: (_ for _ in ()).throw(OSError("sync")))
        cli_module._sync_managed_service(object(), tmp_path / "config.toml", strict=True)
    cli_module._sync_managed_service(object(), tmp_path / "config.toml", strict=False)
    assert "Warnung: systemd-Konfiguration" in capsys.readouterr().err


def test_account_delete_staging_and_rollback_error_paths(monkeypatch, tmp_path):
    account = Account(id="alpha", label="Alpha", profile_dir=str(tmp_path / "profile"))
    config = SimpleNamespace(accounts=(account,))
    args = SimpleNamespace(
        config=tmp_path / "config.toml", account="alpha", delete_profile=False,
        force_delete_profile=False, format="table",
    )
    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "resolve_account", lambda *_args: account)
    monkeypatch.setattr(cli_module, "account_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(cli_module, "profile_job_creation_lock", lambda: nullcontext())
    monkeypatch.setattr(cli_module, "_cancel_account_profile_jobs", lambda _account: None)
    monkeypatch.setattr(cli_module, "_managed_service_sync_required", lambda _path: False)
    monkeypatch.setattr(cli_module, "_sync_managed_service", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_module, "remove_account", lambda *_args, **_kwargs: (SimpleNamespace(accounts=()), None))
    monkeypatch.setattr(cli_module, "restore_account", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_module, "_delete_profile_dir", lambda *_args, **_kwargs: "fehlt")
    monkeypatch.setattr(cli_module, "_validate_profile_delete_target", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_module, "remove_account_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_module, "revoke_bridge_token", lambda _account: None)
    args.delete_profile = True
    assert cli_module._cmd_account_delete(args) == 0

    class RollbackTx:
        def __init__(self, *, commit_error=False):
            self.commit_error = commit_error

        def rollback(self):
            raise OSError("rollback failed")

        def commit(self):
            if self.commit_error:
                raise OSError("commit failed")

    args.delete_profile = False
    monkeypatch.setattr(cli_module, "remove_account_state", lambda *_args, **_kwargs: RollbackTx())
    monkeypatch.setattr(cli_module, "revoke_bridge_token", lambda _account: (_ for _ in ()).throw(OSError("revoke failed")))
    with pytest.raises(ExceptionGroup, match="state deletion rollback failed"):
        cli_module._cmd_account_delete(args)

    class ProfileTx(cli_module._ProfileDeleteTransaction):
        def __init__(self, *, commit_error=False):
            super().__init__(Path("profile"), None, "geloescht", None)
            self.commit_error = commit_error

        def rollback(self):
            raise OSError("profile rollback failed")

        def commit(self):
            if self.commit_error:
                raise OSError("profile commit failed")
            return self.profile_state

    args.delete_profile = True
    monkeypatch.setattr(cli_module, "_delete_profile_dir", lambda *_args, **_kwargs: ProfileTx())
    monkeypatch.setattr(cli_module, "remove_account_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_module, "revoke_bridge_token", lambda _account: (_ for _ in ()).throw(OSError("revoke failed")))
    with pytest.raises(ExceptionGroup, match="profile deletion rollback failed"):
        cli_module._cmd_account_delete(args)

    monkeypatch.setattr(cli_module, "revoke_bridge_token", lambda _account: None)
    monkeypatch.setattr(cli_module, "remove_account_state", lambda *_args, **_kwargs: RollbackTx(commit_error=True))
    monkeypatch.setattr(cli_module, "_delete_profile_dir", lambda *_args, **_kwargs: ProfileTx())
    with pytest.raises(ExceptionGroup, match="profile deletion rollback failed"):
        cli_module._cmd_account_delete(args)

    monkeypatch.setattr(cli_module, "remove_account_state", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("state failed")))
    monkeypatch.setattr(cli_module, "_delete_profile_dir", lambda *_args, **_kwargs: ProfileTx())
    with pytest.raises(ExceptionGroup, match="profile deletion rollback failed"):
        cli_module._cmd_account_delete(args)


def test_account_delete_restore_failures_are_reported(monkeypatch, tmp_path):
    account = Account(id="alpha", label="Alpha", profile_dir=str(tmp_path / "profile"))
    config = SimpleNamespace(accounts=(account,))
    args = SimpleNamespace(
        config=tmp_path / "config.toml", account="alpha", delete_profile=False,
        force_delete_profile=False, format="table",
    )
    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "resolve_account", lambda *_args: account)
    monkeypatch.setattr(cli_module, "account_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(cli_module, "profile_job_creation_lock", lambda: nullcontext())
    monkeypatch.setattr(cli_module, "_cancel_account_profile_jobs", lambda _account: None)
    monkeypatch.setattr(cli_module, "_managed_service_sync_required", lambda _path: False)
    monkeypatch.setattr(cli_module, "remove_account", lambda *_args, **_kwargs: (SimpleNamespace(accounts=()), None))
    monkeypatch.setattr(cli_module, "restore_account", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("restore failed")))
    monkeypatch.setattr(cli_module, "remove_account_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_module, "revoke_bridge_token", lambda _account: None)
    monkeypatch.setattr(cli_module, "_sync_managed_service", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("sync failed")))
    with pytest.raises(ValueError, match="restore account config after service sync failure"):
        cli_module._cmd_account_delete(args)

    monkeypatch.setattr(cli_module, "_sync_managed_service", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_module, "remove_account_state", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cleanup failed")))
    with pytest.raises(ValueError, match="restore account config after cleanup failure"):
        cli_module._cmd_account_delete(args)


def test_cli_policy_and_routing_helper_edges(monkeypatch, tmp_path):
    account = Account(id="alpha", label="Alpha", profile_dir="/tmp/alpha", auth_json_path="configured.json")
    no_auth_account = Account(id="beta", label="Beta", profile_dir="/tmp/beta")
    policy_config = AppConfig(accounts=(account,))

    assert cli_module._validate_single_account_auth_override(no_auth_account, Path("override.json")) is None
    monkeypatch.setattr(cli_module, "auth_identity_for_account", lambda _account: (None, None))
    with pytest.raises(ValueError, match="no canonical identity"):
        cli_module._validate_single_account_auth_override(account, Path("override.json"))
    monkeypatch.setattr(cli_module, "auth_identity_for_account", lambda _account: ("user", "account"))
    monkeypatch.setattr(cli_module, "auth_identity_from_file", lambda _path: (_ for _ in ()).throw(cli_module.DirectAuthError("bad auth")))
    assert cli_module._validate_single_account_auth_override(account, Path("override.json")) is None
    with pytest.raises(ValueError, match="cannot be combined"):
        cli_module._backend_override(SimpleNamespace(backend="app-server", direct=False, auth_json=Path("auth")))
    with pytest.raises(ValueError, match="ACCOUNT"):
        cli_module._resolve_policy_account(policy_config, None, None)

    monkeypatch.setattr(cli_module, "auth_identity_from_file", lambda _path: ("user", None))
    with pytest.raises(ValueError, match="canonical backend"):
        cli_module._resolve_policy_account(policy_config, None, Path("auth"))
    monkeypatch.setattr(cli_module, "auth_identity_from_file", lambda _path: ("user", "backend"))
    monkeypatch.setattr(cli_module, "auth_identity_for_account", lambda _account: (_ for _ in ()).throw(OSError("read")))
    with pytest.raises(ValueError, match="exactly one"):
        cli_module._resolve_policy_account(policy_config, None, Path("auth"))

    monkeypatch.setattr(cli_module, "auth_identity_for_account", lambda _account: ("user", "backend"))
    assert cli_module._resolve_policy_account(
        policy_config, "alpha", Path("auth")
    ) is account
    monkeypatch.setattr(cli_module, "resolve_account", lambda _config, _ref: no_auth_account)
    with pytest.raises(ValueError, match="different accounts"):
        cli_module._resolve_policy_account(policy_config, "beta", Path("auth"))

    monkeypatch.setattr(cli_module, "load_current_usage", lambda _id: None)
    monkeypatch.setattr(cli_module, "load_usage_snapshot", lambda _id: None)
    assert _usage_for_policy(no_auth_account).error == "no usage snapshot"
    usage = AccountUsage(
        account_id="beta", label="Beta", captured_at=datetime.now(ZoneInfo("UTC")),
        status=AccountStatus.OK, backend_configured="direct", backend_used="direct",
    )
    monkeypatch.setattr(cli_module, "load_usage_snapshot", lambda _id: usage)
    assert _usage_for_policy(no_auth_account) is usage
    monkeypatch.setattr(cli_module, "auth_identity_from_file", lambda _path: (_ for _ in ()).throw(OSError("missing")))
    assert _usage_for_policy(account).error == "auth.json identity unavailable"


def test_cli_validation_and_usage_result_helper_edges(monkeypatch):
    with pytest.raises(ValueError, match="localhost"):
        cli_module._validate_bridge_host("not-a-host", allow_remote=False)
    assert cli_module._validate_bridge_host("localhost", allow_remote=False) is None
    with pytest.raises(ValueError, match="absolute HTTP"):
        cli_module._bridge_endpoint("http://[invalid", 8787)
    with pytest.raises(ValueError, match="--auth-json"):
        cli_module._validate_direct_auth_mapping((Account("a", "A", "/tmp/a"), Account("b", "B", "/tmp/b")), Path("auth"))

    config = SimpleNamespace(accounts=(Account("a", "A", "/tmp/a"),))
    monkeypatch.setattr(cli_module, "fetch_all", lambda *_args, **_kwargs: [object()])
    with pytest.raises(ValueError, match="identity mismatch"):
        cli_module._load_overview_usages(config)

    class BrokenMain:
        @property
        def has_valid_usage(self):
            raise ValueError("broken main")

    usage = AccountUsage(
        account_id="a", label="A", captured_at=datetime.now(ZoneInfo("UTC")),
        status=AccountStatus.OK, backend_configured="direct", backend_used="direct", main=BrokenMain(),
    )
    assert _is_successful_usage(usage) is False
    assert cli_module._has_valid_usage_provenance(object()) is False
    monkeypatch.setattr(cli_module, "backend_provenance_matches_configured", lambda *_args: (_ for _ in ()).throw(ValueError("bad")))
    assert cli_module._has_valid_usage_provenance(
        AccountUsage(
            account_id="a", label="A", captured_at=datetime.now(ZoneInfo("UTC")),
            backend_configured="direct", backend_used="direct",
        )
    ) is False
    assert cli_module._is_safe_watchdog_usage(
        AccountUsage(
            account_id="a", label="A", captured_at=datetime.now(ZoneInfo("UTC")),
            status=AccountStatus.BLOCKED, backend_configured="direct", backend_used="direct",
        )
    ) is False
    assert not cli_module._all_usage_results_valid([usage], ["a", "b"], predicate=lambda _item: True)
    assert not cli_module._all_usage_results_valid([object()], ["a"], predicate=lambda _item: True)


def test_profile_delete_and_ingest_helper_edges(monkeypatch, tmp_path):
    missing = tmp_path / "missing"
    assert cli_module._validate_profile_delete_target(missing, force=False) == missing.resolve()
    with pytest.raises(ValueError, match="not a directory"):
        file_path = tmp_path / "file"
        file_path.write_text("x", encoding="utf-8")
        cli_module._validate_profile_delete_target(file_path, force=True)
    with pytest.raises(ValueError, match="outside the default"):
        outside = tmp_path / "outside"
        outside.mkdir()
        cli_module._validate_profile_delete_target(outside, force=False)
    with pytest.raises(ValueError, match="unsafe profile"):
        cli_module._validate_profile_delete_target(Path("/"), force=True)
    marker_dir = tmp_path / "marker-dir"
    marker_dir.mkdir()
    (marker_dir / ".codex-usage-profile").mkdir()
    with pytest.raises(ValueError, match="marker"):
        cli_module._validate_profile_delete_target(marker_dir, force=True)
    symlink_target = tmp_path / "target"
    symlink_target.mkdir()
    symlink = tmp_path / "symlink"
    symlink.symlink_to(symlink_target, target_is_directory=True)
    monkeypatch.setattr(cli_module, "assert_no_symlink_ancestors", lambda *_args, **_kwargs: None)
    with pytest.raises(ValueError, match="must not be a symlink"):
        cli_module._validate_profile_delete_target(symlink, force=True)

    profile = tmp_path / "profiles"
    profile.mkdir()
    browser = profile / "firefox"
    browser.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="browser profile"):
        cli_module._profile_delete_lock_targets(profile, browser="firefox")
    browser.unlink()
    browser.write_text("not-dir", encoding="utf-8")
    with pytest.raises(ValueError, match="browser profile"):
        cli_module._profile_delete_lock_targets(profile, browser="firefox")
    browser.unlink()
    oauth = profile / "oauth"
    oauth.mkdir()
    (oauth / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="OAuth browser profile"):
        cli_module._profile_delete_lock_targets(profile, browser="firefox")
    (oauth / "link").unlink()
    (oauth / "file").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="OAuth browser profile"):
        cli_module._profile_delete_lock_targets(profile, browser="firefox")
    (oauth / "file").unlink()
    (oauth / "nested").mkdir()
    assert cli_module._profile_delete_lock_targets(profile, browser="firefox")
    assert cli_module._is_relative_to(tmp_path / "x", tmp_path) is True
    assert cli_module._is_relative_to(tmp_path / "x", tmp_path / "other") is False

    assert cli_module._delete_profile_dir(missing, browser="firefox", force=False) == "fehlt"
    assert cli_module._payload_from_raw_ingest("   ") == {"bodyText": ""}
    assert cli_module._payload_from_raw_ingest("[1, 2]") == {"bodyText": "[1, 2]"}
    monkeypatch.setattr(cli_module, "read_private_text", lambda *_args, **_kwargs: ("payload", None))
    assert cli_module._read_ingest_raw(SimpleNamespace(stdin=False, file=Path("input"))) == "payload"

    class BinaryStdin:
        def __init__(self, payload):
            self.buffer = BytesIO(payload)

    monkeypatch.setattr(cli_module.sys, "stdin", BinaryStdin(b"hello"))
    assert cli_module._read_ingest_stdin() == "hello"
    monkeypatch.setattr(cli_module.sys, "stdin", BinaryStdin(b"\xff"))
    with pytest.raises(ValueError, match="UTF-8"):
        cli_module._read_ingest_stdin()
    monkeypatch.setattr(cli_module, "MAX_INGEST_BYTES", 2)
    monkeypatch.setattr(cli_module.sys, "stdin", BinaryStdin(b"123"))
    with pytest.raises(ValueError, match="too large"):
        cli_module._read_ingest_stdin()

    class TextStdin:
        buffer = None

        def __init__(self, value):
            self.value = value

        def read(self, _size):
            return self.value

    monkeypatch.setattr(cli_module, "MAX_INGEST_BYTES", 10)
    monkeypatch.setattr(cli_module.sys, "stdin", TextStdin("plain"))
    assert cli_module._read_ingest_stdin() == "plain"
    monkeypatch.setattr(cli_module.sys, "stdin", TextStdin("x" * 11))
    with pytest.raises(ValueError, match="too large"):
        cli_module._read_ingest_stdin()
    monkeypatch.setattr(cli_module.sys, "stdin", TextStdin("\ud800"))
    with pytest.raises(ValueError, match="UTF-8"):
        cli_module._read_ingest_stdin()


def test_profile_delete_transaction_commit_and_rollback_edges(monkeypatch, tmp_path):
    class Locks:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    locks = Locks()
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    path = tmp_path / "profile"
    tx = cli_module._ProfileDeleteTransaction(path, quarantine, "geloescht", locks)
    assert tx.rollback() is None
    assert path.is_dir()
    assert locks.closed is True

    locks = Locks()
    quarantine = tmp_path / "quarantine-2"
    quarantine.mkdir()
    path = tmp_path / "profile-2"
    tx = cli_module._ProfileDeleteTransaction(path, quarantine, "geloescht", locks)
    monkeypatch.setattr(cli_module.shutil, "rmtree", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("rmtree")))
    with pytest.raises(OSError, match="rmtree"):
        tx.commit()
    assert locks.closed is False

    locks = Locks()
    quarantine = tmp_path / "quarantine-3"
    quarantine.mkdir()
    path = tmp_path / "profile-3"
    tx = cli_module._ProfileDeleteTransaction(path, quarantine, "geloescht", locks)
    monkeypatch.setattr(cli_module.shutil, "rmtree", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("rmtree")))
    original_rollback = tx.rollback

    def rollback_with_failure():
        path.mkdir()
        return original_rollback()

    monkeypatch.setattr(tx, "rollback", rollback_with_failure)
    # Direct transaction behavior does not wrap commit errors; wrapper belongs to _delete_profile_dir.
    with pytest.raises(OSError, match="rmtree"):
        tx.commit()

    profile = tmp_path / "deletable"
    profile.mkdir()
    (profile / ".codex-usage-profile").write_text("marker\n", encoding="utf-8")
    monkeypatch.setattr(cli_module.shutil, "rmtree", lambda _path, **_kwargs: (_ for _ in ()).throw(OSError("rmtree")))
    with pytest.raises(OSError, match="rmtree"):
        cli_module._delete_profile_dir(profile, browser="firefox", force=False)
    assert profile.is_dir()

    profile2 = tmp_path / "deletable-2"
    profile2.mkdir()
    (profile2 / ".codex-usage-profile").write_text("marker\n", encoding="utf-8")

    def rmtree_and_recreate(path, **_kwargs):
        profile2.mkdir()
        raise OSError("partial")

    monkeypatch.setattr(cli_module.shutil, "rmtree", rmtree_and_recreate)
    with pytest.raises(ExceptionGroup, match="profile deletion rollback failed"):
        cli_module._delete_profile_dir(profile2, browser="firefox", force=False)


def test_cli_remaining_small_branches(monkeypatch, capsys, tmp_path):
    # Allow one polling sleep before job reaches terminal state.
    statuses = iter(("running", "completed"))
    monkeypatch.setattr(cli_module, "list_profile_jobs", lambda _account: [{"job_id": "job", "status": "running"}])
    monkeypatch.setattr(cli_module, "cancel_profile_job", lambda _job: None)
    monkeypatch.setattr(cli_module, "profile_job_status", lambda _job: {"status": next(statuses)})
    monkeypatch.setattr(cli_module.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(cli_module.time, "sleep", lambda _seconds: None)
    cli_module._cancel_account_profile_jobs("alpha")

    class CommitErrorTx:
        def commit(self):
            raise OSError("commit")

    delete_account = Account("a", "A", "/tmp/a")
    monkeypatch.setattr(cli_module, "load_config", lambda _path: SimpleNamespace(accounts=(delete_account,)))
    monkeypatch.setattr(cli_module, "resolve_account", lambda *_args: delete_account)
    monkeypatch.setattr(cli_module, "account_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(cli_module, "profile_job_creation_lock", lambda: nullcontext())
    monkeypatch.setattr(cli_module, "_cancel_account_profile_jobs", lambda _account: None)
    monkeypatch.setattr(cli_module, "_managed_service_sync_required", lambda _path: False)
    monkeypatch.setattr(cli_module, "_sync_managed_service", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_module, "remove_account", lambda *_args, **_kwargs: (SimpleNamespace(accounts=()), None))
    monkeypatch.setattr(cli_module, "remove_account_state", lambda *_args, **_kwargs: CommitErrorTx())
    monkeypatch.setattr(cli_module, "revoke_bridge_token", lambda _account: None)
    with pytest.raises(OSError, match="commit"):
        cli_module._cmd_account_delete(
            SimpleNamespace(
                config=tmp_path / "config", account="a", delete_profile=False,
                force_delete_profile=False, format="table",
            )
        )

    with pytest.raises(ValueError, match="not allowed"):
        cli_module._cmd_policy_set_limits(
            SimpleNamespace(scope="global", identifier="bad", hourly=None, weekly=None, monthly=None)
        )
    monkeypatch.setattr(cli_module, "service_status", lambda: {"installed": False})
    cli_module._sync_managed_service(object(), tmp_path / "config", strict=False)

    assert cli_module._default_root_command([]) == ["once"]
    assert cli_module._default_root_command(["--config", "cfg"]) == ["--config", "cfg", "once"]
    assert cli_module._default_root_command(["--config=cfg"]) == ["--config=cfg", "once"]
    assert cli_module._default_root_command(["--config", "cfg", "--format", "json"]) == ["--config", "cfg", "once", "--format", "json"]
    assert cli_module._default_root_command(["--config", "cfg", "account"]) == ["--config", "cfg", "account"]
    assert cli_module._default_root_command(["--config", "cfg", "--help"]) == ["--config", "cfg", "--help"]
    assert cli_module._default_root_command(["unknown-command"]) == ["unknown-command"]
    with pytest.raises(ValueError, match="no accounts"):
        cli_module._select_accounts(SimpleNamespace(accounts=()), None)

    usage = AccountUsage(
        account_id="a", label="A", captured_at=datetime.now(ZoneInfo("UTC")),
        status=AccountStatus.OK, backend_configured="direct", backend_used="direct",
        five_hour=LimitWindow(name="5h", remaining=50),
    )
    assert _is_successful_usage(usage) is True
    no_windows = AccountUsage(
        account_id="a", label="A", captured_at=datetime.now(ZoneInfo("UTC")),
        status=AccountStatus.OK, backend_configured="direct", backend_used="direct",
    )
    assert _is_successful_usage(no_windows) is False
    stale = AccountUsage(
        account_id="a", label="A", captured_at=datetime.now(ZoneInfo("UTC")),
        status=AccountStatus.OK, backend_configured="direct", backend_used="direct",
        cache_invalidated=True, five_hour=LimitWindow(name="5h", remaining=50),
    )
    assert cli_module._is_safe_watchdog_usage(stale) is False
    blocked = AccountUsage(
        account_id="a", label="A", captured_at=datetime.now(ZoneInfo("UTC")),
        status=AccountStatus.BLOCKED, backend_configured="direct", backend_used="direct",
        blocked_reason="rate limit",
    )
    assert cli_module._is_safe_watchdog_usage(blocked) is True
    assert not cli_module._all_usage_results_valid([usage], ["a", "a"], predicate=lambda _item: True)
    usage_b = AccountUsage(
        account_id="b", label="B", captured_at=datetime.now(ZoneInfo("UTC")),
        status=AccountStatus.OK, backend_configured="direct", backend_used="direct",
    )
    assert not cli_module._all_usage_results_valid([usage, usage_b], ["a", "a"], predicate=lambda _item: True)
    assert not cli_module._all_usage_results_valid([usage, usage_b], ["a", "c"], predicate=lambda _item: True)

    profile = tmp_path / "profile-locks"
    profile.mkdir()
    oauth = profile / "oauth"
    oauth.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="OAuth profile root"):
        cli_module._profile_delete_lock_targets(profile, browser="firefox")
    oauth.unlink()
    (profile / "oauth").write_text("not-dir", encoding="utf-8")
    with pytest.raises(ValueError, match="OAuth profile root"):
        cli_module._profile_delete_lock_targets(profile, browser="firefox")


def test_service_sync_not_installed_and_cli_module_guard(monkeypatch):
    monkeypatch.setattr(cli_module, "service_status", lambda: {"installed": False})
    cli_module._sync_managed_service(object(), Path("config"), strict=False)
    assert cli_module._cmd_service(SimpleNamespace(action="status", config=None, format="table")) == 0
    monkeypatch.setattr(sys, "argv", ["codex-usage", "--version"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("codex_usage.cli", run_name="__main__")
    assert exc.value.code == 0


def test_cli_remaining_branch_edges(monkeypatch, tmp_path):
    statuses = iter(("completed", "completed"))
    monkeypatch.setattr(
        cli_module,
        "list_profile_jobs",
        lambda _account: [
            {"job_id": "terminal", "status": "completed"},
            {"job_id": "terminal-2", "status": "completed"},
        ],
    )
    monkeypatch.setattr(cli_module, "profile_job_status", lambda _job: {"status": next(statuses)})
    cli_module._cancel_account_profile_jobs("alpha")

    first = Account("first", "First", "/tmp/first", auth_json_path="first.json")
    second = Account("second", "Second", "/tmp/second", auth_json_path="second.json")
    config = AppConfig(accounts=(first, second))
    monkeypatch.setattr(cli_module, "auth_identity_from_file", lambda _path: ("user", "backend"))
    monkeypatch.setattr(
        cli_module,
        "auth_identity_for_account",
        lambda account: ("user", "other") if account.id == "first" else ("user", "backend"),
    )
    assert cli_module._resolve_policy_account(config, None, Path("agent-auth.json")) is second

    assert cli_module._validate_direct_auth_mapping((first,), None) is None
    assert cli_module._validate_direct_auth_mapping((first, second), None) is None
    cli_module._validate_fetch_mode_flags(
        SimpleNamespace(headed=True, direct=False, auth_json=None, backend=None)
    )

    class Locks:
        def close(self):
            pass

    no_quarantine = cli_module._ProfileDeleteTransaction(
        tmp_path / "profile", None, "fehlt", Locks()
    )
    assert no_quarantine.commit() == "fehlt"
    no_quarantine.rollback()
    rollback = cli_module._ProfileDeleteTransaction(
        tmp_path / "profile", tmp_path / "quarantine", "geloescht", Locks(), rollbackable=False
    )
    rollback.quarantine.mkdir()
    with pytest.raises(OSError, match="cannot be rolled back"):
        rollback.rollback()

    profile = tmp_path / "race-profile"
    profile.mkdir()
    oauth = profile / "oauth"
    oauth.mkdir()
    original_iterdir = Path.iterdir

    class RaceCandidate:
        def is_symlink(self):
            return False

        def is_dir(self):
            return False

        def exists(self):
            return False

    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda self: iter((RaceCandidate(),)) if self == oauth else original_iterdir(self),
    )
    assert cli_module._profile_delete_lock_targets(profile, browser="firefox")


def test_account_delete_enters_nested_account_lock_normally(monkeypatch, tmp_path, capsys):
    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    account = Account("a", "A", str(tmp_path / "profile"))
    config = SimpleNamespace(accounts=(account,))
    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "resolve_account", lambda *_args: account)
    monkeypatch.setattr(cli_module, "account_lock", lambda *_args: Context())
    monkeypatch.setattr(cli_module, "profile_job_creation_lock", lambda: Context())
    monkeypatch.setattr(cli_module, "_cancel_account_profile_jobs", lambda _account: None)
    monkeypatch.setattr(cli_module, "_managed_service_sync_required", lambda _path: False)
    monkeypatch.setattr(cli_module, "_sync_managed_service", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_module, "remove_account", lambda *_args, **_kwargs: (SimpleNamespace(accounts=()), None))
    monkeypatch.setattr(cli_module, "remove_account_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_module, "revoke_bridge_token", lambda _account: None)
    assert cli_module._cmd_account_delete(
        SimpleNamespace(
            config=tmp_path / "config", account="a", delete_profile=False,
            force_delete_profile=False, format="table",
        )
    ) == 0
    assert "Account geloescht" in capsys.readouterr().out


def test_profile_command_handlers_cover_text_errors_and_job_results(monkeypatch, capsys):
    account = Account(id="alpha", label="Alpha", profile_dir="/tmp/alpha")
    layout = SimpleNamespace(
        account_id="alpha",
        profile_dir=Path("/tmp/alpha"),
        codex_home=Path("/tmp/alpha/codex-home"),
        auth_json=Path("/tmp/alpha/codex-home/auth.json"),
        metadata=Path("/tmp/alpha/metadata.json"),
        jobs=Path("/tmp/alpha/jobs"),
        migration=Path("/tmp/alpha/migration.json"),
    )
    monkeypatch.setattr(cli_module, "load_config", lambda _path: object())
    monkeypatch.setattr(cli_module, "resolve_account", lambda *_args: account)
    monkeypatch.setattr(cli_module, "ensure_profile_layout", lambda _account: layout)
    assert cli_module._cmd_profile_layout(SimpleNamespace(config=None, account="alpha", format="table")) == 0
    assert "codex_home:" in capsys.readouterr().out

    monkeypatch.setattr(cli_module, "rollback_auth_migration", lambda _path: None)
    migrate_args = SimpleNamespace(
        rollback=Path("manifest.json"),
        config=None,
        search_root=(),
        dry_run=False,
        manifest=None,
        format="table",
    )
    assert cli_module._cmd_profile_migrate(migrate_args) == 0
    assert "rolled_back" in capsys.readouterr().out

    monkeypatch.setattr(
        cli_module,
        "run_device_login",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(cli_module.DeviceLoginError("login failed")),
    )
    login_args = SimpleNamespace(
        config=None,
        account="alpha",
        codex_bin="codex",
        timeout=60,
        format="table",
    )
    assert cli_module._cmd_profile_device_login(login_args) == 2
    assert "login failed" in capsys.readouterr().out

    monkeypatch.setattr(cli_module, "account_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(cli_module, "create_profile_job", lambda **kwargs: {"ok": True, **kwargs})
    create_args = SimpleNamespace(
        account_id="new",
        label="New",
        browser="firefox",
        backend="direct",
        profile_dir=None,
        reactivation_browser="auto",
        expected_backend_account_id=None,
        config=None,
        json_events=False,
        tag=None,
        series=None,
        series_active=False,
    )
    assert cli_module._cmd_profile_create(create_args) == 0
    capsys.readouterr()

    monkeypatch.setattr(cli_module, "list_profile_jobs", lambda _account: [])
    monkeypatch.setattr(cli_module, "profile_job_status", lambda _job: {"ok": False})
    monkeypatch.setattr(cli_module, "cancel_profile_job", lambda _job: {"ok": False})
    assert cli_module._cmd_profile_jobs(SimpleNamespace(account="alpha")) == 0
    capsys.readouterr()
    assert cli_module._cmd_profile_job_status(SimpleNamespace(job_id="job")) == 2
    capsys.readouterr()
    assert cli_module._cmd_profile_job_cancel(SimpleNamespace(job_id="job")) == 2
    capsys.readouterr()


def test_reactivation_and_account_handlers_cover_success_and_errors(monkeypatch, capsys):
    account = Account(id="alpha", label="Alpha", profile_dir="/tmp/alpha")
    persisted = []
    monkeypatch.setattr(cli_module, "load_config", lambda _path: object())
    monkeypatch.setattr(cli_module, "resolve_account", lambda *_args: account)
    monkeypatch.setattr(
        cli_module,
        "mark_account_auth_sync_required",
        lambda account_id, **kwargs: persisted.append((account_id, kwargs)),
    )

    monkeypatch.setattr(
        cli_module,
        "reactivate_account",
        lambda *_args, **kwargs: {"ok": True, "browser": kwargs["browser"]},
    )
    monkeypatch.setattr(
        cli_module,
        "sync_account_auth",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must remain explicit")),
    )
    assert cli_module._cmd_reactivate(
        SimpleNamespace(config=None, account="alpha", browser="firefox", format="table")
    ) == 0
    output = capsys.readouterr().out
    assert "Account reaktiviert" in output
    assert "sync_required" in output

    monkeypatch.setattr(
        cli_module,
        "reactivate_account",
        lambda *_args, **kwargs: {"ok": True, "browser": kwargs["browser"]},
    )
    assert cli_module._cmd_reactivate(
        SimpleNamespace(config=None, account="alpha", browser="firefox", format="json")
    ) == 0
    assert json.loads(capsys.readouterr().out)["auth_sync_required"] is True

    monkeypatch.setattr(
        cli_module,
        "reactivate_account",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            cli_module.ReactivationError("reactivation failed")
        ),
    )
    assert cli_module._cmd_reactivate(
        SimpleNamespace(config=None, account="alpha", browser="auto", format="json")
    ) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "reactivation failed"
    assert persisted == [
        ("alpha", {"path": None}),
        ("alpha", {"path": None}),
    ]

    monkeypatch.setattr(
        cli_module,
        "open_account_in_reactivation_browser",
        lambda *_args, **_kwargs: {"ok": True, "browser": "firefox"},
    )
    assert cli_module._cmd_account_manage(
        SimpleNamespace(config=None, account="alpha", browser=None, format="table")
    ) == 0
    assert "Account geöffnet" in capsys.readouterr().out

    monkeypatch.setattr(
        cli_module,
        "open_account_in_reactivation_browser",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            cli_module.ReactivationError("manage failed")
        ),
    )
    assert cli_module._cmd_account_manage(
        SimpleNamespace(config=None, account="alpha", browser="chromium", format="json")
    ) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "manage failed"

    monkeypatch.setattr(
        cli_module,
        "start_account_terminal",
        lambda _account: {"ok": True, "profile_dir": "/tmp/alpha"},
    )
    assert cli_module._cmd_account_terminal(
        SimpleNamespace(config=None, account="alpha", format="table")
    ) == 0
    assert "Terminal gestartet" in capsys.readouterr().out

    monkeypatch.setattr(
        cli_module,
        "start_account_terminal",
        lambda _account: (_ for _ in ()).throw(
            cli_module.TerminalError("terminal failed")
        ),
    )
    assert cli_module._cmd_account_terminal(
        SimpleNamespace(config=None, account="alpha", format="json")
    ) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "terminal failed"


def test_profile_migrate_dry_run_and_apply(monkeypatch, capsys, tmp_path):
    item = SimpleNamespace(
        account_id="alpha",
        source=Path("old/auth.json"),
        target=Path("new/auth.json"),
        status="planned",
        reason="found",
    )
    plan = SimpleNamespace(migration_id="migration-1", items=(item,))
    monkeypatch.setattr(cli_module, "load_config", lambda _path: SimpleNamespace(accounts=()))
    monkeypatch.setattr(cli_module, "plan_auth_migration", lambda *_args, **_kwargs: plan)

    dry_args = SimpleNamespace(
        rollback=None,
        config=None,
        search_root=(Path("search"),),
        dry_run=True,
        manifest=None,
        format="json",
    )
    assert cli_module._cmd_profile_migrate(dry_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"

    monkeypatch.setattr(
        cli_module,
        "apply_auth_migration",
        lambda _plan, manifest: {"status": "applied", "manifest": str(manifest)},
    )
    apply_args = SimpleNamespace(
        rollback=None,
        config=None,
        search_root=(),
        dry_run=False,
        manifest=tmp_path / "manifest.json",
        format="table",
    )
    assert cli_module._cmd_profile_migrate(apply_args) == 0
    assert capsys.readouterr().out.strip() == "applied"


def test_general_cli_does_not_expose_integration_snapshot():
    parser = cli_module._build_parser()

    with pytest.raises(SystemExit) as error:
        parser.parse_args(["integration-snapshot", "--schema", "2", "--format", "json"])
    assert error.value.code == 2


def test_policy_and_spark_health_commands_cover_validation_and_outputs(monkeypatch, capsys):
    policy = {"global": {"allow_paid_overage": True}}
    monkeypatch.setattr(cli_module, "set_policy_rule", lambda *args: policy)
    assert cli_module._cmd_policy_set(
        SimpleNamespace(scope="global", identifier=None, value="allow")
    ) == 0
    assert json.loads(capsys.readouterr().out) == policy

    with pytest.raises(ValueError, match="--id is not allowed"):
        cli_module._cmd_policy_set(
            SimpleNamespace(scope="global", identifier="bad", value="deny")
        )
    monkeypatch.setattr(cli_module, "set_credit_limits", lambda *args, **kwargs: {"limits": kwargs})
    assert cli_module._cmd_policy_set_limits(
        SimpleNamespace(scope="account", identifier="alpha", hourly=1, weekly=2, monthly=3)
    ) == 0
    capsys.readouterr()
    with pytest.raises(ValueError, match="--id is required"):
        cli_module._cmd_policy_set_limits(
            SimpleNamespace(scope="account", identifier=None, hourly=None, weekly=None, monthly=None)
        )

    monkeypatch.setattr(cli_module, "load_policy", lambda: policy)
    assert cli_module._cmd_policy_overview(SimpleNamespace()) == 0
    assert json.loads(capsys.readouterr().out) == policy

    monkeypatch.setattr(cli_module, "spark_health_status", lambda account: {"account": account, "state": "healthy"})
    assert cli_module._cmd_spark_health(
        SimpleNamespace(backend_account_id="backend", state=None, reason=None)
    ) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "healthy"
    monkeypatch.setattr(
        cli_module,
        "set_spark_health",
        lambda account, state, *, reason: {"account": account, "state": state, "reason": reason},
    )
    assert cli_module._cmd_spark_health(
        SimpleNamespace(backend_account_id="backend", state="failed", reason="timeout")
    ) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "timeout"
    with pytest.raises(ValueError, match="--reason requires"):
        cli_module._cmd_spark_health(
            SimpleNamespace(backend_account_id="backend", state=None, reason="oops")
        )


def test_openai_accounts_command_returns_complete_live_projection(monkeypatch, capsys):
    config = AppConfig(
        accounts=(
            Account(
                id="profile-one",
                label="OpenAI One",
                profile_dir="/private/profile-one",
                auth_json_path="/private/profile-one/codex-home/auth.json",
                series="A",
                series_active=True,
                auth_sync_required=True,
            ),
        ),
        masterjet=MasterjetConnection(
            transport="https", endpoint="https://masterjet.example.test/control"
        ),
    )
    remote = OpenAIControlAccount(
        ref="openai-one", label="OpenAI One", enabled=True,
        local_profile_ref="profile-one", source_host_ref="host-one",
        auth_state="ready",
        access_expires_at=datetime(2026, 8, 28, 18, tzinfo=UTC),
        credential_generation=7, vault_projection_state="synced",
        usage_state="available",
    )
    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(
        cli_module,
        "_new_masterjet_client",
        lambda *_args, **_kwargs: SimpleNamespace(call=lambda operation, arguments: (remote,)),
    )
    monkeypatch.setattr(cli_module, "save_control_snapshot", lambda *_args, **_kwargs: None)

    assert main(["masterjet", "openai-accounts", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["stale"] is False
    assert payload["accounts"][0]["vault_projection_state"] == "synced"
    assert payload["accounts"][0]["usage_state"] == "available"
    assert payload["local_accounts"] == [{
        "account": "profile-one", "label": "OpenAI One",
        "local_auth_state": "missing", "auth_sync_required": True,
        "series-active": True,
    }]
    assert "/private/profile-one" not in json.dumps(payload)


def test_account_details_cli_returns_exact_live_projection_envelope(monkeypatch, capsys):
    account = SimpleNamespace(
        ref="google-one", label="Google One", enabled=True, subject_bound=True,
        oauth_state="ready", inventory_generation=4, quota_state="fresh",
        project_count=1, billing_count=0, reload_state="ready",
    )
    project = SimpleNamespace(
        ref="hive-one", project_name="Amber Orchard", purpose="quota_probe",
        key_name="Willow Meadow", billing_ref=None, status="ready",
        probe_state="ready", quota_state="available",
    )
    controller = SimpleNamespace(
        account_details=lambda: (SimpleNamespace(account=account, projects=(project,)),)
    )
    monkeypatch.setattr(cli_module, "_new_google_controller", lambda _path: controller)
    monkeypatch.setattr(
        cli_module, "_save_google_projection", lambda *_args, **_kwargs: None
    )

    assert main(["google", "accounts", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["stale"] is False
    assert payload["accounts"] == [cli_module._google_account_json(account)]
    assert payload["projects"]["google-one"][0]["project_name"] == "Amber Orchard"
    assert payload["projects"]["google-one"][0]["key_name"] == "Willow Meadow"


def test_live_projection_connection_commands_use_one_canonical_config(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.toml"
    save_config(AppConfig(accounts=()), config_path)
    assert main([
        "--config", str(config_path), "masterjet", "connection-set",
        "--transport", "https", "--endpoint",
        "https://masterjet.example.test/control", "--timeout-seconds", "7", "--json",
    ]) == 0
    set_payload = json.loads(capsys.readouterr().out)
    assert load_config(config_path).masterjet == MasterjetConnection(
        transport="https", endpoint="https://masterjet.example.test/control",
        timeout_seconds=7,
    )

    assert main(["--config", str(config_path), "masterjet", "connection-show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == set_payload

    calls = []

    class Client:
        def __init__(self, connection, **_kwargs):
            calls.append(connection)

        def call(self, operation, arguments):
            calls.append((operation, arguments))
            return ()

    before = config_path.read_bytes()
    monkeypatch.setattr(cli_module, "MasterjetControlClient", Client)
    assert main(["--config", str(config_path), "masterjet", "connection-test", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert config_path.read_bytes() == before
    assert calls[-1] == ("openai.accounts.list", {})


def test_masterjet_client_factory_wires_https_providers_and_keeps_local_peer_only(
    tmp_path, monkeypatch
):
    credential_dir = tmp_path / "credentials"
    credential_dir.mkdir(mode=0o700)
    credential = credential_dir / "masterjet-control-bearer"
    credential.write_text("remote-bearer", encoding="ascii")
    credential.chmod(0o400)
    captured = []

    class Client:
        def __init__(self, connection, **kwargs):
            captured.append((connection, kwargs))

    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credential_dir))
    monkeypatch.setattr(cli_module, "MasterjetControlClient", Client)

    remote = cli_module._new_masterjet_client(
        MasterjetConnection(transport="https", endpoint="https://masterjet.example.test/control"),
        step_up_stdin=True,
    )
    local = cli_module._new_masterjet_client(
        MasterjetConnection(transport="local", endpoint="/run/user/1000/masterjet.sock"),
        step_up_stdin=True,
    )

    assert isinstance(remote, Client)
    assert isinstance(local, Client)
    assert set(captured[0][1]) == {"bearer_provider", "step_up_provider"}
    assert captured[0][1]["bearer_provider"]() == "remote-bearer"
    assert captured[1][1] == {}


def test_live_projection_connection_set_rejects_invalid_endpoint_before_write(
    tmp_path, capsys
):
    config_path = tmp_path / "config.toml"
    save_config(AppConfig(accounts=()), config_path)
    before = config_path.read_bytes()

    assert main([
        "--config", str(config_path), "masterjet", "connection-set",
        "--transport", "https", "--endpoint",
        "https://user:secret@masterjet.example.test/control", "--json",
    ]) == 2

    assert config_path.read_bytes() == before
    assert json.loads(capsys.readouterr().out) == {
        "ok": False, "code": "control.endpoint_invalid"
    }


def test_full_plan_preview_cli_contains_every_visible_name_pair(monkeypatch, capsys):
    plan = SimpleNamespace(
        account_ref="google-one", plan_id="plan-one", expected_generation=4,
        plan_digest="sha256:" + "a" * 64,
        expires_at=datetime(2026, 8, 28, 18, tzinfo=ZoneInfo("UTC")),
        step_count=5,
        projects=(
            SimpleNamespace(project_name="Amber Orchard", key_name="Willow Meadow"),
            SimpleNamespace(project_name="Velvet Harbor", key_name="Silver Forest"),
        ),
    )
    monkeypatch.setattr(
        cli_module, "_new_google_controller",
        lambda _path: SimpleNamespace(provision_plan=lambda _account: plan),
    )

    assert main(["google", "provision-plan", "google-one", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["step_count"] == 5
    assert payload["projects"] == [
        {"project_name": "Amber Orchard", "key_name": "Willow Meadow"},
        {"project_name": "Velvet Harbor", "key_name": "Silver Forest"},
    ]
