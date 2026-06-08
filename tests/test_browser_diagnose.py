from __future__ import annotations

import json

import pytest

from codex_usage.browser import (
    _detect_page_state,
    _diagnose_auth_json,
    _redact_url,
    _save_probe_payloads,
)
from codex_usage.extractor import JsonCandidate
from codex_usage.models import Account


def test_diagnose_auth_json_redacts_token_values(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": "sk-secret",
                "auth_mode": "chatgpt",
                "last_refresh": "2026-06-08T00:00:00Z",
                "tokens": {
                    "access_token": "access-secret",
                    "id_token": "id-secret",
                    "refresh_token": "refresh-secret",
                    "account_id": "acct-secret",
                },
            }
        ),
        encoding="utf-8",
    )

    result = _diagnose_auth_json(auth_path)
    serialized = json.dumps(result)

    assert result["exists"] is True
    assert result["auth_mode"] == "chatgpt"
    assert result["has_openai_api_key"] is True
    assert result["token_fields"] == ["access_token", "account_id", "id_token", "refresh_token"]
    assert result["token_presence"] == {
        "access_token": True,
        "id_token": True,
        "refresh_token": True,
        "account_id": True,
    }
    assert result["has_browser_storage_state"] is False
    assert "access-secret" not in serialized
    assert "refresh-secret" not in serialized
    assert "sk-secret" not in serialized


def test_diagnose_detects_cloudflare_challenge_and_redacts_url():
    challenge_url = "https://chatgpt.com/cdn-cgi/challenge-platform/h/g/flow/secret-token"

    assert _redact_url(challenge_url) == "https://chatgpt.com/cdn-cgi/challenge-platform/..."
    assert (
        _detect_page_state(
            "https://chatgpt.com/codex/cloud/settings/analytics",
            "Just a moment...",
            "",
            [{"status": 200, "url": challenge_url}],
        )
        == "cloudflare"
    )


def test_save_probe_payloads_rejects_symlink_save_dir(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    save_link = tmp_path / "probe"
    save_link.symlink_to(outside, target_is_directory=True)
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")

    with pytest.raises(ValueError, match="probe save directory"):
        _save_probe_payloads(
            save_link,
            account,
            [JsonCandidate(url="https://chatgpt.com/backend-api/wham/usage", payload={})],
            "visible body",
        )

    assert not (outside / "privat-01.json").exists()
    assert not (outside / "privat-body.txt").exists()


def test_save_probe_payloads_rejects_symlink_output_file(tmp_path):
    save_dir = tmp_path / "probe"
    save_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    (save_dir / "privat-body.txt").symlink_to(outside)
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")

    with pytest.raises(ValueError, match="probe output path"):
        _save_probe_payloads(save_dir, account, [], "visible body")

    assert outside.read_text(encoding="utf-8") == "keep"
