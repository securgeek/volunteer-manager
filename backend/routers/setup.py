"""First-run setup and App Settings.

On a fresh database the setup wizard creates the first organizer
(`setup/create_admin`, refused once any user exists), then imports or builds the schedule and
imports the volunteer roster from CSV. The CSV imports are safe to run again: each row is in
its own savepoint, so a bad row is reported and skipped while the rest are kept.

App Settings live here too: the site title, the event timezone, and the two logos, which are
stored as data URLs in `app_kv` and pushed to the portal for the volunteer pages.
"""
import logging
import uuid
import base64
import re
import csv
import io
from contextlib import contextmanager
from typing import Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg2
from psycopg2 import errorcodes
from fastapi import APIRouter, Request, Depends, Form, HTTPException, UploadFile, File

from checkin_codes import DEFAULT_TZ
from audit import audit
from db import get_db, get_session_data, RoleChecker, _session_cache, PgClient

router = APIRouter()
log = logging.getLogger(__name__)

# A CSV row's database error, in words for the organizer, by SQLSTATE. The raw message can
# carry constraint names and the values involved, so it goes to the server log, not the
# reply (CodeQL #8, #9).
_ROW_PROBLEMS = {
    errorcodes.UNIQUE_VIOLATION: "it duplicates an existing entry",
    errorcodes.FOREIGN_KEY_VIOLATION: "it refers to something that doesn't exist",
    errorcodes.NOT_NULL_VIOLATION: "a required column is empty",
    errorcodes.STRING_DATA_RIGHT_TRUNCATION: "a value is too long",
    errorcodes.CHECK_VIOLATION: "a value isn't allowed",
    errorcodes.INVALID_TEXT_REPRESENTATION: "a value is the wrong type",
    errorcodes.INVALID_DATETIME_FORMAT: "a date isn't valid (use YYYY-MM-DD)",
    errorcodes.DATETIME_FIELD_OVERFLOW: "a date isn't valid (use YYYY-MM-DD)",
}


def _row_problem(exc: psycopg2.Error, what: str) -> str:
    """Say why a CSV row wasn't saved, without the database's own message.

    Args:
        exc: The error the row's statements raised.
        what: Which row, for the log line, e.g. ``schedule row 3``.

    Returns:
        A short reason, such as "a value is too long".
    """
    log.warning("CSV import: %s not saved (SQLSTATE %s): %s", what, exc.pgcode,
                exc.diag.message_primary if exc.diag else exc)
    return _ROW_PROBLEMS.get(exc.pgcode, "it couldn't be saved")


@contextmanager
def _row_savepoint(db: PgClient):
    """Run one CSV row's statements under a savepoint.

    On Postgres a failed statement aborts the whole transaction: without this, one bad row
    makes every later row fail too, and the final commit silently rolls back the rows before
    it. Rolling back to the savepoint undoes just the failed row and lets the import go on.

    Args:
        db: The request's connection.

    Yields:
        Nothing; the row's statements run inside the ``with`` block.

    Raises:
        psycopg2.Error: Re-raised from the row, after rolling the row back.
    """
    db.execute("SAVEPOINT import_row;")
    try:
        yield
    except psycopg2.Error:
        db.execute("ROLLBACK TO SAVEPOINT import_row;")
        raise
    db.execute("RELEASE SAVEPOINT import_row;")


def _queue_branding_push() -> None:
    """Ask the sync loop to push branding.

    Title and logo are not schedule mutations, so broadcast() does not cover them — these two
    endpoints are the only writers, and both call this.
    """
    import portal_sync
    portal_sync.request_push()


@router.get("/api/setup_status")
def setup_status(
    request: Request,
    db: PgClient = Depends(get_db),
):
    """Return the current setup wizard state and the authenticated user's identity.

    Args:
        request: Starlette Request; session cookie checked to determine auth state.
        db: Injected database connection.

    Returns:
        dict with keys ``has_users``, ``has_title``, ``has_schedule``, ``has_volunteers``
        (booleans), ``authenticated`` (bool), ``username`` (str or None), ``role`` (str or None).
    """
    has_users      = bool(db.execute("SELECT 1 FROM users LIMIT 1;").fetchone())
    has_title      = bool(db.execute("SELECT 1 FROM app_kv WHERE app_key = 'app_title';").fetchone())
    has_schedule   = bool(db.execute("SELECT 1 FROM schedule LIMIT 1;").fetchone())
    has_volunteers = bool(db.execute("SELECT 1 FROM volunteers LIMIT 1;").fetchone())
    s_id           = request.session.get("session_id")
    s_data         = _session_cache.get(s_id) if s_id else None
    if s_data is None and s_id:
        row = db.execute(
            "SELECT s.user_id, u.username, u.role FROM sessions s "
            "LEFT JOIN users u ON s.user_id = u.user_id WHERE s.session_id = ?;",
            (s_id,)
        ).fetchone()
        if row:
            s_data = {"user_id": row["user_id"], "username": row["username"],
                      "role": row["role"] or "organizer"}
            _session_cache[s_id] = s_data
    return {
        "has_users":      has_users,
        "has_title":      has_title,
        "has_schedule":   has_schedule,
        "has_volunteers": has_volunteers,
        "authenticated":  s_data is not None,
        "username":       s_data["username"] if s_data else None,
        "role":           s_data["role"]     if s_data else None,
    }


@router.post("/api/setup/create_admin")
def setup_create_admin(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: PgClient = Depends(get_db),
):
    """Create the first organizer account; rejects with 403 once any user exists.

    Args:
        request: The incoming request, for the audit log.
        username: Desired admin username; lowercased and stripped before storage.
        password: Plaintext password; must be at least 8 characters.
        db: Injected database connection.

    Returns:
        dict with keys ``username`` and ``role`` (always ``"organizer"``).

    Raises:
        HTTPException 400: Password shorter than 8 characters.
        HTTPException 403: A user account already exists.
    """
    from argon2 import PasswordHasher
    if db.execute("SELECT 1 FROM users LIMIT 1;").fetchone():
        audit("setup.create_admin.refused", request, None,
              "First-admin setup called after setup was complete", username=username)
        raise HTTPException(status_code=403, detail="Setup already complete — admin already exists")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    user_id = str(uuid.uuid4())
    ph      = PasswordHasher()
    db.execute(
        "INSERT INTO users (user_id, username, password_hash, role, audit_access) "
        "VALUES (?, ?, ?, 'organizer', TRUE);",
        (user_id, username.lower().strip(), ph.hash(password))
    )
    db.commit()
    audit("setup.create_admin", request, None, "Created the first admin account",
          who=username.lower().strip(), user_id=user_id)
    return {"username": username.lower().strip(), "role": "organizer"}


@router.post("/api/setup/import_schedule")
def setup_import_schedule(
    request: Request,
    file: UploadFile = File(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Import schedule slots from CSV; every insert is ON CONFLICT DO NOTHING, so re-importing is safe.

    Args:
        request: Starlette Request; used for audit logging.
        file: UTF-8 CSV with required columns ``day_key``, ``day_str``, ``time_key``,
              ``time_str``, ``pos_key``, ``pos_cat``, ``pos_str``; optional column ``date``
              (ISO ``YYYY-MM-DD``) stored as ``event_date`` in days_table.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        dict with keys ``inserted_slots``, ``days``, ``times``, ``positions``,
        ``skipped``, and ``errors`` (list of per-row error messages).

    Raises:
        HTTPException 400: File is not UTF-8, or required CSV columns are missing.
        HTTPException 401/403: Insufficient role.
    """
    content = file.file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded") from None

    reader   = csv.DictReader(io.StringIO(text))
    required = {"day_key", "day_str", "time_key", "time_str", "pos_key", "pos_cat", "pos_str"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise HTTPException(
            status_code=400,
            detail="CSV must have columns: day_key, day_str, time_key, time_str, pos_key, pos_cat, pos_str"
        )

    inserted  = 0
    skipped   = 0
    errors    = []
    seen_days = {}
    seen_times = {}
    seen_pos   = {}

    for row_num, row in enumerate(reader, start=2):
        day_key    = (row.get("day_key")  or "").strip()
        day_str    = (row.get("day_str")  or "").strip()
        time_key   = (row.get("time_key") or "").strip()
        time_str   = (row.get("time_str") or "").strip()
        pos_key    = (row.get("pos_key")  or "").strip()
        pos_cat    = (row.get("pos_cat")  or "").strip()
        pos_str    = (row.get("pos_str")  or "").strip()
        event_date = (row.get("date")     or "").strip() or None

        if not all([day_key, day_str, time_key, time_str, pos_key, pos_cat, pos_str]):
            errors.append(f"Row {row_num}: missing required field — skipped")
            skipped += 1
            continue

        try:
            with _row_savepoint(db):
                if day_key not in seen_days:
                    db.execute("INSERT INTO days_table VALUES (?, ?, ?) ON CONFLICT (day_key) DO NOTHING;", (day_key, day_str, event_date))
                if time_key not in seen_times:
                    db.execute("INSERT INTO times_table VALUES (?, ?) ON CONFLICT (time_key) DO NOTHING;", (time_key, time_str))
                if pos_key not in seen_pos:
                    db.execute("INSERT INTO positions VALUES (?, ?, ?) ON CONFLICT (pos_key) DO UPDATE SET pos_cat = EXCLUDED.pos_cat, pos_str = EXCLUDED.pos_str;", (pos_key, pos_cat, pos_str))
                db.execute(
                    "INSERT INTO day_times (day_key, time_key) VALUES (?, ?) ON CONFLICT DO NOTHING;",
                    (day_key, time_key)
                )
                db.execute(
                    "INSERT INTO schedule (day_key, time_key, pos_key) VALUES (?, ?, ?) ON CONFLICT (day_key, time_key, pos_key) DO NOTHING;",
                    (day_key, time_key, pos_key)
                )
        except psycopg2.Error as e:
            errors.append(f"Row {row_num} ({day_key}/{time_key}/{pos_key}): "
                          f"{_row_problem(e, f'schedule row {row_num}')}")
            skipped += 1
            continue
        # Only once the row is saved: a rolled-back row's day must still be inserted by the
        # next row that uses it.
        seen_days.setdefault(day_key, day_str)
        seen_times.setdefault(time_key, time_str)
        seen_pos.setdefault(pos_key, (pos_cat, pos_str))
        inserted += 1

    db.commit()
    audit("setup.import_schedule", request, request.session.get("session_id"),
          f"Imported schedule: {inserted} slots, {len(seen_days)} days, "
          f"{len(seen_times)} times, {len(seen_pos)} positions ({skipped} skipped).")
    return {
        "inserted_slots": inserted,
        "days":           len(seen_days),
        "times":          len(seen_times),
        "positions":      len(seen_pos),
        "skipped":        skipped,
        "errors":         errors,
    }


@router.post("/api/setup/import_volunteers")
def setup_import_volunteers(
    request: Request,
    file: UploadFile = File(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Import volunteers from CSV; re-importing updates the imported columns of existing records.

    Args:
        request: Starlette Request; used for audit logging.
        file: UTF-8 CSV with required columns ``vol_id``, ``first_name``, ``last_name``;
              optional columns ``nickname``, ``time_pref``, ``add_time``.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        dict with keys ``inserted``, ``skipped``, and ``errors`` (list of per-row messages).

    Raises:
        HTTPException 400: File is not UTF-8, or required CSV columns are missing.
        HTTPException 401/403: Insufficient role.
    """
    content = file.file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded") from None

    reader   = csv.DictReader(io.StringIO(text))
    required = {"vol_id", "first_name", "last_name"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise HTTPException(
            status_code=400,
            detail=f"CSV must have columns: vol_id, first_name, last_name, nickname, time_pref, add_time"
        )

    inserted = 0
    skipped  = 0
    errors   = []

    for row_num, row in enumerate(reader, start=2):
        vol_id     = (row.get("vol_id")     or "").strip()
        first_name = (row.get("first_name") or "").strip()
        last_name  = (row.get("last_name")  or "").strip()
        nickname   = (row.get("nickname")   or "").strip()
        time_pref  = (row.get("time_pref")  or "").strip()
        add_time   = (row.get("add_time")   or "").strip()

        if not vol_id or not first_name or not last_name:
            errors.append(f"Row {row_num}: missing vol_id, first_name, or last_name — skipped")
            skipped += 1
            continue

        vol_name = f"{first_name} {last_name[0]}."
        if nickname:
            vol_name += f" ({nickname})"

        try:
            # Re-importing updates the imported columns only. (This was SQLite's INSERT OR
            # REPLACE, a syntax error on Postgres; it would also have deleted the row and lost
            # every column the CSV doesn't carry.)
            with _row_savepoint(db):
                db.execute(
                    "INSERT INTO volunteers (vol_id, name, time_pref, add_time) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (vol_id) DO UPDATE SET "
                    "name = EXCLUDED.name, time_pref = EXCLUDED.time_pref, "
                    "add_time = EXCLUDED.add_time;",
                    (vol_id, vol_name, time_pref, add_time)
                )
            inserted += 1
        except psycopg2.Error as e:
            errors.append(f"Row {row_num} (id={vol_id}): "
                          f"{_row_problem(e, f'volunteer row {row_num}')}")
            skipped += 1

    db.commit()
    audit("setup.import_volunteers", request, request.session.get("session_id"),
          f"Imported {inserted} volunteers via CSV ({skipped} skipped).")
    return {"inserted": inserted, "skipped": skipped, "errors": errors}


# The conference's public contact details, set with the title (setup wizard, App Settings) and
# pushed to the portal with the branding, where they become links on public pages. Each is
# optional; blank removes it and the pages leave that line out.
CONFERENCE_FIELDS = ("contact_email", "conference_url", "code_of_conduct_url")
MAX_CONFERENCE_FIELD = 300
# One plain address: no display name, list or spaces. The same shape the mail relay accepts.
_CONTACT_EMAIL = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


def clean_contact_email(raw: Optional[str]) -> str:
    """Check a contact address for the volunteer pages.

    Args:
        raw: What the organizer typed. Surrounding whitespace is ignored.

    Returns:
        The address, or ``""`` for blank.

    Raises:
        HTTPException 400: Not one plain address, or too long.
    """
    email = (raw or "").strip()
    if email and (len(email) > MAX_CONFERENCE_FIELD or not _CONTACT_EMAIL.match(email)):
        raise HTTPException(status_code=400,
                            detail="The contact email must be one plain address")
    return email


def clean_public_link(raw: Optional[str], what: str) -> str:
    """Check a link the volunteer pages will show.

    Only ``http(s)`` links with a host pass. Anything else, a ``javascript:`` URL above all,
    would run script on a public page when clicked.

    Args:
        raw: What the organizer typed. Surrounding whitespace is ignored.
        what: The field's name, for the error message.

    Returns:
        The link, or ``""`` for blank.

    Raises:
        HTTPException 400: Not an http(s) link with a host, contains whitespace, or too long.
    """
    link = (raw or "").strip()
    if not link:
        return ""
    parts = urlsplit(link)
    if (len(link) > MAX_CONFERENCE_FIELD or parts.scheme not in ("http", "https")
            or not parts.netloc or any(c.isspace() for c in link)):
        raise HTTPException(status_code=400,
                            detail=f"The {what} must be a web address starting with https://")
    return link


@router.get("/api/app_config")
def app_config(db: PgClient = Depends(get_db)):
    """Return public application configuration; no authentication required.

    Args:
        db: Injected database connection.

    Returns:
        dict with keys ``title``, ``logo``, ``favicon``, ``header_logo``, ``event_tz`` and
        the three ``CONFERENCE_FIELDS``: the configured app title, the year's theme logo and
        its 32x32 favicon (data URLs), the header logo the volunteer pages show top left (data
        URL), the timezone shift times are in, and the public contact address and links.
        ``title`` defaults to ``"Conference Scheduler"`` and ``event_tz`` to
        ``checkin_codes.DEFAULT_TZ`` if not yet set; the images are ``None`` and the
        conference fields ``""`` if not set. All of it is shown on public pages anyway.
    """
    rows = {r["app_key"]: r["val"] for r in db.execute(
        "SELECT app_key, val FROM app_kv "
        "WHERE app_key IN ('app_title', 'app_logo', 'app_favicon', 'app_header_logo', "
        "'event_tz', 'contact_email', 'conference_url', 'code_of_conduct_url');"
    ).fetchall()}
    return {
        "title":       rows.get("app_title", "Conference Scheduler"),
        **{key: rows.get(key, "") for key in CONFERENCE_FIELDS},
        "logo":        rows.get("app_logo"),
        "favicon":     rows.get("app_favicon"),
        "header_logo": rows.get("app_header_logo"),
        "event_tz":    rows.get("event_tz", DEFAULT_TZ),
    }


@router.post("/api/app_settings/event_tz")
def set_event_tz(
    request: Request,
    event_tz: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Set the timezone the schedule's shift times are in.

    Check-in compares the time now with each shift's start (days_table.event_date plus the
    start of time_str) in this zone, and the export API gives shift times with its offset.

    Args:
        request: Starlette Request; used for audit logging.
        event_tz: IANA zone name, e.g. ``America/New_York``.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient.

    Returns:
        dict with keys ``ok`` and ``event_tz``.

    Raises:
        HTTPException 400: Not a known IANA zone.
        HTTPException 401/403: Insufficient role.
    """
    event_tz = event_tz.strip()
    try:
        ZoneInfo(event_tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=400,
                            detail=f"{event_tz!r} isn't a known timezone") from None
    db.execute(
        "INSERT INTO app_kv (app_key, val) VALUES ('event_tz', ?) "
        "ON CONFLICT(app_key) DO UPDATE SET val = excluded.val;",
        (event_tz,)
    )
    db.commit()
    audit("settings.change", request, request.session.get("session_id"),
          f"Set event timezone to {event_tz}")
    return {"ok": True, "event_tz": event_tz}


@router.post("/api/setup/set_title")
def setup_set_title(
    request: Request,
    title: str = Form(...),
    with_contact: bool = Form(default=False),
    contact_email: Optional[str] = Form(default=None),
    conference_url: Optional[str] = Form(default=None),
    code_of_conduct_url: Optional[str] = Form(default=None),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Store the conference's title, and optionally its public contact address and links.

    The setup wizard and App Settings both use this. With ``with_contact`` set, the three
    conference fields are all taken as sent, and a blank or missing one is removed, so the
    volunteer pages leave that line out. Without it (the wizard's Skip) they keep their stored
    values. A flag rather than "missing means unchanged", because FastAPI reads an empty form
    field as missing. Everything is checked before anything is stored.

    Args:
        request: Starlette Request; used for audit logging.
        title: Display title for the application; must be non-empty.
        with_contact: Replace the three conference fields with what this request sends.
        contact_email: The address the volunteer pages give for questions.
        conference_url: The conference's website, linked from the volunteer pages.
        code_of_conduct_url: The code of conduct, linked from the volunteer pages.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        dict with keys ``ok``, ``title`` and each conference field as now stored.

    Raises:
        HTTPException 400: title is blank, or a conference field fails its check.
        HTTPException 401/403: Insufficient role.
    """
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    given: dict[str, str] = {}
    if with_contact:
        given = {"contact_email": clean_contact_email(contact_email),
                 "conference_url": clean_public_link(conference_url, "website"),
                 "code_of_conduct_url": clean_public_link(code_of_conduct_url,
                                                          "code of conduct link")}
    db.execute(
        "INSERT INTO app_kv (app_key, val) VALUES ('app_title', ?) "
        "ON CONFLICT(app_key) DO UPDATE SET val = excluded.val;",
        (title,)
    )
    for key, value in given.items():
        if value:
            db.execute("INSERT INTO app_kv (app_key, val) VALUES (?, ?) "
                       "ON CONFLICT(app_key) DO UPDATE SET val = excluded.val;", (key, value))
        else:
            db.execute("DELETE FROM app_kv WHERE app_key = ?;", (key,))
    db.commit()
    changed = ", ".join(given)
    audit("settings.change", request, request.session.get("session_id"),
          f"Set app title to {title!r}" + (f" and {changed}" if changed else ""))
    _queue_branding_push()
    stored = {r["app_key"]: r["val"] for r in db.execute(
        "SELECT app_key, val FROM app_kv WHERE app_key = ANY(?::text[]);",
        (list(CONFERENCE_FIELDS),)).fetchall()}
    return {"ok": True, "title": title, **{k: stored.get(k, "") for k in CONFERENCE_FIELDS}}


# Logos arrive as file uploads, not data-URL form fields: Starlette caps a plain field at 1 MB
# (a data URL is a third bigger than the image), while a file part is read here up to our own
# limit. nginx allows a bigger body on exactly these two paths (frontend/default.conf).
MAX_LOGO_BYTES = 2_000_000
MAX_FAVICON_CHARS = 200_000
_LOGO_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _logo_type(data: bytes) -> Optional[str]:
    """The image type from the file's own bytes, never the browser's claim; None if not one."""
    for magic, ctype in _LOGO_MAGIC:
        if data.startswith(magic):
            return ctype
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:1024].lstrip().lower()
    if head.startswith((b"<svg", b"<?xml")) and b"<svg" in head:
        return "image/svg+xml"
    return None


def _logo_data_url(upload: Optional[UploadFile]) -> str:
    """Read an uploaded logo into the data URL app_kv stores.

    Raises:
        HTTPException 400: No file, or not a PNG, JPEG, GIF, WebP or SVG.
        HTTPException 413: Over MAX_LOGO_BYTES.
    """
    if upload is None:
        raise HTTPException(status_code=400, detail="Choose an image to upload")
    data = upload.file.read(MAX_LOGO_BYTES + 1)
    if len(data) > MAX_LOGO_BYTES:
        raise HTTPException(status_code=413, detail="That image is over 2 MB. Use a smaller one.")
    ctype = _logo_type(data)
    if not ctype:
        raise HTTPException(status_code=400,
                            detail="That isn't a PNG, JPEG, GIF, WebP or SVG image.")
    return f"data:{ctype};base64,{base64.b64encode(data).decode()}"


@router.post("/api/app_logo")
def set_app_logo(
    request: Request,
    logo:    Optional[UploadFile] = File(default=None),
    favicon: str = Form(default=""),
    remove:  bool = Form(default=False),
    s_data:  dict = Depends(RoleChecker("organizer")),
    db:      PgClient = Depends(get_db),
):
    """Store or remove the year's theme logo and its auto-generated 32x32 favicon.

    The theme logo is the conference's primary one: the favicon is made from it, and the
    volunteer pages show it large on the landing page and faintly behind every page. The
    smaller logo in their upper left is the header logo (``set_header_logo``).

    Args:
        request: Starlette Request; used for audit logging.
        logo: The image file, up to MAX_LOGO_BYTES. Stored as a data URL.
        favicon: 32x32 PNG as a base64 data URL drawn from the logo in the browser.
        remove: True removes the logo and favicon; ``logo`` is then ignored.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        dict ``{"ok": True}``.

    Raises:
        HTTPException 400: No file, not an image, or a malformed favicon.
        HTTPException 413: Logo over 2 MB.
        HTTPException 401/403: Insufficient role.
    """
    if remove:
        db.execute("DELETE FROM app_kv WHERE app_key IN ('app_logo', 'app_favicon');")
    else:
        url = _logo_data_url(logo)
        if favicon and (not favicon.startswith("data:image/png;base64,")
                        or len(favicon) > MAX_FAVICON_CHARS):
            raise HTTPException(status_code=400, detail="The favicon must be a small PNG")
        for key, val in [("app_logo", url), ("app_favicon", favicon)]:
            db.execute(
                "INSERT INTO app_kv (app_key, val) VALUES (?, ?) "
                "ON CONFLICT(app_key) DO UPDATE SET val = excluded.val;",
                (key, val)
            )
    db.commit()
    audit("settings.change", request, request.session.get("session_id"),
          "Removed app logo" if remove else "Updated app logo and favicon")
    _queue_branding_push()
    return {"ok": True}


@router.post("/api/app_header_logo")
def set_header_logo(
    request: Request,
    logo:    Optional[UploadFile] = File(default=None),
    remove:  bool = Form(default=False),
    s_data:  dict = Depends(RoleChecker("organizer")),
    db:      PgClient = Depends(get_db),
):
    """Store or remove the header logo: the volunteer pages' upper left, as on bsidescharm.org.

    No favicon is made from it; that comes from the theme logo (``set_app_logo``). Without
    one, the volunteer pages fall back to the theme logo, then to the title.

    Args:
        request: Starlette Request; used for audit logging.
        logo: The image file, up to MAX_LOGO_BYTES. Stored as a data URL.
        remove: True removes the header logo; ``logo`` is then ignored.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        dict ``{"ok": True}``.

    Raises:
        HTTPException 400: No file, or not an image.
        HTTPException 413: Logo over 2 MB.
        HTTPException 401/403: Insufficient role.
    """
    if remove:
        db.execute("DELETE FROM app_kv WHERE app_key = 'app_header_logo';")
    else:
        db.execute(
            "INSERT INTO app_kv (app_key, val) VALUES ('app_header_logo', ?) "
            "ON CONFLICT(app_key) DO UPDATE SET val = excluded.val;",
            (_logo_data_url(logo),)
        )
    db.commit()
    audit("settings.change", request, request.session.get("session_id"),
          "Removed header logo" if remove else "Updated header logo")
    _queue_branding_push()
    return {"ok": True}
