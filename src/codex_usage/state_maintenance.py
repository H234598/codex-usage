"""Fail-closed quarantine for non-configured local usage state.

This module deliberately has one public entry point.  It never changes the
configured account set or the owner source; it only moves complete, verified
state bundles for account ids which are absent from both authoritative owner
inventories into a private, durable quarantine.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .account_lock import state_maintenance_lock
from .config import AppConfig, default_config_path, default_state_dir, load_config
from .integration_pool_authority import (
    POOL_AUTHORITY_SOURCE_MAX_BYTES,
    PoolAuthorityInvalid,
    parse_pool_authority_source,
)
from .pool_authority_owner import (
    pool_authority_pending_path,
    pool_authority_source_path,
)
from .private_io import (
    _rename_private_lock_residue_no_replace,
    assert_no_symlink_ancestors,
    ensure_private_directory,
    private_path_lock,
    read_private_text,
    write_private_text,
)
from .source_lock import source_lock

_ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_STATE_ROOT_NAME = "codex-usage"
_QUARANTINE_ROOT_NAME = "maintenance-quarantine-v1"
_MANIFEST_NAME = "manifest.json"
_AUDIT_NAME = "audit.json"
_MAX_STATE_ARTIFACT_BYTES = 1_000_000
_MAX_ARTIFACTS = 1_000
_MAX_PENDING_TRANSACTIONS = 16
_KINDS = ("current", "snapshots", "debug", "generations", "locks")
_IGNORED_LOCK_NAMES = frozenset(("__all_accounts__.lock",))
_PENDING_TRANSACTION_NAME_RE = re.compile(r"\.pending-[0-9a-f]{32}")


@dataclass(frozen=True)
class _Identity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    ctime_ns: int
    sha256: str

    def audit_dict(self) -> dict[str, int | str]:
        return {
            "ctime_ns": self.ctime_ns,
            "device": self.device,
            "gid": self.gid,
            "inode": self.inode,
            "mode": self.mode,
            "nlink": self.nlink,
            "sha256": self.sha256,
            "size": self.size,
            "uid": self.uid,
        }


@dataclass(frozen=True)
class _Artifact:
    account_id: str
    kind: str
    source: Path
    relative_path: str
    identity: _Identity

    def audit_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "identity": self.identity.audit_dict(),
            "kind": self.kind,
            "relative_path": self.relative_path,
        }


@dataclass(frozen=True)
class _VerifiedTransient:
    source: Path
    identity: _Identity


@dataclass(frozen=True)
class _ScannedArtifacts:
    artifacts: tuple[_Artifact, ...]
    verified_transients: tuple[_VerifiedTransient, ...]


@dataclass(frozen=True)
class _AuthoritySnapshot:
    configured_account_ids: tuple[str, ...]
    config_identity: _Identity
    source_identity: _Identity


@dataclass(frozen=True)
class StateMaintenanceReport:
    """Canonical audit result of the one state-only maintenance operation."""

    applied: bool
    configured_account_ids: tuple[str, ...]
    quarantined_account_ids: tuple[str, ...]
    artifact_count: int
    audit_json: str
    audit_sha256: str
    quarantine_path: Path | None


def quarantine_unconfigured_usage_state(
    *,
    config_path: Path | None = None,
    data_home: Path | None = None,
    state_home: Path | None = None,
    apply: bool = False,
) -> StateMaintenanceReport:
    """Audit or quarantine only complete state bundles absent from owner inventory.

    Both modes first take the global source lock.  ``apply=False`` is a
    no-write dry run.  ``apply=True`` additionally takes the process-wide
    state-maintenance barrier, then records every source identity in a private
    pending transaction.  Any exception before publication rolls every moved
    artifact back; a later invocation recovers an interrupted pending
    transaction before considering new work.
    """
    if type(apply) is not bool:
        raise ValueError("state maintenance apply must be boolean")
    selected_config = _select_config_path(config_path)
    selected_data = _select_data_home(data_home)
    selected_state = _select_state_home(state_home)
    state_root = selected_data / _STATE_ROOT_NAME

    with source_lock(
        state_root,
        timeout_seconds=0,
        create_lock=apply,
    ):
        if not apply:
            with _authority_guard(selected_config, selected_state):
                authority = _load_authority_snapshot(selected_config, selected_state)
                scanned = _scan_artifacts(state_root, authority.configured_account_ids)
                _verify_authority_snapshot(selected_config, selected_state, authority)
                _verify_artifacts(scanned.artifacts, scanned.verified_transients)
                return _report(authority, scanned.artifacts, applied=False, quarantine_path=None)

        # Reject invalid owner/config/state inputs before creating the maintenance
        # barrier itself.  This keeps a rejected request strictly no-mutation.
        with _authority_guard(selected_config, selected_state):
            preflight_authority = _load_authority_snapshot(selected_config, selected_state)
            preflight = _scan_artifacts(
                state_root, preflight_authority.configured_account_ids
            )
            _verify_authority_snapshot(selected_config, selected_state, preflight_authority)
            _verify_artifacts(preflight.artifacts, preflight.verified_transients)

        with state_maintenance_lock():
            with _authority_guard(selected_config, selected_state):
                quarantine_root = state_root / _QUARANTINE_ROOT_NAME
                authority = _load_authority_snapshot(selected_config, selected_state)
                _recover_pending_transactions(quarantine_root, authority)
                _verify_authority_snapshot(selected_config, selected_state, authority)
                scanned = _scan_artifacts(state_root, authority.configured_account_ids)
                _before_quarantine_mutation()
                _verify_authority_snapshot(selected_config, selected_state, authority)
                _verify_artifacts(scanned.artifacts, scanned.verified_transients)
                report = _report(authority, scanned.artifacts, applied=True, quarantine_path=None)
                if not scanned.artifacts:
                    return report
                return _apply_quarantine(
                    state_root=state_root,
                    quarantine_root=quarantine_root,
                    authority=authority,
                    artifacts=scanned.artifacts,
                    verified_transients=scanned.verified_transients,
                    report=report,
                )


def _select_config_path(value: Path | None) -> Path:
    selected = default_config_path() if value is None else value
    if not isinstance(selected, Path) or not selected.is_absolute():
        raise ValueError("state maintenance config path must be absolute")
    return selected


def _select_data_home(value: Path | None) -> Path:
    selected = default_state_dir().parent if value is None else value
    if not isinstance(selected, Path) or not selected.is_absolute():
        raise ValueError("state maintenance data home must be absolute")
    return selected


def _select_state_home(value: Path | None) -> Path:
    if value is None:
        source = pool_authority_source_path()
        selected = source.parents[2]
    else:
        selected = value
    if not isinstance(selected, Path) or not selected.is_absolute():
        raise ValueError("state maintenance state home must be absolute")
    return selected


@contextmanager
def _authority_guard(config_path: Path, state_home: Path) -> Iterator[None]:
    source_path = pool_authority_source_path(state_home)
    # Both targets must already have ordinary owner/config locks.  A recovery
    # operator must not turn a dry run into a write merely by obtaining locks.
    with ExitStack() as stack:
        stack.enter_context(
            private_path_lock(config_path, label="state maintenance config lock", create=False)
        )
        stack.enter_context(
            private_path_lock(
                source_path,
                label="state maintenance owner source lock",
                create=False,
            )
        )
        yield


def _load_authority_snapshot(config_path: Path, state_home: Path) -> _AuthoritySnapshot:
    pending = pool_authority_pending_path(state_home)
    if pending.exists() or pending.is_symlink():
        raise ValueError("pool authority pending recovery is required before state maintenance")
    config_before = _private_identity(config_path, label="state maintenance config")
    config = load_config(config_path)
    config_after = _private_identity(config_path, label="state maintenance config")
    if config_after != config_before:
        raise ValueError("state maintenance config changed while reading")
    source_path = pool_authority_source_path(state_home)
    source_text, source_stat = read_private_text(
        source_path,
        regular_label="state maintenance owner source",
        read_label="state maintenance owner source",
        max_bytes=POOL_AUTHORITY_SOURCE_MAX_BYTES,
        too_large_label="state maintenance owner source",
        invalid_utf8_label="state maintenance owner source",
    )
    source_identity = _identity_from_text(source_text, source_stat, label="owner source")
    if stat.S_IMODE(source_stat.st_mode) != 0o600 or source_stat.st_nlink != 1:
        raise ValueError("state maintenance owner source must be a private regular file")
    try:
        source = parse_pool_authority_source(source_text.encode("utf-8"))
    except PoolAuthorityInvalid as exc:
        raise ValueError("state maintenance owner source is invalid") from exc
    _require_authority_parity(config, source)
    return _AuthoritySnapshot(
        configured_account_ids=tuple(sorted(account.id for account in config.accounts)),
        config_identity=config_before,
        source_identity=source_identity,
    )


def _require_authority_parity(config: AppConfig, source: dict[str, object]) -> None:
    if not config.pool_authority.configured:
        raise ValueError("pool authority owner inventory is not configured")
    config_ids = tuple(sorted(account.id for account in config.accounts))
    owner_ids = tuple(item.account_id for item in config.pool_authority.authorities)
    if config_ids != owner_ids:
        raise ValueError("pool authority config inventory does not match accounts")
    expected = {
        "pool_authority_source_schema_version": 2,
        "authorities": [item.to_source_record() for item in config.pool_authority.authorities],
    }
    if source != expected:
        raise ValueError("pool authority source does not match config owner inventory")


def _verify_authority_snapshot(
    config_path: Path,
    state_home: Path,
    expected: _AuthoritySnapshot,
) -> None:
    current = _load_authority_snapshot(config_path, state_home)
    if current != expected:
        raise ValueError("pool authority changed before state maintenance mutation")


def _scan_artifacts(
    state_root: Path, configured_ids: tuple[str, ...]
) -> _ScannedArtifacts:
    _require_private_directory(state_root, label="state maintenance root")
    current = state_root / "current"
    locks = state_root / "locks"
    _require_private_directory(current, label="state maintenance current directory")
    _require_private_directory(locks, label="state maintenance locks directory")
    verified_transients: list[_VerifiedTransient] = []
    by_kind = {
        "current": _scan_named_directory(
            current,
            "current",
            suffix=".json",
            verified_transients=verified_transients,
        ),
        "snapshots": _scan_optional_named_directory(
            state_root / "snapshots", "snapshots", suffix=".json"
        ),
        "debug": _scan_optional_named_directory(
            state_root / "debug", "debug", suffix="-last-ingest.json"
        ),
        "generations": _scan_optional_named_directory(
            state_root / "generations", "generations", suffix=".json"
        ),
        "locks": _scan_named_directory(
            locks,
            "locks",
            suffix=".lock",
            ignored_names=_IGNORED_LOCK_NAMES,
        ),
    }
    configured = set(configured_ids)
    artifacts: list[_Artifact] = []
    for kind in _KINDS:
        for account_id, path, identity in by_kind[kind]:
            if account_id not in configured:
                artifacts.append(
                    _Artifact(
                        account_id=account_id,
                        kind=kind,
                        source=path,
                        relative_path=f"{kind}/{path.name}",
                        identity=identity,
                    )
                )
    if len(artifacts) > _MAX_ARTIFACTS:
        raise ValueError("state maintenance has too many foreign artifacts")
    return _ScannedArtifacts(
        artifacts=tuple(sorted(artifacts, key=lambda item: (item.account_id, item.kind))),
        verified_transients=tuple(verified_transients),
    )


def _scan_optional_named_directory(
    directory: Path,
    kind: str,
    *,
    suffix: str,
) -> tuple[tuple[str, Path, _Identity], ...]:
    if not directory.exists() and not directory.is_symlink():
        return ()
    return _scan_named_directory(directory, kind, suffix=suffix)


def _scan_named_directory(
    directory: Path,
    kind: str,
    *,
    suffix: str,
    ignored_names: frozenset[str] = frozenset(),
    verified_transients: list[_VerifiedTransient] | None = None,
) -> tuple[tuple[str, Path, _Identity], ...]:
    _require_private_directory(directory, label=f"state maintenance {kind} directory")
    initial = _directory_identity(directory, label=f"state maintenance {kind} directory")
    entries: list[tuple[str, Path, _Identity]] = []
    try:
        paths = tuple(sorted(directory.iterdir(), key=lambda item: item.name))
    except OSError as exc:
        raise ValueError(f"state maintenance {kind} directory is unavailable") from exc
    if len(paths) > _MAX_ARTIFACTS:
        raise ValueError(f"state maintenance {kind} directory has too many entries")
    for path in paths:
        if path.name in ignored_names or _is_transient(path.name):
            continue
        if kind == "current":
            identity = _historical_current_lock_sidecar_identity(path)
            if identity is not None:
                if verified_transients is None:  # pragma: no cover - internal invariant
                    raise AssertionError("current sidecar identities must be retained")
                verified_transients.append(_VerifiedTransient(path, identity))
                continue
        account_id = _account_id_from_name(path.name, suffix)
        if account_id is None:
            raise ValueError(f"state maintenance {kind} contains a nontransient unknown entry")
        identity = _private_identity(path, label=f"state maintenance {kind}")
        entries.append((account_id, path, identity))
    if _directory_identity(directory, label=f"state maintenance {kind} directory") != initial:
        raise ValueError(f"state maintenance {kind} directory changed while scanning")
    return tuple(entries)


def _is_transient(name: str) -> bool:
    return name.startswith(".") and (".tmp-" in name or ".rollback" in name)


def _historical_current_lock_sidecar_identity(path: Path) -> _Identity | None:
    """Bind only the legacy ``<account>.json.lock`` form the Producer skips."""
    name = path.name
    if not name.endswith(".json.lock"):
        return None
    if _account_id_from_name(name.removesuffix(".lock"), ".json") is None:
        return None
    return _private_identity(path, label="state maintenance current lock sidecar")


def _account_id_from_name(name: str, suffix: str) -> str | None:
    if not name.endswith(suffix):
        return None
    account_id = name[: -len(suffix)]
    if account_id in {"", ".", ".."} or _ACCOUNT_ID_RE.fullmatch(account_id) is None:
        return None
    return account_id


def _verify_artifacts(
    artifacts: tuple[_Artifact, ...],
    verified_transients: tuple[_VerifiedTransient, ...] = (),
) -> None:
    for artifact in artifacts:
        current = _private_identity(artifact.source, label=f"state maintenance {artifact.kind}")
        if current != artifact.identity:
            raise ValueError("state artifact changed before maintenance mutation")
    for transient in verified_transients:
        current = _private_identity(
            transient.source,
            label="state maintenance current lock sidecar",
        )
        if current != transient.identity:
            raise ValueError("state artifact changed before maintenance mutation")


def _report(
    authority: _AuthoritySnapshot,
    artifacts: tuple[_Artifact, ...],
    *,
    applied: bool,
    quarantine_path: Path | None,
) -> StateMaintenanceReport:
    account_ids = tuple(sorted({item.account_id for item in artifacts}))
    audit = {
        "artifacts": [item.audit_dict() for item in artifacts],
        "configured_account_ids": list(authority.configured_account_ids),
        "operation": "quarantine_unconfigured_usage_state",
        "quarantined_account_ids": list(account_ids),
        "schema_version": 1,
    }
    audit_json = _canonical_json(audit)
    return StateMaintenanceReport(
        applied=applied,
        configured_account_ids=authority.configured_account_ids,
        quarantined_account_ids=account_ids,
        artifact_count=len(artifacts),
        audit_json=audit_json,
        audit_sha256=hashlib.sha256(audit_json.encode("utf-8")).hexdigest(),
        quarantine_path=quarantine_path,
    )


def _apply_quarantine(
    *,
    state_root: Path,
    quarantine_root: Path,
    authority: _AuthoritySnapshot,
    artifacts: tuple[_Artifact, ...],
    verified_transients: tuple[_VerifiedTransient, ...],
    report: StateMaintenanceReport,
) -> StateMaintenanceReport:
    state_root_identity = _directory_identity(
        state_root,
        label="state maintenance root",
    )
    ensure_private_directory(quarantine_root, label="state maintenance quarantine root")
    _require_private_directory(quarantine_root, label="state maintenance quarantine root")
    state_root_after_quarantine_creation = _directory_identity(
        state_root,
        label="state maintenance root",
    )
    if state_root_after_quarantine_creation[:-1] != state_root_identity[:-1]:
        raise ValueError("state maintenance root changed while creating quarantine root")
    # Every transaction attempt must make the state-root namespace durable
    # before creating a pending journal or moving an artifact.  A previous
    # failed fsync may have left a private, empty quarantine root behind.
    _fsync_verified_directory(
        state_root,
        label="state maintenance root",
    )
    transaction_name = f".pending-{secrets.token_hex(16)}"
    pending = quarantine_root / transaction_name
    completed = quarantine_root / f"transaction-{transaction_name.removeprefix('.pending-')}"
    try:
        pending.mkdir(mode=0o700)
    except OSError as exc:
        raise ValueError("could not create state maintenance transaction") from exc
    moved: list[_Artifact] = []
    try:
        _fsync_verified_directory(
            quarantine_root,
            label="state maintenance quarantine root",
        )
        _verify_artifacts(artifacts, verified_transients)
        _write_manifest(pending, authority, artifacts, moved)
        _verify_authority_snapshot_for_apply(
            authority,
            state_root,
            artifacts,
            verified_transients,
        )
        for artifact in artifacts:
            destination = pending / artifact.relative_path
            _verify_artifacts((), verified_transients)
            ensure_private_directory(
                destination.parent,
                label="state maintenance transaction artifact directory",
            )
            _verify_artifacts((artifact,), verified_transients)
            if destination.exists() or destination.is_symlink():
                raise ValueError("state maintenance quarantine destination already exists")
            _rename_no_replace(
                artifact.source,
                destination,
                label="state maintenance artifact quarantine",
            )
            if not _same_moved_artifact_identity(
                _private_identity(destination, label="state maintenance quarantined artifact"),
                artifact.identity,
            ):
                raise ValueError("state maintenance artifact changed during quarantine")
            moved.append(artifact)
            _verify_artifacts((), verified_transients)
            _write_manifest(pending, authority, artifacts, moved)
        _verify_artifacts((), verified_transients)
        write_private_text(
            pending / _AUDIT_NAME,
            report.audit_json,
            label="state maintenance audit",
            mode=0o600,
        )
        _verify_artifacts((), verified_transients)
        _rename_no_replace(
            pending,
            completed,
            label="state maintenance transaction publication",
        )
        _require_private_directory(
            completed,
            label="state maintenance completed transaction",
        )
        return StateMaintenanceReport(
            applied=True,
            configured_account_ids=report.configured_account_ids,
            quarantined_account_ids=report.quarantined_account_ids,
            artifact_count=report.artifact_count,
            audit_json=report.audit_json,
            audit_sha256=report.audit_sha256,
            quarantine_path=completed,
        )
    except BaseException as primary_error:
        if completed.exists() or completed.is_symlink():
            # Publication is the commit point. It is already an inspectable,
            # durable quarantine rather than an ambiguous pending state.
            raise ValueError(
                "state maintenance transaction published but final verification failed"
            ) from primary_error
        try:
            _rollback_pending(pending, artifacts)
        except BaseException as rollback_error:
            raise BaseExceptionGroup(
                "state maintenance rollback failed", [primary_error, rollback_error]
            ) from None
        raise


def _verify_authority_snapshot_for_apply(
    authority: _AuthoritySnapshot,
    state_root: Path,
    artifacts: tuple[_Artifact, ...],
    verified_transients: tuple[_VerifiedTransient, ...],
) -> None:
    # The caller already holds both authority locks.  This function deliberately
    # only repeats local state invariants after the pending journal is durable.
    _require_private_directory(state_root, label="state maintenance root")
    _verify_artifacts(artifacts, verified_transients)
    if not authority.configured_account_ids:
        raise ValueError("pool authority inventory must not be empty for state maintenance")


def _write_manifest(
    pending: Path,
    authority: _AuthoritySnapshot,
    artifacts: tuple[_Artifact, ...],
    moved: list[_Artifact],
) -> None:
    moved_paths = {item.relative_path for item in moved}
    manifest = {
        "artifacts": [item.audit_dict() for item in artifacts],
        "config_identity": authority.config_identity.audit_dict(),
        "configured_account_ids": list(authority.configured_account_ids),
        "moved_relative_paths": [
            item.relative_path for item in artifacts if item.relative_path in moved_paths
        ],
        "schema_version": 1,
        "source_identity": authority.source_identity.audit_dict(),
    }
    write_private_text(
        pending / _MANIFEST_NAME,
        _canonical_json(manifest),
        label="state maintenance manifest",
        mode=0o600,
    )


def _recover_pending_transactions(
    quarantine_root: Path,
    authority: _AuthoritySnapshot,
) -> None:
    if not quarantine_root.exists() and not quarantine_root.is_symlink():
        return
    _require_private_directory(quarantine_root, label="state maintenance quarantine root")
    try:
        children = tuple(sorted(quarantine_root.iterdir(), key=lambda item: item.name))
    except OSError as exc:
        raise ValueError("state maintenance quarantine root is unavailable") from exc
    pending = tuple(item for item in children if item.name.startswith(".pending-"))
    if len(pending) > _MAX_PENDING_TRANSACTIONS:
        raise ValueError("too many pending state maintenance transactions")
    for transaction in pending:
        _recover_pending_transaction(transaction, authority)


def _recover_pending_transaction(
    pending: Path,
    authority: _AuthoritySnapshot,
) -> None:
    _require_private_directory(pending, label="state maintenance pending transaction")
    if _PENDING_TRANSACTION_NAME_RE.fullmatch(pending.name) is None:
        raise ValueError("state maintenance pending transaction name is invalid")
    pending_identity = _directory_identity(
        pending,
        label="state maintenance pending transaction",
    )
    try:
        entries = tuple(pending.iterdir())
    except OSError as exc:
        raise ValueError("state maintenance pending transaction is unavailable") from exc
    if not entries:
        if (
            _directory_identity(
                pending,
                label="state maintenance pending transaction",
            )
            != pending_identity
        ):
            raise ValueError("state maintenance pending transaction changed during recovery")
        _remove_empty_pending_transaction(pending, pending_identity)
        return
    manifest_path = pending / _MANIFEST_NAME
    text, manifest_stat = read_private_text(
        manifest_path,
        regular_label="state maintenance manifest",
        read_label="state maintenance manifest",
        max_bytes=_MAX_STATE_ARTIFACT_BYTES,
        too_large_label="state maintenance manifest",
        invalid_utf8_label="state maintenance manifest",
    )
    if stat.S_IMODE(manifest_stat.st_mode) != 0o600 or manifest_stat.st_nlink != 1:
        raise ValueError("state maintenance manifest must be a private regular file")
    try:
        manifest = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("state maintenance manifest is invalid") from exc
    artifacts = _artifacts_from_manifest(pending, manifest, authority)
    _rollback_pending(pending, artifacts)


def _artifacts_from_manifest(
    pending: Path,
    manifest: object,
    authority: _AuthoritySnapshot,
) -> tuple[_Artifact, ...]:
    if type(manifest) is not dict or set(manifest) != {
        "artifacts",
        "config_identity",
        "configured_account_ids",
        "moved_relative_paths",
        "schema_version",
        "source_identity",
    }:
        raise ValueError("state maintenance manifest is invalid")
    if (
        _identity_from_audit(manifest["config_identity"]) != authority.config_identity
        or _identity_from_audit(manifest["source_identity"]) != authority.source_identity
        or manifest["configured_account_ids"] != list(authority.configured_account_ids)
    ):
        raise ValueError("state maintenance manifest is invalid")
    raw_artifacts = manifest.get("artifacts")
    if type(raw_artifacts) is not list or not raw_artifacts or len(raw_artifacts) > _MAX_ARTIFACTS:
        raise ValueError("state maintenance manifest is invalid")
    artifacts: list[_Artifact] = []
    for item in raw_artifacts:
        if type(item) is not dict or set(item) != {
            "account_id", "identity", "kind", "relative_path"
        }:
            raise ValueError("state maintenance manifest is invalid")
        account_id = item["account_id"]
        kind = item["kind"]
        relative = item["relative_path"]
        if (
            type(account_id) is not str
            or _ACCOUNT_ID_RE.fullmatch(account_id) is None
            or account_id in authority.configured_account_ids
            or type(kind) is not str
            or kind not in _KINDS
            or type(relative) is not str
            or relative != f"{kind}/{_filename_for(kind, account_id)}"
        ):
            raise ValueError("state maintenance manifest is invalid")
        identity = _identity_from_audit(item["identity"])
        artifacts.append(
            _Artifact(
                account_id=account_id,
                kind=kind,
                source=_state_source_from_pending(pending, kind, account_id),
                relative_path=relative,
                identity=identity,
            )
        )
    moved_paths = manifest["moved_relative_paths"]
    if type(moved_paths) is not list or any(type(item) is not str for item in moved_paths):
        raise ValueError("state maintenance manifest is invalid")
    allowed_paths = {item.relative_path for item in artifacts}
    if (
        len(set(moved_paths)) != len(moved_paths)
        or any(item not in allowed_paths for item in moved_paths)
        or moved_paths
        != [item.relative_path for item in artifacts[: len(moved_paths)]]
    ):
        raise ValueError("state maintenance manifest is invalid")
    canonical = tuple(sorted(artifacts, key=lambda entry: (entry.account_id, entry.kind)))
    if canonical != tuple(artifacts):
        raise ValueError("state maintenance manifest is not canonical")
    return tuple(artifacts)


def _state_source_from_pending(pending: Path, kind: str, account_id: str) -> Path:
    root = pending.parent.parent
    return root / kind / _filename_for(kind, account_id)


def _filename_for(kind: str, account_id: str) -> str:
    if kind == "debug":
        return f"{account_id}-last-ingest.json"
    if kind == "locks":
        return f"{account_id}.lock"
    return f"{account_id}.json"


def _rollback_pending(pending: Path, artifacts: tuple[_Artifact, ...]) -> None:
    rollback_errors: list[BaseException] = []
    for artifact in reversed(artifacts):
        destination = pending / artifact.relative_path
        source_exists = artifact.source.exists() or artifact.source.is_symlink()
        destination_exists = destination.exists() or destination.is_symlink()
        if source_exists and not destination_exists:
            continue
        if not source_exists and destination_exists:
            try:
                if not _same_moved_artifact_identity(
                    _private_identity(destination, label="state maintenance rollback artifact"),
                    artifact.identity,
                ):
                    raise ValueError("state maintenance quarantine artifact identity changed")
                _rename_no_replace(
                    destination,
                    artifact.source,
                    label="state maintenance rollback",
                )
                if not _same_moved_artifact_identity(
                    _private_identity(artifact.source, label="state maintenance rollback artifact"),
                    artifact.identity,
                ):
                    raise ValueError("state maintenance rollback artifact identity changed")
            except BaseException as exc:
                rollback_errors.append(exc)
            continue
        rollback_errors.append(ValueError("state maintenance rollback artifact state is ambiguous"))
    if rollback_errors:
        raise BaseExceptionGroup("state maintenance rollback failed", rollback_errors)
    _remove_pending_transaction(pending)


def _rename_no_replace(source: Path, destination: Path, *, label: str) -> None:
    """Move a verified artifact without ever replacing an existing target."""
    source_parent = source.parent
    destination_parent = destination.parent
    source_expected = _directory_identity(source_parent, label=f"{label} source parent")
    destination_expected = _directory_identity(
        destination_parent,
        label=f"{label} destination parent",
    )
    source_fd = destination_fd = -1
    try:
        source_fd = _open_verified_directory(source_parent, source_expected, label=label)
        destination_fd = _open_verified_directory(
            destination_parent,
            destination_expected,
            label=label,
        )
        _rename_private_lock_residue_no_replace(
            source_fd=source_fd,
            source_name=source.name,
            destination_fd=destination_fd,
            destination_name=destination.name,
        )
        _fsync_directory_fd(destination_fd)
        if source_fd != destination_fd:
            _fsync_directory_fd(source_fd)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise ValueError(f"{label} destination already exists") from exc
        raise ValueError(f"{label} failed") from exc
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if source_fd >= 0:
            os.close(source_fd)


def _open_verified_directory(
    path: Path,
    expected: tuple[int, int, int, int, int],
    *,
    label: str,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} directory is unavailable") from exc
    try:
        item = os.fstat(fd)
        actual = (
            item.st_dev,
            item.st_ino,
            stat.S_IMODE(item.st_mode),
            item.st_uid,
            item.st_ctime_ns,
        )
        if not stat.S_ISDIR(item.st_mode) or actual != expected:
            raise ValueError(f"{label} directory changed before rename")
        result = fd
        fd = -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def _fsync_directory_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise


def _remove_pending_transaction(pending: Path) -> None:
    _require_private_directory(pending, label="state maintenance pending transaction")
    try:
        children = {child.name: child for child in pending.iterdir()}
    except OSError as exc:
        raise ValueError("state maintenance pending transaction is unavailable") from exc
    if set(children) - {*_KINDS, _AUDIT_NAME, _MANIFEST_NAME}:
        raise ValueError("state maintenance pending transaction contains an unknown entry")
    for kind in _KINDS:
        child = children.get(kind)
        if child is None:
            continue
        _require_private_directory(
            child,
            label="state maintenance transaction artifact directory",
        )
        if tuple(child.iterdir()):
            raise ValueError(
                "state maintenance pending transaction contains remaining artifacts"
            )
    for name in (_AUDIT_NAME, _MANIFEST_NAME):
        child = children.get(name)
        if child is not None:
            _private_identity(child, label="state maintenance transaction file")
    # Remove every artifact directory and the optional audit before manifest.
    # A crash after manifest removal can therefore leave only an empty private
    # pending directory, which recovery can identify and safely remove.
    for kind in _KINDS:
        child = children.get(kind)
        if child is not None:
            child.rmdir()
    audit = children.get(_AUDIT_NAME)
    if audit is not None:
        audit.unlink()
    manifest = children.get(_MANIFEST_NAME)
    if manifest is not None:
        manifest.unlink()
    pending.rmdir()
    _fsync_verified_directory(
        pending.parent,
        label="state maintenance quarantine root",
    )


def _remove_empty_pending_transaction(
    pending: Path,
    expected: tuple[int, int, int, int, int],
) -> None:
    parent = pending.parent
    parent_expected = _directory_identity(parent, label="state maintenance quarantine root")
    parent_fd = pending_fd = -1
    try:
        parent_fd = _open_verified_directory(
            parent,
            parent_expected,
            label="state maintenance quarantine root",
        )
        pending_fd = _open_verified_directory(
            pending,
            expected,
            label="state maintenance pending transaction",
        )
        with os.scandir(pending_fd) as iterator:
            if next(iterator, None) is not None:
                raise ValueError("state maintenance pending transaction is not empty")
        item = os.stat(pending.name, dir_fd=parent_fd, follow_symlinks=False)
        if _directory_identity_from_stat(item) != expected:
            raise ValueError("state maintenance pending transaction changed during recovery")
        os.rmdir(pending.name, dir_fd=parent_fd)
        _fsync_directory_fd(parent_fd)
    except OSError as exc:
        raise ValueError("could not remove empty state maintenance transaction") from exc
    finally:
        if pending_fd >= 0:
            os.close(pending_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _private_identity(path: Path, *, label: str) -> _Identity:
    text, item = read_private_text(
        path,
        regular_label=label,
        read_label=label,
        max_bytes=_MAX_STATE_ARTIFACT_BYTES,
        too_large_label=label,
        invalid_utf8_label=label,
    )
    return _identity_from_text(text, item, label=label)


def _identity_from_text(text: str, item: os.stat_result, *, label: str) -> _Identity:
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_uid != os.geteuid()
        or item.st_nlink != 1
        or stat.S_IMODE(item.st_mode) != 0o600
    ):
        raise ValueError(f"{label} must be a private regular file")
    payload = text.encode("utf-8")
    return _Identity(
        device=item.st_dev,
        inode=item.st_ino,
        mode=stat.S_IMODE(item.st_mode),
        uid=item.st_uid,
        gid=item.st_gid,
        nlink=item.st_nlink,
        size=item.st_size,
        ctime_ns=item.st_ctime_ns,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _identity_from_audit(value: object) -> _Identity:
    if type(value) is not dict or set(value) != {
        "ctime_ns",
        "device",
        "gid",
        "inode",
        "mode",
        "nlink",
        "sha256",
        "size",
        "uid",
    }:
        raise ValueError("state maintenance manifest is invalid")
    if any(type(value[name]) is not int for name in value if name != "sha256"):
        raise ValueError("state maintenance manifest is invalid")
    digest = value["sha256"]
    if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("state maintenance manifest is invalid")
    return _Identity(
        device=value["device"],
        inode=value["inode"],
        mode=value["mode"],
        uid=value["uid"],
        gid=value["gid"],
        nlink=value["nlink"],
        size=value["size"],
        ctime_ns=value["ctime_ns"],
        sha256=digest,
    )


def _same_moved_artifact_identity(left: _Identity, right: _Identity) -> bool:
    """A rename changes ctime; every other bound artifact property must survive."""
    return (
        left.device,
        left.inode,
        left.mode,
        left.uid,
        left.gid,
        left.nlink,
        left.size,
        left.sha256,
    ) == (
        right.device,
        right.inode,
        right.mode,
        right.uid,
        right.gid,
        right.nlink,
        right.size,
        right.sha256,
    )


def _require_private_directory(path: Path, *, label: str) -> None:
    assert_no_symlink_ancestors(path, label=label)
    try:
        item = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISDIR(item.st_mode)
        or item.st_uid != os.geteuid()
        or stat.S_IMODE(item.st_mode) != 0o700
    ):
        raise ValueError(f"{label} must be a private real directory")


def _directory_identity(path: Path, *, label: str) -> tuple[int, int, int, int, int]:
    _require_private_directory(path, label=label)
    item = path.lstat()
    return _directory_identity_from_stat(item)


def _directory_identity_from_stat(item: os.stat_result) -> tuple[int, int, int, int, int]:
    return item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode), item.st_uid, item.st_ctime_ns


def _fsync_verified_directory(path: Path, *, label: str) -> None:
    expected = _directory_identity(path, label=label)
    fd = -1
    try:
        fd = _open_verified_directory(path, expected, label=label)
        _fsync_directory_fd(fd)
    finally:
        if fd >= 0:
            os.close(fd)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"


def _before_quarantine_mutation() -> None:
    """Focused tests replace this hook to prove TOCTOU rejection before moves."""
    return None
