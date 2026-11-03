"""Mission lifecycle states and the only transitions the orchestrator may perform."""

from __future__ import annotations

from enum import Enum

from scrappy_forge.util import ForgeError


class MissionState(str, Enum):
    CREATED = "CREATED"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    INTEGRATING = "INTEGRATING"
    DEPLOYING = "DEPLOYING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = frozenset({MissionState.COMPLETED, MissionState.FAILED, MissionState.CANCELLED})

# Any non-terminal state may additionally move to BLOCKED, FAILED or CANCELLED.
_FORWARD: dict[MissionState, frozenset[MissionState]] = {
    MissionState.CREATED: frozenset({MissionState.ANALYZING}),
    MissionState.ANALYZING: frozenset({MissionState.PLANNING}),
    MissionState.PLANNING: frozenset({MissionState.READY}),
    MissionState.READY: frozenset({MissionState.RUNNING}),
    MissionState.RUNNING: frozenset({MissionState.VERIFYING}),
    # VERIFYING -> RUNNING is the bounded repair loop.
    MissionState.VERIFYING: frozenset({MissionState.RUNNING, MissionState.INTEGRATING}),
    MissionState.INTEGRATING: frozenset({MissionState.DEPLOYING, MissionState.COMPLETED}),
    MissionState.DEPLOYING: frozenset({MissionState.COMPLETED}),
    # An operator unblocks a mission by resuming it at the state it was blocked in.
    MissionState.BLOCKED: frozenset(
        {
            MissionState.ANALYZING,
            MissionState.PLANNING,
            MissionState.READY,
            MissionState.RUNNING,
            MissionState.VERIFYING,
            MissionState.INTEGRATING,
            MissionState.DEPLOYING,
        }
    ),
}


def allowed_next(state: MissionState) -> frozenset[MissionState]:
    if state in TERMINAL:
        return frozenset()
    extra = {MissionState.FAILED, MissionState.CANCELLED}
    if state != MissionState.BLOCKED:
        extra.add(MissionState.BLOCKED)
    return _FORWARD[state] | extra


def check_transition(current: MissionState, target: MissionState) -> None:
    if target not in allowed_next(current):
        raise ForgeError(f"Illegal mission transition {current.value} -> {target.value}")
