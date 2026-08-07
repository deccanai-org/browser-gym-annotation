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
        # A real delta: a zero-delta scroll is now folded away as trackpad jitter,
        # and this test is about sequence ordering, not scroll semantics.
        payload = {"dy": 300} if k == "scroll" else None
        recorder.record_event(db_session, attempt_id=attempt.id, kind=k, payload=payload)
    db_session.commit()
    # Raw events, not folded actions: scroll no longer becomes a step, but every
    # interaction is still appended to the audit log in order.
    from sqlalchemy import select as _select

    from app import models as _m
    seqs = [e.seq for e in db_session.scalars(
        _select(_m.InteractionEvent)
        .where(_m.InteractionEvent.attempt_id == attempt.id)
        .order_by(_m.InteractionEvent.seq)
    ).all()]
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
    # `long_press`, not `press`: `press` was shared with non-printable KEYS, and
    # the executor maps act("press") to key(args.key or "Enter") — so a slider
    # held for five seconds committed as "press Enter".
    assert [a["kind"] for a in out] == ["long_press"]


def test_a_click_the_page_could_not_name_is_still_a_click():
    """The single defect behind 14 of the 90 human steps recorded so far.

    `_same_target` answers False for two unidentified targets on purpose — that
    is what stops one typing run splitting into two fills. But the pointer branch
    treated "not the same element" as sufficient for a DRAG, so any click whose
    element the page could not name came out as `drag`: no locator, description
    "drag", not replayable, and untrue. Every one of the 14 had an empty locator
    and 13 had not moved by a pixel.
    """
    out = recorder.coalesce([
        {"seq": 1, "kind": "mousePressed", "target": {}, "payload": {"nx": 0.5, "ny": 0.4}, "t": 0},
        {"seq": 2, "kind": "mouseReleased", "target": {}, "payload": {"nx": 0.5, "ny": 0.4}, "t": 120},
    ])
    assert [a["kind"] for a in out] == ["click"]


def test_a_press_that_travels_is_still_a_drag():
    """The guard above must not cost us real drags."""
    out = recorder.coalesce([
        {"seq": 1, "kind": "mousePressed", "target": {"testId": "slider"}, "payload": {"nx": 0.20, "ny": 0.5}, "t": 0},
        {"seq": 2, "kind": "mouseReleased", "target": {"testId": "slider"}, "payload": {"nx": 0.80, "ny": 0.5}, "t": 300},
    ])
    assert [a["kind"] for a in out] == ["drag"]


def test_a_short_drag_between_two_named_elements_is_still_a_drag():
    """Dragging a card onto an adjacent column moves only a few pixels, so
    distance alone cannot decide it — two DIFFERENT named targets still can."""
    out = recorder.coalesce([
        {"seq": 1, "kind": "mousePressed", "target": {"testId": "card-7"}, "payload": {"nx": 0.500, "ny": 0.5}, "t": 0},
        {"seq": 2, "kind": "mouseReleased", "target": {"testId": "col-done"}, "payload": {"nx": 0.502, "ny": 0.5}, "t": 200},
    ])
    assert [a["kind"] for a in out] == ["drag"]


@pytest.mark.parametrize("kind", sorted(recorder.ENVIRONMENT_KINDS))
def test_something_the_page_did_itself_is_not_a_step(kind):
    """A popup the page opened is not an action an annotator took, and a shipped
    sample that lists it teaches a policy to emit `popup`. `coalesce` used to end
    in a catch-all that promoted every unrecognised kind — 15 of 90 human steps
    came out as bare `popup` with no target."""
    out = recorder.coalesce([
        {"seq": 1, "kind": kind, "target": {}, "payload": {"type": "notice"}, "t": 0},
        {"seq": 2, "kind": "mousePressed", "target": {"testId": "ok"}, "payload": {}, "t": 50},
        {"seq": 3, "kind": "mouseReleased", "target": {"testId": "ok"}, "payload": {}, "t": 90},
    ])
    assert [a["kind"] for a in out] == ["click"], f"{kind} must not become a step"


def test_an_unknown_action_kind_is_still_kept():
    """The gate is a deny-list on purpose: dropping a real action that was added
    later is far worse than keeping a stray one."""
    out = recorder.coalesce([{"seq": 1, "kind": "hover", "target": {"testId": "x"}, "payload": {}, "t": 0}])
    assert [a["kind"] for a in out] == ["hover"]


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
    # The placeholder is NOT emitted as a value. It used to be, and the executor
    # then typed the literal "«redacted»" into the password field — and the replay
    # gate passed it, shipping a green but broken golden. The action is flagged so
    # certification refuses it until a human supplies the value.
    assert out[0]["payload"]["value"] is None
    assert out[0]["needsValue"] is True


def test_a_scroll_never_becomes_a_step():
    """Scroll is viewport motion, not a state change: it changes nothing a
    verifier reads, and it varies with screen size and zoom, so it is not
    reproducible across annotators. The raw events survive for audit (see
    test_a_gated_scroll_still_advances_the_fold below); only the promotion to a
    step is suppressed."""
    out = recorder.coalesce([
        {"seq": 1, "kind": "scroll", "payload": {"auto": True, "dy": 300}},
        {"seq": 2, "kind": "scroll", "payload": {"dy": 120}},
    ])
    assert out == []


def test_a_gated_scroll_still_advances_the_fold(db_session, attempt):
    """The scroll events must be CONSUMED, not skipped: if they were left
    unfolded the materialize watermark would never pass them and every later
    batch would re-read them forever."""
    for i in range(3):
        recorder.record_event(db_session, attempt_id=attempt.id, kind="scroll",
                              payload={"dy": 300, "t": 1000 + i})
    recorder.record_event(db_session, attempt_id=attempt.id, kind="navigate",
                          payload={"url": "/cart"})
    db_session.commit()
    # every raw event is still in the append-only log...
    from sqlalchemy import select as _select

    from app import models as _m
    raw = db_session.scalars(
        _select(_m.InteractionEvent)
        .where(_m.InteractionEvent.attempt_id == attempt.id)
        .order_by(_m.InteractionEvent.seq)
    ).all()
    assert [e.kind for e in raw] == ["scroll", "scroll", "scroll", "navigate"]
    # ...but only the navigate becomes an action
    assert [a["kind"] for a in recorder.candidate_actions(db_session, attempt.id)] == ["navigate"]


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


# --------------------------------------------------------------------------- fidelity
# These lock in the fixes that make a HUMAN-recorded trajectory faithful. Each one
# is a bug that used to ship a plausible-looking but wrong golden.

def _k(seq, kind, t, target, **payload):
    return {"seq": seq, "kind": kind, "target": target,
            "payload": {"t": t, **payload}, "t": t, "url": "/", "tab": "t1"}


def test_backspace_yields_the_field_s_real_value_not_the_keys_we_sent():
    """"mug" + Backspace + "s" is ONE fill of "mus".

    It used to become fill("mug"), press(Backspace), fill("s") — and because the
    executor's fill REPLACES the field, replaying that produced "s". The value now
    comes from the page via the ack, so editing keys are absorbed into the edit.
    """
    t = {"targetKey": "q", "testId": "q"}
    out = recorder.coalesce([
        _k(1, "keyChar", 0, t, text="m", value="m"),
        _k(2, "keyChar", 100, t, text="u", value="mu"),
        _k(3, "keyChar", 200, t, text="g", value="mug"),
        _k(4, "keyPress", 300, t, key="Backspace", value="mu"),
        _k(5, "keyChar", 400, t, text="s", value="mus"),
    ])
    assert [a["kind"] for a in out] == ["fill"]
    assert out[0]["payload"]["value"] == "mus"


def test_a_press_that_travels_is_a_drag_not_a_click():
    """The pane used to synthesise the release 1ms after the press, so every drag
    folded into a click at the START point — losing the gesture entirely."""
    t = {"targetKey": "slider", "testId": "slider"}
    out = recorder.coalesce([
        _k(1, "mouseDown", 0, t, nx=0.10, ny=0.5, button="left"),
        _k(2, "mouseUp", 200, t, nx=0.60, ny=0.5, button="left", clicks=1),
    ])
    assert out[0]["kind"] == "drag"
    assert out[0]["payload"]["from"]["nx"] == 0.10


def test_a_right_click_is_not_recorded_as_a_left_click():
    t = {"targetKey": "row", "testId": "row"}
    out = recorder.coalesce([
        _k(1, "mouseDown", 0, t, nx=0.5, ny=0.5, button="right"),
        _k(2, "mouseUp", 30, t, nx=0.5, ny=0.5, button="right", clicks=1),
    ])
    assert out[0]["kind"] == "click" and out[0]["payload"]["button"] == "right"


def test_a_double_click_is_one_action_not_two():
    t = {"targetKey": "cell", "testId": "cell"}
    out = recorder.coalesce([
        _k(1, "mouseDown", 0, t, nx=0.5, ny=0.5), _k(2, "mouseUp", 30, t, nx=0.5, ny=0.5, clicks=2),
    ])
    assert out[0]["kind"] == "dblclick"


def test_a_wheel_flick_is_consumed_and_the_next_action_still_folds():
    """20 wheel ticks used to coalesce into one scroll step. They now produce no
    step at all — but the click that follows must still fold normally, which is
    what proves the gate consumes rather than derails."""
    t = {"targetKey": "list"}
    flick = [_k(i, "scroll", i * 20, t, dy=30, nx=0.5, ny=0.5) for i in range(1, 21)]
    assert recorder.coalesce(flick) == []
    jitter = [_k(1, "scroll", 0, t, dy=5, nx=0.5, ny=0.5), _k(2, "scroll", 50, t, dy=-3, nx=0.5, ny=0.5)]
    assert recorder.coalesce(jitter) == []
    after = flick + [_k(21, "mouseDown", 500, t, nx=0.5, ny=0.5),
                     _k(22, "mouseUp", 530, t, nx=0.5, ny=0.5, clicks=1)]
    assert [a["kind"] for a in recorder.coalesce(after)] == ["click"]


def test_two_unnamed_targets_are_not_the_same_element():
    """`{} == {}` used to be True, so every field the page could not name folded
    into a single fill with another."""
    out = recorder.coalesce([
        _k(1, "keyChar", 0, {}, text="a", value="a"),
        _k(2, "keyChar", 100, {}, text="b", value="b"),
    ])
    assert len(out) == 2


def test_typed_text_is_redacted_under_every_kind_the_client_sends(db_session, attempt):
    """Redaction keys off the event KIND, so renaming a kind silently disables it.

    The client's vocabulary changed (`key` -> `keyChar`, plus `keyPress`/`paste`);
    if this list falls behind, a password goes into an append-only log in clear.
    """
    pw = {"testId": "input-password", "type": "password"}
    for kind in ("key", "keyChar", "keyPress", "type", "fill", "paste"):
        ev = recorder.record_event(
            db_session, attempt_id=attempt.id, kind=kind,
            payload={"text": "hunter2", "value": "hunter2"}, target=pw,
        )
        assert ev.payload.get("redacted") is True, kind
        assert "hunter2" not in str(ev.payload), f"{kind} leaked the secret"
