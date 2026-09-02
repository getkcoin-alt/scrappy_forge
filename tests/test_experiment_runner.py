from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from scrappy_forge.experiment_runner import run_benchmark_plan
from scrappy_forge.omni_eval import ControllerDecision
from scrappy_forge.research_queue import BenchmarkPlan


class FirstActionController:
    def __init__(self, controller_id: str) -> None:
        self.controller_id = controller_id

    async def choose(self, observation, *, feedback, seed):
        actions = observation.get("available_actions", [])
        action_id = actions[0]["id"] if actions else "__finish__"
        return ControllerDecision(action_id=action_id, model=self.controller_id)


def _plan() -> BenchmarkPlan:
    return BenchmarkPlan(
        correlation_id=UUID("11111111-1111-4111-8111-111111111111"),
        request_event_id=UUID("22222222-2222-4222-8222-222222222222"),
        suite_id="omni-city-held-out-v0",
        scope="general-world-model-and-planning",
        scenario_glob="benchmarks/omni_city_v0/held_out/*.json",
        simulated=True,
        real_world_authority=False,
        execution_authority=False,
        reason="verified Experience outcome=failed",
    )


async def test_runner_executes_only_registered_held_out_suite():
    root = Path(__file__).resolve().parents[1]
    run = await run_benchmark_plan(
        _plan(),
        baseline=FirstActionController("baseline"),
        candidate=FirstActionController("candidate"),
        repo_root=root,
    )

    assert run.suite_id == "omni-city-held-out-v0"
    assert run.correlation_id == "11111111-1111-4111-8111-111111111111"
    assert len(run.baseline_reports) == len(run.candidate_reports) >= 1
    assert run.execution_authority is False
    assert run.real_world_authority is False
    assert run.simulated is True
    assert run.decision.status == "inconclusive"


async def test_runner_rejects_forged_suite_path():
    root = Path(__file__).resolve().parents[1]
    plan = _plan()
    forged = BenchmarkPlan(
        correlation_id=plan.correlation_id,
        request_event_id=plan.request_event_id,
        suite_id=plan.suite_id,
        scope=plan.scope,
        scenario_glob="../../*.json",
        simulated=True,
        real_world_authority=False,
        execution_authority=False,
        reason=plan.reason,
    )

    with pytest.raises(ValueError, match="registered held-out suite"):
        await run_benchmark_plan(
            forged,
            baseline=FirstActionController("baseline"),
            candidate=FirstActionController("candidate"),
            repo_root=root,
        )


async def test_runner_rejects_execution_authority():
    root = Path(__file__).resolve().parents[1]
    plan = _plan()
    forged = BenchmarkPlan(
        correlation_id=plan.correlation_id,
        request_event_id=plan.request_event_id,
        suite_id=plan.suite_id,
        scope=plan.scope,
        scenario_glob=plan.scenario_glob,
        simulated=True,
        real_world_authority=False,
        execution_authority=True,
        reason=plan.reason,
    )

    with pytest.raises(ValueError, match="must not carry execution authority"):
        await run_benchmark_plan(
            forged,
            baseline=FirstActionController("baseline"),
            candidate=FirstActionController("candidate"),
            repo_root=root,
        )
