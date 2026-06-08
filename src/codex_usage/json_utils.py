from __future__ import annotations

import json
from typing import Any


def loads_strict(value: str | bytes | bytearray) -> Any:
    return json.loads(value, parse_constant=_reject_constant)


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
