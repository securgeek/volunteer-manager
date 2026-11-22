#!/usr/bin/env python3
"""Unit tests for the counts-grid reconciliation helpers. No database, no running stack.

These cover the decisions apply_grid makes about which slots to add and — the part worth
testing hardest — which to delete. Run from the backend directory:

    python3 test_grid_logic.py
"""
import sys

sys.path.insert(0, '.')
from routers.schedule import _reconcile_cell, _infer_prefix, _new_pos_key

fails = []


def eq(got, want, label):
    if got != want:
        fails.append(f"{label}\n     got  {got}\n     want {want}")
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


ORDER = ['sec1', 'sec2', 'sec3', 'sec4', 'sec5']


def free(*keys):
    """A cell holding these positions, none of them assigned."""
    return {k: None for k in keys}


print("\n_reconcile_cell — growing")
eq(_reconcile_cell(ORDER, {}, 3), (['sec1', 'sec2', 'sec3'], [], 0),
   "empty cell -> 3 takes the lowest three")
eq(_reconcile_cell(ORDER, free('sec1', 'sec2'), 4), (['sec3', 'sec4'], [], 0),
   "2 -> 4 adds the next two")
eq(_reconcile_cell(ORDER, free('sec2', 'sec5'), 4), (['sec1', 'sec3'], [], 0),
   "gaps are filled from the bottom, holes first")
eq(_reconcile_cell(ORDER, free('sec1'), 9), (['sec2', 'sec3', 'sec4', 'sec5'], [], 0),
   "asking for more than the category offers takes what exists")

print("\n_reconcile_cell — unchanged")
eq(_reconcile_cell(ORDER, free('sec1', 'sec2'), 2), ([], [], 0), "no change is a no-op")
eq(_reconcile_cell(ORDER, {}, 0), ([], [], 0), "empty stays empty")

print("\n_reconcile_cell — shrinking")
eq(_reconcile_cell(ORDER, free('sec1', 'sec2', 'sec3'), 1), ([], ['sec3', 'sec2'], 0),
   "3 -> 1 drops from the top down")
eq(_reconcile_cell(ORDER, free('sec1', 'sec2'), 0), ([], ['sec2', 'sec1'], 0),
   "-> 0 clears the cell")

print("\n_reconcile_cell — assigned slots are never removed")
eq(_reconcile_cell(ORDER, {'sec1': None, 'sec2': 'v9', 'sec3': None}, 1), ([], ['sec3', 'sec1'], 0),
   "keeps the assigned slot and drops both free ones to land on the asked-for count")
eq(_reconcile_cell(ORDER, {'sec1': None, 'sec2': 'v2', 'sec3': 'v3'}, 1), ([], ['sec1'], 1),
   "two assigned and one free: removes the free one, reports 1 short")
eq(_reconcile_cell(ORDER, {'sec1': 'v1', 'sec2': 'v2'}, 0), ([], [], 2),
   "every slot assigned: nothing removed, shortfall 2")
eq(_reconcile_cell(ORDER, {'sec1': 'v1', 'sec2': None, 'sec3': None}, 2), ([], ['sec3'], 0),
   "enough free slots above the assigned one, no shortfall")
eq(_reconcile_cell(ORDER, {'sec1': None, 'sec2': 'v2', 'sec3': None, 'sec4': None}, 2),
   ([], ['sec4', 'sec3'], 0),
   "walks past an assigned slot to find enough free ones")

print("\n_infer_prefix — new keys must join the run that is already there")
eq(_infer_prefix(['sec1', 'sec2', 'sec10'], 'Security'), 'sec', "a consistent run gives its stem")
eq(_infer_prefix([], 'Security'), 'security', "no positions falls back to the name")
eq(_infer_prefix(['sec1', 'reg1'], 'Security'), 'security', "disagreeing stems fall back to the name")
eq(_infer_prefix(['room-a1', 'room-a2'], 'Room'), 'room-a', "hyphens survive")
eq(_infer_prefix([], 'Room Monitors'), 'roommonitors', "the fallback strips to alphanumerics")
eq(_infer_prefix(['1', '2'], 'Odd'), 'odd', "all-digit keys leave no stem")

print("\n_new_pos_key")
eq(_new_pos_key('sec', 7, {'sec1', 'sec2'}), ('sec7', 7), "a free key is taken as is")
eq(_new_pos_key('sec', 1, {'sec1', 'sec2', 'sec3'}), ('sec4', 4), "a taken key skips to the next free")

print()
if fails:
    print(f"{len(fails)} failed:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("all grid logic tests passed")
