from __future__ import annotations

import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import AppConfig, default_state_dir, resolve_account
from .extractor import JsonCandidate, extract_windows, load_json_candidate
from .models import Account, AccountStatus, AccountUsage
from .render import render_table
from .state import load_usage_snapshot, save_usage_snapshot

MAX_INGEST_BYTES = 10_000_000
TEXT_PAYLOAD_FIELDS = (
    "bodyText",
    "body_text",
    "text",
    "innerText",
    "domText",
    "textContent",
    "accessibilityText",
    "svgText",
    "htmlText",
)


def usage_from_ingest_payload(account: Account, payload: dict[str, Any]) -> AccountUsage:
    captured_at = _parse_captured_at(payload.get("capturedAt") or payload.get("captured_at"))
    body_text = _combined_payload_text(payload)
    json_candidates = _json_candidates_from_payload(payload)
    five_hour, weekly = extract_windows(
        body_text=body_text,
        json_candidates=json_candidates,
        now=captured_at,
    )
    status = AccountStatus.OK if five_hour and weekly else AccountStatus.PARTIAL
    error = _ingest_error(body_text, payload) if status != AccountStatus.OK else None
    source_urls = {_redact_url(str(payload.get("url") or ""))}
    source_urls.update(_redact_url(candidate.url) for candidate in json_candidates)
    source_urls.discard("")
    return AccountUsage(
        account_id=account.id,
        label=account.label,
        captured_at=captured_at,
        five_hour=five_hour,
        weekly=weekly,
        status=status,
        error=error,
        source_urls=tuple(sorted(source_urls)),
    )


def _ingest_error(body_text: str, payload: dict[str, Any]) -> str | None:
    text_length = payload.get("textLength") if payload.get("textLength") is not None else "-"
    context = (
        f" url={_redact_url(str(payload.get('url') or '')) or '-'}"
        f" title={str(payload.get('title') or '-')[:80]}"
        f" ready={payload.get('readyState') or '-'}"
        f" textLength={text_length}"
    )
    if not body_text.strip():
        return f"missing page text{context}"
    return f'usage limits not found{context} excerpt="{_safe_excerpt(body_text)}"'


def save_bridge_debug_payload(
    account_id: str,
    payload: dict[str, Any],
    snapshot_dir: Path | None = None,
) -> Path:
    directory = (snapshot_dir.parent if snapshot_dir else default_state_dir()) / "debug"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / f"{_safe_filename(account_id)}-last-ingest.json"
    debug_payload = dict(payload)
    if "url" in debug_payload:
        debug_payload["url"] = _redact_url(str(debug_payload.get("url") or ""))
    for field in TEXT_PAYLOAD_FIELDS:
        value = debug_payload.get(field)
        if isinstance(value, str):
            debug_payload[field] = _sanitize_debug_text(value)
    api_responses = debug_payload.get("apiResponses")
    if isinstance(api_responses, list):
        debug_payload["apiResponses"] = [_sanitize_api_response(item) for item in api_responses]
    path.write_text(json.dumps(debug_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def _combined_payload_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for field in TEXT_PAYLOAD_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        parts.append(text)
    return "\n\n".join(parts)


def _json_candidates_from_payload(payload: dict[str, Any]) -> list[JsonCandidate]:
    candidates: list[JsonCandidate] = []
    for item in payload.get("apiResponses") or payload.get("api_responses") or ():
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if isinstance(status, int) and status >= 400:
            continue
        content_type = str(item.get("contentType") or item.get("content_type") or "").lower()
        if content_type and "json" not in content_type:
            continue
        url = _redact_url(str(item.get("url") or ""))
        body = item.get("bodyText") or item.get("body") or item.get("text")
        if not url or not isinstance(body, str):
            continue
        candidate = load_json_candidate(url, body)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _safe_excerpt(value: str, limit: int = 240) -> str:
    excerpt = " ".join(value.split())
    excerpt = excerpt.replace("\\", "\\\\").replace('"', '\\"')
    if len(excerpt) <= limit:
        return excerpt
    return excerpt[: limit - 3] + "..."


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in value)


def _sanitize_debug_text(value: str) -> str:
    text = re.sub(
        r"<script\b[^>]*>.*?</script>",
        "<script>[redacted]</script>",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"<style\b[^>]*>.*?</style>",
        "<style>[redacted]</style>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r'("(?:accessToken|sessionToken|refreshToken|idToken|apiKey)"\s*:\s*")[^"]+',
        r"\1[redacted]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b",
        "[redacted.jwt]",
        text,
    )
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted.email]", text)
    return text


def _sanitize_api_response(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    redacted = dict(item)
    if "url" in redacted:
        redacted["url"] = _redact_url(str(redacted.get("url") or ""))
    for field in ("bodyText", "body", "text"):
        value = redacted.get(field)
        if isinstance(value, str):
            redacted[field] = _sanitize_debug_text(value)
    return redacted



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
            debug_path = None
            if usage.error:
                debug_path = save_bridge_debug_payload(usage.account_id, payload, snapshot_dir)
                print(f"Diagnose {usage.account_id}: {usage.error}", flush=True)
                print(f"Debug-Dump: {debug_path}", flush=True)
            self._send_json(
                200,
                {
                    "status": usage.status.value,
                    "account": usage.account_id,
                    "saved": str(path),
                    "error": usage.error,
                    "debug": str(debug_path) if debug_path else None,
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
const CODEX_USAGE_MIN_TEXT = 40;
const CODEX_USAGE_MAX_FIELD_CHARS = 2000000;
const CODEX_USAGE_READY_TIMEOUT_MS = 60000;
const CODEX_USAGE_API_PATHS = [
  "/backend-api/wham/usage",
  "/backend-api/wham/usage/daily-token-usage-breakdown",
  "/backend-api/wham/usage/daily-enterprise-token-usage-breakdown",
  "/backend-api/wham/usage/credit-usage-events"
];
let codexUsageLastTextLength = -1;
let codexUsageStopped = false;
let codexUsageIntervalId = null;
let codexUsageReadyObserver = null;
let codexUsageReadyTimer = null;

function limitCodexUsageText(value) {{
  const text = String(value || "");
  return text.length > CODEX_USAGE_MAX_FIELD_CHARS
    ? text.slice(0, CODEX_USAGE_MAX_FIELD_CHARS)
    : text;
}}

function isCodexUsageTruncated(value) {{
  return String(value || "").length > CODEX_USAGE_MAX_FIELD_CHARS;
}}

function looksLikeCodexUsageJson(contentType, bodyText) {{
  return String(contentType || "").toLowerCase().includes("json")
    || /^[\\s\\n]*[{{\\[]/.test(String(bodyText || ""));
}}

function isCodexUsageExtensionContextError(error) {{
  return String((error && error.message) || error || "")
    .toLowerCase()
    .includes("extension context invalidated");
}}

function stopCodexUsageBridge(reason) {{
  if (codexUsageStopped) {{
    return;
  }}
  codexUsageStopped = true;
  if (codexUsageIntervalId) {{
    clearInterval(codexUsageIntervalId);
    codexUsageIntervalId = null;
  }}
  if (codexUsageReadyTimer) {{
    clearInterval(codexUsageReadyTimer);
    codexUsageReadyTimer = null;
  }}
  if (codexUsageReadyObserver) {{
    codexUsageReadyObserver.disconnect();
    codexUsageReadyObserver = null;
  }}
  console.warn("codex-usage bridge stopped", reason);
}}

function collectCodexUsageAttributeText() {{
  const attrs = ["aria-label", "aria-valuetext", "aria-valuenow", "title", "alt"];
  const selector = attrs.map((name) => `[${{name}}]`).join(",");
  return Array.from(document.querySelectorAll(selector))
    .flatMap((element) => attrs.map((name) => element.getAttribute(name)))
    .filter((value) => value && String(value).trim())
    .join("\\n");
}}

function collectCodexUsageSvgText() {{
  return Array.from(document.querySelectorAll("svg text, svg title, svg desc"))
    .map((element) => element.textContent || "")
    .filter((value) => value.trim())
    .join("\\n");
}}

async function fetchCodexUsageApi(path) {{
  const url = new URL(path, location.origin);
  const response = await fetch(url.href, {{
    method: "GET",
    credentials: "include",
    headers: {{ "Accept": "application/json" }}
  }});
  const contentType = response.headers.get("content-type") || "";
  const bodyText = await response.text();
  const isJson = looksLikeCodexUsageJson(contentType, bodyText);
  return {{
    url: url.href,
    status: response.status,
    ok: response.ok,
    contentType,
    bodyText: isJson ? limitCodexUsageText(bodyText) : "",
    bodyExcerpt: isJson ? "" : limitCodexUsageText(bodyText).slice(0, 500),
    truncated: isJson ? isCodexUsageTruncated(bodyText) : false
  }};
}}

async function fetchCodexUsageApis() {{
  const results = [];
  for (const path of CODEX_USAGE_API_PATHS) {{
    try {{
      results.push(await fetchCodexUsageApi(path));
    }} catch (error) {{
      results.push({{ url: new URL(path, location.origin).href, error: String(error) }});
    }}
  }}
  return results;
}}

function sanitizedCodexUsageRoot() {{
  if (!document.documentElement) {{
    return null;
  }}
  const clone = document.documentElement.cloneNode(true);
  clone
    .querySelectorAll("script, style, link, meta, noscript, template")
    .forEach((element) => element.remove());
  return clone;
}}

function collectCodexUsage() {{
  const bodyText = document.body ? (document.body.innerText || "") : "";
  const sanitizedRoot = sanitizedCodexUsageRoot();
  const domText = sanitizedRoot ? (sanitizedRoot.textContent || "") : "";
  const accessibilityText = collectCodexUsageAttributeText();
  const svgText = collectCodexUsageSvgText();
  const htmlText = sanitizedRoot ? (sanitizedRoot.outerHTML || "") : "";
  const searchText = [bodyText, domText, accessibilityText, svgText, htmlText]
    .filter((value) => value && String(value).trim())
    .join("\\n\\n");
  return {{
    account: CODEX_USAGE_ACCOUNT,
    url: location.href,
    title: document.title,
    capturedAt: new Date().toISOString(),
    readyState: document.readyState,
    textLength: searchText.length,
    htmlLength: htmlText.length,
    fieldLengths: {{
      bodyText: bodyText.length,
      domText: domText.length,
      accessibilityText: accessibilityText.length,
      svgText: svgText.length,
      htmlText: htmlText.length
    }},
    truncatedFields: {{
      bodyText: isCodexUsageTruncated(bodyText),
      domText: isCodexUsageTruncated(domText),
      accessibilityText: isCodexUsageTruncated(accessibilityText),
      svgText: isCodexUsageTruncated(svgText),
      htmlText: isCodexUsageTruncated(htmlText)
    }},
    visibleTextLength: bodyText.length,
    bodyText: limitCodexUsageText(bodyText),
    domText: limitCodexUsageText(domText),
    accessibilityText: limitCodexUsageText(accessibilityText),
    svgText: limitCodexUsageText(svgText),
    htmlText: limitCodexUsageText(htmlText)
  }};
}}

async function sendCodexUsage() {{
  if (codexUsageStopped) {{
    return;
  }}
  const payload = collectCodexUsage();
  payload.apiResponses = await fetchCodexUsageApis();
  if (codexUsageStopped) {{
    return;
  }}
  if (!payload.bodyText.trim() && codexUsageLastTextLength === payload.textLength) {{
    console.warn("codex-usage bridge: page text is still empty", payload);
  }}
  codexUsageLastTextLength = payload.textLength;
  try {{
    if (
      typeof chrome === "undefined"
      || !chrome.runtime
      || !chrome.runtime.id
      || !chrome.runtime.sendMessage
    ) {{
      stopCodexUsageBridge("extension context unavailable");
      return;
    }}
    chrome.runtime.sendMessage(
      {{ type: "codexUsageIngest", payload }},
      (response) => {{
        const lastError = chrome.runtime && chrome.runtime.lastError;
        if (lastError) {{
          if (isCodexUsageExtensionContextError(lastError)) {{
            stopCodexUsageBridge(lastError.message);
            return;
          }}
          console.warn("codex-usage bridge", lastError.message);
          return;
        }}
        console.log("codex-usage bridge", response);
      }}
    );
  }} catch (error) {{
    if (isCodexUsageExtensionContextError(error)) {{
      stopCodexUsageBridge(error.message || String(error));
      return;
    }}
    console.warn("codex-usage bridge", error);
  }}
}}

function sendWhenReady(startedAt = Date.now()) {{
  if (codexUsageStopped) {{
    return;
  }}
  let sent = false;
  const sendAndStop = (observer, timer) => {{
    if (sent) {{
      return;
    }}
    sent = true;
    if (observer) {{
      observer.disconnect();
      codexUsageReadyObserver = null;
    }}
    if (timer) {{
      clearInterval(timer);
      codexUsageReadyTimer = null;
    }}
    sendCodexUsage();
  }};
  const isReady = () => {{
    const payload = collectCodexUsage();
    const hasEnoughVisibleText = payload.bodyText.trim().length >= CODEX_USAGE_MIN_TEXT;
    const waitedLongEnough = Date.now() - startedAt >= CODEX_USAGE_READY_TIMEOUT_MS;
    return hasEnoughVisibleText || (document.readyState === "complete" && waitedLongEnough);
  }};
  const payload = collectCodexUsage();
  const hasEnoughVisibleText = payload.bodyText.trim().length >= CODEX_USAGE_MIN_TEXT;
  if (hasEnoughVisibleText) {{
    sendCodexUsage();
    return;
  }}
  const observer = new MutationObserver(() => {{
    if (codexUsageStopped) {{
      sendAndStop(observer, timer);
      return;
    }}
    if (isReady()) {{
      sendAndStop(observer, timer);
    }}
  }});
  codexUsageReadyObserver = observer;
  observer.observe(
    document.documentElement,
    {{ childList: true, subtree: true, characterData: true }}
  );
  const timer = setInterval(() => {{
    if (codexUsageStopped) {{
      sendAndStop(observer, timer);
      return;
    }}
    if (isReady()) {{
      sendAndStop(observer, timer);
    }}
  }}, 1000);
  codexUsageReadyTimer = timer;
}}

sendWhenReady();
codexUsageIntervalId = setInterval(sendCodexUsage, CODEX_USAGE_INTERVAL_MS);
"""
