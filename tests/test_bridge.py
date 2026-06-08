from __future__ import annotations

import json

from codex_usage.bridge import (
    render_bridge_snippet,
    save_bridge_debug_payload,
    usage_from_ingest_payload,
    write_bridge_extension,
)
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


def test_usage_from_ingest_payload_reports_empty_text_context():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "title": "Codex",
            "readyState": "complete",
            "textLength": 0,
            "bodyText": "",
        },
    )

    assert usage.status == AccountStatus.PARTIAL
    assert usage.error is not None
    assert "missing page text" in usage.error
    assert "ready=complete" in usage.error
    assert "textLength=0" in usage.error


def test_usage_from_ingest_payload_uses_full_dom_payload_fields():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "bodyText": "Codex analytics",
            "accessibilityText": """
            5-hour limit 42 / 100 Reset 08.06.2026 04:26
            Weekly limit 310 / 1000 Reset 14.06.2026 04:26
            """,
        },
    )

    assert usage.status == AccountStatus.OK
    assert usage.five_hour is not None
    assert usage.five_hour.used == 42
    assert usage.weekly is not None
    assert usage.weekly.limit == 1000


def test_usage_from_ingest_payload_extracts_api_responses():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "bodyText": "Codex analytics",
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage?secret=1",
                    "status": 200,
                    "contentType": "application/json",
                    "bodyText": json.dumps(
                        {
                            "five_hour_usage_limit": {
                                "used": 42,
                                "limit": 100,
                                "reset_at": "2026-06-08T04:26:00+02:00",
                            },
                            "weekly_usage_limit": {
                                "used": 310,
                                "limit": 1000,
                                "reset_at": "2026-06-14T04:26:00+02:00",
                            },
                        }
                    ),
                }
            ],
        },
    )

    assert usage.status == AccountStatus.OK
    assert usage.source_urls == (
        "https://chatgpt.com/backend-api/wham/usage",
        "https://chatgpt.com/codex/cloud/settings/analytics",
    )
    assert usage.five_hour is not None
    assert usage.five_hour.used == 42
    assert usage.weekly is not None
    assert usage.weekly.limit == 1000


def test_usage_from_ingest_payload_ignores_failed_html_api_responses():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics",
            "bodyText": "Codex analytics",
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 404,
                    "contentType": "text/html",
                    "bodyText": (
                        "<html><body>marketing 97 55 five_hour_usage_limit "
                        "weekly_usage_limit</body></html>"
                    ),
                },
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage",
                    "status": 200,
                    "contentType": "application/json; charset=utf-8",
                    "bodyText": json.dumps(
                        {
                            "five_hour_usage_limit": {
                                "used": 97,
                                "limit": 100,
                                "reset_at": "2026-06-08T04:26:00+02:00",
                            },
                            "weekly_usage_limit": {
                                "used": 55,
                                "limit": 1000,
                                "reset_at": "2026-06-14T04:26:00+02:00",
                            },
                        }
                    ),
                },
            ],
        },
    )

    assert usage.status == AccountStatus.OK
    assert usage.five_hour is not None
    assert usage.five_hour.used == 97
    assert usage.weekly is not None
    assert usage.weekly.used == 55


def test_usage_from_ingest_payload_reports_search_excerpt():
    account = Account(id="privat", label="Privat", profile_dir="/tmp/profile")
    usage = usage_from_ingest_payload(
        account,
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics?secret=1",
            "title": "Codex",
            "readyState": "complete",
            "textLength": 123,
            "bodyText": "Codex analytics without the expected limit labels",
        },
    )

    assert usage.status == AccountStatus.PARTIAL
    assert usage.error is not None
    assert "usage limits not found" in usage.error
    assert "https://chatgpt.com/codex/cloud/settings/analytics" in usage.error
    assert "secret=1" not in usage.error
    assert 'excerpt="Codex analytics without the expected limit labels"' in usage.error


def test_save_bridge_debug_payload_redacts_url_and_locks_file(tmp_path):
    path = save_bridge_debug_payload(
        "BW/Privat",
        {
            "url": "https://chatgpt.com/codex/cloud/settings/analytics?secret=1",
            "bodyText": "user@example.test 1 / 2",
            "htmlText": (
                "<html><script>"
                '"accessToken":"aaa.bbb.ccc","sessionToken":"ddd.eee.fff"'
                "</script><body>debug</body></html>"
            ),
            "apiResponses": [
                {
                    "url": "https://chatgpt.com/backend-api/wham/usage?secret=1",
                    "bodyText": '{"accessToken":"aaa.bbb.ccc","email":"user@example.test"}',
                }
            ],
        },
        tmp_path / "snapshots",
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "BW_Privat-last-ingest.json"
    assert payload["url"] == "https://chatgpt.com/codex/cloud/settings/analytics"
    assert payload["bodyText"] == "[redacted.email] 1 / 2"
    assert "accessToken" not in payload["htmlText"]
    assert "sessionToken" not in payload["htmlText"]
    assert "<script>[redacted]</script>" in payload["htmlText"]
    assert "<body>debug</body>" in payload["htmlText"]
    assert payload["apiResponses"][0]["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert "aaa.bbb.ccc" not in payload["apiResponses"][0]["bodyText"]
    assert "user@example.test" not in payload["apiResponses"][0]["bodyText"]
    assert path.stat().st_mode & 0o077 == 0


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


def test_write_bridge_extension_creates_vivaldi_compatible_files(tmp_path):
    output = write_bridge_extension(
        "BW_Privat",
        tmp_path / "extension",
        endpoint="http://127.0.0.1:8765/ingest",
        interval_seconds=300,
    )

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    background = (output / "background.js").read_text(encoding="utf-8")
    content = (output / "content.js").read_text(encoding="utf-8")

    assert manifest["manifest_version"] == 3
    assert "https://chatgpt.com/*" in manifest["host_permissions"]
    assert "http://127.0.0.1:8765/*" in manifest["host_permissions"]
    assert manifest["content_scripts"][0]["matches"] == [
        "https://chatgpt.com/codex/cloud/settings/analytics*"
    ]
    assert "fetch(ENDPOINT" in background
    assert "chrome.runtime.sendMessage" in content
    assert "/backend-api/wham/usage" in content
    assert '"/wham/usage"' not in content
    assert "fetchCodexUsageApis" in content
    assert "apiResponses" in content
    assert 'credentials: "include"' in content
    assert "looksLikeCodexUsageJson" in content
    assert "bodyExcerpt" in content
    assert "stopCodexUsageBridge" in content
    assert "extension context invalidated" in content
    assert "codexUsageIntervalId = setInterval" in content
    assert "document.body.innerText" in content
    assert "sanitizedCodexUsageRoot" in content
    assert "script, style, link, meta, noscript, template" in content
    assert "sanitizedRoot.outerHTML" in content
    assert "collectCodexUsageAttributeText" in content
    assert "collectCodexUsageSvgText" in content
    assert "fieldLengths" in content
    assert "truncatedFields" in content
    assert "visibleTextLength" in content
    assert "CODEX_USAGE_READY_TIMEOUT_MS = 60000" in content
    assert "htmlText" in content
    assert "MutationObserver" in content
    assert "readyState" in content
    assert "textLength" in content
    assert "BW_Privat" in content
    assert "300000" in content
