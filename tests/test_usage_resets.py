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
