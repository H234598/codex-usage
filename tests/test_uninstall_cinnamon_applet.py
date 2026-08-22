from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "uninstall_cinnamon_applet.py"
spec = importlib.util.spec_from_file_location("uninstall_cinnamon_applet", SCRIPT)
assert spec is not None and spec.loader is not None
uninstaller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uninstaller)


def test_dry_run_and_missing_target_are_idempotent(tmp_path: Path, capsys) -> None:
    target_root = tmp_path / "applets"

    assert uninstaller.main(["--target-root", str(target_root), "--dry-run"]) == 0
    assert not target_root.exists()
    assert "status=dry-run" in capsys.readouterr().out

    assert uninstaller.main(["--target-root", str(target_root)]) == 0
    assert "status=not-installed" in capsys.readouterr().out


def test_main_removes_owned_target(tmp_path: Path, capsys) -> None:
    target_root = tmp_path / "applets"
    target = target_root / uninstaller.APPLET_UUID
    target.mkdir(parents=True)
    (target / "applet.js").write_text("owned", encoding="utf-8")

    assert uninstaller.main(["--target-root", str(target_root)]) == 0
    assert not target.exists()
    assert "status=uninstalled" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_main_refuses_unsafe_target(tmp_path: Path, capsys, kind: str) -> None:
    target_root = tmp_path / "applets"
    target_root.mkdir()
    target = target_root / uninstaller.APPLET_UUID
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        sentinel = outside / "keep"
        sentinel.write_text("keep", encoding="utf-8")
        target.symlink_to(outside, target_is_directory=True)
    else:
        sentinel = target
        sentinel.write_text("keep", encoding="utf-8")

    assert uninstaller.main(["--target-root", str(target_root)]) == 1
    assert "refusing" in capsys.readouterr().err
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_main_reports_rmtree_failure_without_claiming_success(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    target_root = tmp_path / "applets"
    target = target_root / uninstaller.APPLET_UUID
    target.mkdir(parents=True)

    def fail_rmtree(_path: Path) -> None:
        raise OSError("busy")

    monkeypatch.setattr(uninstaller.shutil, "rmtree", fail_rmtree)

    assert uninstaller.main(["--target-root", str(target_root)]) == 1
    assert target.is_dir()
    assert "busy" in capsys.readouterr().err


def test_main_rejects_unsafe_target_root(tmp_path: Path, capsys) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target_root = tmp_path / "redirected"
    target_root.symlink_to(outside, target_is_directory=True)

    assert uninstaller.main(["--target-root", str(target_root)]) == 1
    assert "symlink" in capsys.readouterr().err


def test_main_rejects_non_directory_target_root(tmp_path: Path, capsys) -> None:
    target_root = tmp_path / "applets"
    target_root.write_text("not a directory", encoding="utf-8")

    assert uninstaller.main(["--target-root", str(target_root)]) == 1
    assert "non-directory" in capsys.readouterr().err


def test_directory_chain_rejects_existing_file(tmp_path: Path) -> None:
    file_path = tmp_path / "file"
    file_path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="non-directory"):
        uninstaller._assert_real_directory_chain(file_path / "child")
