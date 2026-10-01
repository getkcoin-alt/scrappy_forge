"""Verify Command Center Ed25519 promotion approvals.

The private approval key never belongs in Forge. Forge receives only a public key
and can therefore verify governance evidence without being able to mint it.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from .candidate_artifact import (
    CandidateManifest,
    CheckAttestation,
    HumanApproval,
    PromotionDecision,
    evaluate_promotion,
)

APPROVAL_SCHEMA = "scrappy-promotion-approval.v0.3"
SIGNATURE_ALGORITHM = "ed25519"
ApprovalDecision = Literal["approved", "rejected"]


@dataclass(frozen=True, slots=True)
class SignedPromotionApproval:
    approval_schema: str
    manifest_sha256: str
    approver_actor_id: str
    actor_kind: str
    decision: ApprovalDecision
    approved_at: str
    nonce: str
    key_id: str
    signature_algorithm: str
    signature_base64: str


def _single_line(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized or "\n" in normalized or "\r" in normalized:
        raise ValueError(f"{field} must be a non-empty single-line value")
    return normalized


def _sha256_hex(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError("manifest_sha256 must be a 64-character lowercase hex digest")
    return normalized


def _timestamp(value: str) -> str:
    normalized = _single_line(value, "approved_at")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("approved_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("approved_at must include a timezone")
    return normalized


def approval_signing_bytes(approval: SignedPromotionApproval) -> bytes:
    manifest = _sha256_hex(approval.manifest_sha256)
    actor = _single_line(approval.approver_actor_id, "approver_actor_id")
    approved_at = _timestamp(approval.approved_at)
    nonce = _single_line(approval.nonce, "nonce")
    key_id = _single_line(approval.key_id, "key_id")
    if approval.approval_schema != APPROVAL_SCHEMA:
        raise ValueError("unsupported signed approval schema")
    if approval.actor_kind != "human":
        raise ValueError("signed approval actor_kind must be human")
    if approval.decision not in ("approved", "rejected"):
        raise ValueError("signed approval decision is invalid")
    if approval.signature_algorithm != SIGNATURE_ALGORITHM:
        raise ValueError("unsupported approval signature algorithm")

    return "\n".join(
        [
            APPROVAL_SCHEMA,
            f"manifest_sha256={manifest}",
            f"approver_actor_id={actor}",
            "actor_kind=human",
            f"decision={approval.decision}",
            f"approved_at={approved_at}",
            f"nonce={nonce}",
            f"key_id={key_id}",
            f"signature_algorithm={SIGNATURE_ALGORITHM}",
        ]
    ).encode("utf-8")


def signed_approval_from_dict(value: dict[str, Any]) -> SignedPromotionApproval:
    try:
        return SignedPromotionApproval(
            approval_schema=str(value["approval_schema"]),
            manifest_sha256=str(value["manifest_sha256"]),
            approver_actor_id=str(value["approver_actor_id"]),
            actor_kind=str(value["actor_kind"]),
            decision=str(value["decision"]),  # type: ignore[arg-type]
            approved_at=str(value["approved_at"]),
            nonce=str(value["nonce"]),
            key_id=str(value["key_id"]),
            signature_algorithm=str(value["signature_algorithm"]),
            signature_base64=str(value["signature_base64"]),
        )
    except KeyError as exc:
        raise ValueError(f"signed approval missing field: {exc.args[0]}") from exc


def verify_signed_approval(
    approval: SignedPromotionApproval,
    *,
    public_key_pem: bytes | str,
    expected_manifest_sha256: str,
    expected_key_id: str,
    expected_approver_actor_id: str,
) -> None:
    """Verify signature and bind it to the expected candidate and governance identity."""

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - packaging boundary
        raise RuntimeError("signed approval verification requires the 'auth' extra") from exc

    payload = approval_signing_bytes(approval)
    expected_manifest = _sha256_hex(expected_manifest_sha256)
    if approval.manifest_sha256 != expected_manifest:
        raise ValueError("signed approval is bound to a different manifest")
    if approval.key_id != _single_line(expected_key_id, "expected_key_id"):
        raise ValueError("signed approval key_id is not trusted for this promotion")
    if approval.approver_actor_id != _single_line(
        expected_approver_actor_id, "expected_approver_actor_id"
    ):
        raise ValueError("signed approval actor is not the expected human approver")

    pem = public_key_pem.encode("utf-8") if isinstance(public_key_pem, str) else public_key_pem
    try:
        key = serialization.load_pem_public_key(pem)
    except (TypeError, ValueError) as exc:
        raise ValueError("approval public key PEM is invalid") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("approval public key must be Ed25519")

    try:
        signature = base64.b64decode(approval.signature_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("signed approval signature is not valid base64") from exc

    try:
        key.verify(signature, payload)
    except InvalidSignature as exc:
        raise ValueError("signed approval signature verification failed") from exc


def evaluate_signed_promotion(
    manifest: CandidateManifest,
    *,
    security: CheckAttestation,
    regression: CheckAttestation,
    approval: SignedPromotionApproval,
    public_key_pem: bytes | str,
    expected_key_id: str,
    expected_approver_actor_id: str,
) -> PromotionDecision:
    """Verify signed governance evidence, then reuse the inert v0.2 promotion gate."""

    verify_signed_approval(
        approval,
        public_key_pem=public_key_pem,
        expected_manifest_sha256=manifest.manifest_sha256,
        expected_key_id=expected_key_id,
        expected_approver_actor_id=expected_approver_actor_id,
    )
    human = HumanApproval(
        manifest_sha256=approval.manifest_sha256,
        approver_actor_id=approval.approver_actor_id,
        actor_kind="human",
        decision=approval.decision,
        approved_at=approval.approved_at,
    )
    return evaluate_promotion(
        manifest,
        security=security,
        regression=regression,
        approval=human,
    )


__all__ = [
    "APPROVAL_SCHEMA",
    "SIGNATURE_ALGORITHM",
    "SignedPromotionApproval",
    "approval_signing_bytes",
    "evaluate_signed_promotion",
    "signed_approval_from_dict",
    "verify_signed_approval",
]
