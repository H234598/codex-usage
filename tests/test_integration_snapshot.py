from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from itertools import repeat
from pathlib import Path

import pytest

import codex_usage.integration_snapshot as snapshot_module
from codex_usage.history import UsageSample
from codex_usage.models import AccountStatus, AccountUsage, LimitWindow, UsagePool
from codex_usage.private_io import write_private_text

CAPTURED_ALPHA = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
GENERATED = datetime(2026, 8, 15, 10, 5, tzinfo=UTC)
RESET = datetime(2026, 8, 15, 15, 0, tzinfo=UTC)


class _RaisingTimezone(tzinfo):
    def utcoffset(self, _value):
        raise RuntimeError("synthetic timezone marker")


class _BrokenFloat(float):
    def __float__(self):
        raise RuntimeError("synthetic snapshot float conversion marker")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(10**10_000, id="percent-huge-int"),
    ],
)
def test_schema2_canonical_percent_rejects_overflow(value):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        _canonical_percent,
    )

    with pytest.raises(IntegrationInvalidSource):
        _canonical_percent(value)


@pytest.mark.parametrize("value", [None, [], {}, "invalid", 1, True])
def test_schema2_projection_rejects_malformed_generated_timestamp(value):
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((), generated_at=value)  # type: ignore[arg-type]


def test_schema2_projection_rejects_timezone_callbacks_that_raise():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, _utc_text

    with pytest.raises(IntegrationInvalidSource):
        _utc_text(datetime(2026, 8, 15, 10, 0, tzinfo=_RaisingTimezone()))


def test_schema2_canonical_timestamp_rejects_string_subclass_hooks():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, _canonical_timestamp

    class BrokenStr(str):
        def __contains__(self, _value):
            raise RuntimeError("synthetic snapshot timestamp marker")

    with pytest.raises(IntegrationInvalidSource):
        _canonical_timestamp(BrokenStr("2026-08-15T10:00:00Z"))


def test_schema2_canonical_token_rejects_string_subclass_hooks():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, _canonical_token

    class BrokenStr(str):
        def __len__(self):
            raise RuntimeError("synthetic snapshot token marker")

    with pytest.raises(IntegrationInvalidSource):
        _canonical_token(BrokenStr("alpha"), maximum=64)


@pytest.mark.parametrize("value", [None, [], {}, "invalid", 1, True])
def test_schema2_projection_rejects_malformed_usage_timestamp(value):
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document(
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


def _tracker_sample(
    captured_at: datetime,
    used_percent: float,
    *,
    account_id: str = "alpha",
    pool: str = "main",
    window_seconds: int = 18_000,
    reset_at: datetime = RESET,
    reset_generation: str = "reset-1",
) -> UsageSample:
    return UsageSample(
        account_id=account_id,
        pool=pool,
        window_seconds=window_seconds,
        captured_at=captured_at,
        used_percent=used_percent,
        reset_at=reset_at,
        reset_generation=reset_generation,
        source="synthetic",
    )


def test_schema2_golden_document_contains_only_current_limits_and_tracker_evidence():
    from codex_usage.integration_snapshot import (
        build_schema2_document,
        serialize_schema2_document,
    )

    usage = replace(
        _usage("alpha"),
        main=_pool(
            "main",
            (
                LimitWindow(
                    name="5h",
                    percent=75.0,
                    duration_seconds=18_000,
                    reset_at=RESET,
                ),
            ),
        ),
    )
    document = build_schema2_document(
        (usage,),
        generated_at=GENERATED,
        tracker_samples={
            ("alpha", "main", 18_000): (
                _tracker_sample(CAPTURED_ALPHA - timedelta(minutes=10), 15.0),
                _tracker_sample(CAPTURED_ALPHA, 25.0),
            )
        },
    )

    assert json.loads(serialize_schema2_document(document)) == {
        "accounts": [
            {
                "account_id": "alpha",
                "freshness": {
                    "captured_at": "2026-08-15T10:00:00Z",
                    "fresh_until": "2026-08-15T10:15:00Z",
                    "stale": False,
                },
                "limits": [
                    {
                        "pool": "main",
                        "remaining_percent": 75.0,
                        "reset_at": "2026-08-15T15:00:00Z",
                        "used_percent": 25.0,
                        "window_seconds": 18_000,
                    }
                ],
                "status": "ok",
                "tracker_evidence": [
                    {
                        "coverage": "complete",
                        "ema_time_constant_seconds": 3_600,
                        "first_sample_at": "2026-08-15T09:50:00Z",
                        "last_sample_at": "2026-08-15T10:00:00Z",
                        "limit_window_seconds": 18_000,
                        "pool": "main",
                        "projected_used_percent_at_reset": 100.0,
                        "rate_percentage_points_per_second": 1.0 / 60.0,
                        "reset_generation": "reset-1",
                        "sample_count": 2,
                    }
                ],
            }
        ],
        "generated_at": "2026-08-15T10:05:00Z",
        "schema_version": 2,
    }


def test_schema2_is_only_active_snapshot_api():
    assert hasattr(snapshot_module, "build_schema2_document")
    assert hasattr(snapshot_module, "serialize_schema2_document")
    assert not hasattr(snapshot_module, "publish_schema2_cache")
    assert not hasattr(snapshot_module, "build_schema1_document")
    assert not hasattr(snapshot_module, "serialize_schema1_document")
    assert not hasattr(snapshot_module, "publish_schema1_cache")


def test_schema2_builder_rejects_limit_source_over_cap_before_projection(monkeypatch):
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    monkeypatch.setattr(snapshot_module, "_MAX_LIMITS_PER_ACCOUNT", 1)
    usage = replace(
        _usage("alpha"),
        main=_pool(
            "main",
            (
                LimitWindow(name="unsupported-a"),
                LimitWindow(name="unsupported-b"),
            ),
        ),
    )

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((usage,), generated_at=GENERATED)


def test_schema2_builder_rejects_availability_source_over_cap(monkeypatch):
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    monkeypatch.setattr(snapshot_module, "_MAX_AVAILABILITY_SOURCES_PER_POOL", 1)
    pool = replace(
        _pool("main", (LimitWindow(name="5h", remaining=75),)),
        availability_sources=("one", "two"),
    )

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((_usage_with_pools((pool,)),), generated_at=GENERATED)


def test_schema2_builder_accepts_exact_availability_source_cap(monkeypatch):
    from codex_usage.integration_snapshot import build_schema2_document

    monkeypatch.setattr(snapshot_module, "_MAX_AVAILABILITY_SOURCES_PER_POOL", 2)
    pool = replace(
        _pool("main", (LimitWindow(name="5h", remaining=75),)),
        availability_sources=("one", "two"),
    )

    account = build_schema2_document(
        (_usage_with_pools((pool,)),),
        generated_at=GENERATED,
    )["accounts"][0]

    assert len(account["limits"]) == 1


def test_schema2_builder_rejects_invalid_explicit_values_capture():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    usage = replace(_usage("alpha"), values_captured_at=[])

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((usage,), generated_at=GENERATED)


@pytest.mark.parametrize("capture_field", ["captured_at", "values_captured_at"])
def test_schema2_builder_rejects_capture_after_generation(capture_field):
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    usage = _usage("alpha")
    if capture_field == "values_captured_at":
        usage = replace(usage, values_captured_at=GENERATED + timedelta(seconds=1))
    else:
        usage = replace(usage, captured_at=GENERATED + timedelta(seconds=1))

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((usage,), generated_at=GENERATED)


def test_schema2_numeric_boundaries_reject_subclasses_before_operations():
    broken_float = _BrokenFloat(50.0)

    with pytest.raises(snapshot_module.IntegrationInvalidSource):
        snapshot_module._canonical_percent(broken_float)
    class BrokenLimitWindow(LimitWindow):
        @property
        def remaining_percent(self):
            return broken_float

    with pytest.raises(snapshot_module.IntegrationInvalidSource):
        snapshot_module._pool_windows(
            _pool("main", (BrokenLimitWindow(name="5h", duration_seconds=18_000),))
        )



def _usage_with_pools(pools: tuple[UsagePool, ...]) -> AccountUsage:
    return replace(_usage("alpha"), main=None, models=pools)


def test_schema2_projection_is_sorted_allowlisted_and_deterministic(tmp_path):
    from codex_usage.integration_snapshot import (
        build_schema2_document,
        read_current_usage_records,
        serialize_schema2_document,
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
    document = build_schema2_document(read_current_usage_records(current), generated_at=GENERATED)
    payload = json.loads(serialize_schema2_document(document))
    assert [account["account_id"] for account in payload["accounts"]] == ["alpha", "zeta"]
    assert all(
        set(account) == {"account_id", "freshness", "limits", "status", "tracker_evidence"}
        for account in payload["accounts"]
    )
    encoded = serialize_schema2_document(document).decode("utf-8")
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
def test_schema2_projection_rejects_unusable_remaining_values(remaining):
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

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
    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((usage,), generated_at=GENERATED)


def test_schema2_projection_rejects_nonallowlisted_limit_windows():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    usage = _usage_with_pools(
        (
            _pool(
                "main",
                (LimitWindow(name="1d", remaining=75, duration_seconds=86_400),),
            ),
        )
    )

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((usage,), generated_at=GENERATED)


@pytest.mark.parametrize(
    "reset_at",
    ["invalid", datetime(2026, 8, 15, 10)],
)
def test_schema2_projection_rejects_malformed_limit_reset(reset_at):
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    usage = _usage_with_pools(
        (_pool("main", (LimitWindow(name="5h", remaining=75, reset_at=reset_at),)),)
    )

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((usage,), generated_at=GENERATED)


def test_schema2_projection_includes_credit_limit_without_tracker_evidence():
    from codex_usage.integration_snapshot import build_schema2_document

    usage = replace(
        _usage("alpha"),
        credits=LimitWindow(
            name="30d",
            percent=80.0,
            duration_seconds=2_592_000,
            reset_at=RESET + timedelta(days=30),
        ),
    )

    account = build_schema2_document((usage,), generated_at=GENERATED)["accounts"][0]

    assert account["limits"] == [
        {
            "pool": "credits",
            "remaining_percent": 80.0,
            "reset_at": "2026-09-14T15:00:00Z",
            "used_percent": 20.0,
            "window_seconds": 2_592_000,
        },
        {
            "pool": "main",
            "remaining_percent": 75.0,
            "used_percent": 25.0,
            "window_seconds": 18_000,
        },
    ]
    assert account["tracker_evidence"] == []


def test_schema2_projection_omits_absolute_credit_and_preserves_percent_control():
    from codex_usage.integration_snapshot import (
        build_schema2_document,
        serialize_schema2_document,
    )

    absolute_values = {
        "absolute-a": 0.0,
        "absolute-b": 12.0,
        "absolute-c": 80.0,
        "absolute-d": 100.0,
        "absolute-e": 100.01,
        "absolute-f": 794.0,
    }
    absolutes = tuple(
        replace(
            _usage(account_id),
            credits=LimitWindow(name="credits", remaining=remaining),
        )
        for account_id, remaining in absolute_values.items()
    )
    percent = replace(
        _usage("percent-credit"),
        credits=LimitWindow(name="credits", percent=80.0),
    )

    document = build_schema2_document((*absolutes, percent), generated_at=GENERATED)
    serialized = serialize_schema2_document(document)
    accounts = {account["account_id"]: account for account in document["accounts"]}

    for account_id in absolute_values:
        assert accounts[account_id]["limits"] == [
            {
                "pool": "main",
                "remaining_percent": 75.0,
                "used_percent": 25.0,
                "window_seconds": 18_000,
            }
        ]
    assert accounts["percent-credit"]["limits"] == [
        {
            "pool": "credits",
            "remaining_percent": 80.0,
            "used_percent": 20.0,
            "window_seconds": 2_592_000,
        },
        {
            "pool": "main",
            "remaining_percent": 75.0,
            "used_percent": 25.0,
            "window_seconds": 18_000,
        },
    ]
    assert all(
        evidence["pool"] != "credits"
        for account in accounts.values()
        for evidence in account["tracker_evidence"]
    )
    assert b'"remaining":' not in serialized
    assert b'"limit":' not in serialized
    assert all(
        value != 794.0
        for limit in accounts["absolute-f"]["limits"]
        for value in limit.values()
        if type(value) in (int, float)
    )


@pytest.mark.parametrize(
    "credits",
    [
        pytest.param(LimitWindow(name="credits", remaining=-1.0), id="negative"),
        pytest.param(LimitWindow(name="credits", remaining=float("inf")), id="nonfinite"),
        pytest.param(
            LimitWindow(name="credits", remaining=794.0, used=float("nan")),
            id="nonfinite-used",
        ),
        pytest.param(
            LimitWindow(name="credits", remaining=80.0, percent=70.0),
            id="inconsistent-percent",
        ),
        pytest.param(
            LimitWindow(name="credits", remaining=101.0, limit=100.0),
            id="pair-remaining-limit",
        ),
        pytest.param(
            LimitWindow(name="credits", used=101.0, limit=100.0),
            id="pair-used-limit",
        ),
        pytest.param(
            LimitWindow(name="credits", used=20.0, percent=70.0),
            id="pair-used-percent",
        ),
        pytest.param(
            LimitWindow(name="credits", used=20.0, remaining=80.0),
            id="pair-used-remaining-without-denominator",
        ),
        pytest.param(
            LimitWindow(name="credits", used=20.0, limit=100.0, remaining=70.0),
            id="triple-used-limit-remaining",
        ),
        pytest.param(
            LimitWindow(name="credits", used=20.0, limit=100.0, percent=70.0),
            id="triple-used-limit-percent",
        ),
        pytest.param(
            LimitWindow(
                name="credits",
                remaining=80.0,
                limit=100.0,
                percent=70.0,
            ),
            id="triple-remaining-limit-percent",
        ),
        pytest.param(
            LimitWindow(name="credits", used=20.0, remaining=70.0, percent=70.0),
            id="triple-without-limit",
        ),
        pytest.param(
            LimitWindow(
                name="credits",
                used=20.0,
                limit=100.0,
                remaining=70.0,
                percent=70.0,
            ),
            id="quad",
        ),
        pytest.param(
            LimitWindow(
                name="credits",
                used=20.0,
                limit=100.0,
                percent=80.0000000001,
            ),
            id="beyond-float-rounding",
        ),
    ],
)
def test_schema2_projection_rejects_invalid_absolute_credit_source(credits):
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    usage = replace(_usage("alpha"), credits=credits)

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((usage,), generated_at=GENERATED)


@pytest.mark.parametrize("status", tuple(AccountStatus))
def test_schema2_projection_validates_invalid_credit_before_status_suppression(
    status,
):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        build_schema2_document,
    )

    usage = replace(
        _usage("alpha"),
        status=status,
        credits=LimitWindow(name="credits", source="invalid:credits"),
    )

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((usage,), generated_at=GENERATED)


def test_schema2_projection_rejects_secondary_credit_alias_domain_violation():
    from codex_usage.direct import _credit_window
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        build_schema2_document,
    )

    credits = _credit_window(
        {
            "credits": {
                "remaining": 0.0,
                "available": -math.ulp(0.0),
            }
        },
        GENERATED,
    )
    assert credits == LimitWindow(name="credits", source="invalid:credits")
    usage = replace(_usage("alpha"), credits=credits)

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((usage,), generated_at=GENERATED)


def test_schema2_projection_rejects_credit_reset_alias_conflict():
    from codex_usage.direct import _credit_window
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        build_schema2_document,
    )

    credits = _credit_window(
        {
            "credits": {
                "percent": 80,
                "reset_at": "2026-09-01T00:00:00Z",
                "resetAt": "2026-10-01T00:00:00Z",
            }
        },
        GENERATED,
    )
    assert credits == LimitWindow(name="credits", source="invalid:credits")
    usage = replace(_usage("alpha"), credits=credits)

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((usage,), generated_at=GENERATED)


def test_schema2_projection_preserves_equal_offset_credit_reset_alias_instant():
    from codex_usage.direct import _credit_window
    from codex_usage.integration_snapshot import build_schema2_document

    credits = _credit_window(
        {
            "credits": {
                "percent": 80,
                "reset_at": "2026-09-01T00:00:00Z",
                "resetAt": "2026-09-01T02:00:00+02:00",
            }
        },
        GENERATED,
    )
    usage = replace(_usage("alpha"), credits=credits)

    document = build_schema2_document((usage,), generated_at=GENERATED)
    credit = next(
        limit
        for limit in document["accounts"][0]["limits"]
        if limit["pool"] == "credits"
    )

    assert credit["reset_at"] == "2026-09-01T00:00:00Z"


@pytest.mark.parametrize(
    ("credits", "expected_remaining_percent"),
    [
        pytest.param(LimitWindow(name="credits", percent=80.0), 80.0, id="percent"),
        pytest.param(
            LimitWindow(name="credits", limit=100.0, percent=80.0),
            80.0,
            id="limit-percent",
        ),
        pytest.param(
            LimitWindow(name="credits", remaining=80.0, percent=80.0),
            80.0,
            id="remaining-percent",
        ),
        pytest.param(
            LimitWindow(name="credits", used=20.0, limit=100.0),
            80.0,
            id="used-limit",
        ),
        pytest.param(
            LimitWindow(name="credits", remaining=80.0, limit=100.0),
            80.0,
            id="remaining-limit",
        ),
        pytest.param(
            LimitWindow(
                name="credits",
                used=20.0,
                remaining=80.0,
                limit=100.0,
                percent=80.0,
            ),
            80.0,
            id="quad",
        ),
        pytest.param(
            LimitWindow(
                name="credits",
                used=0.1 + 0.2,
                remaining=0.7,
                limit=1.0,
                percent=70.0,
            ),
            70.0,
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
            (1.0 - math.nextafter(1.0, 0.0)) * 100.0,
            id="nextafter-quad",
        ),
    ],
)
def test_schema2_projection_preserves_consistent_explicit_credit_fields(
    credits,
    expected_remaining_percent,
):
    from codex_usage.integration_snapshot import build_schema2_document

    usage = replace(_usage("alpha"), credits=credits)

    account = build_schema2_document((usage,), generated_at=GENERATED)["accounts"][0]

    credit_limit = next(
        limit for limit in account["limits"] if limit["pool"] == "credits"
    )
    assert credit_limit["remaining_percent"] == pytest.approx(
        expected_remaining_percent
    )
    assert credit_limit["used_percent"] == pytest.approx(
        100.0 - expected_remaining_percent
    )


def test_schema2_projection_preserves_valid_partial_limits_and_evidence():
    from codex_usage.integration_snapshot import build_schema2_document, serialize_schema2_document

    usage = replace(_usage_with_tracker_limit(), status=AccountStatus.PARTIAL)
    document = build_schema2_document(
        (usage,),
        generated_at=GENERATED,
        tracker_samples={
            ("alpha", "main", 18_000): (
                _tracker_sample(CAPTURED_ALPHA, 25.0),
            )
        },
    )

    account = json.loads(serialize_schema2_document(document))["accounts"][0]
    assert account["status"] == "partial"
    assert account["limits"][0]["pool"] == "main"
    assert account["tracker_evidence"][0]["coverage"] == "insufficient"


def test_schema2_projection_rejects_unhashable_pool_key_without_raising():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    usage = _usage_with_pools(
        (replace(_pool("main", (LimitWindow(name="5h", remaining=75),)), key=[]),)
    )

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((usage,), generated_at=GENERATED)


def _usage_with_tracker_limit() -> AccountUsage:
    return replace(
        _usage("alpha"),
        main=_pool(
            "main",
            (
                LimitWindow(
                    name="5h",
                    percent=75.0,
                    duration_seconds=18_000,
                    reset_at=RESET,
                ),
            ),
        ),
    )


def test_schema2_projection_omits_invalid_tracker_series():
    from codex_usage.integration_snapshot import build_schema2_document

    document = build_schema2_document(
        (_usage_with_tracker_limit(),),
        generated_at=GENERATED,
        tracker_samples={
            ("alpha", "main", 18_000): (
                _tracker_sample(CAPTURED_ALPHA - timedelta(minutes=10), 25.0),
                _tracker_sample(CAPTURED_ALPHA, 20.0),
            )
        },
    )

    assert document["accounts"][0]["tracker_evidence"] == []


def test_schema2_projection_preserves_insufficient_tracker_evidence():
    from codex_usage.integration_snapshot import build_schema2_document

    document = build_schema2_document(
        (_usage_with_tracker_limit(),),
        generated_at=GENERATED,
        tracker_samples={
            ("alpha", "main", 18_000): (_tracker_sample(CAPTURED_ALPHA, 25.0),)
        },
    )

    assert document["accounts"][0]["tracker_evidence"][0]["coverage"] == "insufficient"


def test_schema2_projection_preserves_stale_tracker_evidence():
    from codex_usage.integration_snapshot import build_schema2_document

    document = build_schema2_document(
        (_usage_with_tracker_limit(),),
        generated_at=GENERATED,
        tracker_samples={
            ("alpha", "main", 18_000): (
                _tracker_sample(CAPTURED_ALPHA - timedelta(minutes=30), 15.0),
                _tracker_sample(CAPTURED_ALPHA - timedelta(minutes=20), 25.0),
            )
        },
    )

    assert document["accounts"][0]["tracker_evidence"][0]["coverage"] == "stale"


def test_schema2_serializer_requires_integer_ema_time_constant():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        build_schema2_document,
        serialize_schema2_document,
    )

    document = build_schema2_document(
        (_usage_with_tracker_limit(),),
        generated_at=GENERATED,
        tracker_samples={
            ("alpha", "main", 18_000): (_tracker_sample(CAPTURED_ALPHA, 25.0),)
        },
    )
    document["accounts"][0]["tracker_evidence"][0]["ema_time_constant_seconds"] = 3_600.0

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)


@pytest.mark.parametrize(
    ("coverage", "sample_count"),
    [("complete", 1), ("partial", 1), ("stale", 1), ("insufficient", 2)],
)
def test_schema2_serializer_rejects_inconsistent_coverage_sample_count(
    coverage,
    sample_count,
):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    document = _valid_schema2_document(with_evidence=True)
    evidence = document["accounts"][0]["tracker_evidence"][0]
    evidence["coverage"] = coverage
    evidence["sample_count"] = sample_count

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)


def test_schema2_serializer_rejects_fractional_sample_time_reversal():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        build_schema2_document,
        serialize_schema2_document,
    )

    document = build_schema2_document(
        (_usage_with_tracker_limit(),),
        generated_at=GENERATED,
        tracker_samples={
            ("alpha", "main", 18_000): (_tracker_sample(CAPTURED_ALPHA, 25.0),)
        },
    )
    evidence = document["accounts"][0]["tracker_evidence"][0]
    evidence["first_sample_at"] = "2026-08-15T10:00:00.100000Z"
    evidence["last_sample_at"] = "2026-08-15T10:00:00Z"

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)


def test_schema2_serializer_rejects_capture_after_generation():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    document = _valid_schema2_document()
    document["accounts"][0]["freshness"] = {
        "captured_at": "2026-08-15T10:06:00Z",
        "fresh_until": "2026-08-15T10:21:00Z",
        "stale": False,
    }

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)


def test_current_reader_ignores_private_lock_and_temporary_files(tmp_path):
    from codex_usage.integration_snapshot import read_current_usage_records

    current = tmp_path / "data" / "codex-usage" / "current"
    _write_current_fixture(current, _usage("alpha"))
    for path in (
        current / "alpha.json.lock",
        current / ".alpha.json.tmp-123-secret",
        current / ".alpha.json.rollback-123-secret",
        current / ".alpha.json.rollback",
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


def test_current_reader_accepts_exact_directory_entry_cap(tmp_path, monkeypatch):
    from codex_usage.integration_snapshot import read_current_usage_records

    current = tmp_path / "data" / "codex-usage" / "current"
    _write_current_fixture(current, _usage("alpha"))
    _write_current_fixture(current, _usage("beta"))
    assert len(tuple(current.iterdir())) == 2
    monkeypatch.setattr(snapshot_module, "_MAX_DIRECTORY_ENTRIES", 2)

    assert [item.account_id for item in read_current_usage_records(current)] == [
        "alpha",
        "beta",
    ]


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
        serialize_schema2_document,
    )

    def unexpected_secret_scan(_value):
        pytest.fail("secret scan must not traverse oversized account list")

    monkeypatch.setattr(integration_snapshot, "_scan_secrets", unexpected_secret_scan)

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(
            {
                "accounts": [None] * (integration_snapshot._MAX_ACCOUNTS + 1),
                "generated_at": "2026-08-15T10:05:00Z",
                "schema_version": 2,
            }
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


def test_projection_rejects_duplicate_identity_and_never_uses_label():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((_usage("alpha"), _usage("alpha")), generated_at=GENERATED)


def test_serialization_is_bounded_and_secret_free_for_valid_fixture():
    from codex_usage.integration_snapshot import build_schema2_document, serialize_schema2_document

    payload = serialize_schema2_document(
        build_schema2_document((_usage("alpha"),), generated_at=GENERATED)
    )
    assert len(payload) <= 2 * 1024 * 1024
    for marker in (b"token", b"profile", b"/home/", b"synthetic", b"raw"):
        assert marker not in payload.lower()


def test_projection_rejects_more_than_32_aggregated_limits_across_pools():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    pools = tuple(
        _pool(
            f"pool-{index:02d}",
            (LimitWindow(name="5h", percent=75.0, duration_seconds=18_000),),
        )
        for index in range(33)
    )
    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((_usage_with_pools(pools),), generated_at=GENERATED)


def test_projection_accepts_exactly_32_unique_aggregated_limits():
    from codex_usage.integration_snapshot import build_schema2_document

    pools = tuple(
        _pool(
            f"pool-{index:02d}",
            (LimitWindow(name="5h", percent=75.0, duration_seconds=18_000),),
        )
        for index in range(32)
    )
    document = build_schema2_document((_usage_with_pools(pools),), generated_at=GENERATED)
    assert len(document["accounts"][0]["limits"]) == 32


def test_projection_rejects_too_many_model_pools_before_materialization():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        build_schema2_document,
    )

    pools = tuple(_pool(f"pool-{index:02d}", ()) for index in range(33))

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((_usage_with_pools(pools),), generated_at=GENERATED)


def test_projection_rejects_duplicate_canonical_limit_identity():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    duplicate = LimitWindow(
        name="5h",
        percent=75.0,
        duration_seconds=18_000,
        reset_at=RESET,
    )
    pools = (_pool("main", (duplicate, duplicate)),)
    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((_usage_with_pools(pools),), generated_at=GENERATED)


def test_projection_rejects_duplicate_limit_identity_with_different_reset():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    first = LimitWindow(
        name="5h",
        percent=75.0,
        duration_seconds=18_000,
        reset_at=RESET,
    )
    second = replace(first, reset_at=RESET.replace(hour=16))
    pools = (_pool("main", (first, second)),)
    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((_usage_with_pools(pools),), generated_at=GENERATED)


def test_serialization_rejects_boolean_schema_version():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(
            {
                "schema_version": True,
                "generated_at": "2026-08-15T10:05:00Z",
                "accounts": [],
            }
        )


def test_serialization_converts_mapping_callback_failures_to_invalid_source():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    class RaisingMapping(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError("synthetic mapping failure")

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(RaisingMapping())


def test_serialization_accepts_true_integer_schema_version():
    from codex_usage.integration_snapshot import serialize_schema2_document

    assert serialize_schema2_document(
        {
            "schema_version": 2,
            "generated_at": "2026-08-15T10:05:00Z",
            "accounts": [],
        }
    ) == b'{"accounts":[],"generated_at":"2026-08-15T10:05:00Z","schema_version":2}'



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
    with pytest.raises(IntegrationInvalidSource):
        _pool_windows(_pool("main", (LimitWindow(name="unknown", remaining=50),)))

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
    with pytest.raises(IntegrationInvalidSource):
        _pool_windows(
            _pool("main", (InvalidDurationWindow(name="5h", remaining=75, duration_seconds=0),))
        )
    with pytest.raises(IntegrationInvalidSource):
        _pool_windows(
            _pool("main", (LimitWindow(name="5h", remaining=75, reset_at="invalid"),))
        )
    with pytest.raises(IntegrationInvalidSource):
        _pool_windows(
            _pool(
                "main",
                (LimitWindow(name="5h", remaining=75, reset_at=datetime(2026, 8, 15, 10)),),
            )
        )

    class RaisingWindow(LimitWindow):
        @property
        def remaining_percent(self):
            raise RuntimeError("synthetic remaining callback")

    with pytest.raises(IntegrationInvalidSource):
        _pool_windows(_pool("main", (RaisingWindow(name="5h"),)))


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


def test_snapshot_document_projection_rejects_invalid_inputs_and_exports_exact_fields():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document(None, generated_at=GENERATED)  # type: ignore[arg-type]
    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((object(),), generated_at=GENERATED)  # type: ignore[arg-type]

    for usage in (
        replace(_usage("bad id"), account_id="bad/id"),
        replace(_usage("alpha"), stale=1),  # type: ignore[arg-type]
    ):
        with pytest.raises(IntegrationInvalidSource):
            build_schema2_document((usage,), generated_at=GENERATED)

    document = build_schema2_document((_usage("alpha"),), generated_at=GENERATED)
    assert set(document) == {"accounts", "generated_at", "schema_version"}
    assert set(document["accounts"][0]) == {
        "account_id",
        "freshness",
        "limits",
        "status",
        "tracker_evidence",
    }


def _valid_schema2_document(*, with_evidence: bool = False) -> dict[str, object]:
    from codex_usage.integration_snapshot import build_schema2_document

    usage = _usage_with_tracker_limit() if with_evidence else _usage("alpha")
    tracker_samples = (
        {("alpha", "main", 18_000): (_tracker_sample(CAPTURED_ALPHA, 25.0),)}
        if with_evidence
        else None
    )
    return build_schema2_document(
        (usage,),
        generated_at=GENERATED,
        tracker_samples=tracker_samples,
    )


def _schema2_document_with_coverage(
    coverage: str,
    *,
    first_sample_at: str = "2026-08-15T09:40:00Z",
    last_sample_at: str,
) -> dict[str, object]:
    document = _valid_schema2_document(with_evidence=True)
    evidence = document["accounts"][0]["tracker_evidence"][0]
    evidence.update(
        {
            "coverage": coverage,
            "first_sample_at": first_sample_at,
            "last_sample_at": last_sample_at,
            "sample_count": 2,
        }
    )
    return document


@pytest.mark.parametrize("coverage", ["complete", "partial"])
def test_schema2_serializer_accepts_fresh_coverage_at_exact_900_second_boundary(coverage):
    from codex_usage.integration_snapshot import serialize_schema2_document

    document = _schema2_document_with_coverage(
        coverage,
        last_sample_at="2026-08-15T09:50:00Z",
    )

    assert json.loads(serialize_schema2_document(document))["accounts"][0][
        "tracker_evidence"
    ][0]["coverage"] == coverage


@pytest.mark.parametrize(
    ("coverage", "last_sample_at"),
    [
        pytest.param("complete", "2026-08-15T09:49:59.999999Z", id="old-complete"),
        pytest.param("partial", "2026-08-15T09:49:59.999999Z", id="old-partial"),
        pytest.param("stale", "2026-08-15T09:50:00Z", id="fresh-stale-boundary"),
        pytest.param("complete", "2026-08-15T10:05:00.000001Z", id="future-sample"),
    ],
)
def test_schema2_serializer_rejects_coverage_inconsistent_with_sample_age(
    coverage,
    last_sample_at,
):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    document = _schema2_document_with_coverage(
        coverage,
        last_sample_at=last_sample_at,
    )

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)


def test_schema2_serializer_retains_old_single_sample_insufficient_semantics():
    from codex_usage.integration_snapshot import serialize_schema2_document

    document = _valid_schema2_document(with_evidence=True)
    evidence = document["accounts"][0]["tracker_evidence"][0]
    evidence["first_sample_at"] = evidence["last_sample_at"] = "2026-08-15T09:00:00Z"

    assert json.loads(serialize_schema2_document(document))["accounts"][0][
        "tracker_evidence"
    ][0]["coverage"] == "insufficient"


@pytest.mark.parametrize("location", ["root", "account", "freshness", "limit", "evidence"])
def test_schema2_serializer_rejects_unknown_and_secret_like_fields(location):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    document = _valid_schema2_document(with_evidence=True)
    account = document["accounts"][0]
    targets = {
        "root": document,
        "account": account,
        "freshness": account["freshness"],
        "limit": account["limits"][0],
        "evidence": account["tracker_evidence"][0],
    }
    targets[location]["password"] = "synthetic-marker"

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rate_percentage_points_per_second", float("nan")),
        ("rate_percentage_points_per_second", float("inf")),
        ("rate_percentage_points_per_second", -1.0),
        ("rate_percentage_points_per_second", 100.000001),
        ("projected_used_percent_at_reset", float("nan")),
        ("projected_used_percent_at_reset", float("inf")),
        ("projected_used_percent_at_reset", -1.0),
        ("projected_used_percent_at_reset", 100.000001),
        ("sample_count", 0),
        ("sample_count", snapshot_module.MAX_HISTORY_SAMPLES + 1),
    ],
)
def test_schema2_serializer_rejects_nonfinite_and_out_of_range_tracker_values(field, value):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    document = _valid_schema2_document(with_evidence=True)
    document["accounts"][0]["tracker_evidence"][0][field] = value

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)


def test_schema2_serializer_rejects_overlong_ids_and_invalid_limit_evidence_pair():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    document = _valid_schema2_document(with_evidence=True)
    account = document["accounts"][0]
    account["tracker_evidence"][0]["reset_generation"] = "r" * 129
    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)

    document = _valid_schema2_document(with_evidence=True)
    document["accounts"][0]["tracker_evidence"][0]["pool"] = "other"
    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)

    document = _valid_schema2_document()
    document["accounts"][0]["account_id"] = "a" * 65
    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)


def test_schema2_serializer_rejects_absolute_local_path_tokens():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    document = _valid_schema2_document(with_evidence=True)
    account = document["accounts"][0]
    account["limits"][0]["pool"] = "/home/synthetic/private"
    account["tracker_evidence"][0]["pool"] = "/home/synthetic/private"

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)


@pytest.mark.parametrize(
    "token",
    [
        "file:///home/synthetic/private",
        "reset:/home/synthetic/private",
        "reset-/home/synthetic/private",
        "reset_/home/synthetic/private",
        "reset;/home/synthetic/private",
        "reset./home/synthetic/private",
    ],
)
def test_schema2_serializer_rejects_prefixed_absolute_local_path_tokens(token):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    document = _valid_schema2_document(with_evidence=True)
    document["accounts"][0]["tracker_evidence"][0]["reset_generation"] = token

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)

    for benign in ("reset:main/5h", "provider/model", "team/name/v2"):
        valid = _valid_schema2_document(with_evidence=True)
        valid["accounts"][0]["tracker_evidence"][0]["reset_generation"] = benign
        assert serialize_schema2_document(valid)


def test_schema2_account_iterable_accepts_max_and_stops_at_max_plus_one(monkeypatch):
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    monkeypatch.setattr(snapshot_module, "_MAX_ACCOUNTS", 2)
    assert len(
        build_schema2_document(
            (_usage("alpha"), _usage("beta")),
            generated_at=GENERATED,
        )["accounts"]
    ) == 2

    def over_cap():
        yield _usage("alpha")
        yield _usage("beta")
        yield _usage("gamma")
        pytest.fail("account iterable consumed beyond MAX+1")

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document(over_cap(), generated_at=GENERATED)
    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document(repeat(_usage("alpha")), generated_at=GENERATED)


def test_schema2_history_iterable_accepts_max_and_stops_at_max_plus_one(monkeypatch):
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    monkeypatch.setattr(snapshot_module, "MAX_HISTORY_SAMPLES", 2)
    exact = (
        _tracker_sample(CAPTURED_ALPHA - timedelta(minutes=10), 15.0),
        _tracker_sample(CAPTURED_ALPHA, 25.0),
    )
    assert build_schema2_document(
        (_usage_with_tracker_limit(),),
        generated_at=GENERATED,
        tracker_samples={("alpha", "main", 18_000): exact},
    )["accounts"][0]["tracker_evidence"]

    def over_cap():
        yield from exact
        yield _tracker_sample(CAPTURED_ALPHA + timedelta(minutes=1), 26.0)
        pytest.fail("history iterable consumed beyond MAX+1")

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document(
            (_usage_with_tracker_limit(),),
            generated_at=GENERATED,
            tracker_samples={("alpha", "main", 18_000): over_cap()},
        )
    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document(
            (_usage_with_tracker_limit(),),
            generated_at=GENERATED,
            tracker_samples={
                ("alpha", "main", 18_000): repeat(exact[0]),
            },
        )


def test_schema2_tracker_series_mapping_accepts_max_and_rejects_max_plus_one(monkeypatch):
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    monkeypatch.setattr(snapshot_module, "_MAX_TRACKER_SERIES", 2)
    spark_reset = RESET + timedelta(days=1)
    usage = replace(
        _usage_with_tracker_limit(),
        models=(
            _pool(
                "gpt-5.3-codex-spark",
                (
                    LimitWindow(
                        name="weekly",
                        percent=60.0,
                        duration_seconds=604_800,
                        reset_at=spark_reset,
                    ),
                ),
            ),
        ),
    )
    exact = {
        ("alpha", "main", 18_000): (_tracker_sample(CAPTURED_ALPHA, 25.0),),
        ("alpha", "gpt-5.3-codex-spark", 604_800): (
            _tracker_sample(
                CAPTURED_ALPHA,
                40.0,
                pool="gpt-5.3-codex-spark",
                window_seconds=604_800,
                reset_at=spark_reset,
                reset_generation="reset-spark",
            ),
        ),
    }

    account = build_schema2_document(
        (usage,),
        generated_at=GENERATED,
        tracker_samples=exact,
    )["accounts"][0]
    assert len(account["tracker_evidence"]) == 2

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document(
            (usage,),
            generated_at=GENERATED,
            tracker_samples={
                **exact,
                ("alpha", "main", 604_800): (),
            },
        )


def test_schema2_document_and_trend_size_boundaries(monkeypatch):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    document = _valid_schema2_document(with_evidence=True)
    payload = serialize_schema2_document(document)
    monkeypatch.setattr(snapshot_module, "_MAX_DOCUMENT_BYTES", len(payload))
    assert serialize_schema2_document(document) == payload
    monkeypatch.setattr(snapshot_module, "_MAX_DOCUMENT_BYTES", len(payload) - 1)
    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)

    monkeypatch.setattr(snapshot_module, "_MAX_DOCUMENT_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(snapshot_module, "_MAX_TRACKER_EVIDENCE_PER_ACCOUNT", 1)
    serialized_account = json.loads(serialize_schema2_document(document))["accounts"][0]
    assert len(serialized_account["tracker_evidence"]) == 1
    document["accounts"][0]["tracker_evidence"].append(
        dict(document["accounts"][0]["tracker_evidence"][0])
    )
    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("used_percent", float("nan")),
        ("used_percent", float("inf")),
        ("remaining_percent", -1.0),
        ("remaining_percent", 100.000001),
        ("window_seconds", 86_400),
    ],
)
def test_schema2_serializer_rejects_invalid_limit_values(field, value):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    document = _valid_schema2_document(with_evidence=True)
    document["accounts"][0]["limits"][0][field] = value

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)


@pytest.mark.parametrize(
    ("complement_delta", "accepted"),
    [
        pytest.param(0.5e-9, True, id="positive-inside"),
        pytest.param(-0.5e-9, True, id="negative-inside"),
        pytest.param(2e-9, False, id="positive-outside"),
        pytest.param(-2e-9, False, id="negative-outside"),
    ],
)
def test_schema2_limit_complement_uses_only_absolute_tolerance(
    complement_delta,
    accepted,
):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    document = _valid_schema2_document(with_evidence=True)
    document["accounts"][0]["limits"][0]["remaining_percent"] = 75.0 + complement_delta

    if accepted:
        serialize_schema2_document(document)
    else:
        with pytest.raises(IntegrationInvalidSource):
            serialize_schema2_document(document)


def test_schema2_serializer_rejects_duplicate_tracker_identity():
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    document = _valid_schema2_document(with_evidence=True)
    evidence = document["accounts"][0]["tracker_evidence"]
    evidence.append(dict(evidence[0]))

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)


@pytest.mark.parametrize(
    "reset_at",
    [
        "2026-08-15T10:00:00Z",
        "2026-08-15T10:04:00Z",
    ],
)
def test_schema2_serializer_rejects_tracker_reset_not_after_generation(reset_at):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    document = _valid_schema2_document(with_evidence=True)
    document["accounts"][0]["limits"][0]["reset_at"] = reset_at

    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(document)


def test_schema2_builder_rejects_tracker_reset_mismatch():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    mismatched_reset = RESET + timedelta(minutes=1)
    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document(
            (_usage_with_tracker_limit(),),
            generated_at=GENERATED,
            tracker_samples={
                ("alpha", "main", 18_000): (
                    _tracker_sample(
                        CAPTURED_ALPHA,
                        25.0,
                        reset_at=mismatched_reset,
                    ),
                )
            },
        )


def test_schema2_builder_rejects_tracker_samples_from_another_account():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document(
            (_usage_with_tracker_limit(),),
            generated_at=GENERATED,
            tracker_samples={
                ("alpha", "main", 18_000): (
                    _tracker_sample(
                        CAPTURED_ALPHA - timedelta(minutes=10),
                        15.0,
                        account_id="beta",
                    ),
                    _tracker_sample(CAPTURED_ALPHA, 25.0, account_id="beta"),
                )
            },
        )


def test_schema2_builder_rejects_duplicate_pool_window_with_different_resets():
    from codex_usage.integration_snapshot import IntegrationInvalidSource, build_schema2_document

    windows = (
        LimitWindow(name="5h", percent=75.0, reset_at=RESET),
        LimitWindow(
            name="5h",
            percent=75.0,
            reset_at=RESET + timedelta(minutes=1),
        ),
    )
    usage = replace(_usage("alpha"), main=_pool("main", windows))

    with pytest.raises(IntegrationInvalidSource):
        build_schema2_document((usage,), generated_at=GENERATED)



def test_snapshot_serializer_rejects_oversized_payload(monkeypatch):
    from codex_usage.integration_snapshot import (
        IntegrationInvalidSource,
        serialize_schema2_document,
    )

    monkeypatch.setattr(snapshot_module, "_MAX_DOCUMENT_BYTES", 1)
    with pytest.raises(IntegrationInvalidSource):
        serialize_schema2_document(
            {
                "accounts": [],
                "generated_at": "2026-08-15T10:05:00Z",
                "schema_version": 2,
            }
        )
