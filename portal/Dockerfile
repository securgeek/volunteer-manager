# The 3.14 line, not an exact patch: Docker Hub stops rebuilding a patch tag once the next
# patch is out, so a tag like 3.14.3 freezes its Alpine packages (and their CVEs) for good.
# apk upgrade takes Alpine fixes newer than the image itself. Keep all three Python images on
# the same line (root CLAUDE.md → Images).
FROM python:3.14-alpine
RUN apk upgrade --no-cache

# Runs as appuser (1001), the same uid as the backend, since both mount ./logs with :U.
# /data is the SQLite volume; compose mounts it with :U so data written while the portal ran
# as root is re-owned on start.
RUN addgroup -g 1001 -S appuser \
 && adduser  -u 1001 -S appuser -G appuser \
 && mkdir -p /data /app/logs \
 && chown appuser:appuser /data /app/logs

WORKDIR /app

# Installed as root, so the app can read its code and packages but not change them. No build
# tools: every requirement ships a musllinux wheel, and the healthcheck uses Python, not curl.
# pip goes once they're in: nothing runs it later.
COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore \
        -r requirements.txt \
 && python -m pip uninstall --yes --no-cache-dir --root-user-action=ignore pip

COPY . .

# The SQLite file lives on a volume; /app/logs is bind-mounted for MAIL_BACKEND=file.
ENV PORTAL_DB=/data/portal.db
VOLUME ["/data"]

USER appuser
EXPOSE 5001

# One worker, on purpose — SQLite has a single writer.
CMD ["python", "main.py", "--host", "0.0.0.0", "--port", "5001"]
