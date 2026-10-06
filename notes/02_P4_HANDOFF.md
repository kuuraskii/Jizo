# Jizo P4 Handoff — Integration + Compare

## Owner

**Riya — P4 Integration + Compare**

## Supporting owner

**Pushkar**

## Purpose

P4 connects the P1 scoring/event contract, P2 resilience proxy, and P3 persistence layer into a usable API surface and provides the protected-vs-control comparison.

## Deliverables

| Component | Responsibility |
|---|---|
| `backend/main.py` | FastAPI routes, orchestration, persistence wiring, WebSocket |
| `backend/compare.py` | Control-vs-experiment comparison |
| `tests/test_main.py` | Route/integration behavior tests |
| `tests/test_compare.py` | Comparator tests |

Pushkar's P4 ownership is `axes.py` and the demo harness.

## Main flow

```text
P1 schemas/scoring
       ↓
P2 resilient_get()
       ↓
P4 main.py
  ├── /route/plan
  │     ├── control arm
  │     └── experiment arm
  │             ↓
  │        compare.py
  │             ↓
  │      control / experiment / delta
  │
  └── /fi/run
        ├── occurrence 1
        ├── occurrence 2
        ├── ...
        └── occurrence N
               ↓
           score_run()
               ↓
           P3 save_run()
```

## `/route/plan` architecture

Both arms share a trace ID so they can be correlated, but each arm has its own evidence bus.

```text
                 shared trace_id
                       │
             ┌─────────┴─────────┐
             │                   │
       control_bus         experiment_bus
             │                   │
        control mode       experiment mode
             │                   │
             └─────────┬─────────┘
                       ↓
                 compare_sides()
```

This is important because the evidence itself carries no mode field. P4 therefore assigns mode at persistence time from the arm that produced the evidence.

## `/fi/run` architecture

`total_occurrences` is executed by P4.

```text
one run / one trace
        │
        ├── occurrence 1 → resilient_get()
        ├── occurrence 2 → resilient_get()
        ├── occurrence 3 → resilient_get()
        └── occurrence N → resilient_get()
                              │
                         retries internal
                              │
                         one call_index
        │
        ↓
     score once
        ↓
     save once
```

This is what makes k-of-n drills executable through the API.

## Important verified case

A `k=3-of-4` drill was verified with the fault targeted at call 3.

Result:

- call 3 received the injected fault
- fallback was served
- TS passed
- no premature/missed/duplicate verdict was produced

## API endpoints

### `POST /route/plan`

Returns the selected arm result together with the control-vs-experiment comparison and evidence count.

### `POST /fi/run`

Runs a fault-injection drill according to the submitted drill specification.

### `GET /fi/runs/{id}`

Returns a stored or unfinished run.

### `GET /breaker/state`

Returns breaker reporting state.

### `GET /health`

Returns health information.

### `GET /ready`

Returns readiness status.

### `WS /ws/dashboard`

Dashboard update channel.

## Test evidence

Final suite:

**334 passed, 11 skipped**

P4-specific test counts:

- `tests/test_main.py`: 76 passed
- `tests/test_compare.py`: 34 passed

## Cross-team handoff items

The following remain outside Riya's P4 ownership:

1. P3 `save_run()` currently cannot receive the real attempt count.
2. The frozen P1 event schema does not have an event-level retry-attempt field.
3. P2 does not currently populate `leaked_raw_error`.
4. P2 has no live caller for `record_transition()`.
5. P2/P3 `base_url` handling still needs a path suitable for a real upstream.
6. Live upstream execution remains an integration/demo-stage task.

## Operational note

Each fresh `/fi/run` execution should use a unique `run_id`, because completed runs are idempotent in P3.

## Definition of P4 completion

P4's implementation criteria are met by:

- working integration routes
- separate control/experiment evidence
- working comparator
- executable total-occurrence drills
- correct call-index behavior
- persistent run retrieval
- passing automated tests

Live upstream proof and remaining cross-team resilience instrumentation are not represented as completed by this handoff.
