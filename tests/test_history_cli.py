import json
from datetime import UTC, datetime, timedelta

from codex_usage.cli import main
from codex_usage.history import HistoryStore, UsageSample


def _sample(captured_at: datetime, used_percent: float) -> UsageSample:
    return UsageSample(
        account_id="alpha",
        pool="main",
        window_seconds=18_000,
        captured_at=captured_at,
        used_percent=used_percent,
        reset_generation="a",
        source="test",
    )


def test_history_status_json(tmp_path, capsys):
    path = tmp_path / "history.sqlite3"
    with HistoryStore(path) as store:
        store.record(_sample(datetime(2026, 8, 16, 10, tzinfo=UTC), 10))

    assert main(["history", "status", "--path", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sample_count"] == 1
    assert payload["schema_version"] == "1"


def test_consumption_cli_reads_private_history(tmp_path, capsys):
    path = tmp_path / "history.sqlite3"
    base = datetime(2026, 8, 16, 10, tzinfo=UTC)
    with HistoryStore(path) as store:
        store.record(_sample(base - timedelta(hours=1), 10))
        store.record(_sample(base - timedelta(minutes=30), 10))
        store.record(_sample(base, 25))

    assert main(
        [
            "consumption",
            "--account",
            "alpha",
            "--amount",
            "1",
            "--unit",
            "hours",
            "--path",
            str(path),
            "--now",
            base.isoformat(),
            "--format",
            "json",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["account_id"] == "alpha"
    assert payload["windows"][0]["consumed_percentage_points"] == 15.0
    assert payload["windows"][0]["estimated_seconds_to_exhaustion"] == 18_000


def test_consumption_cli_accepts_week_units(tmp_path, capsys):
    path = tmp_path / "history.sqlite3"
    base = datetime(2026, 8, 16, 10, tzinfo=UTC)
    with HistoryStore(path) as store:
        store.record(_sample(base - timedelta(hours=1), 10))
        store.record(_sample(base, 25))

    assert main(
        [
            "consumption",
            "--account",
            "alpha",
            "--amount",
            "1",
            "--unit",
            "weeks",
            "--path",
            str(path),
            "--now",
            base.isoformat(),
            "--format",
            "json",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["windows"][0]["lookback_seconds"] == 604_800


def test_consumption_cli_all_includes_monthly_window(tmp_path, capsys):
    path = tmp_path / "history.sqlite3"

    assert main(
        [
            "consumption",
            "--account",
            "alpha",
            "--amount",
            "1",
            "--unit",
            "hours",
            "--limit-window",
            "all",
            "--path",
            str(path),
            "--format",
            "json",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [window["limit_window_seconds"] for window in payload["windows"]] == [
        18_000,
        604_800,
        2_592_000,
    ]


def test_consumption_cli_all_includes_other_history_windows(tmp_path, capsys):
    path = tmp_path / "history.sqlite3"
    base = datetime(2026, 8, 16, 10, tzinfo=UTC)
    other_samples = [
        UsageSample(
            account_id="alpha",
            pool="main",
            window_seconds=86_400,
            captured_at=base - timedelta(minutes=30),
            used_percent=10,
            reset_generation="a",
            source="test",
        ),
        UsageSample(
            account_id="alpha",
            pool="main",
            window_seconds=86_400,
            captured_at=base,
            used_percent=25,
            reset_generation="a",
            source="test",
        ),
    ]
    with HistoryStore(path) as store:
        store.record_many(tuple(other_samples))

    assert main(
        [
            "consumption",
            "--account",
            "alpha",
            "--amount",
            "1",
            "--unit",
            "hours",
            "--limit-window",
            "all",
            "--path",
            str(path),
            "--now",
            base.isoformat(),
            "--format",
            "json",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [window["limit_window_seconds"] for window in payload["windows"]] == [
        18_000,
        604_800,
        2_592_000,
        86_400,
    ]


def test_consumption_cli_all_includes_all_configurable_history_windows(tmp_path, capsys):
    path = tmp_path / "history.sqlite3"
    base = datetime(2026, 8, 16, 10, tzinfo=UTC)
    durations = tuple(86_400 + offset for offset in range(33))
    with HistoryStore(path) as store:
        store.record_many(
            tuple(
                UsageSample(
                    account_id="alpha",
                    pool="main",
                    window_seconds=duration,
                    captured_at=base,
                    used_percent=10,
                    reset_generation="a",
                    source="test",
                )
                for duration in durations
            )
        )

    assert main(
        [
            "consumption",
            "--account",
            "alpha",
            "--amount",
            "1",
            "--unit",
            "hours",
            "--limit-window",
            "all",
            "--path",
            str(path),
            "--now",
            base.isoformat(),
            "--format",
            "json",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [window["limit_window_seconds"] for window in payload["windows"]] == [
        18_000,
        604_800,
        2_592_000,
        *durations,
    ]


def test_consumption_cli_uses_bounded_history_query(tmp_path, monkeypatch, capsys):
    path = tmp_path / "history.sqlite3"
    base = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
    with HistoryStore(path) as store:
        store.record(_sample(base, 10))

    def unexpected_samples(*args, **kwargs):
        raise AssertionError("consumption must use bounded history query")

    monkeypatch.setattr(HistoryStore, "samples", unexpected_samples)
    assert main(
        [
            "consumption",
            "--account",
            "alpha",
            "--amount",
            "1",
            "--unit",
            "hours",
            "--path",
            str(path),
            "--now",
            base.isoformat(),
            "--format",
            "json",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["windows"][0]["coverage"] == "insufficient"
