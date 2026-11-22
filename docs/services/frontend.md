# Frontend: the website

One Vue 3 build serves three areas. nginx serves it, and sends API calls on to the backend
and the portal.

| Path | What it is | Talks to |
|---|---|---|
| `/` | The public call for volunteers | the portal (branding) |
| `/apply/*` | The volunteer portal: sign up, sign in, manage a registration | the portal only |
| `/admin` | The organizer app | the backend |
| `/admin/invite`, `/admin/reset` | Accepting an organizer invite; resetting a password | the backend |

Code: `frontend/`. Vue 3 with `<script setup>`, PrimeVue 4 components, Vite and vue-router.
Design notes for each component are in `frontend/CLAUDE.md`.

## Two sites from one build

nginx (`frontend/default.conf`) runs two servers on the same files:

- **Port 8082, the public hostname** serves `/`, `/apply/*`, the assets and `/api/portal/*`.
  Everything else, `/admin` and the organizer API included, is a 404 from nginx, so nothing
  public can reach the backend.
- **Port 8080, the admin hostname** serves everything. A fresh load of `/` there redirects
  to `/admin`, so organizers land on the sign-in page.

Both take the client address from the edge's client-IP header, and pass it on as a single
`X-Forwarded-For` value.

## How the pages work

- **The admin app loads lazily.** The landing page is the only view in the main bundle, so a
  volunteer on conference wifi never downloads the organizer editors or the barcode scanner.
- **The `/apply/` pages call `/api/portal/*` only**, never a scheduler endpoint.
- **API paths are absolute** (`fetch('/api/...')`), because pages live at nested paths.
  Request bodies are `FormData`, not JSON.
- **Link pages do nothing on load.** `/apply/verify`, `/admin/invite` and `/admin/reset`
  spend their token only when the button is pressed, then remove it from the browser
  history.
- **Volunteer email addresses are encrypted and decrypted in the browser.** Adding a
  volunteer by hand encrypts the address to the public key before it's sent. Showing
  addresses in **Admin → Volunteers** decrypts them locally with `private.pem`, which never
  leaves the organizer's machine.
- **Open organizer pages refresh themselves.** The backend sends a server-sent event after
  each change, and the page refetches what it shows.

## Look

The organizer app uses PrimeVue's Aura theme with a custom dark preset. The volunteer pages
use bsidescharm.org's light look (`assets/portal-theme.css`), with the title and logos the
organizers set in App Settings.

## Developing

```bash
cd frontend
npm install
npm run dev        # Vite with hot reload; proxies /api/portal to the portal, /api to the backend
npm run build      # what the container builds
```

The dev server expects the backend and the portal to be running, for example with the
compose stack up. See [Getting started](../getting-started.md).
