from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .json_utils import loads_strict
from .private_io import read_private_text

_MANIFEST_MAX_BYTES = 128 * 1024
MAX_ATTESTATION_FILE_BYTES = 4 * 1024 * 1024
MAX_RELEASE_TREE_ENTRIES = 4096
MAX_RELEASE_TREE_BYTES = 128 * 1024 * 1024
_DIST_INFO_PREFIX = "codex_usage_integration_producer-0.6.532.dist-info"
_EXPECTED_VERSION = "0.6.532"
_EXPECTED_DISTRIBUTION = "codex-usage-integration-producer"


class IntegrationAttestationUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ActiveRelease:
    version: str
    release_dir: Path
    launcher_path: Path
    entrypoint_path: Path
    entrypoint_sha256: str
    wheel_sha256: str
    record_sha256: str
    launcher_sha256: str
    release_tree_sha256: str


def _unavailable() -> IntegrationAttestationUnavailable:
    return IntegrationAttestationUnavailable()


def _private_regular(path: Path, *, mode: int) -> os.stat_result:
    try:
        item = path.lstat()
    except (OSError, ValueError):
        raise _unavailable() from None
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_nlink != 1
        or stat.S_IMODE(item.st_mode) != mode
        or item.st_uid != os.getuid()
    ):
        raise _unavailable()
    return item


def _private_directory(path: Path, *, mode: int = 0o700) -> os.stat_result:
    try:
        item = path.lstat()
    except (OSError, ValueError):
        raise _unavailable() from None
    if (
        not stat.S_ISDIR(item.st_mode)
        or stat.S_IMODE(item.st_mode) != mode
        or item.st_uid != os.getuid()
    ):
        raise _unavailable()
    return item


def _file_bytes(path: Path, *, mode: int) -> bytes:
    item = _private_regular(path, mode=mode)
    if item.st_size > MAX_ATTESTATION_FILE_BYTES:
        raise _unavailable()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
        item = os.fstat(fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or item.st_uid != os.getuid()
            or stat.S_IMODE(item.st_mode) != mode
        ):
            raise _unavailable()
        if item.st_size > MAX_ATTESTATION_FILE_BYTES:
            raise _unavailable()
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            payload = handle.read(MAX_ATTESTATION_FILE_BYTES + 1)
            if len(payload) > MAX_ATTESTATION_FILE_BYTES:
                raise _unavailable()
            return payload
    except (OSError, ValueError):
        raise _unavailable() from None
    finally:
        if "fd" in locals() and fd >= 0:
            os.close(fd)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _valid_hash(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise _unavailable()
    try:
        int(value, 16)
    except ValueError:
        raise _unavailable() from None
    return value


def _absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _unavailable()
    path = Path(value)
    if not path.is_absolute() or str(path) != value:
        raise _unavailable()
    return path


def _contained(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise _unavailable() from None
    if not relative.parts or any(
        part in {"", ".", ".."} or "\\" in part for part in relative.parts
    ):
        raise _unavailable()


def _release_tree_rows(*, release_dir: Path) -> list[bytes]:
    root_stat = release_dir.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or release_dir.is_symlink():
        raise _unavailable()
    rows: list[bytes] = []
    entries_seen = 1
    file_bytes = 0

    def walk(path: Path, relative: str, item: os.stat_result) -> None:
        nonlocal entries_seen, file_bytes
        mode = stat.S_IMODE(item.st_mode)
        if stat.S_ISDIR(item.st_mode):
            rows.append(f"D {relative}\0{mode:04o}\n".encode())
            try:
                with os.scandir(path) as entries:
                    children = []
                    for entry in entries:
                        if entries_seen >= MAX_RELEASE_TREE_ENTRIES:
                            raise _unavailable()
                        entries_seen += 1
                        children.append(entry)
                    children.sort(key=lambda entry: entry.name)
                for entry in children:
                    name = entry.name
                    if not name or name in {".", ".."} or "\\" in name:
                        raise _unavailable()
                    child = path / name
                    child_stat = child.lstat()
                    walk(child, f"{relative}/{name}", child_stat)
            except (OSError, ValueError):
                raise _unavailable() from None
            return
        if stat.S_ISREG(item.st_mode):
            if item.st_nlink != 1:
                raise _unavailable()
            if item.st_size > MAX_ATTESTATION_FILE_BYTES:
                raise _unavailable()
            if file_bytes + item.st_size > MAX_RELEASE_TREE_BYTES:
                raise _unavailable()
            payload = _read_nofollow_bytes(path)
            file_bytes += len(payload)
            if file_bytes > MAX_RELEASE_TREE_BYTES:
                raise _unavailable()
            rows.append(
                f"F {relative}\0{mode:04o}\0{len(payload)}\0".encode()
                + _sha256_bytes(payload).encode("ascii")
                + b"\n"
            )
            return
        raise _unavailable()

    walk(release_dir, ".", root_stat)
    return rows


def _read_nofollow_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        item = os.fstat(fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or item.st_size > MAX_ATTESTATION_FILE_BYTES
        ):
            raise _unavailable()
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            payload = handle.read(MAX_ATTESTATION_FILE_BYTES + 1)
            if len(payload) > MAX_ATTESTATION_FILE_BYTES:
                raise _unavailable()
            return payload
    except (OSError, ValueError):
        raise _unavailable() from None
    finally:
        if fd >= 0:
            os.close(fd)


def _release_tree_sha256(*, release_dir: Path) -> str:
    try:
        rows = _release_tree_rows(release_dir=release_dir)
        return hashlib.sha256(b"".join(rows)).hexdigest()
    except IntegrationAttestationUnavailable:
        raise
    except Exception:
        raise _unavailable() from None


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        text, item = read_private_text(
            path,
            regular_label="integration manifest",
            read_label="integration manifest",
            max_bytes=_MANIFEST_MAX_BYTES,
            too_large_label="integration manifest",
            invalid_utf8_label="integration manifest",
        )
    except Exception:
        raise _unavailable() from None
    if item.st_nlink != 1 or stat.S_IMODE(item.st_mode) != 0o600:
        raise _unavailable()
    try:
        value = loads_strict(text)
    except Exception:
        raise _unavailable() from None
    if not isinstance(value, dict):
        raise _unavailable()
    return value


def _manifest_string(manifest: Mapping[str, object], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value:
        raise _unavailable()
    return value


def _record_digest(value: str, payload: bytes) -> bool:
    if not value.startswith("sha256="):
        return False
    encoded = value[7:]
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, binascii.Error):
        return False
    return decoded == hashlib.sha256(payload).digest()


def _record_rows(record_path: Path, release_dir: Path) -> dict[str, tuple[str, int]]:
    payload = _file_bytes(record_path, mode=0o600)
    site_packages = record_path.parent.parent
    seen: set[str] = set()
    validated: dict[str, tuple[str, int]] = {}
    try:
        reader = csv.reader(io.StringIO(payload.decode("utf-8")))
        rows_seen = 0
        for row in reader:
            rows_seen += 1
            if rows_seen > MAX_RELEASE_TREE_ENTRIES:
                raise _unavailable()
            if len(row) != 3 or row[0] in seen:
                raise _unavailable()
            relative_text, digest, size_text = row
            seen.add(relative_text)
            if (
                not relative_text
                or relative_text.startswith("/")
                or "\\" in relative_text
                or "\x00" in relative_text
                or any(part in {"", ".", ".."} for part in relative_text.split("/"))
            ):
                raise _unavailable()
            target = site_packages / relative_text
            _contained(target, release_dir)
            item = _private_regular(target, mode=0o600)
            target_payload = _read_nofollow_bytes(target)
            if digest or size_text:
                if not digest or not size_text or not size_text.isdecimal():
                    raise _unavailable()
                if not _record_digest(digest, target_payload) or int(size_text) != item.st_size:
                    raise _unavailable()
                validated[relative_text] = (digest, item.st_size)
            elif target != record_path:
                raise _unavailable()
            else:
                validated[relative_text] = ("", -1)
    except (UnicodeDecodeError, csv.Error):
        raise _unavailable() from None
    if not seen:
        raise _unavailable()
    if str(record_path.relative_to(site_packages).as_posix()) not in seen:
        raise _unavailable()
    return validated


def _verify_manifest(
    *,
    manifest_path: Path,
    state_home: Path,
    data_home: Path,
    expected_entrypoint_path: Path | None,
) -> ActiveRelease:
    manifest = _read_manifest(manifest_path)
    schema_version = manifest.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise _unavailable()
    if manifest.get("version") != _EXPECTED_VERSION:
        raise _unavailable()
    source_manifest_digest = _valid_hash(manifest.get("source_manifest_sha256"))
    manifest_state = _absolute_path(manifest.get("state_home"))
    manifest_data = _absolute_path(manifest.get("data_home"))
    if manifest_state != state_home or manifest_data != data_home:
        raise _unavailable()
    _private_directory(state_home)
    _private_directory(data_home)
    integration = state_home / "codex-usage" / "integration"
    _private_directory(state_home / "codex-usage")
    _private_directory(integration)

    release_dir = _absolute_path(manifest.get("release_dir"))
    releases_dir = integration / "releases"
    _private_directory(releases_dir)
    _contained(release_dir, releases_dir)
    _private_directory(release_dir)
    release_id = f"{_EXPECTED_VERSION}-{source_manifest_digest[:16]}"
    if (
        release_dir.parent != releases_dir
        or release_dir.name != release_id
        or manifest.get("release_id") != release_id
    ):
        raise _unavailable()

    launcher_path = _absolute_path(manifest.get("launcher_path"))
    entrypoint_path = _absolute_path(manifest.get("entrypoint_path"))
    wheel_path = _absolute_path(manifest.get("wheel_path"))
    record_path = _absolute_path(manifest.get("record_path"))
    for path in (launcher_path, entrypoint_path, wheel_path, record_path):
        _contained(path, release_dir)
    if expected_entrypoint_path is not None:
        if not isinstance(expected_entrypoint_path, Path):
            raise _unavailable()
        if (
            not expected_entrypoint_path.is_absolute()
            or expected_entrypoint_path != entrypoint_path
        ):
            raise _unavailable()

    entrypoint_payload = _file_bytes(entrypoint_path, mode=0o600)
    wheel_payload = _file_bytes(wheel_path, mode=0o600)
    record_payload = _file_bytes(record_path, mode=0o600)
    launcher_payload = _file_bytes(launcher_path, mode=0o700)
    entrypoint_hash = _valid_hash(manifest.get("entrypoint_sha256"))
    wheel_hash = _valid_hash(manifest.get("wheel_sha256"))
    record_hash = _valid_hash(manifest.get("record_sha256"))
    launcher_hash = _valid_hash(manifest.get("launcher_sha256"))
    tree_hash = _valid_hash(manifest.get("release_tree_sha256"))
    if (
        _sha256_bytes(entrypoint_payload) != entrypoint_hash
        or _sha256_bytes(wheel_payload) != wheel_hash
        or _sha256_bytes(record_payload) != record_hash
        or _sha256_bytes(launcher_payload) != launcher_hash
    ):
        raise _unavailable()
    if b" -B -I -m codex_usage.integration_entrypoint" not in launcher_payload:
        raise _unavailable()
    record_rows = _record_rows(record_path, release_dir)
    entrypoint_relative = entrypoint_path.relative_to(record_path.parent.parent).as_posix()
    entrypoint_record = record_rows.get(entrypoint_relative)
    if (
        entrypoint_record is None
        or not entrypoint_record[0]
        or entrypoint_record[1] != len(entrypoint_payload)
        or not _record_digest(entrypoint_record[0], entrypoint_payload)
    ):
        raise _unavailable()
    try:
        metadata_path = record_path.parent / "METADATA"
        metadata = _read_nofollow_bytes(metadata_path).decode("utf-8")
    except (UnicodeDecodeError, IntegrationAttestationUnavailable):
        raise _unavailable() from None
    if (
        f"Version: {_EXPECTED_VERSION}\n" not in metadata
        or f"Name: {_EXPECTED_DISTRIBUTION}\n" not in metadata
    ):
        raise _unavailable()
    if _release_tree_sha256(release_dir=release_dir) != tree_hash:
        raise _unavailable()
    return ActiveRelease(
        version=_EXPECTED_VERSION,
        release_dir=release_dir,
        launcher_path=launcher_path,
        entrypoint_path=entrypoint_path,
        entrypoint_sha256=entrypoint_hash,
        wheel_sha256=wheel_hash,
        record_sha256=record_hash,
        launcher_sha256=launcher_hash,
        release_tree_sha256=tree_hash,
    )


def verify_active_release(
    *,
    state_home: Path,
    data_home: Path,
    expected_entrypoint_path: Path,
) -> ActiveRelease:
    try:
        return _verify_manifest(
            manifest_path=state_home / "codex-usage" / "integration" / "active.json",
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=expected_entrypoint_path,
        )
    except IntegrationAttestationUnavailable:
        raise
    except Exception:
        raise _unavailable() from None
