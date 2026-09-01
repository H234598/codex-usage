from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from codex_usage import cli
from codex_usage.masterjet_contracts import (
    ControlOperation,
    ControlOperationStatusV1,
    OllamaPlanResultV1,
)

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
_DIGEST = "sha256:" + "a" * 64


def _status(
    operation_id: str, state: str, *, result: OllamaPlanResultV1 | None = None
) -> ControlOperationStatusV1:
    return ControlOperationStatusV1(
        ControlOperation(
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
        ),
        "ollama.instance.plan" if result is not None else None,
        result,
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


def _args(action: str, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "action": action,
        "config": None,
        "step_up_stdin": False,
        "expected_generation": 4,
        "idempotency_key": "idem-remote",
        "operation_id": "operation-plan",
        "plan_id": "operation-plan",
        "plan_digest": _DIGEST,
        "instance_ref": "remote-west",
        "ref": "remote-west",
        "label": "Remote West",
        "host_ref": "worker-west",
        "ollama_executable": "/private/ollama",
        "models_directory": "/private/models",
        "selected_model_ref": ["model-a"],
        "allowed_cpus": "2-3",
        "cpu_quota_percent": 200,
        "cpu_weight": 50,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_masterjet_ollama_entrypoint_uses_consumer_renderer_and_operations_get_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _Client(
        [
            _status("operation-plan", "queued"),
            _status(
                "operation-plan",
                "succeeded",
                result=OllamaPlanResultV1("remote-plan"),
            ),
            _status(
                "operation-plan",
                "succeeded",
                result=OllamaPlanResultV1("remote-plan"),
            ),
            _status("operation-apply", "queued"),
        ]
    )
    monkeypatch.setattr(cli, "load_config", lambda _path: SimpleNamespace(masterjet=object()))
    monkeypatch.setattr(cli, "_new_masterjet_client", lambda *_args, **_kwargs: client)

    assert cli._cmd_masterjet_ollama(_args("plan")) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["operation_id"] == "operation-plan"
    assert planned["state"] == "queued"

    assert cli._cmd_masterjet_ollama(_args("poll")) == 0
    polled = json.loads(capsys.readouterr().out)
    assert polled["operation_id"] == "operation-plan"
    assert polled["state"] == "succeeded"
    assert client.calls[1][0:2] == (
        "operations.get",
        {"operation_id": "operation-plan"},
    )

    assert cli._cmd_masterjet_ollama(_args("apply")) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["operation_id"] == "operation-apply"
    assert [call[0] for call in client.calls[2:]] == [
        "operations.get",
        "ollama.instance.apply",
    ]


@pytest.mark.parametrize(
    ("label", "state"),
    (("failed", "failed"), ("stale", "failed"), ("unknown", "unknown")),
)
def test_masterjet_ollama_entrypoint_locks_mutations_after_terminal_failure(
    label: str,
    state: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert label in {"failed", "stale", "unknown"}
    client = _Client([_status("operation-plan", state)])
    monkeypatch.setattr(cli, "load_config", lambda _path: SimpleNamespace(masterjet=object()))
    monkeypatch.setattr(cli, "_new_masterjet_client", lambda *_args, **_kwargs: client)

    assert cli._cmd_masterjet_ollama(_args("apply")) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": False, "code": "control.plan_stale"}
    assert [call[0] for call in client.calls] == ["operations.get"]
