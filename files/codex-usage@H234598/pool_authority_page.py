#!/usr/bin/env python3
"""PoolAuthority owner editor backed only by Codex Usage's canonical config."""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402, I001
from JsonSettingsWidgets import SettingsWidget  # noqa: E402


_AUTHORITY_FIELDS = (
    "account_id",
    "pool_id",
    "provider",
    "hive_available",
    "allowed_model_families",
    "reasoning_minimum",
    "reasoning_maximum",
    "allowed_lifecycles",
    "persistent_leadership_eligible",
    "long_running_leadership_eligible",
)
_EDITABLE_FIELDS = tuple(field for field in _AUTHORITY_FIELDS if field != "account_id")
_BOOLEAN_FIELDS = (
    "hive_available",
    "persistent_leadership_eligible",
    "long_running_leadership_eligible",
)
_LIST_FIELDS = ("allowed_model_families", "allowed_lifecycles")
_REASONING_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")
_LIFECYCLES = frozenset(("ephemeral", "session", "persistent"))
_ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_POOL_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_PROVIDER_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_MODEL_FAMILY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def _invalid_authority() -> None:
    raise ValueError("invalid pool authority")


def _text(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _invalid_authority()
    return value


def _closed_strings(
    value: object,
    *,
    pattern: re.Pattern[str] | None = None,
    allowed: frozenset[str] | None = None,
) -> list[str]:
    if type(value) is not list or not 1 <= len(value) <= 32:
        _invalid_authority()
    values = cast(list[object], value)
    if any(type(item) is not str for item in values):
        _invalid_authority()
    strings = cast(list[str], values)
    if strings != sorted(strings) or len(strings) != len(set(strings)):
        _invalid_authority()
    if pattern is not None and any(pattern.fullmatch(item) is None for item in strings):
        _invalid_authority()
    if allowed is not None and any(item not in allowed for item in strings):
        _invalid_authority()
    return list(strings)


def _canonical_authority(value: object) -> dict[str, object]:
    if type(value) is not dict:
        try:
            source_record = value.to_source_record()  # type: ignore[attr-defined]
        except (AttributeError, TypeError, ValueError):
            _invalid_authority()
        value = source_record
    if type(value) is not dict or set(value) != set(_AUTHORITY_FIELDS):
        _invalid_authority()
    authority = cast(dict[str, object], value)
    minimum = authority["reasoning_minimum"]
    maximum = authority["reasoning_maximum"]
    if (
        type(minimum) is not str
        or type(maximum) is not str
        or minimum not in _REASONING_LEVELS
        or maximum not in _REASONING_LEVELS
        or _REASONING_LEVELS.index(minimum) > _REASONING_LEVELS.index(maximum)
    ):
        _invalid_authority()
    if any(type(authority[field]) is not bool for field in _BOOLEAN_FIELDS):
        _invalid_authority()
    return {
        "account_id": _text(authority["account_id"], _ACCOUNT_ID_RE),
        "pool_id": _text(authority["pool_id"], _POOL_ID_RE),
        "provider": _text(authority["provider"], _PROVIDER_RE),
        "hive_available": authority["hive_available"],
        "allowed_model_families": _closed_strings(
            authority["allowed_model_families"], pattern=_MODEL_FAMILY_RE
        ),
        "reasoning_minimum": minimum,
        "reasoning_maximum": maximum,
        "allowed_lifecycles": _closed_strings(
            authority["allowed_lifecycles"], allowed=_LIFECYCLES
        ),
        "persistent_leadership_eligible": authority[
            "persistent_leadership_eligible"
        ],
        "long_running_leadership_eligible": authority[
            "long_running_leadership_eligible"
        ],
    }


def _snapshot_parts(snapshot: object) -> tuple[int, object, object]:
    try:
        generation = snapshot.generation  # type: ignore[attr-defined]
        account_ids = snapshot.account_ids  # type: ignore[attr-defined]
        authorities = snapshot.authorities  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        raise ValueError("invalid pool authority snapshot") from None
    if type(generation) is not int or generation < 0:
        raise ValueError("invalid pool authority snapshot")
    return generation, account_ids, authorities


def _configured_config_path(settings: object) -> Path | None:
    if settings is None:
        return None
    getter = getattr(settings, "get_value", None)
    if not callable(getter):
        raise ValueError("invalid pool authority settings")
    value = getter("config-path")
    if type(value) is not str or "\x00" in value or len(value) > 4096:
        raise ValueError("invalid pool authority settings")
    return Path(value) if value.strip() else None


class PoolAuthorityOwnerModel:
    """Strict, redaction-free UI state for the ten public owner fields only."""

    __slots__ = ("_account_ids", "_authorities", "generation")

    editable_fields = _EDITABLE_FIELDS

    def __init__(self) -> None:
        self._account_ids: tuple[str, ...] = ()
        self._authorities: tuple[dict[str, object], ...] = ()
        self.generation: int | None = None

    @property
    def account_ids(self) -> tuple[str, ...]:
        return self._account_ids

    @property
    def authorities(self) -> tuple[dict[str, object], ...]:
        return tuple(self._copy_authority(authority) for authority in self._authorities)

    @property
    def is_complete(self) -> bool:
        return self.generation is not None and tuple(
            cast(str, item["account_id"]) for item in self._authorities
        ) == self._account_ids

    def render(self, snapshot: object) -> None:
        try:
            generation, raw_account_ids, raw_authorities = _snapshot_parts(snapshot)
            if type(raw_account_ids) not in (list, tuple):
                raise ValueError("invalid pool authority inventory")
            inventory = tuple(_text(item, _ACCOUNT_ID_RE) for item in raw_account_ids)
            if inventory != tuple(sorted(inventory)) or len(inventory) != len(set(inventory)):
                raise ValueError("invalid pool authority inventory")
            if type(raw_authorities) not in (list, tuple):
                raise ValueError("invalid pool authority inventory")
            authorities = tuple(_canonical_authority(item) for item in raw_authorities)
            account_ids = [cast(str, item["account_id"]) for item in authorities]
            if (
                account_ids != sorted(account_ids)
                or len(account_ids) != len(set(account_ids))
                or not set(account_ids).issubset(inventory)
            ):
                raise ValueError("invalid pool authority inventory")
        except ValueError:
            self.fail_closed()
            raise
        self._account_ids = inventory
        self._authorities = authorities
        self.generation = generation

    def replace_editable(self, account_id: object, values: object) -> None:
        if type(values) is not dict or set(values) != set(self.editable_fields):
            _invalid_authority()
        target_id = _text(account_id, _ACCOUNT_ID_RE)
        replacements = cast(dict[str, object], values)
        if target_id not in self._account_ids:
            raise ValueError("invalid pool authority inventory")
        changed: list[dict[str, object]] = []
        found = False
        for authority in self._authorities:
            if authority["account_id"] != target_id:
                changed.append(self._copy_authority(authority))
                continue
            candidate = {"account_id": target_id, **replacements}
            changed.append(_canonical_authority(candidate))
            found = True
        if not found:
            changed.append(_canonical_authority({"account_id": target_id, **replacements}))
        self._authorities = tuple(
            sorted(changed, key=lambda authority: cast(str, authority["account_id"]))
        )

    def draft(self) -> list[dict[str, object]]:
        if self.generation is None:
            raise ValueError("pool authority inventory unavailable")
        if not self.is_complete:
            raise ValueError("pool authority inventory is incomplete")
        return [self._copy_authority(authority) for authority in self._authorities]

    def fail_closed(self) -> None:
        self._account_ids = ()
        self._authorities = ()
        self.generation = None

    @staticmethod
    def _copy_authority(authority: Mapping[str, object]) -> dict[str, object]:
        copied = dict(authority)
        for field in _LIST_FIELDS:
            copied[field] = list(cast(list[str], copied[field]))
        return copied


class PoolAuthorityOwnerController:
    """Reject late loads/saves before they can alter the rendered config generation."""

    __slots__ = ("_destroyed", "_request_epoch", "model")

    def __init__(self, model: PoolAuthorityOwnerModel | None = None) -> None:
        self.model = model if model is not None else PoolAuthorityOwnerModel()
        self._request_epoch = 0
        self._destroyed = False

    def begin_load(self) -> int:
        if self._destroyed:
            raise RuntimeError("pool authority page destroyed")
        self._request_epoch += 1
        self.model.fail_closed()
        return self._request_epoch

    def receive_load(self, epoch: int, snapshot: object) -> bool:
        if not self._accepts(epoch):
            return False
        self.model.render(snapshot)
        return True

    def begin_save(self) -> tuple[int, int, list[dict[str, object]]]:
        if self._destroyed or self.model.generation is None:
            raise RuntimeError("pool authority inventory unavailable")
        self._request_epoch += 1
        return self._request_epoch, self.model.generation, self.model.draft()

    def receive_save(self, epoch: int, snapshot: object) -> bool:
        if not self._accepts(epoch):
            return False
        self.model.render(snapshot)
        return True

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        self._request_epoch += 1
        self.model.fail_closed()

    def _accepts(self, epoch: int) -> bool:
        return type(epoch) is int and not self._destroyed and epoch == self._request_epoch


class PoolAuthorityOwnerActions:
    """Lazy bridge to the config owner API; it creates no UI-side storage."""

    def __init__(
        self, config_path_supplier: Callable[[], Path | None] | None = None
    ) -> None:
        self._config_path_supplier = config_path_supplier

    @staticmethod
    def _backend():
        try:
            backend = importlib.import_module("codex_usage.pool_authority_owner")
        except (ImportError, ValueError) as exc:
            raise ValueError("pool authority owner backend unavailable") from exc
        if not callable(getattr(backend, "load_pool_authority_owner", None)) or not callable(
            getattr(backend, "save_pool_authority_owner", None)
        ):
            raise ValueError("pool authority owner backend unavailable")
        return backend

    def load(self):
        return self._backend().load_pool_authority_owner(**self._config_path_kwargs())

    def save(self, authorities: Sequence[dict[str, object]], *, expected_generation: int):
        backend = self._backend()
        owner_type = getattr(backend, "PoolAuthorityOwner", None)
        if type(owner_type) is not type:
            raise ValueError("pool authority owner backend unavailable")
        try:
            records = [owner_type(**_canonical_authority(authority)) for authority in authorities]
        except (TypeError, ValueError):
            raise ValueError("invalid pool authority") from None
        return backend.save_pool_authority_owner(
            records,
            expected_generation=expected_generation,
            **self._config_path_kwargs(),
        )

    def _config_path_kwargs(self) -> dict[str, Path]:
        if self._config_path_supplier is None:
            return {}
        config_path = self._config_path_supplier()
        if config_path is None:
            return {}
        if not isinstance(config_path, Path):
            raise ValueError("invalid pool authority settings")
        return {"config_path": config_path}


class PoolAuthorityOwnerPage(SettingsWidget):
    """Explicit owner inputs; account identity comes solely from the config inventory."""

    bind_dir = None

    def __init__(self, info, key, settings):
        del info, key
        SettingsWidget.__init__(self)
        self.set_orientation(Gtk.Orientation.VERTICAL)
        self.set_spacing(6)
        self._controller = PoolAuthorityOwnerController()
        self._actions = PoolAuthorityOwnerActions(
            lambda: _configured_config_path(settings)
        )
        self._entries: dict[str, dict[str, object]] = {}
        self.connect("destroy", self._on_destroy)
        self._status = Gtk.Label(label="PoolAuthority-Ownerwerte nicht geladen")
        self._status.set_xalign(0.0)
        self._status.set_line_wrap(True)
        self.pack_start(self._status, False, False, 0)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        refresh = Gtk.Button(label="PoolAuthority laden")
        refresh.connect("clicked", self._refresh)
        buttons.pack_start(refresh, False, False, 0)
        self._save_button = Gtk.Button(label="Speichern und publizieren")
        self._save_button.set_sensitive(False)
        self._save_button.connect("clicked", self._save)
        buttons.pack_start(self._save_button, False, False, 0)
        self.pack_start(buttons, False, False, 0)
        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.pack_start(self._body, True, True, 0)
        self.show_all()

    def _refresh(self, *_args) -> None:
        try:
            epoch = self._controller.begin_load()
            self._clear_body()
            self._save_button.set_sensitive(False)
            self._status.set_text("PoolAuthority-Ownerwerte werden geladen …")
            snapshot = self._actions.load()
            if not self._controller.receive_load(epoch, snapshot):
                return
            self._render()
            if self._controller.model.is_complete:
                self._status.set_text("Aktuell · vollständige Account-Parität erforderlich")
            else:
                self._status.set_text(
                    "Unvollständig · Werte für jedes Konto explizit eingeben; "
                    "Speichern bleibt fail-closed"
                )
            self._save_button.set_sensitive(True)
        except (RuntimeError, TypeError, ValueError):
            self._clear_body()
            self._save_button.set_sensitive(False)
            self._status.set_text("Ungültige oder unvollständige Authority · Speichern gesperrt")

    def _save(self, *_args) -> None:
        try:
            for account_id in self._controller.model.account_ids:
                self._controller.model.replace_editable(account_id, self._values(account_id))
            epoch, expected_generation, authorities = self._controller.begin_save()
            self._save_button.set_sensitive(False)
            self._status.set_text("Authority wird gespeichert und publiziert …")
            snapshot = self._actions.save(authorities, expected_generation=expected_generation)
            if not self._controller.receive_save(epoch, snapshot):
                return
            self._render()
            self._status.set_text("Gespeichert und publiziert")
            self._save_button.set_sensitive(True)
        except (RuntimeError, TypeError, ValueError):
            self._save_button.set_sensitive(self._controller.model.generation is not None)
            self._status.set_text("Ungültige oder unvollständige Authority · Speichern gesperrt")

    def _render(self) -> None:
        self._clear_body()
        by_account = {
            cast(str, authority["account_id"]): authority
            for authority in self._controller.model.authorities
        }
        for account_id in self._controller.model.account_ids:
            authority = by_account.get(account_id)
            frame = Gtk.Frame(label=f"Account-ID: {account_id}")
            grid = Gtk.Grid(column_spacing=8, row_spacing=5)
            grid.set_margin_start(8)
            grid.set_margin_end(8)
            grid.set_margin_top(8)
            grid.set_margin_bottom(8)
            fields: dict[str, object] = {}
            row = 0
            for field, label in (
                ("pool_id", "Pool-ID"),
                ("provider", "Provider"),
                ("hive_available", "Hive verfügbar"),
                ("allowed_model_families", "Modellfamilien (Komma-getrennt)"),
                ("reasoning_minimum", "Reasoning-Minimum"),
                ("reasoning_maximum", "Reasoning-Maximum"),
                ("allowed_lifecycles", "Lebenszyklen (Komma-getrennt)"),
                ("persistent_leadership_eligible", "Persistente Leadership möglich"),
                ("long_running_leadership_eligible", "Long-running Leadership möglich"),
            ):
                caption = Gtk.Label(label=label)
                caption.set_xalign(0.0)
                grid.attach(caption, 0, row, 1, 1)
                if field in _BOOLEAN_FIELDS:
                    widget = Gtk.ComboBoxText()
                    widget.append_text("false")
                    widget.append_text("true")
                    if authority is not None:
                        widget.set_active(1 if authority[field] is True else 0)
                else:
                    widget = Gtk.Entry()
                    if authority is not None:
                        value = authority[field]
                        widget.set_text(
                            ", ".join(cast(list[str], value))
                            if field in _LIST_FIELDS
                            else cast(str, value)
                        )
                grid.attach(widget, 1, row, 1, 1)
                fields[field] = widget
                row += 1
            frame.add(grid)
            self._entries[account_id] = fields
            self._body.pack_start(frame, False, False, 0)
        self._body.show_all()

    def _values(self, account_id: str) -> dict[str, object]:
        fields = self._entries.get(account_id)
        if fields is None or set(fields) != set(_EDITABLE_FIELDS):
            _invalid_authority()
        values: dict[str, object] = {}
        for field in _EDITABLE_FIELDS:
            widget = fields[field]
            if field in _BOOLEAN_FIELDS:
                selected = cast(Gtk.ComboBoxText, widget).get_active()
                if selected not in (0, 1):
                    _invalid_authority()
                values[field] = selected == 1
            elif field in _LIST_FIELDS:
                values[field] = self._list_value(cast(Gtk.Entry, widget).get_text())
            else:
                values[field] = cast(Gtk.Entry, widget).get_text().strip()
        return values

    @staticmethod
    def _list_value(value: object) -> list[str]:
        if type(value) is not str:
            _invalid_authority()
        parts = [part.strip() for part in value.split(",")]
        if not all(parts) or len(parts) != len(set(parts)):
            _invalid_authority()
        return sorted(parts)

    def _clear_body(self) -> None:
        self._entries.clear()
        for child in self._body.get_children():
            self._body.remove(child)
            child.destroy()

    def _on_destroy(self, *_args) -> None:
        self._controller.destroy()
        self._entries.clear()


__all__ = [
    "PoolAuthorityOwnerActions",
    "PoolAuthorityOwnerController",
    "PoolAuthorityOwnerModel",
    "PoolAuthorityOwnerPage",
]
