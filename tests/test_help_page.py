from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

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


def test_help_module_does_not_depend_on_format_module() -> None:
    module_name = "_codex_usage_help_loader_probe"
    bound_name = "_codex_usage_format_table_selector"
    original_path = list(sys.path)
    original_format_module = sys.modules.get("format_table_selector")
    original_bound_module = sys.modules.pop(bound_name, None)
    collision = ModuleType("format_table_selector")
    sys.modules["format_table_selector"] = collision
    sys.path[:] = [path for path in sys.path if path != str(APPLET_DIR)]
    try:
        spec = importlib.util.spec_from_file_location(
            module_name,
            APPLET_DIR / "help_page.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module._help_definition.__module__ == module_name
        assert bound_name not in sys.modules
    finally:
        sys.path[:] = original_path
        sys.modules.pop(module_name, None)
        sys.modules.pop(bound_name, None)
        if original_bound_module is not None:
            sys.modules[bound_name] = original_bound_module
        if original_format_module is not None:
            sys.modules["format_table_selector"] = original_format_module
        else:
            sys.modules.pop("format_table_selector", None)


def test_help_text_helpers_preserve_detail_and_escape_markup() -> None:
    assert _clean_text("  one\n two ") == "one two"
    assert _clean_text(None) == ""
    assert _option_text({"Ja": True, "Nein": False}) == "Auswahl: Ja = True; Nein = False"
    assert _option_text(["A", "B"]) == "Auswahl: A; B"
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
    detailed = _field_text({
        "id": "path",
        "title": "Ordner",
        "type": "file",
        "step": 1,
        "units": "px",
        "select-dir": True,
        "expand-width": True,
    })
    assert "Schrittweite: 1" in detailed
    assert "Einheit: px" in detailed
    assert "Ordnerauswahl: aktiviert" in detailed
    assert "Breite: horizontal ausdehnen" in detailed
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
    assert {
        "Einstellungen",
        "Formatierungen",
        "Accounts · OpenAI",
        "Accounts · Google",
    }.issubset(titles)
    settings_group = next(group for group in groups if group["title"] == "Einstellungen")
    settings_entries = [
        entry for section in settings_group["sections"] for entry in section["entries"]
    ]
    assert "Spalten im Leisten-Editor" in {entry["title"] for entry in settings_entries}
    refresh_entry = next(
        entry for entry in settings_entries
        if entry["title"] == "Aktualisierungsintervall in Sekunden"
    )
    assert "Standard: 300" in refresh_entry["text"]
    assert "Grenzen: 60 bis 3600" in refresh_entry["text"]
    assert "Schrittweite: 60" in refresh_entry["text"]

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


def test_format_tables_own_hover_click_and_null_columns_without_legacy_target_table() -> None:
    schema = _schema()
    format_tables = {
        table["key"] for table in schema["format-table-selector"]["tables"]
    }
    assert "account-style-targets" not in format_tables
    for table in schema["format-table-selector"]["tables"]:
        key = table["key"]
        definition = _help_definition(schema[key], schema, key)
        if key == "account-display-settings":
            continue
        columns = {column["id"]: column for column in definition["columns"]}
        assert columns["show-hover"]["title"] == "In Hovermenü anzeigen"
        assert columns["show-click"]["title"] == "In Klickmenü anzeigen"
        assert columns["hide-when-zero"]["title"] == "Bei Null ausblenden"


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

    credit_entries = [
        entry
        for group in groups
        for section in group["sections"]
        for entry in section["entries"]
        if entry["title"] == "Δ Creditverbrauch"
    ]
    assert len(credit_entries) == 1
    credit_fields = {field["title"]: field for field in credit_entries[0]["fields"]}
    assert "Dynamisch" in credit_fields
    assert "Schwelle Verbrauch %" in credit_fields


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
    assert [column["id"] for column in malformed_base["columns"]] == [
        [],
        "override",
        "dynamic",
    ]


def test_help_materializes_copies_when_columns_are_malformed() -> None:
    base = {"columns": [{"id": "account", "title": "Account", "type": "string"}]}
    copied = _help_definition(
        {"format-copy-of": "base", "columns": None},
        {"base": base},
        "account-panel-tag-styles",
    )
    assert copied["columns"] == base["columns"]

    delta = _help_definition(
        {"columns": "broken"},
        {"account-percent-styles": base},
        "account-delta-styles",
    )
    assert [column["id"] for column in delta["columns"]] == ["account", "dynamic"]


def test_help_definition_does_not_mutate_delta_schema_when_base_is_missing() -> None:
    definition = {"columns": [{"id": "account", "title": "Account"}]}

    resolved = _help_definition(definition, {}, "account-delta-styles")

    assert [column["id"] for column in definition["columns"]] == ["account"]
    assert [column["id"] for column in resolved["columns"]] == ["account", "dynamic"]


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


def test_help_page_skips_invalid_utf8_markup_without_aborting() -> None:
    schema = {
        "layout": {
            "pages": ["page"],
            "page": {"title": "Seite\ud800", "sections": ["section"]},
            "section": {"title": "Abschnitt", "keys": ["field"]},
        },
        "field": {"type": "label", "description": "Text\x00\ud800"},
    }
    assert _markup("Text\x00\ud800") == "Text??"
    widget = HelpPage({}, "help-content", SimpleNamespace(settings=schema))
    widget.destroy()


def test_help_page_requests_readable_minimum_size() -> None:
    widget = HelpPage({}, "help-content", SimpleNamespace(settings=_schema()))
    try:
        width, height = widget.get_size_request()
        assert width >= 720
        assert height >= 560
        assert widget.content_widget.get_min_content_width() >= 640
        assert widget.content_widget.get_min_content_height() >= 480
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
    schema = _schema()
    expected_entries = sum(
        len(section["entries"])
        for group in build_help_groups(schema)
        for section in group["sections"]
    )
    widget = HelpPage({}, "help-content", SimpleNamespace(settings=schema))
    try:
        expanders = list(_expanders(widget))
        assert len(expanders) == expected_entries
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
