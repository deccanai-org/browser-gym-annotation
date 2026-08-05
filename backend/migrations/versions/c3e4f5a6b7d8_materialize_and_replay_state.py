"""Steps appear as the annotator works, and carry whether they were proven.

Two things, both needed for a human-driven trajectory:

* `trajectory_version.materialized_through_seq` — how far the raw event stream
  has been folded into steps. Advanced in the SAME transaction as the steps it
  produced, so re-running the folder is a no-op rather than a second copy. There
  is no Commit button to double-click, which is the point.

* `trajectory_step.replay_state` / `replay_error` — whether a step has been
  proven to replay. Recording a step and PROVING it are separate: the old
  `/commit` had to restore a checkpoint into the live environment, which destroys
  the annotator's working state, so it could only ever run once, at the end, and
  a single bad action threw the whole sequence away. Certification runs in a
  scratch world instead, as often as you like, and marks steps rather than
  deleting them.

Revision ID: c3e4f5a6b7d8
Revises: b2d3f4a5c6e7
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3e4f5a6b7d8"
down_revision = "b2d3f4a5c6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trajectory_version",
                  sa.Column("materialized_through_seq", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("trajectory_step",
                  sa.Column("replay_state", sa.String(16), nullable=False, server_default="unverified"))
    op.add_column("trajectory_step", sa.Column("replay_error", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("trajectory_step", "replay_error")
    op.drop_column("trajectory_step", "replay_state")
    op.drop_column("trajectory_version", "materialized_through_seq")
