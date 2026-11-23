-- Release 1.1. Re-run on every start while in progress, so every statement must be safe to
-- run twice. Each change here is also in scheduler.sql, the schema a fresh install gets.

-- The audit log, queryable (audit.py). Every audit() call writes a line to logs/audit.jsonl
-- and a row here, on its own connection, so a request that rolls back still leaves its row.
-- The file is the full record; this table is pruned to AUDIT_RETENTION_DAYS. ts is UTC.
-- user_id has no foreign key on purpose: a deleted user's history must survive them.
-- source is 'backend', or 'portal' for events pulled from the portal.
CREATE TABLE IF NOT EXISTS audit_events (
    id        BIGSERIAL PRIMARY KEY,
    ts        TIMESTAMP NOT NULL,
    source    TEXT NOT NULL DEFAULT 'backend',
    event     TEXT NOT NULL,
    severity  TEXT NOT NULL CHECK (severity IN ('info', 'low', 'medium', 'high', 'critical')),
    actor     TEXT,
    user_id   TEXT,
    ip        TEXT,
    method    TEXT,
    path      TEXT,
    message   TEXT NOT NULL,
    data      JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_events_ts ON audit_events (ts);
CREATE INDEX IF NOT EXISTS audit_events_event_ts ON audit_events (event, ts);
