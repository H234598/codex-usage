from datetime import UTC, datetime, timedelta

from codex_usage.spark_health import set_spark_health, spark_health_status

NOW = datetime(2026, 7, 16, 4, 0, tzinfo=UTC)


def test_spark_health_defaults_to_unknown(tmp_path):
    result = spark_health_status("backend-nufker", path=tmp_path / "health.json", now=NOW)

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


def test_spark_health_failure_stays_fail_closed(tmp_path):
    path = tmp_path / "health.json"
    set_spark_health("backend-nufker", "failed", reason="spark_turn_timeout", path=path, now=NOW)

    result = spark_health_status("backend-nufker", path=path, now=NOW + timedelta(days=30))

    assert result["state"] == "failed"
    assert result["reason"] == "spark_turn_timeout"
