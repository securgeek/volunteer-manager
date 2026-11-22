"""Volunteer signup: email first, questionnaire, two timeslots that don't overlap, submit.

routers/signup.py, in-process like test_portal_local.py, whose fixtures and helpers this
reuses: a temp-file SQLite database, MAIL_BACKEND=file, and signed sync pushes standing in for
the scheduler.

    pytest portal/test_signup.py -v
"""
import json

import pytest

from test_portal_local import (  # noqa: F401  (fixtures are used by name)
    _add_volunteer, _conn, _link_from_mail, _signed, app_env, client, rsa_keys,
)

GOOD_ANSWERS = {
    "experience": "other_con", "other_con_name": "BSides Delaware",
    "tshirt": "L", "flexible": True, "ack_arrive_early": True, "ack_full_slot": True,
    "dinner": "unsure", "dietary": "vegetarian",
}

# The 2027 shape. 1130 overlaps both neighbours on each day; Sunday ends at 1400.
TIMES = [("0800", "0800-1200"), ("1130", "1130-1530"), ("1500", "1500-1900"),
         ("1400", "1400-1800")]
REMAINING = {("sat", "0800"): 5, ("sat", "1130"): 3, ("sat", "1500"): 1,
             ("sun", "0800"): 0, ("sun", "1130"): 2, ("sun", "1400"): 4}


def _push_schedule(client, remaining=None, op="sched"):
    remaining = REMAINING if remaining is None else remaining
    payload = {
        "op_id": op, "content_hash": op,
        "days": [{"day_key": "sat", "day_str": "Saturday", "event_date": "2027-04-24",
                  "sort_order": 0},
                 {"day_key": "sun", "day_str": "Sunday", "event_date": "2027-04-25",
                  "sort_order": 1}],
        "times": [{"time_key": k, "time_str": s, "sort_order": i}
                  for i, (k, s) in enumerate(TIMES)],
        "day_times": [{"day_key": d, "time_key": t, "remaining": n}
                      for (d, t), n in remaining.items()],
        "positions": [], "slots": [],
    }
    r = _signed(client, "POST", "/api/sync/schedule", payload)
    assert r.status_code == 200, r.text


def _settings(client, op, **kv):
    r = _signed(client, "POST", "/api/sync/settings", {"op_id": op, "settings": kv})
    assert r.status_code == 200, r.text


@pytest.fixture
def open_signup(client, rsa_keys):
    _settings(client, "open", portal_signup_open="1", email_pubkey=rsa_keys[1])
    _push_schedule(client)
    return client


def _verified(client, app_env, email="new.person@example.com"):
    """check_email, then click the mailed link: a signup session in `client`'s cookies."""
    r = client.post("/api/portal/signup/check_email", data={"email": email})
    assert r.status_code == 200 and r.json()["sent"] is True, r.text
    token = _link_from_mail(app_env)
    r = client.post("/api/portal/verify", data={"t": token})
    assert r.status_code == 200 and r.json()["purpose"] == "signup", r.text
    return client


def _submit(client, **over):
    data = {"first_name": "Ada", "last_name": "Lovelace", "handle": "",
            "answers": GOOD_ANSWERS, "prefs": ["sat_0800", "sat_1500"],
            "extras": ["sun_1130"], "add_any": False}
    data.update(over)
    return client.post("/api/portal/signup/submit", data={"signup": json.dumps(data)})


def _remaining(app_env):
    conn = _conn(app_env)
    out = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT day_key, time_key, remaining FROM snapshot_day_times").fetchall()}
    conn.close()
    return out


# ── state ─────────────────────────────────────────────────────────────────────

def test_closed_until_opened_with_a_key_and_a_workable_pair(client, rsa_keys):
    def state():
        return client.get("/api/portal/signup/status").json()["state"]
    assert state() == "closed"
    _settings(client, "o1", portal_signup_open="1")
    assert state() == "closed"                       # no public key yet
    _settings(client, "o2", email_pubkey=rsa_keys[1])
    assert state() == "full"                         # no timeslots
    _push_schedule(client, {("sat", "0800"): 3}, op="one")
    assert state() == "full"                         # one timeslot can't make two picks
    _push_schedule(client, {("sat", "0800"): 3, ("sat", "1130"): 3}, op="overlapping")
    assert state() == "full"                         # two, but nobody can work both
    _push_schedule(client, op="all")
    assert state() == "open"
    _settings(client, "o3", portal_signup_open="0")
    assert state() == "closed"


def test_fields_are_served_from_the_one_definition(client):
    import signup_fields
    assert client.get("/api/portal/signup/fields").json() == {"fields": signup_fields.FIELDS}


def test_every_question_is_on_a_step_and_the_shifts_ones_come_first():
    import signup_fields
    sections = [f["section"] for f in signup_fields.FIELDS]
    assert set(sections) <= {"shifts", "additional"}
    assert sections == sorted(sections, key=["shifts", "additional"].index)
    assert [f["key"] for f in signup_fields.FIELDS] == [
        "experience", "flexible", "ack_arrive_early", "ack_full_slot",
        "tshirt", "dinner", "dietary"]


def test_timeslots_come_by_day_with_full_flags_overlaps_and_nobody_in_them(open_signup):
    out = open_signup.get("/api/portal/timeslots").json()["timeslots"]
    assert [t["key"] for t in out] == ["sat_0800", "sat_1130", "sat_1500",
                                      "sun_0800", "sun_1130", "sun_1400"]
    by_key = {t["key"]: t for t in out}
    assert by_key["sat_0800"]["label"] == "Saturday 0800-1200"
    assert by_key["sun_0800"]["full"] is True and by_key["sun_0800"]["remaining"] == 0
    assert sorted(by_key["sat_1130"]["overlaps"]) == ["sat_0800", "sat_1500"]
    assert by_key["sat_0800"]["overlaps"] == ["sat_1130"]
    assert sorted(by_key["sun_1400"]["overlaps"]) == ["sun_1130"]
    assert set(out[0]) == {"key", "day_key", "time_key", "day_str", "time_str", "event_date",
                           "label", "remaining", "full", "overlaps"}


def test_a_snapshot_without_remaining_reads_as_full(client, rsa_keys):
    """An older scheduler that sends no `remaining` must close signup, not open it."""
    _settings(client, "o", portal_signup_open="1", email_pubkey=rsa_keys[1])
    payload = {"op_id": "old", "content_hash": "old",
               "days": [{"day_key": "sat", "day_str": "Saturday", "sort_order": 0}], "times": [],
               "day_times": [{"day_key": "sat", "time_key": "0800"},
                             {"day_key": "sat", "time_key": "1500"}]}
    assert _signed(client, "POST", "/api/sync/schedule", payload).status_code == 200
    assert client.get("/api/portal/signup/status").json()["state"] == "full"


# ── check_email ───────────────────────────────────────────────────────────────

def test_check_email_refuses_when_closed(client):
    r = client.post("/api/portal/signup/check_email", data={"email": "a@example.com"})
    assert r.status_code == 409


def test_check_email_rejects_a_non_address(open_signup):
    r = open_signup.post("/api/portal/signup/check_email", data={"email": "not an address"})
    assert r.status_code == 400


def test_a_registered_address_stops_at_check_email(app_env, open_signup):
    _add_volunteer(app_env, email="jim@example.com")
    r = open_signup.post("/api/portal/signup/check_email", data={"email": " JIM@example.com "})
    assert r.status_code == 200 and r.json() == {
        "registered": True, "sent": False,
        "detail": "That address is already signed up. Sign in with it instead."}
    assert not list((app_env[1] / "mail").glob("*.eml"))  # no mail went out


def test_the_signup_link_gives_a_signup_session_and_nothing_more(app_env, open_signup):
    c = _verified(open_signup, app_env)
    assert c.get("/api/portal/signup/session").json() == {"signup_session": True,
                                                         "volunteer": False}
    assert c.get("/api/portal/me").status_code == 403


# ── check_name ────────────────────────────────────────────────────────────────

def test_check_name_needs_a_signup_session(open_signup):
    r = open_signup.post("/api/portal/signup/check_name",
                         data={"first_name": "Jim", "last_name": "Elliott"})
    assert r.status_code == 401


def test_check_name_says_only_whether_someone_similar_exists(app_env, open_signup):
    _add_volunteer(app_env, vol_id="1042", email="jim@example.com")      # name "Jim E."
    c = _verified(open_signup, app_env)

    def match(**kw):
        r = c.post("/api/portal/signup/check_name", data=kw)
        assert r.status_code == 200 and set(r.json()) == {"possible_match"}
        return r.json()["possible_match"]

    assert match(first_name="jim", last_name="Evans") is True     # first + last initial
    assert match(first_name="Jim", last_name="Smith") is False
    assert match(first_name="Ada", last_name="Lovelace") is False


# ── submit ────────────────────────────────────────────────────────────────────

def test_submit_creates_a_pending_volunteer_and_upgrades_the_session(app_env, open_signup,
                                                                      rsa_keys):
    import security
    c = _verified(open_signup, app_env)
    r = _submit(c, handle="countess")
    assert r.status_code == 200, r.text
    vol_id = r.json()["vol_id"]
    assert vol_id.startswith("p-") and r.json()["status"] == "pending"

    conn = _conn(app_env)
    v = conn.execute("SELECT * FROM volunteers WHERE vol_id = ?", (vol_id,)).fetchone()
    picks = conn.execute("SELECT day_key, time_key, kind, rank FROM vol_picks WHERE vol_id = ? "
                         "ORDER BY kind DESC, rank", (vol_id,)).fetchall()
    conn.close()

    assert v["name"] == "Ada L. (countess)" and v["status"] == "pending"
    assert v["time_pref"] == "Saturday 0800-1200;Saturday 1500-1900"
    assert v["add_time"] == "Sunday 1130-1530"
    assert json.loads(v["answers"]) == GOOD_ANSWERS
    assert v["possible_duplicate_of"] is None
    assert security.decrypt_email(rsa_keys[0], v["email_enc"]) == "new.person@example.com"
    assert [tuple(p) for p in picks] == [("sat", "0800", "pref", 1), ("sat", "1500", "pref", 2),
                                         ("sun", "1130", "add", None)]
    left = _remaining(app_env)
    assert left[("sat", "0800")] == 4 and left[("sat", "1500")] == 0   # decremented locally
    assert left[("sun", "1130")] == 2                                  # extras don't consume

    me = c.get("/api/portal/me")
    assert me.status_code == 200 and me.json()["volunteer"]["status"] == "pending"
    assert [p["label"] for p in me.json()["picks"]] == [
        "Saturday 0800-1200", "Saturday 1500-1900", "Sunday 1130-1530"]
    assert c.get("/api/portal/signup/session").json()["volunteer"] is True


def test_the_scheduler_pulls_the_signup_with_its_picks(app_env, open_signup):
    c = _verified(open_signup, app_env)
    vol_id = _submit(c).json()["vol_id"]
    pulled = _signed(open_signup, "GET", "/api/sync/volunteers?since=0").json()["volunteers"]
    rec = next(v for v in pulled if v["vol_id"] == vol_id)
    assert rec["status"] == "pending"
    assert {(p["day_key"], p["time_key"], p["kind"]) for p in rec["picks"]} == {
        ("sat", "0800", "pref"), ("sat", "1500", "pref"), ("sun", "1130", "add")}


def test_a_name_match_is_recorded_on_the_server_not_taken_from_the_browser(app_env,
                                                                         open_signup):
    _add_volunteer(app_env, vol_id="1042", email="jim@example.com")
    c = _verified(open_signup, app_env)
    vol_id = _submit(c, first_name="Jim", last_name="Evans",
                     possible_duplicate_of=["9999"]).json()["vol_id"]
    conn = _conn(app_env)
    dupes = conn.execute("SELECT possible_duplicate_of FROM volunteers WHERE vol_id = ?",
                         (vol_id,)).fetchone()[0]
    conn.close()
    assert json.loads(dupes) == ["1042"]


@pytest.mark.parametrize("over, status, text", [
    ({"prefs": ["sat_0800"]}, 400, "exactly two"),
    ({"prefs": ["sat_0800", "sat_0800"]}, 400, "exactly two"),
    ({"prefs": ["sat_0800", "sat_1130"]}, 400, "overlap"),        # 0800-1200 vs 1130-1530
    ({"prefs": ["sun_1130", "sun_1400"]}, 400, "overlap"),        # 1130-1530 vs 1400-1800
    ({"prefs": ["sat_0800", "sun_0800"]}, 409, "filled up"),      # sun 0800 is full
    ({"prefs": ["sat_0800", "sun_2200"]}, 409, "filled up"),      # not on offer
    ({"extras": ["nope_0800"]}, 409, "extra availability"),
    ({"first_name": "  "}, 400, "First name is required"),
    ({"last_name": "x" * 61}, 400, "at most 60"),
    ({"answers": {**GOOD_ANSWERS, "ack_full_slot": False}}, 400, "must be checked"),
    ({"answers": {**GOOD_ANSWERS, "other_con_name": ""}}, 400, "Which con? is required"),
    ({"answers": {**GOOD_ANSWERS, "tshirt": "5XL"}}, 400, "not one of the choices"),
    ({"answers": {k: v for k, v in GOOD_ANSWERS.items() if k != "dinner"}}, 400,
     "please choose one"),
])
def test_submit_refusals(app_env, open_signup, over, status, text):
    c = _verified(open_signup, app_env)
    r = _submit(c, **over)
    assert r.status_code == status, r.text
    assert text in json.dumps(r.json()["detail"])
    assert c.get("/api/portal/signup/session").json()["signup_session"] is True  # still unspent


def test_two_picks_on_different_days_never_clash(app_env, open_signup):
    """sat 1130 and sun 1130 share a time_key; only the same day can overlap."""
    c = _verified(open_signup, app_env)
    assert _submit(c, prefs=["sat_1130", "sun_1130"], extras=[]).status_code == 200


def test_extras_may_overlap_the_picks(app_env, open_signup):
    """Extras are 'also available if needed', not a commitment; overlap is the organizer's call."""
    c = _verified(open_signup, app_env)
    assert _submit(c, extras=["sat_1130"]).status_code == 200


def test_a_timeslot_that_filled_meanwhile_is_named_so_the_form_can_reopen_it(app_env, open_signup):
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app, base_url="https://portal.test") as second:
        first = _verified(open_signup, app_env, "first@example.com")
        _verified(second, app_env, "second@example.com")   # both mid-form
        assert _submit(first).status_code == 200            # takes sat 1500's last space
        r = _submit(second)
    assert r.status_code == 409 and r.json()["detail"]["full"] == ["sat_1500"]


def test_unknown_answer_keys_are_dropped_and_flexible_defaults_false(app_env, open_signup):
    c = _verified(open_signup, app_env)
    answers = {k: v for k, v in GOOD_ANSWERS.items() if k != "flexible"}
    vol_id = _submit(c, answers={**answers, "is_admin": True}).json()["vol_id"]
    conn = _conn(app_env)
    stored = json.loads(conn.execute("SELECT answers FROM volunteers WHERE vol_id = ?",
                                     (vol_id,)).fetchone()[0])
    conn.close()
    assert "is_admin" not in stored
    assert stored["flexible"] is False      # unticked is an answer, not a missing one


def test_first_time_needs_no_con_name_and_dietary_is_optional(app_env, open_signup):
    c = _verified(open_signup, app_env)
    answers = {"experience": "first_time", "tshirt": "4XL", "ack_arrive_early": True,
               "ack_full_slot": True, "dinner": "no"}
    assert _submit(c, answers=answers).status_code == 200


def test_one_signup_per_session_and_per_address(app_env, open_signup):
    c = _verified(open_signup, app_env)
    assert _submit(c).status_code == 200
    again = _submit(c)
    assert again.status_code == 409 and "already signed up" in again.json()["detail"]

    # A second link for the same address, asked for after the first signup went through.
    r = open_signup.post("/api/portal/signup/check_email",
                         data={"email": "new.person@example.com"})
    assert r.json()["registered"] is True


def test_two_links_for_one_address_cannot_both_sign_up(app_env, open_signup):
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app, base_url="https://portal.test") as a, \
         TestClient(main.app, base_url="https://portal.test") as b:
        _verified(a, app_env, "twice@example.com")
        _verified(b, app_env, "twice@example.com")   # both links issued before either submit
        assert _submit(a, prefs=["sat_0800", "sun_1400"]).status_code == 200
        r = _submit(b, prefs=["sat_0800", "sun_1400"])
    assert r.status_code == 409 and "already signed up" in r.json()["detail"]


def test_submit_refuses_when_signup_closed_meanwhile(app_env, open_signup):
    c = _verified(open_signup, app_env)
    _settings(open_signup, "close", portal_signup_open="0")
    assert _submit(c).status_code == 409


def test_signup_audit_carries_no_address(app_env, open_signup):
    c = _verified(open_signup, app_env)
    _submit(c)
    conn = _conn(app_env)
    blob = " ".join(f"{r['action']} {r['detail']}" for r in
                    conn.execute("SELECT action, detail FROM portal_audit").fetchall())
    conn.close()
    assert "signup.submit" in blob and "@" not in blob


def test_an_approval_push_keeps_the_portals_name_parts_and_follows_the_rekey(app_env, open_signup):
    """The scheduler doesn't hold first_name / last_name / nickname and leaves them out of its
    push; an upsert that blanked omitted columns would erase what the volunteer typed."""
    c = _verified(open_signup, app_env)
    vol_id = _submit(c, handle="countess").json()["vol_id"]
    r = _signed(open_signup, "POST", "/api/sync/volunteers", {
        "op_id": "approve-1",
        "rekeys": [{"vol_id": vol_id, "new_vol_id": "2001"}],
        "volunteers": [{"vol_id": "2001", "name": "Ada L. (countess)", "status": "active",
                        "picks": [{"day_key": "sat", "time_key": "0800", "kind": "pref", "rank": 1},
                                  {"day_key": "sat", "time_key": "1500", "kind": "pref", "rank": 2}]}],
    })
    assert r.status_code == 200 and r.json()["renamed"] == 1, r.text
    me = c.get("/api/portal/me").json()
    assert (me["volunteer"]["vol_id"], me["volunteer"]["status"]) == ("2001", "active")
    assert (me["volunteer"]["first_name"], me["volunteer"]["last_name"],
            me["volunteer"]["nickname"]) == ("Ada", "Lovelace", "countess")
    assert [p["time_key"] for p in me["picks"]] == ["0800", "1500"]


# ── migration ─────────────────────────────────────────────────────────────────

# ── changing a registration (POST /api/portal/me/update) ──────────────────────

def _signed_up(app_env, open_signup, **over):
    c = _verified(open_signup, app_env)
    r = _submit(c, **over)
    assert r.status_code == 200, r.text
    return c, r.json()["vol_id"]


def _update(c, **over):
    data = {"prefs": ["sat_0800", "sat_1500"], "extras": [], "add_any": False, "answers": {}}
    data.update(over)
    return c.post("/api/portal/me/update", data={"update": json.dumps(data)})


def test_me_shows_answers_scheduled_shifts_and_whether_editable(app_env, open_signup):
    c, vol_id = _signed_up(app_env, open_signup)
    _push_schedule(open_signup, op="with-slot")
    _signed(open_signup, "POST", "/api/sync/schedule", {
        "op_id": "assign", "content_hash": "assign",
        "days": [{"day_key": "sat", "day_str": "Saturday", "sort_order": 0}],
        "times": [{"time_key": "0800", "time_str": "0800-1200", "sort_order": 0}],
        "day_times": [{"day_key": "sat", "time_key": "0800", "remaining": 4}],
        "positions": [{"pos_key": "sec1", "pos_cat": "Security", "pos_str": "Security 1"}],
        "slots": [{"day_key": "sat", "time_key": "0800", "pos_key": "sec1", "vol_id": vol_id}]})
    me = c.get("/api/portal/me").json()
    assert me["editable"] is True and me["answers"]["tshirt"] == "L"
    assert me["scheduled"] == [{"key": "sat_0800", "day_key": "sat", "time_key": "0800",
                                "label": "Saturday 0800-1200", "position": "Security 1"}]


def test_a_volunteer_can_move_a_shift_and_change_editable_answers(app_env, open_signup):
    c, vol_id = _signed_up(app_env, open_signup)
    before = _remaining(app_env)
    r = _update(c, prefs=["sat_0800", "sun_1400"], extras=["sat_1130"],
                answers={"tshirt": "XL", "dinner": "yes", "flexible": False})
    assert r.status_code == 200, r.text
    me = r.json()
    assert [p["label"] for p in me["picks"]] == ["Saturday 0800-1200", "Sunday 1400-1800",
                                                 "Saturday 1130-1530"]
    assert (me["answers"]["tshirt"], me["answers"]["dinner"], me["answers"]["flexible"]) == \
        ("XL", "yes", False)
    after = _remaining(app_env)
    assert after[("sat", "1500")] == before[("sat", "1500")] + 1    # dropped: a place back
    assert after[("sun", "1400")] == before[("sun", "1400")] - 1    # new: a place taken
    assert after[("sat", "0800")] == before[("sat", "0800")]        # kept: unchanged

    conn = _conn(app_env)
    v = conn.execute("SELECT time_pref, add_time, rev FROM volunteers WHERE vol_id = ?",
                     (vol_id,)).fetchone()
    conn.close()
    assert v["time_pref"] == "Saturday 0800-1200;Sunday 1400-1800"
    assert v["add_time"] == "Saturday 1130-1530"
    pulled = _signed(open_signup, "GET", "/api/sync/volunteers?since=0").json()["volunteers"]
    assert {(p["day_key"], p["time_key"]) for p in
            next(x for x in pulled if x["vol_id"] == vol_id)["picks"]} == {
        ("sat", "0800"), ("sun", "1400"), ("sat", "1130")}       # the scheduler will see it


def test_only_editable_answers_change(app_env, open_signup):
    c, _ = _signed_up(app_env, open_signup)
    r = _update(c, answers={"experience": "first_time", "dietary": "none", "tshirt": "S"})
    assert r.status_code == 200
    a = r.json()["answers"]
    assert a["experience"] == "other_con" and a["dietary"] == "vegetarian"   # untouched
    assert a["tshirt"] == "S"


def test_a_held_shift_can_be_kept_even_when_full(app_env, open_signup):
    c, _ = _signed_up(app_env, open_signup)           # took sat 1500's last place
    assert _remaining(app_env)[("sat", "1500")] == 0
    assert _update(c, answers={"dinner": "no"}).status_code == 200


@pytest.mark.parametrize("over, status, text", [
    ({"prefs": ["sat_0800"]}, 400, "exactly two"),                 # dropped to one
    ({"prefs": ["sat_0800", "sat_1130"]}, 400, "overlap"),
    ({"prefs": ["sat_0800", "sun_0800"]}, 409, "filled up"),       # new pick, full
    ({"prefs": ["sat_0800", "sun_2200"]}, 409, "filled up"),       # not on offer
    ({"answers": {"tshirt": "9XL"}}, 400, "not one of the choices"),
    ({"answers": {"dinner": ""}}, 400, "please choose one"),
])
def test_update_refusals_leave_everything_as_it_was(app_env, open_signup, over, status, text):
    c, _ = _signed_up(app_env, open_signup)
    before = (c.get("/api/portal/me").json(), _remaining(app_env))
    r = _update(c, **over)
    assert r.status_code == status, r.text
    assert text in json.dumps(r.json()["detail"])
    assert (c.get("/api/portal/me").json(), _remaining(app_env)) == before


def test_editing_works_with_signup_closed(app_env, open_signup):
    c, _ = _signed_up(app_env, open_signup)
    _settings(open_signup, "close", portal_signup_open="0")
    assert _update(c, prefs=["sat_0800", "sun_1400"]).status_code == 200


def _make_inactive(client, vol_id, when="2026-10-25T12:00:00Z", op="inactive"):
    r = _signed(client, "POST", "/api/sync/volunteers", {
        "op_id": op, "volunteers": [{"vol_id": vol_id, "name": "Ada L.", "inactive_at": when}]})
    assert r.status_code == 200, r.text


def test_an_inactive_volunteer_cannot_edit(app_env, open_signup):
    c, vol_id = _signed_up(app_env, open_signup)
    _make_inactive(open_signup, vol_id)
    assert c.get("/api/portal/me").json()["editable"] is False
    assert _update(c).status_code == 403


def test_update_needs_a_full_session(app_env, open_signup):
    signup_only = _verified(open_signup, app_env)
    assert _update(signup_only).status_code == 403



# ── cancelling and rejoining ──────────────────────────────────────────────────

def _cancel(c):
    return c.post("/api/portal/me/cancel")


def _rejoin(c, **over):
    data = {"prefs": ["sat_0800", "sat_1500"], "extras": [], "add_any": False, "answers": {}}
    data.update(over)
    return c.post("/api/portal/me/rejoin", data={"update": json.dumps(data)})


def _fields(app_env, vol_id):
    conn = _conn(app_env)
    row = conn.execute("SELECT status, cancelled_at IS NOT NULL, inactive_at IS NOT NULL "
                       "FROM volunteers WHERE vol_id = ?", (vol_id,)).fetchone()
    conn.close()
    return tuple(row)


def _approve(client, vol_id, op="approve"):
    _signed(client, "POST", "/api/sync/volunteers", {
        "op_id": op, "volunteers": [{"vol_id": vol_id, "name": "Ada L.", "status": "active"}]})


def test_cancelling_frees_the_places_keeps_approval_and_locks_editing(app_env, open_signup):
    c, vol_id = _signed_up(app_env, open_signup)
    _approve(open_signup, vol_id)
    before = _remaining(app_env)
    r = _cancel(c)
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["volunteer"]["status"] == "active"        # still oriented
    assert me["cancelled"] is True and me["editable"] is False and me["can_rejoin"] is True
    assert _fields(app_env, vol_id) == ("active", 1, 0)
    after = _remaining(app_env)
    assert after[("sat", "0800")] == before[("sat", "0800")] + 1
    assert after[("sat", "1500")] == before[("sat", "1500")] + 1
    assert _update(c).status_code == 403
    assert _cancel(c).status_code == 409


def test_rejoin_keeps_approval_and_needs_space_again(app_env, open_signup):
    c, vol_id = _signed_up(app_env, open_signup)        # took sat 1500's last place
    _approve(open_signup, vol_id)
    _cancel(c)
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app, base_url="https://portal.test") as other:
        _verified(other, app_env, "other@example.com")
        assert _submit(other).status_code == 200          # takes the place they gave back
    r = _rejoin(c)
    assert r.status_code == 409 and r.json()["detail"]["full"] == ["sat_1500"]
    assert _fields(app_env, vol_id) == ("active", 1, 0)

    r = _rejoin(c, prefs=["sat_0800", "sun_1400"], answers={"tshirt": "M"})
    assert r.status_code == 200, r.text
    assert r.json()["volunteer"]["status"] == "active" and r.json()["cancelled"] is False
    assert r.json()["answers"]["tshirt"] == "M"
    assert _fields(app_env, vol_id) == ("active", 0, 0)
    assert _rejoin(c).status_code == 409                  # not cancelled any more


def test_a_pending_volunteer_rejoins_still_pending(app_env, open_signup):
    c, vol_id = _signed_up(app_env, open_signup)
    _cancel(c)
    assert _rejoin(c).json()["volunteer"]["status"] == "pending"


def test_rejoin_works_with_signup_closed(app_env, open_signup):
    c, _ = _signed_up(app_env, open_signup)
    _cancel(c)
    _settings(open_signup, "close", portal_signup_open="0")
    assert _rejoin(c).status_code == 200


def test_an_inactive_volunteer_can_neither_cancel_nor_rejoin(app_env, open_signup):
    c, vol_id = _signed_up(app_env, open_signup)
    _cancel(c)
    _make_inactive(open_signup, vol_id)
    me = c.get("/api/portal/me").json()
    assert me["inactive"] is True and me["can_rejoin"] is False
    assert _rejoin(c).status_code == 409 and _cancel(c).status_code == 409


def test_pushes_can_neither_set_nor_clear_a_cancellation(app_env, open_signup):
    """cancelled_at is the volunteer's. An approval arriving after they cancelled applies —
    approval is separate — but leaves them cancelled; nor can a push cancel anyone."""
    c, vol_id = _signed_up(app_env, open_signup)
    _cancel(c)
    _signed(open_signup, "POST", "/api/sync/volunteers", {
        "op_id": "late-approve", "volunteers": [{"vol_id": vol_id, "name": "Ada L.",
                                                 "status": "active", "cancelled_at": None}]})
    assert _fields(app_env, vol_id) == ("active", 1, 0)
    _rejoin(c)
    _signed(open_signup, "POST", "/api/sync/volunteers", {
        "op_id": "stale", "volunteers": [{"vol_id": vol_id, "name": "Ada L.",
                                          "cancelled_at": "2026-10-25T00:00:00Z"}]})
    assert _fields(app_env, vol_id) == ("active", 0, 0)


def test_inactive_is_set_and_cleared_by_the_organizer_and_keeps_approval(app_env, open_signup):
    c, vol_id = _signed_up(app_env, open_signup)
    _approve(open_signup, vol_id)
    _make_inactive(open_signup, vol_id)
    assert _fields(app_env, vol_id) == ("active", 0, 1)
    _make_inactive(open_signup, vol_id, when=None, op="reinstate")
    assert _fields(app_env, vol_id) == ("active", 0, 0)
    assert c.get("/api/portal/me").json()["editable"] is True


def test_an_inactive_volunteer_gets_no_sign_in_link(app_env, open_signup):
    _add_volunteer(app_env, vol_id="1042", email="jim@example.com")
    _make_inactive(open_signup, "1042")
    r = open_signup.post("/api/portal/request_link", data={"email": "jim@example.com"})
    assert r.json()["sent"] is False and "isn't active" in r.json()["detail"]


# ── early-access invites ──────────────────────────────────────────────────────

def _push_invites(client, op, *invites):
    """Each invite as (invite_id, token, expires_at, revoked_at); only the hash is sent."""
    import hashlib
    r = _signed(client, "POST", "/api/sync/signup_invites", {
        "op_id": op, "content_hash": op,
        "invites": [{"invite_id": i, "token_hash": hashlib.sha256(t.encode()).hexdigest(),
                     "expires_at": exp, "revoked_at": rev} for i, t, exp, rev in invites]})
    assert r.status_code == 200, r.text


FUTURE, PAST = "2099-01-01T00:00:00Z", "2000-01-01T00:00:00Z"


@pytest.fixture
def closed_signup(client, rsa_keys):
    """Everything a signup needs except the switch: the call for volunteers is closed."""
    _settings(client, "closed", portal_signup_open="0", email_pubkey=rsa_keys[1])
    _push_schedule(client)
    _push_invites(client, "inv1", ("inv-a", "tok-a", FUTURE, None))
    return client


def _invited(client, app_env, email="early@example.com", token="tok-a"):
    r = client.post("/api/portal/signup/check_email", data={"email": email, "invite": token})
    assert r.status_code == 200 and r.json()["sent"] is True, r.text
    r = client.post("/api/portal/verify", data={"t": _link_from_mail(app_env)})
    assert r.status_code == 200, r.text
    return client


def test_a_closed_call_turns_away_anyone_without_an_invite(app_env, closed_signup):
    r = closed_signup.post("/api/portal/signup/check_email", data={"email": "x@example.com"})
    assert r.status_code == 409 and "aren't open" in r.json()["detail"]
    r = closed_signup.post("/api/portal/signup/check_email",
                           data={"email": "x@example.com", "invite": "not-a-real-token"})
    assert r.status_code == 409 and "invite link" in r.json()["detail"]


def test_an_invite_lets_one_person_sign_up_while_closed(app_env, closed_signup):
    c = closed_signup
    assert c.get("/api/portal/signup/invite", params={"t": "tok-a"}).json() == {"status": "valid"}
    _invited(c, app_env)
    r = _submit(c)
    assert r.status_code == 200, r.text
    vol_id = r.json()["vol_id"]
    conn = _conn(app_env)
    assert conn.execute("SELECT invite_id FROM volunteers WHERE vol_id = ?",
                        (vol_id,)).fetchone()[0] == "inv-a"
    assert conn.execute("SELECT used_at FROM signup_invites").fetchone()[0] is not None
    conn.close()
    assert c.get("/api/portal/signup/invite", params={"t": "tok-a"}).json() == {"status": "used"}
    # The scheduler learns who used it from the volunteers pull.
    pulled = _signed(c, "GET", "/api/sync/volunteers?since=0").json()["volunteers"]
    assert [v["invite_id"] for v in pulled if v["vol_id"] == vol_id] == ["inv-a"]


def test_an_invite_is_used_at_submit_so_only_one_signup_gets_it(app_env, closed_signup, client):
    from fastapi.testclient import TestClient
    import main
    first = _invited(closed_signup, app_env, email="one@example.com")
    with TestClient(main.app, base_url="https://portal.test") as other:
        # A second address on the same link gets as far as the form: nothing is used yet.
        _invited(other, app_env, email="two@example.com")
        assert _submit(first).status_code == 200
        r = _submit(other, first_name="Grace", last_name="Hopper")
        assert r.status_code == 409 and "invite link" in r.json()["detail"]


def test_expired_and_revoked_invites_do_nothing(app_env, closed_signup):
    c = closed_signup
    _push_invites(c, "inv2", ("inv-old", "tok-old", PAST, None),
                  ("inv-rev", "tok-rev", FUTURE, "2026-01-01T00:00:00Z"))
    for token, status in (("tok-old", "expired"), ("tok-rev", "revoked"), ("nope", "unknown")):
        assert c.get("/api/portal/signup/invite", params={"t": token}).json()["status"] == status
        r = c.post("/api/portal/signup/check_email", data={"email": "z@example.com",
                                                          "invite": token})
        assert r.status_code == 409


def test_revoking_between_email_and_submit_stops_the_signup(app_env, closed_signup):
    _invited(closed_signup, app_env)
    _push_invites(closed_signup, "inv3", ("inv-a", "tok-a", FUTURE, "2026-11-21T00:00:00Z"))
    r = _submit(closed_signup)
    assert r.status_code == 409 and "invite link" in r.json()["detail"]


def test_an_invite_gets_past_closed_but_not_full(app_env, closed_signup):
    _push_schedule(closed_signup, {("sat", "0800"): 0, ("sat", "1500"): 0}, op="empty")
    r = closed_signup.post("/api/portal/signup/check_email",
                           data={"email": "early@example.com", "invite": "tok-a"})
    assert r.status_code == 409 and "full" in r.json()["detail"]


def test_an_invite_push_replaces_the_list_but_keeps_uses(app_env, closed_signup):
    c = closed_signup
    _invited(c, app_env)
    assert _submit(c).status_code == 200
    _push_invites(c, "inv4", ("inv-a", "tok-a", FUTURE, None), ("inv-b", "tok-b", FUTURE, None))
    conn = _conn(app_env)
    rows = dict(conn.execute("SELECT invite_id, used_at FROM signup_invites").fetchall())
    assert rows["inv-a"] is not None and rows["inv-b"] is None     # the use survived the push
    conn.close()
    _push_invites(c, "inv5", ("inv-b", "tok-b", FUTURE, None))      # inv-a dropped
    assert c.get("/api/portal/signup/invite", params={"t": "tok-a"}).json()["status"] == "unknown"
    assert _signed(c, "GET", "/api/sync/heartbeat").json()["invites_hash"] == "inv5"


def test_an_invite_push_rejects_bad_rows(closed_signup):
    for bad in ({"invite_id": "x", "token_hash": "short", "expires_at": FUTURE},
                {"invite_id": "x", "token_hash": "a" * 64, "expires_at": "tomorrow"},
                {"invite_id": "x", "token_hash": "a" * 64}):
        r = _signed(closed_signup, "POST", "/api/sync/signup_invites",
                    {"op_id": f"bad-{len(str(bad))}", "invites": [bad]})
        assert r.status_code == 400
