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
| G-15 | Guard `min_count > 1` unmet | Window never opens, so `prem=True` | `test_guard_min_count_greater_than_one_is_enforced` | Handled |
| S-16 | `target.phase` other than `RECV` | Judges read it; healthy rows must exist at that phase | `test_non_default_target_phase_is_honoured` | Handled |
| S-17 | A foreign fault type appears in the trace | Only `spec.fault` counts; the drill is not marked missed | `test_a_foreign_fault_type_does_not_count_as_our_drill` | Handled |
| S-18 | Breaker state changes mid-trace | Does not by itself fail a drill (scorer ignores `breaker_state`) | `test_breaker_state_alone_does_not_fail_a_drill` | Handled |
| G-12 | Guard asked when no `SEND` has been logged yet | Call 1 is treated as the candidate; it fires only if the guard is already satisfied | `test_guard_never_fires_before_any_call_starts` | Handled |
| G-13 | Guard asked twice for the same in-flight call | Both calls see the same call number; a polling caller can double-inject | — | **Not handled** — see L-08 |
| G-14 | `should_fire` called after `SEND` | Judges the in-flight call, which is the order P2's proxy naturally uses | TC-G-01 | Handled |

## 2. Trace isolation

| # | Edge case | Expected behaviour | Covered by | Status |
|---|---|---|---|---|
| T-01 | Two concurrent traces | Fully isolated; each sees only its own events | TC-B-02 | Handled |
| T-02 | Query a traceId never seen | Empty list, no exception | `test_temporal.py` bus test | Handled |
| T-03 | `occurrence_count` on an unseen triple | Returns 0 (defaultdict) | TC-B-01 | Handled |
| T-04 | `reset()` one trace | Only that trace's events and counters cleared | `test_reset_clears_one_trace_only` | Handled |
| T-05 | `reset()` with no argument | Whole notebook **and both counters** cleared | `test_reset_all_clears_counters_too` | Handled |
| T-06 | Bus shared as a global singleton | Would leak evidence across requests | prevented by design (per-trace instance) | Handled |
| T-07 | Same trace reused after `reset()` | Counters restart at 1 | — | Handled |

## 3. Scoring verdicts

| # | Edge case | Expected behaviour | Covered by | Status |
|---|---|---|---|---|
| S-01 | Empty event list | `miss=True`, `ts=False` — never a free pass | `test_empty_trace_never_passes_for_any_pattern` | Handled (fails safe) |
| S-02 | Fault present, no effect recorded | Treated as **premature** — the injector ignored its own guard, so the run proves nothing | `test_post_effect_fails_when_no_commit_was_evidenced` | Handled (was a false PASS) |
| S-03 | One call applies its side effect twice | `mult=True`, `ts=False` even though the customer was served | TC-PE-02 | Handled |
| S-03b | Two *different* calls each commit once | **Not** a duplicate — normal traffic, `mult=False` | `test_effects_in_separate_calls_are_not_a_duplicate` | Handled (was a false FAIL) |
| S-03c | An unrelated API commits in the same trace | Scoped out — no cross-API contamination | `test_unrelated_api_does_not_influence_verdict` | Handled (was a false FAIL) |
| S-04 | Raw upstream 5xx reaches the caller | `ps=False` (policy leak), `ts=False`, via the explicit `leaked_raw_error` flag | `test_leaked_raw_error_fails_the_drill` | Handled (was inverted) |
| S-04b | A 500 that we converted into a friendly message | `ts=True` — must not be punished | `test_correct_handling_of_500_passes` | Handled (was a false FAIL) |
| S-05 | Fault type differs from the pattern default | Judge reads `spec.fault`; an HTTP_500 on an order-sensitive drill is not automatically "missed" | `test_drill_using_a_non_default_fault_is_not_scored_as_missed` | Handled |
| S-06 | Rival response **and** a commit | `prem=True` **and** `cw=False` — served from bad data is not "withstanding" | TC-OS-02 | Handled |
| S-07 | Duplicate effect *and* no fault | Both `mult=True` and `miss=True`, failing independently | `test_three_effects_in_one_call_reports_the_count` (miss side untested) | Handled |
| S-08 | Fault on a healthy occurrence | `prem=True` (`fired_on` contains something other than k) | TC-K-02 | Handled |
| S-09 | Target occurrence never faults | `miss=True` | TC-K-03 | Handled |
| S-10 | A healthy call also degrades | **Fails** — ALL expected healthy calls must serve LIVE, not just one | `test_k_of_n_fails_when_a_healthy_call_also_dies` | Handled (was a false PASS) |
| S-10b | `served_from=LIVE` accidentally set on a SEND/POST_EFFECT row | Cannot satisfy CW — only response-phase rows count | `test_served_from_on_a_bookkeeping_row_cannot_manufacture_a_pass` | Handled (was a false PASS) |
| S-13 | Fault served `LIVE` (the rival payload handed to the caller) | `prem=True`, `ts=False` — the canonical race bug | `test_order_sensitive_detects_serving_the_rival_payload` | Handled (was a false PASS) |
| S-14 | Spec targets call k, fault lands on another call | `prem=True` for every pattern, not just k-of-n | `test_post_effect_detects_fault_on_wrong_occurrence` | Handled (was a false PASS) |
| S-15 | Drill uses a fault other than the pattern default | Read from `spec.fault`; never scored as "missed" for using it | `test_drill_using_a_non_default_fault_is_not_scored_as_missed` | Handled (was a false FAIL) |
| S-11 | `total_occurrences=1` (single-call drill) | Allowed (was `ge=2`, relaxed so post-effect drills can be single-call) | TC-PE-01..04 | Handled |
| S-12 | `n=1` with k-of-n pattern | Expected-healthy list is empty, so CW rests on the faulted call alone | `test_k_of_n_with_n_equals_one_has_no_healthy_calls` | Handled |

## 4. Validation

Test references below were added after the coverage audit; the register originally listed these 12 cases with no traceability at all.

| # | Edge case | Expected behaviour | Status |
|---|---|---|---|
| V-01 | `min_count < 1` on `GuardAfter` | `ValueError` | Handled |
| V-02 | `occurrence < 1` on `FaultTarget` | `ValueError` | Handled |
| V-03 | Whitespace-only `api_key` | `ValueError` | Handled |
| V-04 | `total_occurrences < 1` on `DrillSpec` | `ValueError` | Handled |
| V-05 | `timeout_s <= 0` or `max_attempts < 1` | `ValueError` | Handled |
| V-06 | `breaker_error_threshold` outside 0-1 | `ValueError` | Handled |
| V-07 | Mutating an `ApiPolicy` after creation | Blocked — `frozen=True` | Handled |
| V-08 | `jitter_s < 0` | `ValueError` | `test_temporal.py` policy test | Handled |
| V-09 | `backoff_max_s < backoff_initial_s` | **Accepted silently** — every delay clamps to max | **Not handled** — see L-04 |
| V-10 | Unknown pattern string from an API request | Pydantic rejects with a clear enum error | Handled |
| V-11 | Unknown fault type string | Rejected as above | Handled |
| V-12 | Negative `latency_ms` | `ValueError` | Handled |

## 5. Pattern helpers

| # | Edge case | Expected behaviour | Status |
|---|---|---|---|
| H-01 | Guard/target mispairing by hand | Impossible via helpers — they always pair correctly | Handled |
| H-02 | `k > n` (target beyond the trace length) | Fault never fires, `miss=True` | `test_target_k_beyond_n_is_missed_not_passed` | Handled (fails safe) |
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
| B-06 | `jitter_s=0` | Delay is exactly `min(initial * 2**n, max)` — deterministic | `test_temporal.py` backoff test | Handled |
| B-07 | One call logs attempt + fallback rows | `call_index` counts it once; `occurrence` still counts rows | `test_call_index_is_not_row_count` | Handled |

---

## 7. Circuit breaker (`backend/breaker.py`, Part 2)

| # | Edge case | Expected behaviour | Covered by | Status |
|---|---|---|---|---|
| BR-01 | Fewer failures than `breaker_min_volume` | Never trips — a thin sample is not 100% errors | `test_does_not_trip_below_the_volume_threshold` | Handled |
| BR-02 | Error ratio exactly at the threshold | Trips (`>=`, not `>`) | `test_trips_at_the_threshold_not_only_above_it` | Handled |
| BR-03 | Error ratio just below threshold | Stays closed (20% sits in the safe 20-30% band) | `test_stays_closed_below_the_threshold` | Handled |
| BR-04 | Old failures never age out | Sliding deque evicts the back; an old burst cannot hold the breaker open | `test_mixed_window_evicts_the_oldest_samples` | Handled |
| BR-05 | More outcomes than the window size | Window stays capped at `breaker_window` | `test_window_never_exceeds_the_configured_size` | Handled |
| BR-06 | `error_pct()` on an empty window | Returns 0.0, never divides by zero | `test_error_pct_of_an_empty_window_is_zero` | Handled |
| BR-07 | Retry attempted while OPEN | Refused — the retry storm (+38% recovery time) | `test_no_retries_while_open` | Handled |
| BR-08 | Retry attempted while HALF_OPEN | Refused — retrying defeats the probe budget we are testing with | `test_no_retries_while_half_open` | Handled |
| BR-09 | Sleep window not yet elapsed | Stays OPEN, fast-fails | `test_stays_open_until_the_sleep_window_elapses` | Handled |
| BR-10 | More probes requested than the budget | Capped at `half_open_probes`; slots are **reserved**, not merely checked | `test_half_open_probe_budget_is_genuinely_limited` | Handled (was a real bug) |
| BR-11 | Probe budget exhausted, window not elapsed | Further calls refused | same test | Handled |
| BR-12 | Probe window elapses | Fresh budget starts | `test_probe_budget_refreshes_after_its_window` | Handled |
| BR-13 | Time passes with no probes | Stays HALF_OPEN — only clean probes close it | `test_recovery_without_probes_stays_half_open` | Handled |
| BR-14 | One probe fails | Immediately reopens; a single failure means still broken | `test_one_failed_probe_reopens_the_breaker` | Handled |
| BR-15 | Breaker closes after recovery | Error window cleared, so old failures cannot re-trip it | `test_closing_clears_the_error_window` | Handled |
| BR-16 | Failures recorded repeatedly while already OPEN | Only one `OPEN` transition logged | `test_repeated_open_does_not_duplicate_transition_rows` | Handled |
| BR-17 | Bulkhead pool saturated | Reject rather than queue — queueing turns a slowdown into a timeout | `test_bulkhead_refuses_rather_than_queueing_when_full` | Handled |
| BR-18 | `release()` called too often | Counter floors at 0, never negative | `test_release_never_goes_negative` | Handled |
| BR-19 | `bulkhead_max_concurrency=0` | `ValueError` at construction | `test_bulkhead_rejects_zero_concurrency` | Handled |
| BR-20 | Two dependencies, one saturated | The other is unaffected — containment is per-dependency | `test_bulkhead_pool_is_per_breaker_not_shared` | Handled |
| BR-21 | Slot requested while OPEN | Refused; the storm guard beats the pool | `test_no_slot_is_taken_while_open` | Handled |
| BR-22 | Concurrent `record_*` from several threads | No deadlock; window stays capped | `test_concurrent_failures_do_not_deadlock_or_lose_records` | Handled (was a deadlock) |
| BR-23 | Same `api_key` requested twice from the registry | Same breaker instance, shared counters | `test_registry_returns_one_breaker_per_api_key` | Handled |
| BR-24 | Sleeping machine woken by a clock jump past the sleep window | OPEN -> HALF_OPEN on read, no timer thread needed | `test_moves_to_half_open_after_the_sleep_window` | Handled |
| BR-25 | Probe consumed via `allow_call()` but the request is never sent | The slot is spent on nothing; the probe budget shrinks without any evidence | — | **Not handled** — see L-10 |
| BR-27 | `record_success()` without `was_probe=True` while HALF_OPEN | Ignored — cannot close the breaker | `test_non_probe_successes_cannot_close_the_breaker` | Handled |
| BR-28 | `record_failure()` without `was_probe=True` while HALF_OPEN | Cannot reopen the breaker | `test_non_probe_failure_cannot_reopen_the_breaker` | Handled |
| BR-29 | 20 requests in flight when the breaker trips; replies arrive after the sleep window | Ignored — late replies cannot fake recovery | `test_non_probe_successes_cannot_close_the_breaker` | Handled |
| BR-30 | Dashboard polls `state` / `snapshot()` repeatedly | Pure — no reader can advance the machine | `test_reading_state_never_advances_the_machine` | Handled |
| BR-31 | `allow_call()` paired with `acquire_slot()` | Burns two probe slots per request; documented as wrong | `test_acquire_slot_alone_uses_the_whole_probe_budget` | Handled |
| BR-32 | `acquire_slot()` then an exception before release | Caller must use `finally` with `release_slot()` | `test_release_slot_is_symmetric_with_acquire_slot` | Handled |
| BR-33 | `should_retry()` on a non-idempotent call | `False` — a dropped response on a POST is not retried | `test_retry_is_gated_on_idempotence_and_attempts` | Handled |
| BR-34 | `should_retry()` after the last attempt | `False` — bounded by `max_attempts` | `test_retry_is_gated_on_idempotence_and_attempts` | Handled |
| BR-35 | Two threads race to create the same breaker's registry entry | One instance; no orphaned machine | `test_registry_is_thread_safe` | Handled |
| BR-36 | Pool refuses while a HALF_OPEN probe was already reserved | Probe refunded, so 10 pool rejections cannot strand a healthy dependency in HALF_OPEN | `test_pool_rejection_does_not_consume_the_probe_budget` | Handled |
| BR-37 | Caller writes `if not breaker.acquire_slot():` | Safe — `GateResult` makes only `REFUSED` falsy. An `Optional[bool]` made "admitted" falsy, so every healthy call took the fallback and leaked its slot | `test_the_gate_result_is_safe_under_plain_truthiness`, `test_refusal_is_the_only_falsy_gate_result` | Handled |
| BR-38 | Caller passes `was_probe=False` for a genuine probe | Breaker trips and never closes | `test_the_request_path_does_advance_the_machine` | **Not handled** — caller error; see L-10 |
| BR-39 | A teammate reads `breaker.is_open` / `is_closed` / `is_half_open` | Pure — they answer the question without performing the transition | `test_reading_state_never_advances_the_machine` | Handled |
| BR-40 | `GET /breaker/state` after the sleep window elapsed | Reports HALF_OPEN without acting, so the dashboard cannot show a stale OPEN | `test_reporting_view_is_truthful_without_acting` | Handled |
| BR-41 | Two dashboards read `registry.snapshot()` at once | Items copied out under the lock; no nested breaker lock, so no deadlock | `test_registry_snapshot_holds_no_lock_while_reading_breakers` | Handled |
| BR-26 | Breaker never OPENs because volume is never reached | Long-lived `error_pct` is still accurate for the dashboard | `test_does_not_trip_below_the_volume_threshold` | Handled |

---

## 8. Structured logging (`backend/logging_conf.py`, Part 2)

| # | Edge case | Expected behaviour | Covered by | Status |
|---|---|---|---|---|
| LG-01 | Caller forgets to set the attempt number | Logs `1`; a core field is never missing | `test_attempt_defaults_to_one` | Handled |
| LG-02 | Caller forgets to log a failure | `__exit__` logs it from the exception type | `test_exception_inside_the_block_is_logged_automatically` | Handled |
| LG-03 | Exception raised inside the block | Logged **and** re-raised — logging never masks the error | `test_context_manager_does_not_swallow_the_exception` | Handled |
| LG-04 | Caller logs the failure explicitly *and* it raises | Exactly one record | `test_an_explicit_failure_is_not_logged_twice` | Handled |
| LG-05 | Failure with `status_code=None` | Field omitted rather than serialised as null | `test_failure_line_logs_at_warning` | Handled |
| LG-06 | Enum values (breaker state, served-from) | Render `.value`, not the repr | `test_success_line_carries_every_core_field` | Handled |
| LG-07 | `structlog` not installed | Logger still works with the same kwargs | `test_get_logger_works_without_structlog` | Handled |
| LG-08 | Output must be parseable by Azure Monitor | Valid JSON on every line | `test_json_line_is_valid_json_with_no_none_values` | Handled |
| LG-09 | A field holds an exception object | Renders as a string instead of breaking the line | `test_json_line_tolerates_unserialisable_values` | Handled |
| LG-10 | `configure_logging()` called more than once | No raise | `test_configure_logging_is_safe_to_call_twice` | Handled |
| LG-11 | Debugging on a terminal | `json_output=False` renders console-style | `test_configure_logging_accepts_console_output` | Handled |
| LG-12 | Called without the context manager | `_start` is `None`, so `latency_ms` is `None` and the field is dropped | — | Handled (no crash) |
| LG-13 | Call spans `success()` then a later failure on the same call | Two records; the breaker decides the verdict, not the log | — | **Not handled** — see L-11 |
| LG-14 | Concurrent calls from several threads | structlog is thread-safe; the stdlib adapter has no shared mutable state | — | Handled (by construction) |
| LG-15 | An `api_key` query parameter in a logged URL | Redacted; the rest of the URL stays readable | `test_credentials_are_redacted` | Handled |
| LG-16 | `headers={"Authorization": "Bearer ..."}` | Redacted | `test_credentials_are_redacted` | Handled |
| LG-17 | A credential inside an exception message | Redacted — a URL in the error text is scanned too | `test_credentials_are_redacted` | Handled |
| LG-18 | A caller field named `event` | Ignored; the call site's event name wins, no `TypeError` | `test_caller_supplied_event_does_not_crash_or_rename` | Handled |
| LG-19 | `bind(trace_id=...)` on the stdlib path | Fields are actually attached, matching structlog | `test_stdlib_adapter_bind_carries_context` | Handled |
| LG-20 | A NaN latency | Refused — NaN is invalid JSON and would cost the whole line | `test_nan_is_not_emitted_as_json` | Handled |
| LG-21 | structlog installed instead of the stdlib path | The redaction processor runs on the structlog chain too | `test_structlog_path_redacts_too` | Handled |
| LG-22 | `extra()` handed to a raw logger that does no redaction | Redacted at the call site, before the logger sees it | `test_credentials_are_redacted` | Handled |
| LG-23 | Over-broad redaction hiding ordinary values | `"Delhi"`, `"weather"`, plain URLs and numbers survive | `test_ordinary_values_are_not_redacted` | Handled |
| LG-24 | `X-Api-Key` / `subscription_key` header | Redacted — any key ending in a credential word, except bare `api_key` which names the dependency here | `test_credentials_are_redacted` | Handled |
| LG-25 | A self-referential dict passed to `redact()` | `<circular>` placeholder; `RecursionError` never reaches the request path | `test_redaction_survives_a_self_referential_structure` | Handled |
| LG-26 | A payload nested more than 6 deep | Truncated with a marker instead of recursing forever | `test_redaction_stops_at_a_depth_limit` | Handled |
| LG-27 | A transition row read long after the fact | `error_pct` is the rate at the transition, not today's | `test_transition_rows_record_the_error_rate_of_that_moment` | Handled |
| LG-28 | `configure_logging(level="typo")` | Falls back to INFO instead of raising at import time | `test_configure_logging_runs_on_both_paths` | Handled |
| LG-29 | structlog installed / not installed | Suite green both ways; `get_logger()` returns a usable object either way | `test_get_logger_returns_a_usable_logger_on_both_paths` | Handled |
| LG-30 | `latency_ms` read before the call starts | `None`, never `0.0` - a fake zero reads as an infinitely fast API | `test_latency_is_absent_until_the_call_starts` | Handled |

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
| L-10 | A probe consumed via `record_success` without `allow_call()` | The budget is not decremented, so direct recording bypasses the cap; only the proxy path is protected | Aditi (P2) — always gate via `allow_call()` |
| L-11 | One logical call producing both a success and a failure line | Two log records for one call; the verdict comes from the scorer, not the log, but the log alone looks contradictory | Aditi (P2) — decide whether to emit one terminal line per call |
| L-12 | Retry is a new `call_index`, so per-call `Mult` cannot see a double charge | The one flag meant to catch the double charge is blind to a cross-call retry | Pushkar (P1) + Aditi (P2) — see `B1` in `backend/scoring.py` |
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