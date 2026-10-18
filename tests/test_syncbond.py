import pytest
from jsonschema import ValidationError

from scrappy_forge.syncbond import SYNCBOND_VERSION, make_envelope, validate_envelope


def test_forge_emits_experiment_result_envelope() -> None:
    event = make_envelope(
        actor_id="service:scrappy-forge",
        actor_kind="service",
        event_type="experiment.result",
        source="scrappy-forge",
        payload={
            "experiment_id": "exp-001",
            "status": "passed",
            "evidence": ["pytest: 42 passed"],
        },
    )

    assert event["schema_version"] == SYNCBOND_VERSION
    assert event["event_type"] == "experiment.result"
    assert event["resolution"] == "known"


def test_forge_rejects_unknown_event_type() -> None:
    event = make_envelope(
        actor_id="service:scrappy-forge",
        actor_kind="service",
        event_type="experiment.result",
        source="scrappy-forge",
        payload={},
    )
    event["event_type"] = "god_mode.enabled"

    with pytest.raises(ValidationError):
        validate_envelope(event)
