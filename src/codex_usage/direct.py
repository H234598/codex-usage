from __future__ import annotations

import base64
import errno
import json
import math
import os
import stat
import time
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .extractor import LOCAL_TZ, JsonCandidate, extract_windows
from .identity import backend_identity_from_payload, backend_plan_type_from_payload
from .json_utils import loads_strict
from .models import Account, AccountStatus, AccountUsage, LimitWindow

WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DIRECT_RESPONSE_SAMPLE_COUNT = 3
DIRECT_STABILITY_ATTEMPTS = 3
DIRECT_STABILITY_RETRY_DELAY_SECONDS = 0.05
DIRECT_RESET_BUCKET_SECONDS = 5
DIRECT_RESET_TRANSITION_MARGIN_SECONDS = 30
DIRECT_PROGRESSIVE_STEP_PERCENT = 1
MAX_RESPONSE_BYTES = 2_000_000
MAX_AUTH_JSON_BYTES = 1_000_000
MAX_ACCESS_TOKEN_CHARS = 16_384
MAX_AUTH_ID_CHARS = 256
PLAN_TYPE_ALIASES = {"pro": "plus"}
SUPPORTED_WINDOW_SECONDS = frozenset((18_000, 604_800))
INFERRED_INACTIVE_FIVE_HOUR_SOURCE = "inferred:inactive-five-hour"


def default_auth_json_path() -> Path:
    return Path.home() / ".codex" / "auth.json"


def fetch_account_usage_direct(
    account: Account,
    *,
    auth_json_path: Path | None = None,
    reject_ambiguous_backend_identity: bool = False,
    timeout_seconds: int = 20,
) -> AccountUsage:
    captured_at = datetime.now(tz=LOCAL_TZ)
    path = _resolve_auth_json_path(account, auth_json_path)
    auth_metadata: dict[str, datetime | None] = {}
    auth_user_id: str | None = None
    auth_account_id: str | None = None
    auth_plan_type: str | None = None
    try:
        (
            token,
            auth_metadata,
            auth_user_id,
            auth_account_id,
            auth_plan_type,
        ) = _load_auth_token_and_metadata(path)
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
                backend_user_id=auth_user_id,
                backend_account_id=auth_account_id,
            )
        try:
            payload = _fetch_stable_wham_usage(
                token,
                account_id=auth_account_id,
                timeout_seconds=timeout_seconds,
            )
        except DirectAuthError as exc:
            if not _is_retryable_direct_auth_error(exc):
                raise
            try:
                (
                    refreshed_token,
                    refreshed_metadata,
                    refreshed_user_id,
                    refreshed_account_id,
                    refreshed_plan_type,
                ) = _load_auth_token_and_metadata(path)
            except DirectAuthError:
                raise exc from None
            if auth_identity_changed(
                before_user_id=auth_user_id,
                before_account_id=auth_account_id,
                after_user_id=refreshed_user_id,
                after_account_id=refreshed_account_id,
            ) or _auth_plan_type_changed(auth_plan_type, refreshed_plan_type):
                auth_user_id = None
                auth_account_id = None
                auth_plan_type = None
                raise DirectAuthError("auth.json identity changed during usage request") from None
            if (
                refreshed_token == token
                or _is_access_token_expired(
                    refreshed_metadata.get("auth_access_expires_at"),
                    now=datetime.now(tz=LOCAL_TZ),
                )
            ):
                raise exc from None
            token = refreshed_token
            auth_metadata = refreshed_metadata
            auth_user_id = refreshed_user_id
            auth_account_id = refreshed_account_id
            auth_plan_type = refreshed_plan_type
            payload = _fetch_stable_wham_usage(
                token,
                account_id=auth_account_id,
                timeout_seconds=timeout_seconds,
            )
        (
            _,
            refreshed_metadata,
            refreshed_user_id,
            refreshed_account_id,
            refreshed_plan_type,
        ) = (
            _load_auth_token_and_metadata(path)
        )
        if auth_identity_changed(
            before_user_id=auth_user_id,
            before_account_id=auth_account_id,
            after_user_id=refreshed_user_id,
            after_account_id=refreshed_account_id,
        ) or _auth_plan_type_changed(auth_plan_type, refreshed_plan_type):
            # Do not let a pre-request identity authorize stale values after a token switch.
            auth_user_id = None
            auth_account_id = None
            auth_plan_type = None
            raise DirectAuthError("auth.json identity changed during usage request")
        auth_metadata = refreshed_metadata
        auth_user_id = refreshed_user_id
        auth_account_id = refreshed_account_id
        auth_plan_type = refreshed_plan_type
        backend_user_id, backend_account_id = backend_identity_from_payload(payload)
        backend_plan_type = backend_plan_type_from_payload(payload)
        try:
            backend_user_id, backend_account_id = canonical_backend_identity(
                backend_user_id,
                backend_account_id,
                auth_user_id=auth_user_id,
                auth_account_id=auth_account_id,
                auth_plan_type=auth_plan_type,
                backend_plan_type=backend_plan_type,
                require_backend_identity=True,
                reject_ambiguous_backend_identity=reject_ambiguous_backend_identity,
            )
        except ValueError as exc:
            raise DirectFetchError(str(exc)) from exc
        candidate = JsonCandidate(url=WHAM_USAGE_URL, payload=payload)
        five_hour, weekly = extract_windows(
            body_text="",
            json_candidates=(candidate,),
            now=captured_at,
        )
        five_hour = infer_inactive_five_hour_window(
            five_hour,
            weekly,
            plan_type=backend_plan_type or auth_plan_type,
            source="direct",
        )
        inferred_inactive_five_hour = is_inferred_inactive_five_hour(five_hour)
        status = (
            AccountStatus.OK
            if _has_usage_values(five_hour, weekly) and not inferred_inactive_five_hour
            else AccountStatus.PARTIAL
        )
        error = (
            None
            if status == AccountStatus.OK
            else (
                _inactive_five_hour_error("direct", backend_plan_type or auth_plan_type)
                if inferred_inactive_five_hour
                else _missing_usage_limits_error(payload, backend_plan_type)
            )
        )
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
            backend_user_id=auth_user_id,
            backend_account_id=auth_account_id,
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
            backend_user_id=auth_user_id,
            backend_account_id=auth_account_id,
        )


class DirectAuthError(Exception):
    pass


class DirectFetchError(Exception):
    pass


def infer_inactive_five_hour_window(
    five_hour: LimitWindow | None,
    weekly: LimitWindow | None,
    *,
    plan_type: str | None,
    source: str,
) -> LimitWindow | None:
    """Represent a paid plan's inactive 5h bucket without inventing a reset."""
    if (
        five_hour is not None
        or weekly is None
        or not weekly.has_usage_value
        or not plan_type
        or _normalized_plan_type(plan_type) == "free"
    ):
        return five_hour
    return LimitWindow(
        name="5h",
        used=0.0,
        limit=100.0,
        remaining=100.0,
        percent=100.0,
        reset_at=None,
        raw=None,
        source=f"{INFERRED_INACTIVE_FIVE_HOUR_SOURCE}:{source}",
    )


def is_inferred_inactive_five_hour(window: LimitWindow | None) -> bool:
    return bool(
        window is not None
        and window.source.startswith(INFERRED_INACTIVE_FIVE_HOUR_SOURCE)
    )


def inactive_five_hour_error(backend: str, plan_type: str | None) -> str:
    plan = _normalized_plan_type(plan_type) if plan_type else "unknown"
    return (
        f"5h limit inactive in {backend} response "
        f"(plan {plan}; assumed 100% remaining; reset unknown)"
    )


def _inactive_five_hour_error(backend: str, plan_type: str | None) -> str:
    return inactive_five_hour_error(backend, plan_type)


def _missing_usage_limits_error(
    payload: dict[str, Any],
    backend_plan_type: str | None,
) -> str:
    rate_limit = payload.get("rate_limit")
    unsupported: list[int] = []
    available: set[int] = set()
    if isinstance(rate_limit, dict):
        for key in ("primary_window", "secondary_window"):
            window = rate_limit.get(key)
            if not isinstance(window, dict):
                continue
            raw_seconds = window.get("limit_window_seconds")
            if isinstance(raw_seconds, bool) or not isinstance(raw_seconds, (int, float)):
                continue
            try:
                seconds = float(raw_seconds)
            except (OverflowError, TypeError, ValueError):
                continue
            if (
                seconds > 0
                and seconds.is_integer()
            ):
                duration = int(seconds)
                if duration not in SUPPORTED_WINDOW_SECONDS:
                    unsupported.append(duration)
                else:
                    used_percent = window.get("used_percent")
                    if (
                        isinstance(used_percent, (int, float))
                        and not isinstance(used_percent, bool)
                        and 0 <= used_percent <= 100
                    ):
                        available.add(duration)
    plan = _normalized_plan_type(backend_plan_type) if backend_plan_type else "unknown"
    if not unsupported and len(available) == 1:
        available_window = "5h" if 18_000 in available else "weekly"
        missing_window = "weekly" if 18_000 in available else "5h"
        return (
            f"{missing_window} limit unavailable in direct response "
            f"(plan {plan}; available window {available_window})"
        )
    if not unsupported:
        return "usage limits not found in direct response"
    durations = ", ".join(f"{seconds}s" for seconds in sorted(set(unsupported)))
    return (
        "requested 5h/weekly limits unavailable in direct response "
        f"(plan {plan}; backend window {durations})"
    )


def _is_retryable_direct_auth_error(error: DirectAuthError) -> bool:
    return str(error) in {
        "direct auth failed: HTTP 401",
        "direct auth failed: HTTP 403",
    }


def auth_identity_changed(
    *,
    before_user_id: str | None,
    before_account_id: str | None,
    after_user_id: str | None,
    after_account_id: str | None,
) -> bool:
    """Treat account IDs as primary while rejecting two known user IDs that differ."""
    if before_account_id or after_account_id:
        if before_account_id != after_account_id:
            return True
        return before_user_id != after_user_id
    return before_user_id != after_user_id


def _resolve_auth_json_path(account: Account, override: Path | None) -> Path:
    if override is not None:
        return override.expanduser()
    if account.auth_json_path:
        return Path(account.auth_json_path).expanduser()
    return default_auth_json_path()


def _load_auth_token_and_metadata(
    path: Path,
) -> tuple[str, dict[str, datetime | None], str | None, str | None, str | None]:
    raw, _ = read_auth_json_file(path)
    try:
        payload = loads_strict(raw)
    except ValueError as exc:
        raise DirectAuthError(f"invalid auth.json: {path}") from exc
    if not isinstance(payload, dict):
        raise DirectAuthError(f"invalid auth.json structure: {path}")
    token, metadata = _extract_auth_details(payload, path=path)
    auth_user_id, auth_account_id = auth_identity_from_payload(payload, path=path)
    auth_plan_type = auth_plan_type_from_payload(payload, path=path)
    return token, metadata, auth_user_id, auth_account_id, auth_plan_type


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


def auth_email_from_payload(
    payload: dict[str, Any],
    *,
    path: Path,
) -> str | None:
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None
    emails: list[str] = []
    for token_name in ("id_token", "access_token"):
        claims = _jwt_claims(tokens.get(token_name))
        if not isinstance(claims, dict) or "email" not in claims:
            continue
        email = _safe_auth_identity(claims.get("email"))
        if email is None:
            raise DirectAuthError(f"auth.json token email is invalid: {path}")
        emails.append(email)
    if len({email.casefold() for email in emails}) > 1:
        raise DirectAuthError(f"auth.json token emails disagree: {path}")
    return emails[0] if emails else None


def auth_email_from_file(path: Path) -> str | None:
    path = path.expanduser()
    raw, _ = read_auth_json_file(path)
    try:
        payload = loads_strict(raw)
    except ValueError as exc:
        raise DirectAuthError(f"invalid auth.json: {path}") from exc
    if not isinstance(payload, dict):
        raise DirectAuthError(f"invalid auth.json structure: {path}")
    return auth_email_from_payload(payload, path=path)


def auth_plan_type_from_payload(
    payload: dict[str, Any],
    *,
    path: Path,
) -> str | None:
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None
    plan_types: list[str] = []
    for token_name in ("id_token", "access_token"):
        claims = _jwt_claims(tokens.get(token_name))
        if not isinstance(claims, dict):
            continue
        auth_claims = claims.get("https://api.openai.com/auth")
        if not isinstance(auth_claims, dict):
            continue
        plan_type = _safe_auth_plan_type(auth_claims.get("chatgpt_plan_type"))
        if plan_type is not None:
            plan_types.append(plan_type)
    if len(set(plan_types)) > 1:
        raise DirectAuthError(f"auth.json token plan types disagree: {path}")
    return plan_types[0] if plan_types else None


def auth_plan_type_from_file(path: Path) -> str | None:
    path = path.expanduser()
    raw, _ = read_auth_json_file(path)
    try:
        payload = loads_strict(raw)
    except ValueError as exc:
        raise DirectAuthError(f"invalid auth.json: {path}") from exc
    if not isinstance(payload, dict):
        raise DirectAuthError(f"invalid auth.json structure: {path}")
    return auth_plan_type_from_payload(payload, path=path)


def auth_plan_type_for_account(account: Account) -> str | None:
    if not account.auth_json_path:
        return None
    return auth_plan_type_from_file(Path(account.auth_json_path))


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


def _safe_auth_plan_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 64 or any(
        char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value
    ):
        return None
    return value


def _auth_plan_type_changed(before: str | None, after: str | None) -> bool:
    return bool(before and after and _normalized_plan_type(before) != _normalized_plan_type(after))


def _normalized_plan_type(value: str) -> str:
    normalized = value.strip().casefold()
    return PLAN_TYPE_ALIASES.get(normalized, normalized)


def _response_identity_matches_auth(
    *,
    backend_user_id: str | None,
    backend_account_id: str | None,
    auth_user_id: str | None,
    auth_account_id: str | None,
) -> bool:
    if backend_account_id and auth_account_id:
        if backend_account_id == auth_account_id:
            return True
        if backend_account_id == auth_user_id:
            return not backend_user_id or backend_user_id == auth_user_id
        return False
    if backend_account_id and auth_user_id:
        # WHAM can echo the shared user ID as account_id. Without the
        # configured account ID, an unrelated backend account is unverifiable.
        return (
            backend_account_id == auth_user_id
            and (not backend_user_id or backend_user_id == auth_user_id)
        )
    if auth_user_id and backend_user_id and backend_user_id != auth_user_id:
        return False
    return True


def canonical_backend_identity(
    backend_user_id: str | None,
    backend_account_id: str | None,
    *,
    auth_user_id: str | None,
    auth_account_id: str | None,
    auth_plan_type: str | None = None,
    backend_plan_type: str | None = None,
    require_backend_identity: bool = False,
    reject_ambiguous_backend_identity: bool = False,
) -> tuple[str | None, str | None]:
    if (
        require_backend_identity
        and (auth_user_id or auth_account_id)
        and not (backend_user_id or backend_account_id)
    ):
        raise ValueError("backend response has no account identity")
    if reject_ambiguous_backend_identity and auth_user_id and not auth_account_id:
        raise ValueError("backend response has ambiguous account identity")
    if (
        auth_plan_type
        and backend_plan_type
        and _normalized_plan_type(auth_plan_type)
        != _normalized_plan_type(backend_plan_type)
    ):
        raise ValueError("backend response belongs to a different account")
    if (
        auth_plan_type
        and auth_user_id
        and auth_account_id
        and backend_account_id == auth_user_id
        and backend_account_id != auth_account_id
        and not backend_plan_type
    ):
        raise ValueError("backend response belongs to a different account")
    if (
        reject_ambiguous_backend_identity
        and auth_user_id
        and auth_account_id
        and backend_account_id != auth_account_id
    ):
        raise ValueError("backend response has ambiguous account identity")
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
    last_error: DirectFetchError | None = None
    for attempt in range(DIRECT_STABILITY_ATTEMPTS):
        try:
            payloads = [
                _fetch_wham_usage(
                    token,
                    account_id=account_id,
                    timeout_seconds=timeout_seconds,
                )
                for _ in range(DIRECT_RESPONSE_SAMPLE_COUNT)
            ]
            return _select_stable_wham_usage(payloads)
        except DirectFetchError as exc:
            last_error = exc
            if attempt + 1 >= DIRECT_STABILITY_ATTEMPTS:
                raise
            time.sleep(DIRECT_STABILITY_RETRY_DELAY_SECONDS)
        except StopIteration:
            # Test doubles with only one sample batch must still exercise the
            # original rejection path instead of leaking an iterator error.
            if last_error is not None:
                raise last_error from None
            raise
    assert last_error is not None
    raise last_error


def _select_stable_wham_usage(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple, list[tuple[int, dict[str, Any]]]] = {}
    for index, payload in enumerate(payloads):
        groups.setdefault(_usage_response_signature(payload), []).append((index, payload))
    value_groups = [
        group
        for group in groups.values()
        if _usage_response_completeness(group[0][1]) > 0
    ]
    candidate_groups = value_groups or list(groups.values())
    best_group = max(
        candidate_groups,
        key=lambda group: (
            _usage_response_completeness(group[0][1]),
            len(group),
            -group[0][0],
        ),
    )
    latest_index = len(payloads) - 1
    latest_is_in_best_group = any(index == latest_index for index, _ in best_group)
    latest_is_relative_reset = _latest_response_is_relative_reset(
        payloads,
        best_group,
    )
    if _has_reset_regression(payloads) and not latest_is_in_best_group:
        if latest_is_relative_reset:
            return payloads[-1]
        raise DirectFetchError("direct response limits were inconsistent across samples")
    if latest_is_relative_reset:
        return payloads[-1]
    if len(best_group) * 2 <= len(payloads):
        if _usage_response_progresses(payloads):
            return payloads[-1]
        raise DirectFetchError("direct response limits were inconsistent across samples")
    if _latest_response_progresses_beyond_group(payloads, best_group):
        return payloads[-1]
    return best_group[0][1]


def _usage_response_signature(payload: dict[str, Any]) -> tuple:
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return (backend_identity_from_payload(payload), (None, None))
    return (
        backend_identity_from_payload(payload),
        tuple(
            _usage_window_signature(rate_limit.get(key))
            for key in ("primary_window", "secondary_window")
        ),
    )


def _usage_window_signature(value: Any) -> tuple | None:
    if not isinstance(value, dict):
        return None
    return (
        _signature_number(value.get("limit_window_seconds")),
        _signature_number(value.get("used_percent")),
        _signature_reset_identity(value),
        _signature_relative_reset_phase(value),
    )


def _signature_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _signature_reset(value: Any) -> int | None:
    number = _signature_number(value)
    if number is None:
        return None
    return int(number // DIRECT_RESET_BUCKET_SECONDS)


def _usage_response_completeness(payload: dict[str, Any]) -> int:
    _identity, windows = _usage_response_signature(payload)
    return sum(
        value is not None
        and value[0] in SUPPORTED_WINDOW_SECONDS
        and value[1] is not None
        and 0 <= value[1] <= 100
        for value in windows
    )


def _usage_response_progresses(
    payloads: list[dict[str, Any]],
    *,
    max_step_percent: float | None = DIRECT_PROGRESSIVE_STEP_PERCENT,
) -> bool:
    signatures = [_usage_response_signature(payload) for payload in payloads]
    identities = {signature[0] for signature in signatures}
    if len(identities) != 1:
        return False
    observed_window = False
    for window_index in range(2):
        windows = [signature[1][window_index] for signature in signatures]
        if all(window is None for window in windows):
            continue
        if any(window is None or window[1] is None for window in windows):
            return False
        if not _progressive_window_identity_is_stable(windows):
            return False
        observed_window = True
        used_values = [window[1] for window in windows]
        for previous, current in pairwise(used_values):
            if current < previous:
                return False
            if max_step_percent is not None and current - previous > max_step_percent:
                return False
    return observed_window


def _latest_response_progresses_beyond_group(
    payloads: list[dict[str, Any]],
    best_group: list[tuple[int, dict[str, Any]]],
) -> bool:
    if not _usage_response_progresses(payloads, max_step_percent=None):
        return False
    latest = _usage_response_signature(payloads[-1])[1]
    stable = _usage_response_signature(best_group[0][1])[1]
    progressed = False
    for latest_window, stable_window in zip(latest, stable, strict=True):
        if latest_window is None and stable_window is None:
            continue
        if latest_window is None or stable_window is None:
            return False
        if latest_window[1] is None or stable_window[1] is None:
            return False
        if latest_window[1] < stable_window[1]:
            return False
        progressed = progressed or latest_window[1] > stable_window[1]
    return progressed


def _latest_response_is_relative_reset(
    payloads: list[dict[str, Any]],
    best_group: list[tuple[int, dict[str, Any]]],
) -> bool:
    if len(payloads) < 2 or not best_group:
        return False
    latest_index = len(payloads) - 1
    if any(index == latest_index for index, _ in best_group):
        return False
    previous_index, previous = max(best_group, key=lambda item: item[0])
    if previous_index >= latest_index:
        return False
    if backend_identity_from_payload(previous) != backend_identity_from_payload(payloads[-1]):
        return False

    reset_seen = False
    for window_key in ("primary_window", "secondary_window"):
        previous_window = _rate_limit_window(previous, window_key)
        current_window = _rate_limit_window(payloads[-1], window_key)
        if previous_window is None or current_window is None:
            if previous_window is None and current_window is None:
                continue
            # A reset transition must not turn a complete sample into a
            # partial one. Otherwise one fresh window can make us accept a
            # response that silently drops the other account limit.
            return False
        previous_used = _signature_number(previous_window.get("used_percent"))
        current_used = _signature_number(current_window.get("used_percent"))
        if previous_used is None or current_used is None:
            if previous_used is None and current_used is None:
                continue
            return False
        previous_duration = _signature_number(previous_window.get("limit_window_seconds"))
        current_duration = _signature_number(current_window.get("limit_window_seconds"))
        previous_identity = _signature_reset_identity(previous_window)
        current_identity = _signature_reset_identity(current_window)
        if current_used > previous_used:
            # The reset candidate is sampled immediately after the majority.
            # Permit only a small monotone change in the non-reset window and
            # require its window identity to remain unchanged.
            if (
                previous_duration is None
                or current_duration is None
                or previous_duration != current_duration
                or previous_identity != current_identity
                or current_used - previous_used > DIRECT_PROGRESSIVE_STEP_PERCENT
            ):
                return False
            continue
        usage_decreased = current_used < previous_used
        previous_after = _signature_number(previous_window.get("reset_after_seconds"))
        current_after = _signature_number(current_window.get("reset_after_seconds"))
        if not usage_decreased:
            if (
                previous_duration is None
                or current_duration is None
                or previous_duration != current_duration
                or previous_after is None
                or current_after is None
                or current_after <= previous_after
                or current_after < current_duration - DIRECT_RESET_TRANSITION_MARGIN_SECONDS
            ):
                continue
            reset_seen = True
            continue
        if (
            previous_duration is None
            or current_duration is None
            or previous_duration != current_duration
            or previous_after is None
            or current_after is None
            or current_after <= previous_after
            or current_after < current_duration - DIRECT_RESET_TRANSITION_MARGIN_SECONDS
        ):
            return False
        reset_seen = True
    return reset_seen


def _rate_limit_window(payload: dict[str, Any], key: str) -> dict[str, Any] | None:
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None
    window = rate_limit.get(key)
    return window if isinstance(window, dict) else None


def _progressive_window_identity_is_stable(windows: list[tuple]) -> bool:
    durations = {window[0] for window in windows}
    identities = {window[2] for window in windows}
    return len(durations) == 1 and len(identities) == 1 and None not in identities


def _has_reset_regression(payloads: list[dict[str, Any]]) -> bool:
    signatures = [_usage_response_signature(payload) for payload in payloads]
    for window_index in range(2):
        windows = [signature[1][window_index] for signature in signatures]
        for previous, current in pairwise(windows):
            if previous is None or current is None:
                continue
            previous_identity = previous[2]
            current_identity = current[2]
            if (
                previous_identity is not None
                and current_identity is not None
                and previous_identity[0] == current_identity[0]
                and current_identity[1] < previous_identity[1]
            ):
                return True
            if (
                previous[0] is not None
                and current[0] is not None
                and previous[0] == current[0]
                and previous[1] is not None
                and current[1] is not None
                and current[1] < previous[1]
            ):
                # A fixed reset_at can survive the bucket transition. A lower
                # usage value is still a reset signal and must not lose to an
                # older majority when it is the last sample.
                return True
            if (
                len(previous) > 3
                and len(current) > 3
                and previous[3] != current[3]
                and current[3] == "fresh"
            ):
                # A reset can preserve used_percent (for example 0 -> 0).
                # The countdown jumping back to a fresh full window is then
                # the only observable transition.
                return True
    return False


def _signature_reset_identity(value: dict[str, Any]) -> tuple[str, int] | None:
    duration = _signature_number(value.get("limit_window_seconds"))
    reset_after = _signature_number(value.get("reset_after_seconds"))
    if duration is not None and reset_after is not None and reset_after >= 0:
        # Relative countdowns move on every poll, both before and after usage.
        # The fixed window duration identifies the window; usage regressions
        # remain guarded separately by _has_reset_regression.
        return ("after", int(duration // DIRECT_RESET_BUCKET_SECONDS))
    reset_at = _signature_reset(value.get("reset_at"))
    return None if reset_at is None else ("at", reset_at)


def _signature_relative_reset_phase(value: dict[str, Any]) -> str | None:
    duration = _signature_number(value.get("limit_window_seconds"))
    reset_after = _signature_number(value.get("reset_after_seconds"))
    if duration is None or reset_after is None or duration <= 0 or reset_after < 0:
        return None
    if reset_after <= DIRECT_RESET_TRANSITION_MARGIN_SECONDS:
        return "near-reset"
    if reset_after >= duration - DIRECT_RESET_TRANSITION_MARGIN_SECONDS:
        return "fresh"
    return None


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
        return datetime.fromtimestamp(float(exp), tz=UTC).astimezone(LOCAL_TZ)
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
        f"{expiry.astimezone(LOCAL_TZ).strftime('%d.%m.%Y %H:%M')}; "
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
