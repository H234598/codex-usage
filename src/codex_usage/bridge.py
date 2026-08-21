from __future__ import annotations

import hmac
import ipaddress
import json
import math
import re
import secrets
import ssl
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .account_lock import account_lock
from .config import (
    AppConfig,
    default_state_dir,
    load_config,
    resolve_account,
)
from .direct import (
    DirectAuthError,
    _credit_window,
    auth_identity_for_account,
    auth_plan_type_for_account,
    canonical_backend_identity,
)
from .extractor import (
    LOCAL_TZ,
    MAX_JSON_CANDIDATES,
    JsonCandidate,
    extract_windows,
    load_json_candidate,
)
from .identity import (
    backend_identity_from_candidates,
    backend_identity_from_payload,
    backend_plan_type_from_candidates,
    select_identity_consistent_candidates,
)
from .json_utils import loads_strict
from .models import Account, AccountStatus, AccountUsage, UsagePool
from .private_io import (
    assert_no_symlink_ancestors,
    ensure_private_directory,
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
from .usage_resets import parse_usage_resets

MAX_INGEST_BYTES = 10_000_000
# Keep bridge collection bounded to the extractor's maximum candidate set.
MAX_BRIDGE_API_RESPONSES = MAX_JSON_CANDIDATES
BRIDGE_ACCOUNT_HEADER = "X-Codex-Usage-Account"
BRIDGE_MAX_CONNECTIONS = 64
BRIDGE_REQUEST_TIMEOUT_SECONDS = 15
MAX_CAPTURE_FUTURE_SECONDS = 5 * 60
AUTHENTICATED_BRIDGE_GRACE_SECONDS = 60
BRIDGE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
CHROME_EXTENSION_ORIGIN_RE = re.compile(r"chrome-extension://[a-p]{32}")
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
KNOWN_BRIDGE_RESPONSE_SOURCES = frozenset(
    ("content-probe", "page-fetch", "page-hook", "page-refresh")
)


def usage_from_ingest_payload(account: Account, payload: dict[str, Any]) -> AccountUsage:
    if not isinstance(account, Account):
        raise ValueError("account is invalid")
    if not isinstance(payload, dict):
        raise ValueError("ingest payload must be an object")
    captured_at = _parse_captured_at(_captured_at_value(payload))
    body_text = _combined_payload_text(payload)
    text_sources = _payload_text_sources(payload)
    auth_user_id, auth_account_id = auth_identity_for_account(account)
    raw_json_candidates = _json_candidates_from_payload(payload)
    json_candidates = select_identity_consistent_candidates(
        raw_json_candidates,
        auth_user_id=auth_user_id,
        auth_account_id=auth_account_id,
    )
    identity_candidates = json_candidates
    if not identity_candidates:
        # A rejected user-only response may still explain why this capture is
        # partial. Keep its identity metadata for validation, never its limits.
        identity_candidates = [
            candidate
            for candidate in raw_json_candidates
            if backend_identity_from_payload(candidate.payload) != (None, None)
        ]
    # A structured response with usage values is authoritative. If it only
    # identifies the already authenticated account, the rendered graph can
    # still be the only source for the limits; allow that fallback only after
    # the backend account identity has been fully confirmed.
    structured_identity_present = any(
        backend_identity_from_payload(candidate.payload) != (None, None)
        for candidate in identity_candidates
    )
    json_windows = extract_windows(
        body_text="",
        json_candidates=json_candidates,
        text_sources=(),
        now=captured_at,
    )
    raw_json_windows = extract_windows(
        body_text="",
        json_candidates=raw_json_candidates,
        text_sources=(),
        now=captured_at,
    )
    json_has_usage = any(
        window is not None and window.has_usage_value
        for window in (*json_windows, *raw_json_windows)
    )
    allow_dom_fallback = (
        not structured_identity_present
        or _structured_identity_matches_account(
            account,
            json_candidates,
            auth_user_id=auth_user_id,
            auth_account_id=auth_account_id,
        )
    )
    if allow_dom_fallback:
        five_hour, weekly = extract_windows(
            body_text="",
            json_candidates=json_candidates,
            text_sources=text_sources,
            now=captured_at,
        )
    else:
        five_hour, weekly = json_windows
    credits = next(
        (
            window
            for candidate in json_candidates
            for window in (_credit_window(candidate.payload, captured_at),)
            if window is not None
        ),
        None,
    )
    backend_user_id, backend_account_id = backend_identity_from_candidates(identity_candidates)
    backend_plan_type = backend_plan_type_from_candidates(identity_candidates)
    auth_plan_type = auth_plan_type_for_account(account)
    backend_user_id, backend_account_id = canonical_backend_identity(
        backend_user_id,
        backend_account_id,
        auth_user_id=auth_user_id,
        auth_account_id=auth_account_id,
        auth_plan_type=auth_plan_type,
        backend_plan_type=backend_plan_type,
        # Manual CLI ingest has no backend response identity. The secure
        # browser bridge path enforces identity in ingest_and_save(); keep
        # parser-only/manual diagnostics usable without attributing them.
        require_backend_identity=bool(auth_user_id or auth_account_id),
        require_backend_account_id=bool(auth_account_id and json_has_usage),
        # Browser cookies do not prove which account is active when WHAM
        # echoes the shared user ID as account_id. Never attribute those
        # limits to a configured account; the direct backend has its own
        # explicit account header and is intentionally unaffected here.
        reject_ambiguous_backend_identity=bool(auth_account_id and backend_account_id),
    )
    status = (
        AccountStatus.OK
        if five_hour is not None
        and weekly is not None
        and five_hour.has_usage_value
        and weekly.has_usage_value
        else AccountStatus.PARTIAL
    )
    error = (
        _ingest_error(body_text, payload) if status != AccountStatus.OK else None
    )
    cache_invalidated = not any(
        window is not None and window.has_usage_value
        for window in (five_hour, weekly)
    )
    bridge_windows = tuple(
        window
        for window in (five_hour, weekly)
        if window is not None
    )
    main = (
        UsagePool(
            key="main",
            display_name="Codex",
            windows=bridge_windows,
            availability_sources=("usage", "browser"),
        )
        if bridge_windows
        else None
    )
    source_urls = {_redact_url(payload.get("url"))}
    source_urls.update(_redact_url(candidate.url) for candidate in identity_candidates)
    source_urls.discard("")
    return AccountUsage(
        account_id=account.id,
        label=account.label,
        captured_at=captured_at,
        five_hour=five_hour,
        weekly=weekly,
        credits=credits,
        main=main,
        usage_resets=parse_usage_resets(payload),
        status=status,
        error=error,
        source_urls=tuple(sorted(source_urls)),
        backend_configured=account.backend,
        backend_used="browser",
        backend_user_id=backend_user_id,
        backend_account_id=backend_account_id,
        cache_invalidated=cache_invalidated,
    )


def _structured_identity_matches_account(
    account: Account,
    candidates: list[JsonCandidate],
    *,
    auth_user_id: str | None,
    auth_account_id: str | None,
) -> bool:
    if not account.auth_json_path:
        return False
    backend_user_id, backend_account_id = backend_identity_from_candidates(candidates)
    if not backend_account_id:
        return False
    try:
        canonical_backend_identity(
            backend_user_id,
            backend_account_id,
            auth_user_id=auth_user_id,
            auth_account_id=auth_account_id,
            require_backend_identity=True,
        )
    except ValueError:
        return False
    return True


def _ingest_error(body_text: str, payload: dict[str, Any]) -> str | None:
    text_length = payload.get("textLength") if payload.get("textLength") is not None else None
    context = (
        f" url={_safe_context_value(_redact_url(payload.get('url')), 200)}"
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
    if not isinstance(account_id, str):
        raise ValueError("account id is invalid")
    if not isinstance(payload, dict):
        raise ValueError("debug payload must be an object")
    if snapshot_dir is not None and not isinstance(snapshot_dir, Path):
        raise ValueError("snapshot directory is invalid")
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
        try:
            ensure_private_directory(directory, label="debug directory")
        except OSError as exc:
            raise ValueError("could not secure debug directory") from exc
        path = directory / f"{safe_account_id}-last-ingest.json"
        if state_generation != current_generation:
            return path
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"debug path must be a regular file: {path}")
        debug_payload = {
            field: payload[field] for field in DEBUG_PAYLOAD_FIELDS if field in payload
        }
        if "url" in debug_payload:
            debug_payload["url"] = _redact_url(debug_payload.get("url"))
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
    return "\n\n".join(text for _source, text in _payload_text_sources(payload))


def _payload_text_sources(payload: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    sources: list[tuple[str, str]] = []
    seen: set[str] = set()
    truncated_fields = _truncated_payload_fields(payload)
    if truncated_fields is None:
        return ()
    for field in TEXT_PAYLOAD_FIELDS:
        if field in truncated_fields:
            continue
        value = payload.get(field)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        sources.append((field, text))
    return tuple(sources)


def _truncated_payload_fields(payload: dict[str, Any]) -> set[str] | None:
    if "truncatedFields" not in payload:
        return set()
    raw_fields = payload["truncatedFields"]
    if not isinstance(raw_fields, dict):
        return None
    truncated: set[str] = set()
    for field in TEXT_PAYLOAD_FIELDS:
        if field not in raw_fields:
            continue
        value = raw_fields[field]
        if not isinstance(value, bool):
            return None
        if value:
            truncated.add(field)
    return truncated


def _json_candidates_from_payload(payload: dict[str, Any]) -> list[JsonCandidate]:
    responses_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    response_sequences: dict[tuple[str, str], int | None] = {}
    conflicting_keys: set[tuple[str, str]] = set()
    response_count = 0
    for field in ("apiResponses", "api_responses"):
        value = payload.get(field)
        if not isinstance(value, list):
            continue
        response_count += len(value)
        if response_count > MAX_BRIDGE_API_RESPONSES:
            return []
        for item in value:
            if not isinstance(item, dict):
                continue
            source = _bridge_response_source(item)
            if source is None:
                continue
            raw_url = item.get("url")
            if not isinstance(raw_url, str):
                continue
            url = _redact_url(raw_url)
            if not url:
                continue
            key = (source, url)
            if "requestSequence" in item:
                sequence = item["requestSequence"]
                if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                    continue
            else:
                sequence = None
            previous_sequence = response_sequences.get(key)
            if key in responses_by_key:
                if previous_sequence is not None and (
                    sequence is None or sequence < previous_sequence
                ):
                    continue
                if sequence is None:
                    responses_by_key[key] = item
                    response_sequences[key] = sequence
                    continue
                if sequence == previous_sequence:
                    if _response_without_sequence(responses_by_key[key]) != (
                        _response_without_sequence(item)
                    ):
                        conflicting_keys.add(key)
                    continue
                conflicting_keys.discard(key)
            responses_by_key[key] = item
            response_sequences[key] = sequence

    for key in conflicting_keys:
        responses_by_key.pop(key, None)
        response_sequences.pop(key, None)

    ordered_candidates: list[tuple[bool, int, int, int, JsonCandidate]] = []
    for candidate_index, item in enumerate(responses_by_key.values()):
        if not _response_metadata_is_valid(item):
            continue
        raw_url = item.get("url")
        if not isinstance(raw_url, str):
            continue
        url = _redact_url(raw_url)
        body_values: list[str] = []
        body_fields_valid = True
        for field in ("bodyText", "body", "text"):
            if field not in item:
                continue
            value = item[field]
            if not isinstance(value, str):
                body_fields_valid = False
                break
            body_values.append(value)
        if not body_fields_valid:
            continue
        body = next((value for value in body_values if value.strip()), None)
        if not url or not isinstance(body, str):
            continue
        candidate = load_json_candidate(url, body)
        if candidate is not None:
            source = _bridge_response_source(item)
            if source is None:
                continue
            sequence = response_sequences.get(
                (source, url)
            )
            ordered_candidates.append(
                (
                    sequence is not None,
                    sequence if sequence is not None else -1,
                    _bridge_response_source_priority(source),
                    candidate_index,
                    candidate,
                )
            )

    # The extension retains one response per capture source. Sort the surviving
    # candidates by freshness so extractor and identity tie-breakers cannot
    # depend on the order in which sources arrived in the ingest payload.
    ordered_candidates.sort(key=lambda item: item[:4])
    return [item[4] for item in ordered_candidates]


def _response_without_sequence(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in item.items()
        if key != "requestSequence"
    }


def _bridge_response_source(item: dict[str, Any]) -> str | None:
    value = item.get("source", "")
    if not isinstance(value, str):
        return None
    # Empty source keeps compatibility with older manually generated payloads.
    # Named sources affect candidate freshness and must come from known hooks.
    return value if not value or value in KNOWN_BRIDGE_RESPONSE_SOURCES else None


def _response_metadata_is_valid(item: dict[str, Any]) -> bool:
    if any(field not in item for field in ("status", "ok", "truncated")):
        return False
    truncated = item["truncated"]
    if not isinstance(truncated, bool) or truncated:
        return False
    status = item["status"]
    if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status < 300:
        return False
    ok = item["ok"]
    if not isinstance(ok, bool) or not ok:
        return False
    content_types = []
    for field in ("contentType", "content_type"):
        if field not in item:
            continue
        value = item[field]
        if not isinstance(value, str) or not value.strip() or "json" not in value.casefold():
            return False
        content_types.append(value)
    return bool(content_types)


def _bridge_response_source_priority(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    source = value
    return {
        "content-probe": 10,
        "page-refresh": 20,
        "page-fetch": 30,
    }.get(source, 0)


def _safe_excerpt(value: str, limit: int = 240) -> str:
    excerpt = re.sub(r"\s+", " ", value).strip()
    excerpt = excerpt.replace("\\", "\\\\").replace('"', '\\"')
    if len(excerpt) <= limit:
        return excerpt
    return excerpt[: limit - 3] + "..."


def _safe_context_value(value: Any, limit: int) -> str:
    if value is None or value == "":
        return "-"
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return "-"
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in value)


def _validate_bridge_account_ref(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,64}", value
    ):
        raise ValueError("account id must be valid for bridge token storage")
    return value


def _validate_bridge_endpoint(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("endpoint must be an absolute HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError("endpoint must be an absolute HTTP(S) URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or (parsed_port is not None and not 1 <= parsed_port <= 65535)
        or not parsed.path.startswith("/")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint must be an absolute HTTP(S) URL without credentials/query")
    return value


def _validate_bridge_interval(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 60:
        raise ValueError("interval must be at least 60 seconds")
    return value


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
        redacted["url"] = _redact_url(redacted.get("url"))
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
        if not math.isfinite(value) or value < 0:
            return None
        try:
            return int(value)
        except (OverflowError, ValueError):
            return None
    if isinstance(value, str) and value.isdecimal():
        try:
            return int(value)
        except ValueError:
            return None
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
    account_ref = _validate_bridge_account_ref(account_ref)
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
    account_ref = _validate_bridge_account_ref(account_ref)
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
    try:
        account_ref = _validate_bridge_account_ref(account_ref)
    except ValueError:
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
    if not text.endswith("\n"):
        raise ValueError("invalid bridge token file")
    return _validate_bridge_token(text[:-1])


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
    account_ref = _validate_bridge_account_ref(account_ref)
    endpoint = _validate_bridge_endpoint(endpoint)
    interval_seconds = _validate_bridge_interval(interval_seconds)
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
  const maxFieldChars = 2000000;
  const maxAckChars = 4096;
  const maxAckChunkBytes = 65536;
  const maxSerializedPayloadBytes = 9500000;
  const textEncoder = typeof TextEncoder === "function" ? new TextEncoder() : null;
  let sendInFlight = null;
  let sendPending = false;

  function limitText(value) {{
    const text = String(value || "");
    return text.length > maxFieldChars ? text.slice(0, maxFieldChars) : text;
  }}

  function utf8ByteLength(value) {{
    const text = String(value || "");
    return textEncoder ? textEncoder.encode(text).length : text.length * 6;
  }}

  function serializedPayloadBytes(payload) {{
    return utf8ByteLength(JSON.stringify(payload));
  }}

  function fitPayload(payload) {{
    const metadataFields = new Set([
      "account", "url", "title", "capturedAt", "readyState"
    ]);
    let serializedBytes = serializedPayloadBytes(payload);
    if (serializedBytes < maxSerializedPayloadBytes) {{
      return payload;
    }}
    for (let attempt = 0; attempt < 8; attempt += 1) {{
      if (serializedBytes < maxSerializedPayloadBytes) {{
        return payload;
      }}
      const candidates = [];
      for (const field of Object.keys(payload)) {{
        if (
          typeof payload[field] === "string"
          && !metadataFields.has(field)
          && payload[field].length > 1000
        ) {{
          candidates.push(field);
        }}
      }}
      if (!candidates.length) {{
        break;
      }}
      const trimRatio = Math.min(
        0.5,
        (maxSerializedPayloadBytes / serializedBytes) * 0.9,
      );
      for (const field of candidates) {{
        payload[field] = payload[field].slice(
          0,
          Math.floor(payload[field].length * Math.max(0, trimRatio)),
        );
      }}
      serializedBytes = serializedPayloadBytes(payload);
    }}
    for (const field of Object.keys(payload)) {{
      if (
        typeof payload[field] === "string"
        && !metadataFields.has(field)
      ) {{
        payload[field] = "";
      }}
    }}
    return payload;
  }}

  async function readBoundedAckText(response) {{
    const reader = response && response.body && typeof response.body.getReader === "function"
      ? response.body.getReader()
      : null;
    if (!reader) {{
      const text = await response.text();
      return String(text || "").slice(0, maxAckChars);
    }}
    const decoder = new TextDecoder();
    const parts = [];
    let length = 0;
    let shouldCancel = false;
    while (true) {{
      const item = await reader.read();
      if (item.done) {{
        const tail = decoder.decode();
        const remaining = maxAckChars - length;
        parts.push(tail.slice(0, remaining));
        break;
      }}
      const bytes = item.value || new Uint8Array();
      for (let offset = 0; offset < bytes.length; offset += maxAckChunkBytes) {{
        if (length >= maxAckChars) {{
          shouldCancel = true;
          break;
        }}
        const chunk = decoder.decode(
          bytes.subarray(offset, offset + maxAckChunkBytes),
          {{ stream: true }},
        );
        const remaining = maxAckChars - length;
        if (chunk.length > remaining) {{
          parts.push(chunk.slice(0, remaining));
          shouldCancel = true;
          break;
        }}
        parts.push(chunk);
        length += chunk.length;
      }}
      if (shouldCancel) {{
        try {{
          await reader.cancel();
        }} catch (_error) {{
          // The response is already bounded; cancellation is best effort.
        }}
        break;
      }}
    }}
    return parts.join("");
  }}

  function collectAttributeText() {{
    const attrs = ["aria-label", "aria-valuetext", "aria-valuenow", "title", "alt"];
    if (!document.querySelectorAll) {{
      return {{ text: "", truncated: false }};
    }}
    const selector = attrs.reduce(
      (result, name) => result ? `${{result}},[${{name}}]` : `[${{name}}]`,
      "",
    );
    const parts = [];
    let length = 0;
    let truncated = false;
    const elements = document.querySelectorAll(selector);
    for (let index = 0; index < elements.length; index += 1) {{
      const element = elements[index];
      for (const name of attrs) {{
        const value = element.getAttribute(name);
        const text = String(value || "");
        if (!text.trim()) {{
          continue;
        }}
        const prefix = length ? "\\n" : "";
        const remaining = maxFieldChars - length;
        const chunk = (prefix + text).slice(0, remaining);
        parts.push(chunk);
        length += chunk.length;
        if (chunk.length < prefix.length + text.length) {{
          truncated = true;
          return {{ text: parts.join(""), truncated }};
        }}
      }}
    }}
    return {{ text: parts.join(""), truncated }};
  }}

  function collectSvgText() {{
    if (!document.querySelectorAll) {{
      return {{ text: "", truncated: false }};
    }}
    const parts = [];
    let length = 0;
    let truncated = false;
    const elements = document.querySelectorAll("svg text, svg title, svg desc");
    for (let index = 0; index < elements.length; index += 1) {{
      const element = elements[index];
      const text = String(element.textContent || "");
      if (!text.trim()) {{
        continue;
      }}
      const prefix = length ? "\\n" : "";
      const remaining = maxFieldChars - length;
      const chunk = (prefix + text).slice(0, remaining);
      parts.push(chunk);
      length += chunk.length;
      if (chunk.length < prefix.length + text.length) {{
        truncated = true;
        break;
      }}
    }}
    return {{ text: parts.join(""), truncated }};
  }}

  function boundedVisibleText(root) {{
    if (!root) {{
      return {{ text: "", truncated: false }};
    }}
    if (root.nodeType !== 1) {{
      const fallback = String(root.innerText || "");
      return {{
        text: fallback.slice(0, maxFieldChars),
        truncated: fallback.length > maxFieldChars
      }};
    }}
    const maxNodes = 500000;
    const skippedTags = new Set([
      "script", "style", "link", "meta", "noscript", "template"
    ]);
    const blockTags = new Set([
      "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
      "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
      "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol",
      "p", "pre", "section", "table", "td", "th", "tr", "ul"
    ]);
    const parts = [];
    let length = 0;
    let nodesSeen = 0;
    let truncated = false;

    function append(value) {{
      if (length >= maxFieldChars) {{
        truncated = true;
        return;
      }}
      const text = String(value || "");
      const remaining = maxFieldChars - length;
      const chunk = text.slice(0, remaining);
      parts.push(chunk);
      length += chunk.length;
      truncated = truncated || chunk.length < text.length;
    }}

    const stack = [{{ kind: "visit", node: root }}];
    while (stack.length && length < maxFieldChars && nodesSeen < maxNodes) {{
      const item = stack.pop();
      if (item.kind === "close") {{
        append("\\n");
        continue;
      }}
      const node = item.node;
      if (!node) {{
        continue;
      }}
      nodesSeen += 1;
      if (node.nodeType === 3) {{
        append(node.nodeValue);
        continue;
      }}
      if (node.nodeType !== 1) {{
        continue;
      }}
      const tag = String(node.tagName || "").toLowerCase();
      if (!tag || skippedTags.has(tag) || node.hidden) {{
        continue;
      }}
      const style = typeof getComputedStyle === "function"
        ? getComputedStyle(node)
        : null;
      if (
        style
        && (style.display === "none" || style.visibility === "hidden")
      ) {{
        continue;
      }}
      if (blockTags.has(tag)) {{
        append("\\n");
        stack.push({{ kind: "close" }});
      }}
      const children = node.childNodes || [];
      for (let index = children.length - 1; index >= 0; index -= 1) {{
        stack.push({{ kind: "visit", node: children[index] }});
      }}
    }}
    if (stack.length) {{
      truncated = true;
    }}
    return {{ text: parts.join(""), truncated }};
  }}

  function boundedDomCapture(root) {{
    if (!root) {{
      return {{ text: "", html: "", textTruncated: false, htmlTruncated: false }};
    }}
    const maxNodes = 500000;
    const skippedTags = new Set([
      "script", "style", "link", "meta", "noscript", "template"
    ]);
    const voidTags = new Set([
      "area", "base", "br", "col", "embed", "hr", "img", "input",
      "link", "meta", "param", "source", "track", "wbr"
    ]);
    const attributesToKeep = new Set([
      "style", "class", "role", "hidden", "aria-hidden", "aria-valuenow",
      "aria-valuemin", "aria-valuemax", "aria-label", "title"
    ]);
    const textParts = [];
    const htmlParts = [];
    let textLength = 0;
    let htmlLength = 0;
    let nodesSeen = 0;
    let textTruncated = false;
    let htmlTruncated = false;

    function append(parts, value, limit, state) {{
      const text = String(value || "");
      if (state.length >= limit) {{
        state.truncated = true;
        return;
      }}
      const remaining = limit - state.length;
      parts.push(text.slice(0, remaining));
      state.length += Math.min(text.length, remaining);
      state.truncated = state.truncated || text.length > remaining;
    }}

    function escape(value) {{
      return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\"/g, "&quot;");
    }}

    const stack = [{{ kind: "visit", node: root }}];
    while (
      stack.length
      && nodesSeen < maxNodes
      && (textLength < maxFieldChars || htmlLength < maxFieldChars)
    ) {{
      const item = stack.pop();
      if (item.kind === "close") {{
        const closingState = {{ length: htmlLength, truncated: htmlTruncated }};
        append(htmlParts, "</" + item.tag + ">", maxFieldChars, closingState);
        htmlLength = closingState.length;
        htmlTruncated = closingState.truncated;
        continue;
      }}
      const node = item.node;
      if (!node) {{
        continue;
      }}
      nodesSeen += 1;
      if (node.nodeType === 3) {{
        const state = {{ length: textLength, truncated: textTruncated }};
        append(textParts, node.nodeValue, maxFieldChars, state);
        textLength = state.length;
        textTruncated = state.truncated;
        const htmlState = {{ length: htmlLength, truncated: htmlTruncated }};
        append(htmlParts, escape(node.nodeValue), maxFieldChars, htmlState);
        htmlLength = htmlState.length;
        htmlTruncated = htmlState.truncated;
        continue;
      }}
      if (node.nodeType !== 1) {{
        continue;
      }}
      const tag = String(node.tagName || "").toLowerCase();
      if (!tag || skippedTags.has(tag)) {{
        continue;
      }}
      const htmlState = {{ length: htmlLength, truncated: htmlTruncated }};
      append(htmlParts, "<" + tag, maxFieldChars, htmlState);
      const attributes = node.attributes || [];
      for (let index = 0; index < attributes.length; index += 1) {{
        const attribute = attributes[index];
        const name = String(attribute.name || "").toLowerCase();
        if (attributesToKeep.has(name)) {{
          append(
            htmlParts,
            " " + attribute.name + '=\\"' + escape(attribute.value) + '\\"',
            maxFieldChars,
            htmlState,
          );
        }}
      }}
      append(htmlParts, ">", maxFieldChars, htmlState);
      htmlLength = htmlState.length;
      htmlTruncated = htmlState.truncated;
      if (!voidTags.has(tag)) {{
        stack.push({{ kind: "close", tag }});
      }}
      const children = node.childNodes || [];
      for (let index = children.length - 1; index >= 0; index -= 1) {{
        stack.push({{ kind: "visit", node: children[index] }});
      }}
    }}
    if (stack.length) {{
      textTruncated = true;
      htmlTruncated = true;
    }}
    return {{
      text: textParts.join(""),
      html: htmlParts.join(""),
      textTruncated,
      htmlTruncated,
    }};
  }}

  function collectPayload() {{
    const bodyCapture = boundedVisibleText(document.body);
    const bodyText = bodyCapture.text;
    const root = boundedDomCapture(document.documentElement);
    const domText = root.text;
    const accessibilityCapture = collectAttributeText();
    const accessibilityText = accessibilityCapture.text;
    const svgCapture = collectSvgText();
    const svgText = svgCapture.text;
    const htmlText = root.html;
    const searchText = [bodyText, domText, accessibilityText, svgText, htmlText]
      .filter((value) => value && String(value).trim())
      .join("\\n\\n");
    return {{
      account,
      url: location.href,
      title: document.title,
      capturedAt: new Date().toISOString(),
      readyState: document.readyState,
      textLength: searchText.length,
      htmlLength: htmlText.length,
      visibleTextLength: bodyText.length,
      fieldLengths: {{
        bodyText: bodyText.length,
        domText: domText.length,
        accessibilityText: accessibilityText.length,
        svgText: svgText.length,
        htmlText: htmlText.length
      }},
      truncatedFields: {{
        bodyText: bodyCapture.truncated,
        domText: root.textTruncated,
        accessibilityText: accessibilityCapture.truncated,
        svgText: svgCapture.truncated,
        htmlText: root.htmlTruncated
      }},
      bodyText: limitText(bodyText),
      domText: limitText(domText),
      accessibilityText: limitText(accessibilityText),
      svgText: limitText(svgText),
      htmlText: limitText(htmlText)
    }};
  }}

  async function sendCodexUsage() {{
    if (sendInFlight) {{
      sendPending = true;
      return sendInFlight;
    }}
    const operation = (async () => {{
      try {{
        const response = await fetch(endpoint, {{
          method: "POST",
          headers: {{
            "Content-Type": "application/json",
            "X-Codex-Usage-Account": account,
            "Authorization": "Bearer " + token
          }},
          body: JSON.stringify(fitPayload(collectPayload()))
        }});
        console.log("codex-usage bridge", response.status, await readBoundedAckText(response));
      }} catch (error) {{
        console.warn("codex-usage bridge failed", String(error));
      }}
    }})();
    sendInFlight = operation;
    try {{
      await operation;
    }} finally {{
      if (sendInFlight === operation) {{
        sendInFlight = null;
      }}
      if (sendPending) {{
        sendPending = false;
        void sendCodexUsage();
      }}
    }}
    return operation;
  }}
  void sendCodexUsage();
  setInterval(() => {{ void sendCodexUsage(); }}, intervalMs);
}})();"""


def write_bridge_extension(
    account_ref: str,
    output_dir: Path,
    *,
    endpoint: str,
    interval_seconds: int,
    token: str | None = None,
) -> Path:
    account_ref = _validate_bridge_account_ref(account_ref)
    if not isinstance(output_dir, Path):
        raise ValueError("extension output directory is invalid")
    endpoint = _validate_bridge_endpoint(endpoint)
    interval_seconds = _validate_bridge_interval(interval_seconds)
    token = _validate_bridge_token(token) if token else bridge_token_for_account(account_ref)
    _prepare_private_directory(output_dir, label="extension output directory")
    with private_path_lock(
        output_dir / ".codex-usage-write",
        label="bridge extension output lock",
    ):
        return _write_bridge_extension_transaction(
            account_ref,
            output_dir,
            endpoint=endpoint,
            interval_seconds=interval_seconds,
            token=token,
        )


def _write_bridge_extension_transaction(
    account_ref: str,
    output_dir: Path,
    *,
    endpoint: str,
    interval_seconds: int,
    token: str,
) -> Path:
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
        "background.js": _render_extension_background(account_ref, endpoint, token),
        "content.js": _render_extension_content(account_ref, interval_seconds),
        "page-hook.js": _render_extension_page_hook(),
    }
    paths = {filename: output_dir / filename for filename in files}
    for path in paths.values():
        _validate_extension_output_path(path)

    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}.",
        dir=str(output_dir),
    ) as transaction:
        transaction_dir = Path(transaction)
        stage_dir = transaction_dir / "stage"
        backup_dir = transaction_dir / "backup"
        stage_dir.mkdir(mode=0o700)
        backup_dir.mkdir(mode=0o700)
        for filename, content in files.items():
            _write_private_text(
                stage_dir / filename,
                content,
                label="extension staging path",
            )

        backed_up: list[tuple[Path, Path]] = []
        committed: list[Path] = []
        try:
            for path in paths.values():
                _validate_extension_output_path(path)
            for filename, path in paths.items():
                backup = backup_dir / filename
                if path.exists():
                    path.replace(backup)
                    backed_up.append((path, backup))
                (stage_dir / filename).replace(path)
                committed.append(path)
        except Exception as primary_error:
            rollback_errors: list[Exception] = []
            for path in reversed(committed):
                try:
                    if path.is_symlink() or (path.exists() and not path.is_file()):
                        raise ValueError(f"extension output path is not a regular file: {path}")
                    if path.exists():
                        path.unlink()
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            for path, backup in reversed(backed_up):
                try:
                    backup.replace(path)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                raise ExceptionGroup(
                    "bridge extension commit rollback failed",
                    [primary_error, *rollback_errors],
                ) from None
            raise
    return output_dir


def _validate_extension_output_path(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"extension output path must be a regular file: {path}")
    if path.exists() and path.stat().st_nlink != 1:
        raise ValueError(f"extension output path must not be hard-linked: {path}")


def _prepare_private_directory(path: Path, *, label: str) -> None:
    try:
        ensure_private_directory(path, label=label)
    except OSError as exc:
        raise ValueError(f"could not secure {label}") from exc


def _write_private_text(path: Path, content: str, *, label: str) -> None:
    write_private_output_text(path, content, label=label)


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = BRIDGE_MAX_CONNECTIONS

    def __init__(self, *args, tls_context=None, **kwargs):
        self._tls_context = tls_context
        super().__init__(*args, **kwargs)
        self._connection_slots = BoundedSemaphore(BRIDGE_MAX_CONNECTIONS)

    def get_request(self):
        request, client_address = super().get_request()
        if self._tls_context is None:
            return request, client_address
        wrapped_request = None
        try:
            request.settimeout(BRIDGE_REQUEST_TIMEOUT_SECONDS)
            wrapped_request = self._tls_context.wrap_socket(
                request,
                server_side=True,
                do_handshake_on_connect=False,
            )
            wrapped_request.settimeout(BRIDGE_REQUEST_TIMEOUT_SECONDS)
            return wrapped_request, client_address
        except BaseException:
            (wrapped_request or request).close()
            raise

    def process_request(self, request, client_address):
        if not self._connection_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            if self._tls_context is not None:
                try:
                    request.do_handshake()
                except (OSError, ssl.SSLError):
                    request.close()
                    return
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


def run_bridge_server(
    config: AppConfig,
    *,
    host: str,
    port: int,
    snapshot_dir: Path | None = None,
    config_path: Path | None = None,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
) -> None:
    if not isinstance(config, AppConfig):
        raise ValueError("config is invalid")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("bridge host is invalid")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("bridge port is invalid")
    for value, label in (
        (snapshot_dir, "snapshot directory"),
        (config_path, "config path"),
        (tls_cert, "TLS certificate path"),
        (tls_key, "TLS key path"),
    ):
        if value is not None and not isinstance(value, Path):
            raise ValueError(f"{label} is invalid")
    tls_context = _tls_context(tls_cert, tls_key)
    if _bridge_host_requires_tls(host) and tls_context is None:
        raise ValueError("non-loopback bridge bindings require TLS")
    try:
        selected_config_path = config_path.expanduser() if config_path else None
    except RuntimeError as exc:
        raise ValueError("config path cannot be resolved") from exc
    tokens = {
        account.id: bridge_token_for_account(account.id)
        for account in config.accounts
    }
    handler = _make_handler(
        config,
        snapshot_dir,
        tokens,
        config_path=selected_config_path,
    )
    server = _BoundedThreadingHTTPServer(
        (host, port),
        handler,
        tls_context=tls_context,
    )
    scheme = "https" if tls_context is not None else "http"
    print(f"Bridge-Server: {scheme}://{host}:{port}/ingest")
    print("Stop: Ctrl+C")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _bridge_host_requires_tls(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return False
    try:
        address = ipaddress.ip_address(normalized.strip("[]"))
    except ValueError:
        return True
    return not address.is_loopback


def _tls_context(
    tls_cert: Path | None,
    tls_key: Path | None,
) -> ssl.SSLContext | None:
    if (tls_cert is None) != (tls_key is None):
        raise ValueError("TLS requires both certificate and key")
    if tls_cert is None:
        return None
    if tls_key is None:
        raise ValueError("TLS requires both certificate and key")
    try:
        certificate = tls_cert.expanduser()
        key = tls_key.expanduser()
    except RuntimeError as exc:
        raise ValueError("TLS path cannot be resolved") from exc
    assert_no_symlink_ancestors(certificate, label="TLS certificate")
    assert_no_symlink_ancestors(key, label="TLS key")
    if certificate.is_symlink() or not certificate.is_file():
        raise ValueError(f"TLS certificate must be a regular file: {certificate}")
    if key.is_symlink() or not key.is_file():
        raise ValueError(f"TLS key must be a regular file: {key}")
    if key.stat().st_mode & 0o077:
        raise ValueError(f"TLS key permissions too broad: {key}")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(certfile=str(certificate), keyfile=str(key))
    except (OSError, ssl.SSLError) as exc:
        raise ValueError("invalid TLS certificate or key") from exc
    return context


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
        try:
            _reject_ambiguous_browser_identity(config, account, payload)
            if account.auth_json_path is not None:
                if not _usage_matches_current_auth(account, usage):
                    raise ValueError("bridge payload belongs to a different backend account")
            _reject_browser_identity_from_other_configured_account(
                config,
                account,
                payload,
            )
        except ValueError:
            _invalidate_rejected_browser_state(
                account,
                snapshot,
                current,
                snapshot_dir,
            )
            raise
        if account.auth_json_path is None and known is None:
            raise ValueError("browser account identity is not initialized")
    # Diagnostic/manual parsing may use receive time. Never let that fallback
    # cross the browser ingest trust boundary.
    if require_backend_identity:
        _parse_captured_at(_captured_at_value(payload), strict=True)
    if (
        known is not None
        and not backend_identity_matches(usage, known)
        and not _usage_matches_current_auth(account, usage)
    ):
        _invalidate_rejected_browser_state(
            account,
            snapshot,
            current,
            snapshot_dir,
        )
        raise ValueError("bridge payload belongs to a different backend account")
    if known is not None:
        try:
            if usage.captured_at < known.captured_at:
                raise ValueError("bridge payload is older than known state")
        except TypeError as exc:
            raise ValueError("bridge payload timestamps are not comparable") from exc
    if known is not None and _browser_payload_is_covered_by_authenticated_state(
        config,
        usage,
        snapshot,
        current,
    ):
        raise ValueError("browser payload cannot replace current authenticated state")
    path = save_usage_snapshot(usage, snapshot_dir)
    current_dir = snapshot_dir.parent / "current" if snapshot_dir else None
    save_current_usage(usage, current_dir)
    return usage, path


def _browser_payload_is_covered_by_authenticated_state(
    config: AppConfig,
    browser_usage: AccountUsage,
    *known_usages: AccountUsage | None,
) -> bool:
    if browser_usage.backend_used != "browser":
        return False
    freshness_window = max(int(config.interval_seconds), 60) + AUTHENTICATED_BRIDGE_GRACE_SECONDS
    for known_usage in known_usages:
        if known_usage is None:
            continue
        if (
            not isinstance(known_usage.backend_used, str)
            or known_usage.backend_used not in {"direct", "app-server"}
        ):
            continue
        if not isinstance(known_usage.status, AccountStatus) or known_usage.status not in {
            AccountStatus.OK,
            AccountStatus.PARTIAL,
            AccountStatus.BLOCKED,
        }:
            continue
        if not backend_identity_matches(browser_usage, known_usage):
            continue
        try:
            age = (browser_usage.captured_at - known_usage.captured_at).total_seconds()
        except (AttributeError, TypeError):
            # Unknown ordering must block browser data from replacing
            # authenticated state.
            return True
        if 0 <= age <= freshness_window:
            return True
    return False


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
    except TypeError as exc:
        raise ValueError("known usage timestamps are not comparable") from exc


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


def _reject_browser_identity_from_other_configured_account(
    config: AppConfig,
    account: Account,
    payload: dict[str, Any],
) -> None:
    """Do not attribute a browser cookie identity to another configured row."""
    identity = backend_identity_from_candidates(_json_candidates_from_payload(payload))
    if identity == (None, None):
        return

    matching_accounts: list[str] = []
    for candidate in config.accounts:
        if candidate.auth_json_path is None:
            continue
        try:
            auth_user_id, auth_account_id = auth_identity_for_account(candidate)
            canonical_backend_identity(
                identity[0],
                identity[1],
                auth_user_id=auth_user_id,
                auth_account_id=auth_account_id,
                require_backend_identity=True,
            )
        except (DirectAuthError, ValueError):
            continue
        matching_accounts.append(candidate.id)

    if not matching_accounts:
        return
    if account.id not in matching_accounts:
        raise ValueError("browser payload belongs to a different configured account")
    if len(matching_accounts) > 1:
        raise ValueError("browser payload has ambiguous backend account identity")


def _invalidate_rejected_browser_state(
    account: Account,
    snapshot: AccountUsage | None,
    current: AccountUsage | None,
    snapshot_dir: Path | None,
) -> None:
    """Clear browser values after a proven identity switch or mismatch."""
    if account.backend != "browser" or account.auth_json_path is not None:
        return
    known = _newest_known_usage(snapshot, current)
    if known is None:
        return
    invalidated = replace(
        _invalidate_cached_usage(account, known),
        error="cached browser usage discarded after backend identity changed",
        state_generation=load_state_generation(account.id, snapshot_dir),
    )
    save_usage_snapshot(invalidated, snapshot_dir)
    current_dir = snapshot_dir.parent / "current" if snapshot_dir else None
    save_current_usage(invalidated, current_dir)


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
        windows = _usage_windows(usage)
        return windows == ()
    auth_user_id, auth_account_id = auth_identity
    return _usage_matches_auth_identity(
        usage,
        auth_user_id=auth_user_id,
        auth_account_id=auth_account_id,
    )


def _invalidate_cached_usage(
    account: Account,
    usage: AccountUsage,
    *,
    error: str = "cached usage discarded after auth.json identity change",
) -> AccountUsage:
    return replace(
        usage,
        label=account.label,
        five_hour=None,
        weekly=None,
        main=None,
        models=(),
        status=AccountStatus.PARTIAL,
        error=error,
        backend_configured=account.backend,
        backend_used=account.backend,
        backend_user_id=None,
        backend_account_id=None,
        fallback_reason=None,
        values_captured_at=None,
        stale=True,
        cache_invalidated=True,
    )


def _account_uses_authenticated_backend(account: Account) -> bool:
    return account.backend == "app-server" or (
        account.backend == "direct" and account.auth_json_path is not None
    )


def _invalidate_browser_cache(
    account: Account,
    usage: AccountUsage,
) -> AccountUsage:
    return replace(
        _invalidate_cached_usage(account, usage),
        error=(
            "cached browser usage ignored for configured "
            f"{account.backend} backend"
        ),
    )


def _authenticated_snapshot_supersedes_browser_current(
    current: AccountUsage,
    snapshot: AccountUsage,
    interval_seconds: int,
) -> bool:
    """Prefer a fresh authoritative authenticated snapshot over browser state."""
    if current.backend_used != "browser":
        return False
    if (
        not isinstance(snapshot.backend_used, str)
        or snapshot.backend_used not in {"direct", "app-server"}
    ):
        return False
    if not isinstance(snapshot.status, AccountStatus) or snapshot.status not in {
        AccountStatus.OK,
        AccountStatus.PARTIAL,
        AccountStatus.BLOCKED,
    }:
        return False
    if snapshot.cache_invalidated:
        return False
    if not backend_identity_matches(current, snapshot):
        return False
    snapshot_windows = _usage_windows(snapshot)
    if snapshot_windows is None:
        return False
    has_usage_value = any(window.has_usage_value for window in snapshot_windows)
    authoritative_empty = (
        snapshot.status in {AccountStatus.PARTIAL, AccountStatus.BLOCKED}
        and not snapshot_windows
    )
    if not has_usage_value and not authoritative_empty:
        return False
    try:
        now = datetime.now(tz=LOCAL_TZ)
        age_seconds = (now - snapshot.captured_at).total_seconds()
        values_captured_at = snapshot.values_captured_at or snapshot.captured_at
        values_age_seconds = (now - values_captured_at).total_seconds()
    except (TypeError, AttributeError):
        # Do not merge browser values when authenticated freshness cannot be
        # established.
        return True
    freshness_window = max(int(interval_seconds), 60) + AUTHENTICATED_BRIDGE_GRACE_SECONDS
    if not 0 <= age_seconds <= freshness_window:
        return False
    if snapshot.stale and not 0 <= values_age_seconds <= freshness_window:
        return False
    return True


def _usage_windows(usage: AccountUsage) -> tuple[Any, ...] | None:
    try:
        windows: list[Any] = [usage.five_hour, usage.weekly]
        if not isinstance(usage.models, tuple):
            return None
        pools = [usage.main, *usage.models]
    except (AttributeError, TypeError):
        return None
    for pool in pools:
        if pool is None:
            continue
        pool_windows = getattr(pool, "windows", None)
        if not isinstance(pool_windows, tuple):
            return None
        windows.extend(pool_windows)
    return tuple(window for window in windows if window is not None)


def load_latest_usages(config: AppConfig, snapshot_dir: Path | None = None) -> list[AccountUsage]:
    if not isinstance(config, AppConfig):
        raise ValueError("config is invalid")
    if snapshot_dir is not None and not isinstance(snapshot_dir, Path):
        raise ValueError("snapshot directory is invalid")
    if snapshot_dir is None:
        with account_lock("__all_accounts__"):
            return _load_latest_usages_unlocked(config, snapshot_dir)
    return _load_latest_usages_unlocked(config, snapshot_dir)


def _load_latest_usages_unlocked(
    config: AppConfig,
    snapshot_dir: Path | None = None,
) -> list[AccountUsage]:
    usages: list[AccountUsage] = []
    current_dir = snapshot_dir.parent / "current" if snapshot_dir else None
    reference_at = datetime.now(tz=LOCAL_TZ)
    for account in config.accounts:
        last_success = load_usage_snapshot(account.id, snapshot_dir)
        current = load_current_usage(account.id, current_dir)
        if _capture_is_too_far_in_future(last_success, reference_at):
            last_success = None
        if _capture_is_too_far_in_future(current, reference_at):
            current = None
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
        rejected_browser: AccountUsage | None = None
        if _account_uses_authenticated_backend(account):
            for candidate in (last_success, current):
                if candidate is not None and candidate.backend_used == "browser":
                    rejected_browser = _newest_known_usage(
                        rejected_browser,
                        candidate,
                    )
            if last_success is not None and last_success.backend_used == "browser":
                last_success = None
            if current is not None and current.backend_used == "browser":
                current = None
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
        if (
            current is not None
            and last_success is not None
            and _authenticated_snapshot_supersedes_browser_current(
                current,
                last_success,
                config.interval_seconds,
            )
        ):
            usage = last_success
        elif current is not None:
            usage = merge_current_with_last_success(current, last_success)
        elif last_success is not None:
            usage = last_success
        elif rejected_browser is not None:
            usage = _invalidate_browser_cache(account, rejected_browser)
        else:
            continue
        usage = replace(usage, label=account.label)
        usage = expire_reset_windows(
            usage,
            reference_at=reference_at,
        )
        usages.append(_mark_latest_stale(usage, config.interval_seconds))
    return usages


def _capture_is_too_far_in_future(
    usage: AccountUsage | None,
    reference_at: datetime,
) -> bool:
    if usage is None:
        return False
    try:
        return usage.captured_at > reference_at + timedelta(seconds=MAX_CAPTURE_FUTURE_SECONDS)
    except (TypeError, ValueError, OverflowError):
        return True


def _mark_latest_stale(usage: AccountUsage, interval_seconds: int) -> AccountUsage:
    grace_seconds = max(60, interval_seconds + 60)
    try:
        age_seconds = (datetime.now(tz=LOCAL_TZ) - usage.captured_at).total_seconds()
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

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(BRIDGE_REQUEST_TIMEOUT_SECONDS)

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
            content_lengths = self.headers.get_all("Content-Length") or []
            transfer_encodings = self.headers.get_all("Transfer-Encoding") or []
            if len(content_lengths) != 1 or transfer_encodings:
                self._send_json(413, {"error": "invalid payload size"})
                return
            content_length_text = content_lengths[0]
            if (
                not re.fullmatch(r"[0-9]+", content_length_text)
                or len(content_length_text) > len(str(MAX_INGEST_BYTES))
            ):
                self._send_json(413, {"error": "invalid payload size"})
                return
            try:
                content_length = int(content_length_text)
            except ValueError:
                self._send_json(413, {"error": "invalid payload size"})
                return
            if content_length <= 0 or content_length > MAX_INGEST_BYTES:
                self._send_json(413, {"error": "invalid payload size"})
                return
            request_config = self._config_for_request()
            if request_config is None:
                self._send_json(503, {"error": "configuration unavailable"})
                return
            account_headers = self.headers.get_all(BRIDGE_ACCOUNT_HEADER) or []
            if len(account_headers) != 1:
                self._send_json(401, {"error": "authorization required"})
                return
            account_ref = account_headers[0]
            try:
                resolved_account = resolve_account(request_config, account_ref)
            except KeyError:
                self._send_json(401, {"error": "authorization required"})
                return
            if resolved_account.id != account_ref or not self._is_authorized(
                account_ref, request_config
            ):
                self._send_json(401, {"error": "authorization required"})
                return
            try:
                payload = loads_strict(self.rfile.read(content_length).decode("utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                self._send_json(400, {"error": "invalid JSON payload"})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "invalid JSON payload"})
                return

            if payload.get("account") != account_ref:
                self._send_json(400, {"error": "account mismatch"})
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
            self.send_header(
                "Access-Control-Allow-Headers",
                f"Content-Type, Authorization, {BRIDGE_ACCOUNT_HEADER}",
            )
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
            origins = self.headers.get_all("Origin") or []
            if len(origins) != 1:
                return False
            origin = origins[0]
            return origin == "https://chatgpt.com" or bool(
                CHROME_EXTENSION_ORIGIN_RE.fullmatch(origin)
            )

        def _config_for_request(self) -> AppConfig | None:
            if config_path is None:
                return config
            try:
                return load_config(config_path)
            except (OSError, UnicodeError, ValueError):
                return None

        def _is_authorized(self, account_ref: str, request_config: AppConfig) -> bool:
            if not isinstance(account_ref, str) or not account_ref:
                return False
            try:
                resolve_account(request_config, account_ref)
            except KeyError:
                return False
            authorizations = self.headers.get_all("Authorization") or []
            if len(authorizations) != 1:
                return False
            authorization = authorizations[0]
            prefix = "Bearer "
            if config_path is None and not tokens.get(account_ref):
                return False
            if not authorization.startswith(prefix):
                return False
            supplied = authorization[len(prefix):]
            if not supplied or supplied != supplied.strip():
                return False
            return bridge_token_matches(account_ref, supplied)

    return BridgeHandler


def _log_bridge_error(message: str, exc: Exception) -> None:
    print(f"{message}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def _captured_at_value(payload: dict[str, Any]) -> Any:
    fields = [field for field in ("capturedAt", "captured_at") if field in payload]
    if not fields:
        return None
    values = [payload[field] for field in fields]
    if len(values) == 2 and values[0] != values[1]:
        raise ValueError("conflicting capture timestamps")
    return values[0]


def _parse_captured_at(value: Any, *, strict: bool = False) -> datetime:
    received_at = datetime.now(tz=LOCAL_TZ)
    if value is None or (isinstance(value, str) and not value.strip()):
        if strict:
            raise ValueError("capture timestamp is required")
        return received_at
    if not isinstance(value, str):
        raise ValueError("capture timestamp must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (OSError, OverflowError, ValueError) as exc:
        if strict:
            raise ValueError("invalid capture timestamp") from exc
        return received_at
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if strict:
            raise ValueError("capture timestamp must include timezone")
        try:
            parsed = parsed.replace(tzinfo=LOCAL_TZ)
        except (OSError, OverflowError, ValueError):
            return received_at
    else:
        try:
            parsed = parsed.astimezone(LOCAL_TZ)
        except (OSError, OverflowError, ValueError) as exc:
            if strict:
                raise ValueError("invalid capture timestamp") from exc
            return received_at
    if parsed > received_at + timedelta(seconds=MAX_CAPTURE_FUTURE_SECONDS):
        if strict:
            raise ValueError("capture timestamp is too far in the future")
        return received_at
    return parsed


def _redact_url(url: Any) -> str:
    if not isinstance(url, str) or not url:
        return ""
    try:
        parts = urlsplit(url)
        port = parts.port
    except (TypeError, ValueError):
        return ""
    hostname = parts.hostname
    if not hostname:
        return ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = hostname if port is None else f"{hostname}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _render_extension_background(account_ref: str, endpoint: str, token: str) -> str:
    account_json = json.dumps(account_ref)
    endpoint_json = json.dumps(endpoint)
    token_json = json.dumps(_validate_bridge_token(token))
    return f"""const ACCOUNT = {account_json};
const ENDPOINT = {endpoint_json};
const TOKEN = {token_json};
const CODEX_USAGE_ACK_MAX_CHARS = 4096;
const CODEX_USAGE_ACK_CHUNK_BYTES = 65536;

async function readCodexUsageAckText(response) {{
  const reader = response && response.body && typeof response.body.getReader === "function"
    ? response.body.getReader()
    : null;
  if (!reader) {{
    const text = await response.text();
    return String(text || "").slice(0, CODEX_USAGE_ACK_MAX_CHARS);
  }}
  const decoder = new TextDecoder();
  const parts = [];
  let length = 0;
  let shouldCancel = false;
  while (true) {{
    const item = await reader.read();
    if (item.done) {{
      const tail = decoder.decode();
      const remaining = CODEX_USAGE_ACK_MAX_CHARS - length;
      parts.push(tail.slice(0, remaining));
      break;
    }}
    const bytes = item.value || new Uint8Array();
    for (let offset = 0; offset < bytes.length; offset += CODEX_USAGE_ACK_CHUNK_BYTES) {{
      if (length >= CODEX_USAGE_ACK_MAX_CHARS) {{
        shouldCancel = true;
        break;
      }}
      const chunk = decoder.decode(
        bytes.subarray(offset, offset + CODEX_USAGE_ACK_CHUNK_BYTES),
        {{ stream: true }},
      );
      const remaining = CODEX_USAGE_ACK_MAX_CHARS - length;
      if (chunk.length > remaining) {{
        parts.push(chunk.slice(0, remaining));
        shouldCancel = true;
        break;
      }}
      parts.push(chunk);
      length += chunk.length;
    }}
    if (shouldCancel) {{
      try {{
        await reader.cancel();
      }} catch (_error) {{
        // The response is already bounded; cancellation is best effort.
      }}
      break;
    }}
  }}
  return parts.join("");
}}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {{
  if (!message || message.type !== "codexUsageIngest") {{
    return false;
  }}
  fetch(ENDPOINT, {{
    method: "POST",
    headers: {{
      "Content-Type": "application/json",
      "X-Codex-Usage-Account": ACCOUNT,
      "Authorization": "Bearer " + TOKEN
    }},
    body: JSON.stringify(message.payload)
  }})
    .then(async (response) => {{
      sendResponse({{
        ok: response.ok,
        status: response.status,
        text: await readCodexUsageAckText(response)
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
const CODEX_USAGE_CAPTURED_API_MAX_CHARS = 4000000;
const CODEX_USAGE_RESPONSE_CHUNK_BYTES = 65536;
const CODEX_USAGE_MAX_SERIALIZED_PAYLOAD_BYTES = 9500000;
const codexUsageTextEncoder = typeof TextEncoder === "function"
  ? new TextEncoder() : null;
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

function codexUsageUtf8ByteLength(value) {{
  const text = String(value || "");
  if (codexUsageTextEncoder) {{
    return codexUsageTextEncoder.encode(text).length;
  }}
  // Conservative fallback for older extension runtimes: JSON escaping can
  // expand a UTF-16 code unit to at most six ASCII bytes.
  return text.length * 6;
}}

function codexUsageSerializedPayloadBytes(payload) {{
  return codexUsageUtf8ByteLength(JSON.stringify(payload));
}}

function codexUsagePayloadTextCandidates(payload) {{
  const metadataFields = new Set([
    "account", "url", "title", "capturedAt", "readyState"
  ]);
  const candidates = [];
  for (const field of Object.keys(payload)) {{
    if (field !== "apiResponses"
      && typeof payload[field] === "string"
      && !metadataFields.has(field)
      && payload[field].length > 1000) {{
      candidates.push({{ owner: payload, field, value: payload[field] }});
    }}
  }}
  for (const response of Array.isArray(payload.apiResponses)
    ? payload.apiResponses : []) {{
    if (!response || typeof response !== "object") {{
      continue;
    }}
    for (const field of Object.keys(response)) {{
      if (
        typeof response[field] === "string"
        && !metadataFields.has(field)
        && response[field].length > 1000
      ) {{
        candidates.push({{ owner: response, field, value: response[field] }});
      }}
    }}
  }}
  return candidates;
}}

function fitCodexUsagePayload(payload) {{
  const metadataFields = new Set([
    "account", "url", "title", "capturedAt", "readyState"
  ]);
  let serializedBytes = codexUsageSerializedPayloadBytes(payload);
  if (serializedBytes < CODEX_USAGE_MAX_SERIALIZED_PAYLOAD_BYTES) {{
    return payload;
  }}
  if (Array.isArray(payload.apiResponses)) {{
    payload.apiResponses = payload.apiResponses.map((item) => (
      item && typeof item === "object" ? {{ ...item }} : item
    ));
  }}
  for (let attempt = 0; attempt < 8; attempt += 1) {{
    if (serializedBytes < CODEX_USAGE_MAX_SERIALIZED_PAYLOAD_BYTES) {{
      return payload;
    }}
    const candidates = codexUsagePayloadTextCandidates(payload);
    if (!candidates.length) {{
      break;
    }}
    const trimRatio = Math.min(
      0.5,
      (CODEX_USAGE_MAX_SERIALIZED_PAYLOAD_BYTES / serializedBytes) * 0.9,
    );
    for (const candidate of candidates) {{
      const nextLength = Math.floor(
        candidate.value.length * Math.max(0, trimRatio),
      );
      candidate.owner[candidate.field] = candidate.value.slice(
        0,
        Math.max(0, nextLength),
      );
    }}
    serializedBytes = codexUsageSerializedPayloadBytes(payload);
  }}
  while (serializedBytes >= CODEX_USAGE_MAX_SERIALIZED_PAYLOAD_BYTES
    && Array.isArray(payload.apiResponses)
    && payload.apiResponses.length) {{
    const removable = payload.apiResponses.findIndex(
      (item) => !codexUsageIsMainUsageResponse(item),
    );
    payload.apiResponses.splice(removable >= 0 ? removable : 0, 1);
    serializedBytes = codexUsageSerializedPayloadBytes(payload);
  }}
  if (serializedBytes >= CODEX_USAGE_MAX_SERIALIZED_PAYLOAD_BYTES) {{
    for (const field of Object.keys(payload)) {{
      if (
        field !== "apiResponses"
        && typeof payload[field] === "string"
        && !metadataFields.has(field)
      ) {{
        payload[field] = "";
      }}
    }}
    payload.apiResponses = [];
  }}
  return payload;
}}

async function readBoundedCodexUsageResponse(response) {{
  const reader = response && response.body && typeof response.body.getReader === "function"
    ? response.body.getReader()
    : null;
  if (!reader) {{
    const text = await response.text();
    return {{
      text: limitCodexUsageText(text),
      truncated: isCodexUsageTruncated(text)
    }};
  }}
  const decoder = new TextDecoder();
  const parts = [];
  let length = 0;
  let truncated = false;
  let shouldCancel = false;
  while (true) {{
    const item = await reader.read();
    if (item.done) {{
      const tail = decoder.decode();
      const remaining = CODEX_USAGE_MAX_FIELD_CHARS - length;
      if (tail.length > remaining) {{
        parts.push(tail.slice(0, remaining));
        truncated = true;
      }} else {{
        parts.push(tail);
        length += tail.length;
      }}
      break;
    }}
    const bytes = item.value || new Uint8Array();
    for (
      let offset = 0;
      offset < bytes.length;
      offset += CODEX_USAGE_RESPONSE_CHUNK_BYTES
    ) {{
      if (length >= CODEX_USAGE_MAX_FIELD_CHARS) {{
        truncated = true;
        shouldCancel = true;
        break;
      }}
      const chunk = decoder.decode(
        bytes.subarray(offset, offset + CODEX_USAGE_RESPONSE_CHUNK_BYTES),
        {{ stream: true }},
      );
      const remaining = CODEX_USAGE_MAX_FIELD_CHARS - length;
      if (chunk.length > remaining) {{
        parts.push(chunk.slice(0, remaining));
        truncated = true;
        shouldCancel = true;
        break;
      }}
      parts.push(chunk);
      length += chunk.length;
    }}
    if (shouldCancel) {{
      try {{
        await reader.cancel();
      }} catch (_error) {{
        // The response is already bounded; cancellation is best effort.
      }}
      break;
    }}
  }}
  return {{ text: parts.join(""), truncated }};
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

function compactCodexUsageApiResponse(item) {{
  if (!item || item.truncated !== true) {{
    return item;
  }}
  const bodyText = String(item.bodyText || item.body || item.text || "");
  return {{
    ...item,
    bodyText: "",
    body: "",
    text: "",
    bodyExcerpt: bodyText.slice(0, 500)
  }};
}}

function codexUsageApiResponseTextSize(item) {{
  return ["bodyText", "body", "text", "bodyExcerpt"].reduce(
    (total, name) => total + String((item && item[name]) || "").length,
    0,
  );
}}

function trimCodexUsageApiResponses(items) {{
  const main = [];
  const others = [];
  for (let index = items.length - 1; index >= 0; index -= 1) {{
    const item = items[index];
    if (!item || typeof item !== "object") {{
      continue;
    }}
    if (!main.length && codexUsageIsMainUsageResponse(item)) {{
      main.push(item);
    }} else {{
      others.push(item);
    }}
  }}
  const retained = [];
  let total = 0;
  for (const item of [...main, ...others]) {{
    const size = codexUsageApiResponseTextSize(item);
    if (total + size > CODEX_USAGE_CAPTURED_API_MAX_CHARS) {{
      continue;
    }}
    retained.push(item);
    total += size;
  }}
  return retained.reverse();
}}

function codexUsageIsMainUsageResponse(item) {{
  try {{
    if (!item || typeof item !== "object" || Array.isArray(item)) {{
      return false;
    }}
    const parsed = new URL(String((item && item.url) || ""), location.origin);
    const path = parsed.pathname.replace(/\\/+$/, "") || "/";
    if (parsed.origin !== location.origin || path !== "/backend-api/wham/usage") {{
      return false;
    }}
    if (
      !Number.isInteger(item.status)
      || item.status < 200
      || item.status >= 300
      || item.ok !== true
      || item.truncated !== false
    ) {{
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
  item = compactCodexUsageApiResponse(item);
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
  const bounded = trimCodexUsageApiResponses(codexUsageCapturedApiResponses);
  codexUsageCapturedApiResponses.splice(
    0,
    codexUsageCapturedApiResponses.length,
    ...bounded,
  );
}}

function dedupeCodexUsageApiResponses(items) {{
  const byKey = new Map();
  for (const rawItem of items) {{
    const item = compactCodexUsageApiResponse(rawItem);
    if (!item || typeof item !== "object" || !item.url) {{
      continue;
    }}
    const key = codexUsageApiResponseKey(item);
    const current = byKey.get(key);
    if (!current || codexUsageApiResponseIsNewer(item, current)) {{
      byKey.set(key, item);
    }}
  }}
  return trimCodexUsageApiResponses(
    Array.from(byKey.values()).slice(-CODEX_USAGE_CAPTURED_API_LIMIT),
  );
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
  const selector = attrs.reduce(
    (result, name) => result ? `${{result}},[${{name}}]` : `[${{name}}]`,
    "",
  );
  const parts = [];
  let length = 0;
  let truncated = false;
  const elements = document.querySelectorAll(selector);
  for (let index = 0; index < elements.length; index += 1) {{
    const element = elements[index];
    for (const name of attrs) {{
      const value = element.getAttribute(name);
      const text = String(value || "");
      if (!text.trim()) {{
        continue;
      }}
      const prefix = length ? "\\n" : "";
      const remaining = CODEX_USAGE_MAX_FIELD_CHARS - length;
      const chunk = (prefix + text).slice(0, remaining);
      parts.push(chunk);
      length += chunk.length;
      if (chunk.length < prefix.length + text.length) {{
        truncated = true;
        return {{ text: parts.join(""), truncated }};
      }}
    }}
  }}
  return {{ text: parts.join(""), truncated }};
}}

function collectCodexUsageSvgText() {{
  const parts = [];
  let length = 0;
  let truncated = false;
  const elements = document.querySelectorAll("svg text, svg title, svg desc");
  for (let index = 0; index < elements.length; index += 1) {{
    const element = elements[index];
    const text = String(element.textContent || "");
    if (!text.trim()) {{
      continue;
    }}
    const prefix = length ? "\\n" : "";
    const remaining = CODEX_USAGE_MAX_FIELD_CHARS - length;
    const chunk = (prefix + text).slice(0, remaining);
    parts.push(chunk);
    length += chunk.length;
    if (chunk.length < prefix.length + text.length) {{
      truncated = true;
      break;
    }}
  }}
  return {{ text: parts.join(""), truncated }};
}}

function boundedCodexUsageVisibleText(root) {{
  if (!root) {{
    return {{ text: "", truncated: false }};
  }}
  if (root.nodeType !== 1) {{
    const fallback = String(root.innerText || "");
    return {{
      text: fallback.slice(0, CODEX_USAGE_MAX_FIELD_CHARS),
      truncated: fallback.length > CODEX_USAGE_MAX_FIELD_CHARS
    }};
  }}
  const maxNodes = 500000;
  const skippedTags = new Set([
    "script", "style", "link", "meta", "noscript", "template"
  ]);
  const blockTags = new Set([
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
    "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
    "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol",
    "p", "pre", "section", "table", "td", "th", "tr", "ul"
  ]);
  const parts = [];
  let length = 0;
  let nodesSeen = 0;
  let truncated = false;

  function append(value) {{
    if (length >= CODEX_USAGE_MAX_FIELD_CHARS) {{
      truncated = true;
      return;
    }}
    const text = String(value || "");
    const remaining = CODEX_USAGE_MAX_FIELD_CHARS - length;
    const chunk = text.slice(0, remaining);
    parts.push(chunk);
    length += chunk.length;
    truncated = truncated || chunk.length < text.length;
  }}

  const stack = [{{ kind: "visit", node: root }}];
  while (
    stack.length
    && length < CODEX_USAGE_MAX_FIELD_CHARS
    && nodesSeen < maxNodes
  ) {{
    const item = stack.pop();
    if (item.kind === "close") {{
      append("\\n");
      continue;
    }}
    const node = item.node;
    if (!node) {{
      continue;
    }}
    nodesSeen += 1;
    if (node.nodeType === 3) {{
      append(node.nodeValue);
      continue;
    }}
    if (node.nodeType !== 1) {{
      continue;
    }}
    const tag = String(node.tagName || "").toLowerCase();
    if (!tag || skippedTags.has(tag) || node.hidden) {{
      continue;
    }}
    const style = typeof getComputedStyle === "function"
      ? getComputedStyle(node)
      : null;
    if (
      style
      && (style.display === "none" || style.visibility === "hidden")
    ) {{
      continue;
    }}
    if (blockTags.has(tag)) {{
      append("\\n");
      stack.push({{ kind: "close" }});
    }}
    const children = node.childNodes || [];
    for (let index = children.length - 1; index >= 0; index -= 1) {{
      stack.push({{ kind: "visit", node: children[index] }});
    }}
  }}
  if (stack.length) {{
    truncated = true;
  }}
  return {{ text: parts.join(""), truncated }};
}}

async function fetchCodexUsageApi(path) {{
  const url = new URL(path, location.origin);
  const response = await fetch(url.href, {{
    method: "GET",
    credentials: "include",
    headers: {{ "Accept": "application/json" }}
  }});
  const contentType = response.headers.get("content-type") || "";
  const captured = await readBoundedCodexUsageResponse(response);
  const bodyText = captured.text;
  const isJson = looksLikeCodexUsageJson(contentType, bodyText);
  return {{
    url: url.href,
    status: response.status,
    ok: response.ok,
    contentType,
    bodyText: isJson ? bodyText : "",
    bodyExcerpt: isJson ? "" : bodyText.slice(0, 500),
    truncated: isJson ? captured.truncated : false
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

function boundedCodexUsageDomCapture(root) {{
  if (!root) {{
    return {{ text: "", html: "", textTruncated: false, htmlTruncated: false }};
  }}
  const maxNodes = 500000;
  const skippedTags = new Set([
    "script", "style", "link", "meta", "noscript", "template"
  ]);
  const voidTags = new Set([
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr"
  ]);
  const attributesToKeep = new Set([
    "style", "class", "role", "hidden", "aria-hidden", "aria-valuenow",
    "aria-valuemin", "aria-valuemax", "aria-label", "title"
  ]);
  const textParts = [];
  const htmlParts = [];
  let textLength = 0;
  let htmlLength = 0;
  let nodesSeen = 0;
  let textTruncated = false;
  let htmlTruncated = false;

  function append(parts, value, limit, state) {{
    const text = String(value || "");
    if (state.length >= limit) {{
      state.truncated = true;
      return;
    }}
    const remaining = limit - state.length;
    parts.push(text.slice(0, remaining));
    state.length += Math.min(text.length, remaining);
    state.truncated = state.truncated || text.length > remaining;
  }}

  function escape(value) {{
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;");
  }}

  const stack = [{{ kind: "visit", node: root }}];
  while (
    stack.length
    && nodesSeen < maxNodes
    && (
      textLength < CODEX_USAGE_MAX_FIELD_CHARS
      || htmlLength < CODEX_USAGE_MAX_FIELD_CHARS
    )
  ) {{
    const item = stack.pop();
    if (item.kind === "close") {{
      const closingState = {{ length: htmlLength, truncated: htmlTruncated }};
      append(htmlParts, "</" + item.tag + ">", CODEX_USAGE_MAX_FIELD_CHARS, closingState);
      htmlLength = closingState.length;
      htmlTruncated = closingState.truncated;
      continue;
    }}
    const node = item.node;
    if (!node) {{
      continue;
    }}
    nodesSeen += 1;
    if (node.nodeType === 3) {{
      const state = {{ length: textLength, truncated: textTruncated }};
      append(textParts, node.nodeValue, CODEX_USAGE_MAX_FIELD_CHARS, state);
      textLength = state.length;
      textTruncated = state.truncated;
      const htmlState = {{ length: htmlLength, truncated: htmlTruncated }};
      append(
        htmlParts,
        escape(node.nodeValue),
        CODEX_USAGE_MAX_FIELD_CHARS,
        htmlState,
      );
      htmlLength = htmlState.length;
      htmlTruncated = htmlState.truncated;
      continue;
    }}
    if (node.nodeType !== 1) {{
      continue;
    }}
    const tag = String(node.tagName || "").toLowerCase();
    if (!tag || skippedTags.has(tag)) {{
      continue;
    }}
    const htmlState = {{ length: htmlLength, truncated: htmlTruncated }};
    append(htmlParts, "<" + tag, CODEX_USAGE_MAX_FIELD_CHARS, htmlState);
    const attributes = node.attributes || [];
    for (let index = 0; index < attributes.length; index += 1) {{
      const attribute = attributes[index];
      const name = String(attribute.name || "").toLowerCase();
      if (attributesToKeep.has(name)) {{
        append(
          htmlParts,
          " " + attribute.name + '=\\"' + escape(attribute.value) + '\\"',
          CODEX_USAGE_MAX_FIELD_CHARS,
          htmlState,
        );
      }}
    }}
    append(htmlParts, ">", CODEX_USAGE_MAX_FIELD_CHARS, htmlState);
    htmlLength = htmlState.length;
    htmlTruncated = htmlState.truncated;
    if (!voidTags.has(tag)) {{
      stack.push({{ kind: "close", tag }});
    }}
    const children = node.childNodes || [];
    for (let index = children.length - 1; index >= 0; index -= 1) {{
      stack.push({{ kind: "visit", node: children[index] }});
    }}
  }}
  if (stack.length) {{
    textTruncated = true;
    htmlTruncated = true;
  }}
  return {{
    text: textParts.join(""),
    html: htmlParts.join(""),
    textTruncated,
    htmlTruncated,
  }};
}}

function collectCodexUsage() {{
  const bodyCapture = boundedCodexUsageVisibleText(document.body);
  const bodyText = bodyCapture.text;
  const root = boundedCodexUsageDomCapture(document.documentElement);
  const domText = root.text;
  const accessibilityCapture = collectCodexUsageAttributeText();
  const accessibilityText = accessibilityCapture.text;
  const svgCapture = collectCodexUsageSvgText();
  const svgText = svgCapture.text;
  const htmlText = root.html;
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
      bodyText: bodyCapture.truncated,
      domText: root.textTruncated,
      accessibilityText: accessibilityCapture.truncated,
      svgText: svgCapture.truncated,
      htmlText: root.htmlTruncated
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
  fitCodexUsagePayload(payload);
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
  const CODEX_USAGE_CAPTURED_API_MAX_CHARS = 4000000;
  const CODEX_USAGE_RESPONSE_CHUNK_BYTES = 65536;
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

  async function readBoundedCodexUsageResponse(response) {
    const reader = response && response.body && typeof response.body.getReader === "function"
      ? response.body.getReader()
      : null;
    if (!reader) {
      const text = await response.text();
      return {
        text: limitCodexUsageText(text),
        truncated: isCodexUsageTruncated(text)
      };
    }
    const decoder = new TextDecoder();
    const parts = [];
    let length = 0;
    let truncated = false;
    let shouldCancel = false;
    while (true) {
      const item = await reader.read();
      if (item.done) {
        const tail = decoder.decode();
        const remaining = CODEX_USAGE_MAX_FIELD_CHARS - length;
        if (tail.length > remaining) {
          parts.push(tail.slice(0, remaining));
          truncated = true;
        } else {
          parts.push(tail);
          length += tail.length;
        }
        break;
      }
      const bytes = item.value || new Uint8Array();
      for (
        let offset = 0;
        offset < bytes.length;
        offset += CODEX_USAGE_RESPONSE_CHUNK_BYTES
      ) {
        if (length >= CODEX_USAGE_MAX_FIELD_CHARS) {
          truncated = true;
          shouldCancel = true;
          break;
        }
        const chunk = decoder.decode(
          bytes.subarray(offset, offset + CODEX_USAGE_RESPONSE_CHUNK_BYTES),
          { stream: true },
        );
        const remaining = CODEX_USAGE_MAX_FIELD_CHARS - length;
        if (chunk.length > remaining) {
          parts.push(chunk.slice(0, remaining));
          truncated = true;
          shouldCancel = true;
          break;
        }
        parts.push(chunk);
        length += chunk.length;
      }
      if (shouldCancel) {
        try {
          await reader.cancel();
        } catch (_error) {
          // The response is already bounded; cancellation is best effort.
        }
        break;
      }
    }
    return { text: parts.join(""), truncated };
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

  function compactCodexUsageApiResponse(item) {
    if (!item || item.truncated !== true) {
      return item;
    }
    const bodyText = String(item.bodyText || item.body || item.text || "");
    return {
      ...item,
      bodyText: "",
      body: "",
      text: "",
      bodyExcerpt: bodyText.slice(0, 500)
    };
  }

  function codexUsageApiResponseTextSize(item) {
    return ["bodyText", "body", "text", "bodyExcerpt"].reduce(
      (total, name) => total + String((item && item[name]) || "").length,
      0,
    );
  }

  function trimCodexUsageApiResponses(items) {
    const main = [];
    const others = [];
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const item = items[index];
      if (!item || typeof item !== "object") {
        continue;
      }
      if (!main.length && codexUsageIsMainUsageUrl(item.url)) {
        main.push(item);
      } else {
        others.push(item);
      }
    }
    const retained = [];
    let total = 0;
    for (const item of [...main, ...others]) {
      const size = codexUsageApiResponseTextSize(item);
      if (total + size > CODEX_USAGE_CAPTURED_API_MAX_CHARS) {
        continue;
      }
      retained.push(item);
      total += size;
    }
    return retained.reverse();
  }

  function rememberCodexUsageApiResponse(item, requestId = null) {
    item = compactCodexUsageApiResponse(item);
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
    const bounded = trimCodexUsageApiResponses(codexUsageCapturedApiResponses);
    codexUsageCapturedApiResponses.splice(
      0,
      codexUsageCapturedApiResponses.length,
      ...bounded,
    );
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
      const captured = await readBoundedCodexUsageResponse(clone);
      const bodyText = captured.text;
      const isJson = looksLikeCodexUsageJson(contentType, bodyText);
      rememberCodexUsageApiResponse({
        source: "page-fetch",
        url: new URL(url, location.origin).href,
        requestSequence,
        status: clone.status,
        ok: clone.ok,
        contentType,
        bodyText: isJson ? bodyText : "",
        bodyExcerpt: isJson ? "" : bodyText.slice(0, 500),
        truncated: isJson ? captured.truncated : false
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
