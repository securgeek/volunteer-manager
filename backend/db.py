"""PostgreSQL access, schema migration and the session/role dependencies.

Every router talks to the database through `PgClient`, a small sqlite3-shaped wrapper over a
pooled psycopg2 connection. It rewrites `?` placeholders to `%s`, which is why the queries
throughout the backend still use `?`. Rows come back as dicts (`RealDictCursor`), so code reads
`row["col"]`, never `row[0]`.

Connection settings come from the `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` and
`DB_PASSWORD` environment variables. The schema is `scheduler.sql` for a fresh database, and
one file per release after 1.0 (`migrations/<release>.sql`) for an existing one, as listed in
`SCHEMA_RELEASES`.
"""
import os
from dataclasses import dataclass
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool
from fastapi import Request, Depends, HTTPException

DB_HOST     = os.getenv("DB_HOST",     "postgres")
DB_PORT     = int(os.getenv("DB_PORT", "5432"))
DB_NAME     = os.getenv("DB_NAME",     "scheduler")
DB_USER     = os.getenv("DB_USER",     "scheduler")
DB_PASSWORD = os.getenv("DB_PASSWORD", "changeme")

# The schema, release by release, oldest first: (release, schema version, file, in progress).
#
# 1.0 is the baseline: scheduler.sql in full, at schema version 19, where the pre-release
# patches left off. Each later release adds exactly one file under migrations/, named for the
# release (migrations/1.1.sql), applied once to a database older than it. Every change to the
# schema in that release goes into that one file, and into scheduler.sql too, which is always
# the complete current schema for a fresh install.
#
# While a release is in progress (True), its file is re-run on every start as well, so a
# development database picks up each addition. So it must be safe to run twice:
# CREATE ... IF NOT EXISTS, ADD COLUMN IF NOT EXISTS, ON CONFLICT DO NOTHING. Set it to False
# when the release is tagged (docs/operations/releasing.md).
SCHEMA_RELEASES = (
    ("1.0", 19, None, False),
    ("1.1", 20, "1.1.sql", True),       # audit_events, users.audit_access
)
BASELINE_VERSION = SCHEMA_RELEASES[0][1]
SCHEMA_VERSION = SCHEMA_RELEASES[-1][1]
_HERE = os.path.dirname(os.path.abspath(__file__))



@dataclass
class _DbState:
    """Module state: the connection pool, made on first use."""

    pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


_state = _DbState()

_session_cache: dict          = {}


# ── connection helpers ────────────────────────────────────────────────────────

def _dsn() -> dict:
    return dict(host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                user=DB_USER, password=DB_PASSWORD,
                cursor_factory=psycopg2.extras.RealDictCursor)


def _pool() -> psycopg2.pool.ThreadedConnectionPool:
    """The connection pool, created on first use from the current DB_* settings."""
    if _state.pool is None:
        _state.pool = psycopg2.pool.ThreadedConnectionPool(1, 20, **_dsn())
    return _state.pool


def close_pool() -> None:
    """Close every pooled connection and forget the pool; the next use makes a new one.

    For tests, which point DB_* at a throwaway database before the first connection.
    """
    if _state.pool is not None:
        _state.pool.closeall()
        _state.pool = None


# ── migration ─────────────────────────────────────────────────────────────────

def _strip_comment(line: str) -> str:
    """Drop a trailing `--` comment, ignoring `--` inside a single-quoted literal."""
    in_str = False
    for i, ch in enumerate(line):
        if ch == "'":
            in_str = not in_str
        elif not in_str and line.startswith('--', i):
            return line[:i]
    return line


def _apply_sql_file(conn, path: str):
    """Execute a SQL file statement by statement, tolerating already-exists errors."""
    with open(path, encoding="utf-8") as f:
        sql = f.read()
    # Strip comments first: a leading comment would otherwise share a split chunk with the
    # following statement, and a ';' inside any comment would split a statement in half.
    sql = '\n'.join(_strip_comment(l) for l in sql.splitlines())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for stmt in sql.split(';'):
                stmt = stmt.strip()
                if not stmt:
                    continue
                try:
                    cur.execute(stmt)
                except psycopg2.Error as e:
                    msg = str(e).lower()
                    if 'already exists' not in msg and 'duplicate column' not in msg:
                        raise
    finally:
        conn.autocommit = False


def _schema_version(conn) -> int:
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT MAX(version) AS v FROM schema_versions")
            row = cur.fetchone()
            version = row["v"] if row and row["v"] is not None else 0
        except psycopg2.Error:
            version = 0
    # Either way, end the transaction the SELECT opened: _apply_sql_file sets
    # autocommit, which psycopg2 refuses to do inside an open transaction.
    conn.rollback()
    return version


def migrate(conn):
    """Bring the schema up to `SCHEMA_VERSION`.

    A fresh database (version 0) gets `scheduler.sql`, which is the whole current schema. An
    existing one gets each release file in `SCHEMA_RELEASES` newer than its recorded version,
    in order, plus the file of a release still in progress (re-run on every start), and then
    records the new version.

    Args:
        conn: A raw psycopg2 connection (not a `PgClient`). Committed on success.

    Raises:
        RuntimeError: A database from before 1.0 (older than `BASELINE_VERSION`), which the
            per-release files can't upgrade.
    """
    version = _schema_version(conn)
    if version == 0:
        _apply_sql_file(conn, os.path.join(_HERE, 'scheduler.sql'))
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schema_versions (version) VALUES (%s) ON CONFLICT DO NOTHING;",
                (SCHEMA_VERSION,)
            )
        conn.commit()
        return
    if version < BASELINE_VERSION:
        raise RuntimeError(
            f"This database is at schema version {version}, from before release 1.0 "
            f"(version {BASELINE_VERSION}). Upgrade it with a pre-1.0 build first, or dump and "
            "restore its data into a fresh 1.0 database.")
    for _release, release_version, path, in_progress in SCHEMA_RELEASES[1:]:
        if release_version > version or (in_progress and release_version == version):
            _apply_sql_file(conn, os.path.join(_HERE, 'migrations', path))
    if version < SCHEMA_VERSION:
        # Record the new version so the files above don't run again on the next start.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schema_versions (version) VALUES (%s) ON CONFLICT DO NOTHING;",
                (SCHEMA_VERSION,)
            )
        conn.commit()


# ── PgClient — sqlite3-compatible interface over psycopg2 ─────────────────────

class _Cursor:
    def __init__(self, cur):
        self._cur = cur

    @property
    def rowcount(self):
        """How many rows the statement changed or returned."""
        return self._cur.rowcount

    def fetchone(self):
        """The next row as a dict, or `None` when there is none or the statement returns no rows."""
        try:
            return self._cur.fetchone()
        except psycopg2.ProgrammingError:
            return None

    def fetchall(self):
        """The remaining rows as dicts. `[]` when the statement returns no rows."""
        try:
            return self._cur.fetchall()
        except psycopg2.ProgrammingError:
            return []


class PgClient:
    """A sqlite3-shaped wrapper around one psycopg2 connection.

    Lets the backend write `db.execute("... WHERE x = ?", (x,)).fetchone()` as it did on SQLite.
    `?` becomes `%s` before the statement reaches psycopg2, and the values are always bound
    parameters.

    Args:
        conn: The psycopg2 connection to wrap. `PgClient` does not own it: the pool does.
    """
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params: tuple = ()) -> _Cursor:
        """Run one statement with bound parameters.

        Args:
            sql: A complete literal statement using `?` placeholders.
            params: The values for the placeholders, in order.

        Returns:
            A cursor with `fetchone`, `fetchall` and `rowcount`. Fetching from a statement that
            returns no rows gives `None` or `[]` rather than raising.
        """
        pg_sql = sql.replace('?', '%s')
        cur = self._conn.cursor()
        cur.execute(pg_sql, params or None)
        return _Cursor(cur)

    def commit(self):
        """Commit the current transaction."""
        self._conn.commit()

    def close(self):
        """Do nothing: the pool owns the connection and `get_db` returns it."""
        pass


def open_db() -> PgClient:
    """Open a standalone connection, outside the pool, and migrate it.

    Used at startup and by scripts. Request handlers use `get_db` instead.

    Returns:
        A `PgClient` on a new connection with the schema up to date.
    """
    conn = psycopg2.connect(**_dsn())
    migrate(conn)
    return PgClient(conn)


def get_db():
    """FastAPI dependency: borrow a pooled connection for one request.

    FastAPI caches the dependency per request, so `get_session_data` and the endpoint share the
    same connection. Anything left uncommitted when the request ends is rolled back before the
    connection goes back to the pool.

    Yields:
        A `PgClient` on the borrowed connection.
    """
    pool = _pool()
    conn = pool.getconn()
    try:
        yield PgClient(conn)
    finally:
        conn.rollback()
        pool.putconn(conn)


def get_conn() -> PgClient:
    """Borrow a pooled connection outside a request.

    get_db is a FastAPI generator dependency and cannot be used by the portal sync loop, which
    is a background task with no request to hang off. Pair every call with put_conn.
    """
    return PgClient(_pool().getconn())


def put_conn(client: "PgClient") -> None:
    """Return a connection borrowed with get_conn, rolling back anything left open.

    Args:
        client: The client `get_conn` returned. Don't use it afterwards.
    """
    conn = client._conn
    try:
        conn.rollback()
    finally:
        _pool().putconn(conn)


# ── auth helpers ──────────────────────────────────────────────────────────────

def get_session_data(
    request: Request,
    db: PgClient = Depends(get_db),
) -> Optional[dict]:
    """FastAPI dependency: the signed-in organizer for this request, if any.

    Looks the cookie's `session_id` up in `_session_cache` first and the `sessions` table second,
    caching what it finds. A session id with no row (expired or signed out elsewhere) is dropped
    from the cookie.

    Args:
        request: The incoming request, whose session cookie holds `session_id`.
        db: The request's database connection.

    Returns:
        `{"user_id", "username", "role"}`, or `None` when nobody is signed in.
    """
    s_id = request.session.get("session_id")
    if not s_id:
        return None
    if s_id in _session_cache:
        return _session_cache[s_id]
    row = db.execute(
        "SELECT s.user_id, u.username, u.role FROM sessions s "
        "LEFT JOIN users u ON s.user_id = u.user_id WHERE s.session_id = ?;",
        (s_id,)
    ).fetchone()
    if row:
        s_data = {"user_id": row["user_id"], "username": row["username"],
                  "role": row["role"] or "organizer"}
        _session_cache[s_id] = s_data
        return s_data
    request.session.pop("session_id", None)
    return None


class RoleChecker:
    """FastAPI dependency that admits only the given roles.

    Use it as `Depends(RoleChecker("organizer"))` or `Depends(RoleChecker("organizer",
    "checkin"))`. Calling it raises 401 with no session and 403 for any other role, and
    otherwise returns the session data.

    Args:
        *roles: The roles allowed through: any of `organizer`, `checkin` and `view`.
    """
    def __init__(self, *roles):
        self.roles = set(roles)

    def __call__(self, s_data: Optional[dict] = Depends(get_session_data)) -> dict:
        if not s_data:
            raise HTTPException(status_code=401, detail="Unauthorized")
        if s_data.get("role") not in self.roles:
            raise HTTPException(status_code=403, detail="Forbidden")
        return s_data


def get_app_kv(db: PgClient, key: str, default_factory=None) -> Optional[str]:
    """Read one value from the `app_kv` table, optionally creating it.

    Args:
        db: The database connection.
        key: The `app_key` to read.
        default_factory: Called to make the value when the key doesn't exist yet. The new value
            is stored and committed. Leave it out to just read.

    Returns:
        The stored value, the newly created one, or `None` when the key is missing and there
        is no factory.
    """
    row = db.execute("SELECT val FROM app_kv WHERE app_key = ?;", (key,)).fetchone()
    if row:
        return row["val"]
    if default_factory is not None:
        val = default_factory()
        db.execute("INSERT INTO app_kv (app_key, val) VALUES (?, ?);", (key, val))
        db.commit()
        return val
    return None
