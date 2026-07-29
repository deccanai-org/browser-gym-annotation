"""Recorder: raw capture, redaction, and the action boundaries that turn an event
stream into replayable actions."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app import models, recorder


@pytest.fixture()
def attempt(db_session):
    task = models.Task(external_id=f"RC-{uuid4().hex[:8]}", title="t", prompt="p", source="gym")
    ann = models.Annotator(email=f"rc-{uuid4().hex[:8]}@test")
    db_session.add_all([task, ann])
    db_session.flush()
    s = models.ReviewSession(task_id=task.id, annotator_id=ann.id, source="gym")
    db_session.add(s)
    db_session.commit()
    return s


# --------------------------------------------------------------------------- raw capture
def test_events_are_appended_with_a_monotonic_sequence(db_session, attempt):
    for k in ("navigate", "click", "scroll"):
        recorder.record_event(db_session, attempt_id=attempt.id, kind=k)
    db_session.commit()
    seqs = [e["seq"] for e in recorder.candidate_actions(db_session, attempt.id)]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3


def test_exploration_is_recorded_but_is_not_a_committed_step(db_session, attempt):
    """Raw events must never appear as trajectory steps on their own — the whole
    point of the split is that exploring cannot pollute the golden."""
    recorder.record_event(db_session, attempt_id=attempt.id, kind="click")
    db_session.commit()
    assert db_session.query(models.InteractionEvent).count() == 1
    assert db_session.query(models.TrajectoryStep).count() == 0


# --------------------------------------------------------------------------- redaction
@pytest.mark.parametrize("target", [
    {"type": "password"},
    {"name": "cardNumber"},
    {"testId": "input-cvv"},
    {"label": "Social Security Number"},
    {"autocomplete": "current-password"},
])
def test_sensitive_values_are_redacted_at_record_time(db_session, attempt, target):
    """Redaction has to happen on the way IN — an append-only log cannot be
    retroactively cleaned."""
    ev = recorder.record_event(
        db_session, attempt_id=attempt.id, kind="type",
        payload={"text": "hunter2", "value": "hunter2"}, target=target,
    )
    db_session.commit()
    assert "hunter2" not in str(ev.payload)
    assert ev.payload["redacted"] is True


def test_ordinary_fields_are_not_redacted(db_session, attempt):
    ev = recorder.record_event(
        db_session, attempt_id=attempt.id, kind="type",
        payload={"text": "blue mug"}, target={"name": "search"},
    )
    db_session.commit()
    assert ev.payload["text"] == "blue mug"
    assert not ev.payload.get("redacted")


# --------------------------------------------------------------------------- boundaries
def test_press_and_release_become_one_click():
    t = {"testId": "btn-cart"}
    out = recorder.coalesce([
        {"seq": 1, "kind": "mousePressed", "target": t, "payload": {"t": 100}, "t": 100},
        {"seq": 2, "kind": "mouseReleased", "target": t, "payload": {"t": 160}, "t": 160},
    ])
    assert [a["kind"] for a in out] == ["click"]
    assert out[0]["sources"] == [1, 2], "the click must point back at both raw events"


def test_a_long_gap_is_not_folded_into_a_click():
    """A press held for seconds is a drag or long-press, not a click; folding it
    would replay something the human did not do."""
    t = {"testId": "slider"}
    out = recorder.coalesce([
        {"seq": 1, "kind": "mousePressed", "target": t, "t": 0},
        {"seq": 2, "kind": "mouseReleased", "target": t, "t": 5000},
    ])
    assert [a["kind"] for a in out] == ["press", "mouseReleased"]


def test_keystrokes_coalesce_into_one_fill_with_the_final_value():
    """A trajectory should say 'type the answer', not replay twelve keystrokes."""
    t = {"testId": "input-search"}
    out = recorder.coalesce([
        {"seq": 1, "kind": "key", "target": t, "payload": {"text": "m", "value": "m", "t": 0}, "t": 0},
        {"seq": 2, "kind": "key", "target": t, "payload": {"text": "u", "value": "mu", "t": 90}, "t": 90},
        {"seq": 3, "kind": "key", "target": t, "payload": {"text": "g", "value": "mug", "t": 180}, "t": 180},
    ])
    assert [a["kind"] for a in out] == ["fill"]
    assert out[0]["payload"]["value"] == "mug"
    assert out[0]["sources"] == [1, 2, 3]


def test_typing_into_a_different_field_starts_a_new_fill():
    a, b = {"testId": "first"}, {"testId": "second"}
    out = recorder.coalesce([
        {"seq": 1, "kind": "key", "target": a, "payload": {"value": "x", "t": 0}, "t": 0},
        {"seq": 2, "kind": "key", "target": b, "payload": {"value": "y", "t": 50}, "t": 50},
    ])
    assert [x["kind"] for x in out] == ["fill", "fill"]
    assert [x["payload"]["value"] for x in out] == ["x", "y"]


def test_a_redacted_keystroke_stays_redacted_after_coalescing():
    """Coalescing must not reconstruct a secret from its parts."""
    t = {"testId": "input-password"}
    out = recorder.coalesce([
        {"seq": 1, "kind": "key", "target": t, "payload": {"text": "«redacted»", "redacted": True, "t": 0}, "t": 0},
        {"seq": 2, "kind": "key", "target": t, "payload": {"text": "«redacted»", "redacted": True, "t": 40}, "t": 40},
    ])
    assert out[0]["payload"]["redacted"] is True
    assert out[0]["payload"]["value"] == "«redacted»"


def test_automatic_scrolls_are_dropped_but_user_scrolls_are_kept():
    """A page scrolling itself is not a human action; replaying it fights the page."""
    out = recorder.coalesce([
        {"seq": 1, "kind": "scroll", "payload": {"auto": True, "dy": 300}},
        {"seq": 2, "kind": "scroll", "payload": {"dy": 120}},
    ])
    assert [a["kind"] for a in out] == ["scroll"]
    assert out[0]["sources"] == [2]


def test_navigation_survives_coalescing_untouched():
    out = recorder.coalesce([{"seq": 1, "kind": "navigate", "payload": {"url": "/cart"}}])
    assert out[0]["kind"] == "navigate" and out[0]["payload"]["url"] == "/cart"


# --------------------------------------------------------------------------- locators
def test_semantic_locator_prefers_durable_identifiers():
    loc = recorder.semantic_locator({
        "testId": "link-cart", "role": "link", "name": "Cart",
        "id": "cart", "selector": "div > a:nth-child(3)", "text": "Cart",
    })
    assert loc["testId"] == "link-cart"
    assert loc["role"] == "link" and loc["name"] == "Cart"
    assert "css" in loc, "the brittle selector is kept as a fallback, not dropped"


def test_semantic_locator_degrades_gracefully():
    assert recorder.semantic_locator({}) == {}
    assert recorder.semantic_locator({"role": "button", "name": "Save"}) == {"role": "button", "name": "Save"}


# --------------------------------------------------------------------------- fail closed
def test_a_keystroke_with_no_named_target_is_redacted(db_session, attempt):
    """The asymmetry decides this. A wrong redaction costs one recoverable search
    query; a missed one writes a password into an append-only log forever. The
    client is expected to name the focused element, so an unnamed one means
    something already went wrong — exactly when guessing is worst."""
    for target in ({}, None):
        ev = recorder.record_event(
            db_session, attempt_id=attempt.id, kind="key",
            payload={"text": "hunter2"}, target=target,
        )
        assert ev.payload["redacted"] is True
        assert "hunter2" not in str(ev.payload)
    db_session.commit()


def test_a_named_ordinary_field_is_still_not_redacted(db_session, attempt):
    """Failing closed must not swallow every legitimate keystroke."""
    ev = recorder.record_event(
        db_session, attempt_id=attempt.id, kind="key",
        payload={"text": "blue mug"}, target={"testId": "input-search", "type": "text"},
    )
    db_session.commit()
    assert ev.payload["text"] == "blue mug" and not ev.payload.get("redacted")


def test_non_typing_events_are_untouched_by_the_empty_target_rule(db_session, attempt):
    """A click carries no secret; redacting it would destroy the locator trail."""
    ev = recorder.record_event(db_session, attempt_id=attempt.id, kind="click", payload={"nx": 0.5}, target={})
    db_session.commit()
    assert not ev.payload.get("redacted") and ev.payload["nx"] == 0.5
