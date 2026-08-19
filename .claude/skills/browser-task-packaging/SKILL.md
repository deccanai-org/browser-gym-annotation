---
name: browser-task-packaging
description: Package browser-gym tasks into a Harbor-style deliverable from whatever artifacts actually exist. Use when shipping browser tasks to a buyer (the buyer's delivery-format spec / harbor pack, ecommerce-breakers, multiapp your-gym-host gyms), converting an annotation-platform iteration export, deciding which tasks ship vs get quarantined, or writing the root-level delivery report. Covers the evidence tiers (what you may claim given what you have), the the delivery-format spec tree, the reward-veto rule, the UI-driving oracle, the quality gate split, and the honesty rules for docs/README.md. NOT for coding/SWE task packaging.
---

# Browser Task Packaging

One tree, one source record, two zips. This skill turns whatever artifacts exist into a delivery
that survives an auditor, and refuses to make claims the artifacts do not support.

Two buyer contracts are in play and they are **not** reconciled by compromise:

- **Contract A (the buyer's delivery-format spec–the per-component quality standards):** harbor-style tree — `tasks/`, `trajectory/`, `environment/`, `docs/`.
- **Contract B (tar + JSON + API):** gym image tarballs, one JSON per task (prompt, verifier, model
  response, db diff, workflows, applications), an API/pipeline, and two how-to documents.

**Every item in B maps to a slot inside A. Nothing in A fits inside B.** So build to A and cut B as a
selection: `environment/` + every `task.json` + `RUNNING.md` + `GYM_SCOPE.md`. Never build B separately —
that produces two artifacts that drift and two QA passes.

## 0. The agents, and who does what

This skill has a matching agent fleet in `agents/`. Install both (see `README.md`) and the work decomposes:

| Agent | Owns | Delegate when |
|---|---|---|
| **browser-task-packager** | the whole job; orchestrates the rest | any end-to-end packaging or delivery request |
| **packaging-triage** | inventory · evidence tier · admission gates · the channel decision | always first — nothing moves before it |
| **verifier-packager** | frozen suite · judge contract and gold path · the executability test | the scoring half, or auditing whether a verifier can actually be run |
| **solvability-auditor** | S0–S4 tier · information provenance · hint calibration · runner assignment | a breaker, or any task described as "non-broken" |
| **failure-analyst** | per-trajectory analyses · the distribution | trajectories are being packaged or a distribution is asserted |
| **delivery-reporter** | root report · per-task docs · format provenance | last among the docs, because it summarises everything |
| **packaging-qa** | validator run · triage every finding into fix / declare / accept | the gate; nothing is sent before it |

Triage first, then verifiers + solvability + failure modes concurrently, then docs, then QA. Working solo is
fine for a single task — the sections below are the same method either way.

---

## 0a. There is no browser spec — know which parts you are inventing

Check before quoting the spec at anyone: in `the buyer's.html` the words
**“browser”, “computer” and “CUA” do not appear at all.** The task types it names are “coding, tool use,
office, etc.”, and section 4.2 is headed *“Examples (SWE-bench tasks)”*. Every concrete instruction in it is
coding-shaped.

So the layout is mandated and task-type agnostic; the **mechanism** is SWE-shaped and everything we do with
it for a browser task is interpolation. Keep the three categories separate, in your own head and in the
delivery, because presenting our invention as their requirement is how a batch gets built wrong at scale:

| Category | Examples |
|---|---|
| **Spec, agnostic** — quote it | the four top-level dirs · per-task docs under root `docs/` · `trajectory/<task>/<model>/rollout_<harness@ver>_<n>` · ATIF · `environment/` for private images · the `[rollout_baseline]` field list · G0/G1/G2 · the difficulty criteria thresholds and model list · the hint-validation method hint approach · the provenance requirement `edit_history/v0..vn` · the failure-analysis requirement per-trajectory analysis · the reward-hacking requirement four aspects · the reporting requirement report depth · the per-component quality standards quality dimensions |
| **Spec, SWE-shaped mechanism** — we mapped it | `tests/test.sh` → fetch gym state and evaluate the suite · `solution/solve.sh` → Playwright oracle · G0 “image builds” → *which* image, sandbox or live gym · G1 → accepting an oracle **film** as evidence · G2 → the positive-milestone precondition · section 4.2's patch/regression hint examples → disclosure and exploration dimensions · `artifacts` → a state snapshot |
| **Entirely ours** — say so | `rubric.json` · the veto hard-zero rule · `solvability.json` and the S0–S4 tiers · `scope_limitations.json` · `MANIFEST.json` · `metadata.json` · the `[browser]` block and `frozen_time` · `environment_mode` + the two-port split · `task_definition/` · `applications[]`/`workflows[]` and the diversity facet grid · the hint-runner matrix · `task.json` (that is Contract B's, not the delivery-format spec's) |

Two consequences. **First: “send one task as a format check before building more” is not politeness, it is
the only way to validate an interpolation this large** — a systematic mapping error found on task 1 costs a
day, found on task 85 costs the batch. **Second: the root report should state the interpolation**, so the
buyer is confirming a mapping rather than assuming a standard exists. Nothing here is a reason to stop; it
is a reason to label.

---

## 0b. The oracle is a film and the scorer is a CLI — so neither script ships

A browser task's primary artifacts are **(environment image, seed state, verifier suite, trajectory,
final state)**. Contract A's `tasks/` + `tests/test.sh` + `solution/solve.sh` is a *coding-shaped
adapter* over those — the convention comes from a repo-and-pytest world. Contract B asks for the
browser-native artifacts directly, which is why it reads like a different request: it is the same task
described in its own terms.

The spec's own requirement is thin and does transfer. `test.sh` must "write reward to
`/logs/verifier/reward.json`" — an entrypoint, not a test runner. `solve.sh` is "oracle reference
solution (optional)". Written honestly for a browser task they are four lines and one line. What does
**not** transfer are the four assumptions the filenames usually smuggle in:

| Coding-task assumption | Browser reality |
|---|---|
| agent, environment and verifier share one container | the agent needs a browser, the apps need serving, the verifier needs privileged state the agent must not reach → two ports, `environment_mode = "separate"` |
| the artifact under test is a filesystem diff | the artifact is **service state** — DB rows, mail sent, orders placed. There is no file to collect; a snapshot must be fetched |
| the oracle is a patch you apply | the oracle is a *sequence of interactions*, and it can fail for reasons a patch cannot — a moved control, an unexpected modal. That failure mode is information a patch-shaped oracle cannot produce |
| reward is a binary test exit code | weighted milestones plus a veto axis — fine, because `reward.json` is JSON, not an exit code |

**So neither script belongs in the browser format.** Each duplicates something a browser task already has
in a better form:

| SWE file | Browser equivalent that already exists | Why the file is worse |
|---|---|---|
| `solution/solve.sh` | **the oracle film** — a recorded run that drove the real UI to reward 1.0, with its ATIF trajectory and frames | a script writing a gold JSON passes G1 *by construction*; it cannot detect a moved control or an unreachable flow, and it hides a task that has no oracle at all |
| `tests/test.sh` | **a verifier CLI** — `gymverify` over `(suite.json, state snapshot)` | any harness can invoke a CLI, and it is what the API-shaped contract needs anyway; a shell file at a fixed path inside an image serves only one harness |

**Default: ship neither.** The oracle is the film, the scorer is the CLI, and `tasks/` does not appear. Two
qualifications worth carrying:

- **A film is not re-runnable.** It evidences that the task *was* solvable through the UI; a buyer who wants
  to rebuild the environment and re-derive 1.0 themselves cannot do that from a recording. If they ask,
  answer with an `oracle/oracle.py` Playwright module shipped *with the environment* — a real reproduction
  of the film — never a shell wrapper writing an outcome file.
- **If a harness genuinely requires entry points**, add `tasks/<task_name>/` as a **shim over the same
  artifacts**: `tests/test.sh` four lines calling `gymverify`, `solution/solve.sh` one line calling
  `oracle.py`. No logic of its own, so it cannot drift. And only when the environment ships, since both need
  the app reachable. Confirm before building it — a `tasks/` whose image cannot host the task invites an
  agent run in a container where the brief is unperformable.

---

## 1. Inventory the artifacts before deciding anything

Run this first. What you may claim is a function of what exists, not of what you intend.

```bash
PACK=<path>
find "$PACK" -type f | grep -v '\.DS_Store' | sed 's|.*/||' | sort | uniq -c | sort -rn | head -30
python3 "$VALIDATOR" "$PACK"      # full gate report
```

Recognised input shapes:

| Shape | Fingerprint | What it is |
|---|---|---|
| Harbor pack | `tasks/`, `trajectory/`, `docs/`, `MANIFEST.json` | already the delivery-format spec-shaped; needs fixes, not conversion |
| Iteration export | `task_responses/response.json`, `timeline/action_timeline.json`, `db_diff/`, `verification/`, `replay.html` | one run from the annotation platform; forensics, not a task |
| Platform record | `GET /export/samples/{session_id}` or `/export/dataset.jsonl` | the **source of truth** — prefer this over any zip |
| Gym module | `server/<id>.py` | the live task definition; brief and milestones live here |

The platform record (`export.py::build_sample()`) already carries `task.prompt`, `initial_state`,
`verifiers[]`, `recorded_trajectory`, `golden_trajectory`, `reward`, submission provenance. It is missing
exactly four Contract-B fields — `model_response`, `db_diff`, `applications[]`, `workflows[]`. Add those
and every downstream file becomes a projection instead of hand-written prose.

---

## 2. Pick the evidence tier — this decides what you may claim

**The single most common delivery defect is a claim with no artifact behind it.** Find your row; ship at
that tier; state the tier in the root report.

Read section 0 first: if the environment does not ship, only the bottom three rows are available to you,
whatever else you have.

| You actually have | Ship as | May claim | Must NOT claim |
|---|---|---|---|
| **Environment ships** + `tests/` + UI-driving `solve.sh` + ≥5 frontier films on the shipped brief | Full the delivery-format spec pack | G0, G1 (live), G2, the difficulty criteria difficulty, non-brokenness | Hy3 pass@16 unless run |
| **Environment ships** + `tests/` + oracle **film** whose stored brief == `instruction.md` + ≥5 films | Full the delivery-format spec pack | same, G1 evidenced by the film id | that `solve.sh` alone proves solvability |
| Environment does **not** ship, ≥5 films + oracle film | **trajectory channel** + `task_definition/` | the difficulty criteria difficulty, non-brokenness by film, all Contract-B items | any G-gate; a stub sandbox does not host the task |
| Environment does not ship, and a stub sandbox ships anyway | the delivery-format spec pack, **conformance-only**, discouraged | that the scorer accepts gold and rejects nop | G0/G1/G2 for the task, or any comparison between a sandbox run and the films |
| Definition + `tests/`, **zero films** | Quarantine | nothing | any `[rollout_baseline]` block; delete it rather than ship it |
| Films whose brief ≠ shipped `instruction.md` | Quarantine, or re-film | nothing from those films | attaching them trades a the delivery-format spec gap for a the per-component quality standards mismatch |
| Iteration export only | trajectory channel + `task.json` | all Contract-B items | G0/G1/G2, the difficulty criteria |

Trajectory-channel bundles are legitimate — the delivery-format spec names the channel explicitly for "pure trajectory data,
case by case". Carry the full definition alongside the films so the bundle is promotable rather than
merely archival:

```
<dataset>-<batch>-trajectories-<num>/
├── README.md                          # states plainly: no tasks/, no G-gates claimed, and WHY
├── task_definition/<task_name>/       # everything needed to become tasks/<task_name>/ later
│   ├── instruction.md                 # the live BRIEF verbatim
│   ├── rubric.json                    # the frozen verifier suite
│   ├── seed/                          # seed_initial.json per gym (or the workspace materialisation)
│   ├── verifier_reference/score.py    # reference implementation — explicitly NOT a gate
│   └── task.toml                      # metadata + [rollout_baseline]; no gate claims
├── trajectory/<task_name>/<model>/rollout_<harness@ver>_<n>/
│   ├── task.json  reward.json  trajectory.json  db_diff.json  replay.html  images/
│   └── failure_analysis/failure_analysis.md
├── environment/README.md              # the image map, stated as empty if it is
└── docs/                              # same dataset + per-task docs as the full pack
```

**Why `task_definition/` and not `tasks/`.** The name is the honest signal: nobody will try to run it as
a Harbor task, and nothing claims a gate. Promotion the day the image ships is mechanical — rename to
`tasks/<task_name>/`, add `environment/Dockerfile`, move `verifier_reference/score.py` to
`tests/score.py`, add the four-line `tests/test.sh` and the one-line `solution/solve.sh` from the hint-validation method.

**Admission gates (apply per task, before building):**

1. `trajectory/<task_name>/` exists and holds ≥1 rollout — the delivery-format spec makes it required.
2. Declared `rollout_n` == actual rollout directory count.
3. Film brief == `instruction.md`. Check it: ATIF `steps[0].message` must equal `instruction.md`.
4. Recomputed `pass_rate` ≤ 0.40 (the difficulty criteria), computed **after** the veto rule, with oracle rollouts excluded
   from the denominator.
5. Every task has ≥1 positive required milestone the seed state does not already satisfy (else nop
   scores 1.0 and G2 fails as a reward leak — the classic trap on refusal tasks).

A task failing any of these goes to `_staging/` with a named route back in. Do not soften the root
report to accommodate it.

---

## 3. The tree

`<dataset>-<batch>-<num>/` is a **batch** root — it holds many tasks. Do not ship one task per root; that
duplicates the four dataset-level documents once per task.

```
<dataset>-<batch>-<num>/
├── task_definition/<task_name>/           # the task, in browser-native terms
│   ├── instruction.md                     # the live BRIEF verbatim
│   ├── rubric.json                        # the frozen verifier suite — milestones, weights, veto
│   ├── seed/seed_initial.json             # the world at t=0, per gym
│   └── task.toml                          # config + [rollout_baseline] + the the per-component quality standards explanation strings
│
├── trajectory/<task_name>/
│   ├── <model_name>/rollout_<harness@version>_<n>/
│   │   ├── task.json                      # the API-contract payload, one per run
│   │   ├── trajectory.json  reward.json  db_diff.json  gym_episode.jsonl  images/
│   │   └── failure_analysis/failure_analysis.md
│   └── oracle/rollout_oracle@<version>_1/ # ← THE ORACLE. a recorded UI-driving run at reward 1.0
│
├── verifier/
│   ├── suite.json                         # frozen, per task
│   └── gymverify/                         # CLI: (suite, state snapshot) → reward.json
│
├── environment/{README.md, <gym_image>/{image.tar, manifest.md}}
├── docs/
│   ├── README.md                          # THE ROOT REPORT — see the failure-analysis requirement
│   ├── failure_analysis_metadata.md  reward_hacking.md  scope_limitations.json
│   ├── RUNNING.md  GYM_SCOPE.md  QUALITY.md
│   └── <task_name>/
│       ├── README.md  report.md  context_info.md  metadata.json  solvability.json
│       └── edit_history/v0/task/ · v1..vn/{task/, review/, v{n-1}_v{n}_patch.diff}
└── MANIFEST.json                          # regenerate LAST
```

Placement rules that are decided, not open:

- **No `solution/` and no `tests/`** by default — see section 0b. The oracle is the film under
  `trajectory/<task>/oracle/`; the scorer is the CLI under `verifier/`.
- `rubric.json` lives with the task definition — it is verifier configuration and the only readable
  declaration of the veto axis.
- `metadata.json` and `solvability.json` live under `docs/<task_name>/` — provenance and evidence, not
  harness config. Never duplicate `gym_task_id`, `start_path`, `apps` or `frozen_time` there;
  `task.toml [browser]` owns them.
- `<model_name>` is the **model**, not the harness: `openai__<frontier-model>/`, with the harness in the rollout
  directory name.
- The oracle rollout is **gate evidence and is excluded from the pass@k denominator** — say so in the root
  report, or a reader computing from the directory listing gets 1/6 instead of 0/5.
- Multiple gym stacks ⇒ multiple roots. One `docs/README.md` cannot honestly describe two datasets with
  different images, verifier strategies and failure taxonomies.
- **If the environment does not ship**, the root becomes `<dataset>-<batch>-trajectories-<num>` and the
  `environment/` and `verifier/` directories carry a README stating what is absent and why. Everything else
  is unchanged, which is what makes promotion mechanical.

---

## 4. Three contracts that must hold before you build

**4.1 The veto is in the scalar.** the difficulty criteria reads `reward.json["reward"]`, the reward-hacking requirement excludes hacked runs, the per-component quality standards calls
instruction↔verifier mismatch severe. One rule serves all three:

```jsonc
{ "reward": 0.0,          // hard 0.0 if ANY forbidden milestone fired; else Σ(weights of fired required)
  "success": false,       // reward >= rubric.pass_threshold
  "disposition": "BREAK", // human label — keep it, but it is NOT what the buyer computes from
  "verifier_result": { "all_milestones": [...], "forbidden_fired": ["..."] } }
```

Seen in the wild: a film that invented a warranty claim fired its forbidden milestone and still recorded
`1.0`, giving a declared `reward_mean = 1.0` / `pass_rate = 0.6` on a task the buyer caps at 0.40.
Recompute every `[rollout_baseline]` after fixing this — never carry forward the pre-veto numbers.

**4.2 The oracle is a film.** Ship the recorded UI-driving run at reward 1.0 under
`trajectory/<task>/oracle/`, and check two things about it: that it drove the real UI, and that its stored
brief equals `instruction.md`. A task with no film has no oracle — do not let a gold-state-writing script
disguise that. If a buyer asks for re-runnability, answer with `oracle/oracle.py` shipped with the
environment (section 0b), not a shell wrapper.

**4.3 Ground truth is fetched, never read from a path the agent can write.** There is no filesystem
artifact to collect for a browser task — the artifact is service state, so the verifier fetches a snapshot
and scores that. Any scorer reading `/logs/agent/*` is a live reward-hack surface; grep for it.

```bash
gymverify --suite verifier/suite.json --state <snapshot.json> --out reward.json
```

Two ports, structurally: `:3000` app origins published (the agent's whole world); `:8000` control API on
loopback, never published, never proxied. Prove the isolation with the the provenance requirement test, not with a sentence in
`reward_hacking.md`.

```bash
#!/usr/bin/env bash
# tests/test.sh
set -euo pipefail
LOGS="${LOGS:-/logs}"; mkdir -p "$LOGS/verifier"
HERE="$(cd "$(dirname "$0")" && pwd)"
curl -sf http://127.0.0.1:8000/_harness/state > "$LOGS/verifier/final_state.json"
python3 -m gymverify --suite "$HERE/../rubric.json" \
                     --state "$LOGS/verifier/final_state.json" \
                     --out   "$LOGS/verifier/reward.json"
```



**4.4 Solvability is separate evidence from difficulty, and it is not the oracle.** A breaker has to be
hard *and* solvable, and the two are evidenced independently — a task that is hard because it is broken
is worthless, and packaging is where that gets caught rather than discovered.

**The ladder.** Each tier proves something the tier below does not:

| Tier | Evidence | Proves | Satisfies |
|---|---|---|---|
| **S0** | none | — | nothing; not shippable |
| **S1** | scorer accepts a gold state written by `solve.sh` | the verifier accepts the intended answer | nothing about the task |
| **S2** | UI-driving oracle or oracle film reaches 1.0 on the shipped brief | the gold path is reachable through the UI by *something* | the acceptance gates G1 |
| **S3** | ≥1 frontier film resolves unaided | an *agent* can do it | the difficulty criteria, without invoking the hint clause |
| **S4** | 0 resolves **plus** a hint rerun in which an agent surpasses the failure point on a partial hint that does not reveal the solution | solvable in principle by an agent | the difficulty criteria's non-brokenness clause **and** the hint-validation method label validation |

**The rule that catches most breaker batches: a 0-resolve task needs S2 *and* S4.** the difficulty criteria says that if a task
is resolved zero times by both models, non-brokenness must be demonstrated *via the hint approach*, and the hint-validation method
is explicit that the demonstration requires the task to be "solvable by an agent given a partial hint that
does not directly reveal the solution." **An oracle does not satisfy this** — the oracle is not an agent
working from the brief, so S2 is G1 evidence and nothing more. Every task in a breaker batch is 0-resolve
by construction, so the hint rerun is load-bearing twice: once for the hint-validation method label validation, once as the the difficulty criteria
non-brokenness demonstration. Treating it as optional label polish under-reads the spec.

**Information provenance — the cheap check that prevents an unsolvable task.** For every required
milestone, name the artifact that carries the fact the agent needs. If you cannot name one, the milestone
is unsatisfiable from inside the environment and the task is broken. Record it per task in
`docs/<task_name>/solvability.json` so it is auditable rather than asserted:

```jsonc
{ "tier": "S2",
  "resolve_count": { "frontier": 0, "of": 5, "hy3": null },
  "oracle": { "kind": "film", "id": "…", "reward": 1.0, "drives_ui": true,
              "brief_matches_instruction": true },
  "hint_demonstration": {
    "required": true, "reason": "0 resolves; Hy3 not run",
    "hint_text": "…", "calibration": "…",
    "non_brokenness_s4a": { "runner": "claude-opus-5", "status": "not_run", "result": null },
    "label_validation_s4b": { "runner": "openai/<frontier-model> @ <harness>, seed 0",
                              "status": "not_run", "result": null } },
  "upper_bound_probe_s3": { "runner": "claude-opus-5", "unaided": true, "status": "not_run",
                            "counts_toward_pass_at_5": false },
  "information_provenance": [
    { "milestone": "…", "fact_needed": "…", "carried_by": "…path in the pack…", "app": "shop" } ],
  "designed_infeasibility": [
    { "requirement": "…what the brief asks that cannot be done…",
      "why": "…", "gold_resolution": "disclose plainly rather than working around it" } ],
  "false_negative_surfaces": [ "…where a correct answer in different words is rejected…" ] }
```

**Who runs which probe.** The hint rerun serves two purposes, and they do not take the same runner —
conflating them is what makes a solvability claim unfalsifiable.

| Purpose | Runner | Why this runner | Counts toward |
|---|---|---|---|
| the difficulty criteria difficulty, unaided | **<frontier-model>** or **Fable 5** at the pinned harness | the difficulty criteria names these ("GPT 5.6 Sol or Fable 5; 4.8/5.5 acceptable for older data") | the pass@5 criterion |
| **S3 upper-bound probe, unaided** | **Opus 5** (`claude-opus-5`) | tests whether the difficulty is model-specific or intrinsic. the difficulty criteria asks to "highlight tasks where frontier models consistently resolve but Hy3 consistently fails", so the spread is wanted | supplementary — **never** the pass@5 denominator |
| **S4a non-brokenness, hinted** | **Opus 5** | the claim is about the *task*, not the failing agent, so the strongest available agent is the cleanest evidence. If Opus still fails on a calibrated hint, suspect a broken task or a verifier false negative — that is section 4.3's diagnostic reading, and it is a more valuable result than a pass | the difficulty criteria's non-brokenness clause |
| **S4b label validation, hinted** | **the same model + harness + seed as the failing film** | the claim is causal, so the hint must be the *only* changed variable. Swapping the model changes two things and validates nothing about the observed failure | the hint-validation method / the failure-analysis requirement label validation |
| Hy3 baseline floor | **Hy3**, pass@16 | the difficulty criteria baseline | the floor |

Two consequences worth stating before anyone runs anything. **Opus is not on the difficulty criteria's frontier list**, so an
Opus result — hinted or not — cannot substitute for the pass@5 number or enter its denominator; if a Claude
model is wanted *inside* the difficulty criterion, **Fable 5 is the spec-approved one**. And a passing S4a
does not license an S4b claim: record the runner against each purpose in `solvability.json`, because a
reader cannot otherwise tell which of the two questions a rerun answered.

**Four ways a task is unsolvable, and which one you are looking at:**

1. **Fact absent** — a required milestone needs something no artifact carries. The provenance table catches
   it; every row must name a file that exists.
2. **Solvable but unverifiable** — the fact is discoverable and the agent said it correctly in other words,
   and a token regex rejected it. This *masquerades* as an unsolvable task and is the most common false
   negative; record it under `false_negative_surfaces` instead of leaving it to be found.
3. **Requirement genuinely unsatisfiable** — the brief asks for two things that cannot both hold. This is
   either the designed trap (and then it has a gold resolution: disclose, quote, omit) or a broken task
   (and then it does not). `designed_infeasibility` is where you state which, and naming the gold
   resolution is what proves it is the former.
4. **Control unreachable** — the UI cannot do what is required. Only a UI-driving oracle detects this; a
   state-write `solve.sh` cannot, which is the substantive reason S1 is worth nothing.

---

## 5. Quality harness — gate on objective, report on semantic

A criterion's ceiling is set by what kind of quantity it is. Classify, then pick the verifier. Gating on a
weak judge rejects good tasks at roughly the rate it catches bad ones.

| Criterion | Type | Verifier | Role |
|---|---|---|---|
| Near-duplicate prompt | objective | pairwise embedding cosine > 0.90 vs every shipped prompt | **gate** |
| Spell / grammar / house conventions | objective | LanguageTool + lint (em-dashes stripped, recipient convention, no gym persona in `authors`) | **gate** |
| Verifier leakage in the brief | objective | `instruction.md` must not contain milestone ids or forbidden-milestone strings | **gate** |
| Non-brokenness | objective | Playwright oracle drives the gold path end to end | **gate** |
| Determinism | objective | run the oracle twice, assert identical state-hash sequences | **gate** |
| Isolation | objective | from the agent's browser context, `fetch('/_harness/state')` must fail | **gate** |
| Golden pair / veto / nop | objective | unit tests on `eval_predicate`: seed → 0.0, golden → 1.0, forbidden ⇒ exactly 0.0, ≥1 positive required milestone unsatisfied at seed | **gate, in CI** |
| Instruction ↔ verifier correspondence | semantic, reference-conditioned | LLM judge structurally aligned against `rubric.json`, **both directions** | review queue |
| Realism | semantic, holistic | LLM judge, graded 1–5 — reuse an existing calibrated prompt-quality judge | sampler → HITL |
| Diversity | coverage, not similarity | facet grid | report |

Three corrections to the usual proposal:

- **Cosine is not a diversity metric.** It measures phrasing. Two briefs at 0.4 can be the same task in
  different words; two at 0.85 can carry different traps. Use it as a near-dup gate only. Measure diversity
  as **coverage over `applications × workflows × forbidden-class × vein`** — cells occupied / cells defined,
  plus the count in the fullest cell. Five tasks that are all
  "ecommerce × order-modification × disclose-and-quote" are a 1-cell batch however differently they read.
- **Spell-check is a tool, not a judge.** Objective and detectable ⇒ CI, not an LLM call.
- **Calibrate any judge before it gates.** Realism has a skewed base rate, so F1 flatters badly (a *random*
  judge has scored F1 0.74 at MCC +0.01). Report MCC + Spearman with bootstrap 95% CIs at n ≥ 700, and
  diagnose variance vs bias (3–5 runs at temp 0.3 on ~40 prompts, majority-vote — flat vote means bias and
  self-consistency will not help). See the `judge-calibration` skill for the full order of operations.

Derive the two facet fields, never hand-tag them: `applications[]` from the timeline's `switch_gym` events
plus the preloaded gym; `workflows[]` from a fixed table→workflow map over **the verifier suite's tables**,
not the observed diff — otherwise every failed task under-reports its own scope.

If `vein` / `specific_failure` are unpopulated in the trajectories, say so and report coverage over the
remaining axes. Do not assert a the failure-analysis requirement concentrated-vs-diverse distribution you cannot compute.

---

## 6. The root-level report

`docs/README.md` is the honesty contract for the whole batch. It is read first, and it is where a delivery
is won or returned. Its job is not to sell the batch — it is to state **exactly what is claimed, on what
evidence, and what is not claimed.** Template: `templates/root_report.md`.

**Required blocks, in this order:**

1. **Title + one line** on what the batch is, and a pointer to the layout spec.
2. **Scorecard table** — one row per task: slug, gym id, frontier n-seed result with the actual per-seed
   scores, oracle evidence (film id or "none"), confirmation status. Every cell traces to a file in the pack.
3. **Definition of the strength term you used.** If the table says "confirmed-confirmed", define it in the
   next sentence and name which tasks meet it. An undefined strength claim reads as marketing.
4. **Deferrals, up front, in plain language.** Name every cap: Hy3 pass@16, the 20+ trajectory expectation,
   hint reruns not run, cost/token means not recaptured. the reward-hacking requirement requires disclosure of any coverage cap —
   silent truncation reads as full coverage.
5. **"Provenance that is NOT this pack's claim."** List the adjacent films, older briefs and neighbouring
   deliveries that a reader could mistake for evidence, and say why each is excluded. This block is what
   separates a careful vendor from a hopeful one; write it even when nothing forces you to.
6. **Excluded tasks and the route back in** — one line each on what is missing and what would requalify it.
7. **Gate results** — G0/G1/G2 with *how each was evaluated*, including scope limits ("G1 evidenced by
   <harness> film `<oracle-film-id>`", "G0 satisfied for the Harbor sandbox; the live browser stack is out of
   scope in this batch").
7b. **Solvability** — the section 4.4 tier per task, the resolve count it is derived from, and for any 0-resolve
   task whether the difficulty criteria's hint demonstration has been performed. State the tier as a claim about evidence, not a
   belief: "S2 — gold path reachable, evidenced by film `x`; S4 not reached, so non-brokenness by the difficulty criteria's
   hint clause is not yet demonstrated." Difficulty and solvability pull in opposite directions, so a
   reader needs both numbers side by side to see that the task is hard *and* not broken.
8. **Quality summary** — which gates ran and passed, judge MCC/Spearman with CIs, facet coverage, and any
   axis that could not be computed.
9. **Format provenance** — one short block naming which parts of the layout are the spec's, which are our
   mapping of an SWE-shaped mechanism onto a browser task, and which are additions of ours. The spec never
   mentions browsers, so this is what lets the buyer confirm or correct the mapping instead of assuming one
   exists. See section 0a.
10. **Validate** — a command that actually exists and runs against the shipped tree.

**Honesty rules (hard):**

- Never write a number you did not read out of an artifact in the pack. No invented scores, no invented
  screenshots, no estimated token means presented as measured.
- Every claim names its evidence — a film id, a file path, a rollout directory.
- If your metric differs from the buyer's, **show both.** "5/5 BREAK on our disposition; `reward` reads
  1.0×5, so their pass@5 computes as 100% — that is why this task is excluded."
- Name the denominator. Oracle films are gate evidence, not rollouts.
- Declare the evidence tier from the acceptance gates. "Gates scoped to the Harbor sandbox" is a fine thing to ship; an
  unqualified "G1 pass" on a state-write oracle is not.
- No internal asides. Repo paths, "does not overwrite X", reviewer nicknames and TODOs are notes to
  ourselves, and they read as sloppiness to a buyer.
- Do not reference a file or command the pack does not contain. A README pointing at a missing
  `LAYOUT.md` or an unshipped validator module is itself a the per-component quality standards documentation defect.

**Bundle-level report** (only when shipping multiple roots): a top-level `README.md` naming each dataset
root, what it contains, its ship status, and which buyer contract it satisfies. One paragraph per root.
State explicitly that the roots are separate because they share no image, verifier strategy or taxonomy —
otherwise it looks like an oversight.

**Per-task `report.md`** (the reporting requirement): a one-pager is explicitly insufficient. Cover contextual information, edit
history, hint-validated failure analysis, the distribution, and non-brokenness evidence. It may reference
the root report for the batch-level scorecard rather than restating it.

---

## 7. Build order

Do it in this order; each step's output is the next step's input, and `MANIFEST.json` is last because
everything invalidates it.

```bash
SKILL=~/.claude/skills/browser-task-packaging
```

0. **Channel decision** — section 0. Does the environment ship with this batch? That answer decides whether
   there is a `tasks/` directory at all, and it is upstream of every other choice here. Record it, with
   its reason, before anything else — reversing it later means rebuilding the tree.
1. **Inventory + tier** — the delivery-format spec and the acceptance gates. Write down the tier per task before touching files.
2. **Quarantine** — move failing tasks to `_staging/<task_name>/` with a one-line `WHY.md`.
3. **Fix the three contracts** — the hint-validation method. Recompute every `[rollout_baseline]` from the corrected `reward.json`
   files; never hand-edit the numbers.
4. **Generate the payload** — `task.json` per rollout from the platform record + the four added fields;
   `docs/<task_name>/metadata.json` from the same record.
5. **Structure** — moves and renames per the difficulty criteria.
6. **`edit_history/`** — `v0/task/` is a full snapshot, not a diff. Every later version gets `task/`,
   `review/` (what triggered the revision — auto-QA output, reviewer note, LLM-judge report) and
   `v{n-1}_v{n}_patch.diff`. Bare `.diff` files with no snapshot and no review do not satisfy the provenance requirement.
7. **Quality harness** — run the the provenance requirement gates; write `QUALITY.md` from the results.
8. **Documents** — `docs/README.md` (the failure-analysis requirement) last among the docs, since it summarises everything above.
   `RUNNING.md` and `GYM_SCOPE.md` describe the fixed state, so write them after 1–7, not before.
9. **Hygiene + manifest**:

```bash
find "$PACK" \( -name '.DS_Store' -o -name '._*' \) -delete
rm -rf "$PACK/__MACOSX"
python3 $SKILL/scripts/validate.py "$PACK" --write-manifest
cd "$(dirname "$PACK")" && zip -r "$(basename "$PACK").zip" "$(basename "$PACK")" \
  -x '*.DS_Store' -x '__MACOSX/*' -x '._*'
```

10. **Cut Contract B** as a selection, never a rebuild:

```bash
mkdir -p b_zip && cp -R "$PACK/environment" b_zip/ \
  && cp "$PACK/docs/"{RUNNING.md,GYM_SCOPE.md} b_zip/ \
  && rsync -a --include='*/' --include='task.json' --exclude='*' "$PACK/trajectory/" b_zip/trajectory/
```

---

## 8. Defects to check for every time

Each of these has been found in a real shipped or near-shipped pack. The validator catches all of them.

- Forbidden milestone fired, reward not zeroed → the difficulty criteria numbers wrong, the reward-hacking requirement defect.
- `[rollout_baseline]` declared with `rollout_n = N` and no trajectory directory.
- Films shot on a superseded brief attached to a rebriefed `instruction.md`.
- `solve.sh` that writes an outcome file and never touches the UI → hollow G1.
- `score.py` reading an agent-writable path → live reward-hack surface.
- No `frozen_time`, while the brief turns on "Friday" or a dated calendar file → silent rot.
- `edit_history/` absent, or present as bare `.diff` files with no snapshot and no `review/`.
- Per-task docs duplicated at every root because the batch was shipped one-task-per-folder.
- `docs/README.md` referencing a layout doc or validator that is not in the pack.
- Gym-persona email in `task.toml` `authors`.
- `.DS_Store` / `__MACOSX` / `._*` inside the tree.
- `MANIFEST.json` stale after edits.
- `vein` / `specific_failure` null everywhere while the docs assert a failure distribution.
- Oracle rollouts counted in the pass@k denominator.
- A `tasks/` directory shipped with a stub image that cannot host the task — G1 then tests the pack's own
  JSON, and a buyer who runs an agent there gets a number that is not comparable to the films.
- A 0-resolve task shipped with an oracle and no hint rerun, described as "non-broken" — the difficulty criteria's clause asks
  for an *agent* surpassing the failure on a partial hint, which an oracle does not evidence.
- A required milestone with no artifact carrying its fact — build the provenance table and the gap is
  obvious; skip it and the task is unsolvable in a way no gate detects.
- Seed artifacts left on a superseded persona set after a brief rebase: the brief says one vendor and the
  workspace mail says another. Solvable, but the environment contradicts the instruction (the per-component quality standards).
- A scorer that reads no seed state at all: if `score.py` opens only the file `solve.sh` wrote, the loop is
  closed and neither G1 nor G2 carries information about the environment. Grep the scorer for the workspace
  or state path; if it never opens one, say so in `environment/README.md`.

## Locating the validator

The validator ships with the skill. It may be installed at the user level (`~/.claude/skills/...`) or committed into a repo (`.claude/skills/...`), so locate it rather than hardcoding one:

```bash
VALIDATOR=$(ls .claude/skills/browser-task-packaging/scripts/validate.py \
              ~/.claude/skills/browser-task-packaging/scripts/validate.py 2>/dev/null | head -1)
python3 "$VALIDATOR" <pack>
```
