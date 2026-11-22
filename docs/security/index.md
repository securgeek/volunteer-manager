# Security

What the system is made of, what's known to be wrong with it, and what static analysis says
about the code. The numbers on this page come from the two pages below it, and each says when
it was made.

<!-- security: summary -->

## What's here

- **[SBOM and vulnerabilities](sbom.md)**: a CycloneDX software bill of materials for
  everything that ships (each container image, the JavaScript bundled into the site, and the
  `vol_mail` tool), with the known vulnerabilities trivy found in each component.
- **[Bandit results](bandit.md)**: bandit's findings for every app at every level, and each
  accepted exception with its reason. Regenerated on every docs build.
- **[Quality & security](../development/quality-and-security.md)**: what the lint enforces,
  the security design (separation, secrets, authentication), and the known gaps.
- **[Secrets and keys](../operations/secrets.md)**: every secret, where it lives, and how to
  rotate it.

## Keeping it current

The bandit results are rebuilt whenever the docs are. The SBOM describes built images, so it's
made by a script rather than at build time:

```bash
podman-compose build     # the images the SBOM describes
tools/sbom.sh            # one CycloneDX file per artifact, with vulnerabilities, into docs/security/sbom/
mkdocs build --strict    # or ./run_lint.sh; renders them here
```

Commit `docs/security/sbom/` afterwards. Run it before each release, and whenever you want a
fresh vulnerability check: trivy's database changes daily, so the same images can show new
vulnerabilities a week later.

The Python libraries are pinned to their current major version (`>=` the release tested, `<`
the next major), so a rebuild picks up fixes but not breaking changes. The JavaScript
libraries are pinned exactly by `frontend/package-lock.json`.
