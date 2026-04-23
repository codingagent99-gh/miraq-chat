"""
core/actions.py

Canonical action-type vocabulary and lightweight builder helpers for the
headless WooCommerce Store API checkout migration.

Every action object in the wire payload has exactly two keys:
  - ``type``    (string)  — one of the ActionType constants below
  - ``payload`` (object)  — type-specific dict, always a plain dict (never None)

Builder functions return dicts ready for JSON serialisation.  They raise
``ValueError`` when a required field is missing so callers get an immediate,
readable error rather than a silent bad payload on the wire.

All six action types are always emitted — there is no feature flag gating:
``ADD_TO_CART``, ``OPEN_CART_PANEL``, ``UPDATE_CART_ITEM``, ``REMOVE_CART_ITEM``,
``OPEN_CHECKOUT_PANEL``, ``PROPOSE_CHECKOUT_ADDRESS``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ──────────────────────────────────────────────────────────────────────────────
# Action-type constants
# ──────────────────────────────────────────────────────────────────────────────

class ActionType:
    """String constants for every action type in the headless checkout contract."""

    # Cart actions.
    ADD_TO_CART      = "ADD_TO_CART"
    OPEN_CART_PANEL  = "OPEN_CART_PANEL"

    # Checkout actions.
    UPDATE_CART_ITEM          = "UPDATE_CART_ITEM"
    REMOVE_CART_ITEM          = "REMOVE_CART_ITEM"
    OPEN_CHECKOUT_PANEL       = "OPEN_CHECKOUT_PANEL"
    PROPOSE_CHECKOUT_ADDRESS  = "PROPOSE_CHECKOUT_ADDRESS"


# ──────────────────────────────────────────────────────────────────────────────
# Builder helpers
# ──────────────────────────────────────────────────────────────────────────────

def build_add_to_cart(
    product_id: int,
    quantity: int,
    variation_id: Optional[int] = None,
    variation: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Build an ``ADD_TO_CART`` action.

    Required: ``product_id``, ``quantity``.
    Optional: ``variation_id``, ``variation`` (list of ``{attribute, value}`` dicts).
    """
    if product_id is None:
        raise ValueError("build_add_to_cart: product_id is required")
    if quantity is None:
        raise ValueError("build_add_to_cart: quantity is required")

    payload: Dict[str, Any] = {
        "product_id": int(product_id),
        "quantity":   int(quantity),
    }
    if variation_id is not None:
        payload["variation_id"] = int(variation_id)
    if variation:
        payload["variation"] = variation

    return {"type": ActionType.ADD_TO_CART, "payload": payload}


def build_open_cart_panel() -> Dict[str, Any]:
    """Build an ``OPEN_CART_PANEL`` action (no payload fields required)."""
    return {"type": ActionType.OPEN_CART_PANEL, "payload": {}}


def build_update_cart_item(
    quantity: int,
    key: Optional[str] = None,
    product_id: Optional[int] = None,
    variation_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Build an ``UPDATE_CART_ITEM`` action.

    Required: ``quantity``.
    At least one of ``key`` or ``product_id`` should be provided so the
    frontend can locate the item, but this is not enforced here (the frontend
    decides the resolution order).
    """
    if quantity is None:
        raise ValueError("build_update_cart_item: quantity is required")

    payload: Dict[str, Any] = {"quantity": int(quantity)}
    if key is not None:
        payload["key"] = key
    if product_id is not None:
        payload["product_id"] = int(product_id)
    if variation_id is not None:
        payload["variation_id"] = int(variation_id)

    return {"type": ActionType.UPDATE_CART_ITEM, "payload": payload}


def build_remove_cart_item(
    key: Optional[str] = None,
    product_id: Optional[int] = None,
    variation_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Build a ``REMOVE_CART_ITEM`` action.

    At least one of ``key`` or ``product_id`` should be provided so the
    frontend can locate the item, but this is not enforced here.
    """
    payload: Dict[str, Any] = {}
    if key is not None:
        payload["key"] = key
    if product_id is not None:
        payload["product_id"] = int(product_id)
    if variation_id is not None:
        payload["variation_id"] = int(variation_id)

    return {"type": ActionType.REMOVE_CART_ITEM, "payload": payload}


def build_open_checkout_panel() -> Dict[str, Any]:
    """Build an ``OPEN_CHECKOUT_PANEL`` action (no payload fields required)."""
    return {"type": ActionType.OPEN_CHECKOUT_PANEL, "payload": {}}


def build_propose_checkout_address(
    parsed: Dict[str, Any],
    existing_on_file: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build a ``PROPOSE_CHECKOUT_ADDRESS`` action.

    Required: ``parsed`` — the parsed address dict.
    Optional: ``existing_on_file`` — the address currently stored for this customer.
    """
    if not parsed:
        raise ValueError("build_propose_checkout_address: parsed address is required")

    payload: Dict[str, Any] = {"parsed": parsed}
    if existing_on_file is not None:
        payload["existing_on_file"] = existing_on_file

    return {"type": ActionType.PROPOSE_CHECKOUT_ADDRESS, "payload": payload}


