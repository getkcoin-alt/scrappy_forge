"""Deterministic research planning for SYNCBOND experiment requests.

This module decides *where an experiment may be evaluated*, not how to execute
it. Only explicit benchmark suites registered here may be selected. A generated
BenchmarkPlan carries zero host or real-world authority and must be handed to a
separate evaluator runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from scrappy_forge.syncbond import validate_envelope


@dataclass(frozen=True, slots=True)
class BenchmarkSuite:
    suite_id: str
    scope: str
    scenario_glob: str
    simulated: bool
    real_world_authority: bool


@dataclass(frozen=True, slots=True)
class BenchmarkPlan:
    correlation_id: UUID
    request_event_id: UUID
    suite_id: str
    scope: str
    scenario_glob: str
    simulated: bool
    real_world_authority: bool
    execution_authority: bool
    reason: str


_SUITES: dict[str, BenchmarkSuite] = {
    "omni-city-held-out-v0": BenchmarkSuite(
        suite_id="omni-city-held-out-v0",
        scope="general-world-model-and-planning",
        scenario_glob="benchmarks/omni_city_v0/held_out/*.json",
        simulated=True,
        real_world_authority=False,
    ),
}


def registered_suites() -> tuple[BenchmarkSuite, ...]:
    return tuple(_SUITES[key] for key in sorted(_SUITES))


def plan_benchmark(
    request: dict[str, Any],
    *,
    suite_id: str = "omni-city-held-out-v0",
) -> BenchmarkPlan:
    """Validate one experiment request and select an allow-listed benchmark.

    Evidence cannot name an arbitrary command, path, URL or tool. The caller may
    select only a suite already registered in this module.
    """

    validate_envelope(request)
    if request.get("event_type") != "experiment.requested":
        raise ValueError("research planning requires experiment.requested")
    if request.get("resolution") != "pending":
        raise ValueError("experiment request must still be pending")

    payload = request.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("experiment request payload must be an object")
    if payload.get("trigger") != "verified-experience":
        raise ValueError("unsupported experiment trigger")
    if payload.get("execution_authority") is not False:
        raise ValueError("experiment request must not carry execution authority")
    if payload.get("next_stage") != "research-and-benchmark":
        raise ValueError("experiment request is not routed to research-and-benchmark")

    suite = _SUITES.get(suite_id)
    if suite is None:
        raise ValueError(f"benchmark suite is not registered: {suite_id}")
    if not suite.simulated or suite.real_world_authority:
        raise ValueError("registered benchmark suite violates simulation boundary")

    try:
        correlation_id = UUID(str(request["correlation_id"]))
        request_event_id = UUID(str(request["event_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("experiment request identifiers are invalid") from exc

    outcome = str(payload.get("outcome", "unknown"))
    return BenchmarkPlan(
        correlation_id=correlation_id,
        request_event_id=request_event_id,
        suite_id=suite.suite_id,
        scope=suite.scope,
        scenario_glob=suite.scenario_glob,
        simulated=suite.simulated,
        real_world_authority=suite.real_world_authority,
        execution_authority=False,
        reason=f"verified Experience outcome={outcome}",
    )


__all__ = [
    "BenchmarkPlan",
    "BenchmarkSuite",
    "plan_benchmark",
    "registered_suites",
]
