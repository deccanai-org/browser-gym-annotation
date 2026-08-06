"""The semantic world diff — what a step actually changed.

These lock the properties the trajectory depends on: that the delta agrees with
the world hash, that an entity reads as one change rather than a wall of
scalars, that ids (not positions) identify things, and that a secret never
reaches the delta.
"""

from app import checkpoints
from app.worlddiff import MAX_CHANGES, diff_worlds, is_empty, summarize


def _world(**apps):
    base = {"task_id": "M1", "seed": 0, "step": 1, "finished": False,
            "shop": {"cart": {"items": [], "applied_promo": None}, "orders": {}},
            "mail": {"inbox": {}, "sent": {}, "unread_count": 0},
            "events": []}
    for k, v in apps.items():
        base[k] = {**base.get(k, {}), **v} if isinstance(v, dict) else v
    return base


def test_an_unchanged_world_is_not_a_change():
    w = _world()
    d = diff_worlds(w, dict(w))
    assert d["changed"] is False and d["changes"] == [] and is_empty(d)


def test_a_volatile_key_is_not_a_state_change():
    """flash_messages and action_log churn on every read at every nesting level.
    If they counted, every single step would report a change."""
    a = _world()
    b = _world()
    b["shop"]["action_log"] = ["clicked", "clicked again"]
    b["shop"]["flash_messages"] = ["Added to cart"]
    assert diff_worlds(a, b)["changed"] is False


def test_an_integral_float_round_trip_is_not_a_change():
    """A world stored in a JSON column returns 0.0 where the gym said 0. Without
    the shared normalizer this reports a change on every step."""
    a = _world()
    a["shop"]["orders"] = {"O1": {"id": "O1", "total": 10}}
    b = _world()
    b["shop"]["orders"] = {"O1": {"id": "O1", "total": 10.0}}
    assert diff_worlds(a, b)["changed"] is False


def test_changed_always_agrees_with_the_world_hash():
    """The invariant that stops the delta and the divergence gate disagreeing."""
    pairs = [
        (_world(), _world()),
        (_world(), _world(shop={"cart": {"items": [{"product_id": "p1", "qty": 1}], "applied_promo": None}, "orders": {}})),
        (_world(), _world(mail={"inbox": {}, "sent": {}, "unread_count": 3})),
    ]
    for a, b in pairs:
        expected = checkpoints.hash_world(a) != checkpoints.hash_world(b)
        assert diff_worlds(a, b)["changed"] is expected


def test_a_new_order_is_one_add_not_a_wall_of_scalars():
    a = _world()
    b = _world()
    b["shop"]["orders"] = {"ORD_7": {
        "id": "ORD_7", "status": "placed", "total": 49.99, "address": "1 Main St",
        "payment": "visa", "items": [{"sku": "x"}], "eta": "2026-05-24",
        "gift": False, "note": "", "coupon": None,
    }}
    d = diff_worlds(a, b)
    adds = [c for c in d["changes"] if c["op"] == "add" and c["path"] == "shop.orders"]
    assert len(adds) == 1, d["changes"]
    c = adds[0]
    assert c["id"] == "ORD_7" and c["app"] == "shop"
    assert c["to"]["status"] == "placed" and c["to"]["total"] == 49.99
    assert c["size"] == [0, 1]


def test_a_removed_entity_is_one_remove():
    a = _world()
    a["shop"]["subscriptions"] = {"S1": {"id": "S1", "status": "active"}}
    b = _world()
    b["shop"]["subscriptions"] = {}
    d = diff_worlds(a, b)
    rems = [c for c in d["changes"] if c["op"] == "remove"]
    assert len(rems) == 1 and rems[0]["id"] == "S1"


def test_a_field_change_inside_an_entity_carries_the_entity_id():
    a = _world()
    a["shop"]["orders"] = {"ORD_7": {"id": "ORD_7", "status": "placed"}}
    b = _world()
    b["shop"]["orders"] = {"ORD_7": {"id": "ORD_7", "status": "cancelled"}}
    d = diff_worlds(a, b)
    c = next(x for x in d["changes"] if x["op"] == "set")
    assert c["id"] == "ORD_7"
    assert c["path"] == "shop.orders.ORD_7.status"
    assert c["from"] == "placed" and c["to"] == "cancelled"


def test_cart_items_diff_by_product_id_not_by_position():
    """Removing the first of three items must be ONE remove, not 'everything
    after position 0 changed'."""
    items = [{"product_id": "p1", "qty": 1}, {"product_id": "p2", "qty": 1}, {"product_id": "p3", "qty": 1}]
    a = _world(shop={"cart": {"items": items, "applied_promo": None}, "orders": {}})
    b = _world(shop={"cart": {"items": items[1:], "applied_promo": None}, "orders": {}})
    d = diff_worlds(a, b)
    assert [c["op"] for c in d["changes"]] == ["remove"]
    assert d["changes"][0]["id"] == "p1"


def test_reordering_the_cart_is_not_a_change():
    items = [{"product_id": "p1", "qty": 1}, {"product_id": "p2", "qty": 2}]
    a = _world(shop={"cart": {"items": items, "applied_promo": None}, "orders": {}})
    b = _world(shop={"cart": {"items": list(reversed(items)), "applied_promo": None}, "orders": {}})
    assert diff_worlds(a, b)["changes"] == []


def test_a_quantity_change_is_a_set_on_that_item():
    a = _world(shop={"cart": {"items": [{"product_id": "p1", "qty": 1}], "applied_promo": None}, "orders": {}})
    b = _world(shop={"cart": {"items": [{"product_id": "p1", "qty": 3}], "applied_promo": None}, "orders": {}})
    d = diff_worlds(a, b)
    c = next(x for x in d["changes"] if x["op"] == "set")
    assert c["id"] == "p1" and c["from"] == 1 and c["to"] == 3


def test_an_appended_event_log_is_an_add_not_a_whole_list_set():
    a = _world()
    a["events"] = [{"kind": "cart_add"}]
    b = _world()
    b["events"] = [{"kind": "cart_add"}, {"kind": "order_placed"}]
    d = diff_worlds(a, b)
    assert [c["op"] for c in d["changes"]] == ["add"]
    assert d["changes"][0]["to"]["kind"] == "order_placed"


def test_a_cross_app_effect_names_both_apps():
    """Placing a shop order also lands a confirmation mail — the multi-app signal."""
    a = _world()
    b = _world()
    b["shop"]["orders"] = {"ORD_7": {"id": "ORD_7", "status": "placed"}}
    b["mail"]["inbox"] = {"m88": {"id": "m88", "subject": "Order confirmation"}}
    b["mail"]["unread_count"] = 1
    d = diff_worlds(a, b)
    assert d["apps"] == ["mail", "shop"]


def test_caps_truncate_the_list_but_keep_the_counts_exact():
    a = _world()
    b = _world()
    b["shop"]["orders"] = {f"O{i}": {"id": f"O{i}", "status": "placed"} for i in range(MAX_CHANGES + 50)}
    d = diff_worlds(a, b)
    assert d["truncated"] is True
    assert len(d["changes"]) <= MAX_CHANGES
    assert d["counts"]["add"] == MAX_CHANGES + 50   # exact despite truncation


def test_change_order_is_deterministic():
    """Same transition, different dict insertion order → identical JSON."""
    import json

    a1 = _world(); a2 = _world()
    b1 = _world(); b2 = _world()
    b1["shop"]["orders"] = {"A": {"id": "A"}, "B": {"id": "B"}}
    b2["shop"]["orders"] = {"B": {"id": "B"}, "A": {"id": "A"}}
    d1, d2 = diff_worlds(a1, b1), diff_worlds(a2, b2)
    assert json.dumps(d1["changes"], sort_keys=True) == json.dumps(d2["changes"], sort_keys=True)


def test_a_sensitive_value_never_reaches_the_delta():
    """The recorder redacts secrets at capture time; the delta must not be a
    second route to the same data."""
    a = _world()
    a["shop"]["payments"] = {"P1": {"id": "P1", "card_number": "4111111111111111"}}
    b = _world()
    b["shop"]["payments"] = {"P1": {"id": "P1", "card_number": "4222222222222222"}}
    blob = str(diff_worlds(a, b))
    assert "4222222222222222" not in blob and "4111111111111111" not in blob


def test_summary_reads_as_a_sentence():
    a = _world()
    b = _world()
    b["shop"]["orders"] = {"ORD_7": {"id": "ORD_7", "status": "placed"}}
    b["mail"]["unread_count"] = 1
    s = summarize(diff_worlds(a, b))
    assert "ORD_7" in s and "unread_count" in s
