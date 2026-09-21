import json

import pytest

from scrappy_forge.benchmark import (
    BenchmarkArtifact,
    BenchmarkMetrics,
    BenchmarkResult,
    comparable_cases,
    load_corpus,
)
from scrappy_forge.util import ForgeError


def result(case, variant, **metrics):
    return BenchmarkResult(
        case_id=case,
        variant=variant,
        commit="abc123",
        model="fixture/model",
        budget={"max_steps": 8, "usd": 1.0},
        metrics=BenchmarkMetrics(**metrics),
    )


def test_artifact_summarizes_controller_metrics(tmp_path):
    artifact = BenchmarkArtifact(corpus="fixture-v1", baseline_commit="base", experiment_commit="exp")
    artifact.add(
        result(
            "one",
            "baseline",
            task_completed=True,
            first_pass_correct=True,
            patch_accepted=True,
            wall_seconds=10,
            review_seconds=5,
            model_requests=2,
            input_tokens=100,
            output_tokens=50,
            tool_calls=3,
            cost_usd=0.1,
            recovery_success=True,
        )
    )
    artifact.add(
        result(
            "one",
            "experiment",
            task_completed=True,
            patch_accepted=True,
            wall_seconds=5,
            review_seconds=2,
            model_requests=1,
            input_tokens=80,
            output_tokens=40,
            tool_calls=2,
            cost_usd=0.05,
            recovery_success=True,
        )
    )
    path = tmp_path / "artifact.json"
    artifact.write(path)
    data = json.loads(path.read_text())
    assert data["comparison"]["baseline"]["task_completion_rate"] == 1.0
    assert data["comparison"]["experiment"]["median_wall_seconds"] == 5
    assert comparable_cases(artifact.results) == {"one"}


def test_artifact_rejects_duplicate_case_variant():
    artifact = BenchmarkArtifact(corpus="fixture", baseline_commit="base")
    artifact.add(result("one", "baseline"))
    with pytest.raises(ForgeError, match="Duplicate"):
        artifact.add(result("one", "baseline"))


def test_metrics_reject_negative_values():
    with pytest.raises(ForgeError, match="cannot be negative"):
        BenchmarkMetrics(tool_calls=-1).validate()


def test_load_corpus_validates_unique_ids(tmp_path):
    path = tmp_path / "corpus.json"
    path.write_text(
        json.dumps(
            [
                {"id": "a", "repository": "repo", "objective": "first"},
                {"id": "a", "repository": "repo", "objective": "second"},
            ]
        )
    )
    with pytest.raises(ForgeError, match="unique"):
        load_corpus(path)
