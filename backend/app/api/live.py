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
import contextlib
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
from sqlalchemy.orm import Session

from app import models, workspace
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
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, path, parsed.query, ""))


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
            return payload

        _ATTACHED.pop(str(s.id), None)

        # ACQUIRE this attempt's own gym before resolving the endpoint.
        # `endpoint_for` only READS an existing lease — it never provisions — so
        # without this every annotator silently shares one gym, which is exactly
        # the corruption isolation exists to prevent. Falling back is deliberate
        # (a docker hiccup must not stop someone working) but must be VISIBLE, so
        # the response says which world they got.
        isolated = False
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

        # SEED THE WORLD FIRST. The gym holds one global session per process and
        # keeps whatever the last caller left in it — so without this the
        # annotator drives a browser showing some other task's world entirely.
        # Observed: an annotator opened M46/sneaked_addon ("check out the keyboard
        # in my cart") and got an empty cart and a different task's on-page brief,
        # because the last thing to touch the gym was M15. Anything they recorded
        # would have been against the wrong state.
        if task is not None and task.external_id and (task.source == "gym" or s.source == "gym"):
            if endpoint.reset(task.external_id, s.seed) is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"could not seed the gym for {task.external_id} — the live browser would "
                        "show the wrong world, so it was not opened"
                    ),
                )

        # …and land where the TASK starts, not at the gym root. M46 begins on
        # /cart; dropping the annotator on the home page makes them navigate to
        # the state the task already guaranteed them.
        start_url = _browser_visible(
            _task_start_url(endpoint.base_url, task.start_url if task is not None else "")
        )
        opened = _open_browser(start_url, current.email)
        entry = _Attached(
            live_session_id=str(opened["session_id"]),
            owner=current.email,
            url=start_url,
            viewport=opened.get("viewport") or _DEFAULT_VIEWPORT,
            isolated=isolated,
        )
        _ATTACHED[str(s.id)] = entry

    db.add(models.AuditLog(
        session_id=s.id, actor=current.email, action="live.open",
        target=entry.live_session_id, meta={"url": start_url},
    ))
    db.commit()
    return {
        "sessionId": entry.live_session_id,
        "ticket": opened["ticket"],
        "viewport": entry.viewport,
        "url": entry.url,
        "isolated": entry.isolated,
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
    db.add(models.AuditLog(
        session_id=s.id, actor=current.email, action="live.close", target=entry.live_session_id, meta={},
    ))
    db.commit()
    return {"closed": True}
