# The 3.14 line, not an exact patch: Docker Hub stops rebuilding a patch tag once the next
# patch is out, so a tag like 3.14.3 freezes its Alpine packages (and their CVEs) for good.
# apk upgrade takes Alpine fixes newer than the image itself. Keep all three Python images on
# the same line (root CLAUDE.md → Images).
FROM python:3.14-alpine
RUN apk upgrade --no-cache

# No build tools: every requirement ships a musllinux wheel (psycopg2-binary bundles libpq),
# and the healthcheck uses Python, not curl.
RUN mkdir -p /app/logs \
 && addgroup -g 1001 -S appuser \
 && adduser  -u 1001 -S appuser -G appuser

WORKDIR /app

# Installed as root, so the app can read its packages but not change them. pip goes once
# they're in: nothing runs it later, and it's the image's only Python package with CVEs.
COPY requirements.txt .
RUN pip3 install --no-cache-dir --disable-pip-version-check --root-user-action=ignore \
        -r ./requirements.txt \
 && python3 -m pip uninstall --yes --no-cache-dir --root-user-action=ignore pip

COPY main.py .
COPY scheduler.sql .
COPY migrations/ ./migrations/
COPY db.py .
COPY portal_sync.py .
COPY passkey_providers.py .
COPY checkin_codes.py .
COPY api_auth.py .
COPY names.py .
COPY email_render.py .
COPY portal_content.py .
COPY routers/ ./routers/

USER appuser
EXPOSE 5000

CMD ["python3", "main.py"]
