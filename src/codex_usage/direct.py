from __future__ import annotations

import errno
import json
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .extractor import JsonCandidate, extract_windows
from .models import Account, AccountStatus, AccountUsage

WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
MAX_RESPONSE_BYTES = 2_000_000
MAX_AUTH_JSON_BYTES = 1_000_000


def default_auth_json_path() -> Path:
    return Path.home() / ".codex" / "auth.json"


def fetch_account_usage_direct(
    account: Account,
    *,
    auth_json_path: Path | None = None,
    timeout_seconds: int = 20,
) -> AccountUsage:
    captured_at = datetime.now().astimezone()
    path = _resolve_auth_json_path(account, auth_json_path)
    try:
        token = _load_access_token(path)
        payload = _fetch_wham_usage(token, timeout_seconds=timeout_seconds)
        candidate = JsonCandidate(url=WHAM_USAGE_URL, payload=payload)
        five_hour, weekly = extract_windows(
            body_text="",
            json_candidates=(candidate,),
            now=captured_at,
        )
        status = AccountStatus.OK if five_hour and weekly else AccountStatus.PARTIAL
        error = None if status == AccountStatus.OK else "usage limits not found in direct response"
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            five_hour=five_hour,
            weekly=weekly,
            status=status,
            error=error,
            source_urls=(_redact_url(WHAM_USAGE_URL),),
        )
    except DirectAuthError as exc:
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            status=AccountStatus.LOGIN_REQUIRED,
            error=str(exc),
        )
    except DirectFetchError as exc:
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            status=AccountStatus.ERROR,
            error=str(exc),
        )


class DirectAuthError(Exception):
    pass


class DirectFetchError(Exception):
    pass


def _resolve_auth_json_path(account: Account, override: Path | None) -> Path:
    if override is not None:
        return override.expanduser()
    if account.auth_json_path:
        return Path(account.auth_json_path).expanduser()
    return default_auth_json_path()


def _load_access_token(path: Path) -> str:
    raw, _ = read_auth_json_file(path)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DirectAuthError(f"invalid auth.json: {path}") from exc
    if not isinstance(payload, dict):
        raise DirectAuthError(f"invalid auth.json structure: {path}")
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        raise DirectAuthError(f"auth.json has no tokens object: {path}")
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise DirectAuthError(f"auth.json has no access_token: {path}")
    return access_token


def read_auth_json_file(path: Path) -> tuple[str, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise DirectAuthError(f"auth.json is not a regular file: {path}") from exc
        raise DirectAuthError(f"cannot read auth.json: {path}") from exc

    try:
        file_stat = os.fstat(fd)
        _validate_auth_json_stat(path, file_stat)
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(MAX_AUTH_JSON_BYTES + 1)
    except OSError as exc:
        raise DirectAuthError(f"cannot read auth.json: {path}") from exc
    finally:
        if fd >= 0:
            os.close(fd)

    if len(raw) > MAX_AUTH_JSON_BYTES:
        raise DirectAuthError(f"auth.json too large; max {MAX_AUTH_JSON_BYTES} bytes")
    try:
        return raw.decode("utf-8"), file_stat
    except UnicodeDecodeError as exc:
        raise DirectAuthError(f"invalid auth.json: {path}") from exc


def validate_auth_json_file(path: Path):
    if path.is_symlink():
        raise DirectAuthError(f"auth.json is not a regular file: {path}")
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise DirectAuthError(f"cannot read auth.json: {path}") from exc
    _validate_auth_json_stat(path, file_stat)
    return file_stat


def _validate_auth_json_stat(path: Path, file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise DirectAuthError(f"auth.json is not a regular file: {path}")
    if file_stat.st_size > MAX_AUTH_JSON_BYTES:
        raise DirectAuthError(f"auth.json too large; max {MAX_AUTH_JSON_BYTES} bytes")
    if file_stat.st_mode & 0o077:
        raise DirectAuthError(f"auth.json permissions too broad; run chmod 600 {path}")


def _fetch_wham_usage(token: str, *, timeout_seconds: int) -> dict[str, Any]:
    request = Request(
        WHAM_USAGE_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            content_type = _response_content_type(response).lower()
            if content_type and "json" not in content_type:
                raise DirectFetchError("direct response is not JSON content")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise DirectAuthError(f"direct auth failed: HTTP {exc.code}") from exc
        raise DirectFetchError(f"direct fetch failed: HTTP {exc.code}") from exc
    except URLError as exc:
        raise DirectFetchError(f"direct fetch failed: {exc.reason}") from exc
    except OSError as exc:
        raise DirectFetchError(f"direct fetch failed: {exc}") from exc

    if len(body) > MAX_RESPONSE_BYTES:
        raise DirectFetchError("direct response too large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DirectFetchError("direct response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise DirectFetchError("direct response is not a JSON object")
    return payload


def _response_content_type(response: Any) -> str:
    headers = getattr(response, "headers", None)
    if headers is not None:
        value = headers.get("content-type") or headers.get("Content-Type")
        if value:
            return str(value)
    getheader = getattr(response, "getheader", None)
    if callable(getheader):
        return str(getheader("content-type") or "")
    return ""


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
