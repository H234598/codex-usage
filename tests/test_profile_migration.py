import json
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

from codex_usage import profile_layout, profile_migration
from codex_usage.models import Account
from codex_usage.profile_migration import (
    AuthMigrationItem,
    AuthMigrationPlan,
    apply_auth_migration,
    plan_auth_migration,
    rollback_auth_migration,
)


class _RaisingTimezone(tzinfo):
    def utcoffset(self, _value):
        raise RuntimeError("synthetic timezone marker")


def _account(tmp_path: Path, auth: Path | None = None) -> Account:
    return Account(
        id="alpha",
        label="Alpha",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(auth) if auth else None,
    )


def test_auth_migration_dry_run_finds_explicit_source_without_writing(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text('{"tokens":{"access_token":"secret"}}', encoding="utf-8")
    source.chmod(0o600)

    plan = plan_auth_migration((_account(tmp_path, source),))

    assert len(plan.items) == 1
    assert plan.items[0].status == "planned"
    assert plan.items[0].target == tmp_path / "profile" / "codex-home" / "auth.json"
    assert not plan.items[0].target.exists()
    assert plan.items[0].secret_marker is None


def test_auth_migration_deduplicates_identical_search_candidates(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    account = Account(
        id="alpha",
        label="Alpha",
        profile_dir=str(tmp_path),
    )

    plan = plan_auth_migration((account,), search_roots=(tmp_path,))

    assert plan.items[0].status == "planned"
    assert plan.items[0].source == source


def test_auth_migration_deduplicates_dotdot_search_candidates(tmp_path):
    search_root = tmp_path / "search"
    source = search_root / "alpha" / "auth.json"
    source.parent.mkdir(parents=True)
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    alias_parent = search_root / "nested"
    alias_parent.mkdir()
    account = Account(
        id="alpha",
        label="Alpha",
        profile_dir=str(tmp_path / "profile"),
    )

    plan = plan_auth_migration(
        (account,), search_roots=(search_root, alias_parent / "..")
    )

    assert plan.items[0].status == "planned"
    assert plan.items[0].source == source


def test_auth_migration_rejects_unknown_auth_home(tmp_path):
    account = _account(
        tmp_path,
        Path("~definitely-no-such-user-zzzz/auth.json"),
    )

    with pytest.raises(ValueError, match="auth source cannot be resolved"):
        plan_auth_migration((account,))


def test_auth_migration_plan_classifies_symlink_ancestor_source_as_conflict(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "auth.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(outside, target_is_directory=True)
    account = Account(
        id="alpha",
        label="Alpha",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=str(linked_root / "auth.json"),
    )

    plan = plan_auth_migration((account,))

    assert plan.items[0].status == "conflict"
    assert "symlink ancestors" in (plan.items[0].reason or "")


def test_auth_migration_plan_does_not_classify_symlink_target_as_canonical(tmp_path):
    profile = tmp_path / "profile"
    target = profile / "codex-home" / "auth.json"
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside-auth.json"
    outside.write_text("{}", encoding="utf-8")
    outside.chmod(0o600)
    target.symlink_to(outside)

    plan = plan_auth_migration((_account(tmp_path, target),))

    assert plan.items[0].status == "conflict"
    assert plan.items[0].reason == "auth source is a symlink"


def test_auth_migration_plan_classifies_symlink_ancestor_target_as_conflict(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    profile = tmp_path / "profile"
    profile.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (profile / "codex-home").symlink_to(outside, target_is_directory=True)

    plan = plan_auth_migration((_account(tmp_path, source),))

    assert plan.items[0].status == "conflict"
    assert "symlink ancestors" in (plan.items[0].reason or "")


def test_auth_migration_rejects_unrepresentable_created_at(tmp_path):
    plan = AuthMigrationPlan(
        migration_id="m-test",
        items=(),
        created_at=datetime.min.replace(tzinfo=timezone(timedelta(hours=14))),
    )

    with pytest.raises(ValueError, match="migration plan"):
        apply_auth_migration(plan, tmp_path / "migration" / "manifest.json")


def test_auth_migration_rejects_timezone_callbacks_that_raise(tmp_path):
    plan = AuthMigrationPlan(
        migration_id="m-test",
        items=(),
        created_at=datetime(2026, 8, 16, 10, 0, tzinfo=_RaisingTimezone()),
    )

    with pytest.raises(ValueError, match="migration plan"):
        apply_auth_migration(plan, tmp_path / "migration" / "manifest.json")


def test_auth_migration_plan_rejects_too_many_accounts(tmp_path):
    accounts = tuple(
        Account(
            id=f"account-{index}",
            label=f"Account {index}",
            profile_dir=str(tmp_path / f"profile-{index}"),
        )
        for index in range(profile_migration.MAX_MIGRATION_ITEMS + 1)
    )

    with pytest.raises(ValueError, match="at most"):
        plan_auth_migration(accounts)


@pytest.mark.parametrize("search_roots", [None, [], "invalid"])
def test_auth_migration_plan_rejects_non_tuple_search_roots(search_roots):
    with pytest.raises(ValueError, match="search roots"):
        plan_auth_migration(
            (Account(id="alpha", label="Alpha", profile_dir="/tmp/alpha"),),
            search_roots=search_roots,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("account_id", [None, [], "../escape", "__all_accounts__"])
def test_auth_migration_plan_rejects_invalid_account_id(tmp_path, account_id):
    account = Account(
        id=account_id,
        label="Alpha",
        profile_dir=str(tmp_path / "profile"),
    )

    with pytest.raises(ValueError, match="account id is invalid"):
        plan_auth_migration((account,))


def test_auth_migration_plan_rejects_duplicate_account_ids(tmp_path):
    first_source = tmp_path / "first-auth.json"
    second_source = tmp_path / "second-auth.json"
    first_source.write_text("{}", encoding="utf-8")
    second_source.write_text("{}", encoding="utf-8")
    first_source.chmod(0o600)
    second_source.chmod(0o600)
    first = Account(
        id="alpha",
        label="First",
        profile_dir=str(tmp_path / "first-profile"),
        auth_json_path=str(first_source),
    )
    second = Account(
        id="alpha",
        label="Second",
        profile_dir=str(tmp_path / "second-profile"),
        auth_json_path=str(second_source),
    )

    with pytest.raises(ValueError, match="duplicate account id"):
        plan_auth_migration((first, second))


def test_auth_migration_apply_rejects_duplicate_item_account_ids(tmp_path):
    plan = AuthMigrationPlan(
        migration_id="m-test",
        items=(
            AuthMigrationItem(
                account_id="alpha",
                source=None,
                target=tmp_path / "first" / "auth.json",
                status="canonical",
            ),
            AuthMigrationItem(
                account_id="alpha",
                source=None,
                target=tmp_path / "second" / "auth.json",
                status="canonical",
            ),
        ),
        created_at=datetime.now(UTC),
    )

    with pytest.raises(ValueError, match="migration plan"):
        apply_auth_migration(plan, tmp_path / "migration" / "manifest.json")

    assert not (tmp_path / "migration" / "manifest.json").exists()


@pytest.mark.parametrize("duplicate_field", ["source", "target"])
def test_auth_migration_apply_rejects_duplicate_item_resources(
    tmp_path, duplicate_field
):
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    first_source = source
    second_source = source if duplicate_field == "source" else tmp_path / "second.json"
    if second_source != source:
        second_source.write_text("{}", encoding="utf-8")
        second_source.chmod(0o600)
    first_target = tmp_path / "first" / "auth.json"
    second_target = (
        first_target
        if duplicate_field == "target"
        else tmp_path / "second" / "auth.json"
    )
    first_target.parent.mkdir(parents=True)
    if second_target != first_target:
        second_target.parent.mkdir(parents=True)
    plan = AuthMigrationPlan(
        migration_id="m-test",
        items=(
            AuthMigrationItem(
                account_id="alpha",
                source=first_source,
                target=first_target,
                status="planned",
            ),
            AuthMigrationItem(
                account_id="beta",
                source=second_source,
                target=second_target,
                status="planned",
            ),
        ),
        created_at=datetime.now(UTC),
    )

    with pytest.raises(ValueError, match="migration plan"):
        apply_auth_migration(plan, tmp_path / "migration" / "manifest.json")

    assert not first_target.exists()
    assert not second_target.exists()
    assert not (tmp_path / "migration" / "manifest.json").exists()


@pytest.mark.parametrize("auth_json_path", [[], {}, 1, object()])
def test_auth_migration_plan_rejects_invalid_auth_source_type(
    tmp_path, auth_json_path
):
    account = Account(
        id="alpha",
        label="Alpha",
        profile_dir=str(tmp_path / "profile"),
        auth_json_path=auth_json_path,
    )

    with pytest.raises(ValueError, match="auth source is invalid"):
        plan_auth_migration((account,))


@pytest.mark.parametrize(
    "plan",
    [
        None,
        AuthMigrationPlan(
            migration_id="m-test",
            items=(None,),
            created_at=datetime.now(UTC),
        ),
        AuthMigrationPlan(
            migration_id="m-test",
            items=(
                AuthMigrationItem(
                    account_id="alpha",
                    source=None,
                    target=Path("relative/auth.json"),
                    status="canonical",
                ),
            ),
            created_at=datetime.now(UTC),
        ),
    ],
)
def test_auth_migration_apply_rejects_malformed_plan(tmp_path, plan):
    with pytest.raises(ValueError, match="migration plan"):
        apply_auth_migration(plan, tmp_path / "migration" / "manifest.json")  # type: ignore[arg-type]


def test_auth_migration_apply_rejects_invalid_item_account_id(tmp_path):
    plan = AuthMigrationPlan(
        migration_id="m-test",
        items=(
            AuthMigrationItem(
                account_id="../escape",
                source=None,
                target=tmp_path / "profile" / "codex-home" / "auth.json",
                status="canonical",
            ),
        ),
        created_at=datetime.now(UTC),
    )

    with pytest.raises(ValueError, match="migration plan"):
        apply_auth_migration(plan, tmp_path / "migration" / "manifest.json")


def test_auth_migration_plan_rejects_existing_canonical_target(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text("{\"source\":true}", encoding="utf-8")
    source.chmod(0o600)
    target = tmp_path / "profile" / "codex-home" / "auth.json"
    target.parent.mkdir(parents=True)
    target.write_text("{\"existing\":true}", encoding="utf-8")
    target.chmod(0o600)

    plan = plan_auth_migration((_account(tmp_path, source),))

    assert plan.items[0].status == "conflict"
    assert plan.items[0].reason == "canonical auth target already exists"


def test_auth_migration_apply_and_rollback_keep_source_and_never_return_secret(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text('{"tokens":{"access_token":"secret"}}', encoding="utf-8")
    source.chmod(0o600)
    plan = plan_auth_migration((_account(tmp_path, source),))
    manifest = tmp_path / "migration" / "manifest.json"

    applied = apply_auth_migration(plan, manifest)
    assert applied["status"] == "applied"
    target = tmp_path / "profile" / "codex-home" / "auth.json"
    assert json.loads(target.read_text(encoding="utf-8"))["tokens"]["access_token"] == "secret"
    assert "secret" not in json.dumps(applied)

    rollback_auth_migration(manifest)
    assert not target.exists()
    assert source.exists()


def test_auth_migration_binds_manifest_directory_mode_to_checked_directory(
    tmp_path, monkeypatch
):
    source = tmp_path / "auth.json"
    source.write_text('{"source":true}', encoding="utf-8")
    source.chmod(0o600)
    plan = plan_auth_migration((_account(tmp_path, source),))
    manifest_parent = tmp_path / "migration"
    manifest_parent.mkdir(mode=0o755)
    manifest = manifest_parent / "manifest.json"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    original_chmod = Path.chmod

    def replace_target_before_path_chmod(path, mode):
        if path == manifest_parent:
            manifest_parent.rmdir()
            manifest_parent.symlink_to(outside, target_is_directory=True)
        return original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", replace_target_before_path_chmod)
    apply_auth_migration(plan, manifest)

    assert manifest_parent.is_dir() and not manifest_parent.is_symlink()
    assert manifest_parent.stat().st_mode & 0o777 == 0o700
    assert outside.stat().st_mode & 0o777 == 0o755


def test_auth_migration_does_not_overwrite_existing_manifest(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text('{"source":true}', encoding="utf-8")
    source.chmod(0o600)
    plan = plan_auth_migration((_account(tmp_path, source),))
    manifest = tmp_path / "migration" / "manifest.json"
    manifest.parent.mkdir(mode=0o700)
    original_manifest = '{"status":"applied","items":[]}'
    manifest.write_text(original_manifest, encoding="utf-8")
    manifest.chmod(0o600)

    with pytest.raises(ValueError, match="existing file"):
        apply_auth_migration(plan, manifest)

    assert manifest.read_text(encoding="utf-8") == original_manifest
    assert not plan.items[0].target.exists()


def test_auth_migration_apply_does_not_overwrite_target_added_after_plan(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text("{\"source\":true}", encoding="utf-8")
    source.chmod(0o600)
    plan = plan_auth_migration((_account(tmp_path, source),))
    target = plan.items[0].target
    target.parent.mkdir(parents=True)
    target.write_text("{\"existing\":true}", encoding="utf-8")
    target.chmod(0o600)

    with pytest.raises(ValueError, match="existing file"):
        apply_auth_migration(plan, tmp_path / "migration" / "manifest.json")

    assert target.read_text(encoding="utf-8") == "{\"existing\":true}"


def test_auth_migration_apply_rejects_source_permissions_changed_after_plan(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    plan = plan_auth_migration((_account(tmp_path, source),))
    source.chmod(0o644)

    with pytest.raises(ValueError, match="auth source permissions"):
        apply_auth_migration(plan, tmp_path / "migration" / "manifest.json")

    assert not plan.items[0].target.exists()


def test_auth_migration_prevalidates_all_sources_before_first_write(tmp_path):
    first_source = tmp_path / "first-auth.json"
    second_source = tmp_path / "second-auth.json"
    first_source.write_text("{\"first\":true}", encoding="utf-8")
    second_source.write_text("{\"second\":true}", encoding="utf-8")
    first_source.chmod(0o600)
    second_source.chmod(0o600)
    first = _account(tmp_path, first_source)
    second = Account(
        id="beta",
        label="Beta",
        profile_dir=str(tmp_path / "beta-profile"),
        auth_json_path=str(second_source),
    )
    plan = plan_auth_migration((first, second))
    second_source.write_text("not-json", encoding="utf-8")

    with pytest.raises(ValueError):
        apply_auth_migration(plan, tmp_path / "migration" / "manifest.json")

    assert not plan.items[0].target.exists()


def test_auth_migration_cleans_targets_written_before_runtime_failure(tmp_path, monkeypatch):
    first_source = tmp_path / "first-auth.json"
    second_source = tmp_path / "second-auth.json"
    first_source.write_text("{\"first\":true}", encoding="utf-8")
    second_source.write_text("{\"second\":true}", encoding="utf-8")
    first_source.chmod(0o600)
    second_source.chmod(0o600)
    first = _account(tmp_path, first_source)
    second = Account(
        id="beta",
        label="Beta",
        profile_dir=str(tmp_path / "beta-profile"),
        auth_json_path=str(second_source),
    )
    plan = plan_auth_migration((first, second))
    original_write = profile_migration.write_private_text
    canonical_writes = 0

    def fail_second_canonical_write(path, text, *, label, **kwargs):
        nonlocal canonical_writes
        if label == "canonical auth.json":
            canonical_writes += 1
            if canonical_writes == 2:
                raise OSError("simulated target write failure")
        return original_write(path, text, label=label, **kwargs)

    monkeypatch.setattr(
        "codex_usage.profile_migration.write_private_text",
        fail_second_canonical_write,
    )

    with pytest.raises(OSError, match="target write failure"):
        apply_auth_migration(plan, tmp_path / "migration" / "manifest.json")

    assert not plan.items[0].target.exists()


def test_auth_migration_applies_all_prevalidated_accounts(tmp_path):
    first_source = tmp_path / "first-auth.json"
    second_source = tmp_path / "second-auth.json"
    first_source.write_text("{\"first\":true}", encoding="utf-8")
    second_source.write_text("{\"second\":true}", encoding="utf-8")
    first_source.chmod(0o600)
    second_source.chmod(0o600)
    plan = plan_auth_migration(
        (
            _account(tmp_path, first_source),
            Account(
                id="beta",
                label="Beta",
                profile_dir=str(tmp_path / "beta-profile"),
                auth_json_path=str(second_source),
            ),
        )
    )

    manifest = apply_auth_migration(plan, tmp_path / "migration" / "manifest.json")

    assert manifest["status"] == "applied"
    assert [item["status"] for item in manifest["items"]] == ["applied", "applied"]
    assert all(item.target.exists() for item in plan.items)


def test_auth_migration_rolls_back_layout_when_setup_fails(tmp_path, monkeypatch):
    source = tmp_path / "auth.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    plan = plan_auth_migration((_account(tmp_path, source),))
    original_ensure_directory = profile_layout._ensure_directory

    def fail_profile_jobs(path, label, **kwargs):
        if label == "profile jobs":
            raise OSError("simulated layout failure")
        return original_ensure_directory(path, label, **kwargs)

    monkeypatch.setattr(profile_layout, "_ensure_directory", fail_profile_jobs)

    with pytest.raises(OSError, match="layout failure"):
        apply_auth_migration(plan, tmp_path / "migration" / "manifest.json")

    assert not (tmp_path / "profile").exists()


def test_auth_migration_rolls_back_previous_profile_when_later_setup_fails(
    tmp_path, monkeypatch
):
    first_source = tmp_path / "first-auth.json"
    second_source = tmp_path / "second-auth.json"
    first_source.write_text("{\"first\":true}", encoding="utf-8")
    second_source.write_text("{\"second\":true}", encoding="utf-8")
    first_source.chmod(0o600)
    second_source.chmod(0o600)
    first = _account(tmp_path, first_source)
    second = Account(
        id="beta",
        label="Beta",
        profile_dir=str(tmp_path / "beta-profile"),
        auth_json_path=str(second_source),
    )
    plan = plan_auth_migration((first, second))
    original_ensure_profile_layout = profile_migration.ensure_profile_layout
    calls = 0

    def fail_second_layout(account, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second layout failure")
        return original_ensure_profile_layout(account, **kwargs)

    monkeypatch.setattr(
        profile_migration,
        "ensure_profile_layout",
        fail_second_layout,
    )

    with pytest.raises(OSError, match="second layout failure"):
        apply_auth_migration(plan, tmp_path / "migration" / "manifest.json")

    assert not (tmp_path / "profile").exists()


def test_auth_migration_preserves_existing_profile_metadata(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    profile = tmp_path / "profile"
    profile.mkdir()
    metadata = profile / "profile.json"
    original_metadata = '{"account_id":"alpha","label":"Mein Konto","schema_version":1}\n'
    metadata.write_text(original_metadata, encoding="utf-8")
    metadata.chmod(0o600)
    plan = plan_auth_migration((_account(tmp_path, source),))

    apply_auth_migration(plan, tmp_path / "migration" / "manifest.json")

    assert metadata.read_text(encoding="utf-8") == original_metadata


def test_auth_migration_rejects_existing_profile_metadata_symlink(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    profile = tmp_path / "profile"
    profile.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (profile / "profile.json").symlink_to(outside)
    plan = plan_auth_migration((_account(tmp_path, source),))

    with pytest.raises(ValueError, match="profile metadata"):
        apply_auth_migration(plan, tmp_path / "migration" / "manifest.json")

    assert not plan.items[0].target.exists()


def test_auth_migration_rejects_manifest_path_equal_to_source(tmp_path):
    source = tmp_path / "auth.json"
    original_source = "{\"source\":true}\n"
    source.write_text(original_source, encoding="utf-8")
    source.chmod(0o600)
    plan = plan_auth_migration((_account(tmp_path, source),))

    with pytest.raises(ValueError, match="manifest path conflicts"):
        apply_auth_migration(plan, source)

    assert source.read_text(encoding="utf-8") == original_source


def test_auth_migration_rejects_manifest_path_equal_to_target(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text("{\"source\":true}", encoding="utf-8")
    source.chmod(0o600)
    plan = plan_auth_migration((_account(tmp_path, source),))

    with pytest.raises(ValueError, match="manifest path conflicts"):
        apply_auth_migration(plan, plan.items[0].target)

    assert not plan.items[0].target.exists()


def test_auth_migration_rejects_manifest_path_dotdot_alias(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    alias_parent = tmp_path / "nested"
    alias_parent.mkdir()
    plan = plan_auth_migration((_account(tmp_path, source),))

    with pytest.raises(ValueError, match="manifest path conflicts"):
        apply_auth_migration(plan, alias_parent / ".." / "auth.json")


def test_auth_migration_rejects_manifest_path_equal_to_profile_metadata(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    plan = plan_auth_migration((_account(tmp_path, source),))
    metadata = plan.items[0].target.parent.parent / "profile.json"

    with pytest.raises(ValueError, match="manifest path conflicts"):
        apply_auth_migration(plan, metadata)

    assert not plan.items[0].target.exists()



def test_auth_migration_rollback_rejects_oversized_target(tmp_path, monkeypatch):
    from codex_usage import profile_migration

    source = tmp_path / "auth.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    plan = plan_auth_migration((_account(tmp_path, source),))
    manifest = tmp_path / "migration" / "manifest.json"
    apply_auth_migration(plan, manifest)

    target = tmp_path / "profile" / "codex-home" / "auth.json"
    monkeypatch.setattr(profile_migration, "MAX_AUTH_BYTES", 4)
    target.write_bytes(b"xxxxx")

    with pytest.raises(ValueError, match="too large"):
        rollback_auth_migration(manifest)
    assert target.exists()


def test_auth_migration_rollback_rejects_manifest_item_without_target(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    plan = plan_auth_migration((_account(tmp_path, source),))
    manifest_path = tmp_path / "migration" / "manifest.json"
    manifest = apply_auth_migration(plan, manifest_path)
    manifest["items"][0].pop("target")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)

    with pytest.raises(ValueError, match="manifest is invalid"):
        rollback_auth_migration(manifest_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("account_id", "../escape"), ("status", "unexpected")],
)
def test_auth_migration_rollback_rejects_invalid_manifest_item_fields(
    tmp_path, field, value
):
    source = tmp_path / "auth.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    plan = plan_auth_migration((_account(tmp_path, source),))
    manifest_path = tmp_path / "migration" / "manifest.json"
    manifest = apply_auth_migration(plan, manifest_path)
    manifest["items"][0][field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)

    with pytest.raises(ValueError, match="manifest is invalid"):
        rollback_auth_migration(manifest_path)

    assert (tmp_path / "profile" / "codex-home" / "auth.json").exists()
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "applied"


@pytest.mark.parametrize("items", [{}, "invalid", [None]])
def test_auth_migration_rollback_rejects_invalid_item_collection(tmp_path, items):
    manifest_path = tmp_path / "migration" / "manifest.json"
    manifest_path.parent.mkdir(mode=0o700)
    manifest_path.write_text(
        json.dumps({"status": "applied", "items": items}),
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)

    with pytest.raises(ValueError, match="manifest is invalid"):
        rollback_auth_migration(manifest_path)


def test_auth_migration_rollback_rejects_too_many_items(tmp_path):
    manifest_path = tmp_path / "migration" / "manifest.json"
    manifest_path.parent.mkdir(mode=0o700)
    manifest_path.write_text(
        json.dumps(
            {
                "status": "applied",
                "items": [{}] * (profile_migration.MAX_MIGRATION_ITEMS + 1),
            }
        ),
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)

    with pytest.raises(ValueError, match="manifest is invalid"):
        rollback_auth_migration(manifest_path)


def test_auth_migration_rejects_same_source_for_two_accounts(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    one = _account(tmp_path, source)
    two = Account(
        id="beta",
        label="Beta",
        profile_dir=str(tmp_path / "beta"),
        auth_json_path=str(source),
    )

    with pytest.raises(ValueError, match="multiple accounts"):
        plan_auth_migration((one, two))
