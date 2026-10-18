"""Convert Omni-City benchmark reports into portable SYNCBOND evidence."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .syncbond import make_envelope
from .util import encoded, sha


def experiment_result_envelope(
    report: dict[str, Any],
    *,
    correlation_id: UUID | None = None,
) -> dict[str, Any]:
    """Build a compact ``experiment.result`` without claiming real-world authority."""

    if report.get("report_schema") != "omni-city-eval.v0.1":
        raise ValueError("unsupported Omni-City evaluation report")
    if report.get("simulated") is not True or report.get("real_world_authority") is not False:
        raise ValueError("Omni-City experiment evidence must remain simulation-scoped")

    report_hash = sha(encoded(report))
    metrics = {
        key: report.get(key)
        for key in (
            "complete",
            "goal_score",
            "budget",
            "spent",
            "requests",
            "action_attempts",
            "valid_actions",
            "invalid_actions",
            "precondition_violations",
            "budget_violations",
            "replans",
            "evaluator_interventions",
        )
    }
    payload = {
        "experiment_kind": "omni-city-held-out-evaluation",
        "simulated": True,
        "real_world_authority": False,
        "scenario_id": report.get("scenario_id"),
        "scenario_hash": report.get("scenario_hash"),
        "controller_id": report.get("controller_id"),
        "observed_models": report.get("observed_models", []),
        "config_hash": report.get("config_hash"),
        "report_hash": report_hash,
        "metrics": metrics,
        "stopped_because": report.get("stopped_because"),
    }
    provenance = [
        {
            "source": f"omni-city:{report.get('scenario_id')}",
            "scenario_hash": report.get("scenario_hash"),
            "final_snapshot_id": report.get("final_snapshot_id"),
            "report_hash": report_hash,
            "scope": "simulation_only",
        }
    ]
    return make_envelope(
        actor_id="node:scrappy-forge",
        actor_kind="node",
        event_type="experiment.result",
        source="scrappy-forge",
        payload=payload,
        correlation_id=correlation_id,
        resolution="known",
        confidence=1.0,
        provenance=provenance,
    )


def evidence_bundle(
    report: dict[str, Any],
    *,
    correlation_id: UUID | None = None,
) -> dict[str, Any]:
    """Return full report plus compact cross-system experiment envelope."""

    return {
        "report": report,
        "syncbond": experiment_result_envelope(report, correlation_id=correlation_id),
    }
