#!/usr/bin/env python3
"""Validate a browser-task delivery pack against the harbor-style the delivery-format spec layout.

Usage:
    python3 validate.py <pack_dir> [--write-manifest] [--json]

Exit code 0 = no FAILs (WARNs allowed), 1 = at least one FAIL, 2 = usage error.

Every check here corresponds to a defect found in a real pack. Nothing is inferred:
a check that cannot be evaluated from the delivered files reports SKIP, not PASS.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import json
import re
import sys
import tomllib
from collections import Counter
from pathlib import Path

PASS_CAP = float(os.environ.get("PACKAGING_PASS_CAP", "0.50"))  # buyer-specific; override via env
DUP_COSINE = 0.90        # near-duplicate prompt gate (informational here)
HYGIENE = (".DS_Store", "__MACOSX")
DATASET_DOCS = ("README.md", "failure_analysis_metadata.md", "reward_hacking.md")
# browser-native shape (default) vs SWE-compat shim shape
NATIVE_DIR, COMPAT_DIR = "task_definition", "tasks"
NATIVE_REQUIRED = ("task.toml", "instruction.md", "rubric.json")
COMPAT_REQUIRED = ("task.toml", "instruction.md", "tests/test.sh", "environment/Dockerfile")
DOC_REQUIRED = ("README.md", "report.md", "context_info.md")
PERSONA_HINTS = ("<gym-persona>", "<gym-persona>", "xmail.com", "example.com")


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, level: str, check: str, detail: str) -> None:
        self.rows.append((level, check, detail))

    def fails(self) -> int:
        return sum(1 for lv, _, _ in self.rows if lv == "FAIL")

    def render(self) -> str:
        order = {"FAIL": 0, "WARN": 1, "SKIP": 2, "PASS": 3}
        icon = {"FAIL": "✗", "WARN": "!", "SKIP": "–", "PASS": "✓"}
        out = []
        for lv, check, detail in sorted(self.rows, key=lambda r: (order[r[0]], r[1])):
            out.append(f"{icon[lv]} {lv:<4} {check:<34} {detail}")
        c = Counter(lv for lv, _, _ in self.rows)
        out.append("")
        out.append(f"  {c['PASS']} pass · {c['WARN']} warn · {c['SKIP']} skip · {c['FAIL']} FAIL")
        return "\n".join(out)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def walk_files(root: Path, skip_hygiene: bool = True) -> list[Path]:
    out = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        parts = set(rel.parts)
        if skip_hygiene and (parts & set(HYGIENE) or rel.name.startswith("._")
                             or rel.name == ".DS_Store"):
            continue
        if rel.parts and rel.parts[0] == "_staging":
            continue
        out.append(p)
    return out


def load_toml(p: Path) -> dict:
    try:
        with p.open("rb") as fh:
            return tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001 - report, don't crash the run
        return {"__error__": str(exc)}


# ───────────────────────── checks ─────────────────────────

def check_hygiene(root: Path, r: Report) -> None:
    junk = [p for p in root.rglob("*")
            if p.name == ".DS_Store" or p.name.startswith("._") or p.name == "__MACOSX"]
    if junk:
        r.add("FAIL", "hygiene", f"{len(junk)} junk entries in tree (e.g. {junk[0].relative_to(root)})")
    else:
        r.add("PASS", "hygiene", "no .DS_Store / __MACOSX / ._* in tree")


def check_manifest(root: Path, r: Report, write: bool) -> None:
    mf = root / "MANIFEST.json"
    disk = {str(p.relative_to(root)) for p in walk_files(root)} - {"MANIFEST.json"}
    if write:
        payload = {
            "schema_version": "browser-task-manifest-v1",
            "package": root.name,
            "layout": "browser-task-pack-v1",
            "tasks": sorted(p.name for p in (root / "tasks").iterdir() if p.is_dir())
            if (root / "tasks").is_dir() else [],
            "hygiene": {"exclude": ["__MACOSX", ".DS_Store", "._*"]},
            "files": [{"path": f, "sha256": sha256(root / f)} for f in sorted(disk)],
        }
        mf.write_text(json.dumps(payload, indent=2) + "\n")
        r.add("PASS", "manifest", f"regenerated for {len(disk)} files")
        return
    if not mf.exists():
        r.add("WARN", "manifest", "no MANIFEST.json (optional, but it is the cheapest integrity signal)")
        return
    m = json.loads(mf.read_text())
    listed = {f["path"] for f in m.get("files", [])}
    missing, extra = sorted(listed - disk), sorted(disk - listed)
    bad = [f["path"] for f in m.get("files", [])
           if f["path"] in disk and f.get("sha256") and sha256(root / f["path"]) != f["sha256"]]
    if missing or extra or bad:
        detail = []
        if missing:
            detail.append(f"{len(missing)} listed-but-absent ({missing[0]})")
        if extra:
            detail.append(f"{len(extra)} unlisted ({extra[0]})")
        if bad:
            detail.append(f"{len(bad)} sha mismatch ({bad[0]})")
        r.add("FAIL", "manifest", "; ".join(detail) + " — regenerate LAST, after all edits")
    else:
        r.add("PASS", "manifest", f"{len(listed)}/{len(listed)} files present, all sha256 verify")


def check_dataset_docs(root: Path, r: Report) -> None:
    docs = root / "docs"
    if not docs.is_dir():
        r.add("FAIL", "docs/", "missing — the delivery-format spec requires all documentation under docs/")
        return
    for name in DATASET_DOCS:
        if (docs / name).exists():
            r.add("PASS", f"docs/{name}", "present")
        else:
            r.add("FAIL", f"docs/{name}", "missing — the delivery-format spec dataset-level requirement")
    # dangling references inside the root report
    readme = docs / "README.md"
    if readme.exists():
        text = readme.read_text(errors="replace")
        refs = set(re.findall(r"`([A-Za-z0-9_./~-]+\.(?:md|py|json|toml))`", text))
        def resolvable(ref: str) -> bool:
            if ref.startswith(("/", "~")):        # runtime path or a path on our machine, not a pack file
                return True
            if (root / ref).exists() or (docs / ref).exists():
                return True
            return any(root.rglob(Path(ref).name))   # exists somewhere in the tree
        dangling = sorted(ref for ref in refs if "/" in ref and not resolvable(ref))
        if dangling:
            r.add("FAIL", "docs/README refs", f"points at files not in the pack: {', '.join(dangling[:3])}")
        else:
            r.add("PASS", "docs/README refs", "every referenced path exists in the pack")
        for aside in ("does not overwrite", "do not overwrite", "TODO:", "FIXME", "XXX:",
                      "note to self", "~/.claude", "~/Downloads"):
            if aside.lower() in text.lower():
                r.add("WARN", "docs/README asides", f"internal aside in a buyer-facing doc: {aside!r}")
                break


def rollout_dirs(task_traj: Path) -> list[Path]:
    return sorted(p for p in task_traj.glob("*/rollout_*") if p.is_dir())


def load_waivers(root: Path) -> dict[str, str]:
    """docs/scope_limitations.json downgrades a FAIL to a WARN that prints the stated reason.

    A waiver must name the check id and give a reason; the reason is echoed verbatim so the
    limitation stays visible in the report rather than disappearing from it.
    """
    p = root / "docs" / "scope_limitations.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}
    return {k: v.get("reason", "(no reason given)") for k, v in raw.items() if isinstance(v, dict)}


def emit(r: Report, waivers: dict[str, str], check_id: str, check: str, detail: str) -> None:
    if check_id in waivers:
        r.add("WARN", check, f"DECLARED LIMITATION — {detail} · stated reason: {waivers[check_id]}")
    else:
        r.add("FAIL", check, detail)


def check_task(root: Path, slug: str, r: Report, waivers: dict[str, str] | None = None,
               task_dir: str = COMPAT_DIR) -> None:
    waivers = waivers or {}
    native = task_dir == NATIVE_DIR
    tdir = root / task_dir / slug
    for rel in (NATIVE_REQUIRED if native else COMPAT_REQUIRED):
        if not (tdir / rel).exists():
            r.add("FAIL", f"{slug}: {rel}", "missing")
    if native:
        # the oracle is a film, and the scorer is a CLI — neither belongs here
        strays = [d for d in ("solution", "tests") if (tdir / d).exists()]
        if strays:
            r.add("FAIL", f"{slug}: {strays}", "SWE entry points inside task_definition/ — the oracle is "
                                               "the film under trajectory/<task>/oracle/ and the scorer "
                                               "lives in verifier/")
        else:
            r.add("PASS", f"{slug}: shape", "browser-native — no solution/ or tests/")
        if not (tdir / "seed").exists():
            r.add("WARN", f"{slug}: seed/", "no seed/ — without a t=0 snapshot the task cannot be rebuilt")
        cfg = load_toml(tdir / "task.toml") if (tdir / "task.toml").exists() else {}
        check_task_config(root, slug, tdir, cfg, r)   # frozen_time, memory, authors
        check_rubric(root, slug, tdir, r)             # leakage + the nop floor
        check_solvability(root, slug, r)
        check_trajectories(root, slug, cfg, r)
        check_task_docs(root, slug, r)
        return
    solve = tdir / "solution" / "solve.sh"
    if not solve.exists():
        r.add("WARN", f"{slug}: solve.sh", "absent — correct for the browser-native shape; if this is the "
                                           "SWE-compat shim it should be a one-line exec of oracle.py")
    else:
        body = solve.read_text(errors="replace")
        drives_ui = bool(re.search(r"playwright|selenium|:3000|localhost|https?://", body, re.I))
        writes_outcome = "outcome.json" in body
        if writes_outcome and not drives_ui:
            emit(r, waivers, "oracle.state_write_only", f"{slug}: solve.sh",
                 "writes an outcome file and never touches the UI — hollow G1")
        elif drives_ui:
            r.add("PASS", f"{slug}: solve.sh", "drives the UI")

    # ground truth must not come from an agent-writable path
    score = tdir / "tests" / "score.py"
    if score.exists():
        s = score.read_text(errors="replace")
        if "/logs/agent" in s or "agent\" / \"outcome" in s or "agent'/'outcome" in s:
            emit(r, waivers, "verifier.agent_writable_path", f"{slug}: score.py",
                 "reads /logs/agent/* — the agent can write that path")

    cfg = load_toml(tdir / "task.toml") if (tdir / "task.toml").exists() else {}
    if "__error__" in cfg:
        r.add("FAIL", f"{slug}: task.toml", f"unparseable: {cfg['__error__']}")
        cfg = {}

    check_task_config(root, slug, tdir, cfg, r)
    check_rubric(root, slug, tdir, r)
    check_task_docs(root, slug, r)
    check_solvability(root, slug, r)
    check_trajectories(root, slug, cfg, r)


def check_task_config(root: Path, slug: str, tdir: Path, cfg: dict, r: Report) -> None:
    browser = cfg.get("browser", {})
    if not (browser.get("frozen_time") or cfg.get("task", {}).get("frozen_time")):
        r.add("WARN", f"{slug}: frozen_time", "not declared — time-sensitive briefs rot silently")
    mem = cfg.get("environment", {}).get("memory_mb")
    if mem is not None and not isinstance(mem, int):
        r.add("WARN", f"{slug}: memory_mb", f"{mem!r} is not an integer MB (the per-component quality standards config lint)")
    for a in cfg.get("task", {}).get("authors", []) or []:
        email = (a.get("email") or "").lower()
        if any(h in email for h in PERSONA_HINTS):
            r.add("WARN", f"{slug}: authors", f"{email} looks like a gym persona, not an author")

def check_rubric(root: Path, slug: str, tdir: Path, r: Report) -> None:
    # instruction ↔ verifier leakage
    instr_p, rubric_p = tdir / "instruction.md", tdir / "rubric.json"
    if instr_p.exists() and rubric_p.exists():
        instr = instr_p.read_text(errors="replace").lower()
        try:
            rubric = json.loads(rubric_p.read_text())
        except Exception:  # noqa: BLE001
            rubric = {}
        ids = [m.get("id", "") for m in rubric.get("milestones", [])]
        leaked = [i for i in ids if i and i.replace("_", " ") in instr or (i and i in instr)]
        if leaked:
            r.add("FAIL", f"{slug}: leakage", f"instruction contains milestone id(s): {leaked[:2]}")
        else:
            r.add("PASS", f"{slug}: leakage", "no milestone ids in the brief")
        # G2 precondition. Counting positive required milestones is NOT enough: one the SEED already
        # satisfies is banked by a do-nothing agent and contributes to a nop floor. fired_at_step == 0
        # in the shipped rollouts is the evidence — read it rather than assuming.
        positives = [m for m in rubric.get("milestones", [])
                     if m.get("required") and not m.get("forbidden")]
        if not positives:
            r.add("FAIL", f"{slug}: nop gate", "no positive required milestone — empty episode can score 1.0")
        else:
            seed_sat, floor = set(), 0.0
            tdir = root / "trajectory" / slug
            if tdir.is_dir():
                for rj in tdir.rglob("reward.json"):
                    try:
                        ms = (json.loads(rj.read_text()).get("verifier_result") or {}).get(
                            "all_milestones", [])
                    except Exception:  # noqa: BLE001
                        continue
                    for m in ms:
                        if (m.get("required") and not m.get("forbidden")
                                and int(m.get("fired_at_step", -1)) == 0
                                and m.get("name") not in seed_sat):
                            seed_sat.add(m.get("name"))
                            floor += float(m.get("weight", 0.0))
            thr = float(rubric.get("pass_threshold", 1.0))
            genuine = [m for m in positives if m.get("id") not in seed_sat]
            if seed_sat and not genuine:
                r.add("FAIL", f"{slug}: nop gate", f"every positive required milestone fires at seed "
                                                   f"({sorted(seed_sat)}) — a nop scores {floor:.2f}")
            elif seed_sat:
                r.add("WARN", f"{slug}: nop floor",
                      f"{sorted(seed_sat)} fire at step 0, so a do-nothing agent banks {floor:.2f} of "
                      f"{thr:.2f}. G2 holds only because {len(genuine)} genuine positive(s) remain — "
                      "state the floor in the report; do not claim the seed satisfies nothing")
            else:
                r.add("PASS", f"{slug}: nop gate",
                      f"{len(positives)} positive required milestone(s), none satisfied at seed")

def check_task_docs(root: Path, slug: str, r: Report) -> None:
    ddir = root / "docs" / slug
    for name in DOC_REQUIRED:
        if not (ddir / name).exists():
            r.add("FAIL", f"{slug}: docs/{name}", "missing (the provenance requirement/the reporting requirement)")
    eh = ddir / "edit_history"
    if not eh.is_dir():
        r.add("FAIL", f"{slug}: edit_history", "absent — the provenance requirement requires v0..vn")
    elif not (eh / "v0" / "task").is_dir():
        r.add("FAIL", f"{slug}: edit_history", "v0/task/ snapshot missing; bare .diff files do not satisfy the provenance requirement")
    else:
        later = sorted(p for p in eh.glob("v[1-9]*") if p.is_dir())
        thin = [p.name for p in later if not (p / "review").is_dir() or not (p / "task").is_dir()]
        if thin:
            r.add("WARN", f"{slug}: edit_history", f"{thin} lack task/ or review/")
        else:
            r.add("PASS", f"{slug}: edit_history", f"v0 + {len(later)} revision(s) with task/ and review/")

def check_solvability(root: Path, slug: str, r: Report) -> None:
    """section 4.4: solvability is evidence, not a belief. A 0-resolve task needs S2 AND S4."""
    sp = root / "docs" / slug / "solvability.json"
    if not sp.exists():
        r.add("FAIL", f"{slug}: solvability", "no docs/<task>/solvability.json — state the tier, the "
                                              "resolve count and the per-milestone information provenance")
        return
    try:
        d = json.loads(sp.read_text())
    except Exception as exc:  # noqa: BLE001
        r.add("FAIL", f"{slug}: solvability", f"unparseable: {exc}")
        return

    tier = d.get("tier")
    if tier not in {"S0", "S1", "S2", "S3", "S4"}:
        r.add("FAIL", f"{slug}: solvability", f"tier {tier!r} is not one of S0..S4")
    elif tier in {"S0", "S1"}:
        r.add("FAIL", f"{slug}: solvability", f"tier {tier} — a scorer accepting our own gold state is "
                                              "not evidence about the task; not shippable")
    else:
        r.add("PASS", f"{slug}: solvability tier", f"{tier}")

    # provenance: every row must name a file that exists in the pack
    prov = d.get("information_provenance") or []
    rub = root / "tasks" / slug / "rubric.json"
    if not prov:
        r.add("FAIL", f"{slug}: provenance", "information_provenance empty — every required milestone "
                                             "must name the artifact carrying its fact")
    else:
        missing_files = [p_["carried_by"] for p_ in prov
                         if p_.get("carried_by") and not p_["carried_by"].startswith("(")
                         and not (root / p_["carried_by"]).exists()]
        if missing_files:
            r.add("FAIL", f"{slug}: provenance", f"names artifacts absent from the pack: {missing_files[:2]}")
        else:
            r.add("PASS", f"{slug}: provenance", f"{len(prov)} milestone fact(s) traced to shipped artifacts")
        if rub.exists():
            try:
                required = {m["id"] for m in json.loads(rub.read_text()).get("milestones", [])
                            if m.get("required")}
            except Exception:  # noqa: BLE001
                required = set()
            covered = {p_.get("milestone") for p_ in prov}
            gap = sorted(required - covered)
            if gap:
                r.add("FAIL", f"{slug}: provenance", f"required milestone(s) with no fact provenance: {gap}")

    # the difficulty criteria non-brokenness clause for a 0-resolve task
    rc = d.get("resolve_count") or {}
    hint = d.get("hint_demonstration") or {}
    zero_resolve = rc.get("frontier") == 0 and not rc.get("hy3")
    s4a = hint.get("non_brokenness_s4a") or {}
    s4b = hint.get("label_validation_s4b") or {}
    if zero_resolve:
        if s4a.get("status") == "passed":
            r.add("PASS", f"{slug}: the difficulty criteria non-brokenness",
                  f"0-resolve task with a passing hinted rerun (S4a, runner {s4a.get('runner')})")
        else:
            r.add("WARN", f"{slug}: the difficulty criteria non-brokenness",
                  "0-resolve task, S4a status=%r — the difficulty criteria asks for an AGENT surpassing the failure on a "
                  "partial hint; an oracle film evidences G1 only. Declare this gap in docs/README.md"
                  % s4a.get("status", "absent"))
    # the two purposes take different runners; conflating them makes the claim unfalsifiable
    if s4b.get("status") == "passed":
        runner = str(s4b.get("runner", ""))
        models = set()
        tj = root / "trajectory" / slug
        if tj.is_dir():
            models = {p_.name for p_ in tj.iterdir() if p_.is_dir() and "oracle" not in p_.name.lower()}
        if models and not any(m.split("__")[-1].split("_")[0] in runner for m in models):
            r.add("FAIL", f"{slug}: S4b label validation",
                  f"runner {runner!r} does not match the failing film model(s) {sorted(models)} — a "
                  "causal label needs the hint as the ONLY changed variable")
    if s4a.get("status") == "passed" and s4b.get("status") != "passed":
        r.add("WARN", f"{slug}: solvability", "S4a passed but S4b did not run — non-brokenness is "
                                              "evidenced, the failure LABELS remain unverified")
    probe = d.get("upper_bound_probe_s3") or {}
    if probe.get("status") == "passed" and probe.get("counts_toward_pass_at_5"):
        r.add("FAIL", f"{slug}: solvability", "upper-bound probe marked as counting toward pass@5 — "
                                              "a non-the difficulty criteria model may not enter that denominator")
    if d.get("designed_infeasibility") and not all(
            x.get("gold_resolution") for x in d["designed_infeasibility"]):
        r.add("FAIL", f"{slug}: solvability", "designed_infeasibility entry without a gold_resolution — "
                                              "an infeasible requirement with no resolution is a broken task")


def check_trajectories(root: Path, slug: str, cfg: dict, r: Report) -> None:
    tj = root / "trajectory" / slug
    baselines = {k: v for k, v in cfg.items() if k.startswith("rollout_baseline")}
    if isinstance(cfg.get("rollout_baseline"), dict):
        baselines = {f"rollout_baseline.{k}": v for k, v in cfg["rollout_baseline"].items()}

    if not tj.is_dir():
        if baselines:
            declared = sum(int(v.get("rollout_n", 0)) for v in baselines.values() if isinstance(v, dict))
            r.add("FAIL", f"{slug}: trajectory/",
                  f"absent while task.toml declares rollout_n={declared} — the delivery-format spec requires trajectory/")
        else:
            r.add("FAIL", f"{slug}: trajectory/", "absent — the delivery-format spec requires it")
        return

    rollouts = rollout_dirs(tj)
    oracle = [p for p in rollouts if "oracle" in p.parts[-2].lower() or "oracle" in p.name.lower()]
    scored = [p for p in rollouts if p not in oracle]
    if not scored:
        r.add("FAIL", f"{slug}: trajectory/", "no non-oracle rollouts")
        return

    # model dir must not carry the harness
    for mdir in {p.parent for p in scored}:
        rn = mdir.name
        harnesses = {p.name.split("rollout_", 1)[1].rsplit("_", 1)[0] for p in mdir.glob("rollout_*")}
        if any(h.split("@")[0] and h.split("@")[0] in rn for h in harnesses):
            r.add("WARN", f"{slug}: model dir", f"{rn!r} embeds the harness; <model_name> should be the model")

    rewards, veto_violations, brief_mismatch = [], [], []
    for p in scored:
        rj = p / "reward.json"
        if not rj.exists():
            r.add("FAIL", f"{slug}: {p.name}", "no reward.json")
            continue
        d = json.loads(rj.read_text())
        ms = (d.get("verifier_result") or {}).get("all_milestones", []) or []
        fired = [m.get("name") for m in ms
                 if m.get("forbidden") and int(m.get("fired_at_step", -1)) >= 0]
        # Apply the veto when recomputing, so auditing a pack whose rewards predate the fix
        # yields the post-veto number rather than repeating the stored pre-veto one.
        rewards.append(0.0 if fired else float(d.get("reward", 0.0)))
        if fired and float(d.get("reward", 0.0)) != 0.0:
            veto_violations.append((p.name, d.get("reward"), fired[0]))
        # film brief == shipped instruction
        tjson = p / "trajectory.json"
        instr_p = root / "tasks" / slug / "instruction.md"
        if tjson.exists() and instr_p.exists():
            try:
                steps = json.loads(tjson.read_text()).get("steps", [])
                first = next((s.get("message", "") for s in steps if s.get("source") == "user"), None)
                if first and first.strip() != instr_p.read_text().strip():
                    brief_mismatch.append(p.name)
            except Exception:  # noqa: BLE001
                pass

    if veto_violations:
        n, rew, ms_name = veto_violations[0]
        r.add("FAIL", f"{slug}: veto rule",
              f"{len(veto_violations)} film(s) fired a forbidden milestone with reward != 0 "
              f"(e.g. {n} reward={rew} on {ms_name})")
    else:
        r.add("PASS", f"{slug}: veto rule", "no forbidden milestone fired with a non-zero reward")

    if brief_mismatch:
        r.add("FAIL", f"{slug}: film brief", f"{len(brief_mismatch)} film(s) differ from instruction.md "
                                             f"(e.g. {brief_mismatch[0]}) — the per-component quality standards mismatch")
    else:
        r.add("PASS", f"{slug}: film brief", "film user-message matches instruction.md")

    for name, v in baselines.items():
        if not isinstance(v, dict):
            continue
        declared_n = int(v.get("rollout_n", 0) or 0)
        if declared_n and declared_n != len(scored):
            r.add("FAIL", f"{slug}: {name}", f"rollout_n={declared_n} but {len(scored)} rollout dirs on disk")
        thr = 1.0
        rub = root / "tasks" / slug / "rubric.json"
        if rub.exists():
            try:
                thr = float(json.loads(rub.read_text()).get("pass_threshold", 1.0))
            except Exception:  # noqa: BLE001
                pass
        actual = sum(1 for x in rewards if x >= thr) / len(rewards)
        declared_rate = v.get("pass_rate")
        if declared_rate is not None and abs(float(declared_rate) - actual) > 1e-6:
            r.add("FAIL", f"{slug}: {name}", f"pass_rate declared {declared_rate}, recomputed {actual:.2f}")
        if actual > PASS_CAP:
            r.add("FAIL", f"{slug}: the difficulty criteria difficulty", f"pass@{len(rewards)} = {actual:.2f} exceeds the "
                                                    f"{PASS_CAP:.2f} cap — quarantine or re-film")
        else:
            r.add("PASS", f"{slug}: the difficulty criteria difficulty", f"pass@{len(rewards)} = {actual:.2f}")
        for f in ("mean_cost_usd", "mean_prompt_tokens", "mean_completion_tokens"):
            if f not in v:
                r.add("WARN", f"{slug}: {name}", f"{f} absent (the delivery-format spec names it)")
                break

    # failure taxonomy population — blocks any the failure-analysis requirement distribution claim
    veins = []
    for p in scored + oracle:
        rj = p / "reward.json"
        if rj.exists():
            d = json.loads(rj.read_text())
            veins.append(d.get("vein"))
    if veins and all(v is None for v in veins):
        r.add("WARN", f"{slug}: taxonomy", "vein null in every rollout — do not assert a the failure-analysis requirement distribution")

    for p in scored:
        if not (p / "failure_analysis" / "failure_analysis.md").exists():
            r.add("FAIL", f"{slug}: {p.name}", "no failure_analysis/failure_analysis.md (the failure-analysis requirement)")
            break
        fa = (p / "failure_analysis" / "failure_analysis.md").read_text(errors="replace").lower()
        if "not run" in fa or "unverified" in fa:
            r.add("WARN", f"{slug}: hint validation",
                  "hint rerun not run — labels are unverified by the hint-validation method; state the cap in docs/README.md")
            break


def check_environment(root: Path, r: Report) -> None:
    env = root / "environment"
    if not env.is_dir():
        r.add("WARN", "environment/", "absent — required by the delivery-format spec only if a task needs a private image")
        return
    images = [p for p in env.iterdir() if p.is_dir()]
    if not (env / "README.md").exists():
        r.add("FAIL", "environment/README.md", "missing — must map each image to its tasks")
    if not images:
        r.add("SKIP", "environment/<image>", "no image shipped; scope the gates explicitly in docs/README.md")
    for img in images:
        if not (img / "manifest.md").exists():
            r.add("FAIL", f"environment/{img.name}", "no manifest.md (name:tag, digest, load instructions)")
        elif not any(img.glob("*.tar")) and not (img / "Dockerfile").exists():
            r.add("FAIL", f"environment/{img.name}", "neither an image archive nor a Dockerfile + context")
        else:
            r.add("PASS", f"environment/{img.name}", "artifact + manifest present")


def check_judges(root: Path, vdir: Path, r: Report) -> None:
    """A model-backed check is only shipped if it can be reproduced, or its absence is specified."""
    for suite_p in sorted(vdir.glob("*/suite.json")):
        slug = suite_p.parent.name
        try:
            checks = json.loads(suite_p.read_text()).get("checks", [])
        except Exception:  # noqa: BLE001
            continue
        judged = [c for c in checks if c.get("kind") == "llm_judge"]
        if not judged:
            r.add("PASS", f"{slug}: judge", "no llm_judge checks — nothing to reproduce")
            continue
        jw = sum(c.get("weight", 0.0) for c in judged)
        tw = sum(c.get("weight", 0.0) for c in checks) or 1.0
        jd = suite_p.parent / "judge"
        if not jd.is_dir():
            r.add("FAIL", f"{slug}: judge", f"{len(judged)} llm_judge check(s) carrying {jw:.2f} of "
                                            f"{tw:.2f} weight, and no judge/ package — a buyer cannot "
                                            "reproduce a single verdict")
            continue
        have_prompt = any((jd / n).exists() for n in ("prompt.md", "prompt.txt", "config.json"))
        have_gold = any(jd.glob("gold*"))
        pending = jd / "PENDING.md"
        if have_prompt:
            r.add("PASS", f"{slug}: judge", f"prompt/config shipped for {jw:.2f} of {tw:.2f} weight")
        elif pending.exists():
            r.add("WARN", f"{slug}: judge", f"{jw:.2f} of {tw:.2f} weight is llm_judge with NO shipped "
                                            "adjudicator; absence is specified in judge/PENDING.md — "
                                            "do not describe these checks as reproducible")
        else:
            r.add("FAIL", f"{slug}: judge", f"{jw:.2f} of {tw:.2f} weight is llm_judge with no prompt and "
                                            "no PENDING.md — the gap is neither closed nor declared")
        if have_gold:
            r.add("PASS", f"{slug}: judge gold", "deterministic gold fast path shipped")
        else:
            r.add("WARN", f"{slug}: judge gold", "no gold fast path — if the gym decides gold-first, that "
                                                 "half of the decision is missing from the package")


def check_contract_b(root: Path, r: Report) -> None:
    """The tar+JSON+API contract is a selection from this tree; check its slots exist."""
    have = [p for p in (root / "trajectory").rglob("task.json")] if (root / "trajectory").is_dir() else []
    if have:
        keys = set(json.loads(have[0].read_text()).keys())
        need = {"task", "verifiers", "model_response", "db_diff", "reward"}
        missing = sorted(need - keys)
        if missing:
            r.add("WARN", "task.json", f"{len(have)} file(s) present, missing keys: {missing}")
        else:
            r.add("PASS", "task.json", f"{len(have)} file(s), all contract-B keys present")
        t = json.loads(have[0].read_text()).get("task", {})
        for f in ("applications", "workflows"):
            if not t.get(f):
                r.add("WARN", "task.json", f"task.{f} empty — derive it, do not hand-tag it")
    else:
        r.add("SKIP", "task.json", "none found; required only for the tar+JSON+API contract")
    for name in ("RUNNING.md", "GYM_SCOPE.md"):
        if not (root / "docs" / name).exists():
            r.add("SKIP", f"docs/{name}", "absent; required only for the tar+JSON+API contract")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pack")
    ap.add_argument("--write-manifest", action="store_true",
                    help="regenerate MANIFEST.json from the tree instead of verifying it")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    root = Path(a.pack).expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    r = Report()
    check_hygiene(root, r)
    check_dataset_docs(root, r)
    check_environment(root, r)

    waivers = load_waivers(root)
    native, compat = root / NATIVE_DIR, root / COMPAT_DIR
    if native.is_dir():
        r.add("PASS", "shape", f"browser-native ({NATIVE_DIR}/); oracle is a film, scorer is a CLI")
        for slug in sorted(p.name for p in native.iterdir() if p.is_dir()):
            check_task(root, slug, r, waivers, task_dir=NATIVE_DIR)
        if compat.is_dir():
            r.add("WARN", "shape", "both task_definition/ and tasks/ present — the shim must be a wrapper "
                                   "over the same artifacts, never a second implementation")
        vdir = root / "verifier"
        if not vdir.is_dir():
            r.add("FAIL", "verifier/", "missing — the browser-native shape scores through a CLI, not tests/")
        elif not (vdir / "suite.json").exists() and not any(vdir.glob("*/suite.json")):
            r.add("WARN", "verifier/", "no suite.json — ship the frozen suite as a first-class artifact")
        else:
            r.add("PASS", "verifier/", "frozen suite shipped")
            check_judges(root, vdir, r)
    elif compat.is_dir():
        slugs = sorted(p.name for p in compat.iterdir() if p.is_dir())
        if not slugs:
            r.add("FAIL", "tasks/", "no task directories")
        r.add("WARN", "shape", "SWE-compat (tasks/ with entry points) — confirm the buyer's harness "
                               "actually executes them; the browser-native default is task_definition/")
        for slug in slugs:
            check_task(root, slug, r, waivers, task_dir=COMPAT_DIR)
    else:
        r.add("FAIL", "shape", f"neither {NATIVE_DIR}/ nor {COMPAT_DIR}/ present")

    check_contract_b(root, r)
    check_manifest(root, r, a.write_manifest)   # last: reflects the tree as shipped

    if a.json:
        print(json.dumps([{"level": lv, "check": c, "detail": d} for lv, c, d in r.rows], indent=2))
    else:
        print(f"\n{root.name}\n")
        print(r.render())
    return 1 if r.fails() else 0


if __name__ == "__main__":
    sys.exit(main())
