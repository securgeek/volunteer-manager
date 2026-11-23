# Backend: the scheduler API

The backend is the organizers' system of record: the schedule, the volunteer roster,
notes, ratings, check-ins, radios, organizer accounts and export API keys. It's a FastAPI
app on PostgreSQL 18, and it's reachable only through the admin hostname, behind an
access gate at the edge.

Code: `backend/`. Reference: [backend.main](../reference/backend/main.md) and the modules
beside it.

## Layout

| Module | What it does |
|---|---|
| [`main`](../reference/backend/main.md) | The app: routers, session middleware, and startup (SSE loop, portal sync). |
| [`db`](../reference/backend/db.md) | The connection pool, `PgClient`, migrations, `get_session_data` and `RoleChecker`. Imports nothing from the app. |
| [`portal_sync`](../reference/backend/portal_sync.md) | The sync loop that keeps the portal up to date, and pulls signups back. |
| [`checkin_codes`](../reference/backend/checkin_codes.md) | Shift check-in codes (HMAC) and the early/late rule. |
| [`api_auth`](../reference/backend/api_auth.md) | Export API keys: scopes, IP allow-lists, the failure limiter. |
| [`email_render`](../reference/backend/email_render.md) | The schedule email: Markdown to HTML and text, placeholders, QR codes, the package zip. |
| [`names`](../reference/backend/names.md) | Splitting a stored name, and the export API's name format. |
| `routers/` | One module per area of the API. |

## Data

The schema is `scheduler.sql`, the whole current schema, for a fresh database. 1.0 is the
baseline. Each later release that changes the schema adds one file, `migrations/<release>.sql`,
which an existing database gets once, in order, when the backend starts. The list is
`SCHEMA_RELEASES` in `db.py`. See [Releasing](../operations/releasing.md#schema-changes).

The main tables:

- **Schedule.** `days_table`, `times_table`, `positions` and `categories` define the
  structure. `day_times` says which times each day runs. `schedule` has one row per slot (a
  position at a timeslot), holding the assigned `vol_id`.
- **Volunteers.** `volunteers` (name, status, encrypted email, picks and answers from the
  portal), `vol_picks` (timeslot preferences), `vol_notes` (notes, check-ins and ratings)
  and `shift_flags`.
- **Radios.** `radios` and `radio_checkouts`.
- **Accounts.** `users`, `sessions`, `user_passkeys`, `user_invites`, `password_resets` and
  `api_tokens`.
- **Settings.** `app_kv`: the site title and logos, the event timezone, the email public
  key, the check-in key and the cookie secret.

Every query is a complete literal with `?` placeholders, which `PgClient` rewrites for
psycopg2. Rows come back as dicts.

## A volunteer's state

Three fields, each with one owner:

| Field | Owner | Values |
|---|---|---|
| `status` | organizers | `pending` (signed up), then `active` (approved, i.e. through orientation) |
| `cancelled_at` | the volunteer, through the portal | set when they cancel, cleared when they rejoin |
| `inactive_at` | organizers | **Make inactive** and **Reinstate** |

Three generated columns turn those into the rules everything else uses: `is_assignable`
(approved, and neither cancelled nor inactive) for the assignment pickers, `is_counted` for
capacity and demand, and `is_live` for the pending queue. A slot whose volunteer is no
longer live **needs a replacement**, and the Schedule tab's banner lists it.

## Organizer accounts

Three roles:

| Role | Can do |
|---|---|
| `organizer` | everything |
| `checkin` | check volunteers in, rate them, issue and return radios |
| `view` | read only |

Organizers sign in with a password (Argon2) or a passkey. New organizers can be invited with
a 72-hour single-use link, and a forgotten password can be reset by email. Both kinds of mail
go out through the portal, because the backend has no mail path of its own.

## Sync with the portal

The backend is always the client. Every `PORTAL_SYNC_INTERVAL` seconds, and shortly after
any change, one pass runs in this order:

1. **Heartbeat.** Checks the portal is up and that both sides hold the same keys.
2. **Pull volunteers** changed since the last cursor, with their picks.
3. **Push** pending decisions (approvals, inactive, re-keys), then any settings that
   differ (signup open, the email public key).
4. **Push** the schedule snapshot, the branding, and the early-access invite list.

Each push replaces the portal's copy wholesale, and a `content_hash` makes an unchanged
push free. Each volunteer column has one owner, so a push can't overwrite an edit the
volunteer just made in the portal. A decision waits in `portal_dirty` until the portal
accepts it. [Interaction diagrams](../architecture/interactions.md) shows the whole pass.

**Volunteer Portal** in the admin site shows the last pass, any error, and whether the keys
match.

## Purging volunteers

**Purge** deletes inactive volunteers outright, here and on the portal. It's for clearing
test volunteers, so their addresses can sign up again from scratch. The portal goes first.
If it can't be reached, nothing is deleted. Picks, notes, check-ins and ratings go with the
volunteer, their slots become empty, and radio history keeps the name.

## Logs

`logs/audit.jsonl` records every write, sign-in, sign-out and refusal, one JSON object per
line: the time in UTC, an event code such as `auth.login.fail`, its severity, who did it, the
client address, the request path, a message, and structured details. Read it with `jq`:

```bash
jq -c 'select(.event == "auth.login.fail")' logs/audit.jsonl          # failed sign-ins
jq -c 'select(.severity == "high" or .severity == "medium")' logs/audit.jsonl
jq -r '.ip' logs/audit.jsonl | sort | uniq -c | sort -rn | head        # busiest addresses
```

Each event is also a row in the `audit_events` table, which the admin site's Audit Log reads.
The table keeps `AUDIT_RETENTION_DAYS` (365 by default); the file keeps everything.

The list of event codes and their severities is `EVENTS` in `backend/audit.py`.

**Security alerts** (`backend/alerts.py`) watch those events: bursts of failed sign-ins, a
password spray, account and role changes, credential changes made by someone else, a new
email key or export key, refused export calls, and sign-ins or key use from a new network.
A fired alert is recorded as `alert.fired` and emailed, through the portal, to organizers
with audit-log access and an address on their account. Each kind fires at most once an hour
for the same cause, and at most 15 alerts are mailed an hour in all. The old
free-text `logs/audit.log` (before this change) is left as it was and no longer written.

Successful healthchecks are left out of the access log, and a healthy, idle backend logs
nothing. A sync problem is logged once as a warning, and recovery as one
`portal sync healthy` line.
