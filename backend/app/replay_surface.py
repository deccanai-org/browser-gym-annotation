"""Where a replay actually runs.

A trajectory is only reproducible if it is replayed against the surface it was
recorded on. For the realistic-UI pilot the annotator works in the five mock SPAs
— their clicks are resolved by `recorder.semantic_locator` against THAT DOM, so
every step carries a `testId`/`role`/`name` that exists in the mock and nowhere
else. Both `certify` and `finalize` nevertheless opened their scratch browser at
the gym's own server-rendered pages, so the locators could not resolve, the
replay failed at action 0, certify marked every step `diverged`, and finalize
raised `ReplayRejected`. The Check button and the ship gate were structurally
unable to pass, and nothing in the message said why.

This module owns that decision in ONE place so the two paths cannot drift again:

    with scratch_surface(db, attempt, task) as surface:
        ...  surface.world       # the gym to reset/score against
             surface.start_url   # where to open the browser
             surface.rewrite(a)  # an action, retargeted at this surface

A scratch surface is always throwaway. Replaying restores checkpoints and pushes
its projection to the hub, so running it in the annotator's own world would
rewind them mid-task and overwrite their saved app state. A bridged attempt
therefore certifies in a DIFFERENT bridge session with FRESH sids; the annotator's
five tabs are untouched.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Iterator

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import bridge_client, cua_hub, live_world, models
from app.config import settings


@dataclass
class ScratchSurface:
    """Everything a replay needs to run somewhere that is not the annotator's world."""

    world: object                      # a WorldPort: reset/world/verify
    start_url: str                     # browser-visible; where to open the tab
    apps: list[dict] = field(default_factory=list)
    sid_map: dict[str, str] = field(default_factory=dict)   # attempt sid -> scratch sid
    #: The `?session=` a recorded URL carries, and the one THIS surface answers
    #: to. A bridged tab reports its mutations to the bridge under that id.
    attempt_session: str = ""
    scratch_session: str = ""
    bridged: bool = False

    def rewrite(self, action: dict) -> dict:
        """Retarget one recorded action at THIS surface.

        A recorded `navigate` carries the attempt's own sid AND its `?session=`
        in the URL. Replayed verbatim it would drive the scratch browser back
        into the annotator's world — the one thing a scratch run must never touch
        — so both are swapped. Everything else replays unchanged: a semantic
        locator is surface-independent by construction, which is the point of
        recording one.

        The `?session=` half was missing, and it is what a bridged tab reports
        its mutations under. So the sids were swapped, the tab looked like a
        scratch tab, and every write went to the ANNOTATOR's world instead:
        certifying M105 replayed all thirteen steps, closed the compose modal on
        a real send, and read a scratch world with no sent mail in it. Reported
        as a divergence at the last step, and the annotator's own world had
        quietly gained the email.
        """
        args = action.get("args") or {}
        url = args.get("url")
        if not isinstance(url, str) or not url:
            return action
        out = url
        for attempt_sid, scratch_sid in self.sid_map.items():
            if attempt_sid and attempt_sid in out:
                out = out.replace(attempt_sid, scratch_sid)
        if self.attempt_session and self.scratch_session:
            out = out.replace(f"session={self.attempt_session}", f"session={self.scratch_session}")
        if out == url:
            return action
        return {**action, "args": {**args, "url": out}}


def _primary_key(task) -> str:
    meta = getattr(task, "meta", None)
    if isinstance(meta, dict) and meta.get("primaryApp"):
        return str(meta["primaryApp"])
    return cua_hub.primary_app(getattr(task, "start_url", "") or "")


@contextlib.contextmanager
def scratch_surface(db: Session, attempt: models.ReviewSession, task,
                    *, purpose: str = "certify") -> Iterator[ScratchSurface]:
    """A throwaway surface matching how this attempt was RECORDED.

    Bridged attempt -> a scratch bridge session, fresh sids, and the browser
    opened on the primary MOCK app (in bridged mode, so the engine is live and
    the milestone verdict is real).

    Anything else -> the attempt's own gym, which is the surface those attempts
    were recorded on.
    """
    # Imported here: app.api.live imports this module's callers, so importing it
    # at module scope closes a cycle.
    from app.api import live as live_api

    if not live_world.owns_bridged_world(attempt):
        world = live_world.world_for(db, attempt)
        yield ScratchSurface(
            world=world,
            start_url=live_api.browser_visible_gym_url(
                getattr(world, "base_url", "") or settings.gym_url),
            bridged=False,
        )
        return

    scratch_id = f"{attempt.id}:{purpose}"
    sids = cua_hub.attempt_sids()      # throwaway; discarded with the session
    task_ext = task.external_id if task is not None else ""
    try:
        # force=True: a scratch surface must be CLEAN, every time.
        #
        # `open_session` is idempotent by design — re-opening a session already on
        # this task and seed attaches to the world that is there, so an annotator
        # reconnecting cannot lose work in progress. Exactly right for their own
        # session, and exactly wrong here: `scratch_id` is derived from the
        # attempt and the purpose, so the SECOND check of an attempt attached to
        # the world the FIRST check had already replayed — order placed and all —
        # and the replay diverged the moment it re-ran the step that placed it.
        #
        # It reproduced perfectly: the first certify on a clean pool verified all
        # 11 steps, and every certify after it failed at step 7 with 8 verified,
        # for a trajectory that was fine. Resetting is the whole point of a
        # scratch world; keeping one is what makes it not scratch.
        out = bridge_client.open_session(scratch_id, task_ext, attempt.seed, sids, force=True)
    except bridge_client.BridgePoolExhausted as exc:
        raise HTTPException(
            status_code=503,
            detail=(f"no free gym to check this in — {exc}. Checking needs its own gym so it "
                    f"cannot disturb yours; try again shortly."),
        ) from exc
    except bridge_client.BridgeError as exc:
        raise HTTPException(status_code=409,
                            detail=f"could not start a gym to check in: {exc}") from exc

    apps: list[dict] = []
    try:
        gym_url = str(out.get("gym_url") or "")
        apps = cua_hub.apps_for(
            sids,
            start_paths=live_api._cua_start_paths(task),
            # Resolved by the BROWSER, not by this process — ours points at
            # host.docker.internal, which does not resolve on the host, and a tab
            # that cannot reach the bridge silently drops to local-store mode with
            # no engine behind it.
            bridge=live_api._browser_visible(bridge_client.base_url()),
            session=scratch_id,
        )
        for a in apps:
            a["url"] = live_api._browser_visible(a["url"])
        want = _primary_key(task)
        primary = next((a for a in apps if a["app"] == want), apps[0] if apps else None)

        # Map the attempt's own sids onto this surface's, so a recorded navigate
        # cannot steer the replay back into the annotator's world.
        recorded = {a.get("app"): a.get("attempt_sid")
                    for a in (attempt.cua_apps or []) if a.get("app")}
        sid_map = {recorded[app]: sid for app, sid in sids.items()
                   if recorded.get(app) and recorded[app] != sid}

        yield ScratchSurface(
            world=live_world.BridgedWorld(gym_url, scratch_id),
            start_url=(primary or {}).get("url") or live_api.browser_visible_gym_url(gym_url),
            apps=apps,
            sid_map=sid_map,
            attempt_session=str(attempt.id),
            scratch_session=scratch_id,
            bridged=True,
        )
    finally:
        # Give the gym back and discard the throwaway worlds. Both are
        # best-effort: teardown must never mask the replay's own result, but
        # skipping them leaks a pool slot and a row per check.
        with contextlib.suppress(Exception):
            bridge_client.close_session(scratch_id)
        with contextlib.suppress(Exception):
            cua_hub.end_attempt(apps)
