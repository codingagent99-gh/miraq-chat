"""
handlers/bulk_order_handler.py — Bulk order flow handler.

A signed-in shopper orders several products in one message. Every line is
the shopper's own and goes to their cart; WooCommerce checkout then takes
the billing and shipping details from their account.

Public functions:
    handle_bulk_order_trigger()   — entry point, AWAITING_BULK_ORDER_INPUT
    handle_bulk_order_input()     — parse utterance → missing product /
                                    quantity / variant prompts → cart
    handle_bulk_product_reply()   — AWAITING_BULK_PRODUCT
    handle_bulk_quantity_reply()  — AWAITING_BULK_QUANTITY
    handle_cancel_bulk_order()

Session management (db.session.add / db.session.commit) is the caller's
responsibility in chat.py. This module only mutates conversation attributes
and calls flag_modified.
"""

import time

from flask import jsonify
from sqlalchemy.orm.attributes import flag_modified

from conversation_flow import FlowState
from chat_logger import get_logger
from handlers.chat_utils import default_pagination
from parsers.bulk_order_parser import parse_bulk_order_utterance

# Variant helpers live in handlers/bulk/variants.py. chat.py imports
# handle_bulk_variant_selection_reply from here.
from handlers.bulk.variants import (
    _ask_for_bulk_variant,
    _ensure_missing_axes,
    _is_variable_product,
    handle_bulk_variant_selection_reply,
)

# Cart step lives in handlers/bulk/orders.py; variants.py imports
# _build_bulk_confirmation_response from here.
from handlers.bulk.common import _BULK_STATE_KEYS
from handlers.bulk.orders import _build_bulk_confirmation_response
import re
import difflib

__all__ = [
    "handle_bulk_order_trigger",
    "handle_bulk_order_input",
    "handle_bulk_product_reply",
    "handle_bulk_quantity_reply",
    "handle_bulk_variant_selection_reply",
    "handle_cancel_bulk_order",
    "_build_bulk_confirmation_response",
]
logger = get_logger("miraq_chat")




def handle_cancel_bulk_order(user_context, conversation, page, start_time):
    pending_lines = user_context.get("pending_bulk_lines", [])
    is_single_self_order = (
        len(pending_lines) == 1
        and pending_lines[0].get("is_self_order")
    )

    for k in _BULK_STATE_KEYS:
        user_context.pop(k, None)
    flag_modified(conversation, "context_data")
    conversation.flow_state = FlowState.IDLE.value
    elapsed = round((time.time() - start_time) * 1000)

    bot_message = (
        "Order cancelled. What else can I help you with?"
        if is_single_self_order
        else "Bulk order cancelled. What else can I help you with?"
    )

    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Browse Products", "Start a bulk order"],
        "session_id": str(conversation.id),
        "metadata": {"flow_state": FlowState.IDLE.value, "response_time_ms": elapsed},
        "flow_state": FlowState.IDLE.value,
        "pagination": default_pagination(page),
    }), 200











# ══════════════════════════════════════════════════════════════
# ── Function 1: handle_bulk_order_trigger ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_order_trigger(conversation, user_context, page, start_time):
    """
    Entry point for the bulk order flow.
    Sets flow state to AWAITING_BULK_ORDER_INPUT and prompts the shopper.
    Returns a Flask response.
    """
    conversation.flow_state = FlowState.AWAITING_BULK_ORDER_INPUT.value

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": (
            "Tell me everything you'd like to order, with as many products "
            "as you like, and I'll add them all to your cart.\r\n\r\n"
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


# ══════════════════════════════════════════════════════════════
# ── Function 2: handle_bulk_order_input ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_order_input(message, store_loader, conversation, user_context, page, start_time, pre_resolved=None):
    """
    Parses the free-text bulk order utterance, serializes lines into
    user_context, asks for anything missing (product, quantity, variant) and
    then adds every line to the shopper's cart. Returns a Flask response.
    """
    customer_id = conversation.customer_id

    # Step 2: Parse
    lines = parse_bulk_order_utterance(
        text=message,
        store_loader=store_loader,
        self_customer_id=str(customer_id) if customer_id else None,
    )
    # Step 3: Nothing parsed
    if not lines:
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                "I couldn't parse any order lines from that. Please try again."
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

    # Step 4: Serialize to dicts for JSONB storage
    lines_as_dicts = [
        {
            "raw_fragment": l.raw_fragment,
            "company_name": l.company_name,
            "recipient_name": getattr(l, "recipient_name", "") or "",
            "email": l.email,
            "product_name": l.product_name,
            "quantity": l.quantity,
            "product_id": l.product_id,
            "variation_id": l.variation_id,
            "customer_id": l.customer_id,
            "customer_display_name": l.customer_display_name,
            "is_self_order": l.is_self_order,
            "shipping_address": l.shipping_address,
            "billing_address": l.billing_address,
            "is_reorder": l.is_reorder,
            "reorder_source_order_id": l.reorder_source_order_id,
            "unresolved": l.unresolved,
            "unresolved_reason": l.unresolved_reason,
            "quantity_explicitly_set":  l.quantity_explicitly_set,
            "unmatched_variant_hint": getattr(l, "unmatched_variant_hint", "") or "",
            # Terms the shopper typed that no VARIATION enumerates — the axis is
            # "Any" on the variations and its real options live on the parent.
            # _ask_for_bulk_variant resolves these against the parent options
            # and pre-selects them, so the shopper isn't asked again for a size
            # they already gave. This dict is built field-by-field, so a field
            # missing here is silently dropped between parser and prompt
            # however correct both ends are.
            "unmatched_variant_terms": list(getattr(l, "unmatched_variant_terms", None) or []),
            # EVERY term the shopper typed for this line, matched or not.
            # unmatched_variant_terms above is not enough on its own: it holds
            # only terms NO variation enumerates, so a size that some OTHER
            # variation of the same product happens to list never appears there
            # — and if the variation actually chosen is "Any" on that axis, the
            # shopper gets asked for a size they already gave. Pembroke's
            # '12\"x12\"' is exactly that case.
            "variant_terms": list(getattr(l, "variant_terms", None) or []),
            "conflicting_variant_terms": list(getattr(l, "conflicting_variant_terms", None) or []),
            # getattr, not attribute access: these fields were added to
            # BulkOrderLine alongside this handler, so a half-deploy where
            # the parser is older would otherwise 500 on every bulk order
            # rather than simply skipping the newer behaviour.
            "blank_variant_axes": list(getattr(l, "blank_variant_axes", None) or []),
            "candidate_variation_ids": list(getattr(l, "candidate_variation_ids", None) or []),
            "specified_variant_axes": list(getattr(l, "specified_variant_axes", None) or []),
            "self_contained_variant": bool(getattr(l, "self_contained_variant", False)),
            # Seeded from the parser: the chip-card fallback pins Sample Size
            # itself for products with no dedicated chip-card variation.
            "variant_meta": dict(getattr(l, "variant_meta", None) or {}),
        }
        for l in lines
    ]
    
     # ── Patch: apply classifier-resolved product when parser misidentified digits in name ──
    if pre_resolved and pre_resolved.product_id:
        _pre_name = pre_resolved.product_name or ""
        for ld in lines_as_dicts:
            if ld.get("unresolved") and ld.get("unresolved_reason") in (
                "product_not_found", "both_not_found"
            ):
                # pre_resolved is ONE whole-message classifier resolution,
                # but a bulk request can have several unresolved lines — only
                # patch a line whose OWN text actually names this product.
                # Without this check, a line the parser genuinely couldn't
                # match to anything (e.g. an attribute-only description like
                # "Grey Marble") silently gets relabeled with an unrelated
                # product resolved from a DIFFERENT line, and the fabricated
                # line goes to the confirmation table looking legitimate.
                if not _pre_name or not re.search(
                    r'\b' + re.escape(_pre_name) + r'\b', ld["raw_fragment"], re.I
                ):
                    continue

                # Strip the known product name from raw_fragment,
                # then re-scan what's left for an explicit quantity
                stripped = re.sub(
                    re.escape(pre_resolved.product_name or ""),
                    "", ld["raw_fragment"], flags=re.I,
                ).strip()
                qty_match = re.search(r'\b(\d+)\b(?!\.\d)', stripped)

                ld["product_id"]   = pre_resolved.product_id
                ld["product_name"] = pre_resolved.product_name
                ld["variation_id"] = pre_resolved.variation_id

                if qty_match:
                    ld["quantity"] = int(qty_match.group(1))
                    ld["quantity_explicitly_set"] = True

                # Fix unresolved: still unresolved only if customer is also missing
                if ld.get("customer_id"):
                    ld["unresolved"]        = False
                    ld["unresolved_reason"] = None
                else:
                    ld["unresolved_reason"] = "email_not_provided"

                logger.debug(
                    f"bulk_handler | pre_resolved patch applied | "
                    f"product='{pre_resolved.product_name}' id={pre_resolved.product_id} "
                    f"qty={ld['quantity']}"
                )

    user_context["pending_bulk_lines"] = lines_as_dicts
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    
    
    # Step 4.5: Any line with a completely blank product name → ask before anything else
    product_blank_indices = [
        i for i, l in enumerate(lines_as_dicts)
        if not l.get("product_name", "").strip()
    ]
    if product_blank_indices:
        user_context["bulk_product_missing_indices"] = product_blank_indices
        user_context["bulk_product_current_pos"] = 0
        conversation.flow_state = FlowState.AWAITING_BULK_PRODUCT.value
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")

        already_resolved = [l for l in lines_as_dicts if not l.get("unresolved")]
        resolved_note = f"\r\n\r\n{len(already_resolved)} other line(s) already resolved." if already_resolved else ""

        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                f"What product and quantity would you like to order?{resolved_note}"
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_PRODUCT.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_PRODUCT.value,
            "pagination": default_pagination(page),
        }), 200

    # Step 4.7: Lines with no explicit quantity — ask before variant selection
    qty_unset = [
        i for i, l in enumerate(lines_as_dicts)
        if not l.get("quantity_explicitly_set") and not l.get("unresolved")
    ]
    if qty_unset:
        return _prompt_for_quantity(
            qty_unset, lines_as_dicts, conversation, user_context, page, start_time
        )
           
    # Step 5: Check for variable products with unresolved variation_id
    needs_variant_indices = [
        i for i, l in enumerate(lines_as_dicts)
        # "not variation_id" is not enough: a variation can match on colour and
        # still leave Finish / Sample Size as WooCommerce "Any", which the
        # storefront makes the shopper choose. blank_variant_axes carries those.
        if l["product_id"]
        and _is_variable_product(l["product_id"], store_loader)
        and (not l["variation_id"]
             or l.get("blank_variant_axes")
             or _ensure_missing_axes(l, user_context))
    ]

    if needs_variant_indices:
        user_context["bulk_variant_line_indices"] = needs_variant_indices
        user_context["bulk_variant_current_pos"] = 0
        user_context["bulk_variant_cache"] = {}
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, 0,
            conversation, user_context, page, start_time,
        )

    # Step 6: No variant selection needed — return structured confirmation
    return _build_bulk_confirmation_response(
        lines_as_dicts, conversation, user_context, page, start_time
    )
























# ══════════════════════════════════════════════════════════════
# ── Public: handle_bulk_product_reply ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_product_reply(message, store_loader, conversation, user_context, page, start_time):
    """
    Called during AWAITING_BULK_PRODUCT when the shopper provides a product
    name (and optional quantity) for a line that had a blank product.

    After filling the product, resumes the normal pipeline:
      blank product → quantity → variant selection → cart
    """
    lines_as_dicts = user_context.get("pending_bulk_lines", [])
    missing_indices = user_context.get("bulk_product_missing_indices", [])
    pos = user_context.get("bulk_product_current_pos", 0)

    # Guard
    if not missing_indices or pos >= len(missing_indices):
        return _continue_after_slots_filled(lines_as_dicts, store_loader, conversation, user_context, page, start_time)

    line_idx = missing_indices[pos]
    line = lines_as_dicts[line_idx]

    # ── Parse quantity from the reply ──
    qty_match = re.search(r'\b(\d+)\b', message)
    if qty_match:
        line["quantity"] = int(qty_match.group(1))
        # Strip quantity from the product portion
        product_text = (message[:qty_match.start()] + message[qty_match.end():]).strip().strip(".,- ")
    else:
        product_text = message.strip().strip(".,- ")

    # ── Resolve product against catalog ──
    resolved_id = None
    resolved_name = product_text

    if store_loader and product_text:
        products = store_loader.products or []

        # Exact match
        for p in products:
            if p.get("name", "").lower() == product_text.lower():
                resolved_id = p["id"]
                resolved_name = p["name"]
                break

        # First-word match
        if resolved_id is None:
            first_word = product_text.split()[0] if product_text.split() else ""
            for p in products:
                if p.get("name", "").lower() == first_word.lower():
                    resolved_id = p["id"]
                    resolved_name = p["name"]
                    break

        # Fuzzy fallback
        if resolved_id is None:
            product_names = [p.get("name", "") for p in products]
            close = difflib.get_close_matches(product_text, product_names, n=1, cutoff=0.6)
            if close:
                resolved_name = close[0]
                resolved_id = next(
                    (p["id"] for p in products if p.get("name") == resolved_name), None
                )

    line["product_name"] = resolved_name
    line["product_id"] = resolved_id

    # Recompute unresolved state
    customer_missing = line.get("customer_id") is None
    if resolved_id:
        line["unresolved"] = customer_missing
        if customer_missing:
            line["unresolved_reason"] = "email_not_provided" if not line.get("email") else "email_not_found"
        else:
            line["unresolved_reason"] = None
    else:
        line["unresolved"] = True
        line["unresolved_reason"] = "product_not_found"

    lines_as_dicts[line_idx] = line
    next_pos = pos + 1
    user_context["bulk_product_current_pos"] = next_pos
    user_context["pending_bulk_lines"] = lines_as_dicts
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    # More blank products in this batch?
    if next_pos < len(missing_indices):
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": "What product and quantity would you like to order?",
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_PRODUCT.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_PRODUCT.value,
            "pagination": default_pagination(page),
        }), 200

    # All blank products filled — continue pipeline
    return _continue_after_slots_filled(
        lines_as_dicts, store_loader, conversation, user_context, page, start_time
    )



















def _continue_after_slots_filled(lines_as_dicts, store_loader, conversation, user_context, page, start_time):
    """
    Shared exit point after all blank-product slots are filled: ask for any
    missing quantity, then any missing variant, then add everything to the
    cart.
    """
    user_context.pop("bulk_product_missing_indices", None)
    user_context.pop("bulk_product_current_pos", None)

    qty_unset = [
        i for i, l in enumerate(lines_as_dicts)
        if not l.get("quantity_explicitly_set") and not l.get("unresolved")
    ]
    if qty_unset:
        return _prompt_for_quantity(
            qty_unset, lines_as_dicts, conversation, user_context, page, start_time
        )
    return _continue_after_quantity_filled(
        lines_as_dicts, store_loader, conversation, user_context, page, start_time
    )


# ══════════════════════════════════════════════════════════════
# ── Private: _prompt_for_quantity ──
# ══════════════════════════════════════════════════════════════

def _prompt_for_quantity(qty_unset, lines_as_dicts, conversation, user_context, page, start_time):
    """
    Store the pending indices, set AWAITING_BULK_QUANTITY, and ask for
    the quantity of the first unset line.
    """
    user_context["bulk_quantity_pending_indices"] = qty_unset
    user_context["bulk_quantity_current_pos"]     = 0
    conversation.flow_state  = FlowState.AWAITING_BULK_QUANTITY.value
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    line = lines_as_dicts[qty_unset[0]]
    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success":     True,
        "bot_message": _build_quantity_prompt(line),
        "intent":      "guided_flow",
        "products":    [],
        "suggestions": ["Cancel"],
        "session_id":  str(conversation.id),
        "metadata": {
            "flow_state":       FlowState.AWAITING_BULK_QUANTITY.value,
            "response_time_ms": elapsed,
        },
        "flow_state":  FlowState.AWAITING_BULK_QUANTITY.value,
        "pagination":  default_pagination(page),
    }), 200

def _build_quantity_prompt(line: dict) -> str:
    product = line['product_name']
    customer = line['customer_display_name']
    if line.get('is_self_order'):
        return f"How many **{product}** shall I order?"
    return f"How many **{product}** for **{customer}**?"

# ══════════════════════════════════════════════════════════════
# ── Public: handle_bulk_quantity_reply ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_quantity_reply(message, store_loader, conversation, user_context, page, start_time):
    """
    Called during AWAITING_BULK_QUANTITY when the shopper specifies a quantity.
    Stamps the quantity, advances to the next unset line or continues pipeline.
    """
    import re as _re

    lines_as_dicts   = user_context.get("pending_bulk_lines", [])
    pending_indices  = user_context.get("bulk_quantity_pending_indices", [])
    pos              = user_context.get("bulk_quantity_current_pos", 0)

    if not pending_indices or pos >= len(pending_indices):
        return _continue_after_quantity_filled(
            lines_as_dicts, store_loader, conversation, user_context, page, start_time
        )

    line_idx = pending_indices[pos]
    line     = lines_as_dicts[line_idx]

    qty_match = _re.search(r'\b(\d+)\b', message)
    if not qty_match:
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success":     True,
            "bot_message": (
                f"Please enter a quantity for **{line['product_name']}** "
                f"(e.g. 1, 5, 10):"
            ),
            "intent":      "guided_flow",
            "products":    [],
            "suggestions": ["Cancel"],
            "session_id":  str(conversation.id),
            "metadata": {
                "flow_state":       FlowState.AWAITING_BULK_QUANTITY.value,
                "response_time_ms": elapsed,
            },
            "flow_state":  FlowState.AWAITING_BULK_QUANTITY.value,
            "pagination":  default_pagination(page),
        }), 200

    line["quantity"]              = int(qty_match.group(1))
    line["quantity_explicitly_set"] = True
    lines_as_dicts[line_idx]     = line

    next_pos = pos + 1
    user_context["bulk_quantity_current_pos"] = next_pos
    user_context["pending_bulk_lines"]        = lines_as_dicts
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    # More lines still need a quantity?
    if next_pos < len(pending_indices):
        next_line = lines_as_dicts[pending_indices[next_pos]]
        elapsed   = round((time.time() - start_time) * 1000)
        return jsonify({
            "success":     True,
            "bot_message": _build_quantity_prompt(next_line),
            "intent":      "guided_flow",
            "products":    [],
            "suggestions": ["Cancel"],
            "session_id":  str(conversation.id),
            "metadata": {
                "flow_state":       FlowState.AWAITING_BULK_QUANTITY.value,
                "response_time_ms": elapsed,
            },
            "flow_state":  FlowState.AWAITING_BULK_QUANTITY.value,
            "pagination":  default_pagination(page),
        }), 200

    return _continue_after_quantity_filled(
        lines_as_dicts, store_loader, conversation, user_context, page, start_time
    )


def _continue_after_quantity_filled(lines_as_dicts, store_loader, conversation, user_context, page, start_time):
    """
    Exit point after all quantity slots are filled.
    Cleans up quantity-tracking keys then continues to variant selection or confirmation.
    """
    user_context.pop("bulk_quantity_pending_indices", None)
    user_context.pop("bulk_quantity_current_pos", None)

    needs_variant_indices = [
        i for i, l in enumerate(lines_as_dicts)
        # See the note at the first gate: a matched variation can still leave
        # axes as "Any", which the shopper must still choose.
        if l.get("product_id")
        and _is_variable_product(l["product_id"], store_loader)
        and (not l.get("variation_id")
             or l.get("blank_variant_axes")
             or _ensure_missing_axes(l, user_context))
    ]
    if needs_variant_indices:
        user_context["bulk_variant_line_indices"] = needs_variant_indices
        user_context["bulk_variant_current_pos"]  = 0
        user_context["bulk_variant_cache"]        = {}
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, 0,
            conversation, user_context, page, start_time,
        )

    return _build_bulk_confirmation_response(
        lines_as_dicts, conversation, user_context, page, start_time
    )