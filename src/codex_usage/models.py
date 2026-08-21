from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

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
                not isinstance(self.duration_seconds, int)
                or isinstance(self.duration_seconds, bool)
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
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, int)
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
        if self.main is not None or not (self.five_hour or self.weekly):
            return
        windows = tuple(
            replace(window, duration_seconds=duration)
            if window.duration_seconds is None
            else window
            for window, duration in (
                (self.five_hour, 18_000),
                (self.weekly, 604_800),
            )
            if window is not None
        )
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
        terminal_status = self.status in {
            AccountStatus.ERROR,
            AccountStatus.LOGIN_REQUIRED,
        }
        values_hidden = self.cache_invalidated or terminal_status
        serialized_models = (
            {
                pool.key: _pool_to_dict(pool)
                for pool in self.models
                if isinstance(pool, UsagePool) and isinstance(pool.key, str)
            }
            if not values_hidden and isinstance(self.models, tuple)
            else {}
        )
        return {
            "account": self.account_id,
            "label": self.label,
            "captured_at": self.captured_at.isoformat(),
            "five_hour": None
            if values_hidden
            else _window_to_dict(self.five_hour),
            "weekly": None if values_hidden else _window_to_dict(self.weekly),
            "credits": None if values_hidden else _window_to_dict(self.credits),
            "main": None if values_hidden else _pool_to_dict(self.main),
            "models": serialized_models,
            "status": self.status.value,
            "error": self.error,
            "blocked_until": self.blocked_until.isoformat() if self.blocked_until else None,
            "blocked_reason": self.blocked_reason,
            "auth_last_refresh": self.auth_last_refresh.isoformat()
            if self.auth_last_refresh
            else None,
            "auth_access_expires_at": self.auth_access_expires_at.isoformat()
            if self.auth_access_expires_at
            else None,
            "auth_id_expires_at": self.auth_id_expires_at.isoformat()
            if self.auth_id_expires_at
            else None,
            "source_urls": list(self.source_urls),
            "backend_configured": self.backend_configured,
            "backend_used": self.backend_used,
            "backend_user_id": self.backend_user_id,
            "backend_account_id": self.backend_account_id,
            "fallback_reason": self.fallback_reason,
            "values_captured_at": self.values_captured_at.isoformat()
            if self.values_captured_at and not values_hidden
            else None,
            "stale": self.stale or terminal_status,
            "cache_invalidated": self.cache_invalidated or terminal_status,
            "usage_resets": self.usage_resets.as_dict(),
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
    if window is None:
        return None
    return {
        "name": window.name,
        "duration_seconds": window.duration_seconds,
        "used": window.used,
        "limit": window.limit,
        "remaining": window.remaining,
        "percent": window.percent,
        "reset_at": window.reset_at.isoformat() if window.reset_at else None,
        "raw": window.raw,
        "source": window.source,
    }


def _pool_to_dict(pool: UsagePool | None) -> dict[str, Any] | None:
    if pool is None:
        return None
    return {
        "key": pool.key,
        "display_name": pool.display_name,
        "windows": [_window_to_dict(window) for window in pool.windows],
        "available": pool.available,
        "allowed": pool.allowed,
        "limit_reached": pool.limit_reached,
        "metered_feature": pool.metered_feature,
        "availability_sources": list(pool.availability_sources),
        "exhausted": pool.exhausted,
    }


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _valid_percent(value: Any) -> float | None:
    number = _finite_number(value)
    return number if number is not None and 0 <= number <= 100 else None
