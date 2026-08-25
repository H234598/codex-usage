from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import pwd
import re
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from . import private_io
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
)

_LOCK_MAX_BYTES = 4096
_EVIDENCE_LOCK_STATE = threading.local()
_BINDING_MAX_BYTES = 32 * 1024
_POINTER_MAX_BYTES = 4096
_PAYLOAD_MAX_BYTES = 2 * 1024 * 1024
_BINDING_FIELDS = frozenset(
    (
        "active_manifest_sha256",
        "binding_schema_version",
        "generation_id",
        "payload_filename",
        "payload_sha256",
        "payload_size_bytes",
        "published_at",
        "producer_version",
        "release_id",
        "source_manifest_sha256",
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
_RELEASE_ID_RE = re.compile(r"0\.6\.536-[0-9a-f]{16}")


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


@dataclass(frozen=True)
class EvidencePointer:
    current_generation_id: str
    current_binding_sha256: str
    pointer_schema_version: int
    previous_generation_id: str | None
    previous_binding_sha256: str | None


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


class IntegrationBusy(IntegrationEvidenceError):
    pass


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


def _canonical_binding(binding: EvidenceBinding) -> dict[str, object]:
    if type(binding) is not EvidenceBinding:
        _invalid_contract()
    if type(binding.binding_schema_version) is not int or binding.binding_schema_version != 1:
        _invalid_contract()
    if binding.payload_filename != "account-usage-v2.json":
        _invalid_contract()
    if (
        type(binding.payload_size_bytes) is not int
        or not 1 <= binding.payload_size_bytes <= _PAYLOAD_MAX_BYTES
    ):
        _invalid_contract()
    if binding.producer_version != "0.6.536":
        _invalid_contract()
    if type(binding.release_id) is not str or _RELEASE_ID_RE.fullmatch(binding.release_id) is None:
        _invalid_contract()
    return {
        "active_manifest_sha256": _require_digest(binding.active_manifest_sha256),
        "binding_schema_version": 1,
        "generation_id": _require_generation_id(binding.generation_id),
        "payload_filename": "account-usage-v2.json",
        "payload_sha256": _require_digest(binding.payload_sha256),
        "payload_size_bytes": binding.payload_size_bytes,
        "published_at": _canonical_timestamp(binding.published_at),
        "producer_version": "0.6.536",
        "release_id": binding.release_id,
        "source_manifest_sha256": _require_digest(binding.source_manifest_sha256),
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
        binding = EvidenceBinding(
            active_manifest_sha256=_require_digest(value["active_manifest_sha256"]),
            binding_schema_version=value["binding_schema_version"],
            generation_id=_require_generation_id(value["generation_id"]),
            payload_filename=value["payload_filename"],
            payload_sha256=_require_digest(value["payload_sha256"]),
            payload_size_bytes=value["payload_size_bytes"],
            published_at=value["published_at"],
            producer_version=value["producer_version"],
            release_id=value["release_id"],
            source_manifest_sha256=_require_digest(value["source_manifest_sha256"]),
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
        or item.st_uid != os.getuid()
        or stat.S_IMODE(item.st_mode) != 0o700
    ):
        raise IntegrationEvidenceInvalid()


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
        passwd_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
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
        if fd >= 0:
            os.close(fd)


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
            or item.st_uid != os.getuid()
            or item.st_nlink != 1
            or stat.S_IMODE(item.st_mode) != 0o600
            or item.st_size > _LOCK_MAX_BYTES
        ):
            raise IntegrationEvidenceInvalid()
        return fd
    except IntegrationEvidenceError:
        if "fd" in locals():
            os.close(fd)
        raise
    except OSError as exc:
        if "fd" in locals():
            os.close(fd)
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
        if named_fd >= 0:
            os.close(named_fd)


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
        if fresh_root_fd >= 0:
            os.close(fresh_root_fd)


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(fd)


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
        if integration_fd >= 0:
            os.close(integration_fd)
        if app_fd >= 0:
            os.close(app_fd)
        if state_fd >= 0:
            os.close(state_fd)
    return state_identity, integration_identity


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
        if current_fd >= 0:
            os.close(current_fd)
        if release_fd >= 0:
            os.close(release_fd)
        os.close(root_fd)


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
        if (
            held_set.release_mode != release_mode
            or held_set.current_mode != current_mode
        ):
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
                os.close(fd)
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
        os.close(root_fd)
