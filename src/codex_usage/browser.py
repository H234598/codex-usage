from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .config import AppConfig
from .extractor import JsonCandidate, extract_windows
from .models import Account, AccountStatus, AccountUsage

JSON_MAX_BYTES = 2_000_000
LOGIN_HINTS = ("log in", "sign in", "anmelden", "einloggen", "continue with")


def login_account(account: Account, config: AppConfig) -> None:
    profile_dir = _prepare_profile(account)
    with _profile_lock(profile_dir):
        with sync_playwright() as playwright:
            context = _launch_persistent_context(playwright, account, profile_dir, headless=False)
            page = context.new_page()
            page.goto(config.analytics_url, wait_until="domcontentloaded", timeout=60_000)
            print(f"Browserprofil: {profile_dir}")
            print(f"Browser: {account.browser}")
            print("Melde dich im geoeffneten Browser an und oeffne ggf. die Codex-Analytics-Seite.")
            input("Druecke Enter, wenn der Account eingeloggt ist und die Seite sichtbar ist ... ")
            context.close()


def fetch_account_usage(
    account: Account,
    config: AppConfig,
    *,
    headed: bool = False,
    timeout_ms: int = 45_000,
) -> AccountUsage:
    captured_at = datetime.now().astimezone()
    candidates: list[JsonCandidate] = []
    source_urls: set[str] = set()

    try:
        profile_dir = _prepare_profile(account)
        with _profile_lock(profile_dir):
            with sync_playwright() as playwright:
                context = _launch_persistent_context(
                    playwright,
                    account,
                    profile_dir,
                    headless=not headed and config.headless,
                )
                page = context.new_page()
                page.on("response", lambda response: _capture_json_response(response, candidates))
                page.goto(config.analytics_url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=12_000)
                except PlaywrightTimeoutError:
                    pass
                body_text = _safe_body_text(page)
                current_url = page.url
                context.close()

        source_urls.update(_redact_url(candidate.url) for candidate in candidates)
        five_hour, weekly = extract_windows(body_text=body_text, json_candidates=candidates)
        status = _status_for_result(
            body_text=body_text,
            current_url=current_url,
            five_hour=five_hour,
            weekly=weekly,
        )
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            five_hour=five_hour,
            weekly=weekly,
            status=status,
            source_urls=tuple(sorted(source_urls)),
        )
    except PlaywrightError as exc:
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            status=AccountStatus.ERROR,
            error=_clean_error(str(exc)),
        )


def probe_account(
    account: Account,
    config: AppConfig,
    *,
    headed: bool = True,
    save_dir: Path | None = None,
) -> dict[str, Any]:
    captured_at = datetime.now().astimezone()
    candidates: list[JsonCandidate] = []
    profile_dir = _prepare_profile(account)
    with _profile_lock(profile_dir):
        with sync_playwright() as playwright:
            context = _launch_persistent_context(
                playwright,
                account,
                profile_dir,
                headless=not headed,
            )
            page = context.new_page()
            page.on("response", lambda response: _capture_json_response(response, candidates))
            page.goto(config.analytics_url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
            except PlaywrightTimeoutError:
                pass
            body_text = _safe_body_text(page)
            context.close()

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


def _capture_json_response(response: Any, candidates: list[JsonCandidate]) -> None:
    url = response.url
    if not _looks_relevant_url(url):
        return
    content_type = response.headers.get("content-type", "")
    content_length = response.headers.get("content-length")
    if content_length and int(content_length) > JSON_MAX_BYTES:
        return
    if "json" not in content_type.lower() and not re.search(r"/(api|backend|accounts?)/", url):
        return
    try:
        if hasattr(response, "finished"):
            response.finished()
        text = response.text()
    except Exception:
        return
    if len(text.encode("utf-8", errors="ignore")) > JSON_MAX_BYTES:
        return
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return
    candidates.append(JsonCandidate(url=url, payload=payload))


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
        return page.locator("body").inner_text(timeout=10_000)
    except PlaywrightError:
        return ""


def _status_for_result(
    *,
    body_text: str,
    current_url: str,
    five_hour: object,
    weekly: object,
) -> AccountStatus:
    lower = body_text.lower()
    if "auth" in current_url.lower() or (
        not five_hour and not weekly and any(hint in lower for hint in LOGIN_HINTS)
    ):
        return AccountStatus.LOGIN_REQUIRED
    if not five_hour or not weekly:
        return AccountStatus.PARTIAL
    return AccountStatus.OK


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


def _prepare_profile(account: Account) -> Path:
    root = Path(account.profile_dir).expanduser()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    _chmod_private(root)
    marker = root / ".codex-usage-profile"
    if not marker.exists():
        marker.write_text("codex-usage persistent browser profile root\n", encoding="utf-8")
        _chmod_private(marker, mode=0o600)

    path = root / _profile_browser_dir(account.browser)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    _chmod_private(path)
    engine_marker = path / ".codex-usage-browser-profile"
    if not engine_marker.exists():
        engine_marker.write_text(f"{account.browser}\n", encoding="utf-8")
        _chmod_private(engine_marker, mode=0o600)
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
    with lock_path.open("w", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"profile is already in use: {profile_dir}") from exc
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
    save_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    saved: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        path = save_dir / f"{account.id}-{index:02d}.json"
        payload_text = json.dumps(candidate.payload, ensure_ascii=False, indent=2)
        path.write_text(payload_text, encoding="utf-8")
        path.chmod(0o600)
        saved.append(str(path))
    body_path = save_dir / f"{account.id}-body.txt"
    body_path.write_text(body_text, encoding="utf-8")
    body_path.chmod(0o600)
    saved.append(str(body_path))
    return saved


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _clean_error(error: str) -> str:
    return re.sub(r"\s+", " ", error).strip()[:500]
