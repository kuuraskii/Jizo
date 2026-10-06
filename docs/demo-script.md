# JIZO — 5-minute demo script

Owner: P6. Two full timed rehearsals before stage; hold the "30 seconds left"
card. Read the **Pre-flight** section out loud once while the machine boots.

The harness is a tiny **logistics dispatcher** that confirms a delivery route
using two real APIs nobody owns — Open-Meteo (weather) and Nominatim
(geocode). It is **the crash-test dummy, not the product**. Never call it "the
weather app" on stage; introduce it as *the test harness for the JIZO library*.

---

## Pre-flight (do this before you walk on)

```bash
docker compose up -d                       # Postgres, healthy in ~10s
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/resilient
python -m alembic upgrade head
python -m backend.seed                     # registry + demo runs + baselines

uvicorn backend.main:app --port 8000 &     # the runnable product
python -m backend.dashboard --api-key weather &   # the watchtower
```

Smoke check (all three must be true before you start):

```bash
curl -s localhost:8000/health  | grep -q '"status"'   && echo "health ok"
curl -s localhost:8000/ready   | grep -q '"ready":true' && echo "ready ok"
curl -s "localhost:8000/route/plan?mode=experiment"   | grep -q '"trace_id"' && echo "route ok"
```

If **Wi-Fi is off**, the demo still runs: fallbacks come from cache/seed
defaults. Say so — it is a feature, not a rescue.

---

## The 5 minutes

### 0:00 — The problem (30s)

> "Every app calls APIs it doesn't control. Open-Meteo, Nominatim — they
> rate-limit, they 503, they die at 3am. Most teams *believe* their retry and
> circuit breaker work, because nothing crashed in staging. That's luck, not
> evidence. JIZO is a resilience layer that **proves** it works — by breaking
> things on purpose, at known timings, and scoring what happened."

### 0:30 — One call, fully protected (45s)

```bash
curl -s "localhost:8000/route/plan?mode=experiment"
```

> "One route confirm fans out to both APIs through the protector: timeout,
> bounded retry with backoff and jitter, a circuit breaker, and a fallback
> ladder — live, then cache, then a default, then a clear message. Every call
> carries `X-Breaker-State`."

Point at the dashboard: **hero = HEALTHY**, eight trend charts, the single
**API Key dropdown**.

### 1:15 — Moment 1: the post-effect drill (45s)

```bash
curl -s -X POST localhost:8000/fi/run -H 'content-type: application/json' -d '{
  "run_id":"demo-post-effect",
  "pattern":"post_effect",
  "fault":"http_500",
  "target":{"api_key":"weather","phase":"post_effect","occurrence":1},
  "guard":{"api_key":"weather","phase":"post_effect","min_count":1},
  "total_occurrences":1,
  "idempotent":false
}'
```

> "The dangerous failure isn't slow — it's *did it already happen?* This call
> commits a side effect, then the dependency fails. Watch `Mult`."

On the dashboard, the **Drill Scorecards** panel updates; `Mult=false`, `TS=true`.

### 2:00 — Moment 2: k-of-n, only call 3 (45s)

```bash
curl -s -X POST localhost:8000/fi/run -H 'content-type: application/json' -d '{
  "run_id":"demo-k-of-n",
  "pattern":"k_of_n",
  "fault":"http_500",
  "target":{"api_key":"weather","phase":"recv","occurrence":3},
  "guard":{"api_key":"weather","phase":"send","min_count":3},
  "total_occurrences":4,
  "idempotent":true
}'
```

> "Four logical calls. We break exactly the third. Only the third falls back;
> one, two and four never noticed. That precision is the whole point — a
> random fault test would have broken the wrong call and told you nothing."

Dashboard: the third call's row shows `servedFrom=default`, the others `live`.

### 2:45 — Moment 3: sustained failures → OPEN → recover (60s)

> "Now we stop being polite. Sustained 5xx until the breaker opens."

Drive failures up to the sourced minimum sample (volume 20), or use the demo
policy with `breaker_min_volume=5` — **and say which one out loud**:

```bash
for i in $(seq 1 20); do
  curl -s "localhost:8000/route/plan?mode=control" >/dev/null
done
curl -s localhost:8000/breaker/state
```

> "`OPEN`. From here the protector stops touching the upstream — fast-fail,
> zero retries. A retry storm against a dying dependency is how you turn their
> outage into yours. Watch the dashboard."

Point at the **hero flipping to NOT WELL** (hot pink) and **Open Circuit
Breakers** ticking up. Then restore and recover:

```bash
# after the sleep window, the probe budget arms automatically
curl -s "localhost:8000/route/plan?mode=experiment"   # HALF_OPEN probe
curl -s localhost:8000/breaker/state                  # -> CLOSED on success
```

> "OPEN, then a limited probe — never a stampede — then CLOSED. Automatic."

### 3:45 — The watchtower (45s)

Switch the **API Key dropdown** from weather to another dependency.

> "One dropdown, one screen, no scrolling. Every number here is read from the
> same proof log a judge can query later — this is not a mock-up."

Point at the **4-axis radar** and **Protected vs Control** bars.

### 4:30 — Close (30s)

> "Success went from 75% unprotected to 96% protected — measured, not claimed.
> Every number in this deck traces to a test, a log line, or a cited paper.
> **Proven, not assumed.**"

---

## The 30-second fallback card (if the live demo dies)

Do **not** debug on stage. Switch to the offline pack and keep talking:

1. `backend/dashboard.py` renders the same board from the database with no
   server: `python -m backend.dashboard` (falls back to a browser tab).
2. `curl -s localhost:8000/health` — show dependency truth even if the UI is
   down.
3. Show the last structured log line: attempts, `breaker_state`, `served_from`.
4. Show the saved screenshot of the 3 drill moments.

> "The live box is struggling — which is exactly the failure JIZO is built
> for. Here is the same evidence, captured a minute ago."

Keep printed/offline copies of: the dashboard screenshot, the `/health`
payload, and one drill log line.

---

## Rehearsal discipline

- Run the whole thing **twice, timed**, on the actual demo laptop.
- Rehearse the **sleep window** in Moment 3 (the breaker needs ~10s before the
  probe) — fill it with the "retry storm" line, don't stand in silence.
- Rehearse the **offline path** with Wi-Fi physically off.
- Nobody waits on anyone: if a teammate's panel isn't ready, the script still
  completes on the dashboard + curls.
