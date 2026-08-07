"""Two uniqueness rules the code already assumed it had.

Both are the same shape of bug: an ordinal assigned as `max()+1` with no lock and
no constraint, so a second concurrent writer reads the same max and the row it
writes silently displaces work.

`interaction_event.seq` — the fold watermark is `seq > N`. Two recorders on one
attempt (a second tab, or a reopened pane whose old recorder has not died) can
mint the same seq, and the watermark then steps past both. The events are durably
recorded and permanently skipped: the annotator's actions are in the database and
missing from their trajectory.

`trajectory_step.suffix_ordinal` — `materialize()` documents itself as idempotent
("advance the watermark in the SAME transaction as the steps it produced, so
re-running produces nothing new"), which held only because one caller ran at a
time. Two concurrent folds write the whole batch twice at identical ordinals, the
duplicates flatten into the golden, and a client is sold a trajectory that does
every action twice.

Checked against the live database before writing: zero violating pairs in either
table, so this adds a constraint rather than requiring a repair.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_event_seq_per_attempt", "interaction_event", ["attempt_id", "seq"],
    )
    # NULLs do not collide in Postgres, so agent-run steps (which carry no
    # version) are unaffected — this constrains only steps that belong to a
    # version, which is exactly the set materialize writes.
    op.create_unique_constraint(
        "uq_step_ordinal_per_version", "trajectory_step", ["version_id", "suffix_ordinal"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_step_ordinal_per_version", "trajectory_step", type_="unique")
    op.drop_constraint("uq_event_seq_per_attempt", "interaction_event", type_="unique")
