from __future__ import annotations

import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import AppConfig, resolve_account
from .extractor import extract_windows
from .models import Account, AccountStatus, AccountUsage
from .render import render_table
from .state import load_usage_snapshot, save_usage_snapshot

MAX_INGEST_BYTES = 2_000_000


def usage_from_ingest_payload(account: Account, payload: dict[str, Any]) -> AccountUsage:
    captured_at = _parse_captured_at(payload.get("capturedAt") or payload.get("captured_at"))
    body_text = str(
        payload.get("bodyText")
        or payload.get("body_text")
        or payload.get("text")
        or payload.get("innerText")
        or ""
    )
    five_hour, weekly = extract_windows(body_text=body_text, now=captured_at)
    status = AccountStatus.OK if five_hour and weekly else AccountStatus.PARTIAL
    error = None if body_text.strip() else "missing page text"
    source_url = _redact_url(str(payload.get("url") or ""))
    return AccountUsage(
        account_id=account.id,
        label=account.label,
        captured_at=captured_at,
        five_hour=five_hour,
        weekly=weekly,
        status=status,
        error=error,
        source_urls=(source_url,) if source_url else (),
    )


def render_bridge_snippet(account_ref: str, *, endpoint: str, interval_seconds: int) -> str:
    account_json = json.dumps(account_ref)
    endpoint_json = json.dumps(endpoint)
    interval_ms = max(interval_seconds, 60) * 1000
    return f"""(() => {{
  const account = {account_json};
  const endpoint = {endpoint_json};
  const intervalMs = {interval_ms};
  async function sendCodexUsage() {{
    const payload = {{
      account,
      url: location.href,
      title: document.title,
      capturedAt: new Date().toISOString(),
      bodyText: document.body ? document.body.innerText : ""
    }};
    const response = await fetch(endpoint, {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify(payload)
    }});
    console.log("codex-usage bridge", response.status, await response.text());
  }}
  sendCodexUsage();
  setInterval(sendCodexUsage, intervalMs);
}})();"""


def write_bridge_extension(
    account_ref: str,
    output_dir: Path,
    *,
    endpoint: str,
    interval_seconds: int,
) -> Path:
    output_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    manifest = {
        "manifest_version": 3,
        "name": f"codex-usage bridge ({account_ref})",
        "version": "0.1.0",
        "description": (
            "Exports visible ChatGPT Codex analytics text to the local codex-usage bridge."
        ),
        "host_permissions": [
            "https://chatgpt.com/*",
            endpoint.rsplit("/", 1)[0] + "/*",
        ],
        "background": {"service_worker": "background.js"},
        "content_scripts": [
            {
                "matches": ["https://chatgpt.com/codex/cloud/settings/analytics*"],
                "js": ["content.js"],
                "run_at": "document_idle",
            }
        ],
    }
    files = {
        "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2),
        "background.js": _render_extension_background(endpoint),
        "content.js": _render_extension_content(account_ref, interval_seconds),
    }
    for filename, content in files.items():
        path = output_dir / filename
        path.write_text(content, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return output_dir


def run_bridge_server(
    config: AppConfig,
    *,
    host: str,
    port: int,
    snapshot_dir: Path | None = None,
) -> None:
    handler = _make_handler(config, snapshot_dir)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Bridge-Server: http://{host}:{port}/ingest")
    print("Stop: Ctrl+C")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def ingest_and_save(
    config: AppConfig,
    account_ref: str,
    payload: dict[str, Any],
    snapshot_dir: Path | None = None,
) -> tuple[AccountUsage, Path]:
    account = resolve_account(config, account_ref)
    usage = usage_from_ingest_payload(account, payload)
    path = save_usage_snapshot(usage, snapshot_dir)
    return usage, path


def load_latest_usages(config: AppConfig, snapshot_dir: Path | None = None) -> list[AccountUsage]:
    usages: list[AccountUsage] = []
    for account in config.accounts:
        usage = load_usage_snapshot(account.id, snapshot_dir)
        if usage is not None:
            usages.append(usage)
    return usages


def _make_handler(config: AppConfig, snapshot_dir: Path | None):
    class BridgeHandler(BaseHTTPRequestHandler):
        server_version = "codex-usage-bridge/0.1"

        def do_OPTIONS(self) -> None:
            self._send_cors(204)

        def do_POST(self) -> None:
            if self.path != "/ingest":
                self._send_json(404, {"error": "not found"})
                return
            content_length = int(self.headers.get("content-length", "0"))
            if content_length <= 0 or content_length > MAX_INGEST_BYTES:
                self._send_json(413, {"error": "invalid payload size"})
                return
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                account_ref = str(payload.get("account") or "")
                usage, path = ingest_and_save(config, account_ref, payload, snapshot_dir)
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
                return

            latest = load_latest_usages(config, snapshot_dir)
            print(render_table(latest), flush=True)
            self._send_json(
                200,
                {
                    "status": usage.status.value,
                    "account": usage.account_id,
                    "saved": str(path),
                },
            )

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_cors(status, content_type="application/json", length=len(body))
            self.wfile.write(body)

        def _send_cors(
            self,
            status: int,
            *,
            content_type: str = "text/plain",
            length: int = 0,
        ) -> None:
            self.send_response(status)
            self.send_header("Access-Control-Allow-Origin", self._allowed_origin())
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Type", content_type)
            if length:
                self.send_header("Content-Length", str(length))
            self.end_headers()

        def _allowed_origin(self) -> str:
            origin = self.headers.get("Origin", "")
            if origin == "https://chatgpt.com" or origin.startswith("chrome-extension://"):
                return origin
            return "https://chatgpt.com"

    return BridgeHandler


def _parse_captured_at(value: Any) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return parsed.astimezone()
    return datetime.now().astimezone()


def _redact_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _render_extension_background(endpoint: str) -> str:
    endpoint_json = json.dumps(endpoint)
    return f"""const ENDPOINT = {endpoint_json};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {{
  if (!message || message.type !== "codexUsageIngest") {{
    return false;
  }}
  fetch(ENDPOINT, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify(message.payload)
  }})
    .then(async (response) => {{
      sendResponse({{
        ok: response.ok,
        status: response.status,
        text: await response.text()
      }});
    }})
    .catch((error) => {{
      sendResponse({{ ok: false, error: String(error) }});
    }});
  return true;
}});
"""


def _render_extension_content(account_ref: str, interval_seconds: int) -> str:
    account_json = json.dumps(account_ref)
    interval_ms = max(interval_seconds, 60) * 1000
    return f"""const CODEX_USAGE_ACCOUNT = {account_json};
const CODEX_USAGE_INTERVAL_MS = {interval_ms};

function collectCodexUsage() {{
  return {{
    account: CODEX_USAGE_ACCOUNT,
    url: location.href,
    title: document.title,
    capturedAt: new Date().toISOString(),
    bodyText: document.body ? document.body.innerText : ""
  }};
}}

function sendCodexUsage() {{
  chrome.runtime.sendMessage(
    {{ type: "codexUsageIngest", payload: collectCodexUsage() }},
    (response) => {{
      if (chrome.runtime.lastError) {{
        console.warn("codex-usage bridge", chrome.runtime.lastError.message);
        return;
      }}
      console.log("codex-usage bridge", response);
    }}
  );
}}

sendCodexUsage();
setInterval(sendCodexUsage, CODEX_USAGE_INTERVAL_MS);
"""
