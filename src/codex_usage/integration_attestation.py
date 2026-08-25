from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .json_utils import loads_strict
from .private_io import (
    FileIdentity,
    IntegrationEvidenceInvalid,
    IntegrationEvidenceUnavailable,
    open_private_dir_at,
    open_verified_state_home,
    read_private_bytes_at,
    read_private_text,
)

_MANIFEST_MAX_BYTES = 128 * 1024
MAX_ATTESTATION_FILE_BYTES = 4 * 1024 * 1024
MAX_RELEASE_TREE_ENTRIES = 4096
MAX_RELEASE_TREE_BYTES = 128 * 1024 * 1024
_DIST_INFO_PREFIX = "codex_usage_integration_producer-0.6.535.dist-info"
_EXPECTED_VERSION = "0.6.535"
_EXPECTED_DISTRIBUTION = "codex-usage-integration-producer"
_PREVIOUS_SCHEMA2_DIST_INFO_PREFIX = "codex_usage_integration_producer-0.6.534.dist-info"
_PREVIOUS_SCHEMA2_VERSION = "0.6.534"
_LEGACY_SCHEMA2_DIST_INFO_PREFIX = "codex_usage_integration_producer-0.6.533.dist-info"
_LEGACY_SCHEMA2_VERSION = "0.6.533"
_LEGACY_DIST_INFO_PREFIX = "codex_usage_integration_producer-0.6.532.dist-info"
_LEGACY_VERSION = "0.6.532"
_CURRENT_SCHEMA2_MANIFEST_FIELDS = frozenset(
    {
        "data_home",
        "entrypoint_path",
        "entrypoint_sha256",
        "launcher_path",
        "launcher_sha256",
        "record_path",
        "record_sha256",
        "release_dir",
        "release_id",
        "release_tree_sha256",
        "schema_version",
        "source_manifest_sha256",
        "state_home",
        "version",
        "wheel_path",
        "wheel_sha256",
    }
)
_PREVIOUS_SCHEMA2_MANIFEST_FIELDS = frozenset(
    {
        "data_home",
        "entrypoint_path",
        "entrypoint_sha256",
        "launcher_path",
        "launcher_sha256",
        "record_path",
        "record_sha256",
        "release_dir",
        "release_id",
        "release_tree_sha256",
        "schema_version",
        "source_manifest_sha256",
        "state_home",
        "version",
        "wheel_path",
        "wheel_sha256",
    }
)
_LEGACY_SCHEMA1_MANIFEST_FIELDS = frozenset(
    {
        "data_home",
        "entrypoint_path",
        "entrypoint_sha256",
        "launcher_path",
        "launcher_sha256",
        "record_path",
        "record_sha256",
        "release_dir",
        "release_id",
        "release_tree_sha256",
        "schema_version",
        "source_manifest_sha256",
        "state_home",
        "version",
        "wheel_path",
        "wheel_sha256",
    }
)


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


@dataclass(frozen=True)
class VerifiedActiveManifest:
    active_release: ActiveRelease
    release_id: str
    source_manifest_sha256: str
    active_manifest_bytes: bytes
    active_manifest_sha256: str
    state_home_identity: FileIdentity
    integration_parent_identity: FileIdentity
    active_file_identity: FileIdentity


@dataclass(frozen=True)
class _ReleaseTreeEvidence:
    releases_identity: FileIdentity
    entry_identities: tuple[tuple[str, FileIdentity], ...]
    rows: tuple[bytes, ...]


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
    initial_item = _private_regular(path, mode=mode)
    if initial_item.st_size > MAX_ATTESTATION_FILE_BYTES:
        raise _unavailable()
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    parent_fd = -1
    try:
        parent_item = path.parent.lstat()
        if not stat.S_ISDIR(parent_item.st_mode) or parent_item.st_uid != os.getuid():
            raise _unavailable()
        parent_fd = os.open(path.parent, directory_flags)
        opened_parent = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or opened_parent.st_uid != os.getuid()
            or opened_parent.st_dev != parent_item.st_dev
            or opened_parent.st_ino != parent_item.st_ino
        ):
            raise _unavailable()
        fd = os.open(path.name, file_flags, dir_fd=parent_fd)
        item = os.fstat(fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or item.st_uid != os.getuid()
            or stat.S_IMODE(item.st_mode) != mode
            or item.st_dev != initial_item.st_dev
            or item.st_ino != initial_item.st_ino
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
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _valid_hash(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise _unavailable()
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
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    root_fd = -1
    try:
        root_fd = os.open(release_dir, directory_flags)
        root_stat = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or stat.S_ISLNK(root_stat.st_mode)
            or root_stat.st_uid != os.getuid()
        ):
            raise _unavailable()
    except (OSError, ValueError, IntegrationAttestationUnavailable):
        if root_fd >= 0:
            try:
                os.close(root_fd)
            except OSError:
                pass
            root_fd = -1
        raise _unavailable() from None
    rows: list[bytes] = []
    entries_seen = 1
    file_bytes = 0
    stack: list[tuple[int, str, os.stat_result]] = [(root_fd, ".", root_stat)]
    root_fd = -1
    try:
        while stack:
            directory_fd, relative, item = stack.pop()
            try:
                mode = stat.S_IMODE(item.st_mode)
                if stat.S_ISDIR(item.st_mode):
                    rows.append(f"D {relative}\0{mode:04o}\n".encode())
                    children: list[tuple[str, int, os.stat_result]] = []
                    try:
                        with os.scandir(directory_fd) as entries:
                            for entry in entries:
                                if entries_seen >= MAX_RELEASE_TREE_ENTRIES:
                                    raise _unavailable()
                                entries_seen += 1
                                name = entry.name
                                if not name or name in {".", ".."} or "\\" in name:
                                    raise _unavailable()
                                child_item = entry.stat(follow_symlinks=False)
                                if stat.S_ISLNK(child_item.st_mode) or not (
                                    stat.S_ISDIR(child_item.st_mode)
                                    or stat.S_ISREG(child_item.st_mode)
                                ) or child_item.st_uid != os.getuid():
                                    raise _unavailable()
                                child_flags = (
                                    directory_flags
                                    if stat.S_ISDIR(child_item.st_mode)
                                    else file_flags
                                )
                                child_fd = -1
                                try:
                                    child_fd = os.open(
                                        name,
                                        child_flags,
                                        dir_fd=directory_fd,
                                    )
                                    opened_item = os.fstat(child_fd)
                                    if (
                                        stat.S_IFMT(opened_item.st_mode)
                                        != stat.S_IFMT(child_item.st_mode)
                                        or opened_item.st_dev != child_item.st_dev
                                        or opened_item.st_ino != child_item.st_ino
                                        or opened_item.st_uid != os.getuid()
                                    ):
                                        raise _unavailable()
                                    children.append((name, child_fd, opened_item))
                                    child_fd = -1
                                finally:
                                    if child_fd >= 0:
                                        os.close(child_fd)
                        children.sort(key=lambda child: child[0], reverse=True)
                        stack.extend(
                            (
                                child_fd,
                                f"{relative}/{name}",
                                child_item,
                            )
                            for name, child_fd, child_item in children
                        )
                        children.clear()
                    finally:
                        for _, child_fd, _ in children:
                            os.close(child_fd)
                    continue
                if stat.S_ISREG(item.st_mode):
                    if item.st_nlink != 1:
                        raise _unavailable()
                    if item.st_size > MAX_ATTESTATION_FILE_BYTES:
                        raise _unavailable()
                    if file_bytes + item.st_size > MAX_RELEASE_TREE_BYTES:
                        raise _unavailable()
                    file_fd = directory_fd
                    directory_fd = -1
                    payload = _read_nofollow_fd(file_fd)
                    file_bytes += len(payload)
                    if file_bytes > MAX_RELEASE_TREE_BYTES:
                        raise _unavailable()
                    rows.append(
                        f"F {relative}\0{mode:04o}\0{len(payload)}\0".encode()
                        + _sha256_bytes(payload).encode("ascii")
                        + b"\n"
                    )
                    continue
                raise _unavailable()
            finally:
                if directory_fd >= 0:
                    os.close(directory_fd)
        return rows
    except (OSError, ValueError):
        raise _unavailable() from None
    finally:
        for directory_fd, _, _ in stack:
            os.close(directory_fd)


def _release_tree_evidence_at(
    *,
    integration_fd: int,
    release_id: str,
) -> _ReleaseTreeEvidence:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    releases_fd = -1
    release_fd = -1
    stack: list[tuple[int, str, os.stat_result]] = []
    try:
        releases_fd = open_private_dir_at(integration_fd, "releases")
        releases_item = os.fstat(releases_fd)
        releases_identity = _fd_identity(releases_fd)
        release_fd = open_private_dir_at(releases_fd, release_id)
        release_item = os.fstat(release_fd)
        stack.append((release_fd, ".", release_item))
        release_fd = -1
        rows: list[bytes] = []
        identities: list[tuple[str, FileIdentity]] = []
        entries_seen = 1
        file_bytes = 0
        while stack:
            item_fd, relative, initial = stack.pop()
            try:
                mode = stat.S_IMODE(initial.st_mode)
                identity = FileIdentity(initial.st_dev, initial.st_ino, mode)
                identities.append((relative, identity))
                if stat.S_ISDIR(initial.st_mode):
                    if initial.st_uid != os.getuid():
                        raise _unavailable()
                    rows.append(f"D {relative}\0{mode:04o}\n".encode())
                    children: list[tuple[str, int, os.stat_result]] = []
                    try:
                        with os.scandir(item_fd) as entries:
                            for entry in entries:
                                if entries_seen >= MAX_RELEASE_TREE_ENTRIES:
                                    raise _unavailable()
                                entries_seen += 1
                                name = entry.name
                                if (
                                    not name
                                    or name in {".", ".."}
                                    or "/" in name
                                    or "\\" in name
                                    or "\x00" in name
                                ):
                                    raise _unavailable()
                                child_initial = entry.stat(follow_symlinks=False)
                                if stat.S_ISDIR(child_initial.st_mode):
                                    child_flags = directory_flags
                                elif stat.S_ISREG(child_initial.st_mode):
                                    child_flags = file_flags
                                else:
                                    raise _unavailable()
                                child_fd = -1
                                try:
                                    child_fd = os.open(
                                        name,
                                        child_flags,
                                        dir_fd=item_fd,
                                    )
                                    opened = os.fstat(child_fd)
                                    if (
                                        stat.S_IFMT(opened.st_mode)
                                        != stat.S_IFMT(child_initial.st_mode)
                                        or opened.st_dev != child_initial.st_dev
                                        or opened.st_ino != child_initial.st_ino
                                        or opened.st_uid != os.getuid()
                                        or opened.st_mode != child_initial.st_mode
                                        or (
                                            stat.S_ISREG(opened.st_mode)
                                            and (
                                                opened.st_nlink != 1
                                                or opened.st_size
                                                > MAX_ATTESTATION_FILE_BYTES
                                            )
                                        )
                                    ):
                                        raise _unavailable()
                                    children.append((name, child_fd, opened))
                                    child_fd = -1
                                finally:
                                    if child_fd >= 0:
                                        os.close(child_fd)
                        children.sort(key=lambda child: child[0], reverse=True)
                        stack.extend(
                            (
                                child_fd,
                                f"{relative}/{name}",
                                child_item,
                            )
                            for name, child_fd, child_item in children
                        )
                        children.clear()
                    finally:
                        for _, child_fd, _ in children:
                            os.close(child_fd)
                    final = os.fstat(item_fd)
                    if (
                        final.st_dev != initial.st_dev
                        or final.st_ino != initial.st_ino
                        or final.st_mode != initial.st_mode
                        or final.st_uid != initial.st_uid
                    ):
                        raise _unavailable()
                    continue
                if not stat.S_ISREG(initial.st_mode):
                    raise _unavailable()
                if (
                    initial.st_uid != os.getuid()
                    or initial.st_nlink != 1
                    or initial.st_size > MAX_ATTESTATION_FILE_BYTES
                    or file_bytes + initial.st_size > MAX_RELEASE_TREE_BYTES
                ):
                    raise _unavailable()
                payload = bytearray()
                while len(payload) <= MAX_ATTESTATION_FILE_BYTES:
                    chunk = os.read(
                        item_fd,
                        min(
                            65_536,
                            MAX_ATTESTATION_FILE_BYTES + 1 - len(payload),
                        ),
                    )
                    if not chunk:
                        break
                    payload.extend(chunk)
                if len(payload) > MAX_ATTESTATION_FILE_BYTES:
                    raise _unavailable()
                final = os.fstat(item_fd)
                if (
                    final.st_dev != initial.st_dev
                    or final.st_ino != initial.st_ino
                    or final.st_mode != initial.st_mode
                    or final.st_uid != initial.st_uid
                    or final.st_nlink != initial.st_nlink
                    or final.st_size != initial.st_size
                    or final.st_mtime_ns != initial.st_mtime_ns
                    or final.st_ctime_ns != initial.st_ctime_ns
                ):
                    raise _unavailable()
                file_bytes += len(payload)
                if file_bytes > MAX_RELEASE_TREE_BYTES:
                    raise _unavailable()
                rows.append(
                    f"F {relative}\0{mode:04o}\0{len(payload)}\0".encode()
                    + _sha256_bytes(bytes(payload)).encode("ascii")
                    + b"\n"
                )
            finally:
                os.close(item_fd)
        current_releases = os.fstat(releases_fd)
        if (
            current_releases.st_dev != releases_item.st_dev
            or current_releases.st_ino != releases_item.st_ino
            or current_releases.st_mode != releases_item.st_mode
            or current_releases.st_uid != releases_item.st_uid
        ):
            raise _unavailable()
        return _ReleaseTreeEvidence(
            releases_identity=releases_identity,
            entry_identities=tuple(identities),
            rows=tuple(rows),
        )
    except IntegrationAttestationUnavailable:
        raise
    except (OSError, ValueError):
        raise _unavailable() from None
    finally:
        for item_fd, _, _ in stack:
            os.close(item_fd)
        if release_fd >= 0:
            os.close(release_fd)
        if releases_fd >= 0:
            os.close(releases_fd)


def _read_nofollow_fd(fd: int) -> bytes:
    try:
        item = os.fstat(fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or item.st_uid != os.getuid()
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


def _read_nofollow_bytes(
    path: Path,
    *,
    expected_file_identity: os.stat_result | None = None,
) -> bytes:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    parent_fd = -1
    try:
        parent_item = path.parent.lstat()
        if not stat.S_ISDIR(parent_item.st_mode) or parent_item.st_uid != os.getuid():
            raise _unavailable()
        parent_fd = os.open(path.parent, directory_flags)
        opened_parent = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or opened_parent.st_uid != os.getuid()
            or opened_parent.st_dev != parent_item.st_dev
            or opened_parent.st_ino != parent_item.st_ino
        ):
            raise _unavailable()
        fd = os.open(path.name, file_flags, dir_fd=parent_fd)
        item = os.fstat(fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or item.st_uid != os.getuid()
            or item.st_size > MAX_ATTESTATION_FILE_BYTES
            or (
                expected_file_identity is not None
                and (
                    item.st_dev != expected_file_identity.st_dev
                    or item.st_ino != expected_file_identity.st_ino
                    or stat.S_IMODE(item.st_mode)
                    != stat.S_IMODE(expected_file_identity.st_mode)
                )
            )
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
        if parent_fd >= 0:
            os.close(parent_fd)


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


def _manifest_from_canonical_bytes(payload: bytes) -> dict[str, object]:
    try:
        text = payload.decode("utf-8")
        value = loads_strict(text)
    except (UnicodeDecodeError, ValueError):
        raise _unavailable() from None
    if not isinstance(value, dict):
        raise _unavailable()
    canonical = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if canonical != payload:
        raise _unavailable()
    return value


def _require_manifest_fields(
    manifest: dict[str, object],
    *,
    expected_fields: frozenset[str],
) -> dict[str, object]:
    if set(manifest) != expected_fields:
        raise _unavailable()
    return manifest


def _manifest_string(manifest: Mapping[str, object], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value:
        raise _unavailable()
    return value


def _record_digest(value: str, payload: bytes) -> bool:
    if type(value) is not str or not value.startswith("sha256="):
        return False
    encoded = value[7:]
    if len(encoded) != 43 or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in encoded
    ):
        return False
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, binascii.Error):
        return False
    expected = hashlib.sha256(payload).digest()
    return (
        decoded == expected
        and encoded == base64.urlsafe_b64encode(expected).decode("ascii").rstrip("=")
    )


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
            target_payload = _read_nofollow_bytes(
                target,
                expected_file_identity=item,
            )
            if digest or size_text:
                if not digest or not size_text or not size_text.isdecimal():
                    raise _unavailable()
                try:
                    size = int(size_text)
                except (OverflowError, ValueError):
                    raise _unavailable() from None
                if not _record_digest(digest, target_payload) or size != item.st_size:
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


def _verify_manifest_contract(
    *,
    manifest_path: Path,
    state_home: Path,
    data_home: Path,
    expected_entrypoint_path: Path | None,
    expected_schema_version: int,
    expected_version: str,
    expected_dist_info_prefix: str,
    expected_fields: frozenset[str],
    manifest_payload: bytes | None = None,
) -> ActiveRelease:
    manifest = _require_manifest_fields(
        (
            _read_manifest(manifest_path)
            if manifest_payload is None
            else _manifest_from_canonical_bytes(manifest_payload)
        ),
        expected_fields=expected_fields,
    )
    schema_version = manifest.get("schema_version")
    if type(schema_version) is not int or schema_version != expected_schema_version:
        raise _unavailable()
    if manifest.get("version") != expected_version:
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
    release_id = f"{expected_version}-{source_manifest_digest[:16]}"
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
    site_packages = record_path.parent.parent
    try:
        site_packages_parts = site_packages.relative_to(release_dir).parts
    except ValueError:
        raise _unavailable() from None
    python_directory = site_packages_parts[2] if len(site_packages_parts) == 4 else ""
    if (
        len(site_packages_parts) != 4
        or site_packages_parts[:2] != ("venv", "lib")
        or site_packages_parts[3] != "site-packages"
        or not python_directory.startswith("python3.")
        or not python_directory.removeprefix("python3.").isdecimal()
        or launcher_path != release_dir / "venv" / "bin" / "codex-usage"
        or wheel_path != release_dir / "producer.whl"
        or record_path
        != site_packages / expected_dist_info_prefix / "RECORD"
        or entrypoint_path
        != site_packages / "codex_usage" / "integration_entrypoint.py"
    ):
        raise _unavailable()
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
        f"Version: {expected_version}\n" not in metadata
        or f"Name: {_EXPECTED_DISTRIBUTION}\n" not in metadata
    ):
        raise _unavailable()
    if _release_tree_sha256(release_dir=release_dir) != tree_hash:
        raise _unavailable()
    return ActiveRelease(
        version=expected_version,
        release_dir=release_dir,
        launcher_path=launcher_path,
        entrypoint_path=entrypoint_path,
        entrypoint_sha256=entrypoint_hash,
        wheel_sha256=wheel_hash,
        record_sha256=record_hash,
        launcher_sha256=launcher_hash,
        release_tree_sha256=tree_hash,
    )


def _verify_manifest(
    *,
    manifest_path: Path,
    state_home: Path,
    data_home: Path,
    expected_entrypoint_path: Path | None,
) -> ActiveRelease:
    return _verify_manifest_contract(
        manifest_path=manifest_path,
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=expected_entrypoint_path,
        expected_schema_version=2,
        expected_version=_EXPECTED_VERSION,
        expected_dist_info_prefix=_DIST_INFO_PREFIX,
        expected_fields=_CURRENT_SCHEMA2_MANIFEST_FIELDS,
    )


def _verify_previous_schema2_manifest_for_upgrade(
    *,
    manifest_path: Path,
    state_home: Path,
    data_home: Path,
) -> ActiveRelease:
    return _verify_manifest_contract(
        manifest_path=manifest_path,
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=None,
        expected_schema_version=2,
        expected_version=_PREVIOUS_SCHEMA2_VERSION,
        expected_dist_info_prefix=_PREVIOUS_SCHEMA2_DIST_INFO_PREFIX,
        expected_fields=_PREVIOUS_SCHEMA2_MANIFEST_FIELDS,
    )


def _verify_legacy_manifest_for_upgrade(
    *,
    manifest_path: Path,
    state_home: Path,
    data_home: Path,
) -> ActiveRelease:
    return _verify_manifest_contract(
        manifest_path=manifest_path,
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=None,
        expected_schema_version=1,
        expected_version=_LEGACY_VERSION,
        expected_dist_info_prefix=_LEGACY_DIST_INFO_PREFIX,
        expected_fields=_LEGACY_SCHEMA1_MANIFEST_FIELDS,
    )


def _verify_legacy_schema2_manifest_for_upgrade(
    *,
    manifest_path: Path,
    state_home: Path,
    data_home: Path,
) -> ActiveRelease:
    return _verify_manifest_contract(
        manifest_path=manifest_path,
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=None,
        expected_schema_version=2,
        expected_version=_LEGACY_SCHEMA2_VERSION,
        expected_dist_info_prefix=_LEGACY_SCHEMA2_DIST_INFO_PREFIX,
        expected_fields=_PREVIOUS_SCHEMA2_MANIFEST_FIELDS,
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


def _before_active_identity_recheck(_integration_fd: int) -> None:
    return None


def _fd_identity(fd: int) -> FileIdentity:
    item = os.fstat(fd)
    return FileIdentity(item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode))


def verify_active_manifest_at(
    *,
    state_home: Path,
    data_home: Path,
    expected_entrypoint_path: Path,
) -> VerifiedActiveManifest:
    state_fd = -1
    app_fd = -1
    integration_fd = -1
    try:
        try:
            state_fd = open_verified_state_home(state_home)
            state_identity = _fd_identity(state_fd)
            app_fd = open_private_dir_at(state_fd, "codex-usage")
            integration_fd = open_private_dir_at(app_fd, "integration")
            integration_identity = _fd_identity(integration_fd)
            active_payload, active_identity = read_private_bytes_at(
                integration_fd,
                "active.json",
                maximum=_MANIFEST_MAX_BYTES,
                mode=0o600,
            )
        except FileNotFoundError as exc:
            raise IntegrationEvidenceUnavailable() from exc
        except OSError as exc:
            raise IntegrationEvidenceUnavailable() from exc
        except ValueError as exc:
            raise IntegrationEvidenceInvalid() from exc

        try:
            active_release = _verify_manifest_contract(
                manifest_path=(
                    state_home / "codex-usage" / "integration" / "active.json"
                ),
                state_home=state_home,
                data_home=data_home,
                expected_entrypoint_path=expected_entrypoint_path,
                expected_schema_version=2,
                expected_version=_EXPECTED_VERSION,
                expected_dist_info_prefix=_DIST_INFO_PREFIX,
                expected_fields=_CURRENT_SCHEMA2_MANIFEST_FIELDS,
                manifest_payload=active_payload,
            )
            manifest = _manifest_from_canonical_bytes(active_payload)
            release_id = _manifest_string(manifest, "release_id")
            source_manifest_sha256 = _valid_hash(
                manifest.get("source_manifest_sha256")
            )
            release_evidence = _release_tree_evidence_at(
                integration_fd=integration_fd,
                release_id=release_id,
            )
            if (
                hashlib.sha256(b"".join(release_evidence.rows)).hexdigest()
                != active_release.release_tree_sha256
            ):
                raise _unavailable()
        except IntegrationAttestationUnavailable as exc:
            raise IntegrationEvidenceUnavailable() from exc
        except Exception as exc:
            raise IntegrationEvidenceUnavailable() from exc

        try:
            _before_active_identity_recheck(integration_fd)
            repeated_payload, repeated_identity = read_private_bytes_at(
                integration_fd,
                "active.json",
                maximum=_MANIFEST_MAX_BYTES,
                mode=0o600,
            )
            if (
                _fd_identity(state_fd) != state_identity
                or _fd_identity(integration_fd) != integration_identity
                or repeated_identity != active_identity
                or repeated_payload != active_payload
            ):
                raise IntegrationEvidenceInvalid()
            if (
                _release_tree_evidence_at(
                    integration_fd=integration_fd,
                    release_id=release_id,
                )
                != release_evidence
            ):
                raise IntegrationEvidenceInvalid()

            fresh_state_fd = open_verified_state_home(state_home)
            fresh_app_fd = -1
            fresh_integration_fd = -1
            try:
                fresh_app_fd = open_private_dir_at(fresh_state_fd, "codex-usage")
                fresh_integration_fd = open_private_dir_at(
                    fresh_app_fd,
                    "integration",
                )
                fresh_payload, fresh_active_identity = read_private_bytes_at(
                    fresh_integration_fd,
                    "active.json",
                    maximum=_MANIFEST_MAX_BYTES,
                    mode=0o600,
                )
                if (
                    _fd_identity(fresh_state_fd) != state_identity
                    or _fd_identity(fresh_integration_fd) != integration_identity
                    or fresh_active_identity != active_identity
                    or fresh_payload != active_payload
                    or _release_tree_evidence_at(
                        integration_fd=fresh_integration_fd,
                        release_id=release_id,
                    )
                    != release_evidence
                ):
                    raise IntegrationEvidenceInvalid()
            finally:
                if fresh_integration_fd >= 0:
                    os.close(fresh_integration_fd)
                if fresh_app_fd >= 0:
                    os.close(fresh_app_fd)
                os.close(fresh_state_fd)
        except IntegrationEvidenceInvalid:
            raise
        except Exception as exc:
            raise IntegrationEvidenceInvalid() from exc

        return VerifiedActiveManifest(
            active_release=active_release,
            release_id=release_id,
            source_manifest_sha256=source_manifest_sha256,
            active_manifest_bytes=active_payload,
            active_manifest_sha256=_sha256_bytes(active_payload),
            state_home_identity=state_identity,
            integration_parent_identity=integration_identity,
            active_file_identity=active_identity,
        )
    finally:
        if integration_fd >= 0:
            os.close(integration_fd)
        if app_fd >= 0:
            os.close(app_fd)
        if state_fd >= 0:
            os.close(state_fd)
