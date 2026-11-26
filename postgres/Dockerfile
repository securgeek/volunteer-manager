# The official image, kept as it is (its build of PostgreSQL, its entrypoint, POSTGRES_*
# handling and initdb), with two changes (docs/security/sbom.md):
#
# - apk upgrade takes Alpine fixes newer than the image itself, like the app images do.
# - gosu goes. It's a static Go binary built with an old Go, and it carried almost every
#   finding in the image's scan. The entrypoint only calls it to drop root, so starting as
#   postgres skips it. That means the data volume must be owned by uid 70 (it is when
#   the image's own initdb made it; root CLAUDE.md -> Images).
#
# The 18 line, not an exact patch (root CLAUDE.md -> Images). A major upgrade is a new volume
# and a dump and restore (docs/operations/deployment.md -> Upgrading PostgreSQL).
FROM postgres:18-alpine
RUN apk upgrade --no-cache \
 && rm -f /usr/local/bin/gosu

USER postgres
