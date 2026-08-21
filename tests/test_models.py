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
