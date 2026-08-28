#!/usr/bin/env python3
"""OpenAI account status and safe Masterjet controls for Cinnamon settings."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402
from JsonSettingsWidgets import SettingsWidget  # noqa: E402

_OPENAI_REMOTE_FIELDS = frozenset(
    {
        "ref",
        "label",
        "enabled",
        "local_profile_ref",
        "source_host_ref",
        "auth_state",
        "access_expires_at",
        "credential_generation",
        "vault_projection_state",
        "usage_state",
    }
)
_OPENAI_LOCAL_FIELDS = frozenset(
    {
        "account",
        "label",
        "tag",
        "series",
        "series-active",
        "auth-json",
        "profile-dir",
        "test-home",
        "browser",
        "backend",
        "local_auth_state",
        "auth_sync_required",
    }
)
_SECRET_FIELD_PARTS = (
    "access_token",
    "refresh_token",
    "client_secret",
    "api_key",
    "password",
    "cookie",
    "totp",
    "bearer",
    "project_id",
    "provider_id",
)
_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")


def _text(value: object, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {field}")
    result = value.strip()
    if not result or len(result) > maximum or "\x00" in result:
        raise ValueError(f"invalid {field}")
    return result


def _code(value: object, field: str) -> str:
    result = _text(value, field, maximum=64)
    if any(character not in _CODE_CHARS for character in result):
        raise ValueError(f"invalid {field}")
    return result


def _mapping(value: object, fields: frozenset[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not set(value).issubset(fields):
        raise ValueError(f"private or invalid {label} fields")
    for key in value:
        folded = str(key).casefold().replace("-", "_")
        if any(marker in folded for marker in _SECRET_FIELD_PARTS):
            raise ValueError(f"private {label} field")
    return value


@dataclass(frozen=True, slots=True)
class OpenAIAccountRow:
    account_ref: str
    label: str
    local_auth_state: str
    vault_projection_state: str
    access_expires_at: str | None
    credential_generation: int
    usage_state: str
    hive_active: bool
    reauth_enabled: bool
    auth_sync_enabled: bool
    disable_enabled: bool


class OpenAIAccountsModel:
    """Immutable redacted rows; local paths and credential bytes never enter it."""

    __slots__ = ("_rows", "stale")

    def __init__(self) -> None:
        self._rows: tuple[OpenAIAccountRow, ...] = ()
        self.stale = False

    @property
    def rows(self) -> tuple[OpenAIAccountRow, ...]:
        return self._rows

    def row(self, account_ref: str) -> OpenAIAccountRow:
        matches = [row for row in self._rows if row.account_ref == account_ref]
        if len(matches) != 1:
            raise KeyError(account_ref)
        return matches[0]

    def render(
        self,
        local_accounts: Iterable[object],
        masterjet_accounts: Iterable[object],
        *,
        stale: bool = False,
    ) -> None:
        remote_by_profile: dict[str, Mapping[str, object]] = {}
        for value in masterjet_accounts:
            remote_row = _mapping(value, _OPENAI_REMOTE_FIELDS, "OpenAI projection")
            if set(remote_row) != _OPENAI_REMOTE_FIELDS:
                raise ValueError("private or invalid OpenAI projection fields")
            profile_ref = _text(remote_row["local_profile_ref"], "local_profile_ref")
            if profile_ref in remote_by_profile:
                raise ValueError("duplicate OpenAI projection")
            remote_by_profile[profile_ref] = remote_row

        rows: list[OpenAIAccountRow] = []
        seen: set[str] = set()
        mutations_enabled = stale is False
        for value in local_accounts:
            local = _mapping(value, _OPENAI_LOCAL_FIELDS, "local OpenAI account")
            account_ref = _text(local.get("account"), "account")
            if account_ref in seen:
                raise ValueError("duplicate local OpenAI account")
            seen.add(account_ref)
            remote = remote_by_profile.get(account_ref)
            local_state = _code(local.get("local_auth_state", "unknown"), "local_auth_state")
            sync_required = local.get("auth_sync_required", False) is True
            if remote is None:
                vault_state = "unavailable"
                expires_at = None
                generation = 0
                usage_state = "unavailable"
                remote_enabled = False
            else:
                vault_state = _code(remote["vault_projection_state"], "vault_projection_state")
                raw_expiry = remote["access_expires_at"]
                expires_at = (
                    None
                    if raw_expiry is None
                    else _text(raw_expiry, "access_expires_at", maximum=64)
                )
                raw_generation = remote["credential_generation"]
                if type(raw_generation) is not int or raw_generation < 0:
                    raise ValueError("invalid credential_generation")
                generation = raw_generation
                usage_state = _code(remote["usage_state"], "usage_state")
                remote_enabled = remote["enabled"] is True
            rows.append(
                OpenAIAccountRow(
                    account_ref=account_ref,
                    label=_text(local.get("label", account_ref), "label"),
                    local_auth_state=local_state,
                    vault_projection_state=vault_state,
                    access_expires_at=expires_at,
                    credential_generation=generation,
                    usage_state=usage_state,
                    hive_active=local.get("series-active") is True and remote_enabled,
                    reauth_enabled=mutations_enabled,
                    auth_sync_enabled=mutations_enabled
                    and (sync_required or vault_state not in {"current", "synced"}),
                    disable_enabled=mutations_enabled,
                )
            )
        self._rows = tuple(rows)
        self.stale = bool(stale)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(rows={self._rows!r}, stale={self.stale!r})"


@dataclass(frozen=True, slots=True)
class CommandResult:
    ok: bool
    payload: object
    code: str


class BoundedJsonRunner:
    """Run only fixed local CLI commands with bounded output in a worker thread."""

    __slots__ = ("_dispatcher", "_max_output", "_timeout")

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        max_output_bytes: int = 512 * 1024,
        dispatcher: Callable[..., object] = GLib.idle_add,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_output = max_output_bytes
        self._dispatcher = dispatcher

    def submit(
        self,
        argv: Sequence[str],
        *,
        stdin_data: bytes | bytearray | None = None,
        callback: Callable[[CommandResult], object] | None = None,
    ) -> None:
        command = tuple(argv)
        if not command or any(
            not isinstance(part, str) or not part or "\x00" in part for part in command
        ):
            raise ValueError("invalid command")
        secret = bytearray(stdin_data) if stdin_data is not None else None

        def worker() -> None:
            try:
                result = self._run(command, secret)
            finally:
                if secret is not None:
                    secret[:] = b"\x00" * len(secret)
                    secret.clear()
            if callback is not None:
                self._dispatcher(callback, result)

        threading.Thread(target=worker, name="codex-usage-control", daemon=True).start()

    def _run(self, argv: tuple[str, ...], stdin_data: bytearray | None) -> CommandResult:
        process = None
        output = bytearray()
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_safe_environment(),
                start_new_session=True,
            )
            if process.stdin is not None:
                process.stdin.write(stdin_data or b"")
                process.stdin.close()
            deadline = time.monotonic() + self._timeout
            stream = process.stdout
            while stream is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                ready, _, _ = select.select([stream], [], [], remaining)
                if not ready:
                    raise TimeoutError
                chunk = os.read(stream.fileno(), min(8192, self._max_output + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > self._max_output:
                    raise ValueError("output too large")
            returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            payload = json.loads(output.decode("utf-8"))
            if returncode == 0:
                return CommandResult(True, payload, "")
            code = payload.get("code") if isinstance(payload, dict) else None
            return CommandResult(False, None, _redacted_code(code))
        except (OSError, UnicodeError, ValueError, TimeoutError, subprocess.TimeoutExpired):
            return CommandResult(False, None, "control.transport_unavailable")
        finally:
            if process is not None and process.poll() is None:
                _terminate_process_group(process)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(timeout={self._timeout!r}, max_output={self._max_output!r})"


def _safe_environment() -> dict[str, str]:
    allowed = ("HOME", "PATH", "LANG", "LC_ALL", "XDG_CONFIG_HOME", "XDG_STATE_HOME")
    return {name: os.environ[name] for name in allowed if name in os.environ}


def _redacted_code(value: object) -> str:
    try:
        return _code(value, "code")
    except ValueError:
        return "control.transport_unavailable"


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
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


def validate_masterjet_endpoint(transport: object, endpoint: object) -> str:
    kind = _text(transport, "transport", maximum=16)
    value = _text(endpoint, "endpoint", maximum=2048)
    if kind == "local":
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("local masterjet endpoint must be absolute")
        return value
    if kind != "https":
        raise ValueError("unsupported masterjet transport")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid HTTPS masterjet endpoint")
    return value


def save_masterjet_connection(
    setter: Callable[[str, object], object], transport: object, endpoint: object
) -> None:
    kind = _text(transport, "transport", maximum=16)
    value = validate_masterjet_endpoint(kind, endpoint)
    setter("masterjet-connection", {"transport": kind, "endpoint": value})


class OpenAIActions:
    __slots__ = ("_executable", "_runner")

    def __init__(self, runner: BoundedJsonRunner, *, executable: str | None = None) -> None:
        self._runner = runner
        self._executable = executable or str(Path.home() / ".local/bin/codex-usage")

    def reauthenticate(self, account_ref: str) -> None:
        self._runner.submit([self._executable, "reactivate", _text(account_ref, "account")])

    def sync_auth(self, account_ref: str) -> None:
        self._runner.submit(
            [
                self._executable,
                "account",
                "auth-sync",
                _text(account_ref, "account"),
                "--format",
                "json",
            ]
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class MasterjetConnectionWidget(SettingsWidget):
    bind_dir = None

    def __init__(self, info, key, settings):
        del info, key
        SettingsWidget.__init__(self)
        self.set_orientation(Gtk.Orientation.VERTICAL)
        self.set_spacing(6)
        self._settings = settings
        saved = settings.get_value("masterjet-connection") or {}
        transport = saved.get("transport", "local") if isinstance(saved, dict) else "local"
        endpoint = saved.get("endpoint", "") if isinstance(saved, dict) else ""
        self.transport = Gtk.ComboBoxText()
        self.transport.append("local", "Unix-Socket")
        self.transport.append("https", "HTTPS")
        self.transport.set_active_id(transport if transport in {"local", "https"} else "local")
        self.endpoint = Gtk.Entry()
        self.endpoint.set_text(endpoint if isinstance(endpoint, str) else "")
        self.status = Gtk.Label(label="Nicht getestet")
        self.status.set_xalign(0.0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.pack_start(self.transport, False, False, 0)
        row.pack_start(self.endpoint, True, True, 0)
        save = Gtk.Button(label="Speichern")
        save.connect("clicked", self._save)
        row.pack_start(save, False, False, 0)
        test = Gtk.Button(label="Verbindung testen")
        test.connect("clicked", self._test)
        row.pack_start(test, False, False, 0)
        self.pack_start(row, False, False, 0)
        self.pack_start(self.status, False, False, 0)
        self.show_all()

    def _save(self, *_args) -> None:
        try:
            save_masterjet_connection(
                self._settings.set_value,
                self.transport.get_active_id(),
                self.endpoint.get_text(),
            )
        except ValueError:
            self.status.set_text("Ungültiger Endpoint")
            return
        self.status.set_text("Gespeichert")

    def _test(self, *_args) -> None:
        self.status.set_text("Prüfung läuft …")
        executable = str(Path.home() / ".local/bin/codex-usage")
        BoundedJsonRunner().submit(
            [executable, "masterjet", "status", "--json"], callback=self._tested
        )

    def _tested(self, result: CommandResult) -> bool:
        self.status.set_text("Verbunden" if result.ok else f"Fehler: {result.code}")
        return False


class OpenAIAccountsPage(SettingsWidget):
    """Custom status widget placed beside existing DynamicSeriesList table."""

    bind_dir = None

    def __init__(self, info, key, settings):
        del info, key, settings
        SettingsWidget.__init__(self)
        self.set_orientation(Gtk.Orientation.VERTICAL)
        self.set_spacing(6)
        self.model = OpenAIAccountsModel()
        self._actions = OpenAIActions(BoundedJsonRunner())
        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.pack_start(self._body, True, True, 0)
        self.show_all()

    def render(self, local_accounts, masterjet_accounts, *, stale=False) -> None:
        self.model.render(local_accounts, masterjet_accounts, stale=stale)
        for child in self._body.get_children():
            self._body.remove(child)
            child.destroy()
        if self.model.stale:
            stale_label = Gtk.Label(label="STALE · Mutationen gesperrt")
            stale_label.set_xalign(0.0)
            self._body.pack_start(stale_label, False, False, 0)
        for row in self.model.rows:
            label = Gtk.Label(
                label=(
                    f"{row.label} · lokal {row.local_auth_state} · Vault "
                    f"{row.vault_projection_state} · Ablauf {row.access_expires_at or '—'} · "
                    f"Generation {row.credential_generation} · Hive "
                    f"{'aktiv' if row.hive_active else 'inaktiv'}"
                )
            )
            label.set_xalign(0.0)
            label.set_line_wrap(True)
            self._body.pack_start(label, False, False, 0)
            buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            reauth = Gtk.Button(label="Re-Auth")
            reauth.set_sensitive(row.reauth_enabled)
            reauth.connect("clicked", self._reauth, row.account_ref)
            buttons.pack_start(reauth, False, False, 0)
            sync = Gtk.Button(label="Auth synchronisieren")
            sync.set_sensitive(row.auth_sync_enabled)
            sync.connect("clicked", self._sync_auth, row.account_ref)
            buttons.pack_start(sync, False, False, 0)
            self._body.pack_start(buttons, False, False, 0)
        self._body.show_all()

    def _reauth(self, _button, account_ref: str) -> None:
        self._actions.reauthenticate(account_ref)

    def _sync_auth(self, _button, account_ref: str) -> None:
        self._actions.sync_auth(account_ref)

    def row(self, account_ref: str) -> OpenAIAccountRow:
        return self.model.row(account_ref)


__all__ = [
    "BoundedJsonRunner",
    "CommandResult",
    "MasterjetConnectionWidget",
    "OpenAIAccountRow",
    "OpenAIAccountsModel",
    "OpenAIAccountsPage",
    "OpenAIActions",
    "save_masterjet_connection",
    "validate_masterjet_endpoint",
]
