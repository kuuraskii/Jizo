"""
JIZO - Part 3 Postgres tables.

Owner: Aayush (P3).

Table names and key fields follow the AI-Build Documentation Sec. 7.6
verbatim, because both the PRD (Sec. 4) and that document agree on them:

| Table                | Purpose                    | Source      |
|----------------------|----------------------------|-------------|
| `api_registry`       | Policy per upstream        | Sec. 7.6    |
| `breaker_transitions`| Timeline + audit           | Sec. 7.6    |
| `fi_runs`            | Proof log                  | Sec. 7.6    |
| `request_logs`       | Structured-log mirror      | Sec. 7.6    |

## Why `request_logs` carries more than Sec. 7.6 lists

Sec. 7.6 lists `ts, traceId, api, latency, status, attempt, breaker`. Taken
literally that is **not enough to re-score a drill**, because P1's scorer
reads five fields that are not in that list:

`effect_applied`, `leaked_raw_error`, `served_from`, `fault`, `call_index`

Those five are the contract in `notes/RIYA_P1_SCORING.md` Sec. 10. A table
built to the abbreviated list literally would make every past verdict
unauditable - which defeats the entire reason this layer exists. So
`request_logs` is a **superset**: the documented fields, plus the five the
scorer needs, plus `occurrence` (k) and `phase`, which Sec. 7.3 requires for
the structured log.

## Two additions with a concrete consumer each

* **`mode`** (`control` | `experiment`) - Sec. 7.3 lists `mode` in the log
  line, and Sec. 5.3 / 7.7 build the control-vs-experiment comparison from
  it. Without this column P4's comparator has nothing to split on.
* **`owner`** - Sec. 7.6 lists it on `api_registry`. Nothing enforces it;
  it is there so a judge can see who owns which upstream.

## Three decisions worth defending

1. **Enums are VARCHAR, not native Postgres ENUM.** A native ENUM needs
   `ALTER TYPE` to extend and cannot be removed cheaply, so adding a fault
   type would become a migration. Storing the `.value` string means new
   vocabulary is just new data. CHECK constraints keep it honest, so a typo
   is still rejected.

2. **`fi_runs` stores `timeline` and `spec` as JSONB.** `ScoreResult`
   carries its own timeline and spec "so a consumer can re-score or explain
   any historical run later without re-running anything"
   (`notes/RIYA_P1_SCORING.md` Sec. 10). Rebuilding them from
   `request_logs` at read time would break that the day a column is
   renamed. The spec JSONB is written when the run starts, so intent
   survives even if the run crashes mid-flight.

3. **Column names follow P1 where the two docs disagree.** Sec. 7.6 writes
   `timeout` / `latency`; P1's frozen `ApiPolicy`/`EvidenceEvent` use
   `timeout_s` / `latency_ms`. We keep the Pydantic names so the mapping is
   mechanical and lossless, and note the doc's abbreviation here rather than
   silently diverging. Worth confirming with Pushkar - renaming later costs
   a migration.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    Index,
    Integer,
    String,
    Text,
    false,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ---------------------------------------------------------------------------
# Vocabulary mirrors
#
# Kept as tuples so the CHECK constraints and this module cannot drift
# apart. Values come from the enums in `backend/schemas.py` - if P1 ever
# adds a member, add it here too.
# ---------------------------------------------------------------------------

PHASE_VALUES = ("send", "pre_effect", "post_effect", "recv")
FAULT_VALUES = (
    "drop_response",
    "delay",
    "http_500",
    "http_503",
    "timeout",
    "rival_response",
)
SERVED_FROM_VALUES = ("live", "cache", "default", "message", "none")
BREAKER_STATE_VALUES = ("CLOSED", "OPEN", "HALF_OPEN")
PATTERN_VALUES = ("post_effect", "order_sensitive", "k_of_n")
CRITICALITY_VALUES = ("low", "medium", "high")

#: Sec. 5.3: the ChAP-style traffic splitter routes `?mode=control` or
#: `?mode=experiment`. P4 owns the splitter; this column is where the result
#: lands so the comparison can be recomputed from stored evidence.
MODE_VALUES = ("control", "experiment")

#: No value we store is longer than this, and an unbounded `varchar` is not
#: indexable in Postgres.
_ENUM_LEN = 24


def _in_list(column: str, values: tuple[str, ...]) -> str:
    """Build a CHECK constraint restricting a column to known values."""
    options = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({options})"


class Base(DeclarativeBase):
    """Declarative base for every JIZO table."""


# ---------------------------------------------------------------------------
# api_registry  <- backend.schemas.ApiPolicy        [Doc Sec. 7.6]
# ---------------------------------------------------------------------------


class ApiRegistryRow(Base):
    """Policy per upstream.

    Key fields per Sec. 7.6: `api_key, base_url, criticality, timeout,
    owner`. The remaining columns carry the rest of P1's `ApiPolicy`, because
    a policy without its backoff and breaker numbers cannot actually drive
    the protector - P2 reads these.

    The threshold values are not invented. Each is copied from the tuning
    table in Sec. 11.4, with its citation in `notes/README.md` Sec. 1. A
    judge asking "why 25%?" gets a citation rather than a shrug.

    `ApiPolicy` is `frozen=True`, so there is deliberately no `updated_at`
    and no update path - to change a policy, insert a new row.
    """

    __tablename__ = "api_registry"

    api_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)

    # Sec. 7.6 calls this `timeout`; P1's frozen ApiPolicy calls it
    # `timeout_s`, so we keep the Pydantic name.
    timeout_s: Mapped[float] = mapped_column(Float, nullable=False, default=3.0,
                                          server_default="3.0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3,
                                           server_default="3")

    # Sec. 11.4: BACKOFF_INIT=0.075; BACKOFF_MAX=1.8; JITTER=0.05.
    # The cap is what stops attempt 10 waiting ~77s after the customer has
    # already given up.
    backoff_initial_s: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.075, server_default="0.075"
    )
    backoff_max_s: Mapped[float] = mapped_column(Float, nullable=False, default=1.8,
                                              server_default="1.8")
    jitter_s: Mapped[float] = mapped_column(Float, nullable=False, default=0.05,
                                        server_default="0.05")

    # Sec. 11.4: WINDOW=100; ERROR_PCT=25; VOLUME_MIN=20; SLEEP=10;
    # PROBE_RATE='10/5s'. Five breaker parameters from Falahah et al. 2021
    # Sec. 3 (citing Aquino et al. 2019 + Richardson 2018).
    breaker_window: Mapped[int] = mapped_column(Integer, nullable=False, default=100,
                                             server_default="100")
    breaker_error_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.25, server_default="0.25"
    )
    breaker_min_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=20,
                                                 server_default="20")
    breaker_sleep_s: Mapped[float] = mapped_column(Float, nullable=False, default=10.0,
                                                server_default="10.0")
    half_open_probes: Mapped[int] = mapped_column(Integer, nullable=False, default=10,
                                               server_default="10")
    half_open_window_s: Mapped[float] = mapped_column(
        Float, nullable=False, default=5.0, server_default="5.0")

    # Whether retrying is safe. GET is safe; a write is not. An explicit
    # per-operation flag, never inferred from the HTTP method -
    # `notes/README.md` Sec. 4 records why idempotence is not safety (51% of
    # real operations are state-changing, Tan et al. 2026 Table I).
    idempotent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                          server_default=true())
    criticality: Mapped[str] = mapped_column(
        String(16), nullable=False, default="medium", server_default="medium"
    )

    # Sec. 4: per-dependency bulkhead pool (Sec. 7 "Max Concurrent Request"
    # is breaker parameter 2 of 5). Sec. 10 requires Nominatim <= 1 rps.
    bulkhead_max_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, default=20, server_default="20"
    )
    courtesy_rps: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Sec. 7.6 lists `owner`. Not enforced anywhere - it exists so a judge
    # can see which upstream belongs to whom.
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("max_attempts >= 1", name="ck_api_registry_max_attempts"),
        CheckConstraint("timeout_s > 0", name="ck_api_registry_timeout_positive"),
        CheckConstraint(
            "breaker_error_threshold > 0 AND breaker_error_threshold <= 1",
            name="ck_api_registry_threshold_range",
        ),
        # Closes gap L-04: ApiPolicy silently accepts backoff_max_s <
        # backoff_initial_s, so every delay clamps to max.
        CheckConstraint(
            "backoff_max_s >= backoff_initial_s",
            name="ck_api_registry_backoff_order",
        ),
        CheckConstraint("bulkhead_max_concurrency >= 1",
                        name="ck_api_registry_bulkhead_positive"),
        CheckConstraint(
            _in_list("criticality", CRITICALITY_VALUES),
            name="ck_api_registry_criticality",
        ),
    )


# ---------------------------------------------------------------------------
# breaker_transitions                              [Doc Sec. 7.6]
# ---------------------------------------------------------------------------


class BreakerTransitionRow(Base):
    """Circuit-breaker state changes - timeline and audit.

    Key fields per Sec. 7.6: `ts, api_key, from_state, to_state, error_pct,
    reason`. This table was missing from my first cut, which was a real gap:
    without it there is no source for PRD Sec. 5's "Open Circuit Breakers
    Count" panel, its breaker timeline, or `GET /breaker/state`'s
    `errorPct` (Sec. 7.7).

    **Aditi (P2) writes here** as the breaker flips. That handoff should
    happen before she builds, so she is not inventing her own audit shape.

    One row per transition, never updated - a history that can be edited is
    not an audit trail.
    """

    __tablename__ = "breaker_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Sec. 7.6 `ts`. Written explicitly rather than relying solely on
    # created_at, because ordering transitions is the whole point of this
    # table and a timeline that depends on tie-breaking is not a timeline.
    ts: Mapped[dt.datetime] = mapped_column(nullable=False, server_default=func.now())

    api_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # CLOSED / OPEN / HALF_OPEN. `from_state` is NULL only for the very
    # first CLOSED entry when a breaker is registered.
    from_state: Mapped[str | None] = mapped_column(String(_ENUM_LEN), nullable=True)
    to_state: Mapped[str] = mapped_column(String(_ENUM_LEN), nullable=False)

    # Sec. 7.7 returns `errorPct` from this column. Stored as a percentage
    # 0-100, matching the doc's `error_pct` naming and the
    # `error_threshold_pct = 25` config in Sec. 4.4.
    error_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Why it flipped. Free text is correct here: reasons are human-facing
    # ("sustained 503 above 25% over 100 requests"), not queryable values.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "from_state IS NULL OR " + _in_list("from_state", BREAKER_STATE_VALUES),
            name="ck_breaker_from_state",
        ),
        CheckConstraint(_in_list("to_state", BREAKER_STATE_VALUES),
                        name="ck_breaker_to_state"),
        CheckConstraint("error_pct IS NULL OR (error_pct >= 0 AND error_pct <= 100)",
                        name="ck_breaker_error_pct_range"),
        # A transition that does not change anything is a bug in the caller,
        # not a valid row.
        CheckConstraint("from_state IS NULL OR from_state <> to_state",
                        name="ck_breaker_state_actually_changed"),
        # The timeline query: newest-first per api.
        Index("ix_breaker_ts_api", "ts", "api_key"),
    )


# ---------------------------------------------------------------------------
# request_logs  <- backend.schemas.EvidenceEvent    [Doc Sec. 7.6 + 7.3]
# ---------------------------------------------------------------------------


class RequestLogRow(Base):
    """Structured-log mirror - one row per observed step.

    Key fields per Sec. 7.6: `ts, traceId, api, latency, status, attempt,
    breaker`. Sec. 7.3 gives the full JSON log line whose fields are
    required: `ts, traceId, phase, occurrence k, attempt, status,
    latencyMs, breaker state/transition, guard, mode`.

    Plus the five columns P1's scorer reads, without which no drill can ever
    be re-scored:
    `effect_applied`, `leaked_raw_error`, `served_from`, `fault`,
    `call_index`.

    Rows are append-only. Editing one would rewrite the evidence a verdict
    rests on, which is precisely what event sourcing exists to prevent.
    """

    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Sec. 7.6 `ts` / Sec. 7.3 `traceId`. W3C-compatible: it correlates with
    # whatever the protector emitted, so a trace can be reassembled across
    # services. Indexed first because "show me this trace" dominates queries.
    ts: Mapped[dt.datetime] = mapped_column(nullable=False, server_default=func.now())
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    # Sec. 7.3 `api` - the short key, e.g. 'open-meteo'. NOT an auth key.
    api_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # Sec. 7.3 `phase` req/resp. P1's richer vocabulary keeps the bookkeeping
    # stages too, which is what lets the scorer distinguish "sent" from
    # "committed" from "answered".
    phase: Mapped[str] = mapped_column(String(_ENUM_LEN), nullable=False)

    # Sec. 7.3 `k` - which matching op this was.
    occurrence: Mapped[int] = mapped_column(Integer, nullable=False)

    # Sec. 7.6 `attempt`, Sec. 7.3 `attempt`. Retry attempt number,
    # **1-based**, matching P2's `resilient_get`, which tracks
    # `attempt_1based = attempt_i + 1` and logs `attempt=1` for the first
    # try. This is deliberately NOT `ApiPolicy.backoff_delay_s(attempt)`,
    # which is 0-based: the backoff formula counts retries, whereas this
    # column counts attempts, as the Sec. 7.3 log line does.
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Sec. 7.6 `status`, Sec. 7.3 `status`.
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Sec. 7.6 `latency`, Sec. 7.3 `latencyMs`. Nullable in the schema but
    # worth storing properly: it is the only way to recover true timeline
    # ordering once concurrency lands (gap L-01, `notes/README.md` Sec. 12).
    # Backfilling later is painful.
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Sec. 7.6 `breaker`, Sec. 7.3 `breaker` state. P2 sets this on every
    # result, alongside the `X-Breaker-State` header (Sec. 4.4).
    breaker_state: Mapped[str | None] = mapped_column(
        String(_ENUM_LEN), nullable=True
    )

    # Sec. 7.3 `guard` - the After-guard as a readable string, e.g.
    # "After(recv_geocode#1)". Not queryable, but it is in the required log
    # line and it is what makes the log explain itself.
    guard: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Sec. 7.3 `mode` control/experiment. P4's traffic splitter writes it;
    # the control-vs-experiment comparison (Sec. 5.3) is computed from it.
    mode: Mapped[str | None] = mapped_column(String(_ENUM_LEN), nullable=True)

    # --- the five load-bearing columns ---------------------------------
    # Read directly by backend/scoring.py. Drop or rename any of these and
    # no historical run can be re-scored.
    effect_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                              server_default=false())
    leaked_raw_error: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    served_from: Mapped[str] = mapped_column(
        String(_ENUM_LEN), nullable=False, default="none", server_default="none"
    )
    fault: Mapped[str | None] = mapped_column(String(_ENUM_LEN), nullable=True)
    # Counts CALLS, advancing only on SEND. Distinct from `occurrence`,
    # which counts ROWS. One call logs a failed attempt AND a fallback, so
    # reading "the 3rd call" off a row counter would silently drift.
    call_index: Mapped[int] = mapped_column(Integer, nullable=False, default=1,
                                         server_default="1")
    # ---------------------------------------------------------------------

    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Both counters are 1-based in P1 (`ge=1`), so a 0 is always a bug.
        CheckConstraint("occurrence >= 1", name="ck_request_logs_occurrence_positive"),
        CheckConstraint("call_index >= 1", name="ck_request_logs_call_index_positive"),
        # 1-based, matching P2's resilient_get (attempt_1based). An attempt
        # count of 0 is always a bug: the first try is attempt 1.
        CheckConstraint("attempt IS NULL OR attempt >= 1",
                        name="ck_request_logs_attempt_positive"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0",
                        name="ck_request_logs_latency_non_negative"),
        CheckConstraint(_in_list("phase", PHASE_VALUES), name="ck_request_logs_phase"),
        CheckConstraint(_in_list("served_from", SERVED_FROM_VALUES),
                        name="ck_request_logs_served_from"),
        CheckConstraint("fault IS NULL OR " + _in_list("fault", FAULT_VALUES),
                        name="ck_request_logs_fault"),
        CheckConstraint(
            "breaker_state IS NULL OR "
            + _in_list("breaker_state", BREAKER_STATE_VALUES),
            name="ck_request_logs_breaker_state",
        ),
        CheckConstraint("mode IS NULL OR " + _in_list("mode", MODE_VALUES),
                        name="ck_request_logs_mode"),
        # The lookup both the scorer and the dashboard perform: every row
        # for one call, in order.
        Index("ix_request_logs_trace_api_call", "trace_id", "api_key", "call_index"),
        # Supports the control-vs-experiment split without a table scan.
        Index("ix_request_logs_mode", "mode"),
    )


# ---------------------------------------------------------------------------
# fi_runs  <- backend.schemas.DrillSpec + ScoreResult [Doc Sec. 7.6]
# ---------------------------------------------------------------------------


class FiRunRow(Base):
    """The proof log: one row per drill run.

    Key fields per Sec. 7.6: `run_id, pattern, target_api/k/phase, guard,
    TS, CW/PS/Prem/Miss/Mult`.

    **The score columns are nullable.** This is deliberate and it is how the
    PRD's single-table shape stays correct: the spec is written when the run
    *starts*, so if a run crashes mid-flight we still hold the intent. A row
    with `ts IS NULL` means "started, never finished" - which is real
    information, not a gap.

    `ts` is Temporal Success, the headline:
    `CW AND PS AND NOT Prem AND NOT Miss AND NOT Mult`. Strict conjunction
    on purpose - one duplicated action loses the whole claim, which is the
    correct bias for a tool whose output is used to make reliability claims
    (Tan et al. 2026 Sec. VI-B).
    """

    __tablename__ = "fi_runs"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    pattern: Mapped[str] = mapped_column(String(_ENUM_LEN), nullable=False)

    # The trace this run's evidence was recorded under, so a stored run can
    # find its own `request_logs` rows without the caller already knowing the
    # trace id. Additive and nullable: the column was added during review
    # because without it `load_run()` and `load_evidence()` were unlinkable
    # (gap A2 in the review), and `request_logs` has no `run_id` either.
    trace_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )

    # Flattened from FaultTarget so the pairing can be queried across runs.
    # Sec. 7.6 spells this `target_api/k/phase`.
    target_api: Mapped[str] = mapped_column(String(64), nullable=False)
    target_k: Mapped[int] = mapped_column(Integer, nullable=False)
    target_phase: Mapped[str] = mapped_column(String(_ENUM_LEN), nullable=False)

    # Sec. 7.6 `guard`. Both flattened columns and the verbatim spec are
    # stored, so intent survives a change to this table's layout.
    guard_api: Mapped[str | None] = mapped_column(String(64), nullable=True)
    guard_phase: Mapped[str | None] = mapped_column(String(_ENUM_LEN), nullable=True)
    guard_min_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    fault: Mapped[str] = mapped_column(String(_ENUM_LEN), nullable=False)
    idempotent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                              server_default=true())

    # "n" in k-of-n: how many repeated calls the drill watches.
    total_occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=4,
                                                server_default="4")

    # --- the verdict ----------------------------------------------------
    # Nullable on purpose: see the class docstring. The CHECK constraint
    # below still guarantees that a COMPLETE verdict is self-consistent.
    ts: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    cw: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ps: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    prem: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    miss: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mult: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # -------------------------------------------------------------------

    # The snapshot that lets any historical run be re-explained without
    # re-running it. Stored, not reconstructed from request_logs - a pass is
    # only meaningful next to the evidence behind it.
    timeline: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    spec: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Plain-English reasons: a score without reasons is a red number nobody
    # can act on.
    notes: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("target_k >= 1", name="ck_fi_runs_target_k"),
        CheckConstraint("total_occurrences >= 1", name="ck_fi_runs_total_occurrences"),
        CheckConstraint(
            "guard_min_count IS NULL OR guard_min_count >= 1",
            name="ck_fi_runs_guard_min_count",
        ),
        CheckConstraint(_in_list("pattern", PATTERN_VALUES), name="ck_fi_runs_pattern"),
        CheckConstraint(_in_list("fault", FAULT_VALUES), name="ck_fi_runs_fault"),
        CheckConstraint(_in_list("target_phase", PHASE_VALUES),
                        name="ck_fi_runs_target_phase"),
        # A FINISHED run must be self-consistent. `ts IS NOT NULL` means the
        # run completed, and then the conjunction is mandatory - so no future
        # writer can store a self-inconsistent verdict, which is what makes
        # a stored score auditable. An unfinished row is exempt by design.
        CheckConstraint(
            "ts IS NULL OR ts = (cw AND ps AND NOT prem AND NOT miss AND NOT mult)",
            name="ck_fi_runs_ts_consistent",
        ),
        # If ts is present every component must be too. A half-written
        # verdict is worse than no verdict.
        CheckConstraint(
            "ts IS NULL OR (cw IS NOT NULL AND ps IS NOT NULL "
            "AND prem IS NOT NULL AND miss IS NOT NULL AND mult IS NOT NULL)",
            name="ck_fi_runs_flags_complete",
        ),
    )