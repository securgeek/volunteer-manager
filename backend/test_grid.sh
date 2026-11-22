#!/usr/bin/env bash
# Exercise /api/schedule/apply_grid against a RUNNING stack. Nothing here is mocked — it
# writes real rows. It works entirely inside a throwaway category (ZZ-Grid-<pid>) on a
# day/time pair that already exists, and deletes everything it made at the end.
#
#   ./test_grid.sh <username> <password> [base_url]
#
# base_url defaults to https://localhost (curl -k, since the cert is self-signed).
#
# The one thing this CANNOT cover on its own is the assigned-slot guard: assigning a
# volunteer needs a real vol_id. Do that part by hand — the script prints instructions
# at the end.

set -uo pipefail

USER_NAME="${1:-}"
PASS="${2:-}"
BASE="${3:-https://localhost}"

if [ -z "$USER_NAME" ] || [ -z "$PASS" ]; then
    echo "usage: $0 <username> <password> [base_url]" >&2
    exit 2
fi

command -v jq >/dev/null || { echo "this harness needs jq" >&2; exit 2; }

JAR="$(mktemp)"
CAT="ZZ-Grid-$$"
PASSES=0
FAILS=0
LAST_BODY=""

cleanup() { rm -f "$JAR"; }
trap cleanup EXIT

req() {  # req <method> <path> [curl args...] -> sets LAST_BODY, echoes status
    local method="$1" path="$2"; shift 2
    local out
    out=$(curl -sk -b "$JAR" -c "$JAR" -X "$method" "$@" -w $'\n%{http_code}' "$BASE/api/$path")
    LAST_BODY="${out%$'\n'*}"
    echo "${out##*$'\n'}"
}

check() {  # check <name> <expected_status> <method> <path> [curl args...]
    local name="$1" want="$2"; shift 2
    local status; status=$(req "$@")
    if [ "$status" = "$want" ]; then
        PASSES=$((PASSES + 1)); printf '  \033[32mPASS\033[0m  %-44s %s\n' "$name" "$status"
    else
        FAILS=$((FAILS + 1));  printf '  \033[31mFAIL\033[0m  %-44s got %s, wanted %s\n' "$name" "$status" "$want"
        printf '        %s\n' "$(echo "$LAST_BODY" | head -c 300)"
    fi
}

expect_jq() {  # expect_jq <jq filter> <expected> <name>
    local got; got=$(printf '%s' "$LAST_BODY" | jq -r "$1" 2>/dev/null)
    if [ "$got" = "$2" ]; then
        PASSES=$((PASSES + 1)); printf '  \033[32mPASS\033[0m  %-44s %s\n' "$3" "$got"
    else
        FAILS=$((FAILS + 1));  printf '  \033[31mFAIL\033[0m  %-44s got %s, wanted %s\n' "$3" "$got" "$2"
    fi
}

# count_slots <cell> — how many slots the test category has in "day|time"
count_slots() {
    req GET schedule/structure >/dev/null
    printf '%s' "$LAST_BODY" | jq --arg cat "$CAT" --arg cell "$1" '
        (.positions | map(select(.pos_cat == $cat) | .pos_key)) as $keys
        | [.slots[] | select((.day_key + "|" + .time_key) == $cell and (.pos_key | IN($keys[])))]
        | length'
}

apply() {  # apply <count> [mode] [names-json]
    local n="$1" mode="${2:-numbered}" names="${3:-null}"
    local cfg
    cfg=$(jq -nc --arg cat "$CAT" --arg cell "$CELL" --argjson n "$n" \
                 --arg mode "$mode" --argjson names "$names" '
        {cells: [$cell],
         categories: [{name: $cat, mode: $mode, names: $names, counts: {($cell): $n}}]}')
    req POST schedule/apply_grid -F "config=$cfg"
}

echo "Counts grid — $BASE (category $CAT)"
echo

check "login" 200 POST login -F "username=$USER_NAME" -F "password=$PASS"

# Borrow the first real day/time pair that exists; the grid only writes to declared pairs.
req GET schedule/structure >/dev/null
CELL=$(printf '%s' "$LAST_BODY" | jq -r '.day_times[0] | .day_key + "|" + .time_key')
if [ "$CELL" = "null" ] || [ -z "$CELL" ]; then
    echo "  no day/time pairs exist yet — build a schedule first" >&2
    exit 2
fi
echo "  using cell $CELL"
echo

check "create test category" 200 POST schedule/upsert_category -F "cat_name=$CAT"

echo
echo "-- growing --"
check "0 -> 4" 200 POST schedule/apply_grid \
      -F "config=$(jq -nc --arg c "$CAT" --arg k "$CELL" \
          '{cells:[$k],categories:[{name:$c,mode:"numbered",counts:{($k):4}}]}')"
expect_jq '.slots_created' '4' "created 4 slots"
expect_jq '.positions_created' '4' "created 4 positions"
[ "$(count_slots "$CELL")" = "4" ] \
  && { PASSES=$((PASSES+1)); printf '  \033[32mPASS\033[0m  %-44s 4\n' "cell really holds 4"; } \
  || { FAILS=$((FAILS+1));   printf '  \033[31mFAIL\033[0m  %-44s %s\n' "cell really holds 4" "$(count_slots "$CELL")"; }

apply 6 >/dev/null
expect_jq '.slots_created' '2' "4 -> 6 adds exactly 2"

echo
echo "-- idempotence --"
apply 6 >/dev/null
expect_jq '.slots_created' '0' "re-applying the same grid adds nothing"
expect_jq '.slots_removed' '0' "re-applying the same grid removes nothing"

echo
echo "-- shrinking --"
apply 2 >/dev/null
expect_jq '.slots_removed' '4' "6 -> 2 removes exactly 4"
expect_jq '.blocked | length' '0' "nothing blocked (no assignments)"
[ "$(count_slots "$CELL")" = "2" ] \
  && { PASSES=$((PASSES+1)); printf '  \033[32mPASS\033[0m  %-44s 2\n' "cell really holds 2"; } \
  || { FAILS=$((FAILS+1));   printf '  \033[31mFAIL\033[0m  %-44s %s\n' "cell really holds 2" "$(count_slots "$CELL")"; }

echo
echo "-- named mode renames positions in order --"
apply 3 named '["Alpha Room","Bravo Room","Charlie Room"]' >/dev/null
req GET schedule/structure >/dev/null
expect_jq "[.positions[] | select(.pos_cat == \"$CAT\") | .pos_str] | join(\",\")" \
          "Alpha Room,Bravo Room,Charlie Room" "names applied in order"

echo
echo "-- new keys join the run already there, they do not start a second one --"
# Every key in the category must share one stem: a category named "ZZ-Grid-123" whose
# positions are zzgrid1.. must not sprout a differently-prefixed key when it grows.
expect_jq "[.positions[] | select(.pos_cat == \"$CAT\") | (.pos_key | sub(\"[0-9]+$\"; \"\"))] | unique | length" \
          "1" "all position keys share one stem"

echo
echo "-- rejected input --"
check "unknown category" 400 POST schedule/apply_grid \
      -F "config=$(jq -nc --arg k "$CELL" \
          '{cells:[$k],categories:[{name:"No-Such-Category-Xyz",counts:{($k):1}}]}')"
check "cell that is not a declared pair" 400 POST schedule/apply_grid \
      -F "config=$(jq -nc --arg c "$CAT" '{cells:["nope|9999"],categories:[{name:$c,counts:{}}]}')"
check "non-numeric count" 400 POST schedule/apply_grid \
      -F "config=$(jq -nc --arg c "$CAT" --arg k "$CELL" \
          '{cells:[$k],categories:[{name:$c,counts:{($k):"lots"}}]}')"
check "negative count" 400 POST schedule/apply_grid \
      -F "config=$(jq -nc --arg c "$CAT" --arg k "$CELL" \
          '{cells:[$k],categories:[{name:$c,counts:{($k):-1}}]}')"
check "count over the cap" 400 POST schedule/apply_grid \
      -F "config=$(jq -nc --arg c "$CAT" --arg k "$CELL" \
          '{cells:[$k],categories:[{name:$c,counts:{($k):999}}]}')"
check "malformed config" 400 POST schedule/apply_grid -F "config=not json"
check "no cells" 400 POST schedule/apply_grid \
      -F "config=$(jq -nc --arg c "$CAT" '{cells:[],categories:[{name:$c,counts:{}}]}')"

echo
echo "-- cleanup: zeroing the row retires the whole category --"
apply 0 >/dev/null
expect_jq '.slots_removed' '3' "-> 0 removes the last 3 slots"
[ "$(count_slots "$CELL")" = "0" ] \
  && { PASSES=$((PASSES+1)); printf '  \033[32mPASS\033[0m  %-44s 0\n' "cell is empty"; } \
  || { FAILS=$((FAILS+1));   printf '  \033[31mFAIL\033[0m  %-44s %s\n' "cell is empty" "$(count_slots "$CELL")"; }
check "delete test category" 200 POST schedule/delete_category -F "cat_name=$CAT"

echo
echo "$PASSES passed, $FAILS failed"
cat <<'MANUAL'

Not covered here — check this one by hand, it is the guard that matters most:
  1. In the editor, set a category to 4 at some time, Apply.
  2. Assign a volunteer to one of those slots in the schedule view.
  3. Back in the grid, set that cell to 0 and Apply.
  Expect: the three empty slots go, the assigned one stays, and a warning names
  the category and cell with "1 kept".
MANUAL

[ "$FAILS" -eq 0 ]
