import json
from pathlib import Path

import pytest

import codex_usage.profile_layout as profile_layout_module
from codex_usage.models import Account
from codex_usage.profile_layout import ensure_profile_layout, layout_for_account


def _account(tmp_path: Path) -> Account:
    return Account(id="alpha", label="Alpha", profile_dir=str(tmp_path / "profile"))


def test_profile_layout_uses_canonical_codex_home_and_private_metadata(tmp_path):
    profile = tmp_path / "parent" / "nested" / "profile"
    layout = ensure_profile_layout(
        Account(id="alpha", label="Alpha", profile_dir=str(profile))
    )

    assert layout.codex_home == profile / "codex-home"
    assert layout.auth_json == layout.codex_home / "auth.json"
    assert layout.jobs.is_dir()
    assert layout.migration.is_dir()
    assert layout.codex_home.stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "parent").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "parent" / "nested").stat().st_mode & 0o777 == 0o700
    assert layout.metadata.stat().st_mode & 0o777 == 0o600
    assert json.loads(layout.metadata.read_text(encoding="utf-8")) == {
        "account_id": "alpha",
        "label": "Alpha",
        "schema_version": 1,
    }


def test_profile_layout_rejects_non_account_input():
    with pytest.raises(ValueError, match="account is invalid"):
        layout_for_account(object())  # type: ignore[arg-type]


def test_profile_layout_requires_absolute_profile_directory():
    with pytest.raises(ValueError, match="must be absolute"):
        layout_for_account(Account(id="alpha", label="Alpha", profile_dir="relative"))


def test_profile_metadata_write_uses_private_path_lock(tmp_path, monkeypatch):
    observed: list[tuple[object, dict[str, object]]] = []
    original_lock = profile_layout_module.private_path_lock

    def traced_lock(lock_path, **kwargs):
        observed.append((lock_path, kwargs))
        return original_lock(lock_path, **kwargs)

    monkeypatch.setattr(profile_layout_module, "private_path_lock", traced_lock)
    layout = ensure_profile_layout(_account(tmp_path))

    assert observed == [
        (
            layout.profile_dir.parent / f".{layout.profile_dir.name}.profile-metadata",
            {"label": "profile metadata lock"},
        )
    ]


def test_profile_layout_rejects_insecure_preserved_metadata(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    metadata = profile / "profile.json"
    metadata.write_text("{}\n", encoding="utf-8")
    metadata.chmod(0o640)

    with pytest.raises(ValueError, match="profile metadata"):
        ensure_profile_layout(
            _account(tmp_path),
            preserve_existing_metadata=True,
        )

    assert metadata.stat().st_mode & 0o777 == 0o640


def test_profile_layout_rejects_symlink_profile(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "profile"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        layout_for_account(Account(id="alpha", label="Alpha", profile_dir=str(link)))


def test_profile_layout_rejects_symlink_profile_target_after_ancestor_scan(
    tmp_path, monkeypatch
):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "profile"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(
        profile_layout_module,
        "assert_no_symlink_ancestors",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(ValueError, match="must not be a symlink"):
        layout_for_account(Account(id="alpha", label="Alpha", profile_dir=str(link)))


@pytest.mark.parametrize("profile_dir", [None, "", [], 1, object()])
def test_profile_layout_rejects_invalid_profile_directory_type(profile_dir):
    with pytest.raises(ValueError, match="profile dir is invalid"):
        layout_for_account(  # type: ignore[arg-type]
            Account(id="alpha", label="Alpha", profile_dir=profile_dir)
        )


@pytest.mark.parametrize("account_id", ["../", "__all_accounts__", "", None])
def test_profile_layout_rejects_invalid_account_id(tmp_path, account_id):
    with pytest.raises(ValueError, match="account id"):
        layout_for_account(
            Account(
                id=account_id,
                label="Account",
                profile_dir=str(tmp_path / "profile"),
            )  # type: ignore[arg-type]
        )


def test_profile_layout_rejects_invalid_label_before_creating_profile(tmp_path):
    profile = tmp_path / "profile"

    with pytest.raises(ValueError, match="account label"):
        layout_for_account(
            Account(id="alpha", label=object(), profile_dir=str(profile))  # type: ignore[arg-type]
        )

    assert not profile.exists()


def test_profile_layout_rejects_unknown_home_user():
    account = Account(
        id="work",
        label="Work",
        profile_dir="~definitely-no-such-user-zzzz/profile",
    )

    with pytest.raises(ValueError, match="profile dir is invalid"):
        layout_for_account(account)


def test_profile_directory_helper_rejects_protected_target_before_chmod(
    tmp_path, monkeypatch
):
    protected = tmp_path / "protected"
    protected.mkdir()
    monkeypatch.setattr(
        Path,
        "home",
        classmethod(lambda _cls: protected),
    )

    with pytest.raises(ValueError, match="protected"):
        profile_layout_module._ensure_directory(protected, "profile dir")


def test_profile_layout_rejects_non_regular_auth_json(tmp_path):
    profile = tmp_path / "profile"
    (profile / "codex-home").mkdir(parents=True)
    (profile / "codex-home" / "auth.json").mkdir()

    with pytest.raises(ValueError, match=r"canonical auth\.json"):
        ensure_profile_layout(_account(tmp_path))


def test_profile_layout_rejects_preserved_metadata_symlink(tmp_path):
    layout = ensure_profile_layout(_account(tmp_path))
    layout.metadata.unlink()
    target = tmp_path / "metadata-target"
    target.write_text("{}\n", encoding="utf-8")
    layout.metadata.symlink_to(target)

    with pytest.raises(ValueError, match="profile metadata must be a regular file"):
        ensure_profile_layout(_account(tmp_path), preserve_existing_metadata=True)


def test_profile_layout_maps_metadata_lstat_error(tmp_path, monkeypatch):
    layout = ensure_profile_layout(_account(tmp_path))
    original_lstat = Path.lstat

    def fail_metadata_lstat(path):
        if path == layout.metadata:
            raise OSError("metadata disappeared")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_metadata_lstat)

    with pytest.raises(ValueError, match="profile metadata must be a regular file"):
        ensure_profile_layout(_account(tmp_path), preserve_existing_metadata=True)


def test_profile_layout_preserves_secure_existing_metadata(tmp_path):
    layout = ensure_profile_layout(_account(tmp_path))

    preserved = ensure_profile_layout(_account(tmp_path), preserve_existing_metadata=True)

    assert preserved == layout


def test_profile_layout_records_metadata_write_failure(tmp_path, monkeypatch):
    path = tmp_path / "profile" / "profile.json"
    monkeypatch.setattr(
        profile_layout_module,
        "write_private_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("metadata write failed")),
    )

    with pytest.raises(OSError, match="metadata write failed"):
        ensure_profile_layout(_account(tmp_path), created_files=[])

    assert not path.exists()


def test_profile_layout_does_not_record_existing_metadata_on_write_failure(
    tmp_path, monkeypatch
):
    layout = ensure_profile_layout(_account(tmp_path))
    original = layout.metadata.read_text(encoding="utf-8")
    monkeypatch.setattr(
        profile_layout_module,
        "write_private_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("metadata write failed")),
    )

    with pytest.raises(OSError, match="metadata write failed"):
        ensure_profile_layout(_account(tmp_path), created_files=[])

    assert layout.metadata.read_text(encoding="utf-8") == original


def test_profile_layout_rewrite_does_not_record_existing_metadata(tmp_path):
    ensure_profile_layout(_account(tmp_path))
    created_files = []

    ensure_profile_layout(_account(tmp_path), created_files=created_files)

    assert created_files == []


def test_profile_layout_records_only_private_regular_created_files(tmp_path):
    missing = tmp_path / "missing"
    directory = tmp_path / "directory"
    directory.mkdir()
    regular = tmp_path / "regular"
    regular.write_text("data", encoding="utf-8")
    created = []

    profile_layout_module._record_created_file(missing, created)
    profile_layout_module._record_created_file(directory, created)
    profile_layout_module._record_created_file(regular, created)

    assert created == [(regular, regular.stat().st_dev, regular.stat().st_ino)]


@pytest.mark.parametrize("created_directories", ["invalid", (), {}])
def test_profile_layout_rejects_invalid_created_directories(tmp_path, created_directories):
    with pytest.raises(ValueError, match="created_directories is invalid"):
        ensure_profile_layout(
            _account(tmp_path),
            created_directories=created_directories,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("created_files", ["invalid", (), {}])
def test_profile_layout_rejects_invalid_created_files(tmp_path, created_files):
    with pytest.raises(ValueError, match="created_files is invalid"):
        ensure_profile_layout(
            _account(tmp_path),
            created_files=created_files,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("preserve_existing_metadata", [None, 0, "yes", []])
def test_profile_layout_rejects_invalid_metadata_flag(
    tmp_path, preserve_existing_metadata
):
    with pytest.raises(ValueError, match="preserve_existing_metadata is invalid"):
        ensure_profile_layout(
            _account(tmp_path),
            preserve_existing_metadata=preserve_existing_metadata,  # type: ignore[arg-type]
        )
