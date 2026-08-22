#!/usr/bin/env python3
"""Show one formatting table at a time behind a centered selector."""

from __future__ import annotations

import copy
import math

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402
from JsonSettingsWidgets import JSONSettingsBackend, SettingsWidget  # noqa: E402
from TreeListWidgets import VARIABLE_TYPE_MAP, List  # noqa: E402


def _valid_text(value: object, *, allow_empty: bool = True) -> bool:
    """Return whether value is safe for GTK and GLib text APIs."""
    if not isinstance(value, str) or (not allow_empty and not value) or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class _BoundFormatList(List, JSONSettingsBackend):
    """Bind one existing list schema to its JSON setting key."""

    def __init__(self, key, definition, settings):
        self.backend = "json"
        self.key = key
        self.settings = settings
        if not isinstance(definition, dict):
            definition = {}
        else:
            definition = copy.deepcopy(definition)
        copy_from = definition.get("format-copy-of")
        if key == "account-delta-styles" or isinstance(copy_from, str):
            base_key = "account-percent-styles" if key == "account-delta-styles" else copy_from
            schema = getattr(settings, "settings", {})
            if not isinstance(schema, dict):
                schema = {}
            base = schema.get(base_key, {})
            if not isinstance(base, dict):
                base = {}
            overrides = {
                name: value for name, value in definition.items()
                if name not in ("columns", "format-copy-of")
            }
            definition = copy.deepcopy(base)
            definition.update(overrides)
            if key == "account-delta-styles":
                definition["description"] = "Tokendelta"
                base_columns = definition.get("columns", [])
                if not isinstance(base_columns, list):
                    base_columns = []
                definition["columns"] = base_columns
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
        columns = definition.get("columns", [])
        if not isinstance(columns, list):
            columns = []
        valid_columns = []
        seen_column_ids = set()
        for column in columns:
            if not (
                isinstance(column, dict)
                and _valid_text(column.get("id"))
                and _valid_text(column.get("title"))
                and isinstance(column.get("type"), str)
                and column["type"] in VARIABLE_TYPE_MAP
            ):
                continue
            if "options" in column:
                options = column["options"]
                if not isinstance(options, (dict, list, tuple)):
                    continue
                option_pairs = (
                    options.items()
                    if isinstance(options, dict)
                    else ((value, value) for value in options)
                )
                option_type = VARIABLE_TYPE_MAP[column["type"]]
                valid_options = True
                for label, value in option_pairs:
                    if not _valid_text(label):
                        valid_options = False
                        break
                    if option_type is str:
                        value_valid = _valid_text(value)
                    elif option_type is int:
                        value_valid = (
                            isinstance(value, int)
                            and not isinstance(value, bool)
                            and -(2**31) <= value <= 2**31 - 1
                        )
                    elif option_type is bool:
                        value_valid = isinstance(value, bool)
                    else:
                        try:
                            value_valid = (
                                isinstance(value, (int, float))
                                and not isinstance(value, bool)
                                and math.isfinite(value)
                            )
                        except (OverflowError, TypeError):
                            value_valid = False
                    if not value_valid:
                        valid_options = False
                        break
                if not valid_options:
                    continue
            allowed_properties = {
                "integer": {"min", "max", "step", "units"},
                "float": {"min", "max", "step", "units"},
                "string": {"expand-width"},
                "file": {"select-dir"},
                "icon": {"expand-width"},
            }.get(column["type"], set())
            if "options" in column:
                allowed_properties = set()
            for property_name in (
                "min", "max", "step", "units", "select-dir", "expand-width"
            ):
                if property_name not in allowed_properties:
                    column.pop(property_name, None)
            for boolean_property in ("select-dir", "expand-width"):
                if (
                    boolean_property in column
                    and not isinstance(column[boolean_property], bool)
                ):
                    column.pop(boolean_property, None)
            if "units" in column and not _valid_text(column["units"]):
                column.pop("units", None)
            if (
                column["type"] in {"integer", "float"}
                and not isinstance(column.get("options"), dict)
            ):
                minimum = column.get("min")
                maximum = column.get("max")
                if (
                    isinstance(minimum, bool)
                    or not isinstance(minimum, (int, float))
                    or isinstance(maximum, bool)
                    or not isinstance(maximum, (int, float))
                ):
                    continue
                try:
                    if (
                        not math.isfinite(minimum)
                        or not math.isfinite(maximum)
                        or minimum > maximum
                    ):
                        continue
                except (OverflowError, TypeError):
                    continue
                if "step" in column:
                    step = column["step"]
                    try:
                        valid_step = (
                            isinstance(step, (int, float))
                            and not isinstance(step, bool)
                            and math.isfinite(step)
                            and step > 0
                            and (column["type"] == "float" or isinstance(step, int))
                        )
                    except (OverflowError, TypeError):
                        valid_step = False
                    if not valid_step:
                        column.pop("step", None)
            if "default" in column:
                default = column["default"]
                option_values = None
                options = column.get("options")
                if isinstance(options, dict):
                    option_values = options.values()
                elif isinstance(options, (list, tuple)):
                    option_values = options
                if option_values is not None:
                    valid_default = default in option_values
                elif column["type"] in {"string", "file", "icon", "sound", "keybinding"}:
                    valid_default = _valid_text(default)
                elif column["type"] == "integer":
                    valid_default = (
                        isinstance(default, int)
                        and not isinstance(default, bool)
                        and -(2**31) <= default <= 2**31 - 1
                    )
                elif column["type"] == "float":
                    try:
                        valid_default = (
                            isinstance(default, (int, float))
                            and not isinstance(default, bool)
                            and math.isfinite(default)
                        )
                    except (OverflowError, TypeError):
                        valid_default = False
                else:
                    valid_default = isinstance(default, bool)
                if (
                    valid_default
                    and option_values is None
                    and column["type"] in {"integer", "float"}
                ):
                    minimum = column.get("min")
                    maximum = column.get("max")
                    if minimum is not None and default < minimum:
                        valid_default = False
                    if maximum is not None and default > maximum:
                        valid_default = False
                if not valid_default:
                    column.pop("default", None)
            if "align" in column:
                align = column["align"]
                try:
                    valid_align = (
                        not isinstance(align, bool)
                        and isinstance(align, (int, float))
                        and math.isfinite(align)
                        and 0 <= align <= 1
                    )
                except (OverflowError, TypeError):
                    valid_align = False
                if valid_align:
                    column["align"] = float(align)
                else:
                    column.pop("align", None)
            if column["id"] in seen_column_ids:
                continue
            seen_column_ids.add(column["id"])
            valid_columns.append(column)
        definition["columns"] = valid_columns
        description = definition.get("description")
        if description is not None and not _valid_text(description):
            description = None
        height = definition.get("height", 300)
        try:
            valid_height = (
                not isinstance(height, bool)
                and isinstance(height, (int, float))
                and math.isfinite(height)
                and height >= 0
            )
        except (OverflowError, TypeError):
            valid_height = False
        if not valid_height:
            height = 300
        else:
            height = min(int(height), 10000)
        show_buttons = definition.get("show-buttons", True)
        if not isinstance(show_buttons, bool):
            show_buttons = True
        hidden_buttons = definition.get("hidden-buttons", [])
        if not isinstance(hidden_buttons, list):
            hidden_buttons = []
        tooltip = definition.get("tooltip", "")
        if not _valid_text(tooltip):
            tooltip = ""
        super().__init__(
            label=description,
            columns=definition.get("columns", []),
            height=height,
            show_buttons=show_buttons,
            hidden_buttons=hidden_buttons,
            tooltip=tooltip,
        )
        try:
            self.attach()
        except Exception:
            self.detach()
            try:
                self.on_setting_changed()
            except Exception:
                pass

    def on_setting_changed(self, *_args):
        """Load only row objects; malformed persisted values render empty."""
        self.model.clear()
        try:
            rows = self.get_value()
        except Exception:
            rows = []
        if not isinstance(rows, list):
            rows = []
        if not self.columns:
            self.content_widget.columns_autosize()
            return
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
                options = column.get("options")
                if isinstance(options, dict):
                    allowed_values = options.values()
                elif isinstance(options, (list, tuple)):
                    allowed_values = options
                else:
                    allowed_values = None
                if allowed_values is not None and row_info[-1] not in allowed_values:
                    row_info = None
                    break
            if row_info is None:
                continue
            try:
                self.model.append(row_info)
            except (OverflowError, TypeError, ValueError):
                continue
        self.content_widget.columns_autosize()

    def detach(self):
        try:
            listeners = getattr(self.settings, "listeners", None)
            if not isinstance(listeners, dict):
                return
            callbacks = listeners.get(self.key)
            if not isinstance(callbacks, list):
                return
            listener = self._settings_changed_callback
            callbacks[:] = [callback for callback in callbacks if callback != listener]
        except Exception:
            return

    def list_changed(self, *args):
        data = []
        for row in self.model:
            row_info = {
                column["id"]: row[index]
                for index, column in enumerate(self.columns)
            }
            data.append(row_info)
        try:
            self.set_value(data)
        except Exception:
            self._saving = False
            return
        self.update_button_sensitivity()

    def remove_item(self, *args):
        model, tree_iter = self.content_widget.get_selection().get_selected()
        if tree_iter is None:
            self.update_button_sensitivity()
            return
        try:
            model.remove(tree_iter)
        except Exception:
            self.update_button_sensitivity()
            return
        self.list_changed()

    def move_item_up(self, *args):
        model, tree_iter = self.content_widget.get_selection().get_selected()
        if tree_iter is None:
            self.update_button_sensitivity()
            return
        previous = model.iter_previous(tree_iter)
        if previous is None:
            self.update_button_sensitivity()
            return
        try:
            model.swap(tree_iter, previous)
        except Exception:
            self.update_button_sensitivity()
            return
        self.list_changed()

    def move_item_down(self, *args):
        model, tree_iter = self.content_widget.get_selection().get_selected()
        if tree_iter is None:
            self.update_button_sensitivity()
            return
        following = model.iter_next(tree_iter)
        if following is None:
            self.update_button_sensitivity()
            return
        try:
            model.swap(tree_iter, following)
        except Exception:
            self.update_button_sensitivity()
            return
        self.list_changed()


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

        raw_definitions = getattr(settings, "settings", {})
        definitions = raw_definitions if isinstance(raw_definitions, dict) else {}
        tables = info.get("tables", []) if isinstance(info, dict) else []
        if not isinstance(tables, list):
            tables = []
        for table in tables:
            if not isinstance(table, dict):
                continue
            table_key = table.get("key")
            if (
                not _valid_text(table_key, allow_empty=False)
                or not isinstance(definitions.get(table_key), dict)
            ):
                continue
            label = table.get("label")
            if not _valid_text(label, allow_empty=False):
                label = table_key
            if table_key in self._table_labels:
                continue
            self._table_labels[table_key] = label
            self._table_definitions[table_key] = definitions[table_key]
            self.combo.append(table_key, label)

        try:
            self.attach()
        except Exception:
            self._detach_selector_listener()
            try:
                self.on_setting_changed()
            except Exception:
                pass

    def _on_table_changed(self, *_args):
        table_key = self.combo.get_active_id()
        if table_key not in self._table_labels:
            return
        if not self._show_table(table_key):
            return
        if not self._saving:
            try:
                self.set_value(table_key)
            except Exception:
                self._saving = False

    def _ensure_table(self, table_key):
        widget = self._tables.get(table_key)
        if widget is not None:
            return widget
        definition = getattr(self, "_table_definitions", {}).get(table_key)
        if definition is None:
            return None
        widget = None
        try:
            widget = _BoundFormatList(table_key, definition, self.settings)
            self.table_stack.add_named(widget, table_key)
            widget.show_all()
        except Exception:
            if widget is not None:
                try:
                    widget.detach()
                except Exception:
                    pass
                try:
                    self.table_stack.remove(widget)
                except Exception:
                    pass
                try:
                    widget.destroy()
                except Exception:
                    pass
            return None
        self._tables[table_key] = widget
        return widget

    def _discard_table(self, table_key):
        widget = self._tables.pop(table_key, None)
        if widget is None:
            return
        try:
            widget.detach()
        except Exception:
            pass
        try:
            self.table_stack.remove(widget)
        except Exception:
            pass
        try:
            widget.destroy()
        except Exception:
            pass

    def _detach_selector_listener(self):
        try:
            listeners = getattr(self.settings, "listeners", None)
            if not isinstance(listeners, dict):
                return
            callbacks = listeners.get(self.key)
            if not isinstance(callbacks, list):
                return
            callback = self._settings_changed_callback
            callbacks[:] = [registered for registered in callbacks if registered != callback]
        except Exception:
            return

    def _show_table(self, table_key):
        if self._ensure_table(table_key) is None:
            return False
        active_table_key = getattr(self, "_active_table_key", None)
        if active_table_key != table_key:
            self._discard_table(active_table_key)
            self._active_table_key = table_key
        self.table_stack.set_visible_child_name(table_key)
        label = GLib.markup_escape_text(self._table_labels[table_key])
        self.table_title.set_markup(f"<b>{label}</b>")
        return True

    def on_setting_changed(self, *_args):
        try:
            table_key = self.get_value()
        except Exception:
            table_key = None
        candidates = []
        if isinstance(table_key, str) and table_key in self._table_labels:
            candidates.append(table_key)
        candidates.extend(
            candidate
            for candidate in self._table_labels
            if candidate not in candidates
        )
        if not candidates:
            return
        self._saving = True
        try:
            for candidate in candidates:
                if not self._show_table(candidate):
                    continue
                self.combo.set_active_id(candidate)
                break
        finally:
            self._saving = False

    def connect_widget_handlers(self, *_args):
        pass

    def destroy(self):
        self._detach_selector_listener()
        for table_key in list(self._tables):
            self._discard_table(table_key)
        return super().destroy()
