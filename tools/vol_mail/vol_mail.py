#!/usr/bin/env python3
"""vol_mail — send volunteers their schedule, with a check-in QR code for each shift.

Run on an organizer's own machine, once or twice before the conference (a draft, then a final
after adjustments). It is not part of the website: no container copies it, and the site never
sends this mail itself.

    vol_mail.py fetch   --url https://<site> --key-file api_key.txt --out box.json
    vol_mail.py preview --package schedule-email.zip --box box.json --vol 1042 --out preview/
    vol_mail.py send    --package schedule-email.zip --box box.json --key private.pem --round draft --dry-run
    vol_mail.py send    --package schedule-email.zip --box box.json --key private.pem --round draft
    vol_mail.py send    ... --round final --only-changed-since draft --dry-run   (then without --dry-run)

Inputs:
- **The package** — Call for Volunteers → Schedule Email → Download package. Markdown is
  already HTML in it; this tool only fills in placeholders, by the same rules as the site's
  preview (backend/email_render.py ``fill`` / ``render_message``, copied below; the test suite
  checks the two agree).
- **The box** — ``fetch`` saves the export API's volunteers, with each address still
  encrypted and each shift's check-in code. Needs an API key with volunteers:read,
  volunteers:email and checkin:codes (Admin → API Keys). ``fetch`` is the only command that
  talks to the scheduler.
- **private.pem** — the organizers' key, which decrypts addresses in memory, one at a time.

Sending is Microsoft Graph, the same way the mail relay does it (mailrelay/relay.py): a
delegated device-code sign-in as VOL_MAIL_MAILBOX, which has Send As on the From address
(MAIL_SENDER, or ``--from``). The
sign-in is kept in memory only; nothing is saved, so each send run signs in afresh.

Safety rails:
- ``send`` needs a ``--dry-run`` of the same round first. The dry run checks every address
  decrypts, renders every message, and writes the round's plan; the real run sends exactly
  that plan, and refuses if the package or box changed since.
- At most ``--per-minute`` sends (20; Graph allows 30 a minute per mailbox). Interrupted? Run
  the same command again: anyone already sent in that round is skipped.
- State and output record vol_ids only, never an address.
"""
import argparse
import base64
import getpass
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timezone

# The From address: --from, or MAIL_SENDER from the environment (the same setting the mail
# relay uses). There's no built-in default, so one conference can't send as another.
SENDER = os.environ.get("MAIL_SENDER", "").strip()
# Previews are never sent; their .eml needs some From all the same.
PREVIEW_SENDER = "volunteers@example.invalid"

GRAPH_LOGIN = "https://login.microsoftonline.com"
GRAPH_API = "https://graph.microsoft.com/v1.0"
SCOPES = "https://graph.microsoft.com/Mail.Send offline_access openid profile"
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

PACKAGE_FORMAT = 1
MAX_GRAPH_REQUEST = 3_500_000      # Graph's sendMail JSON limit is 4 MB; stay clear of it
_ADDR = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")

# Tests swap these out.
_urlopen = urllib.request.urlopen
_sleep = time.sleep


class Fail(RuntimeError):
    """Stop with this message. Never contains an address."""


# ── filling in: a copy of backend/email_render.py, keep them in step ──────────

PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
ASSET_REF = re.compile(r"asset:([A-Za-z0-9._-]+)")
RAW_HTML_FIELDS = ("shifts", "qr")


def fill(template: str, values: dict, as_html: bool) -> str:
    """Replace each `{{field}}` with its value. A copy of `backend/email_render.fill`.

    Args:
        template: Text containing `{{field}}` placeholders.
        values: Field name to value. An unknown field is left as it was.
        as_html: Escape values for HTML, except the fields in `RAW_HTML_FIELDS`.

    Returns:
        The filled-in text.
    """
    def sub(m):
        key = m.group(1)
        if key not in values:
            return m.group(0)
        val = str(values[key])
        return val if (not as_html or key in RAW_HTML_FIELDS) else html.escape(val)
    return PLACEHOLDER.sub(sub, template)


def human_date(iso):
    """`'April 25, 2027'` from `'2027-04-25'`. A copy of the backend's.

    Args:
        iso: An ISO date, or `None`. Anything that doesn't parse comes back unchanged.

    Returns:
        The date in words, or `''`.
    """
    if not iso:
        return ""
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d:%B} {d.day}, {d.year}"


def qr_img(src: str, shift: dict) -> str:
    """The `<img>` tag for one shift's QR code, 200 px square.

    Args:
        src: The image source, `cid:qr-{i}` here.
        shift: The shift, whose `day` and `time` go into the alt text.

    Returns:
        The tag as HTML.
    """
    alt = html.escape(f"Check-in code for {shift['day']} {shift['time']}", quote=True)
    return f'<img src="{src}" width="200" height="200" alt="{alt}" style="display:block;">'


def render_message(parts: dict, vol: dict, event_title: str, qr_src) -> dict:
    """One volunteer's subject, HTML and text. A copy of the backend's, so the site's
    preview and the sent message match.

    Args:
        parts: The package's template parts.
        vol: The volunteer, from the box.
        event_title: The value for `{{event_title}}`.
        qr_src: A callable `(index, shift) -> str` giving each QR image's source.

    Returns:
        `{"subject", "html", "text"}`.
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


def qr_png(code: str) -> bytes:
    """Render a check-in code as a QR code image.

    Args:
        code: The shift's check-in code.

    Returns:
        PNG bytes.
    """
    import segno
    buf = io.BytesIO()
    segno.make(code, error="m").save(buf, kind="png", scale=6, border=2)
    return buf.getvalue()


# ── inputs ────────────────────────────────────────────────────────────────────

def sha256_file(path: str) -> str:
    """Hash a file, to tie a round's plan to the exact package and box it was made from.

    Args:
        path: The file.

    Returns:
        The SHA-256 hex digest.
    """
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def load_package(path: str) -> dict:
    """The package's parts and assets, checked against its manifest.

    Args:
        path: The zip downloaded from Schedule Email.

    Returns:
        `manifest`, `parts` (the template files) and `assets` (name to `(type, bytes)`).

    Raises:
        Fail: Not a package, the wrong format, a bad checksum, or a missing image.
    """
    try:
        with zipfile.ZipFile(path) as z:
            return _read_package(path, z)
    except (OSError, zipfile.BadZipFile) as exc:
        raise Fail(f"{path} isn't a schedule email package ({exc})") from exc


def _read_package(path: str, z: zipfile.ZipFile) -> dict:
    try:
        manifest = json.loads(z.read("manifest.json"))
    except (KeyError, ValueError) as exc:
        raise Fail(f"{path} isn't a schedule email package ({exc})") from exc
    if manifest.get("kind") != "bsides-schedule-email" or manifest.get("format") != PACKAGE_FORMAT:
        raise Fail(f"{path} is format {manifest.get('format')!r}; this tool reads format "
                   f"{PACKAGE_FORMAT}. Update the tool or download the package again.")
    files = manifest["files"]
    parts = {"subject": manifest["subject"],
             **{k: z.read(files[k]).decode() for k in ("body_html", "body_txt", "shift_html", "shift_txt")}}
    assets = {}
    for a in manifest["assets"]:
        data = z.read(a["file"])
        if hashlib.sha256(data).hexdigest() != a["sha256"]:
            raise Fail(f"{path}: {a['name']} doesn't match its checksum; download it again")
        assets[a["name"]] = (a["content_type"], data)
    missing = {m.group(1) for m in ASSET_REF.finditer(parts["body_html"] + parts["shift_html"])} - set(assets)
    if missing:
        raise Fail(f"{path} refers to images it doesn't contain: {', '.join(sorted(missing))}")
    return {"manifest": manifest, "parts": parts, "assets": assets}


def load_box(path: str) -> dict:
    """Read a box written by `fetch`, and check it has what sending needs.

    Args:
        path: The box file.

    Returns:
        The box: the export API's volunteers response.

    Raises:
        Fail: Not a box, or the API key lacked the `checkin:codes` or `volunteers:email` scope.
    """
    try:
        with open(path, encoding="utf-8") as f:
            box = json.load(f)
        box["volunteers"]
    except (OSError, ValueError, KeyError) as exc:
        raise Fail(f"{path} isn't a box from `vol_mail.py fetch` ({exc})") from exc
    for v in box["volunteers"]:
        for s in v["shifts"]:
            if "checkin_code" not in s:
                raise Fail(f"{path} has no check-in codes: the API key needs the checkin:codes scope")
        if "email_enc" not in v:
            raise Fail(f"{path} has no encrypted emails: the API key needs the volunteers:email scope")
    return box


def load_private_key(path: str):
    """Load the organizers' private key, asking for its passphrase if it has one.

    Args:
        path: The PEM file (`private.pem`).

    Returns:
        The key object.

    Raises:
        Fail: When the file isn't a PEM private key.
    """
    from cryptography.hazmat.primitives import serialization
    with open(path, "rb") as f:
        pem = f.read()
    try:
        return serialization.load_pem_private_key(pem, password=None)
    except TypeError:     # encrypted key
        pw = getpass.getpass(f"Passphrase for {path}: ").encode()
        return serialization.load_pem_private_key(pem, password=pw)
    except ValueError as exc:
        raise Fail(f"{path} isn't a PEM private key ({exc})") from exc


def decrypt_email(key, email_enc: str) -> str:
    """Decrypt one volunteer's address. It stays in memory only.

    Args:
        key: The private key from `load_private_key`.
        email_enc: The base64 RSA-OAEP ciphertext from the box.

    Returns:
        The address.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    pt = key.decrypt(base64.b64decode(email_enc),
                     padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                                  algorithm=hashes.SHA256(), label=None))
    return pt.decode().strip()


def shifts_hash(vol: dict) -> str:
    """What the volunteer sees of their schedule; --only-changed-since compares this.

    Args:
        vol: The volunteer, from the box.
    """
    seen = [[s["slot_key"], s["day"], s.get("date"), s["time"], s["position"], s["category"]]
            for s in vol["shifts"]]
    return hashlib.sha256(json.dumps(seen, sort_keys=True).encode()).hexdigest()


# ── one message ───────────────────────────────────────────────────────────────

def build_message(pkg: dict, box: dict, vol: dict) -> dict:
    """Subject, HTML (with cid: images), text, and the inline attachments it needs.

    Args:
        pkg: The package from `load_package`.
        box: The box from `load_box`, for the event title.
        vol: The volunteer.
    """
    msg = render_message(pkg["parts"], vol, box.get("event_title", ""), lambda i, s: f"cid:qr-{i}")
    attachments = [{"cid": f"qr-{i}", "name": f"checkin-{i + 1}.png", "content_type": "image/png",
                    "data": qr_png(s["checkin_code"])} for i, s in enumerate(vol["shifts"])]
    used = sorted({m.group(1) for m in ASSET_REF.finditer(msg["html"])})
    for name in used:
        ctype, data = pkg["assets"][name]
        attachments.append({"cid": f"asset-{name}", "name": name, "content_type": ctype, "data": data})
    msg["html"] = ASSET_REF.sub(lambda m: f"cid:asset-{m.group(1)}", msg["html"])
    msg["attachments"] = attachments
    return msg


def message_digest(msg: dict) -> str:
    """A hash of one finished message, recorded in the plan so `send` can tell whether it changed.

    Args:
        msg: The message from `build_message`.

    Returns:
        The SHA-256 hex digest over the subject, both bodies and every attachment.
    """
    h = hashlib.sha256()
    for part in (msg["subject"], msg["html"], msg["text"]):
        h.update(part.encode() + b"\0")
    for a in msg["attachments"]:
        h.update(a["cid"].encode() + hashlib.sha256(a["data"]).digest())
    return h.hexdigest()


def graph_payload(msg: dict, to: str, sender: str) -> dict:
    """The `sendMail` request body for one message, images attached inline.

    Args:
        msg: The message from `build_message`.
        to: The recipient.
        sender: The From address.

    Returns:
        The JSON body as a dict.
    """
    return {
        "message": {
            "from": {"emailAddress": {"address": sender}},
            "subject": msg["subject"],
            "body": {"contentType": "HTML", "content": msg["html"]},
            "toRecipients": [{"emailAddress": {"address": to}}],
            "attachments": [{
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": a["name"], "contentType": a["content_type"],
                "contentBytes": base64.b64encode(a["data"]).decode(),
                "isInline": True, "contentId": a["cid"],
            } for a in msg["attachments"]],
        },
        "saveToSentItems": True,
    }


def write_eml(msg: dict, path: str, to: str = "preview@example.invalid",
              sender: str = "") -> None:
    """A .eml to open in a mail app: text and HTML alternatives, images inline.

    Args:
        msg: The message from `build_message`.
        path: Where to write it.
        to: The To header. A placeholder by default, since previews aren't sent.
        sender: The From header. Blank uses MAIL_SENDER, or a placeholder for a preview.
    """
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender or SENDER or PREVIEW_SENDER, to, msg["subject"]
    m.set_content(msg["text"])
    m.add_alternative(msg["html"], subtype="html")
    html_part = m.get_payload()[1]
    for a in msg["attachments"]:
        maintype, subtype = a["content_type"].split("/")
        html_part.add_related(a["data"], maintype=maintype, subtype=subtype,
                              cid=f"<{a['cid']}>", filename=a["name"])
    with open(path, "wb") as f:
        f.write(bytes(m))


# ── Graph (from mailrelay/relay.py; the sign-in is never saved) ───────────────

def _error_body(exc):
    try:
        return json.loads(exc.read() or b"{}")
    except ValueError:
        return {}


def _describe(code, body):
    err = body.get("error")
    if isinstance(err, dict):
        return f"HTTP {code} {err.get('code')}: {err.get('message')}"
    if not err:
        return f"HTTP {code}"
    desc = (body.get("error_description") or "").splitlines()
    return f"HTTP {code} {err}: {desc[0] if desc else ''}"


def _post_form(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with _urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _id_token_user(id_token):
    try:
        part = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("preferred_username") or ""
    except (AttributeError, IndexError, ValueError):
        return ""


class Graph:
    """Delegated Graph client. Signs in by device code; tokens live in this object only.

    Args:
        tenant_id: The Entra tenant.
        client_id: The app registration.
        mailbox: The account that must sign in.
        sender: The From address.
        out: Where to print the device-code instructions.
    """

    def __init__(self, tenant_id: str, client_id: str, mailbox: str, sender: str, out=print):
        self.tenant_id, self.client_id, self.mailbox, self.sender = tenant_id, client_id, mailbox, sender
        self.out = out
        self.base = f"{GRAPH_LOGIN}/{urllib.parse.quote(tenant_id)}/oauth2/v2.0"
        self.token, self.refresh, self.expires = None, None, 0.0

    def _keep(self, tok: dict) -> None:
        self.token = tok["access_token"]
        self.refresh = tok.get("refresh_token") or self.refresh
        self.expires = time.time() + int(tok.get("expires_in", 3600)) - 60

    def sign_in(self) -> str:
        """Device-code sign-in as `mailbox`. Tokens stay in this object, never on disk.

        Returns:
            The account that signed in.

        Raises:
            Fail: When sign-in fails, times out, or signs in as another account.
        """
        try:
            dc = _post_form(f"{self.base}/devicecode", {"client_id": self.client_id, "scope": SCOPES})
        except urllib.error.HTTPError as exc:
            raise Fail(f"device code request failed: "
                       f"{_describe(exc.code, _error_body(exc))}") from exc
        self.out(dc["message"])
        interval = int(dc.get("interval", 5))
        deadline = time.time() + int(dc.get("expires_in", 900))
        while time.time() < deadline:
            _sleep(interval)
            try:
                tok = _post_form(f"{self.base}/token", {"client_id": self.client_id,
                                                        "grant_type": DEVICE_CODE_GRANT,
                                                        "device_code": dc["device_code"]})
            except urllib.error.HTTPError as exc:
                err = _error_body(exc)
                if err.get("error") == "authorization_pending":
                    continue
                if err.get("error") == "slow_down":
                    interval += 5
                    continue
                raise Fail(f"sign-in failed: {_describe(exc.code, err)}") from exc
            user = _id_token_user(tok.get("id_token"))
            if user.lower() != self.mailbox.lower():
                raise Fail(f"signed in as {user or 'an unknown account'}, but VOL_MAIL_MAILBOX is "
                           f"{self.mailbox}")
            self._keep(tok)
            return user
        raise Fail("the device code expired before sign-in finished; run the command again")

    def _access_token(self) -> str:
        if self.token and time.time() < self.expires:
            return self.token
        if not self.refresh:
            raise Fail("not signed in")
        try:
            self._keep(_post_form(f"{self.base}/token", {"client_id": self.client_id,
                                                         "grant_type": "refresh_token",
                                                         "refresh_token": self.refresh,
                                                         "scope": SCOPES}))
        except urllib.error.HTTPError as exc:
            raise Fail(f"token refresh failed: "
                       f"{_describe(exc.code, _error_body(exc))}") from exc
        return self.token

    def send(self, msg: dict, to: str) -> None:
        """Send one message, waiting out a 429 (up to three times). Raises Fail otherwise.

        Args:
            msg: The message from `build_message`.
            to: The recipient.
        """
        data = json.dumps(graph_payload(msg, to, self.sender)).encode()
        if len(data) > MAX_GRAPH_REQUEST:
            raise Fail(f"message is {len(data) // 1024} KB, over Graph's limit; use smaller images")
        for attempt in range(4):
            req = urllib.request.Request(
                f"{GRAPH_API}/me/sendMail", data=data, method="POST",
                headers={"Authorization": f"Bearer {self._access_token()}",
                         "Content-Type": "application/json"})
            try:
                with _urlopen(req, timeout=60):
                    return
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    wait = int(exc.headers.get("Retry-After") or 30)
                    self.out(f"  Graph asked to slow down; waiting {wait}s")
                    _sleep(wait)
                    continue
                if exc.code == 401 and attempt < 3:
                    self.token = None
                    continue
                raise Fail(f"Graph sendMail failed: "
                           f"{_describe(exc.code, _error_body(exc))}") from exc


# ── state for a round ─────────────────────────────────────────────────────────

def _state_paths(state_dir: str, rnd: str):
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$", rnd):
        raise Fail("--round must be a short name like draft or final")
    return (os.path.join(state_dir, f"{rnd}.plan.json"), os.path.join(state_dir, f"{rnd}.sent.jsonl"))


def read_sent(path: str) -> dict:
    """vol_id → its last successful send record in this round.

    Args:
        path: The round's send log, one JSON record per line.
    """
    sent = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    if rec.get("ok"):
                        sent[rec["vol_id"]] = rec
    return sent


def _append(path: str, rec: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _write_private(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_fetch(args, out=print) -> int:
    """`fetch`: download the volunteers, with codes and encrypted addresses, into a box file.

    Args:
        args: The parsed command line: `url`, `key_file`, `out`, `status`, `insecure_http`.
        out: Where to print progress.

    Returns:
        The exit status.

    Raises:
        Fail: When the URL isn't https, the request fails, or the key lacks a scope.
    """
    with open(args.key_file, encoding="utf-8") as f:
        api_key = f.read().strip()
    url = args.url.rstrip("/") + "/api/export/volunteers?" + urllib.parse.urlencode(
        {"status": args.status, "assigned": "1"})
    if not url.startswith("https://") and not args.insecure_http:
        raise Fail("the URL must be https:// (the API key and addresses travel with it)")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with _urlopen(req, timeout=60) as resp:
            box = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        hint = {401: " (wrong, expired or revoked key, missing scope, or not an allowed IP)",
                429: " (too many failed attempts from this address; wait 15 minutes)"}.get(exc.code, "")
        raise Fail(f"the scheduler said HTTP {exc.code}{hint}") from exc
    except urllib.error.URLError as exc:
        raise Fail(f"couldn't reach {args.url}: {exc.reason}") from exc
    box["fetched_from"] = args.url
    _write_private(args.out, json.dumps(box, indent=1) + "\n")
    vols = box["volunteers"]
    no_email = sum(1 for v in vols if not v.get("email_enc"))
    out(f"Saved {len(vols)} volunteer(s) with shifts to {args.out} "
        f"({no_email} without an email). Generated {box.get('generated_at')}.")
    load_box(args.out)    # say now, not at send time, if the key lacked a scope
    return 0


def cmd_preview(args, out=print) -> int:
    """`preview`: write messages as `.eml`, `.html` and `.txt` files, sending nothing.

    Args:
        args: The parsed command line: `package`, `box`, `vol`, `out`.
        out: Where to print progress.

    Returns:
        The exit status.

    Raises:
        Fail: When the package or box is bad, or the volunteer isn't in the box.
    """
    pkg, box = load_package(args.package), load_box(args.box)
    vols = [v for v in box["volunteers"] if v["vol_id"] == args.vol] if args.vol else box["volunteers"][:1]
    if not vols:
        raise Fail(f"no volunteer {args.vol} in {args.box}")
    os.makedirs(args.out, exist_ok=True)
    for v in vols:
        msg = build_message(pkg, box, v)
        eml = os.path.join(args.out, f"{v['vol_id']}.eml")
        write_eml(msg, eml)
        # The same HTML with images inlined, for a browser.
        page = msg["html"]
        for a in msg["attachments"]:
            page = page.replace(f"cid:{a['cid']}",
                                f"data:{a['content_type']};base64,{base64.b64encode(a['data']).decode()}")
        with open(os.path.join(args.out, f"{v['vol_id']}.html"), "w", encoding="utf-8") as f:
            f.write(page)
        with open(os.path.join(args.out, f"{v['vol_id']}.txt"), "w", encoding="utf-8") as f:
            f.write(f"Subject: {msg['subject']}\n\n{msg['text']}")
        out(f"{v['vol_id']}: {eml} (.html and .txt beside it)")
    return 0


def _select(box, args, out) -> tuple:
    """(to_send, skipped) for a dry run, from --only and --only-changed-since."""
    only = None
    if args.only:
        with open(args.only, encoding="utf-8") as f:
            only = {line.strip() for line in f if line.strip() and not line.startswith("#")}
    since = None
    if args.only_changed_since:
        _, since_sent = _state_paths(args.state, args.only_changed_since)
        since = read_sent(since_sent)
        if not since:
            raise Fail(f"nothing was sent in round {args.only_changed_since!r}, so nothing to compare with")
    to_send, skipped = [], []
    for v in box["volunteers"]:
        if only is not None and v["vol_id"] not in only:
            continue
        if not v["shifts"]:
            skipped.append((v["vol_id"], "no shifts"))
        elif not v.get("email_enc"):
            skipped.append((v["vol_id"], "no email on file"))
        elif since is not None and since.get(v["vol_id"], {}).get("shifts_hash") == shifts_hash(v):
            skipped.append((v["vol_id"], f"unchanged since {args.only_changed_since}"))
        else:
            to_send.append(v)
    if only:
        missing = only - {v["vol_id"] for v in box["volunteers"]}
        for vid in sorted(missing):
            skipped.append((vid, "not in the box (no shifts, or not active)"))
    return to_send, skipped


def cmd_send(args, graph_factory=None, out=print, confirm=input) -> int:
    """`send`: plan a round with `--dry-run`, then send it.

    The dry run builds every message and writes a plan. A real run sends the plan, skipping
    anyone the round's log already shows as sent, so an interrupted round can be resumed. It
    refuses if the package or box changed since the plan was made.

    Args:
        args: The parsed command line (see `main`).
        graph_factory: Builds the Graph client. Tests pass a fake.
        out: Where to print progress.
        confirm: Asks before sending. Skipped with `--yes`.

    Returns:
        0 when everything went, 1 when anything failed or the operator declined.

    Raises:
        Fail: No plan for the round, or the inputs changed since it was made.
    """
    os.makedirs(args.state, mode=0o700, exist_ok=True)
    plan_path, sent_path = _state_paths(args.state, args.round)
    pkg, box = load_package(args.package), load_box(args.box)
    fingerprint = {"package_sha256": sha256_file(args.package), "box_sha256": sha256_file(args.box)}
    key = load_private_key(args.key)

    if args.dry_run:
        to_send, skipped = _select(box, args, out)
        recipients, bad = [], []
        for v in to_send:
            try:
                addr = decrypt_email(key, v["email_enc"])
            # Deliberately broad: whatever stops one address decrypting skips that volunteer
            # (reported below), not the whole run.
            except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                bad.append((v["vol_id"], "address doesn't decrypt with this key"))
                continue
            if not _ADDR.match(addr):
                bad.append((v["vol_id"], "decrypted address isn't a plain address"))
                continue
            msg = build_message(pkg, box, v)
            recipients.append({"vol_id": v["vol_id"], "shifts": len(v["shifts"]),
                               "shifts_hash": shifts_hash(v), "message_sha256": message_digest(msg)})
        skipped += bad
        plan = {"round": args.round, "created_at": _now(), **fingerprint,
                "box_generated_at": box.get("generated_at"), "subject": pkg["parts"]["subject"],
                "only_changed_since": args.only_changed_since,
                "recipients": recipients, "skipped": [{"vol_id": a, "reason": b} for a, b in skipped]}
        _write_private(plan_path, json.dumps(plan, indent=1) + "\n")
        already = read_sent(sent_path)
        out(f"Round {args.round!r}: {len(recipients)} to send"
            + (f" ({sum(1 for r in recipients if r['vol_id'] in already)} already sent in this round)"
               if already else "") + f", {len(skipped)} skipped. Box generated {box.get('generated_at')}.")
        for vid, why in skipped:
            out(f"  skip {vid}: {why}")
        out(f"Plan written to {plan_path}. Run the same command without --dry-run to send.")
        return 1 if bad else 0

    if not os.path.exists(plan_path):
        raise Fail(f"no plan for round {args.round!r}; run it with --dry-run first")
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)
    for k, label in (("package_sha256", "package"), ("box_sha256", "box")):
        if plan[k] != fingerprint[k]:
            raise Fail(f"the {label} changed since the dry run; run --dry-run again")
    already = read_sent(sent_path)
    todo = [r for r in plan["recipients"] if r["vol_id"] not in already]
    if not todo:
        out(f"Round {args.round!r}: everyone in the plan has been sent. Nothing to do.")
        return 0
    by_id = {v["vol_id"]: v for v in box["volunteers"]}

    graph = (graph_factory or _graph_from_env)(args, out)
    out(f"Round {args.round!r}: about to send {len(todo)} email(s) as {graph.sender}"
        + (f" ({len(already)} already sent)" if already else "") + ".")
    if not args.yes and confirm("Type send to continue: ").strip().lower() != "send":
        out("Cancelled; nothing sent.")
        return 1
    who = graph.sign_in()
    out(f"Signed in as {who}.")

    gap = 60.0 / max(1, args.per_minute)
    failed = 0
    for n, r in enumerate(todo, 1):
        v = by_id[r["vol_id"]]
        msg = build_message(pkg, box, v)
        if message_digest(msg) != r["message_sha256"]:
            raise Fail(f"{r['vol_id']}: the message differs from the dry run; run --dry-run again")
        rec = {"vol_id": r["vol_id"], "at": _now(), "shifts_hash": r["shifts_hash"]}
        try:
            graph.send(msg, decrypt_email(key, v["email_enc"]))
            rec["ok"] = True
            out(f"  [{n}/{len(todo)}] sent {r['vol_id']}")
        except Fail as exc:
            rec.update(ok=False, error=str(exc))
            failed += 1
            out(f"  [{n}/{len(todo)}] FAILED {r['vol_id']}: {exc}")
        _append(sent_path, rec)
        if n < len(todo):
            _sleep(gap)
    out(f"Done: {len(todo) - failed} sent, {failed} failed."
        + (" Run the same command again to retry the failures." if failed else ""))
    return 1 if failed else 0


def _graph_from_env(args, out) -> Graph:
    if not args.sender:
        raise Fail("set MAIL_SENDER, or pass --from: the address the schedule emails come from")
    missing = [k for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "VOL_MAIL_MAILBOX") if not os.getenv(k)]
    if missing:
        raise Fail(f"set {', '.join(missing)} (the mail relay's app registration and the account "
                   f"that has Send As on {args.sender})")
    return Graph(os.environ["GRAPH_TENANT_ID"], os.environ["GRAPH_CLIENT_ID"],
                 os.environ["VOL_MAIL_MAILBOX"], args.sender, out)


def main(argv=None) -> int:
    """Parse the command line and run `fetch`, `preview` or `send`.

    Args:
        argv: The arguments, without the program name. `None` reads `sys.argv`.

    Returns:
        The exit status.
    """
    p = argparse.ArgumentParser(prog="vol_mail.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download the volunteer box from the export API")
    f.add_argument("--url", required=True, help="the admin site, e.g. https://<admin hostname>")
    f.add_argument("--key-file", required=True, help="file holding the API key (bsc_…)")
    f.add_argument("--out", default="box.json")
    f.add_argument("--status", default="active", choices=("active", "all"))
    f.add_argument("--insecure-http", action="store_true", help=argparse.SUPPRESS)

    v = sub.add_parser("preview", help="write one volunteer's email as .eml/.html/.txt")
    v.add_argument("--package", required=True)
    v.add_argument("--box", required=True)
    v.add_argument("--vol", help="vol_id (default: the first in the box)")
    v.add_argument("--out", default="preview")

    s = sub.add_parser("send", help="send a round (--dry-run first)")
    s.add_argument("--package", required=True)
    s.add_argument("--box", required=True)
    s.add_argument("--key", required=True, help="the organizers' private key (PKCS#8 PEM)")
    s.add_argument("--round", required=True, help="a name per send, e.g. draft or final")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--only", help="file of vol_ids, one per line: send to these only")
    s.add_argument("--only-changed-since", metavar="ROUND",
                   help="only volunteers whose shifts differ from what ROUND sent them")
    s.add_argument("--state", default="vol_mail_state", help="where round plans and logs live")
    s.add_argument("--per-minute", type=int, default=20)
    s.add_argument("--from", dest="sender", default=SENDER,
                   help="the From address (default: MAIL_SENDER from the environment)")
    s.add_argument("--yes", action="store_true", help="don't ask before sending")

    args = p.parse_args(argv)
    try:
        return {"fetch": cmd_fetch, "preview": cmd_preview, "send": cmd_send}[args.cmd](args)
    except Fail as exc:
        print(f"vol_mail: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
