from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from . import profile_login
from .config import (
    SUPPORTED_BACKENDS,
    SUPPORTED_BROWSERS,
    SUPPORTED_REACTIVATION_BROWSERS,
    default_config_path,
    default_state_dir,
    load_config,
)
from .direct import DirectAuthError, auth_identity_from_file
from .json_utils import loads_strict
from .models import Account
from .private_io import (
    assert_no_symlink_ancestors,
    ensure_private_directory,
    private_path_lock,
    read_private_text,
    write_private_text,
)

PROFILE_JOB_SCHEMA_VERSION = 1
PROFILE_JOB_MAX_BYTES = 64 * 1024
PROFILE_JOB_EVENT_MAX_BYTES = 8 * 1024
PROFILE_JOB_MAX_EVENTS = 8
PROFILE_JOB_EVENT_VALUE_MAX_CHARS = 512
PROFILE_JOB_MAX_RECORDS = 64
PROFILE_JOB_MAX_DIRECTORY_ENTRIES = PROFILE_JOB_MAX_RECORDS * 8
PROFILE_JOB_WORKER_REAP_TIMEOUT_SECONDS = 1
PROFILE_JOB_ID_RE = re.compile(r"job-[0-9a-f]{32}\Z")
PROFILE_JOB_ACCOUNT_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
PROFILE_JOB_STATUSES = frozenset(
    {"queued", "running", "cancel_requested", "completed", "failed", "cancelled"}
)
PROFILE_JOB_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
PROFILE_JOB_ALLOWED_ENV = frozenset(
    {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LANGUAGE",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)


def create_profile_job(
    *,
    account_id: str,
    label: str,
    browser: str,
    backend: str,
    profile_dir: str,
    expected_backend_account_id: str | None,
    config_path: Path | None,
    json_events: bool,
    reactivation_browser: str = "auto",
    tag: str = "",
    series: str = "",
    series_active: bool = False,
) -> dict[str, object]:
    _validate_create_arguments(
        account_id=account_id,
        label=label,
        browser=browser,
        backend=backend,
        profile_dir=profile_dir,
        expected_backend_account_id=expected_backend_account_id,
        json_events=json_events,
        reactivation_browser=reactivation_browser,
        tag=tag,
        series=series,
        series_active=series_active,
    )
    if config_path is not None and not isinstance(config_path, Path):
        raise ValueError("config path is invalid")
    try:
        selected_config = (config_path or default_config_path()).expanduser()
    except RuntimeError as exc:
        raise ValueError("config path is invalid") from exc
    if not selected_config.is_absolute():
        raise ValueError("config path must be absolute")
    assert_no_symlink_ancestors(selected_config.parent, label="profile job config")
    job_id = "job-" + uuid.uuid4().hex
    manifest = {
        "schema_version": PROFILE_JOB_SCHEMA_VERSION,
        "job_id": job_id,
        "account_id": account_id,
        "label": label,
        "browser": browser,
        "backend": backend,
        "profile_dir": str(Path(profile_dir).expanduser()),
        "reactivation_browser": reactivation_browser,
        "tag": tag,
        "series": series,
        "series_active": series_active,
        "expected_backend_account_id": expected_backend_account_id,
        "config_path": str(selected_config),
        "json_events": json_events,
        "status": "queued",
        "created_at": _now(),
        "updated_at": _now(),
        "worker_pid": None,
        "error": None,
    }
    _write_new_job(manifest)
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "codex_usage.profile_jobs", "worker", job_id],
            env=_worker_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        try:
            _update_job(job_id, status="failed", error="profile_job_start_failed")
        except Exception:
            try:
                _remove_untracked_job(job_id)
            except Exception:
                pass
        raise ValueError("profile job could not be started") from exc
    try:
        _update_job(job_id, worker_pid=process.pid)
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        _reap_untracked_worker(process)
        try:
            _update_job(
                job_id,
                expected_status="queued",
                status="failed",
                error="profile_job_tracking_failed",
            )
        except Exception:
            try:
                _remove_untracked_job(job_id)
            except Exception:
                pass
        raise
    return _public_job(_read_job(job_id))


def _reap_untracked_worker(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=PROFILE_JOB_WORKER_REAP_TIMEOUT_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    pid = getattr(process, "pid", None)
    killed_group = False
    if isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signal.SIGKILL)
            killed_group = True
        except (OSError, ValueError):
            pass
    if not killed_group:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=PROFILE_JOB_WORKER_REAP_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _remove_untracked_job(job_id: str) -> None:
    path = _job_path(job_id)
    with private_path_lock(path, label="profile job cleanup lock"):
        if path.is_symlink():
            raise ValueError("profile job manifest must not be a symlink")
        if not path.exists():
            return
        job = _read_job(job_id)
        if (
            job["worker_pid"] is None
            and job["status"] in {"queued", "cancel_requested"}
        ):
            path.unlink()


def profile_job_status(job_id: str) -> dict[str, object]:
    job = _read_job(job_id)
    if job["status"] in {"queued", "running", "cancel_requested"}:
        pid = job["worker_pid"]
        if job["status"] == "cancel_requested" and not (
            isinstance(pid, int) and pid > 0
        ):
            job = _update_job(
                job_id,
                expected_status="cancel_requested",
                status="cancelled",
                error=None,
            )
        elif isinstance(pid, int) and pid > 0 and not _worker_matches(pid, job_id):
            lost_status = "cancelled" if job["status"] == "cancel_requested" else "failed"
            job = _update_job(
                job_id,
                expected_status=cast(str, job["status"]),
                status=lost_status,
                error=(None if lost_status == "cancelled" else "profile_job_worker_lost"),
            )
    return _public_job(
        job,
        events=_read_job_events(job_id) if job["json_events"] else None,
    )


def list_profile_jobs(account_id: str | None = None) -> list[dict[str, object]]:
    if account_id is not None:
        if not isinstance(account_id, str) or not PROFILE_JOB_ACCOUNT_RE.fullmatch(account_id):
            raise ValueError("account id is invalid")
    root = _job_root()
    paths: list[Path] = []
    entries_seen = 0
    for path in root.iterdir():
        entries_seen += 1
        if entries_seen > PROFILE_JOB_MAX_DIRECTORY_ENTRIES:
            raise ValueError("too many profile job directory entries")
        if not PROFILE_JOB_ID_RE.fullmatch(path.stem):
            continue
        paths.append(path)
        if len(paths) > PROFILE_JOB_MAX_RECORDS:
            raise ValueError("too many profile jobs")
    paths.sort()
    jobs: list[dict[str, object]] = []
    for path in paths:
        job_id = path.stem
        job = _read_job(job_id)
        if job["status"] in PROFILE_JOB_TERMINAL_STATUSES:
            continue
        if account_id is not None and job["account_id"] != account_id:
            continue
        jobs.append(profile_job_status(job_id))
    return jobs


def cancel_profile_job(job_id: str) -> dict[str, object]:
    job = _read_job(job_id)
    status = cast(str, job["status"])
    if status in PROFILE_JOB_TERMINAL_STATUSES:
        return _public_job(job)
    expected_status: str | tuple[str, ...] = (
        (status, "running") if status == "queued" else status
    )
    updated = _update_job(
        job_id,
        expected_status=expected_status,
        status="cancel_requested",
        error=None,
    )
    pid = updated.get("worker_pid")
    if isinstance(pid, int) and pid > 0 and _worker_matches(pid, job_id):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
    current = _read_job(job_id)
    return _public_job(
        current,
        events=_read_job_events(job_id) if current["json_events"] else None,
    )


def run_profile_job(job_id: str) -> int:
    job = _read_job(job_id)
    if job["status"] == "cancel_requested":
        current = _update_job(
            job_id,
            expected_status="cancel_requested",
            status="cancelled",
            error=None,
        )
        return 0 if current["status"] == "cancelled" else 1
    started = _update_job(
        job_id,
        expected_status="queued",
        status="running",
        worker_pid=os.getpid(),
        error=None,
    )
    if started["status"] == "cancel_requested":
        current = _update_job(
            job_id,
            expected_status="cancel_requested",
            status="cancelled",
            error=None,
        )
        return 0 if current["status"] == "cancelled" else 1
    if started["status"] != "running":
        return 1
    account = Account(
        id=cast(str, job["account_id"]),
        label=cast(str, job["label"]),
        profile_dir=cast(str, job["profile_dir"]),
        tag=cast(str, job.get("tag", "")),
        browser=cast(str, job["browser"]),
        backend=cast(str, job["backend"]),
        reactivation_browser=cast(str, job["reactivation_browser"]),
        series=cast(str, job.get("series", "")),
        series_active=cast(bool, job.get("series_active", False)),
    )
    event_sink = (
        (lambda event: _append_job_event(job_id, event))
        if job["json_events"]
        else None
    )
    cancellation = False

    def handle_termination(signum: int, frame: Any) -> None:
        nonlocal cancellation
        cancellation = True

    previous_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, handle_termination)
    try:
        if cancellation or _job_cancel_requested(job_id):
            _mark_job_terminal(job_id, status="cancelled", error=None)
            return 0
        result = profile_login.run_device_login(
            account,
            Path(cast(str, job["config_path"])),
            expected_backend_account_id=cast(
                str | None, job["expected_backend_account_id"]
            ),
            isolate_process_group=False,
            event_sink=event_sink,
        )
    except profile_login.DeviceLoginError as exc:
        if cancellation or _job_cancel_requested(job_id):
            _mark_job_terminal(job_id, status="cancelled", error=None)
            return 0
        _mark_job_terminal(job_id, status="failed", error=str(exc))
        return 1
    except Exception:
        if cancellation or _job_cancel_requested(job_id):
            _mark_job_terminal(job_id, status="cancelled", error=None)
            return 0
        _mark_job_terminal(job_id, status="failed", error="profile_job_failed")
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    if cancellation or _job_cancel_requested(job_id):
        _mark_job_terminal(job_id, status="cancelled", error=None)
        return 0
    if result.ok:
        if cancellation or _job_cancel_requested(job_id):
            _mark_job_terminal(job_id, status="cancelled", error=None)
            return 0
        if not _verify_profile_job_completion(job):
            _mark_job_terminal(
                job_id,
                status="failed",
                error="profile_job_completion_postcondition_failed",
            )
            return 1
        _mark_job_terminal(job_id, status="completed", error=None)
        return 0
    _mark_job_terminal(
        job_id,
        status="failed",
        error=result.error or "profile_login_failed",
    )
    return 1


def _job_cancel_requested(job_id: str) -> bool:
    return _read_job(job_id)["status"] == "cancel_requested"


def _verify_profile_job_completion(job: dict[str, object]) -> bool:
    try:
        config = load_config(Path(str(job["config_path"])))
        account = next(
            (item for item in config.accounts if item.id == job["account_id"]),
            None,
        )
        if account is None:
            return False
        if (
            account.label != job["label"]
            or account.tag != job.get("tag", "")
            or account.profile_dir != str(Path(str(job["profile_dir"])).expanduser().absolute())
            or account.browser != job["browser"]
            or account.backend != job["backend"]
            or account.reactivation_browser != job["reactivation_browser"]
            or account.series != job.get("series", "")
            or account.series_active != job.get("series_active", False)
            or not account.auth_json_path
        ):
            return False
        auth_path = Path(account.auth_json_path)
        if auth_path.is_symlink() or not auth_path.is_file():
            return False
        file_stat = auth_path.stat()
        if (
            file_stat.st_uid != os.getuid()
            or file_stat.st_nlink != 1
            or file_stat.st_mode & 0o077
        ):
            return False
        expected_backend_account_id = job.get("expected_backend_account_id")
        if expected_backend_account_id is None:
            return True
        try:
            _, actual_backend_account_id = auth_identity_from_file(auth_path)
        except (DirectAuthError, ValueError):
            return False
        return actual_backend_account_id == expected_backend_account_id
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _mark_job_terminal(job_id: str, *, status: str, error: str | None) -> dict[str, object]:
    current = _update_job(
        job_id,
        expected_status="running",
        status=status,
        error=error,
    )
    if current["status"] == "cancel_requested":
        return _update_job(
            job_id,
            expected_status="cancel_requested",
            status="cancelled",
            error=None,
        )
    return current


def _append_job_event(job_id: str, event: object) -> None:
    event_path = _event_path(job_id)
    normalized = _normalize_job_event(event)
    with private_path_lock(event_path, label="profile job event lock"):
        events = (
            _read_job_events_unlocked(event_path)
            if event_path.exists() or event_path.is_symlink()
            else []
        )
        if normalized in events or len(events) >= PROFILE_JOB_MAX_EVENTS:
            return
        events.append(normalized)
        write_private_text(
            event_path,
            _serialize_events(events),
            label="profile job events",
        )


def _read_job_events(job_id: str) -> list[dict[str, str]]:
    path = _event_path(job_id)
    if not path.exists() and not path.is_symlink():
        return []
    with private_path_lock(path, label="profile job event lock"):
        if not path.exists() and not path.is_symlink():
            return []
        return _read_job_events_unlocked(path)


def _read_job_events_unlocked(path: Path) -> list[dict[str, str]]:
    text, file_stat = read_private_text(
        path,
        regular_label="profile job events",
        read_label="profile job events",
        max_bytes=PROFILE_JOB_EVENT_MAX_BYTES,
    )
    if file_stat.st_nlink != 1 or file_stat.st_mode & 0o077:
        raise ValueError("profile job event permissions must be 0600")
    value = loads_strict(text)
    if not isinstance(value, list) or len(value) > PROFILE_JOB_MAX_EVENTS:
        raise ValueError("profile job events are invalid")
    return [_normalize_job_event(item) for item in value]


def _normalize_job_event(event: object) -> dict[str, str]:
    if isinstance(event, dict):
        kind = event.get("kind")
        value = event.get("value")
    else:
        kind = getattr(event, "kind", None)
        value = getattr(event, "value", None)
    if not isinstance(kind, str) or kind not in {"url", "code"}:
        raise ValueError("profile job event kind is invalid")
    if (
        not isinstance(value, str)
        or not value
        or len(value) > PROFILE_JOB_EVENT_VALUE_MAX_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("profile job event value is invalid")
    if kind == "url" and not value.lower().startswith("https://"):
        raise ValueError("profile job event URL is invalid")
    return {"kind": kind, "value": value}


def _serialize_events(events: list[dict[str, str]]) -> str:
    text = json.dumps(events, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    if len(text.encode("utf-8")) > PROFILE_JOB_EVENT_MAX_BYTES:
        raise ValueError("profile job events are too large")
    return text


def _delete_job_events(job_id: str) -> None:
    path = _event_path(job_id)
    if path.is_symlink() or not path.exists():
        if path.is_symlink():
            raise ValueError("profile job events must not be a symlink")
        return
    with private_path_lock(path, label="profile job event lock"):
        if path.is_symlink():
            raise ValueError("profile job events must not be a symlink")
        if path.exists():
            path.unlink()


def _event_path(job_id: str) -> Path:
    if not isinstance(job_id, str) or not PROFILE_JOB_ID_RE.fullmatch(job_id):
        raise ValueError("profile job id is invalid")
    return _job_root() / f"{job_id}.events.json"


def worker_main(argv: list[str]) -> int:
    if not isinstance(argv, list) or len(argv) != 1 or not isinstance(argv[0], str):
        return 2
    try:
        return run_profile_job(argv[0])
    except Exception:
        return 1


def _validate_create_arguments(
    *,
    account_id: str,
    label: str,
    browser: str,
    backend: str,
    profile_dir: str,
    expected_backend_account_id: str | None,
    json_events: object,
    reactivation_browser: str,
    series: str = "",
    series_active: bool = False,
    tag: str = "",
    check_profile_path: bool = True,
) -> None:
    if not isinstance(account_id, str) or account_id in {".", "..", "__all_accounts__"}:
        raise ValueError("account id is invalid")
    if not PROFILE_JOB_ACCOUNT_RE.fullmatch(account_id):
        raise ValueError("account id is invalid")
    if not isinstance(label, str) or not label.strip() or len(label) > 256:
        raise ValueError("label is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in label):
        raise ValueError("label is invalid")
    if browser not in SUPPORTED_BROWSERS:
        raise ValueError("browser is invalid")
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError("backend is invalid")
    if reactivation_browser not in SUPPORTED_REACTIVATION_BROWSERS:
        raise ValueError("reactivation browser is invalid")
    if not isinstance(series, str) or (
        series and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,15}", series)
    ):
        raise ValueError("series is invalid")
    if not isinstance(series_active, bool):
        raise ValueError("series_active is invalid")
    if not isinstance(tag, str) or len(tag) > 8 or any(ord(c) < 32 or ord(c) == 127 for c in tag):
        raise ValueError("tag is invalid")
    if series_active and not series:
        raise ValueError("active series requires a series name")
    if not isinstance(json_events, bool):
        raise ValueError("profile job json_events is invalid")
    try:
        profile_path = Path(profile_dir).expanduser()
    except (RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("profile dir is invalid") from exc
    if not profile_path.is_absolute():
        raise ValueError("profile dir must be absolute")
    if check_profile_path:
        assert_no_symlink_ancestors(profile_path, label="profile job profile dir")
        if profile_path.is_symlink():
            raise ValueError("profile dir must not be a symlink")
    if expected_backend_account_id is not None:
        if (
            not isinstance(expected_backend_account_id, str)
            or not expected_backend_account_id
            or len(expected_backend_account_id) > 256
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in expected_backend_account_id
            )
        ):
            raise ValueError("expected backend account id is invalid")


def _worker_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key in PROFILE_JOB_ALLOWED_ENV
    }


def _job_root() -> Path:
    root = default_state_dir() / "profile-jobs"
    assert_no_symlink_ancestors(root, label="profile job directory")
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("profile job directory is invalid")
    ensure_private_directory(root, label="profile job directory")
    return root


@contextmanager
def profile_job_creation_lock():
    with private_path_lock(_job_root() / ".create", label="profile job creation lock"):
        yield


def _job_path(job_id: str) -> Path:
    if not isinstance(job_id, str) or not PROFILE_JOB_ID_RE.fullmatch(job_id):
        raise ValueError("profile job id is invalid")
    return _job_root() / f"{job_id}.json"


def _prune_terminal_jobs(root: Path) -> None:
    candidates: list[Path] = []
    entries_seen = 0
    for candidate in root.iterdir():
        entries_seen += 1
        if entries_seen > PROFILE_JOB_MAX_DIRECTORY_ENTRIES:
            raise ValueError("too many profile job directory entries")
        if PROFILE_JOB_ID_RE.fullmatch(candidate.stem):
            candidates.append(candidate)

    for path in candidates:
        job_id = path.stem
        with private_path_lock(path, label="profile job cleanup lock"):
            if path.is_symlink() or not path.exists():
                continue
            try:
                job = _read_job(job_id)
                if job["status"] not in PROFILE_JOB_TERMINAL_STATUSES:
                    continue
                _delete_job_events(job_id)
            except (FileNotFoundError, ValueError, OSError):
                continue
            path.unlink()


def _write_new_job(manifest: dict[str, object]) -> Path:
    path = _job_path(str(manifest["job_id"]))
    root = path.parent
    with private_path_lock(root / ".create", label="profile job creation lock"):
        _prune_terminal_jobs(root)
        manifest_count = 0
        entries_seen = 0
        for candidate in root.iterdir():
            entries_seen += 1
            if entries_seen > PROFILE_JOB_MAX_DIRECTORY_ENTRIES:
                raise ValueError("too many profile job directory entries")
            if PROFILE_JOB_ID_RE.fullmatch(candidate.stem):
                manifest_count += 1
                if manifest_count >= PROFILE_JOB_MAX_RECORDS:
                    raise ValueError("too many profile jobs")
        if path.exists() or path.is_symlink():
            raise ValueError("profile job id already exists")
        write_private_text(
            path,
            _serialize_manifest(manifest),
            label="profile job manifest",
        )
    return path


def _read_job(job_id: str) -> dict[str, object]:
    path = _job_path(job_id)
    text, file_stat = read_private_text(
        path,
        regular_label="profile job manifest",
        read_label="profile job manifest",
        max_bytes=PROFILE_JOB_MAX_BYTES,
    )
    if file_stat.st_nlink != 1 or file_stat.st_mode & 0o077:
        raise ValueError("profile job manifest permissions must be 0600")
    value = loads_strict(text)
    if not isinstance(value, dict):
        raise ValueError("profile job manifest must be an object")
    return _validate_manifest(value)


def _update_job(
    job_id: str,
    *,
    expected_status: str | tuple[str, ...] | None = None,
    **changes: object,
) -> dict[str, object]:
    path = _job_path(job_id)
    with private_path_lock(path, label="profile job lock"):
        current = _read_job(job_id)
        if expected_status is not None:
            expected = (expected_status,) if isinstance(expected_status, str) else expected_status
            if current["status"] not in expected:
                return current
        current.update(changes)
        current["updated_at"] = _now()
        current = _validate_manifest(current)
        write_private_text(
            path,
            _serialize_manifest(current),
            label="profile job manifest",
        )
    if current["status"] in PROFILE_JOB_TERMINAL_STATUSES:
        _delete_job_events(job_id)
    return current


def _validate_manifest(value: dict[str, Any]) -> dict[str, object]:
    required = {
        "schema_version",
        "job_id",
        "account_id",
        "label",
        "browser",
        "backend",
        "profile_dir",
        "reactivation_browser",
        "tag",
        "series",
        "series_active",
        "expected_backend_account_id",
        "config_path",
        "json_events",
        "status",
        "created_at",
        "updated_at",
        "worker_pid",
        "error",
    }
    legacy_required = required - {"reactivation_browser", "tag", "series", "series_active"}
    if set(value) == legacy_required:
        value = {
            **value,
            "reactivation_browser": "auto",
            "tag": "",
            "series": "",
            "series_active": False,
        }
    elif set(value) == required - {"tag", "series", "series_active"}:
        value = {**value, "tag": "", "series": "", "series_active": False}
    elif set(value) != required:
        raise ValueError("profile job manifest schema is invalid")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != PROFILE_JOB_SCHEMA_VERSION
    ):
        raise ValueError("profile job manifest schema version is invalid")
    _validate_create_arguments(
        account_id=value["account_id"],
        label=value["label"],
        browser=value["browser"],
        backend=value["backend"],
        profile_dir=value["profile_dir"],
        expected_backend_account_id=value["expected_backend_account_id"],
        json_events=value["json_events"],
        reactivation_browser=value["reactivation_browser"],
        tag=value["tag"],
        series=value["series"],
        series_active=value["series_active"],
        check_profile_path=False,
    )
    if not isinstance(value["job_id"], str) or not PROFILE_JOB_ID_RE.fullmatch(value["job_id"]):
        raise ValueError("profile job manifest id is invalid")
    if not isinstance(value["config_path"], str) or not Path(value["config_path"]).is_absolute():
        raise ValueError("profile job config path is invalid")
    if not isinstance(value["status"], str) or value["status"] not in PROFILE_JOB_STATUSES:
        raise ValueError("profile job status is invalid")
    for field in ("created_at", "updated_at"):
        timestamp = value[field]
        if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
            raise ValueError("profile job timestamp is invalid")
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp[:-1] + "+00:00")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("profile job timestamp is invalid") from exc
        if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
            raise ValueError("profile job timestamp is invalid")
    pid = value["worker_pid"]
    if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
        raise ValueError("profile job worker pid is invalid")
    if value["error"] is not None and (
        not isinstance(value["error"], str) or len(value["error"]) > 256
    ):
        raise ValueError("profile job error is invalid")
    return dict(value)


def _serialize_manifest(value: dict[str, object]) -> str:
    text = json_dumps(value)
    if len(text.encode("utf-8")) > PROFILE_JOB_MAX_BYTES:
        raise ValueError("profile job manifest is too large")
    return text


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"


def _public_job(
    value: dict[str, object],
    *,
    events: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    result = {
        "ok": value["status"] not in {"failed", "cancelled"},
        "job_id": value["job_id"],
        "account": value["account_id"],
        "status": value["status"],
        **({"error": value["error"]} if value["error"] else {}),
    }
    if events:
        result["events"] = events
    return result


def _worker_matches(pid: int, job_id: str) -> bool:
    cmdline = f"/proc/{pid}/cmdline"
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = -1
    try:
        fd = os.open(cmdline, flags)
        raw = os.read(fd, 4097)
    except (OSError, UnicodeError):
        return False
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > 4096:
        return False
    try:
        arguments = [part.decode("utf-8", "strict") for part in raw.split(b"\0") if part]
    except UnicodeDecodeError:
        return False
    return (
        "codex_usage.profile_jobs" in arguments
        and "worker" in arguments
        and job_id in arguments
    )


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "worker":
        raise SystemExit(worker_main(sys.argv[2:]))
    raise SystemExit(2)
