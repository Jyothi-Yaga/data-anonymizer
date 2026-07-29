#!/usr/bin/env python3
"""set_plan.py — set which columns a plan anonymizes, without hand-editing JSON.

Usage:
  python set_plan.py <table> COL:type COL:type ...
    type ∈ person | email | phone | org | freetext | id | url
    (omit ':type' -> defaults to org)
Enables exactly the listed columns with the given types (mode auto: freetext vs
structured), disables everything else. Run `analyze <table>` first so the plan exists.
"""
import sys, json, os

if len(sys.argv) < 3:
    print(__doc__); sys.exit(1)
tbl = sys.argv[1]
specs = sys.argv[2:]
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_state')
p = os.path.join(STATE, f"{tbl}.plan.json")
if not os.path.exists(p):
    sys.exit(f"no plan at {p} — run `analyze {tbl}` first")
d = json.load(open(p, encoding='utf-8'))

want = {}
for s in specs:
    c, _, t = s.partition(':')
    want[c.lower()] = (c, t or 'org')

have = {col['column'].lower() for col in d['columns']}
n = 0
for col in d['columns']:
    k = col['column'].lower()
    if k in want:
        _, t = want[k]
        col['enabled'] = True
        col['type'] = t
        col['mode'] = 'freetext' if t == 'freetext' else 'structured'
        n += 1
    else:
        col['enabled'] = False
json.dump(d, open(p, 'w', encoding='utf-8'), indent=2, ensure_ascii=False)
missing = [want[k][0] for k in want if k not in have]
print(f"[{tbl}] enabled {n} column(s): " +
      ", ".join(f"{want[k][0]}:{want[k][1]}" for k in want if k in have))
if missing:
    print(f"  WARNING — not found in plan (check spelling): {missing}")
