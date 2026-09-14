import dataclasses
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

ENV_BINARY = "/usr/bin/env"
PYTHON_BINARY = "/usr/bin/python"
BWRAP_BINARY = "/usr/bin/bwrap"
PRODUCT_LOCK_ROOT = Path("/home/teladi/.local/state/codex-usage/locks")
PROOF_FORMAT = "codex-usage-cycle31-outer-lock-isolation-v1"
PROOF_NAME = ".cycle31-outer-proof.json"
CHILD_UID = os.geteuid()
CHILD_GID = os.getegid()
MAX_LOCK_ENTRIES = 8192
MAX_REGULAR_BYTES = 4096
MAX_PROOF_BYTES = 16 * 1024
RUN_TIMEOUT_SECONDS = 900
CANONICAL_BOOTSTRAP_ENV = (
    "PATH=/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE=1",
    "PYTHONNOUSERSITE=1",
    "PYTHONSAFEPATH=1",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
    "LC_ALL=C.UTF-8",
    "LANG=C.UTF-8",
    "TZ=UTC",
)
ISOLATION_EIGHT_NODEIDS = (
    "tests/test_private_io.py::test_real_lock_isolation_redirects_python_subprocess_run_with_cleared_env",
    "tests/test_private_io.py::test_real_lock_isolation_redirects_shell_check_output_relative_python",
    "tests/test_private_io.py::test_real_lock_isolation_redirects_script_check_call_and_grandchild",
    "tests/test_private_io.py::test_real_lock_isolation_redirects_os_system_and_popen",
    "tests/test_private_io.py::test_real_lock_isolation_redirects_posix_spawn_and_fork_exec",
    "tests/test_private_io.py::test_real_lock_isolation_redirects_asyncio_subprocess_exec_and_shell",
    "tests/test_private_io.py::test_real_lock_isolation_denies_direct_host_lock_root_path_without_private_io[child]",
    "tests/test_private_io.py::test_real_lock_isolation_denies_direct_host_lock_root_path_without_private_io[grandchild]",
)
RELEASE_EIGHT_NODEIDS = (
    "tests/test_private_io.py::test_private_lock_namespace_scan_reports_approved_residue_shapes",
    "tests/test_private_io.py::test_private_lock_reconcile_rechecks_after_last_entry_validation_before_return[moved-file]",
    "tests/test_integration_attestation.py::test_external_entrypoint_binding_uses_manifest_candidate_then_trusted_core",
    "tests/test_integration_attestation.py::test_external_core_binding_accepts_controller_modules_outside_active_release",
    "tests/test_integration_installer.py::test_installer_source_manifest_uses_producer_boundary_without_controller_modules",
    "tests/test_integration_installer.py::test_runtime_wheel_import_closure_is_exact_and_utc_precedes_python_import",
    "tests/test_integration_watchdog.py::test_execute_runs_allowed_watchdog_stage_before_attested_publisher[2]",
    "tests/test_systemd.py::test_service_runs_dedicated_integration_watchdog_with_hardening",
)
ALLOWLISTED_NODEIDS = frozenset(
    (
        *ISOLATION_EIGHT_NODEIDS,
        *RELEASE_EIGHT_NODEIDS,
        "tests/test_private_io.py::test_outer_lock_launcher_canonical_bootstrap_uses_env_i_and_isolated_python",
        "tests/test_private_io.py::test_outer_lock_launcher_rejects_unallowlisted_nodeids",
        "tests/test_private_io.py::test_outer_lock_launcher_bwrap_argv_uses_absolute_tools_and_fixed_boundary",
        "tests/test_private_io.py::test_outer_lock_launcher_rejects_forged_or_direct_host_proof",
        "tests/test_private_io.py::test_outer_lock_launcher_classifies_lock_namespace_drift",
        "tests/test_private_io.py::test_outer_lock_launcher_retains_review_artifacts_and_reports_paths",
        "tests/test_private_io.py::test_outer_lock_isolation_attestation_returns_none_without_proof_env",
        "tests/test_private_io.py::test_outer_lock_isolation_attestation_rejects_adversarial_proofs[forged-proof]",
        "tests/test_private_io.py::test_outer_lock_isolation_attestation_rejects_adversarial_proofs[path-spoofing]",
        "tests/test_private_io.py::test_outer_lock_isolation_attestation_rejects_adversarial_proofs[direct-host-root-visible]",
        "tests/test_integration_installer.py::test_runtime_import_gate_rejects_missing_producer_dependency",
        "tests/test_integration_installer.py::test_python_executable_owner_allows_only_bounded_unmapped_root_overflow",
        "tests/test_integration_installer.py::test_python_executable_owner_rejects_overflow_when_system_root_is_mapped",
        "tests/test_integration_installer.py::test_kernel_overflow_uid_reader_accepts_only_bounded_decimal",
        "tests/test_integration_attestation.py::test_external_core_binding_rejects_missing_producer_release_dependency",
        "tests/test_integration_watchdog.py::test_execute_runs_allowed_watchdog_stage_before_attested_publisher[0]",
        "tests/test_service.py::test_bound_console_script_accepts_real_setuptools_argv0_normalizing_wrapper",
        "tests/test_service.py::test_bound_console_script_rejects_argv0_normalization_bypasses",
    )
)
_CANONICAL_LOCK_RE = re.compile(r"\A[0-9a-f]{64}\.lock\Z")


@dataclasses.dataclass(frozen=True)
class LockEntryRecord:
    name: str
    file_type: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int
    symlink_target: str | None
    content_sha256: str | None


@dataclasses.dataclass(frozen=True)
class LockInventory:
    root: LockEntryRecord
    entries: tuple[LockEntryRecord, ...]


@dataclasses.dataclass(frozen=True)
class DriftOutcome:
    status: str
    reason: str
    deltas: tuple[str, ...] = ()


class OuterLauncherError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 70):
        super().__init__(message)
        self.exit_code = exit_code


def _fail(message: str, exit_code: int = 70) -> NoReturn:
    raise OuterLauncherError(message, exit_code)


def _stable_stat_fields(item: os.stat_result) -> tuple[int, ...]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_uid,
        item.st_gid,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _sha256_file(path: Path, maximum: int) -> str:
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        item = os.fstat(fd)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or item.st_size <= 0
            or item.st_size > maximum
            or bool(stat.S_IMODE(item.st_mode) & 0o022)
        ):
            _fail(f"{path} identity is invalid")
        digest = hashlib.sha256()
        remaining = item.st_size
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                _fail(f"{path} changed during digest")
            digest.update(chunk)
            remaining -= len(chunk)
        if _stable_stat_fields(os.fstat(fd)) != _stable_stat_fields(item):
            _fail(f"{path} changed during digest")
        return digest.hexdigest()
    except OSError as exc:
        raise OuterLauncherError(f"{path} is unavailable", 69) from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _file_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _record_from_stat(
    item: os.stat_result,
    *,
    name: str,
    symlink_target: str | None = None,
    content_sha256: str | None = None,
) -> LockEntryRecord:
    return LockEntryRecord(
        name=name,
        file_type=_file_type(item.st_mode),
        device=item.st_dev,
        inode=item.st_ino,
        mode=item.st_mode,
        uid=item.st_uid,
        gid=item.st_gid,
        nlink=item.st_nlink,
        size=item.st_size,
        mtime_ns=item.st_mtime_ns,
        ctime_ns=item.st_ctime_ns,
        symlink_target=symlink_target,
        content_sha256=content_sha256,
    )


def _assert_normal_absolute(path: Path, *, label: str) -> None:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or "\x00" in str(path)
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        _fail(f"{label} path is invalid", 64)


def _assert_no_symlink_ancestors(path: Path, *, label: str) -> None:
    _assert_normal_absolute(path, label=label)
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            item = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise OuterLauncherError(f"{label} path is unavailable", 69) from exc
        if stat.S_ISLNK(item.st_mode):
            _fail(f"{label} path contains a symlink")


def _validate_repo_root(repo_root: Path) -> None:
    _assert_no_symlink_ancestors(repo_root, label="repo root")
    if repo_root != Path.cwd():
        _fail("cwd does not match repo root", 64)
    item = repo_root.lstat()
    if (
        not stat.S_ISDIR(item.st_mode)
        or item.st_uid != os.geteuid()
        or bool(stat.S_IMODE(item.st_mode) & 0o002)
    ):
        _fail("repo root identity is invalid", 64)
    if not (repo_root / "pyproject.toml").is_file() or not (
        repo_root / "src/codex_usage"
    ).is_dir():
        _fail("repo root is not codex-usage", 64)


def _validate_toolchain() -> None:
    for text in (ENV_BINARY, BWRAP_BINARY):
        path = Path(text)
        _assert_no_symlink_ancestors(path, label=text)
        item = path.lstat()
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_uid not in {0, os.geteuid()}
            or bool(stat.S_IMODE(item.st_mode) & 0o022)
            or not bool(stat.S_IMODE(item.st_mode) & 0o111)
        ):
            _fail(f"{text} identity is invalid", 69)
    python = Path(PYTHON_BINARY)
    _assert_normal_absolute(python, label=PYTHON_BINARY)
    resolved = python.resolve(strict=True)
    _assert_no_symlink_ancestors(resolved, label=PYTHON_BINARY)
    item = resolved.lstat()
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_uid not in {0, os.geteuid()}
        or bool(stat.S_IMODE(item.st_mode) & 0o022)
        or not bool(stat.S_IMODE(item.st_mode) & 0o111)
    ):
        _fail(f"{PYTHON_BINARY} identity is invalid", 69)
    _sha256_file(Path(BWRAP_BINARY), 16 * 1024 * 1024)
    _sha256_file(resolved, 128 * 1024 * 1024)


def _regular_content_sha256(root_fd: int, name: str, item: os.stat_result) -> str | None:
    if not stat.S_ISREG(item.st_mode):
        return None
    if item.st_size > MAX_REGULAR_BYTES:
        _fail("lock namespace regular entry exceeds byte budget")
    fd = -1
    try:
        fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=root_fd,
        )
        opened = os.fstat(fd)
        if _stable_stat_fields(opened) != _stable_stat_fields(item):
            _fail("lock namespace regular entry changed during open")
        payload = bytearray()
        while len(payload) <= MAX_REGULAR_BYTES:
            chunk = os.read(fd, min(65_536, MAX_REGULAR_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > MAX_REGULAR_BYTES:
            _fail("lock namespace regular entry exceeds byte budget")
        if _stable_stat_fields(os.fstat(fd)) != _stable_stat_fields(opened):
            _fail("lock namespace regular entry changed during read")
        return hashlib.sha256(bytes(payload)).hexdigest()
    except OSError as exc:
        raise OuterLauncherError("lock namespace regular entry cannot be read", 69) from exc
    finally:
        if fd >= 0:
            os.close(fd)


def snapshot_lock_namespace(lock_root: Path = PRODUCT_LOCK_ROOT) -> LockInventory:
    _assert_no_symlink_ancestors(lock_root, label="product lock root")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    root_fd = -1
    try:
        root_fd = os.open(lock_root, flags)
        root_item = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_item.st_mode)
            or root_item.st_uid != os.geteuid()
            or stat.S_IMODE(root_item.st_mode) != 0o700
        ):
            _fail("product lock root identity is invalid", 69)
        entries: list[LockEntryRecord] = []
        seen = 0
        with os.scandir(root_fd) as scan:
            for entry in scan:
                seen += 1
                if seen > MAX_LOCK_ENTRIES:
                    _fail("product lock namespace exceeds scan budget", 69)
                name = entry.name
                if type(name) is not str or not name or "/" in name or "\x00" in name:
                    _fail("product lock namespace entry name is invalid", 69)
                item = entry.stat(follow_symlinks=False)
                symlink_target = (
                    os.readlink(name, dir_fd=root_fd)
                    if stat.S_ISLNK(item.st_mode)
                    else None
                )
                content_sha256 = _regular_content_sha256(root_fd, name, item)
                entries.append(
                    _record_from_stat(
                        item,
                        name=name,
                        symlink_target=symlink_target,
                        content_sha256=content_sha256,
                    )
                )
        return LockInventory(
            root=_record_from_stat(root_item, name="."),
            entries=tuple(sorted(entries, key=lambda record: record.name)),
        )
    except OuterLauncherError:
        raise
    except OSError as exc:
        raise OuterLauncherError("product lock namespace cannot be scanned", 69) from exc
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def _valid_canonical_lock(record: LockEntryRecord) -> bool:
    return (
        _CANONICAL_LOCK_RE.fullmatch(record.name) is not None
        and record.file_type == "regular"
        and stat.S_IMODE(record.mode) == 0o600
        and record.uid == os.geteuid()
        and record.nlink == 1
        and 0 <= record.size <= MAX_REGULAR_BYTES
    )


def _issue_names(inventory: LockInventory) -> tuple[str, ...]:
    return tuple(
        record.name
        for record in inventory.entries
        if not _valid_canonical_lock(record)
    )


def _has_task_marker(inventory: LockInventory) -> bool:
    return any(
        record.name == PROOF_NAME or record.name.startswith(".cycle31-")
        for record in inventory.entries
    )


def classify_lock_namespace_drift(
    before: LockInventory,
    after: LockInventory,
) -> DriftOutcome:
    if before == after:
        return DriftOutcome("STABLE", "identical lock namespace")
    if _has_task_marker(after):
        return DriftOutcome("FAIL", "task marker appeared in product lock namespace")
    if before.root != after.root:
        return DriftOutcome("FAIL", "product lock root identity changed")
    before_by_name = {record.name: record for record in before.entries}
    after_by_name = {record.name: record for record in after.entries}
    if set(before_by_name) != set(after_by_name):
        return DriftOutcome("FAIL", "lock namespace path set changed")
    if _issue_names(before) != _issue_names(after):
        return DriftOutcome("FAIL", "lock namespace issue set changed")
    deltas: list[str] = []
    for name in sorted(before_by_name):
        left = before_by_name[name]
        right = after_by_name[name]
        if left == right:
            continue
        if left.file_type != right.file_type:
            return DriftOutcome("FAIL", "lock namespace topology changed")
        if not (_valid_canonical_lock(left) and _valid_canonical_lock(right)):
            return DriftOutcome("FAIL", "noncanonical issue changed")
        deltas.append(name)
    if not deltas:
        return DriftOutcome("FAIL", "lock namespace changed without bounded deltas")
    return DriftOutcome("NEUTRAL_EXTERNAL_DRIFT", "canonical regular lock drift", tuple(deltas))


def outer_proof_payload(
    *,
    production_root: Path,
    synthetic_root: Path,
    proof_path: Path,
    synthetic_root_identity: LockEntryRecord,
    hidden_production_root_identity: LockEntryRecord,
    parent_namespaces: tuple[str, str],
) -> dict[str, object]:
    return {
        "format": PROOF_FORMAT,
        "hidden_production_root_identity": dataclasses.asdict(
            hidden_production_root_identity
        ),
        "literal_production_root": str(production_root),
        "parent_mnt_namespace": parent_namespaces[1],
        "parent_user_namespace": parent_namespaces[0],
        "proof_path": str(proof_path),
        "synthetic_root": str(synthetic_root),
        "synthetic_root_identity": dataclasses.asdict(synthetic_root_identity),
    }


def validate_outer_proof_payload(
    payload: dict[str, object],
    *,
    production_root: Path,
    synthetic_root: Path,
    proof_path: Path,
    proof_sha256: str,
    current_namespaces: tuple[str, str],
    current_root_identity: LockEntryRecord,
) -> None:
    if payload.get("format") != PROOF_FORMAT:
        _fail("outer proof format is invalid")
    if payload.get("literal_production_root") != str(production_root):
        _fail("outer proof literal production root is invalid")
    if payload.get("synthetic_root") != str(synthetic_root):
        _fail("outer proof synthetic root is invalid")
    if payload.get("proof_path") != str(proof_path):
        _fail("outer proof path is invalid")
    hidden = payload.get("hidden_production_root_identity")
    synthetic = payload.get("synthetic_root_identity")
    if not isinstance(hidden, dict) or not isinstance(synthetic, dict):
        _fail("outer proof identity payload is invalid")
    stable_current = dataclasses.asdict(current_root_identity)
    stable_current.pop("mtime_ns", None)
    stable_current.pop("ctime_ns", None)
    stable_hidden = dict(hidden)
    stable_hidden.pop("mtime_ns", None)
    stable_hidden.pop("ctime_ns", None)
    stable_synthetic = dict(synthetic)
    stable_synthetic.pop("mtime_ns", None)
    stable_synthetic.pop("ctime_ns", None)
    if stable_hidden == stable_current:
        _fail("hidden production root remains visible")
    if stable_synthetic != stable_current:
        _fail("synthetic root identity does not match mounted product root")
    parent_user = payload.get("parent_user_namespace")
    parent_mnt = payload.get("parent_mnt_namespace")
    if current_namespaces[0] == parent_user or current_namespaces[1] == parent_mnt:
        _fail("outer proof namespace did not change")
    if _sha256_file(proof_path, MAX_PROOF_BYTES) != proof_sha256:
        _fail("outer proof file hash is invalid")


def _proof_root_identity_for_child(record: LockEntryRecord) -> LockEntryRecord:
    return dataclasses.replace(record, uid=CHILD_UID, gid=CHILD_GID)


def _path_record(path: Path, name: str) -> LockEntryRecord:
    return _record_from_stat(path.lstat(), name=name)


def write_outer_proof(
    *,
    production_root: Path,
    synthetic_root: Path,
    parent_namespaces: tuple[str, str],
) -> str:
    proof_path = synthetic_root.parent / PROOF_NAME
    payload = outer_proof_payload(
        production_root=production_root,
        synthetic_root=synthetic_root,
        proof_path=proof_path,
        synthetic_root_identity=_proof_root_identity_for_child(
            _path_record(synthetic_root, "synthetic root")
        ),
        hidden_production_root_identity=_path_record(production_root, "production root"),
        parent_namespaces=parent_namespaces,
    )
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(raw) > MAX_PROOF_BYTES:
        _fail("outer proof file exceeds byte budget")
    fd = -1
    parent_fd = -1
    try:
        fd = os.open(
            proof_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        written = 0
        while written < len(raw):
            count = os.write(fd, raw[written:])
            if count <= 0:
                _fail("outer proof file changed during write")
            written += count
        os.fsync(fd)
        opened = os.fstat(fd)
        named = proof_path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or _stable_stat_fields(opened) != _stable_stat_fields(named)
            or opened.st_uid != os.geteuid()
            or opened.st_gid != os.getegid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != len(raw)
        ):
            _fail("outer proof file identity is invalid")
        parent_fd = os.open(
            proof_path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        os.fsync(parent_fd)
    except OSError as exc:
        raise OuterLauncherError("outer proof file cannot be written", 69) from exc
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
        if fd >= 0:
            os.close(fd)
    return hashlib.sha256(raw).hexdigest()


def _pythonpath(repo_root: Path) -> str:
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    components = (
        repo_root / "src",
        repo_root / "tests",
        Path(f"/home/teladi/.local/lib/{version}/site-packages"),
        Path(f"/usr/local/lib64/{version}/site-packages"),
        Path(f"/usr/local/lib/{version}/site-packages"),
        Path(f"/usr/lib64/{version}/site-packages"),
        Path(f"/usr/lib/{version}/site-packages"),
    )
    selected: list[str] = []
    for component in components:
        if component.exists():
            _assert_no_symlink_ancestors(component, label="python path component")
            item = component.lstat()
            if not stat.S_ISDIR(item.st_mode) or bool(stat.S_IMODE(item.st_mode) & 0o002):
                _fail("python path component identity is invalid", 69)
            selected.append(str(component))
    if not selected:
        _fail("pytest python path is unavailable", 69)
    return ":".join(selected)


def build_bwrap_argv(
    *,
    repo_root: Path,
    synthetic_root: Path,
    proof_sha256: str,
    nodeids: tuple[str, ...],
) -> tuple[str, ...]:
    _validate_repo_root(repo_root)
    _assert_no_symlink_ancestors(synthetic_root, label="synthetic lock root")
    if (
        len(proof_sha256) != 64
        or any(character not in "0123456789abcdef" for character in proof_sha256)
    ):
        _fail("outer proof sha is invalid", 64)
    proof_path = synthetic_root.parent / PROOF_NAME
    parent_user, parent_mnt = _namespace_ids()
    return (
        BWRAP_BINARY,
        "--unshare-user-try",
        "--uid",
        str(CHILD_UID),
        "--gid",
        str(CHILD_GID),
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--die-with-parent",
        "--bind",
        "/",
        "/",
        "--bind",
        str(synthetic_root),
        str(PRODUCT_LOCK_ROOT),
        "--ro-bind",
        str(repo_root),
        str(repo_root),
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--chdir",
        str(repo_root),
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "LC_ALL",
        "C.UTF-8",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "TZ",
        "UTC",
        "--setenv",
        "PYTHONPATH",
        _pythonpath(repo_root),
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
        "--setenv",
        "PYTHONNOUSERSITE",
        "1",
        "--setenv",
        "PYTHONSAFEPATH",
        "1",
        "--setenv",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "1",
        "--setenv",
        "CODEX_USAGE_TEST_FORBID_INNER_BWRAP",
        "1",
        "--setenv",
        "CODEX_USAGE_TEST_OUTER_LOCK_PARENT_MNT_NS",
        parent_mnt,
        "--setenv",
        "CODEX_USAGE_TEST_OUTER_LOCK_PARENT_USER_NS",
        parent_user,
        "--setenv",
        "CODEX_USAGE_TEST_OUTER_LOCK_PRODUCTION_ROOT",
        str(PRODUCT_LOCK_ROOT),
        "--setenv",
        "CODEX_USAGE_TEST_OUTER_LOCK_PROOF",
        str(proof_path),
        "--setenv",
        "CODEX_USAGE_TEST_OUTER_LOCK_PROOF_SHA256",
        proof_sha256,
        "--setenv",
        "CODEX_USAGE_TEST_OUTER_LOCK_SYNTHETIC_ROOT",
        str(synthetic_root),
        "--",
        PYTHON_BINARY,
        "-S",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        *nodeids,
    )


def canonical_bootstrap_argv(
    *,
    repo_root: Path,
    release_eight: bool = False,
    isolation_eight: bool = False,
    nodeids: tuple[str, ...] = (),
) -> tuple[str, ...]:
    launcher = repo_root / "tests" / "run_outer_lock_isolation.py"
    if release_eight and isolation_eight:
        raise SystemExit(64)
    if release_eight:
        selected = ["--release-eight"]
    elif isolation_eight:
        selected = ["--isolation-eight"]
    else:
        selected = [token for nodeid in nodeids for token in ("--nodeid", nodeid)]
    return (
        ENV_BINARY,
        "-i",
        *CANONICAL_BOOTSTRAP_ENV,
        PYTHON_BINARY,
        "-I",
        "-S",
        str(launcher),
        *selected,
    )


def select_nodeids(
    nodeids: tuple[str, ...],
    *,
    release_eight: bool,
    isolation_eight: bool = False,
) -> tuple[str, ...]:
    if release_eight and isolation_eight:
        raise SystemExit(64)
    if release_eight:
        if nodeids:
            raise SystemExit(64)
        return RELEASE_EIGHT_NODEIDS
    if isolation_eight:
        if nodeids:
            raise SystemExit(64)
        return ISOLATION_EIGHT_NODEIDS
    if not nodeids:
        raise SystemExit(64)
    if any(type(nodeid) is not str or nodeid not in ALLOWLISTED_NODEIDS for nodeid in nodeids):
        raise SystemExit(64)
    return nodeids


def _namespace_ids() -> tuple[str, str]:
    try:
        return (
            os.readlink("/proc/self/ns/user"),
            os.readlink("/proc/self/ns/mnt"),
        )
    except OSError as exc:
        raise OuterLauncherError("namespace identity is unavailable", 69) from exc


def _create_synthetic_root() -> Path:
    temp = Path(tempfile.mkdtemp(prefix="cycle31-release-review.", dir="/tmp"))
    synthetic_root = temp / "synthetic-lock-root"
    synthetic_root.mkdir(mode=0o700)
    synthetic_root.chmod(0o700)
    _assert_no_symlink_ancestors(synthetic_root, label="synthetic lock root")
    item = synthetic_root.lstat()
    if (
        not stat.S_ISDIR(item.st_mode)
        or item.st_uid != os.geteuid()
        or stat.S_IMODE(item.st_mode) != 0o700
    ):
        _fail("synthetic lock root identity is invalid")
    return synthetic_root


def run_outer(nodeids: tuple[str, ...], *, repo_root: Path) -> int:
    _validate_toolchain()
    before = snapshot_lock_namespace(PRODUCT_LOCK_ROOT)
    synthetic_root = _create_synthetic_root()
    temp_root = synthetic_root.parent
    proof_path = temp_root / PROOF_NAME
    try:
        proof_sha = write_outer_proof(
            production_root=PRODUCT_LOCK_ROOT,
            synthetic_root=synthetic_root,
            parent_namespaces=_namespace_ids(),
        )
        argv = build_bwrap_argv(
            repo_root=repo_root,
            synthetic_root=synthetic_root,
            proof_sha256=proof_sha,
            nodeids=nodeids,
        )
        completed = subprocess.run(
            argv,
            env={},
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=RUN_TIMEOUT_SECONDS,
        )
        return_code = completed.returncode
    except subprocess.TimeoutExpired:
        return_code = 75
    finally:
        after = snapshot_lock_namespace(PRODUCT_LOCK_ROOT)
        drift = classify_lock_namespace_drift(before, after)
        print(
            "OUTER_LOCK_ISOLATION_RESULT "
            + json.dumps(
                {
                    "deltas": drift.deltas,
                    "owned_artifacts": {
                        "proof_path": str(proof_path),
                        "synthetic_root": str(synthetic_root),
                        "temp_root": str(temp_root),
                    },
                    "reason": drift.reason,
                    "status": drift.status,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
    if drift.status == "FAIL":
        return 80
    return return_code


def _parse_argv(argv: tuple[str, ...]) -> tuple[bool, bool, tuple[str, ...]]:
    release_eight = False
    isolation_eight = False
    nodeids: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--release-eight":
            release_eight = True
            index += 1
            continue
        if token == "--isolation-eight":
            isolation_eight = True
            index += 1
            continue
        if token == "--nodeid" and index + 1 < len(argv):
            nodeids.append(argv[index + 1])
            index += 2
            continue
        raise SystemExit(64)
    return release_eight, isolation_eight, tuple(nodeids)


def main(argv: tuple[str, ...] | None = None) -> int:
    try:
        selected_args = tuple(sys.argv[1:] if argv is None else argv)
        release_eight, isolation_eight, nodeids = _parse_argv(selected_args)
        selected = select_nodeids(
            nodeids,
            release_eight=release_eight,
            isolation_eight=isolation_eight,
        )
        return run_outer(selected, repo_root=Path.cwd())
    except SystemExit as exc:
        code = exc.code
        return code if type(code) is int else 64
    except OuterLauncherError as exc:
        print(f"outer_lock_isolation_error: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
