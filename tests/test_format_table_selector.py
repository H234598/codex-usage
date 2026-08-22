from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APPLET_DIR = ROOT / "files" / "codex-usage@H234598"
sys.path.insert(0, str(APPLET_DIR))
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings")
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings/bin")

import format_table_selector as format_table_selector_module  # noqa: E402
from format_table_selector import FormatTableSelector, Gtk, _BoundFormatList  # noqa: E402
from TreeListWidgets import list_edit_factory  # noqa: E402


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
            "table-b": {
                "value": [],
                "columns": [{"id": "value", "title": "Value", "type": "string"}],
                "height": 100,
                "show-buttons": False,
                "hidden-buttons": [],
            },
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


def test_constructor_builds_only_selected_table_and_initial_selection() -> None:
    settings = _Settings()
    selector = FormatTableSelector(
        {
            "tables": [
                {"key": "table-a", "label": "A"},
                {"key": "table-b", "label": "B"},
                {"key": "missing", "label": "Missing"},
            ]
        },
        "format-table-selector",
        settings,
    )

    try:
        selector.show_all()
        assert set(selector._tables) == {"table-a"}
        assert set(selector._table_definitions) == {"table-a", "table-b"}
        assert selector.combo.get_active_id() == "table-a"
        assert selector.table_stack.get_visible_child_name() == "table-a"
        assert selector.table_stack.get_transition_type() == Gtk.StackTransitionType.NONE
        assert settings.writes == []
    finally:
        selector.destroy()


@pytest.mark.parametrize(
    "tables",
    [None, "malformed", [None, "malformed", {"key": "table-a", "label": "A"}]],
)
def test_constructor_ignores_malformed_table_declarations(tables) -> None:
    settings = _Settings()
    selector = FormatTableSelector({"tables": tables}, "format-table-selector", settings)

    try:
        if isinstance(tables, list):
            assert set(selector._table_labels) == {"table-a"}
        else:
            assert selector._table_labels == {}
            assert selector._tables == {}
    finally:
        selector.destroy()


def test_constructor_ignores_non_mapping_table_definition() -> None:
    settings = _Settings()
    settings.settings["table-a"] = None
    selector = FormatTableSelector(
        {"tables": [{"key": "table-a", "label": "A"}]},
        "format-table-selector",
        settings,
    )

    try:
        assert selector._table_labels == {}
        assert selector._tables == {}
    finally:
        selector.destroy()


def test_selector_survives_listener_registration_error() -> None:
    class BrokenListenerSettings(_Settings):
        def listen(self, key, callback):
            raise RuntimeError(key)

    settings = BrokenListenerSettings()
    selector = FormatTableSelector(
        {"tables": [{"key": "table-a", "label": "A"}]},
        "format-table-selector",
        settings,
    )

    try:
        assert selector.combo.get_active_id() == "table-a"
        assert set(selector._tables) == {"table-a"}
        assert len(selector._tables["table-a"].model) == 0
    finally:
        selector.destroy()


def test_selector_survives_missing_settings_mapping() -> None:
    selector = FormatTableSelector(
        {"tables": [{"key": "table-a", "label": "A"}]},
        "format-table-selector",
        object(),
    )

    try:
        assert selector._table_labels == {}
        assert selector._tables == {}
    finally:
        selector.destroy()


def test_table_is_built_when_first_selected() -> None:
    settings = _Settings()
    selector = FormatTableSelector(
        {
            "tables": [
                {"key": "table-a", "label": "A"},
                {"key": "table-b", "label": "B"},
            ]
        },
        "format-table-selector",
        settings,
    )

    try:
        selector.show_all()
        selector.combo.set_active_id("table-b")
        while Gtk.events_pending():
            Gtk.main_iteration()
        assert set(selector._tables) == {"table-b"}
        assert settings.listeners["table-a"] == []
        assert len(settings.listeners["table-b"]) == 1
        assert selector.table_stack.get_visible_child_name() == "table-b"
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


def test_table_change_ignores_selector_write_error() -> None:
    selector = _selector()
    selector.set_value = lambda _value: (_ for _ in ()).throw(RuntimeError("write failed"))

    selector._on_table_changed()

    assert selector.table_stack.visible == "account-date-styles"
    assert selector._saving is False


def test_selector_ignores_table_widget_construction_error(monkeypatch) -> None:
    selector = FormatTableSelector.__new__(FormatTableSelector)
    selector._table_definitions = {"table-a": {}}
    selector._tables = {}
    selector.settings = object()

    def fail_widget(*_args, **_kwargs):
        raise RuntimeError("widget construction failed")

    monkeypatch.setattr(format_table_selector_module, "_BoundFormatList", fail_widget)

    assert selector._ensure_table("table-a") is None
    assert selector._tables == {}


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


def test_setting_reload_falls_back_when_selector_read_fails() -> None:
    class BrokenSelectorReadSettings(_Settings):
        def get_value(self, key):
            if key == "format-table-selector":
                raise RuntimeError(key)
            return super().get_value(key)

    settings = BrokenSelectorReadSettings()
    selector = FormatTableSelector(
        {"tables": [{"key": "table-a", "label": "A"}]},
        "format-table-selector",
        settings,
    )

    try:
        assert selector.combo.get_active_id() == "table-a"
        assert settings.writes == []
    finally:
        selector.destroy()


@pytest.mark.parametrize("value", [[], {}])
def test_setting_reload_falls_back_from_unhashable_selection(value) -> None:
    settings = _Settings()
    settings.settings["format-table-selector"]["value"] = value

    selector = FormatTableSelector(
        {"tables": [{"key": "table-a", "label": "A"}]},
        "format-table-selector",
        settings,
    )

    try:
        assert selector.combo.get_active_id() == "table-a"
        assert selector.table_stack.get_visible_child_name() == "table-a"
        assert settings.writes == []
    finally:
        selector.destroy()


def test_destroy_detaches_active_table_listener() -> None:
    settings = _Settings()
    selector = FormatTableSelector(
        {
            "tables": [{"key": "table-a", "label": "A"}],
        },
        "format-table-selector",
        settings,
    )

    assert len(settings.listeners["table-a"]) == 1
    selector.destroy()

    assert selector._tables == {}
    assert settings.listeners["table-a"] == []
    assert settings.listeners["format-table-selector"] == []


def test_copy_table_reuses_percent_columns_but_keeps_own_description() -> None:
    settings = _Settings()
    settings.settings = {
        "account-percent-styles": {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {"id": "bold", "title": "Fett", "type": "boolean"},
            ],
            "value": [],
        },
        "account-panel-tag-styles": {
            "type": "list",
            "format-copy-of": "account-percent-styles",
            "description": "Leiste — Kürzel",
            "tooltip": "Kürzel-Hilfe",
            "height": 300,
            "show-buttons": True,
            "hidden-buttons": ["+"],
            "value": [],
        },
    }

    widget = _BoundFormatList(
        "account-panel-tag-styles",
        settings.settings["account-panel-tag-styles"],
        settings,
    )
    try:
        assert [column["id"] for column in widget.columns] == ["account", "bold"]
    finally:
        widget.destroy()


@pytest.mark.parametrize("columns", [None, [None, {"id": "bad", "type": "unknown"}]])
def test_delta_copy_ignores_malformed_base_columns(columns) -> None:
    settings = _Settings()
    settings.settings["account-percent-styles"] = {"columns": columns, "value": []}
    settings.settings["account-delta-styles"] = {"value": []}

    widget = _BoundFormatList(
        "account-delta-styles",
        settings.settings["account-delta-styles"],
        settings,
    )
    try:
        assert [column["id"] for column in widget.columns] == ["dynamic"]
    finally:
        widget.destroy()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("description", []),
        ("height", "bad"),
        ("show-buttons", "bad"),
        ("hidden-buttons", None),
        ("tooltip", []),
    ],
)
def test_malformed_list_metadata_uses_safe_defaults(field, value) -> None:
    settings = _Settings()
    definition = settings.settings["table-a"]
    definition.update(
        {
            "description": "Description",
            "height": 100,
            "show-buttons": False,
            "hidden-buttons": ["up"],
            "tooltip": "Help",
        }
    )
    definition[field] = value

    widget = _BoundFormatList("table-a", definition, settings)
    try:
        assert widget.show_buttons is (True if field == "show-buttons" else False)
        assert widget.hidden_buttons == ([] if field == "hidden-buttons" else ["up"])
        assert widget.get_tooltip_text() == (None if field == "tooltip" else "Help")
        assert hasattr(widget, "label") is (field != "description")
    finally:
        widget.destroy()


@pytest.mark.parametrize("height", [float("nan"), float("inf"), -1, 220.5, 20000])
def test_format_list_height_is_finite_and_bounded(height) -> None:
    settings = _Settings()
    settings.settings["table-a"]["height"] = height
    widget = _BoundFormatList("table-a", settings.settings["table-a"], settings)

    try:
        expected = 220 if height == 220.5 else 10000 if height == 20000 else 300
        assert widget.get_children()[0].get_size_request()[1] == expected
    finally:
        widget.destroy()


@pytest.mark.parametrize("align", ["bad", float("nan"), float("inf"), -1, 2, True, None, 0.5])
def test_format_table_sanitizes_column_alignment(align) -> None:
    settings = _Settings()
    settings.settings["table-a"]["columns"] = [
        {"id": "value", "title": "Value", "type": "string", "align": align}
    ]
    widget = _BoundFormatList("table-a", settings.settings["table-a"], settings)

    try:
        if align == 0.5:
            assert widget.columns[0]["align"] == 0.5
        else:
            assert "align" not in widget.columns[0]
    finally:
        widget.destroy()


@pytest.mark.parametrize(
    "options",
    [
        {"Bad": 2**31},
        {"Bad\x00Label": 1},
        {"Bad": "bad\x00value"},
        [1, 2],
        None,
    ],
)
def test_format_table_drops_invalid_options(options) -> None:
    settings = _Settings()
    settings.settings["table-a"]["columns"] = [
        {"id": "mode", "title": "Mode", "type": "integer", "options": options}
    ]
    widget = _BoundFormatList("table-a", settings.settings["table-a"], settings)

    try:
        assert widget.columns == []
    finally:
        widget.destroy()


def test_format_table_ignores_numeric_ranges_for_option_columns() -> None:
    settings = _Settings()
    settings.settings["table-a"]["columns"] = [
        {
            "id": "mode",
            "title": "Mode",
            "type": "integer",
            "options": {"Zero": 0},
            "default": 0,
            "min": "bad",
            "max": "bad",
        }
    ]

    widget = _BoundFormatList("table-a", settings.settings["table-a"], settings)

    try:
        assert widget.columns[0]["default"] == 0
    finally:
        widget.destroy()


def test_format_table_option_columns_drop_spinbutton_properties() -> None:
    settings = _Settings()
    settings.settings["table-a"]["columns"] = [
        {
            "id": "mode",
            "title": "Mode",
            "type": "integer",
            "options": {"Zero": 0},
            "min": 0,
            "max": 10,
            "step": 1,
            "units": "Wert",
            "default": 0,
        }
    ]

    widget = _BoundFormatList("table-a", settings.settings["table-a"], settings)
    try:
        editor = list_edit_factory(widget.columns[0])
        try:
            assert all(
                property_name not in widget.columns[0]
                for property_name in ("min", "max", "step", "units")
            )
        finally:
            editor.destroy()
    finally:
        widget.destroy()


@pytest.mark.parametrize(
    ("column_type", "default"),
    [
        ("string", False),
        ("integer", 2**40),
        ("float", float("nan")),
        ("boolean", "yes"),
        ("file", None),
    ],
)
def test_format_table_drops_invalid_defaults(column_type, default) -> None:
    settings = _Settings()
    column = {"id": "value", "title": "Value", "type": column_type, "default": default}
    if column_type in {"integer", "float"}:
        column.update({"min": 0, "max": 10})
    settings.settings["table-a"]["columns"] = [column]
    widget = _BoundFormatList("table-a", settings.settings["table-a"], settings)

    try:
        assert "default" not in widget.columns[0]
    finally:
        widget.destroy()


@pytest.mark.parametrize(
    "column",
    [
        {"id": "n", "title": "N", "type": "integer"},
        {"id": "n", "title": "N", "type": "integer", "min": 0},
        {"id": "n", "title": "N", "type": "integer", "max": 10},
        {"id": "n", "title": "N", "type": "integer", "min": 10, "max": 0},
        {
            "id": "n",
            "title": "N",
            "type": "float",
            "min": float("nan"),
            "max": 1,
        },
    ],
)
def test_format_table_drops_incomplete_numeric_ranges(column) -> None:
    settings = _Settings()
    settings.settings["table-a"]["columns"] = [column]
    widget = _BoundFormatList("table-a", settings.settings["table-a"], settings)

    try:
        assert widget.columns == []
    finally:
        widget.destroy()


def test_malformed_table_value_does_not_break_selector() -> None:
    settings = _Settings()
    settings.settings["table-a"]["value"] = None

    selector = FormatTableSelector(
        {"tables": [{"key": "table-a", "label": "A"}]},
        "format-table-selector",
        settings,
    )

    try:
        assert len(selector._tables["table-a"].model) == 0
        settings.settings["table-a"]["value"] = [None, "broken", {"name": "ok"}]
        selector._tables["table-a"].on_setting_changed()
        assert len(selector._tables["table-a"].model) == 1
        settings.settings["table-a"]["value"] = [{"name": 123}]
        selector._tables["table-a"].on_setting_changed()
        assert len(selector._tables["table-a"].model) == 0
    finally:
        selector.destroy()


def test_format_table_ignores_table_read_error() -> None:
    class BrokenTableReadSettings(_Settings):
        def get_value(self, key):
            if key == "table-a":
                raise RuntimeError(key)
            return super().get_value(key)

    settings = BrokenTableReadSettings()
    selector = FormatTableSelector(
        {"tables": [{"key": "table-a", "label": "A"}]},
        "format-table-selector",
        settings,
    )

    try:
        assert len(selector._tables["table-a"].model) == 0
    finally:
        selector.destroy()


def test_format_table_list_ignores_write_error() -> None:
    class BrokenWriteSettings(_Settings):
        def set_value(self, key, value):
            if key == "table-a":
                raise RuntimeError(key)
            return super().set_value(key, value)

    settings = BrokenWriteSettings()
    widget = _BoundFormatList("table-a", settings.settings["table-a"], settings)

    try:
        widget.model.append(["value"])
        widget.list_changed()
        assert widget._saving is False
    finally:
        widget.destroy()


def test_format_table_row_actions_guard_selection() -> None:
    settings = _Settings()
    widget = _BoundFormatList("table-a", settings.settings["table-a"], settings)

    try:
        widget.remove_item()
        widget.move_item_up()
        widget.move_item_down()
        assert len(widget.model) == 0
    finally:
        widget.destroy()


def test_format_table_ignores_integer_overflow_in_persisted_row() -> None:
    settings = _Settings()
    settings.settings["table-a"]["columns"] = [
        {"id": "value", "title": "Value", "type": "integer"},
    ]
    settings.settings["table-a"]["value"] = [{"value": 2**31}]

    selector = FormatTableSelector(
        {"tables": [{"key": "table-a", "label": "A"}]},
        "format-table-selector",
        settings,
    )

    try:
        assert len(selector._tables["table-a"].model) == 0
    finally:
        selector.destroy()


def test_format_table_ignores_unknown_option_values() -> None:
    settings = _Settings()
    settings.settings["table-a"]["columns"] = [
        {"id": "name", "title": "Name", "type": "string"},
        {
            "id": "mode",
            "title": "Mode",
            "type": "integer",
            "options": {"Immer": 0, "Aus": 3},
        },
    ]
    settings.settings["table-a"]["value"] = [
        {"name": "ok", "mode": 0},
        {"name": "bad", "mode": 99},
    ]

    selector = FormatTableSelector(
        {"tables": [{"key": "table-a", "label": "A"}]},
        "format-table-selector",
        settings,
    )

    try:
        assert len(selector._tables["table-a"].model) == 1
        assert list(selector._tables["table-a"].model[0]) == ["ok", 0]
    finally:
        selector.destroy()


@pytest.mark.parametrize("options", [["A", "B"], ("A", "B")])
def test_format_table_ignores_unknown_sequence_option_values(options) -> None:
    settings = _Settings()
    settings.settings["table-a"]["columns"] = [
        {
            "id": "mode",
            "title": "Mode",
            "type": "string",
            "options": options,
        }
    ]
    settings.settings["table-a"]["value"] = [{"mode": "C"}]

    selector = FormatTableSelector(
        {"tables": [{"key": "table-a", "label": "A"}]},
        "format-table-selector",
        settings,
    )

    try:
        assert len(selector._tables["table-a"].model) == 0
    finally:
        selector.destroy()
