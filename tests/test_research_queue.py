from __future__ import annotations

import hashlib
import json

import pytest

from scrappy_forge.experience_ingest import (
    experiment_request_envelope,
    ingest_experience_bundle,
)
from scrappy_forge.research_queue import plan_benchmark, registered_suites


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _request() -> dict:
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
        "provenance": [],
        "payload": {
            "objective_id": objective,
            "summary": "Verified task failure.",
            "outcome": "failed",
            "evidence": ["state=failed"],
            "lessons": [],
        },
    }
    bundle = {
        "bundle_format": "syncbond.experience-evidence.v1",
        "protocol": "SYNCBOND",
        "schema_version": "5.0.0",
        "correlation_id": correlation,
        "remote_objective_id": objective,
        "envelope": envelope,
    }
    bundle["bundle_sha256"] = hashlib.sha256(_canonical(bundle)).hexdigest()
    candidate = ingest_experience_bundle(bundle)
    assert candidate is not None
    return experiment_request_envelope(candidate)


def test_default_plan_maps_verified_failure_to_held_out_omni_city():
    request = _request()
    plan = plan_benchmark(request)

    assert str(plan.correlation_id) == request["correlation_id"]
    assert plan.suite_id == "omni-city-held-out-v0"
    assert plan.scenario_glob == "benchmarks/omni_city_v0/held_out/*.json"
    assert plan.simulated is True
    assert plan.real_world_authority is False
    assert plan.execution_authority is False


def test_registry_contains_only_simulation_scoped_suites():
    suites = registered_suites()
    assert suites
    assert all(item.simulated is True for item in suites)
    assert all(item.real_world_authority is False for item in suites)


def test_unknown_suite_fails_closed():
    with pytest.raises(ValueError, match="not registered"):
        plan_benchmark(_request(), suite_id="shell-on-host")


def test_request_cannot_arrive_with_execution_authority():
    request = _request()
    request["payload"]["execution_authority"] = True

    with pytest.raises(ValueError, match="must not carry execution authority"):
        plan_benchmark(request)


def test_non_pending_request_is_not_plannable():
    request = _request()
    request["resolution"] = "known"

    with pytest.raises(ValueError, match="must still be pending"):
        plan_benchmark(request)
