from __future__ import annotations

import math
import os
import selectors
import signal
import subprocess
import sys
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
    VerifiedActiveManifest,
    verify_active_manifest_against_trusted_entrypoint,
    verify_runtime_self_attestation,
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
from .private_io import IntegrationEvidenceInvalid, IntegrationEvidenceUnavailable

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


class _IntegrationWatchdogStageTimeout(TimeoutError):
    pass


class _ProcessGroupCleanupError(RuntimeError):
    pass


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
    trusted_entrypoint_path: Path,
    verified: VerifiedActiveManifest,
) -> None:
    verify_runtime_self_attestation(
        trusted_entrypoint_path=trusted_entrypoint_path,
        verified=verified,
        module_names=RUNTIME_SELF_ATTESTED_CORE_MODULES,
        interpreter_path=Path(sys.executable),
    )


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
    trusted_entrypoint_path: Path,
    watchdog_runner: Callable[..., int],
    verifier: Callable[..., VerifiedActiveManifest],
    publisher_runner: Callable[..., int],
    monotonic: Callable[[], float] = time.monotonic,
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
        verified = _call_with_timeout(
            "trusted entrypoint attestation",
            _stage_timeout_seconds(
                deadline=deadline,
                stage_limit=ATTESTATION_TIMEOUT_SECONDS,
                monotonic=monotonic,
            ),
            lambda: verifier(
                state_home=state_home,
                data_home=data_home,
                trusted_entrypoint_path=trusted_entrypoint_path,
            ),
        )
        _call_with_timeout(
            "runtime self attestation",
            _stage_timeout_seconds(
                deadline=deadline,
                stage_limit=RUNTIME_SELF_ATTESTATION_TIMEOUT_SECONDS,
                monotonic=monotonic,
            ),
            lambda: _runtime_self_attestation(
                trusted_entrypoint_path=trusted_entrypoint_path,
                verified=verified,
            ),
        )
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
    return execute(
        tuple(sys.argv[1:] if argv is None else argv),
        environ=os.environ,
        trusted_entrypoint_path=Path(integration_entrypoint.__file__),
        watchdog_runner=_run_watchdog_stage,
        verifier=verify_active_manifest_against_trusted_entrypoint,
        publisher_runner=_run_publisher_stage,
    )


if __name__ == "__main__":
    raise SystemExit(main())
