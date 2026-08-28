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
    _masterjet_cache_key = None

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

        try:
            self.attach()
        except Exception:
            self.detach()
            try:
                self.on_setting_changed()
            except Exception:
                self.model.clear()
                self.content_widget.columns_autosize()

    def on_setting_changed(self, *_args):
        """Load only row objects; malformed persisted values render empty."""
        self.model.clear()
        try:
            rows = self.get_value()
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError, OSError):
            rows = []
        if not isinstance(rows, list):
            rows = []
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
            try:
                self.model.append(row_info)
            except (OverflowError, TypeError, ValueError):
                continue
        self.content_widget.columns_autosize()

    def list_changed(self, *args):
        """Persist edited rows without wedging callbacks after a write error."""
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
            # JSONSettingsBackend leaves this flag set when a backend write fails;
            # reset it so later external updates are not ignored.
            self._saving = False
            self.update_button_sensitivity()
            return
        self.update_button_sensitivity()

    def _column_index(self, column_id):
        for index, column in enumerate(self.columns):
            if column.get("id") == column_id:
                return index
        raise ValueError("dynamic series table is missing " + column_id)

    def detach(self) -> None:
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
            # Cleanup must not turn a failed settings attachment into a startup error.
            return

    def _masterjet_series(self):
        now = time.monotonic()
        argv = [
            str(Path.home() / ".local/bin/codex-usage"),
            "masterjet",
            "openai-routing-options",
            "--json",
        ]
        cache_key = tuple(argv)
        if (
            self._masterjet_cache is not None
            and now - self._masterjet_cache_at < self._MASTERJET_CACHE_SECONDS
            and self.__class__._masterjet_cache_key == cache_key
        ):
            return self._masterjet_cache

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
                except (OSError, subprocess.TimeoutExpired):
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
        self.__class__._masterjet_cache_key = cache_key
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
                owners[series.strip().upper()] = account.strip()
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
        if isinstance(account, str):
            account = account.strip() or None
        else:
            account = None
        if isinstance(current, str):
            current = current.strip().upper()
            if not DynamicSeriesList._SERIES_PREFIX_RE.fullmatch(current):
                current = ""
        else:
            current = ""

        current_owned_by_account = False
        if current and account is not None:
            for row in self.model:
                try:
                    row_account = row[0]
                    row_series = row[self._series_column_index]
                    row_active = row[self._active_column_index]
                except (IndexError, KeyError, TypeError):
                    continue
                if (
                    isinstance(row_account, str)
                    and row_account.strip() == account
                    and isinstance(row_series, str)
                    and row_series.strip().upper() == current
                    and row_active is True
                ):
                    current_owned_by_account = True
                    break

        options = {"Keine Serie": ""}
        for series in available:
            owner = owners.get(series)
            if owner is None or owner == account:
                options[series] = series
        # Preserve an existing legacy/current assignment (notably A) for its owner,
        # but do not expose it to any other account.
        if current and current not in options and (
            owners.get(current) in (None, account) or current_owned_by_account
        ):
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
            try:
                return super().open_add_edit_dialog(info)
            except Exception:
                return None
        finally:
            # Keep the base schema columns intact; the next dialog is filtered again.
            self.columns = original_columns

    def destroy(self):
        self.detach()
        return super().destroy()
