from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLET_UUID = "codex-usage@H234598"
APPLET_DIR = ROOT / "files" / APPLET_UUID


def test_applet_metadata_and_settings_are_consistent() -> None:
    metadata = json.loads((APPLET_DIR / "metadata.json").read_text(encoding="utf-8"))
    settings = json.loads((APPLET_DIR / "settings-schema.json").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_init = (ROOT / "src" / "codex_usage" / "__init__.py").read_text(
        encoding="utf-8"
    )

    assert metadata["uuid"] == APPLET_UUID
    assert metadata["version"] == project["project"]["version"]
    assert f'__version__ = "{metadata["version"]}"' in package_init
    assert metadata["max-instances"] == 1
    assert settings["refresh-interval"]["default"] == 300
    assert settings["refresh-interval"]["min"] >= 60
    assert "show-panel-label" not in settings
    assert settings["panel-account-mode"]["default"] == "combined"
    assert set(settings["panel-account-mode"]["options"].values()) == {
        "combined",
        "per-account",
    }
    assert settings["panel-percent-source"]["default"] == "average"
    assert set(settings["panel-percent-source"]["options"].values()) == {
        "average",
        "five-hour",
        "weekly",
    }
    assert settings["show-reactivation-actions"]["default"] is True
    assert settings["reactivation-browser"]["default"] == "auto"
    assert set(settings["reactivation-browser"]["options"].values()) == {
        "auto",
        "chromium",
        "firefox",
        "vivaldi",
    }
    assert settings["poll-owner"]["default"] == "auto"
    assert set(settings["poll-owner"]["options"].values()) == {
        "applet",
        "auto",
        "systemd",
    }
    backend_table = settings["account-backends"]
    assert backend_table["type"] == "list"
    assert backend_table["show-buttons"] is False
    assert backend_table["columns"][2]["options"] == {
        "Bisheriger Direktabruf": 0,
        "Codex App Server": 1,
    }

    layout = settings["layout"]
    referenced_keys: set[str] = set()
    for page_name in layout["pages"]:
        page = layout[page_name]
        for section_name in page["sections"]:
            referenced_keys.update(layout[section_name]["keys"])
    assert referenced_keys == set(settings) - {"layout"}


def test_applet_uses_argv_subprocesses_and_bounded_json() -> None:
    source = (APPLET_DIR / "applet.js").read_text(encoding="utf-8")

    assert 'argv.push(subcommand, "--format", "json")' in source
    assert "MAX_JSON_CHARS" in source
    assert "COMMAND_TIMEOUT_MS" in source
    assert "Gio.SubprocessLauncher" in source
    assert "force_exit" in source
    assert "_selectedPercent" in source
    assert "_accountTag" in source
    assert "showPanelLabel" not in source
    assert "this.set_applet_label(this._panelLabel(selected, worst));" in source
    assert "_reactivateAccount" in source
    assert '"system-log-in-symbolic"' in source
    assert '"reactivate"' in source
    assert "codex-usage login " not in source
    assert 'bind("account-backends"' in source
    assert "changed.backend" in source
    assert '"service", "status"' in source
    assert "_onAccountBackendsChanged" in source
    assert "backend_configured" in source
    for forbidden in (
        "spawnCommandLine",
        "Util.spawn",
        "shell=True",
        '"/bin/sh"',
        '"bash", "-c"',
        "auth.json",
    ):
        assert forbidden not in source


def test_installer_and_uninstaller_round_trip(tmp_path: Path) -> None:
    target_root = tmp_path / "applets"
    install = _run_script(
        "install_cinnamon_applet.py",
        "--repo-root",
        str(ROOT),
        "--target-root",
        str(target_root),
    )
    assert install.returncode == 0, install.stderr

    installed = target_root / APPLET_UUID
    assert installed.is_dir()
    for name in ("applet.js", "metadata.json", "settings-schema.json", "stylesheet.css"):
        assert (installed / name).is_file()

    uninstall = _run_script(
        "uninstall_cinnamon_applet.py",
        "--target-root",
        str(target_root),
    )
    assert uninstall.returncode == 0, uninstall.stderr
    assert not installed.exists()


def test_installer_refuses_symlink_target(tmp_path: Path) -> None:
    target_root = tmp_path / "applets"
    target_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_text("keep", encoding="utf-8")
    (target_root / APPLET_UUID).symlink_to(outside, target_is_directory=True)

    result = _run_script(
        "install_cinnamon_applet.py",
        "--repo-root",
        str(ROOT),
        "--target-root",
        str(target_root),
    )

    assert result.returncode == 1
    assert "symlink" in result.stderr.lower()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_installer_dry_run_does_not_create_target_root(tmp_path: Path) -> None:
    target_root = tmp_path / "missing" / "applets"
    result = _run_script(
        "install_cinnamon_applet.py",
        "--repo-root",
        str(ROOT),
        "--target-root",
        str(target_root),
        "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    assert "status=dry-run" in result.stdout
    assert not target_root.exists()


def _run_script(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / name), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
