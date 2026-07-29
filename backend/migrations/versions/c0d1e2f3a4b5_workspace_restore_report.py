"""record which version a workspace holds and how far a fork's prefix was rebuilt into a workspace's world

A fork's live world is now rebuilt to the fork point by replaying the branch
prefix. That replay is best-effort — finalize and commit are the strict gates —
so how far it got is something the annotator has to be able to see, and it has to
survive the backend restart that drops the pane attachment but not the container.

restore_total NULL means no rebuild was attempted, which is the ordinary case for
an unforked attempt and is distinct from "attempted and rebuilt nothing".

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-07-24 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c0d1e2f3a4b5"
down_revision: Union[str, Sequence[str], None] = "b9c0d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspace_lease",
        sa.Column(
            "seeded_version_id",
            sa.Uuid(),
            sa.ForeignKey("trajectory_version.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("workspace_lease", sa.Column("restore_done", sa.Integer(), nullable=True))
    op.add_column("workspace_lease", sa.Column("restore_total", sa.Integer(), nullable=True))
    op.add_column("workspace_lease", sa.Column("restore_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("workspace_lease", "seeded_version_id")
    op.drop_column("workspace_lease", "restore_reason")
    op.drop_column("workspace_lease", "restore_total")
    op.drop_column("workspace_lease", "restore_done")
