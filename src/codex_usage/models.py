from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, cast

from .usage_resets import UsageResetState

_KNOWN_WINDOW_NAMES = frozenset(
    {
        "5h",
        "5_hour",
        "five_hour",
        "w",
        "week",
        "weekly",
        "30d",
        "30_day",
        "month",
        "monthly",
    }
)
_WINDOW_NAME_DURATIONS = {
    "5h": 18_000,
    "5_hour": 18_000,
    "five_hour": 18_000,
    "w": 604_800,
    "week": 604_800,
    "weekly": 604_800,
    "30d": 2_592_000,
    "30_day": 2_592_000,
    "month": 2_592_000,
    "monthly": 2_592_000,
}


def _canonical_window_name(duration: int) -> str:
    if duration % 86_400 == 0:
        return f"{duration // 86_400}d"
    if duration % 3_600 == 0:
        return f"{duration // 3_600}h"
    return f"{duration}s"


def _window_identity_key(window: LimitWindow) -> int | None:
    if not isinstance(window, LimitWindow) or not window.has_known_identity:
        return None
    if window.duration_seconds is not None:
        return window.duration_seconds
    return _WINDOW_NAME_DURATIONS.get(window.name.strip().casefold())


def _has_unique_window_identities(windows: tuple[LimitWindow, ...]) -> bool:
    try:
        identities = tuple(_window_identity_key(window) for window in windows)
        return (
            None not in identities
            and len(set(identities)) == len(identities)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _pool_has_usage_source(pool: UsagePool) -> bool:
    return isinstance(pool.availability_sources, tuple) and "usage" in pool.availability_sources


class AccountStatus(StrEnum):
    OK = "ok"
    LOGIN_REQUIRED = "login_required"
    PARTIAL = "partial"
    ERROR = "error"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Account:
    id: str
    label: str
    profile_dir: str
    tag: str = ""
    browser: str = "firefox"
    auth_json_path: str | None = None
    backend: str = "direct"
    reactivation_browser: str = "auto"
    series: str = ""
    series_active: bool = False
    auth_sync_required: bool = False


@dataclass(frozen=True)
class LimitWindow:
    name: str
    used: float | None = None
    limit: float | None = None
    remaining: float | None = None
    percent: float | None = None
    reset_at: datetime | None = None
    raw: str | None = None
    source: str = "unknown"
    duration_seconds: int | None = None

    @property
    def is_complete(self) -> bool:
        return self.used is not None and self.limit is not None and self.reset_at is not None

    @property
    def has_usage_value(self) -> bool:
        return not self.has_invalid_usage_value and self.remaining_percent is not None

    @property
    def has_known_identity(self) -> bool:
        if self.duration_seconds is not None:
            if (
                type(self.duration_seconds) is not int
                or self.duration_seconds <= 0
            ):
                return False
            if not isinstance(self.name, str):
                return False
            normalized_name = self.name.strip().casefold()
            if not normalized_name:
                return True
            expected_duration = _WINDOW_NAME_DURATIONS.get(normalized_name)
            if expected_duration is not None:
                return self.duration_seconds == expected_duration
            return normalized_name == _canonical_window_name(self.duration_seconds)
        if not isinstance(self.name, str):
            return False
        return self.name.strip().casefold() in _KNOWN_WINDOW_NAMES

    @property
    def remaining_percent(self) -> float | None:
        if self.has_invalid_usage_value:
            return None
        if any(
            value is not None and _finite_number(value) is None
            for value in (self.used, self.limit, self.remaining, self.percent)
        ):
            return None
        used = _finite_number(self.used)
        limit = _finite_number(self.limit)
        remaining = _finite_number(self.remaining)
        if used is not None and limit is not None:
            if limit <= 0 or used < 0:
                return None
            if used >= limit:
                return 0.0
            return _valid_percent((limit - used) / limit * 100.0)
        if limit is not None and remaining is not None and limit > 0:
            if not 0 <= remaining <= limit:
                return None
            return _valid_percent(remaining / limit * 100.0)
        if remaining is not None and 0 <= remaining <= 100:
            if self.percent is None:
                return remaining
            explicit_percent = _valid_percent(self.percent)
            if explicit_percent is not None and abs(remaining - explicit_percent) < 0.01:
                return explicit_percent
            return None
        if self.percent is not None:
            return _valid_percent(self.percent)
        return None

    @property
    def has_invalid_usage_value(self) -> bool:
        limit = _finite_number(self.limit)
        used = _finite_number(self.used)
        remaining = _finite_number(self.remaining)
        percent = _valid_percent(self.percent) if self.percent is not None else None
        if any(
            value is not None and _finite_number(value) is None
            for value in (self.used, self.limit, self.remaining)
        ):
            return True
        if self.percent is not None and percent is None:
            return True
        if used is not None and used < 0:
            return True
        if self.limit is not None and (limit is None or limit <= 0):
            return True
        if self.remaining is not None and remaining is not None and remaining < 0:
            return True
        if self.remaining is not None and remaining is None:
            return True
        if (
            limit is None
            and remaining is not None
            and remaining > 100
            and self.percent is None
        ):
            return True
        return bool(
            limit is not None
            and remaining is not None
            and not 0 <= remaining <= limit
        )


@dataclass(frozen=True)
class UsagePool:
    key: str
    display_name: str
    windows: tuple[LimitWindow, ...] = field(default_factory=tuple)
    available: bool = True
    allowed: bool | None = None
    limit_reached: bool | None = None
    metered_feature: str | None = None
    availability_sources: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_valid_usage(self) -> bool:
        try:
            return bool(
                self.available is True
                and (self.allowed is None or isinstance(self.allowed, bool))
                and (
                    self.limit_reached is None
                    or isinstance(self.limit_reached, bool)
                )
                and isinstance(self.windows, tuple)
                and self.windows
                and all(
                    isinstance(window, LimitWindow)
                    and window.has_known_identity
                    and window.has_usage_value
                    for window in self.windows
                )
                and _has_unique_window_identities(self.windows)
            )
        except (AttributeError, TypeError, ValueError):
            return False

    @property
    def exhausted(self) -> bool:
        if not isinstance(self.windows, tuple):
            return True
        if _pool_has_usage_source(self) and (
            not self.windows
            or any(
                not isinstance(window, LimitWindow) or not window.has_usage_value
                for window in self.windows
            )
        ):
            return True
        if not self.windows:
            return (
                self.available is not True
                or (self.allowed is not None and not isinstance(self.allowed, bool))
                or (
                    self.limit_reached is not None
                    and not isinstance(self.limit_reached, bool)
                )
                or self.allowed is False
                or self.limit_reached is True
            )
        if (
            self.available is not True
            or (self.allowed is not None and not isinstance(self.allowed, bool))
            or (
                self.limit_reached is not None
                and not isinstance(self.limit_reached, bool)
            )
            or self.allowed is False
            or self.limit_reached is True
        ):
            return True
        if not _has_unique_window_identities(self.windows):
            return True
        try:
            return any(
                not isinstance(window, LimitWindow)
                or window.has_invalid_usage_value
                or window.remaining_percent == 0
                for window in self.windows
            )
        except (AttributeError, TypeError, ValueError):
            return True

    def window_for_duration(self, duration_seconds: int) -> LimitWindow | None:
        if (
            type(duration_seconds) is not int
            or duration_seconds <= 0
            or
            not isinstance(self.windows, tuple)
            or not self.windows
            or any(not isinstance(window, LimitWindow) for window in self.windows)
            or not _has_unique_window_identities(self.windows)
        ):
            return None
        matches = tuple(
            window
            for window in self.windows
            if _window_identity_key(window) == duration_seconds
        )
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class AccountUsage:
    account_id: str
    label: str
    captured_at: datetime
    five_hour: LimitWindow | None = None
    weekly: LimitWindow | None = None
    credits: LimitWindow | None = None
    main: UsagePool | None = None
    models: tuple[UsagePool, ...] = field(default_factory=tuple)
    status: AccountStatus = AccountStatus.OK
    error: str | None = None
    blocked_until: datetime | None = None
    blocked_reason: str | None = None
    auth_last_refresh: datetime | None = None
    auth_access_expires_at: datetime | None = None
    auth_id_expires_at: datetime | None = None
    source_urls: tuple[str, ...] = field(default_factory=tuple)
    backend_configured: str | None = None
    backend_used: str | None = None
    backend_user_id: str | None = None
    backend_account_id: str | None = None
    fallback_reason: str | None = None
    values_captured_at: datetime | None = None
    stale: bool = False
    cache_invalidated: bool = False
    usage_resets: UsageResetState = field(
        default_factory=lambda: UsageResetState(None, False, False)
    )
    # Internal fetch generation; it prevents an in-flight pre-reconfiguration
    # result from recreating state after the account was reset.
    state_generation: int | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.main is not None:
            return
        windows = tuple(
            replace(window, duration_seconds=duration)
            if window.duration_seconds is None
            else window
            for window, duration in (
                (self.five_hour, 18_000),
                (self.weekly, 604_800),
            )
            if isinstance(window, LimitWindow)
        )
        if not windows:
            return
        object.__setattr__(
            self,
            "main",
            UsagePool(
                key="main",
                display_name="Codex",
                windows=windows,
                availability_sources=("legacy_fields",),
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        status = self.status if isinstance(self.status, AccountStatus) else AccountStatus.ERROR
        terminal_status = status in {
            AccountStatus.ERROR,
            AccountStatus.LOGIN_REQUIRED,
        }
        cache_invalidated = (
            self.cache_invalidated if isinstance(self.cache_invalidated, bool) else True
        )
        stale = self.stale if isinstance(self.stale, bool) else True
        values_hidden = cache_invalidated or terminal_status
        serialized_models: dict[str, dict[str, Any] | None] = {}
        if not values_hidden and isinstance(self.models, tuple):
            serialized_model_keys: dict[str, str] = {}
            ambiguous_model_keys: set[str] = set()
            for pool in self.models:
                if not isinstance(pool, UsagePool) or not isinstance(pool.key, str):
                    continue
                normalized_key = pool.key.casefold()
                if normalized_key in ambiguous_model_keys:
                    continue
                previous_key = serialized_model_keys.get(normalized_key)
                if previous_key is not None:
                    serialized_models.pop(previous_key, None)
                    serialized_model_keys.pop(normalized_key, None)
                    ambiguous_model_keys.add(normalized_key)
                    continue
                serialized_models[pool.key] = _pool_to_dict(pool)
                serialized_model_keys[normalized_key] = pool.key
        serialized_source_urls = (
            [url for url in self.source_urls if isinstance(url, str)]
            if isinstance(self.source_urls, tuple)
            else []
        )
        serialized_usage_resets = (
            self.usage_resets.as_dict()
            if isinstance(self.usage_resets, UsageResetState)
            else UsageResetState(None, False, False).as_dict()
        )
        return {
            "account": _safe_text(self.account_id),
            "label": _safe_text(self.label),
            "captured_at": _isoformat(self.captured_at),
            "five_hour": None
            if values_hidden
            else _window_to_dict(self.five_hour),
            "weekly": None if values_hidden else _window_to_dict(self.weekly),
            "credits": None if values_hidden else _window_to_dict(self.credits),
            "main": None if values_hidden else _pool_to_dict(self.main),
            "models": serialized_models,
            "status": status.value,
            "error": _safe_text(self.error),
            "blocked_until": _isoformat(self.blocked_until),
            "blocked_reason": _safe_text(self.blocked_reason),
            "auth_last_refresh": _isoformat(self.auth_last_refresh),
            "auth_access_expires_at": _isoformat(self.auth_access_expires_at),
            "auth_id_expires_at": _isoformat(self.auth_id_expires_at),
            "source_urls": serialized_source_urls,
            "backend_configured": _safe_text(self.backend_configured),
            "backend_used": _safe_text(self.backend_used),
            "backend_user_id": _safe_text(self.backend_user_id),
            "backend_account_id": _safe_text(self.backend_account_id),
            "fallback_reason": _safe_text(self.fallback_reason),
            "values_captured_at": (
                _isoformat(self.values_captured_at) if not values_hidden else None
            ),
            "stale": stale or terminal_status,
            "cache_invalidated": cache_invalidated or terminal_status,
            "usage_resets": serialized_usage_resets,
        }

    def model_pool(self, model: str) -> UsagePool | None:
        if not isinstance(model, str):
            return None
        normalized = model.strip().casefold()
        if not normalized or not isinstance(self.models, tuple):
            return None
        matches: list[UsagePool] = []
        for pool in self.models:
            if not isinstance(pool, UsagePool) or not isinstance(pool.key, str):
                return None
            if pool.key.casefold() == normalized:
                matches.append(pool)
        return (
            matches[0]
            if len(matches) == 1 and matches[0].key == model
            else None
        )


def _window_to_dict(window: LimitWindow | None) -> dict[str, Any] | None:
    if not isinstance(window, LimitWindow):
        return None
    return {
        "name": _safe_text(window.name),
        "duration_seconds": _safe_int(window.duration_seconds),
        "used": _safe_number(window.used),
        "limit": _safe_number(window.limit),
        "remaining": _safe_number(window.remaining),
        "percent": _safe_number(window.percent),
        "reset_at": _isoformat(window.reset_at),
        "raw": _safe_text(window.raw),
        "source": _safe_text(window.source),
    }


def _pool_to_dict(pool: UsagePool | None) -> dict[str, Any] | None:
    if not isinstance(pool, UsagePool):
        return None
    windows = pool.windows if isinstance(pool.windows, tuple) else ()
    availability_sources = (
        [source for source in pool.availability_sources if isinstance(source, str)]
        if isinstance(pool.availability_sources, tuple)
        else []
    )
    try:
        exhausted = pool.exhausted
    except (AttributeError, TypeError, ValueError):
        exhausted = True
    return {
        "key": _safe_text(pool.key),
        "display_name": _safe_text(pool.display_name),
        "windows": [_window_to_dict(window) for window in windows],
        "available": pool.available if isinstance(pool.available, bool) else None,
        "allowed": pool.allowed if isinstance(pool.allowed, (bool, type(None))) else None,
        "limit_reached": (
            pool.limit_reached
            if isinstance(pool.limit_reached, (bool, type(None)))
            else None
        ),
        "metered_feature": _safe_text(pool.metered_feature),
        "availability_sources": availability_sources,
        "exhausted": exhausted if isinstance(exhausted, bool) else True,
    }


def _isoformat(value: object) -> str | None:
    if not isinstance(value, datetime):
        return None
    try:
        return value.isoformat()
    except Exception:
        return None


def _safe_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _safe_int(value: object) -> int | None:
    return value if type(value) is int else None


def _safe_number(value: object) -> int | float | None:
    if type(value) not in (int, float):
        return None
    return cast(int | float, value) if _finite_number(value) is not None else None


def _finite_number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _valid_percent(value: Any) -> float | None:
    number = _finite_number(value)
    return number if number is not None and 0 <= number <= 100 else None


def credit_values_match(left: Any, right: Any) -> bool:
    left_number = _finite_number(left)
    right_number = _finite_number(right)
    if left_number is None or right_number is None:
        return False
    try:
        lower, upper = sorted((left_number, right_number))
        for _ in range(4):
            if lower == upper:
                return True
            lower = math.nextafter(lower, upper)
        return lower == upper
    except (OverflowError, TypeError, ValueError):
        return False


def credit_window_remaining_percent(window: LimitWindow) -> float | None:
    """Return explicit/denominated credit percent; absolute balances return None."""
    if not isinstance(window, LimitWindow):
        raise ValueError("credit window is invalid")
    values: dict[str, float | None] = {}
    for field_name in ("used", "limit", "remaining", "percent"):
        raw_value = getattr(window, field_name, None)
        if raw_value is None:
            values[field_name] = None
            continue
        value = _finite_number(raw_value)
        if value is None or value < 0:
            raise ValueError("credit window is invalid")
        values[field_name] = value
    used = values["used"]
    limit = values["limit"]
    remaining = values["remaining"]
    percent = values["percent"]
    if percent is not None and percent > 100:
        raise ValueError("credit window is invalid")
    if limit is None:
        if percent is None:
            if remaining is None or used is not None:
                raise ValueError("credit window is invalid")
            return None
        if used is not None or (
            remaining is not None and not credit_values_match(remaining, percent)
        ):
            raise ValueError("credit window is invalid")
        return percent
    if limit <= 0 or (used is None and remaining is None and percent is None):
        raise ValueError("credit window is invalid")

    derived: list[float] = []
    if used is not None:
        if used > limit:
            if not credit_values_match(used, limit):
                raise ValueError("credit window is invalid")
            used = limit
        derived.append((limit - used) / limit * 100.0)
    if remaining is not None:
        if remaining > limit:
            if not credit_values_match(remaining, limit):
                raise ValueError("credit window is invalid")
            remaining = limit
        derived.append(remaining / limit * 100.0)
    if used is not None and remaining is not None:
        try:
            total = used + remaining
        except (OverflowError, TypeError, ValueError):
            raise ValueError("credit window is invalid") from None
        if not credit_values_match(total, limit):
            raise ValueError("credit window is invalid")
    if percent is not None:
        if any(not credit_values_match(value, percent) for value in derived):
            raise ValueError("credit window is invalid")
        return percent
    if not derived or any(
        not credit_values_match(value, derived[0]) for value in derived[1:]
    ):
        raise ValueError("credit window is invalid")
    return derived[0]
