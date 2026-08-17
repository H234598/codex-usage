from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_cli_data_home(tmp_path_factory, monkeypatch, request):
    """Keep CLI tests from writing into the invoking user's data directory."""
    module_name = request.module.__name__.rsplit(".", 1)[-1]
    if module_name in {"test_cli", "test_history_cli", "test_profile_cli"}:
        root = tmp_path_factory.mktemp("cli-xdg")
        monkeypatch.setenv("XDG_DATA_HOME", str(root / "data"))
        monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
