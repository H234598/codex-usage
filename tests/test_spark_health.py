import json
import os
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

from codex_usage.spark_health import (
    SPARK_HEALTH_MAX_RECORDS,
    _health_key,
    set_spark_health,
    spark_health_status,
)

NOW = datetime(2026, 7, 16, 4, 0, tzinfo=UTC)


class _RaisingTimezone(tzinfo):
    def utcoffset(self, _value):
        raise RuntimeError("synthetic timezone marker")


class _BrokenInt(int):
    def __lt__(self, _other):
        raise RuntimeError("synthetic max-age marker")


class _BrokenStr(str):
    def __eq__(self, _other):
        raise RuntimeError("synthetic health state marker")

    def encode(self, *args, **kwargs):
        raise RuntimeError("synthetic backend id marker")


def test_default_spark_health_path_uses_default_state_dir(monkeypatch, tmp_path):
    import codex_usage.spark_health as spark_health_module

    monkeypatch.setattr(spark_health_module, "default_state_dir", lambda: tmp_path)

    assert spark_health_module.default_spark_health_path() == tmp_path / "spark-health.json"
    assert spark_health_module._spark_health_path(None) == tmp_path / "spark-health.json"


def test_spark_health_defaults_to_unknown(tmp_path):
    result = spark_health_status("backend-nufker", path=tmp_path / "health.json", now=NOW)

    assert result["state"] == "unknown"
    assert result["reason"] == "no_successful_spark_turn"


def test_spark_health_rejects_primitive_subclasses_before_operations(tmp_path):
    with pytest.raises(ValueError, match="max_age_seconds"):
        spark_health_status(
            "backend-nufker",
            path=tmp_path / "health.json",
            now=NOW,
            max_age_seconds=_BrokenInt(3_600),
        )

    with pytest.raises(ValueError, match="backend_account_id"):
        set_spark_health(
            _BrokenStr("backend-nufker"),
            "healthy",
            path=tmp_path / "health.json",
            now=NOW,
        )

    with pytest.raises(ValueError, match="spark health state"):
        set_spark_health(
            "backend-nufker",
            _BrokenStr("healthy"),
            path=tmp_path / "health.json",
            now=NOW,
        )


def test_spark_health_rejects_overlong_backend_id(tmp_path):
    with pytest.raises(ValueError, match="backend_account_id is invalid"):
        set_spark_health(
            "u" * 257,
            "healthy",
            path=tmp_path / "health.json",
            now=NOW,
        )

    result = spark_health_status(
        "u" * 257,
        path=tmp_path / "health.json",
        now=NOW,
    )
    assert result["state"] == "unknown"
    assert result["reason"] == "missing_backend_account_id"


def test_spark_health_ignores_deeply_nested_json(tmp_path):
    nested_json = "[" * 2_000 + "]" * 2_000
    path = tmp_path / "health.json"
    path.write_text(nested_json, encoding="utf-8")

    result = spark_health_status("backend-nufker", path=path, now=NOW)

    assert result["state"] == "unknown"
    assert result["reason"] == "no_successful_spark_turn"


def test_spark_health_rejects_symlink_to_missing_file(tmp_path):
    path = tmp_path / "health-link"
    path.symlink_to(tmp_path / "missing-health.json")

    with pytest.raises(ValueError, match="regular file"):
        spark_health_status("backend-nufker", path=path, now=NOW)


def test_spark_health_recovers_from_private_io_error(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(
        "codex_usage.spark_health.read_private_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read blocked")),
    )

    result = spark_health_status("backend-nufker", path=path, now=NOW)

    assert result["reason"] == "no_successful_spark_turn"


def test_spark_health_rejects_non_mapping_records(tmp_path):
    path = tmp_path / "health.json"
    path.write_text(json.dumps({"version": 1, "records": []}), encoding="utf-8")
    path.chmod(0o600)

    result = spark_health_status("backend-nufker", path=path, now=NOW)

    assert result["reason"] == "no_successful_spark_turn"


def test_spark_health_rejects_invalid_record_timestamp(tmp_path):
    path = tmp_path / "health.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    _health_key("backend-nufker"): {
                        "state": "healthy",
                        "checked_at": "not-a-timestamp",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    result = spark_health_status("backend-nufker", path=path, now=NOW)

    assert result["reason"] == "invalid_spark_health_record"
    import codex_usage.spark_health as spark_health_module

    assert spark_health_module._parse_timestamp(None) is None


def test_spark_health_success_is_fresh_until_expiry(tmp_path):
    path = tmp_path / "health.json"
    set_spark_health("backend-nufker", "healthy", path=path, now=NOW)

    fresh = spark_health_status("backend-nufker", path=path, now=NOW + timedelta(minutes=30))
    stale = spark_health_status("backend-nufker", path=path, now=NOW + timedelta(hours=2))

    assert fresh["state"] == "healthy"
    assert stale["state"] == "unknown"
    assert stale["reason"] == "spark_health_stale"


def test_spark_health_status_ignores_group_readable_file(tmp_path):
    path = tmp_path / "health.json"
    set_spark_health("backend-nufker", "healthy", path=path, now=NOW)
    path.chmod(0o640)

    result = spark_health_status("backend-nufker", path=path, now=NOW)

    assert result["state"] == "unknown"
    assert result["reason"] == "no_successful_spark_turn"


def test_spark_health_status_ignores_hard_linked_file(tmp_path):
    path = tmp_path / "health.json"
    set_spark_health("backend-nufker", "healthy", path=path, now=NOW)
    os.link(path, tmp_path / "health-copy.json")

    assert path.stat().st_nlink == 2
    result = spark_health_status("backend-nufker", path=path, now=NOW)

    assert result["state"] == "unknown"
    assert result["reason"] == "no_successful_spark_turn"


def test_spark_health_status_rejects_more_than_max_records(tmp_path):
    path = tmp_path / "health.json"
    records = {
        _health_key("backend-nufker"): {
            "state": "healthy",
            "checked_at": NOW.isoformat(),
        }
    }
    records.update(
        {
            f"{index:064x}": {
                "state": "healthy",
                "checked_at": NOW.isoformat(),
            }
            for index in range(SPARK_HEALTH_MAX_RECORDS)
        }
    )
    path.write_text(
        json.dumps({"version": 1, "records": records}),
        encoding="utf-8",
    )
    path.chmod(0o600)

    result = spark_health_status("backend-nufker", path=path, now=NOW)

    assert result["state"] == "unknown"
    assert result["reason"] == "no_successful_spark_turn"


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_spark_health_status_requires_strict_version(tmp_path, version):
    path = tmp_path / "health.json"
    path.write_text(
        json.dumps(
            {
                "version": version,
                "records": {
                    _health_key("backend-nufker"): {
                        "state": "healthy",
                        "checked_at": NOW.isoformat(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    result = spark_health_status("backend-nufker", path=path, now=NOW)

    assert result["state"] == "unknown"
    assert result["reason"] == "no_successful_spark_turn"


def test_spark_health_failure_stays_fail_closed(tmp_path):
    path = tmp_path / "health.json"
    set_spark_health("backend-nufker", "failed", reason="spark_turn_timeout", path=path, now=NOW)

    result = spark_health_status("backend-nufker", path=path, now=NOW + timedelta(days=30))

    assert result["state"] == "failed"
    assert result["reason"] == "spark_turn_timeout"


def test_spark_health_rejects_naive_checked_at(tmp_path):
    path = tmp_path / "health.json"
    set_spark_health("backend-nufker", "healthy", path=path, now=NOW)
    payload = json.loads(path.read_text())
    record = next(iter(payload["records"].values()))
    record["checked_at"] = "2026-07-16T04:00:00"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)

    result = spark_health_status("backend-nufker", path=path, now=NOW)

    assert result["state"] == "unknown"
    assert result["reason"] == "invalid_spark_health_record"


@pytest.mark.parametrize(
    "checked_at",
    ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"],
)
def test_spark_health_rejects_unrepresentable_checked_at(tmp_path, checked_at):
    path = tmp_path / "health.json"
    set_spark_health("backend-nufker", "healthy", path=path, now=NOW)
    payload = json.loads(path.read_text())
    record = next(iter(payload["records"].values()))
    record["checked_at"] = checked_at
    path.write_text(json.dumps(payload))
    path.chmod(0o600)

    result = spark_health_status("backend-nufker", path=path, now=NOW)

    assert result["state"] == "unknown"
    assert result["reason"] == "invalid_spark_health_record"


def test_spark_health_fails_closed_for_invalid_clock(tmp_path):
    path = tmp_path / "health.json"
    set_spark_health("backend-nufker", "healthy", path=path, now=NOW)

    result = spark_health_status(
        "backend-nufker",
        path=path,
        now=NOW.replace(tzinfo=None),
    )

    assert result == {
        "state": "unknown",
        "reason": "invalid_health_clock",
        "checked_at": None,
        "stale": False,
    }


@pytest.mark.parametrize("invalid_now", [0, "now", NOW.replace(tzinfo=None)])
def test_set_spark_health_rejects_invalid_clock(tmp_path, invalid_now):
    with pytest.raises(ValueError, match="timezone-aware"):
        set_spark_health(
            "backend-nufker",
            "healthy",
            path=tmp_path / "health.json",
            now=invalid_now,
        )


def test_spark_health_rejects_timezone_callbacks_that_raise(tmp_path):
    invalid_now = datetime(2026, 7, 16, 4, 0, tzinfo=_RaisingTimezone())

    with pytest.raises(ValueError, match="timezone-aware"):
        set_spark_health(
            "backend-nufker",
            "healthy",
            path=tmp_path / "health.json",
            now=invalid_now,
        )

    result = spark_health_status(
        "backend-nufker",
        path=tmp_path / "health.json",
        now=invalid_now,
    )
    assert result["reason"] == "invalid_health_clock"


@pytest.mark.parametrize(
    "invalid_now",
    [
        datetime.min.replace(tzinfo=timezone(timedelta(hours=14))),
        datetime.max.replace(tzinfo=timezone(timedelta(hours=-14))),
    ],
)
def test_set_spark_health_rejects_unrepresentable_clock(tmp_path, invalid_now):
    with pytest.raises(ValueError, match="timestamp is out of range"):
        set_spark_health(
            "backend-nufker",
            "healthy",
            path=tmp_path / "health.json",
            now=invalid_now,
        )


@pytest.mark.parametrize("path", [[], "invalid", 1, False, object()])
def test_spark_health_rejects_non_path(path):
    with pytest.raises(ValueError, match="spark health path is invalid"):
        spark_health_status("backend-nufker", path=path)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="spark health path is invalid"):
        set_spark_health("backend-nufker", "healthy", path=path)  # type: ignore[arg-type]


def test_refreshing_old_account_keeps_health_record_in_bounded_rotation(tmp_path):
    path = tmp_path / "health.json"
    for index in range(SPARK_HEALTH_MAX_RECORDS):
        set_spark_health(f"backend-{index}", "healthy", path=path, now=NOW)

    set_spark_health(
        "backend-0",
        "failed",
        reason="spark_turn_timeout",
        path=path,
        now=NOW,
    )
    set_spark_health("backend-new", "healthy", path=path, now=NOW)

    result = spark_health_status("backend-0", path=path, now=NOW)

    assert result["state"] == "failed"
    assert result["reason"] == "spark_turn_timeout"


def test_spark_health_write_fails_when_directory_chmod_fails(tmp_path, monkeypatch):
    path = tmp_path / "spark-health-dir" / "health.json"
    original_fchmod = os.fchmod

    def fail_fchmod(fd, mode):
        if Path(os.readlink(f"/proc/self/fd/{fd}")) == path.parent:
            raise PermissionError("spark health directory chmod blocked")
        return original_fchmod(fd, mode)

    monkeypatch.setattr("codex_usage.private_io.os.fchmod", fail_fchmod)

    with pytest.raises(PermissionError, match="spark health directory chmod blocked"):
        set_spark_health("backend-nufker", "healthy", path=path, now=NOW)

    assert not path.exists()


def test_spark_health_rejects_oversized_payload(tmp_path, monkeypatch):
    import codex_usage.spark_health as spark_health_module

    monkeypatch.setattr(spark_health_module, "SPARK_HEALTH_MAX_BYTES", 1)

    with pytest.raises(ValueError, match="file is too large"):
        set_spark_health("backend-nufker", "healthy", path=tmp_path / "health.json", now=NOW)
