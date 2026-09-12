from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReasoningLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTRA_HIGH = "extra_high"


@dataclass(frozen=True)
class ReasoningDecision:
    level: ReasoningLevel
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ReasoningPolicy:
    high_threshold: float = 0.62
    extra_high_threshold: float = 0.84
    low_threshold: float = 0.28

    def choose(
        self,
        *,
        complexity: float,
        uncertainty: float = 0.0,
        verification_failures: int = 0,
        remaining_budget_ratio: float = 1.0,
        critical: bool = False,
    ) -> ReasoningDecision:
        complexity = min(max(complexity, 0.0), 1.0)
        uncertainty = min(max(uncertainty, 0.0), 1.0)
        remaining_budget_ratio = min(max(remaining_budget_ratio, 0.0), 1.0)
        failure_pressure = min(max(verification_failures, 0) / 3, 1.0)
        budget_pressure = 1.0 - remaining_budget_ratio

        score = 0.48 * complexity + 0.28 * uncertainty + 0.18 * failure_pressure
        score += 0.10 if critical else 0.0
        # When budget is tight, reserve expensive reasoning unless verification is already failing.
        score -= 0.16 * budget_pressure * (1.0 - failure_pressure)
        score = min(max(score, 0.0), 1.0)

        reasons = []
        if complexity >= 0.65:
            reasons.append("high_complexity")
        if uncertainty >= 0.55:
            reasons.append("high_uncertainty")
        if verification_failures:
            reasons.append("verification_failures")
        if critical:
            reasons.append("critical_task")
        if remaining_budget_ratio <= 0.25:
            reasons.append("budget_constrained")

        if score >= self.extra_high_threshold:
            level = ReasoningLevel.EXTRA_HIGH
        elif score >= self.high_threshold:
            level = ReasoningLevel.HIGH
        elif score <= self.low_threshold:
            level = ReasoningLevel.LOW
        else:
            level = ReasoningLevel.MEDIUM
        return ReasoningDecision(level=level, score=score, reasons=tuple(reasons))
