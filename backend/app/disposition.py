"""Disposition — the workflow that answers "whose failure was it?".

A failed attempt tells a report nothing until someone says WHY it failed: the
model got it wrong, or the harness never gave it a chance. Today that answer is
a boolean at best, which is why 85 breaker tasks sit un-analysable — "failed"
cannot be separated from "unsolvable", "seeded wrong", or "the verifier was
lying". This module makes the answer an adjudicated record instead of a flag.

Three rules make the record worth trusting:

* **A disposition is proposed, not declared.** The annotator who ran the attempt
  proposes a value with a note and evidence; a REVIEWER rules on it. The one
  person with a motive to blame the environment is the person whose attempt just
  failed, so their word alone must not become the dataset's answer.
* **A claim against the harness carries evidence.** Every value except
  `model_failure` blames something the annotator does not own, so it must cite at
  least one real Artifact or EnvironmentCheckpoint row. An unevidenced
  "environment_broken" is indistinguishable from a shrug, and a report built out
  of shrugs is worse than no report — it would send someone hunting a bug that
  was never there.
* **It names WHICH environment.** "the environment was broken" is meaningless
  without the image digest and task revision it was broken in: a fix would have
  nothing to verify against, and a re-run would silently prove the wrong thing.
  Both are stamped onto the event as it is recorded, never re-derived afterwards
  — by then the workspace has moved on and the answer would be a guess.

State lives on the columns ReviewSession already carries; the workflow itself —
every proposal, ruling, rework round, note and citation — is an append-only
ledger in `audit_log`, the table this codebase already writes on every mutation.
An attempt's STANDING is derived from the columns alone, so it survives even if
the ledger is trimmed for retention:

    disposition IS NULL                ->  undisposed
    disposition_by_id == annotator_id  ->  proposed    (the annotator's claim)
    disposition_by_id != annotator_id  ->  adjudicated (a reviewer ruled)

A reviewer may never adjudicate their own attempt, which is the whole reason
that last line can be trusted as a proxy for "someone independent looked".
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app import models
from app.versions import ConcurrencyError

# The taxonomy. `model_failure` is the only value that blames the thing under
# test; every other value is a claim that the harness, not the model, is why the
# attempt failed — which is exactly the claim the report needs separated out.
DISPOSITIONS: tuple[str, ...] = (
    "model_failure",
    "task_unsolvable",
    "environment_broken",
    "seed_invalid",
    "instruction_ambiguous",
    "verifier_invalid",
)
BLAMES_THE_HARNESS = frozenset(DISPOSITIONS) - {"model_failure"}

ACCEPT, REJECT, REWORK = "accept", "reject", "request_rework"
DECISIONS = (ACCEPT, REJECT, REWORK)

UNDISPOSED, PROPOSED, ADJUDICATED = "undisposed", "proposed", "adjudicated"

REWORK_REQUESTED, REWORK_DONE = "requested", "done"

# Ledger actions. Dotted-lowercase like every other audited mutation, so the
# existing audit tooling reads them without special-casing.
PROPOSE_ACTION = "session.disposition"
DECISION_ACTION = "session.disposition_decision"
REWORK_ACTION = "session.rework"
_LEDGER_ACTIONS = (PROPOSE_ACTION, DECISION_ACTION, REWORK_ACTION)

# An attempt whose environment was never stamped. Kept as an explicit bucket
# rather than dropped, because "we cannot say which environment" is itself a
# finding the report has to see.
UNRECORDED = "(unrecorded)"


class DispositionError(ValueError):
    """A malformed disposition: a value outside the taxonomy, a harness claim
    with no evidence, or a citation pointing at a row that does not exist."""


class NotProposed(RuntimeError):
    """Adjudicating an attempt nobody has proposed a disposition for. Ruling on
    an empty claim would manufacture a verdict out of nothing."""


class AlreadyAdjudicated(RuntimeError):
    """An annotator trying to overwrite a reviewer's ruling. Re-proposal is
    legitimate only once a reviewer has asked for rework — otherwise the
    adjudication step is decorative and anyone can have the last word."""


class OwnAttempt(RuntimeError):
    """A reviewer ruling on their own attempt. Self-adjudication would make
    `standing` a lie: the column split cannot tell it from an independent ruling."""


# --------------------------------------------------------------------------- state
def standing(s: models.ReviewSession) -> str:
    """Where this attempt's disposition sits in the workflow.

    Derived from the columns, never from the ledger, so the split the report
    depends on ("adjudicated vs proposed") holds even against a trimmed
    audit_log. A row whose actor was deleted (FK SET NULL) reads as `proposed`:
    an unattributable disposition has not been independently ruled on.
    """
    if not s.disposition:
        return UNDISPOSED
    if s.disposition_by_id is None or s.disposition_by_id == s.annotator_id:
        return PROPOSED
    return ADJUDICATED


def environment_digest(db: Session, s: models.ReviewSession) -> str:
    """WHICH environment this attempt ran in, best available source first.

    The attempt head knows it exactly; an older version or a captured checkpoint
    is the fallback. Empty is an honest answer — better than inventing a digest
    the attempt never actually ran against.
    """
    head = db.get(models.TrajectoryVersion, s.active_version_id) if s.active_version_id else None
    if head is not None and head.environment_image_digest:
        return head.environment_image_digest
    digest = db.scalar(
        select(models.TrajectoryVersion.environment_image_digest)
        .where(
            models.TrajectoryVersion.attempt_id == s.id,
            models.TrajectoryVersion.environment_image_digest != "",
        )
        .order_by(models.TrajectoryVersion.version_no.desc())
    )
    if digest:
        return digest
    digest = db.scalar(
        select(models.EnvironmentCheckpoint.environment_image_digest)
        .where(
            models.EnvironmentCheckpoint.attempt_id == s.id,
            models.EnvironmentCheckpoint.environment_image_digest != "",
        )
        .order_by(models.EnvironmentCheckpoint.created_at.desc())
    )
    return digest or ""


# --------------------------------------------------------------------------- evidence
def resolve_evidence(db: Session, items: list[dict]) -> list[dict]:
    """Turn citations into a verified evidence list.

    Every id is resolved against a real row before it is recorded. A dangling
    citation is worse than no citation: it reads as proof in the report and
    evaporates the moment anyone tries to open it.

    A cited checkpoint routinely belongs to the SYSTEM session that recorded the
    canonical gym run rather than to this attempt — that shared run IS the
    evidence for "the environment was broken" — so ownership is deliberately not
    required here.
    """
    out: list[dict] = []
    for item in items:
        artifact_id = item.get("artifactId")
        checkpoint_id = item.get("checkpointId")
        if artifact_id is None and checkpoint_id is None:
            raise DispositionError("each evidence item must cite an artifact or a checkpoint")
        kind = item.get("kind") or ""
        if artifact_id is not None:
            art = db.get(models.Artifact, artifact_id)
            if art is None:
                raise DispositionError(f"no artifact {artifact_id}")
            kind = kind or art.kind
        if checkpoint_id is not None:
            cp = db.get(models.EnvironmentCheckpoint, checkpoint_id)
            if cp is None:
                raise DispositionError(f"no checkpoint {checkpoint_id}")
            kind = kind or "checkpoint"
        out.append({
            "kind": kind,
            "artifactId": str(artifact_id) if artifact_id else None,
            "checkpointId": str(checkpoint_id) if checkpoint_id else None,
            "note": item.get("note") or "",
        })
    return out


# --------------------------------------------------------------------------- ledger
def _order(rows: list[models.AuditLog]) -> list[models.AuditLog]:
    """Oldest first, tie-broken by the per-attempt sequence in the meta.

    `created_at` alone cannot order this: it is second-resolution on SQLite and
    transaction-start time on Postgres, so two events in the same round sort
    arbitrarily — and a history that shows a ruling before the proposal it ruled
    on is worse than no history at all.
    """
    return sorted(rows, key=lambda r: (r.created_at, int((r.meta or {}).get("seq", 0))))


def _ledger(db: Session, attempt_id: UUID) -> list[models.AuditLog]:
    return _order(list(db.scalars(
        select(models.AuditLog)
        .where(models.AuditLog.session_id == attempt_id, models.AuditLog.action.in_(_LEDGER_ACTIONS))
    ).all()))


def _round(db: Session, attempt_id: UUID) -> int:
    """1-based proposal round. Counting the ledger rather than a column means a
    rework loop cannot silently lose track of how many times this was re-argued."""
    n = db.scalar(
        select(func.count())
        .select_from(models.AuditLog)
        .where(models.AuditLog.session_id == attempt_id, models.AuditLog.action == PROPOSE_ACTION)
    )
    return int(n or 0) + 1


def _record(db: Session, s: models.ReviewSession, action: str, *, actor: str, meta: dict) -> None:
    """Append one workflow event and FLUSH it.

    The flush is load-bearing: the app's sessions run autoflush=False, so the
    `describe()` that builds this very response would read the ledger without the
    event it just wrote and hand the client a stale history.
    """
    seq = db.scalar(
        select(func.count())
        .select_from(models.AuditLog)
        .where(models.AuditLog.session_id == s.id, models.AuditLog.action.in_(_LEDGER_ACTIONS))
    )
    db.add(models.AuditLog(
        session_id=s.id, actor=actor, action=action, target=str(s.id),
        meta={**meta, "seq": int(seq or 0) + 1},
    ))
    db.flush()


def _cas(db: Session, s: models.ReviewSession, expected_revision: int, **values) -> int:
    """Compare-and-swap on the attempt revision.

    A disposition is a lifecycle transition, not a note field: two people looking
    at the same stale screen must not both get to write the answer, and the loser
    has to reload and see what the winner said.
    """
    res = db.execute(
        update(models.ReviewSession)
        .where(models.ReviewSession.id == s.id, models.ReviewSession.revision == expected_revision)
        .values(revision=expected_revision + 1, **values)
    )
    if res.rowcount != 1:
        raise ConcurrencyError("this attempt moved on; reload before recording a disposition")
    db.refresh(s)
    return s.revision


# --------------------------------------------------------------------------- workflow
def propose(
    db: Session,
    s: models.ReviewSession,
    *,
    annotator: models.Annotator,
    disposition: str,
    note: str,
    evidence: list[dict],
    expected_revision: int,
) -> dict:
    """The annotator's claim about why their attempt ended the way it did.

    The environment digest is DERIVED, never accepted from the caller. The one
    person with a motive to blame the environment is the person whose attempt
    just failed, so letting them name which environment they are blaming would
    send a fix to be verified against an image the attempt never ran in — and the
    claim would still look fully evidenced.
    """
    if disposition not in DISPOSITIONS:
        raise DispositionError(
            f"unknown disposition {disposition!r} — one of {', '.join(DISPOSITIONS)}"
        )
    note = (note or "").strip()
    if not note:
        raise DispositionError("a disposition needs a note saying what was observed")
    if standing(s) == ADJUDICATED and s.rework_status != REWORK_REQUESTED:
        raise AlreadyAdjudicated(
            "a reviewer already ruled on this attempt — ask for rework before re-proposing"
        )
    cited = resolve_evidence(db, evidence)
    if disposition in BLAMES_THE_HARNESS and not cited:
        raise DispositionError(
            f"{disposition} blames the harness — cite at least one artifact or checkpoint"
        )
    digest = environment_digest(db, s)
    satisfies_rework = s.rework_status == REWORK_REQUESTED
    rnd = _round(db, s.id)
    rev = _cas(
        db, s, expected_revision,
        disposition=disposition,
        disposition_note=note,
        disposition_by_id=annotator.id,
        disposition_at=func.now(),
        rework_status=REWORK_DONE if satisfies_rework else s.rework_status,
    )
    _record(db, s, PROPOSE_ACTION, actor=annotator.email, meta={
        "event": "proposed",
        "disposition": disposition,
        "note": note,
        "evidence": cited,
        "environmentImageDigest": digest,
        "taskRevision": s.task_revision,
        "round": rnd,
        "satisfiesRework": satisfies_rework,
        "attemptRevision": rev,
    })
    return describe(db, s)


def adjudicate(
    db: Session,
    s: models.ReviewSession,
    *,
    reviewer: models.Annotator,
    decision: str,
    disposition: str | None,
    note: str,
    expected_revision: int,
) -> dict:
    """A reviewer's ruling on the proposal: accept it, replace it, or send it back."""
    if decision not in DECISIONS:
        raise DispositionError(f"decision must be one of {', '.join(DECISIONS)}")
    if s.annotator_id is not None and s.annotator_id == reviewer.id:
        raise OwnAttempt("a reviewer cannot adjudicate their own attempt")
    if not s.disposition:
        raise NotProposed("there is no proposed disposition on this attempt to rule on")
    note = (note or "").strip()
    proposed = s.disposition
    digest = environment_digest(db, s)
    meta = {
        "disposition": proposed,
        "note": note,
        "environmentImageDigest": digest,
        "taskRevision": s.task_revision,
    }

    if decision == ACCEPT:
        if disposition and disposition != proposed:
            raise DispositionError(
                "accepting with a different value is a reject — name it as one so the "
                "record shows the proposal was overturned"
            )
        rev = _cas(
            db, s, expected_revision,
            disposition=proposed, disposition_by_id=reviewer.id, disposition_at=func.now(),
            # A ruling ANSWERS any outstanding rework request. Leaving the flag
            # set holds the re-propose door open forever, and the annotator can
            # then overwrite the reviewer's ruling — which makes adjudication
            # decorative, the one thing this module exists to prevent.
            rework_status="",
        )
        meta |= {"event": "accepted", "attemptRevision": rev}
        _record(db, s, DECISION_ACTION, actor=reviewer.email, meta=meta)
        return describe(db, s)

    if decision == REJECT:
        if not note:
            raise DispositionError("rejecting a proposal requires a note saying why")
        if not disposition:
            raise DispositionError(
                "rejecting requires the disposition that actually holds — request rework "
                "instead when it is not yet knowable"
            )
        if disposition not in DISPOSITIONS:
            raise DispositionError(
                f"unknown disposition {disposition!r} — one of {', '.join(DISPOSITIONS)}"
            )
        if disposition == proposed:
            raise DispositionError("that is the proposed value — accept it instead")
        # disposition_note always describes the disposition that STANDS. The
        # annotator's original reasoning is not lost — it is the previous ledger
        # entry — but leaving it here would attach their note to a value they
        # never argued for.
        rev = _cas(
            db, s, expected_revision,
            disposition=disposition, disposition_note=note,
            disposition_by_id=reviewer.id, disposition_at=func.now(),
            rework_status="",  # a ruling answers any outstanding rework request
        )
        meta |= {"event": "rejected", "disposition": disposition, "replaced": proposed,
                 "attemptRevision": rev}
        _record(db, s, DECISION_ACTION, actor=reviewer.email, meta=meta)
        return describe(db, s)

    if not note:
        raise DispositionError("requesting rework requires a note saying what to redo")
    # Rework leaves the claim and its author alone: nothing has been adjudicated,
    # the ball is simply back with the annotator, and `standing` must keep saying
    # so rather than counting an unfinished argument as a reviewer's answer.
    rev = _cas(db, s, expected_revision, rework_status=REWORK_REQUESTED)
    meta |= {"event": "rework_requested", "attemptRevision": rev}
    _record(db, s, REWORK_ACTION, actor=reviewer.email, meta=meta)
    return describe(db, s)


# --------------------------------------------------------------------------- read
def _actor_emails(db: Session, ids: list[UUID]) -> dict[UUID, str]:
    if not ids:
        return {}
    rows = db.execute(
        select(models.Annotator.id, models.Annotator.email).where(models.Annotator.id.in_(ids))
    ).all()
    return {aid: email for aid, email in rows}


def history(db: Session, attempt_id: UUID) -> list[dict]:
    """Every proposal, ruling and rework round, oldest first."""
    return [
        {
            "at": row.created_at.isoformat(),
            "actor": row.actor,
            "event": (row.meta or {}).get("event", ""),
            "disposition": (row.meta or {}).get("disposition"),
            "replaced": (row.meta or {}).get("replaced"),
            "note": (row.meta or {}).get("note", ""),
            "evidence": (row.meta or {}).get("evidence", []),
            "environmentImageDigest": (row.meta or {}).get("environmentImageDigest", ""),
            "taskRevision": (row.meta or {}).get("taskRevision"),
            "round": (row.meta or {}).get("round"),
        }
        for row in _ledger(db, attempt_id)
    ]


def _evidence_backs_the_ruling(s: models.ReviewSession, latest: dict | None) -> bool:
    """Whether the latest proposal's artifacts actually support what now stands.
    They do when nobody overturned it — i.e. the standing disposition is still the
    one that was proposed."""
    return bool(latest) and s.disposition == latest.get("disposition")


def describe(db: Session, s: models.ReviewSession) -> dict:
    """The attempt's disposition as the UI needs to restore it."""
    rows = history(db, s.id)
    proposals = [r for r in rows if r["event"] == "proposed"]
    latest = proposals[-1] if proposals else None
    by = db.get(models.Annotator, s.disposition_by_id) if s.disposition_by_id else None
    return {
        "attemptId": str(s.id),
        "revision": s.revision,
        "disposition": s.disposition,
        "standing": standing(s),
        "note": s.disposition_note or "",
        "by": by.email if by is not None else None,
        "at": s.disposition_at.isoformat() if s.disposition_at else None,
        "reworkStatus": s.rework_status or "",
        "taskRevision": s.task_revision,
        # The environment recorded WITH the claim, not the one the attempt happens
        # to point at now — that is the only digest a fix can be verified against.
        "environmentImageDigest": (
            latest["environmentImageDigest"] if latest else environment_digest(db, s)
        ),
        # Evidence belongs to the CLAIM it was gathered for. When a reviewer
        # overturns a proposal the standing disposition is theirs, and attaching
        # the annotator's artifacts to it would present evidence collected to
        # argue "the environment was broken" as if it backed "the model failed" —
        # in the one report that exists to tell those two apart. The artifacts are
        # not lost; they are still in `history` under the proposal they support.
        "evidence": latest["evidence"] if _evidence_backs_the_ruling(s, latest) else [],
        "round": latest["round"] if latest else 0,
        "history": rows,
        "taxonomy": list(DISPOSITIONS),
    }


def _attempt_rows(db: Session, external_ids: list[str] | None):
    """Human attempts, joined to their task.

    System gym runs are excluded: they carry an `agent` (a human session never
    does) and exist only to hold the canonical trajectory. Counting them would
    add one permanently-undisposed row per task and quietly halve every rate the
    report quotes.
    """
    q = (
        select(models.ReviewSession, models.Task.external_id, models.Task.title)
        .join(models.Task, models.Task.id == models.ReviewSession.task_id)
        .where(models.ReviewSession.agent == "")
    )
    if external_ids:
        q = q.where(models.Task.external_id.in_(external_ids))
    return db.execute(q).all()


def _proposal_stamps(db: Session, attempt_ids: list[UUID]) -> dict[UUID, dict]:
    """attempt id → the environment/task-revision stamp of its latest proposal."""
    if not attempt_ids:
        return {}
    rows = _order(list(db.scalars(
        select(models.AuditLog)
        .where(
            models.AuditLog.session_id.in_(attempt_ids),
            models.AuditLog.action == PROPOSE_ACTION,
        )
    ).all()))
    out: dict[UUID, dict] = {}
    for row in rows:  # ascending order → the last write per attempt wins
        meta = row.meta or {}
        out[row.session_id] = {
            "environmentImageDigest": meta.get("environmentImageDigest") or "",
            "taskRevision": meta.get("taskRevision"),
        }
    return out


def _blank_counts() -> dict[str, dict[str, int]]:
    # Zero-filled across the WHOLE taxonomy so the report renders a stable table
    # and a value nobody chose is visibly zero rather than absent.
    return {d: {"proposed": 0, "adjudicated": 0, "total": 0} for d in DISPOSITIONS}


def summarize(db: Session, *, external_ids: list[str] | None = None) -> dict:
    """How many attempts landed in each disposition, adjudicated vs merely proposed.

    This is the question the 85-task report is blocked on. The split matters as
    much as the counts: forty attempts calling themselves `environment_broken`
    means nothing until a reviewer who did not run them agrees, and a summary
    that merged the two would let one annotator's bad week read as a broken
    harness.
    """
    rows = _attempt_rows(db, external_ids)
    stamps = _proposal_stamps(db, [s.id for s, _ext, _t in rows if s.disposition])

    overall = _blank_counts()
    by_task: dict[str, dict] = {}
    by_env: dict[str, dict] = {}
    totals = {"attempts": 0, "undisposed": 0, "proposed": 0, "adjudicated": 0, "reworkRequested": 0}

    for s, external_id, title in rows:
        task = by_task.setdefault(external_id, {
            "taskExternalId": external_id, "title": title, "attempts": 0,
            "undisposed": 0, "proposed": 0, "adjudicated": 0,
            "byDisposition": _blank_counts(),
        })
        totals["attempts"] += 1
        task["attempts"] += 1
        if s.rework_status == REWORK_REQUESTED:
            totals["reworkRequested"] += 1
        where = standing(s)
        if where == UNDISPOSED:
            totals["undisposed"] += 1
            task["undisposed"] += 1
            continue
        totals[where] += 1
        task[where] += 1
        # A value outside the taxonomy can only be a pre-workflow row; bucket it
        # under its own key rather than dropping an attempt out of the totals.
        for bucket in (overall, task["byDisposition"]):
            counts = bucket.setdefault(s.disposition, {"proposed": 0, "adjudicated": 0, "total": 0})
            counts[where] += 1
            counts["total"] += 1
        digest = (stamps.get(s.id) or {}).get("environmentImageDigest") or UNRECORDED
        env = by_env.setdefault(digest, {"attempts": 0, "dispositions": {}})
        env["attempts"] += 1
        env["dispositions"][s.disposition] = env["dispositions"].get(s.disposition, 0) + 1

    return {
        "tasks": sorted(by_task),
        "totals": totals,
        "byDisposition": overall,
        "byEnvironment": by_env,
        "byTask": sorted(by_task.values(), key=lambda t: t["taskExternalId"]),
        "taxonomy": list(DISPOSITIONS),
    }


def queue(db: Session, *, limit: int = 200) -> list[dict]:
    """Proposals waiting on a reviewer, longest-waiting first.

    Without this a reviewer would need an attempt UUID from somewhere, and the
    adjudication step would exist but never actually happen.
    """
    waiting = db.execute(
        select(models.ReviewSession, models.Task.external_id, models.Annotator.email)
        .join(models.Task, models.Task.id == models.ReviewSession.task_id)
        .join(models.Annotator, models.Annotator.id == models.ReviewSession.annotator_id, isouter=True)
        # The `standing() == PROPOSED` rule, expressed in SQL so the limit bounds
        # the QUEUE and not the scan — filtering after a limit would hide waiting
        # proposals behind a wall of already-adjudicated ones.
        .where(
            models.ReviewSession.disposition.is_not(None),
            models.ReviewSession.agent == "",
            or_(
                models.ReviewSession.disposition_by_id.is_(None),
                models.ReviewSession.disposition_by_id == models.ReviewSession.annotator_id,
            ),
            # A rework request IS the reviewer's action. Until the annotator
            # answers it the ball is theirs, so leaving it in a queue titled
            # "waiting on a reviewer" pads that queue with work nobody can do —
            # and a queue that cannot be emptied stops being read.
            models.ReviewSession.rework_status != REWORK_REQUESTED,
        )
        .order_by(models.ReviewSession.disposition_at)
        .limit(limit)
    ).all()
    stamps = _proposal_stamps(db, [s.id for s, _e, _m in waiting])
    return [
        {
            "attemptId": str(s.id),
            "taskExternalId": ext,
            "annotator": email or "—",
            "disposition": s.disposition,
            "note": s.disposition_note or "",
            "reworkStatus": s.rework_status or "",
            "revision": s.revision,
            "taskRevision": s.task_revision,
            "environmentImageDigest": (stamps.get(s.id) or {}).get("environmentImageDigest", ""),
            "at": s.disposition_at.isoformat() if s.disposition_at else None,
        }
        for s, ext, email in waiting
    ]
