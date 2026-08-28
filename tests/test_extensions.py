import asyncio
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scrappy_forge.engine import Engine, create_session
from scrappy_forge.extensions import Skills, register_plugins
from scrappy_forge.mcp_client import tool_name
from scrappy_forge.policy import Policy
from scrappy_forge.tools import Registry
from scrappy_forge.util import ForgeError
from scrappy_forge.workspace import Workspace


async def yes(_):
    return True


@pytest.fixture
def skill(tmp_path):
    body = tmp_path / "workflow.md"
    body.write_text("Inspect routes, then run tests.")
    manifest = tmp_path / "skill.json"
    manifest.write_text(
        json.dumps(
            {
                "format": 1,
                "name": "backend",
                "version": "1.0.0",
                "description": "Backend workflow",
                "file": "workflow.md",
            }
        )
    )
    return manifest


def test_skill_lazy_and_versioned(skill):
    skills = Skills([str(skill)])
    catalog = skills.catalog()
    assert catalog[0]["version"] == "1.0.0"
    assert "content_untrusted" not in catalog[0]
    assert "run tests" in skills.read("backend")["content_untrusted"]


def test_changed_skill_fails_closed(skill):
    skills = Skills([str(skill)])
    (skill.parent / "workflow.md").write_text("changed")
    with pytest.raises(ForgeError):
        skills.read("backend")


async def test_command_plugin_no_shell_and_json(repo, tmp_path):
    manifest = tmp_path / "plugin.json"
    manifest.write_text(
        json.dumps(
            {
                "format": 1,
                "name": "sample",
                "version": "1.0.0",
                "tools": [
                    {
                        "name": "echo",
                        "type": "command",
                        "description": "Echo JSON input",
                        "argv": [
                            sys.executable,
                            "-c",
                            "import sys,json; print(json.dumps({'received':json.load(sys.stdin)}))",
                        ],
                        "input_schema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    }
                ],
            }
        )
    )
    policy = Policy("ask", set(), yes, lambda *_: None)
    registry = Registry(policy)
    from scrappy_forge.execution import Runner

    loaded = register_plugins([str(manifest)], registry, Runner("trusted-local"), Workspace(repo))
    assert loaded[0]["version"] == "1.0.0"
    result = await registry.call("plugin_sample_echo", {"value": "hello; rm -rf /"})
    assert result["result"]["received"]["value"] == "hello; rm -rf /"


async def test_changed_plugin_fails_closed(repo, tmp_path):
    path = tmp_path / "plugin.json"
    data = {
        "format": 1,
        "name": "p",
        "version": "1.0.0",
        "tools": [
            {
                "name": "x",
                "type": "command",
                "description": "x",
                "argv": [sys.executable, "-c", "print('{}')"],
                "input_schema": {"type": "object"},
            }
        ],
    }
    path.write_text(json.dumps(data))
    registry = Registry(Policy("ask", set(), yes, lambda *_: None))
    from scrappy_forge.execution import Runner

    register_plugins([str(path)], registry, Runner("trusted-local"), Workspace(repo))
    path.write_text(json.dumps({**data, "version": "1.0.1"}))
    with pytest.raises(ForgeError):
        await registry.call("plugin_p_x", {})


@pytest.fixture
def connector_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"received": body}).encode())

        def do_GET(self):
            requests.append({"path": self.path})
            self.send_response(302)
            self.send_header("Location", "/must-not-follow")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", requests
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


async def test_rest_connector_real_http_and_redirect_refusal(repo, tmp_path, connector_server, monkeypatch):
    from scrappy_forge.execution import Runner

    url, requests = connector_server
    monkeypatch.setenv("FORGE_FIXTURE_AUTH", "Bearer fixture-not-a-real-secret")
    manifest = tmp_path / "http-plugin.json"
    common = {
        "type": "http",
        "allow_loopback": True,
        "headers_env": {"Authorization": "FORGE_FIXTURE_AUTH"},
        "input_schema": {"type": "object"},
    }
    manifest.write_text(
        json.dumps(
            {
                "format": 1,
                "name": "fixture",
                "version": "1.0.0",
                "tools": [
                    {**common, "name": "post", "method": "POST", "url": url + "/echo"},
                    {**common, "name": "redirect", "method": "GET", "url": url + "/redirect"},
                ],
            }
        )
    )
    registry = Registry(Policy("ask", set(), yes, lambda *_: None))
    register_plugins([str(manifest)], registry, Runner("trusted-local"), Workspace(repo))
    result = await registry.call("plugin_fixture_post", {"value": "a literal $(command)"})
    assert not result["isError"]
    assert json.loads(result["body"])["received"]["value"] == "a literal $(command)"
    assert requests[0]["auth"] == "Bearer fixture-not-a-real-secret"
    with pytest.raises(ForgeError, match="redirects are refused"):
        await registry.call("plugin_fixture_redirect", {})
    assert [r["path"] for r in requests] == ["/echo", "/redirect"]


async def test_mcp_stdio_real_transport(store, settings, repo, mcp_script, monkeypatch):
    monkeypatch.setenv("UNBOUND_TEST_SECRET", "not-for-child")
    settings.mcp = {
        "fixture": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(mcp_script), "--transport", "stdio"],
        }
    }
    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, approve=yes) as engine:
        result = await engine.mcp.connect("fixture")
        assert result["connected"]
        echo = tool_name("fixture", "echo")
        called = await engine.registry.call(echo, {"value": "hello"})
        content = json.dumps(called)
        assert "hello" in content
        assert "inherited_secret" in content and "true" not in content.lower()
        resources = tool_name("fixture", "resources_list")
        prompts = tool_name("fixture", "prompts_list")
        engine.registry.search("resources prompts")
        assert "note://intro" in json.dumps(await engine.registry.call(resources, {}))
        assert "greet" in json.dumps(await engine.registry.call(prompts, {}))
        resource = await engine.registry.call(tool_name("fixture", "resource_read"), {"uri": "note://intro"})
        prompt = await engine.registry.call(
            tool_name("fixture", "prompt_get"), {"name": "greet", "arguments": {"person": "Forge"}}
        )
        assert "fixture resource" in json.dumps(resource)
        assert "Hello Forge" in json.dumps(prompt["prompt_untrusted"])


async def test_mcp_connection_failure_rolls_back_and_can_retry(
    store, settings, repo, mcp_script, monkeypatch
):
    settings.mcp = {"fixture": {"command": sys.executable, "args": [str(mcp_script)]}}
    state = create_session(store, settings, repo)
    async with Engine(store, settings, state, approve=yes) as engine:
        original = engine.registry.register
        before = set(engine.registry.tools)
        calls = 0

        def failing_register(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ForgeError("Simulated invalid connector schema")
            return original(*args, **kwargs)

        monkeypatch.setattr(engine.registry, "register", failing_register)
        with pytest.raises(ForgeError, match="invalid connector schema"):
            await engine.mcp.connect("fixture")
        assert set(engine.registry.tools) == before
        assert "fixture" not in engine.mcp.sessions
        monkeypatch.setattr(engine.registry, "register", original)
        assert (await engine.mcp.connect("fixture"))["connected"]
        result = await engine.registry.call(tool_name("fixture", "echo"), {"value": "after retry"})
        assert "after retry" in json.dumps(result)


async def wait_port(port):
    for _ in range(100):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.03)
    raise AssertionError("server did not start")


async def test_mcp_streamable_http_real_transport(store, settings, repo, mcp_script):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(mcp_script),
        "--transport",
        "streamable-http",
        "--port",
        str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": os.environ["PATH"], "HOME": str(repo)},
    )
    try:
        await wait_port(port)
        settings.mcp = {
            "fixture": {"transport": "http", "url": f"http://127.0.0.1:{port}/mcp", "allow_loopback": True}
        }
        state = create_session(store, settings, repo)
        async with Engine(store, settings, state, approve=yes) as engine:
            result = await engine.mcp.connect("fixture")
            assert result["connected"]
            called = await engine.registry.call(tool_name("fixture", "echo"), {"value": "http"})
            assert "http" in json.dumps(called)
    finally:
        proc.terminate()
        await proc.wait()


def test_mcp_tool_names_stable_and_bounded():
    assert tool_name("x", "y") == tool_name("x", "y")
    assert len(tool_name("x" * 100, "y" * 100)) <= 59
