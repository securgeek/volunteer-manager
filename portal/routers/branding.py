"""Serve the pushed conference branding.

The logo arrives as a data URL (that is how the scheduler stores it) and is kept decoded, so
it can be served as bytes with an ETag. Inlining a 2.5MB data URL into the page would put it
on every load for every volunteer, uncacheable — the worst possible shape for a phone on
conference wifi.
"""
import json
import sqlite3

from fastapi import APIRouter, Depends, Request, Response

import content
import db

router = APIRouter()


@router.get("/api/portal/branding")
def branding(conn: sqlite3.Connection = Depends(db.get_db)):
    """Title, contact details, and whether each image exists, for the page shell to render.

    The contact address and links are ``""`` when the organizers haven't set them, and the
    pages then leave that line out.

    Args:
        conn: The database connection.
    """
    row = conn.execute(
        "SELECT app_title, portal_title, content_hash, contact_email, conference_url, "
        "code_of_conduct_url, LENGTH(logo) AS logo_len, "
        "LENGTH(favicon) AS fav_len, LENGTH(header_logo) AS header_len "
        "FROM branding WHERE id = 1"
    ).fetchone()
    if row is None:
        # Nothing pushed yet: a neutral shell beats an empty header.
        return {"title": "Volunteer Portal", "has_logo": False, "has_favicon": False,
                "has_header_logo": False, "content_hash": None, "contact_email": "",
                "conference_url": "", "code_of_conduct_url": ""}
    return {
        "title": row["portal_title"] or row["app_title"] or "Volunteer Portal",
        "has_logo": bool(row["logo_len"]),
        "has_favicon": bool(row["fav_len"]),
        "has_header_logo": bool(row["header_len"]),
        "content_hash": row["content_hash"],
        "contact_email": row["contact_email"] or "",
        "conference_url": row["conference_url"] or "",
        "code_of_conduct_url": row["code_of_conduct_url"] or "",
    }


@router.get("/api/portal/landing")
def landing(conn: sqlite3.Connection = Depends(db.get_db)):
    """The public landing page's content, rendered: the organizers' if pushed, else the default.

    Rendered on each request, since its placeholders come from the branding, which is pushed
    separately. The Markdown parts come back as HTML that is safe to insert (content.py: no
    raw HTML, no images, web and email links only); the opportunity cards come back as data.

    Args:
        conn: The database connection.

    Returns:
        ``intro``, ``main`` and ``sidebar`` (HTML), ``roles_heading`` and ``roles``.
    """
    row = conn.execute("SELECT landing FROM portal_content WHERE id = 1").fetchone()
    page = json.loads(row["landing"]) if row and row["landing"] else content.DEFAULT_LANDING
    brand = conn.execute("SELECT app_title, contact_email, conference_url, code_of_conduct_url "
                         "FROM branding WHERE id = 1").fetchone()
    brand = dict(brand) if brand else {}
    values = {"conference": brand.get("app_title") or "",
              "contact_email": brand.get("contact_email") or "",
              "conference_url": brand.get("conference_url") or "",
              "code_of_conduct_url": brand.get("code_of_conduct_url") or ""}
    return content.render_landing(page, values)


# One complete statement per image column: a column name can't be a parameter. Each image
# carries its own type; the favicon is always the PNG the scheduler's browser drew.
_IMAGE_SQL = {
    "logo": "SELECT logo AS blob, logo_mime AS mime, content_hash FROM branding WHERE id = 1",
    "favicon": "SELECT favicon AS blob, NULL AS mime, content_hash FROM branding WHERE id = 1",
    "header_logo": "SELECT header_logo AS blob, header_logo_mime AS mime, content_hash "
                   "FROM branding WHERE id = 1",
}


def _serve_image(request: Request, conn: sqlite3.Connection, column: str,
                 fallback_mime: str) -> Response:
    row = conn.execute(_IMAGE_SQL[column]).fetchone()
    if row is None or not row["blob"]:
        return Response(status_code=404)
    etag = f'"{row["content_hash"] or "none"}-{column}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(
        content=row["blob"],
        media_type=row["mime"] or fallback_mime,
        headers={"ETag": etag, "Cache-Control": "public, max-age=3600, must-revalidate"},
    )


@router.get("/api/portal/branding/logo")
def logo(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    """The theme logo the scheduler pushed. Cached by ETag; 404 when none is set.

    Args:
        request: The incoming request, for `If-None-Match`.
        conn: The database connection.
    """
    return _serve_image(request, conn, "logo", "image/png")


@router.get("/api/portal/branding/header_logo")
def header_logo(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    """The small logo the volunteer pages show top left. Cached by ETag; 404 when none.

    Args:
        request: The incoming request, for `If-None-Match`.
        conn: The database connection.
    """
    return _serve_image(request, conn, "header_logo", "image/png")


@router.get("/api/portal/branding/favicon")
def favicon(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    """The favicon made from the theme logo. Cached by ETag; 404 when none is set.

    Args:
        request: The incoming request, for `If-None-Match`.
        conn: The database connection.
    """
    return _serve_image(request, conn, "favicon", "image/png")
