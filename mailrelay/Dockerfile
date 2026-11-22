# The 3.14 line, not an exact patch: Docker Hub stops rebuilding a patch tag once the next
# patch is out, so a tag like 3.14.3 freezes its Alpine packages (and their CVEs) for good.
# apk upgrade takes Alpine fixes newer than the image itself. Keep all three Python images on
# the same line (root CLAUDE.md → Images).
FROM python:3.14-alpine
RUN apk upgrade --no-cache

# Standard library only — nothing to pip install. Only relay.py goes into the image; the
# tests stay out. uid 10001 must not change: the existing mailrelay_state volume, and the
# refresh token in it, belong to that uid.
# Not even pip: the image's copy goes, so the container holding the Graph credential has no
# Python package beyond the standard library.
RUN python -m pip uninstall --yes --no-cache-dir --root-user-action=ignore pip \
    && addgroup -S -g 10001 relay \
    && adduser -S -D -H -u 10001 -G relay relay \
    && mkdir /state && chown relay /state && chmod 700 /state
WORKDIR /app
COPY relay.py .

# The refresh token lives here, on the mailrelay_state volume. With read_only: true in
# compose it's the only writable path. A fresh named volume copies this directory's
# owner and mode from the image.
VOLUME ["/state"]

USER relay
EXPOSE 5002
CMD ["python", "-u", "relay.py"]
