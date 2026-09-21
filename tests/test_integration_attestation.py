from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest_plugins = ("test_integration_evidence",)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TRUSTED_CORE_MODULE_FILES = (
    "__init__.py",
    "account_lock.py",
    "config.py",
    "consumption.py",
    "extractor.py",
    "history.py",
    "integration_attestation.py",
    "integration_entrypoint.py",
    "integration_evidence.py",
    "integration_pool_authority.py",
    "integration_snapshot.py",
    "integration_timeout_contract.py",
    "integration_watchdog.py",
    "json_utils.py",
    "models.py",
    "private_io.py",
    "source_lock.py",
    "state.py",
    "usage_limits.py",
    "usage_resets.py",
)
_PRODUCER_RELEASE_MODULE_FILES = tuple(
    name
    for name in _TRUSTED_CORE_MODULE_FILES
    if name not in {"integration_timeout_contract.py", "integration_watchdog.py"}
)


def _core_record_digest(payload: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
    return "sha256=" + encoded.decode("ascii").rstrip("=")


def _trusted_core_dist_info(trusted_entrypoint: Path) -> Path:
    matches = sorted(trusted_entrypoint.parent.parent.glob("codex_usage-*.dist-info"))
    assert len(matches) == 1
    return matches[0]


def _write_core_record(
    trusted_entrypoint: Path,
    *,
    dist_info: Path | None = None,
    rows: list[str] | None = None,
) -> Path:
    site_packages = trusted_entrypoint.parent.parent
    selected_dist_info = (
        _trusted_core_dist_info(trusted_entrypoint)
        if dist_info is None
        else dist_info
    )
    metadata = selected_dist_info / "METADATA"
    record = selected_dist_info / "RECORD"
    if rows is None:
        entrypoint_relative = trusted_entrypoint.relative_to(site_packages).as_posix()
        metadata_relative = metadata.relative_to(site_packages).as_posix()
        record_relative = record.relative_to(site_packages).as_posix()
        package = trusted_entrypoint.parent
        rows = [
            (
                f"codex_usage/{module_name},"
                f"{_core_record_digest((package / module_name).read_bytes())},"
                f"{(package / module_name).stat().st_size}"
            )
            for module_name in _TRUSTED_CORE_MODULE_FILES
        ]
        assert entrypoint_relative in {row.split(",", 1)[0] for row in rows}
        rows.extend(
            [
                (
                    f"{metadata_relative},"
                    f"{_core_record_digest(metadata.read_bytes())},"
                    f"{metadata.stat().st_size}"
                ),
                f"{record_relative},,",
            ]
        )
    record.write_text("\n".join(rows) + "\n", encoding="utf-8")
    record.chmod(0o644)
    return record


def _trusted_entrypoint_copy(
    tmp_path: Path,
    release_entrypoint: Path,
    *,
    version: str = "0.6.538",
    distribution: str = "codex-usage",
) -> Path:
    site_packages = tmp_path / f"trusted-core-{version}/site-packages"
    trusted_parent = site_packages / "codex_usage"
    trusted_parent.mkdir(mode=0o700, parents=True)
    trusted_parent.chmod(0o700)
    release_package = release_entrypoint.parent
    for module_name in _TRUSTED_CORE_MODULE_FILES:
        trusted_module = trusted_parent / module_name
        source_module = release_package / module_name
        if not source_module.exists() and module_name in {
            "integration_timeout_contract.py",
            "integration_watchdog.py",
        }:
            source_module = _PROJECT_ROOT / "src" / "codex_usage" / module_name
        trusted_module.write_bytes(source_module.read_bytes())
        trusted_module.chmod(0o644)
    trusted = trusted_parent / "integration_entrypoint.py"
    dist_info = site_packages / f"codex_usage-{version}.dist-info"
    dist_info.mkdir(mode=0o700)
    dist_info.chmod(0o700)
    metadata = dist_info / "METADATA"
    metadata.write_text(
        (
            "Metadata-Version: 2.4\n"
            f"Name: {distribution}\n"
            f"Version: {version}\n"
        ),
        encoding="utf-8",
    )
    metadata.chmod(0o644)
    _write_core_record(trusted, dist_info=dist_info)
    return trusted


def _rewrite_release_record_row(record_path: Path, relative: str, payload: bytes) -> None:
    replacement = f"{relative},{_core_record_digest(payload)},{len(payload)}"
    rows = record_path.read_text(encoding="utf-8").splitlines()
    matched = False
    rewritten: list[str] = []
    for row in rows:
        if row.startswith(f"{relative},"):
            if matched:
                raise AssertionError(f"duplicate test row for {relative}")
            rewritten.append(replacement)
            matched = True
        else:
            rewritten.append(row)
    if not matched:
        rewritten.insert(0, replacement)
    record_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    record_path.chmod(0o600)


def _remove_release_record_row(record_path: Path, relative: str) -> None:
    rows = [
        row
        for row in record_path.read_text(encoding="utf-8").splitlines()
        if not row.startswith(f"{relative},")
    ]
    record_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    record_path.chmod(0o600)


def _update_active_manifest_after_release_forgery(state_home: Path) -> None:
    from codex_usage import integration_attestation

    active = state_home / "codex-usage/integration/active.json"
    manifest = json.loads(active.read_bytes())
    record_path = Path(manifest["record_path"])
    entrypoint_path = Path(manifest["entrypoint_path"])
    manifest["entrypoint_sha256"] = hashlib.sha256(entrypoint_path.read_bytes()).hexdigest()
    manifest["record_sha256"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
    manifest["release_tree_sha256"] = integration_attestation._release_tree_sha256(
        release_dir=Path(manifest["release_dir"])
    )
    active.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    active.chmod(0o600)


def _forge_active_release_module_self_consistent(
    state_home: Path,
    module_name: str,
) -> None:
    active = state_home / "codex-usage/integration/active.json"
    manifest = json.loads(active.read_bytes())
    record_path = Path(manifest["record_path"])
    module_path = record_path.parent.parent / "codex_usage" / module_name
    module_path.write_bytes(module_path.read_bytes() + b"\n# forged active module\n")
    module_path.chmod(0o600)
    _rewrite_release_record_row(
        record_path,
        f"codex_usage/{module_name}",
        module_path.read_bytes(),
    )
    _update_active_manifest_after_release_forgery(state_home)


def test_runtime_self_attestation_mode_race_returns_invalid_status(
    evidence_layout,
    tmp_path,
    monkeypatch,
):
    """Would fail if a recheck mode race leaked an attestation exception."""
    from codex_usage import integration_attestation, integration_watchdog

    state_home, data_home, release_entrypoint, _payload, verified = evidence_layout
    trusted_entrypoint = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    runtime_module = trusted_entrypoint.parent / "integration_attestation.py"
    real_read = integration_attestation._read_nofollow_bytes
    race_armed = False
    mode_transitioned = False

    def arm_mode_race(_trusted_entrypoint_path: Path) -> None:
        nonlocal race_armed
        race_armed = True

    def read_after_mode_transition(path: Path, **kwargs) -> bytes:
        nonlocal mode_transitioned
        if race_armed and not mode_transitioned and path == runtime_module:
            mode_transitioned = True
            runtime_module.chmod(0o664)
        return real_read(path, **kwargs)

    monkeypatch.setattr(
        integration_attestation,
        "_before_runtime_self_attestation_recheck",
        arm_mode_race,
    )
    monkeypatch.setattr(
        integration_attestation,
        "_read_nofollow_bytes",
        read_after_mode_transition,
    )
    monkeypatch.setattr(
        integration_attestation,
        "__file__",
        str(runtime_module),
    )
    assert integration_attestation.__spec__ is not None
    monkeypatch.setattr(
        integration_attestation.__spec__,
        "origin",
        str(runtime_module),
    )
    monkeypatch.setattr(
        integration_watchdog,
        "RUNTIME_SELF_ATTESTED_CORE_MODULES",
        ("codex_usage.integration_attestation",),
    )
    environment = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
    }
    published: list[object] = []

    status = integration_watchdog.execute(
        ("--config", str(tmp_path / "config.toml")),
        environ=environment,
        trusted_entrypoint_path=trusted_entrypoint,
        watchdog_runner=lambda _path, **_kwargs: 2,
        verifier=lambda **_kwargs: verified,
        publisher_runner=lambda *_args, **_kwargs: published.append(_args) or 0,
    )
    assert mode_transitioned
    assert published == []
    assert status == 70


def test_external_entrypoint_binding_uses_manifest_candidate_then_trusted_core(
    evidence_layout,
    tmp_path,
    monkeypatch,
):
    from codex_usage import integration_attestation

    state_home, data_home, release_entrypoint, _payload, expected = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    real_verifier = integration_attestation.verify_active_manifest_at
    calls: list[dict[str, Path]] = []

    def observe_verifier(**kwargs):
        calls.append(kwargs)
        return real_verifier(**kwargs)

    monkeypatch.setattr(
        integration_attestation,
        "verify_active_manifest_at",
        observe_verifier,
    )

    assert integration_attestation.verify_active_manifest_against_trusted_entrypoint(
        state_home=state_home,
        data_home=data_home,
        trusted_entrypoint_path=trusted,
    ) == expected
    assert calls == [
        {
            "state_home": state_home,
            "data_home": data_home,
            "expected_entrypoint_path": release_entrypoint,
        },
        {
            "state_home": state_home,
            "data_home": data_home,
            "expected_entrypoint_path": release_entrypoint,
        },
    ]
    assert trusted != release_entrypoint


@pytest.mark.parametrize("module_name", _PRODUCER_RELEASE_MODULE_FILES)
def test_external_core_module_binding_rejects_self_consistent_release_module_forgery(
    evidence_layout,
    tmp_path,
    module_name,
):
    """Would fail if active release RECORD/manifests could self-attest module bytes."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)

    _forge_active_release_module_self_consistent(state_home, module_name)
    assert integration_attestation.verify_active_manifest_at(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=release_entrypoint,
    ).active_release.entrypoint_path == release_entrypoint

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


@pytest.mark.parametrize("defect", ("missing-active-module", "extra-active-module"))
def test_external_core_module_binding_rejects_self_consistent_release_set_forgery(
    evidence_layout,
    tmp_path,
    defect,
):
    """Would fail if the active release module set were not checked exactly."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    active = state_home / "codex-usage/integration/active.json"
    manifest = json.loads(active.read_bytes())
    record_path = Path(manifest["record_path"])
    package = record_path.parent.parent / "codex_usage"
    if defect == "missing-active-module":
        module_path = package / "models.py"
        module_path.unlink()
        _remove_release_record_row(record_path, "codex_usage/models.py")
    else:
        module_path = package / "forged_extra.py"
        module_path.write_bytes(b"# forged extra active module\n")
        module_path.chmod(0o600)
        _rewrite_release_record_row(
            record_path,
            "codex_usage/forged_extra.py",
            module_path.read_bytes(),
        )
    _update_active_manifest_after_release_forgery(state_home)
    assert integration_attestation.verify_active_manifest_at(
        state_home=state_home,
        data_home=data_home,
        expected_entrypoint_path=release_entrypoint,
    ).active_release.entrypoint_path == release_entrypoint

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


def test_external_core_binding_accepts_controller_modules_outside_active_release(
    evidence_layout,
    tmp_path,
):
    """Would fail if Watchdog/controller modules were still required in the Producer release."""
    from codex_usage import integration_attestation

    state_home, data_home, release_entrypoint, _payload, expected = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    active = state_home / "codex-usage/integration/active.json"
    manifest = json.loads(active.read_bytes())
    record_path = Path(manifest["record_path"])
    package = record_path.parent.parent / "codex_usage"
    for module_name in ("integration_timeout_contract.py", "integration_watchdog.py"):
        assert not (package / module_name).exists()
        assert f"codex_usage/{module_name}" not in record_path.read_text(
            encoding="utf-8"
        )

    verified = integration_attestation.verify_active_manifest_against_trusted_entrypoint(
        state_home=state_home,
        data_home=data_home,
        trusted_entrypoint_path=trusted,
    )

    assert verified.release_id == expected.release_id


def test_external_core_binding_rejects_missing_producer_release_dependency(
    evidence_layout,
    tmp_path,
):
    """Would fail if active Producer release bytes did not require imported modules."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    active = state_home / "codex-usage/integration/active.json"
    manifest = json.loads(active.read_bytes())
    record_path = Path(manifest["record_path"])
    package = record_path.parent.parent / "codex_usage"
    (package / "integration_snapshot.py").unlink()
    _remove_release_record_row(record_path, "codex_usage/integration_snapshot.py")
    _update_active_manifest_after_release_forgery(state_home)

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


def test_external_core_module_binding_rejects_missing_trusted_module(
    evidence_layout,
    tmp_path,
):
    """Would fail if trusted Core provenance checked only entrypoint and dist-info."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    trusted.parent.joinpath("integration_snapshot.py").unlink()
    record = _trusted_core_dist_info(trusted) / "RECORD"
    rows = [
        row
        for row in record.read_text(encoding="utf-8").splitlines()
        if not row.startswith("codex_usage/integration_snapshot.py,")
    ]
    _write_core_record(trusted, rows=rows)

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


def test_external_entrypoint_binding_rejects_different_core_release_bytes(
    evidence_layout,
    tmp_path,
):
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    trusted.write_bytes(trusted.read_bytes() + b"\n# different core release\n")

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


def test_external_entrypoint_binding_rejects_same_bytes_from_previous_core_version(
    evidence_layout,
    tmp_path,
):
    """Would fail if trusted-core provenance were bound only to entrypoint bytes."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(
        tmp_path,
        release_entrypoint,
        version="0.6.536",
    )

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


def test_external_entrypoint_binding_rejects_metadata_body_spoof(
    evidence_layout,
    tmp_path,
):
    """Would fail if METADATA was parsed by substring instead of real headers."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    metadata = _trusted_core_dist_info(trusted) / "METADATA"
    metadata.write_text(
        (
            "Metadata-Version: 2.4\n"
            "Name: codex-usage-shadow\n"
            "Version: 0.6.536\n"
            "\n"
            "Name: codex-usage\n"
            "Version: 0.6.537\n"
        ),
        encoding="utf-8",
    )
    metadata.chmod(0o644)
    _write_core_record(trusted)

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


def test_external_entrypoint_binding_rejects_stale_parallel_core_dist_info(
    evidence_layout,
    tmp_path,
):
    """Would fail if stale codex-usage dist-info was ignored beside .537."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    site_packages = trusted.parent.parent
    stale = site_packages / "codex_usage-0.6.536.dist-info"
    stale.mkdir(mode=0o700)
    stale.chmod(0o700)
    metadata = stale / "METADATA"
    metadata.write_text(
        "Metadata-Version: 2.4\nName: codex-usage\nVersion: 0.6.536\n",
        encoding="utf-8",
    )
    metadata.chmod(0o644)
    record = stale / "RECORD"
    record.write_text("codex_usage-0.6.536.dist-info/RECORD,,\n", encoding="utf-8")
    record.chmod(0o644)

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


def test_external_entrypoint_binding_rejects_missing_core_record_file(
    evidence_layout,
    tmp_path,
):
    """Would fail if Core RECORD was optional for trusted source provenance."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    (_trusted_core_dist_info(trusted) / "RECORD").unlink()

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


@pytest.mark.parametrize(
    "defect",
    (
        "missing-entrypoint",
        "wrong-entrypoint-hash",
        "duplicate-entrypoint",
        "missing-metadata",
        "wrong-metadata-hash",
        "missing-record-self",
        "duplicate-record-self",
    ),
)
def test_external_entrypoint_binding_rejects_invalid_core_record_rows(
    evidence_layout,
    tmp_path,
    defect,
):
    """Would fail if trusted Core RECORD did not bind entrypoint and METADATA."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    site_packages = trusted.parent.parent
    dist_info = _trusted_core_dist_info(trusted)
    metadata = dist_info / "METADATA"
    record = dist_info / "RECORD"
    entrypoint_relative = trusted.relative_to(site_packages).as_posix()
    metadata_relative = metadata.relative_to(site_packages).as_posix()
    record_relative = record.relative_to(site_packages).as_posix()
    entrypoint_row = (
        f"{entrypoint_relative},{_core_record_digest(trusted.read_bytes())},"
        f"{trusted.stat().st_size}"
    )
    metadata_row = (
        f"{metadata_relative},{_core_record_digest(metadata.read_bytes())},"
        f"{metadata.stat().st_size}"
    )
    record_row = f"{record_relative},,"
    rows = [entrypoint_row, metadata_row, record_row]
    if defect == "missing-entrypoint":
        rows.remove(entrypoint_row)
    elif defect == "wrong-entrypoint-hash":
        rows[0] = f"{entrypoint_relative},sha256={'A' * 43},{trusted.stat().st_size}"
    elif defect == "duplicate-entrypoint":
        rows.insert(1, entrypoint_row)
    elif defect == "missing-metadata":
        rows.remove(metadata_row)
    elif defect == "wrong-metadata-hash":
        rows[1] = f"{metadata_relative},sha256={'A' * 43},{metadata.stat().st_size}"
    elif defect == "missing-record-self":
        rows.remove(record_row)
    elif defect == "duplicate-record-self":
        rows.append(record_row)
    _write_core_record(trusted, rows=rows)

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


@pytest.mark.parametrize("file_name", ("METADATA", "RECORD"))
def test_external_entrypoint_binding_rejects_core_provenance_inode_swap(
    evidence_layout,
    tmp_path,
    monkeypatch,
    file_name,
):
    """Would fail if Core provenance was not rechecked by inode after the hook."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    target = _trusted_core_dist_info(trusted) / file_name

    def replace_core_provenance_file(_path: Path) -> None:
        replacement = target.with_name(f"{file_name}.replacement")
        replacement.write_bytes(target.read_bytes())
        replacement.chmod(0o644)
        os.replace(replacement, target)

    monkeypatch.setattr(
        integration_attestation,
        "_before_trusted_entrypoint_recheck",
        replace_core_provenance_file,
    )

    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


@pytest.mark.parametrize("file_name", ("METADATA", "RECORD"))
def test_external_entrypoint_binding_rejects_core_provenance_gid_transition(
    evidence_layout,
    tmp_path,
    monkeypatch,
    file_name,
):
    """Would fail if stable Core METADATA/RECORD identity ignored st_gid."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    target = _trusted_core_dist_info(trusted) / file_name
    drift_enabled = False
    real_fstat = integration_attestation.os.fstat

    def enable_drift(_path: Path) -> None:
        nonlocal drift_enabled
        drift_enabled = True

    def fstat_with_provenance_gid_transition(fd: int):
        item = real_fstat(fd)
        if not drift_enabled:
            return item
        try:
            opened = Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            return item
        if opened != target:
            return item
        values = list(item)
        values[5] = item.st_gid + 1
        return os.stat_result(tuple(values))

    monkeypatch.setattr(
        integration_attestation,
        "_before_trusted_entrypoint_recheck",
        enable_drift,
    )
    monkeypatch.setattr(integration_attestation.os, "fstat", fstat_with_provenance_gid_transition)

    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


@pytest.mark.parametrize("defect", ("writable-mode", "hardlink", "symlink"))
def test_external_entrypoint_binding_rejects_untrusted_source_metadata(
    evidence_layout,
    tmp_path,
    defect,
):
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    if defect == "writable-mode":
        trusted.chmod(0o664)
    elif defect == "hardlink":
        os.link(trusted, trusted.with_name("second-link.py"))
    else:
        target = trusted.with_name("target.py")
        trusted.rename(target)
        trusted.symlink_to(target)

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


def test_external_entrypoint_binding_rejects_writable_core_package_directory(
    evidence_layout,
    tmp_path,
):
    """Would fail if a 0777 codex_usage package directory could anchor trust."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    trusted.parent.chmod(0o777)

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


def test_external_entrypoint_binding_rejects_writable_core_ancestor_above_site(
    evidence_layout,
    tmp_path,
):
    """Would fail if only package and site-packages directories were mode-checked."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    public_anchor = tmp_path / "public-core-anchor"
    public_anchor.mkdir(mode=0o777)
    public_anchor.chmod(0o777)
    trusted = _trusted_entrypoint_copy(public_anchor, release_entrypoint)

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


def test_external_entrypoint_binding_rejects_trusted_ancestor_rebind(
    evidence_layout,
    tmp_path,
    monkeypatch,
):
    """Would fail if a checked ancestor could be rebound while descendants stayed intact."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    anchor = tmp_path / "core-anchor"
    anchor.mkdir(mode=0o700)
    anchor.chmod(0o700)
    trusted = _trusted_entrypoint_copy(anchor, release_entrypoint)
    core_tree = anchor / "trusted-core-0.6.538"
    saved_tree = tmp_path / "saved-trusted-core"

    def rebind_anchor_without_replacing_descendants(_trusted_entrypoint: Path) -> None:
        core_tree.rename(saved_tree)
        anchor.rmdir()
        anchor.mkdir(mode=0o700)
        anchor.chmod(0o700)
        saved_tree.rename(core_tree)

    monkeypatch.setattr(
        integration_attestation,
        "_before_trusted_entrypoint_recheck",
        rebind_anchor_without_replacing_descendants,
    )

    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


def test_attestation_fd_identity_includes_gid(monkeypatch):
    """Would fail if FileIdentity did not bind group-owner transitions."""
    from codex_usage import integration_attestation

    real_fstat = integration_attestation.os.fstat
    expected_gid = Path(__file__).stat().st_gid + 1

    def fstat_with_distinct_gid(fd: int):
        item = real_fstat(fd)
        values = list(item)
        values[5] = item.st_gid + 1
        return os.stat_result(tuple(values))

    with monkeypatch.context() as scoped_monkeypatch:
        scoped_monkeypatch.setattr(
            integration_attestation.os,
            "fstat",
            fstat_with_distinct_gid,
        )
        with open(__file__, "rb") as handle:
            identity = integration_attestation._fd_identity(handle.fileno())

    assert identity.gid == expected_gid


def test_attestation_stable_file_identity_includes_gid():
    """Would fail if stable METADATA/RECORD identity ignored group owner."""
    from codex_usage import integration_attestation

    item = SimpleNamespace(
        st_dev=1,
        st_ino=2,
        st_mode=0o100644,
        st_uid=1000,
        st_gid=1000,
        st_nlink=1,
        st_size=12,
        st_mtime_ns=34,
        st_ctime_ns=56,
    )
    changed = SimpleNamespace(**{**vars(item), "st_gid": item.st_gid + 1})

    assert integration_attestation._stable_file_identity(item) != (
        integration_attestation._stable_file_identity(changed)
    )


@pytest.mark.parametrize("drift", ("uid", "gid", "ctime"))
def test_external_entrypoint_binding_rejects_trusted_ancestor_metadata_transition(
    evidence_layout,
    tmp_path,
    monkeypatch,
    drift,
):
    """Would fail if trusted ancestor identity ignored allowed-owner metadata drift."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceInvalid

    if drift == "uid" and os.geteuid() == 0:
        pytest.skip("root cannot synthesize an allowed owner transition")

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    anchor = tmp_path / "core-anchor"
    anchor.mkdir(mode=0o700)
    anchor.chmod(0o700)
    trusted = _trusted_entrypoint_copy(anchor, release_entrypoint)
    core_tree = anchor / "trusted-core-0.6.538"
    drift_enabled = False
    real_fstat = integration_attestation.os.fstat

    def enable_drift(_trusted_entrypoint: Path) -> None:
        nonlocal drift_enabled
        drift_enabled = True

    def fstat_with_allowed_metadata_transition(fd: int):
        item = real_fstat(fd)
        if not drift_enabled:
            return item
        try:
            opened = Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            return item
        if opened != core_tree:
            return item
        values = list(item)
        if drift == "uid":
            values[4] = 0
        elif drift == "gid":
            values[5] = item.st_gid + 1
        else:
            values[9] = item.st_ctime + 1
        return os.stat_result(tuple(values))

    monkeypatch.setattr(
        integration_attestation,
        "_before_trusted_entrypoint_recheck",
        enable_drift,
    )
    monkeypatch.setattr(integration_attestation.os, "fstat", fstat_with_allowed_metadata_transition)

    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


def test_external_entrypoint_binding_bounds_core_dist_info_scan(
    evidence_layout,
    tmp_path,
    monkeypatch,
):
    """Would fail if trusted Core dist-info scanning could walk unbounded entries."""
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceUnavailable

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)
    site_packages = trusted.parent.parent
    real_scandir = integration_attestation.os.scandir

    class FakeEntry:
        def __init__(self, index: int):
            self.name = f"unrelated-{index}.dist-info"

    class FakeScandir:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return False

        def __iter__(self):
            for index in range(
                integration_attestation.MAX_RELEASE_TREE_ENTRIES + 1
            ):
                yield FakeEntry(index)
            raise AssertionError("trusted Core dist-info scan was unbounded")

    def bounded_scandir(fd):
        if isinstance(fd, int):
            try:
                target = Path(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                target = None
            if target == site_packages:
                return FakeScandir()
        return real_scandir(fd)

    monkeypatch.setattr(integration_attestation.os, "scandir", bounded_scandir)

    with pytest.raises(IntegrationEvidenceUnavailable):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


def test_external_entrypoint_binding_rejects_trusted_source_inode_swap(
    evidence_layout,
    tmp_path,
    monkeypatch,
):
    from codex_usage import integration_attestation
    from codex_usage.private_io import IntegrationEvidenceInvalid

    state_home, data_home, release_entrypoint, _payload, _verified = evidence_layout
    trusted = _trusted_entrypoint_copy(tmp_path, release_entrypoint)

    def replace_trusted_source(_path: Path) -> None:
        replacement = trusted.with_name("replacement.py")
        replacement.write_bytes(trusted.read_bytes())
        replacement.chmod(0o644)
        os.replace(replacement, trusted)

    monkeypatch.setattr(
        integration_attestation,
        "_before_trusted_entrypoint_recheck",
        replace_trusted_source,
    )

    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_against_trusted_entrypoint(
            state_home=state_home,
            data_home=data_home,
            trusted_entrypoint_path=trusted,
        )


def _replace_active_json_inode_once(integration_fd: int) -> None:
    source_fd = os.open(
        "active.json",
        os.O_RDONLY | os.O_NOFOLLOW,
        dir_fd=integration_fd,
    )
    try:
        payload = os.read(source_fd, 128 * 1024 + 1)
    finally:
        os.close(source_fd)
    replacement = ".active-replacement"
    fd = os.open(
        replacement,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=integration_fd,
    )
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(replacement, "active.json", src_dir_fd=integration_fd, dst_dir_fd=integration_fd)


def test_verify_active_manifest_at_rejects_active_json_inode_swap(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, _verified = evidence_layout
    monkeypatch.setattr(
        integration_attestation,
        "_before_active_identity_recheck",
        _replace_active_json_inode_once,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )


def _swap_directory(path: Path) -> None:
    old = path.with_name(path.name + "-old")
    replacement = path.with_name(path.name + "-replacement")
    replacement.mkdir(mode=0o700)
    os.rename(path, old)
    os.rename(replacement, path)


def test_verify_active_manifest_at_rejects_state_home_swap(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, _verified = evidence_layout

    def swap_state(_integration_fd):
        _swap_directory(state_home)

    monkeypatch.setattr(
        integration_attestation,
        "_before_active_identity_recheck",
        swap_state,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )


def test_verify_active_manifest_at_rejects_integration_parent_swap(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, _verified = evidence_layout
    integration = state_home / "codex-usage" / "integration"

    def swap_integration(_integration_fd):
        _swap_directory(integration)

    monkeypatch.setattr(
        integration_attestation,
        "_before_active_identity_recheck",
        swap_integration,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )


def test_verify_active_manifest_at_rejects_release_directory_swap_after_hash(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, verified = evidence_layout
    release_dir = verified.active_release.release_dir
    replacement = release_dir.parent / ".release-replacement"
    old = release_dir.parent / ".release-old"
    shutil.copytree(release_dir, replacement, copy_function=shutil.copy2)

    def swap_release(_integration_fd):
        os.rename(release_dir, old)
        os.rename(replacement, release_dir)

    monkeypatch.setattr(
        integration_attestation,
        "_before_active_identity_recheck",
        swap_release,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )


def test_verify_active_manifest_at_rejects_release_file_swap_after_hash(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, verified = evidence_layout
    replacement = verified.active_release.release_dir.parent / ".entrypoint-replacement"
    shutil.copy2(entrypoint, replacement)

    def swap_entrypoint(_integration_fd):
        os.replace(replacement, entrypoint)

    monkeypatch.setattr(
        integration_attestation,
        "_before_active_identity_recheck",
        swap_entrypoint,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )


def test_verify_active_manifest_at_rejects_descendant_directory_rebinding(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, verified = evidence_layout
    release_parent = verified.active_release.release_dir.parent
    package_dir = entrypoint.parent
    replacement = release_parent / ".package-replacement"
    old = release_parent / ".package-old"
    shutil.copytree(package_dir, replacement, copy_function=shutil.copy2)
    walks = 0

    def swap_after_last_descendant_walk(_release_fd):
        nonlocal walks
        walks += 1
        if walks == 3:
            os.rename(package_dir, old)
            os.rename(replacement, package_dir)

    monkeypatch.setattr(
        integration_attestation,
        "_before_release_namespace_recheck",
        swap_after_last_descendant_walk,
        raising=False,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )
    assert walks == 3


def test_verify_active_manifest_at_rejects_in_place_release_bytes_after_scan(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, _verified = evidence_layout
    walks = 0

    def mutate_after_last_descendant_walk(_release_fd):
        nonlocal walks
        walks += 1
        if walks == 3:
            payload = bytearray(entrypoint.read_bytes())
            payload[0] ^= 1
            entrypoint.write_bytes(payload)

    monkeypatch.setattr(
        integration_attestation,
        "_before_release_namespace_recheck",
        mutate_after_last_descendant_walk,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )
    assert walks == 3


def test_verify_active_manifest_at_rejects_release_metadata_change_after_scan(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, _verified = evidence_layout
    walks = 0

    def mutate_after_last_descendant_walk(_release_fd):
        nonlocal walks
        walks += 1
        if walks == 3:
            item = entrypoint.stat()
            os.utime(
                entrypoint,
                ns=(item.st_atime_ns, item.st_mtime_ns + 1_000_000_000),
            )

    monkeypatch.setattr(
        integration_attestation,
        "_before_release_namespace_recheck",
        mutate_after_last_descendant_walk,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )
    assert walks == 3


def test_verify_active_manifest_at_rejects_release_size_change_after_scan(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, _verified = evidence_layout
    walks = 0

    def mutate_after_last_descendant_walk(_release_fd):
        nonlocal walks
        walks += 1
        if walks == 3:
            entrypoint.write_bytes(entrypoint.read_bytes() + b"\n")

    monkeypatch.setattr(
        integration_attestation,
        "_before_release_namespace_recheck",
        mutate_after_last_descendant_walk,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )
    assert walks == 3


def test_verify_active_manifest_at_rejects_inserted_release_entry_after_scan(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, verified = evidence_layout
    inserted = verified.active_release.release_dir / ".late-entry"
    walks = 0

    def insert_after_last_descendant_walk(_release_fd):
        nonlocal walks
        walks += 1
        if walks == 3:
            inserted.write_bytes(b"late")
            inserted.chmod(0o600)

    monkeypatch.setattr(
        integration_attestation,
        "_before_release_namespace_recheck",
        insert_after_last_descendant_walk,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )
    assert walks == 3


def test_verify_active_manifest_at_rejects_deleted_release_entry_after_scan(
    evidence_layout, monkeypatch
):
    from codex_usage import integration_attestation
    from codex_usage.integration_evidence import IntegrationEvidenceInvalid

    state_home, data_home, entrypoint, _payload, _verified = evidence_layout
    walks = 0

    def delete_after_last_descendant_walk(_release_fd):
        nonlocal walks
        walks += 1
        if walks == 3:
            entrypoint.unlink()

    monkeypatch.setattr(
        integration_attestation,
        "_before_release_namespace_recheck",
        delete_after_last_descendant_walk,
    )
    with pytest.raises(IntegrationEvidenceInvalid):
        integration_attestation.verify_active_manifest_at(
            state_home=state_home,
            data_home=data_home,
            expected_entrypoint_path=entrypoint,
        )
    assert walks == 3
