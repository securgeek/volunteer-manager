# Deployment

The production site runs on one host under rootless **podman-compose**, reached only
through a **tunnel** from an edge service that terminates TLS and gates the admin site. This page covers how it's laid out, how to ship a change,
and the less routine jobs: the first install, upgrading PostgreSQL and backups.

## Layout

```mermaid
flowchart LR
    v([Volunteers]) --> cf[Edge<br/>TLS]
    o([Organizers]) --> cfa[Access gate] --> cf
    i([Integrators and<br/>tools/vol_mail]) -.->|"/api/export/*<br/>bypasses the gate, bearer key"| cf
    cf --> cfd[Tunnel client<br/>on the host]
    subgraph host [Host: podman-compose]
        cfd -->|127.0.0.1:8082| fe8082[frontend nginx<br/>public pages]
        cfd -->|127.0.0.1:8080| fe8080[frontend nginx<br/>full site]
        fe8082 -->|/api/portal/*| portal
        fe8080 -->|/api/portal/*| portal
        fe8080 -->|"/api/* (incl. /api/export/*)"| backend
        backend --> postgres[(postgres)]
        backend -->|signed sync API| portal
        portal --> pdb[(portal.db)]
        portal -->|mail_internal| mailrelay
    end
    mailrelay -->|mail_egress| msgraph[Microsoft Graph]
```

| Hostname | Host port | Serves | Gate |
|---|---|---|---|
| public (`PORTAL_PUBLIC_URL`) | `PORTAL_SITE_PORT` (8082) | `/`, `/apply/*`, assets, `/api/portal/*`. Everything else is a 404. | public |
| admin (`ADMIN_PUBLIC_URL`) | `FRONTEND_PORT` (8080) | the full site: `/admin` and `/api/*` | the access gate, bypassed for `/api/export/*` |

The real hostnames are in `.env`, not in the repo.

- **Nothing else publishes a port.** The portal, the backend, PostgreSQL and the relay are
  reachable only on the compose networks.
- **Both published ports are loopback-only.** TLS ends at the edge, so nginx serves plain
  HTTP that must not be reachable from the LAN.
- **The export API bypasses the gate** because the servers that call it carry a bearer key
  instead. See [Export API](../api/export.md).
- **nginx takes the client address from the edge's client-IP header**, named in
  `frontend/default.conf`. The portal's rate limits depend on it, so a different edge has to
  send that header, or `default.conf` has to name the one it does send.
- **The mail relay is on two networks**: `mail_internal`, shared only with the portal and
  with no route out, and `mail_egress`, its way to Microsoft.

The tunnel client runs on the host, not in compose, so the stack can be rebuilt without
dropping the tunnel.

## Shipping a change

From the directory above the repo on your machine, copy it to the server:

```bash
set -a; . BSides/.env; set +a        # for DEPLOY_HOST
rsync -aF BSides/ "$DEPLOY_HOST:Scheduler/"
```

`.rsync-filter` keeps keys, development tooling, the docs, `tools/` and the legacy app off
the server. It also protects the server's own files, such as `logs/`, so `--delete` is safe
to add. `.env` is copied, because the containers read it. Then, on the server:

```bash
cd ~/Scheduler
./maintenance.sh on        # stack down, "back soon" page up
podman-compose build
./maintenance.sh off       # maintenance page down, stack up
```

Restart the whole stack this way, not one service at a time. Migrations run by themselves
when the backend and the portal start.

**The maintenance page** is a separate compose project (`compose.maintenance.yml`, project
`bsides-maintenance`) with one nginx on the same two loopback ports, so the tunnel needs no
change. Every page is a 503 with `Retry-After`, and every `/api/*` call is a JSON 503 that the
apps can show. Everything it serves is in `maintenance/`, since nothing else is running.
`./maintenance.sh status` shows which stack is up. The two can't run at once because they
want the same ports.

### Why the restarts work this way

podman-compose turns each `depends_on` into a hard `podman --requires` link. A required
container can't be removed while a dependent exists, and `down` stops containers one at a
time. So only `backend → postgres` keeps a `depends_on`. nginx resolves the backend and the
portal on every request instead, and returns 502 until they're up.

Total stop time is the sum of every container's. That's why the frontend stops on `SIGTERM`
and uvicorn has a 3-second graceful shutdown: without them, open `/api/events` streams held
each container to podman's 10-second kill.

## First install

1. Install podman, podman-compose and your tunnel's client on the host. Create the tunnel,
   with the public hostname pointed at `http://127.0.0.1:8082` and the admin hostname at
   `http://127.0.0.1:8080`.
2. At the edge, put an access gate (organizer sign-in) on the admin hostname, with a bypass
   for the path `/api/export/*`. Check the edge sends the client-IP header that
   `frontend/default.conf` reads.
3. Write `.env` on your machine from `.env.example`.
   [Configuration](configuration.md) describes every setting.
4. Run rsync and the build as above, then `podman-compose up -d`.
5. Open the admin hostname. The setup wizard creates the first organizer.
6. Upload the organizers' public key (**Admin → Email Key**). Signup can't open without it.
7. To send mail for real, set up the relay: [Graph mail setup](graph-mail-setup.md).

## Upgrading PostgreSQL

The official image keeps its data in a directory named for the major version, and refuses
to start on an older major's data. The database is small, so a major upgrade is a dump and
restore into a fresh volume, not `pg_upgrade`. The 16 → 18 upgrade was done on 2026-11-01.

For the next one, add a `pgdata19` volume, point the `postgres` service's image and mount at
it in `compose.yml`, and on the server:

```bash
cd ~/Scheduler
podman-compose stop backend                     # no writes during the dump
podman exec scheduler_postgres_1 pg_dump -U scheduler -Fc scheduler > ~/pg-old.dump
chmod 600 ~/pg-old.dump                         # outside ~/Scheduler: rsync --delete
COUNTS="select table_name, (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I', table_name), false, true, '')))[1]::text::int from information_schema.tables where table_schema='public' and table_type='BASE TABLE' order by 1"
podman exec scheduler_postgres_1 psql -U scheduler -d scheduler -Atc "$COUNTS" > ~/pg-old-counts.txt
podman-compose down
# rsync the new compose.yml, then:
podman-compose up -d postgres                   # wait for (healthy)
podman exec -i scheduler_postgres_1 pg_restore -U scheduler -d scheduler --no-owner --exit-on-error < ~/pg-old.dump
podman exec scheduler_postgres_1 psql -U scheduler -d scheduler -Atc "$COUNTS" | diff ~/pg-old-counts.txt - && echo "row counts match"
podman-compose up -d
```

To roll back, put the old image and mount back in `compose.yml`: the old volume is never
touched. Once the new version has settled, `podman volume rm` the old volume and
`shred -u` the dump, which holds volunteer names and email ciphertext.

## Backups

| What | Where | How |
|---|---|---|
| Scheduler database | `pgdata18` volume | `podman exec scheduler_postgres_1 pg_dump -U scheduler -Fc scheduler > backup.dump` |
| Portal database | `portal_data` volume, `/data/portal.db` | `sqlite3 /data/portal.db ".backup '/data/backups/portal-$(date +%F-%H%M).db'"` inside the container. Never `cp`: with WAL, a copied file can be torn. |
| Relay sign-in | `mailrelay_state` volume | Don't back it up. Run `relay.py login` again instead. |
| Audit log | `logs/audit.jsonl` (bind mount; `audit.log` before the JSON log) | an ordinary file |
| `.env` and `private.pem` | your machine | keep them in a password manager. See [Secrets](secrets.md). |

Both database dumps hold volunteer names and email ciphertext. Keep them owner-only
(`chmod 600`), and don't leave them in `~/Scheduler`, where an rsync with `--delete` would
remove them.
