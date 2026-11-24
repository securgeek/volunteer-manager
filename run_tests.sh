#!/usr/bin/env bash
# Run every suite that works without Postgres, SMTP, or a browser.
#
# Two pytest processes, not one, and not by preference: backend/ and portal/ are separate
# applications that both define modules named `db` and `main`. sys.modules holds one module per
# name, so a single process would bind the wrong one for one of the suites. Each suite's
# conftest.py fails loudly if that happens anyway.
#
#   ./run_tests.sh            # every suite
#   ./run_tests.sh -v         # extra args pass through to pytest
#
# Also runs the volunteer pages' input rules against the portal's shared cases with Node's own
# test runner (no packages), when node is installed.
#
# What these CANNOT cover, by design: anything needing Postgres, real SMTP delivery, the
# scheduler<->portal integration against a live portal, or any browser flow. For those, bring
# the stack up.

set -uo pipefail
cd "$(dirname "$0")"

PYTEST="${PYTEST:-python3 -m pytest}"
FAILED=0

run_suite() {
    local name="$1" path="$2"; shift 2
    echo
    echo "─── $name ────────────────────────────────────────────────────"
    # shellcheck disable=SC2086
    if $PYTEST "$path" "$@"; then
        echo "✓ $name"
    else
        echo "✗ $name"
        FAILED=1
    fi
}

run_suite "portal"    portal/    "$@"
run_suite "scheduler" backend/   "$@"
run_suite "vol_mail"  tools/vol_mail/ "$@"   # imports backend/email_render; own process like the others

echo
echo "─── frontend input rules ────────────────────────────────────────────────────"
if command -v node >/dev/null 2>&1; then
    if (cd frontend && node --test src/lib/portal_rules.test.js >/dev/null 2>&1); then
        echo "✓ frontend input rules (portal/test_data/validation_cases.json)"
    else
        (cd frontend && node --test src/lib/portal_rules.test.js 2>&1 | grep -E "^not ok|# (pass|fail)")
        echo "✗ frontend input rules disagree with the portal's"
        FAILED=1
    fi
else
    echo "! node isn't installed: the frontend input rules weren't checked"
fi

echo
if [ "$FAILED" -eq 0 ]; then
    echo "All suites passed."
    echo
    echo "Not covered here: Postgres-backed paths, real SMTP, the scheduler<->portal"
    echo "integration, and every browser flow. Those need the stack running."
else
    echo "At least one suite failed."
fi
exit "$FAILED"
