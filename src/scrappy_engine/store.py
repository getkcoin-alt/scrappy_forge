"""Durable system of record for missions.

SQLite (WAL, foreign keys, BEGIN IMMEDIATE transactions) is the V1 local store. The
schema mirrors the SE-001 table list so a PostgreSQL backend can implement the same
methods; that backend does not exist yet.

Audit events are append-only: UPDATE/DELETE are rejected by triggers and every row
carries a hash chained to the previous row, so silent edits are detectable.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from scrappy_forge.util import ForgeError, encoded, sha

from .contracts import MissionSpec
from .states import TERMINAL, MissionState, check_transition

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    state TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    spec TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    attempts INTEGER NOT NULL DEFAULT 0,
    model_calls INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    blocked_state TEXT,
    reason TEXT,
    workspace TEXT,
    context TEXT NOT NULL DEFAULT '{}',
    result TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    ordinal INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE (mission_id, ordinal)
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output TEXT,
    error TEXT,
    cost_usd REAL NOT NULL DEFAULT 0,
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS tool_calls (
    execution_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    agent_role TEXT NOT NULL,
    tool_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    arguments TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    decision TEXT NOT NULL,
    output TEXT,
    evidence_hash TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    UNIQUE (mission_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    kind TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    role TEXT NOT NULL,
    title TEXT NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    at REAL NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
"""

GENESIS = "0" * 64


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _loads(value):
    return None if value is None else json.loads(value)


class EngineStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def tx(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _read(self, sql: str, params=()) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    # ---- audit -------------------------------------------------------------

    @staticmethod
    def _audit(conn, mission_id: str | None, kind: str, data: dict) -> int:
        row = conn.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = row["hash"] if row else GENESIS
        at = time.time()
        body = encoded(data)
        digest = sha(encoded([prev, mission_id, kind, body, at]))
        cur = conn.execute(
            "INSERT INTO audit_events (mission_id, kind, data, at, prev_hash, hash) VALUES (?,?,?,?,?,?)",
            (mission_id, kind, body, at, prev, digest),
        )
        return int(cur.lastrowid)

    def audit(self, mission_id: str | None, kind: str, data: dict) -> int:
        with self.tx() as conn:
            return self._audit(conn, mission_id, kind, data)

    def events(self, mission_id: str, *, after: int = 0, limit: int = 500) -> list[dict]:
        rows = self._read(
            "SELECT seq, kind, data, at FROM audit_events WHERE mission_id=? AND seq>? ORDER BY seq LIMIT ?",
            (mission_id, after, limit),
        )
        return [
            {"seq": r["seq"], "kind": r["kind"], "data": json.loads(r["data"]), "at": r["at"]} for r in rows
        ]

    def verify_audit_chain(self) -> bool:
        prev = GENESIS
        for r in self._read("SELECT * FROM audit_events ORDER BY seq"):
            expected = sha(encoded([prev, r["mission_id"], r["kind"], r["data"], r["at"]]))
            if r["prev_hash"] != prev or r["hash"] != expected:
                return False
            prev = r["hash"]
        return True

    # ---- projects ----------------------------------------------------------

    def connect_project(self, name: str, path: Path) -> dict:
        resolved = Path(path).resolve()
        if not resolved.is_dir():
            raise ForgeError("Project path must be an existing directory")
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM projects WHERE path=?", (str(resolved),)).fetchone()
            if row:
                return dict(row)
            project = {"id": _new_id("prj"), "name": name, "path": str(resolved), "created_at": time.time()}
            conn.execute("INSERT INTO projects VALUES (:id,:name,:path,:created_at)", project)
            self._audit(
                conn, None, "project_connected", {"project_id": project["id"], "path": project["path"]}
            )
            return project

    def get_project(self, project_id: str) -> dict:
        rows = self._read("SELECT * FROM projects WHERE id=?", (project_id,))
        if not rows:
            raise ForgeError("Unknown project")
        return dict(rows[0])

    def projects(self) -> list[dict]:
        return [dict(r) for r in self._read("SELECT * FROM projects ORDER BY created_at")]

    # ---- missions ----------------------------------------------------------

    def _mission(self, row) -> dict:
        value = dict(row)
        value["spec"] = json.loads(value["spec"])
        value["context"] = json.loads(value["context"])
        value["result"] = _loads(value["result"])
        value["cancel_requested"] = bool(value["cancel_requested"])
        return value

    def create_mission(self, spec: MissionSpec, *, idempotency_key: str | None = None) -> tuple[dict, bool]:
        request_hash = spec.request_hash()
        with self.tx() as conn:
            if conn.execute("SELECT 1 FROM projects WHERE id=?", (spec.project_id,)).fetchone() is None:
                raise ForgeError("Unknown project; connect it first")
            if idempotency_key:
                row = conn.execute(
                    "SELECT * FROM missions WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if row:
                    if row["request_hash"] != request_hash:
                        raise ForgeError("Idempotency key was already used for a different mission request")
                    return self._mission(row), False
            now = time.time()
            mission_id = _new_id("msn")
            conn.execute(
                "INSERT INTO missions (id, project_id, state, spec, request_hash, idempotency_key,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    mission_id,
                    spec.project_id,
                    MissionState.CREATED.value,
                    encoded(spec.to_dict()),
                    request_hash,
                    idempotency_key,
                    now,
                    now,
                ),
            )
            self._audit(
                conn,
                mission_id,
                "mission_created",
                {"objective": spec.objective, "autonomy_level": spec.autonomy_level.name},
            )
            row = conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
            return self._mission(row), True

    def get_mission(self, mission_id: str) -> dict:
        rows = self._read("SELECT * FROM missions WHERE id=?", (mission_id,))
        if not rows:
            raise ForgeError("Unknown mission")
        return self._mission(rows[0])

    def missions(self, *, limit: int = 100) -> list[dict]:
        rows = self._read("SELECT * FROM missions ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._mission(r) for r in rows]

    def transition(
        self, mission_id: str, target: MissionState, *, reason: str | None = None, **fields
    ) -> dict:
        allowed_fields = {"workspace", "context", "result", "attempts"}
        if set(fields) - allowed_fields:
            raise ForgeError("Unsupported mission field update")
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
            if row is None:
                raise ForgeError("Unknown mission")
            current = MissionState(row["state"])
            check_transition(current, target)
            updates = {"state": target.value, "updated_at": time.time(), "reason": reason}
            updates["blocked_state"] = current.value if target == MissionState.BLOCKED else None
            for key, value in fields.items():
                updates[key] = encoded(value) if key in {"context", "result"} else value
            assignments = ", ".join(f"{k}=:{k}" for k in updates)
            cur = conn.execute(
                f"UPDATE missions SET {assignments}, version=version+1 WHERE id=:id AND version=:version",
                {**updates, "id": mission_id, "version": row["version"]},
            )
            if cur.rowcount != 1:
                raise ForgeError("Concurrent mission update detected")
            self._audit(
                conn,
                mission_id,
                "state_changed",
                {"from": current.value, "to": target.value, "reason": reason},
            )
            return self._mission(conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone())

    def update_context(self, mission_id: str, context: dict) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE missions SET context=?, updated_at=?, version=version+1 WHERE id=?",
                (encoded(context), time.time(), mission_id),
            )

    def charge(self, mission_id: str, *, model_calls: int = 0, cost_usd: float = 0.0) -> dict:
        with self.tx() as conn:
            conn.execute(
                "UPDATE missions SET model_calls=model_calls+?, cost_usd=cost_usd+?, version=version+1"
                " WHERE id=?",
                (model_calls, cost_usd, mission_id),
            )
            return self._mission(conn.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone())

    def request_cancel(self, mission_id: str) -> dict:
        with self.tx() as conn:
            row = conn.execute("SELECT state FROM missions WHERE id=?", (mission_id,)).fetchone()
            if row is None:
                raise ForgeError("Unknown mission")
            if MissionState(row["state"]) not in TERMINAL:
                conn.execute(
                    "UPDATE missions SET cancel_requested=1, version=version+1 WHERE id=?", (mission_id,)
                )
                self._audit(conn, mission_id, "cancel_requested", {})
        return self.get_mission(mission_id)

    # ---- plan tasks, decisions, artifacts ----------------------------------

    def set_tasks(self, mission_id: str, titles: list[str]) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM tasks WHERE mission_id=?", (mission_id,))
            for i, title in enumerate(titles, 1):
                conn.execute(
                    "INSERT INTO tasks VALUES (?,?,?,?,?)", (_new_id("tsk"), mission_id, i, title, "planned")
                )

    def set_task_status(self, mission_id: str, status: str) -> None:
        with self.tx() as conn:
            conn.execute("UPDATE tasks SET status=? WHERE mission_id=?", (status, mission_id))

    def tasks(self, mission_id: str) -> list[dict]:
        return [
            dict(r)
            for r in self._read("SELECT * FROM tasks WHERE mission_id=? ORDER BY ordinal", (mission_id,))
        ]

    def add_decision(self, mission_id: str, role: str, title: str, rationale: str, status="proposed") -> str:
        decision_id = _new_id("dec")
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?,?)",
                (decision_id, mission_id, role, title[:500], rationale[:8000], status, time.time()),
            )
            self._audit(conn, mission_id, "decision_recorded", {"decision_id": decision_id, "role": role})
        return decision_id

    def decisions(self, mission_id: str) -> list[dict]:
        return [
            dict(r)
            for r in self._read(
                "SELECT * FROM decisions WHERE mission_id=? ORDER BY created_at", (mission_id,)
            )
        ]

    def add_artifact(self, mission_id: str, kind: str, content: str) -> dict:
        artifact = {
            "id": _new_id("art"),
            "mission_id": mission_id,
            "kind": kind,
            "sha256": sha(content),
            "content": content,
            "created_at": time.time(),
        }
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO artifacts VALUES (:id,:mission_id,:kind,:sha256,:content,:created_at)", artifact
            )
            self._audit(conn, mission_id, "artifact_stored", {"artifact_id": artifact["id"], "kind": kind})
        return artifact

    def artifacts(self, mission_id: str) -> list[dict]:
        return [
            dict(r)
            for r in self._read(
                "SELECT * FROM artifacts WHERE mission_id=? ORDER BY created_at", (mission_id,)
            )
        ]

    # ---- agent runs --------------------------------------------------------

    def start_agent_run(
        self, mission_id: str, role: str, provider: str, attempt: int, input_hash: str
    ) -> str:
        run_id = _new_id("run")
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO agent_runs (id, mission_id, role, provider, attempt, status, input_hash, started_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (run_id, mission_id, role, provider, attempt, "running", input_hash, time.time()),
            )
            self._audit(
                conn, mission_id, "agent_started", {"run_id": run_id, "role": role, "provider": provider}
            )
        return run_id

    def finish_agent_run(self, run_id: str, *, status: str, output=None, error=None, cost_usd=0.0) -> None:
        with self.tx() as conn:
            row = conn.execute("SELECT mission_id, role FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            conn.execute(
                "UPDATE agent_runs SET status=?, output=?, error=?, cost_usd=?, finished_at=? WHERE id=?",
                (
                    status,
                    None if output is None else encoded(output),
                    error,
                    cost_usd,
                    time.time(),
                    run_id,
                ),
            )
            self._audit(
                conn,
                row["mission_id"],
                "agent_finished",
                {"run_id": run_id, "role": row["role"], "status": status},
            )

    def agent_runs(self, mission_id: str) -> list[dict]:
        rows = self._read("SELECT * FROM agent_runs WHERE mission_id=? ORDER BY started_at", (mission_id,))
        return [{**dict(r), "output": _loads(r["output"])} for r in rows]

    # ---- tool calls --------------------------------------------------------

    def find_tool_call(self, mission_id: str, idempotency_key: str) -> dict | None:
        rows = self._read(
            "SELECT * FROM tool_calls WHERE mission_id=? AND idempotency_key=?", (mission_id, idempotency_key)
        )
        return self._tool_call(rows[0]) if rows else None

    @staticmethod
    def _tool_call(row) -> dict:
        value = dict(row)
        value["arguments"] = json.loads(value["arguments"])
        value["output"] = _loads(value["output"])
        return value

    def begin_tool_call(self, record: dict) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO tool_calls (execution_id, mission_id, agent_role, tool_id, scope, arguments,"
                " idempotency_key, status, decision, started_at, output, finished_at)"
                " VALUES (:execution_id,:mission_id,:agent_role,:tool_id,:scope,:arguments,:idempotency_key,"
                ":status,:decision,:started_at,:output,:finished_at)",
                {**record, "arguments": encoded(record["arguments"]), "output": None, "finished_at": None},
            )
            self._audit(
                conn,
                record["mission_id"],
                "tool_" + record["status"],
                {
                    "execution_id": record["execution_id"],
                    "tool_id": record["tool_id"],
                    "role": record["agent_role"],
                    "decision": record["decision"],
                },
            )

    def finish_tool_call(self, execution_id: str, *, status: str, output: dict, evidence_hash: str) -> None:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT mission_id, tool_id FROM tool_calls WHERE execution_id=?", (execution_id,)
            ).fetchone()
            conn.execute(
                "UPDATE tool_calls SET status=?, output=?, evidence_hash=?, finished_at=? WHERE execution_id=?",
                (status, encoded(output), evidence_hash, time.time(), execution_id),
            )
            self._audit(
                conn,
                row["mission_id"],
                "tool_" + status,
                {"execution_id": execution_id, "tool_id": row["tool_id"], "evidence_hash": evidence_hash},
            )

    def tool_calls(self, mission_id: str) -> list[dict]:
        rows = self._read("SELECT * FROM tool_calls WHERE mission_id=? ORDER BY started_at", (mission_id,))
        return [self._tool_call(r) for r in rows]

    def get_tool_call(self, execution_id: str) -> dict:
        rows = self._read("SELECT * FROM tool_calls WHERE execution_id=?", (execution_id,))
        if not rows:
            raise ForgeError("Unknown execution record")
        return self._tool_call(rows[0])

    # ---- crash recovery ----------------------------------------------------

    def recover(self) -> list[str]:
        """Mark interrupted work as uncertain and return resumable mission IDs."""
        with self.tx() as conn:
            interrupted = conn.execute(
                "SELECT execution_id, mission_id FROM tool_calls WHERE status='started'"
            ).fetchall()
            for row in interrupted:
                conn.execute(
                    "UPDATE tool_calls SET status='uncertain', finished_at=? WHERE execution_id=?",
                    (time.time(), row["execution_id"]),
                )
                self._audit(conn, row["mission_id"], "tool_uncertain", {"execution_id": row["execution_id"]})
            runs = conn.execute("SELECT id, mission_id FROM agent_runs WHERE status='running'").fetchall()
            for row in runs:
                conn.execute(
                    "UPDATE agent_runs SET status='interrupted', finished_at=? WHERE id=?",
                    (time.time(), row["id"]),
                )
                self._audit(conn, row["mission_id"], "agent_interrupted", {"run_id": row["id"]})
            placeholders = ",".join("?" for _ in TERMINAL)
            rows = conn.execute(
                f"SELECT id FROM missions WHERE state NOT IN ({placeholders}) AND state != 'BLOCKED'",
                tuple(s.value for s in TERMINAL),
            ).fetchall()
            return [r["id"] for r in rows]
