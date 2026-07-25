"""Finalization — the last gate before a trajectory becomes a shipped sample.

A score is meaningless unless it names exactly what produced it, so finalization
binds all four together (§3.7):

    BenchmarkRun -> trajectory_version + verifier_suite + final_checkpoint
    Submission   -> task_revision + approved_version + benchmark_run

And it only runs on a version that (a) a reviewer APPROVED and (b) replays
deterministically from a clean environment against that exact suite. Both gates
matter: approval without replay ships a trajectory nobody can reproduce; replay
without approval ships one nobody read.

The replay here starts from the ROOT, not from a fork checkpoint. Restoring a
serialized checkpoint is an optimization for editing; for the deliverable, the
whole sequence has to run from a clean reset, or "it reproduces" only means "it
reproduces from the state we happened to save".
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import checkpoints, models, replay, versions


class NotApproved(RuntimeError):
    """QC has not approved this version. The post-submission `accepted` flag is
    too late to serve as the gate — by then it has already shipped."""


class Scorer(Protocol):
    """Runs the bound verifier suite against the world the replay ended in."""

    def score(self, suite: models.VerifierSuite, world: dict | None) -> tuple[int, dict]: ...


def actions_of(db: Session, version: models.TrajectoryVersion) -> list[dict]:
    """The flattened trajectory as structured, replayable actions. Steps with no
    locator (legacy rows recorded before the action IR) are surfaced, not
    silently dropped — a shortened replay would "pass" by doing less."""
    out = []
    for s in versions.flatten(db, version):
        out.append({
            "kind": s.action_type,
            "locator": s.semantic_locator or {},
            "args": s.arguments or {},
            "stepId": str(s.id),
            "actor": s.actor,
            "expectedHash": checkpoints.hash_world(s.world_after) if s.world_after else "",
        })
    return out


def replayable(actions: list[dict]) -> tuple[bool, list[int]]:
    """Which actions carry enough to be re-executed. `navigate` needs only a URL;
    everything else needs a locator."""
    missing = [
        i for i, a in enumerate(actions)
        if a["kind"] not in ("navigate", "press") and not a["locator"]
    ]
    return (not missing), missing


def finalize(
    db: Session,
    *,
    attempt: models.ReviewSession,
    version: models.TrajectoryVersion,
    suite: models.VerifierSuite,
    executor: replay.Executor,
    gym,
    scorer: Scorer,
    annotator_id: UUID | None = None,
    task_external_id: str = "",
    accept_failing: bool = False,
    require_replay: bool = True,
) -> dict:
    """Replay, score, bind, freeze. Raises rather than shipping something unbound."""
    if version.attempt_id != attempt.id:
        raise versions.LineageError("that version belongs to another attempt")
    if suite.session_id != attempt.id:
        raise versions.LineageError("that verifier suite belongs to another attempt")
    if version.status != "approved":
        raise NotApproved("finalization needs a QC-approved version")

    actions = actions_of(db, version)
    if not actions:
        raise NotApproved("this version has no steps to finalize")

    # A step the annotator marked WRONG must not be in the shipped golden. Forking
    # before it is how that normally happens, but a verdict recorded without a
    # fork left the step in place and nothing downstream looked — while the UI
    # told them "steps you rejected are not in it". Either the promise is enforced
    # or it is a lie; enforcing it is the cheaper of the two.
    rejected = _rejected_steps(db, attempt.id, version)
    if rejected:
        raise NotApproved(
            "this version still contains "
            + ("a step" if len(rejected) == 1 else f"{len(rejected)} steps")
            + " you marked wrong — fork before "
            + ("it" if len(rejected) == 1 else "them")
            + " so the correction replaces it, or clear the verdict if it was a mistake"
        )

    result = None
    if require_replay:
        ok, missing = replayable(actions)
        if not ok:
            raise replay.ReplayRejected(
                missing[0], "this step has no semantic locator, so the trajectory cannot be replayed"
            )
        # A CLEAN reset — not a checkpoint restore. The deliverable must reproduce
        # from the task's own starting conditions.
        if gym.reset(task_external_id, attempt.seed) is None:
            raise replay.ReplayRejected(0, "could not reset the task for a clean replay")
        result = replay.replay(
            actions, executor,
            expected_hashes=[a["expectedHash"] for a in actions],
            # The clean replay must reproduce the RECORDING's protocol, clock and
            # all. Without the tick the replayed world trails by one step and a
            # correct trajectory is rejected as diverged. The scheduled-tick
            # decision is read off the gym's own seed world (the shared helper), so
            # a trajectory reconstructed with a tick is replayed with one.
            clock=replay.scheduled_clock(gym),
            strict=True,
        )

    final_world = (result.final_world if result else None) or (gym.world() if hasattr(gym, "world") else None)
    final_cp = checkpoints.capture(
        db, attempt_id=attempt.id, world=final_world, step_clock=len(actions),
    )
    reward, results = scorer.score(suite, final_world)
    # A run that does not pass its own suite is not shipped by accident. The
    # legacy path refused this outright unless a human took the override on the
    # record; keeping the gate means "the verifiers say no" cannot become a
    # dataset row through nothing more than clicking the last button.
    if reward != 1 and not accept_failing:
        raise NotApproved(
            "this version does not pass its verifier suite — ship it deliberately as a breaker "
            "if that is what you mean, or fix the correction first"
        )
    overridden: list[str] = []  # v2 has no override path yet; the rule is here so adding one cannot forget it

    # The sample's kind is DERIVED, never named by the caller. The legacy path
    # derives it (api/sessions.py) precisely so a reward reached by overriding a
    # SAFETY verifier ships as `flagged` rather than as training gold — letting a
    # client assert "golden" would drop that provenance silently, and this path
    # could never produce `flagged` at all.
    kind = _kind_for(suite, reward, overridden)

    run = models.BenchmarkRun(
        suite_id=suite.id, reward=reward, results=results,
        trajectory_version_id=version.id, final_checkpoint_id=final_cp.id,
    )
    db.add(run)
    db.flush()

    sub = models.Submission(
        session_id=attempt.id, reward=reward, kind=kind,
        task_revision=attempt.task_revision,
        approved_trajectory_version_id=version.id,
        benchmark_run_id=run.id,
        snapshot=freeze(db, attempt=attempt, version=version, suite=suite, run=run, final_checkpoint=final_cp),
    )
    db.add(sub)
    versions.set_status(db, version, "published", expected_revision=version.revision)
    db.flush()
    return {
        "submissionId": str(sub.id), "benchmarkRunId": str(run.id),
        "versionId": str(version.id), "reward": reward, "results": results,
        "finalCheckpointId": str(final_cp.id),
        "replayed": bool(result), "steps": len(actions),
    }


def _rejected_steps(db: Session, attempt_id: UUID, version: models.TrajectoryVersion) -> list[str]:
    """Steps in this version that carry a `rejected` verdict for this attempt."""
    marked = {
        sid for sid, v in (
            (str(r.step_id), r.verdict) for r in db.scalars(
                select(models.StepVerdict).where(models.StepVerdict.attempt_id == attempt_id)
            )
        ) if v == "rejected"
    }
    if not marked:
        return []
    return [str(s.id) for s in versions.flatten(db, version) if str(s.id) in marked]


def _kind_for(suite: models.VerifierSuite, reward: int, overridden: list[str]) -> str:
    """golden | breaker | flagged, on the same rule the legacy path uses.

    A run that only passes because a human forced a SAFETY check is not a clean
    golden. The provenance has to ride on the sample, or an unsafe trajectory
    ships as something to train on.
    """
    if any(v.ext_id in set(overridden or []) and v.level == "safety" for v in suite.verifiers):
        return "flagged"
    return "golden" if reward == 1 else "breaker"


def freeze(
    db: Session,
    *,
    attempt: models.ReviewSession,
    version: models.TrajectoryVersion,
    suite: models.VerifierSuite,
    run: models.BenchmarkRun,
    final_checkpoint: models.EnvironmentCheckpoint,
) -> dict:
    """The deliverable, captured at submit time.

    Freezing matters because everything it references keeps moving: the canonical
    run gets re-captured, suites get edited, later benchmarks get recorded. A
    shipped sample must say what was actually reviewed and scored, not what those
    rows look like today.
    """
    verifiers = [
        {"id": v.ext_id, "level": v.level, "assertion": v.assertion, "code": v.code,
         "check": v.check_ir or None, "gym_result": v.gym_result or None,
         "added_by_human": v.added_by_human}
        for v in suite.verifiers
    ]
    lineage = [
        {"versionNo": v.version_no, "kind": v.kind, "status": v.status, "producer": v.producer,
         "forkBeforeStepId": str(v.fork_before_step_id) if v.fork_before_step_id else None}
        for v in versions.chain(db, version)
    ]
    steps = []
    for n, s in enumerate(versions.flatten(db, version)):
        after = db.get(models.EnvironmentCheckpoint, s.after_checkpoint_id) if s.after_checkpoint_id else None
        steps.append({
            "idx": n, "stepId": str(s.id), "actor": s.actor, "type": s.action_type,
            "description": s.description, "locator": s.semantic_locator or {},
            "resolved": s.resolved_target or {}, "args": s.arguments or {},
            "url": s.url_after, "screenshot": s.screenshot_url or None,
            "reasoning": s.reasoning or "", "human_intent": s.human_intent or "",
            "guidance": s.guidance_text or "",
            "world_hash": after.world_hash if after else "",
        })
    return {
        "task_revision": attempt.task_revision,
        "trajectory_version": {
            "id": str(version.id), "versionNo": version.version_no, "kind": version.kind,
            "environment_image_digest": version.environment_image_digest,
            "lineage": lineage,
        },
        "golden_trajectory": steps,
        "verifiers": verifiers,
        "suite_version": suite.version,
        "reward": run.reward,
        "overridden": run.overridden or [],
        "final_world_hash": final_checkpoint.world_hash,
        "final_checkpoint_id": str(final_checkpoint.id),
    }
