# JIZO — 5-minute demo script

Owner: P6. Two full timed rehearsals before stage; hold the "30 seconds left"
card. This walks the **Demo App** (`/`) and the **Dashboard** (`/dashboard`) —
both served by the same process, reading the same database.

The harness is a tiny **logistics dispatcher** that confirms a delivery route
using two real APIs nobody owns — Open-Meteo (weather) and Nominatim
(geocode). It is **the crash-test dummy, not the product**. Never call it "the
weather app" on stage; introduce it as *the test harness for the JIZO library*.

---

## Pre-flight (before you walk on)

```bash
docker compose up -d                       # Postgres, healthy in ~10s
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/resilient
python -m alembic upgrade head
python -m backend.seed                     # registry + demo runs + baselines

uvicorn backend.main:app --port 8000
```

> **Port note:** if a local Postgres already holds 5432, either stop it or map
> the container elsewhere and set `DATABASE_URL` to match. The demo does not
> care which port, only that `DATABASE_URL` points at the seeded database.

Open **http://localhost:8000** in a browser. Keep **/dashboard** in a second tab.

Smoke check (all three must print `ok`):

```bash
curl -s localhost:8000/health | grep -q '"status"' && echo "health ok"
curl -s localhost:8000/ready  | grep -q '"ready":true' && echo "ready ok"
curl -s localhost:8000/ | grep -q "Demo App" && echo "demo ok"
```

If **Wi-Fi is off**, the demo still runs: the fallback ladder serves cache and
seeded defaults. Say so — it is a feature, not a rescue.

---

## The 5 minutes

### 0:00 — The problem (30s)

> "Every app calls APIs it doesn't control. Open-Meteo, Nominatim — they
> rate-limit, they 503, they die at 3am. Most teams *believe* their retry and
> circuit breaker work, because nothing crashed in staging. That's luck, not
> evidence. JIZO is a resilience layer that **proves** it works — by breaking
> things on purpose, at known timings, and scoring what happened."

### 0:30 — Protect: one confirm, live (30s)

On the **Demo App**, click **Confirm route**.

> "One route confirm fans out to weather and geocoding through the protector.
> Both answered **live**."

Point at the **mechanism strip** (`Timeout → Retry → Circuit breaker →
Fallback`), the two cards (`LIVE`), and the **Live evidence** table.

> "That table isn't a mock — every row is a real request, read back out of the
> database. Attempt 1, status 200, 900ms."

### 1:00 — Degrade: break the upstreams (45s)

Click **☠ Break the upstreams**, then **Confirm route** again.

> "I just killed both APIs."

Verdict turns amber, badges go `CACHE`, **Retry + Timeout + Fallback** light up,
`attempts: 3`.

> "It retried three times, timed out, and served a cached answer. **The
> customer never saw an error.**"

### 1:45 — Resist: the circuit breaker (60s)

In the **Circuit breaker** panel, click **Hammer it** (~5s).

> "Now I stop being polite — sustained failures until the breaker opens."

Chip flips to **OPEN**. Then click **Confirm route**.

> "`attempts: 0`. It stopped touching the API entirely — that's the retry-storm
> guard. A retry storm against a dying dependency is how you turn their outage
> into yours."

Click **Recover** (~13s — narrate over it: *"it waits out the sleep window,
then probes with a limited budget — never a stampede"*).

> "Back to **CLOSED**, automatically."

### 2:45 — Prove: fault injection (75s)

In the **Proof** panel, click each drill. Each prints a verdict.

- **Post-effect** → `PASS`. *"The dangerous one: the answer is lost after the
  work committed. `Mult ✓` — it did **not** charge twice."*
- **k-of-n** → `PASS`. *"Four calls, we break exactly the third. Only the third
  fell back; one, two and four never noticed."*
- **Order-sensitive** → `PASS`. *"A rival answer arrives before the real one. It
  never committed on the stale data."*

> "This is the difference: we don't assert the protection works — we break the
> call at a known timing and **score** it."

### 4:00 — Observe: the dashboard (45s)

Switch to the **/dashboard** tab (or click **Open dashboard**).

> "Same process, same database — the demo's traffic is already here."

Point at the hero, the eight trend charts, the **open breakers** count, the
**TS scorecards**, the **4-axis radar**, and the **Protected vs Control** bars.

> "This is the watchtower. Every number traces to a row a judge can query."

### 4:45 — Close (15s)

> "Success goes from ~75% unprotected to ~96% protected — measured, not
> claimed. Every number in this deck traces to a test, a log line, or a cited
> paper. **Proven, not assumed.**"

---

## What each step proves (rubric mapping)

| Step | JIZO method |
|---|---|
| Confirm (live) | the happy path; persistence |
| Break upstreams | timeout, retry with backoff, fallback ladder |
| Hammer → OPEN | circuit breaker |
| Confirm while OPEN | storm guard (fast-fail, zero upstream contact) |
| Recover | probe budget; OPEN → HALF_OPEN → CLOSED |
| The three drills | temporal fault injection + the TS scorer |
| Dashboard | control-vs-experiment, 4-axis radar, the proof log |

**Not on screen: the bulkhead** (per-dependency concurrency limiter). Say it in
words — "a per-dependency pool so one slow API can't starve the others" — don't
pretend you showed it.

---

## The 30-second fallback card (if the live demo dies)

Do **not** debug on stage. Keep talking and switch to:

1. **/dashboard** still renders from the database with no upstreams — the same
   board, from stored rows.
2. `curl -s localhost:8000/health` — dependency truth even if the UI is down.
3. The **Live evidence** table — the stored attempts, from Postgres.
4. A saved screenshot of the five steps.

> "The live box is struggling — which is exactly the failure JIZO is built for.
> Here is the same evidence, captured a minute ago."

Keep printed/offline copies of: a dashboard screenshot, the `/health` payload,
and one drill log line.

---

## Rehearsal discipline

- Run the whole thing **twice, timed**, on the demo laptop.
- **Restarting the server resets the in-memory breakers to CLOSED** — a clean
  start. Rehearse from a fresh `uvicorn`.
- Rehearse the **Recover wait** (~13s). Fill it with the probe-budget line.
- Rehearse the **offline path** with Wi-Fi physically off.
- Nominatim rate-limits: if the first click shows `DEFAULT` instead of `LIVE`,
  that is real, not broken — say *"there, it just degraded, and the route still
  confirmed."*
