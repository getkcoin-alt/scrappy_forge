-- Scrappy Engine initial schema (SQLite)
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
CREATE INDEX IF NOT EXISTS audit_events_mission ON audit_events (mission_id, seq);
CREATE INDEX IF NOT EXISTS tool_calls_mission ON tool_calls (mission_id, started_at);
CREATE INDEX IF NOT EXISTS agent_runs_mission ON agent_runs (mission_id, started_at);
