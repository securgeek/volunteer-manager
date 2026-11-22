#!/usr/bin/env bash
# Lint and docs checks: pylint, bandit, and a strict MkDocs build.
#
# pylint runs once per application, not once for the repo, for the same reason run_tests.sh
# splits the test suites: backend/ and portal/ both define modules named `db` and `main`, and
# one pylint process would resolve imports against whichever it saw first.
#
#   ./run_lint.sh                 # everything
#   ./run_lint.sh --no-docs       # skip the MkDocs build (also --no-pylint, --no-bandit)
#   ./run_lint.sh --todo          # also list TODO/FIXME comments (never fails)
#   ./run_lint.sh -- --disable=C  # anything after -- goes to every pylint run
#
# What fails the run (see .pylintrc and bandit.yaml for the reasoning):
#   - any security-tier pylint message (fail-on in .pylintrc), in code or tests
#   - a pylint score below the app's ratchet in APPS below
#   - any bandit finding at medium severity and medium confidence or above
#   - any warning from `mkdocs build --strict` (a broken docstring, reference or link)
#
# Needs requirements-dev.txt, plus backend/requirements.txt for the docs build.

set -uo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
DO_PYLINT=1 DO_BANDIT=1 DO_DOCS=1 DO_TODO=0
PYLINT_EXTRA=()
while [ $# -gt 0 ]; do
    case "$1" in
        --no-pylint) DO_PYLINT=0 ;;
        --no-bandit) DO_BANDIT=0 ;;
        --no-docs)   DO_DOCS=0 ;;
        --todo)      DO_TODO=1 ;;
        --) shift; PYLINT_EXTRA=("$@"); break ;;
        *) echo "unknown option: $1 (see the header of $0)" >&2; exit 2 ;;
    esac
    shift
done

# name | directory | import roots: code | import roots: tests | ratchet: code | ratchet: tests
# Import roots are what the app's code finds on sys.path, first match wins; they become
# pylint's --source-roots and PYTHONPATH. Backend tests also reach into portal/ (security.py),
# so portal comes after backend there: `db` and `main` still resolve to backend's.
# Ratchets are the score rounded down (raised 2026-11-18, after the security-tier cleanup).
# Raise them as the code is cleaned up; never lower them to get a change through.
APPS=(
    "portal|portal|portal|portal|8.9|9.8"
    "backend|backend|backend|backend,portal|8.8|9.7"
    "mailrelay|mailrelay|mailrelay|mailrelay|8.8|9.7"
    "vol_mail|tools/vol_mail|tools/vol_mail,backend|tools/vol_mail,backend|8.6|9.3"
)

# Extra disables for test files only: pytest idioms, not problems.
#   redefined-outer-name   fixtures are injected by parameter name
#   unused-argument        fixtures requested for their side effect
#   protected-access       tests poke at module internals on purpose
#   import-outside-toplevel / wrong-import-position   imports after sys.path or env setup
#   missing-function-docstring   the test name is the documentation
#   magic-value-comparison       asserting on literal expected values is the point
TEST_DISABLE="redefined-outer-name,unused-argument,protected-access,import-outside-toplevel,\
wrong-import-position,missing-function-docstring,magic-value-comparison"

FAILED=0

section() {
    echo
    echo "─── $1 ────────────────────────────────────────────────────"
}

# Fills CODE_FILES / TEST_FILES for one app directory.
split_files() {
    local dir="$1"
    mapfile -t CODE_FILES < <(find "$dir" -name '*.py' -not -path '*/__pycache__/*' \
        -not -name 'test_*.py' -not -name 'conftest.py' | sort)
    mapfile -t TEST_FILES < <(find "$dir" -name '*.py' -not -path '*/__pycache__/*' \
        \( -name 'test_*.py' -o -name 'conftest.py' \) | sort)
}

run_pylint() {
    local label="$1" roots="$2" under="$3"; shift 3
    # Everything up to "--files" is extra pylint options; the rest are files.
    local opts=()
    while [ $# -gt 0 ] && [ "$1" != "--files" ]; do opts+=("$1"); shift; done
    shift
    [ $# -eq 0 ] && return 0
    echo "• pylint $label (fail-under $under)"
    # --source-roots names the modules; PYTHONPATH lets astroid resolve imports from every root.
    if ! PYTHONPATH="${roots//,/:}" "$PYTHON" -m pylint --rcfile=.pylintrc \
            --source-roots="$roots" --fail-under="$under" \
            "${opts[@]}" "${PYLINT_EXTRA[@]}" "$@"; then
        echo "✗ pylint $label"
        FAILED=1
    fi
}

run_bandit() {
    local label="$1"; shift
    local opts=()
    while [ $# -gt 0 ] && [ "$1" != "--files" ]; do opts+=("$1"); shift; done
    shift
    [ $# -eq 0 ] && return 0
    echo "• bandit $label"
    if ! "$PYTHON" -m bandit -q -c bandit.yaml --severity-level medium --confidence-level medium \
            "${opts[@]}" "$@"; then
        echo "✗ bandit $label"
        FAILED=1
    fi
}

# The dynamic-sql plugin (lint/pylint_sql.py) is part of the security tier; if it's broken,
# every run below would pass for the wrong reason.
if [ "$DO_PYLINT" -eq 1 ]; then
    section "lint plugin"
    echo "• pytest lint/"
    if ! "$PYTHON" -m pytest -q lint/; then
        echo "✗ lint plugin tests"
        FAILED=1
    fi
fi

for entry in "${APPS[@]}"; do
    IFS='|' read -r name dir roots test_roots code_under test_under <<<"$entry"
    split_files "$dir"
    section "$name"
    if [ "$DO_PYLINT" -eq 1 ]; then
        run_pylint "$name code" "$roots" "$code_under" --files "${CODE_FILES[@]}"
        run_pylint "$name tests" "$test_roots" "$test_under" --disable="$TEST_DISABLE" \
            --files "${TEST_FILES[@]}"
    fi
    if [ "$DO_BANDIT" -eq 1 ]; then
        run_bandit "$name code" --files "${CODE_FILES[@]}"
        run_bandit "$name tests" --skip B101 --files "${TEST_FILES[@]}"
    fi
    if [ "$DO_TODO" -eq 1 ]; then
        PYTHONPATH="${test_roots//,/:}" "$PYTHON" -m pylint --rcfile=.pylintrc \
            --source-roots="$test_roots" --disable=all \
            --enable=fixme --fail-on= --fail-under=0 --score=n "${CODE_FILES[@]}" \
            "${TEST_FILES[@]}" || true
    fi
done

# The root conftest.py belongs to no app.
section "repo root"
[ "$DO_PYLINT" -eq 1 ] && run_pylint "conftest.py" "." 0 --disable="$TEST_DISABLE" --files conftest.py
[ "$DO_BANDIT" -eq 1 ] && run_bandit "conftest.py" --skip B101 --files conftest.py

if [ "$DO_DOCS" -eq 1 ]; then
    section "docs"
    echo "• mkdocs build --strict"
    if "$PYTHON" -m mkdocs build --strict --clean --quiet; then
        echo "✓ docs built in site/"
    else
        echo "✗ docs"
        FAILED=1
    fi
fi

echo
if [ "$FAILED" -eq 0 ]; then
    echo "Lint passed."
else
    echo "Lint failed: see the ✗ lines above."
fi
exit "$FAILED"
