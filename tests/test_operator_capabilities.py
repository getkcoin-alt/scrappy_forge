from __future__ import annotations

import pytest

from scrappy_forge.capabilities import Capability, CapabilityBroker
from scrappy_forge.evaluator import EvaluatorReview, aggregate_reviews
from scrappy_forge.policy import permission_class


def test_permission_classes_match_operator_model():
    assert permission_class(risk="read", origin="builtin") == "green"
    assert permission_class(risk="edit", origin="builtin") == "amber"
    assert permission_class(risk="memory", origin="builtin") == "amber"
    assert permission_class(risk="execute", origin="builtin") == "red"
    assert permission_class(risk="read", origin="mcp") == "red"
    assert permission_class(risk="read", origin="builtin", protected=True) == "red"


def test_capability_broker_uses_opaque_handles_only():
    broker = CapabilityBroker()
    broker.register(
        Capability(
            capability_id="github-primary",
            kind="git",
            provider="github",
            handle="capability://git/github-primary",
            status="available",
            permission_class="amber",
            description="GitHub repository operations",
        )
    )

    matches = broker.find("git")
    assert len(matches) == 1
    assert matches[0].handle == "capability://git/github-primary"
    assert "token" not in str(broker.list()).lower()


def test_capability_request_is_pending_and_contains_no_secret_value():
    broker = CapabilityBroker()
    request = broker.request(
        kind="proxy",
        reason="upstream returned 429",
        required_scope="supplier-verification",
        suggested_provider="webshare",
    )

    value = request.to_dict()
    assert value["status"] == "pending"
    assert value["secret_required"] is True
    assert "password" not in value
    assert "credential" not in value


def _review(manifest: str, verdict: str, confidence: float, evaluator: str) -> EvaluatorReview:
    return EvaluatorReview(
        evaluator_id=evaluator,
        evaluator_class="independent-model",
        candidate_manifest_sha256=manifest,
        verdict=verdict,  # type: ignore[arg-type]
        confidence=confidence,
        rationale=("independent evaluation",),
        evidence_refs=(f"benchmark://{evaluator}",),
    )


def test_superior_evaluator_requires_multiple_confident_approvals():
    manifest = "a" * 64
    result = aggregate_reviews(
        manifest,
        [_review(manifest, "approve", 0.9, "judge-a"), _review(manifest, "approve", 0.8, "judge-b")],
    )

    assert result["status"] == "approve"
    assert result["merge_authority"] is False
    assert result["deploy_authority"] is False


def test_confident_decline_vetoes_candidate():
    manifest = "b" * 64
    result = aggregate_reviews(
        manifest,
        [_review(manifest, "approve", 0.95, "judge-a"), _review(manifest, "decline", 0.9, "judge-b")],
    )

    assert result["status"] == "decline"


def test_mismatched_candidate_evidence_fails_closed():
    with pytest.raises(ValueError, match="same candidate manifest"):
        aggregate_reviews(
            "c" * 64,
            [_review("d" * 64, "approve", 0.9, "judge-a")],
            min_approvals=1,
        )
