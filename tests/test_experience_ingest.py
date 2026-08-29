from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from scrappy_forge.experience_ingest import (
    experiment_request_envelope,
    ingest_experience_bundle,
)


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _bundle(*, outcome: str = "failed") -> dict:
    correlation = "11111111-1111-4111-8111-111111111111"
    objective = "22222222-2222-4222-8222-222222222222"
    envelope = {
        "protocol": "SYNCBOND",
        "schema_version": "5.0.0",
        "event_id": "33333333-3333-4333-8333-333333333333",
        "correlation_id": correlation,
        "actor_id": "service:vault-zeta",
        "actor_kind": "service",
        "event_type": "experience.recorded",
        "source": "vault-zeta",
        "created_at": "2026-08-29T00:01:00+00:00",
        "resolution": "known",
        "confidence": None,
        "provenance": [
            {
                "source": "scrappy-os",
                "observed_at": "2026-08-29T00:00:00+00:00",
                "reference": f"/tasks/{objective}",
            }
        ],
        "payload": {
            "objective_id": objective,
            "summary": "Bounded task produced a verified non-success outcome.",
            "outcome": outcome,
            "evidence": ["state=failed", "verified_steps=1"],
            "lessons": [],
        },
    }
    value = {
        "bundle_format": "syncbond.experience-evidence.v1",
        "protocol": "SYNCBOND",
        "schema_version": "5.0.0",
        "correlation_id": correlation,
        "remote_objective_id": objective,
        "envelope": envelope,
    }
    value["bundle_sha256"] = hashlib.sha256(_canonical(value)).hexdigest()
    return value


def _rehash(bundle: dict) -> dict:
    value = {key: val for key, val in bundle.items() if key != "bundle_sha256"}
    bundle["bundle_sha256"] = hashlib.sha256(_canonical(value)).hexdigest()
    return bundle


def test_failed_experience_becomes_metadata_only_candidate():
    candidate = ingest_experience_bundle(_bundle())

    assert candidate is not None
    assert str(candidate.correlation_id) == "11111111-1111-4111-8111-111111111111"
    assert str(candidate.remote_objective_id) == "22222222-2222-4222-8222-222222222222"
    assert candidate.outcome == "failed"
    assert candidate.evidence == ("state=failed", "verified_steps=1")
    assert len(candidate.source_bundle_sha256) == 64


def test_candidate_becomes_pending_research_request_without_execution_authority():
    candidate = ingest_experience_bundle(_bundle())
    assert candidate is not None

    request = experiment_request_envelope(candidate)

    assert request["event_type"] == "experiment.requested"
    assert request["correlation_id"] == str(candidate.correlation_id)
    assert request["resolution"] == "pending"
    assert request["payload"]["execution_authority"] is False
    assert request["payload"]["next_stage"] == "research-and-benchmark"
    assert request["payload"]["source_bundle_sha256"] == candidate.source_bundle_sha256


def test_succeeded_experience_is_not_failure_driven_candidate():
    assert ingest_experience_bundle(_bundle(outcome="succeeded")) is None


def test_tampered_bundle_fails_before_triage():
    bundle = _bundle()
    bundle["envelope"]["payload"]["outcome"] = "succeeded"

    with pytest.raises(ValueError, match="hash verification failed"):
        ingest_experience_bundle(bundle)


def test_rehashed_wrong_event_type_still_fails_contract():
    bundle = deepcopy(_bundle())
    bundle["envelope"]["event_type"] = "experiment.result"
    _rehash(bundle)

    with pytest.raises(ValueError, match="experience.recorded"):
        ingest_experience_bundle(bundle)


def test_rehashed_correlation_mismatch_fails_continuity():
    bundle = deepcopy(_bundle())
    bundle["envelope"]["correlation_id"] = "44444444-4444-4444-8444-444444444444"
    _rehash(bundle)

    with pytest.raises(ValueError, match="correlation continuity"):
        ingest_experience_bundle(bundle)


def test_unsupported_version_fails_closed_even_with_valid_hash():
    bundle = deepcopy(_bundle())
    bundle["schema_version"] = "6.0.0"
    _rehash(bundle)

    with pytest.raises(ValueError, match="Unsupported SYNCBOND schema version"):
        ingest_experience_bundle(bundle)
