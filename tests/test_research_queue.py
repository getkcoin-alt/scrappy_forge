from __future__ import annotations

import pytest

from scrappy_forge.experience_ingest import (
    experiment_request_envelope,
    ingest_experience_bundle,
)
from scrappy_forge.research_queue import plan_benchmark, registered_suites
from tests.test_experience_ingest import _bundle


def _request() -> dict:
    candidate = ingest_experience_bundle(_bundle())
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
