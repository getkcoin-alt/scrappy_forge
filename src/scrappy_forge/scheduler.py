from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .task_graph import (
    IdempotencyClass,
    NodeState,
    SideEffectClass,
    TaskGraph,
    TaskNode,
)
from .util import ForgeError

Executor = Callable[[TaskNode], Awaitable[dict]]
Reconciler = Callable[[TaskNode], Awaitable[str]]


@dataclass(frozen=True)
class ScheduledBatch:
    nodes: tuple[TaskNode, ...]


class Scheduler:
    """Controller-side scheduler for typed operations.

    It decides concurrency and retry safety. The provided executor remains responsible
    for invoking Registry/Policy/ExecutionKernel; this class never grants permission.
    """

    def __init__(self, graph: TaskGraph, *, max_concurrency: int = 4):
        if max_concurrency < 1:
            raise ForgeError("Scheduler concurrency must be positive")
        self.graph = graph
        self.max_concurrency = max_concurrency

    @staticmethod
    def _overlap(left: TaskNode, right: TaskNode) -> bool:
        return bool(set(left.resources) & set(right.resources))

    @classmethod
    def _compatible(cls, selected: list[TaskNode], candidate: TaskNode) -> bool:
        for running in selected:
            if not cls._overlap(running, candidate):
                continue
            if running.mutates or candidate.mutates:
                return False
        return True

    def next_batch(self) -> ScheduledBatch:
        ready = sorted(self.graph.ready(), key=lambda node: node.id)
        selected: list[TaskNode] = []
        for node in ready:
            if len(selected) >= self.max_concurrency:
                break
            if self._compatible(selected, node):
                selected.append(node)
        return ScheduledBatch(tuple(selected))

    async def _run_one(self, node: TaskNode, executor: Executor) -> None:
        # RUNNING and the attempt count are durably reserved before this method
        # is dispatched. A controller crash must never turn an in-flight write
        # back into a READY operation.
        if node.deadline is not None and node.deadline <= time.time():
            self.graph.transition(node.id, NodeState.FAILED, error="deadline expired before execution")
            return
        try:
            if node.deadline is None:
                result = await executor(node)
            else:
                # Deadlines are absolute Unix timestamps so they survive restarts.
                async with asyncio.timeout(max(0, node.deadline - time.time())):
                    result = await executor(node)
        except asyncio.CancelledError:
            # Cancellation does not prove the write never reached its target.
            if node.side_effect != SideEffectClass.NONE:
                self.graph.transition(
                    node.id, NodeState.UNCERTAIN, error="cancelled during side effect; reconcile before retry"
                )
            else:
                self.graph.transition(node.id, NodeState.CANCELLED, error="cancelled")
            raise
        except Exception as exc:  # executor is a controller boundary; normalize failures here
            message = str(exc)[:1000]
            uncertain_external = node.side_effect != SideEffectClass.NONE and node.idempotency in {
                IdempotencyClass.RECONCILE_BEFORE_RETRY,
                IdempotencyClass.NON_IDEMPOTENT,
            }
            if uncertain_external:
                self.graph.transition(node.id, NodeState.UNCERTAIN, error=message)
                return
            if node.attempts <= node.max_retries and node.idempotency in {
                IdempotencyClass.READ_ONLY,
                IdempotencyClass.IDEMPOTENT,
            }:
                self.graph.transition(node.id, NodeState.FAILED, error=message)
                self.graph.transition(node.id, NodeState.READY, error=message)
                return
            self.graph.transition(node.id, NodeState.FAILED, error=message)
            return
        self.graph.transition(node.id, NodeState.VERIFIED, result=result)

    async def run_ready(
        self, executor: Executor, *, on_started: Callable[[], None] | None = None
    ) -> list[TaskNode]:
        batch = self.next_batch().nodes
        if not batch:
            return []
        for node in batch:
            self.graph.transition(node.id, NodeState.RUNNING)
            node.attempts += 1
        # Synchronous, fail-closed persistence hook. No executor is invoked
        # until every RUNNING reservation has been saved by the controller.
        if on_started is not None:
            try:
                on_started()
            except BaseException:
                for node in batch:
                    self.graph.transition(
                        node.id, NodeState.FAILED, error="failed to persist execution reservation"
                    )
                raise
        await asyncio.gather(*(self._run_one(node, executor) for node in batch))
        return list(batch)

    async def reconcile_uncertain(self, node_id: str, reconciler: Reconciler) -> TaskNode:
        node = self.graph.nodes[node_id]
        if node.state != NodeState.UNCERTAIN:
            raise ForgeError("Only uncertain operations can be reconciled")
        outcome = await reconciler(node)
        if outcome == "verified":
            return self.graph.transition(node_id, NodeState.VERIFIED, result={"reconciled": True})
        if outcome == "retry":
            if node.idempotency not in {
                IdempotencyClass.IDEMPOTENT,
                IdempotencyClass.RECONCILE_BEFORE_RETRY,
            }:
                raise ForgeError("Reconciliation cannot authorize retry of non-idempotent action")
            return self.graph.transition(node_id, NodeState.READY, error=None)
        if outcome == "failed":
            return self.graph.transition(node_id, NodeState.FAILED, error="reconciliation failed")
        raise ForgeError("Reconciler must return verified, retry, or failed")
