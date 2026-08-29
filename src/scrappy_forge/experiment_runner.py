"""Bounded execution of allow-listed Scrappy Forge benchmark plans.

This runner executes only simulator evaluations selected by research_queue. It
cannot run shell commands, mutate repositories, deploy services, or act in the
real world. Promotion remains a separate human-controlled process.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evolution import EvolutionDecision, EvolutionThresholds, compare_evaluations
from .omni_eval import EvaluationConfig, OmniController, evaluate_file
from .research_queue import BenchmarkPlan

_ALLOWED_SUITE = "omni-city-held-out-v0"
_ALLOWED_GLOB = "benchmarks/omni_city_v0/held_out/*.json"


@dataclass(frozen=True, slots=True)
class ExperimentRun:
    suite_id: str
    correlation_id: str
    baseline_controller_id: str
    candidate_controller_id: str
    baseline_reports: tuple[dict[str, Any], ...]
    candidate_reports: tuple[dict[str, Any], ...]
    decision: EvolutionDecision
    simulated: bool = True
    real_world_authority: bool = False
    execution_authority: bool = False


def _scenario_paths(plan: BenchmarkPlan, repo_root: Path) -> list[Path]:
    if plan.suite_id != _ALLOWED_SUITE or plan.scenario_glob != _ALLOWED_GLOB:
        raise ValueError("benchmark plan does not match the registered held-out suite")
    if plan.simulated is not True or plan.real_world_authority is not False:
        raise ValueError("benchmark plan violates simulation boundary")
    if plan.execution_authority is not False:
        raise ValueError("benchmark plan must not carry execution authority")

    root = repo_root.resolve()
    paths = sorted(root.glob(_ALLOWED_GLOB))
    if not paths:
        raise ValueError("held-out benchmark suite contains no scenarios")
    for path in paths:
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("held-out scenario resolved outside repository root") from exc
        if resolved.suffix != ".json" or not resolved.is_file():
            raise ValueError("held-out benchmark contains an invalid scenario path")
    return paths


async def run_benchmark_plan(
    plan: BenchmarkPlan,
    *,
    baseline: OmniController,
    candidate: OmniController,
    repo_root: Path,
    config: EvaluationConfig | None = None,
    thresholds: EvolutionThresholds | None = None,
) -> ExperimentRun:
    """Run matched baseline/candidate episodes over the allow-listed suite."""

    paths = _scenario_paths(plan, repo_root)
    baseline_reports: list[dict[str, Any]] = []
    candidate_reports: list[dict[str, Any]] = []

    for path in paths:
        baseline_reports.append(await evaluate_file(path, baseline, config=config))
        candidate_reports.append(await evaluate_file(path, candidate, config=config))

    decision = compare_evaluations(
        baseline_reports,
        candidate_reports,
        thresholds=thresholds,
    )
    return ExperimentRun(
        suite_id=plan.suite_id,
        correlation_id=str(plan.correlation_id),
        baseline_controller_id=baseline.controller_id,
        candidate_controller_id=candidate.controller_id,
        baseline_reports=tuple(baseline_reports),
        candidate_reports=tuple(candidate_reports),
        decision=decision,
    )


__all__ = ["ExperimentRun", "run_benchmark_plan"]
