from __future__ import annotations

import json
import math
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .account_lock import account_lock
from .config import default_state_dir
from .extractor import LOCAL_TZ
from .json_utils import loads_strict
from .models import AccountStatus, AccountUsage, LimitWindow, UsagePool
from .private_io import (
    assert_no_symlink_ancestors,
    ensure_private_directory,
    private_path_lock,
    read_private_text,
    write_private_text,
)
from .usage_limits import MAX_WINDOW_SECONDS
from .usage_resets import UsageResetState, parse_usage_resets

MAX_SNAPSHOT_BYTES = 1_000_000
SNAPSHOT_ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MAX_SNAPSHOT_TEXT = 500
MAX_SNAPSHOT_URLS = 20
MAX_STATE_GENERATION_BYTES = 4096
MAX_STATE_TRANSACTION_ENTRIES = 8
AUTHENTICATED_BACKENDS = frozenset(("direct", "app-server"))
KNOWN_BACKENDS = AUTHENTICATED_BACKENDS | frozenset(("browser",))
WINDOW_DURATIONS = {
    "five_hour": 18_000,
    "weekly": 604_800,
    "thirty_day": 2_592_000,
}
MAX_MODEL_POOLS = 20
MAX_POOL_WINDOWS = 8
MAX_RESET_FUTURE_SKEW_SECONDS = 5 * 60
APP_SERVER_FALLBACK_REASON_PREFIX = "app-server unavailable: "
KNOWN_APP_SERVER_UNAVAILABLE_DETAILS = frozenset(
    (
        "codex command was not found",
        "codex command is not executable",
        "could not start codex app server",
        "codex app server exited unexpectedly",
        "installed Codex does not support rate-limit RPC",
    )
)
KNOWN_FALLBACK_REASONS = frozenset(
    (
        "previous direct limits retained after reset transition",
        "previous authenticated limits retained after reset transition",
    )
)
INFERRED_INACTIVE_FIVE_HOUR_SOURCE = "inferred:inactive-five-hour"
KNOWN_INFERRED_INACTIVE_FIVE_HOUR_SOURCES = frozenset(
    (
        f"{INFERRED_INACTIVE_FIVE_HOUR_SOURCE}:direct",
        f"{INFERRED_INACTIVE_FIVE_HOUR_SOURCE}:app-server",
    )
)


def backend_provenance_matches_configured(
    usage: AccountUsage,
    configured_backend: str,
) -> bool:
    """Reject authenticated cache data produced by an explicit other backend."""
    if (
        not isinstance(configured_backend, str)
        or configured_backend not in KNOWN_BACKENDS
    ):
        return False
    if not isinstance(usage, AccountUsage):
        return False
    if not _backend_provenance_fields_valid(usage):
        return False
    if not _backend_provenance_is_complete(usage):
        return False
    if usage.backend_configured != configured_backend:
        return False
    if usage.backend_used == "browser":
        # Browser can be an intentional fallback for an account configured
        # with an authenticated backend, but only when that provenance is
        # explicit. An unlabelled browser snapshot is not attributable.
        return usage.backend_configured == configured_backend
    if usage.backend_used not in AUTHENTICATED_BACKENDS:
        return True
    if usage.backend_used == configured_backend:
        return True
    return configured_backend == "app-server" and _has_backend_fallback_proof(usage)


def backend_provenance_matches(left: AccountUsage, right: AccountUsage) -> bool:
    """Avoid merging values across authenticated backends without fallback proof."""
    if not isinstance(left, AccountUsage) or not isinstance(right, AccountUsage):
        return False
    if not _backend_provenance_is_complete(left) or not _backend_provenance_is_complete(right):
        return False
    if (
        left.backend_configured
        and right.backend_configured
        and left.backend_configured != right.backend_configured
    ):
        return False
    left_backend = left.backend_used
    right_backend = right.backend_used
    if "browser" in {left_backend, right_backend}:
        return left_backend == right_backend == "browser"
    if left_backend not in AUTHENTICATED_BACKENDS or right_backend not in AUTHENTICATED_BACKENDS:
        return True
    if left_backend == right_backend:
        return True
    return _has_backend_fallback_proof(left) or _has_backend_fallback_proof(right)


def _backend_provenance_fields_valid(usage: AccountUsage) -> bool:
    return (
        _backend_value_valid(usage.backend_configured, KNOWN_BACKENDS)
        and _backend_value_valid(usage.backend_used, KNOWN_BACKENDS)
    )


def _backend_value_valid(value: str | None, allowed: frozenset[str]) -> bool:
    return value is None or value == "" or (
        isinstance(value, str) and value in allowed
    )


def _backend_provenance_is_complete(usage: AccountUsage) -> bool:
    return (
        isinstance(usage.backend_configured, str)
        and bool(usage.backend_configured)
        and usage.backend_configured in KNOWN_BACKENDS
        and isinstance(usage.backend_used, str)
        and bool(usage.backend_used)
        and usage.backend_used in KNOWN_BACKENDS
    )


def _has_backend_fallback_proof(usage: AccountUsage) -> bool:
    if (
        usage.backend_configured != "app-server"
        or usage.backend_used != "direct"
    ):
        return False
    fallback_reason = usage.fallback_reason
    if not isinstance(fallback_reason, str):
        return False
    if fallback_reason in KNOWN_FALLBACK_REASONS:
        return True
    return bool(
        fallback_reason.startswith(APP_SERVER_FALLBACK_REASON_PREFIX)
        and fallback_reason[len(APP_SERVER_FALLBACK_REASON_PREFIX) :]
        in KNOWN_APP_SERVER_UNAVAILABLE_DETAILS
    )


def default_snapshot_dir() -> Path:
    return default_state_dir() / "snapshots"


def default_current_dir() -> Path:
    return default_state_dir() / "current"


def _state_directory(path: object | None, default: Path) -> Path:
    if path is None:
        return default
    if not isinstance(path, Path):
        raise ValueError("state directory is invalid")
    return path


def load_state_generation(
    account_id: str,
    directory: Path | None = None,
) -> int:
    _validate_snapshot_account_id(account_id)
    directory = _state_directory(directory, default_snapshot_dir())
    with account_state_lock(account_id):
        return _load_state_generation_unlocked(account_id, directory)


@contextmanager
def account_state_lock(account_id: str) -> Iterator[None]:
    _validate_snapshot_account_id(account_id)
    with account_lock(account_id):
        yield


def _load_state_generation_unlocked(
    account_id: str,
    directory: Path | None = None,
) -> int:
    _validate_snapshot_account_id(account_id)
    directory = _state_directory(directory, default_snapshot_dir())
    generation_path = _state_generation_path(
        account_id,
        directory,
    )
    return _read_state_generation(generation_path, account_id)


def save_usage_snapshot(usage: AccountUsage, snapshot_dir: Path | None = None) -> Path:
    if not isinstance(usage, AccountUsage):
        raise ValueError("usage is invalid")
    _validate_snapshot_account_id(usage.account_id)
    directory = _state_directory(snapshot_dir, default_snapshot_dir())
    assert_no_symlink_ancestors(directory, label="snapshot directory")
    with account_state_lock(usage.account_id):
        return _save_usage(usage, directory, preserve_existing_values=True)


def save_current_usage(usage: AccountUsage, current_dir: Path | None = None) -> Path:
    if not isinstance(usage, AccountUsage):
        raise ValueError("usage is invalid")
    _validate_snapshot_account_id(usage.account_id)
    directory = _state_directory(current_dir, default_current_dir())
    assert_no_symlink_ancestors(directory, label="snapshot directory")
    with account_state_lock(usage.account_id):
        return _save_usage(usage, directory)


def _save_usage(
    usage: AccountUsage,
    directory: Path,
    *,
    preserve_existing_values: bool = False,
) -> Path:
    _validate_snapshot_account_id(usage.account_id)
    usage = replace(usage, captured_at=_saved_datetime(usage.captured_at))
    assert_no_symlink_ancestors(directory, label="snapshot directory")
    if directory.is_symlink():
        raise ValueError(f"snapshot directory must not be a symlink: {directory}")
    ensure_private_directory(directory, label="snapshot directory")
    path = directory / f"{usage.account_id}.json"
    current_generation = _read_state_generation(
        _state_generation_path(usage.account_id, directory),
        usage.account_id,
    )
    if usage.state_generation is not None and usage.state_generation != current_generation:
        return path
    if usage.state_generation is None:
        usage = replace(usage, state_generation=current_generation)
    with private_path_lock(path, label="snapshot lock"):
        existing = _load_usage(usage.account_id, directory)
        if existing is not None:
            try:
                if existing.captured_at > usage.captured_at:
                    return path
            except TypeError:
                pass
            if _equal_capture_prefers_existing(existing, usage):
                return path
            if (
                preserve_existing_values
                and not _authoritative_empty_limits(usage)
                and _backend_provenance_is_complete(usage)
                and _backend_provenance_is_complete(existing)
                and backend_identity_matches(usage, existing)
                and backend_provenance_matches(usage, existing)
            ):
                usage = merge_current_with_last_success(usage, existing)
        payload = usage.as_dict()
        payload["state_generation"] = usage.state_generation
        text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
        if len(text.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError(f"snapshot file too large; max {MAX_SNAPSHOT_BYTES} bytes")
        write_private_text(path, text, label="snapshot path")
    return path


def _equal_capture_prefers_existing(
    existing: AccountUsage,
    incoming: AccountUsage,
) -> bool:
    try:
        if existing.captured_at != incoming.captured_at:
            return False
    except TypeError:
        return False
    existing_priority = _backend_capture_priority(existing)
    incoming_priority = _backend_capture_priority(incoming)
    if existing_priority != incoming_priority:
        return existing_priority > incoming_priority
    if existing.backend_used == incoming.backend_used:
        return False
    return not backend_provenance_matches(existing, incoming)


def _backend_capture_priority(usage: AccountUsage) -> int:
    if usage.backend_used == "browser":
        return 0
    if (
        isinstance(usage.backend_used, str)
        and usage.backend_used in AUTHENTICATED_BACKENDS
    ):
        if usage.backend_configured == usage.backend_used:
            return 2
        return 1
    return -1


def load_usage_snapshot(account_id: str, snapshot_dir: Path | None = None) -> AccountUsage | None:
    return _load_usage(account_id, _state_directory(snapshot_dir, default_snapshot_dir()))


def load_current_usage(account_id: str, current_dir: Path | None = None) -> AccountUsage | None:
    return _load_usage(account_id, _state_directory(current_dir, default_current_dir()))


@dataclass
class _StateDeleteTransaction:
    transaction_dir: Path | None
    moved: list[tuple[Path, Path]]
    generation_path: Path
    generation_before: tuple[str, int] | None
    locks: ExitStack
    closed: bool = False

    def commit(self) -> None:
        if self.closed:
            return
        try:
            if self.transaction_dir is not None:
                _remove_state_transaction_dir(self.transaction_dir)
                self.transaction_dir = None
        except BaseException as primary_error:
            try:
                self.rollback()
            except BaseException as rollback_error:
                raise BaseExceptionGroup(
                    "state deletion rollback failed",
                    [primary_error, rollback_error],
                ) from None
            raise
        self.locks.close()
        self.closed = True

    def rollback(self) -> None:
        if self.closed:
            return
        rollback_errors: list[BaseException] = []
        try:
            _restore_generation_state(self.generation_path, self.generation_before)
        except BaseException as rollback_error:
            rollback_errors.append(rollback_error)
        backup_restore_failed = False
        for path, backup in reversed(self.moved):
            try:
                if path.exists() or path.is_symlink():
                    raise ValueError(f"state path appeared during rollback: {path}")
                backup.rename(path)
            except BaseException as rollback_error:
                backup_restore_failed = True
                rollback_errors.append(rollback_error)
        if self.transaction_dir is not None and not backup_restore_failed:
            try:
                _remove_state_transaction_dir(self.transaction_dir)
                self.transaction_dir = None
            except BaseException as rollback_error:
                rollback_errors.append(rollback_error)
        self.locks.close()
        self.closed = True
        if rollback_errors:
            raise BaseExceptionGroup("state deletion rollback failed", rollback_errors)


def remove_account_state(
    account_id: str,
    *,
    lock_held: bool = False,
    defer_commit: bool = False,
) -> _StateDeleteTransaction | None:
    _validate_snapshot_account_id(account_id)
    if defer_commit and not lock_held:
        raise ValueError("deferred state deletion requires held account lock")
    if lock_held:
        return _remove_account_state_unlocked(account_id, defer_commit=defer_commit)
    with account_state_lock(account_id):
        transaction = _remove_account_state_unlocked(account_id, defer_commit=False)
        if transaction is not None:
            transaction.commit()
    return None


def _remove_account_state_unlocked(
    account_id: str,
    *,
    defer_commit: bool = False,
) -> _StateDeleteTransaction | None:
    state_root = default_state_dir()
    targets = (
        (default_snapshot_dir(), f"{account_id}.json", "snapshot path"),
        (default_current_dir(), f"{account_id}.json", "current path"),
        (
            default_state_dir() / "debug",
            f"{account_id}-last-ingest.json",
            "debug path",
        ),
    )
    generation_path = _state_generation_path(account_id, default_snapshot_dir())
    generation_before = _capture_generation_state(generation_path, account_id)
    transaction_dir: Path | None = None
    moved: list[tuple[Path, Path]] = []
    locks = ExitStack()
    transaction: _StateDeleteTransaction | None = None
    try:
        ensure_private_directory(state_root, label="state directory")
        transaction_dir = Path(
            tempfile.mkdtemp(prefix=f".{account_id}.state-delete-", dir=state_root)
        )
        for directory, filename, label in targets:
            assert_no_symlink_ancestors(directory, label=f"{label} directory")
            if not directory.exists() and not directory.is_symlink():
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError(f"{label} directory must be a real directory: {directory}")
            path = directory / filename
            locks.enter_context(private_path_lock(path, label=f"{label} lock"))
            if path.is_symlink():
                raise ValueError(f"{label} must be a regular file: {path}")
            if path.is_dir() and not path.is_symlink():
                raise ValueError(f"{label} must be a regular file: {path}")
            if path.exists() or path.is_symlink():
                file_stat = path.lstat()
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ValueError(f"{label} must be a regular file: {path}")
                if file_stat.st_nlink != 1:
                    raise ValueError(f"{label} must not be hard-linked: {path}")
                backup = transaction_dir / f"{len(moved):02d}-{filename}"
                path.rename(backup)
                moved.append((path, backup))

        # Invalidate only after all target paths are staged. Rollback restores
        # previous generation if generation write or commit fails.
        _increment_state_generation(account_id, state_root)
        transaction = _StateDeleteTransaction(
            transaction_dir=transaction_dir,
            moved=moved,
            generation_path=generation_path,
            generation_before=generation_before,
            locks=locks,
        )
        if defer_commit:
            return transaction
        transaction.commit()
        transaction = None
    except BaseException as primary_error:
        if transaction is not None:
            try:
                transaction.rollback()
            except BaseException as rollback_error:
                raise BaseExceptionGroup(
                    "state cleanup rollback failed",
                    [primary_error, rollback_error],
                ) from None
            raise
        rollback_errors: list[BaseException] = []
        try:
            _restore_generation_state(generation_path, generation_before)
        except BaseException as rollback_error:
            rollback_errors.append(rollback_error)
        backup_restore_failed = False
        for path, backup in reversed(moved):
            try:
                if path.exists() or path.is_symlink():
                    raise ValueError(f"state path appeared during rollback: {path}")
                backup.rename(path)
            except BaseException as rollback_error:
                backup_restore_failed = True
                rollback_errors.append(rollback_error)
        if transaction_dir is not None and not backup_restore_failed:
            try:
                _remove_state_transaction_dir(transaction_dir)
            except BaseException as rollback_error:
                rollback_errors.append(rollback_error)
        if rollback_errors:
            raise BaseExceptionGroup(
                "state cleanup rollback failed",
                [primary_error, *rollback_errors],
            ) from None
        raise
    finally:
        if transaction is None:
            locks.close()
    return None


def _remove_state_transaction_dir(path: Path) -> None:
    children: list[Path] = []
    for child in path.iterdir():
        if len(children) >= MAX_STATE_TRANSACTION_ENTRIES:
            raise ValueError("too many state transaction entries")
        if child.is_symlink() or not child.is_file():
            raise ValueError(f"unexpected state transaction entry: {child}")
        children.append(child)
    for child in children:
        child.unlink()
    path.rmdir()


def _capture_generation_state(
    path: Path,
    account_id: str,
) -> tuple[str, int] | None:
    _read_state_generation(path, account_id)
    if not path.exists():
        return None
    text, file_stat = read_private_text(
        path,
        regular_label="state generation",
        read_label="state generation",
        max_bytes=MAX_STATE_GENERATION_BYTES,
    )
    return text, file_stat.st_mode & 0o777


def _restore_generation_state(
    path: Path,
    previous: tuple[str, int] | None,
) -> None:
    if previous is None:
        if path.is_dir() and not path.is_symlink():
            raise ValueError(f"state generation must be a regular file: {path}")
        if path.exists() or path.is_symlink():
            path.unlink()
        return
    write_private_text(
        path,
        previous[0],
        label="state generation rollback",
        mode=previous[1],
    )


def _state_generation_path(account_id: str, directory: Path) -> Path:
    return directory.parent / "generations" / f"{account_id}.json"


def _read_state_generation(path: Path, account_id: str) -> int:
    assert_no_symlink_ancestors(path, label="state generation")
    if not path.exists():
        if path.is_symlink():
            raise ValueError(f"state generation must be a regular file: {path}")
        return 0
    text, file_stat = read_private_text(
        path,
        regular_label="state generation",
        read_label="state generation",
        max_bytes=MAX_STATE_GENERATION_BYTES,
        too_large_label="state generation",
        invalid_utf8_label="state generation",
    )
    if file_stat.st_nlink != 1 or file_stat.st_mode & 0o077:
        raise ValueError(f"state generation must be a private regular file: {path}")
    payload = loads_strict(text)
    if not isinstance(payload, dict) or payload.get("account") != account_id:
        raise ValueError(f"state generation account mismatch: {path}")
    generation = payload.get("generation")
    if type(generation) is not int or generation < 0:
        raise ValueError(f"invalid state generation: {path}")
    return generation


def _increment_state_generation(account_id: str, state_dir: Path) -> int:
    directory = state_dir / "generations"
    assert_no_symlink_ancestors(directory, label="state generation directory")
    if directory.is_symlink():
        raise ValueError(f"state generation directory must not be a symlink: {directory}")
    try:
        ensure_private_directory(directory, label="state generation directory")
    except OSError as exc:
        raise ValueError("could not secure state generation directory") from exc
    path = directory / f"{account_id}.json"
    generation = _read_state_generation(path, account_id) + 1
    text = json.dumps(
        {"account": account_id, "generation": generation},
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )
    write_private_text(path, text, label="state generation")
    return generation


def _load_usage(account_id: str, directory: Path) -> AccountUsage | None:
    try:
        _validate_snapshot_account_id(account_id)
    except (OverflowError, ValueError):
        return None
    path = directory / f"{account_id}.json"
    if not path.exists():
        return None
    try:
        text, file_stat = read_private_text(
            path,
            regular_label="snapshot path",
            read_label="snapshot file",
            max_bytes=MAX_SNAPSHOT_BYTES,
            too_large_label="snapshot file",
            invalid_utf8_label="snapshot file",
        )
        if file_stat.st_nlink != 1 or file_stat.st_mode & 0o077:
            return None
        payload = loads_strict(text)
        if not isinstance(payload, dict):
            return None
        snapshot_account = payload.get("account")
        if not isinstance(snapshot_account, str) or snapshot_account != account_id:
            return None
        usage = usage_from_dict(payload)
        provenance_is_valid = (
            _backend_provenance_is_complete(usage)
            and isinstance(usage.backend_configured, str)
            and backend_provenance_matches_configured(
                usage,
                usage.backend_configured,
            )
        )
        if not provenance_is_valid:
            error = "incomplete cached backend provenance"
            if usage.error:
                error = f"{usage.error}; {error}"
            usage = replace(
                usage,
                five_hour=None,
                weekly=None,
                main=None,
                models=(),
                error=error,
                status=(
                    AccountStatus.PARTIAL
                    if usage.status == AccountStatus.OK
                    else usage.status
                ),
                values_captured_at=None,
                stale=True,
                cache_invalidated=True,
            )
        generation = _read_state_generation(
            _state_generation_path(account_id, directory),
            account_id,
        )
        if usage.state_generation is None:
            return usage if generation == 0 else None
        if usage.state_generation != generation:
            return None
        return usage
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return None


def expire_reset_windows(
    usage: AccountUsage,
    *,
    reference_at: datetime,
) -> AccountUsage:
    if not isinstance(usage, AccountUsage):
        raise ValueError("usage is invalid")
    if not isinstance(usage.models, tuple) or len(usage.models) > MAX_MODEL_POOLS:
        return replace(
            usage,
            models=(),
            status=(
                AccountStatus.PARTIAL
                if usage.status == AccountStatus.OK
                else usage.status
            ),
            error="model pool catalog invalid",
            values_captured_at=None,
            stale=True,
            cache_invalidated=True,
        )
    expired_names: list[str] = []
    five_hour = usage.five_hour
    weekly = usage.weekly
    five_hour_expired = False
    weekly_expired = False
    values_captured_at = _values_capture_for_expiry(usage)
    if _cached_window_expired(
        five_hour,
        captured_at=_window_expiry_capture(usage, five_hour, values_captured_at),
        reference_at=reference_at,
        expected_kind="five_hour",
    ):
        expired_names.append("5h")
        five_hour_expired = True
        five_hour = None
    if _cached_window_expired(
        weekly,
        captured_at=_window_expiry_capture(usage, weekly, values_captured_at),
        reference_at=reference_at,
        expected_kind="weekly",
    ):
        expired_names.append("weekly")
        weekly_expired = True
        weekly = None
    main, main_expired = _expire_pool_windows(
        usage.main,
        usage=usage,
        values_captured_at=values_captured_at,
        reference_at=reference_at,
        expired_names=expired_names,
    )
    model_pools: tuple[UsagePool, ...] = ()
    models_changed = False
    model_catalog_invalid = False
    for pool in usage.models:
        if not isinstance(pool, UsagePool):
            model_catalog_invalid = True
            models_changed = True
            continue
        try:
            if (
                not isinstance(pool.key, str)
                or not pool.key
                or not isinstance(pool.windows, tuple)
                or any(
                    not isinstance(window, LimitWindow)
                    or not window.has_known_identity
                    for window in pool.windows
                )
            ):
                model_catalog_invalid = True
        except (AttributeError, TypeError, ValueError):
            model_catalog_invalid = True
        updated_pool, pool_expired = _expire_pool_windows(
            pool,
            usage=usage,
            values_captured_at=values_captured_at,
            reference_at=reference_at,
            expired_names=expired_names,
            name_prefix=pool.key,
        )
        if updated_pool is None:
            models_changed = True
            continue
        models_changed = models_changed or pool_expired
        model_pools += (updated_pool,)
    core_expired = five_hour_expired or weekly_expired or main_expired
    model_windows_remaining = any(
        isinstance(pool, UsagePool) and bool(pool.windows)
        for pool in model_pools
    )
    try:
        blocked_until_expired = (
            usage.status == AccountStatus.BLOCKED
            and usage.blocked_until is not None
            and _localize_datetime(usage.blocked_until) <= _localize_datetime(reference_at)
        )
    except (AttributeError, OverflowError, TypeError, ValueError):
        blocked_until_expired = False
    clear_expired_block = (
        usage.status == AccountStatus.BLOCKED
        and not five_hour
        and not weekly
        and not (main and main.windows)
        and not model_windows_remaining
        and (usage.blocked_until is None or blocked_until_expired)
    )

    if not models_changed and not core_expired and not clear_expired_block:
        return usage

    error: str | None
    if core_expired:
        names = ", ".join(expired_names)
        error = f"cached limit window expired: {names}; refresh required"
        if model_catalog_invalid:
            error += "; model pool catalog invalid"
    elif clear_expired_block:
        error = "cached blocked state expired; refresh required"
    elif model_catalog_invalid:
        error = (
            f"{usage.error}; model pool catalog invalid"
            if usage.error
            else "model pool catalog invalid"
        )
    else:
        error = usage.error
    status = usage.status
    blocked_until = usage.blocked_until
    blocked_reason = usage.blocked_reason
    if core_expired and status == AccountStatus.OK:
        status = AccountStatus.PARTIAL
    elif clear_expired_block:
        status = AccountStatus.PARTIAL
        blocked_until = None
        blocked_reason = None
    elif model_catalog_invalid and status == AccountStatus.OK:
        status = AccountStatus.PARTIAL
    return replace(
        usage,
        five_hour=five_hour,
        weekly=weekly,
        main=main,
        models=model_pools,
        status=status,
        error=error,
        blocked_until=blocked_until,
        blocked_reason=blocked_reason,
        stale=usage.stale or core_expired or model_catalog_invalid or clear_expired_block,
    )


def _expire_pool_windows(
    pool: UsagePool | None,
    *,
    usage: AccountUsage,
    values_captured_at: datetime,
    reference_at: datetime,
    expired_names: list[str],
    name_prefix: str | None = None,
) -> tuple[UsagePool | None, bool]:
    if pool is None:
        return pool, False
    if not isinstance(pool, UsagePool):
        return None, True
    if not isinstance(pool.key, str) or not pool.key:
        return None, True
    if not isinstance(pool.windows, tuple):
        return replace(pool, windows=(), available=False), True
    if not pool.windows:
        return pool, False
    remaining: list[LimitWindow] = []
    expired = False
    for window in pool.windows:
        if not isinstance(window, LimitWindow):
            expired = True
            name = f"{name_prefix}:invalid" if name_prefix else "invalid"
            if name not in expired_names:
                expired_names.append(name)
            continue
        if _cached_window_expired(
            window,
            captured_at=_window_expiry_capture(usage, window, values_captured_at),
            reference_at=reference_at,
        ):
            expired = True
            name = f"{name_prefix}:{window.name}" if name_prefix else window.name
            if name not in expired_names:
                expired_names.append(name)
        else:
            remaining.append(window)
    if not expired:
        return pool, False
    return replace(
        pool,
        windows=tuple(remaining),
        available=pool.available and bool(remaining),
    ), True


def _window_expiry_capture(
    usage: AccountUsage,
    window: LimitWindow | None,
    values_captured_at: datetime,
) -> datetime:
    if (
        isinstance(window, LimitWindow)
        and window.reset_at is not None
        and isinstance(usage.captured_at, datetime)
    ):
        # An explicit reset belongs to the current observation. The shared
        # values timestamp may point to an older counterpart restored during
        # a partial browser merge and would reject this fresh reset as too far
        # in the future.
        return _localize_datetime(usage.captured_at)
    return values_captured_at


def usage_from_dict(payload: dict[str, Any]) -> AccountUsage:
    if not isinstance(payload, dict):
        raise ValueError("state payload must be an object")
    raw_five_hour = payload.get("five_hour")
    raw_weekly = payload.get("weekly")
    raw_credits = payload.get("credits")
    five_hour = _window_from_dict(raw_five_hour, expected_kind="five_hour")
    weekly = _window_from_dict(raw_weekly, expected_kind="weekly")
    credits = _window_from_dict(raw_credits, expected_kind="credits")
    raw_main = payload.get("main")
    main = _pool_from_dict(raw_main, expected_key="main")
    raw_models = payload.get("models")
    parsed_model_pools = _model_pools_from_dict(raw_models)
    invalid_model_fields = [
        "models"
    ] if "models" in payload and parsed_model_pools is None else []
    model_pools = parsed_model_pools or ()
    invalid_window_fields = [
        field
        for field, raw_window, parsed_window in (
            ("five_hour", raw_five_hour, five_hour),
            ("weekly", raw_weekly, weekly),
        )
        if isinstance(raw_window, dict) and parsed_window is None
    ]
    invalid_window_fields.extend(
        field
        for field, raw_window in (
            ("five_hour", raw_five_hour),
            ("weekly", raw_weekly),
        )
        if field in payload and raw_window is not None and not isinstance(raw_window, dict)
    )
    invalid_pool_fields = [
        "main"
    ] if "main" in payload and raw_main is not None and main is None else []
    sanitized_window_fields = [
        field
        for field, raw_window, parsed_window in (
            ("five_hour", raw_five_hour, five_hour),
            ("weekly", raw_weekly, weekly),
        )
        if _window_had_invalid_cached_value(raw_window, parsed_window)
    ]
    raw_status = payload.get("status")
    status_missing = "status" not in payload
    status = AccountStatus(str(raw_status)) if not status_missing else AccountStatus.PARTIAL
    error = _optional_snapshot_text(payload.get("error"), limit=MAX_SNAPSHOT_TEXT)
    raw_cache_invalidated = payload.get("cache_invalidated")
    cache_flag_invalid = (
        "cache_invalidated" in payload
        and not isinstance(raw_cache_invalidated, bool)
    )
    cache_invalidated = raw_cache_invalidated is True or cache_flag_invalid
    raw_stale = payload.get("stale")
    stale_flag_invalid = "stale" in payload and not isinstance(raw_stale, bool)
    stale_metadata_missing = "stale" not in payload
    cache_metadata_missing = "cache_invalidated" not in payload
    raw_values_captured_at = payload.get("values_captured_at")
    invalid_values_captured_at = (
        "values_captured_at" in payload
        and raw_values_captured_at is not None
        and _optional_datetime(raw_values_captured_at) is None
    )
    values_captured_at = _optional_datetime(raw_values_captured_at)
    raw_state_generation = payload.get("state_generation")
    state_generation = _optional_state_generation(raw_state_generation)
    invalid_state_generation = (
        "state_generation" in payload
        and (raw_state_generation is None or state_generation is None)
    )
    raw_backend_fields = (
        ("backend_configured", payload.get("backend_configured")),
        ("backend_used", payload.get("backend_used")),
        ("backend_user_id", payload.get("backend_user_id")),
        ("backend_account_id", payload.get("backend_account_id")),
    )
    backend_fields = {
        field: _optional_snapshot_identity(value, limit=256)
        for field, value in raw_backend_fields
    }
    invalid_backend_fields = [
        field
        for field, raw_value in raw_backend_fields
        if raw_value is not None and backend_fields[field] is None
    ]
    forced_stale = False
    metadata_errors: list[str] = []
    if status_missing:
        metadata_errors.append("missing cached status")
    if stale_metadata_missing:
        metadata_errors.append("missing cached stale flag")
    elif stale_flag_invalid:
        metadata_errors.append("invalid cached stale flag")
    if cache_metadata_missing:
        metadata_errors.append("missing cached invalidation flag")
    elif cache_flag_invalid:
        metadata_errors.append("invalid cached invalidation flag")
    if metadata_errors:
        if status == AccountStatus.OK:
            status = AccountStatus.PARTIAL
        error = "; ".join((*filter(None, (error,)), *metadata_errors))
    if invalid_window_fields:
        if status == AccountStatus.OK:
            status = AccountStatus.PARTIAL
        invalid_error = (
            "invalid cached limit window slot: "
            + ", ".join(invalid_window_fields)
        )
        error = f"{error}; {invalid_error}" if error else invalid_error
        forced_stale = True
    if invalid_pool_fields:
        if status == AccountStatus.OK:
            status = AccountStatus.PARTIAL
        invalid_error = "invalid cached usage pool: " + ", ".join(invalid_pool_fields)
        error = f"{error}; {invalid_error}" if error else invalid_error
        forced_stale = True
        cache_invalidated = True
    if invalid_model_fields:
        if status == AccountStatus.OK:
            status = AccountStatus.PARTIAL
        invalid_error = "invalid cached model pools: " + ", ".join(invalid_model_fields)
        error = f"{error}; {invalid_error}" if error else invalid_error
        forced_stale = True
        cache_invalidated = True
    if sanitized_window_fields:
        if status == AccountStatus.OK:
            status = AccountStatus.PARTIAL
        sanitized_error = (
            "invalid cached limit value: "
            + ", ".join(sanitized_window_fields)
        )
        error = f"{error}; {sanitized_error}" if error else sanitized_error
        forced_stale = True
    if invalid_values_captured_at:
        if status == AccountStatus.OK:
            status = AccountStatus.PARTIAL
        timestamp_error = "invalid cached values timestamp"
        error = f"{error}; {timestamp_error}" if error else timestamp_error
        five_hour = None
        weekly = None
        credits = None
        main = None
        model_pools = ()
        values_captured_at = None
        cache_invalidated = True
    if invalid_state_generation:
        if status == AccountStatus.OK:
            status = AccountStatus.PARTIAL
        generation_error = "invalid cached state generation"
        error = f"{error}; {generation_error}" if error else generation_error
        cache_invalidated = True
    if invalid_backend_fields:
        if status == AccountStatus.OK:
            status = AccountStatus.PARTIAL
        identity_error = (
            "invalid cached backend identity: "
            + ", ".join(invalid_backend_fields)
        )
        error = f"{error}; {identity_error}" if error else identity_error
        cache_invalidated = True
    if cache_invalidated:
        five_hour = None
        weekly = None
        credits = None
        main = None
        model_pools = ()
        values_captured_at = None
        if status == AccountStatus.OK:
            status = AccountStatus.PARTIAL
    if status == AccountStatus.LOGIN_REQUIRED:
        # An expired login must never retain values that could be mistaken for
        # a current, attributable limit observation. Transient backend errors
        # intentionally keep stale values when cache invalidation is false.
        if any(
            value is not None
            for value in (five_hour, weekly, credits, main)
        ) or model_pools:
            terminal_error = "terminal usage status cannot carry limit values"
            error = f"{error}; {terminal_error}" if error else terminal_error
        five_hour = None
        weekly = None
        credits = None
        main = None
        model_pools = ()
        values_captured_at = None
        cache_invalidated = True
        forced_stale = True
    if status == AccountStatus.OK and not _has_valid_core_usage(
        five_hour,
        weekly,
        main,
    ):
        status = AccountStatus.PARTIAL
        missing_error = "missing cached usage value"
        error = f"{error}; {missing_error}" if error else missing_error
        forced_stale = True
        cache_invalidated = True
        five_hour = None
        weekly = None
        credits = None
        main = None
        model_pools = tuple(
            replace(pool, windows=(), available=False)
            for pool in model_pools
        )
        values_captured_at = None
    usage_resets = parse_usage_resets(payload)
    if cache_invalidated or status in {
        AccountStatus.ERROR,
        AccountStatus.LOGIN_REQUIRED,
    }:
        usage_resets = UsageResetState(None, False, False)
    return AccountUsage(
        account_id=_snapshot_text(payload["account"], limit=64),
        label=_snapshot_text_or_default(
            payload.get("label"), payload["account"], limit=120
        ),
        captured_at=_snapshot_datetime(payload["captured_at"]),
        five_hour=five_hour,
        weekly=weekly,
        credits=credits,
        main=main,
        models=model_pools,
        usage_resets=usage_resets,
        status=status,
        error=error,
        blocked_until=_optional_datetime(payload.get("blocked_until")),
        blocked_reason=_optional_snapshot_text(
            payload.get("blocked_reason"),
            limit=MAX_SNAPSHOT_TEXT,
        ),
        auth_last_refresh=_optional_datetime(payload.get("auth_last_refresh")),
        auth_access_expires_at=_optional_datetime(payload.get("auth_access_expires_at")),
        auth_id_expires_at=_optional_datetime(payload.get("auth_id_expires_at")),
        source_urls=_snapshot_source_urls(payload.get("source_urls")),
        backend_configured=backend_fields["backend_configured"],
        backend_used=backend_fields["backend_used"],
        backend_user_id=backend_fields["backend_user_id"],
        backend_account_id=backend_fields["backend_account_id"],
        fallback_reason=_optional_snapshot_text(
            payload.get("fallback_reason"), limit=MAX_SNAPSHOT_TEXT
        ),
        values_captured_at=values_captured_at,
        stale=(
            raw_stale is True
            or stale_metadata_missing
            or stale_flag_invalid
            or status_missing
            or bool(metadata_errors)
            or cache_invalidated
            or forced_stale
        ),
        cache_invalidated=cache_invalidated,
        state_generation=state_generation,
    )


def merge_current_with_last_success(
    current: AccountUsage,
    last_success: AccountUsage | None,
) -> AccountUsage:
    if not isinstance(current, AccountUsage):
        raise ValueError("current usage is invalid")
    if last_success is not None and not isinstance(last_success, AccountUsage):
        raise ValueError("last success usage is invalid")
    if last_success is None:
        return current
    if current.status == AccountStatus.LOGIN_REQUIRED:
        return replace(
            current,
            five_hour=None,
            weekly=None,
            main=None,
            models=(),
            values_captured_at=None,
            stale=True,
            cache_invalidated=True,
        )
    if _authoritative_empty_limits(current):
        return current
    if not backend_identity_matches(current, last_success):
        return current
    if (
        not _backend_provenance_is_complete(current)
        or not _backend_provenance_is_complete(last_success)
    ):
        return current
    if not backend_provenance_matches(current, last_success):
        return current
    try:
        if last_success.captured_at > current.captured_at:
            if _has_complete_usage_windows(last_success):
                return last_success
            return _merge_newer_partial_usage(current, last_success)
    except TypeError:
        pass
    preserve_missing_window_values = _allow_missing_window_restore(current)
    current_values_captured_at = _values_capture_for_expiry(current)
    last_success_values_captured_at = _values_capture_for_expiry(last_success)
    five_hour = _merge_window_with_last_success(
        current.five_hour,
        last_success.five_hour,
        reference_at=current.captured_at,
        current_captured_at=current_values_captured_at,
        last_success_captured_at=last_success_values_captured_at,
        expected_kind="five_hour",
        preserve_missing_value=preserve_missing_window_values,
    )
    weekly = _merge_window_with_last_success(
        current.weekly,
        last_success.weekly,
        reference_at=current.captured_at,
        current_captured_at=current_values_captured_at,
        last_success_captured_at=last_success_values_captured_at,
        expected_kind="weekly",
        preserve_missing_value=preserve_missing_window_values,
    )
    main = _merge_pool_windows_with_last_success(
        current.main,
        last_success.main,
        reference_at=current.captured_at,
        current_captured_at=current_values_captured_at,
        last_success_captured_at=last_success_values_captured_at,
        preserve_missing_value=preserve_missing_window_values,
    )
    models = _merge_model_pools_with_last_success(
        current.models,
        last_success.models,
        reference_at=current.captured_at,
        current_captured_at=current_values_captured_at,
        last_success_captured_at=last_success_values_captured_at,
        preserve_missing_value=preserve_missing_window_values,
    )
    if (
        five_hour is current.five_hour
        and weekly is current.weekly
        and main is current.main
        and models is current.models
    ):
        return current
    return replace(
        current,
        five_hour=five_hour,
        weekly=weekly,
        main=main,
        models=models,
        values_captured_at=last_success.values_captured_at or last_success.captured_at,
        stale=True,
    )


def _has_complete_usage_windows(usage: AccountUsage) -> bool:
    if usage.main is not None:
        return isinstance(usage.main, UsagePool) and usage.main.has_valid_usage
    return bool(
        isinstance(usage.five_hour, LimitWindow)
        and isinstance(usage.weekly, LimitWindow)
        and usage.five_hour.has_usage_value
        and usage.weekly.has_usage_value
    )


def _merge_newer_partial_usage(
    older: AccountUsage,
    newer: AccountUsage,
) -> AccountUsage:
    preserve_missing_window_values = _allow_missing_window_restore(newer)
    older_values_captured_at = _values_capture_for_expiry(older)
    newer_values_captured_at = _values_capture_for_expiry(newer)
    five_hour = _merge_window_with_last_success(
        newer.five_hour,
        older.five_hour,
        reference_at=newer.captured_at,
        current_captured_at=newer_values_captured_at,
        last_success_captured_at=older_values_captured_at,
        expected_kind="five_hour",
        preserve_missing_value=preserve_missing_window_values,
    )
    weekly = _merge_window_with_last_success(
        newer.weekly,
        older.weekly,
        reference_at=newer.captured_at,
        current_captured_at=newer_values_captured_at,
        last_success_captured_at=older_values_captured_at,
        expected_kind="weekly",
        preserve_missing_value=preserve_missing_window_values,
    )
    main = _merge_pool_windows_with_last_success(
        newer.main,
        older.main,
        reference_at=newer.captured_at,
        current_captured_at=newer_values_captured_at,
        last_success_captured_at=older_values_captured_at,
        preserve_missing_value=preserve_missing_window_values,
    )
    models = _merge_model_pools_with_last_success(
        newer.models,
        older.models,
        reference_at=newer.captured_at,
        current_captured_at=newer_values_captured_at,
        last_success_captured_at=older_values_captured_at,
        preserve_missing_value=preserve_missing_window_values,
    )
    if (
        five_hour is newer.five_hour
        and weekly is newer.weekly
        and main is newer.main
        and models is newer.models
    ):
        return newer
    return replace(
        newer,
        five_hour=five_hour,
        weekly=weekly,
        main=main,
        models=models,
        values_captured_at=older.values_captured_at or older.captured_at,
        stale=True,
    )


def _allow_missing_window_restore(usage: AccountUsage) -> bool:
    if usage.backend_used == "browser" and _has_resetless_usage_window(usage):
        # AccountUsage has one capture timestamp for both windows. Restoring an
        # older counterpart here would make the fresh resetless value expire at
        # the older capture time as well.
        return False
    return not (
        usage.status == AccountStatus.PARTIAL
        and isinstance(usage.backend_used, str)
        and usage.backend_used in AUTHENTICATED_BACKENDS
    )


def _has_resetless_usage_window(usage: AccountUsage) -> bool:
    return any(
        isinstance(window, LimitWindow)
        and window.has_usage_value
        and window.reset_at is None
        for window in (usage.five_hour, usage.weekly)
    )


def _authoritative_empty_limits(usage: AccountUsage) -> bool:
    if usage.cache_invalidated and usage.five_hour is None and usage.weekly is None:
        return True
    if usage.status == AccountStatus.PARTIAL:
        return (
            usage.five_hour is None
            and usage.weekly is None
                and not _has_valid_core_usage(
                    usage.five_hour,
                    usage.weekly,
                    usage.main,
                )
                and isinstance(usage.backend_used, str)
                and usage.backend_used in {"direct", "app-server"}
            )
    return (
        usage.status == AccountStatus.ERROR
        and usage.cache_invalidated
        and usage.five_hour is None
        and usage.weekly is None
        and isinstance(usage.backend_used, str)
        and usage.backend_used in {"direct", "app-server"}
    )


def _merge_pool_windows_with_last_success(
    current: UsagePool | None,
    last_success: UsagePool | None,
    *,
    reference_at: datetime,
    current_captured_at: datetime | None,
    last_success_captured_at: datetime | None,
    preserve_missing_value: bool,
) -> UsagePool | None:
    if (
        current is None
        or last_success is None
        or not isinstance(current, UsagePool)
        or not isinstance(last_success, UsagePool)
        or not isinstance(current.windows, tuple)
        or not isinstance(last_success.windows, tuple)
        or not current.windows
        or not last_success.windows
    ):
        return current
    previous_by_identity = _window_identity_map(last_success.windows)
    if (
        _window_identity_map(current.windows) is None
        or previous_by_identity is None
    ):
        return current
    merged_windows = list(current.windows)
    changed = False
    for index, window in enumerate(current.windows):
        identity = _window_identity_key(window)
        if identity is None or identity not in previous_by_identity:
            continue
        merged = _merge_window_with_last_success(
            window,
            previous_by_identity[identity],
            reference_at=reference_at,
            current_captured_at=current_captured_at,
            last_success_captured_at=last_success_captured_at,
            preserve_missing_value=preserve_missing_value,
        )
        if merged is None:
            return current
        if merged is not window:
            merged_windows[index] = merged
            changed = True
    return replace(current, windows=tuple(merged_windows)) if changed else current


def _merge_model_pools_with_last_success(
    current: tuple[UsagePool, ...],
    last_success: tuple[UsagePool, ...],
    *,
    reference_at: datetime,
    current_captured_at: datetime | None,
    last_success_captured_at: datetime | None,
    preserve_missing_value: bool,
) -> tuple[UsagePool, ...]:
    if not isinstance(current, tuple) or not isinstance(last_success, tuple):
        return current
    previous_by_key: dict[str, UsagePool] = {}
    for pool in last_success:
        if (
            not isinstance(pool, UsagePool)
            or not isinstance(pool.key, str)
            or pool.key in previous_by_key
        ):
            return current
        previous_by_key[pool.key] = pool
    merged_pools: list[UsagePool] = []
    current_keys: set[str] = set()
    changed = False
    for pool in current:
        if (
            not isinstance(pool, UsagePool)
            or not isinstance(pool.key, str)
            or pool.key in current_keys
        ):
            return current
        current_keys.add(pool.key)
        merged = _merge_pool_windows_with_last_success(
            pool,
            previous_by_key.get(pool.key),
            reference_at=reference_at,
            current_captured_at=current_captured_at,
            last_success_captured_at=last_success_captured_at,
            preserve_missing_value=preserve_missing_value,
        )
        if merged is None:
            return current
        merged_pools.append(merged)
        changed = changed or merged is not pool
    return tuple(merged_pools) if changed else current


def _merge_window_with_last_success(
    current: LimitWindow | None,
    last_success: LimitWindow | None,
    *,
    reference_at: datetime,
    current_captured_at: datetime | None = None,
    last_success_captured_at: datetime | None = None,
    expected_kind: str | None = None,
    preserve_missing_value: bool = True,
) -> LimitWindow | None:
    if not _window_matches_expected_kind(current, expected_kind):
        return current
    if not _window_matches_expected_kind(last_success, expected_kind):
        return current
    if current is None:
        return (
            last_success
            if preserve_missing_value
            and not _cached_window_expired(
                last_success,
                captured_at=last_success_captured_at,
                reference_at=reference_at,
            )
            else None
        )
    if last_success is None:
        return current
    if not _window_duration_matches(current, last_success):
        return current
    if _is_inferred_inactive_five_hour(current):
        # An omitted paid-plan 5h bucket means 100% with no known reset. Never
        # revive the reset metadata of the previous active bucket.
        return current
    if not preserve_missing_value and not current.has_usage_value:
        return current
    if not current.has_usage_value and _cached_window_expired(
        current,
        captured_at=current_captured_at,
        reference_at=reference_at,
    ):
        # A newer reset-only observation can prove that the old window ended.
        return current
    if current.has_usage_value:
        if current.reset_at is None and last_success.reset_at is not None:
            if _cached_window_expired(
                last_success,
                captured_at=last_success_captured_at,
                reference_at=reference_at,
            ):
                return current
            return replace(current, reset_at=last_success.reset_at)
        return current
    if _cached_window_expired(
        last_success,
        captured_at=last_success_captured_at,
        reference_at=reference_at,
    ):
        return current
    if current.reset_at is None:
        return last_success
    return replace(last_success, reset_at=current.reset_at)


def _window_matches_expected_kind(
    window: LimitWindow | None,
    expected_kind: str | None,
) -> bool:
    if window is None or expected_kind is None:
        return True
    if expected_kind == "credits":
        return isinstance(window.name, str) and window.name.strip().casefold() == "credits"
    kind = _window_kind(window)
    if kind is not None and kind != expected_kind:
        return False
    duration = _window_duration_seconds(window)
    name = getattr(window, "name", None)
    if kind is None and isinstance(name, str) and name.strip():
        expected_duration = WINDOW_DURATIONS.get(expected_kind)
        return (
            window.has_known_identity
            and expected_duration is not None
            and duration == expected_duration
        )
    if kind is None and duration is None:
        return False
    expected_duration = WINDOW_DURATIONS.get(expected_kind)
    return (
        expected_duration is None
        or duration is None
        or duration == expected_duration
    )


def _window_identity_key(window: LimitWindow | None) -> int | None:
    if not isinstance(window, LimitWindow) or not window.has_known_identity:
        return None
    kind = _window_kind(window)
    duration = _window_duration_seconds(window)
    if kind is not None:
        expected_duration = WINDOW_DURATIONS.get(kind)
        if expected_duration is None or (
            duration is not None and duration != expected_duration
        ):
            return None
        return expected_duration
    if duration is None or duration > MAX_WINDOW_SECONDS:
        return None
    return duration


def _window_identity_map(
    windows: tuple[LimitWindow, ...],
) -> dict[int, LimitWindow] | None:
    identities: dict[int, LimitWindow] = {}
    for window in windows:
        identity = _window_identity_key(window)
        if identity is None or identity in identities:
            return None
        identities[identity] = window
    return identities


def _window_duration_matches(
    current: LimitWindow,
    last_success: LimitWindow,
) -> bool:
    current_kind = _window_kind(current)
    previous_kind = _window_kind(last_success)
    if bool(current_kind) != bool(previous_kind):
        return False
    if current_kind and previous_kind and current_kind != previous_kind:
        return False
    current_duration = _window_duration_seconds(current)
    previous_duration = _window_duration_seconds(last_success)
    expected_duration = WINDOW_DURATIONS.get(current_kind or previous_kind or "")
    if expected_duration is not None and any(
        duration is not None and duration != expected_duration
        for duration in (current_duration, previous_duration)
    ):
        return False
    return (
        current_duration is None
        or previous_duration is None
        or current_duration == previous_duration
    )


def _window_kind(window: LimitWindow | None) -> str | None:
    if window is None:
        return None
    name = getattr(window, "name", None)
    if not isinstance(name, str):
        return None
    normalized = re.sub(r"[-\s]+", "_", name.strip().casefold())
    if normalized in {"5h", "5_hour", "five_hour"}:
        if not window.has_known_identity:
            return None
        return "five_hour"
    if normalized in {"w", "week", "weekly"}:
        if not window.has_known_identity:
            return None
        return "weekly"
    if normalized in {"30d", "30_day", "month", "monthly"}:
        if not window.has_known_identity:
            return None
        return "thirty_day"
    duration = _window_duration_seconds(window)
    if normalized and not window.has_known_identity:
        return None
    if duration == WINDOW_DURATIONS["five_hour"]:
        return "five_hour"
    if duration == WINDOW_DURATIONS["weekly"]:
        return "weekly"
    return None


def _is_inferred_inactive_five_hour(window: LimitWindow | None) -> bool:
    return bool(
        window is not None
        and isinstance(window.source, str)
        and window.source in KNOWN_INFERRED_INACTIVE_FIVE_HOUR_SOURCES
    )


def _window_duration_seconds(window: LimitWindow | None) -> int | None:
    duration = getattr(window, "duration_seconds", None)
    if type(duration) is int and duration > 0:
        return duration
    raw = getattr(window, "raw", None)
    if not isinstance(raw, str):
        return None
    match = re.search(
        r'"limit_window_seconds"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        raw,
    )
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if (
        not math.isfinite(value)
        or value <= 0
        or value > MAX_WINDOW_SECONDS
        or not value.is_integer()
    ):
        return None
    return int(value)


def _window_reset_expired(window: LimitWindow | None, reference_at: datetime) -> bool:
    if window is None or window.reset_at is None:
        return False
    try:
        return _localize_datetime(window.reset_at) <= _localize_datetime(reference_at)
    except (AttributeError, OverflowError, TypeError, ValueError):
        return True


def _cached_window_expired(
    window: LimitWindow | None,
    *,
    captured_at: datetime | None,
    reference_at: datetime,
    expected_kind: str | None = None,
) -> bool:
    if window is None:
        return False
    if not isinstance(window, LimitWindow):
        return True
    if expected_kind is not None and not _window_matches_expected_kind(window, expected_kind):
        return True
    if _is_inferred_inactive_five_hour(window) and window.reset_at is None:
        # This is a plan-level inactive bucket, not a resetless active window.
        # Keep the explicit 100% observation until a fresh response replaces it.
        return False
    if window.reset_at is not None:
        if _window_reset_expired(window, reference_at):
            return True
        duration = _window_duration_seconds(window)
        if duration is None:
            duration = WINDOW_DURATIONS.get(_window_kind(window) or "")
        if duration is None:
            # A reset without a trusted duration cannot prove cache freshness.
            return True
        if not isinstance(captured_at, datetime) or not isinstance(window.reset_at, datetime):
            return True
        try:
            captured_utc = _localize_datetime(captured_at).astimezone(UTC)
            reset_utc = _localize_datetime(window.reset_at).astimezone(UTC)
            return reset_utc > captured_utc + timedelta(
                seconds=duration + MAX_RESET_FUTURE_SKEW_SECONDS
            )
        except Exception:
            return True
    duration = _window_duration_seconds(window)
    if duration is None:
        duration = WINDOW_DURATIONS.get(_window_kind(window) or "")
    if duration is None or not isinstance(captured_at, datetime):
        return True
    try:
        captured_utc = _localize_datetime(captured_at).astimezone(UTC)
        reference_utc = _localize_datetime(reference_at).astimezone(UTC)
        return (
            captured_utc + timedelta(seconds=duration)
            <= reference_utc
        )
    except Exception:
        return True


def _localize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=LOCAL_TZ)
    try:
        offset = value.utcoffset()
    except Exception:
        return value.replace(tzinfo=LOCAL_TZ)
    if offset is None:
        return value.replace(tzinfo=LOCAL_TZ)
    return value


def _values_capture_for_expiry(usage: AccountUsage) -> datetime:
    candidate = (
        _localize_datetime(usage.values_captured_at)
        if isinstance(usage.values_captured_at, datetime)
        else None
    )
    if not isinstance(usage.captured_at, datetime):
        return datetime.min.replace(tzinfo=UTC)
    captured_at = _localize_datetime(usage.captured_at)
    if candidate is None:
        return captured_at
    try:
        if candidate <= captured_at:
            return candidate
    except Exception:
        return captured_at
    return captured_at


def backend_identity_matches(left: AccountUsage, right: AccountUsage) -> bool:
    if not isinstance(left, AccountUsage) or not isinstance(right, AccountUsage):
        return False
    if (
        left.backend_used is not None
        and not isinstance(left.backend_used, str)
    ) or (
        right.backend_used is not None
        and not isinstance(right.backend_used, str)
    ):
        return False
    if (
        left.backend_used in AUTHENTICATED_BACKENDS
        and right.backend_used in AUTHENTICATED_BACKENDS
        and not any(
            (
                left.backend_account_id,
                right.backend_account_id,
                left.backend_user_id,
                right.backend_user_id,
            )
        )
    ):
        # An explicit authenticated backend without identity proof must not
        # restore values captured before a possible account switch.
        return False
    if (
        left.backend_used in AUTHENTICATED_BACKENDS
        and right.backend_used in AUTHENTICATED_BACKENDS
        and not (left.backend_account_id and right.backend_account_id)
    ):
        return False
    if left.backend_used == "browser" and right.backend_used == "browser":
        if not (left.backend_user_id or left.backend_account_id):
            return False
        if not (right.backend_user_id or right.backend_account_id):
            return False
        if bool(left.backend_account_id) != bool(right.backend_account_id):
            # A shared browser user ID cannot bind an account-bound snapshot
            # to a user-only snapshot when multiple accounts may share it.
            return False
    left_account_id = left.backend_account_id
    right_account_id = right.backend_account_id
    if left_account_id:
        if left_account_id != right_account_id:
            return False
        if left.backend_user_id and right.backend_user_id:
            return left.backend_user_id == right.backend_user_id
        return True

    return left.backend_user_id == right.backend_user_id


def _window_from_dict(
    payload: dict[str, Any] | None,
    *,
    expected_kind: str | None = None,
) -> LimitWindow | None:
    if not isinstance(payload, dict):
        return None
    raw_duration = payload.get("duration_seconds")
    if raw_duration is not None and _snapshot_window_duration(raw_duration) is None:
        return None
    raw_reset_at = payload.get("reset_at")
    reset_at = (
        None
        if raw_reset_at is None or raw_reset_at == ""
        else _snapshot_datetime(raw_reset_at)
    )
    raw_source = payload.get("source")
    source: str
    if raw_source is None or raw_source == "":
        source = "unknown"
    else:
        validated_source = _optional_snapshot_identity(raw_source, limit=120)
        if validated_source is None:
            raise ValueError("snapshot window source is invalid")
        source = validated_source
    window = LimitWindow(
        name=_snapshot_window_name(payload.get("name")),
        used=_optional_float(payload.get("used")),
        limit=_optional_float(payload.get("limit")),
        remaining=_optional_float(payload.get("remaining")),
        percent=_optional_float(payload.get("percent")),
        reset_at=reset_at,
        raw=_optional_snapshot_text(payload.get("raw"), limit=MAX_SNAPSHOT_TEXT),
        source=source,
        duration_seconds=_snapshot_window_duration(raw_duration),
    )
    if any(
        _snapshot_number_is_invalid(payload, field)
        for field in ("used", "limit", "remaining", "percent")
    ):
        window = replace(window, used=None, limit=None, remaining=None, percent=None)
    if window.percent is not None and not 0 <= window.percent <= 100:
        # Explicit percentages outside the display domain are not usage
        # values. Absolute fields can still provide a trustworthy result.
        window = replace(window, percent=None)
    if window.used is not None and window.used < 0:
        # Do not let an unqualified remaining counter survive an invalid
        # absolute usage pair and become a plausible cached percentage.
        window = replace(window, used=None, remaining=None, percent=None)
    if window.limit is not None and window.limit <= 0:
        window = replace(window, used=None, limit=None, remaining=None)
    if window.remaining is not None and window.remaining < 0:
        window = replace(window, used=None, remaining=None, percent=None)
    if (
        window.limit is not None
        and window.limit > 0
        and window.remaining is not None
        and window.remaining > window.limit
    ):
        window = replace(window, used=None, remaining=None, percent=None)
    if (
        expected_kind != "credits" and
        (window.limit is None or window.limit <= 0)
        and window.remaining is not None and not 0 <= window.remaining <= 100
    ):
        # Without a positive denominator, values outside the percentage range
        # are ambiguous absolute counters and must not survive cache loading.
        window = replace(window, remaining=None)
    if expected_kind is not None and not _window_matches_expected_kind(window, expected_kind):
        return None
    return window


def _has_valid_core_usage(
    five_hour: LimitWindow | None,
    weekly: LimitWindow | None,
    main: UsagePool | None,
) -> bool:
    if main is None:
        return any(
            isinstance(window, LimitWindow) and window.has_usage_value
            for window in (five_hour, weekly)
        )
    return isinstance(main, UsagePool) and main.has_valid_usage


def _pool_from_dict(
    payload: Any,
    *,
    expected_key: str | None = None,
) -> UsagePool | None:
    if not isinstance(payload, dict):
        return None
    key = (
        expected_key
        if "key" not in payload
        else _optional_snapshot_identity(payload.get("key"), limit=120)
    )
    if not key or (expected_key is not None and key != expected_key):
        return None
    raw_windows = payload.get("windows")
    if not isinstance(raw_windows, list) or len(raw_windows) > MAX_POOL_WINDOWS:
        return None
    windows: list[LimitWindow] = []
    for raw_window in raw_windows:
        window = _window_from_dict(raw_window)
        if window is None:
            return None
        windows.append(window)
    if "availability_sources" not in payload:
        sources: tuple[str, ...] = ()
    else:
        raw_sources = payload.get("availability_sources")
        if (
            not isinstance(raw_sources, list)
            or len(raw_sources) > 8
            or any(not isinstance(value, str) or not value.strip() for value in raw_sources)
        ):
            return None
        sources = tuple(_snapshot_text(value, limit=40) for value in raw_sources)
    available = payload.get("available")
    if not isinstance(available, bool):
        return None
    raw_allowed = payload.get("allowed")
    raw_limit_reached = payload.get("limit_reached")
    raw_exhausted = payload.get("exhausted")
    control_flags_valid = all(
        value is None or isinstance(value, bool)
        for value in (raw_allowed, raw_limit_reached, raw_exhausted)
    )
    pool = UsagePool(
        key=key,
        display_name=_snapshot_text_or_default(
            payload.get("display_name"), key, limit=120
        ),
        windows=tuple(windows),
        # Invalid control flags cannot prove availability. Disable pool.
        available=available and control_flags_valid,
        allowed=raw_allowed if isinstance(raw_allowed, bool) else None,
        limit_reached=(
            raw_limit_reached if isinstance(raw_limit_reached, bool) else None
        ),
        metered_feature=_optional_snapshot_text(
            payload.get("metered_feature"), limit=120
        ),
        availability_sources=tuple(dict.fromkeys(sources)),
    )
    if windows and _window_identity_map(tuple(windows)) is None:
        pool = replace(pool, available=False)
    if isinstance(raw_exhausted, bool) and raw_exhausted != pool.exhausted:
        # The derived flag may not contradict the actual limit fields.
        pool = replace(pool, available=False)
    return pool


def _model_pools_from_dict(payload: Any) -> tuple[UsagePool, ...] | None:
    if not isinstance(payload, dict) or len(payload) > MAX_MODEL_POOLS:
        return None
    pools: list[UsagePool] = []
    normalized_keys: set[str] = set()
    for raw_key, raw_pool in payload.items():
        key = _optional_snapshot_identity(raw_key, limit=120)
        if not key:
            return None
        normalized_key = key.casefold()
        if normalized_key in normalized_keys:
            return None
        normalized_keys.add(normalized_key)
        if (
            not isinstance(raw_pool, dict)
            or "exhausted" not in raw_pool
        ):
            return None
        pool = _pool_from_dict(raw_pool, expected_key=key)
        if pool is None:
            return None
        pools.append(pool)
    return tuple(pools)


def _snapshot_window_duration(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        return None
    return value if 0 < value <= MAX_WINDOW_SECONDS else None


def _snapshot_window_name(value: Any) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > 40 or any(
        char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F
        for char in value
    ):
        raise ValueError("snapshot window name is invalid")
    return value


def _window_had_invalid_cached_value(
    payload: dict[str, Any] | None,
    window: LimitWindow | None,
) -> bool:
    if not isinstance(payload, dict) or window is None:
        return False
    if any(
        _snapshot_number_is_invalid(payload, field)
        for field in ("used", "limit", "remaining", "percent")
    ):
        return True
    raw_used = _optional_float(payload.get("used"))
    raw_limit = _optional_float(payload.get("limit"))
    raw_remaining = _optional_float(payload.get("remaining"))
    if raw_remaining is not None and raw_remaining < 0:
        return True
    if raw_used is not None and raw_used < 0:
        return not window.has_usage_value
    if raw_limit is not None and raw_limit <= 0:
        raw_percent = _optional_float(payload.get("percent"))
        if raw_percent is not None and 0 <= raw_percent <= 100:
            return False
        return not window.has_usage_value
    raw_percent = _optional_float(payload.get("percent"))
    if raw_percent is not None and not 0 <= raw_percent <= 100:
        return True
    if (
        raw_remaining is not None
        and raw_limit is not None
        and raw_limit > 0
        and raw_remaining > raw_limit
    ):
        return True
    if raw_remaining is None or 0 <= raw_remaining <= 100:
        return False
    if window.limit is not None and window.limit > 0:
        return False
    return window.remaining is None and window.percent is None


def _snapshot_number_is_invalid(payload: dict[str, Any], field: str) -> bool:
    if field not in payload or payload[field] is None:
        return False
    value = payload[field]
    if type(value) not in (int, float):
        return True
    try:
        return not math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return True


def _optional_float(value: Any) -> float | None:
    if value is None or type(value) not in (int, float):
        return None
    try:
        coerced = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return coerced if math.isfinite(coerced) else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_state_generation(value: Any) -> int | None:
    if type(value) is not int or value < 0:
        return None
    return value


def _optional_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _snapshot_datetime(value)
    except ValueError:
        return None


def _snapshot_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("snapshot datetime must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=LOCAL_TZ)
    return parsed


def _saved_datetime(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("captured_at must be a datetime")
    return _localize_datetime(value)


def _snapshot_text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError("snapshot text must be a string")
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _snapshot_text_or_default(value: Any, default: str, *, limit: int) -> str:
    if value is None or value == "":
        return default
    return _snapshot_text(value, limit=limit)


def _optional_snapshot_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return _snapshot_text(value, limit=limit)


def _optional_snapshot_identity(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(
        char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F
        for char in value
    ):
        return None
    return value


def _snapshot_source_urls(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or len(value) > MAX_SNAPSHOT_URLS
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError("invalid snapshot source URLs")
    return tuple(_snapshot_text(item, limit=300) for item in value)


def _validate_snapshot_account_id(account_id: object) -> None:
    if (
        not isinstance(account_id, str)
        or account_id in {".", ".."}
        or not SNAPSHOT_ACCOUNT_ID_RE.fullmatch(account_id)
    ):
        raise ValueError("account id must be 1-64 chars: letters, digits, underscore, dot, dash")
