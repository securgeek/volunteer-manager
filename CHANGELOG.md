# Changelog

What changed in each release of Volunteer Manager. Newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Add to **Unreleased** as changes land; it becomes
the release's section when it's tagged (docs/operations/releasing.md).

A release that needs anything beyond the usual deploy (copy, build, restart: Deployment →
Shipping a change) starts with **Upgrade notes**: what to do, in order, and what to check
afterwards. No upgrade notes means nothing extra to do.

## [Unreleased] — 1.1

Audit log, security alerts and stricter input checks.

### Upgrade notes

The full checklist is
[Deployment → Upgrading to 1.1](https://securgeek.github.io/volunteer-manager/latest/operations/deployment/#upgrading-to-11-audit-log-and-alerts).
In short:

1. **Back up both databases**, then ship the release and **restart the whole stack**: the
   backend, portal and frontend (nginx) all change and must run together. A backend without
   the new portal can't send alert emails.
2. **Migrations run by themselves** on start: backend schema 19 → 20, portal 7 → 8. Both
   only add tables and columns, so the 1.0 code still runs on them if you need to roll back.
3. **`.env`** (both optional): `PORTAL_STRICT=report` is the default; set
   `AUDIT_RETENTION_DAYS` for a retention other than 365 days.
4. **Alert emails** start out going to the earliest organizer account only. Make sure that
   account has an email in Profile, tick anyone else under **Security alerts** in
   Admin → Users, then press **Send test alert** in Admin → Audit Log.
5. **Logs:** `logs/audit.log` stops growing and `logs/audit.jsonl` takes over. Archive the old
   file if you like, and set up rotation for the new one (Deployment → Backups).
6. **A few days later**, if Admin → Audit Log shows no `portal.anomaly` events from real
   volunteers, set `PORTAL_STRICT=enforce` and restart the portal.

If you ran a pre-release 1.1 build: its `users.audit_access` is replaced by
`users.audit_alerts`, ticked on the earliest organizer only. Re-tick anyone else.

Developers: `tools/gather-screenshots` now needs Node 22.12 or later.

### Added

- **Audit log** (Admin → Audit Log, for every organizer). Every sign-in and failed attempt,
  account and role change, schedule and volunteer change, export API call and refused request
  is recorded and searchable: filter by kind of event, severity, who, address, site or text,
  and expand a row for its details. Written to `logs/audit.jsonl` and to the new
  `audit_events` table.
- **Security alert emails** to the organizers ticked under **Security alerts** in
  Admin → Users: bursts of failed sign-ins and password sprays; new or changed accounts and
  roles; someone else's password, passkeys or reset address changed; a new email key or export
  API key; sign-ins or key use from a new network; attack patterns in requests; and input the
  volunteer pages could never send. At most once an hour per cause, then a summary of
  anything held back. **Send test alert** checks the mail path end to end.
- **Request watching** in the backend and the portal (`security_signals.py`, the same file in
  both): SQL and script injection, path traversal, scanners and probing, cross-site writes and
  forged session cookies. It only watches; the portal's findings reach the backend's audit log
  over the sync API.
- **Strict input rules** shared by the volunteer pages (`frontend/src/lib/portal_rules.js`)
  and the portal (`portal/validation.py`), held together by one list of test cases. Input that
  breaks them is a `portal.anomaly`: alerted on, and refused when `PORTAL_STRICT=enforce`.
- Settings `AUDIT_RETENTION_DAYS` (default 365) and `PORTAL_STRICT` (default `report`).
- Docs: Deployment → *Upgrading to 1.1*, audit-log rotation, an architecture diagram from a
  hostile request to an alert, and the organizer guide to the audit log and alerts.

### Changed

- The audit log is structured JSON (`logs/audit.jsonl`); the free-text `logs/audit.log` is no
  longer written.
- The sign-in page says how long to wait after too many wrong passwords, instead of "Invalid
  username or password".
- The volunteer pages explain a bad name, handle, email or answer in the form before sending,
  and say so when a sign-in link is damaged.
- The docs site is titled **Volunteer Manager**; the conference is spelled "BSidesCharm"
  throughout, and the maintenance page no longer names a conference.
- `tools/gather-screenshots` moves to `puppeteer-core` 25 and needs **Node 22.12+**; it
  reinstalls its packages when its lock file changes.
- Schema: backend 20 (`audit_events`, `users.audit_alerts`), portal 8
  (`portal_security_events`). Both migrate on start.
- nginx caps volunteer-portal request bodies at 64 KB.

### Fixed

- A username containing a NUL character crashed sign-in with a server error; any request
  carrying one is now refused with a 400.
- `sessions.ip_addr` recorded the proxy's address instead of the client's.

### Security

- The old audit log wrote a failed sign-in's username verbatim, so one containing a newline
  could forge log lines. Every line is now JSON with non-ASCII escaped.
- Failed password sign-ins are rate limited, per username and address (successes never
  count, so a check-in desk on the venue network can't lock itself out).
- Sign-in no longer reveals which usernames exist through response time.
- Alert emails are defanged (details an attacker typed can't arrive as clickable links) and
  have their own hourly mail allowance, apart from password resets.
- `tools/gather-screenshots`: six Dependabot alerts closed (`extract-zip`, `basic-ftp`, `zod`).

## [1.0.1] — 2026-11-22

### Fixed

- The portal's code reference named the database setting `PORTAL_DB_PATH`; it's `PORTAL_DB`.
- CSV imports say why a row wasn't saved in plain words, rather than the database's message.
- A failed invite email says it couldn't be sent and to copy the link.

### Security

- CodeQL fixes: the portal's verify page shows only a real token's shape, HTML-escaped; the
  email and link patterns can't backtrack on hostile input; the plain-text email renderer caps
  alt text and addresses.

### Added

- README screenshots, and `tools/gather-screenshots` to retake them on a throwaway demo stack.
- Refreshed interaction diagrams, including the export API.

## [1.0.0] — 2026-11-21

First release: the volunteer portal (call for volunteers, email-first signup with magic-link
sign-in, shift picking, self-service changes), the organizer app (schedule builder and editor,
approvals, assignment, schedule emails with check-in QR codes, check-in, radios, notes and
ratings, passkey or password sign-in with roles), the read-only export API, and email
addresses stored only encrypted to an offline key.

[Unreleased]: https://github.com/securgeek/volunteer-manager/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/securgeek/volunteer-manager/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/securgeek/volunteer-manager/releases/tag/v1.0.0
