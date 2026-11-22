"""db.migrate and SCHEMA_RELEASES on the portal: the 1.0 baseline, and one file per release.

No server needed: each test opens its own SQLite file.

    pytest portal/test_portal_schema.py -v
"""
import importlib
import os
import sys

import pytest


@pytest.fixture
def dbm():
    sys.modules.pop("db", None)          # the portal's db, whatever ran before
    return importlib.import_module("db")


def _probe_rows(conn):
    return conn.execute("SELECT COUNT(*) FROM release_probe").fetchone()[0]


def test_the_baseline_is_release_1_0(dbm):
    assert dbm.SCHEMA_RELEASES[0][:3] == ("1.0", 7, None)
    assert dbm.BASELINE_VERSION == 7
    assert dbm.PORTAL_SCHEMA_VERSION == dbm.SCHEMA_RELEASES[-1][1]
    for release, _version, name, _in_progress in dbm.SCHEMA_RELEASES[1:]:
        assert name == f"{release}.sql"
        assert os.path.exists(os.path.join(dbm._HERE, "migrations", name)), name
    assert not any(r[3] for r in dbm.SCHEMA_RELEASES[:-1])


def test_a_fresh_database_is_the_current_schema(dbm, tmp_path):
    conn = dbm.connect(str(tmp_path / "p.db"))
    assert dbm.migrate(conn) == dbm.PORTAL_SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"volunteers", "branding", "portal_content", "signup_invites"} <= tables


def test_a_database_from_before_1_0_is_refused(dbm, tmp_path):
    conn = dbm.connect(str(tmp_path / "p.db"))
    dbm.migrate(conn)
    conn.execute("PRAGMA user_version = 6")
    with pytest.raises(RuntimeError, match="before release 1.0"):
        dbm.migrate(conn)


def test_a_release_file_runs_once_and_again_while_in_progress(dbm, tmp_path, monkeypatch):
    conn = dbm.connect(str(tmp_path / "p.db"))
    dbm.migrate(conn)                                       # a 1.0 database
    path = tmp_path / "1.1.sql"
    # ADD COLUMN has no IF NOT EXISTS in SQLite: the re-run skips it as already done.
    path.write_text("""
        -- 1.1's changes; a ; in a comment doesn't split anything
        CREATE TABLE IF NOT EXISTS release_probe (n INTEGER PRIMARY KEY);
        ALTER TABLE branding ADD COLUMN release_note TEXT;
        INSERT INTO release_probe DEFAULT VALUES;
    """, encoding="utf-8")
    monkeypatch.setattr(dbm, "SCHEMA_RELEASES",
                        dbm.SCHEMA_RELEASES + (("1.1", 8, str(path), True),))
    monkeypatch.setattr(dbm, "PORTAL_SCHEMA_VERSION", 8)
    assert dbm.migrate(conn) == 8 and _probe_rows(conn) == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    dbm.migrate(conn)                                       # in progress: runs again
    assert _probe_rows(conn) == 2
    assert "release_note" in {r[1] for r in conn.execute("PRAGMA table_info(branding)")}
    monkeypatch.setattr(dbm, "SCHEMA_RELEASES",
                        dbm.SCHEMA_RELEASES[:-1] + (("1.1", 8, str(path), False),))
    dbm.migrate(conn)                                       # tagged: never again
    assert _probe_rows(conn) == 2


def test_a_real_error_in_a_release_file_still_stops_the_start(dbm, tmp_path, monkeypatch):
    conn = dbm.connect(str(tmp_path / "p.db"))
    dbm.migrate(conn)
    path = tmp_path / "1.1.sql"
    path.write_text("ALTER TABLE no_such_table ADD COLUMN x TEXT;", encoding="utf-8")
    monkeypatch.setattr(dbm, "SCHEMA_RELEASES",
                        dbm.SCHEMA_RELEASES + (("1.1", 8, str(path), False),))
    monkeypatch.setattr(dbm, "PORTAL_SCHEMA_VERSION", 8)
    with pytest.raises(Exception, match="no_such_table"):
        dbm.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 7    # not recorded
