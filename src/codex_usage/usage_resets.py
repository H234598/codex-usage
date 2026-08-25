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
            type(self.available) is not int
            or not 0 <= self.available <= 10_000
        ):
            raise ValueError("known reset state requires bounded available value")

    def as_dict(self) -> dict[str, object]:
        available, known, redeem_capability = _safe_reset_state_fields(self)
        return {
            "available": available,
            "known": known,
            "redeem_capability": redeem_capability,
        }


def _safe_reset_state_fields(
    state: UsageResetState,
) -> tuple[int | None, bool, bool]:
    try:
        available = state.available
        known = state.known
        redeem_capability = state.redeem_capability
        UsageResetState(available, known, redeem_capability)
    except Exception as exc:
        raise ValueError("reset state is invalid") from exc
    return available, known, redeem_capability


def parse_usage_resets(payload: object) -> UsageResetState:
    if not isinstance(payload, Mapping):
        return UsageResetState(None, False, False)
    try:
        return _parse_usage_resets_mapping(payload)
    except Exception:
        return UsageResetState(None, False, False)


def _parse_usage_resets_mapping(payload: Mapping) -> UsageResetState:
    app_server_count = None
    if "rateLimitResetCredits" in payload:
        app_server_summary = payload["rateLimitResetCredits"]
        if app_server_summary is not None:
            if (
                not isinstance(app_server_summary, Mapping)
                or "availableCount" not in app_server_summary
            ):
                return UsageResetState(None, False, False)
            app_server_count = app_server_summary["availableCount"]
            if type(app_server_count) is not int or not 0 <= app_server_count <= 10_000:
                return UsageResetState(None, False, False)
    canonical_fields = ("available", "known", "redeem_capability")
    top_level_fields = tuple(key for key in canonical_fields if key in payload)
    nested = payload.get("usage_resets")
    nested_fields = (
        tuple(key for key in canonical_fields if key in nested)
        if isinstance(nested, Mapping)
        else ()
    )
    for present in (top_level_fields, nested_fields):
        if (
            ("known" in present or "redeem_capability" in present)
            and len(present) != len(canonical_fields)
        ):
            return UsageResetState(None, False, False)
    canonical = None
    canonical_nested = False
    if all(
        key in payload for key in canonical_fields
    ):
        canonical = payload
    else:
        if isinstance(nested, Mapping) and all(
            key in nested for key in canonical_fields
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
        if app_server_count is not None:
            candidates.append(app_server_count)
        if canonical_nested and "available" in payload:
            top_level_available = payload["available"]
            if (
                type(top_level_available) is not int
                or not 0 <= top_level_available <= 10_000
            ):
                return UsageResetState(None, False, False)
            candidates.append(top_level_available)
        legacy_keys: tuple[str, ...] = ("resets", "available_resets")
        if not canonical_nested:
            legacy_keys += ("usage_resets",)
        for key in legacy_keys:
            if key not in payload:
                continue
            value = payload[key]
            if key == "usage_resets" and isinstance(value, Mapping) and all(
                field in value
                for field in ("available", "known", "redeem_capability")
            ):
                try:
                    duplicate = UsageResetState(
                        value["available"],
                        value["known"],
                        value["redeem_capability"],
                    )
                except (TypeError, ValueError):
                    return UsageResetState(None, False, False)
                if duplicate != state:
                    return UsageResetState(None, False, False)
                continue
            value = _legacy_reset_available(value)
            if value is None:
                return UsageResetState(None, False, False)
            candidates.append(value)
        if candidates and (
            not state.known
            or any(value != state.available for value in candidates)
        ):
            return UsageResetState(None, False, False)
        return state
    legacy_candidates: list[int] = []
    if "available" in payload:
        # A lone top-level availability field is incomplete canonical data.
        # Legacy sources must not silently legitimize it.
        return UsageResetState(None, False, False)
    if app_server_count is not None:
        legacy_candidates.append(app_server_count)
    for key in ("resets", "usage_resets", "available_resets"):
        if key not in payload:
            continue
        value = _legacy_reset_available(payload[key])
        if value is None:
            return UsageResetState(None, False, False)
        legacy_candidates.append(value)
    if not legacy_candidates:
        return UsageResetState(None, False, False)
    if len(set(legacy_candidates)) != 1:
        return UsageResetState(None, False, False)
    capability = payload.get("redeem_capability", False)
    if not isinstance(capability, bool):
        return UsageResetState(None, False, False)
    return UsageResetState(legacy_candidates[0], True, capability)


def _legacy_reset_available(value: object) -> int | None:
    if isinstance(value, Mapping):
        if "known" in value or "redeem_capability" in value:
            return None
        value = value.get("available")
    if type(value) is not int or not 0 <= value <= 10_000:
        return None
    return value


def format_usage_resets(state: UsageResetState, *, hide_zero: bool = True) -> str:
    if not isinstance(state, UsageResetState):
        raise ValueError("reset state is invalid")
    if not isinstance(hide_zero, bool):
        raise ValueError("hide_zero must be boolean")
    available, known, _redeem_capability = _safe_reset_state_fields(state)
    if not known or available is None:
        return "—"
    if hide_zero and available == 0:
        return ""
    return str(available)


def redeem_usage_reset(state: UsageResetState) -> None:
    if not isinstance(state, UsageResetState):
        raise ValueError("reset state is invalid")
    available, known, redeem_capability = _safe_reset_state_fields(state)
    if redeem_capability is not True:
        raise ValueError("reset redemption capability unavailable")
    if known is not True or available is None:
        raise ValueError("reset state must be known before redemption")
    if available <= 0:
        raise ValueError("reset state must have positive available value")
    raise NotImplementedError("reset redemption gate is not implemented")
