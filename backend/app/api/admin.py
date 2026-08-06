"""Dev-only admin utilities. Every endpoint here is HARD-GATED to the dev
environment (prod sets ENV=prod + auto_create_all=false via Alembic), so a
destructive reset can never fire against a real deployment."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app import models
from app.auth import current_annotator
from app.config import settings
from app.db import get_db

router = APIRouter(prefix="/api/admin", tags=["admin"])

_CONFIRM = "RESET-SESSIONS"


class ResetBody(BaseModel):
    """A destructive endpoint demands an explicit typed confirmation, so a stray or
    automated POST can never wipe a working database by accident."""

    confirm: str = ""


def _require_dev() -> None:
    # Two independent dev signals must BOTH hold: env=dev AND create_all bootstrap
    # (prod flips env=prod and auto_create_all=false). Never wipe prod data.
    if not (settings.env == "dev" and settings.auto_create_all):
        raise HTTPException(status_code=403, detail="admin endpoints are disabled outside dev")


def _require_privileged(current: models.Annotator) -> None:
    """The dev gate alone is NOT enough. A dev instance still holds real annotation
    work, and this route was reachable ANONYMOUSLY — an unauthenticated POST wiped
    58 sessions during an audit. Require a logged-in privileged identity too."""
    if current.role not in ("admin", "reviewer"):
        raise HTTPException(status_code=403, detail="admin endpoints require the admin or reviewer role")


@router.post("/reset-sessions")
def reset_sessions(
    body: ResetBody,
    current: models.Annotator = Depends(current_annotator),
    db: Session = Depends(get_db),
) -> dict:
    """Wipe all annotation SESSIONS (and their cascaded suites/verifiers/runs/
    submissions/branches/trajectories) for a clean-slate test run. Tasks and the
    catalog are preserved — only per-annotator work is cleared, so every task
    reopens fresh at 0 reviewed.

    FOUR independent gates: dev environment, authenticated caller, privileged role,
    and an explicit typed confirmation."""
    _require_dev()
    _require_privileged(current)
    if body.confirm != _CONFIRM:
        raise HTTPException(
            status_code=400,
            detail=f'destructive: this wipes all annotation work — pass {{"confirm": "{_CONFIRM}"}} to proceed',
        )
    before = db.scalar(select(func.count()).select_from(models.ReviewSession)) or 0
    annotators = db.scalar(select(func.count()).select_from(models.Annotator)) or 0
    # audit_log references sessions via ON DELETE SET NULL; clear the noise too.
    db.execute(delete(models.AuditLog))
    # Deleting sessions cascades to trajectory / verifier_suite (→verifier,
    # benchmark_run) / submission / trajectory_branch.
    db.execute(delete(models.ReviewSession))
    # Heal any per-run task fill (e.g. a prompt-edit brief that a pre-fix build
    # wrote onto the canonical row): clear gym prompts so the next original review
    # re-fills them. The task LIST reads breakers.json, so this is display-safe.
    db.execute(update(models.Task).where(models.Task.source == "gym").values(prompt=""))
    db.commit()
    return {
        "ok": True,
        "deletedSessions": int(before),
        "annotatorsKept": int(annotators),
        "tasksKept": int(db.scalar(select(func.count()).select_from(models.Task)) or 0),
    }


class AssignBody(BaseModel):
    """Hand a batch of tasks to a set of annotators.

    `overlap` is how many people should get each task. It defaults to 1, but more
    is a legitimate choice, not a mistake: the QA agreement number only means
    something when several annotators have independently done the same task.
    """

    annotatorEmails: list[str] = []
    batch: str = "sellable-breakers-v2"
    taskIds: list[str] = []          # empty = the curated 85 (meta.inEightyFive)
    overlap: int = 1


@router.post("/assign")
def assign_tasks(
    body: AssignBody,
    current: models.Annotator = Depends(current_annotator),
    db: Session = Depends(get_db),
) -> dict:
    """Round-robin a batch of tasks across annotators.

    Not destructive, so no typed confirmation — but it decides what other people
    see when they sign in, so it keeps the dev + privileged gates.

    Idempotent: re-running with the same inputs adds nothing, because the
    (task, annotator) pair is unique. That matters because the natural way to
    grow a batch is to re-run the same command with one more name on it.
    """
    _require_dev()
    _require_privileged(current)

    if not body.annotatorEmails:
        raise HTTPException(status_code=422, detail="name at least one annotator to assign to")
    if body.overlap < 1:
        raise HTTPException(status_code=422, detail="overlap must be at least 1")

    people = db.scalars(
        select(models.Annotator).where(models.Annotator.email.in_(body.annotatorEmails))
    ).all()
    missing = sorted(set(body.annotatorEmails) - {a.email for a in people})
    if missing:
        raise HTTPException(status_code=404, detail=f"no such annotator: {', '.join(missing)}")
    if body.overlap > len(people):
        raise HTTPException(
            status_code=422,
            detail=f"overlap {body.overlap} needs at least that many annotators (got {len(people)})",
        )
    people.sort(key=lambda a: a.email)      # deterministic round-robin

    if body.taskIds:
        tasks = db.scalars(
            select(models.Task).where(models.Task.external_id.in_(body.taskIds))
            .order_by(models.Task.external_id)
        ).all()
        unknown = sorted(set(body.taskIds) - {t.external_id for t in tasks})
        if unknown:
            raise HTTPException(status_code=404, detail=f"no such task: {', '.join(unknown)}")
    else:
        tasks = [
            t for t in db.scalars(
                select(models.Task).where(models.Task.source == "gym")
                .order_by(models.Task.external_id)
            )
            if (t.meta or {}).get("inEightyFive")
        ]

    existing = {
        (a.task_id, a.annotator_id)
        for a in db.scalars(select(models.TaskAssignment))
    }
    created = 0
    for i, task in enumerate(tasks):
        for k in range(body.overlap):
            who = people[(i + k) % len(people)]
            if (task.id, who.id) in existing:
                continue
            db.add(models.TaskAssignment(
                task_id=task.id, annotator_id=who.id, batch=body.batch,
                status="assigned", assigned_by_id=current.id,
            ))
            existing.add((task.id, who.id))
            created += 1
    db.add(models.AuditLog(
        actor=current.email, action="admin.assign", target=body.batch,
        meta={"tasks": len(tasks), "annotators": len(people),
              "overlap": body.overlap, "created": created},
    ))
    db.commit()

    per_person = {
        a.email: db.scalar(
            select(func.count()).select_from(models.TaskAssignment)
            .where(models.TaskAssignment.annotator_id == a.id,
                   models.TaskAssignment.status == "assigned")
        ) or 0
        for a in people
    }
    return {"batch": body.batch, "tasks": len(tasks), "overlap": body.overlap,
            "created": created, "assigned": per_person}
