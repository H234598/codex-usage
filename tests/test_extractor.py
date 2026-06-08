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


def test_extract_windows_from_short_english_dom_labels():
    body = """
    5-hour limit
    42 / 100 used
    Reset 08.06.2026 04:26

    Weekly limit
    310 / 1000 used
    Reset 14.06.2026 04:26
    """

    five, weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used == 42
    assert five.limit == 100
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "08.06.2026 04:26"
    assert weekly is not None
    assert weekly.used == 310
    assert weekly.limit == 1000
    assert weekly.reset_at is not None
    assert weekly.reset_at.strftime("%d.%m.%Y %H:%M") == "14.06.2026 04:26"


def test_extract_windows_from_remaining_percent_dom_text():
    body = """
    5 Stunden Nutzungsgrenze
    97 % verbleibend
    Zurücksetzungen 6:50

    Wöchentliches Nutzungslimit
    55 % verbleibend
    Zurücksetzungen 10.06.2026 5:05
    Nutzungsaufschlüsselung
    """

    five, weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 97
    assert five.percent == 97
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "08.06.2026 06:50"
    assert weekly is not None
    assert weekly.remaining == 55
    assert weekly.percent == 55
    assert weekly.reset_at is not None
    assert weekly.reset_at.strftime("%d.%m.%Y %H:%M") == "10.06.2026 05:05"


def test_extract_windows_from_progress_bar_width_html():
    body = """
    <section>
      <h2>5 Stunden Nutzungsgrenze</h2>
      <div class="absolute start-0 top-0 h-full transition-[width] bg-[#22c55e]
        rounded-full" style="width: 97%;"></div>
      <p>Zurücksetzungen 6:50</p>
    </section>
    <section>
      <h2>Wöchentliches Nutzungslimit</h2>
      <div class="absolute start-0 top-0 h-full transition-[width] bg-[#22c55e]
        rounded-full" style="width: 55%;"></div>
      <p>Zurücksetzungen 10.06.2026 5:05</p>
    </section>
    """

    five, weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 97
    assert five.percent == 97
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "08.06.2026 06:50"
    assert weekly is not None
    assert weekly.remaining == 55
    assert weekly.percent == 55
    assert weekly.reset_at is not None
    assert weekly.reset_at.strftime("%d.%m.%Y %H:%M") == "10.06.2026 05:05"


def test_extract_windows_skips_label_occurrence_without_values():
    body = """
    5-hour limit
    Loading chart
    Weekly limit
    Loading chart

    Details
    5-hour limit
    42 / 100 used
    Reset 08.06.2026 04:26
    Weekly limit
    310 / 1000 used
    Reset 14.06.2026 04:26
    """

    five, weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used == 42
    assert weekly is not None
    assert weekly.used == 310


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


def test_extract_windows_from_wham_usage_rate_limit_json():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "rate_limit": {
                    "allowed": True,
                    "primary_window": {
                        "used_percent": 3,
                        "limit_window_seconds": 18000,
                        "reset_after_seconds": 13665,
                        "reset_at": 1780894250,
                    },
                    "secondary_window": {
                        "used_percent": 45,
                        "limit_window_seconds": 604800,
                        "reset_after_seconds": 180164,
                        "reset_at": 1781060750,
                    },
                }
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used == 3
    assert five.limit == 100
    assert five.remaining == 97
    assert five.percent == 97
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "08.06.2026 06:50"
    assert weekly is not None
    assert weekly.used == 45
    assert weekly.limit == 100
    assert weekly.remaining == 55
    assert weekly.percent == 55
    assert weekly.reset_at is not None
    assert weekly.reset_at.strftime("%d.%m.%Y %H:%M") == "10.06.2026 05:05"
