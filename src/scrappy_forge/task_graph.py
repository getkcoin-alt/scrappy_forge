from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum

from .util import ForgeError


class NodeState(str, Enum):
    QUEUED = "queued"
    READY = "ready"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    BLOCKED = "blocked"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    VERIFIED = "verified"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class IdempotencyClass(str, Enum):
    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    RECONCILE_BEFORE_RETRY = "reconcile_before_retry"
    NON_IDEMPOTENT = "non_idempotent"


class SideEffectClass(str, Enum):
    NONE = "none"
    WORKSPACE = "workspace"
    HOST = "host"
    EXTERNAL = "external"


TERMINAL_STATES = {
    NodeState.FAILED,
    NodeState.VERIFIED,
    NodeState.CANCELLED,
    NodeState.SUPERSEDED,
}


@dataclass
class TaskNode:
    id: str
    kind: str
    dependencies: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    mutates: bool = False
    state: NodeState = NodeState.QUEUED
    deadline: float | None = None
    max_retries: int = 0
    attempts: int = 0
    idempotency: IdempotencyClass = IdempotencyClass.READ_ONLY
    side_effect: SideEffectClass = SideEffectClass.NONE
    permissions: tuple[str, ...] = ()
    expected_evidence: tuple[str, ...] = ()
    payload: dict = field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    superseded_by: str | None = None

    def __post_init__(self):
        if not self.id or len(self.id) > 128:
            raise ForgeError("Task node ID must contain 1-128 characters")
        if self.max_retries < 0:
            raise ForgeError("Task node retry count cannot be negative")
        if self.mutates and self.idempotency == IdempotencyClass.READ_ONLY:
            raise ForgeError("Mutating task node cannot be classified read-only")
        if self.side_effect == SideEffectClass.EXTERNAL and self.idempotency == IdempotencyClass.READ_ONLY:
            raise ForgeError("External task node requires explicit idempotency classification")

    def to_dict(self) -> dict:
        value = asdict(self)
        value["state"] = self.state.value
        value["idempotency"] = self.idempotency.value
        value["side_effect"] = self.side_effect.value
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "TaskNode":
        data = dict(value)
        data["dependencies"] = tuple(data.get("dependencies", ()))
        data["resources"] = tuple(data.get("resources", ()))
        data["permissions"] = tuple(data.get("permissions", ()))
        data["expected_evidence"] = tuple(data.get("expected_evidence", ()))
        data["state"] = NodeState(data.get("state", NodeState.QUEUED.value))
        data["idempotency"] = IdempotencyClass(data.get("idempotency", IdempotencyClass.READ_ONLY.value))
        data["side_effect"] = SideEffectClass(data.get("side_effect", SideEffectClass.NONE.value))
        return cls(**data)


class TaskGraph:
    def __init__(self, mission_id: str, nodes=()):
        self.mission_id = mission_id
        self.nodes: dict[str, TaskNode] = {}
        for node in nodes:
            self.add(node)

    def add(self, node: TaskNode) -> None:
        if node.id in self.nodes:
            raise ForgeError(f"Duplicate task node: {node.id}")
        missing = [dep for dep in node.dependencies if dep not in self.nodes]
        if missing:
            raise ForgeError("Dependencies must be added before dependent nodes: " + ", ".join(missing))
        self.nodes[node.id] = node
        self._assert_acyclic()
        self.refresh_ready()

    def _assert_acyclic(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str):
            if node_id in visiting:
                raise ForgeError("Task graph contains a cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dep in self.nodes[node_id].dependencies:
                visit(dep)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in self.nodes:
            visit(node_id)

    def refresh_ready(self) -> None:
        for node in self.nodes.values():
            if node.state != NodeState.QUEUED:
                continue
            deps = [self.nodes[dep] for dep in node.dependencies]
            if any(dep.state in {NodeState.FAILED, NodeState.CANCELLED, NodeState.SUPERSEDED} for dep in deps):
                node.state = NodeState.BLOCKED
            elif all(dep.state == NodeState.VERIFIED for dep in deps):
                node.state = NodeState.READY

    def ready(self) -> list[TaskNode]:
        self.refresh_ready()
        return [node for node in self.nodes.values() if node.state == NodeState.READY]

    def transition(self, node_id: str, state: NodeState, *, result=None, error=None) -> TaskNode:
        node = self.nodes[node_id]
        allowed = {
            NodeState.QUEUED: {NodeState.READY, NodeState.BLOCKED, NodeState.CANCELLED, NodeState.SUPERSEDED},
            NodeState.READY: {NodeState.RUNNING, NodeState.CANCELLED, NodeState.SUPERSEDED, NodeState.BLOCKED},
            NodeState.RUNNING: {
                NodeState.AWAITING_INPUT,
                NodeState.BLOCKED,
                NodeState.UNCERTAIN,
                NodeState.FAILED,
                NodeState.VERIFIED,
                NodeState.CANCELLED,
            },
            NodeState.AWAITING_INPUT: {NodeState.READY, NodeState.CANCELLED, NodeState.SUPERSEDED},
            NodeState.BLOCKED: {NodeState.READY, NodeState.CANCELLED, NodeState.SUPERSEDED},
            NodeState.UNCERTAIN: {NodeState.READY, NodeState.FAILED, NodeState.VERIFIED, NodeState.CANCELLED},
            NodeState.FAILED: {NodeState.READY, NodeState.CANCELLED, NodeState.SUPERSEDED},
            NodeState.VERIFIED: {NodeState.SUPERSEDED},
            NodeState.CANCELLED: set(),
            NodeState.SUPERSEDED: set(),
        }
        if state != node.state and state not in allowed[node.state]:
            raise ForgeError(f"Invalid task transition {node.state.value}->{state.value}")
        node.state = state
        node.result = result
        node.error = error
        self.refresh_ready()
        return node

    def cancel(self, node_id: str) -> None:
        node = self.nodes[node_id]
        if node.state not in TERMINAL_STATES:
            self.transition(node_id, NodeState.CANCELLED)
        for candidate in self.nodes.values():
            if node_id in candidate.dependencies and candidate.state in {NodeState.QUEUED, NodeState.READY, NodeState.BLOCKED}:
                candidate.state = NodeState.BLOCKED

    def supersede(self, node_id: str, replacement: TaskNode) -> None:
        original = self.nodes[node_id]
        if original.state == NodeState.RUNNING:
            raise ForgeError("Running task must be cancelled/reconciled before superseding")
        self.transition(node_id, NodeState.SUPERSEDED)
        original.superseded_by = replacement.id
        self.add(replacement)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "nodes": [self.nodes[node_id].to_dict() for node_id in self.nodes],
        }

    @classmethod
    def from_dict(cls, value: dict) -> "TaskGraph":
        graph = cls(str(value["mission_id"]))
        for raw in value.get("nodes", []):
            graph.add(TaskNode.from_dict(raw))
        return graph
