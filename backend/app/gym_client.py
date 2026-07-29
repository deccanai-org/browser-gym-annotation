"""Live gym bridge (M6c).

Thin HTTP client for the running ecommerce-browser-gym harness. It lets the
annotator reset a real gym task and read the TRUE milestone verdict evaluated
against the live world state — the ground truth the fixtures only approximate.
Agent execution (Playwright) stays gym-side; this reads + verifies over HTTP.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from app.config import settings


class GymTaskNotFound(Exception):
    """The gym responded 404 — the task_id is unknown (distinct from unreachable)."""


class GymBadRequest(Exception):
    """The gym responded 4xx (e.g. 422 bad-state overlay) — a precise upstream
    diagnostic that must be surfaced, NOT collapsed into a generic 502."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _req(method: str, path: str, body: dict | None = None, timeout: int = 20, base_url: str | None = None) -> dict | None:
    # base_url targets ONE workspace's gym (per-annotator isolation). Omitted =>
    # the single shared settings.gym_url, which is the legacy/default behaviour.
    url = (base_url or settings.gym_url).rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"content-type": "application/json", "X-Harness-Token": settings.gym_harness_token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:  # a real HTTP response — distinguish the meaning
        if e.code == 404:
            raise GymTaskNotFound(path) from e
        if e.code in (400, 422):  # a precise client-side diagnostic (e.g. bad state overlay)
            try:
                detail = json.loads(e.read()).get("detail", "bad request")
            except Exception:  # noqa: BLE001
                detail = "bad request"
            raise GymBadRequest(e.code, str(detail)) from e
        return None
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None


def tasks() -> list[str] | None:
    d = _req("GET", "/_harness/tasks")
    return d.get("tasks") if d else None


def reset(task_id: str, seed: int = 0) -> dict | None:
    return _req("POST", "/_harness/reset", {"task_id": task_id, "seed": seed})


def snapshot() -> dict | None:
    return _req("GET", "/_harness/snapshot")


def world() -> dict | None:
    return _req("GET", "/_harness/world")


def state() -> dict | None:
    """The real post-run shop GymState (cart / orders / returns / subscriptions /
    account) read-only from GET /_harness/state — the true world the agent left."""
    return _req("GET", "/_harness/state")


def verify(step: int = 0) -> dict | None:
    return _req("POST", "/_harness/verify", {"step": step})


def run_agent(task_id: str, agent: str = "oracle", seed: int = 0, brief: str | None = None) -> dict | None:
    """Trigger a full agent run in the gym (reset → drive → verify). Slow — the
    gym drives a real browser — so allow a generous timeout. `brief` overrides the
    task's instruction (annotator prompt edit → a fresh run under the new prompt)."""
    body: dict = {"agent": agent, "task_id": task_id, "seed": seed}
    if brief:
        body["brief"] = brief
    return _req("POST", "/_harness/run_agent", body, timeout=260)


def load_state(task_id: str, seed: int, state: dict, step: int | None = None) -> dict | None:
    """Resume the gym from a corrected mid-episode world state (reset-to-seed +
    overlay). See POST /_harness/load_state."""
    body: dict = {"task_id": task_id, "seed": seed, "state": state}
    if step is not None:
        body["step"] = step
    return _req("POST", "/_harness/load_state", body)


def resume_run(task_id: str, seed: int, state: dict, url: str, step: int | None = None, agent: str = "llm", correction: str = "") -> dict | None:
    """Drive-forward resume: load a corrected world into the gym and drive an
    OBSERVING agent forward from the mid-episode URL, then verify. Slow (a real
    agent run) + stochastic for LLM agents. `correction` is the reviewer's
    natural-language instruction, injected into the agent's context so the re-run
    is actually steered. See POST /_harness/resume_run."""
    body: dict = {"agent": agent, "task_id": task_id, "seed": seed, "state": state, "url": url or "/"}
    if step is not None:
        body["step"] = step
    if correction:
        body["correction"] = correction
    return _req("POST", "/_harness/resume_run", body, timeout=300)


def resume_verify(task_id: str, seed: int, state: dict, url_trail: list[str], final_url: str = "") -> dict | None:
    """Load a corrected state, then replay /_harness/verify across the trajectory's
    per-step URLs so path-progression milestones fire, and return the REAL final
    milestone verdict on the resumed corrected world."""
    if load_state(task_id, seed, state) is None:
        return None
    last = None
    for i, u in enumerate(url_trail):
        last = _req("POST", "/_harness/verify", {"url": u or "/", "step": i})
    tail = final_url or (url_trail[-1] if url_trail else "/")
    final = _req("POST", "/_harness/verify", {"url": tail, "step": len(url_trail)})
    return final or last


def _screenshot_bytes(path: str, base_url: str | None = None) -> bytes | None:
    """Raw PNG fetch, shared by the module function and GymEndpoint (which binds a
    per-workspace base_url)."""
    import urllib.parse
    url = (base_url or settings.gym_url).rstrip("/") + "/_harness/screenshot?path=" + urllib.parse.quote(path, safe="")
    req = urllib.request.Request(url, headers={"X-Harness-Token": settings.gym_harness_token})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except (urllib.error.URLError, TimeoutError):
        return None


def screenshot(path: str) -> bytes | None:
    """Fetch a per-step screenshot PNG (raw bytes) from the gym."""
    return _screenshot_bytes(path)


def available() -> bool:
    return _req("GET", "/_harness/tasks") is not None


class GymEndpoint:
    """A gym bound to ONE workspace endpoint.

    The module-level functions above target the single shared ``settings.gym_url``
    (legacy/default). A per-session workspace uses an instance instead, so two
    annotators never mutate the same world — the gym keeps one global ``SESSION``
    per process, so isolation must come from talking to a *different process*.
    """

    __slots__ = ("base_url",)

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    # --- read ---------------------------------------------------------------
    def tasks(self) -> list[str] | None:
        d = _req("GET", "/_harness/tasks", base_url=self.base_url)
        return d.get("tasks") if d else None

    def snapshot(self) -> dict | None:
        return _req("GET", "/_harness/snapshot", base_url=self.base_url)

    def world(self) -> dict | None:
        return _req("GET", "/_harness/world", base_url=self.base_url)

    def state(self) -> dict | None:
        return _req("GET", "/_harness/state", base_url=self.base_url)

    def available(self) -> bool:
        return _req("GET", "/_harness/tasks", base_url=self.base_url) is not None

    # --- mutate / drive -----------------------------------------------------
    def reset(self, task_id: str, seed: int = 0) -> dict | None:
        return _req("POST", "/_harness/reset", {"task_id": task_id, "seed": seed}, base_url=self.base_url)

    def verify(self, step: int = 0) -> dict | None:
        return _req("POST", "/_harness/verify", {"step": step}, base_url=self.base_url)

    def tick(self, step: int = 0) -> dict | None:
        """Advance the deterministic clock and flush any now-due scheduled event.

        This is the ONLY thing that calls scheduler.advance_and_flush, so without
        it a task with a schedule never receives its async email or price change
        — and every world from that step on is missing something the recording
        has. The backfill ticks when it reconstructs those tasks; a replay that
        cannot tick could never reproduce what the backfill wrote.
        """
        return _req("POST", "/_harness/tick", {"step": step}, base_url=self.base_url)

    def load_state(self, task_id: str, seed: int, state: dict, step: int | None = None) -> dict | None:
        body: dict = {"task_id": task_id, "seed": seed, "state": state}
        if step is not None:
            body["step"] = step
        return _req("POST", "/_harness/load_state", body, base_url=self.base_url)

    def run_agent(self, task_id: str, agent: str = "oracle", seed: int = 0, brief: str | None = None) -> dict | None:
        body: dict = {"agent": agent, "task_id": task_id, "seed": seed}
        if brief:
            body["brief"] = brief
        return _req("POST", "/_harness/run_agent", body, timeout=260, base_url=self.base_url)

    def resume_run(self, task_id: str, seed: int, state: dict, url: str, step: int | None = None,
                   agent: str = "llm", correction: str = "") -> dict | None:
        body: dict = {"agent": agent, "task_id": task_id, "seed": seed, "state": state, "url": url or "/"}
        if step is not None:
            body["step"] = step
        if correction:
            body["correction"] = correction
        return _req("POST", "/_harness/resume_run", body, timeout=300, base_url=self.base_url)

    def screenshot(self, path: str) -> bytes | None:
        return _screenshot_bytes(path, base_url=self.base_url)


class LiveBrowserClient:
    """A live browser session — the executor a manual/replay flow drives.

    Separate from GymEndpoint on purpose: the gym owns world state, the live
    browser service owns a browser, and neither imports the other. This pairs
    them, because validating a committed sequence needs both — the actions go to
    the browser, the state comparison reads the gym.
    """

    __slots__ = ("base_url", "session_id", "ticket", "gym")

    def __init__(self, base_url: str, session_id: str, ticket: str, gym: "GymEndpoint | None" = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self.ticket = ticket
        self.gym = gym

    def _post(self, path: str, body: dict, timeout: int = 30) -> dict | None:
        url = f"{self.base_url}/live/sessions/{self.session_id}{path}"
        req = urllib.request.Request(
            url, data=json.dumps({**body, "ticket": self.ticket}).encode(),
            method="POST", headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError):
            return None

    # --- replay.Executor ----------------------------------------------------
    def act(self, kind: str, locator: dict | None, args: dict | None) -> dict:
        out = self._post("/act", {"kind": kind, "locator": locator or {}, "args": args or {}})
        # An unreachable live browser is NOT a failed action — say so, so the
        # annotator isn't told their trajectory is wrong when the service is down.
        return out or {"ok": False, "error": "live browser unreachable"}

    def describe(self, x: float, y: float) -> dict:
        return self._post("/describe", {"x": x, "y": y}) or {}

    def world(self) -> dict | None:
        return self.gym.world() if self.gym is not None else None

    def info(self) -> dict | None:
        url = f"{self.base_url}/live/sessions/{self.session_id}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as r:
                return json.loads(r.read())
        except Exception:  # noqa: BLE001
            return None
