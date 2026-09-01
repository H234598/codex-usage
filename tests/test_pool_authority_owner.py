from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from codex_usage.config import (
    AppConfig,
    PoolAuthorityConfig,
    add_or_update_account,
    load_config,
    remove_account,
    restore_account,
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


def test_publish_pending_before_config_replace_recovers_by_forward_commit(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "config.toml"
    state_home = tmp_path / "state"
    save_config(AppConfig(accounts=(_account("alpha", tmp_path),)), config_path)
    import codex_usage.pool_authority_owner as owner_module

    owner_module._prepare_source_directory(_source_path(state_home))
    owner_module._write_pending_publish(
        owner_module.pool_authority_pending_path(state_home),
        owner_module.PoolAuthorityConfig(
            generation=1, authorities=(_authority("alpha"),), configured=True
        ),
        expected_generation=0,
        config_path=config_path,
    )

    owner_module.recover_pool_authority_pending(config_path=config_path, state_home=state_home)

    assert load_pool_authority_owner(config_path=config_path).generation == 1
    assert _source_path(state_home).exists()
    assert not owner_module.pool_authority_pending_path(state_home).exists()


def test_remove_and_restore_account_invalidate_source_and_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, state_home, _saved = _save_complete_authority(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    original = load_config(config_path).accounts[0]

    removed, account = remove_account("alpha", config_path)

    assert account == original
    assert removed.pool_authority.configured is False
    assert not _source_path(state_home).exists()
    from codex_usage.pool_authority_owner import pool_authority_pending_path

    assert not pool_authority_pending_path(state_home).exists()

    restored = restore_account(original, config_path)

    assert restored.pool_authority.configured is False
    assert not _source_path(state_home).exists()


def test_pending_recovery_is_bound_to_the_selected_config_path(tmp_path: Path) -> None:
    first = tmp_path / "first" / "config.toml"
    second = tmp_path / "second" / "config.toml"
    state_home = tmp_path / "state"
    save_config(AppConfig(accounts=(_account("alpha", tmp_path),)), first)
    save_config(AppConfig(accounts=(_account("alpha", tmp_path),)), second)
    import codex_usage.pool_authority_owner as owner_module

    owner_module._prepare_source_directory(_source_path(state_home))
    owner_module._write_pending_publish(
        owner_module.pool_authority_pending_path(state_home),
        owner_module.PoolAuthorityConfig(1, (_authority("alpha"),), True),
        expected_generation=0,
        config_path=first,
    )

    with pytest.raises(ValueError, match="another config"):
        owner_module.recover_pool_authority_pending(config_path=second, state_home=state_home)

    assert load_config(first).pool_authority.configured is False
    assert load_config(second).pool_authority.configured is False


@pytest.mark.parametrize("phase", ["config-replaced", "source-replaced"])
def test_publish_pending_recovers_after_each_post_marker_crash_boundary(
    tmp_path: Path, phase: str
) -> None:
    config_path, state_home, saved = _save_complete_authority(tmp_path)
    source_path = _source_path(state_home)
    import codex_usage.pool_authority_owner as owner_module

    replacement = replace(_authority("alpha"), pool_id="pool-after-crash")
    updated = owner_module._updated_config(
        load_config(config_path),
        candidate_authorities=(replacement,),
        expected_generation=saved.generation,
    )
    owner_module._write_pending_publish(
        owner_module.pool_authority_pending_path(state_home),
        updated.pool_authority,
        expected_generation=saved.generation,
        config_path=config_path,
    )
    if phase in {"config-replaced", "source-replaced"}:
        owner_module._save_config_unlocked(updated, config_path)
    if phase == "source-replaced":
        owner_module.write_private_text(
            source_path,
            owner_module._source_text((replacement,)),
            label="pool authority source",
            mode=0o600,
        )

    owner_module.recover_pool_authority_pending(config_path=config_path, state_home=state_home)

    assert load_config(config_path).pool_authority == updated.pool_authority
    assert source_path.read_bytes() == owner_module._source_text((replacement,)).encode()
    assert not owner_module.pool_authority_pending_path(state_home).exists()


@pytest.mark.parametrize("phase", ["source-present", "source-absent", "config-unconfigured"])
def test_invalidation_pending_recovers_after_each_crash_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    config_path, state_home, _saved = _save_complete_authority(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    import codex_usage.pool_authority_owner as owner_module

    previous = load_config(config_path)
    updated = replace(previous, pool_authority=owner_module.PoolAuthorityConfig())
    pending = owner_module.begin_account_set_invalidation(
        previous=previous, updated=updated, config_path=config_path
    )
    if phase in {"source-absent", "config-unconfigured"}:
        owner_module._save_config_unlocked(updated, config_path)
    if phase == "source-absent":
        _source_path(state_home).unlink()

    owner_module.recover_pool_authority_pending(config_path=config_path, state_home=state_home)

    assert not pending.exists()
    if phase == "source-present":
        assert _source_path(state_home).exists()
        assert load_config(config_path).pool_authority.configured is True
    else:
        assert not _source_path(state_home).exists()
        assert load_config(config_path).pool_authority.configured is False


@pytest.mark.parametrize(
    "mutation", ["symlink", "mode", "hardlink", "malformed", "unknown", "oversize"]
)
def test_untrusted_pending_fails_closed_without_mutation(tmp_path: Path, mutation: str) -> None:
    config_path, state_home, saved = _save_complete_authority(tmp_path)
    source_path = _source_path(state_home)
    previous_source = source_path.read_bytes()
    import codex_usage.pool_authority_owner as owner_module

    pending = owner_module.pool_authority_pending_path(state_home)
    pending.write_text("{}\n", encoding="utf-8")
    pending.chmod(0o600)
    if mutation == "symlink":
        pending.unlink()
        pending.symlink_to(tmp_path / "outside")
    elif mutation == "mode":
        pending.chmod(0o644)
    elif mutation == "hardlink":
        os.link(pending, tmp_path / "pending-link")
    elif mutation == "malformed":
        pass
    elif mutation == "unknown":
        pending.write_text('{"unknown":true}\n', encoding="utf-8")
    elif mutation == "oversize":
        pending.write_bytes(b"x" * 131073)

    with pytest.raises(ValueError):
        owner_module.recover_pool_authority_pending(config_path=config_path, state_home=state_home)

    assert source_path.read_bytes() == previous_source
    assert load_config(config_path).pool_authority.generation == saved.generation == 1


def test_source_and_config_rollback_failure_keeps_pending_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, state_home, saved = _save_complete_authority(tmp_path)
    source_path = _source_path(state_home)
    previous_source = source_path.read_bytes()
    import codex_usage.pool_authority_owner as owner_module

    original_save = owner_module._save_config_unlocked
    original_write = owner_module.write_private_text
    calls = 0

    def fail_rollback(config, path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic rollback failure")
        original_save(config, path)

    def fail_source(path, text, **kwargs):
        if kwargs.get("label") == "pool authority source":
            raise OSError("synthetic source failure")
        original_write(path, text, **kwargs)

    monkeypatch.setattr(owner_module, "_save_config_unlocked", fail_rollback)
    monkeypatch.setattr(owner_module, "write_private_text", fail_source)

    with pytest.raises(ExceptionGroup):
        save_pool_authority_owner(
            (replace(_authority("alpha"), pool_id="pool-recovered"),),
            expected_generation=saved.generation,
            config_path=config_path,
            state_home=state_home,
        )

    pending = owner_module.pool_authority_pending_path(state_home)
    assert pending.exists()
    assert source_path.read_bytes() == previous_source
    monkeypatch.setattr(owner_module, "_save_config_unlocked", original_save)
    monkeypatch.setattr(owner_module, "write_private_text", original_write)
    owner_module.recover_pool_authority_pending(config_path=config_path, state_home=state_home)

    assert not pending.exists()
    assert load_config(config_path).pool_authority.generation == saved.generation + 1
    assert source_path.read_bytes() != previous_source


def test_pending_symlink_ancestor_fails_closed_without_touching_config(tmp_path: Path) -> None:
    config_path, state_home, saved = _save_complete_authority(tmp_path)
    import codex_usage.pool_authority_owner as owner_module

    owner_module._write_pending(
        owner_module.pool_authority_pending_path(state_home),
        {
            "schema_version": 1,
            "operation": "invalidate",
            "config_path": owner_module._config_binding(config_path),
        },
    )
    app_dir = state_home / "codex-usage"
    outside = tmp_path / "outside"
    app_dir.rename(outside)
    app_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        owner_module.recover_pool_authority_pending(config_path=config_path, state_home=state_home)

    assert load_config(config_path).pool_authority.generation == saved.generation


def test_failed_account_config_write_recovers_invalidation_to_old_owner_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, state_home, saved = _save_complete_authority(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    source_path = _source_path(state_home)
    old_source = source_path.read_bytes()
    import codex_usage.config as config_module
    import codex_usage.pool_authority_owner as owner_module

    original_save = config_module._save_config_unlocked

    def fail_new_config(config, path):
        if not config.pool_authority.configured:
            raise OSError("synthetic config write failure")
        original_save(config, path)

    monkeypatch.setattr(config_module, "_save_config_unlocked", fail_new_config)
    with pytest.raises(OSError, match="synthetic config"):
        add_or_update_account(
            "bravo", profile_dir=str(tmp_path / "profiles" / "bravo"), path=config_path
        )
    assert owner_module.pool_authority_pending_path(state_home).exists()
    monkeypatch.setattr(config_module, "_save_config_unlocked", original_save)

    owner_module.recover_pool_authority_pending(config_path=config_path, state_home=state_home)

    assert not owner_module.pool_authority_pending_path(state_home).exists()
    assert load_pool_authority_owner(config_path=config_path) == saved
    assert source_path.read_bytes() == old_source


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


@pytest.mark.parametrize("untrusted", ["mode", "hardlink"])
def test_valid_but_untrusted_pending_cannot_start_recovery_mutation(
    tmp_path: Path, untrusted: str
) -> None:
    config_path = tmp_path / "config" / "config.toml"
    state_home = tmp_path / "state"
    save_config(AppConfig(accounts=(_account("alpha", tmp_path),)), config_path)
    import codex_usage.pool_authority_owner as owner_module

    source_path = _source_path(state_home)
    owner_module._prepare_source_directory(source_path)
    owner_module._write_pending_publish(
        owner_module.pool_authority_pending_path(state_home),
        owner_module.PoolAuthorityConfig(1, (_authority("alpha"),), True),
        expected_generation=0,
        config_path=config_path,
    )
    pending = owner_module.pool_authority_pending_path(state_home)
    if untrusted == "mode":
        pending.chmod(0o644)
    else:
        os.link(pending, tmp_path / "second-pending-link")

    with pytest.raises(ValueError, match="private regular"):
        owner_module.recover_pool_authority_pending(
            config_path=config_path, state_home=state_home
        )

    assert load_config(config_path).pool_authority.configured is False
    assert not source_path.exists()
    assert pending.exists()


def test_public_save_config_cannot_remove_materialized_authority(tmp_path: Path) -> None:
    config_path, state_home, _saved = _save_complete_authority(tmp_path)
    source_path = _source_path(state_home)
    old_source = source_path.read_bytes()

    with pytest.raises(ValueError, match="owner API"):
        save_config(
            replace(load_config(config_path), pool_authority=__import__(
                "codex_usage.config", fromlist=["PoolAuthorityConfig"]
            ).PoolAuthorityConfig()),
            config_path,
        )

    assert source_path.read_bytes() == old_source
    assert load_config(config_path).pool_authority.configured is True


def test_public_save_config_allows_other_changes_with_unchanged_authority(
    tmp_path: Path,
) -> None:
    config_path, state_home, saved = _save_complete_authority(tmp_path)
    source_path = _source_path(state_home)
    source_before = source_path.read_bytes()
    updated = replace(load_config(config_path), interval_seconds=301)

    save_config(updated, config_path)

    assert load_config(config_path) == updated
    assert load_config(config_path).pool_authority == PoolAuthorityConfig(
        saved.generation, saved.authorities, True
    )
    assert source_path.read_bytes() == source_before


def test_public_save_config_rejects_initial_configured_authority(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "config.toml"

    with pytest.raises(ValueError, match="owner API"):
        save_config(
            AppConfig(
                accounts=(_account("alpha", tmp_path),),
                pool_authority=PoolAuthorityConfig(1, (_authority("alpha"),), True),
            ),
            config_path,
        )

    assert not config_path.exists()


def test_two_custom_configs_never_overwrite_the_global_pending_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first" / "config.toml"
    second = tmp_path / "second" / "config.toml"
    state_home = tmp_path / "state"
    save_config(AppConfig(accounts=(_account("alpha", tmp_path),)), first)
    save_config(AppConfig(accounts=(_account("bravo", tmp_path),)), second)
    import codex_usage.pool_authority_owner as owner_module

    original_prepare = owner_module._prepare_config_directory
    original_save = owner_module._save_config_unlocked
    inserted_second_marker = False

    def create_second_marker_after_first_recovery(path: Path) -> None:
        nonlocal inserted_second_marker
        original_prepare(path)
        if path != first.parent or inserted_second_marker:
            return
        inserted_second_marker = True

        def fail_second_config_write(config: AppConfig, target: Path) -> None:
            if target == second:
                raise OSError("synthetic second-config crash after pending")
            original_save(config, target)

        monkeypatch.setattr(owner_module, "_save_config_unlocked", fail_second_config_write)
        with pytest.raises(OSError, match="second-config crash"):
            save_pool_authority_owner(
                (_authority("bravo"),),
                expected_generation=0,
                config_path=second,
                state_home=state_home,
            )
        monkeypatch.setattr(owner_module, "_save_config_unlocked", original_save)

    monkeypatch.setattr(
        owner_module,
        "_prepare_config_directory",
        create_second_marker_after_first_recovery,
    )
    with pytest.raises(ValueError, match="must not overwrite"):
        save_pool_authority_owner(
            (_authority("alpha"),),
            expected_generation=0,
            config_path=first,
            state_home=state_home,
        )

    pending = owner_module.pool_authority_pending_path(state_home)
    assert owner_module._parse_pending(pending.read_text(encoding="utf-8"), second)[
        "operation"
    ] == "publish"
    assert load_config(first).pool_authority == PoolAuthorityConfig()
    assert load_config(second).pool_authority == PoolAuthorityConfig()
    assert not _source_path(state_home).exists()


@pytest.mark.parametrize("phase", ["old", "new"])
def test_invalidation_recovery_requires_the_exact_full_config_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    config_path, state_home, _saved = _save_complete_authority(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    import codex_usage.pool_authority_owner as owner_module

    source_path = _source_path(state_home)
    source_before = source_path.read_bytes()
    previous = load_config(config_path)
    updated = replace(
        previous,
        accounts=(),
        pool_authority=PoolAuthorityConfig(),
    )
    pending = owner_module.begin_account_set_invalidation(
        previous=previous,
        updated=updated,
        config_path=config_path,
    )
    pending_before = pending.read_bytes()
    unexpected = replace(previous if phase == "old" else updated, interval_seconds=301)
    owner_module._save_config_unlocked(unexpected, config_path)

    with pytest.raises(ValueError, match="does not match config"):
        owner_module.recover_pool_authority_pending(config_path=config_path, state_home=state_home)

    assert load_config(config_path) == unexpected
    assert source_path.read_bytes() == source_before
    assert pending.read_bytes() == pending_before


def test_pending_swap_after_read_before_mutation_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config" / "config.toml"
    state_home = tmp_path / "state"
    save_config(AppConfig(accounts=(_account("alpha", tmp_path),)), config_path)
    import codex_usage.pool_authority_owner as owner_module

    source_path = _source_path(state_home)
    owner_module._prepare_source_directory(source_path)
    pending = owner_module.pool_authority_pending_path(state_home)
    owner_module._write_pending_publish(
        pending,
        PoolAuthorityConfig(1, (_authority("alpha"),), True),
        expected_generation=0,
        config_path=config_path,
    )
    replacement = replace(_authority("alpha"), pool_id="pool-swapped")
    original_load = owner_module.load_config
    swapped = False

    def swap_marker_after_preflight(path: Path | None = None) -> AppConfig:
        nonlocal swapped
        loaded = original_load(path)
        if not swapped:
            swapped = True
            alternate = pending.with_name("pool-authority-pending-replacement.json")
            owner_module._write_pending_publish(
                alternate,
                PoolAuthorityConfig(1, (replacement,), True),
                expected_generation=0,
                config_path=config_path,
            )
            os.replace(alternate, pending)
        return loaded

    monkeypatch.setattr(owner_module, "load_config", swap_marker_after_preflight)
    with pytest.raises(ValueError, match="changed"):
        owner_module.recover_pool_authority_pending(config_path=config_path, state_home=state_home)

    assert load_config(config_path).pool_authority == PoolAuthorityConfig()
    assert not source_path.exists()
    assert pending.exists()
    assert owner_module._parse_pending(pending.read_text(encoding="utf-8"), config_path)[
        "authorities"
    ] == (replacement,)


@pytest.mark.parametrize("operation", ["add", "remove", "restore"])
def test_public_account_invalidation_holds_all_accounts_lock_through_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from codex_usage.account_lock import AccountLockError, account_lock

    config_path, state_home, _saved = _save_complete_authority(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    import codex_usage.pool_authority_owner as owner_module

    original_remove_source = owner_module._remove_source
    observed: list[bool] = []

    def assert_transaction_locks(path: Path) -> None:
        with pytest.raises(AccountLockError):
            with account_lock("__all_accounts__", timeout_seconds=0):
                pass
        observed.append(True)
        original_remove_source(path)

    monkeypatch.setattr(owner_module, "_remove_source", assert_transaction_locks)
    if operation == "add":
        add_or_update_account(
            "bravo",
            profile_dir=str(tmp_path / "profiles" / "bravo"),
            path=config_path,
        )
    elif operation == "remove":
        remove_account("alpha", config_path)
    else:
        restore_account(_account("bravo", tmp_path), config_path)

    assert observed == [True]
    assert load_config(config_path).pool_authority == PoolAuthorityConfig()


@pytest.mark.parametrize("operation", ["add", "remove", "restore"])
def test_public_account_config_write_failure_keeps_pending_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    config_path, state_home, saved = _save_complete_authority(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    source_path = _source_path(state_home)
    source_before = source_path.read_bytes()
    import codex_usage.config as config_module
    import codex_usage.pool_authority_owner as owner_module

    original_save = config_module._save_config_unlocked

    def fail_unconfigured_config(config: AppConfig, path: Path) -> None:
        if config.pool_authority == PoolAuthorityConfig():
            raise OSError("synthetic invalidation config failure")
        original_save(config, path)

    monkeypatch.setattr(config_module, "_save_config_unlocked", fail_unconfigured_config)
    with pytest.raises(OSError, match="invalidation config failure"):
        if operation == "add":
            add_or_update_account(
                "bravo",
                profile_dir=str(tmp_path / "profiles" / "bravo"),
                path=config_path,
            )
        elif operation == "remove":
            remove_account("alpha", config_path)
        else:
            restore_account(_account("bravo", tmp_path), config_path)

    pending = owner_module.pool_authority_pending_path(state_home)
    assert pending.exists()
    assert source_path.read_bytes() == source_before
    monkeypatch.setattr(config_module, "_save_config_unlocked", original_save)
    owner_module.recover_pool_authority_pending(config_path=config_path, state_home=state_home)
    assert load_config(config_path).pool_authority == PoolAuthorityConfig(
        saved.generation,
        saved.authorities,
        True,
    )
    assert source_path.read_bytes() == source_before
    assert not pending.exists()


@pytest.mark.parametrize("failure", ["callback", "state"])
def test_public_add_rolls_back_callback_state_profile_and_auth_before_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    config_path, state_home, saved = _save_complete_authority(tmp_path)
    home = tmp_path / "home"
    incoming_auth = tmp_path / "incoming" / "auth.json"
    incoming_auth.parent.mkdir()
    incoming_auth.write_text('{"tokens": {}}\n', encoding="utf-8")
    incoming_auth.chmod(0o600)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setattr(Path, "home", lambda: home)
    source_path = _source_path(state_home)
    source_before = source_path.read_bytes()
    import codex_usage.pool_authority_owner as owner_module

    callback_events: list[str] = []

    def callback(_config: AppConfig) -> None:
        callback_events.append("changed")
        if failure == "callback":
            raise OSError("synthetic callback failure")

    def rollback_callback(_config: AppConfig) -> None:
        callback_events.append("restored")

    if failure == "state":
        monkeypatch.setattr(
            "codex_usage.state.remove_account_state",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic state failure")),
        )

    with pytest.raises(OSError, match=f"synthetic {failure} failure"):
        add_or_update_account(
            "bravo",
            auth_json_path=str(incoming_auth),
            test_home=True,
            path=config_path,
            before_state_cleanup=callback,
            rollback_callback=rollback_callback,
        )

    pending = owner_module.pool_authority_pending_path(state_home)
    assert callback_events == ["changed", "restored"]
    assert incoming_auth.exists()
    assert not (home / ".codex-test" / "bravo").exists()
    assert load_config(config_path).pool_authority == PoolAuthorityConfig(
        saved.generation, saved.authorities, True
    )
    assert source_path.read_bytes() == source_before
    owner_module.recover_pool_authority_pending(config_path=config_path, state_home=state_home)
    assert not pending.exists()
    assert load_pool_authority_owner(config_path=config_path) == saved


@pytest.mark.parametrize("failure", ["source", "pending"])
def test_public_finish_remove_failure_keeps_pending_for_exact_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    config_path, state_home, _saved = _save_complete_authority(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    source_path = _source_path(state_home)
    source_before = source_path.read_bytes()
    import codex_usage.pool_authority_owner as owner_module

    name = "_remove_source" if failure == "source" else "_remove_pending"
    original_remove = getattr(owner_module, name)

    def fail_finish_remove(path: Path) -> None:
        raise OSError(f"synthetic {failure} remove failure")

    monkeypatch.setattr(owner_module, name, fail_finish_remove)
    with pytest.raises(OSError, match=f"{failure} remove failure"):
        add_or_update_account(
            "bravo",
            profile_dir=str(tmp_path / "profiles" / "bravo"),
            path=config_path,
        )

    pending = owner_module.pool_authority_pending_path(state_home)
    assert pending.exists()
    assert load_config(config_path).pool_authority == PoolAuthorityConfig()
    if failure == "source":
        assert source_path.read_bytes() == source_before
    else:
        assert not source_path.exists()
    monkeypatch.setattr(owner_module, name, original_remove)
    owner_module.recover_pool_authority_pending(config_path=config_path, state_home=state_home)
    assert not pending.exists()
    assert load_config(config_path).pool_authority == PoolAuthorityConfig()
    assert not source_path.exists()
