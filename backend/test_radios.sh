#!/usr/bin/env bash
# Exercise the radio endpoints against a RUNNING stack. Nothing here is mocked —
# it logs in as a real user and writes real rows, so point it at a test instance
# if you would rather not see a TEST-RADIO in the Radios dialog afterwards.
#
#   ./test_radios.sh <username> <password> [base_url]
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
BARCODE="TEST-RADIO-$$"
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
        printf '  \033[32mPASS\033[0m  %-38s %s\n' "$name" "$status"
    else
        FAILS=$((FAILS + 1))
        printf '  \033[31mFAIL\033[0m  %-38s got %s, wanted %s\n' "$name" "$status" "$want"
    fi
    [ -n "$body" ] && printf '        %s\n' "$(echo "$body" | head -c 400)"
    return 0
}

# expect_body <substring> <name> — assert against the last response body.
expect_body() {
    if printf '%s' "$LAST_BODY" | grep -qF -- "$1"; then
        PASSES=$((PASSES + 1))
        printf '  \033[32mPASS\033[0m  %-38s %s\n' "$2" "found"
    else
        FAILS=$((FAILS + 1))
        printf '  \033[31mFAIL\033[0m  %-38s missing %s\n' "$2" "$1"
    fi
    return 0
}

# expect_count <substring> <n> <name> — assert a substring appears exactly n times.
expect_count() {
    local got
    got=$(printf '%s' "$LAST_BODY" | grep -oF -- "$1" | wc -l | tr -d ' ')
    if [ "$got" = "$2" ]; then
        PASSES=$((PASSES + 1))
        printf '  \033[32mPASS\033[0m  %-38s %s\n' "$3" "$got"
    else
        FAILS=$((FAILS + 1))
        printf '  \033[31mFAIL\033[0m  %-38s got %s, wanted %s\n' "$3" "$got" "$2"
    fi
    return 0
}

echo "Radio endpoints — $BASE (barcode $BARCODE)"
echo

check "login"                        200 POST login \
      -F "username=$USER_NAME" -F "password=$PASS"
check "list radios"                  200 GET  radios
check "checkout to a free-form name" 200 POST radios/checkout \
      -F "barcode=$BARCODE" -F "person_name=Harness Tester"

# A handoff is just a checkout to someone else: the open row closes itself.
check "handoff to another person"    200 POST radios/checkout \
      -F "barcode=$BARCODE" -F "person_name=Second Holder"
expect_body '"transferred_from":"Harness Tester"' "handoff names the old holder"

# Re-scanning to the current holder must change nothing at all.
check "re-scan to current holder"    200 POST radios/checkout \
      -F "barcode=$BARCODE" -F "person_name=Second Holder"
expect_body '"already_held":true' "re-scan reports already held"
check "re-scan is case-insensitive"  200 POST radios/checkout \
      -F "barcode=$BARCODE" -F "person_name=second holder"
expect_body '"already_held":true' "case-insensitive name match"

check "checkout with no recipient"   400 POST radios/checkout \
      -F "barcode=$BARCODE-B"
check "checkout with empty barcode"  400 POST radios/checkout \
      -F "barcode=" -F "person_name=Nobody"
check "checkin"                      200 POST radios/checkin \
      -F "barcode=$BARCODE"
check "second checkin rejected"      400 POST radios/checkin \
      -F "barcode=$BARCODE"
check "checkin of unknown barcode"   400 POST radios/checkin \
      -F "barcode=NEVER-SEEN-$$"
check "history"                      200 GET  "radios/history?limit=50"
# Exactly two closed rows for this radio: the handoff, and the final check-in.
# Three would mean an idempotent re-scan wrote a phantom return.
expect_count "$BARCODE" 2 "history has 2 rows for this radio"

echo
echo "$PASSES passed, $FAILS failed"
echo
echo "The test radio is still on record. To remove it:"
echo "  docker compose exec postgres psql -U scheduler -d scheduler \\"
echo "    -c \"DELETE FROM radio_checkouts WHERE barcode LIKE 'TEST-RADIO-%';\" \\"
echo "    -c \"DELETE FROM radios WHERE barcode LIKE 'TEST-RADIO-%';\""

[ "$FAILS" -eq 0 ]
