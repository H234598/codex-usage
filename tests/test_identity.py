from codex_usage.extractor import JsonCandidate
from codex_usage.identity import backend_identity_from_candidates


def test_usage_endpoint_identity_wins_over_settings_response_order():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/settings/user",
            payload={"user_id": "settings-user"},
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage/daily-token-usage-breakdown",
            payload={"user_id": "daily-user", "account_id": "daily-account"},
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "user_id": "usage-user",
                "account_id": "usage-account",
                "rate_limit": {},
            },
        ),
    ]

    assert backend_identity_from_candidates(candidates) == (
        "usage-user",
        "usage-account",
    )
