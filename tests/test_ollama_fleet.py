from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from codex_usage.masterjet_contracts import (
    ControlOperation,
    ControlOperationStatusV1,
    OllamaFleetPlanImmediateV1,
    OllamaPlanResultV1,
)
from codex_usage.ollama_fleet import (
    OllamaFleetConsumer,
    OllamaFleetMutationBlocked,
)

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
_DIGEST = "sha256:" + "a" * 64


def _status(
    operation_id: str, state: str, *, result: OllamaPlanResultV1 | None = None
) -> ControlOperationStatusV1:
    operation = ControlOperation(
        operation_id,
        "ollama.instance.plan",
        state,
        4,
        4 if state == "succeeded" else None,
        _DIGEST,
        _NOW,
        _NOW + timedelta(minutes=5),
        1 if state == "succeeded" else 0,
        1 if state in {"failed", "unknown"} else 0,
        0 if state in {"succeeded", "failed", "unknown"} else 1,
        ("host.operation_succeeded",) if state == "succeeded" else (),
    )
    return ControlOperationStatusV1(
        operation, "ollama.instance.plan" if result is not None else None, result
    )


class _Client:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, object, object, object, object]] = []

    def call(
        self,
        operation: str,
        arguments: object,
        expected_generation=None,
        idempotency_key=None,
        plan_digest=None,
    ) -> object:
        self.calls.append(
            (operation, arguments, expected_generation, idempotency_key, plan_digest)
        )
        return self.responses.pop(0)


def test_remote_plan_polls_before_apply_and_unknown_locks_future_mutations() -> None:
    client = _Client(
        [
            _status("operation-plan", "queued"),
            _status(
                "operation-plan", "succeeded", result=OllamaPlanResultV1("remote-plan")
            ),
            _status("operation-apply", "queued"),
            _status("operation-apply", "unknown"),
        ]
    )
    consumer = OllamaFleetConsumer(client)

    planned = consumer.plan(
        {"ref": "remote-west"}, expected_generation=4, idempotency_key="idem-plan"
    )
    assert planned.operation_id == "operation-plan"
    assert planned.mutation_allowed is False
    with pytest.raises(OllamaFleetMutationBlocked, match=r"control\.plan_stale"):
        consumer.apply(expected_generation=4, idempotency_key="idem-apply")

    assert consumer.poll().mutation_allowed is True
    applied = consumer.apply(expected_generation=4, idempotency_key="idem-apply")
    assert applied.operation_id == "operation-apply"
    assert client.calls[-1] == (
        "ollama.instance.apply",
        {"plan_id": "operation-plan"},
        4,
        "idem-apply",
        _DIGEST,
    )
    assert consumer.poll().state == "unknown"
    with pytest.raises(OllamaFleetMutationBlocked, match=r"control\.plan_stale"):
        consumer.stop("remote-west", expected_generation=4, idempotency_key="idem-stop")


def test_local_immediate_plan_uses_the_same_renderer() -> None:
    client = _Client(
        [OllamaFleetPlanImmediateV1("local-plan", _DIGEST, 4, None, "local-west")]
    )
    consumer = OllamaFleetConsumer(client)

    view = consumer.plan(
        {"ref": "local-west"}, expected_generation=4, idempotency_key="idem-plan"
    )

    assert consumer.render() == {
        "operation_id": None,
        "plan_id": "local-plan",
        "plan_digest": _DIGEST,
        "state": "succeeded",
        "mutation_allowed": True,
        "remote": False,
    }
    assert view == consumer.view
