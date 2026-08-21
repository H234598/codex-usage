from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "files" / "codex-usage@H234598"))
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings")
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings/bin")

from panel_settings_list import panel_columns, panel_value_count


def test_panel_value_count_defaults_and_bounds() -> None:
    assert panel_value_count("20") == 20
    assert panel_value_count("64") == 64
    assert panel_value_count("0") == 20
    assert panel_value_count("not-a-number") == 20


def test_panel_columns_expand_legacy_schema_without_mutation() -> None:
    base = [
        {"id": "account", "title": "Account", "type": "string"},
        {"id": "slot1", "title": "Wert 1", "type": "integer", "options": {"Aus": 0}},
    ]

    columns = panel_columns(base, "20")

    assert [column["id"] for column in columns][-1] == "slot20"
    assert base[-1]["id"] == "slot1"
    assert columns[-1]["options"]["Abrufweg"] == 17
