"""Root test configuration: stop the two apps being tested in one process.

`backend/` and `portal/` are separate applications by design, and both define modules named
`db` and `main`. `sys.modules` holds one module per name, so a single pytest process binds one
app's `db` for both suites. The portal then runs psycopg2 code, tries to reach a Postgres host
that does not exist, and the run *hangs* rather than failing — a genuinely bad way to discover
a path collision.

The check lives here, in a collection hook, rather than in each app's conftest: conftest files
are imported during collection, before any test module has imported `db`, so a check there runs
too early to see the conflict.
"""
import pathlib

import pytest

_APPS = ("backend", "portal")


def pytest_collection_finish(session):
    """Refuse a session that collected tests from more than one application."""
    seen = set()
    for item in session.items:
        parts = pathlib.Path(str(item.fspath)).parts
        for app in _APPS:
            if app in parts:
                seen.add(app)
    if len(seen) > 1:
        raise pytest.UsageError(
            "Collected tests from both " + " and ".join(sorted(seen)) + ".\n"
            "These are separate applications that both define 'db' and 'main', and sys.modules "
            "holds one module per name — so one suite would silently get the other's database "
            "layer.\n"
            "Run them in separate processes, or use ./run_tests.sh which does that for you."
        )
