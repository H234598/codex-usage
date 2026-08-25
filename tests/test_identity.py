import pytest

import codex_usage.identity as identity_module
from codex_usage.extractor import JsonCandidate
from codex_usage.identity import (
    backend_identity_from_candidates,
    backend_identity_from_payload,
    backend_plan_type_from_candidates,
    backend_plan_type_from_payload,
    select_identity_consistent_candidates,
)


class _ValueErrorIterator:
    def __iter__(self):
        raise ValueError("synthetic iterator failure")


class _RuntimeErrorIterator:
    def __iter__(self):
        raise RuntimeError("synthetic iterator failure")


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_id", []),
        ("account_id", 42),
        ("user_id", " "),
        ("account_id", "account\nforged"),
    ],
)
def test_backend_identity_rejects_malformed_identity_fields(field, value):
    with pytest.raises(ValueError, match=f"backend response {field} is invalid"):
        backend_identity_from_payload({field: value})


def test_backend_identity_rejects_string_subclass_hooks():
    class BrokenStr(str):
        def __len__(self):
            raise RuntimeError("synthetic identity value marker")

    with pytest.raises(ValueError, match="backend response user_id is invalid"):
        backend_identity_from_payload({"user_id": BrokenStr("user")})


@pytest.mark.parametrize(
    "value",
    [chr(codepoint) for codepoint in range(0x80, 0x8B) if codepoint != 0x85],
)
def test_identity_fields_reject_c1_control_characters(value):
    with pytest.raises(ValueError, match="backend response user_id is invalid"):
        backend_identity_from_payload({"user_id": value})
    with pytest.raises(ValueError, match="backend response plan_type is invalid"):
        backend_plan_type_from_payload({"plan_type": value})
    with pytest.raises(ValueError, match="auth_user_id is invalid"):
        select_identity_consistent_candidates(
            [], auth_user_id=value, auth_account_id=None
        )


def test_identity_payload_helpers_reject_non_mapping_values():
    assert backend_identity_from_payload([]) == (None, None)
    assert backend_plan_type_from_payload([]) is None


def test_identity_payload_helpers_reject_mapping_subclass_hooks():
    class BrokenDict(dict):
        def get(self, _key, _default=None):
            raise RuntimeError("synthetic identity payload marker")

    payload = BrokenDict(user_id="user")

    assert backend_identity_from_payload(payload) == (None, None)
    assert backend_plan_type_from_payload(payload) is None


def test_identity_candidate_priority_rejects_mapping_subclass_hooks():
    class BrokenDict(dict):
        def __contains__(self, _key):
            raise RuntimeError("synthetic identity priority marker")

    candidate = JsonCandidate(
        url="https://chatgpt.com/other",
        payload=BrokenDict(rate_limit={}),
    )

    assert backend_plan_type_from_candidates([candidate]) is None


def test_identity_helpers_skip_candidate_subclass_hooks():
    class BrokenCandidate(JsonCandidate):
        def __getattribute__(self, name):
            if name == "url":
                raise RuntimeError("synthetic identity candidate marker")
            return super().__getattribute__(name)

    candidate = BrokenCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={"user_id": "wrong-user"},
    )

    assert backend_identity_from_candidates([candidate]) == (None, None)


@pytest.mark.parametrize("value", [[], 42, " ", "plan\nforged"])
def test_backend_plan_type_rejects_malformed_values(value):
    with pytest.raises(ValueError, match="backend response plan_type is invalid"):
        backend_plan_type_from_payload({"plan_type": value})


def test_backend_plan_type_rejects_string_subclass_hooks():
    class BrokenStr(str):
        def __len__(self):
            raise RuntimeError("synthetic plan type marker")

    with pytest.raises(ValueError, match="backend response plan_type is invalid"):
        backend_plan_type_from_payload({"plan_type": BrokenStr("plus")})


def test_identity_helpers_skip_candidates_without_usable_urls():
    malformed = JsonCandidate(url=[], payload={"user_id": "wrong-user"})
    valid = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={"user_id": "valid-user", "plan_type": "plus"},
    )

    assert backend_identity_from_candidates([malformed, valid]) == ("valid-user", None)
    assert backend_plan_type_from_candidates([malformed, valid]) == "plus"
    assert select_identity_consistent_candidates(
        [malformed, valid], auth_user_id=None, auth_account_id=None
    ) == [valid]


def test_identity_helpers_skip_candidates_with_malformed_urls():
    malformed = JsonCandidate(url="http://[::1", payload={"user_id": "wrong-user"})
    valid = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={"user_id": "valid-user", "plan_type": "plus"},
    )

    assert backend_identity_from_candidates([malformed, valid]) == ("valid-user", None)
    assert backend_plan_type_from_candidates([malformed, valid]) == "plus"
    assert select_identity_consistent_candidates(
        [malformed, valid], auth_user_id=None, auth_account_id=None
    ) == [valid]


def test_backend_plan_type_skips_candidate_without_plan_type():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "valid-user"},
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/settings",
            payload={"plan_type": "plus"},
        ),
    ]

    assert backend_plan_type_from_candidates(candidates) == "plus"


def test_identity_selection_merges_compatible_duplicate_groups():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "user", "account_id": "account"},
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage/settings",
            payload={"user_id": "user", "account_id": "account"},
        ),
    ]

    assert select_identity_consistent_candidates(
        candidates,
        auth_user_id=None,
        auth_account_id=None,
    ) == candidates


def test_identity_selection_rejects_ambiguous_partial_without_auth():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "shared", "account_id": "account-a"},
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "shared", "account_id": "account-b"},
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage/settings",
            payload={"user_id": "shared"},
        ),
    ]

    with pytest.raises(ValueError, match="multiple backend accounts"):
        select_identity_consistent_candidates(
            candidates,
            auth_user_id=None,
            auth_account_id=None,
        )


def test_identity_selection_returns_empty_for_auth_only_ambiguous_partial():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={"user_id": "shared"},
    )

    assert select_identity_consistent_candidates(
        [candidate],
        auth_user_id="shared",
        auth_account_id="account-a",
    ) == []


def test_identity_selection_rejects_multiple_groups_without_auth():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "user-a", "account_id": "account-a"},
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "user-b", "account_id": "account-b"},
        ),
    ]

    with pytest.raises(ValueError, match="multiple backend accounts"):
        select_identity_consistent_candidates(
            candidates,
            auth_user_id=None,
            auth_account_id=None,
        )


def test_identity_selection_rejects_auth_without_matching_group():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "user-a", "account_id": "account-a"},
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "user-b", "account_id": "account-b"},
        ),
    ]

    with pytest.raises(ValueError, match="different account"):
        select_identity_consistent_candidates(
            candidates,
            auth_user_id="user-c",
            auth_account_id="account-c",
        )


def test_identity_selection_accepts_user_id_account_alias_without_exact_id():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={"account_id": "user-token"},
    )
    foreign = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={"account_id": "foreign-account"},
    )

    assert select_identity_consistent_candidates(
        [candidate, foreign],
        auth_user_id="user-token",
        auth_account_id="configured-account",
    ) == [candidate]


def test_identity_selection_rejects_two_exact_account_groups():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "user-a", "account_id": "account"},
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "user-b", "account_id": "account"},
        ),
    ]

    with pytest.raises(ValueError, match="does not identify one account"):
        select_identity_consistent_candidates(
            candidates,
            auth_user_id=None,
            auth_account_id="account",
        )


def test_identity_selection_rejects_two_user_matching_groups(monkeypatch):
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "user", "account_id": "account-a"},
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "user", "account_id": "account-b"},
        ),
    ]

    monkeypatch.setattr(
        identity_module,
        "_response_identity_matches_auth",
        lambda **_kwargs: True,
    )

    with pytest.raises(ValueError, match="does not identify one account"):
        select_identity_consistent_candidates(
            candidates,
            auth_user_id="user",
            auth_account_id=None,
        )


def test_identity_helpers_cover_partial_compatibility_and_priority_helpers():
    assert identity_module._identities_compatible(("user", None), (None, "account")) is False
    assert identity_module._candidate_priority(
        JsonCandidate(
            url="https://chatgpt.com/other",
            payload={"rateLimits": {}},
        )
    ) == 1
    assert backend_identity_from_candidates(
        [
            JsonCandidate(
                url="https://chatgpt.com/backend-api/wham/usage",
                payload={},
            )
        ]
    ) == (None, None)
    empty = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={},
    )
    assert select_identity_consistent_candidates(
        [empty],
        auth_user_id=None,
        auth_account_id=None,
    ) == [empty]
    assert backend_plan_type_from_payload({}) is None


def test_identity_selection_returns_one_matching_group(monkeypatch):
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "user-a", "account_id": "account-a"},
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={"user_id": "user-b", "account_id": "account-b"},
        ),
    ]

    monkeypatch.setattr(
        identity_module,
        "_response_identity_matches_auth",
        lambda **kwargs: kwargs["backend_account_id"] == "account-a",
    )

    assert select_identity_consistent_candidates(
        candidates,
        auth_user_id="user",
        auth_account_id=None,
    ) == [candidates[0]]


def test_response_identity_auth_matching_edge_cases():
    assert identity_module._response_identity_matches_auth(
        backend_user_id=None,
        backend_account_id="user",
        auth_user_id="user",
        auth_account_id=None,
    ) is True
    assert identity_module._response_identity_matches_auth(
        backend_user_id="foreign",
        backend_account_id="user",
        auth_user_id="user",
        auth_account_id=None,
    ) is False
    assert identity_module._response_identity_matches_auth(
        backend_user_id="foreign",
        backend_account_id=None,
        auth_user_id="user",
        auth_account_id=None,
    ) is False
    assert identity_module._response_identity_matches_auth(
        backend_user_id=None,
        backend_account_id=None,
        auth_user_id=None,
        auth_account_id=None,
    ) is True


def test_identity_helpers_skip_url_string_subclass_hooks():
    class BrokenStr(str):
        def strip(self):
            raise RuntimeError("synthetic identity URL marker")

    candidate = JsonCandidate(
        url=BrokenStr("https://chatgpt.com/backend-api/wham/usage"),
        payload={"user_id": "wrong-user"},
    )

    assert backend_identity_from_candidates([candidate]) == (None, None)


@pytest.mark.parametrize(
    "candidates",
    [None, 1, True, object(), _ValueErrorIterator(), _RuntimeErrorIterator()],
)
def test_identity_helpers_reject_non_iterable_candidates(candidates):
    assert backend_identity_from_candidates(candidates) == (None, None)  # type: ignore[arg-type]
    assert backend_plan_type_from_candidates(candidates) is None  # type: ignore[arg-type]
    assert (
        select_identity_consistent_candidates(  # type: ignore[arg-type]
            candidates,
            auth_user_id=None,
            auth_account_id=None,
        )
        == []
    )


def test_identity_helpers_bound_arbitrary_candidate_iterators():
    def overlong_candidates():
        for _ in range(51):
            yield JsonCandidate(
                url="https://chatgpt.com/backend-api/wham/usage",
                payload={"user_id": "ignored-user"},
            )
        raise AssertionError("identity candidate iterator was consumed past its cap")

    assert backend_identity_from_candidates(overlong_candidates()) == (None, None)
    assert backend_plan_type_from_candidates(overlong_candidates()) is None
    assert (
        select_identity_consistent_candidates(
            overlong_candidates(), auth_user_id=None, auth_account_id=None
        )
        == []
    )


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


def test_select_identity_consistent_candidates_drops_user_only_window_beside_exact_account():
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
            url="https://chatgpt.com/backend-api/wham/usage/daily-token-usage-breakdown",
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


def test_select_identity_consistent_candidates_prefers_exact_account_over_user_alias():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage/settings",
            payload={
                "user_id": "shared-user",
                "account_id": "shared-user",
                "rate_limit": {
                    "secondary_window": {
                        "used_percent": 99,
                        "limit_window_seconds": 604_800,
                    }
                },
            },
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "user_id": "shared-user",
                "account_id": "enterprise-account",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 3,
                        "limit_window_seconds": 18_000,
                    }
                },
            },
        ),
    ]

    selected = select_identity_consistent_candidates(
        candidates,
        auth_user_id="shared-user",
        auth_account_id="enterprise-account",
    )

    assert selected == [candidates[1]]


def test_select_identity_consistent_candidates_rejects_foreign_user_on_shared_user_alias():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={
            "user_id": "foreign-user",
            "account_id": "shared-user",
            "plan_type": "plus",
        },
    )

    with pytest.raises(ValueError, match="different account"):
        select_identity_consistent_candidates(
            [candidate],
            auth_user_id="shared-user",
            auth_account_id="real-account",
        )


def test_select_identity_consistent_candidates_rejects_foreign_user_on_exact_account():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={
            "user_id": "foreign-user",
            "account_id": "real-account",
        },
    )

    with pytest.raises(ValueError, match="different account"):
        select_identity_consistent_candidates(
            [candidate],
            auth_user_id="real-user",
            auth_account_id="real-account",
        )


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


def test_select_identity_consistent_candidates_rejects_partial_user_beside_foreign_account():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "account_id": "foreign-account",
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
                "user_id": "target-user",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 99,
                        "limit_window_seconds": 18_000,
                    }
                },
            },
        ),
    ]

    with pytest.raises(ValueError, match="different account"):
        select_identity_consistent_candidates(
            candidates,
            auth_user_id="target-user",
            auth_account_id="target-account",
        )


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


@pytest.mark.parametrize("order", [("user", "account", "full"), ("account", "user", "full")])
def test_identity_selection_merges_partial_candidates_regardless_of_order(order):
    candidates = {
        "user": JsonCandidate(
            "https://chatgpt.com/backend-api/wham/usage/user",
            {"user_id": "user"},
        ),
        "account": JsonCandidate(
            "https://chatgpt.com/backend-api/wham/usage/account",
            {"account_id": "account"},
        ),
        "full": JsonCandidate(
            "https://chatgpt.com/backend-api/wham/usage",
            {"user_id": "user", "account_id": "account"},
        ),
    }

    selected = select_identity_consistent_candidates(
        [candidates[key] for key in order],
        auth_user_id=None,
        auth_account_id=None,
    )

    assert selected == [candidates[key] for key in order]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("auth_user_id", 1),
        ("auth_account_id", []),
        ("auth_user_id", ""),
        ("auth_account_id", " "),
        ("auth_user_id", "user\nforged"),
        ("auth_account_id", "a" * 257),
    ),
)
def test_identity_selection_rejects_malformed_auth_ids(field, value):
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={"user_id": "user", "account_id": "account"},
    )
    auth = {"auth_user_id": None, "auth_account_id": None}
    auth[field] = value

    with pytest.raises(ValueError, match=f"{field} is invalid"):
        select_identity_consistent_candidates([candidate], **auth)


def test_identity_selection_rejects_auth_id_string_subclass_hooks():
    class BrokenStr(str):
        def __len__(self):
            raise RuntimeError("synthetic auth identity marker")

    with pytest.raises(ValueError, match="auth_user_id is invalid"):
        select_identity_consistent_candidates(
            [], auth_user_id=BrokenStr("user"), auth_account_id=None
        )


def test_identity_selection_rejects_account_id_string_subclass_hooks():
    class BrokenStr(str):
        def __bool__(self):
            raise RuntimeError("synthetic auth account marker")

    with pytest.raises(ValueError, match="auth_account_id is invalid"):
        select_identity_consistent_candidates(
            [], auth_user_id=None, auth_account_id=BrokenStr("account")
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("backend_user_id", []),
        ("auth_account_id", {}),
    ),
)
def test_identity_match_rejects_malformed_ids(field, value):
    values = {
        "backend_user_id": None,
        "backend_account_id": None,
        "auth_user_id": None,
        "auth_account_id": None,
    }
    values[field] = value

    assert identity_module._response_identity_matches_auth(**values) is False


def test_identity_candidate_priority_recovers_from_invalid_url_provider():
    class BrokenUrl:
        def __str__(self):
            raise RuntimeError("synthetic identity URL provider marker")

    candidate = JsonCandidate(url=BrokenUrl(), payload={})  # type: ignore[arg-type]

    assert identity_module._candidate_priority(candidate) == 2
