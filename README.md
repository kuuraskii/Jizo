# JIZO

**When the Upstream API goes down, your application should not go down with it.**

JIZO is a resilience layer for code that depends on APIs you do not control.
It wraps every outgoing call with a timeout, a bounded retry, a circuit breaker
and a fallback — then it *proves* the protection actually works, by deliberately
breaking things at known failure timings and scoring the result.

Most teams believe their retry logic and circuit breaker are correct. They
believe it because it did not crash in staging. That is luck, not evidence.

---

## The problem

You are building a logistics dispatcher. To confirm a delivery route you need
weather data and geocoding. Those come from **Open-Meteo** and **Nominatim** —
services you have no control over. They rate-limit, they return 503s, they go
down at 3am, and you find out because a customer's screen is blank.

The failure is rarely dramatic. It is a single dropped response after a write
has already committed, or two requests racing so that a slow-but-correct answer
overwrites a fast-but-wrong one. These are timing bugs, and timing bugs do not
reproduce when you test them.

**51.0% of real API operations — 19,701 of 38,631 across eight public corpora —
are state-changing** (`POST`/`PUT`/`PATCH`/`DELETE`)
[Tan et al. 2026, Table I]. So for the majority of integrations, the question is
not "was the request slow" but **"did it already happen?"** Getting that wrong
turns one delivery confirmation into two, or silently shows stale data as live.

And the blast radius is not local. **A single failing service, with no circuit
breaker, cascades to an average of 7.3 further services within 90 seconds and
causes complete unavailability in 68% of trials**
[Luo & Girard 2026, Sec. 4.3].

---

## What JIZO does

### 1. Protects every call

| Concern | Behaviour |
|---|---|
| **Timeout** | 3.0 s per attempt. Hierarchy is client > gateway > attempt, so the breaker trips *before* the caller gives up. |
| **Retry** | Max 3 attempts, `delay = min(0.075 × 2^attempt + uniform(0, 0.05), 1.8)` s, capped by a 2.2× retry budget. Free on `GET`; gated on state-changing calls. |
| **Circuit breaker** | 100-request sliding window, trips at 25% errors with volume ≥ 20, sleeps 10 s, then admits 10 probes per 5 s. |
| **Bulkhead** | Per-dependency concurrency pool, so one slow API cannot starve the others. |
| **Fallback ladder** | live → stale cache (flagged as stale) → sensible default → clear message. A raw upstream 500 is never shown while the breaker is open. |
| **Structured logs** | One JSON line per call: `trace_id`, `api_key`, `attempt`, `status_code`, `latency_ms`, `breaker_state`, `served_from`. Credentials are redacted before any logger sees them. |

Every threshold is **read from policy, not hard-coded**, so a judge asking
"why 25%?" gets a citation rather than a shrug. The defaults are taken from
published tuning rather than invented — see [Why these numbers](#why-these-numbers).

### 2. Proves it, instead of assuming it

JIZO can reproduce three specific failure patterns at a chosen moment:

| Pattern | The failure | What passing looks like |
|---|---|---|
| **Post-effect** | The write commits, then the response is lost. | No duplicate action. Fallback served. |
| **Order-sensitive** | Two concurrent calls; the slower, correct answer arrives after the faster, wrong one. | No premature commit on rival data. |
| **k-of-n partial** | Only occurrence *k* of *n* fails. | Only call *k* falls back. The others stay live. |

Each run is scored as **Temporal Success**:

```
TS = CW ∧ PS ∧ ¬Prem ∧ ¬Miss ∧ ¬Mult        [Tan et al. 2026, Sec. VI-B]
```

| Term | Meaning |
|---|---|
| **CW** | Correct Withstanding — the customer still got served *after* the fault |
| **PS** | Protected Served — the fallback carried the request |
| **Prem** | Premature — something committed before its evidence existed |
| **Miss** | Nothing was served at all |
| **Mult** | A duplicate side effect |

It is a strict conjunction, deliberately. A scoring tool's worst possible bug is
a **false pass** — a green result that proves nothing. That is why `Miss` is
non-negotiable, and why an independent review found and closed **8 false-pass
holes** in the scorer before it was considered done.

### 3. Shows you the difference

JIZO runs the same workload **protected and unprotected** and reports the delta
— containment, recovery, detection and stability, as four axes. This is the
Netflix ChAP method: a single run tells you the system worked; only a comparison
tells you *your change* worked
[Basiri et al. 2016/17].

---

## Why this is not just another circuit breaker library

Circuit breakers are well understood. What is missing is **timing**, and
**proof**:

- **Timing.** Matching on API name alone achieves **0.0%** temporal success.
  Adding request/response matching reaches only **55.6%** overall
  [Tan et al. 2026, Tables IV–V]. The behaviour you need to get right depends
  on *when* the fault fired, not just which endpoint it hit — so JIZO guards on
  temporal evidence, not labels.
- **Proof.** A systematic map of the field (12 papers, 2015–2020, 23 articles
  read) finds circuit breakers overwhelmingly live in client-side library code,
  with **no real worked example of a proxy-side breaker**
  [Falahah et al. 2021, Sec. 4.1]. So we built one, and then built the harness
  that demonstrates it.
- **Scale of the search.** Naive fault enumeration explodes — a 25-event request
  reaches **4.04M** candidate orderings for a single 3-event guard. JIZO's
  approach reduces end-to-end search by **95.91%** against heuristic random
  search [Tan et al. 2026].

---

## Why these numbers

Nothing here was guessed. The defaults ship with their sources attached.

| Setting | Value | Source |
|---|---|---|
| Sliding window | 100 requests | Falahah et al. 2021, Sec. 4.4 |
| Error threshold | 25% | 20–30% gives the best sensitivity/stability balance [Pasunoori 2025, Sec. 2] |
| Volume threshold | 20 calls | Prevents tripping on a thin sample [Falahah et al. 2021] |
| Sleep window | 10 s | Demo setting; production uses 1→32 s exponential backoff |
| Half-open probes | 10 per 5 s | 94.7% recovery-detection accuracy [Pasunoori 2025, Sec. 2] |
| Timeout | 3.0 s | Demo default; enterprise profile is 1.8 s sync / 6.5 s async [Pasunoori 2025, Sec. 7] |
| Retry backoff | 75 ms → 1.8 s + jitter | Resilience profile; jitter reported at +83% stability [Pasunoori 2025] |

**Two numbers in particular drive the design:**

- **+38% recovery time** when aggressive retries run during an outage
  [Luo & Girard 2026, Secs. 4.2 & 4.4]. This is why retry is suppressed
  entirely while the breaker is open, rather than merely capped.
- **−83.5% cascading failures and +76% faster recovery** across 189
  cloud-native applications, for properly tuned breakers
  [Pasunoori 2025, Sec. 2]. This is why every threshold is configurable instead
  of fixed at a "reasonable" default.

> These are published findings about systems *like* this one. They are not
> measurements of JIZO, and we do not present them as such.

---

## Getting started

```bash
git clone https://github.com/kuuraskii/Jizo.git
cd Jizo

# Windows
setup_venv.bat
# macOS / Linux
./setup_venv.sh

# then
python -m pytest -q
```

Requires Python 3.11. The only hard dependencies are `pydantic` (validation)
and `pytest`. `structlog` is **optional** — the suite is green with it installed
and with it absent.

### Run the whole thing

```bash
# 1. database - one command brings up Postgres (healthy in ~10s)
docker compose up -d
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/resilient
python -m alembic upgrade head
python -m backend.seed

# 2. the runnable product (FastAPI: /route/plan, /fi/run, /breaker/state, /health)
uvicorn backend.main:app --port 8000

# 3. the dashboard - a native window, no browser, no URL to type
pip install -r requirements-gui.txt          # optional; falls back to a browser
python -m backend.dashboard --api-key weather
```

On a fresh machine the suite is the gate:

```bash
python -m pytest -q          # 419 tests; the DB-backed ones skip if no Postgres
```

The full stage walkthrough lives in [`docs/demo-script.md`](docs/demo-script.md).
Quality gates (secret scan + CI) are configured in `.pre-commit-config.yaml`
and `.github/workflows/ci.yml`.

### Using the breaker

```python
from backend import CircuitBreaker, ApiPolicy

breaker = CircuitBreaker(ApiPolicy(
    api_key="weather",
    base_url="https://api.open-meteo.com",
))


def protected_call(call_upstream, fallback):
    gate = breaker.acquire_slot()
    if not gate:
        return fallback()             # fast-fail — no outbound request
    try:
        result = call_upstream()
        breaker.record_success(was_probe=gate.was_probe)
        return result
    except Exception:
        breaker.record_failure(was_probe=gate.was_probe)
        raise
    finally:
        breaker.release_slot()        # always — a leaked slot starves the pool
```

Three rules, all load-bearing:

1. **`acquire_slot()` is the one gate.** Do not also call `allow_call()` — that
   spends two probe slots per request and silently halves the recovery budget.
2. **Take `was_probe` from the gate**, not from a second read of state. Another
   thread can trip the breaker in between and mislabel a real probe.
3. **`release_slot()` in a `finally`**, or the bulkhead pool starves.

---

## Repository layout

| Path | What lives there |
|---|---|
| `backend/` | The library: contracts, evidence bus, scorer, breaker, proxy, store, dashboard |
| `frontend/` | The dashboard UI (no build step — plain HTML/CSS/JS) |
| `tests/` | The suite, unit + integration |
| `testcases/` | What each test proves, grouped and numbered |
| `edgecases/` | Known limitations, each with an owner |
| `notes/` | Where the numbers come from and why the design is shaped this way |
| `docs/` | The 5-minute demo script |
| `setup_venv.bat` / `.sh` | One-command environment setup |

Start with `backend/README.md` for usage, then `notes/` for the reasoning
behind the thresholds.

### Current status

| Part | Scope | Status |
|---|---|---|
| P1 | Contracts, evidence bus, temporal guards, TS scorer | **Complete** |
| P2 | Circuit breaker, bulkhead, structured logging, `config.py`, `proxy.py` | **Complete** |
| P3 | Persistence: models, migrations, store API, seed, health, secrets | **Complete** |
| P4 | FastAPI routes, control-vs-experiment comparison, 4-axis scores | **Complete** |
| P5 | Dashboard: native window + headless `snapshot()` | **Complete** |
| P6 | QA gates, integration tests, demo script, rehearsal | **Complete** |

The P1 schema in `backend/schemas.py` is the **frozen contract**. Every later
part plugs into it; it does not change without telling the whole team. A test
(`tests/test_schemas_frozen.py`) enforces that.

---

## Engineering notes

A few things we chose deliberately, and would defend:

- **Reading state must never change it.** Observers are not allowed to act. A
  dashboard polling breaker status was originally *driving* the state machine,
  arming a probe budget that no call had requested. `state` is pure;
  `effective_state` acts only on the request path.
- **Only a genuine probe can prove recovery.** A late reply from a request that
  was already in flight when the breaker tripped says nothing about whether the
  dependency recovered. Without this check the breaker would close itself having
  sent zero requests.
- **Log lines are evidence.** A credential is redacted *before* the logger is
  called, not at render time — a logger we do not control must never receive a
  secret. Transition rows record the error rate at the moment of the transition,
  so "what was the failure rate when it tripped?" stays answerable.
- **Measure, don't eyeball.** Two of the bugs fixed in `breaker.py` were missed
  twice by reading the code and were caught immediately by branch coverage — one
  of them a test that asserted something trivially true.

We would rather ship something honestly labelled than something that looks
finished. Known gaps are listed in `edgecases/README.md` with an owner against
each one.

---

## References

| Claim | Source |
|---|---|
| 51.0% of operations are state-changing (19,701 / 38,631) | Tan et al. 2026, Table I |
| Static-Req 0.0% / Static-Phase 55.6% temporal success | Tan et al. 2026, Tables IV–V |
| Guard intuition, Eqs. 1–6 | Tan et al. 2026, Sec. III–IV |
| TS formula `CW ∧ PS ∧ ¬Prem ∧ ¬Miss ∧ ¬Mult` | Tan et al. 2026, Sec. VI-B |
| Breaker states + 5 parameters | Falahah et al. 2021, Sec. 4.4 |
| Proxy-side breaker has "no real example" | Falahah et al. 2021, Sec. 4.1 |
| 1 failing service → 7.3 more within 90 s, 68% unavailability | Luo & Girard 2026, Sec. 4.3 |
| +38% recovery time from aggressive retry during outage | Luo & Girard 2026, Secs. 4.2 & 4.4 |
| Bulkhead + breaker gives finer containment | Luo & Girard 2026, Abstract |
| Breaker tuning: −83.5% cascades, +76% recovery (189 apps) | Pasunoori 2025, Sec. 2 |
| Half-open 10 req / 5 s → 94.7% detection accuracy | Pasunoori 2025, Sec. 2 |
| Timeout, backoff, jitter tuning | Pasunoori 2025, Secs. 2, 6, 7 |
| Control-vs-experiment comparison method | Basiri et al. 2016/17 (Netflix ChAP) |

---


MIT licensed.
