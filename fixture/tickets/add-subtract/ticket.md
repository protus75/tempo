---
state: confirmed
source: human
priority: P2
kind: feature
---

## Depends on
none

## Context
- calc.py
- test_calc.py

## Goal / Why
`calc.py` gains `subtract(a, b)`, so the fixture has one small real change to make.

## Scope in / Scope out
In: `subtract` and its check in `test_calc.py`. Out: every other file.

## Scope fence
- calc.py
- test_calc.py

## Acceptance criteria
1. `python3 test_calc.py` exits 0 and asserts `subtract(5, 3) == 2`.

## Verification
```
python3 test_calc.py
```

## Definition of rejected
Needs a dependency outside the standard library.

## Time budget
- stuck: 20m
