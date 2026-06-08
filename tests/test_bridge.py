from __future__ import annotations

import json

from codex_usage.bridge import (
    render_bridge_snippet,
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
    assert "document.documentElement.innerText" in content
    assert "MutationObserver" in content
    assert "readyState" in content
    assert "textLength" in content
    assert "BW_Privat" in content
    assert "300000" in content
