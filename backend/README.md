# JIZO backend - P1 contracts + P2 protector
# Owners: Pushkar (P1 lead, P2 breaker + logging), Riya (scorer), Aditi (config + proxy)
#
# P1's schemas are the frozen contract for the whole project. Everyone codes
# against these shapes. Do not change a field name without telling the team.

## Part 2 - Breaker + logging (Pushkar)

Aditi owns `config.py` + `proxy.py`; this half is the breaker and the logs.

```python
from backend import CircuitBreaker, ApiPolicy, BreakerState, CallLogger, get_logger

breaker = CircuitBreaker(policy)          # thresholds come from the policy

gate = breaker.acquire_slot()             # storm guard + probe budget + bulkhead
if not gate:                               # only GateResult.REFUSED is falsy
    return fallback(...)                  # fast-fail, no outbound request
try:
    result = await call_upstream()
    breaker.record_success(was_probe=gate.was_probe)
    return result
except Exception:
    breaker.record_failure(was_probe=gate.was_probe)
    if breaker.should_retry(attempt=1):    # False unless CLOSED + idempotent
        await backoff(...)
finally:
    breaker.release_slot()                # always - a leaked slot starves the pool
```

Four traps this example avoids, each of which cost a real bug:

1. **`acquire_slot()` alone.** It already runs the storm guard, the probe
   budget and the bulkhead. Calling `allow_call()` as well spends TWO probe
   slots for one request, which silently halves the recovery budget (10
   probes became 5).
2. **Take `was_probe` from the gate, not from a second state read.** Do not
   write `was_probe = breaker.effective_state is BreakerState.HALF_OPEN` -
   another thread can trip the breaker between the two reads and mislabel a
   real probe. `gate.was_probe` is the flag from the call that admitted it.
3. **`release_slot()` in a `finally`.** An exception path that skips it leaks
   a slot, and the pool silently degrades to refusing everything.
4. **Trust `gate`, don't test it for truthiness of its own meaning.** The gate
   has three honest answers - `REFUSED`, `ADMITTED`, `PROBE` - so it returns a
   `GateResult`, not a bool. Only `REFUSED` is falsy; an earlier
   `Optional[bool]` version made "admitted" falsy too, which made every
   healthy call take the fallback while leaking its slot.

| File | What it holds |
|---|---|
| `backend/breaker.py` | `CircuitBreaker` (CLOSED/OPEN/HALF_OPEN), `GateResult`, `BulkheadPool`, `BreakerRegistry`, `log_breaker_transition` |
| `backend/logging_conf.py` | `configure_logging()`, `get_logger()`, `CallLogger` (measures latency for you) |

Four behaviours worth knowing before you use it:

1. **The retry-storm guard has three gates.** `should_retry(attempt)` is
   `True` only when CLOSED **and** the call is idempotent **and** attempts
   remain. Retrying an open dependency costs ~38% more recovery time
   [Luo & Girard Sec. 4.4]; retrying a non-idempotent operation is how one
   dropped response becomes a double charge, which is the whole reason
   51% of real operations are treated as state-changing [Tan et al. 2026].
2. **The volume threshold protects against false trips.** 19 consecutive
   failures do *not* open the breaker - `breaker_min_volume=20` means a thin
   sample is never read as 100% errors.
3. **`state` is pure; `effective_state` acts.** Reading `state` never changes
   the machine, so a dashboard polling `/breaker/state` cannot drive the
   breaker into probing. The OPEN -> HALF_OPEN step happens only on the
   request path via `effective_state()`, so there is no timer thread either.
4. **Logging never sees credentials.** API keys in query strings,
   `Authorization` headers and tokens inside exception messages are redacted
   before they reach a sink - on both the structlog and stdlib paths.

Read `testcases/cases_breaker.md` and `cases_logging.md` for the 57 tests
behind these, and `edgecases/README.md` §7-8 for the handled cases.

---

## Part 1 - contracts, evidence bus, guards

```python
from backend import (
    EvidenceBus, FaultType, Phase, ServedFrom,
    k_of_n_drill, score_run,
)

# 1. Make a notebook that records what happened, keyed by traceId
bus = EvidenceBus()

# 2. Record steps as they occur (this is the whole emit API)
API = "weather"

# Calls 1 and 2 are healthy.
for _ in range(2):
    bus.record("trace-1", API, Phase.SEND)
    bus.record("trace-1", API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

# Call 3 is the drill target: it breaks, then recovers from cache.
bus.record("trace-1", API, Phase.SEND)
bus.record("trace-1", API, Phase.RECV, fault=FaultType.HTTP_500,
           served_from=ServedFrom.NONE)
bus.record("trace-1", API, Phase.RECV, served_from=ServedFrom.CACHE)

# Call 4 is healthy too.
bus.record("trace-1", API, Phase.SEND)
bus.record("trace-1", API, Phase.RECV, served_from=ServedFrom.LIVE, status_code=200)

# 3. Grade it
result = score_run(k_of_n_drill("trace-1", API, k=3, n=4), bus.events("trace-1"))
print(result.explain())   # -> "TS=PASS | CW=y | PS=y | Prem=n | Miss=n | Mult=n"
```

Every one of the four calls above is required. Drop the cache row and `CW`
goes false; drop call 4 and it goes false too, because the drill promised
four calls. That strictness is the point - see "Failing safe" below.

## The files

| File | What it holds | Who codes against it |
|---|---|---|
| `backend/schemas.py` | `ApiPolicy`, `FaultTarget`, `GuardAfter`, `EvidenceEvent`, `DrillSpec`, `ScoreResult`, `Phase`, `Pattern`, `BreakerState`, `ServedFrom` | Everyone (P2-P6) |
| `backend/faults.py` | `EvidenceBus`, `GuardEvaluator`, pattern helpers (`post_effect_drill`, `order_sensitive_drill`, `k_of_n_drill`) | Aditi (P2), Riya (P4) |
| `backend/scoring.py` | `score_run()` -> TS with CW/PS/Prem/Miss/Mult breakdown | Riya (P4), Nikunj (P5) |
| `backend/breaker.py` | `CircuitBreaker`, `BulkheadPool`, `BreakerRegistry` | Aditi (P2), Riya (P4) |
| `backend/logging_conf.py` | `configure_logging()`, `CallLogger`, `CORE_FIELDS` | Aayush (P3, log export), Nameh (P6) |
| `tests/test_temporal.py` | 36 tests: guard mechanics, bus bookkeeping, helper pairing, schema validation | Nameh (P6) extends |
| `tests/test_scoring.py` | 54 tests: TS verdicts per pattern + regression guards for each review bug | Nameh (P6) extends |
| `tests/test_breaker.py` | 56 tests: trip/reset, probe budget, storm guard, bulkhead, registry races, gate result | Nameh (P6) extends |
| `tests/test_logging.py` | 34 tests: line shape, latency, failure logging, credential redaction, cycles, structlog optional | Nameh (P6) extends |

Full suite: **162 passing**. Run `python -m pytest -q`.

## Key ideas in plain words

* **Evidence bus** - a lab notebook. Every party writes lines; the scorer
  replays them later to judge behaviour. Nothing is guessed after the fact.
* **Call k, not row k** - every event carries both `occurrence` (which row
  this is, per api+phase) and `call_index` (which CALL it belongs to). Judges
  use `call_index`, because one call commonly logs a failed attempt *and* a
  fallback, and counting rows would make "the 3rd call" drift.
* **Failing safe** - no evidence, no fault, or a promised call that never
  arrived all score as failures. A truncated trace is not a clean one.
* **After-guard** - a fault may fire *only after* proof the risky moment
  arrived (e.g. after the upstream committed). This is the idea adapted from
  temporal fault-injection research (Tan et al. 2026) and it is what a blunt
  "break the endpoint" injector cannot do.
* **TS** - `TS = CW AND PS AND NOT Prem AND NOT Miss AND NOT Mult`. Strict on
  purpose: one duplicated action fails the run.

## Running the tests

```bash
.\.venv\Scripts\activate.bat
python -m pytest -v
```

Expected: **180 passed**.

## Rules

1. The schemas are frozen. If you need a new field, add it as optional and
   tell everyone - never rename or remove an existing one.
2. No network calls in this part. Everything runs on stub evidence so the
   suite stays fast and never flakes.
3. `EvidenceBus` is per-request state. Create one per trace or reset between
   tests; do not use it as a global singleton.
4. When P2 logs a fallback, log it **on the same call** as the failure. The
   scorer asks "was the customer served for *this* call?", and a fallback
   recorded against a later call does not answer it.
5. P2 must set `leaked_raw_error=True` when an upstream error body reaches the
   caller. Only the proxy knows whether it converted the error into a friendly
   message, so the scorer will not guess from a status code.