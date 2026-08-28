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
REAUTH_TIMEOUT_SECONDS = 15 * 60


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


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"invalid {field}")
    return value


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
        self.stale = True

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
        stale = _boolean(stale, "stale")
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
            sync_required = _boolean(local.get("auth_sync_required", False), "auth_sync_required")
            series_active = _boolean(local.get("series-active", False), "series-active")
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
                remote_enabled = _boolean(remote["enabled"], "enabled")
            rows.append(
                OpenAIAccountRow(
                    account_ref=account_ref,
                    label=_text(local.get("label", account_ref), "label"),
                    local_auth_state=local_state,
                    vault_projection_state=vault_state,
                    access_expires_at=expires_at,
                    credential_generation=generation,
                    usage_state=usage_state,
                    hive_active=series_active and remote_enabled,
                    reauth_enabled=mutations_enabled,
                    auth_sync_enabled=mutations_enabled
                    and (sync_required or vault_state not in {"current", "synced"}),
                    disable_enabled=mutations_enabled,
                )
            )
        self._rows = tuple(rows)
        self.stale = stale

    def fail_closed(self) -> None:
        self._rows = ()
        self.stale = True

    def __repr__(self) -> str:
        return f"{type(self).__name__}(rows={self._rows!r}, stale={self.stale!r})"


@dataclass(frozen=True, slots=True)
class CommandResult:
    ok: bool
    payload: object
    code: str
    step_up_retry_safe: bool = False


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

        thread = threading.Thread(target=worker, name="codex-usage-control", daemon=True)
        try:
            thread.start()
        except BaseException:
            if secret is not None:
                secret[:] = b"\x00" * len(secret)
                secret.clear()
            raise

    def _run(self, argv: tuple[str, ...], stdin_data: bytearray | None) -> CommandResult:
        process = None
        process_group = None
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
            process_group = process.pid
            if process.stdin is not None:
                process.stdin.write(stdin_data or b"")
                process.stdin.close()
            deadline = time.monotonic() + self._timeout
            stream = process.stdout
            while stream is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                ready, _, _ = select.select([stream], [], [], min(remaining, 0.05))
                if not ready:
                    if process.poll() is not None:
                        break
                    continue
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
            retry_safe = (
                payload.get("step_up_retry_safe") is True
                if isinstance(payload, dict)
                else False
            )
            return CommandResult(False, None, _redacted_code(code), retry_safe)
        except (OSError, UnicodeError, ValueError, TimeoutError, subprocess.TimeoutExpired):
            return CommandResult(False, None, "control.transport_unavailable")
        finally:
            if process is not None and process_group is not None:
                _terminate_process_group(process, process_group)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(timeout={self._timeout!r}, max_output={self._max_output!r})"


def _safe_environment() -> dict[str, str]:
    allowed = (
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_RUNTIME_DIR",
        "CREDENTIALS_DIRECTORY",
    )
    result = {name: os.environ[name] for name in allowed if name in os.environ}
    credential_directory = result.get("CREDENTIALS_DIRECTORY")
    if credential_directory is not None and (
        not Path(credential_directory).is_absolute() or "\x00" in credential_directory
    ):
        result.pop("CREDENTIALS_DIRECTORY")
    return result


def _redacted_code(value: object) -> str:
    try:
        return _code(value, "code")
    except ValueError:
        return "control.transport_unavailable"


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(process: subprocess.Popen[bytes], process_group: int) -> None:
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 0.1
    while _process_group_exists(process_group) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _process_group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=0.5)
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()


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
    del setter
    kind = _text(transport, "transport", maximum=16)
    validate_masterjet_endpoint(kind, endpoint)
    raise RuntimeError("nur kanonische Codex-Usage-Konfiguration ist schreibbar")


def default_masterjet_socket(
    *, environ: Mapping[str, str] | None = None, uid: int | None = None
) -> str:
    values = os.environ if environ is None else environ
    current_uid = os.getuid() if uid is None else uid
    runtime_dir = values.get("XDG_RUNTIME_DIR")
    if not runtime_dir or not Path(runtime_dir).is_absolute():
        runtime_dir = f"/run/user/{current_uid}"
    return str(Path(runtime_dir) / "masterjet.sock")


class OpenAIActions:
    __slots__ = ("_executable", "_projection_ready", "_reauth_runner", "_runner")

    def __init__(
        self,
        runner: BoundedJsonRunner,
        *,
        reauth_runner: BoundedJsonRunner | None = None,
        executable: str | None = None,
    ) -> None:
        self._runner = runner
        self._reauth_runner = reauth_runner or runner
        self._executable = executable or str(Path.home() / ".local/bin/codex-usage")
        self._projection_ready = False

    @property
    def projection_ready(self) -> bool:
        return self._projection_ready

    def set_projection_ready(self, ready: bool) -> None:
        self._projection_ready = _boolean(ready, "projection_ready")

    def _require_projection(self) -> None:
        if not self._projection_ready:
            raise RuntimeError("STALE")

    def refresh(self, *, callback=None) -> None:
        self._projection_ready = False
        self._runner.submit(
            [self._executable, "masterjet", "openai-accounts", "--json"],
            callback=callback,
        )

    def reauthenticate(self, account_ref: str, *, callback=None) -> None:
        account = _text(account_ref, "account")
        self._require_projection()
        self._reauth_runner.submit(
            [
                self._executable,
                "reactivate",
                account,
                "--format",
                "json",
            ],
            callback=callback,
        )

    def sync_auth(self, account_ref: str, *, callback=None) -> None:
        account = _text(account_ref, "account")
        self._require_projection()
        self._runner.submit(
            [
                self._executable,
                "account",
                "auth-sync",
                account,
                "--format",
                "json",
            ],
            callback=callback,
        )

    def with_step_up(self, argv: list[str], provider: Callable[[], object], *, callback=None):
        if not argv or argv[0] != self._executable:
            raise ValueError("step-up command must use the configured Codex Usage CLI")
        self._require_projection()
        value = provider()
        if not isinstance(value, str) or not value.isascii() or not value.isdigit():
            raise ValueError("invalid step-up code")
        if len(value) not in {6, 7, 8}:
            raise ValueError("invalid step-up code")
        secret = bytearray(value.encode("ascii") + b"\n")
        try:
            self._runner.submit(
                [self._executable, "--step-up-stdin", *argv[1:]],
                stdin_data=secret,
                callback=callback,
            )
        finally:
            secret[:] = b"\x00" * len(secret)
            secret.clear()

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class MasterjetConnectionActions:
    __slots__ = ("_executable", "_runner")

    def __init__(self, runner: BoundedJsonRunner, *, executable: str | None = None) -> None:
        self._runner = runner
        self._executable = executable or str(Path.home() / ".local/bin/codex-usage")

    def test(self, *, callback=None) -> None:
        self._runner.submit(
            [self._executable, "masterjet", "connection-test", "--json"],
            callback=callback,
        )

    def show(self, *, callback=None) -> None:
        self._runner.submit(
            [self._executable, "masterjet", "connection-show", "--json"],
            callback=callback,
        )

    def set(
        self,
        transport: object,
        endpoint: object,
        timeout_seconds: object,
        *,
        callback=None,
    ) -> None:
        kind = _text(transport, "transport", maximum=16)
        value = validate_masterjet_endpoint(kind, endpoint)
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
            raise ValueError("invalid timeout_seconds")
        self._runner.submit(
            [
                self._executable,
                "masterjet",
                "connection-set",
                "--transport",
                kind,
                "--endpoint",
                value,
                "--timeout-seconds",
                str(timeout_seconds),
                "--json",
            ],
            callback=callback,
        )


class MasterjetConnectionWidget(SettingsWidget):
    bind_dir = None

    def __init__(self, info, key, settings):
        del info, key, settings
        SettingsWidget.__init__(self)
        self.set_orientation(Gtk.Orientation.VERTICAL)
        self.set_spacing(6)
        self._actions = MasterjetConnectionActions(BoundedJsonRunner())
        authority = Gtk.Label(
            label=(
                "Kanonische Verbindung: ~/.config/codex-usage/config.toml · "
                f"Runtime-Default {default_masterjet_socket()}"
            )
        )
        authority.set_xalign(0.0)
        authority.set_line_wrap(True)
        form = Gtk.Grid(column_spacing=6, row_spacing=6)
        self.transport = Gtk.ComboBoxText()
        self.transport.append("local", "Lokaler Socket")
        self.transport.append("https", "Remote HTTPS")
        self.transport.set_active_id("local")
        self.endpoint = Gtk.Entry()
        self.endpoint.set_placeholder_text(default_masterjet_socket())
        self.timeout = Gtk.SpinButton.new_with_range(1, 300, 1)
        self.timeout.set_value(10)
        form.attach(Gtk.Label(label="Transport"), 0, 0, 1, 1)
        form.attach(self.transport, 1, 0, 1, 1)
        form.attach(Gtk.Label(label="Socket-/HTTPS-Endpoint"), 0, 1, 1, 1)
        form.attach(self.endpoint, 1, 1, 1, 1)
        form.attach(Gtk.Label(label="Timeout (s)"), 0, 2, 1, 1)
        form.attach(self.timeout, 1, 2, 1, 1)
        self.status = Gtk.Label(label="Kanonische Verbindung nicht getestet")
        self.status.set_xalign(0.0)
        save = Gtk.Button(label="Kanonische Verbindung speichern")
        save.connect("clicked", self._save)
        test = Gtk.Button(label="Kanonische Verbindung testen")
        test.connect("clicked", self._test)
        self.pack_start(authority, False, False, 0)
        self.pack_start(form, False, False, 0)
        self.pack_start(save, False, False, 0)
        self.pack_start(test, False, False, 0)
        self.pack_start(self.status, False, False, 0)
        self.show_all()
        self._actions.show(callback=self._shown)

    def _shown(self, result: CommandResult) -> bool:
        if not result.ok or not isinstance(result.payload, Mapping):
            self.status.set_text(f"Fehler: {result.code}")
            return False
        connection = result.payload.get("connection")
        if not isinstance(connection, Mapping) or set(connection) != {
            "transport",
            "endpoint",
            "timeout_seconds",
        }:
            self.status.set_text("Fehler: control.response_invalid")
            return False
        try:
            kind = _text(connection["transport"], "transport", maximum=16)
            endpoint = connection["endpoint"]
            if not isinstance(endpoint, str) or len(endpoint) > 2048 or "\x00" in endpoint:
                raise ValueError("invalid endpoint")
            timeout = connection["timeout_seconds"]
            if type(timeout) is not int or not 1 <= timeout <= 300:
                raise ValueError("invalid timeout")
            if endpoint:
                validate_masterjet_endpoint(kind, endpoint)
            elif kind != "local":
                raise ValueError("invalid endpoint")
        except ValueError:
            self.status.set_text("Fehler: control.response_invalid")
            return False
        self.transport.set_active_id(kind)
        self.endpoint.set_text(endpoint)
        self.timeout.set_value(timeout)
        self.status.set_text("Kanonische Verbindung geladen")
        return False

    def _save(self, *_args) -> None:
        try:
            self._actions.set(
                self.transport.get_active_id(),
                self.endpoint.get_text(),
                self.timeout.get_value_as_int(),
                callback=self._saved,
            )
        except ValueError:
            self.status.set_text("Ungültiger Endpoint · nicht gespeichert")

    def _saved(self, result: CommandResult) -> bool:
        self.status.set_text("Gespeichert" if result.ok else f"Fehler: {result.code}")
        return False

    def _test(self, *_args) -> None:
        self.status.set_text("Prüfung läuft …")
        self._actions.test(callback=self._tested)

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
        self._actions = OpenAIActions(
            BoundedJsonRunner(),
            reauth_runner=BoundedJsonRunner(timeout_seconds=REAUTH_TIMEOUT_SECONDS),
        )
        self._status = Gtk.Label(label="OpenAI-Control nicht geladen")
        self._status.set_xalign(0.0)
        self.pack_start(self._status, False, False, 0)
        refresh = Gtk.Button(label="OpenAI-Accounts laden")
        refresh.connect("clicked", self._refresh)
        self.pack_start(refresh, False, False, 0)
        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.pack_start(self._body, True, True, 0)
        self.show_all()

    def _refresh(self, *_args) -> None:
        self._revoke_projection()
        self._status.set_text("OpenAI-Control wird geladen …")
        self._actions.refresh(callback=self._loaded)

    def _revoke_projection(self) -> None:
        self.model.fail_closed()
        self._actions.set_projection_ready(False)
        for child in self._body.get_children():
            self._body.remove(child)
            child.destroy()

    def _loaded(self, result: CommandResult) -> bool:
        if not result.ok or not isinstance(result.payload, Mapping):
            self._revoke_projection()
            self._status.set_text(f"STALE · {result.code} · Mutationen gesperrt")
            return False
        payload = result.payload
        if set(payload) != {"local_accounts", "accounts", "stale"}:
            self._revoke_projection()
            self._status.set_text("Ungültige Control-Antwort · Mutationen gesperrt")
            return False
        try:
            self.render(
                payload["local_accounts"],
                payload["accounts"],
                stale=payload["stale"],
            )
        except (TypeError, ValueError):
            self._revoke_projection()
            self._status.set_text("Ungültige Control-Antwort · Mutationen gesperrt")
            return False
        self._status.set_text("STALE · Mutationen gesperrt" if self.model.stale else "Aktuell")
        return False

    def render(self, local_accounts, masterjet_accounts, *, stale=False) -> None:
        self.model.render(local_accounts, masterjet_accounts, stale=stale)
        self._actions.set_projection_ready(not self.model.stale)
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
        self._status.set_text("Re-Auth läuft …")
        try:
            self._actions.reauthenticate(account_ref, callback=self._operation_finished)
        except RuntimeError:
            self._status.set_text("STALE · Mutationen gesperrt")

    def _sync_auth(self, _button, account_ref: str) -> None:
        self._status.set_text("Auth-Sync läuft …")
        try:
            argv = [
                self._actions._executable,
                "account",
                "auth-sync",
                account_ref,
                "--format",
                "json",
            ]
            self._actions.sync_auth(
                account_ref,
                callback=lambda result: self._operation_finished(result, argv=argv),
            )
        except RuntimeError:
            self._status.set_text("STALE · Mutationen gesperrt")

    def _operation_finished(self, result: CommandResult, *, argv=None, retried=False) -> bool:
        if (
            not retried
            and argv is not None
            and not result.ok
            and result.code == "control.step_up_required"
            and result.step_up_retry_safe is True
            and self._actions.projection_ready
        ):
            code = self.prompt_step_up()
            if code is not None:
                try:
                    self._actions.with_step_up(
                        argv,
                        lambda: code,
                        callback=lambda retry: self._operation_finished(
                            retry, argv=argv, retried=True
                        ),
                    )
                    return False
                except (RuntimeError, ValueError):
                    pass
        self._status.set_text("Operation abgeschlossen" if result.ok else f"Fehler: {result.code}")
        return False

    def prompt_step_up(self) -> str | None:
        dialog = Gtk.Dialog(
            title="TOTP-Step-up",
            transient_for=self.get_toplevel()
            if isinstance(self.get_toplevel(), Gtk.Window)
            else None,
            flags=Gtk.DialogFlags.MODAL,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK,
            Gtk.ResponseType.OK,
        )
        entry = Gtk.Entry()
        entry.set_visibility(False)
        entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
        dialog.get_content_area().pack_start(entry, False, False, 8)
        dialog.show_all()
        try:
            return entry.get_text() if dialog.run() == Gtk.ResponseType.OK else None
        finally:
            entry.set_text("")
            dialog.destroy()

    def row(self, account_ref: str) -> OpenAIAccountRow:
        return self.model.row(account_ref)


__all__ = [
    "REAUTH_TIMEOUT_SECONDS",
    "BoundedJsonRunner",
    "CommandResult",
    "MasterjetConnectionActions",
    "MasterjetConnectionWidget",
    "OpenAIAccountRow",
    "OpenAIAccountsModel",
    "OpenAIAccountsPage",
    "OpenAIActions",
    "default_masterjet_socket",
    "save_masterjet_connection",
    "validate_masterjet_endpoint",
]
