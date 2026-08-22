from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
APPLET_DIR = ROOT / "files" / "codex-usage@H234598"
sys.path.insert(0, str(APPLET_DIR))
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings")
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings/bin")

from help_page import (  # noqa: E402
    Gtk,
    HelpPage,
    _clean_text,
    _definition_entry,
    _field_text,
    _help_definition,
    _iter_table_keys,
    _markup,
    _option_text,
    build_help_groups,
)


def _schema() -> dict:
    return json.loads((APPLET_DIR / "settings-schema.json").read_text(encoding="utf-8"))


def test_help_text_helpers_preserve_detail_and_escape_markup() -> None:
    assert _clean_text("  one\n two ") == "one two"
    assert _clean_text(None) == ""
    assert _option_text({"Ja": True, "Nein": False}) == "Auswahl: Ja = True; Nein = False"
    assert _option_text(None) == ""
    column = {
        "id": "threshold",
        "title": "Schwelle",
        "type": "integer",
        "default": 20,
        "min": 0,
        "max": 100,
    }
    text = _field_text(column)
    assert "Schwelle als verbleibender Prozentwert" in text
    assert "Standard: 20" in text
    assert "Grenzen: 0 bis 100" in text
    assert "Wertquelle für dieses Leistenfeld" in _field_text({"id": "slot23"})
    combined = _field_text({
        "id": "custom-format",
        "description": "Kurzbeschreibung",
        "tooltip": "Zusätzliche Platzhalterhilfe",
    })
    assert "Kurzbeschreibung" in combined
    assert "Zusätzliche Platzhalterhilfe" in combined
    assert _markup("<tag>\n&") == "&lt;tag&gt;&#10;&amp;"
    assert _markup(0) == "0"
    assert _markup(False) == "False"


def test_help_group_builder_covers_gui_pages_and_format_copies() -> None:
    schema = _schema()
    groups = build_help_groups(schema)
    titles = {group["title"] for group in groups}
    assert "Hilfe" not in titles
    assert {"Einstellungen", "Formatierungen", "Accounts"}.issubset(titles)
    settings_group = next(group for group in groups if group["title"] == "Einstellungen")
    settings_entries = [
        entry for section in settings_group["sections"] for entry in section["entries"]
    ]
    assert "Spalten im Leisten-Editor" in {entry["title"] for entry in settings_entries}

    format_group = next(group for group in groups if group["title"] == "Formatierungen")
    entries = [entry for section in format_group["sections"] for entry in section["entries"]]
    entry_titles = {entry["title"] for entry in entries}
    assert "Leiste — Kürzel" in entry_titles
    tag_entry = next(entry for entry in entries if entry["title"] == "Leiste — Kürzel")
    assert any(field["title"] == "Account-ID" for field in tag_entry["fields"])
    forecast_group = next(group for group in groups if group["title"] == "Prognosen")
    forecast_entries = [
        entry for section in forecast_group["sections"] for entry in section["entries"]
    ]
    assert {entry["title"] for entry in forecast_entries} >= {
        "Prognosentabelle",
        "Limitverbrauch pro Account und Zeitraum",
        "Tokenende je Account",
        "Creditverbrauch je Account",
    }


def test_help_materializes_tokendelta_inherited_format_fields() -> None:
    schema = _schema()
    groups = build_help_groups(schema)
    entries = [
        entry
        for group in groups
        for section in group["sections"]
        for entry in section["entries"]
        if entry["title"] == "Tokendelta"
    ]

    assert len(entries) == 1
    field_titles = {field["title"] for field in entries[0]["fields"]}
    assert "Formatierungsmodus" in field_titles
    assert "Dynamisch" in field_titles


def test_help_definition_and_table_key_helpers_handle_malformed_input() -> None:
    assert tuple(_iter_table_keys({"tables": [{"key": "a"}, {}, {"key": 2}]})) == ("a",)
    assert tuple(_iter_table_keys({})) == ()
    entry = _definition_entry("x", {"columns": [{"id": "unknown", "title": "X"}]})
    assert entry["title"] == "x"
    assert entry["fields"][0]["title"] == "X"
    resolved = _help_definition(
        {"format-copy-of": "base", "description": "copy"},
        {"base": {"columns": [{"id": "account"}]}},
    )
    assert resolved["columns"][0]["id"] == "account"
    malformed_base = _help_definition(
        {
            "format-copy-of": "base",
            "description": "copy",
            "columns": [{"id": "override"}],
        },
        {"base": {"columns": [{"id": []}]}},
        "account-delta-styles",
    )
    assert malformed_base["columns"] == [{"id": []}, {"id": "override"}]


@pytest.mark.parametrize(
    "schema",
    [
        {"layout": {"pages": [[]]}},
        {
            "layout": {
                "pages": ["page"],
                "page": {"sections": [[]]},
            },
        },
        {
            "layout": {
                "pages": ["page"],
                "page": {"sections": ["section"]},
                "section": {"keys": [[]]},
            },
        },
    ],
)
def test_help_group_builder_ignores_unhashable_layout_entries(schema) -> None:
    assert build_help_groups(schema) == []


def test_help_page_builds_scrollable_widget_from_schema() -> None:
    widget = HelpPage({}, "help-content", SimpleNamespace(settings=_schema()))
    try:
        assert widget.content_widget is not None
        widget.on_setting_changed()
        widget.connect_widget_handlers()
    finally:
        widget.destroy()


def _widget_count(widget) -> int:
    count = 1
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            count += _widget_count(child)
    return count


def _expanders(widget):
    if isinstance(widget, Gtk.Expander):
        yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from _expanders(child)


def test_help_page_defers_field_widgets_until_entry_expands() -> None:
    widget = HelpPage({}, "help-content", SimpleNamespace(settings=_schema()))
    try:
        expanders = list(_expanders(widget))
        assert len(expanders) == 55
        initial_count = _widget_count(widget)
        assert initial_count < 300
        assert all(expander.get_child() is None for expander in expanders)

        target = next(
            expander for expander in expanders
            if expander.get_label() == "Leiste — Kürzel"
        )
        target.set_expanded(True)
        assert target.get_child() is not None
        expanded_count = _widget_count(widget)
        assert expanded_count > initial_count

        target.set_expanded(False)
        assert target.get_child() is None
        assert _widget_count(widget) == initial_count
    finally:
        widget.destroy()
