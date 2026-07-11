from __future__ import annotations

import base64
import errno
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .extractor import JsonCandidate, extract_windows
from .identity import backend_identity_from_payload
from .json_utils import loads_strict
from .models import Account, AccountStatus, AccountUsage, LimitWindow

WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DIRECT_RESPONSE_SAMPLE_COUNT = 3
DIRECT_RESET_BUCKET_SECONDS = 5
MAX_RESPONSE_BYTES = 2_000_000
MAX_AUTH_JSON_BYTES = 1_000_000
MAX_ACCESS_TOKEN_CHARS = 16_384
MAX_AUTH_ID_CHARS = 256


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
    auth_metadata: dict[str, datetime | None] = {}
    try:
        token, auth_metadata, auth_user_id, auth_account_id = _load_auth_token_and_metadata(path)
        if _is_access_token_expired(auth_metadata.get("auth_access_expires_at"), now=captured_at):
            return AccountUsage(
                account_id=account.id,
                label=account.label,
                captured_at=captured_at,
                status=AccountStatus.LOGIN_REQUIRED,
                error=_expired_auth_error(account.id, auth_metadata.get("auth_access_expires_at")),
                auth_last_refresh=auth_metadata.get("auth_last_refresh"),
                auth_access_expires_at=auth_metadata.get("auth_access_expires_at"),
                auth_id_expires_at=auth_metadata.get("auth_id_expires_at"),
            )
        payload = _fetch_stable_wham_usage(
            token,
            account_id=auth_account_id,
            timeout_seconds=timeout_seconds,
        )
        _, refreshed_metadata, refreshed_user_id, refreshed_account_id = (
            _load_auth_token_and_metadata(path)
        )
        if auth_identity_changed(
            before_user_id=auth_user_id,
            before_account_id=auth_account_id,
            after_user_id=refreshed_user_id,
            after_account_id=refreshed_account_id,
        ):
            raise DirectAuthError("auth.json identity changed during usage request")
        auth_metadata = refreshed_metadata
        auth_user_id = refreshed_user_id
        auth_account_id = refreshed_account_id
        backend_user_id, backend_account_id = backend_identity_from_payload(payload)
        try:
            backend_user_id, backend_account_id = canonical_backend_identity(
                backend_user_id,
                backend_account_id,
                auth_user_id=auth_user_id,
                auth_account_id=auth_account_id,
                require_backend_identity=True,
            )
        except ValueError as exc:
            raise DirectFetchError(str(exc)) from exc
        candidate = JsonCandidate(url=WHAM_USAGE_URL, payload=payload)
        five_hour, weekly = extract_windows(
            body_text="",
            json_candidates=(candidate,),
            now=captured_at,
        )
        status = (
            AccountStatus.OK
            if _has_usage_values(five_hour, weekly)
            else AccountStatus.PARTIAL
        )
        error = None if status == AccountStatus.OK else "usage limits not found in direct response"
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            five_hour=five_hour,
            weekly=weekly,
            status=status,
            error=error,
            auth_last_refresh=auth_metadata.get("auth_last_refresh"),
            auth_access_expires_at=auth_metadata.get("auth_access_expires_at"),
            auth_id_expires_at=auth_metadata.get("auth_id_expires_at"),
            source_urls=(_redact_url(WHAM_USAGE_URL),),
            backend_user_id=auth_user_id or backend_user_id,
            backend_account_id=auth_account_id or backend_account_id,
        )
    except DirectAuthError as exc:
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            status=AccountStatus.LOGIN_REQUIRED,
            error=str(exc),
            auth_last_refresh=auth_metadata.get("auth_last_refresh"),
            auth_access_expires_at=auth_metadata.get("auth_access_expires_at"),
            auth_id_expires_at=auth_metadata.get("auth_id_expires_at"),
        )
    except DirectFetchError as exc:
        return AccountUsage(
            account_id=account.id,
            label=account.label,
            captured_at=captured_at,
            status=AccountStatus.ERROR,
            error=str(exc),
            auth_last_refresh=auth_metadata.get("auth_last_refresh"),
            auth_access_expires_at=auth_metadata.get("auth_access_expires_at"),
            auth_id_expires_at=auth_metadata.get("auth_id_expires_at"),
        )


class DirectAuthError(Exception):
    pass


class DirectFetchError(Exception):
    pass


def auth_identity_changed(
    *,
    before_user_id: str | None,
    before_account_id: str | None,
    after_user_id: str | None,
    after_account_id: str | None,
) -> bool:
    if before_account_id or after_account_id:
        return before_account_id != after_account_id
    return before_user_id != after_user_id


def _resolve_auth_json_path(account: Account, override: Path | None) -> Path:
    if override is not None:
        return override.expanduser()
    if account.auth_json_path:
        return Path(account.auth_json_path).expanduser()
    return default_auth_json_path()


def _load_auth_token_and_metadata(
    path: Path,
) -> tuple[str, dict[str, datetime | None], str | None, str | None]:
    raw, _ = read_auth_json_file(path)
    try:
        payload = loads_strict(raw)
    except ValueError as exc:
        raise DirectAuthError(f"invalid auth.json: {path}") from exc
    if not isinstance(payload, dict):
        raise DirectAuthError(f"invalid auth.json structure: {path}")
    token, metadata = _extract_auth_details(payload, path=path)
    auth_user_id, auth_account_id = auth_identity_from_payload(payload, path=path)
    return token, metadata, auth_user_id, auth_account_id


def auth_identity_from_payload(
    payload: dict[str, Any],
    *,
    path: Path,
) -> tuple[str | None, str | None]:
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None, None

    user_ids: list[str] = []
    account_ids: list[str] = []
    top_level_account_id = _auth_account_id_from_payload(payload, path=path)
    if top_level_account_id is not None:
        account_ids.append(top_level_account_id)
    for token_name in ("id_token", "access_token"):
        claims = _jwt_claims(tokens.get(token_name))
        if not isinstance(claims, dict):
            continue
        token_user_id: str | None = None
        token_account_id: str | None = None
        auth_claims = claims.get("https://api.openai.com/auth")
        if isinstance(auth_claims, dict):
            token_user_id = _safe_auth_identity(
                auth_claims.get("chatgpt_user_id") or auth_claims.get("user_id")
            )
            token_account_id = _safe_auth_identity(auth_claims.get("chatgpt_account_id"))
        if token_user_id is None:
            token_user_id = _safe_auth_identity(
                claims.get("chatgpt_user_id") or claims.get("user_id")
            )
        if token_user_id is not None:
            user_ids.append(token_user_id)
        if token_account_id is not None:
            account_ids.append(token_account_id)
    if len(set(user_ids)) > 1 or len(set(account_ids)) > 1:
        raise DirectAuthError(f"auth.json token identities disagree: {path}")
    return (
        user_ids[0] if user_ids else None,
        account_ids[0] if account_ids else None,
    )


def auth_identity_from_file(path: Path) -> tuple[str | None, str | None]:
    path = path.expanduser()
    raw, _ = read_auth_json_file(path)
    try:
        payload = loads_strict(raw)
    except ValueError as exc:
        raise DirectAuthError(f"invalid auth.json: {path}") from exc
    if not isinstance(payload, dict):
        raise DirectAuthError(f"invalid auth.json structure: {path}")
    return auth_identity_from_payload(payload, path=path)


def auth_identity_for_account(account: Account) -> tuple[str | None, str | None]:
    if not account.auth_json_path:
        return None, None
    return auth_identity_from_file(Path(account.auth_json_path))


def _auth_account_id_from_payload(
    payload: dict[str, Any],
    *,
    path: Path,
) -> str | None:
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None
    value = tokens.get("account_id")
    if value is None:
        return None
    if not isinstance(value, str):
        raise DirectAuthError(f"auth.json account_id is invalid: {path}")
    value = value.strip()
    if not value or len(value) > MAX_AUTH_ID_CHARS or any(
        char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F
        for char in value
    ):
        raise DirectAuthError(f"auth.json account_id is invalid: {path}")
    return value


def _safe_auth_identity(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_AUTH_ID_CHARS or any(
        char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F
        for char in value
    ):
        return None
    return value


def _response_identity_matches_auth(
    *,
    backend_user_id: str | None,
    backend_account_id: str | None,
    auth_user_id: str | None,
    auth_account_id: str | None,
) -> bool:
    if auth_account_id and backend_account_id:
        accepted_account_ids = {auth_account_id}
        if auth_user_id:
            accepted_account_ids.add(auth_user_id)
        return backend_account_id in accepted_account_ids
    if auth_user_id and backend_user_id and backend_user_id != auth_user_id:
        return False
    return True


def canonical_backend_identity(
    backend_user_id: str | None,
    backend_account_id: str | None,
    *,
    auth_user_id: str | None,
    auth_account_id: str | None,
    require_backend_identity: bool = False,
) -> tuple[str | None, str | None]:
    if (
        require_backend_identity
        and (auth_user_id or auth_account_id)
        and not (backend_user_id or backend_account_id)
    ):
        raise ValueError("backend response has no account identity")
    if not _response_identity_matches_auth(
        backend_user_id=backend_user_id,
        backend_account_id=backend_account_id,
        auth_user_id=auth_user_id,
        auth_account_id=auth_account_id,
    ):
        raise ValueError("backend response belongs to a different account")
    return auth_user_id or backend_user_id, auth_account_id or backend_account_id


def auth_metadata_from_payload(payload: dict[str, Any]) -> dict[str, datetime | None]:
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return {
            "auth_last_refresh": _parse_iso_datetime(payload.get("last_refresh")),
            "auth_access_expires_at": None,
            "auth_id_expires_at": None,
        }
    return {
        "auth_last_refresh": _parse_iso_datetime(payload.get("last_refresh")),
        "auth_access_expires_at": _jwt_expiry(tokens.get("access_token")),
        "auth_id_expires_at": _jwt_expiry(tokens.get("id_token")),
    }


def _extract_auth_details(
    payload: dict[str, Any], *, path: Path
) -> tuple[str, dict[str, datetime | None]]:
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        raise DirectAuthError(f"auth.json has no tokens object: {path}")
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise DirectAuthError(f"auth.json has no access_token: {path}")
    access_token = access_token.strip()
    if len(access_token) > MAX_ACCESS_TOKEN_CHARS:
        raise DirectAuthError("auth.json access_token too large")
    if any(
        char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F
        for char in access_token
    ):
        raise DirectAuthError("auth.json access_token contains invalid characters")
    return access_token, auth_metadata_from_payload(payload)


def read_auth_json_file(path: Path) -> tuple[str, os.stat_result]:
    if path.is_symlink():
        raise DirectAuthError(f"auth.json is not a regular file: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
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


def _fetch_wham_usage(
    token: str,
    *,
    account_id: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Cache-Control": "no-cache, no-store",
        "Pragma": "no-cache",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    request = Request(
        WHAM_USAGE_URL,
        headers=headers,
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
        raise DirectFetchError("direct fetch failed: network error") from exc
    except OSError as exc:
        raise DirectFetchError("direct fetch failed: I/O error") from exc

    if len(body) > MAX_RESPONSE_BYTES:
        raise DirectFetchError("direct response too large")
    try:
        payload = loads_strict(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DirectFetchError("direct response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise DirectFetchError("direct response is not a JSON object")
    return payload


def _fetch_stable_wham_usage(
    token: str,
    *,
    account_id: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    payloads = [
        _fetch_wham_usage(
            token,
            account_id=account_id,
            timeout_seconds=timeout_seconds,
        )
        for _ in range(DIRECT_RESPONSE_SAMPLE_COUNT)
    ]
    groups: dict[tuple, list[tuple[int, dict[str, Any]]]] = {}
    for index, payload in enumerate(payloads):
        groups.setdefault(_usage_response_signature(payload), []).append((index, payload))
    best_group = max(
        groups.values(),
        key=lambda group: (
            len(group),
            _usage_response_completeness(group[0][1]),
            -group[0][0],
        ),
    )
    if len(best_group) * 2 <= len(payloads):
        raise DirectFetchError("direct response limits were inconsistent across samples")
    return best_group[0][1]


def _usage_response_signature(payload: dict[str, Any]) -> tuple:
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return (None, None)
    return tuple(
        _usage_window_signature(rate_limit.get(key))
        for key in ("primary_window", "secondary_window")
    )


def _usage_window_signature(value: Any) -> tuple | None:
    if not isinstance(value, dict):
        return None
    return (
        _signature_number(value.get("limit_window_seconds")),
        _signature_number(value.get("used_percent")),
        _signature_reset(value.get("reset_at")),
    )


def _signature_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _signature_reset(value: Any) -> int | None:
    number = _signature_number(value)
    if number is None:
        return None
    return int(number // DIRECT_RESET_BUCKET_SECONDS)


def _usage_response_completeness(payload: dict[str, Any]) -> int:
    signature = _usage_response_signature(payload)
    return sum(value is not None for value in signature)


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


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _jwt_expiry(token: Any) -> datetime | None:
    claims = _jwt_claims(token)
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(exp), tz=UTC).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def _jwt_claims(token: Any) -> dict[str, Any] | None:
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded)
    except (ValueError, OSError, json.JSONDecodeError, UnicodeError):
        return None
    return claims if isinstance(claims, dict) else None


def _is_access_token_expired(expiry: datetime | None, *, now: datetime) -> bool:
    return bool(expiry is not None and expiry <= now)


def _expired_auth_error(account_id: str, expiry: datetime | None) -> str:
    if expiry is None:
        return f"auth.json access_token expired; run `codex-usage reactivate {account_id}`"
    return (
        "auth.json access_token expired at "
        f"{expiry.astimezone().strftime('%d.%m.%Y %H:%M')}; "
        f"run `codex-usage reactivate {account_id}`"
    )


def _has_usage_values(
    five_hour: LimitWindow | None,
    weekly: LimitWindow | None,
) -> bool:
    return bool(
        five_hour is not None
        and weekly is not None
        and five_hour.has_usage_value
        and weekly.has_usage_value
    )
