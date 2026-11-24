#!/usr/bin/env bash
# Retake the README screenshots on a throwaway local stack filled with fake data.
#
#   tools/gather-screenshots/gather-screenshots.sh            # build, seed, shoot, tear down
#   tools/gather-screenshots/gather-screenshots.sh --keep     # leave the demo running afterwards
#   tools/gather-screenshots/gather-screenshots.sh --down     # tear down a kept demo
#
# The demo is a copy of this checkout in a temporary directory, run as its own compose project
# (bsides-demo) on its own ports, with its own generated .env. It never reads this checkout's
# .env and never touches its volumes, logs/ or ports. Images land in docs/images/screenshots/.
#
# Needs: podman + podman-compose, rsync, openssl, node 22.12+ and npm, and Chromium or Chrome
# (CHROME=/path/to/chrome if it isn't found). See README.md beside this script.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

PROJECT="${DEMO_PROJECT:-bsides-demo}"
ADMIN_PORT="${DEMO_ADMIN_PORT:-18080}"
PUBLIC_PORT="${DEMO_PUBLIC_PORT:-18082}"
OUT="${DEMO_OUT:-$REPO/docs/images/screenshots}"
STATE="${XDG_CACHE_HOME:-$HOME/.cache}/gather-screenshots"   # remembers the work dir for --down

KEEP=0
case "${1:-}" in
  --keep) KEEP=1 ;;
  --down) MODE=down ;;
  "") ;;
  -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
  *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

compose() { (cd "$WORK" && podman-compose -p "$PROJECT" -f compose.yml -f compose.demo.yml "$@"); }

teardown() {
  echo "• tearing down $PROJECT"
  compose down -v >/dev/null 2>&1 || true
  # The containers re-own logs/ to their own uids (:U), so the host user needs the user
  # namespace to delete what they wrote.
  podman unshare rm -rf "$WORK" 2>/dev/null || rm -rf "$WORK"
  rm -f "$STATE/workdir"
}

if [[ "${MODE:-}" == down ]]; then
  WORK="$(cat "$STATE/workdir" 2>/dev/null || true)"
  [[ -n "$WORK" && -d "$WORK" ]] || { echo "no kept demo found"; exit 0; }
  teardown
  exit 0
fi

for cmd in podman podman-compose rsync openssl curl node npm; do
  command -v "$cmd" >/dev/null || { echo "missing: $cmd" >&2; exit 1; }
done
# puppeteer-core 25 needs Node 22.12 or later (package.json engines).
node -e 'const [a, b] = process.versions.node.split(".").map(Number);
         process.exit(a > 22 || (a === 22 && b >= 12) ? 0 : 1)' \
  || { echo "node $(node -v) is too old: puppeteer-core needs 22.12 or later" >&2; exit 1; }
if podman ps -a --format '{{.Names}}' | grep -q "^${PROJECT}_"; then
  echo "containers for $PROJECT already exist; run with --down first" >&2
  exit 1
fi

# ── 1. a clean copy of the source ─────────────────────────────────────────────
mkdir -p "$STATE"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/bsides-demo.XXXXXX")"
echo "$WORK" > "$STATE/workdir"
[[ $KEEP == 1 ]] || trap teardown EXIT
echo "• copying source to $WORK"
rsync -a --exclude .git --exclude .env --exclude 'node_modules' --exclude site \
      --exclude plans --exclude logs --exclude '__pycache__' --exclude '.venv' \
      "$REPO/" "$WORK/"
cp "$HERE/compose.demo.yml" "$WORK/compose.demo.yml"
mkdir -p "$WORK/logs"

# ── 2. its own .env, every secret freshly generated ──────────────────────────
cat > "$WORK/.env" <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 16)
EMAIL_HMAC_KEY=$(openssl rand -hex 32)
SYNC_KEY_ID=demo
SYNC_SECRET=$(openssl rand -hex 32)
PORTAL_SYNC_URL=http://portal:5001
PORTAL_SYNC_INTERVAL=5
PORTAL_PUBLIC_URL=http://localhost:$PUBLIC_PORT
ADMIN_PUBLIC_URL=http://localhost:$ADMIN_PORT
FRONTEND_PORT=$ADMIN_PORT
PORTAL_SITE_PORT=$PUBLIC_PORT
MAIL_BACKEND=file
SMTP_FROM=volunteers@example.org
EOF

# ── 3. build and start (no mailrelay: mail goes to files) ────────────────────
echo "• building and starting $PROJECT (the first build takes a few minutes)"
compose up -d --build postgres backend portal frontend

echo -n "• waiting for the stack"
for _ in $(seq 1 90); do
  if curl -fsS "http://localhost:$ADMIN_PORT/api/health" >/dev/null 2>&1 &&
     curl -fsS -o /dev/null -w '%{http_code}' "http://localhost:$PUBLIC_PORT/api/portal/branding" 2>/dev/null | grep -q 200; then
    echo " up"; break
  fi
  echo -n "."; sleep 2
done
curl -fsS "http://localhost:$ADMIN_PORT/api/health" >/dev/null || { echo " timed out" >&2; exit 1; }

# ── 4. seed and shoot ────────────────────────────────────────────────────────
# Install, or reinstall when package-lock.json has changed since the last install: npm writes
# node_modules/.package-lock.json on every install, so a newer lock means a dependency moved.
(cd "$HERE" && { [[ node_modules/.package-lock.json -nt package-lock.json ]] \
                 || npm ci --no-audit --no-fund; })
mkdir -p "$OUT"
node "$HERE/shoot.mjs" \
  --admin "http://localhost:$ADMIN_PORT" \
  --public "http://localhost:$PUBLIC_PORT" \
  --portal-container "${PROJECT}_portal_1" \
  --out "$OUT"

if [[ $KEEP == 1 ]]; then
  cat <<EOF

The demo is still running:
  organizer site  http://localhost:$ADMIN_PORT/admin   (user: organizer, password: demo-password-1)
  volunteer site  http://localhost:$PUBLIC_PORT/
Tear it down with: $0 --down
EOF
fi
