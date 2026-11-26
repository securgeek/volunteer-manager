#!/usr/bin/env bash
# Write the software bill of materials, with known vulnerabilities, for everything that ships.
#
#   podman-compose build      # build the images first: the SBOM describes what is in them
#   tools/sbom.sh             # writes docs/security/sbom/*.cdx.json and meta.json
#   mkdocs build --strict     # docs/gen_security.py renders them on the Security pages
#
# One CycloneDX file per artifact, made by trivy (https://trivy.dev) with its vulnerability
# scanner on, so each file lists the components and any known CVEs as of today's database:
#
#   backend, portal, mailrelay, frontend,  the images podman-compose built (postgres is the
#   postgres                               official image plus apk upgrade, without gosu)
#   frontend-npm                           the JavaScript libraries bundled into the site
#                                          (package-lock.json, production dependencies only)
#   vol_mail                               the mailer's requirements (pip removed), installed into a
#                                          throwaway venv (it ships in no image)
#
# The images are found by podman-compose's naming, <project>_<service>, with the project taken
# from COMPOSE_PROJECT_NAME or this directory's name in lowercase. Override any one with
# IMAGE_BACKEND, IMAGE_PORTAL, IMAGE_MAILRELAY, IMAGE_FRONTEND or IMAGE_POSTGRES.
#
# Needs trivy and python3. Downloads trivy's vulnerability database if it's out of date, and
# PyPI packages for vol_mail. Writes only docs/security/sbom/. Commit the result: the files
# are what the docs site shows, and a new run replaces them.
set -euo pipefail
cd "$(dirname "$0")/.."

TRIVY="${TRIVY:-trivy}"
PYTHON="${PYTHON:-python3}"
OUT="${SBOM_OUT:-docs/security/sbom}"   # SBOM_OUT: write somewhere else, for a trial run
PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$PWD" | tr '[:upper:]' '[:lower:]')}"

command -v "$TRIVY" >/dev/null || { echo "sbom: trivy not found (set TRIVY=...)" >&2; exit 2; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/out"

# Record what each artifact was made from, for meta.json: name|kind|source|detail
SOURCES="$TMP/sources"
: > "$SOURCES"

scan_image() {
    local name="$1" image="$2" id created
    if ! id="$(podman image inspect --format '{{.Id}}' "$image" 2>/dev/null)"; then
        echo "sbom: no image $image. Build first (podman-compose build), or set IMAGE_${name^^}." >&2
        exit 1
    fi
    created="$(podman image inspect --format '{{.Created}}' "$image")"
    created="${created%%.*} UTC"          # drop the fractional seconds and offset
    echo "• $name: image $image"
    "$TRIVY" image --quiet --image-src podman --scanners vuln --format cyclonedx \
        --output "$TMP/out/$name.cdx.json" "$image"
    echo "$name|image|$image|${id:0:12}, built $created" >> "$SOURCES"
}

"$TRIVY" image --download-db-only --quiet

for svc in backend portal mailrelay frontend postgres; do
    var="IMAGE_${svc^^}"
    scan_image "$svc" "${!var:-localhost/${PROJECT}_${svc}:latest}"
done

echo "• frontend-npm: frontend/package-lock.json"
"$TRIVY" fs --quiet --scanners vuln --format cyclonedx \
    --output "$TMP/out/frontend-npm.cdx.json" frontend/package-lock.json
echo "frontend-npm|lockfile|frontend/package-lock.json|production dependencies" >> "$SOURCES"

echo "• vol_mail: tools/vol_mail/requirements.txt"
"$PYTHON" -m venv "$TMP/vol_mail"
"$TMP/vol_mail/bin/python" -m pip install --quiet --disable-pip-version-check \
    -r tools/vol_mail/requirements.txt
# Then pip goes, as it does from the app images: the SBOM is vol_mail's own libraries. pip
# (and the libraries it bundles in pip/_vendor, which trivy also lists) is installer tooling
# that the tool never runs.
"$TMP/vol_mail/bin/python" -m pip uninstall --quiet --yes --disable-pip-version-check pip
"$TRIVY" rootfs --quiet --scanners vuln --format cyclonedx \
    --output "$TMP/out/vol_mail.cdx.json" "$TMP/vol_mail"
echo "vol_mail|requirements|tools/vol_mail/requirements.txt|installed with $("$PYTHON" --version)" \
    >> "$SOURCES"

"$TRIVY" version --format json > "$TMP/trivy.json"
"$PYTHON" - "$TMP" <<'EOF'
import json, pathlib, sys
from datetime import datetime, timezone

tmp = pathlib.Path(sys.argv[1])
trivy = json.loads((tmp / "trivy.json").read_text(encoding="utf-8"))
artifacts = []
for line in (tmp / "sources").read_text(encoding="utf-8").splitlines():
    name, kind, source, detail = line.split("|", 3)
    artifacts.append({"name": name, "kind": kind, "source": source, "detail": detail,
                      "file": f"{name}.cdx.json"})
meta = {
    "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "trivy_version": trivy.get("Version"),
    "vuln_db_updated_at": (trivy.get("VulnerabilityDB") or {}).get("UpdatedAt"),
    "artifacts": artifacts,
}
(tmp / "out" / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
EOF

# Replace the old set only once every scan has succeeded.
rm -rf "$OUT"
mkdir -p "$OUT"
cp "$TMP"/out/* "$OUT"/
echo "Wrote $(ls "$OUT" | wc -l) files to $OUT. Commit them, then rebuild the docs."
