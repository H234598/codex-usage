from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import MAX_CONFIG_ACCOUNTS, _validate_account_id
from .json_utils import loads_strict
from .models import Account
from .private_io import (
    assert_no_symlink_ancestors,
    ensure_private_directory,
    private_path_lock,
    read_private_text,
    write_private_text,
)
from .profile_layout import ProfileLayout, ensure_profile_layout, layout_for_account

MAX_AUTH_BYTES = 2 * 1024 * 1024
MAX_MIGRATION_ITEMS = MAX_CONFIG_ACCOUNTS


@dataclass(frozen=True)
class AuthMigrationItem:
    account_id: str
    source: Path | None
    target: Path
    status: str
    reason: str | None = None
    secret_marker: None = None


@dataclass(frozen=True)
class AuthMigrationPlan:
    migration_id: str
    items: tuple[AuthMigrationItem, ...]
    created_at: datetime


def plan_auth_migration(
    accounts: tuple[Account, ...],
    *,
    search_roots: tuple[Path, ...] = (),
) -> AuthMigrationPlan:
    if not isinstance(accounts, tuple) or not accounts:
        raise ValueError("accounts are required")
    if len(accounts) > MAX_MIGRATION_ITEMS:
        raise ValueError(f"accounts must contain at most {MAX_MIGRATION_ITEMS} entries")
    if not isinstance(search_roots, tuple):
        raise ValueError("search roots are invalid")
    normalized_roots = tuple(_require_absolute(root, "search root") for root in search_roots)
    items: list[AuthMigrationItem] = []
    sources: dict[Path, str] = {}
    account_ids: set[str] = set()
    for account in accounts:
        if not isinstance(account, Account):
            raise ValueError("account is invalid")
        try:
            _validate_account_id(account.id)
        except ValueError as exc:
            raise ValueError("account id is invalid") from exc
        if account.id in account_ids:
            raise ValueError(f"duplicate account id: {account.id}")
        account_ids.add(account.id)
        layout = layout_for_account(account)
        source = _source_for_account(account, layout, normalized_roots)
        if source is not None:
            previous = sources.get(source)
            if previous is not None and previous != account.id:
                raise ValueError("auth source is assigned to multiple accounts")
            sources[source] = account.id
        status, reason = _classify_source(source, layout.auth_json)
        items.append(
            AuthMigrationItem(
                account_id=account.id,
                source=source,
                target=layout.auth_json,
                status=status,
                reason=reason,
            )
        )
    return AuthMigrationPlan(
        migration_id="m-" + secrets.token_hex(12),
        items=tuple(items),
        created_at=datetime.now(UTC),
    )


def apply_auth_migration(plan: AuthMigrationPlan, manifest_path: Path) -> dict[str, object]:
    _validate_migration_plan(plan)
    manifest_path = _require_absolute(manifest_path, "manifest path")
    _assert_manifest_path_disjoint(plan, manifest_path)
    ensure_private_directory(manifest_path.parent, label="migration manifest directory")
    records: list[dict[str, object]] = []
    prepared: list[tuple[AuthMigrationItem, str]] = []
    created_files: list[tuple[Path, int, int]] = []
    created_directories: list[tuple[Path, int, int]] = []
    with private_path_lock(manifest_path, label="migration lock"):
        try:
            for item in plan.items:
                if item.status == "canonical":
                    records.append(_record(item, "canonical", None))
                    continue
                if item.status != "planned" or item.source is None:
                    raise ValueError(
                        f"cannot apply auth migration for {item.account_id}: "
                        f"{item.reason or item.status}"
                    )
                text, source_stat = read_private_text(
                    item.source,
                    regular_label="auth source",
                    read_label="auth source",
                    max_bytes=MAX_AUTH_BYTES,
                    too_large_label="auth source",
                )
                if source_stat.st_mode & 0o077:
                    raise ValueError("auth source permissions are invalid")
                _validate_auth_json(text)
                _assert_migration_target_available(item.target)
                prepared.append((item, text))

            for item, text in prepared:
                layout = ProfileLayout(
                    account_id=item.account_id,
                    profile_dir=item.target.parent.parent,
                    codex_home=item.target.parent,
                    auth_json=item.target,
                    metadata=item.target.parent.parent / "profile.json",
                    jobs=item.target.parent.parent / "jobs",
                    migration=item.target.parent.parent / "migration",
                )
                ensure_profile_layout(
                    Account(
                        id=item.account_id,
                        label=item.account_id,
                        profile_dir=str(layout.profile_dir),
                    ),
                    created_directories=created_directories,
                    created_files=created_files,
                    preserve_existing_metadata=True,
                )
                write_private_text(
                    item.target,
                    text,
                    label="canonical auth.json",
                    replace_existing=False,
                )
                target_stat = item.target.lstat()
                if not stat.S_ISREG(target_stat.st_mode) or target_stat.st_nlink != 1:
                    raise ValueError("canonical auth.json was changed during migration")
                created_files.append(
                    (item.target, target_stat.st_dev, target_stat.st_ino)
                )
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                records.append(_record(item, "applied", digest))
            manifest = {
                "schema_version": 1,
                "migration_id": plan.migration_id,
                "created_at": plan.created_at.astimezone(UTC).isoformat(),
                "status": "applied",
                "items": records,
            }
            write_private_text(
                manifest_path,
                json.dumps(manifest, ensure_ascii=True, sort_keys=True) + "\n",
                label="migration manifest",
                replace_existing=False,
            )
        except Exception as exc:
            cleanup_errors = _cleanup_created_migration_files(created_files)
            cleanup_errors.extend(
                _cleanup_created_migration_directories(created_directories)
            )
            for cleanup_error in cleanup_errors:
                exc.add_note(f"migration cleanup failed: {cleanup_error}")
            raise
    return manifest


def rollback_auth_migration(manifest_path: Path) -> None:
    manifest_path = _require_absolute(manifest_path, "manifest path")
    with private_path_lock(manifest_path, label="migration lock"):
        text, _ = read_private_text(
            manifest_path,
            regular_label="migration manifest",
            read_label="migration manifest",
            max_bytes=1_000_000,
        )
        manifest = loads_strict(text)
        if not isinstance(manifest, dict) or manifest.get("status") != "applied":
            raise ValueError("migration manifest is invalid")
        items = manifest.get("items")
        if not isinstance(items, list) or len(items) > MAX_MIGRATION_ITEMS:
            raise ValueError("migration manifest is invalid")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("migration manifest is invalid")
            try:
                _validate_account_id(item.get("account_id"))
            except ValueError as exc:
                raise ValueError("migration manifest is invalid") from exc
            status = item.get("status")
            if status == "canonical":
                continue
            if status != "applied":
                raise ValueError("migration manifest is invalid")
            target_text = item.get("target")
            if not isinstance(target_text, str) or not target_text:
                raise ValueError("migration manifest is invalid")
            target = _require_absolute(Path(target_text), "migration target")
            assert_no_symlink_ancestors(target, label="migration target")
            if not target.is_file() or target.is_symlink():
                continue
            text, file_stat = read_private_text(
                target,
                regular_label="migration target",
                read_label="migration target",
                max_bytes=MAX_AUTH_BYTES,
                too_large_label="migration target",
            )
            if file_stat.st_nlink != 1 or file_stat.st_mode & 0o077:
                raise ValueError("migration target permissions are invalid")
            raw = text.encode("utf-8")
            if hashlib.sha256(raw).hexdigest() != item.get("sha256"):
                raise ValueError("canonical auth.json changed after migration")
            target.unlink()
        manifest["status"] = "rolled_back"
        write_private_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=True, sort_keys=True) + "\n",
            label="migration manifest",
        )


def _source_for_account(
    account: Account,
    layout: ProfileLayout,
    search_roots: tuple[Path, ...],
) -> Path | None:
    if account.auth_json_path is not None and account.auth_json_path != "":
        if not isinstance(account.auth_json_path, str):
            raise ValueError("auth source is invalid")
        try:
            return Path(account.auth_json_path).expanduser()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("auth source cannot be resolved") from exc
    candidates = [layout.profile_dir / "auth.json"]
    if len(search_roots) == 1:
        candidates.append(search_roots[0] / "auth.json")
    candidates.extend(root / account.id / "auth.json" for root in search_roots)
    candidates.extend(root / f"{account.id}.json" for root in search_roots)
    existing_by_normalized: dict[Path, Path] = {}
    for path in candidates:
        if path.exists():
            normalized = Path(os.path.normpath(str(path)))
            existing_by_normalized.setdefault(normalized, path)
    existing = tuple(existing_by_normalized.values())
    if len(existing) > 1:
        raise ValueError(f"multiple auth sources for {account.id}")
    return existing[0] if existing else None


def _classify_source(source: Path | None, target: Path) -> tuple[str, str | None]:
    if source is None:
        return "missing", "no unambiguous auth source"
    source = _require_absolute(source, "auth source")
    if source.is_symlink():
        return "conflict", "auth source is a symlink"
    try:
        assert_no_symlink_ancestors(source, label="auth source")
    except ValueError as exc:
        return "conflict", str(exc)
    if target.is_symlink():
        return "conflict", "canonical auth target already exists"
    try:
        assert_no_symlink_ancestors(target, label="migration target")
    except ValueError as exc:
        return "conflict", str(exc)
    if Path(os.path.normpath(str(source))) == Path(os.path.normpath(str(target))):
        return "canonical", None
    if not source.is_file():
        return "missing", "auth source does not exist"
    if target.exists():
        return "conflict", "canonical auth target already exists"
    try:
        mode = source.stat().st_mode & 0o777
    except OSError:
        return "conflict", "auth source cannot be inspected"
    if mode & 0o077:
        return "conflict", "auth source is not private"
    return "planned", None


def _assert_migration_target_available(target: Path) -> None:
    assert_no_symlink_ancestors(target, label="migration target")
    if target.is_symlink() or target.exists():
        raise ValueError(f"canonical auth target is an existing file: {target}")


def _cleanup_created_migration_files(
    files: list[tuple[Path, int, int]],
) -> list[str]:
    errors: list[str] = []
    for target, device, inode in reversed(files):
        try:
            assert_no_symlink_ancestors(target, label="migration cleanup target")
            target_stat = target.lstat()
            if (
                not stat.S_ISREG(target_stat.st_mode)
                or target_stat.st_nlink != 1
                or target_stat.st_dev != device
                or target_stat.st_ino != inode
                or target_stat.st_mode & 0o077
            ):
                continue
            target.unlink()
        except (OSError, ValueError) as exc:
            errors.append(f"{target}: {exc}")
    return errors


def _cleanup_created_migration_directories(
    directories: list[tuple[Path, int, int]],
) -> list[str]:
    errors: list[str] = []
    for directory, device, inode in reversed(directories):
        try:
            assert_no_symlink_ancestors(directory, label="migration cleanup directory")
            directory_stat = directory.lstat()
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or directory_stat.st_dev != device
                or directory_stat.st_ino != inode
                or directory_stat.st_mode & 0o077
            ):
                continue
            directory.rmdir()
        except (OSError, ValueError) as exc:
            errors.append(f"{directory}: {exc}")
    return errors


def _validate_auth_json(text: str) -> None:
    value = loads_strict(text)
    if not isinstance(value, dict):
        raise ValueError("auth source must contain a JSON object")


def _record(item: AuthMigrationItem, status: str, digest: str | None) -> dict[str, object]:
    result: dict[str, object] = {
        "account_id": item.account_id,
        "source": str(item.source) if item.source is not None else None,
        "target": str(item.target),
        "status": status,
    }
    if digest is not None:
        result["sha256"] = digest
    return result


def _require_absolute(path: Path, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return path


def _validate_migration_plan(plan: AuthMigrationPlan) -> None:
    if not isinstance(plan, AuthMigrationPlan):
        raise ValueError("migration plan is invalid")
    if (
        not isinstance(plan.migration_id, str)
        or not plan.migration_id
        or len(plan.migration_id) > 128
    ):
        raise ValueError("migration plan is invalid")
    if not isinstance(plan.created_at, datetime):
        raise ValueError("migration plan is invalid")
    try:
        if plan.created_at.tzinfo is None or plan.created_at.utcoffset() is None:
            raise ValueError("migration plan is invalid")
        plan.created_at.astimezone(UTC)
    except Exception as exc:
        raise ValueError("migration plan is invalid") from exc
    if not isinstance(plan.items, tuple) or len(plan.items) > MAX_MIGRATION_ITEMS:
        raise ValueError("migration plan is invalid")
    account_ids: set[str] = set()
    sources: set[Path] = set()
    targets: set[Path] = set()
    for item in plan.items:
        if not isinstance(item, AuthMigrationItem):
            raise ValueError("migration plan is invalid")
        try:
            _validate_account_id(item.account_id)
        except ValueError as exc:
            raise ValueError("migration plan is invalid") from exc
        if item.account_id in account_ids:
            raise ValueError("migration plan is invalid")
        account_ids.add(item.account_id)
        if (
            not isinstance(item.target, Path)
            or not item.target.is_absolute()
            or (item.source is not None and (
                not isinstance(item.source, Path) or not item.source.is_absolute()
            ))
            or not isinstance(item.status, str)
        ):
            raise ValueError("migration plan is invalid")
        if item.target in targets:
            raise ValueError("migration plan is invalid")
        targets.add(item.target)
        if item.source is not None:
            if item.source in sources:
                raise ValueError("migration plan is invalid")
            sources.add(item.source)


def _assert_manifest_path_disjoint(
    plan: AuthMigrationPlan,
    manifest_path: Path,
) -> None:
    try:
        normalized_manifest = manifest_path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("manifest path cannot be resolved safely") from exc
    for item in plan.items:
        managed_metadata = item.target.parent.parent / "profile.json"
        for candidate, label in (
            (item.source, "auth source"),
            (item.target, "migration target"),
            (managed_metadata, "profile metadata"),
        ):
            if candidate is None:
                continue
            candidate = _require_absolute(candidate, label)
            try:
                normalized_candidate = candidate.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"{label} cannot be resolved safely") from exc
            if normalized_candidate == normalized_manifest:
                raise ValueError("manifest path conflicts with migration path")
