from __future__ import annotations

import importlib.machinery
import sys
from pathlib import Path

import pytest
from codex_master_test_source import (
    codex_master_test_source,
    resolve_codex_master_source,
)


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "codex-master"
    package = root / "src" / "codex_master"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    return root


def test_explicit_codex_master_root_is_portable_and_activates_src_and_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _checkout(tmp_path)
    monkeypatch.setenv("CODEX_MASTER_ROOT", str(root))
    original_path = sys.path[:]
    with codex_master_test_source(require_tests=True) as source:
        assert source.root == root
        assert sys.path[:2] == [str(root / "tests"), str(root / "src")]
    assert sys.path == original_path


def test_explicit_missing_codex_master_root_skips_instead_of_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing"
    monkeypatch.setenv("CODEX_MASTER_ROOT", str(missing))

    with pytest.raises(pytest.skip.Exception, match="CODEX_MASTER_ROOT"):
        with codex_master_test_source(require_tests=True):
            pass


def test_installed_codex_master_package_is_resolved_without_local_checkout_path(
    tmp_path: Path,
) -> None:
    root = _checkout(tmp_path)
    spec = importlib.machinery.ModuleSpec(
        "codex_master",
        loader=None,
        origin=str(root / "src" / "codex_master" / "__init__.py"),
        is_package=True,
    )
    spec.submodule_search_locations = [str(root / "src" / "codex_master")]

    source = resolve_codex_master_source(environ={}, find_spec=lambda _name: spec)

    assert source is not None
    assert source.root == root
    assert source.tests == root / "tests"
