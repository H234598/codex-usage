from __future__ import annotations

import json
from typing import Any


def loads_strict(value: str | bytes | bytearray) -> Any:
    try:
        return json.loads(value, parse_constant=_reject_constant)
    except RecursionError as exc:
        raise ValueError("JSON nesting is too deep") from exc


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
