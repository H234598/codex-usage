from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLET_UUID = "codex-usage@H234598"
APPLET_DIR = ROOT / "files" / APPLET_UUID
_INSTALLER_SPEC = importlib.util.spec_from_file_location(
    "codex_usage_installer",
    ROOT / "scripts" / "install_cinnamon_applet.py",
)
assert _INSTALLER_SPEC is not None and _INSTALLER_SPEC.loader is not None
installer = importlib.util.module_from_spec(_INSTALLER_SPEC)
_INSTALLER_SPEC.loader.exec_module(installer)


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
    assert "panel-account-mode" not in settings
    assert settings["panel-percent-source"]["default"] == "average"
    assert set(settings["panel-percent-source"]["options"].values()) == {
        "average",
        "five-hour",
        "weekly",
    }
    panel_table = settings["account-panel-settings"]
    assert [column["id"] for column in panel_table["columns"]] == [
        "account",
        "tag",
        "order",
        "muted",
        "slot1",
        "slot2",
    ]
    assert panel_table["columns"][2]["min"] == 1
    assert panel_table["columns"][2]["max"] == 100
    assert set(panel_table["columns"][4]["options"].values()) == set(range(4))
    assert panel_table["columns"][3]["default"] is False
    assert settings["panel-account-separator"]["default"] == "bar"
    assert set(settings["panel-account-separator"]["options"].values()) == {
        "bar",
        "dot",
        "slash",
        "brackets",
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
    date_table = settings["account-date-styles"]
    time_table = settings["account-time-styles"]
    for table in (date_table, time_table):
        assert table["type"] == "list"
        assert table["show-buttons"] is False
        assert [column["id"] for column in table["columns"]] == [
            "account",
            "format",
            "mode",
            "threshold",
            "font",
            "size",
            "bold",
            "italic",
            "color",
            "background",
            "below-font",
            "below-size",
            "below-bold",
            "below-italic",
            "below-color",
            "below-background",
        ]
        assert set(table["columns"][2]["options"].values()) == set(range(4))
        assert table["columns"][2]["default"] == 0
        assert table["columns"][3]["default"] == 20
        assert table["columns"][3]["min"] == 0
        assert table["columns"][3]["max"] == 100
        assert table["columns"][5]["max"] == 48
        assert table["columns"][6]["type"] == "boolean"
        assert table["columns"][7]["type"] == "boolean"
        assert set(table["columns"][8]["options"].values()) == set(range(8))
        assert set(table["columns"][9]["options"].values()) == set(range(7))
        assert table["columns"][11]["max"] == 48
        assert table["columns"][12]["default"] is True
        assert table["columns"][13]["type"] == "boolean"
        assert set(table["columns"][14]["options"].values()) == set(range(8))
        assert set(table["columns"][15]["options"].values()) == set(range(7))
    assert set(date_table["columns"][1]["options"].values()) == set(range(4))
    assert set(time_table["columns"][1]["options"].values()) == set(range(3))
    duration_table = settings["account-duration-styles"]
    assert duration_table["type"] == "list"
    assert duration_table["show-buttons"] is False
    assert [column["id"] for column in duration_table["columns"]] == [
        "account",
        "format",
        "mode",
        "threshold",
        "font",
        "size",
        "bold",
        "italic",
        "color",
        "background",
        "below-font",
        "below-size",
        "below-bold",
        "below-italic",
        "below-color",
        "below-background",
    ]
    assert set(duration_table["columns"][1]["options"].values()) == set(range(4))
    assert set(duration_table["columns"][2]["options"].values()) == set(range(4))
    assert duration_table["columns"][3]["default"] == 120
    assert duration_table["columns"][3]["max"] == 10080
    assert set(duration_table["columns"][8]["options"].values()) == set(range(8))
    assert set(duration_table["columns"][9]["options"].values()) == set(range(7))
    assert set(duration_table["columns"][14]["options"].values()) == set(range(8))
    assert set(duration_table["columns"][15]["options"].values()) == set(range(7))
    percent_table = settings["account-percent-styles"]
    assert [column["id"] for column in percent_table["columns"]] == [
        "account",
        "mode",
        "threshold",
        "font",
        "size",
        "bold",
        "italic",
        "color",
        "background",
        "below-font",
        "below-size",
        "below-bold",
        "below-italic",
        "below-color",
        "below-background",
    ]
    assert set(percent_table["columns"][1]["options"].values()) == set(range(4))
    assert percent_table["columns"][1]["default"] == 0
    assert percent_table["columns"][2]["default"] == 20
    assert set(percent_table["columns"][7]["options"].values()) == set(range(8))
    assert set(percent_table["columns"][8]["options"].values()) == set(range(7))
    assert set(percent_table["columns"][13]["options"].values()) == set(range(8))
    assert set(percent_table["columns"][14]["options"].values()) == set(range(7))
    alert_table = settings["account-alert-settings"]
    assert [column["id"] for column in alert_table["columns"]] == [
        "account",
        "five-threshold",
        "weekly-threshold",
        "warnings",
        "errors",
    ]
    assert alert_table["columns"][1]["default"] == 20
    assert alert_table["columns"][2]["default"] == 20
    assert alert_table["columns"][3]["default"] is True
    assert alert_table["columns"][4]["default"] is True
    targets = settings["account-style-targets"]
    assert [column["id"] for column in targets["columns"]] == [
        "account",
        "element",
        "panel",
        "hover",
        "click",
    ]
    assert set(targets["columns"][1]["options"].values()) == {0, 1, 2, 3}
    assert targets["show-buttons"] is False

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
    assert "_panelItems" in source
    assert "_panelSourceLabel" in source
    assert "_panelSeparator" in source
    assert "_accountTag" in source
    assert "showPanelLabel" not in source
    assert "this.set_applet_label(panel.plain);" in source
    assert "this._setPanelMarkup(panel.markup);" in source
    assert "_reactivateAccount" in source
    assert '"system-log-in-symbolic"' in source
    assert '"reactivate"' in source
    assert "codex-usage login " not in source
    assert 'bind("account-backends"' in source
    assert 'bind("account-panel-settings"' in source
    assert 'bind("account-alert-settings"' in source
    assert 'bind("account-percent-styles"' in source
    assert 'bind("account-date-styles"' in source
    assert 'bind("account-time-styles"' in source
    assert 'bind("account-duration-styles"' in source
    assert 'bind("account-style-targets"' in source
    assert "changed.backend" in source
    assert '"service", "status"' in source
    assert "_onAccountBackendsChanged" in source
    assert "backend_configured" in source
    assert "_normalizeStyleRow" in source
    assert "_normalizeTargetRow" in source
    assert "_percentPartsFromValue" in source
    assert "_tooltipContent" in source
    assert "_targetEnabled" in source
    assert "_formatDatePart" in source
    assert "_formatTimePart" in source
    assert "_durationMinutes" in source
    assert "_formatDurationPart" in source
    assert "_displayTimerId" in source
    assert "_styleSpan" in source
    assert "_styleIsActive" in source
    assert "_runSafely" in source
    assert "_removeSource" in source
    assert "_readBoundedProcessOutput" in source
    assert "read_bytes_async" in source
    assert "communicate_utf8_async" not in source
    assert "CIRCUIT_BREAKER_MS" in source
    assert "_buildSafeMenu" in source
    assert "_addHealthAction" in source
    assert 'this._runSafely("health action"' in source
    assert "Settings konnten nicht initialisiert werden" in source
    assert "this.menu = null" in source
    assert "_cacheIsStale" in source
    assert "_repairStaleService" in source
    assert "_serviceAutoAttempted" in source
    assert "this._enableBackgroundService(callback);" in source
    assert 'this._runSafely("service continuation"' in source
    assert "generation === this._generation" in source
    assert "this._timeoutId = 0" in source
    assert "record.timeoutId = 0" in source
    assert "remaining < Number(style.threshold)" in source
    assert "row.conditional === true ? 1 : 0" in source
    assert "style.mode !== undefined" in source
    assert "text.set_markup(markup)" in source
    assert "this.set_applet_tooltip(" in source
    assert "tooltip.markup" in source
    assert '.replace(/&/g, "&amp;")' in source
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


def test_reload_running_applet_uses_bounded_gdbus_call(monkeypatch) -> None:
    calls = []

    class Result:
        returncode = 0

    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/gdbus")
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or Result(),
    )

    assert installer._reload_running_applet() == "ok"
    assert calls == [
        (
            (
                [
                    "/usr/bin/gdbus",
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
            ),
            {
                "check": False,
                "capture_output": True,
                "text": True,
                "timeout": 5,
            },
        )
    ]


def test_reload_running_applet_verifies_the_loaded_version(monkeypatch) -> None:
    calls = []
    expected_version = "0.6.377"

    class Result:
        def __init__(self, stdout=""):
            self.returncode = 0
            self.stdout = stdout

    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/gdbus")

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if "ReloadExtension" in args:
            return Result()
        encoded = json.dumps([expected_version])
        return Result(f"(true, {json.dumps(encoded)!r})")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    assert installer._reload_running_applet(expected_version=expected_version) == "ok"
    assert len(calls) == 2
    assert calls[1][0][-2] == "org.Cinnamon.Eval"


def test_reload_running_applet_reports_a_stale_loaded_version(monkeypatch) -> None:
    class Result:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/gdbus")

    def fake_run(args, **kwargs):
        if "ReloadExtension" in args:
            return Result()
        encoded = json.dumps(["0.6.376"])
        return type(
            "EvalResult",
            (),
            {"returncode": 0, "stdout": f"(true, {json.dumps(encoded)!r})"},
        )()

    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    assert installer._reload_running_applet(expected_version="0.6.377") == "version-mismatch"


def test_reload_running_applet_accepts_current_version_after_reload_error(monkeypatch) -> None:
    class Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/gdbus")

    def fake_run(args, **kwargs):
        if "ReloadExtension" in args:
            return Result()
        encoded = json.dumps(["0.6.377"])
        return type(
            "EvalResult",
            (),
            {"returncode": 0, "stdout": f"(true, {json.dumps(encoded)!r})"},
        )()

    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    assert installer._reload_running_applet(expected_version="0.6.377") == "ok"


def test_reload_running_applet_waits_for_cinnamon_to_recreate_instance(monkeypatch) -> None:
    calls = []
    expected_version = "0.6.377"

    class Result:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/gdbus")
    monkeypatch.setattr(installer.time, "sleep", lambda delay: None)

    def fake_run(args, **kwargs):
        calls.append(args)
        if "ReloadExtension" in args:
            return Result("")
        versions = [] if len(calls) == 2 else [expected_version]
        encoded = json.dumps(versions)
        return Result(f"(true, {json.dumps(encoded)!r})")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    assert installer._reload_running_applet(expected_version=expected_version) == "ok"
    assert len(calls) == 3


def test_installer_help_exposes_running_reload() -> None:
    result = _run_script("install_cinnamon_applet.py", "--help")

    assert result.returncode == 0
    assert "--reload-running" in result.stdout


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
