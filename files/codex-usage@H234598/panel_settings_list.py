#!/usr/bin/env python3
"""Panel table with a user-selected number of ordered value columns."""

from __future__ import annotations

import copy

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402
from JsonSettingsWidgets import JSONSettingsBackend  # noqa: E402
from TreeListWidgets import VARIABLE_TYPE_MAP, List  # noqa: E402

_DEFAULT_COUNT = 20
_MAX_COUNT = 64

_SOURCE_OPTIONS = {
    "Aus": 0,
    "5h": 1,
    "Woche": 2,
    "Mittelwert": 3,
    "Spark 5h": 4,
    "Spark Woche": 5,
    "Spark Mittelwert": 6,
    "Spark weiteres Limit": 7,
    "30-Tage-Limit": 8,
    "Credits": 9,
    "Creditverbrauch": 10,
    "Resets": 11,
    "Tokenende": 12,
    "Tokendelta": 13,
    "Kürzel": 14,
    "Label": 15,
    "Account-ID": 16,
    "Abrufweg": 17,
    "sonstiges": 18,
    "Spark sonstiges": 19,
    "Rest 5h": 20,
    "Rest Woche": 21,
    "Rest 30 Tage": 22,
    "Rest Spark 5h": 23,
    "Rest Spark Woche": 24,
    "Rest Spark sonstiges": 25,
    "Reset 5h": 26,
    "Reset Woche": 27,
    "Reset 30 Tage": 28,
    "Reset Spark 5h": 29,
    "Reset Spark Woche": 30,
    "Reset Spark sonstiges": 31,
    "Delta 5h": 32,
    "Delta Woche": 33,
    "Delta 30 Tage": 34,
    "Delta Spark": 35,
    "Delta sonstiges": 36,
    "Limit 5h %": 37,
    "Limit Woche %": 38,
    "Limit 30 Tage %": 39,
    "Limit Spark 5h %": 40,
    "Limit Spark Woche %": 41,
    "Limit Spark sonstiges %": 42,
    "Routing": 43,
    "Creditverbrauch aktiv": 44,
    "Credit Stundenlimit": 45,
    "Credit Wochenlimit": 46,
    "Credit Monatslimit": 47,
    "Warnungen": 48,
    "Fehler": 49,
    "Login erfolgreich": 50,
    "Status": 51,
}


def panel_value_count(value: object) -> int:
    """Return bounded panel column count; malformed settings use default."""
    try:
        count = int(value) if not isinstance(value, bool) else 0
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_COUNT
    return count if 1 <= count <= _MAX_COUNT else _DEFAULT_COUNT


def panel_columns(base_columns: list[dict[str, object]], count: object) -> list[dict[str, object]]:
    """Expand legacy slot columns to requested count without mutating schema."""
    columns = copy.deepcopy(base_columns)
    slot_template = next(
        (column for column in columns if column.get("id") == "slot1"),
        {"id": "slot1", "title": "Wert 1", "type": "integer"},
    )
    for column in columns:
        if str(column.get("id", "")).startswith("slot"):
            column["options"] = dict(_SOURCE_OPTIONS)
    existing = {column.get("id") for column in columns}
    for index in range(1, panel_value_count(count) + 1):
        key = f"slot{index}"
        if key in existing:
            continue
        column = copy.deepcopy(slot_template)
        column["id"] = key
        column["title"] = f"Wert {index}"
        column["options"] = dict(_SOURCE_OPTIONS)
        columns.append(column)
    return columns


class PanelSettingsList(List, JSONSettingsBackend):
    """Render legacy and newly requested panel slots in one account table."""

    def __init__(self, info, key, settings):
        self.backend = "json"
        self.key = key
        self.settings = settings
        self._base_columns = copy.deepcopy(info.get("columns", []))
        columns = panel_columns(self._base_columns, self._read_count())
        super().__init__(
            label=info.get("description"),
            columns=columns,
            height=info.get("height", 220),
            show_buttons=info.get("show-buttons", False),
            hidden_buttons=info.get("hidden-buttons", []),
            tooltip=info.get("tooltip", ""),
        )
        self.attach()
        try:
            settings.listen("panel-value-count", self._on_count_changed)
        except (AttributeError, TypeError):
            pass

    def _read_count(self) -> int:
        try:
            return panel_value_count(self.settings.get_value("panel-value-count"))
        except (AttributeError, KeyError, TypeError, ValueError):
            return _DEFAULT_COUNT

    def _on_count_changed(self, *_args) -> None:
        columns = panel_columns(self._base_columns, self._read_count())
        if [column["id"] for column in columns] == [column["id"] for column in self.columns]:
            return
        rows = []
        for row in self.model:
            rows.append({
                column["id"]: row[index]
                for index, column in enumerate(self.columns)
            })
        self._rebuild_tree(columns, rows)

    def _rebuild_tree(
        self,
        columns: list[dict[str, object]],
        rows: list[dict[str, object]],
    ) -> None:
        children = self.get_children()
        if not children:
            return
        scrollbox = children[0]
        scrollbox.remove(self.content_widget)
        self.content_widget = Gtk.TreeView()
        scrollbox.add(self.content_widget)
        self.columns = columns

        types = []
        for index, column_def in enumerate(columns):
            types.append(VARIABLE_TYPE_MAP[column_def["type"]])
            has_options = isinstance(column_def.get("options"), dict)
            render_type = "string" if has_options else column_def["type"]
            if render_type == "boolean":
                renderer = Gtk.CellRendererToggle()

                def toggle_checkbox(_renderer, path, column):
                    self.model[path][column] = not self.model[path][column]
                    self.list_changed()

                renderer.connect("toggled", toggle_checkbox, index)
                prop_name = "active"
            elif render_type == "icon":
                renderer = Gtk.CellRendererPixbuf()
                prop_name = "icon_name"
            else:
                renderer = Gtk.CellRendererText()
                prop_name = "text"
            tree_column = Gtk.TreeViewColumn(column_def["title"], renderer)
            if has_options:
                def map_func(_column, rend, model, row_iter, data):
                    value = model[row_iter][data[1]]
                    for label, mapped in data[0].items():
                        if mapped == value:
                            rend.set_property("text", label)

                tree_column.set_cell_data_func(renderer, map_func, [column_def["options"], index])
            else:
                tree_column.add_attribute(renderer, prop_name, index)
            if "align" in column_def:
                renderer.set_alignment(column_def["align"], 0.5)
                tree_column.set_alignment(column_def["align"])
            tree_column.set_resizable(True)
            self.content_widget.append_column(tree_column)

        self.model = Gtk.ListStore(*types)
        self.content_widget.set_model(self.model)
        for row in rows:
            values = []
            for column in columns:
                value = row.get(column["id"], column.get("default"))
                values.append(value)
            self.model.append(values)
        self.content_widget.get_selection().connect("changed", self.update_button_sensitivity)
        self.content_widget.set_activate_on_single_click(False)
        self.content_widget.connect("row-activated", self.on_row_activated)
        self.content_widget.columns_autosize()
        self.show_all()
