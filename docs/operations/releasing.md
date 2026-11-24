# Releasing

Each release is a git tag, plus a copy of these docs on GitHub Pages as they stood at that
tag. [mike](https://github.com/jimporter/mike) publishes the docs: one folder per version on
the `gh-pages` branch, a `latest` alias that follows the newest, and a version menu in the
header.

| URL | Shows |
|---|---|
| `https://securgeek.github.io/volunteer-manager/` | redirects to `latest` |
| `https://securgeek.github.io/volunteer-manager/latest/` | the newest release |
| `https://securgeek.github.io/volunteer-manager/1.0/` | the docs as of the 1.0 release, unchanged |

**Docs versions are `MAJOR.MINOR`.** A patch release (1.0.1) republishes `1.0` in place, and a
new minor or major version (1.1, 2.0) gets its own folder. Tag the release itself however you
like, e.g. `v1.0.0`.

## Schema changes

Each release that changes a database schema has **one** file per app for it:
`backend/migrations/<release>.sql` and `portal/migrations/<release>.sql`. 1.0 has none, since
it's the baseline: the full schema files, `backend/scheduler.sql` and `portal/portal.sql`.

- **When the first schema change of a release goes in,** add its file and an entry to
  `SCHEMA_RELEASES` in that app's `db.py`, marked in progress, for example
  `("1.1", 20, "1.1.sql", True)`. Every later schema change in that release goes into the same
  file. Make the same change to the full schema file too, since that's what a fresh install
  gets.
- **While it's in progress,** the file re-runs every time the app starts, so a development
  database picks up each addition. Write it to be safe to run twice: `CREATE TABLE IF NOT
  EXISTS`, `ADD COLUMN IF NOT EXISTS` (Postgres), `ON CONFLICT DO NOTHING`. The portal skips
  an `ADD COLUMN` that's already there.
- **When you tag the release,** set the entry's last field to `False`. Production then
  applies the file exactly once.

To check a database that got where it is by upgrades matches a fresh install, run
`tools/schema_check.sh`. It compares tables, columns, defaults, indexes and constraints, and
reads the live databases only. `tools/` isn't on the server, so run it over ssh:

```bash
ssh "$DEPLOY_HOST" 'cd Scheduler && bash -s' < tools/schema_check.sh
```

## Steps

Run these from a clean checkout of the commit you're releasing, with the venv that has
`requirements-dev.txt` installed **activated** (`. .venv/bin/activate`). mike starts the
docs build as a separate command found on your PATH (`mkdocs`, or `properdocs` when that's
installed, as the gen-files and literate-nav plugins now pull in). Without the venv first on
PATH it finds a system copy that lacks mike's plugin, and fails with *The "mike" plugin is not
installed*.

1. **Check it passes.**

    ```bash
    ./run_tests.sh            # with TEST_PG_* set: see Getting started
    ./run_lint.sh             # pylint, bandit, and the strict docs build
    ```

2. **Finish the changelog.** In `CHANGELOG.md` at the repo root, rename **Unreleased** to the
   version and date (`## [1.1.0] — 2026-12-05`), start a new empty **Unreleased** above it,
   and update the compare links at the bottom. If upgrading needs anything beyond the usual
   deploy (a setting, a manual step, a rebuild of something unusual), the section starts with
   **Upgrade notes**. The GitHub release notes can be that section.

3. **Close the release's schema changes,** if it has any: set its `SCHEMA_RELEASES` entries to
   `False` (see [Schema changes](#schema-changes)), and check production matches with
   `tools/schema_check.sh`.

4. **Make the SBOM for this release.** The Security pages show it, and the published version
   keeps it.

    ```bash
    podman-compose build
    tools/sbom.sh
    git add docs/security/sbom/ && git commit -m "SBOM for 1.0.0"
    ```

5. **Tag it and push.**

    ```bash
    git tag -a v1.0.0 -m "1.0.0"
    git push origin master v1.0.0    # or main, whichever your default branch is
    ```

6. **Publish the docs from the tag.** Build from the tag itself, so the commit shown on the
   Bandit page is the release's:

    ```bash
    git checkout v1.0.0
    mike deploy --push --update-aliases 1.0 latest
    git checkout master
    ```

    `mike deploy` builds the site, commits it to the `gh-pages` branch under `1.0/`, points
    `latest` at it, and pushes that branch. Your working branch isn't touched.

7. **Deploy the server** as usual: [Deployment → Shipping a change](deployment.md#shipping-a-change).

### The first release only

After step 6, make `latest` the site's front door, and turn Pages on:

```bash
mike set-default --push latest
```

Then on GitHub: **Settings → Pages → Build and deployment → Source: Deploy from a branch**,
branch `gh-pages`, folder `/ (root)`. The site appears within a minute or two.

## Checking before you push

Leave out `--push` and mike commits to your local `gh-pages` branch only. `mike serve` then
shows the versioned site at <http://127.0.0.1:8000>, version menu and all:

```bash
mike deploy --update-aliases 1.0 latest
mike serve
```

Push it with `git push origin gh-pages` once it looks right. Other commands:

```bash
mike list                       # published versions and their aliases
mike delete --push 1.0          # unpublish a version
```

## What each version freezes

A published version is a snapshot. Its pages won't change unless you deploy that version
again:

- **The Bandit results** are as of the build, so as of the release when built from its tag.
- **The SBOM and vulnerabilities** are as of the `tools/sbom.sh` run committed in that
  release. New vulnerabilities in the same packages appear only in later scans. For a current
  view of a running release, rerun `tools/sbom.sh` against its images.
- **Everything else**, from the organizer guide to the code reference, is the release's own.

`mkdocs serve` and `mkdocs build` still work for everyday writing. They build one unversioned
site in `site/`, and the version menu stays empty until mike publishes.
