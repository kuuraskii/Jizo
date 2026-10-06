# Notes - P1 Theory & Design Decisions

Background for the things that could not be explained in code comments:
where the formulas come from, why a design choice was made, and what will
bite the team later.

Read this before changing anything in `backend/`.

| File | What is in it |
|---|---|
| `README.md` | This file - theory, thresholds, design reasoning |
| `RIYA_P1_SCORING.md` | P1 scorer handover, co-owned by Pushkar + Riya |
| `PRD_P2_ACCEPTANCE_CONFLICT.md` | **Read before rehearsing the demo.** The PRD's P2 acceptance test ("3 forced 503s -> OPEN") cannot pass with the sourced volume threshold of 20. The breaker is right; the wording is wrong. |

---

## 1. Where the numbers come from

Every threshold in `ApiPolicy` is copied from published tuning, not
invented. This matters for Q&A - a judge asking "why 25%?" gets a citation,
not a shrug.

| Setting | Value | Source |
|---|---|---|
| Per-attempt timeout | 3.0 s | Demo default. Enterprise profile is ~1.8 s synchronous, ~6.5 s async (Pasunoori 2025 Sec. 7) |
| Max attempts | 3 | Standard retry bound |
| Backoff | 75 ms -> 1.8 s | Resilience-pattern profile; reported -52% recovery time |
| Jitter | uniform(0, 50 ms) | Jitter reported +83% stability |
| Retry budget | 2.2x baseline | Caps total extra load during an incident |
| Breaker window | 100 requests | Falahah 2021 Sec. 4.4 parameter set |
| Breaker error threshold | 25% | Same |
| Breaker min volume | 20 | Prevents tripping on thin samples |
| Sleep window | 10 s | Same |
| Half-open probes | 10 per 5 s | Same |

**Deadline-aware retries** matter: retries must fit inside the caller's
budget, otherwise we retry into a request that has already timed out.

## 2. Why the timeout hierarchy must be client > gateway > attempt

If the client gives up before the breaker trips, the breaker never learns
the dependency is broken. Order matters:

```
caller deadline  >  gateway deadline  >  per-attempt timeout  >  connect timeout
```

Each layer must be slower than the one inside it, so the innermost layer is
the one that reports failure first and gives the breaker evidence to act on.

## 3. Backoff formula

```
delay(attempt) = min(initial * 2**attempt + uniform(0, jitter), max_delay)
```

The `min(...)` cap is essential. Without it, attempt 10 waits
`0.075 * 1024 = 76.8 s`, long after the customer has given up. The jitter
term exists so that many clients recovering at once do not all retry in the
same millisecond (thundering herd / retry storm).

Implemented as `ApiPolicy.backoff_delay_s(attempt)`.

## 4. Idempotence is not safety

A common misconception worth correcting on stage: HTTP says PUT and DELETE
are idempotent, so retrying them "should be fine". Tan et al. note that
safety != idempotence - a PUT can still change server state in ways that
matter.

**51.0%** of 38,631 real operations across 8 public corpora are
POST/PUT/PATCH/DELETE, i.e. potentially state-changing (Tan 2026 Table I;
corpus range 47.4%-59.7%).

That is why `ApiPolicy.idempotent` exists as an explicit flag and why the
post-effect drill defaults to `idempotent=False`. A retry decision must be
per-operation, not per-endpoint.

## 5. The After-guard: why guards, not just faults

The core differentiator, adapted from Tan et al. 2026 (SequenceFI).

A conventional fault injector triggers *before* the side effect, so it can
only ever produce "the call failed and nothing happened". It cannot produce
the genuinely dangerous case: **the effect completed but the caller never
learned the outcome.**

Matching on phase alone is not enough. From Tan et al.:

| Matching strategy | Temporal success |
|---|---|
| Static-Req (match API only) | 0.0% |
| Static-Phase (API + request/response) | 55.6% overall |

Static-Phase reaches 100% on post-effect only because the phase happens to
coincide with the window; it drops to 66.7% on order-sensitive and 0.0% on
k-of-n.

So the guard needs **two** conditions, which is exactly what
`GuardEvaluator.should_fire()` checks:

1. The required prior evidence exists (`GuardAfter`) - the window is open.
2. We are on exactly occurrence k (`FaultTarget`) - the intended call only.

Drop either condition and the three Prem/Miss/Mult failure modes reappear.

## 6. The off-by-one that defines the guard

`should_fire()` judges the call **currently in flight**. So log the call's
`SEND` event first, then ask - which is exactly what a proxy does naturally:
it has sent the request and is now deciding whether this attempt gets the
fault.

Two bugs lived here, both caught by tests rather than reasoning:

1. The evaluator originally read the count *after* logging, always saw 0,
   and refused to fire at all. `TC-G-02` pins it.
2. It originally counted **rows** per `(api, phase)` rather than **calls**.
   Since a call commonly logs a failed attempt *and* a fallback, "the 3rd
   call" drifted. `EvidenceEvent.call_index` now advances only on `SEND`,
   and `EvidenceBus.call_count()` reads it. Verified: a trace whose first
   call emits three rows still reports `call_indexs == [1, 1, 2, 2, 3]`.

Before any `SEND` exists, the candidate is treated as call 1, so the guard
holds rather than firing on nothing.

## 7. Prem / Miss / Mult - why each one exists

These three flags exist because a fault-injection tool can fail in three
distinct ways, and all three produce a *plausible-looking but worthless*
number if ignored.

- **Prem** - the fault fired before the window opened. You measured a
  situation that never occurs in production.
- **Miss** - the fault never fired. You scored a healthy run and called it
  a pass. This is the most dangerous bug in chaos tooling: a green result
  that proves nothing.
- **Mult** - the side effect happened more than once. The protection
  "worked" for the customer while corrupting the system.

`TS = CW AND PS AND NOT Prem AND NOT Miss AND NOT Mult` is strict on purpose.
A conjunction means one violation loses the whole claim, which is the
correct bias for a tool whose output is used to make reliability claims.

## 8. Why CW and PS are separate from Prem/Miss/Mult

- **CW (Correct Withstand)** - did the customer still get an answer *for the
  faulted call*?
- **PS (Policy Success)** - were the safety rules respected?

They are separate because they can disagree, and the disagreement is
informative. A run can serve the customer (CW true) while still duplicating
an action (Mult true) - TC-PE-02. Reporting only CW would hide a double
charge behind a green "customer was served".

CW is deliberately narrow: it only counts response-phase rows at or after the
faulted call. Two mistakes it must never make:

- Accepting a `served_from` that was set on a `SEND` or `POST_EFFECT`
  bookkeeping row.
- Accepting a value served *before* the fault, which says nothing about
  whether the system withstood it.

## 8b. What an independent review changed

An adversarial review of Part 1 (deliberately hunting for false passes)
found **6 ways a broken run could score TS = PASS**, plus several false
FAILs. All fixed, each with a regression test; the suite went 19 -> 32.

A later round reviewed Part 2 and found **10 more** in `breaker.py` and
`logging_conf.py` - the worst being a breaker that closed itself having sent
**zero** requests, and a `redact()` that raised `RecursionError` into the
caller's `except` and masked the real failure. Full trail in
`testcases/REVIEW_FIXES.md`; the breaker rows in `edgecases/README.md`.

A third round used `pytest --cov --cov-branch` instead of reading, and found
two things review-by-reading had missed both times:

1. **`is_open` / `is_closed` / `is_half_open` all drove the state machine.**
   They read `effective_state`, which performs the OPEN -> HALF_OPEN step. The
   purity fix had left them behind, and the test written to guard purity only
   checked `state`, so it passed. A dashboard polling `is_half_open` was
   arming a probe budget on a dependency nobody called.
2. **A test that asserted something trivially true.**
   `test_one_failed_probe_reopens_the_breaker` never entered HALF_OPEN, so the
   reopen code never ran and the assertion held only because the breaker had
   never left OPEN. It is the exact failure this whole review process exists to
   catch, sitting inside the suite that was supposed to catch it.

The lesson worth keeping: both parts looked finished and both had a bug that
faked a passing result. A green suite is not the same as a correct one - and
counting tests, or reading them carefully, misses more than measuring them.

The two structural lessons, worth keeping:

1. **A scoring tool's worst bug is a false pass.** A crash is obvious; a
   green result that proves nothing is how a team ends up claiming
   resilience it never demonstrated. Hence `Miss` (no fault = no pass) is
   non-negotiable, and hence the strict conjunction.
2. **Never infer a fact the scorer cannot actually know.** Whether a 503 was
   converted into a friendly message is P2's business, so P2 sets
   `leaked_raw_error` explicitly instead of the scorer guessing from a
   status code. That guess was itself the inverted-condition bug.

Full detail in `testcases/REVIEW_FIXES.md`.

## 9. `_fired_before_window` - comparing positions, not flags

Premature detection compares **timeline indices**: is the earliest fault
event before the earliest guard-evidence event? It deliberately does not use
a boolean "effect_applied" flag, because the interesting case is a fault
landing in the same phase but earlier in the sequence.

## 10. Event sourcing, and why the bus is in-memory

Writing down every step and replaying it later is event sourcing. Benefits
for this project:

- The scorer is a **pure function** of `(spec, events)` - trivially testable,
  no database, no network.
- Debugging a surprising drill result means reading the timeline, not
  guessing.
- P3 can mirror the same events into `request_logs` without changing P1.

The bus is in-memory and per-trace on purpose. A global singleton would
leak evidence between concurrent requests - exactly what TC-B-02 guards
against. P3 adds persistence; P1 stays fast and dependency-free.

## 11. Decoupling choices, and what each teammate gets

| Decision | Reason |
|---|---|
| `backend/__init__.py` re-exports everything | Teammates import from one place and never depend on file layout |
| `scoring.py` never imports the bus | Scorer is pure -> unit-testable without any runtime state |
| New pattern = 1 dict entry + 1 builder | Adding a 4th drill touches 2 places, not the codebase |
| `ApiPolicy` is `frozen=True` | A policy cannot be mutated mid-flight by accident |
| Occurrence counters keyed by `(trace, api, phase)` | Generic fan-out to N APIs stays correct |
| **`call_index` counted separately from `occurrence`** | A call logging attempt + fallback emits several rows; if k were read off a row counter, "the 3rd call" would silently drift |
| **Every judge filters to `spec.target.api_key` first** | A real request fans out to several APIs; without scoping, an unrelated API's legitimate commit gets blamed on the drill |
| **Judges read `spec.fault` / `spec.target.phase`, never literals** | A drill using a non-default fault type can still be graded correctly |
| **`DrillOutcome.ts` is a property** | One definition of the conjunction; callers cannot re-implement and drift |
| **`leaked_raw_error` is an explicit flag** | Only P2's proxy knows whether a 503 was converted to a friendly message; the scorer must not infer it |
| No network in P1 | Suite runs in ~0.15 s and cannot flake on public APIs |

## 12. Known limitations to fix downstream

Honest list, so nobody is surprised:

1. **Timeline order is insertion order.** True concurrency could interleave
   events with sub-millisecond timestamps that recorded order does not
   reflect. `latency_ms` exists for when P4 needs finer ordering.
2. **`_fired_before_window` uses `min()` of both indices.** If a trace
   contains several drills, per-drill segmentation is needed. Fine for the
   one-run-per-trace model P1 assumes; revisit if P4 batches drills.
3. **No `ServedFrom.NONE` guard on partial states.** An empty `events` list
   scores `miss=True`, which is safe, but the notes could be clearer.
4. **Scorers are pattern-specific, not composed.** A real run may exhibit
   two patterns at once. Current design scores the dominant pattern only.
5. **`ApiPolicy.backoff_delay_s` uses `random`.** Tests that assert exact
   delays must seed or assert a range. P2 should inject the RNG for
   deterministic tests.
6. **`GuardEvaluator.should_fire` is stateless.** Calling it twice with no
   new evidence returns "fire" both times. A caller that polls, or asks once
   per phase, will double-inject. Needs a "window consumed" flag. (P2)
7. **`premature` is pattern-specific**, not one global meaning:
   post-effect = fired before the guard's evidence existed; order-sensitive =
   served or committed on rival data; k-of-n = leaked onto a healthy call.
   The single flag name is convenient for the TS conjunction, but the
   dashboard label must be pattern-aware or it will mislead. (P5)

## 13. Citation map

| Claim | Source |
|---|---|
| 51.0% of operations are state-changing (19,701 / 38,631) | Tan et al. 2026, Table I |
| Static-Req 0.0% / Static-Phase 55.6% temporal success | Tan et al. 2026, Tables IV-V |
| Guard intuition, Eq. 1-6 | Tan et al. 2026 Sec. III-IV |
| Breaker states + 5 parameters | Falahah et al. 2021, Sec. 4.4 |
| Proxy-side breaker has "no real example" | Falahah et al. 2021, Sec. 4.1 |
| 1 failing service -> 7.3 more within 90 s, 68% unavailability | Luo & Girard 2026, Sec. 4.3 |
| Control-vs-experiment method | Basiri et al. 2016/17 (Netflix ChAP) |
| Backoff/jitter tuning, gateway ROI | Pasunoori 2025, Sec. 2 + Table 2 |

Always cite rather than round up. Directional published numbers are not
claims about JIZO.