"""Link a drill run to the trace that holds its evidence.

Owner: Aayush (P3).

Adds `fi_runs.trace_id`.

## Why

`fi_runs` and `request_logs` had no link in either direction: `fi_runs` had
no `trace_id`, and `request_logs` has no `run_id`. So:

* P4 could not find a run's evidence without already knowing the trace id
  from somewhere else, and
* deleting a `fi_runs` row left its `request_logs` rows orphaned and
  undiscoverable, which then duplicated on re-save.

Both were found during review (finding A2). A nullable `trace_id` closes the
first; `store.save_run`'s evidence-existence check closes the second.

## Why nullable

Existing rows have no trace recorded and we cannot invent one, so the column
must be optional for the upgrade to apply to a populated table. New rows get
it set automatically by `save_run` from the events it is given.

Revision ID: 0002_fi_runs_trace_id
Revises: 0001_initial
Create Date: 2026-10-06
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_fi_runs_trace_id"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "fi_runs",
        sa.Column("trace_id", sa.String(128), nullable=True),
    )
    op.create_index("ix_fi_runs_trace_id", "fi_runs", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_fi_runs_trace_id", table_name="fi_runs")
    op.drop_column("fi_runs", "trace_id")
