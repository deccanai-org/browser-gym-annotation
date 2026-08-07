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

from app import checkpoints, disposition, models, replay, versions, worlddiff
from app.config import settings


class NotApproved(RuntimeError):
    """QC has not approved this version. The post-submission `accepted` flag is
    too late to serve as the gate — by then it has already shipped.

    Carries the per-check outcome when the refusal came from the SUITE. The
    message alone said "this version does not pass its verifier suite", which
    does not tell an annotator the one thing they need: whether a check ran and
    said no, or never ran at all. Those have opposite fixes — redo the task
    versus fix the check — and under the fail-closed rule an unprovable check
    blocks a ship exactly as loudly as a failing one.
    """

    def __init__(self, message: str, *, results: dict | None = None,
                 executed: list[str] | None = None, unproven: list[str] | None = None) -> None:
        super().__init__(message)
        self.results = results or {}
        self.executed = list(executed or [])
        self.unproven = list(unproven or [])


class Scorer(Protocol):
    """Runs the bound verifier suite against the world the replay ended in, and
    the TRACE it took to get there.

    The trace is a parameter because leaving it out made an entire class of check
    undeliverable. `trace_max_steps`, `trace_hosts_subset`, `trace_action_count`
    and the LLM `trace_policy` checks all read `ctx["trace"]`, and the autogen
    path appends policy checks to every suite it generates — so any adopted
    autogen suite could never reach reward 1, forever, because the scorer had no
    trace to run them against and recorded them unprovable. It was never actually
    unavailable: finalize builds the action list and replays it immediately
    before scoring.

    Optional so an older scorer still satisfies the protocol; `finalize` passes
    it whenever the callee accepts it.
    """

    def score(self, suite: models.VerifierSuite, world: dict | None,
              trace: list[dict] | None = None) -> tuple[int, dict]: ...


def trace_of(db: Session, version: models.TrajectoryVersion) -> list[dict]:
    """The flattened trajectory in the shape the trace checks read.

    Deliberately NOT `actions_of`: that is the replay IR (kind/locator/args) and
    the checks want `type` and `tabId` — `trace_hosts_subset` asks which tabs the
    run touched, `trace_action_count` counts a kind on a tab. Same rows, a
    different projection.
    """
    return [
        {"type": s.action_type, "tabId": s.tab_id, "stepId": str(s.id),
         "actor": s.actor, "url": s.url_after, "description": s.description}
        for s in versions.flatten(db, version)
    ]


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


# Kinds that address no page element at all, so a missing locator is not a defect
# in them. A tab switch names an APP, not a node — without it here, every
# multi-app trajectory (the whole point of the cross-app breakers) would be
# refused at the last gate for "this step has no semantic locator".
#
# Deliberately NARROWER than restore._LOCATOR_FREE: a fork REBUILD only needs the
# action to run, but a SHIPPED trajectory must carry a locator for everything a
# consumer could later inspect, so `scroll`/`wait` stay out of this set.
_NO_LOCATOR = frozenset({"navigate", "press", "switch_tab", "open_tab", "close_tab"})


def replayable(actions: list[dict]) -> tuple[bool, list[int]]:
    """Which actions carry enough to be re-executed. `navigate` needs only a URL,
    a tab switch only an app; everything else needs a locator."""
    missing = [
        i for i, a in enumerate(actions)
        if a["kind"] not in _NO_LOCATOR and not a["locator"]
    ]
    return (not missing), missing


def gate_report(db: Session, attempt: models.ReviewSession,
                version: models.TrajectoryVersion | None,
                suite: models.VerifierSuite | None) -> list[dict]:
    """Every reason this attempt cannot ship yet, phrased as the thing to go and do.

    `finalize` raises on the FIRST unmet gate, which is right for an operation
    that must not half-ship — but it makes the UI a guessing game: fix one thing,
    press the button, discover the next. This reads the same predicates and
    returns all of them, so the annotator sees the whole list at once.

    Deliberately the same source of truth. A second, hand-maintained copy of
    "what blocks a ship" would drift, and the drift would show up as a button
    that is enabled and then refuses.
    """
    out: list[dict] = []
    if version is None:
        return [{"code": "no_version", "message": "Nothing has been recorded on this task yet."}]

    actions = actions_of(db, version)
    if not actions:
        out.append({"code": "no_steps",
                    "message": "You have not recorded any actions yet."})
    if version.status != "approved":
        out.append({
            "code": "not_approved",
            "message": (f"v{version.version_no} is the head, but nobody has approved it — "
                        f"approve it in Version lineage. Finalize refuses a version no reviewer signed off."),
        })
    rejected = _rejected_steps(db, attempt.id, version)
    if rejected:
        out.append({
            "code": "rejected_steps",
            "message": ("this version still contains "
                        + ("a step" if len(rejected) == 1 else f"{len(rejected)} steps")
                        + " you marked wrong — fork before "
                        + ("it" if len(rejected) == 1 else "them")
                        + " so the correction replaces it, or clear the verdict if it was a mistake"),
        })
    if actions:
        ok, missing = replayable(actions)
        if not ok:
            out.append({
                "code": "unreplayable_step",
                "message": (f"step {missing[0] + 1} has no semantic locator, so the trajectory "
                            f"cannot be replayed."),
                "at": missing[0],
            })
    if suite is None or not (suite.verifiers or []):
        out.append({
            "code": "no_verifiers",
            "message": "This attempt has no verifier suite — generate one in step 2 before shipping.",
        })
    return out


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
    rewrite=None,
) -> dict:
    """Replay, score, bind, freeze. Raises rather than shipping something unbound.

    `rewrite` retargets each recorded action at the surface it is being replayed
    on (see app/replay_surface.py). A recorded `navigate` carries the attempt's
    own sid, so replaying it verbatim in a scratch world would drive the browser
    straight back into the annotator's — the one world this must not touch.
    """
    if version.attempt_id != attempt.id:
        raise versions.LineageError("that version belongs to another attempt")
    if suite.session_id != attempt.id:
        raise versions.LineageError("that verifier suite belongs to another attempt")
    if version.status != "approved":
        raise NotApproved("finalization needs a QC-approved version")

    actions = actions_of(db, version)
    if not actions:
        raise NotApproved("this version has no steps to finalize")
    if rewrite is not None:
        actions = [rewrite(a) for a in actions]

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
    # Hand over the trace as well as the world. Older scorers take two arguments,
    # so this asks rather than assumes — a TypeError here would fail a ship for a
    # reason that has nothing to do with the sample.
    trace = trace_of(db, version)
    try:
        reward, results = scorer.score(suite, final_world, trace)
    except TypeError:
        reward, results = scorer.score(suite, final_world)
    # A run that does not pass its own suite is not shipped by accident. The
    # legacy path refused this outright unless a human took the override on the
    # record; keeping the gate means "the verifiers say no" cannot become a
    # dataset row through nothing more than clicking the last button.
    if reward != 1 and not accept_failing:
        # Say WHICH checks, and which of them never ran. "Does not pass its
        # verifier suite" sent the annotator back to the task when the actual
        # problem was often a check that could not be executed at all — opposite
        # fixes, and under the fail-closed rule an unprovable check blocks a ship
        # exactly as loudly as a failing one.
        failed = sorted(k for k, r in (results or {}).items() if r == "fail")
        unproven = sorted(k for k, r in (results or {}).items() if r not in ("pass", "fail"))
        if unproven and not failed:
            msg = ("this version cannot be scored: "
                   + ", ".join(unproven)
                   + (" could not be run" if len(unproven) == 1 else " could not be run")
                   + ". Fix or remove those checks — a sample is only worth what its "
                     "verifiers actually proved.")
        elif failed:
            msg = ("this version does not pass " + ", ".join(failed)
                   + (" and " + ", ".join(unproven) + " could not be run" if unproven else "")
                   + ". Fix the run, or ship it deliberately as a breaker if that is what you mean.")
        elif not results:
            msg = ("this version has no verifier that proves anything — an empty suite "
                   "cannot earn a reward.")
        else:
            # Reward 0 while every check reads pass. Nothing here can explain
            # that, so do not invent a reason; the general refusal is the honest
            # one.
            msg = ("this version does not pass its verifier suite — ship it deliberately as a "
                   "breaker if that is what you mean, or fix the correction first")
        raise NotApproved(msg, results=results,
                          executed=sorted(k for k, r in (results or {}).items() if r in ("pass", "fail")),
                          unproven=unproven)
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


def _observation_ref(db: Session, s: models.TrajectoryStep) -> dict | None:
    """The page this step was taken against, as a content reference.

    Lives on the step's checkpoint (`dom_artifact_id`) because an observation
    describes a WORLD, not an action, and many steps can share one unchanged
    page — content addressing then costs a single copy. `elements` and `url` are
    lifted out of the artifact's meta so a consumer can decide whether to fetch
    the body at all.
    """
    cp = db.get(models.EnvironmentCheckpoint, s.after_checkpoint_id) if s.after_checkpoint_id else None
    art = db.get(models.Artifact, cp.dom_artifact_id) if cp is not None and cp.dom_artifact_id else None
    if art is None or not art.sha256:
        return None
    meta = art.meta or {}
    return {"path": art.uri, "sha256": art.sha256, "bytes": art.bytes,
            "url": meta.get("url", ""), "elements": meta.get("elements"),
            "truncated": bool(meta.get("truncated"))}


def _artifact_ref(db: Session, s: models.TrajectoryStep) -> dict | None:
    """The step's screenshot as something a CLIENT can use.

    A bundle used to ship `"screenshot": "attempt/<id>/<id>.jpg"` — a path into a
    deployment the buyer has no access to, for bytes the platform had never
    written. This ships the archive-relative content path and the digest, so the
    image can be located inside the export and verified, and a step with no frame
    says so with null instead of a dangling string.
    """
    art = db.get(models.Artifact, s.marks_artifact_id) if s.marks_artifact_id else None
    if art is None or not art.sha256:
        return None
    return {"path": art.uri, "sha256": art.sha256, "bytes": art.bytes,
            "width": (art.meta or {}).get("width"), "height": (art.meta or {}).get("height")}


def _environment_digest(db: Session, attempt: models.ReviewSession,
                        version: models.TrajectoryVersion) -> tuple[str, str]:
    """Which environment BUILD this trajectory was recorded against, and how we know.

    The version's own column is authoritative but is routinely "" — nothing stamps
    it unless the attempt ran in a provisioned workspace — and the export shipped
    that empty string in the one field that pins the build, so every bundle claimed
    the same (blank) environment. Fall back to the attempt's other stamped
    evidence, then to the image this deployment is configured to run, and only
    then admit we do not know. The SOURCE ships with it, because "the digest this
    process happens to be configured with" is a much weaker claim than "the digest
    stamped on the version" and a consumer has to be able to tell them apart.
    """
    if version.environment_image_digest:
        return version.environment_image_digest, "trajectory_version"
    digest = disposition.environment_digest(db, attempt)
    if digest:
        return digest, "attempt"
    if settings.gym_image_digest:
        return settings.gym_image_digest, "settings.gym_image_digest"
    return "", ""


def _task_block(db: Session, attempt: models.ReviewSession) -> dict:
    """The task AS ANNOTATED.

    Export used to rebuild this from the live `task` row at download time, so a
    catalog reseed — which rewrites prompt/category/meta wholesale — could restate
    what a shipped sample says the annotator was asked to do. `seed` is the
    ATTEMPT's seed, not the task's current one: the golden was recorded and scored
    under it, and a client resetting at a later task.seed gets a different world.
    """
    task = db.get(models.Task, attempt.task_id)
    if task is None:
        return {}
    meta = task.meta or {}
    return {
        "id": task.external_id,
        "revision": attempt.task_revision,
        "prompt": task.prompt,
        "category": task.category,
        "difficulty": task.difficulty,
        "constraints": meta.get("constraints", []),
        "allowed_sites": meta.get("allowedSites", []),
        "seed": attempt.seed,
        "start_url": task.start_url,
    }


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
            "url": s.url_after,
            # A CONTENT reference, not a URL. `screenshot_url` is the platform's
            # own route and means nothing once the bundle leaves the deployment;
            # this is the path inside the exported archive plus the digest to
            # check it against, so a client can tell a missing image from a
            # corrupted one. None when the step genuinely has no frame.
            "screenshot": _artifact_ref(db, s),
            # What the page looked like. The other half of an SFT pair: without
            # it the sample says what was done and nothing about what could be
            # seen. Captured on the ack, so it is the page AFTER this action —
            # i.e. the input the NEXT step was decided from.
            "observation": _observation_ref(db, s),
            "reasoning": s.reasoning or "", "human_intent": s.human_intent or "",
            "guidance": s.guidance_text or "",
            "world_hash": after.world_hash if after else "",
            # The state transition this step produced — the SFT target's actual
            # signal, and the DB-diff a verifier validates against.
            "world_delta": s.world_delta or None,
            "state_change": worlddiff.summarize(s.world_delta),
            "delta_span": (s.delta_span or {}).get("stepIds") or [],
            # Provenance a consumer filtering for locator-grounded data needs;
            # neither was ever exported before.
            "coordinate_fallback": s.coordinate_fallback or {},
            "replay_state": s.replay_state,
        })
    # The run UNDER REVIEW, frozen alongside the golden. Export rebuilt it live
    # from the trajectory rows even on this path, so a later re-capture of the
    # canonical run rewrote the "recorded" half of a sample that had shipped —
    # while the docstring above promised it could not drift.
    from app.api.export import _base_trajectory, _steps_of  # local: avoid an import cycle

    digest, digest_source = _environment_digest(db, attempt, version)
    return {
        "task_revision": attempt.task_revision,
        "task": _task_block(db, attempt),
        "trajectory_version": {
            "id": str(version.id), "versionNo": version.version_no, "kind": version.kind,
            "environment_image_digest": digest,
            "environment_image_digest_source": digest_source,
            "lineage": lineage,
        },
        "recorded_trajectory": _steps_of(_base_trajectory(db, attempt)),
        "golden_trajectory": steps,
        "verifiers": verifiers,
        "suite_version": suite.version,
        "reward": run.reward,
        # The per-check outcomes behind that 0/1. Without them the bundle says a
        # run failed and nothing about WHICH assertion it failed, which is the
        # only part a buyer of a breaker sample can act on.
        "results": dict(run.results or {}),
        "overridden": run.overridden or [],
        "final_world_hash": final_checkpoint.world_hash,
        "final_checkpoint_id": str(final_checkpoint.id),
        # The attempt-scope diff: the seeded world vs what the annotator's work
        # produced. Frozen here because everything it references keeps moving.
        "initial_world_hash": (
            (db.get(models.EnvironmentCheckpoint, attempt.initial_checkpoint_id) or
             models.EnvironmentCheckpoint()).world_hash
            if attempt.initial_checkpoint_id else ""),
        "world_summary": attempt.world_summary or {},
    }
