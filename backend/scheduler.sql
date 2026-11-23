-- PostgreSQL schema — version 16
-- Applied on a fresh database (schema version 0 → schema_versions row SCHEMA_VERSION).

CREATE TABLE IF NOT EXISTS schema_versions (
    version     INTEGER PRIMARY KEY,
    applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    user_id           TEXT PRIMARY KEY,
    username          TEXT NOT NULL UNIQUE,
    password_hash     TEXT NOT NULL,
    password_version  INTEGER NOT NULL DEFAULT 1,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at     TIMESTAMP,
    failed_login_cnt  INTEGER NOT NULL DEFAULT 0,
    lockout_until     TIMESTAMP,
    role              TEXT NOT NULL DEFAULT 'organizer',
    -- WebAuthn user handle; random, set on first passkey registration (v9).
    webauthn_user_id  BYTEA,
    -- For password reset only; sign-in is by username (v15).
    email             TEXT,
    -- May read the audit log, and receives security alerts (1.1). Granted only by another
    -- holder; the first admin gets it at setup (routers/setup.py).
    audit_access      BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE UNIQUE INDEX IF NOT EXISTS users_webauthn_user_id
    ON users (webauthn_user_id) WHERE webauthn_user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ip_addr     TEXT NOT NULL,
    user_agent  TEXT NOT NULL
);

-- Passkeys (v9). See migrate_v9.sql for why each piece is shaped this way.
CREATE TABLE IF NOT EXISTS user_passkeys (
    credential_id  BYTEA PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    -- The COSE_Key exactly as registered. Any algorithm fits, so adding one (ML-DSA was the
    -- first beyond ES256/EdDSA/RS256) needs no schema change.
    public_key     BYTEA NOT NULL,
    cose_alg       INTEGER NOT NULL,
    sign_count     BIGINT NOT NULL DEFAULT 0,
    transports     TEXT NOT NULL DEFAULT '',     -- comma-separated, as the browser reported
    backed_up      BOOLEAN NOT NULL DEFAULT FALSE,
    name           TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    last_used_at   TIMESTAMP,
    -- Authenticator model, canonical UUID text; NULL when unknown (v10).
    aaguid         TEXT
);
CREATE INDEX IF NOT EXISTS user_passkeys_by_user ON user_passkeys (user_id);

-- One row per options request, consumed by a single conditional UPDATE so a response can be
-- verified once. user_id is set for registration and NULL for sign-in, where nobody is known
-- yet.
CREATE TABLE IF NOT EXISTS webauthn_challenges (
    challenge_hash  TEXT PRIMARY KEY,
    purpose         TEXT NOT NULL,                -- register | login
    user_id         TEXT REFERENCES users(user_id) ON DELETE CASCADE,
    created_at      TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    expires_at      TIMESTAMP NOT NULL,
    used_at         TIMESTAMP
);
CREATE INDEX IF NOT EXISTS webauthn_challenges_expiry ON webauthn_challenges (expires_at);

-- Invites to create an admin account (routers/invites.py): single use, 72 hours, revocable.
-- Only the SHA-256 of the token is kept, so a copy of this table can't accept one. email is
-- where it was sent and what the new account's email starts as; cleared once used.
-- Early-access invites for the volunteer portal (v18; migrate_v18.sql has the why).
CREATE TABLE IF NOT EXISTS signup_invites (
    invite_id    TEXT PRIMARY KEY,
    token_hash   TEXT NOT NULL UNIQUE,
    label        TEXT NOT NULL DEFAULT '',
    created_by   TEXT NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    expires_at   TIMESTAMP NOT NULL,
    revoked_at   TIMESTAMP,
    used_at      TIMESTAMP,
    used_by      TEXT,
    emailed_at   TIMESTAMP              -- v19: sent from Send invites; never the address
);

CREATE TABLE IF NOT EXISTS user_invites (
    invite_id    TEXT PRIMARY KEY,
    token_hash   TEXT NOT NULL UNIQUE,
    role         TEXT NOT NULL,
    email        TEXT,
    created_by   TEXT NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    expires_at   TIMESTAMP NOT NULL,
    used_at      TIMESTAMP,
    used_by      TEXT,
    revoked_at   TIMESTAMP
);

-- Password resets (routers/password_reset.py): single use, 60 minutes. Token hashed likewise.
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at   TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    expires_at   TIMESTAMP NOT NULL,
    used_at      TIMESTAMP,
    ip           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS password_resets_by_user ON password_resets (user_id);

CREATE TABLE IF NOT EXISTS app_kv (
    app_key  TEXT PRIMARY KEY,
    val      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS days_table (
    day_key     TEXT PRIMARY KEY,
    day_str     TEXT NOT NULL,
    event_date  TEXT
);

CREATE TABLE IF NOT EXISTS times_table (
    time_key  TEXT PRIMARY KEY,
    time_str  TEXT NOT NULL
);

-- Which times each day actually runs; a superset of the day/time pairs used in schedule.
CREATE TABLE IF NOT EXISTS day_times (
    day_key   TEXT NOT NULL REFERENCES days_table(day_key),
    time_key  TEXT NOT NULL REFERENCES times_table(time_key),
    PRIMARY KEY (day_key, time_key)
);

CREATE TABLE IF NOT EXISTS positions (
    pos_key  TEXT PRIMARY KEY,
    pos_cat  TEXT NOT NULL,
    pos_str  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS volunteers (
    vol_id     TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    time_pref  TEXT DEFAULT '',
    add_time   TEXT DEFAULT '',
    archived   BOOLEAN DEFAULT FALSE,
    email_enc  TEXT,
    -- Portal mirror (v7). email_hmac is the deterministic lookup index; the HMAC key that
    -- produces it lives in the environment, never in this database.
    status                TEXT NOT NULL DEFAULT 'active',   -- pending | active (approved: oriented)
    email_hmac            TEXT,
    email_key_fp          TEXT,
    add_any               BOOLEAN NOT NULL DEFAULT FALSE,
    answers               JSONB,
    possible_duplicate_of JSONB,
    created_at            TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'UTC'),
    -- Owed to the portal (v11): see migrate_v11.sql.
    portal_dirty          BOOLEAN NOT NULL DEFAULT FALSE,
    portal_rekey_from     TEXT,
    -- Separate from approval (v14; see migrate_v14.sql).
    cancelled_at          TIMESTAMP,                        -- volunteer cancelled, in the portal
    inactive_at           TIMESTAMP,                        -- organizer made inactive
    -- Who counts (v17; see migrate_v17.sql). Queries use these, never their own copy of the rule.
    is_live               BOOLEAN GENERATED ALWAYS AS
                              (cancelled_at IS NULL AND inactive_at IS NULL) STORED,
    is_counted            BOOLEAN GENERATED ALWAYS AS
                              (status IN ('pending', 'active')
                               AND cancelled_at IS NULL AND inactive_at IS NULL) STORED,
    is_assignable         BOOLEAN GENERATED ALWAYS AS
                              (status = 'active'
                               AND cancelled_at IS NULL AND inactive_at IS NULL) STORED
);
CREATE INDEX IF NOT EXISTS volunteers_portal_dirty ON volunteers (vol_id) WHERE portal_dirty;

CREATE UNIQUE INDEX IF NOT EXISTS volunteers_email_hmac
    ON volunteers (email_hmac) WHERE email_hmac IS NOT NULL;

-- Timeslot picks (v12; replaced bands, see migrate_v12.sql for why).
-- kind 'pref' is one of the two required picks (rank 1 or 2); 'add' is "also available if
-- needed" (rank NULL). Keyed to day_times, so only timeslots the schedule runs can be picked,
-- and removing a timeslot from a day removes its picks with it.
CREATE TABLE IF NOT EXISTS vol_picks (
    vol_id    TEXT NOT NULL REFERENCES volunteers(vol_id) ON DELETE CASCADE,
    day_key   TEXT NOT NULL,
    time_key  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    rank      INTEGER,
    PRIMARY KEY (vol_id, day_key, time_key, kind),
    FOREIGN KEY (day_key, time_key) REFERENCES day_times (day_key, time_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS vol_picks_by_slot ON vol_picks (day_key, time_key);

-- Assignments a volunteer has since dropped from their picks (v13; see migrate_v13.sql).
CREATE TABLE IF NOT EXISTS shift_flags (
    vol_id       TEXT NOT NULL REFERENCES volunteers(vol_id) ON DELETE CASCADE,
    day_key      TEXT NOT NULL,
    time_key     TEXT NOT NULL,
    flagged_at   TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    resolved_at  TIMESTAMP,
    resolved_by  TEXT,
    PRIMARY KEY (vol_id, day_key, time_key)
);

CREATE TABLE IF NOT EXISTS portal_sync_state (
    stream        TEXT PRIMARY KEY,
    last_rev      BIGINT,   -- NULL = no cursor (push streams never have one)
    last_ok_at    TIMESTAMP,
    last_error    TEXT,
    last_error_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vol_notes (
    note_id     TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    vol_id      TEXT NOT NULL REFERENCES volunteers(vol_id) ON DELETE CASCADE,
    note        TEXT NOT NULL,
    username    TEXT NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS schedule_notes (
    day_key     TEXT NOT NULL REFERENCES days_table(day_key),
    time_key    TEXT NOT NULL REFERENCES times_table(time_key),
    pos_key     TEXT NOT NULL REFERENCES positions(pos_key),
    note        TEXT NOT NULL,
    username    TEXT NOT NULL,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (day_key, time_key, pos_key)
);

CREATE TABLE IF NOT EXISTS schedule (
    day_key   TEXT NOT NULL REFERENCES days_table(day_key),
    time_key  TEXT NOT NULL REFERENCES times_table(time_key),
    pos_key   TEXT NOT NULL REFERENCES positions(pos_key),
    vol_id    TEXT REFERENCES volunteers(vol_id) ON DELETE SET NULL,
    PRIMARY KEY (day_key, time_key, pos_key)
);

CREATE TABLE IF NOT EXISTS categories (
    cat_name    TEXT PRIMARY KEY,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    cat_color   TEXT
);

-- Radio tracking: a barcode-identified radio, and a row per checkout.
-- Radios auto-register on first checkout, so there is no inventory step.
CREATE TABLE IF NOT EXISTS radios (
    barcode        TEXT PRIMARY KEY,
    first_seen_at  TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')
);

-- person_name is stored even when vol_id is set: the vol_id FK is ON DELETE SET NULL,
-- and a returned radio's history should still say who had it.
CREATE TABLE IF NOT EXISTS radio_checkouts (
    checkout_id     TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    barcode         TEXT NOT NULL REFERENCES radios(barcode) ON DELETE CASCADE,
    vol_id          TEXT REFERENCES volunteers(vol_id) ON DELETE SET NULL,
    person_name     TEXT NOT NULL,
    checked_out_at  TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    checked_out_by  TEXT NOT NULL,
    checked_in_at   TIMESTAMP,
    checked_in_by   TEXT
);

-- One radio can only be out once; enforced here rather than by a read-then-write.
CREATE UNIQUE INDEX IF NOT EXISTS radio_one_open_checkout
    ON radio_checkouts (barcode) WHERE checked_in_at IS NULL;

CREATE INDEX IF NOT EXISTS radio_checkouts_by_time
    ON radio_checkouts (checked_out_at DESC);

-- Read-only export API keys (routers/api_tokens.py, api_auth.py). Only the SHA-256 of the key
-- is kept, so a copy of this table can't call the API. scopes is space-separated;
-- allowed_ips is space-separated addresses or CIDRs, '' meaning any.
CREATE TABLE IF NOT EXISTS api_tokens (
    token_id      TEXT PRIMARY KEY,
    token_hash    TEXT NOT NULL UNIQUE,
    label         TEXT NOT NULL,
    scopes        TEXT NOT NULL,
    allowed_ips   TEXT NOT NULL DEFAULT '',
    created_by    TEXT NOT NULL,
    created_at    TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    expires_at    TIMESTAMP NOT NULL,
    revoked_at    TIMESTAMP,
    last_used_at  TIMESTAMP,
    last_used_ip  TEXT
);

-- The schedule email template (routers/email_template.py). One row, 'schedule'. The bodies
-- are Markdown; email_render.py turns them into the downloadable package for tools/vol_mail.
CREATE TABLE IF NOT EXISTS email_template (
    template_key  TEXT PRIMARY KEY,
    subject       TEXT NOT NULL DEFAULT '',
    body_md       TEXT NOT NULL DEFAULT '',
    shift_md      TEXT NOT NULL DEFAULT '',
    updated_by    TEXT,
    updated_at    TIMESTAMP
);

-- Images the template refers to as asset:<name>. Raster only (no SVG): many mail apps won't
-- show SVG.
CREATE TABLE IF NOT EXISTS email_assets (
    name          TEXT PRIMARY KEY,
    content_type  TEXT NOT NULL,
    data          BYTEA NOT NULL,
    sha256        TEXT NOT NULL,
    size          INTEGER NOT NULL,
    uploaded_by   TEXT NOT NULL,
    uploaded_at   TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')
);

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

COMMIT;
