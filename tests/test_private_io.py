from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from codex_usage.private_io import private_path_lock, write_private_text


def test_write_private_text_replaces_atomically_and_keeps_mode(tmp_path):
    path = tmp_path / "value.json"
    path.write_text("old", encoding="utf-8")

    write_private_text(path, "new", label="value")

    assert path.read_text(encoding="utf-8") == "new"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert list(tmp_path.glob(".value.json.tmp-*")) == []


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
