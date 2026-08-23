from __future__ import annotations

import codecs
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, cast

from .account_lock import account_lock
from .config import _validate_account_id, add_or_update_account, load_config
from .direct import (
    MAX_AUTH_JSON_BYTES,
    DirectAuthError,
    _extract_auth_details,
    auth_identity_from_payload,
)
from .json_utils import loads_strict
from .models import Account
from .private_io import (
    assert_no_symlink_ancestors,
    ensure_private_directory,
    read_private_text,
    write_private_text,
)
from .profile_layout import ProfileLayout, ensure_profile_layout, layout_for_account

DEVICE_LOGIN_TIMEOUT_SECONDS = 15 * 60
DEVICE_OUTPUT_MAX_BYTES = 64 * 1024
DEVICE_EVENT_MAX_CHARS = 512
ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
DEVICE_CODE_RE = re.compile(
    r"\b(?:"
    r"device[ \t]+code[ \t]*[:\uFF1A][ \t]*"
    r"|one-time[ \t]+code(?:[ \t]*\([^\r\n)]{0,80}\))?"
    r"[ \t]*(?:[:\uFF1A][ \t]*|\r?\n[ \t]*)"
    r")([A-Za-z0-9][A-Za-z0-9_-]{3,127})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
DEVICE_URL_RE = re.compile(
    r"https://[^\s\x00-\x1f\x7f-\x9f<>\"']{1,481}",
    re.IGNORECASE,
)
_SAFE_ENV_NAMES = frozenset(
    {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LANGUAGE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)


class DeviceLoginError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceLoginEvent:
    kind: str
    value: str


@dataclass(frozen=True)
class DeviceLoginResult:
    ok: bool
    account_id: str
    events: tuple[DeviceLoginEvent, ...] = ()
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "account": self.account_id,
            "events": [{"kind": event.kind, "value": event.value} for event in self.events],
            "error": self.error,
        }


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
OutputStreamSink = Callable[[str, str], None]


def _validate_codex_command(codex_bin: object) -> None:
    if not isinstance(codex_bin, str) or not codex_bin or any(
        ord(character) < 32 or ord(character) == 127 for character in codex_bin
    ):
        raise DeviceLoginError("codex command is invalid")
    try:
        codex_bin.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DeviceLoginError("codex command is invalid") from exc


def device_auth_supported(
    codex_bin: str = "codex",
    *,
    runner: CommandRunner | None = None,
    start_new_session: bool = True,
) -> bool:
    """Detect the explicit device-auth flag without starting a login."""

    _validate_codex_command(codex_bin)
    result = _run_command(
        [codex_bin, "login", "--help"],
        env=_safe_environment(),
        timeout=20,
        runner=runner,
        start_new_session=start_new_session,
    )
    output = _bounded_output(result.stdout, result.stderr)
    return result.returncode == 0 and "--device-auth" in output


def run_device_login(
    account: Account,
    config_path: Path,
    *,
    codex_bin: str = "codex",
    timeout_seconds: int = DEVICE_LOGIN_TIMEOUT_SECONDS,
    runner: CommandRunner | None = None,
    event_sink: Callable[[DeviceLoginEvent], None] | None = None,
    expected_backend_account_id: str | None = None,
    isolate_process_group: bool = True,
) -> DeviceLoginResult:
    """Run device auth in a private staging CODEX_HOME and publish on success."""

    if not isinstance(account, Account):
        raise DeviceLoginError("account is invalid")
    try:
        _validate_account_id(account.id)
    except ValueError as exc:
        raise DeviceLoginError("account id is invalid") from exc
    _validate_codex_command(codex_bin)
    if not isinstance(config_path, Path) or not config_path.is_absolute():
        raise DeviceLoginError("config path must be absolute")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise DeviceLoginError("device login timeout is invalid")
    if expected_backend_account_id is not None and (
        not isinstance(expected_backend_account_id, str)
        or not expected_backend_account_id
        or len(expected_backend_account_id) > 256
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in expected_backend_account_id
        )
    ):
        raise DeviceLoginError("expected backend account id is invalid")
    if not isinstance(isolate_process_group, bool):
        raise DeviceLoginError("device login process isolation is invalid")
    layout = layout_for_account(account)
    login_events: tuple[DeviceLoginEvent, ...] = ()
    observed_events: set[tuple[str, str]] = set()
    observed_output = {"stdout": "", "stderr": ""}

    def emit_event(event: DeviceLoginEvent) -> None:
        identity = (event.kind, event.value)
        if identity in observed_events:
            return
        observed_events.add(identity)
        if event_sink is not None:
            event_sink(event)

    def observe_output(stream_name: str, chunk: str) -> None:
        observed_output[stream_name] = (
            observed_output[stream_name] + chunk
        )[-DEVICE_OUTPUT_MAX_BYTES:]
        for event in _device_events(observed_output[stream_name], final=False):
            emit_event(event)

    with account_lock(account.id):
        if not device_auth_supported(
            codex_bin,
            runner=runner,
            start_new_session=isolate_process_group,
        ):
            return DeviceLoginResult(False, account.id, error="device_auth_unavailable")
        ensure_profile_layout(account)
        staging_root = _create_staging_root(layout)
        published_auth = False
        canonical_auth_owned = False
        staging_cleaned = False
        primary_result_active = False
        try:
            staging_home = staging_root / "codex-home"
            staging_home.mkdir(mode=0o700)
            _write_file_store_config(staging_home / "config.toml")
            result = _run_command(
                [codex_bin, "login", "--device-auth"],
                env=_safe_environment(staging_home),
                timeout=timeout_seconds,
                runner=runner,
                start_new_session=isolate_process_group,
                output_stream_sink=observe_output if event_sink is not None else None,
            )
            events = _device_events(
                _bounded_output(result.stdout, result.stderr), final=True
            )
            login_events = events
            for event in events:
                emit_event(event)
            if result.returncode != 0:
                primary_result_active = True
                return DeviceLoginResult(False, account.id, events, "device_login_failed")
            staged_auth = staging_home / "auth.json"
            if not staged_auth.is_file() or staged_auth.is_symlink():
                primary_result_active = True
                return DeviceLoginResult(False, account.id, events, "device_auth_missing")
            _validate_staged_auth(
                staged_auth,
                expected_backend_account_id=expected_backend_account_id,
            )
            if layout.auth_json.exists():
                raise DeviceLoginError("canonical auth.json already exists")
            _copy_private_file(
                staged_auth,
                layout.auth_json,
                expected_backend_account_id=expected_backend_account_id,
            )
            canonical_auth_owned = True
            _write_file_store_config(layout.codex_home / "config.toml")
            try:
                shutil.rmtree(staging_root, ignore_errors=False)
            except OSError as exc:
                raise DeviceLoginError("device_login_cleanup_failed") from exc
            staging_cleaned = True
            published_auth = True
        except subprocess.TimeoutExpired as exc:
            raise DeviceLoginError("device_login_timeout") from exc
        except (OSError, ValueError) as exc:
            raise DeviceLoginError("device_login_io_failed") from exc
        finally:
            primary_error_active = (
                sys.exc_info()[0] is not None or primary_result_active
            )
            if canonical_auth_owned and not published_auth:
                try:
                    layout.auth_json.unlink(missing_ok=True)
                except OSError:
                    pass
            if not staging_cleaned:
                try:
                    shutil.rmtree(staging_root, ignore_errors=False)
                    staging_cleaned = True
                except OSError as exc:
                    if not primary_error_active:
                        raise DeviceLoginError("device_login_cleanup_failed") from exc

    try:
        with account_lock("__all_accounts__"):
            if config_path.exists():
                current_config = load_config(config_path)
                current_account = next(
                    (item for item in current_config.accounts if item.id == account.id),
                    None,
                )
                if current_account != account:
                    raise DeviceLoginError("device_login_account_changed")
            add_or_update_account(
                account.id,
                label=account.label,
                profile_dir=account.profile_dir,
                browser=account.browser,
                backend=account.backend,
                reactivation_browser=account.reactivation_browser,
                auth_json_path=str(layout.auth_json),
                path=config_path,
                _all_accounts_lock_held=True,
            )
    except Exception as exc:
        try:
            layout.auth_json.unlink(missing_ok=True)
        except OSError:
            pass
        raise DeviceLoginError("device_login_finalize_failed") from exc
    return DeviceLoginResult(True, account.id, login_events)


def _run_command(
    argv: list[str],
    *,
    env: Mapping[str, str],
    timeout: int,
    runner: CommandRunner | None,
    start_new_session: bool = True,
    output_sink: Callable[[str], None] | None = None,
    output_stream_sink: OutputStreamSink | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        if runner is not None:
            runner_kwargs: dict[str, object] = {"env": env, "timeout": timeout}
            if output_stream_sink is not None:
                runner_kwargs["output_stream_sink"] = output_stream_sink
            result = runner(argv, **runner_kwargs)
        else:
            result = _run_subprocess_bounded(
                argv,
                env=dict(env),
                timeout=timeout,
                start_new_session=start_new_session,
                output_sink=output_sink,
                output_stream_sink=output_stream_sink,
            )
    except subprocess.TimeoutExpired as exc:
        raise DeviceLoginError("device_login_timeout") from exc
    except OSError as exc:
        raise DeviceLoginError("device_login_process_failed") from exc
    if not isinstance(result, subprocess.CompletedProcess):
        raise DeviceLoginError("device_login_runner_invalid")
    return result


def _run_subprocess_bounded(
    argv: list[str],
    *,
    env: Mapping[str, str],
    timeout: int,
    start_new_session: bool = True,
    output_sink: Callable[[str], None] | None = None,
    output_stream_sink: OutputStreamSink | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        argv,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=start_new_session,
    )
    streams: dict[IO[bytes], str] = {}
    if process.stdout is not None:
        streams[process.stdout] = "stdout"
    if process.stderr is not None:
        streams[process.stderr] = "stderr"
    selector = selectors.DefaultSelector()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    decoders = {
        name: codecs.getincrementaldecoder("utf-8")("replace")
        for name in streams.values()
    }
    total = 0
    deadline = time.monotonic() + timeout
    try:
        for stream in streams:
            if stream is not None:
                selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_bounded_process(process, start_new_session=start_new_session)
                raise subprocess.TimeoutExpired(argv, timeout)
            ready = selector.select(remaining)
            if not ready:
                _terminate_bounded_process(process, start_new_session=start_new_session)
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in ready:
                stream = cast(IO[bytes], key.fileobj)
                chunk = os.read(stream.fileno(), 8192)
                if not chunk:
                    if output_stream_sink is not None or output_sink is not None:
                        decoded = decoders[streams[stream]].decode(b"", final=True)
                        if decoded:
                            if output_stream_sink is not None:
                                output_stream_sink(streams[stream], decoded)
                            else:
                                assert output_sink is not None
                                output_sink(decoded)
                    selector.unregister(stream)
                    continue
                total += len(chunk)
                if total > DEVICE_OUTPUT_MAX_BYTES:
                    _terminate_bounded_process(process, start_new_session=start_new_session)
                    raise DeviceLoginError("device_login_output_too_large")
                if output_stream_sink is not None or output_sink is not None:
                    decoded = decoders[streams[stream]].decode(chunk, final=False)
                    if decoded:
                        if output_stream_sink is not None:
                            output_stream_sink(streams[stream], decoded)
                        else:
                            assert output_sink is not None
                            output_sink(decoded)
                output[streams[stream]].extend(chunk)
        try:
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _terminate_bounded_process(process, start_new_session=start_new_session)
            raise
        return subprocess.CompletedProcess(
            argv,
            returncode,
            bytes(output["stdout"]).decode("utf-8", "replace"),
            bytes(output["stderr"]).decode("utf-8", "replace"),
        )
    except BaseException:
        _terminate_bounded_process(process, start_new_session=start_new_session)
        raise
    finally:
        selector.close()
        for stream in streams:
            if stream is not None:
                stream.close()


def _terminate_bounded_process(
    process: subprocess.Popen[bytes], *, start_new_session: bool = True
) -> None:
    pid = getattr(process, "pid", None)
    signaled_group = False
    if (
        start_new_session
        and type(pid) is int
        and pid > 0
    ):
        try:
            os.killpg(pid, signal.SIGKILL)
            signaled_group = True
        except (OSError, ValueError):
            pass
    if not signaled_group:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _safe_environment(codex_home: Path | None = None) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key in _SAFE_ENV_NAMES}
    if codex_home is not None:
        environment["CODEX_HOME"] = str(codex_home)
    else:
        environment.pop("CODEX_HOME", None)
    return environment


def _bounded_output(stdout: object, stderr: object) -> str:
    values = [value for value in (stdout, stderr) if isinstance(value, str)]
    text = "\n".join(values)
    return text[:DEVICE_OUTPUT_MAX_BYTES]


def _device_events(output: str, *, final: bool = True) -> tuple[DeviceLoginEvent, ...]:
    events: list[DeviceLoginEvent] = []
    seen: set[tuple[str, str]] = set()
    cleaned = ANSI_CSI_RE.sub("", output)
    for match in DEVICE_URL_RE.finditer(cleaned):
        if not final and match.end() == len(cleaned):
            continue
        value = match.group(0).rstrip(".,)")[:DEVICE_EVENT_MAX_CHARS]
        if len(value) > len("https://") + 480:
            continue
        event = ("url", value)
        if event not in seen:
            seen.add(event)
            events.append(DeviceLoginEvent(*event))
            if len(events) >= 8:
                break
    for match in DEVICE_CODE_RE.finditer(cleaned):
        if len(events) >= 8:
            break
        if not final and match.end() == len(cleaned):
            continue
        event = ("code", match.group(1)[:DEVICE_EVENT_MAX_CHARS])
        if event not in seen:
            seen.add(event)
            events.append(DeviceLoginEvent(*event))
    return tuple(events)


def _create_staging_root(layout: ProfileLayout) -> Path:
    assert_no_symlink_ancestors(layout.profile_dir, label="device login profile")
    staging_parent = layout.profile_dir / ".device-login-staging"
    try:
        ensure_private_directory(staging_parent, label="device login staging directory")
    except ValueError as exc:
        raise DeviceLoginError("device login staging path is invalid") from exc
    return Path(tempfile.mkdtemp(prefix="job-", dir=staging_parent))


def _write_file_store_config(path: Path) -> None:
    write_private_text(path, 'cli_auth_credentials_store = "file"\n', label="device login config")


def _copy_private_file(
    source: Path,
    target: Path,
    *,
    expected_backend_account_id: str | None = None,
) -> None:
    assert_no_symlink_ancestors(source, label="staged auth")
    text, file_stat = read_private_text(
        source,
        regular_label="staged auth",
        read_label="staged auth",
        max_bytes=MAX_AUTH_JSON_BYTES,
    )
    if file_stat.st_nlink != 1 or file_stat.st_mode & 0o077:
        raise DeviceLoginError("device_auth_invalid")
    _validate_staged_auth_payload(
        text,
        source,
        expected_backend_account_id=expected_backend_account_id,
    )
    write_private_text(
        target,
        text,
        label="canonical auth.json",
        replace_existing=False,
    )


def _validate_staged_auth(
    path: Path,
    *,
    expected_backend_account_id: str | None = None,
) -> None:
    text, file_stat = read_private_text(
        path,
        regular_label="staged auth",
        read_label="staged auth",
        max_bytes=MAX_AUTH_JSON_BYTES,
    )
    if file_stat.st_nlink != 1 or file_stat.st_mode & 0o077:
        raise DeviceLoginError("device_auth_invalid")
    _validate_staged_auth_payload(
        text,
        path,
        expected_backend_account_id=expected_backend_account_id,
    )


def _validate_staged_auth_payload(
    text: str,
    path: Path,
    *,
    expected_backend_account_id: str | None,
) -> None:
    try:
        payload = loads_strict(text)
    except ValueError as exc:
        raise DeviceLoginError("device_auth_invalid") from exc
    if not isinstance(payload, dict):
        raise DeviceLoginError("device_auth_invalid")
    try:
        _extract_auth_details(payload, path=path)
    except DirectAuthError as exc:
        raise DeviceLoginError("device_auth_invalid") from exc
    if expected_backend_account_id is not None:
        try:
            _, account_id = auth_identity_from_payload(payload, path=path)
        except DirectAuthError as exc:
            raise DeviceLoginError("device_auth_identity_invalid") from exc
        if account_id != expected_backend_account_id:
            raise DeviceLoginError("device_auth_identity_mismatch")
