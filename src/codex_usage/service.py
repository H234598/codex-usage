from __future__ import annotations

import ast
import base64
import csv
import errno
import hashlib
import importlib.metadata as importlib_metadata
import json
import os
import secrets
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePath, PurePosixPath
from typing import IO, Any, cast

from . import integration_installer, private_io
from .config import (
    AppConfig,
    _validate_config,
    _xdg_root,
    default_config_path,
)
from .integration_attestation import (
    PRODUCER_RELEASE_MODULES,
    TRUSTED_CORE_MODULES,
    _active_entrypoint_candidate_from_active_manifest,
    verify_active_manifest_at,
)
from .integration_timeout_contract import INTEGRATION_WATCHDOG_SYSTEMD_TIMEOUT_SECONDS
from .json_utils import loads_strict
from .private_io import (
    ensure_private_directory,
    private_path_lock,
    read_private_text,
    write_private_text,
)

SERVICE_NAME = "codex-usage.service"
TIMER_NAME = "codex-usage.timer"
BROWSER_EXECUTABLE_NAME = "codex-usage-browser"
INTEGRATION_WATCHDOG_EXECUTABLE_NAME = "codex-usage-integration-watchdog"
SERVICE_RUNTIME_V2_DIRECTORY_NAME = "codex-usage-service-runtime-v2"
SERVICE_RUNTIME_V2_CURRENT_NAME = "current"
SERVICE_RUNTIME_V2_STAGING_PREFIX = ".service-runtime-v2-staging-"
SERVICE_RUNTIME_V2_WATCHDOG_NAME = "codex-usage-integration-watchdog-v2"
MANAGED_MARKER = "X-Codex-Usage-Managed=true"
MAX_UNIT_BYTES = 100_000
SYSTEMCTL_OUTPUT_MAX_BYTES = 64 * 1024
SYSTEMCTL_TIMEOUT_SECONDS = 30
SERVICE_OPERATION_LOCK_NAME = ".codex-usage-operation"
SERVICE_OPERATION_LOCK_TIMEOUT_SECONDS = 30
SERVICE_PENDING_V1_NAME = ".codex-usage-service-pending-v1.json"
SERVICE_PENDING_V1_MAX_BYTES = 512 * 1024
SERVICE_PENDING_UNIT_STAGING_PREFIX = ".codex-usage-service-pending-v1-"
EXECUTABLE_SCRIPT_MAX_BYTES = 128 * 1024
INTERPRETER_MAX_BYTES = 128 * 1024 * 1024
PACKAGE_FILE_MAX_BYTES = 1024 * 1024
PACKAGE_RECORD_MAX_BYTES = 2 * 1024 * 1024
MAX_DISTRIBUTION_FILES = 4096
MAX_SERVICE_RUNTIME_FILES = 4096
EXPECTED_DISTRIBUTION_NAME = "codex-usage"
EXPECTED_DISTRIBUTION_VERSION = "0.6.538"
REPEATABLE_CORE_METADATA_FIELDS = frozenset(
    {
        "classifier",
        "dynamic",
        "license-file",
        "obsoletes",
        "obsoletes-dist",
        "platform",
        "provides",
        "provides-dist",
        "provides-extra",
        "requires",
        "requires-dist",
        "requires-external",
        "supported-platform",
        "project-url",
    }
)
SERVICE_RUNTIME_UNSET_ENVIRONMENT_NAMES = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONUSERBASE",
    "PYTHONSTARTUP",
    "PYTHONINSPECT",
    "PYTHONEXECUTABLE",
    "PYTHONBREAKPOINT",
    "PYTHONDEBUG",
    "PYTHONVERBOSE",
    "PYTHONCASEOK",
    "PYTHONPYCACHEPREFIX",
    "PYTHONPLATLIBDIR",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "LD_DEBUG",
    "LD_ORIGIN_PATH",
    "LD_PROFILE",
    "LD_USE_LOAD_BIAS",
    "DYLD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
)
EXEC_MAIN_CODE_NAMES = {
    "1": "exited",
    "2": "killed",
    "3": "dumped",
    "4": "trapped",
    "5": "stopped",
    "6": "continued",
}


class ServiceError(Exception):
    pass


class ServicePartialInstallError(ServiceError):
    def __init__(self, operation: str, partial_units: tuple[Path, ...]) -> None:
        self.operation = operation
        self.partial_units = partial_units
        units = ", ".join(str(path) for path in partial_units)
        super().__init__(
            f"could not roll back {operation}; partial systemd unit state remains: {units}"
        )


@dataclass(frozen=True)
class _RegularFileBinding:
    path: Path
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int
    payload: bytes
    sha256: str


@dataclass(frozen=True)
class _DistributionBinding:
    version: str
    metadata: _RegularFileBinding
    record: _RegularFileBinding
    modules: tuple[_RegularFileBinding, ...]


@dataclass(frozen=True)
class _DirectoryBinding:
    path: Path
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _ExecutableBinding:
    path: Path
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int
    payload: bytes
    sha256: str
    interpreter: _RegularFileBinding
    expected_module: str
    distribution: _DistributionBinding | None = None


@dataclass(frozen=True)
class _ServiceRuntimeBinding:
    root: _DirectoryBinding
    generation: _DirectoryBinding
    interpreter: _RegularFileBinding
    watchdog: _RegularFileBinding
    pyvenv: _RegularFileBinding
    distribution: _DistributionBinding


@dataclass(frozen=True)
class _ServiceRuntimePublication:
    binding: _ServiceRuntimeBinding
    rollback_path: Path | None
    rollback_identity: object | None
    source_payloads: tuple[tuple[str, bytes], ...] = ()


@dataclass
class _PendingServiceTransaction:
    unit_dir: Path
    document: dict[str, object]


@dataclass(frozen=True)
class _ServiceInstallReceipt:
    result: dict[str, Any]
    runtime: _ServiceRuntimePublication
    pending: _PendingServiceTransaction | None


_RESOLVED_INTEGRATION_WATCHDOG_BINDINGS: dict[
    Path, tuple[_ExecutableBinding, _ExecutableBinding]
] = {}


def _raise_service_rollback_error(
    operation: str,
    primary_error: BaseException,
    rollback_errors: list[BaseException],
    *,
    partial_units: tuple[Path, ...] = (),
) -> None:
    errors = [primary_error, *rollback_errors]
    message = f"{operation} rollback failed: primary operation, rollback steps"
    if all(isinstance(error, Exception) for error in errors):
        causes: BaseException = ExceptionGroup(
            message,
            cast("list[Exception]", errors),
        )
    else:
        causes = BaseExceptionGroup(message, errors)
    if partial_units:
        raise ServicePartialInstallError(operation, partial_units) from causes
    raise ServiceError(f"could not roll back {operation}") from causes


@contextmanager
def _service_operation_lock() -> Iterator[None]:
    unit_dir = _unit_directory()
    with private_path_lock(
        unit_dir / SERVICE_OPERATION_LOCK_NAME,
        timeout_seconds=SERVICE_OPERATION_LOCK_TIMEOUT_SECONDS,
        label="systemd service lock",
    ):
        yield


def _pending_service_path(unit_dir: Path | None = None) -> Path:
    return (unit_dir if unit_dir is not None else _unit_directory()) / SERVICE_PENDING_V1_NAME


def _load_pending_service_operation(unit_dir: Path) -> dict[str, object] | None:
    try:
        parent_fd = os.open(unit_dir, private_io._directory_open_flags())
    except OSError as exc:
        raise ServiceError("could not open service transaction directory") from exc
    try:
        try:
            payload, _identity = private_io.read_private_bytes_at(
                parent_fd,
                SERVICE_PENDING_V1_NAME,
                maximum=SERVICE_PENDING_V1_MAX_BYTES,
                mode=0o600,
            )
        except FileNotFoundError:
            return None
    except (OSError, ValueError) as exc:
        raise ServiceError("service pending transaction is unsafe") from exc
    finally:
        os.close(parent_fd)
    try:
        decoded = loads_strict(payload)
    except ValueError as exc:
        raise ServiceError("service pending transaction is invalid") from exc
    if not isinstance(decoded, dict):
        raise ServiceError("service pending transaction is invalid")
    return cast(dict[str, object], decoded)


def _runtime_binding_fingerprint(binding: _ServiceRuntimeBinding) -> str:
    """Return a path-independent identity for a private runtime generation."""
    def directory(item: _DirectoryBinding, *, include_nlink: bool = True) -> list[int]:
        values = [
            item.device,
            item.inode,
            item.mode,
            item.uid,
            item.gid,
        ]
        if include_nlink:
            values.append(item.nlink)
        return values

    def regular(item: _RegularFileBinding) -> list[int | str]:
        return [
            item.device,
            item.inode,
            item.mode,
            item.uid,
            item.gid,
            item.nlink,
            item.size,
            item.mtime_ns,
            item.ctime_ns,
            item.sha256,
        ]

    document = {
        "generation": directory(binding.generation),
        "interpreter": regular(binding.interpreter),
        "metadata": regular(binding.distribution.metadata),
        "modules": [regular(item) for item in binding.distribution.modules],
        "pyvenv": regular(binding.pyvenv),
        "record": regular(binding.distribution.record),
        # A private runtime transaction deliberately creates/removes a sibling
        # staging generation.  That changes only the root directory's link
        # count, not its identity; binding it would make a durably recorded
        # successful rollback impossible to recognize on a retry.
        "root": directory(binding.root, include_nlink=False),
        "version": binding.distribution.version,
        "watchdog": regular(binding.watchdog),
    }
    try:
        encoded = json.dumps(
            document,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:  # pragma: no cover - dataclasses are fixed
        raise ServiceError("service runtime identity is invalid") from exc
    return hashlib.sha256(encoded).hexdigest()


def _timer_enable_link_snapshot(unit_dir: Path) -> str | None:
    wants = unit_dir / "timers.target.wants"
    _assert_no_symlink_ancestors(wants.parent)
    if not wants.exists():
        if wants.is_symlink():
            raise ServiceError("systemd timer wants directory must not be a symlink")
        return None
    if wants.is_symlink() or not wants.is_dir():
        raise ServiceError("systemd timer wants path must be a real directory")
    link = wants / TIMER_NAME
    if not (link.exists() or link.is_symlink()):
        return None
    if not link.is_symlink():
        raise ServiceError("systemd timer enable path must be a symlink")
    try:
        target = os.readlink(link)
        if not target or "\x00" in target or link.resolve(strict=False) != (
            unit_dir / TIMER_NAME
        ).resolve(strict=False):
            raise ServiceError("systemd timer enable link is invalid")
    except (OSError, RuntimeError) as exc:
        raise ServiceError("could not read systemd timer enable link") from exc
    return target


def _pending_runtime_document(
    *,
    root: Path,
    staging: Path,
    old: _ServiceRuntimeBinding | None,
    new: _ServiceRuntimeBinding,
) -> dict[str, object]:
    if staging.parent != root or staging.name == SERVICE_RUNTIME_V2_CURRENT_NAME:
        raise ServiceError("service runtime transaction path is invalid")
    return {
        "current_name": SERVICE_RUNTIME_V2_CURRENT_NAME,
        "new_fingerprint": _runtime_binding_fingerprint(new),
        "old_fingerprint": None if old is None else _runtime_binding_fingerprint(old),
        "root": str(root),
        "staging_name": staging.name,
    }


def _write_pending_service_transaction(transaction: _PendingServiceTransaction) -> None:
    try:
        payload = json.dumps(
            transaction.document,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise ServiceError("service pending transaction is invalid") from exc
    if len(payload.encode("utf-8")) > SERVICE_PENDING_V1_MAX_BYTES:
        raise ServiceError("service pending transaction is too large")
    try:
        write_private_text(
            _pending_service_path(transaction.unit_dir),
            payload,
            label="service pending transaction",
            mode=0o600,
        )
    except (OSError, ValueError) as exc:
        raise ServiceError("could not write service pending transaction") from exc


def _advance_pending_service_transaction(
    transaction: _PendingServiceTransaction,
    phase: str,
) -> None:
    if phase not in {"prepared", "quiesced", "runtime", "units", "reloaded", "activated"}:
        raise ServiceError("service pending transaction phase is invalid")
    transaction.document["phase"] = phase
    _write_pending_service_transaction(transaction)


def _prepare_pending_service_transaction(
    *,
    unit_dir: Path,
    operation: str,
    previous_units: dict[Path, str | None],
    new_units: dict[Path, str],
    activation: tuple[str, str],
    enable_link: str | None,
    root: Path,
    staging: Path,
    old: _ServiceRuntimeBinding | None,
    new: _ServiceRuntimeBinding,
) -> _PendingServiceTransaction:
    if operation not in {"install", "enable"}:
        raise ServiceError("service pending transaction operation is invalid")
    service_path = unit_dir / SERVICE_NAME
    timer_path = unit_dir / TIMER_NAME
    paths = {service_path, timer_path}
    if set(previous_units) != paths or set(new_units) != paths:
        raise ServiceError("service pending transaction units are invalid")
    previous: dict[Path, dict[str, object] | None] = {}
    replacements: dict[Path, dict[str, object]] = {}
    for path in (service_path, timer_path):
        snapshot = _pending_unit_snapshot(path)
        if (None if snapshot is None else snapshot["text"]) != previous_units[path]:
            raise ServiceError("systemd unit changed before pending transaction")
        previous[path] = snapshot
        replacements[path] = {
            "new": _pending_new_unit_snapshot(new_units[path]),
            "old": snapshot,
            "restored_old_identity": None,
            "staging_name": _pending_unit_staging_name(path),
        }
    transaction = _PendingServiceTransaction(
        unit_dir=unit_dir,
        document={
            "activation": [activation[0], activation[1]],
            "enable_link": enable_link,
            "operation": operation,
            "phase": "prepared",
            "runtime": _pending_runtime_document(
                root=root,
                staging=staging,
                old=old,
                new=new,
            ),
            "schema_version": 1,
            "unit_directory": _pending_unit_directory_identity(unit_dir),
            "units": {
                SERVICE_NAME: replacements[service_path],
                TIMER_NAME: replacements[timer_path],
            },
        },
    )
    _write_pending_service_transaction(transaction)
    for path in (service_path, timer_path):
        _stage_pending_service_unit_generation(transaction, path)
    return transaction


def _validate_pending_service_transaction(
    document: dict[str, object],
    unit_dir: Path,
) -> tuple[
    dict[Path, dict[str, object]],
    tuple[str, str],
    str | None,
    dict[str, object],
    dict[str, int],
]:
    expected = {
        "activation",
        "enable_link",
        "operation",
        "phase",
        "runtime",
        "schema_version",
        "unit_directory",
        "units",
    }
    if set(document) != expected or document.get("schema_version") != 1:
        raise ServiceError("service pending transaction is invalid")
    if document.get("operation") not in {"install", "enable"} or document.get(
        "phase"
    ) not in {"prepared", "quiesced", "runtime", "units", "reloaded", "activated"}:
        raise ServiceError("service pending transaction is invalid")
    activation = document.get("activation")
    if (
        not isinstance(activation, list)
        or len(activation) != 2
        or any(type(value) is not str for value in activation)
        or activation[0] not in {"enabled", "disabled", "not-found"}
        or activation[1] not in {"active", "inactive"}
    ):
        raise ServiceError("service pending transaction is invalid")
    enable_link = document.get("enable_link")
    if enable_link is not None and (
        type(enable_link) is not str or not enable_link or "\x00" in enable_link
    ):
        raise ServiceError("service pending transaction is invalid")
    units = document.get("units")
    if not isinstance(units, dict) or set(units) != {SERVICE_NAME, TIMER_NAME}:
        raise ServiceError("service pending transaction is invalid")
    replacements: dict[Path, dict[str, object]] = {}
    for name in (SERVICE_NAME, TIMER_NAME):
        value = units.get(name)
        replacements[unit_dir / name] = _validate_pending_unit_replacement(
            value, unit_name=name
        )
    unit_directory = _validate_pending_unit_directory_identity(
        document.get("unit_directory")
    )
    runtime = document.get("runtime")
    if not isinstance(runtime, dict) or set(runtime) != {
        "current_name",
        "new_fingerprint",
        "old_fingerprint",
        "root",
        "staging_name",
    }:
        raise ServiceError("service pending transaction is invalid")
    if (
        runtime.get("root") != str(_service_runtime_root())
        or runtime.get("current_name") != SERVICE_RUNTIME_V2_CURRENT_NAME
        or type(runtime.get("staging_name")) is not str
        or not str(runtime["staging_name"]).startswith(SERVICE_RUNTIME_V2_STAGING_PREFIX)
        or len(str(runtime["staging_name"]))
        != len(SERVICE_RUNTIME_V2_STAGING_PREFIX) + 32
        or any(
            character not in "0123456789abcdef"
            for character in str(runtime["staging_name"])[
                len(SERVICE_RUNTIME_V2_STAGING_PREFIX) :
            ]
        )
    ):
        raise ServiceError("service pending transaction is invalid")
    for field in ("new_fingerprint", "old_fingerprint"):
        value = runtime.get(field)
        if value is None and field == "old_fingerprint":
            continue
        if (
            type(value) is not str
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ServiceError("service pending transaction is invalid")
    return (
        replacements,
        (activation[0], activation[1]),
        enable_link,
        cast(dict[str, object], runtime),
        unit_directory,
    )


def _pending_unit_directory_identity(unit_dir: Path) -> dict[str, int]:
    binding = _read_bound_directory(unit_dir, label="systemd user unit directory")
    if binding.uid != os.geteuid() or stat.S_IMODE(binding.mode) != 0o700:
        raise ServiceError("systemd user unit directory is not private")
    return {
        "device": binding.device,
        "gid": binding.gid,
        "inode": binding.inode,
        "mode": binding.mode,
        "uid": binding.uid,
    }


def _validate_pending_unit_directory_identity(value: object) -> dict[str, int]:
    fields = {"device", "gid", "inode", "mode", "uid"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ServiceError("service pending transaction is invalid")
    identity: dict[str, int] = {}
    for field in fields:
        number = value.get(field)
        if type(number) is not int or number < 0:
            raise ServiceError("service pending transaction is invalid")
        identity[field] = number
    if identity["uid"] != os.geteuid() or stat.S_IMODE(identity["mode"]) != 0o700:
        raise ServiceError("service pending transaction is invalid")
    return identity


def _pending_unit_file_identity(binding: _RegularFileBinding) -> dict[str, int]:
    return {
        "device": binding.device,
        "gid": binding.gid,
        "inode": binding.inode,
        "mode": binding.mode,
        "mtime_ns": binding.mtime_ns,
        "nlink": binding.nlink,
        "size": binding.size,
        "uid": binding.uid,
    }


def _validate_pending_unit_file_identity(value: object) -> dict[str, int]:
    fields = {
        "device",
        "gid",
        "inode",
        "mode",
        "mtime_ns",
        "nlink",
        "size",
        "uid",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ServiceError("service pending transaction is invalid")
    identity: dict[str, int] = {}
    for field in fields:
        number = value.get(field)
        if type(number) is not int or number < 0:
            raise ServiceError("service pending transaction is invalid")
        identity[field] = number
    if (
        identity["uid"] != os.geteuid()
        or identity["nlink"] != 1
        or identity["size"] > MAX_UNIT_BYTES
        or identity["mode"] & 0o022
    ):
        raise ServiceError("service pending transaction is invalid")
    return identity


def _pending_unit_snapshot(path: Path) -> dict[str, object] | None:
    if not (path.exists() or path.is_symlink()):
        return None
    binding = _read_bound_regular_file(
        path,
        label="systemd unit",
        max_bytes=MAX_UNIT_BYTES,
        single_link=True,
    )
    if binding.uid != os.geteuid() or binding.mode & 0o022:
        raise ServiceError("systemd unit is unsafe")
    try:
        text = binding.payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ServiceError("systemd unit is not valid UTF-8") from exc
    return {
        "identity": _pending_unit_file_identity(binding),
        "sha256": binding.sha256,
        "text": text,
    }


def _pending_new_unit_snapshot(text: str) -> dict[str, object]:
    if type(text) is not str or len(text.encode("utf-8")) > MAX_UNIT_BYTES:
        raise ServiceError("service pending transaction unit is invalid")
    payload = text.encode("utf-8")
    return {
        "identity": None,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "text": text,
    }


def _validate_pending_unit_snapshot(
    value: object,
    *,
    allow_unbound_identity: bool,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"identity", "sha256", "text"}:
        raise ServiceError("service pending transaction is invalid")
    text = value.get("text")
    digest = value.get("sha256")
    if (
        type(text) is not str
        or len(text.encode("utf-8")) > MAX_UNIT_BYTES
        or type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or digest != hashlib.sha256(text.encode("utf-8")).hexdigest()
    ):
        raise ServiceError("service pending transaction is invalid")
    identity_value = value.get("identity")
    if identity_value is None:
        if not allow_unbound_identity:
            raise ServiceError("service pending transaction is invalid")
        identity: dict[str, int] | None = None
    else:
        identity = _validate_pending_unit_file_identity(identity_value)
    return {"identity": identity, "sha256": digest, "text": text}


def _validate_pending_unit_replacement(
    value: object,
    *,
    unit_name: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "new",
        "old",
        "restored_old_identity",
        "staging_name",
    }:
        raise ServiceError("service pending transaction is invalid")
    old_value = value.get("old")
    old = (
        None
        if old_value is None
        else _validate_pending_unit_snapshot(old_value, allow_unbound_identity=False)
    )
    new = _validate_pending_unit_snapshot(
        value.get("new"), allow_unbound_identity=True
    )
    restored_old_identity_value = value.get("restored_old_identity")
    if old is None:
        if restored_old_identity_value is not None:
            raise ServiceError("service pending transaction is invalid")
        restored_old_identity: dict[str, int] | None = None
    elif restored_old_identity_value is None:
        restored_old_identity = None
    else:
        restored_old_identity = _validate_pending_unit_file_identity(
            restored_old_identity_value
        )
    staging_name = value.get("staging_name")
    expected_staging_prefix = f"{SERVICE_PENDING_UNIT_STAGING_PREFIX}{unit_name}-"
    if (
        type(staging_name) is not str
        or not staging_name.startswith(expected_staging_prefix)
        or len(staging_name) != len(expected_staging_prefix) + 32
    ):
        raise ServiceError("service pending transaction is invalid")
    _safe_unit_component(staging_name)
    name_tail = staging_name.rsplit("-", 1)[-1]
    if len(name_tail) != 32 or any(character not in "0123456789abcdef" for character in name_tail):
        raise ServiceError("service pending transaction is invalid")
    return {
        "new": new,
        "old": old,
        "restored_old_identity": restored_old_identity,
        "staging_name": staging_name,
    }


def _pending_unit_staging_name(path: Path) -> str:
    name = _safe_unit_component(path.name)
    return f"{SERVICE_PENDING_UNIT_STAGING_PREFIX}{name}-{secrets.token_hex(16)}"


def _pending_unit_staging_path(
    unit_dir: Path,
    replacement: dict[str, object],
) -> Path:
    name = cast(str, replacement["staging_name"])
    _safe_unit_component(name)
    return unit_dir / name


def _pending_unit_matches(
    actual: dict[str, object] | None,
    expected: dict[str, object],
    *,
    require_identity: bool,
) -> bool:
    if actual is None:
        return False
    if actual["text"] != expected["text"] or actual["sha256"] != expected["sha256"]:
        return False
    return not require_identity or actual["identity"] == expected["identity"]


def _pending_unit_matches_old_generation(
    actual: dict[str, object] | None,
    replacement: dict[str, object],
) -> bool:
    """Accept the original Unit identity or this journal's own restored identity."""
    old = cast(dict[str, object] | None, replacement["old"])
    if old is None:
        return False
    if _pending_unit_matches(actual, old, require_identity=True):
        return True
    restored_identity = cast(
        dict[str, int] | None, replacement["restored_old_identity"]
    )
    if restored_identity is None or actual is None:
        return False
    return (
        actual["text"] == old["text"]
        and actual["sha256"] == old["sha256"]
        and actual["identity"] == restored_identity
    )


def _stage_pending_service_unit_generation(
    transaction: _PendingServiceTransaction,
    path: Path,
) -> None:
    replacements, _activation, _enable_link, _runtime, _unit_directory = (
        _validate_pending_service_transaction(transaction.document, transaction.unit_dir)
    )
    if path not in replacements:
        raise ServiceError("service pending transaction unit is invalid")
    expected_new = cast(dict[str, object], replacements[path]["new"])
    if expected_new["identity"] is not None:
        raise ServiceError("service pending transaction unit is already bound")
    staging = _pending_unit_staging_path(transaction.unit_dir, replacements[path])
    parent_fd = -1
    try:
        parent_fd = os.open(transaction.unit_dir, private_io._directory_open_flags())
        private_io.write_private_bytes_at(
            parent_fd,
            staging.name,
            cast(str, expected_new["text"]).encode("utf-8"),
            mode=0o600,
        )
        os.fsync(parent_fd)
    except (OSError, ValueError) as exc:
        raise ServiceError("could not stage pending systemd unit") from exc
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
    actual = _pending_unit_snapshot(staging)
    if not _pending_unit_matches(actual, expected_new, require_identity=False):
        raise ServiceError("systemd unit changed before pending transaction binding")
    assert actual is not None
    expected_new["identity"] = actual["identity"]
    transaction.document["units"] = {
        SERVICE_NAME: replacements[transaction.unit_dir / SERVICE_NAME],
        TIMER_NAME: replacements[transaction.unit_dir / TIMER_NAME],
    }
    _write_pending_service_transaction(transaction)


def _pending_unit_ready_for_cutover(
    *,
    unit_dir: Path,
    expected_directory: dict[str, int],
    path: Path,
    replacement: dict[str, object],
) -> None:
    if _pending_unit_directory_identity(unit_dir) != expected_directory:
        raise ServiceError("service pending transaction unit directory changed")
    old = cast(dict[str, object] | None, replacement["old"])
    actual = _pending_unit_snapshot(path)
    if old is None:
        if actual is not None:
            raise ServiceError("service pending unit changed before cutover")
    elif not _pending_unit_matches(actual, old, require_identity=True):
        raise ServiceError("service pending unit changed before cutover")
    new = cast(dict[str, object], replacement["new"])
    staging = _pending_unit_snapshot(_pending_unit_staging_path(unit_dir, replacement))
    if new["identity"] is None or not _pending_unit_matches(
        staging, new, require_identity=True
    ):
        raise ServiceError("service pending unit staging changed before cutover")


def _publish_pending_service_unit_generation(
    transaction: _PendingServiceTransaction,
    path: Path,
) -> None:
    replacements, _activation, _enable_link, _runtime, expected_directory = (
        _validate_pending_service_transaction(transaction.document, transaction.unit_dir)
    )
    if path not in replacements:
        raise ServiceError("service pending transaction unit is invalid")
    replacement = replacements[path]
    _pending_unit_ready_for_cutover(
        unit_dir=transaction.unit_dir,
        expected_directory=expected_directory,
        path=path,
        replacement=replacement,
    )
    staging = _pending_unit_staging_path(transaction.unit_dir, replacement)
    parent_fd = -1
    try:
        parent_fd = os.open(transaction.unit_dir, private_io._directory_open_flags())
        os.replace(
            staging.name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    except OSError as exc:
        raise ServiceError("could not publish pending systemd unit") from exc
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
    _after_pending_service_unit_cutover(path)


def _after_pending_service_unit_cutover(_path: Path) -> None:
    """Test seam after the journal-bound atomic Unit rename."""
    return None


def _pending_unit_recovery_actions(
    *,
    unit_dir: Path,
    expected_directory: dict[str, int],
    replacements: dict[Path, dict[str, object]],
) -> dict[Path, tuple[str, bool]]:
    if _pending_unit_directory_identity(unit_dir) != expected_directory:
        raise ServiceError("service pending transaction unit directory changed")
    actions: dict[Path, tuple[str, bool]] = {}
    for path, replacement in replacements.items():
        old = cast(dict[str, object] | None, replacement["old"])
        new = cast(dict[str, object], replacement["new"])
        actual = _pending_unit_snapshot(path)
        staging = _pending_unit_snapshot(
            _pending_unit_staging_path(unit_dir, replacement)
        )
        if new["identity"] is None:
            # A crash can occur after the initial journal fsync but before the
            # private staging file has a durable identity.  That state has not
            # published a Unit: only the verified old target (or absence on a
            # first install) is recoverable, and any visible staging entry is
            # deliberately left fail-closed as unknown.
            if staging is not None:
                raise ServiceError(f"service pending unit is unbound: {path}")
            if old is None and actual is None:
                actions[path] = ("unchanged", False)
                continue
            if _pending_unit_matches_old_generation(actual, replacement):
                actions[path] = ("unchanged", False)
                continue
            raise ServiceError(f"service pending unit changed: {path}")
        if staging is not None and not _pending_unit_matches(
            staging, new, require_identity=True
        ):
            raise ServiceError(f"service pending unit staging changed: {path}")
        if old is None and actual is None:
            actions[path] = ("unchanged", staging is not None)
            continue
        if _pending_unit_matches_old_generation(actual, replacement):
            actions[path] = ("unchanged", staging is not None)
            continue
        if new["identity"] is not None and _pending_unit_matches(
            actual, new, require_identity=True
        ):
            if staging is not None:
                raise ServiceError(f"service pending unit staging is ambiguous: {path}")
            actions[path] = ("delete" if old is None else "restore", False)
            continue
        raise ServiceError(f"service pending unit changed: {path}")
    return actions


def _restore_pending_unit_snapshots(
    *,
    transaction: _PendingServiceTransaction,
    unit_dir: Path,
    expected_directory: dict[str, int],
    replacements: dict[Path, dict[str, object]],
    actions: dict[Path, tuple[str, bool]],
) -> None:
    # Repeat the non-mutating check immediately before touching either managed
    # unit.  A changed unit is deliberately left untouched for a later trusted
    # operator decision instead of being overwritten from a stale journal.
    if _pending_unit_recovery_actions(
        unit_dir=unit_dir,
        expected_directory=expected_directory,
        replacements=replacements,
    ) != actions:
        raise ServiceError("service pending transaction unit state changed")
    errors: list[BaseException] = []
    for path, (action, discard_staging) in actions.items():
        try:
            replacement = replacements[path]
            old = cast(dict[str, object] | None, replacement["old"])
            if action == "delete":
                if old is not None:  # pragma: no cover - action construction guards this
                    raise ServiceError("service pending transaction unit is invalid")
                try:
                    path.unlink()
                    private_io._fsync_directory(path.parent)
                except OSError as exc:
                    raise ServiceError("could not remove pending systemd unit") from exc
                if _pending_unit_snapshot(path) is not None:
                    raise ServiceError("pending systemd unit changed during removal")
            elif action == "restore":
                if old is None:  # pragma: no cover - action construction guards this
                    raise ServiceError("service pending transaction unit is invalid")
                label = "systemd service" if path.name == SERVICE_NAME else "systemd timer"
                write_private_text(path, cast(str, old["text"]), label=label, mode=0o600)
                restored = _pending_unit_snapshot(path)
                if not _pending_unit_matches(restored, old, require_identity=False):
                    raise ServiceError("pending systemd unit changed during restore")
                assert restored is not None
                _bind_pending_restored_old_unit_identity(transaction, path, restored)
            elif action != "unchanged":
                raise ServiceError("service pending transaction unit is invalid")
            if discard_staging:
                staging = _pending_unit_staging_path(unit_dir, replacement)
                new = cast(dict[str, object], replacement["new"])
                staged = _pending_unit_snapshot(staging)
                if not _pending_unit_matches(staged, new, require_identity=True):
                    raise ServiceError("pending systemd unit staging changed during cleanup")
                staging.unlink()
                private_io._fsync_directory(staging.parent)
        except BaseException as exc:
            errors.append(exc)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        group_type = (
            ExceptionGroup
            if all(isinstance(error, Exception) for error in errors)
            else BaseExceptionGroup
        )
        raise group_type("pending systemd unit recovery failed", errors)


def _bind_pending_restored_old_unit_identity(
    transaction: _PendingServiceTransaction,
    path: Path,
    restored: dict[str, object],
) -> None:
    """Durably bind one recovery-owned old Unit replacement before retrying."""
    replacements, _activation, _enable_link, _runtime, _unit_directory = (
        _validate_pending_service_transaction(transaction.document, transaction.unit_dir)
    )
    replacement = replacements.get(path)
    if replacement is None:
        raise ServiceError("service pending transaction unit is invalid")
    old = cast(dict[str, object] | None, replacement["old"])
    if old is None or not _pending_unit_matches(restored, old, require_identity=False):
        raise ServiceError("pending systemd unit changed during restore")
    replacement["restored_old_identity"] = restored["identity"]
    transaction.document["units"] = {
        SERVICE_NAME: replacements[transaction.unit_dir / SERVICE_NAME],
        TIMER_NAME: replacements[transaction.unit_dir / TIMER_NAME],
    }
    _write_pending_service_transaction(transaction)


def _remove_private_runtime_generation(root: Path, generation: Path) -> None:
    identity = integration_installer._provisional_path_identity(generation, directory=True)
    if not integration_installer._remove_owned_entry(
        generation,
        identity,
        integration_installer._directory_identity(root),
        directory=True,
        recursive=True,
    ):
        raise ServiceError("could not remove failed service runtime generation")


def _recover_pending_runtime(runtime: dict[str, object]) -> None:
    root = _service_runtime_root()
    _private_runtime_directory(root, label="service runtime root")
    current = root / SERVICE_RUNTIME_V2_CURRENT_NAME
    staging = root / cast(str, runtime["staging_name"])
    expected_new = cast(str, runtime["new_fingerprint"])
    expected_old = cast(str | None, runtime["old_fingerprint"])

    def binding_at(path: Path) -> _ServiceRuntimeBinding | None:
        if not (path.exists() or path.is_symlink()):
            return None
        return _service_runtime_binding(root, path)

    current_binding = binding_at(current)
    staging_binding = binding_at(staging)
    current_fingerprint = (
        None if current_binding is None else _runtime_binding_fingerprint(current_binding)
    )
    staging_fingerprint = (
        None if staging_binding is None else _runtime_binding_fingerprint(staging_binding)
    )
    if expected_old is None:
        if current_fingerprint == expected_new and staging_binding is None:
            assert current_binding is not None
            _rollback_service_runtime(
                _ServiceRuntimePublication(current_binding, None, None)
            )
            return
        if current_binding is None and staging_fingerprint == expected_new:
            _remove_private_runtime_generation(root, staging)
            return
        if current_binding is None and staging_binding is None:
            return
        raise ServiceError("service pending transaction runtime is ambiguous")
    if current_fingerprint == expected_new and staging_fingerprint == expected_old:
        assert current_binding is not None
        rollback_identity = integration_installer._provisional_path_identity(
            staging,
            directory=True,
        )
        _rollback_service_runtime(
            _ServiceRuntimePublication(current_binding, staging, rollback_identity)
        )
        return
    if current_fingerprint == expected_old and staging_fingerprint == expected_new:
        _remove_private_runtime_generation(root, staging)
        return
    if current_fingerprint == expected_old and staging_binding is None:
        return
    raise ServiceError("service pending transaction runtime is ambiguous")


def _restore_timer_enable_link(unit_dir: Path, expected: str | None) -> None:
    wants = unit_dir / "timers.target.wants"
    link = wants / TIMER_NAME
    if expected is None:
        _cleanup_managed_timer_enable_link()
        return
    ensure_private_directory(wants, label="systemd timer wants directory")
    _assert_no_symlink_ancestors(wants)
    if link.exists() or link.is_symlink():
        _cleanup_managed_timer_enable_link()
    try:
        link.symlink_to(expected)
        private_io._fsync_directory(wants)
    except OSError as exc:
        raise ServiceError("could not restore systemd timer enable link") from exc
    if _timer_enable_link_snapshot(unit_dir) != expected:
        raise ServiceError("systemd timer enable link changed during restore")


def _remove_pending_service_transaction(
    transaction: _PendingServiceTransaction,
) -> None:
    parent_fd = -1
    try:
        parent_fd = os.open(transaction.unit_dir, private_io._directory_open_flags())
        payload, identity = private_io.read_private_bytes_at(
            parent_fd,
            SERVICE_PENDING_V1_NAME,
            maximum=SERVICE_PENDING_V1_MAX_BYTES,
            mode=0o600,
        )
        if loads_strict(payload) != transaction.document:
            raise ServiceError("service pending transaction changed before cleanup")
        item = os.stat(SERVICE_PENDING_V1_NAME, dir_fd=parent_fd, follow_symlinks=False)
        if (
            item.st_dev != identity.device
            or item.st_ino != identity.inode
            or stat.S_IMODE(item.st_mode) != identity.mode
            or item.st_uid != identity.uid
            or item.st_gid != identity.gid
            or item.st_ctime_ns != identity.ctime_ns
        ):
            raise ServiceError("service pending transaction changed before cleanup")
        os.unlink(SERVICE_PENDING_V1_NAME, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except ServiceError:
        raise
    except (OSError, ValueError) as exc:
        raise ServiceError("could not remove service pending transaction") from exc
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _recover_pending_service_operation() -> None:
    """Refuse a potentially mixed service state until its journal can be verified.

    The durable recovery protocol is deliberately completed only by a trusted
    service operation under the existing operation lock; it never deletes an
    unknown journal merely to make status appear healthy.
    """
    unit_dir = _unit_directory()
    pending = _load_pending_service_operation(unit_dir)
    if pending is None:
        return
    (
        replacements,
        activation,
        enable_link,
        runtime,
        expected_directory,
    ) = _validate_pending_service_transaction(pending, unit_dir)
    transaction = _PendingServiceTransaction(unit_dir=unit_dir, document=pending)
    errors: list[BaseException] = []

    # Validate every Unit first, without changing it.  This makes a foreign
    # Unit edit a hard stop rather than a reason to overwrite it.  Once the
    # journal's Unit generation is still ours, quiesce a timer that may have
    # been enabled/restarted after the last durable phase, then roll back the
    # runtime before any systemd command which could re-enable or start it.
    actions: dict[Path, tuple[str, bool]] | None = None
    try:
        actions = _pending_unit_recovery_actions(
            unit_dir=unit_dir,
            expected_directory=expected_directory,
            replacements=replacements,
        )
    except BaseException as exc:
        errors.append(exc)

    if not errors:
        try:
            _quiesce_systemd_activation(_systemd_activation_snapshot())
        except BaseException as exc:
            errors.append(exc)
    if not errors:
        try:
            _recover_pending_runtime(runtime)
        except BaseException as exc:
            errors.append(exc)
    if not errors:
        assert actions is not None
        try:
            _restore_pending_unit_snapshots(
                transaction=transaction,
                unit_dir=unit_dir,
                expected_directory=expected_directory,
                replacements=replacements,
                actions=actions,
            )
        except BaseException as exc:
            errors.append(exc)
    if not errors:
        try:
            _systemctl("daemon-reload")
        except BaseException as exc:
            errors.append(exc)
    if not errors:
        try:
            _restore_timer_enable_link(unit_dir, enable_link)
        except BaseException as exc:
            errors.append(exc)
    if not errors:
        try:
            _restore_systemd_activation(activation)
        except BaseException as exc:
            errors.append(exc)
    if errors:
        # Do not call _restore_systemd_activation after any failed rollback
        # step: enable/start would otherwise execute a still-pending runtime.
        errors.append(ServiceError("service pending transaction activation is blocked"))
    if errors:
        if all(isinstance(error, Exception) for error in errors):
            cause: BaseException = ExceptionGroup(
                "service pending transaction recovery failed",
                cast("list[Exception]", errors),
            )
        else:
            cause = BaseExceptionGroup(
                "service pending transaction recovery failed", errors
            )
        raise ServiceError("could not recover service pending transaction") from cause
    _remove_pending_service_transaction(transaction)


def service_enable(config: AppConfig, config_path: Path | None = None) -> dict[str, Any]:
    _validate_config(config)
    selected_config_path = _select_service_config_path(config_path)
    with _service_operation_lock():
        _recover_pending_service_operation()
        return _service_enable_unlocked(config, selected_config_path)


def _service_enable_unlocked(
    config: AppConfig, config_path: Path | None = None
) -> dict[str, Any]:
    unit_dir = _unit_directory()
    _validate_existing_managed_units(unit_dir)
    installation = _service_install_unlocked(
        config,
        config_path,
        retain_runtime_for_activation=True,
    )
    if not isinstance(installation, _ServiceInstallReceipt):
        raise ServiceError("service installation did not retain a runtime transaction")
    result, runtime, pending = (
        installation.result,
        installation.runtime,
        installation.pending,
    )
    try:
        _systemctl("enable", TIMER_NAME)
        _systemctl("restart", TIMER_NAME)
        _advance_pending_service_transaction(pending, "activated")
    except BaseException as primary_error:
        rollback_errors: list[BaseException] = []
        try:
            _recover_pending_service_operation()
        except BaseException as rollback_error:
            rollback_errors.append(rollback_error)
        if rollback_errors:
            _raise_service_rollback_error(
                "service activation", primary_error, rollback_errors
            )
        raise primary_error
    _commit_service_runtime(runtime)
    _remove_pending_service_transaction(pending)
    return {**result, **_service_status_unlocked()}


def service_install(config: AppConfig, config_path: Path | None = None) -> dict[str, Any]:
    _validate_config(config)
    selected_config_path = _select_service_config_path(config_path)
    with _service_operation_lock():
        _recover_pending_service_operation()
        return _service_install_unlocked(config, selected_config_path)


def _service_install_unlocked(
    config: AppConfig,
    config_path: Path | None = None,
    *,
    retain_runtime_for_activation: bool = False,
) -> dict[str, Any] | _ServiceInstallReceipt:
    unit_dir = _unit_directory()
    _validate_existing_managed_units(unit_dir)
    executable = _resolve_codex_usage()
    config_file = _select_service_config_path(config_path).expanduser().absolute()
    watchdog = (
        _service_runtime_root()
        / SERVICE_RUNTIME_V2_CURRENT_NAME
        / "bin"
        / SERVICE_RUNTIME_V2_WATCHDOG_NAME
    )
    service_text = _render_service(config, watchdog, config_file)
    timer_text = _render_timer(config.interval_seconds)
    paths = (unit_dir / SERVICE_NAME, unit_dir / TIMER_NAME)
    previous = {path: _read_unit_snapshot(path) for path in paths}
    replacements = {paths[0]: service_text, paths[1]: timer_text}
    activation = _systemd_activation_snapshot()
    if activation[0] not in {"enabled", "disabled", "not-found"} or activation[1] not in {
        "active",
        "inactive",
    }:
        raise ServiceError("cannot install over unknown systemd activation state")
    enable_link = _timer_enable_link_snapshot(unit_dir)
    runtime: _ServiceRuntimePublication | None = None
    pending: _PendingServiceTransaction | None = None

    def before_cutover(
        new: _ServiceRuntimeBinding,
        old: _ServiceRuntimeBinding | None,
        staging: Path,
    ) -> None:
        nonlocal pending
        pending = _prepare_pending_service_transaction(
            unit_dir=unit_dir,
            operation="enable" if retain_runtime_for_activation else "install",
            previous_units=previous,
            new_units=replacements,
            activation=activation,
            enable_link=enable_link,
            root=_service_runtime_root(),
            staging=staging,
            old=old,
            new=new,
        )
        _quiesce_systemd_activation(activation)
        _advance_pending_service_transaction(pending, "quiesced")

    try:
        runtime = _materialize_service_runtime(executable, before_cutover=before_cutover)
        assert pending is not None
        _advance_pending_service_transaction(pending, "runtime")
        _before_service_unit_write()
        _revalidate_integration_watchdog_for_unit_write(executable)
        _revalidate_active_producer_for_unit_write(runtime, executable)
        _revalidate_service_runtime_for_unit_write(runtime)
        _publish_pending_service_unit_generation(pending, paths[0])
        _revalidate_integration_watchdog_for_unit_write(executable)
        _revalidate_active_producer_for_unit_write(runtime, executable)
        _revalidate_service_runtime_for_unit_write(runtime)
        _publish_pending_service_unit_generation(pending, paths[1])
        _advance_pending_service_transaction(pending, "units")
        _revalidate_integration_watchdog_for_unit_write(executable)
        _revalidate_active_producer_for_unit_write(runtime, executable)
        _revalidate_service_runtime_for_unit_write(runtime)
        _systemctl("daemon-reload")
        _advance_pending_service_transaction(pending, "reloaded")
        _revalidate_integration_watchdog_for_unit_write(executable)
        _revalidate_active_producer_for_unit_write(runtime, executable)
        _revalidate_service_runtime_for_unit_write(runtime)
    except BaseException as primary_error:
        rollback_errors: list[BaseException] = []
        if pending is not None:
            try:
                _recover_pending_service_operation()
            except BaseException as rollback_error:
                rollback_errors.append(rollback_error)
        elif runtime is not None:
            try:
                _rollback_service_runtime(runtime)
            except BaseException as rollback_error:
                rollback_errors.append(rollback_error)
        if pending is None:
            try:
                _restore_unit_snapshot(previous)
            except BaseException as rollback_error:
                rollback_errors.append(rollback_error)
        partial_units: list[Path] = []
        for path, expected in previous.items():
            try:
                current = _read_unit_snapshot(path)
            except BaseException as inspection_error:
                rollback_errors.append(inspection_error)
                partial_units.append(path)
                continue
            if current != expected:
                partial_units.append(path)
        if rollback_errors or partial_units:
            _raise_service_rollback_error(
                "service installation",
                primary_error,
                rollback_errors,
                partial_units=tuple(partial_units),
            )
        raise primary_error
    assert runtime is not None
    result = {"installed": True, "service": SERVICE_NAME, "timer": TIMER_NAME}
    if retain_runtime_for_activation:
        assert pending is not None
        return _ServiceInstallReceipt(result, runtime, pending)
    try:
        _restore_systemd_activation(activation)
        _restore_timer_enable_link(unit_dir, enable_link)
        _advance_pending_service_transaction(pending, "activated")
        _commit_service_runtime(runtime)
        _remove_pending_service_transaction(pending)
    except BaseException as primary_error:
        try:
            _recover_pending_service_operation()
        except BaseException as rollback_error:
            _raise_service_rollback_error(
                "service installation completion",
                primary_error,
                [rollback_error],
            )
        raise primary_error
    return result


def _select_service_config_path(path: object | None) -> Path:
    if path is not None and not isinstance(path, Path):
        raise ValueError("config path must be a Path")
    selected = default_config_path() if path is None else path
    try:
        return selected.expanduser()
    except RuntimeError as exc:
        raise ValueError("config path cannot be resolved") from exc


def service_disable() -> dict[str, Any]:
    with _service_operation_lock():
        _recover_pending_service_operation()
        return _service_disable_unlocked()


def _service_disable_unlocked() -> dict[str, Any]:
    unit_dir = _unit_directory(create=False)
    if _require_complete_managed_units(unit_dir) is not None:
        _systemctl("disable", "--now", TIMER_NAME)
    return _service_status_unlocked()


def service_uninstall() -> dict[str, Any]:
    with _service_operation_lock():
        _recover_pending_service_operation()
        return _service_uninstall_unlocked()


def _service_uninstall_unlocked() -> dict[str, Any]:
    unit_dir = _unit_directory()
    paths = _require_complete_managed_units(unit_dir)
    if paths is None:
        return {"installed": False, "enabled": False, "active": False}
    previous = {
        path: _read_unit_snapshot(path)
        for path in paths
    }
    activation = _systemd_activation_snapshot()
    try:
        _service_disable_unlocked()
        for path in paths:
            _validate_managed_unit(path)
            path.unlink()
        _systemctl("daemon-reload")
    except Exception as primary_error:
        rollback_errors: list[Exception] = []
        try:
            _restore_unit_snapshot(previous)
        except Exception as rollback_error:
            rollback_errors.append(rollback_error)
        try:
            _systemctl("daemon-reload")
        except Exception as rollback_error:
            rollback_errors.append(rollback_error)
        try:
            _restore_systemd_activation(activation)
        except Exception as rollback_error:
            rollback_errors.append(rollback_error)
        if rollback_errors:
            _raise_service_rollback_error(
                "service uninstallation", primary_error, rollback_errors
            )
        raise primary_error
    return {"installed": False, "enabled": False, "active": False}


def service_status() -> dict[str, Any]:
    with _service_operation_lock():
        _recover_pending_service_operation()
        return _service_status_unlocked()


def _service_status_unlocked() -> dict[str, Any]:
    unit_dir = _unit_directory(create=False)
    service_path = unit_dir / SERVICE_NAME
    timer_path = unit_dir / TIMER_NAME
    installed = _is_managed_unit(service_path) and _is_managed_unit(timer_path)
    if installed:
        enabled = _systemctl_state("is-enabled", TIMER_NAME) == "enabled"
        timer_active = _systemctl_state("is-active", TIMER_NAME) == "active"
        service_active = _systemctl_state("is-active", SERVICE_NAME) in {
            "active",
            "activating",
        }
        timer_details = _systemctl_show(
            TIMER_NAME,
            (
                "SubState",
                "NextElapseUSecMonotonic",
                "NextElapseUSecRealtime",
            ),
        )
        timer_substate = timer_details.get("SubState", "unknown").strip().lower()
        timer_next_elapse = (
            timer_details.get("NextElapseUSecMonotonic")
            or timer_details.get("NextElapseUSecRealtime")
            or ""
        ).strip().lower()
        timer_scheduled = timer_substate == "waiting" and timer_next_elapse not in {
            "",
            "0",
            "infinity",
            "n/a",
        }
        active = timer_active and (service_active or timer_scheduled)
        details = _systemctl_show(
            SERVICE_NAME,
            (
                "Result",
                "ExecMainStatus",
                "ExecMainCode",
                "ExecMainStartTimestamp",
                "ExecMainExitTimestamp",
            ),
        )
    else:
        enabled = False
        active = False
        service_active = False
        timer_scheduled = False
        timer_substate = "unknown"
        details = {}
    return {
        "installed": installed,
        "enabled": enabled,
        "active": active,
        "service_active": service_active,
        "timer_scheduled": timer_scheduled,
        "timer_substate": timer_substate,
        "service_result": details.get("Result", "unknown"),
        "service_exit_status": details.get("ExecMainStatus", "unknown"),
        "service_exit_code": _normalize_exec_main_code(details.get("ExecMainCode")),
        "service_last_start": details.get("ExecMainStartTimestamp", ""),
        "service_last_exit": details.get("ExecMainExitTimestamp", ""),
        "service": SERVICE_NAME,
        "timer": TIMER_NAME,
    }


def managed_service_config_path() -> Path | None:
    service_path = _unit_directory(create=False) / SERVICE_NAME
    if not _is_managed_unit(service_path):
        return None
    try:
        text, _ = read_private_text(
            service_path,
            regular_label="systemd service",
            read_label="systemd service",
            max_bytes=MAX_UNIT_BYTES,
            too_large_label="systemd service",
            invalid_utf8_label="systemd service",
        )
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    for line in text.splitlines():
        if not line.startswith("ExecStart="):
            continue
        try:
            argv = shlex.split(line[len("ExecStart="):])
            config_index = argv.index("--config")
            return Path(argv[config_index + 1].replace("%%", "%")).expanduser().absolute()
        except (IndexError, RuntimeError, ValueError):
            return None
    return None


def _render_service(config: AppConfig, watchdog: Path, config_path: Path) -> str:
    data_root = _xdg_root("XDG_DATA_HOME", Path.home() / ".local" / "share")
    state_root = _xdg_root("XDG_STATE_HOME", Path.home() / ".local" / "state")
    integration_state = state_root / "codex-usage" / "integration"
    lock_root = private_io._private_lock_root()
    # The service is a publisher only. It has no collector, browser, profile,
    # auth, scheduler, or direct-refresh write authority.
    writable = [integration_state, lock_root]
    unique = sorted({str(path) for path in writable})
    read_write = "\n".join(f"ReadWritePaths={_unit_quote(path)}" for path in unique)
    exec_start = " ".join(
        _unit_quote(value)
        for value in (
            str(watchdog),
        )
    )
    return f"""[Unit]
Description=Watch ChatGPT Codex usage analytics
Documentation=https://github.com/H234598/codex-usage
{MANAGED_MARKER}

[Service]
Type=oneshot
ExecStart={exec_start}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONSAFEPATH=1
Environment=PYTHONNOUSERSITE=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment={_unit_quote(f"XDG_DATA_HOME={data_root}")}
Environment={_unit_quote(f"XDG_STATE_HOME={state_root}")}
UnsetEnvironment={" ".join(SERVICE_RUNTIME_UNSET_ENVIRONMENT_NAMES)}
TimeoutStartSec={INTEGRATION_WATCHDOG_SYSTEMD_TIMEOUT_SECONDS}
TimeoutStopSec=15
KillMode=mixed
MemoryMax=1G
TasksMax=256
OOMPolicy=kill
Restart=no
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectClock=true
ProtectHostname=true
ProtectSystem=strict
ProtectHome=read-only
{read_write}
RestrictSUIDSGID=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
LockPersonality=true
SystemCallArchitectures=native

[Install]
WantedBy=default.target
"""


def _render_timer(interval_seconds: int) -> str:
    return f"""[Unit]
Description=Run ChatGPT Codex usage poll periodically
Documentation=https://github.com/H234598/codex-usage
{MANAGED_MARKER}

[Timer]
OnActiveSec=1min
OnUnitActiveSec={interval_seconds}s
AccuracySec=30s
Persistent=true
Unit={SERVICE_NAME}

[Install]
WantedBy=timers.target
"""


def _service_runtime_root() -> Path:
    data_root = _xdg_root("XDG_DATA_HOME", Path.home() / ".local" / "share")
    return data_root / SERVICE_RUNTIME_V2_DIRECTORY_NAME


def _require_system_python_for_service_runtime() -> None:
    try:
        interpreter = Path(sys.executable).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ServiceError("service runtime builder system Python is unavailable") from exc
    try:
        if not interpreter.is_relative_to(Path("/usr/bin")):
            raise ServiceError("service runtime builder must use a system Python")
    except ValueError as exc:
        raise ServiceError("service runtime builder must use a system Python") from exc
    _read_bound_regular_file(
        interpreter,
        label="service runtime builder system Python",
        max_bytes=INTERPRETER_MAX_BYTES,
        executable=True,
        single_link=True,
    )


def _private_runtime_directory(path: Path, *, label: str) -> _DirectoryBinding:
    binding = _read_bound_directory(path, label=label)
    if binding.uid != os.geteuid() or stat.S_IMODE(binding.mode) != 0o700:
        raise ServiceError(f"{label} must be a private directory")
    return binding


def _same_runtime_directory(
    actual: _DirectoryBinding,
    expected: _DirectoryBinding,
) -> bool:
    return (
        actual.device,
        actual.inode,
        actual.mode,
        actual.uid,
        actual.gid,
        actual.nlink,
    ) == (
        expected.device,
        expected.inode,
        expected.mode,
        expected.uid,
        expected.gid,
        expected.nlink,
    )


def _runtime_path_for_record(
    *,
    site_packages: Path,
    bin_directory: Path,
    relative_path: str,
) -> Path:
    _validate_record_relative_path(relative_path)
    parts = PurePosixPath(relative_path).parts
    if ".." not in parts:
        return site_packages / Path(relative_path)
    if parts[:4] != ("..", "..", "..", "bin") or len(parts) != 5:
        raise ServiceError("codex-usage distribution RECORD is invalid")
    executable = parts[-1]
    if executable not in {
        "codex-usage",
        BROWSER_EXECUTABLE_NAME,
        INTEGRATION_WATCHDOG_EXECUTABLE_NAME,
    }:
        raise ServiceError("codex-usage distribution RECORD is invalid")
    return bin_directory / executable


def _runtime_file_payloads(
    codex_usage_executable: Path,
) -> tuple[tuple[str, bytes], ...]:
    expected = _RESOLVED_INTEGRATION_WATCHDOG_BINDINGS.get(codex_usage_executable)
    if expected is None:
        raise ServiceError("integration watchdog executable was not resolved before runtime build")
    try:
        codex_usage = _read_bound_console_script(
            expected[0].path,
            expected_module="codex_usage.cli",
            label="codex-usage executable",
        )
        watchdog = _read_bound_console_script(
            expected[1].path,
            expected_module="codex_usage.integration_watchdog",
            label="integration watchdog executable",
        )
        current = _bind_console_scripts_to_distribution(codex_usage, watchdog)
    except ServiceError as exc:
        raise ServiceError("codex-usage distribution changed before runtime build") from exc
    if current != expected or current[0].distribution is None:
        raise ServiceError("codex-usage distribution changed before runtime build")
    distribution = current[0].distribution
    record_entries = _parse_record(distribution.record.payload)
    if len(record_entries) > MAX_SERVICE_RUNTIME_FILES:
        raise ServiceError("codex-usage distribution RECORD is too large")
    try:
        metadata_root = distribution.metadata.path.parent.name
        source_site_packages = distribution.metadata.path.parents[1]
    except IndexError as exc:
        raise ServiceError("codex-usage distribution path is invalid") from exc
    if not metadata_root.endswith(".dist-info"):
        raise ServiceError("codex-usage distribution path is invalid")
    record_relative = f"{metadata_root}/RECORD"
    _verify_record_selfrow(record_entries, record_relative)
    try:
        source_distribution = importlib_metadata.distribution(EXPECTED_DISTRIBUTION_NAME)
    except Exception as exc:
        raise ServiceError("codex-usage distribution is unavailable") from exc

    core_only_modules = set(TRUSTED_CORE_MODULES) - set(PRODUCER_RELEASE_MODULES)
    selected = {
        *(f"codex_usage/{name}" for name in core_only_modules),
        f"{metadata_root}/METADATA",
    }
    if not selected <= set(record_entries):
        raise ServiceError("codex-usage distribution is missing publisher Core modules")
    state_home = _xdg_root("XDG_STATE_HOME", Path.home() / ".local" / "state")
    data_home = _xdg_root("XDG_DATA_HOME", Path.home() / ".local" / "share")
    try:
        candidate = _active_entrypoint_candidate_from_active_manifest(
            state_home=state_home,
            data_home=data_home,
        )
        active = verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=candidate,
        )
    except Exception as exc:
        raise ServiceError("active integration producer is unavailable") from exc
    active_package = active.active_release.entrypoint_path.parent
    payloads: list[tuple[str, bytes]] = []
    source_bin = current[0].path.parent
    producer_bindings: dict[str, _RegularFileBinding] = {}
    for module_name in PRODUCER_RELEASE_MODULES:
        binding = _read_bound_regular_file(
            active_package / module_name,
            label="active integration producer module",
            max_bytes=PACKAGE_RECORD_MAX_BYTES,
            single_link=True,
        )
        producer_bindings[module_name] = binding
        payloads.append((f"codex_usage/{module_name}", binding.payload))
    for relative_path in sorted(selected):
        source_path = _distribution_file_path(source_distribution, relative_path)
        expected_source_path = _runtime_path_for_record(
            site_packages=source_site_packages,
            bin_directory=source_bin,
            relative_path=relative_path,
        )
        if source_path != expected_source_path:
            raise ServiceError("codex-usage distribution path is invalid")
        binding = _read_bound_regular_file(
            source_path,
            label="codex-usage distribution file",
            max_bytes=PACKAGE_RECORD_MAX_BYTES,
            single_link=True,
        )
        _verify_recorded_file(
            record_entries,
            relative_path,
            binding,
            label="runtime file",
        )
        payloads.append((relative_path, binding.payload))
    try:
        repeated = verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=candidate,
        )
    except Exception as exc:
        raise ServiceError("active integration producer changed during runtime build") from exc
    if repeated != active:
        raise ServiceError("active integration producer changed during runtime build")
    for module_name, expected_binding in producer_bindings.items():
        current_binding = _read_bound_regular_file(
            active_package / module_name,
            label="active integration producer module",
            max_bytes=PACKAGE_RECORD_MAX_BYTES,
            single_link=True,
        )
        if current_binding != expected_binding:
            raise ServiceError("active integration producer changed during runtime build")
    private_record_relative = f"{metadata_root}/RECORD"
    private_record = "".join(
        f"{relative},{_pep376_sha256(payload)},{len(payload)}\n"
        for relative, payload in payloads
    ) + f"{private_record_relative},,\n"
    payloads.append((private_record_relative, private_record.encode("utf-8")))
    return tuple(payloads)


def _runtime_write_file(path: Path, payload: bytes, *, mode: int) -> None:
    ensure_private_directory(path.parent, label="service runtime directory")
    parent_fd = -1
    try:
        parent_fd = os.open(path.parent, private_io._directory_open_flags())
        parent = _private_runtime_directory(path.parent, label="service runtime directory")
        opened = _directory_binding_from_fd(
            path.parent,
            parent_fd,
            label="service runtime directory",
        )
        if opened != parent:
            raise ServiceError("service runtime directory changed during write")
        private_io.write_private_bytes_at(parent_fd, path.name, payload, mode=mode)
        os.fsync(parent_fd)
    except ServiceError:
        raise
    except (OSError, ValueError) as exc:
        raise ServiceError("could not write service runtime") from exc
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _runtime_watchdog_payload(root: Path) -> bytes:
    interpreter = root / SERVICE_RUNTIME_V2_CURRENT_NAME / "venv" / "bin" / "python"
    rendered = str(interpreter)
    if not rendered or any(character in rendered for character in "\x00\n\r"):
        raise ServiceError("service runtime interpreter path is invalid")
    return (
        "#!/bin/sh\n"
        f"exec {shlex.quote(rendered)} -I -B -m codex_usage.integration_watchdog \"$@\"\n"
    ).encode()


def _create_runtime_staging(root: Path) -> Path:
    root_binding = _private_runtime_directory(root, label="service runtime root")
    flags = private_io._directory_open_flags()
    parent_fd = -1
    try:
        parent_fd = os.open(root, flags)
        if (
            _directory_binding_from_fd(root, parent_fd, label="service runtime root")
            != root_binding
        ):
            raise ServiceError("service runtime root changed during staging")
        for _ in range(8):
            name = SERVICE_RUNTIME_V2_STAGING_PREFIX + secrets.token_hex(16)
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            staging_fd = -1
            try:
                staging_fd = os.open(name, flags, dir_fd=parent_fd)
                os.fchmod(staging_fd, 0o700)
                _private_runtime_directory(
                    root / name,
                    label="service runtime staging generation",
                )
                os.fsync(parent_fd)
                return root / name
            finally:
                if staging_fd >= 0:
                    os.close(staging_fd)
    except ServiceError:
        raise
    except OSError as exc:
        raise ServiceError("could not create service runtime staging generation") from exc
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
    raise ServiceError("could not allocate service runtime staging generation")


def _private_runtime_file(
    path: Path,
    *,
    label: str,
    mode: int,
    executable: bool = False,
) -> _RegularFileBinding:
    binding = _read_bound_regular_file(
        path,
        label=label,
        max_bytes=PACKAGE_RECORD_MAX_BYTES,
        executable=executable,
        single_link=True,
    )
    if binding.uid != os.geteuid() or stat.S_IMODE(binding.mode) != mode:
        raise ServiceError(f"{label} must be private")
    return binding


def _service_runtime_binding(root: Path, generation: Path) -> _ServiceRuntimeBinding:
    root_binding = _private_runtime_directory(root, label="service runtime root")
    generation_binding = _private_runtime_directory(
        generation,
        label="service runtime generation",
    )
    if generation.parent != root:
        raise ServiceError("service runtime generation path is invalid")
    venv = generation / "venv"
    _private_runtime_directory(venv, label="service runtime venv")
    bin_directory = venv / "bin"
    _private_runtime_directory(bin_directory, label="service runtime bin directory")
    interpreter = _private_runtime_file(
        bin_directory / "python",
        label="service runtime interpreter",
        mode=0o700,
        executable=True,
    )
    pyvenv = _private_runtime_file(
        venv / "pyvenv.cfg",
        label="service runtime pyvenv configuration",
        mode=0o600,
    )
    try:
        pyvenv_text = pyvenv.payload.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ServiceError("service runtime pyvenv configuration is invalid") from exc
    pyvenv_rows = [line.strip() for line in pyvenv_text.splitlines() if line.strip()]
    if pyvenv_rows.count("include-system-site-packages = false") != 1:
        raise ServiceError("service runtime pyvenv configuration is invalid")
    version_directory = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = venv / "lib" / version_directory / "site-packages"
    _private_runtime_directory(site_packages, label="service runtime site-packages")
    metadata_root = f"codex_usage-{EXPECTED_DISTRIBUTION_VERSION}.dist-info"
    metadata = _private_runtime_file(
        site_packages / metadata_root / "METADATA",
        label="service runtime METADATA",
        mode=0o600,
    )
    headers = _metadata_headers(metadata.payload)
    if (
        _normalize_distribution_name(headers.get("name", ""))
        != EXPECTED_DISTRIBUTION_NAME
        or headers.get("version") != EXPECTED_DISTRIBUTION_VERSION
    ):
        raise ServiceError("service runtime METADATA is invalid")
    record = _private_runtime_file(
        site_packages / metadata_root / "RECORD",
        label="service runtime RECORD",
        mode=0o600,
    )
    record_entries = _parse_record(record.payload)
    if len(record_entries) > MAX_SERVICE_RUNTIME_FILES:
        raise ServiceError("service runtime RECORD is too large")
    record_relative = f"{metadata_root}/RECORD"
    _verify_record_selfrow(record_entries, record_relative)
    modules: list[_RegularFileBinding] = []
    for relative_path in sorted(record_entries):
        path = _runtime_path_for_record(
            site_packages=site_packages,
            bin_directory=bin_directory,
            relative_path=relative_path,
        )
        file_binding = _private_runtime_file(
            path,
            label="service runtime distribution file",
            mode=0o600,
        )
        if relative_path != record_relative:
            _verify_recorded_file(
                record_entries,
                relative_path,
                file_binding,
                label="service runtime distribution file",
            )
        if relative_path.startswith("codex_usage/") and relative_path.endswith(".py"):
            modules.append(file_binding)
    required_modules = {f"codex_usage/{name}" for name in TRUSTED_CORE_MODULES}
    if not required_modules <= set(record_entries) or not modules:
        raise ServiceError("service runtime RECORD is missing Core modules")
    watchdog = _private_runtime_file(
        generation / "bin" / SERVICE_RUNTIME_V2_WATCHDOG_NAME,
        label="service runtime watchdog",
        mode=0o700,
        executable=True,
    )
    if watchdog.payload != _runtime_watchdog_payload(root):
        raise ServiceError("service runtime watchdog is invalid")
    return _ServiceRuntimeBinding(
        root=root_binding,
        generation=generation_binding,
        interpreter=interpreter,
        watchdog=watchdog,
        pyvenv=pyvenv,
        distribution=_DistributionBinding(
            version=EXPECTED_DISTRIBUTION_VERSION,
            metadata=metadata,
            record=record,
            modules=tuple(modules),
        ),
    )


def _materialize_service_runtime(
    codex_usage_executable: Path,
    *,
    before_cutover: Callable[
        [_ServiceRuntimeBinding, _ServiceRuntimeBinding | None, Path], None
    ]
    | None = None,
) -> _ServiceRuntimePublication:
    _require_system_python_for_service_runtime()
    root = _service_runtime_root()
    ensure_private_directory(root, label="service runtime root")
    _private_runtime_directory(root, label="service runtime root")
    payloads = _runtime_file_payloads(codex_usage_executable)
    staging = _create_runtime_staging(root)
    try:
        venv = staging / "venv"
        integration_installer._create_private_virtualenv(venv)
        integration_installer._remove_activation_files(venv)
        ensure_private_directory(venv, label="service runtime venv")
        bin_directory = venv / "bin"
        ensure_private_directory(bin_directory, label="service runtime bin directory")
        interpreter = bin_directory / "python"
        os.chmod(interpreter, 0o700)
        os.chmod(venv / "pyvenv.cfg", 0o600)
        site_packages, _site_identity = integration_installer._find_site_packages(
            venv,
            integration_installer._directory_identity(venv),
        )
        ensure_private_directory(site_packages, label="service runtime site-packages")
        for relative_path, payload in payloads:
            target = _runtime_path_for_record(
                site_packages=site_packages,
                bin_directory=bin_directory,
                relative_path=relative_path,
            )
            _runtime_write_file(target, payload, mode=0o600)
        runtime_bin = staging / "bin"
        ensure_private_directory(runtime_bin, label="service runtime watchdog directory")
        _runtime_write_file(
            runtime_bin / SERVICE_RUNTIME_V2_WATCHDOG_NAME,
            _runtime_watchdog_payload(root),
            mode=0o700,
        )
        _service_runtime_binding(root, staging)
        if payloads != _runtime_file_payloads(codex_usage_executable):
            raise ServiceError("codex-usage distribution changed during runtime build")
        current = root / SERVICE_RUNTIME_V2_CURRENT_NAME
        prior: Path | None = None
        prior_identity: object | None = None
        old_binding: _ServiceRuntimeBinding | None = None
        staging_binding = _service_runtime_binding(root, staging)
        if current.exists() or current.is_symlink():
            old_binding = _service_runtime_binding(root, current)
        if before_cutover is not None:
            before_cutover(staging_binding, old_binding, staging)
        if old_binding is not None:
            parent_fd = os.open(root, private_io._directory_open_flags())
            try:
                integration_installer._rename_exchange(staging.name, current.name, parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            prior = staging
            prior_identity = integration_installer._provisional_path_identity(
                prior,
                directory=True,
            )
        else:
            parent_fd = os.open(root, private_io._directory_open_flags())
            try:
                integration_installer._rename_noreplace(staging.name, current.name, parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        publication = _ServiceRuntimePublication(
            binding=_service_runtime_binding(root, current),
            rollback_path=prior,
            rollback_identity=prior_identity,
            source_payloads=payloads,
        )
        try:
            _after_service_runtime_cutover(publication)
        except BaseException as primary_error:
            try:
                _rollback_service_runtime(publication)
            except BaseException as rollback_error:
                _raise_service_rollback_error(
                    "service runtime cutover",
                    primary_error,
                    [rollback_error],
                )
            raise
        return publication
    except BaseException:
        try:
            if staging.exists() and not staging.is_symlink():
                identity = integration_installer._provisional_path_identity(
                    staging,
                    directory=True,
                )
                root_identity = integration_installer._directory_identity(root)
                integration_installer._remove_owned_entry(
                    staging,
                    identity,
                    root_identity,
                    directory=True,
                    recursive=True,
                )
        except BaseException:
            pass
        raise


def _after_service_runtime_cutover(_runtime: _ServiceRuntimePublication) -> None:
    """Test seam for the post-rename attestation boundary."""
    return None


def _revalidate_service_runtime_for_unit_write(
    runtime: _ServiceRuntimePublication,
) -> None:
    current = _service_runtime_binding(
        _service_runtime_root(),
        _service_runtime_root() / SERVICE_RUNTIME_V2_CURRENT_NAME,
    )
    if current != runtime.binding:
        raise ServiceError("service runtime changed before unit write")


def _revalidate_active_producer_for_unit_write(
    runtime: _ServiceRuntimePublication,
    codex_usage_executable: Path,
) -> None:
    if runtime.source_payloads != _runtime_file_payloads(codex_usage_executable):
        raise ServiceError("active integration producer changed before unit write")


def _rollback_service_runtime(runtime: _ServiceRuntimePublication) -> None:
    root = _service_runtime_root()
    current = root / SERVICE_RUNTIME_V2_CURRENT_NAME
    if runtime.rollback_path is not None and runtime.rollback_identity is not None:
        if _private_runtime_directory(root, label="service runtime root") != runtime.binding.root:
            raise ServiceError("service runtime changed before rollback")
        if _service_runtime_binding(root, current) != runtime.binding:
            raise ServiceError("service runtime changed before rollback")
        if (
            integration_installer._provisional_path_identity(
                runtime.rollback_path,
                directory=True,
            )
            != runtime.rollback_identity
        ):
            raise ServiceError("service runtime changed before rollback")
        parent_fd = os.open(root, private_io._directory_open_flags())
        try:
            integration_installer._rename_exchange(
                current.name,
                runtime.rollback_path.name,
                parent_fd,
            )
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        discarded = _private_runtime_directory(
            runtime.rollback_path,
            label="failed service runtime generation",
        )
        if not _same_runtime_directory(discarded, runtime.binding.generation):
            raise ServiceError("service runtime changed during rollback")
        discarded_identity = integration_installer._provisional_path_identity(
            runtime.rollback_path,
            directory=True,
        )
        if not integration_installer._remove_owned_entry(
            runtime.rollback_path,
            discarded_identity,
            integration_installer._directory_identity(root),
            directory=True,
            recursive=True,
        ):
            raise ServiceError("could not discard failed service runtime generation")
        return
    if not _same_runtime_directory(
        _private_runtime_directory(current, label="service runtime generation"),
        runtime.binding.generation,
    ):
        raise ServiceError("service runtime changed before rollback")
    identity = integration_installer._provisional_path_identity(current, directory=True)
    if not integration_installer._remove_owned_entry(
        current,
        identity,
        integration_installer._directory_identity(root),
        directory=True,
        recursive=True,
    ):
        raise ServiceError("could not discard failed service runtime generation")


def _commit_service_runtime(runtime: _ServiceRuntimePublication) -> None:
    if runtime.rollback_path is None or runtime.rollback_identity is None:
        return
    root = _service_runtime_root()
    try:
        integration_installer._remove_owned_entry(
            runtime.rollback_path,
            runtime.rollback_identity,
            integration_installer._directory_identity(root),
            directory=True,
            recursive=True,
        )
    except BaseException:
        return


def _unit_directory(*, create: bool = True) -> Path:
    root = _xdg_root("XDG_CONFIG_HOME", Path.home() / ".config")
    path = root / "systemd" / "user"
    _assert_no_symlink_ancestors(path.parent)
    if create:
        _ensure_systemd_unit_directory(path)
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ServiceError("systemd user unit directory must be a real directory")
    if not path.exists():
        return path
    if not create:
        try:
            _ensure_systemd_unit_directory(path)
        except (OSError, ValueError) as exc:
            raise ServiceError("could not secure systemd user unit directory") from exc
    return path


def _ensure_systemd_unit_directory(path: Path) -> None:
    try:
        ensure_private_directory(path, label="systemd user unit directory")
        return
    except ValueError:
        if not path.exists() or path.is_symlink() or not path.is_dir():
            raise
    private_io._chmod_private_directory(path, label="systemd user unit directory")
    ensure_private_directory(path, label="systemd user unit directory")


def _assert_no_symlink_ancestors(path: Path) -> None:
    raw_path = Path(path)
    absolute = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        if part == ".":
            continue
        if part == "..":
            current = current.parent
            continue
        current /= part
        if current.is_symlink():
            raise ServiceError("systemd user unit path must not contain symlinks")


def _resolve_codex_usage() -> Path:
    executable = shutil.which("codex-usage")
    if not executable:
        raise ServiceError("codex-usage executable was not found")
    path = Path(executable).absolute()
    codex_binding = _read_bound_console_script(
        path,
        expected_module="codex_usage.cli",
        label="codex-usage executable",
    )
    wrapper = _resolve_integration_watchdog_executable(path)
    codex_binding, wrapper = _bind_console_scripts_to_distribution(
        codex_binding,
        wrapper,
    )
    _RESOLVED_INTEGRATION_WATCHDOG_BINDINGS[path] = (codex_binding, wrapper)
    return path


def _resolve_integration_watchdog_executable(
    codex_usage_executable: Path,
) -> _ExecutableBinding:
    expected = codex_usage_executable.with_name(
        INTEGRATION_WATCHDOG_EXECUTABLE_NAME
    ).absolute()
    resolved = shutil.which(INTEGRATION_WATCHDOG_EXECUTABLE_NAME)
    if not resolved:
        raise ServiceError("integration watchdog executable was not found")
    path = Path(resolved).absolute()
    if path != expected:
        raise ServiceError(
            "integration watchdog executable must be installed beside codex-usage"
        )
    return _read_bound_console_script(
        path,
        expected_module="codex_usage.integration_watchdog",
        label="integration watchdog executable",
    )


def _normalize_distribution_name(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _normalized_path(path: object) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _pep376_sha256(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode("ascii")
    return "sha256=" + digest.rstrip("=")


def _read_bound_regular_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    executable: bool = False,
    single_link: bool = False,
) -> _RegularFileBinding:
    _assert_no_symlink_ancestors(path.parent)
    fd = -1
    try:
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        initial = os.fstat(fd)
        mode = stat.S_IMODE(initial.st_mode)
        if not stat.S_ISREG(initial.st_mode):
            raise ServiceError(f"{label} must be a regular executable")
        if initial.st_uid not in {0, os.geteuid()}:
            raise ServiceError(f"{label} owner is not trusted")
        if single_link and initial.st_nlink != 1:
            raise ServiceError(f"{label} must be single-linked")
        if mode & 0o022:
            raise ServiceError(f"{label} is writable by an untrusted account")
        if executable and not mode & 0o100:
            raise ServiceError(f"{label} is not executable")
        if initial.st_size <= 0 or initial.st_size > max_bytes:
            raise ServiceError(f"{label} size is invalid")
        payload = bytearray()
        while len(payload) <= max_bytes:
            chunk = os.read(fd, min(65_536, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > max_bytes:
            raise ServiceError(f"{label} size is invalid")
        final = os.fstat(fd)
        if (
            final.st_dev != initial.st_dev
            or final.st_ino != initial.st_ino
            or final.st_mode != initial.st_mode
            or final.st_uid != initial.st_uid
            or final.st_gid != initial.st_gid
            or final.st_nlink != initial.st_nlink
            or final.st_size != initial.st_size
            or final.st_mtime_ns != initial.st_mtime_ns
            or final.st_ctime_ns != initial.st_ctime_ns
        ):
            raise ServiceError(f"{label} changed during read")
    except ServiceError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO, errno.ENOTDIR):
            raise ServiceError(f"{label} must be a regular executable") from exc
        raise ServiceError(f"{label} is unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    payload_bytes = bytes(payload)
    return _RegularFileBinding(
        path=path,
        device=initial.st_dev,
        inode=initial.st_ino,
        mode=initial.st_mode,
        uid=initial.st_uid,
        gid=initial.st_gid,
        nlink=initial.st_nlink,
        size=initial.st_size,
        mtime_ns=initial.st_mtime_ns,
        ctime_ns=initial.st_ctime_ns,
        payload=payload_bytes,
        sha256=hashlib.sha256(payload_bytes).hexdigest(),
    )


def _read_bound_regular_file_at(
    parent_fd: int,
    name: str,
    path: Path,
    *,
    label: str,
    max_bytes: int,
) -> _RegularFileBinding:
    fd = -1
    try:
        fd = os.open(
            _safe_unit_component(name),
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        initial = os.fstat(fd)
        mode = stat.S_IMODE(initial.st_mode)
        if not stat.S_ISREG(initial.st_mode):
            raise ServiceError(f"{label} must be a regular executable")
        if initial.st_uid not in {0, os.geteuid()}:
            raise ServiceError(f"{label} owner is not trusted")
        if mode & 0o022:
            raise ServiceError(f"{label} is writable by an untrusted account")
        if initial.st_size <= 0 or initial.st_size > max_bytes:
            raise ServiceError(f"{label} size is invalid")
        payload = bytearray()
        while len(payload) <= max_bytes:
            chunk = os.read(fd, min(65_536, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > max_bytes:
            raise ServiceError(f"{label} size is invalid")
        final = os.fstat(fd)
        if (
            final.st_dev != initial.st_dev
            or final.st_ino != initial.st_ino
            or final.st_mode != initial.st_mode
            or final.st_uid != initial.st_uid
            or final.st_gid != initial.st_gid
            or final.st_nlink != initial.st_nlink
            or final.st_size != initial.st_size
            or final.st_mtime_ns != initial.st_mtime_ns
            or final.st_ctime_ns != initial.st_ctime_ns
        ):
            raise ServiceError(f"{label} changed during attestation")
    except ServiceError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO, errno.ENOTDIR):
            raise ServiceError(f"{label} must be a regular executable") from exc
        raise ServiceError(f"{label} is unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    payload_bytes = bytes(payload)
    return _RegularFileBinding(
        path=path,
        device=initial.st_dev,
        inode=initial.st_ino,
        mode=initial.st_mode,
        uid=initial.st_uid,
        gid=initial.st_gid,
        nlink=initial.st_nlink,
        size=initial.st_size,
        mtime_ns=initial.st_mtime_ns,
        ctime_ns=initial.st_ctime_ns,
        payload=payload_bytes,
        sha256=hashlib.sha256(payload_bytes).hexdigest(),
    )


def _directory_binding_from_fd(path: Path, fd: int, *, label: str) -> _DirectoryBinding:
    item = os.fstat(fd)
    mode = stat.S_IMODE(item.st_mode)
    if not stat.S_ISDIR(item.st_mode):
        raise ServiceError(f"{label} is unavailable")
    if item.st_uid not in {0, os.geteuid()}:
        raise ServiceError(f"{label} owner is not trusted")
    if mode & 0o022:
        raise ServiceError(f"{label} is writable by an untrusted account")
    if item.st_nlink < 1:
        raise ServiceError(f"{label} link count is invalid")
    return _DirectoryBinding(
        path=path,
        device=item.st_dev,
        inode=item.st_ino,
        mode=item.st_mode,
        uid=item.st_uid,
        gid=item.st_gid,
        nlink=item.st_nlink,
        mtime_ns=item.st_mtime_ns,
        ctime_ns=item.st_ctime_ns,
    )


def _read_bound_directory(path: Path, *, label: str) -> _DirectoryBinding:
    _assert_no_symlink_ancestors(path)
    fd = -1
    try:
        fd = os.open(path, private_io._directory_open_flags())
        binding = _directory_binding_from_fd(path, fd, label=label)
        final = _directory_binding_from_fd(path, fd, label=label)
        if final != binding:
            raise ServiceError(f"{label} changed during attestation")
        return binding
    except ServiceError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ServiceError(f"{label} is unsafe") from exc
        raise ServiceError(f"{label} is unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _console_script_interpreter(
    payload: bytes,
    *,
    label: str,
) -> _RegularFileBinding:
    first_line = payload.splitlines()[0] if payload.splitlines() else b""
    if not first_line.startswith(b"#!"):
        raise ServiceError(f"{label} has unexpected entry point")
    try:
        tokens = shlex.split(first_line[2:].decode("utf-8", "strict").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise ServiceError(f"{label} interpreter is invalid") from exc
    if len(tokens) != 1:
        raise ServiceError(f"{label} interpreter is invalid")
    actual = _normalized_path(tokens[0])
    expected = _normalized_path(sys.executable)
    if actual != expected:
        raise ServiceError(f"{label} interpreter is not trusted")
    try:
        interpreter_path = expected.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ServiceError(f"{label} interpreter is unavailable") from exc
    return _read_bound_regular_file(
        interpreter_path,
        label=f"{label} interpreter",
        max_bytes=INTERPRETER_MAX_BYTES,
        executable=True,
        single_link=True,
    )


def _console_script_imports_module(payload: bytes, expected_module: str) -> bool:
    try:
        text = payload.decode("utf-8", "strict")
        tree = ast.parse(text)
    except (SyntaxError, UnicodeDecodeError):
        return False
    statements = tuple(tree.body)
    if statements and isinstance(statements[0], ast.Expr) and isinstance(
        statements[0].value,
        ast.Constant,
    ) and isinstance(statements[0].value.value, str):
        statements = statements[1:]
    sys_import_seen = False
    expected_import_seen = False
    main_call_seen = False
    for statement in statements:
        if (
            isinstance(statement, ast.Import)
            and len(statement.names) == 1
            and statement.names[0].name == "sys"
            and statement.names[0].asname is None
        ):
            if sys_import_seen or expected_import_seen or main_call_seen:
                return False
            sys_import_seen = True
            continue
        if _is_expected_console_main_import(statement, expected_module):
            if expected_import_seen or main_call_seen:
                return False
            expected_import_seen = True
            continue
        if _binds_console_main(statement):
            return False
        if _is_console_script_main_guard(statement):
            if main_call_seen or not expected_import_seen:
                return False
            main_call_seen = _console_main_guard_calls_main(statement)
            continue
        return False
    return expected_import_seen and main_call_seen


def _is_expected_console_main_import(
    statement: ast.stmt,
    expected_module: str,
) -> bool:
    return (
        isinstance(statement, ast.ImportFrom)
        and statement.level == 0
        and statement.module == expected_module
        and len(statement.names) == 1
        and statement.names[0].name == "main"
        and statement.names[0].asname is None
    )


def _binds_console_main(statement: ast.stmt) -> bool:
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return statement.name == "main"
    if isinstance(statement, ast.ImportFrom):
        return any((alias.asname or alias.name) == "main" for alias in statement.names)
    if isinstance(statement, ast.Import):
        return any(
            (alias.asname or alias.name.partition(".")[0]) == "main"
            for alias in statement.names
        )
    if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets: list[ast.expr] = []
        if isinstance(statement, ast.Assign):
            targets.extend(statement.targets)
        else:
            targets.append(statement.target)
        return any(_target_binds_name(target, "main") for target in targets)
    return False


def _target_binds_name(target: ast.expr, name: str) -> bool:
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_target_binds_name(item, name) for item in target.elts)
    return False


def _is_console_script_main_guard(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.If):
        return False
    test = statement.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    left, comparator = test.left, test.comparators[0]
    if not isinstance(test.ops[0], ast.Eq):
        return False
    return (
        isinstance(left, ast.Name)
        and left.id == "__name__"
        and isinstance(comparator, ast.Constant)
        and comparator.value == "__main__"
    )


def _console_main_guard_calls_main(statement: ast.If) -> bool:
    if statement.orelse:
        return False
    body = statement.body
    if len(body) == 2:
        if not _is_pip_261_argv0_normalization(body[0]):
            return False
        body = body[1:]
    if len(body) != 1:
        return False
    guarded = body[0]
    if isinstance(guarded, ast.Expr) and isinstance(guarded.value, ast.Call):
        return _is_allowed_console_main_exit_call(guarded.value)
    return _is_allowed_console_main_system_exit_raise(guarded)


def _is_pip_261_argv0_normalization(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        return False
    if not _is_sys_argv_zero(statement.targets[0]):
        return False
    value = statement.value
    return (
        isinstance(value, ast.Call)
        and not value.keywords
        and len(value.args) == 1
        and isinstance(value.args[0], ast.Constant)
        and type(value.args[0].value) is str
        and value.args[0].value == ".exe"
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "removesuffix"
        and _is_sys_argv_zero(value.func.value)
    )


def _is_sys_argv_zero(expression: ast.expr) -> bool:
    return (
        isinstance(expression, ast.Subscript)
        and isinstance(expression.value, ast.Attribute)
        and isinstance(expression.value.value, ast.Name)
        and expression.value.value.id == "sys"
        and expression.value.attr == "argv"
        and isinstance(expression.slice, ast.Constant)
        and type(expression.slice.value) is int
        and expression.slice.value == 0
    )


def _is_allowed_console_main_exit_call(call: ast.Call) -> bool:
    return _is_direct_console_main_call(call) or _is_sys_exit_console_main_call(call)


def _is_allowed_console_main_system_exit_raise(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Raise)
        and statement.cause is None
        and isinstance(statement.exc, ast.Call)
        and isinstance(statement.exc.func, ast.Name)
        and statement.exc.func.id == "SystemExit"
        and not statement.exc.keywords
        and len(statement.exc.args) == 1
        and isinstance(statement.exc.args[0], ast.Call)
        and _is_direct_console_main_call(statement.exc.args[0])
    )


def _is_direct_console_main_call(call: ast.Call) -> bool:
    if isinstance(call.func, ast.Name) and call.func.id == "main":
        return not call.args and not call.keywords
    return False


def _is_sys_exit_console_main_call(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "exit"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "sys"
        and not call.keywords
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Call)
        and _is_direct_console_main_call(call.args[0])
    )


def _read_bound_console_script(
    path: Path,
    *,
    expected_module: str,
    label: str,
) -> _ExecutableBinding:
    binding = _read_bound_regular_file(
        path,
        label=label,
        max_bytes=EXECUTABLE_SCRIPT_MAX_BYTES,
        executable=True,
        single_link=True,
    )
    interpreter = _console_script_interpreter(binding.payload, label=label)
    if not _console_script_imports_module(binding.payload, expected_module):
        raise ServiceError(f"{label} has unexpected entry point")
    return _ExecutableBinding(
        path=path,
        device=binding.device,
        inode=binding.inode,
        mode=binding.mode,
        uid=binding.uid,
        gid=binding.gid,
        nlink=binding.nlink,
        size=binding.size,
        mtime_ns=binding.mtime_ns,
        ctime_ns=binding.ctime_ns,
        payload=binding.payload,
        sha256=binding.sha256,
        interpreter=interpreter,
        expected_module=expected_module,
    )


def _distribution_file_path(distribution: object, relative_path: str) -> Path:
    try:
        located = distribution.locate_file(relative_path)
    except Exception as exc:
        raise ServiceError("codex-usage distribution RECORD is unavailable") from exc
    return _normalized_path(located)


def _distribution_site_packages(distribution: object) -> _DirectoryBinding:
    site_packages = _distribution_file_path(distribution, "")
    return _read_bound_directory(site_packages, label="codex-usage distribution root")


def _scan_dist_info_roots(
    distribution: object,
    site_packages: Path,
    *,
    site_packages_binding: _DirectoryBinding | None = None,
) -> tuple[str, str, _RegularFileBinding, _RegularFileBinding]:
    flags = private_io._directory_open_flags()
    root_fd = -1
    candidates: list[tuple[str, _RegularFileBinding, _RegularFileBinding]] = []
    try:
        root_fd = os.open(site_packages, flags)
        if site_packages_binding is not None and _directory_binding_from_fd(
            site_packages,
            root_fd,
            label="codex-usage distribution root",
        ) != site_packages_binding:
            raise ServiceError("codex-usage distribution root changed during attestation")
        with os.scandir(root_fd) as names:
            for index, entry in enumerate(names):
                if index >= MAX_DISTRIBUTION_FILES:
                    raise ServiceError("codex-usage distribution RECORD is too large")
                name = entry.name
                if not name.endswith(".dist-info"):
                    continue
                dist_fd = -1
                try:
                    dist_fd = os.open(_safe_unit_component(name), flags, dir_fd=root_fd)
                    dist_item = os.fstat(dist_fd)
                    if not stat.S_ISDIR(dist_item.st_mode):
                        raise ServiceError("codex-usage dist-info root is unavailable")
                    metadata_relative = f"{name}/METADATA"
                    record_relative = f"{name}/RECORD"
                    metadata = _read_bound_regular_file_at(
                        dist_fd,
                        "METADATA",
                        site_packages / metadata_relative,
                        label="codex-usage METADATA",
                        max_bytes=PACKAGE_FILE_MAX_BYTES,
                    )
                    headers = _metadata_headers(metadata.payload)
                    if (
                        _normalize_distribution_name(headers.get("name", ""))
                        != EXPECTED_DISTRIBUTION_NAME
                        or headers.get("version") != EXPECTED_DISTRIBUTION_VERSION
                    ):
                        continue
                    record = _read_bound_regular_file_at(
                        dist_fd,
                        "RECORD",
                        site_packages / record_relative,
                        label="codex-usage RECORD",
                        max_bytes=PACKAGE_RECORD_MAX_BYTES,
                    )
                    _parse_record(record.payload)
                    candidates.append((name, metadata, record))
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise ServiceError("codex-usage dist-info root is unsafe") from exc
                    raise ServiceError("codex-usage dist-info root is unavailable") from exc
                finally:
                    if dist_fd >= 0:
                        os.close(dist_fd)
        if site_packages_binding is not None and _directory_binding_from_fd(
            site_packages,
            root_fd,
            label="codex-usage distribution root",
        ) != site_packages_binding:
            raise ServiceError("codex-usage distribution root changed during attestation")
    except ServiceError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ServiceError("codex-usage distribution root is unsafe") from exc
        raise ServiceError("codex-usage distribution root is unavailable") from exc
    finally:
        if root_fd >= 0:
            os.close(root_fd)
    if len(candidates) != 1:
        raise ServiceError("codex-usage distribution dist-info root is unavailable")
    root, metadata, record = sorted(candidates, key=lambda item: item[0])[0]
    return f"{root}/METADATA", f"{root}/RECORD", metadata, record


def _safe_unit_component(name: object) -> str:
    if (
        type(name) is not str
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise ServiceError("codex-usage distribution path is invalid")
    return name


def _distribution_relative_for_path(
    distribution: object,
    files: tuple[str, ...],
    path: Path,
    *,
    label: str,
) -> str:
    target = _normalized_path(path)
    matches = [
        file
        for file in files
        if not PurePath(file).parent.name.endswith(".dist-info")
        and _distribution_file_path(distribution, file) == target
    ]
    if len(matches) != 1:
        raise ServiceError(f"codex-usage distribution RECORD does not uniquely bind {label}")
    return matches[0]


def _metadata_headers(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ServiceError("codex-usage METADATA is invalid") from exc
    headers: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            break
        key, separator, value = line.partition(":")
        if not separator:
            continue
        normalized = key.strip().lower()
        if normalized in REPEATABLE_CORE_METADATA_FIELDS:
            continue
        if normalized in headers:
            raise ServiceError("codex-usage METADATA has duplicate fields")
        headers[normalized] = value.strip()
    return headers


def _parse_record(payload: bytes) -> dict[str, tuple[str, str]]:
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ServiceError("codex-usage distribution RECORD is invalid") from exc
    entries: dict[str, tuple[str, str]] = {}
    for row in csv.reader(text.splitlines()):
        if len(row) != 3 or not row[0]:
            raise ServiceError("codex-usage distribution RECORD is invalid")
        _validate_record_relative_path(row[0])
        if row[0] in entries:
            raise ServiceError("codex-usage distribution RECORD has duplicate paths")
        entries[row[0]] = (row[1], row[2])
    return entries


def _validate_record_relative_path(relative_path: str) -> None:
    if "\x00" in relative_path:
        raise ServiceError("codex-usage distribution RECORD is invalid")
    path = PurePosixPath(relative_path)
    if path.is_absolute() or str(path) != relative_path:
        raise ServiceError("codex-usage distribution RECORD is invalid")
    parts = path.parts
    if not parts or any(part in {"", "."} for part in parts):
        raise ServiceError("codex-usage distribution RECORD is invalid")
    if ".." not in parts:
        return
    allowed_scripts = {
        ("..", "..", "..", "bin", "codex-usage"),
        ("..", "..", "..", "bin", BROWSER_EXECUTABLE_NAME),
        ("..", "..", "..", "bin", INTEGRATION_WATCHDOG_EXECUTABLE_NAME),
    }
    if parts not in allowed_scripts:
        raise ServiceError("codex-usage distribution RECORD is invalid")


def _verify_record_selfrow(
    record_entries: dict[str, tuple[str, str]],
    record_relative: str,
) -> None:
    if record_entries.get(record_relative) != ("", ""):
        raise ServiceError("codex-usage distribution RECORD is missing RECORD")


def _verify_recorded_file(
    record_entries: dict[str, tuple[str, str]],
    relative_path: str,
    binding: _RegularFileBinding | _ExecutableBinding,
    *,
    label: str,
) -> None:
    entry = record_entries.get(relative_path)
    if entry is None:
        raise ServiceError(f"codex-usage distribution RECORD is missing {label}")
    expected_hash, expected_size = entry
    if expected_hash != _pep376_sha256(binding.payload) or expected_size != str(binding.size):
        raise ServiceError(f"codex-usage distribution RECORD does not match {label}")


def _read_distribution_file(
    distribution: object,
    relative_path: str,
    *,
    label: str,
    max_bytes: int = PACKAGE_FILE_MAX_BYTES,
) -> _RegularFileBinding:
    return _read_bound_regular_file(
        _distribution_file_path(distribution, relative_path),
        label=label,
        max_bytes=max_bytes,
    )


def _bind_console_scripts_to_distribution(
    codex_usage: _ExecutableBinding,
    integration_watchdog: _ExecutableBinding,
) -> tuple[_ExecutableBinding, _ExecutableBinding]:
    try:
        distribution = importlib_metadata.distribution(EXPECTED_DISTRIBUTION_NAME)
    except Exception as exc:
        raise ServiceError("codex-usage distribution is unavailable") from exc
    name = str(getattr(distribution, "metadata", {}).get("Name", ""))
    version = str(getattr(distribution, "version", ""))
    if _normalize_distribution_name(name) != EXPECTED_DISTRIBUTION_NAME:
        raise ServiceError("codex-usage distribution identity is invalid")
    if version != EXPECTED_DISTRIBUTION_VERSION:
        raise ServiceError(
            f"codex-usage distribution must be version {EXPECTED_DISTRIBUTION_VERSION}"
        )
    site_packages_binding = _distribution_site_packages(distribution)
    site_packages = site_packages_binding.path
    metadata_relative, record_relative, metadata, record = _scan_dist_info_roots(
        distribution,
        site_packages,
        site_packages_binding=site_packages_binding,
    )
    headers = _metadata_headers(metadata.payload)
    if _normalize_distribution_name(headers.get("name", "")) != EXPECTED_DISTRIBUTION_NAME:
        raise ServiceError("codex-usage METADATA identity is invalid")
    if headers.get("version") != EXPECTED_DISTRIBUTION_VERSION:
        raise ServiceError(
            f"codex-usage METADATA must be version {EXPECTED_DISTRIBUTION_VERSION}"
        )
    record_entries = _parse_record(record.payload)
    _verify_record_selfrow(record_entries, record_relative)
    files = tuple(record_entries)
    _verify_recorded_file(
        record_entries,
        metadata_relative,
        metadata,
        label="METADATA",
    )
    codex_relative = _distribution_relative_for_path(
        distribution,
        files,
        codex_usage.path,
        label="codex-usage executable",
    )
    watchdog_relative = _distribution_relative_for_path(
        distribution,
        files,
        integration_watchdog.path,
        label="integration watchdog executable",
    )
    _verify_recorded_file(
        record_entries,
        codex_relative,
        codex_usage,
        label="codex-usage executable",
    )
    _verify_recorded_file(
        record_entries,
        watchdog_relative,
        integration_watchdog,
        label="integration watchdog executable",
    )
    modules: list[_RegularFileBinding] = []
    for module in sorted({codex_usage.expected_module, integration_watchdog.expected_module}):
        module_relative = module.replace(".", "/") + ".py"
        if module_relative not in files:
            raise ServiceError(f"codex-usage distribution RECORD is missing {module}")
        module_binding = _read_distribution_file(
            distribution,
            module_relative,
            label=f"{module} module",
        )
        _verify_recorded_file(
            record_entries,
            module_relative,
            module_binding,
            label=f"{module} module",
        )
        modules.append(module_binding)
    distribution_binding = _DistributionBinding(
        version=EXPECTED_DISTRIBUTION_VERSION,
        metadata=metadata,
        record=record,
        modules=tuple(modules),
    )
    return (
        replace(codex_usage, distribution=distribution_binding),
        replace(integration_watchdog, distribution=distribution_binding),
    )


def _revalidate_integration_watchdog_for_unit_write(codex_usage_executable: Path) -> None:
    expected = _RESOLVED_INTEGRATION_WATCHDOG_BINDINGS.get(codex_usage_executable)
    if expected is None:
        raise ServiceError("integration watchdog executable was not resolved before unit write")
    try:
        codex_usage = _read_bound_console_script(
            expected[0].path,
            expected_module="codex_usage.cli",
            label="codex-usage executable",
        )
        integration_watchdog = _read_bound_console_script(
            expected[1].path,
            expected_module="codex_usage.integration_watchdog",
            label="integration watchdog executable",
        )
        current = _bind_console_scripts_to_distribution(
            codex_usage,
            integration_watchdog,
        )
    except ServiceError as exc:
        raise ServiceError(
            "integration watchdog executable changed before unit write"
        ) from exc
    if current != expected:
        raise ServiceError("integration watchdog executable changed before unit write")


def _before_service_unit_write() -> None:
    return None


def _validate_home_path(path: Path) -> None:
    home = Path.home().resolve()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ServiceError("auth_json_path parent is unavailable") from exc
    if resolved != home and home not in resolved.parents:
        raise ServiceError("auth_json_path parent must stay inside the home directory")
    if (
        resolved != Path(os.path.abspath(path))
        or path.is_symlink()
        or not path.is_dir()
    ):
        raise ServiceError("auth_json_path parent must be a real directory")


def _reject_home_write_path(path: Path, *, label: str) -> None:
    try:
        resolved = path.resolve(strict=False)
        home = Path.home().resolve()
    except (OSError, RuntimeError) as exc:
        raise ServiceError(f"{label} cannot be resolved") from exc
    if resolved == home:
        raise ServiceError(f"{label} must not be the home directory")


def _unit_quote(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ServiceError("systemd unit value contains invalid characters")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped.replace("%", "%%") + '"'


def _terminate_systemctl_process(process: subprocess.Popen[bytes]) -> None:
    pid = getattr(process, "pid", None)
    if type(pid) is int and pid > 0:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (OSError, ValueError):
            pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_systemctl_bounded(command: list[str]) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
    )
    stdout = process.stdout
    stderr = process.stderr
    if stdout is None or stderr is None:
        _terminate_systemctl_process(process)
        raise OSError("systemctl output pipe unavailable")
    streams = {stdout: bytearray(), stderr: bytearray()}
    selector = selectors.DefaultSelector()
    total = 0
    deadline = time.monotonic() + SYSTEMCTL_TIMEOUT_SECONDS
    try:
        selector.register(stdout, selectors.EVENT_READ)
        selector.register(stderr, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_systemctl_process(process)
                raise subprocess.TimeoutExpired(command, SYSTEMCTL_TIMEOUT_SECONDS)
            ready = selector.select(remaining)
            if not ready:
                _terminate_systemctl_process(process)
                raise subprocess.TimeoutExpired(command, SYSTEMCTL_TIMEOUT_SECONDS)
            for key, _ in ready:
                stream = cast(IO[bytes], key.fileobj)
                chunk = os.read(stream.fileno(), min(8192, SYSTEMCTL_OUTPUT_MAX_BYTES + 1 - total))
                if not chunk:
                    selector.unregister(stream)
                    continue
                total += len(chunk)
                if total > SYSTEMCTL_OUTPUT_MAX_BYTES:
                    _terminate_systemctl_process(process)
                    raise ServiceError("systemctl output exceeded configured limit")
                streams[stream].extend(chunk)
        try:
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _terminate_systemctl_process(process)
            raise
        return subprocess.CompletedProcess(
            command,
            returncode,
            bytes(streams[stdout]).decode("utf-8", "replace"),
            bytes(streams[stderr]).decode("utf-8", "replace"),
        )
    except BaseException:
        if process.poll() is None:
            _terminate_systemctl_process(process)
        raise
    finally:
        selector.close()
        stdout.close()
        stderr.close()


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = shutil.which("systemctl")
    if not command:
        raise ServiceError("systemctl was not found")
    argv = [command, "--user", *args]
    try:
        completed = _run_systemctl_bounded(argv)
    except (OSError, subprocess.TimeoutExpired, ServiceError) as exc:
        raise ServiceError("systemctl command failed") from exc
    if check and completed.returncode != 0:
        raise ServiceError(f"systemctl {' '.join(args[:1])} failed")
    return completed


def _systemctl_state(command: str, unit: str) -> str:
    try:
        completed = _systemctl(command, unit, check=False)
    except ServiceError:
        return "unknown"
    return completed.stdout.strip().lower()


def _systemctl_show(unit: str, properties: tuple[str, ...]) -> dict[str, str]:
    args = ["show", unit]
    for property_name in properties:
        args.extend(["-p", property_name])
    try:
        completed = _systemctl(*args, check=False)
    except ServiceError:
        return {}
    result: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in properties:
            result[key] = value[:500]
    return result


def _systemd_activation_snapshot() -> tuple[str, str]:
    enabled = _systemctl_state("is-enabled", TIMER_NAME)
    active = _systemctl_state("is-active", TIMER_NAME)
    if enabled in {"", "unknown"} or active in {"", "unknown"}:
        unit_dir = _unit_directory(create=False)
        paths = (unit_dir / SERVICE_NAME, unit_dir / TIMER_NAME)
        if not any(path.exists() or path.is_symlink() for path in paths):
            return ("not-found", "inactive")
    return (enabled, active)


def _quiesce_systemd_activation(snapshot: tuple[str, str]) -> None:
    """Stop the old timer before an attested runtime or unit becomes visible."""
    enabled, active = snapshot
    if enabled not in {"enabled", "disabled", "not-found"} or active not in {
        "active",
        "inactive",
    }:
        raise ServiceError("cannot quiesce unknown systemd activation state")
    if active == "active":
        _systemctl("stop", TIMER_NAME)
    if enabled == "enabled":
        _systemctl("disable", TIMER_NAME)


def _restore_systemd_activation(snapshot: tuple[str, str]) -> None:
    enabled, active = snapshot
    errors: list[Exception] = []
    enabled_command = {
        "enabled": "enable",
        "disabled": "disable",
        "not-found": "disable",
    }.get(enabled)
    if enabled_command is not None:
        try:
            _systemctl(enabled_command, TIMER_NAME)
        except Exception as exc:
            errors.append(exc)
    else:
        errors.append(ServiceError(f"cannot restore systemd enabled state: {enabled}"))
    active_command = {"active": "start", "inactive": "stop"}.get(active)
    if active_command is not None:
        try:
            _systemctl(active_command, TIMER_NAME)
        except Exception as exc:
            errors.append(exc)
    else:
        errors.append(ServiceError(f"cannot restore systemd active state: {active}"))
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise ExceptionGroup("systemd activation restore failed", errors)


def _cleanup_managed_timer_enable_link() -> None:
    unit_dir = _unit_directory(create=False)
    wants_dir = unit_dir / "timers.target.wants"
    _assert_no_symlink_ancestors(wants_dir)
    if not wants_dir.exists():
        if wants_dir.is_symlink():
            raise ServiceError("systemd timer wants directory must not be a symlink")
        return
    if not wants_dir.is_dir():
        raise ServiceError("systemd timer wants path must be a directory")
    link = wants_dir / TIMER_NAME
    if not (link.exists() or link.is_symlink()):
        return
    if not link.is_symlink():
        raise ServiceError("refusing to remove a non-symlink systemd enable path")
    try:
        target = link.resolve(strict=False)
        expected = (unit_dir / TIMER_NAME).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ServiceError("could not resolve systemd enable link") from exc
    if target != expected:
        raise ServiceError("refusing to remove a foreign systemd enable link")
    link.unlink()


def _normalize_exec_main_code(value: str | None) -> str:
    code = str(value or "unknown").strip()
    return EXEC_MAIN_CODE_NAMES.get(code, code or "unknown")


def _is_managed_unit(path: Path) -> bool:
    try:
        _validate_managed_unit(path)
        return True
    except (OSError, ValueError, ServiceError):
        return False


def _validate_existing_managed_units(unit_dir: Path) -> None:
    for name in (SERVICE_NAME, TIMER_NAME):
        path = unit_dir / name
        if path.exists() or path.is_symlink():
            _validate_managed_unit(path)


def _read_unit_snapshot(path: Path) -> str | None:
    if not (path.exists() or path.is_symlink()):
        return None
    text, _ = read_private_text(
        path,
        regular_label="systemd unit",
        read_label="systemd unit",
        max_bytes=MAX_UNIT_BYTES,
    )
    return text


def _restore_unit_snapshot(previous: dict[Path, str | None]) -> None:
    errors: list[BaseException] = []
    for path, text in previous.items():
        try:
            if text is None:
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise ServiceError(f"cannot remove unexpected systemd unit: {path}")
                path.unlink(missing_ok=True)
                continue
            label = "systemd service" if path.name == SERVICE_NAME else "systemd timer"
            write_private_text(path, text, label=label, mode=0o600)
        except BaseException as exc:
            errors.append(exc)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        if all(isinstance(error, Exception) for error in errors):
            raise ExceptionGroup(
                "systemd unit snapshot restore failed",
                cast("list[Exception]", errors),
            )
        raise BaseExceptionGroup("systemd unit snapshot restore failed", errors)


def _require_complete_managed_units(unit_dir: Path) -> tuple[Path, Path] | None:
    paths = (unit_dir / SERVICE_NAME, unit_dir / TIMER_NAME)
    present = [path.exists() or path.is_symlink() for path in paths]
    if not any(present):
        return None
    _validate_existing_managed_units(unit_dir)
    if not all(present):
        raise ServiceError("managed service and timer must both exist")
    return paths


def _validate_managed_unit(path: Path) -> None:
    text, _ = read_private_text(
        path,
        regular_label="systemd unit",
        read_label="systemd unit",
        max_bytes=MAX_UNIT_BYTES,
    )
    if MANAGED_MARKER not in text.splitlines():
        raise ServiceError("refusing to modify an unmanaged systemd unit")


def render_service_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
