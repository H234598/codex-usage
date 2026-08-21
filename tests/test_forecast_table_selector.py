from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLET_DIR = ROOT / "files" / "codex-usage@H234598"
sys.path.insert(0, str(APPLET_DIR))
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings")
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings/bin")

from forecast_table_selector import ForecastTableSelector, Gtk  # noqa: E402

_TABLE_KEYS = (
    "account-consumption-settings",
    "account-forecast-settings",
    "account-credit-consumption-settings",
)


class _Settings:
    def __init__(self):
        self.settings = {
            "forecast-table-selector": {"value": _TABLE_KEYS[0]},
        }
        for key in _TABLE_KEYS:
            self.settings[key] = {
                "value": [],
                "columns": [{"id": "account", "title": "Account", "type": "string"}],
                "height": 100,
                "show-buttons": False,
                "hidden-buttons": [],
            }
        self.listeners = []
        self.writes = []

    def listen(self, key, callback):
        self.listeners.append((key, callback))

    def get_value(self, key):
        return self.settings[key]["value"]

    def set_value(self, key, value):
        self.settings[key]["value"] = value
        self.writes.append((key, value))


def _info():
    return {
        "tables": [
            {"key": _TABLE_KEYS[0], "label": "Tokenverbrauch"},
            {"key": _TABLE_KEYS[1], "label": "Tokenende"},
            {"key": _TABLE_KEYS[2], "label": "Creditverbrauch"},
        ]
    }


def test_constructor_builds_three_tables_and_initial_selection() -> None:
    settings = _Settings()
    selector = ForecastTableSelector(_info(), "forecast-table-selector", settings)

    try:
        selector.show_all()
        assert set(selector._tables) == set(_TABLE_KEYS)
        assert selector.combo.get_active_id() == _TABLE_KEYS[0]
        assert selector.table_stack.get_visible_child_name() == _TABLE_KEYS[0]
        assert selector.table_stack.get_transition_type() == Gtk.StackTransitionType.NONE
        assert settings.writes == []
    finally:
        selector.destroy()


def test_table_change_switches_stack_and_persists_selection() -> None:
    settings = _Settings()
    selector = ForecastTableSelector(_info(), "forecast-table-selector", settings)

    try:
        selector.show_all()
        selector.combo.set_active_id(_TABLE_KEYS[2])
        while Gtk.events_pending():
            Gtk.main_iteration()
        assert selector.table_stack.get_visible_child_name() == _TABLE_KEYS[2]
        assert selector.table_title.get_text() == "Creditverbrauch"
        assert settings.writes == [("forecast-table-selector", _TABLE_KEYS[2])]
    finally:
        selector.destroy()


def test_setting_reload_falls_back_to_first_table_without_writing() -> None:
    settings = _Settings()
    settings.settings["forecast-table-selector"]["value"] = "missing"
    selector = ForecastTableSelector(_info(), "forecast-table-selector", settings)

    try:
        selector.show_all()
        selector.on_setting_changed()
        assert selector.combo.get_active_id() == _TABLE_KEYS[0]
        assert selector.table_stack.get_visible_child_name() == _TABLE_KEYS[0]
        assert settings.writes == []
    finally:
        selector.destroy()
