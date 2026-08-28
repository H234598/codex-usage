from __future__ import annotations

import argparse
import ipaddress
import json
import shutil
import sys
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .account_lock import account_lock
from .bridge import (
    MAX_INGEST_BYTES,
    ingest_and_save,
    load_latest_usages,
    render_bridge_snippet,
    revoke_bridge_token,
    run_bridge_server,
    write_bridge_extension,
)
from .browser import (
    _profile_browser_dir,
    _profile_lock,
    diagnose_account,
    login_account,
    probe_account,
)
from .config import (
    SUPPORTED_BACKENDS,
    SUPPORTED_BROWSERS,
    SUPPORTED_REACTIVATION_BROWSERS,
    MasterjetConnection,
    add_or_update_account,
    compare_and_clear_account_auth_sync_required,
    default_config_path,
    default_state_dir,
    load_config,
    mark_account_auth_sync_required,
    remove_account,
    resolve_account,
    restore_account,
    save_config,
)
from .consumption import (
    ConsumptionWindow,
    calculate_consumption,
    consumption_lookback_seconds,
)
from .direct import (
    DirectAuthError,
    auth_identity_changed,
    auth_identity_for_account,
    auth_identity_from_file,
    validate_auth_json_file,
)
from .google_accounts import (
    GoogleAccountDetails,
    GoogleAccountsController,
    GoogleAccountsError,
    validate_google_plan_digest,
)
from .health import clear_health, load_health, record_health_event
from .history import HistoryStore
from .json_utils import loads_strict
from .masterjet_auth_sync import AuthSyncError, sync_account_auth
from .masterjet_cache import (
    ControlSnapshot,
    load_control_snapshot,
    save_control_snapshot,
)
from .masterjet_client import MasterjetClientError, MasterjetControlClient
from .masterjet_contracts import (
    ControlOperation,
    GoogleControlAccount,
    GoogleControlProject,
    GoogleControlProjectList,
    GoogleOAuthTransactionV1,
    OpenAIControlAccount,
)
from .masterjet_credentials import (
    bearer_provider_from_systemd_credentials,
    stdin_step_up_provider,
    tty_step_up_provider,
    unavailable_step_up_provider,
)
from .models import AccountStatus, AccountUsage
from .private_io import (
    assert_no_symlink_ancestors,
    read_private_text,
)
from .profile_jobs import (
    cancel_profile_job,
    create_profile_job,
    list_profile_jobs,
    profile_job_creation_lock,
    profile_job_status,
)
from .profile_layout import ensure_profile_layout
from .profile_login import DeviceLoginError, DeviceLoginResult, run_device_login
from .profile_migration import (
    apply_auth_migration,
    plan_auth_migration,
    rollback_auth_migration,
)
from .reactivate import (
    MANAGE_ACCOUNT_URL,
    REACTIVATION_BROWSERS,
    ReactivationError,
    open_account_in_reactivation_browser,
    reactivate_account,
)
from .render import (
    _safe_usage_for_display,
    render_account_overview,
    render_account_values,
    render_json,
    render_table,
)
from .routing import (
    DEFAULT_MAX_USAGE_AGE_SECONDS,
    effective_paid_overage,
    evaluate_routing,
    load_policy,
    set_credit_limits,
    set_policy_rule,
)
from .scheduler import fetch_all, watch, watchdog
from .service import (
    managed_service_config_path,
    render_service_json,
    service_disable,
    service_enable,
    service_install,
    service_status,
    service_uninstall,
)
from .spark_health import set_spark_health, spark_health_status
from .state import (
    backend_provenance_matches_configured,
    load_current_usage,
    load_usage_snapshot,
    remove_account_state,
)
from .terminal import TerminalError, start_account_terminal

# `auto` selects a browser; it does not create an OAuth profile. Keep a little
# room for stale profiles while bounding hostile directory enumeration.
MAX_PROFILE_OAUTH_ENTRIES = len(SUPPORTED_REACTIVATION_BROWSERS) * 2

COMMAND_OVERVIEW = """\
Komplette Command-Line-Usage:

Globale Optionen:
  codex-usage [--config CONFIG] COMMAND ...
  codex-usage [--config CONFIG]

Accounts:
  codex-usage account add ACCOUNT_ID [--label LABEL] [--profile-dir DIR]
                                   [--browser BROWSER] [--auth-json PATH]
                                   [--tag TAG] [--clear-auth-json]
                                   [--test-home]
                                   [--reactivation-browser BROWSER]
                                   [--series SERIES]
                                   [--series-active|--no-series-active]
                                   [--backend direct|app-server] [--format table|json]
  codex-usage account backend ACCOUNT direct|app-server [--format table|json]
  codex-usage account auth-sync ACCOUNT [--format table|json]
  codex-usage account overview [--format table|json] [--config-only]
  codex-usage account delete ACCOUNT [--delete-profile] [--force-delete-profile]
                                      [--format table|json]
  codex-usage account manage ACCOUNT [--browser auto|vivaldi|chromium|firefox]
                                     [--format table|json]
  codex-usage account terminal ACCOUNT [--format table|json]

Login und Reaktivierung:
  codex-usage login ACCOUNT
  codex-usage reactivate ACCOUNT [--browser auto|vivaldi|chromium|firefox]
                                 [--format table|json]

Masterjet und Google:
  codex-usage masterjet status --json
  codex-usage masterjet openai-accounts --json
  codex-usage masterjet openai-routing-options --json
  codex-usage masterjet connection-show --json
  codex-usage masterjet connection-test --json
  codex-usage masterjet connection-set --transport local|https --endpoint ENDPOINT
                                       [--timeout-seconds SECONDS] --json
  codex-usage google accounts --json
  codex-usage google add ACCOUNT --oauth-client-json PATH --json
  codex-usage google oauth-begin ACCOUNT --browser BROWSER --json
  codex-usage google inventory-refresh ACCOUNT --json
  codex-usage google provision-plan ACCOUNT --json
  codex-usage google provision-apply ACCOUNT PLAN_ID --plan-digest DIGEST --confirm --json

Abruf und Ueberwachung:
  codex-usage once [--account ACCOUNT] [--format table|json] [--headed]
                   [--backend direct|app-server] [--direct] [--auth-json PATH]
  codex-usage watch [--account ACCOUNT] [--format table|json] [--interval SEKUNDEN]
                    [--headed] [--backend direct|app-server] [--direct]
                    [--auth-json PATH]
  codex-usage watchdog [--account ACCOUNT] [--format table|json]
                       [--headed] [--backend direct|app-server] [--direct]
                       [--auth-json PATH]

Routing und Credits:
  codex-usage policy evaluate [ACCOUNT|--auth-json PATH] --role ROLE
                              [--group ID] [--agent ID] [--job ID]
                              [--max-age SEKUNDEN] [--format json]
  codex-usage policy set global allow|deny|inherit [--format json]
  codex-usage policy set account|group|agent|job allow|deny|inherit --id ID
                              [--format json]
  codex-usage policy set-limits [--hourly N] [--weekly N] [--monthly N]
                                [--scope global|account|group|agent|job] [--id ID]
                                [--format json]
  codex-usage policy overview [--format json]
  codex-usage policy status [--role ROLE] [--max-age SEKUNDEN] [--format json]
  codex-usage spark-health --backend-account-id ID [--state healthy|failed]
                            [--reason TEXT] [--format json]

Analyse und Diagnose:
  codex-usage probe ACCOUNT [--headless] [--save-dir DIR]
  codex-usage diagnose ACCOUNT [--headed] [--screenshot] [--save-dir DIR]
                              [--auth-json PATH]

Gespeicherte Werte und manuelle Aufnahme:
  codex-usage ingest ACCOUNT (--stdin | --file FILE)
  codex-usage latest [--format table|json]
  codex-usage values [--account ACCOUNT]

Historie und Limitverbrauch:
  codex-usage history status [--path PATH] [--format table|json] [--json]
  codex-usage history query --account ACCOUNT --pool POOL --window-seconds SECONDS
                            [--since ISO] [--until ISO] [--path PATH]
                            [--format table|json] [--json]
  codex-usage history prune [--before ISO|--days N] (--dry-run|--apply)
                            [--path PATH] [--format table|json] [--json]
  codex-usage consumption --account ACCOUNT --amount N --unit minutes|hours|days|weeks
                          [--baseline-minutes N] [--baseline-value-minutes N]
                          [--smoothing none|ema-5|ema-10|ema-20|ema-40|
                           ema-80|ema-160|ema-320|ema-640]
                          [--pool POOL] [--limit-window short|weekly|monthly|spark|all]
                          [--path PATH] [--now ISO] [--format table|json] [--json]
Profile und Auth-Migration:
  codex-usage profile layout --account ACCOUNT [--format json]
  codex-usage profile migrate-auth (--dry-run|--apply [--search-root DIR])
                                   | --rollback MANIFEST [--manifest PATH]
                                   [--format table|json]
  codex-usage profile create --account-id ID --label LABEL --browser BROWSER
                             --backend BACKEND --profile-dir PATH
                             [--tag TAG]
                             [--reactivation-browser BROWSER]
                             [--series SERIES]
                             [--series-active|--no-series-active]
                             [--expected-backend-account-id ID] [--json-events]
  codex-usage profile jobs [--account ACCOUNT] [--json]
  codex-usage profile job-status JOB_ID [--json]
  codex-usage profile cancel JOB_ID [--json]
  codex-usage profile device-login --account ACCOUNT [--codex-bin PATH]
                                   [--timeout SEKUNDEN] [--format table|json]

Stabilität und Diagnose:
  codex-usage health [--format table|json] [--clear]

Browser-Bridge:
  codex-usage bridge-snippet ACCOUNT [--endpoint URL] [--port PORT] [--interval SEKUNDEN]
  codex-usage bridge-extension ACCOUNT [--output DIR] [--endpoint URL]
                              [--port PORT] [--interval SEKUNDEN]
  codex-usage bridge-server [--host HOST] [--port PORT] [--allow-remote]
                            [--tls-cert PATH --tls-key PATH]

Sonstiges:
  codex-usage service install|enable|disable|status|uninstall [--format table|json]
  codex-usage paths

ACCOUNT kann eine Account-ID oder ein eindeutiges Label sein.
Direct- und App-Server-Abrufe mit mehreren Accounts brauchen pro Account auth_json_path.
Ein globales --auth-json ist nur fuer genau einen ausgewaehlten Account erlaubt.

Beispiele:
  codex-usage account add BW_Privat --auth-json ~/.codex/auth.json
  codex-usage account backend BW_Privat app-server
  codex-usage once --account BW_Privat --backend app-server
  codex-usage values
  codex-usage watch
  codex-usage service enable
  codex-usage latest --format json

Hinweis:
  `codex-usage` ohne Subcommand entspricht `codex-usage once`.
  Ohne Override nutzt jeder Account seinen gespeicherten Abrufweg.
  app-server aktualisiert ablaufende Codex-Anmeldedaten und faellt nur bei fehlender
  App-Server-Kompatibilitaet auf direct zurueck. --direct erzwingt den alten Abrufweg.
  App-Server-Kontostatusabfragen starten keine Modellanfrage und verbrauchen kein
  Inferenzkontingent.
  bridge-server lauscht ohne --allow-remote nur auf Loopback/localhost.
"""

ACCOUNT_DELETE_PROFILE_JOB_TIMEOUT_SECONDS = 30

KNOWN_COMMANDS = {
    "account",
    "login",
    "reactivate",
    "masterjet",
    "google",
    "once",
    "watch",
    "watchdog",
    "policy",
    "spark-health",
    "probe",
    "diagnose",
    "ingest",
    "latest",
    "values",
    "history",
    "consumption",
    "profile",
    "health",
    "bridge-snippet",
    "bridge-extension",
    "bridge-server",
    "service",
    "paths",
}


def main(argv: list[str] | None = None) -> int:
    if argv is not None and (
        not isinstance(argv, list) or any(not isinstance(argument, str) for argument in argv)
    ):
        print("Fehler: argv is invalid", file=sys.stderr)
        return 2
    parser = _build_parser()
    normalized_argv = _default_root_command(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(normalized_argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except KeyError as exc:
        message = exc.args[0] if exc.args else str(exc)
        print(f"Fehler: {message}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-usage",
        description="Poll ChatGPT Codex analytics limits for multiple accounts.",
        epilog=COMMAND_OVERVIEW,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, default=None, help="Pfad zur config.toml")
    parser.add_argument("--step-up-stdin", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    account = sub.add_parser("account", help="Accounts verwalten")
    account_sub = account.add_subparsers(dest="account_command", required=True)
    add = account_sub.add_parser("add", help="Account-Profil anlegen oder aktualisieren")
    add.add_argument("account_id")
    add.add_argument("--label")
    add.add_argument("--tag", help="Kurzes Account-Kürzel für Anzeige und Masterjet")
    add.add_argument("--profile-dir")
    add.add_argument(
        "--browser",
        choices=SUPPORTED_BROWSERS,
        help="Browser fuer Login und Polling, Standard: firefox",
    )
    add.add_argument("--auth-json", type=Path, help="Codex auth.json fuer direkten Abruf")
    add.add_argument(
        "--test-home",
        action="store_true",
        help="Separates CODEX_HOME unter ~/.codex-test anlegen und Codex dort initialisieren",
    )
    add.add_argument(
        "--clear-auth-json",
        action="store_true",
        help="Gespeicherten auth.json-Pfad entfernen",
    )
    add.add_argument(
        "--reactivation-browser",
        choices=SUPPORTED_REACTIVATION_BROWSERS,
        help="Isolierter OAuth-Browser fuer Reaktivierung",
    )
    add.add_argument("--series", help="Masterjet-Serie, z. B. A, B oder C")
    add.add_argument(
        "--series-active",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Serie fuer Masterjet aktivieren oder deaktivieren",
    )
    add.add_argument("--backend", choices=SUPPORTED_BACKENDS)
    add.add_argument("--format", choices=("table", "json"), default="table")
    add.set_defaults(func=_cmd_account_add)
    overview = account_sub.add_parser(
        "overview",
        help="Account-Uebersicht mit aktuellen Werten anzeigen",
    )
    overview.add_argument("--format", choices=("table", "json"), default="table")
    overview.add_argument(
        "--config-only",
        action="store_true",
        help="Nur konfigurierte Accounts ohne Live-Abruf anzeigen",
    )
    overview.set_defaults(func=_cmd_account_overview)
    backend = account_sub.add_parser("backend", help="Abrufweg eines Accounts setzen")
    backend.add_argument("account", help="Account-ID oder eindeutiges Label")
    backend.add_argument("backend", choices=SUPPORTED_BACKENDS)
    backend.add_argument("--format", choices=("table", "json"), default="table")
    backend.set_defaults(func=_cmd_account_backend)
    auth_sync = account_sub.add_parser(
        "auth-sync",
        help="Kanonische OpenAI-auth.json explizit mit Masterjet synchronisieren",
    )
    auth_sync.add_argument("account", help="Account-ID oder eindeutiges Label")
    auth_sync.add_argument("--format", choices=("table", "json"), default="table")
    auth_sync.set_defaults(func=_cmd_account_auth_sync)
    delete = account_sub.add_parser("delete", help="Account aus der Config entfernen")
    delete.add_argument("account", help="Account-ID oder eindeutiges Label")
    delete.add_argument(
        "--delete-profile",
        action="store_true",
        help="Auch den gespeicherten Browser-Profilordner loeschen",
    )
    delete.add_argument(
        "--force-delete-profile",
        action="store_true",
        help="Profilordner auch ausserhalb des Standardprofils loeschen",
    )
    delete.add_argument("--format", choices=("table", "json"), default="table")
    delete.set_defaults(func=_cmd_account_delete)
    manage = account_sub.add_parser(
        "manage",
        help="Codex Usage im isolierten Reaktivierungsbrowser öffnen",
    )
    manage.add_argument("account", help="Account-ID oder eindeutiges Label")
    manage.add_argument(
        "--browser",
        choices=REACTIVATION_BROWSERS,
        default=None,
        help="Isolierter Browser; Standard: Browser des Accounts",
    )
    manage.add_argument("--format", choices=("table", "json"), default="table")
    manage.set_defaults(func=_cmd_account_manage)
    terminal = account_sub.add_parser(
        "terminal",
        help="Neues Terminal mit Codex im Account-Profil starten",
    )
    terminal.add_argument("account", help="Account-ID oder eindeutiges Label")
    terminal.add_argument("--format", choices=("table", "json"), default="table")
    terminal.set_defaults(func=_cmd_account_terminal)

    login = sub.add_parser("login", help="Sichtbaren Browser fuer einen Account oeffnen")
    login.add_argument("account", help="Account-ID oder eindeutiges Label")
    login.set_defaults(func=_cmd_login)

    reactivate = sub.add_parser(
        "reactivate",
        help="Abgelaufene Codex-auth.json in isoliertem Browser erneuern",
    )
    reactivate.add_argument("account", help="Account-ID oder eindeutiges Label")
    reactivate.add_argument(
        "--browser",
        choices=REACTIVATION_BROWSERS,
        default="auto",
        help="Isolierter OAuth-Browser, Standard: auto (Vivaldi bevorzugt)",
    )
    reactivate.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Ausgabeformat, Standard: table",
    )
    reactivate.set_defaults(func=_cmd_reactivate)

    masterjet = sub.add_parser("masterjet", help="Masterjet-Controlstatus anzeigen")
    masterjet_sub = masterjet.add_subparsers(dest="masterjet_command", required=True)
    masterjet_status = masterjet_sub.add_parser("status", help="Controlstatus anzeigen")
    masterjet_status.add_argument("--json", action="store_true")
    masterjet_status.set_defaults(func=_cmd_masterjet_status)
    masterjet_routing = masterjet_sub.add_parser(
        "openai-routing-options", help="OpenAI-Routingoptionen anzeigen"
    )
    masterjet_routing.add_argument("--json", action="store_true")
    masterjet_routing.set_defaults(func=_cmd_masterjet_openai_routing_options)
    masterjet_openai = masterjet_sub.add_parser(
        "openai-accounts", help="Redigierte OpenAI-Accounts anzeigen"
    )
    masterjet_openai.add_argument("--json", action="store_true")
    masterjet_openai.set_defaults(func=_cmd_masterjet_openai_accounts)
    connection_show = masterjet_sub.add_parser(
        "connection-show", help="Kanonische Verbindung anzeigen"
    )
    connection_show.add_argument("--json", action="store_true")
    connection_show.set_defaults(func=_cmd_masterjet_connection_show)
    connection_test = masterjet_sub.add_parser(
        "connection-test", help="Kanonische Verbindung testen"
    )
    connection_test.add_argument("--json", action="store_true")
    connection_test.set_defaults(func=_cmd_masterjet_connection_test)
    connection_set = masterjet_sub.add_parser(
        "connection-set", help="Kanonische Verbindung speichern"
    )
    connection_set.add_argument("--transport", choices=("local", "https"), required=True)
    connection_set.add_argument("--endpoint", required=True)
    connection_set.add_argument("--timeout-seconds", type=int, default=10)
    connection_set.add_argument("--json", action="store_true")
    connection_set.set_defaults(func=_cmd_masterjet_connection_set)

    google = sub.add_parser("google", help="Google-Controlaccounts verwalten")
    google_sub = google.add_subparsers(dest="google_command", required=True)
    google_accounts = google_sub.add_parser("accounts", help="Google-Accounts anzeigen")
    google_accounts.add_argument("--json", action="store_true")
    google_accounts.set_defaults(func=_cmd_google_accounts)
    google_add = google_sub.add_parser("add", help="OAuth-Client importieren")
    google_add.add_argument("account")
    google_add.add_argument("--oauth-client-json", type=Path, required=True)
    google_add.add_argument("--json", action="store_true")
    google_add.set_defaults(func=_cmd_google_add)
    google_oauth_begin = google_sub.add_parser("oauth-begin", help="Google-OAuth starten")
    google_oauth_begin.add_argument("account")
    google_oauth_begin.add_argument("--browser", choices=SUPPORTED_BROWSERS, required=True)
    google_oauth_begin.add_argument("--json", action="store_true")
    google_oauth_begin.set_defaults(func=_cmd_google_oauth_begin)
    google_inventory = google_sub.add_parser(
        "inventory-refresh", help="Google-Inventar aktualisieren"
    )
    google_inventory.add_argument("account")
    google_inventory.add_argument("--json", action="store_true")
    google_inventory.set_defaults(func=_cmd_google_inventory_refresh)
    google_plan = google_sub.add_parser("provision-plan", help="Provisionierungsplan erzeugen")
    google_plan.add_argument("account")
    google_plan.add_argument("--json", action="store_true")
    google_plan.set_defaults(func=_cmd_google_provision_plan)
    google_apply = google_sub.add_parser("provision-apply", help="Provisionierungsplan anwenden")
    google_apply.add_argument("account")
    google_apply.add_argument("plan_id")
    google_apply.add_argument("--plan-digest")
    google_apply.add_argument("--confirm", action="store_true")
    google_apply.add_argument("--json", action="store_true")
    google_apply.set_defaults(func=_cmd_google_provision_apply)

    once = sub.add_parser("once", help="Alle oder einzelne Accounts einmal auslesen")
    once.add_argument("--account", action="append", dest="account_ids")
    once.add_argument("--format", choices=("table", "json"), default="table")
    once.add_argument("--headed", action="store_true", help="Browser sichtbar starten")
    once.add_argument("--direct", action="store_true", help="Ohne Browser ueber auth.json abrufen")
    once.add_argument("--backend", choices=SUPPORTED_BACKENDS)
    once.add_argument("--auth-json", type=Path, help="auth.json fuer direkten Abruf ueberschreiben")
    once.set_defaults(func=_cmd_once)

    watch_cmd = sub.add_parser("watch", help="Alle 5 Minuten fortlaufend auslesen")
    watch_cmd.add_argument("--account", action="append", dest="account_ids")
    watch_cmd.add_argument("--format", choices=("table", "json"), default="table")
    watch_cmd.add_argument("--interval", type=int, default=None)
    watch_cmd.add_argument("--headed", action="store_true", help="Browser sichtbar starten")
    watch_cmd.add_argument(
        "--direct",
        action="store_true",
        help="Ohne Browser ueber auth.json abrufen",
    )
    watch_cmd.add_argument("--backend", choices=SUPPORTED_BACKENDS)
    watch_cmd.add_argument(
        "--auth-json",
        type=Path,
        help="auth.json fuer direkten Abruf ueberschreiben",
    )
    watch_cmd.set_defaults(func=_cmd_watch)

    watchdog_cmd = sub.add_parser(
        "watchdog",
        help="Einmalig pruefen, limitierte Accounts zu sperren und spaeter freizugeben",
    )
    watchdog_cmd.add_argument("--account", action="append", dest="account_ids")
    watchdog_cmd.add_argument("--format", choices=("table", "json"), default="table")
    watchdog_cmd.add_argument("--headed", action="store_true", help="Browser sichtbar starten")
    watchdog_cmd.add_argument(
        "--direct",
        action="store_true",
        help="Ohne Browser ueber auth.json abrufen",
    )
    watchdog_cmd.add_argument("--backend", choices=SUPPORTED_BACKENDS)
    watchdog_cmd.add_argument(
        "--auth-json",
        type=Path,
        help="auth.json fuer direkten Abruf ueberschreiben",
    )
    watchdog_cmd.set_defaults(func=_cmd_watchdog)

    policy = sub.add_parser(
        "policy",
        help="Modellrouting und Freigabe bezahlter Credits verwalten",
    )
    policy_sub = policy.add_subparsers(dest="policy_command", required=True)
    policy_evaluate = policy_sub.add_parser(
        "evaluate",
        help="Gespeicherte Usagewerte in eine Routingentscheidung umsetzen",
    )
    policy_evaluate.add_argument(
        "account",
        nargs="?",
        help="Account-ID oder eindeutiges Label; alternativ --auth-json",
    )
    policy_evaluate.add_argument(
        "--auth-json",
        type=Path,
        help="Account anhand kanonischer Backend-Account-ID zuordnen",
    )
    policy_evaluate.add_argument("--role", required=True)
    policy_evaluate.add_argument("--group")
    policy_evaluate.add_argument("--agent")
    policy_evaluate.add_argument("--job")
    policy_evaluate.add_argument(
        "--max-age",
        type=int,
        default=DEFAULT_MAX_USAGE_AGE_SECONDS,
        help="Maximales Alter der Usagewerte in Sekunden, Standard: 600",
    )
    policy_evaluate.add_argument("--format", choices=("json",), default="json")
    policy_evaluate.set_defaults(func=_cmd_policy_evaluate)

    policy_set = policy_sub.add_parser(
        "set",
        help="Credit-Freigabe fuer einen Scope setzen oder erben",
    )
    policy_set.add_argument("scope", choices=("global", "account", "group", "agent", "job"))
    policy_set.add_argument("value", choices=("allow", "deny", "inherit"))
    policy_set.add_argument("--id", dest="identifier")
    policy_set.add_argument("--format", choices=("json",), default="json")
    policy_set.set_defaults(func=_cmd_policy_set)

    policy_limits = policy_sub.add_parser(
        "set-limits",
        help="Stunden-, Wochen- und Monatslimit fuer bezahlte Credits setzen",
    )
    for name in ("hourly", "weekly", "monthly"):
        policy_limits.add_argument("--" + name, type=float, default=None)
    policy_limits.add_argument(
        "--scope",
        choices=("global", "account", "group", "agent", "job"),
        default="global",
        help="global oder Geltungsbereich der Limits",
    )
    policy_limits.add_argument("--id", dest="identifier", help="ID des Geltungsbereichs")
    policy_limits.add_argument("--format", choices=("json",), default="json")
    policy_limits.set_defaults(func=_cmd_policy_set_limits)

    policy_overview = policy_sub.add_parser(
        "overview",
        help="Gespeicherte Credit-Richtlinien anzeigen",
    )
    policy_overview.add_argument("--format", choices=("json",), default="json")
    policy_overview.set_defaults(func=_cmd_policy_overview)

    policy_status = policy_sub.add_parser(
        "status",
        help="Richtlinien und Routingentscheidungen aller Accounts anzeigen",
    )
    policy_status.add_argument("--role", default="arbeitsbiene")
    policy_status.add_argument(
        "--max-age",
        type=int,
        default=DEFAULT_MAX_USAGE_AGE_SECONDS,
        help="Maximales Alter der Usagewerte in Sekunden, Standard: 600",
    )
    policy_status.add_argument("--format", choices=("json",), default="json")
    policy_status.set_defaults(func=_cmd_policy_status)

    spark_health = sub.add_parser(
        "spark-health",
        help="Letzten erfolgreichen oder fehlgeschlagenen Spark-Turn verwalten",
    )
    spark_health.add_argument("--backend-account-id", required=True)
    spark_health.add_argument("--state", choices=("healthy", "failed"))
    spark_health.add_argument("--reason")
    spark_health.add_argument("--format", choices=("json",), default="json")
    spark_health.set_defaults(func=_cmd_spark_health)

    probe = sub.add_parser("probe", help="Extraktionsquellen fuer einen Account untersuchen")
    probe.add_argument("account", help="Account-ID oder eindeutiges Label")
    probe.add_argument("--headless", action="store_true", help="Probe unsichtbar starten")
    probe.add_argument("--save-dir", type=Path, help="Rohkandidaten lokal speichern")
    probe.set_defaults(func=_cmd_probe)

    diagnose = sub.add_parser("diagnose", help="Login-/Cloudflare-/Seitenstatus untersuchen")
    diagnose.add_argument("account", help="Account-ID oder eindeutiges Label")
    diagnose.add_argument("--headed", action="store_true", help="Browser sichtbar starten")
    diagnose.add_argument("--screenshot", action="store_true", help="Diagnose-Screenshot speichern")
    diagnose.add_argument("--save-dir", type=Path, help="Ordner fuer Screenshot")
    diagnose.add_argument("--auth-json", type=Path, help="Codex auth.json redigiert mitpruefen")
    diagnose.set_defaults(func=_cmd_diagnose)

    ingest = sub.add_parser("ingest", help="Manuell exportierten Seitentext aufnehmen")
    ingest.add_argument("account", help="Account-ID oder eindeutiges Label")
    ingest_source = ingest.add_mutually_exclusive_group(required=True)
    ingest_source.add_argument("--stdin", action="store_true", help="JSON/Text aus stdin lesen")
    ingest_source.add_argument("--file", type=Path, help="JSON/Text-Datei lesen")
    ingest.set_defaults(func=_cmd_ingest)

    latest = sub.add_parser("latest", help="Zuletzt manuell ingestierte Werte anzeigen")
    latest.add_argument("--format", choices=("table", "json"), default="table")
    latest.set_defaults(func=_cmd_latest)

    values = sub.add_parser("values", help="Knappe Werte-Uebersicht aller Accounts anzeigen")
    values.add_argument("--account", action="append", dest="account_ids")
    values.set_defaults(func=_cmd_values)

    history = sub.add_parser("history", help="Private Usage-Historie verwalten")
    history_sub = history.add_subparsers(dest="history_command", required=True)
    history_status = history_sub.add_parser("status", help="Historienstatus anzeigen")
    history_status.add_argument("--path", type=Path)
    history_status.add_argument("--format", choices=("table", "json"), default="table")
    history_status.add_argument("--json", dest="format", action="store_const", const="json")
    history_status.set_defaults(func=_cmd_history_status)
    history_query = history_sub.add_parser("query", help="Historienwerte abfragen")
    history_query.add_argument("--account", required=True)
    history_query.add_argument("--pool", default="main")
    history_query.add_argument("--window-seconds", type=int, required=True)
    history_query.add_argument("--since")
    history_query.add_argument("--until")
    history_query.add_argument("--path", type=Path)
    history_query.add_argument("--format", choices=("table", "json"), default="table")
    history_query.add_argument("--json", dest="format", action="store_const", const="json")
    history_query.set_defaults(func=_cmd_history_query)
    history_prune = history_sub.add_parser("prune", help="Alte Historienwerte entfernen")
    history_prune.add_argument("--before")
    history_prune.add_argument("--days", type=int, default=30)
    history_prune.add_argument("--dry-run", action="store_true")
    history_prune.add_argument("--apply", action="store_true")
    history_prune.add_argument("--path", type=Path)
    history_prune.add_argument("--format", choices=("table", "json"), default="table")
    history_prune.add_argument("--json", dest="format", action="store_const", const="json")
    history_prune.set_defaults(func=_cmd_history_prune)

    consumption = sub.add_parser("consumption", help="Limitverbrauch in Prozentpunkten berechnen")
    consumption.add_argument("--account", required=True)
    consumption.add_argument("--amount", type=int, required=True)
    consumption.add_argument("--unit", choices=("minutes", "hours", "days", "weeks"), required=True)
    consumption.add_argument(
        "--baseline-minutes",
        type=int,
        help="fester Ausgangspunkt in Minuten vor jetzt (0-9999); ohne Option automatisch",
    )
    consumption.add_argument(
        "--baseline-value-minutes",
        type=int,
        help="separater Ausgangspunkt für den AW-Wert in Minuten (0-9999); ändert das Delta nicht",
    )
    consumption.add_argument(
        "--smoothing",
        choices=(
            "none",
            "ema-5",
            "ema-10",
            "ema-20",
            "ema-40",
            "ema-80",
            "ema-160",
            "ema-320",
            "ema-640",
        ),
        default="none",
        help="zeitgewichtete EMA für die Prognoserate",
    )
    consumption.add_argument("--pool", default="main")
    consumption.add_argument(
        "--limit-window",
        choices=("short", "weekly", "monthly", "spark", "all"),
        default="short",
    )
    consumption.add_argument("--path", type=Path)
    consumption.add_argument("--now")
    consumption.add_argument("--format", choices=("table", "json"), default="table")
    consumption.add_argument("--json", dest="format", action="store_const", const="json")
    consumption.set_defaults(func=_cmd_consumption)

    profile = sub.add_parser("profile", help="Kanonische Accountprofile verwalten")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    profile_layout = profile_sub.add_parser("layout", help="Kanonische Profilpfade anzeigen")
    profile_layout.add_argument("--account", required=True)
    profile_layout.add_argument("--format", choices=("table", "json"), default="json")
    profile_layout.set_defaults(func=_cmd_profile_layout)
    profile_migrate = profile_sub.add_parser("migrate-auth", help="Legacy-auth.json migrieren")
    migrate_mode = profile_migrate.add_mutually_exclusive_group(required=True)
    migrate_mode.add_argument("--dry-run", action="store_true")
    migrate_mode.add_argument("--apply", action="store_true")
    migrate_mode.add_argument("--rollback", type=Path)
    profile_migrate.add_argument("--search-root", type=Path, action="append", default=[])
    profile_migrate.add_argument("--manifest", type=Path)
    profile_migrate.add_argument("--format", choices=("table", "json"), default="json")
    profile_migrate.set_defaults(func=_cmd_profile_migrate)
    profile_create = profile_sub.add_parser(
        "create", help="Neues Accountprofil als Device-Login-Job anlegen"
    )
    profile_create.add_argument("--account-id", required=True)
    profile_create.add_argument("--label", required=True)
    profile_create.add_argument("--tag", default="")
    profile_create.add_argument("--browser", choices=SUPPORTED_BROWSERS, required=True)
    profile_create.add_argument("--backend", choices=SUPPORTED_BACKENDS, required=True)
    profile_create.add_argument("--profile-dir", required=True)
    profile_create.add_argument(
        "--reactivation-browser",
        choices=SUPPORTED_REACTIVATION_BROWSERS,
        default="auto",
    )
    profile_create.add_argument("--series", default="")
    profile_create.add_argument(
        "--series-active", action=argparse.BooleanOptionalAction, default=False
    )
    profile_create.add_argument("--expected-backend-account-id")
    profile_create.add_argument("--json-events", action="store_true")
    profile_create.set_defaults(func=_cmd_profile_create)
    profile_jobs = profile_sub.add_parser("jobs", help="Aktive Profiljobs anzeigen")
    profile_jobs.add_argument("--account")
    profile_jobs.add_argument("--json", action="store_true")
    profile_jobs.set_defaults(func=_cmd_profile_jobs)
    profile_status = profile_sub.add_parser("job-status", help="Profiljobstatus anzeigen")
    profile_status.add_argument("job_id")
    profile_status.add_argument("--json", action="store_true")
    profile_status.set_defaults(func=_cmd_profile_job_status)
    profile_cancel = profile_sub.add_parser("cancel", help="Profiljob abbrechen")
    profile_cancel.add_argument("job_id")
    profile_cancel.add_argument("--json", action="store_true")
    profile_cancel.set_defaults(func=_cmd_profile_job_cancel)
    profile_login = profile_sub.add_parser(
        "device-login", help="Neues Codex-Profil ueber Device-Login authentifizieren"
    )
    profile_login.add_argument("--account", required=True)
    profile_login.add_argument("--codex-bin", default="codex")
    profile_login.add_argument("--timeout", type=int, default=900)
    profile_login.add_argument("--format", choices=("table", "json"), default="json")
    profile_login.set_defaults(func=_cmd_profile_device_login)

    health = sub.add_parser("health", help="Begrenztes Health-Protokoll anzeigen oder löschen")
    health.add_argument("--format", choices=("table", "json"), default="table")
    health.add_argument("--clear", action="store_true", help="Health-Protokoll löschen")
    health.add_argument("--record-component", help=argparse.SUPPRESS)
    health.add_argument("--record-event", help=argparse.SUPPRESS)
    health.add_argument("--account", help=argparse.SUPPRESS)
    health.add_argument("--duration-ms", type=int, help=argparse.SUPPRESS)
    health.add_argument("--error-class", help=argparse.SUPPRESS)
    health.set_defaults(func=_cmd_health)

    snippet = sub.add_parser(
        "bridge-snippet",
        help="Browser-Snippet fuer normalen Browser ausgeben",
    )
    snippet.add_argument("account", help="Account-ID oder eindeutiges Label")
    snippet.add_argument("--endpoint", help="Absolute HTTP(S)-Ingest-URL")
    snippet.add_argument("--port", type=int, default=8765)
    snippet.add_argument("--interval", type=int, default=300)
    snippet.set_defaults(func=_cmd_bridge_snippet)

    extension = sub.add_parser(
        "bridge-extension",
        help="Entpackte Vivaldi/Chromium-Bridge-Extension erzeugen",
    )
    extension.add_argument("account", help="Account-ID oder eindeutiges Label")
    extension.add_argument("--output", type=Path)
    extension.add_argument("--endpoint", help="Absolute HTTP(S)-Ingest-URL")
    extension.add_argument("--port", type=int, default=8765)
    extension.add_argument("--interval", type=int, default=300)
    extension.set_defaults(func=_cmd_bridge_extension)

    bridge = sub.add_parser("bridge-server", help="Lokalen Browser-Bridge-Server starten")
    bridge.add_argument("--host", default="127.0.0.1")
    bridge.add_argument("--port", type=int, default=8765)
    bridge.add_argument(
        "--allow-remote",
        action="store_true",
        help="Nicht-Loopback-Hostbindung explizit erlauben",
    )
    bridge.add_argument("--tls-cert", type=Path, help="TLS-Zertifikat fuer Bridge-Server")
    bridge.add_argument("--tls-key", type=Path, help="Privater TLS-Schluessel fuer Bridge-Server")
    bridge.set_defaults(func=_cmd_bridge_server)

    service = sub.add_parser("service", help="systemd-User-Timer verwalten")
    service.add_argument(
        "action",
        choices=("install", "enable", "disable", "status", "uninstall"),
    )
    service.add_argument("--format", choices=("table", "json"), default="table")
    service.set_defaults(func=_cmd_service)

    paths = sub.add_parser("paths", help="Standardpfade anzeigen")
    paths.set_defaults(func=_cmd_paths)
    return parser


def _cmd_account_add(args: argparse.Namespace) -> int:
    _, account = add_or_update_account(
        args.account_id,
        label=args.label,
        tag=args.tag,
        profile_dir=args.profile_dir,
        browser=args.browser,
        auth_json_path=str(args.auth_json) if args.auth_json else None,
        backend=args.backend,
        reactivation_browser=args.reactivation_browser,
        series=args.series,
        series_active=args.series_active,
        clear_auth_json=args.clear_auth_json,
        test_home=args.test_home,
        path=args.config,
        before_state_cleanup=lambda config: _sync_managed_service(
            config,
            args.config,
            strict=True,
        ),
        rollback_callback=lambda config: _sync_managed_service(
            config,
            args.config,
            strict=True,
        ),
    )
    if args.format == "json":
        print(
            json.dumps(
                {"ok": True, "account": _account_json(account)},
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
        )
        return 0
    print(f"Account gespeichert: {account.id} ({account.label})")
    print(f"Profil: {account.profile_dir}")
    print(f"Browser: {account.browser}")
    print(f"Backend: {account.backend}")
    if account.auth_json_path:
        print(f"Auth JSON: {account.auth_json_path}")
    print(f"Login: codex-usage login {account.id}")
    return 0


def _account_json(account: Any) -> dict[str, object]:
    return {
        "id": account.id,
        "label": account.label,
        "tag": account.tag,
        "profile_dir": account.profile_dir,
        "auth_json_path": account.auth_json_path,
        "browser": account.browser,
        "reactivation_browser": account.reactivation_browser,
        "series": account.series,
        "series_active": account.series_active,
        "backend": account.backend,
        "auth_sync_required": account.auth_sync_required,
        "auth_sync_generation": account.auth_sync_generation,
    }


def _cmd_account_overview(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    usages = {} if args.config_only else _load_overview_usages(config)
    if args.format == "json":
        usages_by_account = {usage.account_id: usage for usage in usages.values()}
        account_payloads = []
        for account in config.accounts:
            usage = usages_by_account.get(account.id)
            account_payloads.append(
                {
                    "id": account.id,
                    "label": account.label,
                    "tag": account.tag,
                    "profile_dir": account.profile_dir,
                    "auth_json_path": account.auth_json_path,
                    "browser": account.browser,
                    "reactivation_browser": account.reactivation_browser,
                    "series": account.series,
                    "series_active": account.series_active,
                    "auth_sync_required": account.auth_sync_required,
                    "auth_sync_generation": account.auth_sync_generation,
                    "backend": account.backend,
                    "backend_used": usage.backend_used if usage else None,
                    "fallback_reason": usage.fallback_reason if usage else None,
                    "usage": _overview_usage_json(
                        usage,
                        expected_backend=account.backend,
                    ),
                }
            )
        payload = {"accounts": account_payloads}
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
        return (
            0
            if args.config_only
            or _all_usage_results_valid(
                usages.values(), (account.id for account in config.accounts)
            )
            else 2
        )
    print(render_account_overview(config, args.config or default_config_path(), usages))
    return (
        0
        if args.config_only
        or _all_usage_results_valid(usages.values(), (account.id for account in config.accounts))
        else 2
    )


def _cmd_account_backend(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    current = resolve_account(config, args.account)
    _, updated = add_or_update_account(
        current.id,
        backend=args.backend,
        path=args.config,
    )
    payload = {
        "ok": True,
        "account": updated.id,
        "label": updated.label,
        "backend": updated.backend,
    }
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(f"Abrufweg gespeichert: {updated.id} ({updated.label}) -> {updated.backend}")
    return 0


def _cancel_account_profile_jobs(account_id: str) -> None:
    terminal_statuses = {"completed", "failed", "cancelled"}
    pending = set()
    for job in list_profile_jobs(account_id):
        job_id = job.get("job_id")
        if not isinstance(job_id, str):
            raise ValueError("profile job id is invalid")
        if job.get("status") not in terminal_statuses:
            pending.add(job_id)
    for job_id in pending:
        cancel_profile_job(job_id)

    deadline = time.monotonic() + ACCOUNT_DELETE_PROFILE_JOB_TIMEOUT_SECONDS
    while pending:
        for job_id in tuple(pending):
            current = profile_job_status(job_id)
            if current.get("status") in terminal_statuses:
                pending.remove(job_id)
        if not pending:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("profile jobs did not stop before account deletion")
        time.sleep(min(0.05, remaining))


def _cmd_account_delete(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    account = resolve_account(config, args.account)
    account_index = config.accounts.index(account)
    profile_path = Path(account.profile_dir).expanduser()
    profile_state = None

    def cleanup_account() -> str | None:
        profile_transaction = None
        state_transaction = None
        if args.delete_profile:
            profile_result = _delete_profile_dir(
                profile_path,
                browser=account.browser,
                force=args.force_delete_profile,
                defer_commit=True,
            )
            if isinstance(profile_result, _ProfileDeleteTransaction):
                profile_transaction = profile_result
                profile_state = profile_result.profile_state
            else:
                profile_state = profile_result
        else:
            profile_state = None
        cleanup_error = None
        try:
            state_transaction = remove_account_state(
                account.id,
                lock_held=True,
                defer_commit=True,
            )
        except BaseException as exc:
            cleanup_error = exc
        try:
            revoke_bridge_token(account.id)
        except BaseException as revoke_error:
            if state_transaction is not None:
                try:
                    state_transaction.rollback()
                except Exception as rollback_error:
                    raise BaseExceptionGroup(
                        "state deletion rollback failed",
                        [revoke_error, rollback_error],
                    ) from None
            if profile_transaction is not None:
                try:
                    profile_transaction.rollback()
                except Exception as rollback_error:
                    raise BaseExceptionGroup(
                        "profile deletion rollback failed",
                        [revoke_error, rollback_error],
                    ) from None
            if cleanup_error is not None:
                raise cleanup_error from revoke_error
            raise
        if cleanup_error is not None:
            if profile_transaction is not None:
                try:
                    profile_transaction.rollback()
                except Exception as rollback_error:
                    raise BaseExceptionGroup(
                        "profile deletion rollback failed",
                        [cleanup_error, rollback_error],
                    ) from None
            raise cleanup_error
        if state_transaction is not None:
            try:
                state_transaction.commit()
            except Exception as primary_error:
                if profile_transaction is not None:
                    try:
                        profile_transaction.rollback()
                    except Exception as rollback_error:
                        raise BaseExceptionGroup(
                            "profile deletion rollback failed",
                            [primary_error, rollback_error],
                        ) from None
                raise
        if profile_transaction is not None:
            try:
                return profile_transaction.commit()
            except Exception as primary_error:
                try:
                    profile_transaction.rollback()
                except Exception as rollback_error:
                    raise BaseExceptionGroup(
                        "profile deletion rollback failed",
                        [primary_error, rollback_error],
                    ) from None
                raise
        return profile_state

    def delete_transaction() -> None:
        nonlocal profile_state
        service_sync_required = _managed_service_sync_required(args.config)
        updated, _ = remove_account(
            account.id,
            path=args.config,
            expected=account,
        )
        try:
            _sync_managed_service(updated, args.config, strict=True)
        except Exception:
            try:
                restore_account(account, path=args.config, index=account_index)
            except Exception as rollback_error:
                raise ValueError(
                    "could not restore account config after service sync failure"
                ) from rollback_error
            raise
        try:
            profile_state = cleanup_account()
        except Exception as cleanup_error:
            try:
                restore_account(account, path=args.config, index=account_index)
                if service_sync_required:
                    _sync_managed_service(config, args.config, strict=True)
            except Exception as rollback_error:
                raise ValueError(
                    "could not restore account config after cleanup failure"
                ) from rollback_error
            raise cleanup_error

    with account_lock("__all_accounts__"):
        with profile_job_creation_lock():
            _cancel_account_profile_jobs(account.id)
            with account_lock(account.id):  # pragma: no branch - context-manager unwind edge
                if args.delete_profile:
                    _validate_profile_delete_target(
                        profile_path,
                        force=args.force_delete_profile,
                    )
                delete_transaction()
    if args.format == "json":
        print(
            json.dumps(
                {
                    "ok": True,
                    "account": account.id,
                    "label": account.label,
                    "profile_deleted": bool(args.delete_profile),
                },
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
        )
    else:
        print(f"Account geloescht: {account.id} ({account.label})")
        if args.delete_profile:
            print(f"Profil: {profile_state} {profile_path}")
        else:
            print(f"Profil behalten: {profile_path}")
    return 0


def _cmd_history_status(args: argparse.Namespace) -> int:
    with HistoryStore(args.path) as store:
        result = store.status()
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(f"Historie: {result['path']}")
        print(f"Samples: {result['sample_count']}")
    return 0


def _cmd_history_query(args: argparse.Namespace) -> int:
    start = _parse_history_datetime(args.since, "since") if args.since else None
    end = _parse_history_datetime(args.until, "until") if args.until else None
    with HistoryStore(args.path) as store:
        samples = store.samples(
            args.account,
            pool=args.pool,
            window_seconds=args.window_seconds,
            start=start,
            end=end,
        )
    result = {
        "account_id": args.account,
        "pool": args.pool,
        "window_seconds": args.window_seconds,
        "samples": [_history_sample_json(sample) for sample in samples],
    }
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        for sample in result["samples"]:
            print(
                f"{sample['captured_at']} {sample['used_percent']:.3f}%"
                f" generation={sample['reset_generation'] or '-'}"
            )
    return 0


def _cmd_history_prune(args: argparse.Namespace) -> int:
    if args.dry_run == args.apply:
        raise ValueError("exactly one of --dry-run or --apply is required")
    if type(args.days) is not int or not 1 <= args.days <= 3650:
        raise ValueError("days must be between 1 and 3650")
    before = (
        _parse_history_datetime(args.before, "before")
        if args.before
        else datetime.now(UTC) - timedelta(days=args.days)
    )
    with HistoryStore(args.path) as store:
        count = store.prune(before, dry_run=args.dry_run)
    result = {"ok": True, "dry_run": args.dry_run, "removed": count, "before": before.isoformat()}
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        action = "würden entfernt" if args.dry_run else "entfernt"
        print(f"{count} Samples {action}.")
    return 0


def _cmd_consumption(args: argparse.Namespace) -> int:
    now = _parse_history_datetime(args.now, "now") if args.now else datetime.now(UTC)
    durations = {
        "short": (18_000,),
        "weekly": (604_800,),
        "monthly": (2_592_000,),
        "spark": (18_000, 604_800, 2_592_000),
        "all": (18_000, 604_800, 2_592_000),
    }[args.limit_window]
    windows: list[ConsumptionWindow] = []
    lookback_seconds = consumption_lookback_seconds(args.amount, args.unit)
    baseline_seconds = (
        lookback_seconds if args.baseline_minutes is None else args.baseline_minutes * 60
    )
    if args.baseline_minutes is not None and not 0 <= args.baseline_minutes <= 9_999:
        raise ValueError("baseline-minutes must be between 0 and 9999")
    if args.baseline_value_minutes is not None and not 0 <= args.baseline_value_minutes <= 9_999:
        raise ValueError("baseline-value-minutes must be between 0 and 9999")
    baseline_value_seconds = (
        0 if args.baseline_value_minutes is None else args.baseline_value_minutes * 60
    )
    try:
        start = now - timedelta(
            seconds=max(lookback_seconds, baseline_seconds, baseline_value_seconds)
        )
    except (OverflowError, ValueError) as exc:
        raise ValueError("now is out of range") from exc
    with HistoryStore(args.path) as store:
        if args.limit_window == "all":
            durations = tuple(
                dict.fromkeys(
                    (
                        *durations,
                        *store.consumption_window_seconds(
                            args.account,
                            pool=args.pool,
                            start=start,
                            end=now,
                        ),
                    )
                )
            )
        for duration in durations:
            samples = store.samples_for_consumption(
                args.account,
                pool=args.pool,
                window_seconds=duration,
                start=start,
                end=now,
            )
            result = calculate_consumption(
                samples,
                amount=args.amount,
                unit=args.unit,
                now=now,
                baseline_minutes=args.baseline_minutes,
                baseline_value_minutes=args.baseline_value_minutes,
                smoothing=args.smoothing,
            )
            if result.limit_window_seconds == 0:
                result = ConsumptionWindow(
                    lookback_seconds=result.lookback_seconds,
                    pool=args.pool,
                    limit_window_seconds=duration,
                    consumed_percentage_points=result.consumed_percentage_points,
                    coverage=result.coverage,
                    sample_count=result.sample_count,
                    estimated_seconds_to_exhaustion=result.estimated_seconds_to_exhaustion,
                    baseline_used_percent=result.baseline_used_percent,
                )
            windows.append(result)
    payload = {"account_id": args.account, "windows": [window.as_dict() for window in windows]}
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        for window in windows:
            print(
                f"{window.pool}/{window.limit_window_seconds}s: "
                f"{window.consumed_percentage_points:.3f} %-Pkt. ({window.coverage})"
            )
    return 0


def _cmd_profile_layout(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    account = resolve_account(config, args.account)
    layout = ensure_profile_layout(account)
    payload = {
        "account_id": layout.account_id,
        "profile_dir": str(layout.profile_dir),
        "codex_home": str(layout.codex_home),
        "auth_json": str(layout.auth_json),
        "metadata": str(layout.metadata),
        "jobs": str(layout.jobs),
        "migration": str(layout.migration),
    }
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")
    return 0


def _cmd_profile_migrate(args: argparse.Namespace) -> int:
    if args.rollback is not None:
        rollback_auth_migration(args.rollback)
        result = {"ok": True, "status": "rolled_back", "manifest": str(args.rollback)}
    else:
        config = load_config(args.config)
        plan = plan_auth_migration(config.accounts, search_roots=tuple(args.search_root))
        if args.dry_run:
            result = {
                "ok": True,
                "status": "dry_run",
                "migration_id": plan.migration_id,
                "items": [
                    {
                        "account_id": item.account_id,
                        "source": str(item.source) if item.source else None,
                        "target": str(item.target),
                        "status": item.status,
                        "reason": item.reason,
                    }
                    for item in plan.items
                ],
            }
        else:
            manifest = args.manifest or (
                default_state_dir() / "migrations" / f"{plan.migration_id}.json"
            )
            result = apply_auth_migration(plan, manifest)
            result["ok"] = True
            result["manifest"] = str(manifest)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(result["status"])
    return 0


def _cmd_profile_device_login(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    account = resolve_account(config, args.account)
    result: DeviceLoginResult | dict[str, object]
    try:
        result = run_device_login(
            account,
            args.config or default_config_path(),
            codex_bin=args.codex_bin,
            timeout_seconds=args.timeout,
        )
    except DeviceLoginError as exc:
        result = {"ok": False, "account": account.id, "events": [], "error": str(exc)}
    payload = result.as_dict() if isinstance(result, DeviceLoginResult) else result
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        message = payload.get("error") or (
            "Device-Login abgeschlossen" if payload.get("ok") else "Device-Login fehlgeschlagen"
        )
        print(message)
    return 0 if payload.get("ok") is True else 2


def _cmd_profile_create(args: argparse.Namespace) -> int:
    with account_lock("__all_accounts__"):
        profile_kwargs = dict(
            account_id=args.account_id,
            label=args.label,
            browser=args.browser,
            backend=args.backend,
            profile_dir=args.profile_dir,
            reactivation_browser=args.reactivation_browser,
            expected_backend_account_id=args.expected_backend_account_id,
            config_path=args.config,
            json_events=args.json_events,
        )
        if args.tag:
            profile_kwargs["tag"] = args.tag
        if args.series:
            profile_kwargs["series"] = args.series
        if args.series_active:
            profile_kwargs["series_active"] = True
        result = create_profile_job(**profile_kwargs)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result.get("ok") is True else 2


def _cmd_profile_jobs(args: argparse.Namespace) -> int:
    result = {"ok": True, "jobs": list_profile_jobs(args.account)}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


def _cmd_profile_job_status(args: argparse.Namespace) -> int:
    result = profile_job_status(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result.get("ok") is not False else 2


def _cmd_profile_job_cancel(args: argparse.Namespace) -> int:
    result = cancel_profile_job(args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result.get("ok") is not False else 2


def _history_sample_json(sample) -> dict[str, object]:
    return {
        "account_id": sample.account_id,
        "pool": sample.pool,
        "window_seconds": sample.window_seconds,
        "captured_at": sample.captured_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "used_percent": sample.used_percent,
        "reset_at": (
            sample.reset_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if sample.reset_at
            else None
        ),
        "reset_generation": sample.reset_generation,
        "source": sample.source,
    }


def _parse_history_datetime(value: str, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    try:
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} is out of range") from exc


def _new_masterjet_client(
    connection: MasterjetConnection, *, step_up_stdin: bool = False
) -> MasterjetControlClient:
    if connection.transport == "local":
        return MasterjetControlClient(connection)
    step_up = (
        stdin_step_up_provider(getattr(sys.stdin, "buffer", sys.stdin))
        if step_up_stdin
        else tty_step_up_provider()
        if sys.stdin.isatty() and sys.stderr.isatty()
        else unavailable_step_up_provider()
    )
    return MasterjetControlClient(
        connection,
        bearer_provider=bearer_provider_from_systemd_credentials(),
        step_up_provider=step_up,
    )


def _new_google_controller(
    config_path: Path | None, *, step_up_stdin: bool = False
) -> GoogleAccountsController:
    config = load_config(config_path)
    return GoogleAccountsController(
        _new_masterjet_client(config.masterjet, step_up_stdin=step_up_stdin)
    )


def _new_google_controller_for_args(args: argparse.Namespace) -> GoogleAccountsController:
    if bool(getattr(args, "step_up_stdin", False)):
        return _new_google_controller(args.config, step_up_stdin=True)
    return _new_google_controller(args.config)


def _new_google_oauth_controller(_config_path: Path | None) -> GoogleAccountsController:
    # Productive bound callback provider arrives with Task 8 callback integration.
    raise GoogleAccountsError("oauth.callback_unavailable")


def _cmd_masterjet_status(args: argparse.Namespace) -> int:
    return _cmd_masterjet_connection_test(args)


def _connection_json(connection: MasterjetConnection) -> dict[str, object]:
    return {
        "transport": connection.transport,
        "endpoint": connection.endpoint,
        "timeout_seconds": connection.timeout_seconds,
    }


def _print_connection_payload(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        return
    if payload.get("ok") is True:
        connection = payload["connection"]
        assert isinstance(connection, dict)
        print(
            f"{connection['transport']}\t{connection['endpoint']}\t{connection['timeout_seconds']}s"
        )
    else:
        print(f"Fehler: {payload.get('code', 'control.transport_unavailable')}", file=sys.stderr)


def _cmd_masterjet_connection_show(args: argparse.Namespace) -> int:
    try:
        connection = load_config(args.config).masterjet
    except Exception:
        payload = {"ok": False, "code": "control.endpoint_invalid"}
        _print_connection_payload(payload, json_output=args.json)
        return 2
    payload = {"ok": True, "connection": _connection_json(connection)}
    _print_connection_payload(payload, json_output=args.json)
    return 0


def _cmd_masterjet_connection_test(args: argparse.Namespace) -> int:
    try:
        connection = load_config(args.config).masterjet
        accounts = _new_masterjet_client(
            connection, step_up_stdin=bool(getattr(args, "step_up_stdin", False))
        ).call("openai.accounts.list", {})
        if type(accounts) is not tuple or any(
            type(account) is not OpenAIControlAccount for account in accounts
        ):
            raise MasterjetClientError("control.response_invalid")
    except MasterjetClientError as exc:
        payload = {"ok": False, "code": exc.code}
        _print_connection_payload(payload, json_output=args.json)
        return 2
    except Exception:
        payload = {"ok": False, "code": "control.transport_unavailable"}
        _print_connection_payload(payload, json_output=args.json)
        return 2
    payload = {"ok": True, "connection": _connection_json(connection)}
    _print_connection_payload(payload, json_output=args.json)
    return 0


def _cmd_masterjet_connection_set(args: argparse.Namespace) -> int:
    try:
        current = load_config(args.config)
        connection = MasterjetConnection(
            transport=args.transport,
            endpoint=args.endpoint,
            timeout_seconds=args.timeout_seconds,
        )
        save_config(replace(current, masterjet=connection), args.config)
    except Exception:
        payload = {"ok": False, "code": "control.endpoint_invalid"}
        _print_connection_payload(payload, json_output=args.json)
        return 2
    payload = {"ok": True, "connection": _connection_json(connection)}
    _print_connection_payload(payload, json_output=args.json)
    return 0


def _openai_account_json(account: OpenAIControlAccount) -> dict[str, object]:
    return {
        "ref": account.ref,
        "label": account.label,
        "enabled": account.enabled,
        "local_profile_ref": account.local_profile_ref,
        "source_host_ref": account.source_host_ref,
        "auth_state": account.auth_state,
        "access_expires_at": (
            None
            if account.access_expires_at is None
            else _control_timestamp(account.access_expires_at)
        ),
        "credential_generation": account.credential_generation,
        "vault_projection_state": account.vault_projection_state,
        "usage_state": account.usage_state,
    }


def _local_openai_account_json(account: Any) -> dict[str, object]:
    state = "missing"
    if account.auth_json_path:
        path = Path(account.auth_json_path)
        try:
            validate_auth_json_file(path)
        except Exception:
            try:
                state = "invalid" if path.exists() else "missing"
            except OSError:
                state = "invalid"
        else:
            state = "ready"
    return {
        "account": account.id,
        "label": account.label,
        "local_auth_state": state,
        "auth_sync_required": account.auth_sync_required,
        "series-active": account.series_active,
    }


def _preserved_snapshot() -> ControlSnapshot:
    try:
        cached = load_control_snapshot(default_state_dir(), 30.0)
    except Exception:
        return ControlSnapshot()
    return ControlSnapshot() if cached.stale else cached.snapshot


def _save_openai_projection(accounts: tuple[OpenAIControlAccount, ...]) -> None:
    preserved = _preserved_snapshot()
    save_control_snapshot(
        default_state_dir(),
        ControlSnapshot(
            openai_accounts=accounts,
            google_accounts=preserved.google_accounts,
            google_projects=preserved.google_projects,
        ),
        observed_at=time.time(),
    )


def _cmd_masterjet_openai_accounts(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        stale = False
        try:
            value = _new_masterjet_client(
                config.masterjet, step_up_stdin=bool(getattr(args, "step_up_stdin", False))
            ).call("openai.accounts.list", {})
            if type(value) is not tuple or any(
                type(account) is not OpenAIControlAccount for account in value
            ):
                raise MasterjetClientError("control.response_invalid")
            accounts = value
            _save_openai_projection(accounts)
        except MasterjetClientError as live_error:
            cached = load_control_snapshot(default_state_dir(), 30.0)
            if cached.stale:
                raise live_error from None
            accounts = cached.snapshot.openai_accounts
            stale = True
        payload = {
            "stale": stale,
            "local_accounts": [_local_openai_account_json(item) for item in config.accounts],
            "accounts": [_openai_account_json(item) for item in accounts],
        }
    except MasterjetClientError as exc:
        payload = {"ok": False, "code": exc.code}
        print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        return 2
    except Exception:
        payload = {"ok": False, "code": "control.transport_unavailable"}
        print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        return 2
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0


def _cmd_masterjet_openai_routing_options(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        stale = False
        try:
            accounts = _new_masterjet_client(
                config.masterjet, step_up_stdin=bool(getattr(args, "step_up_stdin", False))
            ).call("openai.accounts.list", {})
        except MasterjetClientError as live_error:
            try:
                cached = load_control_snapshot(default_state_dir(), 30.0)
            except Exception:
                raise live_error from None
            if cached.stale:
                raise MasterjetClientError("control.cache_unavailable") from None
            accounts = cached.snapshot.openai_accounts
            stale = True
        if type(accounts) is not tuple or any(
            type(account) is not OpenAIControlAccount for account in accounts
        ):
            raise MasterjetClientError("control.response_invalid")
        by_profile: dict[str, OpenAIControlAccount] = {}
        for account in accounts:
            if account.local_profile_ref in by_profile:
                raise MasterjetClientError("control.response_invalid")
            by_profile[account.local_profile_ref] = account
        series = [
            {
                "prefix": account.series,
                "enabled": (account.id in by_profile and by_profile[account.id].enabled),
                "provider": "openai_chatgpt",
            }
            for account in config.accounts
            if account.series
        ]
    except MasterjetClientError as exc:
        print(json.dumps({"ok": False, "code": exc.code}, ensure_ascii=False, allow_nan=False))
        return 2
    except Exception:
        print(
            json.dumps(
                {"ok": False, "code": "control.transport_unavailable"},
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        return 2
    print(json.dumps({"stale": stale, "series": series}, ensure_ascii=False, allow_nan=False))
    return 0


def _cmd_google_accounts(args: argparse.Namespace) -> int:
    try:
        stale = False
        try:
            details = _new_google_controller_for_args(args).account_details()
            _save_google_projection(details)
        except Exception as live_error:
            cached_details = _load_google_projection_cache()
            if cached_details is None:
                raise live_error
            details = cached_details
            stale = True
    except Exception as exc:
        return _print_google_error(exc, json_output=args.json)
    if args.json:
        payload = _google_details_json(details, stale=stale)
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print("REF\tLABEL\tOAUTH\tGENERATION\tQUOTA")
        for detail in details:
            row = detail.account
            print(
                f"{row.ref}\t{row.label}\t{row.oauth_state}\t"
                f"{row.inventory_generation}\t{row.quota_state}"
            )
    return 0


def _cmd_google_add(args: argparse.Namespace) -> int:
    try:
        controller = _new_google_controller_for_args(args)
        imported = controller.import_oauth_client(args.account, args.oauth_client_json)
    except Exception as exc:
        return _print_google_error(exc, json_output=args.json)
    payload: dict[str, object] = {
        "account_ref": imported.account_ref,
        "generation": imported.generation,
        "status": imported.status,
        "ok": imported.status not in {"partial", "failed", "blocked"},
    }
    if payload["ok"] is False:
        payload["code"] = f"control.operation_{imported.status}"
    _print_google_payload(payload, json_output=args.json)
    return 0 if payload["ok"] is True else 2


def _cmd_google_oauth_begin(args: argparse.Namespace) -> int:
    try:
        transaction = _new_google_oauth_controller(args.config).oauth_begin(
            args.account, browser=args.browser
        )
    except Exception as exc:
        return _print_google_error(exc, json_output=args.json)
    _print_google_payload(
        _google_oauth_json(transaction),
        json_output=args.json,
    )
    return 0


def _cmd_google_inventory_refresh(args: argparse.Namespace) -> int:
    try:
        operation = _new_google_controller_for_args(args).inventory_refresh(args.account)
    except Exception as exc:
        return _print_google_error(exc, json_output=args.json)
    return _print_google_operation(operation, json_output=args.json)


def _cmd_google_provision_plan(args: argparse.Namespace) -> int:
    try:
        plan = _new_google_controller_for_args(args).provision_plan(args.account)
    except Exception as exc:
        return _print_google_error(exc, json_output=args.json)
    payload = {
        "account_ref": plan.account_ref,
        "plan_id": plan.plan_id,
        "expected_generation": plan.expected_generation,
        "plan_digest": plan.plan_digest,
        "expires_at": _control_timestamp(plan.expires_at),
        "step_count": plan.step_count,
        "projects": [
            {"project_name": item.project_name, "key_name": item.key_name} for item in plan.projects
        ],
    }
    _print_google_payload(payload, json_output=args.json)
    return 0


def _cmd_google_provision_apply(args: argparse.Namespace) -> int:
    if args.confirm is not True:
        return _print_google_error(
            GoogleAccountsError("confirmation_required"),
            json_output=args.json,
        )
    try:
        plan_digest = validate_google_plan_digest(args.plan_digest)
        operation = _new_google_controller_for_args(args).provision_apply(
            args.plan_id, account_ref=args.account, plan_digest=plan_digest
        )
    except Exception as exc:
        return _print_google_error(exc, json_output=args.json)
    return _print_google_operation(operation, json_output=args.json)


def _google_account_json(account: GoogleControlAccount) -> dict[str, object]:
    return {
        "ref": account.ref,
        "label": account.label,
        "enabled": account.enabled,
        "subject_bound": account.subject_bound,
        "oauth_state": account.oauth_state,
        "inventory_generation": account.inventory_generation,
        "quota_state": account.quota_state,
        "project_count": account.project_count,
        "billing_count": account.billing_count,
        "reload_state": account.reload_state,
    }


def _google_project_json(project: GoogleControlProject) -> dict[str, object]:
    return {
        "ref": project.ref,
        "project_name": project.project_name,
        "purpose": project.purpose,
        "key_name": project.key_name,
        "billing_ref": project.billing_ref,
        "status": project.status,
        "probe_state": project.probe_state,
        "quota_state": project.quota_state,
    }


def _google_details_json(
    details: tuple[GoogleAccountDetails, ...], *, stale: bool
) -> dict[str, object]:
    return {
        "stale": stale,
        "accounts": [_google_account_json(item.account) for item in details],
        "projects": {
            item.account.ref: [_google_project_json(project) for project in item.projects]
            for item in details
        },
    }


def _save_google_projection(details: tuple[GoogleAccountDetails, ...]) -> None:
    preserved = _preserved_snapshot()
    save_control_snapshot(
        default_state_dir(),
        ControlSnapshot(
            openai_accounts=preserved.openai_accounts,
            google_accounts=tuple(item.account for item in details),
            google_projects=tuple(
                GoogleControlProjectList(
                    schema_version=1,
                    account_ref=item.account.ref,
                    inventory_generation=item.account.inventory_generation,
                    projects=item.projects,
                )
                for item in details
            ),
        ),
        observed_at=time.time(),
    )


def _load_google_projection_cache() -> tuple[GoogleAccountDetails, ...] | None:
    try:
        cached = load_control_snapshot(default_state_dir(), 30.0)
    except Exception:
        return None
    if cached.stale:
        return None
    accounts = cached.snapshot.google_accounts
    projects = {item.account_ref: item for item in cached.snapshot.google_projects}
    if set(projects) != {item.ref for item in accounts}:
        return None
    result: list[GoogleAccountDetails] = []
    try:
        for account in accounts:
            projection = projects[account.ref]
            if (
                projection.inventory_generation != account.inventory_generation
                or len(projection.projects) != account.project_count
            ):
                return None
            result.append(GoogleAccountDetails(account, projection.projects))
    except (TypeError, ValueError):
        return None
    return tuple(result)


def _google_oauth_json(transaction: GoogleOAuthTransactionV1) -> dict[str, object]:
    return {
        "id": transaction.id,
        "account_ref": transaction.account_ref,
        "authorization_url": transaction.authorization_url,
        "expires_at": _control_timestamp(transaction.expires_at),
        "generation": transaction.generation,
    }


def _print_google_operation(operation: ControlOperation, *, json_output: bool) -> int:
    failed = operation.state in {"partial", "failed", "blocked"}
    payload: dict[str, object] = {
        "id": operation.id,
        "kind": operation.kind,
        "state": operation.state,
        "expected_generation": operation.expected_generation,
        "resulting_generation": operation.resulting_generation,
        "plan_digest": operation.plan_digest,
        "expires_at": _control_timestamp(operation.expires_at),
        "ok": not failed,
    }
    if failed:
        payload["code"] = f"control.operation_{operation.state}"
    _print_google_payload(payload, json_output=json_output)
    return 2 if failed else 0


def _print_google_payload(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def _control_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _print_google_error(exc: Exception, *, json_output: bool) -> int:
    code = exc.code if isinstance(exc, GoogleAccountsError) else "control.transport_unavailable"
    if json_output:
        print(json.dumps({"ok": False, "code": code}, ensure_ascii=False, allow_nan=False))
    else:
        print(f"Fehler: {code}", file=sys.stderr)
    return 2


def _cmd_login(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    account = resolve_account(config, args.account)
    login_account(account, config)
    mark_account_auth_sync_required(account.id, path=args.config)
    print("Auth-Sync: sync_required")
    return 0


def _cmd_account_auth_sync(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    account = resolve_account(config, args.account)
    client = _new_masterjet_client(
        config.masterjet, step_up_stdin=bool(getattr(args, "step_up_stdin", False))
    )
    try:
        result = sync_account_auth(account, client)
    except AuthSyncError as exc:
        if args.format == "json":
            print(json.dumps({"ok": False, "code": exc.code}, ensure_ascii=False))
        else:
            print(f"Fehler: {exc.code}", file=sys.stderr)
        return 2
    compare_and_clear_account_auth_sync_required(account, path=args.config)
    projection = {
        "account_ref": result.account_ref,
        "generation": result.generation,
        "status": result.status,
    }
    if args.format == "json":
        print(json.dumps(projection, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(f"Account: {result.account_ref}")
        print(f"Generation: {result.generation}")
        print(f"Status: {result.status}")
    return 0


def _cmd_reactivate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    account = resolve_account(config, args.account)
    try:
        result = dict(reactivate_account(account, browser=args.browser))
        if result.get("ok") is True:
            mark_account_auth_sync_required(account.id, path=args.config)
            result["auth_sync_required"] = True
    except ReactivationError as exc:
        result = {
            "ok": False,
            "account": account.id,
            "label": account.label,
            "browser": args.browser,
            "error": str(exc),
        }
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    elif result["ok"]:
        print(f"Account reaktiviert: {account.id} ({account.label})")
        print(f"Browserprofil: isoliert ({result['browser']})")
        print("Auth-Sync: sync_required")
    else:
        print(f"Reaktivierung fehlgeschlagen: {result['error']}")
    return 0 if result["ok"] else 2


def _cmd_account_manage(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    account = resolve_account(config, args.account)
    try:
        result = open_account_in_reactivation_browser(
            account,
            url=MANAGE_ACCOUNT_URL,
            browser=args.browser,
        )
    except ReactivationError as exc:
        result = {
            "ok": False,
            "account": account.id,
            "label": account.label,
            "browser": args.browser or account.reactivation_browser,
            "url": MANAGE_ACCOUNT_URL,
            "error": str(exc),
        }
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    elif result["ok"]:
        print(f"Account geöffnet: {account.id} ({account.label})")
        print("Browserprofil: isoliert (Reaktivierungsbrowser)")
    else:
        print(f"Account konnte nicht geöffnet werden: {result['error']}")
    return 0 if result["ok"] else 2


def _cmd_account_terminal(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    account = resolve_account(config, args.account)
    try:
        result = start_account_terminal(account)
    except TerminalError as exc:
        result = {
            "ok": False,
            "account": account.id,
            "label": account.label,
            "profile_dir": account.profile_dir,
            "error": str(exc),
        }
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    elif result["ok"]:
        print(f"Terminal gestartet: {account.id} ({account.label})")
        print(f"Profil: {result['profile_dir']}")
    else:
        print(f"Terminal konnte nicht gestartet werden: {result['error']}")
    return 0 if result["ok"] else 2


def _cmd_once(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    accounts = _select_accounts(config, args.account_ids)
    _validate_fetch_mode_flags(args)
    backend_override = _backend_override(args)
    direct = bool(args.direct or args.auth_json or backend_override == "direct")
    if direct:
        _validate_direct_auth_mapping(accounts, args.auth_json)
    usages = fetch_all(
        config,
        accounts,
        headed=args.headed,
        direct=direct,
        backend_override=backend_override,
        auth_json_path=args.auth_json,
        save_snapshots=True,
    )
    print(render_json(usages) if args.format == "json" else render_table(usages))
    return 0 if _all_usage_results_valid(usages, (account.id for account in accounts)) else 2


def _cmd_watch(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    accounts = _select_accounts(config, args.account_ids)
    _validate_fetch_mode_flags(args)
    if args.interval is not None:
        _validate_min_interval(args.interval)
    backend_override = _backend_override(args)
    direct = bool(args.direct or args.auth_json or backend_override == "direct")
    if direct:
        _validate_direct_auth_mapping(accounts, args.auth_json)
    watch(
        config,
        accounts,
        output=args.format,
        headed=args.headed,
        direct=direct,
        backend_override=backend_override,
        auth_json_path=args.auth_json,
        interval_seconds=args.interval,
    )
    return 0


def _cmd_watchdog(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    accounts = _select_accounts(config, args.account_ids)
    _validate_fetch_mode_flags(args)
    backend_override = _backend_override(args)
    direct = bool(args.direct or args.auth_json or backend_override == "direct")
    if direct:
        _validate_direct_auth_mapping(accounts, args.auth_json)
    usages = watchdog(
        config,
        accounts,
        output=args.format,
        headed=args.headed,
        direct=direct,
        backend_override=backend_override,
        auth_json_path=args.auth_json,
    )
    return (
        0
        if _all_usage_results_valid(
            usages,
            (account.id for account in accounts),
            predicate=_is_safe_watchdog_usage,
        )
        else 2
    )


def _cmd_policy_evaluate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    account = _resolve_policy_account(config, args.account, args.auth_json)
    usage = _usage_for_policy(account, auth_json_path=args.auth_json)
    policy = load_policy()
    paid_overage_allowed, policy_source = effective_paid_overage(
        policy,
        account=account.id,
        group=args.group,
        agent=args.agent,
        job=args.job,
    )
    result = evaluate_routing(
        usage,
        role=args.role,
        paid_overage_allowed=paid_overage_allowed,
        policy_source=policy_source,
        max_age_seconds=args.max_age,
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return _policy_decision_exit_code(result)


def _cmd_policy_set(args: argparse.Namespace) -> int:
    values = {"allow": True, "deny": False, "inherit": None}
    if args.scope == "global" and args.identifier:
        raise ValueError("--id is not allowed for global policy")
    if args.scope != "global" and not args.identifier:
        raise ValueError("--id is required for account, group, agent and job policy")
    policy = set_policy_rule(
        args.scope,
        args.identifier,
        values[args.value],
    )
    print(json.dumps(policy, ensure_ascii=True, sort_keys=True))
    return 0


def _cmd_policy_set_limits(args: argparse.Namespace) -> int:
    limits = {
        "hourly": args.hourly,
        "weekly": args.weekly,
        "monthly": args.monthly,
    }
    if args.scope == "global" and args.identifier:
        raise ValueError("--id is not allowed for global credit limits")
    if args.scope != "global" and not args.identifier:
        raise ValueError("--id is required for scoped credit limits")
    policy = set_credit_limits(limits, scope=args.scope, identifier=args.identifier)
    print(json.dumps(policy, ensure_ascii=True, sort_keys=True))
    return 0


def _cmd_policy_overview(args: argparse.Namespace) -> int:
    print(json.dumps(load_policy(), ensure_ascii=True, sort_keys=True))
    return 0


def _cmd_policy_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    policy = load_policy()
    decisions = {}
    for account in config.accounts:
        paid_overage_allowed, policy_source = effective_paid_overage(
            policy,
            account=account.id,
        )
        decisions[account.id] = evaluate_routing(
            _usage_for_policy(account),
            role=args.role,
            paid_overage_allowed=paid_overage_allowed,
            policy_source=policy_source,
            max_age_seconds=args.max_age,
        )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "policy": policy,
                "decisions": decisions,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return (
        0
        if decisions
        and all(_policy_decision_exit_code(decision) == 0 for decision in decisions.values())
        else 2
    )


def _policy_decision_exit_code(result: dict[str, Any]) -> int:
    decision = result.get("decision")
    return (
        0
        if isinstance(decision, str)
        and decision
        in {
            "spark",
            "main",
            "credits",
            "unchanged",
        }
        else 2
    )


def _cmd_spark_health(args: argparse.Namespace) -> int:
    if args.state is None and args.reason is not None:
        raise ValueError("--reason requires --state")
    if args.state is None:
        result = spark_health_status(args.backend_account_id)
    else:
        result = set_spark_health(
            args.backend_account_id,
            args.state,
            reason=args.reason,
        )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


def _usage_for_policy(
    account,
    *,
    auth_json_path: Path | None = None,
) -> AccountUsage:
    usage = load_current_usage(account.id) or load_usage_snapshot(account.id)
    if usage is None:
        return _invalid_policy_usage(account, "no usage snapshot")
    if (
        usage.backend_configured != account.backend
        or not isinstance(usage.backend_used, str)
        or usage.backend_used not in {"browser", "direct", "app-server"}
        or not backend_provenance_matches_configured(usage, account.backend)
    ):
        return _invalid_policy_usage(account, "usage backend provenance mismatch")
    required_auth_path = auth_json_path
    if required_auth_path is None and account.auth_json_path:
        required_auth_path = Path(account.auth_json_path)
    if required_auth_path is not None:
        try:
            auth_user_id, auth_account_id = auth_identity_from_file(required_auth_path)
        except (DirectAuthError, OSError, ValueError):
            return _invalid_policy_usage(account, "auth.json identity unavailable")
        if (
            not isinstance(usage.backend_used, str)
            or usage.backend_used not in {"direct", "app-server"}
            or not usage.backend_account_id
            or auth_identity_changed(
                before_user_id=usage.backend_user_id,
                before_account_id=usage.backend_account_id,
                after_user_id=auth_user_id,
                after_account_id=auth_account_id,
            )
        ):
            return _invalid_policy_usage(account, "usage auth identity mismatch")
    return usage


def _invalid_policy_usage(account, error: str) -> AccountUsage:
    return AccountUsage(
        account_id=account.id,
        label=account.label,
        captured_at=datetime.now(tz=UTC),
        status=AccountStatus.ERROR,
        error=error,
        backend_configured=account.backend,
        backend_used=account.backend,
        stale=True,
        cache_invalidated=True,
    )


def _resolve_policy_account(config, account_ref: str | None, auth_json: Path | None):
    if auth_json is None:
        if not account_ref:
            raise ValueError("policy evaluate requires ACCOUNT or --auth-json")
        return resolve_account(config, account_ref)
    _user_id, backend_account_id = auth_identity_from_file(auth_json)
    if not backend_account_id:
        raise ValueError("auth.json has no canonical backend account id")
    matches = []
    for account in config.accounts:
        try:
            _configured_user_id, configured_account_id = auth_identity_for_account(account)
        except (DirectAuthError, OSError, ValueError):
            continue
        if configured_account_id == backend_account_id:
            matches.append(account)
    if len(matches) != 1:
        raise ValueError("auth.json backend account id must match exactly one configured account")
    matched = matches[0]
    if account_ref and resolve_account(config, account_ref).id != matched.id:
        raise ValueError("ACCOUNT and --auth-json identify different accounts")
    return matched


def _cmd_probe(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    result = probe_account(
        resolve_account(config, args.account),
        config,
        headed=not args.headless,
        save_dir=args.save_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


def _cmd_diagnose(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    save_dir = args.save_dir
    if args.screenshot and save_dir is None:
        save_dir = Path("diagnose-output")
    result = diagnose_account(
        resolve_account(config, args.account),
        config,
        headed=args.headed,
        screenshot_dir=save_dir if args.screenshot else None,
        auth_json_path=args.auth_json,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if "error" not in result else 2


def _cmd_ingest(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    account = resolve_account(config, args.account)
    raw = _read_ingest_raw(args)
    payload = _payload_from_raw_ingest(raw)
    usage, path = ingest_and_save(
        config,
        account.id,
        payload,
        require_backend_identity=account.auth_json_path is not None,
    )
    print(render_table([usage]))
    print(f"Gespeichert: {path}")
    return 0 if _is_successful_usage(usage) else 2


def _cmd_latest(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    usages = load_latest_usages(config)
    if args.format == "json":
        print(render_json(usages))
    else:
        print(render_table(usages) if usages else "Keine Snapshots vorhanden.")
    return (
        0
        if _all_usage_results_valid(
            usages,
            (account.id for account in config.accounts),
            predicate=_is_safe_watchdog_usage,
        )
        else 2
    )


def _cmd_values(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    accounts = _select_accounts(config, args.account_ids)
    usages = _load_overview_usages(config, accounts)
    print(render_account_values(accounts, usages))
    return (
        0
        if _all_usage_results_valid(
            usages.values(),
            (account.id for account in accounts),
        )
        else 2
    )


def _cmd_health(args: argparse.Namespace) -> int:
    has_record = bool(args.record_component or args.record_event)
    if has_record:
        if args.clear or not args.record_component or not args.record_event:
            raise ValueError("health recording requires component and event without --clear")
        record_health_event(
            args.record_component,
            args.record_event,
            account=args.account,
            duration_ms=args.duration_ms,
            error_class=args.error_class,
        )
    elif args.clear:
        clear_health()
    payload = load_health()
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(f"Health-Ereignisse: {payload['event_count']}")
        for key, count in sorted(payload["event_counts"].items()):
            print(f"{key}: {count}")
    return 0


def _cmd_bridge_snippet(args: argparse.Namespace) -> int:
    _validate_port(args.port)
    _validate_min_interval(args.interval)
    config = load_config(args.config)
    account = resolve_account(config, args.account)
    endpoint = _bridge_endpoint(args.endpoint, args.port)
    print(render_bridge_snippet(account.id, endpoint=endpoint, interval_seconds=args.interval))
    return 0


def _cmd_bridge_extension(args: argparse.Namespace) -> int:
    _validate_port(args.port)
    _validate_min_interval(args.interval)
    config = load_config(args.config)
    account = resolve_account(config, args.account)
    endpoint = _bridge_endpoint(args.endpoint, args.port)
    output = args.output or default_state_dir() / "extensions" / account.id
    path = write_bridge_extension(
        account.id,
        output,
        endpoint=endpoint,
        interval_seconds=args.interval,
    )
    print(f"Extension erzeugt: {path}")
    print("Vivaldi: vivaldi://extensions -> Entwicklermodus -> Entpackte Erweiterung laden")
    return 0


def _cmd_bridge_server(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _validate_port(args.port)
    _validate_bridge_host(args.host, allow_remote=args.allow_remote)
    if (args.tls_cert is None) != (args.tls_key is None):
        raise ValueError("TLS requires both --tls-cert and --tls-key")
    if args.allow_remote and args.tls_cert is None:
        raise ValueError("remote bridge requires --tls-cert and --tls-key")
    run_bridge_server(
        config,
        host=args.host,
        port=args.port,
        config_path=args.config or default_config_path(),
        tls_cert=args.tls_cert,
        tls_key=args.tls_key,
    )
    return 0


def _cmd_service(args: argparse.Namespace) -> int:
    if args.action == "status":
        result = service_status()
    elif args.action == "disable":
        result = service_disable()
    elif args.action == "uninstall":
        result = service_uninstall()
    else:
        config = load_config(args.config)
        if args.action == "install":
            result = service_install(config, args.config)
        else:
            result = service_enable(config, args.config)
    if args.format == "json":
        print(render_service_json(result))
    else:
        print(
            "systemd: "
            f"installiert={'ja' if result.get('installed') else 'nein'}, "
            f"aktiviert={'ja' if result.get('enabled') else 'nein'}, "
            f"aktiv={'ja' if result.get('active') else 'nein'}"
        )
    return 0


def _cmd_paths(args: argparse.Namespace) -> int:
    print(f"config: {args.config or default_config_path()}")
    return 0


def _managed_service_sync_required(config_path: Path | None) -> bool:
    try:
        if not service_status().get("installed"):
            return False
        requested = (config_path or default_config_path()).expanduser().absolute()
        return managed_service_config_path() == requested
    except Exception:
        # Unknown service state must be treated conservatively: a later strict
        # sync may fail, so destructive cleanup must not run first.
        return True


def _sync_managed_service(config, config_path: Path | None, *, strict: bool = False) -> None:
    try:
        if not service_status().get("installed"):
            return
        requested = (config_path or default_config_path()).expanduser().absolute()
        if managed_service_config_path() != requested:
            return
        service_install(config, config_path)
    except Exception as exc:
        if strict:
            raise
        print(
            f"Warnung: systemd-Konfiguration nicht aktualisiert: {type(exc).__name__}",
            file=sys.stderr,
        )


def _default_root_command(argv: list[str]) -> list[str]:
    if not argv:
        return ["once"]
    if argv[0] in {"-h", "--help", "--version"}:
        return argv

    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"-h", "--help", "--version"}:
            return argv
        if token == "--config":
            index += 2
            continue
        if token == "--step-up-stdin":
            index += 1
            continue
        if token.startswith("--config="):
            index += 1
            continue
        break

    if index >= len(argv):
        return [*argv, "once"]
    if argv[index] in KNOWN_COMMANDS:
        return argv
    if argv[index].startswith("-"):
        return [*argv[:index], "once", *argv[index:]]
    return argv


def _select_accounts(config, account_ids: list[str] | None):
    if not config.accounts:
        raise ValueError("no accounts configured; run `codex-usage account add <id>` first")
    if not account_ids:
        return config.accounts
    selected = tuple(resolve_account(config, account_ref) for account_ref in account_ids)
    if len({account.id for account in selected}) != len(selected):
        raise ValueError("duplicate account selection")
    return selected


def _validate_direct_auth_mapping(accounts, auth_json_path: Path | None) -> None:
    account_list = list(accounts)
    if auth_json_path is not None and len(account_list) > 1:
        raise ValueError("--auth-json can only override direct auth for one selected account")
    if len(account_list) <= 1:
        if account_list and auth_json_path is not None:
            _validate_single_account_auth_override(account_list[0], auth_json_path)
        return
    missing = [account.id for account in account_list if not account.auth_json_path]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(
            "direct mode with multiple accounts requires per-account --auth-json; "
            f"missing: {joined}"
        )


def _validate_single_account_auth_override(account, auth_json_path: Path) -> None:
    if not account.auth_json_path:
        return
    try:
        expected_user_id, expected_account_id = auth_identity_for_account(account)
    except (DirectAuthError, OSError, ValueError) as exc:
        raise ValueError(
            "configured auth.json identity unavailable; cannot use --auth-json override"
        ) from exc
    if not (expected_user_id or expected_account_id):
        raise ValueError(
            "configured auth.json has no canonical identity; cannot use --auth-json override"
        )
    try:
        override_user_id, override_account_id = auth_identity_from_file(auth_json_path)
    except DirectAuthError:
        # Keep detailed auth-file errors in the fetch result instead of hiding
        # them behind a preflight validation failure.
        return
    if auth_identity_changed(
        before_user_id=expected_user_id,
        before_account_id=expected_account_id,
        after_user_id=override_user_id,
        after_account_id=override_account_id,
    ):
        raise ValueError("--auth-json identity does not match the selected account")


def _validate_fetch_mode_flags(args: argparse.Namespace) -> None:
    if not getattr(args, "headed", False):
        return
    if (
        getattr(args, "direct", False)
        or getattr(args, "auth_json", None) is not None
        or getattr(args, "backend", None) is not None
    ):
        raise ValueError("--headed cannot be combined with --direct, --auth-json or --backend")


def _backend_override(args: argparse.Namespace) -> str | None:
    backend = getattr(args, "backend", None)
    direct = bool(getattr(args, "direct", False))
    auth_json = getattr(args, "auth_json", None)
    if direct and backend not in (None, "direct"):
        raise ValueError("--direct cannot be combined with --backend app-server")
    if auth_json is not None and backend == "app-server":
        raise ValueError("--auth-json cannot be combined with --backend app-server")
    return backend


def _validate_bridge_host(host: str, *, allow_remote: bool) -> None:
    if allow_remote:
        return
    normalized = host.strip()
    if normalized == "localhost":
        return
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise ValueError(
            "bridge-server host must be loopback/localhost unless --allow-remote is set"
        ) from exc
    if not address.is_loopback:
        raise ValueError(
            "bridge-server host must be loopback/localhost unless --allow-remote is set"
        )


def _bridge_endpoint(endpoint: str | None, port: int) -> str:
    _validate_port(port)
    if endpoint is None:
        return f"http://127.0.0.1:{port}/ingest"
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError("--endpoint must be an absolute HTTP(S) URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or (parsed_port is not None and not 1 <= parsed_port <= 65535)
        or not parsed.path.startswith("/")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("--endpoint must be an absolute HTTP(S) URL without credentials/query")
    return endpoint


def _validate_port(port: int) -> None:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("--port must be between 1 and 65535")


def _validate_min_interval(interval_seconds: int) -> None:
    if type(interval_seconds) is not int or interval_seconds < 60:
        raise ValueError("--interval must be at least 60 seconds")


def _load_overview_usages(config, accounts=None):
    selected = tuple(accounts or config.accounts)
    fetched = tuple(fetch_all(config, selected, save_snapshots=True))
    expected_ids = tuple(account.id for account in selected)
    try:
        result_ids = tuple(usage.account_id for usage in fetched)
        identities_match = (
            len(result_ids) == len(expected_ids)
            and len(set(result_ids)) == len(result_ids)
            and len(set(expected_ids)) == len(expected_ids)
            and set(result_ids) == set(expected_ids)
        )
    except (AttributeError, TypeError, ValueError):
        identities_match = False
    if not identities_match:
        raise ValueError("usage result identity mismatch")
    return {usage.account_id: usage for usage in fetched}


def _overview_usage_json(
    usage: AccountUsage | None,
    *,
    expected_backend: str | None = None,
) -> dict | None:
    if usage is None:
        return None
    usage = _safe_usage_for_display(usage, expected_backend=expected_backend)
    serialized = usage.as_dict()
    return {
        "captured_at": serialized["captured_at"],
        "five_hour": _overview_window_json(serialized["five_hour"]),
        "weekly": _overview_window_json(serialized["weekly"]),
        "main": serialized["main"],
        "models": serialized["models"],
        "status": serialized["status"],
        "error": serialized["error"],
        "stale": serialized["stale"],
    }


def _overview_window_json(window: dict[str, Any] | None) -> dict | None:
    if window is None:
        return None
    return {
        "used": window["used"],
        "limit": window["limit"],
        "remaining": window["remaining"],
        "percent": window["percent"],
        "reset_at": window["reset_at"],
    }


def _is_successful_usage(usage: AccountUsage) -> bool:
    if not _has_valid_usage_provenance(usage):
        return False
    if (
        usage.status is not AccountStatus.OK
        or usage.error is not None
        or usage.stale is not False
        or usage.cache_invalidated is not False
    ):
        return False
    try:
        if usage.main is not None:
            return usage.main.has_valid_usage
        return any(
            window is not None and window.has_usage_value
            for window in (usage.five_hour, usage.weekly)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _has_valid_usage_provenance(usage: AccountUsage) -> bool:
    if not isinstance(usage, AccountUsage):
        return False
    configured = usage.backend_configured
    used = usage.backend_used
    if (
        not isinstance(configured, str)
        or configured not in {"direct", "app-server"}
        or not isinstance(used, str)
        or used not in {"browser", "direct", "app-server"}
    ):
        return False
    try:
        return backend_provenance_matches_configured(usage, configured)
    except (AttributeError, TypeError, ValueError):
        return False


def _is_safe_watchdog_usage(usage: AccountUsage) -> bool:
    if not _has_valid_usage_provenance(usage):
        return False
    if usage.cache_invalidated is not False or usage.stale is not False:
        return False
    if usage.status is AccountStatus.BLOCKED:
        return any(
            value not in (None, "")
            for value in (usage.blocked_until, usage.blocked_reason, usage.error)
        )
    return _is_successful_usage(usage)


def _all_usage_results_valid(
    usages,
    expected_account_ids,
    *,
    predicate=_is_successful_usage,
) -> bool:
    results = list(usages)
    expected = tuple(expected_account_ids)
    if len(results) != len(expected):
        return False
    try:
        result_ids = tuple(usage.account_id for usage in results)
        if len(set(result_ids)) != len(result_ids):
            return False
        if len(set(expected)) != len(expected):
            return False
        if set(result_ids) != set(expected):
            return False
    except (AttributeError, TypeError):
        return False
    return all(predicate(usage) for usage in results)


def _validate_profile_delete_target(path: Path, *, force: bool) -> Path:
    assert_no_symlink_ancestors(path, label="profile path")
    if path.is_symlink():
        raise ValueError(f"profile path must not be a symlink: {path}")
    resolved = path.resolve()
    home = Path.home().resolve()
    forbidden = {
        Path("/").resolve(),
        home,
        home / ".config",
        home / ".local",
        home / ".local/share",
    }
    if resolved in forbidden:
        raise ValueError(f"refusing to delete unsafe profile path: {resolved}")
    if not path.exists():
        return resolved
    if not path.is_dir():
        raise ValueError(f"profile path is not a directory: {path}")

    marker = path / ".codex-usage-profile"
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        raise ValueError(f"profile marker must be a regular file: {marker}")
    marker_exists = marker.exists()
    default_root = (default_state_dir() / "profiles").expanduser().resolve()
    in_default_root = _is_relative_to(resolved, default_root)
    if not force and not marker_exists and not in_default_root:
        raise ValueError(
            "refusing to delete profile outside the default profile root without "
            "--force-delete-profile"
        )
    return resolved


@dataclass
class _ProfileDeleteTransaction:
    path: Path
    quarantine: Path | None
    profile_state: str
    locks: ExitStack
    rollbackable: bool = True

    def commit(self) -> str:
        if self.quarantine is not None:

            def mark_nonrollbackable(_function, _path, exception_info):
                self.rollbackable = False
                raise exception_info[1]

            shutil.rmtree(self.quarantine, onerror=mark_nonrollbackable)
        self.locks.close()
        return self.profile_state

    def rollback(self) -> None:
        try:
            if self.quarantine is not None:
                if not self.rollbackable:
                    raise OSError("profile deletion cannot be rolled back after partial cleanup")
                if self.path.exists() or self.path.is_symlink():
                    raise OSError(f"profile path appeared during rollback: {self.path}")
                self.quarantine.rename(self.path)
        finally:
            self.locks.close()


def _delete_profile_dir(
    path: Path,
    *,
    browser: str,
    force: bool,
    defer_commit: bool = False,
) -> str | _ProfileDeleteTransaction:
    path = _validate_profile_delete_target(path, force=force)
    if not path.exists():
        return "fehlt"
    locks = ExitStack()
    try:
        for target in _profile_delete_lock_targets(path, browser=browser):
            locks.enter_context(_profile_lock(target, lock_root=path))
        quarantine = Path(tempfile.mkdtemp(prefix=f".{path.name}.delete-", dir=path.parent))
        quarantine.rmdir()
        path.rename(quarantine)
    except BaseException:
        locks.close()
        raise

    transaction = _ProfileDeleteTransaction(path, quarantine, "geloescht", locks)
    if defer_commit:
        return transaction
    try:
        return transaction.commit()
    except BaseException as primary_error:
        try:
            transaction.rollback()
        except BaseException as rollback_error:
            raise BaseExceptionGroup(
                "profile deletion rollback failed",
                [primary_error, rollback_error],
            ) from None
        raise


def _profile_delete_lock_targets(path: Path, *, browser: str) -> tuple[Path, ...]:
    targets: list[Path] = []
    browser_profile = path / _profile_browser_dir(browser)
    if browser_profile.is_symlink():
        raise ValueError(f"browser profile path must not be a symlink: {browser_profile}")
    if browser_profile.exists():
        if not browser_profile.is_dir():
            raise ValueError(f"browser profile path must be a directory: {browser_profile}")
    targets.append(browser_profile)

    oauth_root = path / "oauth"
    if oauth_root.is_symlink():
        raise ValueError(f"OAuth profile root must not be a symlink: {oauth_root}")
    if oauth_root.exists():
        if not oauth_root.is_dir():
            raise ValueError(f"OAuth profile root must be a directory: {oauth_root}")
        for index, candidate in enumerate(oauth_root.iterdir(), start=1):
            if index > MAX_PROFILE_OAUTH_ENTRIES:
                raise ValueError(
                    f"too many OAuth browser profiles: max {MAX_PROFILE_OAUTH_ENTRIES}"
                )
            if candidate.is_symlink():
                raise ValueError(f"OAuth browser profile must not be a symlink: {candidate}")
            if candidate.is_dir():
                targets.append(candidate)
            elif candidate.exists():
                raise ValueError(f"OAuth browser profile must be a directory: {candidate}")
    return tuple(sorted(set(targets), key=str))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _payload_from_raw_ingest(raw: str) -> dict:
    stripped = raw.strip()
    if not stripped:
        return {"bodyText": ""}
    try:
        payload = loads_strict(stripped)
    except ValueError:
        return {"bodyText": raw}
    if isinstance(payload, dict):
        return payload
    return {"bodyText": raw}


def _read_ingest_raw(args: argparse.Namespace) -> str:
    if args.stdin:
        return _read_ingest_stdin()

    path = args.file
    text, _ = read_private_text(
        path,
        regular_label="ingest file",
        read_label="ingest file",
        max_bytes=MAX_INGEST_BYTES,
        too_large_label="ingest payload",
        invalid_utf8_label="ingest file",
    )
    return text


def _read_ingest_stdin() -> str:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        raw_bytes = buffer.read(MAX_INGEST_BYTES + 1)
        if len(raw_bytes) > MAX_INGEST_BYTES:
            raise ValueError(f"ingest payload too large; max {MAX_INGEST_BYTES} bytes")
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("ingest stdin is not valid UTF-8") from exc

    raw = sys.stdin.read(MAX_INGEST_BYTES + 1)
    try:
        byte_length = len(raw.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("ingest stdin is not valid UTF-8") from exc
    if byte_length > MAX_INGEST_BYTES:
        raise ValueError(f"ingest payload too large; max {MAX_INGEST_BYTES} bytes")
    return raw


if __name__ == "__main__":
    raise SystemExit(main())
