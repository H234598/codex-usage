from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path

import pytest

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
