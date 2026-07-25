"""Bring a freshly seeded world up to where a fork branches off.

An annotator who forks before step 9 wants to drive step 9. What they got was the
TASK SEED — the world as it is before the agent has done anything — so before they
could begin the correction they had to re-perform the first eight steps by hand,
every time. For a fork deep into a long trajectory that is most of the work, and
it is work whose only purpose is to get back to a state the platform already has a
complete record of.

So: after the reset, replay the branch's own flattened prefix into the world
through the annotator's browser. Same actions, same order, same clock protocol the
recording used — this is `finalize`'s replay, pointed at the live pane instead of a
scratch one.

**Deliberately not strict.** Finalize rejects a trajectory that does not reproduce,
and it should: it decides what ships. This decides what an annotator sees when
their pane opens, and the useful answer to "step 6 no longer lands" is a world at
step 5 plus a clear note, not a dead pane and no way forward. What survives a
partial restore is bounded by machinery that is already strict:

* `commit_actions` restores the fork checkpoint and replays the proposed sequence
  before recording anything (`api/versions.py`), so a commit does not depend on
  the live world's prior state at all — it re-establishes it, and rejects with
  "the committed sequence does not replay" when the actions do not hold up.
* `finalize` replays the whole trajectory from a clean reset with `strict=True`.

So a partial restore costs an annotator context and possibly some rework. It
cannot put a wrong trajectory into a shipped sample. That is the reason this is
allowed to be best-effort, and the reason it must be *visible* — an annotator who
believes their world is at step 8 when it is at step 5 will burn an hour finding
out.

**Not a checkpoint restore.** A fork carries `fork_checkpoint_id`, and restoring it
would be one call instead of N. But per-app id counters live in neither
``to_json()`` nor ``statecodec._MUTABLE_SUBAPP``, so a restore mid-episode restarts
them and the next minted id overwrites an existing record (ROADMAP 0.1). Replaying
from a clean reset has no such gap: it is how the world was built the first time.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app import finalize, models, replay, versions
from app.config import settings

# Actions the live browser executes WITHOUT resolving a locator (service.py act():
# they return before `self.resolve(...)`). finalize.replayable() exempts only
# `navigate`/`press` because a shipped trajectory must carry a locator for
# everything the annotator could later inspect — but a REBUILD only needs the
# action to run, and truncating a rebuild at a locator-free `wait` or `scroll`
# strands the annotator earlier than the world can actually be taken. Measured:
# `wait`/`open_tab`/`switch_tab` appear in ~10% of trajectories.
_LOCATOR_FREE = frozenset({"navigate", "press", "wait", "scroll", "open_tab", "switch_tab", "close_tab"})


def _replayable_prefix(actions: list[dict]) -> tuple[list[dict], int | None]:
    """The longest leading run that can be re-executed, and the index it stopped
    at. A step needs a locator unless its kind runs without one."""
    for i, a in enumerate(actions):
        if a["kind"] not in _LOCATOR_FREE and not a["locator"]:
            return actions[:i], i
    return actions, None


@dataclass(frozen=True)
class RestoreReport:
    """How far the world was brought, and why it stopped short if it did."""

    done: int = 0        # actions successfully replayed
    total: int = 0       # actions the branch's prefix contains
    reason: str = ""     # empty when complete
    attempted: bool = False

    @property
    def partial(self) -> bool:
        return self.attempted and self.done < self.total


def prefix_of(db: Session, attempt: models.ReviewSession) -> tuple[list[dict], str]:
    """The actions that rebuild this attempt's branch, and why there are none.

    Only a FORKED version has a prefix worth rebuilding. Version 1 is the
    canonical agent run: replaying it would leave the annotator at the END of the
    recorded trajectory, which is a different feature nobody asked for and would
    make simply looking at a task cost a full replay.
    """
    head = versions.head(db, attempt)
    if head is None:
        return [], "this attempt has no versions"
    if head.parent_version_id is None:
        return [], "not a fork"
    try:
        return finalize.actions_of(db, head), ""
    except versions.LineageError as exc:
        return [], str(exc)


def restore_prefix(
    db: Session,
    attempt: models.ReviewSession,
    *,
    executor: replay.Executor,
    gym,
) -> RestoreReport:
    """Replay the branch prefix into the world the pane is about to show.

    Call ONLY on a freshly reset world. The clock counts from zero, so replaying
    into a world that is already partway through puts every hash comparison — and
    every scheduled event — against the wrong step.
    """
    cap = settings.live_restore_max_steps
    if cap == 0:
        return RestoreReport(reason="prefix restore is disabled")

    actions, why = prefix_of(db, attempt)
    if not actions:
        return RestoreReport(reason=why)

    total = len(actions)

    # Truncate at the first step that cannot be re-executed rather than failing the
    # whole restore. A prefix recorded before semantic locators existed still gets
    # the annotator as far as its last replayable step, which is strictly more than
    # the seed world they get today.
    actions, missing_at = _replayable_prefix(actions)
    stopped = ""
    if missing_at is not None:
        stopped = f"step {missing_at + 1} has no semantic locator, so the rebuild stopped there"
    if len(actions) > cap:
        actions = actions[:cap]
        stopped = f"stopped at the {cap}-step rebuild limit"
    if not actions:
        return RestoreReport(total=total, reason=stopped or "nothing in this prefix can be replayed", attempted=True)

    result = replay.replay(
        actions,
        executor,
        expected_hashes=[a["expectedHash"] for a in actions],
        # Reset-then-replay, so the clock's scheduled-tick decision is read off the
        # gym's own seed world — the shared helper finalize uses.
        clock=replay.scheduled_clock(gym),
        strict=False,
    )

    done = len(result.steps)
    if result.ok and not stopped:
        return RestoreReport(done=done, total=total, attempted=True)
    reason = stopped or (
        f"step {(result.rejected_at or 0) + 1} could not be rebuilt: {result.reason}"
    )
    return RestoreReport(done=done, total=total, reason=reason, attempted=True)


def record(db: Session, lease: models.WorkspaceLease | None, report: RestoreReport) -> None:
    """Persist the report on the workspace whose world it describes.

    On the lease rather than in process memory because the claim is about what is
    in THAT gym: it must survive the backend restart that drops the attachment but
    not the container, and it must stop being true the moment the workspace is
    released.
    """
    if lease is None:
        return
    lease.restore_done = report.done if report.attempted else None
    lease.restore_total = report.total if report.attempted else None
    lease.restore_reason = report.reason if report.attempted else None
    db.commit()


def report_of(lease: models.WorkspaceLease | None) -> RestoreReport:
    """What the last restore into this workspace achieved, or nothing attempted."""
    if lease is None or lease.restore_total is None:
        return RestoreReport()
    return RestoreReport(
        done=int(lease.restore_done or 0),
        total=int(lease.restore_total),
        reason=lease.restore_reason or "",
        attempted=True,
    )


def as_payload(report: RestoreReport) -> dict | None:
    """The shape the pane renders. None when there was nothing to rebuild, so an
    ordinary unforked attempt says nothing rather than showing an empty progress
    note."""
    if not report.attempted:
        return None
    return {
        "done": report.done,
        "total": report.total,
        "partial": report.partial,
        "reason": report.reason,
    }


__all__ = [
    "RestoreReport", "as_payload", "prefix_of", "record", "report_of", "restore_prefix",
]
