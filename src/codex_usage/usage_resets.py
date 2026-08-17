from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class UsageResetState:
    available: int | None
    known: bool
    redeem_capability: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.known, bool):
            raise ValueError("known must be boolean")
        if not isinstance(self.redeem_capability, bool):
            raise ValueError("redeem_capability must be boolean")
        if not self.known:
            if self.available is not None:
                raise ValueError("unknown reset state must not have available value")
            return
        if (
            isinstance(self.available, bool)
            or not isinstance(self.available, int)
            or not 0 <= self.available <= 10_000
        ):
            raise ValueError("known reset state requires bounded available value")

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "known": self.known,
            "redeem_capability": self.redeem_capability,
        }


def parse_usage_resets(payload: object) -> UsageResetState:
    if not isinstance(payload, Mapping):
        return UsageResetState(None, False, False)
    canonical = None
    canonical_nested = False
    if all(
        key in payload
        for key in ("available", "known", "redeem_capability")
    ):
        canonical = payload
    else:
        nested = payload.get("usage_resets")
        if isinstance(nested, Mapping) and all(
            key in nested
            for key in ("available", "known", "redeem_capability")
        ):
            canonical = nested
            canonical_nested = True
    if canonical is not None:
        try:
            state = UsageResetState(
                canonical["available"],
                canonical["known"],
                canonical["redeem_capability"],
            )
        except (TypeError, ValueError):
            return UsageResetState(None, False, False)
        candidates: list[int] = []
        legacy_keys = ("resets", "available_resets")
        if not canonical_nested:
            legacy_keys += ("usage_resets",)
        for key in legacy_keys:
            if key not in payload:
                continue
            value = payload[key]
            if isinstance(value, Mapping):
                value = value.get("available")
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
                return UsageResetState(None, False, False)
            candidates.append(value)
        if candidates and (
            not state.known
            or any(value != state.available for value in candidates)
        ):
            return UsageResetState(None, False, False)
        return state
    candidates: list[int] = []
    for key in ("resets", "usage_resets", "available_resets"):
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, Mapping):
            value = value.get("available")
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
            return UsageResetState(None, False, False)
        candidates.append(value)
    if not candidates:
        return UsageResetState(None, False, False)
    if len(set(candidates)) != 1:
        return UsageResetState(None, False, False)
    capability = payload.get("redeem_capability", False)
    if not isinstance(capability, bool):
        return UsageResetState(None, False, False)
    return UsageResetState(candidates[0], True, capability)


def format_usage_resets(state: UsageResetState, *, hide_zero: bool = True) -> str:
    if not isinstance(state, UsageResetState):
        raise ValueError("reset state is invalid")
    if not isinstance(hide_zero, bool):
        raise ValueError("hide_zero must be boolean")
    if not state.known or state.available is None:
        return "—"
    if hide_zero and state.available == 0:
        return ""
    return str(state.available)


def redeem_usage_reset(state: UsageResetState) -> None:
    if not isinstance(state, UsageResetState):
        raise ValueError("reset state is invalid")
    if state.redeem_capability is not True:
        raise ValueError("reset redemption capability unavailable")
    if state.known is not True or state.available is None:
        raise ValueError("reset state must be known before redemption")
    if state.available <= 0:
        raise ValueError("reset state must have positive available value")
    raise NotImplementedError("reset redemption gate is not implemented")
