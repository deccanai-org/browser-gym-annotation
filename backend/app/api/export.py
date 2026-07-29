"""Sample packaging / export (JP + Nav's deliverable).

The platform's product is not the broken run — it's the GOLDEN sample: a broken
agent run driven to a passing end-state. This module assembles a completed
annotation into the deliverable bundle — the evaluation triplet (initial setup +
seeded data + golden environment) plus the golden trajectory, the verifier suite,
and the reward — and exports it as JSON per sample or JSONL for the whole dataset.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import canonical, models
from app.auth import require_reviewer
from app.db import get_db

router = APIRouter(prefix="/api/export", tags=["export"])


def _latest(db: Session, model, session_id: UUID, order):
    return db.scalar(select(model).where(model.session_id == session_id).order_by(order))


def _steps_of(traj: models.Trajectory | None) -> list[dict]:
    if traj is None:
        return []
    return [{
        "idx": s.idx, "type": s.action_type, "description": s.description,
        "tab": s.tab_id, "screenshot": s.screenshot_url or None,
    } for s in sorted(traj.steps, key=lambda x: x.idx)]


def _base_trajectory(db: Session, s: models.ReviewSession) -> models.Trajectory | None:
    """The run the annotator ACTUALLY reviewed.

    A fixture session owns its own Trajectory row. A GYM session does not — it
    reviews the shared canonical run, which is persisted on the SYSTEM gym session
    for the same task. Looking the trajectory up by the human session id alone
    therefore found nothing and shipped every gym sample with an empty
    recorded_trajectory (and a golden that was empty, or worse, only the correction
    tail starting at a non-zero index). Resolve the canonical run through the ONE
    resolver, so a shipped sample names the same run the annotator reviewed — this
    copy of the rule had already drifted (it carried no LIMIT where the other three
    did, so on the same data it could answer differently).
    """
    own = _latest(db, models.Trajectory, s.id, models.Trajectory.created_at.desc())
    if own is not None:
        return own
    return canonical.for_attempt(db, s)


def _versioned_sample(
    db: Session, s: models.ReviewSession, task, annotator, sub, frozen: dict,
    recorded: list[dict], seed_state: dict,
) -> dict:
    """The deliverable for a version-bound submission — assembled entirely from
    the frozen snapshot, so nothing it says can drift after it shipped."""
    tv = frozen["trajectory_version"]
    golden = frozen.get("golden_trajectory", [])
    # The correction is no longer a scalar fork index plus a text blob: it is the
    # lineage, and each step says who authored it and under whose instruction.
    corrections = [
        {"version_no": st_v["versionNo"], "kind": st_v["kind"], "producer": st_v["producer"]}
        for st_v in tv.get("lineage", []) if st_v["versionNo"] > 1
    ]
    return {
        "sample_id": str(s.id),
        "schema": "golden-sample/2",
        "task": {
            "id": task.external_id if task else None,
            "revision": frozen.get("task_revision", 1),
            "prompt": task.prompt if task else "",
            "category": task.category if task else "",
            "difficulty": task.difficulty if task else "",
            "constraints": (task.meta or {}).get("constraints", []) if task else [],
            "allowed_sites": (task.meta or {}).get("allowedSites", []) if task else [],
            "seed": task.seed if task else 0,
        },
        "initial_state": seed_state.get("world") or {k: v for k, v in seed_state.items() if k != "world"},
        "recorded_trajectory": recorded,
        "trajectory_version": {
            "id": tv["id"], "version_no": tv["versionNo"], "kind": tv["kind"],
            "environment_image_digest": tv.get("environment_image_digest", ""),
            "lineage": tv.get("lineage", []),
        },
        "corrections": corrections,
        "golden_trajectory": golden,              # per-step actor, locator, intent, world hash
        "verifiers": [
            {"id": v.get("id"), "level": v.get("level"), "assertion": v.get("assertion"),
             "check": v.get("check"), "gym_result": v.get("gym_result"),
             "added_by_human": v.get("added_by_human")}
            for v in frozen.get("verifiers", [])
        ],
        "verifier_suite_version": frozen.get("suite_version"),
        "reward": frozen.get("reward"),
        "final_world_hash": frozen.get("final_world_hash", ""),
        "submission": {
            "reward": sub.reward, "kind": sub.kind, "accepted": sub.accepted,
            "override": sub.submitted_with_override,
            "overridden_verifiers": frozen.get("overridden", []),
            "benchmark_run_id": str(sub.benchmark_run_id) if sub.benchmark_run_id else None,
            "at": sub.created_at.isoformat(),
        },
        "annotator": annotator.email if annotator else None,
        "metadata": {"source": s.source, "agent": s.agent or None, "status": s.status,
                     "created_at": s.created_at.isoformat()},
    }


def build_sample(db: Session, s: models.ReviewSession) -> dict:
    """Assemble the deliverable bundle for one annotation session."""
    task = db.get(models.Task, s.task_id)
    annotator = db.get(models.Annotator, s.annotator_id) if s.annotator_id else None
    traj = _base_trajectory(db, s)
    branch = _latest(db, models.TrajectoryBranch, s.id, models.TrajectoryBranch.created_at.desc())
    suite = _latest(db, models.VerifierSuite, s.id, models.VerifierSuite.version.desc())
    run = None
    if suite:
        run = db.scalar(
            select(models.BenchmarkRun).where(models.BenchmarkRun.suite_id == suite.id).order_by(models.BenchmarkRun.created_at.desc())
        )
    # Prefer the ACCEPTED (adjudicated) submission over merely the latest.
    sub = db.scalar(
        select(models.Submission).where(models.Submission.session_id == s.id, models.Submission.accepted.is_(True)).order_by(models.Submission.created_at.desc())
    ) or _latest(db, models.Submission, s.id, models.Submission.created_at.desc())

    recorded = _steps_of(traj)
    # Golden = the recorded steps up to the correction point + the corrected tail.
    # No correction (the run was already passing) ⇒ the recorded run is golden.
    if branch is not None:
        head = [st for st in recorded if st["idx"] <= branch.from_step]
        tail = (branch.steps or {}).get("steps", [])
        golden = head + [{"idx": branch.from_step + 1 + i, "type": t.get("type"), "description": t.get("description"), "tab": t.get("tabId")} for i, t in enumerate(tail)]
        correction = {"from_step": branch.from_step, "text": branch.correction, "mode": branch.mode}
    else:
        golden = recorded
        correction = None

    seed_state = (task.seed_state or {}) if task else {}
    # Prefer the snapshot frozen at submit time (Cluster A) — it is immune to any
    # post-submit mutation of the live suite/benchmark. Fall back to the live
    # rebuild only for legacy submissions predating the snapshot column.
    frozen = sub.snapshot if (sub is not None and sub.snapshot) else None
    # A version-bound submission carries the whole lineage in its snapshot, so the
    # sample ships WHAT WAS APPROVED — including which steps were the agent's and
    # which the human's. The legacy branch below reconstructs an approximation
    # from a scalar fork index, which cannot express a hybrid trajectory at all.
    if frozen and frozen.get("trajectory_version"):
        return _versioned_sample(db, s, task, annotator, sub, frozen, recorded, seed_state)
    if frozen:
        verifiers = [
            {"level": v.get("level"), "assertion": v.get("assertion"),
             "check": v.get("check"), "gym_result": v.get("gym_result")}
            for v in frozen.get("verifiers", [])
        ]
        reward = frozen.get("reward")
        overridden = frozen.get("overridden", [])
        # Trajectories frozen at submit time win over a live rebuild, so a shipped
        # sample can never drift when the canonical run is re-captured later.
        if frozen.get("recorded_trajectory") is not None:
            recorded = frozen["recorded_trajectory"]
        if frozen.get("golden_trajectory") is not None:
            golden = frozen["golden_trajectory"]
    else:
        verifiers = []
        if suite:
            for v in db.scalars(select(models.Verifier).where(models.Verifier.suite_id == suite.id)):
                verifiers.append({
                    "level": v.level, "assertion": v.assertion,
                    "check": v.check_ir or None, "gym_result": v.gym_result or None,
                })
        reward = run.reward if run else (sub.reward if sub else None)
        overridden = (run.overridden if run else [])

    return {
        "sample_id": str(s.id),
        "task": {
            "id": task.external_id if task else None,
            "prompt": task.prompt if task else "",
            "category": task.category if task else "",
            "difficulty": task.difficulty if task else "",
            "constraints": (task.meta or {}).get("constraints", []) if task else [],
            "allowed_sites": (task.meta or {}).get("allowedSites", []) if task else [],
            "seed": task.seed if task else 0,
        },
        # The evaluation triplet: initial setup + seeded data (the seed-0 world).
        "initial_state": seed_state.get("world") or {k: v for k, v in seed_state.items() if k != "world"},
        "recorded_trajectory": recorded,          # the run under review (often the broken one)
        "correction": correction,                 # the human fix, if any
        "golden_trajectory": golden,              # the SFT trajectory — reaches a passing end-state
        "verifiers": verifiers,                   # the verifier suite that scores it
        "reward": reward,
        "submission": None if sub is None else {
            "reward": sub.reward, "kind": sub.kind, "accepted": sub.accepted,
            "override": sub.submitted_with_override,
            "overridden_verifiers": overridden,  # provenance: which checks a human forced (frozen at submit)
            "at": sub.created_at.isoformat(),
        },
        "annotator": annotator.email if annotator else None,
        "metadata": {"source": s.source, "agent": s.agent or None, "status": s.status, "created_at": s.created_at.isoformat()},
    }


def _submitted_sessions(db: Session, accepted_only: bool):
    q = (
        select(models.ReviewSession)
        .join(models.Submission, models.Submission.session_id == models.ReviewSession.id)
        .order_by(models.ReviewSession.created_at.desc())
    )
    if accepted_only:
        q = q.where(models.Submission.accepted.is_(True))
    # distinct sessions (a session has at most one submission after the lock)
    seen, out = set(), []
    for s in db.scalars(q):
        if s.id not in seen:
            seen.add(s.id)
            out.append(s)
    return out


@router.get("/samples")
def list_samples(accepted: bool = False, db: Session = Depends(get_db)) -> dict:
    """Exportable golden samples (submitted; `accepted=true` = adjudicator-accepted)."""
    rows = []
    for s in _submitted_sessions(db, accepted):
        task = db.get(models.Task, s.task_id)
        sub = _latest(db, models.Submission, s.id, models.Submission.created_at.desc())
        rows.append({
            "sampleId": str(s.id), "taskId": task.external_id if task else None,
            "reward": sub.reward if sub else None, "kind": sub.kind if sub else None,
            "accepted": sub.accepted if sub else False, "source": s.source,
        })
    return {"count": len(rows), "samples": rows}


@router.get("/samples/{session_id}")
def export_sample(session_id: UUID, db: Session = Depends(get_db)) -> dict:
    s = db.get(models.ReviewSession, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="sample (session) not found")
    return build_sample(db, s)


@router.get("/dataset.jsonl")
def export_dataset(accepted: bool = False,
                   current: models.Annotator = Depends(require_reviewer),
                   db: Session = Depends(get_db)) -> Response:
    """The whole golden dataset as JSONL — one deliverable sample bundle per line."""
    import json

    lines = [json.dumps(build_sample(db, s), default=str) for s in _submitted_sessions(db, accepted)]
    body = "\n".join(lines) + ("\n" if lines else "")
    return Response(
        content=body, media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="golden_samples.jsonl"'},
    )
