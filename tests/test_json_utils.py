from __future__ import annotations

import pytest

from codex_usage.json_utils import loads_strict


def test_loads_strict_rejects_deeply_nested_json_as_value_error():
    nested_json = "[" * 100_000 + "]" * 100_000

    with pytest.raises(ValueError, match="JSON nesting is too deep"):
        loads_strict(nested_json)


def test_loads_strict_ignores_structural_characters_inside_strings():
    assert loads_strict('{"value": "[[[{\\\"still a string}]]]"}') == {
        "value": '[[[{"still a string}]]]'
    }


def test_loads_strict_rejects_duplicate_object_keys():
    with pytest.raises(ValueError, match="duplicate JSON key: usage"):
        loads_strict('{"usage": 97, "usage": 55}')


@pytest.mark.parametrize("value", [None, [], 1, object(), memoryview(b"{}")])
def test_loads_strict_rejects_invalid_input_type(value):
    with pytest.raises(ValueError, match="JSON input is invalid"):
        loads_strict(value)  # type: ignore[arg-type]


def test_loads_strict_rejects_input_subclass_hooks():
    class BrokenStr(str):
        def __iter__(self):
            raise RuntimeError("synthetic JSON string marker")

    class BrokenBytes(bytes):
        def decode(self, *_args, **_kwargs):
            raise RuntimeError("synthetic JSON bytes marker")

    for value in (BrokenStr("{}"), BrokenBytes(b"{}")):
        with pytest.raises(ValueError, match="JSON input is invalid"):
            loads_strict(value)
