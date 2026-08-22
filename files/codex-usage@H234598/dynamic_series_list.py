#!/usr/bin/env python3
"""Account table with per-row filtering for the Masterjet series selector."""

from __future__ import annotations

import copy
import json
import os
import re
import select
import signal
import subprocess
import time
from pathlib import Path

from JsonSettingsWidgets import JSONSettingsBackend
from TreeListWidgets import List


class DynamicSeriesList(List, JSONSettingsBackend):
    """Keep active series out of every other account's edit combobox."""

    _SERIES_COLUMN = "series"
    _ACTIVE_COLUMN = "series-active"
    _MAX_MASTERJET_OUTPUT = 128 * 1024
    _MAX_SERIES = 256
    _MASTERJET_TIMEOUT_SECONDS = 2.0
    _MASTERJET_CACHE_SECONDS = 30.0
    _SERIES_PREFIX_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,15}")
    _masterjet_cache = None
    _masterjet_cache_at = 0.0

    def __init__(self, info, key, settings):
        self.backend = "json"
        self.key = key
        self.settings = settings
        super().__init__(
            label=info.get("description"),
            columns=info.get("columns", []),
            height=info.get("height", 200),
            show_buttons=info.get("show-buttons", True),
            hidden_buttons=info.get("hidden-buttons", []),
            tooltip=info.get("tooltip", ""),
        )
        self._series_column_index = self._column_index(self._SERIES_COLUMN)
        self._active_column_index = self._column_index(self._ACTIVE_COLUMN)

        self.attach()

    def _column_index(self, column_id):
        for index, column in enumerate(self.columns):
            if column.get("id") == column_id:
                return index
        raise ValueError("dynamic series table is missing " + column_id)

    def detach(self) -> None:
        listeners = getattr(self.settings, "listeners", None)
        if not isinstance(listeners, dict):
            return
        callbacks = listeners.get(self.key)
        if not isinstance(callbacks, list):
            return
        listener = self._settings_changed_callback
        callbacks[:] = [callback for callback in callbacks if callback != listener]

    def _masterjet_series(self):
        now = time.monotonic()
        if (
            self._masterjet_cache is not None
            and now - self._masterjet_cache_at < self._MASTERJET_CACHE_SECONDS
        ):
            return self._masterjet_cache

        command = os.environ.get("CODEX_MASTER_MCP", "").strip()
        argv = [command, "fleet", "series", "list"] if command else [
            str(Path.home() / ".local/bin/codex-master-mcp"),
            "fleet", "series", "list",
        ]
        result = []
        process = None
        output = bytearray()
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + self._MASTERJET_TIMEOUT_SECONDS
            stream = process.stdout
            while stream is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Masterjet-Serienabfrage timeout")
                ready, _, _ = select.select([stream], [], [], remaining)
                if not ready:
                    raise TimeoutError("Masterjet-Serienabfrage timeout")
                chunk = os.read(
                    stream.fileno(),
                    min(8192, self._MAX_MASTERJET_OUTPUT + 1 - len(output)),
                )
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > self._MAX_MASTERJET_OUTPUT:
                    raise ValueError("Masterjet-Serienantwort zu groß")
            if process.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
                raise RuntimeError("Masterjet-Serienabfrage fehlgeschlagen")
            payload = json.loads(output.decode("utf-8"))
            raw_series = payload.get("series") if isinstance(payload, dict) else None
            if not isinstance(raw_series, list) or len(raw_series) > self._MAX_SERIES:
                raise ValueError("ungültige Masterjet-Serienantwort")
            for item in raw_series:
                if not isinstance(item, dict) or item.get("enabled") is not True:
                    continue
                if item.get("provider") != "openai_chatgpt":
                    continue
                prefix = item.get("prefix")
                if isinstance(prefix, str) and self._SERIES_PREFIX_RE.fullmatch(prefix):
                    result.append(prefix.upper())
            result = tuple(sorted(set(result)))
        except (
            OSError,
            UnicodeError,
            ValueError,
            RuntimeError,
            TimeoutError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
        ):
            result = ()
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    try:
                        process.kill()
                    except (OSError, ProcessLookupError):
                        pass
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        try:
                            process.kill()
                        except (OSError, ProcessLookupError):
                            pass
                    try:
                        process.wait(timeout=0.5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        self.__class__._masterjet_cache = result
        self.__class__._masterjet_cache_at = now
        return result

    def _active_owners(self):
        owners = {}
        for row in self.model:
            try:
                account = row[0]
                series = row[self._series_column_index]
                active = row[self._active_column_index]
            except (IndexError, KeyError, TypeError):
                continue
            if (
                isinstance(account, str)
                and account.strip()
                and isinstance(series, str)
                and series.strip()
                and active is True
            ):
                owners[series.strip().upper()] = account
        return owners

    def _series_options_for(self, info):
        owners = self._active_owners()
        available = self._masterjet_series()
        current = ""
        account = None
        if info is not None and not isinstance(info, (str, bytes)):
            try:
                current = info[self._series_column_index]
                account = info[0]
            except (IndexError, KeyError, TypeError):
                current = ""
                account = None
        if not isinstance(account, str) or not account.strip():
            account = None
        current = current.strip().upper() if isinstance(current, str) else ""

        options = {"Keine Serie": ""}
        for series in available:
            owner = owners.get(series)
            if owner is None or owner == account:
                options[series] = series
        # Preserve an existing legacy/current assignment (notably A) for its owner,
        # but do not expose it to any other account.
        if current and current not in options and (owners.get(current) in (None, account)):
            options.setdefault(current + " (aktuell)", current)
        return options

    def open_add_edit_dialog(self, info=None):
        original_columns = self.columns
        columns = copy.deepcopy(self.columns)
        for column in columns:
            if column.get("id") == self._SERIES_COLUMN:
                column["options"] = self._series_options_for(info)
                break
        self.columns = columns
        try:
            return super().open_add_edit_dialog(info)
        finally:
            # Keep the base schema columns intact; the next dialog is filtered again.
            self.columns = original_columns

    def destroy(self):
        self.detach()
        return super().destroy()
