from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .browser import login_account, probe_account
from .config import (
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
  codex-usage account add ACCOUNT_ID [--label LABEL] [--profile-dir DIR]
  codex-usage account list
  codex-usage account overview
  codex-usage account delete ACCOUNT [--delete-profile] [--force-delete-profile]
  codex-usage login ACCOUNT
  codex-usage once [--account ACCOUNT] [--format table|json] [--headed]
  codex-usage watch [--account ACCOUNT] [--format table|json] [--interval SEKUNDEN] [--headed]
  codex-usage probe ACCOUNT [--headless] [--save-dir DIR]
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
    add.set_defaults(func=_cmd_account_add)
    listing = account_sub.add_parser("list", help="Accounts anzeigen")
    listing.set_defaults(func=_cmd_account_list)
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

    paths = sub.add_parser("paths", help="Standardpfade anzeigen")
    paths.set_defaults(func=_cmd_paths)
    return parser


def _cmd_account_add(args: argparse.Namespace) -> int:
    _, account = add_or_update_account(
        args.account_id,
        label=args.label,
        profile_dir=args.profile_dir,
        path=args.config,
    )
    print(f"Account gespeichert: {account.id} ({account.label})")
    print(f"Profil: {account.profile_dir}")
    print(f"Login: codex-usage login {account.id}")
    return 0


def _cmd_account_list(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not config.accounts:
        print("Keine Accounts konfiguriert.")
        return 0
    for account in config.accounts:
        print(f"{account.id}\t{account.label}\t{account.profile_dir}")
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


if __name__ == "__main__":
    raise SystemExit(main())
