"""Scrappy Engine mechanism tests.

The scripted provider stands in for models, so these tests prove the orchestration
mechanism (state machine, policy, evidence, gates, recovery) — not model capability.
All checks really execute (trusted-local) against real files.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
from starlette.testclient import TestClient

from scrappy_engine.api import create_app
from scrappy_engine.autonomy import AutonomyLevel, PolicyEngine
from scrappy_engine.contracts import MissionSpec
from scrappy_engine.intelligence import ProviderRouter, ScriptedProvider
from scrappy_engine.runner import MissionRunner, acceptance_gate
from scrappy_engine.states import MissionState, check_transition
from scrappy_engine.store import EngineStore
from scrappy_engine.tools import PermissionDenied, ToolGateway, ToolSpec
from scrappy_forge.execution import Runner
from scrappy_forge.util import ForgeError

BUGGY_AUTH = """USERS = {"alice": "wonderland"}


def authenticate(username, password):
    stored = USERS.get(username)
    if stored is None:
        return False
    return stored.startswith(password)
"""

TESTS = """import unittest

from auth import authenticate


class AuthTests(unittest.TestCase):
    def test_valid_login(self):
        self.assertTrue(authenticate("alice", "wonderland"))

    def test_unknown_user(self):
        self.assertFalse(authenticate("bob", "wonderland"))
"""

REGRESSION_TEST = """

class RegressionTests(unittest.TestCase):
    def test_prefix_password_rejected(self):
        self.assertFalse(authenticate("alice", ""))
        self.assertFalse(authenticate("alice", "wonder"))
"""


class SimulatedCrash(BaseException):
    """Escapes every except-clause, like a killed worker."""


def git(cwd, *argv):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *argv],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def auth_repo(tmp_path):
    root = tmp_path / "authsvc"
    (root / "tests").mkdir(parents=True)
    (root / "auth.py").write_text(BUGGY_AUTH)
    (root / "tests" / "__init__.py").write_text("")
    (root / "tests" / "test_auth.py").write_text(TESTS)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


PG_DSN = os.environ.get("SCRAPPY_ENGINE_TEST_PG_DSN")


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "sqlite":
        yield EngineStore(tmp_path / "engine" / "engine.db")
        return
    if not PG_DSN:
        pytest.skip("set SCRAPPY_ENGINE_TEST_PG_DSN to run the PostgreSQL store tests")
    import psycopg

    name = "se_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    parts = urlsplit(PG_DSN)
    yield EngineStore(urlunsplit(parts._replace(path="/" + name)))
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE {name} WITH (FORCE)")


def checks():
    return [
        {
            "name": "unit",
            "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
            "timeout": 60,
        },
        # Held-out operator check: the engineer never sees or controls it.
        {
            "name": "heldout",
            "argv": [
                sys.executable,
                "-c",
                "from auth import authenticate as a; assert not a('alice', 'wond')",
            ],
            "timeout": 60,
        },
    ]


def make_spec(store, repo, **overrides):
    project = store.connect_project("authsvc", repo)
    raw = {
        "project_id": project["id"],
        "objective": "Find the underlying bug in the authentication workflow, fix it and add regression tests.",
        "environment": "trusted-local",
        "autonomy_level": "A3",
        "constraints": {"max_retries": 2, "max_model_calls": 20},
        "acceptance_criteria": ["Truncated passwords are rejected", "Regression test added"],
        "checks": checks(),
    }
    raw.update(overrides)
    return MissionSpec.from_dict(raw)


def architect(brief):
    assert "auth.py" in brief["file_contents"], "architect must receive repository evidence"
    if brief["baseline_checks"]:  # skipped below A2
        assert any(not c["passed"] for c in brief["baseline_checks"])
    return {
        "summary": "authenticate() uses startswith, accepting any prefix of the stored password",
        "root_cause_hypothesis": "prefix comparison instead of exact constant-time comparison",
        "files": ["auth.py", "tests/test_auth.py"],
        "steps": ["Use hmac.compare_digest for exact comparison", "Add regression tests for prefixes"],
        "risks": ["timing side channels"],
    }


WRONG_FIX = {
    "summary": "use substring check",
    "edits": [
        {
            "op": "replace",
            "path": "auth.py",
            "old": "stored.startswith(password)",
            "new": "password in stored",
        }
    ],
}


def right_fix(brief):
    current = brief["file_contents"]["auth.py"]
    old = "password in stored" if "password in stored" in current else "stored.startswith(password)"
    return {
        "summary": "exact constant-time comparison plus regression tests",
        "edits": [
            {
                "op": "write",
                "path": "auth.py",
                "content": "import hmac\n\n"
                + current.replace(old, "hmac.compare_digest(stored.encode(), password.encode())"),
            },
            {
                "op": "write",
                "path": "tests/test_auth.py",
                "content": brief["file_contents"]["tests/test_auth.py"] + REGRESSION_TEST,
            },
        ],
    }


DEBUG = {
    "root_cause": "substring test still accepts 'wond'",
    "next_step": "compare exactly",
    "evidence": ["heldout"],
}
APPROVE = {"verdict": "approve", "findings": [{"severity": "info", "summary": "fix is exact and tested"}]}


def router(engineer_script, reviewer=APPROVE):
    builder = ScriptedProvider(
        "builder",
        {"architect": [architect], "engineer": engineer_script, "debugger": [DEBUG]},
        family="fam-a",
    )
    critic = ScriptedProvider("critic", {"reviewer": [reviewer]}, family="fam-b")
    return ProviderRouter([builder, critic], {"reviewer": ["critic"]})


def runner_for(store, tmp_path, rtr):
    return MissionRunner(
        store, rtr, home=tmp_path / "engine", runner_factory=lambda env: Runner(mode="trusted-local")
    )


async def test_first_acceptance_mission_fixes_auth_bug_with_evidence(store, auth_repo, tmp_path):
    spec = make_spec(store, auth_repo)
    mission, created = store.create_mission(spec)
    assert created
    final = await runner_for(store, tmp_path, router([WRONG_FIX, right_fix])).run(mission["id"])

    assert final["state"] == "COMPLETED", final["reason"]
    result = final["result"]
    assert result["reproduced"] is True
    assert result["attempts"] == 2  # wrong fix failed the held-out check, debugger fed the repair
    assert result["reviewer_provider"] == "critic"
    assert result["review"]["verdict"] == "approve"

    # Every claimed passing check is backed by a real QA execution record on the final tree.
    calls = {c["execution_id"]: c for c in store.tool_calls(mission["id"])}
    for check in result["final_checks"]:
        record = calls[check["execution_id"]]
        assert record["agent_role"] == "qa" and record["status"] == "succeeded"
        assert record["output"]["passed"] is True and record["output"]["tree_hash"] == result["tree_hash"]
    baseline = {c["name"]: c["passed"] for c in result["baseline_checks"]}
    assert baseline == {"unit": True, "heldout": False}

    # Patch, commit on a mission branch, operator checkout untouched.
    patch = next(a for a in store.artifacts(mission["id"]) if a["kind"] == "patch")
    assert "compare_digest" in patch["content"] and "test_prefix_password_rejected" in patch["content"]
    assert (auth_repo / "auth.py").read_text() == BUGGY_AUTH
    log = git(auth_repo, "log", "--format=%H", result["branch"]).stdout.split()
    assert result["commit"] == log[0]
    assert store.verify_audit_chain()
    states = [e["data"]["to"] for e in store.events(mission["id"]) if e["kind"] == "state_changed"]
    assert states == [
        "ANALYZING",
        "PLANNING",
        "READY",
        "RUNNING",
        "VERIFYING",
        "RUNNING",
        "VERIFYING",
        "INTEGRATING",
        "COMPLETED",
    ]


async def test_mission_cannot_complete_without_passing_evidence(store, auth_repo, tmp_path):
    mission, _ = store.create_mission(make_spec(store, auth_repo, constraints={"max_retries": 1}))
    final = await runner_for(store, tmp_path, router([WRONG_FIX])).run(mission["id"])
    assert final["state"] == "FAILED"
    assert "heldout failed" in final["reason"]
    assert final["result"]["status"] == "FAILED"
    assert not store.artifacts(mission["id"])


async def test_blocking_review_prevents_completion(store, auth_repo, tmp_path):
    block = {"verdict": "request_changes", "findings": [{"severity": "blocking", "summary": "deletes tests"}]}
    mission, _ = store.create_mission(make_spec(store, auth_repo, constraints={"max_retries": 0}))
    final = await runner_for(store, tmp_path, router([right_fix], reviewer=block)).run(mission["id"])
    assert final["state"] == "FAILED"
    assert "blocking review finding: deletes tests" in final["reason"]


async def test_low_autonomy_level_blocks_before_editing(store, auth_repo, tmp_path):
    mission, _ = store.create_mission(make_spec(store, auth_repo, autonomy_level="A1"))
    final = await runner_for(store, tmp_path, router([right_fix])).run(mission["id"])
    assert final["state"] == "BLOCKED"
    assert "test.run" in final["reason"] and "A2" in final["reason"]
    assert not any(c["tool_id"].startswith("fs.write") for c in store.tool_calls(mission["id"]))


async def test_crash_mid_mission_resumes_from_durable_state(store, auth_repo, tmp_path):
    crashed = {"done": False}

    def crash_once(brief):
        if not crashed["done"]:
            crashed["done"] = True
            raise SimulatedCrash()
        return right_fix(brief)

    mission, _ = store.create_mission(make_spec(store, auth_repo))
    with pytest.raises(SimulatedCrash):
        await runner_for(store, tmp_path, router([crash_once])).run(mission["id"])
    assert store.get_mission(mission["id"])["state"] == "RUNNING"

    # New process: fresh store handle and runner, recovery first.
    restarted = EngineStore(store.location)
    assert mission["id"] in restarted.recover()
    assert any(r["status"] == "interrupted" for r in restarted.agent_runs(mission["id"]))
    final = await runner_for(restarted, tmp_path, router([crash_once])).run(mission["id"])
    assert final["state"] == "COMPLETED"


async def test_interrupted_tool_call_becomes_uncertain(store, auth_repo):
    spec = make_spec(store, auth_repo)
    mission, _ = store.create_mission(spec)

    async def dies(_args):
        raise SimulatedCrash()

    gw = ToolGateway(store, mission["id"], PolicyEngine(AutonomyLevel.A3))
    gw.register(ToolSpec("fs.write", "fs.write", {"type": "object"}, dies))
    with pytest.raises(SimulatedCrash):
        await gw.execute("engineer", "fs.write", {}, idempotency_key="k1")
    store.recover()
    assert store.find_tool_call(mission["id"], "k1")["status"] == "uncertain"
    with pytest.raises(ForgeError, match="reconcile"):
        await gw.execute("engineer", "fs.write", {}, idempotency_key="k1")


async def test_policy_denials_are_recorded(store, auth_repo):
    mission, _ = store.create_mission(make_spec(store, auth_repo))

    async def ok(_args):
        return {}

    gw = ToolGateway(store, mission["id"], PolicyEngine(AutonomyLevel.A4))
    gw.register(ToolSpec("fs.write", "fs.write", {"type": "object"}, ok))
    gw.register(ToolSpec("test.run", "test.run", {"type": "object"}, ok))
    with pytest.raises(PermissionDenied):
        await gw.execute("reviewer", "fs.write", {})
    with pytest.raises(PermissionDenied):
        await gw.execute("engineer", "test.run", {})  # engineer cannot produce its own acceptance evidence
    statuses = [c["status"] for c in store.tool_calls(mission["id"])]
    assert statuses == ["denied", "denied"]
    assert not PolicyEngine(AutonomyLevel.A4).decide("devops", "deploy.production").allowed


async def test_provider_failover_and_blocking_when_all_fail(store, auth_repo, tmp_path):
    bad = ScriptedProvider("bad", {"architect": [{"nonsense": True}]}, family="x")
    good = ScriptedProvider("good", {"architect": [architect], "engineer": [right_fix]}, family="y")
    critic = ScriptedProvider("critic", {"reviewer": [APPROVE]}, family="z")
    rtr = ProviderRouter([bad, good, critic], {"architect": ["bad", "good"], "reviewer": ["critic"]})
    mission, _ = store.create_mission(make_spec(store, auth_repo))
    final = await runner_for(store, tmp_path, rtr).run(mission["id"])
    assert final["state"] == "COMPLETED"
    assert [r["status"] for r in store.agent_runs(mission["id"]) if r["role"] == "architect"] == [
        "failed",
        "succeeded",
    ]

    only_bad = ProviderRouter([ScriptedProvider("bad", {"architect": [{"nonsense": 1}]})])
    other, _ = store.create_mission(make_spec(store, auth_repo, objective="Same bug, second mission run."))
    final = await runner_for(store, tmp_path, only_bad).run(other["id"])
    assert final["state"] == "BLOCKED" and final["blocked_state"] == "PLANNING"
    resumed = runner_for(store, tmp_path, router([right_fix]))
    resumed.resume(other["id"])
    assert (await resumed.run(other["id"]))["state"] == "COMPLETED"


async def test_model_call_budget_stops_runaway_retries(store, auth_repo, tmp_path):
    mission, _ = store.create_mission(
        make_spec(store, auth_repo, constraints={"max_retries": 10, "max_model_calls": 4})
    )
    final = await runner_for(store, tmp_path, router([WRONG_FIX])).run(mission["id"])
    assert final["state"] == "FAILED" and "budget exhausted" in final["reason"]
    assert final["model_calls"] == 4


async def test_cancel_before_execution(store, auth_repo, tmp_path):
    mission, _ = store.create_mission(make_spec(store, auth_repo))
    store.request_cancel(mission["id"])
    final = await runner_for(store, tmp_path, router([right_fix])).run(mission["id"])
    assert final["state"] == "CANCELLED"


def test_idempotent_mission_creation(store, auth_repo):
    spec = make_spec(store, auth_repo)
    first, created = store.create_mission(spec, idempotency_key="retry-key-1")
    again, created_again = store.create_mission(spec, idempotency_key="retry-key-1")
    assert created and not created_again and first["id"] == again["id"]
    other = make_spec(store, auth_repo, objective="A different objective entirely.")
    with pytest.raises(ForgeError, match="Idempotency key"):
        store.create_mission(other, idempotency_key="retry-key-1")


def test_state_machine_and_append_only_audit(store):
    check_transition(MissionState.VERIFYING, MissionState.RUNNING)
    for current, target in [
        (MissionState.CREATED, MissionState.COMPLETED),
        (MissionState.COMPLETED, MissionState.RUNNING),
        (MissionState.RUNNING, MissionState.INTEGRATING),
    ]:
        with pytest.raises(ForgeError):
            check_transition(current, target)
    store.audit(None, "probe", {})
    statements = ["UPDATE audit_events SET kind='x'", "DELETE FROM audit_events"]
    if store.backend == "postgres":
        statements.append("TRUNCATE audit_events")
    for statement in statements:
        conn = store._connect()
        try:
            with pytest.raises(Exception, match="append-only"):
                conn.execute(statement)
        finally:
            conn.close()
    assert store.schema_versions() == ["0001_init"]
    assert store.verify_audit_chain()


def test_spec_rejects_production_deploy_and_missing_checks(store, auth_repo):
    with pytest.raises(ForgeError, match="A5"):
        make_spec(store, auth_repo, constraints={"allow_production_deploy": True})
    with pytest.raises(ForgeError, match="checks"):
        make_spec(store, auth_repo, checks=[])


def test_acceptance_gate_rejects_weak_evidence(store, auth_repo):
    spec = make_spec(store, auth_repo)
    good = [
        {"name": "unit", "passed": True, "tree_hash": "t", "agent_role": "qa", "execution_id": "e1"},
        {"name": "heldout", "passed": True, "tree_hash": "t", "agent_role": "qa", "execution_id": "e2"},
    ]
    assert (
        acceptance_gate(
            spec=spec, checks=good, current_tree="t", patch="x", review=APPROVE, reviewer_role="reviewer"
        )
        == []
    )
    forged = [dict(good[0], agent_role="engineer"), dict(good[1], tree_hash="old")]
    reasons = acceptance_gate(
        spec=spec, checks=forged, current_tree="t", patch="", review=None, reviewer_role=None
    )
    assert any("not produced by QA" in r for r in reasons)
    assert any("different tree" in r for r in reasons)
    assert "no change was produced" in reasons and "no independent review" in reasons
    assert acceptance_gate(
        spec=spec, checks=good, current_tree="t", patch="x", review=APPROVE, reviewer_role="engineer"
    ) == ["reviewer is the same role as the implementer"]


def test_api_auth_idempotency_and_project_roots(store, auth_repo, tmp_path):
    token = "t" * 32
    runner = runner_for(store, tmp_path, router([right_fix]))
    app = create_app(runner, token=token, project_roots=[auth_repo.parent], autostart=False)
    auth = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        assert client.get("/v1/health").json()["audit_chain_valid"] is True
        assert client.get("/v1/missions").status_code == 401
        assert client.post("/v1/projects/connect", json={"path": "/etc"}, headers=auth).status_code == 400
        project = client.post("/v1/projects/connect", json={"path": str(auth_repo)}, headers=auth).json()
        body = {
            "project_id": project["id"],
            "objective": "Fix auth bug",
            "environment": "trusted-local",
            "checks": checks(),
        }
        headers = {**auth, "Idempotency-Key": "client-retry-0001"}
        first = client.post("/v1/missions", json=body, headers=headers)
        second = client.post("/v1/missions", json=body, headers=headers)
        assert (first.status_code, second.status_code) == (201, 200)
        assert first.json()["id"] == second.json()["id"]
        changed = client.post("/v1/missions", json={**body, "objective": "Other"}, headers=headers)
        assert changed.status_code == 409
        mission_id = first.json()["id"]
        events = client.get(f"/v1/missions/{mission_id}/events", headers=auth).json()["events"]
        assert events[0]["kind"] == "mission_created"
        assert {a["role"] for a in client.get("/v1/agents", headers=auth).json()["agents"]} >= {
            "architect",
            "engineer",
            "qa",
            "reviewer",
        }
        cancelled = client.post(f"/v1/missions/{mission_id}/cancel", headers=auth).json()
        assert cancelled["state"] == "CANCELLED"
        assert client.get("/v1/missions/msn_missing", headers=auth).status_code == 404


def test_concurrent_writers_keep_audit_chain_linear(store):
    errors = []

    def writer(n):
        try:
            for i in range(15):
                store.audit(None, "concurrency_probe", {"writer": n, "i": i})
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert store.verify_audit_chain()


def test_reopening_store_does_not_reapply_migrations(store):
    again = EngineStore(store.location)
    assert again.schema_versions() == ["0001_init"]
    assert again.verify_audit_chain()


async def test_performance_routing_learns_from_recorded_outcomes(store, auth_repo, tmp_path):
    flaky = ScriptedProvider("flaky", {"architect": [{"nonsense": True}]}, family="x")
    steady = ScriptedProvider("steady", {"architect": [architect], "engineer": [right_fix]}, family="y")
    critic = ScriptedProvider("critic", {"reviewer": [APPROVE]}, family="z")
    rtr = ProviderRouter([flaky, steady, critic], {"architect": ["flaky", "steady"], "reviewer": ["critic"]})
    for i in range(3):
        mission, _ = store.create_mission(
            make_spec(store, auth_repo, objective=f"Fix the auth bug, run {i}.")
        )
        rtr.failures.clear()  # simulate a fresh process: only the durable store remembers
        assert (await runner_for(store, tmp_path, rtr).run(mission["id"]))["state"] == "COMPLETED"
    stats = store.provider_stats()
    assert stats[("flaky", "architect")] == {"succeeded": 0, "failed": 3}
    rtr.failures.clear()
    # Operator preference still says "flaky" first, but measured failure now outranks it:
    # proven-good first, unmeasured (neutral prior) next, proven-bad last.
    assert [p.name for p in rtr.order("architect", stats=stats)] == ["steady", "critic", "flaky"]
    assert [p.name for p in rtr.order("architect")][:2] == ["flaky", "steady"]
    entry = next(p for p in rtr.catalogue(stats) if p["name"] == "flaky")
    assert entry["roles"]["architect"]["failure_rate"] == pytest.approx(0.8)
