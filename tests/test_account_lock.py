from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from codex_usage.account_lock import AccountLockError, account_lock


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


def test_account_lock_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    with pytest.raises(AccountLockError, match="invalid account id"):
        with account_lock("../outside"):
            pass

    assert not (tmp_path / "outside.lock").exists()
