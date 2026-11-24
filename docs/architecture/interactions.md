# Object interaction diagrams

How the BSides services, and the main objects inside them, talk to each other. Each
diagram is drawn from the code and `compose.yml`. The names are real: routes, functions,
tables and columns.

| # | Diagram | Read it when you're changing… |
|---|---|---|
| 1 | [Deployment and trust boundaries](#1-deployment-and-trust-boundaries) | compose, nginx, the tunnel, anything network-facing |
| 2 | [An organizer change](#2-an-organizer-change) | any backend write endpoint |
| 3 | [One portal sync tick](#3-one-portal-sync-tick) | `portal_sync.py`, `portal/routers/sync.py` |
| 4 | [Volunteer signup](#4-volunteer-signup) | `portal/routers/signup.py`, `auth.py`, the `/apply` pages |
| 5 | [Approval and the push back](#5-approval-and-the-push-back) | `routers/portal_admin.py`, the volunteer push |
| 6 | [An export API call](#6-an-export-api-call) | `api_auth.py`, `routers/export.py` |
| 7 | [A schedule email round](#7-a-schedule-email-round) | `email_template.py`, `tools/vol_mail/` |
| 8 | [Shift check-in](#8-shift-check-in) | `checkin_codes.py`, `routers/checkin.py` |
| 9 | [Admin invite and reset mail](#9-admin-invite-and-reset-mail) | `invites.py`, `password_reset.py`, `send_admin_mail` |
| 10 | [Who owns which volunteer column](#10-who-owns-which-volunteer-column) | any volunteer column, either side |
| 11 | [An early-access invite](#11-an-early-access-invite) | `routers/portal_admin.py` invites, `signup_invites` on either side |
| 12 | [Purging a volunteer](#12-purging-a-volunteer) | `purge_volunteers` on either side, any table holding a `vol_id` |
| 13 | [Passkey sign-in](#13-passkey-sign-in) | `routers/passkeys.py`, `auth.start_session`, `lib/passkey.js` |
| 14 | [From a hostile request to an alert](#14-from-a-hostile-request-to-an-alert) | `security_signals.py`, `portal/validation.py`, `audit.py`, `alerts.py` |

The diagrams are Mermaid. They render on this site, on GitHub, and in VS Code's Markdown
preview with a Mermaid extension. If a diagram and the code disagree, the code is right:
fix the diagram in the same change.

---

## 1. Deployment and trust boundaries

Everything runs under podman-compose on one host. The only way in from the internet is a
tunnel: its client runs on the host, outside compose, and dials the frontend's two
loopback-only ports. Thick lines are internet-facing.

```mermaid
flowchart LR
    subgraph internet["Internet"]
        vol["Volunteer browser"]
        org["Organizer browser"]
        integ["Integrator server"]
        graph_api["Microsoft Graph"]
        cf["Edge<br/>TLS, access gate"]
    end

    subgraph orgbox["An organizer's machine"]
        volmail["tools/vol_mail<br/>+ private.pem"]
    end

    subgraph host["Host"]
        cfd["tunnel client<br/>(not in compose)"]

        subgraph default_net["podman-compose network: default"]
            nginx["frontend: nginx<br/>:8080 full site<br/>:8082 portal only<br/>host 127.0.0.1 only"]
            backend["backend: FastAPI :5000<br/>scheduler"]
            pg[("postgres 18<br/>pgdata18")]
            portal["portal: FastAPI :5001<br/>no host port"]
            pdb[("SQLite<br/>portal.db")]
        end

        subgraph mail_internal["network: mail_internal (internal)"]
            relay["mailrelay :5002<br/>Graph refresh token"]
        end
    end

    vol ==>|"public host<br/>/apply, /api/portal"| cf
    org ==>|"admin host<br/>after the gate's sign-in"| cf
    integ ==>|"admin host /api/export/*<br/>gate bypass, bearer key"| cf
    volmail ==>|"export API<br/>volunteers:email, checkin:codes"| cf

    cf ==>|"tunnel"| cfd
    cfd -->|"public host → :8082"| nginx
    cfd -->|"admin host → :8080"| nginx
    nginx -->|"/api/portal/*<br/>(both servers)"| portal
    nginx -->|"/api/*<br/>(:8080 only)"| backend
    backend --> pg
    portal --> pdb

    backend -->|"signed sync API<br/>http://portal:5001<br/>scheduler is always the client"| portal
    portal -->|"POST /send, bearer token"| relay
    relay ==>|"mail_egress: /me/sendMail"| graph_api

    volmail ==>|"device-code sign-in, sendMail"| graph_api
```

Points the picture makes:

- **The portal never calls the scheduler.** All sync traffic is scheduler → portal, so the
  scheduler needs no inbound route.
- **The public hostname can't reach the scheduler.** nginx's `:8082` server serves the SPA and
  `/api/portal/*` only; `/admin` and every other `/api/*` are a 404 there, and `/api/sync/*`
  isn't under `/api/portal/`. The organizer side is on its own hostname behind an access
  gate, with `/api/export/*` let through for servers, which carry their own bearer keys.
- **Only the mail relay holds the Graph credential.** It sits on an internal-only network
  that the portal shares. Its own egress network reaches Microsoft and nothing else.
- **`private.pem` never reaches the host.** Email addresses are only decrypted on an
  organizer's machine, by vol_mail or by the browser (Volunteers tab → Decrypt).
- **Client IPs:** nginx takes the client IP from the edge's client-IP header, which the edge
  sets, and passes it on as a single `X-Forwarded-For` value (portal/CLAUDE.md → Auth
  invariants).
- **Nothing but the frontend publishes a host port,** and only on `127.0.0.1`, so plain HTTP
  isn't reachable from the LAN.

---

## 2. An organizer change

Any write in the organizer app, for example assigning a volunteer to a slot. The same tail
end runs for every schedule mutation: browsers learn about it over SSE, and the portal gets
it on the next push.

```mermaid
sequenceDiagram
    autonumber
    actor O as Organizer
    participant FE as Admin SPA
    participant NG as nginx
    participant R as routers/schedule.py
    participant DB as db.py
    participant PG as Postgres
    participant EV as routers/events.py
    participant PS as portal_sync

    O->>FE: assign volunteer to slot
    FE->>NG: POST /api/assign_vol (FormData, session cookie)
    NG->>R: proxy
    R->>DB: Depends(get_db) borrows a pooled connection
    R->>DB: Depends(RoleChecker("organizer"))
    DB->>DB: get_session_data: _session_cache, else sessions table
    alt not signed in, or wrong role
        DB-->>FE: 401 or 403
    end
    R->>PG: UPDATE schedule SET vol_id = ? WHERE ...
    R->>PG: COMMIT
    R->>EV: broadcast("assignment")
    EV->>PS: request_push()
    PS-->>PS: sets _state.push_event (thread-safe)
    EV-->>FE: SSE "assignment" to every open /api/events stream
    R->>DB: audit(request, ...)
    R->>PG: _build_schedule (full schedule)
    R-->>FE: 200 with the updated schedule
    Note over PS: The sync loop wakes, waits PUSH_DEBOUNCE (2 s) so a burst<br/>of edits pushes once, then runs a tick (diagram 3).
```

---

## 3. One portal sync tick

`portal_sync._sync_once`, every `PORTAL_SYNC_INTERVAL` seconds (30), or sooner when
`request_push()` wakes it. Every request is signed. The portal rejects any failure with the
same bare 401.

```mermaid
sequenceDiagram
    autonumber
    participant L as portal_sync loop
    participant PG as Postgres
    participant SEC as portal security.py
    participant P as portal routers/sync.py
    participant SQ as portal.db

    Note over L,P: Each request carries x-sync-key-id, -timestamp, -nonce, -signature<br/>signing METHOD, path, timestamp, nonce, sha256(body)
    L->>P: GET /api/sync/heartbeat
    P->>SEC: verify signature, ±300 s window, nonce unseen
    P->>SQ: INSERT sync_nonces (replay guard)
    P-->>L: revs, snapshot_hash, branding_hash, hmac_key_fp, pubkey_fp

    opt volunteers_rev > our cursor
        L->>P: GET /api/sync/volunteers?since=cursor (pages of 500)
        P-->>L: changed volunteers and their picks
        L->>PG: skip a row that is the old id of a pending re-key
        L->>PG: dirty row: volunteer-owned columns only, else upsert the whole row
        L->>PG: mark the signup_invites row used when invite_id comes back
        L->>PG: raise shift_flags for dropped assigned picks, replace vol_picks
        L-->>L: broadcast("volunteers") when anything was written
    end

    opt rows with portal_dirty
        L->>P: POST /api/sync/volunteers (rekeys first, then upserts, op_id)
        P->>SQ: static upsert, only the columns sent
        P-->>L: written, renamed
        L->>PG: clear portal_dirty where status still matches what was sent
    end

    L->>P: GET /api/sync/config
    P-->>L: the portal's settings
    opt settings differ
        L->>P: POST /api/sync/settings (portal_signup_open, email_pubkey if fp differs)
    end

    L->>PG: build_snapshot (days, times, day_times with remaining, positions, slots)
    opt snapshot content_hash differs
        L->>P: POST /api/sync/schedule (op_id)
        P->>SQ: BEGIN IMMEDIATE, replace each snapshot table, COMMIT
    end

    opt branding hash differs
        L->>P: POST /api/sync/branding (title, logos, favicon, contact links)
    end

    L->>PG: build_content (landing, questionnaire, invite email)
    opt heartbeat has content_hash and it differs
        L->>P: POST /api/sync/content (op_id)
    end

    L->>PG: build_invites (early-access invite hashes, diagram 11)
    opt heartbeat has invites_hash and it differs
        L->>P: POST /api/sync/signup_invites (op_id), replaces the portal's list
    end

    L->>PG: portal_sync_state: mark ok, or record the error
    Note over L: Any exception ends the tick as failed. The loop logs on the<br/>transition only and tries again next tick.
```

Every POST carries an `op_id`. The portal stores the result in `sync_ops` and replays it for
a repeat, so a push that timed out after the portal committed can be retried safely.

---

## 4. Volunteer signup

Email first. The portal can only ever mail an address someone has just shown they know,
because nothing in it can decrypt `email_enc`.

```mermaid
sequenceDiagram
    autonumber
    actor V as Volunteer
    participant SPA as /apply pages
    participant S as portal signup.py and auth.py
    participant SQ as portal.db
    participant M as mailer.py
    participant MR as mailrelay
    participant G as Microsoft Graph

    V->>SPA: enter email (and the ?invite= token, if the link carried one)
    SPA->>S: POST /api/portal/signup/check_email {email, invite}
    S->>S: email_hmac = HMAC(EMAIL_HMAC_KEY, normalize(email))
    S->>SQ: ratelimit.check per IP and per address
    alt over the limit
        S-->>SPA: 429
    else already registered
        S-->>SPA: "sign in instead" (goes to request_link)
    else signup closed and no live invite, or full
        S-->>SPA: 409 closed or full
    else no email_pubkey
        S-->>SPA: 409 closed
    else open, or a live invite
        S->>S: email_enc = RSA-OAEP(email_pubkey, email)
        S->>SQ: INSERT login_tokens (sha256 of token, purpose signup, invite_id)
        S->>M: send magic link {PORTAL_PUBLIC_URL}/apply/verify?t=...
        M->>MR: POST /send {to, subject, body}, bearer MAIL_RELAY_TOKEN
        MR->>G: /me/sendMail, from MAIL_SENDER (set at startup)
        S-->>SPA: check your mail
    end
    Note over S: Every reply waits out the same floor, so timing doesn't<br/>tell one outcome from another. The invite isn't spent here.

    V->>SPA: open the link at /apply/verify
    Note over SPA: Loading the page spends nothing:<br/>mail scanners and link previews fetch links.
    V->>SPA: confirm
    SPA->>S: POST /api/portal/verify {t}
    S->>SQ: conditional UPDATE consumes the token (rowcount decides a race)
    S->>SQ: INSERT session (hash only), vol_id NULL means signup session
    S-->>SPA: session cookie

    SPA->>S: GET signup/fields, GET timeslots (remaining, overlaps)
    SPA->>S: POST signup/check_name
    S-->>SPA: possible_match only
    V->>SPA: answers, two prefs, extras
    SPA->>S: POST /api/portal/signup/submit
    S->>SQ: BEGIN IMMEDIATE
    S->>SQ: address already has a volunteer row?
    S->>SQ: _consume_invite: conditional UPDATE signup_invites SET used_at
    S->>SQ: re-check open (or the invite), re-check picks
    S->>SQ: INSERT volunteers p-xxxxxxxx status pending, invite_id, vol_picks
    alt already signed up, or the unique index on email_hmac (two submits raced)
        S-->>SPA: 409 already signed up
    else closed and the invite is gone
        S-->>SPA: 409
    else a pick filled meanwhile
        S-->>SPA: 409, detail.full names it, the form reopens the shifts step
    else ok
        S->>SQ: remaining minus 1 for the two prefs
        S->>SQ: upgrade the session in place to this vol_id, COMMIT
        S-->>SPA: signed up, pending
    end
    Note over S,SQ: Any 409 rolls the whole transaction back, the invite's use with it.
    Note over S,SQ: Next sync tick (diagram 3): volunteers_rev moved, so the scheduler<br/>pulls the pending row and its picks into Postgres.
```

---

## 5. Approval and the push back

An organizer approves a pending signup, optionally giving it a badge number. The request
doesn't call the portal itself: `portal_dirty` marks the row, and `request_push()` wakes the
sync loop, which carries it about `PUSH_DEBOUNCE` (2 s) later. If the portal is down, the
row stays dirty and every later tick retries it.

```mermaid
sequenceDiagram
    autonumber
    actor O as Organizer
    participant A as routers/portal_admin.py
    participant PG as Postgres
    participant L as portal_sync loop
    participant P as portal routers/sync.py
    participant SQ as portal.db

    O->>A: POST /api/admin/portal/approve {vol_id, new_vol_id?}
    A->>PG: _pending: status pending, not cancelled or inactive
    alt badge number given
        A->>PG: refuse if the vol_id has schedule, vol_notes or radio rows (one EXISTS query)
        A->>PG: copy the row under new_vol_id, status active, portal_rekey_from = old id
        A->>PG: move vol_picks, delete the old row
    else no badge number
        A->>PG: UPDATE status active, portal_dirty TRUE
    end
    A->>PG: COMMIT, audit
    A->>L: portal_sync.request_push()
    A-->>O: ok, final vol_id
    Note over PG: is_assignable is now TRUE (a generated column),<br/>so the volunteer appears in every picker.

    L->>P: tick after the debounce: POST /api/sync/volunteers
    Note right of L: rekeys first (old to new vol_id), then the dirty rows<br/>with scheduler-owned columns only
    P->>SQ: rename vol_id in sessions, login_tokens, vol_picks, snapshot_slots
    P->>SQ: upsert: CASE WHEN sent per column, so unsent columns keep their value
    P-->>L: written, renamed
    L->>PG: clear portal_dirty only where status still equals what was sent
    Note over L,PG: While a row is dirty, a pull takes only the volunteer-owned columns, and<br/>skips the old id of a pending re-key outright, so the portal's older copy<br/>can't undo a decision that hasn't reached the portal yet.
```

---

## 6. An export API call

A server, or vol_mail, reads the schedule or the volunteers with a bearer key. Every refusal
looks the same.

```mermaid
sequenceDiagram
    autonumber
    participant C as Caller (server or vol_mail)
    participant NG as nginx
    participant T as api_auth.TokenChecker(scope)
    participant E as routers/export.py
    participant PG as Postgres

    C->>NG: GET /api/export/volunteers?status=active<br/>Authorization: Bearer bsc_...
    NG->>T: proxy (client IP = first X-Forwarded-For hop, and nginx sends just one)
    T->>T: _over_limit(ip)? 10 failures in 15 min, keyed by rate_key (IPv6 by its /64)
    alt over the limit
        T-->>C: 429
    end
    T->>PG: SELECT ... FROM api_tokens WHERE token_hash = sha256(key)<br/>AND not revoked AND not expired
    alt no row, scope missing, or IP not in allowed_ips
        T->>T: _record_fail(ip)
        T->>T: audit "Export API refused"
        T-->>C: 401 {"detail": "Unauthorized"} (the same in every case)
    end
    T->>PG: UPDATE last_used_at, last_used_ip, COMMIT
    T->>T: audit under the key's label and id, never the key
    T->>E: key row with scopes
    E->>PG: _slots: one query for every slot, with check-in state
    E->>PG: volunteers WHERE (? OR v.is_assignable)
    opt key has checkin:codes
        E->>E: checkin_codes.make_code per shift
    end
    E->>E: response_model VolunteersExport, exclude_unset<br/>(email_enc only with volunteers:email)
    E-->>C: 200 JSON
```

The caller's guide, with every field, is the [Export API](../api/export.md) page, generated
from the same response models.

---

## 7. A schedule email round

The site never sends schedule email. It provides the template and the data; vol_mail, on an
organizer's machine, decrypts the addresses and sends. Usually a draft round, then a final.

```mermaid
sequenceDiagram
    autonumber
    actor O as Organizer
    participant SITE as Scheduler (email_template.py)
    participant VM as vol_mail.py
    participant EX as Export API
    participant KEY as private.pem
    participant G as Microsoft Graph

    O->>SITE: write the template (Markdown, images), preview as any volunteer
    O->>SITE: GET /api/email_template/package
    SITE-->>O: zip: HTML and text with placeholders, assets, manifest with sha256

    O->>VM: vol_mail.py fetch
    VM->>EX: GET /api/export/volunteers?assigned=1 (diagram 6)
    EX-->>VM: volunteers with email_enc and checkin_code per shift
    VM->>VM: box.json, mode 0600

    O->>VM: vol_mail.py preview (one volunteer)
    VM-->>O: .eml, .html, .txt with real codes and QR images

    O->>VM: vol_mail.py send --round draft --dry-run
    VM->>KEY: decrypt each email_enc (RSA-OAEP)
    VM->>VM: skip any that don't decrypt or aren't a plain address, and report them
    VM->>VM: write the plan: recipients + sha256 of package and box

    O->>VM: vol_mail.py send --round draft
    VM->>VM: refuse if the package or box changed since the dry run
    VM->>VM: skip anyone already in this round's sent log
    VM-->>O: "Type send to continue"
    VM->>G: device-code sign-in (kept in memory only)
    loop each recipient
        VM->>KEY: decrypt email_enc
        VM->>G: sendMail (HTML, inline QR images) as MAIL_SENDER
        VM->>VM: append to the round's sent log
    end
```

---

## 8. Shift check-in

The QR code in the email is an HMAC over the slot and the volunteer. A valid code isn't
enough on its own: the volunteer must still be assigned to that slot.

```mermaid
sequenceDiagram
    autonumber
    actor D as Check-in desk
    participant SC as BarcodeScanner.vue
    participant CK as routers/checkin.py
    participant CC as checkin_codes.py
    participant PG as Postgres

    D->>SC: scan QR (or type the code)
    SC->>CK: POST /api/checkin/scan {code, force}
    CK->>CK: RoleChecker organizer or checkin
    CK->>PG: app_kv.checkin_key
    CK->>CC: verify_code(key, "BSC1.slot_key.b64(vol_id).mac")
    alt bad MAC or malformed
        CC-->>D: 400
    end
    CK->>CK: record_checkin(vol_id, slot_key, force), the one check-in path
    CK->>PG: is vol_id still assigned to slot_key?
    alt reassigned since the email went out
        CK-->>D: 409
    end
    CK->>CC: shift_start(event_date, time_str, event_tz)
    CK->>PG: already a [CheckIn] note?
    alt already checked in
        CK-->>D: already: true, nothing written
    end
    alt over 45 min early or 10 min late, and not force
        CK-->>D: needs_confirm, nothing written
        D->>SC: Check in anyway
        SC->>CK: POST /api/checkin/scan {code, force: true}
    end
    CK->>PG: INSERT vol_notes "[CheckIn] | slot_key"
    opt forced early or late
        CK->>PG: INSERT vol_notes "[CheckInTiming] Early or Late N min | slot_key"
    end
    CK-->>D: checked in
```

Rotating the key (`POST /api/checkin/rotate_key`) changes `checkin_key`, which voids every
code already mailed.

---

## 9. Admin invite and reset mail

The scheduler has no mail path of its own. It asks the portal, through the one narrow
endpoint that mails an address the portal didn't verify itself. That endpoint takes four
kinds: `invite` and `reset` (drawn here), `signup_invite` (diagram 11) and `alert`
(diagram 14).

```mermaid
sequenceDiagram
    autonumber
    actor O as Organizer
    participant B as backend invites.py or password_reset.py
    participant PG as Postgres
    participant PS as portal_sync.send_admin_mail
    participant P as portal routers/sync.py
    participant M as Mailer.send_admin
    participant MR as mailrelay
    participant G as Microsoft Graph

    O->>B: POST /api/invites {role, email} (or someone: POST password_reset/request)
    B->>PG: INSERT user_invites: sha256(token), expires NOW() + ? * INTERVAL
    B->>PS: kind invite, address, link {site_origin()}/admin/invite?t=...
    Note right of B: site_origin() is ADMIN_PUBLIC_URL,<br/>else PORTAL_PUBLIC_URL (one origin)
    PS->>P: POST /api/sync/send_admin_mail (signed, op_id)
    P->>P: link must start with its own ADMIN_PUBLIC_URL + /admin/invite?t= or /admin/reset?t=
    P->>P: one address, strict pattern, at most 20 admin mails an hour
    P->>M: fixed template admin_invite.txt or admin_reset.txt
    M->>MR: POST /send
    MR->>G: /me/sendMail
    P->>P: audit "admin_mail.sent", detail invite (no address, no link)
    P-->>PS: ok
    alt mail failed
        B-->>O: invite created, email_error, copy the link yourself
    else sent
        B-->>O: invite created, emailed
    end
    Note over B,O: Password reset always returns the same reply (GENERIC),<br/>whatever happened. Failures go to the log and the audit trail.
```

---

## 10. Who owns which volunteer column

A volunteer exists in both databases. Each column has exactly one owner, and each side only
ever sends the columns it owns. That's what lets an approval waiting to go out and an edit
the volunteer just made both survive.

```mermaid
flowchart LR
    subgraph portal_side["Portal owns (volunteer's own edits)"]
        vo["time_pref, add_time, add_any<br/>answers, vol_picks<br/>cancelled_at"]
    end
    subgraph sched_side["Scheduler owns (organizer decisions)"]
        so["status (pending, active)<br/>name<br/>inactive_at"]
    end
    subgraph derived["Derived in Postgres (generated columns)"]
        gc["is_live = not cancelled and not inactive<br/>is_counted = pending or active, and live<br/>is_assignable = active and live"]
    end

    vo -->|"pull: always taken,<br/>even on a dirty row"| pgv[("Postgres volunteers")]
    so -->|"push: only these columns,<br/>CASE WHEN sent per column"| pv[("portal.db volunteers")]
    pgv --> gc
    gc -->|"pickers, capacity,<br/>needs-replacement flags"| uses["schedule.py, volunteers.py,<br/>portal_admin.py, export.py,<br/>portal_sync.compute_remaining"]
```

Defined in `portal_sync.VOLUNTEER_OWNED` / `SCHEDULER_OWNED` (scheduler side) and
`routers/sync.PUSH_COLUMNS` (portal side). `cancelled_at` isn't in the portal's push
columns at all, so the scheduler can neither set nor clear it.

---

## 11. An early-access invite

A single-use link that lets one person sign up while the call for volunteers is closed. The
scheduler makes it and keeps only its hash; the portal gets the hashes on the sync, spends
the invite at submit, and the scheduler learns of the use from the next pull. An organizer
can copy a link (`POST /api/admin/portal/invites`) or have it emailed (`.../invites/send`,
drawn here).

```mermaid
sequenceDiagram
    autonumber
    actor O as Organizer
    participant A as routers/portal_admin.py
    participant PG as Postgres
    participant PS as portal_sync
    participant P as portal routers/sync.py
    participant MR as Mailer → mailrelay
    actor V as Volunteer
    participant S as portal signup.py

    O->>A: POST /api/admin/portal/invites/send {name, email, days}
    A->>A: clean_email, then one send per INVITE_SEND_GAP_SEC (10 s), all organizers
    alt within 10 s of the last send
        A-->>O: 429, Retry-After
    end
    A->>PG: INSERT signup_invites: sha256(token), label, expires NOW() + days, COMMIT
    Note right of A: link = {PORTAL_PUBLIC_URL}/apply/signup?invite=token,<br/>never stored, never returned by send
    A->>PS: push_signup_invites (before the mail, so the link works on arrival)
    PS->>P: POST /api/sync/signup_invites {invites: hashes, content_hash, op_id}
    P->>P: replace the list wholesale, keep used_at, store signup_invites_hash
    A->>PS: send_admin_mail kind signup_invite
    PS->>P: POST /api/sync/send_admin_mail (signed, op_id)
    P->>P: link must start with PORTAL_PUBLIC_URL + /apply/signup?invite=
    P->>P: at most 200 invite mails an hour
    P->>MR: the organizers' invite template (Volunteer Portal → Invite email)
    alt push or mail failed
        A->>PG: DELETE the invite, request_push()
        A-->>O: 429 (portal's hourly cap) or 502
    else sent
        A->>PG: UPDATE emailed_at, audit (label, never the address)
        A-->>O: invite_id, label, expires_at
    end

    V->>S: open /apply/signup?invite=…
    S->>S: GET signup/invite: valid, used, expired or revoked (read-only)
    V->>S: check_email, verify, submit (diagram 4), the invite riding on the login token
    S->>S: submit: _consume_invite in the same transaction as the INSERT
    Note over PS,S: Next tick (diagram 3): the pulled volunteer carries invite_id,<br/>so the scheduler sets used_at and used_by. The signup is still pending.
```

Revoking (`invites/revoke`) sets `revoked_at` and calls `request_push()`; the portal refuses
the link from the next push on.

---

## 12. Purging a volunteer

Deleting a volunteer outright, for clearing test signups so their address can sign up
again. Inactive volunteers only. The portal goes first: deleted here first, the next pull
would bring the volunteer straight back.

```mermaid
sequenceDiagram
    autonumber
    actor O as Organizer
    participant A as routers/portal_admin.py
    participant PS as portal_sync.purge_volunteers
    participant PG as Postgres
    participant P as portal routers/sync.py
    participant SQ as portal.db
    participant EV as routers/events.py

    O->>A: POST /api/admin/volunteers/purge {vol_ids, confirm}
    A->>A: 1 to PURGE_MAX (500) ids, confirm = the vol_id, or the count
    A->>PG: every id exists and has inactive_at
    alt missing, or still active
        A-->>O: 404, or 409 "make them inactive first"
    end
    A->>PS: purge_volunteers(ids)
    PS->>PS: take _state.lock, the one every sync tick holds
    PS->>PG: SELECT vol_id, portal_rekey_from
    opt portal configured
        PS->>P: POST /api/sync/purge_volunteers {vol_ids + any re-key old ids, op_id}
        P->>SQ: BEGIN IMMEDIATE
        P->>SQ: delete the row, picks, sessions and login_tokens by id or email_hmac
        P->>SQ: clear snapshot_slots, drop the address's rate-limit buckets, audit, COMMIT
        P-->>PS: purged
        alt portal failed in any way
            PS-->>A: RuntimeError, nothing deleted here
            A-->>O: 502, try again
        end
    end
    PS->>PG: _PURGE_LOCAL_SQL: delete picks, shift_flags, vol_notes,<br/>empty vol_id in schedule and radio_checkouts (rows kept)
    PS->>PG: DELETE volunteers RETURNING vol_id, COMMIT
    PS->>PS: release the lock, request_push()
    A->>A: audit each, by name and id
    A->>EV: broadcast("volunteers"), broadcast("schedule")
    A-->>O: purged ids
```

Because the delete runs under the sync lock, a pull already under way finishes first, and
no pull can start between the portal's delete and the scheduler's. Radio history keeps
`person_name`, so a returned radio still says who had it.

---

## 13. Passkey sign-in

An organizer signs in with a passkey instead of a password. No username is typed: the
authenticator offers whatever passkeys it holds for this site. The session at the end is the
one the password login makes, from the same function.

```mermaid
sequenceDiagram
    autonumber
    actor O as Organizer
    participant L as setup/Login.vue + lib/passkey.js
    participant AU as Authenticator (browser, phone or manager)
    participant PK as routers/passkeys.py
    participant AS as auth.start_session
    participant PG as Postgres

    L->>PK: GET /api/passkeys/config
    PK->>PK: _site(): rp_id = ADMIN_PUBLIC_URL's host (else PORTAL_PUBLIC_URL's)
    PK-->>L: enabled (false when unset, or plain http off localhost)
    Note over L: The button shows only when enabled and the browser has WebAuthn.

    O->>L: Sign in with a passkey
    L->>PK: POST /api/passkeys/login/options
    PK->>PG: sweep expired webauthn_challenges
    PK->>PG: INSERT sha256(challenge), purpose login, no user_id, 5 min, COMMIT
    PK-->>L: options: rp_id, challenge, user verification required
    L->>AU: startAuthentication (@simplewebauthn/browser, lazy-loaded)
    AU->>O: PIN or biometric
    AU-->>L: signed assertion (credential id, userHandle, sign count)
    alt prompt cancelled
        L-->>O: nothing shown (cancelled)
    end

    L->>PK: POST /api/passkeys/login/verify {credential}
    PK->>PG: _consume_challenge: UPDATE SET used_at WHERE unused, unexpired,<br/>purpose login, COMMIT, before any signature check
    alt no such challenge (expired or replayed)
        PK-->>L: 400
    end
    PK->>PG: SELECT user_passkeys JOIN users WHERE credential_id = ?
    alt unknown passkey, or userHandle ≠ users.webauthn_user_id
        PK->>PK: audit
        PK-->>L: 401 "Passkey not recognized. Sign in with your password"
    end
    PK->>PK: verify_authentication_response (challenge, rp_id, origin,<br/>public key, sign count, user verification)
    alt signature or count fails
        PK->>PK: audit
        PK-->>L: 401
    end
    PK->>PG: UPDATE sign_count, backed_up, last_used_at
    PK->>AS: start_session(row)
    AS->>PG: UPDATE users last_login_at, INSERT sessions, COMMIT
    AS->>AS: session cookie, _session_cache entry
    PK->>PK: audit "Signed in with a passkey (alg)"
    PK-->>L: {username, role}, the same as POST /api/login
```

The challenge burns on the first attempt whether the signature then checks or not, so a
captured response is good for nothing. A passkey removed from the account still lives on the
device, which keeps offering it; that's the 401 that says to use the password. Adding a
passkey (`register/options`) needs the current password, so a stolen session cookie can't
plant one.

---

## 14. From a hostile request to an alert

A request to the volunteer portal that looks like an attack, or that the volunteer pages
could never have sent, is noticed on the portal, pulled into the scheduler's audit log on
the next sync tick, and mailed as an alert. A finding on the organizer site takes the same
path from `audit()` on, without the pull. Nothing here blocks the request, except a NUL, or
an anomaly with `PORTAL_STRICT=enforce`.

```mermaid
sequenceDiagram
    autonumber
    actor X as Client
    participant SS as portal security_signals.SecuritySignals
    participant V as portal validation.py
    participant SQ as portal.db portal_security_events
    participant L as portal_sync loop
    participant AU as audit.ingest / audit()
    participant AL as alerts.observe
    participant PG as Postgres audit_events
    participant W as alerts worker
    participant P as portal send_admin_mail (kind alert)
    actor O as Organizers with audit_alerts

    X->>SS: POST /api/portal/signup/submit
    SS->>SS: signatures, scanner, probe path, NUL (400), cross-site
    SS->>V: the route runs
    V->>V: strict rules: unknown key, wrong type, name_chars, ...
    alt an anomaly
        V->>SQ: portal.anomaly {rule, where, mode, held_back} (≤ 1/min per client+rule+path)
        Note right of V: enforce: 400 Malformed request<br/>report: the old forgiving path
    end
    SS->>SS: score the response status, once per hour per client
    SS->>SQ: security.attack_signature / suspicious_client / sync_auth_fail
    L->>P: GET /api/sync/heartbeat → security_events_rev
    L->>SQ: GET /api/sync/security_events?since=cursor
    L->>AU: ingest(event, "portal"): checked as untrusted
    AU->>AU: logs/audit.jsonl (json.dumps, ensure_ascii)
    AU->>AL: observe(record)
    AL->>AL: rule by address, cooldown 1 h, held_back counted
    AL->>AU: audit("alert.fired", severity of the rule)
    AU->>PG: INSERT (own connection, own commit), source portal
    AL->>W: queue (≤ 15 mailed an hour)
    W->>P: one per recipient: rule, title, facts (defanged by the portal)
    P->>O: [title] Security alert, link {ADMIN_PUBLIC_URL}/admin/audit
    W->>AU: alert.mailed (a count) or alert.mail_fail (by username)
    Note over AL,W: flush_held_back every 10 min: a "(continued)" alert<br/>for a cooled-down rule with events held back
```
