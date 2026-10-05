# Test Case Records - Order-sensitive Pattern

Pattern: `ORDER_SENSITIVE`. Fault default `RIVAL_RESPONSE`.
Guard: waits for `SEND` evidence on the same api.

## What the pattern simulates

Two concurrent calls to the same upstream. A rival (stale or superseded)
response arrives **before** the one we are waiting for.

Correct handling: recognise the answer as stale, do **not** commit on it,
and serve the customer something safe. Committing here is the bug - the
system acts on data it should have discarded.

---

## TC-OS-01 - Clean run, rival discarded

**Setup events** (`trace="run-os-clean"`)

| # | phase | served_from | effect_applied | fault |
|---|---|---|---|---|
| 1 | SEND | - | - | - |
| 2 | RECV | NONE | - | RIVAL_RESPONSE |
| 3 | RECV | MESSAGE | - | - |

**Expected:** `prem=False`, `ts=True`

**Reasoning:**
- `miss=False` - a `rival_response` fault is present (checked by value, not
  just "any fault", so an HTTP_500 would not satisfy this judge).
- `prem=False` - `premature = bool(rival_fired) and bool(effects)`. There
  are **no** effect events, so no premature commit happened.
- `mult=False` - zero effects.
- `cw=True` - MESSAGE is servable, so the customer was not left hanging.
- `ps=True` - no duplicate, no premature commit.

Note the judge checks `premature` twice, for CW and PS. That is deliberate:
"did we commit early" and "did we behave correctly" are related but
separate claims, and we want both flags honest.

---

## TC-OS-02 - Premature commit on rival data

**Setup events** (`trace="run-os-prem"`)

| # | phase | served_from | effect_applied | fault |
|---|---|---|---|---|
| 1 | SEND | - | - | - |
| 2 | RECV | NONE | - | RIVAL_RESPONSE |
| 3 | POST_EFFECT | - | yes | - |
| 4 | RECV | LIVE | - | - |

**Expected:** `prem=True`, `ts=False`

**Reasoning:** the rival response arrived, and then a commit happened
anyway (event 3). `premature = True`. This is the exact defect the pattern
exists to detect, so CW is also forced to `False` - serving the customer
from bad data is not "correctly withstanding" anything.

---

## TC-OS-03 - Missed fault

**Setup events** (`trace="run-os-miss"`)

| # | phase | served_from | effect_applied | fault |
|---|---|---|---|---|
| 1 | SEND | - | - | - |
| 2 | RECV | LIVE | - | - |

**Expected:** `miss=True`, `ts=False`

**Reasoning:** no `rival_response` fault. The race was never exercised, so
the drill proves nothing regardless of how healthy the trace looks.

---

## TC-OS-04 - Duplicate effect

**Setup events** (`trace="run-os-dup"`)

| # | phase | served_from | effect_applied | fault |
|---|---|---|---|---|
| 1 | SEND | - | - | - |
| 2 | POST_EFFECT | - | yes | - |
| 3 | POST_EFFECT | - | yes | - |
| 4 | RECV | LIVE | - | - |

**Expected:** `mult=True`, `ts=False`

**Reasoning:** two effects, `len(effects) = 2 > 1`. Note this trace has no
fault event, so `miss` is also `True`. Both flags fail independently, which
is correct: the run had a duplication problem *and* failed to exercise the
pattern.