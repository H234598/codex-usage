from __future__ import annotations

import errno
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


@pytest.mark.parametrize("operation,target,nth", [
    ("root_ancestors", "ancestors", 1),
    ("lock_create_open", "open", 1),
    ("lock_existing_open", "open", 2),
    ("lock_initial_fstat", "fstat", 1),
    ("lock_fchmod", "fchmod", 1),
    ("lock_postchmod_fstat", "fstat", 2),
    ("lock_initial_lstat", "lstat", 1),
    ("lock_acquire", "flock", 1),
    ("lock_recheck_fstat", "fstat", 2),
    ("lock_recheck_lstat", "lstat", 2),
    ("root_recheck_ancestors", "ancestors", 2),
    ("lock_helper_error_close", "helper_close", 1),
    ("lock_entry_cleanup_close", "entry_close", 1),
])
def test_d372_each_entry_boundary_preserves_exception_and_attributes_operation(
    tmp_path, monkeypatch, operation, target, nth
):
    from codex_usage import source_lock as module

    root = _private_directory(tmp_path / "source")
    lock = root / ".source-lock-v2"
    lock.touch(mode=0o600)
    if operation in ("lock_fchmod", "lock_postchmod_fstat"):
        lock.chmod(0o640)
    error = OSError(errno.EROFS, "synthetic-private-marker")
    calls = 0
    if target == "ancestors":
        owner, name = module, "assert_no_symlink_ancestors"
    elif target == "lstat":
        owner, name = Path, "lstat"
    elif target == "flock":
        owner, name = module.fcntl, "flock"
    else:
        owner, name = module.os, "close" if target.endswith("close") else target
    original = getattr(owner, name)

    def failing(*args, **kwargs):
        nonlocal calls
        relevant = target != "lstat" or args[0] == lock
        if relevant:
            calls += 1
            if calls == nth:
                if target.endswith("close"):
                    original(*args, **kwargs)
                raise error
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(owner, name, failing)
        if target == "helper_close":
            patch.setattr(module.os, "fstat", lambda _fd: (_ for _ in ()).throw(ValueError()))
        if target == "entry_close":
            patch.setattr(
                module.fcntl, "flock",
                lambda *_args: (_ for _ in ()).throw(OSError(errno.EIO, "ignored")),
            )
        with module.observe_source_lock() as observation:
            with pytest.raises(OSError) as caught:
                with module.source_lock(root, timeout_seconds=0):
                    pytest.fail("entry unexpectedly succeeded")
    assert caught.value is error
    assert observation.tokens(error) == (operation, "read_only_fs")
    assert observation.tokens(OSError(errno.EIO, "different")) == ("unrecognized", "io")


@pytest.mark.parametrize("number,want", [
    (errno.EROFS, "read_only_fs"), (errno.EACCES, "permission"),
    (errno.EPERM, "permission"), (errno.ENOENT, "missing"),
    (errno.ENOTDIR, "path_structure"), (errno.ELOOP, "path_structure"),
    (errno.EISDIR, "path_structure"), (errno.ENOSPC, "storage_limit"),
    (errno.EDQUOT, "storage_limit"), (errno.EMFILE, "descriptor_limit"),
    (errno.ENFILE, "descriptor_limit"), (errno.ENOLCK, "lock_resource"),
    (errno.EBADF, "invalid_descriptor"), (errno.EINTR, "interrupted"),
    (errno.EIO, "io"), (errno.ENOSYS, "unsupported"),
    (errno.EOPNOTSUPP, "unsupported"), (999999, "other"),
    (None, "other"), (True, "other"), ("EROFS", "other"),
])
def test_d372_errno_is_finite_and_exact_integer_only(number, want):
    from codex_usage import source_lock as module

    error = OSError()
    error.errno = number
    with module.observe_source_lock() as observation:
        assert observation.tokens(error) == ("unrecognized", want)


def test_d372_swallowed_unlock_does_not_replace_entry_failure(tmp_path, monkeypatch):
    from codex_usage import source_lock as module

    root = _private_directory(tmp_path / "source")
    primary = OSError(errno.EIO, "primary")
    swallowed = OSError(errno.EROFS, "unlock")

    def failing_flock(_fd, flags):
        if flags == module.fcntl.LOCK_UN:
            raise swallowed
        raise primary

    monkeypatch.setattr(module.fcntl, "flock", failing_flock)
    with module.observe_source_lock() as observation:
        with pytest.raises(OSError) as caught:
            with module.source_lock(root, timeout_seconds=0):
                pytest.fail("entry succeeded")
    assert caught.value is primary
    assert observation.tokens(primary) == ("lock_acquire", "io")


def test_d372_observations_are_nested_and_thread_local():
    from codex_usage import source_lock as module

    error = OSError(errno.EIO, "private")

    def fail():
        raise error

    def record():
        with pytest.raises(OSError):
            module._lock_operation("lock_acquire", fail)

    with module.observe_source_lock() as outer:
        with module.observe_source_lock() as inner:
            record()
        assert inner.tokens(error) == ("lock_acquire", "io")
        assert outer.tokens(error) == ("unrecognized", "io")
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(record).result()
        assert outer.tokens(error) == ("unrecognized", "io")
        record()
        assert outer.tokens(error) == ("lock_acquire", "io")
    with module.observe_source_lock() as fresh:
        assert fresh.tokens(error) == ("unrecognized", "io")


def test_d372_custom_errno_property_and_text_are_never_evaluated():
    from codex_usage import source_lock as module

    class Hostile(OSError):
        @property
        def errno(self):
            pytest.fail("dynamic errno evaluated")

        def __str__(self):
            pytest.fail("exception formatted")

    error = Hostile(errno.EROFS, "private")
    with module.observe_source_lock() as observation:
        assert observation.tokens(error) == ("unrecognized", "read_only_fs")


def test_d372_non_oserror_class_property_is_not_evaluated():
    from codex_usage import source_lock as module

    class Hostile(ValueError):
        @property
        def __class__(self):
            pytest.fail("dynamic class evaluated")

    with module.observe_source_lock() as observation:
        assert observation.tokens(Hostile()) == ("unrecognized", "other")


def test_d372_contention_keeps_timeout_contract(tmp_path, monkeypatch):
    from codex_usage import source_lock as module

    root = _private_directory(tmp_path / "source")
    def busy(_fd, flags):
        if flags != module.fcntl.LOCK_UN:
            raise BlockingIOError(errno.EAGAIN, "busy")
    monkeypatch.setattr(module.fcntl, "flock", busy)
    with module.observe_source_lock() as observation:
        with pytest.raises(TimeoutError) as caught:
            with module.source_lock(root, timeout_seconds=0):
                pytest.fail("contention admitted")
    assert observation.tokens(caught.value) == ("unrecognized", "other")


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def test_source_lock_rejects_a_symlinked_source_root(tmp_path: Path) -> None:
    from codex_usage.source_lock import source_lock

    target = _private_directory(tmp_path / "target")
    root = tmp_path / "codex-usage"
    root.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="source root"):
        with source_lock(root, timeout_seconds=0):
            pytest.fail("symlinked source root was locked")


def test_source_lock_rebind_is_rejected_before_release(tmp_path: Path) -> None:
    from codex_usage.source_lock import source_lock

    root = _private_directory(tmp_path / "codex-usage")
    replacement = _private_directory(tmp_path / "replacement")

    with pytest.raises(ValueError, match="source root changed"):
        with source_lock(root, timeout_seconds=0) as binding:
            assert binding.root == root
            os.rename(root, tmp_path / "former-source")
            os.rename(replacement, root)
            binding.revalidate()

    item = root.lstat()
    assert stat.S_ISDIR(item.st_mode)
    assert stat.S_IMODE(item.st_mode) == 0o700


def test_source_lock_rejects_a_hard_linked_lock_file(tmp_path: Path) -> None:
    from codex_usage.source_lock import source_lock

    root = _private_directory(tmp_path / "codex-usage")
    with source_lock(root, timeout_seconds=0):
        pass
    lock_file = root / ".source-lock-v2"
    assert lock_file.is_file()
    os.link(lock_file, root / "linked-lock")

    with pytest.raises(ValueError, match="source lock must be a private regular file"):
        with source_lock(root, timeout_seconds=0):
            pytest.fail("hard-linked source lock was accepted")


def test_private_source_file_binding_is_digest_and_identity_bound(tmp_path: Path) -> None:
    from codex_usage.source_lock import capture_private_source_file

    source = tmp_path / "current.json"
    source.write_bytes(b'{"account":"alpha"}')
    source.chmod(0o600)

    payload, binding = capture_private_source_file(source, maximum=4096)

    assert payload == b'{"account":"alpha"}'
    assert binding.to_contract() == {
        "ctime_ns": source.stat().st_ctime_ns,
        "device": source.stat().st_dev,
        "gid": os.getegid(),
        "inode": source.stat().st_ino,
        "mode": 0o600,
        "mtime_ns": source.stat().st_mtime_ns,
        "sha256": "96aece557fbfb95d616dd77c78df73b36b09a9e57e778f8b74522ce0e31eb719",
        "size_bytes": len(payload),
        "uid": os.geteuid(),
    }


def test_private_source_file_binding_rejects_hard_links(tmp_path: Path) -> None:
    from codex_usage.source_lock import capture_private_source_file

    source = tmp_path / "current.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    os.link(source, tmp_path / "linked-current.json")

    with pytest.raises(ValueError, match="source file must be a private regular file"):
        capture_private_source_file(source, maximum=4096)


def test_current_state_writer_participates_in_the_source_lock(tmp_path: Path, monkeypatch) -> None:
    from datetime import UTC, datetime

    from codex_usage import state
    from codex_usage.models import AccountUsage
    from codex_usage.source_lock import source_lock

    root = _private_directory(tmp_path / "codex-usage")
    current = root / "current"
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=datetime(2026, 9, 21, tzinfo=UTC),
        backend_configured="direct",
        backend_used="direct",
    )
    monkeypatch.setattr(
        state,
        "source_lock",
        lambda path, *, create_root=False: source_lock(
            path, timeout_seconds=0, create_root=create_root
        ),
    )

    with source_lock(root, timeout_seconds=0):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(state.save_current_usage, usage, current)
            with pytest.raises(TimeoutError, match="source lock"):
                future.result()


def test_history_wal_access_participates_in_the_source_lock(tmp_path: Path, monkeypatch) -> None:
    from codex_usage import history as history_module
    from codex_usage.source_lock import source_lock

    root = _private_directory(tmp_path / "codex-usage")
    history = root / "usage-history.sqlite3"
    monkeypatch.setattr(
        history_module,
        "source_lock",
        lambda path: source_lock(path, timeout_seconds=0),
    )

    def access_history() -> None:
        with history_module.HistoryStore(history):
            pytest.fail("history SQLite access bypassed the source lock")

    with source_lock(root, timeout_seconds=0):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(access_history)
            with pytest.raises(TimeoutError, match="source lock"):
                future.result()
