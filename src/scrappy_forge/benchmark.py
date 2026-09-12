from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from .util import ForgeError


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    repository: str
    objective: str
    acceptance: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    budget_usd: float | None = None

    @classmethod
    def from_dict(cls, value: dict) -> "BenchmarkCase":
        required = {"id", "repository", "objective"}
        missing = required - set(value)
        if missing:
            raise ForgeError("Benchmark case missing fields: " + ", ".join(sorted(missing)))
        return cls(
            id=str(value["id"]),
            repository=str(value["repository"]),
            objective=str(value["objective"]),
            acceptance=tuple(str(v) for v in value.get("acceptance", ())),
            tags=tuple(str(v) for v in value.get("tags", ())),
            budget_usd=float(value["budget_usd"]) if value.get("budget_usd") is not None else None,
        )


@dataclass
class BenchmarkMetrics:
    task_completed: bool = False
    first_pass_correct: bool = False
    patch_accepted: bool = False
    regression_count: int = 0
    review_seconds: float = 0.0
    wall_seconds: float = 0.0
    model_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    recovery_success: bool | None = None
    human_interventions: int = 0

    def validate(self) -> None:
        numeric = {
            "regression_count": self.regression_count,
            "review_seconds": self.review_seconds,
            "wall_seconds": self.wall_seconds,
            "model_requests": self.model_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tool_calls": self.tool_calls,
            "cost_usd": self.cost_usd,
            "human_interventions": self.human_interventions,
        }
        if any(value < 0 for value in numeric.values()):
            raise ForgeError("Benchmark metrics cannot be negative")


@dataclass
class BenchmarkResult:
    case_id: str
    variant: str
    commit: str
    model: str
    budget: dict
    metrics: BenchmarkMetrics
    evidence: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        self.metrics.validate()
        value = asdict(self)
        value["schema_version"] = 1
        return value


class BenchmarkArtifact:
    """Append-only, machine-readable benchmark artifact.

    The harness intentionally does not infer correctness from agent prose. Callers must
    populate metrics from held-out checks, blinded review, accounting and controller events.
    """

    def __init__(self, *, corpus: str, baseline_commit: str, experiment_commit: str | None = None):
        self.corpus = corpus
        self.baseline_commit = baseline_commit
        self.experiment_commit = experiment_commit
        self.results: list[BenchmarkResult] = []

    def add(self, result: BenchmarkResult) -> None:
        if any(r.case_id == result.case_id and r.variant == result.variant for r in self.results):
            raise ForgeError(f"Duplicate benchmark result for {result.variant}:{result.case_id}")
        result.metrics.validate()
        self.results.append(result)

    def summary(self, variant: str) -> dict:
        rows = [r.metrics for r in self.results if r.variant == variant]
        if not rows:
            return {"cases": 0}

        def rate(name: str) -> float:
            return sum(bool(getattr(row, name)) for row in rows) / len(rows)

        recovery = [r.recovery_success for r in rows if r.recovery_success is not None]
        return {
            "cases": len(rows),
            "task_completion_rate": rate("task_completed"),
            "first_pass_correctness": rate("first_pass_correct"),
            "accepted_patch_rate": rate("patch_accepted"),
            "regressions": sum(r.regression_count for r in rows),
            "median_review_seconds": statistics.median(r.review_seconds for r in rows),
            "median_wall_seconds": statistics.median(r.wall_seconds for r in rows),
            "model_requests": sum(r.model_requests for r in rows),
            "input_tokens": sum(r.input_tokens for r in rows),
            "output_tokens": sum(r.output_tokens for r in rows),
            "tool_calls": sum(r.tool_calls for r in rows),
            "cost_usd": round(sum(r.cost_usd for r in rows), 8),
            "recovery_success_rate": (
                sum(bool(value) for value in recovery) / len(recovery) if recovery else None
            ),
            "human_interventions": sum(r.human_interventions for r in rows),
        }

    def compare(self, baseline: str = "baseline", experiment: str = "experiment") -> dict:
        return {"baseline": self.summary(baseline), "experiment": self.summary(experiment)}

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "corpus": self.corpus,
            "baseline_commit": self.baseline_commit,
            "experiment_commit": self.experiment_commit,
            "results": [result.to_dict() for result in self.results],
            "comparison": self.compare(),
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")


def load_corpus(path: Path) -> list[BenchmarkCase]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ForgeError(f"Could not load benchmark corpus: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ForgeError("Benchmark corpus must be a non-empty JSON array")
    cases = [BenchmarkCase.from_dict(value) for value in raw]
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ForgeError("Benchmark case IDs must be unique")
    return cases


def comparable_cases(results: Iterable[BenchmarkResult]) -> set[str]:
    variants: dict[str, set[str]] = {}
    for result in results:
        variants.setdefault(result.variant, set()).add(result.case_id)
    if len(variants) < 2:
        return set()
    sets = list(variants.values())
    return set.intersection(*sets)
