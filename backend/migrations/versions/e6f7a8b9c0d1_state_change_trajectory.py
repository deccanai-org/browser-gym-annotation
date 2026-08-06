"""Per-step semantic state deltas, and the attempt's initial→final summary.

The trajectory recorded what the annotator's HAND did — normalized pointer
coordinates, scroll deltas — and nothing about what the world did in response.
For SFT the useful signal is the functional transition ("submitting an order"),
and for verifier validation it is the DB diff; neither was reconstructible,
because `world_after` is written only on the LAST step of each materialize
batch and the intermediate steps are deliberately blank.

`world_delta` records the DELTA between two observations rather than a second
copy of the world, so the timeline becomes per-step without a world blob per
step. `delta_span` is what keeps that honest: the gym world can only be read
once per fold batch, so when a batch covered two actions the delta belongs to
the WINDOW, and the span names every step in it rather than attributing the
change to whichever action happened to be last.

`review_session.world_summary` is the same diff at attempt scope — the seeded
initial world vs the end state.

All three are nullable, so this adds no default and rewrites no table; every row
recorded before this simply has no delta, which is exactly the truth about it.

Revision ID: e6f7a8b9c0d1
Revises: d5f6a7b8c9e0
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "d5f6a7b8c9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trajectory_step", sa.Column("world_delta", sa.JSON(), nullable=True))
    op.add_column("trajectory_step", sa.Column("delta_span", sa.JSON(), nullable=True))
    op.add_column("review_session", sa.Column("world_summary", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_session", "world_summary")
    op.drop_column("trajectory_step", "delta_span")
    op.drop_column("trajectory_step", "world_delta")
