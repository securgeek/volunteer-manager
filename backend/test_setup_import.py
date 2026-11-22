"""The setup wizard's CSV imports, against a REAL Postgres.

What these pin: one bad row is skipped and reported, and every other row is still saved. On
Postgres a failed statement aborts the whole transaction, so without a savepoint per row the
rows after it fail too and the final commit quietly rolls back the rows before it, while the
response still counts them as inserted.

Needs a throwaway Postgres; skipped unless TEST_PG_HOST is set. Everything in the target
database's public schema is DROPPED.

    TEST_PG_HOST=127.0.0.1 TEST_PG_PORT=55432 TEST_PG_DB=scheduler_test \\
    TEST_PG_USER=scheduler TEST_PG_PASSWORD=test  pytest backend/test_setup_import.py -v
"""
import os
import sys

import pytest

if not os.getenv("TEST_PG_HOST"):
    pytest.skip("needs a throwaway Postgres: set TEST_PG_HOST (see module docstring)",
                allow_module_level=True)

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import db as dbm  # noqa: E402

SITE = "https://scheduler.test"


def _raw_conn():
    import psycopg2
    dsn = {k: v for k, v in dbm._dsn().items() if k != "cursor_factory"}
    return psycopg2.connect(**dsn)


def _sql(stmt, params=()):
    conn = _raw_conn()
    with conn.cursor() as cur:
        cur.execute(stmt, params)
        rows = cur.fetchall() if cur.description else None
    conn.commit()
    conn.close()
    return rows


@pytest.fixture(scope="module")
def app():
    dbm.DB_HOST = os.environ["TEST_PG_HOST"]
    dbm.DB_PORT = int(os.getenv("TEST_PG_PORT", "5432"))
    dbm.DB_NAME = os.getenv("TEST_PG_DB", "scheduler_test")
    dbm.DB_USER = os.getenv("TEST_PG_USER", "scheduler")
    dbm.DB_PASSWORD = os.getenv("TEST_PG_PASSWORD", "")
    dbm.close_pool()

    _sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    cwd = os.getcwd()
    os.chdir(_HERE)  # migrate() opens scheduler.sql and migrations/
    try:
        dbm.open_db()
    finally:
        os.chdir(cwd)

    # A server-side failure on demand: any position keyed 'bad' is rejected by Postgres
    # itself, mid-transaction, the way a real constraint violation would be.
    _sql("""
        CREATE FUNCTION reject_bad_pos() RETURNS trigger AS $$
        BEGIN
            IF NEW.pos_key = 'bad' THEN RAISE EXCEPTION 'rejected by test trigger'; END IF;
            RETURN NEW;
        END $$ LANGUAGE plpgsql;
        CREATE TRIGGER reject_bad_pos BEFORE INSERT ON positions
            FOR EACH ROW EXECUTE FUNCTION reject_bad_pos();
    """)

    from fastapi import FastAPI
    from starlette.middleware.sessions import SessionMiddleware
    from routers import auth, setup
    application = FastAPI()
    for r in (auth, setup):
        application.include_router(r.router)
    application.add_middleware(SessionMiddleware, secret_key="test-secret")

    from argon2 import PasswordHasher
    _sql("INSERT INTO users (user_id, username, password_hash, role) VALUES (%s, %s, %s, %s)",
         ("u-alice", "alice", PasswordHasher().hash("alice-password"), "organizer"))
    yield application
    dbm.close_pool()


def _organizer(app):
    from fastapi.testclient import TestClient
    c = TestClient(app, base_url=SITE)
    r = c.post("/api/login", data={"username": "alice", "password": "alice-password"})
    assert r.status_code == 200, r.text
    return c


def _upload(client, path, csv_text):
    return client.post(path, files={"file": ("import.csv", csv_text.encode(), "text/csv")})


SCHEDULE_CSV = """day_key,day_str,time_key,time_str,pos_key,pos_cat,pos_str,date
sat,Saturday,0800,0800-1200,reg1,Registration,Registration 1,2099-04-25
sat,Saturday,0800,0800-1200,bad,Registration,Rejected,2099-04-25
sat,Saturday,0800,0800-1200,reg2,Registration,Registration 2,2099-04-25
"""


def test_schedule_import_keeps_the_rows_around_a_bad_one(app):
    body = _upload(_organizer(app), "/api/setup/import_schedule", SCHEDULE_CSV).json()
    assert body["inserted_slots"] == 2 and body["skipped"] == 1
    assert len(body["errors"]) == 1 and "Row 3" in body["errors"][0]
    saved = {r[0] for r in _sql("SELECT pos_key FROM schedule")}
    assert saved == {"reg1", "reg2"}


VOLUNTEERS_CSV = """vol_id,first_name,last_name,nickname,time_pref,add_time
2001,Ada,Lovelace,,Saturday 0800-1200,
2002,Grace,Hopper,Amazing Grace,,
"""


def test_volunteer_import_saves_rows(app):
    body = _upload(_organizer(app), "/api/setup/import_volunteers", VOLUNTEERS_CSV).json()
    assert body["inserted"] == 2 and body["skipped"] == 0, body
    names = dict(_sql("SELECT vol_id, name FROM volunteers ORDER BY vol_id"))
    assert names == {"2001": "Ada L.", "2002": "Grace H. (Amazing Grace)"}


def test_volunteer_reimport_updates_in_place(app):
    csv_text = VOLUNTEERS_CSV.replace("Amazing Grace", "Grace")
    body = _upload(_organizer(app), "/api/setup/import_volunteers", csv_text).json()
    assert body["inserted"] == 2 and body["skipped"] == 0, body
    assert _sql("SELECT name FROM volunteers WHERE vol_id = '2002'")[0][0] == "Grace H. (Grace)"
    assert _sql("SELECT COUNT(*) FROM volunteers")[0][0] == 2
