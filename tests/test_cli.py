from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import codex_usage.cli as cli_module
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
from codex_usage.config import AppConfig, add_or_update_account, load_config, save_config
from codex_usage.models import Account, AccountStatus, AccountUsage, LimitWindow, UsagePool
from codex_usage.spark_health import set_spark_health
from codex_usage.state import load_current_usage, save_current_usage, save_usage_snapshot


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


def test_integration_snapshot_rejects_symlinked_cache_parent_before_chmod(
    tmp_path, monkeypatch
):
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o755)
    integration = tmp_path / "integration"
    integration.symlink_to(target, target_is_directory=True)
    cache_path = integration / "account-usage-v1.json"

    monkeypatch.setattr(cli_module, "read_current_usage_records", lambda _path: ())
    monkeypatch.setattr(
        cli_module,
        "build_schema1_document",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(cli_module, "serialize_schema1_document", lambda _document: b"{}")
    monkeypatch.setattr(
        cli_module,
        "publish_schema1_cache",
        lambda *_args, **_kwargs: pytest.fail("publish must not run"),
    )

    args = type(
        "IntegrationSnapshotArgs",
        (),
        {"current_dir": tmp_path / "current", "cache_path": cache_path},
    )()
    with pytest.raises(ValueError, match="integration cache"):
        cli_module._cmd_integration_snapshot(args)

    assert target.stat().st_mode & 0o777 == 0o755


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
    assert "[--current-dir DIR] [--cache-path PATH]" in output
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
