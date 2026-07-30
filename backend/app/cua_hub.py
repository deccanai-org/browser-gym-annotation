"""Registry + session wiring for the realistic cua-hub mock UIs.

The gym's realistic UIs (Amazon/Gmail/eBay/Google Calendar/Uber Eats, from XLang's
CUA-Gym-Hub) are seeded per (task, seed, app) under a stable seed_sid by the gym-side
batch (`tools/seed_all_tasks.py`). This module lets the annotator's live browser open
those UIs: on session open it CLONES the frozen seed_sid into a fresh per-attempt
attempt_sid (so each annotator gets an isolated world) and returns the URL to open.

    shop     -> Amazon           cua-hub-amazon.<domain>
    mail     -> Gmail            cua-hub-gmail.<domain>
    market   -> eBay             cua-hub-ebay.<domain>
    calendar -> Google Calendar  cua-hub-google-calendar.<domain>
    food     -> Uber Eats        cua-hub-uber-eats.<domain>

Env:
    CUA_HUB_MODE        "1" to route gym tasks at the realistic UIs (default off).
    CUA_HUB_DOMAIN      default ``delta.deccanexperts.ai``
    CUA_HUB_SCHEME      default ``https``
    CUA_HUB_SID_PARAM   URL param a mock loads its seed SID from (default ``sid``).
    CUA_HUB_URL_<APP>   per-app base-URL override (e.g. CUA_HUB_URL_SHOP=http://localhost:5201
                        for local dev / a reverse proxy). Falls back to the subdomain URL.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

CUA_HUB_DOMAIN = os.environ.get("CUA_HUB_DOMAIN", "delta.deccanexperts.ai")
CUA_HUB_SCHEME = os.environ.get("CUA_HUB_SCHEME", "https")
CUA_HUB_SID_PARAM = os.environ.get("CUA_HUB_SID_PARAM", "sid")


def enabled() -> bool:
    return os.environ.get("CUA_HUB_MODE", "").strip().lower() in ("1", "true", "on", "yes")


@dataclass(frozen=True)
class MockApp:
    key: str        # annotator app key (matches the frontend APP_COLOR map)
    mock_key: str   # the `mock` column value in cua-gym
    subdomain: str  # cua-hub-<subdomain>.<domain>
    title: str
    start_path: str  # where the live browser lands (gmail is hash-routed)


REGISTRY: dict[str, MockApp] = {
    "shop": MockApp("shop", "amazon_mock", "amazon", "Amazon", "/"),
    "mail": MockApp("mail", "gmail_mock", "gmail", "Gmail", "/#/inbox"),
    "market": MockApp("market", "ebay_mock", "ebay", "eBay", "/"),
    "calendar": MockApp("calendar", "google_calendar", "google-calendar", "Google Calendar", "/"),
    "food": MockApp("food", "uber_eats_mock", "uber-eats", "Uber Eats", "/"),
}

# gym URL path segment -> app key (the shop lives at the root)
_SEG_TO_APP = {"mail": "mail", "food": "food", "market": "market", "valuemart": "market", "calendar": "calendar"}


def host(app_key: str) -> str:
    return f"cua-hub-{REGISTRY[app_key].subdomain}.{CUA_HUB_DOMAIN}"


def base_url(app_key: str) -> str:
    """The app's mock origin — a per-app env override, else the subdomain."""
    return os.environ.get(f"CUA_HUB_URL_{app_key.upper()}") or f"{CUA_HUB_SCHEME}://{host(app_key)}"


def mock_url(app_key: str, start_path: str | None = None, seed_sid: str | None = None) -> str:
    """URL the live browser opens: the realistic UI at start_path carrying the SID.

    Keeps the query before the fragment so hash-routed SPAs (Gmail's /#/inbox) work.
    """
    sp = start_path if start_path is not None else REGISTRY[app_key].start_path
    if not sp.startswith(("/", "#", "?")):
        sp = "/" + sp
    parts = urlsplit(sp)
    query = parts.query
    if seed_sid:
        extra = urlencode({CUA_HUB_SID_PARAM: seed_sid})
        query = f"{query}&{extra}" if query else extra
    return base_url(app_key) + urlunsplit(("", "", parts.path or "/", query, parts.fragment))


def allowed_sites(app_keys: list[str]) -> list[dict]:
    return [{"host": host(k), "app": REGISTRY[k].key, "title": REGISTRY[k].title}
            for k in app_keys if k in REGISTRY]


# --------------------------------------------------------------- task wiring ---
def is_cua_task(task) -> bool:
    """A gym task routed at the realistic UIs (mode on + it's a gym-sourced task)."""
    if not enabled() or task is None:
        return False
    return bool(getattr(task, "external_id", None)) or getattr(task, "source", None) == "gym"


def seed_sid(task_external_id: str, seed: int, app_key: str) -> str:
    """Stable seed_sid — MUST match the gym seeder (tools/session_manager._seed_sid)."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"seed-{task_external_id}-{seed}-{app_key}")


def primary_app(start_url: str | None) -> str:
    """Which app the live browser lands on — from the task's start-url path segment."""
    seg = urlsplit(start_url or "").path.lstrip("/").split("/")[0].lower()
    return _SEG_TO_APP.get(seg, "shop")


# --------------------------------------------------------------- state API -----
def _http(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode() or "{}")


def _get_state(app_key: str, sid: str) -> dict:
    return _http("GET", f"{base_url(app_key).rstrip('/')}/state?sid={sid}").get("stored_state") or {}


def _post_state(app_key: str, sid: str, state: dict) -> None:
    _http("POST", f"{base_url(app_key).rstrip('/')}/post?sid={sid}", {"action": "set", "state": state})


def start_attempt(task_external_id: str, seed: int, apps: list[str] | None = None) -> list[dict]:
    """Clone each app's frozen seed_sid into a fresh attempt_sid; return per-app open info.

    [{app, mock_key, title, attempt_sid, url}] — apps whose seed isn't present are skipped.
    """
    out: list[dict] = []
    for app in (apps or list(REGISTRY)):
        if app not in REGISTRY:
            continue
        try:
            state = _get_state(app, seed_sid(task_external_id, seed, app))
            if not state:
                continue  # not seeded (run tools/seed_all_tasks first) — skip
            attempt = str(uuid.uuid4())
            _post_state(app, attempt, state)
        except Exception:
            continue
        out.append({"app": app, "mock_key": REGISTRY[app].mock_key, "title": REGISTRY[app].title,
                    "attempt_sid": attempt, "url": mock_url(app, seed_sid=attempt)})
    return out
