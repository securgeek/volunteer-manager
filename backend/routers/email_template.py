"""The schedule email template (Call for Volunteers → Schedule Email). Organizers only.

Edit the Markdown template, upload images for it, preview it as any volunteer, and download
the package tools/vol_mail sends from. Rendering is email_render.py; this file is storage and
endpoints. Nothing here sends mail.
"""
import base64
import hashlib
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

import email_render
from db import PgClient, RoleChecker, audit, get_app_kv, get_db
from routers.export import volunteer_records

router = APIRouter()

TEMPLATE_KEY = "schedule"
MAX_ASSET, MAX_ASSETS_TOTAL = 1_000_000, 5_000_000
MAX_TEXT = 50_000

# Raster only: many mail apps won't show SVG. Identified by content, not by the filename.
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png", "png"),
          (b"\xff\xd8\xff", "image/jpeg", "jpg"),
          (b"GIF87a", "image/gif", "gif"),
          (b"GIF89a", "image/gif", "gif"))


def _sniff(data: bytes):
    for magic, ctype, ext in _MAGIC:
        if data.startswith(magic):
            return ctype, ext
    return None


def _asset_name(raw: str, ext: str) -> str:
    """A safe, lowercase file name ending in the type's real extension."""
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", (raw or "").strip().lower())
    stem = re.sub(r"[^a-z0-9._-]+", "-", stem).strip("-.") or "image"
    name = f"{stem[:56]}.{ext}"
    if not email_render.ASSET_NAME.match(name):
        raise HTTPException(status_code=400, detail="Use a simple file name: letters, numbers, - _ .")
    return name


def _load(db: PgClient) -> dict:
    row = db.execute("SELECT subject, body_md, shift_md, updated_by, updated_at "
                     "FROM email_template WHERE template_key = ?;", (TEMPLATE_KEY,)).fetchone()
    if not row:
        return {"subject": email_render.DEFAULT_SUBJECT, "body_md": email_render.DEFAULT_BODY,
                "shift_md": email_render.DEFAULT_SHIFT, "updated_by": None, "updated_at": None,
                "is_default": True}
    return {**dict(row), "updated_at": row["updated_at"].isoformat() + "Z" if row["updated_at"] else None,
            "is_default": False}


_ASSETS_SQL = "SELECT name, content_type, sha256, size FROM email_assets ORDER BY name;"
_ASSETS_WITH_DATA_SQL = ("SELECT name, content_type, sha256, size, data FROM email_assets "
                         "ORDER BY name;")


def _assets(db: PgClient, with_data: bool = False) -> list:
    rows = db.execute(_ASSETS_WITH_DATA_SQL if with_data else _ASSETS_SQL).fetchall()
    return [{**dict(r), **({"data": bytes(r["data"])} if with_data else {})} for r in rows]


def _missing_assets(db: PgClient, body_md: str, shift_md: str) -> list:
    have = {a["name"] for a in _assets(db)}
    return sorted(email_render.asset_refs(body_md, shift_md) - have)


def _store_asset(db: PgClient, name: str, ctype: str, data: bytes, username: str) -> dict:
    if len(data) > MAX_ASSET:
        raise HTTPException(status_code=413, detail="Images can be at most 1 MB each")
    others = db.execute("SELECT COALESCE(SUM(size), 0) AS total FROM email_assets WHERE name <> ?;",
                        (name,)).fetchone()["total"]
    if others + len(data) > MAX_ASSETS_TOTAL:
        raise HTTPException(status_code=413, detail="Images can be at most 5 MB in total")
    digest = hashlib.sha256(data).hexdigest()
    db.execute(
        "INSERT INTO email_assets (name, content_type, data, sha256, size, uploaded_by) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET "
        "content_type = excluded.content_type, data = excluded.data, sha256 = excluded.sha256, "
        "size = excluded.size, uploaded_by = excluded.uploaded_by, "
        "uploaded_at = NOW() AT TIME ZONE 'UTC';",
        (name, ctype, data, digest, len(data), username))
    db.commit()
    return {"name": name, "content_type": ctype, "sha256": digest, "size": len(data)}


@router.get("/api/email_template")
def get_template(
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """The template (the built-in default until first saved), its images, and what it can use.

    Args:
        s_data: The organizer's session.
        db: The request's database connection.

    Returns:
        dict with ``subject``, ``body_md``, ``shift_md``, ``updated_by``, ``updated_at``,
        ``is_default``, ``assets``, ``missing_assets``, ``problems`` and ``placeholders``.
    """
    t = _load(db)
    return {**t, "assets": _assets(db),
            "missing_assets": _missing_assets(db, t["body_md"], t["shift_md"]),
            "problems": email_render.check_template(t["subject"], t["body_md"], t["shift_md"]),
            "placeholders": {"subject": email_render.SUBJECT_FIELDS,
                             "body": email_render.BODY_FIELDS,
                             "shift": email_render.SHIFT_FIELDS}}


@router.post("/api/email_template")
def save_template(
    request: Request,
    subject: str = Form(default=""),
    body_md: str = Form(default=""),
    shift_md: str = Form(default=""),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Save the template. Problems that would break sending are refused; images the template
    refers to but that aren't uploaded yet are allowed (upload them next) and listed.

    Args:
        request: The incoming request, for the session id and the audit log.
        subject: The subject line template.
        body_md: The body, in Markdown.
        shift_md: The block repeated once per shift, in Markdown.
        s_data: The organizer's session. Their username is recorded as `updated_by`.
        db: The request's database connection.

    Raises:
        HTTPException 400: An unknown placeholder, no {{shifts}} or {{qr}}, a blank subject.
        HTTPException 413: Over MAX_TEXT.
    """
    if max(len(subject), len(body_md), len(shift_md)) > MAX_TEXT:
        raise HTTPException(status_code=413, detail="The template is too long")
    problems = email_render.check_template(subject, body_md, shift_md)
    if problems:
        raise HTTPException(status_code=400, detail=" ".join(problems))
    db.execute(
        "INSERT INTO email_template (template_key, subject, body_md, shift_md, updated_by, "
        "updated_at) VALUES (?, ?, ?, ?, ?, NOW() AT TIME ZONE 'UTC') "
        "ON CONFLICT (template_key) DO UPDATE SET subject = excluded.subject, "
        "body_md = excluded.body_md, shift_md = excluded.shift_md, "
        "updated_by = excluded.updated_by, updated_at = excluded.updated_at;",
        (TEMPLATE_KEY, subject.strip(), body_md, shift_md, s_data["username"]))
    db.commit()
    audit(request, request.session.get("session_id"), "Saved the schedule email template")
    return {"ok": True, "missing_assets": _missing_assets(db, body_md, shift_md)}


@router.post("/api/email_template/assets")
async def upload_asset(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(default=""),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Upload (or replace, by name) a PNG, JPEG or GIF for the template.

    Args:
        file: The image; its type is read from its content.
        name: Optional name to use in ``asset:<name>``; defaults to the file's name. The
            extension is always the image's real type.

    Raises:
        HTTPException 400: Not a PNG, JPEG or GIF; or an unusable name.
        HTTPException 413: Over 1 MB, or over 5 MB for all images together.
    """
    data = await file.read(MAX_ASSET + 1)
    found = _sniff(data)
    if not found:
        raise HTTPException(status_code=400, detail="Only PNG, JPEG and GIF images can be used "
                            "(many mail apps won't show SVG)")
    ctype, ext = found
    stored = _store_asset(db, _asset_name(name or file.filename or "", ext), ctype, data,
                          s_data["username"])
    audit(request, request.session.get("session_id"), f"Uploaded email image {stored['name']}")
    return stored


@router.post("/api/email_template/assets/site_logo")
def add_site_logo(
    request: Request,
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Copy the App Settings logo into the template's images as ``site-logo.<ext>``.

    Args:
        request: The incoming request, for the session id and the audit log.
        s_data: The organizer's session.
        db: The request's database connection.

    Raises:
        HTTPException 404: No logo is set.
        HTTPException 400: The logo is an SVG (or otherwise not PNG, JPEG or GIF).
    """
    logo = get_app_kv(db, "app_logo")
    if not logo:
        raise HTTPException(status_code=404, detail="No site logo is set (Admin → App Settings)")
    try:
        data = base64.b64decode(logo.split(",", 1)[1])
    except (IndexError, ValueError):
        raise HTTPException(status_code=400, detail="The site logo couldn't be read") from None
    found = _sniff(data)
    if not found:
        raise HTTPException(status_code=400, detail="The site logo isn't a PNG, JPEG or GIF "
                            "(an SVG won't show in many mail apps). Upload a PNG version instead.")
    ctype, ext = found
    stored = _store_asset(db, f"site-logo.{ext}", ctype, data, s_data["username"])
    audit(request, request.session.get("session_id"), "Copied the site logo into the email images")
    return stored


@router.post("/api/email_template/assets/delete")
def delete_asset(
    request: Request,
    name: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Remove an image. The template keeps any reference to it, which then shows as missing.

    Args:
        request: The incoming request, for the session id and the audit log.
        name: The image's file name.
        s_data: The organizer's session.
        db: The request's database connection.
    """
    db.execute("DELETE FROM email_assets WHERE name = ?;", (name,))
    db.commit()
    audit(request, request.session.get("session_id"), f"Deleted email image {name}")
    return {"ok": True}


@router.get("/api/email_template/assets/{name}")
def get_asset(
    name: str,
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """An image's bytes, for its thumbnail in the editor.

    Args:
        name: The image's file name, from the path.
        s_data: The organizer's session.
        db: The request's database connection.
    """
    row = db.execute("SELECT content_type, data FROM email_assets WHERE name = ?;",
                     (name,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No such image")
    return Response(content=bytes(row["data"]), media_type=row["content_type"],
                    headers={"Cache-Control": "private, no-cache",
                             "X-Content-Type-Options": "nosniff"})


@router.get("/api/email_template/recipients")
def recipients(
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Active volunteers with at least one shift: who the mailer would send to, and who can
    be previewed. ``has_email`` is false for anyone the mailer will skip.

    Args:
        s_data: The organizer's session.
        db: The request's database connection.
    """
    vols = volunteer_records(db, "active", assigned=True, with_email=True)["volunteers"]
    return [{"vol_id": v["vol_id"], "name": v["name"], "shift_count": len(v["shifts"]),
             "has_email": bool(v["email_enc"])} for v in vols]


@router.post("/api/email_template/preview")
def preview(
    vol_id: str = Form(...),
    subject: str = Form(default=""),
    body_md: str = Form(default=""),
    shift_md: str = Form(default=""),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """One volunteer's email, from the template as it stands in the editor (saved or not).

    Real shifts and real check-in codes; images and QR codes inlined as data URLs. Shown in a
    sandboxed iframe, so nothing in it can run.

    Args:
        vol_id: The volunteer to preview for.
        subject: The subject line template, unsaved.
        body_md: The body, unsaved.
        shift_md: The per-shift block, unsaved.
        s_data: The organizer's session.
        db: The request's database connection.

    Returns:
        dict with ``subject``, ``html``, ``text``, ``problems`` and ``missing_assets``.

    Raises:
        HTTPException 404: Not an active volunteer with a shift.
    """
    found = [v for v in volunteer_records(db, "active", assigned=True, with_codes=True)["volunteers"]
             if v["vol_id"] == vol_id]
    if not found:
        raise HTTPException(status_code=404, detail="Pick an active volunteer who has a shift")
    vol = found[0]
    parts = email_render.build_parts(subject, body_md, shift_md)
    qrs = [email_render.qr_png(s["checkin_code"]) for s in vol["shifts"]]
    msg = email_render.render_message(
        parts, vol, get_app_kv(db, "app_title") or "",
        lambda i, s: "data:image/png;base64," + base64.b64encode(qrs[i]).decode())
    assets = {a["name"]: (a["content_type"], a["data"]) for a in _assets(db, with_data=True)}
    return {**msg, "html": email_render.inline_assets(msg["html"], assets),
            "problems": email_render.check_template(subject, body_md, shift_md),
            "missing_assets": _missing_assets(db, body_md, shift_md)}


@router.get("/api/email_template/package")
def package(
    request: Request,
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """The saved template as a zip for tools/vol_mail: manifest, HTML and text, images.

    Args:
        request: The incoming request, for the session id and the audit log.
        s_data: The organizer's session. Their username goes in the manifest.
        db: The request's database connection.

    Raises:
        HTTPException 409: The saved template has problems, or refers to images not uploaded.
    """
    t = _load(db)
    problems = email_render.check_template(t["subject"], t["body_md"], t["shift_md"])
    missing = _missing_assets(db, t["body_md"], t["shift_md"])
    if missing:
        problems.append("Images not uploaded yet: " + ", ".join(missing))
    if problems:
        raise HTTPException(status_code=409, detail=" ".join(problems))
    now = datetime.now(timezone.utc)
    meta = {"created_at": now.isoformat().replace("+00:00", "Z"), "created_by": s_data["username"],
            "event_title": get_app_kv(db, "app_title") or "",
            "template_updated_at": t["updated_at"]}
    data = email_render.build_package(t["subject"], t["body_md"], t["shift_md"],
                                      _assets(db, with_data=True), meta)
    audit(request, request.session.get("session_id"), "Downloaded the schedule email package")
    fname = f"schedule-email-{now:%Y%m%d-%H%M}.zip"
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})
