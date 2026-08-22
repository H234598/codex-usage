#!/usr/bin/env python3
"""Fast-mode icon selector with a preview and per-icon tooltip."""

from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "3.0")
from gi.repository import GdkPixbuf, GLib, Gtk  # noqa: E402
from JsonSettingsWidgets import JSONSettingsBackend, SettingsWidget  # noqa: E402


def _load_icon(path):
    try:
        if path.is_file() and path.suffix == ".svg":
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(path), FastModeIconSelector._ICON_SIZE, FastModeIconSelector._ICON_SIZE, True
            )
    except (GLib.Error, OSError, TypeError, ValueError):
        pass
    return None


class FastModeIconSelector(SettingsWidget, JSONSettingsBackend):
    """Show the actual SVG in the selector and describe it on hover."""

    bind_dir = None
    _MAX_ICONS = 32
    _ICON_SIZE = 24

    def __init__(self, info, key, settings):
        self.backend = "json"
        self.key = key
        self.settings = settings
        self._values = []
        self._tooltips = {}
        SettingsWidget.__init__(self)
        self.set_spacing(8)
        self.set_border_width(5)
        self.combo = Gtk.ComboBox()
        self.store = Gtk.ListStore(GdkPixbuf.Pixbuf, str, str)
        self.combo.set_model(self.store)
        self.content_widget = self.combo
        image_renderer = Gtk.CellRendererPixbuf()
        text_renderer = Gtk.CellRendererText()
        self.combo.pack_start(image_renderer, False)
        self.combo.add_attribute(image_renderer, "pixbuf", 0)
        self.combo.pack_start(text_renderer, True)
        self.combo.add_attribute(text_renderer, "text", 1)
        self.combo.set_has_tooltip(True)
        self.combo.connect("changed", self._on_changed)
        self.combo.connect("query-tooltip", self._on_query_tooltip)
        self.pack_start(self.combo, False, False, 0)

        options = info.get("options", {})
        if not isinstance(options, dict):
            options = {}
        icon_dir = Path(__file__).resolve().parent / "icons"
        for label, value in options.items():
            if len(self._values) >= self._MAX_ICONS:
                break
            if (
                not isinstance(label, str)
                or not isinstance(value, str)
                or "\x00" in label
                or "\x00" in value
                or Path(value).name != value
                or not value.endswith(".svg")
            ):
                continue
            try:
                label.encode("utf-8")
                value.encode("utf-8")
            except UnicodeEncodeError:
                continue
            path = icon_dir / value
            pixbuf = _load_icon(path)
            self.store.append([pixbuf, label, value])
            self._values.append(value)
            self._tooltips[value] = f"{label}: {value}"
        try:
            self.attach()
        except Exception:
            self._detach_settings_listener()
            try:
                self.on_setting_changed()
            except Exception:
                pass

    def _on_changed(self, *_args):
        value = self.get_widget_value()
        self.combo.set_tooltip_text(self._tooltips.get(value, value or ""))
        if not getattr(self, "_saving", False):
            try:
                self.set_value(value)
            except Exception:
                self._saving = False

    def _on_query_tooltip(self, _widget, _x, _y, _keyboard, tooltip):
        value = self.get_widget_value()
        text = self._tooltips.get(value)
        if not text:
            return False
        tooltip.set_text(text)
        return True

    def on_setting_changed(self, *_args):
        saving = getattr(self, "_saving", False)
        self._saving = True
        try:
            try:
                value = self.get_value()
            except Exception:
                value = ""
            self.set_widget_value(value)
        finally:
            self._saving = saving

    def connect_widget_handlers(self, *_args):
        pass

    def _detach_settings_listener(self):
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

    def set_widget_value(self, value):
        if value not in self._values:
            value = self._values[0] if self._values else ""
        index = self._values.index(value) if value in self._values else -1
        self.combo.set_active(index)
        self.combo.set_tooltip_text(self._tooltips.get(value, value or ""))

    def get_widget_value(self):
        iterator = self.combo.get_active_iter()
        if iterator is None:
            return ""
        return self.store.get_value(iterator, 2)

    def destroy(self):
        self._detach_settings_listener()
        return super().destroy()
