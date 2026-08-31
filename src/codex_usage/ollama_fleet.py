"""Canonical asynchronous state for the existing Ollama fleet applet.

This module intentionally has no GTK/Cinnamon widgets.  It is the single
consumer state used by the applet: local immediate plans and remote Agent
operations project to the same render data, while terminal failures and
unknown effects close every mutation gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .masterjet_contracts import (
    ControlOperationStatusV1,
    OllamaFleetPlanImmediateV1,
)

_BLOCKING_STATES = frozenset({"failed", "unknown", "blocked", "partial"})


class OllamaFleetClient(Protocol):
    def call(
        self,
        operation: str,
        arguments: object,
        expected_generation: int | None = None,
        idempotency_key: str | None = None,
        plan_digest: str | None = None,
    ) -> object: ...


class OllamaFleetMutationBlocked(RuntimeError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class OllamaFleetViewV1:
    operation_id: str | None
    plan_id: str | None
    plan_digest: str | None
    state: str
    mutation_allowed: bool
    remote: bool


class OllamaFleetConsumer:
    """State machine for plan/apply/probe/stop without a parallel GUI path."""

    __slots__ = ("_client", "_view")

    def __init__(self, client: OllamaFleetClient) -> None:
        if not callable(getattr(client, "call", None)):
            raise TypeError("ollama.client_invalid")
        self._client = client
        self._view: OllamaFleetViewV1 | None = None

    @property
    def view(self) -> OllamaFleetViewV1 | None:
        return self._view

    def plan(
        self,
        arguments: Mapping[str, object],
        *,
        expected_generation: int,
        idempotency_key: str,
    ) -> OllamaFleetViewV1:
        result = self._client.call(
            "ollama.instance.plan",
            dict(arguments),
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
        )
        self._view = self._plan_view(result)
        return self._view

    def poll(self) -> OllamaFleetViewV1:
        view = self._require_view()
        if view.operation_id is None:
            return view
        result = self._client.call("operations.get", {"operation_id": view.operation_id})
        if type(result) is not ControlOperationStatusV1:
            raise OllamaFleetMutationBlocked("control.response_invalid")
        operation = result.operation
        if operation.id != view.operation_id:
            raise OllamaFleetMutationBlocked("control.response_invalid")
        self._view = OllamaFleetViewV1(
            operation.id,
            view.plan_id,
            view.plan_digest,
            operation.state,
            operation.state == "succeeded",
            True,
        )
        return self._view

    def apply(self, *, expected_generation: int, idempotency_key: str) -> OllamaFleetViewV1:
        view = self._require_mutable_plan()
        result = self._client.call(
            "ollama.instance.apply",
            {"plan_id": view.plan_id},
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            plan_digest=view.plan_digest,
        )
        return self._record_mutation(result, view)

    def probe(
        self,
        instance_ref: str,
        *,
        expected_generation: int,
        idempotency_key: str,
    ) -> OllamaFleetViewV1:
        view = self._require_mutable_state()
        result = self._client.call(
            "ollama.instance.probe",
            {"instance_ref": instance_ref},
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
        )
        return self._record_mutation(result, view)

    def stop(
        self,
        instance_ref: str,
        *,
        expected_generation: int,
        idempotency_key: str,
    ) -> OllamaFleetViewV1:
        view = self._require_mutable_state()
        result = self._client.call(
            "ollama.instance.stop",
            {"instance_ref": instance_ref},
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
        )
        return self._record_mutation(result, view)

    def render(self) -> dict[str, object]:
        view = self._require_view()
        return {
            "operation_id": view.operation_id,
            "plan_id": view.plan_id,
            "plan_digest": view.plan_digest,
            "state": view.state,
            "mutation_allowed": view.mutation_allowed,
            "remote": view.remote,
        }

    def _plan_view(self, result: object) -> OllamaFleetViewV1:
        if type(result) is ControlOperationStatusV1:
            operation = result.operation
            return OllamaFleetViewV1(
                operation.id,
                operation.id,
                operation.plan_digest,
                operation.state,
                operation.state == "succeeded",
                True,
            )
        if type(result) is OllamaFleetPlanImmediateV1:
            return OllamaFleetViewV1(
                None,
                result.plan_id,
                result.plan_digest,
                "succeeded",
                True,
                False,
            )
        raise OllamaFleetMutationBlocked("control.response_invalid")

    def _record_mutation(
        self, result: object, prior: OllamaFleetViewV1
    ) -> OllamaFleetViewV1:
        if type(result) is ControlOperationStatusV1:
            operation = result.operation
            self._view = OllamaFleetViewV1(
                operation.id,
                prior.plan_id,
                prior.plan_digest,
                operation.state,
                operation.state == "succeeded",
                True,
            )
            return self._view
        # The local direct adapter uses the same renderer with an immediately
        # terminal state.  Its detailed result stays in the applet's existing
        # result panel rather than creating a second Ollama UI.
        self._view = OllamaFleetViewV1(
            None,
            prior.plan_id,
            prior.plan_digest,
            "succeeded",
            True,
            False,
        )
        return self._view

    def _require_view(self) -> OllamaFleetViewV1:
        if self._view is None:
            raise OllamaFleetMutationBlocked("control.plan_stale")
        return self._view

    def _require_mutable_plan(self) -> OllamaFleetViewV1:
        view = self._require_mutable_state()
        if view.plan_id is None or view.plan_digest is None:
            raise OllamaFleetMutationBlocked("control.plan_stale")
        return view

    def _require_mutable_state(self) -> OllamaFleetViewV1:
        view = self._require_view()
        if not view.mutation_allowed or view.state in _BLOCKING_STATES:
            raise OllamaFleetMutationBlocked("control.plan_stale")
        return view


__all__ = [
    "OllamaFleetClient",
    "OllamaFleetConsumer",
    "OllamaFleetMutationBlocked",
    "OllamaFleetViewV1",
]
