# Test Cases - P1 Temporal Drill Engine

Plain-language record of every test in `tests/test_temporal.py`, so a
reviewer can check the logic without reading code.

Run them with:

```bash
.\.venv\Scripts\python.exe -m pytest tests/test_temporal.py -v
```

Per-file breakdown:
* `cases_post_effect.md` - pattern 1 (4 cases)
* `cases_order_sensitive.md` - pattern 2 (4 cases)
* `cases_k_of_n.md` - pattern 3 (4 cases) + guard and bus mechanics
* `REVIEW_FIXES.md` - what the code review found and why each fix matters

Current status: **180 passed**.

## Why these tests exist

A fault-injection tool is only trustworthy if it can prove two opposite
things at once:

1. It **does** fire the fault when the dangerous window is genuinely open.
2. It **does not** fire early, or on healthy calls, or twice.

So for each of the 3 patterns we test the failure-detection direction
(Prem / Miss / Mult) *and* the clean direction (TS = 1). A suite that only
tests the happy path would pass even if the guard was nonsense.

---

## Group 1 - Post-effect pattern (answer lost after commit)

The dangerous one. The upstream already did the work (e.g. charged the
card), then the answer never arrives. A naive retry does the work twice.

| # | Test name | What it sets up | Expected | Why it matters |
|---|---|---|---|---|
| 1 | `test_post_effect_clean_run_scores_ts1` | SEND, POST_EFFECT, fault DROP_RESPONSE on RECV, then CACHE response | `ts=True`, `mult=False` | The target behaviour: work done once, customer still served |
| 2 | `test_post_effect_detects_duplicate_action` | Two POST_EFFECT events after the dropped response | `mult=True`, `ts=False` | Catches the double-charge / double-dispatch bug |
| 3 | `test_post_effect_detects_premature_fault` | Fault fires at PRE_EFFECT (before commit) | `prem=True`, `ts=False` | An early fault proves nothing about the real window |
| 4 | `test_post_effect_detects_missed_fault` | Completely healthy trace, no fault | `miss=True`, `ts=False` | Prevents claiming a pass from a drill that never ran |

## Group 2 - Order-sensitive pattern (rival response wins the race)

A stale or competing answer arrives before the real one. The system must
not commit on the rival data.

| # | Test name | What it sets up | Expected | Why it matters |
|---|---|---|---|---|
| 5 | `test_order_sensitive_clean_run_scores_ts1` | Rival response, **no** commit, MESSAGE served | `prem=False`, `ts=True` | Correct handling: discard stale answer, tell the user |
| 6 | `test_order_sensitive_detects_premature_commit` | Rival response, then a commit happens | `prem=True`, `ts=False` | This is the exact bug the pattern exists to catch |
| 7 | `test_order_sensitive_detects_missed_fault` | No rival response at all | `miss=True`, `ts=False` | The race was never exercised |
| 8 | `test_order_sensitive_detects_duplicate_effect` | Two POST_EFFECT events | `mult=True`, `ts=False` | Duplicated action under a race |

## Group 3 - k-of-n pattern (only call #k breaks)

Proves the protection is *precise* rather than blunt.

| # | Test name | What it sets up | Expected | Why it matters |
|---|---|---|---|---|
| 9 | `test_k_of_n_headline_case_only_third_call_degrades` | 4 calls; only call 3 gets HTTP_500 then CACHE | `ts=True`, and `fault_occurrences == [3]` | **The demo case.** Asserts the exact occurrence, not just "a fault happened" |
| 10 | `test_k_of_n_detects_fault_on_wrong_occurrence` | Fault on call 2 instead of 3 | `prem=True`, `ts=False` | A blunt injector would fail this |
| 11 | `test_k_of_n_detects_missed_fault` | All 4 calls healthy | `miss=True`, `ts=False` | Nothing was proven |
| 12 | `test_k_of_n_detects_duplicate_effect` | Effect applied twice during a partial failure | `mult=True`, `ts=False` | Partial failure must not duplicate work |

## Group 4 - Guard mechanics (the precision mechanism)

| # | Test name | What it sets up | Expected | Why it matters |
|---|---|---|---|---|
| 13 | `test_guard_holds_until_after_evidence_exists` | Ask before and after the POST_EFFECT event | `False` then `True`, reason mentions "waiting for guard evidence" | The After-guard actually gates |
| 14 | `test_guard_fires_only_on_exact_k` | 4 calls with k=3 | `fired_at == [3]` | Earlier calls untouched, later calls refused |
| 15 | `test_guard_refuses_after_target_window_passed` | k=1 but already 2 calls done | `should_fire=False`, reason contains "already passed" | No stray faults on healthy calls |

## Group 6 - Regression tests from the code review

Added after an adversarial review found false passes in the scorer. Each
one fails loudly if the old bug ever returns.

| # | Test name | What it sets up | Expected | Why it matters |
|---|---|---|---|---|
| 20 | `test_post_effect_fails_when_no_commit_was_evidenced` | Fault fired, cache served, **no POST_EFFECT at all** | `ts=False` | The drill never created its outcome-uncertain window |
| 21 | `test_post_effect_detects_fault_on_wrong_occurrence` | Spec targets call 2, fault on call 1 | `prem=True`, `ts=False` | Judges must honour `spec.target` |
| 22 | `test_correct_handling_of_500_passes` | 500 handled with a cache fallback | `ts=True` | Punishing correct behaviour is also a scoring bug |
| 23 | `test_leaked_raw_error_fails_the_drill` | Raw 503 body passed to the caller | `ps=False`, `ts=False` | Policy must not leak upstream errors |
| 24 | `test_k_of_n_fails_when_a_healthy_call_also_dies` | Fault on call 3, but call 2 is dead too | `cw=False`, `ts=False` | "Protection" that breaks everything is not a pass |
| 25 | `test_order_sensitive_detects_serving_the_rival_payload` | Rival response served `LIVE` | `prem=True`, `ts=False` | The canonical race bug was invisible |
| 26 | `test_effects_in_separate_calls_are_not_a_duplicate` | Two calls each commit once | `mult=False`, `ts=True` | Mult is per-call, not per-trace |
| 27 | `test_unrelated_api_does_not_influence_verdict` | A second API commits in the same trace | `ts=True` | No cross-API contamination |
| 28 | `test_call_index_is_not_row_count` | Call 1 logs 3 rows | `call_indexs == [1,1,2]` | k must mean call number, not row number |
| 29 | `test_k_of_n_survives_calls_that_log_two_rows` | Row-heavy calls, k=2 | `ts=True` | Row drift must not shift the target |
| 30 | `test_drill_using_a_non_default_fault_is_not_scored_as_missed` | Order-sensitive drill with `DELAY` | `miss=False`, `ts=True` | Judges read `spec.fault`, not a literal |
| 31 | `test_ts_lives_on_the_outcome_too` | Any run | `outcome.ts == score_run().ts` | One definition of TS |
| 32 | `test_fi_run_converts_to_a_matching_spec` | `FiRun` wire shape | Converts without drift | Callers must not hand-roll this |

See `testcases/REVIEW_FIXES.md` for the full before/after of each bug.

## Group 5 - Evidence bus mechanics (bookkeeping correctness)

| # | Test name | What it sets up | Expected | Why it matters |
|---|---|---|---|---|
| 16 | `test_occurrence_counter_is_per_api_and_phase` | 2 sends on `alpha`, 1 on `beta` | 2 / 1 / 0 | One API's traffic must never inflate another's k |
| 17 | `test_bus_keeps_traces_separate` | Two concurrent traces | 1 event each, both traces known | Concurrent requests must not see each other's evidence |
| 18 | `test_guard_after_rejects_zero_min_count` | `min_count=0` | raises `ValueError` | A guard demanding zero evidence is meaningless |
| 19 | `test_pattern_specs_pair_target_and_guard_correctly` | Build all 3 specs | guards match intent, all 3 patterns covered | Helpers must not be mis-paired |

---

## Coverage map against the scoring rule

`TS = CW AND PS AND NOT Prem AND NOT Miss AND NOT Mult`

| Condition | Negative test | Positive test |
|---|---|---|
| `Prem` (fault too early) | #3, #6, #10 | #13, #15 (guard refuses) |
| `Miss` (fault never fired) | #4, #7, #11 | #1, #5, #9 |
| `Mult` (duplicate action) | #2, #8, #12 | #1, #5, #9 |
| `CW` (customer served) | implied in #2 | #1, #5, #9 |
| `PS` (policy held) | implied in #2, #8 | #1, #5, #9 |

Each condition has at least one test that must fail and one that must pass.

## Manual spot-check

```bash
.\.venv\Scripts\python.exe -c "from backend import *; \
b=EvidenceBus(); s=k_of_n_drill('t','weather',k=3,n=4); \
[b.record('t','weather',Phase.SEND) for _ in range(2)]; \
[healthy for healthy in [b.record('t','weather',Phase.RECV,served_from=ServedFrom.LIVE)]]; \
print(score_run(s, b.events('t')).explain())"
```

Simpler: run the suite. It is faster than reading this file.