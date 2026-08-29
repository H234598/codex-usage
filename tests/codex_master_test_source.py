from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from pathlib import Path

import pytest


@dataclass(frozen=True)
class CodexMasterTestSource:
    root: Path
    source: Path
    tests: Path | None


def resolve_codex_master_source(
    *,
    environ: Mapping[str, str] = os.environ,
    find_spec: Callable[[str], ModuleSpec | None] = importlib.util.find_spec,
) -> CodexMasterTestSource | None:
    configured = environ.get("CODEX_MASTER_ROOT")
    if configured is not None:
        root = Path(configured)
        if not root.is_absolute():
            return None
        return _source_at(root)

    try:
        spec = find_spec("codex_master")
    except (ImportError, AttributeError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    package = Path(next(iter(spec.submodule_search_locations)))
    source = package.parent
    root = source.parent if source.name == "src" else source
    tests = root / "tests"
    return CodexMasterTestSource(root, source, tests if tests.is_dir() else None)


@contextmanager
def codex_master_test_source(
    *, require_tests: bool, module_level: bool = False
) -> Iterator[CodexMasterTestSource]:
    source = resolve_codex_master_source()
    if source is None or (require_tests and source.tests is None):
        pytest.skip(
            "codex-master cross-repo source unavailable; set CODEX_MASTER_ROOT",
            allow_module_level=module_level,
        )
    original_path = sys.path[:]
    try:
        sys.path.insert(0, str(source.source))
        if require_tests:
            assert source.tests is not None
            sys.path.insert(0, str(source.tests))
        yield source
    finally:
        sys.path[:] = original_path


def _source_at(root: Path) -> CodexMasterTestSource | None:
    source = root / "src"
    package = source / "codex_master" / "__init__.py"
    if not package.is_file():
        return None
    tests = root / "tests"
    return CodexMasterTestSource(root, source, tests if tests.is_dir() else None)
