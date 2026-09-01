from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import pwd
import re
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from . import private_io
from .integration_attestation import VerifiedActiveManifest, verify_active_manifest_at
from .integration_pool_authority import (
    POOL_AUTHORITY_FILENAME,
    POOL_AUTHORITY_MAX_BYTES,
    POOL_AUTHORITY_PENDING_FILENAME,
    POOL_AUTHORITY_SOURCE_FILENAME,
    POOL_AUTHORITY_SOURCE_MAX_BYTES,
    PoolAuthorityInvalid,
    build_pool_authority_projection,
    parse_pool_authority_projection,
    parse_pool_authority_source,
    serialize_pool_authority_projection,
)
from .integration_snapshot import (
    IntegrationInvalidSource,
    _canonical_document_v2,
    serialize_schema2_document,
)
from .json_utils import loads_strict
from .private_io import (
    FileIdentity,
    IntegrationEvidenceError,
    IntegrationEvidenceInvalid,
    IntegrationEvidenceUnavailable,
    private_path_lock,
)

_LOCK_MAX_BYTES = 4096
_EVIDENCE_LOCK_STATE = threading.local()
_BINDING_MAX_BYTES = 32 * 1024
_POINTER_MAX_BYTES = 4096
_PAYLOAD_MAX_BYTES = 2 * 1024 * 1024
_BINDING_FIELDS = frozenset(
    (
        "binding_schema_version",
        "pool_authority_filename",
        "pool_authority_sha256",
        "pool_authority_size_bytes",
        "usage_binding",
    )
)
_USAGE_BINDING_FIELDS = frozenset(
    (
        "active_manifest_sha256",
        "generation_id",
        "payload_filename",
        "payload_sha256",
        "payload_size_bytes",
        "published_at",
        "producer_version",
        "release_id",
        "source_manifest_sha256",
        "usage_binding_schema_version",
    )
)
_POINTER_FIELDS = frozenset(
    (
        "current_binding_sha256",
        "current_generation_id",
        "pointer_schema_version",
        "previous_binding_sha256",
        "previous_generation_id",
    )
)
_ALLOWED_WINDOW_SECONDS = frozenset((18_000, 604_800, 2_592_000))
ALLOWED_WINDOW_SECONDS = _ALLOWED_WINDOW_SECONDS
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_GENERATION_ID_RE = re.compile(r"[0-9a-f]{32}")
_RELEASE_ID_RE = re.compile(r"0\.6\.537-[0-9a-f]{16}")
_STAGING_RE = re.compile(r"\.tmp-([0-9a-f]{32})")
_STAGING_FILE_RE = re.compile(
    r"\.tmp-(?:account-usage-v2(?:\.binding)?|pool-authority-v2)\.json-[0-9a-f]{32}"
)
_POINTER_STAGING_PREFIX = ".tmp-current.json-"
_POINTER_STAGING_RE = re.compile(r"\.tmp-current\.json-[0-9a-f]{32}")
_POINTER_STAGING_MAX_ENTRIES = 64
_INTEGRATION_RECOVERY_MAX_ENTRIES = 128


@dataclass(frozen=True)
class EvidenceBinding:
    active_manifest_sha256: str
    binding_schema_version: int
    generation_id: str
    payload_filename: str
    payload_sha256: str
    payload_size_bytes: int
    published_at: str
    producer_version: str
    release_id: str
    source_manifest_sha256: str
    usage_binding_schema_version: int
    pool_authority_filename: str
    pool_authority_sha256: str
    pool_authority_size_bytes: int


@dataclass(frozen=True)
class EvidencePointer:
    current_generation_id: str
    current_binding_sha256: str
    pointer_schema_version: int
    previous_generation_id: str | None
    previous_binding_sha256: str | None


@dataclass(frozen=True)
class EvidenceGenerationBundle:
    usage: dict[str, object]
    pool_authority: dict[str, object]
    binding: EvidenceBinding


@dataclass
class _HeldEvidenceLocks:
    state_home: Path
    state_identity: FileIdentity
    integration_identity: FileIdentity
    lock_root_identity: FileIdentity
    release_name: str
    release_identity: FileIdentity
    current_name: str
    current_identity: FileIdentity
    release_mode: str
    current_mode: str
    depth: int = 1


@dataclass(frozen=True)
class _ValidatedEvidenceGeneration:
    document: dict[str, object] | None
    pool_authority: dict[str, object]
    binding: EvidenceBinding
    generation_identity: FileIdentity
    binding_identity: FileIdentity
    pool_authority_identity: FileIdentity
    payload_identity: FileIdentity | None


@dataclass(frozen=True)
class _CompleteEvidenceGeneration:
    generation_id: str
    published_at: datetime
    generation_identity: FileIdentity
    binding_identity: FileIdentity
    pool_authority_identity: FileIdentity
    payload_identity: FileIdentity


@dataclass(frozen=True)
class _GenerationNamespace:
    complete_names: tuple[str, ...]
    staging_names: tuple[str, ...]


@dataclass(frozen=True)
class _PointerStagingArtifact:
    name: str
    snapshot: os.stat_result


class IntegrationBusy(IntegrationEvidenceError):
    pass


def _before_publish_active_reverify(
    _state_home: Path,
    _data_home: Path,
    _verified_active_manifest: VerifiedActiveManifest,
) -> None:
    return None


def _before_publish_staging() -> None:
    return None


def _before_publish_retention_reclaim() -> None:
    return None


def _before_publish_payload_recheck(
    _parent_fd: int,
    _name: str,
    _held_fd: int,
) -> None:
    return None


def _before_publish_binding_recheck(
    _parent_fd: int,
    _name: str,
    _held_fd: int,
) -> None:
    return None


def _before_publish_pool_authority_source_recheck(
    _parent_fd: int,
    _name: str,
    _held_fd: int,
) -> None:
    try:
        os.stat(
            POOL_AUTHORITY_PENDING_FILENAME,
            dir_fd=_parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    raise IntegrationEvidenceInvalid()


def _before_publish_pool_authority_recheck(
    _parent_fd: int,
    _name: str,
    _held_fd: int,
) -> None:
    return None


def _before_publish_generation_recheck(
    _generations_fd: int,
    _generation_id: str,
    _generation_fd: int,
) -> None:
    return None


def _before_publish_pointer_parent_recheck(
    _state_home: Path,
    _integration_fd: int,
) -> None:
    return None


def _before_reader_current_recheck(
    _parent_fd: int,
    _name: str,
    _held_fd: int,
) -> None:
    return None


def _before_reader_pointer_parent_recheck(
    _state_home: Path,
    _integration_fd: int,
) -> None:
    return None


def _before_reader_generation_recheck(
    _generations_fd: int,
    _generation_id: str,
    _generation_fd: int,
) -> None:
    return None


def _before_reader_payload_recheck(
    _parent_fd: int,
    _name: str,
    _held_fd: int,
) -> None:
    return None


def _before_reader_binding_recheck(
    _parent_fd: int,
    _name: str,
    _held_fd: int,
) -> None:
    return None


def _before_reader_pool_authority_recheck(
    _parent_fd: int,
    _name: str,
    _held_fd: int,
) -> None:
    return None


def _before_reader_final_revalidate(
    _generations_fd: int,
    _pointer: EvidencePointer,
) -> None:
    return None


def _verify_active_manifest_for_reader(
    *,
    state_home: Path,
    data_home: Path,
    expected_entrypoint_path: Path,
) -> VerifiedActiveManifest:
    return verify_active_manifest_at(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=expected_entrypoint_path,
    )


def _verify_active_manifest_for_publish(
    *,
    state_home: Path,
    data_home: Path,
    expected_entrypoint_path: Path,
) -> VerifiedActiveManifest:
    return verify_active_manifest_at(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=expected_entrypoint_path,
    )


def _invalid_contract() -> None:
    raise IntegrationEvidenceInvalid()


def _canonical_timestamp(value: object) -> str:
    if type(value) is not str or len(value) > 64 or "T" not in value:
        _invalid_contract()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        _invalid_contract()
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _invalid_contract()
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _require_exact_object(value: object, *, fields: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict:
        _invalid_contract()
    mapping = cast(dict[object, object], value)
    if len(mapping) != len(fields) or set(mapping) != fields:
        _invalid_contract()
    if any(type(key) is not str for key in mapping):
        _invalid_contract()
    return cast(dict[str, object], mapping)


def _require_digest(value: object) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _invalid_contract()
    return value


def _require_generation_id(value: object) -> str:
    if type(value) is not str or _GENERATION_ID_RE.fullmatch(value) is None:
        _invalid_contract()
    return value


def _canonical_usage_binding(binding: EvidenceBinding) -> dict[str, object]:
    if type(binding) is not EvidenceBinding:
        _invalid_contract()
    if (
        type(binding.usage_binding_schema_version) is not int
        or binding.usage_binding_schema_version != 2
    ):
        _invalid_contract()
    if binding.payload_filename != "account-usage-v2.json":
        _invalid_contract()
    if (
        type(binding.payload_size_bytes) is not int
        or not 1 <= binding.payload_size_bytes <= _PAYLOAD_MAX_BYTES
    ):
        _invalid_contract()
    if binding.producer_version != "0.6.537":
        _invalid_contract()
    if type(binding.release_id) is not str or _RELEASE_ID_RE.fullmatch(binding.release_id) is None:
        _invalid_contract()
    return {
        "active_manifest_sha256": _require_digest(binding.active_manifest_sha256),
        "generation_id": _require_generation_id(binding.generation_id),
        "payload_filename": "account-usage-v2.json",
        "payload_sha256": _require_digest(binding.payload_sha256),
        "payload_size_bytes": binding.payload_size_bytes,
        "published_at": _canonical_timestamp(binding.published_at),
        "producer_version": "0.6.537",
        "release_id": binding.release_id,
        "source_manifest_sha256": _require_digest(binding.source_manifest_sha256),
        "usage_binding_schema_version": 2,
    }


def serialize_usage_binding(binding: EvidenceBinding) -> bytes:
    payload = _serialize_contract(_canonical_usage_binding(binding))
    if not 1 <= len(payload) <= _BINDING_MAX_BYTES:
        _invalid_contract()
    return payload


def _canonical_binding(binding: EvidenceBinding) -> dict[str, object]:
    if type(binding) is not EvidenceBinding:
        _invalid_contract()
    if type(binding.binding_schema_version) is not int or binding.binding_schema_version != 2:
        _invalid_contract()
    if binding.pool_authority_filename != POOL_AUTHORITY_FILENAME:
        _invalid_contract()
    if (
        type(binding.pool_authority_size_bytes) is not int
        or not 1 <= binding.pool_authority_size_bytes <= POOL_AUTHORITY_MAX_BYTES
    ):
        _invalid_contract()
    return {
        "binding_schema_version": 2,
        "pool_authority_filename": POOL_AUTHORITY_FILENAME,
        "pool_authority_sha256": _require_digest(binding.pool_authority_sha256),
        "pool_authority_size_bytes": binding.pool_authority_size_bytes,
        "usage_binding": _canonical_usage_binding(binding),
    }


def _canonical_pointer(pointer: EvidencePointer) -> dict[str, object]:
    if type(pointer) is not EvidencePointer:
        _invalid_contract()
    if type(pointer.pointer_schema_version) is not int or pointer.pointer_schema_version != 1:
        _invalid_contract()
    current_generation_id = _require_generation_id(pointer.current_generation_id)
    previous_digest = pointer.previous_binding_sha256
    previous_generation_id = pointer.previous_generation_id
    if (previous_digest is None) != (previous_generation_id is None):
        _invalid_contract()
    if previous_digest is not None:
        previous_digest = _require_digest(previous_digest)
        previous_generation_id = _require_generation_id(previous_generation_id)
        if previous_generation_id == current_generation_id:
            _invalid_contract()
    return {
        "current_binding_sha256": _require_digest(pointer.current_binding_sha256),
        "current_generation_id": current_generation_id,
        "pointer_schema_version": 1,
        "previous_binding_sha256": previous_digest,
        "previous_generation_id": previous_generation_id,
    }


def _serialize_contract(value: dict[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        _invalid_contract()


def serialize_binding(binding: EvidenceBinding) -> bytes:
    payload = _serialize_contract(_canonical_binding(binding))
    if not 1 <= len(payload) <= _BINDING_MAX_BYTES:
        _invalid_contract()
    return payload


def parse_binding(payload: bytes) -> EvidenceBinding:
    if type(payload) is not bytes or not 1 <= len(payload) <= _BINDING_MAX_BYTES:
        _invalid_contract()
    try:
        value = _require_exact_object(loads_strict(payload), fields=_BINDING_FIELDS)
        usage = _require_exact_object(value["usage_binding"], fields=_USAGE_BINDING_FIELDS)
        binding = EvidenceBinding(
            active_manifest_sha256=_require_digest(usage["active_manifest_sha256"]),
            binding_schema_version=value["binding_schema_version"],
            generation_id=_require_generation_id(usage["generation_id"]),
            payload_filename=usage["payload_filename"],
            payload_sha256=_require_digest(usage["payload_sha256"]),
            payload_size_bytes=usage["payload_size_bytes"],
            published_at=usage["published_at"],
            producer_version=usage["producer_version"],
            release_id=usage["release_id"],
            source_manifest_sha256=_require_digest(usage["source_manifest_sha256"]),
            usage_binding_schema_version=usage["usage_binding_schema_version"],
            pool_authority_filename=value["pool_authority_filename"],
            pool_authority_sha256=_require_digest(value["pool_authority_sha256"]),
            pool_authority_size_bytes=value["pool_authority_size_bytes"],
        )
        canonical = serialize_binding(binding)
    except (TypeError, ValueError, UnicodeDecodeError, RecursionError):
        _invalid_contract()
    if canonical != payload:
        _invalid_contract()
    return binding


def serialize_pointer(pointer: EvidencePointer) -> bytes:
    payload = _serialize_contract(_canonical_pointer(pointer))
    if not 1 <= len(payload) <= _POINTER_MAX_BYTES:
        _invalid_contract()
    return payload


def parse_pointer(payload: bytes) -> EvidencePointer:
    if type(payload) is not bytes or not 1 <= len(payload) <= _POINTER_MAX_BYTES:
        _invalid_contract()
    try:
        value = _require_exact_object(loads_strict(payload), fields=_POINTER_FIELDS)
        pointer = EvidencePointer(
            current_binding_sha256=_require_digest(value["current_binding_sha256"]),
            current_generation_id=_require_generation_id(value["current_generation_id"]),
            pointer_schema_version=value["pointer_schema_version"],
            previous_binding_sha256=value["previous_binding_sha256"],
            previous_generation_id=value["previous_generation_id"],
        )
        canonical = serialize_pointer(pointer)
    except (TypeError, ValueError, UnicodeDecodeError, RecursionError):
        _invalid_contract()
    if canonical != payload:
        _invalid_contract()
    return pointer


def validate_v2_payload_bytes(payload: bytes) -> dict[str, object]:
    if type(payload) is not bytes or not 1 <= len(payload) <= _PAYLOAD_MAX_BYTES:
        raise IntegrationInvalidSource()
    try:
        document = _canonical_document_v2(loads_strict(payload))
        canonical = serialize_schema2_document(document)
    except IntegrationInvalidSource:
        raise
    except (TypeError, ValueError, UnicodeDecodeError, RecursionError):
        raise IntegrationInvalidSource() from None
    if canonical != payload:
        raise IntegrationInvalidSource()
    for account in cast(list[dict[str, object]], document["accounts"]):
        for limit in cast(list[dict[str, object]], account["limits"]):
            if limit["window_seconds"] not in _ALLOWED_WINDOW_SECONDS:
                raise IntegrationInvalidSource()
        for evidence in cast(list[dict[str, object]], account["tracker_evidence"]):
            if evidence["limit_window_seconds"] not in _ALLOWED_WINDOW_SECONDS:
                raise IntegrationInvalidSource()
    return document


def _evidence_lock_name(target: Path) -> str:
    if type(target) is not type(Path()) or not target.is_absolute():
        raise IntegrationEvidenceInvalid()
    return private_io._private_lock_name(target)


def _deadline(timeout_seconds: float) -> float:
    if type(timeout_seconds) not in (int, float):
        raise IntegrationEvidenceInvalid()
    try:
        seconds = float(timeout_seconds)
    except (OverflowError, TypeError, ValueError):
        raise IntegrationEvidenceInvalid() from None
    if not math.isfinite(seconds) or seconds < 0:
        raise IntegrationEvidenceInvalid()
    deadline = time.monotonic() + seconds
    if not math.isfinite(deadline):
        raise IntegrationEvidenceInvalid()
    return deadline


def _validate_directory(item: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(item.st_mode)
        or item.st_uid != os.geteuid()
        or stat.S_IMODE(item.st_mode) != 0o700
    ):
        raise IntegrationEvidenceInvalid()


def _close_fds(*fds: int) -> None:
    for fd in fds:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _open_lock_root(*, create: bool) -> int:
    lock_root = private_io._private_lock_root()
    if create:
        try:
            private_io.ensure_private_directory(
                lock_root,
                label="integration evidence lock root",
            )
        except (OSError, ValueError) as exc:
            raise IntegrationEvidenceInvalid() from exc
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        passwd_home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise IntegrationEvidenceUnavailable() from exc
    enforce_from = len(lock_root.parts) - 1
    if lock_root.parts[: len(passwd_home.parts)] == passwd_home.parts:
        enforce_from = len(passwd_home.parts) - 1
    fd = -1
    try:
        fd = os.open(lock_root.anchor, flags)
        for index, component in enumerate(lock_root.parts[1:], start=1):
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if index >= enforce_from:
                _validate_directory(os.fstat(fd))
        result = fd
        fd = -1
        return result
    except FileNotFoundError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    except IntegrationEvidenceError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise IntegrationEvidenceInvalid() from exc
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        _close_fds(fd)


def _open_lock_file(lock_root_fd: int, name: str, *, create: bool) -> int:
    flags = (
        os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    created = False
    try:
        try:
            fd = os.open(name, flags, dir_fd=lock_root_fd)
        except FileNotFoundError:
            if not create:
                raise IntegrationEvidenceUnavailable() from None
            fd = os.open(
                name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=lock_root_fd,
            )
            created = True
        if created:
            os.fchmod(fd, 0o600)
            os.fsync(lock_root_fd)
        item = os.fstat(fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_uid != os.geteuid()
            or item.st_nlink != 1
            or stat.S_IMODE(item.st_mode) != 0o600
            or item.st_size > _LOCK_MAX_BYTES
        ):
            raise IntegrationEvidenceInvalid()
        return fd
    except IntegrationEvidenceError:
        if "fd" in locals():
            _close_fds(fd)
        raise
    except OSError as exc:
        if "fd" in locals():
            _close_fds(fd)
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise IntegrationEvidenceInvalid() from exc
        raise IntegrationEvidenceUnavailable() from exc


def _acquire_lock(fd: int, *, mode: str, deadline: float) -> None:
    operation = {"shared": fcntl.LOCK_SH, "exclusive": fcntl.LOCK_EX}[mode]
    before = os.fstat(fd)
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            break
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                raise IntegrationBusy() from exc
            time.sleep(0.05)
        except OSError as exc:
            raise IntegrationEvidenceUnavailable() from exc
    after = os.fstat(fd)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_mode != after.st_mode
        or before.st_uid != after.st_uid
        or before.st_nlink != after.st_nlink
        or after.st_size > _LOCK_MAX_BYTES
    ):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        raise IntegrationEvidenceInvalid()


def _verify_held_lock_entry(lock_root_fd: int, name: str, held_fd: int) -> None:
    named_fd = -1
    try:
        try:
            named_fd = _open_lock_file(lock_root_fd, name, create=False)
        except IntegrationEvidenceUnavailable as exc:
            raise IntegrationEvidenceInvalid() from exc
        held = os.fstat(held_fd)
        named = os.fstat(named_fd)
        if (
            held.st_dev != named.st_dev
            or held.st_ino != named.st_ino
            or held.st_mode != named.st_mode
            or held.st_uid != named.st_uid
            or held.st_nlink != named.st_nlink
            or held.st_size != named.st_size
        ):
            raise IntegrationEvidenceInvalid()
    finally:
        _close_fds(named_fd)


def _verify_held_lock_namespace(
    *,
    held_root_fd: int,
    lock_root_identity: FileIdentity,
    release_name: str,
    release_fd: int,
    current_name: str,
    current_fd: int,
) -> None:
    fresh_root_fd = -1
    try:
        try:
            fresh_root_fd = _open_lock_root(create=False)
        except IntegrationEvidenceUnavailable as exc:
            raise IntegrationEvidenceInvalid() from exc
        if (
            _fd_identity(held_root_fd) != lock_root_identity
            or _fd_identity(fresh_root_fd) != lock_root_identity
        ):
            raise IntegrationEvidenceInvalid()
        _verify_held_lock_entry(fresh_root_fd, release_name, release_fd)
        _verify_held_lock_entry(fresh_root_fd, current_name, current_fd)
    finally:
        _close_fds(fresh_root_fd)


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    _close_fds(fd)


def _fd_identity(fd: int) -> FileIdentity:
    item = os.fstat(fd)
    return FileIdentity(item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode))


def _verify_lock_target_parent(state_home: Path) -> tuple[FileIdentity, FileIdentity]:
    state_fd = -1
    app_fd = -1
    integration_fd = -1
    try:
        state_fd = private_io.open_verified_state_home(state_home)
        state_identity = _fd_identity(state_fd)
        app_fd = private_io.open_private_dir_at(state_fd, "codex-usage")
        integration_fd = private_io.open_private_dir_at(app_fd, "integration")
        integration_identity = _fd_identity(integration_fd)
    except FileNotFoundError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    finally:
        _close_fds(integration_fd, app_fd, state_fd)
    return state_identity, integration_identity


def bootstrap_evidence_lock_inodes(*, state_home: Path) -> None:
    if type(state_home) is not type(Path()) or not state_home.is_absolute():
        raise IntegrationEvidenceInvalid()
    state_identity, integration_identity = _verify_lock_target_parent(state_home)
    integration = state_home / "codex-usage" / "integration"
    release_name = _evidence_lock_name(integration / "producer-install")
    current_name = _evidence_lock_name(integration / "current.json")
    root_fd = -1
    release_fd = -1
    current_fd = -1
    try:
        root_fd = _open_lock_root(create=True)
        lock_root_identity = _fd_identity(root_fd)
        release_fd = _open_lock_file(root_fd, release_name, create=True)
        current_fd = _open_lock_file(root_fd, current_name, create=True)
        os.fsync(root_fd)
        _verify_held_lock_namespace(
            held_root_fd=root_fd,
            lock_root_identity=lock_root_identity,
            release_name=release_name,
            release_fd=release_fd,
            current_name=current_name,
            current_fd=current_fd,
        )
        if _verify_lock_target_parent(state_home) != (
            state_identity,
            integration_identity,
        ):
            raise IntegrationEvidenceInvalid()
    except IntegrationEvidenceError:
        raise
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        _close_fds(current_fd, release_fd, root_fd)


def _matches_held_lock_set(
    held_set: _HeldEvidenceLocks,
    *,
    release_name: str,
    current_name: str,
) -> bool:
    root_fd = _open_lock_root(create=False)
    release_fd = -1
    current_fd = -1
    try:
        if _fd_identity(root_fd) != held_set.lock_root_identity:
            return False
        release_fd = _open_lock_file(root_fd, release_name, create=False)
        if _fd_identity(release_fd) != held_set.release_identity:
            return False
        current_fd = _open_lock_file(root_fd, current_name, create=False)
        return _fd_identity(current_fd) == held_set.current_identity
    finally:
        _close_fds(current_fd, release_fd, root_fd)


@contextmanager
def evidence_lock_set(
    *,
    state_home: Path,
    release_mode: Literal["shared", "exclusive"],
    current_mode: Literal["shared", "exclusive"],
    timeout_seconds: float,
    create: bool,
) -> Iterator[None]:
    if type(state_home) is not type(Path()) or not state_home.is_absolute():
        raise IntegrationEvidenceInvalid()
    if release_mode not in {"shared", "exclusive"} or current_mode not in {
        "shared",
        "exclusive",
    }:
        raise IntegrationEvidenceInvalid()
    if type(create) is not bool:
        raise IntegrationEvidenceInvalid()
    state_identity, integration_identity = _verify_lock_target_parent(state_home)
    deadline = _deadline(timeout_seconds)
    order_probe = getattr(_EVIDENCE_LOCK_STATE, "held", None)
    if order_probe is not None and "current" in order_probe and "release" not in order_probe:
        raise IntegrationBusy()

    integration = state_home / "codex-usage" / "integration"
    release_name = _evidence_lock_name(integration / "producer-install")
    current_name = _evidence_lock_name(integration / "current.json")
    held_sets = getattr(_EVIDENCE_LOCK_STATE, "sets", None)
    if held_sets is None:
        held_sets = []
        _EVIDENCE_LOCK_STATE.sets = held_sets
    for held_set in held_sets:
        if (
            held_set.state_home != state_home
            or held_set.release_name != release_name
            or held_set.current_name != current_name
        ):
            continue
        if (
            held_set.state_identity != state_identity
            or held_set.integration_identity != integration_identity
        ):
            raise IntegrationEvidenceInvalid()
        try:
            matches_held_set = _matches_held_lock_set(
                held_set,
                release_name=release_name,
                current_name=current_name,
            )
        except IntegrationEvidenceError as exc:
            raise IntegrationEvidenceInvalid() from exc
        if not matches_held_set:
            raise IntegrationEvidenceInvalid()
        if held_set.release_mode != release_mode or held_set.current_mode != current_mode:
            raise IntegrationBusy()
        held_set.depth += 1
        try:
            yield
        finally:
            held_set.depth -= 1
        return

    targets = (
        ("release", release_name, release_mode),
        ("current", current_name, current_mode),
    )
    root_fd = _open_lock_root(create=create)
    lock_root_identity = _fd_identity(root_fd)
    acquired: list[tuple[str, int]] = []
    acquired_identities: dict[str, FileIdentity] = {}
    held_set = None
    try:
        for logical_name, lock_name, mode in targets:
            fd = _open_lock_file(
                root_fd,
                lock_name,
                create=create,
            )
            try:
                _acquire_lock(fd, mode=mode, deadline=deadline)
                _verify_held_lock_entry(root_fd, lock_name, fd)
            except Exception:
                _close_fds(fd)
                raise
            acquired.append((logical_name, fd))
            acquired_identities[logical_name] = _fd_identity(fd)
        _verify_held_lock_namespace(
            held_root_fd=root_fd,
            lock_root_identity=lock_root_identity,
            release_name=release_name,
            release_fd=acquired[0][1],
            current_name=current_name,
            current_fd=acquired[1][1],
        )
        held_set = _HeldEvidenceLocks(
            state_home=state_home,
            state_identity=state_identity,
            integration_identity=integration_identity,
            lock_root_identity=lock_root_identity,
            release_name=release_name,
            release_identity=acquired_identities["release"],
            current_name=current_name,
            current_identity=acquired_identities["current"],
            release_mode=release_mode,
            current_mode=current_mode,
        )
        held_sets.append(held_set)
        try:
            yield
        finally:
            held_sets.remove(held_set)
    finally:
        if held_set is not None and held_set in held_sets:
            held_sets.remove(held_set)
        for _, fd in reversed(acquired):
            _release_lock(fd)
        _close_fds(root_fd)


def _require_verified_manifest(value: object) -> VerifiedActiveManifest:
    if type(value) is not VerifiedActiveManifest:
        raise IntegrationEvidenceInvalid()
    verified = cast(VerifiedActiveManifest, value)
    if (
        verified.active_release.version != "0.6.537"
        or _RELEASE_ID_RE.fullmatch(verified.release_id) is None
        or verified.active_manifest_sha256
        != hashlib.sha256(verified.active_manifest_bytes).hexdigest()
        or _DIGEST_RE.fullmatch(verified.source_manifest_sha256) is None
    ):
        raise IntegrationEvidenceInvalid()
    return verified


def _require_same_verified_manifest(
    first: VerifiedActiveManifest,
    second: VerifiedActiveManifest,
) -> None:
    _require_verified_manifest(first)
    _require_verified_manifest(second)
    if first != second:
        raise IntegrationEvidenceUnavailable()


def _open_evidence_parents(state_home: Path) -> tuple[int, int, int, int]:
    state_fd = app_fd = integration_fd = generations_fd = -1
    try:
        state_fd = private_io.open_verified_state_home(state_home)
        app_fd = private_io.open_private_dir_at(state_fd, "codex-usage")
        integration_fd = private_io.open_private_dir_at(app_fd, "integration")
        generations_fd = private_io.open_private_dir_at(integration_fd, "generations")
        result = (state_fd, app_fd, integration_fd, generations_fd)
        state_fd = app_fd = integration_fd = generations_fd = -1
        return result
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        _close_fds(generations_fd, integration_fd, app_fd, state_fd)


def _named_identity(parent_fd: int, name: str, *, directory: bool) -> FileIdentity:
    fd = -1
    try:
        if directory:
            fd = private_io.open_private_dir_at(parent_fd, name)
        else:
            fd = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent_fd,
            )
        return _fd_identity(fd)
    finally:
        _close_fds(fd)


def _verify_named_file(
    parent_fd: int,
    name: str,
    expected: bytes,
    *,
    maximum: int,
    hook,
) -> FileIdentity:
    fd = -1
    try:
        fd = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        initial = os.fstat(fd)
        identity = private_io._require_private_file_stat(
            initial,
            maximum=maximum,
            mode=0o600,
        )
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(fd, min(65_536, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        hook(parent_fd, name, fd)
        final = os.fstat(fd)
        if (
            bytes(payload) != expected
            or private_io._require_private_file_stat(
                final,
                maximum=maximum,
                mode=0o600,
            )
            != identity
            or final.st_size != initial.st_size
            or final.st_mtime_ns != initial.st_mtime_ns
            or final.st_ctime_ns != initial.st_ctime_ns
            or _named_identity(parent_fd, name, directory=False) != identity
        ):
            raise IntegrationEvidenceInvalid()
        return identity
    except IntegrationEvidenceError:
        raise
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        _close_fds(fd)


def _read_verified_evidence_file(
    parent_fd: int,
    name: str,
    *,
    maximum: int,
    hook,
) -> tuple[bytes, FileIdentity]:
    """Read one private evidence file without rebinding its parent name."""
    fd = -1
    try:
        fd = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        initial = os.fstat(fd)
        identity = private_io._require_private_file_stat(
            initial,
            maximum=maximum,
            mode=0o600,
        )
        payload = bytearray()
        while len(payload) < initial.st_size:
            chunk = os.read(fd, min(65_536, initial.st_size - len(payload)))
            if not chunk:
                raise IntegrationEvidenceUnavailable()
            payload.extend(chunk)
        hook(parent_fd, name, fd)
        final = os.fstat(fd)
        if (
            private_io._require_private_file_stat(
                final,
                maximum=maximum,
                mode=0o600,
            )
            != identity
            or final.st_size != initial.st_size
            or final.st_mtime_ns != initial.st_mtime_ns
            or final.st_ctime_ns != initial.st_ctime_ns
            or _named_identity(parent_fd, name, directory=False) != identity
        ):
            raise IntegrationEvidenceInvalid()
        return bytes(payload), identity
    except IntegrationEvidenceError:
        raise
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise IntegrationEvidenceInvalid() from exc
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        _close_fds(fd)


def _validate_pointer_binding(
    *,
    generations_fd: int,
    generation_id: str,
    binding_sha256: str,
    verified: VerifiedActiveManifest | None,
    read_payload: bool,
    require_current_active: bool = True,
    hooks: bool = True,
) -> _ValidatedEvidenceGeneration:
    generation_fd = -1
    try:
        generation_fd = private_io.open_private_dir_at(generations_fd, generation_id)
        generation_identity = _fd_identity(generation_fd)
        if read_payload and hooks:
            _before_reader_generation_recheck(
                generations_fd,
                generation_id,
                generation_fd,
            )
        binding_bytes, binding_identity = _read_verified_evidence_file(
            generation_fd,
            "account-usage-v2.binding.json",
            maximum=_BINDING_MAX_BYTES,
            hook=(_before_reader_binding_recheck if read_payload and hooks else lambda *_: None),
        )
        binding = parse_binding(binding_bytes)
        if (
            binding.generation_id != generation_id
            or hashlib.sha256(binding_bytes).hexdigest() != binding_sha256
        ):
            raise IntegrationEvidenceInvalid()
        document: dict[str, object] | None = None
        payload_identity: FileIdentity | None = None
        if read_payload:
            if require_current_active and verified is None:
                raise IntegrationEvidenceInvalid()
            payload, payload_identity = _read_verified_evidence_file(
                generation_fd,
                binding.payload_filename,
                maximum=_PAYLOAD_MAX_BYTES,
                hook=_before_reader_payload_recheck if hooks else lambda *_: None,
            )
            document = validate_v2_payload_bytes(payload)
            if (
                len(payload) != binding.payload_size_bytes
                or hashlib.sha256(payload).hexdigest() != binding.payload_sha256
                or binding.published_at != document["generated_at"]
            ):
                raise IntegrationEvidenceInvalid()
        pool_authority_bytes, pool_authority_identity = _read_verified_evidence_file(
            generation_fd,
            binding.pool_authority_filename,
            maximum=POOL_AUTHORITY_MAX_BYTES,
            hook=(
                _before_reader_pool_authority_recheck if read_payload and hooks else lambda *_: None
            ),
        )
        pool_authority = parse_pool_authority_projection(pool_authority_bytes)
        if (
            len(pool_authority_bytes) != binding.pool_authority_size_bytes
            or hashlib.sha256(pool_authority_bytes).hexdigest() != binding.pool_authority_sha256
            or pool_authority["generation_id"] != binding.generation_id
            or pool_authority["release_id"] != binding.release_id
            or pool_authority["producer_version"] != binding.producer_version
            or pool_authority["issued_at"] != binding.published_at
            or pool_authority["usage_payload_sha256"] != binding.payload_sha256
            or pool_authority["usage_binding_sha256"]
            != hashlib.sha256(serialize_usage_binding(binding)).hexdigest()
        ):
            raise IntegrationEvidenceInvalid()
        if document is not None:
            usage_account_ids = {
                item["account_id"] for item in cast(list[dict[str, object]], document["accounts"])
            }
            authority_account_ids = {
                item["account_id"]
                for item in cast(list[dict[str, object]], pool_authority["authorities"])
            }
            if usage_account_ids != authority_account_ids:
                raise IntegrationEvidenceInvalid()
            if (
                require_current_active
                and verified is not None
                and (
                    binding.active_manifest_sha256 != verified.active_manifest_sha256
                    or binding.release_id != verified.release_id
                    or binding.source_manifest_sha256 != verified.source_manifest_sha256
                )
            ):
                raise IntegrationEvidenceInvalid()
        if (
            _fd_identity(generation_fd) != generation_identity
            or _named_identity(generations_fd, generation_id, directory=True) != generation_identity
        ):
            raise IntegrationEvidenceInvalid()
        return _ValidatedEvidenceGeneration(
            document=document,
            pool_authority=pool_authority,
            binding=binding,
            generation_identity=generation_identity,
            binding_identity=binding_identity,
            pool_authority_identity=pool_authority_identity,
            payload_identity=payload_identity,
        )
    except IntegrationEvidenceError:
        raise
    except (IntegrationInvalidSource, PoolAuthorityInvalid) as exc:
        raise IntegrationEvidenceInvalid() from exc
    except FileNotFoundError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        _close_fds(generation_fd)


def _reader_status(
    document: dict[str, object],
    *,
    now: datetime,
    pool_authority: dict[str, object] | None = None,
) -> str:
    stale = False
    partial = False
    for account in cast(list[dict[str, object]], document["accounts"]):
        freshness = cast(dict[str, object], account["freshness"])
        fresh_until = datetime.fromisoformat(
            cast(str, freshness["fresh_until"]).replace("Z", "+00:00")
        )
        if freshness["stale"] is True or now > fresh_until:
            stale = True
        if account["status"] != "ok":
            partial = True
        limits = cast(list[dict[str, object]], account["limits"])
        evidence = cast(list[dict[str, object]], account["tracker_evidence"])
        complete = {
            (item["pool"], item["limit_window_seconds"])
            for item in evidence
            if item["coverage"] == "complete"
        }
        if any((item["pool"], item["window_seconds"]) not in complete for item in limits):
            partial = True
    if stale:
        return "stale"
    if partial:
        return "partial"
    if pool_authority is not None:
        expires_at = datetime.fromisoformat(
            cast(str, pool_authority["expires_at"]).replace("Z", "+00:00")
        )
        if now >= expires_at:
            return "stale"
    return "complete"


def read_current_generation_bundle(
    *,
    state_home: Path,
    data_home: Path,
    expected_entrypoint_path: Path,
    now: datetime,
) -> tuple[EvidenceGenerationBundle | None, str]:
    """Return the one atomically bound UsageEvidenceV2/PoolAuthorityV2 bundle."""
    if (
        type(state_home) is not type(Path())
        or type(data_home) is not type(Path())
        or type(expected_entrypoint_path) is not type(Path())
        or type(now) is not datetime
        or not state_home.is_absolute()
        or not data_home.is_absolute()
        or not expected_entrypoint_path.is_absolute()
        or now.tzinfo is None
        or now.utcoffset() != timedelta(0)
    ):
        return None, "invalid"
    try:
        with evidence_lock_set(
            state_home=state_home,
            release_mode="shared",
            current_mode="shared",
            timeout_seconds=0,
            create=False,
        ):
            verified = _require_verified_manifest(
                _verify_active_manifest_for_reader(
                    state_home=state_home,
                    data_home=data_home,
                    expected_entrypoint_path=expected_entrypoint_path,
                )
            )
            state_fd = app_fd = integration_fd = generations_fd = -1
            try:
                state_fd, app_fd, integration_fd, generations_fd = _open_evidence_parents(
                    state_home
                )
                state_identity = _fd_identity(state_fd)
                integration_identity = _fd_identity(integration_fd)
                generations_identity = _fd_identity(generations_fd)
                if (
                    state_identity != verified.state_home_identity
                    or integration_identity != verified.integration_parent_identity
                    or _named_identity(integration_fd, "generations", directory=True)
                    != generations_identity
                ):
                    raise IntegrationEvidenceInvalid()
                current_bytes, current_identity = _read_verified_evidence_file(
                    integration_fd,
                    "current.json",
                    maximum=_POINTER_MAX_BYTES,
                    hook=_before_reader_current_recheck,
                )
                pointer = parse_pointer(current_bytes)
                current_generation = _validate_pointer_binding(
                    generations_fd=generations_fd,
                    generation_id=pointer.current_generation_id,
                    binding_sha256=pointer.current_binding_sha256,
                    verified=verified,
                    read_payload=True,
                )
                document = current_generation.document
                previous_generation: _ValidatedEvidenceGeneration | None = None
                if (
                    pointer.previous_generation_id is not None
                    and pointer.previous_binding_sha256 is not None
                ):
                    previous_generation = _validate_pointer_binding(
                        generations_fd=generations_fd,
                        generation_id=pointer.previous_generation_id,
                        binding_sha256=pointer.previous_binding_sha256,
                        verified=None,
                        read_payload=False,
                    )
                _before_reader_pointer_parent_recheck(state_home, integration_fd)
                fresh_state_identity, fresh_integration_identity = _fresh_parent_identities(
                    state_home
                )
                if (
                    _fd_identity(state_fd) != state_identity
                    or _fd_identity(integration_fd) != integration_identity
                    or _fd_identity(generations_fd) != generations_identity
                    or _named_identity(integration_fd, "generations", directory=True)
                    != generations_identity
                    or fresh_state_identity != state_identity
                    or fresh_integration_identity != integration_identity
                ):
                    raise IntegrationEvidenceInvalid()
                repeated = _require_verified_manifest(
                    _verify_active_manifest_for_reader(
                        state_home=state_home,
                        data_home=data_home,
                        expected_entrypoint_path=expected_entrypoint_path,
                    )
                )
                _require_same_verified_manifest(verified, repeated)
                repeated_current, repeated_identity = _read_verified_evidence_file(
                    integration_fd,
                    "current.json",
                    maximum=_POINTER_MAX_BYTES,
                    hook=lambda *_: None,
                )
                if (
                    repeated_current != current_bytes
                    or repeated_identity != current_identity
                    or parse_pointer(repeated_current) != pointer
                    or document is None
                ):
                    raise IntegrationEvidenceInvalid()
                _before_reader_final_revalidate(generations_fd, pointer)
                final_generation = _validate_pointer_binding(
                    generations_fd=generations_fd,
                    generation_id=pointer.current_generation_id,
                    binding_sha256=pointer.current_binding_sha256,
                    verified=verified,
                    read_payload=True,
                    hooks=False,
                )
                if (
                    pointer.previous_generation_id is not None
                    and pointer.previous_binding_sha256 is not None
                ):
                    final_previous_generation = _validate_pointer_binding(
                        generations_fd=generations_fd,
                        generation_id=pointer.previous_generation_id,
                        binding_sha256=pointer.previous_binding_sha256,
                        verified=None,
                        read_payload=False,
                        hooks=False,
                    )
                    if final_previous_generation != previous_generation:
                        raise IntegrationEvidenceInvalid()
                final_current, final_identity = _read_verified_evidence_file(
                    integration_fd,
                    "current.json",
                    maximum=_POINTER_MAX_BYTES,
                    hook=lambda *_: None,
                )
                if (
                    final_generation != current_generation
                    or final_current != current_bytes
                    or final_identity != current_identity
                    or parse_pointer(final_current) != pointer
                ):
                    raise IntegrationEvidenceInvalid()
                final_verified = _require_verified_manifest(
                    _verify_active_manifest_for_reader(
                        state_home=state_home,
                        data_home=data_home,
                        expected_entrypoint_path=expected_entrypoint_path,
                    )
                )
                _require_same_verified_manifest(verified, final_verified)
                final_state_identity, final_integration_identity = _fresh_parent_identities(
                    state_home
                )
                if (
                    _fd_identity(state_fd) != state_identity
                    or _fd_identity(integration_fd) != integration_identity
                    or _fd_identity(generations_fd) != generations_identity
                    or _named_identity(integration_fd, "generations", directory=True)
                    != generations_identity
                    or final_state_identity != state_identity
                    or final_integration_identity != integration_identity
                ):
                    raise IntegrationEvidenceInvalid()
                return (
                    EvidenceGenerationBundle(
                        usage=document,
                        pool_authority=current_generation.pool_authority,
                        binding=current_generation.binding,
                    ),
                    _reader_status(
                        document,
                        now=now,
                        pool_authority=current_generation.pool_authority,
                    ),
                )
            finally:
                _close_fds(generations_fd, integration_fd, app_fd, state_fd)
    except IntegrationBusy:
        return None, "busy"
    except (IntegrationEvidenceUnavailable, FileNotFoundError):
        return None, "unavailable"
    except (IntegrationEvidenceInvalid, IntegrationInvalidSource, ValueError, OSError):
        return None, "invalid"


def read_current_evidence(
    *,
    state_home: Path,
    data_home: Path,
    expected_entrypoint_path: Path,
    now: datetime,
) -> tuple[dict[str, object], str]:
    """Return UsageEvidenceV2 from the single validated V2 generation bundle."""
    bundle, status = read_current_generation_bundle(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=expected_entrypoint_path,
        now=now,
    )
    return (bundle.usage if bundle is not None else {}), status


def _safe_unlink_owned_file(parent_fd: int, name: str, identity: FileIdentity) -> None:
    try:
        if _named_identity(parent_fd, name, directory=False) == identity:
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except (OSError, ValueError):
        return


def _write_staged_file(
    staging_fd: int,
    final_name: str,
    payload: bytes,
    *,
    maximum: int,
    hook,
) -> None:
    temporary_name = f".tmp-{final_name}-{secrets.token_hex(16)}"
    identity = private_io.write_private_bytes_at(
        staging_fd,
        temporary_name,
        payload,
        mode=0o600,
    )
    renamed = False
    try:
        os.rename(
            temporary_name,
            final_name,
            src_dir_fd=staging_fd,
            dst_dir_fd=staging_fd,
        )
        renamed = True
        os.fsync(staging_fd)
        _verify_named_file(
            staging_fd,
            final_name,
            payload,
            maximum=maximum,
            hook=hook,
        )
    except IntegrationEvidenceError:
        raise
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        if not renamed:
            _safe_unlink_owned_file(staging_fd, temporary_name, identity)


def _validate_existing_current(
    integration_fd: int,
    generations_fd: int,
) -> EvidencePointer | None:
    try:
        payload, _identity = private_io.read_private_bytes_at(
            integration_fd,
            "current.json",
            maximum=_POINTER_MAX_BYTES,
            mode=0o600,
        )
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    pointer = parse_pointer(payload)
    generation_fd = -1
    try:
        generation_fd = private_io.open_private_dir_at(
            generations_fd,
            pointer.current_generation_id,
        )
        binding_bytes, _identity = private_io.read_private_bytes_at(
            generation_fd,
            "account-usage-v2.binding.json",
            maximum=_BINDING_MAX_BYTES,
            mode=0o600,
        )
        binding = parse_binding(binding_bytes)
        if (
            binding.generation_id != pointer.current_generation_id
            or hashlib.sha256(binding_bytes).hexdigest() != pointer.current_binding_sha256
        ):
            raise IntegrationEvidenceInvalid()
        return pointer
    except IntegrationEvidenceError:
        raise
    except FileNotFoundError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        _close_fds(generation_fd)


def _private_file_snapshot_at(
    parent_fd: int,
    name: str,
    *,
    maximum: int,
) -> os.stat_result:
    fd = -1
    try:
        fd = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        item = os.fstat(fd)
        private_io._require_private_file_stat(
            item,
            maximum=maximum,
            mode=0o600,
        )
        return item
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise IntegrationEvidenceInvalid() from exc
        raise
    finally:
        _close_fds(fd)


def _same_private_file_snapshot(
    expected: os.stat_result,
    actual: os.stat_result,
) -> bool:
    return (
        expected.st_dev,
        expected.st_ino,
        expected.st_mode,
        expected.st_uid,
        expected.st_gid,
        expected.st_nlink,
        expected.st_size,
        expected.st_mtime_ns,
        expected.st_ctime_ns,
    ) == (
        actual.st_dev,
        actual.st_ino,
        actual.st_mode,
        actual.st_uid,
        actual.st_gid,
        actual.st_nlink,
        actual.st_size,
        actual.st_mtime_ns,
        actual.st_ctime_ns,
    )


def _require_pointer_staging_snapshot(item: os.stat_result) -> None:
    private_io._require_private_file_stat(
        item,
        maximum=_POINTER_MAX_BYTES,
        mode=0o600,
    )


def _scan_integration_recovery_namespace(
    integration_fd: int,
) -> tuple[_PointerStagingArtifact, ...]:
    try:
        private_io._require_private_directory_fd(integration_fd)
        artifacts: list[_PointerStagingArtifact] = []
        with os.scandir(integration_fd) as entries:
            for entry_count, entry in enumerate(entries, start=1):
                if entry_count > _INTEGRATION_RECOVERY_MAX_ENTRIES:
                    raise IntegrationEvidenceInvalid()
                if not entry.name.startswith(_POINTER_STAGING_PREFIX):
                    continue
                if _POINTER_STAGING_RE.fullmatch(entry.name) is None:
                    raise IntegrationEvidenceInvalid()
                artifacts.append(
                    _PointerStagingArtifact(
                        name=entry.name,
                        snapshot=entry.stat(follow_symlinks=False),
                    )
                )
                if len(artifacts) > _POINTER_STAGING_MAX_ENTRIES:
                    raise IntegrationEvidenceInvalid()
        for artifact in artifacts:
            _require_pointer_staging_snapshot(artifact.snapshot)
        return tuple(artifacts)
    except IntegrationEvidenceError:
        raise
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc


def _remove_safe_pointer_staging_artifact(
    integration_fd: int,
    artifact: _PointerStagingArtifact,
) -> None:
    fd = -1
    try:
        if _POINTER_STAGING_RE.fullmatch(artifact.name) is None:
            raise IntegrationEvidenceInvalid()
        fd = os.open(
            artifact.name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=integration_fd,
        )
        opened = os.fstat(fd)
        _require_pointer_staging_snapshot(opened)
        if not _same_private_file_snapshot(artifact.snapshot, opened):
            raise IntegrationEvidenceInvalid()
        named = os.stat(
            artifact.name,
            dir_fd=integration_fd,
            follow_symlinks=False,
        )
        _require_pointer_staging_snapshot(named)
        if not _same_private_file_snapshot(
            artifact.snapshot, named
        ) or not _same_private_file_snapshot(opened, named):
            raise IntegrationEvidenceInvalid()
        os.unlink(artifact.name, dir_fd=integration_fd)
    except IntegrationEvidenceError:
        raise
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise IntegrationEvidenceInvalid() from exc
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        _close_fds(fd)


def _recover_pointer_staging_artifacts(
    *,
    integration_fd: int,
    artifacts: tuple[_PointerStagingArtifact, ...],
) -> None:
    for artifact in sorted(artifacts, key=lambda item: item.name):
        _remove_safe_pointer_staging_artifact(integration_fd, artifact)
    if artifacts:
        os.fsync(integration_fd)


def _remove_safe_staging_directory(generations_fd: int, name: str) -> None:
    staging_fd = -1
    try:
        staging_fd = private_io.open_private_dir_at(generations_fd, name)
        staging_identity = _fd_identity(staging_fd)
        entries = []
        with os.scandir(staging_fd) as iterator:
            for entry in iterator:
                entries.append(entry)
                if len(entries) > 6:
                    raise IntegrationEvidenceInvalid()
        snapshots: list[tuple[str, os.stat_result, int]] = []
        for entry in entries:
            if (
                entry.name
                not in {
                    "account-usage-v2.json",
                    "account-usage-v2.binding.json",
                    POOL_AUTHORITY_FILENAME,
                }
                and _STAGING_FILE_RE.fullmatch(entry.name) is None
            ):
                raise IntegrationEvidenceInvalid()
            item = entry.stat(follow_symlinks=False)
            if "binding" in entry.name:
                maximum = _BINDING_MAX_BYTES
            elif "pool-authority" in entry.name:
                maximum = POOL_AUTHORITY_MAX_BYTES
            else:
                maximum = _PAYLOAD_MAX_BYTES
            private_io._require_private_file_stat(
                item,
                maximum=maximum,
                mode=0o600,
            )
            snapshots.append((entry.name, item, maximum))
        for entry_name, snapshot, maximum in snapshots:
            current = _private_file_snapshot_at(
                staging_fd,
                entry_name,
                maximum=maximum,
            )
            if not _same_private_file_snapshot(snapshot, current):
                raise IntegrationEvidenceInvalid()
            os.unlink(entry_name, dir_fd=staging_fd)
        os.fsync(staging_fd)
        if (
            _fd_identity(staging_fd) != staging_identity
            or _named_identity(generations_fd, name, directory=True) != staging_identity
        ):
            raise IntegrationEvidenceInvalid()
        os.rmdir(name, dir_fd=generations_fd)
        os.fsync(generations_fd)
    except IntegrationEvidenceError:
        raise
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        _close_fds(staging_fd)


def recover_evidence_staging(*, state_home: Path) -> None:
    if type(state_home) is not type(Path()) or not state_home.is_absolute():
        raise IntegrationEvidenceInvalid()
    try:
        with evidence_lock_set(
            state_home=state_home,
            release_mode="exclusive",
            current_mode="exclusive",
            timeout_seconds=0,
            create=False,
        ):
            state_fd = app_fd = integration_fd = generations_fd = -1
            try:
                state_fd, app_fd, integration_fd, generations_fd = _open_evidence_parents(
                    state_home
                )
                state_identity = _fd_identity(state_fd)
                integration_identity = _fd_identity(integration_fd)
                generations_identity = _fd_identity(generations_fd)
                _recover_evidence_staging_from_fds(
                    integration_fd=integration_fd,
                    generations_fd=generations_fd,
                )
                fresh_state_identity, fresh_integration_identity = _fresh_parent_identities(
                    state_home
                )
                if (
                    _fd_identity(state_fd) != state_identity
                    or _fd_identity(integration_fd) != integration_identity
                    or _fd_identity(generations_fd) != generations_identity
                    or fresh_state_identity != state_identity
                    or fresh_integration_identity != integration_identity
                    or _named_identity(
                        integration_fd,
                        "generations",
                        directory=True,
                    )
                    != generations_identity
                ):
                    raise IntegrationEvidenceInvalid()
            finally:
                _close_fds(generations_fd, integration_fd, app_fd, state_fd)
    except IntegrationEvidenceError:
        raise
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc


def _scan_generation_namespace(generations_fd: int) -> _GenerationNamespace:
    private_io._require_private_directory_fd(generations_fd)
    staging_names: list[str] = []
    complete_names: list[str] = []
    with os.scandir(generations_fd) as entries:
        for entry in entries:
            if _GENERATION_ID_RE.fullmatch(entry.name) is not None:
                complete_names.append(entry.name)
                if len(complete_names) > 257:
                    raise IntegrationEvidenceInvalid()
                continue
            if _STAGING_RE.fullmatch(entry.name) is None:
                raise IntegrationEvidenceInvalid()
            staging_names.append(entry.name)
            if len(staging_names) > 16:
                raise IntegrationEvidenceInvalid()
    return _GenerationNamespace(
        complete_names=tuple(complete_names),
        staging_names=tuple(staging_names),
    )


def _recover_evidence_staging_from_namespace(
    *,
    generations_fd: int,
    namespace: _GenerationNamespace,
) -> None:
    for name in sorted(namespace.staging_names):
        _remove_safe_staging_directory(generations_fd, name)


def _recover_evidence_staging_from_fds(
    *,
    integration_fd: int,
    generations_fd: int,
) -> _GenerationNamespace:
    pointer_artifacts = _scan_integration_recovery_namespace(integration_fd)
    namespace = _scan_generation_namespace(generations_fd)
    _recover_evidence_staging_from_namespace(
        generations_fd=generations_fd,
        namespace=namespace,
    )
    _recover_pointer_staging_artifacts(
        integration_fd=integration_fd,
        artifacts=pointer_artifacts,
    )
    return namespace


def _fresh_parent_identities(state_home: Path) -> tuple[FileIdentity, FileIdentity]:
    try:
        return _verify_lock_target_parent(state_home)
    except IntegrationEvidenceUnavailable as exc:
        raise IntegrationEvidenceInvalid() from exc


def _atomic_replace_current(
    integration_fd: int,
    pointer_bytes: bytes,
    *,
    before_replace: Callable[[], None] | None = None,
) -> None:
    temporary_name = f".tmp-current.json-{secrets.token_hex(16)}"
    identity = private_io.write_private_bytes_at(
        integration_fd,
        temporary_name,
        pointer_bytes,
        mode=0o600,
    )
    renamed = False
    try:
        _verify_named_file(
            integration_fd,
            temporary_name,
            pointer_bytes,
            maximum=_POINTER_MAX_BYTES,
            hook=lambda *_: None,
        )
        if before_replace is not None:
            before_replace()
        os.replace(
            temporary_name,
            "current.json",
            src_dir_fd=integration_fd,
            dst_dir_fd=integration_fd,
        )
        # Current namespace commit point; later work is durability/maintenance only.
        renamed = True
        try:
            os.fsync(integration_fd)
        except Exception:
            pass
    except IntegrationEvidenceError:
        raise
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        if not renamed:
            _safe_unlink_owned_file(integration_fd, temporary_name, identity)


def _prune_complete_generations(
    *,
    integration_fd: int,
    generations_fd: int,
    generations: list[_CompleteEvidenceGeneration],
    pointer: EvidencePointer | None,
    maximum: int,
) -> None:
    if type(maximum) is not int or maximum < 0 or maximum > 256:
        raise IntegrationEvidenceInvalid()
    if len(generations) <= maximum:
        return
    protected = (
        {
            pointer.current_generation_id,
            pointer.previous_generation_id,
        }
        if pointer is not None
        else set()
    )
    candidates = sorted(
        (generation for generation in generations if generation.generation_id not in protected),
        key=lambda generation: (
            generation.published_at,
            generation.generation_id,
        ),
    )
    delete_count = len(generations) - maximum
    if len(candidates) < delete_count:
        raise IntegrationEvidenceInvalid()
    for generation in candidates[:delete_count]:
        _stage_and_remove_complete_generation(
            integration_fd=integration_fd,
            generations_fd=generations_fd,
            generation=generation,
            expected_pointer=pointer,
        )


def gc_evidence_generations(
    *,
    state_home: Path,
    data_home: Path,
    pointer: EvidencePointer,
    verified_active_manifest: VerifiedActiveManifest,
) -> None:
    verified = _require_verified_manifest(verified_active_manifest)
    if (
        type(state_home) is not type(Path())
        or type(data_home) is not type(Path())
        or not state_home.is_absolute()
        or not data_home.is_absolute()
    ):
        raise IntegrationEvidenceInvalid()
    expected_pointer = parse_pointer(serialize_pointer(pointer))

    with evidence_lock_set(
        state_home=state_home,
        release_mode="exclusive",
        current_mode="exclusive",
        timeout_seconds=0,
        create=False,
    ):
        state_fd = app_fd = integration_fd = generations_fd = -1
        try:
            state_fd, app_fd, integration_fd, generations_fd = _open_evidence_parents(state_home)
            state_identity = _fd_identity(state_fd)
            integration_identity = _fd_identity(integration_fd)
            generations_identity = _fd_identity(generations_fd)
            if (
                state_identity != verified.state_home_identity
                or integration_identity != verified.integration_parent_identity
                or _named_identity(integration_fd, "generations", directory=True)
                != generations_identity
            ):
                raise IntegrationEvidenceInvalid()

            namespace = _recover_evidence_staging_from_fds(
                integration_fd=integration_fd,
                generations_fd=generations_fd,
            )
            fresh = _verify_active_manifest_for_publish(
                state_home=state_home,
                data_home=data_home,
                expected_entrypoint_path=verified.active_release.entrypoint_path,
            )
            _require_same_verified_manifest(verified, fresh)

            current_bytes, _current_identity = _read_verified_evidence_file(
                integration_fd,
                "current.json",
                maximum=_POINTER_MAX_BYTES,
                hook=lambda *_: None,
            )
            current_pointer = parse_pointer(current_bytes)
            if current_pointer != expected_pointer:
                raise IntegrationEvidenceInvalid()
            _validate_pointer_binding(
                generations_fd=generations_fd,
                generation_id=current_pointer.current_generation_id,
                binding_sha256=current_pointer.current_binding_sha256,
                verified=None,
                read_payload=True,
                require_current_active=False,
                hooks=False,
            )
            if (
                current_pointer.previous_generation_id is not None
                and current_pointer.previous_binding_sha256 is not None
            ):
                _validate_pointer_binding(
                    generations_fd=generations_fd,
                    generation_id=current_pointer.previous_generation_id,
                    binding_sha256=current_pointer.previous_binding_sha256,
                    verified=None,
                    read_payload=True,
                    require_current_active=False,
                    hooks=False,
                )

            generations = _inspect_complete_generation_names(
                generations_fd=generations_fd,
                names=namespace.complete_names,
            )
            _prune_complete_generations(
                integration_fd=integration_fd,
                generations_fd=generations_fd,
                generations=generations,
                pointer=current_pointer,
                maximum=256,
            )
            if (
                _fd_identity(state_fd) != state_identity
                or _fd_identity(integration_fd) != integration_identity
                or _fd_identity(generations_fd) != generations_identity
                or _named_identity(integration_fd, "generations", directory=True)
                != generations_identity
            ):
                raise IntegrationEvidenceInvalid()
        except IntegrationEvidenceError:
            raise
        except IntegrationInvalidSource as exc:
            raise IntegrationEvidenceInvalid() from exc
        except ValueError as exc:
            raise IntegrationEvidenceInvalid() from exc
        except OSError as exc:
            raise IntegrationEvidenceUnavailable() from exc
        finally:
            _close_fds(generations_fd, integration_fd, app_fd, state_fd)


def _scan_complete_generations(
    *,
    generations_fd: int,
) -> list[_CompleteEvidenceGeneration]:
    namespace = _scan_generation_namespace(generations_fd)
    return _inspect_complete_generation_names(
        generations_fd=generations_fd,
        names=namespace.complete_names,
    )


def _inspect_complete_generation_names(
    *,
    generations_fd: int,
    names: tuple[str, ...],
) -> list[_CompleteEvidenceGeneration]:
    complete: list[_CompleteEvidenceGeneration] = []
    try:
        private_io._require_private_directory_fd(generations_fd)
        if len(names) > 257 or any(_GENERATION_ID_RE.fullmatch(name) is None for name in names):
            raise IntegrationEvidenceInvalid()
        for name in names:
            complete.append(
                _inspect_complete_generation(
                    generations_fd=generations_fd,
                    generation_id=name,
                )
            )
        return complete
    except IntegrationEvidenceError:
        raise
    except IntegrationInvalidSource as exc:
        raise IntegrationEvidenceInvalid() from exc
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc


def _inspect_complete_generation(
    *,
    generations_fd: int,
    generation_id: str,
) -> _CompleteEvidenceGeneration:
    generation_fd = -1
    try:
        generation_fd = private_io.open_private_dir_at(generations_fd, generation_id)
        generation_identity = _fd_identity(generation_fd)
        names: set[str] = set()
        with os.scandir(generation_fd) as entries:
            for entry in entries:
                names.add(entry.name)
                if len(names) > 3:
                    raise IntegrationEvidenceInvalid()
        if names != {
            "account-usage-v2.json",
            "account-usage-v2.binding.json",
            POOL_AUTHORITY_FILENAME,
        }:
            raise IntegrationEvidenceInvalid()
        binding_bytes, binding_identity = _read_verified_evidence_file(
            generation_fd,
            "account-usage-v2.binding.json",
            maximum=_BINDING_MAX_BYTES,
            hook=lambda *_: None,
        )
        binding = parse_binding(binding_bytes)
        pool_authority_bytes, pool_authority_identity = _read_verified_evidence_file(
            generation_fd,
            binding.pool_authority_filename,
            maximum=POOL_AUTHORITY_MAX_BYTES,
            hook=lambda *_: None,
        )
        pool_authority = parse_pool_authority_projection(pool_authority_bytes)
        payload, payload_identity = _read_verified_evidence_file(
            generation_fd,
            binding.payload_filename,
            maximum=_PAYLOAD_MAX_BYTES,
            hook=lambda *_: None,
        )
        document = validate_v2_payload_bytes(payload)
        usage_account_ids = {
            item["account_id"] for item in cast(list[dict[str, object]], document["accounts"])
        }
        authority_account_ids = {
            item["account_id"]
            for item in cast(list[dict[str, object]], pool_authority["authorities"])
        }
        if (
            binding.generation_id != generation_id
            or len(payload) != binding.payload_size_bytes
            or hashlib.sha256(payload).hexdigest() != binding.payload_sha256
            or binding.published_at != document["generated_at"]
            or len(pool_authority_bytes) != binding.pool_authority_size_bytes
            or hashlib.sha256(pool_authority_bytes).hexdigest() != binding.pool_authority_sha256
            or pool_authority["generation_id"] != binding.generation_id
            or pool_authority["release_id"] != binding.release_id
            or pool_authority["producer_version"] != binding.producer_version
            or pool_authority["issued_at"] != binding.published_at
            or pool_authority["usage_payload_sha256"] != binding.payload_sha256
            or pool_authority["usage_binding_sha256"]
            != hashlib.sha256(serialize_usage_binding(binding)).hexdigest()
            or usage_account_ids != authority_account_ids
            or _fd_identity(generation_fd) != generation_identity
            or _named_identity(generations_fd, generation_id, directory=True) != generation_identity
        ):
            raise IntegrationEvidenceInvalid()
        return _CompleteEvidenceGeneration(
            generation_id=generation_id,
            published_at=datetime.fromisoformat(
                binding.published_at.replace("Z", "+00:00")
            ).astimezone(UTC),
            generation_identity=generation_identity,
            binding_identity=binding_identity,
            pool_authority_identity=pool_authority_identity,
            payload_identity=payload_identity,
        )
    finally:
        _close_fds(generation_fd)


def _stage_and_remove_complete_generation(
    *,
    integration_fd: int,
    generations_fd: int,
    generation: _CompleteEvidenceGeneration,
    expected_pointer: EvidencePointer | None,
) -> None:
    temporary_name = f".tmp-{generation.generation_id}"
    if expected_pointer is None:
        try:
            os.stat("current.json", dir_fd=integration_fd, follow_symlinks=False)
        except FileNotFoundError:
            current_pointer = None
        else:
            raise IntegrationEvidenceInvalid()
    else:
        current_bytes, _current_identity = _read_verified_evidence_file(
            integration_fd,
            "current.json",
            maximum=_POINTER_MAX_BYTES,
            hook=lambda *_: None,
        )
        current_pointer = parse_pointer(current_bytes)
    if (
        current_pointer != expected_pointer
        or generation.generation_id
        in {
            expected_pointer.current_generation_id if expected_pointer else None,
            expected_pointer.previous_generation_id if expected_pointer else None,
        }
        or _inspect_complete_generation(
            generations_fd=generations_fd,
            generation_id=generation.generation_id,
        )
        != generation
    ):
        raise IntegrationEvidenceInvalid()
    try:
        os.stat(temporary_name, dir_fd=generations_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise IntegrationEvidenceInvalid()

    os.rename(
        generation.generation_id,
        temporary_name,
        src_dir_fd=generations_fd,
        dst_dir_fd=generations_fd,
    )
    os.fsync(generations_fd)
    _remove_safe_staging_directory(generations_fd, temporary_name)


def rollback_current_evidence(
    *,
    state_home: Path,
    data_home: Path,
    verified_active_manifest: VerifiedActiveManifest,
) -> EvidencePointer:
    verified = _require_verified_manifest(verified_active_manifest)
    if (
        type(state_home) is not type(Path())
        or type(data_home) is not type(Path())
        or not state_home.is_absolute()
        or not data_home.is_absolute()
    ):
        raise IntegrationEvidenceInvalid()

    with evidence_lock_set(
        state_home=state_home,
        release_mode="exclusive",
        current_mode="exclusive",
        timeout_seconds=0,
        create=False,
    ):
        state_fd = app_fd = integration_fd = generations_fd = -1
        try:
            state_fd, app_fd, integration_fd, generations_fd = _open_evidence_parents(state_home)
            state_identity = _fd_identity(state_fd)
            integration_identity = _fd_identity(integration_fd)
            generations_identity = _fd_identity(generations_fd)
            if (
                state_identity != verified.state_home_identity
                or integration_identity != verified.integration_parent_identity
                or _named_identity(integration_fd, "generations", directory=True)
                != generations_identity
            ):
                raise IntegrationEvidenceInvalid()

            _recover_evidence_staging_from_fds(
                integration_fd=integration_fd,
                generations_fd=generations_fd,
            )
            fresh = _verify_active_manifest_for_publish(
                state_home=state_home,
                data_home=data_home,
                expected_entrypoint_path=verified.active_release.entrypoint_path,
            )
            _require_same_verified_manifest(verified, fresh)
            current_bytes, current_identity = _read_verified_evidence_file(
                integration_fd,
                "current.json",
                maximum=_POINTER_MAX_BYTES,
                hook=lambda *_: None,
            )
            pointer = parse_pointer(current_bytes)
            if pointer.previous_generation_id is None or pointer.previous_binding_sha256 is None:
                raise IntegrationEvidenceUnavailable()
            _validate_pointer_binding(
                generations_fd=generations_fd,
                generation_id=pointer.current_generation_id,
                binding_sha256=pointer.current_binding_sha256,
                verified=verified,
                read_payload=True,
                hooks=False,
            )
            try:
                _validate_pointer_binding(
                    generations_fd=generations_fd,
                    generation_id=pointer.previous_generation_id,
                    binding_sha256=pointer.previous_binding_sha256,
                    verified=verified,
                    read_payload=True,
                    hooks=False,
                )
            except IntegrationEvidenceError as exc:
                raise IntegrationEvidenceUnavailable() from exc
            repeated_current, repeated_identity = _read_verified_evidence_file(
                integration_fd,
                "current.json",
                maximum=_POINTER_MAX_BYTES,
                hook=lambda *_: None,
            )
            repeated_verified = _verify_active_manifest_for_publish(
                state_home=state_home,
                data_home=data_home,
                expected_entrypoint_path=verified.active_release.entrypoint_path,
            )
            _require_same_verified_manifest(verified, repeated_verified)
            if (
                repeated_current != current_bytes
                or repeated_identity != current_identity
                or parse_pointer(repeated_current) != pointer
                or _fd_identity(state_fd) != state_identity
                or _fd_identity(integration_fd) != integration_identity
                or _fd_identity(generations_fd) != generations_identity
                or _named_identity(integration_fd, "generations", directory=True)
                != generations_identity
            ):
                raise IntegrationEvidenceInvalid()
            rolled_back = EvidencePointer(
                pointer.previous_generation_id,
                pointer.previous_binding_sha256,
                1,
                pointer.current_generation_id,
                pointer.current_binding_sha256,
            )
            _atomic_replace_current(
                integration_fd,
                serialize_pointer(rolled_back),
            )
            return rolled_back
        except IntegrationEvidenceError:
            raise
        except IntegrationInvalidSource as exc:
            raise IntegrationEvidenceInvalid() from exc
        except ValueError as exc:
            raise IntegrationEvidenceInvalid() from exc
        except OSError as exc:
            raise IntegrationEvidenceUnavailable() from exc
        finally:
            _close_fds(generations_fd, integration_fd, app_fd, state_fd)


def publish_evidence_generation(
    payload: bytes,
    *,
    state_home: Path,
    data_home: Path,
    verified_active_manifest: VerifiedActiveManifest,
) -> EvidencePointer:
    verified = _require_verified_manifest(verified_active_manifest)
    if (
        type(state_home) is not type(Path())
        or type(data_home) is not type(Path())
        or not state_home.is_absolute()
        or not data_home.is_absolute()
    ):
        raise IntegrationEvidenceInvalid()
    with evidence_lock_set(
        state_home=state_home,
        release_mode="exclusive",
        current_mode="exclusive",
        timeout_seconds=0,
        create=False,
    ):
        return _publish_evidence_generation_locked(
            payload,
            state_home=state_home,
            data_home=data_home,
            verified_active_manifest=verified,
        )


def _publish_evidence_generation_locked(
    payload: bytes,
    *,
    state_home: Path,
    data_home: Path,
    verified_active_manifest: VerifiedActiveManifest,
) -> EvidencePointer:
    verified = _require_verified_manifest(verified_active_manifest)
    if (
        type(state_home) is not type(Path())
        or type(data_home) is not type(Path())
        or not state_home.is_absolute()
        or not data_home.is_absolute()
    ):
        raise IntegrationEvidenceInvalid()
    document = validate_v2_payload_bytes(payload)
    published_at = cast(str, document["generated_at"])
    published_instant = datetime.fromisoformat(published_at.replace("Z", "+00:00")).astimezone(UTC)
    payload_digest = hashlib.sha256(payload).hexdigest()
    state_fd = app_fd = integration_fd = generations_fd = generation_fd = -1
    source_lock = None
    try:
        state_fd, app_fd, integration_fd, generations_fd = _open_evidence_parents(state_home)
        held_state_identity = _fd_identity(state_fd)
        held_integration_identity = _fd_identity(integration_fd)
        held_generations_identity = _fd_identity(generations_fd)
        if (
            held_state_identity != verified.state_home_identity
            or held_integration_identity != verified.integration_parent_identity
            or _named_identity(integration_fd, "generations", directory=True)
            != held_generations_identity
        ):
            raise IntegrationEvidenceInvalid()

        source_lock_path = (
            state_home / "codex-usage" / "integration" / POOL_AUTHORITY_SOURCE_FILENAME
        )
        source_lock = private_path_lock(source_lock_path, label="pool authority source lock")
        source_lock.__enter__()
        authority_source_bytes, _authority_source_identity = _read_verified_evidence_file(
            integration_fd,
            POOL_AUTHORITY_SOURCE_FILENAME,
            maximum=POOL_AUTHORITY_SOURCE_MAX_BYTES,
            hook=_before_publish_pool_authority_source_recheck,
        )
        authority_source = parse_pool_authority_source(authority_source_bytes)

        namespace = _recover_evidence_staging_from_fds(
            integration_fd=integration_fd,
            generations_fd=generations_fd,
        )
        old_pointer = _validate_existing_current(integration_fd, generations_fd)
        if old_pointer is not None:
            current_generation = _validate_pointer_binding(
                generations_fd=generations_fd,
                generation_id=old_pointer.current_generation_id,
                binding_sha256=old_pointer.current_binding_sha256,
                verified=None,
                read_payload=True,
                require_current_active=False,
                hooks=False,
            )
            if current_generation.document is None:
                raise IntegrationEvidenceInvalid()
            current_published_at = cast(
                str,
                current_generation.document["generated_at"],
            )
            current_published_instant = datetime.fromisoformat(
                current_published_at.replace("Z", "+00:00")
            ).astimezone(UTC)
            if published_instant < current_published_instant:
                raise IntegrationEvidenceInvalid()
            if (
                old_pointer.previous_generation_id is not None
                and old_pointer.previous_binding_sha256 is not None
            ):
                _validate_pointer_binding(
                    generations_fd=generations_fd,
                    generation_id=old_pointer.previous_generation_id,
                    binding_sha256=old_pointer.previous_binding_sha256,
                    verified=None,
                    read_payload=True,
                    require_current_active=False,
                    hooks=False,
                )

        _before_publish_active_reverify(state_home, data_home, verified)
        repeated = _verify_active_manifest_for_publish(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=verified.active_release.entrypoint_path,
        )
        _require_same_verified_manifest(verified, repeated)
        complete_generations = _inspect_complete_generation_names(
            generations_fd=generations_fd,
            names=namespace.complete_names,
        )
        _before_publish_retention_reclaim()
        _prune_complete_generations(
            integration_fd=integration_fd,
            generations_fd=generations_fd,
            generations=complete_generations,
            pointer=old_pointer,
            maximum=255,
        )

        for _attempt in range(16):
            generation_id = secrets.token_hex(16)
            staging_name = f".tmp-{generation_id}"
            try:
                os.stat(generation_id, dir_fd=generations_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                continue
            try:
                os.mkdir(staging_name, mode=0o700, dir_fd=generations_fd)
            except FileExistsError:
                continue
            break
        else:
            raise IntegrationEvidenceUnavailable()

        _before_publish_staging()
        generation_fd = private_io.open_private_dir_at(
            generations_fd,
            staging_name,
        )
        staged_identity = _fd_identity(generation_fd)
        binding = EvidenceBinding(
            active_manifest_sha256=verified.active_manifest_sha256,
            binding_schema_version=2,
            generation_id=generation_id,
            payload_filename="account-usage-v2.json",
            payload_sha256=payload_digest,
            payload_size_bytes=len(payload),
            published_at=published_at,
            producer_version="0.6.537",
            release_id=verified.release_id,
            source_manifest_sha256=verified.source_manifest_sha256,
            usage_binding_schema_version=2,
            pool_authority_filename=POOL_AUTHORITY_FILENAME,
            pool_authority_sha256="0" * 64,
            pool_authority_size_bytes=1,
        )
        usage_binding_digest = hashlib.sha256(serialize_usage_binding(binding)).hexdigest()
        pool_authority = build_pool_authority_projection(
            source=authority_source,
            usage_document=document,
            usage_binding_published_at=binding.published_at,
            generation_id=generation_id,
            release_id=verified.release_id,
            usage_payload_sha256=payload_digest,
            usage_binding_sha256=usage_binding_digest,
        )
        pool_authority_bytes = serialize_pool_authority_projection(pool_authority)
        binding = EvidenceBinding(
            active_manifest_sha256=binding.active_manifest_sha256,
            binding_schema_version=2,
            generation_id=binding.generation_id,
            payload_filename=binding.payload_filename,
            payload_sha256=binding.payload_sha256,
            payload_size_bytes=binding.payload_size_bytes,
            published_at=binding.published_at,
            producer_version=binding.producer_version,
            release_id=binding.release_id,
            source_manifest_sha256=binding.source_manifest_sha256,
            usage_binding_schema_version=2,
            pool_authority_filename=POOL_AUTHORITY_FILENAME,
            pool_authority_sha256=hashlib.sha256(pool_authority_bytes).hexdigest(),
            pool_authority_size_bytes=len(pool_authority_bytes),
        )
        binding_bytes = serialize_binding(binding)
        if parse_binding(binding_bytes) != binding:
            raise IntegrationEvidenceInvalid()
        _write_staged_file(
            generation_fd,
            "account-usage-v2.json",
            payload,
            maximum=_PAYLOAD_MAX_BYTES,
            hook=_before_publish_payload_recheck,
        )
        _write_staged_file(
            generation_fd,
            POOL_AUTHORITY_FILENAME,
            pool_authority_bytes,
            maximum=POOL_AUTHORITY_MAX_BYTES,
            hook=_before_publish_pool_authority_recheck,
        )
        _write_staged_file(
            generation_fd,
            "account-usage-v2.binding.json",
            binding_bytes,
            maximum=_BINDING_MAX_BYTES,
            hook=_before_publish_binding_recheck,
        )
        os.fsync(generation_fd)
        os.rename(
            staging_name,
            generation_id,
            src_dir_fd=generations_fd,
            dst_dir_fd=generations_fd,
        )
        os.fsync(generations_fd)
        _before_publish_generation_recheck(
            generations_fd,
            generation_id,
            generation_fd,
        )
        if (
            _fd_identity(generation_fd) != staged_identity
            or _named_identity(
                generations_fd,
                generation_id,
                directory=True,
            )
            != staged_identity
        ):
            raise IntegrationEvidenceInvalid()

        repeated = _verify_active_manifest_for_publish(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=verified.active_release.entrypoint_path,
        )
        _require_same_verified_manifest(verified, repeated)
        pointer = EvidencePointer(
            generation_id,
            hashlib.sha256(binding_bytes).hexdigest(),
            1,
            old_pointer.current_generation_id if old_pointer else None,
            old_pointer.current_binding_sha256 if old_pointer else None,
        )
        pointer_bytes = serialize_pointer(pointer)

        def rebind_publish_parents() -> None:
            _before_publish_pointer_parent_recheck(state_home, integration_fd)
            fresh_state_identity, fresh_integration_identity = _fresh_parent_identities(state_home)
            fresh_generations_fd = -1
            try:
                fresh_generations_fd = private_io.open_private_dir_at(
                    integration_fd,
                    "generations",
                )
                if (
                    _fd_identity(state_fd) != held_state_identity
                    or _fd_identity(integration_fd) != held_integration_identity
                    or _fd_identity(generations_fd) != held_generations_identity
                    or fresh_state_identity != held_state_identity
                    or fresh_integration_identity != held_integration_identity
                    or _fd_identity(fresh_generations_fd) != held_generations_identity
                    or _named_identity(
                        integration_fd,
                        "generations",
                        directory=True,
                    )
                    != held_generations_identity
                ):
                    raise IntegrationEvidenceInvalid()
            finally:
                _close_fds(fresh_generations_fd)

        _atomic_replace_current(
            integration_fd,
            pointer_bytes,
            before_replace=rebind_publish_parents,
        )
        return pointer
    except IntegrationEvidenceError:
        raise
    except (IntegrationInvalidSource, PoolAuthorityInvalid) as exc:
        raise IntegrationEvidenceInvalid() from exc
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        if source_lock is not None:
            source_lock.__exit__(None, None, None)
        _close_fds(
            generation_fd,
            generations_fd,
            integration_fd,
            app_fd,
            state_fd,
        )
