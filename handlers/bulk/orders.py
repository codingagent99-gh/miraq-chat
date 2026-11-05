"""Cart step for the bulk-order flow.

Every bulk order is the shopper's own, so once the lines are complete they
are added to the shopper's cart. Nothing here creates an order.
"""

import time

from flask import jsonify
from sqlalchemy.orm.attributes import flag_modified

from conversation_flow import FlowState
from chat_logger import get_logger
from handlers.chat_utils import default_pagination
from handlers.bulk.common import _BULK_STATE_KEYS

logger = get_logger("miraq_chat")

def _line_product_is_live(line, store_loader):
    """True when a parked line's product_id still exists in the current catalog.

    Bulk lines are parked in conversation.context_data while the shopper is
    asked for variants and quantities, and that state outlives the catalog:
    StoreLoader reloads every 6 hours (_refresh_interval) while the
    conversation sits open. Nothing invalidated the parked product_id, so a
    line resolved before a reload was trusted verbatim after it.

    Before the ids became GID-derived, that was actively dangerous: ids were
    positional (idx + 1), so deleting one product shifted every later id down
    by one, and a parked line silently resolved to a DIFFERENT product. The
    add succeeded, the cart filled with the wrong items, and the confirmation
    named the products the shopper had asked for — because the label comes
    from the parked line, not from what was added. No error anywhere.

    Ids are permanent now, so the remaining case is the honest one: the
    product was deleted or unpublished mid-conversation and the id resolves
    to nothing. This also catches any state parked under the old positional
    scheme, since a small ordinal will not match a 13-digit Shopify id.

    Returns True when the loader is unavailable or the product is found —
    "unknown" must never block an add, only a positive miss does.
    """
    if not store_loader:
        return True

    products = getattr(store_loader, "products", None)
    if not products:
        return True

    pid = str(line.get("product_id"))
    for candidate in products:
        if (str(candidate.get("id", "")) == pid
                or str(candidate.get("_shopify_gid", "")) == pid):
            return True
    return False

def _build_bulk_cart_response(lines_as_dicts, conversation, user_context, page, start_time):
    """
    The shopper's multi-line order: add every line to their cart.

    The widget adds the items with the shopper's own cookie and nonce, and
    WooCommerce's own checkout fills billing and shipping from their account,
    so nothing here needs an address or creates an order.

    It also keeps the backend out of the identity business here. Nothing in
    this path sends a customer_id, so a session carrying an id WooCommerce
    does not recognise can still order.

    Same action vocabulary as the single-item ADD_TO_CART flow in
    cart_handler: one ADD_TO_CART per line, OPEN_CART_PANEL last.
    """
    from store_registry import get_store_loader
    from ecommerce.cart_actions import build_cart_add_action
    from core.actions import build_open_cart_panel

    store_loader = get_store_loader()

    actions = []
    added_labels = []
    unresolved_count = 0
    failed_labels = []
    stale_labels = []

    for line in lines_as_dicts:
        if line.get("unresolved") or not line.get("product_id"):
            unresolved_count += 1
            continue

        name = line.get("product_name") or "item"
        qty = int(line.get("quantity") or 1)

        # The catalog can reload between the line being parked and the shopper
        # finishing the flow — see _line_product_is_live. Checked before
        # building the action, because build_cart_add_action would happily
        # emit an add for whatever the id now points at.
        if not _line_product_is_live(line, store_loader):
            logger.warning(
                f"bulk_order | parked line product_id={line['product_id']!r} "
                f"({name}) is no longer in the catalog — catalog reloaded "
                f"mid-order, or state predates the GID-derived ids. Line "
                f"dropped rather than added blind."
            )
            stale_labels.append(name)
            continue

        # build_variation_payload=True on purpose, matching the confirm-add
        # flow rather than the bare ADD_TO_CART intent: variant_meta holds the
        # axes the variation itself cannot encode (WooCommerce "Any"
        # attributes), and those are exactly what the shopper was just asked
        # for. Dropping them would put an item in the cart missing the finish
        # and size they chose. Costs one variation fetch per line.
        action, err = build_cart_add_action(
            product_id=line["product_id"],
            quantity=qty,
            name=name,
            variation_id=line.get("variation_id") or None,
            resolved_attrs=line.get("variant_meta") or {},
            store_loader=store_loader,
            build_variation_payload=True,
            # This function ALWAYS builds its own itemised bot_message below
            # (added_labels), so each ADD_TO_CART/SHOPIFY_ADD_TO_CART action
            # must not also trigger the widget's per-item confirmation —
            # that duplicated the summary with one "✅ Added X" line per
            # product, plus a redundant /chat/cart-result round trip and a
            # re-dispatched OPEN_CART_PANEL for each. See build_add_to_cart().
            suppress_result=True,
        )

        if action is None:
            # Emitting a broken action would fail silently in the browser and
            # the shopper would see a cart missing an item with no explanation.
            logger.warning(
                f"bulk_order | cart add could not resolve a variant for "
                f"product_id={line['product_id']!r} ({name}) reason={err} — "
                f"line dropped from the cart batch"
            )
            failed_labels.append(name)
            continue

        actions.append(action)
        added_labels.append(f"{qty} × **{name}**")

    if actions:
        actions.append(build_open_cart_panel())
        bot_message = f"Added {len(added_labels)} item(s) to your cart 🛒\r\n\r\n"
        bot_message += "\r\n".join(f"- {label}" for label in added_labels)
        suggestions = ["View cart", "Checkout", "Browse products"]
    else:
        # Nothing could be added. Say why (below) and end the flow rather
        # than leaving the shopper inside it.
        logger.warning("bulk_order | self-order produced no addable cart lines")
        bot_message = "I couldn't add anything to your cart."
        suggestions = ["Browse products", "Start a bulk order"]
    if failed_labels:
        bot_message += (
            f"\r\n\r\nI couldn't add {', '.join(failed_labels)} — "
            "the options weren't specific enough. Search for it and pick the "
            "variant you want."
        )
    if stale_labels:
        # Deliberately a separate sentence from failed_labels: "be more
        # specific" is useless advice when the product is simply gone.
        bot_message += (
            f"\r\n\r\n{', '.join(stale_labels)} is no longer available in the "
            "catalogue, so I left it out. Search for it and I'll show you "
            "what's in stock."
        )
    if unresolved_count:
        bot_message += f"\r\n\r\n{unresolved_count} line(s) I couldn't match to a product were skipped."

    logger.info(
        f"bulk_order | customer self-order → cart | added={len(added_labels)} | "
        f"failed={len(failed_labels)} | stale={len(stale_labels)} | "
        f"unresolved={unresolved_count}"
    )

    for k in _BULK_STATE_KEYS:
        user_context.pop(k, None)
    for k in ("bulk_variant_line_indices", "bulk_variant_current_pos", "bulk_variant_cache"):
        user_context.pop(k, None)

    conversation.flow_state = FlowState.IDLE.value
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "guided_flow",
        "products": [],
        "suggestions": suggestions,
        "actions": actions,
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.IDLE.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.IDLE.value,
        "pagination": default_pagination(page),
    }), 200

def _build_bulk_confirmation_response(lines_as_dicts, conversation, user_context, page, start_time):
    """
    Shared exit for every route through the flow (trigger parse, product
    reply, quantity reply, variant reply): add the lines to the cart, or
    re-prompt when nothing in the message matched a product.

    The name is kept because those routes all call it.
    """
    resolved_count = sum(1 for l in lines_as_dicts if not l.get("unresolved"))

    # Nothing resolvable at all — re-prompt instead of an empty cart reply
    # that echoes the user's own raw text back as a fake "product".
    if resolved_count == 0:
        elapsed = round((time.time() - start_time) * 1000)
        conversation.flow_state = FlowState.AWAITING_BULK_ORDER_INPUT.value
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return jsonify({
            "success": True,
            "bot_message": (
                "I couldn't find any products in that message. "
                "Try the format:\r\n\r\n"
                "**Example:**\r\n"
                "*Order 20 Harmony White, 15 Coral Grey*"
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_ORDER_INPUT.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_ORDER_INPUT.value,
            "pagination": default_pagination(page),
        }), 200

    return _build_bulk_cart_response(
        lines_as_dicts, conversation, user_context, page, start_time
    )
