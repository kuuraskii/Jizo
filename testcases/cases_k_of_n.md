# Test Case Records - k-of-n Pattern + Guard Mechanics

Pattern: `K_OF_N`. Guard: waits for `SEND` evidence.
The headline demo case: **4 calls, only call #3 breaks.**

## What the pattern simulates

Several identical calls in a row. Exactly one of them - the k-th - fails.
The rest are healthy.

This is the precision test. A blunt injector breaks everything and looks
"safe" because nothing got through; a precise injector degrades only the
one call that actually failed. Only the second one demonstrates real
protection.

---

## TC-K-01 - Headline case, only call 3 degrades

**Setup events** (`trace="run-k3-clean"`)

| # | phase | served_from | status | fault |
|---|---|---|---|---|
| 1 | SEND | - | - | - |
| 2 | RECV | LIVE | 200 | - |
| 3 | SEND | - | - | - |
| 4 | RECV | NONE | - | HTTP_500 |
| 5 | RECV | CACHE | - | - |
| 6 | SEND | - | - | - |
| 7 | RECV | LIVE | 200 | - |

**Spec:** `k_of_n_drill("run-k3-clean", "weather", k=3, n=4)`

**Expected:** `ts=True`, `mult=False`, `fault_occurrences == [3]`

**Reasoning:**
- Occurrence counting is per `(traceId, api_key, phase)`. The four RECV
  events get occurrences 1, 2, 3, 4. Only RECV #3 (the 4th event overall)
  carries the fault.
- `fired_on = [3]`, so `miss=False` (3 is in there) and `prem=False`
  (nothing leaked onto 1, 2 or 4).
- `healthy` counts RECV events that are not occurrence 3, have no fault, and
  served LIVE. That is occurrences 1, 2 and 4 -> 3 of 3 expected.
- `mult=False`, no effects.

The extra assertion `fault_occurrences == [3]` is the strongest one in the
suite: it checks the *exact* occurrence rather than accepting "a fault
happened somewhere". That is what makes the stage claim defensible.

---

## TC-K-02 - Fault on the wrong occurrence

**Setup events** (`trace="run-k-prem"`)

| # | phase | served_from | fault |
|---|---|---|---|
| 1 | SEND | - | - |
| 2 | RECV | LIVE | - |
| 3 | SEND | - | - |
| 4 | RECV | NONE | HTTP_500 |
| 5 | SEND | - | - |
| 6 | RECV | LIVE | - |
| 7 | SEND | - | - |
| 8 | RECV | LIVE | - |

**Spec:** `k_of_n_drill("run-k-prem", "weather", k=3, n=4)`

**Expected:** `prem=True`, `ts=False`

**Reasoning:** `fired_on = [2]`. Since 2 is not the target k=3, `miss=True`
as well, and because `premature` triggers on *any* occurrence other than k,
`prem=True`. A blunt injector fails this test; a precise one passes.

---

## TC-K-03 - Missed fault

**Setup events** (`trace="run-k-miss"`): 4 clean calls (SEND + LIVE RECV each)

**Expected:** `miss=True`, `ts=False`

**Reasoning:** `fired_on = []`, so 3 is not present. Nothing broke, so the
drill proves nothing.

---

## TC-K-04 - Duplicate effect during partial failure

**Setup events** (`trace="run-k-dup"`)

| # | phase | served_from | effect_applied | fault |
|---|---|---|---|---|
| 1 | SEND | - | - | - |
| 2 | POST_EFFECT | - | yes | - |
| 3 | RECV | LIVE | - | - |
| 4 | SEND | - | - | - |
| 5 | RECV | LIVE | - | - |
| 6 | SEND | - | - | - |
| 7 | RECV | NONE | - | HTTP_500 |
| 8 | POST_EFFECT | - | yes | - |
| 9 | SEND | - | - | - |
| 10 | RECV | LIVE | - | - |

**Expected:** `mult=True`, `ts=False`

**Reasoning:** events 2 and 8 both apply an effect -> `len(effects) = 2 > 1`.
The drill correctly fired on occurrence 3, but a duplicate side effect
means the run fails regardless. This encodes the priority: **correctness of
the side effect outranks whether the fault was well-aimed.**

---

# Guard mechanics

The guard is the mechanism that makes the fault precise. These are the tests
that pin its behaviour.

## TC-G-01 - Guard blocks until the window opens

**Sequence:** record SEND only, ask `should_fire`, then record POST_EFFECT,
ask again.

**Expected:** `False` (reason contains "waiting for guard evidence"), then `True`

**Reasoning:** the guard requires `min_count=1` of POST_EFFECT evidence.
Before it exists, `guard_satisfied` is `False` and the guard refuses
regardless of occurrence. This is the After-guard behaviour adapted from
temporal fault-injection research: the fault may only fire once the risky
moment is real.

## TC-G-02 - Fires only on the exact k

**Sequence:** 4 iterations of record SEND -> `should_fire` -> record RECV.

**Expected:** `fired_at == [3]`

**Reasoning:** `should_fire` computes the occurrence of the call it is
*about to authorise* (`seen + 1`), not the count of calls already logged.
Occurrences 1, 2 are held ("occurrence < target k"); occurrence 3 fires;
occurrence 4 is refused ("target window already passed"). This asymmetry
is intentional - a late fire would hit a healthy call.

**Implementation note:** this off-by-one was a real bug caught by this test.
Reading the count *after* logging made the evaluator see 0 and never fire.

## TC-G-03 - Refusal after the window has passed

**Setup:** k=1 target, but two calls already completed.

**Expected:** `should_fire=False`, reason contains "already passed"

**Reasoning:** prevents a stale, still-pending fault from landing on a later
healthy call - the injector equivalent of a stray shot.

## TC-B-01 - Occurrence counters are scoped

**Setup:** 2 SENDs on `alpha`, 1 SEND on `beta`.

**Expected:** `alpha` SEND = 2, `beta` SEND = 1, `alpha` RECV = 0

**Reasoning:** counters are keyed by `(trace_id, api_key, phase)`. Without
this scoping, concurrent fan-out to two APIs would corrupt each other's k -
and since JIZO is generic, any number of APIs may be in flight at once.

## TC-B-02 - Traces stay isolated

**Setup:** one SEND each on `trace-a` and `trace-b`.

**Expected:** 1 event per trace, both traces known

**Reasoning:** concurrent users must never see each other's evidence. This
is the isolation guarantee that makes the scorer trustworthy under load.

## TC-B-03 - Zero min_count is rejected

**Setup:** `GuardAfter(..., min_count=0)`

**Expected:** raises `ValueError`

**Reasoning:** a guard requiring zero evidence would be permanently
satisfied, i.e. no guard at all. Failing fast is better than silently
disabling the safety mechanism.

## TC-S-01 - Helpers pair target and guard correctly

**Setup:** build all three specs.

**Expected:** post-effect guards on POST_EFFECT, order-sensitive on SEND,
k-of-n has `target.occurrence == 3` and `total_occurrences == 4`, and all
three `Pattern` members are covered.

**Reasoning:** the most common source of bogus drill results is a
hand-written guard that does not match the intent. These helpers exist so
that pairing is never manual, and this test keeps them honest.