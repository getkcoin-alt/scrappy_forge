from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .reasoning_policy import ReasoningDecision, ReasoningLevel, ReasoningPolicy
from .util import ForgeError


class PrivacyClass(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"
    LOCAL_ONLY = "local_only"


@dataclass(frozen=True)
class ModelCapability:
    provider: str
    model: str
    context_tokens: int
    output_tokens: int
    tool_support: bool = False
    structured_outputs: bool = False
    reasoning_levels: tuple[ReasoningLevel, ...] = (ReasoningLevel.MEDIUM,)
    latency_ms_p50: float | None = None
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    reliability: float = 1.0
    computer_use: bool = False
    privacy: PrivacyClass = PrivacyClass.PRIVATE
    available: bool = True
    metadata: dict = field(default_factory=dict, compare=False)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class TaskProfile:
    complexity: float
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    uncertainty: float = 0.0
    verification_failures: int = 0
    remaining_budget_ratio: float = 1.0
    requires_tools: bool = False
    requires_structured_output: bool = False
    requires_computer_use: bool = False
    privacy: PrivacyClass = PrivacyClass.PRIVATE
    critical: bool = False
    max_latency_ms: float | None = None
    max_estimated_cost_usd: float | None = None


@dataclass(frozen=True)
class RoutingDecision:
    primary: ModelCapability
    reasoning: ReasoningDecision
    fallbacks: tuple[ModelCapability, ...]
    reviewer: ModelCapability | None = None


class ModelRouter:
    """Deterministic capability router; it selects models but never authorizes actions."""

    def __init__(self, capabilities=(), reasoning_policy: ReasoningPolicy | None = None):
        self.capabilities: dict[str, ModelCapability] = {}
        self.reasoning_policy = reasoning_policy or ReasoningPolicy()
        self.failures: dict[str, int] = {}
        for capability in capabilities:
            self.register(capability)

    def register(self, capability: ModelCapability) -> None:
        if capability.context_tokens <= 0 or capability.output_tokens <= 0:
            raise ForgeError("Model token capacities must be positive")
        if not 0 <= capability.reliability <= 1:
            raise ForgeError("Model reliability must be between 0 and 1")
        self.capabilities[capability.key] = capability

    def mark_failure(self, key: str) -> None:
        self.failures[key] = self.failures.get(key, 0) + 1

    def clear_failure(self, key: str) -> None:
        self.failures.pop(key, None)

    def _privacy_ok(self, model: ModelCapability, requested: PrivacyClass) -> bool:
        order = {
            PrivacyClass.PUBLIC: 0,
            PrivacyClass.PRIVATE: 1,
            PrivacyClass.LOCAL_ONLY: 2,
        }
        return order[model.privacy] >= order[requested]

    def _estimated_cost(self, model: ModelCapability, task: TaskProfile) -> float:
        return (
            task.estimated_input_tokens * model.input_cost_per_million
            + task.estimated_output_tokens * model.output_cost_per_million
        ) / 1_000_000

    def _eligible(self, model: ModelCapability, task: TaskProfile, level: ReasoningLevel) -> bool:
        if not model.available or self.failures.get(model.key, 0) >= 3:
            return False
        if model.context_tokens < task.estimated_input_tokens:
            return False
        if model.output_tokens < task.estimated_output_tokens:
            return False
        if task.requires_tools and not model.tool_support:
            return False
        if task.requires_structured_output and not model.structured_outputs:
            return False
        if task.requires_computer_use and not model.computer_use:
            return False
        if not self._privacy_ok(model, task.privacy):
            return False
        if level not in model.reasoning_levels:
            return False
        if task.max_latency_ms is not None and model.latency_ms_p50 is not None:
            if model.latency_ms_p50 > task.max_latency_ms:
                return False
        if task.max_estimated_cost_usd is not None:
            if self._estimated_cost(model, task) > task.max_estimated_cost_usd:
                return False
        return True

    def _score(self, model: ModelCapability, task: TaskProfile) -> float:
        latency = model.latency_ms_p50 if model.latency_ms_p50 is not None else 5000.0
        latency_score = 1.0 / (1.0 + latency / 1000)
        cost = self._estimated_cost(model, task)
        cost_score = 1.0 / (1.0 + cost)
        failure_penalty = min(self.failures.get(model.key, 0) * 0.18, 0.54)
        capability_bonus = 0.0
        capability_bonus += 0.08 if task.requires_tools and model.tool_support else 0.0
        capability_bonus += 0.05 if task.requires_structured_output and model.structured_outputs else 0.0
        capability_bonus += 0.05 if task.requires_computer_use and model.computer_use else 0.0
        return 0.58 * model.reliability + 0.20 * latency_score + 0.17 * cost_score + capability_bonus - failure_penalty

    def route(self, task: TaskProfile, *, exclude: set[str] | None = None) -> RoutingDecision:
        reasoning = self.reasoning_policy.choose(
            complexity=task.complexity,
            uncertainty=task.uncertainty,
            verification_failures=task.verification_failures,
            remaining_budget_ratio=task.remaining_budget_ratio,
            critical=task.critical,
        )
        excluded = exclude or set()
        candidates = [
            model
            for model in self.capabilities.values()
            if model.key not in excluded and self._eligible(model, task, reasoning.level)
        ]
        if not candidates:
            raise ForgeError("No model satisfies the task capability/privacy/budget constraints")
        candidates.sort(key=lambda model: (-self._score(model, task), model.key))
        primary = candidates[0]
        fallbacks = tuple(candidates[1:])
        reviewer = None
        if task.critical:
            reviewer = next(
                (
                    model
                    for model in candidates[1:]
                    if model.provider != primary.provider or model.model != primary.model
                ),
                None,
            )
        return RoutingDecision(primary=primary, reasoning=reasoning, fallbacks=fallbacks, reviewer=reviewer)
