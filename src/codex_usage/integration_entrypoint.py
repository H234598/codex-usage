from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .history import HistoryStore, usage_samples_from_usage
from .integration_attestation import VerifiedActiveManifest
from .integration_evidence import (
    IntegrationBusy as EvidenceBusy,
)
from .integration_evidence import (
    publish_evidence_generation,
)
from .integration_snapshot import (
    IntegrationSnapshotError,
    IntegrationUnavailable,
    build_schema2_document,
    read_current_usage_records,
    serialize_schema2_document,
)
from .private_io import IntegrationEvidenceInvalid, IntegrationEvidenceUnavailable

_EXPECTED_ARGV = ("integration-snapshot", "--schema", "2", "--format", "json")
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
    )


def _require_aware_utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError()
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError()
    except Exception:
        raise ValueError() from None
    try:
        return value.astimezone(UTC)
    except Exception:
        raise ValueError() from None


def _error_result(code: int) -> CommandResult:
    if type(code) is not int or code not in _ERROR_TOKENS:
        code = 69
    return CommandResult(code, b"", _ERROR_TOKENS[code])


def _default_verifier() -> Callable[[Path, Path, Path], VerifiedActiveManifest]:
    try:
        from .integration_attestation import verify_active_manifest_at
    except Exception:

        def unavailable(_: Path, __: Path, ___: Path) -> VerifiedActiveManifest:
            raise IntegrationUnavailable()

        return unavailable

    def verify(
        state_home: Path,
        data_home: Path,
        expected_entrypoint_path: Path,
    ) -> VerifiedActiveManifest:
        return verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=expected_entrypoint_path,
        )

    return verify


def execute(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    clock: Callable[[], datetime],
    expected_entrypoint_path: Path,
    verifier: Callable[[Path, Path, Path], VerifiedActiveManifest],
) -> CommandResult:
    try:
        normalized_argv = tuple(argv)
    except Exception:
        return _error_result(64)
    if (
        len(normalized_argv) != len(_EXPECTED_ARGV)
        or any(type(value) is not str for value in normalized_argv)
        or normalized_argv != _EXPECTED_ARGV
    ):
        return _error_result(64)
    try:
        paths = _runtime_paths(environ)
        first = verifier(paths.state_home, paths.data_home, expected_entrypoint_path)
        generated_at = _require_aware_utc(clock())
        usages = read_current_usage_records(paths.current_dir)
        tracker_samples = _load_tracker_samples(paths.history_path, usages, generated_at)
        document = build_schema2_document(
            usages,
            generated_at=generated_at,
            tracker_samples=tracker_samples or None,
        )
        payload = serialize_schema2_document(document)
        second = verifier(paths.state_home, paths.data_home, expected_entrypoint_path)
        _require_matching_verified_manifests(first, second)
        publish_evidence_generation(
            payload,
            state_home=paths.state_home,
            data_home=paths.data_home,
            verified_active_manifest=second,
        )
        return CommandResult(0, payload, b"")
    except EvidenceBusy:
        return _error_result(75)
    except IntegrationEvidenceUnavailable:
        return _error_result(69)
    except IntegrationEvidenceInvalid:
        return _error_result(70)
    except IntegrationSnapshotError as exc:
        return _error_result(exc.exit_code)
    except TimeoutError:
        return _error_result(75)
    except (OSError, TypeError, ValueError):
        return _error_result(70)
    except Exception:
        return _error_result(69)


def _require_matching_verified_manifests(
    first: object,
    second: object,
) -> None:
    if (
        type(first) is not VerifiedActiveManifest
        or type(second) is not VerifiedActiveManifest
        or first.active_manifest_sha256
        != hashlib.sha256(first.active_manifest_bytes).hexdigest()
        or second.active_manifest_sha256
        != hashlib.sha256(second.active_manifest_bytes).hexdigest()
        or first != second
    ):
        raise IntegrationEvidenceUnavailable()


def _load_tracker_samples(
    history_path: Path,
    usages: tuple,
    now: datetime,
) -> dict[tuple[str, str, int], tuple]:
    if not history_path.is_file():
        return {}
    result: dict[tuple[str, str, int], tuple] = {}
    with HistoryStore(history_path) as store:
        for usage in usages:
            for sample in usage_samples_from_usage(usage):
                key = (sample.account_id, sample.pool, sample.window_seconds)
                if key in result:
                    continue
                samples = store.samples(
                    sample.account_id,
                    pool=sample.pool,
                    window_seconds=sample.window_seconds,
                    end=now,
                )
                if samples:
                    result[key] = samples
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
