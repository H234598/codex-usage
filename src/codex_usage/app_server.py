from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .direct import (
    DirectAuthError,
    _auth_plan_type_changed,
    _normalized_plan_type,
    auth_identity_changed,
    auth_identity_from_payload,
    auth_metadata_from_payload,
    auth_plan_type_from_payload,
    read_auth_json_file,
)
from .json_utils import loads_strict
from .models import Account, AccountStatus, AccountUsage, LimitWindow

APP_SERVER_BACKEND = "app-server"
APP_SERVER_TIMEOUT_SECONDS = 30
FIVE_HOUR_WINDOW_MINUTES = 300
WEEKLY_WINDOW_MINUTES = 10_080
APP_SERVER_MAX_LINE_BYTES = 2_000_000
APP_SERVER_MAX_MESSAGES = 100
APP_SERVER_STDERR_BYTES = 4096
TOKEN_REFRESH_WINDOW_SECONDS = 15 * 60


class AppServerError(Exception):
    pass


class AppServerUnavailableError(AppServerError):
    pass


class AppServerProtocolError(AppServerError):
    pass


class AppServerAuthError(AppServerError):
    pass


class AppServerFetchError(AppServerError):
    pass


def fetch_account_usage_app_server(
    account: Account,
    *,
    timeout_seconds: int = APP_SERVER_TIMEOUT_SECONDS,
    codex_command: str | None = None,
) -> AccountUsage:
    captured_at = datetime.now().astimezone()
    try:
        (
            auth_path,
            auth_metadata,
            auth_user_id_before,
            auth_account_id_before,
            auth_plan_type_before,
        ) = _auth_context(account)
        if not (auth_user_id_before or auth_account_id_before):
            raise DirectAuthError("auth.json has no account identity")
        refresh = _should_refresh(auth_metadata.get("auth_access_expires_at"), now=captured_at)
        payload = _read_rate_limits(
            auth_path.parent,
            refresh=refresh,
            timeout_seconds=timeout_seconds,
            codex_command=codex_command,
            expected_plan_type=auth_plan_type_before,
        )
        (
            _,
            auth_metadata,
            auth_user_id,
            auth_account_id,
            auth_plan_type,
        ) = _auth_context(account)
        if auth_identity_changed(
            before_user_id=auth_user_id_before,
            before_account_id=auth_account_id_before,
            after_user_id=auth_user_id,
            after_account_id=auth_account_id,
        ):
            raise AppServerAuthError("auth.json identity changed during rate-limit request")
        if _auth_plan_type_changed(auth_plan_type_before, auth_plan_type):
            raise AppServerAuthError("auth.json plan type changed during rate-limit request")
        five_hour, weekly = _windows_from_response(payload)
        status = (
            AccountStatus.OK
            if five_hour is not None
            and weekly is not None
            and five_hour.has_usage_value
            and weekly.has_usage_value
            else AccountStatus.PARTIAL
        )
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            five_hour=five_hour,
            weekly=weekly,
            status=status,
            error=(
                None
                if status == AccountStatus.OK
                else "usage limits not found in app server response"
            ),
            auth_last_refresh=auth_metadata.get("auth_last_refresh"),
            auth_access_expires_at=auth_metadata.get("auth_access_expires_at"),
            auth_id_expires_at=auth_metadata.get("auth_id_expires_at"),
            backend_configured=account.backend,
            backend_used=APP_SERVER_BACKEND,
            backend_user_id=auth_user_id,
            backend_account_id=auth_account_id,
        )
    except (DirectAuthError, AppServerAuthError) as exc:
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            status=AccountStatus.LOGIN_REQUIRED,
            error=_bounded_error(exc),
            backend_configured=account.backend,
            backend_used=APP_SERVER_BACKEND,
        )
    except AppServerUnavailableError:
        raise
    except AppServerError as exc:
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            status=AccountStatus.ERROR,
            error=_bounded_error(exc),
            backend_configured=account.backend,
            backend_used=APP_SERVER_BACKEND,
        )


def _auth_context(
    account: Account,
) -> tuple[
    Path,
    dict[str, datetime | None],
    str | None,
    str | None,
    str | None,
]:
    if not account.auth_json_path:
        raise DirectAuthError("account has no auth_json_path")
    path = Path(account.auth_json_path).expanduser()
    if path.name != "auth.json":
        raise DirectAuthError("app-server requires auth_json_path filename auth.json")
    raw, _ = read_auth_json_file(path)
    try:
        payload = loads_strict(raw)
    except ValueError as exc:
        raise DirectAuthError("invalid auth.json") from exc
    if not isinstance(payload, dict):
        raise DirectAuthError("invalid auth.json structure")
    auth_user_id, auth_account_id = auth_identity_from_payload(payload, path=path)
    return (
        path,
        auth_metadata_from_payload(payload),
        auth_user_id,
        auth_account_id,
        auth_plan_type_from_payload(payload, path=path),
    )


def _read_rate_limits(
    codex_home: Path,
    *,
    refresh: bool,
    timeout_seconds: int,
    codex_command: str | None,
    expected_plan_type: str | None,
) -> dict[str, Any]:
    _validate_codex_home(codex_home)
    command = _resolve_codex(codex_command)
    deadline = time.monotonic() + timeout_seconds
    process = _start_app_server(command, codex_home)
    reader = _LineReader(process.stdout)
    stderr_reader = _StderrReader(process.stderr)
    reader.start()
    stderr_reader.start()
    try:
        _send(
            process,
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "codex_usage",
                        "title": "codex-usage",
                        "version": __version__,
                    }
                },
            },
        )
        _response_for(reader, 1, deadline=deadline, stderr_reader=stderr_reader)
        _send(process, {"method": "initialized", "params": {}})
        def read_account(request_id: int, *, refresh_token: bool) -> None:
            _send(
                process,
                {
                    "method": "account/read",
                    "id": request_id,
                    "params": {"refreshToken": refresh_token},
                },
            )
            account_result = _response_for(
                reader,
                request_id,
                deadline=deadline,
                stderr_reader=stderr_reader,
            )
            account = account_result.get("account")
            if not isinstance(account, dict) or account.get("type") != "chatgpt":
                raise AppServerAuthError("Codex app server requires ChatGPT login")
            server_plan_type = account.get("planType")
            if expected_plan_type and server_plan_type is not None:
                if (
                    not isinstance(server_plan_type, str)
                    or _normalized_plan_type(server_plan_type)
                    != _normalized_plan_type(expected_plan_type)
                ):
                    raise AppServerAuthError(
                        "Codex app server plan type differs from auth.json"
                    )

        try:
            read_account(2, refresh_token=refresh)
            return _request_rate_limits(
                process,
                reader,
                request_id=3,
                deadline=deadline,
                stderr_reader=stderr_reader,
            )
        except AppServerAuthError:
            if refresh:
                raise
            read_account(4, refresh_token=True)
            return _request_rate_limits(
                process,
                reader,
                request_id=5,
                deadline=deadline,
                stderr_reader=stderr_reader,
            )
    finally:
        _stop_process(process)


def _request_rate_limits(
    process: subprocess.Popen[bytes],
    reader: _LineReader,
    *,
    request_id: int,
    deadline: float,
    stderr_reader: _StderrReader,
) -> dict[str, Any]:
    _send(process, {"method": "account/rateLimits/read", "id": request_id})
    response = _response_for(
        reader,
        request_id,
        deadline=deadline,
        stderr_reader=stderr_reader,
    )
    if not isinstance(response, dict):
        raise AppServerProtocolError("app server rate-limit result is not an object")
    return response


def _resolve_codex(explicit: str | None) -> str:
    value = explicit or shutil.which("codex")
    if not value:
        raise AppServerUnavailableError("codex command was not found")
    path = Path(value).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AppServerUnavailableError("codex command is not executable")
    return str(path)


def _start_app_server(command: str, codex_home: Path) -> subprocess.Popen[bytes]:
    _validate_codex_home(codex_home)
    env = _app_server_environment(codex_home)
    try:
        return subprocess.Popen(
            [command, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise AppServerUnavailableError("could not start codex app server") from exc


def _validate_codex_home(codex_home: Path) -> None:
    _assert_no_symlink_ancestors(codex_home)
    if codex_home.is_symlink() or not codex_home.is_dir():
        raise AppServerAuthError("CODEX_HOME must be a real directory")


def _assert_no_symlink_ancestors(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise AppServerAuthError("CODEX_HOME must not contain symlinks")
        if not current.exists():
            break


def _app_server_environment(codex_home: Path) -> dict[str, str]:
    names = {
        "HOME",
        "PATH",
        "LANG",
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
    env = {
        key: value
        for key, value in os.environ.items()
        if key in names or key.startswith("LC_")
    }
    env["CODEX_HOME"] = str(codex_home)
    return env


def _send(process: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
    if process.stdin is None:
        raise AppServerProtocolError("app server stdin is unavailable")
    raw = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > 64_000:
        raise AppServerProtocolError("app server request is too large")
    try:
        process.stdin.write(raw)
        process.stdin.flush()
    except OSError as exc:
        raise AppServerProtocolError("could not write to codex app server") from exc


def _response_for(
    reader: _LineReader,
    request_id: int,
    *,
    deadline: float,
    stderr_reader: _StderrReader,
) -> dict[str, Any]:
    for _ in range(APP_SERVER_MAX_MESSAGES):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppServerFetchError("codex app server timed out")
        try:
            item = reader.items.get(timeout=remaining)
        except queue.Empty as exc:
            raise AppServerFetchError("codex app server timed out") from exc
        if isinstance(item, Exception):
            if isinstance(item, EOFError):
                raise AppServerUnavailableError("codex app server exited unexpectedly") from item
            raise AppServerProtocolError(str(item)) from item
        try:
            payload = loads_strict(item.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AppServerProtocolError("codex app server returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("id") != request_id:
            continue
        error = payload.get("error")
        if error is not None:
            _raise_rpc_error(error)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise AppServerProtocolError("codex app server result is not an object")
        return result
    raise AppServerProtocolError("too many codex app server messages")


def _raise_rpc_error(error: Any) -> None:
    if not isinstance(error, dict):
        raise AppServerProtocolError("codex app server returned an invalid error")
    code = error.get("code")
    message = " ".join(str(error.get("message") or "app server request failed").split())[:500]
    lower = message.lower()
    if code == -32601 or "method not found" in lower or "unknown method" in lower:
        raise AppServerUnavailableError("installed Codex does not support rate-limit RPC")
    if any(word in lower for word in ("auth", "login", "token", "unauthorized", "forbidden")):
        raise AppServerAuthError(message)
    raise AppServerFetchError(message)


def _windows_from_response(
    payload: dict[str, Any],
) -> tuple[LimitWindow | None, LimitWindow | None]:
    snapshot = payload.get("rateLimits")
    by_id = payload.get("rateLimitsByLimitId")
    codex_snapshot = by_id.get("codex") if isinstance(by_id, dict) else None
    has_codex_buckets = isinstance(codex_snapshot, dict) and any(
        isinstance(codex_snapshot.get(key), dict) for key in ("primary", "secondary")
    )
    if isinstance(snapshot, dict):
        snapshot = dict(snapshot)
    elif has_codex_buckets:
        snapshot = {}
    else:
        raise AppServerProtocolError("app server response has no rateLimits object")
    top_level_snapshot = dict(snapshot)
    if has_codex_buckets:
        for key in ("primary", "secondary"):
            value = codex_snapshot.get(key)
            if isinstance(value, dict):
                if not _valid_used_percent(value):
                    continue
                duration = value.get("windowDurationMins")
                if (
                    duration is not None
                    and _strict_int(duration)
                    not in {FIVE_HOUR_WINDOW_MINUTES, WEEKLY_WINDOW_MINUTES}
                ):
                    continue
                if duration is None:
                    top_level = top_level_snapshot.get(key)
                    top_level_duration = (
                        _strict_int(top_level.get("windowDurationMins"))
                        if isinstance(top_level, dict)
                        else None
                    )
                    if top_level_duration in {
                        FIVE_HOUR_WINDOW_MINUTES,
                        WEEKLY_WINDOW_MINUTES,
                    }:
                        continue
                    if (
                        isinstance(top_level, dict)
                        and top_level.get("windowDurationMins") is not None
                    ):
                        # An explicit unsupported or malformed top-level
                        # duration makes a duration-less nested bucket
                        # unclassifiable; do not infer a false 5h/weekly value.
                        continue
                snapshot[key] = value
    if not isinstance(snapshot, dict):
        raise AppServerProtocolError("app server response has no rateLimits object")
    candidates: list[tuple[str, dict[str, Any]]] = []
    for key in ("primary", "secondary"):
        value = snapshot.get(key)
        if isinstance(value, dict):
            candidates.append((key, value))
    if not candidates:
        return None, None

    duration_candidates: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    for item in candidates:
        duration = _strict_int(item[1].get("windowDurationMins"))
        if duration is not None:
            duration_candidates.setdefault(duration, []).append(item)
    with_duration = {
        duration: items[0]
        for duration, items in duration_candidates.items()
        if len(items) == 1
    }
    if with_duration:
        five_item = with_duration.get(FIVE_HOUR_WINDOW_MINUTES)
        weekly_item = with_duration.get(WEEKLY_WINDOW_MINUTES)
        secondary = candidates[1] if len(candidates) > 1 else None
        if (
            five_item is None
            and _window_duration_is_missing(candidates[0][1])
            and weekly_item is not None
        ):
            five_item = candidates[0] if candidates[0][0] == "primary" else None
        if (
            weekly_item is None
            and secondary is not None
            and _window_duration_is_missing(secondary[1])
            and five_item is not None
        ):
            weekly_item = secondary if secondary and secondary[0] == "secondary" else None
    elif duration_candidates:
        five_item = None
        weekly_item = None
    else:
        by_key = dict(candidates)
        five_item = ("primary", by_key["primary"]) if "primary" in by_key else None
        weekly_item = (
            ("secondary", by_key["secondary"])
            if "secondary" in by_key
            else None
        )
    five = _window("five_hour", five_item[1]) if five_item else None
    weekly = _window("weekly", weekly_item[1]) if weekly_item else None
    return five, weekly


def _window(name: str, payload: dict[str, Any]) -> LimitWindow:
    used = _strict_int(payload.get("usedPercent"))
    if used is None or not 0 <= used <= 100:
        raise AppServerProtocolError("app server usedPercent is invalid")
    reset_value = _strict_int(payload.get("resetsAt"))
    reset_at = None
    if reset_value is not None:
        try:
            reset_at = datetime.fromtimestamp(reset_value, tz=UTC).astimezone()
        except (OSError, OverflowError, ValueError) as exc:
            raise AppServerProtocolError("app server resetsAt is invalid") from exc
    return LimitWindow(
        name=name,
        used=float(used),
        limit=100.0,
        remaining=float(100 - used),
        percent=float(100 - used),
        reset_at=reset_at,
        raw=None,
        source=APP_SERVER_BACKEND,
    )


def _strict_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _valid_used_percent(payload: dict[str, Any]) -> bool:
    used = _strict_int(payload.get("usedPercent"))
    return used is not None and 0 <= used <= 100


def _window_duration_is_missing(payload: dict[str, Any]) -> bool:
    return "windowDurationMins" not in payload or payload.get("windowDurationMins") is None


def _should_refresh(expiry: datetime | None, *, now: datetime) -> bool:
    if expiry is None:
        return False
    return (expiry - now).total_seconds() <= TOKEN_REFRESH_WINDOW_SECONDS


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is not None:
        _signal_process_group(process, signal.SIGTERM, fallback=False)
        return
    if not _signal_process_group(process, signal.SIGTERM):
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if not _signal_process_group(process, signal.SIGKILL):
            return
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return
    except OSError:
        return


def _signal_process_group(
    process: subprocess.Popen[bytes], signum: int, *, fallback: bool = True
) -> bool:
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signum)
            return True
        except (OSError, ValueError):
            pass
    if not fallback:
        return False
    try:
        if signum == signal.SIGKILL:
            process.kill()
        else:
            process.terminate()
        return True
    except OSError:
        return False


def _bounded_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:500] or type(exc).__name__


class _LineReader(threading.Thread):
    def __init__(self, stream: Any):
        super().__init__(daemon=True)
        self.stream = stream
        self.items: queue.Queue[bytes | Exception] = queue.Queue(
            maxsize=APP_SERVER_MAX_MESSAGES + 1
        )

    def run(self) -> None:
        if self.stream is None:
            self._put_item(
                AppServerProtocolError("app server stdout is unavailable"),
                replace_oldest=True,
            )
            return
        try:
            while True:
                line = self.stream.readline(APP_SERVER_MAX_LINE_BYTES + 1)
                if not line:
                    self._put_item(
                        EOFError("codex app server closed stdout"),
                        replace_oldest=True,
                    )
                    return
                if len(line) > APP_SERVER_MAX_LINE_BYTES or not line.endswith(b"\n"):
                    self._put_item(
                        AppServerProtocolError("codex app server response is too large"),
                        replace_oldest=True,
                    )
                    return
                if not self._put_item(line):
                    self._put_item(
                        AppServerProtocolError(
                            "codex app server returned too many pending messages"
                        ),
                        replace_oldest=True,
                    )
                    return
        except (OSError, ValueError):
            self._put_item(
                AppServerProtocolError("could not read codex app server output"),
                replace_oldest=True,
            )

    def _put_item(self, item: bytes | Exception, *, replace_oldest: bool = False) -> bool:
        try:
            self.items.put_nowait(item)
            return True
        except queue.Full:
            if not replace_oldest:
                return False
            try:
                self.items.get_nowait()
            except queue.Empty:
                return False
            try:
                self.items.put_nowait(item)
                return True
            except queue.Full:
                return False


class _StderrReader(threading.Thread):
    def __init__(self, stream: Any):
        super().__init__(daemon=True)
        self.stream = stream
        self._chunks: list[bytes] = []
        self._size = 0

    def run(self) -> None:
        if self.stream is None:
            return
        try:
            while True:
                chunk = self.stream.read(1024)
                if not chunk:
                    return
                if self._size < APP_SERVER_STDERR_BYTES:
                    kept = chunk[: APP_SERVER_STDERR_BYTES - self._size]
                    self._chunks.append(kept)
                    self._size += len(kept)
        except (OSError, ValueError):
            return

    def text(self) -> str:
        raw = b"".join(self._chunks)
        text = raw.decode("utf-8", errors="replace")
        return " ".join(text.split())[:500]
