from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import itertools
import json
import multiprocessing
import multiprocessing.spawn
import multiprocessing.util
import os
import pwd
import shlex
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import conftest as test_conftest
import pytest

import codex_usage.config as config_module
import codex_usage.private_io as private_io
from codex_usage.private_io import (
    assert_no_symlink_ancestors,
    ensure_private_directory,
    private_path_lock,
    read_private_text,
    write_private_text,
)

INVALID_LOCK_TIMEOUTS = (
    True,
    -1,
    float("nan"),
    float("inf"),
    float("-inf"),
    "1",
    10**10_000,
)
_LITERAL_PRODUCTION_LOCK_ROOT = Path(
    "/home/teladi/.local/state/codex-usage/locks"
)
_REAL_ISOLATION_PROBE_MARKER = "cycle27-real-lock-isolation-probe"
_OUTER_LOCK_LAUNCHER = Path(__file__).resolve().parent / "run_outer_lock_isolation.py"


class _SyntheticPrivateLockCleanupCancellation(BaseException):
    pass


def _evidence_lock_child(
    state_home_text,
    release_mode,
    current_mode,
    ready,
    release,
    result,
):
    from codex_usage.integration_evidence import IntegrationBusy, evidence_lock_set

    ready.set()
    try:
        with evidence_lock_set(
            state_home=Path(state_home_text),
            release_mode=release_mode,
            current_mode=current_mode,
            timeout_seconds=0,
            create=False,
        ):
            result.put("acquired")
    except IntegrationBusy:
        result.put("busy")
    finally:
        release.wait(10)


def _evidence_lock_child_holds(
    state_home_text,
    release_mode,
    current_mode,
    ready,
    release,
    result,
):
    from codex_usage.integration_evidence import IntegrationBusy, evidence_lock_set

    ready.set()
    try:
        with evidence_lock_set(
            state_home=Path(state_home_text),
            release_mode=release_mode,
            current_mode=current_mode,
            timeout_seconds=0,
            create=False,
        ):
            result.put("acquired")
            release.wait(10)
    except IntegrationBusy:
        result.put("busy")


def _create_evidence_lock_inodes(state_home):
    from codex_usage import integration_evidence

    integration = state_home / "codex-usage" / "integration"
    integration.mkdir(mode=0o700, parents=True)
    integration.parent.chmod(0o700)
    lock_root = private_io._private_lock_root()
    ensure_private_directory(lock_root, label="test evidence lock root")
    targets = (
        state_home / "codex-usage" / "integration" / "producer-install",
        state_home / "codex-usage" / "integration" / "current.json",
    )
    for target in targets:
        lock_path = lock_root / integration_evidence._evidence_lock_name(target)
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)


def test_private_lock_root_uses_passwd_home_for_effective_uid(
    tmp_path, monkeypatch
):
    """Would fail if HOME, XDG, or the real UID selected the lock namespace."""
    effective_uid = 12345
    real_uid = 54321
    passwd_home = tmp_path / "passwd-effective-home"
    looked_up: list[int] = []

    monkeypatch.setenv("HOME", str(tmp_path / "environment-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "environment-state"))
    monkeypatch.setattr(private_io.os, "geteuid", lambda: effective_uid)
    monkeypatch.setattr(private_io.os, "getuid", lambda: real_uid)

    def passwd_entry(uid: int):
        looked_up.append(uid)
        return type("PasswdEntry", (), {"pw_dir": str(passwd_home)})()

    monkeypatch.setattr(pwd, "getpwuid", passwd_entry)

    assert private_io._private_lock_root_from_passwd() == (
        passwd_home / ".local/state/codex-usage/locks"
    )
    assert looked_up == [effective_uid]


def child_lock_attempt(tmp_path, *, held, requested):
    from codex_usage.integration_evidence import evidence_lock_set

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    _create_evidence_lock_inodes(state_home)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    result = context.Queue()
    process = context.Process(
        target=_evidence_lock_child_holds,
        args=(
            str(state_home),
            requested[0],
            requested[1],
            ready,
            release,
            result,
        ),
    )
    try:
        with evidence_lock_set(
            state_home=state_home,
            release_mode=held[0],
            current_mode=held[1],
            timeout_seconds=0,
            create=False,
        ):
            process.start()
            assert ready.wait(10)
            child_result = result.get(timeout=10)
        release.set()
        process.join(10)
        assert process.exitcode == 0
        return child_result
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(10)


def test_lock_deadline_rejects_non_finite_monotonic_result(monkeypatch):
    monkeypatch.setattr(private_io.time, "monotonic", lambda: float("inf"))

    with pytest.raises(ValueError, match="non-negative finite"):
        private_io._lock_deadline(0)


def test_require_private_directory_maps_lstat_error(tmp_path, monkeypatch):
    path = tmp_path / "missing"

    def fail_lstat(_path):
        raise OSError("synthetic lstat failure")

    monkeypatch.setattr(Path, "lstat", fail_lstat)

    with pytest.raises(ValueError, match="must be a real directory"):
        private_io._require_private_directory(path, label="private directory")


def test_chmod_private_directory_rejects_non_directory_descriptor(
    tmp_path, monkeypatch
):
    path = tmp_path / "directory"
    path.mkdir()
    descriptor = 41
    monkeypatch.setattr(private_io.os, "open", lambda *_args, **_kwargs: descriptor)
    monkeypatch.setattr(
        private_io.os,
        "fstat",
        lambda _fd: SimpleNamespace(st_mode=0, st_uid=private_io.os.getuid()),
    )
    monkeypatch.setattr(private_io.os, "close", lambda _fd: None)

    with pytest.raises(ValueError, match="private user-owned directory"):
        private_io._chmod_private_directory(path, label="private directory")


def test_chmod_private_directory_keeps_sentinel_when_open_fails(tmp_path, monkeypatch):
    def fail_open(*_args, **_kwargs):
        raise OSError("synthetic directory open failure")

    monkeypatch.setattr(private_io.os, "open", fail_open)

    with pytest.raises(OSError, match="directory open failure"):
        private_io._chmod_private_directory(
            tmp_path,
            label="private directory",
        )


def test_assert_no_symlink_ancestors_ignores_dot_components(tmp_path):
    assert_no_symlink_ancestors(
        tmp_path / "nested" / "." / "value",
        label="private path",
    )


def test_assert_no_symlink_ancestors_handles_explicit_dot_component(monkeypatch):
    fake_path = SimpleNamespace(
        is_absolute=lambda: True,
        anchor="/",
        parts=("/", ".", "value"),
    )
    monkeypatch.setattr(private_io, "_require_path", lambda *_args, **_kwargs: fake_path)

    assert_no_symlink_ancestors(Path("/value"), label="private path")


def test_ensure_private_directory_rejects_symlink_path_after_ancestor_check(
    tmp_path, monkeypatch
):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(
        private_io,
        "assert_no_symlink_ancestors",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(ValueError, match="must not be a symlink"):
        ensure_private_directory(link, label="private directory")


def test_ensure_private_directory_maps_resolve_error(tmp_path, monkeypatch):
    def fail_resolve(_path, **_kwargs):
        raise OSError("synthetic resolve failure")

    monkeypatch.setattr(private_io.Path, "resolve", fail_resolve)

    with pytest.raises(ValueError, match="cannot be resolved safely"):
        ensure_private_directory(tmp_path / "new", label="private directory")


def test_ensure_private_directory_rejects_existing_file(tmp_path):
    target = tmp_path / "file"
    target.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="must be a real directory"):
        ensure_private_directory(target, label="private directory")


def test_ensure_private_directory_keeps_raced_mkdir_as_existing(tmp_path, monkeypatch):
    target = tmp_path / "new"
    created_paths: list[tuple[Path, int, int]] = []
    original_mkdir = private_io.os.mkdir

    def create_then_report_exists(path, mode=0o777, *, dir_fd=None):
        original_mkdir(path, mode, dir_fd=dir_fd)
        raise FileExistsError(path)

    monkeypatch.setattr(private_io.os, "mkdir", create_then_report_exists)

    ensure_private_directory(
        target,
        label="private directory",
        created_paths=created_paths,
    )

    assert target.is_dir()
    assert created_paths == []


def test_ensure_private_directory_rejects_eexist_public_component_without_chmod(
    tmp_path,
    monkeypatch,
):
    """Would fail if mkdirat EEXIST chmoded a raced non-private directory."""
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    target = parent / "child"
    original_mkdir = private_io.os.mkdir
    before: tuple[int, ...] | None = None

    def create_public_child_then_report_exists(path, mode=0o777, *, dir_fd=None):
        nonlocal before
        if dir_fd is not None and path == target.name:
            original_mkdir(path, 0o755, dir_fd=dir_fd)
            os.chmod(path, 0o755, dir_fd=dir_fd)
            item = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
            before = (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_uid,
                item.st_gid,
                item.st_nlink,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )
            raise FileExistsError(path)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(private_io.os, "mkdir", create_public_child_then_report_exists)

    with pytest.raises(ValueError, match="private user-owned directory"):
        ensure_private_directory(target, label="private directory")

    assert before is not None
    assert _lock_metadata(target) == before
    assert stat.S_IMODE(target.lstat().st_mode) == 0o755


def test_ensure_private_directory_records_created_identity(tmp_path):
    target = tmp_path / "new"
    created_paths: list[tuple[Path, int, int]] = []

    ensure_private_directory(
        target,
        label="private directory",
        created_paths=created_paths,
    )

    assert created_paths[0][0] == target
    assert created_paths[0][1:] == (target.stat().st_dev, target.stat().st_ino)


def test_file_identity_binds_uid_gid_and_ctime():
    """Would fail if FileIdentity ignored owner or ctime rebinding evidence."""
    item = SimpleNamespace(
        st_dev=1,
        st_ino=2,
        st_mode=stat.S_IFDIR | 0o700,
        st_uid=123,
        st_gid=456,
        st_ctime_ns=789,
    )

    identity = private_io._directory_identity(item)

    assert identity.uid == 123
    assert identity.gid == 456
    assert identity.ctime_ns == 789
    assert identity != dataclasses.replace(identity, uid=124)
    assert identity != dataclasses.replace(identity, ctime_ns=790)


def test_ensure_private_directory_rejects_symlink_created_in_missing_loop(
    monkeypatch,
):
    class RacingPath:
        def __init__(self):
            self.symlink_checks = 0
            self.parent = self

        def is_absolute(self):
            return True

        def is_symlink(self):
            self.symlink_checks += 1
            return self.symlink_checks >= 2

        def resolve(self, **_kwargs):
            return Path("/not-protected")

        def exists(self):
            return False

        def __str__(self):
            return "/racing-path"

    path = RacingPath()
    monkeypatch.setattr(private_io, "_require_path", lambda *_args, **_kwargs: path)
    monkeypatch.setattr(private_io, "assert_no_symlink_ancestors", lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="must not be a symlink"):
        ensure_private_directory(Path("/racing-path"), label="private directory")


def test_ensure_private_directory_rejects_path_without_parent(monkeypatch):
    class RootlessPath:
        parent = None

        def is_absolute(self):
            return True

        def is_symlink(self):
            return False

        def resolve(self, **_kwargs):
            return Path("/not-protected")

        def exists(self):
            return False

        def __str__(self):
            return "/rootless-path"

    path = RootlessPath()
    path.parent = path
    monkeypatch.setattr(private_io, "_require_path", lambda *_args, **_kwargs: path)
    monkeypatch.setattr(private_io, "assert_no_symlink_ancestors", lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="no usable directory parent"):
        ensure_private_directory(Path("/rootless-path"), label="private directory")


@pytest.mark.parametrize(
    "timeout_seconds",
    INVALID_LOCK_TIMEOUTS,
    ids=("bool", "negative", "nan", "inf", "negative-inf", "string", "huge-int"),
)
def test_private_path_lock_rejects_invalid_timeout_before_creating_lock(
    tmp_path, timeout_seconds
):
    path = tmp_path / "config.toml"

    with pytest.raises(ValueError, match="non-negative finite"):
        with private_path_lock(
            path,
            timeout_seconds=timeout_seconds,
            label="config lock",
        ):
            pass

    assert not (tmp_path / "config.toml.lock").exists()


def test_private_io_rejects_numeric_subclasses_before_arithmetic(tmp_path):
    class BrokenFloat(float):
        def __float__(self):
            raise RuntimeError("synthetic lock timeout marker")

    class BrokenInt(int):
        def __lt__(self, _other):
            raise RuntimeError("synthetic byte budget marker")

        def __and__(self, _other):
            raise RuntimeError("synthetic mode marker")

    with pytest.raises(ValueError, match="non-negative finite"):
        with private_path_lock(
            tmp_path / "config.toml",
            timeout_seconds=BrokenFloat(1),
            label="config lock",
        ):
            pass

    with pytest.raises(ValueError, match="max_bytes is invalid"):
        read_private_text(
            tmp_path / "value.txt",
            regular_label="private",
            read_label="private",
            max_bytes=BrokenInt(10),
        )

    with pytest.raises(ValueError, match="mode must be private"):
        write_private_text(
            tmp_path / "value.txt",
            "secret",
            label="private",
            mode=BrokenInt(0o600),
        )

    assert not (tmp_path / "config.toml.lock").exists()
    assert not (tmp_path / "value.txt").exists()


@pytest.mark.parametrize("path", [None, [], "invalid", 1, False, object()])
def test_private_io_rejects_non_path(path):
    with pytest.raises(ValueError, match="path is invalid"):
        assert_no_symlink_ancestors(path, label="private")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="path is invalid"):
        ensure_private_directory(path, label="private")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="path is invalid"):
        write_private_text(path, "value", label="private")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="path is invalid"):
        read_private_text(
            path,  # type: ignore[arg-type]
            regular_label="private",
            read_label="private",
            max_bytes=10,
        )
    with pytest.raises(ValueError, match="path is invalid"):
        with private_path_lock(path, label="private"):  # type: ignore[arg-type]
            pass


def test_private_io_rejects_path_subclass_before_methods(tmp_path):
    path_type = type(tmp_path)

    class BrokenPath(path_type):
        def is_symlink(self):
            raise RuntimeError("synthetic private path marker")

    with pytest.raises(ValueError, match="path is invalid"):
        write_private_text(
            BrokenPath(tmp_path / "value.json"),
            "value",
            label="private",
        )


@pytest.mark.parametrize("max_bytes", [None, True, -1, "10"])
def test_read_private_text_rejects_invalid_byte_budget(tmp_path, max_bytes):
    with pytest.raises(ValueError, match="max_bytes is invalid"):
        read_private_text(
            tmp_path / "value.txt",
            regular_label="private",
            read_label="private",
            max_bytes=max_bytes,  # type: ignore[arg-type]
        )


def test_read_private_text_rejects_symlink_path(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_text("secret", encoding="utf-8")
    path = tmp_path / "link.txt"
    path.symlink_to(target)
    monkeypatch.setattr(
        private_io,
        "assert_no_symlink_ancestors",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(ValueError, match="must be a regular file"):
        private_io.read_private_text(
            path,
            regular_label="private",
            read_label="private",
            max_bytes=100,
        )


@pytest.mark.parametrize(
    ("error_number", "message"),
    [
        (private_io.errno.ELOOP, "must be a regular file"),
        (private_io.errno.EACCES, "cannot read"),
    ],
)
def test_read_private_text_maps_open_errors(tmp_path, monkeypatch, error_number, message):
    path = tmp_path / "value.txt"
    path.write_text("secret", encoding="utf-8")

    def fail_open(*_args, **_kwargs):
        raise OSError(error_number, "synthetic open failure")

    monkeypatch.setattr(private_io.os, "open", fail_open)

    with pytest.raises(ValueError, match=message):
        private_io.read_private_text(
            path,
            regular_label="private",
            read_label="private",
            max_bytes=100,
        )


def test_read_private_text_reads_regular_utf8_file(tmp_path):
    path = tmp_path / "value.txt"
    path.write_text("secret", encoding="utf-8")

    text, item = private_io.read_private_text(
        path,
        regular_label="private",
        read_label="private",
        max_bytes=100,
    )

    assert text == "secret"
    assert item.st_size == len("secret")


def test_read_private_text_rejects_file_larger_than_budget(tmp_path):
    path = tmp_path / "value.txt"
    path.write_text("secret", encoding="utf-8")

    with pytest.raises(ValueError, match="private too large; max 3 bytes"):
        private_io.read_private_text(
            path,
            regular_label="private",
            read_label="private",
            max_bytes=3,
        )


def test_read_private_text_maps_fdopen_error(tmp_path, monkeypatch):
    path = tmp_path / "value.txt"
    path.write_text("secret", encoding="utf-8")

    def fail_fdopen(*_args, **_kwargs):
        raise OSError("synthetic read failure")

    monkeypatch.setattr(private_io.os, "fdopen", fail_fdopen)

    with pytest.raises(ValueError, match="cannot read private"):
        private_io.read_private_text(
            path,
            regular_label="private",
            read_label="private",
            max_bytes=100,
        )


def test_read_private_text_rejects_read_result_over_budget(tmp_path, monkeypatch):
    path = tmp_path / "value.txt"
    path.write_text("secret", encoding="utf-8")

    class OverlongHandle:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return b"too long"

    monkeypatch.setattr(
        private_io.os,
        "fstat",
        lambda _fd: SimpleNamespace(
            st_mode=private_io.stat.S_IFREG,
            st_uid=private_io.os.getuid(),
            st_size=0,
        ),
    )
    monkeypatch.setattr(private_io.os, "fdopen", lambda *_args, **_kwargs: OverlongHandle())

    with pytest.raises(ValueError, match="private too large; max 3 bytes"):
        private_io.read_private_text(
            path,
            regular_label="private",
            read_label="private",
            max_bytes=3,
        )


def test_read_private_text_rejects_invalid_utf8(tmp_path):
    path = tmp_path / "value.txt"
    path.write_bytes(b"\xff")

    with pytest.raises(ValueError, match="private is not valid UTF-8"):
        private_io.read_private_text(
            path,
            regular_label="private",
            read_label="private",
            max_bytes=100,
        )


@pytest.mark.parametrize("text", [None, [], 1, object()])
def test_write_private_text_rejects_invalid_text(tmp_path, text):
    with pytest.raises(ValueError, match="text is invalid"):
        write_private_text(
            tmp_path / "value.txt",
            text,  # type: ignore[arg-type]
            label="private",
        )


def test_ensure_private_directory_secures_all_new_path_components(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    existing.chmod(0o755)
    target = existing / "nested" / "private"

    ensure_private_directory(target, label="private directory")

    assert (target.stat().st_mode & 0o777) == 0o700
    assert (target.parent.stat().st_mode & 0o777) == 0o700
    assert (existing.stat().st_mode & 0o777) == 0o755


@pytest.mark.parametrize("created_paths", [(), {}, "invalid", object()])
def test_ensure_private_directory_rejects_invalid_created_paths_before_io(
    tmp_path, created_paths
):
    target = tmp_path / "new" / "nested"

    with pytest.raises(ValueError, match="created_paths is invalid"):
        ensure_private_directory(
            target,
            label="private directory",
            created_paths=created_paths,  # type: ignore[arg-type]
        )

    assert not target.exists()
    assert not (tmp_path / "new").exists()


def test_ensure_private_directory_rejects_existing_0755_without_chmod(
    tmp_path,
):
    """Would fail if an existing 0755 target was chmoded instead of rejected."""
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    before = _lock_metadata(target)
    before_hash = _lock_metadata_hash(target)

    with pytest.raises(ValueError, match="private user-owned directory"):
        ensure_private_directory(target, label="private directory")

    assert _lock_metadata(target) == before
    assert _lock_metadata_hash(target) == before_hash


def test_ensure_private_directory_rejects_ancestor_move_to_symlinked_subtree(
    tmp_path,
    monkeypatch,
):
    """Would fail if final validation reopened the full path through an ancestor symlink."""
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    state.chmod(0o700)
    target = state / "locks"
    target.mkdir(mode=0o700)
    target.chmod(0o700)
    moved_state = tmp_path / "state-moved"
    swapped = False

    def swap_ancestor_before_final_recheck(path: Path) -> None:
        nonlocal swapped
        if path == target and not swapped:
            swapped = True
            state.rename(moved_state)
            state.symlink_to(moved_state, target_is_directory=True)

    monkeypatch.setattr(
        private_io,
        "_before_private_directory_final_recheck",
        swap_ancestor_before_final_recheck,
        raising=False,
    )

    with pytest.raises(ValueError, match=r"unsafe component|changed"):
        ensure_private_directory(target, label="private directory")

    assert swapped
    assert state.is_symlink()
    assert (moved_state / "locks").is_dir()


def test_ensure_private_directory_rejects_raced_0755_without_chmod(
    tmp_path,
    monkeypatch,
):
    """Would fail if a raced-in 0755 directory was silently chmoded to 0700."""
    parent = tmp_path / "state"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    target = parent / "locks"
    original_mkdir = private_io.os.mkdir
    raced = False

    def race_existing_public_dir(path, mode=0o777, *, dir_fd=None):
        nonlocal raced
        if dir_fd is not None and path == target.name and not raced:
            raced = True
            original_mkdir(path, 0o755, dir_fd=dir_fd)
            private_io.os.chmod(path, 0o755, dir_fd=dir_fd)
            raise FileExistsError(path)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(private_io.os, "mkdir", race_existing_public_dir)

    with pytest.raises(ValueError, match="private user-owned directory"):
        ensure_private_directory(target, label="private directory")

    assert raced
    assert stat.S_IMODE(target.lstat().st_mode) == 0o755


def test_ensure_private_directory_keeps_existing_0755_parent_readonly(
    tmp_path,
):
    """Would fail if an existing parent was chmoded while creating children."""
    parent = tmp_path / "existing-parent"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    before = _lock_metadata(parent)
    target = parent / "nested" / "private"

    assert ensure_private_directory(target, label="private directory") == target

    after = _lock_metadata(parent)
    assert after[0:5] == before[0:5]
    assert stat.S_IMODE(target.lstat().st_mode) == 0o700
    assert stat.S_IMODE(target.parent.lstat().st_mode) == 0o700


def test_ensure_private_directory_rejects_post_close_rebind_without_chmod_foreign(
    tmp_path,
    monkeypatch,
):
    """Would fail if final path chmod accepted a rebound foreign directory inode."""
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    target.chmod(0o700)
    original_target = tmp_path / "target-original"
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o755)
    foreign.chmod(0o755)
    foreign_before = _lock_metadata(foreign)
    swapped = False

    def swap_before_final_recheck(path: Path) -> None:
        nonlocal swapped
        if path == target and not swapped:
            swapped = True
            target.rename(original_target)
            foreign.rename(target)

    monkeypatch.setattr(
        private_io,
        "_before_private_directory_final_recheck",
        swap_before_final_recheck,
        raising=False,
    )

    with pytest.raises(ValueError, match="changed"):
        ensure_private_directory(target, label="private directory")

    assert swapped
    assert original_target.stat().st_mode & 0o777 == 0o700
    foreign_after = _lock_metadata(target)
    assert foreign_after[:8] == foreign_before[:8]
    assert stat.S_IMODE(target.lstat().st_mode) == 0o755


def test_ensure_private_directory_rejects_created_ancestor_rebind_with_same_leaf(
    tmp_path,
    monkeypatch,
):
    """Would fail if final validation bound only the leaf inode, not the chain."""
    state = tmp_path / "state"
    target = state / "locks"
    moved_state = tmp_path / "state-original"
    swapped = False

    def swap_created_ancestor_before_final_recheck(path: Path) -> None:
        nonlocal swapped
        if path == target and not swapped:
            swapped = True
            state.rename(moved_state)
            state.mkdir(mode=0o700)
            state.chmod(0o700)
            (moved_state / "locks").rename(target)

    monkeypatch.setattr(
        private_io,
        "_before_private_directory_final_recheck",
        swap_created_ancestor_before_final_recheck,
        raising=False,
    )

    with pytest.raises(ValueError, match="changed"):
        ensure_private_directory(target, label="private directory")

    assert swapped
    assert target.is_dir()
    assert moved_state.is_dir()


def test_ensure_private_directory_fails_when_descriptor_chmod_fails(tmp_path, monkeypatch):
    target = tmp_path / "parent" / "target"
    target.parent.mkdir(mode=0o700)
    target.parent.chmod(0o700)

    def fail_fchmod(_fd, _mode):
        raise OSError("simulated descriptor chmod failure")

    monkeypatch.setattr(private_io.os, "fchmod", fail_fchmod)

    with pytest.raises(OSError, match="descriptor chmod failure"):
        ensure_private_directory(target, label="private directory")


def test_ensure_private_directory_rejects_root_before_chmod(monkeypatch):
    def fail_chmod(_self, _mode):
        pytest.fail("root must not be chmodded")

    monkeypatch.setattr(Path, "chmod", fail_chmod)

    with pytest.raises(ValueError, match="protected"):
        ensure_private_directory(Path("/"), label="private directory")


def test_ensure_private_directory_rejects_home_target_without_mutation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o755)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    with pytest.raises(ValueError, match="protected"):
        ensure_private_directory(home, label="private directory")

    assert (home.stat().st_mode & 0o777) == 0o755


def test_ensure_private_directory_rejects_symlink_after_missing_segment_without_prefix(
    tmp_path,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)
    target = tmp_path / "missing" / ".." / "redirected" / "new"

    with pytest.raises(ValueError, match="symlink ancestors"):
        ensure_private_directory(target, label="private directory")

    assert not (tmp_path / "missing").exists()
    assert not (outside / "new").exists()


def test_ensure_private_directory_rejects_parent_rebind_before_missing_child_create(
    tmp_path,
    monkeypatch,
):
    """Would fail if missing-child creation followed a rebound parent symlink."""
    lock_parent = tmp_path / "state"
    target = lock_parent / "locks"
    hidden_parent = tmp_path / "state-hidden"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    outside.chmod(0o755)
    original_mkdir = private_io.os.mkdir
    swapped = False

    def swap_parent_before_child_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        is_child_create = (
            (dir_fd is None and Path(path) == target)
            or (dir_fd is not None and path == target.name)
        )
        if is_child_create and not swapped:
            swapped = True
            lock_parent.rename(hidden_parent)
            lock_parent.symlink_to(outside, target_is_directory=True)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(private_io.os, "mkdir", swap_parent_before_child_mkdir)

    with pytest.raises(ValueError):
        ensure_private_directory(target, label="private directory")

    assert swapped
    assert not (outside / target.name).exists()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755


def test_ensure_private_directory_rejects_parent_rebind_before_open_no_follow(
    tmp_path,
    monkeypatch,
):
    """Would fail if a pre-open parent replacement escaped ValueError handling."""
    lock_parent = tmp_path / "state"
    lock_parent.mkdir(mode=0o700)
    lock_parent.chmod(0o700)
    target = lock_parent / "locks"
    hidden_parent = tmp_path / "state-hidden"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    outside.chmod(0o755)
    original_open = private_io.os.open
    swapped = False

    def swap_parent_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if (
            dir_fd is not None
            and path == lock_parent.name
            and Path(os.readlink(f"/proc/self/fd/{dir_fd}")) == tmp_path
            and not swapped
        ):
            swapped = True
            lock_parent.rename(hidden_parent)
            lock_parent.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(private_io.os, "open", swap_parent_before_open)

    with pytest.raises(ValueError):
        ensure_private_directory(target, label="private directory")

    assert swapped
    assert not (outside / target.name).exists()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755


def test_ensure_private_directory_rejects_ancestor_rebind_before_parent_open_without_mutation(
    tmp_path,
    monkeypatch,
):
    """Would fail if O_NOFOLLOW protected only the opened parent leaf."""
    root = tmp_path / "root"
    state = root / "state"
    state.mkdir(mode=0o700, parents=True)
    state.chmod(0o700)
    root.chmod(0o700)
    target = state / "locks"
    hidden_root = tmp_path / "root-hidden"
    outside = tmp_path / "outside"
    outside_state = outside / "state"
    outside.mkdir(mode=0o700)
    outside.chmod(0o700)
    original_open = private_io.os.open
    swapped = False

    def swap_ancestor_before_parent_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        opens_state = (dir_fd is None and Path(path) == state) or (
            dir_fd is not None and path == "state"
        )
        if opens_state and not swapped:
            swapped = True
            root.rename(hidden_root)
            (hidden_root / "state").rename(outside_state)
            root.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(private_io.os, "open", swap_ancestor_before_parent_open)

    with pytest.raises(ValueError):
        ensure_private_directory(target, label="private directory")

    assert swapped
    assert root.is_symlink()
    assert not (outside_state / target.name).exists()


def test_assert_no_symlink_ancestors_scans_after_missing_segment(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)
    target = tmp_path / "missing" / ".." / "redirected" / "value"

    with pytest.raises(ValueError, match="symlink ancestors"):
        assert_no_symlink_ancestors(target, label="private path")

    assert not (tmp_path / "missing").exists()


def test_ensure_private_directory_rejects_foreign_owner(tmp_path, monkeypatch):
    target = tmp_path / "private"
    target.mkdir(mode=0o700)
    target.chmod(0o700)
    monkeypatch.setattr(private_io.os, "geteuid", lambda: 2**31 - 1)

    with pytest.raises(ValueError):
        ensure_private_directory(target, label="private directory")


def test_read_private_text_rejects_foreign_owner(tmp_path, monkeypatch):
    path = tmp_path / "value.json"
    path.write_text("secret", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(private_io.os, "geteuid", lambda: 2**31 - 1)

    with pytest.raises(ValueError):
        private_io.read_private_text(
            path,
            regular_label="value",
            read_label="value",
            max_bytes=1024,
        )


def test_write_private_text_rejects_foreign_existing_owner(tmp_path, monkeypatch):
    path = tmp_path / "value.json"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(private_io.os, "geteuid", lambda: 2**31 - 1)

    with pytest.raises(ValueError):
        write_private_text(path, "new", label="value")


def test_private_path_lock_rejects_foreign_owner(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    lock_path = path.with_name(path.name + ".lock")
    lock_path.write_text("", encoding="utf-8")
    lock_path.chmod(0o600)
    monkeypatch.setattr(private_io.os, "geteuid", lambda: 2**31 - 1)

    with pytest.raises(ValueError):
        with private_path_lock(path, label="config lock"):
            pass


def test_private_path_lock_release_attempts_all_closes_after_baseexception(
    tmp_path,
    monkeypatch,
):
    """Would fail if a lock fd close failure prevented closing the root fd."""
    path = tmp_path / "config.toml"
    path.write_text("{}", encoding="utf-8")
    real_close = private_io.os.close
    releasing = False
    failed_fd: int | None = None
    close_attempts = 0

    def fail_first_release_close(fd: int) -> None:
        nonlocal close_attempts, failed_fd
        if releasing:
            close_attempts += 1
            if failed_fd is None:
                failed_fd = fd
                raise _SyntheticPrivateLockCleanupCancellation("lock close interrupted")
        real_close(fd)

    monkeypatch.setattr(private_io.os, "close", fail_first_release_close)
    try:
        with pytest.raises(BaseExceptionGroup) as exc:
            with private_path_lock(path, label="config lock"):
                releasing = True
                raise _SyntheticPrivateLockCleanupCancellation("primary cancellation")
    finally:
        if failed_fd is not None:
            try:
                real_close(failed_fd)
            except OSError:
                pass

    flattened = []
    for error in exc.value.exceptions:
        if isinstance(error, BaseExceptionGroup):
            flattened.extend(error.exceptions)
        else:
            flattened.append(error)
    assert [type(error).__name__ for error in flattened[:2]] == [
        "_SyntheticPrivateLockCleanupCancellation",
        "_SyntheticPrivateLockCleanupCancellation",
    ]
    assert close_attempts >= 2


def test_private_lock_cleanup_flattens_nested_baseexception_groups():
    """Would fail if private lock cleanup hid nested BaseException leaves."""
    with pytest.raises(BaseExceptionGroup) as exc_info:
        private_io._raise_private_lock_cleanup_errors(
            "profile lock",
            BaseExceptionGroup(
                "primary",
                [_SyntheticPrivateLockCleanupCancellation("primary cancelled")],
            ),
            [
                ExceptionGroup(
                    "cleanup",
                    [
                        OSError("unlock failed"),
                        RuntimeError("close failed"),
                    ],
                )
            ],
        )

    assert [type(error).__name__ for error in exc_info.value.exceptions] == [
        "_SyntheticPrivateLockCleanupCancellation",
        "OSError",
        "RuntimeError",
    ]


def test_write_private_text_replaces_atomically_and_keeps_mode(tmp_path):
    path = tmp_path / "value.json"
    path.write_text("old", encoding="utf-8")

    write_private_text(path, "new", label="value")

    assert path.read_text(encoding="utf-8") == "new"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert list(tmp_path.glob(".value.json.tmp-*")) == []


def test_write_private_text_keeps_live_target_single_linked_before_replace(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "value.json"
    path.write_text("old", encoding="utf-8")
    original_replace = private_io.os.replace
    observed_link_counts: list[int] = []
    observed_rollback_modes: list[int] = []

    def observe_replace(source, target):
        if Path(target) == path and ".tmp-" in Path(source).name:
            observed_link_counts.append(path.stat().st_nlink)
            rollbacks = list(tmp_path.glob(".value.json.rollback-*"))
            assert len(rollbacks) == 1
            observed_rollback_modes.append(rollbacks[0].stat().st_mode & 0o777)
        return original_replace(source, target)

    monkeypatch.setattr(private_io.os, "replace", observe_replace)

    write_private_text(path, "new", label="value")

    assert observed_link_counts == [1]
    assert observed_rollback_modes == [0o600]
    assert path.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize("artifact_kind", ["hardlink", "copy"])
def test_write_private_text_recovers_stale_rollback_artifact(
    tmp_path,
    artifact_kind,
):
    path = tmp_path / "value.json"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o600)
    rollback = tmp_path / (
        ".value.json.rollback-crash"
        if artifact_kind == "hardlink"
        else ".value.json.rollback"
    )
    if artifact_kind == "hardlink":
        rollback.hardlink_to(path)
    else:
        rollback.write_text("older", encoding="utf-8")
        rollback.chmod(0o600)

    write_private_text(path, "new", label="value")

    assert path.read_text(encoding="utf-8") == "new"
    assert path.stat().st_nlink == 1
    assert not rollback.exists()


def test_write_private_text_rejects_insecure_stale_rollback_artifact(tmp_path):
    path = tmp_path / "value.json"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o600)
    rollback = tmp_path / ".value.json.rollback-crash"
    rollback.write_text("older", encoding="utf-8")
    rollback.chmod(0o640)

    with pytest.raises(ValueError, match="private user-owned"):
        write_private_text(path, "new", label="value")

    assert path.read_text(encoding="utf-8") == "old"
    assert path.stat().st_nlink == 1
    assert rollback.read_text(encoding="utf-8") == "older"


def test_write_private_text_rejects_hardlinked_rollback_without_restoring_it(tmp_path):
    path = tmp_path / "value.json"
    rollback = tmp_path / ".value.json.rollback-crash"
    rollback.write_text("old", encoding="utf-8")
    rollback.chmod(0o600)
    alias = tmp_path / "rollback-alias"
    alias.hardlink_to(rollback)

    with pytest.raises(ValueError):
        write_private_text(path, "new", label="value")

    assert not path.exists()
    assert rollback.read_text(encoding="utf-8") == "old"
    assert alias.read_text(encoding="utf-8") == "old"
    assert rollback.stat().st_nlink == 2


@pytest.mark.parametrize("failure_stage", ["read", "write", "fsync"])
def test_write_private_text_cleans_owned_partial_rollback_on_copy_failure(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    path = tmp_path / "value.json"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o600)
    original_open = private_io.os.open
    original_read = private_io.os.read
    original_write = private_io.os.write
    original_fsync = private_io.os.fsync
    source_fds: set[int] = set()
    rollback_fds: set[int] = set()
    first_source_read = True

    def track_open(file, flags, mode=0o777, **kwargs):
        fd = original_open(file, flags, mode, **kwargs)
        opened_path = Path(file)
        if opened_path == path:
            source_fds.add(fd)
        if opened_path.name.startswith(".value.json.rollback-"):
            rollback_fds.add(fd)
        return fd

    def fail_read(fd, size):
        nonlocal first_source_read
        if failure_stage == "read" and fd in source_fds:
            if not first_source_read:
                raise OSError("synthetic rollback read failure")
            first_source_read = False
            return original_read(fd, min(size, 1))
        return original_read(fd, size)

    def fail_write(fd, value):
        if failure_stage == "write" and fd in rollback_fds:
            original_write(fd, value[:1])
            raise OSError("synthetic rollback write failure")
        return original_write(fd, value)

    def fail_fsync(fd):
        if failure_stage == "fsync" and fd in rollback_fds:
            raise OSError("synthetic rollback fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(private_io.os, "open", track_open)
    monkeypatch.setattr(private_io.os, "read", fail_read)
    monkeypatch.setattr(private_io.os, "write", fail_write)
    monkeypatch.setattr(private_io.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match=f"rollback {failure_stage} failure"):
        write_private_text(path, "new", label="value")

    assert path.read_text(encoding="utf-8") == "old"
    assert path.stat().st_nlink == 1
    assert list(tmp_path.glob(".value.json.rollback*")) == []


def test_write_private_text_rejects_oversized_rollback_source_before_copy(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "value.json"
    path.write_text("old-too-large", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(private_io, "_MAX_PRIVATE_ROLLBACK_BYTES", 3, raising=False)

    with pytest.raises(ValueError, match="too large for rollback"):
        write_private_text(path, "new", label="value")

    assert path.read_text(encoding="utf-8") == "old-too-large"
    assert list(tmp_path.glob(".value.json.rollback*")) == []


def test_overlapping_write_private_text_transactions_preserve_each_other(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "value.json"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o600)
    original_copy = private_io._copy_private_file
    first_owns_rollback = Event()
    release_first = Event()
    second_started = Event()
    second_finished = Event()
    copy_calls = 0

    def overlap_copy(*args, **kwargs):
        nonlocal copy_calls
        original_copy(*args, **kwargs)
        copy_calls += 1
        if copy_calls == 1:
            first_owns_rollback.set()
            if not release_first.wait(5):
                raise TimeoutError("test did not release first writer")

    def second_write():
        second_started.set()
        try:
            write_private_text(path, "second", label="value")
        finally:
            second_finished.set()

    monkeypatch.setattr(private_io, "_copy_private_file", overlap_copy)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(write_private_text, path, "first", label="value")
        assert first_owns_rollback.wait(5)
        second = executor.submit(second_write)
        assert second_started.wait(5)
        second_blocked = not second_finished.wait(0.2)
        release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert second_blocked
    assert path.read_text(encoding="utf-8") == "second"
    assert path.stat().st_nlink == 1
    assert list(tmp_path.glob(".value.json.rollback*")) == []


def test_write_private_text_reuses_same_thread_private_path_lock(tmp_path):
    path = tmp_path / "value.json"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o600)

    with private_path_lock(path, timeout_seconds=0, label="outer lock"):
        write_private_text(path, "new", label="value")

    assert path.read_text(encoding="utf-8") == "new"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["value.json"]


def test_private_path_lock_reenters_after_creating_an_owned_sibling_lock(
    tmp_path,
    monkeypatch,
):
    """Would fail if a held lock rejected its own sibling-lock creation."""
    lock_root = tmp_path / "locks"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    with private_path_lock(first, label="first"):
        with private_path_lock(second, label="second"):
            with private_path_lock(first, label="first reentry"):
                pass


def test_private_path_lock_rejects_replaced_held_sibling_before_second_entry(
    tmp_path,
    monkeypatch,
):
    """Would fail if a sibling replacement was absorbed into held lock state."""
    lock_root = tmp_path / "locks"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first_lock_name = private_io._private_lock_name(first)
    second_lock_name = private_io._private_lock_name(second)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"")
    replacement.chmod(0o600)
    replaced = False
    entered_second = False
    real_revalidate = private_io._revalidate_existing_private_lock

    def replace_first_after_second_revalidation(**kwargs):
        nonlocal replaced
        result = real_revalidate(**kwargs)
        if not replaced and kwargs["lock_name"] == second_lock_name:
            os.replace(replacement, lock_root / first_lock_name)
            replaced = True
        return result

    monkeypatch.setattr(
        private_io,
        "_revalidate_existing_private_lock",
        replace_first_after_second_revalidation,
    )

    with private_path_lock(first, label="first"):
        with pytest.raises(ValueError, match=r"changed while locking|namespace changed"):
            with private_path_lock(second, label="second"):
                entered_second = True

    assert replaced
    assert not entered_second


def test_private_path_lock_rejects_replaced_held_sibling_for_existing_second_lock(
    tmp_path,
    monkeypatch,
):
    """Would fail if FileExistsError sibling entry skipped held-lock validation."""
    lock_root = tmp_path / "locks"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first_lock_name = private_io._private_lock_name(first)
    second_lock_name = private_io._private_lock_name(second)

    # Seed both files so acquiring second exercises os.open(... O_EXCL)'s
    # FileExistsError path rather than sibling creation.
    with private_path_lock(first, label="seed first"):
        pass
    with private_path_lock(second, label="seed second"):
        pass
    assert (lock_root / second_lock_name).is_file()

    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"")
    replacement.chmod(0o600)
    replaced = False
    entered_second = False
    real_revalidate = private_io._revalidate_existing_private_lock

    def replace_first_after_second_revalidation(**kwargs):
        nonlocal replaced
        result = real_revalidate(**kwargs)
        if not replaced and kwargs["lock_name"] == second_lock_name:
            os.replace(replacement, lock_root / first_lock_name)
            replaced = True
        return result

    monkeypatch.setattr(
        private_io,
        "_revalidate_existing_private_lock",
        replace_first_after_second_revalidation,
    )

    with private_path_lock(first, label="first"):
        with pytest.raises(ValueError, match=r"changed while locking|namespace changed"):
            with private_path_lock(second, label="second"):
                entered_second = True

    assert replaced
    assert not entered_second


@pytest.mark.parametrize("mode", [0o640, True, "600", -1])
def test_write_private_text_rejects_non_private_mode(tmp_path, mode):
    path = tmp_path / "value.json"

    with pytest.raises(ValueError, match="mode must be private"):
        write_private_text(path, "secret", label="value", mode=mode)  # type: ignore[arg-type]

    assert not path.exists()


def test_write_private_text_rejects_existing_directory(tmp_path):
    path = tmp_path / "value"
    path.mkdir()

    with pytest.raises(ValueError, match="must be a regular file"):
        write_private_text(path, "secret", label="value")


def test_write_private_text_rejects_non_directory_parent(tmp_path):
    parent = tmp_path / "parent"
    parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="parent must be a real directory"):
        write_private_text(parent / "value", "secret", label="value")


def test_write_private_text_rejects_invalid_temporary_descriptor(tmp_path, monkeypatch):
    path = tmp_path / "value"
    original_fstat = private_io.os.fstat

    def reject_temporary(fd):
        original = original_fstat(fd)
        try:
            opened_name = Path(os.readlink(f"/proc/self/fd/{fd}")).name
        except OSError:
            return original
        if not opened_name.startswith(".value.tmp-"):
            return original
        return SimpleNamespace(
            st_mode=0,
            st_nlink=1,
            st_uid=private_io.os.getuid(),
        )

    monkeypatch.setattr(private_io.os, "fstat", reject_temporary)

    with pytest.raises(ValueError, match="temporary value is not a private regular file"):
        write_private_text(path, "secret", label="value")


def test_write_private_text_rejects_short_write(tmp_path, monkeypatch):
    monkeypatch.setattr(private_io.os, "write", lambda *_args: 0)

    with pytest.raises(OSError, match="short write for value"):
        write_private_text(tmp_path / "value", "secret", label="value")


def test_create_only_write_groups_unlink_and_rollback_errors(tmp_path, monkeypatch):
    path = tmp_path / "value.json"
    original_unlink = Path.unlink

    def fail_target_and_temporary_unlink(self, *, missing_ok=False):
        if self == path or self.name.startswith(".value.json.tmp-"):
            raise OSError("unlink failed")
        return original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_target_and_temporary_unlink)

    with pytest.raises(ExceptionGroup) as exc:
        write_private_text(path, "new", label="value", replace_existing=False)

    assert [str(error) for error in exc.value.exceptions] == [
        "unlink failed",
        "unlink failed",
    ]


def test_write_private_text_maps_replace_symlink_error(tmp_path, monkeypatch):
    path = tmp_path / "value"

    def fail_replace(*_args, **_kwargs):
        raise OSError(private_io.errno.ELOOP, "synthetic replace symlink")

    monkeypatch.setattr(private_io.os, "replace", fail_replace)

    with pytest.raises(ValueError, match="must be a regular file"):
        write_private_text(path, "secret", label="value")


def test_write_private_text_rejects_string_subclass_before_encode(tmp_path):
    class BrokenStr(str):
        def encode(self, *_args, **_kwargs):
            raise RuntimeError("synthetic private text marker")

    with pytest.raises(ValueError, match="text is invalid"):
        write_private_text(
            tmp_path / "value.json",
            BrokenStr("secret"),
            label="value",
        )


def test_write_private_text_can_create_without_replacing_existing_file(tmp_path):
    path = tmp_path / "value.json"
    path.write_text("old", encoding="utf-8")

    with pytest.raises(ValueError, match="existing file"):
        write_private_text(path, "new", label="value", replace_existing=False)

    assert path.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".value.json.tmp-*")) == []


def test_create_only_write_rolls_back_target_when_temporary_unlink_fails(
    tmp_path, monkeypatch
):
    path = tmp_path / "value.json"
    original_unlink = Path.unlink

    def fail_temporary_unlink(self, *, missing_ok=False):
        if self.name.startswith(".value.json.tmp-"):
            raise OSError("temporary unlink failed")
        return original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)

    with pytest.raises(OSError, match="temporary unlink failed"):
        write_private_text(
            path,
            "new",
            label="value",
            replace_existing=False,
        )

    assert not path.exists()
    assert len(list(tmp_path.glob(".value.json.tmp-*"))) == 1


def test_private_io_rejects_symlinked_ancestor(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink ancestors"):
        write_private_text(redirected / "value.json", "secret", label="value")

    assert not (outside / "value.json").exists()


def test_private_io_rejects_symlink_ancestor_hidden_before_dotdot(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)
    deceptive_path = redirected / ".." / "escape.json"

    with pytest.raises(ValueError, match="symlink ancestors"):
        write_private_text(deceptive_path, "secret", label="value")

    assert not (tmp_path / "escape.json").exists()
    assert not list(tmp_path.glob(".escape.json.tmp-*"))


def test_write_private_text_keeps_old_value_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "value.json"
    path.write_text("old", encoding="utf-8")

    def fail_replace(source, target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("codex_usage.private_io.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failure"):
        write_private_text(path, "new", label="value")

    assert path.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".value.json.tmp-*")) == []


def test_write_private_text_preserves_replace_error_when_cleanup_fails(
    tmp_path, monkeypatch
):
    path = tmp_path / "value.json"
    path.write_text("old", encoding="utf-8")

    def fail_replace(source, target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("codex_usage.private_io.os.replace", fail_replace)

    from pathlib import Path

    original_unlink = Path.unlink

    def fail_temporary_cleanup(self, *, missing_ok=False):
        if self.name.startswith(".value.json.tmp-"):
            raise OSError("simulated cleanup failure")
        return original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_temporary_cleanup)

    with pytest.raises(OSError, match="replace failure"):
        write_private_text(path, "new", label="value")

    assert path.read_text(encoding="utf-8") == "old"


def test_write_private_text_keeps_old_value_when_fsync_fails(tmp_path, monkeypatch):
    path = tmp_path / "value.json"
    path.write_text("old", encoding="utf-8")

    monkeypatch.setattr(
        "codex_usage.private_io.os.fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("simulated fsync failure")),
    )

    with pytest.raises(OSError, match="fsync failure"):
        write_private_text(path, "new", label="value")

    assert path.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".value.json.tmp-*")) == []


def test_write_private_text_restores_old_value_when_directory_fsync_fails(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "value.json"
    path.write_text("old", encoding="utf-8")
    fsync_calls = 0

    def fail_post_replace_fsync(_path):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(private_io, "_fsync_directory", fail_post_replace_fsync)

    with pytest.raises(OSError, match="directory fsync failure"):
        write_private_text(path, "new", label="value")

    assert path.read_text(encoding="utf-8") == "old"
    assert fsync_calls >= 2
    assert list(tmp_path.glob(".value.json.*-*")) == []


@pytest.mark.parametrize("error_number", [private_io.errno.EINVAL, private_io.errno.EACCES])
def test_fsync_directory_maps_open_errors(tmp_path, monkeypatch, error_number):
    def fail_open(*_args, **_kwargs):
        raise OSError(error_number, "synthetic directory open failure")

    monkeypatch.setattr(private_io.os, "open", fail_open)

    if error_number == private_io.errno.EINVAL:
        private_io._fsync_directory(tmp_path)
    else:
        with pytest.raises(OSError, match="directory open failure"):
            private_io._fsync_directory(tmp_path)


def test_private_path_lock_rejects_non_directory_parent(tmp_path):
    parent = tmp_path / "parent"
    parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="parent must be a real directory"):
        with private_path_lock(parent / "config", label="config lock"):
            pass


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_private_path_lock_rejects_invalid_lock_path(tmp_path, kind):
    path = tmp_path / "config"
    lock_path = private_io._private_lock_path(path)
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("target", encoding="utf-8")
        lock_path.symlink_to(target)
    else:
        lock_path.mkdir()

    try:
        with pytest.raises(ValueError, match=r"regular file|private lock namespace"):
            with private_path_lock(path, label="config lock"):
                pass
    finally:
        if lock_path.is_symlink() or lock_path.is_file():
            lock_path.unlink()
        elif lock_path.is_dir():
            lock_path.rmdir()


@pytest.mark.parametrize("error_number", [private_io.errno.ELOOP, private_io.errno.EACCES])
def test_private_path_lock_maps_open_errors(tmp_path, monkeypatch, error_number):
    def fail_open(*_args, **_kwargs):
        raise OSError(error_number, "synthetic lock open failure")

    monkeypatch.setattr(private_io.os, "open", fail_open)
    monkeypatch.setattr(
        private_io,
        "_private_lock_path",
        lambda path: path.with_name(path.name + ".lock"),
    )

    if error_number == private_io.errno.ELOOP:
        with pytest.raises(ValueError, match="must be a regular file"):
            with private_path_lock(tmp_path / "config", label="config lock"):
                pass
    else:
        with pytest.raises(OSError, match="lock open failure"):
            with private_path_lock(tmp_path / "config", label="config lock"):
                pass


def test_private_path_lock_retries_after_transient_contention(tmp_path, monkeypatch):
    path = tmp_path / "config"
    flock_calls = []
    sleeps = []

    def fake_flock(_fd, operation):
        flock_calls.append(operation)
        if (
            operation == private_io.fcntl.LOCK_EX | private_io.fcntl.LOCK_NB
            and len(flock_calls) == 1
        ):
            raise BlockingIOError

    monotonic_values = iter([0.0, 0.1])
    monkeypatch.setattr(private_io.fcntl, "flock", fake_flock)
    monkeypatch.setattr(private_io.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(private_io.time, "sleep", sleeps.append)

    with private_path_lock(path, timeout_seconds=1, label="config lock"):
        pass

    assert sleeps == [0.05]
    assert flock_calls[-1] == private_io.fcntl.LOCK_UN


def test_private_path_lock_records_only_lock_file_created_by_transaction(tmp_path):
    path = tmp_path / "config"
    created_lock_files = []

    with private_path_lock(
        path,
        label="config lock",
        created_lock_files=created_lock_files,
    ):
        pass

    lock_path = created_lock_files[0][0]
    lock_stat = lock_path.lstat()
    assert created_lock_files == [
        (lock_path, lock_stat.st_dev, lock_stat.st_ino)
    ]

    preexisting_lock_files = []
    with private_path_lock(
        path,
        label="config lock",
        created_lock_files=preexisting_lock_files,
    ):
        pass

    assert preexisting_lock_files == []


def test_private_path_lock_no_create_does_not_create_missing_lock_root(
    tmp_path, monkeypatch
):
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()
    missing_lock_root = tmp_path / "missing-lock-root"
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: missing_lock_root)

    with pytest.raises(FileNotFoundError):
        with private_path_lock(target, label="profile lock", create=False):
            pass

    assert not missing_lock_root.exists()


def test_private_path_lock_no_create_reuses_same_thread_lock(tmp_path):
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()

    with private_path_lock(target, label="outer profile lock"):
        with private_path_lock(target, label="nested profile lock", create=False):
            pass


def test_private_path_lock_create_allows_clean_namespace(tmp_path, monkeypatch):
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)

    with private_path_lock(target, label="profile lock", create=True):
        assert (lock_root / private_io._private_lock_name(target)).is_file()


def _lock_metadata(path: Path, *, follow_symlinks: bool = True) -> tuple[int, ...]:
    item = path.stat() if follow_symlinks else path.lstat()
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_uid,
        item.st_gid,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _lock_metadata_hash(path: Path, *, follow_symlinks: bool = True) -> str:
    payload = repr(_lock_metadata(path, follow_symlinks=follow_symlinks)).encode()
    return hashlib.sha256(payload).hexdigest()


def _existing_private_lock(
    tmp_path: Path,
    monkeypatch,
    *,
    root_mode: int = 0o700,
    lock_mode: int = 0o600,
    payload: bytes = b"",
) -> tuple[Path, Path, Path]:
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(root_mode)
    lock_path = lock_root / private_io._private_lock_name(target)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, lock_mode)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    lock_path.chmod(lock_mode)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)
    return target, lock_root, lock_path


def _touch_private_lock(path: Path) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    path.chmod(0o600)


def _canonical_lock_name(index: int) -> str:
    return f"{index:064x}.lock"


@pytest.mark.parametrize(
    ("machine", "expected_number"),
    [
        ("x86_64", 316),
        ("aarch64", 276),
        ("arm64", 276),
    ],
)
def test_renameat2_syscall_number_maps_supported_architectures(
    monkeypatch,
    machine,
    expected_number,
):
    monkeypatch.setattr(private_io.os, "uname", lambda: SimpleNamespace(machine=machine))

    assert private_io._renameat2_syscall_number() == expected_number


def test_renameat2_syscall_number_rejects_unknown_architecture(monkeypatch):
    monkeypatch.setattr(
        private_io.os,
        "uname",
        lambda: SimpleNamespace(machine="mips64"),
    )

    with pytest.raises(OSError, match="renameat2 unsupported on mips64"):
        private_io._renameat2_syscall_number()


class _FakeRenameAt2Syscall:
    def __init__(self, result: int):
        self.result = result
        self.restype = None
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class _LibcWithoutRenameAt2:
    renameat2 = None

    def __init__(self, syscall):
        self.syscall = syscall


class _LibcWithRenameAt2:
    def __init__(self, renameat2, syscall):
        self.renameat2 = renameat2
        self.syscall = syscall


def test_rename_private_lock_residue_uses_raw_syscall_without_libc_renameat2(
    monkeypatch,
):
    """Would fail if the libc-missing branch used the wrong syscall contract."""
    syscall = _FakeRenameAt2Syscall(0)
    monkeypatch.setattr(
        private_io.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: _LibcWithoutRenameAt2(syscall),
    )
    monkeypatch.setattr(
        private_io.os,
        "uname",
        lambda: SimpleNamespace(machine="x86_64"),
    )

    private_io._rename_private_lock_residue_no_replace(
        source_fd=11,
        source_name="source.lock",
        destination_fd=12,
        destination_name="target.lock",
    )

    assert len(syscall.calls) == 1
    number, source_fd, source_name, destination_fd, destination_name, flags = syscall.calls[0]
    assert number.value == 316
    assert source_fd.value == 11
    assert source_name.value == b"source.lock"
    assert destination_fd.value == 12
    assert destination_name.value == b"target.lock"
    assert flags.value == private_io._RENAME_NOREPLACE


def test_rename_private_lock_residue_prefers_libc_renameat2_when_present(
    monkeypatch,
):
    """Would fail if libc renameat2 availability still fell through to syscall."""
    renameat2 = _FakeRenameAt2Syscall(0)
    syscall = _FakeRenameAt2Syscall(0)
    monkeypatch.setattr(
        private_io.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: _LibcWithRenameAt2(renameat2, syscall),
    )

    private_io._rename_private_lock_residue_no_replace(
        source_fd=13,
        source_name="source.lock",
        destination_fd=14,
        destination_name="target.lock",
    )

    assert len(renameat2.calls) == 1
    assert syscall.calls == []


def test_rename_private_lock_residue_raw_syscall_propagates_enosys(
    monkeypatch,
):
    """Would fail if a missing kernel renameat2 syscall used a weaker fallback."""
    syscall = _FakeRenameAt2Syscall(-1)
    monkeypatch.setattr(
        private_io.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: _LibcWithoutRenameAt2(syscall),
    )
    monkeypatch.setattr(private_io.ctypes, "get_errno", lambda: private_io.errno.ENOSYS)
    monkeypatch.setattr(
        private_io.os,
        "uname",
        lambda: SimpleNamespace(machine="x86_64"),
    )

    with pytest.raises(OSError) as exc_info:
        private_io._rename_private_lock_residue_no_replace(
            source_fd=15,
            source_name="source.lock",
            destination_fd=16,
            destination_name="target.lock",
        )

    assert exc_info.value.errno == private_io.errno.ENOSYS
    assert len(syscall.calls) == 1


@pytest.mark.parametrize(
    "error_number",
    [private_io.errno.EEXIST, private_io.errno.EACCES],
)
def test_rename_private_lock_residue_raw_syscall_propagates_errors(
    monkeypatch,
    error_number,
):
    """Would fail if raw-syscall EEXIST or hard errors were normalized away."""
    syscall = _FakeRenameAt2Syscall(-1)
    monkeypatch.setattr(
        private_io.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: _LibcWithoutRenameAt2(syscall),
    )
    monkeypatch.setattr(private_io.ctypes, "get_errno", lambda: error_number)
    monkeypatch.setattr(
        private_io.os,
        "uname",
        lambda: SimpleNamespace(machine="x86_64"),
    )

    with pytest.raises(OSError) as exc_info:
        private_io._rename_private_lock_residue_no_replace(
            source_fd=21,
            source_name="source.lock",
            destination_fd=22,
            destination_name="target.lock",
        )

    assert exc_info.value.errno == error_number
    assert len(syscall.calls) == 1


def test_rename_private_lock_residue_raw_syscall_unknown_arch_fails_closed(
    monkeypatch,
):
    """Would fail if an unknown raw-syscall ABI attempted a best-effort rename."""
    syscall = _FakeRenameAt2Syscall(0)
    monkeypatch.setattr(
        private_io.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: _LibcWithoutRenameAt2(syscall),
    )
    monkeypatch.setattr(
        private_io.os,
        "uname",
        lambda: SimpleNamespace(machine="mips64"),
    )

    with pytest.raises(OSError, match="renameat2 unsupported on mips64"):
        private_io._rename_private_lock_residue_no_replace(
            source_fd=31,
            source_name="source.lock",
            destination_fd=32,
            destination_name="target.lock",
        )

    assert syscall.calls == []


def _pytest_residue_run(index: int) -> int:
    return (1081, 1097, 1118, 1127)[index]


def _pytest_residue_config_path(index: int, parameter_index: int) -> Path:
    return Path(
        f"/tmp/pytest-of-teladi/pytest-{_pytest_residue_run(index)}/"
        f"test_private_path_lock_rejects{parameter_index}/config"
    )


def _pytest_residue_lock_name(index: int, parameter_index: int) -> str:
    return private_io._private_lock_name(
        _pytest_residue_config_path(index, parameter_index)
    )


def _pytest_residue_symlink_target(index: int) -> Path:
    return _pytest_residue_config_path(index, 9).with_name("target")


def _operator_residue_approvals(
    report: private_io.PrivateLockNamespaceReport,
) -> tuple[private_io.PrivateLockResidueApproval, ...]:
    reason_by_type = {
        "directory": "operator-approved-empty-directory-lock",
        "symlink": "operator-approved-dangling-symlink-lock",
    }
    return tuple(
        private_io.PrivateLockResidueApproval(
            name=issue.name,
            reason=reason_by_type[issue.snapshot.file_type],
            snapshot=issue.snapshot,
        )
        for issue in report.issues
        if issue.reason == "unsupported-private-lock-residue"
        and issue.snapshot.file_type in reason_by_type
    )


def _manual_private_lock_reconcile_report(
    lock_root: Path,
) -> private_io.PrivateLockNamespaceReport:
    report = private_io.scan_private_lock_namespace(lock_root)
    approvals = _operator_residue_approvals(report)
    if not approvals:
        return report
    return private_io.scan_private_lock_namespace(
        lock_root,
        approved_residues=approvals,
    )


def test_private_lock_namespace_scan_reports_approved_residue_shapes(
    tmp_path,
):
    """Would fail if noncanonical or non-regular lock residues stayed invisible."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    _touch_private_lock(lock_root / _canonical_lock_name(1))
    _touch_private_lock(lock_root / f"{_canonical_lock_name(1)}.moved")
    _touch_private_lock(lock_root / _canonical_lock_name(2))
    _touch_private_lock(lock_root / f"{_canonical_lock_name(2)}.moved-nested")
    for index in range(4):
        directory = lock_root / _pytest_residue_lock_name(index, 10)
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    for index in range(4):
        symlink = lock_root / _pytest_residue_lock_name(index, 9)
        symlink.symlink_to(_pytest_residue_symlink_target(index))

    raw_report = private_io.scan_private_lock_namespace(lock_root)

    assert len(raw_report.issues) == 10
    assert sorted((issue.name, issue.reason) for issue in raw_report.issues) == sorted([
        (f"{_canonical_lock_name(1)}.moved", "approved-empty-moved-lock"),
        (f"{_canonical_lock_name(2)}.moved-nested", "approved-empty-moved-lock"),
        (_pytest_residue_lock_name(0, 10), "unsupported-private-lock-residue"),
        (_pytest_residue_lock_name(0, 9), "unsupported-private-lock-residue"),
        (_pytest_residue_lock_name(1, 10), "unsupported-private-lock-residue"),
        (_pytest_residue_lock_name(1, 9), "unsupported-private-lock-residue"),
        (_pytest_residue_lock_name(2, 10), "unsupported-private-lock-residue"),
        (_pytest_residue_lock_name(2, 9), "unsupported-private-lock-residue"),
        (_pytest_residue_lock_name(3, 10), "unsupported-private-lock-residue"),
        (_pytest_residue_lock_name(3, 9), "unsupported-private-lock-residue"),
    ])
    report = private_io.scan_private_lock_namespace(
        lock_root,
        approved_residues=_operator_residue_approvals(raw_report),
    )
    assert sorted((issue.name, issue.reason) for issue in report.issues) == sorted([
        (f"{_canonical_lock_name(1)}.moved", "approved-empty-moved-lock"),
        (f"{_canonical_lock_name(2)}.moved-nested", "approved-empty-moved-lock"),
        (_pytest_residue_lock_name(0, 10), "operator-approved-empty-directory-lock"),
        (_pytest_residue_lock_name(0, 9), "operator-approved-dangling-symlink-lock"),
        (_pytest_residue_lock_name(1, 10), "operator-approved-empty-directory-lock"),
        (_pytest_residue_lock_name(1, 9), "operator-approved-dangling-symlink-lock"),
        (_pytest_residue_lock_name(2, 10), "operator-approved-empty-directory-lock"),
        (_pytest_residue_lock_name(2, 9), "operator-approved-dangling-symlink-lock"),
        (_pytest_residue_lock_name(3, 10), "operator-approved-empty-directory-lock"),
        (_pytest_residue_lock_name(3, 9), "operator-approved-dangling-symlink-lock"),
    ])
    assert all(issue.snapshot.uid == os.geteuid() for issue in report.issues)
    symlink_issues = [
        issue for issue in report.issues if issue.snapshot.file_type == "symlink"
    ]
    assert sorted(issue.snapshot.symlink_target for issue in symlink_issues) == sorted(
        str(_pytest_residue_symlink_target(index)) for index in range(4)
    )
    directory_issues = [
        issue for issue in report.issues if issue.snapshot.file_type == "directory"
    ]
    assert {
        stat.S_IMODE(issue.snapshot.mode)
        for issue in directory_issues
    } == {0o700}
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )
    assert len(approval) == 64
    assert len(
        private_io._private_lock_quarantine_targets(report, quarantine_root)
    ) == 10


def test_private_lock_namespace_scan_rejects_old_long_pytest_residue_basename(
    tmp_path,
):
    """Would fail if the old generic pytest basename stayed approval-capable."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    long_config = Path(
        "/tmp/pytest-of-teladi/pytest-1081/"
        "test_private_path_lock_rejects_invalid_lock_path_rejects10/config"
    )
    long_symlink_config = long_config.parent.with_name(
        "test_private_path_lock_rejects_invalid_lock_path_rejects9"
    ) / "config"
    directory = lock_root / private_io._private_lock_name(long_config)
    symlink = lock_root / private_io._private_lock_name(long_symlink_config)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    symlink.symlink_to(long_symlink_config.with_name("target"))

    report = private_io.scan_private_lock_namespace(lock_root)

    assert sorted((issue.name, issue.reason) for issue in report.issues) == sorted([
        (directory.name, "unsupported-private-lock-residue"),
        (symlink.name, "unsupported-private-lock-residue"),
    ])


def test_private_lock_namespace_scan_rejects_unapproved_pytest_run_pair(
    tmp_path,
):
    """Would fail if any matching pytest-N run was treated as approved evidence."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    run = 9999
    config = Path(
        f"/tmp/pytest-of-teladi/pytest-{run}/"
        "test_private_path_lock_rejects10/config"
    )
    symlink_config = config.parent.with_name("test_private_path_lock_rejects9") / "config"
    directory = lock_root / private_io._private_lock_name(config)
    symlink = lock_root / private_io._private_lock_name(symlink_config)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    symlink.symlink_to(symlink_config.with_name("target"))

    report = private_io.scan_private_lock_namespace(lock_root)

    assert sorted((issue.name, issue.reason) for issue in report.issues) == sorted([
        (directory.name, "unsupported-private-lock-residue"),
        (symlink.name, "unsupported-private-lock-residue"),
    ])


def test_private_lock_namespace_scan_accepts_real_pytest_umask_directory_residue(
    tmp_path,
):
    """Would fail if real mkdir residue mode 0755 was excluded from evidence."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    directory = lock_root / _pytest_residue_lock_name(0, 10)
    symlink = lock_root / _pytest_residue_lock_name(0, 9)
    old_umask = os.umask(0o022)
    try:
        directory.mkdir(mode=0o777)
    finally:
        os.umask(old_umask)
    symlink.symlink_to(_pytest_residue_symlink_target(0))

    raw_report = private_io.scan_private_lock_namespace(lock_root)
    report = private_io.scan_private_lock_namespace(
        lock_root,
        approved_residues=_operator_residue_approvals(raw_report),
    )

    assert stat.S_IMODE(directory.lstat().st_mode) == 0o755
    assert sorted((issue.name, issue.reason) for issue in report.issues) == sorted([
        (_pytest_residue_lock_name(0, 10), "operator-approved-empty-directory-lock"),
        (_pytest_residue_lock_name(0, 9), "operator-approved-dangling-symlink-lock"),
    ])


def test_private_lock_namespace_scan_rejects_pytest_symlink_hardlink_alias(
    tmp_path,
):
    """Would fail if approved pytest symlink evidence could leave an external alias."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    directory = lock_root / _pytest_residue_lock_name(0, 10)
    symlink = lock_root / _pytest_residue_lock_name(0, 9)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    symlink.symlink_to(_pytest_residue_symlink_target(0))
    os.link(symlink, tmp_path / "external-symlink-alias", follow_symlinks=False)

    report = private_io.scan_private_lock_namespace(lock_root)

    assert symlink.lstat().st_nlink == 2
    assert sorted((issue.name, issue.reason) for issue in report.issues) == sorted([
        (directory.name, "unsupported-private-lock-residue"),
        (symlink.name, "unsupported-private-lock-residue"),
    ])


@pytest.mark.parametrize(
    "shape",
    ("unpaired-directory", "nonempty-directory", "world-writable-directory"),
)
def test_private_lock_namespace_scan_rejects_directory_residue_without_exact_provenance(
    tmp_path,
    shape,
):
    """Would fail if any empty canonical directory was treated as approved."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    directory = lock_root / _pytest_residue_lock_name(0, 10)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    if shape != "unpaired-directory":
        symlink = lock_root / _pytest_residue_lock_name(0, 9)
        symlink.symlink_to(_pytest_residue_symlink_target(0))
    if shape == "nonempty-directory":
        (directory / "payload").write_text("not empty", encoding="utf-8")
    elif shape == "world-writable-directory":
        directory.chmod(0o777)

    report = private_io.scan_private_lock_namespace(lock_root)

    assert sorted((issue.name, issue.reason) for issue in report.issues) == sorted([
        (directory.name, "unsupported-private-lock-residue"),
        *(
            []
            if shape == "unpaired-directory"
            else [
                (
                    _pytest_residue_lock_name(0, 9),
                    "unsupported-private-lock-residue",
                )
            ]
        ),
    ])


def test_private_lock_namespace_scan_rejects_unpaired_pytest_symlink_residue(
    tmp_path,
):
    """Would fail if a pytest-looking non-exact symlink was enough by itself."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    symlink = lock_root / _canonical_lock_name(20)
    symlink.symlink_to(_pytest_residue_symlink_target(0))

    report = private_io.scan_private_lock_namespace(lock_root)

    assert [(issue.name, issue.reason) for issue in report.issues] == [
        (symlink.name, "unsupported-private-lock-residue"),
    ]


def test_private_lock_namespace_scan_rejects_exact_unpaired_pytest_symlink_residue(
    tmp_path,
):
    """Would fail if an exact rejects9 symlink did not require its rejects10 pair."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    symlink = lock_root / _pytest_residue_lock_name(0, 9)
    symlink.symlink_to(_pytest_residue_symlink_target(0))

    report = private_io.scan_private_lock_namespace(lock_root)

    assert [(issue.name, issue.reason) for issue in report.issues] == [
        (symlink.name, "unsupported-private-lock-residue"),
    ]


def test_private_lock_namespace_scan_rejects_wrong_pytest_residue_pair(
    tmp_path,
):
    """Would fail if a symlink from one pytest run approved another run's directory."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    directory = lock_root / _pytest_residue_lock_name(1, 10)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    symlink = lock_root / _pytest_residue_lock_name(0, 9)
    symlink.symlink_to(_pytest_residue_symlink_target(0))

    report = private_io.scan_private_lock_namespace(lock_root)

    assert sorted((issue.name, issue.reason) for issue in report.issues) == sorted([
        (directory.name, "unsupported-private-lock-residue"),
        (symlink.name, "unsupported-private-lock-residue"),
    ])


def test_private_lock_namespace_scan_rejects_correct_name_pair_with_wrong_target_run(
    tmp_path,
):
    """Would fail if pair approval ignored the symlink target's pytest run."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    directory = lock_root / _pytest_residue_lock_name(0, 10)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    symlink = lock_root / _pytest_residue_lock_name(0, 9)
    symlink.symlink_to(_pytest_residue_symlink_target(1))

    report = private_io.scan_private_lock_namespace(lock_root)

    assert sorted((issue.name, issue.reason) for issue in report.issues) == sorted([
        (directory.name, "unsupported-private-lock-residue"),
        (symlink.name, "unsupported-private-lock-residue"),
    ])


def test_private_path_lock_no_create_rejects_foreign_namespace_residue(
    tmp_path,
    monkeypatch,
):
    """Would fail if lock acquisition validated only its own lock file."""
    target, lock_root, lock_path = _existing_private_lock(tmp_path, monkeypatch)
    residue = lock_root / f"{_canonical_lock_name(2)}.moved-nested"
    _touch_private_lock(residue)
    lock_before = _lock_metadata(lock_path)
    residue_before = _lock_metadata(residue)

    with pytest.raises(ValueError, match="private lock namespace"):
        with private_path_lock(target, label="profile lock", create=False):
            pass

    assert _lock_metadata(lock_path) == lock_before
    assert _lock_metadata(residue) == residue_before


def test_private_path_lock_create_rejects_foreign_namespace_residue_without_mutation(
    tmp_path,
    monkeypatch,
):
    """Would fail if create=True only validated the lock being acquired."""
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    residue = lock_root / f"{_canonical_lock_name(3)}.moved"
    _touch_private_lock(residue)
    residue_before = _lock_metadata(residue)
    expected_lock = lock_root / private_io._private_lock_name(target)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)

    with pytest.raises(ValueError, match="private lock namespace"):
        with private_path_lock(target, label="profile lock", create=True):
            pass

    assert _lock_metadata(residue) == residue_before
    assert not expected_lock.exists()


def test_private_path_lock_create_rejects_0755_contaminated_root_before_chmod(
    tmp_path,
    monkeypatch,
):
    """Would fail if create=True chmoded an existing unsafe lock root before scanning."""
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o755)
    lock_root.chmod(0o755)
    residue = lock_root / f"{_canonical_lock_name(5)}.moved"
    _touch_private_lock(residue)
    root_before = _lock_metadata(lock_root)
    residue_before = _lock_metadata(residue)
    expected_lock = lock_root / private_io._private_lock_name(target)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)

    with pytest.raises(ValueError):
        with private_path_lock(target, label="profile lock", create=True):
            pass

    assert _lock_metadata(lock_root) == root_before
    assert _lock_metadata(residue) == residue_before
    assert stat.S_IMODE(lock_root.stat().st_mode) == 0o755
    assert not expected_lock.exists()


def test_private_path_lock_create_rejects_0700_contaminated_root_without_hash_drift(
    tmp_path,
    monkeypatch,
):
    """Would fail if create=True touched a valid root before fail-closed scanning."""
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(6)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report_before = private_io.scan_private_lock_namespace(lock_root)
    approval_before = private_io.private_lock_namespace_approval_hash(report_before)
    root_before = _lock_metadata(lock_root)
    moved_before = _lock_metadata(moved)
    expected_lock = lock_root / private_io._private_lock_name(target)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)

    with pytest.raises(ValueError, match="private lock namespace"):
        with private_path_lock(target, label="profile lock", create=True):
            pass

    report_after = private_io.scan_private_lock_namespace(lock_root)
    assert report_after == report_before
    assert private_io.private_lock_namespace_approval_hash(report_after) == approval_before
    assert _lock_metadata(lock_root) == root_before
    assert _lock_metadata(moved) == moved_before
    assert not expected_lock.exists()


def test_private_path_lock_create_rejects_raced_foreign_canonical_lock_without_chmod(
    tmp_path,
    monkeypatch,
):
    """Would fail if a post-scan O_CREAT race chmoded an unknown lock inode."""
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    lock_name = private_io._private_lock_name(target)
    lock_path = lock_root / lock_name
    original_open = private_io.os.open
    injected = False
    foreign_before: tuple[int, ...] | None = None
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)

    def inject_foreign_lock_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal injected, foreign_before
        if (
            dir_fd is not None
            and path == lock_name
            and flags & private_io.os.O_CREAT
            and not injected
        ):
            injected = True
            fd = original_open(
                path,
                private_io.os.O_WRONLY
                | private_io.os.O_CREAT
                | private_io.os.O_EXCL
                | getattr(private_io.os, "O_NOFOLLOW", 0),
                0o644,
                dir_fd=dir_fd,
            )
            try:
                os.write(fd, b"x" * 5000)
            finally:
                os.close(fd)
            os.chmod(path, 0o644, dir_fd=dir_fd)
            foreign = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
            foreign_before = (
                foreign.st_dev,
                foreign.st_ino,
                foreign.st_mode,
                foreign.st_uid,
                foreign.st_gid,
                foreign.st_nlink,
                foreign.st_size,
                foreign.st_mtime_ns,
                foreign.st_ctime_ns,
            )
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(private_io.os, "open", inject_foreign_lock_before_open)

    with pytest.raises(ValueError):
        with private_path_lock(target, label="profile lock", create=True):
            pass

    assert injected
    assert foreign_before is not None
    assert _lock_metadata(lock_path) == foreign_before
    assert stat.S_IMODE(lock_path.lstat().st_mode) == 0o644
    assert lock_path.read_bytes() == b"x" * 5000


@pytest.mark.parametrize("root_mode", (0o700, 0o755))
def test_private_path_lock_create_restarts_readonly_after_raced_root_create(
    tmp_path,
    monkeypatch,
    root_mode,
):
    """Would fail if an ENOENT race chmoded a newly appeared contaminated root."""
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()
    lock_root = tmp_path / "lock-root"
    canonical = lock_root / _canonical_lock_name(10)
    moved = lock_root / f"{canonical.name}.moved"
    report_before: private_io.PrivateLockNamespaceReport | None = None
    approval_before: str | None = None
    original_open = private_io._open_existing_private_lock_root
    original_chmod = private_io._chmod_private_directory
    raced = False
    chmod_targets: list[Path] = []

    def create_contaminated_root_then_report_missing(root: Path, **kwargs):
        nonlocal raced, report_before, approval_before
        if root == lock_root and not raced:
            raced = True
            lock_root.mkdir(mode=root_mode)
            lock_root.chmod(root_mode)
            _touch_private_lock(canonical)
            _touch_private_lock(moved)
            if root_mode == 0o700:
                root_fd = os.open(lock_root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    report_before = private_io._scan_private_lock_namespace_fd(
                        lock_root,
                        root_fd,
                    )
                    approval_before = private_io.private_lock_namespace_approval_hash(
                        report_before
                    )
                finally:
                    os.close(root_fd)
            raise FileNotFoundError(root)
        return original_open(root, **kwargs)

    def observe_chmod(path: Path, *, label: str) -> None:
        if path == lock_root:
            chmod_targets.append(path)
        original_chmod(path, label=label)

    monkeypatch.setattr(
        private_io,
        "_open_existing_private_lock_root",
        create_contaminated_root_then_report_missing,
    )
    monkeypatch.setattr(private_io, "_chmod_private_directory", observe_chmod)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)

    with pytest.raises(ValueError, match="private lock namespace"):
        with private_path_lock(target, label="profile lock", create=True):
            pass

    assert raced
    assert chmod_targets == []
    assert stat.S_IMODE(lock_root.lstat().st_mode) == root_mode
    assert moved.exists()
    if root_mode == 0o700:
        assert report_before is not None
        report_after = private_io.scan_private_lock_namespace(lock_root)
        assert report_after == report_before
        assert private_io.private_lock_namespace_approval_hash(report_after) == (
            approval_before
        )
    assert not (lock_root / private_io._private_lock_name(target)).exists()


def test_private_path_lock_create_does_not_follow_parent_symlink_swap_before_mkdir(
    tmp_path,
    monkeypatch,
):
    """Would fail if root creation used a validated parent path after it was rebound."""
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()
    lock_parent = tmp_path / "state"
    lock_parent.mkdir(mode=0o700)
    lock_parent.chmod(0o700)
    lock_root = lock_parent / "locks"
    hidden_parent = tmp_path / "state-hidden"
    foreign_parent = tmp_path / "foreign"
    foreign_parent.mkdir(mode=0o700)
    foreign_parent.chmod(0o700)
    original_mkdir = private_io.os.mkdir
    swapped = False

    def swap_parent_before_root_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        is_root_create = (
            (dir_fd is None and Path(path) == lock_root)
            or (dir_fd is not None and path == lock_root.name)
            or (
                dir_fd is not None
                and isinstance(path, str)
                and path.startswith(f".{lock_root.name}.create-")
            )
        )
        if is_root_create and not swapped:
            swapped = True
            lock_parent.rename(hidden_parent)
            lock_parent.symlink_to(foreign_parent, target_is_directory=True)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(private_io.os, "mkdir", swap_parent_before_root_mkdir)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)

    with pytest.raises(ValueError, match="private lock namespace"):
        with private_path_lock(target, label="profile lock", create=True):
            pass

    assert swapped
    assert not (foreign_parent / "locks").exists()
    assert not (foreign_parent / "locks" / private_io._private_lock_name(target)).exists()


def test_private_path_lock_create_rejects_post_mkdir_rebind_before_lock_file_create(
    tmp_path,
    monkeypatch,
):
    """Would fail if a replacement root at the same path was accepted after mkdir."""
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()
    lock_parent = tmp_path / "state"
    lock_parent.mkdir(mode=0o700)
    lock_parent.chmod(0o700)
    lock_root = lock_parent / "locks"
    created_root = tmp_path / "locks-created"
    original_mkdir = private_io.os.mkdir
    original_rename_no_replace = private_io._rename_private_lock_residue_no_replace
    rebound = False

    def rebind_root_after_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal rebound
        result = original_mkdir(path, mode, dir_fd=dir_fd)
        is_root_create = (
            dir_fd is None and Path(path) == lock_root
        )
        if is_root_create and not rebound:
            rebound = True
            lock_root.rename(created_root)
            original_mkdir(lock_root, 0o700)
        return result

    def rebind_root_after_publish_rename(**kwargs):
        nonlocal rebound
        original_rename_no_replace(**kwargs)
        if kwargs["destination_name"] == lock_root.name and not rebound:
            rebound = True
            lock_root.rename(created_root)
            original_mkdir(lock_root, 0o700)

    monkeypatch.setattr(private_io.os, "mkdir", rebind_root_after_mkdir)
    monkeypatch.setattr(
        private_io,
        "_rename_private_lock_residue_no_replace",
        rebind_root_after_publish_rename,
    )
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)

    with pytest.raises(ValueError, match="private lock namespace"):
        with private_path_lock(target, label="profile lock", create=True):
            pass

    assert rebound
    assert created_root.is_dir()
    assert lock_root.is_dir()
    assert not (lock_root / private_io._private_lock_name(target)).exists()


def test_private_path_lock_reentrant_create_revalidates_foreign_namespace(
    tmp_path,
    monkeypatch,
):
    """Would fail if a same-thread nested lock skipped the namespace scan."""
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)
    entered_nested = False

    with private_path_lock(target, label="outer profile lock", create=True):
        residue = lock_root / f"{_canonical_lock_name(4)}.moved-nested"
        _touch_private_lock(residue)
        residue_before = _lock_metadata(residue)
        with pytest.raises(ValueError, match="private lock namespace"):
            with private_path_lock(target, label="nested profile lock", create=True):
                entered_nested = True
        assert _lock_metadata(residue) == residue_before

    assert not entered_nested


def test_private_path_lock_reentrant_create_rejects_residue_without_hash_drift(
    tmp_path,
    monkeypatch,
):
    """Would fail if a nested create=True lock touched the namespace before scanning."""
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)

    with private_path_lock(target, label="outer profile lock", create=True):
        canonical = lock_root / _canonical_lock_name(7)
        moved = lock_root / f"{canonical.name}.moved-nested"
        _touch_private_lock(canonical)
        _touch_private_lock(moved)
        report_before = private_io.scan_private_lock_namespace(lock_root)
        approval_before = private_io.private_lock_namespace_approval_hash(report_before)
        root_before = _lock_metadata(lock_root)
        moved_before = _lock_metadata(moved)
        with pytest.raises(ValueError, match="private lock namespace"):
            with private_path_lock(target, label="nested profile lock", create=True):
                pass
        report_after = private_io.scan_private_lock_namespace(lock_root)

    assert report_after == report_before
    assert private_io.private_lock_namespace_approval_hash(report_after) == approval_before
    assert _lock_metadata(lock_root) == root_before
    assert _lock_metadata(moved) == moved_before


def test_private_path_lock_reentrant_create_rejects_detached_held_lock_root(
    tmp_path,
    monkeypatch,
):
    """Would fail if a nested lock accepted a held fd after canonical root rebind."""
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    detached_root = tmp_path / "lock-root-detached"
    lock_name = private_io._private_lock_name(target)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)
    entered_nested = False

    with private_path_lock(target, label="outer profile lock", create=True):
        held_lock = lock_root / lock_name
        held_identity = _lock_metadata(held_lock)
        lock_root.rename(detached_root)
        lock_root.mkdir(mode=0o700)
        lock_root.chmod(0o700)
        with pytest.raises(ValueError, match=r"changed while locking|namespace changed"):
            with private_path_lock(target, label="nested profile lock", create=True):
                entered_nested = True

        assert _lock_metadata(detached_root / lock_name) == held_identity
        assert not (lock_root / lock_name).exists()

    assert not entered_nested


def test_private_path_lock_reentrant_create_attempts_all_validation_closes(
    tmp_path,
    monkeypatch,
):
    """Would fail if a nested create=True validation close leaked the root fd."""
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)
    real_open_root = private_io._open_existing_private_lock_root
    real_open_lock = private_io._open_existing_private_lock_file
    real_close = private_io.os.close
    armed = False
    validation_fds: set[int] = set()
    close_attempts: list[int] = []

    def record_root_fd(*args, **kwargs):
        fd, identities = real_open_root(*args, **kwargs)
        if armed:
            validation_fds.add(fd)
        return fd, identities

    def record_lock_fd(*args, **kwargs):
        fd, identity = real_open_lock(*args, **kwargs)
        if armed:
            validation_fds.add(fd)
        return fd, identity

    def close_validation_fd_then_fail(fd: int) -> None:
        if armed and fd in validation_fds:
            close_attempts.append(fd)
            real_close(fd)
            raise OSError(f"synthetic reentrant validation close {len(close_attempts)}")
        real_close(fd)

    monkeypatch.setattr(private_io, "_open_existing_private_lock_root", record_root_fd)
    monkeypatch.setattr(private_io, "_open_existing_private_lock_file", record_lock_fd)
    monkeypatch.setattr(private_io.os, "close", close_validation_fd_then_fail)

    try:
        with private_path_lock(target, label="outer profile lock", create=True):
            armed = True
            with pytest.raises(ExceptionGroup) as exc_info:
                with private_path_lock(target, label="nested profile lock", create=True):
                    pass
            armed = False
    finally:
        monkeypatch.setattr(private_io.os, "close", real_close)
        for fd in validation_fds:
            try:
                real_close(fd)
            except OSError:
                pass

    flattened = _flatten_exception_group(exc_info.value)
    assert len(close_attempts) == 2
    assert sorted(str(error) for error in flattened) == [
        "synthetic reentrant validation close 1",
        "synthetic reentrant validation close 2",
    ]


def test_private_path_lock_create_rejects_root_swap_after_flock_before_yield(
    tmp_path,
    monkeypatch,
):
    """Would fail if lock identity was not rebound immediately before yielding."""
    target = tmp_path / "profile" / "profile.json"
    target.parent.mkdir()
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    detached_root = tmp_path / "lock-root-detached"
    real_flock = private_io.fcntl.flock
    swapped = False
    entered = False
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)

    def swap_root_after_exclusive_acquire(fd: int, operation: int) -> None:
        nonlocal swapped
        result = real_flock(fd, operation)
        if (
            operation & private_io.fcntl.LOCK_EX
            and operation & private_io.fcntl.LOCK_NB
            and not swapped
        ):
            swapped = True
            lock_root.rename(detached_root)
            lock_root.mkdir(mode=0o700)
            lock_root.chmod(0o700)
        return result

    monkeypatch.setattr(private_io.fcntl, "flock", swap_root_after_exclusive_acquire)

    with pytest.raises(ValueError, match=r"changed while locking|namespace changed"):
        with private_path_lock(target, label="profile lock", create=True):
            entered = True

    assert swapped
    assert not entered
    assert detached_root.is_dir()
    assert lock_root.is_dir()
    assert not (lock_root / private_io._private_lock_name(target)).exists()


def test_private_lock_reconcile_requires_exact_approval_hash(tmp_path):
    """Would fail if a stale or generic operator approval could move locks."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)

    with pytest.raises(ValueError, match="approval hash"):
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash="0" * 64,
        )

    assert moved.exists()
    assert not quarantine_root.exists()


def test_private_lock_reconcile_binds_approval_to_quarantine_root(tmp_path):
    """Would fail if an approval for one quarantine directory could move to another."""
    lock_root = tmp_path / "lock-root"
    approved_quarantine = tmp_path / "approved-quarantine"
    other_quarantine = tmp_path / "other-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=approved_quarantine,
    )

    with pytest.raises(ValueError, match="approval hash"):
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=other_quarantine,
            approval_hash=approval,
        )

    assert moved.exists()
    assert not approved_quarantine.exists()
    assert not other_quarantine.exists()


def test_private_lock_reconcile_rejects_snapshot_swap_before_rename(tmp_path):
    """Would fail if approval was bound only to names instead of inode evidence."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = _manual_private_lock_reconcile_report(lock_root)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )
    moved.unlink()
    _touch_private_lock(moved)

    with pytest.raises(ValueError, match="changed before quarantine"):
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    assert moved.exists()
    assert not quarantine_root.exists()


def test_private_lock_reconcile_quarantines_only_approved_no_follow_residues(
    tmp_path,
):
    """Would fail if reconcile followed symlinks or deleted evidence in place."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    moved_names = (
        f"{_canonical_lock_name(1)}.moved",
        f"{_canonical_lock_name(2)}.moved-nested",
    )
    for index, moved_name in enumerate(moved_names, start=1):
        _touch_private_lock(lock_root / _canonical_lock_name(index))
        _touch_private_lock(lock_root / moved_name)
    for index in range(4):
        directory = lock_root / _pytest_residue_lock_name(index, 10)
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    symlink_targets = []
    for index in range(4):
        target = _pytest_residue_symlink_target(index)
        symlink_targets.append(str(target))
        (lock_root / _pytest_residue_lock_name(index, 9)).symlink_to(target)
    report = _manual_private_lock_reconcile_report(lock_root)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )

    result = private_io.quarantine_private_lock_residues(
        report,
        quarantine_root=quarantine_root,
        approval_hash=approval,
    )

    assert len(result.quarantined) == 10
    assert sorted(entry.original_name for entry in result.quarantined) == sorted(
        issue.name for issue in report.issues
    )
    assert all(not (lock_root / issue.name).exists() for issue in report.issues)
    assert all((lock_root / _canonical_lock_name(index)).is_file() for index in (1, 2))
    quarantined_symlinks = [
        entry for entry in result.quarantined if entry.file_type == "symlink"
    ]
    assert sorted(
        os.readlink(quarantine_root / entry.quarantine_name)
        for entry in quarantined_symlinks
    ) == sorted(symlink_targets)
    assert all(
        (quarantine_root / entry.quarantine_name).is_dir()
        for entry in result.quarantined
        if entry.file_type == "directory"
    )


def test_private_lock_namespace_scan_requires_operator_approval_for_historical_pytest_residues(
    tmp_path,
):
    """Would fail if host-specific pytest runs in production auto-approved residues."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    directory = lock_root / _pytest_residue_lock_name(0, 10)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    symlink = lock_root / _pytest_residue_lock_name(0, 9)
    symlink.symlink_to(_pytest_residue_symlink_target(0))

    report = private_io.scan_private_lock_namespace(lock_root)

    assert sorted(issue.reason for issue in report.issues) == [
        "unsupported-private-lock-residue",
        "unsupported-private-lock-residue",
    ]


def test_private_lock_namespace_scan_binds_operator_approval_to_exact_residue_snapshot(
    tmp_path,
):
    """Would fail if reconcile eligibility came from built-in host/test constants."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    directory = lock_root / _pytest_residue_lock_name(0, 10)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    symlink = lock_root / _pytest_residue_lock_name(0, 9)
    symlink.symlink_to(_pytest_residue_symlink_target(0))
    raw_report = private_io.scan_private_lock_namespace(lock_root)
    approvals = tuple(
        private_io.PrivateLockResidueApproval(
            name=issue.name,
            reason=(
                "operator-approved-empty-directory-lock"
                if issue.snapshot.file_type == "directory"
                else "operator-approved-dangling-symlink-lock"
            ),
            snapshot=issue.snapshot,
        )
        for issue in raw_report.issues
    )

    approved_report = private_io.scan_private_lock_namespace(
        lock_root,
        approved_residues=approvals,
    )

    assert sorted(issue.reason for issue in approved_report.issues) == [
        "operator-approved-dangling-symlink-lock",
        "operator-approved-empty-directory-lock",
    ]
    assert tuple(issue.snapshot for issue in approved_report.issues) == tuple(
        issue.snapshot for issue in raw_report.issues
    )


def test_private_lock_namespace_scan_rejects_stale_operator_residue_approval(
    tmp_path,
):
    """Would fail if a copied operator approval could approve a changed inode."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    directory = lock_root / _pytest_residue_lock_name(0, 10)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    raw_report = private_io.scan_private_lock_namespace(lock_root)
    issue = raw_report.issues[0]
    approval = private_io.PrivateLockResidueApproval(
        name=issue.name,
        reason="operator-approved-empty-directory-lock",
        snapshot=dataclasses.replace(issue.snapshot, inode=issue.snapshot.inode + 1),
    )

    with pytest.raises(ValueError, match="residue approval"):
        private_io.scan_private_lock_namespace(
            lock_root,
            approved_residues=(approval,),
        )


def test_private_lock_namespace_scan_rejects_forged_operator_approval_type(
    tmp_path,
):
    """Would fail if approval duck-typing could forge operator intent."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    directory = lock_root / _pytest_residue_lock_name(0, 10)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    issue = private_io.scan_private_lock_namespace(lock_root).issues[0]
    forged = SimpleNamespace(
        name=issue.name,
        reason="operator-approved-empty-directory-lock",
        snapshot=issue.snapshot,
    )

    with pytest.raises(ValueError, match="residue approval is invalid"):
        private_io.scan_private_lock_namespace(
            lock_root,
            approved_residues=(forged,),
        )


def test_private_lock_namespace_scan_rejects_operator_approval_wrong_name(
    tmp_path,
):
    """Would fail if a valid-looking approval could float to another residue."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    directory = lock_root / _pytest_residue_lock_name(0, 10)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    issue = private_io.scan_private_lock_namespace(lock_root).issues[0]
    approval = private_io.PrivateLockResidueApproval(
        name=_canonical_lock_name(63),
        reason="operator-approved-empty-directory-lock",
        snapshot=issue.snapshot,
    )

    with pytest.raises(ValueError, match="did not match current namespace"):
        private_io.scan_private_lock_namespace(
            lock_root,
            approved_residues=(approval,),
        )


def test_private_lock_namespace_scan_rejects_operator_approval_wrong_reason(
    tmp_path,
):
    """Would fail if a reason string were not bound to the residue file type."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    directory = lock_root / _pytest_residue_lock_name(0, 10)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    issue = private_io.scan_private_lock_namespace(lock_root).issues[0]
    approval = private_io.PrivateLockResidueApproval(
        name=issue.name,
        reason="operator-approved-dangling-symlink-lock",
        snapshot=issue.snapshot,
    )

    with pytest.raises(ValueError, match="does not match residue type"):
        private_io.scan_private_lock_namespace(
            lock_root,
            approved_residues=(approval,),
        )


def test_private_lock_namespace_scan_rejects_duplicate_operator_approval(
    tmp_path,
):
    """Would fail if duplicate approvals could blur the reviewed snapshot set."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    directory = lock_root / _pytest_residue_lock_name(0, 10)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    issue = private_io.scan_private_lock_namespace(lock_root).issues[0]
    approval = private_io.PrivateLockResidueApproval(
        name=issue.name,
        reason="operator-approved-empty-directory-lock",
        snapshot=issue.snapshot,
    )

    with pytest.raises(ValueError, match="duplicated"):
        private_io.scan_private_lock_namespace(
            lock_root,
            approved_residues=(approval, approval),
        )


def test_private_lock_reconcile_rejects_partial_operator_approval_before_mutation(
    tmp_path,
):
    """Would fail if a partial operator set could move one residue first."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    directory = lock_root / _pytest_residue_lock_name(0, 10)
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    symlink = lock_root / _pytest_residue_lock_name(0, 9)
    symlink.symlink_to(_pytest_residue_symlink_target(0))
    raw_report = private_io.scan_private_lock_namespace(lock_root)
    directory_issue = next(
        issue for issue in raw_report.issues if issue.snapshot.file_type == "directory"
    )
    partial_report = private_io.scan_private_lock_namespace(
        lock_root,
        approved_residues=(
            private_io.PrivateLockResidueApproval(
                name=directory_issue.name,
                reason="operator-approved-empty-directory-lock",
                snapshot=directory_issue.snapshot,
            ),
        ),
    )
    approval = private_io.private_lock_namespace_approval_hash(
        partial_report,
        quarantine_root=quarantine_root,
    )

    with pytest.raises(ValueError, match="not approved for quarantine"):
        private_io.quarantine_private_lock_residues(
            partial_report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    assert directory.is_dir()
    assert os.path.lexists(symlink)
    assert not quarantine_root.exists()


def test_private_lock_reconcile_reports_partial_on_baseexception_after_rename(
    tmp_path,
    monkeypatch,
):
    """Would fail if BaseException after a move escaped without partial evidence."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )
    quarantine_name = private_io._safe_quarantine_name(report.issues[0])

    def cancel_after_rename(_original_name: str, _quarantine_name: str) -> None:
        raise _SyntheticPrivateLockCleanupCancellation("synthetic reconcile cancellation")

    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_commit",
        cancel_after_rename,
    )

    with pytest.raises(private_io.PrivateLockPartialCommitError) as exc_info:
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    assert isinstance(
        exc_info.value.primary_error,
        _SyntheticPrivateLockCleanupCancellation,
    )
    assert [entry.original_name for entry in exc_info.value.committed] == [moved.name]
    assert (quarantine_root / quarantine_name).exists()


def _snapshot_quarantined_entry(path: Path) -> private_io.PrivateLockSnapshot:
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        return private_io._private_lock_snapshot_at(parent_fd, path.name)
    finally:
        os.close(parent_fd)


def test_private_lock_reconcile_success_reports_post_rename_snapshot(tmp_path):
    """Would fail if success evidence reused the pre-rename lock snapshot."""
    lock_root = tmp_path / "state/locks"
    quarantine_root = tmp_path / "state/lock-quarantine"
    lock_root.mkdir(mode=0o700, parents=True)
    lock_root.parent.chmod(0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    issue = report.issues[0]
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )

    result = private_io.quarantine_private_lock_residues(
        report,
        quarantine_root=quarantine_root,
        approval_hash=approval,
    )

    entry = result.quarantined[0]
    post_snapshot = _snapshot_quarantined_entry(quarantine_root / entry.quarantine_name)
    assert entry.snapshot == post_snapshot
    assert entry.snapshot.ctime_ns == post_snapshot.ctime_ns
    assert entry.snapshot != issue.snapshot


def test_private_lock_reconcile_partial_reports_post_rename_snapshot(
    tmp_path,
    monkeypatch,
):
    """Would fail if partial evidence reported stale pre-rename metadata."""
    lock_root = tmp_path / "state/locks"
    quarantine_root = tmp_path / "state/lock-quarantine"
    lock_root.mkdir(mode=0o700, parents=True)
    lock_root.parent.chmod(0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    issue = report.issues[0]
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )

    def fail_after_rename(_original_name: str, _quarantine_name: str) -> None:
        raise OSError("synthetic post-rename failure")

    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_commit",
        fail_after_rename,
    )

    with pytest.raises(private_io.PrivateLockPartialCommitError) as exc_info:
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    entry = exc_info.value.committed[0]
    post_snapshot = _snapshot_quarantined_entry(quarantine_root / entry.quarantine_name)
    assert entry.snapshot == post_snapshot
    assert entry.snapshot.ctime_ns == post_snapshot.ctime_ns
    assert entry.snapshot != issue.snapshot


def test_private_lock_reconcile_rejects_lockroot_ancestor_rebind_before_return(
    tmp_path,
    monkeypatch,
):
    """Would fail if success trusted held fds after the named lockroot moved."""
    state = tmp_path / "state"
    detached_state = tmp_path / "state-detached"
    lock_root = state / "locks"
    quarantine_root = state / "lock-quarantine"
    lock_root.mkdir(mode=0o700, parents=True)
    state.chmod(0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )

    def rebind_state_before_return(_root_fd, _quarantine_fd, _entries) -> None:
        state.rename(detached_state)
        (state / "locks").mkdir(mode=0o700, parents=True)
        (state / "locks").chmod(0o700)
        (state / "lock-quarantine").mkdir(mode=0o700)
        (state / "lock-quarantine").chmod(0o700)

    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_return",
        rebind_state_before_return,
    )

    with pytest.raises(private_io.PrivateLockPartialCommitError):
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    assert detached_state.is_dir()


def test_private_lock_reconcile_partial_does_not_bind_rebound_quarantine_ancestor(
    tmp_path,
    monkeypatch,
):
    """Would fail if partial evidence ignored a renamed quarantine ancestor."""
    outer = tmp_path / "outer"
    detached_outer = tmp_path / "outer-detached"
    state = outer / "state"
    lock_root = state / "locks"
    quarantine_root = state / "lock-quarantine"
    lock_root.mkdir(mode=0o700, parents=True)
    state.chmod(0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )

    def rebind_quarantine_ancestor(
        _original_name: str,
        _quarantine_name: str,
    ) -> None:
        outer.rename(detached_outer)
        replacement_lock_root = outer / "state" / "locks"
        replacement_lock_root.mkdir(mode=0o700, parents=True)
        (outer / "state").chmod(0o700)
        replacement_lock_root.chmod(0o700)
        (outer / "state" / "lock-quarantine").mkdir(mode=0o700)
        (outer / "state" / "lock-quarantine").chmod(0o700)
        raise OSError("synthetic quarantine ancestor rebind")

    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_commit",
        rebind_quarantine_ancestor,
    )

    with pytest.raises(private_io.PrivateLockPartialCommitError) as exc_info:
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    quarantine_name = private_io._safe_quarantine_name(report.issues[0])
    assert os.path.lexists(detached_outer / "state" / "lock-quarantine" / quarantine_name)
    assert exc_info.value.committed == ()
    assert exc_info.value.quarantine_root_snapshot is None
    assert exc_info.value.unmoved == ()
    assert exc_info.value.unverified == report.issues


def test_private_lock_reconcile_rejects_quarantine_root_symlink_before_return(
    tmp_path,
    monkeypatch,
):
    """Would fail if final return did not revalidate the named quarantine path."""
    state = tmp_path / "state"
    lock_root = state / "locks"
    quarantine_root = state / "lock-quarantine"
    detached_quarantine = state / "lock-quarantine-detached"
    foreign_quarantine = state / "foreign-quarantine"
    lock_root.mkdir(mode=0o700, parents=True)
    state.chmod(0o700)
    lock_root.chmod(0o700)
    foreign_quarantine.mkdir(mode=0o700)
    foreign_quarantine.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )

    def swap_quarantine_to_symlink_before_return(
        _root_fd,
        _quarantine_fd,
        _entries,
    ) -> None:
        quarantine_root.rename(detached_quarantine)
        quarantine_root.symlink_to(foreign_quarantine, target_is_directory=True)

    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_return",
        swap_quarantine_to_symlink_before_return,
    )

    with pytest.raises(private_io.PrivateLockPartialCommitError):
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    assert quarantine_root.is_symlink()
    assert detached_quarantine.is_dir()


def test_private_lock_reconcile_keeps_quarantine_ancestor_fd_live_until_return(
    tmp_path,
    monkeypatch,
):
    """Would fail if quarantine ancestors were only snapshotted and closed."""
    outer = tmp_path / "outer"
    state = outer / "state"
    lock_root = state / "locks"
    quarantine_root = state / "lock-quarantine"
    lock_root.mkdir(mode=0o700, parents=True)
    outer.chmod(0o700)
    state.chmod(0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )
    outer_item = outer.stat()
    outer_identity = (outer_item.st_dev, outer_item.st_ino)
    watched_outer_fds: set[int] = set()
    closed_outer_fds: set[int] = set()
    real_open = private_io.os.open
    real_close = private_io.os.close

    def tracking_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        try:
            item = os.fstat(fd)
        except OSError:
            return fd
        if (item.st_dev, item.st_ino) == outer_identity:
            watched_outer_fds.add(fd)
        return fd

    def tracking_close(fd: int) -> None:
        if fd in watched_outer_fds:
            closed_outer_fds.add(fd)
        real_close(fd)

    def assert_outer_fd_live_before_return(_root_fd, _quarantine_fd, _entries) -> None:
        assert watched_outer_fds - closed_outer_fds, (
            "private lock quarantine ancestor fd was closed before return"
        )

    monkeypatch.setattr(private_io.os, "open", tracking_open)
    monkeypatch.setattr(private_io.os, "close", tracking_close)
    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_return",
        assert_outer_fd_live_before_return,
    )

    result = private_io.quarantine_private_lock_residues(
        report,
        quarantine_root=quarantine_root,
        approval_hash=approval,
    )

    assert len(result.quarantined) == 1


def test_private_lock_reconcile_reports_partial_commit_when_commit_hook_fails(
    tmp_path,
    monkeypatch,
):
    """Would fail if a post-rename failure claimed atomic rollback."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    quarantine_name = private_io._safe_quarantine_name(report.issues[0])
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )

    def fail_after_rename(_original_name: str, _quarantine_name: str) -> None:
        raise OSError("synthetic quarantine fsync failure")

    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_commit",
        fail_after_rename,
    )

    with pytest.raises(private_io.PrivateLockPartialCommitError) as exc_info:
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    error = exc_info.value
    assert isinstance(error.primary_error, OSError)
    assert "synthetic quarantine fsync failure" in str(error.primary_error)
    assert [entry.original_name for entry in error.committed] == [report.issues[0].name]
    assert error.unmoved == ()
    assert not moved.exists()
    assert (quarantine_root / quarantine_name).exists()


def test_private_lock_reconcile_does_not_overwrite_raced_quarantine_evidence(
    tmp_path,
    monkeypatch,
):
    """Would fail if check-then-rename overwrote an existing quarantine target."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    issue = report.issues[0]
    raced_quarantine_name = private_io._safe_quarantine_name(issue)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )

    def create_raced_destination(_original_name: str, quarantine_name: str) -> None:
        assert quarantine_name == raced_quarantine_name
        _touch_private_lock(quarantine_root / quarantine_name)

    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_rename",
        create_raced_destination,
        raising=False,
    )

    with pytest.raises(ValueError, match="quarantine target already exists"):
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    assert moved.exists()
    assert (quarantine_root / raced_quarantine_name).is_file()


def test_private_lock_reconcile_keeps_quarantine_when_lockroot_restore_would_fail(
    tmp_path,
    monkeypatch,
):
    """Would fail if rollback tried to restore after a committed forward move."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    quarantine_name = private_io._safe_quarantine_name(report.issues[0])
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )

    def fail_after_rename(_original_name: str, _quarantine_name: str) -> None:
        lock_root.chmod(0o500)
        raise OSError("synthetic commit failure")

    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_commit",
        fail_after_rename,
    )

    try:
        with pytest.raises(private_io.PrivateLockPartialCommitError) as exc_info:
            private_io.quarantine_private_lock_residues(
                report,
                quarantine_root=quarantine_root,
                approval_hash=approval,
            )
    finally:
        lock_root.chmod(0o700)

    assert not moved.exists()
    assert (quarantine_root / quarantine_name).exists()
    assert [entry.original_name for entry in exc_info.value.committed] == [
        report.issues[0].name
    ]


def test_private_lock_reconcile_reports_partial_commit_with_raced_original_preserved(
    tmp_path,
    monkeypatch,
):
    """Would fail if partial commit overwrote a raced original-name inode."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    issue = report.issues[0]
    quarantine_name = private_io._safe_quarantine_name(issue)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )
    raced_original: dict[str, tuple[int, ...]] = {}

    def reoccupy_original_then_fail(original_name: str, _quarantine_name: str) -> None:
        replacement = lock_root / original_name
        _touch_private_lock(replacement)
        raced_original["metadata"] = _lock_metadata(replacement)
        raise OSError("synthetic commit failure")

    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_commit",
        reoccupy_original_then_fail,
    )

    with pytest.raises(private_io.PrivateLockPartialCommitError) as exc_info:
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    assert _lock_metadata(moved) == raced_original["metadata"]
    assert (quarantine_root / quarantine_name).exists()
    assert [entry.original_name for entry in exc_info.value.committed] == [issue.name]
    assert exc_info.value.unmoved == ()
    assert exc_info.value.unverified == ()


def test_private_lock_reconcile_reports_partial_commit_without_restore_source_rebind(
    tmp_path,
    monkeypatch,
):
    """Would fail if a quarantine-name rebind triggered a source restore attempt."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    issue = report.issues[0]
    quarantine_name = private_io._safe_quarantine_name(issue)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )
    approved_copy = quarantine_root / "approved-evidence-kept"
    foreign_metadata: tuple[int, ...] | None = None

    def swap_quarantine_source_then_fail(
        _original_name: str,
        current_quarantine_name: str,
    ) -> None:
        nonlocal foreign_metadata
        assert current_quarantine_name == quarantine_name
        (quarantine_root / current_quarantine_name).rename(approved_copy)
        _touch_private_lock(quarantine_root / current_quarantine_name)
        foreign_metadata = _lock_metadata(quarantine_root / current_quarantine_name)
        raise OSError("synthetic commit failure")

    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_commit",
        swap_quarantine_source_then_fail,
    )

    with pytest.raises(private_io.PrivateLockPartialCommitError) as exc_info:
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    assert foreign_metadata is not None
    assert not moved.exists()
    assert _lock_metadata(quarantine_root / quarantine_name) == foreign_metadata
    approved_after = approved_copy.lstat()
    assert (
        approved_after.st_dev,
        approved_after.st_ino,
        approved_after.st_mode,
        approved_after.st_uid,
        approved_after.st_gid,
        approved_after.st_nlink,
        approved_after.st_size,
        approved_after.st_mtime_ns,
    ) == (
        issue.snapshot.device,
        issue.snapshot.inode,
        issue.snapshot.mode,
        issue.snapshot.uid,
        issue.snapshot.gid,
        issue.snapshot.nlink,
        issue.snapshot.size,
        issue.snapshot.mtime_ns,
    )
    assert exc_info.value.committed == ()
    assert exc_info.value.unmoved == ()
    assert exc_info.value.unverified == (issue,)


def test_private_lock_reconcile_rejects_quarantine_rebind_after_last_snapshot(
    tmp_path,
    monkeypatch,
):
    """Would fail if a post-validation quarantine rebind could report success."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    issue = report.issues[0]
    quarantine_name = private_io._safe_quarantine_name(issue)
    approved_copy = quarantine_root / "approved-evidence-kept"
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )
    foreign_metadata: tuple[int, ...] | None = None

    def swap_quarantine_name_after_last_snapshot(
        _original_name: str,
        current_quarantine_name: str,
    ) -> None:
        nonlocal foreign_metadata
        assert current_quarantine_name == quarantine_name
        (quarantine_root / current_quarantine_name).rename(approved_copy)
        _touch_private_lock(quarantine_root / current_quarantine_name)
        foreign_metadata = _lock_metadata(quarantine_root / current_quarantine_name)

    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_commit",
        swap_quarantine_name_after_last_snapshot,
    )

    with pytest.raises(private_io.PrivateLockPartialCommitError) as exc_info:
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    assert foreign_metadata is not None
    assert not moved.exists()
    assert _lock_metadata(quarantine_root / quarantine_name) == foreign_metadata
    approved_after = approved_copy.lstat()
    assert approved_after.st_dev == issue.snapshot.device
    assert approved_after.st_ino == issue.snapshot.inode
    assert exc_info.value.committed == ()
    assert exc_info.value.unmoved == ()
    assert exc_info.value.unverified == (issue,)


def test_private_lock_reconcile_success_result_binds_root_identities(tmp_path):
    """Would fail if a success result omitted the identities it validated."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )

    result = private_io.quarantine_private_lock_residues(
        report,
        quarantine_root=quarantine_root,
        approval_hash=approval,
    )

    assert result.lock_root_snapshot == (
        private_io.scan_private_lock_namespace(lock_root).root_snapshot
    )
    assert result.quarantine_root_snapshot.file_type == "directory"
    assert result.quarantine_root_snapshot.uid == os.geteuid()
    assert stat.S_IMODE(result.quarantine_root_snapshot.mode) == 0o700


@pytest.mark.parametrize(
    "residue_kind",
    ("moved-file", "pytest-directory", "pytest-symlink"),
)
def test_private_lock_reconcile_rechecks_after_last_entry_validation_before_return(
    tmp_path,
    monkeypatch,
    residue_kind,
):
    """Would fail if the final success return did not rebind quarantine evidence."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    if residue_kind == "moved-file":
        canonical = lock_root / _canonical_lock_name(1)
        _touch_private_lock(canonical)
        _touch_private_lock(lock_root / f"{canonical.name}.moved")
        target_file_type = "regular"
    else:
        directory = lock_root / _pytest_residue_lock_name(0, 10)
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        symlink = lock_root / _pytest_residue_lock_name(0, 9)
        symlink.symlink_to(_pytest_residue_symlink_target(0))
        target_file_type = (
            "directory" if residue_kind == "pytest-directory" else "symlink"
        )
    report = _manual_private_lock_reconcile_report(lock_root)
    target_issue = next(
        issue for issue in report.issues if issue.snapshot.file_type == target_file_type
    )
    quarantine_name = private_io._safe_quarantine_name(target_issue)
    approved_copy = f"approved-{residue_kind}"
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )

    def swap_after_last_entry_validation(_root_fd, quarantine_fd, _entries) -> None:
        os.rename(
            quarantine_name,
            approved_copy,
            src_dir_fd=quarantine_fd,
            dst_dir_fd=quarantine_fd,
        )
        if target_file_type == "directory":
            os.mkdir(quarantine_name, 0o700, dir_fd=quarantine_fd)
        elif target_file_type == "symlink":
            os.symlink(
                target_issue.snapshot.symlink_target,
                quarantine_name,
                dir_fd=quarantine_fd,
            )
        else:
            fd = os.open(
                quarantine_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=quarantine_fd,
            )
            os.close(fd)

    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_return",
        swap_after_last_entry_validation,
        raising=False,
    )

    with pytest.raises(private_io.PrivateLockPartialCommitError) as exc_info:
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    assert exc_info.value.lock_root_snapshot.file_type == "directory"
    assert exc_info.value.lock_root_snapshot.uid == os.geteuid()
    assert exc_info.value.quarantine_root_snapshot.file_type == "directory"
    assert target_issue not in exc_info.value.committed
    assert target_issue in exc_info.value.unverified
    assert os.path.lexists(quarantine_root / approved_copy)
    assert os.path.lexists(quarantine_root / quarantine_name)


@pytest.mark.parametrize(
    "residue_kind",
    ("moved-file", "pytest-directory", "pytest-symlink"),
)
def test_private_lock_reconcile_reports_unverified_partial_on_final_parent_fsync_rebind(
    tmp_path,
    monkeypatch,
    residue_kind,
):
    """Would fail if final parent fsync reported a foreign quarantine inode as committed."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    if residue_kind == "moved-file":
        canonical = lock_root / _canonical_lock_name(1)
        _touch_private_lock(canonical)
        residue = lock_root / f"{canonical.name}.moved"
        _touch_private_lock(residue)
        target_file_type = "regular"
    else:
        directory = lock_root / _pytest_residue_lock_name(0, 10)
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        symlink = lock_root / _pytest_residue_lock_name(0, 9)
        symlink.symlink_to(_pytest_residue_symlink_target(0))
        target_file_type = "directory" if residue_kind == "pytest-directory" else "symlink"
    report = _manual_private_lock_reconcile_report(lock_root)
    target_issue = next(
        issue for issue in report.issues if issue.snapshot.file_type == target_file_type
    )
    quarantine_name = private_io._safe_quarantine_name(target_issue)
    approved_copy = quarantine_root / f"approved-{residue_kind}"
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )
    real_fsync_directory_fd = private_io._fsync_directory_fd
    swapped = False

    def swap_quarantine_name_during_final_parent_fsync(fd: int) -> None:
        nonlocal swapped
        try:
            synced_path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            synced_path = None
        if (
            not swapped
            and synced_path == quarantine_root.parent
            and not os.path.lexists(lock_root / target_issue.name)
            and os.path.lexists(quarantine_root / quarantine_name)
        ):
            swapped = True
            (quarantine_root / quarantine_name).rename(approved_copy)
            replacement = quarantine_root / quarantine_name
            if target_file_type == "directory":
                replacement.mkdir(mode=0o700)
                replacement.chmod(0o700)
            elif target_file_type == "symlink":
                replacement.symlink_to(target_issue.snapshot.symlink_target)
            else:
                _touch_private_lock(replacement)
        real_fsync_directory_fd(fd)

    monkeypatch.setattr(
        private_io,
        "_fsync_directory_fd",
        swap_quarantine_name_during_final_parent_fsync,
    )

    with pytest.raises(private_io.PrivateLockPartialCommitError) as exc_info:
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    assert swapped
    assert not os.path.lexists(lock_root / target_issue.name)
    assert os.path.lexists(quarantine_root / quarantine_name)
    assert os.path.lexists(approved_copy)
    assert target_issue.name not in {
        entry.original_name for entry in exc_info.value.committed
    }
    assert target_issue in exc_info.value.unverified


@pytest.mark.parametrize(
    "residue_kind",
    ("moved-file", "pytest-directory", "pytest-symlink"),
)
def test_private_lock_reconcile_reports_partial_when_quarantine_root_rebinds_at_final_fsync(
    tmp_path,
    monkeypatch,
    residue_kind,
):
    """Would fail if a final quarantine-root rebind still returned success."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    detached_quarantine = tmp_path / "lock-quarantine-detached"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    if residue_kind == "moved-file":
        canonical = lock_root / _canonical_lock_name(1)
        _touch_private_lock(canonical)
        _touch_private_lock(lock_root / f"{canonical.name}.moved")
    else:
        directory = lock_root / _pytest_residue_lock_name(0, 10)
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        symlink = lock_root / _pytest_residue_lock_name(0, 9)
        symlink.symlink_to(_pytest_residue_symlink_target(0))
    report = _manual_private_lock_reconcile_report(lock_root)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )
    real_fsync_directory_fd = private_io._fsync_directory_fd
    swapped = False

    def swap_whole_quarantine_root_during_final_parent_fsync(fd: int) -> None:
        nonlocal swapped
        try:
            synced_path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            synced_path = None
        if (
            not swapped
            and synced_path == quarantine_root.parent
            and quarantine_root.is_dir()
            and all(not os.path.lexists(lock_root / issue.name) for issue in report.issues)
        ):
            swapped = True
            quarantine_root.rename(detached_quarantine)
            quarantine_root.mkdir(mode=0o700)
            quarantine_root.chmod(0o700)
            replacement = quarantine_root / private_io._safe_quarantine_name(
                report.issues[0]
            )
            _touch_private_lock(replacement)
        real_fsync_directory_fd(fd)

    monkeypatch.setattr(
        private_io,
        "_fsync_directory_fd",
        swap_whole_quarantine_root_during_final_parent_fsync,
    )

    with pytest.raises(private_io.PrivateLockPartialCommitError) as exc_info:
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    assert swapped
    assert exc_info.value.committed == ()
    assert exc_info.value.unmoved == ()
    assert exc_info.value.unverified == report.issues
    for issue in report.issues:
        assert os.path.lexists(
            detached_quarantine / private_io._safe_quarantine_name(issue)
        )


@pytest.mark.parametrize(
    "residue_kind",
    ("moved-file", "pytest-directory", "pytest-symlink"),
)
def test_private_lock_reconcile_does_not_restore_source_swapped_after_revalidate(
    tmp_path,
    monkeypatch,
    residue_kind,
):
    """Would fail if partial commit reopened the old rollback source seam."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    if residue_kind == "moved-file":
        canonical = lock_root / _canonical_lock_name(1)
        residue = lock_root / f"{canonical.name}.moved"
        _touch_private_lock(canonical)
        _touch_private_lock(residue)
        target_file_type = "regular"
    else:
        directory = lock_root / _pytest_residue_lock_name(0, 10)
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        symlink = lock_root / _pytest_residue_lock_name(0, 9)
        symlink.symlink_to(_pytest_residue_symlink_target(0))
        target_file_type = (
            "directory" if residue_kind == "pytest-directory" else "symlink"
        )
    report = _manual_private_lock_reconcile_report(lock_root)
    target_issue = next(
        issue for issue in report.issues if issue.snapshot.file_type == target_file_type
    )
    quarantine_name = private_io._safe_quarantine_name(target_issue)
    approved_copy = quarantine_root / f"approved-{residue_kind}"
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )
    foreign_metadata: tuple[int, ...] | None = None

    def swap_source_before_commit_error(
        original_name: str,
        current_quarantine_name: str,
    ) -> None:
        nonlocal foreign_metadata
        if original_name != target_issue.name:
            return
        (quarantine_root / current_quarantine_name).rename(approved_copy)
        replacement = quarantine_root / current_quarantine_name
        if target_file_type == "directory":
            replacement.mkdir(mode=0o700)
            replacement.chmod(0o700)
        elif target_file_type == "symlink":
            replacement.symlink_to(target_issue.snapshot.symlink_target)
        else:
            _touch_private_lock(replacement)
        foreign_metadata = _lock_metadata(replacement, follow_symlinks=False)
        raise OSError("synthetic commit failure")

    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_commit",
        swap_source_before_commit_error,
    )

    with pytest.raises(private_io.PrivateLockPartialCommitError) as exc_info:
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    assert foreign_metadata is not None
    assert not os.path.lexists(lock_root / target_issue.name)
    assert _lock_metadata(quarantine_root / quarantine_name, follow_symlinks=False) == (
        foreign_metadata
    )
    approved_after = approved_copy.lstat()
    assert approved_after.st_dev == target_issue.snapshot.device
    assert approved_after.st_ino == target_issue.snapshot.inode
    assert approved_after.st_mode == target_issue.snapshot.mode
    assert approved_after.st_uid == target_issue.snapshot.uid
    assert approved_after.st_gid == target_issue.snapshot.gid
    assert approved_after.st_nlink == target_issue.snapshot.nlink
    assert approved_after.st_size == target_issue.snapshot.size
    if target_file_type == "symlink":
        assert os.readlink(approved_copy) == target_issue.snapshot.symlink_target
    committed_names = {entry.original_name for entry in exc_info.value.committed}
    unmoved_names = {issue.name for issue in exc_info.value.unmoved}
    assert target_issue.name not in committed_names
    assert target_issue.name not in unmoved_names
    assert target_issue in exc_info.value.unverified


def test_lock_isolation_snapshot_detects_added_deleted_replaced_and_metadata_drift(
    tmp_path,
):
    """Would fail if the isolation guard compared only added lock names."""
    root = tmp_path / "lock-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    lock = root / _canonical_lock_name(1)
    _touch_private_lock(lock)
    before = test_conftest._lock_namespace_snapshot(root)

    added = root / _canonical_lock_name(2)
    _touch_private_lock(added)
    with pytest.raises(AssertionError, match="changed"):
        test_conftest._assert_lock_namespace_unchanged(
            root,
            before,
            label="test lock root",
        )
    added.unlink()

    lock.unlink()
    with pytest.raises(AssertionError, match="changed"):
        test_conftest._assert_lock_namespace_unchanged(
            root,
            before,
            label="test lock root",
        )
    _touch_private_lock(lock)

    lock.unlink()
    _touch_private_lock(lock)
    with pytest.raises(AssertionError, match="changed"):
        test_conftest._assert_lock_namespace_unchanged(
            root,
            before,
            label="test lock root",
        )

    before_replacement = test_conftest._lock_namespace_snapshot(root)
    lock.chmod(0o640)
    with pytest.raises(AssertionError, match="changed"):
        test_conftest._assert_lock_namespace_unchanged(
            root,
            before_replacement,
            label="test lock root",
        )

    before_time = test_conftest._lock_namespace_snapshot(root)
    os.utime(
        lock,
        ns=(
            before_time.entries[0].mtime_ns or lock.stat().st_atime_ns,
            (before_time.entries[0].mtime_ns or lock.stat().st_mtime_ns) + 1_000_000,
        ),
        follow_symlinks=False,
    )
    with pytest.raises(AssertionError, match="changed"):
        test_conftest._assert_lock_namespace_unchanged(
            root,
            before_time,
            label="test lock root",
        )


def test_lock_isolation_guard_binds_real_autouse_configuration(pytestconfig):
    """Would fail if the real fixtures did not bind fd-pinned root evidence."""
    session_guard = pytestconfig._private_lock_session_guard
    test_guard = pytestconfig._private_lock_current_test_guard

    for guard in (session_guard, test_guard):
        snapshot = guard.before
        assert guard.production_root == pytestconfig._private_lock_production_root
        assert snapshot.include_times is True
        assert snapshot.root is not None
        assert snapshot.root_identities
        assert snapshot.root_identities[-1] == snapshot.root
        assert len(snapshot.approval_hash) == 64
        assert snapshot.approval_hash == test_conftest._lock_namespace_approval_hash(
            snapshot
        )
        guard.assert_unchanged(label="real fixture lock root")


def test_lock_isolation_guard_hashes_regular_lock_content_with_noatime(
    tmp_path,
    monkeypatch,
):
    """Would fail if regular lock content was not fd-read with O_NOATIME."""
    root = tmp_path / "lock-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    lock = root / _canonical_lock_name(1)
    lock.write_bytes(b"bounded lock payload")
    lock.chmod(0o600)
    opened: list[tuple[object, int]] = []
    real_open = test_conftest._REAL_OPEN

    def record_open(path, flags, *args, **kwargs):
        opened.append((path, flags))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(test_conftest, "_REAL_OPEN", record_open)

    snapshot = test_conftest._lock_namespace_snapshot(root)

    assert snapshot.root is not None
    assert snapshot.root.content_sha256 is None
    assert snapshot.entries[0].content_sha256 == hashlib.sha256(
        b"bounded lock payload"
    ).hexdigest()
    assert any(
        path == lock.name and flags & getattr(os, "O_NOATIME", 0)
        for path, flags in opened
    )
    assert all(
        flags & getattr(os, "O_NOATIME", 0)
        for path, flags in opened
        if path in {root.name, lock.name}
    )


def _synthetic_regular_stat(
    *,
    uid: int,
    gid: int | None = None,
    mode: int = 0o755,
    device: int = 17,
    inode: int = 23,
    nlink: int = 1,
    size: int = 64,
) -> os.stat_result:
    return os.stat_result(
        (
            stat.S_IFREG | mode,
            inode,
            device,
            nlink,
            uid,
            uid if gid is None else gid,
            size,
            1_700_000_000,
            1_700_000_001,
            1_700_000_002,
        )
    )


def _patch_synthetic_bwrap(
    monkeypatch,
    *,
    fd_stats: tuple[os.stat_result, ...],
    path_stats: tuple[os.stat_result, ...] | None = None,
    payload: bytes = b"synthetic-bwrap\n",
) -> tuple[list[tuple[str, int]], list[int]]:
    opened: list[tuple[str, int]] = []
    closed: list[int] = []
    opened_fds: list[int] = []
    reads: dict[int, bytes] = {}
    next_fd = 910
    real_open = test_conftest._REAL_OPEN
    real_fstat = test_conftest._REAL_FSTAT
    real_stat = test_conftest._REAL_STAT
    real_read = test_conftest._REAL_READ
    real_close = test_conftest._REAL_CLOSE

    def fake_open(path, flags, *args, **kwargs):
        nonlocal next_fd
        if path != "/usr/bin/bwrap":
            return real_open(path, flags, *args, **kwargs)
        if args or kwargs:
            raise AssertionError("bwrap open must not use dir_fd indirection")
        fd = next_fd
        next_fd += 1
        opened.append((path, flags))
        opened_fds.append(fd)
        reads[fd] = payload
        return fd

    def fake_fstat(fd):
        try:
            index = opened_fds.index(fd)
        except ValueError:
            return real_fstat(fd)
        return fd_stats[min(index, len(fd_stats) - 1)]

    def fake_stat(path, *args, **kwargs):
        if path != "/usr/bin/bwrap":
            return real_stat(path, *args, **kwargs)
        if kwargs.get("follow_symlinks") is not False:
            raise AssertionError("bwrap path revalidation must not follow symlinks")
        index = max(0, len(opened_fds) - 1)
        stats = fd_stats if path_stats is None else path_stats
        return stats[min(index, len(stats) - 1)]

    def fake_read(fd, size):
        if fd not in reads:
            return real_read(fd, size)
        chunk = reads[fd][:size]
        reads[fd] = reads[fd][size:]
        return chunk

    def fake_close(fd):
        if fd not in opened_fds:
            real_close(fd)
            return
        closed.append(fd)

    monkeypatch.setattr(test_conftest, "_REAL_OPEN", fake_open)
    monkeypatch.setattr(test_conftest, "_REAL_FSTAT", fake_fstat)
    monkeypatch.setattr(test_conftest, "_REAL_STAT", fake_stat)
    monkeypatch.setattr(test_conftest, "_REAL_READ", fake_read)
    monkeypatch.setattr(test_conftest, "_REAL_CLOSE", fake_close)
    return opened, closed


def test_bwrap_fd_accepts_real_overflow_uid_only_with_real_procfs_and_bwrap_binding():
    """Would fail if overflow acceptance did not use real procfs plus bwrap fd proof."""
    bwrap_item = os.stat("/usr/bin/bwrap", follow_symlinks=False)
    if bwrap_item.st_uid != 65534:
        pytest.skip("environment prerequisite: /usr/bin/bwrap is not overflow-owned")

    fd = test_conftest._open_bwrap_fd()
    try:
        opened_item = os.fstat(fd)
        assert opened_item.st_uid == 65534
        assert stat.S_ISREG(opened_item.st_mode)
        assert test_conftest._same_bwrap_identity(opened_item, bwrap_item)
    finally:
        os.close(fd)


def test_bwrap_owner_trust_does_not_mask_uid_map_proof_type_errors(monkeypatch):
    """Would fail if a TypeError inside the real uid_map proof was treated as rejection."""
    payload = b"synthetic-bwrap\n"
    overflow_stat = _synthetic_regular_stat(
        uid=65534,
        gid=65534,
        size=len(payload),
    )
    binding = test_conftest._BwrapOverflowOwnerBinding(
        device=overflow_stat.st_dev,
        inode=overflow_stat.st_ino,
        mode=overflow_stat.st_mode,
        uid=overflow_stat.st_uid,
        gid=overflow_stat.st_gid,
        nlink=overflow_stat.st_nlink,
        size=overflow_stat.st_size,
        mtime_ns=overflow_stat.st_mtime_ns,
        ctime_ns=overflow_stat.st_ctime_ns,
        sha256=hashlib.sha256(payload).hexdigest(),
        namespaces=test_conftest._namespace_identities(),
    )

    def broken_uid_map_read():
        raise TypeError("synthetic uid_map proof type error")

    monkeypatch.setattr(
        test_conftest,
        "_read_proc_self_uid_map",
        broken_uid_map_read,
    )

    with pytest.raises(TypeError, match="synthetic uid_map proof type error"):
        test_conftest._bwrap_owner_is_trusted(
            overflow_stat,
            bwrap_binding=binding,
        )


def test_bwrap_fd_rejects_nobody_owned_fake_binary_with_forged_global_uid_map_proof(
    monkeypatch,
):
    """Would fail if actual uid_map proof trusted an arbitrary uid 65534 binary."""
    payload = b"synthetic-bwrap\n"
    overflow_stat = _synthetic_regular_stat(
        uid=65534,
        gid=65534,
        size=len(payload),
    )
    _opened, closed = _patch_synthetic_bwrap(
        monkeypatch,
        fd_stats=(overflow_stat, overflow_stat),
        payload=payload,
    )

    with pytest.raises(ValueError, match="bwrap executable identity is invalid"):
        test_conftest._open_bwrap_fd()

    assert closed == [910]


def test_bwrap_fd_rejects_overflow_uid_without_unmapped_host_root_uid_map(
    monkeypatch,
):
    """Would fail if uid 65534 became a generic trusted owner."""
    payload = b"synthetic-bwrap\n"
    overflow_stat = _synthetic_regular_stat(
        uid=65534,
        gid=65534,
        size=len(payload),
    )
    _opened, closed = _patch_synthetic_bwrap(
        monkeypatch,
        fd_stats=(overflow_stat,),
        payload=payload,
    )

    with pytest.raises(ValueError, match="bwrap executable identity is invalid"):
        test_conftest._open_bwrap_fd()

    assert closed == [910]


def test_bwrap_fd_rejects_rebound_overflow_binary_after_open(monkeypatch):
    """Would fail if the trusted fd was not rebound to the named /usr/bin/bwrap."""
    payload = b"synthetic-bwrap\n"
    opened_stat = _synthetic_regular_stat(
        uid=65534,
        gid=65534,
        device=17,
        inode=23,
        size=len(payload),
    )
    rebound_stat = _synthetic_regular_stat(
        uid=65534,
        gid=65534,
        device=18,
        inode=24,
        size=len(payload),
    )
    _opened, closed = _patch_synthetic_bwrap(
        monkeypatch,
        fd_stats=(opened_stat, rebound_stat),
        path_stats=(opened_stat, rebound_stat),
        payload=payload,
    )

    with pytest.raises(ValueError, match="bwrap executable identity is invalid"):
        test_conftest._open_bwrap_fd()

    assert closed == [910]


def test_bwrap_fd_rejects_reused_named_and_mirror_fd(monkeypatch):
    """Would fail if the verification mirror could close the trusted bwrap fd."""
    payload = b"synthetic-bwrap\n"
    trusted_stat = _synthetic_regular_stat(uid=0, gid=0, size=len(payload))
    opened: list[tuple[str, int]] = []
    closed: list[int] = []
    reads = {910: payload}

    def fake_open(path, flags, *args, **kwargs):
        if args or kwargs:
            raise AssertionError("bwrap open must not use dir_fd indirection")
        if path != "/usr/bin/bwrap":
            raise AssertionError(f"unexpected bwrap path {path!r}")
        opened.append((path, flags))
        reads[910] = payload
        return 910

    def fake_fstat(fd):
        if fd != 910:
            raise AssertionError(f"unexpected bwrap fd {fd}")
        return trusted_stat

    def fake_read(fd, size):
        if fd != 910:
            raise AssertionError(f"unexpected bwrap read fd {fd}")
        chunk = reads[fd][:size]
        reads[fd] = reads[fd][size:]
        return chunk

    def fake_stat(path, *args, **kwargs):
        if path != "/usr/bin/bwrap":
            raise AssertionError(f"unexpected bwrap stat path {path!r}")
        if kwargs.get("follow_symlinks") is not False:
            raise AssertionError("bwrap path revalidation must not follow symlinks")
        return trusted_stat

    def fake_close(fd):
        closed.append(fd)

    monkeypatch.setattr(test_conftest, "_REAL_OPEN", fake_open)
    monkeypatch.setattr(test_conftest, "_REAL_FSTAT", fake_fstat)
    monkeypatch.setattr(test_conftest, "_REAL_STAT", fake_stat)
    monkeypatch.setattr(test_conftest, "_REAL_READ", fake_read)
    monkeypatch.setattr(test_conftest, "_REAL_CLOSE", fake_close)

    with pytest.raises(ValueError, match="bwrap executable identity is invalid"):
        test_conftest._open_bwrap_fd()

    assert closed == [910, 910]


def test_bwrap_fd_uses_fixed_usr_bin_bwrap_despite_path_shadow(monkeypatch, tmp_path):
    """Would fail if PATH shadowing could select the executable used by the harness."""
    shadow_dir = tmp_path / "shadow-bin"
    shadow_dir.mkdir()
    (shadow_dir / "bwrap").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(shadow_dir))
    payload = b"synthetic-bwrap\n"
    trusted_stat = _synthetic_regular_stat(uid=0, gid=0, size=len(payload))
    opened, closed = _patch_synthetic_bwrap(
        monkeypatch,
        fd_stats=(trusted_stat, trusted_stat),
        payload=payload,
    )

    fd = test_conftest._open_bwrap_fd()

    assert fd == 910
    assert closed == [911]
    assert opened == [
        ("/usr/bin/bwrap", opened[0][1]),
        ("/usr/bin/bwrap", opened[1][1]),
    ]


def test_lock_isolation_unavailable_is_release_blocker_not_skip():
    """Would fail if nested bwrap absence could turn required evidence green."""
    with pytest.raises(RuntimeError, match="release evidence blocked"):
        test_conftest._require_bwrap_lock_isolation_available(False)

    assert test_conftest._require_bwrap_lock_isolation_available(True) is None


def _synthetic_dir_stat(
    *,
    uid: int,
    gid: int | None = None,
    mode: int = 0o700,
    device: int = 17,
    inode: int = 23,
    nlink: int = 2,
    size: int = 40,
) -> os.stat_result:
    return os.stat_result(
        (
            stat.S_IFDIR | mode,
            inode,
            device,
            nlink,
            uid,
            uid if gid is None else gid,
            size,
            1_700_000_000,
            1_700_000_001,
            1_700_000_002,
        )
    )


def _synthetic_symlink_stat(
    *,
    uid: int,
    gid: int | None = None,
    device: int = 17,
    inode: int = 23,
    size: int = 12,
) -> os.stat_result:
    return os.stat_result(
        (
            stat.S_IFLNK | 0o777,
            inode,
            device,
            1,
            uid,
            uid if gid is None else gid,
            size,
            1_700_000_000,
            1_700_000_001,
            1_700_000_002,
        )
    )


def _outer_lock_identity_payload(item: os.stat_result) -> dict[str, object]:
    return {
        "ctime_ns": item.st_ctime_ns,
        "device": item.st_dev,
        "file_type": test_conftest._lock_file_type(item.st_mode),
        "gid": item.st_gid,
        "inode": item.st_ino,
        "mode": item.st_mode,
        "mtime_ns": item.st_mtime_ns,
        "nlink": item.st_nlink,
        "size": item.st_size,
        "uid": item.st_uid,
    }


def _outer_mount_root(synthetic_root: Path) -> str:
    return "/" + synthetic_root.relative_to("/tmp").as_posix()


def _outer_proof_environment(
    monkeypatch,
    tmp_path: Path,
    *,
    production_root: Path | None = None,
    synthetic_root: Path | None = None,
    proof_payload_override=None,
    proof_sha_override: str | None = None,
    production_stat: os.stat_result | None = None,
    synthetic_stat: os.stat_result | None = None,
    proof_stat: os.stat_result | None = None,
    proof_final_stat: os.stat_result | None = None,
    mount_root: str | None = None,
    mount_fstype: str = "tmpfs",
    mount_options: str = "rw,nosuid,nodev",
    current_mnt_namespace: str = "mnt:[child]",
    current_user_namespace: str = "user:[child]",
    final_mnt_namespace: str | None = None,
    env_override: dict[str, str | None] | None = None,
) -> tuple[Path, dict[str, str]]:
    product = production_root or Path("/home/teladi/.local/state/codex-usage/locks")
    synthetic = synthetic_root or Path(
        "/tmp/cycle31-release-review.TEST/synthetic-lock-root"
    )
    synthetic_item = synthetic_stat or _synthetic_dir_stat(uid=os.geteuid())
    production_item = production_stat or synthetic_item
    hidden_item = _synthetic_dir_stat(uid=os.geteuid(), device=99, inode=101)
    proof_path = synthetic.parent / ".cycle31-outer-proof.json"
    payload = {
        "format": "codex-usage-cycle31-outer-lock-isolation-v1",
        "hidden_production_root_identity": _outer_lock_identity_payload(hidden_item),
        "literal_production_root": str(product),
        "parent_mnt_namespace": "mnt:[parent]",
        "parent_user_namespace": "user:[parent]",
        "proof_path": str(proof_path),
        "synthetic_root": str(synthetic),
        "synthetic_root_identity": _outer_lock_identity_payload(synthetic_item),
    }
    if proof_payload_override is not None:
        payload = proof_payload_override(payload)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    proof_item = proof_stat or _synthetic_regular_stat(
        uid=os.geteuid(),
        mode=0o600,
        device=synthetic_item.st_dev,
        inode=synthetic_item.st_ino + 1,
        size=len(raw),
    )
    final_proof_item = proof_final_stat or proof_item
    expected_sha = proof_sha_override or hashlib.sha256(raw).hexdigest()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        (
            f"42 1 0:51 {mount_root or _outer_mount_root(synthetic)} {product} "
            f"{mount_options} master:124 - {mount_fstype} tmpfs rw,seclabel\n"
        ),
        encoding="utf-8",
    )
    reads = [raw, b""]
    fstats = [proof_item, final_proof_item]

    def fake_stat(path, *args, **kwargs):
        if kwargs.get("follow_symlinks") is not False:
            raise AssertionError("outer proof stat must not follow symlinks")
        normalized = str(path)
        if normalized == str(product):
            return production_item
        if normalized == str(synthetic):
            return synthetic_item
        if normalized == str(proof_path):
            return proof_item
        raise FileNotFoundError(normalized)

    def fake_open(path, flags, *args, **kwargs):
        if str(path) != str(proof_path):
            raise FileNotFoundError(path)
        if args or kwargs:
            raise AssertionError("outer proof open must not use dir_fd")
        assert flags & getattr(os, "O_NOFOLLOW", 0)
        return 910

    def fake_fstat(fd):
        if fd != 910:
            raise AssertionError(f"unexpected proof fd {fd}")
        return fstats.pop(0)

    def fake_read(fd, size):
        if fd != 910:
            raise AssertionError(f"unexpected proof read fd {fd}")
        return reads.pop(0)[:size]

    namespace_calls: list[str] = []

    def fake_readlink(path):
        namespace_calls.append(path)
        if path == "/proc/self/ns/user":
            return current_user_namespace
        if path == "/proc/self/ns/mnt":
            if final_mnt_namespace is not None and namespace_calls.count(path) > 1:
                return final_mnt_namespace
            return current_mnt_namespace
        raise FileNotFoundError(path)

    monkeypatch.setattr(test_conftest, "_PROC_SELF_MOUNTINFO", mountinfo)
    monkeypatch.setattr(test_conftest, "_REAL_STAT", fake_stat)
    monkeypatch.setattr(test_conftest, "_REAL_OPEN", fake_open)
    monkeypatch.setattr(test_conftest, "_REAL_FSTAT", fake_fstat)
    monkeypatch.setattr(test_conftest, "_REAL_READ", fake_read)
    monkeypatch.setattr(test_conftest, "_REAL_READLINK", fake_readlink)
    monkeypatch.setattr(test_conftest, "_REAL_CLOSE", lambda _fd: None)
    environ = {
        "CODEX_USAGE_TEST_OUTER_LOCK_PARENT_MNT_NS": "mnt:[parent]",
        "CODEX_USAGE_TEST_OUTER_LOCK_PARENT_USER_NS": "user:[parent]",
        "CODEX_USAGE_TEST_OUTER_LOCK_PRODUCTION_ROOT": str(product),
        "CODEX_USAGE_TEST_OUTER_LOCK_PROOF": str(proof_path),
        "CODEX_USAGE_TEST_OUTER_LOCK_PROOF_SHA256": expected_sha,
        "CODEX_USAGE_TEST_OUTER_LOCK_SYNTHETIC_ROOT": str(synthetic),
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    if env_override:
        for name, value in env_override.items():
            if value is None:
                environ.pop(name, None)
            else:
                environ[name] = value
    return product, environ


def test_outer_lock_isolation_attestation_accepts_real_kernel_mount_proof(
    tmp_path,
    monkeypatch,
):
    """Would fail if a valid outer mount namespace still required inner bwrap."""
    production_root, environ = _outer_proof_environment(monkeypatch, tmp_path)
    before = dict(environ)

    proof = test_conftest._attest_outer_lock_isolation_from_environment(
        production_root,
        environ=environ,
    )

    assert proof is not None
    assert proof.production_root == production_root
    assert proof.synthetic_root == Path(environ["CODEX_USAGE_TEST_OUTER_LOCK_SYNTHETIC_ROOT"])
    assert proof.proof_sha256 == environ["CODEX_USAGE_TEST_OUTER_LOCK_PROOF_SHA256"]
    assert environ == before


def test_outer_lock_isolation_attestation_returns_none_without_proof_env():
    """Would fail if ordinary direct-host tests were silently treated as reviewed."""
    assert (
        test_conftest._attest_outer_lock_isolation_from_environment(
            Path("/home/teladi/.local/state/codex-usage/locks"),
            environ={},
        )
        is None
    )


@pytest.mark.parametrize(
    ("case", "kwargs", "match"),
    (
        pytest.param(
            "missing-proof",
            {"env_override": {"CODEX_USAGE_TEST_OUTER_LOCK_PROOF": None}},
            "incomplete",
            id="missing-proof",
        ),
        pytest.param(
            "forged-proof",
            {"proof_payload_override": lambda _payload: {"format": "forged"}},
            "invalid",
            id="forged-proof",
        ),
        pytest.param(
            "stale-proof-root",
            {
                "proof_payload_override": lambda payload: {
                    **payload,
                    "synthetic_root_identity": {
                        **payload["synthetic_root_identity"],
                        "inode": 9999,
                    },
                },
            },
            "changed",
            id="stale-proof-root",
        ),
        pytest.param(
            "wrong-literal-target",
            {
                "proof_payload_override": lambda payload: {
                    **payload,
                    "literal_production_root": "/tmp/not-the-product-root",
                },
            },
            "literal",
            id="wrong-literal-target",
        ),
        pytest.param(
            "wrong-literal-source",
            {"mount_root": "/wrong-source"},
            "mount source",
            id="wrong-literal-source",
        ),
        pytest.param(
            "wrong-device",
            {"production_stat": _synthetic_dir_stat(uid=os.geteuid(), device=18)},
            "synthetic root",
            id="wrong-device",
        ),
        pytest.param(
            "wrong-mode",
            {"synthetic_stat": _synthetic_dir_stat(uid=os.geteuid(), mode=0o755)},
            "mode",
            id="wrong-mode",
        ),
        pytest.param(
            "wrong-owner",
            {"synthetic_stat": _synthetic_dir_stat(uid=os.geteuid() + 1)},
            "owner",
            id="wrong-owner",
        ),
        pytest.param(
            "symlink-rebind",
            {"production_stat": _synthetic_symlink_stat(uid=os.geteuid())},
            "directory",
            id="symlink-rebind",
        ),
        pytest.param(
            "proof-rebind",
            {
                "proof_final_stat": _synthetic_regular_stat(
                    uid=os.geteuid(),
                    mode=0o600,
                    device=17,
                    inode=999,
                ),
            },
            "changed",
            id="proof-rebind",
        ),
        pytest.param(
            "wrong-sha",
            {"proof_sha_override": "0" * 64},
            "sha",
            id="wrong-sha",
        ),
        pytest.param(
            "wrong-mountinfo-fs",
            {"mount_fstype": "ext4"},
            "mountinfo",
            id="wrong-mountinfo-fs",
        ),
        pytest.param(
            "wrong-mountinfo-options",
            {"mount_options": "rw"},
            "mountinfo",
            id="wrong-mountinfo-options",
        ),
        pytest.param(
            "same-mount-namespace",
            {"current_mnt_namespace": "mnt:[parent]"},
            "mount namespace",
            id="same-mount-namespace",
        ),
        pytest.param(
            "changed-namespace-after-proof",
            {"final_mnt_namespace": "mnt:[changed]"},
            "namespace changed",
            id="changed-namespace-after-proof",
        ),
        pytest.param(
            "path-spoofing",
            {"env_override": {"PATH": "/tmp/shadow-bin"}},
            "PATH",
            id="path-spoofing",
        ),
        pytest.param(
            "direct-host-root-visible",
            {
                "proof_payload_override": lambda payload: {
                    **payload,
                    "hidden_production_root_identity": payload[
                        "synthetic_root_identity"
                    ],
                },
            },
            "host product root",
            id="direct-host-root-visible",
        ),
        pytest.param(
            "wrong-synthetic-root",
            {"synthetic_root": Path("/tmp/not-cycle30/synthetic-lock-root")},
            "synthetic root",
            id="wrong-synthetic-root",
        ),
    ),
)
def test_outer_lock_isolation_attestation_rejects_adversarial_proofs(
    tmp_path,
    monkeypatch,
    case,
    kwargs,
    match,
):
    """Would fail if forged outer proof could replace real kernel isolation."""
    del case
    production_root, environ = _outer_proof_environment(
        monkeypatch,
        tmp_path,
        **kwargs,
    )

    with pytest.raises(RuntimeError, match=match):
        test_conftest._attest_outer_lock_isolation_from_environment(
            production_root,
            environ=environ,
        )


def _load_outer_lock_launcher():
    spec = importlib.util.spec_from_file_location(
        "codex_usage_cycle31_outer_lock_launcher",
        _OUTER_LOCK_LAUNCHER,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("outer launcher module spec is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_outer_lock_launcher_canonical_bootstrap_uses_env_i_and_isolated_python():
    """Would fail if exact launcher selections were conflated or PATH-shadowed."""
    launcher = _load_outer_lock_launcher()
    expected_isolation_eight = (
        "tests/test_private_io.py::test_real_lock_isolation_redirects_python_subprocess_run_with_cleared_env",
        "tests/test_private_io.py::test_real_lock_isolation_redirects_shell_check_output_relative_python",
        "tests/test_private_io.py::test_real_lock_isolation_redirects_script_check_call_and_grandchild",
        "tests/test_private_io.py::test_real_lock_isolation_redirects_os_system_and_popen",
        "tests/test_private_io.py::test_real_lock_isolation_redirects_posix_spawn_and_fork_exec",
        "tests/test_private_io.py::test_real_lock_isolation_redirects_asyncio_subprocess_exec_and_shell",
        "tests/test_private_io.py::test_real_lock_isolation_denies_direct_host_lock_root_path_without_private_io[child]",
        "tests/test_private_io.py::test_real_lock_isolation_denies_direct_host_lock_root_path_without_private_io[grandchild]",
    )
    expected_release_eight = (
        "tests/test_private_io.py::test_private_lock_namespace_scan_reports_approved_residue_shapes",
        "tests/test_private_io.py::test_private_lock_reconcile_rechecks_after_last_entry_validation_before_return[moved-file]",
        "tests/test_integration_attestation.py::test_external_entrypoint_binding_uses_manifest_candidate_then_trusted_core",
        "tests/test_integration_attestation.py::test_external_core_binding_accepts_controller_modules_outside_active_release",
        "tests/test_integration_installer.py::test_installer_source_manifest_uses_producer_boundary_without_controller_modules",
        "tests/test_integration_installer.py::test_runtime_wheel_import_closure_is_exact_and_utc_precedes_python_import",
        "tests/test_integration_watchdog.py::test_execute_runs_allowed_watchdog_stage_before_attested_publisher[2]",
        "tests/test_systemd.py::test_service_runs_dedicated_integration_watchdog_with_hardening",
    )

    assert launcher.ISOLATION_EIGHT_NODEIDS == expected_isolation_eight
    assert launcher.RELEASE_EIGHT_NODEIDS == expected_release_eight
    assert len(launcher.ISOLATION_EIGHT_NODEIDS) == 8
    assert len(launcher.RELEASE_EIGHT_NODEIDS) == 8
    assert set(launcher.ISOLATION_EIGHT_NODEIDS).isdisjoint(
        launcher.RELEASE_EIGHT_NODEIDS
    )
    assert (
        launcher.select_nodeids((), release_eight=False, isolation_eight=True)
        == expected_isolation_eight
    )
    assert (
        launcher.select_nodeids((), release_eight=True, isolation_eight=False)
        == expected_release_eight
    )
    with pytest.raises(SystemExit) as mixed_selection:
        launcher.select_nodeids((), release_eight=True, isolation_eight=True)
    assert mixed_selection.value.code == 64

    argv = launcher.canonical_bootstrap_argv(
        repo_root=Path(__file__).resolve().parents[1],
        release_eight=True,
    )
    isolation_argv = launcher.canonical_bootstrap_argv(
        repo_root=Path(__file__).resolve().parents[1],
        isolation_eight=True,
    )

    assert argv[:2] == ("/usr/bin/env", "-i")
    assert "PATH=/usr/bin:/bin" in argv
    assert "PYTHONDONTWRITEBYTECODE=1" in argv
    assert "PYTHONNOUSERSITE=1" in argv
    assert "PYTHONSAFEPATH=1" in argv
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in argv
    assert "PYTHONPATH=" not in "\n".join(argv)
    python_index = argv.index("/usr/bin/python")
    assert argv[python_index : python_index + 4] == (
        "/usr/bin/python",
        "-I",
        "-S",
        str(_OUTER_LOCK_LAUNCHER),
    )
    assert argv[-1] == "--release-eight"
    assert "--isolation-eight" not in argv
    assert isolation_argv[:2] == ("/usr/bin/env", "-i")
    assert isolation_argv[python_index : python_index + 4] == (
        "/usr/bin/python",
        "-I",
        "-S",
        str(_OUTER_LOCK_LAUNCHER),
    )
    assert isolation_argv[-1] == "--isolation-eight"
    assert "--release-eight" not in isolation_argv


def test_outer_lock_launcher_rejects_unallowlisted_nodeids():
    """Would fail if Cycle31 launcher accepted arbitrary pytest commands."""
    launcher = _load_outer_lock_launcher()

    with pytest.raises(SystemExit) as exc_info:
        launcher.select_nodeids(
            (
                "tests/test_private_io.py::test_outer_lock_launcher_rejects_unallowlisted_nodeids",
                "tests/test_cli.py::test_arbitrary_host_command",
            ),
            release_eight=False,
        )

    assert exc_info.value.code == 64


def test_outer_lock_launcher_bwrap_argv_uses_absolute_tools_and_fixed_boundary(
    tmp_path,
):
    """Would fail if PATH shadowing or a relative Python could fabricate evidence."""
    launcher = _load_outer_lock_launcher()
    synthetic_root = tmp_path / "synthetic-lock-root"
    synthetic_root.mkdir(mode=0o700)
    proof_path = synthetic_root / ".cycle31-outer-proof.json"
    proof_path.write_bytes(b"{}\n")
    proof_path.chmod(0o600)
    proof_sha = hashlib.sha256(proof_path.read_bytes()).hexdigest()

    argv = launcher.build_bwrap_argv(
        repo_root=Path(__file__).resolve().parents[1],
        synthetic_root=synthetic_root,
        proof_sha256=proof_sha,
        nodeids=(launcher.RELEASE_EIGHT_NODEIDS[0],),
    )

    assert argv[0] == "/usr/bin/bwrap"
    assert "--unshare-user-try" in argv
    assert "bwrap" not in argv[1:]
    boundary = argv.index("--")
    assert argv[boundary + 1 : boundary + 4] == (
        "/usr/bin/python",
        "-S",
        "-m",
    )
    assert argv[boundary + 4] == "pytest"
    assert str(_LITERAL_PRODUCTION_LOCK_ROOT) in argv
    assert str(synthetic_root) in argv
    proof_env_index = argv.index("CODEX_USAGE_TEST_OUTER_LOCK_PROOF")
    assert argv[proof_env_index + 1] == str(tmp_path / ".cycle31-outer-proof.json")
    assert argv[proof_env_index + 1] != str(
        _LITERAL_PRODUCTION_LOCK_ROOT / ".cycle31-outer-proof.json"
    )
    assert "CODEX_USAGE_TEST_FORBID_INNER_BWRAP" in argv
    assert "CODEX_USAGE_TEST_OUTER_LOCK_PROOF_SHA256" in argv


def test_outer_lock_launcher_rejects_forged_or_direct_host_proof(tmp_path):
    """Would fail if a forged proof could replace real namespace isolation."""
    launcher = _load_outer_lock_launcher()
    snapshot = launcher.LockEntryRecord(
        name=".",
        file_type="directory",
        device=1,
        inode=2,
        mode=stat.S_IFDIR | 0o700,
        uid=os.geteuid(),
        gid=os.getegid(),
        nlink=2,
        size=40,
        mtime_ns=100,
        ctime_ns=101,
        symlink_target=None,
        content_sha256=None,
    )
    proof_path = tmp_path / ".cycle31-outer-proof.json"
    payload = launcher.outer_proof_payload(
        production_root=_LITERAL_PRODUCTION_LOCK_ROOT,
        synthetic_root=tmp_path / "synthetic-lock-root",
        proof_path=proof_path,
        synthetic_root_identity=snapshot,
        hidden_production_root_identity=snapshot,
        parent_namespaces=("user:[same]", "mnt:[same]"),
    )
    proof_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    proof_path.chmod(0o600)

    with pytest.raises(RuntimeError, match="hidden production root"):
        launcher.validate_outer_proof_payload(
            payload,
            production_root=_LITERAL_PRODUCTION_LOCK_ROOT,
            synthetic_root=tmp_path / "synthetic-lock-root",
            proof_path=proof_path,
            proof_sha256=hashlib.sha256(proof_path.read_bytes()).hexdigest(),
            current_namespaces=("user:[same]", "mnt:[same]"),
            current_root_identity=snapshot,
        )


def test_outer_lock_launcher_classifies_lock_namespace_drift():
    """Would fail if external drift could be mistaken for stable evidence."""
    launcher = _load_outer_lock_launcher()
    canonical = launcher.LockEntryRecord(
        name=f"{1:064x}.lock",
        file_type="regular",
        device=1,
        inode=2,
        mode=stat.S_IFREG | 0o600,
        uid=os.geteuid(),
        gid=os.getegid(),
        nlink=1,
        size=0,
        mtime_ns=100,
        ctime_ns=101,
        symlink_target=None,
        content_sha256=hashlib.sha256(b"").hexdigest(),
    )
    issue = launcher.LockEntryRecord(
        name=f"{2:064x}.lock.moved",
        file_type="regular",
        device=1,
        inode=3,
        mode=stat.S_IFREG | 0o600,
        uid=os.geteuid(),
        gid=os.getegid(),
        nlink=1,
        size=0,
        mtime_ns=100,
        ctime_ns=101,
        symlink_target=None,
        content_sha256=hashlib.sha256(b"").hexdigest(),
    )
    before = launcher.LockInventory(root=canonical, entries=(canonical, issue))
    changed_canonical = dataclasses.replace(
        canonical,
        size=1,
        mtime_ns=200,
        ctime_ns=201,
        content_sha256=hashlib.sha256(b"x").hexdigest(),
    )
    neutral = launcher.classify_lock_namespace_drift(
        before,
        launcher.LockInventory(root=canonical, entries=(changed_canonical, issue)),
    )
    stable = launcher.classify_lock_namespace_drift(before, before)
    task_marker = launcher.LockEntryRecord(
        name=".cycle31-outer-proof.json",
        file_type="regular",
        device=1,
        inode=4,
        mode=stat.S_IFREG | 0o600,
        uid=os.geteuid(),
        gid=os.getegid(),
        nlink=1,
        size=2,
        mtime_ns=300,
        ctime_ns=301,
        symlink_target=None,
        content_sha256=hashlib.sha256(b"{}\n").hexdigest(),
    )
    failed = launcher.classify_lock_namespace_drift(
        before,
        launcher.LockInventory(root=canonical, entries=(canonical, issue, task_marker)),
    )
    pathset_failed = launcher.classify_lock_namespace_drift(
        before,
        launcher.LockInventory(root=canonical, entries=(canonical,)),
    )
    topology_failed = launcher.classify_lock_namespace_drift(
        before,
        launcher.LockInventory(
            root=canonical,
            entries=(
                canonical,
                dataclasses.replace(issue, file_type="directory"),
            ),
        ),
    )
    noncanonical_failed = launcher.classify_lock_namespace_drift(
        before,
        launcher.LockInventory(
            root=canonical,
            entries=(canonical, dataclasses.replace(issue, size=1)),
        ),
    )

    assert stable.status == "STABLE"
    assert neutral.status == "NEUTRAL_EXTERNAL_DRIFT"
    assert neutral.deltas == (f"{1:064x}.lock",)
    assert failed.status == "FAIL"
    assert "task marker" in failed.reason
    assert pathset_failed.status == "FAIL"
    assert "path set" in pathset_failed.reason
    assert topology_failed.status == "FAIL"
    assert "topology" in topology_failed.reason
    assert noncanonical_failed.status == "FAIL"
    assert "noncanonical" in noncanonical_failed.reason


def test_outer_lock_launcher_retains_review_artifacts_and_reports_paths(
    tmp_path,
    monkeypatch,
    capsys,
):
    """Would fail if the review launcher deleted or hid owned temp artifacts."""
    launcher = _load_outer_lock_launcher()
    temp_root = tmp_path / "cycle31-release-review.TEST"
    synthetic_root = temp_root / "synthetic-lock-root"
    synthetic_root.mkdir(parents=True, mode=0o700)
    synthetic_root.chmod(0o700)
    proof_path = temp_root / launcher.PROOF_NAME
    proof_path.write_bytes(b"{}\n")
    proof_path.chmod(0o600)
    root = launcher.LockEntryRecord(
        name=".",
        file_type="directory",
        device=1,
        inode=2,
        mode=stat.S_IFDIR | 0o700,
        uid=os.geteuid(),
        gid=os.getegid(),
        nlink=2,
        size=40,
        mtime_ns=100,
        ctime_ns=101,
        symlink_target=None,
        content_sha256=None,
    )
    inventory = launcher.LockInventory(root=root, entries=())
    completed = SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher, "_validate_toolchain", lambda: None)
    monkeypatch.setattr(launcher, "snapshot_lock_namespace", lambda _root: inventory)
    monkeypatch.setattr(launcher, "_create_synthetic_root", lambda: synthetic_root)
    monkeypatch.setattr(
        launcher,
        "write_outer_proof",
        lambda **_kwargs: hashlib.sha256(proof_path.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        launcher,
        "build_bwrap_argv",
        lambda **_kwargs: (launcher.BWRAP_BINARY, "--", launcher.PYTHON_BINARY),
    )
    monkeypatch.setattr(launcher.subprocess, "run", lambda *_args, **_kwargs: completed)

    status = launcher.run_outer(("tests/test_private_io.py::synthetic",), repo_root=tmp_path)

    assert status == 0
    assert temp_root.is_dir()
    assert synthetic_root.is_dir()
    assert proof_path.is_file()
    diagnostic = capsys.readouterr().err
    prefix = "OUTER_LOCK_ISOLATION_RESULT "
    payload = json.loads(diagnostic.removeprefix(prefix))
    assert payload["status"] == "STABLE"
    assert payload["owned_artifacts"] == {
        "proof_path": str(proof_path),
        "synthetic_root": str(synthetic_root),
        "temp_root": str(temp_root),
    }


def test_uid_map_overflow_proof_rejects_untrusted_proc_view_with_correct_binding(
    tmp_path,
    monkeypatch,
):
    """Would fail if overflow ownership proof came from an untrusted uid_map view."""
    uid_map = tmp_path / "forged.uid_map"
    uid_map.write_text("0 1000 1\n", encoding="ascii")
    monkeypatch.setattr(test_conftest, "_PROC_SELF_UID_MAP", uid_map)
    binding = test_conftest._BwrapOverflowOwnerBinding(
        device=17,
        inode=23,
        mode=stat.S_IFREG | 0o755,
        uid=65534,
        gid=65534,
        nlink=1,
        size=64,
        mtime_ns=1_700_000_001_000_000_000,
        ctime_ns=1_700_000_002_000_000_000,
        sha256=hashlib.sha256(b"synthetic-bwrap\n").hexdigest(),
        namespaces=test_conftest._namespace_identities(),
    )

    assert (
        test_conftest._uid_map_treats_host_root_as_overflow(bwrap_binding=binding)
        is False
    )


@pytest.mark.parametrize(
    "uid_map_payload",
    (
        pytest.param(b"0 1000 1", id="truncated"),
        pytest.param(b"+0 1000 1\n", id="malformed-plus-sign"),
        pytest.param(b"0 1000 1\n1 1001 1\n", id="extra-line"),
        pytest.param(b"0 1000 1\n" * 4096, id="oversized"),
    ),
)
def test_uid_map_overflow_parser_rejects_malformed_or_oversized_payloads(
    uid_map_payload,
):
    """Would fail if malformed uid_map text could prove overflow ownership."""
    assert test_conftest._parse_host_root_overflow_uid_map(uid_map_payload) is False


def test_uid_map_overflow_proof_rejects_proc_fd_rebind(monkeypatch):
    """Would fail if uid_map path identity was trusted after the opened fd changed."""
    uid_map_payload = b"      1000       1000          1\n"
    namespaces = ("user:[1]", "mnt:[2]")
    opened_stat = _synthetic_regular_stat(uid=0, gid=0, device=31, inode=41)
    rebound_stat = _synthetic_regular_stat(uid=0, gid=0, device=31, inode=42)
    fstat_results = [opened_stat, rebound_stat]
    closed: list[int] = []
    binding = test_conftest._BwrapOverflowOwnerBinding(
        device=17,
        inode=23,
        mode=stat.S_IFREG | 0o755,
        uid=65534,
        gid=65534,
        nlink=1,
        size=64,
        mtime_ns=1_700_000_001_000_000_000,
        ctime_ns=1_700_000_002_000_000_000,
        sha256=hashlib.sha256(b"synthetic-bwrap\n").hexdigest(),
        namespaces=namespaces,
    )

    monkeypatch.setattr(test_conftest, "_path_is_on_procfs_mount", lambda _path: True)

    def fake_readlink(path):
        return namespaces[test_conftest._PROC_SELF_NAMESPACE_PATHS.index(path)]

    def fake_open(path, flags, *args, **kwargs):
        if path != test_conftest._PROC_SELF_UID_MAP:
            raise AssertionError(f"unexpected uid_map path {path!r}")
        if args or kwargs:
            raise AssertionError("uid_map open must not use dir_fd indirection")
        assert flags & getattr(os, "O_NOFOLLOW", 0)
        return 910

    def fake_fstat(fd):
        if fd != 910:
            raise AssertionError(f"unexpected uid_map fd {fd}")
        return fstat_results.pop(0)

    def fake_stat(path, *args, **kwargs):
        if path != test_conftest._PROC_SELF_UID_MAP:
            raise AssertionError(f"unexpected uid_map stat path {path!r}")
        if kwargs.get("follow_symlinks") is not False:
            raise AssertionError("uid_map path revalidation must not follow symlinks")
        return opened_stat

    uid_map_reads = [uid_map_payload, b""]

    def fake_read(fd, size):
        if fd != 910:
            raise AssertionError(f"unexpected uid_map read fd {fd}")
        return uid_map_reads.pop(0)[:size]

    monkeypatch.setattr(test_conftest, "_REAL_READLINK", fake_readlink)
    monkeypatch.setattr(test_conftest, "_REAL_OPEN", fake_open)
    monkeypatch.setattr(test_conftest, "_REAL_FSTAT", fake_fstat)
    monkeypatch.setattr(test_conftest, "_REAL_STAT", fake_stat)
    monkeypatch.setattr(test_conftest, "_REAL_READ", fake_read)
    monkeypatch.setattr(test_conftest, "_REAL_CLOSE", lambda fd: closed.append(fd))

    assert (
        test_conftest._uid_map_treats_host_root_as_overflow(bwrap_binding=binding)
        is False
    )
    assert closed == [910]


def test_lock_isolation_snapshot_is_bounded_without_materialized_listdir(
    tmp_path,
    monkeypatch,
):
    """Would fail if the test guard built an unbounded os.listdir snapshot."""
    root = tmp_path / "lock-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)

    def forbidden_listdir(_fd: int):
        raise AssertionError("guard namespace was materialized with os.listdir")

    with monkeypatch.context() as scoped:
        scoped.setattr(test_conftest, "_REAL_LISTDIR", forbidden_listdir)
        snapshot = test_conftest._lock_namespace_snapshot(root)

    assert snapshot.entries == ()


def test_lock_isolation_snapshot_rejects_entry_count_before_stat(
    tmp_path,
    monkeypatch,
):
    """Would fail if the guard kept scanning past the namespace entry bound."""
    root = tmp_path / "lock-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)

    class FakeScandir:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return False

        def __iter__(self):
            for index in range(private_io._PRIVATE_LOCK_NAMESPACE_MAX_ENTRIES + 1):
                yield SimpleNamespace(name=_canonical_lock_name(index))

    def forbidden_stat(*_args, **_kwargs):
        raise AssertionError("guard statted after exceeding the namespace bound")

    with monkeypatch.context() as scoped:
        scoped.setattr(test_conftest, "_REAL_SCANDIR", lambda _fd: FakeScandir())
        scoped.setattr(test_conftest, "_REAL_STAT", forbidden_stat)
        with pytest.raises(AssertionError, match="too many entries"):
            test_conftest._lock_namespace_snapshot(root)


def test_lock_isolation_snapshot_approval_uses_productive_hash_contract(tmp_path):
    """Would fail if the guard used a private test-only approval digest."""
    root = tmp_path / "lock-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    _touch_private_lock(root / _canonical_lock_name(1))

    snapshot = test_conftest._lock_namespace_snapshot(root)

    assert snapshot.approval_hash == test_conftest._lock_namespace_approval_hash(
        snapshot,
    )

    expected_report = private_io.scan_private_lock_namespace(root)
    assert all(not issue.reason.startswith("guard-") for issue in expected_report.issues)
    assert snapshot.approval_hash == private_io.private_lock_namespace_approval_hash(
        expected_report,
    )


def test_private_lock_namespace_scan_hashes_regular_residue_content(tmp_path):
    """Would fail if productive approval evidence omitted regular content hashes."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)

    report = private_io.scan_private_lock_namespace(lock_root)
    issue = report.issues[0]

    assert issue.snapshot.content_sha256 == hashlib.sha256(b"").hexdigest()
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )
    drifted_issue = dataclasses.replace(
        issue,
        snapshot=dataclasses.replace(issue.snapshot, content_sha256="0" * 64),
    )
    drifted_report = dataclasses.replace(report, issues=(drifted_issue,))
    assert private_io.private_lock_namespace_approval_hash(
        drifted_report,
        quarantine_root=quarantine_root,
    ) != approval


def test_lock_isolation_guard_detects_approval_hash_drift(tmp_path):
    """Would fail if final verification trusted stale snapshot content by fields only."""
    root = tmp_path / "lock-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    _touch_private_lock(root / _canonical_lock_name(1))
    before = test_conftest._lock_namespace_snapshot(root)
    drifted = dataclasses.replace(before, approval_hash="0" * 64)

    with pytest.raises(AssertionError, match="approval"):
        test_conftest._assert_lock_namespace_unchanged(
            root,
            drifted,
            label="test lock root",
        )


def test_lock_isolation_guard_rejects_root_and_entry_timestamp_drift(
    tmp_path,
):
    """Would fail if final verification normalized root or entry mtime/ctime drift."""
    root = tmp_path / "lock-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    lock_name = _canonical_lock_name(1)
    lock = root / lock_name
    _touch_private_lock(lock)
    guard = test_conftest._open_lock_namespace_guard(
        root,
        label="test lock root",
    )

    try:
        os.utime(
            root,
            ns=(
                (guard.before.root.mtime_ns or root.stat().st_mtime_ns) + 1_000_000,
                (guard.before.root.ctime_ns or root.stat().st_ctime_ns) + 1_000_000,
            ),
        )
        entry = guard.before.entries[0]
        os.utime(
            lock,
            ns=(
                (entry.mtime_ns or lock.stat().st_mtime_ns) + 1_000_000,
                (entry.ctime_ns or lock.stat().st_ctime_ns) + 1_000_000,
            ),
        )

        with pytest.raises(AssertionError, match=r"changed|root"):
            guard.assert_unchanged(label="test lock root")
    finally:
        guard.close()


def test_lock_isolation_guard_reports_external_timestamp_drift_separately(
    tmp_path,
):
    """Would fail if outside root drift was still attributed as test mutation."""
    root = tmp_path / "lock-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    _touch_private_lock(root / _canonical_lock_name(1))
    guard = test_conftest._open_lock_namespace_guard(
        root,
        label="test wrote persistent locks into product root",
    )

    try:
        os.utime(
            root,
            ns=(
                (guard.before.root.mtime_ns or root.stat().st_mtime_ns) + 1_000_000,
                (guard.before.root.ctime_ns or root.stat().st_ctime_ns) + 1_000_000,
            ),
        )

        with pytest.raises(AssertionError, match="external host lock namespace drift"):
            guard.assert_unchanged(label="test wrote persistent locks into product root")
    finally:
        guard.close()


def test_lock_isolation_guard_rejects_ancestor_timestamp_drift_neutrally(
    tmp_path,
):
    """Would fail if ancestor ctime/mtime drift was ignored while only the leaf was rebound."""
    parent = tmp_path / "ancestor"
    root = parent / "lock-root"
    root.mkdir(parents=True, mode=0o700)
    parent.chmod(0o700)
    root.chmod(0o700)
    guard = test_conftest._open_lock_namespace_guard(
        root,
        label="test wrote persistent locks into product root",
    )

    try:
        ancestor = guard.before.root_identities[-2]
        os.utime(
            parent,
            ns=(
                (ancestor.mtime_ns or parent.stat().st_mtime_ns) + 1_000_000,
                (ancestor.ctime_ns or parent.stat().st_ctime_ns) + 1_000_000,
            ),
        )

        with pytest.raises(AssertionError, match="external host lock namespace drift"):
            guard.assert_unchanged(label="test wrote persistent locks into product root")
    finally:
        guard.close()


def test_lock_isolation_guard_reports_entry_delta_without_writer_provenance_neutrally(
    tmp_path,
):
    """Would fail if unproven lock entry deltas were attributed to the test."""
    root = tmp_path / "lock-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    lock = root / _canonical_lock_name(1)
    _touch_private_lock(lock)
    guard = test_conftest._open_lock_namespace_guard(
        root,
        label="test wrote persistent locks into product root",
    )

    try:
        lock.write_bytes(b"test mutation")

        with pytest.raises(
            AssertionError,
            match="external host lock namespace drift",
        ) as exc_info:
            guard.assert_unchanged(label="test wrote persistent locks into product root")
        assert "entry mutation" not in str(exc_info.value)
    finally:
        guard.close()


def test_lock_isolation_guard_rejects_timestamp_drift_with_legacy_flag(
    tmp_path,
):
    """Would fail if any path normalized post-run mtime/ctime to before values."""
    root = tmp_path / "lock-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    lock = root / _canonical_lock_name(1)
    _touch_private_lock(lock)
    guard = test_conftest._open_lock_namespace_guard(
        root,
        label="test lock root",
        allow_root_time_drift=True,
    )

    try:
        os.utime(
            lock,
            ns=(
                (guard.before.entries[0].mtime_ns or lock.stat().st_mtime_ns)
                + 1_000_000,
                (guard.before.entries[0].ctime_ns or lock.stat().st_ctime_ns)
                + 1_000_000,
            ),
        )

        with pytest.raises(AssertionError, match="changed"):
            guard.assert_unchanged(label="test lock root")
    finally:
        guard.close()


def test_lock_isolation_guard_detects_named_root_replacement_with_same_entries(
    tmp_path,
):
    """Would fail if the guard rescanned the path instead of the pinned root fd."""
    root = tmp_path / "lock-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    lock_name = _canonical_lock_name(1)
    _touch_private_lock(root / lock_name)
    guard = test_conftest._open_lock_namespace_guard(
        root,
        label="test lock root",
    )
    moved = tmp_path / "old-lock-root"
    try:
        root.rename(moved)
        root.mkdir(mode=0o700)
        root.chmod(0o700)
        _touch_private_lock(root / lock_name)

        with pytest.raises(AssertionError, match="root"):
            guard.assert_unchanged(label="test lock root")
    finally:
        guard.close()


def test_lock_isolation_guard_detects_named_root_deletion(tmp_path):
    """Would fail if an unlinked but still-open lock root passed final verification."""
    root = tmp_path / "lock-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    guard = test_conftest._open_lock_namespace_guard(
        root,
        label="test lock root",
    )
    try:
        root.rmdir()

        with pytest.raises(AssertionError, match="root"):
            guard.assert_unchanged(label="test lock root")
    finally:
        guard.close()


def test_lock_isolation_guard_uses_open_time_syscalls_for_late_verification(
    tmp_path,
):
    """Would fail if leaked test monkeypatches could blind the guard finalizer."""
    root = tmp_path / "lock-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    _touch_private_lock(root / _canonical_lock_name(1))
    guard = test_conftest._open_lock_namespace_guard(
        root,
        label="test wrote persistent locks into product root",
    )
    patch = pytest.MonkeyPatch()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("leaked monkeypatch reached guard cleanup")

    try:
        for name in (
            "_REAL_SCANDIR",
            "_REAL_LSTAT",
            "_REAL_OPEN",
            "_REAL_FSTAT",
            "_REAL_STAT",
            "_REAL_LISTDIR",
            "_REAL_READLINK",
            "_REAL_READ",
            "_REAL_CLOSE",
        ):
            patch.setattr(test_conftest, name, forbidden)

        guard.assert_unchanged(label="test wrote persistent locks into product root")
        guard.close()
    finally:
        patch.undo()
        if not guard._closed:
            guard.close()


def test_lock_isolation_guard_uses_open_time_syscalls_after_os_monkeypatch(
    tmp_path,
):
    """Would fail if global os stat/close monkeypatches reached guard cleanup."""
    root = tmp_path / "lock-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    _touch_private_lock(root / _canonical_lock_name(1))
    guard = test_conftest._open_lock_namespace_guard(
        root,
        label="test wrote persistent locks into product root",
    )
    patch = pytest.MonkeyPatch()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("leaked os monkeypatch reached guard cleanup")

    try:
        patch.setattr(os, "stat", forbidden)
        patch.setattr(os, "fstat", forbidden)
        patch.setattr(os, "close", forbidden)

        guard.assert_unchanged(label="test wrote persistent locks into product root")
        guard.close()
    finally:
        patch.undo()
        if not guard._closed:
            guard.close()


def test_lock_isolation_subprocess_wrapper_covers_codex_usage_entry_forms(
    tmp_path,
):
    """Would fail if a codex-usage subprocess form bypassed the temp lock root."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"
    production_root.mkdir(mode=0o700)
    test_root.mkdir(mode=0o700)
    bwrap_fd_path = "/proc/self/fd/91"
    cases = (
        (["codex-usage", "watchdog"], {}),
        ([Path("/opt/bin/codex-usage-browser"), "--help"], {}),
        ((b"/opt/bin/codex-usage-integration-watchdog", b"--config", b"/tmp/cfg"), {}),
        ([sys.executable, "-m", "codex_usage", "--help"], {}),
        ("codex-usage --version", {"shell": True}),
        (["/bin/sh", "-c", "ignored"], {"executable": "/opt/bin/codex-usage"}),
    )

    for args, kwargs in cases:
        wrapped_args, wrapped_kwargs = test_conftest._wrap_codex_usage_subprocess_args(
            args,
            kwargs,
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path=bwrap_fd_path,
            bwrap_pass_fd=91,
        )
        wrapped = tuple(wrapped_args)
        assert wrapped[:5] == (
            bwrap_fd_path,
            "--bind",
            "/",
            "/",
            "--bind",
        )
        assert wrapped[5:8] == (str(test_root), str(production_root), "--")
        assert ("--dir", str(production_root)) not in itertools.pairwise(
            wrapped,
        )
        assert wrapped_kwargs["pass_fds"] == (91,)


@pytest.mark.parametrize(
    "case",
    (
        pytest.param("str-sequence", id="str-sequence"),
        pytest.param("bytes-sequence", id="bytes-sequence"),
    ),
)
def test_real_lock_isolation_subprocess_executable_preserves_argv0(
    case,
):
    """Would fail if executable= replaced the caller supplied argv[0]."""
    sentinel = f"cycle29-argv0-{case}"
    code = (
        "from __future__ import annotations\n"
        "import json\n"
        "from pathlib import Path\n"
        "cmdline = Path('/proc/self/cmdline').read_bytes().split(b'\\0')\n"
        "print(json.dumps({\n"
        "    'argv0': cmdline[0].decode('utf-8'),\n"
        "    'python_flags': {\n"
        "        'isolated': __import__('sys').flags.isolated,\n"
        "        'no_site': __import__('sys').flags.no_site,\n"
        "    },\n"
        "}))\n"
    )
    if case == "bytes-sequence":
        args = (os.fsencode(sentinel), b"-I", b"-S", b"-c", os.fsencode(code))
        executable = os.fsencode(sys.executable)
    else:
        args = (sentinel, "-I", "-S", "-c", code)
        executable = sys.executable

    completed = subprocess.run(
        args,
        executable=executable,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload == {
        "argv0": sentinel,
        "python_flags": {"isolated": 1, "no_site": 1},
    }


@pytest.mark.parametrize(
    ("args", "executable", "expected_argv0", "expected_command"),
    (
        pytest.param(
            "cycle29-string-argv0",
            "/opt/bin/actual-python",
            "cycle29-string-argv0",
            ("/opt/bin/actual-python",),
            id="string-args",
        ),
        pytest.param(
            (b"cycle29-bytes-argv0", b"-c", b"pass"),
            b"/opt/bin/actual-python",
            b"cycle29-bytes-argv0",
            (b"/opt/bin/actual-python", b"-c", b"pass"),
            id="bytes-sequence",
        ),
    ),
)
def test_lock_isolation_subprocess_executable_tail_keeps_caller_argv0(
    tmp_path,
    args,
    executable,
    expected_argv0,
    expected_command,
):
    """Would fail if the bwrap tail used executable= as argv[0]."""
    wrapped_args, wrapped_kwargs = test_conftest._wrap_codex_usage_subprocess_args(
        args,
        {"executable": executable},
        production_root=tmp_path / "product-locks",
        test_root=tmp_path / "test-locks",
        bwrap_fd_path="/proc/self/fd/91",
        bwrap_pass_fd=91,
    )

    assert tuple(wrapped_args[:7]) == (
        "/proc/self/fd/91",
        "--bind",
        "/",
        "/",
        "--bind",
        str(tmp_path / "test-locks"),
        str(tmp_path / "product-locks"),
    )
    assert tuple(wrapped_args[7:10]) == ("--argv0", expected_argv0, "--")
    assert tuple(wrapped_args[10:]) == expected_command
    assert wrapped_kwargs["executable"] == "/proc/self/fd/91"


def test_lock_isolation_subprocess_wrapper_wraps_non_codex_usage_commands(
    tmp_path,
):
    """Would fail if arbitrary subprocess launches could smuggle host FDs through bwrap."""
    args = [sys.executable, "-c", "print('ok')"]
    kwargs = {"pass_fds": (17,)}

    with pytest.raises(ValueError, match="pass_fds"):
        test_conftest._wrap_codex_usage_subprocess_args(
            args,
            kwargs,
            production_root=tmp_path / "product-locks",
            test_root=tmp_path / "test-locks",
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


def test_lock_isolation_subprocess_wrapper_wraps_legitimate_command_without_host_fds(
    tmp_path,
):
    """Would fail if ordinary safe launches were rejected with dangerous kwargs."""
    args = [sys.executable, "-c", "print('ok')"]

    wrapped_args, wrapped_kwargs = test_conftest._wrap_codex_usage_subprocess_args(
        args,
        {"env": {"PATH": "/usr/bin"}},
        production_root=tmp_path / "product-locks",
        test_root=tmp_path / "test-locks",
        bwrap_fd_path="/proc/self/fd/91",
        bwrap_pass_fd=91,
    )

    assert tuple(wrapped_args[:8]) == (
        "/proc/self/fd/91",
        "--bind",
        "/",
        "/",
        "--bind",
        str(tmp_path / "test-locks"),
        str(tmp_path / "product-locks"),
        "--",
    )
    assert ("--dir", str(tmp_path / "product-locks")) not in itertools.pairwise(
        wrapped_args,
    )
    assert tuple(wrapped_args[8:]) == tuple(args)
    assert wrapped_kwargs["pass_fds"] == (91,)
    assert wrapped_kwargs["close_fds"] is True


def test_lock_isolation_subprocess_wrapper_allows_exact_bounded_executable_fd(
    tmp_path,
):
    """Would fail if the bounded /proc/self/fd executable contract was blocked."""
    executable_fd = os.open(sys.executable, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        executable_fd_path = f"/proc/self/fd/{executable_fd}"
        wrapped_args, wrapped_kwargs = test_conftest._wrap_codex_usage_subprocess_args(
            [executable_fd_path, "-c", "print('ok')"],
            {"pass_fds": (executable_fd,)},
            production_root=tmp_path / "product-locks",
            test_root=tmp_path / "test-locks",
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )
    finally:
        os.close(executable_fd)

    assert tuple(wrapped_args[:8]) == (
        "/proc/self/fd/91",
        "--bind",
        "/",
        "/",
        "--bind",
        str(tmp_path / "test-locks"),
        str(tmp_path / "product-locks"),
        "--",
    )
    assert tuple(wrapped_args[8:]) == (executable_fd_path, "-c", "print('ok')")
    assert wrapped_kwargs["pass_fds"] == (executable_fd, 91)
    assert wrapped_kwargs["close_fds"] is True


def test_lock_isolation_subprocess_wrapper_preserves_safe_existing_bwrap(
    tmp_path,
):
    """Would fail if a fd-pinned already isolated bwrap command was double-wrapped."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"
    bwrap_fd_path = "/proc/self/fd/91"
    args = [
        bwrap_fd_path,
        "--bind",
        "/",
        "/",
        "--bind",
        str(test_root),
        str(production_root),
        "--",
        sys.executable,
        "-c",
        "pass",
    ]
    kwargs = {"env": {}}

    wrapped_args, wrapped_kwargs = test_conftest._wrap_codex_usage_subprocess_args(
        args,
        kwargs,
        production_root=production_root,
        test_root=test_root,
        bwrap_fd_path=bwrap_fd_path,
        bwrap_pass_fd=91,
    )

    assert wrapped_args is args
    assert wrapped_kwargs is kwargs


@pytest.mark.parametrize("bwrap_path", ("/tmp/bwrap", "bwrap"))
def test_lock_isolation_subprocess_wrapper_wraps_safe_looking_untrusted_bwrap(
    tmp_path,
    bwrap_path,
):
    """Would fail if argv[0]'s basename made a fake bwrap look trusted."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"
    bwrap_fd_path = "/proc/self/fd/91"
    args = [
        bwrap_path,
        "--bind",
        "/",
        "/",
        "--bind",
        str(test_root),
        str(production_root),
        "--",
        sys.executable,
        "-c",
        "pass",
    ]

    wrapped_args, wrapped_kwargs = test_conftest._wrap_codex_usage_subprocess_args(
        args,
        {"env": {}},
        production_root=production_root,
        test_root=test_root,
        bwrap_fd_path=bwrap_fd_path,
        bwrap_pass_fd=91,
    )

    assert wrapped_args is not args
    assert tuple(wrapped_args[:8]) == (
        bwrap_fd_path,
        "--bind",
        "/",
        "/",
        "--bind",
        str(test_root),
        str(production_root),
        "--",
    )
    assert tuple(wrapped_args[8:]) == tuple(args)
    assert wrapped_kwargs["pass_fds"] == (91,)


def test_lock_isolation_subprocess_wrapper_rejects_bwrap_dir_production_root(
    tmp_path,
):
    """Would fail if bwrap could touch the host production mountpoint."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"

    with pytest.raises(ValueError, match="production root"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [
                "/usr/bin/bwrap",
                "--bind",
                "/",
                "/",
                "--dir",
                str(production_root),
                "--bind",
                str(test_root),
                str(production_root),
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            {},
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


@pytest.mark.parametrize(
    "operation",
    (
        ("--bind", "/", "ALIAS"),
        ("--tmpfs", "ALIAS/child"),
        ("--overlay-src", "/host-lower", "--overlay", "/host-rw", "/host-work", "ALIAS/child"),
    ),
)
def test_lock_isolation_subprocess_wrapper_rejects_mount_through_bwrap_symlink_alias(
    tmp_path,
    operation,
):
    """Would fail if --symlink was not modeled as an argv-internal alias."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"
    alias = "/lockroot-alias"
    operation_tokens = tuple(
        f"{alias}/child" if token == "ALIAS/child" else alias if token == "ALIAS" else token
        for token in operation
    )

    with pytest.raises(ValueError, match="production root"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [
                "/proc/self/fd/91",
                "--bind",
                "/",
                "/",
                "--bind",
                str(test_root),
                str(production_root),
                "--symlink",
                str(production_root),
                alias,
                *operation_tokens,
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            {},
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


def test_lock_isolation_subprocess_wrapper_forgets_symlink_alias_after_later_mount(
    tmp_path,
):
    """Would fail if an overwritten symlink alias kept poisoning later child mounts."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"
    alias = "/lockroot-alias"
    args = [
        "/proc/self/fd/91",
        "--bind",
        "/",
        "/",
        "--bind",
        str(test_root),
        str(production_root),
        "--symlink",
        str(production_root),
        alias,
        "--bind",
        str(test_root),
        alias,
        "--tmpfs",
        f"{alias}/child",
        "--",
        sys.executable,
        "-c",
        "pass",
    ]
    kwargs = {"env": {}}

    wrapped_args, wrapped_kwargs = test_conftest._wrap_codex_usage_subprocess_args(
        args,
        kwargs,
        production_root=production_root,
        test_root=test_root,
        bwrap_fd_path="/proc/self/fd/91",
        bwrap_pass_fd=91,
    )

    assert wrapped_args is args
    assert wrapped_kwargs is kwargs


@pytest.mark.parametrize(
    "operation",
    (
        ("--bind", "/", "ALIAS"),
        ("--tmpfs", "ALIAS/child"),
        ("--overlay-src", "/host-lower", "--overlay", "/host-rw", "/host-work", "ALIAS/child"),
    ),
)
def test_lock_isolation_subprocess_wrapper_resolves_relative_symlink_targets(
    tmp_path,
    operation,
):
    """Would fail if relative bwrap symlink targets were not parent-relative aliases."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"
    alias = tmp_path / "lockroot-alias"
    operation_tokens = tuple(
        str(alias / "child")
        if token == "ALIAS/child"
        else str(alias)
        if token == "ALIAS"
        else token
        for token in operation
    )

    with pytest.raises(ValueError, match="production root"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [
                "/proc/self/fd/91",
                "--bind",
                "/",
                "/",
                "--bind",
                str(test_root),
                str(production_root),
                "--symlink",
                production_root.name,
                str(alias),
                *operation_tokens,
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            {},
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


def test_lock_isolation_subprocess_wrapper_forgets_relative_symlink_alias_after_later_mount(
    tmp_path,
):
    """Would fail if last-wins handling only cleared absolute symlink aliases."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"
    alias = tmp_path / "lockroot-alias"
    args = [
        "/proc/self/fd/91",
        "--bind",
        "/",
        "/",
        "--bind",
        str(test_root),
        str(production_root),
        "--symlink",
        production_root.name,
        str(alias),
        "--bind",
        str(test_root),
        str(alias),
        "--tmpfs",
        str(alias / "child"),
        "--",
        sys.executable,
        "-c",
        "pass",
    ]
    kwargs = {"env": {}}

    wrapped_args, wrapped_kwargs = test_conftest._wrap_codex_usage_subprocess_args(
        args,
        kwargs,
        production_root=production_root,
        test_root=test_root,
        bwrap_fd_path="/proc/self/fd/91",
        bwrap_pass_fd=91,
    )

    assert wrapped_args is args
    assert wrapped_kwargs is kwargs


def test_lock_isolation_subprocess_wrapper_rejects_unknown_relative_symlink_alias(
    tmp_path,
):
    """Would fail if unresolved relative symlink aliases were treated as harmless."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"
    alias = tmp_path / "lockroot-alias"

    with pytest.raises(ValueError, match="symlink alias"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [
                "/proc/self/fd/91",
                "--bind",
                "/",
                "/",
                "--bind",
                str(test_root),
                str(production_root),
                "--symlink",
                "missing-target",
                str(alias),
                "--tmpfs",
                str(alias / "child"),
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            {},
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


def test_lock_isolation_subprocess_wrapper_rejects_relative_symlink_alias_cycle(
    tmp_path,
):
    """Would fail if parent-relative symlink aliases could cycle back to the lock root."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"
    alias_a = tmp_path / "alias-a"
    alias_b = tmp_path / "alias-b"

    with pytest.raises(ValueError, match="symlink alias"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [
                "/proc/self/fd/91",
                "--bind",
                "/",
                "/",
                "--bind",
                str(test_root),
                str(production_root),
                "--symlink",
                alias_b.name,
                str(alias_a),
                "--symlink",
                alias_a.name,
                str(alias_b),
                "--tmpfs",
                str(alias_a / "child"),
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            {},
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


@pytest.mark.parametrize(
    "override",
    (
        ("--bind", "/", "DEST"),
        ("--ro-bind", "/", "DEST"),
        ("--dev-bind", "/", "DEST"),
        ("--tmpfs", "DEST"),
        ("--overlay", "/", "DEST"),
    ),
)
def test_lock_isolation_subprocess_wrapper_rejects_late_bwrap_mount_override(
    tmp_path,
    override,
):
    """Would fail if bwrap preservation ignored the last mount on product root."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"
    override_tokens = tuple(
        str(production_root) if token == "DEST" else token for token in override
    )

    with pytest.raises(ValueError, match="production root"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [
                "/usr/bin/bwrap",
                "--bind",
                "/",
                "/",
                "--bind",
                str(test_root),
                str(production_root),
                *override_tokens,
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            {},
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


def test_lock_isolation_subprocess_wrapper_rejects_dangerous_child_mount_after_safe_root(
    tmp_path,
):
    """Would fail if one final safe child mount hid an earlier dangerous child mount."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"

    with pytest.raises(ValueError, match="production root"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [
                "/usr/bin/bwrap",
                "--bind",
                "/",
                "/",
                "--bind",
                str(test_root),
                str(production_root),
                "--bind",
                "/",
                str(production_root / "child"),
                "--bind",
                str(test_root),
                str(production_root / "other-child"),
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            {},
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


def test_lock_isolation_subprocess_wrapper_rejects_late_bwrap_env_override(
    tmp_path,
):
    """Would fail if bwrap env evaluation accepted early-good last-bad values."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"

    with pytest.raises(ValueError, match="production root"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [
                "/usr/bin/bwrap",
                "--bind",
                "/",
                "/",
                "--bind",
                str(test_root),
                str(production_root),
                "--setenv",
                "XDG_STATE_HOME",
                str(test_root.parent),
                "--setenv",
                "XDG_STATE_HOME",
                str(production_root.parent),
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            {},
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


def test_lock_isolation_subprocess_wrapper_rejects_real_overlay_mount_arity(
    tmp_path,
):
    """Would fail if --overlay was parsed as SOURCE DEST instead of RW WORK DEST."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"

    with pytest.raises(ValueError, match="production root"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [
                "/usr/bin/bwrap",
                "--bind",
                "/",
                "/",
                "--bind",
                str(test_root),
                str(production_root),
                "--overlay-src",
                "/host-lower",
                "--overlay",
                "/host-rw",
                "/host-work",
                str(production_root),
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            {},
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


def test_lock_isolation_subprocess_wrapper_rejects_unknown_bwrap_mount_option(
    tmp_path,
):
    """Would fail if an unknown bwrap option could smuggle a production mount."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"

    with pytest.raises(ValueError, match="unknown bwrap option"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [
                "/usr/bin/bwrap",
                "--bind",
                "/",
                "/",
                "--bind",
                str(test_root),
                str(production_root),
                "--host-bind",
                "/",
                str(production_root / "child"),
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            {},
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


def _open_nul_args_file(tmp_path: Path, tokens: tuple[str | bytes, ...]) -> int:
    args_file = tmp_path / "bwrap.args"
    args_file.write_bytes(
        b"\0".join(
            token if isinstance(token, bytes) else token.encode("utf-8")
            for token in tokens
        )
    )
    return os.open(args_file, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))


@pytest.mark.parametrize(
    "hidden",
    (
        ("--bind", "/", "DEST"),
        ("--tmpfs", "DEST"),
        ("--overlay-src", "/host-lower", "--overlay", "/host-rw", "/host-work", "DEST"),
        ("--symlink", "DEST", "/lockroot-alias", "--tmpfs", "/lockroot-alias/child"),
        ("--ro-bind-data", "72", "DEST"),
    ),
)
def test_lock_isolation_subprocess_wrapper_rejects_hidden_bwrap_args_fd_mounts(
    tmp_path,
    hidden,
):
    """Would fail if bwrap --args FD content was not modeled before preserving bwrap."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"
    hidden_tokens = tuple(str(production_root) if token == "DEST" else token for token in hidden)
    args_fd = _open_nul_args_file(tmp_path, hidden_tokens)
    try:
        with pytest.raises(ValueError, match="production root"):
            test_conftest._wrap_codex_usage_subprocess_args(
                [
                    "/proc/self/fd/91",
                    "--bind",
                    "/",
                    "/",
                    "--bind",
                    str(test_root),
                    str(production_root),
                    "--args",
                    str(args_fd),
                    "--",
                    sys.executable,
                    "-c",
                    "pass",
                ],
                {},
                production_root=production_root,
                test_root=test_root,
                bwrap_fd_path="/proc/self/fd/91",
                bwrap_pass_fd=91,
            )
    finally:
        os.close(args_fd)


@pytest.mark.parametrize(
    "hidden",
    (
        ("--tmpfs",),
        ("--setenv", "XDG_STATE_HOME"),
        (b"\xff",),
    ),
)
def test_lock_isolation_subprocess_wrapper_rejects_malformed_hidden_bwrap_args_fd(
    tmp_path,
    hidden,
):
    """Would fail if malformed bwrap --args FD input was treated as safe isolation."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"
    args_fd = _open_nul_args_file(tmp_path, hidden)
    try:
        with pytest.raises(ValueError, match="args"):
            test_conftest._wrap_codex_usage_subprocess_args(
                [
                    "/proc/self/fd/91",
                    "--bind",
                    "/",
                    "/",
                    "--bind",
                    str(test_root),
                    str(production_root),
                    "--args",
                    str(args_fd),
                    "--",
                    sys.executable,
                    "-c",
                    "pass",
                ],
                {},
                production_root=production_root,
                test_root=test_root,
                bwrap_fd_path="/proc/self/fd/91",
                bwrap_pass_fd=91,
            )
    finally:
        os.close(args_fd)


@pytest.mark.parametrize("args_fd_token", ("-1", "91"))
def test_lock_isolation_subprocess_wrapper_rejects_unreadable_or_reused_bwrap_args_fd(
    tmp_path,
    args_fd_token,
):
    """Would fail if unreadable or reused --args FDs could hide unparsed options."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"

    with pytest.raises(ValueError, match="args"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [
                "/proc/self/fd/91",
                "--bind",
                "/",
                "/",
                "--bind",
                str(test_root),
                str(production_root),
                "--args",
                args_fd_token,
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            {},
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


@pytest.mark.parametrize(
    "override",
    (
        ("--bind-fd", "7", "DEST"),
        ("--ro-bind-fd", "7", "DEST"),
        ("--bind-data", "7", "DEST"),
        ("--ro-bind-data", "7", "DEST"),
        ("--file", "7", "DEST"),
        ("--symlink", "/host-target", "DEST"),
    ),
)
def test_lock_isolation_subprocess_wrapper_rejects_fd_data_file_symlink_targets(
    tmp_path,
    override,
):
    """Would fail if fd/data/file/symlink mounts could overwrite the lock root."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"
    override_tokens = tuple(
        str(production_root) if token == "DEST" else token for token in override
    )

    with pytest.raises(ValueError, match="production root"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [
                "/usr/bin/bwrap",
                "--bind",
                "/",
                "/",
                "--bind",
                str(test_root),
                str(production_root),
                *override_tokens,
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            {},
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


@pytest.mark.parametrize(
    "env_name",
    (
        "LD_AUDIT",
        "LD_PRELOAD",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
    ),
)
def test_lock_isolation_subprocess_wrapper_rejects_loader_environment(
    tmp_path,
    env_name,
):
    """Would fail if loader or Python path environment ran before the bwrap boundary."""
    with pytest.raises(ValueError, match="environment"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [sys.executable, "-c", "pass"],
            {"env": {env_name: "/tmp/injector"}},
            production_root=tmp_path / "product-locks",
            test_root=tmp_path / "test-locks",
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


def test_lock_isolation_subprocess_wrapper_rejects_preexec_fn(tmp_path):
    """Would fail if caller code could run with host-root FDs before bwrap exec."""
    with pytest.raises(ValueError, match="preexec_fn"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [sys.executable, "-c", "pass"],
            {"preexec_fn": lambda: None},
            production_root=tmp_path / "product-locks",
            test_root=tmp_path / "test-locks",
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


def test_lock_isolation_popen_guard_binds_real_signature_before_wrapping(monkeypatch):
    """Would fail if Popen collisions reached the isolation wrapper or real Popen."""

    def forbidden_wrap(*_args, **_kwargs):
        pytest.fail("signature-invalid Popen call reached the isolation wrapper")

    monkeypatch.setattr(
        test_conftest,
        "_wrap_codex_usage_subprocess_args",
        forbidden_wrap,
    )
    excessive_call = (
        [sys.executable, "-c", "pass"],
        *([None] * (len(test_conftest._POPEN_POSITIONAL_PARAMETER_NAMES) + 1)),
    )
    cases = (
        (([sys.executable, "-c", "pass"],), {"args": [sys.executable]}),
        (([sys.executable, "-c", "pass"], -1), {"bufsize": -1}),
        (excessive_call, {}),
        (([sys.executable, "-c", "pass"], -1, None), {"executable": None}),
        (
            (
                [sys.executable, "-c", "pass"],
                -1,
                None,
                subprocess.DEVNULL,
            ),
            {"stdin": subprocess.DEVNULL},
        ),
        (
            (
                [sys.executable, "-c", "pass"],
                -1,
                None,
                None,
                subprocess.DEVNULL,
            ),
            {"stdout": subprocess.DEVNULL},
        ),
        (
            (
                [sys.executable, "-c", "pass"],
                -1,
                None,
                None,
                None,
                subprocess.DEVNULL,
            ),
            {"stderr": subprocess.DEVNULL},
        ),
        (
            (
                [sys.executable, "-c", "pass"],
                -1,
                None,
                None,
                None,
                None,
                None,
            ),
            {"preexec_fn": None},
        ),
        (
            (
                [sys.executable, "-c", "pass"],
                -1,
                None,
                None,
                None,
                None,
                None,
                True,
                False,
            ),
            {"shell": False},
        ),
        (
            (
                [sys.executable, "-c", "pass"],
                -1,
                None,
                None,
                None,
                None,
                None,
                True,
                False,
                str(Path.cwd()),
            ),
            {"cwd": str(Path.cwd())},
        ),
        (
            (
                [sys.executable, "-c", "pass"],
                -1,
                None,
                None,
                None,
                None,
                None,
                True,
                False,
                None,
                {"PATH": "/usr/bin"},
            ),
            {"env": {"PATH": "/usr/bin"}},
        ),
        (
            (
                [sys.executable, "-c", "pass"],
                -1,
                None,
                None,
                None,
                None,
                None,
                True,
                False,
                None,
                None,
                None,
                None,
                True,
                False,
                (),
            ),
            {"pass_fds": ()},
        ),
    )

    for call_args, call_kwargs in cases:
        with pytest.raises(ValueError, match=r"arguments|positional"):
            subprocess.Popen(*call_args, **call_kwargs)


@pytest.mark.parametrize(
    "popen_args",
    (
        (-1, sys.executable),
        (-1, None, subprocess.DEVNULL),
        (-1, None, None, subprocess.DEVNULL),
        (-1, None, None, None, subprocess.DEVNULL),
        (-1, None, None, None, None, lambda: None),
        (-1, None, None, None, None, None, True, True),
        (-1, None, None, None, None, None, True, False, str(Path.cwd())),
        (-1, None, None, None, None, None, True, False, None, {"PATH": "/usr/bin"}),
        (
            -1,
            None,
            None,
            None,
            None,
            None,
            True,
            False,
            None,
            None,
            None,
            None,
            True,
            False,
            (),
        ),
    ),
)
def test_lock_isolation_popen_guard_rejects_non_bufsize_positional_controls_before_wrap(
    monkeypatch,
    popen_args,
):
    """Would fail if positional Popen controls bypassed bound-argument policy."""

    def forbidden_wrap(*_args, **_kwargs):
        pytest.fail("unsafe positional Popen controls reached the isolation wrapper")

    monkeypatch.setattr(
        test_conftest,
        "_wrap_codex_usage_subprocess_args",
        forbidden_wrap,
    )

    with pytest.raises(ValueError, match="positional"):
        subprocess.Popen([sys.executable, "-c", "pass"], *popen_args)


def test_lock_isolation_popen_guard_passes_keyword_controls_to_wrapper(
    monkeypatch,
    tmp_path,
):
    """Would fail if bound keyword controls were dropped before isolation policy."""

    class CapturedPopenControls(Exception):
        pass

    captured: list[dict[str, object]] = []

    def capture_wrap(args, popen_kwargs, **_kwargs):
        captured.append({"args": args, **popen_kwargs})
        raise CapturedPopenControls

    monkeypatch.setattr(
        test_conftest,
        "_wrap_codex_usage_subprocess_args",
        capture_wrap,
    )

    with pytest.raises(CapturedPopenControls):
        subprocess.Popen(
            [sys.executable, "-c", "pass"],
            shell=False,
            executable=None,
            env={"PATH": "/usr/bin"},
            cwd=str(tmp_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(),
            text=True,
        )

    assert len(captured) == 1
    assert captured[0]["args"] == [sys.executable, "-c", "pass"]
    assert captured[0]["shell"] is False
    assert captured[0]["executable"] is None
    assert captured[0]["env"] == {"PATH": "/usr/bin"}
    assert captured[0]["cwd"] == str(tmp_path)
    assert captured[0]["stdin"] is subprocess.DEVNULL
    assert captured[0]["stdout"] is subprocess.PIPE
    assert captured[0]["stderr"] is subprocess.PIPE
    assert captured[0]["pass_fds"] == ()
    assert captured[0]["text"] is True


def test_lock_isolation_popen_guard_allows_positional_bufsize():
    """Would fail if the guard rejected Popen's supported positional bufsize."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.write('ok')"],
        -1,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 0
    assert stdout == "ok"
    assert stderr == ""


@pytest.mark.parametrize("api_name", ("popen", "call"))
def test_lock_isolation_rejects_positional_popen_executable_before_launch(api_name):
    """Would fail if positional executable bypassed the lock isolation wrapper."""
    popen_args = (
        -1,
        sys.executable,
        subprocess.DEVNULL,
        subprocess.DEVNULL,
        subprocess.DEVNULL,
    )

    with pytest.raises(ValueError, match="positional"):
        if api_name == "popen":
            process = subprocess.Popen(
                [sys.executable, "-c", "pass"],
                *popen_args,
            )
            try:
                process.wait(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            pytest.fail("positional executable Popen bypass was accepted")
        else:
            subprocess.call([sys.executable, "-c", "pass"], *popen_args)


@pytest.mark.parametrize("api_name", ("popen", "call"))
def test_lock_isolation_rejects_positional_popen_shell_before_launch(api_name):
    """Would fail if positional shell=True bypassed the lock isolation wrapper."""
    popen_args = (
        -1,
        None,
        subprocess.DEVNULL,
        subprocess.DEVNULL,
        subprocess.DEVNULL,
        None,
        True,
        True,
    )

    with pytest.raises(ValueError, match="positional"):
        if api_name == "popen":
            process = subprocess.Popen("/bin/true", *popen_args)
            try:
                process.wait(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            pytest.fail("positional shell Popen bypass was accepted")
        else:
            subprocess.call("/bin/true", *popen_args)


def test_lock_isolation_posix_spawn_rejects_file_actions(monkeypatch):
    """Would fail if posix_spawn file_actions could open or dup host paths before bwrap."""
    if test_conftest._REAL_POSIX_SPAWN is None:
        pytest.skip("posix_spawn is unavailable")

    def fake_spawn(*_args, **_kwargs):
        raise AssertionError("posix_spawn must be rejected before launch")

    monkeypatch.setattr(test_conftest, "_REAL_POSIX_SPAWN", fake_spawn)

    with pytest.raises(ValueError, match="file_actions"):
        os.posix_spawn(
            sys.executable,
            (sys.executable, "-c", "pass"),
            {"PATH": "/usr/bin"},
            file_actions=[(os.POSIX_SPAWN_OPEN, 7, "/etc/passwd", os.O_RDONLY, 0)],
        )


def test_lock_isolation_subprocess_wrapper_rejects_path_equivalent_target(
    tmp_path,
):
    """Would fail if target comparison was only lexical string equality."""
    production_root = tmp_path / "product-locks"
    test_root = tmp_path / "test-locks"
    equivalent_target = (
        f"{production_root.parent}/{production_root.name}/../{production_root.name}"
    )

    with pytest.raises(ValueError, match="production root"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [
                "/usr/bin/bwrap",
                "--bind",
                "/",
                "/",
                "--bind",
                str(test_root),
                str(production_root),
                "--tmpfs",
                equivalent_target,
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            {},
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


def test_lock_isolation_subprocess_wrapper_rejects_symlink_alias_mount_target(
    tmp_path,
):
    """Would fail if symlink-equivalent mount targets were compared lexically."""
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    production_root = real_parent / "product-locks"
    test_root = tmp_path / "test-locks"
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="production root"):
        test_conftest._wrap_codex_usage_subprocess_args(
            [
                "/usr/bin/bwrap",
                "--bind",
                "/",
                "/",
                "--bind",
                str(test_root),
                str(production_root),
                "--tmpfs",
                str(alias_parent / production_root.name / "child"),
                "--",
                sys.executable,
                "-c",
                "pass",
            ],
            {},
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


def test_lock_isolation_spawnv_wrapper_covers_python_module_entrypoint(tmp_path):
    """Would fail if multiprocessing spawn children could smuggle host FDs."""
    with pytest.raises(ValueError, match="pass_fds"):
        test_conftest._wrap_codex_usage_spawnv_args(
            sys.executable,
            (sys.executable, "-m", "codex_usage", "watchdog"),
            (11,),
            production_root=tmp_path / "product-locks",
            test_root=tmp_path / "test-locks",
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


def _resource_tracker_spawn_request(read_fd: int):
    executable = multiprocessing.spawn.get_executable()
    args = (
        executable,
        *multiprocessing.util._args_from_interpreter_flags(),
        "-c",
        f"from multiprocessing.resource_tracker import main;main({read_fd})",
    )
    return executable, args


def _decode_spawn_vector(values):
    return tuple(os.fsdecode(os.fspath(value)) for value in values)


def test_lock_isolation_spawnv_wrapper_accepts_legitimate_resource_tracker(tmp_path):
    """Would fail if stdlib resource_tracker spawn was blocked by a blanket pass_fds ban."""
    read_fd, write_fd = os.pipe()
    try:
        path, args = _resource_tracker_spawn_request(read_fd)
        passfds = (sys.stderr.fileno(), read_fd)

        wrapped_path, wrapped_args, wrapped_passfds = (
            test_conftest._wrap_codex_usage_spawnv_args(
                path,
                args,
                passfds,
                production_root=tmp_path / "product-locks",
                test_root=tmp_path / "test-locks",
                bwrap_fd_path="/proc/self/fd/91",
                bwrap_pass_fd=91,
            )
        )

        decoded_args = _decode_spawn_vector(wrapped_args)
        boundary = decoded_args.index("--")
        assert os.fsdecode(os.fspath(wrapped_path)) == "/proc/self/fd/91"
        assert decoded_args[boundary + 1 :] == _decode_spawn_vector(args)
        assert set(wrapped_passfds) == {91, sys.stderr.fileno(), read_fd}
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_lock_isolation_spawnv_wrapper_accepts_resource_tracker_without_interpreter_flags(
    tmp_path,
):
    """Would fail if a stdlib tracker without interpreter flags was blocked."""
    read_fd, write_fd = os.pipe()
    try:
        executable = multiprocessing.spawn.get_executable()
        args = (
            executable,
            "-c",
            f"from multiprocessing.resource_tracker import main;main({read_fd})",
        )

        _path, _args, passfds = test_conftest._wrap_codex_usage_spawnv_args(
            executable,
            args,
            (sys.stderr.fileno(), read_fd),
            production_root=tmp_path / "product-locks",
            test_root=tmp_path / "test-locks",
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )

        assert set(passfds) == {91, sys.stderr.fileno(), read_fd}
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_lock_isolation_spawnv_wrapper_accepts_spawn_main_without_interpreter_flags(
    tmp_path,
):
    """Would fail if a stdlib child without interpreter flags was blocked."""
    tracker_read, tracker_write = os.pipe()
    pipe_read, pipe_write = os.pipe()
    try:
        executable = multiprocessing.spawn.get_executable()
        args = (
            executable,
            "-c",
            "from multiprocessing.spawn import spawn_main; "
            f"spawn_main(tracker_fd={tracker_read}, pipe_handle={pipe_read})",
            "--multiprocessing-fork",
        )

        _path, _args, passfds = test_conftest._wrap_codex_usage_spawnv_args(
            executable,
            args,
            (tracker_read, pipe_read),
            production_root=tmp_path / "product-locks",
            test_root=tmp_path / "test-locks",
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )

        assert set(passfds) == {91, tracker_read, pipe_read}
    finally:
        os.close(tracker_read)
        os.close(tracker_write)
        os.close(pipe_read)
        os.close(pipe_write)


def test_lock_isolation_spawnv_wrapper_rejects_forged_resource_tracker_executable(
    tmp_path,
):
    """Would fail if an attacker could reuse tracker argv with a shadow executable."""
    forged = tmp_path / "python"
    forged.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    forged.chmod(0o700)
    read_fd, write_fd = os.pipe()
    try:
        _path, args = _resource_tracker_spawn_request(read_fd)

        with pytest.raises(ValueError, match=r"resource_tracker|pass_fds"):
            test_conftest._wrap_codex_usage_spawnv_args(
                os.fsencode(forged),
                (os.fsencode(forged), *args[1:]),
                (sys.stderr.fileno(), read_fd),
                production_root=tmp_path / "product-locks",
                test_root=tmp_path / "test-locks",
                bwrap_fd_path="/proc/self/fd/91",
                bwrap_pass_fd=91,
            )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_lock_isolation_spawnv_wrapper_rejects_forged_resource_tracker_argv(
    tmp_path,
):
    """Would fail if main(fd) in argv was not bound to the inherited pipe FD."""
    read_fd, write_fd = os.pipe()
    try:
        path, args = _resource_tracker_spawn_request(read_fd)
        forged_args = (
            *args[:-1],
            "from multiprocessing.resource_tracker import main;main(999)",
        )

        with pytest.raises(ValueError, match=r"resource_tracker|pass_fds"):
            test_conftest._wrap_codex_usage_spawnv_args(
                path,
                forged_args,
                (sys.stderr.fileno(), read_fd),
                production_root=tmp_path / "product-locks",
                test_root=tmp_path / "test-locks",
                bwrap_fd_path="/proc/self/fd/91",
                bwrap_pass_fd=91,
            )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_lock_isolation_spawnv_wrapper_rejects_resource_tracker_extra_fd(
    tmp_path,
):
    """Would fail if the resource_tracker exception carried unrelated host FDs."""
    read_fd, write_fd = os.pipe()
    extra_read_fd, extra_write_fd = os.pipe()
    try:
        path, args = _resource_tracker_spawn_request(read_fd)

        with pytest.raises(ValueError, match=r"resource_tracker|pass_fds"):
            test_conftest._wrap_codex_usage_spawnv_args(
                path,
                args,
                (sys.stderr.fileno(), read_fd, extra_read_fd),
                production_root=tmp_path / "product-locks",
                test_root=tmp_path / "test-locks",
                bwrap_fd_path="/proc/self/fd/91",
                bwrap_pass_fd=91,
            )
    finally:
        os.close(read_fd)
        os.close(write_fd)
        os.close(extra_read_fd)
        os.close(extra_write_fd)


def test_lock_isolation_spawnv_wrapper_rejects_resource_tracker_fd_reuse(
    tmp_path,
):
    """Would fail if duplicated tracker FDs bypassed exact inherited-FD binding."""
    read_fd, write_fd = os.pipe()
    try:
        path, args = _resource_tracker_spawn_request(read_fd)

        with pytest.raises(ValueError, match=r"resource_tracker|pass_fds"):
            test_conftest._wrap_codex_usage_spawnv_args(
                path,
                args,
                (read_fd, read_fd),
                production_root=tmp_path / "product-locks",
                test_root=tmp_path / "test-locks",
                bwrap_fd_path="/proc/self/fd/91",
                bwrap_pass_fd=91,
            )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_lock_isolation_spawnv_wrapper_rejects_resource_tracker_closed_fd(
    tmp_path,
):
    """Would fail if numeric fd matching was accepted after descriptor reuse/close."""
    read_fd, write_fd = os.pipe()
    path, args = _resource_tracker_spawn_request(read_fd)
    os.close(read_fd)
    os.close(write_fd)

    with pytest.raises(ValueError, match=r"resource_tracker|pass_fds"):
        test_conftest._wrap_codex_usage_spawnv_args(
            path,
            args,
            (read_fd,),
            production_root=tmp_path / "product-locks",
            test_root=tmp_path / "test-locks",
            bwrap_fd_path="/proc/self/fd/91",
            bwrap_pass_fd=91,
        )


def test_lock_isolation_spawnv_wrapper_rejects_resource_tracker_loader_env(
    tmp_path,
    monkeypatch,
):
    """Would fail if inherited Python loader env reached the resource_tracker spawn."""
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "shadow"))
    read_fd, write_fd = os.pipe()
    try:
        path, args = _resource_tracker_spawn_request(read_fd)

        with pytest.raises(ValueError, match=r"environment|resource_tracker|pass_fds"):
            test_conftest._wrap_codex_usage_spawnv_args(
                path,
                args,
                (sys.stderr.fileno(), read_fd),
                production_root=tmp_path / "product-locks",
                test_root=tmp_path / "test-locks",
                bwrap_fd_path="/proc/self/fd/91",
                bwrap_pass_fd=91,
            )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_lock_isolation_spawnv_wrapper_accepts_empty_passfds(tmp_path):
    """Would fail if the bwrap FD itself was not the only allowed inherited FD."""
    path, args, passfds = test_conftest._wrap_codex_usage_spawnv_args(
        sys.executable,
        (sys.executable, "-m", "codex_usage", "watchdog"),
        (),
        production_root=tmp_path / "product-locks",
        test_root=tmp_path / "test-locks",
        bwrap_fd_path="/proc/self/fd/91",
        bwrap_pass_fd=91,
    )

    assert path == "/proc/self/fd/91"
    assert tuple(args[:8]) == (
        "/proc/self/fd/91",
        "--bind",
        "/",
        "/",
        "--bind",
        str(tmp_path / "test-locks"),
        str(tmp_path / "product-locks"),
        "--",
    )
    assert ("--dir", str(tmp_path / "product-locks")) not in itertools.pairwise(
        args,
    )
    assert tuple(passfds) == (91,)


def test_lock_isolation_wraps_execvp_execvpe_and_spawnv_family(monkeypatch):
    """Would fail if path-searching exec/spawn forms bypassed lock isolation."""
    required_reals = (
        "_REAL_EXECVP",
        "_REAL_EXECVPE",
        "_REAL_SPAWNV",
        "_REAL_SPAWNVE",
        "_REAL_SPAWNVP",
        "_REAL_SPAWNVPE",
    )
    for name in required_reals:
        assert hasattr(test_conftest, name), f"{name} is not guarded"

    calls: list[tuple[str, object, tuple[object, ...]]] = []

    def fake_execvp(path, args):
        calls.append(("execvp", path, tuple(args)))
        return None

    def fake_execvpe(path, args, env):
        assert env == {"SAFE": "1"}
        calls.append(("execvpe", path, tuple(args)))
        return None

    def fake_spawnv(mode, path, args):
        assert mode == os.P_WAIT
        calls.append(("spawnv", path, tuple(args)))
        return 0

    def fake_spawnve(mode, path, args, env):
        assert mode == os.P_WAIT
        assert env == {"SAFE": "1"}
        calls.append(("spawnve", path, tuple(args)))
        return 0

    monkeypatch.setattr(test_conftest, "_REAL_EXECVP", fake_execvp)
    monkeypatch.setattr(test_conftest, "_REAL_EXECVPE", fake_execvpe)
    monkeypatch.setattr(test_conftest, "_REAL_SPAWNV", fake_spawnv)
    monkeypatch.setattr(test_conftest, "_REAL_SPAWNVE", fake_spawnve)
    monkeypatch.setattr(test_conftest, "_REAL_SPAWNVP", fake_spawnv)
    monkeypatch.setattr(test_conftest, "_REAL_SPAWNVPE", fake_spawnve)

    original_args = (sys.executable, "-c", "pass")
    os.execvp(sys.executable, original_args)
    os.execvpe(sys.executable, original_args, {"SAFE": "1"})
    os.spawnv(os.P_WAIT, sys.executable, original_args)
    os.spawnve(os.P_WAIT, sys.executable, original_args, {"SAFE": "1"})
    os.spawnvp(os.P_WAIT, sys.executable, original_args)
    os.spawnvpe(os.P_WAIT, sys.executable, original_args, {"SAFE": "1"})

    assert [name for name, _, _ in calls] == [
        "execvp",
        "execvpe",
        "spawnv",
        "spawnve",
        "spawnv",
        "spawnve",
    ]
    for _, path, wrapped_args in calls:
        wrapped = tuple(os.fsdecode(os.fspath(value)) for value in wrapped_args)
        boundary = wrapped.index("--")
        assert os.fsdecode(os.fspath(path)) == wrapped[0]
        assert wrapped[1:5] == ("--bind", "/", "/", "--bind")
        assert wrapped[boundary + 1 :] == original_args


@pytest.mark.parametrize(
    "environment_name",
    sorted(test_conftest._FORBIDDEN_LOADER_ENV_NAMES),
)
def test_lock_isolation_execvpe_rejects_forbidden_loader_env_before_exec(
    monkeypatch,
    environment_name,
):
    """Would fail if execvpe validated argv/path before rejecting loader env."""

    def forbidden_execvpe(_path, _args, _env):
        pytest.fail("_REAL_EXECVPE must not be reached with forbidden loader env")

    monkeypatch.setattr(test_conftest, "_REAL_EXECVPE", forbidden_execvpe)

    with pytest.raises(ValueError, match="environment"):
        os.execvpe(
            sys.executable,
            (sys.executable, "-c", "pass"),
            {"SAFE": "1", environment_name: "/tmp/shadow"},
        )


def _runtime_lock_root_identity_code(*, spawn_grandchild: bool = False) -> str:
    if spawn_grandchild:
        return (
            "from __future__ import annotations\n"
            "import subprocess, sys\n"
            f"code = {_runtime_lock_root_identity_code()!r}\n"
            "output = subprocess.check_output([sys.executable, '-c', code], env={})\n"
            "sys.stdout.buffer.write(output)\n"
        )
    return (
        "from __future__ import annotations\n"
        "import json, os, stat\n"
        "from codex_usage import private_io\n"
        "item = os.stat(private_io._private_lock_root(), follow_symlinks=False)\n"
        "file_type = 'directory' if stat.S_ISDIR(item.st_mode) else 'other'\n"
        "print(json.dumps({\n"
        "    'ctime_ns': item.st_ctime_ns,\n"
        "    'device': item.st_dev,\n"
        "    'file_type': file_type,\n"
        "    'gid': item.st_gid,\n"
        "    'inode': item.st_ino,\n"
        "    'mode': item.st_mode,\n"
        "    'mtime_ns': item.st_mtime_ns,\n"
        "    'nlink': item.st_nlink,\n"
        f"    'probe_marker': {_REAL_ISOLATION_PROBE_MARKER!r},\n"
        "    'size': item.st_size,\n"
        "    'uid': item.st_uid,\n"
        "}))\n"
    )


def _direct_lock_root_write_probe_code(*, spawn_grandchild: bool = False) -> str:
    if spawn_grandchild:
        return (
            "from __future__ import annotations\n"
            "import json, os, subprocess, sys\n"
            "if sys.flags.isolated != 1 or sys.flags.no_site != 1:\n"
            "    raise SystemExit('probe parent python startup was not isolated/no-site')\n"
            f"code = {_direct_lock_root_write_probe_code()!r}\n"
            "grandchild_env = {}\n"
            "if 'PYTHONPATH' in os.environ:\n"
            "    grandchild_env['PYTHONPATH'] = os.environ['PYTHONPATH']\n"
            "output = subprocess.check_output(\n"
            "    [sys.executable, '-I', '-S', '-c', code, *sys.argv[1:]],\n"
            "    env=grandchild_env,\n"
            ")\n"
            "payload = json.loads(output)\n"
            "payload['parent_python_flags'] = {\n"
            "    'isolated': sys.flags.isolated,\n"
            "    'no_site': sys.flags.no_site,\n"
            "}\n"
            "print(json.dumps(payload))\n"
        )
    return (
        "from __future__ import annotations\n"
        "import hashlib, json, os, stat, sys\n"
        "LITERAL_ROOT = '/home/teladi/.local/state/codex-usage/locks'\n"
        "\n"
        "def fail(message):\n"
        "    raise SystemExit(message)\n"
        "\n"
        "if sys.flags.isolated != 1 or sys.flags.no_site != 1:\n"
        "    fail('probe python startup was not isolated/no-site')\n"
        "\n"
        "def unescape_mountinfo(value):\n"
        "    return (\n"
        "        value.replace('\\\\040', ' ')\n"
        "        .replace('\\\\011', '\\t')\n"
        "        .replace('\\\\012', '\\n')\n"
        "        .replace('\\\\134', '\\\\')\n"
        "    )\n"
        "\n"
        "def identity(path):\n"
        "    item = os.lstat(path)\n"
        "    if stat.S_ISDIR(item.st_mode):\n"
        "        file_type = 'directory'\n"
        "    elif stat.S_ISREG(item.st_mode):\n"
        "        file_type = 'regular'\n"
        "    elif stat.S_ISLNK(item.st_mode):\n"
        "        file_type = 'symlink'\n"
        "    else:\n"
        "        file_type = 'other'\n"
        "    return {\n"
        "        'ctime_ns': item.st_ctime_ns,\n"
        "        'device': item.st_dev,\n"
        "        'file_type': file_type,\n"
        "        'gid': item.st_gid,\n"
        "        'inode': item.st_ino,\n"
        "        'mode': item.st_mode,\n"
        "        'mtime_ns': item.st_mtime_ns,\n"
        "        'nlink': item.st_nlink,\n"
        "        'size': item.st_size,\n"
        "        'uid': item.st_uid,\n"
        "    }\n"
        "\n"
        "def same_identity(left, right):\n"
        "    return left == right\n"
        "\n"
        "def mountinfo_has_literal_bind(path):\n"
        "    with open('/proc/self/mountinfo', encoding='utf-8') as handle:\n"
        "        for line in handle:\n"
        "            prefix, separator, _suffix = line.partition(' - ')\n"
        "            if not separator:\n"
        "                fail('malformed mountinfo')\n"
        "            fields = prefix.split()\n"
        "            if len(fields) >= 5 and unescape_mountinfo(fields[4]) == path:\n"
        "                return True\n"
        "    return False\n"
        "\n"
        "def read_regular_sha256(path):\n"
        "    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)\n"
        "    fd = os.open(path, flags)\n"
        "    try:\n"
        "        before = os.fstat(fd)\n"
        "        if not stat.S_ISREG(before.st_mode) or before.st_size > 4096:\n"
        "            fail('proof file is invalid')\n"
        "        payload = bytearray()\n"
        "        while len(payload) <= 4096:\n"
        "            chunk = os.read(fd, min(4096, 4097 - len(payload)))\n"
        "            if not chunk:\n"
        "                break\n"
        "            payload.extend(chunk)\n"
        "        if len(payload) > 4096:\n"
        "            fail('proof file is too large')\n"
        "        after = os.fstat(fd)\n"
        "        if identity_tuple(before) != identity_tuple(after):\n"
        "            fail('proof file changed during read')\n"
        "        return hashlib.sha256(bytes(payload)).hexdigest()\n"
        "    finally:\n"
        "        os.close(fd)\n"
        "\n"
        "def identity_tuple(item):\n"
        "    return (\n"
        "        item.st_dev,\n"
        "        item.st_ino,\n"
        "        item.st_mode,\n"
        "        item.st_uid,\n"
        "        item.st_gid,\n"
        "        item.st_nlink,\n"
        "        item.st_size,\n"
        "        item.st_mtime_ns,\n"
        "        item.st_ctime_ns,\n"
        "    )\n"
        "\n"
        "root = sys.argv[1]\n"
        "marker = sys.argv[2]\n"
        "test_root = sys.argv[3]\n"
        "proof_name = sys.argv[4]\n"
        "expected_proof_sha256 = sys.argv[5]\n"
        "expected_root_identity = json.loads(sys.argv[6])\n"
        "hidden_root_identity = json.loads(sys.argv[7])\n"
        "parent_mnt_namespace = sys.argv[8]\n"
        "if root != LITERAL_ROOT:\n"
        "    fail('production root is not the literal lock root')\n"
        "root_identity = identity(root)\n"
        "test_root_identity = identity(test_root)\n"
        "if not same_identity(root_identity, test_root_identity):\n"
        "    fail('literal lock root is not the synthetic test root')\n"
        "if not same_identity(root_identity, expected_root_identity):\n"
        "    fail('synthetic root identity changed before marker open')\n"
        "if same_identity(root_identity, hidden_root_identity):\n"
        "    fail('original namespace lock root is still exposed')\n"
        "if not mountinfo_has_literal_bind(root):\n"
        "    fail('literal lock root is not a mountinfo bind target')\n"
        "mnt_namespace = os.readlink('/proc/self/ns/mnt')\n"
        "if mnt_namespace == parent_mnt_namespace:\n"
        "    fail('mount namespace did not change')\n"
        "proof_path = os.path.join(root, proof_name)\n"
        "proof_source_path = os.path.join(test_root, proof_name)\n"
        "if identity(proof_path) != identity(proof_source_path):\n"
        "    fail('literal proof file is not the synthetic proof file')\n"
        "proof_sha256 = read_regular_sha256(proof_path)\n"
        "if proof_sha256 != expected_proof_sha256:\n"
        "    fail('literal proof hash mismatch')\n"
        "fd = os.open(os.path.join(root, marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)\n"
        "try:\n"
        "    os.write(fd, b'direct-kernel-probe')\n"
        "finally:\n"
        "    os.close(fd)\n"
        "print(json.dumps({\n"
        "    'file_type': root_identity['file_type'],\n"
        "    'literal_path': root,\n"
        "    'mnt_namespace_changed': True,\n"
        "    'mountinfo_has_literal_bind': True,\n"
        "    'proof_sha256': proof_sha256,\n"
        "    'python_flags': {\n"
        "        'isolated': sys.flags.isolated,\n"
        "        'no_site': sys.flags.no_site,\n"
        "    },\n"
        "    'root_identity': root_identity,\n"
        "}))\n"
    )


def _run_direct_lock_root_probe(
    *,
    production_root: Path,
    test_root: Path,
    marker_name: str,
    proof_name: str,
    expected_proof_sha256: str,
    expected_root_identity: dict[str, object],
    hidden_root_identity: dict[str, object],
    spawn_grandchild: bool,
    child_env: dict[str, str] | None = None,
    outer_proof=None,
) -> dict[str, object]:
    if outer_proof is None:
        outer_proof = test_conftest._attest_outer_lock_isolation_from_environment(
            production_root,
            environ=os.environ,
        )
    bwrap_fd: int | None = None
    if outer_proof is not None:
        if outer_proof.synthetic_root != test_root:
            raise AssertionError("outer proof synthetic root does not match probe root")
        parent_mnt_namespace = outer_proof.parent_namespaces[1]
        wrapped_path = sys.executable
        wrapped_args = (
            sys.executable,
            "-I",
            "-S",
            "-c",
            _direct_lock_root_write_probe_code(spawn_grandchild=spawn_grandchild),
            str(production_root),
            marker_name,
            str(test_root),
            proof_name,
            expected_proof_sha256,
            json.dumps(expected_root_identity, sort_keys=True),
            json.dumps(hidden_root_identity, sort_keys=True),
            parent_mnt_namespace,
        )
    else:
        bwrap_fd = test_conftest._open_bwrap_fd()
        os.set_inheritable(bwrap_fd, True)
        bwrap_fd_path = f"/proc/self/fd/{bwrap_fd}"
        parent_mnt_namespace = os.readlink("/proc/self/ns/mnt")
        wrapped_path, wrapped_args = test_conftest._wrap_lock_isolation_exec_args(
            sys.executable,
            (
                sys.executable,
                "-I",
                "-S",
                "-c",
                _direct_lock_root_write_probe_code(spawn_grandchild=spawn_grandchild),
                str(production_root),
                marker_name,
                str(test_root),
                proof_name,
                expected_proof_sha256,
                json.dumps(expected_root_identity, sort_keys=True),
                json.dumps(hidden_root_identity, sort_keys=True),
                parent_mnt_namespace,
            ),
            production_root=production_root,
            test_root=test_root,
            bwrap_fd_path=bwrap_fd_path,
        )
    try:
        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        try:
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(stdout_read)
                    os.close(stderr_read)
                    os.dup2(stdout_write, 1)
                    os.dup2(stderr_write, 2)
                    os.close(stdout_write)
                    os.close(stderr_write)
                    test_conftest._REAL_EXECVE(wrapped_path, wrapped_args, child_env or {})
                except BaseException:
                    os._exit(126)
            os.close(stdout_write)
            os.close(stderr_write)
            stdout = b""
            stderr = b""
            while True:
                chunk = os.read(stdout_read, 65_536)
                if not chunk:
                    break
                stdout += chunk
            while True:
                chunk = os.read(stderr_read, 65_536)
                if not chunk:
                    break
                stderr += chunk
            waited_pid, status = os.waitpid(pid, 0)
        finally:
            for fd in (stdout_read, stdout_write, stderr_read, stderr_write):
                try:
                    os.close(fd)
                except OSError:
                    pass
    finally:
        if bwrap_fd is not None:
            test_conftest._REAL_CLOSE(bwrap_fd)
    assert waited_pid == pid
    assert os.waitstatus_to_exitcode(status) == 0, stderr.decode("utf-8", "replace")
    return json.loads(stdout)


@pytest.mark.parametrize("spawn_grandchild", (False, True), ids=("child", "grandchild"))
def test_real_lock_isolation_denies_direct_host_lock_root_path_without_private_io(
    tmp_path,
    spawn_grandchild,
):
    """Would fail if child isolation was only a private_io/sitecustomize redirect."""
    production_root = _LITERAL_PRODUCTION_LOCK_ROOT
    outer_proof = test_conftest._attest_outer_lock_isolation_from_environment(
        production_root,
        environ=os.environ,
    )
    if outer_proof is None:
        test_root = tmp_path / "synthetic-lock-root"
        test_root.mkdir(mode=0o700)
        test_root.chmod(0o700)
        hidden_root_identity = _lock_root_identity_payload(production_root)
    else:
        test_root = outer_proof.synthetic_root
        hidden_root_identity = outer_proof.hidden_production_root_identity
    marker_suffix = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:16]
    marker_name = (
        f"cycle27-{marker_suffix}-{'grandchild' if spawn_grandchild else 'child'}"
    )
    proof_name = f".{marker_name}.proof"
    proof_payload = (
        f"{_REAL_ISOLATION_PROBE_MARKER}:{os.getpid()}:{spawn_grandchild}"
    ).encode("ascii")
    (test_root / proof_name).write_bytes(proof_payload)
    (test_root / proof_name).chmod(0o600)
    expected_root_identity = _lock_root_identity_payload(test_root)

    assert not os.path.lexists(production_root / marker_name)

    try:
        payload = _run_direct_lock_root_probe(
            production_root=production_root,
            test_root=test_root,
            marker_name=marker_name,
            proof_name=proof_name,
            expected_proof_sha256=hashlib.sha256(proof_payload).hexdigest(),
            expected_root_identity=expected_root_identity,
            hidden_root_identity=hidden_root_identity,
            spawn_grandchild=spawn_grandchild,
            outer_proof=outer_proof,
        )

        assert payload["file_type"] == "directory"
        assert payload["literal_path"] == str(_LITERAL_PRODUCTION_LOCK_ROOT)
        assert payload["mountinfo_has_literal_bind"] is True
        assert payload["proof_sha256"] == hashlib.sha256(proof_payload).hexdigest()
        assert payload["python_flags"] == {"isolated": 1, "no_site": 1}
        if spawn_grandchild:
            assert payload["parent_python_flags"] == {"isolated": 1, "no_site": 1}
        assert payload["root_identity"] == expected_root_identity
        assert payload["mnt_namespace_changed"] is True
        assert (test_root / marker_name).read_bytes() == b"direct-kernel-probe"
        if outer_proof is None:
            assert not (production_root / marker_name).exists()
    finally:
        for path in (test_root / marker_name, test_root / proof_name):
            if os.path.lexists(path):
                path.unlink()


@pytest.mark.parametrize("spawn_grandchild", (False, True), ids=("child", "grandchild"))
def test_real_lock_isolation_direct_probe_ignores_malicious_sitecustomize(
    tmp_path,
    spawn_grandchild,
):
    """Would fail if direct probes let Python site startup run before the proof."""
    production_root = _LITERAL_PRODUCTION_LOCK_ROOT
    outer_proof = test_conftest._attest_outer_lock_isolation_from_environment(
        production_root,
        environ=os.environ,
    )
    if outer_proof is None:
        test_root = tmp_path / "synthetic-lock-root"
        test_root.mkdir(mode=0o700)
        test_root.chmod(0o700)
        hidden_root_identity = _lock_root_identity_payload(production_root)
    else:
        test_root = outer_proof.synthetic_root
        hidden_root_identity = outer_proof.hidden_production_root_identity
    attacker_path = tmp_path / "attacker-pythonpath"
    attacker_path.mkdir(mode=0o700)
    marker_path = tmp_path / "sitecustomize-loaded"
    (attacker_path / "sitecustomize.py").write_text(
        (
            "from pathlib import Path\n"
            f"Path({str(marker_path)!r}).write_text('loaded', encoding='utf-8')\n"
        ),
        encoding="utf-8",
    )
    attack_env = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(attacker_path),
    }
    positive = test_conftest._REAL_SUBPROCESS_POPEN(
        [sys.executable, "-c", "import sys; print(sys.flags.isolated, sys.flags.no_site)"],
        cwd=tmp_path,
        env=attack_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    positive_stdout, positive_stderr = positive.communicate(timeout=5)
    assert positive.returncode == 0, positive_stderr
    assert positive_stdout == "0 0\n"
    assert marker_path.read_text(encoding="utf-8") == "loaded"
    marker_path.unlink()
    marker_suffix = hashlib.sha256(
        f"{tmp_path}:malicious:{spawn_grandchild}".encode(),
    ).hexdigest()[:16]
    marker_name = (
        f"cycle28-{marker_suffix}-{'grandchild' if spawn_grandchild else 'child'}"
    )
    proof_name = f".{marker_name}.proof"
    proof_payload = (
        f"{_REAL_ISOLATION_PROBE_MARKER}:{os.getpid()}:{spawn_grandchild}:malicious"
    ).encode("ascii")
    (test_root / proof_name).write_bytes(proof_payload)
    (test_root / proof_name).chmod(0o600)

    try:
        payload = _run_direct_lock_root_probe(
            production_root=production_root,
            test_root=test_root,
            marker_name=marker_name,
            proof_name=proof_name,
            expected_proof_sha256=hashlib.sha256(proof_payload).hexdigest(),
            expected_root_identity=_lock_root_identity_payload(test_root),
            hidden_root_identity=hidden_root_identity,
            spawn_grandchild=spawn_grandchild,
            child_env=attack_env,
            outer_proof=outer_proof,
        )

        assert payload["python_flags"] == {"isolated": 1, "no_site": 1}
        if spawn_grandchild:
            assert payload["parent_python_flags"] == {"isolated": 1, "no_site": 1}
        assert not marker_path.exists()
    finally:
        for path in (test_root / marker_name, test_root / proof_name):
            if os.path.lexists(path):
                path.unlink()


def _write_runtime_lock_root_identity_code() -> str:
    return (
        "from __future__ import annotations\n"
        "import json, pathlib, stat, sys\n"
        "from codex_usage import private_io\n"
        "item = private_io._private_lock_root().stat(follow_symlinks=False)\n"
        "file_type = 'directory' if stat.S_ISDIR(item.st_mode) else 'other'\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({\n"
        "    'ctime_ns': item.st_ctime_ns,\n"
        "    'device': item.st_dev,\n"
        "    'file_type': file_type,\n"
        "    'gid': item.st_gid,\n"
        "    'inode': item.st_ino,\n"
        "    'mode': item.st_mode,\n"
        "    'mtime_ns': item.st_mtime_ns,\n"
        "    'nlink': item.st_nlink,\n"
        f"    'probe_marker': {_REAL_ISOLATION_PROBE_MARKER!r},\n"
        "    'size': item.st_size,\n"
        "    'uid': item.st_uid,\n"
        "}), encoding='utf-8')\n"
    )


def _lock_root_identity_payload(path: Path) -> dict[str, object]:
    item = os.stat(path, follow_symlinks=False)
    return {
        "ctime_ns": item.st_ctime_ns,
        "device": item.st_dev,
        "file_type": "directory" if stat.S_ISDIR(item.st_mode) else "other",
        "gid": item.st_gid,
        "inode": item.st_ino,
        "mode": item.st_mode,
        "mtime_ns": item.st_mtime_ns,
        "nlink": item.st_nlink,
        "size": item.st_size,
        "uid": item.st_uid,
    }


def _assert_runtime_lock_identity_payload(
    raw_payload: bytes | str,
    expected_root: Path,
) -> None:
    payload = json.loads(raw_payload)
    assert payload.pop("probe_marker") == _REAL_ISOLATION_PROBE_MARKER
    assert payload == _lock_root_identity_payload(expected_root)


def test_real_lock_isolation_redirects_python_subprocess_run_with_cleared_env():
    """Would fail if sys.executable -c with env clearing saw the product lock root."""
    output = subprocess.check_output(
        [sys.executable, "-c", _runtime_lock_root_identity_code()],
        env={},
    )

    _assert_runtime_lock_identity_payload(output, private_io._private_lock_root())


def test_real_lock_isolation_redirects_shell_check_output_relative_python():
    """Would fail if shell or relative argv forms bypassed the temporary lock root."""
    command = " ".join(
        (
            shlex.quote(Path(sys.executable).name),
            "-c",
            shlex.quote(_runtime_lock_root_identity_code()),
        )
    )
    output = subprocess.check_output(
        command,
        shell=True,
        env={"PATH": str(Path(sys.executable).parent)},
    )

    _assert_runtime_lock_identity_payload(output, private_io._private_lock_root())


def test_real_lock_isolation_redirects_script_check_call_and_grandchild(
    tmp_path,
):
    """Would fail if script or descendant launches escaped the inherited namespace."""
    script = tmp_path / "spawn-grandchild.py"
    identity_path = tmp_path / "grandchild-root-identity.json"
    script.write_text(
        (
            f"#!{sys.executable}\n"
            "from __future__ import annotations\n"
            "import pathlib, subprocess, sys\n"
            f"code = {_runtime_lock_root_identity_code()!r}\n"
            "output = subprocess.check_output([sys.executable, '-c', code], env={})\n"
            f"pathlib.Path({str(identity_path)!r}).write_bytes(output)\n"
        ),
        encoding="utf-8",
    )
    script.chmod(0o700)

    subprocess.check_call([str(script)], env={})

    _assert_runtime_lock_identity_payload(
        identity_path.read_text(encoding="utf-8"),
        private_io._private_lock_root(),
    )


def test_real_lock_isolation_redirects_os_system_and_popen(tmp_path):
    """Would fail if shell helpers bypassed the temporary lock root."""
    system_identity = tmp_path / "system-root-identity.json"
    command = shlex.join(
        [sys.executable, "-c", _write_runtime_lock_root_identity_code(), str(system_identity)]
    )

    assert os.system(command) == 0

    _assert_runtime_lock_identity_payload(
        system_identity.read_text(encoding="utf-8"),
        private_io._private_lock_root(),
    )
    popen_output = os.popen(
        shlex.join([sys.executable, "-c", _runtime_lock_root_identity_code()])
    ).read()
    _assert_runtime_lock_identity_payload(popen_output, private_io._private_lock_root())


def test_real_lock_isolation_redirects_posix_spawn_and_fork_exec(tmp_path):
    """Would fail if low-level spawn or fork/exec bypassed lock isolation."""
    spawn_identity = tmp_path / "spawn-root-identity.json"
    argv = (
        sys.executable,
        "-c",
        _write_runtime_lock_root_identity_code(),
        str(spawn_identity),
    )

    pid = os.posix_spawn(sys.executable, argv, os.environ.copy())
    waited_pid, status = os.waitpid(pid, 0)

    assert waited_pid == pid
    assert os.waitstatus_to_exitcode(status) == 0
    _assert_runtime_lock_identity_payload(
        spawn_identity.read_text(encoding="utf-8"),
        private_io._private_lock_root(),
    )

    fork_identity = tmp_path / "fork-root-identity.json"
    pid = os.fork()
    if pid == 0:
        os.execve(
            sys.executable,
            (
                sys.executable,
                "-c",
                _write_runtime_lock_root_identity_code(),
                str(fork_identity),
            ),
            os.environ.copy(),
        )
    waited_pid, status = os.waitpid(pid, 0)

    assert waited_pid == pid
    assert os.waitstatus_to_exitcode(status) == 0
    _assert_runtime_lock_identity_payload(
        fork_identity.read_text(encoding="utf-8"),
        private_io._private_lock_root(),
    )


def test_real_lock_isolation_redirects_asyncio_subprocess_exec_and_shell():
    """Would fail if asyncio subprocess APIs bypassed the patched launcher."""
    import asyncio

    async def run_cases():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _runtime_lock_root_identity_code(),
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await process.communicate()
        assert process.returncode == 0
        _assert_runtime_lock_identity_payload(stdout, private_io._private_lock_root())

        shell_process = await asyncio.create_subprocess_shell(
            shlex.join([sys.executable, "-c", _runtime_lock_root_identity_code()]),
            stdout=asyncio.subprocess.PIPE,
        )
        shell_stdout, _stderr = await shell_process.communicate()
        assert shell_process.returncode == 0
        _assert_runtime_lock_identity_payload(
            shell_stdout,
            private_io._private_lock_root(),
        )

    asyncio.run(run_cases())


def test_private_lock_reconcile_reports_partial_commit_on_post_commit_fsync_failure(
    tmp_path,
    monkeypatch,
):
    """Would fail if post-commit fsync failure tried to roll evidence back by name."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )

    rollback_phase = False

    def fail_after_rename(_original_name: str, _quarantine_name: str) -> None:
        nonlocal rollback_phase
        rollback_phase = True
        raise OSError("synthetic commit failure")

    original_fsync_directory_fd = private_io._fsync_directory_fd

    def fail_rollback_fsync(fd: int) -> None:
        if rollback_phase:
            raise OSError("synthetic rollback fsync failure")
        original_fsync_directory_fd(fd)

    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_commit",
        fail_after_rename,
    )
    monkeypatch.setattr(private_io, "_fsync_directory_fd", fail_rollback_fsync)

    with pytest.raises(private_io.PrivateLockPartialCommitError) as exc_info:
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    quarantine_name = private_io._safe_quarantine_name(report.issues[0])
    assert not moved.exists()
    assert (quarantine_root / quarantine_name).exists()
    assert [entry.original_name for entry in exc_info.value.committed] == [
        report.issues[0].name
    ]


def test_private_lock_reconcile_attempts_all_final_fd_closes_after_first_close_error(
    tmp_path,
    monkeypatch,
):
    """Would fail if the first close error leaked later reconcile descriptors."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )
    real_close = private_io.os.close
    real_close_descriptors = private_io._close_private_lock_reconcile_descriptors
    real_verify_entries = private_io._verify_private_lock_quarantine_entries
    tracked_fds: set[int] = set()
    close_attempts: list[int] = []
    final_cleanup_armed = False

    def verify_entries_then_arm_final_cleanup(quarantine_fd, entries) -> None:
        nonlocal final_cleanup_armed
        real_verify_entries(quarantine_fd, entries)
        final_cleanup_armed = True

    def close_descriptors(*fds: int) -> None:
        tracked_fds.update(fd for fd in fds if fd >= 0)
        real_close_descriptors(*fds)

    def close_and_fail_first_tracked(fd: int) -> None:
        if final_cleanup_armed and fd in tracked_fds:
            close_attempts.append(fd)
            real_close(fd)
            if len(close_attempts) == 1:
                raise OSError("synthetic final close failure")
            return
        real_close(fd)

    monkeypatch.setattr(private_io.os, "close", close_and_fail_first_tracked)
    monkeypatch.setattr(
        private_io,
        "_close_private_lock_reconcile_descriptors",
        close_descriptors,
    )
    monkeypatch.setattr(
        private_io,
        "_verify_private_lock_quarantine_entries",
        verify_entries_then_arm_final_cleanup,
    )

    with pytest.raises(OSError, match="synthetic final close failure"):
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    assert final_cleanup_armed
    assert len(close_attempts) >= 3


def _flatten_exception_group(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, ExceptionGroup):
        flattened: list[BaseException] = []
        for nested in exc.exceptions:
            flattened.extend(_flatten_exception_group(nested))
        return flattened
    return [exc]


def test_private_lock_reconcile_aggregates_primary_partial_and_close_errors(
    tmp_path,
    monkeypatch,
):
    """Would fail if close failures were discarded during a primary unwind."""
    lock_root = tmp_path / "lock-root"
    quarantine_root = tmp_path / "lock-quarantine"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(1)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report = private_io.scan_private_lock_namespace(lock_root)
    approval = private_io.private_lock_namespace_approval_hash(
        report,
        quarantine_root=quarantine_root,
    )
    real_close = private_io.os.close
    real_close_descriptors = private_io._close_private_lock_reconcile_descriptors
    tracked_fds: set[int] = set()
    close_attempts: list[int] = []
    final_cleanup_armed = False

    def fail_after_rename(_original_name: str, _quarantine_name: str) -> None:
        nonlocal final_cleanup_armed
        final_cleanup_armed = True
        raise OSError("synthetic primary quarantine failure")

    def close_descriptors(*fds: int) -> None:
        tracked_fds.update(fd for fd in fds if fd >= 0)
        real_close_descriptors(*fds)

    def close_and_fail_tracked(fd: int) -> None:
        if final_cleanup_armed and fd in tracked_fds:
            close_attempts.append(fd)
            real_close(fd)
            if len(close_attempts) <= 2:
                raise OSError(f"synthetic close failure {len(close_attempts)}")
            return
        real_close(fd)

    monkeypatch.setattr(private_io.os, "close", close_and_fail_tracked)
    monkeypatch.setattr(
        private_io,
        "_close_private_lock_reconcile_descriptors",
        close_descriptors,
    )
    monkeypatch.setattr(
        private_io,
        "_before_private_lock_reconcile_commit",
        fail_after_rename,
    )

    with pytest.raises(ExceptionGroup) as exc_info:
        private_io.quarantine_private_lock_residues(
            report,
            quarantine_root=quarantine_root,
            approval_hash=approval,
        )

    flattened = _flatten_exception_group(exc_info.value)
    assert final_cleanup_armed
    assert len(close_attempts) >= 3
    assert any(isinstance(error, private_io.PrivateLockPartialCommitError) for error in flattened)
    assert any("synthetic primary quarantine failure" in str(error) for error in flattened)
    assert sorted(
        str(error)
        for error in flattened
        if isinstance(error, OSError) and "synthetic close failure" in str(error)
    ) == ["synthetic close failure 1", "synthetic close failure 2"]


def test_private_lock_namespace_scan_is_bounded_without_materialized_listdir(
    tmp_path,
    monkeypatch,
):
    """Would fail if namespace scanning built a complete os.listdir list first."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    root_fd = os.open(lock_root, os.O_RDONLY | os.O_DIRECTORY)

    class FakeScandir:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return False

        def __iter__(self):
            for index in range(private_io._PRIVATE_LOCK_NAMESPACE_MAX_ENTRIES + 1):
                yield SimpleNamespace(name=_canonical_lock_name(index))

    def forbidden_listdir(_fd: int):
        raise AssertionError("namespace was materialized with os.listdir")

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(private_io.os, "listdir", forbidden_listdir)
            scoped.setattr(private_io.os, "scandir", lambda _fd: FakeScandir())
            with pytest.raises(ValueError, match="too many entries"):
                private_io._scan_private_lock_namespace_fd(lock_root, root_fd)
    finally:
        os.close(root_fd)


@pytest.mark.parametrize(
    ("root_mode", "lock_mode", "payload"),
    [
        pytest.param(0o777, 0o600, b"", id="world-writable-root"),
        pytest.param(0o700, 0o644, b"", id="wrong-lock-mode"),
        pytest.param(0o700, 0o600, b"x" * 5000, id="oversized-lock"),
    ],
)
def test_private_path_lock_no_create_rejects_unsafe_namespace_without_mutation(
    tmp_path, monkeypatch, root_mode, lock_mode, payload
):
    target, lock_root, lock_path = _existing_private_lock(
        tmp_path,
        monkeypatch,
        root_mode=root_mode,
        lock_mode=lock_mode,
        payload=payload,
    )
    root_before = _lock_metadata(lock_root)
    lock_before = _lock_metadata(lock_path)

    with pytest.raises(ValueError):
        with private_path_lock(target, label="profile lock", create=False):
            pass

    assert _lock_metadata(lock_root) == root_before
    assert _lock_metadata(lock_path) == lock_before
    assert stat.S_IMODE(lock_path.stat().st_mode) == lock_mode
    assert lock_path.read_bytes() == payload


def test_private_path_lock_no_create_rejects_symlink_root_without_mutation(
    tmp_path, monkeypatch
):
    target, real_root, lock_path = _existing_private_lock(tmp_path, monkeypatch)
    linked_root = tmp_path / "linked-lock-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: linked_root)
    link_before = _lock_metadata(linked_root, follow_symlinks=False)
    root_before = _lock_metadata(real_root)
    lock_before = _lock_metadata(lock_path)

    with pytest.raises(ValueError):
        with private_path_lock(target, label="profile lock", create=False):
            pass

    assert _lock_metadata(linked_root, follow_symlinks=False) == link_before
    assert _lock_metadata(real_root) == root_before
    assert _lock_metadata(lock_path) == lock_before


def test_private_path_lock_no_create_revalidates_named_inode_after_flock(
    tmp_path, monkeypatch
):
    target, lock_root, lock_path = _existing_private_lock(tmp_path, monkeypatch)
    old_lock_path = lock_root / f".{lock_path.name}.old"
    real_flock = private_io.fcntl.flock
    replaced = False

    def replace_after_acquire(fd, operation):
        nonlocal replaced
        result = real_flock(fd, operation)
        if operation & private_io.fcntl.LOCK_EX and not replaced:
            replaced = True
            lock_path.rename(old_lock_path)
            replacement_fd = os.open(
                lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(replacement_fd)
        return result

    monkeypatch.setattr(private_io.fcntl, "flock", replace_after_acquire)
    entered = False
    with pytest.raises(ValueError):
        with private_path_lock(target, label="profile lock", create=False):
            entered = True

    assert replaced
    assert not entered
    assert old_lock_path.is_file()
    assert lock_path.is_file()
    assert old_lock_path.stat().st_ino != lock_path.stat().st_ino


def test_private_path_lock_keeps_waiter_before_acquire_on_persistent_inode(tmp_path):
    path = tmp_path / "profile" / "profile.json"
    path.parent.mkdir()
    created_lock_files = []
    waiter_started = Event()

    def contend_for_lock():
        waiter_started.set()
        with private_path_lock(path, timeout_seconds=0, label="profile lock"):
            pass

    with private_path_lock(
        path,
        label="profile lock",
        created_lock_files=created_lock_files,
    ):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(contend_for_lock)
            assert waiter_started.wait(1)
            with pytest.raises(TimeoutError, match="already in use"):
                future.result()
            config_module._cleanup_created_private_files(
                created_lock_files,
                label="created profile lock file",
            )

    lock_path = created_lock_files[0][0]
    assert lock_path.parent != path.parent
    assert lock_path.exists()


def test_private_path_lock_namespace_is_stable_across_home_environment_changes(
    tmp_path, pytestconfig
):
    target = tmp_path / "shared" / "config.toml"
    target.parent.mkdir()
    source_root = Path(__file__).resolve().parents[1] / "src"
    lock_root = private_io._private_lock_root()
    production_lock_root = pytestconfig._private_lock_production_root
    isolation_prefix = [
        "/usr/bin/bwrap",
        "--bind",
        "/",
        "/",
        "--bind",
        str(lock_root),
        str(production_lock_root),
        "--",
    ]
    holder_code = "\n".join(
        (
            "from pathlib import Path",
            "import sys, time",
            "from codex_usage.private_io import private_path_lock",
            "with private_path_lock(Path(sys.argv[1]), timeout_seconds=5):",
            "    print('held', flush=True)",
            "    time.sleep(2)",
        )
    )
    contender_code = "\n".join(
        (
            "from pathlib import Path",
            "import sys",
            "from codex_usage.private_io import private_path_lock",
            "try:",
            "    with private_path_lock(Path(sys.argv[1]), timeout_seconds=0.2):",
            "        print('acquired', flush=True)",
            "except TimeoutError:",
            "    print('timeout', flush=True)",
        )
    )
    env_a = os.environ.copy()
    env_b = os.environ.copy()
    env_a["HOME"] = str(tmp_path / "home-a")
    env_b["HOME"] = str(tmp_path / "home-b")
    env_a["PYTHONPATH"] = str(source_root)
    env_b["PYTHONPATH"] = str(source_root)
    (tmp_path / "home-a").mkdir()
    (tmp_path / "home-b").mkdir()
    holder = subprocess.Popen(
        [*isolation_prefix, sys.executable, "-c", holder_code, str(target)],
        env=env_a,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        contender = subprocess.run(
            [*isolation_prefix, sys.executable, "-c", contender_code, str(target)],
            env=env_b,
            capture_output=True,
            text=True,
            check=True,
        )
        assert contender.stdout.strip() == "timeout"
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_created_lock_cleanup_does_not_unlink_post_unlock_waiter_inode(tmp_path):
    path = tmp_path / "profile" / "profile.json"
    path.parent.mkdir()
    created_lock_files = []
    waiter_started = Event()
    waiter_entered = Event()
    release_waiter = Event()

    def wait_for_lock():
        waiter_started.set()
        with private_path_lock(path, timeout_seconds=2, label="profile lock"):
            waiter_entered.set()
            assert release_waiter.wait(2)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with private_path_lock(
            path,
            label="profile lock",
            created_lock_files=created_lock_files,
        ):
            future = executor.submit(wait_for_lock)
            assert waiter_started.wait(1)
            assert not waiter_entered.wait(0.1)
        assert waiter_entered.wait(1)

        config_module._cleanup_created_private_files(
            created_lock_files,
            label="created profile lock file",
        )
        assert created_lock_files[0][0].exists()
        with pytest.raises(TimeoutError, match="already in use"):
            with private_path_lock(path, timeout_seconds=0, label="profile lock"):
                pass
        release_waiter.set()
        future.result()


def test_private_path_lock_ignores_unlock_error(tmp_path, monkeypatch):
    def fail_unlock(_fd, operation):
        if operation == private_io.fcntl.LOCK_UN:
            raise OSError("synthetic unlock failure")

    monkeypatch.setattr(private_io.fcntl, "flock", fail_unlock)

    with private_path_lock(tmp_path / "config", label="config lock"):
        pass


@pytest.mark.parametrize("error_type", (KeyboardInterrupt, SystemExit))
def test_private_path_lock_propagates_non_oserror_unlock_error(
    tmp_path,
    monkeypatch,
    error_type,
):
    """Would fail if unlock cleanup swallowed a non-OSError BaseException."""
    def fail_unlock(_fd, operation):
        if operation == private_io.fcntl.LOCK_UN:
            raise error_type("synthetic unlock interruption")

    monkeypatch.setattr(private_io.fcntl, "flock", fail_unlock)

    with pytest.raises(error_type, match="synthetic unlock interruption") as exc_info:
        with private_path_lock(tmp_path / "config", label="config lock"):
            pass

    assert type(exc_info.value) is error_type


def test_fsync_directory_opens_and_closes_one_descriptor(tmp_path, monkeypatch):
    opened: list[int] = []
    synced: list[int] = []
    closed: list[int] = []

    def fake_open(path, flags):
        assert path == tmp_path
        descriptor = 41 + len(opened)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(private_io.os, "open", fake_open)
    monkeypatch.setattr(private_io.os, "fsync", synced.append)
    monkeypatch.setattr(private_io.os, "close", closed.append)

    private_io._fsync_directory(tmp_path)

    assert opened == [41]
    assert synced == [41]
    assert closed == [41]


def test_private_io_handles_missing_optional_open_flags(tmp_path, monkeypatch):
    directory = tmp_path / "directory"
    directory.mkdir()
    value = tmp_path / "value.txt"
    value.write_text("secret", encoding="utf-8")

    for attribute in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK"):
        monkeypatch.delattr(private_io.os, attribute, raising=False)

    private_io._chmod_private_directory(directory, label="private directory")
    text, _ = private_io.read_private_text(
        value,
        regular_label="private",
        read_label="private",
        max_bytes=100,
    )
    assert text == "secret"
    private_io.write_private_text(tmp_path / "written.txt", "new", label="private")
    private_io._fsync_directory(tmp_path)
    with private_path_lock(tmp_path / "config", timeout_seconds=0):
        pass


def test_private_path_lock_serializes_same_path(tmp_path):
    path = tmp_path / "config.toml"
    entered: list[str] = []

    def nested_attempt():
        with private_path_lock(path, timeout_seconds=0, label="config lock"):
            entered.append("nested")

    with private_path_lock(path, label="config lock"):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(nested_attempt)
            with pytest.raises(TimeoutError, match="already in use"):
                future.result()

    with private_path_lock(path, label="config lock"):
        entered.append("after")
    assert entered == ["after"]


def test_release_shared_lock_blocks_child_release_exclusive(tmp_path):
    assert child_lock_attempt(
        tmp_path,
        held=("shared", "shared"),
        requested=("exclusive", "shared"),
    ) == "busy"


def test_current_shared_lock_blocks_child_current_exclusive(tmp_path):
    assert child_lock_attempt(
        tmp_path,
        held=("shared", "shared"),
        requested=("shared", "exclusive"),
    ) == "busy"


def test_same_target_lock_upgrade_and_downgrade_are_rejected(tmp_path):
    from codex_usage.integration_evidence import IntegrationBusy, evidence_lock_set

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    _create_evidence_lock_inodes(state_home)
    with evidence_lock_set(
        state_home=state_home,
        release_mode="shared",
        current_mode="shared",
        timeout_seconds=0,
        create=False,
    ):
        with pytest.raises(IntegrationBusy):
            with evidence_lock_set(
                state_home=state_home,
                release_mode="exclusive",
                current_mode="shared",
                timeout_seconds=0,
                create=False,
            ):
                pass
    with evidence_lock_set(
        state_home=state_home,
        release_mode="exclusive",
        current_mode="exclusive",
        timeout_seconds=0,
        create=False,
    ):
        with pytest.raises(IntegrationBusy):
            with evidence_lock_set(
                state_home=state_home,
                release_mode="shared",
                current_mode="exclusive",
                timeout_seconds=0,
                create=False,
            ):
                pass


def test_evidence_lock_set_rejects_current_before_release(tmp_path):
    from codex_usage import integration_evidence

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    _create_evidence_lock_inodes(state_home)
    integration_evidence._EVIDENCE_LOCK_STATE.held = {
        "current": ("shared", 1),
    }
    try:
        with pytest.raises(integration_evidence.IntegrationBusy):
            with integration_evidence.evidence_lock_set(
                state_home=state_home,
                release_mode="shared",
                current_mode="shared",
                timeout_seconds=0,
                create=False,
            ):
                pass
    finally:
        integration_evidence._EVIDENCE_LOCK_STATE.held = {}


def test_runtime_missing_lock_inode_is_unavailable(tmp_path):
    from codex_usage.integration_evidence import (
        IntegrationEvidenceUnavailable,
        evidence_lock_set,
    )

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    integration = state_home / "codex-usage" / "integration"
    integration.mkdir(mode=0o700, parents=True)
    integration.parent.chmod(0o700)
    ensure_private_directory(
        private_io._private_lock_root(),
        label="test evidence lock root",
    )
    with pytest.raises(IntegrationEvidenceUnavailable):
        with evidence_lock_set(
            state_home=state_home,
            release_mode="shared",
            current_mode="shared",
            timeout_seconds=0,
            create=False,
        ):
            pass


def test_bootstrap_evidence_lock_inodes_rejects_0755_contaminated_root_before_chmod(
    tmp_path,
    monkeypatch,
):
    """Would fail if evidence bootstrap chmoded an unsafe root before scanning."""
    from codex_usage import integration_evidence

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    integration = state_home / "codex-usage" / "integration"
    integration.mkdir(mode=0o700, parents=True)
    integration.parent.chmod(0o700)
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o755)
    lock_root.chmod(0o755)
    residue = lock_root / f"{_canonical_lock_name(8)}.moved"
    _touch_private_lock(residue)
    root_before = _lock_metadata(lock_root)
    residue_before = _lock_metadata(residue)
    lock_names = (
        integration_evidence._evidence_lock_name(integration / "producer-install"),
        integration_evidence._evidence_lock_name(integration / "current.json"),
    )
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)

    with pytest.raises(integration_evidence.IntegrationEvidenceInvalid):
        integration_evidence.bootstrap_evidence_lock_inodes(state_home=state_home)

    assert _lock_metadata(lock_root) == root_before
    assert _lock_metadata(residue) == residue_before
    assert stat.S_IMODE(lock_root.stat().st_mode) == 0o755
    assert all(not (lock_root / name).exists() for name in lock_names)


def test_evidence_lock_set_create_rejects_0700_contaminated_root_without_hash_drift(
    tmp_path,
    monkeypatch,
):
    """Would fail if evidence create=True touched a valid root before fail-closed scan."""
    from codex_usage import integration_evidence

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    integration = state_home / "codex-usage" / "integration"
    integration.mkdir(mode=0o700, parents=True)
    integration.parent.chmod(0o700)
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    canonical = lock_root / _canonical_lock_name(9)
    moved = lock_root / f"{canonical.name}.moved"
    _touch_private_lock(canonical)
    _touch_private_lock(moved)
    report_before = private_io.scan_private_lock_namespace(lock_root)
    approval_before = private_io.private_lock_namespace_approval_hash(report_before)
    root_before = _lock_metadata(lock_root)
    moved_before = _lock_metadata(moved)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)

    with pytest.raises(integration_evidence.IntegrationEvidenceInvalid):
        with integration_evidence.evidence_lock_set(
            state_home=state_home,
            release_mode="shared",
            current_mode="shared",
            timeout_seconds=0,
            create=True,
        ):
            pass

    report_after = private_io.scan_private_lock_namespace(lock_root)
    assert report_after == report_before
    assert private_io.private_lock_namespace_approval_hash(report_after) == approval_before
    assert _lock_metadata(lock_root) == root_before
    assert _lock_metadata(moved) == moved_before


def test_evidence_lock_set_create_restarts_readonly_after_raced_root_create(
    tmp_path,
    monkeypatch,
):
    """Would fail if evidence create=True chmoded a root that appeared after ENOENT."""
    from codex_usage import integration_evidence

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    integration = state_home / "codex-usage" / "integration"
    integration.mkdir(mode=0o700, parents=True)
    integration.parent.chmod(0o700)
    lock_root = tmp_path / "lock-root"
    canonical = lock_root / _canonical_lock_name(11)
    moved = lock_root / f"{canonical.name}.moved"
    original_open = private_io._open_existing_private_lock_root
    original_chmod = private_io._chmod_private_directory
    raced = False
    chmod_targets: list[Path] = []

    def create_contaminated_root_then_report_missing(root: Path, **kwargs):
        nonlocal raced
        if root == lock_root and not raced:
            raced = True
            lock_root.mkdir(mode=0o755)
            lock_root.chmod(0o755)
            _touch_private_lock(canonical)
            _touch_private_lock(moved)
            raise FileNotFoundError(root)
        return original_open(root, **kwargs)

    def observe_chmod(path: Path, *, label: str) -> None:
        if path == lock_root:
            chmod_targets.append(path)
        original_chmod(path, label=label)

    monkeypatch.setattr(
        private_io,
        "_open_existing_private_lock_root",
        create_contaminated_root_then_report_missing,
    )
    monkeypatch.setattr(private_io, "_chmod_private_directory", observe_chmod)
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)

    with pytest.raises(integration_evidence.IntegrationEvidenceInvalid):
        with integration_evidence.evidence_lock_set(
            state_home=state_home,
            release_mode="shared",
            current_mode="shared",
            timeout_seconds=0,
            create=True,
        ):
            pass

    assert raced
    assert chmod_targets == []
    assert stat.S_IMODE(lock_root.lstat().st_mode) == 0o755
    assert moved.exists()


def test_bootstrap_evidence_lock_inodes_creates_missing_clean_namespace(
    tmp_path,
    monkeypatch,
):
    """Would fail if read-only preflight blocked safe evidence lock-root creation."""
    from codex_usage import integration_evidence

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    integration = state_home / "codex-usage" / "integration"
    integration.mkdir(mode=0o700, parents=True)
    integration.parent.chmod(0o700)
    lock_root = tmp_path / "lock-root"
    lock_names = (
        integration_evidence._evidence_lock_name(integration / "producer-install"),
        integration_evidence._evidence_lock_name(integration / "current.json"),
    )
    monkeypatch.setattr(private_io, "_private_lock_root", lambda: lock_root)

    integration_evidence.bootstrap_evidence_lock_inodes(state_home=state_home)

    assert stat.S_IMODE(lock_root.stat().st_mode) == 0o700
    for name in lock_names:
        lock = lock_root / name
        assert lock.is_file()
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_evidence_lock_set_rejects_missing_integration_parent(tmp_path):
    from codex_usage.integration_evidence import (
        IntegrationEvidenceUnavailable,
        evidence_lock_set,
    )

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    with pytest.raises(IntegrationEvidenceUnavailable):
        with evidence_lock_set(
            state_home=state_home,
            release_mode="shared",
            current_mode="shared",
            timeout_seconds=0,
            create=False,
        ):
            pass


def test_partial_evidence_lock_failure_does_not_poison_thread_state(tmp_path):
    from codex_usage import integration_evidence

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    lock_root = private_io._private_lock_root()
    ensure_private_directory(lock_root, label="test evidence lock root")
    integration = state_home / "codex-usage" / "integration"
    for path in (integration.parent, integration):
        path.mkdir(mode=0o700)
    release_target = integration / "producer-install"
    release_lock = lock_root / integration_evidence._evidence_lock_name(release_target)
    release_fd = os.open(
        release_lock,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    os.close(release_fd)
    with pytest.raises(integration_evidence.IntegrationEvidenceUnavailable):
        with integration_evidence.evidence_lock_set(
            state_home=state_home,
            release_mode="shared",
            current_mode="shared",
            timeout_seconds=0,
            create=False,
        ):
            pass
    current_target = integration / "current.json"
    current_lock = lock_root / integration_evidence._evidence_lock_name(current_target)
    current_fd = os.open(
        current_lock,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    os.close(current_fd)
    with integration_evidence.evidence_lock_set(
        state_home=state_home,
        release_mode="shared",
        current_mode="shared",
        timeout_seconds=0,
        create=False,
    ):
        pass


def test_same_mode_nested_different_state_home_acquires_distinct_child_locks(
    tmp_path,
):
    from codex_usage.integration_evidence import evidence_lock_set

    first_state_home = tmp_path / "first-state"
    second_state_home = tmp_path / "second-state"
    for state_home in (first_state_home, second_state_home):
        state_home.mkdir(mode=0o700)
        _create_evidence_lock_inodes(state_home)

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    result = context.Queue()
    process = context.Process(
        target=_evidence_lock_child,
        args=(
            str(second_state_home),
            "exclusive",
            "exclusive",
            ready,
            release,
            result,
        ),
    )
    try:
        with evidence_lock_set(
            state_home=first_state_home,
            release_mode="shared",
            current_mode="shared",
            timeout_seconds=0,
            create=False,
        ):
            with evidence_lock_set(
                state_home=second_state_home,
                release_mode="shared",
                current_mode="shared",
                timeout_seconds=0,
                create=False,
            ):
                process.start()
                assert ready.wait(10)
                child_result = result.get(timeout=10)
        release.set()
        process.join(10)
        assert process.exitcode == 0
        assert child_result == "busy"
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(10)


def test_lock_entry_replacement_after_flock_fails_before_independent_domain(
    tmp_path, monkeypatch
):
    from codex_usage import integration_evidence

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    _create_evidence_lock_inodes(state_home)
    lock_root = private_io._private_lock_root()
    release_target = (
        state_home / "codex-usage" / "integration" / "producer-install"
    )
    release_lock = lock_root / integration_evidence._evidence_lock_name(
        release_target
    )
    old_lock = lock_root.parent / ".replaced-release-old"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    result = context.Queue()
    process = context.Process(
        target=_evidence_lock_child_holds,
        args=(
            str(state_home),
            "exclusive",
            "exclusive",
            ready,
            release,
            result,
        ),
    )
    original_acquire = integration_evidence._acquire_lock
    replaced = False
    child_result = None

    def acquire_then_replace(fd, *, mode, deadline):
        nonlocal replaced, child_result
        original_acquire(fd, mode=mode, deadline=deadline)
        if replaced:
            return
        replaced = True
        os.rename(release_lock, old_lock)
        replacement_fd = os.open(
            release_lock,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.close(replacement_fd)
        process.start()
        assert ready.wait(10)
        child_result = result.get(timeout=10)

    monkeypatch.setattr(
        integration_evidence,
        "_acquire_lock",
        acquire_then_replace,
    )
    entered = False
    try:
        with pytest.raises(integration_evidence.IntegrationEvidenceInvalid):
            with integration_evidence.evidence_lock_set(
                state_home=state_home,
                release_mode="exclusive",
                current_mode="exclusive",
                timeout_seconds=0,
                create=False,
            ):
                entered = True
    finally:
        release.set()
        if process.pid is not None:
            process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
    assert not entered
    assert child_result == "acquired"
    assert process.exitcode == 0
    with integration_evidence.evidence_lock_set(
        state_home=state_home,
        release_mode="exclusive",
        current_mode="exclusive",
        timeout_seconds=0,
        create=False,
    ):
        pass


def test_lock_root_replacement_after_flock_fails_before_independent_domain(
    tmp_path, monkeypatch
):
    from codex_usage import integration_evidence

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    _create_evidence_lock_inodes(state_home)
    lock_root = private_io._private_lock_root()
    old_root = lock_root.with_name(f"{lock_root.name}-old")
    integration = state_home / "codex-usage" / "integration"
    lock_names = (
        integration_evidence._evidence_lock_name(integration / "producer-install"),
        integration_evidence._evidence_lock_name(integration / "current.json"),
    )
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    result = context.Queue()
    process = context.Process(
        target=_evidence_lock_child_holds,
        args=(
            str(state_home),
            "exclusive",
            "exclusive",
            ready,
            release,
            result,
        ),
    )
    original_acquire = integration_evidence._acquire_lock
    acquisitions = 0
    child_result = None

    def acquire_then_replace_root(fd, *, mode, deadline):
        nonlocal acquisitions, child_result
        original_acquire(fd, mode=mode, deadline=deadline)
        acquisitions += 1
        if acquisitions != 2:
            return
        os.rename(lock_root, old_root)
        lock_root.mkdir(mode=0o700)
        for lock_name in lock_names:
            replacement_fd = os.open(
                lock_root / lock_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(replacement_fd)
        process.start()
        assert ready.wait(10)
        child_result = result.get(timeout=10)

    monkeypatch.setattr(
        integration_evidence,
        "_acquire_lock",
        acquire_then_replace_root,
    )
    entered = False
    try:
        with pytest.raises(integration_evidence.IntegrationEvidenceInvalid):
            with integration_evidence.evidence_lock_set(
                state_home=state_home,
                release_mode="exclusive",
                current_mode="exclusive",
                timeout_seconds=0,
                create=False,
            ):
                entered = True
    finally:
        release.set()
        if process.pid is not None:
            process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
    assert not entered
    assert child_result == "acquired"
    assert process.exitcode == 0
    with integration_evidence.evidence_lock_set(
        state_home=state_home,
        release_mode="exclusive",
        current_mode="exclusive",
        timeout_seconds=0,
        create=False,
    ):
        pass


def test_nested_same_logical_target_rejects_replaced_lock_inodes(tmp_path):
    from codex_usage import integration_evidence

    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    _create_evidence_lock_inodes(state_home)
    lock_root = private_io._private_lock_root()
    integration = state_home / "codex-usage" / "integration"
    lock_names = (
        integration_evidence._evidence_lock_name(integration / "producer-install"),
        integration_evidence._evidence_lock_name(integration / "current.json"),
    )

    with integration_evidence.evidence_lock_set(
        state_home=state_home,
        release_mode="exclusive",
        current_mode="exclusive",
        timeout_seconds=0,
        create=False,
    ):
        for lock_name in lock_names:
            lock_path = lock_root / lock_name
            os.rename(lock_path, lock_root.parent / f".{lock_name}.old")
            replacement_fd = os.open(
                lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(replacement_fd)
        entered = False
        with pytest.raises(integration_evidence.IntegrationEvidenceInvalid):
            with integration_evidence.evidence_lock_set(
                state_home=state_home,
                release_mode="exclusive",
                current_mode="exclusive",
                timeout_seconds=0,
                create=False,
            ):
                entered = True
        assert not entered

    with integration_evidence.evidence_lock_set(
        state_home=state_home,
        release_mode="exclusive",
        current_mode="exclusive",
        timeout_seconds=0,
        create=False,
    ):
        pass
