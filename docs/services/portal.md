# Portal: the volunteer side

The portal is where volunteers sign up, sign in and manage their own registration. It's a
**separate application** from the scheduler: its own SQLite database, its own sessions, and
no shared code. The only link between the two is the signed sync API, and traffic on it
flows one way, from the scheduler to the portal.

Code: `portal/`. Reference: [portal.main](../reference/portal/main.md) and the modules beside it.

## Why it's separate

The portal faces the internet; the scheduler sits behind an access gate. If the portal's
host were fully compromised, the attacker would get volunteer names, email **ciphertext**
and a copy of the schedule. They wouldn't get organizer accounts, notes, ratings, radio
history, the scheduler's cookie secret, or a single readable email address. The portal
can't decrypt addresses, and it has no way to call the scheduler.

## Layout

| Module | What it does |
|---|---|
| [`main`](../reference/portal/main.md) | The app and its startup. Runs one worker, since SQLite has one writer. |
| [`config`](../reference/portal/config.md) | Settings from the environment. Refuses to start without the shared keys. |
| [`db`](../reference/portal/db.md) | SQLite connections, migrations, the revision counter, `portal_kv` and the audit log. |
| [`security`](../reference/portal/security.md) | Email HMAC and encryption, token hashing, request signing. |
| [`ratelimit`](../reference/portal/ratelimit.md) | Per-address and per-client buckets in the database. |
| [`mailer`](../reference/portal/mailer.md) | Sends through the relay, SMTP, or `.eml` files. |
| [`routers/auth`](../reference/portal/routers/auth.md) | Sign-in links, sessions, and `GET /api/portal/me`. |
| [`routers/signup`](../reference/portal/routers/signup.md) | The signup form, and changing, cancelling and rejoining. |
| [`routers/sync`](../reference/portal/routers/sync.md) | The API the scheduler calls. |
| [`routers/branding`](../reference/portal/routers/branding.md) | The title, logos and contact details (email, website, code of conduct) the scheduler pushed. |
| [`signup_fields`](../reference/portal/signup_fields.md) | The questionnaire in force, and checking answers against it. |
| [`content`](../reference/portal/content.md) | The organizer-editable landing page, questionnaire and invite email: defaults, checks, the sandboxed templates and the safe Markdown. |

The volunteer pages themselves are routes in the frontend under `/apply/`, and nginx sends
`/api/portal/*` here.

## Signing in

Volunteers have no passwords. They sign in with a **magic link** mailed to their address:

1. `request_link` mails a single-use link, valid for 15 minutes, to a registered address.
2. The link opens `/apply/verify`, which does nothing until the volunteer presses the button.
   Mail scanners fetch links, so a GET never spends a token.
3. The button's POST spends the token in one conditional UPDATE, so two clicks can't both
   win, and starts a session: 12 hours, or 90 days with **Remember this browser**.

Only SHA-256 hashes of tokens and session ids are stored. A copy of `portal.db` isn't enough
to sign in as anyone.

Link requests are rate-limited per address and per client, where an IPv6 client counts by its
/64. Each request also takes at least 250 ms, so timing doesn't reveal whether an address is
registered.

## What organizers can change

The landing page, the questionnaire and the early-access invite email are edited in the
scheduler's Volunteer Portal dialog and pushed here, where each push is checked again before
anything is stored:

- **Templates** (the landing page's Markdown and the invite) are Jinja, rendered in the
  sandbox and cut down further: variables from a fixed list, `{% if %}`, comparisons and a
  few text filters. No loops, `set`, includes, calls or attribute access.
- **Markdown** is rendered with raw HTML and images off, and links only to web pages, email
  addresses and this site.
- **The questionnaire** must be a shape the form and the server's validation understand.

Until a part is saved, the portal uses its default, which is what the site said before this
was editable.

## Signing up

Signup is email first:

1. **Email.** A registered address is told to sign in instead. Any other gets a signup link.
2. The link gives a **signup session**, which can do exactly one thing: submit the form.
3. **About you, shifts, questions, review.** The form submits the name, two preferred
   timeslots in order, any extra timeslots, and the questionnaire answers.
4. The portal creates a `pending` volunteer with a `p-…` id and upgrades the session in
   place. An organizer approves them in the scheduler.

Signup is **open** only when an organizer has opened it, the email public key is on the
portal, and there are two non-overlapping timeslots with space. Otherwise the page shows it
as closed or full. An **early-access invite** link gets one person past "closed", but never
past "full". The invite is spent at submit, not when the email is typed.

The two preferred timeslots can't overlap. Space is checked again inside the submit's
transaction, so two people can't take the last place at once. `remaining` comes from the
scheduler's snapshot, which includes any overbooking allowance, and the portal counts down
from it until the next push.

## A volunteer's own page

`/apply/me` shows the volunteer's status, their assigned shifts, their picks and their
answers. While pending or approved, they can:

- **Change their shifts.** The same rules as signup. A shift they already hold can be kept
  even when it reads as full. Dropping a shift they're assigned to is allowed: the page warns
  them, and the scheduler flags it for an organizer.
- **Change the answers marked editable** (T-shirt size, dinner, flexibility).
- **Cancel.** Their places go back at once. The picks are kept, so they can rejoin later.
- **Rejoin** after cancelling, choosing shifts again. Their approval was never touched.
- **Sign out**, here or everywhere.

## Email addresses

| Value | What it is | Who can reverse it |
|---|---|---|
| `email_hmac` | HMAC-SHA256 of the lowercased address, with `EMAIL_HMAC_KEY` | nobody (but an address can be checked against it, with the key) |
| `email_enc` | RSA-OAEP ciphertext under the organizers' public key | only `private.pem`, which never comes here |

A unique index on `email_hmac` is what prevents two signups for one address, even within the
same second.

Since the portal can't decrypt addresses, **it only ever mails an address someone has just
typed**: a sign-in link, during their own request. The one exception is admin mail the
scheduler asks for (organizer invites, password resets and early-access invites). Each is a
fixed template whose link must point at this deployment's own site, and each is rate-limited.
Every other message to volunteers is sent offline, by `vol_mail`.

## The sync API

The scheduler signs every request with `SYNC_SECRET`: method, path, timestamp, nonce and a
hash of the body. A timestamp more than 300 seconds off, or a nonce seen before, is refused.
Every failure is the same bare 401. Every POST carries an `op_id`, and a repeat gets the
stored result back, so a retry can't apply twice.

| Call | What it does |
|---|---|
| `GET heartbeat`, `GET config` | Health, cursors, key fingerprints, settings |
| `GET volunteers?since=` | Volunteers changed since a cursor, with their picks |
| `POST schedule` | Replace the schedule snapshot |
| `POST volunteers` | Apply organizer decisions, and re-keys to a badge number |
| `POST branding`, `settings`, `signup_invites`, `announcements` | Replace each |
| `POST send_admin_mail` | Send one admin message |
| `POST content` | Replace the landing page, questionnaire and invite email, each checked here first |
| `POST purge_volunteers` | Delete volunteers outright, with their sessions and rate-limit counters |
| `POST email_index` | Fill in `email_hmac` for volunteers who have only ciphertext, from values made offline with the private key |

## Data

SQLite at `/data/portal.db` on the `portal_data` volume, with WAL and foreign keys on. The
schema version is `PRAGMA user_version`, separate from the scheduler's, and the portal
migrates itself on startup. Back it up with `sqlite3 .backup`, never `cp`. See
[Deployment → Backups](../operations/deployment.md#backups).
