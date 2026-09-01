from __future__ import annotations

import http.client
import threading
from urllib.parse import urlencode, urlsplit

import pytest

import codex_usage.google_oauth_loopback as loopback_module
from codex_usage.google_oauth_loopback import (
    GoogleOAuthBrowserLauncher,
    GoogleOAuthLoopbackError,
    LoopbackOAuthCallbackLease,
    LoopbackOAuthCallbackProvider,
)


def _callback(uri: str, *, code: str, state: str) -> tuple[int, bytes]:
    parsed = urlsplit(uri)
    result: list[tuple[int, bytes]] = []

    def request() -> None:
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        connection.request(
            "GET",
            f"{parsed.path}?{urlencode({'code': code, 'state': state})}",
            headers={"Host": f"{parsed.hostname}:{parsed.port}"},
        )
        response = connection.getresponse()
        result.append((response.status, response.read()))
        connection.close()

    worker = threading.Thread(target=request)
    worker.start()
    worker.join(3)
    assert not worker.is_alive()
    return result[0]


def test_real_loopback_lease_receives_one_exact_state_bound_code() -> None:
    lease = LoopbackOAuthCallbackProvider().acquire()
    parsed = urlsplit(lease.redirect_uri)
    assert parsed.scheme == "http"
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port is not None
    assert parsed.path.startswith("/oauth/callback/")
    result: list[bytearray] = []

    receiver = threading.Thread(
        target=lambda: result.append(
            lease.receive(expected_state="state-one", timeout_seconds=2.0)
        )
    )
    receiver.start()
    status, body = _callback(
        lease.redirect_uri,
        code="private-authorization-code",
        state="state-one",
    )
    receiver.join(3)
    lease.close()

    assert not receiver.is_alive()
    assert status == 200
    assert b"private-authorization-code" not in body
    assert result == [bytearray(b"private-authorization-code")]
    result[0][:] = b"\0" * len(result[0])


def test_browser_launcher_uses_only_local_start_uri_and_closes_child(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []
    closed: list[object] = []
    process = object()
    monkeypatch.setattr(loopback_module, "default_state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        loopback_module, "_select_browser", lambda _browser: ("firefox", "/bin/firefox")
    )
    monkeypatch.setattr(
        loopback_module, "_prepare_real_private_directory", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        loopback_module,
        "_browser_command",
        lambda _executable, _kind, _profile, url: ["browser", url],
    )
    monkeypatch.setattr(loopback_module, "_browser_environment", lambda: {"LANG": "C"})

    def popen(command, **_kwargs):
        commands.append(command)
        return process

    monkeypatch.setattr(loopback_module.subprocess, "Popen", popen)
    monkeypatch.setattr(
        loopback_module,
        "_kill_login_process_group",
        lambda child: closed.append(child),
    )
    launch_uri = "http://127.0.0.1:8765/oauth/start/nonsecret"

    lease = GoogleOAuthBrowserLauncher().open("firefox", "google-one", launch_uri)
    lease.close()

    assert commands == [["browser", launch_uri]]
    assert closed == [process]


def test_browser_launcher_rejects_non_ascii_account_before_browser_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = []
    monkeypatch.setattr(
        loopback_module,
        "_select_browser",
        lambda browser: selected.append(browser),
    )

    with pytest.raises(GoogleOAuthLoopbackError, match=r"oauth\.browser_unavailable"):
        GoogleOAuthBrowserLauncher().open(
            "firefox",
            "göogle-one",
            "http://127.0.0.1:8765/oauth/start/nonsecret",
        )

    assert selected == []


@pytest.mark.parametrize("control_exception", [KeyboardInterrupt, SystemExit])
def test_browser_launcher_propagates_control_exception(
    monkeypatch: pytest.MonkeyPatch, control_exception
) -> None:
    interrupt = control_exception("stop-now")
    monkeypatch.setattr(
        loopback_module, "_select_browser", lambda _browser: (_ for _ in ()).throw(interrupt)
    )

    with pytest.raises(control_exception) as caught:
        GoogleOAuthBrowserLauncher().open(
            "firefox",
            "google-one",
            "http://127.0.0.1:8765/oauth/start/nonsecret",
        )

    assert caught.value is interrupt


@pytest.mark.parametrize("control_exception", [KeyboardInterrupt, SystemExit])
def test_callback_acquire_closes_listener_and_propagates_control_exception(
    monkeypatch: pytest.MonkeyPatch, control_exception
) -> None:
    interrupt = control_exception("stop-now")

    class Listener:
        closed = False

        def set_inheritable(self, _inheritable: bool) -> None:
            raise interrupt

        def close(self) -> None:
            self.closed = True

    listener = Listener()
    monkeypatch.setattr(loopback_module.socket, "socket", lambda *_args: listener)

    with pytest.raises(control_exception) as caught:
        LoopbackOAuthCallbackProvider().acquire()

    assert caught.value is interrupt
    assert listener.closed is True


@pytest.mark.parametrize("control_exception", [KeyboardInterrupt, SystemExit])
def test_callback_prepare_propagates_control_exception(
    monkeypatch: pytest.MonkeyPatch, control_exception
) -> None:
    import codex_usage.masterjet_contracts as contracts

    interrupt = control_exception("stop-now")
    lease = LoopbackOAuthCallbackProvider().acquire()
    monkeypatch.setattr(
        contracts,
        "google_oauth_redirect_uri",
        lambda _url: (_ for _ in ()).throw(interrupt),
    )
    try:
        with pytest.raises(control_exception) as caught:
            lease.prepare_authorization("https://accounts.google.com/o/oauth2/v2/auth")
        assert caught.value is interrupt
    finally:
        lease.close()


@pytest.mark.parametrize("control_exception", [KeyboardInterrupt, SystemExit])
def test_callback_receive_propagates_control_exception_and_revokes_authorization(
    control_exception,
) -> None:
    interrupt = control_exception("stop-now")

    class Listener:
        closed = False

        def settimeout(self, _timeout: float) -> None:
            pass

        def accept(self):
            raise interrupt

        def close(self) -> None:
            self.closed = True

    listener = Listener()
    lease = LoopbackOAuthCallbackLease(
        listener, port=8765, path="/oauth/callback/token", start_path="/oauth/start/token"
    )
    lease._authorization_url = "https://accounts.google.com/o/oauth2/v2/auth"

    with pytest.raises(control_exception) as caught:
        lease.receive(expected_state="state-one", timeout_seconds=1.0)

    assert caught.value is interrupt
    assert lease._authorization_url is None
    lease.close()
    assert listener.closed is True


def test_loopback_lease_denies_cross_state_and_replay() -> None:
    lease = LoopbackOAuthCallbackProvider().acquire()
    failures: list[str] = []

    def receive() -> None:
        try:
            lease.receive(expected_state="account-one-state", timeout_seconds=2.0)
        except GoogleOAuthLoopbackError as error:
            failures.append(error.code)

    receiver = threading.Thread(target=receive)
    receiver.start()
    status, _body = _callback(
        lease.redirect_uri,
        code="private-authorization-code",
        state="account-two-state",
    )
    receiver.join(3)

    assert status == 400
    assert failures == ["oauth.callback_invalid"]
    with pytest.raises(GoogleOAuthLoopbackError, match=r"oauth\.callback_consumed"):
        lease.receive(expected_state="account-one-state", timeout_seconds=0.1)
    lease.close()


def test_loopback_launch_uri_redirects_without_state_in_browser_argv() -> None:
    lease = LoopbackOAuthCallbackProvider().acquire()
    authorization_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        f"redirect_uri={urlencode({'value': lease.redirect_uri})[6:]}&state=state-one"
    )
    lease.prepare_authorization(authorization_url)
    assert "state-one" not in lease.launch_uri
    result: list[bytearray] = []
    receiver = threading.Thread(
        target=lambda: result.append(
            lease.receive(expected_state="state-one", timeout_seconds=2.0)
        )
    )
    receiver.start()

    parsed = urlsplit(lease.launch_uri)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    connection.request("GET", parsed.path)
    response = connection.getresponse()
    location = response.getheader("Location")
    response.read()
    connection.close()
    assert response.status == 302
    assert location == authorization_url

    status, _body = _callback(
        lease.redirect_uri,
        code="private-authorization-code",
        state="state-one",
    )
    receiver.join(3)
    lease.close()

    assert status == 200
    assert result == [bytearray(b"private-authorization-code")]
