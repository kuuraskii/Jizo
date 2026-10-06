"""
JIZO - Part 3 public persistence API.

Owner: Aayush (P3).

This is the surface Part 4 should code against. Nothing here is private by
convention (`_name`), because the alternative is P4 importing the internals
of `seed.py` and freezing them by accident.

| Task | Call |
|---|---|
| Load policies from the DB into P2's registry | `policies = await load_registry(session)` then `register_registry(policies)` |
| Grade + store a drill run | `await save_run(session, spec, result)` |
| Read a stored verdict back | `await load_run(session, run_id)` |
| Rebuild a run's evidence | `await load_evidence(session, trace_id)` |
| Record a breaker state change | `await record_transition(...)` |

## Two things this module exists to fix

1. **`api_registry` never reached P2.** P2's `config.py` has
   `register_policy()` and `load_policy()`, but nothing ever called
   `register_policy`, so the table, `.env` and P2's hardcoded dict were three
   disconnected worlds: editing `.env` changed nothing about a live call.
   `register_registry()` is the missing bridge. It is **synchronous on
   purpose** - see its docstring for why awaiting it mid-request would
   interleave the registry dict across tasks.

2. **A run was written in two transactions.** The old seed committed the spec
   and evidence, then separately committed the verdict. A crash in between
   left `ts IS NULL` forever, because the next run saw "already present" and
   skipped it. `save_run()` writes spec, evidence and verdict in one
   transaction and repairs an incomplete run on re-save.

## Rehydration is deliberate

`load_run()` rebuilds a real `ScoreResult` from the stored JSONB rather than
returning raw dicts, so P4/P5 can call `.explain()` on a run graded last week
and get the same answer without re-running anything. That is the whole point
of storing the timeline (Riya's note Sec. 10).
"""

from __future__ import annotations

import datetime as dt
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    ApiRegistryRow,
    BreakerTransitionRow,
    CRITICALITY_VALUES,
    FiRunRow,
    RequestLogRow,
)
from .schemas import (
    ApiPolicy,
    BreakerState,
    DrillSpec,
    EvidenceEvent,
    FaultType,
    Pattern,
    ScoreResult,
    ServedFrom,
)

__all__ = [
    "row_to_policy",
    "policy_to_row",
    "load_registry",
    "register_registry",
    "event_to_row",
    "row_to_event",
    "spec_to_row",
    "save_run",
    "load_run",
    "load_evidence",
    "load_run_evidence",
    "record_transition",
    "phases_for",
]


# ---------------------------------------------------------------------------
# Policy registry  (api_registry <-> P1 ApiPolicy <-> P2 register_policy)
# ---------------------------------------------------------------------------


def row_to_policy(row: ApiRegistryRow) -> ApiPolicy:
    """Rebuild P1's frozen `ApiPolicy` from a stored registry row.

    `ApiPolicy` is `frozen=True`, so this constructs a new one rather than
    mutating anything. The field-by-field mapping is explicit on purpose: a
    rename in either direction should fail loudly here rather than silently
    default a value.
    """
    return ApiPolicy(
        api_key=row.api_key,
        base_url=row.base_url,
        timeout_s=row.timeout_s,
        max_attempts=row.max_attempts,
        backoff_initial_s=row.backoff_initial_s,
        backoff_max_s=row.backoff_max_s,
        jitter_s=row.jitter_s,
        breaker_window=row.breaker_window,
        breaker_error_threshold=row.breaker_error_threshold,
        breaker_min_volume=row.breaker_min_volume,
        breaker_sleep_s=row.breaker_sleep_s,
        half_open_probes=row.half_open_probes,
        half_open_window_s=row.half_open_window_s,
        idempotent=row.idempotent,
        criticality=row.criticality,
        bulkhead_max_concurrency=row.bulkhead_max_concurrency,
        courtesy_rps=row.courtesy_rps,
    )


def policy_to_row(policy: ApiPolicy, owner: Optional[str] = None) -> ApiRegistryRow:
    """Turn a P1 `ApiPolicy` into a registry row ready to insert.

    Validates `criticality` here, not at commit time: P1's `ApiPolicy`
    accepts any string, but the table CHECKs `low|medium|high`. A policy the
    DB would reject raises `ValueError` now, with the policy in hand - not an
    `IntegrityError` later with a broken session.
    """
    if policy.criticality not in CRITICALITY_VALUES:
        raise ValueError(
            f"criticality {policy.criticality!r} is not stored; "
            f"expected one of {CRITICALITY_VALUES}"
        )
    return ApiRegistryRow(
        api_key=policy.api_key,
        base_url=policy.base_url,
        timeout_s=policy.timeout_s,
        max_attempts=policy.max_attempts,
        backoff_initial_s=policy.backoff_initial_s,
        backoff_max_s=policy.backoff_max_s,
        jitter_s=policy.jitter_s,
        breaker_window=policy.breaker_window,
        breaker_error_threshold=policy.breaker_error_threshold,
        breaker_min_volume=policy.breaker_min_volume,
        breaker_sleep_s=policy.breaker_sleep_s,
        half_open_probes=policy.half_open_probes,
        half_open_window_s=policy.half_open_window_s,
        idempotent=policy.idempotent,
        criticality=policy.criticality,
        bulkhead_max_concurrency=policy.bulkhead_max_concurrency,
        courtesy_rps=policy.courtesy_rps,
        owner=owner,
    )


async def load_registry(session: AsyncSession) -> list[ApiPolicy]:
    """Every stored policy, as P1 `ApiPolicy` objects.

    Sorted by `api_key` so callers get a stable order and a test can compare
    runs deterministically.
    """
    rows = (
        await session.execute(select(ApiRegistryRow).order_by(ApiRegistryRow.api_key))
    ).scalars().all()
    return [row_to_policy(row) for row in rows]


def register_registry(session_loaded: Sequence[ApiPolicy]) -> list[str]:
    """Push loaded policies into P2's in-process registry.

    Takes the already-awaited list from `load_registry()` rather than a
    session, because `register_policy` mutates a module-level dict and must
    not be interleaved with another task's awaits. Call it once at startup:

        policies = await load_registry(session)
        register_registry(policies)

    Returns the keys registered, for a startup log line.

    Imported lazily so this module stays usable when P2 is absent (the data
    layer does not depend on the protector).
    """
    from .config import register_policy

    for policy in session_loaded:
        register_policy(policy)
    return [p.api_key for p in session_loaded]


# ---------------------------------------------------------------------------
# Evidence  (request_logs <-> P1 EvidenceEvent)
# ---------------------------------------------------------------------------


def event_to_row(event: EvidenceEvent, *, mode: Optional[str] = None,
                 attempt: int = 1, guard: Optional[str] = None) -> RequestLogRow:
    """Map one P1 evidence event onto a `request_logs` row.

    `mode`, `attempt` and `guard` are parameters rather than being read off
    the event, because `EvidenceEvent` carries none of them (P1's schema is
    frozen and the bus has no concept of an HTTP attempt, a ChAP traffic
    group, or a guard string). P4 supplies them from its own request context:

    * `mode`    - `control` | `experiment`, from the traffic splitter
    * `attempt` - 1-based, from `ResilientResponse.attempts` / the call logger
    * `guard`   - e.g. `str(spec.guard.describe())`

    Defaults are the honest "we do not know" values: the first attempt, and
    **no mode**. The mode default was originally `"control"`, which would
    have silently tagged every experiment-arm row as the control arm and
    corrupted P4's control-vs-experiment comparison - worse than a NULL,
    because it looks like real data.
    """
    return RequestLogRow(
        trace_id=event.trace_id,
        api_key=event.api_key,
        phase=event.phase.value,
        occurrence=event.occurrence,
        attempt=attempt,
        status_code=event.status_code,
        latency_ms=event.latency_ms,
        breaker_state=event.breaker_state.value if event.breaker_state else None,
        guard=guard,
        mode=mode,
        effect_applied=event.effect_applied,
        leaked_raw_error=event.leaked_raw_error,
        served_from=event.served_from.value,
        fault=event.fault.value if event.fault else None,
        call_index=event.call_index,
        note=event.note,
    )


def row_to_event(row: RequestLogRow) -> EvidenceEvent:
    """Rebuild an `EvidenceEvent` from a stored row.

    Enough to feed straight back into P1's `score_run`, which is what makes a
    stored drill re-gradeable.

    Lossy by necessity: `attempt`, `mode` and `guard` live on the row but have
    nowhere to go on `EvidenceEvent`, so they are dropped. A
    `load_evidence` -> `score_run` -> `save_run` round-trip therefore resets
    every `attempt` to 1 unless the caller passes `attempts` explicitly.
    """
    return EvidenceEvent(
        trace_id=row.trace_id,
        api_key=row.api_key,
        phase=phases_for(row.phase),
        served_from=ServedFrom(row.served_from),
        status_code=row.status_code,
        effect_applied=row.effect_applied,
        fault=FaultType(row.fault) if row.fault else None,
        occurrence=row.occurrence,
        call_index=row.call_index,
        latency_ms=row.latency_ms,
        breaker_state=(
            BreakerState(row.breaker_state) if row.breaker_state else None
        ),
        leaked_raw_error=row.leaked_raw_error,
        note=row.note,
    )


def phases_for(value: str):
    """`Phase` from a stored string. Kept separate so the import stays local."""
    from .schemas import Phase

    return Phase(value)


async def load_evidence(
    session: AsyncSession,
    trace_id: str,
    api_key: Optional[str] = None,
) -> list[EvidenceEvent]:
    """Every stored event for one trace, in recorded order.

    **Ordered by `id`, not `ts`.** All rows of one run are usually inserted in
    a single flush, so their `ts` values tie and ordering by timestamp would
    be arbitrary. `id` is monotonic per insert and is the only reliable
    order until P4 needs true concurrency (gap L-01).
    """
    stmt = select(RequestLogRow).where(RequestLogRow.trace_id == trace_id)
    if api_key is not None:
        stmt = stmt.where(RequestLogRow.api_key == api_key)

    rows = (await session.execute(stmt.order_by(RequestLogRow.id))).scalars().all()
    return [row_to_event(row) for row in rows]


async def load_run_evidence(
    session: AsyncSession, run_id: str
) -> list[EvidenceEvent]:
    """A stored run's evidence, resolved through its own `trace_id`.

    `load_evidence()` needs a trace id, which a caller holding only a
    `run_id` does not have. This looks the run up first, so the pair
    (`load_run`, `load_run_evidence`) is enough to fully reconstruct a run
    from its id alone.

    Returns an empty list when the run is unknown or predates the
    `fi_runs.trace_id` column.
    """
    row = await session.get(FiRunRow, run_id)
    if row is None or not row.trace_id:
        return []
    return await load_evidence(session, row.trace_id)


# ---------------------------------------------------------------------------
# Runs  (fi_runs <-> P1 DrillSpec + ScoreResult)
# ---------------------------------------------------------------------------


def spec_to_row(spec: DrillSpec) -> FiRunRow:
    """A `fi_runs` row holding the plan, with the score columns left NULL.

    The score columns stay NULL until the run finishes, which is what lets a
    run that crashes mid-flight still record what it was trying to do.
    """
    return FiRunRow(
        run_id=spec.run_id,
        pattern=spec.pattern.value,
        target_api=spec.target.api_key,
        target_k=spec.target.occurrence,
        target_phase=spec.target.phase.value,
        guard_api=spec.guard.api_key,
        guard_phase=spec.guard.phase.value,
        guard_min_count=spec.guard.min_count,
        fault=spec.fault.value,
        idempotent=spec.idempotent,
        total_occurrences=spec.total_occurrences,
        spec=spec.model_dump(mode="json"),
    )


async def _evidence_already_stored(
    session: AsyncSession, events: Sequence[EvidenceEvent]
) -> bool:
    """Are these events' traces already in `request_logs`?

    `request_logs` has no natural unique key, so nothing at the database
    level stops the same evidence being written twice. Deleting a `fi_runs`
    row while leaving its evidence - then re-saving - is exactly how a run
    ended up with 18 rows instead of 9 during review. This check makes the
    write idempotent with respect to evidence.
    """
    trace_ids = {event.trace_id for event in events}
    if not trace_ids:
        return True

    count = await session.scalar(
        select(func.count())
        .select_from(RequestLogRow)
        .where(RequestLogRow.trace_id.in_(trace_ids))
    )
    return bool(count)


async def save_run(
    session: AsyncSession,
    spec: DrillSpec,
    result: ScoreResult,
    events: Optional[Sequence[EvidenceEvent]] = None,
    *,
    mode: Optional[str] = None,
    guard: Optional[str] = None,
    attempts: Optional[Sequence[int]] = None,
    commit: bool = True,
) -> FiRunRow:
    """Persist a drill run and return its row - one transaction, atomically.

    Behaviour:

    * **New run** - inserts the spec, any supplied evidence, then the verdict.
    * **Incomplete run** (`ts IS NULL`) - writes the missing evidence (if it
      is genuinely absent) and repairs the verdict. The old seed skipped any
      run it had seen before, so a crash between the two commits stranded the
      row as permanently unfinished - and a spec-only row created via
      `spec_to_row()` would never get its evidence.
    * **Complete run** - the verdict is left untouched, so re-saving is
      idempotent.

    Evidence is written only when that trace has no rows yet. Re-saving must
    not append a second copy of the same events, because `request_logs` has
    no unique key to catch the duplicate.

    Note the evidence rows and the `fi_runs.timeline` are two views of the
    same run. Writing them in one transaction is what keeps them consistent:
    a `fi_runs` row whose verdict says PASS but whose evidence is missing
    would re-grade to FAIL.

    `spec.run_id` is the primary key of `fi_runs`, so the verdict is applied
    to the row the spec created - never inserted as a second row.

    `attempts` maps each event to its 1-based HTTP attempt, in order. Without
    it every row is stored as attempt 1, which is wrong for any run that
    retried - pass `ResilientResponse.attempts`-style per-event numbers from
    the caller. A length mismatch raises `ValueError` rather than silently
    mislabelling.

    `commit` owns the transaction by default. Pass `commit=False` when this
    call is one step of a larger unit of work (e.g. a FastAPI route that does
    more after saving) - the rows are flushed so they are visible
    in-transaction, and the caller commits or rolls back.
    """
    if spec.run_id != result.run_id:
        raise ValueError(
            f"spec.run_id {spec.run_id!r} != result.run_id {result.run_id!r}; "
            "a verdict must be stored against the run it graded"
        )
    if attempts is not None and events is not None and len(attempts) != len(events):
        raise ValueError(
            f"{len(attempts)} attempts for {len(events)} events; "
            "pass one attempt number per event, in order"
        )

    row = await session.get(FiRunRow, spec.run_id)

    if row is None:
        row = spec_to_row(spec)
        session.add(row)

    if events:
        # Link the run to its evidence whenever we are told the trace,
        # regardless of whether the rows still need inserting. Setting this
        # only inside the insert branch meant a re-save (evidence already
        # present) left `trace_id` NULL and `load_run_evidence()` returned
        # nothing.
        if row.trace_id is None:
            row.trace_id = events[0].trace_id

        if not await _evidence_already_stored(session, events):
            # One row per event. `guard`/`mode`/`attempt` cannot come from
            # EvidenceEvent, so the caller passes what it knows.
            for i, event in enumerate(events):
                attempt = attempts[i] if attempts is not None else 1
                session.add(event_to_row(event, mode=mode, guard=guard,
                                         attempt=attempt))

    if row.ts is not None:
        # Verdict already recorded. Evidence may still have been missing
        # (a spec-only row), which the block above has now filled in.
        if commit:
            await session.commit()
        else:
            await session.flush()
        return row

    # Apply the verdict to the existing row (either the one just added in
    # this session, or the incomplete one we are repairing).
    row.ts = result.ts
    row.cw = result.cw
    row.ps = result.ps
    row.prem = result.prem
    row.miss = result.miss
    row.mult = result.mult
    row.timeline = [e.model_dump(mode="json") for e in result.timeline]
    row.spec = result.spec.model_dump(mode="json") if result.spec else row.spec
    row.notes = list(result.notes)

    if commit:
        await session.commit()
    else:
        await session.flush()
    return row


async def load_run(session: AsyncSession, run_id: str) -> Optional[ScoreResult]:
    """Read a stored verdict back as a real `ScoreResult`.

    Returns `None` for an unknown run, and `None` for a run that never
    finished (`ts IS NULL`). An unfinished run is not a failure - it means the
    drill started and never produced a verdict.
    """
    row = await session.get(FiRunRow, run_id)
    if row is None or row.ts is None:
        return None

    timeline = (
        [EvidenceEvent.model_validate(e) for e in (row.timeline or [])]
    )
    spec = DrillSpec.model_validate(row.spec) if row.spec else None

    return ScoreResult(
        run_id=row.run_id,
        pattern=Pattern(row.pattern),
        ts=row.ts,
        cw=bool(row.cw),
        ps=bool(row.ps),
        prem=bool(row.prem),
        miss=bool(row.miss),
        mult=bool(row.mult),
        timeline=timeline,
        spec=spec,
        notes=list(row.notes or []),
    )


# ---------------------------------------------------------------------------
# Breaker transitions
# ---------------------------------------------------------------------------


async def record_transition(
    session: AsyncSession,
    *,
    api_key: str,
    to_state: BreakerState | str,
    from_state: Optional[BreakerState | str] = None,
    error_pct: Optional[float] = None,
    reason: Optional[str] = None,
    ts: Optional[dt.datetime] = None,
    commit: bool = True,
) -> BreakerTransitionRow:
    """Record one breaker state change. P2 writes here as it flips.

    Two traps this handles so callers do not have to:

    * **Naive timestamps.** `breaker_transitions.ts` is
      `TIMESTAMP WITHOUT TIME ZONE`, and asyncpg rejects an aware datetime
      for a naive column with "can't subtract offset-naive and offset-aware
      datetimes". A tz-aware `ts` is converted to naive UTC here; omitting
      `ts` uses the server's `now()`.
    * **`error_pct` is 0-100**, not 0-1, matching the doc's `error_pct` and
      the `error_threshold_pct = 25` config. The table's CHECK only rejects
      `< 0` and `> 100`, so a caller who passes a *fraction* (0.25 meaning
      25%) stores `0.25` - i.e. 0.25% - **silently**. Nothing here rescales
      it, because guessing which unit a caller meant is worse than storing
      what they said; conversion is the caller's job.

    NOTE for P2: `CircuitBreaker.transitions()` yields `at` as
    **seconds since boot** (`time.monotonic()`), which is not a wall clock
    and cannot be stored in a timestamp column. Pass a real datetime, or
    omit `ts`.
    """
    def _state(value) -> Optional[str]:
        if value is None:
            return None
        return value.value if isinstance(value, BreakerState) else str(value)

    row = BreakerTransitionRow(
        api_key=api_key,
        from_state=_state(from_state),
        to_state=_state(to_state),
        error_pct=error_pct,
        reason=reason,
    )
    if ts is not None:
        row.ts = (
            ts.astimezone(dt.timezone.utc).replace(tzinfo=None)
            if ts.tzinfo is not None
            else ts
        )

    session.add(row)
    if commit:
        await session.commit()
    return row