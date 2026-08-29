"""Immutable candidate artifacts and a non-executing promotion gate.

This module turns a measured ``ExperimentRun`` into a content-addressed candidate
manifest. Promotion is eligibility only: it never merges, deploys, mutates a
repository, or grants runtime authority.

Every attestation is bound to the exact manifest SHA-256. Human approval is an
explicit input produced outside Forge; Forge cannot manufacture it internally.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

from .experiment_runner import ExperimentRun

AttestationStatus = Literal["passed", "failed"]
ApprovalDecision = Literal["approved", "rejected"]


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


def _commit_sha(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 40 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError(f"{field} must be a full 40-character git commit SHA")
    return normalized


@dataclass(frozen=True, slots=True)
class CandidateManifest:
    manifest_schema: str
    correlation_id: str
    suite_id: str
    baseline_source_commit: str
    candidate_source_commit: str
    baseline_controller_id: str
    candidate_controller_id: str
    decision_status: str
    goal_score_delta: float
    baseline_evidence_sha256: str
    candidate_evidence_sha256: str
    decision_sha256: str
    simulated: bool
    real_world_authority: bool
    execution_authority: bool
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class CheckAttestation:
    check_name: str
    manifest_sha256: str
    status: AttestationStatus
    evidence_ref: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class HumanApproval:
    manifest_sha256: str
    approver_actor_id: str
    actor_kind: str
    decision: ApprovalDecision
    approved_at: str


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    eligible_for_merge: bool
    manifest_sha256: str
    reasons: tuple[str, ...]
    execution_authority: bool = False
    merge_authority: bool = False
    deploy_authority: bool = False


def _manifest_body(manifest: CandidateManifest) -> dict[str, Any]:
    value = asdict(manifest)
    value.pop("manifest_sha256")
    return value


def verify_candidate_manifest(manifest: CandidateManifest) -> None:
    if manifest.manifest_schema != "scrappy-candidate.v0.2":
        raise ValueError("unsupported candidate manifest schema")
    if manifest.decision_status != "improvement":
        raise ValueError("candidate manifest must be backed by an improvement decision")
    if manifest.simulated is not True or manifest.real_world_authority is not False:
        raise ValueError("candidate manifest must remain simulation-scoped")
    if manifest.execution_authority is not False:
        raise ValueError("candidate manifest must not carry execution authority")
    expected = _sha256(_manifest_body(manifest))
    if not hmac.compare_digest(expected, manifest.manifest_sha256):
        raise ValueError("candidate manifest hash verification failed")


def build_candidate_manifest(
    run: ExperimentRun,
    *,
    baseline_source_commit: str,
    candidate_source_commit: str,
) -> CandidateManifest:
    """Create a deterministic manifest only for measured improvements."""

    if run.decision.status != "improvement":
        raise ValueError("only measured improvements can become candidate artifacts")
    if run.simulated is not True or run.real_world_authority is not False:
        raise ValueError("experiment run must remain simulation-scoped")
    if run.execution_authority is not False or run.decision.execution_authority is not False:
        raise ValueError("experiment run must not carry execution authority")

    baseline_commit = _commit_sha(baseline_source_commit, "baseline_source_commit")
    candidate_commit = _commit_sha(candidate_source_commit, "candidate_source_commit")
    baseline_hash = _sha256(list(run.baseline_reports))
    candidate_hash = _sha256(list(run.candidate_reports))
    decision_hash = _sha256(asdict(run.decision))

    body = {
        "manifest_schema": "scrappy-candidate.v0.2",
        "correlation_id": run.correlation_id,
        "suite_id": run.suite_id,
        "baseline_source_commit": baseline_commit,
        "candidate_source_commit": candidate_commit,
        "baseline_controller_id": run.baseline_controller_id,
        "candidate_controller_id": run.candidate_controller_id,
        "decision_status": run.decision.status,
        "goal_score_delta": run.decision.goal_score_delta,
        "baseline_evidence_sha256": baseline_hash,
        "candidate_evidence_sha256": candidate_hash,
        "decision_sha256": decision_hash,
        "simulated": True,
        "real_world_authority": False,
        "execution_authority": False,
    }
    manifest = CandidateManifest(**body, manifest_sha256=_sha256(body))
    verify_candidate_manifest(manifest)
    return manifest


def _valid_timestamp(value: str, field: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")


def evaluate_promotion(
    manifest: CandidateManifest,
    *,
    security: CheckAttestation,
    regression: CheckAttestation,
    approval: HumanApproval,
) -> PromotionDecision:
    """Return merge eligibility without performing or authorizing a merge."""

    verify_candidate_manifest(manifest)
    reasons: list[str] = []

    for expected_name, attestation in (("security", security), ("regression", regression)):
        if attestation.check_name != expected_name:
            raise ValueError(f"expected {expected_name} attestation")
        if not hmac.compare_digest(attestation.manifest_sha256, manifest.manifest_sha256):
            raise ValueError(f"{expected_name} attestation is bound to a different manifest")
        _valid_timestamp(attestation.observed_at, f"{expected_name}.observed_at")
        if not attestation.evidence_ref.strip():
            raise ValueError(f"{expected_name} attestation requires evidence_ref")
        if attestation.status != "passed":
            reasons.append(f"{expected_name} gate did not pass")

    if not hmac.compare_digest(approval.manifest_sha256, manifest.manifest_sha256):
        raise ValueError("human approval is bound to a different manifest")
    if approval.actor_kind != "human":
        raise ValueError("promotion approval must come from a human actor")
    if not approval.approver_actor_id.strip():
        raise ValueError("promotion approval requires approver_actor_id")
    _valid_timestamp(approval.approved_at, "approval.approved_at")
    if approval.decision != "approved":
        reasons.append("human approval was not granted")

    eligible = not reasons
    if eligible:
        reasons.append("all evidence-bound promotion gates passed")

    return PromotionDecision(
        eligible_for_merge=eligible,
        manifest_sha256=manifest.manifest_sha256,
        reasons=tuple(reasons),
        execution_authority=False,
        merge_authority=False,
        deploy_authority=False,
    )


__all__ = [
    "CandidateManifest",
    "CheckAttestation",
    "HumanApproval",
    "PromotionDecision",
    "build_candidate_manifest",
    "evaluate_promotion",
    "verify_candidate_manifest",
]
