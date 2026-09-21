from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import stat
import sys
from pathlib import Path
from types import MappingProxyType, ModuleType

_PREPARE_SOURCE_SUCCESS = "integration_producer_source_prepared\n"
_PREPARE_MAX_SOURCE_FILE_BYTES = 2 * 1024 * 1024


class _PrepareSourceArgumentError(Exception):
    pass


class _PrepareSourceError(Exception):
    pass


def _prepare_fail() -> None:
    raise _PrepareSourceError()


def _prepare_absolute_path(value: object) -> Path:
    if type(value) is not str or "\x00" in value:
        _prepare_fail()
    path = Path(value)
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
        _prepare_fail()
    return path


def _prepare_no_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    try:
        root = current.lstat()
    except (OSError, ValueError):
        _prepare_fail()
    if not stat.S_ISDIR(root.st_mode) or stat.S_ISLNK(root.st_mode):
        _prepare_fail()
    for part in path.parts[1:]:
        current /= part
        try:
            item = current.lstat()
        except (OSError, ValueError):
            _prepare_fail()
        if stat.S_ISLNK(item.st_mode):
            _prepare_fail()


def _prepare_directory_identity(path: Path) -> tuple[int, int, int, int, int]:
    _prepare_no_symlink_ancestors(path)
    try:
        item = path.lstat()
    except (OSError, ValueError):
        _prepare_fail()
    if (
        not stat.S_ISDIR(item.st_mode)
        or item.st_uid != os.geteuid()
        or stat.S_IMODE(item.st_mode) not in {0o700, 0o755}
    ):
        _prepare_fail()
    return (item.st_dev, item.st_ino, item.st_uid, item.st_mode, item.st_nlink)


def _prepare_file_identity(path: Path) -> tuple[int, int, int, int, int, int, int, int]:
    _prepare_no_symlink_ancestors(path.parent)
    try:
        item = path.lstat()
    except (OSError, ValueError):
        _prepare_fail()
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_uid != os.geteuid()
        or item.st_nlink != 1
        or stat.S_IMODE(item.st_mode) not in {0o600, 0o644}
        or not 0 < item.st_size <= _PREPARE_MAX_SOURCE_FILE_BYTES
    ):
        _prepare_fail()
    return (
        item.st_dev,
        item.st_ino,
        item.st_uid,
        item.st_mode,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _prepare_open_directory(
    name: str,
    *,
    parent_fd: int,
    expected: tuple[int, int, int, int, int],
) -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        _prepare_fail()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except (OSError, ValueError):
        _prepare_fail()
    try:
        item = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        _prepare_fail()
    identity = (item.st_dev, item.st_ino, item.st_uid, item.st_mode, item.st_nlink)
    if not stat.S_ISDIR(item.st_mode) or identity != expected:
        os.close(descriptor)
        _prepare_fail()
    return descriptor


def _prepare_open_root(path: Path, expected: tuple[int, int, int, int, int]) -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        _prepare_fail()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
        item = os.fstat(descriptor)
    except (OSError, ValueError):
        _prepare_fail()
    identity = (item.st_dev, item.st_ino, item.st_uid, item.st_mode, item.st_nlink)
    if not stat.S_ISDIR(item.st_mode) or identity != expected:
        os.close(descriptor)
        _prepare_fail()
    return descriptor


def _prepare_read_regular(
    name: str,
    *,
    parent_fd: int,
    expected: tuple[int, int, int, int, int, int, int, int],
) -> bytes:
    if not hasattr(os, "O_NOFOLLOW"):
        _prepare_fail()
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
    except (OSError, ValueError):
        _prepare_fail()
    try:
        before = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_uid,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if not stat.S_ISREG(before.st_mode) or before_identity != expected:
            _prepare_fail()
        payload = bytearray()
        while len(payload) <= _PREPARE_MAX_SOURCE_FILE_BYTES:
            block = os.read(
                descriptor,
                min(64 * 1024, _PREPARE_MAX_SOURCE_FILE_BYTES + 1 - len(payload)),
            )
            if not block:
                break
            payload.extend(block)
        after = os.fstat(descriptor)
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_uid,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if (
            len(payload) != before.st_size
            or before_identity != after_identity
            or after_identity != expected
        ):
            _prepare_fail()
        return bytes(payload)
    except _PrepareSourceError:
        raise
    except OSError:
        _prepare_fail()
    finally:
        os.close(descriptor)


def _prepare_source_modules(installer_payload: bytes) -> tuple[str, ...]:
    try:
        tree = ast.parse(installer_payload)
    except (SyntaxError, ValueError):
        _prepare_fail()
    assignments = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "SOURCE_MODULES"
            for target in node.targets
        )
    ]
    if len(assignments) != 1:
        _prepare_fail()
    try:
        names = ast.literal_eval(assignments[0])
    except (SyntaxError, ValueError):
        _prepare_fail()
    if type(names) is not tuple or not 1 <= len(names) <= 64 or len(set(names)) != len(names):
        _prepare_fail()
    for name in names:
        if type(name) is not str or not name.endswith(".py"):
            _prepare_fail()
        stem = name[:-3]
        if not stem.isascii() or not stem.isidentifier() or stem.lower() != stem:
            _prepare_fail()
    if "__init__.py" not in names:
        _prepare_fail()
    return names


def _prepare_reject_source_bytecode(*directory_fds: int) -> None:
    try:
        for descriptor in directory_fds:
            with os.scandir(os.dup(descriptor)) as entries:
                if any(
                    entry.name == "__pycache__" or entry.name.endswith(".pyc")
                    for entry in entries
                ):
                    _prepare_fail()
    except _PrepareSourceError:
        raise
    except OSError:
        _prepare_fail()


def _prepare_after_closure_validation(_mutation_target: str) -> None:
    """Internal synchronization seam between closure validation and mutation."""


def _prepare_revalidate_source_closure(
    *,
    source_root: Path,
    directories: dict[str, Path],
    directory_identities: dict[str, tuple[int, int, int, int, int]],
    file_identities: dict[str, tuple[int, int, int, int, int, int, int, int]],
    source_modules: tuple[str, ...],
    root_fd: int,
    scripts_fd: int,
    src_fd: int,
    package_fd: int,
) -> None:
    directory_fds = {
        "root": root_fd,
        "scripts": scripts_fd,
        "src": src_fd,
        "package": package_fd,
    }
    for name, path in directories.items():
        expected = directory_identities[name]
        if _prepare_directory_identity(path) != expected:
            _prepare_fail()
        try:
            item = os.fstat(directory_fds[name])
        except OSError:
            _prepare_fail()
        if (
            not stat.S_ISDIR(item.st_mode)
            or (item.st_dev, item.st_ino, item.st_uid, item.st_mode, item.st_nlink)
            != expected
        ):
            _prepare_fail()
    _prepare_reject_source_bytecode(src_fd, package_fd)
    for relative in sorted(file_identities):
        if _prepare_file_identity(source_root / relative) != file_identities[relative]:
            _prepare_fail()
        if relative == "pyproject.toml":
            parent_fd, name = root_fd, relative
        elif relative == "scripts/install_integration_producer.py":
            parent_fd, name = scripts_fd, "install_integration_producer.py"
        else:
            parent_fd, name = package_fd, Path(relative).name
        _prepare_read_regular(
            name,
            parent_fd=parent_fd,
            expected=file_identities[relative],
        )
    if {
        relative.removeprefix("src/codex_usage/")
        for relative in file_identities
        if relative.startswith("src/codex_usage/")
    } != {"integration_installer.py", *source_modules}:
        _prepare_fail()


def _prepare_source_root(source_root_text: object) -> None:
    source_root = _prepare_absolute_path(source_root_text)
    script_path = _prepare_absolute_path(str(Path(__file__).absolute()))
    repo_root = script_path.parents[1]
    if source_root != repo_root:
        _prepare_fail()
    script_path = repo_root / "scripts" / "install_integration_producer.py"
    directories = {
        "root": source_root,
        "scripts": source_root / "scripts",
        "src": source_root / "src",
        "package": source_root / "src" / "codex_usage",
    }
    directory_identities = {
        name: _prepare_directory_identity(path) for name, path in directories.items()
    }
    source_files = {
        "pyproject.toml": source_root / "pyproject.toml",
        "scripts/install_integration_producer.py": script_path,
        "src/codex_usage/integration_installer.py": source_root
        / "src/codex_usage/integration_installer.py",
    }
    file_identities = {
        relative: _prepare_file_identity(path) for relative, path in source_files.items()
    }
    root_fd = scripts_fd = src_fd = package_fd = -1
    try:
        root_fd = _prepare_open_root(source_root, directory_identities["root"])
        scripts_fd = _prepare_open_directory(
            "scripts", parent_fd=root_fd, expected=directory_identities["scripts"]
        )
        src_fd = _prepare_open_directory(
            "src", parent_fd=root_fd, expected=directory_identities["src"]
        )
        package_fd = _prepare_open_directory(
            "codex_usage", parent_fd=src_fd, expected=directory_identities["package"]
        )
        installer_payload = _prepare_read_regular(
            "integration_installer.py",
            parent_fd=package_fd,
            expected=file_identities["src/codex_usage/integration_installer.py"],
        )
        source_modules = _prepare_source_modules(installer_payload)
        for filename in source_modules:
            relative = f"src/codex_usage/{filename}"
            path = source_root / relative
            file_identities[relative] = _prepare_file_identity(path)
        _prepare_reject_source_bytecode(src_fd, package_fd)

        _prepare_revalidate_source_closure(
            source_root=source_root,
            directories=directories,
            directory_identities=directory_identities,
            file_identities=file_identities,
            source_modules=source_modules,
            root_fd=root_fd,
            scripts_fd=scripts_fd,
            src_fd=src_fd,
            package_fd=package_fd,
        )
        _prepare_after_closure_validation("root")
        _prepare_revalidate_source_closure(
            source_root=source_root,
            directories=directories,
            directory_identities=directory_identities,
            file_identities=file_identities,
            source_modules=source_modules,
            root_fd=root_fd,
            scripts_fd=scripts_fd,
            src_fd=src_fd,
            package_fd=package_fd,
        )
        os.fchmod(root_fd, 0o700)
        root_after = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_after.st_mode)
            or root_after.st_dev != directory_identities["root"][0]
            or root_after.st_ino != directory_identities["root"][1]
            or root_after.st_uid != os.geteuid()
            or stat.S_IMODE(root_after.st_mode) != 0o700
        ):
            _prepare_fail()
        directory_identities["root"] = (
            root_after.st_dev,
            root_after.st_ino,
            root_after.st_uid,
            root_after.st_mode,
            root_after.st_nlink,
        )
        for relative in sorted(file_identities):
            if relative == "pyproject.toml":
                parent_fd, name = root_fd, relative
            elif relative == "scripts/install_integration_producer.py":
                parent_fd, name = scripts_fd, "install_integration_producer.py"
            else:
                parent_fd, name = package_fd, Path(relative).name
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
            try:
                before = os.fstat(descriptor)
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_uid,
                    before.st_mode,
                    before.st_nlink,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != file_identities[relative]:
                    _prepare_fail()
                _prepare_revalidate_source_closure(
                    source_root=source_root,
                    directories=directories,
                    directory_identities=directory_identities,
                    file_identities=file_identities,
                    source_modules=source_modules,
                    root_fd=root_fd,
                    scripts_fd=scripts_fd,
                    src_fd=src_fd,
                    package_fd=package_fd,
                )
                _prepare_after_closure_validation(relative)
                _prepare_revalidate_source_closure(
                    source_root=source_root,
                    directories=directories,
                    directory_identities=directory_identities,
                    file_identities=file_identities,
                    source_modules=source_modules,
                    root_fd=root_fd,
                    scripts_fd=scripts_fd,
                    src_fd=src_fd,
                    package_fd=package_fd,
                )
                current = os.fstat(descriptor)
                if (
                    current.st_dev,
                    current.st_ino,
                    current.st_uid,
                    current.st_mode,
                    current.st_nlink,
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                ) != file_identities[relative]:
                    _prepare_fail()
                os.fchmod(descriptor, 0o644)
                after = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(after.st_mode)
                    or after.st_dev != before.st_dev
                    or after.st_ino != before.st_ino
                    or after.st_uid != os.geteuid()
                    or after.st_nlink != 1
                    or after.st_size != before.st_size
                    or stat.S_IMODE(after.st_mode) != 0o644
                ):
                    _prepare_fail()
                file_identities[relative] = (
                    after.st_dev,
                    after.st_ino,
                    after.st_uid,
                    after.st_mode,
                    after.st_nlink,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _prepare_revalidate_source_closure(
            source_root=source_root,
            directories=directories,
            directory_identities=directory_identities,
            file_identities=file_identities,
            source_modules=source_modules,
            root_fd=root_fd,
            scripts_fd=scripts_fd,
            src_fd=src_fd,
            package_fd=package_fd,
        )
        os.fsync(root_fd)
    except _PrepareSourceError:
        raise
    except (OSError, ValueError):
        _prepare_fail()
    finally:
        for descriptor in (package_fd, src_fd, scripts_fd, root_fd):
            if descriptor >= 0:
                os.close(descriptor)


def _early_prepare_source(argv: list[str]) -> bool:
    if "--prepare-source" not in argv:
        return False
    if (
        len(argv) != 3
        or argv.count("--prepare-source") != 1
        or argv.count("--source-root") != 1
    ):
        raise _PrepareSourceArgumentError()
    source_root_index = argv.index("--source-root")
    if source_root_index == len(argv) - 1:
        raise _PrepareSourceArgumentError()
    _prepare_source_root(argv[source_root_index + 1])
    return True


def _require_source_directory(path: Path) -> None:
    item = path.lstat()
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISDIR(item.st_mode)
        or item.st_uid != os.geteuid()
        or stat.S_IMODE(item.st_mode) not in {0o700, 0o755}
    ):
        raise ValueError("installer source directory is unsafe")


def _require_source_file(path: Path) -> tuple[int, ...]:
    item = path.lstat()
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISREG(item.st_mode)
        or item.st_uid != os.geteuid()
        or stat.S_IMODE(item.st_mode) != 0o644
    ):
        raise ValueError("installer source file is unsafe")
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_uid,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
    )


def _read_source_file(path: Path) -> tuple[bytes, tuple[int, ...]]:
    expected = _require_source_file(path)
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("installer source requires no-follow support")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    try:
        opened = os.fstat(descriptor)
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_uid,
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if opened_identity != expected or not 0 < opened.st_size <= 2 * 1024 * 1024:
            raise ValueError("installer source file is unstable")
        payload = bytearray()
        while len(payload) <= 2 * 1024 * 1024:
            chunk = os.read(descriptor, min(64 * 1024, 2 * 1024 * 1024 + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) != opened.st_size or opened_identity != (
            (after := os.fstat(descriptor)).st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("installer source file changed during read")
        return bytes(payload), expected
    finally:
        os.close(descriptor)


def _declared_source_modules(installer_payload: bytes) -> tuple[str, ...]:
    tree = ast.parse(installer_payload)
    assignments = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "SOURCE_MODULES"
            for target in node.targets
        )
    ]
    if len(assignments) != 1:
        raise ValueError("installer source module declaration is unavailable")
    names = ast.literal_eval(assignments[0])
    if type(names) is not tuple or not 1 <= len(names) <= 64 or len(set(names)) != len(names):
        raise ValueError("installer source module declaration is invalid")
    for name in names:
        if type(name) is not str or not name.endswith(".py"):
            raise ValueError("installer source module name is invalid")
        stem = name[:-3]
        if not stem.isascii() or not stem.isidentifier() or stem.lower() != stem:
            raise ValueError("installer source module name is invalid")
    if "__init__.py" not in names:
        raise ValueError("installer package initializer is unavailable")
    return names


def _local_imports(payload: bytes) -> set[str]:
    dependencies: set[str] = set()
    for node in ast.walk(ast.parse(payload)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "codex_usage":
                    dependencies.add("__init__")
                elif alias.name.startswith("codex_usage."):
                    relative = alias.name.removeprefix("codex_usage.").split(".")
                    if len(relative) != 1:
                        raise ValueError("nested installer package import is unsafe")
                    dependencies.add(relative[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level != 1:
                    raise ValueError("parent-relative installer import is unsafe")
                if node.module:
                    relative = node.module.split(".")
                    if len(relative) != 1:
                        raise ValueError("nested installer package import is unsafe")
                    dependencies.add(relative[0])
                else:
                    dependencies.update(alias.name.split(".")[0] for alias in node.names)
            elif node.module == "codex_usage":
                dependencies.update(alias.name for alias in node.names)
            elif node.module and node.module.startswith("codex_usage."):
                relative = node.module.removeprefix("codex_usage.").split(".")
                if len(relative) != 1:
                    raise ValueError("nested installer package import is unsafe")
                dependencies.add(relative[0])
    return dependencies


def _validate_loaded_modules(
    expected_modules: dict[str, tuple[Path, tuple[int, ...]]],
    source_finder: _ValidatedSourceFinder,
) -> None:
    for module_name, (expected_path, expected_identity) in expected_modules.items():
        module = sys.modules.get(module_name)
        loader = source_finder.loader_for(module_name)
        source = source_finder.source_for(module_name)
        if (
            type(module) is not ModuleType
            or type(loader) is not _ValidatedSourceLoader
            or source is None
        ):
            raise ValueError("installer module loader is unavailable")
        source_path, payload, digest = source
        spec = vars(module).get("__spec__")
        origin = vars(module).get("__file__")
        if (
            type(spec) is not importlib.machinery.ModuleSpec
            or spec.loader is not loader
            or vars(module).get("__loader__") is not loader
            or spec.origin != str(expected_path)
            or source_path != expected_path
            or loader.name != module_name
            or loader.path != str(expected_path)
            or loader._payload is not payload
            or loader._digest != digest
            or hashlib.sha256(payload).digest() != digest
            or type(origin) is not str
            or not Path(origin).is_absolute()
            or Path(origin) != expected_path
            or Path(origin).resolve(strict=True) != expected_path
            or _require_source_file(expected_path) != expected_identity
        ):
            raise ValueError("preloaded installer module has foreign origin")


class _ValidatedSourceLoader(importlib.machinery.SourceFileLoader):
    def __init__(
        self,
        fullname: str,
        path: str,
        payload: bytes,
        digest: bytes,
    ) -> None:
        super().__init__(fullname, path)
        self._payload = payload
        self._digest = digest

    def get_code(self, fullname: str):
        if (
            fullname != self.name
            or hashlib.sha256(self._payload).digest() != self._digest
        ):
            raise ImportError("installer source loader path is unsafe")
        return compile(self._payload, self.path, "exec", dont_inherit=True)


class _ValidatedSourceFinder(importlib.abc.MetaPathFinder):
    def __init__(
        self,
        package_root: Path,
        sources: dict[str, tuple[Path, bytes, bytes]],
    ) -> None:
        self._package_root = package_root
        self._sources = MappingProxyType(dict(sources))
        self._loaders: dict[str, _ValidatedSourceLoader] = {}

    def find_spec(self, fullname: str, path=None, target=None):
        if fullname != "codex_usage" and not fullname.startswith("codex_usage."):
            return None
        source = self._sources.get(fullname)
        if source is None:
            raise ImportError("installer import is outside validated closure")
        source_path, payload, digest = source
        loader = _ValidatedSourceLoader(fullname, str(source_path), payload, digest)
        self._loaders[fullname] = loader
        spec = importlib.util.spec_from_file_location(
            fullname,
            source_path,
            loader=loader,
            submodule_search_locations=(
                [str(self._package_root)] if fullname == "codex_usage" else None
            ),
        )
        if spec is None:
            raise ImportError("installer source spec is unavailable")
        return spec

    def loader_for(self, fullname: str) -> _ValidatedSourceLoader | None:
        return self._loaders.get(fullname)

    def source_for(self, fullname: str) -> tuple[Path, bytes, bytes] | None:
        return self._sources.get(fullname)


def _install_source_only_finders(
    source_root: Path,
    package_root: Path,
    sources: dict[str, tuple[Path, bytes, bytes]],
) -> None:
    def loader(fullname: str, path: str) -> _ValidatedSourceLoader:
        source = sources.get(fullname)
        if source is None or str(source[0]) != path:
            raise ImportError("installer source is outside validated closure")
        return _ValidatedSourceLoader(fullname, path, source[1], source[2])

    for directory in (source_root, package_root):
        directory_text = str(directory)
        sys.path_importer_cache.pop(directory_text, None)
        sys.path_importer_cache[directory_text] = importlib.machinery.FileFinder(
            directory_text,
            (loader, importlib.machinery.SOURCE_SUFFIXES),
        )


def _require_source_only_runtime(source_root: Path, package_root: Path) -> None:
    if (
        not sys.dont_write_bytecode
        or os.environ.get("PYTHONDONTWRITEBYTECODE") != "1"
        or sys.pycache_prefix is not None
    ):
        raise ValueError("installer bytecode suppression is unavailable")
    if any(
        type(name) is str and (name == "codex_usage" or name.startswith("codex_usage."))
        for name in sys.modules
    ):
        raise ValueError("installer package is already loaded")
    for directory in (source_root, package_root):
        with os.scandir(directory) as entries:
            if any(entry.name == "__pycache__" or entry.name.endswith(".pyc") for entry in entries):
                raise ValueError("installer source bytecode cache is unsafe")


def _bootstrap_repo_source() -> tuple[
    dict[str, tuple[Path, tuple[int, ...]]],
    _ValidatedSourceFinder,
    list[object],
    tuple[object, ...],
]:
    script_path = Path(__file__).absolute()
    if script_path.resolve(strict=True) != script_path:
        raise ValueError("installer entrypoint must not use symlinks")
    if (
        script_path.name != "install_integration_producer.py"
        or script_path.parent.name != "scripts"
    ):
        raise ValueError("installer entrypoint has unexpected layout")

    repo_root = script_path.parents[1]
    source_root = repo_root / "src"
    package_root = source_root / "codex_usage"
    for directory in (repo_root, script_path.parent, source_root, package_root):
        _require_source_directory(directory)
    _require_source_file(script_path)
    _require_source_file(repo_root / "pyproject.toml")
    _require_source_only_runtime(source_root, package_root)

    installer_path = package_root / "integration_installer.py"
    installer_payload, installer_identity = _read_source_file(installer_path)
    source_modules = _declared_source_modules(installer_payload)
    payloads = {"integration_installer": installer_payload}
    sources = {
        "codex_usage.integration_installer": (
            installer_path,
            installer_payload,
            hashlib.sha256(installer_payload).digest(),
        ),
    }
    expected_modules = {
        "codex_usage.integration_installer": (installer_path, installer_identity),
    }
    for filename in source_modules:
        module_path = package_root / filename
        payload, identity = _read_source_file(module_path)
        stem = filename[:-3]
        payloads[stem] = payload
        module_name = "codex_usage" if stem == "__init__" else f"codex_usage.{stem}"
        sources[module_name] = (
            module_path,
            payload,
            hashlib.sha256(payload).digest(),
        )
        expected_modules[module_name] = (module_path, identity)

    declared_stems = set(payloads)
    for payload in payloads.values():
        if not _local_imports(payload) <= declared_stems:
            raise ValueError("installer local import is absent from source closure")

    source_text = str(source_root)
    if sys.path[:1] != [source_text]:
        sys.path.insert(0, source_text)
    _install_source_only_finders(source_root, package_root, sources)
    source_finder = _ValidatedSourceFinder(package_root, sources)
    if type(sys.meta_path) is not list:
        raise ValueError("installer meta path is unsafe")
    meta_path = sys.meta_path
    ambient_meta_path = tuple(meta_path)
    meta_path[:] = [
        source_finder,
        importlib.machinery.BuiltinImporter,
        importlib.machinery.FrozenImporter,
        importlib.machinery.PathFinder,
    ]
    return expected_modules, source_finder, meta_path, ambient_meta_path


def _restore_guarded_meta_path(
    source_finder: _ValidatedSourceFinder,
    meta_path: list[object],
    ambient_meta_path: tuple[object, ...],
) -> None:
    meta_path[:] = [
        source_finder,
        *(finder for finder in ambient_meta_path if finder is not source_finder),
    ]
    sys.meta_path = meta_path


try:
    if _early_prepare_source(sys.argv[1:]):
        sys.stdout.write(_PREPARE_SOURCE_SUCCESS)
        raise SystemExit(0)
except _PrepareSourceArgumentError:
    sys.stderr.write("integration_producer_unavailable\n")
    raise SystemExit(64) from None
except _PrepareSourceError:
    sys.stderr.write("integration_producer_source_prepare_rejected\n")
    raise SystemExit(69) from None


try:
    (
        _EXPECTED_MODULES,
        _SOURCE_FINDER,
        _META_PATH,
        _AMBIENT_META_PATH,
    ) = _bootstrap_repo_source()
except (OSError, RuntimeError, SyntaxError, TypeError, ValueError):
    sys.stderr.write("integration_producer_unavailable\n")
    raise SystemExit(69) from None


try:
    try:
        from codex_usage.integration_installer import (
            IntegrationCleanupError,
            IntegrationInstallError,
            install_release,
            rollback_active_release,
        )

        for _module_name in _EXPECTED_MODULES:
            if _module_name not in sys.modules:
                importlib.import_module(_module_name)
        _validate_loaded_modules(_EXPECTED_MODULES, _SOURCE_FINDER)
    finally:
        _restore_guarded_meta_path(
            _SOURCE_FINDER,
            _META_PATH,
            _AMBIENT_META_PATH,
        )
except (ImportError, OSError, RuntimeError, SyntaxError, TypeError, ValueError):
    sys.stderr.write("integration_producer_unavailable\n")
    raise SystemExit(69) from None


class _InstallerArgumentError(Exception):
    pass


class _InstallerParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        if parsed.prepare_source:
            if parsed.rollback or any(
                value is not None
                for value in (
                    parsed.state_home,
                    parsed.data_home,
                    parsed.python,
                    parsed.temporary_root,
                )
            ):
                self.error("--prepare-source only accepts --source-root")
            if parsed.source_root is None:
                self.error("--prepare-source requires --source-root")
            return parsed
        install_values = (
            parsed.source_root,
            parsed.python,
            parsed.temporary_root,
        )
        if parsed.rollback:
            if any(value is not None for value in install_values):
                self.error("--rollback cannot use install arguments")
            if parsed.state_home is None or parsed.data_home is None:
                self.error("--rollback requires --state-home and --data-home")
        elif any(
            value is None
            for value in (
                parsed.source_root,
                parsed.state_home,
                parsed.data_home,
                parsed.python,
                parsed.temporary_root,
            )
        ):
            self.error("install requires all absolute path arguments")
        return parsed

    def error(self, message):
        raise _InstallerArgumentError()


def _parser() -> argparse.ArgumentParser:
    parser = _InstallerParser(add_help=True)
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--prepare-source", action="store_true")
    parser.add_argument("--source-root")
    parser.add_argument("--state-home")
    parser.add_argument("--data-home")
    parser.add_argument("--python")
    parser.add_argument("--temporary-root")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.prepare_source:
            _prepare_source_root(args.source_root)
            sys.stdout.write(_PREPARE_SOURCE_SUCCESS)
        elif args.rollback:
            rollback_active_release(
                state_home=Path(args.state_home),
                data_home=Path(args.data_home),
            )
            sys.stdout.write("integration_producer_rollback_ok\n")
        else:
            install_release(
                source_root=Path(args.source_root),
                state_home=Path(args.state_home),
                data_home=Path(args.data_home),
                python_executable=Path(args.python),
                temporary_root=Path(args.temporary_root),
            )
            sys.stdout.write("integration_producer_install_ok\n")
        return 0
    except _InstallerArgumentError:
        sys.stderr.write("integration_producer_unavailable\n")
        return 64
    except SystemExit:
        raise
    except IntegrationCleanupError:
        sys.stderr.write("integration_producer_cleanup_failed\n")
        return 70
    except _PrepareSourceError:
        sys.stderr.write("integration_producer_source_prepare_rejected\n")
        return 69
    except (IntegrationInstallError, OSError, ValueError):
        sys.stderr.write("integration_producer_unavailable\n")
        return 69


if __name__ == "__main__":
    raise SystemExit(main())
