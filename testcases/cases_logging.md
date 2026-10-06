# Test Case Records - Structured Logging (Part 2)

Owner: Pushkar. Implementation: `backend/logging_conf.py`. Tests: `tests/test_logging.py`.
**34 tests, all passing.**

---

## Why this file exists

On stage you cannot grep a `print`. Every line must be one JSON object with
the same keys, so when something breaks during the demo you can find the exact
call, see which attempt failed, and prove what happened afterwards.

This is also how a judge question gets answered:

> "How do you know the breaker opened in three seconds rather than thirty?"

Filter on `api_key` and `breaker_state`, read `latency_ms`. The answer exists
because the log line format is fixed.

## The uniform line

```json
{"event":"upstream_call","trace_id":"trace-abc","api_key":"weather",
 "attempt":2,"status_code":200,"latency_ms":812.4,
 "breaker_state":"HALF_OPEN","served_from":"cache"}
```

`CORE_FIELDS` pins the key set in code, so a renamed field breaks a test rather
than silently breaking a teammate's grep.

---

## Group 1 - Line shape (3 tests)

| # | Test | Proves |
|---|---|---|
| 1 | `test_core_fields_are_the_documented_set` | The 8 keys are fixed and asserted literally |
| 2 | `test_success_line_carries_every_core_field` | A successful call renders **all 8** keys. Also pins that enums render as their `.value` (`HALF_OPEN`, not the repr) |
| 3 | `test_attempt_defaults_to_one` | A caller that forgets `set_attempt` still logs `1`, so a field is never missing |

## Group 2 - Latency (1 test)

| # | Test | Proves |
|---|---|---|
| 4 | `test_latency_is_measured_not_passed_in` | Latency comes from the context manager, so it is never hand-calculated wrong or forgotten |

## Group 3 - Failure logging (4 tests)

| # | Test | Setup | Expected | Why it matters |
|---|---|---|---|---|
| 5 | `test_failure_line_logs_at_warning` | `failure("ConnectTimeout")` | level=`warning`, `error_type` set, `status_code` **omitted** when None | Failures must stand out in a filter; `None` fields are dropped rather than serialised |
| 6 | `test_failure_includes_status_when_known` | `failure(..., 503)` | `status_code==503` | Distinguishes transport errors from HTTP errors |
| 7 | `test_exception_inside_the_block_is_logged_automatically` | raise inside the block | failure logged anyway | A caller **cannot forget** to log a failed call - the `__exit__` hook catches it |
| 8 | `test_context_manager_does_not_swallow_the_exception` | raise `ValueError` | re-raises with `error_type=ValueError` | **Logging must never mask the original error** |

## Group 4 - Idempotence + extras (2 tests)

| # | Test | Setup | Expected | Why it matters |
|---|---|---|---|---|
| 9 | `test_an_explicit_failure_is_not_logged_twice` | explicit `.failure()` | exactly 1 record | No double counting when the caller is careful |
| 10 | `test_extra_fields_are_attached` | `.extra(city="Delhi")` | custom fields present, core shape intact | Callers add context without changing the contract |

## Group 5 - structlog is optional (3 tests)

| # | Test | Proves |
|---|---|---|
| 11 | `test_get_logger_works_without_structlog` | With structlog absent, the logger still accepts the same kwargs. **This is why `requirements.txt` keeps structlog commented out** - `import jizo` must never fail |
| 12 | `test_configure_logging_is_safe_to_call_twice` | Process start-up may call it more than once; must not raise |
| 13 | `test_configure_logging_accepts_console_output` | `json_output=False` works, for debugging on a terminal |

## Group 6 - JSON validity (2 tests)

| # | Test | Proves | Why it matters |
|---|---|---|---|
| 14 | `test_json_line_is_valid_json_with_no_none_values` | Output parses; `None` fields dropped | Azure Monitor's log exporter parses these - a malformed line is silently dropped in production |
| 15 | `test_json_line_tolerates_unserialisable_values` | An exception object in a field renders as a string | Otherwise one odd field breaks a whole log line |

## Group 7 - Breaker transitions (1 test)

| # | Test | Proves |
|---|---|---|
| 16 | `test_breaker_transition_is_its_own_event` | Transitions get their own `breaker_transition` event with `api_key`, `breaker_state`, `error_pct`, rather than being buried in a call line |

## Group 8 - stdlib fallback adapter (4 tests)

| # | Test | Proves |
|---|---|---|
| 17 | `test_stdlib_adapter_renders_json_lines` | Without structlog the output is still one valid JSON object. Also pins that `api_key="weather"` (the dependency name) is **not** redacted |
| 18 | `test_stdlib_adapter_bind_carries_context` | `bind()` attaches fields. Regression: it returned `self` and dropped every kwarg, so caller context vanished while structlog's bind really binds - two renderers, two behaviours |
| 19 | `test_caller_supplied_event_does_not_crash_or_rename` | A caller field named `event` raised `TypeError` on the stdlib path and silently renamed the line on the structlog path. The call site's event name always wins |
| 20 | `test_nan_is_not_emitted_as_json` | NaN is invalid JSON, so an exporter drops the whole line |

## Group 9 - Credential redaction (3 tests)

| # | Test | Proves | Why it matters |
|---|---|---|---|
| 21 | `test_credentials_are_redacted` | `token=`, `secret=`, an `Authorization: Bearer ...` header, and an `api_key=` query parameter are all replaced | Log sinks are retained and searchable in Azure Monitor, so anything written is effectively permanent. **This group did not exist before review** - the old coverage map claimed a test proved it, and no test did |
| 22 | `test_ordinary_values_are_not_redacted` | `"Delhi"`, a plain URL and `api_key="weather"` survive | Redaction that blanks everything is useless; only credentials are touched |
| 23 | `test_structlog_path_redacts_too` | The structlog processor chain applies the same redaction | Otherwise installing structlog silently disables it |
| 24 | `test_redaction_survives_a_self_referential_structure` | A dict pointing at itself yields `<circular>` | `RecursionError` escapes into the caller's `except` and masks the real failure with our own logging |
| 25 | `test_redaction_stops_at_a_depth_limit` | 40-deep nesting truncates at `MAX_REDACT_DEPTH` | Logging must not be able to take down a request |
| 26 | `test_compound_credential_headers_are_redacted` | `X-Api-Key`, `subscription_key`, `openai_api_key` all redacted, bare `api_key` still kept | The first rule matched only exact `secret_key` pairs, so real header names leaked |

## Group 10 - The breaker must not be driven by an observer (2 tests)

| # | Test | Proves | Why it matters |
|---|---|---|---|
| 24 | `test_logging_a_transition_does_not_advance_the_breaker` | Calling the log helper does not move the breaker into HALF_OPEN | Regression: the helper read the lazy `state` property, so **the dashboard polling `/breaker/state` was driving the state machine** |
| 25 | `test_repeated_transition_logging_is_quiet` | 5 calls on a settled breaker log nothing; the same transition is never logged twice | Regression: it logged a "transition" every call, with no `from`/`to`, no delta and no timestamp |

## Group 11 - Close the gaps coverage found (6 tests)

`pytest --cov --cov-branch` showed `_StdlibJsonLogger.warning()` had **zero**
coverage, because every other test passed a fake logger. That method is what
`CallLogger.failure()` calls - the one line that proves the dependency went
down. A typo in it would have shipped unnoticed.

| # | Test | Proves |
|---|---|---|
| 26 | `test_call_logger_failure_reaches_a_real_stdlib_logger` | A real `CallLogger` failure through the real stdlib adapter emits at WARNING with outcome, trace_id, status_code, error_type and a real latency |
| 27 | `test_every_stdlib_adapter_level_emits_valid_json` | `debug`/`info`/`warning`/`error` all emit parseable JSON; severity rides on the stdlib record, which is what P3 filters on |
| 28 | `test_bearer_token_in_free_text_is_redacted` | `"HTTP 401 for Authorization: Bearer abc123"` is fully redacted - most clients put the URL in the error text, so a credential arrives without ever being a named field |
| 29 | `test_get_logger_returns_a_usable_logger_on_both_paths` | `get_logger()` works whichever implementation is installed, and returns the right kind of object |
| 30 | `test_configure_logging_runs_on_both_paths` | `configure_logging()` survives a bogus level string instead of raising at import time |
| 31 | `test_latency_is_absent_until_the_call_starts` | `latency_ms` is `None` before the call starts, never `0.0` - a fake 0.0 would read as an infinitely fast API on the dashboard |

Both structlog paths are verified by running the suite twice: once with
structlog absent and once with it installed. Both green.

---

## Coverage map against demo needs

| Need | Test |
|---|---|
| Grep one call across a whole trace | #1, #2 |
| Prove the breaker opened in N seconds | #16, #4 |
| See which attempt failed | #2, #3, **#26** |
| Prove no secret/token leaked into logs | #19-#21, #24-#26, **#28** |
| A dashboard cannot drive the state machine | #17 |
| Repeating a log call is idempotent | #6, #18 |
| Log export works without structlog installed | #11, #17, **#29, #30** |
| A crash is never silent | #7, #8 |
| Latency on the dashboard is honest | **#31** |