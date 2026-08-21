import pytest

from codex_usage.models import LimitWindow, UsagePool


@pytest.mark.parametrize("duration", [True, 1.0, "1", 0, -1, None])
def test_window_for_duration_rejects_invalid_duration_types(duration):
    pool = UsagePool(
        key="custom",
        display_name="Custom",
        windows=(LimitWindow(name="1s", duration_seconds=1, remaining=1),),
    )

    assert pool.window_for_duration(duration) is None
