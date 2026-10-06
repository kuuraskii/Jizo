# Riya - P1 Scoring Implementation

Author: **Riya** (P1 scorer co-owner, S25CSEU0213)
Scope: `backend/scoring.py` - the Temporal Success scorer for JIZO.
Status: complete, `61 passed / 0 failed`.

---

## 1. What I implemented

I implemented `backend/scoring.py`, the part of JIZO that decides whether a
fault-injection drill actually proved anything.

JIZO deliberately breaks upstream APIs on purpose so that resilience can be
demonstrated rather than asserted. But breaking an API is easy and easy to do
uselessly: you can fire a fault too early, fire it on the wrong call, never
fire it at all, or fire it and accidentally perform the action twice. Each of
those produces a green-looking number that proves nothing. For a tool whose
whole purpose is producing trustworthy reliability claims, **a false pass is
worse than a crash**.

`scoring.py` is the grader that rules those cases out. It takes two inputs:

* a `DrillSpec` - what we intended to do (which pattern, which fault, which
  API, which call number k, which guard)
* a `list[EvidenceEvent]` - what actually happened, recorded by the evidence
  bus

and returns the five flags CW / PS / Prem / Miss / Mult plus one headline
verdict, Temporal Success (TS).

The scorer does not inject anything and does not observe anything. It reads a
finished trace and judges it. That is the whole job.

---

## 2. Files I changed

Changed by me:

| File | What changed |
|---|---|
| `backend/scoring.py` | **New file.** The whole scorer: shared evidence handling, three pattern judges, `evaluate()`, `score_run()` |
| `backend/__init__.py` | **Additive only.** Added `from .scoring import evaluate, score_run` and their two `__all__` entries, so `from backend import score_run` works as `backend/README.md` documents |
| `tests/test_scoring.py` | **Two lines.** Added `evaluate` and `score_run` to the existing `from backend import (...)` block. They were used throughout the file but never imported, so all 25 tests failed on import with `NameError` |
| `notes/RIYA_P1_SCORING.md` | This document |

**Important files I did NOT change:**

| File | Owner | Why untouched |
|---|---|---|
| `backend/schemas.py` | Pushkar | Frozen contract. All parts of the project are written against these exact shapes; renaming or removing a field breaks every other part at once |
| `backend/faults.py` | Pushkar | Evidence bus + guard engine were already complete. I only read from them |
| `tests/test_temporal.py` | Pushkar | Already complete (36 tests: guards, bus bookkeeping, `ApiPolicy`, `FiRun`, schema validation). It passes unmodified |

I also left `requirements.txt`, `backend/README.md`, `notes/README.md`,
`testcases/*` and `edgecases/README.md` alone. No test was edited, weakened or
deleted to make the suite green - the implementation was changed to match the
tests instead.

---

## 3. Scoring pipeline

```
DrillSpec  +  EvidenceEvent timeline
        |
        v
   evaluate()            <- reads the evidence, dispatches to a judge
        |
        v
   DrillOutcome          <- five flags + human-readable notes
        |
        v
   score_run()           <- copies the flags, attaches timeline + spec
        |
        v
   ScoreResult           <- ts, cw, ps, prem, miss, mult, timeline, spec, notes
```

**`evaluate(spec, events) -> DrillOutcome`** is the engine. It copies the
events into a local list, looks up the judge for `spec.pattern` in a small
`_JUDGES` dictionary, and returns that judge's `DrillOutcome`.

**`DrillOutcome`** is the raw finding: `correct_withstand`, `policy_success`,
`premature`, `missed`, `duplicate`, and a list of explanatory `notes`. It does
**not** carry a separate `ts` field - `ts` is a computed property on the model
(see section 4).

**`score_run(spec, events) -> ScoreResult`** is the public wrapper, and it is
deliberately thin. It calls `evaluate()`, copies the five flags straight out of
the outcome, and reads `ts` from the outcome's property. So
`score_run(...).ts` and `evaluate(...).ts` cannot disagree - there is only one
definition of the formula, and `score_run` never recomputes it.

`score_run` additionally attaches:

* `timeline` - the events it graded, so a consumer can show or re-check them
* `spec` - what the drill asked for, so intent travels with the verdict
* `notes` - plain-English reasons, ready for the dashboard or a log line

`ScoreResult.explain()` renders the verdict as one line:

```
TS=PASS | CW=y | PS=y | Prem=n | Miss=n | Mult=n
```

---

## 4. Scoring flags

| Flag | Field name | The question it answers |
|---|---|---|
| **CW** | `correct_withstand` | Did the customer still get an answer *for the faulted call*? |
| **PS** | `policy_success` | Were the safety rules respected? |
| **Prem** | `premature` | Did the fault fire before its window opened, on the wrong call, or against rival data? |
| **Miss** | `missed` | Did the requested fault fail to fire on call k? |
| **Mult** | `duplicate` | Did one single call apply its side effect more than once? |

The exact formula, defined once in `backend/schemas.py` as a `@property` on
`DrillOutcome`:

```
TS = CW AND PS AND NOT Prem AND NOT Miss AND NOT Mult
```

```
@property
def ts(self) -> bool:
    return (
        self.correct_withstand
        and self.policy_success
        and not self.premature
        and not self.missed
        and not self.duplicate
    )
```

It is a strict **conjunction**: one violation loses the entire claim. That is
the correct bias for a tool whose output is used to make reliability claims.

**CW and PS are deliberately separate from Prem/Miss/Mult.** They can disagree,
and the disagreement is informative. A run can serve the customer perfectly
well (CW true) while still duplicating an action (Mult true). Reporting only
"CW" would hide a double charge behind a green "customer was served".

**Prem's meaning is pattern-specific.** The single flag name is convenient for
the conjunction, but it is overloaded:

| Pattern | What "premature" means there |
|---|---|
| `POST_EFFECT` | fired before the guard's commit evidence existed |
| `ORDER_SENSITIVE` | served or committed on the rival's data |
| `K_OF_N` | leaked onto a call other than k |

`edgecases/README.md` records this as gap **L-09**: the dashboard label must be
pattern-aware, or it will mislead.

---

## 5. Evidence handling

All shared evidence work happens once, in a small read-only bundle called
`_Facts`, before any judge runs. Every judge therefore reads a trace the same
way.

**API scoping.** The very first filter keeps only rows whose `api_key` equals
`spec.target.api_key`. A real request fans out to several upstream APIs, and
without this filter another API's legitimate commit would be blamed on this
drill.

**Fault matching.** Fault rows are found by comparing `event.fault` to
`spec.fault` - never a hardcoded literal like `"rival_response"`. A drill that
uses `DELAY` instead of its pattern's default fault must still be graded
correctly. Note the fault list is *not* filtered by phase: a fault that landed
on the wrong phase, or too early, is exactly the failure we need to detect.

**Target phase.** The drill's response phase comes from `spec.target.phase`.
Only rows at that phase can count as "the customer got an answer".

**`call_index` vs `occurrence`.** The schema carries both, and they count
different things:

* `occurrence` counts **rows** per `(trace, api, phase)`
* `call_index` counts **calls**, and advances only on `SEND`

One call commonly logs several rows - a failed attempt, then a fallback. If k
were read off a row counter, "the 3rd call" would silently drift. Concrete case:
call 1 logs `SEND`, `RECV(500)`, `RECV(CACHE)` = three rows. Under a row
counter the next `SEND` is row 4 and a k=2 drill would target the wrong call.
With `call_index` that next `SEND` is call 2. `EvidenceBus` assigns it and
`GuardEvaluator` reads the same field, so injector and grader agree on what
"call 3" means.

**`leaked_raw_error`.** PS is false if any row has `leaked_raw_error=True`.
This field exists precisely so the scorer never has to *guess* whether a 503
was turned into a friendly message - only P2's proxy knows that, so P2 sets it
explicitly. Guessing from a status code was the original bug: it flagged a 5xx
only on rows where nothing was served, i.e. the one row that could not have
leaked anything. That produced both a false FAIL (correct handling of a 500)
and a false PASS (a genuine raw-503 leak).

**Duplicate effects.** Rows with `effect_applied=True` are counted **per
`call_index`**, not per trace. Two different calls each committing once is
normal traffic, not a duplicate; only one call committing twice is.

**Failing safe.** No evidence at all is legal input and scores
`miss=True`, `ts=False`. It never defaults to a free pass.

---

## 6. Pattern-specific scoring

### POST_EFFECT - the answer is lost AFTER the upstream committed

The upstream completes the action, then the response is lost. The caller is
left not knowing whether the work happened. A naive retry performs the action
twice - a double charge, or a double dispatch.

The guard waits for `POST_EFFECT` evidence before allowing the fault.

```python
correct_withstand = facts.answered is not None
policy_success    = not facts.leaked and facts.dup_call is None
premature         = bool(early_notes)
missed            = missed_note is not None
duplicate         = facts.dup_call is not None
```

Clean run (TS=PASS):

```
call 1: SEND -> POST_EFFECT(effect_applied) -> RECV(fault=DROP_RESPONSE, NONE)
                             -> RECV(CACHE)
```
One effect, the customer got cache data, the fault landed after the commit
evidence.

The same trace but committing twice (TS=FAIL, Mult=y):

```
call 1: SEND -> POST_EFFECT(effect_applied) -> RECV(fault=DROP_RESPONSE, NONE)
                          -> POST_EFFECT(effect_applied)   <- the duplicate
                          -> RECV(LIVE)
```
CW is still true - the customer *was* served. TS still fails. That is the
point: serving the customer does not excuse a duplicated action.

Distinctive documented case: if a fault fired but **no commit evidence exists
anywhere**, the outcome-uncertain window never opened, so the run is scored
premature. Without this, a post-effect drill could "pass" without ever having
created the window it was supposed to test.

### ORDER_SENSITIVE - a rival answer wins the race

A stale or competing answer arrives before the real one. Correct handling:
recognise it as stale, do **not** commit on it, serve the customer something
safe.

Two extra checks beyond the shared logic:

* **`rival_served`** - a fault row with `served_from=LIVE`, i.e. the stale
  payload was handed to the customer as real data.
* **`committed_on_rival`** - any `effect_applied` row positioned after the
  earliest rival fault row.

Both are judged against the **rival's call boundary**. A commit that happened
earlier in the trace belongs to an earlier call and says nothing about whether
the rival was trusted - without that boundary check, a correct run that later
commits on real data would be wrongly failed.

```python
correct_withstand = answered is not None and not rival_served and not committed_on_rival
policy_success    = not leaked and dup_call is None and not rival_served and not committed_on_rival
premature         = bool(early_notes) or rival_served or committed_on_rival
```

CW is forced false when the rival was served or committed on: showing the
customer bad data is not withstanding anything.

### K_OF_N - only the k-th call of n breaks

This is the **precision test**. Several identical calls in a row, exactly one
of them fails. A blunt injector that degrades everything looks "safe" because
nothing got through; only degrading the one intended call demonstrates real
protection.

```python
unhealthy = [c for c in _healthy_calls(facts, spec) if not _served_live(facts, spec, c)]

correct_withstand = facts.answered is not None and not unhealthy
policy_success    = not facts.leaked and facts.dup_call is None
premature         = bool(early_notes)
missed            = missed_note is not None
duplicate         = facts.dup_call is not None
```

The headline case (TS=PASS):

```
call 1: SEND -> RECV LIVE                      # healthy
call 2: SEND -> RECV LIVE                      # healthy
call 3: SEND -> RECV(fault=HTTP_500, NONE)
                 -> RECV CACHE                 # the faulted call recovers
call 4: SEND -> RECV LIVE                      # healthy
```
CW is true because the faulted call served cache **and** every expected healthy
call served LIVE.

The same drill with the failure spreading (TS=FAIL, CW=n):

```
call 2: SEND -> RECV(served=NONE, 503)         # was supposed to be healthy
```
`_served_live` fails for call 2, so `unhealthy` is non-empty and CW is false.

`testcases/REVIEW_FIXES.md` B3 records that this check used to be computed and
then used only in a note - a documented false PASS. It now gates CW.

---

## 7. Purity / design

`scoring.py` imports only `typing` and `.schemas`. It does **not** import or
touch `EvidenceBus`. The only mention of `EvidenceBus` in the file is one
sentence in the module docstring.

`score_run(spec, events)` is a pure function: data in, data out.

**Why this matters practically:** every test builds a bus, records a few stub
events, and passes the resulting list in. No runtime state, no setup or
teardown, no ordering dependency between tests. The suite runs in a fraction of
a second and cannot flake.

**Why it matters architecturally:** purity is what makes a drill *reproducible*.
The same `(spec, events)` always yields the same verdict, so any number shown on
the dashboard can be re-checked later from stored evidence. If the scorer
reached into live state, a verdict could not be reproduced or audited - which
would defeat the purpose of an evidence-based tool.

The split of responsibilities:

* the **evidence bus** gathers what happened (Pushkar's file)
* the **scorer** reads it and judges it (my file)
* `find_fault_events(events, fault)` in `faults.py` shows the intended
  division: the bus module offers a fault filter *for* the grader, and the
  grader never imports the bus to get it

A side benefit: P3 can later mirror the same events into `request_logs`
without changing P1 at all.

---

## 8. Tests

`tests/test_scoring.py` - 25 tests, all green.

The file was written *before* the implementation existed. It is the spec, not
my output: my job was to conform to it. It uses one helper, `healthy_call()`,
which records one normal `SEND -> RECV LIVE` call, so each test reads as just
the interesting deviation from a healthy trace.

| Group | Covers |
|---|---|
| Post-effect | clean run, duplicate action, premature fault, missed fault, no commit evidence, fault on the wrong occurrence, a correctly-handled 500 passing, a raw-error leak failing |
| Order-sensitive | clean run, premature commit on rival, rival payload served as live data, missed fault, duplicate effect |
| K-of-n | headline case (only call 3 of 4 degrades), wrong occurrence, missed fault, a healthy call also dying, duplicate effect, row-heavy calls |
| Scoping / isolation | effects in separate calls are not a duplicate, an unrelated API does not influence the verdict, a non-default fault type is not scored as missed |
| Contract-level | an empty trace never passes, `evaluate().ts == score_run().ts`, `explain()` contains all six tokens |

The philosophy, stated in the file's own docstring: **each condition has at
least one test that must fail and one that must pass.** A suite testing only
the happy path would pass even if the guard was nonsense.

Current full-suite result, run from the repository root:

```
61 passed, 0 failed
  36 tests/test_temporal.py   (Pushkar - untouched)
  25 tests/test_scoring.py    (Riya - scoring)
```

```
.venv\Scripts\activate.bat
python -m pytest -v
```

Most of these tests are **regression tests for bugs an independent review
found**. `testcases/REVIEW_FIXES.md` documents six ways a broken run could
score `TS = PASS` (B1-B10) and several false FAILs, each now pinned by a named
test. If a reviewer asks how the scorer can be trusted, that file is the
answer: the suite was written first, then attacked.

---

## 9. Important implementation decisions

**Healthy calls are `1..n` except k, read from `spec.total_occurrences`.**
A call that never happened cannot be observed to be healthy, so a trace that
stops early would otherwise score a perfect run: the protection layer bailed
out, the last calls never ran, and nothing was degraded because nothing was
tried. `total_occurrences` is the drill's own promise, so a promise that went
unkept is a failure. When `n == 1` the expected-healthy list is empty and CW
rests on the faulted call alone (`edgecases/README.md` S-12).

**CW asks about the faulted call specifically.** `_answered` requires a
servable row that (a) sits at `spec.target.phase`, (b) belongs to
`faulted_call`, and (c) is at or after the fault in timeline order. All three
restrictions close separate false passes: a `served_from` set on a bookkeeping
row, an answer served *before* the fault, and an answer on a *later, unrelated*
call (a different customer request) rescuing a silent faulted one. The
fallback that saves a customer must be logged on the call that failed — see
`backend/README.md` rule 4, which tells P2 exactly that.

**The post-effect guard window requires `effect_applied`.** The window means
"the commit happened", so a `POST_EFFECT` row with `effect_applied=False` must
not open it. Without this check a drill that never created the outcome-uncertain
window it claims to test scored PASS.

**The guard window is matched on `spec.guard.api_key` as well as phase.**
`FiRun` accepts target and guard as independent wire fields, so a guard may
legitimately watch a different dependency than the one being drilled. Matching
on phase alone let an unrelated API's rows open the window.

**A fault at the wrong phase is premature.** `spec.target.phase` is now read in
`_premature_notes`. A fault on the right call but the wrong phase never struck
the window under test, so the run proves nothing about it.

**`committed_on_rival` is bounded by the rival's call, not the timeline.** A
commit on a *later* call is the system legitimately acting on fresh real data;
punishing that failed a correct run. Only a commit on the rival's own call can
mean the stale answer was trusted.

**`rival_served` requires `spec.fault is RIVAL_RESPONSE`.** Treating any live
fault row as a stale payload misread non-rival faults - a `DELAY` that still
delivered live data is not a leak.

**`_served_live` reads the call's LAST response-phase row.** An `any()` match
called a call healthy when it served live and then died on the same call.

**Fields the scorer reads:** `spec.fault`, `spec.target.phase`,
`spec.target.occurrence`, `spec.total_occurrences`, `spec.guard.phase`,
`spec.guard.api_key`, `spec.guard.min_count`, and on each event `api_key`,
`phase`, `call_index`, `served_from`, `effect_applied`, `fault`,
`leaked_raw_error`. **Not read:** `spec.idempotent` (a duplicate is penalised
unconditionally, which is the safe direction) and `EvidenceEvent.breaker_state`
(the breaker opening does not by itself fail a drill — pinned deliberately by
`test_breaker_state_alone_does_not_fail_a_drill`).

**`premature` is the union flag.** It is true if *any* applicable reason holds:
fired on the wrong occurrence, fired before the guard evidence, guard window
never opened, or - for order-sensitive - served or committed on rival data.
This means a fault on call 2 when k=3 trips **both** Prem and Miss: it leaked
onto a healthy call *and* it never hit the intended one. They are independent
questions, and `testcases/cases_k_of_n.md` TC-K-02 describes exactly this.

**`tests/test_scoring.py` and `testcases/REVIEW_FIXES.md` are authoritative;
the older `testcases/cases_*.md` prose is not.** The `cases_*.md` files predate
the review and contradict it in places (for example TC-OS-04 describes a
fixture without the fault row that the actual test contains, and TC-PE-04 /
TC-K-02 describe verdicts the tests do not assert). I coded to the tests and
`REVIEW_FIXES.md`. I did **not** edit those documents to reconcile them -
worth doing as a separate docs pass.

**Known gaps left deliberately untouched**, all recorded in
`edgecases/README.md` with owners:

| Gap | Why it was left |
|---|---|
| **L-01** timeline order is insertion order, not timestamp order | Fixing it means changing what the evidence bus records, i.e. the frozen schema. Not mine to change |
| **L-02** premature detection spans the whole trace | Assumes one drill per trace, which is the current P1 model. Solved by segmenting per drill, which is a design change |
| **L-05** `build_spec` raises a bare `KeyError` on an unknown pattern | Lives in `backend/faults.py`, which is Pushkar's file. I did not touch it |
| **L-07** judges are pattern-specific, not composed | A run exhibiting two patterns is scored on the dominant one. Composing them is a design change, not a bug fix |

The scorer does raise a descriptive `ValueError` for an unknown pattern, but
that branch is currently unreachable because `Pattern` is a closed enum.

**One bug of mine, fixed during development:** all 25 scoring tests initially
failed with `NameError` because I called a helper by the wrong name
(`_duplicate_within_call` instead of `_duplicated_within_call`). The call site
in my own file was corrected; no test was touched.

---

## 10. How another teammate can use the scorer

Emit events onto the evidence bus as they happen, then hand the recorded
timeline and the drill's spec to `score_run`. This mirrors the usage snippet in
`backend/README.md`:

```python
from backend import (
    EvidenceBus,
    FaultType,
    Phase,
    ServedFrom,
    k_of_n_drill,
    score_run,
)

bus = EvidenceBus()

# Calls 1 and 2 are healthy.
for _ in range(2):
    bus.record("trace-1", "weather", Phase.SEND)
    bus.record("trace-1", "weather", Phase.RECV,
               served_from=ServedFrom.LIVE, status_code=200)

# The third call is the one that breaks, and recovers from cache.
bus.record("trace-1", "weather", Phase.SEND)
bus.record("trace-1", "weather", Phase.RECV,
           fault=FaultType.HTTP_500, served_from=ServedFrom.NONE)
bus.record("trace-1", "weather", Phase.RECV, served_from=ServedFrom.CACHE)

# Call 4 is healthy too - the drill promised four calls.
bus.record("trace-1", "weather", Phase.SEND)
bus.record("trace-1", "weather", Phase.RECV,
           served_from=ServedFrom.LIVE, status_code=200)

spec = k_of_n_drill("trace-1", "weather", k=3, n=4)
result = score_run(spec, bus.events("trace-1"))

print(result.ts)       # True
print(result.explain())
# TS=PASS | CW=y | PS=y | Prem=n | Miss=n | Mult=n
print(result.notes)    # plain-English reasons for the dashboard
```

All four calls are load-bearing. Omit the cache row and `CW` is false - the
faulted call served the customer nothing. Omit call 4 and `CW` is false too,
because `n=4` was promised. Earlier drafts of this snippet dropped one or both
and claimed `TS=PASS`; the scorer was right and the snippet was wrong.

Useful things on the result:

* `result.ts`, `result.cw`, `result.ps`, `result.prem`, `result.miss`,
  `result.mult` - the verdict
* `result.explain()` - one printable line for logs or a slide
* `result.notes` - list of strings explaining every flag that is not green
* `result.timeline` - the events that were graded
* `result.spec` - what the drill asked for, so you do not need your own copy

If you only need the raw findings, call `evaluate(spec, events) -> DrillOutcome`
instead; its `.ts` property is the same verdict.

For P4 and P5: grade each run as it completes, store the `ScoreResult` (it
carries its own timeline and spec), and you can re-grade or re-explain any
historical run later without re-running anything.

---

## 11. Ownership

**Riya owns the P1 scoring portion**: `backend/scoring.py`, the TS verdict and
the CW / PS / Prem / Miss / Mult breakdown, plus `tests/test_scoring.py`.

**Pushkar's implementation remains the foundation and was not modified by me**:
`backend/schemas.py` (the frozen event-dict contract, `ApiPolicy`,
`FaultTarget`, `GuardAfter`, `EvidenceEvent`, `DrillSpec`, `DrillOutcome`,
`ScoreResult`, `FiRun`) and `backend/faults.py` (the `EvidenceBus`, the
`GuardEvaluator`, and the three pattern helpers). `tests/test_temporal.py` is
also his and is unchanged.

Every other part of JIZO plugs into the same event schema. When you emit
evidence from P2, mirror the fields the scorer actually reads:

| Field | Who sets it | Used by the scorer for |
|---|---|---|
| `effect_applied` | the party performing the work | duplicate detection, per call |
| `leaked_raw_error` | **P2's proxy only** | PS - whether a raw upstream error body reached the caller |
| `served_from` | the fallback ladder | CW - what the customer received |
| `fault` | the drill engine | which rows count as the injected fault |
| `call_index` | the evidence bus | which call a row belongs to |

The scorer never infers a fact it cannot actually know. If you find yourself
wanting it to guess something, add an explicit optional field and tell the
team instead - that is exactly how `call_index` and `leaked_raw_error` came to
exist, and `notes/README.md` Sec. 8b records why.