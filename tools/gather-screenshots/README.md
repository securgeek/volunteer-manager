# gather-screenshots

Retakes the screenshots in the project README (`docs/images/screenshots/`) on a throwaway
local stack filled with fake data, so they can be refreshed after a UI change.

```bash
tools/gather-screenshots/gather-screenshots.sh           # build, seed, shoot, tear down
tools/gather-screenshots/gather-screenshots.sh --keep    # leave the demo running to look around
tools/gather-screenshots/gather-screenshots.sh --down    # tear down a kept demo
```

A full run takes a few minutes, most of it the first image build.

## What it needs

podman with podman-compose, rsync, openssl, curl, Node 18+ with npm, and Chromium or Chrome.
It looks for the browser in the usual Linux and macOS places; set `CHROME=/path/to/browser`
otherwise. The first run installs `puppeteer-core` here (`npm ci`); it drives the browser
you have and downloads none.

## What it does

1. **Copies this checkout** to a temporary directory, without `.env`, `logs/`, `.git` or
   `node_modules`, and writes a fresh `.env` there with newly generated secrets,
   `MAIL_BACKEND=file`, and the two sites on `localhost:18080` (organizer) and
   `localhost:18082` (volunteer).
2. **Starts it as its own compose project**, `bsides-demo`, with postgres, backend, portal and
   frontend (no mail relay). `compose.demo.yml` raises the portal's per-IP rate limits, which
   would otherwise refuse a dozen signups from one address in a few seconds. Your own stack,
   its volumes, `logs/` and ports are never touched.
3. **Runs `shoot.mjs`**, which uses the real UI and API in six phases:

   | Phase | What happens | Screenshots |
   |---|---|---|
   | wizard | Creates the organizer, names the conference, uploads `logo.svg`, builds a two-day schedule in the builder | `setup-*` |
   | portal | Uploads a throwaway RSA key and opens signup | — |
   | signup | One volunteer signs up in the browser; 24 more through the portal API, each following its magic link from the portal's mail files | `portal-landing`, `portal-check-email`, `portal-signup-*` |
   | approve | Approves most through the API, then one in the page with a badge number | `approve-pending` |
   | assign | Assigns volunteers to the shifts they picked, then opens an empty slot's picker | `schedule`, `assign-slot`, `volunteer-list` |
   | volunteer | The browser volunteer signs in again and opens the edit form | `portal-sign-in`, `portal-confirm`, `portal-my-info`, `portal-edit` |

4. **Tears the demo down**, volumes and temporary directory included, unless `--keep`.

All the fake data is in `data.mjs`: an invented conference ("DemoCon 2027") and invented
volunteers, every address `@example.org`. Organizer sign-in on a kept demo is `organizer` /
`demo-password-1`.

## Settings

| Variable | Default | |
|---|---|---|
| `DEMO_OUT` | `docs/images/screenshots` | where the PNGs go |
| `DEMO_PROJECT` | `bsides-demo` | compose project name |
| `DEMO_ADMIN_PORT` / `DEMO_PUBLIC_PORT` | `18080` / `18082` | the two sites' host ports |
| `CHROME` | found automatically | the browser to drive |

## Working on it

`shoot.mjs` can run phases on their own against a kept demo:

```bash
node tools/gather-screenshots/shoot.mjs --admin http://localhost:18080 \
  --public http://localhost:18082 --portal-container bsides-demo_portal_1 \
  --out /tmp/shots --only approve,assign
```

Each phase expects the state the previous one left, and running one twice adds its data
twice, so start from a fresh demo (`--down`, then `--keep`) when in doubt. On a failure the
script saves what the browser showed as `_failed.png` in the output directory.

The organizer app is shot at 1366×860 in its dark theme; the volunteer pages on a phone-sized
screen (390×844, touch and mobile on) in their light theme, since that's how most volunteers
open them. Some are full-page captures, so their height varies.

The demo's images (`localhost/bsides-demo_*`) are kept between runs to make the next build
fast; `podman rmi` them if you want the space back.
