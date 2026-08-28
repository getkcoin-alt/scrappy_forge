"""Read-only, bounded local OS observations as a queryable graph.

Snapshots are observations, not an authoritative operating system API. They
carry collector status and age. No pixels, environment variables, process argv,
window titles, file contents, elevation, or remote telemetry are collected.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import math
import os
import plistlib
import sys
import time
from pathlib import Path

from .execution import process
from .tools import Tool, object_schema, text_schema
from .util import ForgeError, atomic_json, clean, encoded, sha
from .workspace import excluded, manifest

SCOPES = {"files", "processes", "network", "windows"}
KINDS = {"project", "directory", "file", "process", "connection", "endpoint", "window"}
MAX_NODES, MAX_EDGES = 10000, 20000


def identity(kind, value):
    return kind + ":" + sha(encoded(value))[:32]


def parse_processes(text):
    rows = []
    for line in text.splitlines()[:4000]:
        fields = line.split(None, 7)
        if len(fields) != 8:
            continue
        try:
            pid, parent = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        if pid < 1 or parent < 0:
            continue
        rows.append(
            {
                "pid": pid,
                "ppid": parent,
                "started": " ".join(fields[2:7]),
                "name": clean(Path(fields[7]).name)[:200],
            }
        )
    return rows


def parse_lsof(text):
    """Parse lsof's machine-readable field protocol; never parse human tables."""
    result, pid, record = [], None, None
    for line in text.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag in {"p", "f"} and record:
            result.append(record)
            record = None
        if tag == "p":
            pid = int(value) if value.isdigit() else None
        elif tag == "f" and pid is not None:
            record = {"pid": pid, "fd": value[:20]}
        elif record is not None and tag == "n":
            record["name"] = clean(value)[:1000]
        elif record is not None and tag == "t":
            record["type"] = value[:30]
        elif record is not None and tag == "P":
            record["protocol"] = value[:20]
        elif record is not None and tag == "T" and value.startswith("ST="):
            record["state"] = value[3:][:40]
    if record:
        result.append(record)
    return result[:6000]


def mac_windows():
    """Public CoreGraphics/CF APIs on Intel and Apple Silicon; metadata only.

    Does not request or attempt to bypass macOS Screen Recording permission.
    macOS may redact metadata according to the user's existing OS permissions.
    """
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    cf.CFPropertyListCreateData.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_long,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    cf.CFPropertyListCreateData.restype = ctypes.c_void_p
    cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
    cf.CFDataGetLength.restype = ctypes.c_long
    cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    cf.CFDataGetBytePtr.restype = ctypes.c_void_p
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
    array = cg.CGWindowListCopyWindowInfo(17, 0)  # on-screen only, excluding desktop elements
    if not array:
        raise ForgeError("Window metadata unavailable in this macOS login session")
    data = None
    try:
        data = cf.CFPropertyListCreateData(None, array, 100, 0, None)  # XML property list
        if not data:
            raise ForgeError("Could not serialize macOS window metadata")
        length = cf.CFDataGetLength(data)
        if not 0 < length <= 2_000_000:
            raise ForgeError("Window metadata exceeded its size limit")
        rows = plistlib.loads(ctypes.string_at(cf.CFDataGetBytePtr(data), length))
        return [
            {
                "number": int(r["kCGWindowNumber"]),
                "pid": int(r["kCGWindowOwnerPID"]),
                "app": clean(r.get("kCGWindowOwnerName", ""))[:200],
                "bounds": r.get("kCGWindowBounds", {}),
                "layer": r.get("kCGWindowLayer", 0),
            }
            for r in rows[:1000]
            if "kCGWindowNumber" in r and "kCGWindowOwnerPID" in r
        ]
    finally:
        if data:
            cf.CFRelease(data)
        cf.CFRelease(array)


class World:
    def __init__(self, root: Path, *, platform=None, command=process, window_reader=mac_windows):
        self.root = root.resolve()
        self.platform = platform or sys.platform
        self.command, self.window_reader = command, window_reader
        self.current = None

    async def capture(self, scopes=("files",)):
        scopes = set(scopes)
        if not scopes or not scopes <= SCOPES:
            raise ForgeError("World scopes must be files, processes, network or windows")
        # Connection/window ownership needs process identity; make this dependency explicit.
        if scopes & {"network", "windows"}:
            scopes.add("processes")
        nodes, edges, statuses = {}, {}, {}
        truncated = False

        def node(kind, value, attributes):
            nonlocal truncated
            key = identity(kind, value)
            if key not in nodes and len(nodes) >= MAX_NODES:
                truncated = True
                return None
            nodes[key] = {"id": key, "kind": kind, "attributes": attributes}
            return key

        def edge(source, relation, target):
            nonlocal truncated
            if not source or not target:
                return
            if len(edges) >= MAX_EDGES:
                truncated = True
                return
            key = identity("edge", [source, relation, target])
            edges[key] = {"id": key, "source": source, "relation": relation, "target": target}

        project_id = sha(str(self.root))[:24]
        project = node("project", project_id, {"name": self.root.name, "scope": "selected_project"})
        file_ids = {}
        if "files" in scopes:
            try:
                source_files = await asyncio.to_thread(manifest, self.root)
                directories = {".": project}
                for name, metadata in source_files.items():
                    parent = Path(name).parent
                    for directory in [*reversed(parent.parents), parent]:
                        text = directory.as_posix()
                        if text not in directories:
                            directories[text] = node("directory", [project_id, text], {"path": text})
                            edge(directories.get(directory.parent.as_posix()), "contains", directories[text])
                    file_ids[name] = node(
                        "file",
                        [project_id, name],
                        {"path": name, "sha256": metadata["sha"], "mode": metadata["mode"]},
                    )
                    edge(directories[parent.as_posix()], "contains", file_ids[name])
                statuses["files"] = {
                    "status": "ok",
                    "source": "scoped filesystem",
                    "count": len(source_files),
                }
            except (OSError, ForgeError) as exc:
                statuses["files"] = {"status": "unavailable", "reason": clean(str(exc))[:200]}

        pids = {}
        if "processes" in scopes:
            ps = next((str(p) for p in (Path("/bin/ps"), Path("/usr/bin/ps")) if p.is_file()), None)
            if self.platform not in {"darwin", "linux"} or not ps:
                statuses["processes"] = {"status": "unsupported"}
            else:
                result = await self.command(
                    [ps, "-U", str(os.getuid()), "-o", "pid=,ppid=,lstart=,comm="],
                    self.root,
                    timeout=5,
                    env={"LC_ALL": "C"},
                    max_output=500000,
                )
                rows = parse_processes(result["output"])
                for row in rows:
                    pids[row["pid"]] = node("process", [row["pid"], row["started"]], row)
                for row in rows:
                    edge(pids.get(row["ppid"]), "parent_of", pids.get(row["pid"]))
                statuses["processes"] = {
                    "status": "ok" if result["exit_code"] == 0 else "partial",
                    "source": "ps; current real user only",
                    "count": len(rows),
                    "truncated": result["output_limited"],
                }

        if "network" in scopes or (file_ids and pids):
            lsof = next(
                (str(p) for p in (Path("/usr/sbin/lsof"), Path("/usr/bin/lsof")) if p.is_file()), None
            )
            if lsof:
                result = await self.command(
                    [lsof, "-nP", "-a", "-u", str(os.getuid()), "-FpfntPT"],
                    self.root,
                    timeout=5,
                    max_output=2_000_000,
                )
                records = parse_lsof(result["output"])
                for row in records:
                    owner = pids.get(row["pid"])
                    if not owner:
                        continue  # do not invent identities for inaccessible/raced processes
                    name = row.get("name", "")
                    if row.get("type") in {"IPv4", "IPv6"} and "network" in scopes:
                        connection = node(
                            "connection",
                            [owner, row["fd"], name],
                            {k: row[k] for k in ("fd", "protocol", "state") if k in row},
                        )
                        edge(owner, "owns", connection)
                        for index, endpoint in enumerate(name.split("->", 1)):
                            dest = node("endpoint", endpoint, {"address": endpoint[:300]})
                            edge(connection, "local_endpoint" if index == 0 else "remote_endpoint", dest)
                    elif file_ids and name.startswith("/"):
                        try:
                            relative = Path(name).relative_to(self.root)
                            if not excluded(relative):
                                edge(owner, "opens", file_ids.get(relative.as_posix()))
                        except ValueError:
                            pass
                status = (
                    "partial"
                    if result["exit_code"] != 0 or result["timed_out"] or result["output_limited"]
                    else "ok"
                )
                statuses["open_files"] = {"status": status, "source": "lsof; project paths only"}
                if "network" in scopes:
                    statuses["network"] = {
                        "status": status,
                        "source": "lsof; current user only",
                        "includes": "visible TCP/UDP sockets, not packet contents",
                    }
            else:
                if "network" in scopes:
                    statuses["network"] = {"status": "unavailable", "reason": "lsof is not installed"}
                statuses["open_files"] = {"status": "unavailable", "reason": "lsof is not installed"}

        if "windows" in scopes:
            if self.platform != "darwin":
                statuses["windows"] = {"status": "unsupported", "reason": "macOS adapter only"}
            else:
                try:
                    rows = await asyncio.to_thread(self.window_reader)
                    for row in rows:
                        owner = pids.get(row["pid"])
                        if owner:
                            window = node("window", [owner, row["number"]], row)
                            edge(owner, "owns_window", window)
                    statuses["windows"] = {
                        "status": "ok",
                        "source": "CoreGraphics on-screen metadata",
                        "titles_collected": False,
                        "os_redaction_possible": True,
                    }
                except (OSError, ValueError, KeyError, AttributeError, ForgeError) as exc:
                    statuses["windows"] = {"status": "unavailable", "reason": clean(str(exc))[:200]}

        value = {
            "schema_version": 1,
            "project_id": project_id,
            "captured_at": time.time(),
            "platform": self.platform,
            "scopes": sorted(scopes),
            "observation_mode": "poll",
            "nodes": list(nodes.values()),
            "edges": list(edges.values()),
            "collectors": statuses,
            "truncated": truncated,
            "untrusted_observations": True,
        }
        value["snapshot_id"] = sha(encoded(value))
        self.current = value
        return value


def validate_snapshot(value):
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ForgeError("Unsupported world snapshot")
    if (
        type(value.get("captured_at")) not in {float, int}
        or not math.isfinite(value["captured_at"])
        or value["captured_at"] <= 0
        or not isinstance(value.get("snapshot_id"), str)
    ):
        raise ForgeError("Invalid world snapshot metadata")
    nodes, edges = value.get("nodes"), value.get("edges")
    if (
        not isinstance(nodes, list)
        or not isinstance(edges, list)
        or len(nodes) > MAX_NODES
        or len(edges) > MAX_EDGES
    ):
        raise ForgeError("Invalid or oversized world graph")
    if any(
        not isinstance(n, dict)
        or n.get("kind") not in KINDS
        or not isinstance(n.get("id"), str)
        or not isinstance(n.get("attributes"), dict)
        for n in nodes
    ):
        raise ForgeError("Invalid world nodes")
    ids = {n["id"] for n in nodes}
    if len(ids) != len(nodes) or any(
        not isinstance(e, dict)
        or e.get("source") not in ids
        or e.get("target") not in ids
        or not isinstance(e.get("relation"), str)
        or not isinstance(e.get("id"), str)
        for e in edges
    ):
        raise ForgeError("Invalid world edges")
    return value


def query(snapshot, *, kind=None, contains="", node_id=None, relation=None, offset=0, limit=50):
    validate_snapshot(snapshot)
    if kind is not None and kind not in KINDS:
        raise ForgeError("Unknown world node kind")
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
        raise ForgeError("Query limit must be 1–200 and offset nonnegative")
    if not isinstance(contains, str) or len(contains) > 200:
        raise ForgeError("Invalid graph query text")
    linked = [
        e
        for e in snapshot["edges"]
        if (not node_id or node_id in {e["source"], e["target"]})
        and (not relation or e["relation"] == relation)
    ]
    reachable = {k for e in linked for k in (e["source"], e["target"])} | ({node_id} if node_id else set())
    matches = [
        n
        for n in snapshot["nodes"]
        if (not kind or n["kind"] == kind)
        and (not contains or contains.lower() in encoded(n["attributes"]).lower())
        and (not node_id and not relation or n["id"] in reachable)
    ]
    selected = matches[offset : offset + limit]
    ids = {n["id"] for n in selected}
    age = max(0, time.time() - snapshot["captured_at"])
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "age_seconds": round(age, 2),
        "stale": age > 30,
        "total": len(matches),
        "nodes": selected,
        "edges": [e for e in linked if e["source"] in ids or e["target"] in ids][:400],
        "collectors": snapshot.get("collectors", {}),
        "truncated": snapshot.get("truncated", False),
    }


def delta(before, after):
    validate_snapshot(before)
    validate_snapshot(after)
    if before.get("project_id") != after.get("project_id") or before.get("scopes") != after.get("scopes"):
        raise ForgeError("Only compare snapshots of the same project and scopes")
    result = {
        "before": before["snapshot_id"],
        "after": after["snapshot_id"],
        "collection_incomplete": before.get("truncated", False) or after.get("truncated", False),
    }
    for field in ("nodes", "edges"):
        left, right = ({n["id"]: n for n in s[field]} for s in (before, after))
        result[field] = {
            "added": sorted(right.keys() - left.keys()),
            "removed": sorted(left.keys() - right.keys()),
            "changed": sorted(k for k in left.keys() & right.keys() if left[k] != right[k]),
        }
    result["note"] = "Missing observations can reflect races or permission changes, not actual deletion."
    return result


def register_world(engine):
    world = World(engine.workspace.root)
    engine.world = world

    async def capture(args):
        value = await world.capture(args.get("scopes", ["files"]))
        return {k: value[k] for k in ("snapshot_id", "captured_at", "scopes", "collectors", "truncated")} | {
            "node_count": len(value["nodes"]),
            "edge_count": len(value["edges"]),
        }

    async def search(args):
        if not world.current:
            raise ForgeError("Capture a world snapshot first")
        if args["snapshot_id"] != world.current["snapshot_id"]:
            raise ForgeError("Snapshot changed; query the current snapshot ID")
        return query(world.current, **{k: v for k, v in args.items() if k != "snapshot_id"})

    engine.registry.register(
        Tool(
            "world_capture",
            "Capture a read-only structured local world graph. Host scopes require approval. "
            "No screenshots, file contents, argv or window titles. Results may be sent to the model.",
            object_schema(
                {
                    "scopes": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"enum": sorted(SCOPES)},
                    }
                }
            ),
            capture,
            risk="external",
            protected=lambda _: True,
            timeout=25,
        )
    )
    engine.registry.register(
        Tool(
            "world_query",
            "Query a captured graph by kind, text, ID or one-hop relation; returns age and partial status. "
            "Approval explicitly permits sharing selected machine metadata with the model.",
            object_schema(
                {
                    "snapshot_id": text_schema(64),
                    "kind": {"enum": sorted(KINDS)},
                    "contains": {"type": "string", "maxLength": 200},
                    "node_id": text_schema(80),
                    "relation": text_schema(80),
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                ["snapshot_id"],
            ),
            search,
            risk="external",
            protected=lambda _: True,
        )
    )


def parser():
    p = argparse.ArgumentParser(prog="forge world")
    p.add_argument("action", choices=["snapshot", "query", "diff"])
    p.add_argument("--repo", type=Path, default=Path.cwd())
    p.add_argument("--scopes", default="files", help="files,processes,network,windows")
    p.add_argument(
        "--allow-host-read", action="store_true", help="Explicitly permit current-user OS metadata"
    )
    p.add_argument("--output", type=Path)
    p.add_argument("--snapshot", type=Path)
    p.add_argument("--before", type=Path)
    p.add_argument("--after", type=Path)
    p.add_argument("--kind", choices=sorted(KINDS))
    p.add_argument("--contains", default="")
    p.add_argument("--node-id")
    p.add_argument("--relation")
    p.add_argument("--limit", type=int, default=50)
    return p


def load_snapshot(path):
    if not path or not path.is_file() or path.stat().st_size > 8_000_000:
        raise ForgeError("Provide a snapshot file smaller than 8 MB")
    return validate_snapshot(json.loads(path.read_text()))


async def run(args, ui):
    if args.action == "snapshot":
        scopes = set(args.scopes.split(","))
        if scopes - {"files"} and not args.allow_host_read:
            raise ForgeError("Host metadata requires --allow-host-read; no collection occurred")
        value = await World(args.repo).capture(scopes)
    elif args.action == "query":
        value = query(
            load_snapshot(args.snapshot),
            kind=args.kind,
            contains=args.contains,
            node_id=args.node_id,
            relation=args.relation,
            limit=args.limit,
        )
    else:
        value = delta(load_snapshot(args.before), load_snapshot(args.after))
    if args.output:
        if args.output.exists() or args.output.is_symlink():
            raise ForgeError("Output already exists; choose a new filename")
        atomic_json(args.output, value)
        ui.say("Saved local graph data. No upload was performed.")
    else:
        ui.say(json.dumps(value, indent=2))
    return 0
