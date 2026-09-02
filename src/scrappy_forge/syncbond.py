"""SYNCBOND v5 adapter for Scrappy Forge.

Forge emits experiment evidence and consumes reproducible failure/objective
requests.  This module validates the common envelope without adding another
runtime dependency; Forge already ships jsonschema.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator

SYNCBOND_VERSION = "5.0.0"

EVENT_TYPES = {
    "objective.requested",
    "objective.completed",
    "world.observed",
    "world.entity.changed",
    "action.proposed",
    "action.result",
    "approval.requested",
    "approval.resolved",
    "experience.recorded",
    "experiment.requested",
    "experiment.result",
    "node.status",
}

RESOLUTION_STATES = {
    "known",
    "unknown",
    "pending",
    "conflicted",
    "unauthorized",
    "unavailable",
}

ACTOR_KINDS = {"human", "service", "node", "agent"}

SYNCBOND_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "protocol",
        "schema_version",
        "event_id",
        "correlation_id",
        "actor_id",
        "actor_kind",
        "event_type",
        "source",
        "created_at",
        "resolution",
        "provenance",
        "payload",
    ],
    "properties": {
        "protocol": {"const": "SYNCBOND"},
        "schema_version": {"const": SYNCBOND_VERSION},
        "event_id": {"type": "string", "format": "uuid"},
        "correlation_id": {"type": "string", "format": "uuid"},
        "actor_id": {"type": "string", "minLength": 1},
        "actor_kind": {"enum": sorted(ACTOR_KINDS)},
        "event_type": {"enum": sorted(EVENT_TYPES)},
        "source": {"type": "string", "minLength": 1},
        "created_at": {"type": "string", "format": "date-time"},
        "resolution": {"enum": sorted(RESOLUTION_STATES)},
        "confidence": {"type": ["number", "null"], "minimum": 0.0, "maximum": 1.0},
        "provenance": {"type": "array", "items": {"type": "object"}},
        "payload": {"type": "object"},
    },
}

_VALIDATOR = Draft202012Validator(SYNCBOND_SCHEMA)


def validate_envelope(value: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError when an envelope violates v5."""

    _VALIDATOR.validate(value)


def make_envelope(
    *,
    actor_id: str,
    actor_kind: str,
    event_type: str,
    source: str,
    payload: dict[str, Any],
    correlation_id: UUID | None = None,
    resolution: str = "known",
    confidence: float | None = None,
    provenance: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build and validate one transport-neutral SYNCBOND event."""

    value: dict[str, Any] = {
        "protocol": "SYNCBOND",
        "schema_version": SYNCBOND_VERSION,
        "event_id": str(uuid4()),
        "correlation_id": str(correlation_id or uuid4()),
        "actor_id": actor_id,
        "actor_kind": actor_kind,
        "event_type": event_type,
        "source": source,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resolution": resolution,
        "confidence": confidence,
        "provenance": provenance or [],
        "payload": payload,
    }
    validate_envelope(value)
    return value
