from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APPLET_DIR = ROOT / "files" / "codex-usage@H234598"
sys.path.insert(0, str(APPLET_DIR))
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings")
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings/bin")

from dynamic_series_list import DynamicSeriesList  # noqa: E402
from TreeListWidgets import List  # noqa: E402

_MISSING = object()
_MASTERJET_CACHE_FIELDS = (
    "_masterjet_cache",
    "_masterjet_cache_at",
    "_masterjet_cache_key",
)


@pytest.fixture(autouse=True)
def _restore_dynamic_series_class_cache():
    original = {
        field: getattr(DynamicSeriesList, field, _MISSING)
        for field in _MASTERJET_CACHE_FIELDS
    }
    for field in _MASTERJET_CACHE_FIELDS:
        if hasattr(DynamicSeriesList, field):
            delattr(DynamicSeriesList, field)
    try:
        yield
    finally:
        for field, value in original.items():
            if value is _MISSING:
                if hasattr(DynamicSeriesList, field):
                    delattr(DynamicSeriesList, field)
            else:
                setattr(DynamicSeriesList, field, value)


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


class _Settings:
    def __init__(self):
        self.values = {"account-series-settings": []}
        self.listeners = {}

    def listen(self, key, callback):
        self.listeners.setdefault(key, []).append(callback)

    def get_value(self, key):
        return self.values[key]

    def set_value(self, key, value):
        self.values[key] = value


class _ReadErrorSettings(_Settings):
    def get_value(self, key):
        raise OSError("settings read failed")


class _ListenerErrorSettings(_Settings):
    def listen(self, key, callback):
        raise OSError("listener registration failed")


class _WriteErrorSettings(_Settings):
    def set_value(self, key, value):
        raise OSError("settings write failed")


class _ListenerPropertyErrorSettings:
    def get_value(self, key):
        return []

    def listen(self, key, callback):
        raise OSError("listener registration failed")

    @property
    def listeners(self):
        raise RuntimeError("listener registry unavailable")


def test_active_owners_require_a_real_boolean_true() -> None:
    table = _SeriesTable([
        ["alpha", "A", True],
        ["beta", "B", "false"],
        ["gamma", "C", 1],
    ], ("A", "B", "C"))

    assert DynamicSeriesList._active_owners(table) == {"A": "alpha"}


def test_active_owners_normalize_account_whitespace() -> None:
    table = _SeriesTable([
        ["  alpha  ", "A", True],
    ], ("A",))

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


def test_series_options_drop_malformed_current_series() -> None:
    table = _SeriesTable([["alpha", "A\x00", True]], ())

    assert DynamicSeriesList._series_options_for(table, table.model[0]) == {
        "Keine Serie": "",
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


def test_series_options_keep_conflicting_current_assignment_editable() -> None:
    table = _SeriesTable([
        ["alpha", "A", True],
        ["beta", "A", True],
    ], ("A", "B"))

    assert DynamicSeriesList._series_options_for(table, table.model[0]) == {
        "Keine Serie": "",
        "B": "B",
        "A (aktuell)": "A",
    }


def test_masterjet_series_uses_only_codex_usage_control_cli(
    tmp_path, monkeypatch
) -> None:
    class _Stream:
        def fileno(self):
            return 17

    class _Process:
        pid = 123

        def __init__(self):
            self.stdout = _Stream()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    process = _Process()
    chunks = iter([
        json.dumps({
            "stale": False,
            "series": [
                {"prefix": "a", "enabled": True, "provider": "openai_chatgpt"}
            ]
        }).encode("utf-8"),
        b"",
    ])
    captured = {}

    def _popen(argv, **kwargs):
        captured["argv"] = argv
        return process

    monkeypatch.setenv("CODEX_MASTER_MCP", str(tmp_path / "codex-master-mcp"))
    monkeypatch.setattr("dynamic_series_list.Path.home", lambda: tmp_path)
    monkeypatch.setattr("dynamic_series_list.subprocess.Popen", _popen)
    monkeypatch.setattr(
        "dynamic_series_list.select.select",
        lambda *_args: ([process.stdout], [], []),
    )
    monkeypatch.setattr("dynamic_series_list.os.read", lambda *_args: next(chunks))
    series_widget = DynamicSeriesList.__new__(DynamicSeriesList)
    assert DynamicSeriesList._masterjet_series(series_widget) == ("A",)
    assert series_widget._masterjet_projection_ready is True
    assert captured["argv"] == [
        str(tmp_path / ".local/bin/codex-usage"),
        "masterjet",
        "openai-routing-options",
        "--json",
    ]
    assert all("codex-master-mcp" not in argument for argument in captured["argv"])


def test_masterjet_series_rechecks_projection_instead_of_offering_cached_routing(
    tmp_path, monkeypatch
) -> None:
    command = tmp_path / ".local/bin/codex-usage"
    command.parent.mkdir(parents=True)
    command.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'stale': False, 'series': ["
        "{'prefix': 'a', 'enabled': True, 'provider': 'openai_chatgpt'},"
        "{'prefix': 'b', 'enabled': False, 'provider': 'openai_chatgpt'},"
        "{'prefix': 'g', 'enabled': True, 'provider': 'gemini'},"
        "{'prefix': 'A', 'enabled': True, 'provider': 'openai_chatgpt'},"
        "{'prefix': 'bad value', 'enabled': True, 'provider': 'openai_chatgpt'}"
        "]}))\n",
        encoding="utf-8",
    )
    command.chmod(0o700)
    monkeypatch.setattr("dynamic_series_list.Path.home", lambda: tmp_path)
    series_widget = DynamicSeriesList.__new__(DynamicSeriesList)
    assert DynamicSeriesList._masterjet_series(series_widget) == ("A",)

    # Routing mutation must recheck current projection instead of trusting cache.
    command.write_text("raise SystemExit(7)\n", encoding="utf-8")
    assert DynamicSeriesList._masterjet_series(series_widget) == ()
    assert series_widget._masterjet_projection_ready is False

def test_masterjet_series_keeps_ascii_hyphen_and_underscore_prefixes(tmp_path, monkeypatch) -> None:
    command = tmp_path / ".local/bin/codex-usage"
    command.parent.mkdir(parents=True)
    command.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'stale': False, 'series': ["
        "{'prefix': 'q-inplace', 'enabled': True, 'provider': 'openai_chatgpt'},"
        "{'prefix': 'a_b', 'enabled': True, 'provider': 'openai_chatgpt'},"
        "{'prefix': '9bad', 'enabled': True, 'provider': 'openai_chatgpt'},"
        "{'prefix': 'ä', 'enabled': True, 'provider': 'openai_chatgpt'}"
        "]}))\n",
        encoding="utf-8",
    )
    command.chmod(0o700)
    monkeypatch.setattr("dynamic_series_list.Path.home", lambda: tmp_path)
    series_widget = DynamicSeriesList.__new__(DynamicSeriesList)
    assert DynamicSeriesList._masterjet_series(series_widget) == ("A_B", "Q-INPLACE")

def test_stale_routing_projection_is_not_offered_or_editable(tmp_path, monkeypatch) -> None:
    command = tmp_path / ".local/bin/codex-usage"
    command.parent.mkdir(parents=True)
    command.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'stale': True, 'series': ["
        "{'prefix': 'a', 'enabled': True, 'provider': 'openai_chatgpt'}]}))\n",
        encoding="utf-8",
    )
    command.chmod(0o700)
    monkeypatch.setattr("dynamic_series_list.Path.home", lambda: tmp_path)
    series_widget = DynamicSeriesList.__new__(DynamicSeriesList)

    assert DynamicSeriesList._masterjet_series(series_widget) == ()
    assert series_widget._masterjet_projection_ready is False

    series_widget.columns = [
        {"id": "account"},
        {"id": "series"},
        {"id": "series-active"},
    ]
    series_widget.model = [["alpha", "A", True]]
    series_widget._series_column_index = 1
    series_widget._active_column_index = 2
    monkeypatch.setattr(
        "dynamic_series_list.List.open_add_edit_dialog",
        lambda *_args: "mutation-dialog-opened",
    )

    assert DynamicSeriesList.open_add_edit_dialog(series_widget, series_widget.model[0]) is None


def test_stale_routing_projection_cannot_persist_series_active_change() -> None:
    writes = []
    reloads = []
    widget = DynamicSeriesList.__new__(DynamicSeriesList)
    widget.model = [["alpha", "A", True]]
    widget.columns = [
        {"id": "account"},
        {"id": "series"},
        {"id": "series-active"},
    ]
    widget.set_value = lambda value: writes.append(value)
    widget.update_button_sensitivity = lambda: None
    widget.on_setting_changed = lambda: reloads.append("reloaded")

    def stale_projection():
        widget._masterjet_projection_ready = False
        return ()

    widget._masterjet_series = stale_projection
    DynamicSeriesList.list_changed(widget)

    assert writes == []
    assert reloads == ["reloaded"]


def test_masterjet_series_fails_closed_when_child_closes_stdout_but_hangs(
    tmp_path, monkeypatch
) -> None:
    command = tmp_path / ".local/bin/codex-usage"
    command.parent.mkdir(parents=True)
    command.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import time\n"
        "os.close(1)\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    command.chmod(0o700)
    monkeypatch.setattr("dynamic_series_list.Path.home", lambda: tmp_path)
    series_widget = DynamicSeriesList.__new__(DynamicSeriesList)
    series_widget._MASTERJET_TIMEOUT_SECONDS = 0.1
    assert DynamicSeriesList._masterjet_series(series_widget) == ()


def test_masterjet_cleanup_ignores_child_exit_race(monkeypatch) -> None:
    class _Stream:
        def fileno(self):
            return 17

    class _Process:
        pid = 123

        def __init__(self):
            self.stdout = _Stream()

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            raise ProcessLookupError("child already exited")

    process = _Process()
    monkeypatch.setattr(
        "dynamic_series_list.subprocess.Popen", lambda *_args, **_kwargs: process
    )
    monkeypatch.setattr(
        "dynamic_series_list.select.select",
        lambda *_args: ([process.stdout], [], []),
    )
    monkeypatch.setattr("dynamic_series_list.os.read", lambda *_args: b"")
    monkeypatch.setattr(
        "dynamic_series_list.os.killpg",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError("group gone")),
    )
    series_widget = DynamicSeriesList.__new__(DynamicSeriesList)
    assert DynamicSeriesList._masterjet_series(series_widget) == ()


def test_masterjet_cleanup_reaps_after_second_kill(monkeypatch) -> None:
    class _Stream:
        def fileno(self):
            return 17

    class _Process:
        pid = 123

        def __init__(self):
            self.stdout = _Stream()
            self.wait_calls = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls < 3:
                raise subprocess.TimeoutExpired(["masterjet"], timeout)
            return -9

    process = _Process()
    monkeypatch.setattr(
        "dynamic_series_list.subprocess.Popen", lambda *_args, **_kwargs: process
    )
    monkeypatch.setattr(
        "dynamic_series_list.select.select",
        lambda *_args: ([process.stdout], [], []),
    )
    monkeypatch.setattr("dynamic_series_list.os.read", lambda *_args: b"")
    monkeypatch.setattr("dynamic_series_list.os.killpg", lambda *_args: None)

    series_widget = DynamicSeriesList.__new__(DynamicSeriesList)

    assert DynamicSeriesList._masterjet_series(series_widget) == ()
    assert process.wait_calls == 3


def test_masterjet_cleanup_ignores_wait_reaping_race(monkeypatch) -> None:
    class _Stream:
        def fileno(self):
            return 17

    class _Process:
        pid = 123

        def __init__(self):
            self.stdout = _Stream()
            self.wait_calls = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                return 0
            raise OSError("child already reaped")

        def kill(self):
            pass

    process = _Process()
    monkeypatch.setattr(
        "dynamic_series_list.subprocess.Popen", lambda *_args, **_kwargs: process
    )
    monkeypatch.setattr(
        "dynamic_series_list.select.select",
        lambda *_args: ([process.stdout], [], []),
    )
    monkeypatch.setattr("dynamic_series_list.os.read", lambda *_args: b"")
    monkeypatch.setattr(
        "dynamic_series_list.os.killpg",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError("group gone")),
    )

    series_widget = DynamicSeriesList.__new__(DynamicSeriesList)

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


def test_destroy_detaches_settings_listener() -> None:
    settings = _Settings()
    widget = DynamicSeriesList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {"id": "series", "title": "Serie", "type": "string"},
                {"id": "series-active", "title": "Aktiv", "type": "boolean"},
            ],
            "show-buttons": False,
        },
        "account-series-settings",
        settings,
    )

    assert len(settings.listeners["account-series-settings"]) == 1
    widget.destroy()

    assert settings.listeners["account-series-settings"] == []


def test_malformed_account_rows_do_not_break_settings_table() -> None:
    settings = _Settings()
    settings.values["account-series-settings"] = None
    widget = DynamicSeriesList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {"id": "series", "title": "Serie", "type": "string"},
                {"id": "series-active", "title": "Aktiv", "type": "boolean"},
            ],
            "show-buttons": False,
        },
        "account-series-settings",
        settings,
    )

    try:
        assert len(widget.model) == 0
        settings.values["account-series-settings"] = [None, "broken", {
            "account": "alpha",
            "series": "A",
            "series-active": True,
        }]
        widget.on_setting_changed()
        assert len(widget.model) == 1
        settings.values["account-series-settings"] = [{
            "account": 123,
            "series": "A",
            "series-active": True,
        }]
        widget.on_setting_changed()
        assert len(widget.model) == 0
    finally:
        widget.destroy()


def test_settings_read_error_keeps_series_table_open() -> None:
    settings = _ReadErrorSettings()
    widget = None
    try:
        widget = DynamicSeriesList(
            {
                "columns": [
                    {"id": "account", "title": "Account", "type": "string"},
                    {"id": "series", "title": "Serie", "type": "string"},
                    {"id": "series-active", "title": "Aktiv", "type": "boolean"},
                ],
                "show-buttons": False,
            },
            "account-series-settings",
            settings,
        )
        assert len(widget.model) == 0
    finally:
        if widget is not None:
            widget.destroy()


def test_settings_write_error_resets_saving_and_keeps_listener_active(monkeypatch) -> None:
    settings = _WriteErrorSettings()
    settings.values["account-series-settings"] = [{
        "account": "alpha",
        "series": "A",
        "series-active": True,
    }]
    widget = DynamicSeriesList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {"id": "series", "title": "Serie", "type": "string"},
                {"id": "series-active", "title": "Aktiv", "type": "boolean"},
            ],
            "show-buttons": False,
        },
        "account-series-settings",
        settings,
    )
    monkeypatch.setattr(
        DynamicSeriesList,
        "_masterjet_series",
        lambda self: setattr(self, "_masterjet_projection_ready", True) or ("A",),
    )

    try:
        widget.list_changed()
        assert widget._saving is False

        settings.values["account-series-settings"] = [{
            "account": "beta",
            "series": "B",
            "series-active": False,
        }]
        settings.listeners["account-series-settings"][0]()
        assert next(iter(widget.model))[0] == "beta"
    finally:
        widget.destroy()


def test_listener_registration_error_keeps_series_table_open() -> None:
    settings = _ListenerErrorSettings()
    widget = None
    try:
        widget = DynamicSeriesList(
            {
                "columns": [
                    {"id": "account", "title": "Account", "type": "string"},
                    {"id": "series", "title": "Serie", "type": "string"},
                    {"id": "series-active", "title": "Aktiv", "type": "boolean"},
                ],
                "show-buttons": False,
            },
            "account-series-settings",
            settings,
        )
        assert len(widget.model) == 0
    finally:
        if widget is not None:
            widget.destroy()


def test_listener_cleanup_error_keeps_series_table_open() -> None:
    settings = _ListenerPropertyErrorSettings()
    widget = None
    try:
        widget = DynamicSeriesList(
            {
                "columns": [
                    {"id": "account", "title": "Account", "type": "string"},
                    {"id": "series", "title": "Serie", "type": "string"},
                    {"id": "series-active", "title": "Aktiv", "type": "boolean"},
                ],
                "show-buttons": False,
            },
            "account-series-settings",
            settings,
        )
        assert len(widget.model) == 0
    finally:
        if widget is not None:
            widget.destroy()


def test_integer_overflow_in_account_row_does_not_break_settings_table() -> None:
    settings = _Settings()
    settings.values["account-series-settings"] = [{"account": "alpha", "browser": 2**31}]
    widget = DynamicSeriesList(
        {
            "columns": [
                {"id": "account", "title": "Account", "type": "string"},
                {"id": "series", "title": "Serie", "type": "string"},
                {"id": "series-active", "title": "Aktiv", "type": "boolean"},
                {"id": "browser", "title": "Browser", "type": "integer"},
            ],
            "show-buttons": False,
        },
        "account-series-settings",
        settings,
    )

    try:
        assert len(widget.model) == 0
    finally:
        widget.destroy()


def test_open_dialog_filters_series_column_and_restores_schema(monkeypatch) -> None:
    widget = DynamicSeriesList.__new__(DynamicSeriesList)
    original_columns = [
        {"id": "account", "title": "Account", "type": "string"},
        {"id": "series", "title": "Serie", "type": "string"},
    ]
    widget.columns = original_columns
    widget._series_options_for = lambda _info: {"Keine Serie": "", "A": "A"}
    widget._masterjet_projection_ready = True
    captured = []

    def fake_open_add_edit_dialog(self, info=None):
        captured.append((self.columns, info))
        return ["alpha", "A"]

    monkeypatch.setattr(List, "open_add_edit_dialog", fake_open_add_edit_dialog)

    assert DynamicSeriesList.open_add_edit_dialog(widget, ["alpha", ""]) == ["alpha", "A"]
    assert widget.columns is original_columns
    assert captured[0][1] == ["alpha", ""]
    assert captured[0][0][1]["options"] == {"Keine Serie": "", "A": "A"}


def test_open_dialog_survives_base_editor_error_and_restores_schema(monkeypatch) -> None:
    widget = DynamicSeriesList.__new__(DynamicSeriesList)
    original_columns = [
        {"id": "account", "title": "Account", "type": "string"},
        {"id": "series", "title": "Serie", "type": "string"},
    ]
    widget.columns = original_columns
    widget._series_options_for = lambda _info: {"Keine Serie": "", "A": "A"}

    def fail_open_add_edit_dialog(self, _info=None):
        raise RuntimeError("base editor failed")

    monkeypatch.setattr(List, "open_add_edit_dialog", fail_open_add_edit_dialog)

    assert DynamicSeriesList.open_add_edit_dialog(widget, ["alpha", ""]) is None
    assert widget.columns is original_columns


@pytest.mark.parametrize("iteration", range(2))
def test_dynamic_series_cache_state_is_order_independent(iteration: int) -> None:
    assert iteration in (0, 1)
    assert not hasattr(DynamicSeriesList, "_masterjet_cache")
    assert not hasattr(DynamicSeriesList, "_masterjet_cache_at")
    assert not hasattr(DynamicSeriesList, "_masterjet_cache_key")
    DynamicSeriesList._masterjet_cache = ("must-not-leak",)
    DynamicSeriesList._masterjet_cache_at = 99.0
    DynamicSeriesList._masterjet_cache_key = "must-not-leak"
