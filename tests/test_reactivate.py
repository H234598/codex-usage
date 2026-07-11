from __future__ import annotations

import base64
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codex_usage import oauth_browser
from codex_usage.cli import main
from codex_usage.config import AppConfig, save_config
from codex_usage.models import Account
from codex_usage.reactivate import (
    OAUTH_PROFILE_MARKER,
    ReactivationError,
    _validate_refreshed_auth,
    reactivate_account,
)


def _jwt_with_exp(expiry: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).rstrip(
        b"="
    ).decode()
    return f"{header}.{payload}.signature"


def _executable(path: Path) -> str:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return str(path)


def test_reactivate_account_uses_isolated_codex_home(tmp_path, monkeypatch):
    auth_home = tmp_path / "agent-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    profile_root = tmp_path / "profiles" / "work"
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(profile_root),
        auth_json_path=str(auth_path),
    )
    codex = _executable(tmp_path / "codex")
    helper = _executable(tmp_path / "codex-usage-browser")
    browser = _executable(tmp_path / "vivaldi-stable")
    monkeypatch.setattr(
        "codex_usage.reactivate._select_browser",
        lambda requested: ("vivaldi", browser),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        expiry = int((datetime.now(UTC) + timedelta(days=10)).timestamp())
        auth_path.write_text(
            json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": _jwt_with_exp(expiry)}}),
            encoding="utf-8",
        )
        auth_path.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("codex_usage.reactivate.subprocess.run", fake_run)

    result = reactivate_account(
        account,
        codex_command=codex,
        browser_helper=helper,
    )

    assert result["ok"] is True
    assert result["browser"] == "vivaldi"
    assert captured["argv"] == [codex, "login"]
    assert captured["env"]["CODEX_HOME"] == str(auth_home)
    assert captured["env"]["BROWSER"] == helper
    assert captured["env"]["CODEX_USAGE_BROWSER_PROFILE"] == str(
        profile_root / "oauth" / "vivaldi"
    )
    assert "OPENAI_API_KEY" not in captured["env"]
    assert (profile_root / "oauth" / "vivaldi" / OAUTH_PROFILE_MARKER).is_file()


def test_reactivate_rejects_different_account_and_restores_auth_json(tmp_path, monkeypatch):
    auth_home = tmp_path / "agent-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    expiry = int((datetime.now(UTC) + timedelta(days=10)).timestamp())
    old_raw = json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "account_id": "account-old",
                "access_token": _jwt_with_exp(expiry),
            },
        }
    )
    auth_path.write_text(old_raw, encoding="utf-8")
    auth_path.chmod(0o600)
    profile_root = tmp_path / "profiles" / "work"
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(profile_root),
        auth_json_path=str(auth_path),
    )
    codex = _executable(tmp_path / "codex")
    helper = _executable(tmp_path / "codex-usage-browser")
    browser = _executable(tmp_path / "vivaldi-stable")
    monkeypatch.setattr(
        "codex_usage.reactivate._select_browser",
        lambda requested: ("vivaldi", browser),
    )

    def fake_run(argv, **kwargs):
        auth_path.write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {
                        "account_id": "account-new",
                        "access_token": _jwt_with_exp(expiry),
                    },
                }
            ),
            encoding="utf-8",
        )
        auth_path.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("codex_usage.reactivate.subprocess.run", fake_run)

    with pytest.raises(ReactivationError, match="different account"):
        reactivate_account(account, codex_command=codex, browser_helper=helper)

    assert auth_path.read_text(encoding="utf-8") == old_raw
    assert auth_path.stat().st_mode & 0o777 == 0o600


def test_reactivate_login_failure_restores_auth_json(tmp_path, monkeypatch):
    auth_home = tmp_path / "agent-home"
    auth_home.mkdir()
    auth_path = auth_home / "auth.json"
    expiry = int((datetime.now(UTC) + timedelta(days=10)).timestamp())
    old_raw = json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "account_id": "account-old",
                "access_token": _jwt_with_exp(expiry),
            },
        }
    )
    auth_path.write_text(old_raw, encoding="utf-8")
    auth_path.chmod(0o600)
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profiles" / "work"),
        auth_json_path=str(auth_path),
    )
    codex = _executable(tmp_path / "codex")
    helper = _executable(tmp_path / "codex-usage-browser")
    browser = _executable(tmp_path / "vivaldi-stable")
    monkeypatch.setattr(
        "codex_usage.reactivate._select_browser",
        lambda requested: ("vivaldi", browser),
    )

    def fake_run(argv, **kwargs):
        auth_path.write_text('{"partial": true}\n', encoding="utf-8")
        auth_path.chmod(0o600)
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr("codex_usage.reactivate.subprocess.run", fake_run)

    with pytest.raises(ReactivationError, match="exit code 1"):
        reactivate_account(account, codex_command=codex, browser_helper=helper)

    assert auth_path.read_text(encoding="utf-8") == old_raw
    assert auth_path.stat().st_mode & 0o777 == 0o600


def test_validate_refreshed_auth_rejects_empty_access_token(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": ""}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    with pytest.raises(ReactivationError, match=r"without a valid auth\.json"):
        _validate_refreshed_auth(auth_path)


def test_validate_refreshed_auth_rejects_malformed_access_token(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "bad token"}}),
        encoding="utf-8",
    )
    auth_path.chmod(0o600)

    with pytest.raises(ReactivationError, match=r"without a valid auth\.json"):
        _validate_refreshed_auth(auth_path)


def test_oauth_browser_launches_vivaldi_with_isolated_profile(tmp_path, monkeypatch):
    executable = Path(_executable(tmp_path / "vivaldi-stable"))
    profile = tmp_path / "oauth-profile"
    profile.mkdir()
    (profile / OAUTH_PROFILE_MARKER).write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_USAGE_BROWSER_EXECUTABLE", str(executable))
    monkeypatch.setenv("CODEX_USAGE_BROWSER_KIND", "vivaldi")
    monkeypatch.setenv("CODEX_USAGE_BROWSER_PROFILE", str(profile))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "must-not-leak"))

    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return object()

    monkeypatch.setattr(oauth_browser.subprocess, "Popen", fake_popen)

    assert oauth_browser.main(["https://auth.openai.com/oauth/authorize?client_id=test"]) == 0
    assert captured["command"][0] == str(executable)
    assert f"--user-data-dir={profile}" in captured["command"]
    assert captured["command"][-1].startswith("https://auth.openai.com/")
    assert "CODEX_HOME" not in captured["env"]


def test_oauth_browser_rejects_non_openai_url(tmp_path, monkeypatch, capsys):
    assert oauth_browser.main(["https://example.com/login"]) == 1
    assert "refusing non-OpenAI" in capsys.readouterr().err


def test_reactivate_rejects_symlink_in_profile_path(tmp_path):
    auth_home = tmp_path / "agent-home"
    auth_home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    profile_link = tmp_path / "profiles"
    profile_link.symlink_to(outside, target_is_directory=True)
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(profile_link / "work"),
        auth_json_path=str(auth_home / "auth.json"),
    )

    with pytest.raises(ReactivationError, match="must not contain symlinks"):
        reactivate_account(account)

    assert not (outside / "work").exists()


def test_reactivate_rejects_symlink_in_auth_parent(tmp_path):
    real_auth_home = tmp_path / "real-agent-home"
    real_auth_home.mkdir()
    auth_home = tmp_path / "agent-home"
    auth_home.symlink_to(real_auth_home, target_is_directory=True)
    profile_root = tmp_path / "profiles" / "work"
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(profile_root),
        auth_json_path=str(auth_home / "auth.json"),
    )

    with pytest.raises(ReactivationError, match="must not contain symlinks"):
        reactivate_account(account)

    assert not profile_root.exists()


def test_reactivate_cli_renders_json(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(tmp_path / "agent" / "auth.json"),
    )
    save_config(AppConfig(accounts=(account,)), config_path)
    monkeypatch.setattr(
        "codex_usage.cli.reactivate_account",
        lambda selected, browser: {
            "ok": True,
            "account": selected.id,
            "label": selected.label,
            "browser": browser,
            "auth_updated": True,
            "auth_access_expires_at": None,
        },
    )

    assert (
        main(
            [
                "--config",
                str(config_path),
                "reactivate",
                "work",
                "--browser",
                "vivaldi",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": True,
        "account": "work",
        "label": "Work",
        "browser": "vivaldi",
        "auth_updated": True,
        "auth_access_expires_at": None,
    }
