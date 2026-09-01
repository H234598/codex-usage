from __future__ import annotations

import base64
import json
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import codex_usage.reactivate as reactivate_module
from codex_usage import oauth_browser
from codex_usage.browser import _profile_lock
from codex_usage.cli import main
from codex_usage.config import AppConfig, save_config
from codex_usage.models import Account
from codex_usage.reactivate import (
    MANAGE_ACCOUNT_URL,
    OAUTH_PROFILE_MARKER,
    ReactivationError,
    _kill_login_process_group,
    _resolve_executable,
    _validate_refreshed_auth,
    _validate_refreshed_identity,
    open_account_in_reactivation_browser,
    reactivate_account,
)


def _jwt_with_exp(expiry: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).rstrip(
        b"="
    ).decode()
    return f"{header}.{payload}.signature"


class _BrokenInt(int):
    def __ge__(self, _other):
        raise RuntimeError("synthetic reactivation integer comparison marker")

    def __le__(self, _other):
        raise RuntimeError("synthetic reactivation integer comparison marker")


def _jwt_with_user_id(user_id: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {"https://api.openai.com/auth": {"chatgpt_user_id": user_id}}
        ).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


def _executable(path: Path) -> str:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return str(path)


def _fake_login_popen(callback, returncode=0):
    class FakeProcess:
        pid = 9876

        def __init__(self):
            self.callback_called = False

        def wait(self, timeout):
            if not self.callback_called:
                self.callback_called = True
                callback()
            return returncode

        def kill(self):
            return None

    return FakeProcess()


def test_reactivate_symlink_check_rejects_dotdot_bypass(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ReactivationError, match="must not contain symlinks"):
        reactivate_module._assert_no_symlink_ancestors(
            redirected / ".." / "target",
            label="profile path",
        )


def test_reactivate_symlink_check_scans_after_missing_segment(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ReactivationError, match="must not contain symlinks"):
        reactivate_module._assert_no_symlink_ancestors(
            tmp_path / "missing" / ".." / "redirected" / "target",
            label="profile path",
        )


@pytest.mark.parametrize("explicit", ["", " ", [], False])
def test_resolve_reactivation_executable_rejects_explicit_invalid_values(
    explicit, tmp_path, monkeypatch
):
    fallback = tmp_path / "codex"
    fallback.write_text("#!/bin/sh\n", encoding="utf-8")
    fallback.chmod(0o700)
    monkeypatch.setattr(reactivate_module.shutil, "which", lambda _name: str(fallback))

    with pytest.raises(ReactivationError, match="codex command is invalid"):
        _resolve_executable(explicit, "codex", label="codex command")


def test_resolve_reactivation_executable_rejects_unknown_home():
    with pytest.raises(ReactivationError, match="codex command is invalid"):
        _resolve_executable(
            "~definitely-no-such-user-zzzz/codex",
            "codex",
            label="codex command",
        )


def test_resolve_reactivation_executable_accepts_default(tmp_path, monkeypatch):
    fallback = _executable(tmp_path / "codex")
    monkeypatch.setattr(reactivate_module.shutil, "which", lambda _name: fallback)

    assert _resolve_executable(None, "codex", label="codex command") == fallback


def test_reactivate_uses_account_browser_when_override_is_missing(monkeypatch, tmp_path):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
        reactivation_browser="vivaldi",
    )
    captured = {}
    monkeypatch.setattr(
        "codex_usage.reactivate._reactivate_account_unlocked",
        lambda _account, **kwargs: captured.update(kwargs) or {},
    )

    reactivate_account(account, browser=None)

    assert captured["browser"] == "vivaldi"


def test_reactivate_maps_account_lock_error(tmp_path, monkeypatch):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
    )

    class Locked:
        def __enter__(self):
            raise reactivate_module.AccountLockError("account is busy")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(reactivate_module, "account_lock", lambda _account_id: Locked())

    with pytest.raises(ReactivationError, match="account is busy"):
        reactivate_account(account)


def test_manage_account_opens_the_existing_isolated_browser_profile(monkeypatch, tmp_path):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
        reactivation_browser="vivaldi",
    )
    profile = tmp_path / "oauth-profile"
    helper = str(tmp_path / "codex-usage-browser")
    captured = {}
    monkeypatch.setattr(
        "codex_usage.reactivate._select_browser",
        lambda requested: (requested, "/usr/bin/vivaldi-stable"),
    )
    monkeypatch.setattr(
        "codex_usage.reactivate._prepare_oauth_profile",
        lambda _account, _browser: profile,
    )
    monkeypatch.setattr(
        "codex_usage.reactivate._resolve_executable",
        lambda _explicit, _fallback, label: helper,
    )
    monkeypatch.setattr(
        reactivate_module.subprocess,
        "Popen",
        lambda command, **kwargs: captured.update(command=command, kwargs=kwargs),
    )

    result = open_account_in_reactivation_browser(account)

    assert result == {
        "ok": True,
        "account": "work",
        "label": "Work",
        "browser": "vivaldi",
        "url": MANAGE_ACCOUNT_URL,
    }
    assert captured["command"] == [helper, MANAGE_ACCOUNT_URL]
    assert captured["kwargs"]["env"]["CODEX_USAGE_BROWSER_PROFILE"] == str(profile)
    assert "CODEX_HOME" not in captured["kwargs"]["env"]


def test_manage_account_auto_reuses_the_account_browser(monkeypatch, tmp_path):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
        browser="firefox",
        reactivation_browser="auto",
    )
    profile = tmp_path / "firefox-profile"
    helper = str(tmp_path / "codex-usage-browser")
    captured = {}

    monkeypatch.setattr(
        "codex_usage.reactivate._select_browser",
        lambda requested: (requested, "/usr/bin/firefox")
        if requested == "firefox"
        else pytest.fail("auto must prefer the configured account browser"),
    )
    monkeypatch.setattr(
        "codex_usage.reactivate._manage_browser_profile",
        lambda _account, browser: captured.update(browser=browser) or profile,
    )
    monkeypatch.setattr(
        "codex_usage.reactivate._resolve_executable",
        lambda _explicit, _fallback, label: helper,
    )
    monkeypatch.setattr(
        reactivate_module.subprocess,
        "Popen",
        lambda command, **kwargs: captured.update(command=command, kwargs=kwargs),
    )

    result = open_account_in_reactivation_browser(account)

    assert result["ok"] is True
    assert captured["browser"] == "firefox"


def test_manage_account_auto_falls_back_when_account_browser_is_unavailable(
    monkeypatch, tmp_path
):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
        browser="firefox",
        reactivation_browser="auto",
    )
    calls = []
    profile = tmp_path / "profile"
    helper = str(tmp_path / "helper")

    def select(requested):
        calls.append(requested)
        if requested == "firefox":
            raise ReactivationError("firefox unavailable")
        return "chromium", "/usr/bin/chromium"

    monkeypatch.setattr(reactivate_module, "_select_browser", select)
    monkeypatch.setattr(
        reactivate_module,
        "_manage_browser_profile",
        lambda _account, _browser: profile,
    )
    monkeypatch.setattr(
        reactivate_module,
        "_resolve_executable",
        lambda _explicit, _fallback, *, label: helper,
    )
    monkeypatch.setattr(reactivate_module.subprocess, "Popen", lambda *args, **kwargs: None)

    result = open_account_in_reactivation_browser(account)

    assert result["browser"] == "chromium"
    assert calls == ["firefox", "auto"]


def test_manage_account_maps_browser_start_oserror(monkeypatch, tmp_path):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
        reactivation_browser="vivaldi",
    )
    monkeypatch.setattr(
        reactivate_module,
        "_select_browser",
        lambda _requested: ("vivaldi", "/usr/bin/vivaldi"),
    )
    monkeypatch.setattr(
        reactivate_module,
        "_manage_browser_profile",
        lambda _account, _browser: tmp_path / "profile",
    )
    monkeypatch.setattr(
        reactivate_module,
        "_resolve_executable",
        lambda _explicit, _fallback, *, label: "/usr/bin/helper",
    )

    def fail_start(*_args, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(reactivate_module.subprocess, "Popen", fail_start)

    with pytest.raises(ReactivationError, match="could not start isolated account browser"):
        open_account_in_reactivation_browser(account)


@pytest.mark.parametrize(
    "url",
    [
        "https://chatgpt.com/codex/cloud/settings/analytics",
        "https://example.com/codex/cloud/settings/analytics#usage",
        "https://chatgpt.com/codex/cloud/settings/analytics?x=1#usage",
    ],
)
def test_manage_account_rejects_non_usage_urls(tmp_path, url):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
        reactivation_browser="vivaldi",
    )

    with pytest.raises(ReactivationError, match="Codex Usage page"):
        open_account_in_reactivation_browser(account, url=url)


def test_manage_account_rejects_malformed_url(tmp_path):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
        reactivation_browser="vivaldi",
    )

    with pytest.raises(ReactivationError, match="manage account URL is invalid"):
        open_account_in_reactivation_browser(account, url="https://[::1")


@pytest.mark.parametrize(
    "timeout_seconds",
    (
        pytest.param(True, id="bool"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(3601, id="above-maximum"),
        pytest.param(1.0, id="float"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
        pytest.param("1", id="string"),
        pytest.param(10**10_000, id="huge-int"),
        pytest.param(_BrokenInt(60), id="integer-subclass"),
    ),
)
def test_reactivate_rejects_invalid_timeout_before_account_lock(
    tmp_path, monkeypatch, timeout_seconds
):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
    )

    def fail_lock(_account_id):
        pytest.fail("account lock must not be entered")

    monkeypatch.setattr(reactivate_module, "account_lock", fail_lock)

    with pytest.raises(ReactivationError, match="reactivation timeout is invalid"):
        reactivate_account(account, timeout_seconds=timeout_seconds)


@pytest.mark.parametrize("account", [None, [], "invalid", 1, True, object()])
def test_reactivation_entrypoints_reject_non_account_input(account):
    with pytest.raises(ReactivationError, match="account is invalid"):
        reactivate_account(account)  # type: ignore[arg-type]
    with pytest.raises(ReactivationError, match="account is invalid"):
        open_account_in_reactivation_browser(account)  # type: ignore[arg-type]


@pytest.mark.parametrize("account_id", [None, [], "../escape", "__all_accounts__"])
def test_reactivation_entrypoints_reject_invalid_account_id(tmp_path, account_id):
    account = Account(
        id=account_id,
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
    )

    with pytest.raises(ReactivationError, match="account id is invalid"):
        reactivate_account(account)
    with pytest.raises(ReactivationError, match="account id is invalid"):
        open_account_in_reactivation_browser(account)


def test_reactivation_rejects_malformed_account_paths(tmp_path):
    malformed_profile = Account(
        id="work",
        label="Work",
        profile_dir=[],  # type: ignore[arg-type]
    )
    with pytest.raises(ReactivationError, match="account profile_dir is invalid"):
        open_account_in_reactivation_browser(malformed_profile)

    malformed_auth = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
        auth_json_path=[],  # type: ignore[arg-type]
    )
    with pytest.raises(ReactivationError, match="account auth_json_path is invalid"):
        reactivate_account(malformed_auth)

    relative_auth = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
        auth_json_path="auth.json",
    )
    with pytest.raises(ReactivationError, match="account auth_json_path is invalid"):
        reactivate_module._validate_auth_target(relative_auth)

    relative_profile = Account(
        id="work",
        label="Work",
        profile_dir="relative-profile",
    )
    with pytest.raises(ReactivationError, match="account profile_dir is invalid"):
        reactivate_module._account_profile_root(relative_profile)


def test_reactivation_rejects_unknown_home_paths(tmp_path):
    malformed_auth = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
        auth_json_path="~definitely-no-such-user-zzzz/auth.json",
    )
    with pytest.raises(ReactivationError, match="account auth_json_path is invalid"):
        reactivate_module._validate_auth_target(malformed_auth)

    malformed_profile = Account(
        id="work",
        label="Work",
        profile_dir="~definitely-no-such-user-zzzz/profile",
    )
    with pytest.raises(ReactivationError, match="account profile_dir is invalid"):
        reactivate_module._account_profile_root(malformed_profile)


def test_reactivate_account_uses_isolated_codex_home(tmp_path, monkeypatch):
    auth_home = tmp_path / "agent-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    profile_root = tmp_path / "profiles" / "work"
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(profile_root),
        auth_json_path=str(auth_path),
    )
    codex = _executable(tmp_path / "codex")
    helper = _executable(tmp_path / "codex-usage-browser")
    browser = _executable(tmp_path / "vivaldi-stable")
    monkeypatch.setattr(
        "codex_usage.reactivate._select_browser",
        lambda requested: ("vivaldi", browser),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("USER", "teladi")
    monkeypatch.setenv("LOGNAME", "teladi")
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("TZ", "Europe/Berlin")
    monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(tmp_path / "ca.pem"))
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(tmp_path / "node-ca.pem"))

    captured = {}
    lock_results = []

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        def write_auth():
            oauth_profile = profile_root / "oauth" / "vivaldi"
            try:
                with _profile_lock(oauth_profile):
                    pass
            except RuntimeError:
                lock_results.append(True)
            else:
                lock_results.append(False)
            expiry = int((datetime.now(UTC) + timedelta(days=10)).timestamp())
            auth_path.write_text(
                json.dumps(
                    {"auth_mode": "chatgpt", "tokens": {"access_token": _jwt_with_exp(expiry)}}
                ),
                encoding="utf-8",
            )
            auth_path.chmod(0o600)

        return _fake_login_popen(write_auth)

    monkeypatch.setattr("codex_usage.reactivate.subprocess.Popen", fake_popen)

    result = reactivate_account(
        account,
        codex_command=codex,
        browser_helper=helper,
    )

    assert result["ok"] is True
    assert result["browser"] == "vivaldi"
    assert captured["argv"] == [codex, "login"]
    assert captured["env"]["CODEX_HOME"] == str(auth_home)
    assert captured["env"]["BROWSER"] == helper
    assert captured["env"]["CODEX_USAGE_BROWSER_PROFILE"] == str(
        profile_root / "oauth" / "vivaldi"
    )
    assert lock_results == [True]
    assert "OPENAI_API_KEY" not in captured["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in captured["env"]
    assert captured["env"]["TMPDIR"] == str(tmp_path / "tmp")
    assert captured["env"]["USER"] == "teladi"
    assert captured["env"]["LOGNAME"] == "teladi"
    assert captured["env"]["TERM"] == "xterm"
    assert captured["env"]["TZ"] == "Europe/Berlin"
    assert captured["env"]["CODEX_CA_CERTIFICATE"] == str(tmp_path / "ca.pem")
    assert "NODE_EXTRA_CA_CERTS" not in captured["env"]
    assert (profile_root / "oauth" / "vivaldi" / OAUTH_PROFILE_MARKER).is_file()


def test_reactivate_reports_oauth_profile_lock_contention(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    auth_home = tmp_path / "agent-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    profile = tmp_path / "profiles" / "work" / "oauth" / "chromium"
    profile.mkdir(parents=True)
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(profile.parent.parent),
        auth_json_path=str(auth_path),
    )
    codex = _executable(tmp_path / "codex")
    helper = _executable(tmp_path / "codex-usage-browser")
    browser = _executable(tmp_path / "chromium")
    monkeypatch.setattr(
        "codex_usage.reactivate._select_browser",
        lambda requested: ("chromium", browser),
    )
    monkeypatch.setattr(
        "codex_usage.reactivate._prepare_oauth_profile",
        lambda _account, _browser: profile,
    )

    with _profile_lock(profile):
        with pytest.raises(ReactivationError, match="profile is already in use"):
            reactivate_account(
                account,
                codex_command=codex,
                browser_helper=helper,
            )


def test_reactivate_wraps_invalid_profile_lock(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    account = Account(id="work", label="Work", profile_dir=str(profile))

    class InvalidProfileLock:
        def __enter__(self):
            raise ValueError("unsafe profile lock")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        reactivate_module,
        "_validate_auth_target",
        lambda _account: tmp_path / "auth.json",
    )
    monkeypatch.setattr(
        reactivate_module,
        "_select_browser",
        lambda _browser: ("chromium", "/bin/true"),
    )
    monkeypatch.setattr(
        reactivate_module,
        "_prepare_oauth_profile",
        lambda _account, _browser: profile,
    )
    monkeypatch.setattr(
        reactivate_module,
        "_resolve_executable",
        lambda *_args, **_kwargs: "/bin/true",
    )
    monkeypatch.setattr(
        reactivate_module,
        "_profile_lock",
        lambda _profile: InvalidProfileLock(),
    )

    with pytest.raises(ReactivationError, match="unsafe profile lock") as captured:
        reactivate_module._reactivate_account_unlocked(
            account,
            browser="chromium",
            timeout_seconds=60,
            codex_command=None,
            browser_helper=None,
        )

    assert isinstance(captured.value.__cause__, ValueError)


def test_reactivate_discards_login_output_without_capture_buffer(tmp_path, monkeypatch):
    auth_path = tmp_path / "agent-home" / "auth.json"
    auth_path.parent.mkdir()
    profile_root = tmp_path / "profiles" / "work"
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(profile_root),
        auth_json_path=str(auth_path),
    )
    codex = _executable(tmp_path / "codex")
    helper = _executable(tmp_path / "codex-usage-browser")
    browser = _executable(tmp_path / "chromium")
    monkeypatch.setattr(
        "codex_usage.reactivate._select_browser",
        lambda requested: ("chromium", browser),
    )

    def fake_popen(argv, **kwargs):
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL

        def write_auth():
            expiry = int((datetime.now(UTC) + timedelta(days=10)).timestamp())
            auth_path.write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {"access_token": _jwt_with_exp(expiry)},
                    }
                ),
                encoding="utf-8",
            )
            auth_path.chmod(0o600)

        return _fake_login_popen(write_auth)

    monkeypatch.setattr("codex_usage.reactivate.subprocess.Popen", fake_popen)

    result = reactivate_account(
        account,
        codex_command=codex,
        browser_helper=helper,
    )

    assert result["ok"] is True


def test_reactivate_timeout_kills_login_process_group(tmp_path, monkeypatch):
    auth_path = tmp_path / "agent-home" / "auth.json"
    auth_path.parent.mkdir()
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
        auth_json_path=str(auth_path),
    )
    codex = _executable(tmp_path / "codex")
    helper = _executable(tmp_path / "codex-usage-browser")
    browser = _executable(tmp_path / "chromium")
    monkeypatch.setattr(
        "codex_usage.reactivate._select_browser",
        lambda requested: ("chromium", browser),
    )
    calls = []

    class FakeProcess:
        pid = 4321

        def __init__(self):
            self.wait_calls = 0

        def wait(self, timeout):
            self.wait_calls += 1
            calls.append(("wait", timeout))
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired([codex, "login"], timeout)
            return -9

        def kill(self):
            calls.append(("kill",))

    def fake_popen(argv, **kwargs):
        calls.append(("popen", argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr("codex_usage.reactivate.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "codex_usage.reactivate.subprocess.run",
        lambda *args, **kwargs: pytest.fail("reactivation must use Popen"),
    )
    monkeypatch.setattr(
        "codex_usage.reactivate.os.killpg",
        lambda pid, signum: calls.append(("killpg", pid, signum)),
    )

    with pytest.raises(ReactivationError, match="login timed out"):
        reactivate_account(
            account,
            codex_command=codex,
            browser_helper=helper,
            timeout_seconds=1,
        )

    popen_call = next(call for call in calls if call[0] == "popen")
    assert popen_call[2]["start_new_session"] is True
    assert popen_call[2]["stdout"] is subprocess.DEVNULL
    assert popen_call[2]["stderr"] is subprocess.DEVNULL
    assert ("killpg", 4321, signal.SIGKILL) in calls


def test_kill_login_process_group_rejects_boolean_pid(monkeypatch):
    calls = []

    class FakeProcess:
        pid = True

        def kill(self):
            calls.append(("kill",))

        def wait(self, timeout):
            calls.append(("wait", timeout))

    monkeypatch.setattr(
        "codex_usage.reactivate.os.killpg",
        lambda pid, signum: calls.append(("killpg", pid, signum)),
    )

    _kill_login_process_group(FakeProcess())

    assert calls == [("kill",), ("wait", 2)]


def test_kill_login_process_group_rejects_numeric_subclass_pid(monkeypatch):
    calls = []

    class FakeProcess:
        pid = _BrokenInt(4321)

        def kill(self):
            calls.append(("kill",))

        def wait(self, timeout):
            calls.append(("wait", timeout))

    monkeypatch.setattr(
        "codex_usage.reactivate.os.killpg",
        lambda pid, signum: calls.append(("killpg", pid, signum)),
    )

    _kill_login_process_group(FakeProcess())

    assert calls == [("kill",), ("wait", 2)]


def test_reactivation_wait_oserror_kills_process_group(tmp_path, monkeypatch):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
    )
    calls = []

    class FakeProcess:
        pid = 4321

        def wait(self, timeout):
            calls.append(("wait", timeout))
            raise OSError("wait failed")

        def kill(self):
            calls.append(("kill",))

    monkeypatch.setattr(
        "codex_usage.reactivate.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        "codex_usage.reactivate.os.killpg",
        lambda pid, signum: calls.append(("killpg", pid, signum)),
    )

    with pytest.raises(ReactivationError, match="could not wait for codex login"):
        reactivate_module._run_reactivation(
            account,
            auth_path=tmp_path / "auth.json",
            browser_kind="chromium",
            browser_executable="chromium",
            profile_dir=tmp_path / "profile",
            codex="codex",
            helper="helper",
            timeout_seconds=1,
        )

    assert ("killpg", 4321, signal.SIGKILL) in calls


def test_reactivation_start_oserror_is_mapped(tmp_path, monkeypatch):
    account = Account(id="work", label="Work", profile_dir=str(tmp_path / "profile"))

    def fail_start(*_args, **_kwargs):
        raise OSError("start failed")

    monkeypatch.setattr(reactivate_module.subprocess, "Popen", fail_start)

    with pytest.raises(ReactivationError, match="could not start codex login"):
        reactivate_module._run_reactivation(
            account,
            auth_path=tmp_path / "auth.json",
            browser_kind="chromium",
            browser_executable="chromium",
            profile_dir=tmp_path / "profile",
            codex="codex",
            helper="helper",
            timeout_seconds=1,
        )


def test_reactivation_restore_error_wins_over_login_error(tmp_path, monkeypatch):
    account = Account(id="work", label="Work", profile_dir=str(tmp_path / "profile"))

    class FailedProcess:
        pid = 1

        def wait(self, timeout):
            return 1

    monkeypatch.setattr(reactivate_module.subprocess, "Popen", lambda *a, **k: FailedProcess())

    def fail_restore(*_args, **_kwargs):
        raise ReactivationError("restore failed")

    monkeypatch.setattr(reactivate_module, "_restore_auth_backup", fail_restore)

    with pytest.raises(ReactivationError, match="restore failed"):
        reactivate_module._run_reactivation(
            account,
            auth_path=tmp_path / "auth.json",
            browser_kind="chromium",
            browser_executable="chromium",
            profile_dir=tmp_path / "profile",
            codex="codex",
            helper="helper",
            timeout_seconds=1,
        )


def test_reactivation_unexpected_error_is_wrapped(tmp_path, monkeypatch):
    account = Account(id="work", label="Work", profile_dir=str(tmp_path / "profile"))

    class SuccessfulProcess:
        pid = 1

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(
        reactivate_module.subprocess,
        "Popen",
        lambda *args, **kwargs: SuccessfulProcess(),
    )
    monkeypatch.setattr(
        reactivate_module,
        "_validate_refreshed_auth",
        lambda _path: (_ for _ in ()).throw(ValueError("unexpected")),
    )
    monkeypatch.setattr(reactivate_module, "_restore_auth_backup", lambda *_a, **_k: None)

    with pytest.raises(ReactivationError, match="login failed unexpectedly"):
        reactivate_module._run_reactivation(
            account,
            auth_path=tmp_path / "auth.json",
            browser_kind="chromium",
            browser_executable="chromium",
            profile_dir=tmp_path / "profile",
            codex="codex",
            helper="helper",
            timeout_seconds=1,
        )


def test_kill_login_process_group_ignores_kill_errors(monkeypatch):
    class UnkillableProcess:
        pid = 4321

        def kill(self):
            raise OSError("kill failed")

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("codex", timeout)

    monkeypatch.setattr(
        reactivate_module.os,
        "killpg",
        lambda _pid, _signal: (_ for _ in ()).throw(OSError("group gone")),
    )

    _kill_login_process_group(UnkillableProcess())


def test_capture_auth_backup_maps_read_error_and_hardlink(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{}\n", encoding="utf-8")
    auth_path.chmod(0o600)

    monkeypatch.setattr(
        reactivate_module,
        "read_auth_json_file",
        lambda _path: (_ for _ in ()).throw(
            reactivate_module.DirectAuthError("read failed")
        ),
    )
    with pytest.raises(ReactivationError, match="preserve previous auth"):
        reactivate_module._capture_auth_backup(auth_path)

    alias = tmp_path / "alias"
    alias.hardlink_to(auth_path)
    monkeypatch.setattr(
        reactivate_module,
        "read_auth_json_file",
        lambda _path: (
            "{}\n",
            type("Stat", (), {"st_nlink": 2, "st_mode": 0o100600})(),
        ),
    )
    with pytest.raises(ReactivationError, match="must not be hard-linked"):
        reactivate_module._capture_auth_backup(auth_path)


def test_identity_from_auth_backup_ignores_invalid_payloads(tmp_path):
    path = tmp_path / "auth.json"

    assert reactivate_module._identity_from_auth_backup(path, ("[]", 0)) == (None, None)
    assert reactivate_module._identity_from_auth_backup(path, ("{broken", 0)) == (
        None,
        None,
    )


def test_refreshed_identity_rejects_non_object_auth(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("[]\n", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ReactivationError, match="verifiable account identity"):
        _validate_refreshed_identity(path, ("user-old", "account-old"))


def test_refreshed_identity_accepts_expected_user_as_account_id(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "tokens": {
                    "account_id": "user-old",
                    "access_token": _jwt_with_user_id("user-old"),
                }
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    _validate_refreshed_identity(path, ("user-old", "account-old"))


def test_refreshed_identity_accepts_user_only_identity(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps({"tokens": {"access_token": _jwt_with_user_id("user-old")}}),
        encoding="utf-8",
    )
    path.chmod(0o600)

    _validate_refreshed_identity(path, ("user-old", None))


def test_restore_auth_without_backup_rejects_unsafe_targets(tmp_path):
    symlink_target = tmp_path / "target"
    symlink_target.write_text("keep\n", encoding="utf-8")
    symlink = tmp_path / "auth.json"
    symlink.symlink_to(symlink_target)
    with pytest.raises(ReactivationError, match="restore previous auth"):
        reactivate_module._restore_auth_backup(symlink, None)

    hardlinked = tmp_path / "hardlinked-auth.json"
    hardlinked.write_text("keep\n", encoding="utf-8")
    hardlink_alias = tmp_path / "hardlinked-alias"
    hardlink_alias.hardlink_to(hardlinked)
    with pytest.raises(ReactivationError, match="restore previous auth"):
        reactivate_module._restore_auth_backup(hardlinked, None)


def test_restore_auth_without_backup_removes_existing_file(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("partial\n", encoding="utf-8")

    reactivate_module._restore_auth_backup(path, None)

    assert not path.exists()


def test_restore_auth_without_backup_maps_unlink_error(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    path.write_text("partial\n", encoding="utf-8")
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda _path: (_ for _ in ()).throw(OSError("unlink failed")),
    )

    with pytest.raises(ReactivationError, match="restore previous auth"):
        reactivate_module._restore_auth_backup(path, None)


@pytest.mark.parametrize(
    ("auth_json_path", "message"),
    [
        (None, "no auth_json_path"),
        ("/tmp/not-auth.json", "point to auth.json"),
    ],
)
def test_validate_auth_target_rejects_missing_or_wrong_filename(
    tmp_path, auth_json_path, message
):
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=auth_json_path,
    )

    with pytest.raises(ReactivationError, match=message):
        reactivate_module._validate_auth_target(account)


def test_validate_auth_target_rejects_non_directory_parent(tmp_path):
    parent = tmp_path / "parent-file"
    parent.write_text("not a directory\n", encoding="utf-8")
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(parent / "auth.json"),
    )

    with pytest.raises(ReactivationError, match="real directory"):
        reactivate_module._validate_auth_target(account)


@pytest.mark.parametrize("target_kind", ["directory", "symlink"])
def test_validate_auth_target_rejects_non_regular_target(tmp_path, target_kind):
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "auth.json"
    if target_kind == "directory":
        target.mkdir()
    else:
        real_target = tmp_path / "real-auth"
        real_target.write_text("keep\n", encoding="utf-8")
        target.symlink_to(real_target)
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(target),
    )

    with pytest.raises(ReactivationError, match="regular file"):
        reactivate_module._validate_auth_target(account)


def test_select_browser_rejects_unsupported_and_missing(monkeypatch):
    with pytest.raises(ReactivationError, match="unsupported reactivation browser"):
        reactivate_module._select_browser("safari")

    monkeypatch.setattr(reactivate_module.shutil, "which", lambda _command: None)
    with pytest.raises(ReactivationError, match="browser is not installed"):
        reactivate_module._select_browser("firefox")


@pytest.mark.parametrize("url", [None, "x" * 2049])
def test_validate_manage_url_rejects_non_string_or_overlong(url):
    with pytest.raises(ReactivationError, match="manage account URL is invalid"):
        reactivate_module._validate_manage_url(url)  # type: ignore[arg-type]


def test_manage_browser_profile_reuses_compatible_existing_profile(tmp_path):
    root = tmp_path / "profiles" / "work"
    browser_dir = root / "firefox"
    browser_dir.mkdir(parents=True)
    marker = browser_dir / ".codex-usage-browser-profile"
    marker.write_text("marker\n", encoding="utf-8")
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(root),
        browser="firefox",
    )

    assert reactivate_module._manage_browser_profile(account, "firefox") == browser_dir


def test_manage_browser_profile_falls_back_without_profile_marker(tmp_path, monkeypatch):
    root = tmp_path / "profiles" / "work"
    (root / "firefox").mkdir(parents=True)
    fallback = tmp_path / "oauth" / "firefox"
    monkeypatch.setattr(
        reactivate_module,
        "_prepare_oauth_profile",
        lambda _account, _browser: fallback,
    )
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(root),
        browser="firefox",
    )

    assert reactivate_module._manage_browser_profile(account, "firefox") == fallback


@pytest.mark.parametrize("error", [OSError("mkdir failed"), ValueError("unsafe")])
def test_prepare_real_private_directory_maps_creation_errors(
    tmp_path, monkeypatch, error
):
    path = tmp_path / "profile"
    monkeypatch.setattr(
        reactivate_module,
        "ensure_private_directory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(ReactivationError):
        reactivate_module._prepare_real_private_directory(path, label="profile")


def test_prepare_real_private_directory_rejects_symlink(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    path = tmp_path / "profile"
    path.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(reactivate_module, "_assert_no_symlink_ancestors", lambda *a, **k: None)

    with pytest.raises(ReactivationError, match="must not be a symlink"):
        reactivate_module._prepare_real_private_directory(path, label="profile")


def test_symlink_ancestor_scanner_skips_dot_segments(monkeypatch):
    real_path = reactivate_module.Path

    class DottedPath:
        anchor = "/"
        parts = ("/", ".", "safe")

        def is_absolute(self):
            return True

    monkeypatch.setattr(
        reactivate_module,
        "Path",
        lambda value: value if isinstance(value, DottedPath) else real_path(value),
    )

    reactivate_module._assert_no_symlink_ancestors(DottedPath(), label="profile")


def test_resolve_executable_rejects_missing_fallback_and_non_executable(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(reactivate_module.shutil, "which", lambda _name: None)
    with pytest.raises(ReactivationError, match="codex command was not found"):
        _resolve_executable(None, "codex", label="codex command")

    path = tmp_path / "codex"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ReactivationError, match="codex command is not executable"):
        _resolve_executable(str(path), "codex", label="codex command")


@pytest.mark.parametrize("raw", ["{broken", "[]"])
def test_validate_refreshed_auth_rejects_malformed_or_non_object(raw, tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(raw, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ReactivationError, match=r"without a valid auth\.json"):
        _validate_refreshed_auth(path)


def test_validate_refreshed_auth_rejects_expired_access_token(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {"auth_mode": "chatgpt", "tokens": {"access_token": _jwt_with_exp(1)}}
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(ReactivationError, match="expired access token"):
        _validate_refreshed_auth(path)


def test_reactivate_rejects_different_account_and_restores_auth_json(tmp_path, monkeypatch):
    auth_home = tmp_path / "agent-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    expiry = int((datetime.now(UTC) + timedelta(days=10)).timestamp())
    old_raw = json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "account_id": "account-old",
                "access_token": _jwt_with_exp(expiry),
            },
        }
    )
    auth_path.write_text(old_raw, encoding="utf-8")
    auth_path.chmod(0o600)
    profile_root = tmp_path / "profiles" / "work"
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(profile_root),
        auth_json_path=str(auth_path),
    )
    codex = _executable(tmp_path / "codex")
    helper = _executable(tmp_path / "codex-usage-browser")
    browser = _executable(tmp_path / "vivaldi-stable")
    monkeypatch.setattr(
        "codex_usage.reactivate._select_browser",
        lambda requested: ("vivaldi", browser),
    )

    def fake_popen(argv, **kwargs):
        def write_auth():
            auth_path.write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "account_id": "account-new",
                            "access_token": _jwt_with_exp(expiry),
                        },
                    }
                ),
                encoding="utf-8",
            )
            auth_path.chmod(0o600)

        return _fake_login_popen(write_auth)

    monkeypatch.setattr("codex_usage.reactivate.subprocess.Popen", fake_popen)

    with pytest.raises(ReactivationError, match="different account"):
        reactivate_account(account, codex_command=codex, browser_helper=helper)

    assert auth_path.read_text(encoding="utf-8") == old_raw
    assert auth_path.stat().st_mode & 0o777 == 0o600


def test_reactivate_rejects_same_account_id_with_different_user_id(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "account_id": "account-old",
                    "access_token": _jwt_with_user_id("user-new"),
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    with pytest.raises(ReactivationError, match="different account"):
        _validate_refreshed_identity(
            auth_path,
            ("user-old", "account-old"),
        )


def test_reactivate_rejects_missing_user_id_even_when_account_matches(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "account_id": "account-old",
                    "access_token": _jwt_with_exp(
                        int((datetime.now(UTC) + timedelta(days=10)).timestamp())
                    ),
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    with pytest.raises(ReactivationError, match="different account"):
        _validate_refreshed_identity(
            auth_path,
            ("user-old", "account-old"),
        )


def test_reactivate_rejects_missing_account_id_even_when_user_matches(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": _jwt_with_user_id("user-old"),
                }
            }
        ),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    with pytest.raises(ReactivationError, match="different account"):
        _validate_refreshed_identity(
            auth_path,
            ("user-old", "account-old"),
        )


def test_reactivate_login_failure_restores_auth_json(tmp_path, monkeypatch):
    auth_home = tmp_path / "agent-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    expiry = int((datetime.now(UTC) + timedelta(days=10)).timestamp())
    old_raw = json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "account_id": "account-old",
                "access_token": _jwt_with_exp(expiry),
            },
        }
    )
    auth_path.write_text(old_raw, encoding="utf-8")
    auth_path.chmod(0o600)
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
        auth_json_path=str(auth_path),
    )
    codex = _executable(tmp_path / "codex")
    helper = _executable(tmp_path / "codex-usage-browser")
    browser = _executable(tmp_path / "vivaldi-stable")
    monkeypatch.setattr(
        "codex_usage.reactivate._select_browser",
        lambda requested: ("vivaldi", browser),
    )

    def fake_popen(argv, **kwargs):
        def write_auth():
            auth_path.write_text('{"partial": true}\n', encoding="utf-8")
            auth_path.chmod(0o600)

        return _fake_login_popen(write_auth, returncode=1)

    monkeypatch.setattr("codex_usage.reactivate.subprocess.Popen", fake_popen)

    with pytest.raises(ReactivationError, match="exit code 1"):
        reactivate_account(account, codex_command=codex, browser_helper=helper)

    assert auth_path.read_text(encoding="utf-8") == old_raw
    assert auth_path.stat().st_mode & 0o777 == 0o600


def test_validate_refreshed_auth_rejects_empty_access_token(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": ""}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    with pytest.raises(ReactivationError, match=r"without a valid auth\.json"):
        _validate_refreshed_auth(auth_path)


def test_validate_refreshed_auth_rejects_malformed_access_token(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "bad token"}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    with pytest.raises(ReactivationError, match=r"without a valid auth\.json"):
        _validate_refreshed_auth(auth_path)


def test_oauth_browser_launches_vivaldi_with_isolated_profile(tmp_path, monkeypatch):
    executable = Path(_executable(tmp_path / "vivaldi-stable"))
    profile = tmp_path / "oauth-profile"
    profile.mkdir()
    profile.chmod(0o700)
    marker = profile / OAUTH_PROFILE_MARKER
    marker.write_text("{}\n", encoding="utf-8")
    marker.chmod(0o600)
    monkeypatch.setenv("CODEX_USAGE_BROWSER_EXECUTABLE", str(executable))
    monkeypatch.setenv("CODEX_USAGE_BROWSER_KIND", "vivaldi")
    monkeypatch.setenv("CODEX_USAGE_BROWSER_PROFILE", str(profile))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "must-not-leak"))

    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return object()

    monkeypatch.setattr(oauth_browser.subprocess, "Popen", fake_popen)

    assert oauth_browser.main(["https://auth.openai.com/oauth/authorize?client_id=test"]) == 0
    assert captured["command"][0] == str(executable)
    assert f"--user-data-dir={profile}" in captured["command"]
    assert captured["command"][-1].startswith("https://auth.openai.com/")
    assert "CODEX_HOME" not in captured["env"]


def test_oauth_browser_requires_exactly_one_login_url(capsys):
    assert oauth_browser.main([]) == 2
    assert "exactly one login URL" in capsys.readouterr().err


def test_oauth_browser_rejects_overlong_login_url():
    with pytest.raises(ValueError, match="URL is too long"):
        oauth_browser._validate_login_url("https://auth.openai.com/" + "x" * 8192)


def test_oauth_browser_rejects_invalid_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_USAGE_BROWSER_KIND", "unsupported")
    monkeypatch.setenv("CODEX_USAGE_BROWSER_EXECUTABLE", str(tmp_path / "missing"))
    monkeypatch.setenv("CODEX_USAGE_BROWSER_PROFILE", str(tmp_path / "profile"))

    with pytest.raises(ValueError, match="browser kind"):
        oauth_browser._browser_configuration()


def test_oauth_browser_rejects_missing_and_non_executable_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_USAGE_BROWSER_KIND", "vivaldi")
    monkeypatch.setenv("CODEX_USAGE_BROWSER_PROFILE", str(tmp_path / "profile"))
    monkeypatch.setenv("CODEX_USAGE_BROWSER_EXECUTABLE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="browser executable"):
        oauth_browser._browser_configuration()

    executable = tmp_path / "browser"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o600)
    monkeypatch.setenv("CODEX_USAGE_BROWSER_EXECUTABLE", str(executable))
    with pytest.raises(ValueError, match="not executable"):
        oauth_browser._browser_configuration()


def test_oauth_browser_rejects_relative_profile(tmp_path, monkeypatch):
    executable = Path(_executable(tmp_path / "vivaldi-stable"))
    monkeypatch.setenv("CODEX_USAGE_BROWSER_KIND", "vivaldi")
    monkeypatch.setenv("CODEX_USAGE_BROWSER_EXECUTABLE", str(executable))
    monkeypatch.setenv("CODEX_USAGE_BROWSER_PROFILE", "relative-profile")

    with pytest.raises(ValueError, match="browser profile"):
        oauth_browser._browser_configuration()


def test_oauth_browser_builds_firefox_command(tmp_path):
    command = oauth_browser._browser_command(
        "/usr/bin/firefox",
        "firefox",
        tmp_path / "profile",
        "https://auth.openai.com/login",
    )

    assert command == [
        "/usr/bin/firefox",
        "-no-remote",
        "-profile",
        str(tmp_path / "profile"),
        "-new-window",
        "https://auth.openai.com/login",
    ]


def test_oauth_browser_rejects_non_private_profile(tmp_path, monkeypatch):
    executable = Path(_executable(tmp_path / "vivaldi-stable"))
    profile = tmp_path / "oauth-profile"
    profile.mkdir()
    marker = profile / OAUTH_PROFILE_MARKER
    marker.write_text("{}\n", encoding="utf-8")
    profile.chmod(0o755)
    marker.chmod(0o600)
    monkeypatch.setenv("CODEX_USAGE_BROWSER_EXECUTABLE", str(executable))
    monkeypatch.setenv("CODEX_USAGE_BROWSER_KIND", "vivaldi")
    monkeypatch.setenv("CODEX_USAGE_BROWSER_PROFILE", str(profile))
    monkeypatch.setattr(
        oauth_browser.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("browser must not start"),
    )

    assert oauth_browser.main(["https://auth.openai.com/login"]) == 1


def test_oauth_browser_rejects_foreign_profile_owner(tmp_path, monkeypatch):
    executable = Path(_executable(tmp_path / "vivaldi-stable"))
    profile = tmp_path / "oauth-profile"
    profile.mkdir()
    profile.chmod(0o700)
    marker = profile / OAUTH_PROFILE_MARKER
    marker.write_text("{}\n", encoding="utf-8")
    marker.chmod(0o600)
    monkeypatch.setenv("CODEX_USAGE_BROWSER_EXECUTABLE", str(executable))
    monkeypatch.setenv("CODEX_USAGE_BROWSER_KIND", "vivaldi")
    monkeypatch.setenv("CODEX_USAGE_BROWSER_PROFILE", str(profile))
    monkeypatch.setattr(oauth_browser.os, "getuid", lambda: profile.stat().st_uid + 1)
    monkeypatch.setattr(
        oauth_browser.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("browser must not start"),
    )

    assert oauth_browser.main(["https://auth.openai.com/login"]) == 1


def test_oauth_browser_rejects_non_private_marker(tmp_path, monkeypatch):
    executable = Path(_executable(tmp_path / "vivaldi-stable"))
    profile = tmp_path / "oauth-profile"
    profile.mkdir()
    profile.chmod(0o700)
    marker = profile / OAUTH_PROFILE_MARKER
    marker.write_text("{}\n", encoding="utf-8")
    marker.chmod(0o644)
    monkeypatch.setenv("CODEX_USAGE_BROWSER_EXECUTABLE", str(executable))
    monkeypatch.setenv("CODEX_USAGE_BROWSER_KIND", "vivaldi")
    monkeypatch.setenv("CODEX_USAGE_BROWSER_PROFILE", str(profile))
    monkeypatch.setattr(
        oauth_browser.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("browser must not start"),
    )

    assert oauth_browser.main(["https://auth.openai.com/login"]) == 1


@pytest.mark.parametrize("symlink_marker", [OAUTH_PROFILE_MARKER, ".codex-usage-browser-profile"])
def test_oauth_browser_rejects_any_symlinked_marker(tmp_path, monkeypatch, symlink_marker):
    executable = Path(_executable(tmp_path / "vivaldi-stable"))
    profile = tmp_path / "oauth-profile"
    profile.mkdir()
    profile.chmod(0o700)
    outside = tmp_path / "outside-marker"
    outside.write_text("keep\n", encoding="utf-8")
    symlink = profile / symlink_marker
    symlink.symlink_to(outside)
    regular_marker = profile / (
        ".codex-usage-browser-profile"
        if symlink_marker == OAUTH_PROFILE_MARKER
        else OAUTH_PROFILE_MARKER
    )
    regular_marker.write_text("{}\n", encoding="utf-8")
    regular_marker.chmod(0o600)
    monkeypatch.setenv("CODEX_USAGE_BROWSER_EXECUTABLE", str(executable))
    monkeypatch.setenv("CODEX_USAGE_BROWSER_KIND", "vivaldi")
    monkeypatch.setenv("CODEX_USAGE_BROWSER_PROFILE", str(profile))
    monkeypatch.setattr(
        oauth_browser.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("browser must not start"),
    )

    assert oauth_browser.main(["https://auth.openai.com/login"]) == 1
    assert outside.read_text(encoding="utf-8") == "keep\n"


def test_oauth_browser_rejects_profile_symlink_ancestor(tmp_path, monkeypatch):
    executable = Path(_executable(tmp_path / "vivaldi-stable"))
    real_parent = tmp_path / "real-parent"
    profile = real_parent / "oauth-profile"
    profile.mkdir(parents=True)
    profile.chmod(0o700)
    marker = profile / OAUTH_PROFILE_MARKER
    marker.write_text("{}\n", encoding="utf-8")
    marker.chmod(0o600)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    monkeypatch.setenv("CODEX_USAGE_BROWSER_EXECUTABLE", str(executable))
    monkeypatch.setenv("CODEX_USAGE_BROWSER_KIND", "vivaldi")
    monkeypatch.setenv(
        "CODEX_USAGE_BROWSER_PROFILE",
        str(linked_parent / "oauth-profile"),
    )
    monkeypatch.setattr(
        oauth_browser.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("browser must not start"),
    )

    assert oauth_browser.main(["https://auth.openai.com/login"]) == 1


def test_oauth_browser_rejects_non_openai_url(tmp_path, monkeypatch, capsys):
    assert oauth_browser.main(["https://example.com/login"]) == 1
    assert "refusing non-OpenAI" in capsys.readouterr().err


@pytest.mark.parametrize("url", [None, [], {}])
def test_oauth_browser_rejects_non_string_url(url):
    assert oauth_browser.main([url]) == 1


@pytest.mark.parametrize("argv", [1, object()])
def test_oauth_browser_rejects_non_sequence_argv(argv):
    assert oauth_browser.main(argv) == 2  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    [
        "https://auth.openai.com:8443/login",
        "https://auth.openai.com:invalid/login",
    ],
)
def test_oauth_browser_rejects_nonstandard_or_invalid_port(url, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_USAGE_BROWSER_KIND", "firefox")
    monkeypatch.setenv("CODEX_USAGE_BROWSER_EXECUTABLE", "/usr/bin/firefox")
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    marker = profile / OAUTH_PROFILE_MARKER
    marker.write_text("marker\n", encoding="utf-8")
    marker.chmod(0o600)
    monkeypatch.setenv("CODEX_USAGE_BROWSER_PROFILE", str(profile))
    monkeypatch.setattr(
        oauth_browser.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("browser must not start"),
    )

    assert oauth_browser.main([url]) == 1


def test_oauth_browser_module_main_guard_executes(monkeypatch):
    import runpy

    monkeypatch.setattr(sys, "argv", ["oauth-browser"])

    with pytest.raises(SystemExit) as error:
        runpy.run_module("codex_usage.oauth_browser", run_name="__main__")

    assert error.value.code == 2


def test_reactivate_rejects_symlink_in_profile_path(tmp_path):
    auth_home = tmp_path / "agent-home"
    auth_home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    profile_link = tmp_path / "profiles"
    profile_link.symlink_to(outside, target_is_directory=True)
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(profile_link / "work"),
        auth_json_path=str(auth_home / "auth.json"),
    )

    with pytest.raises(ReactivationError, match="must not contain symlinks"):
        reactivate_account(account)

    assert not (outside / "work").exists()


def test_reactivate_rejects_symlink_in_auth_parent(tmp_path):
    real_auth_home = tmp_path / "real-agent-home"
    real_auth_home.mkdir()
    auth_home = tmp_path / "agent-home"
    auth_home.symlink_to(real_auth_home, target_is_directory=True)
    profile_root = tmp_path / "profiles" / "work"
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(profile_root),
        auth_json_path=str(auth_home / "auth.json"),
    )

    with pytest.raises(ReactivationError, match="must not contain symlinks"):
        reactivate_account(account)

    assert not profile_root.exists()


def test_reactivate_cli_renders_json(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "agent" / "auth.json"),
    )
    save_config(AppConfig(accounts=(account,)), config_path)
    monkeypatch.setattr(
        "codex_usage.cli.reactivate_account",
        lambda selected, browser: {
            "ok": True,
            "account": selected.id,
            "label": selected.label,
            "browser": browser,
            "auth_updated": True,
            "auth_access_expires_at": None,
        },
    )

    assert (
        main(
            [
                "--config",
                str(config_path),
                "reactivate",
                "work",
                "--browser",
                "vivaldi",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": True,
        "account": "work",
        "label": "Work",
        "browser": "vivaldi",
        "auth_updated": True,
        "auth_access_expires_at": None,
        "auth_sync_required": True,
    }
