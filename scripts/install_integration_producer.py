from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _bootstrap_repo_source() -> None:
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
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("installer source directory is unsafe")
    for file_path in (
        script_path,
        repo_root / "pyproject.toml",
        package_root / "__init__.py",
        package_root / "integration_installer.py",
    ):
        if file_path.is_symlink() or not file_path.is_file():
            raise ValueError("installer source file is unsafe")

    source_text = str(source_root)
    if sys.path[:1] != [source_text]:
        sys.path.insert(0, source_text)


try:
    _bootstrap_repo_source()
except (OSError, RuntimeError, ValueError):
    sys.stderr.write("integration_producer_unavailable\n")
    raise SystemExit(69) from None


from codex_usage.integration_installer import (  # noqa: E402
    IntegrationCleanupError,
    IntegrationInstallError,
    install_release,
    rollback_active_release,
)


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
