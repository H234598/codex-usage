from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_cinnamon_applet.py"
spec = importlib.util.spec_from_file_location("install_cinnamon_applet", SCRIPT)
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def _source(root: Path, version: str = "1.2.3") -> Path:
    source = root / installer.APPLET_UUID
    source.mkdir(parents=True)
    for name in installer.REQUIRED_FILES:
        path = source / name
        if name == "metadata.json":
            path.write_text(json.dumps({"version": version}), encoding="utf-8")
        else:
            path.write_text(name, encoding="utf-8")
    return source


def test_cached_schema_migration_is_idempotent(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    schema_dir = project / "files" / installer.APPLET_UUID
    schema_dir.mkdir(parents=True)
    (schema_dir / "settings-schema.json").write_text(
        json.dumps({"example": {"type": "entry", "default": "default"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "__file__", str(project / "scripts" / "installer.py"))

    config_root = tmp_path / "config"
    cached_dir = config_root / "cinnamon" / "spices" / installer.APPLET_UUID
    cached_dir.mkdir(parents=True)
    cached = cached_dir / f"{installer.APPLET_UUID}.json"
    cached.write_text(
        json.dumps({"example": {"type": "entry", "default": "default", "value": "saved"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))

    assert installer._migrate_cached_settings() is True
    migrated = cached.read_bytes()
    assert installer._migrate_cached_settings() is False
    assert cached.read_bytes() == migrated


def test_source_and_version_validation(tmp_path) -> None:
    source = _source(tmp_path)

    installer._validate_source(source)
    assert installer._read_applet_version(source) == "1.2.3"

    (source / "metadata.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="no version"):
        installer._read_applet_version(source)


def test_source_validation_rejects_symlink_member(tmp_path) -> None:
    source = _source(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    (source / "applet.js").unlink()
    (source / "applet.js").symlink_to(outside)

    with pytest.raises(ValueError, match="missing or unsafe"):
        installer._validate_source(source)


def test_directory_and_target_validation(tmp_path) -> None:
    target_root = tmp_path / "target"
    installer._validate_target_root(target_root, create=True)
    installer._assert_real_directory_chain(target_root)

    target = target_root / installer.APPLET_UUID
    target.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="non-directory"):
        installer._validate_existing_target(target)

    target.unlink()
    outside = tmp_path / "outside"
    outside.mkdir()
    target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked"):
        installer._validate_existing_target(target)

    chain = tmp_path / "chain"
    chain.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        installer._assert_real_directory_chain(chain / "child")


def test_install_atomically_replaces_target_and_cleans_staging(tmp_path) -> None:
    source = _source(tmp_path / "source-root")
    target_root = tmp_path / "target-root"
    target_root.mkdir()
    target = target_root / installer.APPLET_UUID
    target.mkdir()
    (target / "old").write_text("old", encoding="utf-8")

    installer._install_atomically(source, target_root, target)

    assert (target / "applet.js").read_text(encoding="utf-8") == "applet.js"
    assert not (target / "old").exists()
    assert not list(target_root.glob(f".{installer.APPLET_UUID}.stage-*"))


def test_enum_migration_handles_nested_values() -> None:
    value = {"type": "enum", "options": {"A": "a"}, "nested": [{"type": "enum"}]}

    assert installer._migrate_enum_types(value) is True
    assert value["type"] == "string"
    assert value["nested"][0]["type"] == "enum"
    assert installer._migrate_enum_types(value) is False


def test_reload_and_dbus_helpers(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/gdbus")
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )

    assert installer._reload_running_applet() == "ok"
    assert calls and calls[0][0][0] == "/usr/bin/gdbus"
    assert installer._dbus_service_unavailable(SimpleNamespace(stderr="ServiceUnknown"))
    assert not installer._dbus_service_unavailable(SimpleNamespace(stderr="other"))


def test_verify_running_version_parses_gdbus_json(monkeypatch) -> None:
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="(true, '[\"1.2.3\"]')",
            stderr="",
        ),
    )

    assert installer._verify_running_applet_version("gdbus", "1.2.3") == "ok"


def test_main_supports_dry_run_and_rejects_missing_source(tmp_path, capsys) -> None:
    target_root = tmp_path / "target"
    assert installer.main([
        "--repo-root", str(ROOT),
        "--target-root", str(target_root),
        "--dry-run",
    ]) == 0
    assert not target_root.exists()
    assert "status=dry-run" in capsys.readouterr().out

    assert installer.main(["--repo-root", str(tmp_path / "missing")]) == 1
