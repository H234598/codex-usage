from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLET_DIR = ROOT / "files" / "codex-usage@H234598"
sys.path.insert(0, str(APPLET_DIR))
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings")
sys.path.insert(0, "/usr/share/cinnamon/cinnamon-settings/bin")

from fast_mode_icon_selector import FastModeIconSelector, _load_icon  # noqa: E402


class _Combo:
    def __init__(self, active: int = -1):
        self.active = active
        self.tooltip = None

    def set_active(self, active: int) -> None:
        self.active = active

    def get_active_iter(self):
        return self.active if self.active >= 0 else None

    def set_tooltip_text(self, text):
        self.tooltip = text


class _Store:
    def __init__(self, values: list[str]):
        self.values = values

    def get_value(self, iterator, column):
        assert column == 2
        return self.values[iterator]


def _selector() -> FastModeIconSelector:
    selector = FastModeIconSelector.__new__(FastModeIconSelector)
    selector._values = ["one.svg", "two.svg"]
    selector._tooltips = {
        "one.svg": "Erstes Symbol: one.svg",
        "two.svg": "Zweites Symbol: two.svg",
    }
    selector.combo = _Combo()
    selector.store = _Store(selector._values)
    selector._saving = False
    selector.saved = []
    selector.set_value = selector.saved.append
    return selector


def test_icon_selector_roundtrips_selection_and_tooltip() -> None:
    selector = _selector()

    selector.set_widget_value("two.svg")
    assert selector.combo.active == 1
    assert selector.get_widget_value() == "two.svg"
    assert selector.combo.tooltip == "Zweites Symbol: two.svg"

    selector._on_changed()
    assert selector.saved == ["two.svg"]

    class _Tooltip:
        text = None

        def set_text(self, value):
            self.text = value

    tooltip = _Tooltip()
    assert selector._on_query_tooltip(None, 0, 0, False, tooltip) is True
    assert tooltip.text == "Zweites Symbol: two.svg"


def test_icon_selector_falls_back_to_first_icon_for_unknown_value() -> None:
    selector = _selector()

    selector.set_widget_value("missing.svg")

    assert selector.combo.active == 0
    assert selector.get_widget_value() == "one.svg"
    assert selector.combo.tooltip == "Erstes Symbol: one.svg"


def test_icon_selector_does_not_save_while_settings_are_being_applied() -> None:
    selector = _selector()
    selector.set_widget_value("one.svg")
    selector._saving = True

    selector._on_changed()

    assert selector.saved == []


def test_icon_selector_does_not_write_when_settings_reload_updates_combo() -> None:
    selector = _selector()

    class _EmittingCombo(_Combo):
        def set_active(self, active: int) -> None:
            super().set_active(active)
            selector._on_changed()

    selector.combo = _EmittingCombo()
    selector.get_value = lambda: "two.svg"

    selector.on_setting_changed()

    assert selector.saved == []


def test_icon_loader_ignores_corrupt_svg(tmp_path: Path) -> None:
    path = tmp_path / "broken.svg"
    path.write_text("not an svg", encoding="utf-8")

    assert _load_icon(path) is None
