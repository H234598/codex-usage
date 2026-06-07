from __future__ import annotations

from codex_usage.bridge import render_bridge_snippet, usage_from_ingest_payload
from codex_usage.models import Account, AccountStatus


def test_usage_from_ingest_payload_extracts_visible_values():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics?secret=1",
            "capturedAt": "2026-06-08T04:20:00+02:00",
            "bodyText": """
            5 Stunden Nutzungsgrenze
            42 / 100 genutzt
            Zurücksetzungen 08.06.2026 04:26
            Wöchentliches Nutzungslimit
            310 / 1000 genutzt
            Zurücksetzungen 14.06.2026 04:26
            """,
        },
    )

    assert usage.status == AccountStatus.OK
    assert usage.source_urls == ("https://chatgpt.com/codex/cloud/settings/analytics",)
    assert usage.five_hour is not None
    assert usage.five_hour.used == 42
    assert usage.weekly is not None
    assert usage.weekly.limit == 1000


def test_render_bridge_snippet_contains_account_endpoint_and_interval():
    snippet = render_bridge_snippet(
        "BW_Privat",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )

    assert '"BW_Privat"' in snippet
    assert '"http://127.0.0.1:8765/ingest"' in snippet
    assert "setInterval" in snippet
    assert "300000" in snippet
