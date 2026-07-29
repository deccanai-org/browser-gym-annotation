"""bind a task's canonical run instead of guessing it

Which recorded run a gym task is annotated against was decided by "the oldest gym
trajectory carrying a replay payload", copied into four modules. Per-step world
capture landed in the gym harness after some runs were already recorded, so for
tasks like M40_bogus_pricematch and M76_ambiguous_subscription_cancel the oldest
run is the one with NO world trail — and a canonical run with no world trail
cannot be forked from at all, silently. This table makes canonical a decision a
reviewer records (one row per task) rather than a property of insertion order.

The trajectory FK cascades on delete: if the bound run is removed the decision
goes with it and resolution falls back, rather than leaving a binding that points
at a row which no longer exists.

NOTE for autogenerate: the model lives in app/canonical.py, next to the resolver
that reads it. migrations/env.py currently imports only app.models, so it must
also import app.canonical before any `alembic revision --autogenerate`, or the
diff will propose dropping this table.

Revision ID: c9d0e1f2a3b4
Revises: 2a9c17f4bd30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "2a9c17f4bd30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "canonical_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("trajectory_id", sa.Uuid(), nullable=False),
        sa.Column("bound_by_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trajectory_id"], ["trajectory.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bound_by_id"], ["annotator.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        # One binding per task: a second row would put the drift straight back.
        sa.UniqueConstraint("task_id", name="uq_canonical_run_task"),
    )
    op.create_index(op.f("ix_canonical_run_task_id"), "canonical_run", ["task_id"])
    op.create_index(op.f("ix_canonical_run_trajectory_id"), "canonical_run", ["trajectory_id"])
    op.create_index(op.f("ix_canonical_run_bound_by_id"), "canonical_run", ["bound_by_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_canonical_run_bound_by_id"), table_name="canonical_run")
    op.drop_index(op.f("ix_canonical_run_trajectory_id"), table_name="canonical_run")
    op.drop_index(op.f("ix_canonical_run_task_id"), table_name="canonical_run")
    op.drop_table("canonical_run")
