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

-- Who gets security alerts by email (alerts.py). Every organizer can read the audit log; this
-- only says whose inbox the alerts go to, and any organizer can change it. Seeded once, on the
-- earliest organizer: the app_kv marker stops a re-run (this file re-runs while 1.1 is in
-- progress) from ticking someone again after the organizers chose nobody. A fresh install
-- sets the marker in scheduler.sql and ticks the first admin at setup instead.
ALTER TABLE users ADD COLUMN IF NOT EXISTS audit_alerts BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE users SET audit_alerts = TRUE
 WHERE user_id = (SELECT user_id FROM users WHERE role = 'organizer'
                   ORDER BY created_at, user_id LIMIT 1)
   AND NOT EXISTS (SELECT 1 FROM app_kv WHERE app_key = 'audit_alerts_seeded');
INSERT INTO app_kv (app_key, val) VALUES ('audit_alerts_seeded', '1') ON CONFLICT DO NOTHING;
-- An earlier 1.1 build had audit_access (read the log and get alerts) in its place.
ALTER TABLE users DROP COLUMN IF EXISTS audit_access;
