# Edge Cases - P1 Temporal Drill Engine

Every edge case JIZO already handles, and — more usefully — the ones it
deliberately does **not** handle yet.

Purpose: a reviewer should be able to attack this code with any input here
and know the expected answer before running it. A case listed as *Not
handled* is not a bug being hidden; it is a known gap with a named owner
for P2-P4.

---

## 1. Guard / occurrence counting

| # | Edge case | Expected behaviour | Covered by | Status |
|---|---|---|---|---|
| G-01 | Guard asked before any evidence exists | Refuse, reason "waiting for guard evidence" | TC-G-01 | Handled |
| G-02 | Guard asked after the window opened | Fire on the exact occurrence | TC-G-01 | Handled |
| G-03 | We are **past** the target occurrence | Refuse, reason "already passed" — a late fire would hit a healthy call | TC-G-03 | Handled |
| G-04 | We are **before** the target occurrence | Hold, reason "occurrence < target k" | TC-G-02 | Handled |
| G-05 | `k=1` and no calls made yet | `seen+1 = 1`, so fires on the very first call once the guard is satisfied | TC-G-01 | Handled |
| G-06 | `min_count=0` on a guard | Reject at construction with `ValueError` | TC-B-03 | Handled |
| G-07 | `occurrence=0` on a target | Reject (`ge=1`) — occurrences are 1-based | `FaultTarget` validator | Handled |
| G-08 | Blank `api_key` | Reject at construction | `FaultTarget` validator | Handled |
| G-09 | Two APIs in flight, same phase | Counters stay independent; one API's traffic cannot inflate the other's k | TC-B-01 | Handled |
| G-10 | Guard phase never occurs at all | Guard never satisfied, fault never fires, `miss=True` on scoring | implied | Handled (fails safe) |
| G-11 | Multiple drills sharing one trace | Premature detection spans the whole trace, so a fault from drill A could mark drill B premature | — | **Not handled** — see L-02 |
| G-12 | Guard asked when no `SEND` has been logged yet | Treats call 1 as the candidate, so the guard holds rather than firing on nothing | — | Handled |
| G-13 | Guard asked twice for the same in-flight call | Both calls see the same call number; a polling caller can double-inject | — | **Not handled** — see L-08 |
| G-14 | `should_fire` called after `SEND` | Judges the in-flight call, which is the order P2's proxy naturally uses | TC-G-01 | Handled |

## 2. Trace isolation

| # | Edge case | Expected behaviour | Covered by | Status |
|---|---|---|---|---|
| T-01 | Two concurrent traces | Fully isolated; each sees only its own events | TC-B-02 | Handled |
| T-02 | Query a traceId never seen | Empty list, no exception | — | Handled |
| T-03 | `occurrence_count` on an unseen triple | Returns 0 (defaultdict) | TC-B-01 | Handled |
| T-04 | `reset()` one trace | Only that trace's events and counters cleared | — | Handled |
| T-05 | `reset()` with no argument | Whole notebook cleared (used between tests) | — | Handled |
| T-06 | Bus shared as a global singleton | Would leak evidence across requests | prevented by design (per-trace instance) | Handled |
| T-07 | Same trace reused after `reset()` | Counters restart at 1 | — | Handled |

## 3. Scoring verdicts

| # | Edge case | Expected behaviour | Covered by | Status |
|---|---|---|---|---|
| S-01 | Empty event list | `miss=True`, `ts=False` — never a free pass | — | Handled (fails safe) |
| S-02 | Fault present, no effect recorded | Treated as **premature** — the injector ignored its own guard, so the run proves nothing | `test_post_effect_fails_when_no_commit_was_evidenced` | Handled (was a false PASS) |
| S-03 | One call applies its side effect twice | `mult=True`, `ts=False` even though the customer was served | TC-PE-02 | Handled |
| S-03b | Two *different* calls each commit once | **Not** a duplicate — normal traffic, `mult=False` | `test_effects_in_separate_calls_are_not_a_duplicate` | Handled (was a false FAIL) |
| S-03c | An unrelated API commits in the same trace | Scoped out — no cross-API contamination | `test_unrelated_api_does_not_influence_verdict` | Handled (was a false FAIL) |
| S-04 | Raw upstream 5xx reaches the caller | `ps=False` (policy leak), `ts=False`, via the explicit `leaked_raw_error` flag | `test_leaked_raw_error_fails_the_drill` | Handled (was inverted) |
| S-04b | A 500 that we converted into a friendly message | `ts=True` — must not be punished | `test_correct_handling_of_500_passes` | Handled (was a false FAIL) |
| S-05 | Fault type differs from the pattern default | Judge reads `spec.fault`; an HTTP_500 on an order-sensitive drill is not automatically "missed" | `test_drill_using_a_non_default_fault_is_not_scored_as_missed` | Handled |
| S-06 | Rival response **and** a commit | `prem=True` **and** `cw=False` — served from bad data is not "withstanding" | TC-OS-02 | Handled |
| S-07 | Duplicate effect *and* no fault | Both `mult=True` and `miss=True`, failing independently | TC-OS-04 | Handled |
| S-08 | Fault on a healthy occurrence | `prem=True` (`fired_on` contains something other than k) | TC-K-02 | Handled |
| S-09 | Target occurrence never faults | `miss=True` | TC-K-03 | Handled |
| S-10 | A healthy call also degrades | **Fails** — ALL expected healthy calls must serve LIVE, not just one | `test_k_of_n_fails_when_a_healthy_call_also_dies` | Handled (was a false PASS) |
| S-10b | `served_from=LIVE` accidentally set on a SEND/POST_EFFECT row | Cannot satisfy CW — only response-phase rows count | — | Handled (was a false PASS) |
| S-13 | Fault served `LIVE` (the rival payload handed to the caller) | `prem=True`, `ts=False` — the canonical race bug | `test_order_sensitive_detects_serving_the_rival_payload` | Handled (was a false PASS) |
| S-14 | Spec targets call k, fault lands on another call | `prem=True` for every pattern, not just k-of-n | `test_post_effect_detects_fault_on_wrong_occurrence` | Handled (was a false PASS) |
| S-15 | Drill uses a fault other than the pattern default | Read from `spec.fault`; never scored as "missed" for using it | `test_drill_using_a_non_default_fault_is_not_scored_as_missed` | Handled (was a false FAIL) |
| S-11 | `total_occurrences=1` (single-call drill) | Allowed (was `ge=2`, relaxed so post-effect drills can be single-call) | TC-PE-01..04 | Handled |
| S-12 | `n=1` with k-of-n pattern | `healthy_expected = 0`, so `cw=True` from the fault path alone | — | Handled |

## 4. Validation

| # | Edge case | Expected behaviour | Status |
|---|---|---|---|
| V-01 | `min_count < 1` on `GuardAfter` | `ValueError` | Handled |
| V-02 | `occurrence < 1` on `FaultTarget` | `ValueError` | Handled |
| V-03 | Whitespace-only `api_key` | `ValueError` | Handled |
| V-04 | `total_occurrences < 1` on `DrillSpec` | `ValueError` | Handled |
| V-05 | `timeout_s <= 0` or `max_attempts < 1` | `ValueError` | Handled |
| V-06 | `breaker_error_threshold` outside 0-1 | `ValueError` | Handled |
| V-07 | Mutating an `ApiPolicy` after creation | Blocked — `frozen=True` | Handled |
| V-08 | `jitter_s < 0` | `ValueError` | Handled |
| V-09 | `backoff_max_s < backoff_initial_s` | **Accepted silently** — every delay clamps to max | **Not handled** — see L-04 |
| V-10 | Unknown pattern string from an API request | Pydantic rejects with a clear enum error | Handled |
| V-11 | Unknown fault type string | Rejected as above | Handled |
| V-12 | Negative `latency_ms` | `ValueError` | Handled |

## 5. Pattern helpers

| # | Edge case | Expected behaviour | Status |
|---|---|---|---|
| H-01 | Guard/target mispairing by hand | Impossible via helpers — they always pair correctly | Handled |
| H-02 | `k > n` (target beyond the trace length) | Fault never fires, `miss=True` | Handled (fails safe) |
| H-03 | `build_spec()` with no fault/idempotency given | Pattern defaults applied | Handled |
| H-04 | Unknown pattern passed to `build_spec` | `KeyError` | **Not handled** — see L-05 |
| H-05 | All 3 patterns covered by helpers | Asserted in the suite | Handled |

## 6. Backoff maths (`ApiPolicy`)

| # | Edge case | Expected behaviour | Status |
|---|---|---|---|
| B-01 | `attempt=0` | `min(0.075 + jitter, 1.8)` ≈ 75-125 ms | Handled |
| B-05 | One call logs attempt + fallback rows | `call_index` counts it once; `occurrence` still counts rows | `test_call_index_is_not_row_count` | Handled |
| B-02 | Large attempt number | Clamped to `backoff_max_s` (1.8 s) | Handled |
| B-03 | `jitter_s=0` | Deterministic exponential | Handled |
| B-04 | Non-deterministic jitter | Uses `random`, so exact-delay assertions need seeding | **Not handled** — see L-03 |

---

## Known gaps, with owners

| ID | Gap | Impact | Owner |
|---|---|---|---|
| L-01 | Timeline order is insertion order, not timestamp order | Sub-millisecond races may order wrongly once P4 adds true concurrency | Riya (P4) |
| L-02 | Premature detection uses `min()` across the whole timeline | Only correct for one drill per trace; breaks if drills are batched into a trace | Riya (P4) |
| L-03 | `backoff_delay_s` uses the global `random` | Tests asserting exact delays will flake | Aditi (P2) — inject the RNG |
| L-04 | `backoff_max_s < backoff_initial_s` accepted silently | Every delay clamps to max; likely a config typo | Aditi (P2) — add a validator |
| L-05 | `build_spec` raises bare `KeyError` on an unknown pattern | Leaks an unhelpful 500 instead of a 422 | Riya (P4) |
| L-06 | No metrics/counters for guard decisions | Cannot show "guard held 412 times" on the dashboard | Nikunj (P5) + Nameh (P6) |
| L-07 | Scorers are pattern-specific, not composed | A run exhibiting two patterns is scored on the dominant one only | Riya (P4) |
| L-08 | `GuardEvaluator.should_fire` is stateless | A polling or per-phase caller can be told "fire" twice for one call, double-injecting | Aditi (P2) — needs a "window consumed" flag |
| L-09 | `premature` means something slightly different per pattern | Documented in `scoring.py`, but the dashboard label must be pattern-aware or it will mislead | Nikunj (P5) |

## Design invariants worth preserving

These are the rules future changes must not break:

1. **The scorer must stay pure.** `score_run(spec, events)` takes data and
   returns data. It must never import or touch `EvidenceBus`.
2. **Failing safe beats failing open.** An uneventful trace scores `miss=True`
   and `ts=False`. Never relax this to make a demo look greener.
3. **Guards gate on evidence, never on wall-clock.** Time-based guards were
   rejected deliberately: they are flaky and unreproducible on stage.
4. **A guard must never widen its own condition.** `min_count` only ever
   tightens. Otherwise a fault can grant itself permission.
5. **Adding a pattern costs 2 edits** — one judge in `scoring.py`, one
   builder in `faults.py`. If a change needs more, the design has drifted.