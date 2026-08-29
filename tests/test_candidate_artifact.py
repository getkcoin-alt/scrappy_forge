from __future__ import annotations

from dataclasses import replace

import pytest

from scrappy_forge.candidate_artifact import (
    CheckAttestation,
    HumanApproval,
    build_candidate_manifest,
    evaluate_promotion,
    verify_candidate_manifest,
)
from scrappy_forge.evolution import EvolutionDecision
from scrappy_forge.experiment_runner import ExperimentRun


def _decision(*, status: str = "improvement") -> EvolutionDecision:
    return EvolutionDecision(
        status=status,
        baseline_mean_goal_score=0.50,
        candidate_mean_goal_score=0.65,
        goal_score_delta=0.15,
        baseline_complete=1,
        candidate_complete=2,
        baseline_invalid_actions=1,
        candidate_invalid_actions=0,
        baseline_interventions=1,
        candidate_interventions=0,
        baseline_budget_violations=0,
        candidate_budget_violations=0,
        reasons=("measured improvement",),
        execution_authority=False,
    )


def _run(*, status: str = "improvement") -> ExperimentRun:
    baseline = ({"scenario_id": "a", "goal_score": 0.5},)
    candidate = ({"scenario_id": "a", "goal_score": 0.65},)
    return ExperimentRun(
        suite_id="omni-city-held-out-v0",
        correlation_id="11111111-1111-4111-8111-111111111111",
        baseline_controller_id="baseline",
        candidate_controller_id="candidate",
        baseline_reports=baseline,
        candidate_reports=candidate,
        decision=_decision(status=status),
        simulated=True,
        real_world_authority=False,
        execution_authority=False,
    )


def _manifest():
    return build_candidate_manifest(
        _run(),
        baseline_source_commit="a" * 40,
        candidate_source_commit="b" * 40,
    )


def _security(manifest_hash: str, *, status: str = "passed") -> CheckAttestation:
    return CheckAttestation(
        check_name="security",
        manifest_sha256=manifest_hash,
        status=status,
        evidence_ref="ci://security/123",
        observed_at="2026-08-29T02:30:00+00:00",
    )


def _regression(manifest_hash: str, *, status: str = "passed") -> CheckAttestation:
    return CheckAttestation(
        check_name="regression",
        manifest_sha256=manifest_hash,
        status=status,
        evidence_ref="ci://regression/456",
        observed_at="2026-08-29T02:31:00+00:00",
    )


def _approval(manifest_hash: str, *, decision: str = "approved") -> HumanApproval:
    return HumanApproval(
        manifest_sha256=manifest_hash,
        approver_actor_id="human:karnveer",
        actor_kind="human",
        decision=decision,
        approved_at="2026-08-29T02:32:00+00:00",
    )


def test_manifest_is_content_addressed_and_inert():
    manifest = _manifest()
    verify_candidate_manifest(manifest)

    assert len(manifest.manifest_sha256) == 64
    assert manifest.decision_status == "improvement"
    assert manifest.execution_authority is False
    assert manifest.real_world_authority is False


def test_non_improvement_cannot_become_candidate_artifact():
    with pytest.raises(ValueError, match="only measured improvements"):
        build_candidate_manifest(
            _run(status="inconclusive"),
            baseline_source_commit="a" * 40,
            candidate_source_commit="b" * 40,
        )


def test_short_or_symbolic_commit_refs_are_rejected():
    with pytest.raises(ValueError, match="full 40-character"):
        build_candidate_manifest(
            _run(),
            baseline_source_commit="main",
            candidate_source_commit="feature",
        )


def test_tampered_manifest_fails_hash_verification():
    manifest = _manifest()
    tampered = replace(manifest, candidate_controller_id="other")

    with pytest.raises(ValueError, match="hash verification failed"):
        verify_candidate_manifest(tampered)


def test_all_bound_gates_make_candidate_merge_eligible_without_merge_authority():
    manifest = _manifest()
    decision = evaluate_promotion(
        manifest,
        security=_security(manifest.manifest_sha256),
        regression=_regression(manifest.manifest_sha256),
        approval=_approval(manifest.manifest_sha256),
    )

    assert decision.eligible_for_merge is True
    assert decision.execution_authority is False
    assert decision.merge_authority is False
    assert decision.deploy_authority is False


def test_failed_security_gate_blocks_eligibility():
    manifest = _manifest()
    decision = evaluate_promotion(
        manifest,
        security=_security(manifest.manifest_sha256, status="failed"),
        regression=_regression(manifest.manifest_sha256),
        approval=_approval(manifest.manifest_sha256),
    )

    assert decision.eligible_for_merge is False
    assert "security gate did not pass" in decision.reasons


def test_missing_human_approval_blocks_eligibility():
    manifest = _manifest()
    decision = evaluate_promotion(
        manifest,
        security=_security(manifest.manifest_sha256),
        regression=_regression(manifest.manifest_sha256),
        approval=_approval(manifest.manifest_sha256, decision="rejected"),
    )

    assert decision.eligible_for_merge is False
    assert "human approval was not granted" in decision.reasons


def test_non_human_approval_is_rejected():
    manifest = _manifest()
    approval = replace(_approval(manifest.manifest_sha256), actor_kind="agent")

    with pytest.raises(ValueError, match="must come from a human actor"):
        evaluate_promotion(
            manifest,
            security=_security(manifest.manifest_sha256),
            regression=_regression(manifest.manifest_sha256),
            approval=approval,
        )


def test_attestation_for_other_manifest_cannot_be_replayed():
    manifest = _manifest()
    wrong = "f" * 64

    with pytest.raises(ValueError, match="different manifest"):
        evaluate_promotion(
            manifest,
            security=_security(wrong),
            regression=_regression(manifest.manifest_sha256),
            approval=_approval(manifest.manifest_sha256),
        )


def test_naive_attestation_timestamp_is_rejected():
    manifest = _manifest()
    security = replace(_security(manifest.manifest_sha256), observed_at="2026-08-29T02:30:00")

    with pytest.raises(ValueError, match="include a timezone"):
        evaluate_promotion(
            manifest,
            security=security,
            regression=_regression(manifest.manifest_sha256),
            approval=_approval(manifest.manifest_sha256),
        )
