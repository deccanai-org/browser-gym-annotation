"""Registry + session wiring for the realistic gym UIs (ShopGym / ValueMart /
ShopMail / GymCal / GymEats).

These are the five mock storefronts the annotator actually works in. This module
answers three questions that have three *different* answers, and conflating any
two of them is a bug we have already been bitten by:

    api_base(app)   the state API we read/seed          (backend-visible)
    ui_base(app)    the SPA a browser opens             (browser-visible)
    mock_url(...)   the openable URL for one attempt    (SPA + ?sid=[&bridge=&session=])

Handing someone an api_base URL opens JSON, not a storefront. The two are
different hosts on the hosted stack and different *namespaces* locally (the
backend runs in a container, the browser does not), so they are kept apart here
and each is independently overridable.

Bridged mode is the default. `?bridge=&session=` puts a tab in bridged mode, where
its clicks drive the real gym engine rather than the mock's local store — which is
what makes cross-app effects (an order in ShopGym producing an email in ShopMail)
and the gym's own milestone verifiers work. See `bridge_client`.

Seed SIDs are UUIDv5 and MUST match the gym's `tools/cua_env.seed_sid` byte for
byte — the hub stores state in Postgres and rejects a sid that isn't a real UUID,
so a mismatch is not a near-miss, it is a hard failure for every task.

Env:
    CUA_HUB_MODE            "1" to route gym tasks at the realistic UIs (default off).
    CUA_HUB_API_ROOT        state-API root; per-app `/api/<mock_key>` is appended.
    CUA_HUB_API_URL_<APP>   per-app state-API override (wins over the root).
    CUA_HUB_UI_URL_<APP>    per-app SPA-origin override (e.g. http://127.0.0.1:5201).
    CUA_HUB_DOMAIN          default ``delta.deccanexperts.ai`` (hosted SPA hosts).
    CUA_HUB_SCHEME          default ``https``
    CUA_HUB_SID_PARAM       URL param a mock loads its SID from (default ``sid``).
    CUA_SEED_REV            seed-SID family; must equal the gym's SEED_REV.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

CUA_HUB_DOMAIN = os.environ.get("CUA_HUB_DOMAIN", "delta.deccanexperts.ai")
CUA_HUB_SCHEME = os.environ.get("CUA_HUB_SCHEME", "https")
CUA_HUB_SID_PARAM = os.environ.get("CUA_HUB_SID_PARAM", "sid")
CUA_HUB_API_ROOT = os.environ.get("CUA_HUB_API_ROOT", "https://cua-gym-hub.delta.soulhq.ai")

# Keep in lockstep with tools/cua_env.py in the gym repo.
NS_GYM = uuid.uuid5(uuid.NAMESPACE_URL, "https://gym.deccanexperts.ai/cua-seed/v1")


def seed_rev() -> int:
    try:
        return int(os.environ.get("CUA_SEED_REV", "1"))
    except ValueError:
        return 1


def enabled() -> bool:
    return os.environ.get("CUA_HUB_MODE", "").strip().lower() in ("1", "true", "on", "yes")


class CuaHubError(Exception):
    """Base — anything that stopped us opening a realistic UI."""


class CuaHubUnreachable(CuaHubError):
    """No usable response from the state API (down, wrong URL, non-JSON body).

    Deliberately distinct from CuaSeedMissing: "I can't reach it" and "it isn't
    seeded" need opposite fixes, and collapsing them into one message is why every
    task used to fail identically.
    """


class CuaSeedMissing(CuaHubError):
    """The state API answered, but there is no world under that seed SID."""

    def __init__(self, app_key: str, sid: str):
        super().__init__(f"{app_key}: no seeded world under sid {sid}")
        self.app_key = app_key
        self.sid = sid


@dataclass(frozen=True)
class MockApp:
    key: str         # annotator app key (matches the frontend APP_COLOR map)
    mock_key: str    # the `mock` column value in cua-gym / the hub's API segment
    subdomain: str   # cua-hub-<subdomain>.<domain>
    title: str
    start_path: str  # where the live browser lands (gmail is hash-routed)


# mock_key MUST match the gym's APP_TO_MOCK; the hub rejects an unknown mock name.
REGISTRY: dict[str, MockApp] = {
    "shop": MockApp("shop", "amazon_mock", "amazon", "ShopGym", "/"),
    "mail": MockApp("mail", "gmail_mock", "gmail", "ShopMail", "/#/inbox"),
    "market": MockApp("market", "ebay_mock", "ebay", "ValueMart", "/"),
    "calendar": MockApp("calendar", "google_calendar_mock", "google-calendar", "GymCal", "/"),
    "food": MockApp("food", "uber_eats_mock", "uber-eats", "GymEats", "/"),
}

APP_KEYS = list(REGISTRY)

# gym URL path segment -> app key (the shop lives at the root)
_SEG_TO_APP = {"": "shop", "mail": "mail", "food": "food",
               "market": "market", "valuemart": "market", "calendar": "calendar"}


def host(app_key: str) -> str:
    return f"cua-hub-{REGISTRY[app_key].subdomain}.{CUA_HUB_DOMAIN}"


def ui_base(app_key: str) -> str:
    """SPA origin — what the live browser opens. Resolved in the BROWSER's namespace."""
    override = os.environ.get(f"CUA_HUB_UI_URL_{app_key.upper()}")
    if override:
        return override.rstrip("/")
    return f"{CUA_HUB_SCHEME}://{host(app_key)}"


def api_base(app_key: str) -> str:
    """State-API base — what we GET /state and POST /post against.

    Resolved in the BACKEND's namespace, which is a different one from ui_base
    whenever the backend is containerised and the browser is not.
    """
    override = os.environ.get(f"CUA_HUB_API_URL_{app_key.upper()}")
    if override:
        return override.rstrip("/")
    root = os.environ.get("CUA_HUB_API_ROOT", CUA_HUB_API_ROOT).rstrip("/")
    return f"{root}/api/{REGISTRY[app_key].mock_key}"


def mock_url(app_key: str, sid: str | None = None, start_path: str | None = None,
             bridge: str | None = None, session: str | None = None) -> str:
    """The openable URL for one attempt.

    Query goes BEFORE any fragment — Gmail routes on the hash, and a sid parked
    after '#' never reaches the app. Passing bridge+session puts the tab in
    bridged mode; every tab of one session must carry the SAME session value or
    their cross-app effects land in different gym engines.
    """
    sp = start_path or REGISTRY[app_key].start_path
    if not sp.startswith(("/", "#", "?")):
        sp = "/" + sp
    parts = urlsplit(sp)
    params: dict[str, str] = {}
    if sid:
        params[CUA_HUB_SID_PARAM] = sid
    if bridge:
        params["bridge"] = bridge.rstrip("/")
    if session:
        params["session"] = session
    extra = urlencode(params)
    query = f"{parts.query}&{extra}" if parts.query and extra else (extra or parts.query)
    return ui_base(app_key) + urlunsplit(("", "", parts.path or "/", query, parts.fragment))


def allowed_sites(app_keys: list[str]) -> list[dict]:
    return [{"host": host(k), "app": REGISTRY[k].key, "title": REGISTRY[k].title}
            for k in app_keys if k in REGISTRY]


# --------------------------------------------------------------- task wiring ---
def is_cua_task(task) -> bool:
    """A gym task routed at the realistic UIs.

    Strictly gym-sourced: `external_id` is non-null on EVERY row (fixtures too),
    so testing it here would drag the fixture tasks into a path that cannot serve
    them.
    """
    return enabled() and task is not None and getattr(task, "source", None) == "gym"


def seed_sid(task_external_id: str, seed: int, app_key: str, rev: int | None = None) -> str:
    """Stable per-(task, seed, app) seed SID.

    MUST equal tools/cua_env.seed_sid in the gym repo — same namespace, same key
    format, same rev. Verified against tools/cua_task_sid_map.json.
    """
    r = seed_rev() if rev is None else rev
    return str(uuid.uuid5(NS_GYM, f"{task_external_id}|{seed}|{app_key}|r{r}"))


def primary_app(start_url: str | None) -> str:
    """Which app the live browser lands on, from the task's start-url segment."""
    seg = urlsplit(start_url or "").path.lstrip("/").split("/")[0].lower()
    return _SEG_TO_APP.get(seg, "shop")


# --------------------------------------------------------------- state API -----
def _http(method: str, url: str, body: dict | None = None, timeout: int = 20) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or "{}"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:200]
        except Exception:  # noqa: BLE001
            pass
        raise CuaHubUnreachable(f"{method} {url} -> HTTP {e.code} {detail}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise CuaHubUnreachable(f"{method} {url} -> {e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # The classic misconfiguration: pointed at the SPA instead of the state API.
        raise CuaHubUnreachable(
            f"{method} {url} returned non-JSON — is CUA_HUB_API_ROOT pointing at the SPA "
            f"origin instead of the state API?"
        ) from e


def _get_state(app_key: str, sid: str) -> dict:
    """Stored state for a sid, or raise CuaSeedMissing.

    `has_custom_state` is the gate: it is the one field both the hosted hub and
    the local vite middleware answer honestly. It matters because the mocks now
    ship a rich baked default — an unknown sid renders a complete but WRONG world
    rather than failing, so "did anything come back" is not a safe test.
    """
    out = _http("GET", f"{api_base(app_key)}/state?sid={sid}")
    if not out.get("has_custom_state"):
        raise CuaSeedMissing(app_key, sid)
    return out.get("stored_state") or {}


def _get_initial(app_key: str, sid: str) -> dict:
    """The FROZEN baseline for a sid — `/go`'s initial_state, not `/state`'s current.

    Current state drifts the moment anything opens the sid (the mocks POST their
    hydrated state on mount), so cloning from it would copy someone's drift into
    a new attempt. Gated behind _get_state because `/go` on an unknown sid returns
    the baked default locally.
    """
    _get_state(app_key, sid)          # proves the sid is really seeded
    go = _http("GET", f"{api_base(app_key)}/go?sid={sid}")
    return go.get("initial_state") or go.get("current_state") or {}


def _post_state(app_key: str, sid: str, state: dict, action: str = "set") -> None:
    _http("POST", f"{api_base(app_key)}/post?sid={sid}", {"action": action, "state": state})


def start_attempt(task_external_id: str, seed: int, apps: list[str] | None = None,
                  bridge: str | None = None, session: str | None = None) -> tuple[list[dict], list[dict]]:
    """PLAIN mode: clone each app's frozen seed into a fresh attempt SID.

    Used when there is no bridge. Returns (opened, failures) — failures carry a
    reason so a missing app shows as "ShopMail unavailable: not seeded" instead of
    silently vanishing from the tab strip.

    Bridged sessions do NOT use this: the bridge generates the world from a real
    gym reset and pushes it to the attempt SIDs itself (see attempt_sids/apps_for).
    """
    opened: list[dict] = []
    failures: list[dict] = []
    for app in (apps or APP_KEYS):
        if app not in REGISTRY:
            continue
        try:
            state = _get_initial(app, seed_sid(task_external_id, seed, app))
            attempt = str(uuid.uuid4())
            _post_state(app, attempt, state)
        except CuaHubError as e:
            failures.append({"app": app, "title": REGISTRY[app].title, "error": str(e)})
            continue
        opened.append(_app_entry(app, attempt, bridge=bridge, session=session))
    return opened, failures


def attempt_sids(apps: list[str] | None = None) -> dict[str, str]:
    """Fresh per-attempt SIDs — one isolated world per annotator, per app.

    Use this for a world that is genuinely throwaway (a certify scratch session).
    For an annotator's own attempt use `attempt_sids_for`, or reopening the task
    abandons the world they built.
    """
    return {app: str(uuid.uuid4()) for app in (apps or APP_KEYS) if app in REGISTRY}


def attempt_sids_for(attempt_id, apps: list[str] | None = None) -> dict[str, str]:
    """Per-(attempt, app) SIDs that are the SAME every time this attempt opens.

    The annotator's world lives in `mock_states`, keyed by these SIDs. Minting
    fresh uuid4s on every open — which is what happened before — pointed the
    reopened tabs at empty rows, so leaving a task and coming back silently threw
    the work away and leaked five rows per reopen.

    Derived (uuid5) rather than stored, so they survive `cua_apps` being lost and
    are still globally unique: the attempt id is itself a uuid4. Includes the seed
    rev so a rev bump gives every attempt a clean set, exactly like `seed_sid`.
    """
    return {
        app: str(uuid.uuid5(NS_GYM, f"attempt|{attempt_id}|{app}|r{seed_rev()}"))
        for app in (apps or APP_KEYS) if app in REGISTRY
    }


def _app_entry(app: str, sid: str, start_path: str | None = None,
               bridge: str | None = None, session: str | None = None) -> dict:
    m = REGISTRY[app]
    return {
        "app": app, "mock_key": m.mock_key, "title": m.title, "attempt_sid": sid,
        "start_path": start_path or m.start_path,
        "url": mock_url(app, sid=sid, start_path=start_path, bridge=bridge, session=session),
    }


def apps_for(sids: dict[str, str], start_paths: dict[str, str] | None = None,
             bridge: str | None = None, session: str | None = None) -> list[dict]:
    """Per-app open info for an already-provisioned set of attempt SIDs."""
    sp = start_paths or {}
    return [_app_entry(app, sid, start_path=sp.get(app), bridge=bridge, session=session)
            for app, sid in sids.items() if app in REGISTRY]


def end_attempt(apps: list[dict]) -> None:
    """Discard an attempt's worlds. Best-effort — teardown must never raise.

    Without this every session leaks a row into the shared cua-gym Postgres
    forever.
    """
    for a in apps or []:
        app, sid = a.get("app"), a.get("attempt_sid")
        if not app or not sid or app not in REGISTRY:
            continue
        try:
            _http("POST", f"{api_base(app)}/post?sid={sid}", {"action": "reset"})
        except CuaHubError:
            continue
