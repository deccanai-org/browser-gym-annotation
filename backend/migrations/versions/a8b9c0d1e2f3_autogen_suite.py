"""Cache the generated verifier suite per (task, seed).

The oracle loop resets the gym, runs the oracle agent, and iterates a reward
model until the suite scores 0 on the initial world and 1 on the golden one —
then returned it as JSON and kept nothing. Every annotator on the same breaker
paid for the whole run again, and the result reached no attempt at all, so step
2 of the review screen has been decorative for the entire pilot.

Keyed on (task, seed) because that is what the suite is a property of: the same
task at the same seed has the same two worlds, so the checks that discriminate
them are the same checks.

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, Sequence[str], None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "autogen_suite",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("task.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("oracle", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("brief", sa.Text(), nullable=False, server_default=""),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("gate", sa.JSON(), nullable=True),
        sa.Column("iterations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "seed", name="uq_autogen_suite_task_seed"),
    )


def downgrade() -> None:
    op.drop_table("autogen_suite")
