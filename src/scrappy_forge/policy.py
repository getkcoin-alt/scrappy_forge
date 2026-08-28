from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from .util import ForgeError, encoded, redact, sha

Approval = Callable[[dict], Awaitable[bool]]


@dataclass
class Policy:
    mode: str
    allow_tools: set[str]
    approve: Approval
    audit: Callable

    async def authorize(
        self, name: str, risk: str, args: dict, *, origin="builtin", protected=False, binding=""
    ):
        request = {
            "tool": name,
            "risk": risk,
            "origin": origin,
            "arguments": redact(args),
            "binding": binding,
            "protected": protected,
        }
        request["request_hash"] = sha(encoded({"name": name, "args": args, "binding": binding}))
        if self.mode == "plan":
            allowed = risk == "read" and origin == "builtin"
        elif risk == "read" and origin == "builtin":
            allowed = True
        elif name in self.allow_tools and not protected:
            allowed = True
        elif self.mode == "accept-edits" and risk == "edit" and origin == "builtin" and not protected:
            allowed = True
        else:
            allowed = await self.approve(request)
        self.audit("approval", {**request, "allowed": allowed})
        if not allowed:
            raise ForgeError(f"Permission denied for {name}; ask the user rather than bypassing the boundary")


async def deny(_):
    return False
