from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

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


def test_extract_windows_prefers_newer_text_usage_over_older_reset_metadata():
    body = """
    5-hour usage limit
    3 / 100 used
    Reset 08.06.2026 06:50

    5-hour usage limit
    20 / 100 used
    """

    five, _weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used == 20
    assert five.limit == 100
    assert five.remaining == 80
    assert five.reset_at is None


def test_extract_windows_does_not_treat_limit_only_as_usage():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/other",
        payload={
            "five_hour": {
                "limit": 100,
                "reset_at": "2026-07-12T14:00:00+02:00",
            },
            "weekly": {
                "limit": 100,
                "reset_at": "2026-07-18T08:00:00+02:00",
            },
        },
    )

    five, weekly = extract_windows(
        body_text="",
        json_candidates=[candidate],
        now=datetime(2026, 7, 12, 11, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None and five.limit == 100
    assert weekly is not None and weekly.limit == 100
    assert five.has_usage_value is False
    assert weekly.has_usage_value is False


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


def test_extract_windows_does_not_match_longer_hour_labels_as_five_hour():
    for label in ("15h", "25h", "15-hour"):
        five, _weekly = extract_windows(
            body_text=f"{label} usage limit 80% remaining Reset 13.07.2026 04:00",
            now=datetime(2026, 7, 12, 4, 0, tzinfo=ZoneInfo("Europe/Berlin")),
        )

        assert five is None

        five, _weekly = extract_windows(
            body_text="",
            json_candidates=[
                JsonCandidate(
                    url="https://chatgpt.com/backend-api/generic",
                    payload={f"{label}_usage_limit": {"used_percent": 20}},
                )
            ],
        )

        assert five is None


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


def test_extract_windows_prefers_absolute_usage_over_conflicting_used_percent():
    body = """
    5-hour limit
    42 / 100 used
    3% used
    Reset 08.06.2026 04:26
    """

    five, _weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used == 42
    assert five.limit == 100
    assert five.remaining == 58
    assert five.percent == 58


def test_extract_windows_prefers_absolute_usage_over_conflicting_progress_width():
    body = """
    5-hour limit
    42 / 100 used
    <div style="width: 97%;"></div>
    Reset 08.06.2026 04:26
    """

    five, _weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used == 42
    assert five.limit == 100
    assert five.remaining == 58
    assert five.percent == 58


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


def test_extract_windows_prefers_html_progress_over_hidden_text_clone():
    html = """
    <section>
      <h2>5-hour limit</h2>
      <div class="transition-[width] rounded-full" style="width: 97%;"></div>
    </section>
    <section hidden>
      <h2>5-hour limit</h2>
      <span>20% remaining Reset 12.07.2026 18:00</span>
    </section>
    """

    five, _weekly = extract_windows(
        body_text="",
        text_sources=(("htmlText", html),),
        now=datetime(2026, 7, 12, 17, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 97
    assert five.source == "htmlText"


def test_extract_windows_prefers_visible_html_progress_over_hidden_progress_clone():
    html = """
    <section>
      <h2>5-hour limit</h2>
      <div class="transition-[width] rounded-full bg-[#22c55e]" style="width: 97%;"></div>
      <span>Reset 12.07.2026 19:00</span>
    </section>
    <section hidden>
      <h2>5-hour limit</h2>
      <div class="transition-[width] rounded-full bg-[#22c55e]" style="width: 20%;"></div>
      <span>Reset 12.07.2026 18:00</span>
    </section>
    """

    five, _weekly = extract_windows(
        body_text="",
        text_sources=(("htmlText", html),),
        now=datetime(2026, 7, 12, 17, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 97
    assert five.reset_at is not None
    assert five.reset_at.strftime("%H:%M") == "19:00"


@pytest.mark.parametrize(
    "hidden_attribute",
    ("hidden", 'aria-hidden="true"', 'style="display: none"'),
)
def test_extract_windows_ignores_hidden_progress_beside_visible_text(hidden_attribute):
    html = f"""
    <section>
      <h2>5-hour limit</h2>
      <span>80% remaining Reset 12.07.2026 19:00</span>
    </section>
    <section {hidden_attribute}>
      <h2>5-hour limit</h2>
      <div class="transition-[width] rounded-full bg-[#22c55e]"
        style="width: 20%;"></div>
    </section>
    """

    five, _weekly = extract_windows(
        body_text="",
        text_sources=(("htmlText", html),),
        now=datetime(2026, 7, 12, 17, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 80
    assert five.percent == 80
    assert five.reset_at is not None
    assert five.reset_at.strftime("%H:%M") == "19:00"


def test_extract_windows_prefers_visible_html_progress_over_aria_hidden_clone():
    html = """
    <section>
      <h2>5-hour limit</h2>
      <div class="transition-[width] rounded-full" style="width: 97%;"></div>
    </section>
    <section aria-hidden="true">
      <h2>5-hour limit</h2>
      <div class="transition-[width] rounded-full" style="width: 20%;"></div>
    </section>
    """

    five, _weekly = extract_windows(
        body_text="",
        text_sources=(("htmlText", html),),
    )

    assert five is not None
    assert five.remaining == 97


def test_extract_windows_ignores_layout_width_before_progress_bar():
    body = """
    <section style="width: 42%;">
      <h2>5 Stunden Nutzungsgrenze</h2>
      <div class="layout" style="width: 100%;"></div>
      <div class="absolute start-0 top-0 h-full transition-[width]
        bg-[#22c55e] rounded-full" style="width: 97%;"></div>
      <span>Zurücksetzungen 08.06.2026 06:50</span>
    </section>
    <section>
      <h2>Wöchentliches Nutzungslimit</h2>
      <div class="layout" style="width: 88%;"></div>
      <div class="absolute start-0 top-0 h-full transition-[width]
        bg-[#22c55e] rounded-full" style="width: 55%;"></div>
      <span>Zurücksetzungen 10.06.2026 05:05</span>
    </section>
    """

    five, weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None and five.remaining == 97
    assert weekly is not None and weekly.remaining == 55


def test_extract_windows_prefers_progress_bar_over_generic_used_text():
    body = """
    <section>
      <h2>5 Stunden Nutzungsgrenze</h2>
      <div>100% used</div>
      <div class="transition-[width] rounded-full bg-[#22c55e]"
        style="width: 97%;"></div>
      <span>Zurücksetzungen 08.06.2026 06:50</span>
    </section>
    <section>
      <h2>Wöchentliches Nutzungslimit</h2>
      <div>100% used</div>
      <div class="transition-[width] rounded-full bg-[#22c55e]"
        style="width: 55%;"></div>
      <span>Zurücksetzungen 10.06.2026 05:05</span>
    </section>
    """

    five, weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None and five.remaining == 97
    assert weekly is not None and weekly.remaining == 55


def test_extract_windows_prefers_visible_text_over_later_stale_dom_clone():
    visible = """
    5-hour limit 97% remaining Reset 12.07.2026 19:00
    Weekly limit 55% remaining Reset 18.07.2026 08:00
    """
    stale_clone = """
    5-hour limit 20% remaining Reset 12.07.2026 18:00
    Weekly limit 10% remaining Reset 18.07.2026 07:00
    """

    five, weekly = extract_windows(
        body_text="",
        text_sources=(("bodyText", visible), ("domText", stale_clone)),
        now=datetime(2026, 7, 12, 17, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None and five.remaining == 97
    assert five.reset_at is not None
    assert five.reset_at.strftime("%H:%M") == "19:00"
    assert weekly is not None and weekly.remaining == 55
    assert weekly.source == "bodyText"


def test_extract_windows_keeps_unlabelled_visible_percent_over_stale_clone():
    visible = "5-hour limit 97% Reset 12.07.2026 19:00"
    stale_clone = "5-hour limit 20% remaining Reset 12.07.2026 18:00"

    five, _weekly = extract_windows(
        body_text="",
        text_sources=(("bodyText", visible), ("domText", stale_clone)),
        now=datetime(2026, 7, 12, 17, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None and five.percent == 97
    assert five.remaining is None
    assert five.source == "bodyText"


def test_extract_windows_uses_html_progress_over_visible_generic_used_text():
    visible = "5-hour limit 100% used Weekly limit 100% used"
    html = """
    <h2>5-hour limit</h2>
    <div class="transition-[width] rounded-full bg-[#22c55e]" style="width: 97%;"></div>
    <h2>Weekly limit</h2>
    <div class="transition-[width] rounded-full bg-[#22c55e]" style="width: 55%;"></div>
    """

    five, weekly = extract_windows(
        body_text="",
        text_sources=(("bodyText", visible), ("htmlText", html)),
    )

    assert five is not None and five.remaining == 97
    assert weekly is not None and weekly.remaining == 55
    assert five.source == "htmlText"


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


def test_extract_windows_prefers_complete_later_dom_usage_over_partial_value():
    body = """
    5-hour limit
    97% remaining
    {filler}
    5-hour limit
    97% remaining
    Reset 08.06.2026 06:50
    """.format(filler="x" * 1600)

    five, _weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 97
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "08.06.2026 06:50"


def test_extract_windows_prefers_later_complete_dom_usage_block():
    body = """
    5-hour limit
    20% remaining
    Reset 08.06.2026 05:00

    5-hour limit
    97% remaining
    Reset 08.06.2026 06:50

    Weekly limit
    55% remaining
    Reset 10.06.2026 05:05
    """

    five, weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 97
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "08.06.2026 06:50"
    assert weekly is not None
    assert weekly.remaining == 55


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


def test_extract_windows_merges_dom_reset_into_partial_json_window():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/partial",
            payload={"five_hour_usage_limit": {"used_percent": 3}},
        )
    ]
    body = """
    5-hour limit
    <div style="width: 97%;"></div>
    Reset 08.06.2026 06:50
    """

    five, _weekly = extract_windows(
        body_text=body,
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 97
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "08.06.2026 06:50"
    assert five.source == "json:https://chatgpt.com/backend-api/partial+dom-text"


def test_extract_windows_keeps_complete_json_over_conflicting_dom_window():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/fresh",
            payload={
                "five_hour_usage_limit": {
                    "used_percent": 3,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                }
            },
        )
    ]
    body = """
    5-hour limit
    50% remaining
    Reset 09.06.2026 08:00
    """

    five, _weekly = extract_windows(
        body_text=body,
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 97
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "08.06.2026 06:50"
    assert five.source == "json:https://chatgpt.com/backend-api/fresh"


def test_extract_windows_merges_dom_usage_into_reset_only_json_window():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/reset-only",
            payload={
                "five_hour_usage_limit": {
                    "reset_at": "2026-06-08T06:50:00+02:00",
                }
            },
        )
    ]
    body = """
    5-hour limit
    97% remaining
    Reset 09.06.2026 08:00
    """

    five, _weekly = extract_windows(
        body_text=body,
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 97
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "08.06.2026 06:50"
    assert five.source == "dom-text+json:https://chatgpt.com/backend-api/reset-only"


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


def test_extract_windows_prefers_latest_dom_reset_only_metadata():
    body = """
    5-hour limit
    Reset 12.07.2026 15:00
    5-hour limit
    Reset 12.07.2026 16:00
    """

    five, _weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 7, 12, 14, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "12.07.2026 16:00"


def test_extract_windows_prefers_latest_json_reset_only_metadata():
    def candidate(reset_at: str) -> JsonCandidate:
        return JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "rate_limit": {
                    "primary_window": {
                        "limit_window_seconds": 18_000,
                        "reset_at": reset_at,
                    }
                }
            },
        )

    five, _weekly = extract_windows(
        body_text="",
        json_candidates=[
            candidate("2026-07-12T15:00:00+02:00"),
            candidate("2026-07-12T16:00:00+02:00"),
        ],
        now=datetime(2026, 7, 12, 14, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "12.07.2026 16:00"


def test_extract_windows_prefers_complete_json_usage_over_earlier_partial_usage():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/partial",
            payload={
                "five_hour_usage_limit": {
                    "used_percent": 3,
                }
            },
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/fresh",
            payload={
                "five_hour_usage_limit": {
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
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "08.06.2026 06:50"
    assert five.source.endswith("fresh")


def test_extract_windows_prefers_specific_generic_fields_over_aggregates():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "usage": 999,
                    "used_percent": 3,
                    "used": 8,
                    "total": 9999,
                    "limit": 40,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                },
                "weekly_usage_limit": {
                    "usage": 999,
                    "used_percent": 45,
                    "used": 80,
                    "total": 9999,
                    "limit": 400,
                    "reset_at": "2026-06-14T04:26:00+02:00",
                },
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used == 8
    assert five.remaining == 32
    assert five.percent == 80
    assert weekly is not None
    assert weekly.used == 80
    assert weekly.remaining == 320
    assert weekly.percent == 80


def test_extract_windows_does_not_mix_aggregate_reset_between_windows():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/aggregate",
            payload={
                "five_hour_usage_limit": {"used_percent": 3},
                "weekly_usage_limit": {
                    "used_percent": 45,
                    "reset_at": "2026-06-10T05:05:00+02:00",
                },
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 97
    assert five.reset_at is None
    assert five.source.endswith("aggregate")
    assert weekly is not None
    assert weekly.remaining == 55
    assert weekly.reset_at is not None
    assert weekly.reset_at.strftime("%d.%m.%Y %H:%M") == "10.06.2026 05:05"


def test_extract_windows_prefers_absolute_usage_over_conflicting_json_used_percent():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "used": 8,
                    "limit": 40,
                    "used_percent": 3,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                }
            },
        )
    ]

    five, _weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used == 8
    assert five.limit == 40
    assert five.remaining == 32
    assert five.percent == 80


def test_extract_windows_derives_remaining_percent_over_conflicting_remaining_field():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "used": 42,
                    "limit": 100,
                    "remaining_percent": 99,
                }
            },
        )
    ]

    five, _weekly = extract_windows(body_text="", json_candidates=candidates)

    assert five is not None
    assert five.used == 42
    assert five.remaining == 58
    assert five.percent == 58


def test_extract_windows_scales_used_percent_against_absolute_limit():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "used_percent": 3,
                    "limit": 1000,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                },
                "weekly_usage_limit": {
                    "used_ratio": 0.45,
                    "limit": 400,
                    "reset_at": "2026-06-10T05:05:00+02:00",
                },
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.limit == 1000
    assert five.remaining == 970
    assert five.percent == 97
    assert weekly is not None
    assert weekly.limit == 400
    assert weekly.remaining == 220
    assert round(weekly.percent, 6) == 55


def test_extract_windows_converts_generic_used_percent_to_remaining():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "used_percent": 3,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                },
                "weekly_usage_limit": {
                    "used_percent": 45,
                    "reset_at": "2026-06-10T05:05:00+02:00",
                },
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used is None
    assert five.remaining == 97
    assert five.percent == 97
    assert weekly is not None
    assert weekly.used is None
    assert weekly.remaining == 55
    assert weekly.percent == 55


def test_extract_windows_converts_absolute_remaining_with_limit_to_percent():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "remaining": 690,
                    "limit": 1000,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                }
            },
        )
    ]

    five, _weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used is None
    assert five.limit == 1000
    assert five.remaining == 690
    assert five.percent == 69


def test_extract_windows_preserves_generic_one_percent_fields():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "used_percent": 1,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                },
                "weekly_usage_limit": {
                    "remaining_percent": 1,
                    "reset_at": "2026-06-10T05:05:00+02:00",
                },
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 99
    assert five.percent == 99
    assert weekly is not None
    assert weekly.remaining == 1
    assert weekly.percent == 1


def test_extract_windows_normalizes_generic_standalone_ratio():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "ratio": 0.97,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                }
            },
        )
    ]

    five, _weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining is None
    assert five.percent == 97


def test_extract_windows_does_not_treat_duration_as_generic_limit():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "used": 3,
                    "limit_window_seconds": 18_000,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                }
            },
        )
    ]

    five, _weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used == 3
    assert five.limit is None
    assert five.remaining is None


def test_extract_windows_converts_used_percentage_alias_to_remaining():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "used_percentage": 3,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                }
            },
        )
    ]

    five, _weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used is None
    assert five.remaining == 97
    assert five.percent == 97


def test_extract_windows_converts_remaining_ratio_to_percent():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "remaining_ratio": 0.97,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                },
                "weekly_usage_limit": {
                    "available_ratio": 0.55,
                    "reset_at": "2026-06-10T05:05:00+02:00",
                },
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 97
    assert five.percent == 97
    assert weekly is not None
    assert round(weekly.remaining, 6) == 55
    assert round(weekly.percent, 6) == 55


def test_extract_windows_converts_used_ratio_to_remaining():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "used_ratio": 0.03,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                },
                "weekly_usage_limit": {
                    "usage_ratio": 0.45,
                    "reset_at": "2026-06-10T05:05:00+02:00",
                },
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used is None
    assert five.remaining == 97
    assert five.percent == 97
    assert weekly is not None
    assert weekly.used is None
    assert round(weekly.remaining, 6) == 55
    assert round(weekly.percent, 6) == 55


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


def test_extract_windows_from_wham_derives_missing_reset_from_relative_seconds():
    now = datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin"))
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 3,
                        "limit_window_seconds": 18_000,
                        "reset_after_seconds": 13_665,
                    },
                    "secondary_window": {
                        "used_percent": 45,
                        "limit_window_seconds": 604_800,
                        "reset_after_seconds": 180_164,
                    },
                }
            },
        )
    ]

    five, weekly = extract_windows(body_text="", json_candidates=candidates, now=now)

    assert five is not None
    assert five.reset_at == now + timedelta(seconds=13_665)
    assert weekly is not None
    assert weekly.reset_at == now + timedelta(seconds=180_164)


def test_extract_windows_prefers_latest_equal_priority_wham_response():
    def candidate(five_hour: int, weekly: int) -> JsonCandidate:
        return JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "rate_limit": {
                    "primary_window": {
                        "used_percent": five_hour,
                        "limit_window_seconds": 18000,
                    },
                    "secondary_window": {
                        "used_percent": weekly,
                        "limit_window_seconds": 604800,
                    },
                }
            },
        )

    five, weekly = extract_windows(
        body_text="",
        json_candidates=[candidate(3, 45), candidate(20, 60)],
    )

    assert five is not None
    assert five.used == 20
    assert weekly is not None
    assert weekly.used == 60


def test_extract_windows_prefers_newer_usage_over_older_reset_metadata():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 3,
                        "limit_window_seconds": 18000,
                        "reset_at": "1780894250",
                    }
                }
            },
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 20,
                        "limit_window_seconds": 18000,
                    }
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
    assert five.used == 20
    assert five.remaining == 80
    assert five.reset_at is None


def test_extract_windows_prefers_newer_generic_usage_over_older_reset_metadata():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "used": 3,
                    "limit": 100,
                    "reset_at": "1780894250",
                }
            },
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "used": 20,
                    "limit": 100,
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
    assert five.used == 20
    assert five.remaining == 80
    assert five.reset_at is None


def test_extract_windows_prefers_main_rate_limit_over_additional_limits():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "additional_rate_limits": [
                    {
                        "limit_name": "GPT-5.3-Codex-Spark",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 0,
                                "limit_window_seconds": 18_000,
                            },
                            "secondary_window": {
                                "used_percent": 0,
                                "limit_window_seconds": 604_800,
                            },
                        },
                    }
                ],
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 9,
                        "limit_window_seconds": 18_000,
                    },
                    "secondary_window": {
                        "used_percent": 4,
                        "limit_window_seconds": 604_800,
                    },
                },
            },
        )
    ]

    five, weekly = extract_windows(body_text="", json_candidates=candidates)

    assert five is not None
    assert five.used == 9
    assert five.remaining == 91
    assert weekly is not None
    assert weekly.used == 4
    assert weekly.remaining == 96


def test_extract_windows_blocks_additional_limits_for_unrecognized_wham_url():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/capture",
        payload={
            "additional_rate_limits": [
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 0,
                            "limit_window_seconds": 604800,
                        }
                    }
                }
            ],
            "rate_limit": {
                "primary_window": {
                    "used_percent": 2,
                    "limit_window_seconds": 604800,
                },
                "secondary_window": None,
            },
        },
    )

    _five, weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert weekly is not None
    assert weekly.used == 2
    assert weekly.remaining == 98


def test_extract_windows_does_not_use_additional_limits_for_unsupported_main_windows():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "additional_rate_limits": [
                    {
                        "limit_name": "GPT-5.3-Codex-Spark",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 0,
                                "limit_window_seconds": 18_000,
                            },
                            "secondary_window": {
                                "used_percent": 0,
                                "limit_window_seconds": 604_800,
                            },
                        },
                    }
                ],
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 5,
                        "limit_window_seconds": 2_592_000,
                    },
                    "secondary_window": None,
                },
            },
        )
    ]

    five, weekly = extract_windows(body_text="", json_candidates=candidates)

    assert five is None
    assert weekly is None


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


def test_extract_windows_prefers_complete_wham_usage_over_earlier_partial_usage():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/partial",
            payload={
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 3,
                        "limit_window_seconds": 18_000,
                    }
                }
            },
        ),
        JsonCandidate(
            url="https://chatgpt.com/backend-api/fresh",
            payload={
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 3,
                        "limit_window_seconds": 18_000,
                        "reset_at": "1780894250",
                    }
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
    assert five.reset_at is not None
    assert five.source.endswith("fresh")


def test_extract_windows_ignores_camel_case_relative_reset_before_absolute_reset():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "resetAfterSeconds": 13665,
                    "resetsAt": "2026-06-08T06:50:00+02:00",
                    "used": 42,
                    "limit": 100,
                }
            },
        )
    ]

    five, _weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "08.06.2026 06:50"


def test_extract_windows_from_wham_normalizes_ratio_fields():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "rate_limit": {
                    "primary_window": {
                        "used_ratio": 0.03,
                        "limit_window_seconds": 18_000,
                        "reset_at": "1780894250",
                    },
                    "secondary_window": {
                        "remaining_ratio": 0.55,
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
    assert five.used == 3
    assert five.limit == 100
    assert five.remaining == 97
    assert five.percent == 97
    assert weekly is not None
    assert weekly.used is None
    assert weekly.limit is None
    assert round(weekly.remaining, 6) == 55
    assert round(weekly.percent, 6) == 55


def test_extract_windows_from_wham_preserves_one_percent():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/wham/usage",
            payload={
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "limit_window_seconds": 18_000,
                        "reset_at": "1780894250",
                    }
                }
            },
        )
    ]

    five, _weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used == 1
    assert five.limit == 100
    assert five.remaining == 99
    assert five.percent == 99
