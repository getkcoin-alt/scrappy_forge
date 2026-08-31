"""Canonical Scrappy node registry.

Historical names are retained as lineage metadata, but runtime code consumes
only typed roles, ownership, status and authority. A symbolic name never grants
permissions and never proves that a process is online.
"""

from __future__ import annotations

import json
from pathlib import Path

from .util import ForgeError

SCHEMA = "scrappy-intelligence-nodes.v1"


def registry_path() -> Path:
    return Path(__file__).resolve().parents[2] / "platform" / "intelligence-nodes.json"


def load_registry(path: Path | None = None) -> dict:
    target = (path or registry_path()).resolve()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForgeError(f"Cannot load node registry: {target}") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ForgeError("Unsupported Scrappy node registry schema")
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ForgeError("Node registry must contain nodes[]")
    seen: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ForgeError("Node registry entries must be objects")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            raise ForgeError("Every node requires an id")
        if node_id in seen:
            raise ForgeError(f"Duplicate node id: {node_id}")
        seen.add(node_id)
        for required in ("name", "class", "status", "role", "owner_component", "authority"):
            if not isinstance(node.get(required), str) or not node[required].strip():
                raise ForgeError(f"Node {node_id} requires {required}")
    return value


def list_nodes(*, status: str | None = None, owner: str | None = None) -> list[dict]:
    nodes = load_registry()["nodes"]
    if status:
        nodes = [node for node in nodes if node["status"] == status]
    if owner:
        nodes = [node for node in nodes if node["owner_component"] == owner]
    return sorted(nodes, key=lambda node: node["id"])


def get_node(name_or_id: str) -> dict:
    wanted = name_or_id.strip().lower()
    if not wanted:
        raise ForgeError("Node name/id is required")
    for node in load_registry()["nodes"]:
        names = {node["id"].lower(), node["name"].lower()}
        names.update(str(alias).lower() for alias in node.get("aliases", []))
        if wanted in names:
            return node
    raise ForgeError(f"Unknown Scrappy node: {name_or_id}")


def intelligence_summary() -> dict:
    registry = load_registry()
    nodes = registry["nodes"]
    return {
        "schema": registry["schema"],
        "mission": registry["mission"],
        "node_count": len(nodes),
        "implemented_or_mapped": sum(
            1
            for node in nodes
            if node["status"].startswith("implemented") or node["status"].endswith("mapped")
        ),
        "future": [node["id"] for node in nodes if node["status"].startswith("future")],
        "principles": registry["principles"],
    }


__all__ = ["SCHEMA", "get_node", "intelligence_summary", "list_nodes", "load_registry"]
