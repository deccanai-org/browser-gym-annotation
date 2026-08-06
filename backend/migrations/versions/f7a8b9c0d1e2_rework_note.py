"""The reviewer's reason for sending a sample back.

`rework_status` recorded THAT an attempt was returned but not why, so the board
could only say "Returned" — telling the annotator to redo the task without
telling them what was wrong. The note existed on the audit row, which is a ledger
for reviewers, not something the annotator reads.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("review_session",
                  sa.Column("rework_note", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("review_session", "rework_note")
