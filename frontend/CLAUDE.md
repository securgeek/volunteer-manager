# Frontend — Vue 3 + Vite + PrimeVue 4

## Stack

- **Vue 3** (Composition API, `<script setup>`)
- **PrimeVue 4** — import components from `primevue/<component>`
- **Noir theme** — `definePreset(Aura, {...})` in `main.js`; dark mode via `.dark-app` on `<html>`,
  for `/admin*` only. `/` and `/apply/*` use the conference theme (Volunteer pages theme, below)
- **Vite** — proxies `/api/portal` → `http://portal:5001` and `/api` → `http://backend:5000`
  (see `vite.config.js`); `base: '/'`
- **vue-router 4** — history mode, routes in `src/router.js`

## Component map

```
src/
├── App.vue              ← <RouterView/> and nothing else
├── router.js            ← Route table; everything but Landing is lazy
├── main.js              ← PrimeVue setup, Noir theme registration, router
├── views/
│   ├── Landing.vue      ← `/` — call for volunteers, from `GET /api/portal/landing` (organizer-editable); sends the admin host to /admin
│   ├── Admin.vue        ← `/admin` — auth shell, setup wizard, toolbar menus, the three tabs
│   ├── admin/           ← VolunteersTab.vue, RadiosTab.vue (Schedule's tab content is in Admin.vue)
│   ├── AcceptInvite.vue ← `/admin/invite?t=` — an invitee makes their own account
│   ├── ResetPassword.vue← `/admin/reset?t=` — set a new password from the emailed link
│   └── apply/           ← `/apply/*` — volunteer portal (see Routing)
├── composables/usePortal.js ← portal branding, portal_get / portal_post
├── assets/portal-theme.css ← bsidescharm.org's look for the volunteer pages, under html.bsc-portal
├── assets/main.css      ← Global styles
├── lib/colors.js        ← Category chip colors: norm_color / text_on / CAT_FALLBACK
├── lib/passkey.js       ← Organizer passkeys: passkey_sign_in / passkey_register (lazy WebAuthn lib)
├── lib/checkin_timing.js← Words for a check-in's early/late result, shared by both check-in UIs
└── components/
    ├── ScheduleView.vue     ← Day tabs (horizontal) + time sidebar (vertical) + Timeslot grid
    ├── Timeslot.vue         ← Single position chip; category color in, glows green (checked-in) / pulses red (past+empty)
    ├── TimeslotDialog.vue   ← Assign/clear volunteer, check-in, ratings, slot notes
    ├── AddVolunteerForm.vue ← Inline manual add (Volunteers tab), RSA-OAEP email encryption in the browser
    ├── EmailKeyManager.vue  ← Upload public key, decrypt email in-browser with private key
    ├── AppSettings.vue      ← App title and logo, plus EventSettings
    ├── EventSettings.vue    ← Event timezone; Rotate key (voids every check-in code)
    ├── VolunteerCheckin.vue ← Inline (Schedule tab): scan schedule-email codes; early/late confirm
    ├── EmailTemplate.vue    ← Call for Volunteers → Schedule Email: Markdown, images, live preview, package
    ├── ApiTokens.vue        ← Admin → API Keys: create (shown once), list, revoke
    ├── UserManager.vue      ← List/add/role/password/email/delete users, clear passkeys, invites (organizer-only)
    ├── PortalLayout.vue     ← Volunteer pages' frame: logo header, title band, footer (Landing, ApplyShell)
    ├── ShiftPicker.vue      ← The two-shifts-plus-extras picker, shared by signup and ApplyMe editing
    ├── ShiftFlags.vue       ← Banner over the schedule: shifts needing a replacement (Open), and dropped shifts (Unassign / Keep)
    ├── ShiftDemand.vue      ← Per timeslot: slots, open, offered (with overage), picks, remaining, ratio
    ├── VolunteerList.vue    ← Volunteers tab table: every volunteer, state, picks, answers, schedule, optional decrypted Email column
    ├── PendingVolunteers.vue ← Volunteers tab section: signups awaiting approval; approve (badge #) / make inactive
    ├── ProfileDialog.vue    ← Own account: name, role, change own password and email; links to Passkeys
    ├── MyPasskeys.vue       ← Own passkeys: add (needs password), list with manager + algorithm, rename, remove — any role
    ├── BarcodeScanner.vue   ← Camera barcode scanner (lazy ZXing) + manual entry fallback
    ├── RadioCheckout.vue    ← Inline (Radios tab): issue a radio to a volunteer or a typed name
    ├── RadioCheckin.vue     ← Inline (Radios tab): scan-and-go returns; stays open for a run
    ├── RadiosList.vue       ← Radios tab: checked out (per-row check-in), available, and the log
    ├── radio_time.js        ← format_elapsed / format_when for the radio dialogs
    ├── ScheduleEditor.vue   ← Tabbed dialog; loads /api/schedule/structure once, fans it out
    ├── schedule-editor/     ← One component per tab, sharing editor.css
    │   ├── EditorCategories.vue  ← Reorder and recolor categories
    │   ├── EditorDays.vue        ← Days; add row picks the day's times + fills positions
    │   ├── EditorTimes.vue       ← Times; a checkbox per day sets which days run each time
    │   ├── EditorGrid.vue        ← Live counts grid: positions × day/time, applied as a diff
    │   └── time_keys.js          ← derive_time_key / format_time_str, shared with the builder
    └── setup/               ← Wizard steps (create admin, login, title, logo, schedule, volunteers)
        ├── ScheduleSetup.vue     ← Offers the builder or the CSV import
        └── BuildSchedule.vue     ← 4-step builder: days → categories → times → counts grid
```

## Routing

One build serves two sites (`default.conf`). nginx's `try_files … /index.html` hands every
unknown path to the SPA, and `router.js` decides:

- **Port 8080, the admin hostname** (`ADMIN_PUBLIC_URL`, behind the edge's access gate): every route
  below, `/api/portal/*` to the portal and the rest of `/api/*` to the backend. A hard load of
  `/` is a 302 to `/admin` (`absolute_redirect off`, so the Location doesn't carry nginx's
  own port), so organizers land on the sign-in, not the call for volunteers.
- **Port 8082, the public hostname** (`PORTAL_PUBLIC_URL`): `/` and `/apply/*` only. `/admin*`
  and every `/api/*` outside `/api/portal/` are a bare 404 from nginx, so nothing public
  reaches the backend. The admin JS chunk is still downloadable from `/assets/`; it holds no
  secrets and every call it would make 404s.

| Path | View | Talks to |
|---|---|---|
| `/` | `Landing.vue` | `/api/portal/branding`, `/api/portal/landing`, `/api/setup_status` |
| `/apply/` | `apply/ApplyHome.vue` — sign up / sign in cards | — |
| `/apply/signup` | `ApplySignup.vue` — email → inbox → about you → shifts → additional questions → review | `signup/*`, `timeslots`, `logout` |
| `/apply/login` | `ApplyLogin.vue` | `POST /api/portal/request_link` |
| `/apply/inbox` | `ApplyInbox.vue` — resend with cooldown, polls `me` | `request_link`, `me` |
| `/apply/verify?t=` | `ApplyVerify.vue` — the magic link lands here | `POST /api/portal/verify` |
| `/apply/me` | `ApplyMe.vue` — status, scheduled shifts, picks, details; **edit** shifts and editable answers | `me`, `me/update`, `timeslots`, `signup/fields`, `logout*` |
| `/admin` | `Admin.vue` — the organizer app, unchanged | `/api/*` |
| `/admin/invite?t=` | `AcceptInvite.vue` — username, password, optional email; signs in | `invites/check`, `invites/accept` |
| `/admin/reset?t=` | `ResetPassword.vue` — new password; then sign in fresh | `password_reset/check`, `password_reset/complete` |

- **`/admin` must stay lazy.** It carries the editors and the ZXing loader; the landing page is
  the only view in the main bundle, so a volunteer on conference wifi never downloads them.
- **API paths are absolute.** From `/apply/login`, `./api/x` resolves to `/apply/api/x`. Every
  call is `fetch('/api/...')`, and `vite.config.js` uses `base: '/'` for the same reason.
- **The apply views call the portal only.** `/api/portal/*` goes to `portal:5001`; nothing
  under `views/apply/` should ever call a scheduler endpoint.
- **The reverse holds too:** `/api/portal` is a plain prefix match, so a *backend* route
  there is unreachable. The organizer's portal dialog (`PortalStatus.vue`) calls
  `/api/admin/portal/*` for that reason.
- **`AcceptInvite` and `ResetPassword` do nothing on load either** beyond a read-only GET
  check; the token is spent only by their button's POST. Both leave the token out of
  history (`router.replace`).
- **`ApplyVerify` does nothing on load.** Mail scanners GET the link; only its button POSTs the
  token, and it leaves with `router.replace` so the token is not in history.
- The landing page has no menu: its two buttons are the only ways on. It `replace`s itself
  with `/admin` whenever `setup_status` answers ok, which is only on the admin host — the
  public host 404s it. That covers in-app navigation to `/` (the router's catch-all, the
  `/apply/` logo link), which nginx's redirect never sees, and it's how a fresh install's
  owner reaches the first-run wizard. A 502 or network error leaves the page showing.
- The address typed on `/apply/login` is kept in `sessionStorage` (`bsc_apply_email`) for the
  inbox page's resend button — not in the URL, so it stays out of history and logs.

**Two origins, one build.** `plans/vol_signup.md` §1 planned a separate `portal-frontend` on
its own origin with no route to the backend. The public hostname now gives that: script on a
public `/apply/` page runs on an origin whose nginx has no route to the organizer API. The
admin hostname still serves `/apply/` too, on the organizer API's origin, but only to people
past the access gate.

## Volunteer pages theme

`/` and `/apply/*` match bsidescharm.org (WordPress, Astra): white page, light grey header bar
with the logo, a dark slate title band, `#F2F5F7` panels with a thin blue border, a dark
footer; blue `#0274be` buttons and links, hovering to `#3a3a3a`; Montserrat 600 headings and
Source Sans 400 body text at 17px. The organizer app keeps Noir dark.

- **The switch is a class on `<html>`**: `bsc-portal` for the volunteer pages, `dark-app` for
  `/admin*`. `index.html` sets it before first paint (no flash either way) and
  `router.afterEach` keeps it right on navigation. Not a wrapper element, because PrimeVue
  overlays (Select menus, dialogs) teleport to `<body>`.
- **`assets/portal-theme.css` overrides PrimeVue's semantic tokens** (`--p-primary-*`,
  `--p-text-color`, highlight) under `html.bsc-portal`, which outranks the `:root` they're
  emitted on; component tokens reference them, so buttons, focus rings and checkboxes follow.
  Its own `--bsc-*` variables carry the site's colors for the layout.
- **`components/PortalLayout.vue`** is the frame (header, optional `band`, footer) for both
  `Landing.vue` and `apply/ApplyShell.vue`. The header has no Volunteer / Sign in links (the
  pages have their own buttons), only the conference's website (by host name), plus
  **Sign out** when `signed-in` is set: ApplyShell passes the route's `meta.signedIn`, which
  only `/apply/me` has.
- **The conference's contact details** (`contact_email`, `conference_url`,
  `code_of_conduct_url`) come from `/api/portal/branding` via `usePortalBranding()`, set by
  organizers in the wizard's first step or App Settings (`setup/SetTitle.vue`, which sends
  them with `with_contact=true`). `PortalLayout`'s header and footer, `Landing.vue`'s notes
  and `ApplySignup.vue`'s expired-invite card use them, and leave out whatever is `''`. Never
  hard-code a conference's address or links again: that's what made the project single-use.
- **Two logos**, both uploaded in Admin → App Settings (`setup/SetLogo.vue`, `variant`):
  - **Theme Logo** (`/api/app_logo`) — the year's theme, the primary one. The favicon is
    drawn from it, `Landing.vue` shows it large above the intro, and `PortalLayout` lays it
    behind every volunteer page as a fixed watermark (`.portal-watermark`, opacity 0.06,
    `pointer-events: none`, under the header and footer bars).
  - **Header Logo** (`/api/app_header_logo`) — upper left of the volunteer pages, like
    bsidescharm.org's. No favicon. Without one the header falls back to the theme logo, then
    the title. The first-run wizard asks only for the theme logo.
  - Both are sent as a **file upload** (the `File`, not its data URL, which only drives the
    preview), capped at 2 MB and checked when the file is picked; Remove posts `remove=true`
    (`backend/CLAUDE.md` → Logos).
- **Fonts are self-hosted** (`@fontsource/montserrat`, `@fontsource/source-sans-3`, imported
  in `main.js`): volunteers' browsers never call Google from this site, and the organizer
  app never downloads them, since a `@font-face` loads only where it's used.
- **The landing copy is the organizers'** (Volunteer Portal → Landing page,
  `components/portal-content/LandingEditor.vue`). `Landing.vue` fetches
  `/api/portal/landing` and inserts its `intro`, `main` and `sidebar` with `v-html`: the
  portal renders that Markdown with raw HTML and images off and only web/email links
  (`portal/CLAUDE.md` → Editable content), so it's the one place `v-html` takes server text.
  The cards (`roles`) are data. The buttons stay in the page. The intro's first paragraph is
  styled as the lead. The event date is left out on purpose; `/apply/` already says whether
  signup is open.
- **The content editors** are tabs in the Volunteer Portal dialog (`PortalStatus.vue`):
  `portal-content/LandingEditor.vue`, `QuestionnaireEditor.vue` and `InviteEditor.vue`,
  sharing `composables/usePortalContent.js` (one fetch of `/api/admin/portal/content`). Each
  tab mounts on first view and stays, so switching tabs keeps unsaved edits. A question's key
  and an option's value come from its label when added and never change afterwards: answers
  are stored under them. Write a literal `{{ … }}` in these templates with `v-text`, since a
  `}}` ends Vue's own interpolation.
- `maintenance/index.html` (root `CLAUDE.md` → Maintenance page) repeats the same look
  standalone, with its own copies of the logo and fonts.

## Schedule editor

Four tabs: Categories, Days, Times, Positions. `ScheduleEditor.load()` fetches
`/api/schedule/structure` and passes `days`, `times`, `day_times`, `positions`, `slots`, and
`categories` down as props; every child emits `reload` after a write instead of mutating
shared state.

### The counts grid (`EditorGrid.vue`)

The Positions tab is the setup builder's step-4 grid made live — rows are categories, columns
are the day/time pairs that day actually runs, cells are how many of that category's positions
belong there. It replaced three tabs: Positions (individual rows), Slots (per-slot add/remove)
and Bulk Add.

Unlike the rest of the editor, which saves on every action, **the grid batches**: edits stay
local and colored until **Apply Changes** posts the whole thing to `/api/schedule/apply_grid`.
That is the part of the builder worth keeping — you can rough out a whole day and look at it
before committing.

A cell's current value is read straight off the live schedule (count the category's slots in
that cell), so there is no separate state to keep in sync. `pending` previews what Apply would
do, including how many of the removals are blocked by assignments — the backend refuses those,
so it is better to say so before the click than after.

Per-row **Numbered / Named** modes carry over from the builder and are how a position gets
renamed now that the Positions tab is gone: a named category's list becomes its positions'
`pos_str`, in order, and the textarea is pre-filled with the names already in use so switching
to Named is an edit rather than a wipe. A numbered category never renames an existing
position.

The mode a row opens in is **inferred, not defaulted** (`detect_mode`). A category counts as
numbered only when every position is called exactly `{category} {n}`, in order — names that
say nothing beyond the count. Anything else opens as named with the list visible. The rule is
strict on purpose: a real display name must never be hidden behind a toggle. `mode_override`
remembers a row flipped by hand, so applying changes does not snap it back when the props
reload.

`EditorPositions.vue`, `EditorSlots.vue` and `EditorBulkAdd.vue` are no longer imported
anywhere. They are kept on disk for now rather than deleted.

`day_times` is the day×time grid — which times each day actually runs. It drives the checkbox
columns on the Times tab, the time filter on the Slots tab, and the "how many slots will this
make" preview in Bulk Add. Toggling a cell calls `set_day_time`, which refuses when the pair has
assigned volunteers; the child reloads either way so a refused checkbox snaps back.

`fill_all` (pre-checked) on the Days, Times, and Positions add rows creates the slots at once.
It is sent on the **add** path only — those `*_save()` functions are shared with inline editing,
and relabeling a row must not create slots.

## Schedule builder

`ScheduleSetup.vue` sits at the wizard's `import_schedule` step and offers two routes: build a
schedule here, or upload a CSV (`ImportSchedule.vue`, unchanged). `BuildSchedule.vue` is a
four-step `Stepper` — days, categories, times, then a counts grid — and holds **all state
locally until the final Create Schedule**, which POSTs one `/api/schedule/build` call. Stepping
back to rework an answer therefore never leaves a half-written schedule in the database.

The counts grid is the point of the whole thing: rows are categories, columns are the day/time
pairs that day actually runs, and each cell says how many of that category's positions get a
slot there. A category is *Numbered* (auto-labelled `Security 1`, `Security 2` …) or *Named*
(an explicit list, one per line, where a count of 4 means the first 4 on the list — this is how
room monitors work).

## Category colors

Each category carries its own chip color (`categories.cat_color`, `#rrggbb`). It arrives on
the category — not on every position — in both `/api/schedule/structure` (as `cat_color`) and
`/api/get_schedule` (as `color`), and `ScheduleView` passes it to `Timeslot` as `:color`.

**Only the background is stored.** `lib/colors.js` derives the text color from it by relative
luminance, so the editor needs one control per category and no one can pick an unreadable
pair. `DARK_TEXT_ABOVE` sits at 0.30 rather than the 0.179 point where black and white
contrast equally: 0.179 would put black text on orangered, and Security had white there
before colors were configurable. Lowering it to 0.179 maximizes contrast at the cost of
flipping Security's text.

`Timeslot` styles the chip inline. The four `.Security` / `.Floater` / `.Registration` /
`.Room` rules it used to carry are gone — a category named anything else used to render with
no background at all. `pos.category` is still on the class list as an inspection hook, but it
no longer carries color.

Both places that pick a color (`EditorCategories`, `BuildSchedule`) use PrimeVue's
`ColorPicker`, which works in **bare hex with no `#`** — hence the `.slice(1)` going in and
`norm_color()` coming back out. `upsert_category` accepts either form.

## Time keys

`schedule-editor/time_keys.js` derives a `time_key` that no other range already claims. Keys
stay bare `HHMM` starts where possible; when a *different* range already holds that start —
Saturday 1500-1900 vs Sunday 1500-1700 — the end time joins the key (`1500-1700`). Both
`EditorTimes.vue` and `BuildSchedule.vue` use it, and both surface the derived key next to the
add row so a disambiguated key is never a surprise. `EditorTimes` also sends `create: true`,
which makes the backend reject a collision rather than relabel the existing shift.

## Radios

The **Radios tab** (`views/admin/RadiosTab.vue`), for everyone: **Check Out** and **Check In**
buttons at the top for organizer and checkin roles, each expanding its form inline, then
`RadiosList.vue` — checked out (with per-row check-in), available, and a log of every checkout
and return. The log is built from `GET /api/radios` (open checkouts) plus `radios/history`
(completed, latest 500); there's no log endpoint.

**The forms mount only while open**, and `BarcodeScanner` starts the camera on mount and stops
it on unmount, so the camera runs only after someone presses a button — never just because the
tab is showing. Keep the forms behind their `v-if`.

`BarcodeScanner.vue` is shared by the checkout and checkin forms. It `import()`s
`@zxing/browser` and `@zxing/library` **dynamically**, which keeps ~460KB of decoder out of the
main bundle until someone opens a scanner — keep it that way. It always renders a manual entry
field beside the camera: that field is the fallback when permission is denied or no camera
exists, and it is also how a USB or Bluetooth wedge scanner is used.

The camera needs a secure context, and so does the `crypto.subtle` use under **Email
encryption** below — outside one, `crypto.subtle` is `undefined` and encryption fails outright
rather than degrading. nginx serves plain HTTP on 8080 and the edge terminates TLS, so the
secure context comes from the URL the browser used:

- `http://localhost:8080` (local dev) — secure context; localhost is exempt from the HTTPS rule.
- `https://<admin hostname>` (deployment) — secure context with the edge's
  trusted cert. That is what iOS Safari needs: it refused `getUserMedia` behind the old
  self-signed cert.
- `http://<host-LAN-IP>:8080` — **not** a secure context. Camera and `crypto.subtle` both go
  away. The host publish is bound to 127.0.0.1 precisely so nobody reaches the app this way by
  accident.

Manual entry remains the fallback whenever permission is denied, no camera exists, or a USB or
Bluetooth wedge scanner is in use.

Radio writes broadcast a `radio` SSE event; `Admin.vue` forwards it to the Radios tab's
`reload()`.

## Volunteer check-in

**Check In Volunteer** on the Schedule tab opens `VolunteerCheckin.vue` inline, for organizer
and checkin roles. It scans the per-shift QR code from the schedule email
(`POST /api/checkin/scan`; `backend/CLAUDE.md` → Shift check-in) with the same
`BarcodeScanner` the radios use, and mounts only while open for the same camera reason.

More than 45 min early or 10 min late comes back as `needs_confirm`: the panel holds that scan
and shows **Check in anyway** / **Skip**, ignoring further scans until one is pressed so the
question can't scroll away. `TimeslotDialog`'s **Check In** button gets the same prompt, since
`checkin_vol` applies the same rule. Wording for both is `lib/checkin_timing.js`.

## Schedule email

`EmailTemplate.vue` (Call for Volunteers → Schedule Email) edits the template that
`tools/vol_mail` sends. The preview is rendered by the server from the editor's current text,
saved or not, about 700 ms after typing stops, as a chosen volunteer with their real shifts
and codes. It's shown in an `<iframe sandbox="">` with no permissions, on white whatever the
theme. **Download package** zips the *saved* template, so it's disabled while there are
unsaved changes. The placeholder hint under the shift block uses `v-pre`: a literal
`{{shifts}}` inside a Vue template otherwise ends the mustache early.

## Passkeys

Organizer accounts can sign in with a passkey as well as a password (`backend/CLAUDE.md` →
Passkeys). `setup/Login.vue` shows **Sign in with a passkey** only when the browser has
WebAuthn and `GET /api/passkeys/config` says `enabled`. **Passkeys** opens from Profile's
**Manage Passkeys** (user menu → Profile), which every role has.

`lib/passkey.js` `import()`s `@simplewebauthn/browser` (pinned exactly) on first use, so it
stays in its own chunk. A cancelled browser prompt throws an error with `cancelled: true`,
which callers swallow rather than show. The passkey list shows each one's passkey manager
(from its AAGUID, when known) and algorithm, with ML-DSA tagged post-quantum — the only way to
tell what a given authenticator actually made. The name field is optional and starts empty:
the server names a blank one after the passkey manager. Rename is inline, one row at a time.

## Signup (`views/apply/ApplySignup.vue`)

The questions are **not** in this file: they come from `GET /api/portal/signup/fields`
(organizer-edited; the default is `portal/content.py`), in that list's order, on the step
their `section` names: `shifts` under the shift picker on step 2, `additional` on step 3. They
are rendered by type (`choice`, `select`, `ack`, `text`; a
choice option may carry a `detail` text field). The draft lives in `localStorage`
(`bsc_signup_draft`) as timeslot **keys** (`day_time`), so the tab the mail link opens continues
where the first left off, and picks that are no longer offered are dropped with a note. Each
timeslot lists the keys it `overlaps`, and a choice that would clash with the first pick is
greyed out. The inbox step polls
`signup/session` until the link is clicked. A 409 whose `detail.full` lists timeslot keys sends the
volunteer back to the shifts step with those picks removed.

**Approved, cancelled and inactive are separate** (`backend/CLAUDE.md`). `ApplyMe` shows
Inactive, Cancelled, Approved or Awaiting approval, in that order of precedence, offers
**Cancel my registration** while live, and **Rejoin** (the edit form, posting to `me/rejoin`
with nothing held) while cancelled. `VolunteerList` filters by the same derived state and has
**Make inactive** / **Reinstate**, and for inactive volunteers **Purge** (each row) and
**Purge inactive (N)** (toolbar): one confirm dialog that lists what goes and needs the vol_id,
or the count, typed (`POST /api/admin/volunteers/purge`). A slot whose volunteer has cancelled or is inactive keeps its
assignment and is marked: `Timeslot.vue` draws a dashed amber outline and NEEDS REPLACEMENT;
the banner's **Open** calls `ScheduleView`'s exposed `open_slot_by_key`, and `TimeslotDialog`
opens such a slot straight to the volunteer picker.

**One picker for both.** `components/ShiftPicker.vue` holds the overlap, full and extras
logic for signup's shifts step and for editing on `ApplyMe`, so the two can't drift. Editing
passes `held` (current picks stay pickable when full) and `scheduled` (tagged, and `ApplyMe`
warns before a scheduled shift is dropped). Save is disabled until there are exactly two.

**Early-access invites.** `SignupInvites.vue`, in the Volunteer Portal dialog, makes a
single-use link (shown once, with Copy), lists invites with their state, and revokes. The
link is `/apply/signup?invite=…`: `ApplySignup` moves the token to `sessionStorage`
(`bsc_apply_invite`) and off the address bar, asks `signup/invite`, and while signup is
closed shows the email step ("You've been invited to sign up early") for a valid invite or
`invite_gone` for a used, expired or revoked one. It sends `invite` with `check_email`; after
that the portal carries it, so the form step needs nothing.

**Send invites by email** (same section) takes `Name, email` lines, checks them in the
browser (up to 200), and sends one every 10 s with a progress bar, Pause/Resume and Stop. The
loop is `composables/useInviteBatch.js`, **module-level** so it keeps going when the dialog
closes (PrimeVue unmounts dialog contents) and shows its progress on reopening; closing the tab
stops it, after a `beforeunload` prompt. A 429 (early, or the portal's hourly limit) is waited
out per `Retry-After` and retried, not failed. Failed and unsent people can be put back in the
box to try again. Each send makes its own invite server-side, so a stopped batch leaves no
unmailed invites.

`ApplyHome` disables **Get started** unless `signup/status` is `open`. Organizers open signup
with the switch in **Admin → Volunteer Portal** (`PortalStatus.vue`), which lists what's
missing (email key, shifts with space) when it can't take anyone yet, shows the shifts as
volunteers see them, and holds the **Overbook by N%** setting; and approve in **Admin → Pending
Volunteers**, whose count is a badge on the Admin button.

## API calls

All requests use `fetch('/api/...')` — absolute; see Routing — with `FormData` bodies, no JSON
request bodies anywhere. The apply views use `portal_get` / `portal_post` from
`composables/usePortal.js`, which never throw and return `{ ok, status, body }`.
Errors come back as FastAPI `{"detail": "..."}`; components read `body?.detail`.
Schedule state lives in `App.vue` and refreshes off the `/api/events` SSE stream
(`composables/useEvents.js`), so a write in one browser updates the others.

## Reverse proxy and client IP

The edge terminates HTTPS, the tunnel client on the host dials the loopback publish, then nginx
proxies `/api` to `backend:5000` (and `/api/portal` to `portal:5001`, the same way — see
`portal/CLAUDE.md`, auth invariant 3). nginx resolves the real client with the realip module
from the **edge's client-IP header** (named in `default.conf`), which the edge sets and
overwrites, and forwards a single `$remote_addr` value instead of a chain. Not
`X-Forwarded-For`: the edge appends to that one and keeps whatever prefix the client sent. `audit()` in `backend/db.py` reads the leftmost
entry, which is that one trusted value.

Keep the trusted sources in `default.conf` to loopback and the container networks — wherever
the tunnel client's connection appears from after podman's port forward. Only the host and the
compose containers can reach those ports. `GET /whoami` (admin server only) prints the
resolved address and the raw headers for checking this through the live tunnel.

## Dialog patterns

Two patterns in use — pick the one that fits:

1. **Prop-triggered** — parent changes a prop; component `watch`es it and opens.
   Example: `TimeslotDialog` watches `:pos`; parent sets `selected_pos.value = {...slot}`.
   Re-triggering the same slot requires `selected_pos.value = null` then `setTimeout(..., 0)`.

2. **Exposed method** — parent holds a `useTemplateRef` and calls `ref.open()`.
   Example: `EmailKeyManager.open()`, `UserManager.open()`.

Never mutate props. Spread-copy into a local `ref`: `const local = ref({ ...props.pos })`.

## Role gating

`:role` prop is passed from `App.vue` down to components that gate write operations.
`Admin.vue` is a toolbar over three tabs.

| Toolbar menu | Items | Roles |
|---|---|---|
| Call for Volunteers | Volunteer Portal, Shift Demand, Schedule Email | organizer |
| Admin | Users, App Settings, Email Key, API Keys | organizer |
| *username* (right) | Profile, Sign Out | all |

| Tab | Content | Roles |
|---|---|---|
| Schedule | **Edit Schedule** (organizers), **Check In Volunteer** (organizer, checkin), the replacement / dropped-shift banners, the schedule | all |
| Volunteers (pending badge) | `VolunteersTab.vue`: Add Volunteer and Decrypt emails, then awaiting approval, then the list | organizer |
| Radios | `RadiosTab.vue`: Check Out / Check In, then out, available, log | all (forms for radio roles) |

The last tab is remembered per browser (`bsc_admin_tab`); a non-organizer is moved off
Volunteers once their role is known. The banner's **Open** switches to Schedule first.

**Bulk decrypt** (Volunteers tab): the organizer pastes or picks the private key, and
`lib/email_crypto.js` decrypts every volunteer's `email_enc` (sent by
`GET /api/admin/volunteers`) in the browser. The key is a non-extractable `CryptoKey` held in
`VolunteersTab`'s memory only — never stored or sent — and **Forget key** or leaving the page
drops it; the PEM text is cleared as soon as it's imported. A key under which nothing decrypts
is discarded with a "wrong key?" message. `EmailKeyManager` uses the same helpers.

**Profile** (`ProfileDialog.vue`) shows who's signed in and changes their own password
(`POST /api/users/change_password`, current password required) and email
(`users/change_email`, also needing the current password; the email is only where reset links
go). Resetting someone else's password, or setting their email, is still UserManager's.

**Sign-in** (`setup/Login.vue`) has **Forgot password?**, which posts a username to
`password_reset/request` and shows the server's reply as given: it's the same whether or not
the account exists or has an email, so the page must not add its own "not found".

**Users → Invite Someone** (`UserManager.vue`) creates an invite for a role and an optional
address. The link is shown once with Copy (the server keeps only its hash) and says whether
it was mailed; outstanding invites are listed with Revoke.

`RoleChecker` on the backend enforces the same rules server-side.

## Email encryption

- Public key stored server-side in `app_kv` under key `email_pubkey` (SPKI PEM format).
- Encryption: `crypto.subtle.importKey('spki', pem_to_der(pubkey), {name:'RSA-OAEP', hash:'SHA-256'}, false, ['encrypt'])` → `crypto.subtle.encrypt(...)` — all browser-side.
- Decryption: user pastes PKCS#8 private key into `EmailKeyManager`; `crypto.subtle.importKey('pkcs8', ...)` → `crypto.subtle.decrypt(...)`. Private key never sent to server.
- Key generation: `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096` (PKCS#8 private) + `openssl pkey -pubout` (SPKI public). `openssl genrsa` produces PKCS#1 which Web Crypto cannot import.

## Emits convention

Kebab-case: `schedule-updated`, `vol-dropped`, `vol-added`.
