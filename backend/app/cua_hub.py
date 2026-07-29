"""Registry + URL builder for the realistic cua-hub mock UIs (20-task pilot).

Additive and self-contained: nothing here changes the existing gym-origin live or
review path. When the pilot wires live navigation, ``api/live.py`` will call
``mock_url(...)`` to send the browser to the realistic UI + its seed SID instead of
the local gym origin, and ``allowed_sites(...)`` to show the new hosts on the task
card. Until then this is just the mapping, ready to plug in.

These replace the old ``*.gym.local`` placeholder mocks:

    shop     -> Amazon           cua-hub-amazon.<domain>
    mail     -> Gmail            cua-hub-gmail.<domain>
    market   -> eBay             cua-hub-ebay.<domain>
    calendar -> Google Calendar  cua-hub-google-calendar.<domain>
    food     -> Uber Eats        cua-hub-uber-eats.<domain>

Env overrides:
    CUA_HUB_DOMAIN     default ``delta.deccanexperts.ai``
    CUA_HUB_SCHEME     default ``https``
    CUA_HUB_SID_PARAM  the URL param a mock reads its seed SID from — default ``sid``.
                       CONFIRM with Kashyap: the exact param name + whether the mock
                       pulls its ``mock_states`` row by that SID on load.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

CUA_HUB_DOMAIN = os.environ.get("CUA_HUB_DOMAIN", "delta.deccanexperts.ai")
CUA_HUB_SCHEME = os.environ.get("CUA_HUB_SCHEME", "https")
# TODO(kashyap): confirm the exact URL param a cua-hub mock loads its seed SID from.
CUA_HUB_SID_PARAM = os.environ.get("CUA_HUB_SID_PARAM", "sid")


@dataclass(frozen=True)
class MockApp:
    key: str        # annotator app key (matches the frontend APP_COLOR map)
    mock_key: str   # the `mock` column value in cua-gym (mock_states / mock_state_events)
    subdomain: str  # cua-hub-<subdomain>.<domain>
    title: str


REGISTRY: dict[str, MockApp] = {
    "shop": MockApp("shop", "amazon_mock", "amazon", "Amazon"),
    "mail": MockApp("mail", "gmail_mock", "gmail", "Gmail"),
    "market": MockApp("market", "ebay_mock", "ebay", "eBay"),
    "calendar": MockApp("calendar", "google_calendar", "google-calendar", "Google Calendar"),
    "food": MockApp("food", "uber_eats_mock", "uber-eats", "Uber Eats"),
}


def host(app_key: str) -> str:
    """Bare host for an app's realistic UI, e.g. ``cua-hub-amazon.delta.deccanexperts.ai``."""
    return f"cua-hub-{REGISTRY[app_key].subdomain}.{CUA_HUB_DOMAIN}"


def base_url(app_key: str) -> str:
    return f"{CUA_HUB_SCHEME}://{host(app_key)}"


def mock_url(app_key: str, start_path: str = "/", seed_sid: str | None = None) -> str:
    """The URL the live browser opens: the realistic UI at ``start_path`` carrying the seed SID.

    Handles hash-routed SPAs (e.g. Gmail's ``/#/inbox``) by keeping the query
    before the fragment. The exact SID param is CUA_HUB_SID_PARAM (TBD w/ Kashyap).
    """
    if not start_path.startswith(("/", "#", "?")):
        start_path = "/" + start_path
    parts = urlsplit(start_path)
    query = parts.query
    if seed_sid:
        extra = urlencode({CUA_HUB_SID_PARAM: seed_sid})
        query = f"{query}&{extra}" if query else extra
    return base_url(app_key) + urlunsplit(("", "", parts.path or "/", query, parts.fragment))


def allowed_sites(app_keys: list[str]) -> list[dict]:
    """Task-card ``allowedSites`` entries for the realistic UIs (host + app key + title)."""
    return [
        {"host": host(k), "app": REGISTRY[k].key, "title": REGISTRY[k].title}
        for k in app_keys
        if k in REGISTRY
    ]
