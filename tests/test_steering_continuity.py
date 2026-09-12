import asyncio

import pytest

from scrappy_forge.continuity import MissionStore
from scrappy_forge.mission_controller import MissionController
from scrappy_forge.steering import SteeringEvent, SteeringKind
from scrappy_forge.task_graph import (
    IdempotencyClass,
    NodeState,
    SideEffectClass,
    TaskGraph,
    TaskNode,
)
from scrappy_forge.util import ForgeError


def read_node(node_id, *, deps=()):
    return TaskNode(id=node_id, kind="read", dependencies=deps)


def edit_node(node_id, *, deps=()):
    return TaskNode(
        id=node_id,
        kind="edit",
        dependencies=deps,
        resources=(f"file:{node_id}.py",),
        mutates=True,
        idempotency=IdempotencyClass.IDEMPOTENT,
        side_effect=SideEffectClass.WORKSPACE,
    )


def verified(graph, node_id):
    graph.transition(node_id, NodeState.RUNNING)
    graph.transition(node_id, NodeState.VERIFIED, result={"ok": True})


def test_pause_resume_is_durable(tmp_path):
    graph = TaskGraph("mission-a")
    graph.add(read_node("inspect"))
    store = MissionStore(tmp_path, "mission-a")
    controller = MissionController.create(graph, store)
    controller.inject(SteeringKind.PAUSE, "hold execution")

    restored = MissionController.restore(store)
    assert restored.paused is True
    assert restored.steering.events()[-1].kind == SteeringKind.PAUSE

    restored.inject(SteeringKind.RESUME, "continue")
    assert MissionController.restore(store).paused is False


async def test_pause_prevents_scheduler_execution(tmp_path):
    graph = TaskGraph("mission-a")
    graph.add(read_node("inspect"))
    controller = MissionController.create(graph, MissionStore(tmp_path, "mission-a"))
    controller.inject(SteeringKind.PAUSE, "wait")
    calls = 0

    async def execute(_):
        nonlocal calls
        calls += 1
        return {"ok": True}

    assert await controller.run_ready(execute) == []
    assert calls == 0
    assert graph.nodes["inspect"].state == NodeState.READY


async def test_completed_unaffected_branch_survives_correction(tmp_path):
    graph = TaskGraph("mission-a")
    graph.add(read_node("inspect-a"))
    graph.add(edit_node("edit-a", deps=("inspect-a",)))
    graph.add(read_node("inspect-b"))
    verified(graph, "inspect-a")
    verified(graph, "edit-a")
    verified(graph, "inspect-b")

    controller = MissionController.create(graph, MissionStore(tmp_path, "mission-a"))
    impact = controller.inject(
        SteeringKind.TASK_CORRECTION,
        "the assumptions for branch A changed",
        target_ids=("inspect-a",),
    )

    assert set(impact.affected) == {"inspect-a", "edit-a"}
    assert impact.preserved_verified == ("inspect-b",)
    assert graph.nodes["inspect-a"].state == NodeState.SUPERSEDED
    assert graph.nodes["edit-a"].state == NodeState.SUPERSEDED
    assert graph.nodes["inspect-b"].state == NodeState.VERIFIED


async def test_new_information_without_targets_preserves_graph(tmp_path):
    graph = TaskGraph("mission-a")
    graph.add(read_node("inspect"))
    verified(graph, "inspect")
    controller = MissionController.create(graph, MissionStore(tmp_path, "mission-a"))
    impact = controller.inject(SteeringKind.NEW_INFORMATION, "upstream released a new version")
    assert impact.affected == ()
    assert graph.nodes["inspect"].state == NodeState.VERIFIED


def test_correction_requires_explicit_affected_tasks(tmp_path):
    graph = TaskGraph("mission-a")
    graph.add(read_node("inspect"))
    controller = MissionController.create(graph, MissionStore(tmp_path, "mission-a"))
    with pytest.raises(ForgeError, match="requires explicit affected task IDs"):
        controller.inject(SteeringKind.CONSTRAINT_CHANGE, "must support another runtime")


def test_wrong_mission_steering_is_rejected(tmp_path):
    graph = TaskGraph("mission-a")
    graph.add(read_node("inspect"))
    controller = MissionController.create(graph, MissionStore(tmp_path, "mission-a"))
    event = SteeringEvent.create("mission-b", SteeringKind.PAUSE, "pause")
    with pytest.raises(ForgeError, match="different mission"):
        controller.apply_steering(event)


def test_restore_requeues_interrupted_local_operation(tmp_path):
    graph = TaskGraph("mission-a")
    graph.add(edit_node("edit"))
    graph.transition("edit", NodeState.RUNNING)
    store = MissionStore(tmp_path, "mission-a")
    MissionController.create(graph, store)

    restored = MissionController.restore(store)
    assert restored.graph.nodes["edit"].state == NodeState.READY
    assert "restarted" in restored.graph.nodes["edit"].error


def test_restore_marks_interrupted_external_operation_uncertain(tmp_path):
    graph = TaskGraph("mission-a")
    graph.add(
        TaskNode(
            id="publish",
            kind="external",
            mutates=True,
            resources=("external:release",),
            idempotency=IdempotencyClass.RECONCILE_BEFORE_RETRY,
            side_effect=SideEffectClass.EXTERNAL,
        )
    )
    graph.transition("publish", NodeState.RUNNING)
    store = MissionStore(tmp_path, "mission-a")
    MissionController.create(graph, store)

    restored = MissionController.restore(store)
    assert restored.graph.nodes["publish"].state == NodeState.UNCERTAIN
    assert "reconcile before retry" in restored.graph.nodes["publish"].error


def test_event_journal_is_append_only_and_ordered(tmp_path):
    graph = TaskGraph("mission-a")
    graph.add(read_node("inspect"))
    store = MissionStore(tmp_path, "mission-a")
    controller = MissionController.create(graph, store)
    controller.inject(SteeringKind.PAUSE, "pause")
    controller.inject(SteeringKind.RESUME, "resume")
    events = store.events()
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    kinds = [event["kind"] for event in events]
    assert "steering" in kinds and "steering_applied" in kinds
