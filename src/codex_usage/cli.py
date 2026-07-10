from __future__ import annotations

import argparse
import ipaddress
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .bridge import (
    MAX_INGEST_BYTES,
    ingest_and_save,
    load_latest_usages,
    render_bridge_snippet,
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
from .json_utils import loads_strict
from .models import AccountStatus, AccountUsage
from .private_io import read_private_text
from .reactivate import REACTIVATION_BROWSERS, ReactivationError, reactivate_account
from .render import render_account_overview, render_account_values, render_json, render_table
from .scheduler import fetch_all, watch, watchdog
from .service import (
    render_service_json,
    service_disable,
    service_enable,
    service_install,
    service_status,
    service_uninstall,
)

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
  codex-usage account overview [--format table|json]
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

Analyse und Diagnose:
  codex-usage probe ACCOUNT [--headless] [--save-dir DIR]
  codex-usage diagnose ACCOUNT [--headed] [--screenshot] [--save-dir DIR]
                              [--auth-json PATH]

Gespeicherte Werte und manuelle Aufnahme:
  codex-usage ingest ACCOUNT (--stdin | --file FILE)
  codex-usage latest [--format table|json]
  codex-usage values [--account ACCOUNT]

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
    "probe",
    "diagnose",
    "ingest",
    "latest",
    "values",
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
    if args.format == "json":
        usages = {usage.account_id: usage for usage in load_latest_usages(config)}
        payload = {
            "accounts": [
                {
                    "id": account.id,
                    "label": account.label,
                    "browser": account.browser,
                    "backend": account.backend,
                    "backend_used": usages.get(account.id).backend_used
                    if usages.get(account.id)
                    else None,
                    "fallback_reason": usages.get(account.id).fallback_reason
                    if usages.get(account.id)
                    else None,
                }
                for account in config.accounts
            ]
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    usages = _load_overview_usages(config)
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
    direct = bool(args.direct or args.auth_json)
    backend_override = _backend_override(args)
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
    if args.interval is not None and args.interval < 60:
        raise ValueError("--interval must be at least 60 seconds")
    direct = bool(args.direct or args.auth_json)
    backend_override = _backend_override(args)
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
    direct = bool(args.direct or args.auth_json)
    backend_override = _backend_override(args)
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
    raw = _read_ingest_raw(args)
    payload = _payload_from_raw_ingest(raw)
    usage, path = ingest_and_save(config, args.account, payload)
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


def _cmd_bridge_snippet(args: argparse.Namespace) -> int:
    _validate_port(args.port)
    _validate_min_interval(args.interval)
    endpoint = f"http://127.0.0.1:{args.port}/ingest"
    print(render_bridge_snippet(args.account, endpoint=endpoint, interval_seconds=args.interval))
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
    run_bridge_server(config, host=args.host, port=args.port)
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
        if service_status().get("installed"):
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
        return
    missing = [account.id for account in account_list if not account.auth_json_path]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(
            "direct mode with multiple accounts requires per-account --auth-json; "
            f"missing: {joined}"
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
