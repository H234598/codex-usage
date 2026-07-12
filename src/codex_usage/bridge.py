from __future__ import annotations

import hmac
import json
import re
import secrets
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import (
    AppConfig,
    default_state_dir,
    load_config,
    resolve_account,
)
from .direct import (
    DirectAuthError,
    auth_identity_for_account,
    auth_plan_type_for_account,
    canonical_backend_identity,
)
from .extractor import JsonCandidate, extract_windows, load_json_candidate
from .identity import (
    backend_identity_from_candidates,
    backend_identity_from_payload,
    backend_plan_type_from_candidates,
)
from .json_utils import loads_strict
from .models import Account, AccountStatus, AccountUsage
from .private_io import (
    assert_no_symlink_ancestors,
    private_path_lock,
    read_private_text,
)
from .private_io import (
    write_private_text as write_private_output_text,
)
from .render import render_table
from .state import (
    _load_state_generation_unlocked,
    account_state_lock,
    backend_identity_matches,
    backend_provenance_matches_configured,
    expire_reset_windows,
    load_current_usage,
    load_state_generation,
    load_usage_snapshot,
    merge_current_with_last_success,
    save_current_usage,
    save_usage_snapshot,
)

MAX_INGEST_BYTES = 10_000_000
MAX_CAPTURE_FUTURE_SECONDS = 5 * 60
BRIDGE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
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
DEBUG_PAYLOAD_FIELDS = (
    "account",
    "url",
    "title",
    "capturedAt",
    "captured_at",
    "readyState",
    "textLength",
    "htmlLength",
    "fieldLengths",
    "truncatedFields",
    "visibleTextLength",
    "apiResponses",
    "api_responses",
    *TEXT_PAYLOAD_FIELDS,
)
DEBUG_TEXT_FIELDS = (*TEXT_PAYLOAD_FIELDS, "title")
DEBUG_STRING_FIELDS = ("account", "capturedAt", "captured_at", "readyState")
DEBUG_NUMBER_FIELDS = ("textLength", "htmlLength", "visibleTextLength")
DEBUG_API_RESPONSE_FIELDS = (
    "url",
    "status",
    "ok",
    "contentType",
    "content_type",
    "bodyText",
    "body",
    "text",
    "bodyExcerpt",
    "truncated",
    "source",
    "error",
    "requestSequence",
)


def usage_from_ingest_payload(account: Account, payload: dict[str, Any]) -> AccountUsage:
    captured_at = _parse_captured_at(payload.get("capturedAt") or payload.get("captured_at"))
    body_text = _combined_payload_text(payload)
    json_candidates = _select_identity_consistent_candidates(
        account,
        _json_candidates_from_payload(payload),
    )
    # Once a structured response identifies the backend, the page DOM is not
    # independently bound to that account. Do not combine a possibly stale
    # browser page with identity-bearing JSON; an authenticated partial result
    # is safer than displaying values from another account.
    structured_identity_present = any(
        backend_identity_from_payload(candidate.payload) != (None, None)
        for candidate in json_candidates
    )
    five_hour, weekly = extract_windows(
        body_text="" if structured_identity_present else body_text,
        json_candidates=json_candidates,
        now=captured_at,
    )
    backend_user_id, backend_account_id = backend_identity_from_candidates(json_candidates)
    backend_plan_type = backend_plan_type_from_candidates(json_candidates)
    auth_user_id, auth_account_id = auth_identity_for_account(account)
    auth_plan_type = auth_plan_type_for_account(account)
    backend_user_id, backend_account_id = canonical_backend_identity(
        backend_user_id,
        backend_account_id,
        auth_user_id=auth_user_id,
        auth_account_id=auth_account_id,
        auth_plan_type=auth_plan_type,
        backend_plan_type=backend_plan_type,
        require_backend_identity=True,
    )
    status = (
        AccountStatus.OK
        if five_hour is not None
        and weekly is not None
        and five_hour.has_usage_value
        and weekly.has_usage_value
        else AccountStatus.PARTIAL
    )
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
        backend_configured=account.backend,
        backend_used="browser",
        backend_user_id=backend_user_id,
        backend_account_id=backend_account_id,
    )


def _select_identity_consistent_candidates(
    account: Account,
    candidates: list[JsonCandidate],
) -> list[JsonCandidate]:
    groups: list[
        tuple[tuple[str | None, str | None], list[JsonCandidate]]
    ] = []
    for candidate in candidates:
        identity = backend_identity_from_payload(candidate.payload)
        if identity == (None, None):
            continue
        for index, (group_identity, grouped_candidates) in enumerate(groups):
            if not _identities_compatible(identity, group_identity):
                continue
            groups[index] = (
                (
                    identity[0] or group_identity[0],
                    identity[1] or group_identity[1],
                ),
                [*grouped_candidates, candidate],
            )
            break
        else:
            groups.append((identity, [candidate]))
    if not groups:
        return candidates
    if len(groups) == 1:
        return groups[0][1]

    auth_user_id, auth_account_id = auth_identity_for_account(account)
    if not (auth_user_id or auth_account_id):
        raise ValueError("bridge payload contains multiple backend accounts")

    matching_groups = []
    for identity, grouped_candidates in groups:
        try:
            canonical_backend_identity(
                identity[0],
                identity[1],
                auth_user_id=auth_user_id,
                auth_account_id=auth_account_id,
                require_backend_identity=True,
            )
        except ValueError:
            continue
        matching_groups.append(grouped_candidates)
    if len(matching_groups) == 0:
        raise ValueError("backend response belongs to a different account")
    if len(matching_groups) > 1:
        raise ValueError("bridge payload does not identify one configured backend account")
    return matching_groups[0]


def _identities_compatible(
    left: tuple[str | None, str | None],
    right: tuple[str | None, str | None],
) -> bool:
    shared_field = False
    for left_value, right_value in zip(left, right, strict=True):
        if left_value is None or right_value is None:
            continue
        shared_field = True
        if left_value != right_value:
            return False
    return shared_field


def _ingest_error(body_text: str, payload: dict[str, Any]) -> str | None:
    text_length = payload.get("textLength") if payload.get("textLength") is not None else None
    context = (
        f" url={_safe_context_value(_redact_url(str(payload.get('url') or '')), 200)}"
        f" title={_safe_context_value(payload.get('title'), 80)}"
        f" ready={_safe_context_value(payload.get('readyState'), 40)}"
        f" textLength={_safe_context_value(text_length, 40)}"
    )
    if not body_text.strip():
        return f"missing page text{context}"
    return f'usage limits not found{context} excerpt="{_safe_excerpt(body_text)}"'


def save_bridge_debug_payload(
    account_id: str,
    payload: dict[str, Any],
    snapshot_dir: Path | None = None,
    *,
    state_generation: int | None = None,
) -> Path:
    safe_account_id = _safe_filename(account_id)
    if not safe_account_id:
        raise ValueError("account id must produce a safe debug filename")
    if state_generation is None:
        state_generation = load_state_generation(safe_account_id, snapshot_dir)
    with account_state_lock(safe_account_id):
        current_generation = _load_state_generation_unlocked(
            safe_account_id,
            snapshot_dir,
        )
        directory = (snapshot_dir.parent if snapshot_dir else default_state_dir()) / "debug"
        assert_no_symlink_ancestors(directory, label="debug directory")
        if directory.is_symlink():
            raise ValueError(f"debug directory must not be a symlink: {directory}")
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"debug directory is not a real directory: {directory}")
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        path = directory / f"{safe_account_id}-last-ingest.json"
        if state_generation != current_generation:
            return path
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"debug path must be a regular file: {path}")
        debug_payload = {
            field: payload[field] for field in DEBUG_PAYLOAD_FIELDS if field in payload
        }
        if "url" in debug_payload:
            debug_payload["url"] = _redact_url(str(debug_payload.get("url") or ""))
        for field in DEBUG_TEXT_FIELDS:
            value = debug_payload.get(field)
            if isinstance(value, str):
                debug_payload[field] = _sanitize_debug_text(value)
            elif field in debug_payload:
                debug_payload.pop(field, None)
        for field in DEBUG_STRING_FIELDS:
            value = debug_payload.get(field)
            if isinstance(value, str):
                debug_payload[field] = _sanitize_debug_text(value)
            elif field in debug_payload:
                debug_payload.pop(field, None)
        for field in DEBUG_NUMBER_FIELDS:
            if field in debug_payload:
                value = _sanitize_debug_number(debug_payload[field])
                if value is None:
                    debug_payload.pop(field, None)
                else:
                    debug_payload[field] = value
        field_lengths = _sanitize_debug_lengths(debug_payload.get("fieldLengths"))
        if field_lengths:
            debug_payload["fieldLengths"] = field_lengths
        else:
            debug_payload.pop("fieldLengths", None)
        truncated_fields = _sanitize_debug_flags(debug_payload.get("truncatedFields"))
        if truncated_fields:
            debug_payload["truncatedFields"] = truncated_fields
        else:
            debug_payload.pop("truncatedFields", None)
        for field in ("apiResponses", "api_responses"):
            api_responses = debug_payload.get(field)
            if isinstance(api_responses, list):
                debug_payload[field] = _sanitize_api_responses(api_responses)
            else:
                debug_payload.pop(field, None)
        write_private_output_text(
            path,
            json.dumps(debug_payload, ensure_ascii=False, indent=2, allow_nan=False),
            label="debug path",
        )
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
    responses_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    response_sequences: dict[tuple[str, str], int | None] = {}
    response_items: list[Any] = []
    for field in ("apiResponses", "api_responses"):
        value = payload.get(field)
        if isinstance(value, list):
            response_items.extend(value)
    for item in response_items:
        if not isinstance(item, dict):
            continue
        url = _redact_url(str(item.get("url") or ""))
        if not url:
            continue
        key = (str(item.get("source") or ""), url)
        sequence = item.get("requestSequence")
        if isinstance(sequence, bool):
            sequence = None
        elif isinstance(sequence, int) and sequence >= 0:
            pass
        elif isinstance(sequence, str) and sequence.isdecimal():
            sequence = int(sequence)
        else:
            sequence = None
        previous_sequence = response_sequences.get(key)
        if (
            key in responses_by_key
            and previous_sequence is not None
            and (sequence is None or sequence < previous_sequence)
        ):
            continue
        responses_by_key[key] = item
        response_sequences[key] = sequence

    ordered_candidates: list[tuple[bool, int, int, int, JsonCandidate]] = []
    for candidate_index, item in enumerate(responses_by_key.values()):
        if item.get("truncated") is True:
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
            sequence = response_sequences.get(
                (str(item.get("source") or ""), url)
            )
            ordered_candidates.append(
                (
                    sequence is not None,
                    sequence if sequence is not None else -1,
                    _bridge_response_source_priority(item.get("source")),
                    candidate_index,
                    candidate,
                )
            )

    # The extension retains one response per capture source. Sort the surviving
    # candidates by freshness so extractor and identity tie-breakers cannot
    # depend on the order in which sources arrived in the ingest payload.
    ordered_candidates.sort(key=lambda item: item[:4])
    return [item[4] for item in ordered_candidates]


def _bridge_response_source_priority(value: Any) -> int:
    source = str(value or "")
    return {
        "content-probe": 10,
        "page-refresh": 20,
        "page-fetch": 30,
    }.get(source, 0)


def _safe_excerpt(value: str, limit: int = 240) -> str:
    excerpt = " ".join(value.split())
    excerpt = excerpt.replace("\\", "\\\\").replace('"', '\\"')
    if len(excerpt) <= limit:
        return excerpt
    return excerpt[: limit - 3] + "..."


def _safe_context_value(value: Any, limit: int) -> str:
    if value is None or value == "":
        return "-"
    text = " ".join(str(value).split())
    if not text:
        return "-"
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


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
        r'("(?:(?:user|account|organization|workspace)_id)"\s*:\s*")[^"]+',
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


def _sanitize_api_responses(items: list[Any]) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    for item in items:
        response = _sanitize_api_response(item)
        if response:
            responses.append(response)
    return responses


def _sanitize_api_response(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    redacted = {field: item[field] for field in DEBUG_API_RESPONSE_FIELDS if field in item}
    if "url" in redacted:
        redacted["url"] = _redact_url(str(redacted.get("url") or ""))
    for field in ("bodyText", "body", "text", "bodyExcerpt", "error"):
        value = redacted.get(field)
        if isinstance(value, str):
            redacted[field] = _sanitize_debug_text(value)
        elif field in redacted:
            redacted.pop(field, None)
    for field in ("contentType", "content_type", "source"):
        value = redacted.get(field)
        if isinstance(value, str):
            redacted[field] = _sanitize_debug_text(value)
        elif field in redacted:
            redacted.pop(field, None)
    for field in ("ok", "truncated"):
        if field in redacted and not isinstance(redacted[field], bool):
            redacted.pop(field, None)
    for field in ("status", "requestSequence"):
        if field not in redacted:
            continue
        number = _sanitize_debug_number(redacted[field])
        if number is None:
            redacted.pop(field, None)
        else:
            redacted[field] = number
    return redacted


def _sanitize_debug_number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _sanitize_debug_lengths(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    lengths: dict[str, int] = {}
    for field in TEXT_PAYLOAD_FIELDS:
        length = _sanitize_debug_number(value.get(field))
        if length is not None:
            lengths[field] = length
    return lengths or None


def _sanitize_debug_flags(value: Any) -> dict[str, bool] | None:
    if not isinstance(value, dict):
        return None
    flags = {
        field: value[field]
        for field in TEXT_PAYLOAD_FIELDS
        if isinstance(value.get(field), bool)
    }
    return flags or None


def bridge_token_for_account(account_ref: str) -> str:
    if not isinstance(account_ref, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,64}", account_ref
    ):
        raise ValueError("account id must be valid for bridge token storage")
    token_dir = default_state_dir() / "bridge-tokens"
    _prepare_private_directory(token_dir, label="bridge token directory")
    path = token_dir / f"{account_ref}.token"
    with private_path_lock(path, label="bridge token lock"):
        existing = _read_existing_bridge_token(path)
        if existing is not None:
            return existing
        token = secrets.token_urlsafe(32)
        write_private_output_text(
            path,
            token + "\n",
            label="bridge token path",
            mode=0o600,
        )
        return token


def revoke_bridge_token(account_ref: str) -> bool:
    if not isinstance(account_ref, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,64}", account_ref
    ):
        raise ValueError("account id must be valid for bridge token storage")
    token_dir = default_state_dir() / "bridge-tokens"
    if not token_dir.exists() and not token_dir.is_symlink():
        return False
    _prepare_private_directory(token_dir, label="bridge token directory")
    path = token_dir / f"{account_ref}.token"
    with private_path_lock(path, label="bridge token lock"):
        if not path.exists() and not path.is_symlink():
            return False
        if path.is_dir() and not path.is_symlink():
            raise ValueError(f"bridge token path must be a regular file: {path}")
        path.unlink()
        return True


def bridge_token_matches(account_ref: str, supplied: str) -> bool:
    if not isinstance(account_ref, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,64}", account_ref
    ):
        return False
    try:
        _validate_bridge_token(supplied)
        token_dir = default_state_dir() / "bridge-tokens"
        if not token_dir.exists() and not token_dir.is_symlink():
            return False
        _prepare_private_directory(token_dir, label="bridge token directory")
        path = token_dir / f"{account_ref}.token"
        with private_path_lock(path, label="bridge token lock"):
            current = _read_existing_bridge_token(path)
        return current is not None and hmac.compare_digest(current, supplied)
    except (OSError, ValueError):
        return False


def _read_existing_bridge_token(path: Path) -> str | None:
    if not path.exists():
        return None
    text, file_stat = read_private_text(
        path,
        regular_label="bridge token path",
        read_label="bridge token",
        max_bytes=256,
        too_large_label="bridge token",
        invalid_utf8_label="bridge token",
    )
    if file_stat.st_nlink != 1 or file_stat.st_mode & 0o077:
        raise ValueError("bridge token path permissions are too broad")
    return _validate_bridge_token(text.strip())


def _validate_bridge_token(token: str) -> str:
    if not isinstance(token, str) or not BRIDGE_TOKEN_RE.fullmatch(token):
        raise ValueError("invalid bridge token")
    return token


def render_bridge_snippet(
    account_ref: str,
    *,
    endpoint: str,
    interval_seconds: int,
    token: str | None = None,
) -> str:
    account_json = json.dumps(account_ref)
    endpoint_json = json.dumps(endpoint)
    token_json = json.dumps(
        _validate_bridge_token(token) if token else bridge_token_for_account(account_ref)
    )
    interval_ms = max(interval_seconds, 60) * 1000
    return f"""(() => {{
  const account = {account_json};
  const endpoint = {endpoint_json};
  const token = {token_json};
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
      headers: {{
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token
      }},
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
    token: str | None = None,
) -> Path:
    token = _validate_bridge_token(token) if token else bridge_token_for_account(account_ref)
    _prepare_private_directory(output_dir, label="extension output directory")
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
                "run_at": "document_start",
            },
            {
                "matches": ["https://chatgpt.com/codex/cloud/settings/analytics*"],
                "js": ["page-hook.js"],
                "run_at": "document_start",
                "world": "MAIN",
            }
        ],
    }
    files = {
        "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False),
        "background.js": _render_extension_background(endpoint, token),
        "content.js": _render_extension_content(account_ref, interval_seconds),
        "page-hook.js": _render_extension_page_hook(),
    }
    for filename, content in files.items():
        path = output_dir / filename
        _write_private_text(path, content, label="extension output path")
    return output_dir


def _prepare_private_directory(path: Path, *, label: str) -> None:
    assert_no_symlink_ancestors(path, label=label)
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} is not a real directory: {path}")
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _write_private_text(path: Path, content: str, *, label: str) -> None:
    write_private_output_text(path, content, label=label)


def run_bridge_server(
    config: AppConfig,
    *,
    host: str,
    port: int,
    snapshot_dir: Path | None = None,
    config_path: Path | None = None,
) -> None:
    tokens = {
        account.id: bridge_token_for_account(account.id)
        for account in config.accounts
    }
    handler = _make_handler(
        config,
        snapshot_dir,
        tokens,
        config_path=config_path.expanduser() if config_path else None,
    )
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
    *,
    require_backend_identity: bool = False,
) -> tuple[AccountUsage, Path]:
    account = resolve_account(config, account_ref)
    state_generation = load_state_generation(account.id, snapshot_dir)
    usage = replace(
        usage_from_ingest_payload(account, payload),
        state_generation=state_generation,
    )
    if require_backend_identity and not (
        usage.backend_user_id or usage.backend_account_id
    ):
        raise ValueError("bridge payload has no backend account identity")
    snapshot = load_usage_snapshot(account.id, snapshot_dir)
    current_dir = snapshot_dir.parent / "current" if snapshot_dir else None
    current = load_current_usage(account.id, current_dir)
    known = _newest_known_usage(snapshot, current)
    if require_backend_identity:
        _reject_ambiguous_browser_identity(config, account, payload)
        if account.auth_json_path is not None:
            if not _usage_matches_current_auth(account, usage):
                raise ValueError("bridge payload belongs to a different backend account")
        elif known is None:
            raise ValueError("browser account identity is not initialized")
    if (
        known is not None
        and not backend_identity_matches(usage, known)
        and not _usage_matches_current_auth(account, usage)
    ):
        raise ValueError("bridge payload belongs to a different backend account")
    if known is not None:
        try:
            if usage.captured_at < known.captured_at:
                raise ValueError("bridge payload is older than known state")
        except TypeError:
            pass
    path = save_usage_snapshot(usage, snapshot_dir)
    current_dir = snapshot_dir.parent / "current" if snapshot_dir else None
    save_current_usage(usage, current_dir)
    return usage, path


def _newest_known_usage(
    snapshot: AccountUsage | None,
    current: AccountUsage | None,
) -> AccountUsage | None:
    if snapshot is None:
        return current
    if current is None:
        return snapshot
    try:
        return current if current.captured_at >= snapshot.captured_at else snapshot
    except TypeError:
        return current


def _reject_ambiguous_browser_identity(
    config: AppConfig,
    account: Account,
    payload: dict[str, Any],
) -> None:
    if account.auth_json_path is None:
        return
    raw_user_id, raw_account_id = backend_identity_from_candidates(
        _json_candidates_from_payload(payload)
    )
    if not raw_user_id or (raw_account_id is not None and raw_account_id != raw_user_id):
        return
    try:
        auth_user_id, _auth_account_id = auth_identity_for_account(account)
    except DirectAuthError:
        return
    if not auth_user_id or raw_user_id != auth_user_id:
        return
    account_ids: set[str] = set()
    for candidate in config.accounts:
        try:
            candidate_user_id, candidate_account_id = auth_identity_for_account(candidate)
        except DirectAuthError:
            continue
        if candidate_user_id == auth_user_id and candidate_account_id:
            account_ids.add(candidate_account_id)
    if len(account_ids) > 1:
        raise ValueError("browser payload has ambiguous backend account identity")


def _usage_matches_current_auth(account: Account, usage: AccountUsage) -> bool:
    try:
        auth_user_id, auth_account_id = auth_identity_for_account(account)
    except DirectAuthError:
        return False
    return _usage_matches_auth_identity(
        usage,
        auth_user_id=auth_user_id,
        auth_account_id=auth_account_id,
    )


def _usage_matches_auth_identity(
    usage: AccountUsage,
    *,
    auth_user_id: str | None,
    auth_account_id: str | None,
) -> bool:
    if not (auth_user_id or auth_account_id):
        return False
    try:
        canonical_user_id, canonical_account_id = canonical_backend_identity(
            usage.backend_user_id,
            usage.backend_account_id,
            auth_user_id=auth_user_id,
            auth_account_id=auth_account_id,
            require_backend_identity=True,
        )
    except ValueError:
        return False
    return (
        canonical_user_id == usage.backend_user_id
        and canonical_account_id == usage.backend_account_id
    )


def _cached_usage_matches_current_auth(
    usage: AccountUsage,
    auth_identity: tuple[str | None, str | None] | None,
) -> bool:
    """Do not display authenticated values after the account identity changed."""
    if auth_identity is None:
        return True
    if not (usage.backend_user_id or usage.backend_account_id):
        # Identity-free status records are still useful; identity-free limits
        # are not safe to attribute after an auth.json change.
        return usage.five_hour is None and usage.weekly is None
    auth_user_id, auth_account_id = auth_identity
    return _usage_matches_auth_identity(
        usage,
        auth_user_id=auth_user_id,
        auth_account_id=auth_account_id,
    )


def _invalidate_cached_usage(account: Account, usage: AccountUsage) -> AccountUsage:
    return replace(
        usage,
        label=account.label,
        five_hour=None,
        weekly=None,
        status=AccountStatus.PARTIAL,
        error="cached usage discarded after auth.json identity change",
        backend_configured=account.backend,
        backend_used=account.backend,
        backend_user_id=None,
        backend_account_id=None,
        fallback_reason=None,
        values_captured_at=None,
        stale=True,
        cache_invalidated=True,
    )


def load_latest_usages(config: AppConfig, snapshot_dir: Path | None = None) -> list[AccountUsage]:
    usages: list[AccountUsage] = []
    current_dir = snapshot_dir.parent / "current" if snapshot_dir else None
    for account in config.accounts:
        last_success = load_usage_snapshot(account.id, snapshot_dir)
        current = load_current_usage(account.id, current_dir)
        auth_identity: tuple[str | None, str | None] | None = None
        if account.auth_json_path is not None:
            try:
                auth_identity = auth_identity_for_account(account)
            except DirectAuthError:
                auth_identity = (None, None)
        if last_success is not None and not backend_provenance_matches_configured(
            last_success, account.backend
        ):
            last_success = None
        if last_success is not None and not _cached_usage_matches_current_auth(
            last_success,
            auth_identity,
        ):
            last_success = _invalidate_cached_usage(account, last_success)
        if current is not None and not backend_provenance_matches_configured(
            current, account.backend
        ):
            current = None
        if current is not None and not _cached_usage_matches_current_auth(
            current,
            auth_identity,
        ):
            current = _invalidate_cached_usage(account, current)
        if auth_identity is not None:
            try:
                auth_identity_after = auth_identity_for_account(account)
            except DirectAuthError:
                auth_identity_after = (None, None)
            if auth_identity_after != auth_identity:
                if last_success is not None:
                    last_success = _invalidate_cached_usage(account, last_success)
                if current is not None:
                    current = _invalidate_cached_usage(account, current)
        if current is not None:
            usage = merge_current_with_last_success(current, last_success)
        elif last_success is not None:
            usage = last_success
        else:
            continue
        usage = expire_reset_windows(
            usage,
            reference_at=datetime.now().astimezone(),
        )
        usages.append(_mark_latest_stale(usage, config.interval_seconds))
    return usages


def _mark_latest_stale(usage: AccountUsage, interval_seconds: int) -> AccountUsage:
    grace_seconds = max(60, interval_seconds + 60)
    try:
        age_seconds = (datetime.now().astimezone() - usage.captured_at).total_seconds()
    except (TypeError, ValueError, OverflowError):
        age_seconds = grace_seconds + 1
    if usage.stale or age_seconds > grace_seconds:
        return replace(usage, stale=True)
    return usage


def _make_handler(
    config: AppConfig,
    snapshot_dir: Path | None,
    tokens: dict[str, str],
    *,
    config_path: Path | None = None,
):
    class BridgeHandler(BaseHTTPRequestHandler):
        server_version = "codex-usage-bridge/0.1"

        def do_OPTIONS(self) -> None:
            if not self._is_allowed_origin():
                self._send_json(403, {"error": "origin rejected"})
                return
            self._send_cors(204)

        def do_POST(self) -> None:
            if not self._is_allowed_origin():
                self._send_json(403, {"error": "origin rejected"})
                return
            if self.path != "/ingest":
                self._send_json(404, {"error": "not found"})
                return
            try:
                content_length = int(self.headers.get("content-length", "0"))
            except (TypeError, ValueError):
                self._send_json(413, {"error": "invalid payload size"})
                return
            if content_length <= 0 or content_length > MAX_INGEST_BYTES:
                self._send_json(413, {"error": "invalid payload size"})
                return
            try:
                payload = loads_strict(self.rfile.read(content_length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._send_json(400, {"error": "invalid JSON payload"})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "invalid JSON payload"})
                return

            request_config = self._config_for_request()
            if request_config is None:
                self._send_json(503, {"error": "configuration unavailable"})
                return
            account_ref = str(payload.get("account") or "")
            if not self._is_authorized(account_ref, request_config):
                self._send_json(401, {"error": "authorization required"})
                return
            try:
                usage, path = ingest_and_save(
                    request_config,
                    account_ref,
                    payload,
                    snapshot_dir,
                    require_backend_identity=True,
                )
            except KeyError:
                self._send_json(400, {"error": "unknown or ambiguous account"})
                return
            except (DirectAuthError, ValueError) as exc:
                _log_bridge_error("Bridge ingest rejected", exc)
                self._send_json(400, {"error": "ingest rejected"})
                return
            except Exception as exc:
                _log_bridge_error("Bridge ingest failed", exc)
                self._send_json(500, {"error": "ingest failed"})
                return

            latest = load_latest_usages(request_config, snapshot_dir)
            print(render_table(latest), flush=True)
            debug_path = None
            if usage.error:
                print(f"Diagnose {usage.account_id}: {usage.error}", flush=True)
                try:
                    debug_path = save_bridge_debug_payload(
                        usage.account_id,
                        payload,
                        snapshot_dir,
                        state_generation=usage.state_generation,
                    )
                except Exception as exc:
                    _log_bridge_error("Bridge debug dump failed", exc)
                else:
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
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
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
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Content-Type", content_type)
            if length:
                self.send_header("Content-Length", str(length))
            self.end_headers()

        def _allowed_origin(self) -> str:
            origin = self.headers.get("Origin", "")
            if self._is_allowed_origin() and origin:
                return origin
            return "https://chatgpt.com"

        def _is_allowed_origin(self) -> bool:
            origin = self.headers.get("Origin", "")
            return (
                not origin
                or origin == "https://chatgpt.com"
                or origin.startswith("chrome-extension://")
            )

        def _config_for_request(self) -> AppConfig | None:
            if config_path is None:
                return config
            try:
                return load_config(config_path)
            except (OSError, UnicodeError, ValueError):
                return None

        def _is_authorized(self, account_ref: str, request_config: AppConfig) -> bool:
            try:
                resolve_account(request_config, account_ref)
            except KeyError:
                return False
            authorization = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if config_path is None and not tokens.get(account_ref):
                return False
            if not authorization.startswith(prefix):
                return False
            supplied = authorization[len(prefix):].strip()
            return bridge_token_matches(account_ref, supplied)

    return BridgeHandler


def _log_bridge_error(message: str, exc: Exception) -> None:
    print(f"{message}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def _parse_captured_at(value: Any) -> datetime:
    received_at = datetime.now().astimezone()
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            parsed = parsed.astimezone()
        except (OSError, OverflowError, ValueError):
            pass
        else:
            if parsed <= received_at + timedelta(seconds=MAX_CAPTURE_FUTURE_SECONDS):
                return parsed
    return received_at


def _redact_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _render_extension_background(endpoint: str, token: str) -> str:
    endpoint_json = json.dumps(endpoint)
    token_json = json.dumps(_validate_bridge_token(token))
    return f"""const ENDPOINT = {endpoint_json};
const TOKEN = {token_json};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {{
  if (!message || message.type !== "codexUsageIngest") {{
    return false;
  }}
  fetch(ENDPOINT, {{
    method: "POST",
    headers: {{
      "Content-Type": "application/json",
      "Authorization": "Bearer " + TOKEN
    }},
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
const CODEX_USAGE_PAGE_REFRESH_TIMEOUT_MS = 2500;
const CODEX_USAGE_API_PATHS = [
  "/backend-api/wham/usage",
  "/backend-api/wham/usage/daily-token-usage-breakdown",
  "/backend-api/wham/usage/daily-enterprise-token-usage-breakdown",
  "/backend-api/wham/usage/credit-usage-events"
];
const CODEX_USAGE_CAPTURED_API_LIMIT = 50;
let codexUsageLastTextLength = -1;
let codexUsageStopped = false;
let codexUsageIntervalId = null;
let codexUsageReadyObserver = null;
let codexUsageReadyTimer = null;
let codexUsageApiSendTimer = null;
let codexUsageRefreshRequestSequence = 0;
let codexUsageSendInFlight = null;
let codexUsageSendPending = false;
const codexUsageCapturedApiResponses = [];
const codexUsageRefreshWaiters = new Map();

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

function codexUsageApiResponseKey(item) {{
  let url = String((item && item.url) || "");
  try {{
    const parsed = new URL(url, location.origin);
    const path = parsed.pathname.replace(/\\/+$/, "") || "/";
    url = `${{parsed.origin}}${{path}}`;
  }} catch (_error) {{
    // Keep malformed diagnostic URLs isolated without breaking the bridge.
  }}
  return [item.source || "", url].join("\\n");
}}

function codexUsageHasMainUsageResponse() {{
  return codexUsageCapturedApiResponses.some(codexUsageIsMainUsageResponse);
}}

function codexUsageApiResponseSequence(item) {{
  const value = item && item.requestSequence;
  return Number.isInteger(value) && value >= 0 ? value : null;
}}

function codexUsageApiResponseIsNewer(candidate, current) {{
  const candidateSequence = codexUsageApiResponseSequence(candidate);
  const currentSequence = codexUsageApiResponseSequence(current);
  return !(
    currentSequence !== null
    && (candidateSequence === null || candidateSequence < currentSequence)
  );
}}

function codexUsageIsMainUsageResponse(item) {{
  try {{
    const parsed = new URL(String((item && item.url) || ""), location.origin);
    const path = parsed.pathname.replace(/\\/+$/, "") || "/";
    if (parsed.origin !== location.origin || path !== "/backend-api/wham/usage") {{
      return false;
    }}
    if (item.truncated === true) {{
      return false;
    }}
    const status = Number(item.status);
    if (Number.isFinite(status) && (status < 200 || status >= 300)) {{
      return false;
    }}
    const bodyText = String(item.bodyText || item.body || item.text || "");
    return /[\"'](?:rate_limit|rateLimits|rateLimitsByLimitId)[\"']\\s*:/.test(bodyText);
  }} catch (_error) {{
    return false;
  }}
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
  if (codexUsageApiSendTimer) {{
    clearTimeout(codexUsageApiSendTimer);
    codexUsageApiSendTimer = null;
  }}
  for (const waiter of codexUsageRefreshWaiters.values()) {{
    clearTimeout(waiter.timeout);
    waiter.resolve(false);
  }}
  codexUsageRefreshWaiters.clear();
  codexUsageSendPending = false;
  console.warn("codex-usage bridge stopped", reason);
}}

function rememberCodexUsageApiResponse(item) {{
  if (!item || typeof item !== "object" || !item.url) {{
    return;
  }}
  const key = codexUsageApiResponseKey(item);
  for (let index = codexUsageCapturedApiResponses.length - 1; index >= 0; index -= 1) {{
    if (codexUsageApiResponseKey(codexUsageCapturedApiResponses[index]) === key) {{
      if (!codexUsageApiResponseIsNewer(item, codexUsageCapturedApiResponses[index])) {{
        return;
      }}
      codexUsageCapturedApiResponses.splice(index, 1);
      break;
    }}
  }}
  codexUsageCapturedApiResponses.push(item);
  while (codexUsageCapturedApiResponses.length > CODEX_USAGE_CAPTURED_API_LIMIT) {{
    codexUsageCapturedApiResponses.shift();
  }}
}}

function dedupeCodexUsageApiResponses(items) {{
  const byKey = new Map();
  for (const item of items) {{
    if (!item || typeof item !== "object" || !item.url) {{
      continue;
    }}
    const key = codexUsageApiResponseKey(item);
    const current = byKey.get(key);
    if (!current || codexUsageApiResponseIsNewer(item, current)) {{
      byKey.set(key, item);
    }}
  }}
  return Array.from(byKey.values()).slice(-CODEX_USAGE_CAPTURED_API_LIMIT);
}}

function scheduleCodexUsageSend(delayMs = 500) {{
  if (codexUsageStopped) {{
    return;
  }}
  if (codexUsageApiSendTimer) {{
    clearTimeout(codexUsageApiSendTimer);
  }}
  codexUsageApiSendTimer = setTimeout(() => {{
    codexUsageApiSendTimer = null;
    sendCodexUsage();
  }}, delayMs);
}}

function forgetCodexUsageMainUsageResponses() {{
  for (let index = codexUsageCapturedApiResponses.length - 1; index >= 0; index -= 1) {{
    try {{
      const parsed = new URL(
        String(
          (codexUsageCapturedApiResponses[index]
            && codexUsageCapturedApiResponses[index].url) || ""
        ),
        location.origin
      );
      const path = parsed.pathname.replace(/\\/+$/, "") || "/";
      if (parsed.origin === location.origin && path === "/backend-api/wham/usage") {{
        codexUsageCapturedApiResponses.splice(index, 1);
      }}
    }} catch (_error) {{
      // Leave malformed diagnostic entries untouched.
    }}
  }}
}}

function resolveCodexUsagePageRefresh(requestId, succeeded) {{
  const key = String(requestId || "");
  const waiter = codexUsageRefreshWaiters.get(key);
  if (!waiter) {{
    return;
  }}
  clearTimeout(waiter.timeout);
  codexUsageRefreshWaiters.delete(key);
  waiter.resolve(Boolean(succeeded));
}}

function requestCodexUsagePageRefresh() {{
  forgetCodexUsageMainUsageResponses();
  if (codexUsageStopped || typeof window.postMessage !== "function") {{
    return Promise.resolve(false);
  }}
  const requestId = String(++codexUsageRefreshRequestSequence);
  return new Promise((resolve) => {{
    const timeout = setTimeout(() => {{
      codexUsageRefreshWaiters.delete(requestId);
      forgetCodexUsageMainUsageResponses();
      resolve(false);
    }}, CODEX_USAGE_PAGE_REFRESH_TIMEOUT_MS);
    codexUsageRefreshWaiters.set(requestId, {{ resolve, timeout }});
    try {{
      window.postMessage({{
        type: "codexUsageRefresh",
        requestId
      }}, location.origin);
    }} catch (_error) {{
      clearTimeout(timeout);
      codexUsageRefreshWaiters.delete(requestId);
      forgetCodexUsageMainUsageResponses();
      resolve(false);
    }}
  }});
}}

window.addEventListener("message", (event) => {{
  if (event.source !== window || !event.data || event.data.type !== "codexUsageApiResponses") {{
    return;
  }}
  const responses = Array.isArray(event.data.responses) ? event.data.responses : [];
  for (const response of responses) {{
    rememberCodexUsageApiResponse(response);
  }}
  if (event.data.requestId !== undefined && event.data.requestId !== null) {{
    resolveCodexUsagePageRefresh(
      event.data.requestId,
      responses.some(codexUsageIsMainUsageResponse)
    );
  }}
  if (
    responses.length
    && (event.data.requestId === undefined || event.data.requestId === null)
  ) {{
    scheduleCodexUsageSend();
  }}
}});

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

async function sendCodexUsageOnce() {{
  if (codexUsageStopped) {{
    return;
  }}
  const payload = collectCodexUsage();
  const pageRefreshSucceeded = await requestCodexUsagePageRefresh();
  const probeResponses = pageRefreshSucceeded ? [] : await fetchCodexUsageApis();
  payload.apiResponses = dedupeCodexUsageApiResponses([
    ...codexUsageCapturedApiResponses,
    ...probeResponses
  ]);
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
        try {{
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
        }} catch (error) {{
          if (isCodexUsageExtensionContextError(error)) {{
            stopCodexUsageBridge(error.message || String(error));
            return;
          }}
          console.warn("codex-usage bridge", error);
        }}
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

function sendCodexUsage() {{
  if (codexUsageStopped) {{
    return Promise.resolve();
  }}
  if (codexUsageSendInFlight) {{
    codexUsageSendPending = true;
    return codexUsageSendInFlight;
  }}
  const operation = sendCodexUsageOnce().catch((error) => {{
    console.warn("codex-usage bridge send failed", error);
  }});
  codexUsageSendInFlight = operation;
  operation.then(() => {{
    if (codexUsageSendInFlight !== operation) {{
      return;
    }}
    codexUsageSendInFlight = null;
    if (codexUsageSendPending && !codexUsageStopped) {{
      codexUsageSendPending = false;
      scheduleCodexUsageSend(0);
    }}
  }});
  return operation;
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

function startCodexUsageBridge() {{
  if (codexUsageStopped) {{
    return;
  }}
  if (!document.documentElement) {{
    setTimeout(startCodexUsageBridge, 50);
    return;
  }}
  sendWhenReady();
  codexUsageIntervalId = setInterval(sendCodexUsage, CODEX_USAGE_INTERVAL_MS);
}}

startCodexUsageBridge();
"""


def _render_extension_page_hook() -> str:
    return """(() => {
  const CODEX_USAGE_MAX_FIELD_CHARS = 2000000;
  const CODEX_USAGE_CAPTURED_API_LIMIT = 50;
  const CODEX_USAGE_FLUSH_INTERVAL_MS = 1000;
  const CODEX_USAGE_FLUSH_TICKS = 120;
  const codexUsageCapturedApiResponses = [];
  let codexUsageFetchSequence = 0;
  let codexUsageMinimumMainRequestSequence = 0;
  let codexUsageFlushTicks = 0;
  let codexUsageOriginalFetch = null;

  function limitCodexUsageText(value) {
    const text = String(value || "");
    return text.length > CODEX_USAGE_MAX_FIELD_CHARS
      ? text.slice(0, CODEX_USAGE_MAX_FIELD_CHARS)
      : text;
  }

  function isCodexUsageTruncated(value) {
    return String(value || "").length > CODEX_USAGE_MAX_FIELD_CHARS;
  }

  function looksLikeCodexUsageJson(contentType, bodyText) {
    return String(contentType || "").toLowerCase().includes("json")
      || /^[\\s\\n]*[{\\[]/.test(String(bodyText || ""));
  }

  function requestUrl(input) {
    try {
      if (typeof input === "string") {
        return input;
      }
      if (input && typeof input.url === "string") {
        return input.url;
      }
    } catch (_error) {
      return "";
    }
    return "";
  }

  function shouldCaptureCodexUsageUrl(url) {
    try {
      const parsed = new URL(url, location.origin);
      return parsed.origin === location.origin
        && parsed.pathname.startsWith("/backend-api/wham/");
    } catch (_error) {
      return false;
    }
  }

  function codexUsageIsMainUsageUrl(url) {
    try {
      const parsed = new URL(String(url || ""), location.origin);
      const path = parsed.pathname.replace(/\\/+$/, "") || "/";
      return parsed.origin === location.origin
        && path === "/backend-api/wham/usage";
    } catch (_error) {
      return false;
    }
  }

  function codexUsageApiResponseKey(item) {
    let url = String((item && item.url) || "");
    try {
      const parsed = new URL(url, location.origin);
      const path = parsed.pathname.replace(/\\/+$/, "") || "/";
      url = `${parsed.origin}${path}`;
    } catch (_error) {
      // Keep malformed diagnostic URLs isolated without breaking the hook.
    }
    return [item.source || "", url].join("\\n");
  }

  function codexUsageApiResponseSequence(item) {
    const value = item && item.requestSequence;
    return Number.isInteger(value) && value >= 0 ? value : null;
  }

  function codexUsageApiResponseIsNewer(candidate, current) {
    const candidateSequence = codexUsageApiResponseSequence(candidate);
    const currentSequence = codexUsageApiResponseSequence(current);
    return !(
      currentSequence !== null
      && (candidateSequence === null || candidateSequence < currentSequence)
    );
  }

  function rememberCodexUsageApiResponse(item, requestId = null) {
    const requestSequence = codexUsageApiResponseSequence(item);
    if (
      codexUsageIsMainUsageUrl(item.url)
      && requestSequence !== null
      && requestSequence < codexUsageMinimumMainRequestSequence
    ) {
      return;
    }
    const key = codexUsageApiResponseKey(item);
    for (let index = codexUsageCapturedApiResponses.length - 1; index >= 0; index -= 1) {
      if (codexUsageApiResponseKey(codexUsageCapturedApiResponses[index]) === key) {
        if (!codexUsageApiResponseIsNewer(item, codexUsageCapturedApiResponses[index])) {
          if (requestId !== null && requestId !== undefined) {
            flushCodexUsageApiResponses(requestId);
          }
          return;
        }
        codexUsageCapturedApiResponses.splice(index, 1);
        break;
      }
    }
    codexUsageCapturedApiResponses.push(item);
    while (codexUsageCapturedApiResponses.length > CODEX_USAGE_CAPTURED_API_LIMIT) {
      codexUsageCapturedApiResponses.shift();
    }
    flushCodexUsageApiResponses(requestId);
  }

  function forgetCodexUsageMainUsageResponses() {
    codexUsageMinimumMainRequestSequence = codexUsageFetchSequence + 1;
    for (let index = codexUsageCapturedApiResponses.length - 1; index >= 0; index -= 1) {
      if (codexUsageIsMainUsageUrl(codexUsageCapturedApiResponses[index].url)) {
        codexUsageCapturedApiResponses.splice(index, 1);
      }
    }
  }

  function flushCodexUsageApiResponses(requestId = null) {
    if (!codexUsageCapturedApiResponses.length) {
      return;
    }
    const message = {
      type: "codexUsageApiResponses",
      responses: codexUsageCapturedApiResponses.slice()
    };
    if (requestId !== null && requestId !== undefined) {
      message.requestId = String(requestId);
    }
    window.postMessage(message, location.origin);
  }

  async function captureCodexUsageFetchResponse(
    url,
    response,
    requestSequence,
    requestId = null
  ) {
    if (!shouldCaptureCodexUsageUrl(url)) {
      return;
    }
    try {
      const clone = response.clone();
      const contentType = clone.headers.get("content-type") || "";
      const bodyText = await clone.text();
      const isJson = looksLikeCodexUsageJson(contentType, bodyText);
      rememberCodexUsageApiResponse({
        source: "page-fetch",
        url: new URL(url, location.origin).href,
        requestSequence,
        status: clone.status,
        ok: clone.ok,
        contentType,
        bodyText: isJson ? limitCodexUsageText(bodyText) : "",
        bodyExcerpt: isJson ? "" : limitCodexUsageText(bodyText).slice(0, 500),
        truncated: isJson ? isCodexUsageTruncated(bodyText) : false
      }, requestId);
    } catch (error) {
      rememberCodexUsageApiResponse({
        source: "page-fetch",
        url: new URL(url, location.origin).href,
        requestSequence,
        error: String(error)
      }, requestId);
    }
  }

  codexUsageOriginalFetch = typeof window.fetch === "function"
    ? window.fetch.bind(window)
    : null;
  if (!window.__codexUsageFetchHookInstalled && codexUsageOriginalFetch) {
    window.__codexUsageFetchHookInstalled = true;
    const originalFetch = codexUsageOriginalFetch;
    window.fetch = async (...args) => {
      const url = requestUrl(args[0]);
      const requestSequence = ++codexUsageFetchSequence;
      const response = await originalFetch(...args);
      captureCodexUsageFetchResponse(url, response, requestSequence);
      return response;
    };
  }

  async function refreshCodexUsageUsage(requestId) {
    const url = new URL("/backend-api/wham/usage", location.origin).href;
    forgetCodexUsageMainUsageResponses();
    const requestSequence = ++codexUsageFetchSequence;
    if (typeof codexUsageOriginalFetch !== "function") {
      window.postMessage({
        type: "codexUsageApiResponses",
        requestId: String(requestId),
        responses: [{
          source: "page-refresh",
          url,
          error: "page fetch unavailable"
        }]
      }, location.origin);
      return;
    }
    try {
      const response = await codexUsageOriginalFetch(url, {
        method: "GET",
        credentials: "include",
        cache: "no-store",
        headers: { "Accept": "application/json" }
      });
      await captureCodexUsageFetchResponse(url, response, requestSequence, requestId);
    } catch (error) {
      rememberCodexUsageApiResponse({
        source: "page-fetch",
        url,
        requestSequence,
        error: String(error)
      }, requestId);
    }
  }

  window.addEventListener("message", (event) => {
    if (
      event.source !== window
      || !event.data
      || event.data.type !== "codexUsageRefresh"
      || event.data.requestId === undefined
      || event.data.requestId === null
    ) {
      return;
    }
    refreshCodexUsageUsage(event.data.requestId);
  });

  const flushTimer = setInterval(() => {
    codexUsageFlushTicks += 1;
    flushCodexUsageApiResponses();
    if (codexUsageFlushTicks >= CODEX_USAGE_FLUSH_TICKS) {
      clearInterval(flushTimer);
    }
  }, CODEX_USAGE_FLUSH_INTERVAL_MS);
})();
"""
