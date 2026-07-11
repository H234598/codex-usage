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


def test_identity_fields_are_not_combined_across_candidates():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "usage-user", "rate_limit": {}},
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage/daily-token-usage-breakdown",
            payload={"account_id": "other-account"},
        ),
    ]

    assert backend_identity_from_candidates(candidates) == ("usage-user", None)


def test_latest_equal_priority_usage_identity_wins():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "old-user", "account_id": "old-account"},
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "new-user", "account_id": "new-account"},
        ),
    ]

    assert backend_identity_from_candidates(candidates) == ("new-user", "new-account")
