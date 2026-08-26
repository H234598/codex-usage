#!/usr/bin/env python3
"""Render one readable, schema-driven help page for every GUI field."""

from __future__ import annotations

import html
import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
# Cinnamon loads custom widgets by file path. Load sibling explicitly so a
# same-named module from another Xlet cannot satisfy the import.
_APPLET_DIR = str(Path(__file__).resolve().parent)

_FORMAT_MODULE_NAME = "_codex_usage_format_table_selector"
_format_module = sys.modules.get(_FORMAT_MODULE_NAME)
if _format_module is None:
    _format_spec = importlib.util.spec_from_file_location(
        _FORMAT_MODULE_NAME,
        Path(_APPLET_DIR) / "format_table_selector.py",
    )
    if _format_spec is None or _format_spec.loader is None:
        raise ImportError("format_table_selector loader unavailable")
    _format_module = importlib.util.module_from_spec(_format_spec)
    sys.modules[_FORMAT_MODULE_NAME] = _format_module
    try:
        _format_spec.loader.exec_module(_format_module)
    except BaseException:
        sys.modules.pop(_FORMAT_MODULE_NAME, None)
        raise
_materialize_format_definition = _format_module._materialize_format_definition
from gi.repository import Gtk  # noqa: E402
from JsonSettingsWidgets import SettingsWidget  # noqa: E402

_DELTA_STYLE_KEYS = frozenset({
    "account-delta-styles",
    "account-credit-delta-styles",
})

_COLUMN_GUIDANCE = {
    "mode": (
        "Formatierungsmodus: Immer nutzt das obere Format; Nur unter Schwelle nutzt nur "
        "das untere Format; Immer, unter Schwelle anders wechselt abhängig vom "
        "Schwellenwert; Aus lässt Formatierung aus."
    ),
    "font": "Schriftfamilie oberhalb der Schwelle. Theme übernimmt die Cinnamon-Schrift.",
    "size": "Schriftgröße oberhalb der Schwelle in Punkten. 0 übernimmt das Theme.",
    "bold": "Macht den Wert oberhalb der Schwelle fett.",
    "italic": "Macht den Wert oberhalb der Schwelle kursiv.",
    "color": "Schriftfarbe oberhalb der Schwelle. Theme übernimmt die Standardfarbe.",
    "background": (
        "Hintergrund oberhalb der Schwelle. Transparent lässt den Theme-Hintergrund "
        "unverändert."
    ),
    "hover-background": (
        "Hintergrund oberhalb der Schwelle, wenn der Wert mit der Maus überfahren wird."
    ),
    "threshold": (
        "Schwelle als verbleibender Prozentwert. Unterhalb dieser Grenze greifen die "
        "Unter-Schwelle-Felder."
    ),
    "below-font": "Schriftfamilie unterhalb der Schwelle.",
    "below-size": "Schriftgröße unterhalb der Schwelle in Punkten. 0 übernimmt das Theme.",
    "below-bold": "Macht den Wert unterhalb der Schwelle fett.",
    "below-italic": "Macht den Wert unterhalb der Schwelle kursiv.",
    "below-color": "Schriftfarbe unterhalb der Schwelle.",
    "below-background": "Hintergrund unterhalb der Schwelle.",
    "below-hover-background": "Hover-Hintergrund unterhalb der Schwelle.",
    "account": (
        "Account-ID, auf die diese Zeile angewendet wird. Die Zeile wird beim nächsten "
        "Account-Sync erhalten oder ergänzt."
    ),
    "element": "Element, dessen Anzeigeziel konfiguriert wird.",
    "panel": "Zeigt das Element in der Leiste an.",
    "hover": "Zeigt das Element im Hover-Hilfetext an.",
    "click": "Zeigt das Element im Klick-Menü an.",
    "show-hover": (
        "Zeigt diesen formatierten Wert im Hover-Menü an. Das steuert nur die "
        "Sichtbarkeit; Datenabruf und Berechnung bleiben unverändert."
    ),
    "show-click": (
        "Zeigt diesen formatierten Wert im Klick-Menü an. Das steuert nur die "
        "Sichtbarkeit; Datenabruf und Berechnung bleiben unverändert."
    ),
    "label": (
        "Lesbarer Name des Accounts. Er wird in Leiste, Hover und Menü verwendet, wenn "
        "das jeweilige Anzeigeziel aktiv ist."
    ),
    "auth-json": (
        "Lokale Authentifizierungsdatei dieses Accounts. Der Pfad bleibt lokal und wird "
        "nicht als Shell-Befehl ausgeführt."
    ),
    "profile-dir": (
        "Lokaler Profilordner für diesen Account. Dort liegen Codex-Zustand und isolierte "
        "Browser-/Login-Daten."
    ),
    "browser": "Browser, der für die kontospezifische Reaktivierung verwendet wird.",
    "backend": (
        "Abrufweg für Nutzungsdaten. Direktabruf und Codex App Server haben getrennte "
        "Provenienz."
    ),
    "format": (
        "Darstellungsformat des Werts. Benutzerdefinierte Formate verwenden die im "
        "zugehörigen Feld genannten Platzhalter."
    ),
    "order": "Sortierposition des Accounts in der Leiste. Kleinere Zahlen erscheinen zuerst.",
    "muted": "Unterdrückt den Account in der Leiste, ohne Konfiguration oder Abruf zu löschen.",
    "slot1": (
        "Wertquelle für dieses Leistenfeld. Weitere slotN-Felder werden aus Anzahl der "
        "Wertfelder erzeugt; Quellen dürfen nicht doppelt erscheinen."
    ),
    "show-tooltip": (
        "Zeigt den Wert im Hover-Hilfetext an. Dies steuert nur Sichtbarkeit, "
        "nicht Datenabruf."
    ),
    "amount": "Menge des Rückblicks für Verbrauchs- oder Prognoseberechnung.",
    "unit": "Zeiteinheit der Rückblickmenge, zum Beispiel Minuten, Stunden, Tage oder Wochen.",
    "limit-window": (
        "Limitfenster, gegen das Verbrauch oder Prognose gerechnet wird: 5h, Woche, "
        "30 Tage oder Spark."
    ),
    "hide-when-zero": (
        "Blendet fehlende oder unbekannte Werte mit Platzhalter — aus. Ein echter "
        "numerischer 0-Wert bleibt sichtbar; Berechnung und Datenabruf ändern sich nicht."
    ),
    "show-coverage-marker": (
        "Zeigt an, ob Messhistorie vollständig, untere Schranke, veraltet oder "
        "unzureichend ist."
    ),
    "warn-amount": "Menge der Restzeit, unterhalb derer die Prognosewarnung aktiv wird.",
    "warn-unit": "Zeiteinheit der Prognosewarnschwelle.",
    "warnings": "Erlaubt Warnbenachrichtigungen für diesen Account.",
    "errors": "Erlaubt Fehlerbenachrichtigungen für diesen Account.",
    "hover-separator": "Fügt vor diesem Account einen Abstandshalter im Hover-Hilfetext ein.",
    "click-separator": "Fügt vor diesem Account einen Abstandshalter im Klick-Menü ein.",
    "scope": (
        "Ebene der Creditregel. Höhere Spezifität überschreibt globale und weniger "
        "spezifische Regeln."
    ),
    "identifier": "Kennung der gewählten Regel-Ebene. Sie wird nicht normalisiert.",
    "enabled": "Aktiviert diese Routing- oder Creditregel.",
    "allow": "Erlaubt oder verweigert Nutzung innerhalb dieser Regel.",
}


_INTRO = (
    "Codex Usage liest Nutzungs-, Reset-, Credit- und Statusdaten aus den konfigurierten "
    "Accounts. Diese Seite sammelt die Beschreibungen und Tooltips aller anderen GUI-Seiten "
    "automatisch aus dem aktuellen Settings-Schema. Öffne einen Eintrag, um Zweck, Wirkung, "
    "Optionen, Standardwerte und Grenzen zu sehen. Änderungen werden erst wirksam, wenn das "
    "betroffene Feld gespeichert ist; ein Reload des Applets liest die gespeicherten Werte neu.\n\n"
    "Formatierung ist Darstellung, keine Berechnung: Farben, Schrift und Hintergrund ändern "
    "keine Limits, Abrufwege, Routingentscheidungen oder Accountdaten. Im Leisten-Editor "
    "kopierst du Wert 1 bis N eines ausgewählten Accounts und fügst sie in einen anderen ein; "
    "Account-ID, Reihenfolge und Stumm bleiben dabei unverändert."
)


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()


def _option_text(options: object) -> str:
    if isinstance(options, dict):
        values = [
            f"{label} = {value}"
            for label, value in options.items()
            if isinstance(label, str)
        ]
    elif isinstance(options, (list, tuple)):
        values = [str(value) for value in options]
    else:
        return ""
    return "Auswahl: " + "; ".join(values) if values else ""


def _field_text(column: dict[str, object]) -> str:
    description = _clean_text(column.get("description"))
    tooltip = _clean_text(column.get("tooltip"))
    parts = [description] if description else []
    if tooltip and tooltip != description:
        parts.append(tooltip)
    text = "\n\n".join(parts)
    if not text and str(column.get("id", "")).startswith("slot"):
        text = _COLUMN_GUIDANCE["slot1"]
    if not text:
        text = _COLUMN_GUIDANCE.get(
            str(column.get("id", "")),
            "Dieses Feld steuert die Anzeige des genannten Werts; es ändert keine Quelldaten.",
        )
    details = []
    options = _option_text(column.get("options"))
    if options:
        details.append(options)
    if "default" in column:
        details.append(f"Standard: {column['default']}")
    if "min" in column or "max" in column:
        details.append(f"Grenzen: {column.get('min', '—')} bis {column.get('max', '—')}")
    if "step" in column:
        details.append(f"Schrittweite: {column['step']}")
    if "units" in column:
        details.append(f"Einheit: {column['units']}")
    if column.get("select-dir") is True:
        details.append("Ordnerauswahl: aktiviert")
    if column.get("expand-width") is True:
        details.append("Breite: horizontal ausdehnen")
    return "\n".join([text, *details])


def _definition_entry(key: str, definition: dict[str, object]) -> dict[str, object]:
    title = _clean_text(definition.get("description")) or key
    text_parts = []
    description = _clean_text(definition.get("description"))
    tooltip = _clean_text(definition.get("tooltip"))
    if description:
        text_parts.append(description)
    if tooltip and tooltip != description:
        text_parts.append(tooltip)
    if not text_parts:
        text_parts.append("Keine zusätzliche Beschreibung im Schema hinterlegt.")
    details = []
    options = _option_text(definition.get("options"))
    if options:
        details.append(options)
    if "default" in definition:
        details.append(f"Standard: {definition['default']}")
    if "min" in definition or "max" in definition:
        details.append(
            f"Grenzen: {definition.get('min', '—')} bis {definition.get('max', '—')}"
        )
    if "step" in definition:
        details.append(f"Schrittweite: {definition['step']}")
    if "units" in definition:
        details.append(f"Einheit: {definition['units']}")
    if details:
        text_parts.append("\n".join(details))
    fields = []
    columns = definition.get("columns")
    if isinstance(columns, list):
        for column in columns:
            if not isinstance(column, dict):
                continue
            column_id = str(column.get("id", "feld"))
            column_title = _clean_text(column.get("title")) or column_id
            fields.append({"title": column_title, "text": _field_text(column)})
    return {"title": title, "text": "\n\n".join(text_parts), "fields": fields}


def _help_definition(
    definition: dict[str, object], schema: dict[str, object], key: str | None = None
) -> dict[str, object]:
    """Materialize copied table columns for the read-only help view."""
    result = dict(definition)
    copy_from = definition.get("format-copy-of")
    if key in _DELTA_STYLE_KEYS and copy_from is None:
        copy_from = "account-percent-styles"
    base = schema.get(copy_from) if isinstance(copy_from, str) else None
    base_columns = base.get("columns") if isinstance(base, dict) else None
    if isinstance(base_columns, list):
        if key in _DELTA_STYLE_KEYS:
            own_columns = result.get("columns")
            if not isinstance(own_columns, list):
                own_columns = []
            overrides = {
                column.get("id"): column
                for column in own_columns
                if isinstance(column, dict) and isinstance(column.get("id"), str)
            }
            columns = []
            for column in base_columns:
                column_id = column.get("id") if isinstance(column, dict) else None
                if not isinstance(column_id, str):
                    columns.append(column)
                    continue
                columns.append(overrides.pop(column_id, column))
            columns.extend(overrides.values())
            result["columns"] = columns
        elif isinstance(copy_from, str):
            result["columns"] = base_columns
    if key in _DELTA_STYLE_KEYS:
        columns = result.get("columns")
        columns = list(columns) if isinstance(columns, list) else []
        for index, column in enumerate(columns):
            if isinstance(column, dict) and column.get("id") == "threshold":
                updated = dict(column)
                updated["title"] = "Schwelle Verbrauch %"
                updated["tooltip"] = (
                    "Formatierung wird ab diesem verbrauchten Prozentwert "
                    "aktiv (Verbrauch >= Schwelle)."
                )
                columns[index] = updated
        if not any(
            isinstance(column, dict) and column.get("id") == "dynamic"
            for column in columns
        ):
            columns.append({
                "id": "dynamic",
                "title": "Dynamisch",
                "type": "boolean",
                "default": False,
            })
        result["columns"] = columns
    return result


def _iter_table_keys(definition: dict[str, object]) -> Iterable[str]:
    tables = definition.get("tables")
    if not isinstance(tables, list):
        return ()
    return tuple(
        table.get("key")
        for table in tables
        if isinstance(table, dict) and isinstance(table.get("key"), str)
    )


def build_help_groups(schema: object) -> list[dict[str, object]]:
    """Build page/section/field help entries from a settings schema."""
    if not isinstance(schema, dict):
        return []
    layout = schema.get("layout")
    if not isinstance(layout, dict):
        return []
    pages = layout.get("pages")
    if not isinstance(pages, list):
        return []
    seen: set[str] = set()
    groups = []
    for page_key in pages:
        if not isinstance(page_key, str) or page_key == "help-page":
            continue
        page = layout.get(page_key)
        if not isinstance(page, dict):
            continue
        sections = []
        section_keys = page.get("sections")
        if not isinstance(section_keys, list):
            continue
        for section_key in section_keys:
            if not isinstance(section_key, str):
                continue
            section = layout.get(section_key)
            if not isinstance(section, dict):
                continue
            entries = []
            keys = section.get("keys")
            if not isinstance(keys, list):
                continue
            for key in keys:
                if not isinstance(key, str) or key in seen:
                    continue
                definition = schema.get(key)
                if not isinstance(definition, dict):
                    continue
                seen.add(key)
                entries.append(_definition_entry(key, _help_definition(definition, schema, key)))
                for table_key in _iter_table_keys(definition):
                    if table_key in seen:
                        continue
                    table = schema.get(table_key)
                    if not isinstance(table, dict):
                        continue
                    seen.add(table_key)
                    entries.append(
                        _definition_entry(table_key, _help_definition(table, schema, table_key))
                    )
            if entries:
                sections.append({
                    "title": _clean_text(section.get("title")) or section_key,
                    "entries": entries,
                })
        if sections:
            groups.append({
                "title": _clean_text(page.get("title")) or page_key,
                "sections": sections,
            })
    return groups


def _markup(text: object) -> str:
    value = "" if text is None else str(text)
    value = value.replace("\x00", "?")
    value = value.encode("utf-8", "replace").decode("utf-8")
    return html.escape(value, quote=True).replace("\n", "&#10;")


class HelpPage(SettingsWidget):
    """Show schema-derived help in a scrollable, expandable layout."""

    bind_dir = None

    def __init__(self, info, key, settings):
        del key
        SettingsWidget.__init__(self)
        self.set_orientation(Gtk.Orientation.VERTICAL)
        self.set_spacing(10)
        self.set_border_width(0)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_size_request(720, 560)

        self.content_widget = Gtk.ScrolledWindow()
        self.content_widget.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.content_widget.set_hexpand(True)
        self.content_widget.set_vexpand(True)
        self.content_widget.set_min_content_width(640)
        self.content_widget.set_min_content_height(480)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.set_hexpand(True)
        body.set_vexpand(True)
        body.set_margin_top(8)
        body.set_margin_bottom(8)
        body.set_margin_left(8)
        body.set_margin_right(8)
        self.content_widget.add(body)
        self.pack_start(self.content_widget, True, True, 0)

        self._add_card(body, "Willkommen", _INTRO)
        schema = getattr(settings, "settings", {})
        for group in build_help_groups(schema):
            group_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            group_box.set_margin_top(4)
            title = Gtk.Label()
            title.set_xalign(0.0)
            title.set_markup("<b>" + _markup(group["title"]) + "</b>")
            group_box.pack_start(title, False, False, 0)
            for section in group["sections"]:
                section_title = Gtk.Label()
                section_title.set_xalign(0.0)
                section_title.set_markup("<b>" + _markup(section["title"]) + "</b>")
                group_box.pack_start(section_title, False, False, 2)
                for entry in section["entries"]:
                    self._add_entry(group_box, entry)
            body.pack_start(group_box, False, False, 0)
        self.show_all()

    def _add_card(self, parent, title_text: str, body_text: str) -> None:
        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        content.set_margin_left(10)
        content.set_margin_right(10)
        heading = Gtk.Label()
        heading.set_xalign(0.0)
        heading.set_hexpand(True)
        heading.set_markup("<b>" + _markup(title_text) + "</b>")
        content.pack_start(heading, False, False, 0)
        text = Gtk.Label()
        text.set_xalign(0.0)
        text.set_hexpand(True)
        text.set_line_wrap(True)
        text.set_selectable(True)
        text.set_markup(_markup(body_text))
        content.pack_start(text, False, False, 0)
        frame.add(content)
        parent.pack_start(frame, False, False, 0)

    def _add_entry(self, parent, entry: dict[str, object]) -> None:
        title = str(entry.get("title", "Feld"))
        title = title.replace("\x00", "?")
        title = title.encode("utf-8", "replace").decode("utf-8")
        expander = Gtk.Expander(label=title)
        expander.set_hexpand(True)
        expander.connect("notify::expanded", self._on_entry_expanded, entry)
        parent.pack_start(expander, False, False, 0)

    def _entry_content(self, entry: dict[str, object]):
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        content.set_margin_top(6)
        content.set_margin_bottom(6)
        content.set_margin_left(10)
        content.set_margin_right(10)
        body = Gtk.Label()
        body.set_xalign(0.0)
        body.set_hexpand(True)
        body.set_line_wrap(True)
        body.set_selectable(True)
        body.set_markup(_markup(entry.get("text", "")))
        content.pack_start(body, False, False, 0)
        for field in entry.get("fields", []):
            field_title = Gtk.Label()
            field_title.set_xalign(0.0)
            field_title.set_hexpand(True)
            field_title.set_markup("<b>" + _markup(field.get("title", "Feld")) + "</b>")
            content.pack_start(field_title, False, False, 2)
            field_body = Gtk.Label()
            field_body.set_xalign(0.0)
            field_body.set_hexpand(True)
            field_body.set_line_wrap(True)
            field_body.set_selectable(True)
            field_body.set_markup(_markup(field.get("text", "")))
            content.pack_start(field_body, False, False, 0)
        return content

    def _on_entry_expanded(self, expander, _spec, entry: dict[str, object]) -> None:
        child = expander.get_child()
        if expander.get_expanded():
            if child is None:
                child = self._entry_content(entry)
                expander.add(child)
            child.show_all()
            return
        if child is not None:
            expander.remove(child)
            child.destroy()

    def on_setting_changed(self, *_args):
        pass

    def connect_widget_handlers(self, *_args):
        pass
