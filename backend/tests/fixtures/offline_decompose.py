"""Shared offline decompose_fn for unit tests (no LLM)."""

from __future__ import annotations

from typing import Any


def return_task_decompose(brief: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Correctness sub-goals for the B2-style return fixture used by offline tests."""
    return [
        {
            "id": "return_filed",
            "subgoal": "return initiated against the referenced order",
            "assertion": "a return record exists in durable state",
            "predicate": {"kind": "state_len_gte", "path": "returns", "value": 1},
        },
        {
            "id": "return_targets_item",
            "subgoal": "return covers the specified item only",
            "assertion": "return item_ids include ln_mouse",
            "predicate": {
                "kind": "collection_any_contains",
                "path": "returns",
                "field": "item_ids",
                "value": "ln_mouse",
            },
        },
        {
            "id": "return_reason_defective",
            "subgoal": "return reason reflects defective item",
            "assertion": "a return has reason defective",
            "predicate": {
                "kind": "collection_any",
                "path": "returns",
                "fields": {"reason": "defective"},
            },
        },
        {
            "id": "refund_original_payment",
            "subgoal": "refund targets original payment method",
            "assertion": "a return uses refund_method original_payment",
            "predicate": {
                "kind": "collection_any",
                "path": "returns",
                "fields": {"refund_method": "original_payment"},
            },
        },
    ]


def vacuous_orders_decompose(brief: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": "vacuous_order_exists",
            "subgoal": "order already present in seed",
            "assertion": "orders nonempty",
            "predicate": {"kind": "state_len_gte", "path": "orders", "value": 1},
        }
    ]


def overfit_return_id_decompose(brief: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": "specific_return_id",
            "subgoal": "exact golden return id present",
            "assertion": "RET_1 exists",
            "predicate": {"kind": "state_nonempty", "path": "returns.RET_1"},
        }
    ]


def weak_orders_decompose(brief: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": "weak_orders",
            "subgoal": "orders exist",
            "assertion": "orders nonempty",
            "predicate": {"kind": "state_len_gte", "path": "orders", "value": 1},
        }
    ]
