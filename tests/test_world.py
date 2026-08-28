import copy
import json
import time

import pytest

from scrappy_forge.engine import Engine, create_session
from scrappy_forge.util import ForgeError
from scrappy_forge.world import World, delta, parse_lsof, parse_processes, query, validate_snapshot


def test_process_parser_does_not_need_command_lines():
    result = parse_processes(" 11 1 Fri Aug 28 10:11:12 2026 /Applications/Example App\nmalformed\n")
    assert result == [{"pid": 11, "ppid": 1, "started": "Fri Aug 28 10:11:12 2026", "name": "Example App"}]
    assert "argv" not in result[0]


def test_lsof_machine_protocol_parser():
    result = parse_lsof("p11\nf3u\ntIPv4\nPTCP\nn127.0.0.1:8080\nTST=LISTEN\nf4r\ntREG\nn/tmp/project/a.py\n")
    assert result[0]["state"] == "LISTEN" and result[0]["protocol"] == "TCP"
    assert result[1]["name"] == "/tmp/project/a.py"


@pytest.mark.asyncio
async def test_scoped_file_graph_queries_and_deltas(repo):
    (repo / ".env").write_text("secret=never-in-world")
    first = await World(repo).capture(["files"])
    assert validate_snapshot(first)
    assert "never-in-world" not in json.dumps(first)
    assert ".env" not in json.dumps(first)
    result = query(first, kind="file", contains="calculator")
    assert result["total"] == 2
    assert all("content" not in n["attributes"] for n in first["nodes"])
    assert not result["stale"]
    (repo / "calculator.py").write_text("def add(a,b): return a+b\n")
    second = await World(repo).capture(["files"])
    change = delta(first, second)
    assert len(change["nodes"]["changed"]) == 1
    assert not change["nodes"]["added"]


@pytest.mark.asyncio
async def test_graph_neighbor_query_and_staleness(repo):
    snapshot = await World(repo).capture(["files"])
    root = next(n for n in snapshot["nodes"] if n["kind"] == "project")
    result = query(snapshot, node_id=root["id"], relation="contains")
    assert root["id"] in {n["id"] for n in result["nodes"]}
    snapshot["captured_at"] = time.time() - 40
    assert query(snapshot)["stale"]
    with pytest.raises(ForgeError):
        query(snapshot, limit=100000)


@pytest.mark.asyncio
async def test_unsupported_collectors_are_explicit(repo):
    value = await World(repo, platform="unsupported-os").capture(["files", "windows", "network"])
    assert value["collectors"]["windows"]["status"] == "unsupported"
    assert value["collectors"]["processes"]["status"] == "unsupported"


@pytest.mark.asyncio
async def test_macos_window_adapter_shape_with_fixture(repo):
    async def command(argv, cwd, **kwargs):
        return {
            "output": "11 1 Fri Aug 28 10:11:12 2026 /Applications/Editor\n",
            "exit_code": 0,
            "output_limited": False,
            "timed_out": False,
        }

    def reader():
        return [{"number": 7, "pid": 11, "app": "Editor", "bounds": {"X": 1}, "layer": 0}]

    value = await World(repo, platform="darwin", command=command, window_reader=reader).capture(["windows"])
    assert query(value, kind="window")["total"] == 1
    assert any(e["relation"] == "owns_window" for e in value["edges"])
    assert not value["collectors"]["windows"]["titles_collected"]


@pytest.mark.asyncio
async def test_world_tool_cannot_bypass_approval_with_allowlist(repo, settings, store):
    settings.allow_tools += ["world_capture", "world_query"]
    async with Engine(store, settings, create_session(store, settings, repo)) as engine:
        with pytest.raises(ForgeError, match="Permission denied"):
            await engine.registry.call("world_capture", {"scopes": ["files"]})


@pytest.mark.asyncio
async def test_world_tool_snapshot_binding_and_no_argument_mutation(repo, settings, store):
    async def approve(request):
        return True

    async with Engine(store, settings, create_session(store, settings, repo), approve=approve) as engine:
        value = await engine.registry.call("world_capture", {"scopes": ["files"]})
        args = {"snapshot_id": value["snapshot_id"], "kind": "file"}
        before = copy.deepcopy(args)
        result = await engine.registry.call("world_query", args)
        assert result["total"] == 2 and args == before
        with pytest.raises(ForgeError, match="Snapshot changed"):
            await engine.registry.call("world_query", {"snapshot_id": "wrong"})


@pytest.mark.asyncio
async def test_graph_rejects_dangling_edges_and_cross_scope_diff(repo):
    first = await World(repo).capture(["files"])
    invalid = copy.deepcopy(first)
    invalid["edges"][0]["target"] = "nonexistent"
    with pytest.raises(ForgeError, match="edges"):
        validate_snapshot(invalid)
    second = copy.deepcopy(first)
    second["scopes"] = ["processes"]
    with pytest.raises(ForgeError, match="same project"):
        delta(first, second)


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), -1, True])
@pytest.mark.asyncio
async def test_graph_rejects_invalid_timestamps(repo, timestamp):
    snapshot = await World(repo).capture(["files"])
    snapshot["captured_at"] = timestamp
    with pytest.raises(ForgeError, match="metadata"):
        validate_snapshot(snapshot)
