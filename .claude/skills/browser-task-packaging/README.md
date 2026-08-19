# browser-task-packaging

A Claude Code skill plus agent fleet for packaging browser-gym tasks into an auditable deliverable — and
for refusing to make claims the artifacts do not support.

Generic by design: buyer-specific thresholds, task briefs, personas and oracle ids are not included. Set
`PACKAGING_PASS_CAP` for your own difficulty cap.

## Install

Per repo, so everyone who pulls gets it:

```bash
mkdir -p .claude/skills .claude/agents
cp -R browser-task-packaging .claude/skills/
cp browser-task-packaging/agents/*.md .claude/agents/
```

Or per user, for every project: the same two copies into `~/.claude/`.

## Use

```bash
python3 .claude/skills/browser-task-packaging/scripts/validate.py <pack_dir>
python3 .claude/skills/browser-task-packaging/scripts/validate.py <pack_dir> --write-manifest   # LAST
```

Exit 0 means no FAILs. Read every WARN — `DECLARED LIMITATION` lines are gaps someone chose to ship and
documented; a bare WARN usually is not.

Then describe the job (`Package the tasks in ./my-pack`), or name a specialist
(`Use packaging-triage on ./my-pack`), or read the method with `/browser-task-packaging`.

## What is inside

| | |
|---|---|
| `SKILL.md` | the method: evidence tiers · the tree · the reward-veto rule · quality gates · solvability · the report · build order · the defect list |
| `scripts/validate.py` | the gate report. Stdlib only |
| `templates/` | root report · `task.toml` · `task.json` |
| `agents/` | one orchestrator plus six specialists: triage, verifiers, solvability, failure modes, docs, QA |

## The four things people get wrong

1. **A claim with no artifact behind it** — a declared rollout baseline with no trajectories, a
   "non-broken" label with no hint rerun, a token count that was never measured.
2. **A veto that is not in the scalar** — difficulty statistics read `reward.json["reward"]`, so a
   forbidden check firing has to zero it, not discount it.
3. **An oracle that proves nothing** — a script writing a gold state passes by construction. For a browser
   task the oracle is the recorded run that drove the real UI.
4. **A task directory whose image cannot host the task** — an agent placed there cannot attempt the brief,
   and a gate passing there describes your scorer, not your task.
