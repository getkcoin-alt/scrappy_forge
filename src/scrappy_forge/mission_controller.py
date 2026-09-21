from __future__ import annotations

from dataclasses import dataclass

from .continuity import MissionStore
from .scheduler import Scheduler
from .steering import SteeringBus, SteeringEvent, SteeringKind
from .task_graph import NodeState, TaskGraph, TaskNode
from .util import ForgeError


@dataclass(frozen=True)
class SteeringImpact:
    event_id: str
    affected: tuple[str, ...]
    preserved_verified: tuple[str, ...]


class MissionController:
    """Controller-owned durable mission state.

    Operator updates are journaled first, then deterministically applied to the graph.
    Completed verified work is preserved unless the operator explicitly targets it or a
    changed ancestor invalidates that branch. Models may propose replans, but only this
    controller changes authoritative node state.
    """

    def __init__(self, graph: TaskGraph, store: MissionStore, *, max_concurrency: int = 4):
        if graph.mission_id != store.root.name:
            raise ForgeError("Mission graph/store identity mismatch")
        self.graph = graph
        self.store = store
        self.paused = False
        self.metadata: dict = {}
        self.steering = SteeringBus(graph.mission_id, journal=self.store.append_event)
        self.scheduler = Scheduler(graph, max_concurrency=max_concurrency)

    @classmethod
    def create(cls, graph: TaskGraph, store: MissionStore, *, max_concurrency: int = 4):
        controller = cls(graph, store, max_concurrency=max_concurrency)
        controller._persist()
        controller.store.append_event("mission_created", {"mission_id": graph.mission_id})
        return controller

    @classmethod
    def restore(cls, store: MissionStore, *, max_concurrency: int = 4):
        graph, state = store.restore_graph()
        controller = cls(graph, store, max_concurrency=max_concurrency)
        controller.paused = bool(state.get("paused", False))
        controller.metadata = dict(state.get("metadata") or {})
        # Restore without double-writing historical steering events.
        controller.steering._events = [SteeringEvent.from_dict(raw) for raw in state.get("steering", [])]
        controller.store.append_event("mission_restored", {"mission_id": graph.mission_id})
        controller._recover_running_nodes()
        controller._persist()
        return controller

    def _persist(self) -> None:
        self.store.save_graph(
            self.graph,
            paused=self.paused,
            steering=[event.to_dict() for event in self.steering.events()],
            metadata=self.metadata,
        )

    def _recover_running_nodes(self) -> None:
        for node in self.graph.nodes.values():
            if node.state != NodeState.RUNNING:
                continue
            # Local/read work can be rescheduled from trusted state; external work cannot.
            if node.side_effect.value == "external":
                self.graph.transition(
                    node.id,
                    NodeState.UNCERTAIN,
                    error="controller restarted while external operation was running; reconcile before retry",
                )
            else:
                message = "controller restarted while operation was running"
                self.graph.transition(node.id, NodeState.FAILED, error=message)
                self.graph.transition(node.id, NodeState.READY, error=message)

    def descendants(self, node_ids) -> set[str]:
        affected = set(node_ids)
        changed = True
        while changed:
            changed = False
            for node in self.graph.nodes.values():
                if node.id in affected:
                    continue
                if any(dep in affected for dep in node.dependencies):
                    affected.add(node.id)
                    changed = True
        return affected

    def _invalidate_branch(self, targets: set[str], *, event_id: str) -> tuple[set[str], set[str]]:
        affected = self.descendants(targets)
        preserved: set[str] = set()
        for node_id in affected:
            node = self.graph.nodes[node_id]
            if node.state == NodeState.RUNNING:
                raise ForgeError("Cannot replan a running node; cancel or reconcile it first")
            if node.state == NodeState.UNCERTAIN:
                raise ForgeError("Cannot replan an uncertain node before reconciliation")
            if node.state == NodeState.VERIFIED and node_id not in targets:
                # A verified descendant depends on changed input, so it is superseded.
                node.state = NodeState.SUPERSEDED
                node.superseded_by = f"steering:{event_id}"
            elif node.state == NodeState.VERIFIED:
                node.state = NodeState.SUPERSEDED
                node.superseded_by = f"steering:{event_id}"
            elif node.state not in {NodeState.CANCELLED, NodeState.SUPERSEDED}:
                node.state = NodeState.QUEUED
                node.result = None
                node.error = None
        for node_id, node in self.graph.nodes.items():
            if node_id not in affected and node.state == NodeState.VERIFIED:
                preserved.add(node_id)
        self.graph.refresh_ready()
        return affected, preserved

    def apply_steering(self, event: SteeringEvent) -> SteeringImpact:
        self.steering.publish(event)
        targets = set(event.target_ids)
        unknown = targets - set(self.graph.nodes)
        if unknown:
            raise ForgeError("Unknown steering target(s): " + ", ".join(sorted(unknown)))

        affected: set[str] = set()
        preserved: set[str] = {
            node.id for node in self.graph.nodes.values() if node.state == NodeState.VERIFIED
        }

        if event.kind == SteeringKind.PAUSE:
            self.paused = True
        elif event.kind == SteeringKind.RESUME:
            self.paused = False
        elif event.kind == SteeringKind.CANCEL:
            if targets:
                affected = self.descendants(targets)
                for node_id in targets:
                    self.graph.cancel(node_id)
            else:
                affected = set(self.graph.nodes)
                for node in list(self.graph.nodes.values()):
                    if node.state not in {NodeState.VERIFIED, NodeState.CANCELLED, NodeState.SUPERSEDED}:
                        self.graph.cancel(node.id)
        elif event.kind in {
            SteeringKind.CONSTRAINT_CHANGE,
            SteeringKind.PRIORITY_CHANGE,
            SteeringKind.TASK_CORRECTION,
        }:
            if not targets:
                raise ForgeError(f"{event.kind.value} requires explicit affected task IDs")
            affected, preserved = self._invalidate_branch(targets, event_id=event.id)
        elif event.kind == SteeringKind.NEW_INFORMATION:
            # New facts do not invalidate work by default. Explicit targets mean the operator
            # declares which branch must be reconsidered.
            if targets:
                affected, preserved = self._invalidate_branch(targets, event_id=event.id)

        self.store.append_event(
            "steering_applied",
            {
                "event_id": event.id,
                "kind": event.kind.value,
                "affected": sorted(affected),
                "preserved_verified": sorted(preserved),
                "paused": self.paused,
            },
        )
        self._persist()
        return SteeringImpact(event.id, tuple(sorted(affected)), tuple(sorted(preserved)))

    def inject(self, kind: SteeringKind | str, text: str, *, target_ids=(), metadata=None) -> SteeringImpact:
        event = SteeringEvent.create(
            self.graph.mission_id,
            kind,
            text,
            target_ids=target_ids,
            metadata=metadata,
        )
        return self.apply_steering(event)

    async def run_ready(self, executor) -> list[TaskNode]:
        if self.paused:
            return []
        batch = self.scheduler.next_batch().nodes
        if not batch:
            return []
        self.store.append_event("batch_started", {"nodes": [node.id for node in batch]})
        result = await self.scheduler.run_ready(executor)
        self.store.append_event(
            "batch_finished",
            {"nodes": [node.id for node in result], "states": {node.id: node.state.value for node in result}},
        )
        self._persist()
        return result
