from __future__ import annotations

from pathlib import Path


def test_ci_workflow_uses_python_module_invocations_and_dependency_check():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "permissions:\n  contents: read" in workflow
    assert 'PYTHONSAFEPATH: "1"' in workflow
    assert "python -m pip check" in workflow
    assert "python -m ruff check ." in workflow
    assert "python -m pytest" in workflow
    assert "\n        run: ruff check ." not in workflow
    assert "\n        run: pytest\n" not in workflow


def test_ci_workflow_has_concurrency_control():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "concurrency:" in workflow
    assert "cancel-in-progress: true" in workflow
