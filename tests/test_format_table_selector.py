from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLET_DIR = ROOT / "files" / "codex-usage@H234598"
sys.path.insert(0, str(APPLET_DIR))
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings")
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings/bin")

from format_table_selector import FormatTableSelector, Gtk  # noqa: E402


class _Combo:
    def __init__(self, active: str | None):
        self.active = active
        self.set_values: list[str] = []

    def get_active_id(self):
        return self.active

    def set_active_id(self, value: str):
        self.set_values.append(value)
        self.active = value


class _Stack:
    def __init__(self):
        self.visible = None

    def set_visible_child_name(self, value: str):
        self.visible = value


class _Title:
    def __init__(self):
        self.markup = None

    def set_markup(self, value: str):
        self.markup = value


class _Settings:
    def __init__(self):
        self.settings = {
            "format-table-selector": {"value": "table-a"},
            "table-a": {
                "value": [],
                "columns": [{"id": "name", "title": "Name", "type": "string"}],
                "height": 100,
                "show-buttons": False,
                "hidden-buttons": [],
            },
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


def _selector() -> FormatTableSelector:
    selector = FormatTableSelector.__new__(FormatTableSelector)
    selector.combo = _Combo("account-date-styles")
    selector.table_stack = _Stack()
    selector.table_title = _Title()
    selector._table_labels = {
        "account-percent-styles": "Prozent",
        "account-date-styles": "Datum",
    }
    selector._tables = {
        "account-percent-styles": object(),
        "account-date-styles": object(),
    }
    selector._saving = False
    selector.saved = []
    selector.set_value = selector.saved.append
    return selector


def test_constructor_builds_only_declared_tables_and_initial_selection() -> None:
    settings = _Settings()
    selector = FormatTableSelector(
        {
            "tables": [
                {"key": "table-a", "label": "A"},
                {"key": "missing", "label": "Missing"},
            ]
        },
        "format-table-selector",
        settings,
    )

    try:
        selector.show_all()
        assert set(selector._tables) == {"table-a"}
        assert selector.combo.get_active_id() == "table-a"
        assert selector.table_stack.get_visible_child_name() == "table-a"
        assert selector.table_stack.get_transition_type() == Gtk.StackTransitionType.NONE
        assert settings.writes == []
    finally:
        selector.destroy()


def test_table_change_switches_stack_and_persists_selection() -> None:
    selector = _selector()

    selector._on_table_changed()

    assert selector.table_stack.visible == "account-date-styles"
    assert selector.table_title.markup == "<b>Datum</b>"
    assert selector.saved == ["account-date-styles"]


def test_table_change_ignores_unknown_selection() -> None:
    selector = _selector()
    selector.combo.active = "missing"

    selector._on_table_changed()

    assert selector.table_stack.visible is None
    assert selector.saved == []


def test_setting_reload_falls_back_to_first_table_without_writing() -> None:
    selector = _selector()
    selector.get_value = lambda: "missing"

    selector.on_setting_changed()

    assert selector.combo.set_values == ["account-percent-styles"]
    assert selector.table_stack.visible == "account-percent-styles"
    assert selector.table_title.markup == "<b>Prozent</b>"
    assert selector.saved == []


def test_setting_reload_keeps_selected_table_without_writing() -> None:
    selector = _selector()
    selector.get_value = lambda: "account-date-styles"

    selector.on_setting_changed()

    assert selector.combo.set_values == ["account-date-styles"]
    assert selector.table_stack.visible == "account-date-styles"
    assert selector.saved == []
