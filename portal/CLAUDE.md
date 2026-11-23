# Volunteer Portal

A **separate application** from the scheduler. It shares no database, no session secret, and no
code with `backend/`. The only channel between them is the signed sync API in `routers/sync.py`.

If you are about to `from backend import ...` or connect to Postgres from here, stop — that is
the one thing this service exists not to do.

## Why the separation is shaped this way

A full compromise of this host yields volunteer names, email **ciphertext**, and a copy of the
schedule. It does not yield organizer credentials, volunteer notes or ratings, radio history, the
scheduler's cookie secret, or a single readable email address.

## Running it

```bash
docker compose up portal            # needs EMAIL_HMAC_KEY, SYNC_KEY_ID, SYNC_SECRET
python main.py -d                   # locally, same env vars required
pytest test_portal_local.py -v      # no Postgres, no SMTP, no network needed
```

**Quiet logs.** `QuietPolls` (in `main.py`, attached in `lifespan`) drops successful
`GET /api/health`, `/api/sync/heartbeat` and `/api/sync/config` lines from the access log:
the container healthcheck and the scheduler's poll, every tick. A failing one (status ≥ 400,
e.g. a bad signature) and everything else still log. The portal keeps its own copy of the
filter; it shares no code with the backend.

**Runs as uid 1001** (`appuser`), the same as the backend, since both mount `./logs` with `:U`.
Code and packages are installed as root, so the app can read but not modify them. `/data` is
mounted `:U` so podman re-owns it on start: before 2026-11 the portal ran as root, and its
`portal.db` was root-owned.

`--workers 1`, always. SQLite has a single writer, so a second worker manufactures
`SQLITE_BUSY` for nothing. The service cannot be scaled horizontally; at under 200 volunteers
that is not a constraint.

## Store

SQLite at `PORTAL_DB` (`/data/portal.db` in the container, on the `portal_data` volume).

- Schema in `portal.sql`, the whole current schema; version in `PRAGMA user_version`,
  **independent** of the scheduler's `schema_versions` table. **One file per release** after
  1.0 (version 7, the baseline): `migrations/<release>.sql` plus an entry in `SCHEMA_RELEASES`
  in `db.py`, with every change mirrored into `portal.sql`. A release in progress re-runs its
  file on every start: SQLite can't `ADD COLUMN IF NOT EXISTS`, so `_apply_release_file` skips
  "duplicate column" and "already exists", and splits on `;` (no triggers in a release file).
  The scheduler's `backend/CLAUDE.md` → DB conventions has the full rule.
- `db.connect()` applies four pragmas that are **not** optional: WAL (readers don't block the
  writer), `foreign_keys=ON` (off by default in SQLite, and this schema relies on it),
  `synchronous=NORMAL`, `busy_timeout=5000`.
- `check_same_thread=False` is set because FastAPI runs sync dependencies in a threadpool while
  async endpoints run on the event loop, so one request's connection legitimately crosses
  threads. Safe only because each request opens its own connection and never shares it.
- One connection per request via `Depends(db.get_db)`. `?` placeholders and `row["col"]` —
  the same idiom as the scheduler's `PgClient`, on purpose.
- Backup with `.backup`, never `cp` (WAL means a copied file can be torn):
  ```bash
  sqlite3 /data/portal.db ".backup '/data/backups/portal-$(date +%F-%H%M).db'"
  ```

## Secrets

`EMAIL_HMAC_KEY` is **required and has no default**, and `config.py` refuses to start without
it. This is deliberate: if the portal generated a fresh key when the variable was missing, every
stored `email_hmac` would be orphaned — nobody could sign in and the unique index would stop
deduplicating. That is silent at boot and disastrous a week later.

It lives in the environment and **never in the database**, because `portal.db` holds the hashes
it produced; keeping the key alongside them would hand over both halves to anyone who copies the
file. The scheduler reads the same value from its own environment for the same reason, and the
two compare `hmac_key_fp()` on every heartbeat to catch a mismatch.

## Email: two values, two jobs

| Value | What it is | Who can reverse it |
|---|---|---|
| `email_hmac` | `HMAC-SHA256(key, normalize(email))`, hex | nobody (but guessable with the key) |
| `email_enc` | RSA-OAEP(SHA-256) under `portal_kv.email_pubkey`, base64 | only the offline private key |

`normalize` strips and lowercases and does **nothing else** — no Gmail dot-stripping, no
plus-tag removal. Both would merge addresses that are genuinely distinct at some providers, and
a wrong merge means one person holding another's signup.

The partial-unique index on `email_hmac` **is** the duplicate-signup guarantee. `check_email` is
only UX; the index is what holds when two people submit the same address in the same second.

Outbound mail goes through `mailer.py`: `MAIL_BACKEND` is `file` (tests, dev), `smtp`, or
`relay`. `relay` posts `{to, subject, body}` to the `mailrelay` container, which alone holds
the Microsoft Graph credentials and sends plain text as its `MAIL_SENDER` (see
`mailrelay/CLAUDE.md`; tenant setup in `docs/operations/graph-mail-setup.md`). Don't bring the Graph
credentials back into this process. `python mailer.py --test <address>` sends one message
with the current settings.

Nothing in this process can decrypt `email_enc`. That is the design, and its consequence is that
**the portal can only ever mail an address someone has just proved they know** — a magic link,
during their own request. Every other volunteer message is an offline act by whoever holds
`private.pem`.

### Admin mail: the one exception

`POST /api/sync/send_admin_mail` mails **organizer** invites and password resets,
**early-access signup invites** to would-be volunteers, and **security alerts** to organizers,
for the scheduler, which has no mail path of its own. The address there comes from the scheduler, not
from someone proving it, so the endpoint is kept narrow:

- signed sync API only, with `op_id` replay like every POST (a retry doesn't send twice);
- four fixed templates (`templates/admin_invite.txt`, `admin_reset.txt`, `admin_alert.txt`
  via `Mailer.send_admin`, and the organizers' signup invite), kind `invite`, `reset`,
  `alert` or `signup_invite`, plain text;
- the link must start with the organizer site's own `{ADMIN_PUBLIC_URL}/admin/invite?t=` or
  `/admin/reset?t=` (matching the kind), with no whitespace. `ADMIN_PUBLIC_URL` falls back to
  `PORTAL_PUBLIC_URL` (`config.admin_url`); deployed, it's a separate hostname, because the
  public portal host serves no `/admin`;
- one address, checked against a strict pattern (no header injection);
- a `signup_invite` link must start with the portal's own
  `{PORTAL_PUBLIC_URL}/apply/signup?invite=`;
- an `alert` link must be exactly `{ADMIN_PUBLIC_URL}/admin/audit`, and its text is
  **structured and defanged** (`_alert_values`, `alert_text`): `rule` matches
  `^[a-z0-9][a-z0-9_.]{0,63}$`, `severity` is one of five, `title` ≤ 120 characters,
  `held_back` an int, and `facts` at most 10 `[label, value]` pairs (≤ 40 / ≤ 200). Every
  string has control and format characters (newlines, NUL, bidi) turned to spaces, `{{`
  broken so it can't reach the template filler, and link-shaped text broken
  (`https[:]//`, `www[.]`, `javascript[:]`). Alert details include things an attacker typed
  (a username at sign-in), so they mustn't arrive as a clickable link in an organizer's inbox.
  A bare domain (`evil.example`, no scheme or `www.`) is left alone, and some mail apps
  link those anyway; the template's footer tells the reader the details are untrusted;
- at most 20 admin mails an hour, separately 200 signup invites an hour
  (`SIGNUP_INVITE_MAIL_PER_HOUR`, under the relay's 300 so sign-in links still get through
  during a batch), and separately 60 alerts an hour (`ALERT_MAIL_PER_HOUR`, so a burst of
  alerts can't use up the allowance password resets need, mid-attack), all counted from
  `portal_audit`, otherwise 429;
- audit and `sync_ops` record only `admin_mail.sent <kind>`, never the address or link.

A compromised scheduler could therefore send its own site's invite, reset or signup-invite
links to addresses it chose, or an alert of short, single-line, defanged text pointing at
its own `/admin/audit`, but nothing else. `test_admin_mail.py` pins each of these.

### Request signals

`security_signals.py` is **the backend's file, copied unchanged** (`backend/test_security_signals.py`
fails if they differ; `backend/CLAUDE.md` → Request signals has the detail). `main.py` adds it
with this app's `auth.client_ip` (rightmost, invariant 3 below), bodies under `/api/sync/` left
unread, and a 401 under `/api/sync/` reported as `sync_auth_fail` (critical: nothing but the
scheduler should call it, so a bad signature means something inside the compose network is
trying). It only watches, except that a request carrying a NUL is a 400.

Findings go to `portal_security_events` (`report_signal`), never with an address in them. The
heartbeat reports `security_events_rev` (the newest id) and the scheduler pulls new rows from
`GET /api/sync/security_events?since=&limit=` into its audit log and alert rules. Rows older
than 7 days are pruned on each pull.

## Auth invariants

All three are easy to lose in a refactor:

1. **Only hashes are stored.** Login tokens and session ids are generated, handed out once, and
   kept as SHA-256. A read of `portal.db` must not be enough to mint a session.
2. **A GET never consumes a token.** Mail scanners and link prefetchers fetch URLs, so if the
   GET burned the token the real click would always arrive second and fail. `GET verify` renders
   a confirmation; the `POST` behind it consumes, via one conditional UPDATE so two simultaneous
   clicks resolve by rowcount.

3. **The client IP comes from the rightmost `X-Forwarded-For` entry.** One path reaches the
   portal: the edge → the tunnel client on the host → frontend nginx (either server in
   `default.conf`) → portal. nginx resolves the client from the edge's client-IP header with the
   realip module and sends `X-Forwarded-For` as that single value, so rightmost and leftmost are the
   same trusted address. The portal publishes no host port, so nothing reaches it directly.

   `_client_ip` keys the per-IP buckets in `ratelimit.check`, so reverting it to `[0]` makes
   the per-IP limit bypassable by sending a fresh forged header each request. Any other proxy
   added in front of the portal needs this rechecked.

   "Per IP" means `ratelimit.ip_bucket()`: an IPv6 client is counted by its **/64**, since one
   connection is normally handed a whole /64 and could otherwise take a fresh address per
   request. IPv4-mapped addresses count as IPv4. Tokens and sessions still record the exact
   address. The backend has its own copy (`api_auth.rate_key`); the apps share no code.

A session with `vol_id IS NULL` is a **signup session** and authorises exactly one endpoint (the
signup submit). `require_volunteer` rejects it with 403.

## Sync API

Scheduler is the client, portal is the server, **traffic only flows scheduler → portal**. There
is no callback URL and no webhook — which is what lets the portal sit on an internet-facing host
while the scheduler does not.

Every request carries `x-sync-key-id`, `x-sync-timestamp`, `x-sync-nonce`, `x-sync-signature`,
signing:

```
{METHOD}\n{path-with-query}\n{timestamp}\n{nonce}\n{sha256_hex(body)}
```

Three defences: the signature, a ±300s timestamp window, and the `sync_nonces` table. Every
failure returns a bare 401 — a caller who cannot sign has no business learning which check
rejected them.

Besides pushes and pulls, the scheduler can ask for two things: `send_admin_mail` (above), and
`purge_volunteers` (`{op_id, vol_ids}`, up to 500), which deletes each volunteer, their picks,
every session and login token for their id or address hash, and that address's rate-limit
buckets, and clears their `snapshot_slots`, in one transaction. The address can then sign up
again from scratch. Unknown ids are skipped; the audit records `volunteer.purged` with the id.
The scheduler only sends inactive volunteers, and deletes its own copy after this answers.

**`op_id` is required on every POST.** `sync_ops` stores the result and replays it for a repeat,
so a push that times out *after* the portal committed can be retried safely.

Pushes **replace wholesale** rather than diffing. The snapshot is a copy of someone else's
state, and reconciling a diff against a copy is how copies drift; `content_hash` makes an
unchanged push free, so the scheduler can re-push whenever it is unsure.

`security.py` and the scheduler's `portal_sync._sign` are two independent implementations of one
canonical string. `backend/test_portal_sync.py` signs with one and verifies with the other —
keep that test passing, because the failure mode is a bare "Unauthorized" that says nothing
about its cause.

## Branding

Pushed from the scheduler, never edited here. The logo arrives as a data URL and is stored
**decoded**, served from `/api/portal/branding/logo` with an ETag and a one-hour cache. Inlining
a 2.5MB data URL into the page would put it on every load for every volunteer, uncacheable —
the worst shape possible for a phone on conference wifi.

Before the first push, `/api/portal/branding` returns a neutral "Volunteer Portal" rather than
an empty header.

**Two logos** (schema v4). `logo` is the year's **theme logo**, the primary one: the favicon is
drawn from it (in the scheduler's browser), and the volunteer pages show it large on the landing
page and faintly behind every page. `header_logo` is the smaller one in their upper left, as
on bsidescharm.org, served from `/api/portal/branding/header_logo`; without it the header uses
the theme logo. Each keeps its own MIME type (`logo_mime`, `header_logo_mime`) and its own
ETag. A push without `header_logo` (an older scheduler) clears it.

**Contact details** (schema v6): `contact_email`, `conference_url` and `code_of_conduct_url`,
which organizers set with the title. They're checked again here (`_public_contact`): one plain
address, and `http(s)://` links with a host only, since each becomes an `href` on a public
page and a `javascript:` link would run script there. A push without them clears them, and
`/api/portal/branding` returns `""` for each, which the pages leave out.

## Conventions

- `clock.py` is the only place that decides what "now" means, so tests can move it
  (`clock.shift(minutes=20)`). Every timestamp written is UTC ISO-8601 with a trailing `Z`.
- `db.audit()` records **vol_ids and actions, never addresses** — a log full of plaintext
  addresses would recreate exactly what this design avoids. There is a test asserting it.
- `db.next_rev()` must be called inside the same transaction as the mutation it stamps, or a
  pull can see a row whose rev implies it was already sent.
- **SQL is always a complete literal** with `?` parameters, as in the scheduler (root
  `CLAUDE.md` → Linting & docs). `routers/sync.py` keeps its per-table statements in
  `SNAPSHOT_TABLES` / `_REKEY_SQL`, and the volunteers upsert is one static statement with a
  "was it sent" flag per `PUSH_COLUMNS` entry. Adding a pushed column means changing
  `PUSH_COLUMNS` and `_UPSERT_VOLUNTEER_SQL` together; `test_portal_local.py` checks they
  match.
- `MAIL_RELAY_URL` must be `http://` or `https://` (`config.py`), because `mailer.py` hands it
  to `urlopen`, which also opens `file://`.

## Frontend

There is no `portal-frontend/`. The volunteer pages are routes in `frontend/` under `/apply/`
(see `frontend/CLAUDE.md` → Routing), and nginx there proxies `/api/portal/*` here. Magic links
therefore point at `{PORTAL_PUBLIC_URL}/apply/verify?t=…`, and `PORTAL_PUBLIC_URL` is the
**site's** origin — deployed, the public hostname, whose nginx server serves only `/`,
`/apply/` and `/api/portal/`. `GET /api/portal/verify` still renders its own confirm page
as a fallback.

## Editable content (`content.py`, schema v7)

The landing page, the questionnaire and the early-access invite email are the organizers'
(scheduler: Volunteer Portal → Landing page / Questionnaire / Invite email), pushed to
`POST /api/sync/content` and kept as checked JSON in `portal_content`. A `NULL` part uses
`content.py`'s default, which is what the site said before this was editable. The heartbeat
reports `content_hash` (`""` until the first push).

**Every push is checked here again** (`content.validate_content`), however the scheduler
checked it. `backend/portal_content.py` is a character-for-character copy of `content.py`
below its docstring, for the editors' checks and previews, and `backend/test_export.py`
fails if they differ. **Edit both together.**

- **Templates are sandboxed Jinja, cut down further**: `_check_template` walks the parse tree
  and allows only variables from a fixed list, `{% if %}`/`{% else %}`, `==`/`!=`,
  `and`/`or`/`not`, `~` and the filters in `ALLOWED_FILTERS`. No `for`, `set`, macros,
  includes, calls, attribute or item access: a template can't reach anything it wasn't handed
  or spin the portal. Don't widen this without thinking about both of those.
- **Markdown** (`render_markdown`): raw HTML off, images off, and `validateLink` allows only
  `http(s)`, `mailto:`, `#` and this site's own paths. `GET /api/portal/landing` renders on
  each request (its placeholders come from the branding) and the page inserts the HTML. The
  opportunity cards are data, escaped by Vue; icons are from `ROLE_ICONS` only.
- **The questionnaire's shape** is `validate_fields`: five types, keys `^[a-z][a-z0-9_]*$`
  unique across questions and follow-ups, an acknowledgement always required, a checkbox
  never, and `flexible`, if present, a checkbox (the scheduler's picker reads it).
- **The invite** (`kind: signup_invite` in `send_admin_mail`) renders the stored template with
  `content.render_invite`; its body must use `{{ link }}`. The admin invite and reset mails
  are still the fixed files in `templates/`.

## Signup (`routers/signup.py`, `signup_fields.py`)

Email first: `signup/check_email` → the mailed link → a **signup session** (`vol_id IS NULL`)
→ `signup/check_name` and `signup/submit`, which creates a `p-<8 hex>` volunteer with
`status = 'pending'` and upgrades the session in place. The organizer approves or declines in
the scheduler (Pending Volunteers), and that reaches the portal as a volunteers push.

- **Open only when all three hold** (`signup_state`): `portal_signup_open = '1'` (pushed from
  the scheduler's switch), an `email_pubkey` (every address is encrypted to it at
  check_email), and at least one pair of timeslots with `remaining > 0` that don't overlap.
  Otherwise `closed` or `full`.
- **Early-access invites** (schema v5) get one person past `closed`, never past `full` (that's
  `_shifts_full`, checked separately for this reason). The scheduler makes them and pushes
  `{invite_id, token_hash, expires_at, revoked_at}` to `POST /api/sync/signup_invites`, which
  replaces the list but keeps the portal's own `used_at`; the heartbeat reports
  `invites_hash`. The page reads `?invite=` and asks `GET signup/invite?t=` (read-only:
  valid / used / expired / revoked / unknown). `check_email` takes `invite`; a live one rides
  on the login token, then the session (`invite_id` on both). It's **consumed at submit**, in
  the submit transaction by one conditional UPDATE — not at check_email, so a mistyped address
  doesn't burn it, and two addresses on one link race to a single winner. The volunteer row
  keeps `invite_id`, which is how the scheduler learns who used it. An invited signup is still
  `pending`.
- **Check order matters.** "Already signed up" comes before closed or full, so a registered
  person is told to sign in rather than that signup is shut. At submit, *full* is left to the
  per-pick checks: a pick that filled mid-form gets a 409 whose `detail.full` names it, and
  the form reopens the shifts step instead of dead-ending.
- **Picks are re-checked inside `BEGIN IMMEDIATE`**, and `remaining` is decremented there for
  the two prefs (extras don't consume). The scheduler's next push replaces the estimate.
- **`check_name` needs a signup session** and returns only `{possible_match}`. Submit
  recomputes `possible_duplicate_of` on the server; the browser's value is ignored.
- **The questionnaire is organizer-editable** (Editable content, below). `signup_fields.current
  (conn)` is the one in force, served by `signup/fields` and enforced by `validate()` and
  `validate_edit()`, which take it as an argument. Unknown answer keys are dropped at submit;
  on edit, answers to questions no longer asked are kept. Answers are plain JSON here, so
  nothing sensitive belongs in it (no phone numbers, no emergency contacts).
- **Picks are timeslots** (`day_time` keys, `vol_picks`; schema v2 dropped bands). The two
  prefs may not overlap: same day and intersecting `time_str` ranges (`overlap()`; the
  scheduler's `routers/schedule.py` has the same rule). Extras may overlap anything.
  `GET /api/portal/timeslots` lists each with `remaining` and the keys it `overlaps`. A
  snapshot without `remaining` reads as 0, so an out-of-date scheduler closes signup.
- `time_pref` / `add_time` are written as `;`-joined "Saturday 0800-1200" labels, the format the scheduler's
  roster already uses.
- **Volunteer pushes update only the columns they carry.** The scheduler doesn't hold
  first_name / last_name / nickname and leaves them out; an upsert of every column would blank
  what the volunteer typed.

## A volunteer's state: three fields (schema v3)

| Field | Meaning | Set by |
|---|---|---|
| `status` | `pending` until orientation, then `active` (approved) | organizer (pushed) |
| `cancelled_at` | the volunteer cancelled; cleared when they rejoin | volunteer (`me/cancel`, `me/rejoin`) |
| `inactive_at` | an organizer made them inactive; cleared on reinstate | organizer (pushed) |

Separate on purpose: cancelling or going inactive never loses an approval, and because each
column has one owner, neither side's push can undo the other's change. `cancelled_at` isn't
in `push_volunteers`' column list at all, so the scheduler can't set or clear it.
`auth.is_live()` (pending or active, neither cancelled nor inactive) gates editing and
cancelling. Inactive volunteers get no sign-in link and can't rejoin; 'declined' (v1-2) became
pending + inactive.

- **`me/cancel`** sets `cancelled_at` and gives their two places back to `remaining`. Picks
  are kept so rejoin can offer them.
- **`me/rejoin`** takes `update` like `me/update`, with nothing held (every pick needs space
  again, old ones included), and clears `cancelled_at`.

## Changing a registration (`POST /api/portal/me/update`, in `routers/signup.py`)

A signed-in volunteer who `is_live` can change their two shifts, extras, "any time",
and the questions marked `editable` in `signup_fields.py` (T-shirt, dinner, flexible) —
whether or not signup is open. `signup_fields.validate_edit` merges: non-editable answers keep
their stored values whatever the request says.

- Same pick rules as signup (`_parse_picks` / `_check_picks`, shared): exactly two, no
  overlap, a new pick needs `remaining > 0`. A shift already **held** can be kept even when it
  reads as full; their place is counted in it.
- Local `remaining`: +1 for a dropped pick, −1 for a new one, until the next push.
- **Dropping a shift they're assigned to is allowed.** `me` returns `scheduled` (from
  `snapshot_slots`) so the page can warn; the scheduler flags it on its next pull.
- `GET /api/portal/me` also returns `answers` and `editable`.

## Not built yet

Steps 6-7 of `plans/vol_signup.md` §17 (assigned shifts now show on `/apply/me` via `me.scheduled`): the
HMAC backfill of the existing roster, and the remaining settings. The mailer was built, but
not here: it's `tools/vol_mail/` at the repo root, fed by the **scheduler's** export API
rather than by this service, so nothing in the portal changed and it still sends only magic
links and admin mail. Volunteers added by hand in the scheduler now get an `email_hmac` there
(`backend/CLAUDE.md`). **Merge** of a possible
duplicate into the existing record (plans/vol_signup.md §11) is also not built: Pending Volunteers
offers approve-as-separate or decline.
