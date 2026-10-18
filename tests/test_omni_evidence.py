from __future__ import annotations

from uuid import uuid4

import pytest

from scrappy_forge.omni_evidence import experiment_result_envelope
from scrappy_forge.syncbond import validate_envelope


def _report() -> dict:
    return {
        "report_schema": "omni-city-eval.v0.1",
        "simulated": True,
        "real_world_authority": False,
        "scenario_id": "heldout-test-001",
        "scenario_hash": "a" * 64,
        "scenario_seed": 7,
        "controller_id": "openrouter:free-model",
        "observed_models": ["free-model"],
        "config_hash": "b" * 64,
        "stopped_because": "goals_complete",
        "complete": True,
        "goal_score": 1.0,
        "budget": 8.0,
        "spent": 3.0,
        "requests": 2,
        "action_attempts": 2,
        "valid_actions": 2,
        "invalid_actions": 0,
        "precondition_violations": 0,
        "budget_violations": 0,
        "replans": 0,
        "evaluator_interventions": 0,
        "final_snapshot_id": "c" * 64,
    }


def test_experiment_result_is_valid_syncbond_with_caller_correlation() -> None:
    correlation_id = uuid4()
    event = experiment_result_envelope(_report(), correlation_id=correlation_id)

    validate_envelope(event)
    assert event["event_type"] == "experiment.result"
    assert event["correlation_id"] == str(correlation_id)
    assert event["payload"]["experiment_kind"] == "omni-city-held-out-evaluation"
    assert event["payload"]["simulated"] is True
    assert event["payload"]["real_world_authority"] is False
    assert event["payload"]["metrics"]["goal_score"] == 1.0
    assert len(event["payload"]["report_hash"]) == 64
    assert event["provenance"][0]["scope"] == "simulation_only"


def test_experiment_result_refuses_real_world_authority_claim() -> None:
    report = _report()
    report["real_world_authority"] = True

    with pytest.raises(ValueError, match="simulation-scoped"):
        experiment_result_envelope(report)


def test_experiment_result_refuses_unknown_report_schema() -> None:
    report = _report()
    report["report_schema"] = "future-unknown"

    with pytest.raises(ValueError, match="unsupported"):
        experiment_result_envelope(report)
