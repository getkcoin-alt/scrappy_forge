from __future__ import annotations

import base64
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scrappy_forge.candidate_artifact import (
    CheckAttestation,
    build_candidate_manifest,
)
from scrappy_forge.evolution import EvolutionDecision
from scrappy_forge.experiment_runner import ExperimentRun
from scrappy_forge.signed_approval import (
    APPROVAL_SCHEMA,
    SIGNATURE_ALGORITHM,
    SignedPromotionApproval,
    approval_signing_bytes,
    evaluate_signed_promotion,
    verify_signed_approval,
)


def _manifest():
    decision = EvolutionDecision(
        status="improvement",
        baseline_mean_goal_score=0.5,
        candidate_mean_goal_score=0.7,
        goal_score_delta=0.2,
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
    run = ExperimentRun(
        suite_id="omni-city-held-out-v0",
        correlation_id="11111111-1111-4111-8111-111111111111",
        baseline_controller_id="baseline",
        candidate_controller_id="candidate",
        baseline_reports=({"scenario_id": "a", "goal_score": 0.5},),
        candidate_reports=({"scenario_id": "a", "goal_score": 0.7},),
        decision=decision,
        simulated=True,
        real_world_authority=False,
        execution_authority=False,
    )
    return build_candidate_manifest(
        run,
        baseline_source_commit="a" * 40,
        candidate_source_commit="b" * 40,
    )


def _keys():
    private = Ed25519PrivateKey.generate()
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, public_pem


def _approval(manifest_hash: str, private: Ed25519PrivateKey, *, decision: str = "approved"):
    unsigned = SignedPromotionApproval(
        approval_schema=APPROVAL_SCHEMA,
        manifest_sha256=manifest_hash,
        approver_actor_id="human:karnveer",
        actor_kind="human",
        decision=decision,  # type: ignore[arg-type]
        approved_at="2026-08-29T02:40:00+00:00",
        nonce="11111111-1111-4111-8111-111111111111",
        key_id="command-center-primary",
        signature_algorithm=SIGNATURE_ALGORITHM,
        signature_base64="",
    )
    signature = private.sign(approval_signing_bytes(unsigned))
    return replace(unsigned, signature_base64=base64.b64encode(signature).decode("ascii"))


def _check(name: str, manifest_hash: str) -> CheckAttestation:
    return CheckAttestation(
        check_name=name,
        manifest_sha256=manifest_hash,
        status="passed",
        evidence_ref=f"ci://{name}/123",
        observed_at="2026-08-29T02:39:00+00:00",
    )


def test_valid_signed_approval_is_merge_eligible_but_never_grants_authority():
    manifest = _manifest()
    private, public_pem = _keys()
    approval = _approval(manifest.manifest_sha256, private)

    decision = evaluate_signed_promotion(
        manifest,
        security=_check("security", manifest.manifest_sha256),
        regression=_check("regression", manifest.manifest_sha256),
        approval=approval,
        public_key_pem=public_pem,
        expected_key_id="command-center-primary",
        expected_approver_actor_id="human:karnveer",
    )

    assert decision.eligible_for_merge is True
    assert decision.execution_authority is False
    assert decision.merge_authority is False
    assert decision.deploy_authority is False


def test_signature_tampering_is_rejected():
    manifest = _manifest()
    private, public_pem = _keys()
    approval = _approval(manifest.manifest_sha256, private)
    tampered = replace(approval, decision="rejected")

    with pytest.raises(ValueError, match="signature verification failed"):
        verify_signed_approval(
            tampered,
            public_key_pem=public_pem,
            expected_manifest_sha256=manifest.manifest_sha256,
            expected_key_id="command-center-primary",
            expected_approver_actor_id="human:karnveer",
        )


def test_wrong_public_key_is_rejected():
    manifest = _manifest()
    private, _ = _keys()
    _, other_public = _keys()
    approval = _approval(manifest.manifest_sha256, private)

    with pytest.raises(ValueError, match="signature verification failed"):
        verify_signed_approval(
            approval,
            public_key_pem=other_public,
            expected_manifest_sha256=manifest.manifest_sha256,
            expected_key_id="command-center-primary",
            expected_approver_actor_id="human:karnveer",
        )


def test_wrong_key_id_is_rejected_even_with_valid_signature():
    manifest = _manifest()
    private, public_pem = _keys()
    approval = _approval(manifest.manifest_sha256, private)

    with pytest.raises(ValueError, match="key_id is not trusted"):
        verify_signed_approval(
            approval,
            public_key_pem=public_pem,
            expected_manifest_sha256=manifest.manifest_sha256,
            expected_key_id="rotated-key",
            expected_approver_actor_id="human:karnveer",
        )


def test_wrong_actor_is_rejected_even_with_valid_signature():
    manifest = _manifest()
    private, public_pem = _keys()
    approval = _approval(manifest.manifest_sha256, private)

    with pytest.raises(ValueError, match="not the expected human approver"):
        verify_signed_approval(
            approval,
            public_key_pem=public_pem,
            expected_manifest_sha256=manifest.manifest_sha256,
            expected_key_id="command-center-primary",
            expected_approver_actor_id="human:someone-else",
        )


def test_approval_for_another_manifest_is_rejected_before_promotion():
    manifest = _manifest()
    private, public_pem = _keys()
    approval = _approval("f" * 64, private)

    with pytest.raises(ValueError, match="different manifest"):
        verify_signed_approval(
            approval,
            public_key_pem=public_pem,
            expected_manifest_sha256=manifest.manifest_sha256,
            expected_key_id="command-center-primary",
            expected_approver_actor_id="human:karnveer",
        )


def test_signed_rejection_remains_not_eligible():
    manifest = _manifest()
    private, public_pem = _keys()
    approval = _approval(manifest.manifest_sha256, private, decision="rejected")

    decision = evaluate_signed_promotion(
        manifest,
        security=_check("security", manifest.manifest_sha256),
        regression=_check("regression", manifest.manifest_sha256),
        approval=approval,
        public_key_pem=public_pem,
        expected_key_id="command-center-primary",
        expected_approver_actor_id="human:karnveer",
    )

    assert decision.eligible_for_merge is False
    assert "human approval was not granted" in decision.reasons
