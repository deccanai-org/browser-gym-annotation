"""Replay-backfill — recover the world trail an archived run never recorded.

315 tasks have recorded runs and all 315 carry structured per-step actions, but
only 11 carry a per-step world, and in the database exactly ONE canonical run has
one. Fork-before-step-N restores from a per-step world, so today it works for one
task instead of 315. The worlds were never captured; no schema change brings them
back.

The gym is deterministic and every archived action is structured, so they can be
REPLAYED back at zero model cost. The protocol was derived by instrumenting the
real harness, and each line of it was learned by getting it wrong:

1. ``POST /_harness/reset {task_id, seed}``
2. open the browser at the run's OWN recorded ``initial_url`` — never the app
   root. An archived first click routinely targets an element that exists only on
   ``/mail`` or ``/cart``; starting at ``/`` cost one 13-step run every step.
3. per step: execute the action, then ``POST /_harness/verify {url, step}``. That
   call is what sets the world's step counter (``server/main.py``:
   ``s.step = req.step``), and the recorded world was hashed WITH it. Omitting it
   reconstructs the first step and nothing else. Then ``GET /_harness/world``.
4. accept the reconstruction only when it matches what the archive recorded.

Step 4 is the design, not a safety net. A backfill that guesses is worse than no
backfill: the wrong world is silently restored into a fork, and the trajectory
that comes out does not reproduce for whoever trains on it. So every step is
checked against the archive and SKIPPED when it disagrees, and coverage is
REPORTED rather than claimed — "40 of 66 steps" is the honest output, 66 written
blind is not.

Two strengths of evidence exist, because the archive is not uniform:

* ``WORLD`` — the step recorded a full ``world_after`` and the reconstruction
  hashes identically. 11 tasks can be checked this way.
* ``SNAPSHOT`` — the step recorded only the ``snapshot_after`` summary (the other
  304 tasks). Cart/order/return/subscription counts and the acting user are
  compared instead. That is a real check against recorded data, but a coarser
  one, so it is opt-in: the default accepts world evidence only.

Step 3 has a second half that only some tasks need — the deterministic clock. See
:func:`scheduled_events`: whether to tick is decided from the gym's OWN seed
world, per task, because it is a loss in both directions if guessed.
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import checkpoints, gym_client, gym_review, models
from app.config import settings

# The imported run belongs to a system identity distinct from the live gym
# oracle's: a replayed archive is not something this installation's gym produced,
# and an operator looking at provenance must be able to tell them apart.
BACKFILL_ANNOTATOR = "replay-backfill@system.local"

WORLD = "world"
SNAPSHOT = "snapshot"
NONE = ""

# Weakest last. Accepting at SNAPSHOT also accepts WORLD, never the reverse.
_RANK = {WORLD: 2, SNAPSHOT: 1, NONE: 0}

# How the deterministic clock is driven. AUTO is the only setting an operator
# should normally use; the other two exist so the decision can be MEASURED
# against a task rather than argued about. See `scheduled_events`.
TICK_AUTO = "auto"
TICK_ON = "on"
TICK_OFF = "off"
TICK_MODES = (TICK_AUTO, TICK_ON, TICK_OFF)

# The snapshot fields that describe STATE. `step` and `finished` are deliberately
# excluded: older captures serialized the snapshot by reference to a mutable
# GymState, so every step's `snapshot_after` reports the run's FINAL step —
# comparing it would reject every reconstruction of those runs for a difference
# that is an artefact of how they were written to disk.
_SNAPSHOT_FIELDS = (
    "current_user_id", "cart_item_count", "orders_count",
    "returns_count", "subscriptions_count", "applied_promo",
)

# What the live browser executor speaks (live_browser/service.py :: LiveSession.act).
EXECUTOR_KINDS = frozenset({
    "click", "fill", "select", "check", "submit", "navigate",
    "open_tab", "switch_tab", "close_tab", "scroll", "wait", "press", "type",
})

# The archive names four actions differently from the executor. A missed rename is
# silent — the executor rejects the unknown kind, the step is skipped, and a
# perfectly replayable run reports as unreplayable. `key_press` and `scroll_by`
# alone are 10,060 of the archive's recorded actions.
_KIND = {
    "key_press": "press",
    "scroll_by": "scroll",
    "click_mark": "click",
    "type_into_mark": "fill",
    "select_option": "select",
    "goto": "navigate",
    "open_url": "navigate",
}

# Kinds that address an element, and therefore cannot run without a locator.
_NEEDS_LOCATOR = frozenset({"click", "fill", "select", "check", "submit", "type"})


# --------------------------------------------------------------------------- archive
@dataclass(frozen=True)
class ArchivedRun:
    """One recorded episode, as the harness wrote it to ``trajectories/**``.

    The file extension says jsonl; the content is a single pretty-printed JSON
    object. Reading it line-wise yields nothing, which is worth knowing before
    concluding the archive is empty.
    """

    path: Path
    payload: dict

    @property
    def task_id(self) -> str:
        return str(self.payload.get("task_id") or "")

    @property
    def seed(self) -> int:
        return int(self.payload.get("seed") or 0)

    @property
    def agent(self) -> str:
        return str(self.payload.get("agent_name") or "archive")[:32]

    @property
    def episode_id(self) -> str:
        return str(self.payload.get("episode_id") or self.path.stem)

    @property
    def initial_url(self) -> str:
        return str(self.payload.get("initial_url") or "")

    @property
    def steps(self) -> list[dict]:
        return list(self.payload.get("steps") or [])


def load_run(path: str | Path) -> ArchivedRun:
    p = Path(path)
    return ArchivedRun(path=p, payload=json.loads(p.read_text()))


def task_of(path: Path) -> str:
    """The task id a trajectory filename encodes: ``M40_bogus_pricematch__0__ab``
    is ``M40/bogus_pricematch`` seed 0. Derived from the NAME so that indexing a
    4,683-file archive does not have to parse (and hold in memory) every world
    snapshot ever recorded."""
    slug = path.stem.split("__")[0]
    return slug.replace("_", "/", 1)


def index(root: str | Path, *, tasks: list[str] | None = None) -> dict[str, list[Path]]:
    """Every archived episode under ``root``, grouped by task id, newest last."""
    wanted = set(tasks or [])
    out: dict[str, list[Path]] = {}
    for p in sorted(Path(root).rglob("*.jsonl")):
        tid = task_of(p)
        if wanted and tid not in wanted:
            continue
        out.setdefault(tid, []).append(p)
    return out


def replayable(run: ArchivedRun) -> bool:
    return bool(run.steps) and all(to_action(s) is not None for s in run.steps)


def has_world_trail(run: ArchivedRun) -> bool:
    return any(s.get("world_after") for s in run.steps)


def start_url(run: ArchivedRun, gym_url: str) -> str:
    """The run's recorded start PATH, on the gym being replayed against.

    The path is load-bearing — an archived first click routinely targets an
    element that only exists on /mail — but the origin is not: it is whichever
    ephemeral port that capture's gym happened to listen on, and 122 archived
    episodes start on a :8011 that has not existed for months. Keeping it would
    open the browser on a dead host and lose the whole run to a connection error.
    """
    recorded = run.initial_url or "/"
    base = (gym_url or settings.gym_url).rstrip("/")
    if not recorded.startswith("http"):
        return base + ("" if recorded.startswith("/") else "/") + recorded
    rest = recorded.partition("://")[2]
    _, slash, path = rest.partition("/")
    return base + slash + path


# --------------------------------------------------------------------------- the clock
def scheduled_events(seed_world: dict | None) -> int:
    """How many async events this task has queued to fire on the deterministic
    clock, read off the gym's OWN seed world.

    This is the whole tick decision, and it has to be read from the live gym at
    reset rather than inferred from the archive, because ticking is a LOSS in
    both directions and each direction was measured:

    * **A task with a schedule, not ticked.** ``POST /_harness/tick`` is the only
      thing that calls ``scheduler.advance_and_flush`` (``server/main.py``
      ``harness_tick``), and that is the only thing that delivers a due event.
      Measured on the live gym: M15/inbox_price_watch reset, then six
      ``/_harness/verify`` calls and no tick — ``schedule.pending`` stays 2 and
      ``events`` stays empty. Its price-drop mail, due at step 4, never arrives,
      so every world from step 4 on is missing an email the recording has.
    * **A task with NO schedule, ticked anyway.** ``advance_and_flush`` assigns
      ``sched.now = step`` before it looks at the queue, so an empty queue still
      moves the clock — and ``ScheduleState.to_json`` puts ``now`` in the world,
      which ``checkpoints.hash_world`` hashes. Measured: A1/buy_wireless_mouse
      ticked six times changes exactly one thing, ``schedule.now`` 0 → 6, and
      that alone is enough to fail every comparison. This is what took M40 from
      3/3 worlds to 1/3 when the tick was applied unconditionally: only step 0
      survived, because ``advance_and_flush`` treats ``step <= now`` as a no-op.

    18 of the gym's 312 tasks answer >0 here.
    """
    queue = ((seed_world or {}).get("schedule") or {}).get("queue") or []
    return sum(1 for entry in queue if not entry.get("fired"))


def _tick_decision(mode: str, seed_world: dict | None) -> bool:
    """Resolve the tick mode, refusing anything that is not one of the three.

    This parameter used to be `tick: bool = False`. Falling through to AUTO on an
    unrecognised value means a caller still passing the old `False` — "do not
    tick" — silently GETS ticking, which is a measured regression on every task
    without a schedule (47/60 steps to 5/60). An explicit instruction that is
    quietly inverted is worse than an error.
    """
    if mode == TICK_ON:
        return True
    if mode == TICK_OFF:
        return False
    if mode != TICK_AUTO:
        raise ValueError(
            f"tick must be {TICK_AUTO!r}, {TICK_ON!r} or {TICK_OFF!r} — got {mode!r}"
        )
    return scheduled_events(seed_world) > 0


def pick(paths: list[Path]) -> ArchivedRun | None:
    """The best episode to replay for a task.

    Ordered: fully replayable, then carrying a recorded world trail, then longest.
    Replayability comes first because a mark- or pixel-addressed action in the
    middle loses every world after it — the executor cannot perform it, so the
    state stops tracking the recording. The world trail comes next because it is
    the only evidence strong enough to verify a reconstruction exactly; a run
    without one can be checked against counts alone. Length breaks the tie: more
    actions is more of the trail recovered.
    """
    loaded = []
    for p in paths:
        with contextlib.suppress(ValueError, OSError, json.JSONDecodeError):
            loaded.append(load_run(p))
    if not loaded:
        return None
    loaded.sort(key=lambda r: (replayable(r), has_world_trail(r), len(r.steps), r.path.name), reverse=True)
    return loaded[0]


# --------------------------------------------------------------------------- actions
def semantic_locator(args: dict) -> dict:
    """Turn an archived arg blob into the semantic locator the executor speaks.

    The archive stores a raw CSS selector (``[data-test-id='link-order-ORD-5290']``)
    or, for mark-addressed actions, a role + accessible name. Handing the selector
    through verbatim as ``css`` would work today and rot at the first markup
    change; ``testId``/``id`` are what the executor resolves most durably, so the
    common shapes are lifted out rather than passed along.
    """
    sel = str(args.get("selector") or "").strip()
    if sel:
        m = re.fullmatch(r"\[data-test-id=(['\"])(.+?)\1\]", sel)
        if m:
            return {"testId": m.group(2)}
        m = re.fullmatch(r"#([A-Za-z_][\w-]*)", sel)
        if m:
            return {"id": m.group(1)}
        m = re.fullmatch(r"\[name=(['\"])(.+?)\1\]", sel)
        if m:
            return {"name": m.group(2)}
        return {"css": sel}
    if args.get("role") and args.get("name"):
        return {"role": str(args["role"]), "name": str(args["name"])}
    return {}


def _args_for(kind: str, raw: dict) -> dict:
    if kind in ("navigate", "open_tab"):
        return {"url": str(raw.get("url") or "/")}
    if kind == "press":
        return {"key": str(raw.get("key") or "Enter")}
    if kind in ("switch_tab", "close_tab"):
        return {"tab_index": int(raw.get("tab_index", raw.get("index", 0)) or 0)}
    if kind == "scroll":
        return {"amount_px": raw.get("amount_px", 400), "direction": raw.get("direction", "down")}
    if kind in ("fill", "type", "select"):
        return {"value": raw.get("value", "")}
    return {}


def to_action(step: dict) -> dict | None:
    """The executor's ``(kind, locator, args)`` for one archived step, or None when
    the step cannot be replayed at all.

    ``click_xy``/``type_xy`` address pixels and ``click_mark``/``type_into_mark``
    address a Set-of-Marks overlay that existed only for the screenshot the agent
    was looking at. Neither can be re-resolved against today's DOM. The mark
    actions usually ALSO carry role + name, which is a genuine semantic locator,
    so those replay; a bare mark id or a coordinate does not, and is reported as
    unreplayable instead of guessed at.
    """
    raw = step.get("action_args") or {}
    kind = _KIND.get(str(step.get("action_kind") or ""), str(step.get("action_kind") or ""))
    if kind not in EXECUTOR_KINDS:
        return None
    locator = semantic_locator(raw)
    if kind in _NEEDS_LOCATOR and not locator:
        return None
    return {"kind": kind, "locator": locator, "args": _args_for(kind, raw)}


# --------------------------------------------------------------------------- seams
class Executor(Protocol):
    """Whatever performs one structured action in a real browser. The live
    browser service implements it; tests supply a fake."""

    def act(self, kind: str, locator: dict | None, args: dict | None) -> dict: ...


class Gym(Protocol):
    """The harness control plane the replay needs. Note ``verify`` takes the page
    URL — ``gym_client.verify`` does not, because the live harness reads it off
    the agent's own browser and a replay has no such browser."""

    def reset(self, task_id: str, seed: int) -> dict | None: ...
    def verify(self, url: str, step: int) -> dict | None: ...
    def world(self) -> dict | None: ...
    def snapshot(self) -> dict | None: ...
    def tick(self, step: int) -> dict | None: ...


class HarnessGym:
    """The running gym, reached over HTTP.

    Composes ``gym_client.GymEndpoint`` for everything it already does well, and
    posts the two calls it does not expose: a verify carrying the page URL, and
    the deterministic clock tick.
    """

    __slots__ = ("base_url", "token", "_gym")

    def __init__(self, base_url: str = "", token: str = "") -> None:
        self.base_url = (base_url or settings.gym_url).rstrip("/")
        self.token = token or settings.gym_harness_token
        self._gym = gym_client.GymEndpoint(self.base_url)

    def _post(self, path: str, body: dict, timeout: int = 30) -> dict | None:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"content-type": "application/json", "X-Harness-Token": self.token},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
            return None

    def reset(self, task_id: str, seed: int) -> dict | None:
        return self._gym.reset(task_id, seed)

    def world(self) -> dict | None:
        return self._gym.world()

    def snapshot(self) -> dict | None:
        return self._gym.snapshot()

    def verify(self, url: str, step: int) -> dict | None:
        return self._post("/_harness/verify", {"url": url or "/", "step": step})

    def tick(self, step: int) -> dict | None:
        return self._post("/_harness/tick", {"step": step})


@contextlib.contextmanager
def live_executor(initial_url: str, *, base_url: str = "", owner: str = BACKFILL_ANNOTATOR) -> Iterator[Any]:
    """A live browser session opened at the run's OWN recorded start URL.

    Opening at the app root instead is the single most expensive mistake in this
    protocol: a 13-step run whose first click targets an element that only exists
    on ``/mail`` loses all thirteen steps, and the cascade looks like locator rot
    rather than a wrong starting page.
    """
    base = (base_url or settings.live_browser_url).rstrip("/")
    req = urllib.request.Request(
        base + "/live/sessions",
        data=json.dumps({"url": initial_url or "/", "owner": owner}).encode(),
        method="POST",
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        opened = json.loads(r.read())
    sid = opened["session_id"]
    try:
        yield gym_client.LiveBrowserClient(base, sid, opened.get("ticket", ""))
    finally:
        # A leaked session holds a real Chromium; the next task would open another.
        with contextlib.suppress(Exception):
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{base}/live/sessions/{sid}/close", data=b"{}", method="POST",
                    headers={"content-type": "application/json"},
                ),
                timeout=30,
            )


# --------------------------------------------------------------------------- replay
@dataclass
class StepOutcome:
    idx: int
    kind: str
    executed: bool = False
    error: str = ""
    evidence: str = NONE
    accepted: bool = False
    world: dict | None = None
    snapshot: dict | None = None
    url: str = ""
    locator: dict = field(default_factory=dict)
    resolved: dict = field(default_factory=dict)


@dataclass
class Reconstruction:
    run: ArchivedRun
    seed_world: dict | None = None
    steps: list[StepOutcome] = field(default_factory=list)
    # Set when NOTHING from this run may be written — a refusal is about the whole
    # episode (wrong task revision, dead gym), not one step.
    refused: str = ""
    # What the clock did, and why. Reported rather than assumed: a run replayed
    # with the wrong answer here diverges from its first scheduled step onward,
    # and the audit has to be able to say which of the two it chose.
    scheduled: int = 0
    ticked: bool = False

    @property
    def executed(self) -> int:
        return sum(1 for s in self.steps if s.executed)

    @property
    def accepted(self) -> int:
        return sum(1 for s in self.steps if s.accepted)

    def by_evidence(self, level: str) -> int:
        return sum(1 for s in self.steps if s.evidence == level)


def _evidence(step: dict, world: dict | None, snapshot: dict | None) -> str:
    """How well the reconstruction agrees with what was recorded, at the strongest
    level the archive can support. A recorded world that disagrees is a refusal for
    that step — it never falls back to the weaker summary, or a step whose world is
    provably wrong would be accepted on a count of three orders."""
    recorded = step.get("world_after")
    if recorded:
        return WORLD if checkpoints.hash_world(world) == checkpoints.hash_world(recorded) else NONE
    summary = step.get("snapshot_after") or {}
    fields = [f for f in _SNAPSHOT_FIELDS if f in summary]
    if not fields or not snapshot:
        return NONE
    return SNAPSHOT if all(snapshot.get(f) == summary[f] for f in fields) else NONE


def reconstruct(
    run: ArchivedRun,
    gym: Gym,
    executor: Executor,
    *,
    accept: str = WORLD,
    tick: str = TICK_AUTO,
    expected_seed_world: dict | None = None,
) -> Reconstruction:
    """Replay one archived episode and report, per step, whether the world it
    produced is the world that was recorded.

    Execution failures do NOT stop the replay. A later step can still land on a
    verified world, because acceptance is absolute — a hash equal to the recorded
    one is correct regardless of how the replay got there — and on the measured
    runs three tasks recovered more worlds than they executed actions.

    `tick` drives the gym's deterministic clock. AUTO — the default, and the only
    setting with a defensible answer for an arbitrary task — asks the gym's own
    seed world whether this task schedules anything (:func:`scheduled_events`) and
    ticks only then. ON and OFF force it, for measuring a task rather than
    trusting the rule.

    The tick goes BEFORE the action and carries the index of the step about to
    run, matching ``harness/runner.py``'s ``tick()``: it fires at the start of the
    turn with ``step = len(trajectory.steps)``, i.e. the count of steps ALREADY
    recorded. Ticking after the action instead would deliver a step-4 email into
    the world that step 4 was recorded to have produced without it.
    """
    out = Reconstruction(run=run)
    if not run.steps:
        out.refused = "the archived run has no steps"
        return out
    if gym.reset(run.task_id, run.seed) is None:
        out.refused = "the gym could not reset this task"
        return out

    out.seed_world = gym.world()
    if expected_seed_world and checkpoints.hash_world(out.seed_world) != checkpoints.hash_world(expected_seed_world):
        # A different task revision seeds a different world. Every world this
        # replay went on to produce would be a plausible-looking lie, and the
        # per-step check cannot catch it — the whole chain is built on the wrong
        # starting state.
        out.refused = "the gym's seed world is not the one this task was captured against"
        return out

    out.scheduled = scheduled_events(out.seed_world)
    out.ticked = _tick_decision(tick, out.seed_world)

    for i, st in enumerate(run.steps):
        outcome = StepOutcome(idx=i, kind=str(st.get("action_kind") or ""))
        action = to_action(st)
        if action is None:
            outcome.error = "not replayable: addressed by mark id or coordinates, not by locator"
            out.steps.append(outcome)
            continue
        outcome.locator = action["locator"]
        if out.ticked:
            gym.tick(i)
        res = executor.act(action["kind"], action["locator"], action["args"]) or {}
        outcome.executed = bool(res.get("ok"))
        outcome.resolved = res.get("resolved") or {}
        if not outcome.executed:
            outcome.error = str(res.get("error") or "the action did not land")
        outcome.url = str(outcome.resolved.get("url") or st.get("url_after") or "")
        # Not a verdict — this is what advances the world's step counter, and the
        # recorded world was hashed with it. Skipping it reconstructs step 0 and
        # then diverges on every step after.
        gym.verify(outcome.url, i)
        outcome.world = gym.world()
        outcome.snapshot = gym.snapshot()
        outcome.evidence = _evidence(st, outcome.world, outcome.snapshot)
        # An exact world hash is proof on its own — however the replay got there,
        # the state IS the recorded state. A counts summary is not: measured on the
        # oracle archive, A4/home_office_bundle executed 14 of its 26 actions and
        # still agreed on all 26 snapshots, because cart and order counts simply
        # did not move. So the weaker evidence has to be backed by an action that
        # actually landed, or the backfill would certify twelve steps that never
        # happened.
        strong = outcome.evidence == WORLD
        outcome.accepted = _RANK[outcome.evidence] >= _RANK[accept] > 0 and (strong or outcome.executed)
        out.steps.append(outcome)
    return out


# --------------------------------------------------------------------------- persist
@dataclass
class WriteReport:
    task_id: str
    trajectory_id: str = ""
    imported: bool = False
    checkpoints_written: int = 0
    checkpoints_reused: int = 0
    steps_updated: int = 0
    conflicts: int = 0
    refused: str = ""


def _matching_trajectory(db: Session, task: models.Task, run: ArchivedRun) -> models.Trajectory | None:
    """The persisted run that IS this archived episode, identified by its action
    sequence.

    Matching on the arguments rather than on timestamps or agent name is what
    makes re-running the backfill idempotent: an imported trajectory persists the
    same ``action_args`` it was imported from, so the second pass finds it instead
    of importing a duplicate that would then compete to be canonical.
    """
    wanted = [s.get("action_args") or {} for s in run.steps]
    rows = db.scalars(
        select(models.Trajectory)
        .join(models.ReviewSession, models.Trajectory.session_id == models.ReviewSession.id)
        .where(models.ReviewSession.task_id == task.id, models.Trajectory.source == "gym")
        .order_by(models.Trajectory.created_at)
    ).all()
    for traj in rows:
        steps = db.scalars(
            select(models.TrajectoryStep)
            .where(models.TrajectoryStep.trajectory_id == traj.id)
            .order_by(models.TrajectoryStep.idx)
        ).all()
        if len(steps) == len(wanted) and [s.arguments or {} for s in steps] == wanted:
            return traj
    return None


def _payload_with_worlds(run: ArchivedRun, recon: Reconstruction) -> dict:
    """A copy of the archived trajectory with the VERIFIED worlds filled in.

    ``gym_review.to_review`` reads ``gymResume.worldTrail`` straight off these
    steps, and that trail is what the review UI resumes a correction from. Leaving
    it empty is why the client silently falls back to the run's FINAL world, which
    already contains every later step's effects.
    """
    payload = copy.deepcopy(run.payload)
    for outcome in recon.steps:
        if outcome.accepted and outcome.idx < len(payload.get("steps", [])):
            payload["steps"][outcome.idx]["world_after"] = outcome.world
    return payload


def _system_annotator(db: Session) -> models.Annotator:
    ann = db.scalar(select(models.Annotator).where(models.Annotator.email == BACKFILL_ANNOTATOR))
    if ann is None:
        ann = models.Annotator(email=BACKFILL_ANNOTATOR, display_name="Replay backfill")
        db.add(ann)
        db.flush()
    return ann


def _import(db: Session, task: models.Task, run: ArchivedRun, recon: Reconstruction) -> models.Trajectory:
    """Persist an archived episode as a full FK-linked record — session →
    trajectory (+ steps) + verifier suite (+ milestones) + benchmark run — the
    same shape ``app/api/gym.py::_persist_gym_review`` writes for a live run, so a
    backfilled task opens in the review UI exactly like a freshly captured one."""
    review = gym_review.to_review(
        {"trajectory": _payload_with_worlds(run, recon), "seed": run.seed}, run.task_id, run.agent
    )
    review["backfill"] = {
        "episodeId": run.episode_id,
        "source": str(run.path),
        "steps": len(run.steps),
        "worldsReconstructed": recon.accepted,
    }
    verdict = run.payload.get("verifier_result") or {}

    task.source = "gym"
    task.title = task.title or review["task"]["title"]
    task.prompt = task.prompt or review["task"]["prompt"]
    task.category = str(run.payload.get("task_category") or task.category)
    task.difficulty = str(run.payload.get("task_difficulty") or task.difficulty).lower()
    task.priority = review["task"]["priority"]
    task.seed = run.seed
    task.start_url = task.start_url or run.initial_url
    prior = dict(task.seed_state or {})
    prior.setdefault("seed", run.seed)
    prior.setdefault("initial_url", run.initial_url)
    # The seed world is read straight off the gym's own reset, so it is an
    # observation rather than a reconstruction — but only worth recording once
    # the replay showed the reset landed where the recording started.
    if recon.seed_world and recon.accepted and not prior.get("world"):
        prior["world"] = recon.seed_world
    task.seed_state = prior
    db.flush()

    session = models.ReviewSession(
        task_id=task.id, annotator_id=_system_annotator(db).id, source="gym",
        seed=run.seed, agent=run.agent, status="benchmark_run",
    )
    db.add(session)
    db.flush()

    traj = models.Trajectory(
        session_id=session.id, agent=run.agent, seed=run.seed,
        score=float(verdict.get("score", 0.0) or 0.0), success=bool(verdict.get("success")),
        source="gym", raw=review,
    )
    db.add(traj)
    db.flush()
    for i, st in enumerate(run.steps):
        view = review["steps"][i]
        db.add(models.TrajectoryStep(
            trajectory_id=traj.id, idx=view["idx"], action_type=view["type"],
            description=view["description"], tab_id=view.get("tabId", ""),
            screenshot_url=view.get("image") or "",
            url_after=view.get("url") or "",
            reasoning=(st.get("reasoning") or "").strip(),
            actor="agent",
            arguments=st.get("action_args") or {},
        ))

    suite = models.VerifierSuite(session_id=session.id, version=1)
    db.add(suite)
    db.flush()
    for v in review["verifiers"]:
        db.add(models.Verifier(
            suite_id=suite.id, ext_id=v["id"], level=v["level"],
            assertion=v["assertion"], code=v["code"], gym_result=v.get("gymResult", ""),
        ))
    db.add(models.BenchmarkRun(
        suite_id=suite.id, reward=review.get("gymReward", 0),
        results={v["id"]: v.get("gymResult") for v in review["verifiers"]},
    ))
    db.flush()
    return traj


def _fill_trail(traj: models.Trajectory, recon: Reconstruction) -> None:
    """Fill the payload's ``worldTrail`` holes with the verified worlds, and only
    the holes. Overwriting an entry that already carries a world would rewrite the
    run somebody may be mid-review on; the backfill's job is the missing data."""
    raw = copy.deepcopy(traj.raw) if traj.raw else None
    if not raw:
        return
    trail = list((raw.get("gymResume") or {}).get("worldTrail") or [])
    if not trail:
        trail = [None] * len(recon.steps)
    changed = False
    for outcome in recon.steps:
        if outcome.accepted and outcome.idx < len(trail) and not trail[outcome.idx]:
            trail[outcome.idx] = outcome.world
            changed = True
    if changed:
        raw.setdefault("gymResume", {})["worldTrail"] = trail
        # A JSON column tracks identity, not nested mutation — assigning the whole
        # payload back is what makes the change reach the database at all.
        traj.raw = raw


def apply(db: Session, recon: Reconstruction) -> WriteReport:
    """Persist ONLY the verified steps of a reconstruction. Idempotent.

    Checkpoint chaining follows ``_persist_gym_review``: each step's ``before`` is
    the previous step's ``after``, and step 0's is the seed world. It differs in
    one deliberate place — an unverified step BREAKS the chain instead of carrying
    the last known checkpoint forward. Carrying it forward would give step N+1 a
    "before" that is really the state before step N-1, so forking there would
    restore a world two actions stale and call it a clean start. A gap is
    recoverable; a confident wrong answer is not.
    """
    report = WriteReport(task_id=recon.run.task_id)
    if recon.refused:
        report.refused = recon.refused
        return report
    if not recon.accepted:
        report.refused = "no step reconstructed to a world that matches the recording"
        return report

    task = db.scalar(select(models.Task).where(models.Task.external_id == recon.run.task_id))
    if task is None:
        task = models.Task(external_id=recon.run.task_id, source="gym", title="", prompt="")
        db.add(task)
        db.flush()

    traj = _matching_trajectory(db, task, recon.run)
    if traj is None:
        traj = _import(db, task, recon.run, recon)
        report.imported = True
    else:
        _fill_trail(traj, recon)
    report.trajectory_id = str(traj.id)

    steps = db.scalars(
        select(models.TrajectoryStep)
        .where(models.TrajectoryStep.trajectory_id == traj.id)
        .order_by(models.TrajectoryStep.idx)
    ).all()
    attempt_id = traj.session_id

    prev_cp = _seed_checkpoint(db, steps, recon, attempt_id, report)
    for outcome in recon.steps:
        if outcome.idx >= len(steps):
            break
        step = steps[outcome.idx]
        # The state BEFORE this step is the state AFTER the previous one, and that
        # was verified on its own terms — so a step whose OWN world could not be
        # reproduced still gets a restore point. That is the fork that matters most:
        # the step an annotator rejects is precisely the one they branch before.
        touched = step.before_checkpoint_id is None and prev_cp is not None
        if touched:
            step.before_checkpoint_id = prev_cp.id
        after = _after_checkpoint(db, step, outcome, attempt_id, report) if outcome.accepted else None
        if after is not None:
            if step.world_after is None:
                step.world_after = outcome.world
            if not step.semantic_locator:
                step.semantic_locator = outcome.locator
            if not step.resolved_target and outcome.resolved:
                step.resolved_target = outcome.resolved
            touched = True
        prev_cp = after
        report.steps_updated += int(touched)

    db.add(models.AuditLog(
        session_id=attempt_id, actor=BACKFILL_ANNOTATOR, action="backfill.replay",
        target=recon.run.task_id,
        meta={
            "episodeId": recon.run.episode_id, "source": str(recon.run.path),
            "steps": len(recon.steps), "executed": recon.executed, "accepted": recon.accepted,
            "worldEvidence": recon.by_evidence(WORLD), "snapshotEvidence": recon.by_evidence(SNAPSHOT),
            "imported": report.imported, "conflicts": report.conflicts,
            # Provenance: a world reconstructed with the clock running is a
            # different artefact from one reconstructed without it, and an
            # operator auditing a suspect trail must be able to tell which.
            "scheduledEvents": recon.scheduled, "ticked": recon.ticked,
        },
    ))
    db.flush()
    return report


def _seed_checkpoint(
    db: Session, steps: list[models.TrajectoryStep], recon: Reconstruction,
    attempt_id: Any, report: WriteReport,
) -> models.EnvironmentCheckpoint | None:
    """Step 0's "before" — the seed world, so a fork before the FIRST action still
    has a state to restore from."""
    if not steps or not recon.seed_world:
        return None
    existing = db.get(models.EnvironmentCheckpoint, steps[0].before_checkpoint_id) if steps[0].before_checkpoint_id else None
    if existing is not None:
        if existing.world_hash and existing.world_hash != checkpoints.hash_world(recon.seed_world):
            report.conflicts += 1
            return None
        report.checkpoints_reused += 1
        return existing
    report.checkpoints_written += 1
    return checkpoints.capture(
        db, attempt_id=attempt_id, world=recon.seed_world, step_clock=0,
        browser={"url": recon.run.initial_url},
    )


def _after_checkpoint(
    db: Session, step: models.TrajectoryStep, outcome: StepOutcome,
    attempt_id: Any, report: WriteReport,
) -> models.EnvironmentCheckpoint | None:
    """This step's verified "after". Returns None when an already-persisted
    checkpoint disagrees with the reconstruction — a disagreement is evidence the
    environment moved, and overwriting it would destroy the older, first-hand
    record in favour of a replayed one."""
    if step.after_checkpoint_id is not None:
        existing = db.get(models.EnvironmentCheckpoint, step.after_checkpoint_id)
        if existing is not None:
            if existing.world_hash and existing.world_hash != checkpoints.hash_world(outcome.world):
                report.conflicts += 1
                return None
            report.checkpoints_reused += 1
            return existing
    cp = checkpoints.capture(
        db, attempt_id=attempt_id, world=outcome.world,
        backend_state=outcome.snapshot or {}, step_clock=outcome.idx + 1,
        browser={"url": outcome.url},
    )
    step.after_checkpoint_id = cp.id
    report.checkpoints_written += 1
    return cp


# --------------------------------------------------------------------------- audit
def audit_row(recon: Reconstruction) -> dict:
    return {
        "taskId": recon.run.task_id,
        "episodeId": recon.run.episode_id,
        "agent": recon.run.agent,
        "source": str(recon.run.path),
        "steps": len(recon.steps),
        "executed": recon.executed,
        "accepted": recon.accepted,
        "worldEvidence": recon.by_evidence(WORLD),
        "snapshotEvidence": recon.by_evidence(SNAPSHOT),
        "unreplayable": sum(1 for s in recon.steps if not s.executed and "not replayable" in s.error),
        "refused": recon.refused,
        # Reported per task, not per batch: the clock is decided from each task's
        # own seed world, so a batch-level "ticking was on" would be a lie about
        # every task in it that has no schedule.
        "scheduledEvents": recon.scheduled,
        "ticked": recon.ticked,
    }


def summarize(rows: list[dict]) -> dict:
    """Coverage as measured, not as hoped. §8.7 — this counts only what was
    actually replayed in this pass; it is not a statement about the archive."""
    steps = sum(r["steps"] for r in rows)
    accepted = sum(r["accepted"] for r in rows)
    return {
        "tasks": len(rows),
        "refused": sum(1 for r in rows if r["refused"]),
        "scheduledTasks": sum(1 for r in rows if r.get("scheduledEvents")),
        "ticked": sum(1 for r in rows if r.get("ticked")),
        "steps": steps,
        "executed": sum(r["executed"] for r in rows),
        "accepted": accepted,
        "worldEvidence": sum(r["worldEvidence"] for r in rows),
        "snapshotEvidence": sum(r["snapshotEvidence"] for r in rows),
        "tasksFullyCovered": sum(1 for r in rows if r["steps"] and r["accepted"] == r["steps"]),
        "tasksPartlyCovered": sum(1 for r in rows if 0 < r["accepted"] < r["steps"]),
        "tasksUncovered": sum(1 for r in rows if not r["accepted"]),
        "coverage": round(accepted / steps, 4) if steps else 0.0,
    }
