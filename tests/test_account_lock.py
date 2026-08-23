from __future__ import annotations

import errno
from concurrent.futures import ThreadPoolExecutor

import pytest

from codex_usage.account_lock import AccountLockError, account_lock

INVALID_LOCK_TIMEOUTS = (
    True,
    -1,
    float("nan"),
    float("inf"),
    float("-inf"),
    "1",
    10**10_000,
)


@pytest.mark.parametrize(
    "timeout_seconds",
    INVALID_LOCK_TIMEOUTS,
    ids=("bool", "negative", "nan", "inf", "negative-inf", "string", "huge-int"),
)
def test_account_lock_rejects_invalid_timeout_before_creating_state(
    tmp_path, monkeypatch, timeout_seconds
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    with pytest.raises(AccountLockError, match="non-negative finite"):
        with account_lock("work", timeout_seconds=timeout_seconds):
            pass

    assert not (tmp_path / "codex-usage").exists()


def test_account_lock_serializes_same_account(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    entered: list[str] = []

    def nested_attempt():
        with account_lock("work", timeout_seconds=0):
            entered.append("nested")

    with account_lock("work"):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(nested_attempt)
            with pytest.raises(AccountLockError, match="already running"):
                future.result()

    with account_lock("work"):
        entered.append("after")
    assert entered == ["after"]


def test_account_lock_rejects_symlink_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    lock_dir = tmp_path / "codex-usage" / "locks"
    lock_dir.mkdir(parents=True)
    target = tmp_path / "outside"
    target.write_text("keep", encoding="utf-8")
    (lock_dir / "work.lock").symlink_to(target)

    with pytest.raises(AccountLockError, match="regular file"):
        with account_lock("work"):
            pass

    assert target.read_text(encoding="utf-8") == "keep"


def test_account_lock_rejects_foreign_owner_file(tmp_path, monkeypatch):
    from codex_usage import account_lock as account_lock_module

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    lock_dir = tmp_path / "codex-usage" / "locks"
    lock_dir.mkdir(parents=True, mode=0o700)
    lock_path = lock_dir / "work.lock"
    lock_path.write_bytes(b"")
    lock_path.chmod(0o666)
    monkeypatch.setattr(account_lock_module, "_prepare_lock_directory", lambda _: None)
    monkeypatch.setattr(account_lock_module.os, "getuid", lambda: 2**31 - 1)

    with pytest.raises(AccountLockError, match="private regular file"):
        with account_lock("work"):
            pass


def test_account_lock_wraps_lock_file_io_error(tmp_path, monkeypatch):
    from codex_usage import account_lock as account_lock_module

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    lock_dir = tmp_path / "codex-usage" / "locks"
    lock_dir.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(account_lock_module, "_prepare_lock_directory", lambda _: None)
    monkeypatch.setattr(
        account_lock_module.os,
        "fchmod",
        lambda *_args: (_ for _ in ()).throw(OSError("chmod failed")),
    )

    with pytest.raises(AccountLockError, match="could not secure account lock") as captured:
        with account_lock("work"):
            pass

    assert isinstance(captured.value.__cause__, OSError)


@pytest.mark.parametrize(
    ("error_number", "message"),
    [
        (errno.ELOOP, "regular file"),
        (errno.EACCES, "could not open account lock"),
    ],
)
def test_account_lock_maps_open_errors(tmp_path, monkeypatch, error_number, message):
    from codex_usage import account_lock as account_lock_module

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    lock_dir = tmp_path / "codex-usage" / "locks"
    lock_dir.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(account_lock_module, "_prepare_lock_directory", lambda _: None)

    def fail_open(*_args, **_kwargs):
        raise OSError(error_number, "synthetic lock open failure")

    monkeypatch.setattr(account_lock_module.os, "open", fail_open)

    with pytest.raises(AccountLockError, match=message):
        with account_lock("work"):
            pass


def test_account_lock_retries_after_transient_contention(tmp_path, monkeypatch):
    from codex_usage import account_lock as account_lock_module

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    flock_calls = []
    sleeps = []

    def fake_flock(_fd, operation):
        flock_calls.append(operation)
        if (
            operation == account_lock_module.fcntl.LOCK_EX | account_lock_module.fcntl.LOCK_NB
            and len(flock_calls) == 1
        ):
            raise BlockingIOError

    monkeypatch.setattr(account_lock_module.fcntl, "flock", fake_flock)
    monkeypatch.setattr(account_lock_module, "_lock_deadline", lambda _timeout: 1.0)
    monkeypatch.setattr(account_lock_module.time, "monotonic", lambda: 0.1)
    monkeypatch.setattr(account_lock_module.time, "sleep", sleeps.append)

    with account_lock("work", timeout_seconds=1):
        pass

    assert sleeps == [0.05]
    assert flock_calls[-1] == account_lock_module.fcntl.LOCK_UN


def test_account_lock_ignores_unlock_error(tmp_path, monkeypatch):
    from codex_usage import account_lock as account_lock_module

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    def fail_unlock(_fd, operation):
        if operation == account_lock_module.fcntl.LOCK_UN:
            raise OSError("synthetic unlock failure")

    monkeypatch.setattr(account_lock_module.fcntl, "flock", fail_unlock)

    with account_lock("work"):
        pass


def test_account_lock_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    with pytest.raises(AccountLockError, match="invalid account id"):
        with account_lock("../outside"):
            pass

    assert not (tmp_path / "outside.lock").exists()


@pytest.mark.parametrize("account_id", [None, [], {}])
def test_account_lock_rejects_non_string_account_id(tmp_path, monkeypatch, account_id):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    with pytest.raises(AccountLockError, match="invalid account id"):
        with account_lock(account_id):
            pass

    assert not (tmp_path / "codex-usage").exists()


def test_account_lock_wraps_directory_io_error(monkeypatch):
    def fail_directory(*_args, **_kwargs):
        raise OSError("directory unavailable")

    monkeypatch.setattr(
        "codex_usage.account_lock.ensure_private_directory",
        fail_directory,
    )

    with pytest.raises(AccountLockError, match="directory unavailable") as captured:
        with account_lock("work"):
            pass

    assert isinstance(captured.value.__cause__, OSError)
