# P4 Changelog

## Final P4 commit

`9db390f feat(P4): complete integration routes and drill execution`

This commit added the four-file P4 slice:

- `backend/main.py`
- `backend/compare.py`
- `tests/test_main.py`
- `tests/test_compare.py`

## Implementation milestones

### Comparator

Added a pure control-vs-experiment comparator with side metrics and experiment-minus-control deltas.

### FastAPI integration

Added the P4 API routes and application lifecycle wiring.

### Control/experiment separation

Replaced shared-bus/positional attribution with one evidence bus per arm and explicit mode persistence.

### Drill occurrence execution

Moved `total_occurrences` execution into `/fi/run`, preserving the distinction between logical occurrences and internal retries.

### Reliability fixes

- UUID-based trace IDs
- consistent failure result shape
- safe evidence persistence in `finally`
- per-API attempt counts for `/route/plan`
- P3-resolved Nominatim user-agent
- run-id idempotency documentation

## Final verification

**334 passed, 11 skipped**

No P1/P2/P3 source files were modified as part of the final P4 implementation.

## Follow-up items

Cross-team limitations remain documented separately. P4 does not claim live upstream validation, live breaker-transition persistence, or raw-error leak instrumentation as completed.
