"""The volunteer portal's editable content: Volunteer Portal → Landing page / Questionnaire /
Invite email. Organizers only.

Each part is stored in ``app_kv`` as checked JSON (``portal_landing``,
``portal_questionnaire``, ``portal_invite``), or absent for the default, and pushed to the
portal by the sync loop (``portal_sync.push_content``), which checks it all again. The rules
and the defaults are ``portal_content.py``, the scheduler's copy of the portal's own.
"""
import json
from typing import Optional

from fastapi import APIRouter, Request, Depends, Form, HTTPException

import portal_content
from audit import audit
from db import get_db, RoleChecker, PgClient

router = APIRouter()

# Part → (app_kv key, the check, the default). A key is a bound value, never SQL text.
PARTS = {
    "landing": ("portal_landing", portal_content.validate_landing,
                portal_content.DEFAULT_LANDING),
    "questionnaire": ("portal_questionnaire", portal_content.validate_fields,
                      portal_content.DEFAULT_FIELDS),
    "invite": ("portal_invite", portal_content.validate_invite, portal_content.DEFAULT_INVITE),
}
MAX_VALUE = 200_000

# Invented values for the invite preview: the real ones exist only when an invite is sent.
SAMPLE_INVITE = {"first_name": "Alex", "link": "https://portal.example/apply/signup?invite=…",
                 "expires": "December 5"}


def stored_parts(db: PgClient) -> dict:
    """Each part as saved, or ``None`` where it's still the default.

    Args:
        db: The database connection.

    Returns:
        ``{landing, questionnaire, invite}``.
    """
    rows = {r["app_key"]: r["val"] for r in db.execute(
        "SELECT app_key, val FROM app_kv WHERE app_key = ANY(?::text[]);",
        ([key for key, _, _ in PARTS.values()],)).fetchall()}
    return {part: json.loads(rows[key]) if rows.get(key) else None
            for part, (key, _, _) in PARTS.items()}


def placeholder_values(db: PgClient) -> dict:
    """The conference's name and contact details, as the templates see them.

    Args:
        db: The database connection.

    Returns:
        ``conference``, ``contact_email``, ``conference_url`` and ``code_of_conduct_url``.
    """
    rows = {r["app_key"]: r["val"] for r in db.execute(
        "SELECT app_key, val FROM app_kv WHERE app_key IN ('app_title', 'contact_email', "
        "'conference_url', 'code_of_conduct_url');").fetchall()}
    return {"conference": rows.get("app_title", ""),
            **{k: rows.get(k, "") for k in ("contact_email", "conference_url",
                                             "code_of_conduct_url")}}


def _parse(part: str, value: str):
    """Check a part sent from an editor.

    Args:
        part: ``landing``, ``questionnaire`` or ``invite``.
        value: Its JSON.

    Returns:
        The clean definition.

    Raises:
        HTTPException 400: Unknown part, not JSON, too big, or not a valid definition; the
            detail says what's wrong.
    """
    if part not in PARTS:
        raise HTTPException(status_code=400, detail="Unknown part")
    if len(value) > MAX_VALUE:
        raise HTTPException(status_code=413, detail="That's too big")
    try:
        raw = json.loads(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="That isn't valid JSON") from None
    try:
        return PARTS[part][1](raw)
    except portal_content.ContentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/api/admin/portal/content")
def get_content(
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Everything the three editors need.

    Args:
        s_data: The organizer's session.
        db: The request's database connection.

    Returns:
        ``saved`` (each part, or ``None`` at its default), ``defaults``, the placeholders and
        filters each template may use, the opportunity icons, and ``answer_counts``: how many
        volunteers have an answer under each key, for the warning before a question goes.
    """
    counts = {r["key"]: r["n"] for r in db.execute(
        "SELECT k AS key, COUNT(*) AS n FROM volunteers, "
        "jsonb_object_keys(CASE WHEN jsonb_typeof(answers) = 'object' THEN answers "
        "ELSE '{}'::jsonb END) AS k GROUP BY k;").fetchall()}
    return {
        "saved": stored_parts(db),
        "defaults": {part: default for part, (_, _, default) in PARTS.items()},
        "landing_vars": list(portal_content.LANDING_VARS),
        "invite_vars": list(portal_content.INVITE_VARS),
        "filters": sorted(portal_content.ALLOWED_FILTERS),
        "icons": list(portal_content.ROLE_ICONS),
        "flexible_key": portal_content.FLEXIBLE_KEY,
        "field_types": list(portal_content.FIELD_TYPES),
        "answer_counts": counts,
    }


@router.post("/api/admin/portal/content")
def save_content(
    request: Request,
    part: str = Form(...),
    value: Optional[str] = Form(default=None),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Save one part, or put it back to its default. The portal has it within a sync tick.

    Args:
        request: The incoming request, for the audit log.
        part: ``landing``, ``questionnaire`` or ``invite``.
        value: The part's JSON. Left out (Reset to default) removes the saved copy.
        s_data: The organizer's session.
        db: The request's database connection.

    Returns:
        ``{"ok": True, "saved": ...}``: the clean definition, or ``None`` after a reset.

    Raises:
        HTTPException 400: An unknown part or an invalid definition, with what's wrong.
        HTTPException 413: Too big.
    """
    if part not in PARTS:
        raise HTTPException(status_code=400, detail="Unknown part")
    key = PARTS[part][0]
    if value is None:
        db.execute("DELETE FROM app_kv WHERE app_key = ?;", (key,))
        clean = None
    else:
        clean = _parse(part, value)
        db.execute("INSERT INTO app_kv (app_key, val) VALUES (?, ?) "
                   "ON CONFLICT(app_key) DO UPDATE SET val = excluded.val;",
                   (key, json.dumps(clean)))
    db.commit()
    audit("portal.content", request, request.session.get("session_id"),
          f"{'Reset' if clean is None else 'Saved'} the portal's {part}")
    import portal_sync
    portal_sync.request_push()
    return {"ok": True, "saved": clean}


@router.post("/api/admin/portal/content/preview")
def preview_content(
    part: str = Form(...),
    value: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Check a part and show it as the portal will, without saving.

    Args:
        part: ``landing`` or ``invite``. A questionnaire is only checked.
        value: The part's JSON, as the editor has it.
        s_data: The organizer's session.
        db: The request's database connection.

    Returns:
        For the landing page, its rendered parts; for the invite, ``subject`` and ``body`` with
        invented values for the volunteer and the link; for a questionnaire, ``ok``.

    Raises:
        HTTPException 400: An invalid definition, with what's wrong.
    """
    clean = _parse(part, value)
    values = placeholder_values(db)
    try:
        if part == "landing":
            return portal_content.render_landing(clean, values)
        if part == "invite":
            subject, body = portal_content.render_invite(clean, {**values, **SAMPLE_INVITE})
            return {"subject": subject, "body": body}
    except portal_content.ContentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True}
