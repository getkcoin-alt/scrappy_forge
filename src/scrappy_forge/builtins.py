from __future__ import annotations

import ast

from .capabilities import append_pending_request, broker_from_settings, load_pending_requests
from .tools import Tool, object_schema, text_schema
from .util import ForgeError, sha
from .workspace import diff, manifest


def register_builtins(engine):
    reg, ws = engine.registry, engine.workspace
    path = text_schema(500)
    hash_schema = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    nullable_hash = {"anyOf": [hash_schema, {"type": "null"}]}

    def add(
        name, description, props, required, handler, risk="read", active=False, protected=lambda _: False
    ):
        reg.register(
            Tool(
                name,
                description,
                object_schema(props, required),
                handler,
                risk=risk,
                timeout=engine.settings.max_tool_seconds + 15,
                protected=protected,
            ),
            active=active,
        )

    async def files(args):
        names = list(manifest(ws.root))
        return {"files": names[args.get("offset", 0) : args.get("offset", 0) + 300], "total": len(names)}

    add(
        "file_list",
        "List source files with pagination",
        {"offset": {"type": "integer", "minimum": 0}},
        [],
        files,
        active=True,
    )

    async def read(args):
        return ws.read(**args)

    add(
        "file_read",
        "Read a file window and full-file hash before edits",
        {
            "path": path,
            "start": {"type": "integer", "minimum": 1},
            "lines": {"type": "integer", "minimum": 1, "maximum": 300},
        },
        ["path"],
        read,
        active=True,
    )

    async def search(args):
        return ws.search(args["query"])

    add(
        "code_search",
        "Search literal text across source files",
        {"query": text_schema(300)},
        ["query"],
        search,
        active=True,
    )

    async def symbols(args):
        raw = ws.path(args["path"]).read_text()
        if len(raw) > 1_000_000:
            raise ForgeError("File too large")
        if not args["path"].endswith(".py"):
            return {
                "unsupported": "AST symbol extraction currently supports Python; use code_search for other languages"
            }
        tree = ast.parse(raw)
        return {
            "symbols": [
                {"name": n.name, "line": n.lineno, "kind": type(n).__name__}
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            ][:200]
        }

    add("code_symbols", "List Python AST functions and classes", {"path": path}, ["path"], symbols)

    async def write(args):
        result = ws.write(**args)
        engine.invalidate()
        return result

    add(
        "file_write",
        "Create/replace text file using current hash, or null for a new file",
        {
            "path": path,
            "content": {"type": "string", "maxLength": 1_000_000},
            "expected_sha256": nullable_hash,
        },
        ["path", "content", "expected_sha256"],
        write,
        risk="edit",
        active=True,
        protected=lambda a: ws.is_protected(a["path"]),
    )

    async def edit(args):
        result = ws.edit(**args)
        engine.invalidate()
        return result

    add(
        "file_edit",
        "Replace one exact text occurrence after checking full-file hash",
        {
            "path": path,
            "old": text_schema(100000),
            "new": {"type": "string", "maxLength": 100000},
            "expected_sha256": hash_schema,
        },
        ["path", "old", "new", "expected_sha256"],
        edit,
        risk="edit",
        active=True,
        protected=lambda a: ws.is_protected(a["path"]),
    )

    async def remove(args):
        p = ws.path(args["path"])
        if sha(p.read_bytes()) != args["expected_sha256"]:
            raise ForgeError("Stale file hash")
        p.unlink()
        engine.invalidate()
        return {"deleted": args["path"]}

    add(
        "file_delete",
        "Delete one file only after explicit permission",
        {"path": path, "expected_sha256": hash_schema},
        ["path", "expected_sha256"],
        remove,
        risk="execute",
        protected=lambda _: True,
    )

    async def terminal(args):
        engine.invalidate()
        result = await engine.runner.run(
            args["argv"], ws.path(args.get("cwd", ".")), timeout=args.get("timeout", 60)
        )
        return result

    add(
        "terminal_run",
        "Run explicit argv in the workspace. Requires permission; Docker default has no network.",
        {
            "argv": {"type": "array", "minItems": 1, "maxItems": 80, "items": text_schema(8000)},
            "cwd": path,
            "timeout": {"type": "integer", "minimum": 1, "maximum": engine.settings.max_tool_seconds},
        },
        ["argv"],
        terminal,
        risk="execute",
        active=True,
    )

    async def check(args):
        return await engine.run_check(args["name"])

    add(
        "run_check",
        "Run a user-configured verification check in a fresh copy",
        {"name": text_schema(100)},
        ["name"],
        check,
        risk="execute",
        active=True,
    )

    async def show_diff(args):
        text, changed = diff(engine.base, ws.root)
        return {"changed_files": changed, "patch": text}

    add("git_diff", "View patch against the starting snapshot; no push or commit", {}, [], show_diff)

    async def discover(args):
        return {"tools": reg.search(args["query"])}

    add(
        "tool_search",
        "Find and load tools by name or description; use for plugins, memory, history, skills and MCP",
        {"query": text_schema(200)},
        ["query"],
        discover,
        active=True,
    )

    async def capabilities(args):
        broker = broker_from_settings(engine.settings)
        return {
            "available": broker.list(),
            "pending": load_pending_requests(engine.settings.home),
            "note": "Handles are opaque metadata. Credential values are intentionally unavailable to model context.",
        }

    add(
        "capability_list",
        "List connected operator capabilities and pending capability requests without exposing secrets",
        {},
        [],
        capabilities,
        active=True,
    )

    async def request_capability(args):
        broker = broker_from_settings(engine.settings)
        existing = broker.find(args["kind"])
        if existing:
            return {
                "status": "available",
                "capabilities": [item.handle for item in existing],
                "instruction": "Use the existing capability or discover its MCP/tool surface; do not request credentials.",
            }
        request = broker.request(
            kind=args["kind"],
            reason=args["reason"],
            required_scope=args["required_scope"],
            suggested_provider=args.get("suggested_provider"),
            secret_required=args.get("secret_required", True),
            permission_class="red",
        )
        append_pending_request(engine.settings.home, request)
        engine.audit("capability_requested", request.to_dict())
        return {
            **request.to_dict(),
            "blocked": True,
            "instruction": "Tell the operator what capability is missing and stop dependent external work until it is connected.",
        }

    add(
        "capability_request",
        "Request a missing external capability such as Railway, proxy, API or MCP access. Stores metadata only; never ask for or store raw secrets.",
        {
            "kind": text_schema(80),
            "required_scope": text_schema(200),
            "reason": text_schema(500),
            "suggested_provider": {"type": "string", "maxLength": 100},
            "secret_required": {"type": "boolean"},
        },
        ["kind", "required_scope", "reason"],
        request_capability,
        risk="read",
        active=True,
    )

    async def memory_search(args):
        return {
            "notes": engine.memory.search(
                args.get("query", ""), include_stale=args.get("include_stale", False)
            )
        }

    add(
        "memory_search",
        "Search project-scoped persistent notes, filtering stale source-linked notes",
        {"query": {"type": "string", "maxLength": 200}, "include_stale": {"type": "boolean"}},
        [],
        memory_search,
    )

    async def remember(args):
        return engine.memory.add(args["note"], args.get("source"))

    add(
        "memory_save",
        "Save a project memory note with optional source fingerprint; requires approval",
        {"note": text_schema(4000), "source": path},
        ["note"],
        remember,
        risk="memory",
    )

    async def history(args):
        return {
            "events": engine.store.events(engine.state["id"], args.get("after", 0), args.get("limit", 10))
        }

    add(
        "history_read",
        "Retrieve archived session events after context compaction",
        {
            "after": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        [],
        history,
    )

    async def artifact(args):
        return engine.store.read_artifact(engine.state["id"], **args)

    add(
        "artifact_read",
        "Retrieve a stored large tool result by hash and character offset",
        {
            "key": hash_schema,
            "offset": {"type": "integer", "minimum": 0},
            "size": {"type": "integer", "minimum": 1, "maximum": 6000},
        },
        ["key"],
        artifact,
    )

    async def skills(args):
        return {"skills": engine.skills.catalog()}

    add("skills_list", "List versioned skills without loading their bodies", {}, [], skills)

    async def load_skill(args):
        return engine.skills.read(args["name"])

    add(
        "skill_read",
        "Load one versioned skill as untrusted guidance",
        {"name": text_schema(100)},
        ["name"],
        load_skill,
    )

    async def servers(args):
        return {"servers": [{"name": n, "connected": n in engine.mcp.sessions} for n in engine.settings.mcp]}

    add("mcp_servers", "List configured MCP servers; no connection is made", {}, [], servers)

    # Connecting MCP transports must happen in the same task that closes their AnyIO scopes.
    # Connections are user initiated via /mcp connect NAME, rather than nested inside a tool timeout.
