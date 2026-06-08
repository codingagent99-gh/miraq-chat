"""
handlers/bulk_order_handler.py — Bulk order flow handler.

Feature 3: Multi-line, multi-customer bulk ordering for rep and non-rep users.

Public functions:
    handle_bulk_order_trigger()               — entry point, AWAITING_BULK_ORDER_INPUT
    handle_bulk_order_input()                 — parse utterance → confirmation table
    handle_bulk_order_confirmation()          — start per-customer address confirmation
    handle_bulk_address_confirmation_reply()  — route address-step actions

Private helpers:
    _format_bulk_confirmation_table()         — markdown summary table
    _advance_to_next_address_confirmation()   — step through resolved lines
    _create_all_confirmed_orders()            — place all confirmed orders

Session management (db.session.add / db.session.commit) is the caller's
responsibility in chat.py. This module only mutates conversation attributes
and calls flag_modified.
"""

import time
import json

from flask import jsonify
from sqlalchemy.orm.attributes import flag_modified

from woo_client import woo_client
from ecommerce import endpoints
from app_config import (
    DEFAULT_PAYMENT_METHOD,
    DEFAULT_PAYMENT_METHOD_TITLE,
    BULK_ORDER_ROLES,
)
from conversation_flow import FlowState
from chat_logger import get_logger
from handlers.chat_utils import default_pagination
from parsers.bulk_order_parser import parse_bulk_order_utterance, BulkOrderLine
import re
import difflib
logger = get_logger("miraq_chat")

_BULK_STATE_KEYS = (
    "pending_bulk_lines", "bulk_current_line_index",
    "bulk_confirmed_lines", "bulk_address_overrides",
    "bulk_awaiting_address_text",
    "bulk_product_missing_indices", "bulk_product_current_pos",
    "bulk_quantity_pending_indices", "bulk_quantity_current_pos",
)

def handle_cancel_bulk_order(user_context, conversation, page, start_time):
    for k in _BULK_STATE_KEYS:
        user_context.pop(k, None)
    flag_modified(conversation, "context_data")
    conversation.flow_state = FlowState.IDLE.value
    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": "Bulk order cancelled. What else can I help you with?",
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Show me products", "Start a bulk order"],
        "session_id": str(conversation.id),
        "metadata": {"flow_state": FlowState.IDLE.value, "response_time_ms": elapsed},
        "flow_state": FlowState.IDLE.value,
        "pagination": default_pagination(page),
    }), 200


def handle_bulk_confirmation_unclear(conversation, page, start_time):
    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": "Please reply **Yes** to confirm all orders or **No** to cancel.",
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Yes, confirm", "No, cancel"],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_ORDER_CONFIRMATION.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_ORDER_CONFIRMATION.value,
        "pagination": default_pagination(page),
    }), 200
# ══════════════════════════════════════════════════════════════
# ── Helper: detect variable products ──
# ══════════════════════════════════════════════════════════════

def _is_variable_product(product_id: int, store_loader) -> bool:
    """Return True if the catalog entry for product_id has variations."""
    if not store_loader or not product_id:
        return False
    for p in (store_loader.products or []):
        if p.get("id") == product_id:
            return bool(p.get("variations")) or p.get("type") == "variable"
    return False

# ══════════════════════════════════════════════════════════════
# ── Helper: dual-access for BulkOrderLine or dict ──
# ══════════════════════════════════════════════════════════════

def _get(line, key, default=None):
    """Read a field from either a BulkOrderLine dataclass or a plain dict."""
    if isinstance(line, dict):
        return line.get(key, default)
    return getattr(line, key, default)


# ══════════════════════════════════════════════════════════════
# ── Function 1: handle_bulk_order_trigger ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_order_trigger(conversation, user_context, page, start_time):
    """
    Entry point for the bulk order flow.
    Sets flow state to AWAITING_BULK_ORDER_INPUT and prompts the rep.
    Returns a Flask response.
    """
    conversation.flow_state = FlowState.AWAITING_BULK_ORDER_INPUT.value

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": (
            "Tell me everything you need to order today. "
            "You can include multiple customers and products in one message.\n\n"
            "**Example:**\n"
            "*Order 20 Harmony White for abc@buildersco.com, 15 Coral Grey for xyz@interiors.com*"
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

def handle_bulk_order_input(message, store_loader, conversation, user_context, page, start_time):
    """
    Parses the free-text bulk order utterance, serializes lines into
    user_context, and returns a confirmation table for the rep to approve.
    Returns a Flask response.
    """
    role = user_context.get("role", "")
    customer_id = conversation.customer_id

    # Step 2: Parse
    lines = parse_bulk_order_utterance(
        text=message,
        store_loader=store_loader,
        role=role,
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
            "email": l.email,
            "product_name": l.product_name,
            "quantity": l.quantity,
            "product_id": l.product_id,
            "variation_id": l.variation_id,
            "customer_id": l.customer_id,
            "customer_display_name": l.customer_display_name,
            "shipping_address": l.shipping_address,
            "billing_address": l.billing_address,
            "is_reorder": l.is_reorder,
            "reorder_source_order_id": l.reorder_source_order_id,
            "unresolved": l.unresolved,
            "unresolved_reason": l.unresolved_reason,
            "quantity_explicitly_set":  l.quantity_explicitly_set,
            "address_confirmed": False,
            "address_skipped": False,
        }
        for l in lines
    ]

    user_context["pending_bulk_lines"] = lines_as_dicts
    user_context["bulk_current_line_index"] = 0
    user_context["bulk_confirmed_lines"] = []
    user_context["bulk_address_overrides"] = {}
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    
    
    # Step 4.5: Any line with a completely blank product name → ask before anything else
    # (applies to all roles — no point asking email/address without knowing what to order)
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

        first_line = lines_as_dicts[product_blank_indices[0]]
        customer_hint = (
            f" for **{first_line['customer_display_name']}**"
            if first_line.get("customer_id") else ""
        )
        already_resolved = [l for l in lines_as_dicts if not l.get("unresolved")]
        resolved_note = f"\n\n{len(already_resolved)} other line(s) already resolved." if already_resolved else ""

        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                f"What product and quantity would you like to order{customer_hint}?{resolved_note}"
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

    # Step 4.6: Rep lines missing an email → ask before proceeding
    if role in BULK_ORDER_ROLES:
        email_missing = [l for l in lines_as_dicts if l.get("unresolved_reason") == "email_not_provided"]
        if email_missing:
            conversation.flow_state = FlowState.AWAITING_BULK_EMAIL.value
            conversation.context_data = user_context
            flag_modified(conversation, "context_data")

            product_lines = "\n".join(
                f"• **{l['quantity']}× {l['product_name']}**"
                for l in email_missing
            )
            already_resolved = [l for l in lines_as_dicts if not l.get("unresolved")]
            resolved_note = f"\n\n{len(already_resolved)} other line(s) already resolved." if already_resolved else ""

            elapsed = round((time.time() - start_time) * 1000)
            return jsonify({
                "success": True,
                "bot_message": (
                    f"Got it:\n{product_lines}{resolved_note}\n\n"
                    "Please provide the customer's email address."
                ),
                "intent": "guided_flow",
                "products": [],
                "suggestions": ["Cancel"],
                "session_id": str(conversation.id),
                "metadata": {
                    "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
                    "response_time_ms": elapsed,
                },
                "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
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
        if l["product_id"] and not l["variation_id"]
        and _is_variable_product(l["product_id"], store_loader)
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
# ── Function 3: _format_bulk_confirmation_table (private) ──
# ══════════════════════════════════════════════════════════════

def _format_bulk_confirmation_table(lines) -> str:
    """
    Build a markdown table summarising all parsed bulk order lines.
    Accepts both BulkOrderLine dataclass instances and plain dicts.
    """
    rows = []
    for line in lines:
        customer = _get(line, "customer_display_name", "")
        product  = _get(line, "product_name", "")
        qty      = _get(line, "quantity", 0)
        unresolved      = _get(line, "unresolved", False)
        unresolved_reason = _get(line, "unresolved_reason")

        if not unresolved:
            status = "✅ Ready"
        elif unresolved_reason == "product_not_found":
            status = "❌ Product not found"
        elif unresolved_reason == "email_not_provided":
            status = "❌ Email required"
        elif unresolved_reason == "email_not_found":
            status = "❌ Customer not found"
        elif unresolved_reason == "both_not_found":
            status = "❌ Both not found"
        else:
            status = "❌ Unresolved"

        rows.append(f"| {customer} | {product} | {qty} | {status} |")

    resolved_count = sum(1 for l in lines if not _get(l, "unresolved", False))
    skipped_count  = len(lines) - resolved_count

    table = (
        "Here's your bulk order summary:\n\n"
        "| Customer | Product | Qty | Status |\n"
        "|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n"
    )

    if skipped_count > 0:
        table += f"\n⚠️ {skipped_count} line(s) could not be resolved and will be skipped.\n"
    table += f"✅ {resolved_count} order(s) ready to place."

    return table

# ══════════════════════════════════════════════════════════════
# ── Private: _ask_for_bulk_variant ──
# ══════════════════════════════════════════════════════════════

def _ask_for_bulk_variant(
    lines_as_dicts, needs_variant_indices, pos,
    conversation, user_context, page, start_time,
):
    line_idx = needs_variant_indices[pos]
    line = lines_as_dicts[line_idx]
    product_id = line["product_id"]

    cache = user_context.setdefault("bulk_variant_cache", {})
    cache_key = str(product_id)

    if cache_key not in cache:
        var_call = endpoints.list_variants(
            product_id=product_id,
            per_page=100,
            description=f"Fetch variations for bulk order product_id={product_id}",
        )
        var_result = woo_client.execute(var_call)
        raw = var_result.get("data", []) if var_result.get("success") else []
        cache[cache_key] = raw if isinstance(raw, list) else []
        user_context["bulk_variant_cache"] = cache
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")

    variations = cache.get(cache_key, [])

    attr_axes: dict = {}
    for var in variations:
        for attr in var.get("attributes", []):
            name = attr.get("name", "")
            option = attr.get("option", "")
            if name and option:
                attr_axes.setdefault(name, set()).add(option)

    attributes = [
        {"name": name, "options": sorted(opts)}
        for name, opts in attr_axes.items()
    ]

    variation_list = [
        {
            "id": var["id"],
            "attributes": {a["name"]: a["option"] for a in var.get("attributes", [])},
        }
        for var in variations
    ]

    conversation.flow_state = FlowState.AWAITING_BULK_VARIANT_SELECTION.value

    # ▼ CHANGED: wrap data inside "payload" key
    action = {
        "type": "SHOW_BULK_VARIANT_PROMPT",
        "payload": {
            "line_index": line_idx,
            "company": line.get("customer_display_name", ""),
            "product_name": line.get("product_name", ""),
            "quantity": line.get("quantity", 0),
            "progress": {"current": pos + 1, "total": len(needs_variant_indices)},
            "attributes": attributes,
            "variations": variation_list,
        },
    }

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": (
            f"Please select the missing product details for "
            f"**{line['product_name']}** "
            f"({line.get('customer_display_name', '')} × {line.get('quantity', 0) or '?'}):"
        ),
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Cancel"],
        "actions": [action],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_VARIANT_SELECTION.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_VARIANT_SELECTION.value,
        "pagination": default_pagination(page),
    }), 200

# ══════════════════════════════════════════════════════════════
# ── Private: _build_bulk_confirmation_response ──
# ══════════════════════════════════════════════════════════════

def _build_bulk_confirmation_response(lines_as_dicts, conversation, user_context, page, start_time):
    resolved_count = sum(1 for l in lines_as_dicts if not l.get("unresolved"))
    unresolved_count = len(lines_as_dicts) - resolved_count

    # ▼ CHANGED: wrap data inside "payload" key
    action = {
        "type": "SHOW_BULK_ORDER_CONFIRMATION",
        "payload": {
            "lines": lines_as_dicts,
            "resolved_count": resolved_count,
            "unresolved_count": unresolved_count,
        },
    }

    bot_message = f"Here's your bulk order — {resolved_count} order(s) ready to place."
    if unresolved_count:
        bot_message += f" {unresolved_count} unresolved line(s) will be skipped."
    bot_message += " Would you like to proceed?"

    for k in ("bulk_variant_line_indices", "bulk_variant_current_pos", "bulk_variant_cache"):
        user_context.pop(k, None)

    conversation.flow_state = FlowState.AWAITING_BULK_ORDER_CONFIRMATION.value
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Yes, confirm", "No, cancel"],
        "actions": [action],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_ORDER_CONFIRMATION.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_ORDER_CONFIRMATION.value,
        "pagination": default_pagination(page),
    }), 200

# ══════════════════════════════════════════════════════════════
# ── Public: handle_bulk_variant_selection_reply ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_variant_selection_reply(
    message, store_loader, conversation, user_context, page, start_time
):
    """
    Called when the user replies during AWAITING_BULK_VARIANT_SELECTION.
    Scores each variation by how many of its attribute options appear in the
    message, stamps the best match as variation_id, then advances.
    Re-prompts via _ask_for_bulk_variant if no match is found.
    """
    lines_as_dicts = user_context.get("pending_bulk_lines", [])
    needs_variant_indices = user_context.get("bulk_variant_line_indices", [])
    pos = user_context.get("bulk_variant_current_pos", 0)

    # Guard — shouldn't be here
    if not needs_variant_indices or pos >= len(needs_variant_indices):
        return _build_bulk_confirmation_response(
            lines_as_dicts, conversation, user_context, page, start_time
        )

    line_idx = needs_variant_indices[pos]
    line = lines_as_dicts[line_idx]
    product_id = line["product_id"]
    cache = user_context.get("bulk_variant_cache", {})
    variations = cache.get(str(product_id), [])

    # Score: count how many attribute options from a variation appear in the message
    msg_lower = message.lower()
    best_match = None
    best_score = -1

    for var in variations:
        attrs = var.get("attributes", [])
        if not attrs:
            continue
        score = sum(
            1 for a in attrs if a.get("option", "").lower() in msg_lower
        )
        if score > best_score:
            best_score = score
            best_match = var

    if not best_match or best_score == 0:
        # Re-show the same prompt
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, pos,
            conversation, user_context, page, start_time,
        )

    # Stamp the resolved variation
    import re as _re

    line["variation_id"] = best_match["id"]
    line["unresolved"] = False
    line["unresolved_reason"] = None

    # If quantity was not specified earlier, extract the last standalone
    # integer from the user's reply. Attribute options like "12x24" don't
    # produce bare word-boundary integers, so the last match is the qty.
    if not line.get("quantity") or not line.get("quantity_explicitly_set"):
        qty_matches = _re.findall(r'\b(\d+)\b', message)
        if qty_matches:
            line["quantity"] = int(qty_matches[-1])
            line["quantity_explicitly_set"] = True   # ← ADD
    
    lines_as_dicts[line_idx] = line
    user_context["pending_bulk_lines"] = lines_as_dicts

    next_pos = pos + 1
    user_context["bulk_variant_current_pos"] = next_pos
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    if next_pos < len(needs_variant_indices):
        return _ask_for_bulk_variant(
            lines_as_dicts, needs_variant_indices, next_pos,
            conversation, user_context, page, start_time,
        )

    # All variants resolved — show structured confirmation table
    return _build_bulk_confirmation_response(
        lines_as_dicts, conversation, user_context, page, start_time
    )

# ══════════════════════════════════════════════════════════════
# ── Public: handle_bulk_email_reply ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_email_reply(message, store_loader, conversation, user_context, page, start_time):
    """
    Called during AWAITING_BULK_EMAIL when the rep provides customer email(s).
    Extracts email(s), resolves customers via API, stamps the pending lines,
    then resumes the normal bulk flow (variant selection → confirmation).
    """
    import re as _re
    _EMAIL_RE = _re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', _re.I)

    emails = _EMAIL_RE.findall(message)
    if not emails:
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": "I couldn't find a valid email address. Please provide the customer's email.",
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
            "pagination": default_pagination(page),
        }), 200

    lines_as_dicts = user_context.get("pending_bulk_lines", [])
    missing_indices = [
        i for i, l in enumerate(lines_as_dicts)
        if l.get("unresolved_reason") == "email_not_provided"
    ]

    # Multiple emails + single unresolved line → clone the line for each email
    if len(emails) > 1 and len(missing_indices) == 1:
        base_idx = missing_indices[0]
        base = lines_as_dicts[base_idx]
        clones = [{**base, "email": email} for email in emails]
        lines_as_dicts = lines_as_dicts[:base_idx] + clones + lines_as_dicts[base_idx + 1:]
        missing_indices = list(range(base_idx, base_idx + len(emails)))
    else:
        # Assign emails round-robin to unresolved lines
        for i, line_idx in enumerate(missing_indices):
            lines_as_dicts[line_idx]["email"] = emails[i % len(emails)]

    # Resolve each newly assigned email
    email_cache: dict = {}
    for line_idx in missing_indices:
        line = lines_as_dicts[line_idx]
        email = line.get("email", "")
        if not email:
            continue

        if email not in email_cache:
            call = endpoints.search_customers_by_email(
                email=email,
                per_page=1,
                description=f"Bulk email reply lookup: '{email}'",
            )
            result = woo_client.execute(call)
            customers = result.get("data", []) if result.get("success") else []
            if isinstance(customers, list) and customers:
                c = customers[0]
                company = c.get("company") or c.get("billing", {}).get("company", "")
                full_name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
                billing = c.get("billing", {}) or {}
                shipping = c.get("shipping", {}) or {}
                if not shipping.get("address_1"):
                    shipping = billing
                email_cache[email] = {
                    "id": str(c["id"]),
                    "display": company or full_name or f"Customer #{c['id']}",
                    "billing": billing,
                    "shipping": shipping,
                }
            else:
                email_cache[email] = None

        resolution = email_cache.get(email)
        if resolution:
            line["customer_id"] = resolution["id"]
            line["customer_display_name"] = resolution["display"]
            line["billing_address"] = resolution["billing"]
            line["shipping_address"] = resolution["shipping"]
            line["unresolved"] = line.get("product_id") is None
            line["unresolved_reason"] = "product_not_found" if line["unresolved"] else None
        else:
            line["unresolved"] = True
            line["unresolved_reason"] = "email_not_found"
            line["customer_display_name"] = "⚠️ Not found"

    user_context["pending_bulk_lines"] = lines_as_dicts
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    
    blank_after_email = [
        i for i, l in enumerate(lines_as_dicts)
        if not l.get("product_name", "").strip()
    ]
    if blank_after_email:
        user_context["bulk_product_missing_indices"] = blank_after_email
        user_context["bulk_product_current_pos"] = 0
        conversation.flow_state = FlowState.AWAITING_BULK_PRODUCT.value
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")

        first_line = lines_as_dicts[blank_after_email[0]]
        customer_hint = (
            f" for **{first_line['customer_display_name']}**"
            if first_line.get("customer_id") else ""
        )
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": f"What product and quantity would you like to order{customer_hint}?",
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
        
    qty_unset = [
        i for i, l in enumerate(lines_as_dicts)
        if not l.get("quantity_explicitly_set") and not l.get("unresolved")
    ]
    if qty_unset:
        return _prompt_for_quantity(
            qty_unset, lines_as_dicts, conversation, user_context, page, start_time
        )

    # Check for variable products still needing variant selection
    needs_variant_indices = [
        i for i, l in enumerate(lines_as_dicts)
        if l.get("product_id") and not l.get("variation_id")
        and _is_variable_product(l["product_id"], store_loader)
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

    return _build_bulk_confirmation_response(
        lines_as_dicts, conversation, user_context, page, start_time
    )

# ══════════════════════════════════════════════════════════════
# ── Public: handle_product_reorder ──
# ══════════════════════════════════════════════════════════════

def handle_product_reorder(payload, store_loader, conversation, user_context, page, start_time):
    """
    Called when a rep clicks 'Reorder' on the product order history card.

    Payload shape (from _format_product_orders_for_action):
        {
            "order_id":              "12345",
            "order_number":          "12345",
            "customer_id":           "272749971",
            "customer_display_name": "Abel Design Group",
            "items": [
                {"product_name": "Harmony White", "product_id": 16972,
                 "variation_id": 0, "quantity": 20}
            ]
        }

    Builds BulkOrderLine dicts and drops straight into address confirmation —
    no variant or quantity prompts needed (those are already known from the
    original order).
    """
    customer_id      = payload.get("customer_id", "")
    customer_display = payload.get("customer_display_name", "")
    order_id         = payload.get("order_id", "")
    items            = payload.get("items", [])

    if not customer_id or not items:
        logger.warning(f"handle_product_reorder | missing customer_id or items | payload={payload}")
        return None

    # ── Fetch fresh billing/shipping address for this customer ──
    billing  = {}
    shipping = {}
    try:
        cust_call   = endpoints.fetch_customer(
            customer_id=int(customer_id),
            description=f"Fetch address for reorder — {customer_display}",
        )
        cust_result = woo_client.execute(cust_call)
        if cust_result.get("success") and isinstance(cust_result.get("data"), dict):
            data     = cust_result["data"]
            billing  = data.get("billing",  {}) or {}
            shipping = data.get("shipping", {}) or {}
            if not shipping.get("address_1"):
                shipping = billing
    except Exception as exc:
        logger.warning(f"handle_product_reorder | address fetch failed | error={exc}")

    # ── Build one BulkOrderLine per line item ──
    lines_as_dicts = []
    for item in items:
        product_id   = item.get("product_id")
        variation_id = item.get("variation_id") or None
        if not product_id:
            continue
        lines_as_dicts.append({
            "raw_fragment":            "",
            "company_name":            customer_display,
            "email":                   "",
            "product_name":            item.get("product_name", ""),
            "quantity":                item.get("quantity", 1),
            "quantity_explicitly_set": True,
            "product_id":              product_id,
            "variation_id":            variation_id,
            "customer_id":             str(customer_id),
            "customer_display_name":   customer_display,
            "shipping_address":        shipping,
            "billing_address":         billing,
            "is_reorder":              True,
            "reorder_source_order_id": int(order_id) if order_id else None,
            "unresolved":              False,
            "unresolved_reason":       None,
            "address_confirmed":       False,
            "address_skipped":         False,
        })

    if not lines_as_dicts:
        return None

    # ── Store state and go straight to address confirmation ──
    user_context["pending_bulk_lines"]     = lines_as_dicts
    user_context["bulk_current_line_index"] = 0
    user_context["bulk_confirmed_lines"]   = []
    user_context["bulk_address_overrides"] = {}
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    logger.info(
        f"handle_product_reorder | order_id={order_id} | customer={customer_display} "
        f"| {len(lines_as_dicts)} line(s)"
    )

    return _advance_to_next_address_confirmation(
        resolved_lines=lines_as_dicts,
        idx=0,
        conversation=conversation,
        user_context=user_context,
        page=page,
        start_time=start_time,
    )
    
# ══════════════════════════════════════════════════════════════
# ── Public: handle_bulk_product_reply ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_product_reply(message, store_loader, conversation, user_context, page, start_time):
    """
    Called during AWAITING_BULK_PRODUCT when the rep provides a product name
    (and optional quantity) for a line that had a blank product.

    After filling the product, resumes the normal pipeline:
      blank product → email missing → variant selection → confirmation
    """
    lines_as_dicts = user_context.get("pending_bulk_lines", [])
    missing_indices = user_context.get("bulk_product_missing_indices", [])
    pos = user_context.get("bulk_product_current_pos", 0)
    role = user_context.get("role", "")

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
        next_idx = missing_indices[next_pos]
        next_line = lines_as_dicts[next_idx]
        customer_hint = (
            f" for **{next_line['customer_display_name']}**"
            if next_line.get("customer_id") else ""
        )
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": f"What product and quantity would you like to order{customer_hint}?",
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
    Shared exit point after all blank-product slots are filled.
    Cleans up product-tracking keys, then checks for missing emails,
    variable products, and finally builds the confirmation response.
    """
    role = user_context.get("role", "")

    # Clean up product-slot tracking
    user_context.pop("bulk_product_missing_indices", None)
    user_context.pop("bulk_product_current_pos", None)
    
    # Quantity unset on any resolved line?       ← ADD
    qty_unset = [
        i for i, l in enumerate(lines_as_dicts)
        if not l.get("quantity_explicitly_set") and not l.get("unresolved")
    ]
    if qty_unset:
        return _prompt_for_quantity(
            qty_unset, lines_as_dicts, conversation, user_context, page, start_time
        )

    # Email still missing on any line?
    if role in BULK_ORDER_ROLES:
        email_missing = [l for l in lines_as_dicts if l.get("unresolved_reason") == "email_not_provided"]
        if email_missing:
            conversation.flow_state = FlowState.AWAITING_BULK_EMAIL.value
            conversation.context_data = user_context
            flag_modified(conversation, "context_data")

            product_lines = "\n".join(
                f"• **{l['quantity']}× {l['product_name']}**" for l in email_missing
            )
            elapsed = round((time.time() - start_time) * 1000)
            return jsonify({
                "success": True,
                "bot_message": (
                    f"Got it:\n{product_lines}\n\n"
                    "Please provide the customer's email address."
                ),
                "intent": "guided_flow",
                "products": [],
                "suggestions": ["Cancel"],
                "session_id": str(conversation.id),
                "metadata": {
                    "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
                    "response_time_ms": elapsed,
                },
                "flow_state": FlowState.AWAITING_BULK_EMAIL.value,
                "pagination": default_pagination(page),
            }), 200

    # Variable products needing variant selection?
    needs_variant_indices = [
        i for i, l in enumerate(lines_as_dicts)
        if l.get("product_id") and not l.get("variation_id")
        and _is_variable_product(l["product_id"], store_loader)
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

    return _build_bulk_confirmation_response(
        lines_as_dicts, conversation, user_context, page, start_time
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
        "bot_message": (
            f"How many **{line['product_name']}** "
            f"for **{line['customer_display_name']}**?"
        ),
        "intent":      "guided_flow",
        "products":    [],
        "suggestions": ["1", "5", "10", "15", "20", "Cancel"],
        "session_id":  str(conversation.id),
        "metadata": {
            "flow_state":       FlowState.AWAITING_BULK_QUANTITY.value,
            "response_time_ms": elapsed,
        },
        "flow_state":  FlowState.AWAITING_BULK_QUANTITY.value,
        "pagination":  default_pagination(page),
    }), 200


# ══════════════════════════════════════════════════════════════
# ── Public: handle_bulk_quantity_reply ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_quantity_reply(message, store_loader, conversation, user_context, page, start_time):
    """
    Called during AWAITING_BULK_QUANTITY when the rep specifies a quantity.
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
            "suggestions": ["1", "5", "10", "15", "20", "Cancel"],
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
            "bot_message": (
                f"How many **{next_line['product_name']}** "
                f"for **{next_line['customer_display_name']}**?"
            ),
            "intent":      "guided_flow",
            "products":    [],
            "suggestions": ["1", "5", "10", "15", "20", "Cancel"],
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
        if l.get("product_id") and not l.get("variation_id")
        and _is_variable_product(l["product_id"], store_loader)
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
    
# ══════════════════════════════════════════════════════════════
# ── Function 4: handle_bulk_order_confirmation ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_order_confirmation(user_context, conversation, page, start_time):
    """
    Called when the rep confirms the bulk order table (action == "confirm_bulk_order").
    Begins the per-customer address confirmation loop.
    Returns a Flask response.
    """
    lines = user_context.get("pending_bulk_lines", [])
    resolved_lines = [l for l in lines if not l["unresolved"]]

    # No valid lines to place
    if not resolved_lines:
        user_context.pop("pending_bulk_lines", None)
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        conversation.flow_state = FlowState.IDLE.value

        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": "No valid order lines to place.",
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Place another bulk order", "Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.IDLE.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.IDLE.value,
            "pagination": default_pagination(page),
        }), 200

    return _advance_to_next_address_confirmation(
        resolved_lines=resolved_lines,
        idx=0,
        conversation=conversation,
        user_context=user_context,
        page=page,
        start_time=start_time,
    )


# ══════════════════════════════════════════════════════════════
# ── Function 5: handle_bulk_address_confirmation_reply ──
# ══════════════════════════════════════════════════════════════

def handle_bulk_address_confirmation_reply(action, message, conversation, user_context, page, start_time):
    """
    Routes address-step actions during AWAITING_BULK_ADDRESS_CONFIRMATION.
    action is one of:
        "bulk_address_confirmed"         — use the address shown
        "bulk_address_change"            — user wants to type a different address (legacy)
        "bulk_address_override_text"     — user has typed a new address (legacy)
        "bulk_address_override_structured" — user saved edited billing+shipping via the panel
        "bulk_address_skip"              — skip this order entirely
    Returns a Flask response.
    """
    lines = user_context.get("pending_bulk_lines", [])
    resolved_lines = [l for l in lines if not l["unresolved"]]
    idx = user_context.get("bulk_current_line_index", 0)

    # Step 1: All lines already processed
    if idx >= len(resolved_lines):
        return _create_all_confirmed_orders(user_context, conversation, page, start_time)

    current_line = resolved_lines[idx]

    # Step 3: Address confirmed — use address as-is
    if action == "bulk_address_confirmed":
        current_line["address_confirmed"] = True
        user_context["bulk_current_line_index"] = idx + 1
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            resolved_lines, idx + 1, conversation, user_context, page, start_time
        )

    # Step 3b: Structured save from the inline edit panel.
    # message is "__BULK_ADDR__<json>" with {"billing": {...}, "shipping": {...}}.
    # Override is keyed by line index so repeated companies stay independent.
    elif action == "bulk_address_override_structured":
        raw = message.strip()
        if raw.startswith("__BULK_ADDR__"):
            raw = raw[len("__BULK_ADDR__"):]
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError) as exc:
            logger.warning(
                f"bulk_address_override_structured | bad JSON | error={exc} | raw={raw[:200]!r}"
            )
            parsed = {}

        edited_billing = parsed.get("billing") or {}
        edited_shipping = parsed.get("shipping") or {}

        overrides = user_context.setdefault("bulk_address_overrides", {})
        overrides[str(idx)] = {
            "billing": edited_billing,
            "shipping": edited_shipping,
        }
        current_line["address_confirmed"] = True
        user_context.pop("bulk_awaiting_address_text", None)
        user_context["bulk_current_line_index"] = idx + 1
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            resolved_lines, idx + 1, conversation, user_context, page, start_time
        )

    # Step 4: Rep wants to change the address — set sub-state flag, re-prompt
    elif action == "bulk_address_change":
        user_context["bulk_awaiting_address_text"] = True
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": "Please type the new shipping address:",
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Skip this order", "Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
            "pagination": default_pagination(page),
        }), 200

    # Step 5: Rep has typed a new address (legacy free-text path)
    elif action == "bulk_address_override_text":
        user_context.pop("bulk_awaiting_address_text", None)
        overrides = user_context.setdefault("bulk_address_overrides", {})
        overrides[str(idx)] = {
            "shipping": {
                "address_1": message.strip(),
                "city": "",
                "state": "",
                "postcode": "",
                "country": "",
            },
        }
        current_line["address_confirmed"] = True
        user_context["bulk_current_line_index"] = idx + 1
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            resolved_lines, idx + 1, conversation, user_context, page, start_time
        )

    # Step 6: Skip this customer's order
    elif action == "bulk_address_skip":
        current_line["address_skipped"] = True
        user_context["bulk_current_line_index"] = idx + 1
        conversation.context_data = user_context
        flag_modified(conversation, "context_data")
        return _advance_to_next_address_confirmation(
            resolved_lines, idx + 1, conversation, user_context, page, start_time
        )

    # Unexpected action — fall through safely
    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": "Please reply **Yes** to confirm the address, **Change address** to update it, or **Skip** to skip this order.",
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Yes, confirm", "Change address", "Skip this order"],
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
        "pagination": default_pagination(page),
    }), 200


# ══════════════════════════════════════════════════════════════
# ── Function 6: _advance_to_next_address_confirmation (private) ──
# ══════════════════════════════════════════════════════════════

def _advance_to_next_address_confirmation(resolved_lines, idx, conversation, user_context, page, start_time):
    # Step 1: Skip already-processed lines
    while idx < len(resolved_lines):
        line = resolved_lines[idx]
        if not line.get("address_confirmed") and not line.get("address_skipped"):
            break
        idx += 1

    if idx >= len(resolved_lines):
        return _create_all_confirmed_orders(user_context, conversation, page, start_time)

    current_line = resolved_lines[idx]
    user_context["bulk_current_line_index"] = idx
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    conversation.flow_state = FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value

    # The shipping block drives the read-only summary line on the card.
    shipping_block = current_line.get("shipping_address") or {}
    billing_block = current_line.get("billing_address") or {}

    if not shipping_block.get("address_1"):
        try:
            cust_call = endpoints.fetch_customer(
                customer_id=int(current_line["customer_id"]),
                description=f"Fetch address for {current_line['customer_display_name']}",
            )
            cust_result = woo_client.execute(cust_call)
            if cust_result.get("success") and isinstance(cust_result.get("data"), dict):
                data = cust_result["data"]
                fetched_billing = data.get("billing", {}) or {}
                fetched_shipping = data.get("shipping", {}) or {}
                # Shipping block drives this confirmation card; fall back to
                # billing if the customer has no shipping address on file.
                shipping_block = fetched_shipping if fetched_shipping.get("address_1") else fetched_billing
                current_line["shipping_address"] = shipping_block
                # Populate the billing block too, so order creation has both.
                if not billing_block.get("address_1"):
                    billing_block = fetched_billing or shipping_block
                    current_line["billing_address"] = billing_block
                conversation.context_data = user_context
                flag_modified(conversation, "context_data")
        except Exception as exc:
            logger.warning(
                f"_advance_to_next_address_confirmation | failed to fetch address "
                f"for customer_id={current_line.get('customer_id')} | error={exc}"
            )

    addr_parts = [
        shipping_block.get("address_1", ""),
        shipping_block.get("address_2", ""),
        shipping_block.get("city", ""),
        shipping_block.get("state", ""),
        shipping_block.get("postcode", ""),
    ]
    addr_str = ", ".join(p for p in addr_parts if p) or "No address on file"

    items_text = f"{current_line['product_name']} ×{current_line['quantity']}"
    if current_line.get("is_reorder"):
        items_text = f"[Reorder] {items_text}"

    # Full field set so the inline edit panel can prefill every field.
    # Billing carries the CS custom fields; shipping carries order_notes.
    _BILLING_FIELDS = (
        "first_name", "last_name", "company",
        "billing_field_type", "billing_project",
        "address_1", "address_2", "city", "state", "postcode", "country",
        "phone", "email", "project_rep",
    )
    _SHIPPING_FIELDS = (
        "first_name", "last_name", "company",
        "address_1", "address_2", "city", "state", "postcode", "country",
        "order_notes",
    )

    def _pick(block, fields):
        return {f: (block or {}).get(f, "") for f in fields}

    billing_payload = _pick(billing_block, _BILLING_FIELDS)
    shipping_payload = _pick(shipping_block, _SHIPPING_FIELDS)

    # Default "Your Rep" to the logged-in rep's email so the dropdown is
    # pre-selected from the start, unless the company record already has one.
    if not billing_payload.get("project_rep"):
        billing_payload["project_rep"] = user_context.get("rep_email", "")

    # ▼ emit a structured action so React can render the address card + panel
    address_action = {
        "type": "SHOW_BULK_ADDRESS_CONFIRMATION",
        "payload": {
            "customer_name": current_line["customer_display_name"],
            "items_text": items_text,
            # Legacy read-only summary fields (kept for back-compat).
            "address": {
                "address_1": shipping_block.get("address_1", ""),
                "address_2": shipping_block.get("address_2", ""),
                "city":      shipping_block.get("city", ""),
                "state":     shipping_block.get("state", ""),
                "postcode":  shipping_block.get("postcode", ""),
            },
            "addr_str": addr_str,
            # Full structured blocks for the editable panel prefill.
            "billing": billing_payload,
            "shipping": shipping_payload,
            "progress": {"current": idx + 1, "total": len(resolved_lines)},
        },
    }

    bot_message = (
        f"**Order for {current_line['customer_display_name']}** "
        f"({idx + 1} of {len(resolved_lines)})\n\n"
        f"📦 {items_text}\n"
        f"📍 Shipping to: {addr_str}\n\n"
        "Confirm this address?"
    )

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "guided_flow",
        "products": [],
        "suggestions": ["Yes, confirm", "Change address", "Skip this order"],
        "actions": [address_action],                          # ← NEW
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_BULK_ADDRESS_CONFIRMATION.value,
        "pagination": default_pagination(page),
    }), 200

# ══════════════════════════════════════════════════════════════
# ── Function 7: _create_all_confirmed_orders (private) ──
# ══════════════════════════════════════════════════════════════

def _create_all_confirmed_orders(user_context, conversation, page, start_time):
    """
    Places WooCommerce orders for every confirmed line, then clears all
    bulk state and returns a summary response.
    Returns a Flask response.
    """
    # Step 1: Gather lines
    lines = user_context.get("pending_bulk_lines", [])
    resolved_lines = [l for l in lines if not l["unresolved"]]
    address_overrides = user_context.get("bulk_address_overrides", {})

    confirmed_count = sum(1 for l in resolved_lines if l.get("address_confirmed"))
    skipped   = [l for l in resolved_lines if l.get("address_skipped")]

    # Step 2–3: Place orders. Iterate over resolved_lines with index so we can
    # look up per-line address overrides (keyed by index in resolved_lines).
    created_orders = []
    failed_orders  = []

    for line_idx, line in enumerate(resolved_lines):
        if not line.get("address_confirmed"):
            continue

        override = address_overrides.get(str(line_idx)) or {}
        override_billing = override.get("billing") or {}
        override_shipping = override.get("shipping") or {}
        
        def _merge(base, override):
            base = dict(base or {})
            for k, v in (override or {}).items():
                if v not in (None, ""):       # don't let blank panel fields wipe real data
                    base[k] = v
            return base

        billing = _merge(line.get("billing_address"), override_billing)
        shipping = _merge(line.get("shipping_address"), override_shipping)

        # ── Custom CS fields → order meta (not address-block fields) ──
        # project_rep defaults to the logged-in rep's email when still blank.
        rep_email = user_context.get("rep_email", "")
        project_rep  = billing.get("project_rep") or rep_email
        project_name = billing.get("billing_project") or ""
        field_type   = billing.get("billing_field_type") or ""
        order_notes  = shipping.get("order_notes") or ""

        meta_data = []
        if project_rep:
            meta_data.append({"key": "_billing_project_rep", "value": project_rep})
        if project_name:
            meta_data.append({"key": "_billing_project_name", "value": project_name})
        if field_type:
            meta_data.append({"key": "_billing_field_type", "value": field_type})

        # Remove custom keys from the Woo address blocks — they live in meta.
        for _k in ("project_rep", "billing_project", "billing_field_type", "order_notes"):
            billing.pop(_k, None)
            shipping.pop(_k, None)

        payload = {
            "status": "processing",
            "customer_id": line["customer_id"],
            "payment_method": DEFAULT_PAYMENT_METHOD,
            "payment_method_title": DEFAULT_PAYMENT_METHOD_TITLE,
            "set_paid": False,
            "line_items": [
                {
                    "product_id": line["product_id"],
                    "variation_id": line.get("variation_id") or 0,
                    "quantity": line["quantity"],
                }
            ],
        }
        if meta_data:
            payload["meta_data"] = meta_data
        if order_notes:
            payload["customer_note"] = order_notes
        billing  = {k: v for k, v in billing.items() if v}
        shipping = {k: v for k, v in shipping.items() if v}
        if billing.get("address_1"):
            payload["billing"] = billing
        if shipping.get("address_1"):
            payload["shipping"] = shipping

        order_call = endpoints.create_order(
            payload=payload,
            description=f"Bulk order for {line['customer_display_name']}",
        )
        order_resp = woo_client.execute(order_call)

        if order_resp.get("success") and isinstance(order_resp.get("data"), dict):
            new_order = order_resp["data"]
            created_orders.append({
                "order_number": new_order.get("number") or new_order.get("id"),
                "customer": line["customer_display_name"],
                "product": line["product_name"],
                "quantity": line["quantity"],
            })
            logger.info(
                f"bulk_order | created order #{new_order.get('number') or new_order.get('id')} "
                f"for {line['customer_display_name']}"
            )
        else:
            failed_orders.append({
                "customer": line["customer_display_name"],
                "product": line["product_name"],
                "error": str(order_resp.get("error", "Unknown error")),
            })
            logger.warning(
                f"bulk_order | failed for {line['customer_display_name']} "
                f"product='{line['product_name']}' | error={order_resp.get('error')}"
            )

    # Step 4: Clear all bulk state
    user_context.pop("pending_bulk_lines", None)
    user_context.pop("bulk_current_line_index", None)
    user_context.pop("bulk_confirmed_lines", None)
    user_context.pop("bulk_address_overrides", None)
    user_context.pop("bulk_awaiting_address_text", None)
    user_context.pop("bulk_product_missing_indices", None)
    user_context.pop("bulk_product_current_pos", None)
    user_context.pop("bulk_quantity_pending_indices", None)
    user_context.pop("bulk_quantity_current_pos", None)
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    conversation.flow_state = FlowState.IDLE.value

    # Step 5: Build summary message
    bot_message = ""

    if created_orders:
        bot_message += f"✅ **{len(created_orders)} order(s) placed successfully:**\n\n"
        for o in created_orders:
            bot_message += f"• **#{o['order_number']}** — {o['customer']}: {o['product']} ×{o['quantity']}\n"

    if failed_orders:
        bot_message += f"\n⚠️ **{len(failed_orders)} order(s) failed:**\n\n"
        for fail in failed_orders:
            bot_message += f"• {fail['customer']}: {fail['product']} — {fail['error']}\n"

    if skipped:
        bot_message += f"\n⏭️ **{len(skipped)} order(s) skipped.**\n"

    if not bot_message:
        bot_message = "No orders were placed."

    suggestions = ["Show my orders"]
    if failed_orders:
        suggestions.append("Try again")
    suggestions.append("Place another bulk order")

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "bulk_order",
        "products": [],
        "suggestions": suggestions,
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.IDLE.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.IDLE.value,
        "pagination": default_pagination(page),
    }), 200