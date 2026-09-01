"""Config-owned PoolAuthorityV2 source materialization.

The producer consumes only the generated source JSON.  This module is the
single bridge from the explicit ``config.toml`` owner inputs to that source;
it intentionally has no access to usage observations or account metadata
beyond the configured account-ID inventory required for parity checking.
"""

from __future__ import annotations

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
    POOL_AUTHORITY_SOURCE_FILENAME,
    PoolAuthorityInvalid,
    serialize_pool_authority_source,
)
from .private_io import (
    assert_no_symlink_ancestors,
    ensure_private_directory,
    private_path_lock,
    write_private_text,
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


def load_pool_authority_owner(
    *,
    config_path: Path | None = None,
) -> PoolAuthorityOwnerSnapshot:
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
                raise ValueError("could not materialize pool authority source") from source_error

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
