from __future__ import annotations

import base64
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from codex_usage.integration_attestation import ActiveRelease, VerifiedActiveManifest
from codex_usage.private_io import (
    FileIdentity,
    IntegrationEvidenceInvalid,
    IntegrationEvidenceUnavailable,
)

pytest_plugins = ("test_integration_evidence",)


def _flatten_exception_group(exc: BaseException) -> list[BaseException]:
    flattened: list[BaseException] = []
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            flattened.extend(_flatten_exception_group(nested))
    else:
        flattened.append(exc)
    if exc.__cause__ is not None:
        flattened.extend(_flatten_exception_group(exc.__cause__))
    return flattened


def test_project_installs_dedicated_integration_watchdog_script():
    """Missing script entry point leaves the rendered systemd unit unlaunchable."""
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"]["codex-usage-integration-watchdog"] == (
        "codex_usage.integration_watchdog:main"
    )


def test_systemd_timeout_contract_exceeds_publisher_timeout_with_headroom():
    """Would fail if systemd could kill the unit before the publisher RC75 path."""
    from codex_usage import integration_watchdog

    assert integration_watchdog.GENERIC_WATCHDOG_TIMEOUT_SECONDS == 30
    assert integration_watchdog.ATTESTATION_TIMEOUT_SECONDS == 20
    assert integration_watchdog.RUNTIME_SELF_ATTESTATION_TIMEOUT_SECONDS == 20
    assert integration_watchdog.PUBLISH_TIMEOUT_SECONDS == 180
    assert integration_watchdog.PROCESS_CLEANUP_TIMEOUT_SECONDS == 8
    assert integration_watchdog.TOTAL_RUNTIME_BUDGET_SECONDS == 258
    assert integration_watchdog.SYSTEMD_GRACE_SECONDS >= 10
    assert (
        integration_watchdog.GENERIC_WATCHDOG_TIMEOUT_SECONDS
        + integration_watchdog.ATTESTATION_TIMEOUT_SECONDS
        + integration_watchdog.RUNTIME_SELF_ATTESTATION_TIMEOUT_SECONDS
        + integration_watchdog.PUBLISH_TIMEOUT_SECONDS
        + integration_watchdog.PROCESS_CLEANUP_TIMEOUT_SECONDS
        <= integration_watchdog.TOTAL_RUNTIME_BUDGET_SECONDS
    )
    assert integration_watchdog.SYSTEMD_TIMEOUT_START_SECONDS == 270
    assert (
        integration_watchdog.SYSTEMD_TIMEOUT_START_SECONDS
        - integration_watchdog.TOTAL_RUNTIME_BUDGET_SECONDS
        >= integration_watchdog.SYSTEMD_GRACE_SECONDS
    )


def test_execute_passes_remaining_monotonic_budget_to_each_stage(tmp_path, monkeypatch):
    """Would fail if generic, attestation, or publisher ran without a deadline."""
    from codex_usage import integration_watchdog

    environment = _environment(tmp_path)
    config_path = tmp_path / "config/codex-usage/config.toml"
    trusted_entrypoint = tmp_path / "core/codex_usage/integration_entrypoint.py"
    verified = _verified_manifest(tmp_path)
    calls: list[tuple[str, float]] = []
    publisher_timeouts: list[float] = []
    monotonic_values = iter((100.0, 100.0, 112.5, 230.0, 250.0))

    def fake_timeout(label: str, timeout_seconds: float, callback):
        calls.append((label, timeout_seconds))
        return callback()

    monkeypatch.setattr(integration_watchdog, "_call_with_timeout", fake_timeout)
    monkeypatch.setattr(
        integration_watchdog,
        "_runtime_self_attestation",
        lambda **_kwargs: None,
    )

    assert integration_watchdog.execute(
        ("--config", str(config_path)),
        environ=environment,
        runtime_entrypoint_path=trusted_entrypoint,
        watchdog_runner=lambda _path, **_kwargs: 0,
        verifier=lambda **_kwargs: verified,
        publisher_runner=lambda _launcher, _argv, timeout, **_kwargs: publisher_timeouts.append(
            timeout
        )
        or 0,
        monotonic=lambda: next(monotonic_values),
    ) == 0
    assert calls == [
        ("generic watchdog", 30.0),
        ("private service runtime attestation", 20.0),
        ("private service runtime self attestation", 20.0),
    ]
    assert publisher_timeouts == [100.0]


def test_execute_publisher_only_never_runs_the_generic_cli_stage(tmp_path, monkeypatch):
    """The V2 service contract publishes attested evidence without refreshing usage."""
    from codex_usage import integration_watchdog

    environment = _environment(tmp_path)
    verified = _verified_manifest(tmp_path)
    published: list[tuple[Path, tuple[str, ...]]] = []
    monkeypatch.setattr(
        integration_watchdog,
        "_runtime_self_attestation",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        integration_watchdog,
        "_run_watchdog_stage",
        lambda *_args, **_kwargs: pytest.fail("generic CLI path must not run"),
    )

    assert integration_watchdog.execute_publisher_only(
        (),
        environ=environment,
        runtime_entrypoint_path=tmp_path / "trusted.py",
        verifier=lambda **_kwargs: verified,
        publisher_runner=lambda launcher, argv, _timeout, **_kwargs: published.append(
            (launcher, argv)
        )
        or 0,
    ) == 0
    assert published == [
        (verified.active_release.launcher_path, integration_watchdog.PUBLISH_ARGV)
    ]


def test_execute_publisher_only_reports_invalid_arguments_without_input_data(
    tmp_path,
    capsys,
):
    """An invalid unit argv must be diagnosable without echoing its contents."""
    from codex_usage import integration_watchdog

    secret_argument = "publisher-argument-secret"

    assert integration_watchdog.execute_publisher_only(
        (secret_argument,),
        environ=_environment(tmp_path),
        runtime_entrypoint_path=tmp_path / "runtime.py",
        verifier=lambda **_kwargs: pytest.fail("attestation after invalid argv"),
        publisher_runner=lambda *_args, **_kwargs: pytest.fail("publish after invalid argv"),
    ) == 64

    diagnostic = capsys.readouterr().err
    assert "stage=arguments" in diagnostic
    assert "rc=64" in diagnostic
    assert "exception=ValueError" in diagnostic
    assert secret_argument not in diagnostic
    assert len(diagnostic.encode()) <= 512


def test_execute_publisher_only_reports_initial_attestation_failure_without_message(
    tmp_path,
    capsys,
):
    """Unavailable private runtime evidence must retain its RC69 cause class."""
    from codex_usage import integration_watchdog

    secret_message = "initial-attestation-secret"

    def unavailable(**_kwargs):
        raise IntegrationEvidenceUnavailable(secret_message)

    assert integration_watchdog.execute_publisher_only(
        (),
        environ=_environment(tmp_path),
        runtime_entrypoint_path=tmp_path / "runtime.py",
        verifier=unavailable,
        publisher_runner=lambda *_args, **_kwargs: pytest.fail("publish after unavailable"),
    ) == 69

    diagnostic = capsys.readouterr().err
    assert "stage=initial_runtime_attestation" in diagnostic
    assert "rc=69" in diagnostic
    assert "exception=IntegrationEvidenceUnavailable" in diagnostic
    assert secret_message not in diagnostic
    assert len(diagnostic.encode()) <= 512


def test_execute_publisher_only_reports_self_attestation_failure_without_message(
    tmp_path,
    monkeypatch,
    capsys,
):
    """Invalid runtime self-attestation must retain RC70 without leaking data."""
    from codex_usage import integration_watchdog

    secret_message = "self-attestation-secret"
    monkeypatch.setattr(
        integration_watchdog,
        "_runtime_self_attestation",
        lambda **_kwargs: (_ for _ in ()).throw(
            IntegrationEvidenceInvalid(secret_message)
        ),
    )

    assert integration_watchdog.execute_publisher_only(
        (),
        environ=_environment(tmp_path),
        runtime_entrypoint_path=tmp_path / "runtime.py",
        verifier=lambda **_kwargs: _verified_manifest(tmp_path),
        publisher_runner=lambda *_args, **_kwargs: pytest.fail("publish after invalid runtime"),
    ) == 70

    diagnostic = capsys.readouterr().err
    assert "stage=runtime_self_attestation" in diagnostic
    assert "rc=70" in diagnostic
    assert "exception=IntegrationEvidenceInvalid" in diagnostic
    assert secret_message not in diagnostic
    assert len(diagnostic.encode()) <= 512


def test_execute_publisher_only_reports_outer_failure_without_message(
    tmp_path,
    monkeypatch,
    capsys,
):
    """Unexpected wrapper failures must be fail-closed and minimally observable."""
    from codex_usage import integration_watchdog

    secret_message = "outer-wrapper-secret"
    monkeypatch.setattr(
        integration_watchdog,
        "_validated_child_environment",
        lambda _environment: (_ for _ in ()).throw(RuntimeError(secret_message)),
    )

    assert integration_watchdog.execute_publisher_only(
        (),
        environ=_environment(tmp_path),
        runtime_entrypoint_path=tmp_path / "runtime.py",
        verifier=lambda **_kwargs: pytest.fail("attestation after outer failure"),
        publisher_runner=lambda *_args, **_kwargs: pytest.fail("publish after outer failure"),
    ) == 69

    diagnostic = capsys.readouterr().err
    assert "stage=outer_failure" in diagnostic
    assert "rc=69" in diagnostic
    assert "exception=RuntimeError" in diagnostic
    assert secret_message not in diagnostic
    assert len(diagnostic.encode()) <= 512


def test_execute_publisher_only_redacts_custom_exception_class_name(
    tmp_path,
    monkeypatch,
    capsys,
):
    """A custom exception class name is untrusted diagnostic data, not metadata."""
    from codex_usage import integration_watchdog

    secret_class_name = "custom_diagnostic_class_secret"
    custom_error = type(secret_class_name, (Exception,), {})
    monkeypatch.setattr(
        integration_watchdog,
        "_validated_child_environment",
        lambda _environment: (_ for _ in ()).throw(custom_error()),
    )

    assert integration_watchdog.execute_publisher_only(
        (),
        environ=_environment(tmp_path),
        runtime_entrypoint_path=tmp_path / "runtime.py",
        verifier=lambda **_kwargs: pytest.fail("attestation after outer failure"),
        publisher_runner=lambda *_args, **_kwargs: pytest.fail("publish after outer failure"),
    ) == 69

    diagnostic = capsys.readouterr().err
    assert "stage=outer_failure" in diagnostic
    assert "rc=69" in diagnostic
    assert "exception=unrecognized" in diagnostic
    assert secret_class_name not in diagnostic
    assert len(diagnostic.encode()) <= 512


def test_execute_reserves_cleanup_budget_from_publisher_timeout(
    tmp_path,
    monkeypatch,
):
    """Would fail if publisher timeout consumed the process-group cleanup window."""
    from codex_usage import integration_watchdog

    environment = _environment(tmp_path)
    verified = _verified_manifest(tmp_path)
    publisher_timeouts: list[float] = []
    monotonic_values = iter((100.0, 100.0, 100.0, 100.0, 150.0))

    monkeypatch.setattr(
        integration_watchdog,
        "_call_with_timeout",
        lambda _label, _timeout_seconds, callback: callback(),
    )
    monkeypatch.setattr(
        integration_watchdog,
        "_runtime_self_attestation",
        lambda **_kwargs: None,
    )

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=environment,
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=lambda _path, **_kwargs: 0,
        verifier=lambda **_kwargs: verified,
        publisher_runner=lambda _launcher, _argv, timeout, **_kwargs: publisher_timeouts.append(
            timeout
        )
        or 0,
        monotonic=lambda: next(monotonic_values),
    ) == 0
    assert publisher_timeouts == [180.0]

    nearly_exhausted = iter((100.0, 100.0, 100.0, 100.0, 351.0))
    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=environment,
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=lambda _path, **_kwargs: 0,
        verifier=lambda **_kwargs: verified,
        publisher_runner=lambda *_args, **_kwargs: pytest.fail(
            "publisher must not start without cleanup reserve"
        ),
        monotonic=lambda: next(nearly_exhausted),
    ) == 75


def test_execute_returns_temporary_failure_before_systemd_kill_on_slow_generic(
    tmp_path,
    monkeypatch,
):
    """Would fail if generic watchdog timeout was not enforced internally."""
    from codex_usage import integration_watchdog

    def fake_timeout(label: str, _timeout_seconds: float, _callback):
        assert label == "generic watchdog"
        raise TimeoutError(label)

    monkeypatch.setattr(integration_watchdog, "_call_with_timeout", fake_timeout)

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=_environment(tmp_path),
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=lambda _path, **_kwargs: pytest.fail("watchdog should be wrapped"),
        verifier=lambda **_kwargs: pytest.fail("attestation after generic timeout"),
        publisher_runner=lambda *_args, **_kwargs: pytest.fail(
            "publish after generic timeout"
        ),
    ) == 75


def test_execute_rejects_generic_watchdog_status_returned_after_sigalrm_timeout(
    tmp_path,
    monkeypatch,
):
    """Would fail if a swallowed generic-stage SIGALRM could be accepted as RC2."""
    from codex_usage import integration_watchdog

    monkeypatch.setattr(
        integration_watchdog,
        "GENERIC_WATCHDOG_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        integration_watchdog,
        "TOTAL_RUNTIME_BUDGET_SECONDS",
        1,
    )

    def watchdog_runner(_path: Path, **_kwargs) -> int:
        try:
            time.sleep(10)
        except TimeoutError:
            return 2
        return 0

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=_environment(tmp_path),
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=watchdog_runner,
        verifier=lambda **_kwargs: pytest.fail("attestation after timed out generic"),
        publisher_runner=lambda *_args, **_kwargs: pytest.fail(
            "publish after timed out generic"
        ),
    ) == 75


def test_execute_returns_temporary_failure_before_systemd_kill_on_slow_attestation(
    tmp_path,
    monkeypatch,
):
    """Would fail if attestation timeout was not enforced internally."""
    from codex_usage import integration_watchdog

    labels: list[str] = []

    def fake_timeout(label: str, _timeout_seconds: float, callback):
        labels.append(label)
        if label == "private service runtime attestation":
            raise TimeoutError(label)
        return callback()

    monkeypatch.setattr(integration_watchdog, "_call_with_timeout", fake_timeout)

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=_environment(tmp_path),
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=lambda _path, **_kwargs: 0,
        verifier=lambda **_kwargs: pytest.fail("attestation should be wrapped"),
        publisher_runner=lambda *_args, **_kwargs: pytest.fail(
            "publish after attestation timeout"
        ),
    ) == 75
    assert labels == ["generic watchdog", "private service runtime attestation"]


def test_execute_maps_attestation_error_after_sigalrm_timeout_to_rc75(
    tmp_path,
    monkeypatch,
):
    """Would fail if an attestation-stage timeout was downgraded to unavailable."""
    from codex_usage import integration_watchdog

    monkeypatch.setattr(
        integration_watchdog,
        "ATTESTATION_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        integration_watchdog,
        "TOTAL_RUNTIME_BUDGET_SECONDS",
        1,
    )

    def verifier(**_kwargs):
        try:
            time.sleep(10)
        except TimeoutError as exc:
            raise IntegrationEvidenceUnavailable() from exc
        pytest.fail("attestation sleep unexpectedly returned")

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=_environment(tmp_path),
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=lambda _path, **_kwargs: 0,
        verifier=verifier,
        publisher_runner=lambda *_args, **_kwargs: pytest.fail(
            "publish after attestation timeout"
        ),
    ) == 75


def test_call_with_timeout_interrupts_slow_callable_before_outer_unit_budget():
    """Would fail if stage timeout was only documented but not enforced."""
    from codex_usage import integration_watchdog

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        integration_watchdog._call_with_timeout(
            "synthetic slow stage",
            0.05,
            lambda: time.sleep(10),
        )

    assert time.monotonic() - start < 1


def test_call_with_timeout_preserves_timeout_exceptiongroup_cleanup_evidence():
    """Would fail if primary timeout plus cleanup failure became bare TimeoutError."""
    from codex_usage import integration_watchdog

    def callback():
        try:
            time.sleep(10)
        except BaseException as primary:
            raise ExceptionGroup(
                "primary and cleanup failed",
                [primary, RuntimeError("synthetic cleanup failure")],
            ) from primary

    with pytest.raises(ExceptionGroup) as exc_info:
        integration_watchdog._call_with_timeout(
            "synthetic grouped timeout",
            0.05,
            callback,
        )

    flattened = _flatten_exception_group(exc_info.value)
    assert any(
        isinstance(error, integration_watchdog._IntegrationWatchdogStageTimeout)
        for error in flattened
    )
    assert any(
        isinstance(error, RuntimeError)
        and "synthetic cleanup failure" in str(error)
        for error in flattened
    )


def _environment(tmp_path: Path) -> dict[str, str]:
    data_home = tmp_path / "data"
    state_home = tmp_path / "state"
    data_home.mkdir(mode=0o700)
    (state_home / "codex-usage" / "integration").mkdir(
        mode=0o700,
        parents=True,
        exist_ok=True,
    )
    data_home.chmod(0o700)
    state_home.chmod(0o700)
    (state_home / "codex-usage").chmod(0o700)
    (state_home / "codex-usage" / "integration").chmod(0o700)
    return {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
    }


def _expected_child_environment(environment: dict[str, str]) -> dict[str, str]:
    return {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "XDG_DATA_HOME": environment["XDG_DATA_HOME"],
        "XDG_STATE_HOME": environment["XDG_STATE_HOME"],
    }


def _runtime_environment(data_home: Path, state_home: Path) -> dict[str, str]:
    return {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
    }


def _static_child_environment() -> dict[str, str]:
    return {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "XDG_DATA_HOME": "/tmp/codex-usage-watchdog-test-data",
        "XDG_STATE_HOME": "/tmp/codex-usage-watchdog-test-state",
    }


def _poison_ambient_environment(monkeypatch) -> None:
    for name, value in {
        "PYTHONPATH": "/tmp/shadow-pythonpath",
        "PYTHONHOME": "/tmp/shadow-pythonhome",
        "PYTHONUSERBASE": "/tmp/shadow-userbase",
        "LD_PRELOAD": "/tmp/shadow-preload.so",
        "LD_AUDIT": "/tmp/shadow-audit.so",
        "LD_LIBRARY_PATH": "/tmp/shadow-lib",
        "DYLD_INSERT_LIBRARIES": "/tmp/shadow.dylib",
        "DYLD_LIBRARY_PATH": "/tmp/shadow-dyld",
        "CODEX_USAGE_AMBIENT_SENTINEL": "must-not-reach-child",
    }.items():
        monkeypatch.setenv(name, value)


def _verified_manifest(tmp_path: Path) -> VerifiedActiveManifest:
    release_dir = tmp_path / "state/codex-usage/integration/releases/active"
    entrypoint = (
        release_dir
        / "venv/lib/python3.14/site-packages/codex_usage/integration_entrypoint.py"
    )
    launcher = release_dir / "venv/bin/codex-usage"
    manifest_bytes = b"{}\n"
    return VerifiedActiveManifest(
        active_release=ActiveRelease(
            version="0.6.539",
            release_dir=release_dir,
            launcher_path=launcher,
            entrypoint_path=entrypoint,
            entrypoint_sha256="1" * 64,
            wheel_sha256="2" * 64,
            record_sha256="3" * 64,
            launcher_sha256="4" * 64,
            release_tree_sha256="5" * 64,
        ),
        release_id="0.6.539-" + "6" * 16,
        source_manifest_sha256="6" * 64,
        active_manifest_bytes=manifest_bytes,
        active_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        state_home_identity=FileIdentity(1, 2, 0o700),
        integration_parent_identity=FileIdentity(1, 3, 0o700),
        active_file_identity=FileIdentity(1, 4, 0o600),
    )


_RUNTIME_SELF_ATTESTED_MODULES = (
    "codex_usage",
    "codex_usage.integration_attestation",
    "codex_usage.integration_entrypoint",
    "codex_usage.integration_timeout_contract",
    "codex_usage.integration_watchdog",
    "codex_usage.json_utils",
    "codex_usage.private_io",
)


def _runtime_module_relative(module_name: str) -> str:
    if module_name == "codex_usage":
        return "codex_usage/__init__.py"
    return f"codex_usage/{module_name.removeprefix('codex_usage.')}.py"


def _record_digest(payload: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
    return "sha256=" + encoded.decode("ascii").rstrip("=")


def _write_private_service_runtime(
    data_home: Path,
    release_entrypoint: Path,
) -> tuple[Path, Path]:
    """Create the intended private service layout from an attested test release."""
    from codex_usage.integration_attestation import TRUSTED_CORE_MODULES

    abi = f"python{sys.version_info.major}.{sys.version_info.minor}"
    current = data_home / "codex-usage-service-runtime-v2" / "current"
    venv = current / "venv"
    site_packages = venv / "lib" / abi / "site-packages"
    package = site_packages / "codex_usage"
    dist_info = site_packages / "codex_usage-0.6.539.dist-info"
    for directory in (
        data_home / "codex-usage-service-runtime-v2",
        current,
        venv,
        venv / "bin",
        venv / "lib",
        venv / "lib" / abi,
        site_packages,
        package,
        dist_info,
    ):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)

    release_package = release_entrypoint.parent
    project_package = Path(__file__).resolve().parents[1] / "src" / "codex_usage"
    for module_name in TRUSTED_CORE_MODULES:
        source = release_package / module_name
        if not source.is_file():
            source = project_package / module_name
        target = package / module_name
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)

    metadata = dist_info / "METADATA"
    metadata.write_bytes(
        b"Metadata-Version: 2.4\nName: codex-usage\nVersion: 0.6.539\n"
    )
    metadata.chmod(0o600)
    record = dist_info / "RECORD"
    rows = [
        (
            f"codex_usage/{module_name},"
            f"{_record_digest((package / module_name).read_bytes())},"
            f"{(package / module_name).stat().st_size}"
        )
        for module_name in TRUSTED_CORE_MODULES
    ]
    rows.extend(
        (
            (
                "codex_usage-0.6.539.dist-info/METADATA,"
                f"{_record_digest(metadata.read_bytes())},{metadata.stat().st_size}"
            ),
            "codex_usage-0.6.539.dist-info/RECORD,,",
        )
    )
    _write_record(record, rows)
    record.chmod(0o600)

    pyvenv = venv / "pyvenv.cfg"
    pyvenv.write_bytes(
        b"home = /private-service-python\n"
        b"include-system-site-packages = false\n"
        b"version = 3.14.0\n"
    )
    pyvenv.chmod(0o600)
    interpreter = venv / "bin" / "python"
    interpreter.write_bytes(b"#!/bin/sh\nexit 127\n")
    interpreter.chmod(0o700)
    return package / "integration_entrypoint.py", interpreter


def _patch_private_service_runtime_origins(
    monkeypatch,
    runtime_entrypoint: Path,
) -> None:
    from codex_usage import integration_watchdog

    site_packages = runtime_entrypoint.parent.parent
    for module_name in integration_watchdog.RUNTIME_SELF_ATTESTED_CORE_MODULES:
        module = sys.modules[module_name]
        path = site_packages / _runtime_module_relative(module_name)
        monkeypatch.setattr(module, "__file__", str(path), raising=False)
        assert module.__spec__ is not None
        monkeypatch.setattr(module.__spec__, "origin", str(path), raising=False)


def _rewrite_private_runtime_record_binding(
    record_path: Path,
    relative: str,
    payload: bytes,
) -> None:
    replacement = f"{relative},{_record_digest(payload)},{len(payload)}"
    rows = record_path.read_text(encoding="utf-8").splitlines()
    rewritten = [
        replacement if row.startswith(f"{relative},") else row for row in rows
    ]
    assert rewritten != rows
    record_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    record_path.chmod(0o600)


def test_private_service_runtime_attestation_accepts_attested_private_layout(
    evidence_layout,
    monkeypatch,
):
    """Would fail if the watchdog had no strict private service-runtime contract."""
    from codex_usage import integration_watchdog

    state_home, data_home, release_entrypoint, _payload, expected = evidence_layout
    runtime_entrypoint, interpreter = _write_private_service_runtime(
        data_home,
        release_entrypoint,
    )
    monkeypatch.setattr(integration_watchdog.sys, "executable", str(interpreter))

    verified = integration_watchdog.verify_active_manifest_against_private_service_runtime(
        state_home,
        data_home,
        runtime_entrypoint,
        interpreter,
    )
    assert verified == expected
    _patch_private_service_runtime_origins(monkeypatch, runtime_entrypoint)
    integration_watchdog.verify_private_service_runtime_self_attestation(
        runtime_entrypoint,
        interpreter,
        verified,
    )


@pytest.mark.parametrize(
    "defect",
    (
        "wrong-root",
        "wrong-interpreter",
        "producer-venv",
        "wrong-abi",
        "system-site-packages",
        "metadata-version-drift",
        "symlinked-core-module",
        "hardlinked-core-module",
        "nonprivate-core-module",
        "record-drift",
        "producer-byte-drift",
    ),
)
def test_private_service_runtime_attestation_rejects_unsafe_layout_or_drift(
    evidence_layout,
    tmp_path,
    monkeypatch,
    defect,
):
    """Would fail if the private service contract accepted a principal unsafe state."""
    from codex_usage import integration_watchdog
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, release_entrypoint, _payload, _expected = evidence_layout
    runtime_entrypoint, interpreter = _write_private_service_runtime(
        data_home,
        release_entrypoint,
    )
    monkeypatch.setattr(integration_watchdog.sys, "executable", str(interpreter))
    package = runtime_entrypoint.parent
    dist_info = runtime_entrypoint.parent.parent / "codex_usage-0.6.539.dist-info"
    candidate_entrypoint = runtime_entrypoint
    candidate_interpreter = interpreter

    if defect == "wrong-root":
        candidate_entrypoint = tmp_path / "other/current/entrypoint.py"
    elif defect == "wrong-interpreter":
        candidate_interpreter = tmp_path / "user-site/bin/python"
        candidate_interpreter.parent.mkdir(mode=0o700, parents=True)
        candidate_interpreter.write_bytes(b"#!/bin/sh\nexit 127\n")
        candidate_interpreter.chmod(0o700)
    elif defect == "producer-venv":
        candidate_entrypoint = release_entrypoint
        candidate_interpreter = release_entrypoint.parents[4] / "bin" / "python"
    elif defect == "wrong-abi":
        runtime_entrypoint.parents[2].rename(
            runtime_entrypoint.parents[2].with_name("python3.99")
        )
    elif defect == "system-site-packages":
        pyvenv = interpreter.parents[1] / "pyvenv.cfg"
        pyvenv.write_bytes(b"include-system-site-packages = true\n")
        pyvenv.chmod(0o600)
    elif defect == "metadata-version-drift":
        metadata = dist_info / "METADATA"
        metadata.write_bytes(
            b"Metadata-Version: 2.4\nName: codex-usage\nVersion: 0.6.536\n"
        )
        metadata.chmod(0o600)
        _rewrite_private_runtime_record_binding(
            dist_info / "RECORD",
            "codex_usage-0.6.539.dist-info/METADATA",
            metadata.read_bytes(),
        )
    elif defect == "symlinked-core-module":
        runtime_entrypoint.unlink()
        runtime_entrypoint.symlink_to(package / "integration_snapshot.py")
    elif defect == "hardlinked-core-module":
        os.link(package / "integration_watchdog.py", package / "watchdog-hardlink.py")
    elif defect == "nonprivate-core-module":
        package.joinpath("integration_watchdog.py").chmod(0o644)
    elif defect == "record-drift":
        module = package / "integration_watchdog.py"
        module.write_bytes(module.read_bytes() + b"\n# unrecorded drift\n")
        module.chmod(0o600)
    elif defect == "producer-byte-drift":
        module = package / "integration_snapshot.py"
        module.write_bytes(module.read_bytes() + b"\n# forged private producer\n")
        module.chmod(0o600)
        _rewrite_private_runtime_record_binding(
            dist_info / "RECORD",
            "codex_usage/integration_snapshot.py",
            module.read_bytes(),
        )
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(defect)

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_watchdog.verify_active_manifest_against_private_service_runtime(
            state_home,
            data_home,
            candidate_entrypoint,
            candidate_interpreter,
        )


def test_execute_uses_private_runtime_verifiers_for_configured_publisher_gate(
    evidence_layout,
    tmp_path,
    monkeypatch,
):
    """Would fail if the real --config gate bypassed either private verifier."""
    from codex_usage import integration_watchdog

    state_home, data_home, release_entrypoint, _payload, expected = evidence_layout
    runtime_entrypoint, interpreter = _write_private_service_runtime(
        data_home,
        release_entrypoint,
    )
    monkeypatch.setattr(integration_watchdog.sys, "executable", str(interpreter))
    _patch_private_service_runtime_origins(monkeypatch, runtime_entrypoint)
    published: list[tuple[Path, tuple[str, ...], float]] = []

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=_runtime_environment(data_home, state_home),
        runtime_entrypoint_path=runtime_entrypoint,
        watchdog_runner=lambda _path, **_kwargs: 2,
        verifier=integration_watchdog.verify_active_manifest_against_private_service_runtime,
        publisher_runner=lambda launcher, argv, timeout, **_kwargs: published.append(
            (launcher, argv, timeout)
        )
        or 0,
        interpreter_path=interpreter,
    ) == 0
    assert len(published) == 1
    assert published[0][0] == expected.active_release.launcher_path
    assert published[0][1] == integration_watchdog.PUBLISH_ARGV


def _write_record(record_path: Path, rows: list[str]) -> None:
    record_path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_trusted_runtime_core_layout(
    tmp_path: Path,
) -> tuple[Path, Path, VerifiedActiveManifest]:
    from codex_usage.integration_attestation import (
        PRODUCER_RELEASE_MODULES,
        TRUSTED_PRODUCER_CORE_MODULES,
    )

    source_package = Path("src/codex_usage")
    trusted_site = tmp_path / "trusted-core/site-packages"
    trusted_interpreter = tmp_path / "trusted-core/bin/python"
    trusted_interpreter.parent.mkdir(mode=0o700, parents=True)
    trusted_interpreter.write_bytes(b"#!/bin/sh\nexit 127\n")
    trusted_interpreter.chmod(0o700)
    trusted_package = trusted_site / "codex_usage"
    trusted_package.mkdir(mode=0o700, parents=True)
    trusted_dist = trusted_site / "codex_usage-0.6.539.dist-info"
    trusted_dist.mkdir(mode=0o700)
    for module_name in TRUSTED_PRODUCER_CORE_MODULES:
        target = trusted_package / module_name
        shutil.copyfile(source_package / module_name, target)
        target.chmod(0o644)
    trusted_metadata = trusted_dist / "METADATA"
    trusted_metadata.write_text(
        "Metadata-Version: 2.4\nName: codex-usage\nVersion: 0.6.539\n",
        encoding="utf-8",
    )
    trusted_metadata.chmod(0o644)
    trusted_record = trusted_dist / "RECORD"
    trusted_rows = [
        (
            f"codex_usage/{module_name},"
            f"{_record_digest((trusted_package / module_name).read_bytes())},"
            f"{(trusted_package / module_name).stat().st_size}"
        )
        for module_name in TRUSTED_PRODUCER_CORE_MODULES
    ]
    trusted_rows.extend(
        [
            (
                f"codex_usage-0.6.539.dist-info/METADATA,"
                f"{_record_digest(trusted_metadata.read_bytes())},"
                f"{trusted_metadata.stat().st_size}"
            ),
            "codex_usage-0.6.539.dist-info/RECORD,,",
        ]
    )
    _write_record(trusted_record, trusted_rows)
    trusted_record.chmod(0o644)

    release_dir = tmp_path / "state/codex-usage/integration/releases/0.6.539-6666666666666666"
    active_site = release_dir / "venv/lib/python3.14/site-packages"
    active_package = active_site / "codex_usage"
    active_package.mkdir(mode=0o700, parents=True)
    active_dist = active_site / "codex_usage_integration_producer-0.6.539.dist-info"
    active_dist.mkdir(mode=0o700)
    for module_name in PRODUCER_RELEASE_MODULES:
        target = active_package / module_name
        shutil.copyfile(trusted_package / module_name, target)
        target.chmod(0o600)
    for name, payload in (
        (
            "METADATA",
            b"Metadata-Version: 2.4\n"
            b"Name: codex-usage-integration-producer\n"
            b"Version: 0.6.539\n",
        ),
        ("WHEEL", b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"),
        ("top_level.txt", b"codex_usage\n"),
    ):
        path = active_dist / name
        path.write_bytes(payload)
        path.chmod(0o600)
    active_record = active_dist / "RECORD"
    active_rows = [
        (
            f"codex_usage/{module_name},"
            f"{_record_digest((active_package / module_name).read_bytes())},"
            f"{(active_package / module_name).stat().st_size}"
        )
        for module_name in PRODUCER_RELEASE_MODULES
    ]
    active_rows.extend(
        [
            (
                f"codex_usage_integration_producer-0.6.539.dist-info/{name},"
                f"{_record_digest((active_dist / name).read_bytes())},"
                f"{(active_dist / name).stat().st_size}"
            )
            for name in ("METADATA", "WHEEL", "top_level.txt")
        ]
    )
    active_rows.append("codex_usage_integration_producer-0.6.539.dist-info/RECORD,,")
    _write_record(active_record, active_rows)
    active_record.chmod(0o600)
    launcher = release_dir / "venv/bin/codex-usage"
    launcher.parent.mkdir(mode=0o700, parents=True)
    launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
    launcher.chmod(0o700)
    manifest_bytes = b"{}\n"
    verified = VerifiedActiveManifest(
        active_release=ActiveRelease(
            version="0.6.539",
            release_dir=release_dir,
            launcher_path=launcher,
            entrypoint_path=active_package / "integration_entrypoint.py",
            entrypoint_sha256=hashlib.sha256(
                active_package.joinpath("integration_entrypoint.py").read_bytes()
            ).hexdigest(),
            wheel_sha256="2" * 64,
            record_sha256=hashlib.sha256(active_record.read_bytes()).hexdigest(),
            launcher_sha256=hashlib.sha256(launcher.read_bytes()).hexdigest(),
            release_tree_sha256="5" * 64,
        ),
        release_id="0.6.539-" + "6" * 16,
        source_manifest_sha256="6" * 64,
        active_manifest_bytes=manifest_bytes,
        active_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        state_home_identity=FileIdentity(1, 2, 0o700),
        integration_parent_identity=FileIdentity(1, 3, 0o700),
        active_file_identity=FileIdentity(1, 4, 0o600),
    )
    return trusted_site, trusted_package / "integration_entrypoint.py", verified


def _patch_runtime_module_origins(monkeypatch, site_packages: Path) -> None:
    trusted_interpreter = site_packages.parent / "bin/python"
    monkeypatch.setattr(sys, "executable", str(trusted_interpreter))
    for module_name in _RUNTIME_SELF_ATTESTED_MODULES:
        module = sys.modules[module_name]
        path = site_packages / _runtime_module_relative(module_name)
        monkeypatch.setattr(module, "__file__", str(path), raising=False)
        if module.__spec__ is not None:
            monkeypatch.setattr(module.__spec__, "origin", str(path), raising=False)


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("LD_PRELOAD", "/tmp/shadow.so"),
        ("LD_AUDIT", "/tmp/audit.so"),
        ("LD_LIBRARY_PATH", "/tmp/lib"),
        ("DYLD_INSERT_LIBRARIES", "/tmp/shadow.dylib"),
        ("PYTHONPATH", "/tmp/shadow"),
        ("PYTHONHOME", "/tmp/pythonhome"),
        ("PYTHONUSERBASE", "/tmp/userbase"),
        ("PYTHONEXECUTABLE", "/tmp/python"),
        ("PYTHONSAFEPATH", "0"),
        ("PYTHONNOUSERSITE", "0"),
        ("PYTHONDONTWRITEBYTECODE", "0"),
    ),
)
def test_execute_rejects_shadowing_runtime_environment_before_watchdog(
    tmp_path,
    name,
    value,
):
    """Would fail if unit env hardening were not enforced by the runtime wrapper."""
    from codex_usage import integration_watchdog

    environment = _environment(tmp_path)
    environment[name] = value

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=environment,
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=lambda _path, **_kwargs: pytest.fail(
            "watchdog after unsafe runtime env"
        ),
        verifier=lambda **_kwargs: pytest.fail("attestation after unsafe runtime env"),
        publisher_runner=lambda *_args, **_kwargs: pytest.fail(
            "publisher after unsafe runtime env"
        ),
    ) == 70


def test_execute_passes_sanitized_environment_to_generic_watchdog_child(
    tmp_path,
    monkeypatch,
):
    """Would fail if the generic watchdog child inherited ambient loader state."""
    from codex_usage import integration_watchdog

    _poison_ambient_environment(monkeypatch)
    environment = _environment(tmp_path)
    verified = _verified_manifest(tmp_path)
    calls: list[dict[str, object]] = []

    class Process:
        pid = 12345
        stdout = None
        stderr = None

        def poll(self):
            return 2

        def wait(self, *, timeout=None):
            return 2

    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda _command, **kwargs: calls.append(kwargs) or Process(),
    )
    monkeypatch.setattr(integration_watchdog, "_terminate_process_group", lambda _process: None)
    monkeypatch.setattr(
        integration_watchdog,
        "_runtime_self_attestation",
        lambda **_kwargs: None,
    )

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=environment,
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=integration_watchdog._run_watchdog_stage,
        verifier=lambda **_kwargs: verified,
        publisher_runner=lambda *_args, **_kwargs: 0,
    ) == 0
    assert calls[0]["env"] == _expected_child_environment(environment)
    assert "CODEX_USAGE_AMBIENT_SENTINEL" not in calls[0]["env"]
    assert "LD_PRELOAD" not in calls[0]["env"]
    assert "PYTHONPATH" not in calls[0]["env"]


def test_execute_passes_sanitized_environment_to_publisher_child(
    tmp_path,
    monkeypatch,
):
    """Would fail if the attested publisher inherited ambient loader state."""
    from codex_usage import integration_watchdog

    _poison_ambient_environment(monkeypatch)
    environment = _environment(tmp_path)
    verified = _verified_manifest(tmp_path)
    calls: list[dict[str, object]] = []

    class Process:
        pid = 12345
        stdout = None
        stderr = None

        def poll(self):
            return 0

        def wait(self, *, timeout=None):
            return 0

    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda _command, **kwargs: calls.append(kwargs) or Process(),
    )
    monkeypatch.setattr(integration_watchdog, "_terminate_process_group", lambda _process: None)
    monkeypatch.setattr(
        integration_watchdog,
        "_runtime_self_attestation",
        lambda **_kwargs: None,
    )

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=environment,
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=lambda _path, **_kwargs: 2,
        verifier=lambda **_kwargs: verified,
        publisher_runner=integration_watchdog._run_publisher_stage,
    ) == 0
    assert calls[0]["env"] == _expected_child_environment(environment)
    assert "CODEX_USAGE_AMBIENT_SENTINEL" not in calls[0]["env"]
    assert "LD_AUDIT" not in calls[0]["env"]
    assert "PYTHONUSERBASE" not in calls[0]["env"]


def test_execute_freezes_child_environment_before_ambient_and_mapping_race(
    tmp_path,
    monkeypatch,
):
    """Would fail if later os.environ or caller mapping changes reached children."""
    from codex_usage import integration_watchdog

    _poison_ambient_environment(monkeypatch)
    environment = _environment(tmp_path)
    expected = _expected_child_environment(environment)
    verified = _verified_manifest(tmp_path)
    statuses = iter((2, 0))
    child_envs: list[object] = []

    class Process:
        pid = 12345
        stdout = None
        stderr = None

        def __init__(self, status: int):
            self._status = status

        def poll(self):
            return self._status

        def wait(self, *, timeout=None):
            return self._status

    def fake_popen(_command, **kwargs):
        child_envs.append(kwargs.get("env"))
        return Process(next(statuses))

    def verifier(**_kwargs):
        monkeypatch.setenv("LD_AUDIT", "/tmp/late-audit.so")
        environment["PYTHONPATH"] = "/tmp/late-pythonpath"
        environment["CODEX_USAGE_AMBIENT_SENTINEL"] = "late-sentinel"
        return verified

    monkeypatch.setattr(integration_watchdog.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(integration_watchdog, "_terminate_process_group", lambda _process: None)
    monkeypatch.setattr(
        integration_watchdog,
        "_runtime_self_attestation",
        lambda **_kwargs: None,
    )

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=environment,
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=integration_watchdog._run_watchdog_stage,
        verifier=verifier,
        publisher_runner=integration_watchdog._run_publisher_stage,
    ) == 0
    assert child_envs == [expected, expected]


def test_execute_rejects_missing_required_child_environment_before_subprocess(
    tmp_path,
):
    """Would fail if child hardening variables were defaulted or read from ambient env."""
    from codex_usage import integration_watchdog

    environment = _environment(tmp_path)
    del environment["PYTHONSAFEPATH"]

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=environment,
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=lambda _path, **_kwargs: pytest.fail(
            "watchdog after missing child env"
        ),
        verifier=lambda **_kwargs: pytest.fail("verify after missing child env"),
        publisher_runner=lambda *_args, **_kwargs: pytest.fail(
            "publisher after missing child env"
        ),
    ) == 70


def test_execute_runs_runtime_self_attestation_after_release_attestation_before_publish(
    tmp_path,
    monkeypatch,
):
    """Would fail if publisher could start without a final runtime self-attestation."""
    from codex_usage import integration_watchdog

    environment = _environment(tmp_path)
    trusted_entrypoint = tmp_path / "trusted.py"
    verified = _verified_manifest(tmp_path)
    events: list[tuple[str, object]] = []

    def runtime_self_attestation(**kwargs) -> None:
        events.append(("runtime-self-attest", kwargs))
        raise IntegrationEvidenceInvalid()

    monkeypatch.setattr(
        integration_watchdog,
        "_runtime_self_attestation",
        runtime_self_attestation,
        raising=False,
    )

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=environment,
        runtime_entrypoint_path=trusted_entrypoint,
        watchdog_runner=lambda _path, **_kwargs: events.append(("watchdog", _path)) or 2,
        verifier=lambda **kwargs: events.append(("verify-release", kwargs)) or verified,
        publisher_runner=lambda *_args, **_kwargs: pytest.fail(
            "publisher must not start after runtime self-attestation drift"
        ),
    ) == 70
    assert [event[0] for event in events] == [
        "watchdog",
        "verify-release",
        "runtime-self-attest",
    ]


def test_execute_rejects_private_service_runtime_shadow_before_publisher(
    evidence_layout,
    tmp_path,
    monkeypatch,
):
    """Would fail if an imported Watchdog origin escaped the private runtime."""
    from codex_usage import integration_watchdog

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    runtime_entrypoint, interpreter = _write_private_service_runtime(
        data_home,
        release_entrypoint,
    )
    monkeypatch.setattr(integration_watchdog.sys, "executable", str(interpreter))
    _patch_private_service_runtime_origins(monkeypatch, runtime_entrypoint)
    shadow_watchdog = tmp_path / "shadow-watchdog.py"
    shadow_watchdog.write_bytes(b"# shadow\n")
    shadow_watchdog.chmod(0o600)
    monkeypatch.setattr(integration_watchdog, "__file__", str(shadow_watchdog))
    assert integration_watchdog.__spec__ is not None
    monkeypatch.setattr(integration_watchdog.__spec__, "origin", str(shadow_watchdog))

    published: list[object] = []
    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=_runtime_environment(data_home, state_home),
        runtime_entrypoint_path=runtime_entrypoint,
        watchdog_runner=lambda _path, **_kwargs: 2,
        verifier=integration_watchdog.verify_active_manifest_against_private_service_runtime,
        publisher_runner=lambda *_args, **_kwargs: published.append(_args) or 0,
        interpreter_path=interpreter,
    ) == 70
    assert published == []


def test_execute_rejects_nonprivate_service_dist_info_before_publisher(
    evidence_layout,
    tmp_path,
    monkeypatch,
):
    """Would fail if private runtime distribution metadata permissions were ignored."""
    from codex_usage import integration_watchdog

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    runtime_entrypoint, interpreter = _write_private_service_runtime(
        data_home,
        release_entrypoint,
    )
    monkeypatch.setattr(integration_watchdog.sys, "executable", str(interpreter))
    _patch_private_service_runtime_origins(monkeypatch, runtime_entrypoint)
    runtime_entrypoint.parent.parent.joinpath("codex_usage-0.6.539.dist-info").chmod(
        0o777
    )

    published: list[object] = []
    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=_runtime_environment(data_home, state_home),
        runtime_entrypoint_path=runtime_entrypoint,
        watchdog_runner=lambda _path, **_kwargs: 2,
        verifier=integration_watchdog.verify_active_manifest_against_private_service_runtime,
        publisher_runner=lambda *_args, **_kwargs: published.append(_args) or 0,
        interpreter_path=interpreter,
    ) == 69
    assert published == []


def test_execute_rejects_runtime_core_rebind_immediately_before_publisher(
    evidence_layout,
    tmp_path,
    monkeypatch,
):
    """Would fail if the final runtime recheck were not adjacent to publisher launch."""
    from codex_usage import integration_watchdog

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    runtime_entrypoint, interpreter = _write_private_service_runtime(
        data_home,
        release_entrypoint,
    )
    monkeypatch.setattr(integration_watchdog.sys, "executable", str(interpreter))
    _patch_private_service_runtime_origins(monkeypatch, runtime_entrypoint)

    def verifier(**kwargs) -> VerifiedActiveManifest:
        verified = integration_watchdog.verify_active_manifest_against_private_service_runtime(
            **kwargs
        )
        runtime_entrypoint.parent.joinpath("integration_watchdog.py").chmod(0o644)
        return verified

    published: list[object] = []
    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=_runtime_environment(data_home, state_home),
        runtime_entrypoint_path=runtime_entrypoint,
        watchdog_runner=lambda _path, **_kwargs: 2,
        verifier=verifier,
        publisher_runner=lambda *_args, **_kwargs: published.append(_args) or 0,
        interpreter_path=interpreter,
    ) == 70
    assert published == []


def test_execute_rejects_record_consistent_core_runtime_rebind_before_publisher(
    evidence_layout,
    tmp_path,
    monkeypatch,
):
    """Would fail if a self-consistent Core-only rebind escaped the final check."""
    from codex_usage import integration_watchdog

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    runtime_entrypoint, interpreter = _write_private_service_runtime(
        data_home,
        release_entrypoint,
    )
    monkeypatch.setattr(integration_watchdog.sys, "executable", str(interpreter))
    _patch_private_service_runtime_origins(monkeypatch, runtime_entrypoint)
    record = (
        runtime_entrypoint.parent.parent / "codex_usage-0.6.539.dist-info" / "RECORD"
    )

    def verifier(**kwargs) -> VerifiedActiveManifest:
        verified = integration_watchdog.verify_active_manifest_against_private_service_runtime(
            **kwargs
        )
        module = runtime_entrypoint.parent / "integration_watchdog.py"
        module.write_bytes(module.read_bytes() + b"\n# self-consistent core rebind\n")
        module.chmod(0o600)
        _rewrite_private_runtime_record_binding(
            record,
            "codex_usage/integration_watchdog.py",
            module.read_bytes(),
        )
        return verified

    published: list[object] = []
    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=_runtime_environment(data_home, state_home),
        runtime_entrypoint_path=runtime_entrypoint,
        watchdog_runner=lambda _path, **_kwargs: 2,
        verifier=verifier,
        publisher_runner=lambda *_args, **_kwargs: published.append(_args) or 0,
        interpreter_path=interpreter,
    ) == 70
    assert published == []


@pytest.mark.parametrize("watchdog_status", (0, 2))
def test_execute_runs_allowed_watchdog_stage_before_attested_publisher(
    tmp_path,
    monkeypatch,
    watchdog_status,
):
    from codex_usage import integration_watchdog

    environment = _environment(tmp_path)
    config_path = tmp_path / "config/codex-usage/config.toml"
    trusted_entrypoint = tmp_path / "core/codex_usage/integration_entrypoint.py"
    verified = _verified_manifest(tmp_path)
    events: list[object] = []

    def watchdog_runner(path: Path, **_kwargs) -> int:
        events.append(("watchdog", path))
        return watchdog_status

    def verifier(**kwargs) -> VerifiedActiveManifest:
        events.append(("verify-private-runtime", kwargs))
        return verified

    def runtime_self_attestation(**kwargs) -> None:
        events.append(("runtime-self-attest", kwargs))

    def publisher_runner(
        launcher: Path,
        argv: tuple[str, ...],
        timeout: float,
        **_kwargs,
    ) -> int:
        events.append(("publish", launcher, argv, timeout))
        return 0

    monkeypatch.setattr(
        integration_watchdog,
        "_runtime_self_attestation",
        runtime_self_attestation,
    )

    assert integration_watchdog.execute(
        ("--config", str(config_path)),
        environ=environment,
        runtime_entrypoint_path=trusted_entrypoint,
        watchdog_runner=watchdog_runner,
        verifier=verifier,
        publisher_runner=publisher_runner,
    ) == 0
    assert events == [
        ("watchdog", config_path),
        (
            "verify-private-runtime",
            {
                "state_home": Path(environment["XDG_STATE_HOME"]),
                "data_home": Path(environment["XDG_DATA_HOME"]),
                "runtime_entrypoint_path": trusted_entrypoint,
                "interpreter_path": Path(sys.executable),
            },
        ),
        (
            "runtime-self-attest",
            {
                "runtime_entrypoint_path": trusted_entrypoint,
                "interpreter_path": Path(sys.executable),
                "verified": verified,
            },
        ),
        (
            "publish",
            verified.active_release.launcher_path,
            ("integration-snapshot", "--schema", "2", "--format", "json"),
            integration_watchdog.PUBLISH_TIMEOUT_SECONDS,
        ),
    ]


@pytest.mark.parametrize("watchdog_status", (1, 64, 69, 70, 75))
def test_execute_stops_before_attestation_on_hard_watchdog_failure(
    tmp_path,
    watchdog_status,
):
    from codex_usage import integration_watchdog

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=_environment(tmp_path),
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=lambda _path, **_kwargs: watchdog_status,
        verifier=lambda **_kwargs: pytest.fail("attestation after watchdog failure"),
        publisher_runner=lambda *_args, **_kwargs: pytest.fail(
            "publish after watchdog failure"
        ),
    ) == watchdog_status


def test_execute_propagates_missing_authority_publisher_failure(tmp_path, monkeypatch):
    """Absent/malformed producer authority is a bound invalid-source failure."""
    from codex_usage import integration_watchdog

    verified = _verified_manifest(tmp_path)
    monkeypatch.setattr(
        integration_watchdog,
        "_runtime_self_attestation",
        lambda **_kwargs: None,
    )
    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=_environment(tmp_path),
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=lambda _path, **_kwargs: 2,
        verifier=lambda **_kwargs: verified,
        publisher_runner=lambda *_args, **_kwargs: 65,
    ) == 65


@pytest.mark.parametrize(
    ("error", "status"),
    (
        (IntegrationEvidenceUnavailable(), 69),
        (IntegrationEvidenceInvalid(), 69),
    ),
)
def test_execute_maps_initial_private_attestation_failures_to_rc69(
    tmp_path,
    error,
    status,
):
    """Initial private attestation failures must never be misreported as rechecks."""
    from codex_usage import integration_watchdog

    def verifier(**_kwargs):
        raise error

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=_environment(tmp_path),
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=lambda _path, **_kwargs: 0,
        verifier=verifier,
        publisher_runner=lambda *_args, **_kwargs: pytest.fail(
            "publish after attestation failure"
        ),
    ) == status


@pytest.mark.parametrize(
    "environment",
    (
        {"XDG_DATA_HOME": "/tmp/data"},
        {"XDG_STATE_HOME": "/tmp/state"},
        {"XDG_DATA_HOME": "relative-data", "XDG_STATE_HOME": "/tmp/state"},
        {"XDG_DATA_HOME": "/tmp/data", "XDG_STATE_HOME": "relative-state"},
        {"XDG_DATA_HOME": "/tmp/data\x00bad", "XDG_STATE_HOME": "/tmp/state"},
    ),
)
def test_execute_rejects_malformed_xdg_roots_after_watchdog(tmp_path, environment):
    """Malformed XDG roots must fail closed before attestation or publisher launch."""
    from codex_usage import integration_watchdog

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=environment,
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=lambda _path, **_kwargs: 2,
        verifier=lambda **_kwargs: pytest.fail("verify after malformed XDG"),
        publisher_runner=lambda *_args, **_kwargs: pytest.fail(
            "publish after malformed XDG"
        ),
    ) == 70


@pytest.mark.parametrize(
    "argv",
    (
        (),
        ("--config",),
        ("--config", "relative.toml"),
        ("--config", "/tmp/config.toml", "extra"),
        ("--wrong", "/tmp/config.toml"),
    ),
)
def test_execute_rejects_nonexact_unit_arguments_before_watchdog(tmp_path, argv):
    from codex_usage import integration_watchdog

    assert integration_watchdog.execute(
        argv,
        environ=_environment(tmp_path),
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=lambda _path, **_kwargs: pytest.fail(
            "watchdog after invalid arguments"
        ),
        verifier=lambda **_kwargs: pytest.fail("verify after invalid arguments"),
        publisher_runner=lambda *_args, **_kwargs: pytest.fail(
            "publish after invalid arguments"
        ),
    ) == 64


def test_watchdog_stage_uses_exact_generic_command_and_discards_account_output(
    tmp_path,
    monkeypatch,
    capsys,
):
    from codex_usage import integration_watchdog

    calls: list[tuple[list[str], dict[str, object]]] = []
    waits: list[float] = []

    class Process:
        pid = 12345

        def poll(self):
            return 2

        def wait(self, *, timeout=None):
            waits.append(timeout)
            return 2

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return Process()

    monkeypatch.setattr(integration_watchdog.subprocess, "Popen", fake_popen)
    config_path = tmp_path / "config.toml"

    assert integration_watchdog._run_watchdog_stage(
        config_path,
        child_environ=_static_child_environment(),
    ) == 2
    assert calls[0][0] == [
        sys.executable,
        "-m",
        "codex_usage.cli",
        "--config",
        str(config_path),
        "watchdog",
        "--format",
        "json",
    ]
    assert calls[0][1]["stdin"] is integration_watchdog.subprocess.DEVNULL
    assert calls[0][1]["stdout"] is integration_watchdog.subprocess.DEVNULL
    assert calls[0][1]["stderr"] is integration_watchdog.subprocess.DEVNULL
    assert calls[0][1]["start_new_session"] is True
    assert waits == [integration_watchdog.GENERIC_WATCHDOG_TIMEOUT_SECONDS]
    assert capsys.readouterr() == ("", "")


def test_watchdog_stage_does_not_rebind_process_output_streams(
    tmp_path,
    monkeypatch,
):
    """Would fail if generic watchdog isolation mutated process stdout/stderr."""
    from codex_usage import integration_watchdog

    class Process:
        pid = 12345

        def poll(self):
            return 2

        def wait(self, *, timeout=None):
            assert timeout == integration_watchdog.GENERIC_WATCHDOG_TIMEOUT_SECONDS
            return 2

    def fake_popen(command, **kwargs):
        assert command == [
            sys.executable,
            "-m",
            "codex_usage.cli",
            "--config",
            str(tmp_path / "config.toml"),
            "watchdog",
            "--format",
            "json",
        ]
        assert kwargs["stdout"] is integration_watchdog.subprocess.DEVNULL
        assert kwargs["stderr"] is integration_watchdog.subprocess.DEVNULL
        return Process()

    def forbid_global_fd_rebind(*_args, **_kwargs):
        raise AssertionError("watchdog stage must not rebind process file descriptors")

    class ForbiddenRedirect:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("watchdog stage must not rebind Python streams")

    monkeypatch.setattr(
        integration_watchdog,
        "redirect_stdout",
        ForbiddenRedirect,
        raising=False,
    )
    monkeypatch.setattr(
        integration_watchdog,
        "redirect_stderr",
        ForbiddenRedirect,
        raising=False,
    )
    monkeypatch.setattr(integration_watchdog.subprocess, "Popen", fake_popen)

    original_dup2 = integration_watchdog.os.dup2
    integration_watchdog.os.dup2 = forbid_global_fd_rebind
    try:
        assert integration_watchdog._run_watchdog_stage(
            tmp_path / "config.toml",
            child_environ=_static_child_environment(),
        ) == 2
    finally:
        integration_watchdog.os.dup2 = original_dup2


def test_watchdog_stage_sends_generic_output_to_devnull_without_memory_buffer(
    tmp_path,
    monkeypatch,
    capfd,
):
    """Would fail if generic watchdog output was captured in an unbounded buffer."""
    from codex_usage import integration_watchdog

    calls: list[dict[str, object]] = []

    class Process:
        pid = 12345

        def poll(self):
            return 2

        def wait(self, *, timeout=None):
            assert timeout == integration_watchdog.GENERIC_WATCHDOG_TIMEOUT_SECONDS
            return 2

    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda _command, **kwargs: calls.append(kwargs) or Process(),
    )

    assert integration_watchdog._run_watchdog_stage(
        tmp_path / "config.toml",
        child_environ=_static_child_environment(),
    ) == 2
    assert calls[0]["stdout"] is integration_watchdog.subprocess.DEVNULL
    assert calls[0]["stderr"] is integration_watchdog.subprocess.DEVNULL
    assert capfd.readouterr() == ("", "")


def test_publisher_stage_invokes_only_exact_attested_launcher(monkeypatch, tmp_path):
    from codex_usage import integration_watchdog

    launcher = tmp_path / "release/venv/bin/codex-usage"
    calls: list[tuple[object, dict[str, object]]] = []
    waits: list[float] = []

    class Process:
        pid = 12345

        def poll(self):
            return 0

        def wait(self, *, timeout=None):
            waits.append(timeout)
            return 0

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return Process()

    monkeypatch.setattr(integration_watchdog.subprocess, "Popen", fake_popen)

    assert integration_watchdog._run_publisher_stage(
        launcher,
        ("integration-snapshot", "--schema", "2", "--format", "json"),
        integration_watchdog.PUBLISH_TIMEOUT_SECONDS,
        child_environ=_static_child_environment(),
    ) == 0
    assert calls[0][0] == [
        str(launcher),
        "integration-snapshot",
        "--schema",
        "2",
        "--format",
        "json",
    ]
    assert calls[0][1]["start_new_session"] is True
    assert waits == [integration_watchdog.PUBLISH_TIMEOUT_SECONDS]
    assert calls[0][1]["stdout"] is integration_watchdog.subprocess.PIPE
    assert calls[0][1]["stderr"] is integration_watchdog.subprocess.PIPE


def test_publisher_stage_maps_timeout(monkeypatch, tmp_path):
    """Publisher timeouts are temporary failures, not invalid attestation."""
    from codex_usage import integration_watchdog

    class Process:
        pid = 12345

        def wait(self, *, timeout=None):
            raise integration_watchdog.subprocess.TimeoutExpired(
                cmd=["codex-usage"],
                timeout=timeout,
            )

    process = Process()
    terminated: list[object] = []
    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        integration_watchdog,
        "_terminate_process_group",
        terminated.append,
    )

    assert integration_watchdog._run_publisher_stage(
        tmp_path / "release/venv/bin/codex-usage",
        ("integration-snapshot", "--schema", "2", "--format", "json"),
        integration_watchdog.PUBLISH_TIMEOUT_SECONDS,
        child_environ=_static_child_environment(),
    ) == 75
    assert terminated == [process]


def test_publisher_stage_timeout_reports_cleanup_failure_when_group_survives(
    monkeypatch,
    tmp_path,
    capsys,
):
    """Would fail if failed TERM/KILL cleanup was still reported as plain RC75."""
    from codex_usage import integration_watchdog

    class Process:
        pid = 12345

        def wait(self, *, timeout=None):
            if timeout == 0.1:
                raise subprocess.TimeoutExpired(cmd=["publisher"], timeout=timeout)
            return 0

        def poll(self):
            return None

    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Process(),
    )
    monkeypatch.setattr(
        integration_watchdog.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(PermissionError("killpg denied")),
    )
    monkeypatch.setattr(integration_watchdog, "_process_group_exists", lambda _pgid: True)
    monkeypatch.setattr(integration_watchdog.time, "sleep", lambda _seconds: None)

    assert integration_watchdog._run_publisher_stage(
        tmp_path / "release/venv/bin/codex-usage",
        ("integration-snapshot", "--schema", "2", "--format", "json"),
        0.1,
        child_environ=_static_child_environment(),
    ) == 69
    assert "cleanup failed" in capsys.readouterr().err


def test_publisher_stage_timeout_reports_bounded_cleanup_error_types_secret_safe(
    monkeypatch,
    tmp_path,
    capsys,
):
    """Would fail if cleanup/reap details collapsed to bare RC69."""
    from codex_usage import integration_watchdog

    class Process:
        pid = 12345

        def poll(self):
            return None

        def wait(self, *, timeout=None):
            raise subprocess.TimeoutExpired(["publisher"], timeout)

    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Process(),
    )
    monkeypatch.setattr(
        integration_watchdog.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(
            PermissionError("sk-proj-cleanup-secret")
        ),
    )
    monkeypatch.setattr(integration_watchdog, "_process_group_exists", lambda _pgid: True)
    monkeypatch.setattr(integration_watchdog.time, "sleep", lambda _seconds: None)

    assert integration_watchdog._run_publisher_stage(
        tmp_path / "release/venv/bin/codex-usage",
        ("integration-snapshot", "--schema", "2", "--format", "json"),
        0.1,
        child_environ=_static_child_environment(),
    ) == 69
    diagnostic = capsys.readouterr().err
    assert "integration publisher exited with rc=69" in diagnostic
    assert "error=cleanup failed" in diagnostic
    assert "error_types=" in diagnostic
    assert "PermissionError" in diagnostic
    assert "_ProcessGroupCleanupError" in diagnostic
    assert "sk-proj-cleanup-secret" not in diagnostic


def test_publisher_stage_timeout_requires_second_wait_reap_before_rc75(
    monkeypatch,
    tmp_path,
    capsys,
):
    """Would fail if RC75 was returned before the post-KILL leader wait completed."""
    from codex_usage import integration_watchdog

    class Process:
        pid = 12345
        stdout = None
        stderr = None

        def wait(self, *, timeout=None):
            raise subprocess.TimeoutExpired(cmd=["publisher"], timeout=timeout)

    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Process(),
    )
    monkeypatch.setattr(integration_watchdog.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(integration_watchdog, "_process_group_exists", lambda _pgid: False)

    assert integration_watchdog._run_publisher_stage(
        tmp_path / "release/venv/bin/codex-usage",
        ("integration-snapshot", "--schema", "2", "--format", "json"),
        0.1,
        child_environ=_static_child_environment(),
    ) == 69
    assert "cleanup failed" in capsys.readouterr().err


def test_publisher_stage_timeout_treats_esrch_as_already_cleaned(
    monkeypatch,
    tmp_path,
):
    """Would fail if a vanished process group was reported as cleanup failure."""
    from codex_usage import integration_watchdog

    class Process:
        pid = 12345
        stdout = None
        stderr = None
        wait_calls = 0

        def wait(self, *, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired(cmd=["publisher"], timeout=timeout)
            return 0

    process = Process()

    def gone(*_args):
        raise ProcessLookupError("already gone")

    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(integration_watchdog.os, "killpg", gone)
    monkeypatch.setattr(integration_watchdog, "_process_group_exists", lambda _pgid: False)

    assert integration_watchdog._run_publisher_stage(
        tmp_path / "release/venv/bin/codex-usage",
        ("integration-snapshot", "--schema", "2", "--format", "json"),
        0.1,
        child_environ=_static_child_environment(),
    ) == 75


class _SyntheticStageCancellation(BaseException):
    pass


def test_call_with_timeout_preserves_cleanup_exception_that_masks_alarm():
    """Would fail if a finally cleanup error turned a timed-out stage into bare RC75."""
    from codex_usage import integration_watchdog

    def callback():
        try:
            time.sleep(10)
        finally:
            raise RuntimeError("synthetic finally cleanup failure")

    with pytest.raises(ExceptionGroup) as exc_info:
        integration_watchdog._call_with_timeout(
            "synthetic masked alarm",
            0.05,
            callback,
        )

    flattened = _flatten_exception_group(exc_info.value)
    assert any(
        isinstance(error, integration_watchdog._IntegrationWatchdogStageTimeout)
        for error in flattened
    )
    assert any(
        isinstance(error, RuntimeError)
        and "synthetic finally cleanup failure" in str(error)
        for error in flattened
    )


def test_execute_maps_nonfatal_baseexceptiongroup_to_unavailable_rc(
    tmp_path,
):
    """Would fail if a grouped stage cleanup BaseException escaped the unit wrapper."""
    from codex_usage import integration_watchdog

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=_environment(tmp_path),
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=lambda _path, **_kwargs: (_ for _ in ()).throw(
            BaseExceptionGroup(
                "synthetic grouped cleanup",
                [_SyntheticStageCancellation("synthetic cancellation")],
            ),
        ),
        verifier=lambda **_kwargs: pytest.fail("attestation after grouped failure"),
        publisher_runner=lambda *_args, **_kwargs: pytest.fail(
            "publish after grouped failure"
        ),
    ) == 69


def test_publisher_stage_timeout_with_selector_cleanup_failure_is_not_rc75(
    monkeypatch,
    tmp_path,
    capsys,
):
    """Would fail if timeout plus selector cleanup failure reported successful reap."""
    from codex_usage import integration_watchdog

    closed_streams: list[str] = []

    class Stream:
        def __init__(self, name: str):
            self.name = name

        def fileno(self):
            return 91

        def close(self):
            closed_streams.append(self.name)

    class Selector:
        def register(self, _stream, _events):
            return None

        def get_map(self):
            return {"stdout": object()}

        def select(self, _timeout):
            return []

        def close(self):
            raise RuntimeError("synthetic selector cleanup failure")

    class Process:
        pid = 12345
        stdout = Stream("stdout")
        stderr = Stream("stderr")

        def poll(self):
            return None

        def wait(self, *, timeout=None):
            return 0

    monkeypatch.setattr(
        integration_watchdog.selectors,
        "DefaultSelector",
        Selector,
    )
    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Process(),
    )
    monkeypatch.setattr(integration_watchdog.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(integration_watchdog, "_process_group_exists", lambda _pgid: False)

    assert integration_watchdog._run_publisher_stage(
        tmp_path / "release/venv/bin/codex-usage",
        ("integration-snapshot", "--schema", "2", "--format", "json"),
        0.1,
        child_environ=_static_child_environment(),
    ) == 69
    assert closed_streams == ["stdout", "stderr"]
    assert "rc=69" in capsys.readouterr().err


@pytest.mark.parametrize(
    "failure",
    (
        RuntimeError("synthetic outer alarm"),
        _SyntheticStageCancellation("synthetic cancellation"),
    ),
)
def test_subprocess_stage_exception_during_wait_terminates_live_process_group(
    monkeypatch,
    failure,
):
    """Would fail if non-TimeoutExpired wait interrupts left the process group alive."""
    from codex_usage import integration_watchdog

    class Process:
        pid = 12345

        def poll(self):
            return None

        def wait(self, *, timeout=None):
            assert timeout == 10
            raise failure

    process = Process()
    terminated: list[object] = []
    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        integration_watchdog,
        "_terminate_process_group",
        terminated.append,
    )

    with pytest.raises(type(failure), match=str(failure)):
        integration_watchdog._run_subprocess_stage(
            ["synthetic"],
            timeout_seconds=10,
            child_environ=_static_child_environment(),
        )
    assert terminated == [process]


def test_subprocess_stage_cleanup_baseexception_is_aggregated_with_primary(
    monkeypatch,
):
    """Would fail if a cleanup BaseException replaced the original wait failure."""
    from codex_usage import integration_watchdog

    class Process:
        pid = 12345
        stdout = None
        stderr = None

        def wait(self, *, timeout=None):
            raise RuntimeError("synthetic primary wait failure")

    def cleanup_interrupt(*_args):
        raise _SyntheticStageCancellation("synthetic cleanup cancellation")

    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Process(),
    )
    monkeypatch.setattr(integration_watchdog.os, "killpg", cleanup_interrupt)

    with pytest.raises(BaseExceptionGroup) as exc_info:
        integration_watchdog._run_subprocess_stage(
            ["synthetic"],
            timeout_seconds=10,
            child_environ=_static_child_environment(),
        )
    flattened = _flatten_exception_group(exc_info.value)
    assert any(
        isinstance(error, RuntimeError)
        and "synthetic primary wait failure" in str(error)
        for error in flattened
    )
    assert any(
        isinstance(error, _SyntheticStageCancellation)
        and "synthetic cleanup cancellation" in str(error)
        for error in flattened
    )


def test_subprocess_stage_exception_after_leader_reaped_terminates_process_group(
    monkeypatch,
):
    """Would fail if a reaped leader suppressed cleanup for live group members."""
    from codex_usage import integration_watchdog

    class Process:
        pid = 12345

        def poll(self):
            return 0

        def wait(self, *, timeout=None):
            raise _SyntheticStageCancellation("leader was already reaped")

    process = Process()
    terminated: list[object] = []
    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        integration_watchdog,
        "_terminate_process_group",
        terminated.append,
    )

    with pytest.raises(_SyntheticStageCancellation, match="already reaped"):
        integration_watchdog._run_subprocess_stage(
            ["synthetic"],
            timeout_seconds=10,
            child_environ=_static_child_environment(),
        )
    assert terminated == [process]


def _write_leader_reaped_process_tree_script(script: Path, marker: Path) -> None:
    script.write_text(
        (
            f"#!{sys.executable}\n"
            "from __future__ import annotations\n"
            "import pathlib\n"
            "import subprocess\n"
            "import sys\n"
            f"marker = {str(marker)!r}\n"
            "subprocess.Popen([\n"
            "    sys.executable,\n"
            "    '-c',\n"
            "    'import pathlib, time; '\n"
            "    'time.sleep(0.45); '\n"
            "    f'pathlib.Path({marker!r}).write_text(\"mutated\", encoding=\"utf-8\")',\n"
            "])\n"
        ),
        encoding="utf-8",
    )
    script.chmod(0o700)


def test_subprocess_stage_exception_after_real_leader_reap_kills_grandchild(
    tmp_path,
    monkeypatch,
):
    """Would fail if cleanup depended on poll() saying the leader still ran."""
    from codex_usage import integration_watchdog

    script = tmp_path / "leader.py"
    marker = tmp_path / "grandchild-marker"
    _write_leader_reaped_process_tree_script(script, marker)
    real_popen = integration_watchdog.subprocess.Popen

    class ReapedLeaderProxy:
        def __init__(self, process):
            self._process = process
            self.pid = process.pid

        def poll(self):
            return self._process.poll()

        def wait(self, *, timeout=None):
            self._process.wait(timeout=1)
            raise _SyntheticStageCancellation("synthetic cancellation after reap")

    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda *args, **kwargs: ReapedLeaderProxy(real_popen(*args, **kwargs)),
    )

    with pytest.raises(_SyntheticStageCancellation):
        integration_watchdog._run_subprocess_stage(
            [str(script)],
            timeout_seconds=10,
            child_environ=_static_child_environment(),
        )
    time.sleep(0.8)
    assert not marker.exists()


def test_subprocess_stage_success_after_real_leader_reap_kills_grandchild(
    tmp_path,
):
    """Would fail if a successful direct leader could leave descendants running."""
    from codex_usage import integration_watchdog

    script = tmp_path / "leader-success.py"
    marker = tmp_path / "success-grandchild-marker"
    _write_leader_reaped_process_tree_script(script, marker)

    assert (
        integration_watchdog._run_subprocess_stage(
            [str(script)],
            timeout_seconds=10,
            child_environ=_static_child_environment(),
        )
        == 0
    )
    time.sleep(0.8)
    assert not marker.exists()


def test_subprocess_stage_preserves_wait_error_when_group_signal_fails(monkeypatch):
    """Would fail if cleanup errors replaced the original wait interruption."""
    from codex_usage import integration_watchdog

    class Process:
        pid = 12345

        def __init__(self):
            self.wait_calls = 0

        def poll(self):
            return None

        def wait(self, *, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise RuntimeError("synthetic outer alarm")
            return 0

    process = Process()
    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        integration_watchdog.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(PermissionError("killpg denied")),
    )

    with pytest.raises(ExceptionGroup) as exc_info:
        integration_watchdog._run_subprocess_stage(
            ["synthetic"],
            timeout_seconds=10,
            child_environ=_static_child_environment(),
        )
    flattened = _flatten_exception_group(exc_info.value)
    assert any(
        isinstance(error, RuntimeError) and "synthetic outer alarm" in str(error)
        for error in flattened
    )
    assert any(
        isinstance(error, PermissionError) and "killpg denied" in str(error)
        for error in flattened
    )
    assert process.wait_calls >= 2


def test_subprocess_stage_cleanup_disarms_outer_sigalrm_until_group_recheck(
    monkeypatch,
):
    """Would fail if the outer stage timer could interrupt bounded cleanup."""
    from codex_usage import integration_watchdog

    class Process:
        pid = 12345
        wait_calls = 0

        def wait(self, *, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired(cmd=["synthetic"], timeout=timeout)
            return 0

        def poll(self):
            return None

    process = Process()
    group_checks = iter((True, False))
    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(integration_watchdog.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(
        integration_watchdog,
        "_process_group_exists",
        lambda _pgid: next(group_checks),
    )

    def assert_timer_disarmed(_seconds):
        assert signal.getitimer(signal.ITIMER_REAL)[0] == 0

    monkeypatch.setattr(integration_watchdog.time, "sleep", assert_timer_disarmed)

    assert integration_watchdog._call_with_timeout(
        "outer unit budget",
        1.0,
        lambda: integration_watchdog._run_subprocess_stage(
            ["synthetic"],
            timeout_seconds=0.1,
            child_environ=_static_child_environment(),
        ),
    ) == 75


def test_cleanup_disarm_blocks_sigalrm_before_clearing_timer_and_restores_deadline(
    monkeypatch,
):
    """Would fail if cleanup could race a pending alarm or shift its deadline."""
    from codex_usage import integration_watchdog

    events: list[tuple[str, object, object | None]] = []
    previous_mask = {signal.SIGUSR1}
    monotonic_values = iter((100.0, 104.0))
    last_monotonic = 104.0

    def fake_monotonic() -> float:
        nonlocal last_monotonic
        try:
            last_monotonic = next(monotonic_values)
        except StopIteration:
            pass
        return last_monotonic

    def fake_pthread_sigmask(action, mask):
        events.append(("sigmask", action, frozenset(mask)))
        return previous_mask

    def fake_setitimer(which, seconds, interval=0.0):
        events.append(("setitimer", seconds, interval))
        if which != signal.ITIMER_REAL:
            raise AssertionError(which)
        if seconds == 0 and events == [
            ("sigmask", signal.SIG_BLOCK, frozenset({signal.SIGALRM})),
            ("setitimer", 0, 0.0),
        ]:
            return (10.0, 0.25)
        return (0.0, 0.0)

    monkeypatch.setattr(integration_watchdog.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(integration_watchdog.signal, "pthread_sigmask", fake_pthread_sigmask)
    monkeypatch.setattr(integration_watchdog.signal, "setitimer", fake_setitimer)

    with integration_watchdog._disarmed_real_timer_for_cleanup():
        events.append(("body", None, None))

    assert events == [
        ("sigmask", signal.SIG_BLOCK, frozenset({signal.SIGALRM})),
        ("setitimer", 0, 0.0),
        ("body", None, None),
        ("setitimer", 6.0, 0.25),
        ("sigmask", signal.SIG_SETMASK, frozenset(previous_mask)),
    ]


def test_call_with_timeout_restores_preexisting_itimer_deadline_after_elapsed_time(
    monkeypatch,
):
    """Would fail if a colliding pre-existing SIGALRM deadline was shifted later."""
    from codex_usage import integration_watchdog

    events: list[tuple[str, object, object | None]] = []
    previous_handler = object()
    monotonic_values = iter((200.0, 203.0))
    last_monotonic = 203.0

    def fake_monotonic() -> float:
        nonlocal last_monotonic
        try:
            last_monotonic = next(monotonic_values)
        except StopIteration:
            pass
        return last_monotonic

    def fake_setitimer(which, seconds, interval=0.0):
        if which != signal.ITIMER_REAL:
            raise AssertionError(which)
        events.append(("setitimer", seconds, interval))
        if events == [("setitimer", 0, 0.0)]:
            return (5.0, 0.0)
        return (0.0, 0.0)

    def fake_signal(signum, handler):
        if signum != signal.SIGALRM:
            raise AssertionError(signum)
        events.append(("signal", "stage" if callable(handler) else handler, None))

    monkeypatch.setattr(integration_watchdog.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(integration_watchdog.signal, "getsignal", lambda signum: previous_handler)
    monkeypatch.setattr(integration_watchdog.signal, "setitimer", fake_setitimer)
    monkeypatch.setattr(integration_watchdog.signal, "signal", fake_signal)

    assert integration_watchdog._call_with_timeout("bounded stage", 2.0, lambda: "ok") == "ok"
    assert events == [
        ("setitimer", 0, 0.0),
        ("signal", "stage", None),
        ("setitimer", 2.0, 0.0),
        ("setitimer", 0, 0.0),
        ("signal", previous_handler, None),
        ("setitimer", 2.0, 0.0),
    ]


def test_call_with_timeout_does_not_rearm_expired_preexisting_itimer(monkeypatch):
    """Would fail if an already-elapsed prior deadline was silently postponed."""
    from codex_usage import integration_watchdog

    events: list[tuple[str, object, object | None]] = []
    previous_handler = object()
    monotonic_values = iter((300.0, 303.0))
    last_monotonic = 303.0

    def fake_monotonic() -> float:
        nonlocal last_monotonic
        try:
            last_monotonic = next(monotonic_values)
        except StopIteration:
            pass
        return last_monotonic

    def fake_setitimer(which, seconds, interval=0.0):
        if which != signal.ITIMER_REAL:
            raise AssertionError(which)
        events.append(("setitimer", seconds, interval))
        if events == [("setitimer", 0, 0.0)]:
            return (1.0, 0.0)
        return (0.0, 0.0)

    monkeypatch.setattr(integration_watchdog.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(integration_watchdog.signal, "getsignal", lambda signum: previous_handler)
    monkeypatch.setattr(integration_watchdog.signal, "setitimer", fake_setitimer)
    monkeypatch.setattr(
        integration_watchdog.signal,
        "signal",
        lambda signum, handler: events.append(("signal", handler, None)),
    )

    assert integration_watchdog._call_with_timeout("bounded stage", 2.0, lambda: "ok") == "ok"
    assert events == [
        ("setitimer", 0, 0.0),
        ("signal", events[1][1], None),
        ("setitimer", 2.0, 0.0),
        ("setitimer", 0, 0.0),
        ("signal", previous_handler, None),
    ]


def test_publisher_stage_maps_wait_oserror_to_unavailable_rc(monkeypatch, tmp_path):
    """Would fail if Popen.wait OSError was reported as invalid evidence RC70."""
    from codex_usage import integration_watchdog

    class Process:
        pid = 12345

        def wait(self, *, timeout=None):
            raise OSError("synthetic wait failure")

        def poll(self):
            return None

    monkeypatch.setattr(
        integration_watchdog.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Process(),
    )
    monkeypatch.setattr(integration_watchdog, "_terminate_process_group", lambda _process: None)

    assert integration_watchdog._run_publisher_stage(
        tmp_path / "release/venv/bin/codex-usage",
        ("integration-snapshot", "--schema", "2", "--format", "json"),
        10,
        child_environ=_static_child_environment(),
    ) == 69


def test_publisher_stage_emits_bounded_sanitized_failure_diagnostics(
    tmp_path,
    capsys,
):
    """Would fail if publisher stderr stayed fully hidden or unbounded."""
    from codex_usage import integration_watchdog

    launcher = tmp_path / "publisher.py"
    noisy = "x" * 9000
    launcher.write_text(
        (
            f"#!{sys.executable}\n"
            "from __future__ import annotations\n"
            "import sys\n"
            f"sys.stderr.write({noisy!r} + '\\x1b[31munsafe\\n')\n"
            "raise SystemExit(65)\n"
        ),
        encoding="utf-8",
    )
    launcher.chmod(0o700)

    assert integration_watchdog._run_publisher_stage(
        launcher,
        ("integration-snapshot", "--schema", "2", "--format", "json"),
        5,
        child_environ=_static_child_environment(),
    ) == 65
    diagnostic = capsys.readouterr().err
    assert "integration publisher exited with rc=65" in diagnostic
    assert "truncated" in diagnostic
    assert "\x1b" not in diagnostic
    assert len(diagnostic.encode()) <= 4608


def test_publisher_stage_diagnostic_suppresses_stdout_and_keeps_known_stderr_token(
    tmp_path,
    capsys,
):
    """Would fail if successful payload bytes or secrets leaked through stdout logs."""
    from codex_usage import integration_watchdog

    launcher = tmp_path / "publisher.py"
    launcher.write_text(
        (
            f"#!{sys.executable}\n"
            "from __future__ import annotations\n"
            "import sys\n"
            "sys.stdout.write('usage-payload-secret-token\\n')\n"
            "sys.stderr.write('integration_snapshot_invalid_source\\n')\n"
            "raise SystemExit(65)\n"
        ),
        encoding="utf-8",
    )
    launcher.chmod(0o700)

    assert integration_watchdog._run_publisher_stage(
        launcher,
        ("integration-snapshot", "--schema", "2", "--format", "json"),
        5,
        child_environ=_static_child_environment(),
    ) == 65
    diagnostic = capsys.readouterr().err
    assert "integration publisher exited with rc=65" in diagnostic
    assert "integration_snapshot_invalid_source" in diagnostic
    assert "stdout" not in diagnostic
    assert "usage-payload-secret-token" not in diagnostic


def test_publisher_stage_diagnostic_redacts_unknown_stderr_payload(
    tmp_path,
    capsys,
):
    """Would fail if arbitrary publisher stderr was copied into the journal."""
    from codex_usage import integration_watchdog

    launcher = tmp_path / "publisher.py"
    launcher.write_text(
        (
            f"#!{sys.executable}\n"
            "from __future__ import annotations\n"
            "import sys\n"
            "sys.stderr.write('unexpected detail sk-proj-test-secret value\\n')\n"
            "raise SystemExit(69)\n"
        ),
        encoding="utf-8",
    )
    launcher.chmod(0o700)

    assert integration_watchdog._run_publisher_stage(
        launcher,
        ("integration-snapshot", "--schema", "2", "--format", "json"),
        5,
        child_environ=_static_child_environment(),
    ) == 69
    diagnostic = capsys.readouterr().err
    assert "integration publisher exited with rc=69" in diagnostic
    assert "stderr=unrecognized" in diagnostic
    assert "sk-proj-test-secret" not in diagnostic
    assert "unexpected detail" not in diagnostic


def _write_timeout_process_tree_script(script: Path, marker: Path) -> None:
    script.write_text(
        (
            f"#!{sys.executable}\n"
            "from __future__ import annotations\n"
            "import pathlib\n"
            "import subprocess\n"
            "import sys\n"
            "import time\n"
            f"marker = {str(marker)!r}\n"
            "subprocess.Popen([\n"
            "    sys.executable,\n"
            "    '-c',\n"
            "    'import pathlib, time; '\n"
            "    'time.sleep(0.35); '\n"
            "    f'pathlib.Path({marker!r}).write_text(\"mutated\", encoding=\"utf-8\")',\n"
            "])\n"
            "time.sleep(10)\n"
        ),
        encoding="utf-8",
    )
    script.chmod(0o700)


def _write_outer_alarm_process_tree_script(script: Path, marker: Path) -> None:
    script.write_text(
        (
            f"#!{sys.executable}\n"
            "from __future__ import annotations\n"
            "import pathlib\n"
            "import subprocess\n"
            "import sys\n"
            "import time\n"
            f"marker = {str(marker)!r}\n"
            "subprocess.Popen([\n"
            "    sys.executable,\n"
            "    '-c',\n"
            "    'import pathlib, time; '\n"
            "    'time.sleep(0.4); '\n"
            "    f'pathlib.Path({marker!r}).write_text(\"mutated\", encoding=\"utf-8\")',\n"
            "])\n"
            "time.sleep(1.5)\n"
        ),
        encoding="utf-8",
    )
    script.chmod(0o700)


def test_publisher_stage_timeout_kills_grandchild_before_late_evidence_mutation(
    tmp_path,
):
    """Would fail if publisher timeout killed only the direct launcher process."""
    from codex_usage import integration_watchdog

    launcher = tmp_path / "launcher.py"
    marker = tmp_path / "late-evidence-mutation"
    _write_timeout_process_tree_script(launcher, marker)

    assert integration_watchdog._run_publisher_stage(
        launcher,
        ("integration-snapshot", "--schema", "2", "--format", "json"),
        0.1,
        child_environ=_static_child_environment(),
    ) == 75
    time.sleep(0.7)

    assert not marker.exists()


def test_generic_watchdog_stage_timeout_kills_grandchild_before_late_mutation(
    tmp_path,
    monkeypatch,
):
    """Would fail if generic watchdog timeout left a child process group alive."""
    from codex_usage import integration_watchdog

    launcher = tmp_path / "generic-python.py"
    marker = tmp_path / "late-generic-mutation"
    _write_timeout_process_tree_script(launcher, marker)
    monkeypatch.setattr(integration_watchdog.sys, "executable", str(launcher))
    monkeypatch.setattr(integration_watchdog, "GENERIC_WATCHDOG_TIMEOUT_SECONDS", 0.1)

    assert integration_watchdog._run_watchdog_stage(
        tmp_path / "config.toml",
        child_environ=_static_child_environment(),
    ) == 75
    time.sleep(0.7)

    assert not marker.exists()


def test_execute_outer_generic_timeout_kills_real_process_group_before_mutation(
    tmp_path,
    monkeypatch,
):
    """Would fail if execute's SIGALRM during Popen.wait left a grandchild alive."""
    from codex_usage import integration_watchdog

    launcher = tmp_path / "generic-watchdog-tree.py"
    marker = tmp_path / "late-generic-mutation-after-outer-timeout"
    _write_outer_alarm_process_tree_script(launcher, marker)
    monkeypatch.setattr(
        integration_watchdog,
        "GENERIC_WATCHDOG_TIMEOUT_SECONDS",
        0.1,
    )
    monkeypatch.setattr(integration_watchdog, "TOTAL_RUNTIME_BUDGET_SECONDS", 1)

    def watchdog_runner(
        _config_path: Path,
        *,
        child_environ,
    ) -> int:
        return integration_watchdog._run_subprocess_stage(
            [str(launcher)],
            timeout_seconds=10,
            child_environ=child_environ,
        )

    assert integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=_environment(tmp_path),
        runtime_entrypoint_path=tmp_path / "trusted.py",
        watchdog_runner=watchdog_runner,
        verifier=lambda **_kwargs: pytest.fail("attestation after generic timeout"),
        publisher_runner=lambda *_args, **_kwargs: pytest.fail(
            "publish after generic timeout"
        ),
    ) == 75
    time.sleep(1.0)

    assert not marker.exists()


def test_publisher_stage_maps_launch_oserror(monkeypatch, tmp_path):
    """Launcher OSError must not be reported as a successful publish."""
    from codex_usage import integration_watchdog

    def launch_error(*_args, **_kwargs):
        raise OSError("synthetic launcher failure")

    monkeypatch.setattr(integration_watchdog.subprocess, "Popen", launch_error)

    assert integration_watchdog._run_publisher_stage(
        tmp_path / "release/venv/bin/codex-usage",
        ("integration-snapshot", "--schema", "2", "--format", "json"),
        integration_watchdog.PUBLISH_TIMEOUT_SECONDS,
        child_environ=_static_child_environment(),
    ) == 69


def test_main_wires_private_service_runtime_attestation_dependency(monkeypatch):
    from codex_usage import integration_entrypoint, integration_watchdog

    observed: dict[str, object] = {}

    def fake_execute(argv, **kwargs):
        observed["argv"] = argv
        observed.update(kwargs)
        return 0

    monkeypatch.setattr(integration_watchdog, "execute_publisher_only", fake_execute)

    assert integration_watchdog.main(()) == 0
    assert observed["argv"] == ()
    assert observed["environ"] is integration_watchdog.os.environ
    assert observed["runtime_entrypoint_path"] == Path(
        integration_entrypoint.__file__
    )
    assert "watchdog_runner" not in observed
    assert (
        observed["verifier"]
        is integration_watchdog.verify_active_manifest_against_private_service_runtime
    )
    assert observed["publisher_runner"] is integration_watchdog._run_publisher_stage
    assert observed["interpreter_path"] == Path(sys.executable)
