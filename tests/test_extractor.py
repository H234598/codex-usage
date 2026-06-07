from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from codex_usage.extractor import JsonCandidate, extract_windows


def test_extract_windows_from_german_dom_text():
    body = """
    5 Stunden Nutzungsgrenze
    42 / 100 genutzt
    Zurücksetzungen 08.06.2026 04:26

    Wöchentliches Nutzungslimit
    310 von 1000 genutzt
    Zurücksetzungen 14.06.2026 04:26
    """

    five, weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used == 42
    assert five.limit == 100
    assert five.remaining == 58
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "08.06.2026 04:26"
    assert weekly is not None
    assert weekly.used == 310
    assert weekly.limit == 1000
    assert weekly.remaining == 690


def test_extract_windows_prefers_json_candidates():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/codex/analytics",
            payload={
                "five_hour_usage_limit": {
                    "used": 8,
                    "limit": 40,
                    "reset_at": "2026-06-08T04:26:00+02:00",
                },
                "weekly_usage_limit": {
                    "used": 80,
                    "limit": 400,
                    "reset_at": "2026-06-14T04:26:00+02:00",
                },
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="5 Stunden Nutzungsgrenze 1 / 1 Zurücksetzungen 01.01.2026 00:00",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used == 8
    assert five.limit == 40
    assert five.source.startswith("json:")
    assert weekly is not None
    assert weekly.used == 80
    assert weekly.limit == 400
    assert weekly.source.startswith("json:")
