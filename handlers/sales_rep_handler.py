"""
handlers/sales_rep_handler.py — Sales rep conversation flow handlers.

Feature 1: Order-for flow (order on behalf of a customer).
  handle_order_for_prompt()             — entry point, sets AWAITING_ORDER_FOR_COMPANY
  handle_order_for_company_reply()      — handles company name search
  handle_order_for_selection_reply()    — handles selection from multiple matches
  _fetch_and_show_last_5_orders()       — private: fetches orders + reorder hints

Session management (db.session.add / db.session.commit) is the caller's
responsibility in chat.py. This module only mutates conversation attributes
and calls flag_modified.
"""

import re
import time
import difflib

from flask import jsonify
from sqlalchemy.orm.attributes import flag_modified

from woo_client import woo_client
from ecommerce import endpoints
from conversation_flow import FlowState
from chat_logger import get_logger
from handlers.chat_utils import default_pagination
from utils.reorder_analysis import analyse_reorder_patterns
from app_config import BULK_ORDER_ROLES

logger = get_logger("miraq_chat")


# ══════════════════════════════════════════════════════════════
# ── Function 1: handle_order_for_prompt ──
# ══════════════════════════════════════════════════════════════

def handle_order_for_prompt(conversation, page, start_time):
    """
    Entry point for the order-for flow.
    Sets flow state to AWAITING_ORDER_FOR_COMPANY and asks who to order for.
    Returns a Flask response.
    """
    conversation.flow_state = FlowState.AWAITING_ORDER_FOR_COMPANY.value

    # Build suggestions from recently used companies (most recent first, max 3)
    recent = conversation.context_data.get("recent_order_for_companies", [])
    suggestions = list(recent[:3]) + ["Cancel"] if recent else ["Cancel"]

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": (
            "Who would you like to place this order for?\n\n"
            "Please type the company or customer name."
        ),
        "intent": "guided_flow",
        "products": [],
        "suggestions": suggestions,
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_ORDER_FOR_COMPANY.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_ORDER_FOR_COMPANY.value,
        "pagination": default_pagination(page),
    }), 200


# ══════════════════════════════════════════════════════════════
# ── Function 2: handle_order_for_company_reply ──
# ══════════════════════════════════════════════════════════════

def handle_order_for_company_reply(message, conversation, user_context, page, start_time):
    """
    Handles the user's company/customer name input during AWAITING_ORDER_FOR_COMPANY.
    Searches WooCommerce customers and either:
      - auto-selects a single match → _fetch_and_show_last_5_orders
      - presents a numbered list for multiple matches → AWAITING_ORDER_FOR_SELECTION
      - re-prompts on empty input or API failure
    Returns a Flask response.
    """
    search_term = message.strip()

    # Empty input — re-prompt without changing state
    if not search_term:
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                "Who would you like to place this order for?\n\n"
                "Please type the company or customer name."
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_ORDER_FOR_COMPANY.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_ORDER_FOR_COMPANY.value,
            "pagination": default_pagination(page),
        }), 200

    # Step 2: Search customers
    call = endpoints.list_customers_search(
        search=search_term,
        role="all",
        per_page=5,
        description=f"Order-for customer search: '{search_term}'",
    )
    result = woo_client.execute(call)

    # Step 3: API failure
    if not result.get("success") or not isinstance(result.get("data"), list):
        logger.warning(
            f"handle_order_for_company_reply | API error | "
            f"search='{search_term}' | error={result.get('error')}"
        )
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": False,
            "bot_message": (
                "Sorry, I couldn't search customers right now. "
                "Please try again."
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Try again", "Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_ORDER_FOR_COMPANY.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_ORDER_FOR_COMPANY.value,
            "pagination": default_pagination(page),
        }), 200

    customers = result["data"]

    # Step 5: No results
    if len(customers) == 0:
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": True,
            "bot_message": (
                f"I couldn't find any customer matching **'{search_term}'**. "
                "Please check the name and try again."
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Try a different name", "Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_ORDER_FOR_COMPANY.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_ORDER_FOR_COMPANY.value,
            "pagination": default_pagination(page),
        }), 200

    # Step 6: Exactly one match — go straight to orders view
    if len(customers) == 1:
        return _fetch_and_show_last_5_orders(
            customers[0], conversation, user_context, page, start_time
        )

    # Step 7: Multiple matches — rank by closeness and present selection list
    displays = [
        raw.get("billing", {}).get("company")
        or f"{raw.get('first_name', '')} {raw.get('last_name', '')}".strip()
        or f"Customer #{raw.get('id')}"
        for raw in customers
    ]

    # Rank by fuzzy closeness; fall back to original order if no close matches
    ranked_indices = difflib.get_close_matches(
        search_term.lower(),
        [d.lower() for d in displays],
        n=len(displays),
        cutoff=0.3,
    )
    if ranked_indices:
        # Re-order candidates so closest matches appear first
        lower_displays = [d.lower() for d in displays]
        ordered_pairs = sorted(
            enumerate(displays),
            key=lambda p: (
                ranked_indices.index(lower_displays[p[0]])
                if lower_displays[p[0]] in ranked_indices
                else len(ranked_indices)
            ),
        )
    else:
        ordered_pairs = list(enumerate(displays))

    candidates = [
        {
            "id": customers[i]["id"],
            "display": display,
            "billing": customers[i].get("billing", {}),
        }
        for i, display in ordered_pairs
    ]

    # Persist candidates for the selection handler
    user_context["order_for_candidates"] = candidates
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    conversation.flow_state = FlowState.AWAITING_ORDER_FOR_SELECTION.value

    numbered_list = "\n".join(
        f"{n}. {c['display']}" for n, c in enumerate(candidates, 1)
    )
    bot_message = (
        f"I found multiple matches for **'{search_term}'**:\n\n"
        f"{numbered_list}\n\n"
        "Which one?"
    )
    suggestions = [c["display"] for c in candidates] + ["Cancel"]

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "guided_flow",
        "products": [],
        "suggestions": suggestions,
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.AWAITING_ORDER_FOR_SELECTION.value,
            "response_time_ms": elapsed,
        },
        "flow_state": FlowState.AWAITING_ORDER_FOR_SELECTION.value,
        "pagination": default_pagination(page),
    }), 200


# ══════════════════════════════════════════════════════════════
# ── Function 3: handle_order_for_selection_reply ──
# ══════════════════════════════════════════════════════════════

def handle_order_for_selection_reply(message, conversation, user_context, page, start_time):
    """
    Handles the user's selection from the multiple-match list during
    AWAITING_ORDER_FOR_SELECTION.
    Matches by number (e.g. "2") or name substring (e.g. "ABC Corp").
    Returns a Flask response.
    """
    candidates = user_context.get("order_for_candidates", [])

    # Step 1: Missing candidates — something went wrong, reset
    if not candidates:
        logger.warning("handle_order_for_selection_reply | no candidates in context, resetting")
        conversation.flow_state = FlowState.IDLE.value
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success": False,
            "bot_message": "Something went wrong — please start again.",
            "intent": "guided_flow",
            "products": [],
            "suggestions": ["Order for a customer", "Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.IDLE.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.IDLE.value,
            "pagination": default_pagination(page),
        }), 200

    text = message.strip().lower()
    selected = None

    # Step 3: Try numeric match first
    num_match = re.search(r'\b(\d+)\b', text)
    if num_match:
        idx = int(num_match.group(1)) - 1
        if 0 <= idx < len(candidates):
            selected = candidates[idx]
        else:
            # Out-of-range number — re-prompt
            elapsed = round((time.time() - start_time) * 1000)
            return jsonify({
                "success": True,
                "bot_message": (
                    f"Please choose a number between **1** and **{len(candidates)}**."
                ),
                "intent": "guided_flow",
                "products": [],
                "suggestions": [c["display"] for c in candidates] + ["Cancel"],
                "session_id": str(conversation.id),
                "metadata": {
                    "flow_state": FlowState.AWAITING_ORDER_FOR_SELECTION.value,
                    "response_time_ms": elapsed,
                },
                "flow_state": FlowState.AWAITING_ORDER_FOR_SELECTION.value,
                "pagination": default_pagination(page),
            }), 200

    # Step 4: Fallback to name substring match
    if selected is None:
        for c in candidates:
            c_lower = c["display"].lower()
            if text in c_lower or c_lower in text:
                selected = c
                break

    # Step 4 (cont): Still no match — re-prompt
    if selected is None:
        elapsed = round((time.time() - start_time) * 1000)
        numbered_list = "\n".join(
            f"{n}. {c['display']}" for n, c in enumerate(candidates, 1)
        )
        return jsonify({
            "success": True,
            "bot_message": (
                f"I couldn't match that. Please choose one of:\n\n{numbered_list}"
            ),
            "intent": "guided_flow",
            "products": [],
            "suggestions": [c["display"] for c in candidates] + ["Cancel"],
            "session_id": str(conversation.id),
            "metadata": {
                "flow_state": FlowState.AWAITING_ORDER_FOR_SELECTION.value,
                "response_time_ms": elapsed,
            },
            "flow_state": FlowState.AWAITING_ORDER_FOR_SELECTION.value,
            "pagination": default_pagination(page),
        }), 200

    # Step 5: Match found — clear candidates and show orders
    user_context.pop("order_for_candidates", None)
    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    return _fetch_and_show_last_5_orders(
        selected, conversation, user_context, page, start_time
    )


# ══════════════════════════════════════════════════════════════
# ── Function 4: _fetch_and_show_last_5_orders (private) ──
# ══════════════════════════════════════════════════════════════

def _fetch_and_show_last_5_orders(customer_data, conversation, user_context, page, start_time):
    """
    Fetches the last 5 orders for the resolved customer via the rep endpoint,
    annotates them with reorder hints, and returns a Flask response.

    customer_data: dict with at least {"id": int}. May be a raw WooCommerce
                   customer dict or a candidate dict from order_for_candidates.
    """
    # Step 1: Resolve display name
    customer_id_target = str(customer_data["id"])
    display_name = (
        customer_data.get("display")
        or customer_data.get("billing", {}).get("company")
        or f"{customer_data.get('first_name', '')} {customer_data.get('last_name', '')}".strip()
        or f"Customer #{customer_id_target}"
    )

    # Step 2: Fetch orders via custom plugin (rep path)
    call = endpoints.list_rep_orders(
        body={"customer_id": customer_id_target, "per_page": 5},
        description=f"Fetch last 5 orders for {display_name}",
    )
    result = woo_client.execute(call)

    # Step 3: Normalise response into a flat orders list
    orders = []
    if result.get("success"):
        data = result.get("data", [])
        if isinstance(data, list):
            orders = data
        elif isinstance(data, dict):
            orders = data.get("orders", data.get("data", []))

    # Step 4: Reorder analysis
    patterns = analyse_reorder_patterns(orders)

    # Step 5: Update user_context
    user_context["order_for_customer_id"] = customer_id_target
    user_context["order_for_display_name"] = display_name

    # Track recently used companies — deduped, most recent first, max 3
    recent = user_context.get("recent_order_for_companies", [])
    if display_name in recent:
        recent.remove(display_name)
    recent.insert(0, display_name)
    user_context["recent_order_for_companies"] = recent[:3]

    user_context.pop("order_for_candidates", None)

    conversation.context_data = user_context
    flag_modified(conversation, "context_data")

    # Step 6: Reset flow state to IDLE
    conversation.flow_state = FlowState.IDLE.value

    # Step 7: Build bot_message
    if not orders:
        bot_message = (
            f"I couldn't find any recent orders for **{display_name}**. "
            "You can place a new order for them now."
        )
        suggestions = ["Place new order", "Order for someone else", "Cancel"]
    else:
        bot_message = f"Here are the last {len(orders)} orders for **{display_name}**:\n\n"
        for i, order in enumerate(orders, 1):
            order_num = order.get("number") or order.get("id")
            order_date = (order.get("date_created") or "")[:10]
            line_items = order.get("line_items", [])

            items_text = ", ".join(
                f"{item.get('name', '?')} ×{item.get('quantity', 1)}"
                for item in line_items[:3]
            )
            if len(line_items) > 3:
                items_text += f" +{len(line_items) - 3} more"

            bot_message += f"**#{order_num}** — {order_date}\n{items_text}\n"

            # Append overdue reorder hints for any product in this order
            for item in line_items:
                pid = item.get("product_id")
                pattern = patterns.get(pid)
                if pattern and pattern.overdue:
                    bot_message += f"  ⚠️ {pattern.hint}\n"

            bot_message += "\n"

        suggestions = ["Reorder last order", "Place new order", "Order for someone else"]

    # Step 8: Return
    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success": True,
        "bot_message": bot_message,
        "intent": "guided_flow",
        "products": [],
        "suggestions": suggestions,
        "session_id": str(conversation.id),
        "metadata": {
            "flow_state": FlowState.IDLE.value,
            "response_time_ms": elapsed,
            "order_for_customer_id": customer_id_target,
            "order_for_display_name": display_name,
        },
        "flow_state": FlowState.IDLE.value,
        "pagination": default_pagination(page),
    }), 200