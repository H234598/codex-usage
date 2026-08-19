from __future__ import annotations

from pathlib import Path

import pytest

import codex_usage.terminal as terminal_module
from codex_usage.models import Account
from codex_usage.terminal import TerminalError, start_account_terminal


def _account(tmp_path: Path) -> Account:
    profile = tmp_path / "profile"
    (profile / "codex-home").mkdir(parents=True)
    (profile / "codex-home" / "auth.json").write_text("{}\n", encoding="utf-8")
    return Account(id="work", label="Work", profile_dir=str(profile))


def test_start_account_terminal_uses_canonical_auth_home_and_cwd(monkeypatch, tmp_path):
    account = _account(tmp_path)
    captured = {}

    monkeypatch.setattr(
        terminal_module.shutil,
        "which",
        lambda candidate: f"/usr/bin/{candidate}",
    )
    monkeypatch.setattr(
        terminal_module.subprocess,
        "Popen",
        lambda argv, **kwargs: captured.update(argv=argv, kwargs=kwargs),
    )

    result = start_account_terminal(account)

    profile = Path(account.profile_dir)
    assert result["ok"] is True
    assert captured["argv"] == [
        "/usr/bin/ghostty",
        "--working-directory",
        str(profile),
        "-e",
        "/usr/bin/codex",
    ]
    assert captured["kwargs"]["cwd"] == str(profile)
    assert captured["kwargs"]["env"]["CODEX_HOME"] == str(profile / "codex-home")


def test_start_account_terminal_rejects_missing_canonical_auth(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    account = Account(id="work", label="Work", profile_dir=str(profile))

    with pytest.raises(TerminalError, match=r"canonical auth\.json is missing"):
        start_account_terminal(account, terminal="ghostty", codex_command="codex")


def test_terminal_candidates_prefer_ghostty(monkeypatch):
    seen = []

    def fake_which(candidate):
        seen.append(candidate)
        return "/usr/bin/ghostty" if candidate == "ghostty" else None

    monkeypatch.setattr(terminal_module.shutil, "which", fake_which)

    assert terminal_module._resolve_terminal() == ("/usr/bin/ghostty", "ghostty")
    assert seen[0] == "ghostty"
