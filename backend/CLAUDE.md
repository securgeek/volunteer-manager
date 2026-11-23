# Backend — FastAPI

## Module layout

```
backend/
├── main.py          ← App factory, CLI entry point, /api/health
├── db.py            ← Shared: dict_factory, get_db, get_session_data, RoleChecker
├── audit.py         ← The audit log: audit(), the EVENTS registry, client_ip
├── ratelimit.py     ← SlidingWindow: in-memory per-key limits (sign-in, password reset)
├── checkin_codes.py ← Shift check-in codes (HMAC) and the early/late rule; no DB
├── api_auth.py      ← Export API keys: TokenChecker(scope), IP allow-lists, failure limiter
├── names.py         ← split_name / export_name ("Handle [First L.]")
├── email_render.py  ← Schedule email: Markdown → HTML/text parts, fill(), QR, the package zip
├── routers/
│   ├── auth.py      ← POST /api/login, GET /api/logout, start_session()
│   ├── passkeys.py  ← WebAuthn passkeys: register, sign in, list, remove
│   ├── schedule.py  ← _build_schedule, all schedule and structure endpoints
│   ├── volunteers.py← _query_vols, all volunteer endpoints
│   ├── users.py     ← User CRUD (organizer-only); own account: me, change_password, change_email
│   ├── invites.py   ← Admin invites: a 72-hour link to make one's own account
│   ├── password_reset.py ← "Forgot password?" by email
│   ├── email_keys.py← RSA public key storage, encrypted email retrieval
│   ├── radios.py    ← Radio checkout/checkin by barcode
│   ├── checkin.py   ← record_checkin (shared), POST checkin/scan, checkin/rotate_key
│   ├── api_tokens.py← Admin → API Keys: create (shown once), list, revoke
│   ├── export.py    ← GET export/schedule, export/volunteers (bearer keys); volunteer_records
│   ├── email_template.py ← Schedule email template, images, preview, package download
│   └── setup.py     ← Setup wizard endpoints (status, create_admin, CSV imports)
├── scheduler.sql    ← Full current schema, for a fresh DB
├── migrations/      ← One file per release after 1.0 (SCHEMA_RELEASES in db.py)
└── (docs/api/export.md at the repo root: the export API guide, built from routers/export.py)
```

`db.py` has zero imports from app modules — all routers import from it, never the reverse.

**No backend route may start with the text `/api/portal`.** nginx (`location /api/portal`)
and the Vite dev proxy send that whole prefix to the portal service, so such a route 404s from
the browser while working fine when called directly. It's a plain string prefix:
`/api/portal_admin` is swallowed too. The organizer's portal endpoints are therefore
`/api/admin/portal/{status,sync_now,pending}` (`routers/portal_admin.py`).
`test_route_prefixes.py` enforces this.

## Session and auth

- `_session_cache` in `db.py` is a module-level dict keyed by `session_id`. All routers import the same object by reference, so mutations are immediately visible everywhere.
- **The audit log is `audit.py`.** `audit("event.code", request, s_id, message, who=None,
  **data)` appends one JSON object per line to `logs/audit.jsonl` (`--audit`, set through
  `audit.configure()` in `main.py`; unset, as in tests, it writes nothing). Fields: `ts` (UTC,
  `Z`), `event`, `severity`, `actor`, `user_id`, `ip`, `method`, `path`, `message`, `data`.
  - The **event code comes first and is a string literal from `audit.EVENTS`**, which gives
    each code a severity. `test_audit.py` scans every call and fails on an unregistered or
    non-literal code. A new kind of action needs a new registry entry.
  - Put caller-controlled values (usernames, labels, ids) in `**data`, not only in the
    message, so they can be filtered on. Never an email address, password or token.
  - Lines are `json.dumps(..., ensure_ascii=True)`, so no input can forge a line: newlines,
    U+2028/U+2029/U+0085 and bidi overrides all come out as escapes. That was the bug in
    the old free-text `audit.log`, which wrote a failed login's username verbatim.
  - **Each event is also a row in `audit_events`** (1.1 migration) when configured with
    `to_db=True`, which only `main.py` does. Written by `_store` on its **own pooled
    connection with its own commit** (`db.get_conn`/`put_conn`), so a request that rolls
    back still leaves its row. The file is written first and is the complete record; the
    table is pruned to `AUDIT_RETENTION_DAYS` (default 365, 0 = keep) by `audit.prune()`,
    which `main.lifespan` runs at startup and every 24 h. `source` is `backend`, or
    `portal` for portal events pulled in later. NUL can't go into Postgres TEXT/JSONB, so
    the row has U+FFFD where the file has `\u0000`.
  - **A NUL in any query parameter makes psycopg2 raise** (a 500). `/api/login` treats a
    username with one as unknown; other routes still need the planned request middleware.
  - A write failure is logged and swallowed; auditing never fails a request.
  - `client_ip()` lives here (not in `api_auth`, which re-exports it) so `audit.py` imports
    only `db` and nothing imports in a circle.
- `RoleChecker("organizer")` as a Depends raises 401 if unauthenticated, 403 if wrong role.
- **Failed password sign-ins are rate limited** (`routers/auth.py`, `LOGIN_LIMITS`, 15-minute
  sliding windows in memory, `ratelimit.SlidingWindow`): 5 per username from one address, 20
  per address, 30 per username from anywhere. Checked **before** the password, so a refused
  request (429 with `Retry-After`) never tests one, and it doesn't count. Only failures
  count, because every check-in laptop at the venue shares one public address; a success
  clears its username-and-address count. The per-username-only limit is deliberately high:
  set low, anyone who knows a username could lock its owner out from anywhere. A throttled
  client is audited once per key per window (`auth.login.throttled`), not per request.
  `reset_login_limits()` clears it all; `conftest.py` does so before every test, since all
  test clients share one address.
- **An unknown username is verified against a dummy Argon2 hash**, so it takes as long as a
  wrong password and response time doesn't reveal which usernames exist.
- `auth.start_session()` is the one place a session is created. The password login and the
  passkey login both call it, so the two produce identical sessions.

## Passkeys (`routers/passkeys.py`)

An **alternative** to the password, not a replacement: passwords keep working. Any role can
add a passkey to its own account; organizers can clear someone else's (lost device) from User
Management.

- **Relying party = `ADMIN_PUBLIC_URL`**, the organizer site's origin (falling back to
  `PORTAL_PUBLIC_URL` when one origin serves both). rp_id is its hostname. Passkeys are bound
  to that hostname for good: a new admin hostname orphans every one, though passwords still
  work. Unset, or plain http on anything but localhost, returns 503 and
  `GET /api/passkeys/config` reports `enabled: false`, which hides the login button. The
  public portal hostname serves no `/admin`, so passkeys never work there.
- **Adding a passkey takes the current password** (`register/options`). A stolen session
  cookie must not be enough to plant a permanent way back in.
- **Challenges are single-use.** `webauthn_challenges` stores SHA-256 hashes for 5 minutes;
  `_consume_challenge` burns the row in one conditional UPDATE *before* verifying, committed
  either way, so a response gets one attempt and a replay is a 400. Registration challenges
  are bound to the requesting user_id; sign-in ones have none.
- **Discoverable and user-verified, no attestation.** Sign-in needs no username, every use
  needs a PIN or biometric, and any make of authenticator is accepted (synced passkeys from
  iCloud, Google, 1Password, Bitwarden).
- **Algorithms**, in preference order: ML-DSA-65, ML-DSA-44, ML-DSA-87 (post-quantum;
  1Password creates these as of Oct 2026), then ES256, EdDSA, RS256. `public_key` is the raw
  COSE_Key and `cose_alg` is stored beside it, so adding an algorithm needs no migration.
  ML-DSA needs `cryptography>=49`, which `webauthn==3.0.1` requires anyway.
- **Sign count**: py_webauthn rejects a count that doesn't increase, unless both stored and
  received are 0 (synced passkeys always report 0).
- `users.webauthn_user_id` is the WebAuthn user handle: 32 random bytes made on first
  registration. Not the user_id or username. A sign-in whose userHandle doesn't match it is
  refused.
- A removed passkey still lives on the device, which keeps offering it; that sign-in is a 401
  whose detail says to use the password.
- **Which passkey manager** (v10): registration stores the authenticator's AAGUID, and
  `passkey_providers.py` maps it to a name for the list ("Bitwarden · ES256"). All zeros means
  the authenticator didn't say and is stored as NULL. An unlisted AAGUID is kept, so refreshing
  that file (source and date in its docstring) names older passkeys too. Sign-in never reports
  an AAGUID, so passkeys from before v10 stay unnamed until re-added. Display only; it never
  decides whether a passkey is accepted.
- **Names** are one line, at most 60 characters. A blank name at registration becomes the
  passkey manager's name, or "Passkey". Not a browser guess: over the QR-code (hybrid) flow
  the passkey lives on the phone, not on the machine that clicked Add.
- The Dockerfile copies backend files one by one; `passkey_providers.py` has its own `COPY`
  line, and any new top-level module needs one too.

| Method | Path | Auth |
|---|---|---|
| GET  | `passkeys/config` | none |
| POST | `passkeys/register/options` (`password`) | any role |
| POST | `passkeys/register/verify` (`credential`, `name`) | any role |
| POST | `passkeys/login/options` | none |
| POST | `passkeys/login/verify` (`credential`) | none |
| GET  | `passkeys` | any role (own) |
| POST | `passkeys/rename` (`credential_id`, `name`) | any role (own) |
| POST | `passkeys/delete` (`credential_id`) | any role (own) |
| POST | `passkeys/clear_user` (`user_id`) | organizer |

`credential` is the browser's PublicKeyCredential JSON in a form field, like `schedule/build`'s
`config`. `GET /api/users` carries `passkey_count`.

## Invites and password reset (`routers/invites.py`, `routers/password_reset.py`)

Both mail a link through the **portal**, which holds the mail path
(`portal_sync.send_admin_mail` → `POST /api/sync/send_admin_mail`; `portal/CLAUDE.md` → Admin
mail). The scheduler builds the link from `ADMIN_PUBLIC_URL`, else `PORTAL_PUBLIC_URL`
(`portal_sync.site_origin()`), and the portal refuses any link that doesn't start with its own
copy of that origin plus `/admin/invite?t=` or `/admin/reset?t=`.

Shared rules, the same as the portal's magic links:

- **Only token hashes are stored** (`secrets.token_urlsafe(32)`, SHA-256), so a copy of the
  tables can't use a link, and an invite link can't be shown again after creation.
- **GET check is read-only**, because mail and chat previews fetch links. The POST consumes
  the token in one conditional UPDATE.
- Every check that can fail (username, password length, email) runs **before** the token is
  spent, so a typo leaves the link usable.

**Invites** (v15, `user_invites`): an organizer picks a role and optionally an address.
`POST /api/invites` returns the link once, to copy, and mails it when there's an address; a
failed mail still leaves a usable invite and comes back as `email_error`. 72 hours, single
use, revocable. `invites/accept` creates the user and signs them in, in one transaction with
the consume, and blanks the invite's `email` (it's now on the user). A username taken in
the race between check and insert rolls the consume back.

**Password reset** (v15, `password_resets`): sign-in stays by username. "Forgot password?"
posts a username; if the account has `users.email`, a 60-minute link is mailed.

- **The reply never varies** (`GENERIC`): unknown user, no email, mail failure and sent all
  read the same, so the page can't be used to find accounts. Failures go to the log and audit.
- **Rate limited** at 5 per 15 minutes per username and per client IP (an IPv6 client by
  its /64, `api_auth.rate_key`), in memory (one worker). A refused request doesn't count.
- **Completing signs the account out everywhere**: password_version bumped, every session
  deleted and popped from `_session_cache`, every other outstanding link for it spent. It
  doesn't sign in; the user signs in fresh.

**`users.email`** (v15) is only where reset links go. Set it in Profile
(`users/change_email`, which needs the current password, or a stolen session could redirect
resets and take the account), at invite acceptance, or by an organizer
(`users/set_email`, who can already set passwords outright). Blank clears it.

## Portal signups (`routers/portal_admin.py`, `portal_sync.py`)

Volunteers sign up in the portal (`portal/CLAUDE.md` → Signup) and arrive here as
`status = 'pending'` rows with their timeslot picks (`vol_picks`), pulled by the sync loop.

**Early-access invites** (`signup_invites`, v18) let one person sign up while signup is
closed: `POST /api/admin/portal/invites` (label, days 1-90) returns the link
(`{PORTAL_PUBLIC_URL}/apply/signup?invite=…`) **once** and stores only its SHA-256; `GET` lists
them with a state (unused / used / expired / revoked); `invites/revoke` stops an unused one.
`build_invites` / `push_signup_invites` send the hashes when the portal's heartbeat
`invites_hash` differs (and only to a portal that reports one). A pulled volunteer carrying
`invite_id` marks its invite used, with `used_by` the name. The signup is still pending.

**Send invites** (`POST /api/admin/portal/invites/send`: name, email, days) makes one invite,
**pushes the invite list to the portal first** so the link works on arrival, then mails it
through the portal (`send_admin_mail` kind `signup_invite`, plain text, via the relay). The
address passes through and isn't stored or logged; `emailed_at` (v19) records only that it
was sent. A failed send deletes its invite. One send per `INVITE_SEND_GAP_SEC` (10 s) across
all organizers, else 429 with `Retry-After`; the portal's hourly allowance comes back as a 429
too. The admin page paces itself to match (`composables/useInviteBatch.js`). Organizers decide in
Pending Volunteers:

- `POST /api/admin/portal/approve` (`vol_id`, optional `new_vol_id`) → active. A badge number
  re-keys the row: copy under the new id, move `vol_picks`, delete the old one, and record
  `portal_rekey_from`. Only while pending, and refused if the volunteer already has schedule,
  note or radio rows. Not `p-…`: that's the portal's namespace.
- `POST /api/admin/portal/decline` → declined. The row stays, so the address stays registered.
- `POST /api/admin/portal/settings` (`signup_open`) → `app_kv.portal_signup_open`. Refused
  while there's no `email_pubkey`.

**A volunteer's state is three fields** (v14): `status` (`pending` → `active` = approved,
i.e. through orientation; ours), `cancelled_at` (the volunteer's, from the portal) and
`inactive_at` (ours: Make inactive / Reinstate, `POST /api/admin/volunteers/set_inactive`, and
what the pending-queue `decline` now sets). **Who counts is three generated columns** on
`volunteers` (v17, `migrate_v17.sql`): `is_assignable` (approved and neither) for every picker,
`is_counted` (pending or approved and neither) for capacity and demand, `is_live` for the
pending queue. Use the column; don't write the rule out again in a query. Assigned but not
`is_live` means the slot **needs a replacement**: `_build_schedule` sets `needs_replacement`, and
`GET /api/admin/shift_flags` returns them as `replace`. The assignment stays until someone
else is assigned.

**Purge** (`POST /api/admin/volunteers/purge`: `vol_ids`, one or more, and `confirm`, which
must be the vol_id, or the count when there are several) deletes volunteers outright, for
clearing test volunteers so their addresses can sign up again. **Inactive ones only**, as the
guard for real data. `portal_sync.purge_volunteers` asks the portal first
(`POST /api/sync/purge_volunteers`, with any pending re-key's old id too) and deletes here only
once it has answered; a portal failure is a 502 and nothing is deleted. Both run under
`_state.lock`, which every reconcile also holds, so a pull already under way can't put the
volunteer back. Here, `_PURGE_LOCAL_SQL` deletes picks, shift flags and notes (check-ins and
ratings with them) and empties schedule slots and radio checkouts' `vol_id` (radio history
keeps `person_name`): spelled out, not left to `ON DELETE`, in case an older database lacks a
rule. Audited with name and id. Without a portal configured, it deletes here only.

**Columns have one owner each** (`portal_sync.VOLUNTEER_OWNED` / `SCHEDULER_OWNED`). The
volunteer owns picks, extras, answers and `time_pref`/`add_time`/`add_any` (signup, then
editing in the portal) and `cancelled_at`; the scheduler owns `status`, `name` and `inactive_at`. Pushes carry only
scheduler-owned columns; pulls always take volunteer-owned ones, even on a dirty row. That's
what stops an approval waiting to go out from overwriting an edit the volunteer just made.

**Shift flags** (v13, `shift_flags`): when a pull shows a volunteer dropped a timeslot they're
assigned to, `_flag_dropped_assignments` raises a flag, and the pull broadcasts a `volunteers`
SSE event. `GET /api/admin/shift_flags` returns only flags still true (assignment exists, the
timeslot isn't back in their picks, not kept), so unassigning (`clear_timeslot`) or re-picking
clears one by itself; `POST /api/admin/shift_flags/keep` dismisses it, and dropping it again
re-raises it. The banner is `ShiftFlags.vue`.

**Decisions reach the portal through `volunteers.portal_dirty`** (v11), not an immediate push:
each tick pushes every dirty row (rekeys first) and clears the flag only where the status
still matches what was sent. While a row is dirty, or is the old id of a pending re-key, a pull
skips it, so the portal's older copy can't undo a decision that hasn't landed. Pushes carry
only the columns the scheduler owns.

**Each sync tick, in order:** heartbeat → pull volunteers (so new picks count) → push dirty
volunteers → push settings that differ (`portal_signup_open`, and `email_pubkey` when the
heartbeat's `pubkey_fp` differs from ours) → snapshot → branding. Uploading a key or flipping
the switch calls `request_push()` so it doesn't wait a full interval.

**`GET /api/admin/volunteers`** (organizer, `routers/volunteers.py`) is the opposite: every volunteer whatever their status, with timeslot picks, answers, and assigned shifts with check-ins, for Admin → Volunteers. Three queries for the whole roster, grouped in Python.

**Signup picks timeslots, not bands** (v12). A pick is a day plus a start time from
`day_times`; bands were dropped because overlapping band times overstated the very slot most
likely to run short (see `migrate_v12.sql`). `portal_sync.compute_remaining` gives each
timeslot `floor(slots × (1 + overage%)) − assigned − unassigned picks`, with the overage in
`app_kv.portal_overage_pct` (0–200, set from the Volunteer Portal dialog); it travels as
`remaining` on the snapshot's `day_times`. `GET /api/admin/portal/demand` is the organizer's
un-inflated view (Shift Demand).

**The slot dialog's picker (`get_avail_vols`)** groups active volunteers as `requested` (a pick
is this day and time), `flexible` (answered the portal's `flexible` question and picked
something that day), `available` (an extra here, or "any time") and `no_picks`, and drops
anyone assigned that day at an overlapping time. Overlap comes from the `time_str` ranges
(`times_overlap` in `routers/schedule.py`; the portal's `routers/signup.py` has the same rule
for refusing overlapping picks). `get_unassigned_timeslots` uses the same rule.

**Pickers show active volunteers only.** `_query_vols` (behind `get_vols` and
`get_avail_vols`) filters `status = 'active'`, so pending signups can't be assigned before
approval. It also tolerates a `time_pref` with 0, 1 or N entries, and its `ORDER BY` uses the
UNION's output column names: Postgres rejects `v.vol_id` there, which had broken the query
since the move from SQLite.

## Shift check-in (`checkin_codes.py`, `routers/checkin.py`)

Each assigned shift has a code, `BSC1.{slot_key}.{b64url(vol_id)}.{mac}`: HMAC-SHA256 under
`app_kv.checkin_key` (made on first use), truncated to 128 bits. It goes out in the schedule
email as a QR code (tools/vol_mail) and is scanned on the Schedule tab.

- **Deterministic**, so a re-sent email (draft, then final) carries the same code. A valid
  code isn't enough: the volunteer must still be assigned to that slot (409 otherwise), which
  is what voids a code on reassignment. **Rotate key** (App Settings) voids every code.
- **`record_checkin` is the one check-in path.** Both `checkin/scan` and the slot dialog's
  `checkin_vol` call it, so both apply the same rule. A shift's start is `days_table.event_date`
  plus the start of `time_str`, in `app_kv.event_tz` (App Settings; default
  `America/New_York`; needs the `tzdata` package on Alpine). More than **45 min early** or
  **10 min late** returns `needs_confirm` and writes nothing; the operator's **Check in anyway**
  resends with `force`. No date, or a `time_str` that isn't a range, checks in as `unknown`.
- A forced early or late check-in also writes `[CheckInTiming] Early|Late N min | {slot}`.
  It isn't filtered out of the notes report (organizers want to see it), and
  `reset_vol_status` deletes it with the check-in.
- A repeat check-in is `already: true` and writes nothing. Before this, `checkin_vol` inserted
  a duplicate note every click.
- `test_checkin_logic.py` covers the codes and the rule with no database.

## Export API (`api_auth.py`, `routers/export.py`, `routers/api_tokens.py`)

Read-only, for other servers and `tools/vol_mail`. `docs/api/export.md` is the caller's guide.
Its endpoint reference is generated from the response models in `routers/export.py` (see the
module docstring), so a new field goes in the model, with a `description`, or the response
drops it.

- **Bearer keys, not sessions.** Organizers make any number in Admin → API Keys, each with a
  label, scopes, an expiry (≤ 365 days) and an optional IP allow-list. `bsc_` + 32 random
  bytes, shown once, SHA-256 stored (`api_tokens`, v16). Revoked, never deleted.
- It's internet-facing: the admin hostname sits behind an access gate at the edge, but
  `/api/export/*` bypasses the gate so servers and `vol_mail` can call it. **Every failure is
  the same bare 401**; 10 failures per IP in 15 minutes makes that IP a 429 (in memory, one
  worker). "Per IP" is `rate_key()`: an IPv6 client counts by its /64, since one connection
  is normally given a whole /64 to pick fresh addresses from. Allow-lists, audit and
  `last_used_ip` keep the exact address. Calls are
  audited via `audit(..., who=…)` under the key's label and id, never the key.
- Scopes: `schedule:read`, `volunteers:read`, plus `volunteers:email` (adds `email_enc`)
  and `checkin:codes` (adds `checkin_code`). Only the mailer's key needs the last two.
- Both endpoints build on one slot query (`_slots`), so a volunteer's shifts and the schedule
  can't disagree. `volunteer_records()` is shared with the email preview, so the preview
  shows what the tool will be sent. "active" means `is_assignable`.
- **Names**: `names.split_name` reads the stored `First L. (handle)` from the right (so
  `Mary Ann S.` works) and `export_name` writes `Handle [First L.]`. The parts aren't stored
  separately anywhere in the scheduler.
- No CORS, deliberately: callers are servers.

## Schedule email template (`email_render.py`, `routers/email_template.py`)

Call for Volunteers → Schedule Email. Organizers write Markdown (subject, body with
`{{shifts}}`, and a per-shift block with `{{qr}}`), upload PNG/JPEG/GIF images
(`email_assets`, ≤ 1 MB each, ≤ 5 MB total, type read from the bytes, no SVG) or copy in the
site logo, preview it as any volunteer, and **download a package**. This site never sends it;
`tools/vol_mail` does.

- **Markdown is converted once, here.** The package holds HTML and text with placeholders
  still in, and the tool only fills them in with its own copy of `fill`/`render_message`;
  `tools/vol_mail/test_vol_mail.py` checks the two agree. Change both or neither.
- markdown-it runs with `html: False` (raw HTML is escaped) and `breaks: True`.
  Values are HTML-escaped on fill except `shifts` and `qr`, which the filler builds.
- Saving refuses unknown placeholders, a missing `{{shifts}}` or `{{qr}}`, or a blank
  subject. Images not uploaded yet are allowed at save and listed; the package refuses them.
- The preview is rendered server-side with real codes, images and QR codes as data URLs,
  and shown in an `<iframe sandbox="">`.

## `email_hmac` for volunteers added by hand

`add_vol` takes the plain address alongside `email_enc` and stores
`portal_sync.email_hmac()` (strip + lowercase, the portal's exact rule), so hand-added
volunteers get the same duplicate check as portal signups (409 naming who has the address).
The plain address is hashed and dropped, never stored or logged; without `EMAIL_HMAC_KEY`
the volunteer is added unindexed (`email_indexed: false`). For volunteers added before this,
`POST /api/admin/volunteers/email_hmac` fills a missing hash from an address the Volunteers
tab decrypted in the browser; it never overwrites one.

## Portal content (`routers/portal_content.py`, `portal_content.py`)

Volunteer Portal → Landing page / Questionnaire / Invite email. `GET /api/admin/portal/content`
gives the editors the saved parts (`None` at the default), the defaults, the placeholders, the
icons and `answer_counts` (volunteers with an answer under each key, from `answers`' JSONB
keys). `POST /api/admin/portal/content` (`part`, `value` JSON; no `value` resets it) checks
and stores it in `app_kv` (`portal_landing`, `portal_questionnaire`, `portal_invite`) and
calls `request_push()`; `.../preview` renders it as the portal will, with invented values for
the invite. `portal_sync.build_content` / `push_content` send all three to
`POST /api/sync/content` when the heartbeat's `content_hash` differs.

`portal_content.py` is a copy of `portal/content.py` below the docstring, so saving refuses
what the portal would; `test_export.py` fails if the two differ. Edit both together. The
Dockerfile lists every module it copies, this one included.

## Conference contact details (`routers/setup.py`)

`POST /api/setup/set_title` also takes `contact_email`, `conference_url` and
`code_of_conduct_url` (`CONFERENCE_FIELDS`), stored in `app_kv` beside `app_title` and pushed
with the branding (`portal_sync.build_branding`). They're taken only with
`with_contact=true`, all three at once, blank meaning remove: FastAPI reads an empty form field
as missing, so "missing means unchanged" couldn't tell "clear it" from "not sent". Without the
flag (the wizard's Skip) only the title changes. `clean_contact_email` wants one plain
address; `clean_public_link` wants `http(s)://` with a host and no whitespace, because each
becomes an `href` on a public page. The portal checks both again. `GET /api/app_config`
returns them (`""` when unset).

The **sending** address is not one of these: it's `MAIL_SENDER` in `.env`, read by the mail
relay and vol_mail only (`mailrelay/CLAUDE.md`).

## Logos (`routers/setup.py`)

Two, both in `app_kv` as data URLs and both pushed to the portal by `build_branding`:

- **Theme logo** (`app_logo`, `POST /api/app_logo`) — the year's theme, the primary one. The
  browser draws a 32×32 favicon from it (`app_favicon`, posted alongside). The admin app's
  header and the email template's site logo use it; the volunteer pages show it large on the
  landing page and as a faint background.
- **Header logo** (`app_header_logo`, `POST /api/app_header_logo`) — the smaller logo in the
  volunteer pages' upper left. No favicon. `GET /api/app_config` returns it as `header_logo`.

Both are organizer-only. **The image is a file upload** (`logo`), not a data-URL form field:
Starlette caps a plain field at 1 MB, and a data URL is a third bigger than its image, which
used to stop anything over ~750 KB. A file part is read here up to `MAX_LOGO_BYTES` (2 MB;
over is a 413). The type comes from the file's own bytes (`_logo_type`: PNG, JPEG, GIF,
WebP, SVG; anything else is a 400), and the server builds the data URL it stores, so
`app_kv` and everything downstream are unchanged. `remove=true` removes; a save with no file
is a 400, never a silent removal. The favicon stays a small form field (a PNG data URL).

nginx allows a 3 MB body on exactly these two paths (`frontend/default.conf`); every other
path keeps its 1 MB default. The email template's **site logo** copy keeps its own 1 MB
image cap (`MAX_ASSET`), so a theme logo between 1 and 2 MB uploads fine but that button
refuses it.

## DB conventions

- **PostgreSQL 18** (compose service `postgres`, volume `pgdata18`), reached through a
  `ThreadedConnectionPool` in `db.py`. `PgClient` is a thin sqlite3-shaped shim that rewrites
  `?` placeholders to `%s`, which is why every query still uses `?`.
- `RealDictCursor` is set on every connection — always use `row["col"]`, never `row[0]`.
- `get_db()` is a FastAPI generator Depends; FastAPI caches it per-request so all Depends in the same request share one connection.
- **Every statement is a complete literal** with `?` parameters (root `CLAUDE.md` → Linting &
  docs; `dynamic-sql` fails the lint). `PgClient` rewrites every `?`, so `?::text[]` works.
- **A loop over rows that may fail needs a savepoint per row** (`setup._row_savepoint`). One
  failed statement aborts the whole Postgres transaction, and `commit()` then silently rolls
  back. This is what broke the CSV imports.
- The pool lives in `db._state`; tests call `db.close_pool()` after pointing `DB_*` at a
  throwaway database.
- **Schema changes: one file per release.** 1.0 is the baseline: `scheduler.sql` in full, at
  schema version 19 (the pre-release patches are gone; git history has them). A release that
  changes the schema adds one file, `migrations/<release>.sql`, and one entry to
  `SCHEMA_RELEASES` in `db.py` (`("1.1", 20, "1.1.sql", True)`), and every schema change in
  that release goes into that file **and** into `scheduler.sql`, which is always the whole
  current schema for a fresh install. While the entry's last field is `True` (in progress),
  the file re-runs on every start, so it must be idempotent (`IF NOT EXISTS`, `ADD COLUMN IF
  NOT EXISTS`, `ON CONFLICT DO NOTHING`); flip it to `False` when the release is tagged. A
  database older than 1.0 is refused at startup. `tools/schema_check.sh` compares a running
  database with a fresh install from `scheduler.sql`; run it before each release.

## Upgrading PostgreSQL

The official image (18+) keeps data in `/var/lib/postgresql/<major>/docker` under a volume
mounted at `/var/lib/postgresql`, and refuses to start on an older major's data rather than
silently starting empty. The database is small (about 10 MB in Nov 2026), so a major upgrade
is a dump and restore into a fresh volume, not `pg_upgrade`. Done 16 → 18 on 2026-11-01; for
the next one, add a `pgdata19` volume, point the mount and image at it, and:

```bash
cd ~/Scheduler                                  # on the host
podman-compose stop backend                     # no writes during the dump
podman exec scheduler_postgres_1 pg_dump -U scheduler -Fc scheduler > ~/pg-old.dump
chmod 600 ~/pg-old.dump                         # outside ~/Scheduler: rsync --delete
COUNTS="select table_name, (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I', table_name), false, true, '')))[1]::text::int from information_schema.tables where table_schema='public' and table_type='BASE TABLE' order by 1"
podman exec scheduler_postgres_1 psql -U scheduler -d scheduler -Atc "$COUNTS" > ~/pg-old-counts.txt
podman-compose down                             # podman's --requires link blocks replacing postgres alone
# rsync the new compose.yml, then:
podman-compose up -d postgres                   # wait for (healthy)
podman exec -i scheduler_postgres_1 pg_restore -U scheduler -d scheduler --no-owner --exit-on-error < ~/pg-old.dump
podman exec scheduler_postgres_1 psql -U scheduler -d scheduler -Atc "$COUNTS" | diff ~/pg-old-counts.txt - && echo "row counts match"
podman-compose up -d
```

Rollback is the old image and mount in compose.yml; the old volume is never touched. Once the
new version has settled, `podman volume rm` the old volume and `shred -u` the dump (it holds
volunteer names and email ciphertext).

## Schedule structure

`day_times` declares which times each day actually runs — one day may have an evening shift the
others don't. It is a **superset** of the day/time pairs present in `schedule`, and every write
path maintains that: `add_slot`, `bulk_add_slots`, `upsert_day`, `upsert_time`, and the CSV
import all insert the pair; `delete_day`/`delete_time` delete their rows.

Slot creation goes through two helpers in `routers/schedule.py` rather than raw cross products:

- `_day_time_pairs(db, day_keys=None, time_keys=None)` — pairs from `day_times`, optionally
  narrowed. `None` means "all"; an empty list means "none".
- `_fill_slots(db, pairs, pos_keys)` — inserts pairs × positions, `ON CONFLICT DO NOTHING`,
  returns rows created.

So "all times" always means "all times *this day* runs". `upsert_day`, `upsert_time`, and
`upsert_position` each take `fill_all` to populate slots on creation; `bulk_add_slots` takes
`prefix`, `label`, `start`, `count`, `day_keys`, and `time_keys`.

`pos_key` must match `^[a-z0-9][a-z0-9-]*$` (`_validate_key`, wrapped by `_validate_pos_key`).
Underscores are barred because `assign_vol` and `clear_timeslot` parse slot keys as
`pos_key.split("_")[0:3]`. `day_key` and `time_key` share the rule — they are the other two
components of that key — and `/api/schedule/build` enforces it on all three.

Positions are ordered by `LENGTH(pos_key), pos_key` so `sec10` follows `sec9` instead of `sec1`.

### Time keys must be unique per range

`time_str` lives on the global `times_table` row, not on `day_times`, so two days running
different-length shifts from the same start cannot share a key: Saturday 1500-1900 and Sunday
1500-1700 need `1500` and `1500-1700`. `upsert_time` takes `create=True` from the add path and
rejects a key already held by a different range instead of silently relabelling the first day's
shift. The frontend derives non-colliding keys up front (`time_keys.js`); the flag is the
backstop.

### Category colors

`categories.cat_color` is a `#rrggbb` string, validated by `_norm_color()` — it ends up in a
style attribute on the schedule chips, so anything else is a 400. `_build_schedule` puts it on
each category as `color`, not on every position.

A new category with no color given takes the next entry from `_DEFAULT_CAT_COLORS`, so every
category is visibly distinct without anyone picking anything. `upsert_category` is the edit
path too: an existing name keeps its `sort_order`, and is recolored only when a color is
actually supplied — re-adding a name with no color leaves it alone. Only the background is
stored; the frontend derives the text color from it (`frontend/src/lib/colors.js`).

### `/api/schedule/build`

Backs the setup wizard's schedule builder: days, times, categories, positions and slots in one
commit. Takes a single `config` form field holding JSON (the API takes no JSON request bodies),
shaped as `days[]`, `times[]` (each with its own `day_keys`), and `categories[]` — each category
carrying a `counts` map from `"{day_key}|{time_key}"` to how many of its positions that cell
gets. That per-cell count is what `bulk_add_slots` cannot express: ten registration desks at
0800 and four at 1500.

A category is *numbered* (labels `{label} 1…N`, N from its largest count) or *named* (`names`
supplied, a count of N meaning the first N of the list). Either way keys are `{prefix}{n}`, so
the `LENGTH(pos_key), pos_key` display ordering keeps the list in the order it was given.
Everything inserts `ON CONFLICT DO NOTHING`, so a re-run adds only what is missing.

### `/api/schedule/apply_grid`

Backs the editor's counts grid, and is `/api/schedule/build`'s counterpart for a schedule that
already has volunteers in it: build is additive and assumes an empty database, this one **diffs
against live data** and can therefore also take slots away.

- **Assignments are never discarded.** Lowering a count removes only unassigned slots; anything
  with a volunteer stays and the shortfall comes back in `blocked` as `{category, cell, kept}`.
  Same rule as `set_day_time`, `remove_slot` and `delete_position`, which all refuse rather than
  drop an assignment.
- **Counts match on how many, not on which.** `_reconcile_cell(ordered, here, want)` is the pure
  helper that decides: fill from the lowest-numbered positions not already in the cell, empty
  from the highest-numbered end. Raising then lowering a count therefore does not churn through
  the assignments in between. It is unit-tested in `test_grid_logic.py`.
- **`cells` confines the write.** Only the day/time pairs the grid actually displayed are
  reconciled, so a stale grid cannot clear a cell it never showed. Each must be a real
  `day_times` pair.
- **New keys join the run already there.** `_infer_prefix` reads the stem off the category's
  existing keys rather than deriving it from the name — category "Security" is keyed `sec1…`,
  and deriving `security` from the name would scatter `security7` through the run.
- **Display names**: only written for positions the call creates, except in a *named* category,
  where the supplied list is applied to positions 1…N. A numbered category never overwrites an
  existing `pos_str`.
- Positions left with no slots anywhere and beyond what the grid needs are deleted; anything
  still scheduled or assigned is left alone.

## Schedule build

- `_build_schedule` pre-loads all days from `days_table` before joining schedule rows. This ensures days with no slots still appear. Any change to day/slot rendering must preserve that pre-load.
- Back-to-back shift adjacency: `1130` is adjacent to `0800`, `1300`, and `1500`. Used in `get_avail_vols` and `get_unassigned_timeslots`.

## Radio tracking

`radios` is the inventory (one row per barcode) and `radio_checkouts` is the ledger (one row per
issue, closed by setting `checked_in_at`). A radio auto-registers on its first checkout, so
there is no inventory step to run before an event.

- **One open checkout per radio is a database guarantee**, not a read-then-write check: the
  partial unique index `radio_one_open_checkout` covers `barcode WHERE checked_in_at IS NULL`.
  `checkout_radio` reads first so it can name the previous holder, and catches
  `UniqueViolation` for the race between that read and its insert — the only 409 left.
- `person_name` is stored even when `vol_id` is set. The `vol_id` FK is `ON DELETE SET NULL`, and
  a returned radio's history should still say who had it.
- **A handoff is just a checkout to someone else.** Issuing a radio that is already out closes
  the open checkout (`checked_in_by` = the operator) and opens a new one in the same request,
  so passing a radio along takes one scan. The response carries `transferred_from`, and the
  audit line records both names. Nothing guards against a mis-scan taking a radio off its
  holder — the handoff is recorded and reversible, not prevented.
- **Re-scanning to the current holder is a no-op**, reported as `already_held: true` with no
  write at all. `_same_person` compares `vol_id` when both sides have one (two volunteers can
  share a name) and otherwise matches names case-insensitively. Without this, a double scan
  would reset the holder's elapsed time and leave a phantom return in the history.
- Check-in needs only the barcode; the open checkout identifies the holder.
- **Timestamps are UTC by construction.** The columns default to `NOW() AT TIME ZONE 'UTC'` and
  `_iso()` appends a `Z`, because a naive timestamp is parsed as *local* time by the browser and
  every "out for 3h" would be wrong by the viewer's offset. Any new radio timestamp must keep
  both halves of that.

`test_radios.sh` walks the whole path (checkout → handoff → idempotent re-scan → checkin →
double checkin → history row count) against a running stack; it needs a username and password.

## Tests

- `test_grid_logic.py` — unit tests for the grid's add/remove decisions. No database, no
  stack: `cd backend && python3 test_grid_logic.py`.
- `test_grid.sh <user> <pass>` — `/api/schedule/apply_grid` end to end against a running
  stack, inside a throwaway category. Needs `jq`. The assigned-slot guard needs a real
  volunteer, so the script prints the manual steps for that one instead of faking it.
- `test_categories.sh <user> <pass>` — category colors end to end.
- `test_radios.sh <user> <pass>` — radio checkout/checkin end to end.
- `test_remaining_sql.py` — `compute_remaining` (capacity, overage, what counts as demand) run
  against in-memory SQLite. No Postgres.
- `test_signup_sync.py` — the scheduler's half of signup against a real Postgres (pull with
  picks, approve / decline / re-key, the dirty-flag retry, settings push, picker filter), with
  a fake standing in for the portal's side of the wire. Same `TEST_PG_*` variables.
- `test_checkin_logic.py` — check-in codes and the early/late rule. No database:
  `cd backend && python3 test_checkin_logic.py`.
- `test_setup_import.py` — the setup wizard's CSV imports against a **real Postgres**: a bad
  row (forced by a test trigger) is skipped and the rest are kept, and volunteer re-import
  updates in place. Same `TEST_PG_*` variables and schema drop.
- **Postgres for these tests**: the dev container has Postgres 18 at
  `/usr/lib/postgresql/18/bin`. A throwaway cluster:
  `initdb -D /tmp/pg -U postgres -A trust`, then
  `pg_ctl -D /tmp/pg -o "-p 55432 -k /tmp" -w start`, then create role `scheduler` and
  database `scheduler_test`, and run with `TEST_PG_HOST=127.0.0.1 TEST_PG_PORT=55432`.
- `test_export.py` — export keys and endpoints, check-in (scan, confirm, reset, rotate),
  `email_hmac` on add and backfill, the email template, preview and package, and the v15 → v16
  migration, against a **real Postgres** (same `TEST_PG_*` variables and schema drop). The
  database must be UTF-8, as the Postgres image's is: the test feeds `scheduler.sql` in with
  its comments.
- `test_route_prefixes.py` — no backend route under `/api/portal` (see Module layout), and
  `portal_sync.missing_settings()`. No database.
- `test_invites.py` — invites, password reset and `users.email` against a **real Postgres**,
  with `portal_sync.send_admin_mail` faked. Same `TEST_PG_*` variables and schema drop as
  `test_passkeys.py`.
- `test_passkeys.py` — passkey registration and sign-in against a **real Postgres**, with a
  software authenticator (`passkey_test_device.py`, ES256 and ML-DSA) that makes genuinely
  signed responses. Skipped unless `TEST_PG_HOST` is set, and it **drops the target database's
  public schema**, so point it at a throwaway one. See its docstring for the variables.

## Note conventions in `vol_notes`

System notes use fixed prefixes so they can be filtered out of user-facing reports:
- Check-in: `[CheckIn] | {day_key}_{time_key}_{pos_key}`
- Check-in confirmed early or late: `[CheckInTiming] {Early|Late} {N} min | {slot_key}` —
  deliberately **not** filtered from reports
- Ratings: `[Rating] {Good|Bad|No-Show} | {slot_key}`
- Organizer notes: any other string; excluded from reports via `NOT LIKE '[Rating]%' AND NOT LIKE '[CheckIn]%'`

## Running locally

```bash
docker compose up --build     # full stack; backend needs the postgres service
```

`main.py` accepts only `-d` (debug logging) and `--audit <path>`.

**Quiet logs.** `QuietPolls` (in `main.py`, attached in `lifespan`) drops successful
`GET /api/health` lines from uvicorn's access log, and the `httpx` logger is set to WARNING
unless `-d`, so the portal sync's per-request lines are gone. A healthy idle backend logs
nothing; a missed heartbeat is `portal_sync`'s own **warning** (once, on the transition;
repeats at debug), and recovery is one `portal sync healthy` line. The `--add_user` /
`--export_csv` / `--add_slot` CLI flags live in the legacy `Scheduler/restAPI.py` and have no
equivalent here — schedule structure is edited through `/api/schedule/*` and the Schedule Editor
dialog, and the first user is created by the setup wizard.
