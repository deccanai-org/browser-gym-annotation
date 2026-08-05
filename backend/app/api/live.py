"""Live browser sessions — the ticket the pane cannot mint for itself.

A live stream is a remote-control channel, so the live browser service authorises
it with an HMAC ticket signed over ``session_id:owner:exp``. The pane cannot
produce one: minting needs the signing secret AND knowledge of who is signed in,
and only this process has both. Until something mints tickets, the pane has no
session id and no ticket, so it cannot be used at all.

Two rules here are the difference between a working pane and a frozen one:

* The owner is the signed-in annotator's email. The service closes the socket
  4401 when a ticket's owner is not the owner its session was opened for, and a
  socket closed after a successful handshake looks exactly like a hung stream.
* One browser per attempt, re-attached rather than re-opened. A reload that
  opened its own Chromium would leak one per refresh, which exhausts the box long
  before an annotator finishes a task.
"""

from __future__ import annotations

import urllib.parse

import base64
from datetime import datetime, timezone
import contextlib
import dataclasses
import logging
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import (
    bridge_client, checkpoints, cua_hub, gym_client, live_world, models, restore, versions, workspace,
)
from app.api.sessions import _owned_session
from app.auth import current_annotator
from app.config import settings
from app.db import get_db

def _browser_visible(base_url: str) -> str:
    """The gym URL as the BROWSER will see it, which is not how the backend sees it.

    The live browser runs in a different network namespace from this process — in
    the shipped setup the backend is containerised and reaches the gym at
    `host.docker.internal`, while the browser runs on the host, where that name
    does not resolve at all. Handing our own URL straight over produces
    ERR_NAME_NOT_RESOLVED and a pane that never paints.

    Only the HOST is rewritten, never the port: workspace isolation gives each
    attempt its own gym port on the same machine, and that port is the whole
    point of the isolation. Unset means "same namespace", which is correct for a
    single-host dev box and keeps this a no-op there.
    """
    parsed = urllib.parse.urlsplit(base_url)
    host = settings.gym_host_for_browser
    netloc = parsed.netloc
    if host:
        netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    # Preserve the PATH. Callers now pass a task's own start URL (M46 begins on
    # /cart), and normalising every URL to a trailing slash turned that into
    # "/cart/" — a different route.
    #
    # Preserve the FRAGMENT too: this also rewrites the realistic mock URLs now,
    # and ShopMail is hash-routed (`/?sid=…#/inbox`). Dropping the fragment lands
    # the annotator on the app root instead of the task's start route.
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, path, parsed.query, parsed.fragment))


log = logging.getLogger("annotator.live")

router = APIRouter(prefix="/api", tags=["live"])

# Resolved EXACTLY as live_browser/service.py resolves it, fallback chain
# included. A ticket signed with a different secret is not refused loudly — the
# handshake completes and the socket is then closed 4401 — so any divergence here
# reads to the annotator as a permanently blank pane with no error anywhere.
LIVE_STREAM_SECRET = os.getenv("LIVE_STREAM_SECRET", os.getenv("HARNESS_TOKEN", "dev-live-secret"))
LIVE_TICKET_TTL_S = int(os.getenv("LIVE_TICKET_TTL_S", "300"))

# Only used if a response omits it; the service reports its own viewport, which is
# what the pane must scale against.
_DEFAULT_VIEWPORT = {"width": 1280, "height": 800}


def _mint_ticket(live_session_id: str, owner: str) -> str:
    """Sign a short-lived ticket for ONE session and ONE owner.

    The owner is base64url-encoded because owners are emails: a dot-delimited
    ticket splits an address across the signature fields and validates as
    somebody else.
    """
    exp = int(time.time()) + LIVE_TICKET_TTL_S
    sig = hmac.new(
        LIVE_STREAM_SECRET.encode(), f"{live_session_id}:{owner}:{exp}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f"{exp}.{sig}.{base64.urlsafe_b64encode(owner.encode()).decode().rstrip('=')}"


# --------------------------------------------------------------------------- what is open
@dataclass(frozen=True)
class _Attached:
    """The live browser currently open for one attempt."""

    live_session_id: str
    owner: str
    url: str
    viewport: dict
    # Whether this attempt got its OWN gym. Surfaced so an annotator can tell a
    # private world from the shared one rather than finding out by collision.
    isolated: bool = False
    # Where the world came from on this open: "preserved" (their own work is still
    # there), "seeded" (reset to the task seed), "shared" (not a gym task, or the
    # shared gym). Surfaced because a person cannot otherwise tell whether their
    # cart survived except by looking for it.
    world: str = "shared"
    # How far a fork's prefix was rebuilt, or None when there was nothing to
    # rebuild. None and "rebuilt 0 of 9" are different facts and must not render
    # the same.
    restore: dict | None = None
    # cua-hub mode only: the cloned per-app attempt SIDs + open URLs (the tabs).
    cua_apps: list | None = None
    # Set when a SAVED world was put back on this open: {step, at, exact}. None
    # means "freshly seeded". `exact` is reported rather than assumed — handing
    # an annotator a world that is subtly not the one they left is the failure
    # worth being loud about.
    resumed: dict | None = None


# Process memory rather than a WorkspaceLease row, deliberately. A lease describes
# a GYM runtime keyed by pid, and reap_expired() / reconcile_on_startup() walk
# every active lease regardless of purpose: they health-check it with
# GET /_harness/tasks, which a browser service does not serve, then reclaim it
# through the local-process provider by SIGTERMing int(external_ref). A live
# session id is not a pid, so such a row would be declared dead on the first sweep
# and "reclaimed" by killing whatever process holds that number.
#
# The durability rule then holds by construction: this map dies with the process,
# so a restarted backend has nothing to re-attach and cannot hand out a ticket for
# a browser it no longer tracks. The opposite drift — the live service restarting
# under us — is caught by asking it about the session before every re-attach and
# forgetting the entry when it reports it gone.
_ATTACHED: dict[str, _Attached] = {}
# Opening is slow (a real Chromium launch, and under isolation a gym container
# boot on top of it). Two concurrent opens for ONE attempt would leave a second
# browser running that nobody holds the id for — so opens are serialised, but
# PER ATTEMPT rather than globally.
#
# The distinction is not academic once isolation is on. A global lock would make
# every annotator's open queue behind whatever container is currently booting,
# which turns the feature that exists to let people work simultaneously into the
# thing that stops them. Nothing about attempt A's open is a hazard for attempt
# B; the only true invariant is one open at a time per attempt.
_ATTACH_LOCKS: dict[str, threading.Lock] = {}
# Guards the lock TABLE only — held for a dict lookup, never across slow work.
_LOCK = threading.Lock()


def _attempt_lock(attempt_id: str) -> threading.Lock:
    with _LOCK:
        return _ATTACH_LOCKS.setdefault(attempt_id, threading.Lock())


# --------------------------------------------------------------------------- the service
def _live_request(method: str, path: str, body: dict | None = None, timeout: int = 45) -> tuple[int, dict]:
    """One call to the live browser service, status included.

    An unreachable service is a 409 rather than a 500: it is our infrastructure
    being down, and the annotator has to be told that instead of being handed a
    session id that will never stream.
    """
    url = settings.live_browser_url.rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200, (json.loads(r.read() or b"{}") or {})
    except urllib.error.HTTPError as e:
        with contextlib.suppress(ValueError, json.JSONDecodeError):
            return e.code, (json.loads(e.read() or b"{}") or {})
        return e.code, {}
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=409,
            detail=f"the live browser service at {settings.live_browser_url} is unreachable ({exc})",
        ) from exc


def _open_browser(url: str, owner: str) -> dict:
    status, payload = _live_request("POST", "/live/sessions", {"url": url, "owner": owner})
    if status != 200 or not payload.get("session_id"):
        raise HTTPException(
            status_code=409,
            detail=f"the live browser service could not open a browser: {payload.get('detail') or status}",
        )
    return payload


def _task_start_url(gym_base: str, task_start_url: str) -> str:
    """Where the browser should land, as an absolute URL on THIS attempt's gym.

    Two things make this less trivial than it looks, and both were measured:

    * `task.start_url` is stored inconsistently — 270 of 312 tasks hold a relative
      path ("/cart"), 20 hold an absolute URL. Handing a bare path to Playwright's
      goto() fails outright, so the path has to be resolved against a base.
    * When it IS absolute it names the SHARED gym, which is the wrong host for an
      attempt holding an isolated workspace. So only the path and query are taken
      from the task; the host always comes from the attempt's own endpoint.
    """
    base = gym_base.rstrip("/")
    if not task_start_url:
        return base + "/"
    parsed = urllib.parse.urlsplit(task_start_url)
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return base + urllib.parse.urlunsplit(("", "", path, parsed.query, ""))


def browser_visible_gym_url(base_url: str) -> str:
    """The gym URL as the BROWSER sees it — see _browser_visible."""
    return _browser_visible(base_url)


def open_scratch_browser(start_url: str, owner: str) -> tuple[str, str]:
    """A short-lived browser for a server-side replay, returned as (id, ticket).

    Finalization has to EXECUTE the trajectory, which needs a real browser — it
    previously invented a session id and an empty ticket, so every finalize failed
    with "live browser unreachable" and nothing could ever ship. Callers must
    close_scratch_browser() in a finally, or each finalize leaks a Chromium.
    """
    opened = _open_browser(start_url, owner)
    return opened["session_id"], opened["ticket"]


def close_scratch_browser(live_session_id: str) -> None:
    """Reclaim it. Never raises — a finalize that succeeded must not be reported
    as failed because the teardown blipped."""
    with contextlib.suppress(Exception):
        _live_request("POST", f"/live/sessions/{live_session_id}/close", {}, timeout=10)


def _browser_info(live_session_id: str) -> dict | None:
    """What the service still knows about this browser — None once it is gone."""
    status, payload = _live_request("GET", f"/live/sessions/{live_session_id}", timeout=10)
    return payload if status == 200 else None


def _ticket_accepted(live_session_id: str, ticket: str) -> bool:
    """Have the service honour the ticket before a client tries to stream with it.

    Only an explicit 403 is evidence about the ticket. A page that happens to be
    mid-navigation can fail this probe for reasons that have nothing to do with
    the signature, and calling that a secret mismatch would be a false diagnosis.
    """
    status, _ = _live_request("POST", f"/live/sessions/{live_session_id}/focused", {"ticket": ticket}, timeout=10)
    return status != 403


def _reattach(entry: _Attached) -> dict | None:
    """A FRESH ticket for a browser that is already open, or None if it is gone.

    Re-issuing rather than replaying the ticket minted at open time is what makes
    a reload cheap: tickets expire after LIVE_TICKET_TTL_S, and without this every
    reload past that window would have to burn a whole new browser.
    """
    info = _browser_info(entry.live_session_id)
    if info is None:
        return None
    ticket = _mint_ticket(entry.live_session_id, entry.owner)
    if not _ticket_accepted(entry.live_session_id, ticket):
        raise HTTPException(
            status_code=409,
            detail="the live browser service rejected a ticket minted here — LIVE_STREAM_SECRET "
                   "does not match the secret the service is running with",
        )
    return {
        "sessionId": entry.live_session_id,
        "ticket": ticket,
        "viewport": info.get("viewport") or entry.viewport,
        "url": entry.url,
        "isolated": entry.isolated,
        # Re-attaching touches no world: this is the same browser on the same gym,
        # re-ticketed. So whatever the world was at the last open, it is still that
        # plus whatever the annotator has since done to it — which is "preserved",
        # not the "seeded" this attachment was born with. Reporting the birth state
        # would tell someone who has been working for an hour that their world was
        # just reset, and the plausible reaction to that is to redo the work.
        "world": "preserved" if entry.world != "shared" else "shared",
        "restore": entry.restore,
        "apps": entry.cua_apps,
        "resumed": entry.resumed,
    }


def _rebuild_prefix(db, s, *, lease, endpoint, session_id: str, ticket: str) -> "restore.RestoreReport":
    """Replay this attempt's branch prefix into a freshly seeded world and record
    how far it got. A no-op that returns an empty report when the attempt is not a
    fork. The executor is the pane's OWN browser, so the replay leaves the page
    showing the rebuilt state rather than the start URL.

    Both the open and the reset-world paths establish a fresh world the same way;
    this is the shared half so the two handlers do not each re-derive it.
    """
    report = restore.restore_prefix(
        db, s,
        executor=gym_client.LiveBrowserClient(
            base_url=settings.live_browser_url, session_id=session_id, ticket=ticket, gym=endpoint,
        ),
        gym=endpoint,
    )
    restore.record(db, lease, report)
    return report


def _capture_initial_world(db: Session, s) -> None:
    """Snapshot the SEEDED world and make it v1's restore point.

    Two jobs at once, and both are load-bearing:
      * it is the `initial` world a verifier suite is gated against (a check that
        already holds here discriminates nothing), and
      * it is the root version's `fork_checkpoint_id`, without which the replay
        gate has no state to restore to.

    Best-effort: a world we could not read must not stop the annotator working —
    it costs them the gate, not the task, so it is logged rather than raised.
    """
    try:
        world = live_world.world_for(db, s).world()
    except Exception as exc:  # noqa: BLE001 — never block the open on a world read
        log.warning("initial world unavailable for %s (%s)", s.id, exc)
        world = None
    cp = None
    if world is not None:
        cp = checkpoints.capture(db, attempt_id=s.id, world=world, step_clock=0)
        s.initial_checkpoint_id = cp.id
    versions.ensure_manual_root(db, s, fork_checkpoint_id=cp.id if cp is not None else None)


def _resume_checkpoint(db: Session, s) -> models.EnvironmentCheckpoint | None:
    """The newest checkpoint worth restoring — i.e. actual work, not the seed.

    `initial_checkpoint_id` is the seeded world the attempt started from, so
    restoring it would be an expensive no-op. Anything later is the annotator's.
    """
    cp = db.scalar(
        select(models.EnvironmentCheckpoint)
        .where(models.EnvironmentCheckpoint.attempt_id == s.id)
        .order_by(models.EnvironmentCheckpoint.step_clock.desc(),
                  models.EnvironmentCheckpoint.created_at.desc())
    )
    if cp is None or (s.initial_checkpoint_id and cp.id == s.initial_checkpoint_id):
        return None
    return cp


def _resume_world(db: Session, s, task, gym_url: str, bridge_session: str) -> dict | None:
    """Restore the annotator's world into the gym that was just leased.

    Best-effort by design: a world we cannot restore must leave them with a
    freshly seeded task they can still work, not a 500 on open. But the outcome
    is REPORTED (`exact`) rather than swallowed — silently handing someone a
    different world than the one they left is the failure worth being loud about.
    """
    cp = _resume_checkpoint(db, s)
    if cp is None:
        return None
    world = live_world.BridgedWorld(gym_url, bridge_session)
    try:
        ok = checkpoints.restore(cp, world, task_id=task.external_id, seed=s.seed,
                                 # A mismatch must degrade to a seeded world with a
                                 # visible badge, not raise DivergenceError on open.
                                 verify=False)
        if not ok:
            return None
        live = checkpoints.hash_world(world.world())
        # The engine holds their world again; the hub still holds the seed
        # projection, so re-project or the tabs render the seed until first click.
        bridge_client.repush(bridge_session, step=int(cp.step_clock or 0))
        return {"step": int(cp.step_clock or 0),
                "at": cp.created_at.isoformat() if cp.created_at else "",
                "exact": bool(cp.world_hash) and live == cp.world_hash}
    except Exception as exc:  # noqa: BLE001 — never block the open on a restore
        log.warning("resume failed for attempt %s (%s) — seeded instead", s.id, exc)
        return None


def _cua_start_paths(task) -> dict[str, str]:
    """Per-app landing route, precomputed by the gym's own mapper at export time.

    Only the primary app honours the task's deep link (M310 starts on
    /subscriptions); the rest open at their own root.
    """
    apps_meta = ((task.meta or {}).get("apps") or {}) if isinstance(task.meta, dict) else {}
    return {app: (cfg or {}).get("start_path") or "/" for app, cfg in apps_meta.items()}


def _open_cua_session(db: Session, s, task, current: models.Annotator) -> dict:
    """Open the live browser on the task's realistic UI, BRIDGED to the real gym.

    Bridged is the whole point: every click is forwarded to a gym instance leased
    from the pool, which applies the engine's rules and re-projects the world into
    all five mocks. That is what makes cross-app effects real (an order in ShopGym
    produces the confirmation email in ShopMail) and what lets the gym's own
    milestone suite score the annotator's work.

    The bridge seeds the attempt itself — `/bridge/{sid}/open` resets the leased
    gym and baselines all five attempt SIDs from it — so nothing here depends on
    the frozen seed SIDs having been pre-written by the gym-side batch. Falls back
    to a plain per-app clone when no bridge is configured.
    """
    start_paths = _cua_start_paths(task)
    primary_key = ((task.meta or {}).get("primaryApp") if isinstance(task.meta, dict) else None) \
        or cua_hub.primary_app(task.start_url)
    bridge_session = str(s.id)          # stable across re-attach; the bridge keys on it
    gym_url = ""
    resumed: dict | None = None         # set when a saved world was put back

    if bridge_client.enabled():
        # The SAME SIDs every time this attempt opens. The annotator's world lives
        # in `mock_states` under these keys, so minting fresh ones (the old
        # behaviour) pointed the reopened tabs at empty rows — leaving a task and
        # coming back threw the work away. Prefer what was persisted; fall back to
        # the derived set when `cua_apps` is missing.
        persisted = {a["app"]: a["attempt_sid"] for a in (s.cua_apps or [])
                     if a.get("app") and a.get("attempt_sid")}
        sids = persisted or cua_hub.attempt_sids_for(s.id)
        try:
            out = bridge_client.open_session(bridge_session, task.external_id, s.seed, sids)
        except bridge_client.BridgePoolExhausted as exc:
            raise HTTPException(
                status_code=503,
                detail=(f"every gym in the bridge pool is busy — {exc}. One annotator "
                        f"needs one gym; try again shortly."),
            ) from exc
        except bridge_client.BridgeError as exc:
            raise HTTPException(status_code=409, detail=f"could not start the gym for this task: {exc}") from exc
        gym_url = str(out.get("gym_url") or "")
        # Put the annotator's world back. The bridge just reset this gym to the
        # task seed, so without this a returning annotator finds their work gone.
        # Skipped when the bridge REUSED a live session: the world is already
        # theirs and restoring would rewind it.
        if not out.get("reused"):
            resumed = _resume_world(db, s, task, gym_url, bridge_session)
        # The bridge URL is baked into the mock's query string, so it is resolved
        # by the BROWSER, not by us. Ours points at host.docker.internal, which
        # does not resolve on the host — the tab would silently fall back to
        # local-store mode (no engine, no cross-app effects) with nothing to see.
        apps = cua_hub.apps_for(sids, start_paths=start_paths,
                                bridge=_browser_visible(bridge_client.base_url()),
                                session=bridge_session)
    else:
        # No bridge: clone each app's frozen seed. No engine, so no cross-app
        # effects and no milestone verdict — the annotator must be told which of
        # the two worlds they are in, hence `world` below.
        apps, failures = cua_hub.start_attempt(task.external_id, s.seed)
        if not apps:
            why = "; ".join(f"{f['app']}: {f['error']}" for f in failures) or "no apps seeded"
            raise HTTPException(
                status_code=409,
                detail=(f"the realistic UIs are not available for {task.external_id} — {why}"),
            )
        for a in apps:                    # honour the task's landing route
            a["start_path"] = start_paths.get(a["app"], a.get("start_path") or "/")
            a["url"] = cua_hub.mock_url(a["app"], sid=a["attempt_sid"], start_path=a["start_path"])
        bridge_session = ""

    primary = next((a for a in apps if a["app"] == primary_key), apps[0])
    # The browser resolves URLs in its OWN namespace, which is not this process's
    # when the backend is containerised and the browser is not.
    for a in apps:
        a["url"] = _browser_visible(a["url"])
    start_url = primary["url"]
    opened = _open_browser(start_url, current.email)
    entry = _Attached(
        live_session_id=str(opened["session_id"]),
        owner=current.email,
        url=start_url,
        viewport=opened.get("viewport") or _DEFAULT_VIEWPORT,
        isolated=True,        # each attempt is its own cloned world
        world="cua-hub",
        restore=None,
        cua_apps=apps,
        resumed=resumed,
    )
    _ATTACHED[str(s.id)] = entry

    # PERSIST the world this attempt owns. _ATTACHED is process memory by design
    # (it tracks a browser, not a gym), but the attempt SIDs and the leased gym
    # are durable facts: without them a restart orphans the annotator's world, and
    # `world_for` cannot tell a bridged attempt from a workspace one — which is
    # how bridged attempts used to fall through to the SHARED gym.
    s.bridge_session_id = bridge_session
    s.bridge_gym_url = gym_url
    s.cua_apps = apps
    s.mode = "human_do"
    if s.started_at is None:
        s.started_at = datetime.now(timezone.utc)

    # The SEEDED world, captured before the annotator touches anything. It is one
    # of the two worlds Feature 2 scores against, and it is the root version's
    # restore point — `versions.create_root` never sets one, which is why the
    # replay gate was a no-op in production.
    if bridge_session:
        _capture_initial_world(db, s)

    db.add(models.AuditLog(
        session_id=s.id, actor=current.email, action="live.open",
        target=entry.live_session_id,
        meta={"url": start_url, "world": "cua-hub", "apps": [a["app"] for a in apps],
              "bridged": bool(bridge_session), "gymUrl": gym_url},
    ))
    db.commit()
    return {
        "sessionId": entry.live_session_id,
        "ticket": opened["ticket"],
        "viewport": entry.viewport,
        "url": entry.url,
        "isolated": entry.isolated,
        "world": entry.world,
        "restore": entry.restore,
        "apps": apps,
        "resumed": entry.resumed,
    }


# --------------------------------------------------------------------------- routes
@router.post("/sessions/{session_id}/live")
def open_live_session(
    session_id: UUID, current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db)
) -> dict:
    """Open the live browser for this attempt, or re-attach to the open one.

    The client sends nothing: both the owner and the start URL are decisions the
    server has to own. The owner must be the signed-in annotator or the stream
    dies silently, and the URL must be this attempt's own workspace gym — an
    isolated annotator pointed at the shared gym would be driving a world that
    belongs to somebody else.
    """
    s = _owned_session(db, session_id, current)
    with _attempt_lock(str(s.id)):
        entry = _ATTACHED.get(str(s.id))
        payload = _reattach(entry) if entry is not None else None
        if payload is not None:
            # Extend the inactivity window. Now that a workspace holds real hand-built
            # work rather than a disposable seed world, letting the reaper reclaim it
            # under an annotator who is actively using it destroys that work — and
            # acquire() was the only caller, so an uninterrupted session never renewed.
            _touch_workspace(db, s.id)
            return payload

        _ATTACHED.pop(str(s.id), None)

        # --- cua-hub mode: a gym task rendered on the external realistic UI. The
        # mock owns its state, so this bypasses the gym lease / seed / restore
        # machinery below and just opens the seeded mock for this attempt. ---
        _cua_task = db.get(models.Task, s.task_id)
        if cua_hub.is_cua_task(_cua_task):
            return _open_cua_session(db, s, _cua_task, current)

        # ACQUIRE this attempt's own gym before resolving the endpoint.
        # `endpoint_for` only READS an existing lease — it never provisions — so
        # without this every annotator silently shares one gym, which is exactly
        # the corruption isolation exists to prevent. Falling back is deliberate
        # (a docker hiccup must not stop someone working) but must be VISIBLE, so
        # the response says which world they got.
        isolated = False
        # Predeclared: the generic `except` below leaves this unbound on exactly
        # the shared-gym fallback path, which is the path that must behave most
        # conservatively — reading it there would raise NameError instead of
        # falling back.
        lease = None
        try:
            lease = workspace.acquire(db, s.id, annotator_id=current.id)
            isolated = lease is not None and lease.status == "ready"
        except workspace.WorkspaceCapacityError as exc:
            # NOT a fallback case. The cap exists to stop one annotator filling
            # the host; quietly handing them the shared gym instead would put
            # them in someone else's world, which is the exact failure isolation
            # is here to prevent. Tell them to close something.
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — provisioning is best-effort
            log.warning("workspace provisioning failed for %s (%s) — using the shared gym", s.id, exc)

        endpoint = workspace.endpoint_for(db, s.id)
        task = db.get(models.Task, s.task_id)

        # SEED THE WORLD — unless this workspace already holds it.
        #
        # The gym keeps one global session per process and whatever the last
        # caller left in it, so seeding is what stops an annotator driving a
        # browser showing some other task's world entirely. Observed: someone
        # opened M46/sneaked_addon ("check out the keyboard in my cart") and got
        # an empty cart and a different task's on-page brief, because the last
        # thing to touch the gym was M15.
        #
        # But seeding UNCONDITIONALLY is its own bug once each attempt owns a
        # long-lived workspace: the container survives a pane close, so an
        # annotator who flips to the replay view and back was having an hour of
        # hand-built world silently reset to the task seed. Reuse the world when —
        # and only when — our own record and the gym's own answer agree it is the
        # one we seeded for this attempt. Every other case reseeds, including the
        # shared-gym fallback, where the world may belong to somebody else.
        world_state = "shared"
        if task is not None and task.external_id and (task.source == "gym" or s.source == "gym"):
            # Which version's world this is. A fork's world is the fork point of ONE
            # version, and versions of an attempt share a (task, seed), so the reuse
            # decision has to be version-aware or switching versions silently keeps
            # the previous branch's world. head_id is version 1's own id for an
            # unforked attempt (a real row) and None only when the attempt has no
            # versions at all — either way it is simply "the world we built for".
            head = versions.head(db, s)
            head_id = head.id if head is not None else None
            reusable = isolated and workspace.holds_seeded_world(
                lease, endpoint, task_key=task.external_id, seed=s.seed, version_id=head_id
            )
            if reusable:
                world_state = "preserved"
            else:
                result = endpoint.reset(task.external_id, s.seed)
                if result is None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"could not seed the gym for {task.external_id} — the live browser would "
                            "show the wrong world, so it was not opened"
                        ),
                    )
                world_state = "seeded"
                # AFTER the reset returned, never before or alongside it. A marker
                # written optimistically makes the next open a "reuse" of a world
                # that was never built — and the gym lazily fabricates an unrelated
                # default world the moment a page loads, so the annotator would get
                # the wrong brief and an empty cart with no error anywhere.
                if lease is not None:
                    workspace.mark_seeded(
                        db, lease, task_key=task.external_id, seed=s.seed,
                        reset_result=result, version_id=head_id,
                    )

        # …and land where the TASK starts, not at the gym root. M46 begins on
        # /cart; dropping the annotator on the home page makes them navigate to
        # the state the task already guaranteed them.
        start_url = _browser_visible(
            _task_start_url(endpoint.base_url, task.start_url if task is not None else "")
        )
        opened = _open_browser(start_url, current.email)
        ticket = opened["ticket"]

        # REBUILD A FORK'S WORLD. Only on the branch that just reset — a preserved
        # world already holds the annotator's work and replaying into it would put
        # the prefix on top of itself. The browser has to exist first: the prefix is
        # replayed THROUGH it, which is also what leaves the page showing the state
        # the actions produced rather than the start URL.
        if world_state == "seeded":
            report = _rebuild_prefix(
                db, s, lease=lease, endpoint=endpoint,
                session_id=str(opened["session_id"]), ticket=ticket,
            )
            if report.attempted:
                # The rebuild can burn a real share of LIVE_TICKET_TTL_S. Handing
                # back the ticket minted before it means a socket opened seconds
                # later can close 4401, which is terminal in the client and whose
                # only cure is a full round trip through the replay pane.
                ticket = _mint_ticket(str(opened["session_id"]), current.email)
        else:
            report = restore.report_of(lease)

        entry = _Attached(
            live_session_id=str(opened["session_id"]),
            owner=current.email,
            url=start_url,
            viewport=opened.get("viewport") or _DEFAULT_VIEWPORT,
            isolated=isolated,
            world=world_state,
            restore=restore.as_payload(report),
        )
        _ATTACHED[str(s.id)] = entry

    db.add(models.AuditLog(
        session_id=s.id, actor=current.email, action="live.open",
        target=entry.live_session_id,
        meta={"url": start_url, "world": world_state, "restore": entry.restore},
    ))
    db.commit()
    return {
        "sessionId": entry.live_session_id,
        "ticket": ticket,
        "viewport": entry.viewport,
        "url": entry.url,
        "isolated": entry.isolated,
        "world": entry.world,
        "restore": entry.restore,
    }


@router.get("/sessions/{session_id}/live")
def live_session(
    session_id: UUID, current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db)
) -> dict:
    """What is open for this attempt right now, with a fresh ticket.

    A reloading page asks this first so it re-attaches to its browser instead of
    opening one it has no id for — that browser would keep streaming to nobody
    until the service is restarted.
    """
    s = _owned_session(db, session_id, current)
    entry = _ATTACHED.get(str(s.id))
    if entry is None:
        return {"session": None}
    payload = _reattach(entry)
    if payload is None:
        with _attempt_lock(str(s.id)):
            _ATTACHED.pop(str(s.id), None)
        return {"session": None}
    return {"session": payload}


@router.post("/sessions/{session_id}/live/close")
def close_live_session(
    session_id: UUID, current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db)
) -> dict:
    """Give the browser back. Closing twice is not an error — a client that has
    already been disconnected got what it asked for."""
    s = _owned_session(db, session_id, current)
    with _attempt_lock(str(s.id)):
        entry = _ATTACHED.pop(str(s.id), None)
    if entry is None:
        return {"closed": True}

    # Forget it whether or not the service answers. Holding the attachment open
    # because the close call failed strands the attempt on a browser nobody can
    # reach, with no way to open a working one.
    with contextlib.suppress(HTTPException):
        _live_request("POST", f"/live/sessions/{entry.live_session_id}/close", {}, timeout=10)

    # Closing is a SUSPEND, so snapshot the world before letting go of it.
    # `materialize` checkpoints after each batch of events, but the seconds
    # between the last batch and the close would otherwise be lost — and this is
    # the checkpoint the next open restores from, so it is what makes "my exact
    # world as I left it" true rather than approximately true.
    with contextlib.suppress(Exception):
        port = live_world.world_for(db, s)
        world = port.world()
        if world:
            # `world` (compact) keeps the hash basis every recorded step already
            # uses; `backend_state` carries the COMPLETE world, which is what a
            # faithful restore needs — the compact view drops per-product stock,
            # so restoring from it alone would restock what the annotator bought.
            full = None
            with contextlib.suppress(Exception):
                full = port.world_full()
            cp = checkpoints.capture(db, attempt_id=s.id, world=world,
                                     world_full=full,
                                     step_clock=int(world.get("step") or 0))
            s.final_checkpoint_id = cp.id

    # Release the pooled gym. Closing only the Chromium leaked the bridge lease —
    # every task an annotator opened held one of the (few) gym instances forever,
    # so after a couple of tasks every /live 503'd and the board looked empty. The
    # lease belongs to the working session, so give it back when the session ends;
    # the checkpoint above is what the next open rebuilds their world from.
    if s.bridge_session_id:
        with contextlib.suppress(Exception):
            bridge_client.close_session(s.bridge_session_id)
        s.bridge_session_id = ""
        s.bridge_gym_url = ""
    db.add(models.AuditLog(
        session_id=s.id, actor=current.email, action="live.close", target=entry.live_session_id, meta={},
    ))
    db.commit()
    return {"closed": True}


def _touch_workspace(db: Session, attempt_id: UUID) -> None:
    """Best-effort renewal of this attempt's workspace lease."""
    with contextlib.suppress(Exception):
        lease = workspace.active_lease(db, attempt_id)
        if lease is not None:
            workspace.touch(db, lease)


@router.post("/sessions/{session_id}/live/reset-world")
def reset_live_world(
    session_id: UUID, current: models.Annotator = Depends(current_annotator), db: Session = Depends(get_db)
) -> dict:
    """Throw this attempt's world away and rebuild it from the task seed.

    The deliberate counterpart to preserving a world across a reopen. Closing and
    reopening the pane used to be how an annotator started over — implicitly, and
    destructively, every single time. Now that reopening keeps their work, that
    escape hatch has to exist explicitly, or someone who has driven their world
    into a corner has no way out of it.

    Its own route rather than a flag on the open call: opening a pane and
    discarding an hour of work are not the same request, and the client has never
    been the thing that decides which world an attempt gets.
    """
    s = _owned_session(db, session_id, current)
    task = db.get(models.Task, s.task_id)
    if task is None or not task.external_id or not (task.source == "gym" or s.source == "gym"):
        raise HTTPException(status_code=400, detail="this attempt has no gym world to reset")

    with _attempt_lock(str(s.id)):
        lease = workspace.active_lease(db, s.id)
        # world_for, not endpoint_for: a bridged attempt has no lease, and
        # resetting the SHARED gym here would wipe an unrelated annotator's world.
        endpoint = live_world.world_for(db, s)
        # A released gym belongs to whoever leased it next; resetting it would
        # destroy their in-progress world. Refuse rather than reset a stranger's.
        if getattr(endpoint, "kind", "") == "unleased":
            raise HTTPException(
                status_code=409,
                detail="this attempt's gym is not open — reopen the task, then reset",
            )
        head = versions.head(db, s)
        head_id = head.id if head is not None else None

        # Clear the marker BEFORE resetting, and commit it. If the reset then fails
        # or the process dies between the two, the next open re-seeds — which is the
        # harmless direction. Clearing afterwards would leave a marker vouching for
        # a world that was half torn down.
        workspace.clear_seed_mark(db, lease)

        result = endpoint.reset(task.external_id, s.seed)
        if result is None:
            raise HTTPException(
                status_code=409,
                detail=f"could not reset the gym for {task.external_id} — the world was left as it was",
            )
        if lease is not None:
            workspace.mark_seeded(
                db, lease, task_key=task.external_id, seed=s.seed,
                reset_result=result, version_id=head_id,
            )

        # Point the open browser at the task's start URL again, so the annotator
        # sees the fresh world instead of a page rendered from the old one.
        entry = _ATTACHED.get(str(s.id))
        report = restore.RestoreReport()
        if entry is not None:
            start_url = _browser_visible(_task_start_url(endpoint.base_url, task.start_url or ""))
            with contextlib.suppress(HTTPException):
                _live_request(
                    "POST", f"/live/sessions/{entry.live_session_id}/act",
                    {
                        "kind": "navigate",
                        "locator": {},
                        "args": {"url": start_url},
                        "ticket": _mint_ticket(entry.live_session_id, entry.owner),
                    },
                    timeout=30,
                )
            # "Start over" means back to where this branch begins — NOT back to the
            # task seed. On a fork those are different states, and handing back the
            # seed would make the escape hatch re-impose exactly the by-hand prefix
            # work the rebuild exists to remove.
            report = _rebuild_prefix(
                db, s, lease=lease, endpoint=endpoint,
                session_id=entry.live_session_id,
                ticket=_mint_ticket(entry.live_session_id, entry.owner),
            )
            # Replaced, not mutated: _Attached is frozen so that the map is only
            # ever changed by whoever holds the attempt's lock.
            entry = dataclasses.replace(
                entry, url=start_url, world="seeded", restore=restore.as_payload(report),
            )
            _ATTACHED[str(s.id)] = entry
        else:
            restore.record(db, lease, report)

    payload = restore.as_payload(report)
    db.add(models.AuditLog(
        session_id=s.id, actor=current.email, action="live.reset_world",
        target=task.external_id, meta={"seed": s.seed, "restore": payload},
    ))
    db.commit()
    return {"reset": True, "world": "seeded", "restore": payload}
