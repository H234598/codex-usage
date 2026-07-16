from __future__ import annotations

import argparse
import ipaddress
import json
import shutil
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .bridge import (
    MAX_INGEST_BYTES,
    ingest_and_save,
    load_latest_usages,
    render_bridge_snippet,
    revoke_bridge_token,
    run_bridge_server,
    write_bridge_extension,
)
from .browser import diagnose_account, login_account, probe_account
from .config import (
    SUPPORTED_BACKENDS,
    SUPPORTED_BROWSERS,
    add_or_update_account,
    default_config_path,
    default_state_dir,
    load_config,
    remove_account,
    resolve_account,
)
from .direct import (
    DirectAuthError,
    auth_identity_changed,
    auth_identity_for_account,
    auth_identity_from_file,
)
from .health import clear_health, load_health, record_health_event
from .json_utils import loads_strict
from .models import AccountStatus, AccountUsage
from .private_io import read_private_text
from .reactivate import REACTIVATION_BROWSERS, ReactivationError, reactivate_account
from .render import render_account_overview, render_account_values, render_json, render_table
from .routing import (
    DEFAULT_MAX_USAGE_AGE_SECONDS,
    effective_paid_overage,
    evaluate_routing,
    load_policy,
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
from .state import load_current_usage, load_usage_snapshot, remove_account_state

COMMAND_OVERVIEW = """\
Komplette Command-Line-Usage:

Globale Optionen:
  codex-usage [--config CONFIG] COMMAND ...
  codex-usage [--config CONFIG]

Accounts:
  codex-usage account add ACCOUNT_ID [--label LABEL] [--profile-dir DIR]
                                   [--browser BROWSER] [--auth-json PATH]
                                   [--backend direct|app-server]
  codex-usage account backend ACCOUNT direct|app-server [--format table|json]
  codex-usage account overview [--format table|json] [--config-only]
  codex-usage account delete ACCOUNT [--delete-profile] [--force-delete-profile]

Login und Reaktivierung:
  codex-usage login ACCOUNT
  codex-usage reactivate ACCOUNT [--browser auto|vivaldi|chromium|firefox]
                                 [--format table|json]

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
                              [--max-age SEKUNDEN]
  codex-usage policy set global allow|deny|inherit [--format json]
  codex-usage policy set account|group|agent|job allow|deny|inherit --id ID
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

Stabilität und Diagnose:
  codex-usage health [--format table|json] [--clear]

Browser-Bridge:
  codex-usage bridge-snippet ACCOUNT [--port PORT] [--interval SEKUNDEN]
  codex-usage bridge-extension ACCOUNT [--output DIR] [--port PORT] [--interval SEKUNDEN]
  codex-usage bridge-server [--host HOST] [--port PORT] [--allow-remote]

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

KNOWN_COMMANDS = {
    "account",
    "login",
    "reactivate",
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
    "health",
    "bridge-snippet",
    "bridge-extension",
    "bridge-server",
    "service",
    "paths",
}


def main(argv: list[str] | None = None) -> int:
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
    sub = parser.add_subparsers(dest="command", required=True)

    account = sub.add_parser("account", help="Accounts verwalten")
    account_sub = account.add_subparsers(dest="account_command", required=True)
    add = account_sub.add_parser("add", help="Account-Profil anlegen oder aktualisieren")
    add.add_argument("account_id")
    add.add_argument("--label")
    add.add_argument("--profile-dir")
    add.add_argument(
        "--browser",
        choices=SUPPORTED_BROWSERS,
        help="Browser fuer Login und Polling, Standard: firefox",
    )
    add.add_argument("--auth-json", type=Path, help="Codex auth.json fuer direkten Abruf")
    add.add_argument("--backend", choices=SUPPORTED_BACKENDS)
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
    delete.set_defaults(func=_cmd_account_delete)

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
    policy_set.add_argument(
        "scope", choices=("global", "account", "group", "agent", "job")
    )
    policy_set.add_argument("value", choices=("allow", "deny", "inherit"))
    policy_set.add_argument("--id", dest="identifier")
    policy_set.add_argument("--format", choices=("json",), default="json")
    policy_set.set_defaults(func=_cmd_policy_set)

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
    snippet.add_argument("--port", type=int, default=8765)
    snippet.add_argument("--interval", type=int, default=300)
    snippet.set_defaults(func=_cmd_bridge_snippet)

    extension = sub.add_parser(
        "bridge-extension",
        help="Entpackte Vivaldi/Chromium-Bridge-Extension erzeugen",
    )
    extension.add_argument("account", help="Account-ID oder eindeutiges Label")
    extension.add_argument("--output", type=Path)
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
    updated, account = add_or_update_account(
        args.account_id,
        label=args.label,
        profile_dir=args.profile_dir,
        browser=args.browser,
        auth_json_path=str(args.auth_json) if args.auth_json else None,
        backend=args.backend,
        path=args.config,
    )
    _sync_managed_service(updated, args.config)
    print(f"Account gespeichert: {account.id} ({account.label})")
    print(f"Profil: {account.profile_dir}")
    print(f"Browser: {account.browser}")
    print(f"Backend: {account.backend}")
    if account.auth_json_path:
        print(f"Auth JSON: {account.auth_json_path}")
    print(f"Login: codex-usage login {account.id}")
    return 0


def _cmd_account_overview(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    usages = {} if args.config_only else _load_overview_usages(config)
    if args.format == "json":
        usages_by_account = {usage.account_id: usage for usage in usages.values()}
        payload = {
            "accounts": [
                {
                    "id": account.id,
                    "label": account.label,
                    "browser": account.browser,
                    "backend": account.backend,
                    "backend_used": usages_by_account.get(account.id).backend_used
                    if usages_by_account.get(account.id)
                    else None,
                    "fallback_reason": usages_by_account.get(account.id).fallback_reason
                    if usages_by_account.get(account.id)
                    else None,
                    "usage": _overview_usage_json(usages_by_account.get(account.id)),
                }
                for account in config.accounts
            ]
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    print(render_account_overview(config, args.config or default_config_path(), usages))
    return 0


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


def _cmd_account_delete(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    account = resolve_account(config, args.account)
    profile_path = Path(account.profile_dir).expanduser()
    profile_state = None
    if args.delete_profile:
        _validate_profile_delete_target(profile_path, force=args.force_delete_profile)

    remove_account_state(account.id)
    revoke_bridge_token(account.id)
    updated, _ = remove_account(account.id, path=args.config)
    _sync_managed_service(updated, args.config)
    if args.delete_profile:
        profile_state = _delete_profile_dir(profile_path, force=args.force_delete_profile)
    print(f"Account geloescht: {account.id} ({account.label})")
    if args.delete_profile:
        print(f"Profil: {profile_state} {profile_path}")
    else:
        print(f"Profil behalten: {profile_path}")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    login_account(resolve_account(config, args.account), config)
    return 0


def _cmd_reactivate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    account = resolve_account(config, args.account)
    try:
        result = reactivate_account(account, browser=args.browser)
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
    else:
        print(f"Reaktivierung fehlgeschlagen: {result['error']}")
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
    return 0 if all(_is_successful_usage(usage) for usage in usages) else 2


def _cmd_watch(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    accounts = _select_accounts(config, args.account_ids)
    _validate_fetch_mode_flags(args)
    if args.interval is not None and args.interval < 60:
        raise ValueError("--interval must be at least 60 seconds")
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
    return 0 if all(usage.status != AccountStatus.ERROR for usage in usages) else 2


def _cmd_policy_evaluate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    account = _resolve_policy_account(config, args.account, args.auth_json)
    usage = _usage_for_policy(account)
    if args.auth_json is not None:
        _auth_user_id, auth_account_id = auth_identity_from_file(args.auth_json)
        if usage.backend_account_id and usage.backend_account_id != auth_account_id:
            raise ValueError("usage snapshot belongs to another backend account id")
        usage = replace(usage, backend_account_id=auth_account_id)
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
    return 0


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
    return 0


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


def _usage_for_policy(account) -> AccountUsage:
    usage = load_current_usage(account.id) or load_usage_snapshot(account.id)
    if usage is not None:
        return usage
    return AccountUsage(
        account_id=account.id,
        label=account.label,
        captured_at=datetime.now(tz=UTC),
        status=AccountStatus.ERROR,
        error="no usage snapshot",
        backend_configured=account.backend,
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
        raise ValueError(
            "auth.json backend account id must match exactly one configured account"
        )
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
    return 0


def _cmd_values(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    accounts = _select_accounts(config, args.account_ids)
    usages = _load_overview_usages(config, accounts)
    print(render_account_values(accounts, usages))
    return 0 if all(_is_successful_usage(usage) for usage in usages.values()) else 2


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
    endpoint = f"http://127.0.0.1:{args.port}/ingest"
    print(render_bridge_snippet(account.id, endpoint=endpoint, interval_seconds=args.interval))
    return 0


def _cmd_bridge_extension(args: argparse.Namespace) -> int:
    _validate_port(args.port)
    _validate_min_interval(args.interval)
    config = load_config(args.config)
    account = resolve_account(config, args.account)
    endpoint = f"http://127.0.0.1:{args.port}/ingest"
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
    run_bridge_server(
        config,
        host=args.host,
        port=args.port,
        config_path=args.config or default_config_path(),
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


def _sync_managed_service(config, config_path: Path | None) -> None:
    try:
        if not service_status().get("installed"):
            return
        requested = (config_path or default_config_path()).expanduser().absolute()
        if managed_service_config_path() != requested:
            return
        service_install(config, config_path)
    except Exception as exc:
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
    return tuple(resolve_account(config, account_ref) for account_ref in account_ids)


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
        override_user_id, override_account_id = auth_identity_from_file(auth_json_path)
    except DirectAuthError:
        # Keep detailed auth-file errors in the fetch result instead of hiding
        # them behind a preflight validation failure.
        return
    if not (expected_user_id or expected_account_id):
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
        raise ValueError(
            "--headed cannot be combined with --direct, --auth-json or --backend"
        )


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


def _validate_port(port: int) -> None:
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
    ):
        raise ValueError("--port must be between 1 and 65535")


def _validate_min_interval(interval_seconds: int) -> None:
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, int)
        or interval_seconds < 60
    ):
        raise ValueError("--interval must be at least 60 seconds")


def _load_overview_usages(config, accounts=None):
    selected = tuple(accounts or config.accounts)
    fetched = fetch_all(config, selected, save_snapshots=True)
    return {usage.account_id: usage for usage in fetched}


def _overview_usage_json(usage: AccountUsage | None) -> dict | None:
    if usage is None:
        return None
    serialized = usage.as_dict()
    return {
        "captured_at": usage.captured_at.isoformat(),
        "five_hour": _overview_window_json(usage.five_hour),
        "weekly": _overview_window_json(usage.weekly),
        "main": serialized["main"],
        "models": serialized["models"],
        "status": usage.status.value,
        "error": usage.error,
        "stale": usage.stale,
    }


def _overview_window_json(window) -> dict | None:
    if window is None:
        return None
    return {
        "used": window.used,
        "limit": window.limit,
        "remaining": window.remaining,
        "percent": window.percent,
        "reset_at": window.reset_at.isoformat() if window.reset_at else None,
    }


def _is_successful_usage(usage: AccountUsage) -> bool:
    return usage.status == AccountStatus.OK and usage.error is None


def _validate_profile_delete_target(path: Path, *, force: bool) -> None:
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
        return
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


def _delete_profile_dir(path: Path, *, force: bool) -> str:
    _validate_profile_delete_target(path, force=force)
    if not path.exists():
        return "fehlt"
    shutil.rmtree(path)
    return "geloescht"


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
