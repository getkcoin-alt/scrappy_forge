from __future__ import annotations

from types import SimpleNamespace

import pytest

from scrappy_forge.capabilities import (
    Capability,
    CapabilityBroker,
    append_pending_request,
    broker_from_settings,
    load_pending_requests,
    unresolved_pending_requests,
)
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
    assert broker.find("github") == matches
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


def test_pending_capability_request_persists_without_secret(tmp_path):
    broker = CapabilityBroker()
    request = broker.request(
        kind="railway",
        reason="deployment verification requires service state",
        required_scope="project:demo read+deploy",
        suggested_provider="railway",
    )
    append_pending_request(tmp_path, request)

    loaded = load_pending_requests(tmp_path)
    assert loaded == [request.to_dict()]
    serialized = str(loaded).lower()
    assert "api_key" not in serialized
    assert "password" not in serialized


def test_configured_mcp_satisfies_provider_request(tmp_path):
    empty = SimpleNamespace(provider="openrouter", mcp={}, home=tmp_path)
    request = CapabilityBroker().request(
        kind="railway",
        reason="deployment verification requires service state",
        required_scope="project:demo",
        suggested_provider="railway",
    )
    append_pending_request(tmp_path, request)
    assert len(unresolved_pending_requests(empty)) == 1

    configured = SimpleNamespace(
        provider="openrouter",
        mcp={"railway": {"transport": "stdio"}},
        home=tmp_path,
    )
    broker = broker_from_settings(configured)
    assert [item.handle for item in broker.find("railway")] == ["capability://mcp/railway"]
    assert unresolved_pending_requests(configured) == []


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
