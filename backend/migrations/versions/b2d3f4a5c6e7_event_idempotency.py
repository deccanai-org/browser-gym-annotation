"""Make raw-event ingest idempotent.

`EventRecorder.flush` re-queues an entire batch on ANY failure — including a
network drop after the server already committed it. Without a client-minted id
that retry appended a second copy of every event in the batch, and the duplicates
then folded into doubled clicks and doubled fills: a trajectory that says the
annotator did something twice when they did it once.

Nullable + a unique constraint that tolerates NULLs, so rows written before this
existed are untouched (Postgres treats NULLs as distinct in a unique index).

Revision ID: b2d3f4a5c6e7
Revises: a1c2e3b4d5f6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b2d3f4a5c6e7"
down_revision = "a1c2e3b4d5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("interaction_event", sa.Column("client_event_id", sa.String(64), nullable=True))
    op.create_unique_constraint(
        "uq_event_client_id", "interaction_event", ["attempt_id", "client_event_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_event_client_id", "interaction_event", type_="unique")
    op.drop_column("interaction_event", "client_event_id")
