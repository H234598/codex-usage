from __future__ import annotations

import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .config import AppConfig
from .direct import (
    DirectAuthError,
    auth_identity_changed,
    auth_identity_for_account,
    auth_metadata_from_payload,
    auth_plan_type_for_account,
    canonical_backend_identity,
    inactive_five_hour_error,
    infer_inactive_five_hour_window,
    is_inferred_inactive_five_hour,
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
from .models import Account, AccountStatus, AccountUsage, LimitWindow
from .private_io import (
    assert_no_symlink_ancestors,
)
from .private_io import (
    write_private_text as write_private_output_text,
)

JSON_MAX_BYTES = 2_000_000
PROBE_OUTPUT_MAX_BYTES = 2_000_000
BROWSER_TEXT_MAX_CHARS = 2_000_000
TITLE_MAX_CHARS = 500
DIAGNOSTIC_MAX_KEYS = 40
DIAGNOSTIC_MAX_FIELD_CHARS = 200
LOGIN_HINTS = ("log in", "sign in", "anmelden", "einloggen", "continue with")
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


def login_account(account: Account, config: AppConfig) -> None:
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
    captured_at = datetime.now(tz=LOCAL_TZ)
    candidates: list[JsonCandidate] = []
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
                        lambda response: _capture_json_response(response, candidates),
                    )
                    page.goto(
                        config.analytics_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    try:
                        page.wait_for_load_state("networkidle", timeout=12_000)
                    except PlaywrightTimeoutError:
                        pass
                    body_text = _safe_body_text(page)
                    current_url = page.url
                finally:
                    _close_context(context)

        source_urls.update(_redact_url(candidate.url) for candidate in candidates)
        auth_user_id, auth_account_id = auth_identity_for_account(account)
        auth_plan_type = auth_plan_type_for_account(account)
        try:
            candidates = select_identity_consistent_candidates(
                candidates,
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
            )
        if auth_identity_changed(
            before_user_id=auth_user_id_before,
            before_account_id=auth_account_id_before,
            after_user_id=auth_user_id,
            after_account_id=auth_account_id,
        ) or (
            auth_plan_type_before
            and auth_plan_type
            and auth_plan_type_before.casefold() != auth_plan_type.casefold()
        ):
            return AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=captured_at,
                status=AccountStatus.LOGIN_REQUIRED,
                error="auth.json identity changed during browser request",
            )
        structured_identity_present = any(
            backend_identity_from_payload(candidate.payload) != (None, None)
            for candidate in candidates
        )
        json_windows = extract_windows(
            body_text="",
            json_candidates=candidates,
            text_sources=(),
            now=captured_at,
        )
        json_has_usage = any(
            window is not None and window.has_usage_value
            for window in json_windows
        )
        allow_dom_fallback = (
            not structured_identity_present
            or (
                not json_has_usage
                and _structured_identity_matches_account(
                    candidates,
                    auth_user_id=auth_user_id,
                    auth_account_id=auth_account_id,
                )
            )
        )
        if allow_dom_fallback:
            five_hour, weekly = extract_windows(
                body_text=body_text,
                json_candidates=candidates,
                now=captured_at,
            )
        else:
            five_hour, weekly = json_windows
        backend_user_id, backend_account_id = backend_identity_from_candidates(candidates)
        backend_plan_type = backend_plan_type_from_candidates(candidates)
        try:
            backend_user_id, backend_account_id = canonical_backend_identity(
                backend_user_id,
                backend_account_id,
                auth_user_id=auth_user_id,
                auth_account_id=auth_account_id,
                auth_plan_type=auth_plan_type,
                backend_plan_type=backend_plan_type,
                require_backend_identity=True,
            )
        except DirectAuthError as exc:
            return AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=captured_at,
                status=AccountStatus.LOGIN_REQUIRED,
                error=str(exc),
            )
        except ValueError as exc:
            return AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=captured_at,
                status=AccountStatus.ERROR,
                error=str(exc),
            )
        five_hour = infer_inactive_five_hour_window(
            five_hour,
            weekly,
            plan_type=backend_plan_type or auth_plan_type,
            source="browser",
        )
        inferred_inactive_five_hour = is_inferred_inactive_five_hour(five_hour)
        status = _status_for_result(
            body_text=body_text,
            current_url=current_url,
            five_hour=five_hour,
            weekly=weekly,
        )
        error = (
            inactive_five_hour_error("browser", backend_plan_type or auth_plan_type)
            if inferred_inactive_five_hour
            else None
        )
        if inferred_inactive_five_hour:
            status = AccountStatus.PARTIAL
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            five_hour=five_hour,
            weekly=weekly,
            status=status,
            error=error,
            source_urls=tuple(sorted(source_urls)),
            backend_user_id=backend_user_id,
            backend_account_id=backend_account_id,
        )
    except PlaywrightError as exc:
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            status=AccountStatus.ERROR,
            error=_clean_error(str(exc)),
        )
    except DirectAuthError as exc:
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            status=AccountStatus.LOGIN_REQUIRED,
            error=str(exc),
        )


def _structured_identity_matches_account(
    candidates: list[JsonCandidate],
    *,
    auth_user_id: str | None,
    auth_account_id: str | None,
) -> bool:
    if not auth_account_id:
        return False
    backend_user_id, backend_account_id = backend_identity_from_candidates(candidates)
    if backend_account_id != auth_account_id:
        return False
    return not auth_user_id or not backend_user_id or backend_user_id == auth_user_id


def probe_account(
    account: Account,
    config: AppConfig,
    *,
    headed: bool = True,
    save_dir: Path | None = None,
) -> dict[str, Any]:
    captured_at = datetime.now(tz=LOCAL_TZ)
    candidates: list[JsonCandidate] = []
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
                    lambda response: _capture_json_response(response, candidates),
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
                body_text = _safe_body_text(page)
            finally:
                _close_context(context)

    five_hour, weekly = extract_windows(
        body_text=body_text,
        json_candidates=candidates,
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
    captured_at = datetime.now(tz=LOCAL_TZ)
    profile_dir = _prepare_profile(account)
    responses: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "account": account.id,
        "label": account.label,
        "browser": account.browser,
        "profile_dir": str(profile_dir),
        "captured_at": captured_at.isoformat(),
        "analytics_url": config.analytics_url,
        "headed": headed,
        "codex_auth": _diagnose_auth_json(auth_json_path),
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
                            "detected": _detect_page_state(page.url, title, body_text, responses),
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


def _capture_json_response(response: Any, candidates: list[JsonCandidate]) -> None:
    url = response.url
    if not _looks_relevant_url(url):
        return
    content_type = response.headers.get("content-type", "")
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > JSON_MAX_BYTES:
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
    candidates.append(JsonCandidate(url=url, payload=payload))


def _diagnose_auth_json(path: Path | None) -> dict[str, Any]:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    auth_path = path or codex_home / "auth.json"
    expanded = auth_path.expanduser()
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
        for key in sorted(str(key) for key in mapping.keys())[:DIAGNOSTIC_MAX_KEYS]
    ]


def _diagnostic_value(value: Any) -> str | bool | int | float | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
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
    return value.astimezone(LOCAL_TZ).isoformat()


def _capture_diagnostic_response(response: Any, responses: list[dict[str, Any]]) -> None:
    url = response.url
    if "chatgpt.com" not in url.lower() and "openai.com" not in url.lower():
        return
    responses.append(
        {
            "status": response.status,
            "url": _redact_url(url),
            "content_type": response.headers.get("content-type", "").split(";")[0],
        }
    )


def _looks_relevant_url(url: str) -> bool:
    lower = url.lower()
    if "chatgpt.com" not in lower and "openai.com" not in lower:
        return False
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


def _safe_body_text(page: Any) -> str:
    try:
        return _limit_text(
            page.locator("body").inner_text(timeout=10_000),
            BROWSER_TEXT_MAX_CHARS,
        )
    except PlaywrightError:
        return ""


def _safe_title(page: Any) -> str:
    try:
        return _limit_text(page.title(), TITLE_MAX_CHARS)
    except PlaywrightError:
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
) -> str:
    haystack = f"{url}\n{title}\n{body_text}".lower()
    response_urls = "\n".join(str(item.get("url", "")) for item in responses or []).lower()
    response_statuses = {item.get("status") for item in responses or []}
    if title.strip().lower() == "just a moment...":
        return "cloudflare"
    if "/cdn-cgi/challenge-platform/" in response_urls:
        return "cloudflare"
    if 403 in response_statuses and "chatgpt.com/codex/cloud/settings/analytics" in haystack:
        return "cloudflare"
    if any(hint in haystack for hint in CLOUDFLARE_HINTS):
        return "cloudflare"
    if "auth" in url.lower() or any(hint in haystack for hint in LOGIN_HINTS):
        return "login_required"
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
) -> AccountStatus:
    lower = body_text.lower()
    if "auth" in current_url.lower() or (
        not _has_usage_value(five_hour)
        and not _has_usage_value(weekly)
        and any(hint in lower for hint in LOGIN_HINTS)
    ):
        return AccountStatus.LOGIN_REQUIRED
    if not _has_usage_value(five_hour) or not _has_usage_value(weekly):
        return AccountStatus.PARTIAL
    return AccountStatus.OK


def _has_usage_value(window: LimitWindow | None) -> bool:
    return window is not None and window.has_usage_value


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
    root = Path(account.profile_dir).expanduser()
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
    except OSError:
        pass


def _profile_browser_dir(browser: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", browser)


@contextmanager
def _profile_lock(profile_dir: Path):
    lock_path = profile_dir / ".codex-usage.lock"
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
        return sorted(str(key) for key in payload.keys())[:30]
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
    saved: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        path = save_dir / f"{account.id}-{index:02d}.json"
        payload_text = json.dumps(
            candidate.payload,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        _write_bounded_private_text(path, payload_text, label="probe output path")
        saved.append(str(path))
    body_path = save_dir / f"{account.id}-body.txt"
    _write_bounded_private_text(body_path, body_text, label="probe output path")
    saved.append(str(body_path))
    return saved


def _prepare_private_output_dir(path: Path, *, label: str) -> None:
    assert_no_symlink_ancestors(path, label=label)
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} is not a real directory: {path}")
    _chmod_private(path)


def _write_private_text(path: Path, text: str, *, label: str) -> None:
    write_private_output_text(path, text, label=label)


def _write_bounded_private_text(path: Path, text: str, *, label: str) -> None:
    if len(text.encode("utf-8")) > PROBE_OUTPUT_MAX_BYTES:
        raise ValueError(f"{label} too large; max {PROBE_OUTPUT_MAX_BYTES} bytes")
    _write_private_text(path, text, label=label)


def _validate_private_output_path(path: Path, *, label: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"{label} must be a regular file: {path}")


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path
    if path.startswith("/cdn-cgi/challenge-platform/"):
        path = "/cdn-cgi/challenge-platform/..."
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


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
