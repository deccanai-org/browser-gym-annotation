"""Multi-annotator QA — aggregate every annotator's submissions per task, measure
inter-annotator agreement, and let a reviewer adjudicate one as the accepted golden."""

from __future__ import annotations

from collections import Counter
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import disposition, models
from app.auth import require_reviewer
from app.db import get_db
from app.models import Annotator

router = APIRouter(prefix="/api/qa", tags=["qa"])


def _agreement(rewards: list[int]) -> dict:
    if not rewards:
        return {"count": 0, "majorityReward": None, "agreement": None, "unanimous": True, "distribution": {}}
    c = Counter(rewards)
    top_val, top_n = c.most_common(1)[0]
    return {
        "count": len(rewards),
        "majorityReward": top_val,
        "agreement": round(top_n / len(rewards), 3),
        "unanimous": top_n == len(rewards),
        "distribution": {str(k): v for k, v in c.items()},
    }


def _per_annotator_votes(rows) -> tuple[dict, int, bool]:
    """Collapse (email, reward, accepted, created_at) rows to ONE vote per distinct
    annotator (their latest submission), so agreement isn't skewed by a prolific
    annotator. Returns (email→reward, total_submissions, adjudicated)."""
    votes: dict[str, dict] = {}
    total, adjudicated = 0, False
    for email, reward, accepted, created in rows:
        total += 1
        adjudicated = adjudicated or bool(accepted)
        key = email or "(anonymous)"
        cur = votes.get(key)
        if cur is None or created > cur["at"]:
            votes[key] = {"reward": reward, "at": created}
    return ({k: v["reward"] for k, v in votes.items()}, total, adjudicated)


@router.get("/tasks")
def qa_tasks(_current: Annotator = Depends(require_reviewer),
             db: Session = Depends(get_db)) -> dict:
    """Every task that has ≥1 submission, with inter-annotator agreement (one vote
    per distinct annotator).

    Reviewer-only: this is the whole cohort's work. Agreement numbers reveal how
    every other annotator scored a task, so an annotator who could read this
    could align their answer to the majority before submitting — which is exactly
    the signal the agreement math exists to measure."""
    rows = db.execute(
        select(
            models.Task.external_id, models.Task.title,
            models.Annotator.email, models.Submission.reward,
            models.Submission.accepted, models.Submission.created_at,
        )
        .join(models.ReviewSession, models.ReviewSession.id == models.Submission.session_id)
        .join(models.Task, models.Task.id == models.ReviewSession.task_id)
        .join(models.Annotator, models.Annotator.id == models.ReviewSession.annotator_id, isouter=True)
    ).all()

    by_task: dict[str, dict] = {}
    for ext, title, email, reward, accepted, created in rows:
        by_task.setdefault(ext, {"title": title, "rows": []})["rows"].append((email, reward, accepted, created))

    out = []
    for ext, t in by_task.items():
        votes, total, adjudicated = _per_annotator_votes(t["rows"])
        ag = _agreement(list(votes.values()))  # one reward per distinct annotator
        out.append({
            "taskExternalId": ext, "title": t["title"],
            "submissions": total, "annotators": len(votes),
            "adjudicated": adjudicated, "agreement": ag["agreement"],
            "majorityReward": ag["majorityReward"], "unanimous": ag["unanimous"],
            "disputed": not ag["unanimous"], "distribution": ag["distribution"],
        })
    out.sort(key=lambda x: (x["adjudicated"], x["unanimous"], -x["submissions"]))
    return {"tasks": out}


@router.get("/tasks/{external_id:path}/submissions")
def qa_submissions(external_id: str,
                   _current: Annotator = Depends(require_reviewer),
                   db: Session = Depends(get_db)) -> dict:
    """Reviewer-only, for the same reason as /tasks: it names each annotator and
    the reward they submitted."""
    task = db.scalar(select(models.Task).where(models.Task.external_id == external_id))
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    rows = db.execute(
        select(models.Submission, models.Annotator.email)
        .join(models.ReviewSession, models.ReviewSession.id == models.Submission.session_id)
        .join(models.Annotator, models.Annotator.id == models.ReviewSession.annotator_id, isouter=True)
        .where(models.ReviewSession.task_id == task.id)
        .order_by(models.Submission.created_at.desc())
    ).all()
    subs = [{
        "sessionId": str(sub.session_id), "submissionId": str(sub.id),
        "annotator": email or "—", "reward": sub.reward, "kind": sub.kind,
        "override": sub.submitted_with_override, "overrideReason": sub.override_reason,
        "accepted": sub.accepted, "at": sub.created_at.isoformat(),
    } for sub, email in rows]
    votes, _total, _adj = _per_annotator_votes([(email, sub.reward, sub.accepted, sub.created_at) for sub, email in rows])
    return {"taskExternalId": external_id, "title": task.title,
            "agreement": _agreement(list(votes.values())), "submissions": subs}


class AdjudicateBody(BaseModel):
    sessionId: str
    # No `reviewer` field: the audit actor is taken from the authenticated
    # identity below. A client-supplied one let the caller sign someone else's
    # name to the decision that picks the golden sample.
    note: str = ""


@router.post("/tasks/{external_id:path}/adjudicate")
def qa_adjudicate(external_id: str, body: AdjudicateBody,
                  current: Annotator = Depends(require_reviewer),
                  db: Session = Depends(get_db)) -> dict:
    """A reviewer accepts one annotator's submission as the golden for this task
    (clears the flag on the others)."""
    task = db.scalar(select(models.Task).where(models.Task.external_id == external_id))
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    subs = db.scalars(
        select(models.Submission)
        .join(models.ReviewSession, models.ReviewSession.id == models.Submission.session_id)
        .where(models.ReviewSession.task_id == task.id)
    ).all()
    target = None
    for sub in subs:
        is_target = str(sub.session_id) == body.sessionId
        sub.accepted = is_target
        if is_target:
            target = sub
    if target is None:
        raise HTTPException(status_code=404, detail="no submission for that session on this task")
    db.add(models.AuditLog(
        session_id=target.session_id, actor=current.email, action="qa.adjudicate",
        target=external_id, meta={"acceptedReward": target.reward, "note": body.note},
    ))
    db.commit()
    return {"accepted": str(target.session_id), "reward": target.reward}


class ReturnBody(BaseModel):
    note: str = ""


@router.post("/submissions/{submission_id}/return")
def qa_return(submission_id: UUID, body: ReturnBody,
              current: Annotator = Depends(require_reviewer),
              db: Session = Depends(get_db)) -> dict:
    """Send a submission back to its annotator for another attempt.

    Adjudicating was the only verb a reviewer had: a sample they judged wrong
    could be left unaccepted, but nothing told the annotator, and the board's
    `returned` tab could never fill. So rework happened over Slack, or not at all.

    This deliberately does NOT unlock the submitted session. `_assert_not_submitted`
    riding every mutating endpoint is what makes the frozen snapshot trustworthy —
    an exported sample must still describe what was actually reviewed. The redo is
    a NEW attempt, linked back through `origin_session_id` when the annotator
    reopens the task, so the history stays readable in both directions.
    """
    sub = db.get(models.Submission, submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="submission not found")
    s = db.get(models.ReviewSession, sub.session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="the attempt behind that submission is gone")

    note = (body.note or "").strip()
    if not note:
        # Same rule the disposition path enforces: "do it again" without a reason
        # is not a review, and the annotator has no way to act on it.
        raise HTTPException(status_code=422,
                            detail="say why you are sending it back — the annotator only sees this note")
    if s.annotator_id is not None and s.annotator_id == current.id:
        raise HTTPException(status_code=409, detail="you cannot send your own attempt back to yourself")

    sub.accepted = False
    s.rework_status = disposition.REWORK_REQUESTED
    s.rework_note = note
    db.add(models.AuditLog(
        session_id=s.id, actor=current.email, action="qa.return",
        target=str(sub.id), meta={"note": note, "reward": sub.reward},
    ))
    db.commit()
    return {"returned": str(sub.session_id), "reworkStatus": s.rework_status, "note": note}
