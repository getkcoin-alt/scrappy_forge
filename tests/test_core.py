import asyncio
import os
import subprocess
import sys

import pytest

from scrappy_forge.context import ContextManager, TokenBudget, message_groups
from scrappy_forge.config import Settings
from scrappy_forge.engine import create_session
from scrappy_forge.execution import Runner, process
from scrappy_forge.memory import Memory
from scrappy_forge.policy import Policy, deny
from scrappy_forge.tools import Registry, Tool, object_schema
from scrappy_forge.util import ForgeError, clean, sha
from scrappy_forge.workspace import Workspace, diff, snapshot, tree_hash


@pytest.mark.parametrize("name", ["../secret", "/etc/passwd", ".env", ".git/config", "a/../../x", "x\\y"])
def test_path_boundary(repo, name):
    with pytest.raises(ForgeError):
        Workspace(repo).path(name)


def test_symlink_boundary(repo, tmp_path):
    (repo / "link").symlink_to(tmp_path)
    with pytest.raises(ForgeError):
        Workspace(repo).path("link/something")


def test_protected_normalized(repo):
    ws = Workspace(repo, ["tests/*"])
    assert ws.is_protected("./tests//test_calculator.py")


def test_edit_requires_hash_and_unique_text(repo):
    ws = Workspace(repo)
    data = ws.read("calculator.py")
    with pytest.raises(ForgeError):
        ws.edit("calculator.py", "a - b", "a + b", "0" * 64)
    ws.edit("calculator.py", "a - b", "a + b", data["sha256"])
    assert "a + b" in ws.read("calculator.py")["content"]


def test_snapshot_excludes_secrets_and_preserves_modes(repo, tmp_path):
    (repo / ".env").write_text("secret")
    (repo / ".mcp.json").write_text('{"token":"fixture-secret"}')
    (repo / "calculator.py").chmod(0o755)
    snapshot(repo, tmp_path / "copy")
    assert not (tmp_path / "copy/.env").exists()
    assert not (tmp_path / "copy/.mcp.json").exists()
    assert (tmp_path / "copy/calculator.py").stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("new", ["x = 1", "x = 1\n"])
def test_patch_applies_for_new_file(repo, tmp_path, new):
    copy = tmp_path / "copy"
    snapshot(repo, copy)
    Workspace(copy).write("new.py", new, None)
    patch, changed = diff(repo, copy)
    p = tmp_path / "changes.patch"
    p.write_text(patch)
    result = subprocess.run(["git", "apply", "--check", str(p)], cwd=repo, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert changed == ["new.py"]


def test_project_memory_isolation_and_invalidation(store, repo, tmp_path):
    m = Memory(store, str(repo), Workspace(repo))
    saved = m.add("Addition uses calculator.py", "calculator.py", origin="user")
    assert m.search("Addition")[0]["id"] == saved["id"]
    assert store.memories(str(tmp_path / "other"), "Addition") == []
    (repo / "calculator.py").write_text("changed")
    assert m.search("Addition") == []
    assert m.search("Addition", include_stale=True)[0]["stale"]


def test_forget_scoped_to_project(store, repo):
    mid = store.remember(str(repo), "A note", None, None, "user")
    store.forget("another-project", mid)
    assert store.memories(str(repo))
    store.forget(str(repo), mid)
    assert not store.memories(str(repo))


def test_journal_roundtrip(store, settings, repo):
    state = create_session(store, settings, repo)
    eid = store.event(state["id"], "tool", {"hello": "world"})
    assert store.events(state["id"])[0]["id"] == eid
    key = store.artifact(state["id"], {"big": "x" * 10000})
    assert store.read_artifact(state["id"], key, offset=1, size=20)["total_chars"] > 10000
    with pytest.raises(ForgeError):
        store.read_artifact(state["id"], "../secret")


def test_compaction_preserves_pairs_and_task(store, settings, repo):
    state = create_session(store, settings, repo, "Never change authentication")
    for i in range(30):
        state["messages"] += [
            {
                "role": "assistant",
                "tool_calls": [{"id": str(i), "function": {"name": "file_read", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": str(i), "content": "x" * 300},
        ]
    cm = ContextManager(TokenBudget(6000, 512), store)
    old = len(state["messages"])
    context = cm.build(state, "Policy", [], [])
    assert len(state["messages"]) < old
    assert state["compactions"] > 0
    assert "Never change authentication" in context[1]["content"]
    message_groups(state["messages"])


def test_pending_call_cannot_be_compacted():
    with pytest.raises(ForgeError):
        message_groups([{"role": "assistant", "tool_calls": [{"id": "pending"}]}])


def test_token_estimate_calibration():
    budget = TokenBudget(10000, 1000)
    before = budget.estimate([{"content": "x" * 100}])
    budget.observe([{"content": "x" * 100}], [], 200)
    assert budget.estimate([{"content": "x" * 100}]) > before


def test_oversized_pinned_task_stops(store, settings, repo):
    state = create_session(store, settings, repo, "x" * 30000)
    cm = ContextManager(TokenBudget(3000, 512), store)
    with pytest.raises(ForgeError):
        cm.build(state, "Policy", [], [])


async def test_read_builtin_auto_but_external_does_not():
    policy = Policy("ask", set(), deny, lambda *_: None)
    await policy.authorize("read", "read", {})
    with pytest.raises(ForgeError):
        await policy.authorize("remote", "read", {}, origin="mcp:untrusted")


async def test_plan_denies_even_explicit_allow():
    policy = Policy("plan", {"shell"}, deny, lambda *_: None)
    with pytest.raises(ForgeError):
        await policy.authorize("shell", "execute", {})


async def test_protected_edits_need_approval():
    policy = Policy("accept-edits", set(), deny, lambda *_: None)
    await policy.authorize("file_write", "edit", {})
    with pytest.raises(ForgeError):
        await policy.authorize("file_write", "edit", {}, protected=True)


async def test_schema_validated_before_handler():
    reg = Registry(Policy("ask", set(), deny, lambda *_: None))
    calls = []

    async def handler(args):
        calls.append(args)
        return args

    reg.register(Tool("echo", "echo", object_schema({"n": {"type": "integer"}}, ["n"]), handler))
    with pytest.raises(ForgeError):
        await reg.call("echo", {"n": "bad"})
    assert not calls
    assert await reg.call("echo", {"n": 1}) == {"n": 1}


def test_external_schema_refs_denied():
    reg = Registry(Policy("ask", set(), deny, lambda *_: None))
    with pytest.raises(ForgeError):
        reg.register(Tool("bad", "bad", {"$ref": "https://example.com/schema"}, None))


def test_lazy_discovery():
    reg = Registry(Policy("ask", set(), deny, lambda *_: None))
    for n in range(50):
        reg.register(Tool(f"plugin_{n}", f"service {n}", object_schema(), None, origin="plugin:test"))
    assert reg.specs() == []
    assert len(reg.search("service")) == 4
    assert len(reg.specs()) == 4


async def test_real_subprocess_without_api_secret(repo, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "should-not-be-inherited")
    result = await process(
        [sys.executable, "-c", "import os; print(os.getenv('OPENROUTER_API_KEY','absent'))"], repo
    )
    assert result["output"].strip() == "absent"


async def test_timeout_kills_process(repo):
    result = await process([sys.executable, "-c", "import time; time.sleep(30)"], repo, timeout=0.1)
    assert result["timed_out"]


async def test_cancellation_reaps_process(repo):
    pid_file = repo / "child.pid"
    task = asyncio.create_task(
        process(
            [
                sys.executable,
                "-c",
                "import os,time; open('child.pid','w').write(str(os.getpid())); time.sleep(30)",
            ],
            repo,
        )
    )
    try:
        async with asyncio.timeout(3):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
        pid = int(pid_file.read_text())
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_output_budget(repo):
    result = await process([sys.executable, "-c", "print('x'*500000)"], repo, max_output=1000)
    assert result["output_limited"]
    assert len(result["output"]) <= 1000


async def test_verifier_detects_mutation(repo):
    before = tree_hash(repo)
    result = await Runner("trusted-local").verify(
        repo, {"name": "mutate", "argv": [sys.executable, "-c", "open('calculator.py','w').write('bad')"]}
    )
    assert not result["passed"]
    assert result["mutated"]
    assert tree_hash(repo) == before


async def test_no_docker_no_fallback(repo, monkeypatch):
    monkeypatch.setattr(shutil_module(), "which", lambda _: None)
    with pytest.raises(ForgeError):
        await Runner().run(["echo", "hello"], repo)


def shutil_module():
    import scrappy_forge.execution as execution

    return execution.shutil


def test_terminal_controls_removed():
    assert clean("safe\x1b[31mred\x1b[0m\x00") == "safered"


def test_atomic_hash_matches(repo):
    ws = Workspace(repo)
    ws.write("new.py", "x=1\n", None)
    assert ws.read("new.py")["sha256"] == sha("x=1\n")


@pytest.mark.parametrize("timeout", [0, 301, True, "10", None])
def test_invalid_check_timeouts_rejected(timeout):
    settings = Settings(checks=[{"name": "unit", "argv": ["python"], "timeout": timeout}])
    with pytest.raises(ForgeError):
        settings.validate()
