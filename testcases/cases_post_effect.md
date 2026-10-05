# Test Case Records - Post-effect Pattern

Pattern: `POST_EFFECT`. Fault default `DROP_RESPONSE`.
Guard: waits for `POST_EFFECT` evidence on the same api.

## What the pattern simulates

A state-changing request reaches the upstream. The upstream **completes the
action**. Then the response is lost in transit.

The outcome is "outcome-uncertain": we do not know whether the work happened,
even though nothing is broken. This is the case behind the 51% figure in the
research (51.0% of 38,631 real operations are POST/PUT/PATCH/DELETE).

Correct handling: do **not** blindly re-send the action. Serve the customer
from cache or a clear message, and report the action as possibly-committed.

---

## TC-PE-01 - Clean run, action performed once

**Setup events** (`api_key="weather"`, `trace="run-pe-clean"`)

| # | phase | served_from | effect_applied | fault |
|---|---|---|---|---|
| 1 | SEND | - | - | - |
| 2 | POST_EFFECT | - | yes | - |
| 3 | RECV | NONE | - | DROP_RESPONSE |
| 4 | RECV | CACHE | - | - |

**Spec:** `post_effect_drill("run-pe-clean", "weather", k=1, n=1, idempotent=False)`

**Expected:** `ts=True`, `mult=False`

**Reasoning:**
- `miss=False` - a fault event exists.
- `mult=False` - exactly one `effect_applied` event.
- `cw=True` - event 4 serves CACHE, which counts as servable.
- `ps=True` - no duplicate, no raw 5xx leaked.
- `prem=False` - the fault (index 2) is not before the guard evidence (index 1).

All five conditions hold, so `TS=True`.

---

## TC-PE-02 - Duplicate action (double dispatch)

**Setup events** (`trace="run-pe-dup"`)

| # | phase | served_from | effect_applied | fault |
|---|---|---|---|---|
| 1 | SEND | - | - | - |
| 2 | POST_EFFECT | - | yes | - |
| 3 | RECV | NONE | - | DROP_RESPONSE |
| 4 | POST_EFFECT | - | yes | - |
| 5 | RECV | LIVE | - | - |

**Spec:** `post_effect_drill("run-pe-dup", "weather", k=1, n=1, idempotent=False)`

**Expected:** `mult=True`, `ts=False`, notes mention "duplicat"

**Reasoning:** events 2 and 4 both carry `effect_applied=True`, so
`len(effects) = 2 > 1`. This is exactly the double-charge case: the retry
re-ran a non-idempotent action. TS must fail even though the customer got
an answer, which is the point - a served customer does not excuse a
duplicated action.

---

## TC-PE-03 - Premature fault

**Setup events** (`trace="run-pe-prem"`)

| # | phase | served_from | effect_applied | fault |
|---|---|---|---|---|
| 1 | SEND | - | - | - |
| 2 | PRE_EFFECT | NONE | - | DROP_RESPONSE |
| 3 | POST_EFFECT | - | yes | - |
| 4 | RECV | CACHE | - | - |

**Expected:** `prem=True`, `ts=False`

**Reasoning:** `_fired_before_window` compares the earliest fault index
(1) with the earliest guard-evidence index (2). Fault comes first, so the
injector fired before the dangerous window opened. The run tells us nothing
about post-commit behaviour, so it cannot be a pass.

This is the failure mode that makes naive injectors meaningless: they break
the call early and then report a "result" for a situation that never occurred.

---

## TC-PE-04 - Missed fault

**Setup events** (`trace="run-pe-miss"`)

| # | phase | served_from | effect_applied | fault |
|---|---|---|---|---|
| 1 | SEND | - | - | - |
| 2 | POST_EFFECT | - | yes | - |
| 3 | RECV | LIVE | - | - |

**Expected:** `miss=True`, `ts=False`

**Reasoning:** no event carries a fault. Without a fault there is no
outcome-uncertain window, so a TS=1 here would be a fabricated claim. The
scorer deliberately refuses to award a pass for an uneventful run.