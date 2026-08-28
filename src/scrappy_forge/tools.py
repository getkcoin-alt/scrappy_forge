from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from jsonschema import Draft202012Validator, SchemaError

from .util import ForgeError, encoded, sha


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict], Awaitable[dict]]
    risk: str = "read"
    origin: str = "builtin"
    timeout: int = 60
    protected: Callable[[dict], bool] = lambda _: False
    execution: dict = field(default_factory=dict)

    def spec(self):
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.schema},
        }


def object_schema(properties=None, required=()):
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }


def text_schema(maximum=1000):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


class Registry:
    def __init__(self, policy, before_write=None):
        self.tools: dict[str, Tool] = {}
        self.active: set[str] = set()
        self.policy = policy
        self.before_write = before_write

    def register(self, tool: Tool, *, active=False):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.name) or tool.name in self.tools:
            raise ForgeError("Invalid or duplicate tool name")
        if tool.risk not in {"read", "edit", "execute", "external", "memory"}:
            raise ForgeError("Invalid tool risk")
        if len(encoded(tool.schema)) > 24000 or len(tool.description) > 1500:
            raise ForgeError("Tool metadata exceeds size budget")

        def references(value):
            if isinstance(value, dict):
                for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                    if key in value and (not isinstance(value[key], str) or not value[key].startswith("#")):
                        raise ForgeError("Remote schema references are not allowed")
                for v in value.values():
                    references(v)
            elif isinstance(value, list):
                for v in value:
                    references(v)

        references(tool.schema)
        try:
            Draft202012Validator.check_schema(tool.schema)
        except SchemaError as exc:
            raise ForgeError("Invalid tool JSON Schema") from exc
        self.tools[tool.name] = tool
        if active:
            self.active.add(tool.name)

    def specs(self):
        return [self.tools[n].spec() for n in sorted(self.active)]

    def search(self, query: str, limit=4):
        words = set(re.findall(r"\w+", query.lower()))

        def rank(t):
            hay = (t.name + " " + t.description).lower()
            return sum(w in hay for w in words)

        matches = sorted(self.tools.values(), key=lambda t: (-rank(t), t.name))
        selected = [t for t in matches if rank(t) > 0][:limit]
        # Keep all built-ins; external schemas have a small working set.
        external = [n for n in self.active if self.tools[n].origin != "builtin"]
        for n in external:
            self.active.discard(n)
        for tool in selected:
            self.active.add(tool.name)
        return [
            {
                "name": t.name,
                "description": t.description,
                "risk": t.risk,
                "origin": t.origin,
                "schema": t.schema,
            }
            for t in selected
        ]

    async def call(self, name: str, args: dict):
        if name not in self.tools:
            raise ForgeError("Unknown tool; use tool_search")
        tool = self.tools[name]
        errors = list(Draft202012Validator(tool.schema).iter_errors(args))
        if errors:
            raise ForgeError("Tool arguments failed JSON Schema validation: " + errors[0].message[:300])
        binding = sha(
            encoded(
                {
                    "schema": tool.schema,
                    "description": tool.description,
                    "origin": tool.origin,
                    "execution": tool.execution,
                }
            )
        )
        approval_args = {"input": args, "execution": tool.execution} if tool.execution else args
        await self.policy.authorize(
            name,
            tool.risk,
            approval_args,
            origin=tool.origin,
            protected=tool.protected(args),
            binding=binding,
        )
        if tool.risk in {"edit", "execute"} and self.before_write:
            await self.before_write(name, args)
        try:
            async with asyncio.timeout(tool.timeout):
                return await tool.handler(args)
        except TimeoutError as exc:
            raise ForgeError(
                f"Tool {name} timed out; outcome may be uncertain. Inspect before retrying."
            ) from exc
