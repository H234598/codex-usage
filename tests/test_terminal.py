from __future__ import annotations

from pathlib import Path

import pytest

import codex_usage.terminal as terminal_module
from codex_usage.models import Account
from codex_usage.terminal import TerminalError, start_account_terminal


def _account(tmp_path: Path) -> Account:
    profile = tmp_path / "profile"
    (profile / "codex-home").mkdir(parents=True)
    auth = profile / "codex-home" / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o600)
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
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-forward")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-forward")

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
    assert "OPENAI_API_KEY" not in captured["kwargs"]["env"]
    assert "CODEX_API_KEY" not in captured["kwargs"]["env"]


def test_start_account_terminal_rejects_non_account_input():
    with pytest.raises(TerminalError, match="account is invalid"):
        start_account_terminal(object())  # type: ignore[arg-type]


def test_start_account_terminal_rejects_missing_profile_directory(tmp_path):
    account = Account(id="work", label="Work", profile_dir=str(tmp_path / "profile"))

    with pytest.raises(TerminalError, match="profile directory does not exist"):
        start_account_terminal(account)


def test_start_account_terminal_maps_process_start_error(monkeypatch, tmp_path):
    account = _account(tmp_path)
    monkeypatch.setattr(terminal_module.shutil, "which", lambda candidate: f"/usr/bin/{candidate}")
    monkeypatch.setattr(
        terminal_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("terminal unavailable")),
    )

    with pytest.raises(TerminalError, match="could not start terminal"):
        start_account_terminal(account)


@pytest.mark.parametrize("account_id", [None, [], "../escape", "__all_accounts__"])
def test_start_account_terminal_rejects_invalid_account_id(tmp_path, account_id):
    account = Account(
        id=account_id,
        label="Work",
        profile_dir=str(tmp_path / "profile"),
    )

    with pytest.raises(TerminalError, match="account id is invalid"):
        start_account_terminal(account)


def test_start_account_terminal_rejects_missing_canonical_auth(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    account = Account(id="work", label="Work", profile_dir=str(profile))

    with pytest.raises(TerminalError, match=r"canonical auth\.json is missing"):
        start_account_terminal(account, terminal="ghostty", codex_command="codex")


def test_terminal_auth_validation_rejects_group_readable_file(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o640)

    with pytest.raises(TerminalError, match="private regular file"):
        terminal_module._validate_auth_json(auth)


def test_terminal_auth_validation_maps_inspection_error(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o600)
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _path: (_ for _ in ()).throw(OSError("auth disappeared")),
    )

    with pytest.raises(TerminalError, match="cannot be inspected"):
        terminal_module._validate_auth_json(auth)


def test_terminal_auth_validation_rejects_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("{}\n", encoding="utf-8")
    auth = tmp_path / "auth.json"
    auth.symlink_to(target)

    with pytest.raises(TerminalError, match="regular file"):
        terminal_module._validate_auth_json(auth)


def test_terminal_auth_validation_rejects_hardlink(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o600)
    auth_copy = tmp_path / "auth-copy.json"
    auth_copy.hardlink_to(auth)

    with pytest.raises(TerminalError, match="hard-linked"):
        terminal_module._validate_auth_json(auth)


def test_terminal_candidates_prefer_ghostty(monkeypatch):
    seen = []

    def fake_which(candidate):
        seen.append(candidate)
        return "/usr/bin/ghostty" if candidate == "ghostty" else None

    monkeypatch.setattr(terminal_module.shutil, "which", fake_which)

    assert terminal_module._resolve_terminal() == ("/usr/bin/ghostty", "ghostty")
    assert seen[0] == "ghostty"


def test_executable_resolver_reports_missing_command(monkeypatch):
    monkeypatch.setattr(terminal_module.shutil, "which", lambda _candidate: None)

    with pytest.raises(TerminalError, match="codex command was not found"):
        terminal_module._resolve_executable(None, "codex", label="codex command")


def test_terminal_resolver_skips_unavailable_candidates(monkeypatch):
    monkeypatch.setattr(terminal_module.shutil, "which", lambda _candidate: None)

    with pytest.raises(TerminalError, match="no supported terminal"):
        terminal_module._resolve_terminal("ghostty")


def test_terminal_resolver_strips_wrapper_suffix(monkeypatch):
    monkeypatch.setattr(
        terminal_module.shutil,
        "which",
        lambda _candidate: "/usr/bin/ghostty.wrapper",
    )

    assert terminal_module._resolve_terminal("ghostty") == (
        "/usr/bin/ghostty.wrapper",
        "ghostty",
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (
            "gnome-terminal",
            ["terminal", "--working-directory", "/tmp/profile", "--", "codex"],
        ),
        (
            "mate-terminal",
            ["terminal", "--working-directory", "/tmp/profile", "--", "codex"],
        ),
        ("konsole", ["terminal", "--workdir", "/tmp/profile", "-e", "codex"]),
        (
            "xfce4-terminal",
            [
                "terminal",
                "--working-directory",
                "/tmp/profile",
                "--command",
                "codex",
            ],
        ),
        ("ghostty", ["terminal", "--working-directory", "/tmp/profile", "-e", "codex"]),
        ("kitty", ["terminal", "--directory", "/tmp/profile", "codex"]),
        ("alacritty", ["terminal", "--working-directory", "/tmp/profile", "-e", "codex"]),
        ("wezterm", ["terminal", "start", "--cwd", "/tmp/profile", "--", "codex"]),
        ("foot", ["terminal", "--working-directory", "/tmp/profile", "codex"]),
        ("xterm", ["terminal", "-e", "codex"]),
    ],
)
def test_terminal_argv_uses_kind_specific_working_directory_flags(kind, expected):
    assert terminal_module._terminal_argv(
        "terminal",
        kind,
        profile_dir=Path("/tmp/profile"),
        codex="codex",
    ) == expected


@pytest.mark.parametrize("explicit", ["", [], {}, 0])
def test_executable_resolver_rejects_invalid_explicit_value(monkeypatch, explicit):
    monkeypatch.setattr(terminal_module.shutil, "which", lambda _: "/usr/bin/codex")

    with pytest.raises(TerminalError, match="codex command is invalid"):
        terminal_module._resolve_executable(explicit, "codex", label="codex command")


@pytest.mark.parametrize("explicit", ["", [], {}, 0])
def test_terminal_resolver_rejects_invalid_explicit_value(monkeypatch, explicit):
    monkeypatch.setattr(terminal_module.shutil, "which", lambda _: "/usr/bin/ghostty")

    with pytest.raises(TerminalError, match="no supported terminal"):
        terminal_module._resolve_terminal(explicit)
