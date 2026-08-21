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


class FormatTableSelector(SettingsWidget, JSONSettingsBackend):
    """Render formatting tables exclusively, selected by a centered dropdown."""

    bind_dir = None

    def __init__(self, info, key, settings):
        self.backend = "json"
        self.key = key
        self.settings = settings
        self._table_labels = {}
        self._tables = {}
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
            self._table_labels[table_key] = label
            self.combo.append(table_key, label)
            widget = _BoundFormatList(table_key, definitions[table_key], settings)
            self._tables[table_key] = widget
            self.table_stack.add_named(widget, table_key)

        self.attach()

    def _on_table_changed(self, *_args):
        table_key = self.combo.get_active_id()
        if table_key not in self._tables:
            return
        self._show_table(table_key)
        if not self._saving:
            self.set_value(table_key)

    def _show_table(self, table_key):
        self.table_stack.set_visible_child_name(table_key)
        self.table_title.set_markup(f"<b>{self._table_labels[table_key]}</b>")

    def on_setting_changed(self, *_args):
        table_key = self.get_value()
        if table_key not in self._tables:
            table_key = next(iter(self._tables), None)
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
