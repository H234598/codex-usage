from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .consumption import ConsumptionWindow, calculate_consumption
from .history import HistoryStore
from .integration_snapshot import (
    IntegrationSnapshotError,
    IntegrationUnavailable,
    build_schema1_document,
    publish_schema1_cache,
    read_current_usage_records,
    serialize_schema1_document,
)
from .private_io import private_path_lock

_EXPECTED_ARGV = ("integration-snapshot", "--schema", "1", "--format", "json")
_ERROR_TOKENS = {
    64: b"integration_snapshot_invalid_arguments\n",
    65: b"integration_snapshot_invalid_source\n",
    69: b"integration_snapshot_unavailable\n",
    70: b"integration_snapshot_secure_io_failed\n",
    75: b"integration_snapshot_busy\n",
}


@dataclass(frozen=True)
class RuntimePaths:
    data_home: Path
    state_home: Path
    current_dir: Path
    history_path: Path
    integration_dir: Path
    cache_path: Path
    release_lock_target: Path


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: bytes
    stderr: bytes


def _runtime_paths(environ: Mapping[str, str]) -> RuntimePaths:
    roots: dict[str, Path] = {}
    for name in ("XDG_DATA_HOME", "XDG_STATE_HOME"):
        value = environ.get(name)
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError()
        roots[name] = Path(value)
    data_home = roots["XDG_DATA_HOME"]
    state_home = roots["XDG_STATE_HOME"]
    integration_dir = state_home / "codex-usage" / "integration"
    return RuntimePaths(
        data_home=data_home,
        state_home=state_home,
        current_dir=data_home / "codex-usage" / "current",
        history_path=data_home / "codex-usage" / "usage-history.sqlite3",
        integration_dir=integration_dir,
        cache_path=integration_dir / "account-usage-v1.json",
        release_lock_target=integration_dir / "producer-install",
    )


def _require_aware_utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError()
    try:
        if value.utcoffset() is None:
            raise ValueError()
    except Exception:
        raise ValueError() from None
    try:
        return value.astimezone(UTC)
    except (OverflowError, TypeError, ValueError):
        raise ValueError() from None


def _error_result(code: int) -> CommandResult:
    if code not in _ERROR_TOKENS:
        code = 69
    return CommandResult(code, b"", _ERROR_TOKENS[code])


def _default_verifier() -> Callable[[Path, Path, Path], object]:
    try:
        from .integration_attestation import (
            IntegrationAttestationUnavailable,
            verify_active_release,
        )
    except Exception:

        def unavailable(_: Path, __: Path, ___: Path) -> None:
            raise IntegrationUnavailable()

        return unavailable

    def verify(
        state_home: Path,
        data_home: Path,
        expected_entrypoint_path: Path,
    ) -> object:
        try:
            return verify_active_release(
                state_home=state_home,
                data_home=data_home,
                expected_entrypoint_path=expected_entrypoint_path,
            )
        except IntegrationAttestationUnavailable:
            raise IntegrationUnavailable() from None

    return verify


def execute(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    clock: Callable[[], datetime],
    expected_entrypoint_path: Path,
    verifier: Callable[[Path, Path, Path], object],
) -> CommandResult:
    if tuple(argv) != _EXPECTED_ARGV:
        return _error_result(64)
    try:
        paths = _runtime_paths(environ)
        with private_path_lock(
            paths.release_lock_target,
            timeout_seconds=0,
            label="integration producer lock",
        ):
            verifier(paths.state_home, paths.data_home, expected_entrypoint_path)
            generated_at = _require_aware_utc(clock())
            usages = read_current_usage_records(paths.current_dir)
            costs = _load_cost_windows(paths.history_path, usages, generated_at)
            document = build_schema1_document(
                usages,
                generated_at=generated_at,
                cost_windows_by_account=costs or None,
            )
            payload = serialize_schema1_document(document)
            verifier(paths.state_home, paths.data_home, expected_entrypoint_path)
            publish_schema1_cache(payload, cache_path=paths.cache_path)
        return CommandResult(0, payload, b"")
    except IntegrationSnapshotError as exc:
        return _error_result(exc.exit_code)
    except TimeoutError:
        return _error_result(75)
    except (OSError, TypeError, ValueError):
        return _error_result(70)
    except Exception:
        return _error_result(69)


def _load_cost_windows(
    history_path: Path,
    usages: tuple,
    now: datetime,
) -> dict[str, tuple[ConsumptionWindow, ...]]:
    if not history_path.is_file():
        return {}
    result: dict[str, tuple[ConsumptionWindow, ...]] = {}
    with HistoryStore(history_path) as store:
        for usage in usages:
            windows: list[ConsumptionWindow] = []
            for duration in (18_000, 604_800):
                samples = store.samples_for_consumption(
                    usage.account_id,
                    pool="main",
                    window_seconds=duration,
                    start=now - timedelta(hours=1),
                    end=now,
                )
                cost = calculate_consumption(
                    samples,
                    amount=1,
                    unit="hours",
                    now=now,
                )
                if cost.limit_window_seconds == 0:
                    cost = replace(cost, limit_window_seconds=duration)
                windows.append(cost)
            result[usage.account_id] = tuple(windows)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    result = execute(
        tuple(sys.argv[1:] if argv is None else argv),
        environ=os.environ,
        clock=lambda: datetime.now(UTC),
        expected_entrypoint_path=Path(__file__),
        verifier=_default_verifier(),
    )
    stream = sys.stdout.buffer if result.exit_code == 0 else sys.stderr.buffer
    stream.write(result.stdout if result.exit_code == 0 else result.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
