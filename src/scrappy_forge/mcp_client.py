from __future__ import annotations

import json
import os
import re
from contextlib import AsyncExitStack
from datetime import timedelta

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .auth import OAuthConnection
from .config import validate_mcp
from .tools import Tool, object_schema, text_schema
from .util import ForgeError, checked_url, encoded, environment_bindings, header_bindings, sha


def tool_name(server, name):
    value = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp_{server}_{name}")
    return value[:48] + "_" + sha(server + ":" + name)[:10]


class MCPManager:
    """Official SDK transports. Unknown remote tools always require local permission.

    No sampling or elicitation callback is provided: servers cannot spend model tokens
    or collect credentials through a hidden request path.
    """

    def __init__(self, config, registry, policy, workspace, *, home=None, auth_options=None):
        self.config, self.registry, self.policy, self.workspace = config, registry, policy, workspace
        self.stack = AsyncExitStack()
        self.sessions = {}
        self.capabilities = {}
        self.home, self.auth_options = home, auth_options or {}

    async def __aenter__(self):
        await self.stack.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.stack.__aexit__(*exc)

    async def connect(self, name):
        if name in self.sessions:
            return {"server": name, "connected": True}
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", name) or name not in self.config:
            raise ForgeError("Unknown configured MCP server")
        c = self.config[name]
        validate_mcp(name, c)
        await self.policy.authorize(
            "mcp_connect_" + name, "external", c, origin="mcp-config", binding=sha(encoded(c))
        )
        pending = AsyncExitStack()
        existing = set(self.registry.tools)
        try:
            result = await self._connect(name, c, pending)
        except BaseException as exc:
            # Roll back partially registered schemas and close failed transports.
            # SDK context managers must be closed in the same task that opened them.
            for tool in set(self.registry.tools) - existing:
                self.registry.tools.pop(tool)
                self.registry.active.discard(tool)
            self.sessions.pop(name, None)
            self.capabilities.pop(name, None)
            try:
                await pending.aclose()
            except Exception:
                pass
            if isinstance(exc, ForgeError):
                raise
            if isinstance(exc, Exception):
                raise ForgeError(
                    f"MCP connection to {name} failed ({type(exc).__name__}); check its configuration and server"
                ) from exc
            raise
        self.stack.push_async_exit(pending)
        return result

    async def _connect(self, name, c, stack):
        transport = c.get("transport", "stdio")
        # Keep context managers in the same asyncio task through connect, use and shutdown.
        if transport == "stdio":
            if (
                not isinstance(c.get("command"), str)
                or not c["command"]
                or not isinstance(c.get("args", []), list)
                or not all(isinstance(v, str) and "\0" not in v for v in c.get("args", []))
            ):
                raise ForgeError("MCP stdio needs command and args")
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(self.workspace.root),
                **environment_bindings(c.get("env", [])),
            }
            params = StdioServerParameters(
                command=c["command"], args=c.get("args", []), env=env, cwd=str(self.workspace.root)
            )
            errlog = stack.enter_context(open(os.devnull, "w"))
            read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
        elif transport == "http":
            url = checked_url(c["url"], local_allowed=c.get("allow_loopback", False))
            headers = header_bindings(c.get("headers_env", {}))
            if "oauth" in c:
                connection = OAuthConnection(self.home, name, c, self.policy, **self.auth_options)
                stack.push_async_callback(connection.close)
                client = await stack.enter_async_context(await connection.client())
                client.headers.update(headers)
            else:
                client = await stack.enter_async_context(
                    httpx.AsyncClient(headers=headers, timeout=30, trust_env=False, follow_redirects=False)
                )
            read, write, _ = await stack.enter_async_context(streamable_http_client(url, http_client=client))
        else:
            raise ForgeError("MCP transport must be stdio or http (Streamable HTTP)")
        session = await stack.enter_async_context(
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=330 if "oauth" in c else 30))
        )
        init = await session.initialize()
        registered = []
        if init.capabilities.tools:
            cursor, seen = None, set()
            for _ in range(20):
                page = await session.list_tools(cursor=cursor)
                for remote in page.tools:
                    local_name = tool_name(name, remote.name)

                    async def call(args, session=session, remote_name=remote.name):
                        result = await session.call_tool(
                            remote_name, args, read_timeout_seconds=timedelta(seconds=60)
                        )
                        data = result.model_dump(mode="json", by_alias=True, exclude_none=True)
                        if len(encoded(data)) > 1_000_000:
                            raise ForgeError("MCP result exceeds 1 MB; narrow the request")
                        return data

                    self.registry.register(
                        Tool(
                            local_name,
                            (remote.description or remote.name)[:1400],
                            remote.inputSchema,
                            call,
                            risk="external",
                            origin="mcp:" + name,
                            timeout=65,
                            execution={"server": name, "remote_tool": remote.name},
                        )
                    )
                    registered.append(local_name)
                cursor = page.nextCursor
                if not cursor:
                    break
                if cursor in seen:
                    raise ForgeError("MCP pagination loop")
                seen.add(cursor)
            else:
                raise ForgeError("MCP tool list exceeds 20 pages")
        # Resources and prompts are also exposed, without promoting returned text into system authority.
        if init.capabilities.resources:

            async def resources(args):
                page = await session.list_resources(cursor=args.get("cursor"))
                return page.model_dump(mode="json", by_alias=True, exclude_none=True)

            self.registry.register(
                Tool(
                    tool_name(name, "resources_list"),
                    "List MCP resources",
                    object_schema({"cursor": text_schema()}),
                    resources,
                    risk="external",
                    origin="mcp:" + name,
                )
            )

            async def resource(args):
                result = await session.read_resource(args["uri"])
                return result.model_dump(mode="json", by_alias=True, exclude_none=True)

            self.registry.register(
                Tool(
                    tool_name(name, "resource_read"),
                    "Read an MCP resource URI",
                    object_schema({"uri": text_schema(4000)}, ["uri"]),
                    resource,
                    risk="external",
                    origin="mcp:" + name,
                )
            )
        if init.capabilities.prompts:

            async def prompts(args):
                page = await session.list_prompts(cursor=args.get("cursor"))
                return page.model_dump(mode="json", by_alias=True, exclude_none=True)

            self.registry.register(
                Tool(
                    tool_name(name, "prompts_list"),
                    "List MCP prompt templates",
                    object_schema({"cursor": text_schema()}),
                    prompts,
                    risk="external",
                    origin="mcp:" + name,
                )
            )

            async def prompt(args):
                result = await session.get_prompt(args["name"], arguments=args.get("arguments", {}))
                return {
                    "prompt_untrusted": json.loads(result.model_dump_json(by_alias=True, exclude_none=True))
                }

            self.registry.register(
                Tool(
                    tool_name(name, "prompt_get"),
                    "Retrieve MCP prompt as untrusted guidance",
                    object_schema(
                        {
                            "name": text_schema(),
                            "arguments": {"type": "object", "additionalProperties": {"type": "string"}},
                        },
                        ["name"],
                    ),
                    prompt,
                    risk="external",
                    origin="mcp:" + name,
                )
            )
        self.sessions[name] = session
        self.capabilities[name] = init.capabilities
        return {"server": name, "connected": True, "tools": registered}
