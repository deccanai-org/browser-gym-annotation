"""Bridged-session client — the annotator's door into the real gym engine.

The realistic mock UIs can run two ways. Plain, each mock owns its own JSON and a
click only ever changes that one app. **Bridged**, every click is forwarded to a
real gym instance, which applies the engine's rules and re-projects the resulting
world back into all five mocks — so an order placed in ShopGym drops its
confirmation email into ShopMail on its own, and the gym's own milestone suite can
score what the annotator did.

We run bridged. That is what makes cross-app tasks possible and what gives
Feature 2 a ground truth instead of a guess.

One bridge session == one attempt == one gym instance out of the pool. The pool is
finite, so `open` can legitimately fail with "no free gym instance"; that is a
capacity signal, not a bug, and it is reported as such (`BridgePoolExhausted`).

Service: `tools/bridge_service.py` in the gym repo (`CUA_BRIDGE_URL`, default
http://127.0.0.1:8093).

    POST /bridge/{sid}/open   {task_id, seed, sids{app: attempt_sid}}
                              -> {ok, task_id, task_brief, apps{app: state}, gym_url}
    GET  /bridge/{sid}/state  [?app=]      -> {apps{app: state}}
    GET  /bridge/{sid}/verify [?url=]      -> the gym's milestone verdict
    POST /bridge/{sid}/close               -> {ok}
    GET  /bridge/sessions                  -> pool status

Note the service answers 200 with ``{"ok": false, ...}`` for its own failures
(pool exhausted, unknown session) rather than an HTTP error code, so every call
here checks the body, not just the status.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request


def base_url() -> str:
    return (os.environ.get("CUA_BRIDGE_URL") or "http://127.0.0.1:8093").rstrip("/")


def enabled() -> bool:
    """Bridged mode is on when a bridge URL is configured."""
    return bool(os.environ.get("CUA_BRIDGE_URL", "").strip())


class BridgeError(Exception):
    """Base: anything that stopped us getting a bridged world."""


class BridgeUnreachable(BridgeError):
    """No usable HTTP response — wrong URL, service down, timeout, non-JSON body.

    Kept distinct from "the world isn't there" so the annotator is told to start
    the bridge rather than told their task is broken.
    """


class BridgePoolExhausted(BridgeError):
    """Every gym in the pool is leased. Capacity, not corruption — retry later."""


class BridgeSessionUnknown(BridgeError):
    """The bridge has no such session — it was closed, expired, or never opened."""


def _req(method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    url = base_url() + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"content-type": "application/json"} if data is not None else {},
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
        raise BridgeUnreachable(f"{method} {url} -> HTTP {e.code} {detail}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise BridgeUnreachable(f"{method} {url} -> {e}. Is the bridge running (tools/run_bridged_stack.sh)?") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # Almost always CUA_BRIDGE_URL pointing at something that isn't the bridge.
        raise BridgeUnreachable(f"{method} {url} returned non-JSON — is CUA_BRIDGE_URL correct?") from e


def _check(out: dict, session_id: str) -> dict:
    """Turn the service's in-body failures into typed errors."""
    if out.get("ok") is False or out.get("error"):
        err = str(out.get("error") or "bridge refused the request")
        if "no free gym" in err or out.get("status") == 503:
            raise BridgePoolExhausted(err)
        if "unknown session" in err:
            raise BridgeSessionUnknown(f"{err} (session {session_id})")
        raise BridgeError(err)
    return out


def open_session(session_id: str, task_id: str, seed: int, sids: dict[str, str],
                 *, force: bool = False) -> dict:
    """Lease a gym, reset it to (task_id, seed), and baseline all five mocks.

    ``sids`` maps app -> the per-attempt SID this session's world is journalled
    under, so the annotator mutates a clone and the frozen seed is never touched.
    Returns {ok, reused, task_id, task_brief, apps{app: state}, gym_url}.

    Idempotent unless ``force``: re-opening a session already on this task and
    seed ATTACHES to the world that is there rather than resetting it, so a
    reconnect cannot throw away work in progress. Pass ``force=True`` only where
    the reset IS the point (reset-world). An older bridge ignores the flag and
    resets, which is the pre-existing behaviour.
    """
    out = _req("POST", f"/bridge/{urllib.parse.quote(session_id)}/open",
               {"task_id": task_id, "seed": seed, "sids": sids, "force": force})
    return _check(out, session_id)


def repush(session_id: str, *, step: int | None = None) -> dict:
    """Re-project the engine's current world into the five mocks.

    Called after restoring a checkpoint: the engine holds the annotator's world
    again, but the hub still holds the seed projection. `step` re-syncs the
    engine clock so the scheduler does not re-fire already-delivered events.

    Tolerates an older bridge that has no such route — the world is still
    correct, the tabs just render the seed until the first action.
    """
    try:
        return _req("POST", f"/bridge/{urllib.parse.quote(session_id)}/repush",
                    {"step": step})
    except BridgeError:
        return {"ok": False}


def state(session_id: str, app: str | None = None) -> dict[str, dict]:
    """Per-app projected state — the mocks' view of the world."""
    q = f"?app={urllib.parse.quote(app)}" if app else ""
    out = _req("GET", f"/bridge/{urllib.parse.quote(session_id)}/state{q}")
    _check(out, session_id)
    return out.get("apps") or {}


def verify(session_id: str, url: str = "") -> dict:
    """The gym's REAL milestone verdict on this session's world.

    This is the ground truth Feature 2 scores against — the environment's own
    opinion of whether the task was completed, independent of the annotator's
    hand-written verifier suite.
    """
    q = f"?url={urllib.parse.quote(url, safe='')}" if url else ""
    out = _req("GET", f"/bridge/{urllib.parse.quote(session_id)}/verify{q}")
    return _check(out, session_id)


def close_session(session_id: str) -> bool:
    """Release the gym back to the pool. Best-effort: teardown must not raise."""
    try:
        return bool(_req("POST", f"/bridge/{urllib.parse.quote(session_id)}/close").get("ok"))
    except BridgeError:
        return False


def pool_status() -> dict:
    """Capacity snapshot — used by health checks and to explain a 503 to the annotator."""
    return _req("GET", "/bridge/sessions", timeout=10)
