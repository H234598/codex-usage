from __future__ import annotations

import multiprocessing
import os
import pwd
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

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
    original_mkdir = Path.mkdir

    def create_then_report_exists(path, *args, **kwargs):
        original_mkdir(path, *args, **kwargs)
        raise FileExistsError(path)

    monkeypatch.setattr(Path, "mkdir", create_then_report_exists)

    ensure_private_directory(
        target,
        label="private directory",
        created_paths=created_paths,
    )

    assert target.is_dir()
    assert created_paths == []


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


def test_ensure_private_directory_binds_mode_change_to_directory(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    original_chmod = Path.chmod

    def replace_target_before_path_chmod(path, mode):
        if path == target:
            target.rmdir()
            target.symlink_to(outside, target_is_directory=True)
        return original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", replace_target_before_path_chmod)
    ensure_private_directory(target, label="private directory")

    assert target.is_dir() and not target.is_symlink()
    assert target.stat().st_mode & 0o777 == 0o700
    assert outside.stat().st_mode & 0o777 == 0o755


def test_ensure_private_directory_fails_when_descriptor_chmod_fails(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir(mode=0o755)

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

    def track_open(file, flags, mode=0o777):
        fd = original_open(file, flags, mode)
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
    fstat_calls = 0

    def reject_temporary(fd):
        nonlocal fstat_calls
        fstat_calls += 1
        if fstat_calls <= 2:
            return original_fstat(fd)
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

    with pytest.raises(ValueError, match="must be a regular file"):
        with private_path_lock(path, label="config lock"):
            pass


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
        "--dir",
        str(production_lock_root),
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
    old_lock = lock_root / ".replaced-release-old"
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
            os.rename(lock_path, lock_root / f".{lock_name}.old")
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
