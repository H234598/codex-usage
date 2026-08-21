from __future__ import annotations

import errno
import math
import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import chain, islice
from pathlib import Path

from .config import default_state_dir
from .models import AccountStatus, AccountUsage, LimitWindow, UsagePool
from .private_io import ensure_private_directory, private_path_lock

MAX_HISTORY_SAMPLES = 500_000
MAX_HISTORY_WINDOW_SECONDS = 2_592_000
HISTORY_SCHEMA_VERSION = "1"
CREDIT_HISTORY_WINDOW_SECONDS = 2_592_000


def default_history_path() -> Path:
    return default_state_dir() / "usage-history.sqlite3"


def _validated_history_path(path: Path | None) -> Path | None:
    if path is not None and not isinstance(path, Path):
        raise ValueError("history path is invalid")
    return path


def _validate_history_key(
    *,
    account_id: object,
    pool: object,
    window_seconds: object,
) -> None:
    if (
        not isinstance(account_id, str)
        or not account_id
        or len(account_id) > 64
        or any(
            char
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
            for char in account_id
        )
    ):
        raise ValueError("account_id is invalid")
    if not isinstance(pool, str) or not pool or len(pool) > 64:
        raise ValueError("pool is invalid")
    if not isinstance(window_seconds, int) or isinstance(window_seconds, bool):
        raise ValueError("window_seconds is invalid")
    if not 0 < window_seconds <= MAX_HISTORY_WINDOW_SECONDS:
        raise ValueError("window_seconds is invalid")


@dataclass(frozen=True)
class UsageSample:
    account_id: str
    pool: str
    window_seconds: int
    captured_at: datetime
    used_percent: float
    reset_at: datetime | None = None
    reset_generation: str | None = None
    source: str = "unknown"

    def __post_init__(self) -> None:
        _validate_history_key(
            account_id=self.account_id,
            pool=self.pool,
            window_seconds=self.window_seconds,
        )
        _require_aware(self.captured_at, "captured_at")
        if isinstance(self.used_percent, bool) or not isinstance(
            self.used_percent, (int, float)
        ):
            raise ValueError("used_percent is invalid")
        try:
            used_percent = float(self.used_percent)
        except (OverflowError, TypeError, ValueError):
            raise ValueError("used_percent is invalid") from None
        if not math.isfinite(used_percent) or not 0 <= used_percent <= 100:
            raise ValueError("used_percent is invalid")
        if self.reset_at is not None:
            _require_aware(self.reset_at, "reset_at")
        if self.reset_generation is not None and (
            not isinstance(self.reset_generation, str)
            or not self.reset_generation
            or len(self.reset_generation) > 128
            or not self.reset_generation.isascii()
        ):
            raise ValueError("reset_generation is invalid")
        if not isinstance(self.source, str) or not self.source or len(self.source) > 64:
            raise ValueError("source is invalid")


def _require_aware(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    try:
        value.astimezone(UTC)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is out of range") from exc


def _to_millis(value: datetime) -> int:
    return round(value.astimezone(UTC).timestamp() * 1000)


def _from_millis(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("history timestamp is invalid")
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise ValueError("history timestamp is invalid") from exc


def _validated_millis(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("history timestamp is invalid")
    _from_millis(value)
    return value


class HistoryStore:
    def __init__(self, path: Path | None = None):
        path = _validated_history_path(path)
        self.path = default_history_path() if path is None else path
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> HistoryStore:
        self._connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        # Lock file lives beside database; create/validate its parent first.
        self._prepare_path()
        with private_path_lock(self.path, label="history lock"):
            if self._connection is not None:
                return self._connection
            path, expected_stat = self._prepare_path()
            if not hasattr(os, "O_NOFOLLOW") or not Path("/proc/self/fd").is_dir():
                raise ValueError("history path cannot be opened safely")
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
            if expected_stat is None:
                flags |= os.O_EXCL
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            try:
                fd = os.open(path, flags, 0o600)
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.EEXIST):
                    raise ValueError("history path changed while opening") from exc
                if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
                    raise ValueError("history path must be a regular file") from exc
                raise
            connection: sqlite3.Connection | None = None
            try:
                file_stat = os.fstat(fd)
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or file_stat.st_uid != os.getuid()
                    or file_stat.st_nlink != 1
                    or stat.S_IMODE(file_stat.st_mode) != 0o600
                ):
                    raise ValueError("history path must be a private regular file")
                if expected_stat is not None and (
                    file_stat.st_dev != expected_stat.st_dev
                    or file_stat.st_ino != expected_stat.st_ino
                ):
                    raise ValueError("history path changed while opening")
                connection = sqlite3.connect(
                    f"/proc/self/fd/{fd}",
                    timeout=30,
                    isolation_level=None,
                )
                try:
                    path_stat = path.lstat()
                except OSError as exc:
                    raise ValueError("history path changed while opening") from exc
                if (
                    not stat.S_ISREG(path_stat.st_mode)
                    or path_stat.st_dev != file_stat.st_dev
                    or path_stat.st_ino != file_stat.st_ino
                    or path_stat.st_uid != os.getuid()
                    or path_stat.st_nlink != 1
                    or stat.S_IMODE(path_stat.st_mode) != 0o600
                ):
                    raise ValueError("history path changed while opening")
            except BaseException:
                if connection is not None:
                    connection.close()
                raise
            finally:
                os.close(fd)
            assert connection is not None
            connection.row_factory = sqlite3.Row
            try:
                try:
                    metadata_object = connection.execute(
                        "SELECT type FROM sqlite_master WHERE name = 'metadata'"
                    ).fetchone()
                    if metadata_object is None:
                        schema_object = connection.execute(
                            "SELECT 1 FROM sqlite_master LIMIT 1"
                        ).fetchone()
                        if schema_object is not None:
                            raise ValueError("unsupported history schema version")
                    else:
                        if metadata_object["type"] != "table":
                            raise ValueError("unsupported history schema version")
                        schema_row = connection.execute(
                            "SELECT value FROM metadata WHERE key = 'schema_version'"
                        ).fetchone()
                        if (
                            schema_row is None
                            or schema_row["value"] != HISTORY_SCHEMA_VERSION
                        ):
                            raise ValueError("unsupported history schema version")
                except sqlite3.DatabaseError as exc:
                    raise ValueError("unsupported history schema version") from exc
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA busy_timeout=30000")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS samples (
                        id INTEGER PRIMARY KEY,
                        account_id TEXT NOT NULL,
                        pool_key TEXT NOT NULL,
                        window_seconds INTEGER NOT NULL,
                        captured_at_ms INTEGER NOT NULL,
                        used_percent REAL NOT NULL,
                        reset_at_ms INTEGER,
                        reset_generation TEXT,
                        source TEXT NOT NULL,
                        UNIQUE(account_id, pool_key, window_seconds, captured_at_ms)
                    );
                    CREATE INDEX IF NOT EXISTS samples_lookup
                        ON samples(account_id, pool_key, window_seconds, captured_at_ms);
                    INSERT OR IGNORE INTO metadata(key, value)
                        VALUES ('schema_version', '1');
                    """
                )
                self._secure_related_files()
            except Exception:
                connection.close()
                raise
            self._connection = connection
            return connection

    def _prepare_path(self) -> tuple[Path, os.stat_result | None]:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("history path must be absolute")
        path = self.path
        parent = path.parent
        ensure_private_directory(parent, label="history parent")
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            file_stat = None
        except OSError as exc:
            raise ValueError("history path must be a regular file") from exc
        if file_stat is not None:
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError("history path must be a regular file")
            if file_stat.st_nlink != 1:
                raise ValueError("history path must not be hard-linked")
            if stat.S_IMODE(file_stat.st_mode) != 0o600:
                raise ValueError("history path must be private")
        return path, file_stat

    def _secure_related_files(self) -> None:
        for suffix in ("-wal", "-shm"):
            related = Path(str(self.path) + suffix)
            if related.exists() or related.is_symlink():
                _chmod_private_regular(related, label="history sidecar")
        _chmod_private_regular(self.path, label="history path")
    def record(self, sample: UsageSample) -> bool:
        return self.record_many((sample,)) == 1

    def record_many(self, samples: tuple[UsageSample, ...]) -> int:
        if not isinstance(samples, tuple):
            raise ValueError("samples are invalid")
        if len(samples) > MAX_HISTORY_SAMPLES:
            raise ValueError("too many samples")
        if any(not isinstance(sample, UsageSample) for sample in samples):
            raise ValueError("samples are invalid")
        if not samples:
            return 0
        connection = self._connect()
        with private_path_lock(self.path, label="history lock"):
            try:
                connection.execute("BEGIN")
                cursor = connection.executemany(
                    """
                    INSERT OR IGNORE INTO samples(
                        account_id, pool_key, window_seconds, captured_at_ms,
                        used_percent, reset_at_ms, reset_generation, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            sample.account_id,
                            sample.pool,
                            sample.window_seconds,
                            _to_millis(sample.captured_at),
                            float(sample.used_percent),
                            _to_millis(sample.reset_at) if sample.reset_at else None,
                            sample.reset_generation,
                            sample.source,
                        )
                        for sample in samples
                    ),
                )
                count = cursor.rowcount
                connection.commit()
            except Exception:
                try:
                    connection.rollback()
                except Exception:
                    pass
                raise
            self._secure_related_files()
        return count

    def samples(
        self,
        account_id: str,
        *,
        pool: str,
        window_seconds: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[UsageSample, ...]:
        _validate_history_key(
            account_id=account_id,
            pool=pool,
            window_seconds=window_seconds,
        )
        if start is not None:
            _require_aware(start, "start")
        if end is not None:
            _require_aware(end, "end")
        clauses = ["account_id = ?", "pool_key = ?", "window_seconds = ?"]
        parameters: list[object] = [account_id, pool, window_seconds]
        if start is not None:
            clauses.append("captured_at_ms >= ?")
            parameters.append(_to_millis(start))
        if end is not None:
            clauses.append("captured_at_ms <= ?")
            parameters.append(_to_millis(end))
        rows = self._connect().execute(
            "SELECT * FROM samples WHERE "
            + " AND ".join(clauses)
            + " ORDER BY captured_at_ms DESC LIMIT ?",
            [*parameters, MAX_HISTORY_SAMPLES],
        ).fetchall()
        rows.reverse()
        return tuple(_sample_from_row(row) for row in rows)

    def samples_for_consumption(
        self,
        account_id: str,
        *,
        pool: str,
        window_seconds: int,
        start: datetime,
        end: datetime,
    ) -> tuple[UsageSample, ...]:
        _validate_history_key(
            account_id=account_id,
            pool=pool,
            window_seconds=window_seconds,
        )
        _require_aware(start, "start")
        _require_aware(end, "end")
        if start > end:
            return ()
        parameters = (account_id, pool, window_seconds, _to_millis(start))
        connection = self._connect()
        baseline_row = connection.execute(
            "SELECT * FROM samples WHERE account_id = ? AND pool_key = ? "
            "AND window_seconds = ? AND captured_at_ms <= ? "
            "ORDER BY captured_at_ms DESC LIMIT 1",
            parameters,
        ).fetchone()
        observation_limit = max(
            0, MAX_HISTORY_SAMPLES - (1 if baseline_row is not None else 0)
        )
        observations = connection.execute(
            "SELECT * FROM samples WHERE account_id = ? AND pool_key = ? "
            "AND window_seconds = ? AND captured_at_ms > ? AND captured_at_ms <= ? "
            "ORDER BY captured_at_ms DESC LIMIT ?",
            (*parameters[:3], parameters[3], _to_millis(end), observation_limit),
        ).fetchall()
        observations.reverse()
        samples: list[UsageSample] = []
        if baseline_row is not None:
            samples.append(_sample_from_row(baseline_row))
        samples.extend(_sample_from_row(row) for row in observations)
        return tuple(samples)

    def prune(self, before: datetime, *, dry_run: bool = False) -> int:
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be boolean")
        _require_aware(before, "before")
        connection = self._connect()
        if dry_run:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM samples WHERE captured_at_ms < ?",
                (_to_millis(before),),
            ).fetchone()
            return int(row["count"])
        with private_path_lock(self.path, label="history lock"):
            count = connection.execute(
                "DELETE FROM samples WHERE captured_at_ms < ?",
                (_to_millis(before),),
            ).rowcount
            connection.commit()
            self._secure_related_files()
        return count

    def status(self) -> dict[str, int | str]:
        row = self._connect().execute(
            "SELECT COUNT(*) AS count, MIN(captured_at_ms) AS oldest, "
            "MAX(captured_at_ms) AS newest FROM samples"
        ).fetchone()
        return {
            "path": str(self.path),
            "schema_version": HISTORY_SCHEMA_VERSION,
            "sample_count": int(row["count"]),
            "oldest_captured_at_ms": (
                _validated_millis(row["oldest"]) if row["oldest"] is not None else 0
            ),
            "newest_captured_at_ms": (
                _validated_millis(row["newest"]) if row["newest"] is not None else 0
            ),
        }


def _chmod_private_regular(path: Path, *, label: str) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise ValueError(f"{label} must be a regular file") from exc
        raise
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"{label} must be a regular file")
        if file_stat.st_nlink != 1:
            raise ValueError(f"{label} must not be hard-linked")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _sample_from_row(row: sqlite3.Row) -> UsageSample:
    return UsageSample(
        account_id=row["account_id"],
        pool=row["pool_key"],
        window_seconds=row["window_seconds"],
        captured_at=_from_millis(row["captured_at_ms"]),
        used_percent=row["used_percent"],
        reset_at=_from_millis(row["reset_at_ms"]) if row["reset_at_ms"] is not None else None,
        reset_generation=row["reset_generation"],
        source=row["source"],
    )


def usage_samples_from_usage(usage: AccountUsage) -> tuple[UsageSample, ...]:
    samples = tuple(islice(_iter_usage_samples(usage), MAX_HISTORY_SAMPLES + 1))
    if len(samples) > MAX_HISTORY_SAMPLES:
        raise ValueError("too many samples")
    return samples


def _iter_usage_samples(usage: AccountUsage):
    if not isinstance(usage, AccountUsage):
        raise ValueError("usage is invalid")
    if usage.status != AccountStatus.OK or usage.stale or usage.cache_invalidated:
        return
    if not isinstance(usage.account_id, str) or not usage.account_id:
        return
    values_captured_at = usage.values_captured_at
    captured_at = (
        values_captured_at
        if isinstance(values_captured_at, datetime)
        and values_captured_at.tzinfo is not None
        and values_captured_at.utcoffset() is not None
        else usage.captured_at
    )
    if (
        not isinstance(captured_at, datetime)
        or captured_at.tzinfo is None
        or captured_at.utcoffset() is None
    ):
        return
    source = usage.backend_used if isinstance(usage.backend_used, str) else "unknown"
    main_pools = (usage.main,) if isinstance(usage.main, UsagePool) else ()
    try:
        model_pools = iter(usage.models) if usage.models is not None else iter(())
    except TypeError:
        model_pools = iter(())
    for pool in chain(main_pools, model_pools):
        if (
            not isinstance(pool, UsagePool)
            or pool.available is not True
            or not isinstance(pool.windows, tuple)
        ):
            continue
        for window in pool.windows:
            if not isinstance(window, LimitWindow):
                continue
            try:
                if not window.has_known_identity or window.remaining_percent is None:
                    continue
            except (AttributeError, TypeError, ValueError, OverflowError):
                continue
            duration = (
                window.duration_seconds
                if isinstance(window.duration_seconds, int)
                and not isinstance(window.duration_seconds, bool)
                and window.duration_seconds > 0
                else None
            ) or {
                "5h": 18_000,
                "5_hour": 18_000,
                "five_hour": 18_000,
                "w": 604_800,
                "week": 604_800,
                "weekly": 604_800,
            }.get(window.name.strip().casefold() if isinstance(window.name, str) else None)
            if duration is None:
                continue
            reset_at = (
                window.reset_at
                if isinstance(window.reset_at, datetime)
                and window.reset_at.tzinfo is not None
                and window.reset_at.utcoffset() is not None
                else None
            )
            try:
                yield UsageSample(
                    account_id=usage.account_id,
                    pool=pool.key,
                    window_seconds=duration,
                    captured_at=captured_at,
                    used_percent=100.0 - float(window.remaining_percent),
                    reset_at=reset_at,
                    reset_generation=reset_at.astimezone(UTC).isoformat()
                    if reset_at is not None
                    else None,
                    source=source or "unknown",
                )
            except (TypeError, ValueError, OverflowError):
                continue
    credit = usage.credits
    if isinstance(credit, LimitWindow):
        try:
            remaining_percent = credit.remaining_percent
        except (AttributeError, TypeError, ValueError, OverflowError):
            remaining_percent = None
        if remaining_percent is not None:
            duration = (
                credit.duration_seconds
                if isinstance(credit.duration_seconds, int)
                and not isinstance(credit.duration_seconds, bool)
                and credit.duration_seconds > 0
                else CREDIT_HISTORY_WINDOW_SECONDS
            )
            reset_at = (
                credit.reset_at
                if isinstance(credit.reset_at, datetime)
                and credit.reset_at.tzinfo is not None
                and credit.reset_at.utcoffset() is not None
                else None
            )
            try:
                yield UsageSample(
                    account_id=usage.account_id,
                    pool="credits",
                    window_seconds=duration,
                    captured_at=captured_at,
                    used_percent=100.0 - float(remaining_percent),
                    reset_at=reset_at,
                    reset_generation=reset_at.astimezone(UTC).isoformat()
                    if reset_at is not None
                    else None,
                    source=source or "unknown",
                )
            except (TypeError, ValueError, OverflowError):
                return


def record_usage_samples(usage: AccountUsage, *, path: Path | None = None) -> int:
    return record_usage_samples_batch((usage,), path=path)


def record_usage_samples_batch(
    usages: tuple[AccountUsage, ...], *, path: Path | None = None
) -> int:
    path = _validated_history_path(path)
    if not isinstance(usages, tuple):
        raise ValueError("usages are invalid")
    if any(not isinstance(usage, AccountUsage) for usage in usages):
        raise ValueError("usages are invalid")
    samples = tuple(
        islice(
            (
                sample
                for usage in usages
                for sample in _iter_usage_samples(usage)
            ),
            MAX_HISTORY_SAMPLES + 1,
        )
    )
    if len(samples) > MAX_HISTORY_SAMPLES:
        raise ValueError("too many samples")
    if not samples:
        return 0
    with HistoryStore(path) as store:
        return store.record_many(samples)
