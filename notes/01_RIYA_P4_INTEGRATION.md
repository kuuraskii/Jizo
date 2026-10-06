# P4 — Integration + Compare

**Owner:** Riya  
**Supporting owner:** Pushkar  
**Repository:** Jizo  
**Status:** Implemented and tested

## 1. Scope

Riya's P4 implementation covers:

- `backend/main.py`
- `backend/compare.py`
- `tests/test_main.py`
- `tests/test_compare.py`

Pushkar owns `axes.py` and the demo harness. P4 does not modify the frozen P1 event schema or the P2/P3 implementation files.

## 2. API surface

P4 implements the FastAPI integration surface:

- `POST /route/plan`
- `POST /fi/run`
- `GET /fi/runs/{id}`
- `GET /breaker/state`
- `GET /health`
- `GET /ready`
- `WS /ws/dashboard`

### `/route/plan`

Runs the protected experiment arm and the unprotected control arm and exposes their comparison.

The two arms use separate `EvidenceBus` instances but share one trace ID. Each arm's evidence is persisted with its own explicit mode:

- `control`
- `experiment`

This avoids positional evidence slicing and prevents one arm from being mislabeled as the other.

### `/fi/run`

Runs a fault-injection drill and owns `total_occurrences`.

For each logical occurrence, P4 calls `resilient_get()` once. Retries happen inside the P2 proxy and remain within that occurrence's `call_index`.

Conceptually:

- trace ID = overall drill/evidence stream
- `call_index` = logical occurrence
- retry attempt = retry inside one occurrence

After all occurrences complete, P4 calls `score_run()` once and saves the completed result.

### `/fi/runs/{id}`

Returns the stored drill result and supports retrieval of the evidence associated with the run.

P4 reads the P3 intent row directly when it needs to distinguish an unknown run from an unfinished run because `load_run()` returns `None` for both cases.

### `/breaker/state`

Exposes the current breaker reporting view.

### `/health` and `/ready`

Expose the P3 health/readiness checks through the API.

### `/ws/dashboard`

Provides the dashboard WebSocket surface used by P5.

## 3. Control-vs-experiment comparison

`backend/compare.py` is a pure comparison layer.

It provides:

- `resolve_mode()`
- `side_metrics()`
- `side_delta()`
- `compare_sides()`

The comparison reports control metrics, experiment metrics, and the experiment-minus-control delta.

The implementation reuses the existing P1 scoring concepts rather than duplicating scoring logic.

## 4. Trace, occurrence, and retry model

P4 deliberately keeps three concepts separate:

| Concept | Meaning |
|---|---|
| Trace ID | Identifies the overall evidence stream |
| `call_index` | Identifies one logical drill occurrence |
| Attempt/retry | A retry inside one logical occurrence |

For a 4-occurrence drill:

```text
trace T
  call_index 1
    SEND
    retry SEND/RECV events if needed
  call_index 2
  call_index 3
    fault
    fallback RECV
  call_index 4
```

A retry does not create a new occurrence.

## 5. Important P4 correctness properties

### Separate arm evidence

Control and experiment use separate buses while retaining a shared trace ID.

### Failure response consistency

`/route/plan` uses the same core result keys on successful and failed dependency results. Failure adds an optional `error` field instead of replacing the response shape.

### Safe persistence

The route persists both arm buses from the outer scope, preventing an exception from causing an `UnboundLocalError` that masks the original failure.

### Per-API attempt counts

`/route/plan` records the attempt count for each dependency independently instead of applying one maximum attempt count to both dependencies.

### Configured Nominatim user agent

The geocoding request reads the already-resolved `NOMINATIM_UA` configuration supplied by P3 rather than hardcoding the demo user agent.

### Run ID behavior

Completed P3 runs are idempotent. A fresh drill execution should therefore use a new `run_id`.

## 6. Verified drill behavior

A key integration test exercises a `k=3-of-4` drill.

Observed behavior:

```text
call 1: SEND, RECV
call 2: SEND, RECV
call 3: SEND, RECV, RECV
call 4: SEND, RECV
```

The fault lands on call 3, the fallback is served, and the final result is:

```text
TS = True
CW = True
PS = True
Prem = False
Miss = False
Mult = False
```

This demonstrates that the P4 occurrence loop preserves the targeted occurrence and allows a k-of-n drill to be graded successfully.

## 7. Test status

Final verified suite:

- **334 passed**
- **11 skipped**
- `tests/test_main.py`: **76 passed**
- `tests/test_compare.py`: **34 passed**

The final P4 implementation was added without modifying the existing P1/P2/P3 files or `backend/compare.py` after its initial comparator implementation.

## 8. Known cross-team limitations

These are intentionally not fixed in P4 because they belong to other layers:

| Issue | Ownership |
|---|---|
| `/fi/run` cannot persist a real attempt count because `save_run()` does not accept an attempt parameter | P3 |
| `request_logs.attempt` is call-level rather than row-level because the frozen `EvidenceEvent` has no attempt field | P1/P2 |
| `leaked_raw_error` is not currently populated by P2 | P2 |
| No live caller currently writes `breaker_transitions` | P2 |
| `base_url` has no path suitable for driving a real upstream through `/fi/run` | P2/P3 |
| Live upstream integration has not yet been proven | Integration/demo stage |

P4 does not claim these limitations are solved.

## 9. Ownership boundary

P4 should be considered responsible for:

- FastAPI integration
- route orchestration
- control/experiment separation
- comparison
- drill occurrence execution
- persistence wiring
- dashboard API/WebSocket surface

P4 is not responsible for changing:

- the frozen P1 event schema
- P1 scoring semantics
- P2 resilience behavior
- P3 database/store contracts
- Pushkar's `axes.py`
- Pushkar's demo harness
