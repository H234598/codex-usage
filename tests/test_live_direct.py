from __future__ import annotations

import os
from pathlib import Path

import pytest

from codex_usage.direct import fetch_account_usage_direct
from codex_usage.models import Account, AccountStatus


@pytest.mark.skipif(
    not os.environ.get("CODEX_USAGE_LIVE_AUTH_JSON"),
    reason="set CODEX_USAGE_LIVE_AUTH_JSON to run live direct auth test",
)
def test_live_auth_json_fetches_usage_limits():
    auth_path = Path(os.environ["CODEX_USAGE_LIVE_AUTH_JSON"]).expanduser()
    account = Account(
        id="live",
        label="Live",
        profile_dir="/tmp/codex-usage-live",
        auth_json_path=str(auth_path),
    )

    usage = fetch_account_usage_direct(account)

    assert usage.status == AccountStatus.OK
    assert usage.error is None
    assert usage.five_hour is not None
    assert usage.five_hour.remaining is not None
    assert usage.five_hour.reset_at is not None
    assert usage.weekly is not None
    assert usage.weekly.remaining is not None
    assert usage.weekly.reset_at is not None
