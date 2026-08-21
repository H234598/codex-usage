import pytest

from codex_usage.usage_resets import (
    UsageResetState,
    format_usage_resets,
    parse_usage_resets,
    redeem_usage_reset,
)


def test_reset_parser_distinguishes_unknown_zero_and_positive():
    assert parse_usage_resets({"resets": 0}).known is True
    assert parse_usage_resets({"resets": 0}).available == 0
    assert parse_usage_resets({"resets": 2}).available == 2
    assert parse_usage_resets({}).known is False
    assert format_usage_resets(parse_usage_resets({"resets": 2})) == "2"
    assert format_usage_resets(parse_usage_resets({})) == "—"


def test_reset_parser_round_trips_canonical_account_state():
    state = parse_usage_resets({
        "usage_resets": {
            "available": 2,
            "known": True,
            "redeem_capability": True,
        }
    })

    assert state == UsageResetState(2, True, True)


def test_reset_parser_fails_closed_for_invalid_or_conflicting_values():
    assert parse_usage_resets({"resets": -1}).known is False
    assert parse_usage_resets({"resets": True}).known is False
    assert parse_usage_resets({"resets": 2, "usage_resets": 3}).known is False
    assert parse_usage_resets({
        "usage_resets": {
            "available": 2,
            "known": True,
            "redeem_capability": False,
        },
        "resets": 3,
    }).known is False
    with pytest.raises(ValueError, match="hide_zero"):
        format_usage_resets(parse_usage_resets({"resets": 0}), hide_zero="yes")


def test_reset_redemption_is_disabled_without_capability():
    with pytest.raises(ValueError, match="capability"):
        redeem_usage_reset(parse_usage_resets({"resets": 1}))


def test_reset_state_and_redeem_require_consistent_positive_state():
    with pytest.raises(ValueError, match="known"):
        UsageResetState(1, False)
    with pytest.raises(ValueError, match="available"):
        UsageResetState(None, True)
    with pytest.raises(ValueError, match="positive"):
        redeem_usage_reset(UsageResetState(0, True, True))


@pytest.mark.parametrize(
    ("available", "known", "redeem_capability", "message"),
    [
        (None, "yes", False, "known"),
        (None, False, "yes", "redeem_capability"),
        (1, False, False, "unknown"),
        (True, True, False, "bounded"),
        (10_001, True, False, "bounded"),
    ],
)
def test_reset_state_rejects_malformed_fields(
    available, known, redeem_capability, message
):
    with pytest.raises(ValueError, match=message):
        UsageResetState(available, known, redeem_capability)


def test_reset_state_serializes_canonical_fields():
    assert UsageResetState(2, True, True).as_dict() == {
        "available": 2,
        "known": True,
        "redeem_capability": True,
    }


def test_reset_parser_handles_nested_legacy_and_malformed_canonical_values():
    state = parse_usage_resets(
        {
            "usage_resets": {
                "available": 2,
                "known": True,
                "redeem_capability": False,
            },
            "resets": {"available": 2},
        }
    )
    assert state == UsageResetState(2, True, False)
    assert parse_usage_resets(
        {
            "usage_resets": {
                "available": 2,
                "known": True,
                "redeem_capability": False,
            },
            "resets": True,
        }
    ) == UsageResetState(None, False, False)
    assert parse_usage_resets(
        {"available": None, "known": "yes", "redeem_capability": False}
    ) == UsageResetState(None, False, False)
    assert parse_usage_resets(
        {
            "available": 2,
            "known": True,
            "redeem_capability": False,
            "usage_resets": {"available": 2},
        }
    ) == UsageResetState(2, True, False)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, UsageResetState(None, False, False)),
        ({"usage_resets": {"available": 2}}, UsageResetState(2, True, False)),
        ({"usage_resets": {"available": "2"}}, UsageResetState(None, False, False)),
        ({"resets": 2, "redeem_capability": "yes"}, UsageResetState(None, False, False)),
    ],
)
def test_reset_parser_handles_legacy_boundaries(payload, expected):
    assert parse_usage_resets(payload) == expected


def test_reset_formatting_and_redemption_boundaries():
    zero = UsageResetState(0, True, False)
    assert format_usage_resets(zero, hide_zero=False) == "0"
    assert format_usage_resets(zero) == ""
    with pytest.raises(ValueError, match="invalid"):
        format_usage_resets(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid"):
        redeem_usage_reset(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="known"):
        redeem_usage_reset(UsageResetState(None, False, True))
    with pytest.raises(NotImplementedError, match="not implemented"):
        redeem_usage_reset(UsageResetState(1, True, True))
