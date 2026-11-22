# Configuration

Every setting comes from `.env` in the repo root. podman-compose reads it, and
`compose.yml` passes each service the variables that service needs. Start from
`.env.example`, which has the same settings with comments.

`.env` holds secrets. It's ignored by git and must never be committed. Secret settings are
marked **secret** below, and [Secrets](secrets.md) covers how each one is generated and rotated.

## Not here: the conference's details

The conference's name, contact email, website and code-of-conduct link are set in the app,
not in `.env`: in the setup wizard, and later under **Admin → App Settings**. They're pushed
to the portal with the logos, and the volunteer pages show them.

## Database

| Setting | Read by | Default | What it does |
|---|---|---|---|
| `POSTGRES_PASSWORD` | postgres, backend | `changeme` | **Secret.** The `scheduler` role's password. compose gives the backend the same value as `DB_PASSWORD`. |

The backend also reads `DB_HOST`, `DB_PORT`, `DB_NAME` and `DB_USER`. compose sets them to
the `postgres` service, so they don't belong in `.env`. The portal's SQLite file is
`PORTAL_DB`, set to `/data/portal.db` on the `portal_data` volume.

## Shared between the backend and the portal

These must be **identical** on both sides. The backend and the portal compare
fingerprints on every sync heartbeat, and the admin site's **Volunteer Portal** dialog shows
a mismatch.

| Setting | Default | What it does |
|---|---|---|
| `EMAIL_HMAC_KEY` | none, required | **Secret.** The key behind each volunteer's `email_hmac`, which is how an address is looked up without being stored readable. Never stored in either database. The portal refuses to start without it. **Changing it locks every volunteer out.** |
| `SYNC_KEY_ID` | none, required by the portal | Names the signing secret, e.g. `bsc-2026`. Sent with every sync request. |
| `SYNC_SECRET` | none, required by the portal | **Secret.** Signs every request from the scheduler to the portal. |
| `SYNC_SECRET_PREV` | empty | **Secret.** During a rotation, the old secret, so the portal accepts both until the backend restarts with the new one. |

## Where the services find each other

| Setting | Read by | Default | What it does |
|---|---|---|---|
| `PORTAL_SYNC_URL` | backend | `http://portal:5001` in `.env.example` | Where the backend reaches the portal's sync API, over the compose network. **Empty runs the scheduler with no portal at all**: the sync loop never starts. |
| `PORTAL_PUBLIC_URL` | backend, portal | `http://localhost:8080` | The public site's origin, as volunteers see it. Sign-in links and early-access invite links are built on it. |
| `ADMIN_PUBLIC_URL` | backend, portal | `PORTAL_PUBLIC_URL` | The organizer site's origin: the base of admin invite and password-reset links, and the passkey relying party. **Changing its hostname orphans every passkey.** Leave it unset when one origin serves both, as in local development. |
| `PORTAL_SYNC_INTERVAL` | backend | `30` | Seconds between sync passes. A change calls for a push sooner, after a 2-second debounce. |
| `PORTAL_SYNC_VERIFY_TLS` | backend | `1` | Set to `0` only for a self-signed portal certificate in development. |

## Portal mail

| Setting | Read by | Default | What it does |
|---|---|---|---|
| `MAIL_BACKEND` | portal | `file` | `file` writes `.eml` files to `logs/portal_mail/` and sends nothing. `smtp` sends through `SMTP_HOST`. `relay` hands plain text to the mail relay, which sends through Microsoft Graph. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER` | portal | empty, `587`, empty | The SMTP server, for `MAIL_BACKEND=smtp`. STARTTLS is always used. |
| `SMTP_PASS` | portal | empty | **Secret.** For `MAIL_BACKEND=smtp`. |
| `SMTP_FROM` | portal | `volunteers@example.org` | The From address, for `MAIL_BACKEND=smtp`. The relay sends as `MAIL_SENDER` instead. |

## Mail relay

Used with `MAIL_BACKEND=relay`. Setup is in [Graph mail setup](graph-mail-setup.md).

| Setting | Read by | Default | What it does |
|---|---|---|---|
| `MAIL_RELAY_TOKEN` | portal, mailrelay | none | **Secret.** The bearer token the portal sends with each message. At least 32 characters. |
| `GRAPH_TENANT_ID` | mailrelay | none | The Entra tenant ID. |
| `GRAPH_CLIENT_ID` | mailrelay | none | The app registration's client ID. There is no client secret. |
| `RELAY_MAILBOX` | mailrelay | none | The account that signs in with a device code, and whose Sent Items keep a copy. It needs Send As on `MAIL_SENDER`. `login` refuses to save a sign-in for any other account. |
| `MAIL_SENDER` | mailrelay | none | The From address of everything the relay sends, e.g. `volunteers@example.org`, usually a Microsoft 365 group. **Required**: the relay won't start without it, or with anything but one plain address. Set here rather than in the app because it's tied to the tenant's Send As permission, and a request can never change it. |

The relay also reads `RELAY_PORT` (5002), `RELAY_STATE_DIR` (`/state`),
`RELAY_RATE_PER_MIN` (20) and `RELAY_RATE_PER_HOUR` (300). compose doesn't pass them, so
they keep those defaults.

## Frontend

| Setting | Default | What it does |
|---|---|---|
| `FRONTEND_PORT` | `8080` | Host port for the full site, on 127.0.0.1. The tunnel sends the admin hostname here. |
| `PORTAL_SITE_PORT` | `8082` | Host port for the public pages only, on 127.0.0.1. The tunnel sends the public hostname here. |

Keep both above 1024, since rootless podman can't bind lower ports.

## Deployment

| Setting | Read by | What it does |
|---|---|---|
| `DEPLOY_HOST` | you, for rsync | The server to copy the repo to: `rsync -aF BSides/ "$DEPLOY_HOST:Scheduler/"`. No container reads it. It lives in `.env` so the address isn't in the repo. |

## Portal tuning

The portal reads these, but compose doesn't pass them, so they keep their defaults unless
you add them to the `portal` service's `environment`.

| Setting | Default | What it does |
|---|---|---|
| `PORTAL_TOKEN_TTL_MIN` | 15 | How long a sign-in link works. |
| `PORTAL_SESSION_TTL_HOURS` | 12 | A volunteer session's lifetime. |
| `PORTAL_REMEMBER_TTL_DAYS` | 90 | A "Remember this browser" session's lifetime, rolled forward with use. |
| `PORTAL_RATE_PER_MIN`, `PORTAL_RATE_PER_HOUR` | 5, 30 | Link requests per client address (IPv6 by /64) and per email address. |
| `PORTAL_RESPONSE_FLOOR_MS` | 250 | The minimum time a link request takes, so timing doesn't tell whether an address is registered. |
| `SYNC_SKEW_SEC` | 300 | How far a signed request's timestamp may be from the portal's clock. |

## vol_mail

The schedule mailer runs on an organizer's machine and doesn't read `.env`. Export its
settings in the shell first:

| Setting | What it is |
|---|---|
| `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID` | The same app registration as the relay. |
| `VOL_MAIL_MAILBOX` | The account that signs in. It needs Send As on `MAIL_SENDER`. |
| `MAIL_SENDER` | The From address, as for the relay. `--from` overrides it, and `send` stops without one. |
