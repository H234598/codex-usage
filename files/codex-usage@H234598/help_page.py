#!/usr/bin/env python3
"""Render one readable, schema-driven help page for every GUI field."""

from __future__ import annotations

import html
from collections.abc import Iterable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402
from JsonSettingsWidgets import SettingsWidget  # noqa: E402

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
        "Blendet eine sicher berechnete Null aus. Fehlende oder unbekannte Werte werden "
        "dadurch nicht in Null umgewandelt."
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
    "keine Limits, Abrufwege, Routingentscheidungen oder Accountdaten. Eine kopierte "
    "Leistenwert-Tabelle gilt nur für ihren ausgewählten Wert und Account."
)


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()


def _option_text(options: object) -> str:
    if not isinstance(options, dict) or not options:
        return ""
    values = []
    for label, value in options.items():
        if isinstance(label, str):
            values.append(f"{label} = {value}")
    return "Auswahl: " + "; ".join(values) if values else ""


def _field_text(column: dict[str, object]) -> str:
    text = _clean_text(column.get("description")) or _clean_text(column.get("tooltip"))
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
    definition: dict[str, object], schema: dict[str, object]
) -> dict[str, object]:
    """Materialize copied table columns for the read-only help view."""
    result = dict(definition)
    copy_from = definition.get("format-copy-of")
    base = schema.get(copy_from) if isinstance(copy_from, str) else None
    if (
        "columns" not in result and
        isinstance(base, dict) and isinstance(base.get("columns"), list)
    ):
        result["columns"] = base["columns"]
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
        if page_key == "help-page":
            continue
        page = layout.get(page_key)
        if not isinstance(page, dict):
            continue
        sections = []
        for section_key in page.get("sections", []):
            section = layout.get(section_key)
            if not isinstance(section, dict):
                continue
            entries = []
            for key in section.get("keys", []):
                if not isinstance(key, str) or key in seen:
                    continue
                definition = schema.get(key)
                if not isinstance(definition, dict):
                    continue
                seen.add(key)
                entries.append(_definition_entry(key, _help_definition(definition, schema)))
                for table_key in _iter_table_keys(definition):
                    if table_key in seen:
                        continue
                    table = schema.get(table_key)
                    if not isinstance(table, dict):
                        continue
                    seen.add(table_key)
                    entries.append(_definition_entry(table_key, _help_definition(table, schema)))
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
    return html.escape(str(text or ""), quote=True).replace("\n", "&#10;")


class HelpPage(SettingsWidget):
    """Show schema-derived help in a scrollable, expandable layout."""

    bind_dir = None

    def __init__(self, info, key, settings):
        del key
        SettingsWidget.__init__(self)
        self.set_orientation(Gtk.Orientation.VERTICAL)
        self.set_spacing(10)
        self.set_border_width(0)

        self.content_widget = Gtk.ScrolledWindow()
        self.content_widget.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.content_widget.set_vexpand(True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
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
        heading.set_markup("<b>" + _markup(title_text) + "</b>")
        content.pack_start(heading, False, False, 0)
        text = Gtk.Label()
        text.set_xalign(0.0)
        text.set_line_wrap(True)
        text.set_selectable(True)
        text.set_markup(_markup(body_text))
        content.pack_start(text, False, False, 0)
        frame.add(content)
        parent.pack_start(frame, False, False, 0)

    def _add_entry(self, parent, entry: dict[str, object]) -> None:
        expander = Gtk.Expander(label=str(entry.get("title", "Feld")))
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
        body.set_line_wrap(True)
        body.set_selectable(True)
        body.set_markup(_markup(entry.get("text", "")))
        content.pack_start(body, False, False, 0)
        for field in entry.get("fields", []):
            field_title = Gtk.Label()
            field_title.set_xalign(0.0)
            field_title.set_markup("<b>" + _markup(field.get("title", "Feld")) + "</b>")
            content.pack_start(field_title, False, False, 2)
            field_body = Gtk.Label()
            field_body.set_xalign(0.0)
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
