from __future__ import annotations

import base64
import csv
import hashlib
import io
import math
import os
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import IO, cast

from . import integration_entrypoint
from .integration_attestation import (
    RUNTIME_SELF_ATTESTED_CORE_MODULES,
    TRUSTED_CORE_MODULES,
    VerifiedActiveManifest,
    _active_entrypoint_candidate_from_active_manifest,
    _active_release_core_module_evidence,
    _CoreModuleEvidence,
    _metadata_header,
    verify_active_manifest_at,
)
from .integration_timeout_contract import (
    INTEGRATION_WATCHDOG_ATTESTATION_TIMEOUT_SECONDS,
    INTEGRATION_WATCHDOG_GENERIC_TIMEOUT_SECONDS,
    INTEGRATION_WATCHDOG_PROCESS_CLEANUP_TIMEOUT_SECONDS,
    INTEGRATION_WATCHDOG_PUBLISH_TIMEOUT_SECONDS,
    INTEGRATION_WATCHDOG_RUNTIME_SELF_ATTESTATION_TIMEOUT_SECONDS,
    INTEGRATION_WATCHDOG_SYSTEMD_GRACE_SECONDS,
    INTEGRATION_WATCHDOG_SYSTEMD_TIMEOUT_SECONDS,
    INTEGRATION_WATCHDOG_TOTAL_RUNTIME_BUDGET_SECONDS,
)
from .private_io import (
    IntegrationEvidenceInvalid,
    IntegrationEvidenceUnavailable,
    open_private_dir_at,
    open_verified_state_home,
    read_private_bytes_at,
)

PUBLISH_TIMEOUT_SECONDS = INTEGRATION_WATCHDOG_PUBLISH_TIMEOUT_SECONDS
GENERIC_WATCHDOG_TIMEOUT_SECONDS = INTEGRATION_WATCHDOG_GENERIC_TIMEOUT_SECONDS
ATTESTATION_TIMEOUT_SECONDS = INTEGRATION_WATCHDOG_ATTESTATION_TIMEOUT_SECONDS
RUNTIME_SELF_ATTESTATION_TIMEOUT_SECONDS = (
    INTEGRATION_WATCHDOG_RUNTIME_SELF_ATTESTATION_TIMEOUT_SECONDS
)
PROCESS_CLEANUP_TIMEOUT_SECONDS = INTEGRATION_WATCHDOG_PROCESS_CLEANUP_TIMEOUT_SECONDS
TOTAL_RUNTIME_BUDGET_SECONDS = INTEGRATION_WATCHDOG_TOTAL_RUNTIME_BUDGET_SECONDS
SYSTEMD_GRACE_SECONDS = INTEGRATION_WATCHDOG_SYSTEMD_GRACE_SECONDS
SYSTEMD_TIMEOUT_START_SECONDS = INTEGRATION_WATCHDOG_SYSTEMD_TIMEOUT_SECONDS
PUBLISH_ARGV = ("integration-snapshot", "--schema", "2", "--format", "json")
_ALLOWED_WATCHDOG_STATUS = frozenset((0, 2))
_PROCESS_GROUP_TERM_GRACE_SECONDS = 2.0
_PROCESS_GROUP_KILL_GRACE_SECONDS = 2.0
_PROCESS_GROUP_EXIT_GRACE_SECONDS = 2.0
_DIAGNOSTIC_MAX_BYTES = 4096
_DIAGNOSTIC_READ_CHUNK_BYTES = 8192
_DIAGNOSTIC_MAX_ERROR_TYPES = 8
_DIAGNOSTIC_STATUS_CODES = frozenset((64, 65, 69, 70, 75))
_PUBLISHER_ONLY_DIAGNOSTIC_MAX_BYTES = 512
_PUBLISHER_ONLY_DIAGNOSTIC_STAGES = frozenset(
    (
        "arguments",
        "initial_runtime_attestation",
        "runtime_self_attestation",
        "outer_failure",
    )
)
_PUBLISHER_ONLY_EXCEPTION_CLASS_TOKENS = {
    BaseExceptionGroup: "BaseExceptionGroup",
    Exception: "Exception",
    ExceptionGroup: "ExceptionGroup",
    IntegrationEvidenceInvalid: "IntegrationEvidenceInvalid",
    IntegrationEvidenceUnavailable: "IntegrationEvidenceUnavailable",
    OSError: "OSError",
    RuntimeError: "RuntimeError",
    TimeoutError: "TimeoutError",
    TypeError: "TypeError",
    ValueError: "ValueError",
}
_KNOWN_PUBLISHER_STDERR_TOKENS = frozenset(
    (
        "integration_snapshot_invalid_arguments",
        "integration_snapshot_invalid_source",
        "integration_snapshot_unavailable",
        "integration_snapshot_secure_io_failed",
        "integration_snapshot_busy",
    )
)
_FORBIDDEN_RUNTIME_ENVIRONMENT_NAMES = frozenset(
    (
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
)
_REQUIRED_RUNTIME_ENVIRONMENT = {
    "PYTHONSAFEPATH": "1",
    "PYTHONNOUSERSITE": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}
_CHILD_RUNTIME_ENVIRONMENT_NAMES = (
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONNOUSERSITE",
    "PYTHONSAFEPATH",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)
_PRIVATE_SERVICE_RUNTIME_DIRECTORY = "codex-usage-service-runtime-v2"
_PRIVATE_SERVICE_RUNTIME_MAX_BYTES = 4 * 1024 * 1024
_PRIVATE_SERVICE_RUNTIME_INTERPRETER_MAX_BYTES = 128 * 1024 * 1024
_EXPECTED_CORE_DISTRIBUTION = "codex-usage"


class _IntegrationWatchdogStageTimeout(TimeoutError):
    pass


class _ProcessGroupCleanupError(RuntimeError):
    pass


@dataclass(frozen=True)
class _PrivateRuntimeFileEvidence:
    relative: str
    identity: tuple[int, ...]
    payload_sha256: str
    size: int


@dataclass(frozen=True)
class _PrivateServiceRuntimeEvidence:
    directories: tuple[tuple[int, ...], ...]
    pyvenv: _PrivateRuntimeFileEvidence
    interpreter: _PrivateRuntimeFileEvidence
    metadata: _PrivateRuntimeFileEvidence
    record: _PrivateRuntimeFileEvidence
    modules: tuple[_PrivateRuntimeFileEvidence, ...]


@dataclass(frozen=True)
class _PrivateRuntimeInitialAttestation:
    runtime_entrypoint_path: Path
    interpreter_path: Path
    verified: VerifiedActiveManifest
    runtime_evidence: _PrivateServiceRuntimeEvidence


_PRIVATE_RUNTIME_INITIAL_ATTESTATION = threading.local()


@dataclass(frozen=True)
class _StageDiagnostics:
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_truncated: bool = False
    stderr_truncated: bool = False


def _bounded_status(value: object, *, fallback: int) -> int:
    if type(value) is int and 0 <= value <= 255:
        return value
    return fallback


def _parse_unit_argv(argv: Sequence[str]) -> Path:
    try:
        normalized = tuple(argv)
    except Exception as exc:
        raise ValueError("integration watchdog arguments are invalid") from exc
    if (
        len(normalized) != 2
        or normalized[0] != "--config"
        or type(normalized[1]) is not str
        or not normalized[1]
        or "\x00" in normalized[1]
    ):
        raise ValueError("integration watchdog arguments are invalid")
    config_path = Path(normalized[1])
    if not config_path.is_absolute():
        raise ValueError("integration watchdog config path must be absolute")
    return config_path


def _runtime_root(environ: Mapping[str, str], name: str) -> Path:
    value = environ.get(name)
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError(f"{name} is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return path


def _validate_runtime_environment(environ: Mapping[str, str]) -> None:
    for name in _FORBIDDEN_RUNTIME_ENVIRONMENT_NAMES:
        value = environ.get(name)
        if value is not None and value != "":
            raise ValueError(f"{name} is forbidden")
    for name, expected in _REQUIRED_RUNTIME_ENVIRONMENT.items():
        if environ.get(name) != expected:
            raise ValueError(f"{name} is invalid")


def _validated_child_environment(environ: Mapping[str, str]) -> Mapping[str, str]:
    _validate_runtime_environment(environ)
    _runtime_root(environ, "XDG_DATA_HOME")
    _runtime_root(environ, "XDG_STATE_HOME")
    child_environment: dict[str, str] = {}
    for name in _CHILD_RUNTIME_ENVIRONMENT_NAMES:
        value = environ.get(name)
        if type(value) is not str or not value or "\x00" in value:
            raise ValueError(f"{name} is invalid")
        child_environment[name] = value
    return MappingProxyType(child_environment)


def _runtime_self_attestation(
    *,
    runtime_entrypoint_path: Path,
    interpreter_path: Path,
    verified: VerifiedActiveManifest,
) -> None:
    verify_private_service_runtime_self_attestation(
        runtime_entrypoint_path,
        interpreter_path,
        verified,
    )


def _validate_private_service_runtime_paths(
    *,
    data_home: Path,
    runtime_entrypoint_path: Path,
    interpreter_path: Path,
) -> str:
    if (
        not isinstance(data_home, Path)
        or not isinstance(runtime_entrypoint_path, Path)
        or not isinstance(interpreter_path, Path)
        or not data_home.is_absolute()
        or not runtime_entrypoint_path.is_absolute()
        or not interpreter_path.is_absolute()
        or any(part in {"", ".", ".."} for part in data_home.parts[1:])
        or any(
            part in {"", ".", ".."} for part in runtime_entrypoint_path.parts[1:]
        )
        or any(part in {"", ".", ".."} for part in interpreter_path.parts[1:])
        or "\x00" in str(data_home)
        or "\x00" in str(runtime_entrypoint_path)
        or "\x00" in str(interpreter_path)
    ):
        raise IntegrationEvidenceUnavailable()
    abi = f"python{sys.version_info.major}.{sys.version_info.minor}"
    runtime_root = data_home / _PRIVATE_SERVICE_RUNTIME_DIRECTORY / "current"
    expected_entrypoint = (
        runtime_root
        / "venv"
        / "lib"
        / abi
        / "site-packages"
        / "codex_usage"
        / "integration_entrypoint.py"
    )
    expected_interpreter = runtime_root / "venv" / "bin" / "python"
    if (
        runtime_entrypoint_path != expected_entrypoint
        or interpreter_path != expected_interpreter
        or Path(sys.executable) != expected_interpreter
    ):
        raise IntegrationEvidenceUnavailable()
    return abi


def _private_runtime_record_rows(payload: bytes) -> dict[str, tuple[str, int]]:
    rows: dict[str, tuple[str, int]] = {}
    try:
        reader = csv.reader(io.StringIO(payload.decode("utf-8")))
        for count, row in enumerate(reader, start=1):
            if count > 4096 or len(row) != 3:
                raise IntegrationEvidenceUnavailable()
            relative, digest, size_text = row
            if (
                relative in rows
                or not relative
                or relative.startswith("/")
                or "\\" in relative
                or "\x00" in relative
                or any(part in {"", ".", ".."} for part in relative.split("/"))
            ):
                raise IntegrationEvidenceUnavailable()
            if digest or size_text:
                if not digest or not size_text.isdecimal():
                    raise IntegrationEvidenceUnavailable()
                rows[relative] = (digest, int(size_text))
            else:
                rows[relative] = ("", -1)
    except (UnicodeDecodeError, csv.Error, OverflowError, ValueError) as exc:
        raise IntegrationEvidenceUnavailable() from exc
    if not rows:
        raise IntegrationEvidenceUnavailable()
    return rows


def _private_runtime_record_digest(payload: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
    return "sha256=" + encoded.decode("ascii").rstrip("=")


def _require_private_runtime_record_binding(
    rows: Mapping[str, tuple[str, int]],
    relative: str,
    payload: bytes,
) -> None:
    if rows.get(relative) != (_private_runtime_record_digest(payload), len(payload)):
        raise IntegrationEvidenceUnavailable()


def _private_runtime_file_identity(identity: object) -> tuple[int, ...]:
    values = (
        getattr(identity, "device", None),
        getattr(identity, "inode", None),
        getattr(identity, "mode", None),
        getattr(identity, "gid", None),
        getattr(identity, "uid", None),
        getattr(identity, "ctime_ns", None),
    )
    if any(type(value) is not int for value in values):
        raise IntegrationEvidenceUnavailable()
    return cast(tuple[int, ...], values)


def _private_runtime_directory_identity(descriptor: int) -> tuple[int, ...]:
    try:
        item = os.fstat(descriptor)
    except OSError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    if (
        not stat.S_ISDIR(item.st_mode)
        or stat.S_IMODE(item.st_mode) != 0o700
        or item.st_uid != os.geteuid()
    ):
        raise IntegrationEvidenceUnavailable()
    return (
        item.st_dev,
        item.st_ino,
        stat.S_IMODE(item.st_mode),
        item.st_uid,
        item.st_gid,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _private_runtime_file_evidence(
    *,
    relative: str,
    payload: bytes,
    identity: object,
) -> _PrivateRuntimeFileEvidence:
    return _PrivateRuntimeFileEvidence(
        relative=relative,
        identity=_private_runtime_file_identity(identity),
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
    )


def _validate_private_service_pyvenv(payload: bytes) -> None:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrationEvidenceUnavailable() from exc
    if "\x00" in text:
        raise IntegrationEvidenceUnavailable()
    values: list[str] = []
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().casefold() == "include-system-site-packages":
            values.append(value.strip().casefold())
    if values != ["false"]:
        raise IntegrationEvidenceUnavailable()


def _private_service_runtime_core_evidence(
    *,
    data_home: Path,
    runtime_entrypoint_path: Path,
    interpreter_path: Path,
    verified: VerifiedActiveManifest,
) -> _PrivateServiceRuntimeEvidence:
    abi = _validate_private_service_runtime_paths(
        data_home=data_home,
        runtime_entrypoint_path=runtime_entrypoint_path,
        interpreter_path=interpreter_path,
    )
    expected_dist_info = f"codex_usage-{verified.active_release.version}.dist-info"
    if (
        verified.active_release.version != "0.6.540"
        or not verified.release_id.startswith("0.6.540-")
    ):
        raise IntegrationEvidenceUnavailable()

    descriptors: list[int] = []
    try:
        data_fd = open_verified_state_home(data_home)
        descriptors.append(data_fd)
        runtime_fd = open_private_dir_at(data_fd, _PRIVATE_SERVICE_RUNTIME_DIRECTORY)
        descriptors.append(runtime_fd)
        current_fd = open_private_dir_at(runtime_fd, "current")
        descriptors.append(current_fd)
        venv_fd = open_private_dir_at(current_fd, "venv")
        descriptors.append(venv_fd)
        bin_fd = open_private_dir_at(venv_fd, "bin")
        descriptors.append(bin_fd)
        lib_fd = open_private_dir_at(venv_fd, "lib")
        descriptors.append(lib_fd)
        abi_fd = open_private_dir_at(lib_fd, abi)
        descriptors.append(abi_fd)
        site_fd = open_private_dir_at(abi_fd, "site-packages")
        descriptors.append(site_fd)
        package_fd = open_private_dir_at(site_fd, "codex_usage")
        descriptors.append(package_fd)
        dist_fd = open_private_dir_at(site_fd, expected_dist_info)
        descriptors.append(dist_fd)
        initial_directories = tuple(
            _private_runtime_directory_identity(descriptor)
            for descriptor in descriptors
        )

        pyvenv, pyvenv_identity = read_private_bytes_at(
            venv_fd,
            "pyvenv.cfg",
            maximum=_PRIVATE_SERVICE_RUNTIME_MAX_BYTES,
            mode=0o600,
        )
        _validate_private_service_pyvenv(pyvenv)
        interpreter, interpreter_identity = read_private_bytes_at(
            bin_fd,
            "python",
            maximum=_PRIVATE_SERVICE_RUNTIME_INTERPRETER_MAX_BYTES,
            mode=0o700,
        )
        metadata, metadata_identity = read_private_bytes_at(
            dist_fd,
            "METADATA",
            maximum=_PRIVATE_SERVICE_RUNTIME_MAX_BYTES,
            mode=0o600,
        )
        record, record_identity = read_private_bytes_at(
            dist_fd,
            "RECORD",
            maximum=_PRIVATE_SERVICE_RUNTIME_MAX_BYTES,
            mode=0o600,
        )
        if (
            _metadata_header(metadata, "Name") != _EXPECTED_CORE_DISTRIBUTION
            or _metadata_header(metadata, "Version") != verified.active_release.version
        ):
            raise IntegrationEvidenceUnavailable()
        rows = _private_runtime_record_rows(record)
        metadata_relative = f"{expected_dist_info}/METADATA"
        record_relative = f"{expected_dist_info}/RECORD"
        _require_private_runtime_record_binding(rows, metadata_relative, metadata)
        if rows.get(record_relative) != ("", -1):
            raise IntegrationEvidenceUnavailable()

        modules: list[_CoreModuleEvidence] = []
        runtime_modules: list[_PrivateRuntimeFileEvidence] = []
        for module_name in TRUSTED_CORE_MODULES:
            payload, identity = read_private_bytes_at(
                package_fd,
                module_name,
                maximum=_PRIVATE_SERVICE_RUNTIME_MAX_BYTES,
                mode=0o600,
            )
            relative = f"codex_usage/{module_name}"
            _require_private_runtime_record_binding(rows, relative, payload)
            runtime_modules.append(
                _private_runtime_file_evidence(
                    relative=relative,
                    payload=payload,
                    identity=identity,
                )
            )
            modules.append(
                _CoreModuleEvidence(
                    relative=relative,
                    identity=_private_runtime_file_identity(identity),
                    payload_sha256=hashlib.sha256(payload).hexdigest(),
                    size=len(payload),
                    payload=payload,
                )
            )
        modules.sort(key=lambda module: module.relative)
        if {
            module.relative.removeprefix("codex_usage/") for module in modules
        } != set(TRUSTED_CORE_MODULES):
            raise IntegrationEvidenceUnavailable()
        _active_release_core_module_evidence(verified, tuple(modules))
        runtime_modules.sort(key=lambda module: module.relative)
        final_directories = tuple(
            _private_runtime_directory_identity(descriptor)
            for descriptor in descriptors
        )
        if final_directories != initial_directories:
            raise IntegrationEvidenceUnavailable()
        return _PrivateServiceRuntimeEvidence(
            directories=initial_directories,
            pyvenv=_private_runtime_file_evidence(
                relative="venv/pyvenv.cfg",
                payload=pyvenv,
                identity=pyvenv_identity,
            ),
            interpreter=_private_runtime_file_evidence(
                relative="venv/bin/python",
                payload=interpreter,
                identity=interpreter_identity,
            ),
            metadata=_private_runtime_file_evidence(
                relative=metadata_relative,
                payload=metadata,
                identity=metadata_identity,
            ),
            record=_private_runtime_file_evidence(
                relative=record_relative,
                payload=record,
                identity=record_identity,
            ),
            modules=tuple(runtime_modules),
        )
    except IntegrationEvidenceUnavailable:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise IntegrationEvidenceUnavailable() from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _verify_active_manifest_against_private_service_runtime(
    state_home: Path,
    data_home: Path,
    runtime_entrypoint_path: Path,
    interpreter_path: Path,
) -> tuple[VerifiedActiveManifest, _PrivateServiceRuntimeEvidence]:
    candidate_entrypoint = _active_entrypoint_candidate_from_active_manifest(
        state_home=state_home,
        data_home=data_home,
    )
    verified = verify_active_manifest_at(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=candidate_entrypoint,
    )
    runtime_evidence = _private_service_runtime_core_evidence(
        data_home=data_home,
        runtime_entrypoint_path=runtime_entrypoint_path,
        interpreter_path=interpreter_path,
        verified=verified,
    )
    return verified, runtime_evidence


def verify_active_manifest_against_private_service_runtime(
    state_home: Path,
    data_home: Path,
    runtime_entrypoint_path: Path,
    interpreter_path: Path,
) -> VerifiedActiveManifest:
    """Bind the private service venv to the active, exact Producer release."""
    _PRIVATE_RUNTIME_INITIAL_ATTESTATION.value = None
    verified, runtime_evidence = _verify_active_manifest_against_private_service_runtime(
        state_home,
        data_home,
        runtime_entrypoint_path,
        interpreter_path,
    )
    _PRIVATE_RUNTIME_INITIAL_ATTESTATION.value = _PrivateRuntimeInitialAttestation(
        runtime_entrypoint_path=runtime_entrypoint_path,
        interpreter_path=interpreter_path,
        verified=verified,
        runtime_evidence=runtime_evidence,
    )
    return verified


def _private_service_runtime_data_home(runtime_entrypoint_path: Path) -> Path:
    try:
        data_home = runtime_entrypoint_path.parents[7]
    except IndexError as exc:
        raise IntegrationEvidenceInvalid() from exc
    if data_home / _PRIVATE_SERVICE_RUNTIME_DIRECTORY / "current" / "venv" / "lib" / (
        f"python{sys.version_info.major}.{sys.version_info.minor}"
    ) / "site-packages" / "codex_usage" / "integration_entrypoint.py" != runtime_entrypoint_path:
        raise IntegrationEvidenceInvalid()
    return data_home


def _private_service_runtime_state_home(verified: VerifiedActiveManifest) -> Path:
    release_dir = verified.active_release.release_dir
    try:
        releases = release_dir.parent
        integration = releases.parent
        application = integration.parent
        state_home = application.parent
    except (AttributeError, TypeError) as exc:
        raise IntegrationEvidenceInvalid() from exc
    if (
        releases.name != "releases"
        or integration.name != "integration"
        or application.name != "codex-usage"
        or not state_home.is_absolute()
    ):
        raise IntegrationEvidenceInvalid()
    return state_home


def verify_private_service_runtime_self_attestation(
    runtime_entrypoint_path: Path,
    interpreter_path: Path,
    verified: VerifiedActiveManifest,
) -> None:
    """Revalidate the private service venv and imported modules before publish."""
    initial = getattr(_PRIVATE_RUNTIME_INITIAL_ATTESTATION, "value", None)
    _PRIVATE_RUNTIME_INITIAL_ATTESTATION.value = None
    if (
        type(verified) is not VerifiedActiveManifest
        or type(initial) is not _PrivateRuntimeInitialAttestation
        or initial.verified is not verified
        or initial.runtime_entrypoint_path != runtime_entrypoint_path
        or initial.interpreter_path != interpreter_path
    ):
        raise IntegrationEvidenceInvalid()
    try:
        reverified, runtime_evidence = _verify_active_manifest_against_private_service_runtime(
            _private_service_runtime_state_home(verified),
            _private_service_runtime_data_home(runtime_entrypoint_path),
            runtime_entrypoint_path,
            interpreter_path,
        )
    except (IntegrationEvidenceUnavailable, OSError, TypeError, ValueError) as exc:
        raise IntegrationEvidenceInvalid() from exc
    if reverified != verified or runtime_evidence != initial.runtime_evidence:
        raise IntegrationEvidenceInvalid()
    site_packages = runtime_entrypoint_path.parent.parent
    for module_name in RUNTIME_SELF_ATTESTED_CORE_MODULES:
        if module_name == "codex_usage":
            relative = "codex_usage/__init__.py"
        else:
            relative = f"codex_usage/{module_name.removeprefix('codex_usage.')}.py"
        module = sys.modules.get(module_name)
        module_spec = None if module is None else getattr(module, "__spec__", None)
        module_file = None if module is None else getattr(module, "__file__", None)
        origin = None if module_spec is None else getattr(module_spec, "origin", None)
        expected = site_packages / relative
        if module_file != str(expected) or origin != str(expected):
            raise IntegrationEvidenceInvalid()


def _stage_timeout_seconds(
    *,
    deadline: float,
    stage_limit: int,
    monotonic: Callable[[], float],
    reserve_seconds: float = 0.0,
) -> float:
    remaining = deadline - monotonic() - reserve_seconds
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError("integration watchdog runtime budget exhausted")
    return min(float(stage_limit), remaining)


def _remaining_real_timer(
    previous_timer: tuple[float, float],
    *,
    captured_at: float,
    monotonic: Callable[[], float] | None = None,
) -> tuple[float, float] | None:
    if monotonic is None:
        monotonic = time.monotonic
    delay_seconds, interval_seconds = previous_timer
    if delay_seconds <= 0:
        return None
    elapsed_seconds = monotonic() - captured_at
    if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        elapsed_seconds = 0.0
    remaining_seconds = delay_seconds - elapsed_seconds
    if remaining_seconds <= 0 and interval_seconds > 0:
        missed_intervals = math.floor(abs(remaining_seconds) / interval_seconds) + 1
        remaining_seconds += missed_intervals * interval_seconds
    if remaining_seconds <= 0:
        return None
    return (remaining_seconds, interval_seconds)


def _restore_real_timer(
    previous_timer: tuple[float, float],
    *,
    captured_at: float,
) -> None:
    remaining_timer = _remaining_real_timer(previous_timer, captured_at=captured_at)
    if remaining_timer is not None:
        signal.setitimer(signal.ITIMER_REAL, *remaining_timer)


@contextmanager
def _disarmed_real_timer_for_cleanup() -> Iterator[None]:
    previous_mask = None
    if hasattr(signal, "pthread_sigmask"):
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK,
            {signal.SIGALRM},
        )
    timer_captured_at = time.monotonic()
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        yield
    finally:
        try:
            _restore_real_timer(previous_timer, captured_at=timer_captured_at)
        finally:
            if previous_mask is not None:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _call_with_timeout(
    label: str,
    timeout_seconds: float,
    callback: Callable[[], object],
) -> object:
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise TimeoutError(label)
    seconds = float(timeout_seconds)
    timed_out = False

    def timeout_handler(_signum, _frame):
        nonlocal timed_out
        timed_out = True
        raise _IntegrationWatchdogStageTimeout(label)

    previous_handler = signal.getsignal(signal.SIGALRM)
    timer_captured_at = time.monotonic()
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            result = callback()
        except _IntegrationWatchdogStageTimeout as exc:
            raise TimeoutError(label) from exc
        except BaseExceptionGroup as exc:
            if timed_out:
                pending: list[BaseException] = [exc]
                while pending:
                    item = pending.pop()
                    if isinstance(item, BaseExceptionGroup):
                        pending.extend(item.exceptions)
                    elif isinstance(item, _IntegrationWatchdogStageTimeout):
                        raise
                raise _combine_stage_cleanup_errors(
                    "stage timed out during cleanup",
                    _IntegrationWatchdogStageTimeout(label),
                    exc,
                ) from exc
            raise
        except Exception as exc:
            if timed_out:
                explicit_cause = exc.__cause__
                while explicit_cause is not None:
                    if isinstance(explicit_cause, TimeoutError):
                        raise TimeoutError(label) from exc
                    explicit_cause = explicit_cause.__cause__
                raise ExceptionGroup(
                    "stage timed out during cleanup",
                    [_IntegrationWatchdogStageTimeout(label), exc],
                ) from exc
            raise
        if timed_out:
            raise TimeoutError(label)
        return result
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        _restore_real_timer(previous_timer, captured_at=timer_captured_at)


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup_timeout(deadline: float, preferred_seconds: float) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining):
        return 0.0
    return max(0.0, min(float(preferred_seconds), remaining))


def _raise_cleanup_errors(message: str, errors: list[BaseException]) -> None:
    if not errors:
        return
    group_type = (
        ExceptionGroup
        if all(isinstance(error, Exception) for error in errors)
        else BaseExceptionGroup
    )
    raise _ProcessGroupCleanupError(message) from group_type(
        "process-group cleanup errors",
        errors,
    )


def _signal_process_group(
    process_group_id: int,
    signal_number: int,
) -> BaseException | None:
    try:
        os.killpg(process_group_id, signal_number)
    except ProcessLookupError:
        return None
    except BaseException as exc:
        return exc
    return None


def _signal_process(
    process: subprocess.Popen[object],
    method_name: str,
) -> BaseException | None:
    try:
        getattr(process, method_name)()
    except ProcessLookupError:
        return None
    except BaseException as exc:
        return exc
    return None


def _wait_process_bounded(
    process: subprocess.Popen[object],
    timeout_seconds: float,
) -> tuple[bool, BaseException | None]:
    poll = getattr(process, "poll", None)
    if callable(poll):
        try:
            if poll() is not None:
                return True, None
        except BaseException as exc:
            return False, exc
    try:
        process.wait(timeout=max(0.0, float(timeout_seconds)))
    except subprocess.TimeoutExpired:
        return False, None
    except BaseException as exc:
        return False, exc
    return True, None


def _process_group_gone(
    process_group_id: int,
    *,
    deadline: float,
    preferred_seconds: float,
) -> bool:
    timeout_seconds = _cleanup_timeout(deadline, preferred_seconds)
    end = time.monotonic() + timeout_seconds
    while True:
        if not _process_group_exists(process_group_id):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(min(0.01, max(0.0, end - time.monotonic())))


def _combine_stage_cleanup_errors(
    message: str,
    active_error: BaseException,
    cleanup_error: BaseException,
) -> BaseExceptionGroup:
    group_type = (
        ExceptionGroup
        if isinstance(active_error, Exception) and isinstance(cleanup_error, Exception)
        else BaseExceptionGroup
    )
    return group_type(message, [active_error, cleanup_error])


def _terminate_process_group(process: subprocess.Popen[object]) -> None:
    process_group_id = getattr(process, "pid", None)
    deadline = time.monotonic() + PROCESS_CLEANUP_TIMEOUT_SECONDS
    if type(process_group_id) is not int or process_group_id <= 0:
        errors: list[BaseException] = []
        kill_error = _signal_process(process, "kill")
        if kill_error is not None:
            errors.append(kill_error)
        waited, wait_error = _wait_process_bounded(
            process,
            _cleanup_timeout(deadline, _PROCESS_GROUP_KILL_GRACE_SECONDS),
        )
        if wait_error is not None:
            errors.append(wait_error)
        if not waited:
            errors.append(_ProcessGroupCleanupError("leader process did not exit"))
        _raise_cleanup_errors("process cleanup failed", errors)
        return

    errors: list[BaseException] = []
    with _disarmed_real_timer_for_cleanup():
        term_error = _signal_process_group(process_group_id, signal.SIGTERM)
        if term_error is not None:
            errors.append(term_error)
        waited, wait_error = _wait_process_bounded(
            process,
            _cleanup_timeout(deadline, _PROCESS_GROUP_TERM_GRACE_SECONDS),
        )
        if wait_error is not None:
            errors.append(wait_error)
        if not waited:
            kill_error = _signal_process_group(process_group_id, signal.SIGKILL)
            if kill_error is not None:
                errors.append(kill_error)
            waited_after_kill, kill_wait_error = _wait_process_bounded(
                process,
                _cleanup_timeout(deadline, _PROCESS_GROUP_KILL_GRACE_SECONDS),
            )
            if kill_wait_error is not None:
                errors.append(kill_wait_error)
            if not waited_after_kill:
                errors.append(
                    _ProcessGroupCleanupError("leader process did not exit after kill")
                )
        if not _process_group_gone(
            process_group_id,
            deadline=deadline,
            preferred_seconds=_PROCESS_GROUP_EXIT_GRACE_SECONDS,
        ):
            kill_error = _signal_process_group(process_group_id, signal.SIGKILL)
            if kill_error is not None:
                errors.append(kill_error)
            if not _process_group_gone(
                process_group_id,
                deadline=deadline,
                preferred_seconds=_PROCESS_GROUP_KILL_GRACE_SECONDS,
            ):
                errors.append(
                    _ProcessGroupCleanupError(
                        "process group still exists after cleanup"
                    )
                )
    _raise_cleanup_errors("process-group cleanup failed", errors)


def _append_diagnostic_bytes(
    buffer: bytearray,
    chunk: bytes,
) -> bool:
    available = _DIAGNOSTIC_MAX_BYTES - len(buffer)
    if available > 0:
        buffer.extend(chunk[:available])
    return len(chunk) > available


def _wait_with_bounded_output(
    process: subprocess.Popen[object],
    command: Sequence[str],
    *,
    timeout_seconds: float,
) -> tuple[int, _StageDiagnostics]:
    stdout = getattr(process, "stdout", None)
    stderr = getattr(process, "stderr", None)
    if stdout is None or stderr is None:
        return (
            _bounded_status(process.wait(timeout=timeout_seconds), fallback=69),
            _StageDiagnostics(),
        )
    selector = selectors.DefaultSelector()
    streams: dict[IO[bytes], tuple[str, bytearray]] = {
        cast(IO[bytes], stdout): ("stdout", bytearray()),
        cast(IO[bytes], stderr): ("stderr", bytearray()),
    }
    truncated = {"stdout": False, "stderr": False}
    deadline = time.monotonic() + timeout_seconds
    try:
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            ready = selector.select(remaining)
            if not ready:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            for key, _events in ready:
                stream = cast(IO[bytes], key.fileobj)
                chunk = os.read(stream.fileno(), _DIAGNOSTIC_READ_CHUNK_BYTES)
                if not chunk:
                    selector.unregister(stream)
                    continue
                stream_name, buffer = streams[stream]
                truncated[stream_name] = (
                    _append_diagnostic_bytes(buffer, chunk)
                    or truncated[stream_name]
                )
        status = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        return (
            _bounded_status(status, fallback=69),
            _StageDiagnostics(
                stdout=bytes(streams[cast(IO[bytes], stdout)][1]),
                stderr=bytes(streams[cast(IO[bytes], stderr)][1]),
                stdout_truncated=truncated["stdout"],
                stderr_truncated=truncated["stderr"],
            ),
        )
    finally:
        active_error = sys.exc_info()[1]
        cleanup_errors: list[BaseException] = []
        try:
            selector.close()
        except BaseException as exc:
            cleanup_errors.append(exc)
        for stream in streams:
            try:
                stream.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            cleanup_group_type = (
                ExceptionGroup
                if all(isinstance(error, Exception) for error in cleanup_errors)
                else BaseExceptionGroup
            )
            cleanup_group = cleanup_group_type(
                "diagnostic stream cleanup errors",
                cleanup_errors,
            )
            if active_error is not None:
                raise _combine_stage_cleanup_errors(
                    "diagnostic wait and cleanup failed",
                    active_error,
                    cleanup_group,
                ) from active_error
            raise cleanup_group


def _sanitize_diagnostic(payload: bytes) -> str:
    text = payload.decode("utf-8", "replace")
    return "".join(
        character
        if character in {"\n", "\r", "\t"} or (" " <= character <= "~")
        else "?"
        for character in text
    )


def _diagnostic_error_types(error: BaseException) -> tuple[str, ...]:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    names: list[str] = []
    truncated = False
    while pending:
        current = pending.pop(0)
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if len(names) >= _DIAGNOSTIC_MAX_ERROR_TYPES:
            truncated = True
            break
        names.append(type(current).__name__)
        if isinstance(current, BaseExceptionGroup):
            pending[:0] = list(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    if truncated:
        names.append("truncated")
    return tuple(names)


def _emit_stage_diagnostic(
    label: str | None,
    *,
    status: int,
    diagnostics: _StageDiagnostics | None = None,
    error: BaseException | None = None,
) -> None:
    if label is None or status not in _DIAGNOSTIC_STATUS_CODES:
        return
    if diagnostics is None:
        diagnostics = _StageDiagnostics()
    parts = [f"{label} exited with rc={status}"]
    if error is not None:
        if isinstance(error, _ProcessGroupCleanupError):
            parts.append("error=cleanup failed")
        elif isinstance(error, TimeoutError):
            parts.append("error=timed out")
        else:
            parts.append(f"error={type(error).__name__}")
        error_types = _diagnostic_error_types(error)
        if len(error_types) > 1:
            parts.append(f"error_types={','.join(error_types)}")
    if diagnostics.stderr:
        lines = tuple(
            line.strip()
            for line in _sanitize_diagnostic(diagnostics.stderr).splitlines()
            if line.strip()
        )
        known_tokens = tuple(
            line for line in lines if line in _KNOWN_PUBLISHER_STDERR_TOKENS
        )
        if known_tokens:
            parts.append(f"stderr_token={','.join(known_tokens[:4])}")
            if diagnostics.stderr_truncated:
                parts.append("stderr=truncated")
        else:
            suffix = " truncated" if diagnostics.stderr_truncated else ""
            parts.append(f"stderr=unrecognized bytes={len(diagnostics.stderr)}{suffix}")
    sys.stderr.write(("; ".join(parts).encode()[:4607]).decode("utf-8", "ignore") + "\n")


def _publisher_only_exception_class(error: BaseException) -> str:
    """Return an intentionally small, message-free diagnostic exception class."""
    return _PUBLISHER_ONLY_EXCEPTION_CLASS_TOKENS.get(type(error), "unrecognized")


def _emit_publisher_only_diagnostic(
    stage: str,
    *,
    status: int,
    error: BaseException,
) -> None:
    """Report a wrapper failure without serializing exception or input data."""
    safe_stage = stage if stage in _PUBLISHER_ONLY_DIAGNOSTIC_STAGES else "unrecognized"
    safe_status = status if status in _DIAGNOSTIC_STATUS_CODES else 69
    message = (
        "integration publisher service "
        f"stage={safe_stage} rc={safe_status} "
        f"exception={_publisher_only_exception_class(error)}\n"
    )
    sys.stderr.write(message[:_PUBLISHER_ONLY_DIAGNOSTIC_MAX_BYTES])


def _run_subprocess_stage(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    child_environ: Mapping[str, str],
    diagnostic_label: str | None = None,
) -> int:
    try:
        subprocess_environ = _validated_child_environment(child_environ)
    except ValueError as exc:
        _emit_stage_diagnostic(
            diagnostic_label,
            status=70,
            error=exc,
        )
        return 70
    try:
        process = subprocess.Popen(
            command,
            env=subprocess_environ,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if diagnostic_label is not None else subprocess.DEVNULL,
            stderr=subprocess.PIPE if diagnostic_label is not None else subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        _emit_stage_diagnostic(
            diagnostic_label,
            status=69,
            error=exc,
        )
        return 69
    try:
        status, diagnostics = _wait_with_bounded_output(
            process,
            command,
            timeout_seconds=float(timeout_seconds),
        )
        try:
            _terminate_process_group(process)
        except BaseException as cleanup_error:
            _emit_stage_diagnostic(
                diagnostic_label,
                status=69,
                error=cleanup_error,
            )
            return 69
        _emit_stage_diagnostic(
            diagnostic_label,
            status=status,
            diagnostics=diagnostics,
        )
        return status
    except subprocess.TimeoutExpired:
        try:
            _terminate_process_group(process)
        except BaseException as cleanup_error:
            _emit_stage_diagnostic(
                diagnostic_label,
                status=69,
                error=cleanup_error,
            )
            return 69
        _emit_stage_diagnostic(
            diagnostic_label,
            status=75,
            error=TimeoutError("timed out"),
        )
        return 75
    except BaseExceptionGroup as exc:
        try:
            _terminate_process_group(process)
        except BaseException as cleanup_error:
            raise _combine_stage_cleanup_errors(
                "subprocess grouped failure and cleanup failed",
                exc,
                cleanup_error,
            ) from exc
        if isinstance(exc, ExceptionGroup):
            _emit_stage_diagnostic(
                diagnostic_label,
                status=69,
                error=exc,
            )
            return 69
        raise
    except OSError as exc:
        try:
            _terminate_process_group(process)
        except BaseException as cleanup_error:
            if not isinstance(cleanup_error, Exception):
                raise _combine_stage_cleanup_errors(
                    "subprocess wait and cleanup failed",
                    exc,
                    cleanup_error,
                ) from exc
            _emit_stage_diagnostic(
                diagnostic_label,
                status=69,
                error=ExceptionGroup(
                    "subprocess wait and cleanup failed",
                    [exc, cleanup_error],
                ),
            )
            return 69
        _emit_stage_diagnostic(
            diagnostic_label,
            status=69,
            error=exc,
        )
        return 69
    except BaseException:
        active_error = sys.exc_info()[1]
        try:
            _terminate_process_group(process)
        except BaseException as cleanup_error:
            if active_error is not None:
                raise _combine_stage_cleanup_errors(
                    "subprocess stage failed during cleanup",
                    active_error,
                    cleanup_error,
                ) from active_error
        raise


def execute(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    runtime_entrypoint_path: Path,
    watchdog_runner: Callable[..., int],
    verifier: Callable[..., VerifiedActiveManifest],
    publisher_runner: Callable[..., int],
    monotonic: Callable[[], float] = time.monotonic,
    interpreter_path: Path | None = None,
) -> int:
    try:
        config_path = _parse_unit_argv(argv)
    except ValueError:
        return 64
    try:
        child_environ = _validated_child_environment(environ)
        deadline = monotonic() + TOTAL_RUNTIME_BUDGET_SECONDS
        watchdog_status = _bounded_status(
            _call_with_timeout(
                "generic watchdog",
                _stage_timeout_seconds(
                    deadline=deadline,
                    stage_limit=GENERIC_WATCHDOG_TIMEOUT_SECONDS,
                    monotonic=monotonic,
                ),
                lambda: watchdog_runner(
                    config_path,
                    child_environ=child_environ,
                ),
            ),
            fallback=69,
        )
        if watchdog_status not in _ALLOWED_WATCHDOG_STATUS:
            return watchdog_status
        state_home = _runtime_root(child_environ, "XDG_STATE_HOME")
        data_home = _runtime_root(child_environ, "XDG_DATA_HOME")
        runtime_interpreter = Path(sys.executable) if interpreter_path is None else interpreter_path
        try:
            verified = _call_with_timeout(
                "private service runtime attestation",
                _stage_timeout_seconds(
                    deadline=deadline,
                    stage_limit=ATTESTATION_TIMEOUT_SECONDS,
                    monotonic=monotonic,
                ),
                lambda: verifier(
                    state_home=state_home,
                    data_home=data_home,
                    runtime_entrypoint_path=runtime_entrypoint_path,
                    interpreter_path=runtime_interpreter,
                ),
            )
        except (IntegrationEvidenceUnavailable, IntegrationEvidenceInvalid):
            return 69
        try:
            _call_with_timeout(
                "private service runtime self attestation",
                _stage_timeout_seconds(
                    deadline=deadline,
                    stage_limit=RUNTIME_SELF_ATTESTATION_TIMEOUT_SECONDS,
                    monotonic=monotonic,
                ),
                lambda: _runtime_self_attestation(
                    runtime_entrypoint_path=runtime_entrypoint_path,
                    interpreter_path=runtime_interpreter,
                    verified=verified,
                ),
            )
        except (IntegrationEvidenceUnavailable, IntegrationEvidenceInvalid):
            return 70
        publisher_timeout = _stage_timeout_seconds(
            deadline=deadline,
            stage_limit=PUBLISH_TIMEOUT_SECONDS,
            monotonic=monotonic,
            reserve_seconds=PROCESS_CLEANUP_TIMEOUT_SECONDS,
        )
        return _bounded_status(
            publisher_runner(
                verified.active_release.launcher_path,
                PUBLISH_ARGV,
                publisher_timeout,
                child_environ=child_environ,
            ),
            fallback=69,
        )
    except IntegrationEvidenceUnavailable:
        return 69
    except IntegrationEvidenceInvalid:
        return 70
    except TimeoutError:
        return 75
    except (OSError, TypeError, ValueError):
        return 70
    except BaseExceptionGroup as exc:
        pending: list[BaseException] = [exc]
        while pending:
            item = pending.pop()
            if isinstance(item, BaseExceptionGroup):
                pending.extend(item.exceptions)
            elif isinstance(item, (KeyboardInterrupt, SystemExit)):
                raise
        return 69
    except Exception:
        return 69


def execute_publisher_only(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    runtime_entrypoint_path: Path,
    verifier: Callable[..., VerifiedActiveManifest],
    publisher_runner: Callable[..., int],
    monotonic: Callable[[], float] = time.monotonic,
    interpreter_path: Path | None = None,
) -> int:
    """Publish only the already captured V2 evidence through an attested release.

    This is intentionally separate from the historical generic watchdog API:
    the service unit has no refresh authority and cannot reach the CLI,
    scheduler, browser, direct, or Spark paths.
    """
    try:
        normalized_argv = tuple(argv)
    except (TypeError, ValueError) as exc:
        _emit_publisher_only_diagnostic("arguments", status=64, error=exc)
        return 64
    if normalized_argv:
        _emit_publisher_only_diagnostic(
            "arguments",
            status=64,
            error=ValueError(),
        )
        return 64
    try:
        child_environ = _validated_child_environment(environ)
        deadline = monotonic() + TOTAL_RUNTIME_BUDGET_SECONDS
        state_home = _runtime_root(child_environ, "XDG_STATE_HOME")
        data_home = _runtime_root(child_environ, "XDG_DATA_HOME")
        runtime_interpreter = Path(sys.executable) if interpreter_path is None else interpreter_path
        try:
            verified = _call_with_timeout(
                "private service runtime attestation",
                _stage_timeout_seconds(
                    deadline=deadline,
                    stage_limit=ATTESTATION_TIMEOUT_SECONDS,
                    monotonic=monotonic,
                ),
                lambda: verifier(
                    state_home=state_home,
                    data_home=data_home,
                    runtime_entrypoint_path=runtime_entrypoint_path,
                    interpreter_path=runtime_interpreter,
                ),
            )
        except (IntegrationEvidenceUnavailable, IntegrationEvidenceInvalid) as exc:
            _emit_publisher_only_diagnostic(
                "initial_runtime_attestation",
                status=69,
                error=exc,
            )
            return 69
        try:
            _call_with_timeout(
                "private service runtime self attestation",
                _stage_timeout_seconds(
                    deadline=deadline,
                    stage_limit=RUNTIME_SELF_ATTESTATION_TIMEOUT_SECONDS,
                    monotonic=monotonic,
                ),
                lambda: _runtime_self_attestation(
                    runtime_entrypoint_path=runtime_entrypoint_path,
                    interpreter_path=runtime_interpreter,
                    verified=verified,
                ),
            )
        except (IntegrationEvidenceUnavailable, IntegrationEvidenceInvalid) as exc:
            _emit_publisher_only_diagnostic(
                "runtime_self_attestation",
                status=70,
                error=exc,
            )
            return 70
        publisher_timeout = _stage_timeout_seconds(
            deadline=deadline,
            stage_limit=PUBLISH_TIMEOUT_SECONDS,
            monotonic=monotonic,
            reserve_seconds=PROCESS_CLEANUP_TIMEOUT_SECONDS,
        )
        return _bounded_status(
            publisher_runner(
                verified.active_release.launcher_path,
                PUBLISH_ARGV,
                publisher_timeout,
                child_environ=child_environ,
            ),
            fallback=69,
        )
    except IntegrationEvidenceUnavailable as exc:
        _emit_publisher_only_diagnostic("outer_failure", status=69, error=exc)
        return 69
    except IntegrationEvidenceInvalid as exc:
        _emit_publisher_only_diagnostic("outer_failure", status=70, error=exc)
        return 70
    except TimeoutError as exc:
        _emit_publisher_only_diagnostic("outer_failure", status=75, error=exc)
        return 75
    except (OSError, TypeError, ValueError) as exc:
        _emit_publisher_only_diagnostic("outer_failure", status=70, error=exc)
        return 70
    except BaseExceptionGroup as exc:
        pending: list[BaseException] = [exc]
        while pending:
            item = pending.pop()
            if isinstance(item, BaseExceptionGroup):
                pending.extend(item.exceptions)
            elif isinstance(item, (KeyboardInterrupt, SystemExit)):
                raise
        _emit_publisher_only_diagnostic("outer_failure", status=69, error=exc)
        return 69
    except Exception as exc:
        _emit_publisher_only_diagnostic("outer_failure", status=69, error=exc)
        return 69


def _run_watchdog_stage(
    config_path: Path,
    *,
    child_environ: Mapping[str, str],
) -> int:
    command = [
        sys.executable,
        "-m",
        "codex_usage.cli",
        "--config",
        str(config_path),
        "watchdog",
        "--format",
        "json",
    ]
    return _run_subprocess_stage(
        command,
        timeout_seconds=float(GENERIC_WATCHDOG_TIMEOUT_SECONDS),
        child_environ=child_environ,
    )


def _run_publisher_stage(
    launcher: Path,
    argv: tuple[str, ...],
    timeout_seconds: float,
    *,
    child_environ: Mapping[str, str],
) -> int:
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        return 75
    command = [str(launcher), *argv]
    return _run_subprocess_stage(
        command,
        timeout_seconds=float(timeout_seconds),
        child_environ=child_environ,
        diagnostic_label="integration publisher",
    )


def main(argv: Sequence[str] | None = None) -> int:
    return execute_publisher_only(
        tuple(sys.argv[1:] if argv is None else argv),
        environ=os.environ,
        runtime_entrypoint_path=Path(integration_entrypoint.__file__),
        verifier=verify_active_manifest_against_private_service_runtime,
        publisher_runner=_run_publisher_stage,
        interpreter_path=Path(sys.executable),
    )


if __name__ == "__main__":
    raise SystemExit(main())
