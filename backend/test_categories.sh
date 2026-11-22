#!/usr/bin/env bash
# Exercise the category color endpoints against a RUNNING stack. Nothing here is
# mocked — it logs in as a real user and writes real rows. It creates a throwaway
# category, never touches an existing one, and deletes it at the end (a category
# with no positions deletes cleanly).
#
#   ./test_categories.sh <username> <password> [base_url]
#
# base_url defaults to https://localhost (curl -k, since the cert is self-signed).

set -uo pipefail

USER_NAME="${1:-}"
PASS="${2:-}"
BASE="${3:-https://localhost}"

if [ -z "$USER_NAME" ] || [ -z "$PASS" ]; then
    echo "usage: $0 <username> <password> [base_url]" >&2
    exit 2
fi

JAR="$(mktemp)"
CAT="ZZ-Test-Cat-$$"
PASSES=0
FAILS=0
LAST_BODY=""

cleanup() { rm -f "$JAR"; }
trap cleanup EXIT

# check <name> <expected_status> <method> <path> [curl form args...]
check() {
    local name="$1" want="$2" method="$3" path="$4"; shift 4
    local out status body
    out=$(curl -sk -b "$JAR" -c "$JAR" -X "$method" "$@" \
               -w $'\n%{http_code}' "$BASE/api/$path")
    status="${out##*$'\n'}"
    body="${out%$'\n'*}"
    LAST_BODY="$body"
    if [ "$status" = "$want" ]; then
        PASSES=$((PASSES + 1))
        printf '  \033[32mPASS\033[0m  %-40s %s\n' "$name" "$status"
    else
        FAILS=$((FAILS + 1))
        printf '  \033[31mFAIL\033[0m  %-40s got %s, wanted %s\n' "$name" "$status" "$want"
        printf '        %s\n' "$(echo "$body" | head -c 300)"
    fi
    return 0
}

# expect_body <substring> <name> — assert against the last response body.
expect_body() {
    if printf '%s' "$LAST_BODY" | grep -qF -- "$1"; then
        PASSES=$((PASSES + 1))
        printf '  \033[32mPASS\033[0m  %-40s %s\n' "$2" "found"
    else
        FAILS=$((FAILS + 1))
        printf '  \033[31mFAIL\033[0m  %-40s missing %s\n' "$2" "$1"
    fi
    return 0
}

# reject_body <substring> <name> — assert the last response does NOT contain it.
reject_body() {
    if printf '%s' "$LAST_BODY" | grep -qF -- "$1"; then
        FAILS=$((FAILS + 1))
        printf '  \033[31mFAIL\033[0m  %-40s unexpectedly found %s\n' "$2" "$1"
    else
        PASSES=$((PASSES + 1))
        printf '  \033[32mPASS\033[0m  %-40s absent\n' "$2"
    fi
    return 0
}

echo "Category colors — $BASE (category $CAT)"
echo

check "login"                          200 POST login \
      -F "username=$USER_NAME" -F "password=$PASS"

# Every existing category should have a color: the 1.0 schema backfilled the four
# originals, and anything else was given a default when it was created.
check "structure exposes cat_color"    200 GET  schedule/structure
expect_body '"cat_color"' "cat_color present in structure"
reject_body '"cat_color":null' "no category left without a color"

# ── a new category gets a default color without being asked ─────────────────
check "add category, no color"         200 POST schedule/upsert_category \
      -F "cat_name=$CAT"
check "re-read structure"              200 GET  schedule/structure
expect_body "\"cat_name\":\"$CAT\"" "new category is listed"

# ── setting a color ─────────────────────────────────────────────────────────
check "set color"                      200 POST schedule/upsert_category \
      -F "cat_name=$CAT" -F "cat_color=#123456"
check "re-read structure"              200 GET  schedule/structure
expect_body '"cat_color":"#123456"'    "color round-trips"

# Bare hex is accepted (PrimeVue's ColorPicker sends it without the '#').
check "set color, no leading hash"     200 POST schedule/upsert_category \
      -F "cat_name=$CAT" -F "cat_color=abcdef"
check "re-read structure"              200 GET  schedule/structure
expect_body '"cat_color":"#abcdef"'    "bare hex is normalized"

# ── re-adding an existing name must not disturb its color ───────────────────
check "re-add same name, no color"     200 POST schedule/upsert_category \
      -F "cat_name=$CAT"
check "re-read structure"              200 GET  schedule/structure
expect_body '"cat_color":"#abcdef"'    "re-add preserves the color"

# ── rejected input never reaches the style attribute ─────────────────────────
check "reject non-hex color"           400 POST schedule/upsert_category \
      -F "cat_name=$CAT" -F "cat_color=red"
check "reject 3-digit hex"             400 POST schedule/upsert_category \
      -F "cat_name=$CAT" -F "cat_color=#abc"
check "reject injected style"          400 POST schedule/upsert_category \
      -F "cat_name=$CAT" -F "cat_color=#fff;background:url(x)"

# ── the schedule payload carries the color per category ─────────────────────
check "get_schedule"                   200 GET  get_schedule
expect_body '"color"'                  "schedule categories carry color"

# ── reordering must not clobber colors ──────────────────────────────────────
check "reorder (existing order)"       200 GET  schedule/structure
ORDER=$(printf '%s' "$LAST_BODY" \
        | grep -o '"cat_name":"[^"]*"' | sed 's/"cat_name":"//;s/"$//' | paste -sd, -)
check "reorder categories"             200 POST schedule/reorder_categories \
      -F "cat_names=$ORDER"
check "re-read structure"              200 GET  schedule/structure
expect_body '"cat_color":"#abcdef"'    "reorder preserves the color"

# ── cleanup ──────────────────────────────────────────────────────────────────
check "delete test category"           200 POST schedule/delete_category \
      -F "cat_name=$CAT"

echo
echo "$PASSES passed, $FAILS failed"

[ "$FAILS" -eq 0 ]
