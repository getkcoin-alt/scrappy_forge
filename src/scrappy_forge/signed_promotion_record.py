"""Content-addressed v0.3 promotion records retaining signed human evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass
from typing import Any

from .candidate_artifact import CheckAttestation, PromotionDecision
from .signed_approval import SignedPromotionApproval, approval_signing_bytes


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class SignedPromotionRecord:
    record_schema: str
    manifest_sha256: str
    eligible_for_merge: bool
    security_evidence_ref: str
    regression_evidence_ref: str
    approval_payload_sha256: str
    approval_signature_base64: str
    approval_key_id: str
    approver_actor_id: str
    approval_decision: str
    reasons: tuple[str, ...]
    execution_authority: bool
    merge_authority: bool
    deploy_authority: bool
    record_sha256: str


def _record_body(record: SignedPromotionRecord) -> dict[str, Any]:
    value = asdict(record)
    value.pop("record_sha256")
    return value


def verify_signed_promotion_record(record: SignedPromotionRecord) -> None:
    if record.record_schema != "scrappy-promotion-record.v0.3":
        raise ValueError("unsupported signed promotion record schema")
    if record.execution_authority or record.merge_authority or record.deploy_authority:
        raise ValueError("signed promotion record must not carry runtime authority")
    expected = _sha256(_record_body(record))
    if not hmac.compare_digest(expected, record.record_sha256):
        raise ValueError("signed promotion record hash verification failed")


def build_signed_promotion_record(
    decision: PromotionDecision,
    *,
    security: CheckAttestation,
    regression: CheckAttestation,
    approval: SignedPromotionApproval,
) -> SignedPromotionRecord:
    for item in (security, regression, approval):
        if not hmac.compare_digest(item.manifest_sha256, decision.manifest_sha256):
            raise ValueError("signed promotion record inputs must reference the same manifest")
    if decision.execution_authority or decision.merge_authority or decision.deploy_authority:
        raise ValueError("promotion decision unexpectedly carries authority")

    body = {
        "record_schema": "scrappy-promotion-record.v0.3",
        "manifest_sha256": decision.manifest_sha256,
        "eligible_for_merge": decision.eligible_for_merge,
        "security_evidence_ref": security.evidence_ref,
        "regression_evidence_ref": regression.evidence_ref,
        "approval_payload_sha256": hashlib.sha256(approval_signing_bytes(approval)).hexdigest(),
        "approval_signature_base64": approval.signature_base64,
        "approval_key_id": approval.key_id,
        "approver_actor_id": approval.approver_actor_id,
        "approval_decision": approval.decision,
        "reasons": tuple(decision.reasons),
        "execution_authority": False,
        "merge_authority": False,
        "deploy_authority": False,
    }
    record = SignedPromotionRecord(**body, record_sha256=_sha256(body))
    verify_signed_promotion_record(record)
    return record


__all__ = [
    "SignedPromotionRecord",
    "build_signed_promotion_record",
    "verify_signed_promotion_record",
]
