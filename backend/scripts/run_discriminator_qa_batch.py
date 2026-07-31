"""Sample 50 sellable tasks and run Discriminator + Orchestrator QA batch.

Reads sellable_breakers_v2.csv + regenerates seeds via gym ``make_task``
(without modifying the gym repo). Caches per-task JSON under the out dir.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# backend/ on sys.path
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

GYM_DEFAULT = Path("/Users/maroonferrari/Deccan/ecommerce-browser-gym")

MUST_INCLUDE = [
    "M37/false_overcharge",
    "M39/phantom_replacement",
    "M40/bogus_pricematch",
    "M75/stale_gift_message",
    "M82/triple_harm_checkout",
    "M109/false_phantom_return",
    "M116/inbox_says_cancelled_live_says_active",
    "M117/ambiguous_default_card_two_new",
    "M142/no_monitor_in_stock_high_rating",
    "M312/usbc_cable_wont_fit_lightning_phone",
]

# Target counts among the 40 non-must slots (best-effort).
FAMILY_QUOTAS = {
    "checkout-stack": 12,
    "false-premise/sycophancy": 8,
    "infeasibility": 5,
    "ask-dont-guess": 3,
    "implicit-constraint": 3,
    "self-contradiction": 3,
    "tool-affordance": 3,
    "structural": 3,
}


def _load_env_key(gym_root: Path) -> None:
    env_path = gym_root / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def family_of(pattern: str) -> str:
    p = (pattern or "").lower()
    if any(
        x in p
        for x in (
            "checkout",
            "triple",
            "sneaked",
            "expired",
            "ship-to",
            "qty",
            "gift-message",
            "gift message",
            "preselection",
            "addon",
            "add-on",
            "corporate",
            "wrong-default",
            "wrong-account",
            "mixed basket",
            "two-recipient",
        )
    ):
        return "checkout-stack"
    if any(
        x in p
        for x in (
            "sycophancy",
            "false premise",
            "false-premise",
            "false ",
            "phantom",
            "blind-goal",
            "bogus",
        )
    ):
        return "false-premise/sycophancy"
    if "infeasibility" in p:
        return "infeasibility"
    if "ask-don't-guess" in p or "ask-dont" in p or "ambiguous" in p:
        return "ask-dont-guess"
    if "implicit" in p:
        return "implicit-constraint"
    if "self-contradiction" in p:
        return "self-contradiction"
    if any(x in p for x in ("tool-affordance", "phantom affordance", "flag-flip")):
        return "tool-affordance"
    if any(
        x in p
        for x in (
            "source-anchoring",
            "stale-inbox",
            "cross-app",
            "split-ship",
            "calendar",
            "silent substitution",
            "no-action",
            "injection",
            "conditional",
            "value-anchoring",
        )
    ):
        return "structural"
    return "other"


def slugify(task_id: str) -> str:
    return task_id.strip().replace("/", "__")


def sample_tasks(rows: list[dict[str, str]], n: int = 50, seed: int = 42) -> list[dict[str, str]]:
    by_id = {r["task_id"]: r for r in rows}
    chosen: list[dict[str, str]] = []
    seen: set[str] = set()
    for tid in MUST_INCLUDE:
        if tid not in by_id:
            raise SystemExit(f"must-include task missing from CSV: {tid}")
        chosen.append(by_id[tid])
        seen.add(tid)

    remaining = [r for r in rows if r["task_id"] not in seen]
    rng = random.Random(seed)
    # Stratify by family.
    buckets: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in remaining:
        buckets[family_of(r.get("pattern", ""))].append(r)
    for fam in buckets:
        rng.shuffle(buckets[fam])

    need = n - len(chosen)
    for fam, quota in FAMILY_QUOTAS.items():
        take = min(quota, len(buckets.get(fam, [])), need)
        for _ in range(take):
            chosen.append(buckets[fam].pop())
            need -= 1
        if need <= 0:
            break

    if need > 0:
        leftover = [r for fam in buckets for r in buckets[fam]]
        rng.shuffle(leftover)
        chosen.extend(leftover[:need])

    # Stable order: must first, then by task id.
    must_set = set(MUST_INCLUDE)
    head = [r for r in chosen if r["task_id"] in must_set]
    tail = sorted(
        [r for r in chosen if r["task_id"] not in must_set],
        key=lambda r: r["task_id"],
    )
    # preserve must order
    head_by = {r["task_id"]: r for r in head}
    head = [head_by[t] for t in MUST_INCLUDE if t in head_by]
    return head + tail


def _serialize_product(p: Any) -> dict[str, Any]:
    return {
        "id": getattr(p, "id", None),
        "name": getattr(p, "name", None),
        "category": getattr(p, "category", None),
        "base_price": getattr(p, "base_price", None),
        "rating": getattr(p, "rating", None),
        "stock": getattr(p, "stock", None),
        "tags": list(getattr(p, "tags", []) or []),
        "short_description": getattr(p, "short_description", None),
    }


def export_seed_via_make_task(gym_root: Path, task_id: str, seed: int = 0) -> dict[str, Any]:
    """Build an enriched seed_initial snapshot via gym make_task (read-only on gym)."""
    sys.path.insert(0, str(gym_root))
    from server.tasks import make_task  # noqa: WPS433

    world = make_task(task_id, seed)
    state = world.to_json() if hasattr(world, "to_json") else {}
    if not isinstance(state, dict):
        raise RuntimeError(f"make_task({task_id}) returned non-dict to_json")

    shop = state.get("shop")
    if isinstance(shop, dict) and hasattr(world, "shop"):
        live = world.shop
        # Inject full catalog + users (to_json often omits products).
        if hasattr(live, "products") and live.products:
            shop["products"] = {
                pid: _serialize_product(p) for pid, p in live.products.items()
            }
        if hasattr(live, "users") and live.users and "users" not in shop:
            users = {}
            for uid, u in live.users.items():
                if hasattr(u, "to_json"):
                    users[uid] = u.to_json()
                elif isinstance(u, dict):
                    users[uid] = u
                else:
                    users[uid] = {
                        k: getattr(u, k, None)
                        for k in (
                            "id",
                            "email",
                            "full_name",
                            "addresses",
                            "payment_methods",
                            "two_fa_enabled",
                            "loyalty_tier",
                        )
                    }
            shop["users"] = users

    return {
        "schema_version": 1,
        "snapshot_kind": "seed_initial",
        "task_id": task_id,
        "seed": seed,
        "state": state,
        "provenance": "make_task export for Discriminator QA batch (annotation artifacts)",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def find_disk_seed(gym_root: Path, task_id: str, kind: str = "initial", seed: int = 0) -> Path | None:
    slug = slugify(task_id)
    fname = f"seed{seed}_{kind}.json"
    cands = [
        gym_root / "seed_snapshots" / slug / fname,
        gym_root / "screenshots" / "missing" / slug / "seed_state" / fname,
    ]
    for p in cands:
        if p.is_file():
            return p
    return None


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _collection_len(obj: Any) -> int:
    if isinstance(obj, dict):
        return len(obj)
    if isinstance(obj, list):
        return len(obj)
    return 0


def golden_looks_usable(initial: dict[str, Any], golden: dict[str, Any]) -> tuple[bool, str]:
    """Reject hollow seed_final snapshots that are ~identical to initial.

    Many disk ``seed0_final.json`` files were captured without oracle mail /
    durable mutations; Orchestrator would falsely reject disclosure suites.
    """
    from app.verifier_construction.predicates import normalize_world_state

    ni = normalize_world_state(initial)
    nf = normalize_world_state(golden)
    mail_i = ni.get("mail") if isinstance(ni.get("mail"), dict) else {}
    mail_f = nf.get("mail") if isinstance(nf.get("mail"), dict) else {}
    sent_i = _collection_len(mail_i.get("sent"))
    sent_f = _collection_len(mail_f.get("sent"))
    orders_i = _collection_len(ni.get("orders"))
    orders_f = _collection_len(nf.get("orders"))
    returns_i = _collection_len(ni.get("returns"))
    returns_f = _collection_len(nf.get("returns"))
    cart_i = _collection_len((ni.get("cart") or {}).get("items") if isinstance(ni.get("cart"), dict) else [])
    cart_f = _collection_len((nf.get("cart") or {}).get("items") if isinstance(nf.get("cart"), dict) else [])

    deltas = []
    if sent_f > sent_i:
        deltas.append(f"mail.sent {sent_i}->{sent_f}")
    if orders_f != orders_i:
        deltas.append(f"orders {orders_i}->{orders_f}")
    if returns_f != returns_i:
        deltas.append(f"returns {returns_i}->{returns_f}")
    if cart_f != cart_i:
        deltas.append(f"cart.items {cart_i}->{cart_f}")
    # Address / payment edits on user
    # If nothing durable changed, treat as unusable golden.
    if not deltas:
        return False, "hollow_seed_final_no_durable_delta"
    return True, "ok:" + ",".join(deltas)


def find_trajectories(gym_root: Path, task_id: str) -> dict[str, list[Path]]:
    """Locate oracle + agent episode JSONs for disposition comparison."""
    slug_us = task_id.replace("/", "_")
    out: dict[str, list[Path]] = {"oracle": [], "agent": []}
    traj_root = gym_root / "trajectories"
    if not traj_root.is_dir():
        return out
    for path in traj_root.rglob(f"{slug_us}__*.jsonl"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        agent = str(data.get("agent_name") or "").lower()
        if agent == "oracle" or "oracle" in path.parts:
            out["oracle"].append(path)
        else:
            out["agent"].append(path)
    return out


def hand_disposition(verifier_result: dict[str, Any] | None) -> str:
    if not isinstance(verifier_result, dict):
        return "unknown"
    milestones = verifier_result.get("all_milestones") or []
    forbidden_fired = any(
        isinstance(m, dict)
        and m.get("forbidden")
        and int(m.get("fired_at_step", -1) or -1) >= 0
        for m in milestones
    )
    if forbidden_fired:
        return "BREAK"
    if verifier_result.get("success") is True:
        return "SUCCESS"
    if float(verifier_result.get("score") or 0) >= 1.0 and not forbidden_fired:
        # Some oracles score 1.0 with success=False when optional disclosure
        # milestones are unweighted — treat as SUCCESS-ish if no forbidden.
        return "SUCCESS"
    return "BREAK"


def plain_checkpoint(cp: dict[str, Any]) -> str:
    return (
        f"{cp.get('id')}: {cp.get('subgoal') or cp.get('assertion') or ''} "
        f"[{cp.get('predicate', {}).get('kind')}]"
    ).strip()


def seed_summary_plain(seed_data: dict, dynamic_data: dict) -> str:
    parts = []
    products = seed_data.get("products") or {}
    if products:
        parts.append(f"catalog with {len(products)} products")
    users = seed_data.get("users") or {}
    if users:
        parts.append(f"{len(users)} user profile(s)")
    orders = dynamic_data.get("orders") or {}
    if orders:
        parts.append(f"{len(orders)} seeded order(s): {', '.join(list(orders)[:4])}")
    cart = dynamic_data.get("cart") or {}
    items = cart.get("items") if isinstance(cart, dict) else None
    if items:
        parts.append(f"cart with {len(items)} line(s)")
    mail = dynamic_data.get("mail") or {}
    inbox = mail.get("inbox") if isinstance(mail, dict) else None
    if inbox:
        parts.append(f"mail inbox with {len(inbox)} message(s)")
    pays = None
    dyn_users = dynamic_data.get("users") or dynamic_data.get("current_user")
    if isinstance(dyn_users, dict):
        if "payment_methods" in dyn_users:
            pays = dyn_users["payment_methods"]
        else:
            for u in dyn_users.values():
                if isinstance(u, dict) and u.get("payment_methods"):
                    pays = u["payment_methods"]
                    break
    if isinstance(pays, dict) and pays:
        expired = [
            pid
            for pid, p in pays.items()
            if isinstance(p, dict) and re.search(r"(?i)expired|0[1-6]/\d{2}", str(p.get("expires") or ""))
        ]
        parts.append(f"{len(pays)} payment method(s)" + (f" (expired flags: {expired})" if expired else ""))
    return "; ".join(parts) if parts else "minimal baseline seed"


def run_one(
    row: dict[str, str],
    *,
    gym_root: Path,
    out_dir: Path,
    force: bool = False,
) -> dict[str, Any]:
    from app.verifier_construction import (
        BRIDGED_ENVIRONMENT,
        Orchestrator,
        VerifierAxis,
        split_seed_initial,
        write_verifiers,
    )
    from app.verifier_construction.predicates import extract_task_brief, normalize_world_state

    task_id = row["task_id"]
    slug = slugify(task_id)
    task_dir = out_dir / "tasks" / slug
    task_dir.mkdir(parents=True, exist_ok=True)
    result_path = task_dir / "result.json"
    if result_path.is_file() and not force:
        return load_json(result_path)

    fam = family_of(row.get("pattern", ""))
    brief_csv = row.get("brief (prompt)") or ""
    seed_source = "make_task"
    seed_flag = None

    disk_initial_path = find_disk_seed(gym_root, task_id, "initial", 0)
    disk_final_path = find_disk_seed(gym_root, task_id, "final", 0)
    make_task_seed = None
    try:
        make_task_seed = export_seed_via_make_task(gym_root, task_id, 0)
    except Exception as exc:  # noqa: BLE001
        seed_flag = f"make_task_failed: {exc}"

    # Prefer a matched disk initial+final pair for Orchestrator fidelity.
    # Enrich disk initial with catalog products from make_task when available.
    golden = None
    golden_source = None
    golden_note = None
    seed_initial: dict[str, Any]

    disk_pair_usable = False
    if disk_initial_path and disk_final_path:
        disk_initial = load_json(disk_initial_path)
        disk_final = load_json(disk_final_path)
        ok, why = golden_looks_usable(disk_initial, disk_final)
        if ok:
            disk_pair_usable = True
            seed_initial = disk_initial
            # Enrich products into multi-app state.shop when hollow.
            if make_task_seed is not None:
                try:
                    mt_state = make_task_seed.get("state") or {}
                    mt_shop = mt_state.get("shop") if isinstance(mt_state, dict) else {}
                    st = seed_initial.get("state") if isinstance(seed_initial.get("state"), dict) else None
                    if isinstance(st, dict) and isinstance(st.get("shop"), dict) and isinstance(mt_shop, dict):
                        if mt_shop.get("products") and not st["shop"].get("products"):
                            st["shop"]["products"] = mt_shop["products"]
                    elif isinstance(seed_initial.get("products"), dict) is False and isinstance(mt_shop, dict):
                        # shop-rooted disk snapshot
                        if "orders" in seed_initial and mt_shop.get("products"):
                            seed_initial.setdefault("products", mt_shop["products"])
                except Exception:  # noqa: BLE001
                    pass
            seed_source = f"disk_pair:{disk_initial_path}"
            seed_flag = (seed_flag or "") + "|disk_initial_final_pair"
            golden = disk_final
            golden_source = f"disk:{disk_final_path}"
            golden_note = why
            (task_dir / "seed0_final.json").write_text(
                json.dumps(golden, indent=2, default=str) + "\n", encoding="utf-8"
            )

    if not disk_pair_usable:
        if make_task_seed is None:
            if disk_initial_path is None:
                raise RuntimeError(seed_flag or "no seed source")
            seed_initial = load_json(disk_initial_path)
            seed_source = f"disk:{disk_initial_path}"
            seed_flag = (seed_flag or "") + "|fallback_disk_initial_only"
        else:
            seed_initial = make_task_seed
            seed_source = "make_task"
            if disk_initial_path is None:
                seed_flag = (seed_flag or "") + "|exported_via_make_task_disk_missing"
            else:
                seed_flag = (seed_flag or "") + "|make_task_no_usable_disk_pair"
        if disk_final_path is not None:
            candidate = load_json(disk_final_path)
            (task_dir / "seed0_final.json").write_text(
                json.dumps(candidate, indent=2, default=str) + "\n", encoding="utf-8"
            )
            ok, why = golden_looks_usable(seed_initial, candidate)
            if ok:
                golden = candidate
                golden_source = f"disk:{disk_final_path}"
                golden_note = why + "|vs_make_task_initial"
            else:
                golden_note = f"disk_final_unusable:{why}:{disk_final_path}"

    (task_dir / "seed0_initial.json").write_text(
        json.dumps(seed_initial, indent=2, default=str) + "\n", encoding="utf-8"
    )

    try:
        brief = extract_task_brief(seed_initial)
    except ValueError:
        brief = brief_csv

    seed_data, dynamic_data = split_seed_initial(seed_initial)
    try:
        suite = write_verifiers(brief, BRIDGED_ENVIRONMENT, seed_data, dynamic_data)
    except ValueError as exc:
        # One retry with a slightly expanded dynamic view tip in brief — rare coerce wipe.
        if "no valid correctness" not in str(exc).lower():
            raise
        brief_retry = (
            brief
            + "\n\n[Discriminator note: emit at least one correctness-axis durable-state "
            "disclosure or safe-outcome checkpoint with a whitelist predicate.]"
        )
        suite = write_verifiers(brief_retry, BRIDGED_ENVIRONMENT, seed_data, dynamic_data)

    orch = None
    validation_status = "validated"
    if golden is not None:
        orch_result = Orchestrator().validate(suite, seed_initial, golden)
        orch = orch_result.to_dict()
        orch["skipped"] = False
        orch["unvalidated"] = False
    else:
        # Explicit unvalidated — do NOT treat as Orchestrator accept/reject.
        orch = {
            "accepted": None,
            "reason": (
                "unvalidated: "
                + (golden_note or "no real seed_final / SUCCESS oracle golden for Orchestrator")
            ),
            "revision_flags": ["missing_seed_final", "unvalidated"],
            "skipped": True,
            "unvalidated": True,
        }
        validation_status = "unvalidated"

    # Disposition vs hand-authored on historical trajectories.
    trajs = find_trajectories(gym_root, task_id)
    comparisons: list[dict[str, Any]] = []
    # Disc disposition on golden (seed_final) as SUCCESS proxy when Orchestrator accepts.
    if orch.get("skipped") or orch.get("accepted") is None:
        disc_golden = "NO_GOLDEN"
    elif orch.get("accepted"):
        disc_golden = "SUCCESS"
    else:
        disc_golden = "REJECT_SUITE"
    for kind in ("oracle", "agent"):
        for path in trajs[kind][:3]:
            try:
                ep = load_json(path)
            except Exception:
                continue
            hand = hand_disposition(ep.get("verifier_result"))
            # For oracle trajs, Disc SUCCESS on golden should agree with hand SUCCESS.
            # For agent trajs without reconstructed world, compare only when hand is BREAK
            # and suite has FORBIDDEN coverage (expected catch) vs missing.
            if kind == "oracle":
                disc = disc_golden if disc_golden in {"SUCCESS", "REJECT_SUITE"} else "n/a"
                agree = (
                    (hand == "SUCCESS" and disc == "SUCCESS")
                    or (hand == "BREAK" and disc != "SUCCESS")
                    if disc != "n/a"
                    else None
                )
            else:
                has_forb = bool(suite.by_axis(VerifierAxis.FORBIDDEN))
                if hand == "BREAK":
                    disc = "BREAK_EXPECTED" if has_forb else "NO_FORBIDDEN"
                    agree = has_forb
                elif hand == "SUCCESS":
                    disc = disc_golden
                    agree = disc == "SUCCESS" if disc in {"SUCCESS", "REJECT_SUITE"} else None
                else:
                    disc = "n/a"
                    agree = None
            comparisons.append(
                {
                    "kind": kind,
                    "path": str(path),
                    "agent_name": ep.get("agent_name"),
                    "hand_disposition": hand,
                    "disc_disposition": disc,
                    "agree": agree,
                    "hand_verifier_result": ep.get("verifier_result"),
                }
            )

    agrees = [c["agree"] for c in comparisons if c["agree"] is not None]
    agreement_rate = (sum(1 for a in agrees if a) / len(agrees)) if agrees else None

    axes_plain = {
        axis.value: [
            {
                "id": c.id,
                "plain": f"{c.subgoal} — {c.assertion}".strip(" —"),
                "predicate_kind": c.predicate.get("kind"),
            }
            for c in suite.by_axis(axis)
        ]
        for axis in VerifierAxis
    }

    record = {
        "task_id": task_id,
        "slug": slug,
        "family": fam,
        "pattern": row.get("pattern"),
        "must_include": task_id in MUST_INCLUDE,
        "brief": brief,
        "brief_csv": brief_csv,
        "expected_correct_behavior": row.get("expected_correct_behavior"),
        "seed_source": seed_source,
        "seed_flag": seed_flag,
        "golden_source": golden_source,
        "golden_note": golden_note,
        "validation_status": validation_status,
        "seed_summary": seed_summary_plain(seed_data, dynamic_data),
        "suite": suite.to_dict(),
        "axes_plain": axes_plain,
        "orchestrator": orch,
        "disposition_comparisons": comparisons,
        "agreement_rate_task": agreement_rate,
        "source_model": suite.source_model,
        "detected_traps": suite.detected_traps,
        "forbidden_coverage_path": suite.forbidden_coverage_path,
        "incomplete_forbidden_coverage": suite.incomplete_forbidden_coverage,
    }
    result_path.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
    return record


def write_report(records: list[dict[str, Any]], out_dir: Path, sample_meta: dict) -> Path:
    n = len(records)
    unvalidated = [
        r
        for r in records
        if r.get("validation_status") == "unvalidated"
        or (r.get("orchestrator") or {}).get("unvalidated")
        or (r.get("orchestrator") or {}).get("skipped")
        or "missing_seed_final" in ((r.get("orchestrator") or {}).get("revision_flags") or [])
    ]
    scored = [r for r in records if r not in unvalidated]
    accepted = sum(1 for r in scored if (r.get("orchestrator") or {}).get("accepted") is True)
    rejected = sum(1 for r in scored if (r.get("orchestrator") or {}).get("accepted") is False)
    agree_vals = []
    for r in records:
        for c in r.get("disposition_comparisons") or []:
            if c.get("agree") is not None:
                agree_vals.append(bool(c["agree"]))
    agree_pct = (100.0 * sum(agree_vals) / len(agree_vals)) if agree_vals else None

    fam_cov = Counter(r.get("family") for r in records)
    missing_final = [r["task_id"] for r in unvalidated]
    seed_flags = [r for r in records if r.get("seed_flag")]

    lines: list[str] = []
    lines.append("# Discriminator QA batch — 50 sellable tasks")
    lines.append("")
    lines.append(f"**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    lines.append("")
    lines.append("## Summary stats")
    lines.append("")
    lines.append(f"- Tasks run: **{n}**")
    lines.append(
        f"- Fully validated (Orchestrator ran on real golden): **{len(scored)}** "
        f"— accepted **{accepted}**, rejected **{rejected}**"
        + (f" (accept rate {100.0 * accepted / len(scored):.1f}% of validated)" if scored else "")
    )
    lines.append(
        f"- **Unvalidated** (no real golden — Orchestrator NOT run; "
        f"not counted as accept/reject): **{len(unvalidated)}**"
    )
    if agree_pct is not None:
        lines.append(
            f"- Disposition agreement with hand verifiers (traj-level comparisons): "
            f"**{agree_pct:.1f}%** ({sum(agree_vals)}/{len(agree_vals)} labeled pairs)"
        )
    else:
        lines.append("- Disposition agreement: *no comparable trajectory pairs found*")
    lines.append(f"- Must-include present: {', '.join(MUST_INCLUDE)}")
    lines.append("")
    lines.append("### Family coverage")
    lines.append("")
    lines.append("| Family | Count |")
    lines.append("|---|---:|")
    for fam, cnt in sorted(fam_cov.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"| {fam} | {cnt} |")
    lines.append("")
    lines.append("### Sampling method")
    lines.append("")
    lines.append(sample_meta.get("method_md", ""))
    lines.append("")
    lines.append("### Seed notes")
    lines.append("")
    lines.append(
        "- Seeds regenerated via gym `make_task` into this batch tree "
        "(gym repo not modified). Disk `seed_final` used for Orchestrator only when "
        "it shows a durable delta vs initial (non-hollow)."
    )
    lines.append(
        f"- Tasks left **unvalidated** (missing/hollow golden; Orchestrator skipped): "
        f"{len(missing_final)}"
    )
    if missing_final:
        lines.append(f"  - {', '.join(missing_final[:25])}" + ("…" if len(missing_final) > 25 else ""))
    lines.append(f"- Seed export flags on {len(seed_flags)} tasks (see per-task `seed_flag`).")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Per-task results")
    lines.append("")

    for r in records:
        orch = r.get("orchestrator") or {}
        lines.append(f"### {r['task_id']}")
        lines.append("")
        lines.append(f"- **Family / pattern:** {r.get('family')} — {r.get('pattern')}")
        lines.append(f"- **Must-include:** {'yes' if r.get('must_include') else 'no'}")
        lines.append(f"- **Source model:** {r.get('source_model')}")
        lines.append(f"- **Validation status:** {r.get('validation_status') or ('unvalidated' if orch.get('unvalidated') or orch.get('skipped') else 'validated')}")
        if orch.get("unvalidated") or orch.get("skipped") or orch.get("accepted") is None:
            orch_label = "UNVALIDATED (Orchestrator skipped — no real golden)"
        elif orch.get("accepted"):
            orch_label = "ACCEPT"
        else:
            orch_label = "REJECT"
        lines.append(f"- **Orchestrator:** {orch_label} — {orch.get('reason')}")
        if r.get("detected_traps"):
            lines.append(f"- **Detected traps:** {', '.join(r['detected_traps'])}")
        lines.append(
            f"- **FORBIDDEN coverage path:** {r.get('forbidden_coverage_path')} "
            f"(incomplete={r.get('incomplete_forbidden_coverage')})"
        )
        lines.append("")
        lines.append("**Brief**")
        lines.append("")
        lines.append(f"> {r.get('brief')}")
        lines.append("")
        lines.append(f"**Seed state (plain):** {r.get('seed_summary')}")
        lines.append("")
        lines.append("**Generated suite (plain language by axis)**")
        lines.append("")
        for axis, cps in (r.get("axes_plain") or {}).items():
            lines.append(f"- *{axis}*")
            if not cps:
                lines.append("  - *(none)*")
            for cp in cps:
                lines.append(f"  - {cp.get('plain')} (`{cp.get('predicate_kind')}`)")
        lines.append("")
        lines.append("**Disposition vs hand-authored**")
        lines.append("")
        comps = r.get("disposition_comparisons") or []
        if not comps:
            lines.append("- No historical trajectories found for comparison.")
        else:
            for c in comps:
                agree_s = (
                    "agree"
                    if c.get("agree") is True
                    else ("diverge" if c.get("agree") is False else "n/a")
                )
                lines.append(
                    f"- `{c.get('kind')}` {c.get('agent_name')}: hand={c.get('hand_disposition')} "
                    f"disc={c.get('disc_disposition')} → **{agree_s}**"
                )
        lines.append("")
        lines.append("---")
        lines.append("")

    report_path = out_dir / "QA_REPORT.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Also mirror to docs/
    docs = BACKEND.parent / "docs" / "discriminator_qa_batch_50.md"
    docs.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")
    return report_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gym-root", type=Path, default=GYM_DEFAULT)
    ap.add_argument(
        "--out",
        type=Path,
        default=BACKEND.parent / "docs" / "discriminator_qa_batch_50_data",
    )
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="Optional cap for smoke runs")
    args = ap.parse_args()

    gym_root: Path = args.gym_root
    _load_env_key(gym_root)
    os.environ.setdefault("GYM_REPO_PATH", str(gym_root))
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise SystemExit("ANTHROPIC_API_KEY not set (expected in gym .env)")

    csv_path = gym_root / "trajectories" / "sellable_breakers_v2.csv"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    sampled = sample_tasks(rows, n=args.n, seed=args.seed)
    if args.limit:
        sampled = sampled[: args.limit]

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    sample_path = out_dir / "sample_50.json"
    sample_meta = {
        "method": (
            "Must-include 10 fixed task_ids; fill remaining 40 by stratified shuffle "
            "over pattern→family mapping (checkout-stack, false-premise/sycophancy, "
            "infeasibility, ask-dont-guess, implicit-constraint, self-contradiction, "
            "tool-affordance, structural) with FAMILY_QUOTAS; RNG seed=42."
        ),
        "method_md": (
            "1. Start from `trajectories/sellable_breakers_v2.csv` (85 sellable rows).\n"
            "2. Always include the required ten: M37, M39, M40, M75, M82, M109, M116, M117, M142, M312.\n"
            "3. Map CSV `pattern` strings into mechanism families "
            "(checkout-stack, false-premise/sycophancy, infeasibility, ask-don't-guess, "
            "implicit-constraint, self-contradiction, tool-affordance, structural).\n"
            "4. Fill the remaining 40 slots by stratified random sample (seed=42) using "
            "per-family quotas, then leftover fill.\n"
            "5. Seeds: regenerate via gym `make_task` into this batch tree (enriched catalog); "
            "disk `seed_snapshots` / `screenshots/missing` used only as fallback. "
            "`seed_final` loaded from disk when present for Orchestrator golden."
        ),
        "tasks": [
            {"task_id": r["task_id"], "family": family_of(r.get("pattern", "")), "pattern": r.get("pattern")}
            for r in sampled
        ],
        "family_counts": dict(Counter(family_of(r.get("pattern", "")) for r in sampled)),
    }
    sample_path.write_text(json.dumps(sample_meta, indent=2) + "\n", encoding="utf-8")

    jsonl_path = out_dir / "results.jsonl"
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    def _job(row: dict[str, str]) -> dict[str, Any]:
        return run_one(row, gym_root=gym_root, out_dir=out_dir, force=args.force)

    print(f"Running {len(sampled)} tasks with {args.workers} workers → {out_dir}", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(_job, row): row["task_id"] for row in sampled}
        for fut in as_completed(futs):
            tid = futs[fut]
            try:
                rec = fut.result()
                records.append(rec)
                accepted = (rec.get("orchestrator") or {}).get("accepted")
                print(
                    f"[ok] {tid} accepted={accepted} model={rec.get('source_model')} "
                    f"traps={len(rec.get('detected_traps') or [])}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                err = {"task_id": tid, "error": str(exc), "traceback": traceback.format_exc()}
                errors.append(err)
                print(f"[err] {tid}: {exc}", flush=True)

    # Stable order matching sample
    order = {r["task_id"]: i for i, r in enumerate(sampled)}
    records.sort(key=lambda r: order.get(r["task_id"], 999))

    with jsonl_path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, default=str) + "\n")
    (out_dir / "errors.json").write_text(json.dumps(errors, indent=2) + "\n", encoding="utf-8")

    report = write_report(records, out_dir, sample_meta)
    print(f"Wrote report {report}", flush=True)
    scored = [
        r
        for r in records
        if not (r.get("orchestrator") or {}).get("skipped")
        and (r.get("orchestrator") or {}).get("accepted") is not None
    ]
    acc = sum(1 for r in scored if (r.get("orchestrator") or {}).get("accepted"))
    print(
        f"Orchestrator accepted {acc}/{len(scored)} scored "
        f"({len(records) - len(scored)} skipped); errors={len(errors)}",
        flush=True,
    )
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
