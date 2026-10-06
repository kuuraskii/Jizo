# Test Case Records - Circuit Breaker (Part 2)

Owner: Pushkar. Implementation: `backend/breaker.py`. Tests: `tests/test_breaker.py`.
**56 tests, all passing.**

Every threshold is read from `ApiPolicy`, so the suite pins the sourced tuning
as well as the behaviour.

---

## Why the breaker exists

One dependency fails -> every caller waits on it. A breaker stops calling that
dependency and serves the fallback instead, which contains the blast radius.

Published figures this design rests on:
- one failing service cascades to avg 7.3 more within 90 s, and causes complete
  unavailability in 68% of tests, without a breaker [Luo & Girard 2026 Sec. 4.3]
- aggressive retries during an outage add **+38% recovery time** [Luo & Girard
  Sec. 4.4] - this is why the storm guard exists
- half-open probing at 10 req / 5 s gives **94.7% recovery-detection accuracy**
  [Pasunoori 2025 Sec. 2]

## Sourced thresholds

| Setting | Value | Source |
|---|---|---|
| Sliding window | 100 requests | Falahah 2021 Sec. 3 parameter set |
| Error threshold | 25% | 20-30% best sensitivity/stability band [Pasunoori 2025 Sec. 2] |
| Volume threshold | 20 calls | Falahah 2021 Sec. 3; stops false trips on thin samples |
| Sleep window | 10 s | Demo setting; prod uses 1->32 s exp backoff |
| Half-open probes | 10 per 5 s | 94.7% accuracy [Pasunoori 2025 Sec. 2] |
| Bulkhead pool | 20 per dependency | Bulkhead + breaker gives finer containment [Luo & Girard Abstract] |

---

## Group 1 - Threshold wiring (2 tests)

| # | Test | Proves |
|---|---|---|
| 1 | `test_policy_carries_the_sourced_tuning` | The six numbers above are actually in `ApiPolicy` |
| 2 | `test_breaker_uses_the_policy_it_was_given` | The breaker reads its config from the policy, not hardcoded values |

## Group 2 - CLOSED -> OPEN (7 tests)

| # | Test | Setup | Expected | Why it matters |
|---|---|---|---|---|
| 3 | `test_starts_closed` | fresh breaker | `CLOSED`, `allow_call()=True` | Requests flow normally |
| 4 | `test_does_not_trip_below_the_volume_threshold` | 19 failures (one short of 20) | stays `CLOSED` even at 100% error pct | **The false-positive case.** Without the volume gate, one unlucky failure in a small window opens the breaker |
| 5 | `test_trips_once_volume_and_threshold_are_met` | 20 failures | `OPEN` | The gate is a floor, not a ceiling |
| 6 | `test_trips_at_the_threshold_not_only_above_it` | 15 ok + 5 fail = exactly 25% | `OPEN` | Uses `>=`, so the boundary is inclusive |
| 7 | `test_stays_closed_below_the_threshold` | 16 ok + 4 fail = 20% | `CLOSED` | 20% sits inside the safe 20-30% band |
| 8 | `test_mixed_window_evicts_the_oldest_samples` | 5 fails then 100 successes | `error_pct()==0` | Sliding window really slides; an old burst cannot hold the breaker open forever |
| 9 | `test_window_never_exceeds_the_configured_size` | 250 outcomes | `window_size() <= 100` | The deque cap holds |

## Group 3 - The retry-storm guard (4 tests)

| # | Test | Setup | Expected | Why it matters |
|---|---|---|---|---|
| 10 | `test_open_breaker_refuses_calls` | tripped | `allow_call()=False` | Fast-fail instead of queueing behind a dead dependency |
| 11 | `test_no_retries_while_open` | tripped | `should_retry()=False` | This is the +38% recovery-time failure mode |
| 12 | `test_retries_allowed_while_closed` | healthy | `should_retry()=True` | Guards against over-suppression |
| 13 | `test_no_retries_while_half_open` | tripped, sleep elapsed | `should_retry()=False` | HALF_OPEN is *less* certain than CLOSED, not more |

## Group 4 - OPEN -> HALF_OPEN -> CLOSED (9 tests)

| # | Test | Setup | Expected |
|---|---|---|---|
| 14 | `test_stays_open_until_the_sleep_window_elapses` | advance 9.9 s | still `OPEN` |
| 15 | `test_moves_to_half_open_after_the_sleep_window` | advance 10.1 s | `HALF_OPEN` |
| 16 | `test_half_open_probe_budget_is_genuinely_limited` | 12 attempts in HALF_OPEN | **exactly 10 allowed** |
| 17 | `test_probe_budget_refreshes_after_its_window` | exhaust, then advance 5.1 s | allowed again |
| 18 | `test_enough_successful_probes_close_the_breaker` | 10 probe successes | `CLOSED` |
| 19 | `test_one_failed_probe_reopens_the_breaker` | 1 success then 1 failure | `OPEN` |
| 20 | `test_closing_clears_the_error_window` | after recovery | `error_pct()==0`, `window_size()==0` |
| 21 | `test_recovery_without_probes_stays_half_open` | advance 60 s, no probes | still `HALF_OPEN` |
| 22 | `test_transitions_are_recorded_for_the_timeline` | full cycle | all 3 transitions logged for P3/P5 |

**Test 16 is a regression test for a real bug I shipped and caught in review.**
The budget was *checked* but never *consumed*, so all 12 attempts were allowed
against a budget of 10. "Limited probes" was not limited. `allow_call()` now
reserves the slot.

**Test 13 also fixed a real behaviour gap.** `should_retry()` originally
returned `not is_open`, which meant `True` during HALF_OPEN. Retrying while
we are testing recovery defeats the probe budget, so the guard is now
`state is CLOSED`.

## Group 5 - Transition bookkeeping (1 test)

| # | Test | Setup | Expected |
|---|---|---|---|
| 23 | `test_repeated_open_does_not_duplicate_transition_rows` | 40 failures while already OPEN | exactly 1 `OPEN` transition logged |

## Group 6 - Bulkhead / load containment (8 tests)

| # | Test | Setup | Expected | Why it matters |
|---|---|---|---|---|
| 24 | `test_bulkhead_allows_up_to_its_limit` | pool of 3 | 3 acquires succeed | Normal operation |
| 25 | `test_bulkhead_refuses_rather_than_queueing_when_full` | pool of 2, 3 acquires | 3rd is `False` | Queueing a call to a saturated dependency turns a slowdown into a timeout |
| 26 | `test_released_slot_can_be_reused` | release then acquire | succeeds | No leak of slots |
| 27 | `test_release_never_goes_negative` | 2 extra releases | `in_flight==0` | Defensive against misuse |
| 28 | `test_bulkhead_rejects_zero_concurrency` | `max_concurrency=0` | raises `ValueError` | A pool that admits nothing is a config error, caught at startup |
| 29 | `test_bulkhead_pool_is_per_breaker_not_shared` | saturate weather | geocode still admits | Containment is per-dependency |
| 30 | `test_bulkhead_snapshot_has_the_hystrix_pool_gauges` | 1 in flight | max/in_flight/available/rejected | P5's thread-pool row |
| 31 | `test_no_slot_is_taken_while_open` | tripped, then acquire | `False`, `in_flight==0` | Storm guard beats the pool |
| 32 | `test_bulkhead_counts_rejections` | 2 refusals | `rejected==2` | Dashboard shows the rejection count |

## Group 7 - Snapshot + registry (5 tests)

| # | Test | Proves |
|---|---|---|
| 33 | `test_snapshot_is_plain_data_for_the_dashboard` | P5 reads plain dicts, never breaker internals |
| 34 | `test_snapshot_counts_opens` | backs the "Open Breakers Count" panel |
| 35 | `test_registry_returns_one_breaker_per_api_key` | same key reuses; different key isolated |
| 36 | `test_registry_reports_per_api_state` | `GET /breaker/state` shape |
| 37 | `test_registry_reset_drops_every_breaker` | test isolation |

## Group 8 - Concurrency (1 test)

| # | Test | Setup | Expected | Why it matters |
|---|---|---|---|---|
| 38 | `test_concurrent_failures_do_not_deadlock_or_lose_records` | 4 threads x 25 failures | `OPEN`, `window_size()==100` | **Regression for a deadlock I introduced.** `record_failure` held a non-reentrant `Lock` and then read `self.state`, which re-acquired it. First smoke test hung for 120 s. Now `RLock`. |

## Group 9 - A probe must be a real probe (2 tests)

Added after the first adversarial review. Before this, the breaker would close
itself having sent **zero** requests - so "it recovers on its own" was a claim
the code could not actually back up.

| # | Test | Proves |
|---|---|---|
| 39 | `test_non_probe_successes_cannot_close_the_breaker` | 10 successes with `was_probe=False` leave it not-CLOSED. A reply from a request admitted while CLOSED says nothing about recovery. |
| 40 | `test_non_probe_failure_cannot_reopen_the_breaker` | A late failure from before the trip cannot drive a HALF_OPEN probe back to OPEN. |

## Group 10 - Observers must not drive the machine (2 tests)

The dashboard polls state every second. Reading used to be a side effect, so a
browser tab left open could start a probe budget on a system nobody called.

| # | Test | Proves |
|---|---|---|
| 41 | `test_reading_state_never_advances_the_machine` | 50 reads of `state` / `snapshot` / `error_pct` / `transitions` leave it OPEN. |
| 42 | `test_the_request_path_does_advance_the_machine` | `effective_state` is the one place the OPEN -> HALF_OPEN step happens. Separates "pure" from "dead". |

## Group 11 - Retry is three gates, not one (1 test)

| # | Test | Proves |
|---|---|---|
| 43 | `test_retry_is_gated_on_idempotence_and_attempts` | Needs CLOSED **and** `idempotent` **and** attempts left. A dropped response on a POST is not retried - that is how one action becomes two. |

## Group 12 - The gate is one call (5 tests)

The documented pattern used to pair `allow_call()` with `acquire_slot()`, which
spends **two** probe slots per request. 10 configured probes became 5.

| # | Test | Proves |
|---|---|---|
| 44 | `test_acquire_slot_alone_uses_the_whole_probe_budget` | 30 attempts admit exactly `half_open_probes` (10), not 5. |
| 45 | `test_release_slot_is_symmetric_with_acquire_slot` | A slot cannot leak on the exception path and starve the pool. |
| 46 | `test_acquire_slot_returns_the_probe_flag` | The `was_probe` flag comes from the admitting call, not a second state read that another thread could invalidate. |
| 47 | `test_pool_rejection_does_not_consume_the_probe_budget` | **Ten pool rejections used to exhaust the whole budget** and strand a healthy dependency in HALF_OPEN forever. Refused calls refund the probe. |
| 48 | `test_refused_calls_return_none` | `GateResult.REFUSED` separates "refused, nothing spent" from "admitted". |

## Group 13 - Evidence does not drift (2 tests)

| # | Test | Proves |
|---|---|---|
| 49 | `test_transition_rows_record_the_error_rate_of_that_moment` | A row's `error_pct` stays true after the window moves on. Otherwise "what was the failure rate when it tripped?" gets answered with a later number. |
| 50 | `test_error_pct_of_an_empty_window_is_zero` | No divide-by-zero on a cold breaker. |

## Group 14 - One breaker per key, under contention (1 test)

| # | Test | Proves |
|---|---|---|
| 51 | `test_registry_is_thread_safe` | 8 threads racing on `get()` get **one** breaker. An orphan would run its own storm guard, invisible to `states()`. |

## Group 15 - The gate result cannot be misread (2 tests)

The gate has three honest answers, so I first returned `Optional[bool]`:
`None` = refused, `True` = probe, `False` = admitted-but-ordinary. That was a
trap. The line a teammate would naturally write is

```python
if not breaker.acquire_slot():
    return fallback(...)     # fast-fail
```

and with `False` meaning *admitted*, every healthy call took the fallback **and**
leaked its bulkhead slot. `GateResult` fixes it by making only `REFUSED` falsy.

| # | Test | Proves |
|---|---|---|
| 52 | `test_the_gate_result_is_safe_under_plain_truthiness` | Both allowed results are truthy, so the idiom above cannot fire for real traffic. |
| 53 | `test_refusal_is_the_only_falsy_gate_result` | `REFUSED.allowed` and `REFUSED.was_probe` are both False - a refused call is not a probe. |

## Group 16 - Close the gaps coverage found (3 tests)

Measured with `pytest --cov --cov-branch`, which showed lines the suite had
never executed. One of them was a false pass - see the note under #54.

| # | Test | Proves |
|---|---|---|
| 54 | `test_reporting_view_is_truthful_without_acting` | `effective_state_for_reporting()` shows HALF_OPEN once the sleep window passes, **without** performing the transition. Reading it 1x leaves `state` at OPEN. |
| 55 | `test_registry_snapshot_reports_every_breaker` | `BreakerRegistry.snapshot()` returns a row per api_key, each with `state`, `error_pct` and `bulkhead` - the dashboard's always-visible strip. |
| 56 | `test_registry_snapshot_holds_no_lock_while_reading_breakers` | Items are copied out under the registry lock, so no code holds two breaker locks at once and two dashboards cannot deadlock. |

**The false pass at #54.** `test_one_failed_probe_reopens_the_breaker` called
`record_*` straight after `clock.advance()`, but `state` is pure now, so the
breaker was still recorded as OPEN and both calls took the plain-window
branch. The HALF_OPEN -> OPEN reopen code never ran, and the assertion passed
for the wrong reason: the breaker had never left OPEN. Coverage is what
caught it. The rewritten test goes through `acquire_slot()`, asserts it really
is in HALF_OPEN first, and checks `HALF_OPEN -> OPEN` appears in the timeline.

---

## Coverage map against the demo's claims

| Claim a judge will test | Test |
|---|---|
| "the breaker opens in seconds, not minutes" | #5, #15 |
| "it does not open on a single blip" | #4, #6, #7 |
| "it recovers on its own" | #18, #19, **#39, #42** |
| "only a few probes hit the recovering API" | #16, #17, **#44, #47** |
| "no retry storm during the outage" | #10, #11, #13, **#43** |
| "one slow API cannot starve the others" | #29 |
| "the timeline is provable" | #22, #23, **#49** |
| "a dead API does not stop a healthy one" | **#51** |