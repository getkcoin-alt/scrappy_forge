"""Capability discovery and requests for the terminal-first Scrappy operator.

The broker stores capability metadata and opaque handles only. Raw credentials
belong in OS keychains, environment references, MCP/OAuth providers or external
secret managers; model context should not receive them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from .util import ForgeError, atomic_json

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


def request_store_path(home: Path) -> Path:
    return home / "operator" / "capability_requests.json"


def load_pending_requests(home: Path) -> list[dict]:
    path = request_store_path(home)
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForgeError("Capability request store is malformed") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ForgeError("Capability request store is malformed")
    return value


def append_pending_request(home: Path, request: CapabilityRequest) -> None:
    values = load_pending_requests(home)
    values.append(request.to_dict())
    atomic_json(request_store_path(home), values)


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
            if item.status == "available"
            and (item.kind.lower() == wanted or item.provider.lower() == wanted)
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


def broker_from_settings(settings) -> CapabilityBroker:
    """Build the current capability inventory without reading credential values."""
    broker = CapabilityBroker()
    broker.register(
        Capability(
            capability_id="model-provider",
            kind="inference",
            provider=settings.provider,
            handle=f"capability://inference/{settings.provider}",
            status="available",
            permission_class="red",
            description="Configured model inference provider",
        )
    )
    for name in sorted(settings.mcp):
        broker.register(
            Capability(
                capability_id=f"mcp-{name}",
                kind="mcp",
                provider=name,
                handle=f"capability://mcp/{name}",
                status="available",
                permission_class="red",
                description=f"Configured MCP server: {name}",
            )
        )
    return broker


def unresolved_pending_requests(settings) -> list[dict]:
    """Return only requests not satisfied by the current configured inventory."""
    broker = broker_from_settings(settings)
    unresolved = []
    for item in load_pending_requests(settings.home):
        kind = str(item.get("kind") or "").strip()
        suggested = str(item.get("suggested_provider") or "").strip()
        if kind and broker.find(kind):
            continue
        if suggested and broker.find(suggested):
            continue
        unresolved.append(item)
    return unresolved


__all__ = [
    "Capability",
    "CapabilityBroker",
    "CapabilityRequest",
    "PermissionClass",
    "append_pending_request",
    "broker_from_settings",
    "load_pending_requests",
    "request_store_path",
    "unresolved_pending_requests",
]
