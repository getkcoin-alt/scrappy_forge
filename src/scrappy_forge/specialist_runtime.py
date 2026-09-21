from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

from .model_router import ModelRouter, RoutingDecision, TaskProfile
from .util import ForgeError


class SpecialistRole(str, Enum):
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    DEBUGGER = "debugger"


@dataclass(frozen=True)
class AgentBudget:
    max_turns: int = 4
    max_input_tokens: int = 32_000
    max_output_tokens: int = 8_000
    max_cost_usd: float | None = None
    max_seconds: float = 180.0

    def __post_init__(self):
        if self.max_turns <= 0 or self.max_input_tokens <= 0 or self.max_output_tokens <= 0:
            raise ForgeError("Specialist budgets must be positive")
        if self.max_seconds <= 0:
            raise ForgeError("Specialist time budget must be positive")


@dataclass(frozen=True)
class SpecialistRequest:
    mission_id: str
    task_id: str
    role: SpecialistRole
    objective: str
    context: dict
    profile: TaskProfile
    budget: AgentBudget = field(default_factory=AgentBudget)


@dataclass(frozen=True)
class AgentProposal:
    role: SpecialistRole
    task_id: str
    summary: str
    operations: tuple[dict, ...] = ()
    artifacts: tuple[dict, ...] = ()
    evidence_requests: tuple[str, ...] = ()
    confidence: float | None = None

    def __post_init__(self):
        if not self.summary.strip():
            raise ForgeError("Specialist proposal summary is required")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ForgeError("Specialist confidence must be between 0 and 1")


@dataclass(frozen=True)
class SpecialistResult:
    proposal: AgentProposal
    route: RoutingDecision
    turns: int
    elapsed_seconds: float


ModelInvoker = Callable[[SpecialistRequest, RoutingDecision, int], AgentProposal | Awaitable[AgentProposal]]
Journal = Callable[[str, dict], None]


class SpecialistRuntime:
    """Bounded model-role runtime with no execution authority.

    Specialists produce typed proposals only. They cannot call tools, grant permissions,
    transition mission state, or mark work verified. The controller must separately route
    proposed operations through Registry -> Policy -> ExecutionKernel and VerificationEngine.
    """

    def __init__(self, router: ModelRouter, invoke: ModelInvoker, *, journal: Journal | None = None):
        self.router = router
        self.invoke = invoke
        self.journal = journal

    def _emit(self, kind: str, payload: dict) -> None:
        if self.journal is not None:
            self.journal(kind, payload)

    async def run(self, request: SpecialistRequest) -> SpecialistResult:
        if not request.mission_id.strip() or not request.task_id.strip():
            raise ForgeError("Specialist request requires mission_id and task_id")
        if not request.objective.strip():
            raise ForgeError("Specialist request requires an objective")
        if request.profile.estimated_input_tokens > request.budget.max_input_tokens:
            raise ForgeError("Compiled context exceeds specialist input-token budget")
        if request.profile.estimated_output_tokens > request.budget.max_output_tokens:
            raise ForgeError("Requested output exceeds specialist output-token budget")

        profile = request.profile
        if request.budget.max_cost_usd is not None:
            limit = profile.max_estimated_cost_usd
            effective = request.budget.max_cost_usd if limit is None else min(limit, request.budget.max_cost_usd)
            profile = TaskProfile(**{**profile.__dict__, "max_estimated_cost_usd": effective})

        route = self.router.route(profile)
        started = time.monotonic()
        self._emit(
            "specialist_started",
            {
                "mission_id": request.mission_id,
                "task_id": request.task_id,
                "role": request.role.value,
                "model": route.primary.key,
            },
        )

        last_error: Exception | None = None
        for turn in range(1, request.budget.max_turns + 1):
            if time.monotonic() - started >= request.budget.max_seconds:
                raise ForgeError("Specialist time budget exhausted")
            try:
                value = self.invoke(request, route, turn)
                proposal = await value if inspect.isawaitable(value) else value
                if not isinstance(proposal, AgentProposal):
                    raise ForgeError("Specialist invoker must return AgentProposal")
                if proposal.role != request.role or proposal.task_id != request.task_id:
                    raise ForgeError("Specialist proposal identity mismatch")
                elapsed = time.monotonic() - started
                result = SpecialistResult(proposal=proposal, route=route, turns=turn, elapsed_seconds=elapsed)
                self._emit(
                    "specialist_finished",
                    {
                        "mission_id": request.mission_id,
                        "task_id": request.task_id,
                        "role": request.role.value,
                        "turns": turn,
                        "operations": len(proposal.operations),
                    },
                )
                return result
            except (ForgeError, OSError, TimeoutError) as exc:
                last_error = exc
                self.router.mark_failure(route.primary.key)
                self._emit(
                    "specialist_attempt_failed",
                    {
                        "mission_id": request.mission_id,
                        "task_id": request.task_id,
                        "role": request.role.value,
                        "turn": turn,
                        "error": str(exc)[:1000],
                    },
                )
                if turn < request.budget.max_turns:
                    route = self.router.route(profile)

        raise ForgeError(f"Specialist turn budget exhausted: {last_error}")

    async def run_reviewer(self, request: SpecialistRequest, *, implementer_model_key: str) -> SpecialistResult:
        if request.role != SpecialistRole.REVIEWER:
            raise ForgeError("run_reviewer requires reviewer role")
        route = self.router.route(request.profile, exclude={implementer_model_key})
        started = time.monotonic()
        value = self.invoke(request, route, 1)
        proposal = await value if inspect.isawaitable(value) else value
        if not isinstance(proposal, AgentProposal):
            raise ForgeError("Specialist invoker must return AgentProposal")
        if proposal.role != request.role or proposal.task_id != request.task_id:
            raise ForgeError("Specialist proposal identity mismatch")
        return SpecialistResult(proposal, route, 1, time.monotonic() - started)
