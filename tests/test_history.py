import errno
import math
import os
import sqlite3
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from types import SimpleNamespace

import pytest

import codex_usage.history as history_module
from codex_usage.consumption import calculate_consumption
from codex_usage.history import HistoryStore, UsageSample, usage_samples_from_usage
from codex_usage.models import AccountStatus, AccountUsage, LimitWindow, UsagePool


class _BrokenInt(int):
    def __lt__(self, _other):
        raise RuntimeError("synthetic history integer marker")

    def __le__(self, _other):
        raise RuntimeError("synthetic history integer marker")

    def __truediv__(self, _other):
        raise RuntimeError("synthetic history integer marker")


class _BrokenFloat(float):
    def __float__(self):
        raise RuntimeError("synthetic history float marker")


def _sample(*, captured_at: datetime, used_percent: float, account_id: str = "alpha"):
    return UsageSample(
        account_id=account_id,
        pool="main",
        window_seconds=18_000,
        captured_at=captured_at,
        used_percent=used_percent,
        reset_at=None,
        reset_generation="a",
        source="test",
    )


@pytest.mark.parametrize("path", [[], "", "history.sqlite3", 1, object()])
def test_history_store_rejects_invalid_path_type(path):
    with pytest.raises(ValueError, match="history path is invalid"):
        HistoryStore(path)  # type: ignore[arg-type]


@pytest.mark.parametrize("path", [[], "", "history.sqlite3", 1, object()])
def test_record_usage_samples_batch_rejects_invalid_path_before_empty_shortcut(path):
    with pytest.raises(ValueError, match="history path is invalid"):
        history_module.record_usage_samples_batch((), path=path)  # type: ignore[arg-type]


def test_record_usage_samples_batch_rejects_relative_path_before_empty_shortcut():
    with pytest.raises(ValueError, match="history path must be absolute"):
        history_module.record_usage_samples_batch(
            (), path=Path("relative-history.sqlite3")
        )


@pytest.mark.parametrize("method", ("samples", "samples_for_consumption"))
@pytest.mark.parametrize(
    ("account_id", "pool", "window_seconds", "error"),
    (
        pytest.param("bad/account", "main", 18_000, "account_id", id="account-chars"),
        pytest.param("a" * 65, "main", 18_000, "account_id", id="account-long"),
        pytest.param("alpha", "", 18_000, "pool", id="pool-empty"),
        pytest.param("alpha", True, 18_000, "pool", id="pool-bool"),
        pytest.param("alpha", "p" * 65, 18_000, "pool", id="pool-long"),
        pytest.param("alpha", "main", True, "window_seconds", id="window-bool"),
        pytest.param("alpha", "main", 0, "window_seconds", id="window-zero"),
        pytest.param("alpha", "main", -1, "window_seconds", id="window-negative"),
        pytest.param(
            "alpha",
            "main",
            history_module.MAX_HISTORY_WINDOW_SECONDS + 1,
            "window_seconds",
            id="window-too-large",
        ),
        pytest.param(
            "alpha",
            "main",
            "18000",
            "window_seconds",
            id="window-string",
        ),
        pytest.param(
            "alpha",
            "main",
            10**100,
            "window_seconds",
            id="window-huge",
        ),
    ),
)
def test_history_queries_reject_invalid_keys_before_database_creation(
    tmp_path,
    method,
    account_id,
    pool,
    window_seconds,
    error,
):
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path)
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    kwargs = {"pool": pool, "window_seconds": window_seconds}
    if method == "samples_for_consumption":
        kwargs.update(start=now - timedelta(hours=1), end=now)

    with pytest.raises(ValueError, match=error):
        getattr(store, method)(account_id, **kwargs)

    assert not path.exists()


def test_history_store_is_private_and_idempotent(tmp_path):
    path = tmp_path / "state" / "codex-usage" / "usage-history.sqlite3"
    captured = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    sample = _sample(captured_at=captured, used_percent=12.5)

    with HistoryStore(path) as store:
        assert store.record(sample) is True
        assert store.record(sample) is False
        assert store.samples("alpha", pool="main", window_seconds=18_000) == (sample,)

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "captured_at",
            datetime.min.replace(tzinfo=timezone(timedelta(hours=14))),
        ),
        (
            "reset_at",
            datetime.max.replace(tzinfo=timezone(-timedelta(hours=14))),
        ),
    ],
)
def test_history_rejects_aware_timestamps_outside_utc_range(field, value):
    sample_kwargs = {
        "account_id": "alpha",
        "pool": "main",
        "window_seconds": 18_000,
        "captured_at": datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
        "used_percent": 1,
    }
    sample_kwargs[field] = value
    with pytest.raises(ValueError, match=field):
        UsageSample(**sample_kwargs)


class _RaisingTimezone(tzinfo):
    def utcoffset(self, _value):
        raise RuntimeError("synthetic timezone marker")


class _RaisingAstimezone(datetime):
    def astimezone(self, _tz=None):
        raise RuntimeError("synthetic astimezone marker")


def test_history_rejects_timezone_callbacks_that_raise():
    with pytest.raises(ValueError, match="captured_at"):
        UsageSample(
            account_id="alpha",
            pool="main",
            window_seconds=18_000,
            captured_at=datetime(2026, 8, 16, 10, 0, tzinfo=_RaisingTimezone()),
            used_percent=1,
        )


def test_usage_samples_skip_invalid_timezone_callbacks():
    captured = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=captured,
        values_captured_at=datetime(2026, 8, 16, 10, 0, tzinfo=_RaisingTimezone()),
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=(
                LimitWindow(
                    name="5h",
                    percent=75,
                    reset_at=datetime(2026, 8, 16, 11, 0, tzinfo=_RaisingTimezone()),
                ),
            ),
            availability_sources=("usage",),
        ),
        status=AccountStatus.OK,
        backend_used="direct",
    )

    samples = usage_samples_from_usage(usage)

    assert len(samples) == 1
    assert samples[0].captured_at == captured
    assert samples[0].reset_at is None


@pytest.mark.parametrize("field", ["main", "credits"])
def test_usage_samples_skip_reset_astimezone_callbacks_that_raise(field):
    from codex_usage.history import usage_samples_from_usage

    captured = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    broken_window = LimitWindow(
        name="5h" if field == "main" else "credits",
        percent=75,
        reset_at=_RaisingAstimezone(2026, 8, 16, 11, 0, tzinfo=UTC),
    )
    kwargs = {
        "main": UsagePool(
            key="main",
            display_name="Codex",
            windows=(broken_window,),
            availability_sources=("usage",),
        )
        if field == "main"
        else None,
        "credits": broken_window if field == "credits" else None,
    }
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=captured,
        status=AccountStatus.OK,
        backend_used="direct",
        **kwargs,
    )

    assert usage_samples_from_usage(usage) == ()


def test_history_store_rejects_unsupported_schema_without_rewriting_database(
    tmp_path,
):
    path = tmp_path / "history.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('schema_version', '2')"
        )
        connection.execute("CREATE TABLE future_samples (value TEXT NOT NULL)")
        connection.execute("INSERT INTO future_samples(value) VALUES ('preserve-me')")
    path.chmod(0o600)

    with pytest.raises(ValueError, match="unsupported history schema version"):
        with HistoryStore(path):
            pass

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        schema_version = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0]
        future_value = connection.execute(
            "SELECT value FROM future_samples"
        ).fetchone()[0]

    assert tables == {"metadata", "future_samples"}
    assert schema_version == "2"
    assert future_value == "preserve-me"


def test_history_store_rejects_nonempty_database_without_schema_version(tmp_path):
    path = tmp_path / "history.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated_data (value TEXT NOT NULL)")
        connection.execute("INSERT INTO unrelated_data(value) VALUES ('preserve-me')")
    path.chmod(0o600)

    with pytest.raises(ValueError, match="unsupported history schema version"):
        with HistoryStore(path):
            pass

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        value = connection.execute("SELECT value FROM unrelated_data").fetchone()[0]

    assert tables == {"unrelated_data"}
    assert value == "preserve-me"


def test_history_store_path_swap_does_not_modify_replacement_database(
    tmp_path, monkeypatch
):
    path = tmp_path / "history.sqlite3"
    original_path = tmp_path / "history-original.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    with HistoryStore(path):
        pass
    with sqlite3.connect(replacement) as connection:
        connection.execute(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('schema_version', '1')"
        )
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel(value) VALUES ('preserve-me')")
    replacement.chmod(0o600)
    real_connect = sqlite3.connect
    swapped = False

    def swap_before_sqlite_open(database, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            path.rename(original_path)
            path.symlink_to(replacement)
            swapped = True
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(history_module.sqlite3, "connect", swap_before_sqlite_open)

    with pytest.raises(ValueError, match="history path"):
        with HistoryStore(path):
            pass

    with real_connect(replacement) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        sentinel = connection.execute("SELECT value FROM sentinel").fetchone()[0]

    assert swapped is True
    assert tables == {"metadata", "sentinel"}
    assert sentinel == "preserve-me"


def test_history_store_regular_swap_before_fd_open_preserves_replacement(
    tmp_path, monkeypatch
):
    path = tmp_path / "history.sqlite3"
    original_path = tmp_path / "history-original.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    with HistoryStore(path):
        pass
    with sqlite3.connect(replacement) as connection:
        connection.execute(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('schema_version', '1')"
        )
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel(value) VALUES ('preserve-me')")
    replacement.chmod(0o600)
    real_open = history_module.os.open
    real_connect = sqlite3.connect
    swapped = False

    def swap_before_fd_open(candidate, flags, *args, **kwargs):
        nonlocal swapped
        if candidate == path and not swapped:
            path.rename(original_path)
            replacement.rename(path)
            swapped = True
        return real_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(history_module.os, "open", swap_before_fd_open)

    with pytest.raises(ValueError, match="history path"):
        with HistoryStore(path):
            pass

    with real_connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        sentinel = connection.execute("SELECT value FROM sentinel").fetchone()[0]

    assert swapped is True
    assert tables == {"metadata", "sentinel"}
    assert sentinel == "preserve-me"


def test_history_store_hardlink_added_during_connect_precedes_schema_writes(
    tmp_path, monkeypatch
):
    path = tmp_path / "history.sqlite3"
    hardlink = tmp_path / "history-copy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('schema_version', '1')"
        )
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel(value) VALUES ('preserve-me')")
    path.chmod(0o600)
    real_connect = sqlite3.connect
    linked = False

    def link_during_sqlite_open(database, *args, **kwargs):
        nonlocal linked
        if not linked:
            hardlink.hardlink_to(path)
            linked = True
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(history_module.sqlite3, "connect", link_during_sqlite_open)

    with pytest.raises(ValueError, match=r"hard-linked|history path"):
        with HistoryStore(path):
            pass

    with real_connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        sentinel = connection.execute("SELECT value FROM sentinel").fetchone()[0]

    assert linked is True
    assert path.stat().st_nlink == 2
    assert tables == {"metadata", "sentinel"}
    assert sentinel == "preserve-me"


def test_history_store_fails_closed_without_nofollow_support(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite3"
    monkeypatch.delattr(history_module.os, "O_NOFOLLOW")

    with pytest.raises(ValueError, match="history path cannot be opened safely"):
        with HistoryStore(path):
            pass

    assert not path.exists()


def test_history_handles_missing_optional_open_flags(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path)
    store.close()
    for attribute in ("O_CLOEXEC", "O_NONBLOCK"):
        monkeypatch.delattr(history_module.os, attribute, raising=False)

    with store:
        assert store.status()["sample_count"] == 0
    store.close()
    monkeypatch.delattr(history_module.os, "O_NOFOLLOW", raising=False)
    regular = tmp_path / "regular"
    regular.write_bytes(b"value")
    regular.chmod(0o600)
    history_module._chmod_private_regular(regular, label="history file")


def test_history_connection_initialization_uses_private_path_lock(tmp_path, monkeypatch):
    path = tmp_path / "usage-history.sqlite3"
    observed: list[tuple[object, dict[str, object]]] = []
    original_lock = history_module.private_path_lock

    def traced_lock(lock_path, **kwargs):
        observed.append((lock_path, kwargs))
        return original_lock(lock_path, **kwargs)

    monkeypatch.setattr(history_module, "private_path_lock", traced_lock)
    with HistoryStore(path):
        pass

    assert observed == [
        (
            path,
            {"label": "history lock"},
        )
    ]


def test_record_usage_samples_batch_commits_once(tmp_path, monkeypatch):
    path = tmp_path / "usage-history.sqlite3"
    captured = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=captured,
        values_captured_at=captured,
        main=UsagePool(
            key="main",
            display_name="main",
            windows=(
                LimitWindow(name="5h", percent=75, reset_at=captured + timedelta(hours=1)),
                LimitWindow(name="weekly", percent=40, reset_at=captured + timedelta(days=2)),
            ),
            availability_sources=("usage",),
        ),
        status=AccountStatus.OK,
        backend_used="direct",
    )
    transactions = []
    original_connect = HistoryStore._connect

    def traced_connect(store):
        connection = original_connect(store)
        connection.set_trace_callback(
            lambda statement: transactions.append(statement)
            if statement in {"BEGIN", "COMMIT", "ROLLBACK"}
            else None
        )
        return connection

    monkeypatch.setattr(HistoryStore, "_connect", traced_connect)

    assert history_module.record_usage_samples_batch((usage,), path=path) == 2
    assert transactions == ["BEGIN", "COMMIT"]


def test_usage_samples_skip_malformed_usage_containers():
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
        main=[],  # type: ignore[arg-type]
        models=None,  # type: ignore[arg-type]
        credits=[],  # type: ignore[arg-type]
        status=AccountStatus.OK,
        backend_used="direct",
    )

    assert usage_samples_from_usage(usage) == ()


def test_usage_samples_skip_pool_with_malformed_windows():
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
        main=UsagePool(
            key="main",
            display_name="Codex",
            windows=None,  # type: ignore[arg-type]
        ),
        status=AccountStatus.OK,
        backend_used="direct",
    )

    assert usage_samples_from_usage(usage) == ()


def test_history_record_many_uses_sqlite_batch_execution(tmp_path):
    path = tmp_path / "usage-history.sqlite3"
    captured = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    first = _sample(captured_at=captured, used_percent=10)
    second = _sample(captured_at=captured + timedelta(minutes=1), used_percent=20)
    individual_calls = []
    batch_calls = []

    with HistoryStore(path) as store:
        connection = store._connect()

        class TracedConnection:
            def execute(self, statement, *args):
                if statement.lstrip().startswith("INSERT"):
                    individual_calls.append(statement)
                return connection.execute(statement, *args)

            def executemany(self, statement, parameters):
                materialized = tuple(parameters)
                batch_calls.append((statement, materialized))
                return connection.executemany(statement, materialized)

            def __getattr__(self, name):
                return getattr(connection, name)

        store._connection = TracedConnection()
        assert store.record_many((first, first, second)) == 2
        assert len(store.samples("alpha", pool="main", window_seconds=18_000)) == 2

    assert individual_calls == []
    assert len(batch_calls) == 1
    assert len(batch_calls[0][1]) == 3


def test_history_store_rejects_hard_linked_database(tmp_path):
    directory = tmp_path / "state"
    target = directory / "target.sqlite3"
    path = directory / "usage-history.sqlite3"
    with HistoryStore(target):
        pass
    path.hardlink_to(target)

    with pytest.raises(ValueError, match="hard-linked"):
        with HistoryStore(path):
            pass


def test_history_store_rejects_hard_linked_sqlite_sidecar(tmp_path):
    path = tmp_path / "usage-history.sqlite3"
    store = HistoryStore(path)
    store._prepare_path()
    path.write_bytes(b"database")
    path.chmod(0o600)
    target = tmp_path / "sidecar-target"
    target.write_bytes(b"sidecar")
    target.chmod(0o600)
    sidecar = path.with_name(path.name + "-wal")
    sidecar.hardlink_to(target)

    with pytest.raises(ValueError, match="hard-linked"):
        store._secure_related_files()


@pytest.mark.skipif(not hasattr(os, "O_NONBLOCK"), reason="O_NONBLOCK unavailable")
def test_history_sidecar_regular_check_opens_fifo_nonblocking(tmp_path, monkeypatch):
    target = tmp_path / "history.sqlite3-wal"
    os.mkfifo(target)
    real_open = history_module.os.open
    flags_seen = []

    def guarded_open(path, flags, *args):
        if path == target:
            flags_seen.append(flags)
            assert flags & os.O_NONBLOCK
        return real_open(path, flags, *args)

    monkeypatch.setattr(history_module.os, "open", guarded_open)

    with pytest.raises(ValueError, match="history sidecar"):
        history_module._chmod_private_regular(target, label="history sidecar")

    assert flags_seen


def test_history_store_rejects_database_symlink_before_chmod(tmp_path):
    path = tmp_path / "usage-history.sqlite3"
    store = HistoryStore(path)
    store._prepare_path()
    target = tmp_path / "target.sqlite3"
    target.write_bytes(b"database")
    target.chmod(0o644)
    path.symlink_to(target)

    with pytest.raises(ValueError, match="regular file"):
        store._secure_related_files()

    assert target.stat().st_mode & 0o777 == 0o644


def test_history_store_keeps_account_and_window_isolation(tmp_path):
    path = tmp_path / "history.sqlite3"
    captured = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    with HistoryStore(path) as store:
        store.record(_sample(captured_at=captured, used_percent=1))
        store.record(_sample(captured_at=captured, used_percent=2, account_id="beta"))
        store.record(
            UsageSample(
                account_id="alpha",
                pool="main",
                window_seconds=604_800,
                captured_at=captured,
                used_percent=3,
                source="test",
            )
        )
        assert len(store.samples("alpha", pool="main", window_seconds=18_000)) == 1
        assert len(store.samples("beta", pool="main", window_seconds=18_000)) == 1
        assert len(store.samples("alpha", pool="main", window_seconds=604_800)) == 1


def test_history_samples_keep_newest_rows_with_bounded_materialization(
    tmp_path, monkeypatch
):
    path = tmp_path / "history.sqlite3"
    base = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(history_module, "MAX_HISTORY_SAMPLES", 2)

    with HistoryStore(path) as store:
        for offset in range(3):
            assert store.record(
                _sample(
                    captured_at=base + timedelta(minutes=offset),
                    used_percent=offset,
                )
            )
        samples = store.samples("alpha", pool="main", window_seconds=18_000)

    assert [sample.used_percent for sample in samples] == [1, 2]
    assert samples[0].captured_at < samples[1].captured_at


def test_history_record_many_rejects_batches_over_materialization_cap(
    tmp_path, monkeypatch
):
    path = tmp_path / "history.sqlite3"
    base = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(history_module, "MAX_HISTORY_SAMPLES", 2)
    samples = tuple(
        _sample(captured_at=base + timedelta(minutes=offset), used_percent=offset)
        for offset in range(3)
    )

    with HistoryStore(path) as store:
        with pytest.raises(ValueError, match="too many samples"):
            store.record_many(samples)
        assert store.samples("alpha", pool="main", window_seconds=18_000) == ()


def test_history_consumption_samples_keep_baseline_and_window_only(tmp_path):
    path = tmp_path / "history.sqlite3"
    base = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    with HistoryStore(path) as store:
        for offset, used_percent in ((-120, 1), (-60, 2), (-30, 3), (30, 4)):
            store.record(
                _sample(
                    captured_at=base + timedelta(minutes=offset),
                    used_percent=used_percent,
                )
            )
        samples = store.samples_for_consumption(
            "alpha",
            pool="main",
            window_seconds=18_000,
            start=base - timedelta(minutes=60),
            end=base,
        )

    assert [sample.used_percent for sample in samples] == [2, 3]
    assert [sample.captured_at for sample in samples] == [
        base - timedelta(minutes=60),
        base - timedelta(minutes=30),
    ]


def test_history_lists_distinct_consumption_windows_in_time_range(tmp_path):
    path = tmp_path / "history.sqlite3"
    base = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    with HistoryStore(path) as store:
        store.record(_sample(captured_at=base - timedelta(minutes=30), used_percent=1))
        store.record(
            UsageSample(
                account_id="alpha",
                pool="main",
                window_seconds=86_400,
                captured_at=base - timedelta(minutes=20),
                used_percent=2,
                reset_generation="a",
                source="test",
            )
        )
        store.record(
            UsageSample(
                account_id="beta",
                pool="main",
                window_seconds=172_800,
                captured_at=base - timedelta(minutes=10),
                used_percent=3,
                reset_generation="a",
                source="test",
            )
        )

        windows = store.consumption_window_seconds(
            "alpha",
            pool="main",
            start=base - timedelta(hours=1),
            end=base,
        )

    assert windows == (18_000, 86_400)


def test_history_consumption_samples_include_baseline_before_window(tmp_path):
    path = tmp_path / "history.sqlite3"
    base = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    with HistoryStore(path) as store:
        for offset, used_percent in ((-120, 1), (-90, 2), (-30, 3), (30, 4)):
            store.record(
                _sample(
                    captured_at=base + timedelta(minutes=offset),
                    used_percent=used_percent,
                )
            )
        samples = store.samples_for_consumption(
            "alpha",
            pool="main",
            window_seconds=18_000,
            start=base - timedelta(minutes=60),
            end=base,
        )

    assert [sample.used_percent for sample in samples] == [2, 3]
    assert all(sample.captured_at <= base for sample in samples)


def test_history_consumption_samples_keep_baseline_within_materialization_cap(
    tmp_path, monkeypatch
):
    path = tmp_path / "history.sqlite3"
    base = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(history_module, "MAX_HISTORY_SAMPLES", 2)

    with HistoryStore(path) as store:
        for offset, used_percent in ((-120, 10), (-30, 20), (-20, 30)):
            store.record(
                _sample(
                    captured_at=base + timedelta(minutes=offset),
                    used_percent=used_percent,
                )
            )
        samples = store.samples_for_consumption(
            "alpha",
            pool="main",
            window_seconds=18_000,
            start=base - timedelta(minutes=60),
            end=base,
        )

    assert [sample.used_percent for sample in samples] == [10, 30]


def test_history_consumption_samples_stay_within_consumption_cap(
    tmp_path, monkeypatch
):
    path = tmp_path / "history.sqlite3"
    base = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(history_module, "MAX_HISTORY_SAMPLES", 2)

    with HistoryStore(path) as store:
        for offset, used_percent in ((-70, 10), (-30, 20), (-10, 30)):
            store.record(
                _sample(
                    captured_at=base + timedelta(minutes=offset),
                    used_percent=used_percent,
                )
            )
        samples = store.samples_for_consumption(
            "alpha",
            pool="main",
            window_seconds=18_000,
            start=base - timedelta(minutes=60),
            end=base,
        )

    assert len(samples) == 2
    assert samples[-1].used_percent == 30
    result = calculate_consumption(
        samples,
        amount=1,
        unit="hours",
        now=base,
    )
    assert result.sample_count == 2


def test_history_prune_is_dry_run_then_applies(tmp_path):
    path = tmp_path / "history.sqlite3"
    old = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    new = old + timedelta(days=2)
    with HistoryStore(path) as store:
        store.record(_sample(captured_at=old, used_percent=1))
        store.record(_sample(captured_at=new, used_percent=2))
        cutoff = old + timedelta(days=1)
        assert store.prune(cutoff, dry_run=True) == 1
        assert len(store.samples("alpha", pool="main", window_seconds=18_000)) == 2
        assert store.prune(cutoff) == 1
        assert store.samples("alpha", pool="main", window_seconds=18_000)[0].captured_at == new


@pytest.mark.parametrize("dry_run", [[], "false", 1, object()])
def test_history_prune_rejects_invalid_dry_run_type(tmp_path, dry_run):
    with HistoryStore(tmp_path / "history.sqlite3") as store:
        with pytest.raises(ValueError, match="dry_run must be boolean"):
            store.prune(datetime(2026, 8, 16, tzinfo=UTC), dry_run=dry_run)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "used_percent",
    [pytest.param(101, id="over-100"), pytest.param(10**10_000, id="float-overflow")],
)
def test_history_rejects_invalid_sample(tmp_path, used_percent):
    with pytest.raises(ValueError, match="used_percent"):
        UsageSample(
            account_id="alpha",
            pool="main",
            window_seconds=18_000,
            captured_at=datetime(2026, 8, 16, tzinfo=UTC),
            used_percent=used_percent,
        )


@pytest.mark.parametrize("value", [10**100, -10**100, "invalid", None, True, 1.5])
def test_history_rejects_malformed_millis(value):
    with pytest.raises(ValueError, match="history timestamp"):
        history_module._from_millis(value)


def test_history_rejects_numeric_subclasses_before_arithmetic():
    broken_int = _BrokenInt(1_000)
    broken_float = _BrokenFloat(10.0)

    with pytest.raises(ValueError, match="window_seconds"):
        UsageSample(
            account_id="alpha",
            pool="main",
            window_seconds=broken_int,
            captured_at=datetime(2026, 8, 16, tzinfo=UTC),
            used_percent=10.0,
        )
    with pytest.raises(ValueError, match="used_percent"):
        UsageSample(
            account_id="alpha",
            pool="main",
            window_seconds=18_000,
            captured_at=datetime(2026, 8, 16, tzinfo=UTC),
            used_percent=broken_float,
        )
    for helper in (history_module._from_millis, history_module._validated_millis):
        with pytest.raises(ValueError, match="history timestamp"):
            helper(broken_int)


def test_history_validated_millis_accepts_representable_timestamp():
    assert history_module._validated_millis(0) == 0


def test_history_consumption_samples_without_baseline(tmp_path):
    path = tmp_path / "history.sqlite3"
    base = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    with HistoryStore(path) as store:
        store.record(_sample(captured_at=base + timedelta(minutes=10), used_percent=4))
        samples = store.samples_for_consumption(
            "alpha",
            pool="main",
            window_seconds=18_000,
            start=base,
            end=base + timedelta(minutes=20),
        )
    assert [sample.used_percent for sample in samples] == [4]


@pytest.mark.parametrize("captured_at_ms", ["invalid", 1.5])
def test_history_status_rejects_malformed_timestamp_aggregate(tmp_path, captured_at_ms):
    path = tmp_path / "history.sqlite3"
    with HistoryStore(path):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO samples("
            "account_id, pool_key, window_seconds, captured_at_ms, used_percent, "
            "reset_at_ms, reset_generation, source"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("alpha", "main", 18_000, captured_at_ms, 10, None, None, "test"),
        )
        connection.commit()

    with pytest.raises(ValueError, match="history timestamp"):
        with HistoryStore(path) as store:
            store.status()


def test_usage_samples_extract_only_fresh_valid_limit_windows():
    from codex_usage.history import usage_samples_from_usage

    captured = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=captured,
        values_captured_at=captured,
        main=UsagePool(
            key="main",
            display_name="main",
            windows=(
                LimitWindow(name="5h", percent=75, reset_at=captured + timedelta(hours=1)),
                LimitWindow(name="weekly", percent=40, reset_at=captured + timedelta(days=2)),
            ),
            availability_sources=("usage",),
        ),
        credits=LimitWindow(name="credits", percent=70, reset_at=captured + timedelta(days=30)),
        status=AccountStatus.OK,
        backend_used="direct",
    )
    samples = usage_samples_from_usage(usage)
    assert [(sample.pool, sample.window_seconds, sample.used_percent) for sample in samples] == [
        ("main", 18_000, 25.0),
        ("main", 604_800, 60.0),
        ("credits", history_module.CREDIT_HISTORY_WINDOW_SECONDS, 30.0),
    ]


@pytest.mark.parametrize("remaining", [0, 12, 80, 100, 100.01, 794])
def test_usage_samples_omit_denominatorless_absolute_credit(remaining):
    captured = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=captured,
        credits=LimitWindow(name="credits", remaining=remaining),
        status=AccountStatus.OK,
        backend_used="direct",
    )

    assert usage_samples_from_usage(usage) == ()


@pytest.mark.parametrize(
    "credits",
    [
        pytest.param(
            LimitWindow(name="credits", used=101, limit=100),
            id="pair-used-limit",
        ),
        pytest.param(
            LimitWindow(name="credits", used=20, percent=70),
            id="pair-used-percent",
        ),
        pytest.param(
            LimitWindow(name="credits", used=20, remaining=80),
            id="pair-used-remaining-without-denominator",
        ),
        pytest.param(
            LimitWindow(name="credits", used=20, limit=100, remaining=70),
            id="triple-used-limit-remaining",
        ),
        pytest.param(
            LimitWindow(name="credits", used=20, limit=100, percent=70),
            id="triple-used-limit-percent",
        ),
        pytest.param(
            LimitWindow(name="credits", remaining=80, limit=100, percent=70),
            id="triple-remaining-limit-percent",
        ),
        pytest.param(
            LimitWindow(name="credits", used=20, remaining=70, percent=70),
            id="triple-without-limit",
        ),
        pytest.param(
            LimitWindow(
                name="credits",
                used=20,
                limit=100,
                remaining=70,
                percent=70,
            ),
            id="quad",
        ),
        pytest.param(
            LimitWindow(
                name="credits",
                used=20,
                limit=100,
                percent=80.0000000001,
            ),
            id="beyond-float-rounding",
        ),
    ],
)
def test_usage_samples_skip_conflicting_explicit_credit_fields(credits):
    captured = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=captured,
        credits=credits,
        status=AccountStatus.OK,
        backend_used="direct",
    )

    assert usage_samples_from_usage(usage) == ()


@pytest.mark.parametrize(
    ("credits", "expected_used_percent"),
    [
        pytest.param(LimitWindow(name="credits", percent=80), 20, id="percent"),
        pytest.param(
            LimitWindow(name="credits", used=20, limit=100),
            20,
            id="used-limit",
        ),
        pytest.param(
            LimitWindow(name="credits", remaining=80, limit=100),
            20,
            id="remaining-limit",
        ),
        pytest.param(
            LimitWindow(
                name="credits",
                used=20,
                remaining=80,
                limit=100,
                percent=80,
            ),
            20,
            id="quad",
        ),
        pytest.param(
            LimitWindow(
                name="credits",
                used=0.1 + 0.2,
                remaining=0.7,
                limit=1.0,
                percent=70,
            ),
            30,
            id="float-rounding",
        ),
        pytest.param(
            LimitWindow(
                name="credits",
                used=math.nextafter(1.0, 0.0),
                remaining=1.0 - math.nextafter(1.0, 0.0),
                limit=1.0,
                percent=(1.0 - math.nextafter(1.0, 0.0)) * 100.0,
            ),
            100.0 - (1.0 - math.nextafter(1.0, 0.0)) * 100.0,
            id="nextafter-quad",
        ),
    ],
)
def test_usage_samples_preserve_consistent_explicit_credit_fields(
    credits,
    expected_used_percent,
):
    captured = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=captured,
        credits=credits,
        status=AccountStatus.OK,
        backend_used="direct",
    )

    samples = usage_samples_from_usage(usage)

    assert len(samples) == 1
    assert samples[0].pool == "credits"
    assert samples[0].used_percent == pytest.approx(expected_used_percent)


def test_usage_samples_fall_back_for_integer_subclass_credit_duration():
    captured = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=captured,
        credits=LimitWindow(
            name="credits",
            percent=70,
            duration_seconds=_BrokenInt(3_600),
        ),
        status=AccountStatus.OK,
        backend_used="direct",
    )

    samples = usage_samples_from_usage(usage)

    assert [(sample.pool, sample.window_seconds) for sample in samples] == [
        ("credits", history_module.CREDIT_HISTORY_WINDOW_SECONDS)
    ]


def test_usage_samples_reject_string_status_even_when_value_matches():
    captured = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=captured,
        main=UsagePool(
            key="main",
            display_name="main",
            windows=(LimitWindow(name="5h", percent=75),),
            availability_sources=("usage",),
        ),
        status="ok",  # type: ignore[arg-type]
        backend_used="direct",
    )

    assert history_module.usage_samples_from_usage(usage) == ()


@pytest.mark.parametrize("name", ["30d", "30_day", "month", "monthly"])
def test_usage_samples_extract_monthly_window_without_explicit_duration(name):
    captured = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=captured,
        main=UsagePool(
            key="main",
            display_name="main",
            windows=(LimitWindow(name=name, percent=40),),
            availability_sources=("usage",),
        ),
        status=AccountStatus.OK,
        backend_used="direct",
    )

    samples = usage_samples_from_usage(usage)

    assert [(sample.window_seconds, sample.used_percent) for sample in samples] == [
        (history_module.MAX_HISTORY_WINDOW_SECONDS, 60.0)
    ]


def test_usage_samples_reject_overlong_model_iterator_before_consuming_all(
    monkeypatch,
):
    monkeypatch.setattr(history_module, "MAX_HISTORY_SAMPLES", 2)
    captured = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    consumed = 0

    def model_pools():
        nonlocal consumed
        for index in range(10):
            consumed += 1
            yield UsagePool(
                key=f"model-{index}",
                display_name="synthetic",
                windows=(
                    LimitWindow(
                        name="5h",
                        percent=75,
                        duration_seconds=18_000,
                    ),
                ),
                availability_sources=("usage",),
            )

    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=captured,
        models=model_pools(),
        status=AccountStatus.OK,
        backend_used="direct",
    )

    with pytest.raises(ValueError, match="too many samples"):
        history_module.usage_samples_from_usage(usage)

    assert consumed == 3


def test_record_usage_samples_batch_rejects_combined_cap_before_next_batch(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(history_module, "MAX_HISTORY_SAMPLES", 2)
    captured = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    consumed = [0, 0, 0]

    def usage(account_id, position, pool_count):
        def model_pools():
            for index in range(pool_count):
                consumed[position] += 1
                yield UsagePool(
                    key=f"model-{position}-{index}",
                    display_name="synthetic",
                    windows=(
                        LimitWindow(
                            name="5h",
                            percent=75,
                            duration_seconds=18_000,
                        ),
                    ),
                    availability_sources=("usage",),
                )

        return AccountUsage(
            account_id=account_id,
            label=account_id,
            captured_at=captured,
            models=model_pools(),
            status=AccountStatus.OK,
            backend_used="direct",
        )

    with pytest.raises(ValueError, match="too many samples"):
        history_module.record_usage_samples_batch(
            (
                usage("alpha", 0, 1),
                usage("beta", 1, 1),
                usage("gamma", 2, 10),
            ),
            path=tmp_path / "history.sqlite3",
        )

    assert consumed == [1, 1, 1]


def test_default_history_path_uses_default_state_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(history_module, "default_state_dir", lambda: tmp_path)

    assert history_module.default_history_path() == tmp_path / "usage-history.sqlite3"


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("reset_generation", "x" * 129, "reset_generation"),
        ("reset_generation", "ä", "reset_generation"),
        ("source", "", "source"),
        ("source", "x" * 65, "source"),
    ),
)
def test_history_sample_rejects_malformed_metadata(field, value, error):
    kwargs = {
        "account_id": "alpha",
        "pool": "main",
        "window_seconds": 18_000,
        "captured_at": datetime(2026, 8, 16, 10, tzinfo=UTC),
        "used_percent": 10,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=error):
        UsageSample(**kwargs)


def test_history_connect_returns_connection_created_inside_lock(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path)
    sentinel = object()

    class _RaceLock:
        def __enter__(self):
            store._connection = sentinel  # type: ignore[assignment]
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(history_module, "private_path_lock", lambda *_args, **_kwargs: _RaceLock())

    assert store._connect() is sentinel


@pytest.mark.parametrize(
    ("error_number", "expected"),
    (
        (errno.ENOENT, "history path changed while opening"),
        (errno.EEXIST, "history path changed while opening"),
        (errno.ELOOP, "history path must be a regular file"),
        (errno.EISDIR, "history path must be a regular file"),
        (errno.ENXIO, "history path must be a regular file"),
        (errno.EACCES, "Permission denied"),
    ),
)
def test_history_connect_classifies_open_errors(
    tmp_path, monkeypatch, error_number, expected
):
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path)
    monkeypatch.setattr(
        history_module,
        "private_path_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(HistoryStore, "_prepare_path", lambda _self: (path, None))

    def fail_open(*_args, **_kwargs):
        raise OSError(error_number, os.strerror(error_number))

    monkeypatch.setattr(history_module.os, "open", fail_open)
    with pytest.raises((ValueError, OSError), match=expected):
        store._connect()


def test_history_connect_rejects_nonregular_descriptor(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite3"
    path.parent.mkdir(exist_ok=True)
    store = HistoryStore(path)
    monkeypatch.setattr(
        history_module,
        "private_path_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(HistoryStore, "_prepare_path", lambda _self: (path, None))
    monkeypatch.setattr(
        history_module.os,
        "fstat",
        lambda _fd: SimpleNamespace(
            st_mode=0,
            st_uid=os.getuid(),
            st_nlink=1,
            st_dev=1,
            st_ino=1,
        ),
    )

    with pytest.raises(ValueError, match="private regular file"):
        store._connect()


def test_history_connect_rejects_path_lstat_failure_after_sqlite_open(
    tmp_path, monkeypatch
):
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path)
    monkeypatch.setattr(
        history_module,
        "private_path_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(HistoryStore, "_prepare_path", lambda _self: (path, None))
    original_lstat = Path.lstat

    def fail_target_lstat(candidate):
        if candidate == path:
            raise OSError("synthetic lstat marker")
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", fail_target_lstat)

    with pytest.raises(ValueError, match="history path changed while opening"):
        store._connect()


def test_history_connect_rejects_metadata_view(tmp_path):
    path = tmp_path / "history.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE VIEW metadata AS SELECT 1 AS value")
    path.chmod(0o600)

    with pytest.raises(ValueError, match="unsupported history schema version"):
        HistoryStore(path)._connect()


def test_history_connect_translates_schema_database_error(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path)
    monkeypatch.setattr(
        history_module,
        "private_path_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(HistoryStore, "_prepare_path", lambda _self: (path, None))

    class _BrokenConnection:
        row_factory = None

        def execute(self, *_args, **_kwargs):
            raise sqlite3.DatabaseError("synthetic schema marker")

        def close(self):
            return None

    monkeypatch.setattr(
        history_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: _BrokenConnection(),
    )

    with pytest.raises(ValueError, match="unsupported history schema version"):
        store._connect()


def test_history_prepare_path_rejects_relative_path(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3")
    store.path = Path("relative-history.sqlite3")

    with pytest.raises(ValueError, match="history path must be absolute"):
        store._prepare_path()


def test_history_prepare_path_translates_lstat_error(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path)
    original_lstat = Path.lstat

    def fail_target_lstat(candidate):
        if candidate == path:
            raise PermissionError("synthetic path marker")
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", fail_target_lstat)

    with pytest.raises(ValueError, match="history path must be a regular file"):
        store._prepare_path()


@pytest.mark.parametrize("kind", ["directory", "non_private"])
def test_history_prepare_path_rejects_invalid_existing_file(tmp_path, kind):
    path = tmp_path / "history.sqlite3"
    if kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"history")
        path.chmod(0o644)
    store = HistoryStore(path)

    with pytest.raises(ValueError, match="history path"):
        store._prepare_path()


def test_history_record_many_validates_shape_and_empty_batch(tmp_path):
    with HistoryStore(tmp_path / "history.sqlite3") as store:
        with pytest.raises(ValueError, match="samples are invalid"):
            store.record_many([])  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="samples are invalid"):
            store.record_many((object(),))  # type: ignore[arg-type]
        assert store.record_many(()) == 0


def test_history_record_many_reraises_after_rollback_failure(tmp_path):
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path)
    connection = store._connect()

    class _BrokenConnection:
        def execute(self, *args, **kwargs):
            return connection.execute(*args, **kwargs)

        def executemany(self, *_args, **_kwargs):
            raise RuntimeError("synthetic insert marker")

        def rollback(self):
            raise ValueError("synthetic rollback marker")

        def __getattr__(self, name):
            return getattr(connection, name)

    store._connection = _BrokenConnection()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="synthetic insert marker"):
        store.record_many((_sample(captured_at=datetime(2026, 8, 16, tzinfo=UTC), used_percent=1),))


def test_history_queries_apply_aware_start_and_end_bounds(tmp_path):
    path = tmp_path / "history.sqlite3"
    base = datetime(2026, 8, 16, 10, tzinfo=UTC)
    sample = _sample(captured_at=base, used_percent=10)
    with HistoryStore(path) as store:
        store.record(sample)
        assert store.samples(
            "alpha",
            pool="main",
            window_seconds=18_000,
            start=base - timedelta(minutes=1),
            end=base + timedelta(minutes=1),
        ) == (sample,)


def test_history_consumption_and_window_queries_reject_reversed_range(tmp_path):
    base = datetime(2026, 8, 16, 10, tzinfo=UTC)
    with HistoryStore(tmp_path / "history.sqlite3") as store:
        assert store.samples_for_consumption(
            "alpha",
            pool="main",
            window_seconds=18_000,
            start=base,
            end=base - timedelta(minutes=1),
        ) == ()
        assert store.consumption_window_seconds(
            "alpha",
            pool="main",
            start=base,
            end=base - timedelta(minutes=1),
        ) == ()


def test_history_samples_reject_naive_start_and_end(tmp_path):
    base = datetime(2026, 8, 16, 10, tzinfo=UTC)
    with HistoryStore(tmp_path / "history.sqlite3") as store:
        with pytest.raises(ValueError, match="start"):
            store.samples(
                "alpha",
                pool="main",
                window_seconds=18_000,
                start=base.replace(tzinfo=None),
            )
        with pytest.raises(ValueError, match="end"):
            store.samples(
                "alpha",
                pool="main",
                window_seconds=18_000,
                end=base.replace(tzinfo=None),
            )


def test_history_sidecar_open_reraises_unclassified_os_error(tmp_path, monkeypatch):
    target = tmp_path / "history.sqlite3-wal"

    def fail_open(*_args, **_kwargs):
        raise OSError(errno.EACCES, "synthetic sidecar marker")

    monkeypatch.setattr(history_module.os, "open", fail_open)
    with pytest.raises(OSError, match="synthetic sidecar marker"):
        history_module._chmod_private_regular(target, label="history sidecar")


def test_usage_samples_reject_invalid_usage_account_and_capture_time():
    captured = datetime(2026, 8, 16, 10, tzinfo=UTC)
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=captured,
        main=UsagePool(
            key="main",
            display_name="main",
            windows=(LimitWindow(name="5h", percent=75),),
            availability_sources=("usage",),
        ),
        status=AccountStatus.OK,
        backend_used="direct",
    )
    with pytest.raises(ValueError, match="usage is invalid"):
        tuple(history_module._iter_usage_samples(None))  # type: ignore[arg-type]
    assert usage_samples_from_usage(replace(usage, account_id="")) == ()
    assert usage_samples_from_usage(replace(usage, captured_at=captured.replace(tzinfo=None))) == ()


def test_usage_samples_skip_non_iterable_models_and_non_window_entries():
    captured = datetime(2026, 8, 16, 10, tzinfo=UTC)
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=captured,
        main=UsagePool(
            key="main",
            display_name="main",
            windows=(object(),),  # type: ignore[arg-type]
            availability_sources=("usage",),
        ),
        models=object(),  # type: ignore[arg-type]
        status=AccountStatus.OK,
        backend_used="direct",
    )

    assert usage_samples_from_usage(usage) == ()


def test_usage_samples_skip_window_identity_and_duration_failures():
    class _ExplodingIdentityWindow(LimitWindow):
        @property
        def has_known_identity(self):
            raise AttributeError("synthetic identity marker")

    class _TrustedUnknownWindow(LimitWindow):
        @property
        def has_known_identity(self):
            return True

    captured = datetime(2026, 8, 16, 10, tzinfo=UTC)
    windows = (
        _ExplodingIdentityWindow(name="5h", percent=75),
        LimitWindow(name="5h"),
        _TrustedUnknownWindow(name="mystery", percent=75),
    )
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=captured,
        main=UsagePool(
            key="main",
            display_name="main",
            windows=windows,
            availability_sources=("usage",),
        ),
        status=AccountStatus.OK,
        backend_used="direct",
    )

    assert usage_samples_from_usage(usage) == ()


def test_usage_samples_skip_credit_validation_failure():
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=datetime(2026, 8, 16, 10, tzinfo=UTC),
        credits=LimitWindow(name="credits", percent="invalid"),  # type: ignore[arg-type]
        status=AccountStatus.OK,
        backend_used="direct",
    )

    assert usage_samples_from_usage(usage) == ()


def test_record_usage_sample_wrappers_validate_batch_and_empty_results(tmp_path):
    usage = AccountUsage(
        account_id="alpha",
        label="Alpha",
        captured_at=datetime(2026, 8, 16, 10, tzinfo=UTC),
        status=AccountStatus.PARTIAL,
    )
    path = tmp_path / "history.sqlite3"
    assert history_module.record_usage_samples(usage, path=path) == 0
    with pytest.raises(ValueError, match="usages are invalid"):
        history_module.record_usage_samples_batch([], path=path)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="usages are invalid"):
        history_module.record_usage_samples_batch((object(),), path=path)  # type: ignore[arg-type]
