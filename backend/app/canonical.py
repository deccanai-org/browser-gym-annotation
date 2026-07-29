"""Which recorded run IS the canonical run for a task (§3.10).

Every annotator reviewing a breaker reviews the SAME run. That run was chosen by
a heuristic — "the oldest gym trajectory carrying a replay payload" — copied into
four modules, which is how the four copies drifted apart (one has no LIMIT, three
apply LIMIT 50 before testing for a payload, and export disagrees with the version
graph about which of a session's own rows is "the" one).

The heuristic is also wrong on its own terms. Per-step world capture landed in
the gym harness after some runs were already recorded, so the OLDEST run is
routinely the one with no world trail — and a canonical run with no world trail
cannot be forked from at all: `gym._persist_gym_review` writes no per-step
checkpoint, `versions.fork_before` gets `fork_checkpoint_id=None`, and
`replay.restore_and_replay` then skips restoration entirely and reports success
against whatever state the environment happens to be in. Nothing raises. A
post-wipe re-capture is a NEW stochastic run, not a fresher copy of the old one,
so "oldest" is not even a conservative choice — it is an arbitrary one.

So canonical is BOUND, not guessed:

* an explicit `CanonicalRun` row is the source of truth, and nothing derived from
  timestamps or payload shape may override it;
* with nothing bound, the fallback takes the EARLIEST full run — then the longer
  trace, then the id, because two runs recorded in the same second are
  indistinguishable by clock and the answer must not depend on the order the
  storage engine happens to return rows in. Forkability is deliberately not part
  of that ordering: every curated breaker predates per-step world capture, so
  ranking forkable runs first would hand canonical status to any later run,
  including an annotator's prompt-edit re-run, on precisely the tasks this module
  exists to serve. Canonical is an IDENTITY question, not a quality one, and a
  prompt-edit re-run is excluded from candidacy outright;
* an attempt already baselined keeps the base bound on its v1
  (`TrajectoryVersion.base_trajectory_id`, which existed for exactly this and was
  never read back), so re-binding a task cannot swap the run out from under a
  half-finished review.

`audit()` measures world coverage per task rather than asserting it from the
schema (§8.7), and the CLI at the bottom is how M40/M76-style mistakes get
corrected:

    python -m app.canonical audit --limit 50
    python -m app.canonical candidates --task M40_bogus_pricematch
    python -m app.canonical bind --task M40_bogus_pricematch --best \\
        --actor reviewer@deccan.ai --reason "pre-world-capture run was pinned"
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Text, UniqueConstraint, Uuid, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app import models
from app.db import Base

ROLES_THAT_MAY_BIND = ("admin", "reviewer")


class NotPermitted(PermissionError):
    """Binding rewrites what every annotator opening the task sees. It is a
    dataset-wide edit wearing the costume of a one-row write."""


class BindingRefused(ValueError):
    """The proposed run cannot serve as canonical — binding it would break the
    task for everyone instead of fixing it for anyone."""


class CanonicalRun(Base):
    """The task's canonical run, decided by a person.

    A row here is a DECISION, not an observation: it survives new captures, world
    wipes and re-runs, and it is the only thing that can move canonical. The
    trajectory FK cascades — if the bound run is deleted the decision is gone too,
    and resolution falls back rather than pointing at a row that no longer exists.
    """

    __tablename__ = "canonical_run"
    __table_args__ = (UniqueConstraint("task_id", name="uq_canonical_run_task"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("task.id", ondelete="CASCADE"), index=True)
    trajectory_id: Mapped[UUID] = mapped_column(ForeignKey("trajectory.id", ondelete="CASCADE"), index=True)
    bound_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("annotator.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime] = mapped_column(default=func.now(), onupdate=func.now())


# --------------------------------------------------------------------------- evidence
def _raw_trail(traj: models.Trajectory) -> int:
    """Entries of `gymResume.worldTrail` carrying a real world. This is what the
    review UI resumes a correction from; when it is empty the client silently
    falls back to the run's FINAL world, which already contains every later step's
    effects."""
    trail = ((traj.raw or {}).get("gymResume") or {}).get("worldTrail") or []
    return sum(1 for w in trail if w)


def world_evidence(db: Session, traj: models.Trajectory) -> dict:
    """How much of this run can actually be restored from, measured — not inferred
    from the schema. `worldSteps` counts the persisted per-step worlds (what a fork
    restores from) and `trail` the payload's worldTrail (what the UI resumes from);
    they come from the same harness field, so a run recorded before that field
    existed reports zero for both."""
    steps, worlds = db.execute(
        select(func.count(models.TrajectoryStep.id), func.count(models.TrajectoryStep.world_after))
        .where(models.TrajectoryStep.trajectory_id == traj.id)
    ).one()
    return {"steps": int(steps), "worldSteps": int(worlds), "trail": _raw_trail(traj)}


def _forkable(evidence: dict) -> bool:
    return bool(evidence["worldSteps"] or evidence["trail"])


def _rank(traj: models.Trajectory, evidence: dict) -> tuple:
    """Best first: the EARLIEST full run wins.

    Forkability is deliberately NOT a sort key. Ranking a forkable run ahead of an
    older one sounds like an improvement and is a silent data-integrity bug on
    exactly the population this module exists to serve: the curated breakers were
    all captured before the harness recorded per-step worlds, so every one of them
    is un-forkable, and any later run — including an annotator's prompt-edit
    re-run — would outrank the very breaker under review and become what every
    annotator sees. Canonical is an IDENTITY question ("which run is this task's
    recorded failure"), not a quality one.

    Missing world evidence is therefore something `audit` REPORTS, and something a
    backfill, a re-capture or a deliberate binding fixes. It is never fixed by
    quietly selecting somebody else's run.

    `created_at` alone cannot break the tie — SQLite's CURRENT_TIMESTAMP has
    one-second resolution — so a longer trace, then the id, settle it
    deterministically instead of the storage engine's row order.
    """
    return (traj.created_at, -evidence["steps"], str(traj.id))


def candidates(db: Session, task_id: UUID) -> list[tuple[models.Trajectory, dict]]:
    """Every run that COULD be canonical for this task, best first.

    The payload test is in SQL, not applied after a LIMIT: every drive-forward
    continuation and every per-attempt baseline clone persists with `raw=None` and
    matches the task+source filter, so a LIMIT taken first would eventually push
    the only replayable run off the end and 404 a task that plainly has one.
    """
    rows = db.scalars(
        select(models.Trajectory)
        .join(models.ReviewSession, models.Trajectory.session_id == models.ReviewSession.id)
        .where(
            models.ReviewSession.task_id == task_id,
            models.Trajectory.source == "gym",
            models.Trajectory.raw.is_not(None),
        )
        .order_by(models.Trajectory.created_at.asc())
        .limit(200)
    ).all()
    # A legacy payload stored as JSON `null` (or an empty dict) survives the SQL
    # NOT NULL test on both backends but is not replayable. A prompt-edit re-run
    # is excluded outright: it answers a DIFFERENT prompt, so whatever it shows is
    # not this task's recorded failure, and it must never become what the next
    # annotator opens.
    scored = [
        (t, world_evidence(db, t))
        for t in rows
        if t.raw and not (isinstance(t.raw, dict) and t.raw.get("promptOverride"))
    ]
    return sorted(scored, key=lambda pair: _rank(*pair))


# --------------------------------------------------------------------------- resolve
def bound_run(db: Session, task_id: UUID) -> CanonicalRun | None:
    return db.scalar(select(CanonicalRun).where(CanonicalRun.task_id == task_id))


def for_task(db: Session, task_id: UUID) -> models.Trajectory | None:
    """THE resolver. Every caller goes through here."""
    binding = bound_run(db, task_id)
    if binding is not None:
        # SQLite does not enforce foreign keys unless the pragma is on, so a binding
        # can outlive its run there even though Postgres cascades it away. Falling
        # back beats handing every caller a run id that resolves to nothing.
        bound = db.get(models.Trajectory, binding.trajectory_id)
        if bound is not None:
            return bound
    ranked = candidates(db, task_id)
    return ranked[0][0] if ranked else None


def for_attempt(db: Session, attempt: models.ReviewSession) -> models.Trajectory | None:
    """The run THIS attempt is annotating.

    Once an attempt has a v1 its base is frozen there, so a later re-binding of the
    task cannot renumber the steps somebody is halfway through reviewing — their
    verdicts and forks reference rows cloned from the run they were shown.
    """
    root = db.scalar(
        select(models.TrajectoryVersion).where(
            models.TrajectoryVersion.attempt_id == attempt.id,
            models.TrajectoryVersion.parent_version_id.is_(None),
        )
    )
    if root is not None and root.base_trajectory_id:
        base = db.get(models.Trajectory, root.base_trajectory_id)
        if base is not None:
            return base
    return for_task(db, attempt.task_id)


# --------------------------------------------------------------------------- bind
def bind(
    db: Session,
    *,
    task: models.Task,
    trajectory: models.Trajectory,
    actor: models.Annotator,
    reason: str = "",
) -> CanonicalRun:
    """Bind a task's canonical run. Gated: only a reviewer or admin, and always
    audited, because this is what every annotator will open tomorrow."""
    if (actor.role or "") not in ROLES_THAT_MAY_BIND:
        raise NotPermitted("binding the canonical run requires a reviewer or admin")
    owner = db.get(models.ReviewSession, trajectory.session_id)
    if owner is None or owner.task_id != task.id:
        raise BindingRefused("that run belongs to another task")
    if not trajectory.raw:
        raise BindingRefused(
            "that run carries no replay payload — reopening the task would 404 instead of showing it"
        )

    evidence = world_evidence(db, trajectory)
    row = bound_run(db, task.id)
    previous = str(row.trajectory_id) if row is not None else None
    if row is None:
        row = CanonicalRun(task_id=task.id, trajectory_id=trajectory.id)
        db.add(row)
    row.trajectory_id = trajectory.id
    row.bound_by_id = actor.id
    row.reason = reason
    db.add(models.AuditLog(
        session_id=trajectory.session_id, actor=actor.email, action="canonical.bind",
        target=task.external_id,
        meta={"trajectoryId": str(trajectory.id), "previous": previous, "reason": reason,
              "worldSteps": evidence["worldSteps"], "steps": evidence["steps"],
              "forkable": _forkable(evidence)},
    ))
    db.flush()
    return row


# --------------------------------------------------------------------------- preflight
def _binder_email(db: Session, binding: "CanonicalRun | None") -> str | None:
    """Who bound this run, when they can still be named."""
    if binding is None or not binding.bound_by_id:
        return None
    who = db.get(models.Annotator, binding.bound_by_id)
    return who.email if who is not None else None


def audit(db: Session, *, external_ids: list[str] | None = None, limit: int | None = None) -> dict:
    """Per gym task: does its canonical run have a world trail, and is there a
    better candidate sitting unused?

    §8.7 — data availability is a hypothesis to be measured. The summary counts
    ONLY the tasks scanned here; it is not a statement about the dataset.
    """
    q = select(models.Task).where(models.Task.source == "gym").order_by(models.Task.external_id)
    if external_ids:
        q = select(models.Task).where(models.Task.external_id.in_(external_ids)).order_by(models.Task.external_id)
    if limit:
        q = q.limit(limit)

    rows: list[dict] = []
    for task in db.scalars(q):
        ranked = candidates(db, task.id)
        binding = bound_run(db, task.id)
        chosen = for_task(db, task.id)
        evidence = world_evidence(db, chosen) if chosen is not None else {"steps": 0, "worldSteps": 0, "trail": 0}
        best = next((pair for pair in ranked if _forkable(pair[1])), None)
        forkable = _forkable(evidence)
        rows.append({
            "taskId": task.external_id,
            "bound": binding is not None,
            # bound_by_id is ON DELETE SET NULL, but a deactivated-then-purged
            # reviewer can also leave a dangling id, and db.get returns None for
            # it — dereferencing .email there takes down the whole audit, which is
            # the one report that tells anyone the dataset has a gap.
            "boundBy": _binder_email(db, binding),
            "canonicalTrajectoryId": str(chosen.id) if chosen is not None else None,
            "canonicalSteps": evidence["steps"],
            "canonicalWorldSteps": evidence["worldSteps"],
            "canonicalTrail": evidence["trail"],
            "candidates": len(ranked),
            "forkable": forkable,
            # A better run exists and is not the one being served — the exact M40/M76
            # shape, and the only row type that a bind can actually fix.
            "correctable": bool(best is not None and not forkable),
            "bestCandidateId": str(best[0].id) if best is not None else None,
            "bestCandidateWorldSteps": best[1]["worldSteps"] if best is not None else 0,
        })

    return {
        "tasks": rows,
        "summary": {
            "tasks": len(rows),
            "bound": sum(1 for r in rows if r["bound"]),
            "withCanonical": sum(1 for r in rows if r["canonicalTrajectoryId"]),
            "forkable": sum(1 for r in rows if r["forkable"]),
            "unforkable": sum(1 for r in rows if r["canonicalTrajectoryId"] and not r["forkable"]),
            "correctable": sum(1 for r in rows if r["correctable"]),
            "noRun": sum(1 for r in rows if not r["canonicalTrajectoryId"]),
        },
    }


# --------------------------------------------------------------------------- CLI
def _cli(argv: list[str] | None = None) -> int:
    import argparse
    import json

    from app.db import SessionLocal

    p = argparse.ArgumentParser(prog="python -m app.canonical", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("audit", help="per-task world-trail coverage of the canonical run")
    a.add_argument("--task", action="append", dest="tasks", help="external id (repeatable)")
    a.add_argument("--limit", type=int)
    a.add_argument("--json", action="store_true")

    c = sub.add_parser("candidates", help="every bindable run for one task, best first")
    c.add_argument("--task", required=True)

    b = sub.add_parser("bind", help="bind a task's canonical run (reviewer/admin only)")
    b.add_argument("--task", required=True)
    b.add_argument("--trajectory", help="trajectory uuid")
    b.add_argument("--best", action="store_true", help="bind the best forkable candidate")
    b.add_argument("--actor", required=True, help="reviewer/admin email")
    b.add_argument("--reason", default="")

    args = p.parse_args(argv)
    with SessionLocal() as db:
        if args.cmd == "audit":
            out = audit(db, external_ids=args.tasks, limit=args.limit)
            if args.json:
                print(json.dumps(out, indent=2))
                return 0
            for r in out["tasks"]:
                flag = "ok  " if r["forkable"] else ("FIX " if r["correctable"] else "none")
                print(f"{flag} {r['taskId']:<48} world {r['canonicalWorldSteps']}/{r['canonicalSteps']}"
                      f"  candidates {r['candidates']}{'  BOUND' if r['bound'] else ''}")
            print(f"\nscanned {out['summary']['tasks']} tasks: {json.dumps(out['summary'])}")
            return 0

        task = db.scalar(select(models.Task).where(models.Task.external_id == args.task))
        if task is None:
            print(f"unknown task {args.task!r}")
            return 2

        if args.cmd == "candidates":
            for traj, ev in candidates(db, task.id):
                print(f"{traj.id}  {traj.created_at}  agent={traj.agent or '-':<10} "
                      f"world {ev['worldSteps']}/{ev['steps']}  trail {ev['trail']}")
            return 0

        actor = db.scalar(select(models.Annotator).where(models.Annotator.email == args.actor))
        if actor is None:
            print(f"unknown annotator {args.actor!r}")
            return 2
        if args.best:
            # MOST COMPLETE, not merely first-forkable. `candidates` is ordered for
            # canonical IDENTITY (earliest wins), so taking the first forkable one
            # hands back the oldest usable run — which on a task being REPAIRED is
            # very likely the thin one that needed repairing. An operator typing
            # --best is asking for the run with the most evidence.
            usable = [(t, e) for t, e in candidates(db, task.id) if _forkable(e)]
            if not usable:
                print("no candidate with a world trail — nothing safe to bind")
                return 1
            pick = max(usable, key=lambda pair: (pair[1]["worldSteps"], pair[1]["trail"], pair[1]["steps"]))[0]
        else:
            if not args.trajectory:
                print("pass --trajectory <uuid> or --best")
                return 2
            pick = db.get(models.Trajectory, UUID(args.trajectory))
            if pick is None:
                print("unknown trajectory")
                return 2
        try:
            bind(db, task=task, trajectory=pick, actor=actor, reason=args.reason)
        except (NotPermitted, BindingRefused) as exc:
            print(f"refused: {exc}")
            return 1
        db.commit()
        ev = world_evidence(db, pick)
        print(f"bound {task.external_id} → {pick.id} (world {ev['worldSteps']}/{ev['steps']}) by {actor.email}")
        return 0


if __name__ == "__main__":  # pragma: no cover — operator entry point
    raise SystemExit(_cli())
