"""SQLite access for the portal.

Deliberately plain: stdlib ``sqlite3``, one connection per request, ``?`` placeholders and
``row["col"]`` access — the same idiom as the scheduler's ``PgClient`` shim, so query code
reads the same in both services even though they share no database.

The pragmas are not optional. ``foreign_keys`` is off by default in SQLite and this schema
relies on it; WAL is what keeps a reader from blocking behind the writer; ``busy_timeout``
turns a momentary write collision into a wait rather than an exception.
"""
import os
import sqlite3
from dataclasses import dataclass
from typing import Iterator, Optional

# The schema, release by release, oldest first: (release, schema version, file, in progress).
#
# 1.0 is the baseline: portal.sql in full, at PRAGMA user_version 7, where the pre-release
# patches left off. Each later release adds exactly one file under migrations/, named for the
# release (migrations/1.1.sql), applied once to a database older than it. Every schema change
# in that release goes into that one file, and into portal.sql too, which is always the
# complete current schema for a fresh install. The scheduler's versions are separate.
#
# While a release is in progress (True), its file is re-run on every start as well, so a
# development database picks up each addition. So it must be safe to run twice: CREATE ... IF
# NOT EXISTS; an ADD COLUMN that's already there is skipped (SQLite has no IF NOT EXISTS for
# columns). Set it to False when the release is tagged (docs/operations/releasing.md).
SCHEMA_RELEASES = (
    ("1.0", 7, None, False),
)
BASELINE_VERSION = SCHEMA_RELEASES[0][1]
PORTAL_SCHEMA_VERSION = SCHEMA_RELEASES[-1][1]

_HERE = os.path.dirname(os.path.abspath(__file__))


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with the portal's required pragmas applied.

    Args:
        db_path: The SQLite file. Its directory is created if missing.

    Returns:
        A connection in autocommit mode (transactions are explicit `BEGIN`s) with WAL,
        foreign keys and a 5 s busy timeout, and rows as `sqlite3.Row`.
    """
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # check_same_thread=False because FastAPI runs sync dependencies in a threadpool while
    # async endpoints run on the event loop, so one request's connection legitimately crosses
    # threads. It is safe here precisely because each request opens its own connection and
    # never shares it: the access is sequential, never concurrent.
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous  = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _apply_sql_file(conn: sqlite3.Connection, path: str) -> None:
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _strip_comment(line: str) -> str:
    """Drop a trailing ``--`` comment, ignoring ``--`` inside a single-quoted literal."""
    in_str = False
    for i, ch in enumerate(line):
        if ch == "'":
            in_str = not in_str
        elif not in_str and line.startswith("--", i):
            return line[:i]
    return line


def _apply_release_file(conn: sqlite3.Connection, path: str) -> None:
    """Run a release's file statement by statement, skipping what's already there.

    A release in progress is re-run on every start, and SQLite can't say ``ADD COLUMN IF NOT
    EXISTS``, so "duplicate column" and "already exists" are taken as done. Statements are
    split on ``;``, so a release file must not hold a trigger or anything else with one inside.

    Args:
        conn: The connection, in autocommit mode as ``connect`` opens it.
        path: The release's file.
    """
    with open(path, encoding="utf-8") as f:
        sql = "\n".join(_strip_comment(line) for line in f.read().splitlines())
    for stmt in sql.split(";"):
        if not stmt.strip():
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column" not in msg and "already exists" not in msg:
                raise


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the database up to PORTAL_SCHEMA_VERSION. Returns the version now in force.

    A fresh database gets ``portal.sql``, the whole current schema. An existing one gets each
    release file in ``SCHEMA_RELEASES`` newer than its version, in order, plus the file of a
    release still in progress (re-run on every start). The version lives in
    ``PRAGMA user_version``, entirely independent of the scheduler's ``schema_versions``: the
    two services version their schemas separately because they are separate applications.

    Args:
        conn: The connection to migrate.

    Raises:
        RuntimeError: A database from before 1.0, which the per-release files can't upgrade,
            or a release file that's missing.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        _apply_sql_file(conn, os.path.join(_HERE, "portal.sql"))
        conn.execute(f"PRAGMA user_version = {PORTAL_SCHEMA_VERSION}")
        return PORTAL_SCHEMA_VERSION
    if version < BASELINE_VERSION:
        raise RuntimeError(
            f"portal: this database is at schema version {version}, from before release 1.0 "
            f"(version {BASELINE_VERSION}). Upgrade it with a pre-1.0 build first, or start a "
            "fresh one.")
    for _release, release_version, name, in_progress in SCHEMA_RELEASES[1:]:
        if release_version > version or (in_progress and release_version == version):
            path = os.path.join(_HERE, "migrations", name)
            if not os.path.exists(path):
                raise RuntimeError(f"portal: missing release file {path}")
            _apply_release_file(conn, path)
    if version < PORTAL_SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {PORTAL_SCHEMA_VERSION}")
    return max(version, PORTAL_SCHEMA_VERSION)


def next_rev(conn: sqlite3.Connection) -> int:
    """Bump and return the global revision counter.

    Call this inside the same transaction as the mutation it stamps, so a pull can never
    see a row whose rev implies it was already sent.

    Args:
        conn: The connection, inside the mutation's transaction.
    """
    row = conn.execute("UPDATE sync_rev SET rev = rev + 1 WHERE id = 1 RETURNING rev").fetchone()
    return row["rev"]


def current_rev(conn: sqlite3.Connection) -> int:
    """The global revision counter, without bumping it.

    Args:
        conn: The database connection.

    Returns:
        The latest revision handed out, or 0.
    """
    row = conn.execute("SELECT rev FROM sync_rev WHERE id = 1").fetchone()
    return row["rev"] if row else 0


def kv_get(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    """Read one value from `portal_kv`.

    Args:
        conn: The database connection.
        key: The key to read.
        default: What to return when the key isn't set.

    Returns:
        The stored value, or `default`.
    """
    row = conn.execute("SELECT v FROM portal_kv WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, val: str) -> None:
    """Set one value in `portal_kv`, replacing any old one.

    Args:
        conn: The database connection. Not committed here.
        key: The key to set.
        val: The new value.
    """
    conn.execute(
        "INSERT INTO portal_kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, val),
    )


def audit(conn: sqlite3.Connection, action: str, actor_vol_id: Optional[str] = None,
          detail: str = "") -> None:
    """Append to the portal audit log. Never pass an email address as detail.

    Args:
        conn: The database connection, usually inside the change's own transaction.
        action: A dotted event name such as `volunteer.purged`.
        actor_vol_id: The volunteer who acted, or concerned, when there is one.
        detail: Free text. Never an address or token.
    """
    from clock import now_iso
    conn.execute(
        "INSERT INTO portal_audit (ts, actor_vol_id, action, detail) VALUES (?, ?, ?, ?)",
        (now_iso(), actor_vol_id, action, detail),
    )


# ── FastAPI dependency ────────────────────────────────────────────────────────

@dataclass
class _DbConfig:
    """Module state: the database path, set once by configure()."""

    path: Optional[str] = None


_config = _DbConfig()


def configure(db_path: str) -> None:
    """Set the database path used by get_db, and migrate it once at startup.

    Args:
        db_path: The SQLite file, from `PORTAL_DB_PATH`.
    """
    _config.path = db_path
    conn = connect(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """Per-request connection. FastAPI caches it per request, so nested Depends share one."""
    if _config.path is None:
        raise RuntimeError("db.configure() was never called")
    conn = connect(_config.path)
    try:
        yield conn
    finally:
        conn.close()
