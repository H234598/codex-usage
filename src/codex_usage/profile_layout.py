from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path

from .models import Account
from .private_io import (
    assert_no_symlink_ancestors,
    ensure_private_directory,
    private_path_lock,
    write_private_text,
)


@dataclass(frozen=True)
class ProfileLayout:
    account_id: str
    profile_dir: Path
    codex_home: Path
    auth_json: Path
    metadata: Path
    jobs: Path
    migration: Path


def layout_for_account(account: Account) -> ProfileLayout:
    if not isinstance(account, Account):
        raise ValueError("account is invalid")
    profile_dir = Path(account.profile_dir).expanduser()
    if not profile_dir.is_absolute():
        raise ValueError("profile dir must be absolute")
    assert_no_symlink_ancestors(profile_dir, label="profile dir")
    if profile_dir.is_symlink():
        raise ValueError("profile dir must not be a symlink")
    return ProfileLayout(
        account_id=account.id,
        profile_dir=profile_dir,
        codex_home=profile_dir / "codex-home",
        auth_json=profile_dir / "codex-home" / "auth.json",
        metadata=profile_dir / "profile.json",
        jobs=profile_dir / "jobs",
        migration=profile_dir / "migration",
    )


def ensure_profile_layout(
    account: Account,
    *,
    created_directories: list[tuple[Path, int, int]] | None = None,
    created_files: list[tuple[Path, int, int]] | None = None,
    preserve_existing_metadata: bool = False,
) -> ProfileLayout:
    layout = layout_for_account(account)
    _ensure_directory(
        layout.profile_dir,
        "profile dir",
        created_directories=created_directories,
    )
    for directory, label in (
        (layout.codex_home, "codex home"),
        (layout.jobs, "profile jobs"),
        (layout.migration, "profile migration"),
    ):
        _ensure_directory(
            directory,
            label,
            created_directories=created_directories,
        )
    if layout.auth_json.is_symlink() or (
        layout.auth_json.exists() and not layout.auth_json.is_file()
    ):
        raise ValueError("canonical auth.json must be a regular file")
    metadata_lock = layout.profile_dir.parent / f".{layout.profile_dir.name}.profile-metadata"
    with private_path_lock(metadata_lock, label="profile metadata lock"):
        if preserve_existing_metadata and layout.metadata.is_symlink():
            raise ValueError("profile metadata must be a regular file")
        if preserve_existing_metadata and layout.metadata.exists():
            if not layout.metadata.is_file():
                raise ValueError("profile metadata must be a regular file")
            return layout
        metadata_missing = not layout.metadata.exists() and not layout.metadata.is_symlink()
        try:
            write_private_text(
                layout.metadata,
                json.dumps(
                    {"account_id": account.id, "label": account.label, "schema_version": 1},
                    ensure_ascii=True,
                    sort_keys=True,
                )
                + "\n",
                label="profile metadata",
                replace_existing=not metadata_missing,
            )
        except Exception:
            if metadata_missing:
                _record_created_file(layout.metadata, created_files)
            raise
        if metadata_missing:
            _record_created_file(layout.metadata, created_files)
    return layout


def _ensure_directory(
    path: Path,
    label: str,
    *,
    created_directories: list[tuple[Path, int, int]] | None = None,
) -> None:
    ensure_private_directory(
        path,
        label=label,
        created_paths=created_directories,
    )


def _record_created_file(
    path: Path,
    created_files: list[tuple[Path, int, int]] | None,
) -> None:
    if created_files is None:
        return
    try:
        item = path.lstat()
    except OSError:
        return
    if stat.S_ISREG(item.st_mode) and item.st_nlink == 1:
        created_files.append((path, item.st_dev, item.st_ino))
