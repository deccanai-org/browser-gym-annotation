"""record what a workspace's gym was seeded with

A workspace container outlives the live browser attached to it, so reopening a
pane must not re-seed a world the annotator has been building by hand. Skipping
that reset is only safe against a DURABLE record of what the gym holds — process
memory cannot answer it, because a backend restart is exactly one of the events
that drops the attachment while the container keeps running.

seeded_seed is a nullable Integer rather than a defaulted one so that "seeded
with seed 0" stays distinguishable from "never seeded" — seed 0 is the common
case, so collapsing the two would make the marker useless precisely where it
matters most.

Revision ID: b9c0d1e2f3a4
Revises: d1e2f3a4b5c6
Create Date: 2026-07-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b9c0d1e2f3a4"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # All nullable: every lease that predates this migration is correctly
    # "never seeded", which fails closed to a reset.
    op.add_column("workspace_lease", sa.Column("seeded_task_key", sa.Text(), nullable=True))
    op.add_column("workspace_lease", sa.Column("seeded_task_id", sa.Text(), nullable=True))
    op.add_column("workspace_lease", sa.Column("seeded_seed", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("workspace_lease", "seeded_seed")
    op.drop_column("workspace_lease", "seeded_task_id")
    op.drop_column("workspace_lease", "seeded_task_key")
