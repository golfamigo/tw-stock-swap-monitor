"""Tests for bounded, reversible StrategyRun evidence payloads."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from app.domain.evidence_payload import (
    CanonicalJsonSizeLimitError,
    measure_encoded_evidence_bytes,
    measure_json_bytes,
    preflight_evidence_payload,
)
from app.persistence.evidence import encode_evidence


def test_json_byte_counter_counts_utf8_and_stops_at_the_boundary() -> None:
    payload = {"text": "é" * 8_192}
    reference = "".join(
        json.JSONEncoder(
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).iterencode(payload)
    )
    expected_bytes = len(reference.encode("utf-8"))

    assert expected_bytes > 16 * 1024
    assert (
        measure_json_bytes(payload, limit_bytes=expected_bytes, boundary="test") == expected_bytes
    )
    with pytest.raises(CanonicalJsonSizeLimitError, match="boundary=test") as error:
        measure_json_bytes(payload, limit_bytes=16 * 1024, boundary="test")

    assert error.value.limit_bytes == 16 * 1024
    assert error.value.observed_at_least_bytes > 16 * 1024


def test_json_byte_counter_accepts_an_exact_boundary_and_rejects_one_extra_byte() -> None:
    limit = 64
    exact_payload = {"x": "a" * (limit - 8)}
    over_limit_payload = {"x": "a" * (limit - 7)}

    assert measure_json_bytes(exact_payload, limit_bytes=limit, boundary="exact") == limit
    with pytest.raises(CanonicalJsonSizeLimitError, match="boundary=one-byte-over"):
        measure_json_bytes(over_limit_payload, limit_bytes=limit, boundary="one-byte-over")


def test_evidence_preflight_rejects_invalid_values_before_recursive_freeze() -> None:
    nested: object = {"leaf": "value"}
    for _ in range(33):
        nested = [nested]

    with pytest.raises(ValueError, match="depth"):
        preflight_evidence_payload(nested)
    with pytest.raises(ValueError, match="keys"):
        preflight_evidence_payload({1: "invalid"})
    with pytest.raises(ValueError, match="float"):
        preflight_evidence_payload({"value": float("nan")})
    with pytest.raises(ValueError, match="timezone-aware"):
        preflight_evidence_payload({"at": datetime(2035, 1, 1)})
    with pytest.raises(ValueError, match="Decimal"):
        preflight_evidence_payload({"amount": Decimal("1E+1000000000")})


def test_encoded_evidence_counter_measures_the_exact_persistence_envelope() -> None:
    payload = {
        "at": datetime(2035, 1, 1, tzinfo=UTC),
        "amount": Decimal("1.25"),
        "instrument": UUID(int=7),
        "items": ("a", 2),
    }
    encoded = encode_evidence(payload)
    reference = "".join(
        json.JSONEncoder(
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).iterencode(encoded)
    )

    assert measure_encoded_evidence_bytes(
        payload,
        limit_bytes=4_096,
        boundary="StrategyRun.outputs",
    ) == len(reference.encode("utf-8"))
