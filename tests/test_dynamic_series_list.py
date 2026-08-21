from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLET_DIR = ROOT / "files" / "codex-usage@H234598"
sys.path.insert(0, str(APPLET_DIR))
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings")
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings/bin")

from dynamic_series_list import DynamicSeriesList  # noqa: E402


class _SeriesTable:
    _series_column_index = 1
    _active_column_index = 2

    def __init__(self, rows: list[list[object]], available: tuple[str, ...]):
        self.model = rows
        self._available = available

    def _active_owners(self):
        return DynamicSeriesList._active_owners(self)

    def _masterjet_series(self):
        return self._available


class _TreeModelRow:
    """GTK TreeModelRow shape: indexable, but not a list/tuple and no len()."""

    def __init__(self, values: list[object]):
        self._values = values

    def __getitem__(self, index):
        return self._values[index]


def test_active_owners_require_a_real_boolean_true() -> None:
    table = _SeriesTable([
        ["alpha", "A", True],
        ["beta", "B", "false"],
        ["gamma", "C", 1],
    ], ("A", "B", "C"))

    assert DynamicSeriesList._active_owners(table) == {"A": "alpha"}


def test_active_owners_accept_gtk_tree_model_row_shape() -> None:
    table = _SeriesTable([
        _TreeModelRow(["alpha", "A", True]),
        _TreeModelRow(["beta", "B", True]),
    ], ("A", "B"))

    assert DynamicSeriesList._active_owners(table) == {"A": "alpha", "B": "beta"}


def test_series_options_hide_other_active_owners_but_keep_current_assignment() -> None:
    table = _SeriesTable([
        ["alpha", "A", True],
        ["beta", "B", True],
        ["gamma", "", False],
    ], ("A", "B", "C"))

    beta_options = DynamicSeriesList._series_options_for(table, table.model[1])
    gamma_options = DynamicSeriesList._series_options_for(table, table.model[2])

    assert beta_options == {"Keine Serie": "", "B": "B", "C": "C"}
    assert gamma_options == {"Keine Serie": "", "C": "C"}


def test_active_owners_ignore_malformed_rows() -> None:
    table = _SeriesTable([
        ["alpha"],
        None,
        ["beta", "B", True],
    ], ("A", "B"))

    assert DynamicSeriesList._active_owners(table) == {"B": "beta"}


def test_series_options_ignore_malformed_current_row() -> None:
    table = _SeriesTable([["alpha", "A", True]], ("A", "B"))

    assert DynamicSeriesList._series_options_for(table, ["short"]) == {
        "Keine Serie": "",
        "B": "B",
    }


def test_series_options_preserve_current_gtk_tree_model_assignment() -> None:
    table = _SeriesTable([
        _TreeModelRow(["alpha", "A", True]),
        _TreeModelRow(["beta", "B", True]),
    ], ("A", "B", "C"))

    assert DynamicSeriesList._series_options_for(table, table.model[0]) == {
        "Keine Serie": "",
        "A": "A",
        "C": "C",
    }


def test_masterjet_series_filters_provider_state_and_caches_result(tmp_path, monkeypatch) -> None:
    command = tmp_path / "masterjet-series"
    command.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'series': ["
        "{'prefix': 'a', 'enabled': True, 'provider': 'openai_chatgpt'},"
        "{'prefix': 'b', 'enabled': False, 'provider': 'openai_chatgpt'},"
        "{'prefix': 'g', 'enabled': True, 'provider': 'gemini'},"
        "{'prefix': 'A', 'enabled': True, 'provider': 'openai_chatgpt'},"
        "{'prefix': 'bad value', 'enabled': True, 'provider': 'openai_chatgpt'}"
        "]}))\n",
        encoding="utf-8",
    )
    command.chmod(0o700)
    monkeypatch.setenv("CODEX_MASTER_MCP", str(command))
    DynamicSeriesList._masterjet_cache = None
    DynamicSeriesList._masterjet_cache_at = 0.0

    series_widget = DynamicSeriesList.__new__(DynamicSeriesList)
    assert DynamicSeriesList._masterjet_series(series_widget) == ("A",)

    # A second call must use the bounded cache rather than executing again.
    command.write_text("raise SystemExit(7)\n", encoding="utf-8")
    assert DynamicSeriesList._masterjet_series(series_widget) == ("A",)

    DynamicSeriesList._masterjet_cache = None
    DynamicSeriesList._masterjet_cache_at = 0.0


def test_masterjet_series_keeps_ascii_hyphen_and_underscore_prefixes(tmp_path, monkeypatch) -> None:
    command = tmp_path / "masterjet-prefixed-series"
    command.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'series': ["
        "{'prefix': 'q-inplace', 'enabled': True, 'provider': 'openai_chatgpt'},"
        "{'prefix': 'a_b', 'enabled': True, 'provider': 'openai_chatgpt'},"
        "{'prefix': '9bad', 'enabled': True, 'provider': 'openai_chatgpt'},"
        "{'prefix': 'ä', 'enabled': True, 'provider': 'openai_chatgpt'}"
        "]}))\n",
        encoding="utf-8",
    )
    command.chmod(0o700)
    monkeypatch.setenv("CODEX_MASTER_MCP", str(command))
    DynamicSeriesList._masterjet_cache = None
    DynamicSeriesList._masterjet_cache_at = 0.0

    series_widget = DynamicSeriesList.__new__(DynamicSeriesList)
    assert DynamicSeriesList._masterjet_series(series_widget) == ("A_B", "Q-INPLACE")

    DynamicSeriesList._masterjet_cache = None
    DynamicSeriesList._masterjet_cache_at = 0.0


def test_masterjet_series_fails_closed_when_child_closes_stdout_but_hangs(
    tmp_path, monkeypatch
) -> None:
    command = tmp_path / "masterjet-hanging-series"
    command.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import time\n"
        "os.close(1)\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    command.chmod(0o700)
    monkeypatch.setenv("CODEX_MASTER_MCP", str(command))
    DynamicSeriesList._masterjet_cache = None
    DynamicSeriesList._masterjet_cache_at = 0.0

    series_widget = DynamicSeriesList.__new__(DynamicSeriesList)
    series_widget._MASTERJET_TIMEOUT_SECONDS = 0.1
    assert DynamicSeriesList._masterjet_series(series_widget) == ()

def test_column_index_rejects_missing_series_columns() -> None:
    class _Columns:
        def __init__(self) -> None:
            self.columns = [{"id": "account"}]

    try:
        DynamicSeriesList._column_index(_Columns(), "series")
    except ValueError as exc:
        assert str(exc) == "dynamic series table is missing series"
    else:
        raise AssertionError("missing series column was accepted")
