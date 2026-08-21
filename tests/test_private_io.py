from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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


def test_ensure_private_directory_secures_all_new_path_components(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    existing.chmod(0o755)
    target = existing / "nested" / "private"

    ensure_private_directory(target, label="private directory")

    assert (target.stat().st_mode & 0o777) == 0o700
    assert (target.parent.stat().st_mode & 0o777) == 0o700
    assert (existing.stat().st_mode & 0o777) == 0o755


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
