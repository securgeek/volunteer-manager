-- Release 1.1. Re-run on every start while in progress, so every statement must be safe to
-- run twice. Each change here is also in portal.sql, the schema a fresh install gets.

-- Hostile-looking requests noticed by security_signals.py (portal/main.py). The scheduler
-- pulls them (GET /api/sync/security_events) into its audit log, where its alert rules run.
-- Rows older than SECURITY_EVENT_DAYS are pruned on each pull. ts is UTC ISO-8601 with Z.
CREATE TABLE IF NOT EXISTS portal_security_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    event     TEXT NOT NULL,
    severity  TEXT NOT NULL,
    ip        TEXT,
    method    TEXT,
    path      TEXT,
    message   TEXT NOT NULL,
    data      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS portal_security_events_ts ON portal_security_events (ts);
