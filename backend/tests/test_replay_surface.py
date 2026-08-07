"""Where a replay runs — the surface it was RECORDED on.

The regression this locks: certify and finalize opened their scratch browser at
the gym's own pages, while the annotator had recorded against the five realistic
mocks. Every semantic locator was captured from the mock DOM, so nothing
resolved, the replay failed at action 0, and the ship gate could never pass.

The tests fake the bridge and the hub — the point is which URL the surface hands
back and which world it leases, not whether a real browser opens.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app import replay_surface


class _Attempt:
    def __init__(self, *, cua_apps=None, seed=0):
        self.id = uuid4()
        self.seed = seed
        self.bridge_session_id = ""
        self.bridge_gym_url = ""
        self.cua_apps = cua_apps


class _Task:
    external_id = "M2/order_then_track_via_email"
    start_url = "http://localhost:5201/"
    meta = {"primaryApp": "shop"}


ATTEMPT_SIDS = {"shop": "sid-shop-attempt", "mail": "sid-mail-attempt"}
SCRATCH_SIDS = {"shop": "sid-shop-scratch", "mail": "sid-mail-scratch"}


def _bridged_attempt():
    return _Attempt(cua_apps=[
        {"app": "shop", "attempt_sid": ATTEMPT_SIDS["shop"], "url": "http://localhost:5201/?sid=sid-shop-attempt"},
        {"app": "mail", "attempt_sid": ATTEMPT_SIDS["mail"], "url": "http://localhost:5203/?sid=sid-mail-attempt"},
    ])


@pytest.fixture()
def fake_bridge(monkeypatch):
    """A bridge that hands out a gym and records what was opened/closed."""
    calls = {"opened": [], "closed": []}

    def _open(sid, task_ext, seed, sids, *, force=False):
        calls["opened"].append({"sid": sid, "task": task_ext, "seed": seed,
                                "sids": dict(sids), "force": force})
        return {"gym_url": "http://gym-scratch:8077", "reused": False}

    monkeypatch.setattr(replay_surface.bridge_client, "open_session", _open)
    monkeypatch.setattr(replay_surface.bridge_client, "close_session",
                        lambda sid: calls["closed"].append(sid))
    monkeypatch.setattr(replay_surface.bridge_client, "base_url", lambda: "http://bridge:8093")
    monkeypatch.setattr(replay_surface.cua_hub, "attempt_sids", lambda apps=None: dict(SCRATCH_SIDS))
    monkeypatch.setattr(replay_surface.cua_hub, "end_attempt", lambda apps: None)
    return calls


def test_a_bridged_replay_opens_on_the_mock_it_was_recorded_on(db_session, fake_bridge):
    """THE regression. The annotator's locators were captured from the mock DOM;
    opening the gym's own pages meant none of them could ever resolve."""
    from app import cua_hub

    attempt = _bridged_attempt()
    with replay_surface.scratch_surface(db_session, attempt, _Task()) as surface:
        assert surface.bridged is True
        # the SPA's own origin, not the gym's — whatever this environment
        # configures it to be (localhost:5201 locally, the hub when deployed)
        assert "gym-scratch" not in surface.start_url
        assert surface.start_url.startswith(cua_hub.ui_base("shop")), surface.start_url
        # and in BRIDGED mode, or the tab silently runs with no engine behind it
        assert "bridge=" in surface.start_url and "session=" in surface.start_url


def test_the_scratch_run_never_touches_the_annotators_world(db_session, fake_bridge):
    """Replaying restores checkpoints and pushes its projection to the hub. Run
    in the annotator's own session it would rewind them mid-task and overwrite
    their saved app state."""
    attempt = _bridged_attempt()
    with replay_surface.scratch_surface(db_session, attempt, _Task()) as surface:
        opened = fake_bridge["opened"][0]
        assert opened["sid"] != str(attempt.id), "must not reuse the attempt's bridge session"
        assert opened["sid"].startswith(str(attempt.id))
        # fresh sids — none of the annotator's
        assert set(opened["sids"].values()).isdisjoint(set(ATTEMPT_SIDS.values()))
        for sid in ATTEMPT_SIDS.values():
            assert sid not in surface.start_url


def test_the_gym_is_always_given_back(db_session, fake_bridge):
    """One pool slot per check; leaking one is how the board goes empty."""
    attempt = _bridged_attempt()
    with replay_surface.scratch_surface(db_session, attempt, _Task()) as surface:
        sid = fake_bridge["opened"][0]["sid"]
    assert fake_bridge["closed"] == [sid]


def test_the_gym_is_given_back_even_when_the_replay_raises(db_session, fake_bridge):
    attempt = _bridged_attempt()
    with pytest.raises(RuntimeError):
        with replay_surface.scratch_surface(db_session, attempt, _Task()):
            raise RuntimeError("replay blew up")
    assert fake_bridge["closed"], "a failed replay must still return the gym"


def test_a_recorded_navigate_is_retargeted_at_the_scratch_world(db_session, fake_bridge):
    """A recorded URL carries the attempt's own sid. Replayed verbatim it would
    drive the scratch browser straight back into the annotator's world."""
    attempt = _bridged_attempt()
    with replay_surface.scratch_surface(db_session, attempt, _Task()) as surface:
        action = {"kind": "navigate", "locator": {},
                  "args": {"url": f"http://localhost:5203/?sid={ATTEMPT_SIDS['mail']}#/inbox"}}
        out = surface.rewrite(action)
        assert ATTEMPT_SIDS["mail"] not in out["args"]["url"]
        assert SCRATCH_SIDS["mail"] in out["args"]["url"]
        # everything else about the action is untouched
        assert out["kind"] == "navigate"


def test_an_action_with_no_url_is_returned_unchanged(db_session, fake_bridge):
    """A semantic locator is surface-independent by construction — that is the
    whole reason we record one — so a click must pass through untouched."""
    attempt = _bridged_attempt()
    with replay_surface.scratch_surface(db_session, attempt, _Task()) as surface:
        click = {"kind": "click", "locator": {"testId": "buy-now"}, "args": {}}
        assert surface.rewrite(click) is click


def test_a_non_bridged_attempt_replays_against_its_own_gym(db_session, monkeypatch):
    """Workspace attempts were recorded on the gym's own pages, so that IS their
    surface — the fix must not redirect them at a mock they never saw."""
    attempt = _Attempt(cua_apps=None)       # never bridged

    class _World:
        base_url = "http://gym-own:8000"

    monkeypatch.setattr(replay_surface.live_world, "world_for", lambda db, s: _World())
    with replay_surface.scratch_surface(db_session, attempt, _Task()) as surface:
        assert surface.bridged is False
        assert "gym-own" in surface.start_url
        assert surface.sid_map == {}


def test_finalize_and_certify_ask_the_same_module_where_to_replay(db_session, fake_bridge):
    """They used to decide independently, and both decided wrong. One module owns
    it now, so they cannot drift apart again."""
    import inspect

    from app.api import versions as versions_api

    src = inspect.getsource(versions_api)
    # neither path may reach for the gym's URL directly any more
    assert "browser_visible_gym_url(endpoint.base_url)" not in src
    assert src.count("replay_surface.scratch_surface(") == 2, \
        "finalize and certify must both go through the shared surface"


def test_a_scratch_surface_is_distinct_per_purpose(db_session, fake_bridge):
    """certify and finalize must not collide on the same bridge session id when
    an annotator checks and then ships in quick succession."""
    attempt = _bridged_attempt()
    with replay_surface.scratch_surface(db_session, attempt, _Task(), purpose="certify"):
        pass
    with replay_surface.scratch_surface(db_session, attempt, _Task(), purpose="finalize"):
        pass
    opened = [c["sid"] for c in fake_bridge["opened"]]
    assert len(set(opened)) == 2, opened


def test_a_scratch_world_is_reset_every_time_not_reattached(db_session, fake_bridge):
    """The bug that made every check after the first one fail.

    `open_session` is idempotent by design: re-opening a session already on this
    task and seed ATTACHES to the world that is there, so an annotator
    reconnecting cannot lose work. Right for their session, wrong for a scratch
    one — `scratch_id` is derived from the attempt and purpose, so the second
    check attached to the world the first check had already replayed, order
    placed and all, and diverged the moment it re-ran the step that placed it.

    Reproduced against the live stack: first certify on a clean pool verified all
    11 steps; every certify after it failed at step 7 with 8 verified, on a
    trajectory that was fine.
    """
    attempt = _bridged_attempt()
    for _ in range(2):
        with replay_surface.scratch_surface(db_session, attempt, _Task()):
            pass

    assert len(fake_bridge["opened"]) == 2
    assert all(o["force"] is True for o in fake_bridge["opened"]), (
        "a scratch world that is kept is not scratch"
    )
