from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any


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
    browser: str = "firefox"
    auth_json_path: str | None = None
    backend: str = "direct"


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
        return any(
            value is not None
            for value in (self.used, self.remaining, self.percent)
        )

    @property
    def remaining_percent(self) -> float | None:
        if self.percent is not None:
            return self.percent
        if self.limit is not None and self.limit > 0 and self.remaining is not None:
            return max(0.0, min(100.0, self.remaining / self.limit * 100.0))
        return None


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
    def exhausted(self) -> bool:
        if not self.available or self.allowed is False or self.limit_reached is True:
            return True
        return any(window.remaining_percent == 0 for window in self.windows)

    def window_for_duration(self, duration_seconds: int) -> LimitWindow | None:
        return next(
            (
                window
                for window in self.windows
                if window.duration_seconds == duration_seconds
            ),
            None,
        )


@dataclass(frozen=True)
class AccountUsage:
    account_id: str
    label: str
    captured_at: datetime
    five_hour: LimitWindow | None = None
    weekly: LimitWindow | None = None
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
        return {
            "account": self.account_id,
            "label": self.label,
            "captured_at": self.captured_at.isoformat(),
            "five_hour": None
            if self.cache_invalidated
            else _window_to_dict(self.five_hour),
            "weekly": None if self.cache_invalidated else _window_to_dict(self.weekly),
            "main": None if self.cache_invalidated else _pool_to_dict(self.main),
            "models": {}
            if self.cache_invalidated
            else {pool.key: _pool_to_dict(pool) for pool in self.models},
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
            if self.values_captured_at and not self.cache_invalidated
            else None,
            "stale": self.stale,
            "cache_invalidated": self.cache_invalidated,
        }

    def model_pool(self, model: str) -> UsagePool | None:
        normalized = model.strip().casefold()
        return next(
            (pool for pool in self.models if pool.key.casefold() == normalized),
            None,
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
