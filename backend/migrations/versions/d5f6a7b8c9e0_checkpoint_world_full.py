"""Record the COMPLETE world on a checkpoint, for exact suspend/resume.

`environment_checkpoint.world` holds the compact verifier view
(`/_harness/world`). That view is a published contract — verifier paths, the seed
goldens, the db-vs-factory byte-equality tests — so it cannot be widened. But it
drops per-product stock, which placing an order decrements, so restoring an
attempt from it alone silently restocks everything the annotator bought: the
world they come back to is not the world they left.

`world_full` carries `/_harness/world_full` (dataclasses.asdict) alongside it.
Nullable, so every checkpoint captured before this simply falls back to `world`.

Revision ID: d5f6a7b8c9e0
Revises: c3e4f5a6b7d8
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5f6a7b8c9e0"
down_revision: Union[str, Sequence[str], None] = "c3e4f5a6b7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("environment_checkpoint", sa.Column("world_full", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("environment_checkpoint", "world_full")
