"""Disposition endpoints — propose, adjudicate, and count.

The annotator proposes on their OWN attempt (ownership 404s, never 403s, so an
attempt id cannot be used to probe for other people's work); a reviewer rules on
somebody else's. The aggregate is the endpoint the 85-task report is waiting on:
it is what turns a pile of failed attempts into "n of these were the model and m
of these were us".
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy.orm import Session

from app import disposition as service
from app import models
from app.api.sessions import _NulSafe, _get_session, _owned_session
from app.auth import current_annotator, require_reviewer
from app.db import get_db
from app.versions import ConcurrencyError

router = APIRouter(prefix="/api", tags=["disposition"])


def _visible_session(db: Session, session_id: UUID, current: models.Annotator) -> models.ReviewSession:
    """Readable by the annotator who owns the attempt, or by a reviewer who has to
    rule on it. Everyone else gets 404 — the same non-disclosure _owned_session
    gives, since a 403 would confirm the attempt exists."""
    s = _get_session(db, session_id)
    if s.annotator_id == current.id or current.role in ("admin", "reviewer"):
        return s
    raise HTTPException(status_code=404, detail="session not found")


class EvidenceIn(_NulSafe):
    kind: str = Field(default="", max_length=16)
    artifactId: UUID | None = None
    checkpointId: UUID | None = None
    note: str = Field(default="", max_length=500)


class ProposeBody(_NulSafe):
    disposition: str = Field(max_length=32)
    note: str = Field(max_length=4000)
    evidence: list[EvidenceIn] = []
    # The client knows the workspace it actually drove; the server falls back to
    # the attempt's own versions/checkpoints when it doesn't say.
    expectedRevision: int


@router.get("/sessions/{session_id}/disposition")
def get_disposition(
    session_id: UUID,
    current: models.Annotator = Depends(current_annotator),
    db: Session = Depends(get_db),
) -> dict:
    return service.describe(db, _visible_session(db, session_id, current))


@router.post("/sessions/{session_id}/disposition")
def propose_disposition(
    session_id: UUID, body: ProposeBody,
    current: models.Annotator = Depends(current_annotator),
    db: Session = Depends(get_db),
) -> dict:
    """Claim why this attempt ended the way it did.

    Deliberately allowed on a submitted attempt: a disposition is a statement
    ABOUT the attempt, not part of the frozen deliverable, and the moment you
    most want one is right after the sample shipped as a failure.
    """
    s = _owned_session(db, session_id, current, lock=True)
    try:
        out = service.propose(
            db, s, annotator=current,
            disposition=body.disposition, note=body.note,
            evidence=[e.model_dump() for e in body.evidence],
            expected_revision=body.expectedRevision,
        )
    except service.DispositionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except service.AlreadyAdjudicated as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConcurrencyError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return out


class DecisionBody(_NulSafe):
    decision: str = Field(max_length=16)  # accept | reject | request_rework
    disposition: str | None = Field(default=None, max_length=32)  # required to reject
    note: str = Field(default="", max_length=4000)
    expectedRevision: int


@router.post("/sessions/{session_id}/disposition/decision")
def adjudicate_disposition(
    session_id: UUID, body: DecisionBody,
    current: models.Annotator = Depends(require_reviewer),
    db: Session = Depends(get_db),
) -> dict:
    """Rule on a proposal. Reviewer-only, and never on your own attempt."""
    s = _get_session(db, session_id, lock=True)
    try:
        out = service.adjudicate(
            db, s, reviewer=current, decision=body.decision,
            disposition=body.disposition, note=body.note,
            expected_revision=body.expectedRevision,
        )
    except service.DispositionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except service.OwnAttempt as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except service.NotProposed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConcurrencyError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return out


@router.get("/dispositions/queue")
def disposition_queue(
    limit: int = Query(default=200, ge=1, le=1000),
    _current: models.Annotator = Depends(require_reviewer),
    db: Session = Depends(get_db),
) -> dict:
    return {"waiting": service.queue(db, limit=limit)}


@router.get("/dispositions/summary")
def disposition_summary(
    task: list[str] | None = Query(default=None),
    _current: models.Annotator = Depends(require_reviewer),
    db: Session = Depends(get_db),
) -> dict:
    """Per-disposition counts over a set of tasks, split adjudicated vs proposed.

    Reviewer-gated like the dataset export: it aggregates across every
    annotator's attempts, and one annotator does not get to read the whole
    cohort's failure record.
    """
    return service.summarize(db, external_ids=task or None)
