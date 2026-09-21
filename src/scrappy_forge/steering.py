from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Callable

from .util import ForgeError


class SteeringKind(str, Enum):
    CONSTRAINT_CHANGE = "constraint_change"
    PRIORITY_CHANGE = "priority_change"
    TASK_CORRECTION = "task_correction"
    NEW_INFORMATION = "new_information"
    CANCEL = "cancel"
    PAUSE = "pause"
    RESUME = "resume"


@dataclass(frozen=True)
class SteeringEvent:
    id: str
    mission_id: str
    kind: SteeringKind
    text: str
    at: float
    target_ids: tuple[str, ...] = ()
    metadata: dict | None = None

    @classmethod
    def create(
        cls,
        mission_id: str,
        kind: SteeringKind | str,
        text: str,
        *,
        target_ids=(),
        metadata=None,
        now: Callable[[], float] = time.time,
    ) -> "SteeringEvent":
        kind = SteeringKind(kind)
        clean = text.strip()
        if not mission_id or not clean:
            raise ForgeError("Steering event requires mission ID and non-empty instruction")
        targets = tuple(dict.fromkeys(str(item) for item in target_ids if str(item)))
        return cls(
            id=uuid.uuid4().hex,
            mission_id=mission_id,
            kind=kind,
            text=clean,
            at=now(),
            target_ids=targets,
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict:
        value = asdict(self)
        value["kind"] = self.kind.value
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "SteeringEvent":
        data = dict(value)
        data["kind"] = SteeringKind(data["kind"])
        data["target_ids"] = tuple(data.get("target_ids", ()))
        return cls(**data)


class SteeringBus:
    """Typed operator updates for a running mission.

    The bus is intentionally transport-agnostic. It validates and journals instructions;
    the controller decides their effect on task state. A model never consumes an operator
    message as implicit authorization.
    """

    def __init__(self, mission_id: str, *, journal: Callable[[str, dict], object] | None = None):
        self.mission_id = mission_id
        self._journal = journal or (lambda *_: None)
        self._events: list[SteeringEvent] = []

    def publish(self, event: SteeringEvent) -> SteeringEvent:
        if event.mission_id != self.mission_id:
            raise ForgeError("Steering event belongs to a different mission")
        if any(existing.id == event.id for existing in self._events):
            return event
        self._events.append(event)
        self._journal("steering", event.to_dict())
        return event

    def inject(self, kind: SteeringKind | str, text: str, *, target_ids=(), metadata=None) -> SteeringEvent:
        return self.publish(
            SteeringEvent.create(
                self.mission_id,
                kind,
                text,
                target_ids=target_ids,
                metadata=metadata,
            )
        )

    def events(self, *, after: int = 0) -> list[SteeringEvent]:
        return list(self._events[max(0, after) :])

    def restore(self, values: list[dict]) -> None:
        for value in values:
            self.publish(SteeringEvent.from_dict(value))
