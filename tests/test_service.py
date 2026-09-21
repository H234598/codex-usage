from __future__ import annotations

import base64
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import ClassVar

import pytest

import codex_usage.service as service_module
from codex_usage.config import AppConfig
from codex_usage.models import Account
from codex_usage.service import (
    SERVICE_NAME,
    TIMER_NAME,
    ServiceError,
    _terminate_systemctl_process,
    _unit_directory,
    managed_service_config_path,
    service_disable,
    service_enable,
    service_install,
    service_status,
    service_uninstall,
)

EXPECTED_INTEGRATION_WATCHDOG_SYSTEMD_TIMEOUT_SECONDS = 270


def _write_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)


def _inactive_systemctl(*args: str, check: bool = True):
    if args[:1] == ("is-enabled",):
        return subprocess.CompletedProcess(args, 1, "disabled\n", "")
    if args[:1] == ("is-active",):
        return subprocess.CompletedProcess(args, 1, "inactive\n", "")
    return subprocess.CompletedProcess(args, 0, "", "")


def _write_console_script(path: Path, module: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            f"#!{sys.executable}\n"
            "# -*- coding: utf-8 -*-\n"
            "import sys\n"
            f"from {module} import main\n"
            "if __name__ == '__main__':\n"
            "    sys.exit(main())\n"
        ),
        encoding="utf-8",
    )
    path.chmod(0o700)


def _write_console_script_with_main_guard(
    path: Path,
    module: str,
    guard_body: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            f"#!{sys.executable}\n"
            "import sys\n"
            f"from {module} import main\n"
            "if __name__ == '__main__':\n"
            f"{guard_body}"
        ),
        encoding="utf-8",
    )
    path.chmod(0o700)


class _FakeCodexUsageDistribution:
    def __init__(self, base: Path, *, version: str, files: object) -> None:
        self._base = base
        self.version = version
        self.metadata = {"Name": "codex-usage", "Version": version}
        self.files = (
            files
            if not isinstance(files, tuple)
            else tuple(PurePosixPath(file) for file in files)
        )

    def locate_file(self, path: object) -> Path:
        return (self._base / Path(str(path))).resolve(strict=False)


def _record_hash(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode("ascii")
    return "sha256=" + digest.rstrip("=")


def _install_fake_codex_usage_distribution(
    monkeypatch: pytest.MonkeyPatch,
    distribution: _FakeCodexUsageDistribution,
) -> None:
    def load_distribution(name: str):
        if name != "codex-usage":
            raise AssertionError(f"unexpected distribution lookup: {name}")
        return distribution

    monkeypatch.setattr(
        service_module,
        "importlib_metadata",
        SimpleNamespace(distribution=load_distribution),
        raising=False,
    )


def _write_recorded_distribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = "0.6.539",
    corrupt_record_for: str | None = None,
    metadata_suffix: str = "",
    files_override: object | None = None,
) -> tuple[Path, Path, Path]:
    release = tmp_path / "release"
    bin_dir = release / "bin"
    site_packages = release / "lib" / "python" / "site-packages"
    package = site_packages / "codex_usage"
    dist_info = site_packages / f"codex_usage-{version}.dist-info"
    codex_usage = bin_dir / "codex-usage"
    watchdog = bin_dir / "codex-usage-integration-watchdog"
    cli_module = package / "cli.py"
    watchdog_module = package / "integration_watchdog.py"
    metadata = dist_info / "METADATA"
    record = dist_info / "RECORD"
    package_init = package / "__init__.py"

    _write_console_script(codex_usage, "codex_usage.cli")
    _write_console_script(watchdog, "codex_usage.integration_watchdog")
    package.mkdir(parents=True, exist_ok=True)
    dist_info.mkdir(parents=True, exist_ok=True)
    package_init.write_text('__version__ = "0.6.539"\n', encoding="utf-8")
    cli_module.write_text("def main():\n    return 0\n", encoding="utf-8")
    watchdog_module.write_text("def main():\n    return 0\n", encoding="utf-8")
    from codex_usage.integration_attestation import TRUSTED_CORE_MODULES

    source_package = Path(__file__).parents[1] / "src" / "codex_usage"
    for module_name in TRUSTED_CORE_MODULES:
        target = package / module_name
        if target.exists():
            continue
        shutil.copyfile(source_package / module_name, target)
    metadata.write_text(
        (
            f"Metadata-Version: 2.4\nName: codex-usage\nVersion: {version}\n"
            f"{metadata_suffix}"
        ),
        encoding="utf-8",
    )
    relative_paths = {
        "../../../bin/codex-usage": codex_usage,
        "../../../bin/codex-usage-integration-watchdog": watchdog,
        "codex_usage/__init__.py": package_init,
        "codex_usage/cli.py": cli_module,
        "codex_usage/integration_watchdog.py": watchdog_module,
        f"codex_usage-{version}.dist-info/METADATA": metadata,
    }
    for module_name in TRUSTED_CORE_MODULES:
        relative_paths.setdefault(f"codex_usage/{module_name}", package / module_name)
    rows: list[str] = []
    for relative, path in relative_paths.items():
        payload = path.read_bytes()
        record_hash = _record_hash(payload)
        if relative == corrupt_record_for:
            record_hash = "sha256=" + ("A" * 43)
        rows.append(f"{relative},{record_hash},{len(payload)}\n")
    rows.append(f"codex_usage-{version}.dist-info/RECORD,,\n")
    record.write_text("".join(rows), encoding="utf-8")
    files = tuple([*relative_paths, f"codex_usage-{version}.dist-info/RECORD"])
    _install_fake_codex_usage_distribution(
        monkeypatch,
        _FakeCodexUsageDistribution(
            site_packages,
            version=version,
            files=files if files_override is None else files_override,
        ),
    )
    from codex_usage.integration_attestation import ActiveRelease, VerifiedActiveManifest
    from codex_usage.private_io import FileIdentity

    active = VerifiedActiveManifest(
        active_release=ActiveRelease(
            version="0.6.539",
            release_dir=release,
            launcher_path=codex_usage,
            entrypoint_path=package / "integration_entrypoint.py",
            entrypoint_sha256="0" * 64,
            wheel_sha256="0" * 64,
            record_sha256="0" * 64,
            launcher_sha256="0" * 64,
            release_tree_sha256="0" * 64,
        ),
        release_id="0.6.539-0000000000000000",
        source_manifest_sha256="0" * 64,
        active_manifest_bytes=b"{}",
        active_manifest_sha256="0" * 64,
        state_home_identity=FileIdentity(1, 2, 0o700),
        integration_parent_identity=FileIdentity(1, 3, 0o700),
        active_file_identity=FileIdentity(1, 4, 0o600),
    )
    monkeypatch.setattr(
        service_module,
        "_active_entrypoint_candidate_from_active_manifest",
        lambda **_kwargs: active.active_release.entrypoint_path,
    )
    monkeypatch.setattr(
        service_module,
        "verify_active_manifest_at",
        lambda **_kwargs: active,
    )
    return codex_usage, watchdog, record


def _write_recorded_worktree_distribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path]:
    """Build a RECORD-bound test distribution from this exact worktree source."""
    version = "0.6.539"
    release = tmp_path / "worktree-release"
    bin_dir = release / "bin"
    site_packages = release / "lib" / "python" / "site-packages"
    package = site_packages / "codex_usage"
    dist_info = site_packages / f"codex_usage-{version}.dist-info"
    codex_usage = bin_dir / "codex-usage"
    watchdog = bin_dir / "codex-usage-integration-watchdog"
    source_package = Path(__file__).parents[1] / "src" / "codex_usage"

    _write_console_script(codex_usage, "codex_usage.cli")
    _write_console_script(watchdog, "codex_usage.integration_watchdog")
    for source_file in sorted(source_package.rglob("*.py")):
        destination = package / source_file.relative_to(source_package)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination)
        destination.chmod(0o600)
    dist_info.mkdir(parents=True)
    metadata = dist_info / "METADATA"
    metadata.write_text(
        "Metadata-Version: 2.4\nName: codex-usage\nVersion: 0.6.539\n",
        encoding="utf-8",
    )
    metadata.chmod(0o600)

    relative_paths: dict[str, Path] = {
        "../../../bin/codex-usage": codex_usage,
        "../../../bin/codex-usage-integration-watchdog": watchdog,
        f"codex_usage-{version}.dist-info/METADATA": metadata,
    }
    for source_file in sorted(package.rglob("*")):
        if source_file.is_file():
            relative_path = str(
                PurePosixPath("codex_usage") / source_file.relative_to(package)
            )
            relative_paths[relative_path] = source_file
    record = dist_info / "RECORD"
    rows = [
        f"{relative},{_record_hash(path.read_bytes())},{path.stat().st_size}\n"
        for relative, path in sorted(relative_paths.items())
    ]
    rows.append(f"codex_usage-{version}.dist-info/RECORD,,\n")
    record.write_text("".join(rows), encoding="utf-8")
    record.chmod(0o600)
    _install_fake_codex_usage_distribution(
        monkeypatch,
        _FakeCodexUsageDistribution(
            site_packages,
            version=version,
            files=tuple([*relative_paths, f"codex_usage-{version}.dist-info/RECORD"]),
        ),
    )
    from codex_usage.integration_attestation import ActiveRelease, VerifiedActiveManifest
    from codex_usage.private_io import FileIdentity

    active = VerifiedActiveManifest(
        active_release=ActiveRelease(
            version="0.6.539",
            release_dir=release,
            launcher_path=codex_usage,
            entrypoint_path=package / "integration_entrypoint.py",
            entrypoint_sha256="0" * 64,
            wheel_sha256="0" * 64,
            record_sha256="0" * 64,
            launcher_sha256="0" * 64,
            release_tree_sha256="0" * 64,
        ),
        release_id="0.6.539-0000000000000000",
        source_manifest_sha256="0" * 64,
        active_manifest_bytes=b"{}",
        active_manifest_sha256="0" * 64,
        state_home_identity=FileIdentity(1, 2, 0o700),
        integration_parent_identity=FileIdentity(1, 3, 0o700),
        active_file_identity=FileIdentity(1, 4, 0o600),
    )
    monkeypatch.setattr(
        service_module,
        "_active_entrypoint_candidate_from_active_manifest",
        lambda **_kwargs: active.active_release.entrypoint_path,
    )
    monkeypatch.setattr(
        service_module,
        "verify_active_manifest_at",
        lambda **_kwargs: active,
    )
    return codex_usage, watchdog, record


def _mock_resolved_executable_without_attestation(
    monkeypatch: pytest.MonkeyPatch,
    executable: Path,
) -> None:
    monkeypatch.setattr("codex_usage.service._resolve_codex_usage", lambda: executable)
    monkeypatch.setattr(
        "codex_usage.service._revalidate_integration_watchdog_for_unit_write",
        lambda _executable: None,
    )
    runtime = SimpleNamespace(
        binding=SimpleNamespace(
            watchdog=SimpleNamespace(
                path=executable.with_name("codex-usage-integration-watchdog")
            )
        )
    )
    def fake_materialize(_executable, *, before_cutover=None):
        if before_cutover is not None:
            before_cutover(SimpleNamespace(), None, Path("/tmp/service-runtime-staging"))
        return runtime

    monkeypatch.setattr(
        "codex_usage.service._materialize_service_runtime",
        fake_materialize,
    )
    def fake_prepare_pending_service_transaction(**kwargs):
        return SimpleNamespace(
            document={},
            unit_dir=kwargs["unit_dir"],
            new_units=kwargs["new_units"],
        )

    def fake_publish_pending_service_unit_generation(pending, path):
        label = "systemd service" if path.name == SERVICE_NAME else "systemd timer"
        service_module.write_private_text(
            path,
            pending.new_units[path],
            label=label,
            mode=0o600,
        )

    monkeypatch.setattr(
        "codex_usage.service._prepare_pending_service_transaction",
        fake_prepare_pending_service_transaction,
    )
    monkeypatch.setattr(
        "codex_usage.service._publish_pending_service_unit_generation",
        fake_publish_pending_service_unit_generation,
    )
    monkeypatch.setattr(
        "codex_usage.service._advance_pending_service_transaction",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "codex_usage.service._quiesce_systemd_activation",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "codex_usage.service._remove_pending_service_transaction",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "codex_usage.service._revalidate_service_runtime_for_unit_write",
        lambda _runtime: None,
    )
    monkeypatch.setattr(
        "codex_usage.service._revalidate_active_producer_for_unit_write",
        lambda _runtime, _executable: None,
    )
    monkeypatch.setattr("codex_usage.service._rollback_service_runtime", lambda _runtime: None)
    monkeypatch.setattr("codex_usage.service._commit_service_runtime", lambda _runtime: None)


def _prepare_record_bound_service_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the actual runtime transaction; only systemctl stays test-local."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)


def test_user_site_watchdog_is_not_importable_under_hardened_environment(
    tmp_path,
    monkeypatch,
):
    """Records the pre-cutover defect: a user-site wrapper cannot import Core."""
    _codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    completed = subprocess.run(
        [str(watchdog), "--config", str(tmp_path / "config.toml")],
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode != 0
    assert "ModuleNotFoundError" in completed.stderr


def test_service_install_materializes_record_bound_runtime_importable_when_hardened(
    tmp_path,
    monkeypatch,
):
    """Would fail if the unit still executed the user-site watchdog directly."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(
        service_module,
        "_systemctl",
        lambda *args, check=True: subprocess.CompletedProcess(
            args,
            1 if args[:1] in {("is-enabled",), ("is-active",)} else 0,
            "disabled\n" if args[:1] == ("is-enabled",) else (
                "inactive\n" if args[:1] == ("is-active",) else ""
            ),
            "",
        ),
    )

    service_install(AppConfig(accounts=()), tmp_path / "config.toml")

    runtime_root = tmp_path / "data" / "codex-usage-service-runtime-v2"
    runtime_watchdog = (
        runtime_root / "current" / "bin" / "codex-usage-integration-watchdog-v2"
    )
    service_text = (
        tmp_path / "config" / "systemd" / "user" / SERVICE_NAME
    ).read_text(encoding="utf-8")

    exec_start = next(
        line.removeprefix("ExecStart=")
        for line in service_text.splitlines()
        if line.startswith("ExecStart=")
    )
    assert shlex.split(exec_start) == [
        str(runtime_watchdog),
    ]
    assert str(watchdog) not in service_text
    record = (
        runtime_root
        / "current"
        / "venv"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "codex_usage-0.6.539.dist-info"
        / "RECORD"
    ).read_text(encoding="utf-8")
    assert "codex_usage/cli.py" not in record
    assert "codex_usage/scheduler.py" not in record
    assert "codex_usage/reactivate.py" not in record
    completed = subprocess.run(
        [str(runtime_watchdog)],
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "ModuleNotFoundError" not in completed.stderr


def test_private_runtime_real_watchdog_wrapper_is_publisher_only_and_rejects_args(
    tmp_path,
    monkeypatch,
):
    """The V2 publisher must not require a browser or generic CLI closure."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    codex_usage, watchdog, _record = _write_recorded_worktree_distribution(
        tmp_path,
        monkeypatch,
    )

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    runtime = service_module._materialize_service_runtime(
        service_module._resolve_codex_usage()
    )
    hardened_environment = {
        "PATH": "/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    candidate_watchdog = (
        runtime.binding.generation.path
        / "venv"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "codex_usage"
        / "integration_watchdog.py"
    )
    assert candidate_watchdog.read_bytes() == (
        Path(__file__).parents[1]
        / "src"
        / "codex_usage"
        / "integration_watchdog.py"
    ).read_bytes()

    watchdog_import = subprocess.run(
        [str(runtime.binding.watchdog.path)],
        env=hardened_environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert watchdog_import.returncode == 69, watchdog_import.stderr
    assert "playwright" not in watchdog_import.stderr.lower()

    forbidden_argument = subprocess.run(
        [str(runtime.binding.watchdog.path), "--config", str(tmp_path / "config.toml")],
        env=hardened_environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert forbidden_argument.returncode == 64, forbidden_argument.stderr


def test_runtime_uses_active_producer_bytes_for_all_known_divergent_modules(
    tmp_path,
    monkeypatch,
):
    """Producer modules are copied from the attested active release, not Core."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)
    active_package = codex_usage.parents[1] / "lib/python/site-packages/codex_usage"
    divergent = (
        "config.py",
        "integration_attestation.py",
        "integration_evidence.py",
        "integration_pool_authority.py",
        "models.py",
        "private_io.py",
    )
    expected: dict[str, bytes] = {}
    for name in divergent:
        path = active_package / name
        path.write_bytes(path.read_bytes() + b"\n# active producer divergence\n")
        expected[name] = path.read_bytes()

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    runtime = service_module._materialize_service_runtime(
        service_module._resolve_codex_usage()
    )
    package = (
        runtime.binding.generation.path
        / "venv"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "codex_usage"
    )
    assert {name: (package / name).read_bytes() for name in divergent} == expected


def test_service_install_rejects_active_producer_drift_before_unit_write(
    tmp_path,
    monkeypatch,
):
    """An active-producer rebind cannot leave a unit pointing at stale bytes."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(service_module, "_systemctl", _inactive_systemctl)
    config_path = tmp_path / "config.toml"
    service_install(AppConfig(accounts=()), config_path)
    active_module = (
        codex_usage.parents[1]
        / "lib/python/site-packages/codex_usage/integration_pool_authority.py"
    )

    def drift_active_producer() -> None:
        active_module.write_bytes(active_module.read_bytes() + b"\n# drift\n")

    monkeypatch.setattr(service_module, "_before_service_unit_write", drift_active_producer)
    with pytest.raises(ServiceError, match="active integration producer changed before unit write"):
        service_install(AppConfig(accounts=()), config_path)


def test_runtime_rollback_rejects_replaced_previous_generation(
    tmp_path,
    monkeypatch,
):
    """Would fail if a replaced rollback generation could become current."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    executable = service_module._resolve_codex_usage()
    first = service_module._materialize_service_runtime(executable)
    service_module._commit_service_runtime(first)
    second = service_module._materialize_service_runtime(executable)
    assert second.rollback_path is not None
    replacement_source = second.rollback_path.with_name("replaced-previous")
    second.rollback_path.rename(replacement_source)
    second.rollback_path.mkdir(mode=0o700)

    with pytest.raises(ServiceError, match="service runtime changed before rollback"):
        service_module._rollback_service_runtime(second)

    current = service_module._service_runtime_binding(
        tmp_path / "data" / "codex-usage-service-runtime-v2",
        tmp_path / "data" / "codex-usage-service-runtime-v2" / "current",
    )
    assert current.generation == second.binding.generation
    assert current.interpreter == second.binding.interpreter
    assert current.watchdog == second.binding.watchdog


def test_runtime_and_units_rollback_without_retaining_new_generation(
    tmp_path,
    monkeypatch,
):
    """Would fail if a failed unit install left a new runtime generation behind."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(
        service_module,
        "_systemctl",
        _inactive_systemctl,
    )
    config_path = tmp_path / "config.toml"
    service_install(AppConfig(accounts=()), config_path)
    unit_directory = tmp_path / "config" / "systemd" / "user"
    previous_service = (unit_directory / SERVICE_NAME).read_bytes()
    previous_timer = (unit_directory / TIMER_NAME).read_bytes()

    def drift_source_wrapper() -> None:
        watchdog.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        watchdog.chmod(0o700)

    monkeypatch.setattr(
        service_module,
        "_before_service_unit_write",
        drift_source_wrapper,
    )

    with pytest.raises(ServiceError, match="changed before unit write"):
        service_install(AppConfig(accounts=()), config_path)

    assert (unit_directory / SERVICE_NAME).read_bytes() == previous_service
    assert (unit_directory / TIMER_NAME).read_bytes() == previous_timer
    runtime_root = tmp_path / "data" / "codex-usage-service-runtime-v2"
    assert sorted(path.name for path in runtime_root.iterdir()) == ["current"]


@pytest.mark.parametrize("failure", ("render", "snapshot"))
def test_service_install_rolls_back_runtime_before_unit_preparation_failure(
    tmp_path,
    monkeypatch,
    failure,
):
    """A render or snapshot failure must leave the prior runtime and units intact."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(
        service_module,
        "_systemctl",
        lambda *args, check=True: subprocess.CompletedProcess(args, 0, "", ""),
    )
    config_path = tmp_path / "config.toml"
    service_install(AppConfig(accounts=()), config_path)
    unit_directory = tmp_path / "config" / "systemd" / "user"
    previous_units = {
        name: (unit_directory / name).read_bytes()
        for name in (SERVICE_NAME, TIMER_NAME)
    }
    root = tmp_path / "data" / "codex-usage-service-runtime-v2"
    previous_current = (root / "current").lstat()

    if failure == "render":
        monkeypatch.setattr(
            service_module,
            "_render_service",
            lambda *_args: (_ for _ in ()).throw(ServiceError("render failed")),
        )
    else:
        original_snapshot = service_module._read_unit_snapshot
        calls = 0

        def fail_first_snapshot(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ServiceError("snapshot failed")
            return original_snapshot(path)

        monkeypatch.setattr(service_module, "_read_unit_snapshot", fail_first_snapshot)

    with pytest.raises(ServiceError, match=f"{failure} failed"):
        service_install(AppConfig(accounts=()), config_path)

    current = (root / "current").lstat()
    assert (current.st_dev, current.st_ino) == (
        previous_current.st_dev,
        previous_current.st_ino,
    )
    assert {name: (unit_directory / name).read_bytes() for name in previous_units} == previous_units


def test_first_service_install_removes_runtime_when_rendering_fails(tmp_path, monkeypatch):
    """A first-install render failure must not publish a new current generation."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    monkeypatch.setattr(
        service_module.shutil,
        "which",
        lambda name: str(codex_usage if name == "codex-usage" else watchdog),
    )
    monkeypatch.setattr(
        service_module,
        "_render_service",
        lambda *_args: (_ for _ in ()).throw(ServiceError("render failed")),
    )

    with pytest.raises(ServiceError, match="render failed"):
        service_install(AppConfig(accounts=()), tmp_path / "config.toml")

    root = tmp_path / "data" / "codex-usage-service-runtime-v2"
    assert not (root / "current").exists()


@pytest.mark.parametrize("existing", (False, True))
def test_runtime_cutover_rolls_back_when_post_rename_attestation_fails(
    tmp_path,
    monkeypatch,
    existing,
):
    """A fault after either rename primitive must not publish a partial runtime."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    executable = service_module._resolve_codex_usage()
    root = tmp_path / "data" / "codex-usage-service-runtime-v2"
    previous: tuple[int, int] | None = None
    if existing:
        initial = service_module._materialize_service_runtime(executable)
        service_module._commit_service_runtime(initial)
        item = (root / "current").lstat()
        previous = (item.st_dev, item.st_ino)

    monkeypatch.setattr(
        service_module,
        "_after_service_runtime_cutover",
        lambda *_args: (_ for _ in ()).throw(ServiceError("post-rename attestation failed")),
        raising=False,
    )

    with pytest.raises(ServiceError, match="post-rename attestation failed"):
        service_module._materialize_service_runtime(executable)

    current = root / "current"
    if previous is None:
        assert not current.exists()
        assert list(root.iterdir()) == []
    else:
        item = current.lstat()
        assert (item.st_dev, item.st_ino) == previous
        assert sorted(path.name for path in root.iterdir()) == ["current"]


def test_service_status_rejects_corrupt_pending_transaction_before_systemctl(
    tmp_path,
    monkeypatch,
):
    """A corrupt pending operation is evidence of an unknown state, never auto-deleted."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_directory = _unit_directory()
    pending = unit_directory / ".codex-usage-service-pending-v1.json"
    pending.write_text("{not-json}\n", encoding="utf-8")
    pending.chmod(0o600)
    monkeypatch.setattr(
        service_module,
        "_systemctl",
        lambda *_args, **_kwargs: pytest.fail("systemctl after corrupt pending transaction"),
    )

    with pytest.raises(ServiceError, match="pending"):
        service_status()

    assert pending.read_text(encoding="utf-8") == "{not-json}\n"


def test_service_enable_rolls_back_runtime_units_and_activation_on_keyboardinterrupt(
    tmp_path,
    monkeypatch,
):
    """Enable/restart is one transaction even when BaseException interrupts it."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    activation: list[tuple[str, ...]] = []

    def systemctl(*args: str, check: bool = True):
        activation.append(args)
        if args[:1] == ("is-enabled",):
            return subprocess.CompletedProcess(args, 1, "disabled\n", "")
        if args[:1] == ("is-active",):
            return subprocess.CompletedProcess(args, 1, "inactive\n", "")
        if args[:1] == ("restart",):
            raise KeyboardInterrupt("synthetic restart interruption")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service_module, "_systemctl", systemctl)
    config_path = tmp_path / "config.toml"
    service_install(AppConfig(accounts=()), config_path)
    unit_directory = tmp_path / "config" / "systemd" / "user"
    old_units = {
        name: (unit_directory / name).read_bytes()
        for name in (SERVICE_NAME, TIMER_NAME)
    }
    runtime_root = tmp_path / "data" / "codex-usage-service-runtime-v2"
    old_current = (runtime_root / "current").lstat()

    with pytest.raises(KeyboardInterrupt, match="synthetic restart interruption"):
        service_enable(AppConfig(accounts=()), config_path)

    current = (runtime_root / "current").lstat()
    assert (current.st_dev, current.st_ino) == (old_current.st_dev, old_current.st_ino)
    assert {
        name: (unit_directory / name).read_bytes() for name in old_units
    } == old_units


def test_status_recovers_verified_pending_runtime_and_unit_transaction(
    tmp_path,
    monkeypatch,
):
    """A trusted next operation converges an interrupted cutover to its old state."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    def systemctl(*args: str, check: bool = True):
        if args[:1] == ("is-enabled",):
            return subprocess.CompletedProcess(args, 1, "disabled\n", "")
        if args[:1] == ("is-active",):
            return subprocess.CompletedProcess(args, 1, "inactive\n", "")
        if args[:1] == ("show",):
            return subprocess.CompletedProcess(args, 0, "SubState=dead\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(service_module, "_systemctl", systemctl)
    config_path = tmp_path / "config.toml"
    service_install(AppConfig(accounts=()), config_path)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    paths = (unit_dir / SERVICE_NAME, unit_dir / TIMER_NAME)
    previous = {path: service_module._read_unit_snapshot(path) for path in paths}
    root = tmp_path / "data" / "codex-usage-service-runtime-v2"
    old_current = (root / "current").lstat()
    pending = None

    def before_cutover(new, old, staging):
        nonlocal pending
        pending = service_module._prepare_pending_service_transaction(
            unit_dir=unit_dir,
            operation="install",
            previous_units=previous,
            new_units={
                unit_dir / SERVICE_NAME: "new unit\n",
                unit_dir / TIMER_NAME: previous[unit_dir / TIMER_NAME] or "",
            },
            activation=("disabled", "inactive"),
            enable_link=None,
            root=root,
            staging=staging,
            old=old,
            new=new,
        )

    executable = service_module._resolve_codex_usage()
    runtime = service_module._materialize_service_runtime(
        executable,
        before_cutover=before_cutover,
    )
    assert pending is not None
    service_module._advance_pending_service_transaction(pending, "runtime")
    service_module._publish_pending_service_unit_generation(
        pending, unit_dir / SERVICE_NAME
    )

    service_status()

    current = (root / "current").lstat()
    assert (current.st_dev, current.st_ino) == (old_current.st_dev, old_current.st_ino)
    assert {path: service_module._read_unit_snapshot(path) for path in paths} == previous
    assert not (unit_dir / service_module.SERVICE_PENDING_V1_NAME).exists()
    assert runtime.rollback_path is not None


def test_pending_recovery_reverts_runtime_before_timer_reactivation(
    tmp_path,
    monkeypatch,
):
    """A restored timer must never start the pending runtime generation."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    root = tmp_path / "data" / "codex-usage-service-runtime-v2"
    reactivated_runtime_ids: list[tuple[int, int]] = []

    def systemctl(*args: str, check: bool = True):
        if args[:1] == ("is-enabled",):
            return subprocess.CompletedProcess(args, 1, "disabled\n", "")
        if args[:1] == ("is-active",):
            return subprocess.CompletedProcess(args, 1, "inactive\n", "")
        if args[:1] in {("enable",), ("start",)}:
            current = (root / "current").lstat()
            reactivated_runtime_ids.append((current.st_dev, current.st_ino))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(service_module, "_systemctl", systemctl)
    config_path = tmp_path / "config.toml"
    service_install(AppConfig(accounts=()), config_path)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    paths = (unit_dir / SERVICE_NAME, unit_dir / TIMER_NAME)
    previous = {path: service_module._read_unit_snapshot(path) for path in paths}
    old_current = (root / "current").lstat()
    pending = None

    def before_cutover(new, old, staging):
        nonlocal pending
        pending = service_module._prepare_pending_service_transaction(
            unit_dir=unit_dir,
            operation="install",
            previous_units=previous,
            new_units={
                unit_dir / SERVICE_NAME: previous[unit_dir / SERVICE_NAME] or "",
                unit_dir / TIMER_NAME: previous[unit_dir / TIMER_NAME] or "",
            },
            activation=("enabled", "active"),
            enable_link=None,
            root=root,
            staging=staging,
            old=old,
            new=new,
        )

    runtime = service_module._materialize_service_runtime(
        service_module._resolve_codex_usage(),
        before_cutover=before_cutover,
    )
    assert pending is not None
    service_module._advance_pending_service_transaction(pending, "runtime")
    reactivated_runtime_ids.clear()

    service_module._recover_pending_service_operation()

    assert reactivated_runtime_ids == [
        (old_current.st_dev, old_current.st_ino),
        (old_current.st_dev, old_current.st_ino),
    ]
    assert runtime.rollback_path is not None


def test_reloaded_pending_recovery_quiesces_active_timer_before_runtime_rollback(
    tmp_path,
    monkeypatch,
):
    """A crash after restart must stop the new publisher before its runtime rolls back."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    state = {"enabled": "disabled", "active": "inactive"}
    events: list[str] = []

    def systemctl(*args: str, check: bool = True):
        if args[:1] == ("is-enabled",):
            return subprocess.CompletedProcess(
                args,
                0 if state["enabled"] == "enabled" else 1,
                state["enabled"] + "\n",
                "",
            )
        if args[:1] == ("is-active",):
            return subprocess.CompletedProcess(
                args,
                0 if state["active"] == "active" else 3,
                state["active"] + "\n",
                "",
            )
        if args[:1] == ("stop",):
            events.append("stop")
            state["active"] = "inactive"
        elif args[:1] == ("disable",):
            events.append("disable")
            state["enabled"] = "disabled"
        elif args[:1] == ("enable",):
            events.append("enable")
            state["enabled"] = "enabled"
        elif args[:1] in {("start",), ("restart",)}:
            events.append(args[0])
            state["active"] = "active"
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(service_module, "_systemctl", systemctl)
    config_path = tmp_path / "config.toml"
    service_install(AppConfig(accounts=()), config_path)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    paths = (unit_dir / SERVICE_NAME, unit_dir / TIMER_NAME)
    previous = {path: service_module._read_unit_snapshot(path) for path in paths}
    root = tmp_path / "data" / "codex-usage-service-runtime-v2"
    old_current = (root / "current").lstat()
    pending = None

    def before_cutover(new, old, staging):
        nonlocal pending
        pending = service_module._prepare_pending_service_transaction(
            unit_dir=unit_dir,
            operation="enable",
            previous_units=previous,
            new_units={path: previous[path] or "" for path in paths},
            activation=("disabled", "inactive"),
            enable_link=None,
            root=root,
            staging=staging,
            old=old,
            new=new,
        )

    service_module._materialize_service_runtime(
        service_module._resolve_codex_usage(),
        before_cutover=before_cutover,
    )
    assert pending is not None
    service_module._advance_pending_service_transaction(pending, "runtime")
    for path in paths:
        service_module._publish_pending_service_unit_generation(pending, path)
    service_module._advance_pending_service_transaction(pending, "units")
    service_module._systemctl("daemon-reload")
    service_module._advance_pending_service_transaction(pending, "reloaded")
    new_current = (root / "current").lstat()
    state.update(enabled="enabled", active="active")
    events.clear()
    original_recover_runtime = service_module._recover_pending_runtime

    def observe_runtime_rollback(runtime):
        current = (root / "current").lstat()
        assert (current.st_dev, current.st_ino) == (
            new_current.st_dev,
            new_current.st_ino,
        )
        events.append("runtime")
        return original_recover_runtime(runtime)

    monkeypatch.setattr(service_module, "_recover_pending_runtime", observe_runtime_rollback)

    service_module._recover_pending_service_operation()

    assert [event for event in events if event in {"stop", "disable", "runtime"}][
        :3
    ] == ["stop", "disable", "runtime"]
    assert "enable" not in events
    assert "start" not in events
    current = (root / "current").lstat()
    assert (current.st_dev, current.st_ino) == (
        old_current.st_dev,
        old_current.st_ino,
    )


def test_reloaded_pending_recovery_does_not_rollback_after_quiesce_failure(
    tmp_path,
    monkeypatch,
):
    """A failed stop leaves runtime, Units, journal, and later activation untouched."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    state = {"enabled": "disabled", "active": "inactive"}
    calls: list[tuple[str, ...]] = []
    fail_stop = False

    def systemctl(*args: str, check: bool = True):
        calls.append(args)
        if args[:1] == ("is-enabled",):
            return subprocess.CompletedProcess(
                args,
                0 if state["enabled"] == "enabled" else 1,
                state["enabled"] + "\n",
                "",
            )
        if args[:1] == ("is-active",):
            return subprocess.CompletedProcess(
                args,
                0 if state["active"] == "active" else 3,
                state["active"] + "\n",
                "",
            )
        if args[:1] == ("stop",) and fail_stop:
            raise ServiceError("timer stop failed")
        if args[:1] == ("stop",):
            state["active"] = "inactive"
        if args[:1] == ("disable",):
            state["enabled"] = "disabled"
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(service_module, "_systemctl", systemctl)
    config_path = tmp_path / "config.toml"
    service_install(AppConfig(accounts=()), config_path)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    paths = (unit_dir / SERVICE_NAME, unit_dir / TIMER_NAME)
    previous = {path: service_module._read_unit_snapshot(path) for path in paths}
    root = tmp_path / "data" / "codex-usage-service-runtime-v2"
    pending = None

    def before_cutover(new, old, staging):
        nonlocal pending
        pending = service_module._prepare_pending_service_transaction(
            unit_dir=unit_dir,
            operation="enable",
            previous_units=previous,
            new_units={path: previous[path] or "" for path in paths},
            activation=("disabled", "inactive"),
            enable_link=None,
            root=root,
            staging=staging,
            old=old,
            new=new,
        )

    service_module._materialize_service_runtime(
        service_module._resolve_codex_usage(),
        before_cutover=before_cutover,
    )
    assert pending is not None
    service_module._advance_pending_service_transaction(pending, "runtime")
    for path in paths:
        service_module._publish_pending_service_unit_generation(pending, path)
    service_module._advance_pending_service_transaction(pending, "units")
    service_module._advance_pending_service_transaction(pending, "reloaded")
    pending_current = (root / "current").lstat()
    journal_path = unit_dir / service_module.SERVICE_PENDING_V1_NAME
    journal_bytes = journal_path.read_bytes()
    units_before = {path: service_module._pending_unit_snapshot(path) for path in paths}
    state.update(enabled="enabled", active="active")
    fail_stop = True
    calls.clear()

    with pytest.raises(ServiceError) as exc_info:
        service_module._recover_pending_service_operation()

    assert isinstance(exc_info.value.__cause__, ExceptionGroup)
    assert any(
        str(error) == "timer stop failed"
        for error in exc_info.value.__cause__.exceptions
    )
    assert any(
        str(error) == "service pending transaction activation is blocked"
        for error in exc_info.value.__cause__.exceptions
    )
    current = (root / "current").lstat()
    assert (current.st_dev, current.st_ino) == (
        pending_current.st_dev,
        pending_current.st_ino,
    )
    assert {path: service_module._pending_unit_snapshot(path) for path in paths} == units_before
    assert journal_path.read_bytes() == journal_bytes
    assert [args[0] for args in calls] == ["is-enabled", "is-active", "stop"]


def test_pending_recovery_refuses_to_overwrite_foreign_unit_drift(
    tmp_path,
    monkeypatch,
):
    """Recovery must retain a unit changed outside its own pending generation."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    systemctl_calls: list[tuple[str, ...]] = []

    def systemctl(*args: str, check: bool = True):
        systemctl_calls.append(args)
        if args[:1] == ("is-enabled",):
            return subprocess.CompletedProcess(args, 1, "disabled\n", "")
        if args[:1] == ("is-active",):
            return subprocess.CompletedProcess(args, 1, "inactive\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(service_module, "_systemctl", systemctl)
    config_path = tmp_path / "config.toml"
    service_install(AppConfig(accounts=()), config_path)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    service_path = unit_dir / SERVICE_NAME
    paths = (service_path, unit_dir / TIMER_NAME)
    previous = {path: service_module._read_unit_snapshot(path) for path in paths}
    root = tmp_path / "data" / "codex-usage-service-runtime-v2"
    pending = None

    def before_cutover(new, old, staging):
        nonlocal pending
        pending = service_module._prepare_pending_service_transaction(
            unit_dir=unit_dir,
            operation="install",
            previous_units=previous,
            new_units={
                service_path: "pending service unit\n",
                unit_dir / TIMER_NAME: previous[unit_dir / TIMER_NAME] or "",
            },
            activation=("enabled", "active"),
            enable_link=None,
            root=root,
            staging=staging,
            old=old,
            new=new,
        )

    service_module._materialize_service_runtime(
        service_module._resolve_codex_usage(),
        before_cutover=before_cutover,
    )
    assert pending is not None
    service_module._advance_pending_service_transaction(pending, "runtime")
    pending_current = (root / "current").lstat()
    service_module._publish_pending_service_unit_generation(pending, service_path)
    service_module.write_private_text(
        service_path,
        "foreign unit drift\n",
        label="systemd service",
        mode=0o600,
    )
    systemctl_calls.clear()

    with pytest.raises(ServiceError) as exc_info:
        service_module._recover_pending_service_operation()

    assert isinstance(exc_info.value.__cause__, ExceptionGroup)
    assert any(
        "service pending unit changed" in str(error)
        for error in exc_info.value.__cause__.exceptions
    )
    assert service_path.read_text(encoding="utf-8") == "foreign unit drift\n"
    current = (root / "current").lstat()
    assert (current.st_dev, current.st_ino) == (
        pending_current.st_dev,
        pending_current.st_ino,
    )
    assert not any(args[:1] in {("enable",), ("start",)} for args in systemctl_calls)
    assert (unit_dir / service_module.SERVICE_PENDING_V1_NAME).exists()


def test_pending_recovery_refuses_byte_identical_foreign_old_unit_replacement(
    tmp_path,
    monkeypatch,
):
    """A foreign atomic replacement must not masquerade as the journal's old Unit."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    systemctl_calls: list[tuple[str, ...]] = []

    def systemctl(*args: str, check: bool = True):
        systemctl_calls.append(args)
        if args[:1] == ("is-enabled",):
            return subprocess.CompletedProcess(args, 1, "disabled\n", "")
        if args[:1] == ("is-active",):
            return subprocess.CompletedProcess(args, 1, "inactive\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(service_module, "_systemctl", systemctl)
    config_path = tmp_path / "config.toml"
    service_install(AppConfig(accounts=()), config_path)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    service_path = unit_dir / SERVICE_NAME
    timer_path = unit_dir / TIMER_NAME
    paths = (service_path, timer_path)
    previous = {path: service_module._read_unit_snapshot(path) for path in paths}
    root = tmp_path / "data" / "codex-usage-service-runtime-v2"
    pending = None

    def before_cutover(new, old, staging):
        nonlocal pending
        pending = service_module._prepare_pending_service_transaction(
            unit_dir=unit_dir,
            operation="install",
            previous_units=previous,
            new_units={
                service_path: "pending service unit\n",
                timer_path: previous[timer_path] or "",
            },
            activation=("enabled", "active"),
            enable_link=None,
            root=root,
            staging=staging,
            old=old,
            new=new,
        )

    service_module._materialize_service_runtime(
        service_module._resolve_codex_usage(),
        before_cutover=before_cutover,
    )
    assert pending is not None
    service_module._advance_pending_service_transaction(pending, "runtime")
    pending_current = (root / "current").lstat()
    journal_path = unit_dir / service_module.SERVICE_PENDING_V1_NAME
    journal_bytes = journal_path.read_bytes()
    staged = {
        path: unit_dir / pending.document["units"][path.name]["staging_name"]
        for path in paths
    }
    staged_bytes = {path: staged[path].read_bytes() for path in paths}
    staged_ids = {
        path: (
            staged[path].stat().st_dev,
            staged[path].stat().st_ino,
            staged[path].stat().st_mtime_ns,
        )
        for path in paths
    }
    old_service_identity = pending.document["units"][SERVICE_NAME]["old"]["identity"]
    service_module.write_private_text(
        service_path,
        previous[service_path] or "",
        label="systemd service",
        mode=0o600,
    )
    externally_replaced = service_module._pending_unit_snapshot(service_path)
    assert externally_replaced is not None
    assert externally_replaced["identity"] != old_service_identity
    systemctl_calls.clear()

    with pytest.raises(ServiceError) as exc_info:
        service_module._recover_pending_service_operation()

    assert isinstance(exc_info.value.__cause__, ExceptionGroup)
    assert any(
        "service pending unit changed" in str(error)
        for error in exc_info.value.__cause__.exceptions
    )
    assert service_module._pending_unit_snapshot(service_path) == externally_replaced
    current = (root / "current").lstat()
    assert (current.st_dev, current.st_ino) == (
        pending_current.st_dev,
        pending_current.st_ino,
    )
    assert journal_path.read_bytes() == journal_bytes
    for path in paths:
        assert staged[path].read_bytes() == staged_bytes[path]
        item = staged[path].stat()
        assert (item.st_dev, item.st_ino, item.st_mtime_ns) == staged_ids[path]
    assert systemctl_calls == []


def test_pending_recovery_accepts_only_its_journal_bound_restored_old_unit(
    tmp_path,
    monkeypatch,
):
    """A retry may recognize an old Unit only after its own durable restore binding."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(service_module, "_systemctl", _inactive_systemctl)
    config_path = tmp_path / "config.toml"
    service_install(AppConfig(accounts=()), config_path)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    service_path = unit_dir / SERVICE_NAME
    timer_path = unit_dir / TIMER_NAME
    paths = (service_path, timer_path)
    previous = {path: service_module._read_unit_snapshot(path) for path in paths}
    root = tmp_path / "data" / "codex-usage-service-runtime-v2"
    pending = None

    def before_cutover(new, old, staging):
        nonlocal pending
        pending = service_module._prepare_pending_service_transaction(
            unit_dir=unit_dir,
            operation="install",
            previous_units=previous,
            new_units={
                service_path: "pending service unit\n",
                timer_path: "pending timer unit\n",
            },
            activation=("disabled", "inactive"),
            enable_link=None,
            root=root,
            staging=staging,
            old=old,
            new=new,
        )

    service_module._materialize_service_runtime(
        service_module._resolve_codex_usage(),
        before_cutover=before_cutover,
    )
    assert pending is not None
    service_module._advance_pending_service_transaction(pending, "runtime")
    for path in paths:
        service_module._publish_pending_service_unit_generation(pending, path)
    service_module._advance_pending_service_transaction(pending, "units")
    original_write = service_module.write_private_text

    def fail_timer_restore(path, text, *, label, mode):
        if path == timer_path and text == previous[timer_path]:
            raise OSError("timer restore interrupted")
        return original_write(path, text, label=label, mode=mode)

    monkeypatch.setattr(service_module, "write_private_text", fail_timer_restore)
    with pytest.raises(ServiceError):
        service_module._recover_pending_service_operation()

    persisted = service_module._load_pending_service_operation(unit_dir)
    assert persisted is not None
    restored_service = service_module._pending_unit_snapshot(service_path)
    assert restored_service is not None
    assert (
        persisted["units"][SERVICE_NAME]["restored_old_identity"]
        == restored_service["identity"]
    )
    monkeypatch.setattr(service_module, "write_private_text", original_write)

    service_module._recover_pending_service_operation()

    assert {path: service_module._read_unit_snapshot(path) for path in paths} == previous
    assert not (unit_dir / service_module.SERVICE_PENDING_V1_NAME).exists()


def test_pending_recovery_blocks_reactivation_after_runtime_rollback_failure(
    tmp_path,
    monkeypatch,
):
    """A failed runtime rollback remains transparent and keeps the timer quiesced."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    calls: list[tuple[str, ...]] = []

    def systemctl(*args: str, check: bool = True):
        calls.append(args)
        if args[:1] == ("is-enabled",):
            return subprocess.CompletedProcess(args, 1, "disabled\n", "")
        if args[:1] == ("is-active",):
            return subprocess.CompletedProcess(args, 1, "inactive\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(service_module, "_systemctl", systemctl)
    config_path = tmp_path / "config.toml"
    service_install(AppConfig(accounts=()), config_path)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    paths = (unit_dir / SERVICE_NAME, unit_dir / TIMER_NAME)
    previous = {path: service_module._read_unit_snapshot(path) for path in paths}
    root = tmp_path / "data" / "codex-usage-service-runtime-v2"
    pending = None

    def before_cutover(new, old, staging):
        nonlocal pending
        pending = service_module._prepare_pending_service_transaction(
            unit_dir=unit_dir,
            operation="install",
            previous_units=previous,
            new_units={
                unit_dir / SERVICE_NAME: previous[unit_dir / SERVICE_NAME] or "",
                unit_dir / TIMER_NAME: previous[unit_dir / TIMER_NAME] or "",
            },
            activation=("enabled", "active"),
            enable_link=None,
            root=root,
            staging=staging,
            old=old,
            new=new,
        )

    service_module._materialize_service_runtime(
        service_module._resolve_codex_usage(),
        before_cutover=before_cutover,
    )
    assert pending is not None
    service_module._advance_pending_service_transaction(pending, "runtime")
    monkeypatch.setattr(
        service_module,
        "_recover_pending_runtime",
        lambda _runtime: (_ for _ in ()).throw(ServiceError("runtime rollback failed")),
    )
    calls.clear()

    with pytest.raises(ServiceError) as exc_info:
        service_module._recover_pending_service_operation()

    assert isinstance(exc_info.value.__cause__, ExceptionGroup)
    assert any(
        str(error) == "runtime rollback failed"
        for error in exc_info.value.__cause__.exceptions
    )
    assert any(
        str(error) == "service pending transaction activation is blocked"
        for error in exc_info.value.__cause__.exceptions
    )
    assert not any(args[:1] in {("enable",), ("start",)} for args in calls)


def test_runtime_builder_rejects_user_owned_producer_venv_interpreter(
    tmp_path,
    monkeypatch,
):
    """Would fail if Core reused a producer or user-owned Python runtime."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    producer_python = tmp_path / "producer" / "venv" / "bin" / "python"
    producer_python.parent.mkdir(parents=True)
    shutil.copyfile(sys.executable, producer_python)
    producer_python.chmod(0o700)
    monkeypatch.setattr(service_module.sys, "executable", str(producer_python))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    executable = service_module._resolve_codex_usage()

    with pytest.raises(ServiceError, match="system Python"):
        service_module._materialize_service_runtime(executable)


@pytest.mark.parametrize(
    "drift",
    ("symlink", "hardlink", "version", "interpreter", "module"),
)
def test_runtime_revalidation_fails_closed_for_attested_component_drift(
    tmp_path,
    monkeypatch,
    drift,
):
    """Would fail if a runtime component escaped its recorded binding."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    executable = service_module._resolve_codex_usage()
    runtime = service_module._materialize_service_runtime(executable)
    root = tmp_path / "data" / "codex-usage-service-runtime-v2"
    current = root / "current"
    if drift == "symlink":
        relocated = root / "relocated-current"
        current.rename(relocated)
        current.symlink_to(relocated, target_is_directory=True)
    elif drift == "hardlink":
        os.link(
            current / "bin" / "codex-usage-integration-watchdog-v2",
            current / "bin" / "watchdog-hardlink",
        )
    elif drift == "version":
        metadata = (
            current
            / "venv"
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
            / "codex_usage-0.6.539.dist-info"
            / "METADATA"
        )
        metadata.write_text("Name: codex-usage\nVersion: 0.0.0\n", encoding="utf-8")
        metadata.chmod(0o600)
    elif drift == "interpreter":
        interpreter = current / "venv" / "bin" / "python"
        interpreter.write_bytes(b"interpreter drift")
        interpreter.chmod(0o700)
    else:
        module = (
            current
            / "venv"
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
            / "codex_usage"
            / "integration_watchdog.py"
        )
        module.write_text("def main():\n    return 1\n", encoding="utf-8")
        module.chmod(0o600)

    with pytest.raises(ServiceError):
        service_module._revalidate_service_runtime_for_unit_write(runtime)


def test_runtime_watchdog_ignores_user_site_module_shadow(
    tmp_path,
    monkeypatch,
):
    """Would fail if PYTHONPATH could select a user or producer module instead."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)
    shadow_module = tmp_path / "producer-shadow" / "codex_usage"
    shadow_module.mkdir(parents=True)
    (shadow_module / "__init__.py").write_text("", encoding="utf-8")
    (shadow_module / "integration_watchdog.py").write_text(
        "def main():\n    return 37\n",
        encoding="utf-8",
    )

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    executable = service_module._resolve_codex_usage()
    runtime = service_module._materialize_service_runtime(executable)
    completed = subprocess.run(
        [
            "/usr/bin/env",
            f"PYTHONPATH={tmp_path / 'producer-shadow'}",
            str(runtime.binding.watchdog.path),
            "--config",
            str(tmp_path / "config.toml"),
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


class _BrokenInt(int):
    def __gt__(self, _other):
        raise RuntimeError("synthetic service PID comparison marker")


def test_relative_xdg_config_home_uses_default_unit_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")

    unit_directory = _unit_directory()

    assert unit_directory == home / ".config" / "systemd" / "user"
    assert unit_directory.is_dir()
    assert not (cwd / "relative-config").exists()


@pytest.mark.parametrize("operation", (service_install, service_enable))
@pytest.mark.parametrize("config_path", ("", False, 0, [], {}))
def test_service_rejects_invalid_config_path_before_side_effects(
    tmp_path, monkeypatch, operation, config_path
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    with pytest.raises(ValueError, match="config path must be a Path"):
        operation(AppConfig(accounts=()), config_path)

    assert not (tmp_path / "config").exists()


@pytest.mark.parametrize("operation", (service_install, service_enable))
def test_service_rejects_unknown_config_home_before_side_effects(
    tmp_path, monkeypatch, operation
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    with pytest.raises(ValueError, match="config path cannot be resolved"):
        operation(
            AppConfig(accounts=()),
            Path("~definitely-no-such-user-zzzz/config.toml"),
        )

    assert not (tmp_path / "config").exists()


def test_managed_service_config_path_ignores_unknown_user_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = _unit_directory()
    service_path = unit_dir / SERVICE_NAME
    service_path.write_text(
        "[Service]\n"
        f"{service_module.MANAGED_MARKER}\n"
        'ExecStart=/usr/bin/codex-usage --config "~definitely-no-such-user-zzzz/config.toml"\n',
        encoding="utf-8",
    )
    service_path.chmod(0o600)

    assert managed_service_config_path() is None


@pytest.mark.parametrize("operation", (service_install, service_enable))
@pytest.mark.parametrize("interval", ("60\nExecStart=bad", True, 59, 300.5, None, []))
def test_service_rejects_invalid_config_before_side_effects(
    tmp_path, monkeypatch, operation, interval
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    _mock_resolved_executable_without_attestation(monkeypatch, executable)
    monkeypatch.setattr(
        "codex_usage.service._systemctl",
        lambda *args, check=True: subprocess.CompletedProcess(args, 0, "", ""),
    )

    with pytest.raises(ValueError, match="interval_seconds"):
        operation(AppConfig(accounts=(), interval_seconds=interval), tmp_path / "config.toml")

    assert not (tmp_path / "config").exists()


def test_service_symlink_check_rejects_dotdot_bypass(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ServiceError, match="must not contain symlinks"):
        service_module._assert_no_symlink_ancestors(redirected / ".." / "target")


def test_service_symlink_check_scans_after_missing_segment(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ServiceError, match="must not contain symlinks"):
        service_module._assert_no_symlink_ancestors(
            tmp_path / "missing" / ".." / "redirected" / "target"
        )


def test_systemctl_rejects_oversized_output_before_process_finishes(tmp_path, monkeypatch):
    marker = tmp_path / "finished"
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        "#!/bin/sh\n"
        f"{shlex.quote(sys.executable)} -c \"import os, pathlib, sys, time; "
        f"sys.stdout.write('x' * ({service_module.SYSTEMCTL_OUTPUT_MAX_BYTES} + 1)); "
        "sys.stdout.flush(); time.sleep(2); "
        "pathlib.Path(os.environ['SYSTEMCTL_MARKER']).touch()\"\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o700)
    monkeypatch.setattr(service_module.shutil, "which", lambda name: str(fake_systemctl))
    monkeypatch.setenv("SYSTEMCTL_MARKER", str(marker))

    with pytest.raises(ServiceError, match="systemctl command failed"):
        service_module._systemctl("show", "codex-usage.service", check=False)
    time.sleep(2.2)
    assert not marker.exists()


def test_systemctl_cleanup_rejects_boolean_pid(monkeypatch):
    calls = []

    class FakeProcess:
        pid = True

        def kill(self):
            calls.append("kill")

        def wait(self, timeout=None):
            calls.append(("wait", timeout))

    monkeypatch.setattr(
        "codex_usage.service.os.killpg",
        lambda pid, signum: calls.append(("killpg", pid, signum)),
    )

    _terminate_systemctl_process(FakeProcess())

    assert calls == ["kill", ("wait", 1)]


def test_systemctl_cleanup_rejects_numeric_subclass_pid(monkeypatch):
    calls = []

    class FakeProcess:
        pid = _BrokenInt(1234)

        def kill(self):
            calls.append("kill")

        def wait(self, timeout=None):
            calls.append(("wait", timeout))

    monkeypatch.setattr(
        "codex_usage.service.os.killpg",
        lambda pid, signum: calls.append(("killpg", pid, signum)),
    )

    _terminate_systemctl_process(FakeProcess())

    assert calls == ["kill", ("wait", 1)]


def test_service_enable_renders_private_hardened_units(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    auth_home = tmp_path / "agent"
    auth_home.mkdir()
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    local_profile_dir = tmp_path / "local-profile"
    local_profile_dir.mkdir()
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(profile_dir),
        auth_json_path=str(auth_home / "auth.json"),
        backend="app-server",
    )
    local_account = Account(
        id="local",
        label="Local",
        profile_dir=str(local_profile_dir),
    )
    calls: list[tuple[str, ...]] = []

    _mock_resolved_executable_without_attestation(monkeypatch, executable)

    def fake_systemctl(*args, check=True):
        calls.append(args)
        stdout = ""
        if args[0] == "is-enabled":
            stdout = "enabled\n"
        if args[0] == "is-active" and args[1].endswith("timer"):
            stdout = "active\n"
        if args[0] == "show" and args[1].endswith("timer"):
            stdout = (
                "SubState=waiting\n"
                "NextElapseUSecMonotonic=15h\n"
                "NextElapseUSecRealtime=\n"
            )
        elif args[0] == "show":
            stdout = (
                "Result=success\n"
                "ExecMainStatus=0\n"
                "ExecMainCode=1\n"
                "ExecMainStartTimestamp=now\n"
                "ExecMainExitTimestamp=later\n"
            )
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    result = service_enable(
        AppConfig(accounts=(account, local_account), interval_seconds=420),
        tmp_path / "config" / "codex-usage" / "config.toml",
    )

    service_path = tmp_path / "config" / "systemd" / "user" / "codex-usage.service"
    timer_path = tmp_path / "config" / "systemd" / "user" / "codex-usage.timer"
    service = service_path.read_text(encoding="utf-8")
    timer = timer_path.read_text(encoding="utf-8")
    assert "ExecStart=" in service
    assert "Type=oneshot" in service
    assert (
        str(tmp_path / "data" / "codex-usage-service-runtime-v2" / "current" / "bin")
        in service
    )
    assert "--config" not in service
    assert f'Environment="XDG_DATA_HOME={tmp_path / "data"}"' in service
    assert f'Environment="XDG_STATE_HOME={tmp_path / ".local" / "state"}"' in service
    assert (
        f'ReadWritePaths="{tmp_path / ".local" / "state" / "codex-usage" / "integration"}"'
        in service
    )
    assert "ProtectSystem=strict" in service
    assert (
        f"TimeoutStartSec={EXPECTED_INTEGRATION_WATCHDOG_SYSTEMD_TIMEOUT_SECONDS}"
        in service
    )
    assert "RuntimeMaxSec=" not in service
    assert "TimeoutStopSec=15" in service
    assert "KillMode=mixed" in service
    assert "MemoryMax=1G" in service
    assert "TasksMax=256" in service
    assert "OOMPolicy=kill" in service
    assert "Restart=no" in service
    assert f'ReadWritePaths="{profile_dir}"' not in service
    assert f'ReadWritePaths="{local_profile_dir}"' not in service
    assert f'ReadWritePaths="{auth_home}"' not in service
    assert f'ReadWritePaths="{tmp_path / "config" / "codex-usage"}"' not in service
    assert "OnActiveSec=1min" in timer
    assert "OnBootSec=" not in timer
    assert "OnUnitActiveSec=420s" in timer
    assert oct(service_path.stat().st_mode & 0o777) == "0o600"
    assert ("enable", "codex-usage.timer") in calls
    assert ("restart", "codex-usage.timer") in calls
    assert result["installed"] is True
    assert result["enabled"] is True
    assert result["active"] is True
    assert result["timer_scheduled"] is True
    assert result["timer_substate"] == "waiting"
    assert result["service_result"] == "success"
    assert result["service_exit_status"] == "0"
    assert result["service_exit_code"] == "exited"
    assert managed_service_config_path() is None


def test_service_enable_removes_new_units_when_activation_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    calls: list[tuple[str, ...]] = []

    def fail_enable(*args, check=True):
        calls.append(args)
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 4, "not-found\n", "")
        if args[0] == "is-active":
            return subprocess.CompletedProcess(args, 0, "inactive\n", "")
        if args[0] == "enable":
            raise ServiceError("systemctl enable failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_enable)

    with pytest.raises(ServiceError, match="systemctl enable failed"):
        service_enable(AppConfig(accounts=()), tmp_path / "config.toml")

    unit_dir = tmp_path / "config" / "systemd" / "user"
    assert not (unit_dir / "codex-usage.service").exists()
    assert not (unit_dir / "codex-usage.timer").exists()
    assert ("disable", TIMER_NAME) in calls


def test_service_enable_cleans_partial_enable_after_command_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    cleaned = []

    def fail_after_enable_started(*args, check=True):
        if args == ("is-enabled", TIMER_NAME):
            return subprocess.CompletedProcess(args, 1, "disabled\n", "")
        if args == ("is-active", TIMER_NAME):
            return subprocess.CompletedProcess(args, 1, "inactive\n", "")
        if args == ("enable", TIMER_NAME):
            raise ServiceError("systemctl enable failed after link creation")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_after_enable_started)
    monkeypatch.setattr(
        "codex_usage.service._cleanup_managed_timer_enable_link",
        lambda: cleaned.append(True),
    )

    with pytest.raises(ServiceError, match="after link creation"):
        service_enable(AppConfig(accounts=()), tmp_path / "config.toml")

    assert cleaned == [True]


def test_service_install_serializes_concurrent_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    first_reload_entered = threading.Event()
    second_reload_entered = threading.Event()
    release_first_reload = threading.Event()
    reload_count = 0
    reload_lock = threading.Lock()

    def block_first_reload(*args, check=True):
        nonlocal reload_count
        if args == ("is-enabled", TIMER_NAME):
            return subprocess.CompletedProcess(args, 1, "not-found\n", "")
        if args == ("is-active", TIMER_NAME):
            return subprocess.CompletedProcess(args, 3, "inactive\n", "")
        if args == ("daemon-reload",):
            with reload_lock:
                reload_count += 1
                current_reload = reload_count
            if current_reload == 1:
                first_reload_entered.set()
                if not release_first_reload.wait(2):
                    raise AssertionError("first reload was not released")
            elif current_reload == 2:
                second_reload_entered.set()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", block_first_reload)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            service_install,
            AppConfig(accounts=(), interval_seconds=300),
            tmp_path / "config.toml",
        )
        assert first_reload_entered.wait(1)
        second = executor.submit(
            service_install,
            AppConfig(accounts=(), interval_seconds=600),
            tmp_path / "config.toml",
        )
        try:
            assert not second_reload_entered.wait(0.2)
        finally:
            release_first_reload.set()
        assert first.result() == {
            "installed": True,
            "service": SERVICE_NAME,
            "timer": TIMER_NAME,
        }
        assert second.result() == {
            "installed": True,
            "service": SERVICE_NAME,
            "timer": TIMER_NAME,
        }


def test_service_operation_lock_has_bounded_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("codex_usage.service.SERVICE_OPERATION_LOCK_TIMEOUT_SECONDS", 0)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    lock_path = unit_dir / service_module.SERVICE_OPERATION_LOCK_NAME

    def try_lock():
        with service_module._service_operation_lock():
            return True

    with service_module.private_path_lock(lock_path, timeout_seconds=0, label="held lock"):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(try_lock)
            with pytest.raises(TimeoutError, match="already in use"):
                future.result()


def test_service_enable_removes_first_install_enable_link_after_restart_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    wants_dir = unit_dir / "timers.target.wants"
    timer_path = unit_dir / TIMER_NAME
    calls: list[tuple[str, ...]] = []

    def fail_restart(*args, check=True):
        calls.append(args)
        if args == ("is-enabled", TIMER_NAME):
            return subprocess.CompletedProcess(args, 4, "not-found\n", "")
        if args == ("is-active", TIMER_NAME):
            return subprocess.CompletedProcess(args, 3, "inactive\n", "")
        if args == ("enable", TIMER_NAME):
            wants_dir.mkdir(parents=True, exist_ok=True)
            (wants_dir / TIMER_NAME).symlink_to(timer_path)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == ("disable", TIMER_NAME):
            (wants_dir / TIMER_NAME).unlink(missing_ok=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == ("restart", TIMER_NAME):
            raise ServiceError("systemctl restart failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_restart)

    with pytest.raises(ServiceError, match="systemctl restart failed"):
        service_enable(AppConfig(accounts=()), tmp_path / "config.toml")

    assert ("disable", TIMER_NAME) in calls
    assert not (wants_dir / TIMER_NAME).is_symlink()


def test_service_enable_removes_enable_link_when_disable_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    wants_dir = unit_dir / "timers.target.wants"
    timer_path = unit_dir / TIMER_NAME

    def fail_disable(*args, check=True):
        if args == ("is-enabled", TIMER_NAME):
            return subprocess.CompletedProcess(args, 4, "not-found\n", "")
        if args == ("is-active", TIMER_NAME):
            return subprocess.CompletedProcess(args, 3, "inactive\n", "")
        if args == ("enable", TIMER_NAME):
            wants_dir.mkdir(parents=True, exist_ok=True)
            (wants_dir / TIMER_NAME).symlink_to(timer_path)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == ("disable", TIMER_NAME):
            raise ServiceError("systemctl disable failed")
        if args == ("restart", TIMER_NAME):
            raise ServiceError("systemctl restart failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_disable)

    with pytest.raises(ServiceError) as exc:
        service_enable(AppConfig(accounts=()), tmp_path / "config.toml")

    assert isinstance(exc.value.__cause__, ExceptionGroup)
    recovery_error = next(
        error
        for error in exc.value.__cause__.exceptions
        if str(error) == "could not recover service pending transaction"
    )
    assert isinstance(recovery_error.__cause__, ExceptionGroup)
    assert any(
        str(error) == "systemctl disable failed"
        for error in recovery_error.__cause__.exceptions
    )
    assert not (wants_dir / TIMER_NAME).is_symlink()


def test_cleanup_managed_timer_link_refuses_foreign_target(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    wants_dir = unit_dir / "timers.target.wants"
    wants_dir.mkdir(parents=True)
    foreign = tmp_path / "foreign.timer"
    foreign.write_text("foreign\n", encoding="utf-8")
    link = wants_dir / TIMER_NAME
    link.symlink_to(foreign)

    with pytest.raises(ServiceError, match="foreign systemd enable link"):
        service_module._cleanup_managed_timer_enable_link()

    assert link.is_symlink()


def test_cleanup_managed_timer_link_wraps_runtime_resolution_errors(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    wants_dir = unit_dir / "timers.target.wants"
    wants_dir.mkdir(parents=True)
    timer_path = unit_dir / TIMER_NAME
    timer_path.write_text("managed\n", encoding="utf-8")
    link = wants_dir / TIMER_NAME
    link.symlink_to(timer_path)
    original_resolve = service_module.Path.resolve

    def fail_link_resolution(path, strict=False):
        if path == link:
            raise RuntimeError("synthetic symlink resolution failure")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(service_module.Path, "resolve", fail_link_resolution)

    with pytest.raises(ServiceError, match="could not resolve systemd enable link"):
        service_module._cleanup_managed_timer_enable_link()

    assert link.is_symlink()


def test_cleanup_managed_timer_link_refuses_regular_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    wants_dir = tmp_path / "config" / "systemd" / "user" / "timers.target.wants"
    wants_dir.mkdir(parents=True)
    link = wants_dir / TIMER_NAME
    link.write_text("foreign\n", encoding="utf-8")

    with pytest.raises(ServiceError, match="non-symlink"):
        service_module._cleanup_managed_timer_enable_link()

    assert link.is_file()


def test_cleanup_managed_timer_link_refuses_symlinked_wants_directory(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (unit_dir / "timers.target.wants").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ServiceError, match="must not contain symlinks"):
        service_module._cleanup_managed_timer_enable_link()


def test_service_enable_restores_previous_units_when_restart_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    old_service = "old service\nX-Codex-Usage-Managed=true\n"
    old_timer = "old timer\nX-Codex-Usage-Managed=true\n"
    service_path.write_text(old_service, encoding="utf-8")
    timer_path.write_text(old_timer, encoding="utf-8")
    restart_attempts = 0

    def fail_restart(*args, check=True):
        nonlocal restart_attempts
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 0, "enabled\n", "")
        if args[0] == "is-active":
            return subprocess.CompletedProcess(args, 0, "active\n", "")
        if args[0] == "restart":
            restart_attempts += 1
            if restart_attempts == 1:
                raise ServiceError("systemctl restart failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_restart)

    with pytest.raises(ServiceError, match="systemctl restart failed"):
        service_enable(AppConfig(accounts=()), tmp_path / "config.toml")

    assert service_path.read_text(encoding="utf-8") == old_service
    assert timer_path.read_text(encoding="utf-8") == old_timer


def test_service_enable_restores_disabled_inactive_state_after_partial_enable(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name in (SERVICE_NAME, TIMER_NAME):
        (unit_dir / name).write_text(
            "old unit\nX-Codex-Usage-Managed=true\n",
            encoding="utf-8",
        )
    calls: list[tuple[str, ...]] = []

    def fail_restart(*args, check=True):
        calls.append(args)
        if args == ("is-enabled", TIMER_NAME):
            return subprocess.CompletedProcess(args, 0, "disabled\n", "")
        if args == ("is-active", TIMER_NAME):
            return subprocess.CompletedProcess(args, 0, "inactive\n", "")
        if args == ("restart", TIMER_NAME):
            raise ServiceError("systemctl restart failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_restart)

    with pytest.raises(ServiceError, match="systemctl restart failed"):
        service_enable(AppConfig(accounts=()), tmp_path / "config.toml")

    assert ("disable", TIMER_NAME) in calls
    assert ("stop", TIMER_NAME) in calls


def test_restore_systemd_activation_attempts_both_steps_after_failure(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fail_systemctl(*args, check=True):
        calls.append(args)
        raise OSError(f"{args[0]} failed")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_systemctl)

    with pytest.raises(ExceptionGroup) as exc:
        service_module._restore_systemd_activation(("disabled", "inactive"))

    assert calls == [("disable", TIMER_NAME), ("stop", TIMER_NAME)]
    assert [str(error) for error in exc.value.exceptions] == [
        "disable failed",
        "stop failed",
    ]


def test_restore_systemd_activation_reports_unknown_states(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def record_systemctl(*args, check=True):
        calls.append(args)

    monkeypatch.setattr("codex_usage.service._systemctl", record_systemctl)

    with pytest.raises(ExceptionGroup) as exc:
        service_module._restore_systemd_activation(("masked", "failed"))

    assert calls == []
    assert [str(error) for error in exc.value.exceptions] == [
        "cannot restore systemd enabled state: masked",
        "cannot restore systemd active state: failed",
    ]


def test_reject_home_write_path_wraps_runtime_resolution_errors(monkeypatch):
    def fail_resolve(_path, strict=False):
        raise RuntimeError("synthetic resolution failure")

    monkeypatch.setattr(service_module.Path, "resolve", fail_resolve)

    with pytest.raises(ServiceError, match="profile cannot be resolved"):
        service_module._reject_home_write_path(Path("/tmp/profile"), label="profile")


def test_config_rejects_home_as_account_profile_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(tmp_path),
        auth_json_path=str(tmp_path / "agent" / "auth.json"),
        backend="app-server",
    )

    with pytest.raises(ValueError, match="profile dir must not be a protected directory"):
        service_module._validate_config(AppConfig(accounts=(account,)))


def test_publisher_service_does_not_render_account_writable_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(service_module, "_systemctl", _inactive_systemctl)
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    auth_home = tmp_path / "agent"
    auth_home.mkdir()
    auth_json_path = auth_home / "auth.json"
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(profile_dir),
        auth_json_path=str(auth_json_path),
        backend="app-server",
    )

    service_install(
        AppConfig(accounts=(account,)),
        tmp_path / "config" / "codex-usage" / "config.toml",
    )

    service_text = (
        tmp_path / "config" / "systemd" / "user" / SERVICE_NAME
    ).read_text(encoding="utf-8")
    assert str(profile_dir) not in service_text
    assert str(auth_home) not in service_text


def test_publisher_service_escapes_runtime_percent_specifiers_without_config_path(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data%home"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    profile_dir = tmp_path / "profile%user"
    profile_dir.mkdir()
    auth_home = tmp_path / "agent%user"
    auth_home.mkdir()
    account = Account(
        id="work",
        label="Work",
        profile_dir=str(profile_dir),
        auth_json_path=str(auth_home / "auth.json"),
        backend="direct",
    )

    def fake_systemctl(*args, check=True):
        stdout = ""
        if args[0] == "is-enabled":
            stdout = "enabled\n"
        if args[0] == "is-active" and args[1].endswith("timer"):
            stdout = "active\n"
        if args[0] == "show" and args[1].endswith("timer"):
            stdout = (
                "SubState=waiting\n"
                "NextElapseUSecMonotonic=15h\n"
                "NextElapseUSecRealtime=\n"
            )
        elif args[0] == "show":
            stdout = "Result=success\nExecMainStatus=0\nExecMainCode=1\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)
    config_path = tmp_path / "config" / "codex%usage" / "config.toml"

    service_enable(AppConfig(accounts=(account,), interval_seconds=300), config_path)

    service_path = tmp_path / "config" / "systemd" / "user" / "codex-usage.service"
    service = service_path.read_text(encoding="utf-8")
    assert "%%" in service
    assert str(config_path) not in service
    assert str(profile_dir) not in service
    assert str(auth_home) not in service
    assert managed_service_config_path() is None


def test_service_uninstall_refuses_unmanaged_unit(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    service_path.write_text("[Service]\nType=oneshot\n", encoding="utf-8")
    timer_path.write_text("[Timer]\n", encoding="utf-8")
    service_path.chmod(0o600)
    timer_path.chmod(0o600)
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    with pytest.raises(ServiceError, match="unmanaged"):
        service_uninstall()

    assert service_path.exists()
    assert timer_path.exists()
    assert calls == []


def test_service_uninstall_does_not_stop_foreign_unit_without_managed_files(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "active\n", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    assert service_uninstall() == {"installed": False, "enabled": False, "active": False}
    assert calls == []


def test_service_status_hides_foreign_state_without_managed_units(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "active\n", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    result = service_status()

    assert result["installed"] is False
    assert result["enabled"] is False
    assert result["active"] is False
    assert result["service_active"] is False
    assert calls == []


def test_service_status_rejects_elapsed_timer_without_next_run(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name in ("codex-usage.service", "codex-usage.timer"):
        (unit_dir / name).write_text(
            "[Unit]\nX-Codex-Usage-Managed=true\n",
            encoding="utf-8",
        )

    def fake_systemctl(*args, check=True):
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 0, "enabled\n", "")
        if args[0] == "is-active" and args[1].endswith("timer"):
            return subprocess.CompletedProcess(args, 0, "active\n", "")
        if args[0] == "is-active":
            return subprocess.CompletedProcess(args, 0, "inactive\n", "")
        if args[0] == "show" and args[1].endswith("timer"):
            return subprocess.CompletedProcess(
                args,
                0,
                "SubState=elapsed\nNextElapseUSecMonotonic=infinity\nNextElapseUSecRealtime=\n",
                "",
            )
        return subprocess.CompletedProcess(
            args,
            0,
            "Result=success\nExecMainStatus=0\nExecMainCode=1\n",
            "",
        )

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    result = service_status()

    assert result["active"] is False
    assert result["timer_scheduled"] is False
    assert result["timer_substate"] == "elapsed"


def test_service_disable_refuses_unmanaged_unit_without_stopping_it(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "codex-usage.timer").write_text("[Timer]\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "active\n", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    with pytest.raises(ServiceError, match="unmanaged"):
        service_disable()
    assert calls == []


def test_service_disable_skips_mutation_without_managed_units(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "active\n", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    result = service_disable()

    assert result["installed"] is False
    assert all(args[:1] != ("disable",) for args in calls)


def test_service_uninstall_keeps_units_when_disable_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    service_path.write_text("X-Codex-Usage-Managed=true\n", encoding="utf-8")
    timer_path.write_text("X-Codex-Usage-Managed=true\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    disable_attempts = 0

    def fake_systemctl(*args, check=True):
        nonlocal disable_attempts
        calls.append(args)
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 0, "disabled\n", "")
        if args[0] == "is-active":
            return subprocess.CompletedProcess(args, 0, "inactive\n", "")
        if args[0] == "disable" and check:
            disable_attempts += 1
            if disable_attempts == 1:
                raise ServiceError("systemctl disable failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)

    with pytest.raises(ServiceError, match="systemctl disable failed"):
        service_uninstall()

    assert service_path.exists()
    assert timer_path.exists()


def test_service_uninstall_uses_unlocked_disable_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name in (SERVICE_NAME, TIMER_NAME):
        (unit_dir / name).write_text(
            "X-Codex-Usage-Managed=true\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "codex_usage.service._service_disable_unlocked", lambda: {}
    )
    monkeypatch.setattr(
        "codex_usage.service.service_disable",
        lambda: pytest.fail("nested public service_disable call"),
    )
    monkeypatch.setattr(
        "codex_usage.service._systemd_activation_snapshot",
        lambda: ("disabled", "inactive"),
    )
    monkeypatch.setattr(
        "codex_usage.service._systemctl",
        lambda *args, check=True: subprocess.CompletedProcess(args, 0, "", ""),
    )

    assert service_uninstall() == {
        "installed": False,
        "enabled": False,
        "active": False,
    }


def test_service_uninstall_restores_units_when_timer_delete_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    old_service = "old service\nX-Codex-Usage-Managed=true\n"
    old_timer = "old timer\nX-Codex-Usage-Managed=true\n"
    service_path.write_text(old_service, encoding="utf-8")
    timer_path.write_text(old_timer, encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args, check=True):
        calls.append(args)
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 0, "enabled\n", "")
        if args[0] == "is-active":
            return subprocess.CompletedProcess(args, 0, "active\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fake_systemctl)
    original_unlink = Path.unlink

    def fail_timer_unlink(path, *args, **kwargs):
        if path == timer_path:
            raise OSError("simulated timer delete failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_timer_unlink)

    with pytest.raises(OSError, match="simulated timer delete failure"):
        service_uninstall()

    assert service_path.read_text(encoding="utf-8") == old_service
    assert timer_path.read_text(encoding="utf-8") == old_timer
    assert ("enable", "codex-usage.timer") in calls
    assert ("start", "codex-usage.timer") in calls


def test_service_install_refuses_unmanaged_unit_without_overwriting(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    service_path.write_text("[Service]\nType=oneshot\n", encoding="utf-8")
    timer_path.write_text(
        "[Unit]\nX-Codex-Usage-Managed=true\n",
        encoding="utf-8",
    )
    service_path.chmod(0o600)
    timer_path.chmod(0o600)
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    _mock_resolved_executable_without_attestation(monkeypatch, executable)
    monkeypatch.setattr(
        "codex_usage.service._systemctl",
        lambda *args, check=True: subprocess.CompletedProcess(args, 0, "", ""),
    )

    with pytest.raises(ServiceError, match="unmanaged"):
        service_install(AppConfig(accounts=(), interval_seconds=300), tmp_path / "config.toml")

    assert service_path.read_text(encoding="utf-8") == "[Service]\nType=oneshot\n"
    assert timer_path.read_text(encoding="utf-8") == "[Unit]\nX-Codex-Usage-Managed=true\n"


def test_service_install_rolls_back_new_units_when_timer_write_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    codex_usage, watchdog, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(service_module, "_systemctl", _inactive_systemctl)
    def fail_timer_write(path):
        if path.name == "codex-usage.timer":
            raise OSError("simulated timer write failure")

    monkeypatch.setattr(
        "codex_usage.service._after_pending_service_unit_cutover", fail_timer_write
    )

    with pytest.raises(OSError, match="simulated timer write failure"):
        service_install(AppConfig(accounts=(), interval_seconds=300), tmp_path / "config.toml")

    unit_dir = tmp_path / "config" / "systemd" / "user"
    assert not (unit_dir / "codex-usage.service").exists()
    assert not (unit_dir / "codex-usage.timer").exists()


def test_service_install_rolls_back_baseexception_after_service_write(
    tmp_path,
    monkeypatch,
):
    """Would fail if BaseException bypassed the unit write transaction rollback."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _prepare_record_bound_service_install(tmp_path, monkeypatch)
    monkeypatch.setattr(service_module, "_systemctl", _inactive_systemctl)
    writes = 0

    def interrupt_after_service_write(path):
        nonlocal writes
        if path.name == SERVICE_NAME and writes == 0:
            writes += 1
            raise KeyboardInterrupt("service write interrupted")

    monkeypatch.setattr(
        "codex_usage.service._after_pending_service_unit_cutover",
        interrupt_after_service_write,
    )

    with pytest.raises(KeyboardInterrupt, match="service write interrupted"):
        service_install(AppConfig(accounts=(), interval_seconds=300), tmp_path / "config.toml")

    unit_dir = tmp_path / "config" / "systemd" / "user"
    assert not (unit_dir / SERVICE_NAME).exists()
    assert not (unit_dir / TIMER_NAME).exists()


def test_service_install_reports_partial_units_when_baseexception_rollback_fails(
    tmp_path,
    monkeypatch,
):
    """Would fail if a rollback failure hid a partial dual-unit write."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _prepare_record_bound_service_install(tmp_path, monkeypatch)
    monkeypatch.setattr(service_module, "_systemctl", _inactive_systemctl)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    service_path = unit_dir / SERVICE_NAME

    def interrupt_after_service_write(path):
        if path.name == SERVICE_NAME:
            raise KeyboardInterrupt("service write interrupted")

    original_unlink = Path.unlink

    def fail_service_rollback_unlink(path, *args, **kwargs):
        if path == service_path:
            raise OSError("service rollback unlink failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(
        "codex_usage.service._after_pending_service_unit_cutover",
        interrupt_after_service_write,
    )
    monkeypatch.setattr(Path, "unlink", fail_service_rollback_unlink)

    with pytest.raises(service_module.ServicePartialInstallError) as exc_info:
        service_install(AppConfig(accounts=(), interval_seconds=300), tmp_path / "config.toml")

    assert exc_info.value.partial_units == (service_path,)
    assert service_path.exists()
    assert not (unit_dir / TIMER_NAME).exists()


def test_service_install_rolls_back_baseexception_after_timer_write(
    tmp_path,
    monkeypatch,
):
    """Would fail if BaseException after second write left a partial service+timer."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _prepare_record_bound_service_install(tmp_path, monkeypatch)
    monkeypatch.setattr(service_module, "_systemctl", _inactive_systemctl)
    def interrupt_after_timer_write(path):
        if path.name == TIMER_NAME:
            raise SystemExit("timer write interrupted")

    monkeypatch.setattr(
        "codex_usage.service._after_pending_service_unit_cutover",
        interrupt_after_timer_write,
    )

    with pytest.raises(SystemExit, match="timer write interrupted"):
        service_install(AppConfig(accounts=(), interval_seconds=300), tmp_path / "config.toml")

    unit_dir = tmp_path / "config" / "systemd" / "user"
    assert not (unit_dir / SERVICE_NAME).exists()
    assert not (unit_dir / TIMER_NAME).exists()


def test_service_install_restores_existing_units_when_timer_write_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _prepare_record_bound_service_install(tmp_path, monkeypatch)
    monkeypatch.setattr(service_module, "_systemctl", _inactive_systemctl)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    old_service = "old service\nX-Codex-Usage-Managed=true\n"
    old_timer = "old timer\nX-Codex-Usage-Managed=true\n"
    service_path.write_text(old_service, encoding="utf-8")
    timer_path.write_text(old_timer, encoding="utf-8")
    timer_attempts = 0

    def fail_after_timer_write(path):
        nonlocal timer_attempts
        if path.name == "codex-usage.timer" and timer_attempts == 0:
            timer_attempts += 1
            raise OSError("simulated timer fsync failure")

    monkeypatch.setattr(
        "codex_usage.service._after_pending_service_unit_cutover",
        fail_after_timer_write,
    )

    with pytest.raises(OSError, match="simulated timer fsync failure"):
        service_install(AppConfig(accounts=(), interval_seconds=300), tmp_path / "config.toml")

    assert service_path.read_text(encoding="utf-8") == old_service
    assert timer_path.read_text(encoding="utf-8") == old_timer


def test_service_install_aggregates_install_and_restore_failures(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _prepare_record_bound_service_install(tmp_path, monkeypatch)
    monkeypatch.setattr(service_module, "_systemctl", _inactive_systemctl)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    service_path.write_text("old service\nX-Codex-Usage-Managed=true\n", encoding="utf-8")
    timer_path.write_text("old timer\nX-Codex-Usage-Managed=true\n", encoding="utf-8")
    original_write = service_module.write_private_text

    def fail_timer_restore(path, text, *, label, mode=0o600):
        if path.name == "codex-usage.timer":
            raise OSError("timer restore failed")
        return original_write(path, text, label=label, mode=mode)

    monkeypatch.setattr("codex_usage.service.write_private_text", fail_timer_restore)
    monkeypatch.setattr(
        "codex_usage.service._after_pending_service_unit_cutover",
        lambda path: (
            (_ for _ in ()).throw(OSError("timer write failed"))
            if path.name == TIMER_NAME
            else None
        ),
    )

    with pytest.raises(ServiceError) as exc:
        service_install(AppConfig(accounts=(), interval_seconds=300), tmp_path / "config.toml")

    assert isinstance(exc.value.__cause__, ExceptionGroup)
    rollback_errors = exc.value.__cause__.exceptions
    assert [str(error) for error in rollback_errors] == [
        "timer write failed",
        "could not recover service pending transaction",
    ]
    assert isinstance(rollback_errors[1].__cause__, ExceptionGroup)
    assert [str(error) for error in rollback_errors[1].__cause__.exceptions] == [
        "timer restore failed",
        "service pending transaction activation is blocked",
    ]


def test_service_install_preserves_both_unit_restore_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _prepare_record_bound_service_install(tmp_path, monkeypatch)
    monkeypatch.setattr(service_module, "_systemctl", _inactive_systemctl)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    service_path.write_text("old service\nX-Codex-Usage-Managed=true\n", encoding="utf-8")
    timer_path.write_text("old timer\nX-Codex-Usage-Managed=true\n", encoding="utf-8")
    original_write = service_module.write_private_text

    def fail_both_restores(path, text, *, label, mode=0o600):
        if path == service_path:
            raise OSError("service restore failed")
        if path == timer_path:
            raise OSError("timer restore failed")
        return original_write(path, text, label=label, mode=mode)

    monkeypatch.setattr("codex_usage.service.write_private_text", fail_both_restores)
    monkeypatch.setattr(
        "codex_usage.service._after_pending_service_unit_cutover",
        lambda path: (
            (_ for _ in ()).throw(OSError("timer write failed"))
            if path.name == TIMER_NAME
            else None
        ),
    )

    with pytest.raises(ServiceError) as exc:
        service_install(AppConfig(accounts=(), interval_seconds=300), tmp_path / "config.toml")

    rollback_errors = exc.value.__cause__.exceptions
    assert [str(error) for error in rollback_errors[0:1]] == ["timer write failed"]
    assert str(rollback_errors[1]) == "could not recover service pending transaction"
    assert isinstance(rollback_errors[1].__cause__, ExceptionGroup)
    recovery_errors = rollback_errors[1].__cause__.exceptions
    assert len(recovery_errors) == 2
    assert isinstance(recovery_errors[0], ExceptionGroup)
    assert [str(error) for error in recovery_errors[0].exceptions] == [
        "service restore failed",
        "timer restore failed",
    ]
    assert str(recovery_errors[1]) == "service pending transaction activation is blocked"


def test_service_enable_groups_activation_and_pending_recovery_failures(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    receipt = service_module._ServiceInstallReceipt(
        result={"installed": True},
        runtime=SimpleNamespace(),
        pending=SimpleNamespace(),
    )
    monkeypatch.setattr(
        "codex_usage.service._service_install_unlocked",
        lambda *_args, **_kwargs: receipt,
    )

    def fail_systemctl(*args, check=True):
        if args == ("enable", TIMER_NAME):
            raise OSError("activation enable failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_systemctl)
    monkeypatch.setattr(
        "codex_usage.service._recover_pending_service_operation",
        lambda: (_ for _ in ()).throw(ServiceError("pending recovery failed")),
    )

    with pytest.raises(ServiceError) as exc:
        service_module._service_enable_unlocked(
            AppConfig(accounts=()), tmp_path / "config.toml"
        )

    assert isinstance(exc.value.__cause__, ExceptionGroup)
    assert [str(error) for error in exc.value.__cause__.exceptions] == [
        "activation enable failed",
        "pending recovery failed",
    ]


def test_service_uninstall_aggregates_all_rollback_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name in ("codex-usage.service", "codex-usage.timer"):
        (unit_dir / name).write_text(
            "X-Codex-Usage-Managed=true\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "codex_usage.service._systemd_activation_snapshot",
        lambda: ("enabled", "active"),
    )

    def fail_systemctl(*args, check=True):
        if args == ("disable", "--now", TIMER_NAME):
            raise OSError("service disable failed")
        raise OSError(f"rollback systemctl failed: {args[0]}")

    def fail_unit_restore(*_args):
        raise OSError("unit restore failed")

    def fail_activation_restore(*_args):
        raise OSError("activation restore failed")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_systemctl)
    monkeypatch.setattr("codex_usage.service._restore_unit_snapshot", fail_unit_restore)
    monkeypatch.setattr(
        "codex_usage.service._restore_systemd_activation", fail_activation_restore
    )

    with pytest.raises(ServiceError) as exc:
        service_uninstall()

    assert isinstance(exc.value.__cause__, ExceptionGroup)
    assert [str(error) for error in exc.value.__cause__.exceptions] == [
        "service disable failed",
        "unit restore failed",
        "rollback systemctl failed: daemon-reload",
        "activation restore failed",
    ]


def test_service_install_reloads_systemd_after_daemon_reload_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _prepare_record_bound_service_install(tmp_path, monkeypatch)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service_path = unit_dir / "codex-usage.service"
    timer_path = unit_dir / "codex-usage.timer"
    old_service = "old service\nX-Codex-Usage-Managed=true\n"
    old_timer = "old timer\nX-Codex-Usage-Managed=true\n"
    service_path.write_text(old_service, encoding="utf-8")
    timer_path.write_text(old_timer, encoding="utf-8")
    reload_calls = 0

    def fail_once(*args, check=True):
        nonlocal reload_calls
        if args == ("is-enabled", TIMER_NAME):
            return subprocess.CompletedProcess(args, 1, "disabled\n", "")
        if args == ("is-active", TIMER_NAME):
            return subprocess.CompletedProcess(args, 1, "inactive\n", "")
        if args == ("daemon-reload",):
            reload_calls += 1
            if reload_calls == 1:
                raise ServiceError("systemctl daemon-reload failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_usage.service._systemctl", fail_once)

    with pytest.raises(ServiceError, match="systemctl daemon-reload failed"):
        service_install(AppConfig(accounts=(), interval_seconds=300), tmp_path / "config.toml")

    assert reload_calls == 2
    assert service_path.read_text(encoding="utf-8") == old_service
    assert timer_path.read_text(encoding="utf-8") == old_timer


def test_service_install_restricts_existing_unit_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit_dir.chmod(0o755)
    executable = tmp_path / "bin" / "codex-usage"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    _mock_resolved_executable_without_attestation(monkeypatch, executable)
    monkeypatch.setattr(
        "codex_usage.service._systemctl",
        lambda *args, check=True: subprocess.CompletedProcess(args, 0, "", ""),
    )

    service_install(AppConfig(accounts=()), tmp_path / "config.toml")

    assert oct(unit_dir.stat().st_mode & 0o777) == "0o700"


def test_unit_directory_binds_mode_change_to_existing_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit_dir.chmod(0o755)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    original_chmod = Path.chmod

    def replace_target_before_path_chmod(path, mode):
        if path == unit_dir:
            unit_dir.rmdir()
            unit_dir.symlink_to(outside, target_is_directory=True)
        return original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", replace_target_before_path_chmod)
    _unit_directory(create=False)

    assert unit_dir.is_dir() and not unit_dir.is_symlink()
    assert unit_dir.stat().st_mode & 0o777 == 0o700
    assert outside.stat().st_mode & 0o777 == 0o755


def test_service_install_rejects_symlinked_config_home(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    config_home = tmp_path / "config"
    config_home.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    with pytest.raises(ServiceError, match="must not contain symlinks"):
        service_install(AppConfig(accounts=()), tmp_path / "config.toml")

    assert not (outside / "systemd" / "user").exists()


def test_service_enable_reports_pending_recovery_failure(monkeypatch):
    monkeypatch.setattr(service_module, "_unit_directory", lambda: Path("/tmp/units"))
    monkeypatch.setattr(service_module, "_validate_existing_managed_units", lambda _path: None)
    receipt = service_module._ServiceInstallReceipt(
        result={"installed": True},
        runtime=SimpleNamespace(),
        pending=SimpleNamespace(),
    )
    monkeypatch.setattr(
        service_module,
        "_service_install_unlocked",
        lambda *_args, **_kwargs: receipt,
    )
    monkeypatch.setattr(
        service_module,
        "_recover_pending_service_operation",
        lambda: (_ for _ in ()).throw(ServiceError("pending recovery failed")),
    )

    def systemctl(*args, check=True):
        if args == ("restart", TIMER_NAME):
            raise ServiceError("restart failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service_module, "_systemctl", systemctl)

    with pytest.raises(ServiceError, match="could not roll back service activation") as exc:
        service_module._service_enable_unlocked(AppConfig(accounts=()), Path("/tmp/config.toml"))

    assert isinstance(exc.value.__cause__, ExceptionGroup)
    assert [str(error) for error in exc.value.__cause__.exceptions] == [
        "restart failed",
        "pending recovery failed",
    ]


def test_service_install_collects_reload_rollback_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    executable = tmp_path / "codex-usage"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    _mock_resolved_executable_without_attestation(monkeypatch, executable)
    monkeypatch.setattr(service_module, "_restore_unit_snapshot", lambda _previous: None)
    calls = 0

    def systemctl(*args, check=True):
        nonlocal calls
        if args == ("daemon-reload",):
            calls += 1
            raise ServiceError(f"reload {calls} failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service_module, "_systemctl", systemctl)

    with pytest.raises(ServiceError, match="could not roll back service installation"):
        service_install(AppConfig(accounts=()), tmp_path / "config.toml")


def test_managed_service_config_path_handles_read_error_and_missing_execstart(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = _unit_directory()
    service_path = unit_dir / SERVICE_NAME
    service_path.write_text(
        f"{service_module.MANAGED_MARKER}\n[Service]\n",
        encoding="utf-8",
    )
    service_path.chmod(0o600)
    monkeypatch.setattr(service_module, "_is_managed_unit", lambda _path: True)
    monkeypatch.setattr(
        service_module,
        "read_private_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read failed")),
    )
    assert managed_service_config_path() is None

    monkeypatch.undo()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    service_path = _unit_directory() / SERVICE_NAME
    service_path.write_text(f"{service_module.MANAGED_MARKER}\n", encoding="utf-8")
    service_path.chmod(0o600)
    assert managed_service_config_path() is None


def test_managed_service_config_path_returns_none_without_managed_unit(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert managed_service_config_path() is None


def test_unit_directory_rejects_existing_file_and_securing_error(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.parent.mkdir(parents=True)
    unit_dir.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(service_module, "ensure_private_directory", lambda *_args, **_kwargs: None)
    with pytest.raises(ServiceError, match="must be a real directory"):
        _unit_directory()

    unit_dir.unlink()
    unit_dir.mkdir(parents=True)
    monkeypatch.setattr(
        service_module,
        "ensure_private_directory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("chmod failed")),
    )
    with pytest.raises(ServiceError, match="could not secure"):
        _unit_directory(create=False)


def test_service_symlink_ancestor_ignores_dot_segments(tmp_path):
    class FakePath:
        anchor = "/"
        parts = ("/", ".", "target")

        def __init__(self, _value="/"):
            return None

        def is_absolute(self):
            return True

        @property
        def parent(self):
            return self

        def __truediv__(self, _part):
            return self

        def __itruediv__(self, _part):
            return self

        def is_symlink(self):
            return False

        @classmethod
        def cwd(cls):
            return cls()

    original_path = service_module.Path
    service_module.Path = FakePath
    try:
        service_module._assert_no_symlink_ancestors(tmp_path / "plain")
    finally:
        service_module.Path = original_path


@pytest.mark.parametrize("target", [None, "not-executable"])
def test_resolve_codex_usage_rejects_missing_or_non_executable(target, tmp_path, monkeypatch):
    path = tmp_path / "codex-usage"
    if target is not None:
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o600)
    monkeypatch.setattr(
        service_module.shutil,
        "which",
        lambda _name: str(path) if target else None,
    )

    with pytest.raises(ServiceError, match="executable"):
        service_module._resolve_codex_usage()


def test_service_install_rejects_missing_integration_watchdog_before_unit_write(
    tmp_path,
    monkeypatch,
):
    """Would fail if install wrote a unit for a wrapper that is not installed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = tmp_path / "codex-usage"
    _write_console_script(path, "codex_usage.cli")
    systemctl_calls: list[tuple[str, ...]] = []

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(path)
        if name == "codex-usage-integration-watchdog":
            return None
        raise AssertionError(f"unexpected executable lookup: {name}")

    def systemctl(*args, check=True):
        systemctl_calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(service_module, "_systemctl", systemctl)

    with pytest.raises(ServiceError, match="integration watchdog executable"):
        service_install(AppConfig(accounts=()), tmp_path / "config.toml")

    unit_dir = tmp_path / "config" / "systemd" / "user"
    assert not (unit_dir / SERVICE_NAME).exists()
    assert not (unit_dir / TIMER_NAME).exists()
    assert systemctl_calls == []


def test_resolve_codex_usage_rejects_stale_integration_watchdog_path(
    tmp_path,
    monkeypatch,
):
    """Would fail if PATH could resolve an old wrapper outside the release bin dir."""
    codex_usage = tmp_path / "release/bin/codex-usage"
    stale_watchdog = tmp_path / "old/bin/codex-usage-integration-watchdog"
    _write_console_script(codex_usage, "codex_usage.cli")
    _write_console_script(stale_watchdog, "codex_usage.integration_watchdog")

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(stale_watchdog)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    with pytest.raises(ServiceError, match="installed beside codex-usage"):
        service_module._resolve_codex_usage()


def test_resolve_codex_usage_rejects_wrong_integration_watchdog_file(
    tmp_path,
    monkeypatch,
):
    """Would fail if the rendered wrapper path was not executable."""
    codex_usage = tmp_path / "release/bin/codex-usage"
    wrapper = codex_usage.with_name("codex-usage-integration-watchdog")
    _write_console_script(codex_usage, "codex_usage.cli")
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o600)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    with pytest.raises(ServiceError, match="integration watchdog executable"):
        service_module._resolve_codex_usage()


def test_resolve_codex_usage_rejects_symlinked_integration_watchdog(
    tmp_path,
    monkeypatch,
):
    """Would fail if wrapper validation followed a symlink to another release."""
    codex_usage = tmp_path / "release/bin/codex-usage"
    real_wrapper = tmp_path / "other/bin/codex-usage-integration-watchdog"
    wrapper = codex_usage.with_name("codex-usage-integration-watchdog")
    _write_console_script(codex_usage, "codex_usage.cli")
    _write_console_script(real_wrapper, "codex_usage.integration_watchdog")
    wrapper.symlink_to(real_wrapper)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    with pytest.raises(ServiceError, match="regular executable"):
        service_module._resolve_codex_usage()


def test_resolve_codex_usage_rejects_hardlinked_integration_watchdog(
    tmp_path,
    monkeypatch,
):
    """Would fail if a wrapper with an external hardlink alias was accepted."""
    codex_usage = tmp_path / "release/bin/codex-usage"
    wrapper = codex_usage.with_name("codex-usage-integration-watchdog")
    alias = tmp_path / "wrapper-alias"
    _write_console_script(codex_usage, "codex_usage.cli")
    _write_console_script(wrapper, "codex_usage.integration_watchdog")
    alias.hardlink_to(wrapper)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    with pytest.raises(ServiceError, match="single-linked"):
        service_module._resolve_codex_usage()


def test_resolve_codex_usage_rejects_wrong_integration_watchdog_entrypoint(
    tmp_path,
    monkeypatch,
):
    """Would fail if any executable sibling could stand in for the wrapper."""
    codex_usage = tmp_path / "release/bin/codex-usage"
    wrapper = codex_usage.with_name("codex-usage-integration-watchdog")
    _write_console_script(codex_usage, "codex_usage.cli")
    _write_console_script(wrapper, "codex_usage.cli")

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    with pytest.raises(ServiceError, match="unexpected entry point"):
        service_module._resolve_codex_usage()


def test_resolve_codex_usage_rejects_comment_only_integration_watchdog_entrypoint(
    tmp_path,
    monkeypatch,
):
    """Would fail if a comment containing the import line satisfied attestation."""
    codex_usage = tmp_path / "release/bin/codex-usage"
    wrapper = codex_usage.with_name("codex-usage-integration-watchdog")
    _write_console_script(codex_usage, "codex_usage.cli")
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        (
            f"#!{sys.executable}\n"
            "# from codex_usage.integration_watchdog import main\n"
            "from codex_usage.cli import main\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(main())\n"
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o700)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    with pytest.raises(ServiceError, match="unexpected entry point"):
        service_module._resolve_codex_usage()


@pytest.mark.parametrize(
    ("entry_point", "module"),
    (
        ("codex-usage", "codex_usage.cli"),
        ("codex-usage-integration-watchdog", "codex_usage.integration_watchdog"),
    ),
)
def test_bound_console_scripts_accept_pip_261_argv0_normalization(
    tmp_path,
    entry_point,
    module,
):
    """Pip 26.2.1's generated argv[0] guard is a trusted wrapper form."""
    wrapper = tmp_path / "bin" / entry_point
    _write_console_script_with_main_guard(
        wrapper,
        module,
        "    sys.argv[0] = sys.argv[0].removesuffix('.exe')\n"
        "    sys.exit(main())\n",
    )

    binding = service_module._read_bound_console_script(
        wrapper,
        expected_module=module,
        label=f"{entry_point} executable",
    )

    assert binding.expected_module == module


def test_bound_console_script_rejects_foreign_outer_call_with_main_only_argument(tmp_path):
    """A foreign outer call must not inherit trust from its main() argument."""
    wrapper = tmp_path / "bin" / "codex-usage-integration-watchdog"
    _write_console_script_with_main_guard(
        wrapper,
        "codex_usage.integration_watchdog",
        "    untrusted_outer(main())\n",
    )

    with pytest.raises(ServiceError, match="unexpected entry point"):
        service_module._read_bound_console_script(
            wrapper,
            expected_module="codex_usage.integration_watchdog",
            label="integration watchdog executable",
        )


@pytest.mark.parametrize(
    "guard_body",
    (
        "    main()\n",
        "    sys.exit(main())\n",
        "    raise SystemExit(main())\n",
    ),
)
def test_bound_console_script_accepts_exact_main_exit_forms(tmp_path, guard_body):
    wrapper = tmp_path / "bin" / "codex-usage-integration-watchdog"
    _write_console_script_with_main_guard(
        wrapper,
        "codex_usage.integration_watchdog",
        guard_body,
    )

    binding = service_module._read_bound_console_script(
        wrapper,
        expected_module="codex_usage.integration_watchdog",
        label="integration watchdog executable",
    )

    assert binding.expected_module == "codex_usage.integration_watchdog"


@pytest.mark.parametrize(
    "guard_body",
    (
        "    sys.argv[1] = sys.argv[1].removesuffix('.exe')\n"
        "    sys.exit(main())\n",
        "    sys.argv[0] = sys.argv[0].removeprefix('.exe')\n"
        "    sys.exit(main())\n",
        "    sys.argv[0] = sys.argv[0].removesuffix('.bat')\n"
        "    sys.exit(main())\n",
        "    sys.argv[0] = sys.argv[0].removesuffix('.exe', '.bat')\n"
        "    sys.exit(main())\n",
        "    sys.argv[0] = sys.argv[0].removesuffix(suffix='.exe')\n"
        "    sys.exit(main())\n",
        "    sys.exit(main())\n"
        "    sys.argv[0] = sys.argv[0].removesuffix('.exe')\n",
        "    sys.argv[0] = sys.argv[0].removesuffix('.exe')\n"
        "    pass\n"
        "    sys.exit(main())\n",
        "    sys.argv[0] = sys.argv[0].removesuffix('.exe')\n"
        "    sys.exit(main())\n"
        "    pass\n",
        "    sys.exit(main(1))\n",
        "    sys.exit(main(), 1)\n",
        "    sys.exit(code=main())\n",
        "    raise SystemExit(main(), 1)\n",
        "    raise SystemExit(code=main())\n",
    ),
)
def test_bound_console_script_rejects_noncanonical_guard_forms(tmp_path, guard_body):
    """Only the exact pip normalization and three exact main exit forms are trusted."""
    wrapper = tmp_path / "bin" / "codex-usage-integration-watchdog"
    _write_console_script_with_main_guard(
        wrapper,
        "codex_usage.integration_watchdog",
        guard_body,
    )

    with pytest.raises(ServiceError, match="unexpected entry point"):
        service_module._read_bound_console_script(
            wrapper,
            expected_module="codex_usage.integration_watchdog",
            label="integration watchdog executable",
        )


def test_bound_console_script_rejects_dead_if_false_import(tmp_path):
    """Would fail if a dead branch import line satisfied executable attestation."""
    wrapper = tmp_path / "bin" / "codex-usage-integration-watchdog"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        (
            f"#!{sys.executable}\n"
            "import sys\n"
            "if False:\n"
            "    from codex_usage.integration_watchdog import main\n"
            "from codex_usage.cli import main\n"
            "if __name__ == '__main__':\n"
            "    sys.exit(main())\n"
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o700)

    with pytest.raises(ServiceError, match="unexpected entry point"):
        service_module._read_bound_console_script(
            wrapper,
            expected_module="codex_usage.integration_watchdog",
            label="integration watchdog executable",
        )


def test_bound_console_script_rejects_foreign_main_after_expected_import(tmp_path):
    """Would fail if the expected import could be overwritten before execution."""
    wrapper = tmp_path / "bin" / "codex-usage-integration-watchdog"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        (
            f"#!{sys.executable}\n"
            "import sys\n"
            "from codex_usage.integration_watchdog import main\n"
            "from codex_usage.cli import main\n"
            "if __name__ == '__main__':\n"
            "    sys.exit(main())\n"
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o700)

    with pytest.raises(ServiceError, match="unexpected entry point"):
        service_module._read_bound_console_script(
            wrapper,
            expected_module="codex_usage.integration_watchdog",
            label="integration watchdog executable",
        )


def test_bound_console_script_rejects_main_guard_side_effect_before_main(tmp_path):
    """Would fail if main-guard side effects were accepted before main()."""
    wrapper = tmp_path / "bin" / "codex-usage-integration-watchdog"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        (
            f"#!{sys.executable}\n"
            "import os\n"
            "import sys\n"
            "from codex_usage.integration_watchdog import main\n"
            "if __name__ == '__main__':\n"
            "    os.system('touch /tmp/codex-usage-side-effect')\n"
            "    sys.exit(main())\n"
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o700)

    with pytest.raises(ServiceError, match="unexpected entry point"):
        service_module._read_bound_console_script(
            wrapper,
            expected_module="codex_usage.integration_watchdog",
            label="integration watchdog executable",
        )


def test_bound_console_script_rejects_top_level_side_effect(tmp_path):
    """Would fail if arbitrary top-level statements were ignored by attestation."""
    wrapper = tmp_path / "bin" / "codex-usage-integration-watchdog"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        (
            f"#!{sys.executable}\n"
            "import sys\n"
            "open('/tmp/codex-usage-side-effect', 'w').close()\n"
            "from codex_usage.integration_watchdog import main\n"
            "if __name__ == '__main__':\n"
            "    sys.exit(main())\n"
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o700)

    with pytest.raises(ServiceError, match="unexpected entry point"):
        service_module._read_bound_console_script(
            wrapper,
            expected_module="codex_usage.integration_watchdog",
            label="integration watchdog executable",
        )


def test_bound_console_script_rejects_hardlinked_interpreter(
    tmp_path,
    monkeypatch,
):
    """Would fail if the shebang interpreter path was not inode/link bound."""
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"synthetic interpreter")
    interpreter.chmod(0o700)
    alias = tmp_path / "python-alias"
    alias.hardlink_to(interpreter)
    wrapper = tmp_path / "bin" / "codex-usage-integration-watchdog"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        (
            f"#!{interpreter}\n"
            "import sys\n"
            "from codex_usage.integration_watchdog import main\n"
            "if __name__ == '__main__':\n"
            "    sys.exit(main())\n"
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    monkeypatch.setattr(service_module.sys, "executable", str(interpreter))

    with pytest.raises(ServiceError, match="interpreter"):
        service_module._read_bound_console_script(
            wrapper,
            expected_module="codex_usage.integration_watchdog",
            label="integration watchdog executable",
        )


def test_resolve_codex_usage_rejects_wrong_console_script_interpreter(
    tmp_path,
    monkeypatch,
):
    """Would fail if an executable shell script with import text was accepted."""
    codex_usage = tmp_path / "release/bin/codex-usage"
    wrapper = codex_usage.with_name("codex-usage-integration-watchdog")
    codex_usage.parent.mkdir(parents=True, exist_ok=True)
    codex_usage.write_text(
        (
            "#!/bin/sh\n"
            "from codex_usage.cli import main\n"
            "from codex_usage.integration_watchdog import main\n"
        ),
        encoding="utf-8",
    )
    codex_usage.chmod(0o700)
    _write_console_script(wrapper, "codex_usage.integration_watchdog")

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    with pytest.raises(ServiceError, match="interpreter"):
        service_module._resolve_codex_usage()


def test_resolve_codex_usage_rejects_byte_equal_0536_distribution(
    tmp_path,
    monkeypatch,
):
    """Would fail if matching file bytes were accepted from codex-usage 0.6.536."""
    codex_usage, wrapper, _record = _write_recorded_distribution(
        tmp_path,
        monkeypatch,
        version="0.6.536",
    )

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    with pytest.raises(ServiceError, match="0\\.6\\.538"):
        service_module._resolve_codex_usage()


def test_resolve_codex_usage_rejects_record_hash_mismatch(
    tmp_path,
    monkeypatch,
):
    """Would fail if RECORD was present but not binding executable bytes."""
    codex_usage, wrapper, _record = _write_recorded_distribution(
        tmp_path,
        monkeypatch,
        corrupt_record_for="../../../bin/codex-usage-integration-watchdog",
    )

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    with pytest.raises(ServiceError, match="RECORD"):
        service_module._resolve_codex_usage()


def test_resolve_codex_usage_accepts_exact_browser_record_path(
    tmp_path,
    monkeypatch,
):
    """Would fail if the normal PEP 376 browser row was rejected as traversal."""
    codex_usage, wrapper, record = _write_recorded_distribution(tmp_path, monkeypatch)
    browser = codex_usage.with_name("codex-usage-browser")
    browser.write_bytes(b"b" * 175)
    browser.chmod(0o700)
    browser_hash = "sha256=uC6nlIpOb_9g53yoGEPpU0BEF6gVKdzO0_961MVsH3A"
    assert _record_hash(browser.read_bytes()) == browser_hash
    record.write_text(
        record.read_text(encoding="utf-8")
        + f"../../../bin/codex-usage-browser,{browser_hash},175\n",
        encoding="utf-8",
    )

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    assert service_module._resolve_codex_usage() == codex_usage.absolute()


@pytest.mark.parametrize(
    "relative_path",
    [
        pytest.param("../../../bin/codex-usage-browser-extra", id="browser-extra"),
        pytest.param("../../../bin/codex-usage-browser/child", id="browser-child"),
        pytest.param("../../../bin/codex-usage-other", id="other-script"),
        pytest.param("../../bin/codex-usage-browser", id="two-parent-depth"),
        pytest.param("../../../../bin/codex-usage-browser", id="four-parent-depth"),
    ],
)
def test_parse_record_rejects_unallowlisted_browser_traversal_paths(relative_path):
    """Would fail if browser allowlisting admitted adjacent traversal paths."""
    with pytest.raises(ServiceError, match="RECORD"):
        service_module._parse_record(
            f"{relative_path},sha256=uC6nlIpOb_9g53yoGEPpU0BEF6gVKdzO0_961MVsH3A,175\n".encode()
        )


def test_resolve_codex_usage_rejects_integration_watchdog_record_size_mismatch(
    tmp_path,
    monkeypatch,
):
    """Would fail if the active integration watchdog RECORD size was not bound."""
    codex_usage, wrapper, record = _write_recorded_distribution(tmp_path, monkeypatch)
    watchdog_row = "../../../bin/codex-usage-integration-watchdog"
    expected_row = f"{watchdog_row},{_record_hash(wrapper.read_bytes())},{wrapper.stat().st_size}"
    record.write_text(
        record.read_text(encoding="utf-8").replace(
            expected_row,
            f"{watchdog_row},{_record_hash(wrapper.read_bytes())},{wrapper.stat().st_size + 1}",
        ),
        encoding="utf-8",
    )

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    with pytest.raises(ServiceError, match="RECORD"):
        service_module._resolve_codex_usage()


def test_resolve_codex_usage_rejects_duplicate_metadata_fields(
    tmp_path,
    monkeypatch,
):
    """Would fail if duplicate METADATA headers could overwrite trusted identity."""
    codex_usage, wrapper, _record = _write_recorded_distribution(
        tmp_path,
        monkeypatch,
        metadata_suffix="Name: codex-usage\n",
    )

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    with pytest.raises(ServiceError, match="METADATA"):
        service_module._resolve_codex_usage()


def test_resolve_codex_usage_accepts_metadata_24_repeated_requires_dist(
    tmp_path,
    monkeypatch,
):
    """Would fail if valid repeatable Core Metadata fields blocked unit attestation."""
    codex_usage, wrapper, _record = _write_recorded_distribution(
        tmp_path,
        monkeypatch,
        metadata_suffix=(
            "Requires-Dist: playwright>=1.52\n"
            "Requires-Dist: pytest>=8.0; extra == 'dev'\n"
            "Requires-Dist: ruff>=0.11; extra == 'dev'\n"
        ),
    )

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    assert service_module._resolve_codex_usage() == codex_usage.absolute()


@pytest.mark.parametrize(
    "field",
    [
        "Classifier",
        "Dynamic",
        "License-File",
        "Obsoletes",
        "Obsoletes-Dist",
        "Platform",
        "Provides",
        "Provides-Dist",
        "Provides-Extra",
        "Requires",
        "Requires-Dist",
        "Requires-External",
        "Supported-Platform",
        "Project-URL",
    ],
)
def test_metadata_headers_accepts_only_repeatable_core_metadata_fields(field):
    """Would fail if an allowed repeatable field was treated as an identity duplicate."""
    headers = service_module._metadata_headers(
        (
            "Metadata-Version: 2.4\n"
            "Name: codex-usage\n"
            "Version: 0.6.539\n"
            f"{field}: first\n"
            f"{field.lower()}: second\n"
        ).encode()
    )

    assert headers == {
        "metadata-version": "2.4",
        "name": "codex-usage",
        "version": "0.6.539",
    }


@pytest.mark.parametrize(
    "metadata_suffix",
    [
        pytest.param("Name: codex-usage\n", id="duplicate-name"),
        pytest.param("Version: 0.6.539\n", id="duplicate-version"),
        pytest.param("Metadata-Version: 2.4\n", id="duplicate-metadata-version"),
        pytest.param(
            "Unrecognized-Field: first\nunrecognized-field: second\n",
            id="duplicate-unknown-field",
        ),
    ],
)
def test_metadata_headers_rejects_duplicate_identity_or_unknown_field(metadata_suffix):
    """Would fail if non-repeatable or unrecognized METADATA fields were accepted."""
    with pytest.raises(ServiceError, match="METADATA"):
        service_module._metadata_headers(
            (
                "Metadata-Version: 2.4\n"
                "Name: codex-usage\n"
                "Version: 0.6.539\n"
                f"{metadata_suffix}"
            ).encode()
        )


def test_resolve_codex_usage_rejects_duplicate_record_paths(
    tmp_path,
    monkeypatch,
):
    """Would fail if duplicate RECORD rows could overwrite a prior path binding."""
    codex_usage, wrapper, record = _write_recorded_distribution(tmp_path, monkeypatch)
    cli_module = record.parent.parent / "codex_usage/cli.py"
    payload = cli_module.read_bytes()
    record.write_text(
        record.read_text(encoding="utf-8")
        + f"codex_usage/cli.py,{_record_hash(payload)},{len(payload)}\n",
        encoding="utf-8",
    )

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    with pytest.raises(ServiceError, match="RECORD"):
        service_module._resolve_codex_usage()


def test_resolve_codex_usage_rejects_dist_info_rebind_between_scan_and_read(
    tmp_path,
    monkeypatch,
):
    """Would fail if METADATA/RECORD were reopened by path after dist-info scan."""
    codex_usage, wrapper, record = _write_recorded_distribution(tmp_path, monkeypatch)
    site_packages = codex_usage.parents[1] / "lib" / "python" / "site-packages"
    original_dist_info = record.parent
    rebound_site_packages = tmp_path / "rebound" / "site-packages"
    rebound_dist_info = rebound_site_packages / original_dist_info.name
    rebound_dist_info.mkdir(parents=True)
    rebound_metadata = (
        b"Metadata-Version: 2.4\n"
        b"Name: codex-usage\n"
        b"Version: 0.6.539\n"
        b"Summary: rebound metadata\n"
    )
    rebound_record_rows = []
    for line in record.read_text(encoding="utf-8").splitlines():
        path, _digest, _size = line.split(",", 2)
        if path.endswith("/METADATA"):
            rebound_record_rows.append(
                f"{path},{_record_hash(rebound_metadata)},{len(rebound_metadata)}"
            )
        elif path.endswith("/RECORD"):
            rebound_record_rows.append(f"{path},,")
        else:
            rebound_record_rows.append(line)
    (rebound_dist_info / "METADATA").write_bytes(rebound_metadata)
    (rebound_dist_info / "RECORD").write_text(
        "\n".join(rebound_record_rows) + "\n",
        encoding="utf-8",
    )
    locate_counts: dict[str, int] = {}

    class RebindingDistribution:
        version = "0.6.539"
        metadata: ClassVar[dict[str, str]] = {
            "Name": "codex-usage",
            "Version": "0.6.539",
        }

        def locate_file(self, path: object) -> Path:
            relative = str(path)
            locate_counts[relative] = locate_counts.get(relative, 0) + 1
            if relative in {
                f"{original_dist_info.name}/METADATA",
                f"{original_dist_info.name}/RECORD",
            } and locate_counts[relative] > 1:
                return rebound_site_packages / relative
            return site_packages / relative

    _install_fake_codex_usage_distribution(monkeypatch, RebindingDistribution())

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    assert service_module._resolve_codex_usage() == codex_usage.absolute()
    assert f"{original_dist_info.name}/METADATA" not in locate_counts
    assert f"{original_dist_info.name}/RECORD" not in locate_counts


def test_resolve_codex_usage_rejects_site_packages_rebind_before_dist_info_scan(
    tmp_path,
    monkeypatch,
):
    """Would fail if site-packages was reopened by path after its root identity check."""
    codex_usage, wrapper, record = _write_recorded_distribution(tmp_path, monkeypatch)
    site_packages = codex_usage.parents[1] / "lib" / "python" / "site-packages"
    rebound_site_packages = tmp_path / "rebound-site-packages"
    shutil.copytree(site_packages, rebound_site_packages)
    rebound_record = rebound_site_packages / record.relative_to(site_packages)
    module = rebound_site_packages / "codex_usage" / "integration_watchdog.py"
    module.write_text("def main():\n    return 99\n", encoding="utf-8")
    rows = []
    for line in rebound_record.read_text(encoding="utf-8").splitlines():
        path, digest, size = line.split(",", 2)
        if path == "codex_usage/integration_watchdog.py":
            payload = module.read_bytes()
            rows.append(f"{path},{_record_hash(payload)},{len(payload)}")
        else:
            rows.append(f"{path},{digest},{size}")
    rebound_record.write_text("\n".join(rows) + "\n", encoding="utf-8")
    real_scan_dist_info_roots = service_module._scan_dist_info_roots
    rebound_done = False

    def rebind_site_packages_before_scan(
        distribution,
        scanned_site_packages,
        *args,
        **kwargs,
    ):
        nonlocal rebound_done
        if not rebound_done:
            rebound_done = True
            scanned_site_packages.rename(
                scanned_site_packages.with_name("site-packages-old")
            )
            rebound_site_packages.rename(scanned_site_packages)
        return real_scan_dist_info_roots(
            distribution,
            scanned_site_packages,
            *args,
            **kwargs,
        )

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(
        service_module,
        "_scan_dist_info_roots",
        rebind_site_packages_before_scan,
    )

    with pytest.raises(ServiceError, match="distribution root"):
        service_module._resolve_codex_usage()
    assert rebound_done


def test_resolve_codex_usage_bounds_distribution_files_without_materializing(
    tmp_path,
    monkeypatch,
):
    """Would fail if distribution.files was consumed as an unbounded sequence."""
    class HugeFiles:
        def __iter__(self):
            for index in range(8192):
                if index > 4096:
                    raise AssertionError("distribution.files scan was unbounded")
                yield PurePosixPath(f"unrelated-{index}.txt")

    codex_usage, wrapper, _record = _write_recorded_distribution(
        tmp_path,
        monkeypatch,
        files_override=HugeFiles(),
    )

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    assert service_module._resolve_codex_usage() == codex_usage.absolute()


def test_resolve_codex_usage_scans_record_without_distribution_files_property(
    tmp_path,
    monkeypatch,
):
    """Would fail if resolver materialized distribution.files instead of scanning RECORD."""
    codex_usage, wrapper, _record = _write_recorded_distribution(tmp_path, monkeypatch)
    site_packages = codex_usage.parents[1] / "lib" / "python" / "site-packages"

    class RecordOnlyDistribution:
        def __init__(self) -> None:
            self.version = "0.6.539"
            self.metadata = {"Name": "codex-usage", "Version": "0.6.539"}

        @property
        def files(self):  # pragma: no cover - failure path is the assertion itself
            raise AssertionError("distribution.files must not be read")

        def locate_file(self, path: object) -> Path:
            return (site_packages / Path(str(path))).resolve(strict=False)

    _install_fake_codex_usage_distribution(monkeypatch, RecordOnlyDistribution())

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    assert service_module._resolve_codex_usage() == codex_usage.absolute()


def test_resolve_codex_usage_bounds_dist_info_scan_without_materialized_listdir(
    tmp_path,
    monkeypatch,
):
    """Would fail if dist-info scanning sorted an unbounded os.listdir result."""
    codex_usage, wrapper, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    class HugeNames:
        def __iter__(self):
            for index in range(8192):
                if index > service_module.MAX_DISTRIBUTION_FILES:
                    raise AssertionError("dist-info scan was materialized")
                yield f"unrelated-{index}.txt"

    def listdir_must_not_drive_dist_info_scan(path):
        if isinstance(path, int):
            return HugeNames()
        return []

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.os, "listdir", listdir_must_not_drive_dist_info_scan)
    monkeypatch.setattr(service_module.shutil, "which", which)

    assert service_module._resolve_codex_usage() == codex_usage.absolute()


@pytest.mark.parametrize(
    "foreign_record_payload",
    [
        pytest.param(None, id="missing"),
        pytest.param(b"malformed\n", id="malformed"),
    ],
)
def test_resolve_codex_usage_ignores_foreign_distribution_before_record_access(
    tmp_path,
    monkeypatch,
    foreign_record_payload: bytes | None,
):
    """Would fail if a foreign RECORD was read before filtering METADATA identity."""
    codex_usage, wrapper, target_record = _write_recorded_distribution(tmp_path, monkeypatch)
    foreign_dist_info = target_record.parent.with_name("foreign_package-9.9.dist-info")
    foreign_dist_info.mkdir()
    (foreign_dist_info / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: foreign-package\nVersion: 9.9\n",
        encoding="utf-8",
    )
    if foreign_record_payload is not None:
        (foreign_dist_info / "RECORD").write_bytes(foreign_record_payload)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    assert service_module._resolve_codex_usage() == codex_usage.absolute()


def test_resolve_codex_usage_rejects_duplicate_dist_info_roots(
    tmp_path,
    monkeypatch,
):
    """Would fail if resolver trusted RECORD rows while another dist-info root existed."""
    codex_usage, wrapper, record = _write_recorded_distribution(tmp_path, monkeypatch)
    duplicate = record.parent.with_name("codex_usage_duplicate-0.6.539.dist-info")
    duplicate.mkdir()
    (duplicate / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: codex-usage\nVersion: 0.6.539\n",
        encoding="utf-8",
    )
    (duplicate / "RECORD").write_text(
        "codex_usage_duplicate-0.6.539.dist-info/RECORD,,\n",
        encoding="utf-8",
    )

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    with pytest.raises(ServiceError, match=r"dist-info|distribution"):
        service_module._resolve_codex_usage()


def test_resolve_codex_usage_rejects_missing_record_selfrow(
    tmp_path,
    monkeypatch,
):
    """Would fail if RECORD did not bind its own dist-info row."""
    codex_usage, wrapper, record = _write_recorded_distribution(tmp_path, monkeypatch)
    rows = [
        row
        for row in record.read_text(encoding="utf-8").splitlines()
        if not row.startswith("codex_usage-0.6.539.dist-info/RECORD,")
    ]
    record.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    with pytest.raises(ServiceError, match="RECORD"):
        service_module._resolve_codex_usage()


def test_service_install_rechecks_integration_watchdog_immediately_before_unit_write(
    tmp_path,
    monkeypatch,
):
    """Would fail if a wrapper swap after resolution still produced a unit."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    codex_usage, wrapper, _record = _write_recorded_distribution(tmp_path, monkeypatch)
    systemctl_calls: list[tuple[str, ...]] = []

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    def swap_wrapper() -> None:
        wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        wrapper.chmod(0o700)

    def systemctl(*args, check=True):
        systemctl_calls.append(args)
        if args == ("is-enabled", TIMER_NAME):
            return subprocess.CompletedProcess(args, 1, "disabled\n", "")
        if args == ("is-active", TIMER_NAME):
            return subprocess.CompletedProcess(args, 1, "inactive\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(service_module, "_before_service_unit_write", swap_wrapper, raising=False)
    monkeypatch.setattr(service_module, "_systemctl", systemctl)

    with pytest.raises(ServiceError, match="changed before unit write"):
        service_install(AppConfig(accounts=()), tmp_path / "config.toml")

    unit_dir = tmp_path / "config" / "systemd" / "user"
    assert not (unit_dir / SERVICE_NAME).exists()
    assert not (unit_dir / TIMER_NAME).exists()
    assert systemctl_calls == [
        ("is-enabled", TIMER_NAME),
        ("is-active", TIMER_NAME),
        ("is-enabled", TIMER_NAME),
        ("is-active", TIMER_NAME),
        ("daemon-reload",),
        ("disable", TIMER_NAME),
        ("stop", TIMER_NAME),
    ]


def test_service_install_revalidates_distribution_immediately_before_unit_write(
    tmp_path,
    monkeypatch,
):
    """Would fail if RECORD drift after resolution still wrote a partial unit."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    codex_usage, wrapper, record = _write_recorded_distribution(tmp_path, monkeypatch)
    systemctl_calls: list[tuple[str, ...]] = []

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    def drift_record() -> None:
        record.write_text("codex_usage/cli.py,sha256=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,1\n")

    def systemctl(*args, check=True):
        systemctl_calls.append(args)
        if args == ("is-enabled", TIMER_NAME):
            return subprocess.CompletedProcess(args, 1, "disabled\n", "")
        if args == ("is-active", TIMER_NAME):
            return subprocess.CompletedProcess(args, 1, "inactive\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(service_module, "_before_service_unit_write", drift_record, raising=False)
    monkeypatch.setattr(service_module, "_systemctl", systemctl)

    with pytest.raises(ServiceError, match="changed before unit write"):
        service_install(AppConfig(accounts=()), tmp_path / "config.toml")

    unit_dir = tmp_path / "config" / "systemd" / "user"
    assert not (unit_dir / SERVICE_NAME).exists()
    assert not (unit_dir / TIMER_NAME).exists()
    assert systemctl_calls == [
        ("is-enabled", TIMER_NAME),
        ("is-active", TIMER_NAME),
        ("is-enabled", TIMER_NAME),
        ("is-active", TIMER_NAME),
        ("daemon-reload",),
        ("disable", TIMER_NAME),
        ("stop", TIMER_NAME),
    ]


def test_service_install_revalidates_between_dual_unit_writes_and_rolls_back(
    tmp_path,
    monkeypatch,
):
    """Would fail if executable drift after service write still wrote the timer."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    codex_usage, wrapper, _record = _write_recorded_distribution(tmp_path, monkeypatch)
    systemctl_calls: list[tuple[str, ...]] = []
    drifted = False

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    def drift_after_service_write(path):
        nonlocal drifted
        if path.name == SERVICE_NAME and not drifted:
            drifted = True
            wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            wrapper.chmod(0o700)

    def systemctl(*args, check=True):
        systemctl_calls.append(args)
        if args == ("is-enabled", TIMER_NAME):
            return subprocess.CompletedProcess(args, 1, "disabled\n", "")
        if args == ("is-active", TIMER_NAME):
            return subprocess.CompletedProcess(args, 1, "inactive\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service_module.shutil, "which", which)
    monkeypatch.setattr(
        service_module,
        "_after_pending_service_unit_cutover",
        drift_after_service_write,
    )
    monkeypatch.setattr(service_module, "_systemctl", systemctl)

    with pytest.raises(ServiceError, match="changed before unit write"):
        service_install(AppConfig(accounts=()), tmp_path / "config.toml")

    unit_dir = tmp_path / "config" / "systemd" / "user"
    assert drifted
    assert not (unit_dir / SERVICE_NAME).exists()
    assert not (unit_dir / TIMER_NAME).exists()
    assert systemctl_calls == [
        ("is-enabled", TIMER_NAME),
        ("is-active", TIMER_NAME),
        ("is-enabled", TIMER_NAME),
        ("is-active", TIMER_NAME),
        ("daemon-reload",),
        ("disable", TIMER_NAME),
        ("stop", TIMER_NAME),
    ]


def test_service_unit_revalidation_rejects_interpreter_byte_drift(tmp_path, monkeypatch):
    """Would fail if the resolve-time interpreter binding were not rechecked."""
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"trusted-interpreter-bytes")
    interpreter.chmod(0o700)
    monkeypatch.setattr(service_module.sys, "executable", str(interpreter))
    codex_usage, wrapper, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(codex_usage)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)
    executable = service_module._resolve_codex_usage()
    interpreter.write_bytes(b"changed-interpreter-bytes")
    interpreter.chmod(0o700)

    with pytest.raises(ServiceError, match="changed before unit write"):
        service_module._revalidate_integration_watchdog_for_unit_write(executable)


def test_service_unit_write_revalidation_cache_miss_fails_closed(tmp_path):
    """Would fail if missing resolve-time bindings skipped final attestation."""
    with pytest.raises(ServiceError, match="not resolved"):
        service_module._revalidate_integration_watchdog_for_unit_write(
            tmp_path / "release/bin/codex-usage"
        )


def test_resolve_codex_usage_returns_executable_with_installed_wrapper(
    tmp_path,
    monkeypatch,
):
    path, wrapper, _record = _write_recorded_distribution(tmp_path, monkeypatch)

    def which(name: str) -> str | None:
        if name == "codex-usage":
            return str(path)
        if name == "codex-usage-integration-watchdog":
            return str(wrapper)
        raise AssertionError(f"unexpected executable lookup: {name}")

    monkeypatch.setattr(service_module.shutil, "which", which)

    assert service_module._resolve_codex_usage() == path.absolute()


def test_validate_home_path_maps_resolve_error_and_rejects_unsafe_paths(tmp_path, monkeypatch):
    class BrokenPath:
        def resolve(self, strict=True):
            raise OSError("unavailable")

    with pytest.raises(ServiceError, match="unavailable"):
        service_module._validate_home_path(BrokenPath())

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ServiceError, match="inside the home"):
        service_module._validate_home_path(outside)

    regular = home / "file"
    regular.write_text("file", encoding="utf-8")
    with pytest.raises(ServiceError, match="real directory"):
        service_module._validate_home_path(regular)


@pytest.mark.parametrize("value", ["bad\nvalue", "bad\rvalue", "bad\x00value"])
def test_unit_quote_rejects_control_characters(value):
    with pytest.raises(ServiceError, match="invalid characters"):
        service_module._unit_quote(value)


@pytest.mark.parametrize("failure", [OSError("killpg failed"), ValueError("killpg failed")])
def test_systemctl_cleanup_maps_killpg_failures(monkeypatch, failure):
    calls = []

    class Process:
        pid = 123

        def kill(self):
            calls.append("kill")

        def wait(self, timeout=None):
            calls.append(("wait", timeout))

    monkeypatch.setattr(
        service_module.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(failure),
    )

    _terminate_systemctl_process(Process())

    assert calls == ["kill", ("wait", 1)]


def test_systemctl_cleanup_ignores_kill_and_wait_errors(monkeypatch):
    class Process:
        pid = 123

        def kill(self):
            raise OSError("kill failed")

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("systemctl", timeout)

    monkeypatch.setattr(
        service_module.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(OSError("killpg failed")),
    )

    _terminate_systemctl_process(Process())


def test_run_systemctl_bounded_rejects_missing_output_pipe(monkeypatch):
    class Process:
        pid = 123
        stdout = None
        stderr = None

    monkeypatch.setattr(service_module.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(service_module, "_terminate_systemctl_process", lambda _process: None)

    with pytest.raises(OSError, match="output pipe unavailable"):
        service_module._run_systemctl_bounded(["systemctl"])


@pytest.mark.parametrize("mode", ["deadline", "empty"])
def test_run_systemctl_bounded_handles_timeout_and_empty_selector(mode, monkeypatch):
    class Stream:
        def fileno(self):
            return 1

        def close(self):
            return None

    class Process:
        pid = 123
        stdout = Stream()
        stderr = Stream()

        def poll(self):
            return None

        def kill(self):
            return None

        def wait(self, timeout=None):
            return -9

    class Selector:
        def register(self, _stream, _event):
            return None

        def get_map(self):
            return {1: 1}

        def select(self, _timeout):
            return []

        def close(self):
            return None

    monkeypatch.setattr(service_module.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(service_module.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(service_module, "_terminate_systemctl_process", lambda _process: None)
    if mode == "deadline":
        clock = iter((0.0, float(service_module.SYSTEMCTL_TIMEOUT_SECONDS + 1)))
        monkeypatch.setattr(service_module.time, "monotonic", lambda: next(clock))

    with pytest.raises(subprocess.TimeoutExpired):
        service_module._run_systemctl_bounded(["systemctl"])


def test_run_systemctl_bounded_unregisters_eof_and_returns_completed(monkeypatch):
    class Stream:
        def __init__(self, number):
            self.number = number

        def fileno(self):
            return self.number

        def close(self):
            return None

    stdout = Stream(1)
    stderr = Stream(2)

    class Process:
        pid = 123

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    process = Process()
    process.stdout = stdout
    process.stderr = stderr

    class Selector:
        def __init__(self):
            self.active = [stdout, stderr]

        def register(self, _stream, _event):
            return None

        def get_map(self):
            return {stream: stream for stream in self.active}

        def select(self, _timeout):
            return [(SimpleNamespace(fileobj=self.active[0]), None)]

        def unregister(self, stream):
            self.active.remove(stream)

        def close(self):
            return None

    reads = iter((b"", b""))
    monkeypatch.setattr(service_module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(service_module.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(service_module.os, "read", lambda *_args: next(reads))

    result = service_module._run_systemctl_bounded(["systemctl"])

    assert result.returncode == 0
    assert result.stdout == ""


def test_run_systemctl_bounded_terminates_process_after_selector_failure(monkeypatch):
    class Stream:
        def fileno(self):
            return 1

        def close(self):
            return None

    class Process:
        pid = 123
        stdout = Stream()
        stderr = Stream()

        def poll(self):
            return None

    class Selector:
        def register(self, _stream, _event):
            return None

        def get_map(self):
            return {1: 1}

        def select(self, _timeout):
            raise RuntimeError("selector failed")

        def close(self):
            return None

    terminated = []
    monkeypatch.setattr(service_module.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(service_module.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(
        service_module,
        "_terminate_systemctl_process",
        lambda _process: terminated.append(True),
    )

    with pytest.raises(RuntimeError, match="selector failed"):
        service_module._run_systemctl_bounded(["systemctl"])
    assert terminated == [True]


def test_run_systemctl_bounded_maps_wait_timeout_after_streams_close(monkeypatch):
    class Stream:
        def fileno(self):
            return 1

        def close(self):
            return None

    class Process:
        pid = 123
        stdout = Stream()
        stderr = Stream()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("systemctl", timeout)

    class Selector:
        def register(self, _stream, _event):
            return None

        def get_map(self):
            return {}

        def close(self):
            return None

    monkeypatch.setattr(service_module.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(service_module.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(service_module, "_terminate_systemctl_process", lambda _process: None)

    with pytest.raises(subprocess.TimeoutExpired):
        service_module._run_systemctl_bounded(["systemctl"])


def test_systemctl_and_state_helpers_fail_closed(monkeypatch):
    monkeypatch.setattr(service_module.shutil, "which", lambda _name: None)
    with pytest.raises(ServiceError, match="was not found"):
        service_module._systemctl("status")

    monkeypatch.setattr(service_module.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(
        service_module,
        "_run_systemctl_bounded",
        lambda _command: subprocess.CompletedProcess([], 1, "", ""),
    )
    with pytest.raises(ServiceError, match="status failed"):
        service_module._systemctl("status")
    assert service_module._systemctl("status", check=False).returncode == 1

    monkeypatch.setattr(
        service_module,
        "_systemctl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ServiceError("failed")),
    )
    assert service_module._systemctl_state("status", SERVICE_NAME) == "unknown"
    assert service_module._systemctl_show(SERVICE_NAME, ("Result",)) == {}


def test_systemctl_show_ignores_unrecognized_output(monkeypatch):
    monkeypatch.setattr(
        service_module,
        "_systemctl",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            "unrelated\nResult=success\nOther=ignored\n",
            "",
        ),
    )

    assert service_module._systemctl_show(SERVICE_NAME, ("Result",)) == {
        "Result": "success"
    }


def test_render_service_allows_central_private_lock_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    lock_root = tmp_path / "private-locks"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(
        "codex_usage.private_io._private_lock_root", lambda: lock_root
    )

    service = service_module._render_service(
        AppConfig(accounts=()), tmp_path / "codex-usage", tmp_path / "config.toml"
    )

    assert f'ReadWritePaths="{lock_root}"' in service


def test_render_service_unsets_python_and_loader_shadow_environment(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    home.mkdir()
    data_home = tmp_path / "data"
    state_home = tmp_path / "state"
    lock_root = state_home / "codex-usage" / "locks"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setattr(
        "codex_usage.private_io._private_lock_root",
        lambda: lock_root,
    )

    service = service_module._render_service(
        AppConfig(accounts=()), tmp_path / "codex-usage", tmp_path / "config.toml"
    )

    assert "Environment=PYTHONSAFEPATH=1" in service
    assert "Environment=PYTHONNOUSERSITE=1" in service
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in service
    unset_line = next(
        line for line in service.splitlines() if line.startswith("UnsetEnvironment=")
    )
    unset_names = set(shlex.split(unset_line.removeprefix("UnsetEnvironment=")))
    assert {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONEXECUTABLE",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
    } <= unset_names


def test_cleanup_managed_timer_link_rejects_missing_symlink_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = _unit_directory()
    wants_dir = unit_dir / "timers.target.wants"
    wants_dir.symlink_to(tmp_path / "missing-wants", target_is_directory=True)
    monkeypatch.setattr(service_module, "_assert_no_symlink_ancestors", lambda _path: None)

    with pytest.raises(ServiceError, match="must not be a symlink"):
        service_module._cleanup_managed_timer_enable_link()


def test_cleanup_managed_timer_link_rejects_non_directory_wants_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    unit_dir = _unit_directory()
    wants_dir = unit_dir / "timers.target.wants"
    wants_dir.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ServiceError, match="must be a directory"):
        service_module._cleanup_managed_timer_enable_link()


def test_restore_unit_snapshot_rejects_unexpected_directory(tmp_path):
    path = tmp_path / SERVICE_NAME
    path.mkdir()

    with pytest.raises(ServiceError, match="cannot remove unexpected"):
        service_module._restore_unit_snapshot({path: None})


def test_require_complete_managed_units_rejects_partial_install(tmp_path):
    service_path = tmp_path / SERVICE_NAME
    service_path.write_text(service_module.MANAGED_MARKER, encoding="utf-8")

    with pytest.raises(ServiceError, match="must both exist"):
        service_module._require_complete_managed_units(tmp_path)


def test_render_service_json_serializes_bounded_payload():
    assert service_module.render_service_json({"status": "ok"}) == '{\n  "status": "ok"\n}'
