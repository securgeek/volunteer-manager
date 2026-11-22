# Getting started

This page takes you from a fresh clone to the whole stack running on your machine. It then
covers the tests, the lint and these docs. Deploying to the real server is in
[Deployment](operations/deployment.md).

## What's in the repo

| Directory | What it is | Runs as |
|---|---|---|
| `backend/` | The scheduler API (FastAPI, PostgreSQL). Organizers' data and every write. | `backend` container |
| `portal/` | The volunteer portal API (FastAPI, SQLite). Signup and volunteer sign-in. | `portal` container |
| `frontend/` | The Vue 3 site. One build serves the call for volunteers, the portal pages and the organizer app. | `frontend` container (nginx) |
| `mailrelay/` | Sends the portal's mail through Microsoft Graph. Standard library only. | `mailrelay` container |
| `tools/vol_mail/` | Sends schedule emails from an organizer's own machine. | not a container |
| `maintenance/` | The "back soon" page shown while the stack is down. | its own compose project |
| `docs/` | This site. | not deployed |

[Services](services/backend.md) has one page per service, and the
[interaction diagrams](architecture/interactions.md) show how they talk to each other.

## Run the stack

You need podman with podman-compose, or Docker with Compose.

1. Copy the settings file and fill in the secrets:

    ```bash
    cp .env.example .env
    openssl rand -hex 32      # once each for EMAIL_HMAC_KEY and SYNC_SECRET
    ```

    The portal refuses to start without `EMAIL_HMAC_KEY`, `SYNC_KEY_ID` and `SYNC_SECRET`.
    Leave `MAIL_BACKEND=file` for now: sign-in links are then written as `.eml` files to
    `logs/portal_mail/` instead of being sent. Every setting is described in
    [Configuration](operations/configuration.md).

2. Build and start everything:

    ```bash
    podman-compose up -d --build        # or: docker compose up -d --build
    ```

3. Open <http://localhost:8080>. The admin site sends you to the setup wizard, which creates
   the first organizer account and then sets up the schedule and the volunteer roster. The
   [organizer guide](guides/organizer.md) walks through it.

Port 8080 (`FRONTEND_PORT`) serves the whole site. Port 8082 (`PORTAL_SITE_PORT`) serves
only the public pages, as the public hostname does once deployed. Both listen on 127.0.0.1
only.

The mail relay exits at startup until its Graph settings are filled in. That's expected
with `MAIL_BACKEND=file`, and nothing else depends on it.

### Volunteer email addresses

Addresses are encrypted in the browser to an RSA key pair that only organizers hold. Signup
stays closed until the public key is uploaded. To try signup locally, make a throwaway pair
and upload `public.pem` under **Admin → Email Key**:

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out private.pem
openssl pkey -in private.pem -pubout -out public.pem
```

Both files are ignored by git and kept off the server. [Secrets](operations/secrets.md)
explains why.

### The frontend dev server

To work on the frontend with hot reload, keep the containers running and start Vite:

```bash
cd frontend && npm install && npm run dev
```

Vite sends `/api/portal/*` to the portal and the rest of `/api/*` to the backend
(`vite.config.js`).

## Tests

The backend and the portal are separate applications, and both have modules named `db` and
`main`, so each suite runs in its own pytest process:

Work in a virtual environment. `requirements-dev.txt` pulls in the backend's requirements
too, since some tests and the docs build import the backend:

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements-dev.txt
./run_tests.sh                     # portal, backend and vol_mail, one process each
(cd mailrelay && python3 -m pytest)
```

Most backend tests need a real PostgreSQL, and they **drop the target database's schema**.
Point them at a throwaway one:

```bash
podman run -d --rm --name bsides-testpg -p 127.0.0.1:55432:5432 \
  -e POSTGRES_DB=scheduler_test -e POSTGRES_USER=scheduler -e POSTGRES_PASSWORD=test \
  docker.io/library/postgres:18-alpine

TEST_PG_HOST=127.0.0.1 TEST_PG_PORT=55432 TEST_PG_DB=scheduler_test \
TEST_PG_USER=scheduler TEST_PG_PASSWORD=test ./run_tests.sh
```

Without `TEST_PG_HOST`, those tests are skipped. The tests need no network, no mail server
and no browser. Graph, the portal's side of sync and passkey authenticators are faked.

## Lint and docs

```bash
./run_lint.sh              # pylint per app, bandit, and mkdocs build --strict (in the venv)
./run_lint.sh --no-docs    # also --no-pylint, --no-bandit; --todo lists TODO/FIXME
mkdocs serve               # this site at http://127.0.0.1:8000
```

The lint is strict about a few things, and the rest is a score that may only go up.
[Quality & security](development/quality-and-security.md) explains the rules. Two of them
come up in almost every change:

- **SQL is always a complete literal** with bound parameters. Nothing builds SQL text, not
  even from constants.
- **Docstrings are Google style** (`Args:`, `Returns:`, `Raises:`). The
  [code reference](reference/backend/main.md) is built from them, and pylint checks that a
  documented parameter list matches the signature.
