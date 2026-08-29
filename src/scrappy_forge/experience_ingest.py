"""Fail-closed Vault Experience ingestion for Scrappy Forge.

This is a handoff/triage layer, not an execution path. It validates a portable
SYNCBOND evidence bundle and, for non-success outcomes, emits a metadata-only
ExperimentCandidate. No Forge tool, host process, repository mutation or
Omni-City action is invoked from evidence supplied by the bundle.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from scrappy_forge.syncbond import SYNCBOND_VERSION, validate_envelope

BUNDLE_FORMAT = "syncbond.experience-evidence.v1"
Outcome = Literal["failed", "partial", "blocked"]
_CANDIDATE_OUTCOMES = {"failed", "partial", "blocked"}


@dataclass(frozen=True, slots=True)
class ExperimentCandidate:
    correlation_id: UUID
    remote_objective_id: UUID
    outcome: Outcome
    summary: str
    evidence: tuple[str, ...]
    source_bundle_sha256: str
    source_event_id: UUID


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _verify_hash(bundle: dict[str, Any]) -> str:
    claimed = bundle.get("bundle_sha256")
    if not isinstance(claimed, str) or len(claimed) != 64:
        raise ValueError("Experience bundle is missing a valid bundle_sha256")
    body = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    actual = hashlib.sha256(_canonical(body)).hexdigest()
    if not hmac.compare_digest(actual, claimed):
        raise ValueError("Experience bundle hash verification failed")
    return claimed


def _uuid(value: Any, field: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Experience bundle {field} is not a UUID") from exc


def ingest_experience_bundle(bundle: dict[str, Any]) -> ExperimentCandidate | None:
    """Validate one Vault evidence bundle and triage it for experimentation.

    A successful task is durable experience but not, by itself, a failure-driven
    experiment candidate. Failed, blocked and partial outcomes become candidates
    whose only authority is to be considered by a later research/evaluation
    stage.
    """

    if not isinstance(bundle, dict):
        raise ValueError("Experience bundle must be a JSON object")
    if bundle.get("bundle_format") != BUNDLE_FORMAT:
        raise ValueError("Unsupported Experience evidence bundle format")
    if bundle.get("protocol") != "SYNCBOND":
        raise ValueError("Experience bundle protocol must be SYNCBOND")
    if bundle.get("schema_version") != SYNCBOND_VERSION:
        raise ValueError("Unsupported SYNCBOND schema version")

    digest = _verify_hash(bundle)
    correlation_id = _uuid(bundle.get("correlation_id"), "correlation_id")
    remote_objective_id = _uuid(bundle.get("remote_objective_id"), "remote_objective_id")

    envelope = bundle.get("envelope")
    if not isinstance(envelope, dict):
        raise ValueError("Experience bundle envelope must be an object")
    validate_envelope(envelope)
    if envelope.get("event_type") != "experience.recorded":
        raise ValueError("Experience bundle envelope must be experience.recorded")
    if _uuid(envelope.get("correlation_id"), "envelope.correlation_id") != correlation_id:
        raise ValueError("Experience bundle correlation continuity check failed")
    source_event_id = _uuid(envelope.get("event_id"), "envelope.event_id")

    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Experience envelope payload must be an object")
    if _uuid(payload.get("objective_id"), "envelope.payload.objective_id") != remote_objective_id:
        raise ValueError("Experience bundle objective continuity check failed")

    outcome = payload.get("outcome")
    if outcome == "succeeded":
        return None
    if outcome not in _CANDIDATE_OUTCOMES:
        raise ValueError("Experience bundle contains an unsupported outcome")

    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Experience bundle summary must be non-empty")
    evidence_raw = payload.get("evidence", [])
    if not isinstance(evidence_raw, list) or not all(isinstance(item, str) for item in evidence_raw):
        raise ValueError("Experience bundle evidence must be a list of strings")

    return ExperimentCandidate(
        correlation_id=correlation_id,
        remote_objective_id=remote_objective_id,
        outcome=outcome,
        summary=summary.strip(),
        evidence=tuple(evidence_raw),
        source_bundle_sha256=digest,
        source_event_id=source_event_id,
    )


__all__ = ["BUNDLE_FORMAT", "ExperimentCandidate", "ingest_experience_bundle"]
