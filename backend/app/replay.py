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
        out.steps.append({"index": i, "kind": kind, "resolved": res.get("resolved") or {}, "worldHash": digest})
        out.final_world = world

        if expected_hashes and i < len(expected_hashes):
            want = expected_hashes[i]
            # An action the human took that changed nothing recorded no hash; it
            # cannot vouch for anything, so it does not get to fail the replay.
            if want and digest != want:
                return _fail(out, i, "the world diverged from what this action produced when it was recorded", strict, detail=kind)
    return out


def _fail(out: ReplayResult, at: int, reason: str, strict: bool, detail: str = "") -> ReplayResult:
    out.ok = False
    out.rejected_at = at
    out.reason = reason
    if strict:
        raise ReplayRejected(at, reason, detail)
    return out


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
) -> ReplayResult:
    """The full §3.6 gate: put the environment back where the branch starts, then
    replay. Restoration is verified by hash before a single action runs — starting
    from the wrong state would make every downstream comparison meaningless."""
    if checkpoint is not None and not checkpoints.restore(checkpoint, gym, task_id=task_id, seed=seed):
        raise ReplayRejected(0, "could not restore the branch's starting checkpoint")
    return replay(actions, executor, expected_hashes=expected_hashes,
                  clock=advance_clock(gym), strict=strict)
