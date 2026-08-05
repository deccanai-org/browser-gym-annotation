"""Persist the world a bridged (realistic-UI) attempt owns.

Until now the per-attempt mock SIDs lived only in process memory (`_ATTACHED` in
api/live.py), so a backend restart orphaned the annotator's world irrecoverably —
and nothing could tell a bridged attempt from a workspace one, which is why
bridged attempts silently fell through to the SHARED gym in `endpoint_for`.

Also carries the two checkpoints Feature 2 scores against (the seeded start world
and the world the annotator's own work produced) and the `human_do` mode flag.

All columns are nullable/defaulted, so existing rows are untouched.

Revision ID: a1c2e3b4d5f6
Revises: c0d1e2f3a4b5
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1c2e3b4d5f6"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_session", sa.Column("mode", sa.String(16), nullable=False,
                                              server_default="agent_review"))
    op.add_column("review_session", sa.Column("prompt_override", sa.Text(), nullable=False,
                                              server_default=""))
    op.add_column("review_session", sa.Column("bridge_session_id", sa.String(64), nullable=False,
                                              server_default=""))
    op.add_column("review_session", sa.Column("bridge_gym_url", sa.String(255), nullable=False,
                                              server_default=""))
    op.add_column("review_session", sa.Column("cua_apps", sa.JSON(), nullable=True))
    op.add_column("review_session", sa.Column("started_at", sa.DateTime(), nullable=True))
    op.add_column("review_session", sa.Column("finished_at", sa.DateTime(), nullable=True))
    op.add_column("review_session", sa.Column("initial_checkpoint_id", sa.Uuid(), nullable=True))
    op.add_column("review_session", sa.Column("final_checkpoint_id", sa.Uuid(), nullable=True))
    # environment_checkpoint.attempt_id already references review_session, so these
    # close a cycle — created as named constraints (ALTER) for exactly that reason.
    op.create_foreign_key("fk_review_session_initial_checkpoint", "review_session",
                          "environment_checkpoint", ["initial_checkpoint_id"], ["id"],
                          ondelete="SET NULL")
    op.create_foreign_key("fk_review_session_final_checkpoint", "review_session",
                          "environment_checkpoint", ["final_checkpoint_id"], ["id"],
                          ondelete="SET NULL")
    # The queue screen lists an annotator's own attempts by state.
    op.create_index("ix_review_session_annotator_status", "review_session",
                    ["annotator_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_review_session_annotator_status", table_name="review_session")
    op.drop_constraint("fk_review_session_final_checkpoint", "review_session", type_="foreignkey")
    op.drop_constraint("fk_review_session_initial_checkpoint", "review_session", type_="foreignkey")
    for col in ("final_checkpoint_id", "initial_checkpoint_id", "finished_at", "started_at",
                "cua_apps", "bridge_gym_url", "bridge_session_id", "prompt_override", "mode"):
        op.drop_column("review_session", col)
