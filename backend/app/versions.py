"""The trajectory version graph.

An attempt's lineage is a tree of versions: v1 is the canonical agent run, and
every correction or human-authored continuation is a child. The rules that make
it trustworthy (§3.3, §3.5, §8.3):

* **A version stores only its own suffix.** The prefix is resolved by walking
  `parent_version_id`. Inherited steps are referenced, never copied — copying
  would mint new IDs and silently drop every per-step verdict on a re-fork.
* **Identity is the step's UUID.** `suffix_ordinal` orders a version's own steps;
  the global display number is computed while flattening, because a step's
  position changes between branches and therefore cannot be identity.
* **Corrections fork BEFORE the rejected step**, which is then absent from the
  child. "Continue after this step" is a separate command that keeps it.
* **Content is immutable; status transitions.** Both the attempt head and a
  version's status move only under compare-and-swap, so an agent run that
  finishes late can never overwrite a newer decision.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import models

ROOT = "agent_run"
CORRECTION = "agent_correction"
MANUAL = "human_manual"

STATUSES = ("candidate", "approved", "rejected", "published")


class ConcurrencyError(RuntimeError):
    """A compare-and-swap lost. The caller read a stale revision — it must reload
    and decide again rather than clobbering whatever happened in between."""


class LineageError(ValueError):
    """A fork point that is not in the parent's flattened chain. Failing closed
    beats producing a trajectory whose prefix nobody can reconstruct."""


# --------------------------------------------------------------------------- create
def _next_version_no(db: Session, attempt_id: UUID) -> int:
    cur = db.scalar(
        select(func.max(models.TrajectoryVersion.version_no)).where(
            models.TrajectoryVersion.attempt_id == attempt_id
        )
    )
    return int(cur or 0) + 1


def create_root(
    db: Session,
    *,
    attempt_id: UUID,
    base_trajectory_id: UUID | None = None,
    kind: str = ROOT,
    producer: str = "",
    model_config: dict | None = None,
    created_by_id: UUID | None = None,
    environment_image_digest: str = "",
) -> models.TrajectoryVersion:
    """v1 — the canonical run this attempt is annotating.

    `base_trajectory_id` binds the EXACT recorded run (§3.10). The old
    "oldest/newest raw wins" heuristic is what let a post-wipe re-capture quietly
    become canonical.
    """
    v = models.TrajectoryVersion(
        attempt_id=attempt_id,
        parent_version_id=None,
        version_no=_next_version_no(db, attempt_id),
        kind=kind,
        base_trajectory_id=base_trajectory_id,
        producer=producer,
        model_config_json=model_config or {},
        created_by_id=created_by_id,
        environment_image_digest=environment_image_digest,
        status="candidate",
    )
    db.add(v)
    db.flush()
    return v


def ensure_root(
    db: Session, attempt: models.ReviewSession, base: models.Trajectory
) -> models.TrajectoryVersion:
    """Give an attempt its v1 baseline from a recorded run. Idempotent.

    The baseline steps are CLONED into attempt-owned rows. That is not the
    copying §3.3 forbids: the ban protects verdict continuity across a fork
    *within* an attempt, whereas a canonical gym run is shared by every annotator
    reviewing that breaker — each attempt needs its own identity space, or two
    annotators' verdicts and forks would collide on the same step rows. The clone
    happens once, at baseline, before any verdict exists; checkpoints and
    screenshots are referenced, not duplicated.
    """
    existing = db.scalar(
        select(models.TrajectoryVersion).where(
            models.TrajectoryVersion.attempt_id == attempt.id,
            models.TrajectoryVersion.parent_version_id.is_(None),
        )
    )
    if existing is not None:
        return existing

    own = models.Trajectory(
        session_id=attempt.id, agent=base.agent, seed=base.seed,
        score=base.score, success=base.success, source=base.source,
    )
    db.add(own)
    db.flush()
    v1 = create_root(
        db, attempt_id=attempt.id, base_trajectory_id=base.id,
        producer=base.agent or "agent", kind=ROOT,
    )
    src = db.scalars(
        select(models.TrajectoryStep)
        .where(models.TrajectoryStep.trajectory_id == base.id)
        .order_by(models.TrajectoryStep.idx)
    ).all()
    for n, st in enumerate(src):
        db.add(models.TrajectoryStep(
            trajectory_id=own.id, version_id=v1.id, suffix_ordinal=n, idx=st.idx,
            actor=st.actor or "agent", action_type=st.action_type, description=st.description,
            tab_id=st.tab_id, screenshot_url=st.screenshot_url, reasoning=st.reasoning,
            url_after=st.url_after, arguments=st.arguments or {},
            semantic_locator=st.semantic_locator or {}, resolved_target=st.resolved_target or {},
            world_after=st.world_after,
            before_checkpoint_id=st.before_checkpoint_id,
            after_checkpoint_id=st.after_checkpoint_id,
        ))
    db.flush()
    return v1


def _child(
    db: Session,
    *,
    parent: models.TrajectoryVersion,
    fork_before_step_id: UUID | None,
    fork_checkpoint_id: UUID | None,
    kind: str,
    producer: str,
    model_config: dict | None,
    created_by_id: UUID | None,
) -> models.TrajectoryVersion:
    v = models.TrajectoryVersion(
        attempt_id=parent.attempt_id,
        parent_version_id=parent.id,
        version_no=_next_version_no(db, parent.attempt_id),
        kind=kind,
        base_trajectory_id=parent.base_trajectory_id,
        fork_before_step_id=fork_before_step_id,
        fork_checkpoint_id=fork_checkpoint_id,
        environment_image_digest=parent.environment_image_digest,
        producer=producer,
        model_config_json=model_config or {},
        created_by_id=created_by_id,
        status="candidate",
    )
    db.add(v)
    db.flush()
    return v


def fork_before(
    db: Session,
    *,
    parent: models.TrajectoryVersion,
    step: models.TrajectoryStep,
    kind: str = CORRECTION,
    producer: str = "",
    model_config: dict | None = None,
    created_by_id: UUID | None = None,
) -> models.TrajectoryVersion:
    """Reject `step` and branch from the state that preceded it.

    The rejected step must NOT appear in the child — that is the whole point of
    a correction, and forking "at" the step would leave the bad action in the
    golden trajectory.
    """
    prefix = flatten(db, parent)
    if not any(s.id == step.id for s in prefix):
        raise LineageError("the fork step is not in the parent's chain")
    return _child(
        db, parent=parent,
        fork_before_step_id=step.id,
        fork_checkpoint_id=step.before_checkpoint_id,
        kind=kind, producer=producer, model_config=model_config, created_by_id=created_by_id,
    )


def continue_after(
    db: Session,
    *,
    parent: models.TrajectoryVersion,
    step: models.TrajectoryStep,
    kind: str = CORRECTION,
    producer: str = "",
    model_config: dict | None = None,
    created_by_id: UUID | None = None,
) -> models.TrajectoryVersion:
    """Keep `step` and branch from the state that FOLLOWED it.

    Expressed as "fork before the next step", so the flattener keeps exactly one
    truncation rule; the difference that matters — the child starts from the
    after-checkpoint and inherits the step — is preserved.
    """
    prefix = flatten(db, parent)
    at = next((i for i, s in enumerate(prefix) if s.id == step.id), None)
    if at is None:
        raise LineageError("the continuation step is not in the parent's chain")
    nxt = prefix[at + 1] if at + 1 < len(prefix) else None
    return _child(
        db, parent=parent,
        fork_before_step_id=nxt.id if nxt else None,  # None = inherit the whole parent
        fork_checkpoint_id=step.after_checkpoint_id,
        kind=kind, producer=producer, model_config=model_config, created_by_id=created_by_id,
    )


# --------------------------------------------------------------------------- suffix
def _suffix(db: Session, version_id: UUID) -> list[models.TrajectoryStep]:
    return list(
        db.scalars(
            select(models.TrajectoryStep)
            .where(models.TrajectoryStep.version_id == version_id)
            .order_by(models.TrajectoryStep.suffix_ordinal, models.TrajectoryStep.idx)
        ).all()
    )


def next_ordinal(db: Session, version_id: UUID) -> int:
    cur = db.scalar(
        select(func.max(models.TrajectoryStep.suffix_ordinal)).where(
            models.TrajectoryStep.version_id == version_id
        )
    )
    return 0 if cur is None else int(cur) + 1


def adopt_steps(
    db: Session, version: models.TrajectoryVersion, steps: list[models.TrajectoryStep], *, actor: str = "agent"
) -> list[models.TrajectoryStep]:
    """Claim already-persisted rows (e.g. a finished gym run) as this version's
    suffix. Ordinals are assigned here; the rows keep their UUIDs so any verdict
    already recorded against them stays attached."""
    start = next_ordinal(db, version.id)
    for n, st in enumerate(steps):
        st.version_id = version.id
        st.suffix_ordinal = start + n
        st.actor = st.actor or actor
    db.flush()
    return steps


def append_step(
    db: Session, version: models.TrajectoryVersion, *, trajectory_id: UUID, actor: str = "human", **fields
) -> models.TrajectoryStep:
    """Add one committed step to this version's suffix."""
    ordinal = next_ordinal(db, version.id)
    st = models.TrajectoryStep(
        trajectory_id=trajectory_id,
        version_id=version.id,
        suffix_ordinal=ordinal,
        actor=actor,
        idx=fields.pop("idx", ordinal),
        **fields,
    )
    db.add(st)
    db.flush()
    return st


# --------------------------------------------------------------------------- read
def chain(db: Session, version: models.TrajectoryVersion) -> list[models.TrajectoryVersion]:
    """Root → … → version. Guards against a cycle rather than hanging."""
    out: list[models.TrajectoryVersion] = []
    seen: set[UUID] = set()
    cur: models.TrajectoryVersion | None = version
    while cur is not None:
        if cur.id in seen:
            raise LineageError("cycle in the version chain")
        seen.add(cur.id)
        out.append(cur)
        cur = db.get(models.TrajectoryVersion, cur.parent_version_id) if cur.parent_version_id else None
    return list(reversed(out))


def flatten(db: Session, version: models.TrajectoryVersion) -> list[models.TrajectoryStep]:
    """The full, ordered step list this version represents.

    Walks the chain from the root, truncating each parent's contribution at the
    child's fork point and appending the child's own suffix. Inherited steps are
    the SAME rows — same UUIDs — which is what lets verdicts survive a re-fork.
    """
    line = chain(db, version)
    steps: list[models.TrajectoryStep] = []
    for v in line:
        if v.fork_before_step_id is not None:
            at = next((i for i, s in enumerate(steps) if s.id == v.fork_before_step_id), None)
            if at is None:
                raise LineageError(f"version {v.version_no} forks before a step that is not in its prefix")
            steps = steps[:at]
        steps.extend(_suffix(db, v.id))
    return steps


def flat_view(db: Session, version: models.TrajectoryVersion) -> list[dict]:
    """Flattened steps with the DISPLAY number computed here — never persisted,
    because the same step sits at different positions on different branches."""
    out = []
    for n, s in enumerate(flatten(db, version)):
        out.append({
            "displayIdx": n,
            "stepId": str(s.id),
            "versionId": str(s.version_id) if s.version_id else None,
            "inherited": s.version_id != version.id,
            "actor": s.actor,
            "type": s.action_type,
            "description": s.description,
            "url": s.url_after,
            "image": s.screenshot_url,
            "reasoning": s.reasoning,
            "humanIntent": s.human_intent,
            "guidance": s.guidance_text,
        })
    return out


def head(db: Session, attempt: models.ReviewSession) -> models.TrajectoryVersion | None:
    if not attempt.active_version_id:
        return None
    return db.get(models.TrajectoryVersion, attempt.active_version_id)


def versions_for(db: Session, attempt_id: UUID) -> list[models.TrajectoryVersion]:
    return list(
        db.scalars(
            select(models.TrajectoryVersion)
            .where(models.TrajectoryVersion.attempt_id == attempt_id)
            .order_by(models.TrajectoryVersion.version_no)
        ).all()
    )


# --------------------------------------------------------------------------- transitions
def set_head(
    db: Session, attempt: models.ReviewSession, version: models.TrajectoryVersion, *, expected_revision: int
) -> int:
    """Advance the attempt HEAD under compare-and-swap.

    Selection is always an explicit command — a finishing agent job creates a
    candidate and stops. That is precisely what makes out-of-order completions
    safe: the late job's CAS fails instead of resurrecting a superseded branch.
    """
    if version.attempt_id != attempt.id:
        raise LineageError("that version belongs to another attempt")
    res = db.execute(
        update(models.ReviewSession)
        .where(models.ReviewSession.id == attempt.id, models.ReviewSession.revision == expected_revision)
        .values(active_version_id=version.id, revision=expected_revision + 1)
    )
    if res.rowcount != 1:
        raise ConcurrencyError("the attempt moved on; reload before selecting a version")
    db.refresh(attempt)
    return attempt.revision


def set_status(
    db: Session, version: models.TrajectoryVersion, status: str, *, expected_revision: int
) -> int:
    """Move a version's lifecycle status. Content stays immutable — only status
    transitions, and only under CAS."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    res = db.execute(
        update(models.TrajectoryVersion)
        .where(models.TrajectoryVersion.id == version.id, models.TrajectoryVersion.revision == expected_revision)
        .values(status=status, revision=expected_revision + 1)
    )
    if res.rowcount != 1:
        raise ConcurrencyError("this version was decided by someone else; reload")
    db.refresh(version)
    return version.revision


# --------------------------------------------------------------------------- verdicts
def set_verdict(
    db: Session,
    *,
    attempt_id: UUID,
    step_id: UUID,
    verdict: str,
    note: str = "",
    annotator_id: UUID | None = None,
    reviewer_id: UUID | None = None,
) -> models.StepVerdict:
    """Record a per-step verdict against the step's STABLE id. Re-verdicting the
    same step updates in place (one verdict per attempt per step)."""
    row = db.scalar(
        select(models.StepVerdict).where(
            models.StepVerdict.attempt_id == attempt_id, models.StepVerdict.step_id == step_id
        )
    )
    if row is None:
        row = models.StepVerdict(attempt_id=attempt_id, step_id=step_id)
        db.add(row)
    row.verdict = verdict
    row.note = note or row.note
    if annotator_id:
        row.annotator_id = annotator_id
    if reviewer_id:
        row.reviewer_id = reviewer_id
    db.flush()
    return row


def verdicts_for(db: Session, attempt_id: UUID) -> dict[str, dict]:
    rows = db.scalars(
        select(models.StepVerdict).where(models.StepVerdict.attempt_id == attempt_id)
    ).all()
    return {str(r.step_id): {"verdict": r.verdict, "note": r.note} for r in rows}


# --------------------------------------------------------------------------- cap
def count_agent_call(db: Session, attempt: models.ReviewSession, *, counts: bool = True) -> int:
    """Increment the rerun counter. It lives on the ATTEMPT so sibling branches
    cannot bypass it, and a confirmed infrastructure failure (`counts=False`)
    must not burn one of the annotator's runs."""
    if counts:
        attempt.agent_call_count = int(attempt.agent_call_count or 0) + 1
        db.flush()
    return attempt.agent_call_count
