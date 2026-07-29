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
