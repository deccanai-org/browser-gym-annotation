"""Deterministic replay — the gate a trajectory must pass before it is golden.

A human explores, then proposes a sequence of actions to COMMIT. That proposal is
a claim, not a fact: it routinely depends on state the exploration created and the
commit discarded (a dropdown that was opened, a filter that was applied, a tab
that was switched). Replay is how we find out, per §3.6:

    ① restore the parent checkpoint into a clean environment
    ② execute each committed action, structurally — never an LLM
    ③ compare world hashes after every action
    ④ reject on divergence or on an action that did not land
    ⑤ only a validated sequence becomes the new committed head

Rejecting is the point. A trajectory that "mostly replays" is worse than none: it
ships as ground truth and then fails to reproduce for whoever trains on it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from app import checkpoints


class ReplayRejected(RuntimeError):
    """The proposed sequence is not reproducible. Carries the index so the UI can
    point at the exact action that broke."""

    def __init__(self, at: int, reason: str, detail: str = ""):
        super().__init__(f"action {at}: {reason}" + (f" ({detail})" if detail else ""))
        self.at = at
        self.reason = reason
        self.detail = detail


Clock = Callable[[int], object]


class Executor(Protocol):
    """Whatever can perform one structured action and report the world after it.
    The live browser implements this; tests supply a fake."""

    def act(self, kind: str, locator: dict | None, args: dict | None) -> dict: ...
    def world(self) -> dict | None: ...


@dataclass
class ReplayResult:
    ok: bool
    steps: list[dict] = field(default_factory=list)   # per-action outcome + world hash
    final_world: dict | None = None
    rejected_at: int | None = None
    reason: str = ""


def advance_clock(gym, *, scheduled: bool = False) -> "Clock | None":
    """The recording protocol's clock tick, as a callable.

    The gym's deterministic clock IS its step counter, and it is advanced by
    `/_harness/verify {step}` — which is what the harness calls after every agent
    action. A replay that skips it leaves the world one tick behind the world the
    recording captured, so EVERY hash comparison fails and a perfectly good
    trajectory is rejected as diverged. Found by walking the whole loop once: the
    first finalize of a real correction failed at action 0.

    `scheduled` additionally ticks, for the 18 tasks that queue async events. The
    backfill reconstructs those worlds WITH a tick, so a replay that cannot tick
    can never reproduce what it wrote — the same write-path/read-path split that
    has now produced five bugs in this codebase. It is conditional because an
    unconditional tick is a measured regression: `advance_and_flush` assigns
    `sched.now` before consulting the queue and `now` is inside the hashed world,
    so ticking a task with nothing scheduled corrupts every comparison while
    delivering nothing (47/60 steps to 5/60, measured).
    """
    verify = getattr(gym, "verify", None)
    if verify is None:
        return None
    tick = getattr(gym, "tick", None) if scheduled else None

    def _advance(i: int) -> None:
        # Tick BEFORE verify, matching harness/runner.py: the harness ticks at the
        # start of a turn so the observation and the verifier see the same world.
        if tick is not None:
            tick(i)
        verify(i)

    return _advance


def scheduled_clock(gym) -> "Clock | None":
    """`advance_clock` with the scheduled-tick decision read from the gym's own
    seed world — the shared choice a fresh-reset replay makes.

    Whether to tick for async events is decided from the world the reset produced,
    not argued about per caller. Both the finalize replay and the fork-prefix
    rebuild reset first and then replay, so both make exactly this decision; keeping
    it in one place stops the subtle write-path/read-path split (an unconditional
    tick is a measured regression) from being re-derived and drifting.
    """
    from app import backfill  # local: backfill imports models/gym_client, not replay

    world = gym.world() if hasattr(gym, "world") else None
    return advance_clock(gym, scheduled=backfill.scheduled_events(world) > 0)


def replay(
    actions: list[dict],
    executor: Executor,
    *,
    expected_hashes: list[str] | None = None,
    clock: "Clock | None" = None,
    strict: bool = True,
) -> ReplayResult:
    """Run the committed sequence against a restored environment.

    `expected_hashes[i]` (when given) is the world hash this action produced when
    the human performed it. A mismatch means the same actions in the same order
    reached a different state — the sequence depended on something that is not in
    it. `strict=False` records the divergence without raising, for a dry run that
    wants to show the annotator where it broke.
    """
    out = ReplayResult(ok=True)
    for i, a in enumerate(actions):
        kind = a.get("kind") or a.get("action_kind") or ""
        res = executor.act(kind, a.get("locator") or a.get("semantic_locator"), a.get("args") or a.get("arguments"))
        if not (res or {}).get("ok"):
            reason = (res or {}).get("error") or "the action did not land"
            # The classic case: the human opened a menu while exploring, committed
            # only the option click, and the option no longer exists.
            return _fail(out, i, reason, strict, detail=kind)

        # Advance the deterministic clock exactly as the recording did, BEFORE
        # reading the world — otherwise the replayed world trails the recorded one
        # by a tick and every comparison below diverges.
        if clock is not None:
            clock(i)
        world = executor.world()
        digest = checkpoints.hash_world(world)
        # `ok` is what this record exists to say, and it was never written. Its
        # only reader — api/versions.py::certify — does `elif out.get("ok")`, so a
        # step that replayed perfectly fell through to the else branch and was
        # marked `diverged` with "did not replay". That is EVERY step of EVERY
        # successful replay, which is why certify has never passed and why the
        # hand-done attempts in the database look stranded. The tell is in the
        # audit row: `firstFailureAt: null` beside eleven diverged steps — nothing
        # had failed at all.
        # `compared` says whether this step's world was actually CHECKED against
        # anything. A step the recording captured no world for has no expectation
        # to diverge from, so it passes the gate by default — and a trajectory
        # where that is true of EVERY step replays "green" while proving nothing
        # at all. Seen on a real M116 run: 14 steps, 0 recorded worlds, certify
        # reported 14/14 verified, and the gym then said the task was not solved.
        # "A trajectory that mostly replays is worse than none" — this is the
        # degenerate case of that, and the caller has to be able to see it.
        want_i = (expected_hashes[i] if expected_hashes and i < len(expected_hashes) else "")
        out.steps.append({"index": i, "ok": True, "kind": kind, "compared": bool(want_i),
                          "resolved": res.get("resolved") or {}, "worldHash": digest})
        out.final_world = world

        if expected_hashes and i < len(expected_hashes):
            want = expected_hashes[i]
            # An action the human took that changed nothing recorded no hash; it
            # cannot vouch for anything, so it does not get to fail the replay.
            if want and digest != want:
                # ASK AGAIN before calling it divergence.
                #
                # The mock UIs push to the engine asynchronously, so the world can
                # still be one push behind when we read it — and the read lands
                # milliseconds after the action's ack. A stale read then looks
                # exactly like a diverged world.
                #
                # This is not hypothetical and it is not rare: on a real M101
                # trajectory the gift-message edit only reaches the engine on the
                # checkout click, so steps 0-6 all hash the same and step 7 is
                # where the change appears. A cold first run won that race and
                # certified 11/11; every back-to-back run after it lost the race
                # and reported the SAME trajectory as diverged at step 7.
                world = _settled_world(executor, want)
                digest = checkpoints.hash_world(world)
                out.steps[-1]["worldHash"] = digest
                out.final_world = world
            if want and digest != want:
                # Mark the RECORD too, not just the result. The step was appended
                # as ok before the comparison ran, and certify reads the records:
                # a diverged step came back labelled `verified` while the overall
                # answer said the trajectory failed at it. Every step green and
                # the gate red is the worst of both — it reads as a gate bug, so
                # the actual failure gets looked past.
                out.steps[-1]["ok"] = False
                out.steps[-1]["diverged"] = True
                return _fail(out, i, "the world diverged from what this action produced when it was recorded", strict, detail=kind)
    return out


#: How long to keep asking, and how often.
#:
#: Sized against the SLOWEST way a mock reaches the engine, not the fastest. Some
#: mutations ride the mock's own ~2.5s re-poll rather than an immediate push, so
#: a 720ms window (the first guess) could not see them at all: replaying M105
#: filled the compose form correctly, clicked a Send that really was the Send
#: button, and read a world with no sent mail in it — indistinguishable from a
#: click that did nothing, and it failed the trajectory at its last step.
#:
#: The cost is bounded by the fact that this only runs on a MISMATCH and returns
#: the moment the world agrees. A replay where nothing diverges never waits at
#: all; one that genuinely diverges pays this once and then stops.
_SETTLE_TRIES = 25
_SETTLE_MS = 200


def _settled_world(executor: Executor, want: str) -> dict | None:
    """Re-read until the world matches `want`, or we run out of patience.

    Returns the last world read either way — a mismatch that survives this IS a
    divergence and must still fail the replay. The point is only that a read
    taken before the engine caught up is not evidence of one.
    """
    world = None
    for _ in range(_SETTLE_TRIES):
        time.sleep(_SETTLE_MS / 1000)
        world = executor.world()
        if checkpoints.hash_world(world) == want:
            return world
    return world


def _fail(out: ReplayResult, at: int, reason: str, strict: bool, detail: str = "") -> ReplayResult:
    out.ok = False
    out.rejected_at = at
    out.reason = reason
    if strict:
        raise ReplayRejected(at, reason, detail)
    return out


def recording_ticked(worlds: list[dict | None]) -> bool:
    """Did the run being replayed advance the gym's clock?

    An AGENT trajectory does: the harness calls `/_harness/verify {step}` after
    every action, so its recorded worlds carry a rising `step`. A human working
    in the live gym never calls it, so every world they record is at the step the
    session opened on.

    Replaying a human run with the agent's clock ticks a counter the recording
    never moved, and `step` is inside the hashed world — so the comparison fails
    on the tick alone. On M105 that was three of the four differing leaves
    (`.step`, `.shop.step`, `.events[0].step`), and it failed the trajectory at
    its last action for a world that was otherwise identical.

    Read from the recording rather than from the attempt's mode, because it is
    the recording that has to be reproduced.
    """
    seen = {w.get("step") for w in worlds if isinstance(w, dict) and "step" in w}
    return len(seen) > 1


def restore_and_replay(
    checkpoint: Any,
    actions: list[dict],
    executor: Executor,
    gym: Any,
    *,
    task_id: str,
    seed: int,
    expected_hashes: list[str] | None = None,
    strict: bool = True,
    advance: bool = True,
) -> ReplayResult:
    """The full §3.6 gate: put the environment back where the branch starts, then
    replay. Restoration is verified by hash before a single action runs — starting
    from the wrong state would make every downstream comparison meaningless."""
    if checkpoint is not None and not checkpoints.restore(checkpoint, gym, task_id=task_id, seed=seed):
        raise ReplayRejected(0, "could not restore the branch's starting checkpoint")
    return replay(actions, executor, expected_hashes=expected_hashes,
                  clock=advance_clock(gym) if advance else None, strict=strict)
