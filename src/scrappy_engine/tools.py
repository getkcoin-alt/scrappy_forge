"""Permission-aware tool gateway: Scrappy's hands.

Every call follows the SE-001 execution contract (tool_id, mission_id, agent role,
validated input, permission scope, timeout, idempotency key, execution id, output,
evidence hash, status). The record is written *before* execution, so a crash leaves
a 'started' row that recovery turns into 'uncertain' instead of pretending nothing
happened. Agents never touch the filesystem or a shell except through this gateway.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from jsonschema import Draft202012Validator

from scrappy_forge.execution import Runner
from scrappy_forge.util import ForgeError, encoded, redact, sha
from scrappy_forge.workspace import Workspace, diff, manifest, tree_hash

from .autonomy import PolicyEngine
from .contracts import CheckSpec
from .store import EngineStore

Handler = Callable[[dict], Awaitable[dict]]


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str
    scope: str
    schema: dict
    handler: Handler
    timeout: float = 60.0
    idempotent: bool = False
    description: str = ""


class PermissionDenied(ForgeError):
    pass


class ToolGateway:
    def __init__(self, store: EngineStore, mission_id: str, policy: PolicyEngine):
        self.store = store
        self.mission_id = mission_id
        self.policy = policy
        self.tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.tool_id in self.tools:
            raise ForgeError(f"Duplicate tool {spec.tool_id}")
        Draft202012Validator.check_schema(spec.schema)
        self.tools[spec.tool_id] = spec

    def catalogue(self) -> list[dict]:
        return [
            {"tool_id": t.tool_id, "scope": t.scope, "idempotent": t.idempotent, "description": t.description}
            for t in sorted(self.tools.values(), key=lambda t: t.tool_id)
        ]

    async def execute(
        self, role: str, tool_id: str, args: dict, *, idempotency_key: str | None = None
    ) -> dict:
        spec = self.tools.get(tool_id)
        if spec is None:
            raise ForgeError(f"Unknown tool {tool_id}")
        if self.store.get_mission(self.mission_id)["cancel_requested"]:
            raise ForgeError("Mission cancellation requested; no new tool executions")
        errors = list(Draft202012Validator(spec.schema).iter_errors(args))
        if errors:
            raise ForgeError(f"{tool_id} arguments invalid: {errors[0].message[:300]}")
        key = idempotency_key or sha(encoded([role, tool_id, args, uuid.uuid4().hex]))
        previous = self.store.find_tool_call(self.mission_id, key)
        if previous is not None:
            if previous["status"] == "succeeded" and spec.idempotent:
                return previous["output"]
            raise ForgeError(
                f"Execution {previous['execution_id']} already exists with status {previous['status']};"
                " reconcile before retrying"
            )

        decision = self.policy.decide(role, spec.scope)
        record = {
            "execution_id": "exe_" + uuid.uuid4().hex,
            "mission_id": self.mission_id,
            "agent_role": role,
            "tool_id": tool_id,
            "scope": spec.scope,
            "arguments": redact(args),
            "idempotency_key": key,
            "status": "started" if decision.allowed else "denied",
            "decision": decision.reason,
            "started_at": time.time(),
        }
        self.store.begin_tool_call(record)
        if not decision.allowed:
            raise PermissionDenied(f"{tool_id} denied: {decision.reason}")
        try:
            async with asyncio.timeout(spec.timeout):
                output = await spec.handler(args)
            status = "succeeded"
        except TimeoutError:
            output, status = {"error": f"timed out after {spec.timeout}s; outcome uncertain"}, "uncertain"
        except (ForgeError, OSError, ValueError, UnicodeDecodeError) as exc:
            output, status = {"error": str(exc)[:2000]}, "failed"
        output = {**output, "execution_id": record["execution_id"]}
        evidence = sha(encoded(output))
        self.store.finish_tool_call(
            record["execution_id"], status=status, output=output, evidence_hash=evidence
        )
        if status != "succeeded":
            raise ForgeError(f"{tool_id} {status}: {output.get('error')}")
        return output


_PATH = {"type": "string", "minLength": 1, "maxLength": 400}


def _obj(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def standard_tools(workspace_root: Path, checks: tuple[CheckSpec, ...], runner: Runner) -> list[ToolSpec]:
    """Filesystem, test and git tools bound to one isolated mission workspace."""
    ws = Workspace(workspace_root)
    by_name = {c.name: c for c in checks}

    async def fs_list(_args):
        return {"files": sorted(manifest(ws.root))[:2000], "tree_hash": tree_hash(ws.root)}

    async def fs_read(args):
        p = ws.path(args["path"])
        raw = p.read_bytes()
        if len(raw) > 400_000:
            raise ForgeError("File too large for fs.read")
        return {"path": args["path"], "sha256": sha(raw), "content": raw.decode("utf-8")}

    async def fs_write(args):
        p = ws.path(args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args["content"], encoding="utf-8")
        return {"path": args["path"], "sha256": sha(p.read_bytes())}

    async def fs_replace(args):
        p = ws.path(args["path"])
        text = p.read_text(encoding="utf-8")
        count = text.count(args["old"])
        if count == 1:
            p.write_text(text.replace(args["old"], args["new"]), encoding="utf-8")
            applied = True
        elif count == 0 and args["new"] and args["new"] in text:
            applied = False  # already applied (e.g. replay after a crash)
        else:
            raise ForgeError(f"replace target must occur exactly once in {args['path']} (found {count})")
        return {"path": args["path"], "applied": applied, "sha256": sha(p.read_bytes())}

    async def test_run(args):
        check = by_name.get(args["check"])
        if check is None:
            raise ForgeError("Only checks declared in the mission spec may run")
        # Runs against a fresh copy of the workspace; mutation of the copy fails the check.
        result = await runner.verify(
            ws.root, {"name": check.name, "argv": list(check.argv), "timeout": check.timeout}
        )
        result["output"] = result.get("output", "")[-20000:]
        return result

    async def git_diff(args):
        base = Path(args["base"])
        text, files = await asyncio.to_thread(diff, base, ws.root)
        return {"patch": text, "files": files, "tree_hash": tree_hash(ws.root)}

    async def git_commit(args):
        if not (ws.root / ".git").exists():
            raise ForgeError("Workspace is not a git worktree")

        def run(*argv):
            return subprocess.run(["git", *argv], cwd=ws.root, capture_output=True, text=True, timeout=60)

        run("add", "-A")
        done = run(
            "-c",
            "user.name=Scrappy Engine",
            "-c",
            "user.email=engine@scrappy.invalid",
            "commit",
            "--no-verify",
            "-m",
            args["message"],
        )
        if done.returncode != 0:
            raise ForgeError("git commit failed: " + (done.stderr or done.stdout)[-1000:])
        head = run("rev-parse", "HEAD").stdout.strip()
        return {"commit": head}

    return [
        ToolSpec(
            "fs.list", "fs.read", _obj({}), fs_list, idempotent=True, description="List workspace files"
        ),
        ToolSpec(
            "fs.read",
            "fs.read",
            _obj({"path": _PATH}, ["path"]),
            fs_read,
            idempotent=True,
            description="Read a UTF-8 file",
        ),
        ToolSpec(
            "fs.write",
            "fs.write",
            _obj({"path": _PATH, "content": {"type": "string", "maxLength": 400000}}, ["path", "content"]),
            fs_write,
            idempotent=True,
            description="Write a whole file",
        ),
        ToolSpec(
            "fs.replace",
            "fs.write",
            _obj(
                {"path": _PATH, "old": {"type": "string", "minLength": 1}, "new": {"type": "string"}},
                ["path", "old", "new"],
            ),
            fs_replace,
            idempotent=True,
            description="Replace one exact occurrence",
        ),
        ToolSpec(
            "test.run",
            "test.run",
            _obj({"check": {"type": "string", "minLength": 1, "maxLength": 64}}, ["check"]),
            test_run,
            timeout=max([c.timeout for c in checks] + [60]) + 30,
            description="Run a mission-declared check on a fresh copy",
        ),
        ToolSpec(
            "git.diff",
            "fs.read",
            _obj({"base": {"type": "string", "minLength": 1}}, ["base"]),
            git_diff,
            idempotent=True,
            description="Patch between baseline snapshot and workspace",
        ),
        ToolSpec(
            "git.commit",
            "git.commit",
            _obj({"message": {"type": "string", "minLength": 1, "maxLength": 2000}}, ["message"]),
            git_commit,
            description="Commit inside the mission worktree",
        ),
    ]
