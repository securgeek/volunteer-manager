#!/usr/bin/env bash
# Swap the site for the maintenance page and back (compose.maintenance.yml).
#
#   ./maintenance.sh on      # real stack down, maintenance page up
#   podman-compose build     # ...whatever the maintenance is for
#   ./maintenance.sh off     # maintenance page down, real stack up
#   ./maintenance.sh status  # which one is running
#
# The two publish the same loopback ports, so each switch stops one before starting the
# other. COMPOSE overrides the command (e.g. COMPOSE="docker compose").
set -euo pipefail
cd "$(dirname "$0")"

COMPOSE="${COMPOSE:-podman-compose}"
MAINT=(-f compose.maintenance.yml)

case "${1:-}" in
    on)
        $COMPOSE down
        $COMPOSE "${MAINT[@]}" up -d
        echo "Maintenance page is up. Run '$0 off' to bring the site back."
        ;;
    off)
        $COMPOSE "${MAINT[@]}" down
        $COMPOSE up -d
        echo "Site is back up."
        ;;
    status)
        echo "── main stack"
        $COMPOSE ps
        echo "── maintenance"
        $COMPOSE "${MAINT[@]}" ps
        ;;
    *)
        echo "usage: $0 on|off|status" >&2
        exit 2
        ;;
esac
