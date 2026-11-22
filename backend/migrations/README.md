# Schema changes, one file per release

Each release after 1.0 that changes the schema gets one file here, named for the release
(`1.1.sql`, `1.2.sql`), and an entry in `SCHEMA_RELEASES` in `../db.py`. Every schema change
in that release goes into its one file, and also into the full schema file
(`scheduler.sql`), which is what a fresh install gets.

While the release is in progress, its file is re-run on every start, so it must be safe to run
twice. 1.0 has no file here: it's the baseline. See docs/operations/releasing.md.
