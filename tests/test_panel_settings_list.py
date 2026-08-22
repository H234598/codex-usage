from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "files" / "codex-usage@H234598"))
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings")
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings/bin")

import panel_settings_list as panel_settings_list_module  # noqa: E402
from panel_settings_list import (  # noqa: E402
    Gtk,
    PanelSettingsList,
    _panel_slot_id,
    panel_apply_value_settings,
    panel_columns,
    panel_edit_columns,
    panel_editor_dimensions,
    panel_editor_positions,
    panel_value_count,
    panel_value_settings,
)


def test_panel_value_count_defaults_and_bounds() -> None:
    assert panel_value_count("20") == 20
    assert panel_value_count("64") == 64
    assert panel_value_count(" 2 ") == 2
    assert panel_value_count("2.0") == 20
    assert panel_value_count(2.5) == 20
    assert panel_value_count("0") == 20
    assert panel_value_count("not-a-number") == 20


@pytest.mark.parametrize(
    "slot_id",
    ["slot" + chr(0x661), "slot" + chr(0xFF11), "slot0", "slot01", "slotfoo"],
)
def test_panel_slot_id_rejects_noncanonical_ids(slot_id) -> None:
    assert _panel_slot_id(slot_id) is False


def test_panel_columns_replaces_noncanonical_slot_ids() -> None:
    columns = panel_columns(
        [{"id": "slot" + chr(0x661), "title": "Wert", "type": "integer"}],
        1,
    )

    assert [column["id"] for column in columns] == ["slot1"]


def test_panel_edit_columns_defaults_and_bounds() -> None:
    assert panel_edit_columns("2") == 2
    assert panel_edit_columns(3) == 3
    assert panel_edit_columns("5") == 5
    assert panel_edit_columns("2.0") == 3
    assert panel_edit_columns(2.5) == 3
    assert panel_edit_columns("1") == 3
    assert panel_edit_columns("6") == 3
    assert panel_edit_columns("not-a-number") == 3
    assert panel_edit_columns(True) == 3


def test_panel_editor_fills_value_columns_top_to_bottom() -> None:
    columns = [
        {"id": "account", "title": "Account", "type": "string"},
        *[
            {"id": f"slot{index}", "title": f"Wert {index}", "type": "integer"}
            for index in range(1, 16)
        ],
    ]

    positions = panel_editor_positions(columns, 3)

    assert positions[0] == (0, 0)
    assert [positions[index][0] for index in range(1, 6)] == [0] * 5
    assert [positions[index][1] for index in range(1, 6)] == list(range(1, 6))
    assert [positions[index][0] for index in range(6, 11)] == [1] * 5
    assert [positions[index][1] for index in range(6, 11)] == list(range(1, 6))
    assert [positions[index][0] for index in range(11, 16)] == [2] * 5
    assert [positions[index][1] for index in range(11, 16)] == list(range(1, 6))


def test_panel_value_snapshot_preserves_non_value_fields() -> None:
    columns = [
        {"id": "account", "title": "Account", "type": "string"},
        {"id": "slot1", "title": "Wert 1", "type": "integer"},
        {"id": "slot2", "title": "Wert 2", "type": "integer"},
    ]
    source = {"account": "source", "slot1": 4, "slot2": 5, "muted": True}
    target = {"account": "target", "slot1": 0, "slot2": 1, "muted": False}

    copied = panel_value_settings(columns, source)
    pasted = panel_apply_value_settings(columns, target, copied)

    assert copied == {"slot1": 4, "slot2": 5}
    assert pasted == {"account": "target", "slot1": 4, "slot2": 5, "muted": False}


def test_panel_value_snapshot_helpers_ignore_malformed_columns() -> None:
    row = {"account": "target", "slot1": 4}

    assert panel_value_settings(None, row) == {}
    assert panel_apply_value_settings(None, row, {"slot1": 9}) == row


def test_panel_editor_dimensions_scale_with_values_and_columns() -> None:
    narrow = panel_editor_dimensions(15, 3, 1)
    wide = panel_editor_dimensions(15, 5, 1)

    assert narrow[0] < wide[0]
    assert narrow[1] > wide[1]


def test_panel_editor_dimensions_skip_unused_columns() -> None:
    width, _height = panel_editor_dimensions(1, 5, 3)

    assert width == 3 * 280 + 160


def test_panel_editor_dimensions_handles_huge_counts() -> None:
    width, height = panel_editor_dimensions(10**1000, 3, 10**1000)

    assert (width, height) == (1000, 900)


def test_panel_columns_expand_legacy_schema_without_mutation() -> None:
    base = [
        {"id": "account", "title": "Account", "type": "string"},
        {"id": "slot1", "title": "Wert 1", "type": "integer", "options": {"Aus": 0}},
    ]

    columns = panel_columns(base, "20")

    assert [column["id"] for column in columns][-1] == "slot20"
    assert base[-1]["id"] == "slot1"
    assert columns[-1]["options"]["Abrufweg"] == 17


def test_panel_columns_default_slots_to_disabled_source() -> None:
    columns = panel_columns(
        [
            {"id": "account", "title": "Account", "type": "string"},
            {"id": "slot1", "title": "Wert 1", "type": "integer"},
        ],
        3,
    )

    assert [column["default"] for column in columns if column["id"].startswith("slot")] == [
        0,
        0,
        0,
    ]


def test_panel_columns_normalize_malformed_slot_type() -> None:
    columns = panel_columns(
        [
            {"id": "account", "title": "Account", "type": "string"},
            {"id": "slot1", "title": "Wert 1", "type": "string"},
        ],
        1,
    )

    slot = next(column for column in columns if column["id"] == "slot1")
    assert slot["type"] == "integer"
    assert slot["default"] == 0
    assert slot["options"]["Aus"] == 0


def test_panel_columns_drop_invalid_units_text() -> None:
    columns = panel_columns(
        [
            {
                "id": "value",
                "title": "Value",
                "type": "integer",
                "min": 0,
                "max": 10,
                "units": "bad\x00unit",
            }
        ],
        1,
    )

    assert "units" not in columns[0]


@pytest.mark.parametrize(
    "base",
    [
        None,
        "malformed",
        [None, {"id": "missing-title", "type": "string"}],
        [{"id": "unknown", "title": "Unknown", "type": "unknown"}],
        [{"id": "", "title": "Empty id", "type": "string"}],
        [{"id": "empty-title", "title": " ", "type": "string"}],
        [{"id": "bad\x00id", "title": "Bad id", "type": "string"}],
        [{"id": "bad-title", "title": "Bad\x00title", "type": "string"}],
        [{"id": "bad-options", "title": "Bad options", "type": "string", "options": 1}],
        [{"id": "number", "title": "Number", "type": "integer"}],
        [{"id": "number", "title": "Number", "type": "integer", "min": 10, "max": 1}],
        [{"id": "number", "title": "Number", "type": "integer", "min": float("nan"), "max": 10}],
    ],
)
def test_panel_columns_ignores_malformed_schema_columns(base) -> None:
    columns = panel_columns(base, 3)

    assert [column["id"] for column in columns] == ["slot1", "slot2", "slot3"]


def test_panel_columns_drops_invalid_and_duplicate_slot_ids() -> None:
    columns = panel_columns(
        [
            {"id": "account", "title": "Account", "type": "string"},
            {"id": "slot0", "title": "Zero", "type": "integer", "min": 0, "max": 10},
            {"id": "slot01", "title": "Leading zero", "type": "integer", "min": 0, "max": 10},
            {"id": "slotfoo", "title": "Foo", "type": "string"},
            {"id": "slot4", "title": "Four", "type": "integer", "min": 0, "max": 10},
            {"id": "slot1", "title": "One", "type": "integer", "min": 0, "max": 10},
            {"id": "slot1", "title": "Duplicate", "type": "integer", "min": 0, "max": 10},
        ],
        3,
    )

    assert [column["id"] for column in columns] == [
        "account", "slot1", "slot2", "slot3",
    ]


def test_panel_columns_sorts_valid_legacy_slots_numerically() -> None:
    options = {"Aus": 0}
    columns = panel_columns(
        [
            {"id": "account", "title": "Account", "type": "string"},
            {"id": "slot3", "title": "Wert 3", "type": "integer", "options": options},
            {"id": "slot1", "title": "Wert 1", "type": "integer", "options": options},
            {"id": "slot2", "title": "Wert 2", "type": "integer", "options": options},
        ],
        3,
    )

    assert [column["id"] for column in columns] == [
        "account", "slot1", "slot2", "slot3",
    ]


def test_panel_columns_ignores_overlong_numeric_slot_id() -> None:
    columns = panel_columns(
        [{"id": "slot" + ("9" * 5000), "title": "Huge", "type": "integer"}],
        2,
    )

    assert [column["id"] for column in columns] == ["slot1", "slot2"]


class _Dialog:
    last = None
    response = None

    def __init__(self, *_args, **_kwargs):
        self.content_area = Gtk.Box()
        self.default_size = None
        self.__class__.last = self

    def get_content_area(self):
        return self.content_area

    def run(self):
        return self.response or Gtk.ResponseType.CANCEL

    def set_default_size(self, width, height):
        self.default_size = (width, height)

    def destroy(self):
        pass


class _Settings:
    def __init__(self, edit_columns):
        self.values = {
            "account-panel-settings": [],
            "panel-value-count": "20",
            "panel-edit-columns": edit_columns,
        }
        self.listeners = {}

    def listen(self, key, callback):
        self.listeners.setdefault(key, []).append(callback)

    def get_value(self, key):
        return self.values[key]

    def set_value(self, key, value):
        self.values[key] = value


def test_panel_editor_places_values_vertically_in_selected_columns(monkeypatch) -> None:
    captured = {}

    class InspectingDialog(_Dialog):
        def run(self):
            frame = self.content_area.get_children()[0]
            scrolled = frame.get_child()
            captured["scrolled"] = scrolled
            captured["grid"] = scrolled.get_child().get_child()
            return super().run()

    monkeypatch.setattr(Gtk, "Dialog", InspectingDialog)
    info = {
        "columns": [
            {"id": "account", "title": "Account", "type": "string"},
            *[
                {"id": f"slot{index}", "title": f"Wert {index}", "type": "integer"}
                for index in range(1, 8)
            ],
        ],
        "show-buttons": False,
    }
    settings = _Settings(3)
    settings.values["panel-value-count"] = "7"
    panel = PanelSettingsList(info, "account-panel-settings", settings)

    panel.open_add_edit_dialog([None for _ in panel.columns])

    scrolled = captured["scrolled"]
    grid = captured["grid"]
    assert isinstance(grid, Gtk.Grid)
    assert InspectingDialog.last.default_size == panel_editor_dimensions(7, 3, 1)
    assert scrolled.get_size_request()[0] == panel_editor_dimensions(7, 3, 1)[0] - 60
    assert grid.get_child_at(0, 0) is not None
    assert grid.get_child_at(0, 1) is not None
    assert grid.get_child_at(0, 2) is not None
    assert grid.get_child_at(0, 3) is not None
    assert grid.get_child_at(1, 1) is not None
    assert grid.get_child_at(1, 2) is not None
    assert grid.get_child_at(1, 3) is not None
    assert grid.get_child_at(2, 1) is not None
    assert grid.get_child_at(2, 2) is None
    assert grid.get_child_at(2, 3) is None
    assert grid.get_child_at(3, 1) is None
    panel.destroy()


def test_panel_editor_returns_edited_values(monkeypatch) -> None:
    monkeypatch.setattr(Gtk, "Dialog", _Dialog)
    _Dialog.response = Gtk.ResponseType.OK
    info = {
        "columns": [
            {"id": "first", "title": "Erstes Feld", "type": "string"},
            {"id": "second", "title": "Zweites Feld", "type": "string"},
        ],
        "show-buttons": False,
    }
    panel = PanelSettingsList(info, "account-panel-settings", _Settings(3))
    try:
        values = panel.open_add_edit_dialog(
            ["alpha", "beta"] + [None] * (len(panel.columns) - 2)
        )
        assert values[:2] == ["alpha", "beta"]
    finally:
        _Dialog.response = None
        panel.destroy()


def test_panel_copy_paste_changes_only_selected_account_values() -> None:
    settings = _Settings(3)
    settings.values["panel-value-count"] = "2"
    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {
                    "id": "order",
                    "title": "Reihenfolge",
                    "type": "integer",
                    "min": 1,
                    "max": 100,
                    "default": 1,
                },
                {"id": "muted", "title": "Stumm", "type": "boolean", "default": False},
                {"id": "slot1", "title": "Wert 1", "type": "integer"},
                {"id": "slot2", "title": "Wert 2", "type": "integer"},
            ],
            "show-buttons": True,
            "hidden-buttons": ["+", "-", "up", "down"],
        },
        "account-panel-settings",
        settings,
    )

    try:
        assert panel.copy_button is not None
        assert panel.paste_button is not None
        panel.model.append(["source", 1, False, 4, 5])
        panel.model.append(["target", 9, True, 0, 1])
        selection = panel.content_widget.get_selection()
        selection.select_path("0")
        panel.copy_value_settings()
        assert panel.paste_button.get_sensitive() is True
        selection.select_path("1")
        panel.paste_value_settings()

        assert list(panel.model[1]) == ["target", 9, True, 4, 5]
        assert settings.values["account-panel-settings"][1] == {
            "account": "target",
            "order": 9,
            "muted": True,
            "slot1": 4,
            "slot2": 5,
        }
    finally:
        panel.destroy()


def test_panel_rebuild_clears_stale_copy_paste_selection() -> None:
    settings = _Settings(3)
    settings.values["panel-value-count"] = "2"
    settings.values["account-panel-settings"] = [
        {"account": "source", "slot1": 4, "slot2": 5},
        {"account": "target", "slot1": 0, "slot2": 1},
    ]
    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {"id": "slot1", "title": "Wert 1", "type": "integer"},
            ],
            "show-buttons": True,
            "hidden-buttons": ["+", "-", "up", "down"],
        },
        "account-panel-settings",
        settings,
    )

    try:
        selection = panel.content_widget.get_selection()
        selection.select_path("0")
        panel.copy_value_settings()
        assert panel.copy_button.get_sensitive() is True
        assert panel.paste_button.get_sensitive() is True

        settings.values["panel-value-count"] = "4"
        panel._on_count_changed()

        _model, tree_iter = panel.content_widget.get_selection().get_selected()
        assert tree_iter is None
        assert panel.copy_button.get_sensitive() is False
        assert panel.paste_button.get_sensitive() is False
    finally:
        panel.destroy()


def test_panel_editor_destroys_dialog_after_run_error(monkeypatch) -> None:
    class FailingDialog(_Dialog):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.destroyed = False

        def run(self):
            raise RuntimeError("dialog loop failed")

        def destroy(self):
            self.destroyed = True

    monkeypatch.setattr(Gtk, "Dialog", FailingDialog)
    panel = PanelSettingsList(
        {
            "columns": [{"id": "account", "title": "Account", "type": "string"}],
            "show-buttons": False,
        },
        "account-panel-settings",
        _Settings(3),
    )

    try:
        with pytest.raises(RuntimeError, match="dialog loop failed"):
            panel.open_add_edit_dialog()
        assert FailingDialog.last.destroyed is True
    finally:
        panel.destroy()


def test_panel_editor_destroys_editor_widgets_after_dialog(monkeypatch) -> None:
    monkeypatch.setattr(Gtk, "Dialog", _Dialog)
    destroyed = []

    class TrackingEntry(Gtk.Entry):
        def set_widget_value(self, value):
            self.set_text("" if value is None else str(value))

        def get_widget_value(self):
            return self.get_text()

        def destroy(self):
            destroyed.append(self)
            return super().destroy()

    monkeypatch.setattr(
        panel_settings_list_module,
        "list_edit_factory",
        lambda _column: TrackingEntry(),
    )
    panel = PanelSettingsList(
        {
            "columns": [{"id": "account", "title": "Account", "type": "string"}],
            "show-buttons": False,
        },
        "account-panel-settings",
        _Settings(3),
    )

    try:
        assert panel.open_add_edit_dialog() is None
        assert len(destroyed) == len(panel.columns)
    finally:
        panel.destroy()


def test_panel_editor_survives_widget_factory_error(monkeypatch) -> None:
    monkeypatch.setattr(Gtk, "Dialog", _Dialog)

    def fail_factory(_column):
        raise RuntimeError("editor factory failed")

    monkeypatch.setattr(panel_settings_list_module, "list_edit_factory", fail_factory)
    panel = PanelSettingsList(
        {
            "columns": [{"id": "account", "title": "Account", "type": "string"}],
            "show-buttons": False,
        },
        "account-panel-settings",
        _Settings(3),
    )

    try:
        assert panel.open_add_edit_dialog() is None
    finally:
        panel.destroy()


@pytest.mark.parametrize("info", [[], ["alpha"], {}, "alpha"])
def test_panel_editor_treats_short_or_malformed_info_as_empty(monkeypatch, info) -> None:
    monkeypatch.setattr(Gtk, "Dialog", _Dialog)
    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "first", "title": "Erstes Feld", "type": "string"},
                {"id": "second", "title": "Zweites Feld", "type": "string"},
            ],
            "show-buttons": False,
        },
        "account-panel-settings",
        _Settings(3),
    )
    try:
        assert panel.open_add_edit_dialog(info) is None
    finally:
        panel.destroy()


def test_panel_editor_ignores_invalid_existing_values(monkeypatch) -> None:
    monkeypatch.setattr(Gtk, "Dialog", _Dialog)
    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {
                    "id": "order",
                    "title": "Order",
                    "type": "integer",
                    "min": 1,
                    "max": 10,
                    "default": 1,
                },
            ],
            "show-buttons": False,
        },
        "account-panel-settings",
        _Settings(3),
    )
    try:
        assert panel.open_add_edit_dialog(["alpha", "bad"]) is None
    finally:
        panel.destroy()


def test_panel_editor_uses_disabled_source_for_missing_slots(monkeypatch) -> None:
    monkeypatch.setattr(Gtk, "Dialog", _Dialog)
    _Dialog.response = Gtk.ResponseType.OK
    settings = _Settings(3)
    settings.values["panel-value-count"] = "3"
    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "account", "title": "Account-ID", "type": "string"},
                {"id": "slot1", "title": "Wert 1", "type": "integer"},
            ],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )
    try:
        values = panel.open_add_edit_dialog([None] * len(panel.columns))
        assert values[1:] == [0, 0, 0]
    finally:
        _Dialog.response = None
        panel.destroy()


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
def test_panel_malformed_metadata_uses_safe_defaults(field, value) -> None:
    settings = _Settings(3)
    info = {
        "columns": [{"id": "account", "title": "Account", "type": "string"}],
        "description": "Description",
        "height": 100,
        "show-buttons": False,
        "hidden-buttons": ["up"],
        "tooltip": "Help",
    }
    info[field] = value

    panel = PanelSettingsList(info, "account-panel-settings", settings)
    try:
        assert panel.show_buttons is False
        assert panel.hidden_buttons == ([] if field == "hidden-buttons" else ["up"])
        assert panel.get_tooltip_text() == (None if field == "tooltip" else "Help")
        assert hasattr(panel, "label") is (field != "description")
    finally:
        panel.destroy()


@pytest.mark.parametrize("height", [float("nan"), float("inf"), -1, 220.5])
def test_panel_height_metadata_is_finite_and_integer(height) -> None:
    settings = _Settings(3)
    panel = PanelSettingsList(
        {
            "columns": [{"id": "account", "title": "Account", "type": "string"}],
            "height": height,
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        scrollbox = panel.get_children()[0]
        assert scrollbox.get_size_request()[1] == 220
    finally:
        panel.destroy()


@pytest.mark.parametrize("align", ["bad", float("nan"), float("inf"), -1, 2, True, None])
def test_panel_ignores_invalid_column_alignment(align) -> None:
    panel = PanelSettingsList(
        {
            "columns": [{
                "id": "account",
                "title": "Account",
                "type": "string",
                "align": align,
            }],
            "show-buttons": False,
        },
        "account-panel-settings",
        _Settings(3),
    )

    panel.destroy()


@pytest.mark.parametrize(
    "column",
    [
        {"id": "bad", "title": "Bad", "type": "string", "options": {"A": 1}},
        {"id": "bad", "title": "Bad", "type": "float", "options": {"A": "1"}},
        {"id": "bad", "title": "Bad", "type": "integer", "options": [1, 2]},
        {"id": "bad", "title": "Bad", "type": "integer", "options": {"A": 2**31}},
        {"id": "bad", "title": "Bad", "type": "integer", "options": {"A": -(2**31) - 1}},
        {"id": "bad", "title": "Bad", "type": "string", "options": {1: "A"}},
        {"id": "bad", "title": "Bad", "type": "string", "options": {"A": "bad\x00value"}},
    ],
)
def test_panel_editor_drops_malformed_combo_options(monkeypatch, column) -> None:
    monkeypatch.setattr(Gtk, "Dialog", _Dialog)
    settings = _Settings(3)
    settings.values["panel-value-count"] = "1"
    panel = PanelSettingsList(
        {"columns": [column], "show-buttons": False},
        "account-panel-settings",
        settings,
    )

    try:
        assert all(item["id"] != "bad" for item in panel.columns)
        assert panel.open_add_edit_dialog([None] * len(panel.columns)) is None
    finally:
        panel.destroy()


def test_panel_editor_strips_spin_properties_from_combo_columns(monkeypatch) -> None:
    monkeypatch.setattr(Gtk, "Dialog", _Dialog)
    settings = _Settings(3)
    settings.values["panel-value-count"] = "1"
    column = {
        "id": "field",
        "title": "Field",
        "type": "string",
        "options": {"A": "a"},
        "min": 0,
        "max": 1,
        "step": 1,
        "units": "x",
        "expand-width": True,
    }
    panel = PanelSettingsList(
        {"columns": [column], "show-buttons": False},
        "account-panel-settings",
        settings,
    )

    try:
        assert all(key not in panel.columns[0] for key in (
            "min", "max", "step", "units", "expand-width",
        ))
        assert panel.open_add_edit_dialog([None] * len(panel.columns)) is None
    finally:
        panel.destroy()


@pytest.mark.parametrize("step", [0, -1, "bad", float("nan"), float("inf")])
def test_panel_editor_drops_invalid_numeric_step(monkeypatch, step) -> None:
    monkeypatch.setattr(Gtk, "Dialog", _Dialog)
    settings = _Settings(3)
    settings.values["panel-value-count"] = "1"
    panel = PanelSettingsList(
        {
            "columns": [{
                "id": "field",
                "title": "Field",
                "type": "integer",
                "min": 0,
                "max": 10,
                "step": step,
            }],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        assert "step" not in panel.columns[0]
        assert panel.open_add_edit_dialog([None] * len(panel.columns)) is None
    finally:
        panel.destroy()


@pytest.mark.parametrize(
    ("column_type", "property_name", "property_value"),
    [
        ("string", "min", 0),
        ("file", "min", 0),
        ("icon", "step", 1),
        ("sound", "expand-width", True),
    ],
)
def test_panel_editor_strips_properties_unsupported_by_widget(
    monkeypatch, column_type, property_name, property_value
) -> None:
    monkeypatch.setattr(Gtk, "Dialog", _Dialog)
    settings = _Settings(3)
    settings.values["panel-value-count"] = "1"
    column = {
        "id": "field",
        "title": "Field",
        "type": column_type,
        property_name: property_value,
    }
    panel = PanelSettingsList(
        {"columns": [column], "show-buttons": False},
        "account-panel-settings",
        settings,
    )

    try:
        assert property_name not in panel.columns[0]
        assert panel.open_add_edit_dialog([None] * len(panel.columns)) is None
    finally:
        panel.destroy()


def test_panel_add_item_ignores_invalid_widget_values(monkeypatch) -> None:
    monkeypatch.setattr(Gtk, "Dialog", _Dialog)
    _Dialog.response = Gtk.ResponseType.OK
    settings = _Settings(3)
    settings.values["panel-value-count"] = "1"
    panel = PanelSettingsList(
        {
            "columns": [{
                "id": "field",
                "title": "Field",
                "type": "integer",
                "options": {"A": 1},
                "default": 2**40,
            }],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        panel.add_item()
        assert len(panel.model) == 0
    finally:
        _Dialog.response = None
        panel.destroy()


def test_panel_edit_item_restores_row_after_invalid_widget_value() -> None:
    settings = _Settings(3)
    settings.values["panel-value-count"] = "1"
    panel = PanelSettingsList(
        {
            "columns": [{
                "id": "field",
                "title": "Field",
                "type": "integer",
                "options": {"A": 1},
            }],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        panel.model.append([1, 0])
        panel.content_widget.get_selection().select_path("0")
        panel.open_add_edit_dialog = lambda _info: [2**40, 0]
        panel.edit_item()
        assert list(panel.model[0]) == [1, 0]
    finally:
        panel.destroy()


def test_panel_row_actions_ignore_missing_selection() -> None:
    settings = _Settings(3)
    settings.values["panel-value-count"] = "1"
    panel = PanelSettingsList(
        {
            "columns": [{"id": "account", "title": "Account", "type": "string"}],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        panel.remove_item()
        panel.move_item_up()
        panel.move_item_down()
        assert len(panel.model) == 0
    finally:
        panel.destroy()


def test_panel_row_actions_move_and_remove_selected() -> None:
    settings = _Settings(3)
    settings.values["panel-value-count"] = "1"
    panel = PanelSettingsList(
        {
            "columns": [{"id": "account", "title": "Account", "type": "string"}],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        panel.model.append(["alpha", 0])
        panel.model.append(["beta", 0])
        selection = panel.content_widget.get_selection()
        selection.select_path("0")
        panel.move_item_down()
        assert [row[0] for row in panel.model] == ["beta", "alpha"]
        selection.select_path("0")
        panel.remove_item()
        assert [row[0] for row in panel.model] == ["alpha"]
        assert settings.values["account-panel-settings"][0]["account"] == "alpha"
    finally:
        panel.destroy()


def test_panel_edit_callbacks_skip_write_on_settings_read_error() -> None:
    class BrokenRowsSettings(_Settings):
        def get_value(self, key):
            if key == "account-panel-settings":
                raise KeyError(key)
            return super().get_value(key)

    settings = BrokenRowsSettings(3)
    settings.values["panel-value-count"] = "1"
    panel = PanelSettingsList(
        {
            "columns": [{"id": "account", "title": "Account", "type": "string"}],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        panel.model.append(["alpha", 0])
        panel.list_changed()
        assert settings.values["account-panel-settings"] == []
        settings.values["panel-value-count"] = "2"
        panel._on_count_changed()
        assert list(panel.model[0]) == ["alpha", 0, 0]
    finally:
        panel.destroy()


@pytest.mark.parametrize("error", [OSError, RuntimeError])
def test_panel_list_change_recovers_after_settings_write_error(error) -> None:
    class BrokenWriteSettings(_Settings):
        def set_value(self, key, value):
            if key == "account-panel-settings":
                raise error("settings file unavailable")
            return super().set_value(key, value)

    settings = BrokenWriteSettings(3)
    settings.values["panel-value-count"] = "1"
    panel = PanelSettingsList(
        {
            "columns": [{"id": "account", "title": "Account", "type": "string"}],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        panel.model.append(["alpha", 0])
        panel.list_changed()
        assert panel._saving is False
    finally:
        panel.destroy()


def test_panel_destroy_detaches_settings_listeners() -> None:
    settings = _Settings(3)
    panel = PanelSettingsList(
        {"columns": [], "show-buttons": False},
        "account-panel-settings",
        settings,
    )

    assert len(settings.listeners["account-panel-settings"]) == 1
    assert len(settings.listeners["panel-value-count"]) == 1
    panel.destroy()

    assert settings.listeners["account-panel-settings"] == []
    assert settings.listeners["panel-value-count"] == []


@pytest.mark.parametrize(
    "rows",
    [
        None,
        ["malformed"],
        [{"account": 123}],
        [{"account": "alpha", "slot1": 99}],
        [{"account": "alpha", "slot1": 2**31}],
    ],
)
def test_panel_ignores_malformed_persisted_rows(rows) -> None:
    settings = _Settings(3)
    settings.values["account-panel-settings"] = rows

    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {"id": "slot1", "title": "Wert 1", "type": "integer"},
            ],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        assert len(panel.model) == 0
        settings.values["panel-value-count"] = "21"
        panel._on_count_changed()
        assert len(panel.model) == 0
    finally:
        panel.destroy()


@pytest.mark.parametrize("options", [["A", "B"], ("A", "B")])
def test_panel_ignores_unknown_sequence_option_values_on_load(options) -> None:
    settings = _Settings(3)
    settings.values["panel-value-count"] = "1"
    settings.values["account-panel-settings"] = [
        {"account": "alpha", "mode": "unknown"}
    ]
    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {
                    "id": "mode",
                    "title": "Mode",
                    "type": "string",
                    "options": options,
                },
            ],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        assert len(panel.model) == 0
        settings.values["panel-value-count"] = "2"
        panel._on_count_changed()
        assert len(panel.model) == 0
    finally:
        panel.destroy()


def test_panel_ignores_settings_read_errors() -> None:
    class BrokenRowsSettings(_Settings):
        def get_value(self, key):
            if key == "account-panel-settings":
                raise KeyError(key)
            return super().get_value(key)

    settings = BrokenRowsSettings(3)
    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
            ],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        assert len(panel.model) == 0
    finally:
        panel.destroy()


def test_panel_ignores_settings_read_overflow() -> None:
    class OverflowRowsSettings(_Settings):
        def get_value(self, key):
            if key == "account-panel-settings":
                raise OverflowError(key)
            return super().get_value(key)

    settings = OverflowRowsSettings(3)
    panel = PanelSettingsList(
        {
            "columns": [{"id": "account", "title": "Account", "type": "string"}],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        assert len(panel.model) == 0
    finally:
        panel.destroy()


@pytest.mark.parametrize("error", [OSError, RuntimeError])
def test_panel_survives_settings_listener_registration_error(error) -> None:
    class BrokenListenerSettings(_Settings):
        def listen(self, key, callback):
            raise error(key)

    settings = BrokenListenerSettings(3)
    panel = PanelSettingsList(
        {
            "columns": [{"id": "account", "title": "Account", "type": "string"}],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        assert len(panel.model) == 0
    finally:
        panel.destroy()


def test_panel_survives_listener_container_error() -> None:
    class BrokenListenerContainerSettings(_Settings):
        def __getattribute__(self, name):
            if name == "listeners":
                raise RuntimeError("listener container unavailable")
            return super().__getattribute__(name)

        def listen(self, key, callback):
            raise RuntimeError(key)

    settings = BrokenListenerContainerSettings(3)
    panel = PanelSettingsList(
        {
            "columns": [{"id": "account", "title": "Account", "type": "string"}],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        assert len(panel.model) == 0
    finally:
        panel.destroy()


def test_panel_uses_defaults_for_count_and_editor_read_overflow() -> None:
    class BrokenNumericSettings(_Settings):
        def get_value(self, key):
            if key in {"panel-value-count", "panel-edit-columns"}:
                raise OverflowError(key)
            return super().get_value(key)

    settings = BrokenNumericSettings(3)
    panel = PanelSettingsList(
        {
            "columns": [{"id": "account", "title": "Account", "type": "string"}],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        assert [column["id"] for column in panel.columns][:2] == ["account", "slot1"]
        assert panel._read_edit_columns() == 3
    finally:
        panel.destroy()


def test_panel_option_renderer_clears_stale_label_for_unknown_value() -> None:
    settings = _Settings(3)
    settings.values["panel-value-count"] = "1"
    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "account", "title": "Account-ID", "type": "string"},
                {
                    "id": "slot1",
                    "title": "Wert 1",
                    "type": "integer",
                    "options": {"Aus": 0, "5h": 1},
                },
            ],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        settings.values["panel-value-count"] = "2"
        panel._on_count_changed()
        panel.model.append(["alpha", 1, 0])
        panel.model.append(["beta", 99, 0])
        column = panel.content_widget.get_column(1)
        renderer = column.get_cells()[0]
        column.cell_set_cell_data(panel.model, panel.model.get_iter(0), False, False)
        assert renderer.get_property("text") == "5h"
        column.cell_set_cell_data(panel.model, panel.model.get_iter(1), False, False)
        assert renderer.get_property("text") == ""
    finally:
        panel.destroy()


def test_panel_count_change_rebuilds_slots_and_preserves_rows() -> None:
    settings = _Settings(3)
    settings.values["panel-value-count"] = "2"
    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {"id": "slot1", "title": "Wert 1", "type": "integer"},
            ],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        panel.model.append(["alpha", 7, 8])
        settings.values["panel-value-count"] = "4"
        panel._on_count_changed()

        assert [column["id"] for column in panel.columns] == [
            "account", "slot1", "slot2", "slot3", "slot4",
        ]
        assert list(panel.model[0]) == ["alpha", 7, 8, 0, 0]
    finally:
        panel.destroy()


def test_panel_count_change_destroys_replaced_tree_view() -> None:
    settings = _Settings(3)
    settings.values["panel-value-count"] = "2"
    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {"id": "slot1", "title": "Wert 1", "type": "integer"},
            ],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )
    destroyed = []
    old_tree = panel.content_widget
    old_tree.connect("destroy", lambda *_args: destroyed.append(True))

    try:
        settings.values["panel-value-count"] = "3"
        panel._on_count_changed()

        assert destroyed == [True]
    finally:
        panel.destroy()


def test_panel_count_hides_legacy_slots_and_restores_saved_values() -> None:
    settings = _Settings(3)
    settings.values["panel-value-count"] = "2"
    settings.values["account-panel-settings"] = [
        {
            "account": "alpha",
            "slot1": 1,
            "slot2": 2,
            "slot3": 3,
            "slot4": 4,
        }
    ]
    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {"id": "slot1", "title": "Wert 1", "type": "integer"},
                {"id": "slot2", "title": "Wert 2", "type": "integer"},
                {"id": "slot3", "title": "Wert 3", "type": "integer"},
                {"id": "slot4", "title": "Wert 4", "type": "integer"},
            ],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        assert [column["id"] for column in panel.columns] == [
            "account", "slot1", "slot2",
        ]
        panel.model[0][1] = 9
        panel.list_changed()
        assert settings.values["account-panel-settings"][0]["slot3"] == 3
        settings.values["panel-value-count"] = "4"
        panel._on_count_changed()

        assert [column["id"] for column in panel.columns] == [
            "account", "slot1", "slot2", "slot3", "slot4",
        ]
        assert list(panel.model[0]) == ["alpha", 9, 2, 3, 4]
    finally:
        panel.destroy()


def test_panel_count_change_keeps_valid_hidden_slots_with_malformed_rows() -> None:
    settings = _Settings(3)
    settings.values["panel-value-count"] = "2"
    settings.values["account-panel-settings"] = [
        {"account": "alpha", "slot1": 1, "slot2": 2, "slot3": 3},
        "malformed",
    ]
    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {"id": "slot1", "title": "Wert 1", "type": "integer"},
                {"id": "slot2", "title": "Wert 2", "type": "integer"},
            ],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        settings.values["panel-value-count"] = "3"
        panel._on_count_changed()
        assert list(panel.model[0]) == ["alpha", 1, 2, 3]
    finally:
        panel.destroy()


def test_panel_list_change_keeps_hidden_slots_after_malformed_row() -> None:
    settings = _Settings(3)
    settings.values["panel-value-count"] = "2"
    settings.values["account-panel-settings"] = [
        "malformed",
        {"account": "alpha", "slot1": 1, "slot2": 2, "slot3": 3},
    ]
    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {"id": "slot1", "title": "Wert 1", "type": "integer"},
                {"id": "slot2", "title": "Wert 2", "type": "integer"},
            ],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        panel.model[0][0] = "beta"
        panel.list_changed()
        assert settings.values["account-panel-settings"][0] == {
            "account": "beta", "slot1": 1, "slot2": 2, "slot3": 3,
        }
    finally:
        panel.destroy()


def test_panel_list_change_ignores_invalid_account_row_for_position_fallback() -> None:
    settings = _Settings(3)
    settings.values["panel-value-count"] = "2"
    settings.values["account-panel-settings"] = [
        {"account": 123, "slot1": 9, "slot3": 99},
        {"account": "alpha", "slot1": 1, "slot2": 2, "slot3": 3},
    ]
    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {"id": "slot1", "title": "Wert 1", "type": "integer"},
                {"id": "slot2", "title": "Wert 2", "type": "integer"},
            ],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        panel.model[0][0] = "beta"
        panel.list_changed()
        assert settings.values["account-panel-settings"] == [
            {"account": "beta", "slot1": 1, "slot2": 2, "slot3": 3},
        ]
    finally:
        panel.destroy()


def test_panel_list_change_uses_position_for_duplicate_accounts() -> None:
    settings = _Settings(3)
    settings.values["panel-value-count"] = "2"
    settings.values["account-panel-settings"] = [
        {"account": "alpha", "slot1": 1, "slot2": 2, "slot3": 3},
        {"account": "alpha", "slot1": 9, "slot2": 8, "slot3": 99},
    ]
    panel = PanelSettingsList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {"id": "slot1", "title": "Wert 1", "type": "integer"},
                {"id": "slot2", "title": "Wert 2", "type": "integer"},
            ],
            "show-buttons": False,
        },
        "account-panel-settings",
        settings,
    )

    try:
        panel.model[1][0] = "beta"
        panel.list_changed()
        assert settings.values["account-panel-settings"] == [
            {"account": "alpha", "slot1": 1, "slot2": 2, "slot3": 3},
            {"account": "beta", "slot1": 9, "slot2": 8, "slot3": 99},
        ]
    finally:
        panel.destroy()
