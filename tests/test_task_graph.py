import asyncio

import pytest

from scrappy_forge.scheduler import Scheduler
from scrappy_forge.task_graph import (
    IdempotencyClass,
    NodeState,
    SideEffectClass,
    TaskGraph,
    TaskNode,
)
from scrappy_forge.util import ForgeError


def read_node(node_id, *, deps=(), resources=()):
    return TaskNode(id=node_id, kind="read", dependencies=deps, resources=resources)


def edit_node(node_id, *, deps=(), resources=()):
    return TaskNode(
        id=node_id,
        kind="edit",
        dependencies=deps,
        resources=resources,
        mutates=True,
        idempotency=IdempotencyClass.IDEMPOTENT,
        side_effect=SideEffectClass.WORKSPACE,
    )


def test_dependencies_promote_only_after_verified():
    graph = TaskGraph("m1")
    graph.add(read_node("inspect"))
    graph.add(edit_node("change", deps=("inspect",), resources=("file:a.py",)))
    assert [node.id for node in graph.ready()] == ["inspect"]
    graph.transition("inspect", NodeState.RUNNING)
    graph.transition("inspect", NodeState.VERIFIED, result={"ok": True})
    assert [node.id for node in graph.ready()] == ["change"]


def test_serializes_overlapping_mutations_but_allows_independent_reads():
    graph = TaskGraph("m1")
    graph.add(edit_node("a", resources=("file:x.py",)))
    graph.add(edit_node("b", resources=("file:x.py",)))
    graph.add(read_node("c", resources=("file:y.py",)))
    batch = Scheduler(graph, max_concurrency=4).next_batch().nodes
    assert {node.id for node in batch} == {"a", "c"}


async def test_independent_reads_execute_concurrently():
    graph = TaskGraph("m1")
    graph.add(read_node("a", resources=("file:a",)))
    graph.add(read_node("b", resources=("file:b",)))
    active = 0
    peak = 0

    async def execute(node):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"node": node.id}

    await Scheduler(graph, max_concurrency=2).run_ready(execute)
    assert peak == 2
    assert all(node.state == NodeState.VERIFIED for node in graph.nodes.values())


async def test_external_ambiguous_failure_is_not_replayed():
    graph = TaskGraph("m1")
    graph.add(
        TaskNode(
            id="send",
            kind="external",
            mutates=True,
            resources=("external:ticket:123",),
            idempotency=IdempotencyClass.RECONCILE_BEFORE_RETRY,
            side_effect=SideEffectClass.EXTERNAL,
            max_retries=5,
        )
    )
    calls = 0

    async def execute(_):
        nonlocal calls
        calls += 1
        raise RuntimeError("timeout after request")

    scheduler = Scheduler(graph)
    await scheduler.run_ready(execute)
    assert calls == 1
    assert graph.nodes["send"].state == NodeState.UNCERTAIN
    await scheduler.run_ready(execute)
    assert calls == 1


async def test_reconciliation_can_make_retry_explicit():
    graph = TaskGraph("m1")
    graph.add(
        TaskNode(
            id="external",
            kind="external",
            mutates=True,
            idempotency=IdempotencyClass.RECONCILE_BEFORE_RETRY,
            side_effect=SideEffectClass.EXTERNAL,
        )
    )
    graph.transition("external", NodeState.RUNNING)
    graph.transition("external", NodeState.UNCERTAIN, error="timeout")

    async def reconcile(_):
        return "retry"

    node = await Scheduler(graph).reconcile_uncertain("external", reconcile)
    assert node.state == NodeState.READY


def test_graph_round_trip_preserves_state():
    graph = TaskGraph("m1")
    graph.add(read_node("inspect"))
    graph.transition("inspect", NodeState.RUNNING)
    graph.transition("inspect", NodeState.VERIFIED, result={"hash": "abc"})
    restored = TaskGraph.from_dict(graph.to_dict())
    assert restored.nodes["inspect"].state == NodeState.VERIFIED
    assert restored.nodes["inspect"].result == {"hash": "abc"}


def test_mutation_must_declare_idempotency():
    with pytest.raises(ForgeError, match="cannot be classified read-only"):
        TaskNode(id="bad", kind="edit", mutates=True)
