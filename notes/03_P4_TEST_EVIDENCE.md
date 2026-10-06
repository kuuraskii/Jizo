# P4 Test Evidence

## Final result

**334 passed, 11 skipped**

No test failures were present in the final verified P4 state.

## P4 test files

### `tests/test_main.py`

**76 passed**

Covers the FastAPI integration surface, route behavior, evidence persistence, mode handling, trace behavior, failure behavior, and multi-occurrence drill execution.

### `tests/test_compare.py`

**34 passed**

Covers the pure control-vs-experiment comparison behavior.

## Important regression coverage

### Control/experiment attribution

The tests verify that separate evidence buses are persisted with their correct modes rather than labeling both arms with the requested mode.

### Shared trace

Control and experiment retain a common trace ID while their evidence remains separated by bus and mode.

### UUID trace IDs

The route no longer derives trace IDs from the request object's memory identity.

### Failure response shape

Successful and failed dependency results expose the same core fields, with `error` added only when applicable.

### Exception safety

A regression test verifies that an unexpected exception does not get replaced by an `UnboundLocalError` from the persistence `finally` block.

### Per-API attempts

The tests verify that different retry counts for weather and geocoding remain distinct.

### Configured User-Agent

The geocoding request uses the P3-resolved Nominatim user-agent configuration.

### `total_occurrences = 1`

A regression test pins the old single-occurrence behavior to exactly one `resilient_get()` call.

### k-of-n execution

A k=2-of-3 drill can reach `TS=True` through the `/fi/run` API.

### Targeted occurrence

A k=3-of-4 drill verifies that the injected fault lands specifically on call 3.

### Retry versus occurrence

The tests model the P2 retry behavior so retries do not accidentally increment `call_index`.

## Integration evidence

One verified k=3-of-4 run produced:

```text
evidence rows: 9
call_index set: [1, 2, 3, 4]

call 1: [send, recv]
call 2: [send, recv]
call 3: [send, recv, recv]
call 4: [send, recv]
```

The third call was the faulted occurrence. The fallback response produced a passing TS verdict.

## Limitations of the test evidence

The suite is primarily automated/stubbed integration coverage. It does not constitute proof of a successful live-upstream run.

The following remain cross-team integration gaps:

- live breaker transition persistence
- live raw-error leak detection
- real upstream path configuration
- true event-level attempt attribution

Those should not be described as P4 test failures.
