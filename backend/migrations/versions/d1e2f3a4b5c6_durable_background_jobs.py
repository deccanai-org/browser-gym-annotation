"""make a background job survive the process that started it

A jobId is handed to the client and polled for minutes afterwards, but the job
itself lived only in process memory. A restart therefore answered 404 "unknown or
expired job" for every outstanding run — including ones that had already
finished — and the client (frontend/src/lib/api.ts `pollGymJob`) reads a non-200
as a transient blip, so it kept polling to its own timeout and then reported
nothing. This table is the record; the in-process dict becomes a cache over it.

`owner` holds the boot id of the process running the job, which is what lets a
later boot tell "still running, here" apart from "was running in a process that
is now gone" and fail the second kind instead of reporting it queued forever.

The id is a 32-char hex string rather than a Uuid column on purpose: it IS the
jobId already issued to clients, so the row has to be reachable by the exact
string they poll with.

NOTE for autogenerate: the model lives in app/jobs.py, next to the store that
owns it (as CanonicalRun lives in app/canonical.py). migrations/env.py imports
only app.models, so it must also import app.jobs before any
`alembic revision --autogenerate`, or the diff will propose dropping this table.

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "background_job",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=40), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("owner", sa.String(length=32), server_default=sa.text("''"), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_background_job_kind"), "background_job", ["kind"])
    op.create_index(op.f("ix_background_job_status"), "background_job", ["status"])
    # The startup sweep selects on (status, owner); it runs before the first
    # request is served, so it is on the critical path of every boot.
    op.create_index(op.f("ix_background_job_owner"), "background_job", ["owner"])


def downgrade() -> None:
    op.drop_index(op.f("ix_background_job_owner"), table_name="background_job")
    op.drop_index(op.f("ix_background_job_status"), table_name="background_job")
    op.drop_index(op.f("ix_background_job_kind"), table_name="background_job")
    op.drop_table("background_job")
