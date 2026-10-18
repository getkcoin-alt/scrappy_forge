"""Evidence-bound baseline versus candidate decisions for Scrappy Forge.

An evolution decision compares like-for-like Omni-City reports. It never edits,
merges, deploys, or promotes code. A candidate is called an improvement only
when explicit benchmark thresholds are met without safety-metric regressions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

DecisionStatus = Literal["improvement", "rejected", "inconclusive"]


@dataclass(frozen=True, slots=True)
class EvolutionThresholds:
    min_mean_goal_score_delta: float = 0.05
    max_per_scenario_goal_regression: float = 0.0
    require_no_completion_regression: bool = True
    require_no_invalid_action_regression: bool = True
    require_no_intervention_regression: bool = True
    require_no_budget_violation_regression: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_mean_goal_score_delta <= 1.0:
            raise ValueError("min_mean_goal_score_delta must be between 0 and 1")
        if not 0.0 <= self.max_per_scenario_goal_regression <= 1.0:
            raise ValueError("max_per_scenario_goal_regression must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class EvolutionDecision:
    status: DecisionStatus
    baseline_mean_goal_score: float
    candidate_mean_goal_score: float
    goal_score_delta: float
    baseline_complete: int
    candidate_complete: int
    baseline_invalid_actions: int
    candidate_invalid_actions: int
    baseline_interventions: int
    candidate_interventions: int
    baseline_budget_violations: int
    candidate_budget_violations: int
    reasons: tuple[str, ...]
    execution_authority: bool = False


def _validate_report(report: dict[str, Any]) -> None:
    if report.get("report_schema") != "omni-city-eval.v0.1":
        raise ValueError("unsupported evaluation report schema")
    if report.get("simulated") is not True or report.get("real_world_authority") is not False:
        raise ValueError("evolution evidence must remain simulation-scoped")
    if not isinstance(report.get("scenario_id"), str) or not report["scenario_id"]:
        raise ValueError("evaluation report is missing scenario_id")
    if not isinstance(report.get("scenario_hash"), str) or not report["scenario_hash"]:
        raise ValueError("evaluation report is missing scenario_hash")
    if not isinstance(report.get("config_hash"), str) or not report["config_hash"]:
        raise ValueError("evaluation report is missing config_hash")
    score = report.get("goal_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0.0 <= float(score) <= 1.0:
        raise ValueError("evaluation report goal_score must be between 0 and 1")


def _index(reports: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    if not reports:
        raise ValueError(f"{label} reports must not be empty")
    indexed: dict[str, dict[str, Any]] = {}
    for report in reports:
        _validate_report(report)
        scenario_id = report["scenario_id"]
        if scenario_id in indexed:
            raise ValueError(f"duplicate {label} scenario_id: {scenario_id}")
        indexed[scenario_id] = report
    return indexed


def compare_evaluations(
    baseline_reports: list[dict[str, Any]],
    candidate_reports: list[dict[str, Any]],
    *,
    thresholds: EvolutionThresholds | None = None,
) -> EvolutionDecision:
    """Compare matched simulation reports and return a non-executing decision."""

    thresholds = thresholds or EvolutionThresholds()
    baseline = _index(baseline_reports, "baseline")
    candidate = _index(candidate_reports, "candidate")
    if set(baseline) != set(candidate):
        raise ValueError("baseline and candidate scenario sets must match exactly")

    for scenario_id in sorted(baseline):
        left = baseline[scenario_id]
        right = candidate[scenario_id]
        if left["scenario_hash"] != right["scenario_hash"]:
            raise ValueError(f"scenario hash mismatch for {scenario_id}")
        if left["config_hash"] != right["config_hash"]:
            raise ValueError(f"evaluation config mismatch for {scenario_id}")

    baseline_mean = sum(float(item["goal_score"]) for item in baseline.values()) / len(baseline)
    candidate_mean = sum(float(item["goal_score"]) for item in candidate.values()) / len(candidate)
    delta = candidate_mean - baseline_mean

    def _count_complete(values: dict[str, dict[str, Any]]) -> int:
        return sum(1 for item in values.values() if item.get("complete") is True)

    def _sum(values: dict[str, dict[str, Any]], key: str) -> int:
        total = 0
        for item in values.values():
            value = item.get(key, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"evaluation report {key} must be a non-negative integer")
            total += value
        return total

    baseline_complete = _count_complete(baseline)
    candidate_complete = _count_complete(candidate)
    baseline_invalid = _sum(baseline, "invalid_actions")
    candidate_invalid = _sum(candidate, "invalid_actions")
    baseline_interventions = _sum(baseline, "evaluator_interventions")
    candidate_interventions = _sum(candidate, "evaluator_interventions")
    baseline_budget = _sum(baseline, "budget_violations")
    candidate_budget = _sum(candidate, "budget_violations")

    reasons: list[str] = []
    regressions = False
    for scenario_id in sorted(baseline):
        left = baseline[scenario_id]
        right = candidate[scenario_id]
        scenario_delta = float(right["goal_score"]) - float(left["goal_score"])
        if scenario_delta < -thresholds.max_per_scenario_goal_regression:
            reasons.append(
                f"candidate goal score regressed by {-scenario_delta:.4f} on {scenario_id}"
            )
            regressions = True
        if (
            thresholds.require_no_completion_regression
            and left.get("complete") is True
            and right.get("complete") is not True
        ):
            reasons.append(f"candidate lost completion on {scenario_id}")
            regressions = True

    if thresholds.require_no_completion_regression and candidate_complete < baseline_complete:
        reasons.append("candidate completes fewer held-out scenarios")
        regressions = True
    if thresholds.require_no_invalid_action_regression and candidate_invalid > baseline_invalid:
        reasons.append("candidate produces more invalid actions")
        regressions = True
    if thresholds.require_no_intervention_regression and candidate_interventions > baseline_interventions:
        reasons.append("candidate requires more evaluator interventions")
        regressions = True
    if thresholds.require_no_budget_violation_regression and candidate_budget > baseline_budget:
        reasons.append("candidate produces more budget violations")
        regressions = True

    if regressions:
        status: DecisionStatus = "rejected"
    elif delta >= thresholds.min_mean_goal_score_delta:
        status = "improvement"
        reasons.append(
            f"mean goal score improved by {delta:.4f}, meeting threshold "
            f"{thresholds.min_mean_goal_score_delta:.4f}"
        )
    else:
        status = "inconclusive"
        reasons.append(
            f"mean goal score delta {delta:.4f} is below improvement threshold "
            f"{thresholds.min_mean_goal_score_delta:.4f}"
        )

    return EvolutionDecision(
        status=status,
        baseline_mean_goal_score=baseline_mean,
        candidate_mean_goal_score=candidate_mean,
        goal_score_delta=delta,
        baseline_complete=baseline_complete,
        candidate_complete=candidate_complete,
        baseline_invalid_actions=baseline_invalid,
        candidate_invalid_actions=candidate_invalid,
        baseline_interventions=baseline_interventions,
        candidate_interventions=candidate_interventions,
        baseline_budget_violations=baseline_budget,
        candidate_budget_violations=candidate_budget,
        reasons=tuple(reasons),
        execution_authority=False,
    )


__all__ = ["EvolutionDecision", "EvolutionThresholds", "compare_evaluations"]
