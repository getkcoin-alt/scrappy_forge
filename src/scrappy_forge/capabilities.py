"""Capability discovery and requests for the terminal-first Scrappy operator.

The broker stores capability metadata and opaque handles only. Raw credentials
belong in OS keychains, environment references, MCP/OAuth providers or external
secret managers; model context should not receive them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal
from uuid import uuid4

CapabilityStatus = Literal["available", "missing", "disabled"]
PermissionClass = Literal["green", "amber", "red"]


@dataclass(frozen=True, slots=True)
class Capability:
    capability_id: str
    kind: str
    provider: str
    handle: str
    status: CapabilityStatus
    permission_class: PermissionClass
    description: str


@dataclass(frozen=True, slots=True)
class CapabilityRequest:
    request_id: str
    kind: str
    reason: str
    required_scope: str
    permission_class: PermissionClass
    suggested_provider: str | None
    secret_required: bool
    status: Literal["pending"] = "pending"

    def to_dict(self) -> dict:
        return asdict(self)


class CapabilityBroker:
    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}
        self._requests: dict[str, CapabilityRequest] = {}

    def register(self, capability: Capability) -> None:
        if not capability.capability_id.strip():
            raise ValueError("capability_id is required")
        if not capability.handle.startswith("capability://"):
            raise ValueError("capability handle must use capability:// and remain opaque")
        self._capabilities[capability.capability_id] = capability

    def list(self) -> list[dict]:
        return [asdict(value) for value in sorted(self._capabilities.values(), key=lambda x: x.capability_id)]

    def find(self, kind: str) -> list[Capability]:
        wanted = kind.strip().lower()
        return [
            item
            for item in self._capabilities.values()
            if item.kind.lower() == wanted and item.status == "available"
        ]

    def request(
        self,
        *,
        kind: str,
        reason: str,
        required_scope: str,
        permission_class: PermissionClass = "red",
        suggested_provider: str | None = None,
        secret_required: bool = True,
    ) -> CapabilityRequest:
        for field, value in (("kind", kind), ("reason", reason), ("required_scope", required_scope)):
            if not value.strip():
                raise ValueError(f"{field} is required")
        item = CapabilityRequest(
            request_id=str(uuid4()),
            kind=kind.strip(),
            reason=reason.strip(),
            required_scope=required_scope.strip(),
            permission_class=permission_class,
            suggested_provider=suggested_provider.strip() if suggested_provider else None,
            secret_required=secret_required,
        )
        self._requests[item.request_id] = item
        return item

    def pending(self) -> list[dict]:
        return [asdict(value) for value in self._requests.values()]


__all__ = ["Capability", "CapabilityBroker", "CapabilityRequest", "PermissionClass"]
