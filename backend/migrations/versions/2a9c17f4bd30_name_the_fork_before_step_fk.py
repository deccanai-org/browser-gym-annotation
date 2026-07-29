"""name the fork_before_step FK (breaks the version <-> step cycle)

The model now declares this FK with use_alter + an explicit name, because
trajectory_version.fork_before_step_id and trajectory_step.version_id form a
cycle that create_all/drop_all cannot otherwise order. A database created by
458ad795a7ff got the server-generated name, so rename it here — otherwise a
migrated DB and a freshly created one carry different constraint names and every
future autogenerate diff reads as a drop + add.

Revision ID: 2a9c17f4bd30
Revises: 16bd9e537d89
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "2a9c17f4bd30"
down_revision: Union[str, Sequence[str], None] = "16bd9e537d89"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = "trajectory_version_fork_before_step_id_fkey"
NEW = "fk_version_fork_before_step"


def _rename(frm: str, to: str) -> None:
    """Postgres only, and only when the source constraint is actually there —
    a DB created after this change already has the new name, and SQLite has no
    ALTER ... RENAME CONSTRAINT at all."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    exists = bind.execute(
        sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n AND conrelid = 'trajectory_version'::regclass"),
        {"n": frm},
    ).scalar()
    if exists:
        op.execute(f'ALTER TABLE trajectory_version RENAME CONSTRAINT "{frm}" TO "{to}"')


def upgrade() -> None:
    _rename(OLD, NEW)


def downgrade() -> None:
    _rename(NEW, OLD)
