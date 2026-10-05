# Part 1 - Shared Contracts + Evidence Bus + Temporal Drill Engine + TS Scorer
# Owner: Pushkar (lead) + Riya (scorer)
#
# This is the frozen contract for the whole project. Everyone codes against
# these shapes. Do not change a field name without telling the whole team.

## What this part gives you

```python
from backend import EvidenceBus, GuardEvaluator, score_run, k_of_n_drill

# 1. Make a notebook that records what happened, keyed by traceId
bus = EvidenceBus()

# 2. Record steps as they occur (5 lines - this is the whole emit API)
bus.record("trace-1", "weather", Phase.SEND)
bus.record("trace-1", "weather", Phase.POST_EFFECT, effect_applied=True)
bus.record("trace-1", "weather", Phase.RECV, served_from=ServedFrom.LIVE)

# 3. Grade it
result = score_run(k_of_n_drill("trace-1", "weather", k=3, n=4), bus.events("trace-1"))
print(result.explain())   # -> "TS=PASS | CW=y | PS=y | Prem=n | Miss=n | Mult=n"
```

## The files

| File | What it holds | Who codes against it |
|---|---|---|
| `backend/schemas.py` | `ApiPolicy`, `FaultTarget`, `GuardAfter`, `EvidenceEvent`, `DrillSpec`, `ScoreResult`, `Phase`, `Pattern`, `BreakerState`, `ServedFrom` | Everyone (P2-P6) |
| `backend/faults.py` | `EvidenceBus`, `GuardEvaluator`, pattern helpers (`post_effect_drill`, `order_sensitive_drill`, `k_of_n_drill`) | Aditi (P2), Riya (P4) |
| `backend/scoring.py` | `score_run()` -> TS with CW/PS/Prem/Miss/Mult breakdown | Riya (P4), Nikunj (P5) |
| `tests/test_temporal.py` | 19 tests: Prem/Miss/Mult per pattern + clean TS=1 per pattern + guard mechanics | Nameh (P6) extends |

## Key ideas in plain words

* **Evidence bus** - a lab notebook. Every party writes lines; the scorer
  replays them later to judge behaviour. Nothing is guessed after the fact.
* **Occurrence k** - the bus counts 1st, 2nd, 3rd... call per
  `(traceId, api, phase)`. That is how "only call #3 of 4 breaks" is precise.
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

Expected: **19 passed**.

## Rules

1. The schemas are frozen. If you need a new field, add it as optional and
   tell everyone - never rename or remove an existing one.
2. No network calls in this part. Everything runs on stub evidence so the
   suite stays fast and never flakes.
3. `EvidenceBus` is per-request state. Create one per trace or reset between
   tests; do not use it as a global singleton.