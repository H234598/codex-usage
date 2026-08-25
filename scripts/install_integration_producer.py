from __future__ import annotations

import argparse
import ast
import os
import stat
import sys
from pathlib import Path
from types import ModuleType


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
) -> None:
    for module_name, (expected_path, expected_identity) in expected_modules.items():
        if module_name not in sys.modules:
            continue
        module = sys.modules[module_name]
        if type(module) is not ModuleType:
            raise ValueError("preloaded installer module is unsafe")
        origin = vars(module).get("__file__")
        if (
            type(origin) is not str
            or not Path(origin).is_absolute()
            or Path(origin) != expected_path
            or Path(origin).resolve(strict=True) != expected_path
            or _require_source_file(expected_path) != expected_identity
        ):
            raise ValueError("preloaded installer module has foreign origin")


def _bootstrap_repo_source() -> dict[str, tuple[Path, tuple[int, ...]]]:
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

    installer_path = package_root / "integration_installer.py"
    installer_payload, installer_identity = _read_source_file(installer_path)
    source_modules = _declared_source_modules(installer_payload)
    payloads = {"integration_installer": installer_payload}
    expected_modules = {
        "codex_usage.integration_installer": (installer_path, installer_identity),
    }
    for filename in source_modules:
        module_path = package_root / filename
        payload, identity = _read_source_file(module_path)
        stem = filename[:-3]
        payloads[stem] = payload
        module_name = "codex_usage" if stem == "__init__" else f"codex_usage.{stem}"
        expected_modules[module_name] = (module_path, identity)

    declared_stems = set(payloads)
    for payload in payloads.values():
        if not _local_imports(payload) <= declared_stems:
            raise ValueError("installer local import is absent from source closure")
    _validate_loaded_modules(expected_modules)

    source_text = str(source_root)
    if sys.path[:1] != [source_text]:
        sys.path.insert(0, source_text)
    return expected_modules


try:
    _EXPECTED_MODULES = _bootstrap_repo_source()
except (OSError, RuntimeError, SyntaxError, TypeError, ValueError):
    sys.stderr.write("integration_producer_unavailable\n")
    raise SystemExit(69) from None


try:
    from codex_usage.integration_installer import (
        IntegrationCleanupError,
        IntegrationInstallError,
        install_release,
        rollback_active_release,
    )

    _validate_loaded_modules(_EXPECTED_MODULES)
except (ImportError, OSError, RuntimeError, SyntaxError, TypeError, ValueError):
    sys.stderr.write("integration_producer_unavailable\n")
    raise SystemExit(69) from None


class _InstallerArgumentError(Exception):
    pass


class _InstallerParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
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
    parser.add_argument("--source-root")
    parser.add_argument("--state-home")
    parser.add_argument("--data-home")
    parser.add_argument("--python")
    parser.add_argument("--temporary-root")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.rollback:
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
    except (IntegrationInstallError, OSError, ValueError):
        sys.stderr.write("integration_producer_unavailable\n")
        return 69


if __name__ == "__main__":
    raise SystemExit(main())
