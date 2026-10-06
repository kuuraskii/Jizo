# Aayush - P3 Data Layer

Author: **Aayush** (P3 owner)
Scope: `backend/models.py`, `backend/db.py`, `backend/secrets.py`,
`backend/seed.py`, `backend/store.py`, `backend/health.py`, `migrations/`,
`alembic.ini`, `docker-compose.yml`, `.env.example`, `tests/test_data.py`.
Audience: **P4 (FastAPI routes) and P5 (dashboard).**
Status: complete. `235 passed / 0 failed` project-wide, 55 of them P3.

---

## 1. What this part is and who it is for

P1 grades a drill run in memory: `score_run(spec, events) -> ScoreResult`.
P2 protects a live call: timeout, retry, breaker, fallback, structured logs.
Both are pure about the outside world - P1 touches no network and P2 keeps
nothing in a database.

**P3 is where a run stops being a temporary object and becomes a fact.** It
does three jobs:

1. **Persistence.** Four Postgres tables hold the registry policies, the
   breaker timeline, the drill verdicts and the evidence those verdicts rest
   on. `backend/store.py` is the public API over them.
2. **Configuration and secrets.** `backend/secrets.py` resolves
   `env > Key Vault > .env > default`. `policies` are seeded into
   `api_registry`.
3. **Health data.** `backend/health.py` computes the body of `GET /health`
   and `GET /ready`, without P4 importing the database engine internals.

Who reads what:

| You are... | Read |
|---|---|
| **P4**, wiring FastAPI routes | Section 4 - `store.py` is the whole surface. Stop after it and start writing routes |
| **P5**, building the dashboard | Section 2 (what each table answers) and Section 4 (`load_run`, `load_run_evidence`) |
| anyone changing the schema | Section 3 (how a run is stored), Section 7 (decisions), Section 9 (gaps) |
| deploying locally | Section 5 |

The one-line version: **P1 decides, P2 acts, P3 remembers - and remembering
has to be lossless, because a verdict that cannot be re-checked later is just
an assertion again.**

---

## 2. The four tables

The names are fixed by the PRD Sec. 4 and the build doc Sec. 7.6, which
agree. `tests/test_data.py::test_four_tables_match_doc_sec_7_6` asserts the
metadata contains exactly these four and no others.

| Table | The plain-English question it answers | Who writes it |
|---|---|---|
| `api_registry` | "What policy applies to this upstream?" | P3 seed / P4 at startup |
| `breaker_transitions` | "When did this dependency's breaker change state, and why?" | **P2** (as it flips); seed writes the baseline |
| `fi_runs` | "What drill did we intend to run, and what was the verdict?" | P4 via `store.save_run`; the seed for the demo |
| `request_logs` | "What actually happened on each step of the call?" | P4/P2 via `store.event_to_row` / `save_run` |

Note the tense in `breaker_transitions`: "**change** state" is `to_state <>
from_state` (`ck_breaker_state_actually_changed`). A row that would not change
anything is rejected as a caller bug, not stored.

### `request_logs` is a deliberate superset of Sec. 7.6

Sec. 7.6 abbreviates `request_logs` to `ts, traceId, api, latency, status,
attempt, breaker`. Built literally, that table could **not re-score a drill**,
because P1's scorer reads five fields that are not on that list:

```
effect_applied   leaked_raw_error   served_from   fault   call_index
```

Those are the exact fields `backend/scoring.py` reads on every event
(`notes/RIYA_P1_SCORING.md` Sec. 10, and
`tests/test_data.py::test_request_logs_carries_the_five_scorer_fields`). So
`request_logs` carries the documented fields **plus** those five, plus
`occurrence` (k) and `phase`, which Sec. 7.3's structured log line requires.
Without them, a pass recorded last week could not be re-derived from the
database - which defeats the entire reason the layer exists.

Enums are stored as `VARCHAR` with CHECK constraints rather than native
Postgres `ENUM`. Adding a fault type is then new data, not a migration; a typo
is still rejected. The vocabulary tuples in `models.py` are asserted equal to
P1's enums in `test_vocabulary_matches_p1_enums_exactly`.

### `fi_runs` is one row holding intent *and* verdict

`run_id` is the primary key, so a run never spans two rows (an early version
inserted the spec and then a second row with the same key - the regression is
pinned by `test_save_run_uses_one_row_and_never_duplicates_the_key`).

The score columns (`ts, cw, ps, prem, miss, mult`) are **nullable on purpose**.
The spec is written when the run *starts*; if the process dies mid-flight the
row still records what it was trying to do. `ts IS NULL` means "started, never
finished", which is information, not an error.

A **finished** run is forced to be self-consistent by a CHECK constraint:

```
ts IS NULL OR ts = (cw AND ps AND NOT prem AND NOT miss AND NOT mult)
```

and a second one requires all five component flags to be present whenever `ts`
is. No future writer can store a self-inconsistent verdict, so a stored score
is auditable by construction. An unfinished row is exempt by design.

`fi_runs.trace_id` was added by migration `0002_fi_runs_trace_id`. It is
nullable and additive. Its only job is the run-to-evidence link: before it,
`fi_runs` had no `trace_id` and `request_logs` has no `run_id`, so a caller
holding a `run_id` could not find the evidence. `save_run` sets it from the
events it is given; `load_run_evidence(session, run_id)` resolves a run's
evidence from the run id alone.

---

## 3. How a drill run becomes stored rows (worked example)

Use the k-of-n demo the seed ships, because P5 leads with it. Source:
`backend/seed.py::build_k_of_n_run`.

```
call 1: SEND -> RECV LIVE                      healthy
call 2: SEND -> RECV LIVE                      healthy
call 3: SEND -> RECV (fault=http_500, NONE)    the faulted call
             -> RECV CACHE                     recovered
call 4: SEND -> RECV LIVE                      healthy
```

The spec is `k_of_n_drill("seed-run-k-of-n", "geocode", k=3, n=4)`.
`EvidenceBus.record` assigns `occurrence` (rows per `(api, phase)`) and
`call_index` (calls; advances only on `SEND`). The nine rows it produces map
one-to-one onto `request_logs`:

| # | phase | occurrence | call_index | served_from | fault | status | effect_applied |
|---|---|---|---|---|---|---|---|
| 1 | send | 1 | 1 | none | - | - | f |
| 2 | recv | 1 | 1 | live | - | - | f |
| 3 | send | 2 | 2 | none | - | - | f |
| 4 | recv | 2 | 2 | live | - | - | f |
| 5 | send | 3 | 3 | none | - | - | f |
| 6 | recv | 3 | 3 | none | http_500 | 500 | f |
| 7 | recv | 4 | 3 | cache | - | - | f |
| 8 | send | 4 | 4 | none | - | - | f |
| 9 | recv | 5 | 4 | live | - | - | f |

Two things to notice, because they are the two counters P1 is strict about:

- **`occurrence` and `call_index` are different.** Call 3 logs three rows
  (occurrences 3 and 4 at `recv`), but is still call 3. If "the 3rd call" were
  read off a row counter it would drift the moment one call logs a fallback.
- **Row 7 is the fallback.** The customer was served cache, on the call that
  failed. `CW` is true because a response-phase row exists on call 3 at or
  after the fault; it would be false if the cache row were omitted.

`score_run` returns `ts=True, cw=True, ps=True, prem=False, miss=False,
mult=False`. `save_run(session, spec, result, events)` then writes, in **one
transaction**:

1. one `fi_runs` row - `run_id="seed-run-k-of-n"`, `pattern="k_of_n"`,
   `target_api="geocode"`, `target_k=3`, `target_phase="recv"`, `fault=
   "http_500"`, `total_occurrences=4`, plus the verbatim `spec` JSONB;
2. nine `request_logs` rows, all with `trace_id="seed-k-of-n"`;
3. the verdict columns and the `timeline`/`notes` JSONB on the same
   `fi_runs` row, plus `trace_id` so the evidence can be found again.

The seed does this for all three patterns. Live counts on the development
database as this was written:

```
api_registry 3   breaker_transitions 3   request_logs 16   fi_runs 3
seed-run-k-of-n          ts=True  cw=True ps=True prem=False miss=False mult=False
seed-run-post-effect     ts=True  cw=True ps=True prem=False miss=False mult=False
seed-run-order-sensitive ts=True  cw=True ps=True prem=False miss=False mult=False
```

`16 = 9 (k-of-n) + 4 (post-effect) + 3 (order-sensitive)`, matching the
builders.

---

## 4. The public API (`store.py`)

This is the surface P4 codes against. **Everything below is enough to write
the routes; you do not need `models.py` or `seed.py` to do it.**

`save_run` and `load_run` differ from the seed in one way and they are careful
about it: the spec and the verdict are written together, and an incomplete run
is repaired on re-save rather than skipped. A run is never "already present,
skip" if it has no verdict.

### Pull policies out of the DB and into P2's registry

`register_registry` is **synchronous on purpose**. `register_policy` mutates a
module-level dict, so awaiting it mid-request could interleave two tasks'
registrations. Load first, then register.

```python
from backend.db import get_sessionmaker
from backend.store import load_registry, register_registry

async def warm_registry() -> None:
    async with get_sessionmaker()() as session:
        policies = await load_registry(session)   # list[ApiPolicy], sorted by key
    keys = register_registry(policies)             # sync; returns the keys
    print(f"[startup] registered {len(keys)} policies: {keys}")
```

### Record intent before the run exists

```python
from backend.store import spec_to_row

row = spec_to_row(spec)      # score columns are all None
session.add(row)
await session.commit()       # intent survives a crash later in the run
```

### Grade and store a run atomically

```python
from backend.store import save_run

row = await save_run(session, result.spec, result, events=bus.events(trace_id))
assert row.ts == result.ts   # the row is the stored fact
```

`events` is optional; when supplied, `save_run` writes them only if that trace
has no rows yet (a second copy would be a silent duplicate - `request_logs`
has no unique key). `mode` and `guard` are keyword-only and default to the
honest "unknown": `mode=None`, `guard=None`.

### Read a stored verdict back

```python
from backend.store import load_run

result = await load_run(session, run_id)   # ScoreResult | None
if result is None:
    ...   # unknown run, or a run that started and never finished
else:
    print(result.explain())   # TS=PASS | CW=y | PS=y | Prem=n | Miss=n | Mult=n
```

`load_run` rebuilds a real `ScoreResult` from the JSONB, so `.explain()`,
`.timeline` and `.spec` all work on a week-old run with no re-execution.

### Rebuild a run's evidence

```python
from backend.store import load_evidence, load_run_evidence

events = await load_evidence(session, trace_id)          # every row of a trace
events = await load_run_evidence(session, run_id)        # same, via the run's own trace_id
```

`load_evidence` orders by `id`, **not `ts`** (see gap P3-G6).
`load_run_evidence` returns `[]` for an unknown run or one that predates the
`trace_id` column.

### Map one event to a row, and back

```python
from backend.store import event_to_row

row = event_to_row(
    event,
    mode="experiment",                 # P4's traffic splitter
    attempt=2,                         # 1-based, from the call logger
    guard="After(recv_geocode#1)",     # human-readable guard
)
session.add(row)
```

`mode`, `attempt` and `guard` are arguments, not event fields, because
`EvidenceEvent` carries none of them. The defaults are `attempt=1` and
`mode=None`; `mode` deliberately does **not** default to `"control"`, because
that would tag half of P4's comparison as the wrong arm and look like real
data.

`row_to_event(row) -> EvidenceEvent` is the inverse, enough to feed straight
back into `score_run`. `policy_to_row(policy, owner=None)` and
`phases_for(value)` are the other two helpers; `row_to_policy(row)` maps a
registry row onto the frozen `ApiPolicy`.

### Record a breaker transition

```python
from backend.store import record_transition

await record_transition(
    session,
    api_key="weather",
    from_state="CLOSED",
    to_state="OPEN",
    error_pct=42.5,                    # 0-100, not a fraction
    reason="sustained 503 above 25% over 100 requests",
)
```

Two traps it handles for you: a timezone-aware `ts` is converted to naive UTC
(`breaker_transitions.ts` is `TIMESTAMP WITHOUT TIME ZONE`), and omitting `ts`
uses the server clock. It does **not** rescale `error_pct`; see gap P3-G13.

### In a FastAPI route

`backend/db.py` shapes its session as a dependency so P4 does not need a
context manager:

```python
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_session
from backend.store import load_run


@app.get("/fi/runs/{run_id}")
async def get_run(run_id: str, session: AsyncSession = Depends(get_session)):
    result = await load_run(session, run_id)
    return {"found": result is not None,
            "explain": result.explain() if result else None}
```

`get_session` always closes the session and rolls back on an exception, so a
failed request cannot leave a half-written run committed.

---

## 5. How to run it (and what is unverified)

Alembic is the **single schema authority**. `python -m backend.seed` does
**not** create tables; if the migration has not run it fails with:

```
RuntimeError: database schema is missing - run `alembic upgrade head` first
```

`backend/seed.py::_require_schema` deliberately does not call
`Base.metadata.create_all`. An earlier version did, which produced a schema
missing every server default, with no `alembic_version` row, that then could
never be migrated. Pinned by `test_seed_never_calls_create_all`.

Run order, from the repository root:

```
docker compose up -d
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m backend.seed
```

Then P4 exposes `/health` by wrapping `backend.health.check_health()`; the
seed leaves real counts so it reports numbers before anyone runs a drill.

### What has actually been verified

**Verified by the author, against a real local PostgreSQL 16.4:** clean
migrate, seed, **idempotent re-seed** (a second run writes nothing), a
downgrade round-trip, and all three demo runs scoring `TS=PASS`.

**Re-verified while writing this note, against the live development database:**

- the suite is `235 passed / 0 failed`; the DB-backed tests ran rather than
  skipping, so `save_run`, `load_run`, `load_run_evidence`,
  `record_transition` and the health regressions were exercised against a real
  Postgres, not only in memory;
- `alembic current` reports `0002_fi_runs_trace_id (head)`;
- the table counts and the three `TS=True` verdicts shown in Section 3;
- a second `python -m backend.seed` printed `already present, skipped` for all
  three runs and `0 request_logs rows`, i.e. it is idempotent.

**Not verified, and stated plainly: the Docker container has never been
started.** `docker compose up -d` needs WSL2 plus administrator rights that the
author's machine did not have. `docker-compose.yml` is checked by text
(`test_docker_compose_matches_appendix_a1`,
`test_docker_compose_ports_is_a_list_not_a_scalar`) but not by `docker compose
config`, which CI should add. Treat the compose path as the one thing still to
prove. Also note the migration files carry a real `downgrade()`, but this note
did not re-run the downgrade against the live database; the author's original
round-trip is the evidence for that, and
`test_migration_downgrade_drops_everything` only reads the source text.

---

## 6. Config / secrets

`backend/secrets.py` resolves configuration from four sources, in this order:

```
real environment variable  >  Azure Key Vault  >  .env  >  built-in default
```

- **In cloud**, Key Vault is read through Managed Identity via
  `azure-identity`; there is no connection string anywhere.
- **Locally**, `.env` is a tiny hand-rolled parser (`settings.py` has minimal
  dependencies on purpose). It reads `utf-8-sig`, so a BOM from PowerShell's
  `Set-Content -Encoding UTF8` does not turn the first key into garbage, and it
  catches a non-UTF8 file rather than raising.
- **If Key Vault is unreachable, fall back to `.env`.** A demo that dies
  because a cloud service hiccuped is worse than one that runs locally - and
  that is exactly what happens on stage.

Variable names follow build doc Appendix A.1 verbatim (`.env.example` is that
block, paste-ready):
`UPSTREAM_TIMEOUT, MAX_ATTEMPTS, OPENMETEO_BASE, NOMINATIM_BASE, NOMINATIM_UA,
DATABASE_URL, AZURE_KEYVAULT_URL`. The upstreams are **keyless** (build doc
Sec. 1), so there is deliberately no `WEATHER_API_KEY` / `GEOCODE_API_KEY` -
an invented key would be sent to an API that ignores it.

`DATABASE_URL` **must** carry the `+asyncpg` driver suffix. Without it
`backend/db.py` raises a clear `RuntimeError` rather than letting asyncpg fail
obscurely at connect time.

Why the file is called `secrets.py` and not `config.py`: the build doc's Sec.
11.1 tree lists one `config.py` holding env + Key Vault + defaults, but **P2
(Aditi) landed first and claimed `config.py`** for the policy registry, and
`proxy.py` imports from it. Overwriting a merged module would break P2, so P3
owns only the env + Key Vault half. The doc's single-file intent is split
across two modules on purpose. See gaps P3-G4 and P3-G5 for the cost of that
split.

---

## 7. Decisions and why

**Enums are VARCHAR + CHECK, not native Postgres ENUM.** `ALTER TYPE` to add a
fault type is a migration; new data is not. The CHECK keeps a typo rejected.

**`fi_runs` stores `timeline` and `spec` as JSONB.** `ScoreResult` carries its
own timeline and spec so a historical run can be re-graded without re-running
it. Reconstructing them from `request_logs` at read time would break the day a
column is renamed. The spec JSONB is written when the run starts, so intent
outlives a crash.

**No foreign keys between the four tables.** Evidence is append-only and
linked by `trace_id`; the registry can be replaced. A FK from `request_logs.
api_key` to `api_registry` would make an audit row impossible to keep after a
registry change. The link discipline lives in `store.save_run` instead, and
`load_run_evidence` closes the run -> trace direction.

**The whole run is one transaction.** The original seed committed the spec and
evidence, then the verdict separately; a crash in between left `ts IS NULL`
forever because the next run saw "already present" and skipped it. `save_run`
writes spec, evidence and verdict together, and repairs an incomplete run on
re-save.

**`request_logs` duplication is handled in code, not the schema.**
`request_logs` has no natural unique key, so nothing at the database level
stops the same evidence being written twice; `_evidence_already_stored` checks
the trace id before inserting. Deleting a `fi_runs` row while leaving its
evidence - then re-saving - is exactly how a run grew from 9 rows to 18 during
review. Pinned by
`test_save_run_does_not_duplicate_evidence_left_by_a_deleted_run`.

**`/health` never raises and never leaks a secret.** It reports
`status` (`healthy`, or `degraded` when any breaker's latest known state is
`OPEN`, or `unhealthy` when the database is unreachable), `ok`, `uptimeSec`,
`dependencies` keyed by the registered `api_key` with `reachable: false` when
the breaker is `OPEN`, and `tables` row counts. `check_ready()` is stricter: it
also requires `api_registry` to be non-empty, because a pod with no policy
cannot protect anything. The database URL is rendered with
`hide_password=True`, and a generic exception's text is scrubbed before it is
returned, because `db.py`'s driver guard embeds the URL in its message.

**Dependency keys are P2's `api_key` names.** The registry is aligned to P2's
merged `config.py` (`weather`, `geocode`, `payment`). The build doc's own
example log line and `/health` example print `open-meteo` / `nominatim`, which
disagrees with the running code. P2 landed first and its tests depend on the
names, so P3 follows P2; the doc examples should be updated to match.

**`attempt` is 1-based**, matching P2's `resilient_get` (`attempt_1based`).
`ApiPolicy.backoff_delay_s(attempt)` counts *retries* and is 0-based; that is a
different question and is not the column. Pinned by
`test_attempt_check_is_one_based`.

---

## 8. Files changed vs files deliberately untouched

Added by P3:

| File | What it is |
|---|---|
| `backend/models.py` | The four SQLAlchemy 2.0 tables + CHECK vocabularies |
| `backend/db.py` | Async engine, loop-aware singleton, `get_session` dependency |
| `backend/secrets.py` | env + Key Vault resolution, `Config`, `.env` parser |
| `backend/store.py` | The public persistence API (Section 4) |
| `backend/seed.py` | Registry + three demo runs; schema-presence guard |
| `backend/health.py` | `/health` and `/ready` data, never raises |
| `migrations/env.py`, `migrations/script.py.mako` | Alembic wiring; URL from `secrets.py` |
| `migrations/versions/0001_initial.py` | Four tables, CHECK constraints, downgrade |
| `migrations/versions/0002_fi_runs_trace_id.py` | Adds `fi_runs.trace_id` |
| `alembic.ini` | `script_location = migrations`, blank URL on purpose |
| `docker-compose.yml` | Postgres 16 + readiness healthcheck |
| `.env.example` | Appendix A.1 variables, keyless upstreams |
| `tests/test_data.py` | 55 P3 tests |

**Deliberately not touched:**

| File | Owner | Why |
|---|---|---|
| `backend/schemas.py` | Pushkar (P1) | Frozen contract; P3 maps onto it, never changes it |
| `backend/faults.py` | Pushkar (P1) | Evidence bus; P3 only reads it |
| `backend/scoring.py` | Riya (P1) | Scorer stays pure and DB-free (invariant 1) |
| `backend/config.py` | Aditi (P2) | P3 imports `register_policy` / `registered_policies`; owns no defaults there |
| `backend/breaker.py`, `proxy.py`, `logging_conf.py` | Aditi (P2) | P3 consumes transitions; it does not write them yet |
| `backend/__init__.py` | P1/P2 | P4 imports `backend.db` / `backend.store` directly; no re-export added |
| `README.md`, `notes/README.md`, `edgecases/README.md`, `testcases/*` | team | Documentation; P3 did not rewrite them (see P3-G16) |

No test was edited or weakened to make the suite green.

---

## 9. Known gaps, with owners

Honest list. Verified in the code; each has one owner.

| ID | Gap | Impact | Owner |
|---|---|---|---|
| **P3-G1** | `request_logs.guard` and `request_logs.mode` have **no producer**. P2 emits neither, and `event_to_row` defaults them to `None` | The Sec. 7.3 log line's `guard` and the control/experiment split are empty until producers exist | `guard` -> P2; `mode` -> P4 |
| **P3-G2** | No live writer populates `breaker_transitions`. Only the seed's baseline CLOSED rows exist today | The breaker timeline and `/health`'s OPEN/degraded path have no real data until P2 calls `record_transition` | P2, via `store.record_transition` |
| **P3-G4** | Numeric defaults are duplicated in three places: P1's `schemas.py`, P2's `config.py`, P3's `secrets.py` | A threshold changed in one place can disagree with the others | team; reconcile to one source |
| **P3-G5** | `base_url` differs by module: P2's `config.py` is host-only (`https://api.open-meteo.com`), P3's seed is path-qualified (`.../v1/forecast`). `register_registry` overwrites P2's value with P3's | The effective URL depends on whether the registry was warmed | P2 + P3; agree on one form |
| **P3-G6** | Timeline ordering uses `id`, not `ts`, because one flush ties timestamps | Once P4 adds true concurrency, recorded order may not be wall-clock order | P4 / `edgecases` L-01 |
| **P3-G7** | The Docker path has never been started (WSL2 + admin rights) | `docker compose up -d` is the one step not proven end to end | Aayush; CI should run `docker compose config` |
| **P3-G9** | `/health`'s breaker state is "newest `ts`, first row wins"; if two transitions share a `ts` the tie is arbitrary | A dashboard can briefly show the wrong current state | P3; order by `(ts, id)` |
| **P3-G10** | `register_registry` only adds or overrides; it never removes | A policy deleted from `api_registry` survives in P2's in-process registry until restart | P3, on request |
| **P3-G11** | The engine resets on an event-loop change but does not dispose the old pool | Old connections are abandoned, not closed, when `asyncio.run()` is called twice | P3; acceptable for scripts, note for tests |
| **P3-G12** | Identity-map staleness: a same-session bulk `UPDATE` can make `save_run`'s "repair" a silent no-op | A repaired run may still read as unfinished in that session | P3; refresh or expire before repairing |
| **P3-G13** | `error_pct`'s unit is not enforced. The CHECK only rejects `< 0` and `> 100`, so a caller passing `0.25` (meaning 25%) stores `0.25` = 0.25% | A silently wrong breaker rate on the dashboard | caller; `record_transition` stores what it is given |
| **P3-G14** *(new)* | `store.load_run`'s docstring references `load_run_status()`, which does not exist anywhere | A reader is pointed at a function they cannot call; unfinished runs are indistinguishable from unknown ones via the public API | P3 |
| **P3-G15** *(new)* | `seed(reset=True)` exists but `python -m backend.seed` has no flag to reach it | There is no CLI way to reset the demo data | P3 |
| **P3-G16** *(new)* | `README.md`'s status table still lists P3 as "Planned" and "180 tests"; the project is now 235 tests | A reader of the front page gets a stale status | docs owner |

Gaps `P3-G3` and `P3-G8` are absent from the register above because no such
issues were found in the code during this pass; the numbering is left as-is
rather than renumbering the merged list.

---

## 10. Ownership

**Aayush owns P3**: `backend/models.py`, `backend/db.py`, `backend/secrets.py`,
`backend/store.py`, `backend/seed.py`, `backend/health.py`, the two Alembic
revisions and `migrations/env.py`, `alembic.ini`, `docker-compose.yml`,
`.env.example`, and `tests/test_data.py`.

**Read-only dependencies, owned elsewhere and not modified here:**
`backend/schemas.py` and `backend/faults.py` (Pushkar, P1);
`backend/scoring.py` (Riya, P1); `backend/config.py`, `backend/breaker.py`,
`backend/proxy.py`, `backend/logging_conf.py` (Aditi, P2).

**Handoffs:**

- **P4** writes `breaker_transitions` via `store.record_transition`, and
  supplies `mode` / `guard` / `attempt` to `event_to_row` (P3-G1). Warm the
  registry once at startup with `load_registry` + `register_registry`.
- **P5** reads verdicts with `load_run` and evidence with
  `load_run_evidence`; both are rehydratable and need no re-execution.
- **P2** is the only party that can honestly set `leaked_raw_error`, the field
  the scorer's PS flag depends on. Mirror the five scorer fields into
  `request_logs` through `event_to_row`; do not invent a sixth shape.

The rule to keep: **if you find yourself wanting P3 to guess a value, add an
explicit nullable column and tell the team instead.** That is how
`fi_runs.trace_id` and `request_logs.call_index` came to exist, and it is why
a verdict stored today can still be re-scored tomorrow.
