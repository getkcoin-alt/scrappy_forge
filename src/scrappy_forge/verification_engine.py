from __future__ import annotations

import inspect
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Iterable

from .util import ForgeError, encoded, sha
from .workspace import tree_hash


class FindingSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


@dataclass(frozen=True)
class ReviewFinding:
    reviewer: str
    summary: str
    severity: FindingSeverity = FindingSeverity.WARNING
    evidence: tuple[str, ...] = ()
    source: str = "reviewer"

    def __post_init__(self):
        if not self.reviewer.strip() or not self.summary.strip():
            raise ForgeError("Reviewer findings require reviewer and summary")
        if len(self.summary) > 2000:
            raise ForgeError("Reviewer finding summary is too long")


@dataclass(frozen=True)
class CheckEvidence:
    name: str
    passed: bool
    result: dict


@dataclass(frozen=True)
class VerificationReport:
    schema_version: int
    tree_hash: str
    started_at: float
    finished_at: float
    checks: tuple[CheckEvidence, ...]
    findings: tuple[ReviewFinding, ...]
    regression_failures: tuple[str, ...]
    deterministic_passed: bool
    review_blocked: bool
    verified: bool
    evidence_id: str
    verifier_identity: str = "controller"
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        value = asdict(self)
        value["checks"] = [asdict(item) for item in self.checks]
        value["findings"] = [{**asdict(item), "severity": item.severity.value} for item in self.findings]
        return value


@dataclass(frozen=True)
class ReviewRequest:
    tree_hash: str
    task: str
    acceptance: tuple[str, ...]
    changed: bool
    deterministic_checks: tuple[CheckEvidence, ...]
    baseline_checks: tuple[dict, ...]
    evidence_summary: dict = field(default_factory=dict)


CheckRunner = Callable[[dict], Awaitable[dict]]
Reviewer = Callable[[ReviewRequest], Iterable[ReviewFinding] | Awaitable[Iterable[ReviewFinding]]]


class VerificationEngine:
    """Controller-side verification with evidence bound to an immutable tree hash.

    Deterministic checks are authoritative for success. Independent reviewers may add
    blocking findings, but reviewer/model confidence can never turn failing or absent
    deterministic checks into a verified result.
    """

    def __init__(self, workspace_root, *, audit: Callable[[str, dict], object] | None = None):
        self.workspace_root = workspace_root
        self.audit = audit

    @staticmethod
    def _baseline_regressions(baseline: Iterable[dict], current: Iterable[CheckEvidence]) -> tuple[str, ...]:
        baseline_by_name = {
            str(item.get("name")): bool(item.get("passed"))
            for item in baseline
            if item.get("name") is not None
        }
        regressions = []
        for item in current:
            if baseline_by_name.get(item.name) is True and not item.passed:
                regressions.append(item.name)
        return tuple(sorted(set(regressions)))

    @staticmethod
    async def _review(reviewer: Reviewer, request: ReviewRequest) -> tuple[ReviewFinding, ...]:
        raw = reviewer(request)
        if inspect.isawaitable(raw):
            raw = await raw
        findings = tuple(raw or ())
        if any(not isinstance(item, ReviewFinding) for item in findings):
            raise ForgeError("Reviewer must return ReviewFinding values")
        return findings

    async def run(
        self,
        checks: Iterable[dict],
        run_check: CheckRunner,
        *,
        task: str = "",
        acceptance: Iterable[str] = (),
        baseline_checks: Iterable[dict] = (),
        reviewers: Iterable[Reviewer] = (),
        changed: bool = True,
    ) -> VerificationReport:
        started_at = time.time()
        before = tree_hash(self.workspace_root)
        evidence = []
        for check in checks:
            name = str(check.get("name", "")).strip()
            if not name:
                raise ForgeError("Verification check requires a name")
            try:
                result = await run_check(check)
                if not isinstance(result, dict):
                    raise ForgeError("Verification runner must return an object")
                normalized = dict(result)
                normalized.setdefault("name", name)
                normalized["passed"] = bool(normalized.get("passed", False))
            except (ForgeError, OSError) as exc:
                normalized = {"name": name, "passed": False, "error": str(exc)[:1000]}
            evidence.append(CheckEvidence(name=name, passed=normalized["passed"], result=normalized))

        after_checks = tree_hash(self.workspace_root)
        tree_stable = after_checks == before
        baseline = tuple(dict(item) for item in baseline_checks)
        regressions = self._baseline_regressions(baseline, evidence)
        deterministic_passed = bool(evidence) and all(item.passed for item in evidence) and tree_stable

        request = ReviewRequest(
            tree_hash=before,
            task=task,
            acceptance=tuple(str(item) for item in acceptance),
            changed=changed,
            deterministic_checks=tuple(evidence),
            baseline_checks=baseline,
            evidence_summary={
                "tree_stable": tree_stable,
                "regressions": list(regressions),
                "check_count": len(evidence),
            },
        )
        findings: list[ReviewFinding] = []
        for reviewer in reviewers:
            findings.extend(await self._review(reviewer, request))

        final_hash = tree_hash(self.workspace_root)
        if final_hash != before:
            deterministic_passed = False
            findings.append(
                ReviewFinding(
                    reviewer="controller",
                    summary="Workspace changed during verification/review; evidence no longer matches current tree.",
                    severity=FindingSeverity.BLOCKING,
                    source="integrity",
                )
            )
        review_blocked = any(item.severity == FindingSeverity.BLOCKING for item in findings)
        verified = deterministic_passed and not regressions and not review_blocked and final_hash == before
        finished_at = time.time()

        payload = {
            "schema_version": 1,
            "tree_hash": before,
            "checks": [asdict(item) for item in evidence],
            "findings": [{**asdict(item), "severity": item.severity.value} for item in findings],
            "regression_failures": list(regressions),
            "deterministic_passed": deterministic_passed,
            "review_blocked": review_blocked,
            "verified": verified,
        }
        evidence_id = sha(encoded(payload))
        notes = []
        if not evidence:
            notes.append("no deterministic checks configured")
        if not tree_stable or final_hash != before:
            notes.append("workspace changed during verification")
        if regressions:
            notes.append("previously passing baseline check regressed")

        report = VerificationReport(
            schema_version=1,
            tree_hash=before,
            started_at=started_at,
            finished_at=finished_at,
            checks=tuple(evidence),
            findings=tuple(findings),
            regression_failures=regressions,
            deterministic_passed=deterministic_passed,
            review_blocked=review_blocked,
            verified=verified,
            evidence_id=evidence_id,
            notes=tuple(notes),
        )
        if self.audit:
            self.audit("verification_report", report.to_dict())
        return report
