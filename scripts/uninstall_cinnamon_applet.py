#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from install_cinnamon_applet import APPLET_UUID, _assert_real_directory_chain


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Uninstall the Codex Usage Cinnamon applet.")
    parser.add_argument(
        "--target-root",
        type=Path,
        default=Path.home() / ".local" / "share" / "cinnamon" / "applets",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        target_root = args.target_root.expanduser()
        target = target_root / APPLET_UUID
        _assert_real_directory_chain(target_root)
        if target.is_symlink():
            raise ValueError("refusing to remove a symlinked applet target")
        if target.exists() and not target.is_dir():
            raise ValueError("refusing to remove a non-directory applet target")
        print(f"target={target}")
        if args.dry_run:
            print("status=dry-run")
            return 0
        if not target.exists():
            print("status=not-installed")
            return 0
        shutil.rmtree(target)
        print("status=uninstalled")
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
