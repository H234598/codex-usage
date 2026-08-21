import json
from datetime import UTC, datetime

import pytest

from codex_usage.models import AccountStatus, AccountUsage, LimitWindow, UsagePool


@pytest.mark.parametrize("duration", [True, 1.0, "1", 0, -1, None])
def test_window_for_duration_rejects_invalid_duration_types(duration):
    pool = UsagePool(
        key="custom",
        display_name="Custom",
        windows=(LimitWindow(name="1s", duration_seconds=1, remaining=1),),
    )

    assert pool.window_for_duration(duration) is None


def test_account_usage_as_dict_skips_unhashable_model_pool_keys():
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        status=AccountStatus.OK,
        models=(
            UsagePool(key=[], display_name="malformed"),
            UsagePool(key="valid", display_name="Valid"),
        ),
    )

    payload = usage.as_dict()

    assert tuple(payload["models"]) == ("valid",)


@pytest.mark.parametrize("status", [[], {}, "ok"])
def test_account_usage_as_dict_normalizes_invalid_status(status):
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        status=status,
    )

    payload = usage.as_dict()

    assert payload["status"] == "error"
    assert payload["stale"] is True
    assert payload["cache_invalidated"] is True


def test_account_usage_as_dict_handles_invalid_optional_containers():
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        source_urls=None,
        usage_resets=None,
    )

    payload = usage.as_dict()

    assert payload["source_urls"] == []
    assert payload["usage_resets"] == {
        "available": None,
        "known": False,
        "redeem_capability": False,
    }


def test_account_usage_as_dict_keeps_malformed_window_values_json_safe():
    malformed = datetime.now(UTC)
    window = LimitWindow(
        name=malformed,  # type: ignore[arg-type]
        used=malformed,  # type: ignore[arg-type]
        reset_at=[],  # type: ignore[arg-type]
    )
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=[],  # type: ignore[arg-type]
        five_hour=window,
        blocked_until={},  # type: ignore[arg-type]
        auth_last_refresh=[],  # type: ignore[arg-type]
        auth_access_expires_at={},  # type: ignore[arg-type]
        auth_id_expires_at="invalid",  # type: ignore[arg-type]
        values_captured_at=[],  # type: ignore[arg-type]
    )

    payload = usage.as_dict()

    json.dumps(payload, allow_nan=False)
    assert payload["captured_at"] is None
    assert payload["five_hour"]["name"] is None
    assert payload["five_hour"]["used"] is None
    assert payload["five_hour"]["reset_at"] is None


@pytest.mark.parametrize(
    ("five_hour", "weekly", "expected_windows"),
    [
        ([], None, ()),
        (["malformed"], LimitWindow(name="weekly", remaining=80), ("weekly",)),
    ],
)
def test_account_usage_skips_malformed_legacy_windows(
    five_hour, weekly, expected_windows
):
    usage = AccountUsage(
        account_id="account",
        label="Account",
        captured_at=datetime.now(UTC),
        five_hour=five_hour,  # type: ignore[arg-type]
        weekly=weekly,
    )

    if expected_windows:
        assert usage.main is not None
        assert tuple(window.name for window in usage.main.windows) == expected_windows
    else:
        assert usage.main is None
