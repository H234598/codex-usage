from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .history import HistoryStore, UsageSample, usage_samples_from_usage
from .integration_attestation import VerifiedActiveManifest
from .integration_evidence import (
    IntegrationBusy as EvidenceBusy,
)
from .integration_evidence import (
    _publish_evidence_generation_locked,
    evidence_lock_set,
)
from .integration_snapshot import (
    CurrentSourceSnapshot,
    IntegrationSnapshotError,
    IntegrationUnavailable,
    build_schema2_document,
    read_current_usage_records_with_binding,
    serialize_schema2_document,
)
from .private_io import IntegrationEvidenceInvalid, IntegrationEvidenceUnavailable
from .source_lock import SourceFileBinding, capture_private_source_file, source_lock
from .usage_limits import SPARK_MODEL

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


@dataclass(frozen=True)
class HistorySeriesBinding:
    account_id: str
    pool: str
    window_seconds: int
    sample_count: int
    rows_sha256: str

    def to_contract(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "rows_sha256": self.rows_sha256,
            "sample_count": self.sample_count,
            "pool": self.pool,
            "window_seconds": self.window_seconds,
        }


@dataclass(frozen=True)
class HistorySourceBinding:
    database: SourceFileBinding | None
    wal: SourceFileBinding | None
    shm: SourceFileBinding | None
    consumed_rows: tuple[HistorySeriesBinding, ...]
    has_spark_source_evidence: bool

    def to_contract(self) -> dict[str, object]:
        return {
            "consumed_rows": [row.to_contract() for row in self.consumed_rows],
            "database": None if self.database is None else self.database.to_contract(),
            "shm": None if self.shm is None else self.shm.to_contract(),
            "wal": None if self.wal is None else self.wal.to_contract(),
        }


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
        with source_lock(paths.current_dir.parent, timeout_seconds=0):
            with evidence_lock_set(
                state_home=paths.state_home,
                release_mode="exclusive",
                current_mode="exclusive",
                timeout_seconds=0,
                create=False,
            ):
                first = verifier(
                    paths.state_home,
                    paths.data_home,
                    expected_entrypoint_path,
                )
                generated_at = _require_aware_utc(clock())
                current_source = _read_current_source_snapshot(paths.current_dir)
                tracker_samples, history_source = _load_tracker_samples_with_binding(
                    paths.history_path,
                    current_source.usages,
                    generated_at,
                )
                _reject_spark_source_evidence(current_source, history_source)
                source_contract = _source_input_contract(
                    current_source,
                    history_source,
                )
                document = build_schema2_document(
                    current_source.usages,
                    generated_at=generated_at,
                    tracker_samples=tracker_samples or None,
                )
                payload = serialize_schema2_document(document)
                repeated_current_source = _read_current_source_snapshot(paths.current_dir)
                repeated_tracker_samples, repeated_history_source = (
                    _load_tracker_samples_with_binding(
                        paths.history_path,
                        repeated_current_source.usages,
                        generated_at,
                    )
                )
                _reject_spark_source_evidence(
                    repeated_current_source,
                    repeated_history_source,
                )
                if (
                    repeated_current_source != current_source
                    or repeated_history_source != history_source
                    or repeated_tracker_samples != tracker_samples
                    or _source_input_contract(
                        repeated_current_source,
                        repeated_history_source,
                    )
                    != source_contract
                ):
                    raise IntegrationEvidenceInvalid()
                second = verifier(
                    paths.state_home,
                    paths.data_home,
                    expected_entrypoint_path,
                )
                _require_matching_verified_manifests(first, second)
                _publish_evidence_generation_locked(
                    payload,
                    state_home=paths.state_home,
                    data_home=paths.data_home,
                    verified_active_manifest=second,
                    source_input_contract=source_contract,
                    source_input_revalidator=lambda: _revalidate_source_input_contract(
                        paths,
                        generated_at,
                    ),
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


def _read_current_source_snapshot(current_dir: Path) -> CurrentSourceSnapshot:
    return read_current_usage_records_with_binding(current_dir)


def _load_tracker_samples(
    history_path: Path,
    usages: tuple,
    now: datetime,
) -> dict[tuple[str, str, int], tuple]:
    samples, _ = _load_tracker_samples_with_binding(history_path, usages, now)
    return samples


def _load_tracker_samples_with_binding(
    history_path: Path,
    usages: tuple,
    now: datetime,
) -> tuple[dict[tuple[str, str, int], tuple], HistorySourceBinding]:
    before_database = _capture_optional_history_file(history_path)
    if before_database is None:
        return {}, HistorySourceBinding(None, None, None, (), False)
    result: dict[tuple[str, str, int], tuple] = {}
    with HistoryStore(history_path) as store:
        spark_source_evidence = store.has_pool_evidence(SPARK_MODEL)
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
    bindings = tuple(
        _history_series_binding(key, samples)
        for key, samples in sorted(result.items())
    )
    after_database = _capture_optional_history_file(history_path)
    return result, HistorySourceBinding(
        database=after_database,
        wal=_capture_optional_history_file(Path(f"{history_path}-wal")),
        shm=_capture_optional_history_file(Path(f"{history_path}-shm")),
        consumed_rows=bindings,
        has_spark_source_evidence=spark_source_evidence,
    )


def _capture_optional_history_file(path: Path) -> SourceFileBinding | None:
    if not path.exists() and not path.is_symlink():
        return None
    try:
        _, binding = capture_private_source_file(path, maximum=128 * 1024 * 1024)
        return binding
    except OSError as exc:
        raise IntegrationUnavailable() from exc
    except ValueError as exc:
        raise IntegrationEvidenceInvalid() from exc


def _history_series_binding(
    key: tuple[str, str, int],
    samples: tuple,
) -> HistorySeriesBinding:
    account_id, pool, window_seconds = key
    if not samples or any(type(sample) is not UsageSample for sample in samples):
        raise IntegrationEvidenceInvalid()
    rows: list[dict[str, object]] = []
    for sample in samples:
        if (
            sample.account_id != account_id
            or sample.pool != pool
            or sample.window_seconds != window_seconds
        ):
            raise IntegrationEvidenceInvalid()
        rows.append(
            {
                "captured_at": _require_aware_utc(sample.captured_at)
                .isoformat()
                .replace("+00:00", "Z"),
                "reset_at": (
                    None
                    if sample.reset_at is None
                    else _require_aware_utc(sample.reset_at)
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
                "reset_generation": sample.reset_generation,
                "source": sample.source,
                "used_percent": sample.used_percent,
            }
        )
    try:
        payload = json.dumps(
            rows,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise IntegrationEvidenceInvalid() from exc
    return HistorySeriesBinding(
        account_id=account_id,
        pool=pool,
        window_seconds=window_seconds,
        sample_count=len(rows),
        rows_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _source_input_contract(
    current: CurrentSourceSnapshot,
    history: HistorySourceBinding,
) -> dict[str, object]:
    if type(current) is not CurrentSourceSnapshot or type(history) is not HistorySourceBinding:
        raise IntegrationEvidenceInvalid()
    result = current.to_contract()
    result["history"] = history.to_contract()
    return result


def _revalidate_source_input_contract(
    paths: RuntimePaths,
    generated_at: datetime,
) -> dict[str, object]:
    current = _read_current_source_snapshot(paths.current_dir)
    _tracker_samples, history = _load_tracker_samples_with_binding(
        paths.history_path,
        current.usages,
        generated_at,
    )
    _reject_spark_source_evidence(current, history)
    return _source_input_contract(current, history)


def _reject_spark_source_evidence(
    current: CurrentSourceSnapshot,
    history: HistorySourceBinding,
) -> None:
    if current.has_spark_source_evidence or history.has_spark_source_evidence:
        from .integration_snapshot import IntegrationInvalidSource

        raise IntegrationInvalidSource()


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
