import json

from codex_usage.cli import main
from codex_usage.profile_login import DeviceLoginResult


def test_profile_layout_cli_creates_only_nonsecret_layout(tmp_path, capsys):
    config = tmp_path / "config.toml"
    profile = tmp_path / "profile"
    config.write_text(
        "[[accounts]]\n"
        'id = "alpha"\n'
        'label = "Alpha"\n'
        f'profile_dir = "{profile}"\n',
        encoding="utf-8",
    )
    config.chmod(0o600)

    assert main(["--config", str(config), "profile", "layout", "--account", "alpha"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["auth_json"] == str(profile / "codex-home" / "auth.json")


def test_profile_migration_cli_dry_run_does_not_copy_auth(tmp_path, capsys):
    config = tmp_path / "config.toml"
    profile = tmp_path / "profile"
    source = tmp_path / "auth.json"
    source.write_text("{}", encoding="utf-8")
    source.chmod(0o600)
    config.write_text(
        "[[accounts]]\n"
        'id = "alpha"\n'
        'label = "Alpha"\n'
        f'profile_dir = "{profile}"\n'
        f'auth_json_path = "{source}"\n',
        encoding="utf-8",
    )
    config.chmod(0o600)

    assert (
        main(
            [
                "--config",
                str(config),
                "profile",
                "migrate-auth",
                "--dry-run",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dry_run"
    assert payload["items"][0]["status"] == "planned"
    assert not (profile / "codex-home" / "auth.json").exists()


def test_profile_device_login_cli_returns_ephemeral_events(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.toml"
    profile = tmp_path / "profile"
    config.write_text(
        "[[accounts]]\n"
        'id = "alpha"\n'
        'label = "Alpha"\n'
        f'profile_dir = "{profile}"\n',
        encoding="utf-8",
    )
    config.chmod(0o600)
    monkeypatch.setattr(
        "codex_usage.cli.run_device_login",
        lambda *args, **kwargs: DeviceLoginResult(
            True,
            "alpha",
            events=(),
        ),
    )

    assert main(
        [
            "--config",
            str(config),
            "profile",
            "device-login",
            "--account",
            "alpha",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"account": "alpha", "error": None, "events": [], "ok": True}


def test_profile_jobs_cli_returns_active_jobs(monkeypatch, capsys):
    monkeypatch.setattr(
        "codex_usage.cli.list_profile_jobs",
        lambda account_id=None: [{
            "account": account_id or "alpha",
            "job_id": "job-123",
            "ok": True,
            "status": "running",
        }],
    )

    assert main(["profile", "jobs", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "jobs": [{
            "account": "alpha",
            "job_id": "job-123",
            "ok": True,
            "status": "running",
        }],
        "ok": True,
    }
