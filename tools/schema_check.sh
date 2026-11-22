#!/usr/bin/env bash
# Compare the running databases' schemas with the schema files a fresh install gets.
#
# Since 1.0, a fresh install is backend/scheduler.sql and portal/portal.sql, and an existing
# database follows the per-release files in migrations/. This checks the two agree: that a
# database which got here by upgrades has exactly the tables, columns, types, defaults,
# indexes and constraints a fresh one would. Run it before each release, and after any
# hand-made change to a database.
#
# It reads the live databases only (catalog queries, read-only), and builds the fresh copies
# in a throwaway Postgres container and a temporary SQLite file, both removed afterwards.
#
# tools/ isn't synced to the server, so run it there over ssh, from the repo on your machine:
#
#   ssh "$DEPLOY_HOST" 'cd Scheduler && bash -s' < tools/schema_check.sh
#
# or on any machine with the stack up, from the repo root: tools/schema_check.sh
#
# The containers are found by podman-compose's labels, with the project taken from
# COMPOSE_PROJECT_NAME or this directory's name in lowercase. Override with PG_CONTAINER and
# PORTAL_CONTAINER. Exits 0 when both match, 1 on a difference, 2 if it couldn't check.
set -euo pipefail

PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$PWD" | tr '[:upper:]' '[:lower:]')}"
PG_USER="${PG_USER:-scheduler}"
PG_DB="${PG_DB:-scheduler}"
PORTAL_DB_PATH="${PORTAL_DB_PATH:-/data/portal.db}"

for f in backend/scheduler.sql portal/portal.sql; do
    [ -f "$f" ] || { echo "schema_check: no $f here; run it from the repo root" >&2; exit 2; }
done

container_for() {
    podman ps --filter "label=io.podman.compose.project=$PROJECT" \
        --filter "label=io.podman.compose.service=$1" --format '{{.Names}}' | head -1
}
PG_CONTAINER="${PG_CONTAINER:-$(container_for postgres)}"
PORTAL_CONTAINER="${PORTAL_CONTAINER:-$(container_for portal)}"

STATUS=0
TMP="$(mktemp -d)"
FRESH="schemacheck-$$"
cleanup() { podman rm -f "$FRESH" >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT

# Every column (type, nullability, default, generated expression), index and constraint, by
# name and definition, one per line and sorted, so column order (upgrades add columns at the
# end) doesn't count as a difference.
PG_CATALOG="
SELECT 'column ' || table_name || '.' || column_name || ' ' || data_type
       || CASE WHEN character_maximum_length IS NULL THEN ''
               ELSE '(' || character_maximum_length || ')' END
       || CASE WHEN is_nullable = 'NO' THEN ' not null' ELSE '' END
       || COALESCE(' default ' || column_default, '')
       || COALESCE(' generated ' || generation_expression, '')
  FROM information_schema.columns WHERE table_schema = 'public'
UNION ALL
SELECT 'index ' || tablename || ' ' || indexname || ': '
       || regexp_replace(indexdef, '^CREATE (UNIQUE )?INDEX \S+ ON \S+ ', '\1')
  FROM pg_indexes WHERE schemaname = 'public'
UNION ALL
SELECT 'constraint ' || conrelid::regclass || ' ' || pg_get_constraintdef(oid)
  FROM pg_constraint WHERE connamespace = 'public'::regnamespace
ORDER BY 1;"

# No -i: over `ssh ... bash -s` this script arrives on stdin, and -i would swallow the rest.
pg_catalog() {
    podman exec "$1" psql -U "$PG_USER" -d "$2" -AtX -v ON_ERROR_STOP=1 -c "$PG_CATALOG"
}

report() {      # name, live file, fresh file
    if diff -u --label "$1 (running)" --label "$1 (fresh install)" "$2" "$3" > "$TMP/$1.diff"
    then
        echo "✓ $1: the running database matches a fresh install ($(wc -l < "$2") items)"
    else
        echo "✗ $1: the running database differs from a fresh install"
        echo "  (- is only in the running database, + only in a fresh one)"
        sed 's/^/    /' "$TMP/$1.diff"
        STATUS=1
    fi
}

# ── backend: Postgres ─────────────────────────────────────────────────────────────────────
if [ -z "$PG_CONTAINER" ]; then
    echo "schema_check: no running postgres container for project '$PROJECT'" \
         "(set PG_CONTAINER)" >&2
    exit 2
fi
IMAGE="$(podman inspect --format '{{.ImageName}}' "$PG_CONTAINER")"
echo "• backend: $PG_CONTAINER ($IMAGE) against backend/scheduler.sql"
pg_catalog "$PG_CONTAINER" "$PG_DB" > "$TMP/backend.live"
podman run -d --rm --name "$FRESH" -e POSTGRES_USER="$PG_USER" -e POSTGRES_DB=fresh \
    -e POSTGRES_PASSWORD=schemacheck "$IMAGE" >/dev/null
# Ready means over TCP: the image's first-start initialisation runs a temporary server on the
# socket only, then restarts it, and a load sent to that one is cut off.
for _ in $(seq 90); do
    podman exec "$FRESH" pg_isready -h 127.0.0.1 -U "$PG_USER" -d fresh -q 2>/dev/null && break
    sleep 1
done
podman exec -i -e PGOPTIONS=--client-min-messages=error "$FRESH" \
    psql -h 127.0.0.1 -U "$PG_USER" -d fresh -qX -v ON_ERROR_STOP=1 \
    < backend/scheduler.sql > /dev/null
pg_catalog "$FRESH" fresh > "$TMP/backend.fresh"
report backend "$TMP/backend.live" "$TMP/backend.fresh"

# ── portal: SQLite ────────────────────────────────────────────────────────────────────────
# Run inside the portal container (it has the database and Python; no sqlite3 CLI needed).
# The live file is opened read-only; the fresh one is built in memory from portal.sql, sent on
# stdin. The two catalogs come back on stdout, split by a marker line.
PORTAL_CATALOG='
import sqlite3, sys
def catalog(conn):
    out = []
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = '"'"'table'"'"' "
        "AND name NOT LIKE '"'"'sqlite_%'"'"' ORDER BY name")]
    for t in tables:
        for _, name, typ, notnull, default, pk in conn.execute(f"PRAGMA table_info({t})"):
            out.append(f"column {t}.{name} {typ}" + (" not null" if notnull else "")
                       + (f" default {default}" if default is not None else "")
                       + (f" pk{pk}" if pk else ""))
        for _, name, unique, origin, partial in conn.execute(f"PRAGMA index_list({t})"):
            cols = ",".join(str(r[2]) for r in conn.execute(f"PRAGMA index_info({name})"))
            label = name if origin == "c" else origin
            out.append(f"index {t} {label}: ({cols})" + (" unique" if unique else "")
                       + (" partial" if partial else ""))
        for fk in conn.execute(f"PRAGMA foreign_key_list({t})"):
            out.append(f"foreign key {t}.{fk[3]} -> {fk[2]}.{fk[4]} on delete {fk[6]}")
    return sorted(out)
live = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
fresh = sqlite3.connect(":memory:")
fresh.executescript(sys.stdin.read())
print("\n".join(catalog(live)))
print("=== fresh ===")
print("\n".join(catalog(fresh)))
'
if [ -z "$PORTAL_CONTAINER" ]; then
    echo "schema_check: no running portal container for project '$PROJECT'" \
         "(set PORTAL_CONTAINER)" >&2
    exit 2
fi
echo "• portal: $PORTAL_CONTAINER:$PORTAL_DB_PATH against portal/portal.sql"
podman exec -i "$PORTAL_CONTAINER" python3 -c "$PORTAL_CATALOG" "$PORTAL_DB_PATH" \
    < portal/portal.sql > "$TMP/portal.both"
sed '/^=== fresh ===$/,$d' "$TMP/portal.both" > "$TMP/portal.live"
sed '1,/^=== fresh ===$/d' "$TMP/portal.both" > "$TMP/portal.fresh"
report portal "$TMP/portal.live" "$TMP/portal.fresh"

exit "$STATUS"
