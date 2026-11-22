#!/usr/bin/env python3
"""Unit tests for check-in codes and the early/late rule. No database, no running stack.

Run from the backend directory:

    python3 test_checkin_logic.py
"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, '.')
from checkin_codes import make_code, verify_code, shift_start, shift_end, classify

fails = []


def eq(got, want, label):
    if got != want:
        fails.append(f"{label}\n     got  {got}\n     want {want}")
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


KEY = bytes(range(32))
OTHER = bytes(32)

print("\ncodes")
code = make_code(KEY, "1042", "sat_0800_sec3")
eq(verify_code(KEY, code), ("1042", "sat_0800_sec3"), "a code verifies under its key")
eq(make_code(KEY, "1042", "sat_0800_sec3"), code, "the same shift always gives the same code")
eq(verify_code(OTHER, code), None, "another key (after a rotation) rejects it")
eq(verify_code(KEY, "  " + code + "\n"), ("1042", "sat_0800_sec3"), "a wedge scanner's whitespace is ignored")
eq(verify_code(KEY, code.replace("sec3", "sec4")), None, "a changed slot fails the MAC")
other_vol = make_code(KEY, "1043", "sat_0800_sec3").split(".")[2]
parts = code.split(".")
eq(verify_code(KEY, ".".join([parts[0], parts[1], other_vol, parts[3]])), None,
   "a changed vol_id fails the MAC")
eq(verify_code(KEY, code[:-1] + ("A" if code[-1] != "A" else "B")), None, "a changed MAC fails")
eq(verify_code(KEY, "BSC2" + code[4:]), None, "an unknown version prefix fails")
eq(verify_code(KEY, code + ".x"), None, "an extra part fails")
eq(verify_code(KEY, ""), None, "empty fails")
eq(verify_code(KEY, "RADIO-0017"), None, "a radio barcode fails")
eq(verify_code(KEY, "BSC1.sat_0800_sec3.!!!.abc"), None, "bad base64 fails without raising")
eq(verify_code(KEY, "BSC1.SAT_0800_sec3." + parts[2] + "." + parts[3]), None,
   "a slot key outside the key rule fails")
eq(verify_code(KEY, "x" * 400), None, "an over-long code fails")
odd = make_code(KEY, "p-1a2b3c4d.é", "sun_1500-1700_reg10")
eq(verify_code(KEY, odd), ("p-1a2b3c4d.é", "sun_1500-1700_reg10"),
   "vol_ids with dots and non-ASCII survive the encoding")

print("\nshift_start / shift_end")
start = shift_start("2027-04-24", "0800-1200", "America/New_York")
eq(start.isoformat(), "2027-04-24T08:00:00-04:00", "date + range start in the event zone (EDT)")
eq(shift_start("2027-01-09", "930-1130", "America/New_York").isoformat(),
   "2027-01-09T09:30:00-05:00", "three-digit start, standard time")
eq(shift_start("", "0800-1200", "America/New_York"), None, "no event_date can't be judged")
eq(shift_start("2027-04-24", "Morning", "America/New_York"), None, "a time_str that isn't a range")
eq(shift_start("2027-02-30", "0800-1200", "America/New_York"), None, "an impossible date")
eq(shift_start("2027-04-24", "0800-1200", "Not/AZone"), None, "an unknown zone")
eq(shift_end(start, "0800-1200").isoformat(), "2027-04-24T12:00:00-04:00", "end of a day shift")
night = shift_start("2027-04-24", "2200-0200", "America/New_York")
eq(shift_end(night, "2200-0200").isoformat(), "2027-04-25T02:00:00-04:00", "a shift past midnight")

print("\nclassify")
utc = start.astimezone(timezone.utc)
eq(classify(utc - timedelta(minutes=46), start), ("early", 46), "46 min before is early")
eq(classify(utc - timedelta(minutes=45), start), ("on_time", 0), "exactly 45 before is on time")
eq(classify(utc - timedelta(minutes=5), start), ("on_time", 0), "5 min before is on time")
eq(classify(utc + timedelta(minutes=10), start), ("on_time", 0), "exactly 10 after is on time")
eq(classify(utc + timedelta(minutes=10, seconds=30), start), ("late", 10), "10.5 min after is late")
eq(classify(utc + timedelta(hours=2), start), ("late", 120), "2 h after is late")
eq(classify(utc - timedelta(days=1), start), ("early", 1440), "the day before is early")
eq(classify(datetime.now(timezone.utc), None), ("unknown", 0), "no start is unknown")

print()
if fails:
    print(f"{len(fails)} failed:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("all check-in logic tests passed")
