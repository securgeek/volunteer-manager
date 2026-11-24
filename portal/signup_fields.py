"""The signup questionnaire: one definition, used by the form and by the server's validation.

GET /api/portal/signup/fields serves the questionnaire in force (current()) to the frontend,
which renders it in order on the step each question's `section` names, and signup/submit runs
every answer through validate(). Organizers edit it in the scheduler (Volunteer Portal →
Questionnaire); content.py has the default and the rules for a valid one.

Answers are kept when a question is changed or removed: validate_edit only replaces what the
current questions cover, and the organizer app shows an answer it has no question for by key.

Answers are stored as plain JSON on the portal (only the email address is encrypted), so
nothing sensitive belongs in this list: no phone numbers, no emergency contacts.

`flexible` is read by the scheduler too: its slot-dialog picker lists volunteers who answered
it as "Flexible" for other start times on the days they picked (backend routers/schedule.py
get_avail_vols). content.validate_fields keeps it a checkbox under that key.

Fields marked `editable` can be changed afterwards from My info (POST /api/portal/me/update,
checked by validate_edit); the rest are fixed once submitted.

Names (first, last, handle) are not answers. They are columns on the volunteer row, because the
scheduler and the duplicate-name check need them.
"""
import json
from typing import Optional

import validation
from content import DEFAULT_FIELDS, TSHIRT_SIZES  # noqa: F401  (TSHIRT_SIZES: re-exported)

# The questionnaire is organizer-editable (Volunteer Portal → Questionnaire in the scheduler),
# pushed here and kept in portal_content. This is the default, used until one is saved.
# current() gives whichever is in force; content.validate_fields says what a valid one is.
FIELDS = DEFAULT_FIELDS


def current(conn) -> list:
    """The questionnaire in force: the pushed one, else the default.

    Args:
        conn: The database connection.

    Returns:
        The question list, as ``content.validate_fields`` returns it.
    """
    row = conn.execute("SELECT questionnaire FROM portal_content WHERE id = 1").fetchone()
    if row is None or not row["questionnaire"]:
        return FIELDS
    return json.loads(row["questionnaire"])


def _text(raw, limit: int, key: str = "") -> str:
    if raw is None:
        return ""
    # validation.py's rule for free text; a breach is an anomaly (recorded, and refused in
    # enforce mode). In report mode the old forgiving behaviour below still applies.
    _clean, problem = validation.check("text", raw, maximum=limit)
    if problem is not None:
        validation.anomaly(problem[1], f"answers.{key}")
    if not isinstance(raw, str):
        raise ValueError("expected text")
    # One line or several is fine; control characters other than newlines are not.
    clean = "".join(ch for ch in raw if ch == "\n" or ch >= " ").strip()
    if len(clean) > limit:
        raise ValueError(f"at most {limit} characters")
    return clean


def _check(f: dict, answers: dict, out: dict) -> None:
    """Validate one field from `answers` into `out`. Raises ValueError("<label>: <reason>")."""
    key, kind, label = f["key"], f["type"], f["label"]
    raw = answers.get(key)
    try:
        if kind == "ack":
            if raw is not True:
                raise ValueError("must be checked")
            out[key] = True
        elif kind == "check":
            out[key] = raw is True
        elif kind in ("choice", "select"):
            values = {o["value"] for o in f["options"]}
            if raw in (None, ""):
                if f["required"]:
                    raise ValueError("please choose one")
                return
            if raw not in values:
                raise ValueError("not one of the choices")
            out[key] = raw
            for opt in f["options"]:
                detail = opt.get("detail")
                if detail and opt["value"] == raw:
                    text = _text(answers.get(detail["key"]), detail["max"], detail["key"])
                    if not text:
                        raise ValueError(f"{detail['label']} is required")
                    out[detail["key"]] = text
        elif kind == "text":
            text = _text(raw, f["max"], key)
            if f["required"] and not text:
                raise ValueError("required")
            if text:
                out[key] = text
    except ValueError as exc:
        raise ValueError(f"{label}: {exc}") from None


def _keys(f: dict) -> list:
    """A field's answer keys: its own plus any option's detail key."""
    return [f["key"]] + [o["detail"]["key"] for o in f.get("options", []) if o.get("detail")]


def validate(answers, fields: Optional[list] = None) -> dict:
    """Check answers against the questionnaire and return a cleaned copy of known keys only.

    Raises ValueError("<field label>: <reason>") for the first problem found.

    Args:
        answers: The submitted answers, a dict keyed by field.
        fields: The questionnaire to check against, ``current(conn)`` in the routers;
            ``None`` for the default.

    Raises:
        ValueError: For the first missing, unknown-choice or over-long answer.
    """
    if not isinstance(answers, dict):
        raise ValueError("Answers are missing")
    out: dict = {}
    for f in FIELDS if fields is None else fields:
        _check(f, answers, out)
    return out


def validate_edit(changes, existing: dict, fields: Optional[list] = None) -> dict:
    """Apply `changes` to the editable fields of `existing` and return the merged answers.

    Non-editable fields keep their stored values whatever `changes` says. An editable field
    absent from `changes` keeps its stored value too, so a partial update is fine. Raises
    ValueError like validate().

    Args:
        changes: The submitted answers.
        existing: The answers already stored. Answers to questions no longer asked stay.
        fields: The questionnaire to check against, ``current(conn)`` in the routers;
            ``None`` for the default.
    """
    if not isinstance(changes, dict):
        raise ValueError("Answers are missing")
    out = dict(existing or {})
    for f in FIELDS if fields is None else fields:
        if not f.get("editable"):
            continue
        merged = {k: (changes[k] if k in changes else out.get(k)) for k in _keys(f)}
        for k in _keys(f):
            out.pop(k, None)
        _check(f, merged, out)
    return out
