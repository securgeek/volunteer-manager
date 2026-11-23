"""Outbound mail — magic links only.

This is the one place the portal legitimately holds a plaintext address, and only for the
length of a request: the volunteer just typed it, we HMAC it to find their row, and we mail
the address they typed. Nothing readable is written to disk. Every other message to a
volunteer is an offline act by whoever holds the private key (see tools/vol_mail.py).

MAIL_BACKEND=file writes .eml files instead of connecting anywhere, which is what makes the
whole flow exercisable with no mail server — and what the test harness reads links out of.

MAIL_BACKEND=relay hands {to, subject, body} to the mailrelay container, which alone holds the
Microsoft Graph credentials and always sends as its MAIL_SENDER. The relay takes
plain text only, so the HTML alternative is dropped on that path. See mailrelay/CLAUDE.md.
"""
import json
import os
import re
import smtplib
import urllib.error
import urllib.request
import uuid
from email.message import EmailMessage
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))


class MailError(RuntimeError):
    """A send failed. The message carries the relay's error, never the recipient."""


def _relay_send(s, to_addr: str, subject: str, text: str) -> str:
    req = urllib.request.Request(
        f"{s.relay_url}/send", method="POST",
        data=json.dumps({"to": to_addr, "subject": subject, "body": text}).encode(),
        headers={"Authorization": f"Bearer {s.relay_token}",
                 "Content-Type": "application/json"})
    try:
        # nosec B310 — relay_url is refused at startup unless http(s) (config.py)
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            return json.loads(resp.read())["id"]
    except urllib.error.HTTPError as exc:
        try:
            reason = json.loads(exc.read()).get("error")
        except ValueError:
            reason = None
        raise MailError(f"mail relay returned {exc.code}: {reason}") from None
    except urllib.error.URLError as exc:
        raise MailError(f"mail relay unreachable at {s.relay_url}: {exc.reason}") from None


def _template(name: str) -> str:
    with open(os.path.join(_HERE, "templates", name), encoding="utf-8") as f:
        return f.read()


def render(name: str, **vars) -> str:
    """Substitute {{ key }} placeholders. Deliberately not a template engine.

    Args:
        name: The template file in `templates/`.
        **vars: Values for its placeholders.

    Returns:
        The filled-in text.

    Raises:
        KeyError: When a placeholder is left unfilled.
    """
    out = _template(name)
    for key, val in vars.items():
        out = out.replace("{{ " + key + " }}", str(val))
    leftover = re.findall(r"\{\{\s*(\w+)\s*\}\}", out)
    if leftover:
        raise KeyError(f"unfilled template variables in {name}: {sorted(set(leftover))}")
    return out


class Mailer:
    """Sends the portal's mail through the configured backend.

    `MAIL_BACKEND` picks it: `relay` hands the message to the mail relay, `file` writes `.eml`
    files to `MAIL_DIR` (for development and tests), and anything else sends over SMTP with
    STARTTLS.

    Args:
        settings: The portal's `Settings`.
    """
    def __init__(self, settings):
        self.s = settings

    def send(self, to_addr: str, subject: str, text: str,
             html: Optional[str] = None) -> str:
        """Send one message. Returns an opaque id for logging — never the address.

        Args:
            to_addr: The recipient.
            subject: The subject line.
            text: The plain-text body.
            html: An HTML alternative. The relay backend sends text only.
        """
        if self.s.mail_backend == "relay":
            return _relay_send(self.s, to_addr, subject, text)

        msg = EmailMessage()
        msg["From"] = self.s.smtp_from
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg["Message-ID"] = f"<{uuid.uuid4()}@bsides-portal>"
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")

        if self.s.mail_backend == "file":
            os.makedirs(self.s.mail_dir, exist_ok=True)
            name = f"{uuid.uuid4().hex}.eml"
            with open(os.path.join(self.s.mail_dir, name), "wb") as f:
                f.write(bytes(msg))
            return name

        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            if self.s.smtp_user:
                smtp.login(self.s.smtp_user, self.s.smtp_pass)
            smtp.send_message(msg)
        return msg["Message-ID"]

    def send_login_link(self, to_addr: str, token: str, purpose: str,
                        app_title: str = "Volunteer Portal") -> str:
        """Mail a volunteer their sign-in or finish-signup link.

        Args:
            to_addr: The volunteer's address.
            token: The single-use login token for the link.
            purpose: `signup` for a new address; anything else is a sign-in.
            app_title: The portal's title, for the subject and body.

        Returns:
            The message id from `send`.
        """
        verb = "finish signing up" if purpose == "signup" else "sign in"
        # The frontend's confirm page. GET /api/portal/verify still renders a fallback.
        link = f"{self.s.base_url}/apply/verify?t={token}"
        text = render("verify_email.txt", app_title=app_title, verb=verb, link=link,
                      ttl=self.s.token_ttl_min)
        html = render("verify_email.html", app_title=app_title, verb=verb, link=link,
                      ttl=self.s.token_ttl_min)
        return self.send(to_addr, f"{app_title} — {verb}", text, html)


    # Admin mail, asked for by the scheduler over the signed sync API (routers/sync.py
    # send_admin_mail). Fixed templates; the caller has already checked the link.
    ADMIN_KINDS = {
        "invite": ("admin_invite.txt", "You're invited to help run {app_title}",
                   ("inviter", "role", "app_title", "hours")),
        "reset":  ("admin_reset.txt", "Reset your {app_title} password",
                   ("username", "app_title", "minutes")),
        # Values already checked and defanged by routers/sync.py _alert_values.
        "alert":  ("admin_alert.txt", "[{app_title}] Security alert: {title}",
                   ("app_title", "severity", "title", "facts", "held_back")),
        # The early-access signup invite isn't here: its template is the organizers'
        # (content.py, Volunteer Portal → Invite email), rendered in routers/sync.py.
    }

    def send_admin(self, kind: str, to_addr: str, link: str, fields: dict) -> str:
        """Mail one of the fixed admin messages in `ADMIN_KINDS`.

        Args:
            kind: `invite` or `reset`.
            to_addr: The recipient.
            link: The link the scheduler made. The caller has already checked it.
            fields: Values for that kind's template. Missing ones are blank.

        Returns:
            The message id from `send`.

        Raises:
            KeyError: For an unknown `kind`.
        """
        template, subject, keys = self.ADMIN_KINDS[kind]
        values = {k: str(fields.get(k, "")) for k in keys}
        text = render(template, link=link, **values)
        return self.send(to_addr, subject.format(**values), text)


if __name__ == "__main__":
    # A one-message check of the configured backend, without the web flow:
    #     python mailer.py --test you@example.com
    import argparse
    import config

    ap = argparse.ArgumentParser(description="Send one test message with the current mail settings.")
    ap.add_argument("--test", metavar="ADDRESS", required=True)
    args = ap.parse_args()
    s = config.load(require_secrets=False)
    try:
        mid = Mailer(s).send(args.test, "Volunteer Portal mail test",
                             f"Test message from the volunteer portal ({s.mail_backend} backend).")
    except MailError as exc:
        raise SystemExit(f"send failed: {exc}") from exc
    print(f"sent via {s.mail_backend}: {mid}")
