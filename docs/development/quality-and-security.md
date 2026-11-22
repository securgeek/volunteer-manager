# Code quality and security posture

How the BSides code is checked, what blocks a change, how the quality score ratchet works,
and where the security protections live in the design. It also lists what nothing checks yet.

Everything here runs locally with one command:

```bash
python3 -m pip install -r backend/requirements.txt -r requirements-dev.txt   # once
./run_lint.sh
```

`run_lint.sh` runs, in order:

1. the tests for the `dynamic-sql` pylint plugin (`lint/`)
2. pylint, once per app, code and tests separately
3. Bandit
4. `mkdocs build --strict`

Any failure in any step fails the run. Tests run separately (`./run_tests.sh`).

---

## The three tiers

pylint's findings fall into three tiers. Each tier is handled differently.

| Tier | What | What happens |
|---|---|---|
| **1. Security** | The checks in `fail-on` in `.pylintrc` | **Blocks.** Any single finding fails the run, in code or tests, whatever the score. |
| **2. Quality and readability** | Everything else that's enabled: naming, docstrings, complexity, design limits, refactoring hints | **Reported.** Counts against the score; doesn't block on its own. |
| **3. Ratchet** | A minimum score per app | **Blocks** if the score drops below the app's threshold. |

So tier 1 is about *what kind* of finding, and tier 3 is about *how many* tier-2 findings
there are. Bandit's findings block on their own, whatever the score (see [Bandit](#bandit)).

### Tier 1: security (blocking)

| Check | Catches | Why it matters here |
|---|---|---|
| All **E** and **F** messages | Errors: undefined names, bad calls, import failures, syntax | These are bugs, not style. |
| `eval-used`, `exec-used`, `bad-builtin` | `eval`, `exec`, `compile`, `__import__`, `input` | Running strings as code, or reading a terminal in a server. |
| `bare-except`, `broad-exception-caught` | `except:` and `except Exception:` that swallow the error | Swallowed errors hide real failures. The CSV import bug was one: a broad `except` hid a transaction abort. |
| `try-except-raise`, `lost-exception` | Handlers that do nothing, `return` in `finally` | The exception isn't handled the way the code reads. |
| `raise-missing-from` | `raise X` inside `except` without `from` | Keeps (or deliberately drops) the cause, so logs show the real failure. |
| `dangerous-default-value` | Mutable default arguments | Shared state between calls. |
| `subprocess-run-check`, `subprocess-popen-preexec-fn` | `subprocess.run` without `check`, `preexec_fn` | Ignored exit codes; `preexec_fn` isn't safe with threads. |
| `unspecified-encoding` | `open()` without `encoding=` | The default is the platform's. vol_mail runs on organizers' own machines, and on Windows that is cp1252, which crashed on names outside it. |
| `consider-using-with` | Files, zips and so on opened outside `with` | Leaked handles. |
| `global-statement` | `global x` | Module state lives in a small dataclass instance instead (`_state`, `_clock`, `_config`). |
| `undefined-loop-variable` | Using a loop variable after an empty loop | Wrong value, or a crash. |
| `deprecated-module` | `pickle`, `cPickle`, `marshal`, `shelve`, `telnetlib` | Unsafe deserialisation. |
| `dynamic-sql` | SQL text built at runtime ([below](#sql-is-always-a-literal)) | SQL injection, by construction. |

### Tier 2: quality and readability

pylint's defaults, plus most of its optional extensions:

- **Complexity:** mccabe complexity at most 10.
- **Design limits:** at most 6 arguments, 15 locals, 12 branches, 50 statements, 6 returns and 4
  nested blocks per function, and 1000 lines per module.
- **Docstrings:** Google style. When a docstring has an `Args:` section, it must match the
  real signature (`docparams`). A docstring with no such section is fine.
- **Modern idioms:** magic values, `X | None` over `Optional[X]`, sets for membership tests,
  and similar.
- **Line length:** 100.

Turned off everywhere:

- `too-few-public-methods`: Pydantic models.
- `consider-using-assignment-expr`: opinionated.
- `fixme`: TODOs are listed by `./run_lint.sh --todo` instead of counted.

Turned off for test files only (in `run_lint.sh`), because these are normal pytest idioms:

- `redefined-outer-name`, `unused-argument`: fixtures are injected by parameter name.
- `protected-access`: tests poke at module internals on purpose.
- `import-outside-toplevel`, `wrong-import-position`: imports after `sys.path` or environment setup.
- `missing-function-docstring`: the test name is the documentation.
- `magic-value-comparison`: asserting on literal expected values is the point.

---

## The score ratchet

### What the score is

pylint rates each run out of 10:

```
score = 10 − 10 × (5·errors + warnings + refactors + conventions) / statements
```

Two things follow from the formula:

- **Every tier-2 message lowers the score.** An error costs five times as much as anything else.
- **The score is per statement.** New code with no findings *raises* it, so cleaning up and
  writing clean new code both move it up.

### How the ratchet works

Each app has two thresholds, one for its code and one for its tests, in the `APPS` table at
the top of `run_lint.sh`. A run fails if a score falls below its threshold.

| App | Code: score | Code: ratchet | Tests: score | Tests: ratchet |
|---|---|---|---|---|
| portal | 8.98 | **8.9** | 9.81 | **9.8** |
| backend | 8.82 | **8.8** | 9.74 | **9.7** |
| mailrelay | 8.85 | **8.8** | 9.75 | **9.7** |
| vol_mail | 8.62 | **8.6** | 9.40 | **9.3** |

The root `conftest.py` scores 10.00 and has no ratchet.

History:

| Date | Event | Code ratchets (portal / backend / mailrelay / vol_mail) |
|---|---|---|
| 2026-11-18 | First baseline, when lint was introduced | 8.8 / 8.6 / 8.8 / 8.1 |
| 2026-11-18 | Raised after the security-tier cleanup (static SQL, state holders, `raise … from`) | 8.9 / 8.8 / 8.8 / 8.6 |

Scores are per app because the apps are linted separately: backend/ and portal/ both have
modules called `db` and `main`, so one pylint run would resolve imports against the wrong app.
That's the same reason `run_tests.sh` splits the test suites.

### Why a ratchet rather than "fix everything"

The first baseline had about 640 tier-2 messages (730 in all, 87 of them tier 1), most of
them long lines, magic values and missing docstrings. Blocking on all of them at once would have meant one enormous change with
little to show for it, or turning the checks off. The ratchet makes a different promise:
**the code doesn't get worse**, and it gets better as it's touched.

### Rules

- **Never lower a ratchet to get a change through.** If your change lowers the score, fix the
  messages it introduced. If one is wrong for that case, disable it on that line with a
  reason (see [Accepted exceptions](#accepted-exceptions)).
- **Raise a ratchet when the score has moved up** after a cleanup, in the same commit.
- **Round down to one decimal.** pylint shows two decimals and rounds them, so a displayed
  `8.20` can really be `8.198` and fail a ratchet of 8.2. When the display ends in 0, go one
  tenth lower. (vol_mail's code ratchet once failed at a displayed 8.20 for exactly this reason.)

### Useful commands

```bash
./run_lint.sh --no-bandit --no-docs                 # just pylint
./run_lint.sh --no-bandit --no-docs -- --fail-on=   # the scores alone, ignoring tier 1
./run_lint.sh --todo                                # also list TODO/FIXME
```

Run pylint from `BSides/` or pass `--rcfile=BSides/.pylintrc`. Inside an app directory,
pylint doesn't find the config: backend/ isn't a package, so its config search stops there.

---

## SQL is always a literal

**Every SQL statement is one complete string literal, and values go in only as bound `?`
parameters.** Nothing builds SQL text at runtime: no f-strings, `+`, `%` or `.format()`,
not even from constants.

A placeholder can stand in for a value. It can't stand in for a table name, a column name,
or a piece of SQL. So code that needs a choice uses one of these:

| Need | Technique | Example |
|---|---|---|
| A constant value, e.g. an expiry | Bind it | `NOW() … + ? * INTERVAL '1 hour'` |
| An optional clause | A bound flag in a `CASE` or `OR` | `cancelled_at = CASE WHEN ? THEN NULL ELSE cancelled_at END`, `WHERE (? OR v.is_assignable)` |
| One of several tables or columns | A dict or tuple of whole literal statements | `_ALL_KEYS_SQL`, `_IMAGE_SQL`, `SNAPSHOT_TABLES` |
| A variable-length list (Postgres) | An array parameter | `day_key = ANY(?::text[])` with a Python **list**; psycopg2 sends a tuple as a row |
| Updating only some columns | A "was it sent" flag per column | the portal's `_UPSERT_VOLUNTEER_SQL` |
| A rule shared by many queries | Put it in the schema | `volunteers.is_live` / `is_counted` / `is_assignable` (migration v17) |

**Enforcement:**

- **`dynamic-sql`** (`lint/pylint_sql.py`) is in tier 1. It recognises SQL by structure,
  case-sensitively (`SELECT … FROM`, `INSERT INTO`, `UPDATE x SET`, `WHERE`, `VALUES (`, …),
  and resolves named constants, so `_COLS + "FROM users"` is caught too. Its tests are in
  `lint/test_pylint_sql.py` and run first in every `run_lint.sh`.
- **Bandit's B608** covers similar ground as a second net. Before the rewrite it missed 4 of
  the 33 sites.

---

## Bandit

Bandit scans for common security problems that pylint doesn't look for: shell injection,
unsafe deserialisation, weak cryptography, binding to all interfaces, hardcoded passwords and
the like.

- **Application code:** every test, failing at **medium severity and medium confidence or
  above**.
- **Test files:** the same, except B101 (`assert`), because asserts are how pytest works.
- **Never scanned:** `Scheduler/` (legacy reference code), `frontend/`, `node_modules/`, `.venv/`.
- **No global skips.** Every accepted finding is marked on its own line (below).

Below the threshold, application code currently has only two low-severity B101 findings:
`assert`s that guard against programmer error, not input.

---

## Accepted exceptions

Every suppression sits on the line it applies to, with its reason in a comment. This is
the full list. Reviewers should question any new entry.

| Where | Suppresses | Reason |
|---|---|---|
| `backend/portal_sync.py` (`_has_table`, ×2) | `broad-exception-caught` | Any failure only means "not Postgres", then "no such table". Used by the SQLite tests. |
| `backend/portal_sync.py` (`_sync_once`) | `broad-exception-caught` | The sync loop must outlive any failure. The error is recorded in `portal_sync_state` and shown in the Admin UI. |
| `backend/routers/invites.py` (invite mail) | `broad-exception-caught` | A failed mail mustn't undo the invite. The error goes back to the organizer as `email_error`. |
| `backend/routers/password_reset.py` | `broad-exception-caught` | The reply never varies, so no failure may reach the caller. It's logged and audited. |
| `tools/vol_mail/vol_mail.py` (decrypt) | `broad-exception-caught` | One undecryptable address skips that volunteer, and is reported, without stopping the run. |
| `backend/main.py`, `portal/main.py` | Bandit B104 | Binding to all interfaces *inside the container*; `compose.yml` decides what reaches it. |
| `mailrelay/relay.py` | Bandit B104 | The same, and the relay has no published ports: only the portal, on `mail_internal`, can reach it. |
| `portal/mailer.py` | Bandit B310 | `MAIL_RELAY_URL` is refused at startup unless it's `http://` or `https://` (`config.py`). |
| `docs/gen_export_api.py`, `lint/test_pylint_sql.py` | Import position | Tooling that imports app code only after setting `sys.path`. |

To list them from the code, so this table can be checked:

```bash
grep -rn --include=*.py -E "# nosec|pylint: disable" backend portal mailrelay tools lint docs
```

---

## Security in the design

The lint covers how code is written. Most of the system's protection comes from how it's
built. Each of these points is documented in the CLAUDE.md beside the code it describes.

**Separation and blast radius**

- The **volunteer portal** is a separate application on the internet-facing side, with no
  shared database, session secret or code. If that host is fully compromised, the attacker
  gets volunteer names, email **ciphertext** and a copy of the schedule. They don't get
  organizer credentials, notes, ratings, the scheduler's cookie secret, or any readable
  email address.
- **Traffic flows only scheduler → portal.** The portal has no callback URL, so the scheduler
  needs no inbound opening.
- The **mail relay** is the only process holding the Microsoft Graph credential. It's
  standard-library only, sends from one hard-coded address, takes exactly `to` / `subject` /
  `body`, is rate-limited, has a read-only filesystem except `/state`, and has no published
  ports.

**Secrets and personal data**

- **Email addresses** are stored as RSA-OAEP ciphertext. The private key never leaves an
  organizer's machine: `.rsync-filter` keeps `*.pem` off the server. A keyed HMAC acts as
  the lookup index, and its key lives only in the environment, never in a database.
- **Only hashes are stored** for login links, sessions, invite and reset links, and export
  API keys (SHA-256). A copy of a database isn't enough to mint a session or call the API.
- **Audit logs carry ids, never addresses.** There are tests for this.

**Authentication**

- **Export API:** scoped bearer keys with an expiry (at most 365 days) and an optional IP
  allow-list.
  - Every failure gets the same bare 401, and 10 failures per IP in 15 minutes give a 429.
  - Calls are audited by key label, never by the key itself.
  - It sends no CORS headers.
- **Sync API:** HMAC-signed requests, a ±300 s timestamp window, a nonce table against
  replay, and an `op_id` on every POST so a retry can't apply twice.
- **Organizer sign-in:** a password or a passkey.
  - Adding a passkey needs the current password.
  - WebAuthn challenges are single-use.
  - Password-reset replies never vary and are rate-limited.
  - Completing a reset signs the account out everywhere.
- **Links aren't spent by a GET**, so mail scanners and link previews can't use up a token.

**Organizer-edited content**

- **Templates** (the landing page, the invite email) render in Jinja's sandbox, and a parse-
  tree check allows only variables, `{% if %}`, comparisons and a few text filters, so a
  template can't reach other objects or loop forever. Both apps check this: the scheduler
  when it's saved, the portal again on every push.
- **The landing page's Markdown** is rendered with raw HTML and images off and only web,
  email and same-site links, because it's inserted into a public page as HTML.

**Runtime**

- **Images:** Alpine base images, with no build tools or curl in them.
- **Users:** the Python services run as non-root users (uid 1001; the mail relay as 10001).

---

## Known gaps

What nothing checks today, roughly in order of value:

- **Dependency vulnerabilities, between scans.** `tools/sbom.sh` scans every image and
  library with trivy, and the [Security](../security/index.md) pages show the result. It runs
  by hand, though, so the page is only as current as its "as of" date, and nothing fails a
  build on a new CVE. The Python libraries are pinned to their current major version, not to
  exact versions, so two builds a month apart can differ in patch and minor releases.
- **Secrets in the repository.** No secret scanner. `.gitignore` lists `.env` and its
  backups, `*.pem`, `*.key` and `api_key*`, and `.rsync-filter` keeps keys off the server
  (`.env` syncs on purpose). Nothing checks file contents, though. Run a scanner such as
  `gitleaks` before pushing. See [Secrets and keys](../operations/secrets.md).
- **Changing a password doesn't end other sessions.** Changing your own password, or an
  organizer setting one, increments `users.password_version`, but nothing reads that
  column. Other signed-in sessions, and the backend's session cache, carry on. Only a
  password reset by email deletes the account's sessions.
- **The frontend.** The Vue/JavaScript code has no linter or security scanner.
- **Types.** No type checker (mypy or pyright). Annotations aren't verified.
- **Tooling code.** `docs/` and `lint/` aren't in `run_lint.sh`'s app list. The plugin is
  tested, but neither directory is linted.
- **Postgres-only tests** skip unless `TEST_PG_HOST` is set, so `./run_tests.sh` alone
  doesn't run them. That's 128 of the backend's 163 tests, including every export, invite,
  passkey, CSV import and migration test. backend/CLAUDE.md → Tests explains how to start a throwaway Postgres
  for them.
- **Broad catches that re-raise.** pylint's `broad-exception-caught` ignores a handler that
  raises, so two are unflagged:
  - `backend/routers/invites.py` (invite acceptance) reports *any* database error as
    "username is taken". It should catch `psycopg2.errors.UniqueViolation` only.
  - `portal/routers/sync.py` (branding push) treats any exception as malformed base64.
- **MkDocs.** The project behind MkDocs 1.x is no longer maintained upstream (the build
  prints a warning about it). It works today; replacing it is a decision for later.
- **Live testing.** Nothing here runs against the deployed stack. The shell scripts in
  `backend/` (`test_grid.sh`, `test_radios.sh`, `test_categories.sh`) do, and need credentials.
