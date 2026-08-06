"""Core platform schema (Postgres).

A normalized, foreign-keyed relational model for the annotation platform. The
gym owns the live world state; this DB owns the operational record — tasks and
their seed state, per-annotator review sessions, the recorded trajectories +
steps, the verifier suites and their runs, submissions, and an audit log.

Relationships (all FK-backed, cascade where a child cannot outlive its parent):

    annotator ─┐
               ├─< review_session ─┬─< verifier_suite ─┬─< verifier
    task ──────┘        │          │                   └─< benchmark_run
                        │          ├─< trajectory ──────< trajectory_step
                        │          ├─< trajectory_branch (self-referential)
                        │          └─< submission
                        └─< audit_log (nullable)

Enums are short strings for now; tighten to native enums with the first Alembic
migration (create_all bootstraps the schema in dev).
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _pk() -> Mapped[UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid4)


def _fk(target: str, *, nullable: bool = False, ondelete: str = "CASCADE") -> Mapped[UUID]:
    return mapped_column(ForeignKey(target, ondelete=ondelete), nullable=nullable, index=True)


class Annotator(Base):
    __tablename__ = "annotator"
    id: Mapped[UUID] = _pk()
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(32), default="annotator")  # annotator | reviewer | admin
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)  # pbkdf2 (minimal auth); null = no login yet
    # Profile — display_name/avatar_hue drive the header identity + QA rows; the
    # avatar is a colored circle with initials (no upload needed).
    display_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    avatar_hue: Mapped[int] = mapped_column(Integer, default=210)  # 0-359, for the avatar color
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # deactivate instead of delete
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    # foreign_keys is required now that review_session has a SECOND fk to annotator
    # (disposition_by_id) — otherwise the join condition is ambiguous.
    sessions: Mapped[list["ReviewSession"]] = relationship(
        back_populates="annotator", foreign_keys="ReviewSession.annotator_id"
    )


class Task(Base):
    """A reviewable task + its initial seed state. `source` distinguishes the
    hand-authored fixtures from the 312 real gym tasks; `seed_state` holds the
    initial world snapshot so every task starts from a known, reproducible state."""

    __tablename__ = "task"
    id: Mapped[UUID] = _pk()
    external_id: Mapped[str] = mapped_column(String(96), unique=True, index=True)  # "GYM-2041" | "A1/buy_wireless_mouse"
    source: Mapped[str] = mapped_column(String(16), default="fixture", index=True)  # fixture | gym
    title: Mapped[str] = mapped_column(Text)
    prompt: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(96), default="")
    difficulty: Mapped[str] = mapped_column(String(16), default="")  # easy | medium | hard
    priority: Mapped[str] = mapped_column(String(16), default="Medium")
    seed: Mapped[int] = mapped_column(Integer, default=0)
    start_url: Mapped[str] = mapped_column(Text, default="")
    seed_state: Mapped[dict] = mapped_column(JSON, default=dict)  # initial world snapshot
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # constraints, allowed sites, run summary
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    sessions: Mapped[list["ReviewSession"]] = relationship(back_populates="task")


class ReviewSession(Base):
    __tablename__ = "review_session"
    id: Mapped[UUID] = _pk()
    task_id: Mapped[UUID] = _fk("task.id", ondelete="RESTRICT")
    annotator_id: Mapped[UUID | None] = _fk("annotator.id", nullable=True, ondelete="SET NULL")
    source: Mapped[str] = mapped_column(String(16), default="fixture")  # fixture | gym
    seed: Mapped[int] = mapped_column(Integer, default=0)
    agent: Mapped[str] = mapped_column(String(32), default="")  # the agent whose run is under review
    # draft | steps_approved | verifiers_generated | benchmark_run | submitted
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    # DEPRECATED by the v2 model: rerun_from is superseded by
    # TrajectoryVersion.fork_before_step_id, and reviewed_through by StepVerdict
    # (a scalar cannot survive a re-fork, which is why the client had to clamp it).
    # Kept nullable/defaulted so the existing flows keep working during migration.
    rerun_from: Mapped[int | None] = mapped_column(nullable=True)
    reviewed_through: Mapped[int] = mapped_column(Integer, default=0)  # granular per-step review progress
    # --- v2: this row IS the AnnotationAttempt (one workflow record, not two) ---
    task_revision: Mapped[int] = mapped_column(Integer, default=1)  # which revision of the task was annotated
    # The attempt HEAD. Advanced only by an explicit, versioned selection command —
    # never automatically by a finishing agent job (that is what makes out-of-order
    # completions safe). use_alter breaks the review_session <-> trajectory_version cycle.
    active_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("trajectory_version.id", ondelete="SET NULL", use_alter=True, name="fk_attempt_active_version"),
        nullable=True, index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, default=0)  # optimistic lock; every head change is a CAS
    agent_call_count: Mapped[int] = mapped_column(Integer, default=0)  # rerun cap lives on the ATTEMPT so sibling branches can't bypass it
    # Disposition is a workflow, not a boolean — it is what separates "the model
    # failed" from "the environment is broken", which is the 85-task report's blocker.
    disposition: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    disposition_note: Mapped[str] = mapped_column(Text, default="")
    disposition_by_id: Mapped[UUID | None] = _fk("annotator.id", nullable=True, ondelete="SET NULL")
    disposition_at: Mapped[datetime | None] = mapped_column(nullable=True)
    rework_status: Mapped[str] = mapped_column(String(16), default="")  # "" | requested | done
    # WHY it was sent back, in the reviewer's own words. Without this the board
    # can say "Returned" and nothing else, which tells the annotator to redo the
    # task without telling them what was wrong — the audit row holds the note but
    # it is not theirs to read.
    rework_note: Mapped[str] = mapped_column(Text, default="")
    # For a SYSTEM gym run produced by an annotator's correction (drive-forward),
    # the HUMAN session that triggered it. Lets a corrected re-benchmark score from
    # THAT annotator's own correction, never another annotator's — the isolation
    # fix for the task-global verdict leak. Null for canonical/prompt-edit runs.
    origin_session_id: Mapped[UUID | None] = mapped_column(ForeignKey("review_session.id", ondelete="SET NULL"), nullable=True, index=True)
    # --- the realistic-UI (cua-hub) attempt -------------------------------------
    # Which world this attempt owns. These were process-memory only (`_ATTACHED`
    # in api/live.py), so a backend restart orphaned the annotator's world with no
    # way to reopen it — and `world_for()` could not tell a bridged attempt from a
    # workspace one, which is how bridged attempts ended up on the SHARED gym.
    mode: Mapped[str] = mapped_column(String(16), default="agent_review")  # agent_review | human_do
    prompt_override: Mapped[str] = mapped_column(Text, default="")
    bridge_session_id: Mapped[str] = mapped_column(String(64), default="")   # == the attempt id, keyed by the bridge
    bridge_gym_url: Mapped[str] = mapped_column(String(255), default="")     # the gym the bridge leased us
    cua_apps: Mapped[dict | list] = mapped_column(JSON, default=list)        # [{app, mock_key, attempt_sid, url, ...}]
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # The two worlds Feature 2 scores against: the seeded start, and what the
    # annotator's own work produced.
    # use_alter: environment_checkpoint.attempt_id already points BACK at
    # review_session, so these close a cycle. Without it create_all cannot order
    # the two tables and raises CircularDependencyError at dev startup.
    initial_checkpoint_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("environment_checkpoint.id", ondelete="SET NULL", use_alter=True,
                   name="fk_review_session_initial_checkpoint"), nullable=True)
    final_checkpoint_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("environment_checkpoint.id", ondelete="SET NULL", use_alter=True,
                   name="fk_review_session_final_checkpoint"), nullable=True)
    # What this whole attempt changed, initial → final (`world-delta/1`). The
    # per-attempt DB diff a verifier validates against, computed from the two
    # checkpoints above so it always describes the same worlds they name.
    world_summary: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime] = mapped_column(default=func.now(), onupdate=func.now())

    task: Mapped[Task] = relationship(back_populates="sessions")
    annotator: Mapped[Annotator | None] = relationship(back_populates="sessions", foreign_keys=[annotator_id])
    suites: Mapped[list["VerifierSuite"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    trajectories: Mapped[list["Trajectory"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    branches: Mapped[list["TrajectoryBranch"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    submissions: Mapped[list["Submission"]] = relationship(back_populates="session", cascade="all, delete-orphan")


class Trajectory(Base):
    """A recorded agent run under review (the fixture trace, or a real gym run)."""

    __tablename__ = "trajectory"
    id: Mapped[UUID] = _pk()
    session_id: Mapped[UUID] = _fk("review_session.id")
    agent: Mapped[str] = mapped_column(String(32), default="")
    seed: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(16), default="fixture")  # fixture | gym
    # The exact review payload this run produced (task + steps + verifiers + tabs +
    # backendState + gymResume world). Persisted for gym runs so REOPENING a gym
    # task replays the SAME run — instead of re-driving a fresh, stochastic agent
    # each time (which would leave a saved correction fork restoring onto a
    # different trajectory). Null for fixture trajectories AND for drive-forward
    # continuations (which must not shadow the original run). none_as_null=True is
    # REQUIRED: a plain JSON column stores Python None as the JSON value `null`
    # (not SQL NULL), so `raw IS NOT NULL` would wrongly match a payload-less row.
    raw: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    session: Mapped[ReviewSession] = relationship(back_populates="trajectories")
    steps: Mapped[list["TrajectoryStep"]] = relationship(back_populates="trajectory", cascade="all, delete-orphan", order_by="TrajectoryStep.idx")


class TrajectoryStep(Base):
    __tablename__ = "trajectory_step"
    id: Mapped[UUID] = _pk()
    trajectory_id: Mapped[UUID] = _fk("trajectory.id")
    idx: Mapped[int] = mapped_column(Integer)
    action_type: Mapped[str] = mapped_column(String(16), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    tab_id: Mapped[str] = mapped_column(String(32), default="")
    screenshot_url: Mapped[str] = mapped_column(Text, default="")
    reasoning: Mapped[str] = mapped_column(Text, default="")
    url_after: Mapped[str] = mapped_column(Text, default="")
    # --- v2: the COMMITTED, replayable step contract -------------------------
    # `idx` above stays for legacy/display only. Identity is this row's UUID; a
    # step belongs to the VERSION that created it and carries a LOCAL suffix
    # ordinal — the global display number is computed when flattening the parent
    # chain, never persisted, because position changes between branches.
    version_id: Mapped[UUID | None] = _fk("trajectory_version.id", nullable=True, ondelete="CASCADE")
    suffix_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor: Mapped[str] = mapped_column(String(8), default="agent")  # agent | human | system (a trajectory is routinely HYBRID)
    # Structured action IR — replayable without an LLM.
    semantic_locator: Mapped[dict] = mapped_column(JSON, default=dict)   # role/name/test-id/css candidates
    resolved_target: Mapped[dict] = mapped_column(JSON, default=dict)    # what actually matched at dispatch
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    coordinate_fallback: Mapped[dict] = mapped_column(JSON, default=dict)  # {x,y} only as a last resort
    before_checkpoint_id: Mapped[UUID | None] = _fk("environment_checkpoint.id", nullable=True, ondelete="SET NULL")
    after_checkpoint_id: Mapped[UUID | None] = _fk("environment_checkpoint.id", nullable=True, ondelete="SET NULL")
    # Per-step world (previously only inside Trajectory.raw; branch steps had none).
    world_after: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    # What this step CHANGED, semantically — `world-delta/1` (see app/worlddiff.py).
    # `world_after` is the whole world at one observation; this is the difference
    # from the previous one, which is the thing an SFT target actually learns to
    # produce ("submitting an order"), and the thing a DB-diff verifier reads.
    #
    # NULL means NOT OBSERVED, never "nothing changed" — the gym world can only be
    # read once per fold batch, so a step sharing a batch with a later one has no
    # delta of its own. `delta_span` names the window it belongs to, so the record
    # never attributes a change to whichever action happened to be last.
    # none_as_null=True for the same reason Trajectory.raw needs it: a plain JSON
    # column stores None as the JSON value `null`, so `IS NOT NULL` would match.
    world_delta: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    delta_span: Mapped[dict] = mapped_column(JSON, default=dict)  # {"stepIds":[…],"observed":"step"|"window"}
    marks_artifact_id: Mapped[UUID | None] = _fk("artifact.id", nullable=True, ondelete="SET NULL")
    # Provenance of intent — never synthesized after the fact.
    human_intent: Mapped[str] = mapped_column(Text, default="")      # the annotator's own "why"
    guidance_text: Mapped[str] = mapped_column(Text, default="")     # reviewer instruction that produced this step
    guidance_author_id: Mapped[UUID | None] = _fk("annotator.id", nullable=True, ondelete="SET NULL")
    intervention_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Whether this step has been PROVEN to replay. Recording a step and proving it
    # are separate concerns: proving restores a checkpoint, which destroys the
    # annotator's working state, so it runs in a SCRATCH world and marks steps
    # rather than deleting them — one bad action no longer discards the sequence.
    # unverified | verified | diverged | failed | needs_value
    replay_state: Mapped[str] = mapped_column(String(16), default="unverified")
    replay_error: Mapped[str] = mapped_column(Text, default="")

    trajectory: Mapped[Trajectory] = relationship(back_populates="steps")


class TaskAssignment(Base):
    """Who is meant to annotate which task.

    Before this, "assigned" meant every gym task carrying `meta.inEightyFive` —
    so every annotator saw the same 85 rows and every quota target was 85. With
    several people on the batch nobody could tell what was theirs, and the
    progress bar measured the cohort's work rather than the person's.

    A table rather than a derived partition (e.g. hash(email+task) % N) because a
    partition cannot express reassignment, and cannot express deliberate OVERLAP —
    which is exactly what the QA agreement maths needs. Two annotators on one task
    is a feature here, so the key is the pair, not the task.
    """

    __tablename__ = "task_assignment"
    __table_args__ = (
        UniqueConstraint("task_id", "annotator_id", name="uq_task_assignment_task_annotator"),
    )
    id: Mapped[UUID] = _pk()
    task_id: Mapped[UUID] = _fk("task.id")
    annotator_id: Mapped[UUID] = _fk("annotator.id")
    # The batch label the board shows, carried per row so two batches can run at
    # once and an annotator can be on both.
    batch: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="assigned")   # assigned | released
    assigned_by_id: Mapped[UUID | None] = _fk("annotator.id", nullable=True, ondelete="SET NULL")
    assigned_at: Mapped[datetime] = mapped_column(default=func.now())


class AutogenSuite(Base):
    """A generated verifier suite, cached per (task, seed).

    The oracle loop is expensive — it resets the gym, runs the oracle agent, then
    iterates a reward model against the initial-vs-golden worlds until the suite
    scores 0 on one and 1 on the other. It used to return that suite as JSON and
    keep nothing, so every annotator on the same breaker paid for it again and
    the result reached no attempt at all.

    Keyed on (task, seed) rather than on an attempt because that is what it is a
    property OF: the same task at the same seed has the same initial and golden
    worlds, so the suite that discriminates them is the same suite. The second
    annotator gets it for free.

    Deliberately NOT stored in `Task.meta` — `seed.py` rewrites that column on
    every catalog reseed, so a generated artifact parked there is silently lost.
    """

    __tablename__ = "autogen_suite"
    __table_args__ = (UniqueConstraint("task_id", "seed", name="uq_autogen_suite_task_seed"),)
    id: Mapped[UUID] = _pk()
    task_id: Mapped[UUID] = _fk("task.id")
    seed: Mapped[int] = mapped_column(default=0)
    # Whether the suite PASSED the oracle gate (0 on initial, 1 on golden). A
    # suite that failed it is still worth keeping — it is a starting point a human
    # can fix — but it must never be presented as validated.
    oracle: Mapped[bool] = mapped_column(default=False)
    brief: Mapped[str] = mapped_column(Text, default="")
    checks: Mapped[list | dict] = mapped_column(JSON, default=list)   # [{id, level, assertion, code, check}]
    gate: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    iterations: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime] = mapped_column(default=func.now(), onupdate=func.now())


class VerifierSuite(Base):
    __tablename__ = "verifier_suite"
    # A suite version is immutable and unique per session — concurrent saves must
    # not collide on the same (session_id, version) or one silently overwrites the
    # other. save_suite recomputes+retries on the resulting IntegrityError.
    __table_args__ = (UniqueConstraint("session_id", "version", name="uq_verifier_suite_session_version"),)
    id: Mapped[UUID] = _pk()
    session_id: Mapped[UUID] = _fk("review_session.id")
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    session: Mapped[ReviewSession] = relationship(back_populates="suites")
    verifiers: Mapped[list["Verifier"]] = relationship(back_populates="suite", cascade="all, delete-orphan")
    runs: Mapped[list["BenchmarkRun"]] = relationship(back_populates="suite", cascade="all, delete-orphan")


class Verifier(Base):
    __tablename__ = "verifier"
    id: Mapped[UUID] = _pk()
    suite_id: Mapped[UUID] = _fk("verifier_suite.id")
    ext_id: Mapped[str] = mapped_column(String(64), default="")  # stable authoring id (e.g. "sa1") for result keying
    level: Mapped[str] = mapped_column(String(16))  # ui | backend | semantic | process | safety
    assertion: Mapped[str] = mapped_column(Text)
    code: Mapped[str] = mapped_column(Text)
    check_ir: Mapped[dict] = mapped_column(JSON, default=dict)  # executable IR (M5)
    gym_result: Mapped[str] = mapped_column(String(8), default="")  # real milestone result (M8): pass | fail
    fails_until_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    placeholder: Mapped[bool] = mapped_column(Boolean, default=False)
    added_by_human: Mapped[bool] = mapped_column(Boolean, default=False)

    suite: Mapped[VerifierSuite] = relationship(back_populates="verifiers")


class BenchmarkRun(Base):
    __tablename__ = "benchmark_run"
    id: Mapped[UUID] = _pk()
    suite_id: Mapped[UUID] = _fk("verifier_suite.id")
    reward: Mapped[int] = mapped_column(default=0)
    results: Mapped[dict] = mapped_column(JSON, default=dict)  # per-verifier pass/fail
    overridden: Mapped[list] = mapped_column(JSON, default=list)  # verifier ids a human forced to pass
    # --- v2 finalization binding: a score is meaningless unless it names the
    # EXACT trajectory version + suite + end state it was computed against.
    # Scoping by session alone cross-attributes when agent runs finish out of order.
    trajectory_version_id: Mapped[UUID | None] = _fk("trajectory_version.id", nullable=True, ondelete="SET NULL")
    final_checkpoint_id: Mapped[UUID | None] = _fk("environment_checkpoint.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    suite: Mapped[VerifierSuite] = relationship(back_populates="runs")


class Submission(Base):
    __tablename__ = "submission"
    # Exactly one submission per session — submit() locks the session, so the
    # non-atomic check-then-insert is backstopped here against a concurrent
    # double-submit (submit() maps the IntegrityError back to 409).
    __table_args__ = (UniqueConstraint("session_id", name="uq_submission_session"),)
    id: Mapped[UUID] = _pk()
    session_id: Mapped[UUID] = _fk("review_session.id")
    reward: Mapped[int] = mapped_column(default=1)
    kind: Mapped[str] = mapped_column(String(16), default="golden")  # golden | breaker | flagged
    submitted_with_override: Mapped[bool] = mapped_column(Boolean, default=False)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)  # a reviewer adjudicated this the accepted golden (QA)
    # --- v2 finalization binding: the shipped sample names exactly what was
    # approved, scored and against which task revision.
    task_revision: Mapped[int] = mapped_column(Integer, default=1)
    approved_trajectory_version_id: Mapped[UUID | None] = _fk("trajectory_version.id", nullable=True, ondelete="SET NULL")
    benchmark_run_id: Mapped[UUID | None] = _fk("benchmark_run.id", nullable=True, ondelete="SET NULL")
    # The deliverable frozen AT SUBMIT TIME (Cluster A). export.build_sample reads
    # this instead of the live latest-suite/latest-run, so a post-submit mutation
    # can never rewrite what was actually reviewed and scored.
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    session: Mapped[ReviewSession] = relationship(back_populates="submissions")


class TrajectoryBranch(Base):
    """An immutable corrected re-run branch. Every correction creates a new
    version (chained via parent_id); nothing is overwritten. `mode` records how
    the continuation was produced — deterministic (oracle/gold path), or `agent`
    when a live agent re-runs from the corrected state (M6b)."""

    __tablename__ = "trajectory_branch"
    id: Mapped[UUID] = _pk()
    session_id: Mapped[UUID] = _fk("review_session.id")
    parent_id: Mapped[UUID | None] = _fk("trajectory_branch.id", nullable=True, ondelete="SET NULL")
    from_step: Mapped[int] = mapped_column()
    correction: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(16), default="deterministic")  # deterministic | agent
    steps: Mapped[dict] = mapped_column(JSON, default=dict)  # the continuation steps
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    session: Mapped[ReviewSession] = relationship(back_populates="branches")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[UUID] = _pk()
    session_id: Mapped[UUID | None] = _fk("review_session.id", nullable=True, ondelete="SET NULL")
    actor: Mapped[str] = mapped_column(String(255), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str] = mapped_column(String(255), default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=func.now())


# ---------------------------------------------------------------------------
# Live-workspace / versioned-trajectory model (Live Browser Gym v2, Phase 1).
#
# Everything below is ADDITIVE and nullable so the existing fixture + gym flows
# keep working while the new model lands. The design contracts are in the plan:
#   · a checkpoint is a COMPLETE restorable browser+world state (not just world)
#   · raw exploration (InteractionEvent) is separate from committed steps
#   · a version stores only its SUFFIX; the prefix resolves through parents, and
#     inherited steps are NEVER copied to new ids (so verdicts survive a re-fork)
#   · content is immutable; lifecycle STATUS transitions under optimistic locking
# ---------------------------------------------------------------------------


class Artifact(Base):
    """A large binary/JSON blob held outside the row (screenshot, DOM, AX, SoM,
    video). Kept as a table so checkpoints/steps reference an id, and storage can
    move to object storage without touching the referencing schema."""

    __tablename__ = "artifact"
    id: Mapped[UUID] = _pk()
    kind: Mapped[str] = mapped_column(String(16), index=True)  # screenshot | dom | ax | som | video
    uri: Mapped[str] = mapped_column(Text)  # file path now; object-storage URI later
    sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=func.now())


class EnvironmentCheckpoint(Base):
    """A COMPLETE restorable point-in-time: gym world + backend state + browser
    context (tabs, cookies, storage, scroll, viewport) + evidence artifacts +
    hashes. Restoration = reset(task_revision, seed) → replay accepted prefix →
    compare hashes after every action → abort on divergence; a serialized load is
    an optimization, never the correctness story."""

    __tablename__ = "environment_checkpoint"
    id: Mapped[UUID] = _pk()
    attempt_id: Mapped[UUID | None] = _fk("review_session.id", nullable=True, ondelete="CASCADE")
    # --- gym / world ---
    world: Mapped[dict] = mapped_column(JSON, default=dict)            # the compact verifier view (/_harness/world)
    # The COMPLETE world (/_harness/world_full, i.e. dataclasses.asdict). `world`
    # above is a published contract — verifier paths, seed goldens, db-vs-factory
    # byte-equality — so it cannot be widened; but it drops per-product stock,
    # which orders decrement. Restoring from it alone silently restocks whatever
    # the annotator bought, so suspend/resume records this alongside it.
    # Nullable: checkpoints taken before this existed fall back to `world`.
    world_full: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    backend_state: Mapped[dict] = mapped_column(JSON, default=dict)    # /_harness/state summary
    step_clock: Mapped[int] = mapped_column(Integer, default=0)        # deterministic clock = step counter
    # --- browser context ---
    url: Mapped[str] = mapped_column(Text, default="")
    active_tab: Mapped[str] = mapped_column(String(64), default="")
    tabs: Mapped[list] = mapped_column(JSON, default=list)             # FULL tab list, not just the active one
    cookies: Mapped[list] = mapped_column(JSON, default=list)
    storage_state: Mapped[dict] = mapped_column(JSON, default=dict)    # Playwright storage_state
    local_storage: Mapped[dict] = mapped_column(JSON, default=dict)
    viewport: Mapped[dict] = mapped_column(JSON, default=dict)         # {width,height}
    device_pixel_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    scroll: Mapped[dict] = mapped_column(JSON, default=dict)           # {x,y}
    # --- evidence ---
    screenshot_artifact_id: Mapped[UUID | None] = _fk("artifact.id", nullable=True, ondelete="SET NULL")
    dom_artifact_id: Mapped[UUID | None] = _fk("artifact.id", nullable=True, ondelete="SET NULL")
    som_artifact_id: Mapped[UUID | None] = _fk("artifact.id", nullable=True, ondelete="SET NULL")
    # --- integrity ---
    world_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    dom_hash: Mapped[str] = mapped_column(String(64), default="")
    environment_image_digest: Mapped[str] = mapped_column(String(96), default="")
    created_at: Mapped[datetime] = mapped_column(default=func.now())


class TrajectoryVersion(Base):
    """One version in an attempt's lineage: v1 is the canonical agent run, each
    later version is a correction or a human-authored suffix. Stores ONLY its own
    suffix steps — the prefix resolves through `parent_version_id`."""

    __tablename__ = "trajectory_version"
    __table_args__ = (UniqueConstraint("attempt_id", "version_no", name="uq_version_per_attempt"),)

    id: Mapped[UUID] = _pk()
    attempt_id: Mapped[UUID] = _fk("review_session.id")
    parent_version_id: Mapped[UUID | None] = _fk("trajectory_version.id", nullable=True, ondelete="SET NULL")
    version_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(24), default="agent_run")  # agent_run | agent_correction | human_manual
    # v1 binds the EXACT canonical base run (never a "newest/oldest" heuristic).
    base_trajectory_id: Mapped[UUID | None] = _fk("trajectory.id", nullable=True, ondelete="SET NULL")
    # Fork BEFORE the rejected step — the rejected step must not appear in the child.
    # use_alter breaks the trajectory_version <-> trajectory_step cycle (a step
    # belongs to a version; a version forks before a step), so create/drop can be
    # ordered at all.
    fork_before_step_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("trajectory_step.id", ondelete="SET NULL", use_alter=True, name="fk_version_fork_before_step"),
        nullable=True,
        index=True,
    )
    fork_checkpoint_id: Mapped[UUID | None] = _fk("environment_checkpoint.id", nullable=True, ondelete="SET NULL")
    environment_image_digest: Mapped[str] = mapped_column(String(96), default="")
    producer: Mapped[str] = mapped_column(String(64), default="")       # agent name / "human"
    model_config_json: Mapped[dict] = mapped_column(JSON, default=dict)  # model, temperature, etc.
    created_by_id: Mapped[UUID | None] = _fk("annotator.id", nullable=True, ondelete="SET NULL")
    # Content is immutable; STATUS transitions under optimistic concurrency.
    status: Mapped[str] = mapped_column(String(16), default="candidate", index=True)  # candidate|approved|rejected|published
    # How far the raw event stream has been folded into this version's steps.
    # Advanced in the SAME transaction as the steps it produced, so re-folding is
    # a no-op — which is what makes "steps appear as you work" safe to run often.
    materialized_through_seq: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=func.now())


class InteractionEvent(Base):
    """Append-only RAW human/agent interaction. Exploration lives here and never
    pollutes the golden; a committed step points back at the events it came from.
    This — not the server's route-level action_log — is the authoritative action
    source, because backend logs miss navigation/scroll/focus/no-op clicks."""

    __tablename__ = "interaction_event"
    id: Mapped[UUID] = _pk()
    attempt_id: Mapped[UUID] = _fk("review_session.id")
    workspace_lease_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    seq: Mapped[int] = mapped_column(Integer, index=True)  # monotonic per attempt
    kind: Mapped[str] = mapped_column(String(24), index=True)  # click|key|scroll|navigate|popup|tab|backend_effect
    actor: Mapped[str] = mapped_column(String(8), default="human")  # human | agent | system
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    target: Mapped[dict] = mapped_column(JSON, default=dict)  # locator candidates + SoM mark captured BEFORE dispatch
    url: Mapped[str] = mapped_column(Text, default="")
    tab: Mapped[str] = mapped_column(String(64), default="")
    committed_step_id: Mapped[UUID | None] = _fk("trajectory_step.id", nullable=True, ondelete="SET NULL")
    # Minted by the CLIENT, so a retried batch is recognised as the same events.
    # The recorder re-queues a whole batch on any failure — including a network
    # drop AFTER the server committed it — which silently appended a second copy
    # of everything in it, and the duplicates folded into doubled clicks and
    # doubled fills. Nullable: rows written before this existed have none.
    client_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(default=func.now())

    __table_args__ = (
        UniqueConstraint("attempt_id", "client_event_id", name="uq_event_client_id"),
    )


class StepVerdict(Base):
    """Per-step review verdict keyed by the step's STABLE UUID (never a positional
    index), so progress survives a re-fork instead of being clamped."""

    __tablename__ = "step_verdict"
    __table_args__ = (UniqueConstraint("attempt_id", "step_id", name="uq_verdict_per_step"),)

    id: Mapped[UUID] = _pk()
    attempt_id: Mapped[UUID] = _fk("review_session.id")
    step_id: Mapped[UUID] = _fk("trajectory_step.id")
    verdict: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|verified|rejected
    note: Mapped[str] = mapped_column(Text, default="")
    annotator_id: Mapped[UUID | None] = _fk("annotator.id", nullable=True, ondelete="SET NULL")
    reviewer_id: Mapped[UUID | None] = _fk("annotator.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime] = mapped_column(default=func.now(), onupdate=func.now())


class WorkspaceLease(Base):
    """An EPHEMERAL live workspace (gym process + Chromium) leased to an attempt.
    Durable ReviewSession != ephemeral lease. Persisted (not just in memory) so a
    backend restart can reconcile or reprovision. TTL is INACTIVITY-based."""

    __tablename__ = "workspace_lease"
    id: Mapped[UUID] = _pk()
    attempt_id: Mapped[UUID] = _fk("review_session.id")
    annotator_id: Mapped[UUID | None] = _fk("annotator.id", nullable=True, ondelete="SET NULL")
    # The two runtime types are never shared: a long-lived `human` workspace (what
    # the live browser attaches to) vs a short-lived `agent_branch` worker cloned
    # from a checkpoint for one batch run. An agent run must never execute against
    # the human's workspace — the gym resets one global SESSION per process.
    purpose: Mapped[str] = mapped_column(String(16), default="human", index=True)  # human | agent_branch
    runtime_kind: Mapped[str] = mapped_column(String(24), default="local_process")  # local_process | kubernetes
    endpoint: Mapped[str] = mapped_column(Text, default="")      # http://127.0.0.1:PORT
    external_ref: Mapped[str] = mapped_column(Text, default="")  # pid / pod name
    status: Mapped[str] = mapped_column(String(16), default="provisioning", index=True)  # provisioning|ready|expired|terminated
    environment_image_digest: Mapped[str] = mapped_column(String(96), default="")
    # What this workspace's gym was last SEEDED with, written only after a reset
    # actually succeeded. A workspace outlives the live browser attached to it, so
    # reopening a pane must not re-seed a world the annotator has been building by
    # hand — but "skip the reset" is only safe against a durable record of what is
    # in there. Process memory cannot answer it: a backend restart is precisely one
    # of the events that drops the attachment while the container keeps running.
    #
    # Nullable, and `seeded_seed` is a nullable Integer rather than defaulting to 0,
    # so "seeded with seed 0" stays distinguishable from "never seeded".
    seeded_task_key: Mapped[str | None] = mapped_column(Text, nullable=True)   # the registry key we POSTed
    seeded_task_id: Mapped[str | None] = mapped_column(Text, nullable=True)    # the id the GYM echoed back
    seeded_seed: Mapped[int | None] = mapped_column(nullable=True)
    # Which VERSION's world this workspace holds. A fork's world is the fork point
    # of one specific version, and two versions of the same attempt share a
    # (task, seed) — so without this, switching versions and reopening would reuse
    # the previous version's world under a marker that only checks (task, seed).
    # NULL = a plain seed with no branch prefix (an unforked attempt).
    seeded_version_id: Mapped[UUID | None] = _fk("trajectory_version.id", nullable=True, ondelete="SET NULL")
    # How far the branch prefix was replayed into this workspace's world on the
    # last seed. NULL total = no rebuild was attempted (an unforked attempt).
    # Kept here, not in process memory, because the claim is about what is in THAT
    # gym — it must survive a backend restart and die with the workspace.
    restore_done: Mapped[int | None] = mapped_column(nullable=True)
    restore_total: Mapped[int | None] = mapped_column(nullable=True)
    restore_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_active_at: Mapped[datetime] = mapped_column(default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    terminated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class AgentRunJob(Base):
    """A batch agent branch run, bound to the EXACT source it forked from. On
    completion it creates a CANDIDATE child version and never auto-advances the
    attempt head — selection is a separate versioned command, which is what makes
    out-of-order completions safe."""

    __tablename__ = "agent_run_job"
    id: Mapped[UUID] = _pk()
    attempt_id: Mapped[UUID] = _fk("review_session.id")
    source_version_id: Mapped[UUID | None] = _fk("trajectory_version.id", nullable=True, ondelete="SET NULL")
    source_checkpoint_id: Mapped[UUID | None] = _fk("environment_checkpoint.id", nullable=True, ondelete="SET NULL")
    expected_attempt_revision: Mapped[int] = mapped_column(Integer, default=0)
    result_version_id: Mapped[UUID | None] = _fk("trajectory_version.id", nullable=True, ondelete="SET NULL")
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|done|error|cancelled
    idempotency_key: Mapped[str] = mapped_column(String(80), default="", index=True)
    owner: Mapped[str] = mapped_column(String(64), default="")  # worker id, for stale-job recovery
    heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    provider_request_started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    provider_request_id: Mapped[str] = mapped_column(String(128), default="")
    counts_against_cap: Mapped[bool] = mapped_column(Boolean, default=True)  # false for confirmed infra failures
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=func.now())
