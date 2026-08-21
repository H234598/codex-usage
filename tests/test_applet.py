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
    assert metadata["comments"] == f"Version: {metadata['version']}"
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
        "spark-average",
        "spark-five-hour",
        "spark-other",
        "spark-weekly",
        "thirty-day",
        "weekly",
    }
    panel_table = settings["account-panel-settings"]
    assert [column["id"] for column in panel_table["columns"]] == [
        "account",
        "order",
        "muted",
        "slot1",
        "slot2",
        "slot3",
        "slot4",
    ]
    assert panel_table["columns"][1]["min"] == 1
    assert panel_table["columns"][1]["max"] == 100
    assert set(panel_table["columns"][3]["options"].values()) == set(range(11))
    assert panel_table["columns"][2]["default"] is False
    assert settings["panel-account-separator"]["default"] == "bar"
    assert set(settings["panel-account-separator"]["options"].values()) == {
        "bar",
        "dot",
        "slash",
        "brackets",
    }
    assert settings["show-reactivation-actions"]["default"] is True
    assert settings["fast-mode-icon"]["type"] == "custom"
    assert settings["fast-mode-icon"]["file"] == "fast_mode_icon_selector.py"
    assert settings["fast-mode-icon"]["widget"] == "FastModeIconSelector"
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


def test_list_columns_use_cinnamon_supported_variable_types() -> None:
    settings = json.loads((APPLET_DIR / "settings-schema.json").read_text(encoding="utf-8"))
    supported = {
        "string",
        "file",
        "icon",
        "sound",
        "keybinding",
        "integer",
        "float",
        "boolean",
    }

    for key, definition in settings.items():
        if definition.get("type") != "list":
            continue
        for column in definition.get("columns", []):
            assert column["type"] in supported, (key, column["id"], column["type"])


def test_account_table_contains_all_editable_fields() -> None:
    settings = json.loads((APPLET_DIR / "settings-schema.json").read_text(encoding="utf-8"))
    table = settings["account-backends"]

    assert settings["layout"]["backend-section"]["title"] == "Abrufwege und Accounts"
    assert [column["id"] for column in table["columns"]] == [
        "account",
        "label",
        "tag",
        "series",
        "series-active",
        "auth-json",
        "profile-dir",
        "test-home",
        "browser",
        "backend",
    ]
    assert table["show-buttons"] is True
    assert set(table["hidden-buttons"]) == {"up", "down"}
    assert "+" not in table["hidden-buttons"]
    assert "-" not in table["hidden-buttons"]
    assert "Minus löscht" in table["tooltip"]
    assert "automatisch angelegt" in table["description"]
    assert table["columns"][7]["title"] == "Create new Account"
    assert "default" not in table["columns"][2]
    assert "default" not in table["columns"][5]
    assert "default" not in table["columns"][6]
    assert table["columns"][4]["default"] is False
    assert table["columns"][-2]["default"] == 0


def test_display_table_replaces_panel_tag_column() -> None:
    settings = json.loads((APPLET_DIR / "settings-schema.json").read_text(encoding="utf-8"))

    assert [column["id"] for column in settings["account-panel-settings"]["columns"]] == [
        "account", "order", "muted", "slot1", "slot2", "slot3", "slot4",
    ]
    table = settings["account-display-settings"]
    assert [column["id"] for column in table["columns"]] == [
        "account", "tag", "panel", "hover", "click",
        "hover-separator", "click-separator",
    ]
    for column in table["columns"][2:5]:
        assert set(column["options"].values()) == {0, 1, 2}
    assert table["columns"][5]["title"] == "Abstandshalter Hover davor"
    assert table["columns"][6]["title"] == "Abstandshalter Klick davor"
    assert table["columns"][5]["type"] == "boolean"
    assert table["columns"][6]["type"] == "boolean"
    assert table["columns"][5]["default"] is False
    assert table["columns"][6]["default"] is False


def test_consumption_table_exposes_per_account_queries() -> None:
    settings = json.loads((APPLET_DIR / "settings-schema.json").read_text(encoding="utf-8"))
    table = settings["account-consumption-settings"]

    assert settings["layout"]["consumption-periods-section"]["title"] == "Verbrauchszeiträume"
    assert settings["layout"]["credit-periods-section"]["title"] == "Credits und Creditverbrauch"
    assert [column["id"] for column in table["columns"]] == [
        "account", "show-panel", "show-tooltip", "amount", "unit", "limit-window",
        "format", "custom-format", "smoothing", "hide-when-zero",
        "show-coverage-marker", "baseline-enabled", "baseline-minutes",
    ]
    columns = {column["id"]: column for column in table["columns"]}
    assert columns["show-panel"]["default"] is False
    assert columns["show-tooltip"]["default"] is True
    assert columns["show-panel"]["title"] == "Δ Tokenverbrauch Leiste"
    assert columns["show-tooltip"]["title"] == "Δ Tokenverbrauch Hover"
    assert "{value}" in table["description"] or "{value}" in table["tooltip"]
    assert set(columns["unit"]["options"].values()) == {"minutes", "hours", "days", "weeks"}
    assert set(columns["limit-window"]["options"].values()) == {"short", "weekly", "monthly", "spark"}
    assert set(columns["format"]["options"].values()) == {
        "compact", "compact-token", "verbose", "custom"
    }
    assert table["show-buttons"] is True
    assert columns["amount"]["title"] == "Δ Menge"
    assert columns["unit"]["title"] == "Δ Einheit"
    assert columns["baseline-enabled"]["default"] is False
    assert columns["baseline-minutes"]["min"] == 0
    assert columns["baseline-minutes"]["max"] == 9999
    assert set(columns["smoothing"]["options"].values()) == {
        "none", "ema-5", "ema-10", "ema-20", "ema-40", "ema-80", "ema-160", "ema-320", "ema-640"
    }
    assert columns["limit-window"]["title"] == "Δ Limit"
    assert columns["format"]["title"] == "Δ Format"
    assert columns["custom-format"]["title"] == "Δ Eigenes Format"
    forecast = settings["account-forecast-settings"]
    forecast_columns = {column["id"]: column for column in forecast["columns"]}
    assert forecast_columns["show-panel"]["title"] == "Tokenende Leiste"
    assert forecast_columns["show-tooltip"]["title"] == "Tokenende Hover"
    assert set(forecast_columns["limit-window"]["options"].values()) == {
        "short", "weekly", "monthly", "spark"
    }
    assert forecast_columns["limit-window"]["title"] == "Tokenende Limit"
    assert forecast_columns["custom-format"]["title"] == "Tokenende Eigenes Format"
    assert "coverage" in settings["account-consumption-settings-heading"]["description"]
    assert set(table["hidden-buttons"]) == {"+", "-", "up", "down"}


def test_style_tables_group_threshold_fields() -> None:
    settings = json.loads((APPLET_DIR / "settings-schema.json").read_text(encoding="utf-8"))
    expected = {
        "account-percent-styles": [
            "account", "mode", "font", "size", "bold", "italic",
            "color", "background", "hover-background", "threshold", "below-font", "below-size",
            "below-bold", "below-italic", "below-color", "below-background", "below-hover-background",
        ],
        "account-date-styles": [
            "account", "format", "mode", "font", "size", "bold", "italic",
            "color", "background", "hover-background", "threshold", "below-font", "below-size",
            "below-bold", "below-italic", "below-color", "below-background", "below-hover-background",
        ],
        "account-time-styles": [
            "account", "format", "mode", "font", "size", "bold", "italic",
            "color", "background", "hover-background", "threshold", "below-font", "below-size",
            "below-bold", "below-italic", "below-color", "below-background", "below-hover-background",
        ],
        "account-duration-styles": [
            "account", "format", "mode", "font", "size", "bold", "italic",
            "color", "background", "hover-background", "threshold", "below-font", "below-size",
            "below-bold", "below-italic", "below-color", "below-background", "below-hover-background",
        ],
    }
    for name, ids in expected.items():
        columns = settings[name]["columns"]
        assert [column["id"] for column in columns] == ids
        by_id = {column["id"]: column for column in columns}
        assert by_id["font"]["title"].startswith("Über der Schwelle")
        assert by_id["below-font"]["title"].startswith("Unter der Schwelle")
        assert by_id["threshold"]["title"] == "Schwelle %"


def test_format_and_display_sections_use_new_labels() -> None:
    settings = json.loads((APPLET_DIR / "settings-schema.json").read_text(encoding="utf-8"))
    layout = settings["layout"]

    assert layout["format-page"]["sections"] == [
        "percent-style-section",
        "date-style-section",
        "time-style-section",
        "duration-style-section",
        "display-settings-section",
        "display-target-section",
    ]
    assert layout["percent-style-section"]["title"] == "Verbleibendes Tokenlimit in %"
    assert layout["display-settings-section"]["title"] == "Account-Anzeige"
    assert layout["display-target-section"]["title"] == "Formatierungsorte"
    assert layout["percent-style-section"]["title"] == "Verbleibendes Tokenlimit in %"
    assert layout["date-style-section"]["title"] == "OpenAI - Reset: Datum des Reset"
    assert layout["time-style-section"]["title"] == "OpenAI - Reset: Uhrzeit"
    assert layout["duration-style-section"]["title"] == (
        "OpenAI - Reset: Restlaufzeit in Tagen bis Limitreset"
    )


def test_formatting_tables_are_isolated_and_have_editable_rows() -> None:
    settings = json.loads((APPLET_DIR / "settings-schema.json").read_text(encoding="utf-8"))
    layout = settings["layout"]
    expected_sections = {
        "percent-style-section": ("account-percent-styles-heading", "account-percent-styles"),
        "date-style-section": ("account-date-styles-heading", "account-date-styles"),
        "time-style-section": ("account-time-styles-heading", "account-time-styles"),
        "duration-style-section": ("account-duration-styles-heading", "account-duration-styles"),
        "display-settings-section": ("account-display-settings-heading", "account-display-settings"),
        "display-target-section": ("account-style-targets-heading", "account-style-targets"),
    }

    for section_name, keys in expected_sections.items():
        assert layout[section_name]["keys"] == list(keys)
        table = settings[keys[1]]
        assert table["type"] == "list"
        assert table["show-buttons"] is True
        assert "edit" not in table["hidden-buttons"]
        assert table["height"] >= (420 if keys[1] == "account-style-targets" else 300)
    table_keys = [keys[1] for keys in expected_sections.values()]
    assert len(table_keys) == len(set(table_keys))
    assert all(
        sum(table_key in layout[section_name]["keys"] for section_name in expected_sections) == 1
        for table_key in table_keys
    )

    # These were old combined containers. Keeping them in the schema makes it
    # too easy for a Cinnamon settings renderer to attach a table twice.
    assert "formatting-section" not in layout
    assert "style-target-section" not in layout

    target_options = settings["account-style-targets"]["columns"][1]["options"]
    assert target_options == {
        "Prozent": 0,
        "Datum": 1,
        "Uhrzeit": 2,
        "Reset-Restlaufzeit": 3,
        "Verbrauchszeitraum": 4,
        "Zeit bis Tokenende": 5,
        "Usage-Resets": 6,
        "Account-ID": 7,
        "Label": 8,
        "Kürzel": 9,
        "Verbrauch Woche": 10,
        "Credits": 11,
        "Creditverbrauch": 12,
    }
    assert "Doppelklick" in settings["account-style-targets"]["tooltip"]
    assert "13 Elemente" in settings["account-style-targets"]["tooltip"]


def test_alert_table_has_editable_spark_column() -> None:
    settings = json.loads((APPLET_DIR / "settings-schema.json").read_text(encoding="utf-8"))
    table = settings["account-alert-settings"]
    assert [column["id"] for column in table["columns"]] == [
        "account", "five-threshold", "weekly-threshold", "monthly-threshold", "spark-threshold",
        "warnings", "errors",
    ]
    assert table["columns"][4]["title"] == "Spark %"
    assert table["show-buttons"] is True
    assert set(table["hidden-buttons"]) == {"+", "-", "up", "down"}


def test_applet_metadata_and_settings_remainder() -> None:
    settings = json.loads((APPLET_DIR / "settings-schema.json").read_text(encoding="utf-8"))

    assert "login-page" not in settings["layout"]["pages"]
    assert "show-reactivation-actions" in settings["layout"]["reactivation-options-section"]["keys"]
    assert "reactivation-browser" not in settings["layout"]["reactivation-options-section"]["keys"]
    backend_table = settings["account-backends"]
    assert backend_table["type"] == "custom"
    assert backend_table["file"] == "dynamic_series_list.py"
    assert backend_table["widget"] == "DynamicSeriesList"
    assert backend_table["show-buttons"] is True
    assert set(backend_table["hidden-buttons"]) == {"up", "down"}
    assert backend_table["columns"][-1]["options"] == {
        "Bisheriger Direktabruf": 0,
        "Codex App Server": 1,
    }
    assert settings["routing-global-paid-credits"]["default"] is False
    routing_table = settings["routing-credit-overrides"]
    assert routing_table["show-buttons"] is True
    assert [column["id"] for column in routing_table["columns"]] == [
        "scope",
        "identifier",
        "enabled",
        "allow",
        "hourly-limit",
        "weekly-limit",
        "monthly-limit",
    ]
    assert set(routing_table["columns"][0]["options"].values()) == set(range(4))
    date_table = settings["account-date-styles"]
    time_table = settings["account-time-styles"]
    for table in (date_table, time_table):
        assert table["type"] == "list"
        assert table["show-buttons"] is True
        assert set(table["hidden-buttons"]) == {"+", "-", "up", "down"}
        assert [column["id"] for column in table["columns"]] == [
            "account",
            "format",
            "mode",
            "font",
            "size",
            "bold",
            "italic",
            "color",
            "background",
            "hover-background",
            "threshold",
            "below-font",
            "below-size",
            "below-bold",
            "below-italic",
            "below-color",
            "below-background",
            "below-hover-background",
        ]
        columns = {column["id"]: column for column in table["columns"]}
        assert set(columns["mode"]["options"].values()) == set(range(4))
        assert columns["mode"]["default"] == 0
        assert columns["threshold"]["default"] == 20
        assert columns["threshold"]["min"] == 0
        assert columns["threshold"]["max"] == 100
        assert columns["size"]["max"] == 48
        assert columns["bold"]["type"] == "boolean"
        assert columns["italic"]["type"] == "boolean"
        assert set(columns["color"]["options"].values()) == set(range(8))
        assert set(columns["background"]["options"].values()) == set(range(7))
        assert set(columns["hover-background"]["options"].values()) == set(range(7))
        assert columns["below-size"]["max"] == 48
        assert columns["below-bold"]["default"] is True
        assert columns["below-italic"]["type"] == "boolean"
        assert set(columns["below-color"]["options"].values()) == set(range(8))
        assert set(columns["below-background"]["options"].values()) == set(range(7))
        assert set(columns["below-hover-background"]["options"].values()) == set(range(7))
    assert set(date_table["columns"][1]["options"].values()) == set(range(4))
    assert set(time_table["columns"][1]["options"].values()) == set(range(3))
    duration_table = settings["account-duration-styles"]
    assert duration_table["type"] == "list"
    assert duration_table["show-buttons"] is True
    assert set(duration_table["hidden-buttons"]) == {"+", "-", "up", "down"}
    assert [column["id"] for column in duration_table["columns"]] == [
        "account",
        "format",
        "mode",
        "font",
        "size",
        "bold",
        "italic",
        "color",
        "background",
        "hover-background",
        "threshold",
        "below-font",
        "below-size",
        "below-bold",
        "below-italic",
        "below-color",
        "below-background",
        "below-hover-background",
    ]
    assert set(duration_table["columns"][1]["options"].values()) == set(range(4))
    assert set(duration_table["columns"][2]["options"].values()) == set(range(4))
    duration_columns = {column["id"]: column for column in duration_table["columns"]}
    assert duration_columns["threshold"]["default"] == 20
    assert duration_columns["threshold"]["max"] == 100
    assert set(duration_columns["color"]["options"].values()) == set(range(8))
    assert set(duration_columns["background"]["options"].values()) == set(range(7))
    assert set(duration_columns["hover-background"]["options"].values()) == set(range(7))
    assert set(duration_columns["below-color"]["options"].values()) == set(range(8))
    assert set(duration_columns["below-background"]["options"].values()) == set(range(7))
    assert set(duration_columns["below-hover-background"]["options"].values()) == set(range(7))
    percent_table = settings["account-percent-styles"]
    assert [column["id"] for column in percent_table["columns"]] == [
        "account",
        "mode",
        "font",
        "size",
        "bold",
        "italic",
        "color",
        "background",
        "hover-background",
        "threshold",
        "below-font",
        "below-size",
        "below-bold",
        "below-italic",
        "below-color",
        "below-background",
        "below-hover-background",
    ]
    assert set(percent_table["columns"][1]["options"].values()) == set(range(4))
    percent_columns = {column["id"]: column for column in percent_table["columns"]}
    assert percent_columns["mode"]["default"] == 0
    assert percent_columns["threshold"]["default"] == 20
    assert set(percent_columns["color"]["options"].values()) == set(range(8))
    assert set(percent_columns["background"]["options"].values()) == set(range(7))
    assert set(percent_columns["hover-background"]["options"].values()) == set(range(7))
    assert set(percent_columns["below-color"]["options"].values()) == set(range(8))
    assert set(percent_columns["below-background"]["options"].values()) == set(range(7))
    assert set(percent_columns["below-hover-background"]["options"].values()) == set(range(7))
    alert_table = settings["account-alert-settings"]
    assert [column["id"] for column in alert_table["columns"]] == [
        "account",
        "five-threshold",
        "weekly-threshold",
        "monthly-threshold",
        "spark-threshold",
        "warnings",
        "errors",
    ]
    assert alert_table["columns"][1]["default"] == "20"
    assert alert_table["columns"][2]["default"] == "20"
    assert alert_table["columns"][4]["default"] == "20"
    assert alert_table["columns"][5]["default"] is True
    assert alert_table["columns"][6]["default"] is True
    targets = settings["account-style-targets"]
    assert [column["id"] for column in targets["columns"]] == [
        "account",
        "element",
        "panel",
        "hover",
        "click",
    ]
    assert targets["columns"][1]["options"] == {
        "Prozent": 0,
        "Datum": 1,
        "Uhrzeit": 2,
        "Reset-Restlaufzeit": 3,
        "Verbrauchszeitraum": 4,
        "Zeit bis Tokenende": 5,
        "Usage-Resets": 6,
            "Account-ID": 7,
            "Label": 8,
            "Kürzel": 9,
            "Verbrauch Woche": 10,
                "Credits": 11,
                "Creditverbrauch": 12,
        }
    assert targets["show-buttons"] is True
    assert set(targets["hidden-buttons"]) == {"+", "-", "up", "down"}

    layout = settings["layout"]
    referenced_keys: set[str] = set()
    for page_name in layout["pages"]:
        page = layout[page_name]
        for section_name in page["sections"]:
            referenced_keys.update(layout[section_name]["keys"])
    assert referenced_keys == set(settings) - {
        "layout",
        "reactivation-browser",
        "reactivation-browser-migrated",
        "error-notification-state",
    }


def test_reactivation_page_is_removed_and_switch_is_on_codex_usage() -> None:
    settings = json.loads((APPLET_DIR / "settings-schema.json").read_text(encoding="utf-8"))

    assert "login-page" not in settings["layout"]["pages"]
    assert "show-reactivation-actions" in settings["layout"]["reactivation-options-section"]["keys"]
    assert "reactivation-browser" not in settings["layout"]["reactivation-options-section"]["keys"]
    assert "description" in settings["reactivation-browser-migrated"]


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
    for name in ("applet.js", "metadata.json", "settings-schema.json", "stylesheet.css", "dynamic_series_list.py", "fast_mode_icon_selector.py"):
        assert (installed / name).is_file()

    uninstall = _run_script(
        "uninstall_cinnamon_applet.py",
        "--target-root",
        str(target_root),
    )
    assert uninstall.returncode == 0, uninstall.stderr
    assert not installed.exists()


def test_installer_migrates_cached_enum_types_without_changing_values(tmp_path: Path) -> None:
    settings_path = tmp_path / "codex-usage.json"
    settings_path.write_text(
        json.dumps(
            {
                "account-consumption-settings": {
                    "type": "list",
                    "columns": [
                        {
                            "id": "unit",
                            "type": "enum",
                            "options": {"Stunden": "hours"},
                        }
                    ],
                    "value": [{"unit": "hours"}],
                },
                "__md5__": "old",
            }
        ),
        encoding="utf-8",
    )

    assert installer._migrate_cached_settings(settings_path) is True
    migrated = json.loads(settings_path.read_text(encoding="utf-8"))
    column = migrated["account-consumption-settings"]["columns"][0]
    assert column["type"] == "string"
    assert column["options"] == {"Stunden": "hours"}
    assert migrated["account-consumption-settings"]["value"] == [{"unit": "hours"}]
    assert installer._migrate_cached_settings(settings_path) is False


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


def test_reload_running_applet_falls_back_to_cinnamon_dbus(monkeypatch) -> None:
    calls = []
    expected_version = "0.6.434"

    class Result:
        def __init__(self, returncode=0, stdout=""):
            self.returncode = returncode
            self.stdout = stdout

    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/gdbus")

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if "org.Cinnamon.LookingGlass" in args:
            return Result(returncode=1)
        if "org.Cinnamon.ReloadXlet" in args:
            return Result()
        encoded = json.dumps([expected_version])
        return Result(stdout=f"(true, {json.dumps(encoded)!r})")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    assert installer._reload_running_applet(expected_version=expected_version) == "ok"
    assert "org.Cinnamon.ReloadXlet" in calls[1][0]
    assert "org.Cinnamon.Eval" in calls[2][0]


def test_reload_running_applet_reports_not_running_for_missing_cinnamon(monkeypatch) -> None:
    class Result:
        returncode = 1
        stdout = ""
        stderr = "GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown: name is not activatable"

    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/gdbus")
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *args, **kwargs: Result(),
    )

    assert installer._reload_running_applet(expected_version="0.6.435") == "not-running"


def test_reload_running_applet_reports_a_stale_loaded_version(monkeypatch) -> None:
    class Result:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/gdbus")
    monkeypatch.setattr(installer.time, "sleep", lambda delay: None)

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


def test_reload_running_applet_waits_through_the_old_loaded_version(monkeypatch) -> None:
    calls = []
    expected_version = "0.6.378"

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
        versions = ["0.6.377"] if len(calls) == 2 else [expected_version]
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
