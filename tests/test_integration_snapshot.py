from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path

import pytest

import codex_usage.integration_snapshot as snapshot_module
from codex_usage.models import AccountStatus, AccountUsage, LimitWindow, UsagePool
from codex_usage.private_io import write_private_text
from codex_usage.usage_resets import UsageResetState

FIXTURES = Path(__file__).parent / "fixtures" / "integration_snapshot"
CAPTURED_ALPHA = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
GENERATED = datetime(2026, 8, 15, 10, 5, tzinfo=UTC)
RESET = datetime(2026, 8, 15, 15, 0, tzinfo=UTC)


class _RaisingTimezone(tzinfo):
    def utcoffset(self, _value):
        raise RuntimeError("synthetic timezone marker")


class _BrokenInt(int):
    def __lt__(self, _other):
        raise RuntimeError("synthetic snapshot integer comparison marker")

    def __le__(self, _other):
        raise RuntimeError("synthetic snapshot integer comparison marker")

    def __gt__(self, _other):
        raise RuntimeError("synthetic snapshot integer comparison marker")

    def __float__(self):
        raise RuntimeError("synthetic snapshot integer conversion marker")


class _BrokenFloat(float):
    def __float__(self):
        raise RuntimeError("synthetic snapshot float conversion marker")


def test_cost_window_contract_matches_history_limit_and_producer_coverages():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        _canonical_cost_window,
    )

    value = {
        "lookback_seconds": 3600,
        "pool": "main",
        "limit_window_seconds": 18_000,
        "consumed_percentage_points": 12.5,
        "coverage": "complete",
        "sample_count": 500_000,
    }
    assert _canonical_cost_window(value)["sample_count"] == 500_000
    for coverage in ("invalid", "unknown", None, [], {}):
        with pytest.raises(IntegrationInvalidSource):
            _canonical_cost_window({**value, "coverage": coverage})


@pytest.mark.parametrize(
    ("helper", "value"),
    [
        pytest.param("percent", 10**10_000, id="percent-huge-int"),
        pytest.param("cost", 10**10_000, id="cost-huge-int"),
    ],
)
def test_schema1_canonical_float_helpers_reject_overflow(helper, value):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        _canonical_cost,
        _canonical_percent,
    )

    canonicalize = _canonical_percent if helper == "percent" else _canonical_cost
    with pytest.raises(IntegrationInvalidSource):
        canonicalize(value)


@pytest.mark.parametrize("value", [None, [], {}, "invalid", 1, True])
def test_schema1_projection_rejects_malformed_generated_timestamp(value):
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema1_document

    with pytest.raises(IntegrationInvalidSource):
        build_schema1_document((), generated_at=value)  # type: ignore[arg-type]


def test_schema1_projection_rejects_timezone_callbacks_that_raise():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, _utc_text

    with pytest.raises(IntegrationInvalidSource):
        _utc_text(datetime(2026, 8, 15, 10, 0, tzinfo=_RaisingTimezone()))


def test_schema1_canonical_timestamp_rejects_string_subclass_hooks():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, _canonical_timestamp

    class BrokenStr(str):
        def __contains__(self, _value):
            raise RuntimeError("synthetic snapshot timestamp marker")

    with pytest.raises(IntegrationInvalidSource):
        _canonical_timestamp(BrokenStr("2026-08-15T10:00:00Z"))


def test_schema1_canonical_token_rejects_string_subclass_hooks():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, _canonical_token

    class BrokenStr(str):
        def __len__(self):
            raise RuntimeError("synthetic snapshot token marker")

    with pytest.raises(IntegrationInvalidSource):
        _canonical_token(BrokenStr("alpha"), maximum=64)


@pytest.mark.parametrize("value", [None, [], {}, "invalid", 1, True])
def test_schema1_projection_rejects_malformed_usage_timestamp(value):
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema1_document

    with pytest.raises(IntegrationInvalidSource):
        build_schema1_document(
            (replace(_usage("alpha"), captured_at=value),),  # type: ignore[arg-type]
            generated_at=GENERATED,
        )


def _usage(
    account_id: str,
    *,
    captured_at: datetime = CAPTURED_ALPHA,
    stale: bool = False,
    status: AccountStatus = AccountStatus.OK,
) -> AccountUsage:
    main = UsagePool(
        key="main",
        display_name="synthetic only",
        windows=(LimitWindow(name="5h", percent=75.0, duration_seconds=18_000),),
        availability_sources=("usage",),
    )
    return AccountUsage(
        account_id=account_id,
        label="never-exported-label",
        captured_at=captured_at,
        main=main,
        status=status,
        backend_configured="direct",
        backend_used="direct",
        stale=stale,
        source_urls=("https://never-export.example.invalid",),
        backend_user_id="never-exported-user",
    )


def _write_current_fixture(current_dir: Path, usage: AccountUsage) -> Path:
    current_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    current_dir.chmod(0o700)
    payload = usage.as_dict()
    payload["state_generation"] = 0
    path = current_dir / f"{usage.account_id}.json"
    write_private_text(path, json.dumps(payload, sort_keys=True), label="synthetic state")
    return path


def _cache_path(tmp_path: Path) -> Path:
    directory = tmp_path / "state" / "codex-usage" / "integration"
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    return directory / "account-usage-v1.json"


def _pool(key: str, windows: tuple[LimitWindow, ...]) -> UsagePool:
    return UsagePool(
        key=key,
        display_name="synthetic only",
        windows=windows,
        availability_sources=("usage",),
    )


def test_schema1_numeric_boundaries_reject_subclasses_before_operations():
    broken_int = _BrokenInt(1)
    broken_float = _BrokenFloat(50.0)

    for helper, value in (
        (snapshot_module._canonical_percent, broken_float),
        (snapshot_module._canonical_cost, broken_float),
    ):
        with pytest.raises(snapshot_module.IntegrationInvalidSource):
            helper(value)
    with pytest.raises(snapshot_module.IntegrationInvalidSource):
        snapshot_module._canonical_int(broken_int, maximum=100)

    class BrokenLimitWindow(LimitWindow):
        @property
        def remaining_percent(self):
            return broken_float

    assert snapshot_module._pool_windows(
        _pool("main", (BrokenLimitWindow(name="5h", duration_seconds=18_000),))
    ) == []

    cost_window = {
        "lookback_seconds": 3600,
        "pool": "main",
        "limit_window_seconds": 18_000,
        "consumed_percentage_points": 12.5,
        "coverage": "complete",
        "sample_count": broken_int,
    }
    with pytest.raises(snapshot_module.IntegrationInvalidSource):
        snapshot_module._canonical_cost_window(cost_window)

    with pytest.raises(snapshot_module.IntegrationInvalidSource):
        snapshot_module._canonical_document(
            {
                "schema_version": broken_int,
                "generated_at": "2026-08-15T10:05:00Z",
                "accounts": [],
            }
        )
    with pytest.raises(snapshot_module.IntegrationInvalidSource):
        snapshot_module._canonical_document(
            {
                "schema_version": 1,
                "generated_at": "2026-08-15T10:05:00Z",
                "accounts": [
                    {
                        "account_id": "alpha",
                        "status": "ok",
                        "freshness": {
                            "captured_at": "2026-08-15T10:00:00Z",
                            "stale": False,
                        },
                        "usage_resets": {
                            "available": broken_int,
                            "known": True,
                            "redeem_capability": False,
                        },
                    }
                ],
            }
        )


def _usage_with_pools(pools: tuple[UsagePool, ...]) -> AccountUsage:
    return replace(_usage("alpha"), main=None, models=pools)


def test_schema1_projection_is_sorted_allowlisted_and_deterministic(tmp_path):
    from codex_usage.integration_snapshot import (
        build_schema1_document,
        read_current_usage_records,
        serialize_schema1_document,
    )

    current = tmp_path / "data" / "codex-usage" / "current"
    _write_current_fixture(
        current,
        _usage(
            "zeta",
            captured_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
            stale=True,
            status=AccountStatus.PARTIAL,
        ),
    )
    _write_current_fixture(current, _usage("alpha"))
    document = build_schema1_document(read_current_usage_records(current), generated_at=GENERATED)
    assert json.loads(serialize_schema1_document(document)) == json.loads(
        (FIXTURES / "schema1-valid.json").read_text(encoding="utf-8")
    )
    encoded = serialize_schema1_document(document).decode("utf-8")
    for marker in (
        "never-exported-label",
        "never-exported-user",
        "example.invalid",
        "direct",
    ):
        assert marker not in encoded


@pytest.mark.parametrize(
    "remaining",
    [
        object(),
        -1.0,
        101.0,
        float("nan"),
        pytest.param(10**10_000, id="huge-int"),
    ],
)
def test_schema1_projection_skips_unusable_remaining_values(remaining):
    from codex_usage.integration_snapshot import build_schema1_document

    class BrokenLimitWindow(LimitWindow):
        @property
        def remaining_percent(self):
            return remaining

    usage = _usage_with_pools(
        (
            _pool(
                "main",
                (BrokenLimitWindow(name="5h", duration_seconds=18_000),),
            ),
        )
    )
    document = build_schema1_document((usage,), generated_at=GENERATED)
    assert "limits" not in document["accounts"][0]


def test_schema1_projection_exports_custom_limit_windows():
    from codex_usage.integration_snapshot import build_schema1_document

    usage = _usage_with_pools(
        (
            _pool(
                "main",
                (LimitWindow(name="1d", remaining=75, duration_seconds=86_400),),
            ),
        )
    )

    document = build_schema1_document((usage,), generated_at=GENERATED)

    assert document["accounts"][0]["limits"] == [
        {
            "pool": "main",
            "window_seconds": 86_400,
            "used_percent": 25.0,
            "remaining_percent": 75.0,
        }
    ]


def test_schema1_projection_rejects_unhashable_pool_key_without_raising():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema1_document

    usage = _usage_with_pools(
        (replace(_pool("main", (LimitWindow(name="5h", remaining=75),)), key=[]),)
    )

    with pytest.raises(IntegrationInvalidSource):
        build_schema1_document((usage,), generated_at=GENERATED)


def test_schema1_projection_exports_sanitized_cost_windows():
    from codex_usage.consumption import ConsumptionWindow
    from codex_usage.integration_snapshot import build_schema1_document, serialize_schema1_document

    cost = ConsumptionWindow(
        lookback_seconds=172_800,
        pool="main",
        limit_window_seconds=18_000,
        consumed_percentage_points=125.5,
        coverage="partial",
        sample_count=4,
        estimated_seconds_to_exhaustion=None,
    )
    document = build_schema1_document(
        (_usage("alpha"),),
        generated_at=GENERATED,
        cost_windows_by_account={"alpha": (cost,)},
    )
    payload = json.loads(serialize_schema1_document(document))
    assert payload["accounts"][0]["cost_windows"] == [cost.as_dict()]


def test_schema1_projection_rejects_broken_cost_window_converter():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema1_document

    class NonCallable:
        as_dict = object()

    class Raising:
        @property
        def as_dict(self):
            raise RuntimeError("synthetic converter failure")

    for item in (NonCallable(), Raising()):
        with pytest.raises(IntegrationInvalidSource):
            build_schema1_document(
                (_usage("alpha"),),
                generated_at=GENERATED,
                cost_windows_by_account={"alpha": (item,)},
            )


def test_schema1_projection_rejects_oversized_cost_window_input_before_copying():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema1_document

    with pytest.raises(IntegrationInvalidSource):
        build_schema1_document(
            (_usage("alpha"),),
            generated_at=GENERATED,
            cost_windows_by_account={"alpha": [None] * 65},
        )


def test_schema1_projection_exports_usage_reset_state():
    from codex_usage.integration_snapshot import build_schema1_document

    usage = replace(_usage("alpha"), usage_resets=UsageResetState(2, True, True))
    document = build_schema1_document((usage,), generated_at=GENERATED)

    assert document["accounts"][0]["usage_resets"] == {
        "available": 2,
        "known": True,
        "redeem_capability": True,
    }


def test_current_reader_ignores_private_lock_and_temporary_files(tmp_path):
    from codex_usage.integration_snapshot import read_current_usage_records

    current = tmp_path / "data" / "codex-usage" / "current"
    _write_current_fixture(current, _usage("alpha"))
    for path in (
        current / "alpha.json.lock",
        current / ".alpha.json.tmp-123-secret",
    ):
        path.write_text("transient", encoding="utf-8")
        path.chmod(0o600)

    assert [item.account_id for item in read_current_usage_records(current)] == ["alpha"]


def test_current_reader_rejects_foreign_owner_identity(tmp_path, monkeypatch):
    from codex_usage import integration_snapshot
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        read_current_usage_records,
    )

    current = tmp_path / "data" / "codex-usage" / "current"
    _write_current_fixture(current, _usage("alpha"))
    monkeypatch.setattr(integration_snapshot.os, "getuid", lambda: 2**31 - 1)

    with pytest.raises(IntegrationInvalidSource):
        read_current_usage_records(current)


def test_current_reader_bounds_transient_directory_entries(tmp_path, monkeypatch):
    from codex_usage import integration_snapshot
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        read_current_usage_records,
    )

    current = tmp_path / "data" / "codex-usage" / "current"
    current.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(integration_snapshot, "_MAX_DIRECTORY_ENTRIES", 1)
    original_iterdir = Path.iterdir

    def bounded_entries(path):
        if path != current:
            return original_iterdir(path)

        def entries():
            yield path / "alpha.json.lock"
            yield path / ".alpha.json.tmp-123-secret"
            pytest.fail("directory iterator was consumed past transient entry cap")

        return entries()

    monkeypatch.setattr(Path, "iterdir", bounded_entries)
    with pytest.raises(IntegrationInvalidSource):
        read_current_usage_records(current)


def test_secret_scan_does_not_classify_short_dotted_account_id_as_jwt():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, _scan_secrets

    _scan_secrets("team.prod.eu")
    with pytest.raises(IntegrationInvalidSource):
        _scan_secrets(
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )


def test_schema_serializer_rejects_oversized_accounts_before_secret_scan(monkeypatch):
    from codex_usage import integration_snapshot
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema1_document,
    )

    def unexpected_secret_scan(_value):
        pytest.fail("secret scan must not traverse oversized account list")

    monkeypatch.setattr(integration_snapshot, "_scan_secrets", unexpected_secret_scan)

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema1_document(
            {"accounts": [None] * (integration_snapshot._MAX_ACCOUNTS + 1)}
        )


def test_source_failures_do_not_publish_or_mutate_existing_state(tmp_path, monkeypatch):
    from codex_usage import state
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        read_current_usage_records,
    )

    current = tmp_path / "data" / "codex-usage" / "current"
    state_file = _write_current_fixture(current, _usage("alpha"))
    state_file.chmod(0o644)
    monkeypatch.setattr(
        state,
        "save_current_usage",
        lambda *args, **kwargs: pytest.fail("state write"),
    )
    monkeypatch.setattr(
        state,
        "save_usage_snapshot",
        lambda *args, **kwargs: pytest.fail("snapshot write"),
    )
    with pytest.raises(IntegrationInvalidSource):
        read_current_usage_records(current)
    assert not _cache_path(tmp_path).exists()
    assert state_file.stat().st_mode & 0o777 == 0o644


def test_secret_marker_rejects_before_cache_replace(tmp_path, monkeypatch):
    from codex_usage import integration_snapshot

    cache = _cache_path(tmp_path)
    cache.write_text('{"old":"safe"}', encoding="utf-8")
    monkeypatch.setattr(
        integration_snapshot,
        "write_private_text",
        lambda *args, **kwargs: pytest.fail("cache write"),
    )
    cache.chmod(0o600)
    candidate = (FIXTURES / "schema1-secret-marker.json").read_bytes()
    with pytest.raises(integration_snapshot.IntegrationInvalidSource):
        integration_snapshot.publish_schema1_cache(candidate, cache_path=cache)
    assert cache.read_text(encoding="utf-8") == '{"old":"safe"}'


def test_read_current_usage_records_rejects_symlink_source(tmp_path):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        read_current_usage_records,
    )

    current = tmp_path / "data" / "codex-usage" / "current"
    path = _write_current_fixture(current, _usage("alpha"))
    path.unlink()
    path.symlink_to("other.json")
    with pytest.raises(IntegrationInvalidSource):
        read_current_usage_records(current)
    assert not _cache_path(tmp_path).exists()


def test_read_current_usage_records_rejects_group_readable_source(tmp_path):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        read_current_usage_records,
    )

    current = tmp_path / "data" / "codex-usage" / "current"
    path = _write_current_fixture(current, _usage("alpha"))
    path.chmod(0o640)
    with pytest.raises(IntegrationInvalidSource):
        read_current_usage_records(current)
    assert not _cache_path(tmp_path).exists()


def test_read_current_usage_records_rejects_hardlinked_source_and_missing_current(tmp_path):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        IntegrationUnavailable,
        read_current_usage_records,
    )

    missing = tmp_path / "missing" / "current"
    with pytest.raises(IntegrationUnavailable):
        read_current_usage_records(missing)
    current = tmp_path / "data" / "codex-usage" / "current"
    path = _write_current_fixture(current, _usage("alpha"))
    (current / "alpha-copy.json").hardlink_to(path)
    with pytest.raises(IntegrationInvalidSource):
        read_current_usage_records(current)
    assert not _cache_path(tmp_path).exists()


def test_read_current_usage_records_stops_collecting_after_account_cap(tmp_path, monkeypatch):
    from codex_usage import integration_snapshot
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        read_current_usage_records,
    )

    current = tmp_path / "data" / "codex-usage" / "current"
    current.mkdir(parents=True, mode=0o700)
    original_iterdir = Path.iterdir
    monkeypatch.setattr(integration_snapshot, "_MAX_ACCOUNTS", 1)

    def bounded_entries(path):
        if path != current:
            return original_iterdir(path)

        def entries():
            yield path / "first.json"
            yield path / "second.json"
            pytest.fail("directory iterator was consumed past account cap")

        return entries()

    monkeypatch.setattr(Path, "iterdir", bounded_entries)
    with pytest.raises(IntegrationInvalidSource):
        read_current_usage_records(current)


def test_publish_schema1_cache_keeps_old_bytes_when_replace_fails(tmp_path, monkeypatch):
    from codex_usage import integration_snapshot

    cache = _cache_path(tmp_path)
    cache.write_bytes(b'{"old":"safe"}')
    cache.chmod(0o600)
    calls: list[tuple[object, object]] = []

    def fail_replace(*args):
        calls.append(args)
        raise OSError("synthetic")

    monkeypatch.setattr("codex_usage.private_io.os.replace", fail_replace)
    with pytest.raises(integration_snapshot.IntegrationSecureIOError):
        integration_snapshot.publish_schema1_cache(
            b'{"accounts":[],"generated_at":"2026-08-15T10:05:00Z","schema_version":1}',
            cache_path=cache,
        )
    assert len(calls) == 1
    assert cache.read_bytes() == b'{"old":"safe"}'


def test_publish_schema1_cache_rejects_bytes_subclass_before_decode(tmp_path):
    from codex_usage import integration_snapshot

    class BrokenBytes(bytes):
        def decode(self, *_args, **_kwargs):
            raise RuntimeError("synthetic snapshot bytes marker")

    cache = _cache_path(tmp_path)
    payload = b'{"accounts":[],"generated_at":"2026-08-15T10:05:00Z","schema_version":1}'

    with pytest.raises(integration_snapshot.IntegrationInvalidSource):
        integration_snapshot.publish_schema1_cache(
            BrokenBytes(payload),
            cache_path=cache,
        )
    assert not cache.exists()


def test_publish_rejects_foreign_integration_directory(tmp_path, monkeypatch):
    from codex_usage import integration_snapshot
    from codex_usage.integration_snapshot import IntegrationSecureIOError

    cache = _cache_path(tmp_path)
    monkeypatch.setattr(integration_snapshot.os, "getuid", lambda: 2**31 - 1)
    payload = integration_snapshot.serialize_schema1_document(
        json.loads((FIXTURES / "schema1-valid.json").read_bytes())
    )

    with pytest.raises(IntegrationSecureIOError):
        integration_snapshot.publish_schema1_cache(
            payload,
            cache_path=cache,
        )


def test_validate_existing_cache_rejects_foreign_owner(tmp_path, monkeypatch):
    from codex_usage import integration_snapshot
    from codex_usage.integration_snapshot import IntegrationSecureIOError

    cache = _cache_path(tmp_path)
    cache.write_bytes(b"cache")
    cache.chmod(0o600)
    monkeypatch.setattr(integration_snapshot.os, "getuid", lambda: 2**31 - 1)

    with pytest.raises(IntegrationSecureIOError):
        integration_snapshot._validate_existing_cache(cache)


def test_projection_rejects_duplicate_identity_and_never_uses_label():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema1_document

    with pytest.raises(IntegrationInvalidSource):
        build_schema1_document((_usage("alpha"), _usage("alpha")), generated_at=GENERATED)


def test_serialization_is_bounded_and_secret_free_for_valid_fixture(tmp_path):
    from codex_usage.integration_snapshot import build_schema1_document, serialize_schema1_document

    payload = serialize_schema1_document(
        build_schema1_document((_usage("alpha"),), generated_at=GENERATED)
    )
    assert len(payload) <= 2 * 1024 * 1024
    assert b"token" not in payload.lower()
    assert b"profile" not in payload.lower()


def test_projection_rejects_more_than_32_aggregated_limits_across_pools():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema1_document

    pools = tuple(
        _pool(
            f"pool-{index:02d}",
            (LimitWindow(name="5h", percent=75.0, duration_seconds=18_000),),
        )
        for index in range(33)
    )
    with pytest.raises(IntegrationInvalidSource):
        build_schema1_document((_usage_with_pools(pools),), generated_at=GENERATED)


def test_projection_accepts_exactly_32_unique_aggregated_limits():
    from codex_usage.integration_snapshot import build_schema1_document

    pools = tuple(
        _pool(
            f"pool-{index:02d}",
            (LimitWindow(name="5h", percent=75.0, duration_seconds=18_000),),
        )
        for index in range(32)
    )
    document = build_schema1_document((_usage_with_pools(pools),), generated_at=GENERATED)
    assert len(document["accounts"][0]["limits"]) == 32


def test_projection_rejects_too_many_model_pools_before_materialization():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        build_schema1_document,
    )

    pools = tuple(_pool(f"pool-{index:02d}", ()) for index in range(33))

    with pytest.raises(IntegrationInvalidSource):
        build_schema1_document((_usage_with_pools(pools),), generated_at=GENERATED)


def test_projection_rejects_duplicate_canonical_limit_identity():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema1_document

    duplicate = LimitWindow(
        name="5h",
        percent=75.0,
        duration_seconds=18_000,
        reset_at=RESET,
    )
    pools = (_pool("main", (duplicate, duplicate)),)
    with pytest.raises(IntegrationInvalidSource):
        build_schema1_document((_usage_with_pools(pools),), generated_at=GENERATED)


def test_serialization_rejects_boolean_schema_version():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema1_document,
    )

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema1_document(
            {
                "schema_version": True,
                "generated_at": "2026-08-15T10:05:00Z",
                "accounts": [],
            }
        )


def test_serialization_converts_mapping_callback_failures_to_invalid_source():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema1_document,
    )

    class RaisingMapping(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError("synthetic mapping failure")

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema1_document(RaisingMapping())


def test_serialization_accepts_true_integer_schema_version():
    from codex_usage.integration_snapshot import serialize_schema1_document

    assert serialize_schema1_document(
        {
            "schema_version": 1,
            "generated_at": "2026-08-15T10:05:00Z",
            "accounts": [],
        }
    ) == b'{"accounts":[],"generated_at":"2026-08-15T10:05:00Z","schema_version":1}'


@pytest.mark.parametrize("status", [[], {}])
def test_canonical_document_rejects_unhashable_status(status):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        _canonical_document,
    )

    with pytest.raises(IntegrationInvalidSource):
        _canonical_document(
            {
                "schema_version": 1,
                "generated_at": "2026-08-15T10:05:00Z",
                "accounts": [
                    {
                        "account_id": "alpha",
                        "status": status,
                        "freshness": {
                            "captured_at": "2026-08-15T10:00:00Z",
                            "stale": False,
                        },
                    }
                ],
            }
        )


def test_snapshot_path_helpers_fail_closed_on_path_and_owner_errors(tmp_path, monkeypatch):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        IntegrationUnavailable,
        _directory_identity,
        _safe_account_filename,
        _source_file_identity,
    )

    assert _safe_account_filename(tmp_path / "missing.json") is None
    invalid_name = tmp_path / "bad name.json"
    invalid_name.write_text("{}", encoding="utf-8")
    invalid_name.chmod(0o600)
    assert _safe_account_filename(invalid_name) is None

    with pytest.raises(IntegrationUnavailable):
        _directory_identity(tmp_path / "not-a-directory")
    with pytest.raises(IntegrationInvalidSource):
        _source_file_identity(tmp_path)
    with pytest.raises(IntegrationInvalidSource):
        _source_file_identity(tmp_path / "missing.json")

    def raise_value(*_args, **_kwargs):
        raise ValueError("synthetic path marker")

    monkeypatch.setattr(snapshot_module, "assert_no_symlink_ancestors", raise_value)
    with pytest.raises(IntegrationInvalidSource):
        _directory_identity(tmp_path)

    def raise_os(*_args, **_kwargs):
        raise OSError("synthetic path marker")

    monkeypatch.setattr(snapshot_module, "assert_no_symlink_ancestors", raise_os)
    with pytest.raises(IntegrationUnavailable):
        _directory_identity(tmp_path)

    monkeypatch.setattr(snapshot_module, "assert_no_symlink_ancestors", lambda *_a, **_k: None)
    original_lstat = Path.lstat

    def failing_lstat(path):
        if path == tmp_path:
            raise OSError("synthetic lstat marker")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", failing_lstat)
    with pytest.raises(IntegrationUnavailable):
        _directory_identity(tmp_path)


def test_snapshot_current_reader_rejects_relative_and_iterator_failures(tmp_path, monkeypatch):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        IntegrationUnavailable,
        read_current_usage_records,
    )

    with pytest.raises(IntegrationInvalidSource):
        read_current_usage_records(Path("relative/current"))

    current = tmp_path / "current"
    current.mkdir(mode=0o700)
    original_iterdir = Path.iterdir

    def failing_iterdir(path):
        if path == current:
            raise OSError("synthetic iterator marker")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", failing_iterdir)
    with pytest.raises(IntegrationUnavailable):
        read_current_usage_records(current)


def test_snapshot_current_reader_rejects_directory_identity_races(tmp_path, monkeypatch):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        read_current_usage_records,
    )

    current = tmp_path / "current"
    current.mkdir(mode=0o700)
    identity = (1, 2, 0o700)

    identities = iter((identity, (1, 3, 0o700)))
    monkeypatch.setattr(snapshot_module, "_directory_identity", lambda _path: next(identities))
    with pytest.raises(IntegrationInvalidSource):
        read_current_usage_records(current)

    _write_current_fixture(current, _usage("alpha"))
    identities = iter((identity, identity, (1, 3, 0o700)))
    monkeypatch.setattr(snapshot_module, "_directory_identity", lambda _path: next(identities))
    with pytest.raises(IntegrationInvalidSource):
        read_current_usage_records(current)


@pytest.mark.parametrize("failure", ["load", "after-load", "file", "type", "final"])
def test_snapshot_current_reader_rejects_record_races_and_load_failures(
    tmp_path, monkeypatch, failure
):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        read_current_usage_records,
    )

    current = tmp_path / "current"
    _write_current_fixture(current, _usage("alpha"))
    identity = (1, 2, 0o700)
    directory_calls = {
        "load": (identity, identity, identity),
        "after-load": (identity, identity, identity, (1, 3, 0o700)),
        "file": (identity, identity, identity, identity, identity),
        "type": (identity, identity, identity, identity, identity),
        "final": (identity, identity, identity, identity, (1, 3, 0o700)),
    }
    identities = iter(directory_calls[failure])
    monkeypatch.setattr(snapshot_module, "_directory_identity", lambda _path: next(identities))
    if failure == "load":
        monkeypatch.setattr(
            snapshot_module,
            "load_current_usage",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic load")),
        )
    elif failure == "type":
        monkeypatch.setattr(snapshot_module, "load_current_usage", lambda *_args, **_kwargs: None)
    elif failure == "file":
        original_identity = snapshot_module._source_file_identity
        calls = 0

        def changing_file(candidate):
            nonlocal calls
            calls += 1
            result = original_identity(candidate)
            return result if calls == 1 else (*result[:3], result[3] + 1)

        monkeypatch.setattr(snapshot_module, "_source_file_identity", changing_file)
    with pytest.raises(IntegrationInvalidSource):
        read_current_usage_records(current)


def test_snapshot_pool_projection_covers_invalid_pool_and_window_shapes():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        _pool_windows,
    )

    base = _pool("main", (LimitWindow(name="5h", remaining=75, duration_seconds=18_000),))
    invalid_pools = [
        replace(base, key=1),
        replace(base, key="x" * 65),
        replace(base, windows=[]),
        replace(base, available=1),
        replace(base, allowed=1),
        replace(base, limit_reached=1),
        replace(base, availability_sources=(1,)),
        replace(base, available=False),
        replace(base, windows=(object(),)),
    ]
    for pool in invalid_pools:
        if pool.available is False:
            assert _pool_windows(pool) == []
            continue
        with pytest.raises(IntegrationInvalidSource):
            _pool_windows(pool)

    assert _pool_windows(
        _pool("main", (LimitWindow(name="weekly", remaining=50),))
    ) == [
        {
            "pool": "main",
            "window_seconds": 604_800,
            "used_percent": 50.0,
            "remaining_percent": 50.0,
        }
    ]
    assert _pool_windows(_pool("main", (LimitWindow(name="unknown", remaining=50),))) == []

    class InvalidDurationWindow(LimitWindow):
        @property
        def has_known_identity(self):
            return True

    with pytest.raises(IntegrationInvalidSource):
        _pool_windows(
            _pool(
                "main",
                tuple(
                    LimitWindow(
                        name=f"{18_001 + index}s",
                        remaining=75,
                        duration_seconds=18_001 + index,
                    )
                    for index in range(33)
                ),
            )
        )
    assert _pool_windows(
        _pool("main", (InvalidDurationWindow(name="5h", remaining=75, duration_seconds=0),))
    ) == []
    assert _pool_windows(
        _pool("main", (LimitWindow(name="5h", remaining=75, reset_at="invalid"),))
    ) == []
    assert _pool_windows(
        _pool("main", (LimitWindow(name="5h", remaining=75, reset_at=datetime(2026, 8, 15, 10)),))
    ) == []


def test_snapshot_status_and_source_limits_reject_invalid_state():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        _source_limits,
        _status_text,
    )

    with pytest.raises(IntegrationInvalidSource):
        _status_text(object())
    assert _status_text(AccountStatus.BLOCKED) == "error"

    class FakeStatus:
        BLOCKED = object()

        def __init__(self, value):
            self.value = value

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(snapshot_module, "AccountStatus", FakeStatus)
    try:
        with pytest.raises(IntegrationInvalidSource):
            _status_text(FakeStatus("unsupported"))
    finally:
        monkeypatch.undo()

    usage = _usage("alpha")
    with pytest.raises(IntegrationInvalidSource):
        _source_limits(replace(usage, models=[]))
    with pytest.raises(IntegrationInvalidSource):
        _source_limits(replace(usage, models=(object(),)))
    with pytest.raises(IntegrationInvalidSource):
        _source_limits(replace(usage, models=(replace(_pool("main", ()), key=1),)))
    with pytest.raises(IntegrationInvalidSource):
        _source_limits(replace(usage, models=(_pool("main", ()),)))
    with pytest.raises(IntegrationInvalidSource):
        _source_limits(
            replace(
                usage,
                main=_pool("main", (LimitWindow(name="5h", remaining=75),)),
                models=tuple(
                    _pool(f"pool-{index}", (LimitWindow(name="5h", remaining=75),))
                    for index in range(32)
                ),
            )
        )


def test_snapshot_document_projection_rejects_invalid_inputs_and_preserves_optional_fields():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema1_document

    with pytest.raises(IntegrationInvalidSource):
        build_schema1_document([], generated_at=GENERATED)  # type: ignore[arg-type]
    for source_commit in (1, "", "ä", "bad value"):
        with pytest.raises(IntegrationInvalidSource):
            build_schema1_document((), generated_at=GENERATED, source_commit=source_commit)  # type: ignore[arg-type]
    with pytest.raises(IntegrationInvalidSource):
        build_schema1_document((), generated_at=GENERATED, cost_windows_by_account=[])
    with pytest.raises(IntegrationInvalidSource):
        build_schema1_document((object(),), generated_at=GENERATED)  # type: ignore[arg-type]

    for usage in (
        replace(_usage("bad id"), account_id="bad/id"),
        replace(_usage("alpha"), stale=1),  # type: ignore[arg-type]
        replace(_usage("alpha"), usage_resets=None),  # type: ignore[arg-type]
    ):
        with pytest.raises(IntegrationInvalidSource):
            build_schema1_document((usage,), generated_at=GENERATED)

    document = build_schema1_document(
        (_usage("alpha"),),
        generated_at=GENERATED,
        source_commit="abc123",
        cost_windows_by_account={"alpha": [None]},
    )
    assert document["source_commit"] == "abc123"
    assert document["accounts"][0]["cost_windows"] == [None]

    class FailingConverter:
        def as_dict(self):
            raise RuntimeError("synthetic converter marker")

    with pytest.raises(IntegrationInvalidSource):
        build_schema1_document(
            (_usage("alpha"),),
            generated_at=GENERATED,
            cost_windows_by_account={"alpha": (FailingConverter(),)},
        )


def test_snapshot_secret_and_primitive_canonicalizers_reject_malformed_values():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        _canonical_cost,
        _canonical_limit,
        _canonical_percent,
        _canonical_timestamp,
        _canonical_token,
        _scan_secrets,
    )

    nested: object = "safe"
    for _ in range(66):
        nested = [nested]
    with pytest.raises(IntegrationInvalidSource):
        _scan_secrets(nested)
    with pytest.raises(IntegrationInvalidSource):
        _scan_secrets({"Bad-Key": "value"})
    with pytest.raises(IntegrationInvalidSource):
        _scan_secrets({1: "value"})

    with pytest.raises(IntegrationInvalidSource):
        _canonical_timestamp("not-a-Timestamp")
    with pytest.raises(IntegrationInvalidSource):
        _canonical_timestamp("2026-08-15T10:00:00")
    with pytest.raises(IntegrationInvalidSource):
        _canonical_timestamp("2026-08-15T10:00:00+01:00")
    for value in ("bad value", "ä", ""):
        with pytest.raises(IntegrationInvalidSource):
            _canonical_token(value, maximum=64)
    for helper, value in ((_canonical_percent, 101), (_canonical_cost, 10_001)):
        with pytest.raises(IntegrationInvalidSource):
            helper(value)

    with pytest.raises(IntegrationInvalidSource):
        _canonical_limit([])
    with pytest.raises(IntegrationInvalidSource):
        _canonical_limit({"pool": "main"})
    canonical_limit = _canonical_limit(
        {
            "pool": "main",
            "window_seconds": 18_000,
            "reset_at": "2026-08-15T10:00:00Z",
        }
    )
    assert canonical_limit["reset_at"] == "2026-08-15T10:00:00Z"


def test_snapshot_cost_window_canonicalizer_covers_optional_and_required_contracts():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, _canonical_cost_window

    with pytest.raises(IntegrationInvalidSource):
        _canonical_cost_window([])
    with pytest.raises(IntegrationInvalidSource):
        _canonical_cost_window({"pool": "main"})
    base = {
        "lookback_seconds": 3600,
        "pool": "main",
        "limit_window_seconds": 18_000,
        "consumed_percentage_points": 12.5,
        "coverage": "complete",
        "sample_count": 4,
    }
    for estimate in (-1, snapshot_module.MAX_FORECAST_SECONDS + 1, True):
        with pytest.raises(IntegrationInvalidSource):
            _canonical_cost_window({**base, "estimated_seconds_to_exhaustion": estimate})
    assert _canonical_cost_window(
        {**base, "estimated_seconds_to_exhaustion": None, "baseline_used_percent": None}
    )["baseline_used_percent"] is None
    assert _canonical_cost_window(
        {**base, "baseline_used_percent": 25.0}
    )["baseline_used_percent"] == 25.0


def _canonical_document_base(account: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": "2026-08-15T10:05:00Z",
        "accounts": [account],
    }


def test_snapshot_canonical_document_rejects_shapes_and_canonicalizes_optional_sections():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, _canonical_document

    invalid_documents = [
        None,
        {"accounts": {}},
        {"accounts": [], "schema_version": True, "generated_at": "2026-08-15T10:05:00Z"},
    ]
    for document in invalid_documents:
        with pytest.raises(IntegrationInvalidSource):
            _canonical_document(document)
    for account in (None, {"account_id": "alpha"}):
        with pytest.raises(IntegrationInvalidSource):
            _canonical_document(_canonical_document_base(account))

    base_account = {
        "account_id": "alpha",
        "status": "ok",
        "freshness": {"captured_at": "2026-08-15T10:00:00Z", "stale": False},
    }
    malformed_accounts = [
        {**base_account, "account_id": "bad/id"},
        {**base_account, "freshness": {}},
        {**base_account, "freshness": {"captured_at": "2026-08-15T10:00:00Z", "stale": 1}},
        {**base_account, "limits": {}},
        {**base_account, "cost_windows": {}},
        {**base_account, "usage_resets": {}},
    ]
    for account in malformed_accounts:
        with pytest.raises(IntegrationInvalidSource):
            _canonical_document(_canonical_document_base(account))

    duplicate_limit = {
        "pool": "main",
        "window_seconds": 18_000,
        "remaining_percent": 75.0,
    }
    with pytest.raises(IntegrationInvalidSource):
        _canonical_document(
            _canonical_document_base({**base_account, "limits": [duplicate_limit, duplicate_limit]})
        )

    document = _canonical_document_base(
        {
            **base_account,
            "limits": [duplicate_limit],
            "cost_windows": [
                {
                    "lookback_seconds": 3600,
                    "pool": "main",
                    "limit_window_seconds": 18_000,
                    "consumed_percentage_points": 12.5,
                    "coverage": "complete",
                    "sample_count": 4,
                }
            ],
            "usage_resets": {"available": None, "known": False, "redeem_capability": False},
        }
    )
    document["source_commit"] = "abc123"
    valid = _canonical_document(document)
    assert valid["accounts"][0]["usage_resets"]["available"] is None
    assert valid["source_commit"] == "abc123"

    without_optional_resets = _canonical_document(_canonical_document_base(base_account))
    assert "usage_resets" not in without_optional_resets["accounts"][0]
    known_resets = _canonical_document(
        _canonical_document_base(
            {
                **base_account,
                "usage_resets": {
                    "available": 2,
                    "known": True,
                    "redeem_capability": True,
                },
            }
        )
    )
    assert known_resets["accounts"][0]["usage_resets"]["available"] == 2


def test_snapshot_canonical_document_rejects_invalid_reset_contracts():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, _canonical_document

    base = {
        "account_id": "alpha",
        "status": "ok",
        "freshness": {"captured_at": "2026-08-15T10:00:00Z", "stale": False},
    }
    for resets in (
        {"available": 1, "known": 1, "redeem_capability": False},
        {"available": -1, "known": True, "redeem_capability": False},
        {"available": 10_001, "known": True, "redeem_capability": False},
        {"available": None, "known": True, "redeem_capability": False},
        {"available": 1, "known": False, "redeem_capability": False},
        {"available": None, "known": False, "redeem_capability": 1},
    ):
        with pytest.raises(IntegrationInvalidSource):
            _canonical_document(_canonical_document_base({**base, "usage_resets": resets}))


def test_snapshot_serializer_rejects_oversized_and_noncanonical_payloads(tmp_path, monkeypatch):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        publish_schema1_cache,
        serialize_schema1_document,
    )

    monkeypatch.setattr(snapshot_module, "_MAX_DOCUMENT_BYTES", 1)
    with pytest.raises(IntegrationInvalidSource):
        serialize_schema1_document(
            {
                "accounts": [],
                "generated_at": "2026-08-15T10:05:00Z",
                "schema_version": 1,
            }
        )

    monkeypatch.setattr(snapshot_module, "_MAX_DOCUMENT_BYTES", 2 * 1024 * 1024)
    cache = _cache_path(tmp_path)
    noncanonical = b'{"schema_version":1,"generated_at":"2026-08-15T10:05:00Z","accounts":[]}'
    with pytest.raises(IntegrationInvalidSource):
        publish_schema1_cache(noncanonical, cache_path=cache)


def test_snapshot_cache_security_guards_cover_missing_and_io_errors(tmp_path, monkeypatch):
    from codex_usage.integration_snapshot import (
        IntegrationSecureIOError,
        _require_integration_directory,
        _validate_existing_cache,
    )

    with pytest.raises(IntegrationSecureIOError):
        _require_integration_directory(Path("relative/cache.json"))

    cache = _cache_path(tmp_path)

    def raise_value(*_args, **_kwargs):
        raise ValueError("synthetic ancestor marker")

    monkeypatch.setattr(snapshot_module, "assert_no_symlink_ancestors", raise_value)
    with pytest.raises(IntegrationSecureIOError):
        _require_integration_directory(cache)

    monkeypatch.setattr(snapshot_module, "assert_no_symlink_ancestors", lambda *_a, **_k: None)
    original_lstat = Path.lstat

    def fail_directory_lstat(path):
        if path == cache.parent:
            raise OSError("synthetic directory marker")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_directory_lstat)
    with pytest.raises(IntegrationSecureIOError):
        _require_integration_directory(cache)
    monkeypatch.setattr(Path, "lstat", original_lstat)

    _validate_existing_cache(tmp_path / "missing-cache.json")

    def fail_cache_lstat(path):
        if path == cache:
            raise OSError("synthetic cache marker")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_cache_lstat)
    with pytest.raises(IntegrationSecureIOError):
        _validate_existing_cache(cache)


def test_snapshot_publish_maps_decode_and_lock_failures(tmp_path, monkeypatch):
    from codex_usage.integration_snapshot import (
        IntegrationBusy,
        IntegrationInvalidSource,
        publish_schema1_cache,
    )

    cache = _cache_path(tmp_path)
    with pytest.raises(IntegrationInvalidSource):
        publish_schema1_cache(b"\xff", cache_path=cache)

    valid = b'{"accounts":[],"generated_at":"2026-08-15T10:05:00Z","schema_version":1}'
    original_loads_strict = snapshot_module.loads_strict
    def fail_parse(_payload):
        raise ValueError("synthetic parse")

    monkeypatch.setattr(snapshot_module, "loads_strict", fail_parse)
    with pytest.raises(IntegrationInvalidSource):
        publish_schema1_cache(valid, cache_path=cache)
    monkeypatch.setattr(snapshot_module, "loads_strict", original_loads_strict)

    class BusyLock:
        def __enter__(self):
            raise TimeoutError("synthetic lock marker")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(snapshot_module, "private_path_lock", lambda *_a, **_k: BusyLock())
    with pytest.raises(IntegrationBusy):
        publish_schema1_cache(valid, cache_path=cache)
