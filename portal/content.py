"""The portal's organizer-editable content: the landing page, the questionnaire, the invite.

Organizers edit all three in the scheduler (Volunteer Portal → Landing page / Questionnaire /
Invite email), which pushes them here (``POST /api/sync/content``). Until a part has been
saved, the default below is used, which is what the site said before any of this was
editable.

This module is the portal's own copy of the rules. The scheduler has the same rules in
``backend/portal_content.py``, so it can check and preview before saving, and
``backend/test_portal_content.py`` holds the two to each other. The apps share no code. Every
push is checked here again, because a page volunteers load must not trust what arrives over
the wire:

- **Templates** (the landing page's Markdown and the invite email) are Jinja, rendered in the
  sandbox and limited to a small subset: variables from a fixed list, ``{% if %}`` with
  comparisons and ``and``/``or``/``not``, and a few text filters. No loops, no ``{% set %}``,
  no includes, calls, attribute or item access, so a template can't read anything it wasn't
  given or keep the portal busy.
- **Markdown** is rendered with raw HTML off and images off, and links only to ``http(s)``,
  ``mailto:`` or this site's own paths. The result goes into the page as HTML, so this is
  what keeps script out of it.
- **The questionnaire** must be the shape the form and ``signup_fields`` validation expect.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from jinja2 import StrictUndefined, TemplateError, TemplateSyntaxError, nodes
from jinja2.sandbox import SandboxedEnvironment
from markdown_it import MarkdownIt

# ── templates: the allowed Jinja ───────────────────────────────────────────────────────────

ALLOWED_FILTERS = frozenset({"upper", "lower", "title", "capitalize", "trim", "default"})
_ALLOWED_NODES = (nodes.Template, nodes.Output, nodes.TemplateData, nodes.Name, nodes.Const,
                  nodes.If, nodes.Not, nodes.And, nodes.Or, nodes.Compare, nodes.Operand,
                  nodes.CondExpr, nodes.Filter, nodes.Concat)
_ALLOWED_COMPARE = frozenset({"eq", "ne"})
MAX_TEMPLATE = 8000

# What each template may use. All are strings; a blank one is "" so {% if %} works on it.
LANDING_VARS = ("conference", "contact_email", "conference_url", "code_of_conduct_url")
INVITE_VARS = ("first_name", "conference", "link", "expires", "contact_email",
               "conference_url", "code_of_conduct_url")

_ENV = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False,
                            keep_trailing_newline=True)


class ContentError(ValueError):
    """Content that can't be saved or pushed: the message says what and where."""


def _check_template(source: Any, allowed: tuple, where: str, max_len: int = MAX_TEMPLATE):
    """Parse a template and refuse anything outside the allowed subset.

    Args:
        source: The template text.
        allowed: The variable names it may use.
        where: What the template is, for the error message.
        max_len: Its longest allowed length.

    Returns:
        The parsed template, and the set of variable names it uses.

    Raises:
        ContentError: Not text, too long, not valid Jinja, or using anything not allowed.
    """
    if not isinstance(source, str):
        raise ContentError(f"{where} must be text")
    if len(source) > max_len:
        raise ContentError(f"{where} is over {max_len} characters")
    try:
        tree = _ENV.parse(source)
    except TemplateSyntaxError as exc:
        raise ContentError(f"{where}, line {exc.lineno}: {exc.message}") from None
    used = set()
    for node in tree.find_all(nodes.Node):
        if not isinstance(node, _ALLOWED_NODES):
            raise ContentError(f"{where}: {type(node).__name__} isn't allowed; use variables, "
                               "{% if %} and the text filters only")
        if isinstance(node, nodes.Name):
            if node.name not in allowed:
                raise ContentError(f"{where}: unknown variable {node.name!r}; available: "
                                   + ", ".join(allowed))
            used.add(node.name)
        elif isinstance(node, nodes.Filter) and node.name not in ALLOWED_FILTERS:
            raise ContentError(f"{where}: filter {node.name!r} isn't allowed; available: "
                               + ", ".join(sorted(ALLOWED_FILTERS)))
        elif isinstance(node, nodes.Operand) and node.op not in _ALLOWED_COMPARE:
            raise ContentError(f"{where}: only == and != comparisons are allowed")
    return tree, used


def render_template(source: str, values: dict, allowed: tuple, where: str) -> str:
    """Check a template, then render it in the sandbox.

    Args:
        source: The template text.
        values: A string for each allowed variable; missing ones render as "".
        allowed: The variable names it may use.
        where: What the template is, for errors.

    Returns:
        The rendered text.

    Raises:
        ContentError: The template isn't allowed, or fails to render.
    """
    _check_template(source, allowed, where)
    context = {name: str(values.get(name) or "") for name in allowed}
    try:
        return _ENV.from_string(source).render(context)
    except (TemplateError, TypeError, ValueError) as exc:
        raise ContentError(f"{where}: {exc}") from None


# ── Markdown ───────────────────────────────────────────────────────────────────────────────

def _safe_link(url: str) -> bool:
    """Links the landing page may contain: the web, email, and this site's own paths.

    Args:
        url: The link's destination, as written.

    Returns:
        Whether it may be a link.
    """
    low = url.strip().lower()
    return (low.startswith(("https://", "http://", "mailto:", "#"))
            or (low.startswith("/") and not low.startswith("//")))


_MD = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
_MD.disable(["image", "html_block", "html_inline"])
_MD.validateLink = _safe_link


def render_markdown(source: str) -> str:
    """Markdown to HTML that's safe to put in a public page.

    Args:
        source: Markdown, after its template has been rendered.

    Returns:
        HTML. Raw HTML in the source comes out as text, and a disallowed link as plain text.
    """
    return _MD.render(source or "")


# ── the landing page ───────────────────────────────────────────────────────────────────────

# PrimeIcons the Opportunities cards may use, without the "pi pi-" prefix.
ROLE_ICONS = ("microphone", "id-card", "shield", "compass", "users", "user", "megaphone",
              "ticket", "info-circle", "question-circle", "camera", "video", "desktop", "wifi",
              "map", "map-marker", "flag", "star", "heart", "clock", "calendar", "book",
              "comments", "wrench", "cog", "bolt", "gift", "home", "building", "sitemap",
              "lock", "key", "car", "truck", "shopping-bag", "palette", "headphones")
MAX_ROLES = 12

DEFAULT_LANDING = {
    "intro": (
        "{{ conference }} is completely volunteer-run, and we need your help!\n\n"
        "Volunteers help keep every aspect of {{ conference }} running smoothly. Volunteers "
        "are needed for everything from helping run our talk tracks and training to running "
        "registration, security, and more. You get the respect of your peers for being "
        "involved with the community, and you get to learn new things!\n\n"
        "To be a volunteer, you must commit to at least **two (2) time slots** over the "
        "course of the conference. Each time slot is approximately four (4) hours and counts "
        "towards community service requirements for various programs.\n"),
    "roles_heading": "Opportunities include",
    "roles": [
        {"icon": "microphone", "name": "Room Monitors",
         "text": "Make sure talk and training tracks run smoothly."},
        {"icon": "id-card", "name": "Registration",
         "text": "Help attendees and speakers get registered."},
        {"icon": "shield", "name": "Security", "text": "Crowd control."},
        {"icon": "compass", "name": "Floater", "text": "General help across the Con."},
    ],
    "main": (
        "## Why volunteer for {{ conference }}?\n\n"
        "- Opportunity to receive admission to a massively sold-out event\n"
        "- Network with other local volunteers and staff members\n"
        "- Admission to the speaker and staff dinner\n"
        "- A way to give back to the community\n"
        "- Hours of service to a registered 501(c)(3) non-profit organization\n"),
    "sidebar": (
        "{% if code_of_conduct_url %}{{ conference }} enforces its "
        "[Code of Conduct]({{ code_of_conduct_url }}) for all conference participants, "
        "speakers, sponsors, and staff to maintain a safe and relaxing environment.{% endif %}\n\n"
        "{{ conference }} does not provide honorariums or travel assistance for volunteers.\n\n"
        "{% if contact_email %}If there are any questions, please contact "
        "[**{{ contact_email }}**](mailto:{{ contact_email }}){% endif %}\n"),
}
LANDING_PARTS = ("intro", "main", "sidebar")


def _text(raw: Any, where: str, max_len: int, required: bool = True) -> str:
    """A short single-line text field.

    Args:
        raw: The value.
        where: The field, for errors.
        max_len: Its longest allowed length.
        required: Whether it may be blank.

    Returns:
        The text, stripped.

    Raises:
        ContentError: Not text, blank when required, too long, or more than one line.
    """
    if not isinstance(raw, str):
        raise ContentError(f"{where} must be text")
    text = raw.strip()
    if required and not text:
        raise ContentError(f"{where} is required")
    if len(text) > max_len or "\n" in text or "\r" in text:
        raise ContentError(f"{where} must be one line of at most {max_len} characters")
    return text


def validate_landing(raw: Any) -> dict:
    """Check a landing page definition.

    Args:
        raw: ``intro``, ``main`` and ``sidebar`` (templated Markdown), ``roles_heading``, and
            ``roles``, each ``{icon, name, text}``.

    Returns:
        A clean copy.

    Raises:
        ContentError: For the first problem found.
    """
    if not isinstance(raw, dict) or set(raw) != set(DEFAULT_LANDING):
        raise ContentError("the landing page must have exactly: " + ", ".join(DEFAULT_LANDING))
    out: dict = {}
    for part in LANDING_PARTS:
        _check_template(raw[part], LANDING_VARS, f"Landing page {part}")
        out[part] = raw[part]
    out["roles_heading"] = _text(raw["roles_heading"], "The opportunities heading", 120,
                                 required=False)
    roles = raw["roles"]
    if not isinstance(roles, list) or len(roles) > MAX_ROLES:
        raise ContentError(f"opportunities must be a list of at most {MAX_ROLES}")
    out["roles"] = []
    for i, role in enumerate(roles, 1):
        if not isinstance(role, dict) or set(role) != {"icon", "name", "text"}:
            raise ContentError(f"opportunity {i} must have exactly icon, name and text")
        if role["icon"] not in ROLE_ICONS:
            raise ContentError(f"opportunity {i}: unknown icon {role['icon']!r}")
        out["roles"].append({"icon": role["icon"],
                             "name": _text(role["name"], f"Opportunity {i}'s name", 80),
                             "text": _text(role["text"], f"Opportunity {i}'s text", 200,
                                           required=False)})
    return out


def render_landing(landing: dict, values: dict) -> dict:
    """The landing page, ready for the browser.

    Args:
        landing: A definition that passed ``validate_landing``.
        values: The ``LANDING_VARS``, from the branding.

    Returns:
        ``intro``, ``main`` and ``sidebar`` as safe HTML, plus ``roles_heading`` and
        ``roles`` as data (the page escapes them).
    """
    out = {part: render_markdown(render_template(landing[part], values, LANDING_VARS,
                                                 f"Landing page {part}"))
           for part in LANDING_PARTS}
    return {**out, "roles_heading": landing["roles_heading"],
            "roles": copy.deepcopy(landing["roles"])}


# ── the questionnaire ──────────────────────────────────────────────────────────────────────

TSHIRT_SIZES = ["XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL"]
FIELD_TYPES = ("choice", "select", "check", "ack", "text")
SECTIONS = ("shifts", "additional")
# Read by the scheduler's assignment picker (backend routers/schedule.py get_avail_vols):
# it may be reworded or removed, but if it's there it must stay a checkbox under this key.
FLEXIBLE_KEY = "flexible"
MAX_FIELDS = 40
MAX_OPTIONS = 30
MAX_LABEL = 500
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")

# What the form asked before it was editable. Rendered in this order; `section` says which
# signup step a question is on: "shifts" under the shift picker, "additional" the step after.
DEFAULT_FIELDS = [
    {
        "key": "experience", "section": "shifts", "type": "choice", "required": True,
        "editable": False, "label": "Volunteer experience",
        "options": [
            {"value": "bsidescharm", "label": "I have volunteered for BSidesCharm before"},
            {"value": "other_con",
             "label": "I have volunteered for another BSides or security con before",
             # Shown under this option when it's picked, and required with it.
             "detail": {"key": "other_con_name", "label": "Which con?", "max": 120}},
            {"value": "first_time", "label": "This is my first time volunteering"},
        ],
    },
    {
        # Optional, unlike the acknowledgements: unticked is a real answer (False).
        "key": FLEXIBLE_KEY, "section": "shifts", "type": "check", "required": False,
        "editable": True,
        "label": "I'm flexible: an organizer may move me to a different shift on the same day "
                 "if needed.",
    },
    {
        "key": "ack_arrive_early", "section": "shifts", "type": "ack", "required": True,
        "editable": False,
        "label": "I understand that I must arrive at least 15 minutes before my scheduled "
                 "volunteer time slot.",
    },
    {
        "key": "ack_full_slot", "section": "shifts", "type": "ack", "required": True,
        "editable": False,
        "label": "I understand that I must volunteer for the entire time of my selected "
                 "time slots.",
    },
    {
        "key": "tshirt", "section": "additional", "type": "select", "required": True,
        "editable": True, "label": "T-shirt size",
        "options": [{"value": s, "label": s} for s in TSHIRT_SIZES],
    },
    {
        "key": "dinner", "section": "additional", "type": "choice", "required": True,
        "editable": True,
        "label": "Will you be attending the Volunteer and Speaker Dinner (held the night "
                 "before the conference)?",
        "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"},
                    {"value": "unsure", "label": "I'm not sure yet"}],
    },
    {
        "key": "dietary", "section": "additional", "type": "text", "required": False,
        "editable": False, "label": "Dietary restrictions", "max": 300,
    },
]

_FIELD_KEYS = {"key", "section", "type", "required", "editable", "label", "options", "max"}


def _bool(raw: Any, where: str) -> bool:
    if not isinstance(raw, bool):
        raise ContentError(f"{where} must be true or false")
    return raw


def _limit(raw: Any, where: str, top: int) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or not 1 <= raw <= top:
        raise ContentError(f"{where} must be a whole number from 1 to {top}")
    return raw


def _option(raw: Any, where: str, keys: set) -> dict:
    """One choice or dropdown option.

    Args:
        raw: ``{value, label}``, optionally with ``detail``: ``{key, label, max}``.
        where: The option, for errors.
        keys: Answer keys already taken; a detail's key is added.

    Returns:
        A clean copy.

    Raises:
        ContentError: For the first problem found.
    """
    if not isinstance(raw, dict) or not {"value", "label"} <= set(raw) <= {"value", "label",
                                                                         "detail"}:
        raise ContentError(f"{where} must have a value and a label (and optionally a detail)")
    if not isinstance(raw["value"], str) or not _VALUE.match(raw["value"]):
        raise ContentError(f"{where}: its value must be letters, digits, - or _")
    out = {"value": raw["value"], "label": _text(raw["label"], f"{where}'s label", 300)}
    detail = raw.get("detail")
    if detail is not None:
        if not isinstance(detail, dict) or set(detail) != {"key", "label", "max"}:
            raise ContentError(f"{where}: its follow-up must have a key, a label and a max")
        if not isinstance(detail["key"], str) or not _KEY.match(detail["key"]):
            raise ContentError(f"{where}: its follow-up key must be lowercase letters, digits "
                               "or _, starting with a letter")
        if detail["key"] in keys:
            raise ContentError(f"{where}: the key {detail['key']!r} is used twice")
        keys.add(detail["key"])
        out["detail"] = {"key": detail["key"],
                         "label": _text(detail["label"], f"{where}'s follow-up label", 200),
                         "max": _limit(detail["max"], f"{where}'s follow-up length", 500)}
    return out


def validate_fields(raw: Any) -> list:
    """Check a questionnaire definition.

    Args:
        raw: A list of questions, each ``key``, ``section``, ``type``, ``required``,
            ``editable`` and ``label``, plus ``options`` (choice, select) or ``max`` (text).

    Returns:
        A clean copy, in the same order.

    Raises:
        ContentError: For the first problem found.
    """
    if not isinstance(raw, list) or len(raw) > MAX_FIELDS:
        raise ContentError(f"the questionnaire must be a list of at most {MAX_FIELDS} questions")
    keys: set = set()
    out = []
    for i, field in enumerate(raw, 1):
        where = f"Question {i}"
        if not isinstance(field, dict) or not set(field) <= _FIELD_KEYS:
            raise ContentError(f"{where} has unknown settings")
        key = field.get("key")
        if not isinstance(key, str) or not _KEY.match(key):
            raise ContentError(f"{where}: its key must be lowercase letters, digits or _, "
                               "starting with a letter")
        if key in keys:
            raise ContentError(f"{where}: the key {key!r} is used twice")
        keys.add(key)
        kind = field.get("type")
        if kind not in FIELD_TYPES:
            raise ContentError(f"{where}: its type must be one of " + ", ".join(FIELD_TYPES))
        if field.get("section") not in SECTIONS:
            raise ContentError(f"{where}: its step must be one of " + ", ".join(SECTIONS))
        clean = {"key": key, "section": field["section"], "type": kind,
                 "required": _bool(field.get("required"), f"{where}'s required"),
                 "editable": _bool(field.get("editable", False), f"{where}'s editable"),
                 "label": _text(field.get("label"), f"{where}'s label", MAX_LABEL)}
        if kind == "ack" and not clean["required"]:
            raise ContentError(f"{where}: an acknowledgement is always required")
        if kind == "check" and clean["required"]:
            raise ContentError(f"{where}: a checkbox can't be required; use an acknowledgement")
        if key == FLEXIBLE_KEY and kind != "check":
            raise ContentError(f"{where}: {FLEXIBLE_KEY!r} is read by the assignment picker "
                               "and must stay a checkbox")
        if kind in ("choice", "select"):
            options = field.get("options")
            if not isinstance(options, list) or not 1 <= len(options) <= MAX_OPTIONS:
                raise ContentError(f"{where} needs 1 to {MAX_OPTIONS} options")
            clean["options"] = [_option(o, f"{where}, option {j}", keys)
                                for j, o in enumerate(options, 1)]
            values = [o["value"] for o in clean["options"]]
            if len(set(values)) != len(values):
                raise ContentError(f"{where}: two options have the same value")
            if kind == "select" and any("detail" in o for o in clean["options"]):
                raise ContentError(f"{where}: only a choice can ask a follow-up")
        elif "options" in field:
            raise ContentError(f"{where}: only a choice or a dropdown has options")
        if kind == "text":
            clean["max"] = _limit(field.get("max", 300), f"{where}'s length", 2000)
        elif "max" in field:
            raise ContentError(f"{where}: only a text question has a length")
        out.append(clean)
    return out


# ── the invite email ───────────────────────────────────────────────────────────────────────

MAX_INVITE_SUBJECT = 200
DEFAULT_INVITE = {
    "subject": "You're invited to volunteer at {{ conference }}",
    "body": (
        "{{ conference }} volunteers\n\n"
        "Hi {{ first_name }},\n\n"
        "You're invited to sign up to volunteer at {{ conference }} before the call for "
        "volunteers\nopens to everyone. Use the link below to sign up and choose your "
        "shifts:\n\n"
        "{{ link }}\n\n"
        "The link is just for you. It works once, until {{ expires }}. You'll be asked for "
        "your\nemail address and sent a link to confirm it, then you can fill in the rest.\n\n"
        "{{ conference }} is completely volunteer-run. Each volunteer commits to at least two "
        "shifts of\nabout four hours. Your signup goes to the organizers for approval.\n\n"
        "Questions? Just reply to this email.\n"),
}


def validate_invite(raw: Any) -> dict:
    """Check an invite email template.

    Args:
        raw: ``{subject, body}``, both Jinja with ``INVITE_VARS``.

    Returns:
        A clean copy.

    Raises:
        ContentError: For the first problem found, including a body without ``{{ link }}``.
    """
    if not isinstance(raw, dict) or set(raw) != {"subject", "body"}:
        raise ContentError("the invite email must have exactly a subject and a body")
    subject = raw["subject"]
    _check_template(subject, INVITE_VARS, "The subject", MAX_INVITE_SUBJECT)
    if not isinstance(subject, str) or not subject.strip() or "\n" in subject:
        raise ContentError("The subject must be one line")
    _, used = _check_template(raw["body"], INVITE_VARS, "The body")
    if "link" not in used:
        raise ContentError("The body must include {{ link }}, or the invite can't be used")
    return {"subject": subject.strip(), "body": raw["body"]}


def render_invite(invite: dict, values: dict) -> tuple[str, str]:
    """An invite email's subject and body.

    Args:
        invite: A template that passed ``validate_invite``.
        values: The ``INVITE_VARS``.

    Returns:
        ``(subject, body)``. The subject is folded to one line.
    """
    subject = render_template(invite["subject"], values, INVITE_VARS, "The subject")
    body = render_template(invite["body"], values, INVITE_VARS, "The body")
    return " ".join(subject.split()), body


# ── the whole push ─────────────────────────────────────────────────────────────────────────

def validate_content(raw: Any) -> dict:
    """Check a content push: each part is a definition, or ``None`` for the default.

    Args:
        raw: ``{landing, questionnaire, invite}``.

    Returns:
        A clean copy, with ``None`` kept for a part left at its default.

    Raises:
        ContentError: For the first problem found.
    """
    if not isinstance(raw, dict):
        raise ContentError("content must be an object")
    return {
        "landing": None if raw.get("landing") is None else validate_landing(raw["landing"]),
        "questionnaire": None if raw.get("questionnaire") is None
        else validate_fields(raw["questionnaire"]),
        "invite": None if raw.get("invite") is None else validate_invite(raw["invite"]),
    }
