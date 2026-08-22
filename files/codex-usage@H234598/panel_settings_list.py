#!/usr/bin/env python3
"""Panel table with a user-selected number of ordered value columns."""

from __future__ import annotations

import copy
from gettext import gettext as _

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402
from JsonSettingsWidgets import JSONSettingsBackend  # noqa: E402
from TreeListWidgets import VARIABLE_TYPE_MAP, List, list_edit_factory  # noqa: E402

_DEFAULT_COUNT = 20
_MAX_COUNT = 64
_DEFAULT_EDIT_COLUMNS = 3
_MIN_EDIT_COLUMNS = 2
_MAX_EDIT_COLUMNS = 5

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
        if isinstance(value, bool):
            count = 0
        elif isinstance(value, str):
            normalized = value.strip()
            if not normalized.isascii() or not normalized.isdecimal():
                return _DEFAULT_COUNT
            count = int(normalized)
        elif isinstance(value, int):
            count = value
        elif isinstance(value, float) and value.is_integer():
            count = int(value)
        else:
            return _DEFAULT_COUNT
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_COUNT
    return count if 1 <= count <= _MAX_COUNT else _DEFAULT_COUNT


def panel_edit_columns(value: object) -> int:
    """Return bounded editor grid columns; malformed settings use three."""
    try:
        if isinstance(value, bool):
            count = 0
        elif isinstance(value, str):
            normalized = value.strip()
            if not normalized.isascii() or not normalized.isdecimal():
                return _DEFAULT_EDIT_COLUMNS
            count = int(normalized)
        elif isinstance(value, int):
            count = value
        elif isinstance(value, float) and value.is_integer():
            count = int(value)
        else:
            return _DEFAULT_EDIT_COLUMNS
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_EDIT_COLUMNS
    return (
        count
        if _MIN_EDIT_COLUMNS <= count <= _MAX_EDIT_COLUMNS
        else _DEFAULT_EDIT_COLUMNS
    )


def panel_columns(base_columns: list[dict[str, object]], count: object) -> list[dict[str, object]]:
    """Expand legacy slot columns to requested count without mutating schema."""
    requested_count = panel_value_count(count)
    columns = [
        column
        for column in copy.deepcopy(base_columns)
        if not (
            isinstance(column.get("id"), str)
            and column["id"].startswith("slot")
            and column["id"][4:].isdecimal()
            and int(column["id"][4:]) > requested_count
        )
    ]
    slot_template = next(
        (column for column in columns if column.get("id") == "slot1"),
        {"id": "slot1", "title": "Wert 1", "type": "integer", "default": 0},
    )
    for column in columns:
        if str(column.get("id", "")).startswith("slot"):
            column["options"] = dict(_SOURCE_OPTIONS)
            column["default"] = 0
    existing = {column.get("id") for column in columns}
    for index in range(1, requested_count + 1):
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

    def _read_edit_columns(self) -> int:
        try:
            return panel_edit_columns(self.settings.get_value("panel-edit-columns"))
        except (AttributeError, KeyError, TypeError, ValueError):
            return _DEFAULT_EDIT_COLUMNS

    def _remove_listener(self, key, callback) -> None:
        listeners = getattr(self.settings, "listeners", None)
        if not isinstance(listeners, dict):
            return
        callbacks = listeners.get(key)
        if not isinstance(callbacks, list):
            return
        callbacks[:] = [registered for registered in callbacks if registered != callback]

    def on_setting_changed(self, *_args) -> None:
        """Load only list rows whose values fit the GTK model schema."""
        self.model.clear()
        rows = self.get_value()
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_info = []
            for column in self.columns:
                column_id = column["id"]
                if column_id in row:
                    row_info.append(row[column_id])
                elif "default" in column:
                    row_info.append(column["default"])
                else:
                    row_info.append(None)
            try:
                self.model.append(row_info)
            except (OverflowError, TypeError, ValueError):
                continue
        self.content_widget.columns_autosize()

    def detach(self) -> None:
        self._remove_listener(self.key, self._settings_changed_callback)
        self._remove_listener("panel-value-count", self._on_count_changed)

    def open_add_edit_dialog(self, info=None):
        """Edit one account row in a bounded, multi-column scrolled grid."""
        title = _("Add new entry") if info is None else _("Edit entry")
        dialog = Gtk.Dialog(
            title,
            self.get_toplevel(),
            Gtk.DialogFlags.MODAL,
            (
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_OK,
                Gtk.ResponseType.OK,
            ),
        )

        content_area = dialog.get_content_area()
        content_area.set_margin_right(30)
        content_area.set_margin_left(30)
        content_area.set_margin_top(20)
        content_area.set_margin_bottom(20)

        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.IN)
        frame.get_style_context().add_class("view")
        content_area.add(frame)

        scrollbox = Gtk.ScrolledWindow()
        scrollbox.set_size_request(-1, 420)
        scrollbox.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        frame.add(scrollbox)

        grid = Gtk.Grid()
        grid.set_column_spacing(18)
        grid.set_row_spacing(8)
        grid.set_border_width(12)
        grid.set_column_homogeneous(True)
        scrollbox.add(grid)

        widgets = []
        edit_columns = self._read_edit_columns()
        for index, column_definition in enumerate(self.columns):
            widget = list_edit_factory(column_definition)
            widgets.append(widget)

            settings_box = Gtk.ListBox()
            settings_box.set_selection_mode(Gtk.SelectionMode.NONE)
            settings_box.set_hexpand(True)
            settings_box.add(widget)
            grid.attach(settings_box, index % edit_columns, index // edit_columns, 1, 1)

            if info is not None and info[index] is not None:
                widget.set_widget_value(info[index])
            elif "default" in column_definition:
                widget.set_widget_value(column_definition["default"])

        content_area.show_all()
        response = dialog.run()

        if response == Gtk.ResponseType.OK:
            values = [widget.get_widget_value() for widget in widgets]
            dialog.destroy()
            return values

        dialog.destroy()
        return None

    def _on_count_changed(self, *_args) -> None:
        columns = panel_columns(self._base_columns, self._read_count())
        if [column["id"] for column in columns] == [column["id"] for column in self.columns]:
            return
        stored_rows = self.get_value()
        if (
            isinstance(stored_rows, list)
            and stored_rows
            and all(isinstance(row, dict) for row in stored_rows)
        ):
            rows = [dict(row) for row in stored_rows]
        else:
            rows = [
                {
                    column["id"]: row[index]
                    for index, column in enumerate(self.columns)
                }
                for row in self.model
            ]
        self._rebuild_tree(columns, rows)

    def list_changed(self, *args):
        """Save visible edits without discarding temporarily hidden slots."""
        stored_rows = self.get_value()
        previous_rows = stored_rows if isinstance(stored_rows, list) else []
        by_account = {
            row["account"]: row
            for row in previous_rows
            if isinstance(row, dict)
            and isinstance(row.get("account"), str)
            and row["account"]
        }
        data = []
        for index, row in enumerate(self.model):
            row_info = {
                column["id"]: row[column_index]
                for column_index, column in enumerate(self.columns)
            }
            previous = by_account.get(row_info.get("account"))
            if previous is None and index < len(previous_rows):
                previous = previous_rows[index]
            if isinstance(previous, dict):
                for key, value in previous.items():
                    row_info.setdefault(key, value)
            data.append(row_info)
        self.set_value(data)
        self.update_button_sensitivity()

    def _rebuild_tree(
        self,
        columns: list[dict[str, object]],
        rows: list[dict[str, object]],
    ) -> None:
        children = self.get_children()
        if not children:
            return
        scrollbox = children[0]
        old_content_widget = self.content_widget
        scrollbox.remove(old_content_widget)
        old_content_widget.destroy()
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
            try:
                self.model.append(values)
            except (OverflowError, TypeError, ValueError):
                continue
        self.content_widget.get_selection().connect("changed", self.update_button_sensitivity)
        self.content_widget.set_activate_on_single_click(False)
        self.content_widget.connect("row-activated", self.on_row_activated)
        self.content_widget.columns_autosize()
        self.show_all()

    def destroy(self):
        self.detach()
        return super().destroy()
