from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    monkeypatch.setattr(private_io.os, "getuid", lambda: 2**31 - 1)

    with pytest.raises(ValueError):
        ensure_private_directory(target, label="private directory")


def test_read_private_text_rejects_foreign_owner(tmp_path, monkeypatch):
    path = tmp_path / "value.json"
    path.write_text("secret", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(private_io.os, "getuid", lambda: 2**31 - 1)

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
    monkeypatch.setattr(private_io.os, "getuid", lambda: 2**31 - 1)

    with pytest.raises(ValueError):
        write_private_text(path, "new", label="value")


def test_private_path_lock_rejects_foreign_owner(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    lock_path = path.with_name(path.name + ".lock")
    lock_path.write_text("", encoding="utf-8")
    lock_path.chmod(0o600)
    monkeypatch.setattr(private_io.os, "getuid", lambda: 2**31 - 1)

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


@pytest.mark.parametrize("mode", [0o640, True, "600", -1])
def test_write_private_text_rejects_non_private_mode(tmp_path, mode):
    path = tmp_path / "value.json"

    with pytest.raises(ValueError, match="mode must be private"):
        write_private_text(path, "secret", label="value", mode=mode)  # type: ignore[arg-type]

    assert not path.exists()


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
