"""Initial JIZO schema - four tables (owner: Aayush, P3).

Table names and key fields follow AI-Build Documentation Sec. 7.6 verbatim,
which the PRD Sec. 4 repeats:

    api_registry         - policy per upstream
    breaker_transitions  - breaker timeline + audit
    fi_runs              - proof log (TS + CW/PS/Prem/Miss/Mult)
    request_logs         - structured-log mirror

Two choices encoded here that are easy to lose later:

1. **Enums are VARCHAR, not native Postgres ENUM.** Adding a fault type then
   needs no migration at all - just new data. The CHECK constraints keep the
   vocabulary honest, so a typo is still rejected.

2. **`fi_runs` score columns are nullable, and the TS CHECK only fires on a
   complete verdict.** `ts IS NULL` means "started, never finished" - which
   is real information, because the spec was written when the run began. If
   a run crashes mid-flight we still hold the intent. When `ts IS NOT NULL`
   the conjunction is mandatory, so no writer can store a self-inconsistent
   verdict.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-05
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENUM_LEN = 24

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
MODE_VALUES = ("control", "experiment")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    options = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({options})"


def upgrade() -> None:
    # -- api_registry  [Sec. 7.6] ---------------------------------------
    op.create_table(
        "api_registry",
        sa.Column("api_key", sa.String(64), primary_key=True, nullable=False),
        sa.Column("base_url", sa.String(512), nullable=False),
        # Sec. 7.6 writes `timeout`; P1's frozen ApiPolicy writes `timeout_s`.
        # The Pydantic name is kept so the mapping is mechanical.
        sa.Column("timeout_s", sa.Float(), nullable=False, server_default="3.0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("backoff_initial_s", sa.Float(), nullable=False,
                  server_default="0.075"),
        sa.Column("backoff_max_s", sa.Float(), nullable=False, server_default="1.8"),
        sa.Column("jitter_s", sa.Float(), nullable=False, server_default="0.05"),
        sa.Column("breaker_window", sa.Integer(), nullable=False,
                  server_default="100"),
        sa.Column("breaker_error_threshold", sa.Float(), nullable=False,
                  server_default="0.25"),
        sa.Column("breaker_min_volume", sa.Integer(), nullable=False,
                  server_default="20"),
        sa.Column("breaker_sleep_s", sa.Float(), nullable=False,
                  server_default="10.0"),
        sa.Column("half_open_probes", sa.Integer(), nullable=False,
                  server_default="10"),
        sa.Column("half_open_window_s", sa.Float(), nullable=False,
                  server_default="5.0"),
        sa.Column("idempotent", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("criticality", sa.String(16), nullable=False,
                  server_default="medium"),
        sa.Column("bulkhead_max_concurrency", sa.Integer(), nullable=False,
                  server_default="20"),
        sa.Column("courtesy_rps", sa.Float(), nullable=True),
        # Sec. 7.6 lists `owner`. Not enforced - it exists so a judge can see
        # which upstream belongs to whom.
        sa.Column("owner", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("max_attempts >= 1",
                           name="ck_api_registry_max_attempts"),
        sa.CheckConstraint("timeout_s > 0", name="ck_api_registry_timeout_positive"),
        sa.CheckConstraint(
            "breaker_error_threshold > 0 AND breaker_error_threshold <= 1",
            name="ck_api_registry_threshold_range",
        ),
        # Closes gap L-04: ApiPolicy silently accepts backoff_max_s <
        # backoff_initial_s, so every delay clamps to max.
        sa.CheckConstraint("backoff_max_s >= backoff_initial_s",
                           name="ck_api_registry_backoff_order"),
        sa.CheckConstraint("bulkhead_max_concurrency >= 1",
                           name="ck_api_registry_bulkhead_positive"),
        sa.CheckConstraint(_in_list("criticality", CRITICALITY_VALUES),
                           name="ck_api_registry_criticality"),
    )

    # -- breaker_transitions  [Sec. 7.6] --------------------------------
    op.create_table(
        "breaker_transitions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Sec. 7.6 `ts`. Written explicitly rather than relying only on
        # created_at, because ordering transitions is the point of the table.
        sa.Column("ts", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("api_key", sa.String(64), nullable=False),
        # NULL only for the first CLOSED entry, when a breaker is registered.
        sa.Column("from_state", sa.String(_ENUM_LEN), nullable=True),
        sa.Column("to_state", sa.String(_ENUM_LEN), nullable=False),
        # Sec. 7.7 returns this as `errorPct`. Stored 0-100, matching
        # `error_threshold_pct = 25` in Sec. 4.4.
        sa.Column("error_pct", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "from_state IS NULL OR " + _in_list("from_state", BREAKER_STATE_VALUES),
            name="ck_breaker_from_state",
        ),
        sa.CheckConstraint(_in_list("to_state", BREAKER_STATE_VALUES),
                           name="ck_breaker_to_state"),
        sa.CheckConstraint(
            "error_pct IS NULL OR (error_pct >= 0 AND error_pct <= 100)",
            name="ck_breaker_error_pct_range",
        ),
        # A "transition" that changes nothing is a caller bug, not a row.
        sa.CheckConstraint("from_state IS NULL OR from_state <> to_state",
                           name="ck_breaker_state_actually_changed"),
    )
    op.create_index("ix_breaker_transitions_api_key", "breaker_transitions",
                    ["api_key"])
    op.create_index("ix_breaker_ts_api", "breaker_transitions", ["ts", "api_key"])

    # -- request_logs  [Sec. 7.6 + Sec. 7.3] ----------------------------
    op.create_table(
        "request_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("api_key", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(_ENUM_LEN), nullable=False),
        # Sec. 7.3 `k`.
        sa.Column("occurrence", sa.Integer(), nullable=False),
        # Sec. 7.6 + 7.3 `attempt`. 1-based, matching P2's resilient_get
        # (`attempt_1based`), NOT backoff_delay_s's 0-based retry count.
        sa.Column("attempt", sa.Integer(), nullable=True),
        # Sec. 7.6 `status`, Sec. 7.3 `status`.
        sa.Column("status_code", sa.Integer(), nullable=True),
        # Sec. 7.6 `latency`, Sec. 7.3 `latencyMs`. Nullable in the schema
        # but worth storing properly: it is the only way to recover true
        # timeline ordering once concurrency lands (gap L-01).
        sa.Column("latency_ms", sa.Float(), nullable=True),
        # Sec. 7.6 + 7.3 `breaker` state.
        sa.Column("breaker_state", sa.String(_ENUM_LEN), nullable=True),
        # Sec. 7.3 `guard`, e.g. "After(recv_geocode#1)".
        sa.Column("guard", sa.Text(), nullable=True),
        # Sec. 7.3 `mode` control/experiment - the ChAP traffic-splitter tag
        # the control-vs-experiment comparison is computed from.
        sa.Column("mode", sa.String(_ENUM_LEN), nullable=True),
        # --- the five load-bearing columns -------------------------------
        # Read directly by backend/scoring.py. Without these no historical
        # run can be re-scored, so they are a superset of Sec. 7.6's
        # abbreviated field list.
        sa.Column("effect_applied", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("leaked_raw_error", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("served_from", sa.String(_ENUM_LEN), nullable=False,
                  server_default="none"),
        sa.Column("fault", sa.String(_ENUM_LEN), nullable=True),
        # Counts CALLS, advancing only on SEND. Distinct from `occurrence`,
        # which counts ROWS.
        sa.Column("call_index", sa.Integer(), nullable=False, server_default="1"),
        # -----------------------------------------------------------------
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint("occurrence >= 1",
                           name="ck_request_logs_occurrence_positive"),
        sa.CheckConstraint("call_index >= 1",
                           name="ck_request_logs_call_index_positive"),
        sa.CheckConstraint("attempt IS NULL OR attempt >= 1",
                           name="ck_request_logs_attempt_positive"),
        sa.CheckConstraint("latency_ms IS NULL OR latency_ms >= 0",
                           name="ck_request_logs_latency_non_negative"),
        sa.CheckConstraint(_in_list("phase", PHASE_VALUES),
                           name="ck_request_logs_phase"),
        sa.CheckConstraint(_in_list("served_from", SERVED_FROM_VALUES),
                           name="ck_request_logs_served_from"),
        sa.CheckConstraint("fault IS NULL OR " + _in_list("fault", FAULT_VALUES),
                           name="ck_request_logs_fault"),
        sa.CheckConstraint(
            "breaker_state IS NULL OR "
            + _in_list("breaker_state", BREAKER_STATE_VALUES),
            name="ck_request_logs_breaker_state",
        ),
        sa.CheckConstraint("mode IS NULL OR " + _in_list("mode", MODE_VALUES),
                           name="ck_request_logs_mode"),
    )
    op.create_index("ix_request_logs_trace_id", "request_logs", ["trace_id"])
    # The lookup both the scorer and the dashboard perform.
    op.create_index("ix_request_logs_trace_api_call", "request_logs",
                    ["trace_id", "api_key", "call_index"])
    op.create_index("ix_request_logs_mode", "request_logs", ["mode"])

    # -- fi_runs  [Sec. 7.6] --------------------------------------------
    op.create_table(
        "fi_runs",
        sa.Column("run_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("pattern", sa.String(_ENUM_LEN), nullable=False),
        # Sec. 7.6 spells this `target_api/k/phase`.
        sa.Column("target_api", sa.String(64), nullable=False),
        sa.Column("target_k", sa.Integer(), nullable=False),
        sa.Column("target_phase", sa.String(_ENUM_LEN), nullable=False),
        sa.Column("guard_api", sa.String(64), nullable=True),
        sa.Column("guard_phase", sa.String(_ENUM_LEN), nullable=True),
        sa.Column("guard_min_count", sa.Integer(), nullable=True),
        sa.Column("fault", sa.String(_ENUM_LEN), nullable=False),
        sa.Column("idempotent", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("total_occurrences", sa.Integer(), nullable=False,
                  server_default="4"),
        # --- the verdict: nullable on purpose ---------------------------
        # Spec is written when the run STARTS, so a run that crashes
        # mid-flight still holds its intent. ts IS NULL means exactly that.
        sa.Column("ts", sa.Boolean(), nullable=True),
        sa.Column("cw", sa.Boolean(), nullable=True),
        sa.Column("ps", sa.Boolean(), nullable=True),
        sa.Column("prem", sa.Boolean(), nullable=True),
        sa.Column("miss", sa.Boolean(), nullable=True),
        sa.Column("mult", sa.Boolean(), nullable=True),
        # ---------------------------------------------------------------
        # Stored, not reconstructed from request_logs: a pass is only
        # meaningful next to the evidence behind it.
        sa.Column("timeline", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("target_k >= 1", name="ck_fi_runs_target_k"),
        sa.CheckConstraint("total_occurrences >= 1",
                           name="ck_fi_runs_total_occurrences"),
        sa.CheckConstraint("guard_min_count IS NULL OR guard_min_count >= 1",
                           name="ck_fi_runs_guard_min_count"),
        sa.CheckConstraint(_in_list("pattern", PATTERN_VALUES),
                           name="ck_fi_runs_pattern"),
        sa.CheckConstraint(_in_list("fault", FAULT_VALUES),
                           name="ck_fi_runs_fault"),
        sa.CheckConstraint(_in_list("target_phase", PHASE_VALUES),
                           name="ck_fi_runs_target_phase"),
        # TS = CW AND PS AND NOT Prem AND NOT Miss AND NOT Mult
        # (Tan et al. 2026 Sec. VI-B). Enforced whenever the run completed.
        sa.CheckConstraint(
            "ts IS NULL OR ts = (cw AND ps AND NOT prem AND NOT miss AND NOT mult)",
            name="ck_fi_runs_ts_consistent",
        ),
        # A half-written verdict is worse than no verdict.
        sa.CheckConstraint(
            "ts IS NULL OR (cw IS NOT NULL AND ps IS NOT NULL "
            "AND prem IS NOT NULL AND miss IS NOT NULL AND mult IS NOT NULL)",
            name="ck_fi_runs_flags_complete",
        ),
    )
    op.create_index("ix_fi_runs_ts", "fi_runs", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_fi_runs_ts", table_name="fi_runs")
    op.drop_table("fi_runs")
    op.drop_index("ix_request_logs_mode", table_name="request_logs")
    op.drop_index("ix_request_logs_trace_api_call", table_name="request_logs")
    op.drop_index("ix_request_logs_trace_id", table_name="request_logs")
    op.drop_table("request_logs")
    op.drop_index("ix_breaker_ts_api", table_name="breaker_transitions")
    op.drop_index("ix_breaker_transitions_api_key", table_name="breaker_transitions")
    op.drop_table("breaker_transitions")
    op.drop_table("api_registry")