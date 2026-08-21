import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codex_usage.spark_health import (
    SPARK_HEALTH_MAX_RECORDS,
    _health_key,
    set_spark_health,
    spark_health_status,
)

NOW = datetime(2026, 7, 16, 4, 0, tzinfo=UTC)


def test_spark_health_defaults_to_unknown(tmp_path):
    result = spark_health_status("backend-nufker", path=tmp_path / "health.json", now=NOW)

    assert result["state"] == "unknown"
    assert result["reason"] == "no_successful_spark_turn"


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

    def fail_chmod(_path, _mode):
        raise PermissionError("spark health directory chmod blocked")

    monkeypatch.setattr(Path, "chmod", fail_chmod)

    with pytest.raises(PermissionError, match="spark health directory chmod blocked"):
        set_spark_health("backend-nufker", "healthy", path=path, now=NOW)

    assert not path.exists()
