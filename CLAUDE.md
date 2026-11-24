# Volunteer Manager

## Project layout

```
BSides/
├── backend/          ← FastAPI backend (active development)
│   ├── main.py       ← All API endpoints, CLI, DB init
│   ├── scheduler.sql ← SQLite schema (user_version = 1)
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/         ← Vue 3 + Vite + PrimeVue frontend (active development)
│   ├── src/
│   │   ├── App.vue   ← <RouterView/> shell
│   │   ├── router.js ← / (landing), /apply/* (volunteers), /admin (organizer app)
│   │   ├── main.js   ← PrimeVue setup, Noir theme, router
│   │   ├── views/    ← Landing.vue, Admin.vue, apply/
│   │   └── assets/
│   ├── vite.config.js
│   └── Dockerfile
├── tools/vol_mail/   ← Standalone schedule mailer, run on an organizer's machine (no container)
├── tools/gather-screenshots/ ← Retakes the README screenshots on a throwaway demo stack (bsides-demo, ports 18080/18082)
├── docs/             ← MkDocs site: getting started, organizer guide, operations, services, export API, diagrams, quality & security, code reference
├── plans/            ← Working plans (gitignored, never synced)
├── compose.yml       ← Builds both services, backend_data volume
├── compose.maintenance.yml, maintenance/, maintenance.sh ← "back soon" page while the stack is down
├── logs/audit.jsonl  ← Bind-mounted audit log, one JSON object per line (backend/audit.py)
└── Scheduler/        ← Original Flask app (reference only, not active)
```

## Running the stack

```bash
docker compose up --build          # full site on http://localhost:8080 (FRONTEND_PORT), portal-only on :8082 (PORTAL_SITE_PORT)
docker compose up backend          # backend + postgres only, on :5000
```

**Two hostnames, one way in.** The deployment is reached only through a tunnel whose client
runs on the host (not in compose) and dials the frontend's two loopback-only publishes. The
hostnames themselves live in `.env`, never in the repo:

| Hostname | Host port → nginx | Serves | Gate |
|---|---|---|---|
| public (`PORTAL_PUBLIC_URL`) | `PORTAL_SITE_PORT` → 8082 | `/`, `/apply/*`, assets, `/api/portal/*`; everything else 404 | public |
| admin (`ADMIN_PUBLIC_URL`) | `FRONTEND_PORT` → 8080 | the full site, `/admin` and `/api/*` | an access gate at the edge, with a bypass for `/api/export/*` (bearer keys) |

`ADMIN_PUBLIC_URL` is the passkey relying party and the base of invite and reset links; unset,
it falls back to `PORTAL_PUBLIC_URL` (one origin, local dev). nginx takes the client IP from
the edge's client-address header (`frontend/default.conf`), so the portal's per-IP rate limits
see the real client. Nothing else publishes a host port — the portal's 5001 is reached over
the compose network only.

The deployment runs under **podman-compose**, which turns each `depends_on` into a hard
`podman --requires` link — a required container can't be removed while a dependent exists,
and `down` runs its stops one at a time. So only `backend → postgres` keeps a `depends_on`;
frontend goes without and relies on per-request resolution instead (see `compose.yml`). Any
single service can be recreated on its own, e.g.
`podman-compose up -d --build --force-recreate frontend`. For backend add `--no-deps`, or
podman-compose pulls postgres into the recreate and restarts the database too. Don't add
`depends_on` back without checking that.

podman still runs the stops of one `down` one after another, so total stop time is the sum of
each container's. That is why frontend stops with `SIGTERM` (Dockerfile `STOPSIGNAL`) and
uvicorn has `timeout_graceful_shutdown=3`: open `/api/events` SSE streams otherwise held each
of them to podman's 10s SIGKILL.

**Maintenance page.** `./maintenance.sh on` brings the stack down and starts
`compose.maintenance.yml`, a separate compose project (`name: bsides-maintenance`) whose one
nginx publishes the same two loopback ports, so the tunnel needs no change; `off` reverses
it, and `status` shows both. Build in between. Every page is a 503 with `Retry-After` and
`no-store`, `/api/*` a JSON 503 the apps can show. Everything it serves (page, crab logo,
OFL fonts) is in `maintenance/`, since nothing else is running. The two stacks can't run at
once: they want the same ports.

**Images.** Every image is Alpine: the three Python services are `python:3.14-alpine`
(keep them on one line), frontend is `node:24-alpine` to build and `nginx:alpine` to serve,
and postgres `18-alpine`. Tags name a line, never an exact patch: Docker Hub stops rebuilding
a patch tag once the next patch ships, which froze `3.14.3` on a February base with dozens of
OpenSSL and zlib CVEs. Each app image also runs `apk upgrade --no-cache`, and the Python ones
uninstall pip once their packages are in (installed as root, so the app can't change them).
After rebuilding, run `tools/sbom.sh` (docs/security). None
installs build tools or curl: every Python dependency ships a musllinux wheel, and healthchecks are a one-line `urllib` call. Backend and
portal run as uid 1001 (`appuser`; both mount `./logs` with `:U`, so they must match),
mailrelay as 10001 (its volume's owner). Adding a dependency without a musllinux wheel would
need `gcc`/`musl-dev` back; check with `pip download --only-binary=:all: --platform
musllinux_1_2_x86_64 --python-version 3.14`.

`backend/main.py` takes only `-d` (debug logging) and `--audit <path>`. The `--add_user` /
`--export_csv` / `--add_slot` CLI flags documented for the old app exist only in
`Scheduler/restAPI.py`.

Frontend dev server (proxies `/api/portal` to `http://portal:5001` and the rest of `/api` to
`http://backend:5000`):

```bash
cd frontend && npm install && npm run dev
```

## Linting & docs

```bash
python3 -m pip install -r requirements-dev.txt   # once, in a venv; includes backend/requirements.txt
./run_lint.sh              # pylint per app, bandit, mkdocs build --strict
./run_lint.sh --no-docs    # also --no-pylint, --no-bandit; --todo lists TODO/FIXME
mkdocs serve               # docs at http://127.0.0.1:8000
```

Dev-only: none of it goes into an image, and `.rsync-filter` keeps it off the server. The
full write-up, with the ratchet rules, accepted exceptions, security design and known gaps,
is `docs/development/quality-and-security.md`. Update its tables when you change a ratchet
or add a suppression.

**pylint (`.pylintrc`) has three tiers.**
1. **Security, blocking.** `fail-on` lists them: every E/F error, plus `eval`/`exec`,
   bare and broad `except`, `raise ... from` missing, mutable defaults, `subprocess` without
   `check`, `open()` without `encoding`, resources outside `with`, `global`,
   `pickle`/`marshal`/`shelve`, and `dynamic-sql` (below). Any one fails the run, in code or
   tests. A deliberate broad `except` is marked on its line with
   `# pylint: disable=broad-exception-caught` and the reason in a comment above it.
2. **Quality and readability.** Defaults plus most extensions (complexity, docstring/param
   agreement, magic values, modern typing, ...). Reported; counts toward the score.
3. **Ratchet.** `run_lint.sh` holds each app's score at or above its number in `APPS`. Raise
   those as you clean up; don't lower them to land a change.

Like the tests, pylint runs **once per app**: backend/ and portal/ both have `db` and `main`.
Test files get a few pytest-shaped disables (fixtures, `protected-access`, docstrings), listed
in `run_lint.sh`. Run pylint from `BSides/` or pass `--rcfile=BSides/.pylintrc`: inside an
app directory pylint doesn't find the config.

**SQL is always a complete literal; values are bound parameters.** No f-strings, `+`, `%` or
`.format()` building SQL text, even from constants. Table and column names can't be
parameters, so a choice between tables is a dict or tuple of whole literal statements, an
optional clause is `CASE WHEN ? THEN … END` or `(? OR …)` with a bound flag, and a variable
list is `= ANY(?::text[])` with a Python list (Postgres). Shared rules belong in the schema, as
the v17 who-counts columns do, not in a pasted fragment. `lint/pylint_sql.py` (`dynamic-sql`,
blocking, with its own tests in `lint/`) enforces this; Bandit's B608 is the second net.

**Module state lives in a small dataclass instance** (`_state`, `_clock`, `_config`), never in
a `global` statement. Tests reset it through the instance or a helper such as
`db.close_pool()`.

**bandit (`bandit.yaml`)** fails at medium severity and medium confidence. Tests skip only B101
(`assert`). There are no global skips: accept a finding where it is, with the reason,
`# nosec B608 — table name is a literal`.

**Docstrings are Google style** (`Args:` / `Returns:` / `Raises:`). mkdocstrings renders them
into the code reference (`docs/gen_ref_pages.py`, one page per module), and pylint's
`docparams` holds a documented parameter list to the real signature.

**The export API guide is half generated.** `docs/api/export.md` is hand-written prose with
`<!-- export-api: PATH -->` markers. `docs/gen_export_api.py` (an MkDocs hook) fills each one from
the router's OpenAPI: scope, `Query` descriptions, response-model fields and examples. Each
build also writes `site/export-api.md`, one standalone file to send to an integrator, or run
`python docs/gen_export_api.py > export-api.md`.

**The rest of the site is hand-written** for people, not agents, and is the public
counterpart of these CLAUDE.md files: `getting-started.md`, `guides/organizer.md` (the admin
site, for organizers), `operations/` (deployment, every `.env` setting, secrets and keys, Graph
mail setup) and `services/` (one overview per service). When a change alters a setting, a
deploy step, a secret or something an organizer sees, update the matching page as well as
the CLAUDE.md. `.rsync-filter` keeps `docs/` off the server.

**Releases publish the docs to GitHub Pages with mike**, one folder per `MAJOR.MINOR`
(`/1.0/`, `/latest/`), from the tagged checkout: `mike deploy --push --update-aliases 1.0
latest`. It commits to and pushes `gh-pages`, so it's the user's to run, like any push. Steps
in `docs/operations/releasing.md`.

**The Security pages are generated** by `docs/gen_security.py` (an MkDocs hook). Bandit runs
on every build, over the same files and settings as `run_lint.sh`. The SBOM and vulnerability
tables come from `docs/security/sbom/*.cdx.json`, which `tools/sbom.sh` writes with trivy
after `podman-compose build`. Those files are committed, and each page shows its "as of" date.

**Python dependencies are pinned to the current major version**: `>=` the release tested,
`<` the next major (`<1` for a 0.x package). Raise a floor when you test a newer release;
move a ceiling only after reading that major's changelog. Check the musllinux wheels with both
`--platform musllinux_1_2_x86_64 --platform musllinux_1_1_x86_64`: some packages, such as
`watchfiles`, ship only `musllinux_1_1`.

## Backend — FastAPI (`backend/main.py`)

### Auth and sessions
- Organizers sign in with a password **or a passkey** (WebAuthn, relying party =
  `PORTAL_PUBLIC_URL`'s hostname). See `backend/CLAUDE.md` → Passkeys.
- New admins can be **invited** (a 72-hour single-use link) instead of created, and a
  forgotten password is **reset by email** to `users.email`; both mails go out through the
  portal. See `backend/CLAUDE.md` → Invites and password reset.
- Cookie-based sessions via `starlette.middleware.sessions.SessionMiddleware`.
- Secret key stored in `app_kv` table under key `app_secret`; generated on first run.
- `_session_cache: dict` is a module-level cache keyed by `session_id` — avoids a DB hit on every authenticated request. Cleared on logout.
- `get_session_data(request, db)` — Depends, returns `{"user_id", "username", "role"}` or `None`.
- `RoleChecker(*roles)` — callable class used as a Depends; raises 401/403. Example: `Depends(RoleChecker("organizer"))`.

### Audit log and alerts
- Every write, sign-in and refusal is an `audit("event.code", …)` call (`backend/audit.py`):
  a JSON line in `logs/audit.jsonl` and a row in `audit_events`. Codes come from
  `audit.EVENTS`, which a test enforces.
- `backend/alerts.py` runs rules over those events and emails alerts through the portal
  (`send_admin_mail` kind `alert`) to organizers with `users.audit_alerts` and an email.
  See `backend/CLAUDE.md` → Security alerts; the plan is `plans/audit_alerting.md`.
- **Admin → Audit Log** (a dialog; `/admin/audit`, where alert emails link, opens it) is for
  every organizer. Who gets the alert **emails** is separate: `users.audit_alerts`, which any
  organizer ticks for any organizer in Admin → Users. It may be nobody (alerts are then only
  recorded, and both dialogs warn).
- Failed password sign-ins are rate limited (`backend/CLAUDE.md` → Session and auth).
- `security_signals.py` (the **same file** in `backend/` and `portal/`; a test compares them)
  watches every request for attack patterns, probing and malformed input, and refuses only a
  NUL. Portal findings reach the backend's audit log over the sync API.
- The volunteer pages check input by the portal's own rules before sending
  (`frontend/src/lib/portal_rules.js` ↔ `portal/validation.py`, one shared case list in
  `portal/test_data/validation_cases.json`). Input breaking them is a `portal.anomaly`:
  alerted on, and refused when `PORTAL_STRICT=enforce`.

### Roles
| Role | Can do |
|------|--------|
| `organizer` | All write operations |
| `checkin` | Check in volunteers, mark No-Show only |
| `view` | Read-only (GET endpoints) |

### DB
- **PostgreSQL 18** (compose service `postgres`, image `postgres:18-alpine`, volume `pgdata18`;
  the old `pgdata` holds the pre-upgrade 16 data for rollback), via a pooled `psycopg2`
  connection. `PgClient` in `db.py` is a sqlite3-shaped shim that rewrites `?` to `%s` — that's
  why queries still use `?` placeholders.
- `RealDictCursor` makes every row a `dict` — use `row["col"]` not `row[0]`.
- `get_db()` is a generator Depends; FastAPI caches it per-request so `get_session_data` and the endpoint share the same connection.
- Schema lives in `scheduler.sql`, the whole current schema for fresh installs. After the 1.0
  baseline, each release that changes it adds **one** file, `migrations/<release>.sql`, listed
  in `SCHEMA_RELEASES` in `db.py`; the portal does the same. See `backend/CLAUDE.md` → DB
  conventions, and `tools/schema_check.sh`.

### First-run setup flow
On a fresh DB (`user_version != 1`), only the schema is applied. No data is seeded. The entire initial dataset comes from the setup wizard:

1. **Create admin** — first organizer account (no auth required; locked out once any user exists)
2. **Set up the schedule** — either build one in the wizard (days → categories → times →
   a counts grid, applied in one `/api/schedule/build` call) or upload a CSV; one row per
   position-at-timeslot (organizer auth)
3. **Import volunteers** — CSV of the volunteer roster (organizer auth)

`schedule_config`, `_init_db`, `vol_list`, and `_add_vols` have all been removed.

Setup endpoints:
- `GET /api/setup_status` — returns `has_users`, `has_schedule`, `has_volunteers`, `authenticated`, `username`, `role`
- `POST /api/setup/create_admin` — rejected with 403 if any user exists; enforces 8-char password minimum
- `POST /api/setup/import_schedule` — multipart CSV; required columns: `day_key, day_str, time_key, time_str, pos_key, pos_cat, pos_str`; optional column: `date` (ISO `YYYY-MM-DD`, stored as `event_date` in `days_table`); one row per schedule slot; `ON CONFLICT DO NOTHING`, so safe to re-run; a bad row is skipped and reported, the rest are kept (savepoint per row)
- `POST /api/setup/import_volunteers` — multipart CSV; columns: `vol_id, first_name, last_name, nickname, time_pref, add_time`; re-import updates name/time_pref/add_time in place (`ON CONFLICT DO UPDATE`)

Frontend wizard state machine: `loading → create_admin → login → import_schedule → import_volunteers → app`. Each step re-calls `check_status` to advance; "Skip for now" bypasses any import step. The `import_schedule` view renders `setup/ScheduleSetup.vue`, which offers the builder (`setup/BuildSchedule.vue`) or the CSV import.

### API endpoints (all under `/api/`)

| Method | Path | Auth |
|--------|------|------|
| GET | `health` | none |
| GET | `setup_status` | none |
| POST | `setup/create_admin` | none (403 if users exist) |
| POST | `setup/import_schedule` | organizer |
| POST | `setup/import_volunteers` | organizer |
| POST | `login` | none |
| GET | `logout` | any |
| GET | `get_schedule` | any |
| GET | `get_vols` | any |
| POST | `get_avail_vols` | any |
| POST | `get_unassigned_timeslots` | any |
| POST | `assign_vol` | organizer |
| POST | `clear_timeslot` | organizer |
| POST | `add_vol` | organizer |
| POST | `get_vol_rating` | any |
| POST | `rate_volunteer` | organizer, checkin |
| POST | `get_vol_notes` | organizer |
| POST | `add_vol_note` | organizer |
| POST | `delete_vol_note` | organizer |
| POST | `get_schedule_note` | any |
| POST | `set_schedule_note` | organizer |
| POST | `drop_vol` | organizer |
| POST | `checkin_vol` (`force` to check in early/late) | organizer, checkin |
| POST | `checkin/scan` (`code`, `force`) | organizer, checkin |
| POST | `checkin/rotate_key` | organizer |
| POST | `app_settings/event_tz` | organizer |
| GET  | `export/schedule`, `export/volunteers` | **API key** (Bearer), see `docs/api/export.md` |
| GET/POST | `api_tokens`, `api_tokens/revoke` | organizer |
| GET/POST | `email_template`, `/assets`, `/assets/site_logo`, `/assets/delete`, `/preview`; GET `/recipients`, `/package` | organizer |
| POST | `admin/volunteers/email_hmac` | organizer |
| POST | `reset_vol_status` | organizer, checkin |
| POST | `vol_notes_report` | organizer |
| POST | `random_volunteers` | any |
| GET | `schedule/structure` | any |
| POST | `schedule/upsert_day`, `delete_day` | organizer |
| POST | `schedule/upsert_time`, `delete_time` | organizer |
| POST | `schedule/set_day_time` | organizer |
| POST | `schedule/upsert_position`, `delete_position` | organizer |
| POST | `schedule/apply_grid` | organizer |
| POST | `schedule/add_slot`, `remove_slot`, `bulk_add_slots` | organizer |
| POST | `schedule/build` | organizer |
| POST | `schedule/upsert_category`, `delete_category`, `reorder_categories` | organizer |
| GET | `radios` | any |
| GET | `radios/history` | any |
| POST | `radios/checkout` | organizer, checkin |
| POST | `radios/checkin` | organizer, checkin |
| GET | `admin/portal/status`, `admin/portal/pending` | organizer |
| GET | `admin/volunteers` | organizer |
| GET | `admin/shift_flags` | organizer |
| POST | `admin/shift_flags/keep` | organizer |
| POST | `admin/volunteers/set_inactive` | organizer |
| POST | `admin/volunteers/purge` (`vol_ids`, `confirm`; inactive only, here and on the portal) | organizer |
| POST | `users/change_password` (own; needs current password) | any |
| GET  | `users/me` | any (own) |
| POST | `users/change_email` (own; needs current password) | any |
| POST | `users/set_email` | organizer |
| POST | `invites`, `invites/revoke`; GET `invites` | organizer |
| GET  | `invites/check` | none (read-only) |
| POST | `invites/accept` | none (needs the link) |
| POST | `password_reset/request` | none (same reply always; rate limited) |
| GET  | `password_reset/check` | none (read-only) |
| POST | `password_reset/complete` | none (needs the link) |
| POST | `admin/portal/sync_now`, `approve`, `decline`, `settings` | organizer |
| GET/POST | `admin/portal/invites`, `admin/portal/invites/revoke`, `admin/portal/invites/send` (early-access signup links; send emails one, 10 s apart) | organizer |
| GET/POST | `admin/portal/content` (`part`: `landing`, `questionnaire`, `invite`; no `value` resets it), `admin/portal/content/preview` | organizer |
| GET  | `passkeys/config` | none |
| POST | `passkeys/login/options`, `passkeys/login/verify` | none |
| POST | `passkeys/register/options` (needs password), `passkeys/register/verify` | any |
| GET  | `passkeys` | any (own) |
| POST | `passkeys/rename`, `passkeys/delete` | any (own) |
| POST | `passkeys/clear_user` | organizer |
| GET  | `admin/audit` (filters, `before_id` paging), `admin/audit/status` | organizer |
| POST | `admin/audit/test_alert` (one a minute) | organizer |
| POST | `users/set_audit_alerts` (`user_id`, `enabled`; organizers only) | organizer |

### Schedule structure

`day_times` declares which times each day runs — one day may have an evening shift the others
don't — and is kept a superset of the pairs used in `schedule`. All slot creation goes through
`_day_time_pairs()` and `_fill_slots()` in `routers/schedule.py`, so "all times" means "all times
that day actually runs".

- `upsert_day` / `upsert_time` / `upsert_position` take `fill_all` to populate slots on creation.
- `bulk_add_slots` takes `cat`, `prefix`, `label`, `start`, `count`, `day_keys`, `time_keys` and
  creates `{prefix}{n}` positions labelled `{label} {n}`.
- `schedule/build` takes a whole schedule as one JSON `config` form field and writes it in a
  single commit — see `backend/CLAUDE.md`. Its per-cell counts map is what `bulk_add_slots`'
  day × time cross-product cannot express (ten registration desks at 0800, four at 1500).
- `schedule/apply_grid` is the same idea against a live schedule: it diffs the editor's counts
  grid against what exists, adding and removing slots. It never removes a slot with a volunteer
  assigned — those come back in `blocked` instead.
- `pos_key` must match `^[a-z0-9][a-z0-9-]*$` — underscores would break the
  `pos_key.split("_")[0:3]` parsing in `assign_vol`. `day_key` and `time_key` follow the same
  rule for the same reason.
- Each category carries its own chip color (`categories.cat_color`, `#rrggbb`), set on the
  Categories tab of the editor or in the builder. Only the background is stored; the text
  color is derived from it in `frontend/src/lib/colors.js`. Before this, four hardcoded CSS
  rules painted the four original category names and anything else rendered unstyled.
- A `time_key` identifies a **range**, not just a start. `time_str` lives on the global
  `times_table` row, so Saturday 1500-1900 and Sunday 1500-1700 must be `1500` and `1500-1700`
  — sharing `1500` would relabel Saturday's shift. Keys are derived by
  `frontend/src/components/schedule-editor/time_keys.js`; `upsert_time`'s `create` flag rejects
  a collision server-side.

### Radio tracking

`radios` (barcode → first seen) plus `radio_checkouts` (one row per issue, closed by
`checked_in_at`). Radios auto-register on first checkout — no inventory step. A partial unique
index enforces one open checkout per radio. Checking out a radio that is already out hands it
over — the open checkout closes and a new one opens in the same request, reported as
`transferred_from` — while re-scanning to the current holder changes nothing (`already_held`).
Check-in takes the barcode alone. Timestamps default to `NOW() AT TIME ZONE 'UTC'` and are served with a `Z` suffix so
elapsed times are right in any timezone. See `backend/CLAUDE.md`.

Frontend: the **Radios tab**, with Check Out / Check In at the top for the radio roles and the
out / available / log lists below (`frontend/CLAUDE.md` → Radios). Both scanners are backed by
`BarcodeScanner.vue`, which lazy-loads ZXing and always offers manual entry for phones whose
camera is unavailable.

### Volunteer check-in and schedule emails

Each assigned shift has an HMAC check-in code, mailed as a QR code and scanned from the
Schedule tab's **Check In Volunteer**. More than 45 min early or 10 min late asks first
(**Check in anyway**); the slot dialog's Check In does the same. Shift times are read in the
event timezone (App Settings). See `backend/CLAUDE.md` → Shift check-in.

Schedule emails are **not sent by the site**. Organizers write the template on the site (Call
for Volunteers → Schedule Email) and download a package; `tools/vol_mail/` (on an organizer's
machine, in no container) pulls volunteers through the read-only **export API**, decrypts
addresses with `private.pem`, and sends through Microsoft Graph. Usually a draft round, then
a final. See `tools/vol_mail/README.md`.

The export API is also for other servers: bearer keys made in Admin → API Keys, each with
scopes, an expiry and an optional IP allow-list. `docs/api/export.md` is written for
whoever calls it.

### Special note conventions in `vol_notes`
- Check-in: `[CheckIn] | {day_key}_{time_key}_{pos_key}`
- Check-in confirmed early or late: `[CheckInTiming] {Early|Late} {N} min | {slot_key}` (shown in reports)
- Ratings: `[Rating] {Good|Bad|No-Show} | {slot_key}`
- These are filtered out of the volunteer notes report via `NOT LIKE '[Rating]%'` / `NOT LIKE '[CheckIn]%'`.

## Frontend — Vue 3 (`frontend/`)

- **PrimeVue 4** components throughout. Import from `primevue/<component>`.
- **Noir theme** for `/admin*`: custom `definePreset(Aura, {...})` applied in `main.js`; dark
  mode via `.dark-app` class on `<html>`. The volunteer pages (`/`, `/apply/*`) instead wear
  bsidescharm.org's light theme (`bsc-portal`); see `frontend/CLAUDE.md` → Volunteer pages theme.
- One build, three areas: `/` is the public call-for-volunteers landing page, `/apply/` is the
  volunteer portal (talks to `/api/portal/*` only), and `/admin` is the organizer app. The
  public hostname serves the first two; the admin hostname sends `/` straight to `/admin`.
  See `frontend/CLAUDE.md` → Routing.
- All API calls use `fetch('/api/...')` — **absolute**, because pages live at nested paths —
  with `FormData` bodies (no JSON request bodies).
- Auth state: `bsc_username` and `bsc_role` stored in `localStorage`. `bsc_schedule` stores the fetched schedule JSON.
- Role is passed as a `:role` prop down to components that gate write operations.

### Component conventions
- Dialog visibility is managed inside the component, not the parent.
  - Parent triggers via prop change (component `watch`es the prop) — e.g. `TimeslotDialog` watches `:pos`.
  - Or via an exposed method — e.g. `VolNotesReport.open()` via `useTemplateRef`.
- Never mutate props; spread-copy into a local `ref` (`local_pos = { ...pos }`).
- Emits use kebab-case: `schedule-updated`, `vol-dropped`.

### Key components (in `Scheduler/src/components/` — migrate to `frontend/src/components/`)
- `Schedule.vue` — day/time tabs + accordion, empty-slots dialog, raffle dialog
- `TimeslotDialog.vue` — assign/clear volunteer, check-in, ratings, slot notes
- `Volunteers.vue` — volunteer list with search/filter
- `VolunteerDialog.vue` — volunteer detail, schedule management, notes (organizer only)
- `VolNotesReport.vue` — full notes report table with sort
- `Timeslot.vue` — single slot display; glows green (checked-in) or pulses red (past + unchecked)

## Scheduler/ (reference only)

The original Flask app. Do not add features here. Use it as a reference for:
- The complete list of volunteers in `vol_list`
- The `schedule_config` structure
- Any business logic not yet migrated to `backend/`

## Things to know

- `_query_vols` lists **active** volunteers only (pending portal signups wait in Admin → Pending Volunteers) and handles any number of `;`-separated `requested` entries.
- `assign_vol` and `clear_timeslot` both return the full updated schedule (`_build_schedule`), so the frontend can update state without a separate fetch.
- The `add_slot` CLI is the only way to add new schedule positions; there is no UI for it.
- Back-to-back shift filtering: the `1130` slot is treated as adjacent to both `0800` and `1500`/`1300`.
