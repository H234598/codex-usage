#!/usr/bin/env python3
"""Redacted Google account cards and bounded control actions for Cinnamon."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402
from JsonSettingsWidgets import SettingsWidget  # noqa: E402
from openai_accounts_page import BoundedJsonRunner, CommandResult  # noqa: E402

_ACCOUNT_FIELDS = frozenset(
    {
        "ref",
        "label",
        "enabled",
        "subject_bound",
        "inventory_generation",
        "project_count",
        "billing_count",
        "default_oauth_client_ref",
        "oauth_client_availability",
        "oauth_state",
        "quota_state",
        "reload_state",
    }
)
_PROJECT_FIELDS = frozenset(
    {
        "ref",
        "project_name",
        "purpose",
        "key_name",
        "billing_ref",
        "status",
        "probe_state",
        "quota_state",
    }
)
_PLAN_FIELDS = frozenset(
    {
        "account_ref",
        "plan_id",
        "expected_generation",
        "plan_digest",
        "expires_at",
        "step_count",
        "projects",
    }
)
_PLAN_PROJECT_FIELDS = frozenset({"project_name", "key_name"})
_SECRET_FIELD_PARTS = (
    "access_token",
    "refresh_token",
    "client_secret",
    "api_key",
    "password",
    "cookie",
    "totp",
    "bearer",
    "project_id",
    "provider_id",
)
_PLAN_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _text(value: object, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {field}")
    result = value.strip()
    if not result or len(result) > maximum or "\x00" in result:
        raise ValueError(f"invalid {field}")
    return result


def _mapping(value: object, fields: frozenset[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"private or invalid {label} fields")
    for key in value:
        folded = str(key).casefold().replace("-", "_")
        if any(marker in folded for marker in _SECRET_FIELD_PARTS):
            raise ValueError(f"private {label} field")
    return value


def _count(value: object, field: str) -> int:
    if type(value) is not int or value < 0 or value > 1_000_000:
        raise ValueError(f"invalid {field}")
    return value


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"invalid {field}")
    return value


@dataclass(frozen=True, slots=True)
class GoogleProjectRow:
    ref: str
    project_name: str
    purpose: str
    key_name: str
    billing_ref: str | None
    status: str
    probe_state: str
    quota_state: str


@dataclass(frozen=True, slots=True)
class GoogleAccountCard:
    ref: str
    label: str
    enabled: bool
    subject_bound: bool
    inventory_generation: int
    project_count: int
    billing_count: int
    default_oauth_client_ref: str | None
    oauth_client_availability: str
    oauth_state: str
    quota_state: str
    reload_state: str
    projects: tuple[GoogleProjectRow, ...]
    add_enabled: bool
    oauth_enabled: bool
    inventory_enabled: bool
    plan_enabled: bool
    apply_enabled: bool


@dataclass(frozen=True, slots=True)
class GooglePlanPreview:
    account_ref: str
    plan_id: str
    expected_generation: int
    plan_digest: str
    expires_at: str
    step_count: int
    names: tuple[tuple[str, str], ...]


class GoogleAccountsModel:
    __slots__ = ("_cards", "details_available", "stale")

    def __init__(self) -> None:
        self._cards: tuple[GoogleAccountCard, ...] = ()
        self.details_available = False
        self.stale = True

    @property
    def cards(self) -> tuple[GoogleAccountCard, ...]:
        return self._cards

    def card(self, account_ref: str) -> GoogleAccountCard:
        matches = [card for card in self._cards if card.ref == account_ref]
        if len(matches) != 1:
            raise KeyError(account_ref)
        return matches[0]

    def render(self, payload: object) -> None:
        try:
            self._render(payload)
        except ValueError:
            self.fail_closed()
            raise

    def _render(self, payload: object) -> None:
        if isinstance(payload, list):
            accounts = payload
            projects: Mapping[str, object] = {}
            stale = True
            details_available = False
        elif isinstance(payload, Mapping) and set(payload) == {"accounts", "projects", "stale"}:
            accounts = payload["accounts"]
            projects = payload["projects"]
            stale = _boolean(payload["stale"], "stale")
            details_available = True
        else:
            raise ValueError("private or invalid Google response fields")
        if not isinstance(accounts, list) or not isinstance(projects, Mapping):
            raise ValueError("private or invalid Google response fields")
        cards: list[GoogleAccountCard] = []
        seen: set[str] = set()
        mutations_enabled = details_available and stale is False
        for value in accounts:
            account = _mapping(value, _ACCOUNT_FIELDS, "Google account")
            account_ref = _text(account["ref"], "ref")
            if account_ref in seen:
                raise ValueError("duplicate Google account")
            seen.add(account_ref)
            if details_available and account_ref not in projects:
                raise ValueError("private or invalid Google project owner")
            project_rows = self._projects(
                account_ref, projects[account_ref] if details_available else []
            )
            project_count = _count(account["project_count"], "project_count")
            if details_available and len(project_rows) != project_count:
                raise ValueError("invalid Google project_count projection")
            oauth_client_ref = account["default_oauth_client_ref"]
            availability = _text(
                account["oauth_client_availability"],
                "oauth_client_availability",
                maximum=64,
            )
            if oauth_client_ref is not None:
                oauth_client_ref = _text(oauth_client_ref, "default_oauth_client_ref")
            if (availability == "available") != (oauth_client_ref is not None):
                raise ValueError("invalid OAuth client projection")
            enabled = _boolean(account["enabled"], "enabled")
            account_mutations_enabled = mutations_enabled and enabled
            cards.append(
                GoogleAccountCard(
                    ref=account_ref,
                    label=_text(account["label"], "label"),
                    enabled=enabled,
                    subject_bound=_boolean(account["subject_bound"], "subject_bound"),
                    inventory_generation=_count(
                        account["inventory_generation"], "inventory_generation"
                    ),
                    project_count=project_count,
                    billing_count=_count(account["billing_count"], "billing_count"),
                    default_oauth_client_ref=oauth_client_ref,
                    oauth_client_availability=availability,
                    oauth_state=_text(account["oauth_state"], "oauth_state", maximum=64),
                    quota_state=_text(account["quota_state"], "quota_state", maximum=64),
                    reload_state=_text(account["reload_state"], "reload_state", maximum=64),
                    projects=project_rows,
                    add_enabled=account_mutations_enabled,
                    oauth_enabled=account_mutations_enabled and availability == "available",
                    inventory_enabled=account_mutations_enabled,
                    plan_enabled=account_mutations_enabled,
                    apply_enabled=account_mutations_enabled,
                )
            )
        if details_available and set(projects) != seen:
            raise ValueError("private or invalid Google project owner")
        self._cards = tuple(cards)
        self.details_available = details_available
        self.stale = stale

    def fail_closed(self) -> None:
        self._cards = ()
        self.details_available = False
        self.stale = True

    def _projects(self, account_ref: str, values: object) -> tuple[GoogleProjectRow, ...]:
        if not isinstance(values, list) or len(values) > 256:
            raise ValueError("private or invalid Google projects")
        result = []
        seen = set()
        for value in values:
            project = _mapping(value, _PROJECT_FIELDS, "Google project")
            ref = _text(project["ref"], "ref")
            if ref in seen:
                raise ValueError("duplicate Google project")
            seen.add(ref)
            billing_ref = project["billing_ref"]
            if billing_ref is not None:
                billing_ref = _text(billing_ref, "billing_ref")
            result.append(
                GoogleProjectRow(
                    ref=ref,
                    project_name=_text(project["project_name"], "project_name"),
                    purpose=_text(project["purpose"], "purpose", maximum=64),
                    key_name=_text(project["key_name"], "key_name"),
                    billing_ref=billing_ref,
                    status=_text(project["status"], "status", maximum=64),
                    probe_state=_text(project["probe_state"], "probe_state", maximum=64),
                    quota_state=_text(project["quota_state"], "quota_state", maximum=64),
                )
            )
        return tuple(result)

    def preview_plan(self, payload: object) -> GooglePlanPreview:
        plan = _mapping(payload, _PLAN_FIELDS, "Google plan")
        digest = plan["plan_digest"]
        if type(digest) is not str or _PLAN_DIGEST_RE.fullmatch(digest) is None:
            raise ValueError("invalid plan_digest")
        values = plan["projects"]
        if not isinstance(values, list) or len(values) > 256:
            raise ValueError("private or invalid Google plan projects")
        names = []
        for value in values:
            project = _mapping(value, _PLAN_PROJECT_FIELDS, "Google plan project")
            names.append(
                (
                    _text(project["project_name"], "project_name"),
                    _text(project["key_name"], "key_name"),
                )
            )
        step_count = _count(plan["step_count"], "step_count")
        if step_count < len(names):
            raise ValueError("invalid Google plan step_count")
        return GooglePlanPreview(
            account_ref=_text(plan["account_ref"], "account_ref"),
            plan_id=_text(plan["plan_id"], "plan_id"),
            expected_generation=_count(plan["expected_generation"], "expected_generation"),
            plan_digest=digest,
            expires_at=_text(plan["expires_at"], "expires_at", maximum=64),
            step_count=step_count,
            names=tuple(names),
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(cards={self._cards!r}, "
            f"details_available={self.details_available!r}, stale={self.stale!r})"
        )


class GoogleActions:
    __slots__ = (
        "_confirm",
        "_executable",
        "_oauth_runner",
        "_projection_ready",
        "_projection_version",
        "_runner",
    )

    def __init__(
        self,
        runner: BoundedJsonRunner,
        *,
        executable: str | None = None,
        confirm: Callable[[GooglePlanPreview], bool] | None = None,
        oauth_runner: BoundedJsonRunner | None = None,
    ) -> None:
        self._runner = runner
        self._oauth_runner = oauth_runner or runner
        self._executable = executable or str(Path.home() / ".local/bin/codex-usage")
        self._confirm = confirm or (lambda _preview: False)
        self._projection_ready = False
        self._projection_version = 0

    def set_projection_ready(self, ready: bool) -> None:
        self._projection_version += 1
        self._projection_ready = bool(ready)

    @property
    def projection_ready(self) -> bool:
        return self._projection_ready

    @property
    def projection_version(self) -> int:
        return self._projection_version

    def _submit(
        self,
        arguments,
        *,
        stdin_data=None,
        callback=None,
        mutation=True,
        challenge_callback=None,
        runner=None,
    ) -> None:
        if mutation and not self._projection_ready:
            raise RuntimeError("STALE")
        options = {"stdin_data": stdin_data, "callback": callback}
        if challenge_callback is not None:
            options["challenge_callback"] = challenge_callback
        (runner or self._runner).submit(
            [self._executable, *(["--step-up-stdin"] if challenge_callback else []), *arguments],
            **options,
        )

    def refresh_accounts(self, *, callback=None) -> None:
        self._submit(["google", "accounts", "--json"], callback=callback, mutation=False)

    def oauth_begin(
        self, account_ref: str, *, browser: str, callback=None, challenge_callback=None
    ) -> None:
        if browser not in {"firefox", "vivaldi", "chromium"}:
            raise ValueError("unsupported browser")
        self._submit(
            [
                "google",
                "oauth-begin",
                _text(account_ref, "account_ref"),
                "--browser",
                browser,
                "--json",
            ],
            callback=callback,
            challenge_callback=challenge_callback,
            runner=self._oauth_runner,
        )

    def import_oauth_client(
        self, account_ref: str, source: Path, *, callback=None, challenge_callback=None
    ) -> None:
        if not isinstance(source, Path) or not source.is_absolute():
            raise ValueError("OAuth client source must be an absolute local path")
        self._submit(
            [
                "google",
                "add",
                _text(account_ref, "account_ref"),
                "--oauth-client-json",
                str(source),
                "--json",
            ],
            callback=callback,
            challenge_callback=challenge_callback,
        )

    def inventory_refresh(
        self, account_ref: str, *, callback=None, challenge_callback=None
    ) -> None:
        self._submit(
            ["google", "inventory-refresh", _text(account_ref, "account_ref"), "--json"],
            callback=callback,
            challenge_callback=challenge_callback,
        )

    def provision_plan(self, account_ref: str, *, callback=None, challenge_callback=None) -> None:
        self._submit(
            ["google", "provision-plan", _text(account_ref, "account_ref"), "--json"],
            callback=callback,
            challenge_callback=challenge_callback,
        )

    def apply(self, preview: GooglePlanPreview, *, callback=None, challenge_callback=None) -> bool:
        if not self._projection_ready:
            raise RuntimeError("STALE")
        if not isinstance(preview, GooglePlanPreview) or not self._confirm(preview):
            return False
        if not self._projection_ready:
            return False
        self._submit(
            [
                "google",
                "provision-apply",
                preview.account_ref,
                preview.plan_id,
                "--plan-digest",
                preview.plan_digest,
                "--confirm",
                "--json",
            ],
            callback=callback,
            challenge_callback=challenge_callback,
        )
        return True

    def __repr__(self) -> str:
        return f"{type(self).__name__}(projection_ready={self._projection_ready!r})"


class GoogleAccountsPage(SettingsWidget):
    bind_dir = None

    def __init__(self, info, key, settings):
        del info, key, settings
        SettingsWidget.__init__(self)
        self.set_orientation(Gtk.Orientation.VERTICAL)
        self.set_spacing(8)
        self.model = GoogleAccountsModel()
        self._runner = BoundedJsonRunner()
        self._oauth_runner = BoundedJsonRunner(timeout_seconds=900.0)
        self._actions = GoogleActions(
            self._runner,
            confirm=self._confirm_plan,
            oauth_runner=self._oauth_runner,
        )
        self._request_epoch = 0
        self._destroyed = False
        self.connect("destroy", self._on_destroy)
        self._status = Gtk.Label(label="Google-Control noch nicht geladen")
        self._status.set_xalign(0.0)
        self.pack_start(self._status, False, False, 0)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        refresh = Gtk.Button(label="Accounts laden")
        refresh.connect("clicked", self._refresh)
        controls.pack_start(refresh, False, False, 0)
        self._add_button = Gtk.Button(label="Account hinzufügen")
        self._add_button.set_sensitive(False)
        self._add_button.connect("clicked", self._add_account)
        controls.pack_start(self._add_button, False, False, 0)
        self.pack_start(controls, False, False, 0)
        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.pack_start(self._body, True, True, 0)
        self.show_all()

    def _refresh(self, *_args) -> None:
        try:
            epoch = self._begin_request()
        except RuntimeError:
            return
        self._revoke_projection()
        self._status.set_text("Google-Control wird geladen …")
        try:
            self._actions.refresh_accounts(
                callback=self._result_callback(epoch, self._accounts_loaded)
            )
        except RuntimeError:
            self._status.set_text("STALE · Mutationen gesperrt")

    def _begin_request(self) -> int:
        if self._destroyed:
            raise RuntimeError("DESTROYED")
        self._request_epoch += 1
        return self._request_epoch

    def _accepts(self, epoch: int) -> bool:
        return not self._destroyed and epoch == self._request_epoch

    def _result_callback(self, epoch: int, callback):
        def current(result: CommandResult) -> bool:
            if not self._accepts(epoch):
                return False
            return callback(result)

        return current

    def _challenge_callback(self, epoch: int):
        return lambda: self._prompt_running_step_up(epoch)

    def _revoke_projection(self) -> None:
        self.model.fail_closed()
        self._actions.set_projection_ready(False)
        self._set_buttons_sensitive(False)

    def _on_destroy(self, *_args) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        self._request_epoch += 1
        self.model.fail_closed()
        self._actions.set_projection_ready(False)
        self._runner.close()
        if self._oauth_runner is not self._runner:
            self._oauth_runner.close()

    def _accounts_loaded(self, result: CommandResult) -> bool:
        if not result.ok:
            self._revoke_projection()
            self._status.set_text(f"STALE · {result.code} · Mutationen gesperrt")
            return False
        try:
            self.render(result.payload)
        except ValueError:
            self._revoke_projection()
            self._status.set_text("Ungültige redigierte Control-Antwort · Mutationen gesperrt")
        return False

    def render(self, payload: object) -> None:
        self.model.render(payload)
        projection_ready = self.model.details_available and not self.model.stale
        self._actions.set_projection_ready(projection_ready)
        self._add_button.set_sensitive(projection_ready)
        if self.model.stale:
            status = "STALE · Mutationen gesperrt"
        elif not self.model.details_available:
            status = "Aktuell · Projektprojektion nicht verfügbar"
        else:
            status = "Aktuell"
        self._status.set_text(status)
        for child in self._body.get_children():
            self._body.remove(child)
            child.destroy()
        for card in self.model.cards:
            frame = Gtk.Frame(label=card.label)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            summary = Gtk.Label(
                label=(
                    f"{card.ref} · Subject {'gebunden' if card.subject_bound else 'offen'} · "
                    f"Inventory {card.inventory_generation} · "
                    f"Aktiv {'ja' if card.enabled else 'nein'} · "
                    f"OAuth {card.oauth_state} · Quota {card.quota_state} · "
                    f"Reload {card.reload_state} · "
                    f"OAuth-Client {card.oauth_client_availability}"
                )
            )
            summary.set_xalign(0.0)
            box.pack_start(summary, False, False, 0)
            for project in card.projects:
                row = Gtk.Label(
                    label=(
                        f"{project.project_name} · Key {project.key_name} · "
                        f"Billing {project.billing_ref or '—'} · Probe "
                        f"{project.probe_state} · Status {project.status}"
                    )
                )
                row.set_xalign(0.0)
                box.pack_start(row, False, False, 0)
            buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            oauth_client = Gtk.Button(label="OAuth-Client importieren")
            oauth_client.set_sensitive(card.add_enabled)
            oauth_client.connect("clicked", self._choose_oauth_client, card.ref)
            buttons.pack_start(oauth_client, False, False, 0)
            oauth = Gtk.Button(label="OAuth anmelden")
            oauth.set_sensitive(card.oauth_enabled)
            oauth.connect("clicked", self._oauth_begin, card.ref)
            buttons.pack_start(oauth, False, False, 0)
            inventory = Gtk.Button(label="Inventory aktualisieren")
            inventory.set_sensitive(card.inventory_enabled)
            inventory.connect("clicked", self._inventory, card.ref)
            buttons.pack_start(inventory, False, False, 0)
            plan = Gtk.Button(label="Bis Quota planen")
            plan.set_sensitive(card.plan_enabled)
            plan.connect("clicked", self._plan, card.ref)
            buttons.pack_start(plan, False, False, 0)
            box.pack_start(buttons, False, False, 0)
            frame.add(box)
            self._body.pack_start(frame, False, False, 0)
        self._body.show_all()

    def _set_buttons_sensitive(self, sensitive: bool) -> None:
        self._add_button.set_sensitive(sensitive)
        for frame in self._body.get_children():
            for child in frame.get_child().get_children():
                if isinstance(child, Gtk.Box):
                    for button in child.get_children():
                        if isinstance(button, Gtk.Button):
                            button.set_sensitive(sensitive)

    def _inventory(self, _button, account_ref: str) -> None:
        try:
            epoch = self._begin_request()
            self._actions.inventory_refresh(
                account_ref,
                callback=self._result_callback(epoch, self._operation_finished),
                challenge_callback=self._challenge_callback(epoch),
            )
        except RuntimeError:
            self._status.set_text("STALE · Mutationen gesperrt")

    def _oauth_begin(self, _button, account_ref: str) -> None:
        try:
            epoch = self._begin_request()
            self._actions.oauth_begin(
                account_ref,
                browser="firefox",
                callback=self._result_callback(epoch, self._operation_finished),
                challenge_callback=self._challenge_callback(epoch),
            )
        except RuntimeError:
            self._status.set_text("STALE · Mutationen gesperrt")

    def _choose_oauth_client(self, _button, account_ref: str) -> None:
        try:
            self.choose_oauth_client(account_ref, epoch=self._begin_request())
        except RuntimeError:
            self._status.set_text("STALE · Mutationen gesperrt")

    def _add_account(self, *_args) -> None:
        try:
            epoch = self._begin_request()
            account_ref = self._prompt_account_ref(epoch)
            if account_ref is not None:
                self.choose_oauth_client(account_ref, epoch=epoch)
        except RuntimeError:
            self._status.set_text("STALE · Mutationen gesperrt")

    def _plan(self, _button, account_ref: str) -> None:
        try:
            epoch = self._begin_request()
            self._actions.provision_plan(
                account_ref,
                callback=self._result_callback(
                    epoch, lambda result: self._plan_loaded(result, epoch)
                ),
                challenge_callback=self._challenge_callback(epoch),
            )
        except RuntimeError:
            self._status.set_text("STALE · Mutationen gesperrt")

    def _plan_loaded(self, result: CommandResult, epoch: int | None = None) -> bool:
        current_epoch = self._request_epoch if epoch is None else epoch
        if not self._accepts(current_epoch):
            return False
        if not result.ok:
            self._operation_finished(result)
            return False
        try:
            preview = self.model.preview_plan(result.payload)
        except ValueError:
            self._status.set_text("Planvorschau unvollständig · Apply gesperrt")
            return False
        try:
            applied = self._actions.apply(
                preview,
                callback=self._result_callback(current_epoch, self._operation_finished),
                challenge_callback=self._challenge_callback(current_epoch),
            )
        except RuntimeError:
            applied = False
        if not applied and not self._actions.projection_ready:
            self._status.set_text("STALE · Apply gesperrt")
        return False

    def _operation_finished(self, result: CommandResult) -> bool:
        self._status.set_text("Operation abgeschlossen" if result.ok else f"Fehler: {result.code}")
        return False

    def _prompt_running_step_up(self, epoch: int | None = None) -> bytearray | None:
        current_epoch = self._request_epoch if epoch is None else epoch
        if not self._accepts(current_epoch) or not self._actions.projection_ready:
            return None
        projection_version = self._actions.projection_version
        code = self.prompt_step_up()
        if (
            not self._accepts(current_epoch)
            or not self._actions.projection_ready
            or self._actions.projection_version != projection_version
        ):
            if type(code) is bytearray:
                code[:] = b"\x00" * len(code)
                code.clear()
            return None
        return code

    def _confirm_plan(self, preview: GooglePlanPreview) -> bool:
        epoch = self._request_epoch
        if not self._accepts(epoch):
            return False
        names = "\n".join(
            f"• {project_name} · {key_name}" for project_name, key_name in preview.names
        )
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel()
            if isinstance(self.get_toplevel(), Gtk.Window)
            else None,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=f"{preview.step_count} Schritte anwenden?",
        )
        dialog.format_secondary_text(f"Digest: {preview.plan_digest}\n{names}")
        try:
            accepted = dialog.run() == Gtk.ResponseType.OK
            return accepted and self._accepts(epoch)
        finally:
            dialog.destroy()

    def choose_oauth_client(self, account_ref: str, *, epoch: int | None = None) -> None:
        current_epoch = self._begin_request() if epoch is None else epoch
        if not self._accepts(current_epoch):
            raise RuntimeError("STALE")
        dialog = Gtk.FileChooserDialog(
            title="OAuth-Client-JSON auswählen",
            transient_for=self.get_toplevel()
            if isinstance(self.get_toplevel(), Gtk.Window)
            else None,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN,
            Gtk.ResponseType.OK,
        )
        try:
            if dialog.run() != Gtk.ResponseType.OK:
                return
            if not self._accepts(current_epoch):
                return
            filename = dialog.get_filename()
            if isinstance(filename, str):
                self._actions.import_oauth_client(
                    account_ref,
                    Path(filename),
                    callback=self._result_callback(
                        current_epoch, self._operation_finished
                    ),
                    challenge_callback=self._challenge_callback(current_epoch),
                )
        finally:
            dialog.destroy()

    def _prompt_account_ref(self, epoch: int | None = None) -> str | None:
        current_epoch = self._request_epoch if epoch is None else epoch
        if not self._accepts(current_epoch):
            return None
        toplevel = self.get_toplevel()
        dialog = Gtk.Dialog(
            title="Google-Account hinzufügen",
            transient_for=toplevel if isinstance(toplevel, Gtk.Window) else None,
            flags=Gtk.DialogFlags.MODAL,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK,
            Gtk.ResponseType.OK,
        )
        entry = Gtk.Entry()
        entry.set_placeholder_text("Account-Ref")
        dialog.get_content_area().pack_start(entry, False, False, 8)
        dialog.show_all()
        try:
            if dialog.run() != Gtk.ResponseType.OK:
                return None
            if not self._accepts(current_epoch):
                return None
            try:
                return _text(entry.get_text(), "account_ref")
            except ValueError:
                self._status.set_text("Ungültiger Account-Ref")
                return None
        finally:
            dialog.destroy()

    def prompt_step_up(self) -> bytearray | None:
        dialog = Gtk.Dialog(
            title="TOTP-Step-up",
            transient_for=self.get_toplevel()
            if isinstance(self.get_toplevel(), Gtk.Window)
            else None,
            flags=Gtk.DialogFlags.MODAL,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK,
            Gtk.ResponseType.OK,
        )
        entry = Gtk.Entry()
        entry.set_visibility(False)
        entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
        dialog.get_content_area().pack_start(entry, False, False, 8)
        dialog.show_all()
        try:
            if dialog.run() != Gtk.ResponseType.OK:
                return None
            return bytearray(entry.get_text(), "ascii")
        except (TypeError, UnicodeError, ValueError):
            return None
        finally:
            entry.set_text("")
            dialog.destroy()


__all__ = [
    "GoogleAccountCard",
    "GoogleAccountsModel",
    "GoogleAccountsPage",
    "GoogleActions",
    "GooglePlanPreview",
    "GoogleProjectRow",
]
