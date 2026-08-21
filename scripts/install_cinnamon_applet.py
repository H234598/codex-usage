#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APPLET_UUID = "codex-usage@H234598"
REQUIRED_FILES = (
    "applet.js",
    "metadata.json",
    "settings-schema.json",
    "stylesheet.css",
    "dynamic_series_list.py",
    "fast_mode_icon_selector.py",
    "forecast_table_selector.py",
    "format_table_selector.py",
    "help_page.py",
    "panel_settings_list.py",
)
VERSION_CHECK_ATTEMPTS = 10
VERSION_CHECK_DELAY_SECONDS = 0.2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the Codex Usage Cinnamon applet.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--target-root",
        type=Path,
        default=Path.home() / ".local" / "share" / "cinnamon" / "applets",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--reload-running",
        action="store_true",
        help="reload the running Cinnamon applet after installation",
    )
    args = parser.parse_args(argv)

    try:
        source = args.repo_root.expanduser() / "files" / APPLET_UUID
        target_root = args.target_root.expanduser()
        target = target_root / APPLET_UUID
        _validate_source(source)
        expected_version = _read_applet_version(source)
        _validate_target_root(target_root, create=not args.dry_run)
        _validate_existing_target(target)
        print(f"source={source}")
        print(f"target={target}")
        if args.dry_run:
            print("status=dry-run")
            return 0
        _install_atomically(source, target_root, target)
        print("status=installed")
        print(
            "settings-cache-migration="
            + ("updated" if _migrate_cached_settings() else "unchanged")
        )
        if args.reload_running:
            print(f"reload={_reload_running_applet(expected_version=expected_version)}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _validate_source(source: Path) -> None:
    _assert_real_directory_chain(source.parent)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("applet source must be a real directory")
    for name in REQUIRED_FILES:
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"required applet file is missing or unsafe: {name}")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("applet source must not contain symlinks")


def _read_applet_version(source: Path) -> str:
    try:
        payload = json.loads((source / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("applet metadata is not valid JSON") from exc
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise ValueError("applet metadata has no version")
    return version.strip()


def _validate_target_root(target_root: Path, *, create: bool) -> None:
    _assert_real_directory_chain(target_root)
    if create:
        target_root.mkdir(parents=True, exist_ok=True)
        _assert_real_directory_chain(target_root)
    elif target_root.exists() and not target_root.is_dir():
        raise ValueError("target root must be a directory")


def _assert_real_directory_chain(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        if part == "..":
            current = current.parent
            continue
        current /= part
        if current.is_symlink():
            raise ValueError("directory chain must not contain symlinks")
        if current.exists() and not current.is_dir():
            raise ValueError("directory chain contains a non-directory")
        if not current.exists():
            continue


def _validate_existing_target(target: Path) -> None:
    if target.is_symlink():
        raise ValueError("refusing to replace a symlinked applet target")
    if target.exists() and not target.is_dir():
        raise ValueError("refusing to replace a non-directory applet target")


def _install_atomically(source: Path, target_root: Path, target: Path) -> None:
    staging_root = Path(tempfile.mkdtemp(prefix=f".{APPLET_UUID}.stage-", dir=target_root))
    staged = staging_root / APPLET_UUID
    backup = target_root / f".{APPLET_UUID}.backup-{os.getpid()}"
    moved_old = False
    try:
        shutil.copytree(source, staged, symlinks=False)
        _validate_source(staged)
        if backup.exists() or backup.is_symlink():
            raise ValueError("temporary backup target already exists")
        if target.exists():
            os.replace(target, backup)
            moved_old = True
        try:
            os.replace(staged, target)
        except Exception:
            if moved_old and not target.exists() and backup.is_dir() and not backup.is_symlink():
                os.replace(backup, target)
                moved_old = False
            raise
        if moved_old:
            shutil.rmtree(backup)
            moved_old = False
    finally:
        if moved_old and backup.is_dir() and not backup.is_symlink() and not target.exists():
            os.replace(backup, target)
        if staging_root.is_dir() and not staging_root.is_symlink():
            shutil.rmtree(staging_root)


def _migrate_cached_settings(path: Path | None = None) -> bool:
    refresh_schema = path is None
    if path is None:
        config_root = Path(os.environ.get("XDG_CONFIG_HOME", "")).expanduser()
        if not config_root.is_absolute():
            config_root = Path.home() / ".config"
        path = (
            config_root
            / "cinnamon"
            / "spices"
            / APPLET_UUID
            / f"{APPLET_UUID}.json"
        )
    if path.is_symlink() or not path.is_file():
        return False
    try:
        original_mode = path.stat().st_mode & 0o777
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    changed = _migrate_enum_types(payload)
    if refresh_schema:
        source = (
            Path(__file__).resolve().parents[1]
            / "files"
            / APPLET_UUID
            / "settings-schema.json"
        )
        source_bytes = b""
        try:
            source_bytes = source.read_bytes()
            source_payload = json.loads(source_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            source_payload = None
        if isinstance(source_payload, dict):
            for key, definition in source_payload.items():
                if not isinstance(definition, dict):
                    continue
                previous = payload.get(key)
                updated = json.loads(json.dumps(definition, ensure_ascii=False))
                if isinstance(previous, dict) and "value" in previous:
                    updated["value"] = previous["value"]
                elif "default" in definition:
                    # Cinnamon's cached settings file stores both the schema
                    # and the current value.  New settings only have a
                    # ``default`` in the source schema, so materialize that
                    # default when there is no value to preserve.
                    updated["value"] = definition["default"]
                if payload.get(key) != updated:
                    payload[key] = updated
            source_digest = hashlib.md5(source_bytes).hexdigest()
            if payload.get("__md5__") != source_digest:
                payload["__md5__"] = source_digest
                changed = True

    if not changed:
        return False

    serialized = json.dumps(payload, indent=4, ensure_ascii=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.migration-",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor_open = False
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, original_mode)
        os.replace(temporary, path)
    finally:
        if descriptor_open:
            os.close(descriptor)
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    return True


def _migrate_enum_types(value: object) -> bool:
    changed = False
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            if current.get("type") == "enum" and isinstance(current.get("options"), dict):
                current["type"] = "string"
                changed = True
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return changed


def _reload_running_applet(*, expected_version: str | None = None) -> str:
    gdbus = shutil.which("gdbus")
    if not gdbus:
        return "unavailable"
    try:
        result = subprocess.run(
            [
                gdbus,
                "call",
                "--session",
                "--dest",
                "org.Cinnamon.LookingGlass",
                "--object-path",
                "/org/Cinnamon/LookingGlass",
                "--method",
                "org.Cinnamon.LookingGlass.ReloadExtension",
                APPLET_UUID,
                "APPLET",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    if result.returncode != 0:
        try:
            fallback = subprocess.run(
                [
                    gdbus,
                    "call",
                    "--session",
                    "--dest",
                    "org.Cinnamon",
                    "--object-path",
                    "/org/Cinnamon",
                    "--method",
                    "org.Cinnamon.ReloadXlet",
                    APPLET_UUID,
                    "APPLET",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            fallback = None
        if fallback is not None and fallback.returncode == 0:
            if expected_version is None:
                return "ok"
            return _verify_running_applet_version(gdbus, expected_version)
        if (
            _dbus_service_unavailable(result)
            and (fallback is None or _dbus_service_unavailable(fallback))
        ):
            return "not-running"
        return (
            _verify_running_applet_version(gdbus, expected_version)
            if expected_version is not None
            else "not-running"
        )
    if expected_version is None:
        return "ok"
    return _verify_running_applet_version(gdbus, expected_version)


def _dbus_service_unavailable(result: object) -> bool:
    output = " ".join(
        str(getattr(result, field, ""))
        for field in ("stdout", "stderr")
    ).casefold()
    return "serviceunknown" in output or "name is not activatable" in output


def _verify_running_applet_version(gdbus: str, expected_version: str) -> str:
    script = (
        "JSON.stringify(imports.ui.appletManager.getRunningInstancesForUuid("
        + json.dumps(APPLET_UUID)
        + ").map(function(applet){return applet.metadata&&applet.metadata.version;}))"
    )
    observed_versions: list[str] | None = None
    for attempt in range(VERSION_CHECK_ATTEMPTS):
        try:
            result = subprocess.run(
                [
                    gdbus,
                    "call",
                    "--session",
                    "--dest",
                    "org.Cinnamon",
                    "--object-path",
                    "/org/Cinnamon",
                    "--method",
                    "org.Cinnamon.Eval",
                    script,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"
        if result.returncode != 0:
            return "unavailable"
        output = result.stdout.strip()
        if not output.startswith("(true, ") or not output.endswith(")"):
            return "unavailable"
        try:
            encoded_json = ast.literal_eval(output[len("(true, ") : -1])
            versions = json.loads(encoded_json)
            if isinstance(versions, str):
                versions = json.loads(versions)
        except (SyntaxError, ValueError, TypeError):
            return "unavailable"
        if not isinstance(versions, list):
            return "unavailable"
        if expected_version in versions:
            return "ok"
        if versions and all(isinstance(version, str) for version in versions):
            observed_versions = versions
        if attempt + 1 < VERSION_CHECK_ATTEMPTS:
            time.sleep(VERSION_CHECK_DELAY_SECONDS)
    return "version-mismatch" if observed_versions else "not-running"


if __name__ == "__main__":
    raise SystemExit(main())
