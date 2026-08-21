import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

import codex_usage.history as history_module
from codex_usage.consumption import calculate_consumption
from codex_usage.history import HistoryStore, UsageSample
from codex_usage.models import AccountStatus, AccountUsage, LimitWindow, UsagePool


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
