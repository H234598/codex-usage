from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

from codex_usage import cli, profile_jobs
from codex_usage.config import AppConfig, add_or_update_account, load_config
from codex_usage.models import Account, AccountStatus, AccountUsage
from codex_usage.private_io import write_private_text
from codex_usage.profile_layout import ensure_profile_layout
from codex_usage.profile_login import DeviceLoginEvent, DeviceLoginResult
from codex_usage.scheduler import fetch_all


class _BrokenInt(int):
    def __gt__(self, _other):
        raise RuntimeError("synthetic profile-job integer comparison marker")

    def __le__(self, _other):
        raise RuntimeError("synthetic profile-job integer comparison marker")


def test_profile_create_cli_emits_only_job_reference(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.toml"
    observed = {}

    def fake_create_profile_job(**kwargs):
        observed.update(kwargs)
        return {"ok": True, "job_id": "job-123", "status": "queued", "account": "alpha"}

    monkeypatch.setattr(cli, "create_profile_job", fake_create_profile_job, raising=False)

    assert cli.main(
        [
            "--config",
            str(config),
            "profile",
            "create",
            "--account-id",
            "alpha",
            "--label",
            "Alpha",
            "--browser",
            "firefox",
            "--backend",
            "direct",
            "--profile-dir",
            str(tmp_path / "profile"),
            "--reactivation-browser",
            "chromium",
            "--expected-backend-account-id",
            "backend-alpha",
            "--json-events",
        ]
    ) == 0

    assert json.loads(capsys.readouterr().out) == {
        "account": "alpha",
        "job_id": "job-123",
        "ok": True,
        "status": "queued",
    }
    assert observed == {
        "account_id": "alpha",
        "label": "Alpha",
        "browser": "firefox",
        "backend": "direct",
        "profile_dir": str(tmp_path / "profile"),
        "reactivation_browser": "chromium",
        "expected_backend_account_id": "backend-alpha",
        "config_path": config,
        "json_events": True,
    }


def test_profile_create_cli_serializes_against_account_delete(tmp_path, monkeypatch, capsys):
    lock_events = []

    class FakeLock:
        def __enter__(self):
            lock_events.append("enter")

        def __exit__(self, exc_type, exc_value, traceback):
            lock_events.append("exit")

    def fake_account_lock(account_id):
        assert account_id == "__all_accounts__"
        return FakeLock()

    def fake_create_profile_job(**kwargs):
        assert lock_events == ["enter"]
        return {"ok": True, "job_id": "job-123", "status": "queued", "account": "alpha"}

    monkeypatch.setattr(cli, "account_lock", fake_account_lock)
    monkeypatch.setattr(cli, "create_profile_job", fake_create_profile_job, raising=False)

    assert cli.main(
        [
            "--config",
            str(tmp_path / "config.toml"),
            "profile",
            "create",
            "--account-id",
            "alpha",
            "--label",
            "Alpha",
            "--browser",
            "firefox",
            "--backend",
            "direct",
            "--profile-dir",
            str(tmp_path / "profile"),
        ]
    ) == 0

    assert lock_events == ["enter", "exit"]
    assert json.loads(capsys.readouterr().out)["job_id"] == "job-123"


def test_profile_job_status_cli_returns_secret_free_status(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "profile_job_status",
        lambda job_id: {"ok": True, "job_id": job_id, "status": "running", "account": "alpha"},
        raising=False,
    )

    assert cli.main(["profile", "job-status", "job-123", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "account": "alpha",
        "job_id": "job-123",
        "ok": True,
        "status": "running",
    }


def test_profile_job_cancel_cli_returns_status(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "cancel_profile_job",
        lambda job_id: {"ok": True, "job_id": job_id, "status": "cancel_requested"},
        raising=False,
    )

    assert cli.main(["profile", "cancel", "job-123", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "job_id": "job-123",
        "ok": True,
        "status": "cancel_requested",
    }


def test_profile_job_manifest_is_private_and_secret_free(tmp_path, monkeypatch):
    state = tmp_path / "state"
    calls = []

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)
    monkeypatch.setattr(
        profile_jobs.subprocess,
        "Popen",
        lambda argv, **kwargs: (calls.append((argv, kwargs)) or FakeProcess()),
    )

    result = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id="backend-alpha",
        config_path=tmp_path / "config.toml",
        json_events=True,
    )

    manifest = next((state / "profile-jobs").glob("job-*.json"))
    raw = manifest.read_text(encoding="utf-8")
    assert "auth.json" not in raw
    assert "access_token" not in raw
    assert "device code" not in raw
    assert result == {
        "account": "alpha",
        "job_id": result["job_id"],
        "ok": True,
        "status": "queued",
    }
    assert calls[0][0][-2:] == ["worker", result["job_id"]]
    assert calls[0][1]["start_new_session"] is True
    assert manifest.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "json_events",
    (
        pytest.param("false", id="string"),
        pytest.param(0, id="zero"),
        pytest.param(1, id="one"),
        pytest.param(None, id="none"),
        pytest.param([], id="list"),
        pytest.param({}, id="dict"),
    ),
)
def test_profile_job_creation_rejects_invalid_json_events_before_side_effects(
    tmp_path, monkeypatch, json_events
):
    monkeypatch.setattr(
        profile_jobs,
        "_write_new_job",
        lambda manifest: pytest.fail("manifest must not be written"),
    )
    monkeypatch.setattr(
        profile_jobs.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("worker must not start"),
    )

    with pytest.raises(ValueError, match="profile job json_events is invalid"):
        profile_jobs.create_profile_job(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir=str(tmp_path / "profile"),
            expected_backend_account_id=None,
            config_path=tmp_path / "config.toml",
            json_events=json_events,
        )


@pytest.mark.parametrize(
    "series",
    (
        pytest.param(None, id="none"),
        pytest.param(0, id="zero"),
        pytest.param(False, id="false"),
        pytest.param([], id="list"),
    ),
)
def test_profile_job_creation_rejects_non_string_series_before_side_effects(
    tmp_path, monkeypatch, series
):
    monkeypatch.setattr(
        profile_jobs,
        "_write_new_job",
        lambda manifest: pytest.fail("manifest must not be written"),
    )
    monkeypatch.setattr(
        profile_jobs.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("worker must not start"),
    )

    with pytest.raises(ValueError, match="series is invalid"):
        profile_jobs.create_profile_job(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir=str(tmp_path / "profile"),
            expected_backend_account_id=None,
            config_path=tmp_path / "config.toml",
            json_events=False,
            series=series,
        )


def test_profile_job_creation_enforces_manifest_cap_before_starting_worker(tmp_path, monkeypatch):
    state = tmp_path / "state"
    starts = []
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(
        profile_jobs.subprocess,
        "Popen",
        lambda argv, **kwargs: (starts.append(argv) or FakeProcess()),
    )

    for index in range(profile_jobs.PROFILE_JOB_MAX_RECORDS):
        profile_jobs.create_profile_job(
            account_id=f"account-{index}",
            label=f"Account {index}",
            browser="firefox",
            backend="direct",
            profile_dir=str(tmp_path / f"profile-{index}"),
            expected_backend_account_id=None,
            config_path=tmp_path / "config.toml",
            json_events=False,
        )

    with pytest.raises(ValueError, match="too many profile jobs"):
        profile_jobs.create_profile_job(
            account_id="account-over-cap",
            label="Over Cap",
            browser="firefox",
            backend="direct",
            profile_dir=str(tmp_path / "profile-over-cap"),
            expected_backend_account_id=None,
            config_path=tmp_path / "config.toml",
            json_events=False,
        )

    assert len(starts) == profile_jobs.PROFILE_JOB_MAX_RECORDS


def test_profile_job_creation_reclaims_terminal_manifest_capacity(tmp_path, monkeypatch):
    state = tmp_path / "state"
    starts = []
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(
        profile_jobs.subprocess,
        "Popen",
        lambda argv, **kwargs: (starts.append(argv) or FakeProcess()),
    )

    for index in range(profile_jobs.PROFILE_JOB_MAX_RECORDS):
        created = profile_jobs.create_profile_job(
            account_id=f"account-{index}",
            label=f"Account {index}",
            browser="firefox",
            backend="direct",
            profile_dir=str(tmp_path / f"profile-{index}"),
            expected_backend_account_id=None,
            config_path=tmp_path / "config.toml",
            json_events=False,
        )
        profile_jobs._update_job(created["job_id"], status="completed")

    created = profile_jobs.create_profile_job(
        account_id="account-after-terminal-jobs",
        label="After terminal jobs",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile-after"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )

    assert created["status"] == "queued"
    assert len(list((state / "profile-jobs").glob("job-*.json"))) == 1
    assert len(starts) == profile_jobs.PROFILE_JOB_MAX_RECORDS + 1


@pytest.mark.parametrize(
    ("process_pid", "expected_signal"),
    [
        (4321, (4321, profile_jobs.signal.SIGTERM)),
        (True, None),
        (_BrokenInt(4321), None),
    ],
)
def test_profile_job_start_failure_cleans_up_untracked_worker(
    tmp_path, monkeypatch, process_pid, expected_signal
):
    state = tmp_path / "state"
    calls = []
    signals = []
    waits = []
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = process_pid

        def wait(self, timeout=None):
            waits.append(timeout)
            return -15

        def kill(self):
            calls.append(("kill",))

    monkeypatch.setattr(
        profile_jobs.subprocess,
        "Popen",
        lambda argv, **kwargs: (calls.append((argv, kwargs)) or FakeProcess()),
    )
    original_update = profile_jobs._update_job

    def fail_worker_pid_update(job_id, **changes):
        if "worker_pid" in changes:
            raise ValueError("manifest update failed")
        return original_update(job_id, **changes)

    monkeypatch.setattr(profile_jobs, "_update_job", fail_worker_pid_update)
    monkeypatch.setattr(
        profile_jobs.os,
        "killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )

    with pytest.raises(ValueError, match="manifest update failed"):
        profile_jobs.create_profile_job(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir=str(tmp_path / "profile"),
            expected_backend_account_id=None,
            config_path=tmp_path / "config.toml",
            json_events=False,
        )

    job_id = calls[0][0][-1]
    assert signals == ([expected_signal] if expected_signal is not None else [])
    assert calls[1:] == ([("kill",)] if expected_signal is None else [])
    assert waits == [1]
    assert profile_jobs.profile_job_status(job_id)["status"] == "failed"


def test_profile_job_start_failure_update_failure_removes_manifest(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    def fail_start(*args, **kwargs):
        raise OSError("worker start failed")

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", fail_start)

    def fail_failed_update(job_id, **changes):
        assert changes == {"status": "failed", "error": "profile_job_start_failed"}
        raise ValueError("manifest update failed")

    monkeypatch.setattr(profile_jobs, "_update_job", fail_failed_update)

    with pytest.raises(ValueError, match="profile job could not be started"):
        profile_jobs.create_profile_job(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir=str(tmp_path / "profile"),
            expected_backend_account_id=None,
            config_path=tmp_path / "config.toml",
            json_events=False,
        )

    assert list((state / "profile-jobs").glob("job-*.json")) == []


def test_profile_job_tracking_failure_does_not_leave_orphaned_manifest(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    calls = []
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

        def wait(self, timeout=None):
            return -15

        def kill(self):
            calls.append(("kill",))

    monkeypatch.setattr(
        profile_jobs.subprocess,
        "Popen",
        lambda argv, **kwargs: (calls.append((argv, kwargs)) or FakeProcess()),
    )

    def fail_tracking_updates(job_id, **changes):
        if "worker_pid" in changes or changes.get("status") == "failed":
            raise ValueError("manifest update failed")
        raise AssertionError("unexpected profile job update")

    monkeypatch.setattr(profile_jobs, "_update_job", fail_tracking_updates)

    with pytest.raises(ValueError, match="manifest update failed"):
        profile_jobs.create_profile_job(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir=str(tmp_path / "profile"),
            expected_backend_account_id=None,
            config_path=tmp_path / "config.toml",
            json_events=False,
        )

    job_id = calls[0][0][-1]
    assert not (state / "profile-jobs" / f"{job_id}.json").exists()


def test_profile_job_tracking_cancel_race_removes_pidless_manifest(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    calls = []
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

        def wait(self, timeout=None):
            return -15

        def kill(self):
            calls.append(("kill",))

    monkeypatch.setattr(
        profile_jobs.subprocess,
        "Popen",
        lambda argv, **kwargs: (calls.append((argv, kwargs)) or FakeProcess()),
    )
    original_update = profile_jobs._update_job

    def fail_tracking_updates(job_id, **changes):
        if "worker_pid" in changes:
            original_update(job_id, status="cancel_requested")
            raise ValueError("manifest update failed")
        if changes.get("status") == "failed":
            raise ValueError("manifest update failed")
        raise AssertionError("unexpected profile job update")

    monkeypatch.setattr(profile_jobs, "_update_job", fail_tracking_updates)

    with pytest.raises(ValueError, match="manifest update failed"):
        profile_jobs.create_profile_job(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir=str(tmp_path / "profile"),
            expected_backend_account_id=None,
            config_path=tmp_path / "config.toml",
            json_events=False,
        )

    job_id = calls[0][0][-1]
    assert not (state / "profile-jobs" / f"{job_id}.json").exists()


def test_profile_job_untracked_worker_reap_kills_after_wait_timeout():
    calls = []

    class FakeProcess:
        def __init__(self):
            self.wait_calls = 0

        def wait(self, timeout=None):
            self.wait_calls += 1
            calls.append(("wait", timeout))
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired(["worker"], timeout)

        def kill(self):
            calls.append(("kill",))

    profile_jobs._reap_untracked_worker(FakeProcess())

    assert calls == [("wait", 1), ("kill",), ("wait", 1)]


def test_profile_job_untracked_worker_reap_rejects_boolean_pid(monkeypatch):
    calls = []

    class FakeProcess:
        pid = True

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            if len([item for item in calls if item[0] == "wait"]) == 1:
                raise subprocess.TimeoutExpired(["worker"], timeout)

        def kill(self):
            calls.append(("kill",))

    monkeypatch.setattr(
        profile_jobs.os,
        "killpg",
        lambda pid, signum: calls.append(("killpg", pid, signum)),
    )

    profile_jobs._reap_untracked_worker(FakeProcess())

    assert calls == [("wait", 1), ("kill",), ("wait", 1)]


def test_profile_job_untracked_worker_reap_rejects_numeric_subclass_pid(monkeypatch):
    calls = []

    class FakeProcess:
        pid = _BrokenInt(4321)

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            if len([item for item in calls if item[0] == "wait"]) == 1:
                raise subprocess.TimeoutExpired(["worker"], timeout)

        def kill(self):
            calls.append(("kill",))

    monkeypatch.setattr(
        profile_jobs.os,
        "killpg",
        lambda pid, signum: calls.append(("killpg", pid, signum)),
    )

    profile_jobs._reap_untracked_worker(FakeProcess())

    assert calls == [("wait", 1), ("kill",), ("wait", 1)]


def test_profile_job_untracked_worker_reap_kills_process_group_after_timeout(
    monkeypatch,
):
    calls = []

    class FakeProcess:
        pid = 4321

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            if len([item for item in calls if item[0] == "wait"]) == 1:
                raise subprocess.TimeoutExpired(["worker"], timeout)

        def kill(self):
            calls.append(("kill",))

    monkeypatch.setattr(
        profile_jobs.os,
        "killpg",
        lambda pid, signum: calls.append(("killpg", pid, signum)),
    )

    profile_jobs._reap_untracked_worker(FakeProcess())

    assert calls == [
        ("wait", 1),
        ("killpg", 4321, profile_jobs.signal.SIGKILL),
        ("wait", 1),
    ]


def test_profile_job_cancel_signals_only_owned_worker(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)
    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )
    signals = []
    monkeypatch.setattr(profile_jobs, "_worker_matches", lambda pid, job_id: True)
    monkeypatch.setattr(
        profile_jobs.os,
        "killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )

    result = profile_jobs.cancel_profile_job(created["job_id"])

    assert result["status"] == "cancel_requested"
    assert signals == [(4321, profile_jobs.signal.SIGTERM)]


@pytest.mark.parametrize("worker_pid", [True, _BrokenInt(4321)])
def test_profile_job_cancel_rejects_invalid_worker_pid(
    tmp_path, monkeypatch, worker_pid
):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(
        profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess()
    )
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )
    original_update = profile_jobs._update_job

    def return_boolean_worker_pid(job_id, **changes):
        result = original_update(job_id, **changes)
        if changes.get("status") == "cancel_requested":
            return {**result, "worker_pid": worker_pid}
        return result

    monkeypatch.setattr(profile_jobs, "_update_job", return_boolean_worker_pid)
    monkeypatch.setattr(profile_jobs, "_worker_matches", lambda pid, job_id: True)
    signals = []
    monkeypatch.setattr(
        profile_jobs.os,
        "killpg",
        lambda pid, signum: signals.append((pid, signum)),
    )

    result = profile_jobs.cancel_profile_job(created["job_id"])

    assert result["status"] == "cancel_requested"
    assert signals == []


def test_profile_job_status_finishes_cancel_without_worker(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(
        profile_jobs.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )
    profile_jobs._update_job(
        created["job_id"],
        status="cancel_requested",
        worker_pid=None,
    )

    result = profile_jobs.profile_job_status(created["job_id"])

    assert result["status"] == "cancelled"
    assert result["ok"] is False


def test_profile_job_worker_preserves_options_and_cancel_group(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Personal",
        browser="chromium",
        backend="app-server",
        profile_dir=str(tmp_path / "profile"),
        reactivation_browser="chromium",
        expected_backend_account_id="backend-alpha",
        config_path=tmp_path / "config.toml",
        json_events=True,
    )
    observed = {}

    def fake_login(account, config_path, **kwargs):
        observed.update(
            {
                "account": account,
                "config_path": config_path,
                "kwargs": kwargs,
            }
        )
        return DeviceLoginResult(True, account.id)

    monkeypatch.setattr("codex_usage.profile_login.run_device_login", fake_login)
    monkeypatch.setattr(profile_jobs, "_verify_profile_job_completion", lambda job: True)

    assert profile_jobs.run_profile_job(created["job_id"]) == 0
    assert observed["account"].label == "Personal"
    assert observed["account"].browser == "chromium"
    assert observed["account"].backend == "app-server"
    assert observed["account"].reactivation_browser == "chromium"
    assert observed["kwargs"] == {
        "expected_backend_account_id": "backend-alpha",
        "isolate_process_group": False,
        "event_sink": observed["kwargs"]["event_sink"],
    }
    assert callable(observed["kwargs"]["event_sink"])
    assert profile_jobs.profile_job_status(created["job_id"])["status"] == "completed"


def test_profile_job_worker_does_not_reclaim_running_job_from_other_worker(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(
        profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess()
    )
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )
    foreign_pid = os.getpid() + 1
    profile_jobs._update_job(
        created["job_id"],
        expected_status="queued",
        status="running",
        worker_pid=foreign_pid,
    )
    login_calls = []
    monkeypatch.setattr(
        "codex_usage.profile_login.run_device_login",
        lambda *args, **kwargs: login_calls.append(True)
        or DeviceLoginResult(True, "alpha"),
    )
    monkeypatch.setattr(profile_jobs, "_verify_profile_job_completion", lambda job: True)

    assert profile_jobs.run_profile_job(created["job_id"]) == 1
    assert login_calls == []
    assert profile_jobs._read_job(created["job_id"])["status"] == "running"


def test_profile_job_status_exposes_events_only_while_job_is_live(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=True,
    )
    profile_jobs._update_job(created["job_id"], status="running", worker_pid=4321)
    monkeypatch.setattr(profile_jobs, "_worker_matches", lambda pid, job_id: True)
    profile_jobs._append_job_event(
        created["job_id"], DeviceLoginEvent("url", "https://auth.openai.com/device")
    )
    profile_jobs._append_job_event(created["job_id"], DeviceLoginEvent("code", "ABCD-1234"))

    live = profile_jobs.profile_job_status(created["job_id"])

    assert live["events"] == [
        {"kind": "url", "value": "https://auth.openai.com/device"},
        {"kind": "code", "value": "ABCD-1234"},
    ]
    profile_jobs._update_job(created["job_id"], status="completed")
    assert "events" not in profile_jobs.profile_job_status(created["job_id"])


def test_profile_job_list_returns_only_active_secret_free_jobs(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    active = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile-alpha"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=True,
    )
    finished = profile_jobs.create_profile_job(
        account_id="beta",
        label="Beta",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile-beta"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )
    profile_jobs._update_job(finished["job_id"], status="completed")
    monkeypatch.setattr(profile_jobs, "_worker_matches", lambda pid, job_id: True)

    jobs = profile_jobs.list_profile_jobs()

    assert jobs == [active]
    assert all("profile_dir" not in job for job in jobs)


def test_profile_job_list_bounds_manifest_path_collection(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)
    root = state / "profile-jobs"
    root.mkdir(parents=True)
    for index in range(profile_jobs.PROFILE_JOB_MAX_RECORDS + 1):
        (root / f"job-{index:032x}.json").touch(mode=0o600)

    original_glob = Path.glob

    def reject_materializing_glob(path, pattern):
        if path == root:
            raise AssertionError("profile job path collection must be bounded")
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", reject_materializing_glob)

    with pytest.raises(ValueError, match="too many profile jobs"):
        profile_jobs.list_profile_jobs()


def test_profile_job_list_bounds_total_directory_entries(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)
    root = state / "profile-jobs"
    root.mkdir(parents=True)
    entry_limit = profile_jobs.PROFILE_JOB_MAX_RECORDS * 8
    for index in range(entry_limit + 1):
        (root / f"unrelated-{index}").touch(mode=0o600)

    with pytest.raises(ValueError, match="too many profile job directory entries"):
        profile_jobs.list_profile_jobs()


def test_profile_job_creation_bounds_total_directory_entries_before_worker(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    starts = []
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)
    root = state / "profile-jobs"
    root.mkdir(parents=True)
    entry_limit = profile_jobs.PROFILE_JOB_MAX_RECORDS * 8
    for index in range(entry_limit + 1):
        (root / f"unrelated-{index}").touch(mode=0o600)
    monkeypatch.setattr(
        profile_jobs.subprocess,
        "Popen",
        lambda *args, **kwargs: starts.append(args) or pytest.fail("worker must not start"),
    )

    with pytest.raises(ValueError, match="too many profile job directory entries"):
        profile_jobs.create_profile_job(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir=str(tmp_path / "profile"),
            expected_backend_account_id=None,
            config_path=tmp_path / "config.toml",
            json_events=False,
        )

    assert starts == []


def test_profile_job_status_reconciles_lost_worker(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )
    profile_jobs._update_job(created["job_id"], status="running", worker_pid=4321)
    monkeypatch.setattr(profile_jobs, "_worker_matches", lambda pid, job_id: False)

    result = profile_jobs.profile_job_status(created["job_id"])

    assert result == {
        "account": "alpha",
        "error": "profile_job_worker_lost",
        "job_id": created["job_id"],
        "ok": False,
        "status": "failed",
    }


def test_profile_job_status_reconciles_running_without_worker(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )
    profile_jobs._update_job(
        created["job_id"],
        expected_status="queued",
        status="running",
        worker_pid=None,
    )

    result = profile_jobs.profile_job_status(created["job_id"])

    assert result == {
        "account": "alpha",
        "error": "profile_job_worker_lost",
        "job_id": created["job_id"],
        "ok": False,
        "status": "failed",
    }


def test_profile_job_status_reconciles_lost_queued_worker(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )
    profile_jobs._update_job(created["job_id"], worker_pid=4321)
    monkeypatch.setattr(profile_jobs, "_worker_matches", lambda pid, job_id: False)

    result = profile_jobs.profile_job_status(created["job_id"])

    assert result["status"] == "failed"
    assert result["error"] == "profile_job_worker_lost"


def test_profile_job_status_keeps_queued_without_tracked_worker(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )
    profile_jobs._update_job(created["job_id"], worker_pid=None)

    result = profile_jobs.profile_job_status(created["job_id"])

    assert result["status"] == "queued"


def test_profile_job_cancel_does_not_overwrite_terminal_worker_race(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )
    original_update = profile_jobs._update_job

    def complete_before_cancel(job_id, **changes):
        if changes.get("status") == "cancel_requested":
            original_update(job_id, expected_status="queued", status="completed", error=None)
        return original_update(job_id, **changes)

    monkeypatch.setattr(profile_jobs, "_update_job", complete_before_cancel)

    result = profile_jobs.cancel_profile_job(created["job_id"])

    assert result["status"] == "completed"
    assert profile_jobs.profile_job_status(created["job_id"])["status"] == "completed"


def test_profile_job_status_does_not_overwrite_terminal_worker_race(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )
    original_update = profile_jobs._update_job
    original_update(created["job_id"], expected_status="queued", status="running", worker_pid=4321)
    monkeypatch.setattr(profile_jobs, "_worker_matches", lambda pid, job_id: False)

    def complete_before_lost_worker(job_id, **changes):
        if changes.get("status") == "failed":
            original_update(job_id, expected_status="running", status="completed", error=None)
        return original_update(job_id, **changes)

    monkeypatch.setattr(profile_jobs, "_update_job", complete_before_lost_worker)

    result = profile_jobs.profile_job_status(created["job_id"])

    assert result["status"] == "completed"


def test_profile_job_worker_preserves_cancel_requested_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )

    def fake_login(*args, **kwargs):
        profile_jobs._update_job(created["job_id"], status="cancel_requested")
        return DeviceLoginResult(False, "alpha", error="device_login_failed")

    monkeypatch.setattr("codex_usage.profile_login.run_device_login", fake_login)

    assert profile_jobs.run_profile_job(created["job_id"]) == 0
    assert profile_jobs.profile_job_status(created["job_id"])["status"] == "cancelled"


def test_profile_job_worker_does_not_start_after_queued_cancel_race(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )
    login_calls = []
    original_update = profile_jobs._update_job

    def cancel_before_start(job_id, **changes):
        if changes.get("status") == "running":
            original_update(job_id, expected_status="queued", status="cancel_requested")
        return original_update(job_id, **changes)

    monkeypatch.setattr(profile_jobs, "_update_job", cancel_before_start)
    monkeypatch.setattr(
        "codex_usage.profile_login.run_device_login",
        lambda *args, **kwargs: login_calls.append(True),
    )

    assert profile_jobs.run_profile_job(created["job_id"]) == 0
    assert login_calls == []
    assert profile_jobs.profile_job_status(created["job_id"])["status"] == "cancelled"


def test_profile_job_worker_does_not_overwrite_cancel_race(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )
    original_cancel_check = profile_jobs._job_cancel_requested
    cancelled = False

    def cancel_after_check(job_id):
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            profile_jobs._update_job(job_id, status="cancel_requested")
            return False
        return original_cancel_check(job_id)

    monkeypatch.setattr(profile_jobs, "_job_cancel_requested", cancel_after_check)
    monkeypatch.setattr(
        "codex_usage.profile_login.run_device_login",
        lambda *args, **kwargs: DeviceLoginResult(True, "alpha"),
    )

    assert profile_jobs.run_profile_job(created["job_id"]) == 0
    assert profile_jobs.profile_job_status(created["job_id"])["status"] == "cancelled"


def test_profile_job_worker_does_not_start_login_after_running_cancel_race(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )
    original_cancel_check = profile_jobs._job_cancel_requested
    login_calls = []
    cancelled = False

    def cancel_before_login(job_id):
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            profile_jobs._update_job(job_id, status="cancel_requested")
            return True
        return original_cancel_check(job_id)

    monkeypatch.setattr(profile_jobs, "_job_cancel_requested", cancel_before_login)
    monkeypatch.setattr(
        "codex_usage.profile_login.run_device_login",
        lambda *args, **kwargs: login_calls.append(True),
    )

    assert profile_jobs.run_profile_job(created["job_id"]) == 0
    assert login_calls == []
    assert profile_jobs.profile_job_status(created["job_id"])["status"] == "cancelled"


def test_profile_job_worker_rejects_success_without_published_account(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=False,
    )
    monkeypatch.setattr(
        "codex_usage.profile_login.run_device_login",
        lambda *args, **kwargs: DeviceLoginResult(True, "alpha"),
    )

    assert profile_jobs.run_profile_job(created["job_id"]) == 1
    result = profile_jobs.profile_job_status(created["job_id"])
    assert result["status"] == "failed"
    assert result["error"] == "profile_job_completion_postcondition_failed"


def test_profile_job_completion_rejects_reactivation_browser_mismatch(
    tmp_path, monkeypatch
):
    auth_path = tmp_path / "auth.json"
    write_private_text(auth_path, "{}\n", label="test auth")
    account = Account(
        id="alpha",
        label="Alpha",
        profile_dir=str(tmp_path / "profile"),
        browser="firefox",
        auth_json_path=str(auth_path),
        backend="direct",
        reactivation_browser="firefox",
    )
    monkeypatch.setattr(
        profile_jobs,
        "load_config",
        lambda _path: AppConfig(accounts=(account,)),
    )
    job = {
        "account_id": "alpha",
        "label": "Alpha",
        "profile_dir": str(tmp_path / "profile"),
        "browser": "firefox",
        "backend": "direct",
        "reactivation_browser": "chromium",
        "config_path": str(tmp_path / "config.toml"),
    }

    assert profile_jobs._verify_profile_job_completion(job) is False


def test_profile_job_completion_rejects_unknown_profile_home(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    write_private_text(auth_path, "{}\n", label="test auth")
    account = Account(
        id="alpha",
        label="Alpha",
        profile_dir=str(tmp_path / "profile"),
        browser="firefox",
        auth_json_path=str(auth_path),
        backend="direct",
        reactivation_browser="firefox",
    )
    monkeypatch.setattr(
        profile_jobs,
        "load_config",
        lambda _path: AppConfig(accounts=(account,)),
    )
    job = {
        "account_id": "alpha",
        "label": "Alpha",
        "profile_dir": "~definitely-no-such-user-zzzz/profile",
        "browser": "firefox",
        "backend": "direct",
        "reactivation_browser": "firefox",
        "config_path": str(tmp_path / "config.toml"),
    }

    assert profile_jobs._verify_profile_job_completion(job) is False


def test_profile_job_completion_rejects_non_owned_auth(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    write_private_text(auth_path, "{}\n", label="test auth")
    account = Account(
        id="alpha",
        label="Alpha",
        profile_dir=str(tmp_path / "profile"),
        browser="firefox",
        auth_json_path=str(auth_path),
        backend="direct",
        reactivation_browser="firefox",
    )
    monkeypatch.setattr(
        profile_jobs,
        "load_config",
        lambda _path: AppConfig(accounts=(account,)),
    )
    current_uid = os.getuid()
    monkeypatch.setattr(profile_jobs.os, "getuid", lambda: current_uid + 1)
    job = {
        "account_id": "alpha",
        "label": "Alpha",
        "profile_dir": str(tmp_path / "profile"),
        "browser": "firefox",
        "backend": "direct",
        "reactivation_browser": "firefox",
        "config_path": str(tmp_path / "config.toml"),
    }

    assert profile_jobs._verify_profile_job_completion(job) is False


def test_profile_job_completion_rejects_wrong_auth_identity(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    write_private_text(
        auth_path,
        '{"tokens":{"account_id":"backend-other","access_token":"test-token"}}\n',
        label="test auth",
    )
    account = Account(
        id="alpha",
        label="Alpha",
        profile_dir=str(tmp_path / "profile"),
        browser="firefox",
        auth_json_path=str(auth_path),
        backend="direct",
        reactivation_browser="firefox",
    )
    monkeypatch.setattr(
        profile_jobs,
        "load_config",
        lambda _path: AppConfig(accounts=(account,)),
    )
    job = {
        "account_id": "alpha",
        "label": "Alpha",
        "profile_dir": str(tmp_path / "profile"),
        "browser": "firefox",
        "backend": "direct",
        "reactivation_browser": "firefox",
        "expected_backend_account_id": "backend-alpha",
        "config_path": str(tmp_path / "config.toml"),
    }

    assert profile_jobs._verify_profile_job_completion(job) is False


def test_completed_profile_job_is_visible_to_service_and_next_scheduler_cycle(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: tmp_path / "state")
    config_path = tmp_path / "config.toml"
    profile_dir = tmp_path / "profile"
    service_syncs = []
    monkeypatch.setattr(cli, "service_status", lambda: {"installed": True})
    monkeypatch.setattr(cli, "managed_service_config_path", lambda: config_path.absolute())
    monkeypatch.setattr(
        cli,
        "service_install",
        lambda config, path: service_syncs.append((config, path)),
    )
    assert cli.main(
        [
            "--config",
            str(config_path),
            "account",
            "add",
            "alpha",
            "--label",
            "Alpha",
            "--profile-dir",
            str(profile_dir),
        ]
    ) == 0
    capsys.readouterr()
    assert len(service_syncs) == 1
    assert service_syncs[0][0].accounts[0].profile_dir == str(profile_dir)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    created = profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(profile_dir),
        expected_backend_account_id=None,
        config_path=config_path,
        json_events=False,
    )

    def fake_login(account, selected_config, **kwargs):
        layout = ensure_profile_layout(account)
        write_private_text(layout.auth_json, "{}\n", label="test auth")
        add_or_update_account(
            account.id,
            label=account.label,
            profile_dir=account.profile_dir,
            browser=account.browser,
            backend=account.backend,
            reactivation_browser=account.reactivation_browser,
            auth_json_path=str(layout.auth_json),
            path=selected_config,
        )
        return DeviceLoginResult(True, account.id)

    monkeypatch.setattr("codex_usage.profile_login.run_device_login", fake_login)

    assert profile_jobs.run_profile_job(created["job_id"]) == 0
    assert profile_jobs.profile_job_status(created["job_id"])["status"] == "completed"

    current = load_config(config_path)
    assert current.accounts[0].auth_json_path == str(profile_dir / "codex-home" / "auth.json")
    assert service_syncs[0][1] == config_path

    seen = []

    def fake_fetch_one(_config, account, **kwargs):
        seen.append((account.id, account.auth_json_path))
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=datetime.now().astimezone(),
            status=AccountStatus.OK,
            backend_configured=account.backend,
            backend_used="direct",
        )

    monkeypatch.setattr("codex_usage.scheduler._fetch_one", fake_fetch_one)
    fetch_all(current, current.accounts, direct=True)

    assert seen == [("alpha", str(profile_dir / "codex-home" / "auth.json"))]


def test_profile_job_manifest_validation_does_not_require_profile_path(tmp_path):
    manifest = {
        "schema_version": 1,
        "job_id": "job-" + "a" * 32,
        "account_id": "alpha",
        "label": "Alpha",
        "browser": "firefox",
        "backend": "direct",
        "profile_dir": str(tmp_path / "not-created-yet"),
        "reactivation_browser": "auto",
        "expected_backend_account_id": None,
        "config_path": str(tmp_path / "config.toml"),
        "json_events": False,
        "status": "queued",
        "created_at": "2026-08-16T00:00:00Z",
        "updated_at": "2026-08-16T00:00:00Z",
        "worker_pid": None,
        "error": None,
    }

    validated = profile_jobs._validate_manifest(manifest)

    assert validated["profile_dir"] == manifest["profile_dir"]
    manifest["worker_pid"] = _BrokenInt(4321)
    with pytest.raises(ValueError, match="worker pid is invalid"):
        profile_jobs._validate_manifest(manifest)


@pytest.mark.parametrize("schema_version", [True, 1.0, "1"])
def test_profile_job_manifest_requires_strict_schema_version(tmp_path, schema_version):
    manifest = {
        "schema_version": schema_version,
        "job_id": "job-" + "a" * 32,
        "account_id": "alpha",
        "label": "Alpha",
        "browser": "firefox",
        "backend": "direct",
        "profile_dir": str(tmp_path / "profile"),
        "reactivation_browser": "auto",
        "tag": "",
        "series": "",
        "series_active": False,
        "expected_backend_account_id": None,
        "config_path": str(tmp_path / "config.toml"),
        "json_events": False,
        "status": "queued",
        "created_at": "2026-08-16T00:00:00Z",
        "updated_at": "2026-08-16T00:00:00Z",
        "worker_pid": None,
        "error": None,
    }

    with pytest.raises(ValueError, match="schema version is invalid"):
        profile_jobs._validate_manifest(manifest)


def test_profile_job_manifest_rejects_unhashable_status(tmp_path):
    manifest = {
        "schema_version": 1,
        "job_id": "job-" + "a" * 32,
        "account_id": "alpha",
        "label": "Alpha",
        "browser": "firefox",
        "backend": "direct",
        "profile_dir": str(tmp_path / "profile"),
        "reactivation_browser": "auto",
        "tag": "",
        "series": "",
        "series_active": False,
        "expected_backend_account_id": None,
        "config_path": str(tmp_path / "config.toml"),
        "json_events": False,
        "status": [],
        "created_at": "2026-08-16T00:00:00Z",
        "updated_at": "2026-08-16T00:00:00Z",
        "worker_pid": None,
        "error": None,
    }

    with pytest.raises(ValueError, match="status is invalid"):
        profile_jobs._validate_manifest(manifest)


@pytest.mark.parametrize(
    ("field", "timestamp"),
    [
        ("created_at", "not-a-timestampZ"),
        ("updated_at", "not-a-timestampZ"),
        ("created_at", "2026-08-16Z"),
        ("updated_at", "2026-08-16Z"),
    ],
)
def test_profile_job_manifest_rejects_invalid_timestamps(tmp_path, field, timestamp):
    manifest = {
        "schema_version": 1,
        "job_id": "job-" + "a" * 32,
        "account_id": "alpha",
        "label": "Alpha",
        "browser": "firefox",
        "backend": "direct",
        "profile_dir": str(tmp_path / "profile"),
        "reactivation_browser": "auto",
        "tag": "",
        "series": "",
        "series_active": False,
        "expected_backend_account_id": None,
        "config_path": str(tmp_path / "config.toml"),
        "json_events": False,
        "status": "queued",
        "created_at": "2026-08-16T00:00:00Z",
        "updated_at": "2026-08-16T00:00:00Z",
        "worker_pid": None,
        "error": None,
    }
    manifest[field] = timestamp

    with pytest.raises(ValueError, match="timestamp is invalid"):
        profile_jobs._validate_manifest(manifest)


@pytest.mark.parametrize("profile_dir", [None, [], {}])
def test_profile_job_create_rejects_invalid_profile_dir_type(profile_dir):
    with pytest.raises(ValueError, match="profile dir is invalid"):
        profile_jobs._validate_create_arguments(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir=profile_dir,
            expected_backend_account_id=None,
            json_events=False,
            reactivation_browser="auto",
            check_profile_path=False,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("browser", [], "browser is invalid"),
        ("backend", {}, "backend is invalid"),
        ("reactivation_browser", [], "reactivation browser is invalid"),
    ],
)
def test_profile_job_create_rejects_unhashable_selector_types(
    field, value, message
):
    arguments = {
        "account_id": "alpha",
        "label": "Alpha",
        "browser": "firefox",
        "backend": "direct",
        "profile_dir": "/tmp/profile",
        "expected_backend_account_id": None,
        "json_events": False,
        "reactivation_browser": "auto",
        "check_profile_path": False,
    }
    arguments[field] = value

    with pytest.raises(ValueError, match=message):
        profile_jobs._validate_create_arguments(**arguments)


@pytest.mark.parametrize("expected_backend_account_id", ["backend alpha", " backend"])
def test_profile_job_create_rejects_whitespace_backend_account_id(
    expected_backend_account_id,
):
    with pytest.raises(ValueError, match="expected backend account id is invalid"):
        profile_jobs._validate_create_arguments(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir="/tmp/profile",
            expected_backend_account_id=expected_backend_account_id,
            json_events=False,
            reactivation_browser="auto",
            check_profile_path=False,
        )


def test_profile_job_create_rejects_unknown_profile_home():
    with pytest.raises(ValueError, match="profile dir is invalid"):
        profile_jobs._validate_create_arguments(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir="~definitely-no-such-user-zzzz",
            expected_backend_account_id=None,
            json_events=False,
            reactivation_browser="auto",
            check_profile_path=False,
        )


def test_profile_job_create_rejects_unknown_config_home(tmp_path):
    with pytest.raises(ValueError, match="config path is invalid"):
        profile_jobs.create_profile_job(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir=str(tmp_path / "profile"),
            expected_backend_account_id=None,
            config_path=Path("~definitely-no-such-user-zzzz/config.toml"),
            json_events=False,
        )


@pytest.mark.parametrize("config_path", ["", [], {}, {"path": "x"}, 0, False])
def test_profile_job_create_rejects_invalid_config_path_type(
    config_path, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        profile_jobs,
        "_write_new_job",
        lambda manifest: pytest.fail("invalid config path reached job creation"),
    )

    with pytest.raises(ValueError, match="config path is invalid"):
        profile_jobs.create_profile_job(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir=str(tmp_path / "profile"),
            expected_backend_account_id=None,
            config_path=config_path,
            json_events=False,
        )


@pytest.mark.parametrize("kind", [None, [], {}])
def test_profile_job_event_rejects_non_string_kind(kind):
    with pytest.raises(ValueError, match="event kind is invalid"):
        profile_jobs._normalize_job_event({"kind": kind, "value": "https://example.com"})


@pytest.mark.parametrize("argv", [None, (), "job", 1, object(), [None], ["a", "b"]])
def test_profile_job_worker_rejects_invalid_argv(argv):
    assert profile_jobs.worker_main(argv) == 2  # type: ignore[arg-type]


def _unit_job(tmp_path, monkeypatch, *, json_events=False):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(
        profile_jobs.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    return profile_jobs.create_profile_job(
        account_id="alpha",
        label="Alpha",
        browser="firefox",
        backend="direct",
        profile_dir=str(tmp_path / "profile"),
        expected_backend_account_id=None,
        config_path=tmp_path / "config.toml",
        json_events=json_events,
    )


def test_profile_job_create_rejects_relative_config_and_cleanup_failures(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="config path must be absolute"):
        profile_jobs.create_profile_job(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir=str(tmp_path / "profile"),
            expected_backend_account_id=None,
            config_path=Path("relative/config.toml"),
            json_events=False,
        )

    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)
    monkeypatch.setattr(
        profile_jobs.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("start")),
    )
    monkeypatch.setattr(
        profile_jobs,
        "_update_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("update")),
    )
    monkeypatch.setattr(
        profile_jobs,
        "_remove_untracked_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("remove")),
    )
    with pytest.raises(ValueError, match="profile job could not be started"):
        profile_jobs.create_profile_job(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir=str(tmp_path / "profile"),
            expected_backend_account_id=None,
            config_path=tmp_path / "config.toml",
            json_events=False,
        )


def test_profile_job_tracking_cleanup_handles_kill_and_remove_failures(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: state)

    class FakeProcess:
        pid = 4321

        def kill(self):
            raise OSError("kill")

        def wait(self, timeout=None):
            return -15

    monkeypatch.setattr(profile_jobs.subprocess, "Popen", lambda *_a, **_k: FakeProcess())
    monkeypatch.setattr(
        profile_jobs.os,
        "killpg",
        lambda *_a: (_ for _ in ()).throw(OSError("killpg")),
    )
    original_update = profile_jobs._update_job

    def fail_tracking(job_id, **changes):
        if "worker_pid" in changes:
            raise ValueError("tracking")
        if changes.get("status") == "failed":
            raise ValueError("failed update")
        return original_update(job_id, **changes)

    monkeypatch.setattr(profile_jobs, "_update_job", fail_tracking)
    monkeypatch.setattr(
        profile_jobs,
        "_remove_untracked_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("remove")),
    )
    with pytest.raises(ValueError, match="tracking"):
        profile_jobs.create_profile_job(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir=str(tmp_path / "profile"),
            expected_backend_account_id=None,
            config_path=tmp_path / "config.toml",
            json_events=False,
        )


def test_profile_job_reap_swallows_group_kill_process_kill_and_wait_errors(monkeypatch):
    calls = []

    class FakeProcess:
        pid = 4321

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            raise subprocess.TimeoutExpired(["worker"], timeout)

        def kill(self):
            calls.append(("kill",))
            raise OSError("kill")

    monkeypatch.setattr(
        profile_jobs.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(OSError("killpg")),
    )
    profile_jobs._reap_untracked_worker(FakeProcess())
    assert calls == [("wait", 1), ("kill",), ("wait", 1)]


def test_profile_job_remove_untracked_handles_symlink_and_missing(tmp_path, monkeypatch):
    job = _unit_job(tmp_path, monkeypatch)
    path = profile_jobs._job_path(job["job_id"])
    path.unlink()
    path.symlink_to(tmp_path / "target")
    with pytest.raises(ValueError, match="manifest must not be a symlink"):
        profile_jobs._remove_untracked_job(job["job_id"])
    path.unlink()
    profile_jobs._remove_untracked_job(job["job_id"])


def test_profile_job_list_cancel_and_status_cover_filters_and_terminal_paths(tmp_path, monkeypatch):
    job = _unit_job(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="account id is invalid"):
        profile_jobs.list_profile_jobs("bad/id")
    assert profile_jobs.list_profile_jobs("other") == []
    profile_jobs._update_job(job["job_id"], status="completed")
    assert profile_jobs.cancel_profile_job(job["job_id"])["status"] == "completed"

    live = _unit_job(tmp_path / "live", monkeypatch)
    profile_jobs._update_job(live["job_id"], status="running", worker_pid=4321)
    monkeypatch.setattr(profile_jobs, "_worker_matches", lambda *_args: True)
    monkeypatch.setattr(
        profile_jobs.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(OSError("group disappeared")),
    )
    assert profile_jobs.cancel_profile_job(live["job_id"])["status"] == "cancel_requested"


@pytest.mark.parametrize("terminal_status", ["cancelled", "failed", "completed"])
def test_profile_job_run_cancel_requested_handles_update_result(
    tmp_path, monkeypatch, terminal_status
):
    job = _unit_job(tmp_path, monkeypatch)
    profile_jobs._update_job(job["job_id"], status="cancel_requested")
    monkeypatch.setattr(
        profile_jobs,
        "_update_job",
        lambda *_args, **_kwargs: {"status": terminal_status},
    )
    expected = 0 if terminal_status == "cancelled" else 1
    assert profile_jobs.run_profile_job(job["job_id"]) == expected


def test_profile_job_worker_handles_signal_device_error_generic_error_and_failed_result(
    tmp_path, monkeypatch
):
    job = _unit_job(tmp_path, monkeypatch)

    def install_and_terminate(_signal, handler):
        if callable(handler):
            handler(profile_jobs.signal.SIGTERM, None)

    monkeypatch.setattr(
        profile_jobs.signal,
        "signal",
        install_and_terminate,
    )
    assert profile_jobs.run_profile_job(job["job_id"]) == 0
    assert profile_jobs._read_job(job["job_id"])["status"] == "cancelled"

    for error in (
        profile_jobs.profile_login.DeviceLoginError("device failed"),
        RuntimeError("generic failed"),
    ):
        current = _unit_job(tmp_path / str(len(error.args)), monkeypatch)
        monkeypatch.setattr(profile_jobs.signal, "signal", lambda *_args: None)
        monkeypatch.setattr(profile_jobs.signal, "getsignal", lambda *_args: object())
        monkeypatch.setattr(
            profile_jobs.profile_login,
            "run_device_login",
            lambda *_args, error=error, **_kwargs: (_ for _ in ()).throw(error),
        )
        assert profile_jobs.run_profile_job(current["job_id"]) == 1
        assert profile_jobs._read_job(current["job_id"])["status"] == "failed"

    current = _unit_job(tmp_path / "result", monkeypatch)
    monkeypatch.setattr(profile_jobs.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(profile_jobs.signal, "getsignal", lambda *_args: object())
    monkeypatch.setattr(
        profile_jobs.profile_login,
        "run_device_login",
        lambda *_args, **_kwargs: DeviceLoginResult(False, "alpha"),
    )
    assert profile_jobs.run_profile_job(current["job_id"]) == 1
    assert profile_jobs._read_job(current["job_id"])["status"] == "failed"


def test_profile_job_worker_cancels_after_success_before_completion_check(tmp_path, monkeypatch):
    job = _unit_job(tmp_path, monkeypatch)
    monkeypatch.setattr(profile_jobs.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(profile_jobs.signal, "getsignal", lambda *_args: object())
    checks = iter((False, False, True))
    monkeypatch.setattr(profile_jobs, "_job_cancel_requested", lambda *_args: next(checks))
    monkeypatch.setattr(
        profile_jobs.profile_login,
        "run_device_login",
        lambda *_args, **_kwargs: DeviceLoginResult(True, "alpha"),
    )
    assert profile_jobs.run_profile_job(job["job_id"]) == 0
    assert profile_jobs._read_job(job["job_id"])["status"] == "cancelled"


@pytest.mark.parametrize(
    "error",
    [profile_jobs.profile_login.DeviceLoginError("device"), RuntimeError("generic")],
)
def test_profile_job_worker_cancels_while_login_raises(tmp_path, monkeypatch, error):
    job = _unit_job(tmp_path, monkeypatch)
    monkeypatch.setattr(profile_jobs.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(profile_jobs.signal, "getsignal", lambda *_args: object())
    checks = iter((False, True))
    monkeypatch.setattr(profile_jobs, "_job_cancel_requested", lambda *_args: next(checks))
    monkeypatch.setattr(
        profile_jobs.profile_login,
        "run_device_login",
        lambda *_args, error=error, **_kwargs: (_ for _ in ()).throw(error),
    )
    assert profile_jobs.run_profile_job(job["job_id"]) == 0
    assert profile_jobs._read_job(job["job_id"])["status"] == "cancelled"


def test_profile_job_completion_rejects_auth_file_shape_and_identity_errors(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    write_private_text(auth_path, "{}\n", label="test auth")
    account = Account(
        id="alpha",
        label="Alpha",
        profile_dir=str(tmp_path / "profile"),
        browser="firefox",
        auth_json_path=str(auth_path),
        backend="direct",
        reactivation_browser="auto",
    )
    monkeypatch.setattr(profile_jobs, "load_config", lambda _path: AppConfig(accounts=(account,)))
    job = {
        "account_id": "alpha",
        "label": "Alpha",
        "profile_dir": str(tmp_path / "profile"),
        "browser": "firefox",
        "backend": "direct",
        "reactivation_browser": "auto",
        "config_path": str(tmp_path / "config.toml"),
    }
    auth_path.unlink()
    auth_path.symlink_to(tmp_path / "target")
    assert profile_jobs._verify_profile_job_completion(job) is False
    auth_path.unlink()
    auth_path.mkdir()
    assert profile_jobs._verify_profile_job_completion(job) is False
    auth_path.rmdir()
    write_private_text(auth_path, "{}\n", label="test auth")
    monkeypatch.setattr(
        profile_jobs,
        "auth_identity_from_file",
        lambda _path: (_ for _ in ()).throw(profile_jobs.DirectAuthError("bad auth")),
    )
    job["expected_backend_account_id"] = "backend-alpha"
    assert profile_jobs._verify_profile_job_completion(job) is False


def test_profile_job_events_cover_duplicates_caps_permissions_and_shapes(tmp_path, monkeypatch):
    job = _unit_job(tmp_path, monkeypatch, json_events=True)
    event = DeviceLoginEvent("code", "ABCD-1234")
    profile_jobs._append_job_event(job["job_id"], event)
    profile_jobs._append_job_event(job["job_id"], event)
    for index in range(profile_jobs.PROFILE_JOB_MAX_EVENTS):
        profile_jobs._append_job_event(job["job_id"], DeviceLoginEvent("code", f"CODE-{index}"))
    events_path = profile_jobs._event_path(job["job_id"])
    assert len(profile_jobs._read_job_events(job["job_id"])) == profile_jobs.PROFILE_JOB_MAX_EVENTS

    events_path.chmod(0o640)
    with pytest.raises(ValueError, match="permissions"):
        profile_jobs._read_job_events_unlocked(events_path)
    events_path.chmod(0o600)
    write_private_text(events_path, "{}\n", label="bad events")
    with pytest.raises(ValueError, match="events are invalid"):
        profile_jobs._read_job_events_unlocked(events_path)

    too_many = [
        {"kind": event.kind, "value": event.value}
        for _ in range(profile_jobs.PROFILE_JOB_MAX_EVENTS + 1)
    ]
    write_private_text(events_path, json.dumps(too_many), label="too many events")
    with pytest.raises(ValueError, match="events are invalid"):
        profile_jobs._read_job_events_unlocked(events_path)

    for invalid in (
        {"kind": "code", "value": ""},
        {"kind": "code", "value": "x\n"},
        {"kind": "code", "value": "x" * (profile_jobs.PROFILE_JOB_EVENT_VALUE_MAX_CHARS + 1)},
        {"kind": "url", "value": "http://example.com"},
    ):
        with pytest.raises(ValueError):
            profile_jobs._normalize_job_event(invalid)

    monkeypatch.setattr(profile_jobs, "PROFILE_JOB_EVENT_MAX_BYTES", 10)
    with pytest.raises(ValueError, match="events are too large"):
        profile_jobs._serialize_events([{"kind": "code", "value": "x"}])


def test_profile_job_event_delete_and_read_races_are_fail_closed(tmp_path, monkeypatch):
    job = _unit_job(tmp_path, monkeypatch, json_events=True)
    event_path = profile_jobs._event_path(job["job_id"])
    event_path.symlink_to(tmp_path / "target")
    with pytest.raises(ValueError, match="events must not be a symlink"):
        profile_jobs._delete_job_events(job["job_id"])
    event_path.unlink()
    profile_jobs._delete_job_events(job["job_id"])

    write_private_text(event_path, "[]\n", label="events")
    original_lock = profile_jobs.private_path_lock

    @contextmanager
    def remove_inside_lock(*args, **kwargs):
        event_path.unlink()
        with original_lock(*args, **kwargs):
            yield

    monkeypatch.setattr(profile_jobs, "private_path_lock", remove_inside_lock)
    assert profile_jobs._read_job_events(job["job_id"]) == []

    write_private_text(event_path, "[]\n", label="events")

    @contextmanager
    def symlink_inside_lock(*_args, **_kwargs):
        event_path.unlink()
        event_path.symlink_to(tmp_path / "target")
        yield

    monkeypatch.setattr(profile_jobs, "private_path_lock", symlink_inside_lock)
    with pytest.raises(ValueError, match="events must not be a symlink"):
        profile_jobs._delete_job_events(job["job_id"])
    event_path.unlink()
    with pytest.raises(ValueError, match="profile job id is invalid"):
        profile_jobs._event_path("invalid")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("account_id", "__all_accounts__", "account id is invalid"),
        ("account_id", "bad/id", "account id is invalid"),
        ("label", "   ", "label is invalid"),
        ("label", "line\nfeed", "label is invalid"),
        ("series_active", 1, "series_active is invalid"),
        ("tag", "123456789", "tag is invalid"),
        ("series", "", "active series requires a series name"),
    ],
)
def test_profile_job_create_argument_guards(field, value, message):
    arguments = {
        "account_id": "alpha",
        "label": "Alpha",
        "browser": "firefox",
        "backend": "direct",
        "profile_dir": "/tmp/profile",
        "expected_backend_account_id": None,
        "json_events": False,
        "reactivation_browser": "auto",
        "check_profile_path": False,
    }
    if field == "series":
        arguments["series_active"] = True
    arguments[field] = value
    with pytest.raises(ValueError, match=message):
        profile_jobs._validate_create_arguments(**arguments)


def test_profile_job_create_rejects_relative_and_symlink_profile_dirs(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="profile dir must be absolute"):
        profile_jobs._validate_create_arguments(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir="relative/profile",
            expected_backend_account_id=None,
            json_events=False,
            reactivation_browser="auto",
            check_profile_path=False,
        )
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "profile-link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(profile_jobs, "assert_no_symlink_ancestors", lambda *_a, **_k: None)
    with pytest.raises(ValueError, match="profile dir must not be a symlink"):
        profile_jobs._validate_create_arguments(
            account_id="alpha",
            label="Alpha",
            browser="firefox",
            backend="direct",
            profile_dir=str(link),
            expected_backend_account_id=None,
            json_events=False,
            reactivation_browser="auto",
        )


def test_profile_job_root_lock_and_path_guards(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_jobs, "default_state_dir", lambda: tmp_path / "state")
    root = tmp_path / "state" / "profile-jobs"
    root.parent.mkdir(parents=True)
    root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="profile job directory is invalid"):
        profile_jobs._job_root()
    with pytest.raises(ValueError, match="profile job id is invalid"):
        profile_jobs._job_path("bad")
    root.unlink()
    with profile_jobs.profile_job_creation_lock():
        pass


def test_profile_job_prune_and_write_guards_cover_directory_and_duplicate_paths(
    tmp_path, monkeypatch
):
    job = _unit_job(tmp_path, monkeypatch)
    root = profile_jobs._job_root()
    path = profile_jobs._job_path(job["job_id"])
    profile_jobs._update_job(job["job_id"], status="completed")
    path.unlink()
    path.symlink_to(tmp_path / "missing-target")
    profile_jobs._prune_terminal_jobs(root)
    path.unlink()

    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="profile job id already exists"):
        profile_jobs._write_new_job({"job_id": job["job_id"]})
    path.unlink()

    original_read_job = profile_jobs._read_job
    monkeypatch.setattr(
        profile_jobs,
        "_read_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read race")),
    )
    candidate = root / ("job-" + "b" * 32 + ".json")
    candidate.write_text("{}", encoding="utf-8")
    profile_jobs._prune_terminal_jobs(root)
    candidate.unlink()
    monkeypatch.setattr(profile_jobs, "_read_job", original_read_job)

    monkeypatch.setattr(profile_jobs, "PROFILE_JOB_MAX_DIRECTORY_ENTRIES", 0)
    monkeypatch.setattr(profile_jobs, "_prune_terminal_jobs", lambda _root: None)
    (root / "unrelated").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="too many profile job directory entries"):
        profile_jobs._write_new_job({"job_id": "job-" + "c" * 32})


def test_profile_job_read_and_manifest_schema_guards(tmp_path, monkeypatch):
    job = _unit_job(tmp_path, monkeypatch)
    path = profile_jobs._job_path(job["job_id"])
    path.chmod(0o640)
    with pytest.raises(ValueError, match="manifest permissions"):
        profile_jobs._read_job(job["job_id"])
    path.chmod(0o600)
    write_private_text(path, "[]\n", label="bad manifest")
    with pytest.raises(ValueError, match="manifest must be an object"):
        profile_jobs._read_job(job["job_id"])

    manifest = {
        "schema_version": 1,
        "job_id": "bad",
        "account_id": "alpha",
        "label": "Alpha",
        "browser": "firefox",
        "backend": "direct",
        "profile_dir": str(tmp_path / "profile"),
        "reactivation_browser": "auto",
        "tag": "",
        "series": "",
        "series_active": False,
        "expected_backend_account_id": None,
        "config_path": str(tmp_path / "config.toml"),
        "json_events": False,
        "status": "queued",
        "created_at": "2026-08-16T00:00:00Z",
        "updated_at": "2026-08-16T00:00:00Z",
        "worker_pid": None,
        "error": None,
    }
    with pytest.raises(ValueError, match="manifest id is invalid"):
        profile_jobs._validate_manifest(manifest)
    manifest["job_id"] = "job-" + "a" * 32
    manifest["config_path"] = "relative/config.toml"
    with pytest.raises(ValueError, match="config path is invalid"):
        profile_jobs._validate_manifest(manifest)
    manifest["config_path"] = str(tmp_path / "config.toml")
    manifest["created_at"] = "2026-08-16"
    with pytest.raises(ValueError, match="timestamp is invalid"):
        profile_jobs._validate_manifest(manifest)
    manifest["created_at"] = "2026-08-16T00:00:00Z"
    manifest["error"] = "x" * 257
    with pytest.raises(ValueError, match="error is invalid"):
        profile_jobs._validate_manifest(manifest)
    with pytest.raises(ValueError, match="manifest schema is invalid"):
        profile_jobs._validate_manifest({"schema_version": 1})


def test_profile_job_manifest_legacy_shapes_and_size_guard(tmp_path):
    base = {
        "schema_version": 1,
        "job_id": "job-" + "a" * 32,
        "account_id": "alpha",
        "label": "Alpha",
        "browser": "firefox",
        "backend": "direct",
        "profile_dir": str(tmp_path / "profile"),
        "expected_backend_account_id": None,
        "config_path": str(tmp_path / "config.toml"),
        "json_events": False,
        "status": "queued",
        "created_at": "2026-08-16T00:00:00Z",
        "updated_at": "2026-08-16T00:00:00Z",
        "worker_pid": None,
        "error": None,
    }
    legacy = profile_jobs._validate_manifest(base)
    assert legacy["reactivation_browser"] == "auto"
    newer_legacy = profile_jobs._validate_manifest(
        {**base, "reactivation_browser": "auto"}
    )
    assert newer_legacy["tag"] == ""
    with pytest.raises(ValueError, match="manifest is too large"):
        profile_jobs._serialize_manifest({"payload": "x" * profile_jobs.PROFILE_JOB_MAX_BYTES})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"", False),
        (b"x" * 4097, False),
        (b"\xff", False),
        (b"python\0-m\0codex_usage.profile_jobs\0worker\0job-abc\0", True),
        (b"python\0-m\0other\0worker\0job-abc\0", False),
    ],
)
def test_profile_job_worker_match_parser_bounds(monkeypatch, raw, expected):
    monkeypatch.setattr(profile_jobs.os, "open", lambda *_args: 42)
    monkeypatch.setattr(profile_jobs.os, "read", lambda _fd, _size: raw)
    monkeypatch.setattr(profile_jobs.os, "close", lambda _fd: None)
    assert profile_jobs._worker_matches(4321, "job-abc") is expected


def test_profile_job_worker_match_maps_open_failure(monkeypatch):
    monkeypatch.setattr(
        profile_jobs.os,
        "open",
        lambda *_args: (_ for _ in ()).throw(OSError("proc unavailable")),
    )
    assert profile_jobs._worker_matches(4321, "job-abc") is False


def test_profile_job_worker_main_maps_worker_failure(monkeypatch):
    monkeypatch.setattr(
        profile_jobs,
        "run_profile_job",
        lambda _job_id: (_ for _ in ()).throw(RuntimeError("worker failed")),
    )
    assert profile_jobs.worker_main(["job-" + "a" * 32]) == 1


def test_profile_job_module_entrypoint_returns_expected_exit_codes(monkeypatch):
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["profile_jobs", "invalid"])
    with pytest.raises(SystemExit) as invalid_exit:
        runpy.run_module("codex_usage.profile_jobs", run_name="__main__")
    assert invalid_exit.value.code == 2

    monkeypatch.setattr(sys, "argv", ["profile_jobs", "worker", "invalid"])
    with pytest.raises(SystemExit) as worker_exit:
        runpy.run_module("codex_usage.profile_jobs", run_name="__main__")
    assert worker_exit.value.code == 1
