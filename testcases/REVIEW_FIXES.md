# Review Fixes - What Changed and Why

An independent review of Part 1 found **6 false-pass bugs** and several
correctness gaps in the scorer. All are fixed, each with a regression test.
The suite went from 19 to **32 tests**.

This file exists so the change is auditable rather than mysterious.

---

## The core problem

The original judges were too permissive in ways that all pointed the same
direction: **a broken run could score TS = PASS**. For a tool whose entire
purpose is producing trustworthy reliability claims, a false pass is worse
than a crash.

Two root causes:

1. Judges read the whole trace instead of scoping to the drilled API, so
   unrelated traffic could satisfy or break a condition.
2. Conditions were inferred (from status codes, from any row anywhere)
   rather than read from the spec and from explicit evidence.

---

## Fixed bugs

### B1 - Inverted raw-error check (false FAIL on correct behaviour)

**Was:** `served_from == NONE and status_code >= 500`. That flags a 5xx only
on rows where *nothing* was served - the one row that cannot have leaked
anything.

Two consequences, both confirmed by reproduction:
- Correct handling of a 500 (fallback served) scored **FAIL**.
- A genuine raw-503 leak scored **PASS**.

**Fix:** added an explicit `leaked_raw_error: bool` field to
`EvidenceEvent`. The scorer no longer guesses from a status code - only P2's
proxy knows whether it converted an error into a friendly message.

Tests: `test_correct_handling_of_500_passes`, `test_leaked_raw_error_fails_the_drill`.

### B2 - `CW` satisfied by any servable row anywhere (false PASS)

**Was:** `cw = bool(served)` scanned all events regardless of phase. A
`served_from=LIVE` accidentally set on a `POST_EFFECT` bookkeeping row
manufactured a pass even when the faulted call served nothing.

**Fix:** `_answered()` now requires `call_index >= faulted call` **and**
`phase is spec.target.phase`.

Test: `test_post_effect_detects_missing_commit_evidence` (via the CW note).

### B3 - k-of-n `CW` needed only one healthy live call (false PASS)

**Was:** `healthy_expected` was computed and then used only for a note.

**Fix:** `CW` requires **all** expected healthy calls to serve live data.

Test: `test_k_of_n_fails_when_a_healthy_call_also_dies`.

### B4 - Post-effect passed with no commit evidence (false PASS)

**Was:** a fault with no `effect_applied` anywhere only produced a note.
Meanwhile `_fired_before_window` returned `False` when the guard phase was
absent - so Prem was suppressed exactly when the guard had been ignored.

**Fix:** no commit + a fired fault is now treated as premature, with an
explicit note. The guard window not opening at all counts as firing early.

Test: `test_post_effect_fails_when_no_commit_was_evidenced`.

### B5 - Order-sensitive could not detect serving the rival payload (false PASS)

**Was:** only *committing* on rival data was penalised. Handing the stale
answer to the customer verbatim scored **PASS** with every flag green -
and that is the canonical bug for a race-condition drill.

**Fix:** a fault event served `LIVE` is now detected as serving stale data.

Test: `test_order_sensitive_detects_serving_the_rival_payload`.

### B6 - Post-effect ignored `spec.target.occurrence` (false PASS)

**Was:** only k-of-n looked at `k`. A spec targeting call 2, faulted on
call 1, scored as the intended drill.

**Fix:** all judges check `_wrong_occurrence()`.

Test: `test_post_effect_detects_fault_on_wrong_occurrence`.

### B7 - `Mult` counted the whole trace, not per call (false FAIL + cross-API)

**Was:** every `effect_applied` row in the trace was counted. Two distinct
calls each committing once - completely normal - scored `Mult`, and an
unrelated API's commit in the same trace was blamed on the drill.

**Fix:** `_duplicated_within_call()` counts effects per `call_index`, and
every judge filters to `spec.target.api_key` first.

Tests: `test_effects_in_separate_calls_are_not_a_duplicate`,
`test_unrelated_api_does_not_influence_verdict`,
`test_k_of_n_detects_duplicate_effect` (tightened to a true same-call duplicate).

### B8 - A correct order-sensitive run failed (false FAIL)

**Was:** *any* effect while a rival existed was premature, with no way to
tell which answer was committed on.

**Fix:** committed-on-rival is now checked against the rival's call
boundary, and serving stale data is judged separately.

Test: `test_order_sensitive_clean_run_scores_ts1` plus the corrected
duplicate fixture.

### B9 - Judges hardcoded fault type and phase

**Was:** `_score_order_sensitive` filtered `e.fault.value == "rival_response"`
and k-of-n filtered `e.phase == Phase.RECV`. A legal spec using `DELAY`
could never pass; a `TIMEOUT` recorded at SEND was scored as missed.

**Fix:** both read `spec.fault` and `spec.target.phase`.

Test: `test_drill_using_a_non_default_fault_is_not_scored_as_missed`.

### B10 - Occurrence numbering drifted with multi-row calls

**Was:** `k` was read off a per-`(api, phase)` **row** counter, but k means
the k-th **call**. A call logging attempt + fallback shifted every later
call number.

**Fix:** added `call_index` to `EvidenceEvent`, which advances only on
`SEND`. Judges and `GuardEvaluator` use it. Verified: a trace whose call 1
logs three rows still reports `recv call_indexs == [1, 1, 2, 2, 3]`.

Tests: `test_call_index_is_not_row_count`,
`test_k_of_n_survives_calls_that_log_two_rows`.

### B12 - `occurrence_count` mutated state on a read

**Was:** indexing a `defaultdict` inserted unseen keys on read.

**Fix:** `.get(key, 0)`.

---

## Contract changes (teammates must know)

These are additive and optional, so nothing that already works breaks.

| Change | Impact |
|---|---|
| `EvidenceEvent.call_index: int = 1` | **New field.** Bus assigns it. Judges use it. |
| `EvidenceEvent.leaked_raw_error: bool = False` | **New field.** P2's proxy must set it when passing an upstream error body through. |
| `DrillOutcome.ts` is now a property | Single definition of the conjunction; callers stop re-implementing it. |
| `ScoreResult.spec: Optional[DrillSpec]` | Consumers can re-score or explain intent without keeping their own copy. |
| `FiRun.to_spec()` | **Use this** instead of hand-building a `DrillSpec`. Also fixed drifted constraints (`total_occurrences` now `ge=1`, matching `DrillSpec`). |
| `EvidenceBus.call_events(trace, api, k)` | New helper: every row for one call. |
| `EvidenceBus.call_count(trace, api)` | New helper: how many calls have started. |
| `find_fault_events(events, fault=None)` | Now takes an optional fault filter. |

`ApiPolicy`, `Phase`, `FaultType`, `Pattern`, `ServedFrom`, `BreakerState`
are unchanged.

## Behaviour changes to be aware of

1. **`Mult` is per call, not per trace.** Two calls each committing once is
   not a duplicate. This is correct, and it *relaxes* two old verdicts.
2. **A post-effect fault with no commit now fails.** Deliberate: without the
   commit there is no outcome-uncertain window, so nothing was proven.
3. **Guards are judged against the in-flight call.** Log `SEND` first, then
   ask. `should_fire` judges the call currently in flight.
4. **The `premature` flag has a per-pattern meaning**, documented in
   `backend/scoring.py`. It is no longer overloaded across three meanings.

## Verification

| Probe | Expected | Result |
|---|---|---|
| Correct 500 handling | PASS | PASS |
| Raw 503 leak | FAIL | FAIL |
| No fallback, CW from effect row | FAIL | FAIL |
| Healthy call also dead (k-of-n) | FAIL | FAIL |
| Fault with no commit | FAIL | FAIL |
| Rival payload served | FAIL | FAIL |
| Fault on wrong occurrence | FAIL | FAIL |
| Two APIs, each committing once | PASS | PASS |
| Correct order-sensitive run | PASS | PASS |
| Row drift, k=2 | PASS | PASS |
| Headline k=3-of-4 | PASS | PASS |

**11/11 correct, 32/32 tests pass.**

## Known gaps still open

| ID | Gap | Owner |
|---|---|---|
| L-01 | Timeline order is insertion order, not timestamp order | Riya (P4) |
| L-02 | Premature detection spans the whole trace; assumes one drill per trace | Riya (P4) |
| L-03 | `backoff_delay_s` uses global `random`; exact-delay tests will flake | Aditi (P2) |
| L-04 | `backoff_max_s < backoff_initial_s` accepted silently | Aditi (P2) |
| L-05 | `build_spec` raises bare `KeyError` on unknown pattern | Riya (P4) |
| L-06 | No counters for guard decisions (cannot show "guard held 412 times") | Nikunj (P5) / Nameh (P6) |
| L-07 | Judges are pattern-specific, not composed | Riya (P4) |