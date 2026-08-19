# <dataset>-<batch>-<num>

<One line: what this batch is, which gym, how many tasks.>
Layout: harbor-style `<dataset>-<batch>-<num>/` per the delivery spec the delivery-format spec.
**Evidence tier:** <from SKILL the acceptance gates — e.g. "full the delivery-format spec pack; G1 evidenced by <harness> oracle films">

## Scorecard

| Slug | Gym id | Frontier n-seed (per-seed scores) | Oracle (G1) | Confirmation |
|---|---|---|---|---|
| `<slug>` | `<gym/id>` | 5/5 BREAK — 0.65×4, 0.50×1 | film `<id>` HOLD 1.00 | **confirmed-confirmed** |
| `<slug>` | `<gym/id>` | 5/5 BREAK — 0.88×2, 0.42×3 | film `<id>` HOLD 1.00 | **confirmed-confirmed** |

**Confirmed-confirmed** here means: current-brief frontier n/n BREAK **and** a live oracle HOLD whose
stored `task_brief` equals this pack's `instruction.md`. That is `<list the tasks that meet it>` only.
<Define whatever strength term you used. An undefined strength claim reads as marketing.>

Reward semantics: `reward` is `0.0` whenever any forbidden milestone fires, otherwise the sum of fired
required-milestone weights. `pass_rate` is computed on that scalar with oracle rollouts **excluded from the
denominator** — oracle films under `trajectory/<task>/oracle/` are gate evidence, not rollouts.

## What is deferred, and the size of the gap

- **Hy3 pass@16** — <not run / no endpoint>. The the difficulty criteria baseline floor is not evidenced in this batch.
- **20+ trajectories per task** — this batch ships <N> frontier films per task plus <M> oracle films.
- **Hint reruns (the hint-validation method)** — <N of M> run. Labels on the remainder are **unverified** by the spec's own
  definition; hint text is drafted and included, the rerun is not.
- **Cost / token means** — <measured / not recaptured>. Where absent, the fields are zero rather than
  estimated.

<Name every cap. the reward-hacking requirement requires disclosure of any coverage limit; silent truncation reads as full coverage.>

## Provenance that is **not** this pack's claim

- `<older brief / adjacent film set>` — <why it is excluded: superseded wording, different persona, etc.>
- `<neighbouring delivery folder>` — <what it contains and why it is not evidence here>

<Write this block even when nothing forces you to. It is what distinguishes a careful vendor.>

## Excluded from this batch

| Task | Why excluded | Route back in |
|---|---|---|
| `<slug>` | <the difficulty criteria: recomputed pass@5 = 0.60 against a 0.40 cap> | <veto fix, re-film 5 seeds, attach matching-brief oracle> |
| `<slug>` | <the delivery-format spec: `rollout_n = 5` declared, no trajectory directory> | <5 films + 1 oracle on the shipped brief> |

## Gates

| Gate | Result | How it was evaluated |
|---|---|---|
| G0 buildable | pass | `tasks/<t>/environment/Dockerfile` builds from delivered files. <Scope limit, if any.> |
| G1 oracle = 1.0 | pass | <Playwright `solution/solve.sh` drives the gold path / evidenced by film `<id>`.> |
| G2 nop = 0.0 | pass | Empty episode → `0.0`; every task has ≥1 positive required milestone the seed does not satisfy. |
| the difficulty criteria difficulty | pass | Recomputed post-veto `pass_rate` per task, in the scorecard above. |

<If the live browser stack is not shipped, say so here and scope the gates explicitly.>

## Quality

Gates run and passed: near-duplicate cosine, spell/grammar lint, verifier-leakage check, determinism,
isolation probe, golden-pair + veto + nop unit tests.

Judges (reported, not gating): instruction↔verifier correspondence MCC <x> [CI], realism MCC <x> [CI],
both on n=<N>. <Any judge below its usable threshold routes to HITL rather than blocking.>

Diversity: <cells occupied>/<cells defined> over `applications × workflows × forbidden-class`.
<If the vein axis is unpopulated in the trajectories, say so and do not assert a the failure-analysis requirement distribution.>

## Validate

```bash
python3 ~/.claude/skills/browser-task-packaging/scripts/validate.py <dataset>-<batch>-<num>
```

<Only reference commands and files that actually ship or actually exist on the reader's side.>
