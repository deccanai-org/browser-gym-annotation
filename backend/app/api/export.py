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

# /4 added the OBSERVATION per step and turned `screenshot` from a dangling path
# into {path, sha256, bytes}. /5 makes three silences explicit: `initial_state`
# is null (with a reason) rather than a metadata stub, `environment_image_digest`
# is null rather than "", and `task.seed` is the seed the golden was RECORDED
# under rather than the task's current mutable one. All three are breaking reads
# for a consumer of /4, so the version moves with them.
_SCHEMA = "golden-sample/5"
# Submissions that predate version-bound finalization ship a genuinely different,
# thinner shape. It used to go out on the same JSONL with no discriminator at all,
# so a loader either crashed on the missing keys or read a thin row as a full one.
_LEGACY_SCHEMA = "golden-sample-legacy/1"

_NO_SEED_WORLD = (
    "no seed world has been captured for this task, so this sample has no initial "
    "leg to reset into (capture one with POST /api/gym/tasks/{id}/capture-seed)"
)
_NO_IMAGE_DIGEST = (
    "no environment image digest was stamped on this trajectory, its attempt, or "
    "this deployment — the build it was recorded against is unknown"
)


def _initial_state(seed_state: dict) -> tuple[dict | None, dict]:
    """The evaluation triplet's first leg, and where it came from.

    A missing world used to degrade to `{k: v for k, v in seed_state.items() if
    k != "world"}`, which for a gym task means the bundle shipped
    {"initial_url", "category", "difficulty"} in the slot a client resets FROM.
    There was no error and no flag, so a sample with no first leg was
    indistinguishable from a seeded one. Say the absence out loud instead, and
    keep the metadata where it cannot be mistaken for a world.
    """
    world = (seed_state or {}).get("world") or None
    return world, {
        "source": "task.seed_state.world" if world else None,
        "missing_reason": None if world else _NO_SEED_WORLD,
        "metadata": {k: v for k, v in (seed_state or {}).items() if k != "world"},
    }


def _live_task_block(task, s: models.ReviewSession) -> dict:
    """The task rebuilt from the live row — only for submissions frozen before
    `finalize.freeze` started carrying the task block. `seed` is the ATTEMPT's,
    never `task.seed`: the task's seed is mutable and a client resetting at it
    would get a world the golden was never recorded in."""
    meta = (task.meta or {}) if task else {}
    return {
        "id": task.external_id if task else None,
        "prompt": task.prompt if task else "",
        "category": task.category if task else "",
        "difficulty": task.difficulty if task else "",
        "constraints": meta.get("constraints", []),
        "allowed_sites": meta.get("allowedSites", []),
        "seed": s.seed,
        "start_url": task.start_url if task else "",
    }


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
    """The deliverable for a version-bound submission — assembled from the frozen
    snapshot, so nothing it says can drift after it shipped.

    The seed world is the one thing still read live: it is the whole multi-app
    world and freezing a copy per submission would multiply the DB by it. That is
    why `initial_state_provenance` names its source — a live read has to be
    declared as one.
    """
    tv = frozen["trajectory_version"]
    golden = frozen.get("golden_trajectory", [])
    # The correction is no longer a scalar fork index plus a text blob: it is the
    # lineage, and each step says who authored it and under whose instruction.
    corrections = [
        {"version_no": st_v["versionNo"], "kind": st_v["kind"], "producer": st_v["producer"]}
        for st_v in tv.get("lineage", []) if st_v["versionNo"] > 1
    ]
    # The task block was rebuilt from the live row here, which meant a catalog
    # reseed could rewrite the prompt of a sample that had already shipped.
    task_block = dict(frozen.get("task") or _live_task_block(task, s))
    task_block.setdefault("revision", frozen.get("task_revision", 1))
    world, world_provenance = _initial_state(seed_state)
    digest = tv.get("environment_image_digest") or ""
    results = frozen.get("results") or {}
    return {
        "sample_id": str(s.id),
        "schema": _SCHEMA,
        "task": task_block,
        "initial_state": world,
        "initial_state_provenance": world_provenance,
        "recorded_trajectory": recorded,
        "trajectory_version": {
            "id": tv["id"], "version_no": tv["versionNo"], "kind": tv["kind"],
            "environment_image_digest": digest or None,
            "environment_image_digest_provenance": {
                "source": tv.get("environment_image_digest_source") or None,
                "missing_reason": None if digest else _NO_IMAGE_DIGEST,
            },
            "lineage": tv.get("lineage", []),
        },
        "corrections": corrections,
        "golden_trajectory": golden,              # per-step actor, locator, intent, world hash
        "verifiers": [
            {"id": v.get("id"), "level": v.get("level"), "assertion": v.get("assertion"),
             "check": v.get("check"), "gym_result": v.get("gym_result"),
             "added_by_human": v.get("added_by_human"),
             # Which checks produced the reward below. A bare 0/1 does not say
             # which assertion a breaker broke, which is the only actionable part.
             "result": results.get(v.get("id"))}
            for v in frozen.get("verifiers", [])
        ],
        "verifier_results": results,
        "verifier_suite_version": frozen.get("suite_version"),
        "reward": frozen.get("reward"),
        "final_world_hash": frozen.get("final_world_hash", ""),
        # The attempt-scope state transition: seeded world -> end state. Per-step
        # deltas ride inside `golden_trajectory` above.
        "state_trajectory": {
            "schema": "world-delta/1",
            "initial_hash": frozen.get("initial_world_hash", ""),
            "final_hash": frozen.get("final_world_hash", ""),
            "summary": frozen.get("world_summary") or {},
        },
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
    # Trajectories frozen at submit time win over a live rebuild, so a shipped
    # sample can never drift when the canonical run is re-captured later. Hoisted
    # above the version dispatch: the versioned path was handed the LIVE rebuild
    # while its own docstring promised nothing it says can drift.
    if frozen and frozen.get("recorded_trajectory") is not None:
        recorded = frozen["recorded_trajectory"]
    # A version-bound submission carries the whole lineage in its snapshot, so the
    # sample ships WHAT WAS APPROVED — including which steps were the agent's and
    # which the human's. The legacy branch below reconstructs an approximation
    # from a scalar fork index, which cannot express a hybrid trajectory at all.
    if frozen and frozen.get("trajectory_version"):
        return _versioned_sample(db, s, task, annotator, sub, frozen, recorded, seed_state)
    if frozen:
        results = frozen.get("results") or {}
        verifiers = [
            {"id": v.get("id") or v.get("ext_id"), "level": v.get("level"),
             "assertion": v.get("assertion"), "check": v.get("check"),
             "gym_result": v.get("gym_result"),
             "result": results.get(v.get("id") or v.get("ext_id"))}
            for v in frozen.get("verifiers", [])
        ]
        reward = frozen.get("reward")
        overridden = frozen.get("overridden", [])
        if frozen.get("golden_trajectory") is not None:
            golden = frozen["golden_trajectory"]
    else:
        results = dict(run.results or {}) if run else {}
        verifiers = []
        if suite:
            for v in db.scalars(select(models.Verifier).where(models.Verifier.suite_id == suite.id)):
                verifiers.append({
                    "id": v.ext_id, "level": v.level, "assertion": v.assertion,
                    "check": v.check_ir or None, "gym_result": v.gym_result or None,
                    "result": results.get(v.ext_id),
                })
        reward = run.reward if run else (sub.reward if sub else None)
        overridden = (run.overridden if run else [])

    world, world_provenance = _initial_state(seed_state)
    return {
        "sample_id": str(s.id),
        # Every exported line names its shape. Two incompatible bundles used to
        # share the JSONL with only one of them carrying a `schema` key, so a
        # loader could not tell a thin legacy row from a full one without probing
        # for keys that are legitimately absent on both.
        "schema": _LEGACY_SCHEMA,
        "task": _live_task_block(task, s),
        # The evaluation triplet: initial setup + seeded data (the seed-0 world),
        # or an explicit null when this task never had one captured.
        "initial_state": world,
        "initial_state_provenance": world_provenance,
        "recorded_trajectory": recorded,          # the run under review (often the broken one)
        "correction": correction,                 # the human fix, if any
        "golden_trajectory": golden,              # the SFT trajectory — reaches a passing end-state
        "verifiers": verifiers,                   # the verifier suite that scores it
        "verifier_results": results,              # per-check outcomes behind the reward
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
def list_samples(accepted: bool = False,
                 _current: models.Annotator = Depends(require_reviewer),
                 db: Session = Depends(get_db)) -> dict:
    """Exportable golden samples (submitted; `accepted=true` = adjudicator-accepted).

    Reviewer-only, like /dataset.jsonl below: this lists every annotator's
    submission and its reward across the whole cohort."""
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
def export_sample(session_id: UUID,
                  _current: models.Annotator = Depends(require_reviewer),
                  db: Session = Depends(get_db)) -> dict:
    """One complete golden bundle. Reviewer-only — it returns another annotator's
    entire frozen submission, trajectory included."""
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
