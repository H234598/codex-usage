from __future__ import annotations

import hmac
import math
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import default_state_dir
from .oauth_browser import _browser_command, _browser_environment
from .reactivate import (
    _kill_login_process_group,
    _prepare_real_private_directory,
    _select_browser,
)

_MAX_REQUEST_BYTES = 8192
_MAX_CODE_BYTES = 4096
_MAX_STATE_BYTES = 512


class GoogleOAuthLoopbackError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GoogleOAuthBrowserLauncher:
    def open(
        self, browser: str, account_ref: str, launch_uri: str
    ) -> GoogleOAuthBrowserLease:
        _local_launch_uri(launch_uri)
        if (
            type(account_ref) is not str
            or not account_ref
            or len(account_ref) > 128
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
                for character in account_ref
            )
        ):
            raise GoogleOAuthLoopbackError("oauth.browser_unavailable")
        try:
            browser_kind, executable = _select_browser(browser)
            root = default_state_dir() / "google-oauth-browser"
            account_root = root / account_ref
            profile = account_root / browser_kind
            for path, label in (
                (root, "Google OAuth browser root"),
                (account_root, "Google OAuth account browser root"),
                (profile, "Google OAuth browser profile"),
            ):
                _prepare_real_private_directory(path, label=label)
            process = subprocess.Popen(
                _browser_command(executable, browser_kind, profile, launch_uri),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                env=_browser_environment(),
            )
            return GoogleOAuthBrowserLease(process, profile=profile)
        except Exception:
            raise GoogleOAuthLoopbackError("oauth.browser_unavailable") from None


class GoogleOAuthBrowserLease:
    __slots__ = ("_closed", "_process", "_profile")

    def __init__(self, process: subprocess.Popen[bytes], *, profile: Path) -> None:
        self._process = process
        self._profile = profile
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _kill_login_process_group(self._process)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(closed={self._closed!r})"


class LoopbackOAuthCallbackProvider:
    def acquire(self) -> LoopbackOAuthCallbackLease:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lease: LoopbackOAuthCallbackLease | None = None
        try:
            listener.set_inheritable(False)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            token = secrets.token_urlsafe(24)
            lease = LoopbackOAuthCallbackLease(
                listener,
                port=port,
                path=f"/oauth/callback/{token}",
                start_path=f"/oauth/start/{token}",
            )
            return lease
        except Exception:
            raise GoogleOAuthLoopbackError("oauth.callback_unavailable") from None
        finally:
            if lease is None:
                listener.close()


class LoopbackOAuthCallbackLease:
    __slots__ = (
        "_authorization_url",
        "_closed",
        "_consumed",
        "_listener",
        "_lock",
        "_path",
        "_port",
        "_start_path",
    )

    def __init__(
        self, listener: socket.socket, *, port: int, path: str, start_path: str
    ) -> None:
        self._listener = listener
        self._port = port
        self._path = path
        self._start_path = start_path
        self._authorization_url: str | None = None
        self._closed = False
        self._consumed = False
        self._lock = threading.Lock()

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._port}{self._path}"

    @property
    def launch_uri(self) -> str:
        return f"http://127.0.0.1:{self._port}{self._start_path}"

    def prepare_authorization(self, authorization_url: str) -> None:
        from .masterjet_contracts import (  # local import avoids a contract cycle
            google_oauth_redirect_uri,
            google_oauth_state,
        )

        try:
            redirect_uri = google_oauth_redirect_uri(authorization_url)
            google_oauth_state(authorization_url)
        except Exception:
            raise GoogleOAuthLoopbackError("oauth.callback_invalid") from None
        with self._lock:
            if self._closed or self._consumed or redirect_uri != self.redirect_uri:
                raise GoogleOAuthLoopbackError("oauth.callback_invalid")
            self._authorization_url = authorization_url

    def receive(self, *, expected_state: str, timeout_seconds: float) -> bytearray:
        state = _expected_state(expected_state)
        timeout = _timeout(timeout_seconds)
        with self._lock:
            if self._closed:
                raise GoogleOAuthLoopbackError("oauth.callback_unavailable")
            if self._consumed:
                raise GoogleOAuthLoopbackError("oauth.callback_consumed")
            self._consumed = True
            listener = self._listener
            listener.settimeout(timeout)
        deadline = time.monotonic() + timeout
        started = False
        try:
            while True:
                connection: socket.socket | None = None
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    listener.settimeout(remaining)
                    connection, _address = listener.accept()
                    connection.set_inheritable(False)
                    connection.settimeout(remaining)
                    target = _receive_request(
                        connection, expected_host=f"127.0.0.1:{self._port}"
                    )
                    if (
                        self._authorization_url is not None
                        and not started
                        and target == self._start_path
                    ):
                        _redirect(connection, self._authorization_url)
                        started = True
                        continue
                    code = _callback_code(
                        target,
                        expected_path=self._path,
                        expected_state=state,
                    )
                    _reply(
                        connection,
                        200,
                        b"OAuth callback accepted. You may close this window.\n",
                    )
                    return code
                except GoogleOAuthLoopbackError:
                    if connection is not None:
                        _reply(connection, 400, b"OAuth callback rejected.\n")
                    raise
                finally:
                    if connection is not None:
                        connection.close()
        except TimeoutError:
            raise GoogleOAuthLoopbackError("oauth.callback_timeout") from None
        except OSError:
            raise GoogleOAuthLoopbackError("oauth.callback_unavailable") from None
        except Exception:
            raise GoogleOAuthLoopbackError("oauth.callback_invalid") from None
        finally:
            self._authorization_url = None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._authorization_url = None
            self._listener.close()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(closed={self._closed!r})"


def _receive_request(connection: socket.socket, *, expected_host: str) -> str:
    request = bytearray()
    while b"\r\n\r\n" not in request:
        chunk = connection.recv(min(4096, _MAX_REQUEST_BYTES + 1 - len(request)))
        if not chunk:
            raise GoogleOAuthLoopbackError("oauth.callback_invalid")
        request.extend(chunk)
        if len(request) > _MAX_REQUEST_BYTES:
            raise GoogleOAuthLoopbackError("oauth.callback_invalid")
    head, trailing = request.split(b"\r\n\r\n", 1)
    if trailing:
        raise GoogleOAuthLoopbackError("oauth.callback_invalid")
    try:
        lines = head.decode("ascii").split("\r\n")
        method, target, version = lines[0].split(" ")
        headers = [line.split(":", 1) for line in lines[1:]]
    except (UnicodeError, ValueError):
        raise GoogleOAuthLoopbackError("oauth.callback_invalid") from None
    if method != "GET" or version != "HTTP/1.1" or not headers:
        raise GoogleOAuthLoopbackError("oauth.callback_invalid")
    host_values = [value.strip() for name, value in headers if name.casefold() == "host"]
    if host_values != [expected_host]:
        raise GoogleOAuthLoopbackError("oauth.callback_invalid")
    return target


def _callback_code(target: str, *, expected_path: str, expected_state: str) -> bytearray:
    parsed = urlsplit(target)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.path != expected_path
        or parsed.fragment
        or not parsed.query
    ):
        raise GoogleOAuthLoopbackError("oauth.callback_invalid")
    try:
        query = parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=4,
        )
    except (UnicodeError, ValueError):
        raise GoogleOAuthLoopbackError("oauth.callback_invalid") from None
    if set(query) != {"code", "state"} or any(len(values) != 1 for values in query.values()):
        raise GoogleOAuthLoopbackError("oauth.callback_invalid")
    state = query["state"][0]
    if not hmac.compare_digest(state, expected_state):
        raise GoogleOAuthLoopbackError("oauth.callback_invalid")
    code = query["code"][0]
    try:
        encoded = code.encode("ascii")
    except UnicodeError:
        raise GoogleOAuthLoopbackError("oauth.callback_invalid") from None
    if (
        not encoded
        or len(encoded) > _MAX_CODE_BYTES
        or any(not 0x21 <= byte <= 0x7E for byte in encoded)
    ):
        raise GoogleOAuthLoopbackError("oauth.callback_invalid")
    return bytearray(encoded)


def _expected_state(value: object) -> str:
    if type(value) is not str:
        raise GoogleOAuthLoopbackError("oauth.callback_invalid")
    try:
        encoded = value.encode("ascii")
    except UnicodeError:
        raise GoogleOAuthLoopbackError("oauth.callback_invalid") from None
    if not encoded or len(encoded) > _MAX_STATE_BYTES or any(
        not 0x21 <= byte <= 0x7E for byte in encoded
    ):
        raise GoogleOAuthLoopbackError("oauth.callback_invalid")
    return value


def _local_launch_uri(value: object) -> str:
    if type(value) is not str:
        raise GoogleOAuthLoopbackError("oauth.browser_unavailable")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise GoogleOAuthLoopbackError("oauth.browser_unavailable") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/oauth/start/")
        or parsed.query
        or parsed.fragment
    ):
        raise GoogleOAuthLoopbackError("oauth.browser_unavailable")
    return value


def _timeout(value: object) -> float:
    if type(value) not in {int, float}:
        raise GoogleOAuthLoopbackError("oauth.callback_invalid")
    timeout = float(value)
    if not math.isfinite(timeout) or not 0 < timeout <= 900:
        raise GoogleOAuthLoopbackError("oauth.callback_invalid")
    return timeout


def _reply(connection: socket.socket, status: int, body: bytes) -> None:
    reason = b"OK" if status == 200 else b"Bad Request"
    try:
        connection.sendall(
            b"HTTP/1.1 "
            + str(status).encode("ascii")
            + b" "
            + reason
            + b"\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n"
            + body
        )
    except OSError:
        pass


def _redirect(connection: socket.socket, authorization_url: str) -> None:
    try:
        connection.sendall(
            b"HTTP/1.1 302 Found\r\nLocation: "
            + authorization_url.encode("ascii")
            + b"\r\nContent-Length: 0\r\nConnection: close\r\n"
            b"Cache-Control: no-store\r\n\r\n"
        )
    except (OSError, UnicodeError):
        raise GoogleOAuthLoopbackError("oauth.callback_unavailable") from None
