from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


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
