"""Agent branch runs — the handoff between a human workspace and a batch agent.

The isolation rule (§2.1), stated as a sequence:

    capture the branch's starting checkpoint
    → launch an ISOLATED worker from it (never the annotator's own gym)
    → persist the result as a PENDING CHILD version
    → leave the human workspace untouched
    → the human explicitly selects the child if they want it

The last two lines are the load-bearing ones. A completing job never advances
`active_version_id`, which is exactly what makes out-of-order completions safe: a
slow run that lands after the annotator moved on becomes an unselected candidate
instead of silently overwriting their decision.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app import checkpoints, models, recorder, versions

QUEUED, RUNNING, DONE, ERROR = "queued", "running", "done", "error"
TERMINAL = (DONE, ERROR)

# What the annotator reads when the cap stops them. A refusal that only says no
# strands them mid-attempt, so the way forward is named at the one moment they
# are looking for it. The manual path it points at is the one
# tests/test_e2e_correction_loop.py walks end to end.
EXHAUSTED = (
    "this attempt has used its {cap} agent runs — finish it manually: reject the step you "
    "disagree with, then commit your own actions on that branch (each one is replay-validated "
    "before it is kept)"
)


class CapExceeded(RuntimeError):
    """The attempt has used its agent runs. Counted on the ATTEMPT so sibling
    branches cannot reset it by forking."""


def spent(db: Session, attempt: models.ReviewSession) -> int:
    """Runs this attempt has already spent: the ones charged, plus the ones still
    in flight.

    The counter on the attempt only moves when a job reaches a terminal state,
    and the API returns while the job is still QUEUED — `jobs.store.submit`
    hands the work to a thread and returns immediately (app/jobs.py:80). Reading
    the counter alone therefore lets three requests in a row all pass a cap of
    three while it still says zero. A queued or running job holds its run until
    it either lands or is refunded.
    """
    reserved = db.scalar(
        select(func.count())
        .select_from(models.AgentRunJob)
        .where(
            models.AgentRunJob.attempt_id == attempt.id,
            models.AgentRunJob.status.in_((QUEUED, RUNNING)),
            models.AgentRunJob.counts_against_cap.is_(True),
        )
    )
    return int(attempt.agent_call_count or 0) + int(reserved or 0)


def remaining(db: Session, attempt: models.ReviewSession, *, max_calls: int | None) -> int | None:
    """How many runs are left, or None when no cap is configured.

    Exists so the number an annotator is shown is computed the same way as the
    one `enqueue` enforces. A budget they can only discover by being refused is
    how they get stranded halfway through an attempt.
    """
    if not max_calls:
        return None
    return max(0, int(max_calls) - spent(db, attempt))


def _charge(db: Session, job: models.AgentRunJob, attempt: models.ReviewSession | None) -> None:
    """Spend one run for this job, on its FIRST terminal transition only.

    A job ends once, but the code reporting it does not: the reaper can fail a
    job whose worker later reports it done, and an error can be reported twice.
    Charging per report would spend runs the annotator never received.
    """
    if job.status in TERMINAL or attempt is None:
        return
    versions.count_agent_call(db, attempt)


def find_by_key(db: Session, attempt_id: UUID, key: str) -> models.AgentRunJob | None:
    if not key:
        return None
    return db.scalar(
        select(models.AgentRunJob).where(
            models.AgentRunJob.attempt_id == attempt_id, models.AgentRunJob.idempotency_key == key
        )
    )


def enqueue(
    db: Session,
    *,
    attempt: models.ReviewSession,
    source_version: models.TrajectoryVersion,
    step: models.TrajectoryStep,
    mode: str = "before",
    correction: str = "",
    agent: str = "llm",
    created_by_id: UUID | None = None,
    idempotency_key: str = "",
    max_calls: int | None = None,
) -> tuple[models.AgentRunJob, models.TrajectoryVersion]:
    """Create the candidate child + the job bound to the EXACT source it forked
    from. Returns the existing job unchanged when the key was already used — a
    retried request must not spawn a second run or burn a second call."""
    dup = find_by_key(db, attempt.id, idempotency_key)
    if dup is not None:
        child = db.get(models.TrajectoryVersion, dup.result_version_id) if dup.result_version_id else None
        return dup, child

    if max_calls is not None:
        # Serialize the budget check on the attempt row: two requests that read it
        # at the same instant would both see the last run free and both take it.
        # No-op on SQLite; enforced on Postgres — the same lock the mutating
        # session endpoints take (app/api/sessions.py `_get_session`).
        db.execute(select(models.ReviewSession.id).where(models.ReviewSession.id == attempt.id).with_for_update())
        if spent(db, attempt) >= max_calls:
            raise CapExceeded(EXHAUSTED.format(cap=max_calls))

    make = versions.fork_before if mode == "before" else versions.continue_after
    child = make(
        db, parent=source_version, step=step, kind=versions.CORRECTION,
        producer=agent, model_config={"agent": agent, "correction": correction},
        created_by_id=created_by_id,
    )
    job = models.AgentRunJob(
        attempt_id=attempt.id,
        source_version_id=source_version.id,
        source_checkpoint_id=child.fork_checkpoint_id,
        expected_attempt_revision=attempt.revision,
        result_version_id=child.id,
        status=QUEUED,
        idempotency_key=idempotency_key,
        counts_against_cap=True,
    )
    db.add(job)
    db.flush()
    return job, child


def start(db: Session, job: models.AgentRunJob, *, owner: str = "") -> None:
    """Mark a job running — but never resurrect one that already ended.

    `_charge` spends a run only on the FIRST terminal transition, and it detects
    that by the job not already being terminal. Writing RUNNING over a finished
    status erases the evidence, so the next completion charges a second time for
    a run the annotator only ever received once. The reaper failing a job whose
    worker then reports in is not hypothetical — that ordering is precisely what
    the charge-once guard exists for.
    """
    if job.status in TERMINAL:
        return
    job.status = RUNNING
    job.owner = owner
    job.heartbeat_at = datetime.utcnow()
    job.provider_request_started_at = datetime.utcnow()
    db.flush()


def heartbeat(db: Session, job: models.AgentRunJob) -> None:
    job.heartbeat_at = datetime.utcnow()
    db.flush()


def fail(db: Session, job: models.AgentRunJob, error: str, *, infrastructure: bool = False) -> None:
    """A confirmed INFRASTRUCTURE failure must not burn one of the annotator's
    runs — they did not get a model attempt out of it.

    Anything else did give them one, so it is charged HERE rather than left to
    `complete`, which a failed job never reaches. Marking the job as counting and
    then never moving the counter would hand back a free run every time the model
    itself failed, and the cap would not hold.
    """
    if not infrastructure:
        _charge(db, job, db.get(models.ReviewSession, job.attempt_id))
    job.status = ERROR
    job.error = error[:2000]
    job.counts_against_cap = not infrastructure
    db.flush()


def complete(
    db: Session,
    job: models.AgentRunJob,
    *,
    attempt: models.ReviewSession,
    child: models.TrajectoryVersion,
    steps: list[dict],
    trajectory_id: UUID,
    guidance: str = "",
    guidance_author_id: UUID | None = None,
) -> models.TrajectoryVersion:
    """Persist the run's steps as the child's suffix and mark the job done.

    Deliberately does NOT touch `attempt.active_version_id`: selection is a
    separate, versioned command.
    """
    now = datetime.utcnow()
    prev_cp = db.get(models.EnvironmentCheckpoint, job.source_checkpoint_id) if job.source_checkpoint_id else None
    for st in steps:
        after_cp = None
        if st.get("world_after"):
            after_cp = checkpoints.capture(
                db, attempt_id=attempt.id, world=st["world_after"],
                backend_state=st.get("snapshot_after") or {},
                step_clock=(prev_cp.step_clock + 1) if prev_cp is not None else 0,
                browser={"url": st.get("url_after") or ""},
            )
        versions.append_step(
            db, child, trajectory_id=trajectory_id, actor="agent",
            action_type=st.get("action_kind") or st.get("type") or "",
            description=st.get("description", ""),
            reasoning=(st.get("reasoning") or "").strip(),
            url_after=st.get("url_after") or st.get("url") or "",
            screenshot_url=st.get("image") or "",
            arguments=st.get("action_args") or {},
            # Derive the locator from the recorded selector. Without it the step
            # has no replayable target, finalization refuses the whole trajectory,
            # and an agent-assisted correction can be created, selected and
            # approved but never actually shipped — the platform's primary
            # workflow, dead at the last gate.
            semantic_locator=recorder.locator_from_selector((st.get("action_args") or {}).get("selector", "")),
            world_after=st.get("world_after") or None,
            before_checkpoint_id=prev_cp.id if prev_cp is not None else None,
            after_checkpoint_id=after_cp.id if after_cp is not None else None,
            # The reviewer's instruction is provenance on every step it produced —
            # never reconstructed afterwards as if the agent had reasoned it.
            guidance_text=guidance,
            guidance_author_id=guidance_author_id,
            intervention_at=now if guidance else None,
        )
        prev_cp = after_cp or prev_cp

    if job.counts_against_cap:
        _charge(db, job, attempt)
    job.status = DONE
    job.result_version_id = child.id
    db.flush()
    return child


def reap_stale(db: Session, *, older_than: datetime) -> int:
    """A worker that died mid-run leaves a job RUNNING forever, which would block
    the attempt behind a job nobody is executing.

    A job nobody ever picked up strands the annotator the same way, and has no
    heartbeat to go stale: it sits QUEUED holding one of their runs against the
    cap for good. Both failures are ours rather than theirs, so both are refunded.
    """
    rows = db.scalars(
        select(models.AgentRunJob).where(
            or_(
                and_(models.AgentRunJob.status == RUNNING, models.AgentRunJob.heartbeat_at < older_than),
                and_(models.AgentRunJob.status == QUEUED, models.AgentRunJob.created_at < older_than),
            )
        )
    ).all()
    for j in rows:
        fail(
            db, j,
            "worker stopped heartbeating" if j.status == RUNNING else "no worker ever picked this run up",
            infrastructure=True,
        )
    return len(rows)
