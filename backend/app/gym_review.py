"""Map a real gym trajectory + its milestone verdict into the annotator's review
payload (M8), so any of the gym's real tasks can be reviewed in the Task Review
UI — real brief, real steps with real per-step screenshots, real milestones as
the verifier suite.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

_TYPE = {
    "navigate": "navigate", "goto": "navigate", "open": "navigate", "open_url": "navigate",
    "fill": "type", "type": "type", "press": "type",
    "click": "click", "select": "click", "check": "click",
    "submit": "submit",
    "extract": "extract", "read": "extract",
    "open_tab": "tab", "switch_tab": "tab", "new_tab": "tab",
}


def _atype(kind: str | None) -> str:
    return _TYPE.get((kind or "").lower(), "click")


# The gym is one multi-app SPA: the shop lives at the root; the other apps are
# path-prefixed. Map a step URL → (app-key, tab title, pretty host) so a gym run
# renders as SEPARATE app tabs (ShopGym / ShopMail / …) like the fixtures, not
# one window. App keys match the frontend's APP_COLOR map.
_GYM_APPS = {
    "mail": ("mail", "ShopMail", "mail.gym.local"),
    "food": ("food", "Food", "food.gym.local"),
    "market": ("market", "ValueMart", "valuemart.gym.local"),
    "valuemart": ("market", "ValueMart", "valuemart.gym.local"),
    "calendar": ("calendar", "Calendar", "calendar.gym.local"),
}


def _app_of(url: str) -> tuple[str, str, str]:
    seg = urlparse(url or "").path.lstrip("/").split("/")[0].lower()
    return _GYM_APPS.get(seg, ("shop", "ShopGym", "shop.gym.local"))


def _level(m: dict) -> str:
    n = (m.get("name") or "").lower()
    if m.get("forbidden"):
        return "safety"
    if any(w in n for w in ("cart", "order", "checkout", "address", "payment", "refund", "sub", "promo", "email", "sent", "price")):
        return "backend"
    if any(w in n for w in ("page", "view", "on_", "confirm", "render", "visible", "shown", "displayed")):
        return "ui"
    if any(w in n for w in ("judge", "correct", "intent", "match")):
        return "semantic"
    return "process"


def _milestone_result(m: dict) -> str:
    """Whether this milestone is satisfied. The gym reports FIRING, not a verdict:
    `fired_at_step` is -1 until it fires, and a FORBIDDEN milestone passes by not
    firing at all.

    Step 0 is a real step, and `or -1` did not think so. Python reads 0 as falsy,
    so `(m.get("fired_at_step", -1) or -1)` turned a milestone that fired at step
    0 into -1 — never fired. The finalize replay calls verify(i) from i=0 AFTER
    the first action, so anything the annotator satisfied with their FIRST action
    scored fail. Worse in the other direction: a forbidden tripwire tripped at
    step 0 passes the `not fired` branch and is recorded as clean — the harm
    happened and the sample says it did not, on a dataset whose entire product is
    detecting exactly that.
    """
    at = m.get("fired_at_step", -1)
    fired = isinstance(at, (int, float)) and not isinstance(at, bool) and at >= 0
    passed = (not fired) if m.get("forbidden") else fired
    return "pass" if passed else "fail"


def _humanize(s: dict) -> str:
    if (s.get("reasoning") or "").strip():
        return s["reasoning"].strip()[:120]
    kind = (s.get("action_kind") or "step").lower()
    args = s.get("action_args")
    hint = ""
    if isinstance(args, dict):
        for k in ("value", "text", "url", "path", "href", "query", "label", "selector"):
            if args.get(k):
                hint = str(args[k])[:54]
                break
    elif args:
        mm = re.search(r"['\"]([^'\"]{2,54})['\"]", str(args))
        if mm:
            hint = mm.group(1)
    verb = {"navigate": "Navigated to", "goto": "Navigated to", "open_url": "Opened",
            "fill": "Filled", "type": "Typed", "click": "Clicked", "submit": "Submitted",
            "extract": "Read", "read": "Read", "press": "Pressed"}.get(kind, kind.capitalize())
    return (f"{verb} {hint}".strip()) or kind


def to_review(run: dict, task_id: str, agent: str) -> dict:
    t = run.get("trajectory") or {}
    steps_in = t.get("steps") or []
    vr = t.get("verifier_result") or {}
    init_url = t.get("initial_url") or "http://localhost:8000"

    tabs_by_key: dict[str, dict] = {}
    steps = []
    for i, s in enumerate(steps_in):
        sp = s.get("screenshot_path")
        err = bool(s.get("action_error") and str(s.get("action_error")) != "None")
        app_key, app_title, app_host = _app_of(s.get("url_after") or init_url)
        if app_key not in tabs_by_key:
            tabs_by_key[app_key] = {"id": app_key, "app": app_key, "title": app_title, "host": app_host}
        steps.append({
            "idx": i + 1,
            "type": "error" if err else _atype(s.get("action_kind")),
            "tabId": app_key,  # per-app, so the run renders as separate tabs
            "description": _humanize(s),
            "image": f"/api/gym/screenshot?path={sp}" if sp else None,
            "errorMsg": str(s.get("action_error")) if err else None,
            "url": s.get("url_after") or "",
        })
    if not tabs_by_key:
        tabs_by_key["shop"] = {"id": "shop", "app": "shop", "title": "ShopGym", "host": "shop.gym.local"}
    tabs = list(tabs_by_key.values())

    verifiers = []
    for j, m in enumerate(vr.get("all_milestones") or []):
        name = m.get("name") or f"milestone_{j}"
        verifiers.append({
            "id": f"m{j}",
            # WHICH milestone this check is bound to, as executable IR.
            #
            # `id` has to stay positional because `benchmark_run.results` is keyed
            # by it, so the name used to be spent only on the assertion text and
            # the code blurb — and the binding was lost the moment the row was
            # written. A scorer then had nothing to look the milestone up BY: it
            # matched `m0` against a verdict keyed by NAME, missed every time, and
            # marked a suite whose milestones had all fired correctly unprovable.
            "check": {"kind": "gym_milestone", "id": name},
            "level": _level(m),
            "assertion": name.replace("_", " ").strip().capitalize(),
            "code": (
                f"milestone: {name} · weight {m.get('weight')}"
                + (" · required" if m.get("required") else "")
                + (" · forbidden" if m.get("forbidden") else "")
            ),
            "gymResult": _milestone_result(m),
        })

    diff = (t.get("task_difficulty") or "medium").lower()
    priority = {"easy": "Low", "medium": "Medium", "hard": "High"}.get(diff, "Medium")
    title = task_id.split("/")[-1].replace("_", " ").strip().capitalize()
    reward = 1 if vr.get("success") else 0
    errors = sum(1 for s in steps if s["type"] == "error")
    # Show the concrete MODEL (agent_name = "openai[gpt-5.5]" → "gpt-5.5") so the
    # annotator sees which model produced the run, not just the agent family.
    _an = t.get("agent_name") or agent
    _mm = re.search(r"\[([^\]]+)\]", _an)
    _inner = _mm.group(1).strip() if _mm else ""
    agent_label = _inner if _inner and _inner.lower() not in ("default", "none") else agent

    return {
        "task": {
            "id": task_id, "priority": priority, "title": title,
            "meta": f"{t.get('task_category', 'gym')} · {agent_label}",
            "prompt": t.get("task_brief") or "",
            "startState": {"summary": f"Gym task · {agent_label} run · seed {run.get('seed', 0)}", "url": t.get("initial_url") or ""},
            "constraints": [c for c in (diff.capitalize(), t.get("task_category")) if c],
            "allowedSites": [{"host": tb["host"], "app": tb["app"]} for tb in tabs],
            "runSummary": [
                {"value": str(len(steps)), "label": "Steps"},
                {"value": str(len(tabs)), "label": "Tabs opened"},
                {"value": str(errors), "label": "Errors", "tone": "error" if errors else "default"},
                {"value": f"{vr.get('score', 0):.2f}", "label": "Score", "tone": "success" if reward else "error"},
            ],
        },
        "tabs": tabs,
        "steps": steps,
        "correctionSeed": "Correct the outcome, then re-verify in the live gym. Optional state edits, one per line — e.g.  shop.orders.ORD_1.payment_id = pm_personal  (or  shop.orders = {}  to void it).",
        "correctedTail": [],
        "verifiers": verifiers,
        "gymReward": reward,
        "source": "gym",
        # Everything needed to resume this episode from a corrected state (the
        # live world is attached by the run-review job as gymResume.worldState).
        "gymResume": {
            "seed": int(run.get("seed", 0)),
            "urlTrail": [s.get("url_after") or "" for s in steps_in],
            # The FULL world AFTER each step. Correcting step N resumes from
            # worldTrail[N-1]; resuming from `worldState` (the run's FINAL world)
            # replays a state that already contains every later step's effects.
            "worldTrail": [s.get("world_after") for s in steps_in],
            "finalUrl": t.get("final_url") or "",
        },
    }
