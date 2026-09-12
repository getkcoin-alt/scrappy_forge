from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .util import ForgeError, encoded, redact, sha


class Store:
    """Local, project-scoped SQLite state, audit journal and memory indexes."""

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

            -- Legacy notes remain readable for compatibility. New writes use memory_items.
            CREATE TABLE IF NOT EXISTS memory(id INTEGER PRIMARY KEY, project TEXT, note TEXT, source TEXT, source_hash TEXT, origin TEXT, at REAL);
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(note, content='memory', content_rowid='id');
            CREATE TRIGGER IF NOT EXISTS memory_insert AFTER INSERT ON memory BEGIN
              INSERT INTO memory_fts(rowid,note) VALUES(new.id,new.note); END;
            CREATE TRIGGER IF NOT EXISTS memory_delete AFTER DELETE ON memory BEGIN
              INSERT INTO memory_fts(memory_fts,rowid,note) VALUES('delete',old.id,old.note); END;

            CREATE TABLE IF NOT EXISTS memory_items(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project TEXT NOT NULL,
              memory_class TEXT NOT NULL,
              content TEXT NOT NULL,
              provenance TEXT NOT NULL,
              scope TEXT NOT NULL,
              confidence REAL NOT NULL,
              source TEXT,
              source_fingerprint TEXT,
              entity_keys TEXT NOT NULL,
              embedding TEXT,
              origin TEXT NOT NULL,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              invalidated_at REAL,
              invalidation_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS memory_items_project ON memory_items(project,id);
            CREATE INDEX IF NOT EXISTS memory_items_source ON memory_items(project,source);
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts USING fts5(
              content,
              content='memory_items',
              content_rowid='id'
            );
            CREATE TRIGGER IF NOT EXISTS memory_items_insert AFTER INSERT ON memory_items BEGIN
              INSERT INTO memory_items_fts(rowid,content) VALUES(new.id,new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_items_delete AFTER DELETE ON memory_items BEGIN
              INSERT INTO memory_items_fts(memory_items_fts,rowid,content) VALUES('delete',old.id,old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS memory_items_update AFTER UPDATE OF content ON memory_items BEGIN
              INSERT INTO memory_items_fts(memory_items_fts,rowid,content) VALUES('delete',old.id,old.content);
              INSERT INTO memory_items_fts(rowid,content) VALUES(new.id,new.content);
            END;
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

    # Legacy memory API -------------------------------------------------
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

    # Vault Zeta typed memory API --------------------------------------
    def memory_item_add(
        self,
        *,
        project: str,
        memory_class: str,
        content: str,
        provenance: dict,
        scope: dict,
        confidence: float,
        source: str | None,
        source_fingerprint: str | None,
        entity_keys: list[str],
        embedding: list[float] | None,
        origin: str,
    ) -> int:
        if not content.strip() or len(content) > 8000:
            raise ForgeError("Memory content must contain 1-8000 characters")
        if not 0 <= confidence <= 1:
            raise ForgeError("Memory confidence must be between 0 and 1")
        now = time.time()
        cur = self.db.execute(
            """
            INSERT INTO memory_items(
              project,memory_class,content,provenance,scope,confidence,source,
              source_fingerprint,entity_keys,embedding,origin,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                project,
                memory_class,
                redact(content),
                encoded(redact(provenance)),
                encoded(scope),
                confidence,
                source,
                source_fingerprint,
                encoded(sorted(set(entity_keys))),
                encoded(embedding) if embedding is not None else None,
                origin,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def _decode_memory_row(row: sqlite3.Row | dict) -> dict:
        value = dict(row)
        for key in ("provenance", "scope", "entity_keys", "embedding"):
            if value.get(key) is not None and isinstance(value[key], str):
                value[key] = json.loads(value[key])
        return value

    def memory_item_get(self, project: str, mid: int) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM memory_items WHERE project=? AND id=?", (project, mid)
        ).fetchone()
        return self._decode_memory_row(row) if row else None

    def memory_items_recent(self, project: str, limit: int = 64):
        rows = self.db.execute(
            "SELECT * FROM memory_items WHERE project=? ORDER BY created_at DESC LIMIT ?",
            (project, min(max(limit, 1), 512)),
        )
        return [self._decode_memory_row(row) for row in rows]

    def memory_items_fts(self, project: str, query: str, limit: int = 64):
        import re

        words = re.findall(r"\w+", query)[:20]
        if not words:
            return self.memory_items_recent(project, limit)
        expr = " OR ".join('"' + word.replace('"', "") + '"' for word in words)
        rows = self.db.execute(
            """
            SELECT m.*, bm25(memory_items_fts) AS lexical_rank
            FROM memory_items m
            JOIN memory_items_fts f ON m.id=f.rowid
            WHERE m.project=? AND memory_items_fts MATCH ?
            ORDER BY lexical_rank
            LIMIT ?
            """,
            (project, expr, min(max(limit, 1), 512)),
        )
        return [self._decode_memory_row(row) for row in rows]

    def memory_item_invalidate(self, project: str, mid: int, reason: str) -> None:
        if not reason.strip():
            raise ForgeError("Memory invalidation requires a reason")
        self.db.execute(
            "UPDATE memory_items SET invalidated_at=?, invalidation_reason=?, updated_at=? WHERE project=? AND id=?",
            (time.time(), reason[:500], time.time(), project, mid),
        )

    def memory_items_for_source(self, project: str, source: str):
        rows = self.db.execute(
            "SELECT * FROM memory_items WHERE project=? AND source=? ORDER BY id",
            (project, source),
        )
        return [self._decode_memory_row(row) for row in rows]
