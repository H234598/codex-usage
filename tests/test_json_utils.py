from __future__ import annotations

import pytest

from codex_usage.json_utils import loads_strict


def test_loads_strict_rejects_deeply_nested_json_as_value_error():
    nested_json = "[" * 100_000 + "]" * 100_000

    with pytest.raises(ValueError, match="JSON nesting is too deep"):
        loads_strict(nested_json)


def test_loads_strict_rejects_duplicate_object_keys():
    with pytest.raises(ValueError, match="duplicate JSON key: usage"):
        loads_strict('{"usage": 97, "usage": 55}')
