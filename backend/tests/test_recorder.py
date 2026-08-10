"""Recorder: raw capture, redaction, and the action boundaries that turn an event
stream into replayable actions."""

from __future__ import annotations

import time
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
    """It used to come out as `click` with the button only in the payload — and
    the executor's `click` takes no button, so it replayed as a LEFT click and
    certify stamped it verified. A step reading "right-click Save for later"
    shipped as a left click that reported ok."""
    t = {"targetKey": "row", "testId": "row"}
    out = recorder.coalesce([
        _k(1, "mouseDown", 0, t, nx=0.5, ny=0.5, button="right"),
        _k(2, "mouseUp", 30, t, nx=0.5, ny=0.5, button="right", clicks=1),
    ])
    assert out[0]["kind"] == "right_click" and out[0]["payload"]["button"] == "right"
    # It used to have to be OUTSIDE the vocabulary — failing loudly was the only
    # honest option while the executor could not press a right button. It can
    # now, with no JS fallback, so the honest answer changed from "refuse" to
    # "perform the real gesture".
    assert out[0]["kind"] in recorder.EXECUTOR_KINDS, "the executor performs this now"


def test_a_middle_click_is_not_a_left_click_either():
    t = {"targetKey": "row", "testId": "row"}
    out = recorder.coalesce([
        _k(1, "mouseDown", 0, t, nx=0.5, ny=0.5, button="middle"),
        _k(2, "mouseUp", 30, t, nx=0.5, ny=0.5, button="middle", clicks=1),
    ])
    assert out[0]["kind"] == "middle_click"


def test_pressing_enter_to_submit_becomes_the_executor_s_press():
    """`keyPress` is not a kind the executor answers to, so submitting a search
    with Enter — one of the commonest actions in the corpus — produced a step
    certify could only mark diverged, blocking the whole trajectory."""
    t = {"targetKey": "q", "testId": "input-search"}
    out = recorder.coalesce([
        _k(1, "keyChar", 0, t, text="m", value="m"),
        _k(2, "keyChar", 100, t, text="ug", value="mug"),
        _k(3, "keyPress", 200, t, key="Enter", value="mug"),
    ])
    assert [a["kind"] for a in out] == ["fill", "press"]
    assert out[1]["payload"]["key"] == "Enter"
    assert all(a["kind"] in recorder.EXECUTOR_KINDS for a in out)


def test_a_key_event_that_names_no_key_is_not_turned_into_a_press():
    """`act("press")` defaults a missing key to Enter, so translating this would
    submit whatever form happened to be open."""
    out = recorder.coalesce([_k(1, "keyPress", 0, {"targetKey": "q", "testId": "q"})])
    assert out[0]["kind"] == "keyPress"


def test_a_modified_key_keeps_its_modifiers_in_the_key_itself():
    """`act()` passes only `args.key` to the browser. Dropping the modifiers made
    Cmd+V replay as a bare "v" typed into the field."""
    t = {"targetKey": "q", "testId": "input-search"}
    out = recorder.coalesce([_k(1, "keyPress", 0, t, key="v", modifiers=["Meta"])])
    assert out[0]["kind"] == "press" and out[0]["payload"]["key"] == "Meta+v"


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


def test_the_executor_vocabulary_is_declared_once():
    """It was declared twice — here and in backfill — and when the executor
    learned right_click/dblclick only one copy was updated. The result showed up
    while doing a real task: a triple-click in a text field folds to a `dblclick`,
    the executor performs it perfectly well, and the step was still marked failed
    with "the executor has no 'dblclick' action"."""
    from app import backfill

    assert backfill.EXECUTOR_KINDS is recorder.EXECUTOR_KINDS


def test_the_kinds_we_claim_the_executor_speaks_are_the_ones_it_speaks():
    """Reads the executor's own dispatch rather than a copy of it. This list
    living in a different repo is exactly why it drifted; a test that restated it
    would drift the same way."""
    import pathlib
    import re

    svc = pathlib.Path("/Users/dhiren/Deccan AI/E Commerce Broswer Gym/live_browser/service.py")
    if not svc.exists():
        import pytest
        pytest.skip("the gym repo is not checked out beside this one")

    body = svc.read_text()
    act = body[body.index("    async def act("):]
    act = act[:act.index("\n    async def ", 10)]
    handled: set[str] = set()
    for m in re.finditer(r'kind (?:==|in) \(?((?:"[a-z_]+"(?:, )?)+)\)?', act):
        handled |= set(re.findall(r'"([a-z_]+)"', m.group(1)))

    missing = recorder.EXECUTOR_KINDS - handled
    assert not missing, f"we claim the executor speaks {sorted(missing)}, and it does not"


def test_a_rich_editors_markup_rides_beside_its_text():
    """The last differing leaf on a real M105 certify.

    ShopMail stores the compose body as `bodyRef.current.innerHTML`, so a fill
    that only knows the text replays it as flat divs: the same words, a different
    body, and the world hash says diverged at the final step. `value` stays plain
    text — that is what the sample is read for, and nobody training on this wants
    a <span> in it — so the markup travels beside it.
    """
    events = [
        {"seq": 1, "kind": "keyChar", "t": 1000, "target": {"targetKey": "body"},
         "payload": {"text": "H", "value": "H", "valueHtml": "<div>H</div>"}},
        {"seq": 2, "kind": "keyChar", "t": 1050, "target": {"targetKey": "body"},
         "payload": {"text": "i", "value": "Hi", "valueHtml": "<div>Hi</div><span class=\'sig\'>--</span>"}},
    ]
    acts = recorder.coalesce(events)
    fill = next(a for a in acts if a["kind"] == "fill")
    assert fill["payload"]["value"] == "Hi", "the readable value stays plain text"
    assert fill["payload"]["valueHtml"] == "<div>Hi</div><span class=\'sig\'>--</span>"


def test_a_plain_input_carries_no_markup_key_at_all():
    """Only a rich editor reports one, and an absent key is how the executor
    knows to fill by text."""
    events = [{"seq": 1, "kind": "keyChar", "t": 1000, "target": {"targetKey": "to"},
               "payload": {"text": "a", "value": "a"}}]
    fill = next(a for a in recorder.coalesce(events) if a["kind"] == "fill")
    assert "valueHtml" not in fill["payload"]


# --------------------------------------------------------------------------- selecting text
ORDER_ID = {"targetKey": "ord", "testId": "order-id", "text": "Order ORD-4417"}


def _selection(selected: str | None = "ORD-4417") -> list[dict]:
    """Press at the start of a run of text, drag across it, release.

    The gesture the pane sends for a selection: an ordinary down/up pair that
    MOVED, plus the string the page says is selected on the release.
    """
    up = {"nx": 0.412, "ny": 0.42, "button": "left", "clicks": 1}
    if selected is not None:
        up["selectedText"] = selected
    return [
        _k(1, "mouseDown", 0, ORDER_ID, nx=0.300, ny=0.42, button="left"),
        _k(2, "mouseUp", 260, ORDER_ID, **up),
    ]


def test_selecting_text_is_not_a_drag():
    """The bug: selecting an order id to read it recorded as `drag`.

    A drag is press-move-release, and so is a selection — so the pointer branch
    folded every selection into a gesture where nothing was dragged, which the
    executor cannot perform and certify therefore refused. Reproduced with the
    exact event pair above: `drag`, with the selected text buried in its payload.

    A selection is worth keeping, not just re-labelling: reading a value is the
    step that explains why the next fill types what it types.
    """
    out = recorder.coalesce(_selection())
    assert [a["kind"] for a in out] == ["select_text"]
    assert out[0]["payload"]["text"] == "ORD-4417", "the point of the step is WHAT was read"
    assert out[0]["target"]["testId"] == "order-id", "and where it was read from"
    assert out[0]["sources"] == [1, 2]


@pytest.mark.parametrize("selected", [None, ""], ids=["absent", "empty"])
def test_a_press_that_travels_with_nothing_selected_is_still_a_drag(selected):
    """The new fold is ADDITIVE: only a release that reports a selection is one.

    An older pane sends no `selectedText` at all and a drag of a real element
    reports it empty — both must keep today's behaviour, or dragging a card
    between columns would quietly start recording as a reading.
    """
    events = _selection(selected)
    events[1]["target"] = {"targetKey": "col-done", "testId": "col-done"}
    assert [a["kind"] for a in recorder.coalesce(events)] == ["drag"]


def test_copying_a_selection_is_still_its_own_press():
    """Cmd+C is the action that makes the selection useful, and folding the
    selection must not swallow it — the copy is a separate step and the executor
    performs it for real."""
    out = recorder.coalesce(_selection() + [_k(3, "keyPress", 900, ORDER_ID, key="c", modifiers=["Meta"])])
    assert [a["kind"] for a in out] == ["select_text", "press"]
    assert out[1]["payload"]["key"] == "Meta+c"


def test_a_click_that_did_not_move_is_still_a_click():
    """Clicking INTO a paragraph can leave a stale selection in the page, and a
    click is decided by the pointer, not by what happens to be highlighted."""
    out = recorder.coalesce([
        _k(1, "mouseDown", 0, ORDER_ID, nx=0.30, ny=0.42, button="left"),
        _k(2, "mouseUp", 40, ORDER_ID, nx=0.30, ny=0.42, button="left", clicks=1,
           selectedText="ORD-4417"),
    ])
    assert [a["kind"] for a in out] == ["click"]


def test_a_double_click_that_selects_a_word_stays_a_double_click():
    """The executor performs a dblclick for real, so calling it a reading would
    give up a step that genuinely replays. Only a would-be DRAG is reclassified."""
    out = recorder.coalesce([
        _k(1, "mouseDown", 0, ORDER_ID, nx=0.30, ny=0.42),
        _k(2, "mouseUp", 30, ORDER_ID, nx=0.30, ny=0.42, clicks=2, selectedText="ORD-4417"),
    ])
    assert [a["kind"] for a in out] == ["dblclick"]


def test_a_selection_is_recorded_but_never_claimed_to_be_executed():
    """Where `select_text` sits, and why.

    NOT in EXECUTOR_KINDS: `act()` has no select-text action, so certify would
    report a step as replayed that the executor never performed — the same lie
    that made a right-click ship as a left click. Not merely outside it either:
    that is where `drag` sits, and it means `failed` at fold time. It is an
    OBSERVATION — recorded, not executed, not a failure — which is the standing
    ENVIRONMENT_KINDS already has one layer up.
    """
    assert "select_text" not in recorder.EXECUTOR_KINDS
    assert "select_text" in recorder.OBSERVATION_KINDS
    assert not (recorder.OBSERVATION_KINDS & recorder.EXECUTOR_KINDS)


def test_a_selection_does_not_fail_certification(db_session, attempt):
    """The end of the bug, at both gates a step meets.

    `materialize` marks anything outside the executor's vocabulary `failed` at
    fold time, and `replay` — which is what certify and finalize both run —
    aborts the sequence on the first action the executor rejects. Either one
    alone makes a single selection cost the whole trajectory, so both are pinned
    here, together with the click AFTER the selection still replaying: a skipped
    step must not derail what follows it.
    """
    from app import materialize, replay

    base = int(time.time() * 1000) - 60_000
    for i, ev in enumerate(_selection(), start=1):
        recorder.record_event(db_session, attempt_id=attempt.id, kind=ev["kind"],
                              payload={**ev["payload"], "t": base + ev["t"]},
                              target=ev["target"], client_event_id=f"sel{i}")
    db_session.commit()
    made = materialize.materialize(db_session, attempt)
    db_session.commit()
    assert [s.action_type for s in made] == ["select_text"]
    assert made[0].replay_state == "unverified" and made[0].replay_error == ""
    assert made[0].arguments["text"] == "ORD-4417", "the reading has to survive into the step"

    performed: list[str] = []

    class _OnlyWhatTheExecutorSpeaks:
        """The live executor's contract: an unknown kind comes back not-ok."""

        def act(self, kind, locator, args):
            if kind not in recorder.EXECUTOR_KINDS:
                return {"ok": False, "error": f"unsupported action {kind}"}
            performed.append(kind)
            return {"ok": True}

        def world(self):
            return {"n": len(performed)}

    result = replay.replay(
        [{"kind": "select_text", "locator": {"testId": "order-id"}, "args": {"text": "ORD-4417"}},
         {"kind": "click", "locator": {"testId": "btn-cart"}, "args": {}}],
        _OnlyWhatTheExecutorSpeaks(), strict=False,
    )
    assert result.ok and result.rejected_at is None
    assert performed == ["click"], "nothing was executed for the reading"
    # Positional: certify maps outcome i onto step i, so a skipped step still
    # occupies its place — and it must not read as "replayed".
    assert [o["index"] for o in result.steps] == [0, 1]
    assert result.steps[0]["skipped"] is True and result.steps[0]["compared"] is False
