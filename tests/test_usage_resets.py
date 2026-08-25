from collections.abc import Mapping

import pytest

from codex_usage.usage_resets import (
    UsageResetState,
    format_usage_resets,
    parse_usage_resets,
    redeem_usage_reset,
)


class _BrokenResetCount(int):
    def __ge__(self, _other):
        raise RuntimeError("synthetic reset count marker")


class _ExplodingMapping(Mapping):
    def __getitem__(self, _key):
        raise RuntimeError("synthetic mapping callback marker")

    def __iter__(self):
        return iter(("resets",))

    def __len__(self):
        return 1


def _broken_reset_state(field, value=None):
    class BrokenState(UsageResetState):
        pass

    def fail(_self):
        raise RuntimeError(f"synthetic reset {field} marker")

    setattr(BrokenState, field, property(fail))
    state = object.__new__(BrokenState)
    state.__dict__.update(
        {
            "available": 1 if value is None else value,
            "known": True,
            "redeem_capability": True,
        }
    )
    return state


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


def test_reset_parser_accepts_app_server_reset_credit_summary():
    assert parse_usage_resets(
        {"rateLimitResetCredits": {"availableCount": 1, "credits": None}}
    ) == UsageResetState(1, True, False)
    assert parse_usage_resets(
        {
            "available": 1,
            "known": True,
            "redeem_capability": False,
            "rateLimitResetCredits": {"availableCount": 1},
        }
    ) == UsageResetState(1, True, False)


def test_reset_parser_fails_closed_for_invalid_or_conflicting_values():
    assert parse_usage_resets(
        {"rateLimitResetCredits": {"credits": None}}
    ).known is False
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
    assert parse_usage_resets(
        {"rateLimitResetCredits": {"availableCount": True}}
    ).known is False
    assert parse_usage_resets(
        {
            "rateLimitResetCredits": {"availableCount": 1},
            "resets": 2,
        }
    ).known is False
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


def test_reset_state_rejects_integer_subclass_count():
    count = _BrokenResetCount(1)

    with pytest.raises(ValueError, match="bounded"):
        UsageResetState(count, True)
    assert parse_usage_resets({"resets": count}) == UsageResetState(None, False, False)


def test_reset_parser_rejects_mapping_callback_errors():
    assert parse_usage_resets(_ExplodingMapping()) == UsageResetState(None, False, False)


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
    "payload",
    [
        {"usage_resets": {"available": 2, "known": False}},
        {"usage_resets": {"available": 2, "redeem_capability": False}},
        {"available": 2, "known": False, "resets": 2},
        {"available": 2, "redeem_capability": False, "resets": 2},
        {
            "available": 2,
            "known": True,
            "redeem_capability": False,
            "usage_resets": {"available": 2, "known": False},
        },
    ],
)
def test_reset_parser_rejects_partial_canonical_metadata(payload):
    assert parse_usage_resets(payload) == UsageResetState(None, False, False)


def test_reset_parser_rejects_conflicting_top_level_available_with_nested_state():
    assert parse_usage_resets(
        {
            "available": 1,
            "usage_resets": {
                "available": 2,
                "known": True,
                "redeem_capability": False,
            },
        }
    ) == UsageResetState(None, False, False)


def test_reset_parser_accepts_equal_complete_legacy_duplicate():
    assert parse_usage_resets(
        {
            "available": 2,
            "known": True,
            "redeem_capability": False,
            "usage_resets": {
                "available": 2,
                "known": True,
                "redeem_capability": False,
            },
        }
    ) == UsageResetState(2, True, False)


@pytest.mark.parametrize(
    "nested",
    [
        {"available": 2, "known": False, "redeem_capability": False},
        {"available": 2, "known": True, "redeem_capability": True},
        {"available": 2, "known": "yes", "redeem_capability": False},
    ],
)
def test_reset_parser_rejects_conflicting_complete_canonical_sources(nested):
    payload = {
        "available": 2,
        "known": True,
        "redeem_capability": False,
        "usage_resets": nested,
    }

    assert parse_usage_resets(payload) == UsageResetState(None, False, False)


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


@pytest.mark.parametrize("legacy_key", ["resets", "available_resets", "usage_resets"])
def test_reset_parser_rejects_conflicting_top_level_available(legacy_key):
    assert parse_usage_resets({"available": 1, legacy_key: 2}) == UsageResetState(
        None, False, False
    )


def test_reset_parser_rejects_conflicting_top_level_available_with_app_summary():
    assert parse_usage_resets(
        {"available": 1, "rateLimitResetCredits": {"availableCount": 2}}
    ) == UsageResetState(None, False, False)


@pytest.mark.parametrize(
    "source",
    [
        {"resets": 1},
        {"available_resets": 1},
        {"usage_resets": 1},
        {"rateLimitResetCredits": {"availableCount": 1}},
    ],
)
def test_reset_parser_rejects_partial_top_level_available_even_when_equal(source):
    assert parse_usage_resets({"available": 1, **source}) == UsageResetState(
        None, False, False
    )


@pytest.mark.parametrize("legacy_key", ["resets", "available_resets"])
@pytest.mark.parametrize("metadata", ["known", "redeem_capability"])
def test_reset_parser_rejects_metadata_inside_legacy_mapping(legacy_key, metadata):
    value = {"available": 2, metadata: True}

    assert parse_usage_resets({legacy_key: value}) == UsageResetState(
        None, False, False
    )


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


def test_reset_as_dict_maps_generic_available_getter_error():
    with pytest.raises(ValueError, match="reset state is invalid"):
        _broken_reset_state("available").as_dict()


def test_reset_as_dict_maps_generic_known_getter_error():
    with pytest.raises(ValueError, match="reset state is invalid"):
        _broken_reset_state("known").as_dict()


def test_reset_as_dict_maps_generic_capability_getter_error():
    with pytest.raises(ValueError, match="reset state is invalid"):
        _broken_reset_state("redeem_capability").as_dict()


def test_reset_as_dict_rejects_invalid_subclass_fields():
    with pytest.raises(ValueError, match="reset state is invalid"):
        _broken_reset_state("available", value="1").as_dict()


def test_reset_format_maps_generic_available_getter_error():
    with pytest.raises(ValueError, match="reset state is invalid"):
        format_usage_resets(_broken_reset_state("available"))


def test_reset_format_maps_generic_known_getter_error():
    with pytest.raises(ValueError, match="reset state is invalid"):
        format_usage_resets(_broken_reset_state("known"))


def test_reset_redeem_maps_generic_capability_getter_error():
    with pytest.raises(ValueError, match="reset state is invalid"):
        redeem_usage_reset(_broken_reset_state("redeem_capability"))


def test_reset_redeem_maps_generic_known_getter_error():
    with pytest.raises(ValueError, match="reset state is invalid"):
        redeem_usage_reset(_broken_reset_state("known"))


def test_reset_redeem_maps_generic_available_getter_error():
    with pytest.raises(ValueError, match="reset state is invalid"):
        redeem_usage_reset(_broken_reset_state("available"))


def test_reset_redeem_rejects_invalid_subclass_fields():
    with pytest.raises(ValueError, match="reset state is invalid"):
        redeem_usage_reset(_broken_reset_state("available", value="1"))
