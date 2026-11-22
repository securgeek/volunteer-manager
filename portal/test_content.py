"""The organizer-editable content: POST /api/sync/content, the landing page, the questionnaire
and the early-access invite. In-process like test_signup.py, whose helpers this reuses.

    pytest portal/test_content.py -v
"""
# The fixtures are used by name, and the helpers are the other test modules' own.
# pylint: disable=unused-import,import-private-name
import copy
import json

import pytest

import content
import signup_fields
from test_admin_mail import SITE, _invite, _mail
from test_portal_local import _conn, _signed, app_env, client, rsa_keys  # noqa: F401
from test_signup import GOOD_ANSWERS, _submit, _verified, open_signup  # noqa: F401


def _push(client, op, **parts):
    body = {"op_id": op, "content_hash": f"hash-{op}", "landing": None, "questionnaire": None,
            "invite": None, **parts}
    return _signed(client, "POST", "/api/sync/content", body)


def _stored(app_env):
    conn = _conn(app_env)
    row = conn.execute("SELECT landing, questionnaire, invite FROM portal_content").fetchone()
    conn.close()
    return tuple(row) if row else None


# ── the push ──────────────────────────────────────────────────────────────────

def test_the_heartbeat_asks_for_content_until_some_arrives(client):
    hb = _signed(client, "GET", "/api/sync/heartbeat").json()
    assert hb["content_hash"] == ""
    assert _push(client, "c1").status_code == 200
    assert _signed(client, "GET", "/api/sync/heartbeat").json()["content_hash"] == "hash-c1"


def test_a_push_must_be_signed(client):
    assert client.post("/api/sync/content", json={"op_id": "x"}).status_code == 401


@pytest.mark.parametrize("parts", [
    {"landing": dict(content.DEFAULT_LANDING, intro="{% for x in range(9) %}{% endfor %}")},
    {"landing": dict(content.DEFAULT_LANDING, main="{{ conference.__class__ }}")},
    {"landing": dict(content.DEFAULT_LANDING,
                     roles=[{"icon": "skull", "name": "X", "text": ""}])},
    {"questionnaire": [{"key": "Bad Key", "section": "shifts", "type": "text",
                        "required": False, "label": "x"}]},
    {"questionnaire": [{"key": "flexible", "section": "shifts", "type": "text",
                        "required": False, "label": "x"}]},
    {"questionnaire": [{"key": "a", "section": "shifts", "type": "ack", "required": False,
                        "label": "x"}]},
    {"invite": {"subject": "Hi", "body": "No link."}},
    {"invite": {"subject": "Line one\nline two", "body": "{{ link }}"}},
])
def test_a_push_that_fails_its_check_stores_nothing(app_env, client, parts):
    r = _push(client, "bad", **parts)
    assert r.status_code == 400, r.text
    assert _stored(app_env) is None


# ── the landing page ──────────────────────────────────────────────────────────

def test_the_landing_page_defaults_to_the_original_and_fills_in_the_branding(client):
    page = client.get("/api/portal/landing").json()
    assert [r["name"] for r in page["roles"]] == ["Room Monitors", "Registration", "Security",
                                                  "Floater"]
    # No contact set: the contact sentence is left out.
    assert "questions" not in page["sidebar"]
    _signed(client, "POST", "/api/sync/branding", {
        "op_id": "b", "content_hash": "b", "app_title": "ExampleCon",
        "contact_email": "volunteers@example.org", "code_of_conduct_url": "https://ex.org/coc"})
    page = client.get("/api/portal/landing").json()
    assert "ExampleCon is completely volunteer-run" in page["intro"]
    assert '<a href="https://ex.org/coc">Code of Conduct</a>' in page["sidebar"]
    assert '<a href="mailto:volunteers@example.org"><strong>volunteers@example.org</strong></a>' \
        in page["sidebar"]


def test_a_pushed_landing_page_is_rendered_safely(client):
    landing = dict(content.DEFAULT_LANDING,
                   intro="<script>alert(1)</script> [x](javascript:alert(1)) "
                         "![t](https://tracker.example/p.gif) [ok](https://example.org/)",
                   roles=[{"icon": "users", "name": "<b>Crew</b>", "text": "All hands"}])
    assert _push(client, "l1", landing=landing).status_code == 200
    page = client.get("/api/portal/landing").json()
    assert "<script>" not in page["intro"] and "&lt;script&gt;" in page["intro"]
    assert 'href="javascript' not in page["intro"] and "<img" not in page["intro"]
    assert '<a href="https://example.org/">ok</a>' in page["intro"]
    # Card text is data; the page escapes it.
    assert page["roles"] == [{"icon": "users", "name": "<b>Crew</b>", "text": "All hands"}]


# ── the questionnaire ─────────────────────────────────────────────────────────

def test_signup_asks_and_checks_the_pushed_questionnaire(app_env, open_signup):
    fields = [f for f in content.DEFAULT_FIELDS if f["key"] != "tshirt"] + [
        {"key": "pronouns", "section": "additional", "type": "text", "required": True,
         "editable": True, "label": "Pronouns", "max": 40}]
    assert _push(open_signup, "q1", questionnaire=fields).status_code == 200
    served = open_signup.get("/api/portal/signup/fields").json()["fields"]
    assert [f["key"] for f in served][-1] == "pronouns" and "tshirt" not in {f["key"] for f in served}
    c = _verified(open_signup, app_env)
    r = _submit(c)                                              # no pronouns yet
    assert r.status_code == 400 and "Pronouns" in r.json()["detail"]
    r = _submit(c, answers={**GOOD_ANSWERS, "pronouns": "they/them"})
    assert r.status_code == 200, r.text
    conn = _conn(app_env)
    stored = json.loads(conn.execute("SELECT answers FROM volunteers WHERE vol_id = ?",
                                     (r.json()["vol_id"],)).fetchone()[0])
    conn.close()
    # Only the questions asked: the T-shirt answer the old form would have kept is dropped.
    assert stored["pronouns"] == "they/them" and "tshirt" not in stored


def test_an_answer_to_a_removed_question_is_kept_on_edit():
    fields = [f for f in content.DEFAULT_FIELDS if f["key"] != "tshirt"]
    existing = {**GOOD_ANSWERS}
    out = signup_fields.validate_edit({"dinner": "yes"}, existing, fields)
    assert out["dinner"] == "yes" and out["tshirt"] == "L"


def test_the_default_questionnaire_is_the_original():
    assert signup_fields.FIELDS == content.DEFAULT_FIELDS
    assert content.validate_fields(copy.deepcopy(content.DEFAULT_FIELDS)) == \
        content.DEFAULT_FIELDS


# ── the invite email ──────────────────────────────────────────────────────────

def test_an_invite_uses_the_pushed_template(app_env, client):
    invite = {"subject": "Come help at {{ conference|upper }}",
              "body": "Hey {{ first_name }}!\n{{ link }}\n"
                      "{% if contact_email %}Ask {{ contact_email }}.{% else %}Reply.{% endif %}"}
    assert _push(client, "i1", invite=invite).status_code == 200
    r = _invite(client, "inv1", app_title="ExampleCon")
    assert r.status_code == 200, r.text
    (msg,) = _mail(app_env)
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert msg["Subject"] == "Come help at EXAMPLECON"
    assert "Hey Early!" in body and f"{SITE}/apply/signup?invite=TOKinv1" in body
    assert "Reply." in body
