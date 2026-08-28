import json
import subprocess

import pytest

from scrappy_forge.engine import Engine, create_session
from scrappy_forge.apply_changes import apply_verified, preview
from scrappy_forge.util import ForgeError
from scrappy_forge.workspace import tree_hash


class ScriptedRepair:
    """A fixture, never advertised as live LLM intelligence."""

    def __init__(self):
        self.step = 0

    async def complete(self, messages, tools, on_attempt=lambda: None):
        on_attempt()
        self.step += 1
        if self.step == 1:
            name, args = "file_read", {"path": "calculator.py"}
        elif self.step == 2:
            result = json.loads(messages[-1]["content"])
            name, args = (
                "file_edit",
                {
                    "path": "calculator.py",
                    "old": "a - b",
                    "new": "a + b",
                    "expected_sha256": result["sha256"],
                },
            )
        else:
            return {
                "model": "SCRIPTED-NOT-AN-LLM",
                "message": {"role": "assistant", "content": "Changed addition; controller must verify."},
                "usage": {},
            }
        return {
            "model": "SCRIPTED-NOT-AN-LLM",
            "message": {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": str(self.step),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            },
            "usage": {},
        }


async def test_complete_real_fix_flow(store, settings, repo):
    before = tree_hash(repo)
    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, ScriptedRepair()) as engine:
        result = await engine.ask("Fix add. Preserve existing tests.")
        assert result["status"] == "review_ready", result.get("error")
        assert result["verification"][0]["passed"]
        assert not result["baseline_verification"][0]["passed"]
        assert result["verified_hash"] == tree_hash(engine.workspace.root)
        report = json.loads((engine.directory / "report.json").read_text())
        assert report["evidence_current"]
        check = subprocess.run(
            ["git", "apply", "--check", str(engine.directory / "changes.patch")],
            cwd=repo,
            capture_output=True,
        )
        assert check.returncode == 0, check.stderr
    assert tree_hash(repo) == before


async def test_no_checks_unverified(store, settings, repo):
    settings.checks = []
    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, ScriptedRepair()) as engine:
        assert (await engine.ask("Fix add"))["status"] == "unverified"


async def test_stale_evidence_downgraded(store, settings, repo):
    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, ScriptedRepair()) as engine:
        await engine.ask("Fix add")
        (engine.workspace.root / "calculator.py").write_text("def add(a,b): return 999\n")
        engine.export()
        assert engine.state["status"] == "unverified"


async def test_resume_after_failure(store, settings, repo):
    class Broken:
        async def complete(self, *args, **kwargs):
            raise ForgeError("offline")

    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, Broken()) as engine:
        assert (await engine.ask("Fix add"))["status"] == "blocked"
    async with Engine(store, settings, store.load(state["id"]), ScriptedRepair()) as engine:
        assert (await engine.resume())["status"] == "review_ready"
        assert "error" not in engine.state


async def test_failed_export_removes_old_patch(store, settings, repo):
    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, ScriptedRepair()) as engine:
        await engine.ask("Fix add")
        assert (engine.directory / "changes.patch").exists()
        (engine.workspace.root / "calculator.py").write_bytes(b"\x00\xffbinary")
        engine.export()
        assert not (engine.directory / "changes.patch").exists()
        assert engine.state["status"] == "unverified"
        assert json.loads((engine.directory / "report.json").read_text())["export_error"]


async def test_pending_tools_not_replayed(store, settings, repo):
    state = create_session(store, settings, repo)
    state["messages"] = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "unknown", "function": {"name": "terminal_run", "arguments": "{}"}}],
        }
    ]
    store.save(state)
    async with Engine(store, settings, state) as engine:
        assert "NOT replayed" in engine.state["messages"][-1]["content"]
        before = len(engine.state["messages"])
        engine.repair_pending()
        assert len(engine.state["messages"]) == before


async def test_single_writer(store, settings, repo):
    state = create_session(store, settings, repo)
    async with Engine(store, settings, state):
        with pytest.raises(ForgeError):
            async with Engine(store, settings, state):
                pass


async def test_undo_restores_workspace(store, settings, repo):
    async def approve(_):
        return True

    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, ScriptedRepair(), approve=approve) as engine:
        await engine.ask("Fix add")
        await engine.undo()
        assert "a - b" in (engine.workspace.root / "calculator.py").read_text()
        assert engine.state.get("verified_hash") is None


async def test_tool_outputs_offloaded(store, settings, repo):
    from scrappy_forge.tools import Tool, object_schema

    class Big:
        async def complete(self, *args, **kwargs):
            return {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "big", "type": "function", "function": {"name": "big", "arguments": "{}"}}
                    ],
                }
            }

    async def handler(args):
        return {"text": "x" * 20000}

    settings.max_steps = 1
    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, Big()) as engine:
        engine.registry.register(Tool("big", "big", object_schema(), handler))
        result = await engine.ask("Read large output")
        observation = json.loads(result["messages"][-1]["content"])
        assert "artifact" in observation
        assert observation["total_chars"] > 20000


async def test_compaction_during_actual_loop(store, settings, repo):
    class LongConversation:
        def __init__(self):
            self.step = 0

        async def complete(self, messages, tools, on_attempt):
            on_attempt()
            self.step += 1
            if self.step <= 15:
                return {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": str(self.step),
                                "type": "function",
                                "function": {
                                    "name": "file_read",
                                    "arguments": json.dumps({"path": "notes.md"}),
                                },
                            }
                        ],
                    }
                }
            return {"message": {"role": "assistant", "content": "Finished reading."}}

    (repo / "notes.md").write_text("Long source notes. " * 800)
    settings.max_steps = 20
    settings.checks = []
    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, LongConversation()) as engine:
        result = await engine.ask("Read the notes. Do not modify code.")
        assert result["status"] == "answered", result.get("error")
        assert result["compactions"] > 0
        assert "Do not modify code" in result["task"]


async def approve_all(_):
    return True


async def test_verified_apply_changes_original_and_is_idempotent(store, settings, repo):
    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, ScriptedRepair(), approve=approve_all) as engine:
        await engine.ask("Fix addition")
        reviewed = preview(engine)
        result = await apply_verified(engine, expected_patch=reviewed["patch_sha256"])
        assert not result["already_applied"]
        assert tree_hash(repo) == reviewed["candidate_hash"]
        assert "a + b" in (repo / "calculator.py").read_text()
        assert (await apply_verified(engine))["already_applied"]


async def test_apply_refuses_original_drift(store, settings, repo):
    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, ScriptedRepair(), approve=approve_all) as engine:
        await engine.ask("Fix addition")
        (repo / "user-notes.md").write_text("Work added by the user")
        before = tree_hash(repo)
        with pytest.raises(ForgeError, match="Original project changed"):
            await apply_verified(engine)
        assert tree_hash(repo) == before


async def test_apply_requires_verified_patch_and_exact_hash(store, settings, repo):
    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, ScriptedRepair(), approve=approve_all) as engine:
        await engine.ask("Fix addition")
        with pytest.raises(ForgeError, match="patch hash"):
            await apply_verified(engine, expected_patch="0" * 64)
        engine.state.pop("verified_hash")
        with pytest.raises(ForgeError, match="passing configured checks"):
            await apply_verified(engine)
        assert "a - b" in (repo / "calculator.py").read_text()


async def test_apply_rolls_back_on_write_failure(store, settings, repo, monkeypatch):
    import scrappy_forge.apply_changes as changes

    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, ScriptedRepair(), approve=approve_all) as engine:
        await engine.ask("Fix addition")
        (engine.workspace.root / "extra.py").write_text("value = 1\n")
        await engine.verify()
        before = tree_hash(repo)
        real_copy, calls = changes._atomic_copy, 0

        def fail_once(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated disk failure")
            real_copy(source, target)

        monkeypatch.setattr(changes, "_atomic_copy", fail_once)
        with pytest.raises(ForgeError, match="restored"):
            await apply_verified(engine)
        assert tree_hash(repo) == before


async def test_apply_rechecks_after_user_review(store, settings, repo):
    async def change_while_reviewing(request):
        if request["tool"] == "workspace_apply":
            (repo / "calculator.py").write_text("User's concurrent change\n")
        return True

    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, ScriptedRepair(), approve=change_while_reviewing) as engine:
        await engine.ask("Fix addition")
        with pytest.raises(ForgeError, match="changed during review"):
            await apply_verified(engine)
        assert (repo / "calculator.py").read_text() == "User's concurrent change\n"


async def test_interrupted_apply_requires_manual_recovery(store, settings, repo):
    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, ScriptedRepair(), approve=approve_all) as engine:
        await engine.ask("Fix addition")
        engine.state["apply_pending"] = {
            "base_hash": engine.state["base_hash"],
            "candidate_hash": tree_hash(engine.workspace.root),
            "backup": str(engine.base),
        }
        (repo / "calculator.py").write_text("Interrupted partial edit\n")
        before = tree_hash(repo)
        with pytest.raises(ForgeError, match="earlier apply was interrupted"):
            await apply_verified(engine)
        assert tree_hash(repo) == before
