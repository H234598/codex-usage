from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .bridge import (
    ingest_and_save,
    load_latest_usages,
    render_bridge_snippet,
    run_bridge_server,
    write_bridge_extension,
)
from .browser import diagnose_account, login_account, probe_account
from .config import (
    SUPPORTED_BROWSERS,
    add_or_update_account,
    default_config_path,
    default_state_dir,
    load_config,
    remove_account,
    resolve_account,
)
from .render import render_account_overview, render_json, render_table
from .scheduler import fetch_all, watch

COMMAND_OVERVIEW = """\
Befehle:
  codex-usage account add ACCOUNT_ID [--label LABEL] [--profile-dir DIR] [--browser BROWSER]
  codex-usage account overview
  codex-usage account delete ACCOUNT [--delete-profile] [--force-delete-profile]
  codex-usage login ACCOUNT
  codex-usage once [--account ACCOUNT] [--format table|json] [--headed]
  codex-usage watch [--account ACCOUNT] [--format table|json] [--interval SEKUNDEN] [--headed]
  codex-usage probe ACCOUNT [--headless] [--save-dir DIR]
  codex-usage diagnose ACCOUNT [--headed] [--screenshot] [--save-dir DIR] [--auth-json PATH]
  codex-usage ingest ACCOUNT (--stdin | --file FILE)
  codex-usage latest
  codex-usage bridge-snippet ACCOUNT [--port PORT] [--interval SEKUNDEN]
  codex-usage bridge-extension ACCOUNT [--output DIR] [--port PORT] [--interval SEKUNDEN]
  codex-usage bridge-server [--host HOST] [--port PORT]
  codex-usage paths

ACCOUNT kann eine Account-ID oder ein eindeutiges Label sein.
"""


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
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
    add.set_defaults(func=_cmd_account_add)
    overview = account_sub.add_parser("overview", help="Account-Uebersicht anzeigen")
    overview.set_defaults(func=_cmd_account_overview)
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

    once = sub.add_parser("once", help="Alle oder einzelne Accounts einmal auslesen")
    once.add_argument("--account", action="append", dest="account_ids")
    once.add_argument("--format", choices=("table", "json"), default="table")
    once.add_argument("--headed", action="store_true", help="Browser sichtbar starten")
    once.set_defaults(func=_cmd_once)

    watch_cmd = sub.add_parser("watch", help="Alle 5 Minuten fortlaufend auslesen")
    watch_cmd.add_argument("--account", action="append", dest="account_ids")
    watch_cmd.add_argument("--format", choices=("table", "json"), default="table")
    watch_cmd.add_argument("--interval", type=int, default=None)
    watch_cmd.add_argument("--headed", action="store_true", help="Browser sichtbar starten")
    watch_cmd.set_defaults(func=_cmd_watch)

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
    bridge.set_defaults(func=_cmd_bridge_server)

    paths = sub.add_parser("paths", help="Standardpfade anzeigen")
    paths.set_defaults(func=_cmd_paths)
    return parser


def _cmd_account_add(args: argparse.Namespace) -> int:
    _, account = add_or_update_account(
        args.account_id,
        label=args.label,
        profile_dir=args.profile_dir,
        browser=args.browser,
        path=args.config,
    )
    print(f"Account gespeichert: {account.id} ({account.label})")
    print(f"Profil: {account.profile_dir}")
    print(f"Browser: {account.browser}")
    print(f"Login: codex-usage login {account.id}")
    return 0


def _cmd_account_overview(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(render_account_overview(config, args.config or default_config_path()))
    return 0


def _cmd_account_delete(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    account = resolve_account(config, args.account)
    profile_path = Path(account.profile_dir).expanduser()
    if args.delete_profile:
        _validate_profile_delete_target(profile_path, force=args.force_delete_profile)

    remove_account(account.id, path=args.config)
    print(f"Account geloescht: {account.id} ({account.label})")
    if args.delete_profile:
        state = _delete_profile_dir(profile_path)
        print(f"Profil: {state} {profile_path}")
    else:
        print(f"Profil behalten: {profile_path}")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    login_account(resolve_account(config, args.account), config)
    return 0


def _cmd_once(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    accounts = _select_accounts(config, args.account_ids)
    usages = fetch_all(config, accounts, headed=args.headed)
    print(render_json(usages) if args.format == "json" else render_table(usages))
    return 0 if all(usage.error is None for usage in usages) else 2


def _cmd_watch(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    accounts = _select_accounts(config, args.account_ids)
    if args.interval is not None and args.interval < 60:
        raise ValueError("--interval must be at least 60 seconds")
    watch(
        config,
        accounts,
        output=args.format,
        headed=args.headed,
        interval_seconds=args.interval,
    )
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    result = probe_account(
        resolve_account(config, args.account),
        config,
        headed=not args.headless,
        save_dir=args.save_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
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
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if "error" not in result else 2


def _cmd_ingest(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    raw = sys.stdin.read() if args.stdin else args.file.read_text(encoding="utf-8")
    payload = _payload_from_raw_ingest(raw)
    usage, path = ingest_and_save(config, args.account, payload)
    print(render_table([usage]))
    print(f"Gespeichert: {path}")
    return 0 if usage.error is None else 2


def _cmd_latest(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    usages = load_latest_usages(config)
    if args.format == "json":
        print(render_json(usages))
    else:
        print(render_table(usages) if usages else "Keine Snapshots vorhanden.")
    return 0


def _cmd_bridge_snippet(args: argparse.Namespace) -> int:
    endpoint = f"http://127.0.0.1:{args.port}/ingest"
    print(render_bridge_snippet(args.account, endpoint=endpoint, interval_seconds=args.interval))
    return 0


def _cmd_bridge_extension(args: argparse.Namespace) -> int:
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
    run_bridge_server(config, host=args.host, port=args.port)
    return 0


def _cmd_paths(args: argparse.Namespace) -> int:
    print(f"config: {args.config or default_config_path()}")
    return 0


def _select_accounts(config, account_ids: list[str] | None):
    if not config.accounts:
        raise ValueError("no accounts configured; run `codex-usage account add <id>` first")
    if not account_ids:
        return config.accounts
    return tuple(resolve_account(config, account_ref) for account_ref in account_ids)


def _validate_profile_delete_target(path: Path, *, force: bool) -> None:
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

    marker_exists = (path / ".codex-usage-profile").exists()
    default_root = (default_state_dir() / "profiles").expanduser().resolve()
    in_default_root = _is_relative_to(resolved, default_root)
    if not force and not marker_exists and not in_default_root:
        raise ValueError(
            "refusing to delete profile outside the default profile root without "
            "--force-delete-profile"
        )


def _delete_profile_dir(path: Path) -> str:
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
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return {"bodyText": raw}
    if isinstance(payload, dict):
        return payload
    return {"bodyText": raw}


if __name__ == "__main__":
    raise SystemExit(main())
