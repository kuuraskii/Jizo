# P4 API Reference

This document records the P4 API surface implemented in `backend/main.py`.

## `POST /route/plan`

### Purpose

Execute the control and experiment arms and return the selected arm plus a control-vs-experiment comparison.

### Core behavior

- one shared trace ID
- separate control and experiment evidence buses
- control arm is unprotected
- experiment arm uses the resilience path
- each arm's evidence is persisted with its own mode
- comparison is produced by `compare_sides()`

### Response concept

```text
{
  trace_id,
  mode,
  address,
  result,
  comparison,
  evidence_rows
}
```

The dependency result objects use a consistent core field set on both success and failure.

## `POST /fi/run`

### Purpose

Execute a fault-injection drill and produce a P1 `ScoreResult`.

### Execution

For `total_occurrences = N`:

```text
for occurrence 1..N:
    resilient_get(...)
score_run(...)
save_run(...)
```

Retries occur inside `resilient_get()` and do not become new logical occurrences.

### Important caller rule

Use a unique `run_id` for each fresh execution because P3 treats completed run IDs idempotently.

## `GET /fi/runs/{id}`

### Purpose

Retrieve a stored drill result.

P4 distinguishes an unknown run from an unfinished run by consulting the P3 intent row because `load_run()` returns `None` for both.

## `GET /breaker/state`

### Purpose

Return the current breaker reporting state.

## `GET /health`

### Purpose

Expose the P3 health check.

## `GET /ready`

### Purpose

Expose the P3 readiness check.

A non-ready state is returned as an unsuccessful HTTP status.

## `WS /ws/dashboard`

### Purpose

Provide the dashboard update channel for P5.

The route supports dashboard snapshots and P4-originated update messages.

## Comparison fields

The comparator reports, per side:

- success
- fallback
- duplicates
- TS rate when verdicts exist

The delta is:

```text
experiment - control
```

for the reported metrics.

## Ownership boundary

P4 exposes the integration surface but does not own:

- P1 schema/scoring changes
- P2 proxy/breaker behavior
- P3 database/store changes
- P5 dashboard implementation
- Pushkar's axes/demo implementation
