"""Live session API — minting the ticket the browser pane cannot mint for itself.

The failure this endpoint exists to prevent is silent: a ticket the live browser
service will not honour produces a socket that completes its handshake and is
then closed 4401, which on screen is indistinguishable from a pane that is merely
slow. So these tests check the ticket against the service's OWN validation
algorithm rather than against the shape of the string.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app import models
from app.api import live
from app.config import settings
from app.gym_client import GymEndpoint
from app.main import app

OWNER = "test@deccan.ai"  # the account tests/conftest.py signs `client` in as


class FakeLiveBrowser:
    """A stand-in for live_browser/service.py at the HTTP boundary.

    Its ticket check is transcribed from that service rather than imported: the
    service lives in the gym repo, and the property under test is precisely
    whether tickets minted HERE validate THERE.
    """

    def __init__(self) -> None:
        self.secret = live.LIVE_STREAM_SECRET
        self.sessions: dict[str, dict] = {}
        self.opens = 0
        self.opened_urls: list[str] = []
        self.opened_hosts: list[str] = []
        self.closed: list[str] = []
        self.reachable = True
        self.viewport = {"width": 1280, "height": 800}

    # --- the service's own ticket algorithm ---------------------------------
    def check_ticket(self, sid: str, ticket: str) -> str | None:
        try:
            exp_s, sig, owner_b64 = ticket.split(".", 2)
            exp = int(exp_s)
            owner = base64.urlsafe_b64decode(owner_b64 + "=" * (-len(owner_b64) % 4)).decode()
        except (ValueError, AttributeError, UnicodeDecodeError):
            return None
        if exp < time.time():
            return None
        expect = hmac.new(self.secret.encode(), f"{sid}:{owner}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return owner if hmac.compare_digest(expect, sig) else None

    def _mint(self, sid: str, owner: str) -> str:
        exp = int(time.time()) + 300
        sig = hmac.new(self.secret.encode(), f"{sid}:{owner}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{exp}.{sig}.{base64.urlsafe_b64encode(owner.encode()).decode().rstrip('=')}"

    # --- routes -------------------------------------------------------------
    def _route(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        if method == "POST" and path == "/live/sessions":
            self.opens += 1
            sid = f"live-{self.opens}"
            self.sessions[sid] = {"url": body["url"], "owner": body["owner"]}
            return 200, {"session_id": sid, "ticket": self._mint(sid, body["owner"]), "viewport": self.viewport}

        parts = path.strip("/").split("/")  # live/sessions/<sid>[/<verb>]
        sid = parts[2] if len(parts) > 2 else ""
        verb = parts[3] if len(parts) > 3 else ""
        session = self.sessions.get(sid)
        if session is None:
            return 404, {"detail": "unknown session"}
        if method == "GET" and not verb:
            return 200, {"url": session["url"], "tabs": [session["url"]], "viewport": self.viewport}
        if verb == "focused":
            owner = self.check_ticket(sid, body.get("ticket", ""))
            if owner is None or owner != session["owner"]:
                return 403, {"detail": "invalid or expired ticket"}
            return 200, {}
        if verb == "close":
            self.sessions.pop(sid)
            self.closed.append(sid)
            return 200, {"ok": True}
        return 404, {"detail": "not found"}

    def urlopen(self, req, timeout=None):  # noqa: ARG002 — signature of the real urlopen
        if not self.reachable:
            raise urllib.error.URLError("connection refused")
        split = urllib.parse.urlsplit(req.full_url)
        body = json.loads(req.data) if req.data else {}
        if split.path == "/live/sessions" and req.get_method() == "POST":
            self.opened_urls.append(body["url"])
            self.opened_hosts.append(f"{split.scheme}://{split.netloc}")
        status, payload = self._route(req.get_method(), split.path, body)
        raw = json.dumps(payload).encode()
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "error", {}, io.BytesIO(raw))
        return _Response(raw)


class _Response:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc) -> bool:
        return False


@pytest.fixture(autouse=True)
def _forget_open_browsers():
    """The attachment map is process state, so one test's browser would otherwise
    still be attached in the next."""
    live._ATTACHED.clear()
    yield
    live._ATTACHED.clear()


@pytest.fixture()
def live_service(monkeypatch):
    svc = FakeLiveBrowser()
    monkeypatch.setattr(urllib.request, "urlopen", svc.urlopen)
    # Opening a live browser SEEDS the gym for the attempt's task. Patch reset on
    # the CLASS rather than replacing endpoint_for — several of these tests are
    # about which endpoint gets resolved, and overriding that would erase them.
    seeded: list = []
    monkeypatch.setattr(
        GymEndpoint, "reset",
        lambda self, task_id, seed=0: (seeded.append((task_id, seed)), {"ok": True})[1],
    )
    svc.reset_calls = seeded
    return svc


def _task(db_session) -> models.Task:
    task = models.Task(external_id=f"M60_live_{uuid4().hex[:6]}", title="live", prompt="drive it", source="gym")
    db_session.add(task)
    db_session.commit()
    return task


@pytest.fixture()
def attempt(client, db_session) -> str:
    r = client.post(f"/api/tasks/{_task(db_session).external_id}/sessions", json={})
    assert r.status_code == 200, r.text
    return r.json()["sessionId"]


# --------------------------------------------------------------------------- wiring
def test_the_live_routes_are_registered_on_the_application_itself():
    """A router that is only mounted inside its own test module passes every test
    in that module and 404s in the product. This reads the surface the real
    application publishes, not a router this file assembled."""
    paths = app.openapi()["paths"]
    assert sorted(paths["/api/sessions/{session_id}/live"]) == ["get", "post"]
    assert sorted(paths["/api/sessions/{session_id}/live/close"]) == ["post"]


def test_the_live_routes_are_behind_the_login_gate(anon_client):
    """Auth is applied where the router is included, so a route that is mounted
    without it is open to anonymous callers — and a live session is remote
    control of a browser."""
    assert anon_client.post(f"/api/sessions/{uuid4()}/live").status_code == 401
    assert anon_client.get(f"/api/sessions/{uuid4()}/live").status_code == 401
    assert anon_client.post(f"/api/sessions/{uuid4()}/live/close").status_code == 401


# --------------------------------------------------------------------------- opening
def test_opening_returns_a_ticket_the_service_will_honour_for_this_annotator(client, attempt, live_service):
    """The service closes the socket 4401 when the ticket owner is not the owner
    its session was opened for, and that close is invisible to the annotator."""
    r = client.post(f"/api/sessions/{attempt}/live")
    assert r.status_code == 200, r.text
    body = r.json()
    assert live_service.check_ticket(body["sessionId"], body["ticket"]) == OWNER, \
        "the live service must resolve the ticket to the signed-in annotator"
    assert body["viewport"] == {"width": 1280, "height": 800}
    assert set(body) == {"sessionId", "ticket", "viewport", "url", "isolated", "world", "restore"}
    # False, not absent: isolation is off in tests, and the annotator is entitled
    # to know they are in the SHARED world rather than having to infer it from a
    # missing key.
    assert body["isolated"] is False
    # Isolation off => the world was reseeded, and the response says so. A person
    # must be able to tell whether the cart they left is still there.
    assert body["world"] == "seeded"
    # An unforked attempt has no prefix to rebuild — say nothing rather than "0/0".
    assert body["restore"] is None


def test_the_browser_is_opened_against_the_running_live_service(client, attempt, live_service):
    assert client.post(f"/api/sessions/{attempt}/live").status_code == 200
    assert live_service.opened_hosts == [settings.live_browser_url]


def test_the_browser_opens_at_the_attempts_own_workspace_gym(client, attempt, live_service, monkeypatch):
    """An isolated attempt leases its own gym process. Opening the browser at the
    shared gym would put the annotator in somebody else's world — the exact
    corruption workspace isolation exists to prevent."""
    monkeypatch.setattr(live.workspace, "endpoint_for", lambda db, aid: GymEndpoint("http://127.0.0.1:9931"))
    body = client.post(f"/api/sessions/{attempt}/live").json()
    assert live_service.opened_urls == ["http://127.0.0.1:9931/"]
    assert body["url"] == "http://127.0.0.1:9931/"


def test_without_an_isolated_workspace_the_browser_opens_at_the_shared_gym(client, attempt, live_service):
    body = client.post(f"/api/sessions/{attempt}/live").json()
    assert body["url"].startswith(settings.gym_url), "never a hardcoded gym address"


# --------------------------------------------------------------------------- re-attaching
def test_a_reload_re_attaches_instead_of_opening_a_second_browser(client, attempt, live_service):
    """One Chromium per reload is how this runs the box out of memory long before
    an annotator finishes a task."""
    first = client.post(f"/api/sessions/{attempt}/live").json()
    again = client.post(f"/api/sessions/{attempt}/live").json()
    assert live_service.opens == 1, "re-attaching must reuse the browser that is already open"
    assert again["sessionId"] == first["sessionId"]
    assert live_service.check_ticket(again["sessionId"], again["ticket"]) == OWNER


def test_a_re_attach_mints_a_fresh_ticket_rather_than_replaying_the_expiring_one(client, attempt, live_service):
    """Tickets live LIVE_TICKET_TTL_S. Handing back the one minted when the
    browser opened would fail every reload made after that window, and the only
    way out would be burning a new browser."""
    client.post(f"/api/sessions/{attempt}/live")
    reattached = client.get(f"/api/sessions/{attempt}/live").json()["session"]
    expiry = int(reattached["ticket"].split(".")[0])
    assert expiry - time.time() > live.LIVE_TICKET_TTL_S - 5, "the re-issued ticket must carry a full TTL"


def test_nothing_is_reported_open_before_a_browser_is_opened(client, attempt, live_service):
    assert client.get(f"/api/sessions/{attempt}/live").json() == {"session": None}


def test_the_open_browser_is_reported_with_where_it_was_opened(client, attempt, live_service):
    opened = client.post(f"/api/sessions/{attempt}/live").json()
    reported = client.get(f"/api/sessions/{attempt}/live").json()["session"]
    assert reported["sessionId"] == opened["sessionId"]
    assert reported["url"] == opened["url"]
    assert reported["viewport"] == opened["viewport"]


def test_a_browser_that_died_under_us_is_not_reported_as_open(client, attempt, live_service):
    """A restarted live service takes every browser with it. Reporting the stale
    id hands the pane a ticket for a session that answers 4404 and never
    streams — the annotator would sit in front of a blank pane forever."""
    opened = client.post(f"/api/sessions/{attempt}/live").json()
    live_service.sessions.clear()

    assert client.get(f"/api/sessions/{attempt}/live").json() == {"session": None}
    fresh = client.post(f"/api/sessions/{attempt}/live").json()
    assert live_service.opens == 2 and fresh["sessionId"] != opened["sessionId"]


# --------------------------------------------------------------------------- failure modes
def test_an_unreachable_live_service_is_a_409_that_says_so(client, attempt, live_service):
    live_service.reachable = False
    r = client.post(f"/api/sessions/{attempt}/live")
    assert r.status_code == 409
    assert "unreachable" in r.json()["detail"]


def test_a_ticket_the_service_would_reject_fails_loudly_instead_of_silently(client, attempt, live_service):
    """A signing-secret mismatch yields a socket that connects and is then closed
    4401 — on screen, a pane that never paints. Catching it here is the only
    moment we can still say why."""
    client.post(f"/api/sessions/{attempt}/live")
    live_service.secret = "a-different-secret"

    r = client.get(f"/api/sessions/{attempt}/live")
    assert r.status_code == 409
    assert "LIVE_STREAM_SECRET" in r.json()["detail"]


# --------------------------------------------------------------------------- closing
def test_closing_hands_the_browser_back_and_forgets_it(client, attempt, live_service):
    opened = client.post(f"/api/sessions/{attempt}/live").json()
    r = client.post(f"/api/sessions/{attempt}/live/close")
    assert r.status_code == 200 and r.json() == {"closed": True}
    assert live_service.closed == [opened["sessionId"]]
    assert client.get(f"/api/sessions/{attempt}/live").json() == {"session": None}
    assert client.post(f"/api/sessions/{attempt}/live/close").json() == {"closed": True}, \
        "closing an attempt that has nothing open is not an error"


def test_closing_forgets_the_browser_even_when_the_service_cannot_be_reached(client, attempt, live_service):
    """Keeping the attachment because the close call failed strands the attempt on
    a browser nobody can reach, with no way to open a working one."""
    client.post(f"/api/sessions/{attempt}/live")
    live_service.reachable = False
    assert client.post(f"/api/sessions/{attempt}/live/close").json() == {"closed": True}

    live_service.reachable = True
    assert client.get(f"/api/sessions/{attempt}/live").json() == {"session": None}


def test_closing_releases_the_pooled_gym_lease(client, attempt, live_service, db_session, monkeypatch):
    """Closing must give the bridged gym back, not just the Chromium. Holding the
    lease leaked one of the few pooled gyms per task an annotator opened, so after
    a couple of tasks every /live 503'd and the board looked empty."""
    released: list = []
    monkeypatch.setattr(live.bridge_client, "close_session", lambda sid: (released.append(sid), True)[1])

    client.post(f"/api/sessions/{attempt}/live")
    # Mark the attempt as holding a bridge lease, the way a cua open does.
    s = db_session.get(models.ReviewSession, UUID(attempt))
    s.bridge_session_id = "bridge-xyz"
    db_session.commit()

    assert client.post(f"/api/sessions/{attempt}/live/close").json() == {"closed": True}
    assert released == ["bridge-xyz"], "the pooled gym must be handed back on close"

    # And it is forgotten, so a second close does not double-release: the second
    # call finds nothing attached and returns without touching the bridge.
    released.clear()
    assert client.post(f"/api/sessions/{attempt}/live/close").json() == {"closed": True}
    assert released == [], "a redundant close must not release a lease that is already gone"


# --------------------------------------------------------------------------- ownership
def test_another_annotators_attempt_is_a_404_and_never_a_403(client_for, db_session, live_service):
    """403 confirms the attempt exists. An ownership failure must not disclose
    that, which is why every route here goes through _owned_session."""
    owner, other = client_for("ela@deccan.ai"), client_for("nav@deccan.ai")
    sid = owner.post(f"/api/tasks/{_task(db_session).external_id}/sessions", json={}).json()["sessionId"]
    assert owner.post(f"/api/sessions/{sid}/live").status_code == 200

    assert other.post(f"/api/sessions/{sid}/live").status_code == 404
    assert other.get(f"/api/sessions/{sid}/live").status_code == 404
    assert other.post(f"/api/sessions/{sid}/live/close").status_code == 404
    assert live_service.opens == 1, "a non-owner must not be able to open a browser on the attempt"
    assert live_service.closed == [], "a non-owner must not be able to close somebody else's browser"


def test_the_open_and_close_are_attributed_in_the_audit_log(client, attempt, live_service, db_session):
    opened = client.post(f"/api/sessions/{attempt}/live").json()
    client.post(f"/api/sessions/{attempt}/live/close")

    db_session.rollback()
    rows = db_session.scalars(
        select(models.AuditLog).where(models.AuditLog.action.in_(("live.open", "live.close")))
    ).all()
    assert {r.action for r in rows} == {"live.open", "live.close"}
    assert {r.actor for r in rows} == {OWNER}
    assert {r.target for r in rows} == {opened["sessionId"]}


# --------------------------------------------------------------------------- network namespaces
def test_the_browser_gets_a_url_it_can_actually_resolve(monkeypatch):
    """The backend and the browser are in DIFFERENT network namespaces. The
    composed backend reaches the gym at host.docker.internal; the browser runs on
    the host, where that name does not resolve — handing our own URL over gives
    ERR_NAME_NOT_RESOLVED and a pane that never paints. Reproduced live before
    this existed."""
    from app.api import live as live_api
    from app.config import settings

    monkeypatch.setattr(settings, "gym_host_for_browser", "localhost")
    assert live_api._browser_visible("http://host.docker.internal:8000") == "http://localhost:8000/"


def test_the_port_survives_the_rewrite(monkeypatch):
    """Workspace isolation gives each attempt its OWN gym port on the same host.
    Rewriting the port would send every annotator's browser to one shared world —
    the exact thing the isolation exists to prevent."""
    from app.api import live as live_api
    from app.config import settings

    monkeypatch.setattr(settings, "gym_host_for_browser", "localhost")
    assert live_api._browser_visible("http://host.docker.internal:9123/") == "http://localhost:9123/"


def test_an_unset_host_is_a_no_op(monkeypatch):
    """A single-host dev box shares one namespace and must need no configuration."""
    from app.api import live as live_api
    from app.config import settings

    monkeypatch.setattr(settings, "gym_host_for_browser", "")
    assert live_api._browser_visible("http://localhost:8000") == "http://localhost:8000/"


def test_the_task_start_url_survives_the_rewrite(monkeypatch):
    """Callers pass a task's OWN start URL now — M46/sneaked_addon begins on /cart.
    Normalising every URL to a trailing slash turned that into "/cart/", which is a
    different route."""
    from app.api import live as live_api
    from app.config import settings

    monkeypatch.setattr(settings, "gym_host_for_browser", "localhost")
    assert live_api._browser_visible("http://host.docker.internal:8000/cart") == "http://localhost:8000/cart"
    monkeypatch.setattr(settings, "gym_host_for_browser", "")
    assert live_api._browser_visible("http://localhost:8000/cart") == "http://localhost:8000/cart"
    assert live_api._browser_visible("http://localhost:8000") == "http://localhost:8000/"


def test_opening_the_browser_seeds_the_gym_for_this_attempts_task(client, attempt, live_service, db_session):
    """THE wrong-world bug, reported from the UI.

    The gym holds one global session per process and keeps whatever the last
    caller left in it. An annotator opened M46/sneaked_addon — "check out the
    wireless keyboard that's in my cart" — and got an EMPTY cart plus a different
    task's on-page brief, because the last thing to touch the gym was M15. Every
    interaction they recorded would have been against the wrong world."""
    from uuid import UUID

    s = db_session.get(models.ReviewSession, UUID(attempt))
    task = db_session.get(models.Task, s.task_id)

    assert client.post(f"/api/sessions/{attempt}/live").status_code == 200
    assert live_service.reset_calls == [(task.external_id, s.seed)], (
        "the gym must be reset to THIS attempt's task before the annotator sees it"
    )


def test_the_browser_lands_where_the_task_starts(client, db_session, live_service):
    """M46 begins on /cart. Dropping the annotator on the gym home page makes them
    navigate to a state the task already guaranteed them — and invites them to
    record that navigation as though it were part of the trajectory."""
    task = models.Task(external_id=f"M46_seed_{uuid4().hex[:6]}", title="t", prompt="p",
                       source="gym", start_url="http://localhost:8000/cart")
    db_session.add(task)
    db_session.commit()
    sid = client.post(f"/api/tasks/{task.external_id}/sessions", json={}).json()["sessionId"]

    body = client.post(f"/api/sessions/{sid}/live").json()
    assert body["url"] == "http://localhost:8000/cart", "the task's own start URL, path intact"


def test_a_gym_that_cannot_seed_the_task_refuses_to_open(client, attempt, live_service, monkeypatch):
    """Opening anyway would hand the annotator a browser showing someone else's
    world, which is worse than not opening at all."""
    monkeypatch.setattr(GymEndpoint, "reset", lambda self, task_id, seed=0: None)
    r = client.post(f"/api/sessions/{attempt}/live")
    assert r.status_code == 409
    assert "wrong world" in str(r.json()["detail"])


def test_a_relative_task_start_url_is_resolved_against_the_attempts_gym():
    """`task.start_url` is stored inconsistently — measured on the live DB, 270 of
    312 tasks hold a relative path and 20 hold an absolute URL. Handing Playwright
    a bare "/cart" fails outright, so testing only an absolute-URL task (M46) and
    generalising would have broken the browser for the other 270."""
    from app.api.live import _task_start_url

    base = "http://127.0.0.1:9931"
    assert _task_start_url(base, "/cart") == f"{base}/cart"
    assert _task_start_url(base, "/account/orders") == f"{base}/account/orders"
    assert _task_start_url(base, "/") == f"{base}/"
    assert _task_start_url(base, "") == f"{base}/", "no start URL means the gym root"


def test_an_absolute_start_url_still_lands_on_this_attempts_gym():
    """When start_url IS absolute it names the SHARED gym. An attempt holding an
    isolated workspace must not be sent there — that is the corruption workspace
    isolation exists to prevent. Only the path comes from the task."""
    from app.api.live import _task_start_url

    got = _task_start_url("http://127.0.0.1:9931", "http://localhost:8000/cart")
    assert got == "http://127.0.0.1:9931/cart"
    assert "localhost:8000" not in got


def test_the_query_string_survives():
    from app.api.live import _task_start_url

    assert _task_start_url("http://g:8000", "/mail?sent=1") == "http://g:8000/mail?sent=1"


# --------------------------------------------------------------------------- world preservation
def test_the_reset_world_route_is_registered():
    assert sorted(app.openapi()["paths"]["/api/sessions/{session_id}/live/reset-world"]) == ["post"]


def _isolated(monkeypatch, *, preserved: bool):
    """Pretend this attempt owns a healthy workspace whose world may be reused."""
    class _Lease:
        id, status, endpoint = uuid4(), "ready", "http://127.0.0.1:9931"
        seeded_version_id = None
        restore_done = restore_total = restore_reason = None
    monkeypatch.setattr(live.workspace, "acquire", lambda db, aid, **kw: _Lease())
    monkeypatch.setattr(live.workspace, "active_lease", lambda db, aid, **kw: _Lease())
    monkeypatch.setattr(live.workspace, "mark_seeded", lambda db, lease, **kw: None)
    monkeypatch.setattr(live.workspace, "clear_seed_mark", lambda db, lease: None)
    monkeypatch.setattr(live.workspace, "touch", lambda db, lease: None)
    monkeypatch.setattr(live.workspace, "holds_seeded_world", lambda *a, **kw: preserved)


def test_a_reusable_world_is_not_reseeded(client, attempt, live_service, monkeypatch):
    """The whole point. An annotator who closed the pane and came back must find
    the world they were building, not the task seed."""
    _isolated(monkeypatch, preserved=True)
    r = client.post(f"/api/sessions/{attempt}/live")
    assert r.status_code == 200, r.text
    assert r.json()["world"] == "preserved"
    assert live_service.reset_calls == [], "reusing a world must not reset it"


def test_a_world_that_cannot_be_vouched_for_is_reseeded(client, attempt, live_service, monkeypatch):
    """Fail closed: the cost of a needless reseed is a few seconds, the cost of a
    wrong reuse is an annotator recording against somebody else's world."""
    _isolated(monkeypatch, preserved=False)
    r = client.post(f"/api/sessions/{attempt}/live")
    assert r.status_code == 200, r.text
    assert r.json()["world"] == "seeded"
    assert len(live_service.reset_calls) == 1


def test_the_shared_gym_is_always_reseeded(client, attempt, live_service, monkeypatch):
    """Isolation off means the world may be someone else's, no matter what any
    marker claims. This is the M46/M15 guard and it must survive the feature."""
    monkeypatch.setattr(live.workspace, "holds_seeded_world", lambda *a, **kw: True)
    r = client.post(f"/api/sessions/{attempt}/live")
    assert r.status_code == 200, r.text
    assert r.json()["world"] == "seeded"
    assert len(live_service.reset_calls) == 1


def test_reset_world_rebuilds_from_the_seed(client, attempt, live_service, monkeypatch):
    _isolated(monkeypatch, preserved=True)
    assert client.post(f"/api/sessions/{attempt}/live").json()["world"] == "preserved"
    assert live_service.reset_calls == []

    r = client.post(f"/api/sessions/{attempt}/live/reset-world")
    assert r.status_code == 200, r.text
    assert r.json() == {"reset": True, "world": "seeded", "restore": None}
    assert len(live_service.reset_calls) == 1, "the escape hatch must actually reset"


def test_reset_world_is_audited(client, attempt, db_session, live_service, monkeypatch):
    _isolated(monkeypatch, preserved=True)
    client.post(f"/api/sessions/{attempt}/live")
    client.post(f"/api/sessions/{attempt}/live/reset-world")
    actions = [a.action for a in db_session.scalars(select(models.AuditLog))]
    assert "live.reset_world" in actions, "discarding an annotator's work must leave a trace"


def test_reset_world_belongs_to_its_owner(client_for, attempt, live_service, monkeypatch):
    """404, not 403 — ownership is never disclosed. Discarding somebody else's
    hour of work is exactly the kind of thing an ownership check is for."""
    _isolated(monkeypatch, preserved=True)
    stranger = client_for("stranger@test")
    assert stranger.post(f"/api/sessions/{attempt}/live/reset-world").status_code == 404
    assert live_service.reset_calls == []


def test_a_failed_reset_leaves_the_world_alone(client, attempt, live_service, monkeypatch):
    _isolated(monkeypatch, preserved=True)
    client.post(f"/api/sessions/{attempt}/live")
    monkeypatch.setattr(GymEndpoint, "reset", lambda self, task_id, seed=0: None)
    r = client.post(f"/api/sessions/{attempt}/live/reset-world")
    assert r.status_code == 409
    assert "left as it was" in r.json()["detail"]


def test_reattaching_reports_the_world_as_preserved(client, attempt, live_service, monkeypatch):
    """Re-attach resets nothing, so it must not report "seeded" — an annotator an
    hour into a world would read that as their work having just been discarded."""
    _isolated(monkeypatch, preserved=False)
    first = client.post(f"/api/sessions/{attempt}/live").json()
    assert first["world"] == "seeded"
    again = client.post(f"/api/sessions/{attempt}/live").json()
    assert again["sessionId"] == first["sessionId"], "this must be a re-attach, not a new browser"
    assert again["world"] == "preserved"
    assert len(live_service.reset_calls) == 1, "re-attaching must never reseed"
