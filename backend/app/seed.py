"""Seed the task catalog: the hand-authored fixture tasks + all 312 real gym
tasks, so every task exists as a row (with its prompt) from the start.

The gym catalog comes from a VENDORED file, `app/data/cua_tasks.json`, exported
offline by the gym's `tools/export_cua_catalog.py`. It used to come from a live
`GET /_harness/tasks`, which had two problems: a gym outage silently produced an
empty queue, and that endpoint only returns ids — so every gym task's `prompt`
stayed blank and an annotator had nothing to read. The gym is still queried, but
only to REPORT drift between the catalog and a running gym, never as the source.
"""

from __future__ import annotations

import json
import pathlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, gym_client, models
from app.api.tasks import _TASKS
from app.config import settings

_CATALOG = pathlib.Path(__file__).resolve().parent / "data" / "cua_tasks.json"

# Five dummy annotator accounts to test the multi-annotator flow (login-only; open
# self-registration is off). Shared dev password below — TEST ACCOUNTS ONLY. One
# reviewer is seeded so the QA/adjudication path can be exercised.
_DEV_PASSWORD = "annotate1"  # dev-only dummy credential for the seeded test accounts
_DUMMY_ANNOTATORS = [
    ("ana@deccan.ai", "Ana Rivera", 210, "reviewer"),
    ("ben@deccan.ai", "Ben Okafor", 145, "annotator"),
    ("chloe@deccan.ai", "Chloe Tan", 335, "annotator"),
    ("diego@deccan.ai", "Diego Santos", 28, "annotator"),
    ("ela@deccan.ai", "Ela Novak", 268, "annotator"),
]


def seed_annotators(db: Session) -> int:
    """Ensure the 5 dummy test accounts exist (idempotent). Sets a password only
    if one isn't already set, so re-seeding never resets a changed password.

    NEVER runs in production: these accounts share one publicly-known dev password,
    so seeding them into a real deployment hands out working logins — one of them a
    REVIEWER, who can adjudicate which samples ship and pull the whole dataset."""
    if settings.env == "prod":
        return 0
    created = 0
    for email, name, hue, role in _DUMMY_ANNOTATORS:
        a = db.scalar(select(models.Annotator).where(models.Annotator.email == email))
        if a is None:
            a = models.Annotator(email=email)
            db.add(a)
            created += 1
        a.display_name = a.display_name or name
        a.avatar_hue = hue
        a.role = role
        a.is_active = True
        if not a.password_hash:
            a.password_hash = auth.hash_password(_DEV_PASSWORD)
    db.commit()
    return created


def _upsert_fixture(db: Session, external_id: str, fx: dict) -> None:
    task = fx["task"]
    row = db.scalar(select(models.Task).where(models.Task.external_id == external_id))
    if row is None:
        row = models.Task(external_id=external_id)
        db.add(row)
    row.source = "fixture"
    row.title = task["title"]
    row.prompt = task["prompt"]
    row.category = task.get("meta", "")
    row.priority = task.get("priority", "Medium")
    row.start_url = task.get("startState", {}).get("url", "")
    row.seed_state = {"startState": task.get("startState", {})}
    row.meta = {
        "constraints": task.get("constraints", []),
        "allowedSites": task.get("allowedSites", []),
        "runSummary": task.get("runSummary", []),
    }


def load_catalog() -> dict:
    """The vendored gym catalog. Missing file is not fatal — fixtures still seed."""
    if not _CATALOG.exists():
        return {}
    try:
        return json.loads(_CATALOG.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _upsert_gym_task(db: Session, entry: dict, existing: dict[str, models.Task]) -> bool:
    """Upsert one catalog task. Returns True when the row was created."""
    tid = entry["task_id"]
    row = existing.get(tid)
    created = row is None
    if row is None:
        row = models.Task(external_id=tid, source="gym")
        db.add(row)
    row.source = "gym"
    row.title = row.title or tid.split("/")[-1].replace("_", " ").strip().capitalize()
    # The prompt is annotator-EDITABLE, so the catalog is a default, never an
    # override: clobbering it here would silently discard someone's correction.
    if not row.prompt:
        row.prompt = entry.get("prompt", "")
    row.category = entry.get("category") or (tid.split("/")[0] if "/" in tid else "")
    row.start_url = row.start_url or entry.get("start_path", "")
    meta = dict(row.meta or {})
    meta.update({
        "primaryApp": entry.get("primary_app", "shop"),
        "difficulty": entry.get("difficulty", ""),
        "inEightyFive": bool(entry.get("in_85")),
        # Per-app seed SIDs + landing routes. Bridged sessions don't need the SIDs
        # (the bridge baselines its own worlds), but a plain-clone deployment does.
        "apps": entry.get("apps", {}),
    })
    row.meta = meta
    return created


def seed_catalog(db: Session) -> dict:
    """Upsert fixture + gym tasks from the vendored catalog."""
    for ext, fx in _TASKS.items():
        _upsert_fixture(db, ext, fx)

    catalog = load_catalog()
    entries = catalog.get("tasks") or []
    existing = {t.external_id: t for t in db.scalars(
        select(models.Task).where(models.Task.source == "gym")).all()}
    gym_added = sum(1 for e in entries if _upsert_gym_task(db, e, existing))
    db.commit()

    # Reconciliation only: if a gym is up and its task list disagrees with the
    # catalog, say so loudly rather than quietly annotating a stale set.
    ids = gym_client.tasks()
    drift: dict = {}
    if ids is not None:
        cat_ids = {e["task_id"] for e in entries}
        live = set(ids)
        drift = {"onlyInGym": sorted(live - cat_ids)[:10], "onlyInCatalog": sorted(cat_ids - live)[:10],
                 "gymCount": len(live), "catalogCount": len(cat_ids)}

    annotators = seed_annotators(db)
    return {"fixtures": len(_TASKS), "gym_tasks": len(entries), "gym_added": gym_added,
            "gym_reachable": ids is not None, "drift": drift,
            "annotators_created": annotators}
