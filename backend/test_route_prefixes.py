"""Backend routes must never begin with the text "/api/portal".

frontend/default.conf (`location /api/portal`) and vite.config.js both send that whole prefix
to the portal service, so a backend route there 404s from the browser while looking fine in
any test that calls the backend directly. The Volunteer Portal dialog said "Not configured"
for exactly this reason while sync was healthy. Note it is a plain string prefix:
"/api/portal_admin" would be swallowed too.

    pytest backend/test_route_prefixes.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main          # noqa: E402
import portal_sync   # noqa: E402


def _paths(routes, prefix=""):
    """Every route path, descending into included routers.

    FastAPI 0.141 keeps an included router as one entry with no `.path`, so a flat read of
    app.routes sees almost nothing (an earlier version of this test passed vacuously that way).
    Walked rather than read from the OpenAPI schema, which omits include_in_schema=False routes.
    """
    for r in routes:
        if hasattr(r, "path"):
            yield prefix + r.path
        elif hasattr(r, "original_router"):
            yield from _paths(r.original_router.routes, prefix + r.include_context.prefix)


ALL = set(_paths(main.app.routes))


def test_the_walk_actually_sees_the_routes():
    assert {"/api/health", "/api/login", "/api/get_schedule", "/api/passkeys/rename"} <= ALL
    assert len(ALL) > 50


def test_no_backend_route_is_shadowed_by_the_portal_proxy():
    shadowed = sorted(p for p in ALL if p.startswith("/api/portal"))
    assert shadowed == [], f"routed to the portal by nginx, never reach the backend: {shadowed}"


def test_portal_admin_routes_live_under_api_admin():
    assert {"/api/admin/portal/status", "/api/admin/portal/sync_now",
            "/api/admin/portal/pending"} <= ALL


def test_missing_settings_names_what_compose_passes(monkeypatch):
    monkeypatch.setattr(portal_sync, "PORTAL_SYNC_URL", "")
    monkeypatch.setattr(portal_sync, "SYNC_KEY_ID", "k")
    monkeypatch.setattr(portal_sync, "SYNC_SECRET", b"")
    assert portal_sync.missing_settings() == ["PORTAL_SYNC_URL", "SYNC_SECRET"]
    assert portal_sync.configured() is False
    monkeypatch.setattr(portal_sync, "PORTAL_SYNC_URL", "http://portal:5001")
    monkeypatch.setattr(portal_sync, "SYNC_SECRET", b"s")
    assert portal_sync.missing_settings() == [] and portal_sync.configured() is True
