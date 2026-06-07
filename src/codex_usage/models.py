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


@dataclass(frozen=True)
class Account:
    id: str
    label: str
    profile_dir: str


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
    source_urls: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "account": self.account_id,
            "label": self.label,
            "captured_at": self.captured_at.isoformat(),
            "five_hour": _window_to_dict(self.five_hour),
            "weekly": _window_to_dict(self.weekly),
            "status": self.status.value,
            "error": self.error,
            "source_urls": list(self.source_urls),
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
