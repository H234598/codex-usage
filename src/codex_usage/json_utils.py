from __future__ import annotations

import json
from typing import Any

MAX_JSON_NESTING = 128


def loads_strict(value: str | bytes | bytearray) -> Any:
    _reject_deep_nesting(value)
    try:
        return json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except RecursionError as exc:
        raise ValueError("JSON nesting is too deep") from exc


def _reject_deep_nesting(value: str | bytes | bytearray) -> None:
    if isinstance(value, str):
        sequence = value
        quote, escape = '"', "\\"
        open_array, open_object = "[", "{"
        close_array, close_object = "]", "}"
    else:
        sequence = memoryview(value)
        quote, escape, open_array, open_object, close_array, close_object = 34, 92, 91, 123, 93, 125
    depth = 0
    in_string = False
    escaped = False
    for item in sequence:
        if in_string:
            if escaped:
                escaped = False
            elif item == escape:
                escaped = True
            elif item == quote:
                in_string = False
            continue
        if item == quote:
            in_string = True
        elif item == open_array or item == open_object:
            depth += 1
            if depth > MAX_JSON_NESTING:
                raise ValueError("JSON nesting is too deep")
        elif item == close_array or item == close_object:
            depth = max(0, depth - 1)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
