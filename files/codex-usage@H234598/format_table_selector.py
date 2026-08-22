#!/usr/bin/env python3
"""Show one formatting table at a time behind a centered selector."""

from __future__ import annotations

import copy

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402
from JsonSettingsWidgets import JSONSettingsBackend, SettingsWidget  # noqa: E402
from TreeListWidgets import List  # noqa: E402


class _BoundFormatList(List, JSONSettingsBackend):
    """Bind one existing list schema to its JSON setting key."""

    def __init__(self, key, definition, settings):
        self.backend = "json"
        self.key = key
        self.settings = settings
        copy_from = definition.get("format-copy-of")
        if key == "account-delta-styles" or isinstance(copy_from, str):
            base_key = "account-percent-styles" if key == "account-delta-styles" else copy_from
            base = settings.settings.get(base_key, {})
            overrides = {
                name: value for name, value in definition.items()
                if name not in ("columns", "format-copy-of")
            }
            definition = copy.deepcopy(base)
            definition.update(overrides)
            if key == "account-delta-styles":
                definition["description"] = "Tokendelta"
                definition["columns"].append({
                    "id": "dynamic",
                    "title": "Dynamisch",
                    "type": "boolean",
                    "default": False,
                    "tooltip": (
                        "Markiert, wenn hochgerechneter Verbrauch bis zum Reset "
                        "das verbleibende Limit erreicht."
                    ),
                })
        super().__init__(
            label=definition.get("description"),
            columns=definition.get("columns", []),
            height=definition.get("height", 300),
            show_buttons=definition.get("show-buttons", True),
            hidden_buttons=definition.get("hidden-buttons", []),
            tooltip=definition.get("tooltip", ""),
        )
        self.attach()

    def on_setting_changed(self, *_args):
        """Load only row objects; malformed persisted values render empty."""
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
            except (TypeError, ValueError):
                continue
        self.content_widget.columns_autosize()

    def detach(self):
        listeners = getattr(self.settings, "listeners", None)
        if not isinstance(listeners, dict):
            return
        callbacks = listeners.get(self.key)
        if not isinstance(callbacks, list):
            return
        listener = self._settings_changed_callback
        callbacks[:] = [callback for callback in callbacks if callback != listener]


class FormatTableSelector(SettingsWidget, JSONSettingsBackend):
    """Render formatting tables exclusively, selected by a centered dropdown."""

    bind_dir = None

    def __init__(self, info, key, settings):
        self.backend = "json"
        self.key = key
        self.settings = settings
        self._table_labels = {}
        self._table_definitions = {}
        self._tables = {}
        self._active_table_key = None
        self._saving = False

        SettingsWidget.__init__(self)
        self.set_orientation(Gtk.Orientation.VERTICAL)
        self.set_spacing(10)
        self.set_border_width(0)
        self.set_margin_left(0)
        self.set_margin_right(0)

        selector_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        selector_row.set_halign(Gtk.Align.CENTER)
        selector_label = Gtk.Label(label="Tabelle")
        selector_row.pack_start(selector_label, False, False, 0)
        self.combo = Gtk.ComboBoxText()
        self.combo.set_size_request(300, -1)
        self.combo.connect("changed", self._on_table_changed)
        self.content_widget = self.combo
        selector_row.pack_start(self.combo, False, False, 0)
        self.pack_start(selector_row, False, False, 0)

        self.table_title = Gtk.Label()
        self.table_title.set_xalign(0.0)
        self.pack_start(self.table_title, False, False, 0)

        self.table_stack = Gtk.Stack()
        self.table_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.table_stack.set_vexpand(True)
        self.pack_start(self.table_stack, True, True, 0)

        definitions = settings.settings
        for table in info.get("tables", []):
            table_key = table.get("key")
            if not isinstance(table_key, str) or table_key not in definitions:
                continue
            label = table.get("label")
            if not isinstance(label, str) or not label:
                label = table_key
            if table_key in self._table_labels:
                continue
            self._table_labels[table_key] = label
            self._table_definitions[table_key] = definitions[table_key]
            self.combo.append(table_key, label)

        self.attach()

    def _on_table_changed(self, *_args):
        table_key = self.combo.get_active_id()
        if table_key not in self._table_labels:
            return
        self._show_table(table_key)
        if not self._saving:
            self.set_value(table_key)

    def _ensure_table(self, table_key):
        widget = self._tables.get(table_key)
        if widget is not None:
            return widget
        definition = getattr(self, "_table_definitions", {}).get(table_key)
        if definition is None:
            return None
        widget = _BoundFormatList(table_key, definition, self.settings)
        self._tables[table_key] = widget
        self.table_stack.add_named(widget, table_key)
        widget.show_all()
        return widget

    def _discard_table(self, table_key):
        widget = self._tables.pop(table_key, None)
        if widget is None:
            return
        widget.detach()
        self.table_stack.remove(widget)
        widget.destroy()

    def _detach_selector_listener(self):
        listeners = getattr(self.settings, "listeners", None)
        if not isinstance(listeners, dict):
            return
        callbacks = listeners.get(self.key)
        if not isinstance(callbacks, list):
            return
        callback = self._settings_changed_callback
        callbacks[:] = [registered for registered in callbacks if registered != callback]

    def _show_table(self, table_key):
        if self._ensure_table(table_key) is None:
            return
        active_table_key = getattr(self, "_active_table_key", None)
        if active_table_key != table_key:
            self._discard_table(active_table_key)
            self._active_table_key = table_key
        self.table_stack.set_visible_child_name(table_key)
        self.table_title.set_markup(f"<b>{self._table_labels[table_key]}</b>")

    def on_setting_changed(self, *_args):
        table_key = self.get_value()
        if table_key not in self._table_labels:
            table_key = next(iter(self._table_labels), None)
        if table_key is None:
            return
        self._saving = True
        try:
            self.combo.set_active_id(table_key)
            self._show_table(table_key)
        finally:
            self._saving = False

    def connect_widget_handlers(self, *_args):
        pass

    def destroy(self):
        self._detach_selector_listener()
        for table_key in list(self._tables):
            self._discard_table(table_key)
        return super().destroy()
