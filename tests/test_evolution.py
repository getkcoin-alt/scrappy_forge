from __future__ import annotations

import copy

import pytest

from scrappy_forge.evolution import EvolutionThresholds, compare_evaluations


def _report(
    scenario_id: str,
    *,
    score: float,
    complete: bool = False,
    invalid: int = 0,
    interventions: int = 0,
    budget_violations: int = 0,
) -> dict:
    return {
        "report_schema": "omni-city-eval.v0.1",
        "simulated": True,
        "real_world_authority": False,
        "scenario_id": scenario_id,
        "scenario_hash": f"hash:{scenario_id}",
        "config_hash": "config:v1",
        "goal_score": score,
        "complete": complete,
        "invalid_actions": invalid,
        "evaluator_interventions": interventions,
        "budget_violations": budget_violations,
    }


def test_candidate_is_improvement_only_when_score_threshold_is_met_without_regression():
    baseline = [_report("a", score=0.50), _report("b", score=0.60)]
    candidate = [_report("a", score=0.60), _report("b", score=0.70)]

    decision = compare_evaluations(baseline, candidate)

    assert decision.status == "improvement"
    assert decision.goal_score_delta == pytest.approx(0.10)
    assert decision.execution_authority is False


def test_better_score_with_more_invalid_actions_is_rejected():
    baseline = [_report("a", score=0.50, invalid=0)]
    candidate = [_report("a", score=0.90, invalid=1)]

    decision = compare_evaluations(baseline, candidate)

    assert decision.status == "rejected"
    assert "more invalid actions" in " ".join(decision.reasons)


def test_small_score_gain_is_inconclusive():
    baseline = [_report("a", score=0.50)]
    candidate = [_report("a", score=0.53)]

    decision = compare_evaluations(baseline, candidate)

    assert decision.status == "inconclusive"


def test_completion_regression_rejects_even_with_higher_mean_score():
    baseline = [_report("a", score=0.70, complete=True)]
    candidate = [_report("a", score=0.90, complete=False)]

    decision = compare_evaluations(baseline, candidate)

    assert decision.status == "rejected"
    assert "fewer held-out scenarios" in " ".join(decision.reasons)


def test_scenario_sets_must_match_exactly():
    with pytest.raises(ValueError, match="scenario sets must match exactly"):
        compare_evaluations([_report("a", score=0.5)], [_report("b", score=0.6)])


def test_scenario_hash_mismatch_fails_closed():
    baseline = [_report("a", score=0.5)]
    candidate = [copy.deepcopy(baseline[0])]
    candidate[0]["goal_score"] = 0.8
    candidate[0]["scenario_hash"] = "different"

    with pytest.raises(ValueError, match="scenario hash mismatch"):
        compare_evaluations(baseline, candidate)


def test_config_hash_mismatch_fails_closed():
    baseline = [_report("a", score=0.5)]
    candidate = [copy.deepcopy(baseline[0])]
    candidate[0]["goal_score"] = 0.8
    candidate[0]["config_hash"] = "other-config"

    with pytest.raises(ValueError, match="config mismatch"):
        compare_evaluations(baseline, candidate)


def test_real_world_scoped_evidence_is_rejected():
    baseline = [_report("a", score=0.5)]
    candidate = [_report("a", score=0.8)]
    candidate[0]["real_world_authority"] = True

    with pytest.raises(ValueError, match="simulation-scoped"):
        compare_evaluations(baseline, candidate)


def test_threshold_bounds_are_validated():
    with pytest.raises(ValueError, match="between 0 and 1"):
        EvolutionThresholds(min_mean_goal_score_delta=1.1)
