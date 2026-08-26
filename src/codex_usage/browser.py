from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import datetime
from heapq import nsmallest
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .config import AppConfig, _validate_account_id, _validate_config
from .direct import (
    DirectAuthError,
    _auth_plan_type_changed,
    auth_identity_changed,
    auth_identity_for_account,
    auth_metadata_from_payload,
    auth_plan_type_for_account,
    canonical_backend_identity,
    read_auth_json_file,
)
from .extractor import LOCAL_TZ, JsonCandidate, extract_windows
from .identity import (
    backend_identity_from_candidates,
    backend_identity_from_payload,
    backend_plan_type_from_candidates,
    select_identity_consistent_candidates,
)
from .json_utils import loads_strict
from .models import Account, AccountStatus, AccountUsage, LimitWindow, UsagePool
from .private_io import ensure_private_directory, private_path_lock
from .private_io import (
    write_private_text as write_private_output_text,
)

JSON_MAX_BYTES = 2_000_000
JSON_CANDIDATE_MAX_BYTES = 4_000_000
JSON_CANDIDATE_MAX_COUNT = 50
BROWSER_TIMEOUT_MAX_MS = 60 * 60 * 1000
PROBE_OUTPUT_MAX_BYTES = 2_000_000
BROWSER_TEXT_MAX_CHARS = 2_000_000
TITLE_MAX_CHARS = 500
DIAGNOSTIC_MAX_KEYS = 40
DIAGNOSTIC_MAX_FIELD_CHARS = 200
DIAGNOSTIC_MAX_RESPONSES = 100
LOGIN_HINTS = ("log in", "sign in", "anmelden", "einloggen", "continue with")
LOGIN_PAGE_HINTS = (
    "log in to start chatting",
    "log in to get answers",
    "continue with google",
    "continue with apple",
    "continue with phone",
    "sign up for free",
)
CLOUDFLARE_HINTS = (
    "cloudflare",
    "checking your browser",
    "turnstile",
    "cf-chl",
    "cf-challenge",
    "verify you are human",
    "ueberpruefen sie",
    "überprüfen sie",
)
TRUSTED_BROWSER_HOSTS = frozenset(("chatgpt.com", "openai.com"))


def _validate_browser_timeout_ms(timeout_ms: object) -> None:
    if (
        type(timeout_ms) is not int
        or not 1 <= timeout_ms <= BROWSER_TIMEOUT_MAX_MS
    ):
        raise ValueError("browser timeout is invalid")


def login_account(account: Account, config: AppConfig) -> None:
    if not isinstance(account, Account):
        raise ValueError("account is invalid")
    if not isinstance(config, AppConfig):
        raise ValueError("config is invalid")
    _validate_config(config)
    profile_dir = _prepare_profile(account)
    with _profile_lock(profile_dir):
        with sync_playwright() as playwright:
            context = None
            try:
                context = _launch_persistent_context(
                    playwright,
                    account,
                    profile_dir,
                    headless=False,
                )
                page = context.new_page()
                page.goto(
                    config.analytics_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                print(f"Browserprofil: {profile_dir}")
                print(f"Browser: {account.browser}")
                print(
                    "Melde dich im geoeffneten Browser an und oeffne ggf. "
                    "die Codex-Analytics-Seite."
                )
                input(
                    "Druecke Enter, wenn der Account eingeloggt ist und die "
                    "Seite sichtbar ist ... "
                )
            finally:
                _close_context(context)


def fetch_account_usage(
    account: Account,
    config: AppConfig,
    *,
    headed: bool = False,
    timeout_ms: int = 45_000,
) -> AccountUsage:
    if not isinstance(account, Account):
        raise ValueError("account is invalid")
    if not isinstance(config, AppConfig):
        raise ValueError("config is invalid")
    _validate_config(config)
    _validate_browser_timeout_ms(timeout_ms)
    captured_at = datetime.now(tz=LOCAL_TZ)
    candidates: list[JsonCandidate] = []
    candidate_bytes = [0]
    source_urls: set[str] = set()

    try:
        auth_user_id_before, auth_account_id_before = auth_identity_for_account(account)
        auth_plan_type_before = auth_plan_type_for_account(account)
        profile_dir = _prepare_profile(account)
        with _profile_lock(profile_dir):
            with sync_playwright() as playwright:
                context = None
                try:
                    context = _launch_persistent_context(
                        playwright,
                        account,
                        profile_dir,
                        headless=not headed and config.headless,
                    )
                    page = context.new_page()
                    page.on(
                        "response",
                        lambda response: _capture_json_response(
                            response, candidates, candidate_bytes
                        ),
                    )
                    main_response = page.goto(
                        config.analytics_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    try:
                        page.wait_for_load_state("networkidle", timeout=12_000)
                    except PlaywrightTimeoutError:
                        pass
                    body_text, text_sources = _safe_page_text_sources(page)
                    current_url = page.url
                    page_title = _safe_title(page)
                    main_status = getattr(main_response, "status", None)
                finally:
                    _close_context(context)

        raw_candidates = candidates
        for candidate in raw_candidates:
            redacted_url = _redact_url(candidate.url)
            if redacted_url:
                source_urls.add(redacted_url)
        page_state = _detect_page_state(
            current_url,
            page_title,
            body_text,
            main_status=main_status,
        )
        if page_state == "cloudflare":
            return AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=captured_at,
                status=AccountStatus.ERROR,
                error="browser page blocked by cloudflare",
                source_urls=tuple(sorted(source_urls)),
                cache_invalidated=True,
            )
        auth_user_id, auth_account_id = auth_identity_for_account(account)
        auth_plan_type = auth_plan_type_for_account(account)
        if auth_identity_changed(
            before_user_id=auth_user_id_before,
            before_account_id=auth_account_id_before,
            after_user_id=auth_user_id,
            after_account_id=auth_account_id,
        ) or (
            _auth_plan_type_changed(auth_plan_type_before, auth_plan_type)
        ):
            return AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=captured_at,
                status=AccountStatus.LOGIN_REQUIRED,
                error="auth.json identity changed during browser request",
                cache_invalidated=True,
            )
        if page_state == "login_required":
            return AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=captured_at,
                status=AccountStatus.LOGIN_REQUIRED,
                error="browser login required",
                source_urls=tuple(sorted(source_urls)),
                cache_invalidated=True,
            )
        try:
            candidates = select_identity_consistent_candidates(
                raw_candidates,
                auth_user_id=auth_user_id,
                auth_account_id=auth_account_id,
            )
        except ValueError as exc:
            return AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=captured_at,
                status=AccountStatus.ERROR,
                error=str(exc),
                cache_invalidated=True,
            )
        identity_candidates = candidates
        if not identity_candidates:
            # Preserve rejected identity metadata for a partial result, never
            # the limit values from an ambiguous user-only response.
            identity_candidates = [
                candidate
                for candidate in raw_candidates
                if backend_identity_from_payload(candidate.payload) != (None, None)
            ]
        structured_identity_present = any(
            backend_identity_from_payload(candidate.payload) != (None, None)
            for candidate in identity_candidates
        )
        json_windows = extract_windows(
            body_text="",
            json_candidates=candidates,
            text_sources=(),
            now=captured_at,
        )
        raw_json_windows = extract_windows(
            body_text="",
            json_candidates=raw_candidates,
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
                candidates,
                auth_user_id=auth_user_id,
                auth_account_id=auth_account_id,
            )
        )
        if allow_dom_fallback:
            five_hour, weekly = extract_windows(
                body_text=body_text,
                json_candidates=candidates,
                text_sources=text_sources,
                now=captured_at,
            )
        else:
            five_hour, weekly = json_windows
        try:
            backend_user_id, backend_account_id = backend_identity_from_candidates(
                identity_candidates
            )
            backend_plan_type = backend_plan_type_from_candidates(identity_candidates)
        except ValueError as exc:
            return AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=captured_at,
                status=AccountStatus.ERROR,
                error=str(exc),
                cache_invalidated=True,
            )
        try:
            backend_user_id, backend_account_id = canonical_backend_identity(
                backend_user_id,
                backend_account_id,
                auth_user_id=auth_user_id,
                auth_account_id=auth_account_id,
                auth_plan_type=auth_plan_type,
                backend_plan_type=backend_plan_type,
                require_backend_identity=True,
                require_backend_account_id=bool(auth_account_id and json_has_usage),
                # A browser session cannot prove the selected account when
                # WHAM echoes the shared user ID as account_id. Fail closed
                # instead of displaying another account's limits.
                reject_ambiguous_backend_identity=bool(auth_account_id and backend_account_id),
            )
        except DirectAuthError as exc:
            return AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=captured_at,
                status=AccountStatus.LOGIN_REQUIRED,
                error=str(exc),
                cache_invalidated=True,
            )
        except ValueError as exc:
            return AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=captured_at,
                status=AccountStatus.ERROR,
                error=str(exc),
                cache_invalidated=True,
            )
        status = _status_for_result(
            body_text=body_text,
            current_url=current_url,
            five_hour=five_hour,
            weekly=weekly,
            main_status=main_status,
        )
        error = None
        cache_invalidated = False
        if status == AccountStatus.LOGIN_REQUIRED:
            error = "browser login required"
            cache_invalidated = True
        elif _main_response_failed(main_status):
            error = f"browser analytics request failed: HTTP {main_status}"
            cache_invalidated = True
        elif not _has_usage_value(five_hour) and not _has_usage_value(weekly):
            error = "browser page has no usable usage limits"
            cache_invalidated = True
        if cache_invalidated:
            five_hour = None
            weekly = None
        browser_windows = tuple(
            window
            for window in (five_hour, weekly)
            if window is not None
        )
        main = (
            UsagePool(
                key="main",
                display_name="Codex",
                windows=browser_windows,
                availability_sources=("usage", "browser"),
            )
            if browser_windows
            else None
        )
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            five_hour=five_hour,
            weekly=weekly,
            main=main,
            status=status,
            error=error,
            source_urls=tuple(sorted(source_urls)),
            backend_user_id=backend_user_id,
            backend_account_id=backend_account_id,
            cache_invalidated=cache_invalidated,
        )
    except PlaywrightError as exc:
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            status=AccountStatus.ERROR,
            error=_clean_error(str(exc)),
            cache_invalidated=True,
        )
    except DirectAuthError as exc:
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            status=AccountStatus.LOGIN_REQUIRED,
            error=str(exc),
            cache_invalidated=True,
        )


def _structured_identity_matches_account(
    candidates: list[JsonCandidate],
    *,
    auth_user_id: str | None,
    auth_account_id: str | None,
) -> bool:
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


def probe_account(
    account: Account,
    config: AppConfig,
    *,
    headed: bool = True,
    save_dir: Path | None = None,
) -> dict[str, Any]:
    if not isinstance(account, Account):
        raise ValueError("account is invalid")
    if not isinstance(config, AppConfig):
        raise ValueError("config is invalid")
    _validate_config(config)
    captured_at = datetime.now(tz=LOCAL_TZ)
    candidates: list[JsonCandidate] = []
    candidate_bytes = [0]
    profile_dir = _prepare_profile(account)
    with _profile_lock(profile_dir):
        with sync_playwright() as playwright:
            context = None
            try:
                context = _launch_persistent_context(
                    playwright,
                    account,
                    profile_dir,
                    headless=not headed,
                )
                page = context.new_page()
                page.on(
                    "response",
                    lambda response: _capture_json_response(
                        response, candidates, candidate_bytes
                    ),
                )
                page.goto(
                    config.analytics_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                try:
                    page.wait_for_load_state("networkidle", timeout=12_000)
                except PlaywrightTimeoutError:
                    pass
                body_text, text_sources = _safe_page_text_sources(page)
            finally:
                _close_context(context)

    five_hour, weekly = extract_windows(
        body_text=body_text,
        json_candidates=candidates,
        text_sources=text_sources,
        now=captured_at,
    )
    saved: list[str] = []
    if save_dir is not None:
        saved = _save_probe_payloads(save_dir, account, candidates, body_text)

    return {
        "account": account.id,
        "browser": account.browser,
        "captured_at": captured_at.isoformat(),
        "json_candidates": [_summarize_candidate(candidate) for candidate in candidates],
        "five_hour": five_hour.source if five_hour else None,
        "weekly": weekly.source if weekly else None,
        "saved": saved,
    }


def diagnose_account(
    account: Account,
    config: AppConfig,
    *,
    headed: bool = False,
    screenshot_dir: Path | None = None,
    auth_json_path: Path | None = None,
    timeout_ms: int = 60_000,
) -> dict[str, Any]:
    if not isinstance(account, Account):
        raise ValueError("account is invalid")
    if not isinstance(config, AppConfig):
        raise ValueError("config is invalid")
    _validate_config(config)
    if auth_json_path is not None and not isinstance(auth_json_path, Path):
        raise ValueError("auth.json path is invalid")
    _validate_browser_timeout_ms(timeout_ms)
    captured_at = datetime.now(tz=LOCAL_TZ)
    profile_dir = _prepare_profile(account)
    diagnostic_auth_path = auth_json_path
    if diagnostic_auth_path is None and account.auth_json_path:
        diagnostic_auth_path = Path(account.auth_json_path)
    responses: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "account": account.id,
        "label": account.label,
        "browser": account.browser,
        "profile_dir": str(profile_dir),
        "captured_at": captured_at.isoformat(),
        "analytics_url": config.analytics_url,
        "headed": headed,
        "codex_auth": _diagnose_auth_json(diagnostic_auth_path),
    }

    try:
        with _profile_lock(profile_dir):
            with sync_playwright() as playwright:
                context = None
                try:
                    context = _launch_persistent_context(
                        playwright,
                        account,
                        profile_dir,
                        headless=not headed,
                    )
                    page = context.new_page()
                    page.on(
                        "response",
                        lambda response: _capture_diagnostic_response(response, responses),
                    )
                    main_response = page.goto(
                        config.analytics_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    try:
                        page.wait_for_load_state("networkidle", timeout=12_000)
                    except PlaywrightTimeoutError:
                        pass
                    body_text = _safe_body_text(page)
                    title = _safe_title(page)
                    screenshot_path = _save_diagnostic_screenshot(page, account, screenshot_dir)
                    result.update(
                        {
                            "final_url": _redact_url(page.url),
                            "title": title,
                            "main_status": main_response.status if main_response else None,
                            "detected": _detect_page_state(
                                page.url,
                                title,
                                body_text,
                                responses,
                                main_status=main_response.status if main_response else None,
                            ),
                            "body_excerpt": _safe_excerpt(body_text),
                            "responses": responses[-20:],
                            "screenshot": screenshot_path,
                        }
                    )
                finally:
                    _close_context(context)
    except PlaywrightError as exc:
        result.update({"detected": "browser_error", "error": _clean_error(str(exc))})
    return result


def _capture_json_response(
    response: Any,
    candidates: list[JsonCandidate],
    candidate_bytes: list[int] | None = None,
) -> None:
    url = response.url
    if not _looks_relevant_url(url):
        return
    content_type = response.headers.get("content-type", "")
    content_length = response.headers.get("content-length")
    if content_length is not None and content_length != "":
        if not isinstance(content_length, str) or not content_length.strip():
            return
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0 or parsed_content_length > JSON_MAX_BYTES:
                return
        except (TypeError, ValueError):
            return
    if "json" not in content_type.lower() and not re.search(r"/(api|backend|accounts?)/", url):
        return
    try:
        if hasattr(response, "finished"):
            response.finished()
        text = response.text()
    except Exception:
        return
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        return
    if len(encoded) > JSON_MAX_BYTES:
        return
    try:
        payload = loads_strict(text)
    except ValueError:
        return
    if len(candidates) >= JSON_CANDIDATE_MAX_COUNT:
        return
    try:
        payload_bytes = len(
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
    except (TypeError, ValueError, OverflowError):
        return
    if candidate_bytes is None:
        existing_bytes = 0
        for candidate in candidates:
            try:
                existing_bytes += len(
                    json.dumps(
                        candidate.payload,
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8")
                )
            except (TypeError, ValueError, OverflowError):
                return
    else:
        existing_bytes = candidate_bytes[0]
    if existing_bytes + payload_bytes > JSON_CANDIDATE_MAX_BYTES:
        return
    candidates.append(JsonCandidate(url=url, payload=payload))
    if candidate_bytes is not None:
        candidate_bytes[0] += payload_bytes


def _diagnose_auth_json(path: Path | None) -> dict[str, Any]:
    configured_home = os.environ.get("CODEX_HOME")
    if configured_home:
        try:
            candidate = Path(configured_home).expanduser()
        except RuntimeError:
            candidate = Path()
        codex_home = candidate if candidate.is_absolute() else Path.home() / ".codex"
    else:
        codex_home = Path.home() / ".codex"
    auth_path = path or codex_home / "auth.json"
    try:
        expanded = auth_path.expanduser()
    except RuntimeError:
        return {
            "path": str(auth_path),
            "exists": False,
            "readable": False,
            "error": "auth.json path is invalid",
        }
    exists = expanded.exists() or expanded.is_symlink()
    result: dict[str, Any] = {"path": str(expanded), "exists": exists}
    if not exists:
        return result
    try:
        raw, file_stat = read_auth_json_file(expanded)
        payload = loads_strict(raw)
    except DirectAuthError as exc:
        result.update({"readable": False, "error": str(exc)})
        return result
    except (OSError, ValueError) as exc:
        result.update({"readable": False, "error": type(exc).__name__})
        return result

    result.update(
        {
            "readable": True,
            "size_bytes": file_stat.st_size,
            "mode": oct(file_stat.st_mode & 0o777),
            "type": type(payload).__name__,
        }
    )
    if not isinstance(payload, dict):
        return result

    tokens = payload.get("tokens")
    auth_metadata = auth_metadata_from_payload(payload)
    result.update(
        {
            "top_level_keys": _diagnostic_keys(payload),
            "auth_mode": _diagnostic_value(payload.get("auth_mode")),
            "last_refresh": _diagnostic_value(payload.get("last_refresh")),
            "has_openai_api_key": bool(payload.get("OPENAI_API_KEY")),
            "token_fields": _diagnostic_keys(tokens) if isinstance(tokens, dict) else [],
            "has_browser_storage_state": any(
                key in payload for key in ("cookies", "origins", "localStorage", "sessionStorage")
            ),
            "auth_last_refresh": _format_datetime(auth_metadata.get("auth_last_refresh")),
            "auth_access_expires_at": _format_datetime(
                auth_metadata.get("auth_access_expires_at")
            ),
            "auth_id_expires_at": _format_datetime(auth_metadata.get("auth_id_expires_at")),
        }
    )
    if isinstance(tokens, dict):
        result["token_presence"] = {
            key: bool(tokens.get(key))
            for key in ("access_token", "id_token", "refresh_token", "account_id")
        }
    return result


def _diagnostic_keys(mapping: dict[Any, Any]) -> list[str]:
    return [
        _diagnostic_text(key, limit=120)
        for key in nsmallest(
            DIAGNOSTIC_MAX_KEYS,
            (str(key) for key in mapping.keys()),
        )
    ]


def _diagnostic_value(value: Any) -> str | bool | int | float | None:
    if value is None or isinstance(value, bool):
        return value
    if type(value) in (int, float):
        return value
    if isinstance(value, str):
        return _diagnostic_text(value, limit=DIAGNOSTIC_MAX_FIELD_CHARS)
    return type(value).__name__


def _diagnostic_text(value: Any, *, limit: int) -> str:
    text = _clean_error(str(value))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    try:
        return value.astimezone(LOCAL_TZ).isoformat()
    except (OverflowError, TypeError, ValueError):
        return None


def _capture_diagnostic_response(response: Any, responses: list[dict[str, Any]]) -> None:
    url = response.url
    if not _is_trusted_browser_url(url):
        return
    if len(responses) >= DIAGNOSTIC_MAX_RESPONSES:
        del responses[0]
    responses.append(
        {
            "status": response.status,
            "url": _redact_url(url),
            "content_type": response.headers.get("content-type", "").split(";")[0],
        }
    )


def _looks_relevant_url(url: str) -> bool:
    if not _is_trusted_browser_url(url):
        return False
    lower = url.lower()
    return any(
        hint in lower
        for hint in (
            "codex",
            "analytics",
            "usage",
            "limit",
            "quota",
            "conversation_limit",
            "rate_limit",
        )
    )


def _is_trusted_browser_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        hostname = (parts.hostname or "").lower()
        port = parts.port
    except (TypeError, ValueError):
        return False
    if not hostname or not any(
        hostname == trusted_host or hostname.endswith(f".{trusted_host}")
        for trusted_host in TRUSTED_BROWSER_HOSTS
    ):
        return False
    return (
        parts.scheme.lower() == "https"
        and parts.username is None
        and parts.password is None
        and port in (None, 443)
    )


def _safe_body_text(page: Any) -> str:
    try:
        locator = page.locator("body")
        if not hasattr(locator, "evaluate"):
            return _limit_text(
                locator.inner_text(timeout=10_000),
                BROWSER_TEXT_MAX_CHARS,
            )
        body_text = locator.evaluate(
            """element => {
                const maxChars = 2000000;
                const maxNodes = 500000;
                const skippedTags = new Set([
                    "script", "style", "link", "meta", "noscript", "template"
                ]);
                const blockTags = new Set([
                    "address", "article", "aside", "blockquote", "br", "dd",
                    "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
                    "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
                    "li", "main", "nav", "ol", "p", "pre", "section", "table",
                    "td", "th", "tr", "ul"
                ]);
                const parts = [];
                let length = 0;
                let nodesSeen = 0;

                function append(value) {
                    if (length >= maxChars) {
                        return;
                    }
                    const text = String(value || "");
                    const remaining = maxChars - length;
                    const chunk = text.slice(0, remaining);
                    parts.push(chunk);
                    length += chunk.length;
                }

                const stack = [{ kind: "visit", node: element }];
                while (stack.length && length < maxChars && nodesSeen < maxNodes) {
                    const item = stack.pop();
                    if (item.kind === "close") {
                        append("\\n");
                        continue;
                    }
                    const node = item.node;
                    if (!node) {
                        continue;
                    }
                    nodesSeen += 1;
                    if (node.nodeType === 3) {
                        append(node.nodeValue);
                        continue;
                    }
                    if (node.nodeType !== 1) {
                        continue;
                    }
                    const tag = String(node.tagName || "").toLowerCase();
                    if (!tag || skippedTags.has(tag) || node.hidden) {
                        continue;
                    }
                    const style = typeof getComputedStyle === "function"
                        ? getComputedStyle(node)
                        : null;
                    if (
                        style
                        && (style.display === "none" || style.visibility === "hidden")
                    ) {
                        continue;
                    }
                    const isBlock = blockTags.has(tag);
                    if (isBlock) {
                        append("\\n");
                        stack.push({ kind: "close" });
                    }
                    const children = node.childNodes || [];
                    for (let index = children.length - 1; index >= 0; index -= 1) {
                        stack.push({ kind: "visit", node: children[index] });
                    }
                }
                return parts.join("");
            }"""
        )
        return _limit_text(body_text, BROWSER_TEXT_MAX_CHARS)
    except (AttributeError, PlaywrightError, TypeError):
        return ""


def _safe_page_text_sources(page: Any) -> tuple[str, tuple[tuple[str, str], ...]]:
    combined = _safe_combined_page_text_sources(page)
    if combined is None:
        body_text = _safe_body_text(page)
        html_text = _safe_html_text(page)
    else:
        body_text, html_text = combined
    sources = tuple(
        (source, text)
        for source, text in (("bodyText", body_text), ("htmlText", html_text))
        if text.strip()
    )
    return body_text, sources


def _safe_combined_page_text_sources(
    page: Any,
) -> tuple[str, str] | None:
    try:
        result = page.locator("html").evaluate(
            """element => {
                const maxChars = 2000000;
                const maxNodes = 1000000;
                const skippedTags = new Set([
                    "script", "style", "link", "meta", "noscript", "template"
                ]);
                const blockTags = new Set([
                    "address", "article", "aside", "blockquote", "br", "dd",
                    "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
                    "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
                    "li", "main", "nav", "ol", "p", "pre", "section", "table",
                    "td", "th", "tr", "ul"
                ]);
                const voidTags = new Set([
                    "area", "base", "br", "col", "embed", "hr", "img",
                    "input", "link", "meta", "param", "source", "track", "wbr"
                ]);
                const attributesToKeep = new Set([
                    "style", "class", "role", "hidden", "aria-hidden",
                    "aria-valuenow", "aria-valuemin", "aria-valuemax", "aria-label", "title"
                ]);
                const bodyParts = [];
                const htmlParts = [];
                let bodyLength = 0;
                let htmlLength = 0;
                let nodesSeen = 0;

                function appendBody(value) {
                    if (bodyLength >= maxChars) {
                        return;
                    }
                    const text = String(value || "");
                    const remaining = maxChars - bodyLength;
                    const chunk = text.slice(0, remaining);
                    bodyParts.push(chunk);
                    bodyLength += chunk.length;
                }

                function appendHtml(value) {
                    if (htmlLength >= maxChars) {
                        return;
                    }
                    const text = String(value || "");
                    const remaining = maxChars - htmlLength;
                    const chunk = text.slice(0, remaining);
                    htmlParts.push(chunk);
                    htmlLength += chunk.length;
                }

                function escape(value) {
                    return String(value || "")
                        .replaceAll("&", "&amp;")
                        .replaceAll("<", "&lt;")
                        .replaceAll(">", "&gt;")
                        .replaceAll('"', "&quot;");
                }

                function visible(node) {
                    if (node.hidden) {
                        return false;
                    }
                    const style = typeof getComputedStyle === "function"
                        ? getComputedStyle(node)
                        : null;
                    return !style
                        || (style.display !== "none" && style.visibility !== "hidden");
                }

                const stack = [{
                    kind: "visit",
                    node: element,
                    inBody: false,
                    bodyVisible: false
                }];
                while (
                    stack.length
                    && nodesSeen < maxNodes
                    && (bodyLength < maxChars || htmlLength < maxChars)
                ) {
                    const item = stack.pop();
                    if (item.kind === "close") {
                        if (item.bodyBlock) {
                            appendBody("\\n");
                        }
                        if (item.htmlTag) {
                            appendHtml("</" + item.htmlTag + ">");
                        }
                        continue;
                    }
                    const node = item.node;
                    if (!node) {
                        continue;
                    }
                    nodesSeen += 1;
                    if (node.nodeType === 3) {
                        if (item.inBody && item.bodyVisible) {
                            appendBody(node.nodeValue);
                        }
                        appendHtml(escape(node.nodeValue));
                        continue;
                    }
                    if (node.nodeType !== 1) {
                        continue;
                    }
                    const tag = String(node.tagName || "").toLowerCase();
                    if (!tag || skippedTags.has(tag)) {
                        continue;
                    }

                    const isBody = tag === "body";
                    const inBody = item.inBody || isBody;
                    const bodyVisible = isBody
                        ? visible(node)
                        : item.bodyVisible && visible(node);
                    const bodyBlock = inBody && bodyVisible && blockTags.has(tag);
                    if (bodyBlock) {
                        appendBody("\\n");
                    }

                    appendHtml("<" + tag);
                    const attributes = node.attributes || [];
                    for (let index = 0; index < attributes.length; index += 1) {
                        const attribute = attributes[index];
                        const name = String(attribute.name || "").toLowerCase();
                        if (attributesToKeep.has(name)) {
                            appendHtml(
                                " "
                                + attribute.name
                                + '=\\"'
                                + escape(attribute.value)
                                + '\\"'
                            );
                        }
                    }
                    appendHtml(">");

                    const htmlTag = voidTags.has(tag) ? null : tag;
                    if (htmlTag || bodyBlock) {
                        stack.push({ kind: "close", bodyBlock, htmlTag });
                    }
                    const children = node.childNodes || [];
                    for (let index = children.length - 1; index >= 0; index -= 1) {
                        stack.push({
                            kind: "visit",
                            node: children[index],
                            inBody,
                            bodyVisible
                        });
                    }
                }
                return {
                    bodyText: bodyParts.join(""),
                    htmlText: htmlParts.join("")
                };
            }"""
        )
    except (AttributeError, PlaywrightError, TypeError):
        return None
    if not isinstance(result, dict):
        return None
    body_text = result.get("bodyText")
    html_text = result.get("htmlText")
    if not isinstance(body_text, str) or not isinstance(html_text, str):
        return None
    return (
        _limit_text(body_text, BROWSER_TEXT_MAX_CHARS),
        _limit_text(html_text, BROWSER_TEXT_MAX_CHARS),
    )


def _safe_html_text(page: Any) -> str:
    try:
        html_text = page.locator("html").evaluate(
            """element => {
                const maxChars = 2000000;
                const maxNodes = 500000;
                const skippedTags = new Set([
                    "script", "style", "link", "meta", "noscript", "template"
                ]);
                const voidTags = new Set([
                    "area", "base", "br", "col", "embed", "hr", "img",
                    "input", "link", "meta", "param", "source", "track", "wbr"
                ]);
                const attributesToKeep = new Set([
                    "style", "class", "role", "hidden", "aria-hidden",
                    "aria-valuenow", "aria-valuemin", "aria-valuemax", "aria-label", "title"
                ]);
                const parts = [];
                let length = 0;
                let nodesSeen = 0;

                function append(value) {
                    if (length >= maxChars) {
                        return;
                    }
                    const text = String(value || "");
                    const remaining = maxChars - length;
                    parts.push(text.slice(0, remaining));
                    length += Math.min(text.length, remaining);
                }

                function escape(value) {
                    return String(value || "")
                        .replaceAll("&", "&amp;")
                        .replaceAll("<", "&lt;")
                        .replaceAll(">", "&gt;")
                        .replaceAll('"', "&quot;");
                }

                const stack = [{ kind: "visit", node: element }];
                while (stack.length && length < maxChars && nodesSeen < maxNodes) {
                    const item = stack.pop();
                    if (item.kind === "close") {
                        append("</" + item.tag + ">");
                        continue;
                    }
                    const node = item.node;
                    if (!node) {
                        continue;
                    }
                    nodesSeen += 1;
                    if (node.nodeType === 3) {
                        append(escape(node.nodeValue));
                        continue;
                    }
                    if (node.nodeType !== 1) {
                        continue;
                    }
                    const tag = String(node.tagName || "").toLowerCase();
                    if (!tag || skippedTags.has(tag)) {
                        continue;
                    }
                    append("<" + tag);
                    const attributes = node.attributes || [];
                    for (let index = 0; index < attributes.length; index += 1) {
                        const attribute = attributes[index];
                        const name = String(attribute.name || "").toLowerCase();
                        if (attributesToKeep.has(name)) {
                            append(" " + attribute.name + '=\\"' + escape(attribute.value) + '\\"');
                        }
                    }
                    append(">");
                    if (!voidTags.has(tag)) {
                        stack.push({ kind: "close", tag });
                    }
                    const children = node.childNodes || [];
                    for (let index = children.length - 1; index >= 0; index -= 1) {
                        stack.push({ kind: "visit", node: children[index] });
                    }
                }
                return parts.join("");
            }"""
        )
    except (AttributeError, PlaywrightError, TypeError):
        return ""
    if not isinstance(html_text, str):
        return ""
    return _limit_text(html_text, BROWSER_TEXT_MAX_CHARS)


def _safe_title(page: Any) -> str:
    try:
        return _limit_text(page.title(), TITLE_MAX_CHARS)
    except (AttributeError, PlaywrightError, TypeError):
        return ""


def _limit_text(value: str, max_chars: int) -> str:
    return value if len(value) <= max_chars else value[:max_chars]


def _safe_excerpt(body_text: str) -> str:
    clean = re.sub(r"\s+", " ", body_text).strip()
    if not clean:
        return ""
    return clean[:500]


def _detect_page_state(
    url: str,
    title: str,
    body_text: str,
    responses: list[dict[str, Any]] | None = None,
    *,
    main_status: int | None = None,
) -> str:
    safe_url = url if isinstance(url, str) else ""
    safe_title = title if isinstance(title, str) else ""
    safe_body_text = body_text if isinstance(body_text, str) else ""
    haystack = f"{safe_url}\n{safe_title}\n{safe_body_text}".lower()
    response_items = responses if isinstance(responses, (list, tuple)) else ()
    response_urls = "\n".join(
        str(item.get("url", ""))
        for item in response_items
        if isinstance(item, dict)
    ).lower()
    if safe_title.strip().lower() == "just a moment...":
        return "cloudflare"
    if (
        main_status == 403
        and "chatgpt.com/codex/cloud/settings/analytics" in safe_url.lower()
    ):
        return "cloudflare"
    if "auth" in safe_url.lower() or any(hint in haystack for hint in LOGIN_HINTS):
        return "login_required"
    if any(hint in haystack for hint in CLOUDFLARE_HINTS):
        return "cloudflare"
    if "/cdn-cgi/challenge-platform/" in response_urls:
        return "cloudflare"
    if "5 stunden nutzungsgrenze" in haystack or "weekly usage limit" in haystack:
        return "analytics_page"
    if "codex" in haystack and "analytics" in haystack:
        return "possible_analytics_page"
    return "unknown"


def _save_diagnostic_screenshot(
    page: Any,
    account: Account,
    screenshot_dir: Path | None,
) -> str | None:
    if screenshot_dir is None:
        return None
    _validate_account_id(account.id)
    _prepare_private_output_dir(screenshot_dir, label="diagnose screenshot directory")
    path = screenshot_dir / f"{account.id}-diagnose.png"
    _validate_private_output_path(path, label="diagnose screenshot path")
    page.screenshot(path=str(path), full_page=True)
    _validate_private_output_path(path, label="diagnose screenshot path")
    _chmod_private(path, mode=0o600)
    return str(path)


def _status_for_result(
    *,
    body_text: str,
    current_url: str,
    five_hour: LimitWindow | None,
    weekly: LimitWindow | None,
    main_status: int | None = None,
) -> AccountStatus:
    if _main_response_failed(main_status):
        return (
            AccountStatus.LOGIN_REQUIRED
            if main_status in {401, 407}
            else AccountStatus.ERROR
        )
    safe_body_text = body_text if isinstance(body_text, str) else ""
    safe_current_url = current_url if isinstance(current_url, str) else ""
    lower = safe_body_text.lower()
    if "auth" in safe_current_url.lower() or _looks_like_login_page(lower) or (
        not _has_usage_value(five_hour)
        and not _has_usage_value(weekly)
        and any(hint in lower for hint in LOGIN_HINTS)
    ):
        return AccountStatus.LOGIN_REQUIRED
    if not _has_usage_value(five_hour) or not _has_usage_value(weekly):
        return AccountStatus.PARTIAL
    return AccountStatus.OK


def _main_response_failed(status: int | None) -> bool:
    return (
        status is None
        or type(status) is not int
        or status < 200
        or status >= 300
    )


def _has_usage_value(window: LimitWindow | None) -> bool:
    return isinstance(window, LimitWindow) and window.has_usage_value


def _looks_like_login_page(lower_body_text: str) -> bool:
    return any(hint in lower_body_text for hint in LOGIN_PAGE_HINTS)


def _launch_persistent_context(
    playwright: Any,
    account: Account,
    profile_dir: Path,
    *,
    headless: bool,
):
    browser = account.browser
    kwargs: dict[str, Any] = {"user_data_dir": str(profile_dir), "headless": headless}
    if browser == "firefox":
        return playwright.firefox.launch_persistent_context(**kwargs)
    if browser == "chromium":
        return playwright.chromium.launch_persistent_context(**kwargs)
    raise RuntimeError(f"unsupported browser: {browser}")


def _close_context(context: Any) -> None:
    if context is None:
        return
    try:
        context.close()
    except Exception:
        pass


def _prepare_profile(account: Account) -> Path:
    if not isinstance(account.profile_dir, str) or not account.profile_dir:
        raise ValueError("profile directory is invalid")
    if not isinstance(account.browser, str) or account.browser not in {
        "firefox",
        "chromium",
    }:
        raise ValueError("browser is invalid")
    try:
        root = Path(account.profile_dir).expanduser()
    except RuntimeError as exc:
        raise ValueError("profile directory is invalid") from exc
    if not root.is_absolute():
        raise ValueError("profile directory must be absolute")
    _prepare_private_output_dir(root, label="profile directory")
    marker = root / ".codex-usage-profile"
    if marker.exists() or marker.is_symlink():
        _validate_private_output_path(marker, label="profile marker path")
    else:
        _write_private_text(
            marker,
            "codex-usage persistent browser profile root\n",
            label="profile marker path",
        )

    path = root / _profile_browser_dir(account.browser)
    _prepare_private_output_dir(path, label="browser profile directory")
    engine_marker = path / ".codex-usage-browser-profile"
    if engine_marker.exists() or engine_marker.is_symlink():
        _validate_private_output_path(engine_marker, label="browser profile marker path")
    else:
        _write_private_text(
            engine_marker,
            f"{account.browser}\n",
            label="browser profile marker path",
        )
    return path


def _chmod_private(path: Path, mode: int = 0o700) -> None:
    try:
        path.chmod(mode)
    except OSError as exc:
        raise ValueError(f"could not secure private path: {path}") from exc


def _profile_browser_dir(browser: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", browser)


@contextmanager
def _profile_lock(profile_dir: Path, *, lock_root: Path | None = None):
    root = lock_root or _profile_lock_root(profile_dir)
    relative = profile_dir.relative_to(root)
    lock_components = (root.name, *relative.parts)
    encoded_components = tuple(
        component.encode("utf-8").hex() for component in lock_components
    )
    lock_name = "." + ".".join(encoded_components) + ".codex-usage.lock"
    lock_paths = [root.parent / lock_name]
    legacy_lock = profile_dir / ".codex-usage.lock"
    if legacy_lock.is_symlink() or legacy_lock.exists():
        lock_paths.append(legacy_lock)
    with ExitStack() as locks:
        for lock_path in sorted(set(lock_paths), key=str):
            locks.enter_context(_profile_lock_file(lock_path, profile_dir))
        yield


def _profile_lock_root(profile_dir: Path) -> Path:
    if profile_dir.parent.name == "oauth":
        return profile_dir.parent.parent
    return profile_dir.parent


@contextmanager
def _profile_lock_file(lock_path: Path, profile_dir: Path):
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise ValueError(f"profile lock path must be a regular file: {lock_path}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ValueError(f"profile lock path must be a regular file: {lock_path}") from exc
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"profile lock path must be a regular file: {lock_path}")
        if file_stat.st_nlink != 1:
            raise ValueError(f"profile lock path must not be hard-linked: {lock_path}")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as handle:
            fd = -1
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(f"profile is already in use: {profile_dir}") from exc
            handle.seek(0)
            handle.truncate(0)
            handle.write(str(os.getpid()))
            handle.flush()
            try:
                yield
            finally:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    finally:
        if fd >= 0:
            os.close(fd)


def _summarize_candidate(candidate: JsonCandidate) -> dict[str, Any]:
    return {
        "url": _redact_url(candidate.url),
        "top_level_keys": _top_level_keys(candidate.payload),
    }


def _top_level_keys(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        return nsmallest(30, (str(key) for key in payload.keys()))
    if isinstance(payload, list):
        return [f"list[{len(payload)}]"]
    return [type(payload).__name__]


def _save_probe_payloads(
    save_dir: Path,
    account: Account,
    candidates: list[JsonCandidate],
    body_text: str,
) -> list[str]:
    _prepare_private_output_dir(save_dir, label="probe save directory")
    with private_path_lock(
        save_dir / ".codex-usage-probe-write",
        label="probe output lock",
    ):
        return _save_probe_payloads_transaction(
            save_dir,
            account,
            candidates,
            body_text,
        )


def _save_probe_payloads_transaction(
    save_dir: Path,
    account: Account,
    candidates: list[JsonCandidate],
    body_text: str,
) -> list[str]:
    _validate_account_id(account.id)
    files: dict[str, str] = {}
    for index, candidate in enumerate(candidates, start=1):
        payload_text = json.dumps(
            candidate.payload,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        files[f"{account.id}-{index:02d}.json"] = payload_text
    files[f"{account.id}-body.txt"] = body_text
    paths = {filename: save_dir / filename for filename in files}
    for path in paths.values():
        _validate_private_output_path(path, label="probe output path")

    with tempfile.TemporaryDirectory(
        prefix=f".{save_dir.name}.",
        dir=str(save_dir),
    ) as transaction:
        transaction_dir = Path(transaction)
        stage_dir = transaction_dir / "stage"
        backup_dir = transaction_dir / "backup"
        stage_dir.mkdir(mode=0o700)
        backup_dir.mkdir(mode=0o700)
        for filename, content in files.items():
            _write_bounded_private_text(
                stage_dir / filename,
                content,
                label="probe staging path",
            )

        backed_up: list[tuple[Path, Path]] = []
        committed: list[Path] = []
        try:
            for path in paths.values():
                _validate_private_output_path(path, label="probe output path")
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
                        raise ValueError(f"probe output path is not a regular file: {path}")
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
                    "probe output commit rollback failed",
                    [primary_error, *rollback_errors],
                ) from None
            raise
    return [str(path) for path in paths.values()]


def _prepare_private_output_dir(path: Path, *, label: str) -> None:
    try:
        ensure_private_directory(path, label=label)
    except OSError as exc:
        raise ValueError(f"could not secure private path: {path}") from exc


def _write_private_text(path: Path, text: str, *, label: str) -> None:
    write_private_output_text(path, text, label=label)


def _write_bounded_private_text(path: Path, text: str, *, label: str) -> None:
    if len(text.encode("utf-8")) > PROBE_OUTPUT_MAX_BYTES:
        raise ValueError(f"{label} too large; max {PROBE_OUTPUT_MAX_BYTES} bytes")
    _write_private_text(path, text, label=label)


def _validate_private_output_path(path: Path, *, label: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"{label} must be a regular file: {path}")
    if path.exists() and path.stat().st_nlink != 1:
        raise ValueError(f"{label} must not be hard-linked: {path}")


def _redact_url(url: str) -> str:
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
    path = parts.path
    if path.startswith("/cdn-cgi/challenge-platform/"):
        path = "/cdn-cgi/challenge-platform/..."
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def _clean_error(error: str) -> str:
    text = re.sub(r"\s+", " ", error).strip()
    text = re.sub(r"https?://[^\s\"'<>]+", lambda match: _redact_url(match.group(0)), text)
    text = re.sub(
        r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b",
        "[redacted.jwt]",
        text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted.api_key]", text)
    text = re.sub(r"(?<!\w)/(?:home|tmp|var|run|mnt)/[^\s\"'<>]+", "[redacted.path]", text)
    return text[:500]
