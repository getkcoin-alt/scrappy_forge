import pytest

from scrappy_forge.verification_engine import (
    FindingSeverity,
    ReviewFinding,
    VerificationEngine,
)
from scrappy_forge.workspace import tree_hash


async def test_verified_requires_deterministic_checks_and_stable_tree(repo):
    engine = VerificationEngine(repo)

    async def run_check(check):
        return {"name": check["name"], "passed": True, "output": "ok"}

    report = await engine.run([{"name": "unit"}], run_check)
    assert report.verified is True
    assert report.deterministic_passed is True
    assert report.tree_hash == tree_hash(repo)
    assert report.evidence_id


async def test_reviewer_confidence_cannot_override_failing_check(repo):
    engine = VerificationEngine(repo)

    async def run_check(check):
        return {"name": check["name"], "passed": False, "output": "failure"}

    def optimistic_reviewer(_request):
        return [ReviewFinding("review-model", "Looks correct", FindingSeverity.INFO)]

    report = await engine.run([{"name": "unit"}], run_check, reviewers=[optimistic_reviewer])
    assert report.verified is False
    assert report.deterministic_passed is False


async def test_blocking_independent_review_can_stop_release(repo):
    engine = VerificationEngine(repo)

    async def run_check(check):
        return {"name": check["name"], "passed": True}

    def adversarial_reviewer(request):
        assert request.tree_hash == tree_hash(repo)
        return [
            ReviewFinding(
                "independent-reviewer",
                "Acceptance case for malformed input is missing",
                FindingSeverity.BLOCKING,
                evidence=("acceptance:malformed-input",),
            )
        ]

    report = await engine.run(
        [{"name": "unit"}],
        run_check,
        acceptance=["reject malformed input"],
        reviewers=[adversarial_reviewer],
    )
    assert report.review_blocked is True
    assert report.verified is False


async def test_detects_regression_against_passing_baseline(repo):
    engine = VerificationEngine(repo)

    async def run_check(check):
        return {"name": check["name"], "passed": check["name"] != "unit"}

    report = await engine.run(
        [{"name": "unit"}, {"name": "lint"}],
        run_check,
        baseline_checks=[{"name": "unit", "passed": True}, {"name": "lint", "passed": False}],
    )
    assert report.regression_failures == ("unit",)
    assert report.verified is False


async def test_workspace_mutation_during_check_invalidates_evidence(repo):
    engine = VerificationEngine(repo)

    async def mutating_check(check):
        (repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
        return {"name": check["name"], "passed": True}

    report = await engine.run([{"name": "unit"}], mutating_check)
    assert report.verified is False
    assert "workspace changed during verification" in report.notes


async def test_workspace_mutation_by_reviewer_invalidates_evidence(repo):
    engine = VerificationEngine(repo)

    async def run_check(check):
        return {"name": check["name"], "passed": True}

    def bad_reviewer(_request):
        (repo / "calculator.py").write_text("tampered = True\n")
        return []

    report = await engine.run([{"name": "unit"}], run_check, reviewers=[bad_reviewer])
    assert report.verified is False
    assert any(item.source == "integrity" for item in report.findings)


async def test_no_checks_never_self_certifies(repo):
    engine = VerificationEngine(repo)

    async def run_check(_check):
        pytest.fail("runner should not be called")

    report = await engine.run([], run_check)
    assert report.verified is False
    assert report.deterministic_passed is False
    assert "no deterministic checks configured" in report.notes


async def test_audit_receives_tree_bound_report(repo):
    events = []
    engine = VerificationEngine(repo, audit=lambda kind, data: events.append((kind, data)))

    async def run_check(check):
        return {"name": check["name"], "passed": True}

    report = await engine.run([{"name": "unit"}], run_check)
    assert events[0][0] == "verification_report"
    assert events[0][1]["tree_hash"] == report.tree_hash
    assert events[0][1]["evidence_id"] == report.evidence_id
