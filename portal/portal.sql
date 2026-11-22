-- Portal schema — version 3
-- SQLite. Applied on a fresh database (PRAGMA user_version = 0).
--
-- Nothing in this file is a secret that unlocks anything else in it: the email HMAC key
-- lives in the environment (see config.py), never here, so a stolen copy of this database
-- yields hashes and ciphertext but nothing that reads either.

CREATE TABLE IF NOT EXISTS portal_kv (
    k  TEXT PRIMARY KEY,
    v  TEXT NOT NULL
);

-- One monotonic counter, bumped inside the same transaction as any mutation and stamped
-- on every row it touches. Sync cursors are therefore a single integer per stream.
CREATE TABLE IF NOT EXISTS sync_rev (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    rev  INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO sync_rev (id, rev) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS volunteers (
    vol_id                TEXT PRIMARY KEY,
    first_name            TEXT NOT NULL DEFAULT '',
    last_name             TEXT NOT NULL DEFAULT '',
    nickname              TEXT NOT NULL DEFAULT '',
    name                  TEXT NOT NULL DEFAULT '',
    status                TEXT NOT NULL DEFAULT 'pending',   -- pending | active (approved: oriented)
    cancelled_at          TEXT,                              -- volunteer cancelled (v3)
    inactive_at           TEXT,                              -- organizer made inactive (v3)
    time_pref             TEXT NOT NULL DEFAULT '',          -- derived from vol_picks
    add_time              TEXT NOT NULL DEFAULT '',          -- derived from vol_picks
    add_any               INTEGER NOT NULL DEFAULT 0,
    email_hmac            TEXT,
    email_enc             TEXT,
    email_key_fp          TEXT,
    answers               TEXT,                              -- JSON
    possible_duplicate_of TEXT,                              -- JSON list of vol_id
    invite_id             TEXT,                              -- early-access invite used (v5)
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    rev                   INTEGER NOT NULL DEFAULT 0
);

-- The actual duplicate-signup guarantee. check_email is only UX; this is what holds when
-- two people submit the same address in the same second.
CREATE UNIQUE INDEX IF NOT EXISTS volunteers_email_hmac
    ON volunteers (email_hmac) WHERE email_hmac IS NOT NULL;
CREATE INDEX IF NOT EXISTS volunteers_by_rev ON volunteers (rev);

-- Only the SHA-256 of the token is stored: a read of this table must not mint a login.
CREATE TABLE IF NOT EXISTS login_tokens (
    token_hash    TEXT PRIMARY KEY,
    purpose       TEXT NOT NULL,          -- login | signup
    vol_id        TEXT,                   -- NULL for a signup token
    email_hmac    TEXT NOT NULL,
    email_enc     TEXT,
    email_key_fp  TEXT,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    used_at       TEXT,
    ip            TEXT NOT NULL DEFAULT '',
    ua            TEXT NOT NULL DEFAULT '',
    invite_id     TEXT                    -- v5: the early-access invite this signup came with
);
CREATE INDEX IF NOT EXISTS login_tokens_by_vol   ON login_tokens (vol_id);
CREATE INDEX IF NOT EXISTS login_tokens_by_hmac  ON login_tokens (email_hmac);
CREATE INDEX IF NOT EXISTS login_tokens_expiry   ON login_tokens (expires_at);

-- Likewise hashed. vol_id IS NULL marks a signup session, which authorises exactly one
-- endpoint (the signup submit) and nothing else.
CREATE TABLE IF NOT EXISTS sessions (
    sid_hash      TEXT PRIMARY KEY,
    vol_id        TEXT,
    email_hmac    TEXT NOT NULL,
    email_enc     TEXT,
    email_key_fp  TEXT,
    remember      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    last_seen_at  TEXT,
    revoked_at    TEXT,
    ip            TEXT NOT NULL DEFAULT '',
    ua            TEXT NOT NULL DEFAULT '',
    invite_id     TEXT                    -- v5: carried from the login token
);
CREATE INDEX IF NOT EXISTS sessions_by_vol    ON sessions (vol_id);
CREATE INDEX IF NOT EXISTS sessions_expiry    ON sessions (expires_at);

CREATE TABLE IF NOT EXISTS rate_limits (
    bucket_key    TEXT PRIMARY KEY,
    window_start  INTEGER NOT NULL,
    count         INTEGER NOT NULL DEFAULT 0
);

-- ── pushed from the scheduler ────────────────────────────────────────────────
-- Everything below is a copy. The portal never edits these; it only reads them.

CREATE TABLE IF NOT EXISTS snapshot_meta (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    snapshot_rev  INTEGER NOT NULL DEFAULT 0,
    content_hash  TEXT,
    pushed_at     TEXT
);
INSERT OR IGNORE INTO snapshot_meta (id, snapshot_rev) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS snapshot_days (
    day_key     TEXT PRIMARY KEY,
    day_str     TEXT NOT NULL,
    event_date  TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS snapshot_times (
    time_key    TEXT PRIMARY KEY,
    time_str    TEXT NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

-- remaining is pushed by the scheduler (overage included); the portal decrements it locally
-- between pushes.
CREATE TABLE IF NOT EXISTS snapshot_day_times (
    day_key   TEXT NOT NULL,
    time_key  TEXT NOT NULL,
    remaining INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day_key, time_key)
);

CREATE TABLE IF NOT EXISTS snapshot_categories (
    cat_name    TEXT PRIMARY KEY,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    cat_color   TEXT
);

CREATE TABLE IF NOT EXISTS snapshot_positions (
    pos_key  TEXT PRIMARY KEY,
    pos_cat  TEXT NOT NULL DEFAULT '',
    pos_str  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS snapshot_slots (
    day_key   TEXT NOT NULL,
    time_key  TEXT NOT NULL,
    pos_key   TEXT NOT NULL,
    vol_id    TEXT,
    PRIMARY KEY (day_key, time_key, pos_key)
);
CREATE INDEX IF NOT EXISTS snapshot_slots_by_vol ON snapshot_slots (vol_id);

-- Timeslot picks (v2; replaced bands).
-- kind 'pref' is one of the two required picks (rank 1 or 2); 'add' is "also available if
-- needed" (rank NULL).
CREATE TABLE IF NOT EXISTS vol_picks (
    vol_id    TEXT NOT NULL,
    day_key   TEXT NOT NULL,
    time_key  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    rank      INTEGER,
    PRIMARY KEY (vol_id, day_key, time_key, kind)
);
CREATE INDEX IF NOT EXISTS vol_picks_by_slot ON vol_picks (day_key, time_key);

CREATE TABLE IF NOT EXISTS branding (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    app_title     TEXT,
    portal_title  TEXT,
    logo          BLOB,
    logo_mime     TEXT,
    favicon       BLOB,
    header_logo      BLOB,   -- v4: upper-left logo; logo is the year's theme logo
    header_logo_mime TEXT,
    contact_email       TEXT,   -- v6: the conference's public contact address and links
    conference_url      TEXT,
    code_of_conduct_url TEXT,
    content_hash  TEXT,
    pushed_at     TEXT
);

-- v7: organizer-editable content (migrate_v7.sql). NULL = content.py's default.
CREATE TABLE IF NOT EXISTS portal_content (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    landing       TEXT,
    questionnaire TEXT,
    invite        TEXT,
    content_hash  TEXT,
    pushed_at     TEXT
);

CREATE TABLE IF NOT EXISTS announcements (
    ann_id     TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    starts_at  TEXT,
    ends_at    TEXT,
    rev        INTEGER NOT NULL DEFAULT 0
);

-- ── sync plumbing ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sync_nonces (
    nonce    TEXT PRIMARY KEY,
    seen_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sync_nonces_seen ON sync_nonces (seen_at);

CREATE TABLE IF NOT EXISTS sync_ops (
    op_id       TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    result      TEXT NOT NULL   -- JSON, replayed verbatim for a duplicate op_id
);

-- vol_ids and actions, never addresses.
CREATE TABLE IF NOT EXISTS portal_audit (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    actor_vol_id   TEXT,
    action         TEXT NOT NULL,
    detail         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS portal_audit_ts ON portal_audit (ts);

-- v5: early-access invites (migrate_v5.sql has the why).
CREATE TABLE IF NOT EXISTS signup_invites (
    invite_id   TEXT PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT,
    used_at     TEXT
);
