from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from codex_usage.config import (
    AppConfig,
    add_or_update_account,
    load_config,
    save_config,
)
from codex_usage.integration_pool_authority import parse_pool_authority_source
from codex_usage.models import Account
from codex_usage.pool_authority_owner import (
    PoolAuthorityOwner,
    load_pool_authority_owner,
    save_pool_authority_owner,
)


def _account(account_id: str, root: Path) -> Account:
    return Account(
        id=account_id,
        label=account_id,
        profile_dir=str(root / "profiles" / account_id),
    )


def _authority(account_id: str) -> PoolAuthorityOwner:
    return PoolAuthorityOwner(
        account_id=account_id,
        pool_id=f"pool-{account_id}",
        provider="synthetic",
        hive_available=True,
        allowed_model_families=("synthetic-model",),
        reasoning_minimum="low",
        reasoning_maximum="high",
        allowed_lifecycles=("ephemeral", "persistent"),
        persistent_leadership_eligible=True,
        long_running_leadership_eligible=False,
    )


def _source_path(state_home: Path) -> Path:
    return state_home / "codex-usage" / "integration" / "pool-authority-source-v2.json"


def _save_complete_authority(
    tmp_path: Path,
    *,
    account_ids: tuple[str, ...] = ("alpha",),
) -> tuple[Path, Path, object]:
    config_path = tmp_path / "config" / "config.toml"
    state_home = tmp_path / "state"
    save_config(
        AppConfig(accounts=tuple(_account(account_id, tmp_path) for account_id in account_ids)),
        config_path,
    )
    saved = save_pool_authority_owner(
        tuple(_authority(account_id) for account_id in account_ids),
        expected_generation=0,
        config_path=config_path,
        state_home=state_home,
    )
    return config_path, state_home, saved


def test_save_owner_round_trips_canonical_complete_source(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "config.toml"
    state_home = tmp_path / "state"
    save_config(
        AppConfig(accounts=(_account("bravo", tmp_path), _account("alpha", tmp_path))),
        config_path,
    )

    saved = save_pool_authority_owner(
        (_authority("bravo"), _authority("alpha")),
        expected_generation=0,
        config_path=config_path,
        state_home=state_home,
    )

    assert saved.generation == 1
    assert [item.account_id for item in saved.authorities] == ["alpha", "bravo"]
    assert load_pool_authority_owner(config_path=config_path) == saved
    source_path = _source_path(state_home)
    source = json.loads(source_path.read_text(encoding="utf-8"))
    assert source == {
        "authorities": [item.to_source_record() for item in saved.authorities],
        "pool_authority_source_schema_version": 2,
    }
    source_stat = source_path.stat()
    assert stat.S_ISREG(source_stat.st_mode)
    assert source_stat.st_uid == os.geteuid()
    assert stat.S_IMODE(source_stat.st_mode) == 0o600
    assert parse_pool_authority_source(source_path.read_bytes()) == source


def test_load_owner_exposes_only_the_sorted_config_account_inventory(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "config.toml"
    save_config(
        AppConfig(accounts=(_account("bravo", tmp_path), _account("alpha", tmp_path))),
        config_path,
    )

    snapshot = load_pool_authority_owner(config_path=config_path)

    assert snapshot.generation == 0
    assert snapshot.account_ids == ("alpha", "bravo")
    assert snapshot.authorities == ()


def test_invalid_inventory_does_not_prepare_or_mutate_source_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config" / "config.toml"
    state_home = tmp_path / "state"
    save_config(AppConfig(accounts=(_account("alpha", tmp_path),)), config_path)

    import codex_usage.pool_authority_owner as owner_module

    original_ensure = owner_module.ensure_private_directory

    def no_source_directory(path: Path, *, label: str, **kwargs: object) -> Path:
        if label.startswith("pool authority"):
            raise AssertionError("source directory was prepared before validation")
        return original_ensure(path, label=label, **kwargs)

    monkeypatch.setattr(owner_module, "ensure_private_directory", no_source_directory)

    with pytest.raises(ValueError, match="inventory"):
        save_pool_authority_owner(
            (_authority("bravo"),),
            expected_generation=0,
            config_path=config_path,
            state_home=state_home,
        )

    assert not _source_path(state_home).exists()


def test_invalid_first_save_does_not_create_config_or_source_parent(tmp_path: Path) -> None:
    config_path = tmp_path / "not-created" / "config.toml"
    state_home = tmp_path / "state"

    with pytest.raises(ValueError, match="inventory"):
        save_pool_authority_owner(
            (_authority("alpha"),),
            expected_generation=0,
            config_path=config_path,
            state_home=state_home,
        )

    assert not config_path.parent.exists()
    assert not state_home.exists()


@pytest.mark.parametrize(
    "authorities",
    [(), (_authority("alpha"), _authority("bravo"))],
    ids=["missing", "additional"],
)
def test_inventory_parity_failure_leaves_existing_source_and_config_unchanged(
    tmp_path: Path,
    authorities: tuple[PoolAuthorityOwner, ...],
) -> None:
    config_path, state_home, saved = _save_complete_authority(tmp_path)
    source_path = _source_path(state_home)
    previous_source = source_path.read_bytes()

    with pytest.raises(ValueError, match="inventory"):
        save_pool_authority_owner(
            authorities,
            expected_generation=saved.generation,
            config_path=config_path,
            state_home=state_home,
        )

    assert source_path.read_bytes() == previous_source
    assert load_pool_authority_owner(config_path=config_path) == saved


@pytest.mark.parametrize(
    "mutate",
    [
        lambda authority: replace(authority, hive_available=1),
        lambda authority: replace(authority, allowed_model_families=("z", "a")),
        lambda authority: replace(authority, reasoning_minimum="ultra", reasoning_maximum="low"),
        lambda authority: replace(authority, allowed_lifecycles=("session", "session")),
        lambda authority: replace(authority, account_id=1),
    ],
    ids=["boolean-type", "list-order", "reasoning-bounds", "duplicate-list", "account-type"],
)
def test_invalid_owner_value_leaves_existing_source_and_config_unchanged(
    tmp_path: Path,
    mutate,
) -> None:
    config_path, state_home, saved = _save_complete_authority(tmp_path)
    source_path = _source_path(state_home)
    previous_source = source_path.read_bytes()

    with pytest.raises(ValueError):
        save_pool_authority_owner(
            (mutate(_authority("alpha")),),
            expected_generation=saved.generation,
            config_path=config_path,
            state_home=state_home,
        )

    assert source_path.read_bytes() == previous_source
    assert load_pool_authority_owner(config_path=config_path) == saved


def test_duplicate_owner_records_leave_absent_source_and_config_unchanged(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "config.toml"
    state_home = tmp_path / "state"
    save_config(AppConfig(accounts=(_account("alpha", tmp_path),)), config_path)

    with pytest.raises(ValueError, match="duplicate"):
        save_pool_authority_owner(
            (_authority("alpha"), _authority("alpha")),
            expected_generation=0,
            config_path=config_path,
            state_home=state_home,
        )

    assert not _source_path(state_home).exists()
    assert load_pool_authority_owner(config_path=config_path).generation == 0


@pytest.mark.parametrize("field", ["provider", "unexpected"])
def test_config_rejects_missing_or_additional_owner_field(
    tmp_path: Path,
    field: str,
) -> None:
    config_path = tmp_path / "config" / "config.toml"
    save_config(AppConfig(accounts=()), config_path)
    fields = _authority("synthetic").to_source_record()
    if field == "provider":
        del fields[field]
    else:
        fields[field] = "not-authority"
    lines = [
        "[pool_authority]",
        "generation = 0",
        "",
        "[[pool_authority.authorities]]",
    ]
    for name, value in fields.items():
        if type(value) is bool:
            rendered = "true" if value else "false"
        elif type(value) is list:
            rendered = "[" + ", ".join(json.dumps(item) for item in value) + "]"
        else:
            rendered = json.dumps(value)
        lines.append(f"{name} = {rendered}")
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n" + "\n".join(lines),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    with pytest.raises(ValueError, match="exactly ten"):
        load_config(config_path)


def test_config_rejects_handwritten_authority_account_parity_mismatch(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "config.toml"
    save_config(AppConfig(accounts=(_account("alpha", tmp_path),)), config_path)
    fields = _authority("bravo").to_source_record()
    rendered = "\n".join(
        f"{name} = {json.dumps(value)}"
        if type(value) is not bool
        else f"{name} = {'true' if value else 'false'}"
        for name, value in fields.items()
    )
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "\n[pool_authority]\ngeneration = 1\n\n[[pool_authority.authorities]]\n"
        + rendered,
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    with pytest.raises(ValueError, match="account inventory"):
        load_config(config_path)


def test_account_set_change_invalidates_configured_authority_but_same_id_update_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, state_home, saved = _save_complete_authority(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    add_or_update_account(
        "alpha",
        label="changed-only-metadata",
        profile_dir=str(tmp_path / "profiles" / "alpha"),
        path=config_path,
    )
    assert load_config(config_path).pool_authority.generation == saved.generation
    add_or_update_account(
        "bravo", profile_dir=str(tmp_path / "profiles" / "bravo"), path=config_path
    )
    assert load_config(config_path).pool_authority.configured is False
    assert not _source_path(state_home).exists()


def test_owner_values_are_never_derived_from_account_metadata(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "config.toml"
    state_home = tmp_path / "state"
    account = _account("alpha", tmp_path)
    account = replace(
        account,
        label="unrelated-label-backend_used-models-series-trends",
        series="UNRELATED",
        series_active=True,
    )
    save_config(AppConfig(accounts=(account,)), config_path)

    save_pool_authority_owner(
        (_authority("alpha"),),
        expected_generation=0,
        config_path=config_path,
        state_home=state_home,
    )

    source_text = _source_path(state_home).read_text(encoding="utf-8")
    assert "unrelated-label" not in source_text
    assert "backend_used" not in source_text
    assert "UNRELATED" not in source_text


def test_source_replace_is_atomic_and_uses_new_private_regular_file(tmp_path: Path) -> None:
    config_path, state_home, saved = _save_complete_authority(tmp_path)
    source_path = _source_path(state_home)
    previous_stat = source_path.stat()
    previous_bytes = source_path.read_bytes()
    replacement = replace(_authority("alpha"), pool_id="pool-replaced")

    updated = save_pool_authority_owner(
        (replacement,),
        expected_generation=saved.generation,
        config_path=config_path,
        state_home=state_home,
    )

    current_stat = source_path.stat()
    assert updated.generation == saved.generation + 1
    assert source_path.read_bytes() != previous_bytes
    assert current_stat.st_ino != previous_stat.st_ino
    assert stat.S_ISREG(current_stat.st_mode)
    assert current_stat.st_uid == os.geteuid()
    assert stat.S_IMODE(current_stat.st_mode) == 0o600


def test_stale_generation_leaves_existing_source_and_config_unchanged(tmp_path: Path) -> None:
    config_path, state_home, saved = _save_complete_authority(tmp_path)
    source_path = _source_path(state_home)
    previous_source = source_path.read_bytes()

    with pytest.raises(ValueError, match="stale"):
        save_pool_authority_owner(
            (_authority("alpha"),),
            expected_generation=saved.generation - 1,
            config_path=config_path,
            state_home=state_home,
        )

    assert source_path.read_bytes() == previous_source
    assert load_pool_authority_owner(config_path=config_path) == saved


def test_source_write_failure_rolls_back_config_and_preserves_existing_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, state_home, saved = _save_complete_authority(tmp_path)
    source_path = _source_path(state_home)
    previous_source = source_path.read_bytes()
    import codex_usage.pool_authority_owner as owner_module

    def fail_source_write(*_args, **_kwargs) -> None:
        raise OSError("synthetic private write failure")

    monkeypatch.setattr(owner_module, "write_private_text", fail_source_write)

    with pytest.raises(ValueError, match="could not materialize"):
        save_pool_authority_owner(
            (replace(_authority("alpha"), pool_id="pool-new"),),
            expected_generation=saved.generation,
            config_path=config_path,
            state_home=state_home,
        )

    assert source_path.read_bytes() == previous_source
    assert load_pool_authority_owner(config_path=config_path) == saved


def test_initial_source_write_failure_leaves_source_absent_and_config_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config" / "config.toml"
    state_home = tmp_path / "state"
    save_config(AppConfig(accounts=(_account("alpha", tmp_path),)), config_path)
    import codex_usage.pool_authority_owner as owner_module

    def fail_source_write(*_args, **_kwargs) -> None:
        raise OSError("synthetic initial private write failure")

    monkeypatch.setattr(owner_module, "write_private_text", fail_source_write)

    with pytest.raises(ValueError, match="could not materialize"):
        save_pool_authority_owner(
            (_authority("alpha"),),
            expected_generation=0,
            config_path=config_path,
            state_home=state_home,
        )

    assert not _source_path(state_home).exists()
    assert load_pool_authority_owner(config_path=config_path).generation == 0


def test_source_symlink_or_symlinked_ancestor_fails_closed_before_config_mutation(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "config.toml"
    state_home = tmp_path / "state"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    save_config(AppConfig(accounts=(_account("alpha", tmp_path),)), config_path)
    (state_home / "codex-usage").parent.mkdir(mode=0o700)
    (state_home / "codex-usage").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        save_pool_authority_owner(
            (_authority("alpha"),),
            expected_generation=0,
            config_path=config_path,
            state_home=state_home,
        )

    assert not list(outside.iterdir())
    assert load_pool_authority_owner(config_path=config_path).generation == 0


def test_source_swap_race_fails_closed_and_rolls_back_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, state_home, saved = _save_complete_authority(tmp_path)
    source_path = _source_path(state_home)
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    import codex_usage.pool_authority_owner as owner_module

    original_write = owner_module.write_private_text

    def swap_to_symlink(path: Path, text: str, **kwargs: object) -> None:
        path.unlink()
        path.symlink_to(outside)
        original_write(path, text, **kwargs)

    monkeypatch.setattr(owner_module, "write_private_text", swap_to_symlink)

    with pytest.raises(ValueError, match="could not materialize"):
        save_pool_authority_owner(
            (replace(_authority("alpha"), pool_id="pool-new"),),
            expected_generation=saved.generation,
            config_path=config_path,
            state_home=state_home,
        )

    assert source_path.is_symlink()
    assert source_path.read_text(encoding="utf-8") == "outside"
    assert load_pool_authority_owner(config_path=config_path) == saved


def test_source_path_uses_xdg_state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codex_usage.pool_authority_owner import pool_authority_source_path

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert pool_authority_source_path() == _source_path(tmp_path)


def test_save_rejects_untrusted_existing_source_before_config_generation(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "config.toml"
    state_home = tmp_path / "state"
    source_path = state_home / "codex-usage" / "integration" / "pool-authority-source-v2.json"
    source_path.parent.mkdir(parents=True, mode=0o700)
    source_path.parent.chmod(0o700)
    source_path.write_text("old synthetic source\n", encoding="utf-8")
    source_path.chmod(0o644)
    save_config(AppConfig(accounts=(_account("alpha", tmp_path),)), config_path)

    with pytest.raises(ValueError, match="private regular"):
        save_pool_authority_owner(
            (_authority("alpha"),),
            expected_generation=0,
            config_path=config_path,
            state_home=state_home,
        )

    assert source_path.read_text(encoding="utf-8") == "old synthetic source\n"
    assert load_pool_authority_owner(config_path=config_path).generation == 0


def test_owner_source_path_requires_absolute_state_home(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "config.toml"
    save_config(AppConfig(accounts=()), config_path)

    with pytest.raises(ValueError, match="state_home must be absolute"):
        save_pool_authority_owner(
            (),
            expected_generation=0,
            config_path=config_path,
            state_home=Path("relative-state"),
        )
