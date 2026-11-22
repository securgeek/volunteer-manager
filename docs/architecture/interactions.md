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
        L->>P: GET /api/sync/volunteers?since=cursor
        P-->>L: changed volunteers and their picks
        L->>PG: take volunteer-owned columns, skip dirty or re-keying rows
        L->>PG: raise shift_flags for dropped assigned picks
    end

    opt rows with portal_dirty
        L->>P: POST /api/sync/volunteers (rekeys first, then upserts, op_id)
        P->>SQ: static upsert, only the columns sent
        P-->>L: written, renamed
        L->>PG: clear portal_dirty where status still matches what was sent
    end

    opt settings differ
        L->>P: POST /api/sync/settings (portal_signup_open, email_pubkey if fp differs)
    end

    L->>PG: build_snapshot (days, times, day_times with remaining, positions, slots)
    opt snapshot content_hash differs
        L->>P: POST /api/sync/schedule (op_id)
        P->>SQ: BEGIN IMMEDIATE, replace each snapshot table, COMMIT
    end

    opt branding hash differs
        L->>P: POST /api/sync/branding (title, logo, favicon)
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

    V->>SPA: enter email
    SPA->>S: POST /api/portal/signup/check_email
    S->>S: signup_state: open needs the switch, a pubkey and a free pair
    S->>S: email_hmac = HMAC(EMAIL_HMAC_KEY, normalize(email))
    alt already registered
        S-->>SPA: "sign in instead" (goes to request_link)
    else signup closed or full
        S-->>SPA: closed or full
    else open
        S->>S: email_enc = RSA-OAEP(email_pubkey, email)
        S->>SQ: INSERT login_tokens (sha256 of token, purpose signup)
        S->>M: send magic link {PORTAL_PUBLIC_URL}/apply/verify?t=...
        M->>MR: POST /send {to, subject, body}, bearer MAIL_RELAY_TOKEN
        MR->>G: /me/sendMail, from MAIL_SENDER (set at startup)
        S-->>SPA: check your mail (rate limited per IP and per address)
    end

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
    S->>SQ: BEGIN IMMEDIATE: re-check picks, remaining minus 1 for the two prefs
    S->>SQ: INSERT volunteers p-xxxxxxxx status pending, vol_picks
    alt a pick filled meanwhile
        S-->>SPA: 409, detail.full names it, the form reopens the shifts step
    else unique index on email_hmac (two submits raced)
        S-->>SPA: 409 already signed up
    else ok
        S->>SQ: upgrade the session in place to this vol_id
        S-->>SPA: signed up, pending
    end
    Note over S,SQ: Next sync tick (diagram 3): volunteers_rev moved, so the scheduler<br/>pulls the pending row and its picks into Postgres.
```

---

## 5. Approval and the push back

An organizer approves a pending signup, optionally giving it a badge number. Nothing is
pushed immediately: `portal_dirty` marks the row, and the next tick carries it.

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
    Note over PG: is_assignable is now TRUE (generated column, v17),<br/>so the volunteer appears in every picker.

    L->>P: next tick: POST /api/sync/volunteers
    Note right of L: rekeys first (old to new vol_id), then the dirty rows<br/>with scheduler-owned columns only
    P->>SQ: rename vol_id in sessions, login_tokens, vol_picks, snapshot_slots
    P->>SQ: upsert: CASE WHEN sent per column, so unsent columns keep their value
    P-->>L: written, renamed
    L->>PG: clear portal_dirty only where status still equals what was sent
    Note over L,PG: While a row is dirty, pulls skip it, so the portal's older copy<br/>can't undo a decision that hasn't reached the portal yet.
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
    NG->>T: proxy (client IP = rightmost X-Forwarded-For)
    T->>T: _over_limit(ip)? 10 failures in 15 min
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
endpoint that mails an address the portal didn't verify itself.

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
    B->>PS: kind invite, address, link {PORTAL_PUBLIC_URL}/admin/invite?t=...
    PS->>P: POST /api/sync/send_admin_mail (signed, op_id)
    P->>P: link must start with this site's /admin/invite?t= or /admin/reset?t=
    P->>P: one address, strict pattern, at most 20 an hour
    P->>M: fixed template admin_invite.txt or admin_reset.txt
    M->>MR: POST /send
    MR->>G: /me/sendMail
    P->>P: audit "admin_mail.sent invite" (no address, no link)
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
    subgraph derived["Derived in Postgres (generated, v17)"]
        gc["is_live = not cancelled and not inactive<br/>is_counted = pending or active, and live<br/>is_assignable = active and live"]
    end

    vo -->|"pull: always taken,<br/>even on a dirty row"| pgv[("Postgres volunteers")]
    so -->|"push: only these columns,<br/>CASE WHEN sent per column"| pv[("portal.db volunteers")]
    pgv --> gc
    gc -->|"pickers, capacity,<br/>needs-replacement flags"| uses["schedule.py, volunteers.py,<br/>portal_admin.py, export.py"]
```

Defined in `portal_sync.VOLUNTEER_OWNED` / `SCHEDULER_OWNED` (scheduler side) and
`routers/sync.PUSH_COLUMNS` (portal side). `cancelled_at` isn't in the portal's push
columns at all, so the scheduler can neither set nor clear it.
