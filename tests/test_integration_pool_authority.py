from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "pool_authority_v2"
GENERATION_ID = "b" * 32
RELEASE_ID = "0.6.537-" + "a" * 16
PAYLOAD_DIGEST = "1" * 64
BINDING_DIGEST = "2" * 64


def _source_bytes() -> bytes:
    return (FIXTURES / "source-v2-positive.json").read_bytes()


def _usage_document() -> dict[str, object]:
    return json.loads((FIXTURES / "usage-v2-positive.json").read_bytes())


def _projection_bytes() -> bytes:
    from codex_usage.integration_pool_authority import (
        build_pool_authority_projection,
        parse_pool_authority_source,
        serialize_pool_authority_projection,
    )

    projection = build_pool_authority_projection(
        source=parse_pool_authority_source(_source_bytes()),
        usage_document=_usage_document(),
        usage_binding_published_at="2026-08-31T12:00:00Z",
        generation_id=GENERATION_ID,
        release_id=RELEASE_ID,
        usage_payload_sha256=PAYLOAD_DIGEST,
        usage_binding_sha256=BINDING_DIGEST,
    )
    return serialize_pool_authority_projection(projection)


def _request():
    from codex_usage.integration_pool_authority import PoolAuthorityRequest

    return PoolAuthorityRequest(
        account_id="synthetic-alpha",
        pool_id="synthetic-primary",
        provider="openai",
        model_family="sol",
        reasoning="high",
        lifecycle="persistent",
        require_persistent_leadership=True,
        require_long_running_leadership=True,
    )


def _evaluate(payload: bytes, request=None, **overrides) -> bool:
    from codex_usage.integration_pool_authority import evaluate_pool_authority

    values = {
        "now": datetime(2026, 8, 31, 12, 10, tzinfo=UTC),
        "expected_release_id": RELEASE_ID,
        "expected_generation_id": GENERATION_ID,
        "expected_usage_payload_sha256": PAYLOAD_DIGEST,
        "expected_usage_binding_sha256": BINDING_DIGEST,
    }
    values.update(overrides)
    return evaluate_pool_authority(payload, request or _request(), **values)


def _mixed_source_and_usage(quality: str):
    source = json.loads(_source_bytes())
    second_authority = dict(source["authorities"][0])
    second_authority["account_id"] = "synthetic-beta"
    source["authorities"].append(second_authority)
    usage = _usage_document()
    second_account = json.loads(json.dumps(usage["accounts"][0]))
    second_account["account_id"] = "synthetic-beta"
    if quality in {"partial", "unknown", "error"}:
        second_account["status"] = quality
    elif quality == "stale":
        second_account["freshness"]["stale"] = True
    else:  # pragma: no cover - closed test helper input
        raise AssertionError(quality)
    usage["accounts"].append(second_account)
    return source, usage


def test_negative_vector_artifact_is_versioned_and_has_unique_case_ids():
    artifact = json.loads(
        (FIXTURES / "negative-vectors-v2.json").read_text(encoding="utf-8")
    )
    assert set(artifact) == {"artifact_schema_version", "cases"}
    assert type(artifact["artifact_schema_version"]) is int
    assert artifact["artifact_schema_version"] == 2
    case_ids = [case["id"] for case in artifact["cases"]]
    assert case_ids
    assert len(case_ids) == len(set(case_ids))


def test_positive_source_projection_and_decision_are_canonical_and_closed():
    from codex_usage.integration_pool_authority import (
        POOL_AUTHORITY_SCHEMA_VERSION,
        POOL_AUTHORITY_SOURCE_SCHEMA_VERSION,
        PRODUCER_VERSION,
        parse_pool_authority_projection,
        parse_pool_authority_source,
        serialize_pool_authority_source,
    )

    source = parse_pool_authority_source(_source_bytes())
    assert serialize_pool_authority_source(source) == _source_bytes()
    assert source["pool_authority_source_schema_version"] == 2
    projection = parse_pool_authority_projection(_projection_bytes())
    assert _projection_bytes() == (
        FIXTURES / "projection-v2-positive.json"
    ).read_bytes().rstrip(b"\n")
    assert projection["pool_authority_schema_version"] == 2
    assert projection["producer_version"] == "0.6.537"
    assert projection["issued_at"] == "2026-08-31T12:00:00Z"
    assert projection["expires_at"] == "2026-08-31T12:15:00Z"
    assert _evaluate(_projection_bytes()) is True
    assert (
        POOL_AUTHORITY_SCHEMA_VERSION,
        POOL_AUTHORITY_SOURCE_SCHEMA_VERSION,
        PRODUCER_VERSION,
    ) == (2, 2, "0.6.537")


@pytest.mark.parametrize(
    ("now", "expected"),
    (
        (datetime(2026, 8, 31, 11, 59, 59, 999999, tzinfo=UTC), False),
        (datetime(2026, 8, 31, 12, 0, tzinfo=UTC), True),
    ),
)
def test_authority_is_valid_only_at_or_after_issued_at(now, expected):
    assert _evaluate(_projection_bytes(), now=now) is expected


@pytest.mark.parametrize("quality", ("partial", "unknown", "error", "stale"))
def test_mixed_account_quality_publishes_fresh_authority_and_closes_only_bad_account(
    quality,
):
    from codex_usage.integration_pool_authority import (
        build_pool_authority_projection,
        serialize_pool_authority_projection,
    )

    source, usage = _mixed_source_and_usage(quality)
    projection = build_pool_authority_projection(
        source=source,
        usage_document=usage,
        usage_binding_published_at="2026-08-31T12:00:00Z",
        generation_id=GENERATION_ID,
        release_id=RELEASE_ID,
        usage_payload_sha256=PAYLOAD_DIGEST,
        usage_binding_sha256=BINDING_DIGEST,
    )
    payload = serialize_pool_authority_projection(projection)
    authorities = {
        authority["account_id"]: authority
        for authority in projection["authorities"]
    }
    assert set(authorities) == {"synthetic-alpha", "synthetic-beta"}
    assert projection["expires_at"] == "2026-08-31T12:15:00Z"
    assert _evaluate(payload) is True
    bad_request = replace(_request(), account_id="synthetic-beta")
    assert _evaluate(payload, request=bad_request) is False
    assert authorities["synthetic-beta"]["hive_available"] is False
    assert authorities["synthetic-beta"]["persistent_leadership_eligible"] is False
    assert authorities["synthetic-beta"]["long_running_leadership_eligible"] is False


@pytest.mark.parametrize(
    "case",
    json.loads((FIXTURES / "negative-vectors-v2.json").read_text(encoding="utf-8"))[
        "cases"
    ],
    ids=lambda case: case["id"],
)
def test_versioned_negative_vectors_fail_closed(case):
    from codex_usage.integration_pool_authority import (
        parse_pool_authority_projection,
        serialize_pool_authority_projection,
    )

    payload = _projection_bytes()
    request = _request()
    overrides = {}
    kind = case["kind"]
    if kind == "clock":
        overrides["now"] = datetime.fromisoformat(case["now"].replace("Z", "+00:00"))
    elif kind == "expected_generation":
        overrides["expected_generation_id"] = case["value"]
    elif kind == "expected_release":
        overrides["expected_release_id"] = case["value"]
    elif kind == "request":
        request = replace(request, **{case["field"]: case["value"]})
    elif kind in {"authority", "projection"}:
        document = parse_pool_authority_projection(payload)
        if kind == "authority":
            document["authorities"][0][case["field"]] = case["value"]
        else:
            document[case["field"]] = case["value"]
        payload = serialize_pool_authority_projection(document)
    elif kind == "usage":
        usage = _usage_document()
        usage["accounts"][0][case["field"]] = case["value"]
        from codex_usage.integration_pool_authority import (
            build_pool_authority_projection,
            parse_pool_authority_source,
        )

        document = build_pool_authority_projection(
            source=parse_pool_authority_source(_source_bytes()),
            usage_document=usage,
            usage_binding_published_at="2026-08-31T12:00:00Z",
            generation_id=GENERATION_ID,
            release_id=RELEASE_ID,
            usage_payload_sha256=PAYLOAD_DIGEST,
            usage_binding_sha256=BINDING_DIGEST,
        )
        payload = serialize_pool_authority_projection(document)
        assert _evaluate(payload) is False
        return
    elif kind == "binding_published_at":
        from codex_usage.integration_pool_authority import (
            PoolAuthorityInvalid,
            build_pool_authority_projection,
            parse_pool_authority_source,
        )

        with pytest.raises(PoolAuthorityInvalid):
            build_pool_authority_projection(
                source=parse_pool_authority_source(_source_bytes()),
                usage_document=_usage_document(),
                usage_binding_published_at=case["value"],
                generation_id=GENERATION_ID,
                release_id=RELEASE_ID,
                usage_payload_sha256=PAYLOAD_DIGEST,
                usage_binding_sha256=BINDING_DIGEST,
            )
        return
    else:  # pragma: no cover - vector kind is a closed artifact contract
        raise AssertionError(kind)
    assert _evaluate(payload, request=request, **overrides) is False


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_account",
        "unknown_field",
        "duplicate_authority",
        "secret_field",
        "secret_value",
    ),
)
def test_source_missing_partial_unknown_or_secret_shaped_content_is_rejected(mutation):
    from codex_usage.integration_pool_authority import (
        PoolAuthorityInvalid,
        parse_pool_authority_source,
    )

    source = json.loads(_source_bytes())
    if mutation == "missing_account":
        source["authorities"] = []
        parsed = parse_pool_authority_source(
            (json.dumps(source, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )
        from codex_usage.integration_pool_authority import build_pool_authority_projection

        with pytest.raises(PoolAuthorityInvalid):
            build_pool_authority_projection(
                source=parsed,
                usage_document=_usage_document(),
                usage_binding_published_at="2026-08-31T12:00:00Z",
                generation_id=GENERATION_ID,
                release_id=RELEASE_ID,
                usage_payload_sha256=PAYLOAD_DIGEST,
                usage_binding_sha256=BINDING_DIGEST,
            )
        return
    if mutation == "unknown_field":
        source["future"] = True
    elif mutation == "duplicate_authority":
        source["authorities"].append(dict(source["authorities"][0]))
    elif mutation == "secret_value":
        source["authorities"][0]["account_id"] = (
            "aaaaaaaa.bbbbbbbb.cccccccccccccccccc"
        )
    else:
        source["authorities"][0]["credential"] = "synthetic-forbidden"
    payload = (json.dumps(source, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with pytest.raises(PoolAuthorityInvalid):
        parse_pool_authority_source(payload)


def test_projection_tampering_and_noncanonical_bytes_are_rejected():
    from codex_usage.integration_pool_authority import (
        PoolAuthorityInvalid,
        parse_pool_authority_projection,
    )

    payload = _projection_bytes()
    assert _evaluate(payload.replace(b"synthetic-primary", b"synthetic-tampered")) is False
    with pytest.raises(PoolAuthorityInvalid):
        parse_pool_authority_projection(payload + b"\n")
