"""Who is meant to annotate which task.

"Assigned" used to mean every gym task carrying `meta.inEightyFive`, so every
annotator saw the same 85 rows and every quota target was 85 — with several
people on the batch, nobody could tell what was theirs and the progress bar
measured the cohort rather than the person.

Keyed on (task, annotator) rather than on the task, because two annotators on one
task is deliberate here: overlap is what the QA agreement maths is measuring.

Revision ID: c1d2e3f4a5b6
Revises: a8b9c0d1e2f3
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_assignment",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("task.id", ondelete="CASCADE"), nullable=False),
        sa.Column("annotator_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("annotator.id", ondelete="CASCADE"), nullable=False),
        sa.Column("batch", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="assigned"),
        sa.Column("assigned_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("annotator.id", ondelete="SET NULL"), nullable=True),
        sa.Column("assigned_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "annotator_id", name="uq_task_assignment_task_annotator"),
    )
    op.create_index("ix_task_assignment_annotator", "task_assignment", ["annotator_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_task_assignment_annotator", table_name="task_assignment")
    op.drop_table("task_assignment")
