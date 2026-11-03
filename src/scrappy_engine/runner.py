"""The autonomous execution engine: a durable mission state machine.

Each call to ``step`` performs the work for the mission's current state and then
commits exactly one state transition. All progress lives in the store, so a new
process can resume any non-terminal mission after a crash (after ``store.recover()``
has marked interrupted tool calls as uncertain).

The LLM proposes. The policy engine decides. The tool gateway executes. QA and an
independent reviewer produce evidence, and ``acceptance_gate`` alone decides whether
the mission may complete.
"""

from __future__ import annotations

import re
from pathlib import Path

from scrappy_forge.execution import Runner
from scrappy_forge.util import ForgeError, encoded, sha
from scrappy_forge.workspace import tree_hash

from .autonomy import AutonomyLevel, PolicyEngine
from .contracts import MissionSpec
from .intelligence import ROLES, ProviderRouter, check_output
from .states import TERMINAL, MissionState
from .store import EngineStore
from .tools import PermissionDenied, ToolGateway, standard_tools
from .workspaces import MissionWorkspace, prepare

S = MissionState
PROTECTED_PREFIXES = (".github/", ".git/", ".gitlab-ci", ".scrappy/")
MAX_BRIEF_BYTES = 80_000


class BudgetExhausted(ForgeError):
    pass


class ProvidersUnavailable(ForgeError):
    pass


def acceptance_gate(
    *,
    spec: MissionSpec,
    checks: list[dict],
    current_tree: str,
    patch: str,
    review: dict | None,
    reviewer_role: str | None,
    engineer_role: str = "engineer",
) -> list[str]:
    """Return the reasons the mission may NOT be accepted (empty list == accept).

    Pure function over recorded evidence, so it can be tested and audited separately.
    """
    reasons: list[str] = []
    by_name = {c["name"]: c for c in checks}
    for declared in spec.checks:
        record = by_name.get(declared.name)
        if record is None:
            reasons.append(f"check {declared.name} has no execution record")
            continue
        if record.get("agent_role") != "qa":
            reasons.append(f"check {declared.name} evidence was not produced by QA")
        if not record.get("execution_id"):
            reasons.append(f"check {declared.name} evidence has no execution id")
        if not record.get("passed"):
            reasons.append(f"check {declared.name} failed")
        if record.get("tree_hash") != current_tree:
            reasons.append(f"check {declared.name} ran against a different tree than the final patch")
    if not spec.checks:
        reasons.append("no executable acceptance checks declared")
    if not patch.strip():
        reasons.append("no change was produced")
    if review is None:
        reasons.append("no independent review")
    else:
        if reviewer_role == engineer_role:
            reasons.append("reviewer is the same role as the implementer")
        if review.get("verdict") != "approve":
            reasons.append("reviewer requested changes")
        blocking = [f["summary"] for f in review.get("findings", []) if f.get("severity") == "blocking"]
        reasons.extend(f"blocking review finding: {item}" for item in blocking)
    return reasons


class MissionRunner:
    def __init__(
        self,
        store: EngineStore,
        router: ProviderRouter,
        *,
        home: Path,
        runner_factory=None,
        max_steps: int = 60,
    ):
        self.store = store
        self.router = router
        self.home = Path(home)
        self.runner_factory = runner_factory or (
            lambda env: Runner(mode="trusted-local" if env == "trusted-local" else "docker")
        )
        self.max_steps = max_steps

    # ---- helpers -----------------------------------------------------------

    def _spec(self, mission: dict) -> MissionSpec:
        return MissionSpec.from_dict(mission["spec"])

    def _gateway(self, mission: dict, spec: MissionSpec) -> tuple[ToolGateway, MissionWorkspace]:
        ws = MissionWorkspace.from_dict(mission["context"]["workspace"])
        gw = ToolGateway(self.store, mission["id"], PolicyEngine(spec.autonomy_level))
        for tool in standard_tools(ws.work, spec.checks, self.runner_factory(spec.environment)):
            gw.register(tool)
        return gw, ws

    async def _agent(self, mission_id: str, role_name: str, brief: dict, *, avoid_family=None):
        role = ROLES[role_name]
        errors = []
        for provider in self.router.order(role_name, avoid_family=avoid_family):
            mission = self.store.get_mission(mission_id)
            limits = self._spec(mission).constraints
            if mission["model_calls"] >= limits.max_model_calls:
                raise BudgetExhausted(f"model call budget exhausted ({limits.max_model_calls})")
            if mission["cost_usd"] >= limits.budget_limit_usd and limits.budget_limit_usd > 0:
                raise BudgetExhausted(f"monetary budget exhausted (${limits.budget_limit_usd})")
            run_id = self.store.start_agent_run(
                mission_id, role_name, provider.name, mission["attempts"] + 1, sha(encoded(brief))
            )
            try:
                reply = await provider.complete(role, brief)
                output = check_output(role, reply.output)
            except (ForgeError, OSError, TimeoutError) as exc:
                self.store.finish_agent_run(run_id, status="failed", error=str(exc)[:2000])
                self.store.charge(mission_id, model_calls=1)
                self.router.record_failure(provider.name)
                errors.append(f"{provider.name}: {exc}")
                continue
            self.store.finish_agent_run(run_id, status="succeeded", output=output, cost_usd=reply.cost_usd)
            self.store.charge(mission_id, model_calls=1, cost_usd=reply.cost_usd)
            return output, provider
        raise ProvidersUnavailable(
            f"no provider produced a valid {role_name} output: " + "; ".join(errors)[:1500]
        )

    async def _read_files(self, gw: ToolGateway, role: str, paths, budget=MAX_BRIEF_BYTES) -> dict[str, str]:
        files, used = {}, 0
        for path in paths:
            if path in files:
                continue
            try:
                out = await gw.execute(role, "fs.read", {"path": path}, idempotency_key=None)
            except PermissionDenied:
                raise
            except ForgeError:
                continue
            if used + len(out["content"]) > budget:
                continue
            files[path] = out["content"]
            used += len(out["content"])
        return files

    @staticmethod
    def _prioritise(listing: list[str], *texts: str) -> list[str]:
        haystack = "\n".join(texts)

        def rank(path):
            mentioned = path in haystack or Path(path).stem in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", haystack)
            return (not mentioned, not path.endswith((".py", ".ts", ".js", ".go", ".rs", ".java")), path)

        return sorted(listing, key=rank)

    async def _run_checks(self, gw: ToolGateway, spec: MissionSpec, label: str, tree: str) -> list[dict]:
        results = []
        for check in spec.checks:
            key = f"check:{label}:{check.name}:{tree}"
            try:
                out = await gw.execute("qa", "test.run", {"check": check.name}, idempotency_key=key)
            except PermissionDenied:
                raise
            except ForgeError as exc:
                if "already exists" in str(exc):
                    key = f"{key}:retry:{sha(str(exc))[:8]}"
                    out = await gw.execute("qa", "test.run", {"check": check.name}, idempotency_key=key)
                else:
                    raise
            results.append(
                {
                    "name": check.name,
                    "passed": bool(out.get("passed")),
                    "exit_code": out.get("exit_code"),
                    "timed_out": out.get("timed_out"),
                    "mutated": out.get("mutated"),
                    "tree_hash": out.get("tree_hash"),
                    "execution_id": out["execution_id"],
                    "agent_role": "qa",
                    "output_tail": (out.get("output") or "")[-4000:],
                }
            )
        return results

    # ---- state handlers ----------------------------------------------------

    async def _analyze(self, mission: dict, spec: MissionSpec) -> tuple[S, dict, str | None]:
        project = Path(self.store.get_project(spec.project_id)["path"])
        ctx = dict(mission["context"])
        if "workspace" not in ctx:
            ws = prepare(self.home, mission["id"], project)
            ctx["workspace"] = ws.to_dict()
            self.store.update_context(mission["id"], ctx)
            mission = self.store.get_mission(mission["id"])
        gw, ws = self._gateway(mission, spec)
        listing = (await gw.execute("architect", "fs.list", {}))["files"]
        ctx["listing"] = listing[:500]
        if spec.autonomy_level < AutonomyLevel.A2:
            ctx["baseline"] = []
            return S.PLANNING, ctx, "baseline checks skipped: autonomy level below A2"
        try:
            baseline = await self._run_checks(gw, spec, "baseline", tree_hash(ws.work))
        except ForgeError as exc:
            raise ForgeError(f"baseline checks could not execute: {exc}") from exc
        ctx["baseline"] = baseline
        ctx["reproduced"] = any(not item["passed"] for item in baseline)
        note = "baseline failure reproduced" if ctx["reproduced"] else "all baseline checks already pass"
        return S.PLANNING, ctx, note

    async def _plan(self, mission: dict, spec: MissionSpec) -> tuple[S, dict, str | None]:
        ctx = dict(mission["context"])
        gw, _ = self._gateway(mission, spec)
        failing = "\n".join(item["output_tail"] for item in ctx.get("baseline", []) if not item["passed"])
        ordered = self._prioritise(ctx.get("listing", []), spec.objective, failing)
        files = await self._read_files(gw, "architect", ordered)
        brief = {
            "objective": spec.objective,
            "acceptance_criteria": list(spec.acceptance_criteria),
            "repository_files": ctx.get("listing", []),
            "file_contents": files,
            "baseline_checks": ctx.get("baseline", []),
        }
        plan, provider = await self._agent(mission["id"], "architect", brief)
        ctx["plan"] = plan
        ctx["architect_provider"] = provider.name
        self.store.add_decision(
            mission["id"],
            "architect",
            plan["summary"],
            "\n".join(
                [
                    plan.get("root_cause_hypothesis", ""),
                    *plan["steps"],
                    *("risk: " + r for r in plan.get("risks", [])),
                ]
            ),
        )
        self.store.set_tasks(mission["id"], plan["steps"])
        return S.READY, ctx, None

    async def _ready(self, mission: dict, spec: MissionSpec) -> tuple[S, dict, str | None]:
        policy = PolicyEngine(spec.autonomy_level)
        for role, scope in (("engineer", "fs.write"), ("qa", "test.run")):
            decision = policy.decide(role, scope)
            if not decision.allowed:
                return S.BLOCKED, mission["context"], f"preflight: {decision.reason}"
        return S.RUNNING, mission["context"], None

    async def _execute(self, mission: dict, spec: MissionSpec) -> tuple[S, dict, str | None]:
        ctx = dict(mission["context"])
        attempt = mission["attempts"] + 1
        gw, ws = self._gateway(mission, spec)
        plan = ctx["plan"]
        history = ctx.get("history", [])
        feedback = history[-1] if history else None
        failing = "\n".join(c["output_tail"] for c in (feedback or {}).get("checks", []) if not c["passed"])
        ordered = self._prioritise(ctx.get("listing", []), " ".join(plan["files"]), failing)
        files = await self._read_files(gw, "engineer", [*plan["files"], *ordered])
        brief = {
            "objective": spec.objective,
            "acceptance_criteria": list(spec.acceptance_criteria),
            "plan": plan,
            "file_contents": files,
            "attempt": attempt,
            "previous_attempt": feedback,
        }
        change, provider = await self._agent(mission["id"], "engineer", brief)
        edit_errors = []
        for index, edit in enumerate(change["edits"]):
            path = edit["path"]
            if path.startswith(PROTECTED_PREFIXES):
                edit_errors.append(f"{path}: protected path; edit refused")
                continue
            args = {k: v for k, v in edit.items() if k != "op"}
            tool = "fs.write" if edit["op"] == "write" else "fs.replace"
            key = f"edit:{attempt}:{index}:{sha(encoded(args))}"
            try:
                await gw.execute("engineer", tool, args, idempotency_key=key)
            except PermissionDenied:
                raise
            except ForgeError as exc:
                edit_errors.append(f"{path}: {exc}")
        ctx["engineer_provider"] = provider.name
        ctx["engineer_family"] = provider.family
        ctx["current"] = {"attempt": attempt, "summary": change["summary"], "edit_errors": edit_errors}
        self.store.set_task_status(mission["id"], "implemented")
        return (
            S.VERIFYING,
            ctx,
            f"attempt {attempt}: {len(change['edits'])} edit(s), {len(edit_errors)} rejected",
        )

    async def _verify(self, mission: dict, spec: MissionSpec) -> tuple[S, dict, str | None]:
        ctx = dict(mission["context"])
        attempt = mission["attempts"]
        gw, ws = self._gateway(mission, spec)
        tree = tree_hash(ws.work)
        checks = await self._run_checks(gw, spec, f"attempt{attempt}", tree)
        patch = (await gw.execute("director", "git.diff", {"base": str(ws.baseline)}))["patch"]
        review, reviewer = None, None
        if all(c["passed"] for c in checks) and patch.strip():
            brief = {
                "objective": spec.objective,
                "acceptance_criteria": list(spec.acceptance_criteria),
                "patch": patch[:MAX_BRIEF_BYTES],
                "checks": checks,
                "baseline_checks": ctx.get("baseline", []),
                "implementer_summary": ctx["current"]["summary"],
            }
            review, reviewer = await self._agent(
                mission["id"], "reviewer", brief, avoid_family=ctx.get("engineer_family")
            )
        if tree_hash(ws.work) != tree:
            raise ForgeError("workspace changed during verification")
        reasons = acceptance_gate(
            spec=spec,
            checks=checks,
            current_tree=tree,
            patch=patch,
            review=review,
            reviewer_role="reviewer" if review is not None else None,
        )
        record = {
            "attempt": attempt,
            "summary": ctx["current"]["summary"],
            "edit_errors": ctx["current"]["edit_errors"],
            "checks": checks,
            "review": review,
            "reviewer_provider": reviewer.name if reviewer else None,
            "reviewer_independent_family": bool(reviewer and reviewer.family != ctx.get("engineer_family")),
            "gate_failures": reasons,
            "tree_hash": tree,
        }
        if not reasons:
            ctx.setdefault("history", []).append(record)
            ctx["accepted"] = record
            self.store.set_task_status(mission["id"], "verified")
            return S.INTEGRATING, ctx, "acceptance gate passed"
        if attempt > spec.constraints.max_retries:
            ctx.setdefault("history", []).append(record)
            return S.FAILED, ctx, "retry budget exhausted: " + "; ".join(reasons)[:1500]
        if any(not c["passed"] for c in checks):
            debug_brief = {
                "objective": spec.objective,
                "patch": patch[:MAX_BRIEF_BYTES],
                "failing_checks": [c for c in checks if not c["passed"]],
                "edit_errors": ctx["current"]["edit_errors"],
            }
            record["debugger"], _ = await self._agent(mission["id"], "debugger", debug_brief)
        ctx.setdefault("history", []).append(record)
        return S.RUNNING, ctx, "repair: " + "; ".join(reasons)[:1500]

    async def _integrate(self, mission: dict, spec: MissionSpec) -> tuple[S, dict, dict | str | None]:
        ctx = dict(mission["context"])
        gw, ws = self._gateway(mission, spec)
        accepted = ctx["accepted"]
        if tree_hash(ws.work) != accepted["tree_hash"]:
            raise ForgeError("workspace changed after acceptance; refusing to integrate")
        patch = (await gw.execute("director", "git.diff", {"base": str(ws.baseline)}))["patch"]
        artifact = self.store.add_artifact(mission["id"], "patch", patch)
        commit = None
        if ws.kind == "git-worktree" and spec.autonomy_level >= AutonomyLevel.A3:
            commit = (
                await gw.execute(
                    "director",
                    "git.commit",
                    {"message": f"Scrappy mission {mission['id']}\n\n{spec.objective[:1500]}"},
                    idempotency_key=f"commit:{accepted['tree_hash']}",
                )
            )["commit"]
        mission = self.store.get_mission(mission["id"])
        limitations = ["Deployment not performed: no staging adapter is configured in this engine version."]
        if not accepted["reviewer_independent_family"]:
            limitations.append("Reviewer ran on the same model family as the engineer (role-separated only).")
        if not ctx.get("reproduced"):
            limitations.append(
                "Baseline checks already passed; the original failure was not reproduced by them."
            )
        result = {
            "status": "COMPLETED",
            "objective": spec.objective,
            "reproduced": ctx.get("reproduced"),
            "baseline_checks": [
                {k: c[k] for k in ("name", "passed", "execution_id")} for c in ctx["baseline"]
            ],
            "final_checks": [
                {k: c[k] for k in ("name", "passed", "execution_id", "tree_hash")} for c in accepted["checks"]
            ],
            "tree_hash": accepted["tree_hash"],
            "patch_artifact": {"id": artifact["id"], "sha256": artifact["sha256"]},
            "branch": ws.branch,
            "commit": commit,
            "workspace": str(ws.work),
            "review": accepted["review"],
            "reviewer_provider": accepted["reviewer_provider"],
            "attempts": mission["attempts"],
            "model_calls": mission["model_calls"],
            "cost_usd": round(mission["cost_usd"], 6),
            "limitations": limitations,
        }
        return S.COMPLETED, ctx, result

    # ---- driver ------------------------------------------------------------

    async def step(self, mission_id: str) -> S:
        mission = self.store.get_mission(mission_id)
        state = S(mission["state"])
        if state in TERMINAL:
            return state
        if mission["cancel_requested"]:
            self.store.transition(mission_id, S.CANCELLED, reason="cancelled by operator")
            return S.CANCELLED
        if state == S.BLOCKED:
            return state
        if state == S.CREATED:
            self.store.transition(mission_id, S.ANALYZING)
            return S.ANALYZING
        spec = self._spec(mission)
        handler = {
            S.ANALYZING: self._analyze,
            S.PLANNING: self._plan,
            S.READY: self._ready,
            S.RUNNING: self._execute,
            S.VERIFYING: self._verify,
            S.INTEGRATING: self._integrate,
        }.get(state)
        if handler is None:
            self.store.transition(mission_id, S.BLOCKED, reason=f"no handler for {state.value}")
            return S.BLOCKED
        try:
            target, ctx, extra = await handler(mission, spec)
        except BudgetExhausted as exc:
            self.store.transition(mission_id, S.FAILED, reason=str(exc))
            return S.FAILED
        except (ProvidersUnavailable, PermissionDenied) as exc:
            self.store.transition(mission_id, S.BLOCKED, reason=str(exc)[:2000])
            return S.BLOCKED
        except ForgeError as exc:
            if self.store.get_mission(mission_id)["cancel_requested"]:
                self.store.transition(mission_id, S.CANCELLED, reason="cancelled by operator")
                return S.CANCELLED
            self.store.transition(mission_id, S.BLOCKED, reason=str(exc)[:2000])
            return S.BLOCKED
        fields = {"context": ctx}
        reason = extra if isinstance(extra, str) else None
        if isinstance(extra, dict):
            fields["result"] = extra
        if state == S.RUNNING:
            fields["attempts"] = mission["attempts"] + 1
        if target == S.FAILED:
            fields["result"] = {"status": "FAILED", "reason": reason, "history": ctx.get("history", [])}
        self.store.transition(mission_id, target, reason=reason, **fields)
        return target

    async def run(self, mission_id: str) -> dict:
        for _ in range(self.max_steps):
            state = await self.step(mission_id)
            if state in TERMINAL or state == S.BLOCKED:
                break
        else:
            self.store.transition(mission_id, S.BLOCKED, reason="orchestrator step limit reached")
        return self.store.get_mission(mission_id)

    def resume(self, mission_id: str) -> dict:
        mission = self.store.get_mission(mission_id)
        if mission["state"] != S.BLOCKED.value:
            raise ForgeError("Only BLOCKED missions can be resumed")
        return self.store.transition(mission_id, S(mission["blocked_state"]), reason="resumed by operator")
