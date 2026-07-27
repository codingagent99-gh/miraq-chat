"""
cart_handler.py

All cart mutations are owned by the browser session (WooCommerce Store API
requires nonce + cookie auth that only the frontend holds).

This handler never calls woo_cart.py. Instead it returns an ``actions[]``
array that the frontend's useChat.ts dispatches via onActions to useCart.ts,
which hits /wc/store/v1 with the correct credentials.
"""

import time
import logging
from flask import jsonify
from models import Intent
from conversation_flow import FlowState
from handlers.chat_utils import default_pagination
from core.actions import build_open_cart_panel
from ecommerce.cart_actions import build_cart_add_action

logger = logging.getLogger("miraq_chat")


def handle_cart_intent(intent, entities, user_context, conversation, page, start_time):
    """
    Translate a cart-related intent into a frontend action signal.

    Returns a Flask Response on success, or None to fall through to the
    search/classification pipeline (only for ADD_TO_CART with no product_id).
    """
    elapsed_ms = lambda: round((time.time() - start_time) * 1000)
    session_id = str(conversation.id)

    # ── Shared response builder ───────────────────────────────────────────────
    def _resp(bot_message: str, suggestions: list, metadata: dict = None, actions: list = None):
        return jsonify({
            "success":     True,
            "bot_message": bot_message,
            "intent":      intent.value,
            "products":    [],
            "suggestions": suggestions,
            "session_id":  session_id,
            "metadata":    {
                "response_time_ms": elapsed_ms(),
                **(metadata or {}),
            },
            "pagination":  default_pagination(page),
            "flow_state":  FlowState.IDLE.value,
            "actions":     actions if actions is not None else [],
        })

    # ── VIEW_CART ─────────────────────────────────────────────────────────────
    if intent == Intent.VIEW_CART:
        return _resp(
            bot_message = "Here's your cart! 🛒",
            suggestions = ["Checkout", "Browse products", "Clear cart"],
            actions     = [build_open_cart_panel()],
        )

    # ── ADD_TO_CART ───────────────────────────────────────────────────────────
    if intent == Intent.ADD_TO_CART:
        product_id   = entities.product_id
        variation_id = entities.variation_id
        qty          = entities.quantity or 1
        name         = entities.product_name or "item"

        if not product_id:
            # No product resolved yet — fall through to search pipeline so the
            # classifier can find the product first, then re-enter cart flow.
            logger.debug(
                "handle_cart_intent: ADD_TO_CART with no product_id — "
                "falling through to search pipeline"
            )
            return None

        # The action shape is backend-specific: Shopify carts are keyed by
        # VARIANT, so a bare product id cannot be added. build_cart_add_action
        # resolves the variant (explicit → attribute match → single-variant
        # product) and returns None when the choice is genuinely ambiguous.
        from store_registry import get_store_loader
        action, err = build_cart_add_action(
            product_id=product_id,
            quantity=qty,
            name=name,
            variation_id=variation_id,
            resolved_attrs=user_context.get("resolved_attributes") or {},
            store_loader=get_store_loader(),
            # Woo path here has never attached a variation payload (and
            # building one would add an API call) — keep that exactly.
            build_variation_payload=False,
        )

        if action is None:
            # Ambiguous variant — fall through to the search/variant pipeline,
            # which prompts the shopper to choose. Returning a broken action
            # here would fail silently in the browser instead.
            logger.info(
                "handle_cart_intent: ADD_TO_CART could not resolve a variant "
                f"for product_id={product_id!r} (reason={err}) — "
                "falling through to variant selection"
            )
            return None

        return _resp(
            bot_message = f"Adding **{name}** to your cart... 🛒",
            suggestions = ["Browse products", "Go to cart", "Checkout"],
            metadata    = {
                "product_id":   product_id,
                "variation_id": variation_id,  # None is fine — frontend guards it
                "quantity":     qty,
            },
            actions     = [action],
        )

    # ── REMOVE_FROM_CART ──────────────────────────────────────────────────────
    if intent == Intent.REMOVE_FROM_CART:
        item_key = user_context.get("pending_cart_item_key")

        if not item_key:
            # No key yet — ask frontend to open cart so user can tap the item.
            # VIEW_CART signal is the natural prompt to surface the key.
            logger.debug(
                "handle_cart_intent: REMOVE_FROM_CART with no pending_cart_item_key"
            )
            return _resp(
                bot_message = "Which item would you like to remove? Tap it in your cart.",
                suggestions = ["View cart"],
                actions     = [build_open_cart_panel()],
            )

        return _resp(
            bot_message = "Removing that item from your cart...",
            suggestions = ["View cart", "Browse products"],
            metadata    = {"item_key": item_key},
        )

    # ── UPDATE_CART_QTY ───────────────────────────────────────────────────────
    if intent == Intent.UPDATE_CART_QTY:
        item_key = user_context.get("pending_cart_item_key")
        qty      = entities.quantity

        if not item_key or not qty:
            logger.debug(
                "handle_cart_intent: UPDATE_CART_QTY missing item_key=%s qty=%s",
                item_key, qty,
            )
            return _resp(
                bot_message = "Which item's quantity would you like to update? Tap it in your cart.",
                suggestions = ["View cart"],
                actions     = [build_open_cart_panel()],
            )

        return _resp(
            bot_message = f"Updating quantity to {qty}...",
            suggestions = ["View cart", "Browse products", "Checkout"],
            metadata    = {
                "item_key": item_key,
                "quantity": qty,
            },
        )

    # ── CHECKOUT ──────────────────────────────────────────────────────────────
    if intent == Intent.CHECKOUT:
        return _resp(
            bot_message = "Taking you to checkout! 🧾",
            suggestions = ["Browse products"],
            actions     = [build_open_cart_panel()],
        )

    # Unknown cart intent — should never reach here given CART_INTENTS guard in
    # chat.py, but log and return None rather than swallowing silently.
    logger.warning(
        "handle_cart_intent: unhandled cart intent=%s — returning None", intent
    )
    return None