"""What a step CHANGED, in the world's own vocabulary.

The trajectory records what an annotator did TO THE WORLD. Coordinates and
scroll offsets describe the annotator's hand; `shop.orders` gaining `ORD_7`
describes the task. Only the second survives a re-render, a different viewport
or a different screen size, and only the second is a thing an SFT target should
learn to produce.

So this module answers one question: given the world before an action and the
world after it, what is the smallest true description of the difference?

Three rules keep it honest:

* **Normalize through the hash's own normalizer.** `checkpoints.normalize_world`
  strips the keys that churn (`flash_messages`, `action_log`) and canonicalizes
  integral floats, because a world round-tripped through a JSON column comes back
  with `0.0` where the gym said `0`. Sharing that function guarantees
  `diff.changed == (hash_world(a) != hash_world(b))` — the delta and the
  divergence gate can never disagree about whether anything happened.
* **Diff entities by id, never by position.** The world is dicts keyed by entity
  id, so a keyset difference is the real answer. Diffing a cart by list index
  would report "everything after position 2 changed" when one item was removed.
* **Bounded output, exact counts.** A delta is written on the hot path and read
  by a 1.5s poll, so it is capped. When the cap truncates, `counts` still
  reports the true totals — "500 orders were added" must survive even when the
  list of them does not.

Known limitation, inherited deliberately: a world that changes and changes back
within one observation window reports no change. That is exactly what
`hash_world` already claims, and inheriting its blind spot is better than
inventing a second definition of "changed".
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import checkpoints, models
from app.recorder import _REDACTED, _SENSITIVE

SCHEMA_VERSION = 1

# Bounds. A delta rides the materialize write path and is read by the action-log
# poll, so it cannot be unbounded — a bulk seed can move hundreds of entities.
MAX_CHANGES = 200
MAX_DEPTH = 6
MAX_VALUE_BYTES = 2_048
MAX_DELTA_BYTES = 64_000
UI_CHANGE_CAP = 8

# Dotted paths whose dicts are {entity_id: entity}. Keyset diffs on these give
# added/removed entities for free; without the registry a new order would report
# as twenty separate scalar `set`s and read as noise.
ENTITY_COLLECTIONS = frozenset({
    "shop.orders", "shop.returns", "shop.subscriptions", "shop.addresses",
    "shop.payments", "shop.products",
    "mail.inbox", "mail.sent", "mail.drafts",
    "market.orders", "market.coupons", "market.addresses", "market.payments",
    "market.products",
    "food.orders", "food.restaurants",
    "calendar.events",
})

# Lists whose members carry their own identity. Diffing these by index would
# report a removal as "every later position changed".
LIST_KEYS = {
    "shop.cart.items": "product_id",
    "market.cart.items": "product_id",
    "food.cart.items": "item_id",
}

# Keys worth keeping when an entity is summarized. Deliberately excludes body,
# password, token, card — a delta must not become a second route to data the
# recorder redacts at capture time.
_LABEL_KEYS = (
    "id", "name", "title", "subject", "status", "state", "kind",
    "total", "amount", "price", "qty", "quantity",
    "product_id", "sku", "to", "from_", "app", "code",
)


# --- digests ---------------------------------------------------------------

def _redact(key: str, value: Any) -> Any:
    return _REDACTED if _SENSITIVE.search(str(key)) else value


def _cap(value: Any) -> Any:
    """Keep a summarized value small enough to sit in a JSON column."""
    try:
        if len(json.dumps(value, default=str)) <= MAX_VALUE_BYTES:
            return value
    except (TypeError, ValueError):
        return str(value)[:MAX_VALUE_BYTES]
    return {"…": "truncated"}


def _digest(value: Any) -> Any:
    """A short, recognisable stand-in for a whole entity.

    An `add` on `shop.orders` should read as "order ORD_7, placed, $49.99", not
    as the entity's full twenty fields — the point is that a human (and a model)
    can see WHAT was added without the delta becoming a second copy of the world.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k in _LABEL_KEYS:
            if k in value:
                v = value[k]
                out[k] = _redact(k, v) if not isinstance(v, (dict, list)) else {"…": len(v)}
        if not out:  # nothing recognisable — say how big it was
            return {"…": len(value)}
        return _cap(out)
    if isinstance(value, list):
        return _cap({"len": len(value),
                     "sample": [_digest(v) for v in (value[:1] + value[-1:] if len(value) > 1 else value)]})
    return _cap(value)


def _change(app: str, path: str, op: str, id_: str,
            from_: Any, to: Any, size: list[int] | None) -> dict:
    """One change, always the same seven keys. A fixed shape means the UI and the
    tests never reach for `.get()` chains."""
    return {"app": app, "path": path, "op": op, "id": id_,
            "from": from_, "to": to, "size": size}


# --- the walk --------------------------------------------------------------

def _is_entity_collection(path: str, before: Any, after: Any) -> bool:
    if path in ENTITY_COLLECTIONS:
        return True
    # Fallback so an app added tomorrow still diffs sanely rather than degrading
    # into a wall of scalars: a nested dict whose values are all dicts.
    if path.count(".") < 1:
        return False
    for node in (before, after):
        if not isinstance(node, dict) or not node:
            continue
        if all(isinstance(v, dict) for v in node.values()):
            return True
    return False


def _walk(path: str, app: str, before: Any, after: Any,
          out: list[dict], counts: dict[str, int], depth: int, id_: str = "") -> None:
    if before == after:
        return

    if depth >= MAX_DEPTH:
        counts["set"] += 1
        out.append(_change(app, path, "set", id_, _digest(before), _digest(after), None))
        return

    # identified list (cart items) — diff by the member's own id
    key = LIST_KEYS.get(path)
    if key and isinstance(before, list) and isinstance(after, list):
        bmap = {str(m.get(key)): m for m in before if isinstance(m, dict) and m.get(key) is not None}
        amap = {str(m.get(key)): m for m in after if isinstance(m, dict) and m.get(key) is not None}
        if len(bmap) == len([m for m in before if isinstance(m, dict)]) and \
           len(amap) == len([m for m in after if isinstance(m, dict)]):
            size = [len(before), len(after)]
            for k in sorted(set(bmap) - set(amap)):
                counts["remove"] += 1
                out.append(_change(app, path, "remove", k, _digest(bmap[k]), None, size))
            for k in sorted(set(amap) - set(bmap)):
                counts["add"] += 1
                out.append(_change(app, path, "add", k, None, _digest(amap[k]), size))
            for k in sorted(set(bmap) & set(amap)):
                _walk(f"{path}.{k}", app, bmap[k], amap[k], out, counts, depth + 1, k)
            return

    if isinstance(before, dict) and isinstance(after, dict):
        entity = _is_entity_collection(path, before, after)
        size = [len(before), len(after)] if entity else None
        for k in sorted(set(before) | set(after)):
            sub = f"{path}.{k}" if path else k
            child_app = app or (k if not path else app)
            if k not in after:
                counts["remove"] += 1
                out.append(_change(child_app, path if entity else sub, "remove",
                                   k if entity else id_, _digest(before[k]), None, size))
            elif k not in before:
                counts["add"] += 1
                out.append(_change(child_app, path if entity else sub, "add",
                                   k if entity else id_, None, _digest(after[k]), size))
            else:
                _walk(sub, child_app, before[k], after[k], out, counts,
                      depth + 1, k if entity else id_)
        return

    if isinstance(before, list) and isinstance(after, list):
        # append-only (the cross-app event log, a queue) — the common case
        if len(after) > len(before) and after[:len(before)] == before:
            for m in after[len(before):]:
                counts["add"] += 1
                out.append(_change(app, path, "add", "", None, _digest(m), [len(before), len(after)]))
            return
        counts["set"] += 1
        out.append(_change(app, path, "set", id_, _digest(before), _digest(after),
                           [len(before), len(after)]))
        return

    counts["set"] += 1
    out.append(_change(app, path, "set", id_, _redact(path.rsplit(".", 1)[-1], before),
                       _redact(path.rsplit(".", 1)[-1], after), None))


# --- public ----------------------------------------------------------------

def _empty(from_hash: str = "", to_hash: str = "") -> dict:
    return {"v": SCHEMA_VERSION, "changed": False, "from_hash": from_hash, "to_hash": to_hash,
            "apps": [], "summary": "", "counts": {"add": 0, "remove": 0, "set": 0},
            "changes": [], "truncated": False}


def diff_worlds(before: dict | None, after: dict | None) -> dict:
    """The semantic difference between two gym worlds, as `world-delta/1`."""
    from_hash = checkpoints.hash_world(before)
    to_hash = checkpoints.hash_world(after)
    # Hash-first: the common case (a click that opened a menu) costs one sha256
    # and no walking at all.
    if from_hash == to_hash:
        return _empty(from_hash, to_hash)

    b = checkpoints.normalize_world(before or {})
    a = checkpoints.normalize_world(after or {})

    changes: list[dict] = []
    counts = {"add": 0, "remove": 0, "set": 0}
    _walk("", "", b, a, changes, counts, 0)

    # Deterministic order. Without this the same transition serializes two ways
    # across runs and every comparison test flakes.
    changes.sort(key=lambda c: (c["app"], c["path"], c["id"], c["op"]))
    truncated = len(changes) > MAX_CHANGES
    kept = changes[:MAX_CHANGES]

    delta = {
        "v": SCHEMA_VERSION, "changed": True,
        "from_hash": from_hash, "to_hash": to_hash,
        "apps": sorted({c["app"] for c in changes if c["app"]}),
        "counts": counts,          # exact, even when `changes` is truncated
        "changes": kept,
        "truncated": truncated,
    }
    delta["summary"] = summarize(delta)

    try:
        if len(json.dumps(delta, default=str)) > MAX_DELTA_BYTES:
            delta["changes"] = []
            delta["truncated"] = True
    except (TypeError, ValueError):
        delta["changes"] = []
        delta["truncated"] = True
    return delta


def is_empty(delta: dict | None) -> bool:
    return not delta or not delta.get("changed")


def render(change: dict) -> str:
    """One change as one short phrase."""
    path = change.get("path") or ""
    leaf = path.rsplit(".", 1)[-1] or "state"
    op, id_ = change.get("op"), change.get("id") or ""
    if op == "add":
        return f"{leaf} +{id_}".strip() if id_ else f"{leaf} added"
    if op == "remove":
        return f"{leaf} −{id_}".strip() if id_ else f"{leaf} removed"
    return f"{leaf} {change.get('from')} → {change.get('to')}"


def summarize(delta: dict | None, limit: int = 3) -> str:
    if is_empty(delta):
        return ""
    parts = [render(c) for c in (delta.get("changes") or [])[:limit]]
    extra = (delta.get("counts") or {})
    total = extra.get("add", 0) + extra.get("remove", 0) + extra.get("set", 0)
    if total > len(parts):
        parts.append(f"+{total - len(parts)} more")
    return "; ".join(parts)


def compact(delta: dict | None, limit: int = UI_CHANGE_CAP) -> dict | None:
    """The poll-sized projection. `flat_view` re-sends the whole flattened
    trajectory every 1.5s, so the full change list must not ride along."""
    if delta is None:
        return None
    if not delta.get("changed"):
        return _empty(delta.get("from_hash", ""), delta.get("to_hash", ""))
    return {
        "v": delta.get("v", SCHEMA_VERSION), "changed": True,
        "apps": delta.get("apps") or [], "summary": delta.get("summary") or "",
        "counts": delta.get("counts") or {},
        "changes": (delta.get("changes") or [])[:limit],
        "truncated": bool(delta.get("truncated")) or len(delta.get("changes") or []) > limit,
    }


# --- DB-aware tail ---------------------------------------------------------

def previous_world(db: Session, attempt: models.ReviewSession,
                   checkpoint_id) -> tuple[str, dict | None]:
    """The world a delta should be measured against: the checkpoint the next step
    will chain from. Returns (hash, world)."""
    if checkpoint_id is None:
        return "", None
    cp = db.get(models.EnvironmentCheckpoint, checkpoint_id)
    if cp is None:
        return "", None
    return (cp.world_hash or "", cp.world)


def attempt_summary(db: Session, attempt: models.ReviewSession,
                    *, final_world: dict | None = None) -> dict | None:
    """What this whole attempt changed: the initial seeded world vs the end state.

    This is the DB-diff the verifier story wants — one delta per attempt, against
    the world the task started from.
    """
    if attempt.initial_checkpoint_id is None:
        return None
    initial = db.get(models.EnvironmentCheckpoint, attempt.initial_checkpoint_id)
    if initial is None:
        return None

    final = final_world
    if final is None and attempt.final_checkpoint_id is not None:
        cp = db.get(models.EnvironmentCheckpoint, attempt.final_checkpoint_id)
        final = cp.world if cp else None
    if final is None:
        cp = db.scalar(
            select(models.EnvironmentCheckpoint)
            .where(models.EnvironmentCheckpoint.attempt_id == attempt.id)
            .order_by(models.EnvironmentCheckpoint.created_at.desc())
        )
        final = cp.world if cp else None
    if final is None:
        return None

    delta = diff_worlds(initial.world, final)
    delta["scope"] = "attempt"
    return delta
