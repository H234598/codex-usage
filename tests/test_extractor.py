from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from codex_usage.extractor import (
    JsonCandidate,
    _coerce_number,
    _parse_datetime,
    _parse_time_today_or_next,
    _relative_reset_at,
    extract_windows,
)


def test_numeric_coercion_rejects_integer_overflow_without_raising():
    assert _coerce_number(10**10000) is None


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


def test_parse_datetime_uses_dst_aware_local_zone_for_fixed_capture_offset(monkeypatch):
    berlin = ZoneInfo("Europe/Berlin")
    monkeypatch.setattr("codex_usage.extractor.LOCAL_TZ", berlin)
    captured_at = datetime(2026, 3, 28, 12, tzinfo=timezone(timedelta(hours=1)))
    expected = datetime(2026, 3, 30, 1, tzinfo=berlin)

    assert _parse_datetime(expected.timestamp(), captured_at) == expected


def test_relative_reset_at_adds_elapsed_seconds_across_dst(monkeypatch):
    berlin = ZoneInfo("Europe/Berlin")
    monkeypatch.setattr("codex_usage.extractor.LOCAL_TZ", berlin)
    captured_at = datetime(2026, 3, 28, 12, tzinfo=timezone(timedelta(hours=1)))

    assert _relative_reset_at(36 * 60 * 60, captured_at) == datetime(
        2026, 3, 30, 1, tzinfo=berlin
    )


def test_parse_time_today_or_next_uses_next_day_dst_offset(monkeypatch):
    berlin = ZoneInfo("Europe/Berlin")
    monkeypatch.setattr("codex_usage.extractor.LOCAL_TZ", berlin)
    captured_at = datetime(2026, 10, 25, 23, 30, tzinfo=timezone(timedelta(hours=2)))

    assert _parse_time_today_or_next("00:15", captured_at) == datetime(
        2026, 10, 26, 0, 15, tzinfo=berlin
    )


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


def test_extract_windows_text_uses_used_percent_over_absolute_remaining():
    body = """
    5-hour limit
    3% used
    970 remaining
    Reset 08.06.2026 04:26
    """

    five, _weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.remaining == 97
    assert five.percent == 97


def test_extract_windows_text_rejects_denominatorless_absolute_remaining():
    body = """
    5-hour limit
    690 remaining
    Reset 08.06.2026 04:26
    """

    five, _weekly = extract_windows(
        body_text=body,
        now=datetime(2026, 6, 8, 3, 3, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.has_usage_value is False
    assert five.reset_at is not None


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


def test_extract_windows_uses_authoritative_html_progress_over_stale_body_percent():
    body = (
        "5-hour limit 20% remaining Reset 12.07.2026 18:00 "
        "Weekly limit 10% remaining Reset 18.07.2026 07:00"
    )
    html = """
    <h2>5-hour limit</h2>
    <div class="transition-[width] rounded-full bg-[#22c55e]" style="width: 97%;"></div>
    <h2>Weekly limit</h2>
    <div class="transition-[width] rounded-full bg-[#22c55e]" style="width: 55%;"></div>
    """

    five, weekly = extract_windows(
        body_text="",
        text_sources=(("bodyText", body), ("htmlText", html)),
    )

    assert five is not None and five.remaining == 97
    assert weekly is not None and weekly.remaining == 55
    assert five.source == "htmlText"


def test_extract_windows_keeps_absolute_body_usage_over_html_progress():
    body = "5-hour limit 42 / 100 used Reset 12.07.2026 18:00"
    html = """
    <h2>5-hour limit</h2>
    <div class="transition-[width] rounded-full bg-[#22c55e]" style="width: 97%;"></div>
    """

    five, _weekly = extract_windows(
        body_text="",
        text_sources=(("bodyText", body), ("htmlText", html)),
    )

    assert five is not None
    assert five.used == 42
    assert five.remaining == 58
    assert five.source == "bodyText"


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


def test_extract_windows_clamps_over_limit_absolute_usage_to_zero_remaining():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "used": 120,
                    "limit": 100,
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
    assert five.used == 120
    assert five.limit == 100
    assert five.remaining == 0
    assert five.percent == 0


def test_extract_windows_discards_negative_absolute_usage():
    candidates = [
        JsonCandidate(
            url="https://example.test/usage",
            payload={
                "five_hour_usage_limit": {
                "used": -1,
                "limit": 100,
                "remaining": 80,
                "reset_at": "2026-07-13T20:00:00+02:00",
                }
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 7, 13, 18, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used is None
    assert five.limit == 100
    assert five.remaining is None
    assert five.percent is None
    assert five.has_usage_value is False
    assert weekly is None


def test_extract_windows_normalizes_negative_remaining_to_exhausted():
    candidates = [
        JsonCandidate(
            url="https://example.test/usage",
            payload={
                "five_hour_usage_limit": {
                    "remaining": -1,
                    "limit": 100,
                    "reset_at": "2026-07-13T20:00:00+02:00",
                }
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 7, 13, 18, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.limit == 100
    assert five.remaining == 0
    assert five.percent == 0
    assert weekly is None


def test_extract_text_window_clamps_over_limit_absolute_usage_to_zero_remaining():
    five, _weekly = extract_windows(
        body_text="5-hour limit 120 / 100 used Reset 08.06.2026 06:50",
        now=datetime(2026, 6, 8, 4, 20, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used == 120
    assert five.limit == 100
    assert five.remaining == 0
    assert five.percent == 0


def test_extract_windows_rejects_conflicting_generic_percentage_fields():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "used_percent": 3,
                    "remaining_percent": 1,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                }
            },
        )
    ]

    five, _weekly = extract_windows(body_text="", json_candidates=candidates)

    assert five is not None
    assert five.has_usage_value is False
    assert five.reset_at is not None


def test_extract_windows_rejects_conflicting_standalone_percent_field():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/generic",
        payload={
            "five_hour_usage_limit": {
                "used_percent": 3,
                "percent": 45,
                "reset_at": "2026-06-08T06:50:00+02:00",
            }
        },
    )

    five, _weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is not None
    assert five.has_usage_value is False
    assert five.reset_at is not None


@pytest.mark.parametrize(
    "fields",
    [
        {"used_percent": 3, "usage_percent": 45},
        {"remaining_percent": 97, "available_percent": 55},
        {"used_ratio": 0.03, "consumed_ratio": 0.45},
        {"percent": 97, "percentage": 55},
    ],
)
def test_extract_windows_rejects_conflicting_generic_percentage_aliases(fields):
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/generic",
        payload={
            "five_hour_usage_limit": {
                **fields,
                "reset_at": "2026-06-08T06:50:00+02:00",
            }
        },
    )

    five, _weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is not None
    assert five.has_usage_value is False
    assert five.reset_at is not None


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


def test_extract_windows_scopes_target_specific_root_fields():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/generic",
        payload={
            "five_hour_used_percent": 3,
            "weekly_used_percent": 45,
            "five_hour_reset": "2026-06-08T06:50:00+02:00",
            "weekly_reset": "2026-06-10T05:05:00+02:00",
        },
    )

    five, weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is not None
    assert five.remaining == 97
    assert five.reset_at is not None
    assert five.reset_at.strftime("%d.%m.%Y %H:%M") == "08.06.2026 06:50"
    assert weekly is not None
    assert weekly.remaining == 55
    assert weekly.reset_at is not None
    assert weekly.reset_at.strftime("%d.%m.%Y %H:%M") == "10.06.2026 05:05"


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


def test_extract_windows_uses_used_percent_over_absolute_remaining_without_limit():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "used_percent": 3,
                    "remaining": 970,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                }
            },
        )
    ]

    five, _weekly = extract_windows(body_text="", json_candidates=candidates)

    assert five is not None
    assert five.remaining == 97
    assert five.percent == 97


def test_extract_windows_discards_non_positive_absolute_limit():
    candidates = [
        JsonCandidate(
            url="https://example.test/usage",
            payload={
                "five_hour_usage_limit": {
                    "used": 0,
                    "limit": 0,
                    "reset_at": "2026-07-13T20:00:00+02:00",
                }
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 7, 13, 18, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used is None
    assert five.limit is None
    assert five.remaining is None
    assert five.percent is None
    assert five.reset_at is not None
    assert weekly is None


@pytest.mark.parametrize("remaining", [0, 50, 690])
def test_extract_windows_discards_unqualified_remaining_with_non_positive_limit(
    remaining,
):
    candidates = [
        JsonCandidate(
            url="https://example.test/usage",
            payload={
                "five_hour_usage_limit": {
                    "used": 0,
                    "limit": 0,
                    "remaining": remaining,
                    "reset_at": "2026-07-13T20:00:00+02:00",
                }
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 7, 13, 18, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.has_usage_value is False
    assert five.remaining is None
    assert five.percent is None
    assert five.reset_at is not None
    assert weekly is None


def test_extract_windows_preserves_explicit_percent_with_non_positive_limit():
    candidates = [
        JsonCandidate(
            url="https://example.test/usage",
            payload={
                "five_hour_usage_limit": {
                    "used": 0,
                    "limit": 0,
                    "remaining_percent": 50,
                    "reset_at": "2026-07-13T20:00:00+02:00",
                }
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 7, 13, 18, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used is None
    assert five.limit is None
    assert five.remaining == 50
    assert five.percent == 50
    assert weekly is None


@pytest.mark.parametrize("remaining", [0, 50, 690])
def test_extract_windows_discards_remaining_with_standalone_non_positive_limit(
    remaining,
):
    candidates = [
        JsonCandidate(
            url="https://example.test/usage",
            payload={
                "five_hour_usage_limit": {
                    "limit": 0,
                    "remaining": remaining,
                    "reset_at": "2026-07-13T20:00:00+02:00",
                }
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 7, 13, 18, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.limit is None
    assert five.remaining is None
    assert five.percent is None
    assert five.has_usage_value is False
    assert five.reset_at is not None
    assert weekly is None


def test_extract_windows_prefers_explicit_remaining_percent_without_limit():
    candidates = [
        JsonCandidate(
            url="https://example.test/usage",
            payload={
                "five_hour_usage_limit": {
                    "used": 1000,
                    "remaining": 101,
                    "remaining_percent": 0,
                    "reset_at": "2026-07-13T20:00:00+02:00",
                }
            },
        )
    ]

    five, weekly = extract_windows(
        body_text="",
        json_candidates=candidates,
        now=datetime(2026, 7, 13, 18, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used == 1000
    assert five.limit is None
    assert five.remaining == 0
    assert five.percent == 0
    assert weekly is None


@pytest.mark.parametrize("remaining", [0, 50, 690])
def test_extract_text_window_discards_unqualified_remaining_with_non_positive_limit(
    remaining,
):
    five, weekly = extract_windows(
        body_text=(
            f"5-hour limit 0 / 0 used {remaining} remaining "
            "Reset 13.07.2026 20:00"
        ),
        json_candidates=(),
        now=datetime(2026, 7, 13, 18, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.has_usage_value is False
    assert five.used is None
    assert five.limit is None
    assert five.remaining is None
    assert five.percent is None
    assert five.reset_at is not None
    assert weekly is None


def test_extract_text_window_preserves_explicit_percent_with_non_positive_limit():
    five, weekly = extract_windows(
        body_text="5-hour limit 0 / 0 used 50% remaining Reset 13.07.2026 20:00",
        json_candidates=(),
        now=datetime(2026, 7, 13, 18, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert five is not None
    assert five.used is None
    assert five.limit is None
    assert five.remaining == 50
    assert five.percent == 50
    assert five.reset_at is not None
    assert weekly is None


def test_extract_windows_rejects_denominatorless_absolute_remaining_value():
    candidates = [
        JsonCandidate(
            url="https://chatgpt.com/backend-api/generic",
            payload={
                "five_hour_usage_limit": {
                    "remaining": 690,
                    "reset_at": "2026-06-08T06:50:00+02:00",
                }
            },
        )
    ]

    five, _weekly = extract_windows(body_text="", json_candidates=candidates)

    assert five is not None
    assert five.has_usage_value is False
    assert five.reset_at is not None


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


def test_extract_windows_does_not_promote_unscoped_duration_metadata():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={
            "metadata": {
                "limit_window_seconds": 604_800,
                "used_percent": 55,
                "reset_at": "2026-06-10T05:05:00+02:00",
            }
        },
    )

    _five, weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert weekly is None


def test_extract_windows_accepts_scoped_generic_duration_window():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={
            "weekly_usage_limit": {
                "limit_window_seconds": 604_800,
                "used_percent": 55,
                "reset_at": "2026-06-10T05:05:00+02:00",
            }
        },
    )

    _five, weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert weekly is not None
    assert weekly.remaining == 45


@pytest.mark.parametrize(
    "reset_key",
    (
        "reset_after_seconds",
        "reset_seconds",
        "reset_in_seconds",
        "seconds_until_reset",
        "reset_duration",
    ),
)
def test_extract_windows_converts_generic_relative_reset_fields(reset_key):
    now = datetime(2026, 7, 16, 1, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    candidate = JsonCandidate(
        url="https://example.test/usage",
        payload={
            "five_hour_usage_limit": {
                "used_percent": 3,
                reset_key: 900,
            }
        },
    )

    five, _weekly = extract_windows(
        body_text="", json_candidates=[candidate], now=now
    )

    assert five is not None
    assert five.reset_at == now + timedelta(seconds=900)


def test_extract_windows_prefers_absolute_reset_over_relative_reset_field():
    now = datetime(2026, 7, 16, 1, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    candidate = JsonCandidate(
        url="https://example.test/usage",
        payload={
            "five_hour_usage_limit": {
                "used_percent": 3,
                "reset_after_seconds": 900,
                "reset_at": "2026-07-16T04:00:00+02:00",
            }
        },
    )

    five, _weekly = extract_windows(
        body_text="", json_candidates=[candidate], now=now
    )

    assert five is not None
    assert five.reset_at == datetime(
        2026, 7, 16, 4, 0, tzinfo=ZoneInfo("Europe/Berlin")
    )


@pytest.mark.parametrize(
    ("primary_duration", "secondary_duration", "target"),
    [
        (18_000, 18_000, "five_hour"),
        (604_800, 604_800, "weekly"),
    ],
)
def test_extract_windows_rejects_duplicate_wham_target_buckets(
    primary_duration,
    secondary_duration,
    target,
):
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={
            "rate_limit": {
                "primary_window": {
                    "used_percent": 3,
                    "limit_window_seconds": primary_duration,
                },
                "secondary_window": {
                    "used_percent": 45,
                    "limit_window_seconds": secondary_duration,
                },
            }
        },
    )

    five, weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert (five if target == "five_hour" else weekly) is None


def test_extract_windows_rejects_conflicting_wham_percentage_fields():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={
            "rate_limit": {
                "primary_window": {
                    "used_percent": 3,
                    "remaining_percent": 1,
                    "limit_window_seconds": 18_000,
                    "reset_at": 1780894250,
                }
            }
        },
    )

    five, _weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is not None
    assert five.has_usage_value is False
    assert five.reset_at is not None


@pytest.mark.parametrize(
    "fields",
    [
        {"used_percent": 3, "usage_percent": 45},
        {"remaining_percent": 97, "available_percent": 55},
        {"used_ratio": 0.03, "consumed_ratio": 0.45},
    ],
)
def test_extract_windows_rejects_conflicting_wham_percentage_aliases(fields):
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={
            "rate_limit": {
                "primary_window": {
                    **fields,
                    "limit_window_seconds": 18_000,
                    "reset_at": 1780894250,
                }
            }
        },
    )

    five, _weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is not None
    assert five.has_usage_value is False
    assert five.reset_at is not None


def test_extract_windows_separates_durationless_structural_buckets():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/analytics",
        payload={
            "five_hour_and_weekly_usage_limits": {
                "primary_window": {"used_percent": 3},
                "secondary_window": {"used_percent": 45},
            }
        },
    )

    five, weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is not None
    assert five.remaining == 97
    assert weekly is not None
    assert weekly.remaining == 55


def test_extract_windows_does_not_promote_primary_when_secondary_is_null():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/analytics",
        payload={
            "five_hour_and_weekly_usage_limits": {
                "primary_window": {"used_percent": 3},
                "secondary_window": None,
            }
        },
    )

    five, weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is not None
    assert five.remaining == 97
    assert weekly is None


def test_extract_windows_does_not_treat_window_config_as_structural_window():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/analytics",
        payload={
            "five_hour_usage_limits": {
                "primary_window_config": {"used_percent": 3},
                "weekly_window_config": {"used_percent": 45},
            }
        },
    )

    five, weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is not None
    assert five.remaining == 97
    assert weekly is not None
    assert weekly.remaining == 55


def test_extract_windows_accepts_scope_named_buckets_without_limit_keyword():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/analytics",
        payload={
            "five_hour": {"used_percent": 3},
            "weekly": {"used_percent": 45},
        },
    )

    five, weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is not None
    assert five.remaining == 97
    assert weekly is not None
    assert weekly.remaining == 55


def test_extract_windows_accepts_durationless_structural_buckets_in_container():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/analytics",
        payload={
            "wrapper": {
                "primary_window": {"used_percent": 3},
                "secondary_window": {"used_percent": 45},
            }
        },
    )

    five, weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is not None
    assert five.remaining == 97
    assert weekly is not None
    assert weekly.remaining == 55


def test_extract_windows_accepts_scope_named_buckets_in_list():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/analytics",
        payload={
            "windows": [
                {"type": "five_hour", "used_percent": 3},
                {"type": "weekly", "used_percent": 45},
            ]
        },
    )

    five, weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is not None
    assert five.remaining == 97
    assert weekly is not None
    assert weekly.remaining == 55


def test_extract_windows_prefers_target_scoped_values_over_unscoped_fields():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/analytics",
        payload={
            "used_percent": 88,
            "five_hour_usage_limit": {"used_percent": 3},
            "weekly_usage_limit": {"used_percent": 45},
        },
    )

    five, weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is not None
    assert five.remaining == 97
    assert weekly is not None
    assert weekly.remaining == 55


def test_extract_windows_does_not_promote_nested_opposite_scope_values():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/analytics",
        payload={
            "five_hour_and_weekly_usage_limits": {
                "primary_window": {"weekly": {"used_percent": 91}},
                "secondary_window": {"weekly": {"used_percent": 45}},
            }
        },
    )

    five, weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is None
    assert weekly is not None
    assert weekly.remaining == 55


def test_extract_windows_does_not_promote_nested_weekly_under_primary_bucket():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/analytics",
        payload={
            "wrapper": {
                "primary_window": {
                    "weekly": {"used_percent": 91},
                }
            }
        },
    )

    five, weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is None
    assert weekly is None


def test_extract_windows_does_not_promote_unsupported_structural_buckets():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/analytics",
        payload={
            "five_hour_and_weekly_usage_limits": {
                "primary_window": {
                    "used_percent": 5,
                    "limit_window_seconds": 2_592_000,
                },
                "secondary_window": {
                    "used_percent": 45,
                    "limit_window_seconds": 2_592_000,
                },
            }
        },
    )

    five, weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is None
    assert weekly is None


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


def test_extract_windows_from_wham_converts_generic_relative_reset_field():
    now = datetime(2026, 7, 16, 1, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={
            "rate_limit": {
                "primary_window": {
                    "used_percent": 3,
                    "limit_window_seconds": 18_000,
                    "reset_seconds": 900,
                }
            }
        },
    )

    five, _weekly = extract_windows(
        body_text="", json_candidates=[candidate], now=now
    )

    assert five is not None
    assert five.reset_at == now + timedelta(seconds=900)


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


def test_extract_windows_does_not_use_additional_usage_for_reset_only_main_bucket():
    candidate = JsonCandidate(
        url="https://chatgpt.com/backend-api/wham/usage",
        payload={
            "additional_rate_limits": [
                {
                    "limit_name": "GPT-5.3-Codex-Spark",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 1,
                            "limit_window_seconds": 18_000,
                        }
                    },
                }
            ],
            "rate_limit": {
                "primary_window": {
                    "limit_window_seconds": 18_000,
                    "reset_after_seconds": 900,
                }
            },
        },
    )

    five, _weekly = extract_windows(body_text="", json_candidates=[candidate])

    assert five is not None
    assert five.has_usage_value is False
    assert five.reset_at is not None


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
