from __future__ import annotations

import base64
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scrappy_forge.candidate_artifact import CheckAttestation, PromotionDecision
from scrappy_forge.signed_approval import (
    APPROVAL_SCHEMA,
    SIGNATURE_ALGORITHM,
    SignedPromotionApproval,
    approval_signing_bytes,
)
from scrappy_forge.signed_promotion_record import (
    build_signed_promotion_record,
    verify_signed_promotion_record,
)

MANIFEST = "a" * 64


def _check(name: str) -> CheckAttestation:
    return CheckAttestation(
        check_name=name,
        manifest_sha256=MANIFEST,
        status="passed",
        evidence_ref=f"ci://{name}/123",
        observed_at="2026-08-29T02:40:00+00:00",
    )


def _approval() -> SignedPromotionApproval:
    private = Ed25519PrivateKey.generate()
    unsigned = SignedPromotionApproval(
        approval_schema=APPROVAL_SCHEMA,
        manifest_sha256=MANIFEST,
        approver_actor_id="human:karnveer",
        actor_kind="human",
        decision="approved",
        approved_at="2026-08-29T02:41:00+00:00",
        nonce="11111111-1111-4111-8111-111111111111",
        key_id="command-center-primary",
        signature_algorithm=SIGNATURE_ALGORITHM,
        signature_base64="",
    )
    signature = private.sign(approval_signing_bytes(unsigned))
    return replace(unsigned, signature_base64=base64.b64encode(signature).decode("ascii"))


def _decision() -> PromotionDecision:
    return PromotionDecision(
        eligible_for_merge=True,
        manifest_sha256=MANIFEST,
        reasons=("all evidence-bound promotion gates passed",),
        execution_authority=False,
        merge_authority=False,
        deploy_authority=False,
    )


def test_signed_record_preserves_signature_and_is_content_addressed():
    approval = _approval()
    record = build_signed_promotion_record(
        _decision(),
        security=_check("security"),
        regression=_check("regression"),
        approval=approval,
    )

    verify_signed_promotion_record(record)
    assert record.approval_signature_base64 == approval.signature_base64
    assert record.approval_key_id == "command-center-primary"
    assert record.approver_actor_id == "human:karnveer"
    assert len(record.approval_payload_sha256) == 64
    assert len(record.record_sha256) == 64
    assert record.merge_authority is False
    assert record.deploy_authority is False


def test_tampered_signed_record_fails_hash_verification():
    record = build_signed_promotion_record(
        _decision(),
        security=_check("security"),
        regression=_check("regression"),
        approval=_approval(),
    )
    tampered = replace(record, eligible_for_merge=False)

    with pytest.raises(ValueError, match="hash verification failed"):
        verify_signed_promotion_record(tampered)


def test_mixed_manifest_inputs_fail_closed():
    wrong = replace(_check("security"), manifest_sha256="b" * 64)

    with pytest.raises(ValueError, match="same manifest"):
        build_signed_promotion_record(
            _decision(),
            security=wrong,
            regression=_check("regression"),
            approval=_approval(),
        )
