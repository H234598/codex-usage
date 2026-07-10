from __future__ import annotations

from dataclasses import dataclass, field
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

    @property
    def is_complete(self) -> bool:
        return self.used is not None and self.limit is not None and self.reset_at is not None


@dataclass(frozen=True)
class AccountUsage:
    account_id: str
    label: str
    captured_at: datetime
    five_hour: LimitWindow | None = None
    weekly: LimitWindow | None = None
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
    fallback_reason: str | None = None
    values_captured_at: datetime | None = None
    stale: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "account": self.account_id,
            "label": self.label,
            "captured_at": self.captured_at.isoformat(),
            "five_hour": _window_to_dict(self.five_hour),
            "weekly": _window_to_dict(self.weekly),
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
            "fallback_reason": self.fallback_reason,
            "values_captured_at": self.values_captured_at.isoformat()
            if self.values_captured_at
            else None,
            "stale": self.stale,
        }


def _window_to_dict(window: LimitWindow | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {
        "name": window.name,
        "used": window.used,
        "limit": window.limit,
        "remaining": window.remaining,
        "percent": window.percent,
        "reset_at": window.reset_at.isoformat() if window.reset_at else None,
        "raw": window.raw,
        "source": window.source,
    }
