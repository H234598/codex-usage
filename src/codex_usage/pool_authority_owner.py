"""Config-owned PoolAuthorityV2 source materialization.

The producer consumes only the generated source JSON.  This module is the
single bridge from the explicit ``config.toml`` owner inputs to that source;
it intentionally has no access to usage observations or account metadata
beyond the configured account-ID inventory required for parity checking.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path

from .config import (
    MAX_POOL_AUTHORITY_GENERATION,
    AppConfig,
    PoolAuthorityConfig,
    PoolAuthorityOwner,
    _prepare_config_directory,
    _save_config_unlocked,
    _validate_config,
    _validate_pool_authority_config,
    default_config_path,
    load_config,
)
from .integration_pool_authority import (
    POOL_AUTHORITY_PENDING_FILENAME,
    POOL_AUTHORITY_SOURCE_FILENAME,
    POOL_AUTHORITY_SOURCE_MAX_BYTES,
    PoolAuthorityInvalid,
    serialize_pool_authority_source,
)
from .private_io import (
    assert_no_symlink_ancestors,
    ensure_private_directory,
    private_path_lock,
    read_private_text,
    write_private_text,
)
from .private_io import (
    write_private_text as _write_pending_text,
)


@dataclass(frozen=True)
class PoolAuthorityOwnerSnapshot:
    generation: int
    account_ids: tuple[str, ...]
    authorities: tuple[PoolAuthorityOwner, ...]


def default_pool_authority_state_home() -> Path:
    value = os.environ.get("XDG_STATE_HOME")
    if value:
        try:
            candidate = Path(value).expanduser()
        except RuntimeError:
            candidate = Path.home() / ".local" / "state"
        if candidate.is_absolute():
            return candidate
    return Path.home() / ".local" / "state"


def pool_authority_source_path(state_home: Path | None = None) -> Path:
    root = _select_state_home(state_home)
    return root / "codex-usage" / "integration" / POOL_AUTHORITY_SOURCE_FILENAME


def pool_authority_pending_path(state_home: Path | None = None) -> Path:
    return pool_authority_source_path(state_home).with_name(POOL_AUTHORITY_PENDING_FILENAME)


def load_pool_authority_owner(
    *,
    config_path: Path | None = None,
) -> PoolAuthorityOwnerSnapshot:
    recover_pool_authority_pending(config_path=config_path)
    config = load_config(_select_config_path(config_path))
    owner = config.pool_authority
    return PoolAuthorityOwnerSnapshot(
        generation=owner.generation,
        account_ids=tuple(sorted(account.id for account in config.accounts)),
        authorities=owner.authorities,
    )


def save_pool_authority_owner(
    authorities: object,
    *,
    expected_generation: int,
    config_path: Path | None = None,
    state_home: Path | None = None,
) -> PoolAuthorityOwnerSnapshot:
    """Atomically commit a complete config generation, then replace its source.

    All source bytes are validated before either file can change.  A source
    replacement failure rolls the config back while both writes remain under
    the config lock, so a newer config generation cannot be left paired with
    the old source.
    """
    candidate_authorities = _canonical_authorities(authorities)
    _validate_expected_generation(expected_generation)
    config_file = _select_config_path(config_path)
    source_file = pool_authority_source_path(state_home)
    pending_file = pool_authority_pending_path(state_home)
    recover_pool_authority_pending(config_path=config_file, state_home=state_home)
    # The unlocked pass is only a no-mutation preflight. The same checks run
    # again under the config lock before either target is prepared or replaced.
    _updated_config(
        load_config(config_file),
        candidate_authorities=candidate_authorities,
        expected_generation=expected_generation,
    )
    source_text = _source_text(candidate_authorities)
    _prepare_config_directory(config_file.parent)

    with private_path_lock(config_file, label="config lock"):
        previous = load_config(config_file)
        updated = _updated_config(
            previous,
            candidate_authorities=candidate_authorities,
            expected_generation=expected_generation,
        )

        # No source path is created or touched until the entire next config
        # generation and its exact producer payload are known to be valid.
        _prepare_config_directory(config_file.parent)
        _prepare_source_directory(source_file)
        with private_path_lock(source_file, label="pool authority source lock"):
            _assert_safe_source_target(source_file)
            _write_pending_publish(
                pending_file,
                updated.pool_authority,
                expected_generation=previous.pool_authority.generation,
                config_path=config_file,
            )
            _save_config_unlocked(updated, config_file)
            try:
                write_private_text(
                    source_file,
                    source_text,
                    label="pool authority source",
                    mode=0o600,
                )
            except Exception as source_error:
                try:
                    _save_config_unlocked(previous, config_file)
                except Exception as rollback_error:
                    raise ExceptionGroup(
                        "pool authority save rollback failed",
                        [source_error, rollback_error],
                    ) from None
                _remove_pending(pending_file)
                raise ValueError("could not materialize pool authority source") from source_error
            _remove_pending(pending_file)

    return PoolAuthorityOwnerSnapshot(
        generation=updated.pool_authority.generation,
        account_ids=tuple(authority.account_id for authority in candidate_authorities),
        authorities=candidate_authorities,
    )


def _select_config_path(path: Path | None) -> Path:
    if path is not None and not isinstance(path, Path):
        raise ValueError("config path must be a Path")
    return path or default_config_path()


def _select_state_home(state_home: Path | None) -> Path:
    if state_home is not None and not isinstance(state_home, Path):
        raise ValueError("state_home must be a Path")
    selected = state_home or default_pool_authority_state_home()
    if not selected.is_absolute():
        raise ValueError("state_home must be absolute")
    return selected


def _canonical_authorities(value: object) -> tuple[PoolAuthorityOwner, ...]:
    if type(value) not in (list, tuple):
        raise ValueError("pool authority authorities must be a list or tuple")
    entries = tuple(value)
    if any(type(entry) is not PoolAuthorityOwner for entry in entries):
        raise ValueError("pool authority entries must be PoolAuthorityOwner")
    sorted_entries = tuple(sorted(entries, key=lambda entry: entry.account_id))
    owner = PoolAuthorityConfig(
        generation=0,
        authorities=sorted_entries,
        configured=True,
    )
    _validate_pool_authority_config(owner)
    return sorted_entries


def _validate_expected_generation(value: object) -> None:
    if type(value) is not int or not 0 <= value <= MAX_POOL_AUTHORITY_GENERATION:
        raise ValueError("expected_generation must be a bounded integer")


def _validate_account_inventory(
    config: AppConfig,
    authorities: tuple[PoolAuthorityOwner, ...],
) -> None:
    expected = tuple(sorted(account.id for account in config.accounts))
    actual = tuple(authority.account_id for authority in authorities)
    if actual != expected:
        raise ValueError("pool authority account inventory must exactly match config accounts")


def _updated_config(
    config: AppConfig,
    *,
    candidate_authorities: tuple[PoolAuthorityOwner, ...],
    expected_generation: int,
) -> AppConfig:
    if config.pool_authority.generation != expected_generation:
        raise ValueError("pool authority generation is stale")
    _validate_account_inventory(config, candidate_authorities)
    next_generation = expected_generation + 1
    if next_generation > MAX_POOL_AUTHORITY_GENERATION:
        raise ValueError("pool authority generation is exhausted")
    next_owner = PoolAuthorityConfig(
        generation=next_generation,
        authorities=candidate_authorities,
        configured=True,
    )
    _validate_pool_authority_config(next_owner)
    updated = replace(config, pool_authority=next_owner)
    _validate_config(updated)
    return updated


def _source_text(authorities: tuple[PoolAuthorityOwner, ...]) -> str:
    source = {
        "pool_authority_source_schema_version": 2,
        "authorities": [authority.to_source_record() for authority in authorities],
    }
    try:
        payload = serialize_pool_authority_source(source)
    except PoolAuthorityInvalid as exc:  # defensive: config validates first
        raise ValueError("pool authority source is invalid") from exc
    return payload.decode("ascii")


def _prepare_source_directory(source_file: Path) -> None:
    state_home = source_file.parents[2]
    app_directory = source_file.parents[1]
    integration_directory = source_file.parent
    for path, label in (
        (state_home, "pool authority state home"),
        (app_directory, "pool authority state directory"),
        (integration_directory, "pool authority integration directory"),
    ):
        assert_no_symlink_ancestors(path, label=label)
        if path.is_symlink():
            raise ValueError(f"{label} must not be a symlink")
        ensure_private_directory(path, label=label)


def begin_account_set_invalidation(
    *, previous: AppConfig, updated: AppConfig, config_path: Path
) -> Path:
    source_file = pool_authority_source_path()
    pending_file = pool_authority_pending_path()
    _prepare_source_directory(source_file)
    with private_path_lock(source_file, label="pool authority source lock"):
        if pending_file.exists():
            raise ValueError("pool authority pending already exists")
        _write_pending(pending_file, _invalidation_marker(previous, updated, config_path))
    return pending_file


def finish_account_set_invalidation(
    *, previous: AppConfig, updated: AppConfig, config_path: Path
) -> None:
    source_file = pool_authority_source_path()
    pending_file = pool_authority_pending_path()
    with private_path_lock(config_path, label="config lock"):
        with private_path_lock(source_file, label="pool authority source lock"):
            marker = _load_pending(pending_file, config_path)
            if not _matches_invalidation(marker, previous, updated):
                raise ValueError("pool authority pending is invalid")
            if load_config(config_path) != updated:
                raise ValueError("pool authority pending does not match config")
            _remove_source(source_file)
            _remove_pending(pending_file)


def recover_pool_authority_pending(
    *, config_path: Path | None = None, state_home: Path | None = None
) -> None:
    pending_file = pool_authority_pending_path(state_home)
    try:
        pending_file.lstat()
    except FileNotFoundError:
        return
    try:
        payload, _ = read_private_text(
            pending_file,
            regular_label="pool authority pending",
            read_label="pool authority pending",
            max_bytes=POOL_AUTHORITY_SOURCE_MAX_BYTES,
            too_large_label="pool authority pending",
            invalid_utf8_label="pool authority pending",
        )
    except FileNotFoundError:
        return
    config_file = _select_config_path(config_path)
    source_file = pool_authority_source_path(state_home)
    marker = _parse_pending(payload, config_file)
    _prepare_config_directory(config_file.parent)
    with private_path_lock(config_file, label="config lock"):
        with private_path_lock(source_file, label="pool authority source lock"):
            config = load_config(config_file)
            if marker["operation"] == "invalidate":
                if (
                    _account_ids(config) == marker["old_account_ids"]
                    and config.pool_authority == marker["old_authority"]
                ):
                    _remove_pending(pending_file)
                    return
                if (
                    _account_ids(config) != marker["new_account_ids"]
                    or config.pool_authority.configured
                ):
                    raise ValueError("pool authority pending does not match config")
                _remove_source(source_file)
            else:
                records = marker["authorities"]
                generation = marker["generation"]
                expected_generation = marker["expected_generation"]
                expected = PoolAuthorityConfig(
                    generation=generation, authorities=records, configured=True
                )
                _validate_pool_authority_config(expected)
                if config.pool_authority == expected:
                    pass
                elif config.pool_authority.generation == expected_generation:
                    _validate_account_inventory(config, records)
                    _save_config_unlocked(replace(config, pool_authority=expected), config_file)
                else:
                    raise ValueError("pool authority pending does not match config")
                _prepare_source_directory(source_file)
                _assert_safe_source_target(source_file)
                write_private_text(
                    source_file, _source_text(records), label="pool authority source", mode=0o600
                )
            _remove_pending(pending_file)


def _write_pending_publish(
    path: Path,
    authority: PoolAuthorityConfig,
    *,
    expected_generation: int,
    config_path: Path,
) -> None:
    _write_pending(
        path,
        {
            "schema_version": 1,
            "operation": "publish",
            "config_path": _config_binding(config_path),
            "expected_generation": expected_generation,
            "generation": authority.generation,
            "authorities": [item.to_source_record() for item in authority.authorities],
        },
    )


def _write_pending(path: Path, marker: dict[str, object]) -> None:
    text = json.dumps(marker, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    _write_pending_text(path, text, label="pool authority pending", mode=0o600)


def _account_ids(config: AppConfig) -> tuple[str, ...]:
    return tuple(sorted(account.id for account in config.accounts))


def _invalidation_marker(
    previous: AppConfig, updated: AppConfig, config_path: Path
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "operation": "invalidate",
        "config_path": _config_binding(config_path),
        "old_account_ids": list(_account_ids(previous)),
        "new_account_ids": list(_account_ids(updated)),
        "old_generation": previous.pool_authority.generation,
        "old_authorities": [
            item.to_source_record() for item in previous.pool_authority.authorities
        ],
    }


def _matches_invalidation(
    marker: dict[str, object], previous: AppConfig, updated: AppConfig
) -> bool:
    return (
        marker.get("operation") == "invalidate"
        and marker.get("old_account_ids") == _account_ids(previous)
        and marker.get("new_account_ids") == _account_ids(updated)
        and marker.get("old_authority") == previous.pool_authority
    )


def _load_pending(path: Path, config_path: Path) -> dict[str, object]:
    payload, _ = read_private_text(
        path,
        regular_label="pool authority pending",
        read_label="pool authority pending",
        max_bytes=POOL_AUTHORITY_SOURCE_MAX_BYTES,
        too_large_label="pool authority pending",
        invalid_utf8_label="pool authority pending",
    )
    return _parse_pending(payload, config_path)


def _config_binding(path: Path) -> str:
    if not isinstance(path, Path):
        raise ValueError("pool authority pending is invalid")
    try:
        resolved = path.expanduser().absolute()
    except RuntimeError as exc:
        raise ValueError("pool authority pending is invalid") from exc
    rendered = str(resolved)
    if not resolved.is_absolute() or len(rendered) > 4096 or "\x00" in rendered:
        raise ValueError("pool authority pending is invalid")
    return rendered


def _parse_pending(payload: str, config_path: Path) -> dict[str, object]:
    try:
        marker = json.loads(payload)
    except (TypeError, ValueError):
        raise ValueError("pool authority pending is invalid") from None
    if type(marker) is not dict or marker.get("schema_version") != 1:
        raise ValueError("pool authority pending is invalid")
    operation = marker.get("operation")
    if type(operation) is not str or marker.get("config_path") != _config_binding(config_path):
        raise ValueError("pool authority pending is bound to another config")
    invalidation_fields = {
        "schema_version",
        "operation",
        "config_path",
        "old_account_ids",
        "new_account_ids",
        "old_generation",
        "old_authorities",
    }
    if operation == "invalidate" and set(marker) == invalidation_fields:
        old_ids, new_ids = marker["old_account_ids"], marker["new_account_ids"]
        generation, raw = marker["old_generation"], marker["old_authorities"]
        if (
            type(old_ids) is not list
            or type(new_ids) is not list
            or type(generation) is not int
            or type(raw) is not list
        ):
            raise ValueError("pool authority pending is invalid")
        old_authorities = _records_from_pending(raw, generation)
        if tuple(old_ids) != tuple(sorted(old_ids)) or tuple(new_ids) != tuple(sorted(new_ids)):
            raise ValueError("pool authority pending is invalid")
        if tuple(item.account_id for item in old_authorities) != tuple(old_ids):
            raise ValueError("pool authority pending is invalid")
        return {
            "operation": operation,
            "old_account_ids": tuple(old_ids),
            "new_account_ids": tuple(new_ids),
            "old_authority": PoolAuthorityConfig(generation, old_authorities, True),
        }
    required = {
        "schema_version",
        "operation",
        "config_path",
        "expected_generation",
        "generation",
        "authorities",
    }
    if operation != "publish" or set(marker) != required:
        raise ValueError("pool authority pending is invalid")
    expected_generation = marker["expected_generation"]
    generation = marker["generation"]
    raw_authorities = marker["authorities"]
    if (
        type(expected_generation) is not int
        or type(generation) is not int
        or isinstance(expected_generation, bool)
        or isinstance(generation, bool)
        or not 0 <= expected_generation < MAX_POOL_AUTHORITY_GENERATION
        or not 1 <= generation <= MAX_POOL_AUTHORITY_GENERATION
        or generation != expected_generation + 1
        or type(raw_authorities) is not list
    ):
        raise ValueError("pool authority pending is invalid")
    authorities = _records_from_pending(raw_authorities, generation)
    return {
        "operation": operation,
        "generation": generation,
        "expected_generation": expected_generation,
        "authorities": authorities,
    }


def _records_from_pending(
    raw_authorities: list[object], generation: int
) -> tuple[PoolAuthorityOwner, ...]:
    try:
        authorities = tuple(
            PoolAuthorityOwner(
                account_id=item["account_id"],
                pool_id=item["pool_id"],
                provider=item["provider"],
                hive_available=item["hive_available"],
                allowed_model_families=tuple(item["allowed_model_families"]),
                reasoning_minimum=item["reasoning_minimum"],
                reasoning_maximum=item["reasoning_maximum"],
                allowed_lifecycles=tuple(item["allowed_lifecycles"]),
                persistent_leadership_eligible=item["persistent_leadership_eligible"],
                long_running_leadership_eligible=item["long_running_leadership_eligible"],
            )
            for item in raw_authorities
            if type(item) is dict
            and set(item)
            == {
                "account_id",
                "pool_id",
                "provider",
                "hive_available",
                "allowed_model_families",
                "reasoning_minimum",
                "reasoning_maximum",
                "allowed_lifecycles",
                "persistent_leadership_eligible",
                "long_running_leadership_eligible",
            }
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("pool authority pending is invalid") from None
    if len(authorities) != len(raw_authorities):
        raise ValueError("pool authority pending is invalid")
    _validate_pool_authority_config(PoolAuthorityConfig(generation, authorities, True))
    return authorities


def _remove_source(path: Path) -> None:
    _assert_safe_source_target(path)
    if path.exists():
        path.unlink()


def _remove_pending(path: Path) -> None:
    if path.exists():
        _assert_safe_source_target(path)
        path.unlink()


def _assert_safe_source_target(source_file: Path) -> None:
    assert_no_symlink_ancestors(source_file, label="pool authority source")
    try:
        item = source_file.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError("pool authority source is unavailable") from exc
    if (
        stat.S_ISLNK(item.st_mode)
        or not stat.S_ISREG(item.st_mode)
        or item.st_uid != os.geteuid()
        or item.st_nlink != 1
        or stat.S_IMODE(item.st_mode) != 0o600
    ):
        raise ValueError("pool authority source must be a private regular file")
