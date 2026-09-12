from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from .task_graph import TaskGraph
from .util import ForgeError


class MissionStore:
    """Durable mission snapshot plus append-only controller event journal.

    Snapshots are complete enough to reconstruct mission state after a process crash.
    The journal is informational/recovery evidence; replay of external side effects is
    deliberately outside this class and must go through reconciliation.
    """

    SCHEMA_VERSION = 1

    def __init__(self, root: Path, mission_id: str):
        if not mission_id or any(ch in mission_id for ch in "/\\\x00"):
            raise ForgeError("Invalid mission ID")
        self.root = root.resolve() / "missions" / mission_id
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.snapshot_path = self.root / "mission.json"
        self.events_path = self.root / "events.ndjson"

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def save(self, state: dict) -> None:
        payload = dict(state)
        payload["schema_version"] = self.SCHEMA_VERSION
        payload["updated_at"] = time.time()
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self._atomic_write(self.snapshot_path, raw)

    def load(self) -> dict:
        try:
            value = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ForgeError("Mission state not found") from exc
        except json.JSONDecodeError as exc:
            raise ForgeError("Mission state is corrupt") from exc
        if value.get("schema_version") != self.SCHEMA_VERSION:
            raise ForgeError("Unsupported mission state schema")
        return value

    def append_event(self, kind: str, data: dict) -> int:
        if not kind or len(kind) > 128:
            raise ForgeError("Invalid mission event kind")
        seq = self.event_count() + 1
        record = {
            "seq": seq,
            "kind": kind,
            "data": data,
            "at": time.time(),
        }
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return seq

    def events(self, *, after: int = 0) -> list[dict]:
        if after < 0:
            raise ForgeError("Event cursor cannot be negative")
        if not self.events_path.exists():
            return []
        result = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ForgeError("Mission event journal is corrupt") from exc
            if int(value.get("seq", 0)) > after:
                result.append(value)
        return result

    def event_count(self) -> int:
        if not self.events_path.exists():
            return 0
        with self.events_path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

    def save_graph(self, graph: TaskGraph, *, paused: bool, steering: list[dict], metadata=None) -> None:
        self.save(
            {
                "mission_id": graph.mission_id,
                "paused": bool(paused),
                "graph": graph.to_dict(),
                "steering": steering,
                "metadata": dict(metadata or {}),
            }
        )

    def restore_graph(self) -> tuple[TaskGraph, dict]:
        state = self.load()
        graph = TaskGraph.from_dict(state["graph"])
        if graph.mission_id != state.get("mission_id"):
            raise ForgeError("Mission snapshot identity mismatch")
        return graph, state
