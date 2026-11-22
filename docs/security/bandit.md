# Bandit results

<!-- security: bandit -->

## How this is made

[Bandit](https://bandit.readthedocs.io) runs over the same files, with the same
`bandit.yaml`, as `./run_lint.sh`: each app's code, then its tests with B101 (`assert`)
skipped, since asserts are how pytest works. The lint fails on anything at medium severity
and medium confidence or above. This page shows **every** level, so the low findings the lint
lets through are visible too.

An accepted finding is marked `# nosec Bxxx` on its own line, with the reason beside it. There
are no global skips. The reasoning behind each tier is in
[Quality & security](../development/quality-and-security.md#bandit).
