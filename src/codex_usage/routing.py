from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import default_state_dir
from .json_utils import loads_strict
from .models import AccountStatus, AccountUsage, UsagePool
from .private_io import (
    assert_no_symlink_ancestors,
    private_path_lock,
    read_private_text,
    write_private_text,
)
from .spark_health import spark_health_status
from .usage_limits import SPARK_MODEL

POLICY_SCHEMA_VERSION = 1
DECISION_SCHEMA_VERSION = 1
MAIN_MODEL = "gpt-5.4-mini"
MAIN_MINIMUM_REMAINING_PERCENT = 10.0
DEFAULT_MAX_USAGE_AGE_SECONDS = 600
MAX_POLICY_BYTES = 64 * 1024
POLICY_SCOPES = ("account", "group", "agent", "job")
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_.:@+-]{1,128}")
EXEMPT_ROLES = frozenset(
    ("teamleiterin", "teamlead", "leader", "manager", "master", "admin")
)


def default_policy_path() -> Path:
    return default_state_dir() / "routing-policy.json"


def load_policy(path: Path | None = None) -> dict[str, Any]:
    policy_path = path or default_policy_path()
    if not policy_path.exists():
        if policy_path.is_symlink():
            raise ValueError("routing policy must be a regular file")
        return _empty_policy()
    text, file_stat = read_private_text(
        policy_path,
        regular_label="routing policy",
        read_label="routing policy",
        max_bytes=MAX_POLICY_BYTES,
    )
    if file_stat.st_nlink != 1 or file_stat.st_mode & 0o077:
        raise ValueError("routing policy permissions must be 0600")
    try:
        payload = loads_strict(text)
    except ValueError as exc:
        raise ValueError("routing policy is invalid JSON") from exc
    return _validate_policy(payload)


def set_policy_rule(
    scope: str,
    identifier: str | None,
    value: bool | None,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    normalized_scope = scope.strip().casefold()
    if normalized_scope not in ("global", *POLICY_SCOPES):
        raise ValueError("policy scope must be global, account, group, agent or job")
    if normalized_scope == "global":
        if identifier not in (None, ""):
            raise ValueError("global policy does not accept an identifier")
    else:
        identifier = _validate_identifier(identifier)
    policy_path = path or default_policy_path()
    _prepare_private_directory(policy_path.parent)
    with private_path_lock(policy_path, label="routing policy lock"):
        policy = load_policy(policy_path)
        if normalized_scope == "global":
            policy["global"] = bool(value) if value is not None else False
        elif value is None:
            policy[normalized_scope].pop(identifier, None)
        else:
            policy[normalized_scope][identifier] = value
        text = json.dumps(policy, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        if len(text.encode("utf-8")) > MAX_POLICY_BYTES:
            raise ValueError("routing policy is too large")
        write_private_text(policy_path, text, label="routing policy")
    return policy


def effective_paid_overage(
    policy: dict[str, Any],
    *,
    account: str,
    group: str | None = None,
    agent: str | None = None,
    job: str | None = None,
) -> tuple[bool, str]:
    account = _validate_identifier(account)
    group = _validate_optional_identifier(group)
    agent = _validate_optional_identifier(agent)
    job = _validate_optional_identifier(job)
    context = (
        ("job", job),
        ("group", group),
        ("agent", agent),
        ("account", account),
    )
    for scope, identifier in context:
        if identifier is not None and identifier in policy[scope]:
            return policy[scope][identifier], f"{scope}:{identifier}"
    return policy["global"], "global"


def evaluate_routing(
    usage: AccountUsage,
    *,
    role: str,
    paid_overage_allowed: bool,
    policy_source: str = "global",
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_USAGE_AGE_SECONDS,
    spark_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checked_at = now or datetime.now(tz=UTC)
    normalized_role = role.strip().casefold()
    base = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "account": usage.account_id,
        "backend_account_id": usage.backend_account_id,
        "role": role,
        "checked_at": checked_at.isoformat(),
        "captured_at": usage.captured_at.isoformat(),
        "paid_overage_allowed": paid_overage_allowed,
        "policy_source": policy_source,
        "threshold_percent": MAIN_MINIMUM_REMAINING_PERCENT,
    }
    if normalized_role in EXEMPT_ROLES:
        return {
            **base,
            "decision": "unchanged",
            "model": None,
            "reason": "role_exempt",
            "usage_state": "not_applicable",
        }
    invalid_reason = _invalid_usage_reason(
        usage, now=checked_at, max_age_seconds=max_age_seconds
    )
    if invalid_reason:
        return _blocked(base, invalid_reason, usage_state="unknown")

    spark_health = spark_health or spark_health_status(
        usage.backend_account_id or usage.account_id,
        now=checked_at,
    )
    base["spark_health"] = spark_health
    spark = usage.model_pool(SPARK_MODEL)
    spark_health_state = spark_health.get("state")
    if (
        spark is not None
        and spark.available
        and not spark.exhausted
        and spark_health_state == "healthy"
    ):
        spark_state = _pool_usage_state(spark)
        return {
            **base,
            "decision": "spark",
            "model": SPARK_MODEL,
            "reason": "spark_available",
            "usage_state": spark_state,
            "resets": _pool_resets(spark),
        }

    spark_reason = "spark_unavailable_or_exhausted"
    if spark is not None and spark.available and not spark.exhausted:
        spark_reason = (
            "spark_health_failed"
            if spark_health_state == "failed"
            else "spark_health_unverified"
        )
    main_state, main_remaining = _main_state(usage.main)
    if main_state == "safe":
        return {
            **base,
            "decision": "main",
            "model": MAIN_MODEL,
            "reason": spark_reason,
            "usage_state": "known",
            "remaining": main_remaining,
            "resets": _pool_resets(usage.main),
        }
    if main_state == "low" and paid_overage_allowed:
        return {
            **base,
            "decision": "credits",
            "model": MAIN_MODEL,
            "reason": "paid_overage_explicitly_allowed",
            "usage_state": "known",
            "remaining": main_remaining,
            "resets": _pool_resets(usage.main),
        }
    if main_state == "low":
        return _blocked(
            base,
            "main_limit_at_or_below_threshold",
            usage_state="known",
            remaining=main_remaining,
            resets=_pool_resets(usage.main),
        )
    return _blocked(base, "main_limit_unknown", usage_state="unknown")


def _main_state(pool: UsagePool | None) -> tuple[str, dict[str, float]]:
    if pool is None or not pool.available or not pool.windows:
        return "unknown", {}
    remaining: dict[str, float] = {}
    for window in pool.windows:
        value = window.remaining_percent
        if value is None:
            return "unknown", {}
        remaining[window.name] = value
    if pool.allowed is False or pool.limit_reached is True:
        return "low", remaining
    if all(value > MAIN_MINIMUM_REMAINING_PERCENT for value in remaining.values()):
        return "safe", remaining
    return "low", remaining


def _invalid_usage_reason(
    usage: AccountUsage, *, now: datetime, max_age_seconds: int
) -> str | None:
    if isinstance(max_age_seconds, bool) or max_age_seconds < 60:
        raise ValueError("max_age_seconds must be at least 60")
    if usage.cache_invalidated:
        return "cache_invalidated"
    if usage.stale:
        return "usage_stale"
    if usage.status not in (AccountStatus.OK, AccountStatus.PARTIAL):
        return f"usage_status_{usage.status.value}"
    captured_at = usage.values_captured_at or usage.captured_at
    try:
        age = (now.astimezone(UTC) - captured_at.astimezone(UTC)).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return "usage_timestamp_invalid"
    if age < -300:
        return "usage_timestamp_in_future"
    if age > max_age_seconds:
        return "usage_too_old"
    return None


def _pool_usage_state(pool: UsagePool) -> str:
    if not pool.windows:
        return "unknown"
    return (
        "known"
        if all(window.remaining_percent is not None for window in pool.windows)
        else "unknown"
    )


def _pool_resets(pool: UsagePool | None) -> dict[str, str | None]:
    if pool is None:
        return {}
    return {
        window.name: window.reset_at.isoformat() if window.reset_at else None
        for window in pool.windows
    }


def _blocked(
    base: dict[str, Any],
    reason: str,
    *,
    usage_state: str,
    remaining: dict[str, float] | None = None,
    resets: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    result = {
        **base,
        "decision": "blocked",
        "model": None,
        "reason": reason,
        "usage_state": usage_state,
    }
    if remaining is not None:
        result["remaining"] = remaining
    if resets is not None:
        result["resets"] = resets
    return result


def _empty_policy() -> dict[str, Any]:
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "global": False,
        **{scope: {} for scope in POLICY_SCOPES},
    }


def _validate_policy(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ValueError("unsupported routing policy schema")
    if not isinstance(payload.get("global"), bool):
        raise ValueError("routing policy global value is invalid")
    result = _empty_policy()
    result["global"] = payload["global"]
    for scope in POLICY_SCOPES:
        rules = payload.get(scope)
        if not isinstance(rules, dict) or len(rules) > 500:
            raise ValueError(f"routing policy {scope} rules are invalid")
        for identifier, value in rules.items():
            normalized = _validate_identifier(identifier)
            if not isinstance(value, bool):
                raise ValueError(f"routing policy {scope} value is invalid")
            result[scope][normalized] = value
    return result


def _validate_identifier(value: Any) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise ValueError("policy identifier is invalid")
    return value


def _validate_optional_identifier(value: Any) -> str | None:
    return None if value is None else _validate_identifier(value)


def _prepare_private_directory(path: Path) -> None:
    assert_no_symlink_ancestors(path, label="routing policy directory")
    if path.is_symlink():
        raise ValueError("routing policy directory must be a real directory")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("routing policy directory must be a real directory")
    os.chmod(path, stat.S_IRWXU)
