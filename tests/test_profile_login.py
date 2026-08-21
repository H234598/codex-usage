from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

import pytest

import codex_usage.profile_login as profile_login
from codex_usage.config import AppConfig
from codex_usage.models import Account
from codex_usage.profile_login import (
    DeviceLoginError,
    _run_command,
    _terminate_bounded_process,
    device_auth_supported,
    run_device_login,
)


def _account(profile: Path) -> Account:
    return Account(id="alpha", label="Alpha", profile_dir=str(profile))


def test_device_events_parse_current_ansi_codex_prompt():
    output = (
        "1. Open this link in your browser and sign in to your account\n"
        "   \x1b[34mhttps://auth.openai.com/codex/device\x1b[0m\n"
        "2. Enter this one-time code \x1b[2m(expires in 15 minutes)\x1b[0m\n"
        "   \x1b[34mABCD-1234\x1b[0m\n"
    )

    assert [
        (event.kind, event.value)
        for event in profile_login._device_events(output)
    ] == [
        ("url", "https://auth.openai.com/codex/device"),
        ("code", "ABCD-1234"),
    ]


@pytest.mark.parametrize("control", ["\x07", "\x1b", "\x7f", "\x85", "\x9f"])
def test_device_events_stop_urls_before_control_characters(control):
    output = f"Open https://auth.openai.com/codex/device{control}hidden\n"

    assert [
        (event.kind, event.value)
        for event in profile_login._device_events(output)
    ] == [("url", "https://auth.openai.com/codex/device")]


def test_device_events_reject_overlong_urls_without_prefix_match():
    output = f"Open https://{'a' * 481}\n"

    assert profile_login._device_events(output) == ()


def test_device_events_defer_trailing_live_tokens_until_final_chunk():
    assert profile_login._device_events(
        "Open https://auth.example/device", final=False
    ) == ()
    assert profile_login._device_events(
        "Enter device code: ABCD-1234", final=False
    ) == ()
    assert [event.value for event in profile_login._device_events(
        "Enter device code: ABCD-1234\n", final=False
    )] == ["ABCD-1234"]


def test_device_events_cap_unique_events_at_eight():
    output = " ".join([
        "https://auth.example/device/0",
        "https://auth.example/device/1",
        "https://auth.example/device/2",
        "https://auth.example/device/3",
        "https://auth.example/device/3",
        "https://auth.example/device/4",
        "https://auth.example/device/5",
        "https://auth.example/device/6",
        "https://auth.example/device/7",
        "https://auth.example/device/8",
    ])

    assert [event.value for event in profile_login._device_events(output)] == [
        "https://auth.example/device/0",
        "https://auth.example/device/1",
        "https://auth.example/device/2",
        "https://auth.example/device/3",
        "https://auth.example/device/4",
        "https://auth.example/device/5",
        "https://auth.example/device/6",
        "https://auth.example/device/7",
    ]


def test_device_events_ignore_generic_and_malformed_diagnostic_codes():
    output = (
        "error code: E1234\n"
        "exit code: EXIT-7\n"
        "code: ABCD-1234\n"
        "kode: WXYZ-9876\n"
        "device\ncode: SPLIT-1\n"
        "one-time code\n\nABCD-1234\n"
        f"device code: {'A' * 129}\n"
    )

    assert profile_login._device_events(output) == ()


def test_device_login_uses_staging_home_and_publishes_auth_atomically(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    config = tmp_path / "config.toml"
    calls = []

    def runner(argv, *, env, timeout):
        calls.append((argv, env, timeout))
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "Usage: codex login --device-auth", "")
        home = Path(env["CODEX_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        auth = home / "auth.json"
        auth.write_text(
            '{"auth_mode":"chatgpt","tokens":{"access_token":"test-token"}}',
            encoding="utf-8",
        )
        auth.chmod(0o600)
        return subprocess.CompletedProcess(
            argv,
            0,
            "Open https://auth.openai.com/device and enter device code: ABCD-1234",
            "",
        )

    monkeypatch.setattr(
        "codex_usage.profile_login.account_lock", lambda _account: nullcontext()
    )
    monkeypatch.setattr(
        "codex_usage.profile_login.add_or_update_account", lambda *args, **kwargs: None
    )
    result = run_device_login(_account(profile), config, runner=runner)

    assert result.ok is True
    assert [(event.kind, event.value) for event in result.events] == [
        ("url", "https://auth.openai.com/device"),
        ("code", "ABCD-1234"),
    ]
    assert (profile / "codex-home" / "auth.json").read_text(encoding="utf-8")
    assert (profile / "codex-home" / "auth.json").stat().st_mode & 0o777 == 0o600
    assert not list(profile.glob(".device-login-staging/job-*/codex-home/auth.json"))
    assert calls[1][1]["CODEX_HOME"].startswith(str(profile / ".device-login-staging"))
    assert "OPENAI_API_KEY" not in calls[1][1]


def test_staging_root_binds_mode_change_to_checked_directory(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    staging_parent = profile / ".device-login-staging"
    staging_parent.mkdir(mode=0o755)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    layout = profile_login.ProfileLayout(
        account_id="alpha",
        profile_dir=profile,
        codex_home=profile / "codex-home",
        auth_json=profile / "codex-home" / "auth.json",
        metadata=profile / "profile.json",
        jobs=profile / "jobs",
        migration=profile / "migration",
    )
    original_chmod = Path.chmod

    def replace_target_before_path_chmod(path, mode):
        if path == staging_parent:
            staging_parent.rmdir()
            staging_parent.symlink_to(outside, target_is_directory=True)
        return original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", replace_target_before_path_chmod)
    job = profile_login._create_staging_root(layout)
    try:
        assert staging_parent.is_dir() and not staging_parent.is_symlink()
        assert job.parent == staging_parent
        assert staging_parent.stat().st_mode & 0o777 == 0o700
        assert outside.stat().st_mode & 0o777 == 0o755
    finally:
        shutil.rmtree(job, ignore_errors=True)


def test_device_login_reports_staging_cleanup_failure(tmp_path, monkeypatch):
    profile = tmp_path / "profile"

    def runner(argv, *, env, timeout):
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--device-auth", "")
        auth = Path(env["CODEX_HOME"]) / "auth.json"
        auth.write_text('{"tokens":{"access_token":"test-token"}}', encoding="utf-8")
        auth.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def fail_cleanup(*args, **kwargs):
        raise OSError("staging cleanup failed")

    monkeypatch.setattr("codex_usage.profile_login.account_lock", lambda _account: nullcontext())
    monkeypatch.setattr(
        "codex_usage.profile_login.add_or_update_account",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("codex_usage.profile_login.shutil.rmtree", fail_cleanup)

    with pytest.raises(DeviceLoginError, match="device_login_cleanup_failed"):
        run_device_login(_account(profile), tmp_path / "config.toml", runner=runner)

    assert not (profile / "codex-home" / "auth.json").exists()


def test_device_login_preserves_login_failure_when_staging_cleanup_fails(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"

    def runner(argv, *, env, timeout):
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--device-auth", "")
        return subprocess.CompletedProcess(argv, 1, "device auth failed", "")

    monkeypatch.setattr(
        "codex_usage.profile_login.account_lock", lambda _account: nullcontext()
    )
    def fail_cleanup(*args, **kwargs):
        raise OSError("cleanup failed")

    monkeypatch.setattr("codex_usage.profile_login.shutil.rmtree", fail_cleanup)

    result = run_device_login(
        _account(profile), tmp_path / "config.toml", runner=runner
    )

    assert result.ok is False
    assert result.error == "device_login_failed"


def test_device_login_fails_closed_when_capability_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("codex_usage.profile_login.account_lock", lambda _account: nullcontext())
    result = run_device_login(
        _account(tmp_path / "profile"),
        tmp_path / "config.toml",
        runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "no device auth", ""),
    )
    assert result.ok is False
    assert result.error == "device_auth_unavailable"
    assert not (tmp_path / "profile" / ".device-login-staging").exists()


@pytest.mark.parametrize("account_id", [None, [], "../escape", "__all_accounts__"])
def test_device_login_rejects_invalid_account_id(account_id, tmp_path):
    account = Account(
        id=account_id,
        label="Alpha",
        profile_dir=str(tmp_path / "profile"),
    )

    with pytest.raises(DeviceLoginError, match="account id is invalid"):
        run_device_login(account, tmp_path / "config.toml")


def test_device_login_finalize_preserves_account_options(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    config = tmp_path / "config.toml"
    observed = {}

    def runner(argv, *, env, timeout):
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--device-auth", "")
        home = Path(env["CODEX_HOME"])
        auth = home / "auth.json"
        auth.write_text('{"tokens":{"access_token":"test-token"}}', encoding="utf-8")
        auth.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def capture_finalize(*args, **kwargs):
        observed.update(kwargs)

    monkeypatch.setattr("codex_usage.profile_login.account_lock", lambda _account: nullcontext())
    monkeypatch.setattr("codex_usage.profile_login.add_or_update_account", capture_finalize)

    result = run_device_login(
        Account(
            id="alpha",
            label="Personal",
            profile_dir=str(profile),
            browser="chromium",
            backend="app-server",
            reactivation_browser="firefox",
        ),
        config,
        runner=runner,
    )

    assert result.ok is True
    assert observed == {
        "label": "Personal",
        "profile_dir": str(profile),
        "browser": "chromium",
        "backend": "app-server",
        "reactivation_browser": "firefox",
        "auth_json_path": str(profile / "codex-home" / "auth.json"),
        "path": config,
        "_all_accounts_lock_held": True,
    }


def test_device_login_finalization_holds_global_account_lock(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    config = tmp_path / "config.toml"
    account = _account(profile)
    held_locks = []

    class FakeLock:
        def __init__(self, account_id):
            self.account_id = account_id

        def __enter__(self):
            held_locks.append(self.account_id)

        def __exit__(self, exc_type, exc_value, traceback):
            held_locks.pop()

    def runner(argv, *, env, timeout):
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--device-auth", "")
        auth = Path(env["CODEX_HOME"]) / "auth.json"
        auth.write_text('{"tokens":{"access_token":"test-token"}}', encoding="utf-8")
        auth.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def capture_finalize(*args, **kwargs):
        assert held_locks == ["__all_accounts__"]
        assert kwargs["_all_accounts_lock_held"] is True

    monkeypatch.setattr(profile_login, "account_lock", FakeLock)
    monkeypatch.setattr(
        profile_login, "load_config", lambda path: AppConfig((account,)), raising=False
    )
    monkeypatch.setattr(profile_login, "add_or_update_account", capture_finalize)

    result = run_device_login(account, config, runner=runner)

    assert result.ok is True


def test_device_login_does_not_recreate_account_after_config_change(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    config = tmp_path / "config.toml"
    account = _account(profile)
    config.write_text("", encoding="utf-8")

    def runner(argv, *, env, timeout):
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--device-auth", "")
        auth = Path(env["CODEX_HOME"]) / "auth.json"
        auth.write_text('{"tokens":{"access_token":"test-token"}}', encoding="utf-8")
        auth.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(profile_login, "account_lock", lambda _account: nullcontext())
    monkeypatch.setattr(profile_login, "load_config", lambda path: AppConfig(()))

    with pytest.raises(DeviceLoginError, match="device_login_finalize_failed"):
        run_device_login(account, config, runner=runner)

    assert not (profile / "codex-home" / "auth.json").exists()


def test_device_login_rejects_mismatched_expected_backend_account(tmp_path, monkeypatch):
    profile = tmp_path / "profile"

    def runner(argv, *, env, timeout):
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--device-auth", "")
        auth = Path(env["CODEX_HOME"]) / "auth.json"
        auth.write_text(
            '{"tokens":{"account_id":"backend-other","access_token":"test-token"}}',
            encoding="utf-8",
        )
        auth.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("codex_usage.profile_login.account_lock", lambda _account: nullcontext())

    with pytest.raises(DeviceLoginError, match="identity"):
        run_device_login(
            _account(profile),
            tmp_path / "config.toml",
            runner=runner,
            expected_backend_account_id="backend-alpha",
        )

    assert not (profile / "codex-home" / "auth.json").exists()


def test_device_login_rejects_auth_without_access_token(tmp_path, monkeypatch):
    profile = tmp_path / "profile"

    def runner(argv, *, env, timeout):
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--device-auth", "")
        auth = Path(env["CODEX_HOME"]) / "auth.json"
        auth.write_text("{}", encoding="utf-8")
        auth.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("codex_usage.profile_login.account_lock", lambda _account: nullcontext())

    with pytest.raises(DeviceLoginError, match="device_auth_invalid"):
        run_device_login(_account(profile), tmp_path / "config.toml", runner=runner)

    assert not (profile / "codex-home" / "auth.json").exists()


def test_device_login_rechecks_auth_identity_when_staged_file_changes(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"

    def runner(argv, *, env, timeout):
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--device-auth", "")
        auth = Path(env["CODEX_HOME"]) / "auth.json"
        auth.write_text(
            '{"tokens":{"account_id":"backend-alpha","access_token":"test-token"}}',
            encoding="utf-8",
        )
        auth.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    original_copy = profile_login._copy_private_file

    def race_copy(source, target, **kwargs):
        source.write_text(
            '{"tokens":{"account_id":"backend-other","access_token":"test-token"}}',
            encoding="utf-8",
        )
        source.chmod(0o600)
        return original_copy(source, target, **kwargs)

    monkeypatch.setattr("codex_usage.profile_login.account_lock", lambda _account: nullcontext())
    monkeypatch.setattr("codex_usage.profile_login._copy_private_file", race_copy)

    with pytest.raises(DeviceLoginError, match="device_auth_identity_mismatch"):
        run_device_login(
            _account(profile),
            tmp_path / "config.toml",
            runner=runner,
            expected_backend_account_id="backend-alpha",
        )

    assert not (profile / "codex-home" / "auth.json").exists()


def test_device_login_live_events_do_not_join_output_streams(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    observed_events = []

    def runner(argv, **kwargs):
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--device-auth", "")
        kwargs["output_stream_sink"]("stderr", "Enter device code: ABCD")
        kwargs["output_stream_sink"]("stdout", "-1234\n")
        auth = Path(kwargs["env"]["CODEX_HOME"]) / "auth.json"
        auth.write_text('{"tokens":{"access_token":"test-token"}}', encoding="utf-8")
        auth.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("codex_usage.profile_login.account_lock", lambda _account: nullcontext())
    monkeypatch.setattr(
        "codex_usage.profile_login.add_or_update_account",
        lambda *args, **kwargs: None,
    )

    result = run_device_login(
        _account(profile),
        tmp_path / "config.toml",
        runner=runner,
        event_sink=observed_events.append,
    )

    assert result.events == ()
    assert observed_events == []


def test_device_auth_help_probe_requires_explicit_flag():
    assert device_auth_supported(
        runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "login help", "")
    ) is False


@pytest.mark.parametrize("codex_bin", [None, [], 0, False, ""])
def test_device_auth_supported_rejects_invalid_codex_command(codex_bin):
    with pytest.raises(DeviceLoginError, match="codex command is invalid"):
        device_auth_supported(
            codex_bin,
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 0, "--device-auth", ""
            ),
        )


def test_device_login_removes_published_auth_when_config_finalize_fails(tmp_path, monkeypatch):
    profile = tmp_path / "profile"

    def runner(argv, *, env, timeout):
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--device-auth", "")
        home = Path(env["CODEX_HOME"])
        auth = home / "auth.json"
        auth.write_text('{"tokens":{"access_token":"test-token"}}', encoding="utf-8")
        auth.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def fail_finalize(*args, **kwargs):
        raise ValueError("config failure")

    monkeypatch.setattr("codex_usage.profile_login.account_lock", lambda _account: nullcontext())
    monkeypatch.setattr("codex_usage.profile_login.add_or_update_account", fail_finalize)
    try:
        run_device_login(_account(profile), tmp_path / "config.toml", runner=runner)
    except Exception as exc:
        assert str(exc) == "device_login_finalize_failed"
    else:
        raise AssertionError("finalize failure unexpectedly succeeded")
    assert not (profile / "codex-home" / "auth.json").exists()


def test_device_login_removes_auth_when_canonical_config_publish_fails(tmp_path, monkeypatch):
    profile = tmp_path / "profile"

    def runner(argv, *, env, timeout):
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--device-auth", "")
        home = Path(env["CODEX_HOME"])
        auth = home / "auth.json"
        auth.write_text('{"tokens":{"access_token":"test-token"}}', encoding="utf-8")
        auth.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    original_write_file_store_config = profile_login._write_file_store_config

    def fail_config_publish(path):
        if path == profile / "codex-home" / "config.toml":
            raise OSError("config publish failure")
        return original_write_file_store_config(path)

    monkeypatch.setattr("codex_usage.profile_login.account_lock", lambda _account: nullcontext())
    monkeypatch.setattr("codex_usage.profile_login._write_file_store_config", fail_config_publish)

    with pytest.raises(DeviceLoginError, match="device_login_io_failed"):
        run_device_login(_account(profile), tmp_path / "config.toml", runner=runner)

    assert not (profile / "codex-home" / "auth.json").exists()


def test_device_login_preserves_existing_canonical_auth_on_failure(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    canonical = profile / "codex-home" / "auth.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text('{"existing":true}', encoding="utf-8")
    canonical.chmod(0o600)

    def runner(argv, *, env, timeout):
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--device-auth", "")
        staged = Path(env["CODEX_HOME"]) / "auth.json"
        staged.write_text('{"tokens":{"access_token":"test-token"}}', encoding="utf-8")
        staged.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("codex_usage.profile_login.account_lock", lambda _account: nullcontext())

    with pytest.raises(DeviceLoginError, match="already exists"):
        run_device_login(_account(profile), tmp_path / "config.toml", runner=runner)

    assert canonical.read_text(encoding="utf-8") == '{"existing":true}'


def test_device_login_does_not_overwrite_auth_created_after_existence_check(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    canonical = profile / "codex-home" / "auth.json"
    original_copy = profile_login._copy_private_file

    def runner(argv, *, env, timeout):
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--device-auth", "")
        staged = Path(env["CODEX_HOME"]) / "auth.json"
        staged.write_text('{"tokens":{"access_token":"test-token"}}', encoding="utf-8")
        staged.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def race_copy(source, target, **kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"racing":true}', encoding="utf-8")
        target.chmod(0o600)
        original_copy(source, target, **kwargs)

    monkeypatch.setattr("codex_usage.profile_login.account_lock", lambda _account: nullcontext())
    monkeypatch.setattr("codex_usage.profile_login._copy_private_file", race_copy)

    with pytest.raises(DeviceLoginError, match="device_login_io_failed"):
        run_device_login(_account(profile), tmp_path / "config.toml", runner=runner)

    assert canonical.read_text(encoding="utf-8") == '{"racing":true}'


def test_default_device_command_reader_rejects_unbounded_output():
    with pytest.raises(DeviceLoginError, match="output"):
        _run_command(
            [sys.executable, "-c", "print('x' * 100000)"],
            env={},
            timeout=10,
            runner=None,
        )


def test_default_device_command_reader_forwards_output_chunks():
    chunks = []
    result = _run_command(
        [
            sys.executable,
            "-c",
            "print('Open https://auth.openai.com/device and device code: ABCD-1234')",
        ],
        env={},
        timeout=10,
        runner=None,
        output_sink=chunks.append,
    )

    assert result.returncode == 0
    assert "https://auth.openai.com/device" in "".join(chunks)


def test_default_device_command_reader_forwards_named_output_streams():
    chunks = {"stdout": [], "stderr": []}
    result = _run_command(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'out'); os.write(2, b'err')",
        ],
        env={},
        timeout=10,
        runner=None,
        output_stream_sink=lambda name, chunk: chunks[name].append(chunk),
    )

    assert result.returncode == 0
    assert "".join(chunks["stdout"]) == "out"
    assert "".join(chunks["stderr"]) == "err"


def test_default_device_command_reader_preserves_split_utf8_output():
    chunks = []
    result = _run_command(
        [
            sys.executable,
            "-c",
            (
                "import os, time; os.write(1, bytes([195])); "
                "time.sleep(0.05); os.write(1, bytes([132]))"
            ),
        ],
        env={},
        timeout=10,
        runner=None,
        output_sink=chunks.append,
    )

    assert result.returncode == 0
    assert "".join(chunks) == "Ä"


def test_bounded_process_cleanup_does_not_signal_parent_process_group(monkeypatch):
    signals = []

    class FakeProcess:
        pid = 1234

        def kill(self):
            signals.append("kill")

        def wait(self, timeout=None):
            return -9

    monkeypatch.setattr(
        "codex_usage.profile_login.os.killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )

    _terminate_bounded_process(FakeProcess(), start_new_session=False)

    assert signals == ["kill"]


def test_bounded_device_login_kills_process_when_output_sink_fails(tmp_path):
    pid_path = tmp_path / "process.pid"
    script = (
        "import os, sys, time\n"
        "with open(sys.argv[1], 'w', encoding='ascii') as handle:\n"
        "    handle.write(str(os.getpid()))\n"
        "    handle.flush()\n"
        "print('device event', flush=True)\n"
        "time.sleep(30)\n"
    )

    def fail_sink(_chunk):
        raise RuntimeError("event sink failed")

    process_pid = None
    try:
        with pytest.raises(RuntimeError, match="event sink failed"):
            _run_command(
                [sys.executable, "-c", script, str(pid_path)],
                env={},
                timeout=10,
                runner=None,
                output_sink=fail_sink,
            )
        pid_deadline = time.monotonic() + 2
        while time.monotonic() < pid_deadline and not pid_path.exists():
            time.sleep(0.01)
        if not pid_path.exists():
            pytest.fail("device-login process never reported its pid")
        process_pid = int(pid_path.read_text(encoding="ascii"))
        exit_deadline = time.monotonic() + 2
        while time.monotonic() < exit_deadline:
            try:
                os.kill(process_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            pytest.fail("output-sink failure left device-login process running")
    finally:
        if process_pid is not None:
            try:
                os.kill(process_pid, 9)
            except ProcessLookupError:
                pass


def test_bounded_device_login_kills_descendant_processes_on_timeout(tmp_path):
    child_pid_path = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "with open(sys.argv[1], 'w', encoding='ascii') as handle:\n"
        "    handle.write(str(child.pid))\n"
        "    handle.flush()\n"
        "time.sleep(30)\n"
    )
    child_pid = None
    try:
        with pytest.raises(DeviceLoginError, match="timeout"):
            _run_command(
                [sys.executable, "-c", script, str(child_pid_path)],
                env={},
                timeout=1,
                runner=None,
            )
        pid_deadline = time.monotonic() + 2
        while time.monotonic() < pid_deadline and not child_pid_path.exists():
            time.sleep(0.01)
        if not child_pid_path.exists():
            pytest.fail("device-login child never reported its pid")
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        exit_deadline = time.monotonic() + 2
        while time.monotonic() < exit_deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            pytest.fail("device-login timeout left descendant process running")
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass


def test_bounded_device_login_applies_timeout_after_output_eof(monkeypatch):
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    os.close(stdout_write)
    os.close(stderr_write)
    observed = []

    class FakeProcess:
        stdout = os.fdopen(stdout_read, "rb")
        stderr = os.fdopen(stderr_read, "rb")

        def wait(self, timeout=None):
            observed.append(timeout)
            return 0

    monkeypatch.setattr(
        "codex_usage.profile_login.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )

    try:
        result = _run_command(
            ["codex", "login", "--device-auth"],
            env={},
            timeout=5,
            runner=None,
        )

        assert result.returncode == 0
        assert observed and observed[0] is not None
        assert 0 <= observed[0] <= 5
    finally:
        FakeProcess.stdout.close()
        FakeProcess.stderr.close()
