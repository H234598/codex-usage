from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import default_state_dir
from .identity import MAX_BACKEND_ID_CHARS
from .json_utils import loads_strict
from .models import AccountStatus, AccountUsage, LimitWindow, UsagePool
from .private_io import (
    ensure_private_directory,
    private_path_lock,
    read_private_text,
    write_private_text,
)
from .spark_health import SPARK_HEALTH_MAX_AGE_SECONDS, spark_health_status
from .state import backend_provenance_matches_configured
from .usage_limits import (
    FIVE_HOUR_SECONDS,
    SPARK_MODEL,
    WEEKLY_SECONDS,
)

POLICY_SCHEMA_VERSION = 1
DECISION_SCHEMA_VERSION = 1
MAIN_MODEL = "gpt-5.4-mini"
MAIN_MINIMUM_REMAINING_PERCENT = 10.0
DEFAULT_MAX_USAGE_AGE_SECONDS = 600
MAX_RESET_FUTURE_SKEW_SECONDS = 5 * 60
THIRTY_DAY_SECONDS = 30 * 24 * 60 * 60
WINDOW_NAME_DURATIONS = {
    "5h": FIVE_HOUR_SECONDS,
    "5_hour": FIVE_HOUR_SECONDS,
    "five_hour": FIVE_HOUR_SECONDS,
    "w": WEEKLY_SECONDS,
    "week": WEEKLY_SECONDS,
    "weekly": WEEKLY_SECONDS,
    "30d": THIRTY_DAY_SECONDS,
    "30_day": THIRTY_DAY_SECONDS,
    "month": THIRTY_DAY_SECONDS,
    "monthly": THIRTY_DAY_SECONDS,
}
SUPPORTED_WINDOW_SECONDS = frozenset(WINDOW_NAME_DURATIONS.values())
MAX_POLICY_BYTES = 64 * 1024
POLICY_SCOPES = ("account", "group", "agent", "job")
CREDIT_LIMIT_KEYS = ("hourly", "weekly", "monthly")
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
    if value is not None and not isinstance(value, bool):
        raise ValueError("policy value must be a boolean or None")
    if not isinstance(scope, str):
        raise ValueError("policy scope must be global, account, group, agent or job")
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


def set_credit_limits(
    limits: dict[str, float | None], *, scope: str = "global",
    identifier: str | None = None, path: Path | None = None
) -> dict[str, Any]:
    """Persist global or scoped hourly, weekly and monthly paid-credit caps.

    Scoped zero values mean "inherit the global value".  Global zero remains
    the existing disabled-cap value for backwards compatibility.
    """
    normalized = _validate_credit_limits(limits)
    if not isinstance(scope, str):
        raise ValueError("credit limit scope must be global, account, group, agent or job")
    normalized_scope = scope.strip().casefold()
    if normalized_scope not in ("global", *POLICY_SCOPES):
        raise ValueError("credit limit scope must be global, account, group, agent or job")
    if normalized_scope == "global":
        if identifier not in (None, ""):
            raise ValueError("global credit limits do not accept an identifier")
    else:
        identifier = _validate_identifier(identifier)
        normalized = {
            key: (None if value in (None, 0) else value)
            for key, value in normalized.items()
        }
    policy_path = path or default_policy_path()
    _prepare_private_directory(policy_path.parent)
    with private_path_lock(policy_path, label="routing policy lock"):
        policy = load_policy(policy_path)
        if normalized_scope == "global":
            policy["credit_limits"] = normalized
        elif all(value is None for value in normalized.values()):
            policy["credit_limit_overrides"][normalized_scope].pop(identifier, None)
        else:
            policy["credit_limit_overrides"][normalized_scope][identifier] = normalized
        text = json.dumps(policy, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        if len(text.encode("utf-8")) > MAX_POLICY_BYTES:
            raise ValueError("routing policy is too large")
        write_private_text(policy_path, text, label="routing policy")
    return policy


def effective_credit_limits(
    policy: dict[str, Any], *, account: str, group: str | None = None,
    agent: str | None = None, job: str | None = None,
) -> tuple[dict[str, float | None], str]:
    """Resolve each credit cap from the most specific matching scope."""
    account = _validate_identifier(account)
    group = _validate_optional_identifier(group)
    agent = _validate_optional_identifier(agent)
    job = _validate_optional_identifier(job)
    result = dict(policy.get("credit_limits", {}))
    resolved: set[str] = set()
    sources = []
    context = (
        ("job", job), ("group", group), ("agent", agent), ("account", account)
    )
    overrides = policy.get("credit_limit_overrides", {})
    for scope, identifier in context:
        if identifier is None:
            continue
        override = overrides.get(scope, {}).get(identifier)
        if not override:
            continue
        for key in CREDIT_LIMIT_KEYS:
            if key not in resolved and override.get(key) is not None:
                result[key] = override[key]
                resolved.add(key)
        sources.append(f"{scope}:{identifier}")
    return result, (sources[0] if sources else "global")


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
    if not isinstance(role, str) or not role.strip():
        raise ValueError("role must be a non-empty string")
    if not isinstance(paid_overage_allowed, bool):
        raise ValueError("paid_overage_allowed must be a boolean")
    checked_at = now if now is not None else datetime.now(tz=UTC)
    normalized_role = role.strip().casefold()
    base = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "account": usage.account_id,
        "backend_account_id": usage.backend_account_id,
        "role": role,
        "checked_at": _timestamp_text(checked_at),
        "captured_at": _timestamp_text(usage.captured_at),
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

    if spark_health is None:
        spark_health = spark_health_status(
            usage.backend_account_id or usage.account_id,
            now=checked_at,
        )
    elif not isinstance(spark_health, dict):
        spark_health = {
            "state": "unknown",
            "reason": "invalid_spark_health",
            "checked_at": None,
            "stale": False,
        }
    base["spark_health"] = spark_health
    spark = usage.model_pool(SPARK_MODEL)
    spark_has_usage_evidence = _pool_has_usage_evidence(spark)
    spark_has_non_usage_source = _pool_has_non_usage_source(spark)
    spark_health_state = spark_health.get("state")
    spark_state = (
        _pool_usage_state(spark, now=checked_at)
        if spark is not None
        else "unknown"
    )
    spark_health_checked_at_age = _spark_health_age_seconds(
        spark_health,
        now=checked_at,
    )
    spark_health_fresh = _spark_health_is_fresh(spark_health, now=checked_at)
    if (
        spark is not None
        and spark.available
        and spark_has_usage_evidence
        and not spark.exhausted
        and spark_state == "known"
        and spark_health_fresh
    ):
        return {
            **base,
            "decision": "spark",
            "model": SPARK_MODEL,
            "reason": "spark_available",
            "usage_state": spark_state,
            "resets": _pool_resets(spark),
        }

    spark_reason = "spark_unavailable_or_exhausted"
    if spark is not None and spark.available:
        if spark_has_non_usage_source:
            spark_reason = "spark_usage_unknown"
        elif spark_state == "invalid":
            spark_reason = "spark_usage_invalid"
        elif spark_state != "known":
            spark_reason = "spark_usage_unknown"
        elif not spark.exhausted:
            if spark_health_state == "failed":
                spark_reason = (
                    "spark_health_failed"
                    if spark_health.get("stale") is False
                    and spark_health_checked_at_age is not None
                    else "spark_health_unverified"
                )
            elif (
                spark_health_state == "healthy"
                and spark_health.get("stale") is True
            ):
                spark_reason = "spark_health_stale"
            else:
                spark_reason = "spark_health_unverified"
    main_state, main_remaining = _main_state(usage.main, now=checked_at)
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
        if usage.status is not AccountStatus.OK:
            return _blocked(base, "usage_incomplete", usage_state="unknown")
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


def _main_state(
    pool: UsagePool | None,
    *,
    now: datetime,
) -> tuple[str, dict[str, float]]:
    if (
        not isinstance(pool, UsagePool)
        or not _pool_flags_are_valid(pool)
        or pool.key != "main"
        or not pool.available
        or not _pool_has_usage_evidence(pool)
        or not isinstance(pool.windows, tuple)
        or not pool.windows
        or any(not isinstance(window, LimitWindow) for window in pool.windows)
    ):
        return "unknown", {}
    remaining: dict[str, float] = {}
    identities: set[int] = set()
    for window in pool.windows:
        identity = _window_identity_key(window)
        if identity is None or identity in identities:
            return "unknown", {}
        identities.add(identity)
        if not _window_reset_is_current(window, now=now):
            return "unknown", {}
        if window.has_invalid_usage_value:
            return "unknown", {}
        value = window.remaining_percent
        if value is None:
            return "unknown", {}
        if not _valid_remaining_percent(value):
            return "unknown", {}
        if window.name in remaining:
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
    if (
        not isinstance(max_age_seconds, int)
        or isinstance(max_age_seconds, bool)
        or max_age_seconds < 60
    ):
        raise ValueError("max_age_seconds must be a finite integer of at least 60")
    if not isinstance(usage.stale, bool) or not isinstance(usage.cache_invalidated, bool):
        return "usage_metadata_invalid"
    if usage.cache_invalidated:
        return "cache_invalidated"
    if usage.stale:
        return "usage_stale"
    if not isinstance(usage.status, AccountStatus):
        return "usage_status_invalid"
    if usage.status not in (AccountStatus.OK, AccountStatus.PARTIAL):
        return f"usage_status_{usage.status.value}"
    if usage.status == AccountStatus.OK and usage.error is not None:
        return "usage_error"
    if not isinstance(usage.backend_configured, str) or not backend_provenance_matches_configured(
        usage, usage.backend_configured
    ):
        return "backend_provenance_invalid"
    if not _backend_identity_is_valid(usage):
        return "backend_identity_invalid"
    captured_at = (
        usage.values_captured_at
        if usage.values_captured_at is not None
        else usage.captured_at
    )
    if not _aware_datetime(now) or not _aware_datetime(captured_at):
        return "usage_timestamp_invalid"
    try:
        age = (now.astimezone(UTC) - captured_at.astimezone(UTC)).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return "usage_timestamp_invalid"
    if age < -300:
        return "usage_timestamp_in_future"
    if age > max_age_seconds:
        return "usage_too_old"
    if _has_expired_resetless_usage_window(
        usage,
        captured_at=captured_at,
        now=now,
    ):
        return "usage_stale"
    return None


def _has_expired_resetless_usage_window(
    usage: AccountUsage,
    *,
    captured_at: datetime,
    now: datetime,
) -> bool:
    try:
        elapsed = (
            now.astimezone(UTC) - captured_at.astimezone(UTC)
        ).total_seconds()
        pools = [usage.main]
        if isinstance(usage.models, tuple):
            pools.extend(usage.models)
        for pool in pools:
            if not isinstance(pool, UsagePool) or not isinstance(pool.windows, tuple):
                continue
            for window in pool.windows:
                if (
                    not isinstance(window, LimitWindow)
                    or window.reset_at is not None
                    or window.source
                    in {
                        "inferred:inactive-five-hour:direct",
                        "inferred:inactive-five-hour:app-server",
                    }
                ):
                    continue
                duration = _window_identity_key(window)
                if duration is not None and elapsed >= duration:
                    return True
    except (AttributeError, OverflowError, TypeError, ValueError):
        return False
    return False


def _spark_health_is_fresh(payload: dict[str, Any], *, now: datetime) -> bool:
    if payload.get("state") != "healthy" or payload.get("stale") is not False:
        return False
    age = _spark_health_age_seconds(payload, now=now)
    if age is None:
        return False
    return 0 <= age <= SPARK_HEALTH_MAX_AGE_SECONDS


def _spark_health_age_seconds(payload: dict[str, Any], *, now: datetime) -> float | None:
    checked_at = payload.get("checked_at")
    if not isinstance(checked_at, str):
        return None
    try:
        timestamp = datetime.fromisoformat(checked_at)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return None
        return (now.astimezone(UTC) - timestamp.astimezone(UTC)).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None


def _pool_usage_state(pool: UsagePool, *, now: datetime) -> str:
    if not isinstance(pool, UsagePool) or not _pool_flags_are_valid(pool):
        return "invalid"
    if not isinstance(pool.windows, tuple) or not pool.windows:
        return "unknown"
    if any(not isinstance(window, LimitWindow) for window in pool.windows):
        return "unknown"
    identities: set[int] = set()
    names: set[str] = set()
    for window in pool.windows:
        identity = _window_identity_key(window)
        if identity is None or identity in identities:
            return "unknown"
        identities.add(identity)
        if window.name in names:
            return "unknown"
        names.add(window.name)
    if any(not _window_identity_is_known(window) for window in pool.windows):
        return "unknown"
    if any(not _window_reset_is_current(window, now=now) for window in pool.windows):
        return "unknown"
    if any(window.has_invalid_usage_value for window in pool.windows):
        return "invalid"
    return (
        "invalid"
        if any(
            value is not None and not _valid_remaining_percent(value)
            for value in (window.remaining_percent for window in pool.windows)
        )
        else "unknown"
        if any(window.remaining_percent is None for window in pool.windows)
        else "known"
    )


def _pool_flags_are_valid(pool: UsagePool) -> bool:
    return (
        isinstance(pool.available, bool)
        and (pool.allowed is None or isinstance(pool.allowed, bool))
        and (
            pool.limit_reached is None
            or isinstance(pool.limit_reached, bool)
        )
    )


def _pool_has_usage_evidence(pool: UsagePool | None) -> bool:
    if pool is None or not isinstance(pool.availability_sources, tuple):
        return False
    return (
        "usage" in pool.availability_sources
        and all(
            isinstance(source, str) and bool(source.strip())
            for source in pool.availability_sources
        )
    )


def _pool_has_non_usage_source(pool: UsagePool | None) -> bool:
    if pool is None or not isinstance(pool.availability_sources, tuple):
        return True
    return bool(pool.availability_sources) and "usage" not in pool.availability_sources


def _backend_identity_is_valid(usage: AccountUsage) -> bool:
    identities = (usage.backend_user_id, usage.backend_account_id)
    if any(
        value is not None
        and (
            not isinstance(value, str)
            or len(value) > MAX_BACKEND_ID_CHARS
            or not value.strip()
            or any(
                char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F
                for char in value
            )
        )
        for value in identities
    ):
        return False
    if usage.backend_used is not None and not isinstance(usage.backend_used, str):
        return False
    if (
        isinstance(usage.backend_used, str)
        and usage.backend_used in {"direct", "app-server"}
    ):
        return any(value is not None for value in identities)
    return True


def _window_reset_is_current(window: Any, *, now: datetime) -> bool:
    reset_at = getattr(window, "reset_at", None)
    if reset_at is None:
        return True
    if not _aware_datetime(reset_at) or not _aware_datetime(now):
        return False
    try:
        reset_utc = reset_at.astimezone(UTC)
        now_utc = now.astimezone(UTC)
        duration = _window_identity_key(window)
        if duration is None:
            return False
        return now_utc < reset_utc <= now_utc + timedelta(
            seconds=duration + MAX_RESET_FUTURE_SKEW_SECONDS
        )
    except (AttributeError, OverflowError, TypeError, ValueError):
        return False


def _window_identity_is_known(window: Any) -> bool:
    duration = getattr(window, "duration_seconds", None)
    name = getattr(window, "name", None)
    if not isinstance(name, str):
        return False
    if duration is not None and (
        not isinstance(duration, int)
        or isinstance(duration, bool)
        or duration not in SUPPORTED_WINDOW_SECONDS
    ):
        return False
    if not name.strip():
        return duration in SUPPORTED_WINDOW_SECONDS
    expected_duration = WINDOW_NAME_DURATIONS.get(name.strip().casefold())
    if expected_duration is None:
        return name.strip().casefold() == _canonical_window_name(duration)
    return duration is None or duration == expected_duration


def _window_identity_key(window: Any) -> int | None:
    if not _window_identity_is_known(window):
        return None
    duration = getattr(window, "duration_seconds", None)
    if duration is not None:
        return duration
    name = getattr(window, "name", None)
    if not isinstance(name, str):
        return None
    return WINDOW_NAME_DURATIONS.get(name.strip().casefold())


def _canonical_window_name(duration: int | None) -> str:
    if duration is None:
        return ""
    if duration % 86_400 == 0:
        return f"{duration // 86_400}d"
    if duration % 3_600 == 0:
        return f"{duration // 3_600}h"
    return f"{duration}s"


def _valid_remaining_percent(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(numeric) and 0 <= numeric <= 100


def _pool_resets(pool: UsagePool | None) -> dict[str, str | None]:
    if pool is None:
        return {}
    return {
        window.name: _timestamp_text(window.reset_at)
        for window in pool.windows
    }


def _aware_datetime(value: Any) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    try:
        return value.utcoffset() is not None
    except (OverflowError, TypeError, ValueError):
        return False


def _timestamp_text(value: Any) -> str | None:
    if value is None or not _aware_datetime(value):
        return None
    try:
        return value.isoformat()
    except (OverflowError, TypeError, ValueError):
        return None


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
        "credit_limits": {key: None for key in CREDIT_LIMIT_KEYS},
        "credit_limit_overrides": {scope: {} for scope in POLICY_SCOPES},
    }


def _validate_policy(payload: Any) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != POLICY_SCHEMA_VERSION
    ):
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
    result["credit_limits"] = _validate_credit_limits(payload.get("credit_limits", {}))
    overrides = payload.get("credit_limit_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("routing credit limit overrides are invalid")
    for scope in POLICY_SCOPES:
        source = overrides.get(scope, {})
        if not isinstance(source, dict) or len(source) > 500:
            raise ValueError("routing credit limit overrides are invalid")
        for identifier, limits in source.items():
            normalized_identifier = _validate_identifier(identifier)
            normalized_limits = _validate_credit_limits(limits)
            if all(value is None for value in normalized_limits.values()):
                raise ValueError("empty routing credit limit override")
            result["credit_limit_overrides"][scope][normalized_identifier] = (
                normalized_limits
            )
    return result


def _validate_credit_limits(value: Any) -> dict[str, float | None]:
    if not isinstance(value, dict):
        raise ValueError("routing credit limits are invalid")
    result: dict[str, float | None] = {}
    for key in CREDIT_LIMIT_KEYS:
        raw = value.get(key)
        if raw is None:
            result[key] = None
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("routing credit limit is invalid")
        number = float(raw)
        if not math.isfinite(number) or number < 0:
            raise ValueError("routing credit limit is invalid")
        result[key] = number
    return result


def _validate_identifier(value: Any) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise ValueError("policy identifier is invalid")
    return value


def _validate_optional_identifier(value: Any) -> str | None:
    return None if value is None else _validate_identifier(value)


def _prepare_private_directory(path: Path) -> None:
    ensure_private_directory(path, label="routing policy directory")
