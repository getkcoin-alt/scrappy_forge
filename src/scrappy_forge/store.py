from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .util import ForgeError, encoded, redact, sha


class Store:
    """Local, project-scoped SQLite state, audit journal and FTS5 memory."""

    def __init__(self, home: Path):
        self.home = home.resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.home / "forge.sqlite", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, project TEXT, state TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, kind TEXT, data TEXT, at REAL);
            CREATE INDEX IF NOT EXISTS events_session ON events(session,id);
            CREATE TABLE IF NOT EXISTS memory(id INTEGER PRIMARY KEY, project TEXT, note TEXT, source TEXT, source_hash TEXT, origin TEXT, at REAL);
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(note, content='memory', content_rowid='id');
            CREATE TRIGGER IF NOT EXISTS memory_insert AFTER INSERT ON memory BEGIN
              INSERT INTO memory_fts(rowid,note) VALUES(new.id,new.note); END;
            CREATE TRIGGER IF NOT EXISTS memory_delete AFTER DELETE ON memory BEGIN
              INSERT INTO memory_fts(memory_fts,rowid,note) VALUES('delete',old.id,old.note); END;
        """)

    def close(self):
        self.db.close()

    def create(self, project: Path, state: dict) -> str:
        sid = uuid.uuid4().hex[:16]
        state.update(id=sid, project=str(project.resolve()))
        self.save(state)
        return sid

    def save(self, state: dict):
        self.db.execute(
            "INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,updated=excluded.updated",
            (state["id"], state["project"], encoded(state), time.time()),
        )

    def load(self, sid: str) -> dict:
        row = self.db.execute("SELECT state FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            raise ForgeError("Session not found")
        return json.loads(row[0])

    def sessions(self, project: Path | None = None):
        sql = "SELECT id,project,updated FROM sessions"
        args = ()
        if project is not None:
            sql += " WHERE project=?"
            args = (str(project.resolve()),)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY updated DESC LIMIT 30", args)]

    def event(self, sid: str, kind: str, data) -> int:
        cur = self.db.execute(
            "INSERT INTO events(session,kind,data,at) VALUES(?,?,?,?)",
            (sid, kind, encoded(redact(data)), time.time()),
        )
        return cur.lastrowid

    def events(self, sid: str, after: int = 0, limit: int = 20):
        return [
            {**dict(r), "data": json.loads(r["data"])}
            for r in self.db.execute(
                "SELECT * FROM events WHERE session=? AND id>? ORDER BY id LIMIT ?",
                (sid, after, min(limit, 50)),
            )
        ]

    def artifact(self, sid: str, value) -> str:
        raw = encoded(redact(value))
        key = sha(raw)
        directory = self.home / "sessions" / sid / "artifacts"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        p = directory / (key + ".json")
        if not p.exists():
            p.write_text(raw)
        return key

    def read_artifact(self, sid: str, key: str, offset: int = 0, size: int = 6000):
        import re

        if not re.fullmatch(r"[a-f0-9]{64}", key) or not 0 <= offset <= 10_000_000:
            raise ForgeError("Invalid artifact reference")
        p = self.home / "sessions" / sid / "artifacts" / (key + ".json")
        text = p.read_text()
        return {"text": text[offset : offset + min(size, 6000)], "total_chars": len(text), "offset": offset}

    def remember(
        self, project: str, note: str, source: str | None, source_hash: str | None, origin: str
    ) -> int:
        if not note.strip() or len(note) > 4000:
            raise ForgeError("Memory notes must contain 1–4000 characters")
        cur = self.db.execute(
            "INSERT INTO memory(project,note,source,source_hash,origin,at) VALUES(?,?,?,?,?,?)",
            (project, redact(note), source, source_hash, origin, time.time()),
        )
        return cur.lastrowid

    def memories(self, project: str, query: str = "", limit: int = 8):
        import re

        words = re.findall(r"\w+", query)[:12]
        if words:
            expr = " OR ".join('"' + w.replace('"', "") + '"' for w in words)
            rows = self.db.execute(
                "SELECT m.* FROM memory m JOIN memory_fts f ON m.id=f.rowid WHERE m.project=? AND memory_fts MATCH ? ORDER BY rank LIMIT ?",
                (project, expr, limit),
            )
        else:
            rows = self.db.execute(
                "SELECT * FROM memory WHERE project=? ORDER BY id DESC LIMIT ?", (project, limit)
            )
        return [dict(r) for r in rows]

    def forget(self, project: str, mid: int):
        self.db.execute("DELETE FROM memory WHERE project=? AND id=?", (project, mid))
