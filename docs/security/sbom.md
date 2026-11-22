# SBOM and vulnerabilities

<!-- security: sbom -->

## How this is made

`tools/sbom.sh` runs [trivy](https://trivy.dev) over each artifact, with its vulnerability
scanner on, and writes one [CycloneDX](https://cyclonedx.org) file per artifact to
`docs/security/sbom/`, plus `meta.json` with the date and trivy's version. This page is
rendered from those files on every docs build; the files themselves are linked above, for
tools that read CycloneDX.

- **Images** are scanned as built, so the SBOM lists the Alpine packages and every installed
  Python package, at the versions actually in the image. Python itself isn't listed: the
  official image builds it from source rather than installing it as a package, so trivy
  can't see it. Its version is the image's tag (`python:3.14…`).
- **Frontend JavaScript** comes from `package-lock.json`, production dependencies only: the
  build tools (Vite and the like) don't ship.
- **vol_mail** isn't in any image. Its requirements are installed into a throwaway virtual
  environment and scanned there, with `pip` removed first, as it is from the app images. That
  leaves the tool's own libraries. The organizer's own environment still has `pip`, which
  is installer tooling the tool never runs. Scan that environment if you want it counted.
- **Severity** is the rating from the vulnerability's own source (the Alpine, GitHub or
  Python advisory), falling back to the highest rating any source gives.

**The PostgreSQL image is used as published** (`postgres:18-alpine`), so its findings wait on
the image's maintainers. Most are in `gosu`, a small Go program the image's entrypoint runs
once at start to drop root before PostgreSQL starts. It's built with an old Go, and none of
the network-facing Go code it's flagged for runs in this stack. The app images, by contrast,
are built here with `apk upgrade`, so a rebuild takes every Alpine fix.

A listed vulnerability isn't necessarily reachable. Many are in code paths this project never
calls, such as a library feature it doesn't use. Fix what has a fix by rebuilding the images
(the pinned ranges take patch and minor releases), and look at the rest before deciding.
