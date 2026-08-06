"""The annotator's task list — the page they land on after signing in.

This is the queue as the person sees it: the breakers assigned to them, each
tagged with where THEY left off (to do / in progress / submitted), not a flat
catalogue. Status is derived from that annotator's own sessions, so two people
working the same batch see different boards.

Read-only and cheap: one pass over the 85 breakers joined to this annotator's
sessions. Opening a task and doing it is the live-browser flow (POST
/sessions + /live); this endpoint only decides what to show and where each row's
button goes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.auth import current_annotator
from app.db import get_db

router = APIRouter(prefix="/api", tags=["my-tasks"])

# The dataset the pilot draws from — the curated 85. Shown as the batch label so
# an annotator can tell which set they are working, and quoted honestly rather
# than as a made-up sprint name.
BATCH = "sellable-breakers-v2"

# Display domains for the five realistic apps. Presentation only — the annotator
# reaches them through the bridged gym, never these hostnames.
_APP = {
    "shop": ("ShopGym", "shopgym.com"),
    "market": ("ValueMart", "valuemart.com"),
    "mail": ("ShopMail", "shopmail.com"),
    "calendar": ("GymCal", "gymcal.com"),
    "food": ("GymEats", "gymeats.com"),
}

# A session collapses to one of the board's five states. The open statuses
# (draft → benchmark_run) are all "still working"; the other three are decided by
# what happened to the sample AFTER it was submitted:
#
#   returned    a reviewer sent it back — `rework_status == "requested"`
#   in_review   submitted, nobody has adjudicated it yet
#   submitted   submitted AND accepted as the golden
#
# The distinction matters to the person: "waiting on a reviewer" and "done" feel
# nothing alike, and being sent back is actionable while both of the others are
# not. Note `returned` is checked FIRST — a returned attempt is still `status ==
# "submitted"` (the frozen snapshot is never unlocked), so ordering is what keeps
# it from reading as merely awaiting review.
_OPEN = {"draft", "steps_approved", "verifiers_generated", "benchmark_run"}


def _sites(task: models.Task) -> list[dict]:
    """The apps this task's world spans, primary first — the SITES column."""
    meta = task.meta if isinstance(task.meta, dict) else {}
    apps = list((meta.get("apps") or {}).keys())
    primary = meta.get("primaryApp") or "shop"
    ordered = ([primary] if primary in apps else []) + [a for a in apps if a != primary]
    if not ordered:
        ordered = [primary]
    return [{"app": a, "title": _APP.get(a, (a, a))[0], "domain": _APP.get(a, (a, a))[1]}
            for a in ordered]


def _status_of(sess: models.ReviewSession | None, accepted: bool | None = None) -> str:
    """Where this annotator stands on one task.

    `accepted` is the adjudication verdict on that session's submission — None
    when there is no submission or nobody has ruled yet.
    """
    if sess is None:
        return "todo"
    if sess.status != "submitted":
        return "in_progress"
    if (sess.rework_status or "") == "requested":
        return "returned"
    return "submitted" if accepted else "in_review"


@router.get("/my-tasks")
def my_tasks(current: models.Annotator = Depends(current_annotator),
             db: Session = Depends(get_db)) -> dict:
    # The 85 curated breakers, in a stable order (by id) so the board never
    # reshuffles between reloads.
    breakers = [
        t for t in db.execute(
            select(models.Task).where(models.Task.source == "gym").order_by(models.Task.external_id)
        ).scalars()
        if (t.meta or {}).get("inEightyFive")
    ]

    # This annotator's latest session per task — one query, newest wins. `status`
    # and where they resume both come from here, so a shared batch shows each
    # person their own progress.
    latest: dict = {}
    for s in db.execute(
        select(models.ReviewSession).where(models.ReviewSession.annotator_id == current.id)
        # By created_at, NOT updated_at: "where I stand on this task" means my most
        # recent ATTEMPT, and `updated_at` moves for reasons that are not attempts.
        # Answering a rework request writes `rework_status = "done"` on the OLD
        # session, which pushed its timestamp past the fresh one — so starting the
        # redo left the board still showing the returned attempt.
        .order_by(models.ReviewSession.created_at.asc())
    ).scalars():
        latest[s.task_id] = s  # ascending, so the newest attempt per task wins

    # Adjudication verdicts for those sessions — one query, so "waiting on a
    # reviewer" can be told apart from "accepted". Without this every submitted
    # row reads as done and the annotator cannot see their own queue.
    accepted: dict = {}
    session_ids = [s.id for s in latest.values() if s.status == "submitted"]
    if session_ids:
        for sess_id, acc in db.execute(
            select(models.Submission.session_id, models.Submission.accepted)
            .where(models.Submission.session_id.in_(session_ids))
            .order_by(models.Submission.created_at.asc())
        ):
            accepted[sess_id] = bool(acc)      # ascending, so the latest wins

    # Step counts for the in-progress sessions only (a handful) — the "resume at
    # step N" hint. Counting every task's steps would be 85 pointless queries.
    resume: dict = {}
    active_versions = {s.active_version_id: s.task_id
                       for s in latest.values()
                       if _status_of(s, accepted.get(s.id)) == "in_progress" and s.active_version_id}
    if active_versions:
        for vid, n in db.execute(
            select(models.TrajectoryStep.version_id, func.count(models.TrajectoryStep.id))
            .where(models.TrajectoryStep.version_id.in_(list(active_versions)))
            .group_by(models.TrajectoryStep.version_id)
        ):
            resume[active_versions[vid]] = int(n)

    rows = []
    counts = {"todo": 0, "in_progress": 0, "returned": 0, "in_review": 0, "submitted": 0, "all": 0}
    for t in breakers:
        sess = latest.get(t.id)
        status = _status_of(sess, accepted.get(sess.id) if sess is not None else None)
        meta = t.meta if isinstance(t.meta, dict) else {}
        counts[status] += 1
        counts["all"] += 1
        rows.append({
            "id": t.external_id,
            "title": t.title or t.external_id,
            "category": t.category or "",
            "difficulty": t.difficulty or meta.get("difficulty", ""),
            "prompt": t.prompt or "",
            "primaryApp": meta.get("primaryApp") or "shop",
            "sites": _sites(t),
            "status": status,
            "resumeStep": resume.get(t.id),
            "sessionId": str(sess.id) if sess is not None else None,
            # Why a reviewer sent it back — the only thing that makes "Returned"
            # actionable rather than just discouraging.
            "reworkNote": (sess.rework_note or "") if sess is not None and status == "returned" else "",
            "updatedAt": (sess.updated_at.isoformat() if sess is not None and sess.updated_at else None),
        })

    # NEXT UP: the task most naturally resumed — the last one touched that is
    # still open, else the first thing to do. Never a submitted one.
    in_progress = [r for r in rows if r["status"] == "in_progress"]
    in_progress.sort(key=lambda r: r["updatedAt"] or "", reverse=True)
    # Work a reviewer sent back comes first: it is blocking someone else and it
    # is the only state that already says what to do.
    returned = [r for r in rows if r["status"] == "returned"]
    returned.sort(key=lambda r: r["updatedAt"] or "", reverse=True)
    todo = [r for r in rows if r["status"] == "todo"]
    next_up = returned[0] if returned else (in_progress[0] if in_progress else (todo[0] if todo else None))

    return {
        "annotator": {
            "name": current.display_name or current.email,
            "email": current.email,
            "role": current.role,
        },
        "batch": BATCH,
        "assigned": counts["all"],
        # What the ANNOTATOR finished, which is what they control. Counting only
        # adjudicated samples made their number fall whenever a reviewer was
        # behind — work they had already done, un-done on their own board.
        # `accepted` is shown alongside so the review backlog is still visible.
        "quota": {
            "submitted": counts["in_review"] + counts["submitted"],
            "accepted": counts["submitted"],
            "target": counts["all"],
        },
        "counts": counts,
        "nextUp": next_up,
        "tasks": rows,
    }
