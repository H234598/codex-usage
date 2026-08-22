from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
APPLET_DIR = ROOT / "files" / "codex-usage@H234598"
sys.path.insert(0, str(APPLET_DIR))
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings")
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings/bin")

import forecast_table_selector as forecast_table_selector_module  # noqa: E402
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
        self.listeners = {}
        self.writes = []

    def listen(self, key, callback):
        self.listeners.setdefault(key, []).append(callback)

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


def test_forecast_module_loads_without_applet_directory_on_sys_path() -> None:
    module_name = "_codex_usage_forecast_loader_probe"
    original_path = list(sys.path)
    original_format_module = sys.modules.pop("format_table_selector", None)
    sys.path[:] = [path for path in sys.path if path != str(APPLET_DIR)]
    try:
        spec = importlib.util.spec_from_file_location(
            module_name,
            APPLET_DIR / "forecast_table_selector.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.ForecastTableSelector.__name__ == "ForecastTableSelector"
    finally:
        sys.path[:] = original_path
        if original_format_module is not None:
            sys.modules["format_table_selector"] = original_format_module


def test_forecast_module_ignores_colliding_format_module() -> None:
    module_name = "_codex_usage_forecast_collision_probe"
    original_format_module = sys.modules.get("format_table_selector")
    original_bound_module = sys.modules.pop("_codex_usage_format_table_selector", None)
    collision = ModuleType("format_table_selector")
    collision._BoundFormatList = object()
    sys.modules["format_table_selector"] = collision
    try:
        spec = importlib.util.spec_from_file_location(
            module_name,
            APPLET_DIR / "forecast_table_selector.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module._BoundFormatList.__module__ == "_codex_usage_format_table_selector"
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop("_codex_usage_format_table_selector", None)
        if original_bound_module is not None:
            sys.modules["_codex_usage_format_table_selector"] = original_bound_module
        if original_format_module is not None:
            sys.modules["format_table_selector"] = original_format_module
        else:
            sys.modules.pop("format_table_selector", None)


def test_constructor_builds_only_selected_table_and_initial_selection() -> None:
    settings = _Settings()
    selector = ForecastTableSelector(_info(), "forecast-table-selector", settings)

    try:
        selector.show_all()
        assert set(selector._tables) == {_TABLE_KEYS[0]}
        assert set(selector._table_definitions) == set(_TABLE_KEYS)
        assert selector.combo.get_active_id() == _TABLE_KEYS[0]
        assert selector.table_stack.get_visible_child_name() == _TABLE_KEYS[0]
        assert selector.table_stack.get_transition_type() == Gtk.StackTransitionType.NONE
        assert settings.writes == []
    finally:
        selector.destroy()


def test_forecast_selector_survives_listener_registration_error() -> None:
    class BrokenListenerSettings(_Settings):
        def listen(self, key, callback):
            raise RuntimeError(key)

    settings = BrokenListenerSettings()
    selector = ForecastTableSelector(_info(), "forecast-table-selector", settings)

    try:
        assert selector.combo.get_active_id() == _TABLE_KEYS[0]
        assert set(selector._tables) == {_TABLE_KEYS[0]}
    finally:
        selector.destroy()


def test_forecast_selector_survives_missing_settings_mapping() -> None:
    selector = ForecastTableSelector(_info(), "forecast-table-selector", object())

    try:
        assert selector._table_labels == {}
        assert selector._tables == {}
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
        assert set(selector._tables) == {_TABLE_KEYS[2]}
        assert settings.listeners[_TABLE_KEYS[0]] == []
        assert len(settings.listeners[_TABLE_KEYS[2]]) == 1
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


def test_forecast_selector_ignores_read_and_write_errors() -> None:
    class BrokenSettings(_Settings):
        def get_value(self, key):
            if key == "forecast-table-selector":
                raise RuntimeError(key)
            return super().get_value(key)

        def set_value(self, key, value):
            raise RuntimeError(key)

    settings = BrokenSettings()
    selector = ForecastTableSelector(_info(), "forecast-table-selector", settings)

    try:
        assert selector.combo.get_active_id() == _TABLE_KEYS[0]
        selector.combo.set_active_id(_TABLE_KEYS[1])
        while Gtk.events_pending():
            Gtk.main_iteration()
        assert selector._saving is False
    finally:
        selector.destroy()


def test_forecast_selector_ignores_table_widget_construction_error(monkeypatch) -> None:
    selector = ForecastTableSelector.__new__(ForecastTableSelector)
    selector._table_definitions = {_TABLE_KEYS[0]: {}}
    selector._tables = {}
    selector.settings = object()

    def fail_widget(*_args, **_kwargs):
        raise RuntimeError("widget construction failed")

    monkeypatch.setattr(forecast_table_selector_module, "_BoundFormatList", fail_widget)

    assert selector._ensure_table(_TABLE_KEYS[0]) is None
    assert selector._tables == {}


@pytest.mark.parametrize("value", [[], {}])
def test_setting_reload_falls_back_from_unhashable_selection(value) -> None:
    settings = _Settings()
    settings.settings["forecast-table-selector"]["value"] = value
    selector = ForecastTableSelector(_info(), "forecast-table-selector", settings)

    try:
        assert selector.combo.get_active_id() == _TABLE_KEYS[0]
        assert selector.table_stack.get_visible_child_name() == _TABLE_KEYS[0]
        assert settings.writes == []
    finally:
        selector.destroy()


def test_destroy_detaches_active_table_listener() -> None:
    settings = _Settings()
    selector = ForecastTableSelector(_info(), "forecast-table-selector", settings)

    assert len(settings.listeners[_TABLE_KEYS[0]]) == 1
    selector.destroy()

    assert selector._tables == {}
    assert settings.listeners[_TABLE_KEYS[0]] == []
    assert settings.listeners["forecast-table-selector"] == []


def test_discard_table_survives_already_detached_stack_widget() -> None:
    class FailingStack:
        def remove(self, _widget):
            raise RuntimeError("widget already detached")

    class Widget:
        def __init__(self):
            self.detached = False
            self.destroyed = False

        def detach(self):
            self.detached = True

        def destroy(self):
            self.destroyed = True

    widget = Widget()
    selector = ForecastTableSelector.__new__(ForecastTableSelector)
    selector._tables = {_TABLE_KEYS[0]: widget}
    selector.table_stack = FailingStack()

    ForecastTableSelector._discard_table(selector, _TABLE_KEYS[0])

    assert selector._tables == {}
    assert widget.detached is True
    assert widget.destroyed is True
