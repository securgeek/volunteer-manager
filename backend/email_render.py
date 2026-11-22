"""The schedule email template: Markdown in, the preview and the downloadable package out.

Organizers write the template on the site (routers/email_template.py); tools/vol_mail sends it
from the package. To keep the preview and the sent mail from drifting, **Markdown is converted
here, once**. The package carries HTML and text with ``{{placeholders}}`` still in them, and
the tool only fills those in, by the rules in ``fill`` below, which it copies. Change one,
change the other (tools/vol_mail/test_vol_mail.py checks they agree).

Placeholders, ``{{ name }}`` with optional spaces:

- subject and body: name, first_name, handle, vol_id, event_title; the body also has shifts
- shift block (repeated once per shift): day, date, time, position, category, qr

In HTML every value is escaped, except ``shifts`` (the filled shift blocks) and ``qr`` (an
``<img src="cid:…">`` the filler makes), which the filler builds itself. Images are written
``![alt](asset:logo.png)`` and stay ``src="asset:…"`` in the package; the tool attaches each
asset inline, and the preview inlines it as a data URL.

Raw HTML in the template is escaped, not passed through (markdown-it ``html: False``).
"""
import base64
import html
import io
import json
import re
import zipfile
from datetime import date
from typing import Optional

import segno
from markdown_it import MarkdownIt

FORMAT = 1

BODY_FIELDS = ("name", "first_name", "handle", "vol_id", "event_title", "shifts")
SUBJECT_FIELDS = ("name", "first_name", "handle", "vol_id", "event_title")
SHIFT_FIELDS = ("day", "date", "time", "position", "category", "qr")
RAW_HTML_FIELDS = ("shifts", "qr")

PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
ASSET_REF = re.compile(r"asset:([A-Za-z0-9._-]+)")
ASSET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

DEFAULT_SUBJECT = "Your {{event_title}} volunteer schedule"
DEFAULT_BODY = """Hi {{first_name}},

Thank you for volunteering at **{{event_title}}**! Here are your shifts:

{{shifts}}

Please show the code for each shift at the volunteer desk when you arrive. Check-in opens 45 minutes before a shift starts.

If anything here doesn't work for you, reply to this email and let us know.

Thanks,
The {{event_title}} volunteer team
"""
DEFAULT_SHIFT = """### {{day}}, {{time}}
**{{position}}** ({{category}})

{{qr}}
"""

# breaks: a single newline is a line break, as people expect writing an email ("Thanks,"
# then a name), and as the plain-text version shows it anyway.
_md = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable(["table", "strikethrough"])

# Kept deliberately plain: mail apps ignore <style> blocks unevenly, so styles are inline.
_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{event_title}}</title></head>
<body style="margin:0;padding:0;background:#f4f4f5;">
<div style="max-width:600px;margin:0 auto;padding:24px;background:#ffffff;font-family:Arial,Helvetica,sans-serif;font-size:16px;line-height:1.5;color:#111111;">
%s
</div>
</body></html>
"""


# ── checks ────────────────────────────────────────────────────────────────────

def unknown_placeholders(text: str, allowed: tuple) -> list:
    """The `{{field}}` names in a template that it isn't allowed to use.

    Args:
        text: The template text.
        allowed: The field names valid in this part of the template.

    Returns:
        The unknown names, sorted, without braces.
    """
    return sorted({m.group(1) for m in PLACEHOLDER.finditer(text or "")} - set(allowed))


def asset_refs(*texts: str) -> set:
    """The image names a template refers to as `asset:name`.

    Args:
        *texts: Template texts to scan. `None` entries are skipped.

    Returns:
        The set of asset names.
    """
    return {m.group(1) for t in texts for m in ASSET_REF.finditer(t or "")}


def check_template(subject: str, body_md: str, shift_md: str) -> list:
    """Problems that make a template unusable, as sentences; [] when it's fine.

    Args:
        subject: The subject line template.
        body_md: The body, in Markdown.
        shift_md: The block repeated once per shift, in Markdown.
    """
    problems = []
    for label, text, allowed in (("Subject", subject, SUBJECT_FIELDS),
                                 ("Body", body_md, BODY_FIELDS),
                                 ("Shift block", shift_md, SHIFT_FIELDS)):
        bad = unknown_placeholders(text, allowed)
        if bad:
            problems.append(f"{label} uses unknown placeholder(s): "
                            + ", ".join("{{%s}}" % b for b in bad)
                            + ". Available: " + ", ".join("{{%s}}" % a for a in allowed))
    if not (subject or "").strip():
        problems.append("The subject is empty.")
    if "\n" in (subject or ""):
        problems.append("The subject must be one line.")
    if "shifts" not in {m.group(1) for m in PLACEHOLDER.finditer(body_md or "")}:
        problems.append("The body doesn't include {{shifts}}, so no shifts would be listed.")
    if "qr" not in {m.group(1) for m in PLACEHOLDER.finditer(shift_md or "")}:
        problems.append("The shift block doesn't include {{qr}}, so no check-in codes would be sent.")
    return problems


# ── Markdown → package parts ─────────────────────────────────────────────────

def _html_fragment(markdown: str) -> str:
    out = _md.render(markdown or "")
    # {{shifts}} / {{qr}} alone on a line become <p>{{shifts}}</p>; the shift blocks are block
    # HTML that can't sit inside a <p>, so unwrap them to stand alone.
    out = re.sub(r"<p>\s*(\{\{\s*shifts\s*\}\})\s*</p>", r"\1", out)
    # Asset images shouldn't overflow a phone screen.
    out = out.replace('<img src="asset:', '<img style="max-width:100%;height:auto;" src="asset:')
    return out


def _text(markdown: str) -> str:
    # An image becomes its alt text; the rest of the Markdown reads fine as plain text.
    out = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", markdown or "")
    return out.strip() + "\n"


def build_parts(subject: str, body_md: str, shift_md: str) -> dict:
    """The package's template files, placeholders intact.

    Args:
        subject: The subject line template. Runs of whitespace are folded to one space.
        body_md: The body, in Markdown.
        shift_md: The per-shift block, in Markdown.

    Returns:
        `subject`, `body_html` (a whole page), `body_txt`, `shift_html` and `shift_txt`.
    """
    return {
        "subject": " ".join((subject or "").split()),
        "body_html": _PAGE % _html_fragment(body_md),
        "body_txt": _text(body_md),
        "shift_html": _html_fragment(shift_md),
        "shift_txt": _text(shift_md),
    }


# ── filling in (tools/vol_mail has a copy of this) ────────────────────────────

def fill(template: str, values: dict, as_html: bool) -> str:
    """Replace each {{field}} with its value; an unknown field is left as it was.

    Args:
        template: Text containing `{{field}}` placeholders.
        values: Field name to value. Values are converted with `str`.
        as_html: Escape values for HTML, except the fields in `RAW_HTML_FIELDS`, which are
            markup already.
    """
    def sub(m):
        key = m.group(1)
        if key not in values:
            return m.group(0)
        val = str(values[key])
        return val if (not as_html or key in RAW_HTML_FIELDS) else html.escape(val)
    return PLACEHOLDER.sub(sub, template)


def human_date(iso: Optional[str]) -> str:
    """'April 25, 2027' from '2027-04-25'; '' when there's no date.

    Args:
        iso: An ISO date, or `None`. Anything that doesn't parse comes back unchanged.
    """
    if not iso:
        return ""
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d:%B} {d.day}, {d.year}"


def qr_png(code: str) -> bytes:
    """Render a check-in code as a QR code image.

    Args:
        code: The check-in code from `checkin_codes.make_code`.

    Returns:
        PNG bytes, with medium error correction.
    """
    buf = io.BytesIO()
    segno.make(code, error="m").save(buf, kind="png", scale=6, border=2)
    return buf.getvalue()


def qr_img(src: str, shift: dict) -> str:
    """The `<img>` tag for one shift's QR code, 200 px square.

    Args:
        src: The image source: a data URL or a `cid:` reference.
        shift: The shift, whose `day` and `time` go into the alt text.

    Returns:
        The tag as HTML, with the alt text escaped.
    """
    alt = html.escape(f"Check-in code for {shift['day']} {shift['time']}", quote=True)
    return f'<img src="{src}" width="200" height="200" alt="{alt}" style="display:block;">'


def render_message(parts: dict, vol: dict, event_title: str, qr_src) -> dict:
    """One volunteer's subject, HTML and text, from build_parts output.

    vol is an export-API volunteer (names, shifts with checkin_code). qr_src(i, shift) gives the
    image source for shift i: a data URL in the preview, ``cid:qr-{i}`` in the tool.

    Args:
        parts: The output of `build_parts`.
        vol: The volunteer, as the export API returns them.
        event_title: The value for `{{event_title}}`.
        qr_src: A callable `(index, shift) -> str` giving each QR image's source.

    Returns:
        `{"subject", "html", "text"}` for this volunteer.
    """
    who = {"name": vol["name"], "first_name": vol["first_name"], "handle": vol["handle"],
           "vol_id": vol["vol_id"], "event_title": event_title}
    blocks_html, blocks_txt = [], []
    for i, s in enumerate(vol["shifts"]):
        sv = {"day": s["day"], "date": human_date(s.get("date")), "time": s["time"],
              "position": s["position"], "category": s["category"]}
        blocks_html.append(fill(parts["shift_html"], {**sv, "qr": qr_img(qr_src(i, s), s)}, True))
        blocks_txt.append(fill(parts["shift_txt"], {**sv, "qr": f"Check-in code: {s['checkin_code']}"},
                               False))
    return {
        "subject": fill(parts["subject"], who, False),
        "html": fill(parts["body_html"], {**who, "shifts": "\n".join(blocks_html)}, True),
        "text": fill(parts["body_txt"], {**who, "shifts": "\n".join(blocks_txt).strip()}, False),
    }


def inline_assets(html_text: str, assets: dict) -> str:
    """Preview only: asset:name → a data URL, from {name: (content_type, bytes)}.

    Args:
        html_text: Rendered HTML containing `asset:name` references.
        assets: Asset name to `(content_type, data)`. A missing name is left as it was.
    """
    def sub(m):
        found = assets.get(m.group(1))
        if not found:
            return m.group(0)
        ctype, data = found
        return f"data:{ctype};base64,{base64.b64encode(data).decode()}"
    return ASSET_REF.sub(sub, html_text)


# ── the package ───────────────────────────────────────────────────────────────

def build_package(subject: str, body_md: str, shift_md: str, assets: list, meta: dict) -> bytes:
    """The zip tools/vol_mail sends from. assets: dicts with name, content_type, data, sha256.

    Only assets the template refers to are included.

    Args:
        subject: The subject line template.
        body_md: The body, in Markdown.
        shift_md: The per-shift block, in Markdown.
        assets: The uploaded images, each a dict with `name`, `content_type`, `data` and
            `sha256`.
        meta: Extra manifest fields: when the package was made and by whom.

    Returns:
        The zip file's bytes: `manifest.json`, the four template files, `template.md` and the
        images used.
    """
    parts = build_parts(subject, body_md, shift_md)
    used = asset_refs(body_md, shift_md)
    files = {"body_html": "body.html", "body_txt": "body.txt",
             "shift_html": "shift.html", "shift_txt": "shift.txt"}
    manifest = {
        "format": FORMAT,
        "kind": "bsides-schedule-email",
        "subject": parts["subject"],
        **meta,
        "files": {**files, "source": "template.md"},
        "placeholders": {"subject": list(SUBJECT_FIELDS), "body": list(BODY_FIELDS),
                         "shift": list(SHIFT_FIELDS), "raw_html": list(RAW_HTML_FIELDS)},
        "assets": [{"name": a["name"], "file": f"assets/{a['name']}",
                    "content_type": a["content_type"], "sha256": a["sha256"],
                    "size": len(a["data"])}
                   for a in sorted(assets, key=lambda a: a["name"]) if a["name"] in used],
    }
    source = (f"Subject: {subject}\n\n<!-- body -->\n{body_md.rstrip()}\n\n"
              f"<!-- shift block -->\n{shift_md.rstrip()}\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        for key, fname in files.items():
            z.writestr(fname, parts[key])
        z.writestr("template.md", source)
        for a in assets:
            if a["name"] in used:
                z.writestr(f"assets/{a['name']}", a["data"])
    return buf.getvalue()
