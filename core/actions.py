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

Gating logic
------------
``_filter_actions_by_flag(actions, enabled)`` removes actions that are only
valid when the headless checkout feature flag is ON.  When the flag is OFF the
checkout-only action types (``OPEN_CHECKOUT_PANEL``, ``PROPOSE_CHECKOUT_ADDRESS``,
``UPDATE_CART_ITEM``, ``REMOVE_CART_ITEM``) are stripped before serialisation.
Cart actions (``ADD_TO_CART``, ``OPEN_CART_PANEL``) always pass through because
they mirror existing backend behaviour that the frontend already handles.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ──────────────────────────────────────────────────────────────────────────────
# Action-type constants
# ──────────────────────────────────────────────────────────────────────────────

class ActionType:
    """String constants for every action type in the headless checkout contract."""

    # Cart actions — always emitted regardless of HEADLESS_CHECKOUT_ENABLED.
    ADD_TO_CART      = "ADD_TO_CART"
    OPEN_CART_PANEL  = "OPEN_CART_PANEL"

    # Checkout-only actions — gated behind HEADLESS_CHECKOUT_ENABLED.
    UPDATE_CART_ITEM          = "UPDATE_CART_ITEM"
    REMOVE_CART_ITEM          = "REMOVE_CART_ITEM"
    OPEN_CHECKOUT_PANEL       = "OPEN_CHECKOUT_PANEL"
    PROPOSE_CHECKOUT_ADDRESS  = "PROPOSE_CHECKOUT_ADDRESS"


# Set of action types that are gated behind HEADLESS_CHECKOUT_ENABLED.
# Only these are stripped when the flag is OFF; all others pass through.
_CHECKOUT_GATED_ACTIONS: frozenset = frozenset({
    ActionType.UPDATE_CART_ITEM,
    ActionType.REMOVE_CART_ITEM,
    ActionType.OPEN_CHECKOUT_PANEL,
    ActionType.PROPOSE_CHECKOUT_ADDRESS,
})


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


# ──────────────────────────────────────────────────────────────────────────────
# Flag filter
# ──────────────────────────────────────────────────────────────────────────────

def _filter_actions_by_flag(
    actions: List[Dict[str, Any]],
    enabled: bool,
) -> List[Dict[str, Any]]:
    """
    Filter ``actions`` based on the ``HEADLESS_CHECKOUT_ENABLED`` flag value.

    When ``enabled`` is ``False``, any action whose ``type`` is in
    ``_CHECKOUT_GATED_ACTIONS`` is removed.  Cart-level actions
    (``ADD_TO_CART``, ``OPEN_CART_PANEL``) are always kept.

    When ``enabled`` is ``True``, all actions are kept unchanged.

    Parameters
    ----------
    actions:
        List of action dicts (each with ``type`` and ``payload`` keys).
    enabled:
        Value of the ``HEADLESS_CHECKOUT_ENABLED`` feature flag.

    Returns
    -------
    List[Dict[str, Any]]
        Filtered list (never ``None``; empty list when all are gated out).
    """
    if enabled:
        return list(actions)
    return [a for a in actions if a.get("type") not in _CHECKOUT_GATED_ACTIONS]
