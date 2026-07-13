import pytest

from codex_usage.extractor import JsonCandidate
from codex_usage.identity import (
    backend_identity_from_candidates,
    select_identity_consistent_candidates,
)


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


def test_latest_partial_usage_identity_does_not_restore_older_account_id():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "old-user", "account_id": "old-account"},
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "new-user"},
        ),
    ]

    assert backend_identity_from_candidates(candidates) == ("new-user", None)


def test_select_identity_consistent_candidates_does_not_mix_accounts():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "user_id": "user-a",
                "account_id": "account-a",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 3,
                        "limit_window_seconds": 18_000,
                    }
                },
            },
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "user_id": "user-b",
                "account_id": "account-b",
                "rate_limit": {
                    "secondary_window": {
                        "used_percent": 45,
                        "limit_window_seconds": 604_800,
                    }
                },
            },
        ),
    ]

    selected = select_identity_consistent_candidates(
        candidates,
        auth_user_id="user-a",
        auth_account_id="account-a",
    )

    assert selected == [candidates[0]]


def test_select_identity_consistent_candidates_drops_ambiguous_partial_identity():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "user_id": "shared-user",
                "account_id": "account-a",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 3,
                        "limit_window_seconds": 18_000,
                    }
                },
            },
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "user_id": "shared-user",
                "account_id": "account-b",
                "rate_limit": {
                    "secondary_window": {
                        "used_percent": 45,
                        "limit_window_seconds": 604_800,
                    }
                },
            },
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "user_id": "shared-user",
                "rate_limit": {
                    "secondary_window": {
                        "used_percent": 99,
                        "limit_window_seconds": 604_800,
                    }
                },
            },
        ),
    ]

    selected = select_identity_consistent_candidates(
        candidates,
        auth_user_id="shared-user",
        auth_account_id="account-a",
    )

    assert selected == [candidates[0]]


def test_select_identity_consistent_candidates_drops_partial_user_when_other_account_lacks_user():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "user_id": "shared-user",
                "account_id": "account-a",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 3,
                        "limit_window_seconds": 18_000,
                    }
                },
            },
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "account_id": "account-b",
                "rate_limit": {
                    "secondary_window": {
                        "used_percent": 45,
                        "limit_window_seconds": 604_800,
                    }
                },
            },
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "user_id": "shared-user",
                "rate_limit": {
                    "secondary_window": {
                        "used_percent": 99,
                        "limit_window_seconds": 604_800,
                    }
                },
            },
        ),
    ]

    selected = select_identity_consistent_candidates(
        candidates,
        auth_user_id="shared-user",
        auth_account_id="account-a",
    )

    assert selected == [candidates[0]]


def test_select_identity_consistent_candidates_rejects_unknown_account():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={"user_id": "user-a", "account_id": "account-a"},
    )

    with pytest.raises(ValueError, match="different account"):
        select_identity_consistent_candidates(
            [candidate],
            auth_user_id="user-b",
            auth_account_id="account-b",
        )
