from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from .util import ForgeError, encoded, redact, sha

Approval = Callable[[dict], Awaitable[bool]]
PermissionClass = Literal["green", "amber", "red"]


def permission_class(*, risk: str, origin: str = "builtin", protected: bool = False) -> PermissionClass:
    """Map existing tool risk metadata into the operator's three permission classes."""

    if protected:
        return "red"
    if risk == "read" and origin == "builtin":
        return "green"
    if risk in {"edit", "memory"} and origin == "builtin":
        return "amber"
    if risk in {"execute", "external"} or origin != "builtin":
        return "red"
    return "red"


@dataclass
class Policy:
    mode: str
    allow_tools: set[str]
    approve: Approval
    audit: Callable

    async def authorize(
        self, name: str, risk: str, args: dict, *, origin="builtin", protected=False, binding=""
    ):
        tier = permission_class(risk=risk, origin=origin, protected=protected)
        request = {
            "tool": name,
            "risk": risk,
            "permission_class": tier,
            "origin": origin,
            "arguments": redact(args),
            "binding": binding,
            "protected": protected,
        }
        request["request_hash"] = sha(encoded({"name": name, "args": args, "binding": binding}))
        if self.mode == "plan":
            allowed = tier == "green"
        elif tier == "green":
            allowed = True
        elif name in self.allow_tools and not protected:
            allowed = True
        elif self.mode == "accept-edits" and tier == "amber" and risk == "edit" and not protected:
            allowed = True
        else:
            allowed = await self.approve(request)
        self.audit("approval", {**request, "allowed": allowed})
        if not allowed:
            raise ForgeError(f"Permission denied for {name}; ask the user rather than bypassing the boundary")


async def deny(_):
    return False


__all__ = ["Policy", "PermissionClass", "deny", "permission_class"]
