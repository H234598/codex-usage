from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from codex_usage.extractor import (
    JsonCandidate,
    _parse_datetime,
    _parse_time_today_or_next,
    extract_windows,
)


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


def test_parse_datetime_rejects_unrepresentable_timezone_conversion():
    captured_at = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))

    assert _parse_datetime("9999-12-31T23:59:59+00:00", captured_at) is None


def test_parse_time_today_or_next_rejects_unrepresentable_next_day():
    captured_at = datetime(9999, 12, 31, 23, 59, 1)

    assert _parse_time_today_or_next("23:59", captured_at) is None


def test_parse_datetime_rejects_boolean_timestamp_values():
    captured_at = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))

    assert _parse_datetime(True, captured_at) is None
    assert _parse_datetime(False, captured_at) is None


def test_parse_datetime_rejects_overflowing_numeric_timestamps():
    captured_at = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))

    assert _parse_datetime(10**10000, captured_at) is None
    assert _parse_datetime("9" * 10000, captured_at) is None


def test_parse_datetime_keeps_compact_iso_dates_out_of_timestamp_parsing():
    captured_at = datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin"))

    parsed = _parse_datetime("20260608", captured_at)

    assert parsed is not None
    assert parsed.strftime("%d.%m.%Y %H:%M") == "08.06.2026 00:00"


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


def test_extract_windows_normalizes_used_percent_to_remaining_percent():
    body = """
    5-hour limit
    3% used
    Reset 08.06.2026 04:26

    Weekly limit
    45% used
    Reset 10.06.2026 05:05
    """

    five, weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 97
    assert five.percent == 97
    assert weekly is not None
    assert weekly.remaining == 55
    assert weekly.percent == 55


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
      <span>Zurücksetzungen 10.06.2026 5:05</span>
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


def test_extract_windows_prefers_later_dom_usage_over_reset_only_match():
    body = """
    5-hour limit
    Reset 08.06.2026 04:26
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
    assert five.remaining == 58
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


def test_extract_windows_prefers_later_json_usage_over_reset_only_match():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/partial",
            payload={
                "five_hour": {
                    "limit_window_seconds": 18_000,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                }
            },
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/fresh",
            payload={
                "five_hour": {
                    "limit_window_seconds": 18_000,
                    "used_percent": 3,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                }
            },
        ),
    ]

    five, _weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 97
    assert five.source.endswith("fresh")


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


def test_extract_windows_from_wham_numeric_string_reset_timestamps():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 3,
                        "limit_window_seconds": 18_000,
                        "reset_at": "1780894250",
                    },
                    "secondary_window": {
                        "used_percent": 45,
                        "limit_window_seconds": 604_800,
                        "reset_at": "1781060750",
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
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "08.06.2026 06:50"
    assert weekly is not None
    assert weekly.reset_at is not None
    assert weekly.reset_at.strftime("%d.%m.%Y %H:%M") == "10.06.2026 05:05"
