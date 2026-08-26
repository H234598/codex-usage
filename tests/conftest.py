from __future__ import annotations

import multiprocessing.util
import os
import subprocess
from pathlib import Path

import pytest

import codex_usage.private_io as private_io

_REAL_SCANDIR = os.scandir


def _lock_names(root: Path) -> frozenset[str]:
    try:
        with _REAL_SCANDIR(root) as entries:
            return frozenset(entry.name for entry in entries)
    except FileNotFoundError:
        return frozenset()


@pytest.fixture(scope="session", autouse=True)
def isolate_private_lock_root(tmp_path_factory, request):
    """Keep persistent test locks out of the invoking user's product root."""
    production_root = private_io._private_lock_root()
    request.config._private_lock_production_root = production_root
    before = _lock_names(production_root)
    test_root = tmp_path_factory.mktemp("private-lock-root")
    patch = pytest.MonkeyPatch()
    patch.setattr(private_io, "_private_lock_root", lambda: test_root)
    real_popen = subprocess.Popen

    def isolated_popen(args, *popen_args, **popen_kwargs):
        command = args if isinstance(args, (list, tuple)) else ()
        if command and Path(str(command[0])).name == "codex-usage":
            args = [
                "/usr/bin/bwrap",
                "--bind",
                "/",
                "/",
                "--dir",
                str(production_root),
                "--bind",
                str(test_root),
                str(production_root),
                "--",
                *command,
            ]
        return real_popen(args, *popen_args, **popen_kwargs)

    patch.setattr(subprocess, "Popen", isolated_popen)
    real_spawnv_passfds = multiprocessing.util.spawnv_passfds

    def isolated_spawnv_passfds(path, args, passfds):
        def convert(value):
            return value.encode() if isinstance(path, bytes) else value

        return real_spawnv_passfds(
            convert("/usr/bin/bwrap"),
            [
                *(convert(value) for value in (
                    "/usr/bin/bwrap",
                    "--bind",
                    "/",
                    "/",
                    "--dir",
                    str(production_root),
                    "--bind",
                    str(test_root),
                    str(production_root),
                    "--",
                )),
                *args,
            ],
            passfds,
        )

    patch.setattr(multiprocessing.util, "spawnv_passfds", isolated_spawnv_passfds)

    def verify_isolation():
        after = _lock_names(production_root)
        added = sorted(after - before)
        patch.undo()
        if added:
            raise AssertionError(
                "tests wrote persistent locks into product root: "
                + ", ".join(added[:8])
            )

    request.addfinalizer(verify_isolation)


@pytest.fixture(autouse=True)
def verify_test_lock_isolation(request):
    production_root = request.config._private_lock_production_root
    before = _lock_names(production_root)

    def verify_test():
        after = _lock_names(production_root)
        added = sorted(after - before)
        if added:
            raise AssertionError(
                "test wrote persistent locks into product root: "
                + ", ".join(added[:8])
            )

    request.addfinalizer(verify_test)


@pytest.fixture(autouse=True)
def isolate_cli_data_home(tmp_path_factory, monkeypatch, request):
    """Keep CLI tests from writing into the invoking user's data directory."""
    module_name = request.module.__name__.rsplit(".", 1)[-1]
    if module_name in {"test_cli", "test_history_cli", "test_profile_cli"}:
        root = tmp_path_factory.mktemp("cli-xdg")
        monkeypatch.setenv("XDG_DATA_HOME", str(root / "data"))
        monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
