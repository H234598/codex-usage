from __future__ import annotations

import json

from codex_usage.browser import _detect_page_state, _diagnose_auth_json, _redact_url


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
