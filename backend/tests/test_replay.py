"""Replay validation — the gate that stops an unreproducible sequence from
becoming a golden trajectory."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app import checkpoints, models, replay


class FakeExecutor:
    """A tiny world model: actions resolve only when their precondition holds, so
    a sequence that skipped a step genuinely cannot run."""

    def __init__(self, present=("btn-cart", "link-checkout"), effects=None):
        self.present = set(present)
        self.effects = effects or {}
        self.state = {"cart": [], "step": 0}
        self.performed: list[str] = []

    def act(self, kind, locator, args):
        target = (locator or {}).get("testId") or (locator or {}).get("id") or ""
        if kind == "navigate":
            self.state["step"] += 1
            self.performed.append("navigate")
            return {"ok": True, "resolved": {"url": (args or {}).get("url", "")}}
        if target not in self.present:
            return {"ok": False, "error": "no element matched the locator"}
        self.performed.append(target)
        self.state["step"] += 1
        for add in self.effects.get(target, {}).get("reveals", []):
            self.present.add(add)
        for item in self.effects.get(target, {}).get("adds", []):
            self.state["cart"].append(item)
        return {"ok": True, "resolved": {"selector": f'[data-test-id="{target}"]'}}

    def world(self):
        return dict(self.state)


def _actions(*targets):
    return [{"kind": "click", "locator": {"testId": t}} for t in targets]


# --------------------------------------------------------------------------- happy path
def test_a_self_contained_sequence_replays():
    ex = FakeExecutor()
    out = replay.replay(_actions("btn-cart", "link-checkout"), ex)
    assert out.ok and ex.performed == ["btn-cart", "link-checkout"]
    assert [s["worldHash"] for s in out.steps] == [s["worldHash"] for s in out.steps if s["worldHash"]]


def test_the_resolved_target_is_recorded_per_action():
    """What actually matched is evidence; re-deriving it later could pick a
    different element."""
    out = replay.replay(_actions("btn-cart"), FakeExecutor())
    assert out.steps[0]["resolved"]["selector"] == '[data-test-id="btn-cart"]'


# --------------------------------------------------------------------------- the point
def test_a_sequence_depending_on_discarded_exploration_is_rejected():
    """THE verification requirement. The human opened a menu while exploring and
    committed only the option click; from a clean start the option isn't there."""
    ex = FakeExecutor(present=("btn-menu",), effects={"btn-menu": {"reveals": ["opt-gift"]}})
    with pytest.raises(replay.ReplayRejected) as ei:
        replay.replay(_actions("opt-gift"), ex)
    assert ei.value.at == 0 and "no element matched" in ei.value.reason
    assert ex.performed == [], "nothing must be committed from a rejected sequence"


def test_including_the_missing_step_makes_the_same_sequence_valid():
    """The complement: the fix is to commit the step that was skipped, and then it
    replays — so the gate is teaching the annotator, not just refusing."""
    ex = FakeExecutor(present=("btn-menu",), effects={"btn-menu": {"reveals": ["opt-gift"]}})
    out = replay.replay(_actions("btn-menu", "opt-gift"), ex)
    assert out.ok and ex.performed == ["btn-menu", "opt-gift"]


def test_a_mid_sequence_failure_reports_its_index():
    ex = FakeExecutor(present=("a", "c"))
    with pytest.raises(replay.ReplayRejected) as ei:
        replay.replay(_actions("a", "b", "c"), ex)
    assert ei.value.at == 1, "the UI has to point at the exact action that broke"


# --------------------------------------------------------------------------- divergence
def test_reaching_a_different_state_is_divergence_even_when_every_action_lands():
    """Every click succeeded, yet the world is not the one the human produced —
    the sequence depends on something it does not contain."""
    ex = FakeExecutor(present=("btn-cart",), effects={"btn-cart": {"adds": ["mug"]}})
    with pytest.raises(replay.ReplayRejected) as ei:
        replay.replay(_actions("btn-cart"), ex, expected_hashes=[checkpoints.hash_world({"cart": ["laptop"], "step": 1})])
    assert "diverged" in ei.value.reason


def test_matching_hashes_pass_the_gate():
    ex = FakeExecutor(present=("btn-cart",), effects={"btn-cart": {"adds": ["mug"]}})
    out = replay.replay(_actions("btn-cart"), ex, expected_hashes=[checkpoints.hash_world({"cart": ["mug"], "step": 1})])
    assert out.ok


def test_an_action_that_recorded_no_hash_cannot_fail_the_replay():
    """A hash-less expectation vouches for nothing; treating it as a mismatch
    would reject valid sequences captured before world hashing existed."""
    out = replay.replay(_actions("btn-cart"), FakeExecutor(), expected_hashes=[""])
    assert out.ok


def test_dry_run_reports_the_break_without_raising():
    """The annotator needs to SEE where it broke before deciding what to commit."""
    ex = FakeExecutor(present=("a",))
    out = replay.replay(_actions("a", "b"), ex, strict=False)
    assert out.ok is False and out.rejected_at == 1
    assert len(out.steps) == 1, "the successful prefix is still reported"


# --------------------------------------------------------------------------- restoration
class FakeGym:
    def __init__(self, world, loadable=True):
        self._world, self.loadable = world, loadable

    def load_state(self, task_id, seed, state, step=None):
        return {"ok": True} if self.loadable else None

    def world(self):
        return self._world


@pytest.fixture()
def checkpoint(db_session):
    task = models.Task(external_id=f"RP-{uuid4().hex[:6]}", title="t", prompt="p", source="gym")
    ann = models.Annotator(email=f"rp-{uuid4().hex[:6]}@test")
    db_session.add_all([task, ann])
    db_session.flush()
    s = models.ReviewSession(task_id=task.id, annotator_id=ann.id, source="gym")
    db_session.add(s)
    db_session.flush()
    cp = checkpoints.capture(db_session, attempt_id=s.id, world={"cart": [], "step": 0})
    db_session.commit()
    return cp


def test_restore_happens_before_a_single_action_runs(db_session, checkpoint):
    ex = FakeExecutor()
    gym = FakeGym({"cart": [], "step": 0})
    out = replay.restore_and_replay(checkpoint, _actions("btn-cart"), ex, gym, task_id="M40/x", seed=0)
    assert out.ok and ex.performed == ["btn-cart"]


def test_a_failed_restore_stops_the_replay_entirely(db_session, checkpoint):
    """Replaying from the wrong starting state makes every downstream comparison
    meaningless, so it must not start at all."""
    ex = FakeExecutor()
    with pytest.raises(replay.ReplayRejected) as ei:
        replay.restore_and_replay(checkpoint, _actions("btn-cart"), ex, FakeGym(None, loadable=False), task_id="M40/x", seed=0)
    assert "restore" in ei.value.reason
    assert ex.performed == []


def test_restoring_to_the_wrong_world_is_caught_by_the_hash(db_session, checkpoint):
    ex = FakeExecutor()
    gym = FakeGym({"cart": ["something-else"], "step": 7})
    with pytest.raises(checkpoints.DivergenceError):
        replay.restore_and_replay(checkpoint, _actions("btn-cart"), ex, gym, task_id="M40/x", seed=0)
    assert ex.performed == []


# --------------------------------------------------------------------------- the scheduled clock
class ClockGym:
    """Models the real harness: /_harness/verify sets the step counter, and
    /_harness/tick is the ONLY thing that delivers a due scheduled event
    (server/main.py harness_tick -> scheduler.advance_and_flush)."""

    def __init__(self):
        self.ticks: list[int] = []
        self.verifies: list[int] = []

    def tick(self, step=0):
        self.ticks.append(step)
        return {"now": step}

    def verify(self, step=0):
        self.verifies.append(step)
        return {"success": True}


def test_a_scheduled_task_ticks_as_well_as_verifies():
    """THE FIFTH instance of one bug shape. The backfill reconstructs the 18
    scheduled tasks WITH a tick, so a replay that cannot tick can never reproduce
    what it wrote — and GymEndpoint had no tick method at all."""
    gym = ClockGym()
    clock = replay.advance_clock(gym, scheduled=True)
    clock(0)
    clock(1)
    assert gym.ticks == [0, 1], "the async event only arrives if the clock is advanced"
    assert gym.verifies == [0, 1]


def test_a_task_with_nothing_scheduled_does_not_tick():
    """Unconditional ticking is a measured regression: advance_and_flush assigns
    sched.now before consulting the queue and `now` is inside the hashed world, so
    a tick with nothing to deliver corrupts every comparison (47/60 -> 5/60)."""
    gym = ClockGym()
    replay.advance_clock(gym, scheduled=False)(0)
    assert gym.ticks == [] and gym.verifies == [0]


def test_a_gym_without_a_tick_still_replays():
    """The shared gym client gained tick late; a workspace that predates it must
    degrade to verify-only rather than crash a finalize."""
    class OnlyVerify:
        def __init__(self): self.seen = []
        def verify(self, step=0): self.seen.append(step)

    gym = OnlyVerify()
    replay.advance_clock(gym, scheduled=True)(3)
    assert gym.seen == [3]


def test_a_replayed_step_says_it_succeeded():
    """The record only exists to be read, and it did not say the one thing its
    reader asks.

    `api/versions.py::certify` does `elif out.get("ok")` over these entries, and
    nothing ever wrote `ok`. So a step that replayed perfectly fell to the else
    branch and was marked `diverged` with "did not replay" — every step of every
    successful replay. Certify could never pass, and the hand-done attempts in
    the database looked stranded because of it.
    """
    class Ex:
        def act(self, kind, locator, args):
            return {"ok": True, "resolved": {"selector": "#x"}}

        def world(self):
            return {"step": 1}

    out = replay.replay([{"kind": "click", "locator": {"testId": "x"}}], Ex())
    assert out.ok and out.rejected_at is None
    assert len(out.steps) == 1
    assert out.steps[0]["ok"] is True, "certify reads exactly this key"


def test_certify_marks_a_clean_replay_verified_not_diverged():
    """The same bug from the reader's side: a replay with nothing wrong must not
    come back as a wall of diverged steps."""
    class Ex:
        def act(self, kind, locator, args):
            return {"ok": True, "resolved": {}}

        def world(self):
            return {"step": 1}

    out = replay.replay([{"kind": "click", "locator": {"testId": "a"}},
                         {"kind": "click", "locator": {"testId": "b"}}], Ex(), strict=False)
    # Exactly the branch certify takes for each step.
    states = ["verified" if s.get("ok") else "diverged" for s in out.steps]
    assert states == ["verified", "verified"], out.steps


def test_a_stale_world_read_is_asked_again_before_calling_it_divergence():
    """The race that reported a good trajectory as diverged.

    The mock UIs push to the engine asynchronously, so the world can be one push
    behind when the replay reads it — milliseconds after the action's ack. On a
    real M101 run the gift-message edit only reaches the engine on the checkout
    click, so a cold first replay certified 11/11 and every back-to-back replay
    after it failed at that exact step, on the same trajectory.
    """
    from app import checkpoints

    late = {"shop": {"cart": {"gift_message": "Congs"}}}
    want = checkpoints.hash_world(late)

    class SlowWorld:
        """Answers with the pre-push world once, then catches up."""
        def __init__(self):
            self.reads = 0

        def act(self, kind, locator, args):
            return {"ok": True, "resolved": {}}

        def world(self):
            self.reads += 1
            return {"shop": {"cart": {"gift_message": "Get well soon"}}} if self.reads == 1 else late

    ex = SlowWorld()
    out = replay.replay([{"kind": "click", "locator": {"testId": "checkout"}}], ex,
                        expected_hashes=[want], strict=False)
    assert out.ok, f"a read taken too early is not a divergence: {out.reason}"
    assert ex.reads >= 2, "it must actually have asked again"
    assert out.steps[0]["worldHash"] == want, "and record the settled hash, not the stale one"


def test_a_world_that_never_agrees_still_fails():
    """The guard must not turn the divergence gate off — a mismatch that survives
    being asked again is real, and shipping a trajectory that does not reproduce
    is the one thing this gate exists to stop."""
    from app import checkpoints

    class NeverAgrees:
        def act(self, kind, locator, args):
            return {"ok": True, "resolved": {}}

        def world(self):
            return {"shop": {"cart": {}}}

    out = replay.replay([{"kind": "click", "locator": {"testId": "x"}}], NeverAgrees(),
                        expected_hashes=[checkpoints.hash_world({"shop": {"orders": {"O1": {}}}})],
                        strict=False)
    assert not out.ok and out.rejected_at == 0
    assert "diverged" in out.reason


def test_a_diverged_step_is_marked_diverged_in_its_own_record():
    """Every step green beside a red gate reads as a gate bug.

    The record is appended as ok BEFORE the hash comparison runs, and certify
    reports from the records — so a run that failed at step 2 came back with
    `certified: 3`, three `verified` steps, and `firstFailureAt: 2`. The step
    that actually broke is the one thing the annotator needs pointed at.
    """
    class Walker:
        """Answers every action and walks a fixed sequence of worlds."""
        def __init__(self, worlds): self.worlds, self.i = worlds, -1
        def act(self, kind, locator, args): self.i += 1; return {"ok": True}
        def world(self): return self.worlds[min(self.i, len(self.worlds) - 1)]

    ex = Walker([{"n": 0}, {"n": 1}, {"n": 9}])
    want = [checkpoints.hash_world({"n": 0}), checkpoints.hash_world({"n": 1}),
            checkpoints.hash_world({"n": 2})]

    out = replay.replay([{"kind": "click"}] * 3, ex, expected_hashes=want, strict=False)

    assert out.ok is False and out.rejected_at == 2
    assert [s["ok"] for s in out.steps] == [True, True, False]
    assert out.steps[-1]["diverged"] is True


def test_a_human_recording_is_replayed_without_the_agents_clock():
    """`step` is inside the hashed world, so ticking is not free.

    An agent trajectory ticks: the harness calls /_harness/verify after every
    action, so its recorded worlds carry a rising step. A human working in the
    live gym never calls it, and every world they record sits at the step the
    session opened on. Replaying a human run with the agent's clock moves a
    counter the recording never moved — on M105 that was three of the four
    differing leaves (.step, .shop.step, .events[0].step) and it failed the
    trajectory at its last action for a world that was otherwise identical.
    """
    assert replay.recording_ticked([{"step": 0}, {"step": 0}, {"step": 0}]) is False
    assert replay.recording_ticked([{"step": 0}, {"step": 1}, {"step": 2}]) is True
    # No worlds to read, and worlds without a step at all: nothing says the
    # recording ticked, so do not.
    assert replay.recording_ticked([]) is False
    assert replay.recording_ticked([None, {"cart": []}]) is False


def test_restore_and_replay_can_be_told_not_to_tick():
    """The decision has to reach the clock, not just be computed."""
    ticks: list[int] = []

    class Gym:
        def verify(self, i): ticks.append(i)
        def world(self): return {"step": 0}

    class Walker:
        def act(self, kind, locator, args): return {"ok": True}
        def world(self): return {"step": 0}

    replay.restore_and_replay(None, [{"kind": "click"}], Walker(), Gym(),
                              task_id="T", seed=0, advance=False, strict=False)
    assert ticks == [], "a human recording must not be ticked"

    replay.restore_and_replay(None, [{"kind": "click"}], Walker(), Gym(),
                              task_id="T", seed=0, advance=True, strict=False)
    assert ticks == [0], "an agent recording still is"


def test_a_step_with_no_recorded_world_is_not_reported_as_compared():
    """The degenerate pass: 14 steps, 0 recorded worlds, certify says 14/14.

    A step the recording captured no world for has no expectation to diverge
    from, so it clears the gate by default. When that is true of EVERY step the
    replay is green while proving nothing at all — measured on a real M116 run,
    which then failed to solve the task. The gate's own docstring says a
    trajectory that mostly replays is worse than none; this is the degenerate
    case, so the outcome has to carry whether anything was actually checked.
    """
    class Walker:
        def act(self, kind, locator, args): return {"ok": True}
        def world(self): return {"n": 1}

    # Nothing recorded a world: every expectation is the empty string.
    out = replay.replay([{"kind": "click"}] * 3, Walker(),
                        expected_hashes=["", "", ""], strict=False)

    assert out.ok is True, "no expectation means nothing to diverge from"
    assert [s["compared"] for s in out.steps] == [False, False, False], (
        "a step checked against nothing must not claim it was compared"
    )


def test_a_step_that_was_checked_says_so():
    class Walker:
        def act(self, kind, locator, args): return {"ok": True}
        def world(self): return {"n": 1}

    want = checkpoints.hash_world({"n": 1})
    out = replay.replay([{"kind": "click"}] * 2, Walker(),
                        expected_hashes=[want, ""], strict=False)

    assert [s["compared"] for s in out.steps] == [True, False]
