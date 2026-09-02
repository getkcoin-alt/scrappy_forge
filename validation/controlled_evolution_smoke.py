from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scrappy_forge.candidate_artifact import CheckAttestation, build_candidate_manifest
from scrappy_forge.experiment_runner import run_benchmark_plan
from scrappy_forge.omni_eval import ControllerDecision, FINISH_ACTION
from scrappy_forge.research_queue import BenchmarkPlan
from scrappy_forge.signed_approval import (
    APPROVAL_SCHEMA,
    SIGNATURE_ALGORITHM,
    SignedPromotionApproval,
    approval_signing_bytes,
    evaluate_signed_promotion,
)
from scrappy_forge.signed_promotion_record import build_signed_promotion_record


class BaselineController:
    controller_id = "controlled-smoke-baseline"

    async def choose(self, observation, *, feedback, seed):
        return ControllerDecision(action_id=FINISH_ACTION, model=self.controller_id)


class CandidateController:
    controller_id = "controlled-smoke-candidate"

    _SEQUENCES = {
        "heldout-hospital-generator-001": ("connect-tanker", "transfer-fuel"),
        "heldout-water-quality-001": (
            "isolate-alert-zone",
            "flush-bypass",
            "restore-bypass-supply",
        ),
    }

    def __init__(self) -> None:
        self._positions: dict[str, int] = {}

    async def choose(self, observation, *, feedback, seed):
        scenario_id = str(observation.get("scenario_id") or "")
        sequence = self._SEQUENCES.get(scenario_id, ())
        position = self._positions.get(scenario_id, 0)
        if position >= len(sequence):
            return ControllerDecision(action_id=FINISH_ACTION, model=self.controller_id)
        self._positions[scenario_id] = position + 1
        return ControllerDecision(action_id=sequence[position], model=self.controller_id)


def _plan() -> BenchmarkPlan:
    return BenchmarkPlan(
        correlation_id=UUID("11111111-1111-4111-8111-111111111111"),
        request_event_id=UUID("22222222-2222-4222-8222-222222222222"),
        suite_id="omni-city-held-out-v0",
        scope="general-world-model-and-planning",
        scenario_glob="benchmarks/omni_city_v0/held_out/*.json",
        simulated=True,
        real_world_authority=False,
        execution_authority=False,
        reason="controlled dry-run of verified evolution path",
    )


async def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    run = await run_benchmark_plan(
        _plan(),
        baseline=BaselineController(),
        candidate=CandidateController(),
        repo_root=repo_root,
    )
    if run.decision.status != "improvement":
        raise SystemExit(f"controlled cycle did not produce an improvement: {run.decision.status}")

    candidate_commit = os.environ.get("GITHUB_SHA", "b" * 40)
    if len(candidate_commit) != 40:
        candidate_commit = "b" * 40
    manifest = build_candidate_manifest(
        run,
        baseline_source_commit="82ea54b9ff10b1026038c552392d098acda25b48",
        candidate_source_commit=candidate_commit,
    )

    now = datetime.now(timezone.utc).isoformat()
    security = CheckAttestation(
        check_name="security",
        manifest_sha256=manifest.manifest_sha256,
        status="passed",
        evidence_ref="dry-run://security/controlled-cycle-v0.3",
        observed_at=now,
    )
    regression = CheckAttestation(
        check_name="regression",
        manifest_sha256=manifest.manifest_sha256,
        status="passed",
        evidence_ref="dry-run://regression/controlled-cycle-v0.3",
        observed_at=now,
    )

    private_key = Ed25519PrivateKey.generate()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    approval = SignedPromotionApproval(
        approval_schema=APPROVAL_SCHEMA,
        manifest_sha256=manifest.manifest_sha256,
        approver_actor_id="human:controlled-smoke",
        actor_kind="human",
        decision="approved",
        approved_at=now,
        nonce=str(uuid4()),
        key_id="controlled-smoke-ed25519",
        signature_algorithm=SIGNATURE_ALGORITHM,
        signature_base64="",
    )
    signature = private_key.sign(approval_signing_bytes(approval))
    approval = SignedPromotionApproval(
        **{**asdict(approval), "signature_base64": base64.b64encode(signature).decode("ascii")}
    )

    promotion = evaluate_signed_promotion(
        manifest,
        security=security,
        regression=regression,
        approval=approval,
        public_key_pem=public_pem,
        expected_key_id="controlled-smoke-ed25519",
        expected_approver_actor_id="human:controlled-smoke",
    )
    record = build_signed_promotion_record(
        promotion,
        security=security,
        regression=regression,
        approval=approval,
    )

    if not promotion.eligible_for_merge:
        raise SystemExit("controlled cycle failed to reach merge eligibility")
    if promotion.execution_authority or promotion.merge_authority or promotion.deploy_authority:
        raise SystemExit("controlled cycle unexpectedly acquired runtime authority")
    if record.execution_authority or record.merge_authority or record.deploy_authority:
        raise SystemExit("promotion record unexpectedly acquired runtime authority")

    print(
        json.dumps(
            {
                "controlled_cycle": "PASS",
                "simulated": True,
                "baseline_mean_goal_score": run.decision.baseline_mean_goal_score,
                "candidate_mean_goal_score": run.decision.candidate_mean_goal_score,
                "goal_score_delta": run.decision.goal_score_delta,
                "manifest_sha256": manifest.manifest_sha256,
                "promotion_record_sha256": record.record_sha256,
                "eligible_for_merge": promotion.eligible_for_merge,
                "execution_authority": promotion.execution_authority,
                "merge_authority": promotion.merge_authority,
                "deploy_authority": promotion.deploy_authority,
                "final_boundary": "STOP_BEFORE_MERGE",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
