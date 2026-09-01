from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
APPLET_DIR = ROOT / "files" / "codex-usage@H234598"


def _module():
    sys.path.insert(0, str(APPLET_DIR))
    sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings")
    sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings/bin")
    path = APPLET_DIR / "pool_authority_page.py"
    spec = importlib.util.spec_from_file_location("pool_authority_page", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _authority(account_id: str = "synthetic-account") -> dict[str, object]:
    return {
        "account_id": account_id,
        "pool_id": "synthetic-pool",
        "provider": "synthetic",
        "hive_available": True,
        "allowed_model_families": ["sol", "terra"],
        "reasoning_minimum": "medium",
        "reasoning_maximum": "xhigh",
        "allowed_lifecycles": ["persistent", "session"],
        "persistent_leadership_eligible": True,
        "long_running_leadership_eligible": False,
    }


class _Snapshot:
    def __init__(self, generation: int, authorities: list[dict[str, object]]) -> None:
        self.generation = generation
        self.authorities = authorities


def test_owner_ui_model_round_trips_every_explicit_authority_field() -> None:
    module = _module()
    model = module.PoolAuthorityOwnerModel()
    original = _authority()

    model.render(_Snapshot(7, [original]))

    assert model.generation == 7
    assert model.authorities == (_authority(),)
    assert model.draft() == [_authority()]
    assert "account_id" not in model.editable_fields
    assert set(model.editable_fields) == set(original) - {"account_id"}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("provider"),
        lambda value: value.__setitem__("unexpected", True),
        lambda value: value.__setitem__("hive_available", "true"),
        lambda value: value.__setitem__("reasoning_minimum", "ultra"),
        lambda value: value.__setitem__("allowed_lifecycles", ["session", "session"]),
    ],
)
def test_owner_ui_model_rejects_invalid_authority_and_fails_closed(mutation) -> None:
    module = _module()
    model = module.PoolAuthorityOwnerModel()
    malformed = _authority()
    mutation(malformed)

    with pytest.raises(ValueError, match="authority"):
        model.render(_Snapshot(7, [malformed]))

    assert model.authorities == ()
    assert model.generation is None


def test_owner_ui_model_rejects_unsorted_or_duplicate_account_inventory() -> None:
    module = _module()
    model = module.PoolAuthorityOwnerModel()

    with pytest.raises(ValueError, match="inventory"):
        model.render(
            _Snapshot(7, [_authority("synthetic-z"), _authority("synthetic-a")])
        )
    with pytest.raises(ValueError, match="inventory"):
        model.render(_Snapshot(7, [_authority(), _authority()]))

    assert model.authorities == ()


def test_owner_ui_controller_rejects_stale_callback_generation() -> None:
    module = _module()
    controller = module.PoolAuthorityOwnerController(module.PoolAuthorityOwnerModel())
    stale_epoch = controller.begin_load()
    current_epoch = controller.begin_load()

    assert controller.receive_load(stale_epoch, _Snapshot(1, [_authority()])) is False
    assert controller.model.authorities == ()
    assert controller.receive_load(current_epoch, _Snapshot(2, [_authority()])) is True
    save_epoch, expected_generation, authorities = controller.begin_save()
    assert expected_generation == 2
    assert authorities == [_authority()]
    assert controller.receive_save(save_epoch - 1, _Snapshot(3, [_authority()])) is False
    assert controller.model.generation == 2


def test_owner_ui_displays_validation_error_and_does_not_call_save() -> None:
    module = _module()

    class Actions:
        def __init__(self) -> None:
            self.saved = False

        def load(self):
            return _Snapshot(5, [_authority()])

        def save(self, _authorities, *, expected_generation):
            self.saved = True
            raise AssertionError(f"unexpected save at generation {expected_generation}")

    page = module.PoolAuthorityOwnerPage(None, None, None)
    actions = Actions()
    page._actions = actions
    page._refresh()
    page._entries["synthetic-account"]["pool_id"].set_text("INVALID POOL")

    page._save()

    assert actions.saved is False
    assert "Ungültige" in page._status.get_text()
    page.destroy()


def test_owner_ui_actions_adapt_config_owner_records_on_load_and_save(monkeypatch) -> None:
    module = _module()

    class Owner:
        def __init__(self, **values) -> None:
            self.values = values

        def to_source_record(self):
            return dict(self.values)

    captured = []
    original = _authority()
    backend = SimpleNamespace(
        PoolAuthorityOwner=Owner,
        load_pool_authority_owner=lambda: _Snapshot(8, [Owner(**original)]),
        save_pool_authority_owner=lambda authorities, *, expected_generation: captured.append(
            (authorities, expected_generation)
        )
        or _Snapshot(9, authorities),
    )
    monkeypatch.setattr(module.importlib, "import_module", lambda name: backend)
    actions = module.PoolAuthorityOwnerActions()
    model = module.PoolAuthorityOwnerModel()

    model.render(actions.load())
    saved = actions.save(model.draft(), expected_generation=8)

    assert len(captured) == 1
    records, generation = captured[0]
    assert generation == 8
    assert [record.to_source_record() for record in records] == [original]
    assert saved.generation == 9


def test_pool_authority_navigation_is_an_accounts_subpage() -> None:
    schema = json.loads((APPLET_DIR / "settings-schema.json").read_text(encoding="utf-8"))
    layout = schema["layout"]
    account_pages = [
        page for page in layout["pages"] if layout[page]["title"].startswith("Accounts")
    ]

    assert account_pages == [
        "openai-accounts-page",
        "google-accounts-page",
        "pool-authority-accounts-page",
    ]
    assert layout["pool-authority-accounts-page"]["title"] == "Accounts · Pool Authority"
    assert layout["pool-authority-section"]["keys"] == ["pool-authority-owner"]
    assert schema["pool-authority-owner"] == {
        "type": "custom",
        "file": "pool_authority_page.py",
        "widget": "PoolAuthorityOwnerPage",
        "description": "PoolAuthority-Ownerwerte je Account",
        "tooltip": "Bearbeitet nur die kanonische config.toml-Authority. Ohne vollständige, gültige Account-Parität wird nicht gespeichert oder publiziert.",
        "default": "",
    }
