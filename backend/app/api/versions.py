"""Version-graph endpoints — the annotator's view of an attempt's lineage.

Every mutating call is explicit and versioned: a fork creates a CANDIDATE, and
selecting it is a separate compare-and-swap. Nothing here advances an attempt's
head as a side effect, which is what keeps a slow agent run from resurrecting a
branch the annotator already moved past.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import (
    agent_runs, blobstore, bridge_client, canonical, checkpoints, cua_hub, finalize, gym_client, jobs,
    live_world, materialize, models, recorder, replay, replay_surface, versions, workspace,
)
from app import gym_review
from app.api import live as live_api
from app.api import sessions as sessions_api
from app.api.sessions import _assert_not_submitted, _latest_suite, _owned_session
from app.auth import current_annotator
from app.config import settings
from app.db import SessionLocal, get_db

router = APIRouter(prefix="/api", tags=["versions"])


def _live_world(db: Session, attempt: models.ReviewSession):
    """The attempt's world, for an operation that will MUTATE it.

    A bridged attempt whose pane is closed has handed its gym back to the pool,
    so it has no world at all — and the gym it used now belongs to whoever leased
    it next. Refusing here is what stops finalize / certify / commit from
    replaying into a stranger's world. Read-only callers use `world_for`
    directly and simply see None.
    """
    world = live_world.world_for(db, attempt)
    if getattr(world, "kind", "") == "unleased":
        raise HTTPException(
            status_code=409,
            detail="this attempt's gym is not open — reopen the task, then try again",
        )
    return world


def _version(db: Session, attempt: models.ReviewSession, version_id: UUID) -> models.TrajectoryVersion:
    v = db.get(models.TrajectoryVersion, version_id)
    if v is None or v.attempt_id != attempt.id:
        raise HTTPException(status_code=404, detail="version not found on this attempt")
    return v


def _step(db: Session, attempt: models.ReviewSession, step_id: UUID) -> models.TrajectoryStep:
    st = db.get(models.TrajectoryStep, step_id)
    if st is None:
        raise HTTPException(status_code=404, detail="step not found")
    traj = db.get(models.Trajectory, st.trajectory_id)
    if traj is None or traj.session_id != attempt.id:
        raise HTTPException(status_code=404, detail="step not found on this attempt")
    return st


def _describe(db: Session, v: models.TrajectoryVersion, head_id: UUID | None) -> dict:
    return {
        "id": str(v.id),
        "versionNo": v.version_no,
        "parentId": str(v.parent_version_id) if v.parent_version_id else None,
        "kind": v.kind,
        "status": v.status,
        "revision": v.revision,
        "producer": v.producer,
        "forkBeforeStepId": str(v.fork_before_step_id) if v.fork_before_step_id else None,
        "forkCheckpointId": str(v.fork_checkpoint_id) if v.fork_checkpoint_id else None,
        "isHead": v.id == head_id,
        "stepCount": len(versions.flatten(db, v)),
        "createdAt": v.created_at.isoformat(),
    }


@router.get("/sessions/{session_id}/versions")
def list_versions(
    session_id: UUID, current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db)
) -> dict:
    s = _owned_session(db, session_id, current)
    rows = versions.versions_for(db, s.id)
    return {
        "attemptId": str(s.id),
        "revision": s.revision,
        "headVersionId": str(s.active_version_id) if s.active_version_id else None,
        "agentCallCount": s.agent_call_count,
        "versions": [_describe(db, v, s.active_version_id) for v in rows],
        "verdicts": versions.verdicts_for(db, s.id),
    }


@router.get("/sessions/{session_id}/versions/{version_id}/steps")
def version_steps(
    session_id: UUID, version_id: UUID,
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    """The FLATTENED step list — inherited prefix plus this version's own suffix,
    with display numbers computed here (they are not identity)."""
    s = _owned_session(db, session_id, current)
    v = _version(db, s, version_id)
    verdicts = versions.verdicts_for(db, s.id)
    steps = versions.flat_view(db, v)
    for st in steps:
        st["verdict"] = verdicts.get(st["stepId"], {}).get("verdict", "pending")
    return {"versionId": str(v.id), "versionNo": v.version_no, "steps": steps}


class ForkBody(BaseModel):
    parentVersionId: UUID
    stepId: UUID
    mode: str = "before"  # before = reject this step | after = keep it and continue
    kind: str = versions.CORRECTION
    producer: str = ""


@router.post("/sessions/{session_id}/versions/fork")
def fork(
    session_id: UUID, body: ForkBody,
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    """Branch from a step. `before` rejects it (it will NOT appear in the child);
    `after` keeps it and resumes from the state it produced."""
    s = _owned_session(db, session_id, current)
    parent = _version(db, s, body.parentVersionId)
    step = _step(db, s, body.stepId)
    if body.mode not in ("before", "after"):
        raise HTTPException(status_code=422, detail="mode must be 'before' or 'after'")
    make = versions.fork_before if body.mode == "before" else versions.continue_after
    try:
        child = make(db, parent=parent, step=step, kind=body.kind, producer=body.producer, created_by_id=current.id)
    except versions.LineageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.add(models.AuditLog(
        session_id=s.id, actor=current.email, action="version.fork", target=str(child.id),
        meta={"parent": str(parent.id), "mode": body.mode, "step": str(step.id), "versionNo": child.version_no},
    ))
    db.commit()
    return _describe(db, child, s.active_version_id)


class SelectBody(BaseModel):
    versionId: UUID
    expectedRevision: int


@router.post("/sessions/{session_id}/versions/select")
def select_version(
    session_id: UUID, body: SelectBody,
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    """Advance the attempt HEAD. Compare-and-swap against the revision the client
    last read — a stale client gets 409 and reloads instead of clobbering."""
    s = _owned_session(db, session_id, current, lock=True)
    v = _version(db, s, body.versionId)
    try:
        rev = versions.set_head(db, s, v, expected_revision=body.expectedRevision)
    except versions.ConcurrencyError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.add(models.AuditLog(session_id=s.id, actor=current.email, action="version.select", target=str(v.id), meta={"revision": rev}))
    db.commit()
    return {"headVersionId": str(v.id), "revision": rev}


class StatusBody(BaseModel):
    status: str
    expectedRevision: int


@router.post("/sessions/{session_id}/versions/{version_id}/status")
def set_version_status(
    session_id: UUID, version_id: UUID, body: StatusBody,
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    """QC decision on a candidate (§8.5). Content never changes — only status."""
    s = _owned_session(db, session_id, current)
    v = _version(db, s, version_id)
    try:
        rev = versions.set_status(db, v, body.status, expected_revision=body.expectedRevision)
    except versions.ConcurrencyError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.add(models.AuditLog(session_id=s.id, actor=current.email, action="version.status", target=str(v.id), meta={"status": body.status}))
    db.commit()
    return {"versionId": str(v.id), "status": v.status, "revision": rev}


class VerdictBody(BaseModel):
    stepId: UUID
    verdict: str
    note: str = ""


@router.post("/sessions/{session_id}/steps/verdict")
def step_verdict(
    session_id: UUID, body: VerdictBody,
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    """Verify or reject one step, keyed by its stable id so the verdict survives a
    re-fork (the old scalar `reviewed_through` could not)."""
    s = _owned_session(db, session_id, current)
    if body.verdict not in ("pending", "verified", "rejected"):
        raise HTTPException(status_code=422, detail="verdict must be pending|verified|rejected")
    step = _step(db, s, body.stepId)
    row = versions.set_verdict(
        db, attempt_id=s.id, step_id=step.id, verdict=body.verdict, note=body.note, annotator_id=current.id
    )
    db.commit()
    return {"stepId": str(step.id), "verdict": row.verdict, "note": row.note}


@router.post("/sessions/{session_id}/versions/baseline")
def ensure_baseline(
    session_id: UUID,
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    """Materialize v1 for this attempt from the canonical recorded run. Idempotent,
    so the client can call it whenever it opens a task."""
    s = _owned_session(db, session_id, current)
    # Never clone a recorded AGENT run on top of the annotator's own work. When
    # the live browser opens it mints the attempt's v1 by hand (`human_manual`);
    # baselining after that would fall through to `canonical.for_attempt` (the
    # breaker's recorded agent run) and put a stranger's 13-step trajectory ahead
    # of the annotator's — the "agent run reappeared" bug. If a human root already
    # exists, refuse. (Keyed on the root's kind, not the attempt's mode, because
    # mode is only set once the live browser opens — this guards the whole window
    # and leaves the legacy version-graph setup, which baselines a fresh attempt,
    # untouched.)
    manual_root = db.scalar(
        select(models.TrajectoryVersion).where(
            models.TrajectoryVersion.attempt_id == s.id,
            models.TrajectoryVersion.parent_version_id.is_(None),
            models.TrajectoryVersion.kind == versions.MANUAL,
        )
    )
    if manual_root is not None:
        raise HTTPException(
            status_code=409,
            detail="this attempt is performed by hand — its v1 is the annotator's own trajectory, not a baseline",
        )
    base = db.scalar(
        select(models.Trajectory)
        .where(models.Trajectory.session_id == s.id)
        .order_by(models.Trajectory.created_at.asc())
    )
    if base is None:
        base = canonical.for_attempt(db, s)
    if base is None:
        raise HTTPException(status_code=409, detail="this attempt has no recorded run to baseline from")
    v1 = versions.ensure_root(db, s, base)
    db.commit()
    return _describe(db, v1, s.active_version_id)


# --------------------------------------------------------------------------- finalization
class GymScorer:
    """Scores the bound suite from the gym's REAL milestone verdict, read against
    the world the replay ended in — not from a re-derived guess."""

    def __init__(self, gym) -> None:
        self.gym = gym

    def score(self, suite: models.VerifierSuite, world: dict | None) -> tuple[int, dict]:
        verdict = self.gym.verify(0) or {}
        results = {m.get("id"): ("pass" if m.get("passed") else "fail")
                   for m in (verdict.get("milestones") or []) if m.get("id")}
        if not results:  # no milestone detail — fall back to the suite's own ids
            passed = bool(verdict.get("success"))
            results = {v.ext_id: ("pass" if passed else "fail") for v in suite.verifiers}
        return (1 if verdict.get("success") else 0), results


@router.post("/sessions/{session_id}/suite/from-autogen")
def suite_from_autogen(
    session_id: UUID,
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    """Adopt the generated suite for this attempt's task.

    The oracle loop produced a validated suite and handed it back as JSON, where
    it stopped: nothing wrote it to an attempt, so reaching a reward from it meant
    retyping every check by hand. This copies the cached one onto the attempt as a
    real suite version.

    Marked `addedByHuman=False`, so an exported sample says plainly which checks a
    person wrote and which a model did — that provenance is the difference between
    human-authored ground truth and a model grading itself.
    """
    s = _owned_session(db, session_id, current)
    _assert_not_submitted(s)
    task = db.get(models.Task, s.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    row = db.scalar(
        select(models.AutogenSuite)
        .where(models.AutogenSuite.task_id == task.id, models.AutogenSuite.seed == s.seed)
    )
    if row is None or not (row.checks or []):
        raise HTTPException(
            status_code=409,
            detail="no generated suite for this task yet — run “Auto-generate verifiers” first",
        )

    suite = sessions_api.write_suite(db, s.id, list(row.checks or []))
    db.add(models.AuditLog(
        session_id=s.id, actor=current.email, action="suite.from_autogen", target=str(suite.id),
        meta={"version": suite.version, "count": len(row.checks or []), "oracle": row.oracle},
    ))
    db.commit()
    return {
        "suiteId": str(suite.id), "version": suite.version,
        "oracle": row.oracle,
        "verifiers": [
            {"id": v.get("id"), "level": v.get("level"), "assertion": v.get("assertion"),
             "code": v.get("code"), "check": v.get("check"),
             "failsUntilCorrected": False, "placeholder": False, "addedByHuman": False}
            for v in (row.checks or [])
        ],
    }


@router.post("/sessions/{session_id}/prepare-ship")
def prepare_ship(
    session_id: UUID,
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    """Assemble what an attempt needs to ship, and say what is still missing.

    A human-do attempt could record a perfect trajectory and still have no way to
    ship it: finalize needs an approved version AND a verifier suite, and nothing
    in the live path created either. The annotator was left to discover that one
    409 at a time — from a button that looked like it had worked, because the
    client collapsed every error to null.

    Idempotent, and safe to call on every render. It folds any pending
    interactions, resolves the head version, gives the attempt a suite if it has
    none (the gym's own milestones — the same verdict finalize scores against),
    and returns every unmet gate rather than the first.
    """
    s = _owned_session(db, session_id, current)
    _assert_not_submitted(s)

    # Fold anything still unfolded, so the last few actions are in the version
    # the annotator is about to ship rather than arriving after it.
    with contextlib.suppress(Exception):
        materialize.materialize(db, s, world=live_world.world_for(db, s))

    v = versions.head(db, s)
    suite = _latest_suite(db, s.id)

    # No suite, or an empty one: derive it from the gym's own milestones. That is
    # already what the reward is computed from, so this makes explicit the suite
    # the attempt was going to be scored against anyway rather than inventing one.
    created_suite = False
    if v is not None and (suite is None or not (suite.verifiers or [])):
        milestones = []
        with contextlib.suppress(Exception):
            world = live_world.world_for(db, s)
            verdict = world.verify(0) if hasattr(world, "verify") else None
            # The gym names this `all_milestones`; the bridge passes the verdict
            # through unchanged. Reading `milestones` found nothing, so the
            # fallback silently never fired and every attempt still reported
            # "no verifier suite" — a miss only a full-stack run could show.
            milestones = list((verdict or {}).get("all_milestones")
                              or (verdict or {}).get("milestones") or [])
        if milestones:
            suite = models.VerifierSuite(
                session_id=s.id, version=((suite.version + 1) if suite else 1))
            db.add(suite)
            db.flush()
            for m in milestones:
                db.add(models.Verifier(
                    suite_id=suite.id,
                    ext_id=str(m.get("id") or m.get("name") or ""),
                    level=gym_review._level(m),
                    assertion=str(m.get("description") or m.get("name") or ""),
                    code="",
                    # Named, not executable: the gym owns this verdict, and
                    # pretending we can re-run it here would be a second, weaker
                    # copy of the check.
                    check_ir={"kind": "gym_milestone", "id": m.get("id") or m.get("name")},
                    added_by_human=False,
                    # The gym reports firing, not a verdict string — and a
                    # FORBIDDEN milestone passes by NOT firing, so this cannot be
                    # read off `fired_at_step` naively.
                    gym_result=gym_review._milestone_result(m),
                ))
            created_suite = True
            db.add(models.AuditLog(
                session_id=s.id, actor=current.email, action="suite.from_milestones",
                target=str(suite.id), meta={"count": len(milestones)}))
    db.commit()

    blockers = finalize.gate_report(db, s, v, suite)
    return {
        "versionId": str(v.id) if v else None,
        "versionNo": v.version_no if v else None,
        "suiteId": str(suite.id) if suite else None,
        "suiteCreated": created_suite,
        "verifiers": [
            {"id": x.ext_id, "level": x.level, "assertion": x.assertion,
             "gymResult": x.gym_result, "addedByHuman": x.added_by_human}
            for x in (suite.verifiers if suite else [])
        ],
        "blockers": blockers,
        "canShip": not blockers,
    }


class FinalizeBody(BaseModel):
    versionId: UUID
    suiteId: UUID | None = None
    # Shipping a run that fails its own suite has to be deliberate, exactly as the
    # legacy path required an override on the record.
    acceptFailing: bool = False
    # `kind` is deliberately NOT accepted. It is derived server-side from the
    # score and the overridden verifiers, so a client cannot label a run that
    # only passed by overriding a safety check as clean training gold.


@router.post("/sessions/{session_id}/finalize")
def finalize_attempt(
    session_id: UUID, body: FinalizeBody,
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    """Ship an approved version: clean replay, score against the bound suite,
    freeze the deliverable. Refuses rather than shipping something unbound."""
    s = _owned_session(db, session_id, current, lock=True)
    # A submitted attempt is immutable — every other mutating endpoint asserts
    # this, and finalize writes a Submission, a BenchmarkRun and a version status.
    _assert_not_submitted(s)
    v = _version(db, s, body.versionId)
    suite = (
        db.get(models.VerifierSuite, body.suiteId) if body.suiteId
        else db.scalar(
            select(models.VerifierSuite)
            .where(models.VerifierSuite.session_id == s.id)
            .order_by(models.VerifierSuite.version.desc())
        )
    )
    if suite is None:
        raise HTTPException(status_code=409, detail="this attempt has no verifier suite to score against")

    task = db.get(models.Task, s.task_id)
    # Finalization does a CLEAN reset and replays the whole trajectory, so it must
    # run somewhere throwaway AND on the surface the steps were recorded on. It
    # used to do neither: it took the attempt's own world (so the reset wiped the
    # annotator's work before reading it) and opened the browser on the gym's own
    # pages (so a bridged attempt's mock-DOM locators could never resolve, and
    # nothing could ship at all).
    with replay_surface.scratch_surface(db, s, task, purpose="finalize") as surface:
        endpoint = surface.world
        live_sid, live_ticket = live_api.open_scratch_browser(surface.start_url, current.email)
        live = gym_client.LiveBrowserClient(
            base_url=settings.live_browser_url, session_id=live_sid, ticket=live_ticket, gym=endpoint,
        )
        try:
            out = finalize.finalize(
                db, attempt=s, version=v, suite=suite, executor=live, gym=endpoint,
                scorer=GymScorer(endpoint), annotator_id=current.id,
                accept_failing=body.acceptFailing,
                task_external_id=task.external_id if task else "",
                rewrite=surface.rewrite,
            )
        except finalize.NotApproved as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except replay.ReplayRejected as exc:
            raise HTTPException(status_code=422, detail={
                "error": "the approved version does not replay cleanly", "at": exc.at, "reason": exc.reason,
            }) from exc
        except versions.ConcurrencyError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            live_api.close_scratch_browser(live_sid)
            # A NON-bridged attempt's scratch surface IS its own gym (that is the
            # surface it was recorded on), so there the replayed world does land
            # in its workspace. Forget the seed marker: on success the lease is
            # released below and the marker goes with the row, but on FAILURE the
            # workspace survives holding a replayed world under a marker that
            # would otherwise vouch for it as the annotator's own. One extra
            # reseed is the right price for never handing back a world we did not
            # build for them.
            if not surface.bridged:
                with contextlib.suppress(Exception):
                    workspace.clear_seed_mark(db, workspace.active_lease(db, s.id))

    db.add(models.AuditLog(
        session_id=s.id, actor=current.email, action="attempt.finalize", target=out["submissionId"],
        meta={"versionId": out["versionId"], "reward": out["reward"], "steps": out["steps"]},
    ))
    s.status = "submitted"
    db.commit()

    # The attempt is over, so give its gym back. This — not closing the live
    # browser — is the right release point. Closing a pane is not a statement
    # that the attempt is done: an annotator flips to the replay view and back
    # constantly, and tearing the workspace down each time would mean a container
    # boot on every toggle and, under the per-annotator cap, churn against a
    # limit meant to bound genuinely concurrent work. Submission is the one
    # moment the world is provably finished with.
    #
    # Note what this does NOT yet buy: open_live_session reseeds the gym on every
    # fresh open, so reopening a pane still returns the world to the task seed
    # even though the container survived. Preserving hand-driven world state
    # across a reopen needs the recorded prefix replayed back in, which is not
    # wired up — see ROADMAP 2.3.
    #
    # Best-effort: the sample has shipped, and a leaked container is the reaper's
    # problem, not a reason to fail a successful finalize.
    lease = workspace.active_lease(db, s.id)
    if lease is not None:
        with contextlib.suppress(Exception):
            workspace.release(db, lease)
    return out


# --------------------------------------------------------------------------- agent handoff
class AgentRunBody(BaseModel):
    parentVersionId: UUID
    stepId: UUID
    mode: str = "before"
    correction: str = ""
    agent: str = "llm"
    idempotencyKey: str = ""


@router.post("/sessions/{session_id}/versions/agent-run")
def start_agent_run(
    session_id: UUID, body: AgentRunBody,
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    """Hand a branch to a batch agent.

    The worker runs in its OWN gym process cloned from the fork checkpoint, so it
    can never reset the world out from under the annotator's live session. The
    result arrives as a CANDIDATE the human then chooses — they do not watch a
    batch agent drive their browser.
    """
    s = _owned_session(db, session_id, current)
    parent = _version(db, s, body.parentVersionId)
    step = _step(db, s, body.stepId)
    task = db.get(models.Task, s.task_id)
    try:
        job, child = agent_runs.enqueue(
            db, attempt=s, source_version=parent, step=step, mode=body.mode,
            correction=body.correction, agent=body.agent, created_by_id=current.id,
            idempotency_key=body.idempotencyKey, max_calls=settings.agent_run_cap or None,
        )
    except agent_runs.CapExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except versions.LineageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if job.status != agent_runs.QUEUED:  # an idempotent replay of a finished run
        db.commit()
        return {"jobId": str(job.id), "status": job.status,
                "versionId": str(child.id) if child else None, "replayed": True}

    db.add(models.AuditLog(
        session_id=s.id, actor=current.email, action="version.agent_run", target=str(child.id),
        meta={"parent": str(parent.id), "mode": body.mode, "correction": bool(body.correction)},
    ))
    db.commit()
    bg = jobs.store.submit(
        "agent-branch", _agent_branch_job, str(job.id), str(s.id), str(child.id),
        task.external_id if task else "", s.seed, body.agent, body.correction, str(current.id),
    )
    return {"jobId": bg.id, "runId": str(job.id), "versionId": str(child.id), "status": "queued"}


def _agent_branch_job(
    job_id: str, attempt_id: str, child_id: str, task_external_id: str,
    seed: int, agent: str, correction: str, author_id: str,
) -> dict:
    """Background: restore the fork checkpoint into an ISOLATED worker, drive the
    agent forward under the correction, and persist the steps as the candidate's
    suffix. Never touches the attempt head."""
    from app.api.gym import _agent_workspace

    with SessionLocal() as db:
        job = db.get(models.AgentRunJob, UUID(job_id))
        attempt = db.get(models.ReviewSession, UUID(attempt_id))
        child = db.get(models.TrajectoryVersion, UUID(child_id))
        cp = db.get(models.EnvironmentCheckpoint, job.source_checkpoint_id) if job.source_checkpoint_id else None
        start_world = dict(cp.world or {}) if cp is not None else {}
        start_url = cp.url if cp is not None else "/"
        start_step = cp.step_clock if cp is not None else None
        agent_runs.start(db, job, owner="api")
        db.commit()

    try:
        with _agent_workspace(attempt_id) as gym:
            r = gym.resume_run(task_external_id, seed, start_world, start_url, start_step, agent, correction=correction)
            post_world = gym.world() if r is not None else None
    except Exception as exc:  # noqa: BLE001
        with SessionLocal() as db:
            agent_runs.fail(db, db.get(models.AgentRunJob, UUID(job_id)), f"{type(exc).__name__}: {exc}", infrastructure=True)
            db.commit()
        raise jobs.JobFailure("the branch worker could not be driven") from exc

    steps = ((r or {}).get("trajectory") or {}).get("steps") or []
    with SessionLocal() as db:
        job = db.get(models.AgentRunJob, UUID(job_id))
        attempt = db.get(models.ReviewSession, UUID(attempt_id))
        child = db.get(models.TrajectoryVersion, UUID(child_id))
        if r is None:
            # Unreachable gym is OURS, not the annotator's — don't burn a run.
            agent_runs.fail(db, job, "gym unreachable or resume failed", infrastructure=True)
            db.commit()
            raise jobs.JobFailure("gym unreachable or resume failed")
        traj = _attempt_trajectory(db, attempt)
        agent_runs.complete(
            db, job, attempt=attempt, child=child, steps=steps, trajectory_id=traj.id,
            guidance=correction, guidance_author_id=UUID(author_id) if author_id else None,
        )
        out = {"versionId": str(child.id), "steps": len(steps),
               "worldHash": checkpoints.hash_world(post_world)}
        db.commit()
    return out


@router.get("/sessions/{session_id}/runs")
def list_runs(
    session_id: UUID,
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    s = _owned_session(db, session_id, current)
    rows = db.scalars(
        select(models.AgentRunJob)
        .where(models.AgentRunJob.attempt_id == s.id)
        .order_by(models.AgentRunJob.created_at.desc())
    ).all()
    return {"agentCallCount": s.agent_call_count, "cap": settings.agent_run_cap or None, "runs": [
        {"id": str(j.id), "status": j.status, "sourceVersionId": str(j.source_version_id) if j.source_version_id else None,
         "resultVersionId": str(j.result_version_id) if j.result_version_id else None,
         "countsAgainstCap": j.counts_against_cap, "error": j.error,
         "createdAt": j.created_at.isoformat()}
        for j in rows
    ]}


# --------------------------------------------------------------------------- manual capture
class EventBody(BaseModel):
    kind: str
    payload: dict = {}
    target: dict = {}
    url: str = ""
    tab: str = ""
    clientEventId: str | None = None


@router.post("/sessions/{session_id}/events")
def record_events(
    session_id: UUID, body: list[EventBody],
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    """Append raw interactions. Exploration is recorded here and NEVER becomes a
    step on its own — that separation is what lets an annotator look around
    freely without polluting the golden."""
    s = _owned_session(db, session_id, current)
    # A submitted attempt is frozen; appending to it is a silent integrity hole.
    _assert_not_submitted(s)
    recorded = duplicates = 0
    for e in body:
        ev = recorder.record_event(
            db, attempt_id=s.id, kind=e.kind, payload=e.payload,
            target=e.target, url=e.url, tab=e.tab, actor="human",
            client_event_id=e.clientEventId,
        )
        if ev is None:
            duplicates += 1     # already recorded — a retry, not a new interaction
        else:
            recorded += 1
    # Fold the settled prefix into steps in the SAME request. The annotator sees
    # their work become a trajectory as they do it, rather than having to hand-pick
    # actions from a list afterwards — and because the watermark advances in this
    # transaction, running it again produces nothing new.
    # Only the human-do flow folds interactions into steps automatically; in the
    # agent-review flow the annotator's clicking is exploration and must not touch
    # the agent's trajectory.
    steps = (materialize.materialize(db, s, world=live_world.world_for(db, s))
             if materialize.should_materialize(s) else [])
    db.commit()
    return {"recorded": recorded, "duplicates": duplicates, "steps": len(steps)}


class CertifyBody(BaseModel):
    versionId: UUID | None = None


@router.post("/sessions/{session_id}/certify")
def certify(
    session_id: UUID, body: CertifyBody,
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    """Prove the recorded steps actually replay — WITHOUT touching the annotator's world.

    Recording a step and proving it are separate claims, and conflating them is
    what made the old `/commit` painful: it restored a checkpoint into the LIVE
    environment, destroying the working state of whoever was mid-task, so it could
    only ever run once, at the end, and one bad action discarded the whole
    sequence.

    This runs against a SCRATCH browser instead. So it is repeatable, safe to run
    mid-task ("is my work still good?"), and non-destructive: a step that does not
    replay is MARKED `diverged`, never deleted. The annotator fixes that step.
    """
    s = _owned_session(db, session_id, current)
    v = _version(db, s, body.versionId) if body.versionId else versions.head(db, s)
    if v is None:
        raise HTTPException(status_code=409, detail="this attempt has no trajectory to certify")

    steps = [st for st in versions.flatten(db, v) if st.actor == "human"]
    if not steps:
        return {"ok": True, "steps": [], "certified": 0, "firstFailureAt": None}

    # A redacted value was never persisted, so replaying it would type the
    # placeholder into the field. Refuse rather than certify a green-but-broken
    # golden — the annotator supplies the value explicitly.
    blocked = [st for st in steps if st.replay_state == "needs_value"]

    task = db.get(models.Task, s.task_id)
    actions = [{
        "kind": st.action_type,
        "locator": st.semantic_locator or {},
        "args": st.arguments or {},
    } for st in steps]

    start = db.get(models.EnvironmentCheckpoint, v.fork_checkpoint_id) if v.fork_checkpoint_id else None
    live_id = ticket = ""
    # A scratch WORLD as well as a scratch browser — replaying restores a
    # checkpoint, and doing that in the annotator's own gym rewinds them mid-task.
    with replay_surface.scratch_surface(db, s, task, purpose="certify") as surface:
        world = surface.world
        try:
            # Open where the steps were RECORDED. A bridged attempt's locators are
            # captured from the mock SPA's DOM, so opening the gym's own pages
            # meant not one of them could resolve and certify reported every step
            # diverged however good the trajectory was.
            live_id, ticket = live_api.open_scratch_browser(surface.start_url, current.email)
            executor = gym_client.LiveBrowserClient(
                base_url=settings.live_browser_url, session_id=live_id, ticket=ticket, gym=world,
            )
            result = replay.restore_and_replay(
                start, [surface.rewrite(a) for a in actions], executor, world,
                task_id=task.external_id if task else "", seed=s.seed,
                # The per-action world hashes recorded at materialise time. Passing
                # them is what makes the comparison in replay() live rather than dead
                # code — without it the only check is "did the action land".
                expected_hashes=[checkpoints.hash_world(st.world_after) if st.world_after else ""
                                 for st in steps],
                strict=False,   # report EVERY problem, not just the first
            )
        except replay.ReplayRejected as exc:
            result = None
            rejected_at, reason = exc.at, str(exc)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=409, detail=f"could not certify: {exc}") from exc
        else:
            rejected_at, reason = result.rejected_at, result.reason
        finally:
            if live_id:
                with contextlib.suppress(Exception):
                    live_api.close_scratch_browser(live_id)

    outcomes = (result.steps if result else []) or []
    for i, st in enumerate(steps):
        if st in blocked:
            continue
        out = outcomes[i] if i < len(outcomes) else None
        if out is None:
            st.replay_state = "unverified"
        elif out.get("ok"):
            st.replay_state = "verified"
            st.replay_error = ""
        else:
            st.replay_state = "diverged"
            st.replay_error = str(out.get("error") or reason or "did not replay")
    db.add(models.AuditLog(
        session_id=s.id, actor=current.email, action="version.certify", target=str(v.id),
        meta={"steps": len(steps), "firstFailureAt": rejected_at},
    ))
    db.commit()
    return {
        "ok": bool(result and result.ok) and not blocked,
        "certified": sum(1 for st in steps if st.replay_state == "verified"),
        "firstFailureAt": rejected_at,
        "needsValue": [str(st.id) for st in blocked],
        "steps": [{"stepId": str(st.id), "state": st.replay_state, "error": st.replay_error}
                  for st in steps],
    }


class FrameBody(BaseModel):
    """One frame the annotator actually saw, at the moment they acted."""
    clientEventId: str
    jpegBase64: str
    width: int = 0
    height: int = 0


@router.post("/sessions/{session_id}/frames")
def record_frames(
    session_id: UUID, body: list[FrameBody],
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    """Attach screenshots to recorded interactions.

    A SEPARATE endpoint from /events on purpose: a frame is ~60KB, and putting it
    in the event batch would both bloat every request and — because the recorder
    re-queues a failed batch whole — let one failed image upload replay the entire
    interaction stream. A screenshot is nice to have; the events are not.
    """
    s = _owned_session(db, session_id, current)
    _assert_not_submitted(s)
    stored = 0
    for f in body:
        ev = db.scalar(
            select(models.InteractionEvent).where(
                models.InteractionEvent.attempt_id == s.id,
                models.InteractionEvent.client_event_id == f.clientEventId,
            )
        )
        if ev is None:
            continue        # the frame outlived its event; nothing to hang it on
        try:
            raw = base64.b64decode(f.jpegBase64 or "", validate=False)
        except (ValueError, binascii.Error):
            continue
        if not raw:
            continue
        art = checkpoints.add_artifact(
            db, kind="screenshot", uri=f"attempt/{s.id}/{f.clientEventId}.jpg", data=raw,
            meta={"width": f.width, "height": f.height, "clientEventId": f.clientEventId},
        )
        payload = dict(ev.payload or {})
        payload["screenshotArtifactId"] = str(art.id)
        ev.payload = payload
        # If the event already became a step, give the step its picture too.
        if ev.committed_step_id:
            st = db.get(models.TrajectoryStep, ev.committed_step_id)
            if st is not None and not st.screenshot_url:
                st.screenshot_url = blobstore.api_url(art.id)
                st.marks_artifact_id = art.id
        stored += 1
    db.commit()
    return {"stored": stored}


@router.get("/sessions/{session_id}/actions")
def candidate_actions(
    session_id: UUID,
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    """Raw events folded into candidate ACTIONS (keystrokes → one fill, press +
    release → one click, automatic scrolls dropped). The human picks from these;
    nothing is committed automatically."""
    s = _owned_session(db, session_id, current)
    acts = recorder.candidate_actions(db, s.id)
    return {"actions": [
        {
            "sources": a.get("sources", []),
            "kind": a.get("kind"),
            "locator": recorder.semantic_locator(a.get("target")),
            "args": a.get("payload") or {},
            "url": a.get("url", ""),
        }
        for a in acts
    ]}


class CommitBody(BaseModel):
    actions: list[dict]          # the sequence the human chose to commit
    liveSessionId: str           # the live browser it is validated against
    ticket: str
    intents: list[str] = []      # per-action "why", authored by the human
    dryRun: bool = False


@router.post("/sessions/{session_id}/versions/{version_id}/commit")
def commit_actions(
    session_id: UUID, version_id: UUID, body: CommitBody,
    current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db),
) -> dict:
    """Validate a proposed sequence by REPLAY, then append it to the version.

    A proposal is a claim, not a fact: it routinely depends on state the
    exploration created and the commit discarded. Replay from the branch's
    starting checkpoint is what turns it into a fact — and a sequence that fails
    is rejected outright rather than committed with a warning, because a golden
    that "mostly replays" ships as ground truth and then doesn't reproduce.
    """
    s = _owned_session(db, session_id, current)
    v = _version(db, s, version_id)
    if not body.actions:
        raise HTTPException(status_code=422, detail="nothing to commit")

    # _live_world, not endpoint_for — see finalize_attempt. A bridged attempt that
    # committed against the shared gym would read and checkpoint someone else's world.
    endpoint = _live_world(db, s)
    live = gym_client.LiveBrowserClient(
        base_url=settings.live_browser_url, session_id=body.liveSessionId, ticket=body.ticket, gym=endpoint,
    )
    start = db.get(models.EnvironmentCheckpoint, v.fork_checkpoint_id) if v.fork_checkpoint_id else None
    task = db.get(models.Task, s.task_id)
    try:
        result = replay.restore_and_replay(
            start, body.actions, live, endpoint,
            task_id=task.external_id if task else "", seed=s.seed,
            strict=not body.dryRun,
        )
    except replay.ReplayRejected as exc:
        raise HTTPException(status_code=422, detail={
            "error": "the committed sequence does not replay", "at": exc.at, "reason": exc.reason,
        }) from exc
    except checkpoints.DivergenceError as exc:
        raise HTTPException(status_code=409, detail=f"could not restore the branch start: {exc}") from exc

    # NOTE: deliberately NOT clearing the workspace seed marker here. Committing
    # restores the fork checkpoint and replays the committed actions into this
    # attempt's own gym, so the world legitimately MOVES — but it is still this
    # attempt's world for this (task, seed), which is exactly what the marker
    # asserts. Reopening the pane onto fork+committed-actions is correct; it is
    # simply not the exploration that was in progress, which is why the pane
    # reports where its world came from rather than implying nothing moved.
    if body.dryRun or not result.ok:
        return {"ok": result.ok, "rejectedAt": result.rejected_at, "reason": result.reason,
                "steps": result.steps, "committed": 0}

    own = _attempt_trajectory(db, s)
    made = []
    for i, (a, outcome) in enumerate(zip(body.actions, result.steps)):
        st = versions.append_step(
            db, v, trajectory_id=own.id, actor="human",
            action_type=a.get("kind", ""),
            description=a.get("description", "") or f"{a.get('kind','')} {(a.get('locator') or {}).get('testId','')}".strip(),
            semantic_locator=a.get("locator") or {},
            resolved_target=outcome.get("resolved") or {},
            arguments=a.get("args") or {},
            url_after=(outcome.get("resolved") or {}).get("url", ""),
            human_intent=body.intents[i] if i < len(body.intents) else "",
        )
        made.append(st)
    # The end state is evidence: without it the next fork has nothing to start from.
    if result.final_world is not None and made:
        cp = checkpoints.capture(db, attempt_id=s.id, world=result.final_world, step_clock=len(made))
        made[-1].after_checkpoint_id = cp.id
    db.add(models.AuditLog(
        session_id=s.id, actor=current.email, action="version.commit", target=str(v.id),
        meta={"committed": len(made), "versionNo": v.version_no},
    ))
    db.commit()
    return {"ok": True, "committed": len(made), "versionId": str(v.id),
            "steps": versions.flat_view(db, v)}


def _attempt_trajectory(db: Session, s: models.ReviewSession) -> models.Trajectory:
    """The attempt's own trajectory row — human-authored steps hang off it rather
    than off the shared canonical gym run."""
    t = db.scalar(
        select(models.Trajectory)
        .where(models.Trajectory.session_id == s.id)
        .order_by(models.Trajectory.created_at.asc())
    )
    if t is None:
        t = models.Trajectory(session_id=s.id, agent="human", seed=s.seed, source="manual")
        db.add(t)
        db.flush()
    return t
