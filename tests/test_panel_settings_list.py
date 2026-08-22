from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "files" / "codex-usage@H234598"))
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings")
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings/bin")

from panel_settings_list import (  # noqa: E402
    Gtk,
    PanelSettingsList,
    panel_columns,
    panel_edit_columns,
    panel_value_count,
)


def test_panel_value_count_defaults_and_bounds() -> None:
    assert panel_value_count("20") == 20
    assert panel_value_count("64") == 64
    assert panel_value_count(" 2 ") == 2
    assert panel_value_count("2.0") == 20
    assert panel_value_count(2.5) == 20
    assert panel_value_count("0") == 20
    assert panel_value_count("not-a-number") == 20


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


@pytest.mark.parametrize(
    "base",
    [
        None,
        "malformed",
        [None, {"id": "missing-title", "type": "string"}],
        [{"id": "unknown", "title": "Unknown", "type": "unknown"}],
        [{"id": "", "title": "Empty id", "type": "string"}],
        [{"id": "empty-title", "title": " ", "type": "string"}],
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
        self.__class__.last = self

    def get_content_area(self):
        return self.content_area

    def run(self):
        return self.response or Gtk.ResponseType.CANCEL

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


def test_panel_editor_places_fields_in_selected_grid_columns(monkeypatch) -> None:
    monkeypatch.setattr(Gtk, "Dialog", _Dialog)
    info = {
        "columns": [
            {"id": f"field{index}", "title": f"Feld {index}", "type": "string"}
            for index in range(7)
        ],
        "show-buttons": False,
    }
    panel = PanelSettingsList(info, "account-panel-settings", _Settings(3))

    panel.open_add_edit_dialog([None for _ in panel.columns])

    dialog = _Dialog.last
    frame = dialog.content_area.get_children()[0]
    scrolled = frame.get_child()
    grid = scrolled.get_child().get_child()
    assert isinstance(grid, Gtk.Grid)
    assert scrolled.get_size_request()[1] == 420
    assert grid.get_child_at(0, 0) is not None
    assert grid.get_child_at(1, 0) is not None
    assert grid.get_child_at(2, 0) is not None
    assert grid.get_child_at(0, 1) is not None
    assert grid.get_child_at(1, 1) is not None
    assert grid.get_child_at(2, 1) is not None
    assert grid.get_child_at(0, 2) is not None
    assert grid.get_child_at(1, 2) is not None
    assert grid.get_child_at(3, 0) is None
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
