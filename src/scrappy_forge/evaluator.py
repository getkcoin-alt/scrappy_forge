"""Independent evaluator contracts for Scrappy Forge self-improvement.

An evaluator may recommend approve/decline/needs-more-evidence, but this module
never merges, deploys, changes permissions or grants runtime authority.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

EvaluatorVerdict = Literal["approve", "decline", "needs_more_evidence"]


@dataclass(frozen=True, slots=True)
class EvaluatorReview:
    evaluator_id: str
    evaluator_class: str
    candidate_manifest_sha256: str
    verdict: EvaluatorVerdict
    confidence: float
    rationale: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    execution_authority: bool = False
    merge_authority: bool = False
    deploy_authority: bool = False

    def __post_init__(self) -> None:
        if not self.evaluator_id.strip() or not self.evaluator_class.strip():
            raise ValueError("evaluator identity is required")
        digest = self.candidate_manifest_sha256.strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("candidate_manifest_sha256 must be a SHA-256 hex digest")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.execution_authority or self.merge_authority or self.deploy_authority:
            raise ValueError("evaluator reviews must not carry runtime authority")

    def to_dict(self) -> dict:
        return asdict(self)


def aggregate_reviews(
    manifest_sha256: str,
    reviews: list[EvaluatorReview],
    *,
    min_approvals: int = 2,
    min_confidence: float = 0.7,
) -> dict:
    digest = manifest_sha256.strip().lower()
    if min_approvals < 1:
        raise ValueError("min_approvals must be at least 1")
    if not 0.0 <= min_confidence <= 1.0:
        raise ValueError("min_confidence must be between 0 and 1")
    if not reviews:
        return {"status": "needs_more_evidence", "approvals": 0, "declines": 0, "manifest_sha256": digest}

    for review in reviews:
        if review.candidate_manifest_sha256 != digest:
            raise ValueError("all evaluator reviews must bind to the same candidate manifest")

    declines = [r for r in reviews if r.verdict == "decline" and r.confidence >= min_confidence]
    approvals = [r for r in reviews if r.verdict == "approve" and r.confidence >= min_confidence]
    if declines:
        status = "decline"
    elif len(approvals) >= min_approvals:
        status = "approve"
    else:
        status = "needs_more_evidence"
    return {
        "status": status,
        "approvals": len(approvals),
        "declines": len(declines),
        "manifest_sha256": digest,
        "execution_authority": False,
        "merge_authority": False,
        "deploy_authority": False,
    }


__all__ = ["EvaluatorReview", "EvaluatorVerdict", "aggregate_reviews"]
