"""db.migrate and SCHEMA_RELEASES: the 1.0 baseline, and one file per release after it.

Against a real Postgres, like test_export.py, and it DROPS the target database's public schema,
so point TEST_PG_* at a throwaway one.

    TEST_PG_HOST=127.0.0.1 TEST_PG_PORT=55432 pytest test_schema_releases.py -v
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

if not os.getenv("TEST_PG_HOST"):
    pytest.skip("needs a throwaway Postgres: set TEST_PG_HOST", allow_module_level=True)

import db as dbm  # noqa: E402


@pytest.fixture
def pg(monkeypatch):
    """A connection like the one open_db gives migrate, on a freshly emptied schema."""
    import psycopg2
    monkeypatch.setattr(dbm, "DB_HOST", os.environ["TEST_PG_HOST"])
    monkeypatch.setattr(dbm, "DB_PORT", int(os.getenv("TEST_PG_PORT", "5432")))
    monkeypatch.setattr(dbm, "DB_NAME", os.getenv("TEST_PG_DB", "scheduler_test"))
    monkeypatch.setattr(dbm, "DB_USER", os.getenv("TEST_PG_USER", "scheduler"))
    monkeypatch.setattr(dbm, "DB_PASSWORD", os.getenv("TEST_PG_PASSWORD", ""))
    dbm.close_pool()
    conn = psycopg2.connect(**dbm._dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.autocommit = False
    yield conn
    conn.close()


def _version(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(version) AS v FROM schema_versions")
        version = cur.fetchone()["v"]
    conn.rollback()
    return version


def _release_file(tmp_path, sql):
    path = tmp_path / "1.1.sql"
    path.write_text(sql, encoding="utf-8")
    return str(path)        # absolute, so os.path.join(_HERE, "migrations", path) is it


def test_the_baseline_is_release_1_0():
    assert dbm.SCHEMA_RELEASES[0][:3] == ("1.0", 19, None)
    assert dbm.BASELINE_VERSION == 19
    assert dbm.SCHEMA_VERSION == dbm.SCHEMA_RELEASES[-1][1]
    versions = [r[1] for r in dbm.SCHEMA_RELEASES]
    assert versions == sorted(versions) and len(set(versions)) == len(versions)
    # Every release after the baseline has its file, and only the newest may be in progress.
    for release, _version_, name, in_progress in dbm.SCHEMA_RELEASES[1:]:
        assert name == f"{release}.sql"
        assert os.path.exists(os.path.join(_HERE, "migrations", name)), name
    assert not any(r[3] for r in dbm.SCHEMA_RELEASES[:-1])


def test_a_fresh_database_gets_the_whole_schema_and_the_current_version(pg):
    dbm.migrate(pg)
    assert _version(pg) == dbm.SCHEMA_VERSION
    with pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'volunteers'")
        assert cur.fetchone()["n"] == 1
    pg.rollback()
    dbm.migrate(pg)                     # a second start changes nothing
    assert _version(pg) == dbm.SCHEMA_VERSION


def test_a_database_from_before_1_0_is_refused(pg):
    dbm.migrate(pg)
    with pg.cursor() as cur:
        cur.execute("DELETE FROM schema_versions")
        cur.execute("INSERT INTO schema_versions (version) VALUES (18)")
    pg.commit()
    with pytest.raises(RuntimeError, match="before release 1.0"):
        dbm.migrate(pg)


def _only_1_0(monkeypatch):
    """Make the code believe 1.0 is the only release, so a test can add its own after it."""
    monkeypatch.setattr(dbm, "SCHEMA_RELEASES", dbm.SCHEMA_RELEASES[:1])
    monkeypatch.setattr(dbm, "SCHEMA_VERSION", dbm.BASELINE_VERSION)


def test_a_release_file_runs_once_and_again_while_in_progress(pg, tmp_path, monkeypatch):
    _only_1_0(monkeypatch)
    dbm.migrate(pg)                                         # a 1.0 database
    path = _release_file(tmp_path, """
        -- One of 1.1's changes; safe to run twice, as a release in progress must be.
        CREATE TABLE IF NOT EXISTS release_probe (n SERIAL PRIMARY KEY);
        INSERT INTO release_probe DEFAULT VALUES;
    """)

    def rows():
        with pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM release_probe")
            n = cur.fetchone()["n"]
        pg.rollback()
        return n

    in_progress = dbm.SCHEMA_RELEASES + (("1.1", 20, path, True),)
    monkeypatch.setattr(dbm, "SCHEMA_RELEASES", in_progress)
    monkeypatch.setattr(dbm, "SCHEMA_VERSION", 20)
    dbm.migrate(pg)
    assert _version(pg) == 20 and rows() == 1
    dbm.migrate(pg)                                         # re-run while in progress
    assert rows() == 2

    released = dbm.SCHEMA_RELEASES[:-1] + (("1.1", 20, path, False),)
    monkeypatch.setattr(dbm, "SCHEMA_RELEASES", released)
    dbm.migrate(pg)                                         # tagged: never again
    assert rows() == 2 and _version(pg) == 20


def test_a_1_0_database_is_upgraded_through_every_newer_release(pg, tmp_path, monkeypatch):
    _only_1_0(monkeypatch)
    dbm.migrate(pg)
    a = tmp_path / "a.sql"
    a.write_text("CREATE TABLE IF NOT EXISTS probe_a (x INT);", encoding="utf-8")
    b = tmp_path / "b.sql"
    b.write_text("ALTER TABLE probe_a ADD COLUMN IF NOT EXISTS y INT;", encoding="utf-8")
    monkeypatch.setattr(dbm, "SCHEMA_RELEASES", dbm.SCHEMA_RELEASES + (
        ("1.1", 20, str(a), False), ("1.2", 21, str(b), False)))
    monkeypatch.setattr(dbm, "SCHEMA_VERSION", 21)
    dbm.migrate(pg)
    with pg.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'probe_a' ORDER BY column_name")
        assert [r["column_name"] for r in cur.fetchall()] == ["x", "y"]
    pg.rollback()
    assert _version(pg) == 21


# ── the real releases ─────────────────────────────────────────────────────────

def _tables(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'")
        names = {r["table_name"] for r in cur.fetchall()}
    conn.rollback()
    return names


def test_1_1_adds_audit_events_to_a_1_0_database(pg):
    dbm.migrate(pg)
    # Wind it back to how a 1.0 database looks: no audit_events, version 19.
    with pg.cursor() as cur:
        cur.execute("DROP TABLE audit_events")
        cur.execute("DELETE FROM schema_versions")
        cur.execute("INSERT INTO schema_versions (version) VALUES (19)")
    pg.commit()
    assert "audit_events" not in _tables(pg)
    dbm.migrate(pg)
    assert "audit_events" in _tables(pg) and _version(pg) == 20
    dbm.migrate(pg)                                         # in progress: re-runs cleanly
    assert _version(pg) == 20


def test_1_1_grants_audit_access_to_the_earliest_organizer_once(pg):
    dbm.migrate(pg)
    with pg.cursor() as cur:
        for uid, role, when in (("u-view", "view", "2025-01-01"), ("u-org2", "organizer",
                                "2025-03-01"), ("u-org1", "organizer", "2025-02-01")):
            cur.execute("INSERT INTO users (user_id, username, password_hash, role, created_at) "
                        "VALUES (%s, %s, 'x', %s, %s)", (uid, uid, role, when))
        # Wind back to 1.0: no audit_access column.
        cur.execute("ALTER TABLE users DROP COLUMN audit_access")
        cur.execute("DELETE FROM schema_versions")
        cur.execute("INSERT INTO schema_versions (version) VALUES (19)")
    pg.commit()

    def holders():
        with pg.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE audit_access ORDER BY user_id")
            ids = [r["user_id"] for r in cur.fetchall()]
        pg.rollback()
        return ids

    dbm.migrate(pg)
    assert holders() == ["u-org1"]
    with pg.cursor() as cur:                     # u-org1 hands it to u-org2 and drops it
        cur.execute("UPDATE users SET audit_access = (user_id = 'u-org2')")
    pg.commit()
    dbm.migrate(pg)                              # in progress: re-runs, grants nothing new
    assert holders() == ["u-org2"]
