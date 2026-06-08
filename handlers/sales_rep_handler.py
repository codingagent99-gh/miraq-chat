"""
handlers/sales_rep_handler.py — Sales rep conversation flow handlers.

Feature 1: Order-for flow (order on behalf of a customer by email).
  handle_order_for_prompt()        — entry point, sets AWAITING_ORDER_FOR_EMAIL
  handle_order_for_email_reply()   — resolves customer by email, shows last 5 orders
  _fetch_and_show_last_5_orders()  — private: fetches orders + reorder hints

Company-name lookup and multi-match selection were removed.
Email is now the sole customer identifier for this flow.
"""

import time

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
    Sets flow state to AWAITING_ORDER_FOR_EMAIL and asks for the customer email.
    Returns a Flask response.
    """
    conversation.flow_state = FlowState.AWAITING_ORDER_FOR_EMAIL.value

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success":     True,
        "bot_message": (
            "Who would you like to place this order for?\n\n"
            "Please provide the customer's **email address**."
        ),
        "intent":      "guided_flow",
        "products":    [],
        "suggestions": ["Cancel"],
        "session_id":  str(conversation.id),
        "metadata": {
            "flow_state":       FlowState.AWAITING_ORDER_FOR_EMAIL.value,
            "response_time_ms": elapsed,
        },
        "flow_state":  FlowState.AWAITING_ORDER_FOR_EMAIL.value,
        "pagination":  default_pagination(page),
    }), 200


# ══════════════════════════════════════════════════════════════
# ── Function 2: handle_order_for_email_reply ──
# ══════════════════════════════════════════════════════════════

def handle_order_for_email_reply(message, conversation, user_context, page, start_time):
    """
    Handles the rep's email input during AWAITING_ORDER_FOR_EMAIL.
    Looks up the customer by exact email match and shows their last 5 orders.
    Returns a Flask response.
    """
    import re
    _EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', re.I)

    # ── Extract email from message ──
    email_match = _EMAIL_RE.search(message.strip())

    if not email_match:
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success":     True,
            "bot_message": (
                "Please provide a valid email address "
                "(e.g. **david@buildersco.com**)."
            ),
            "intent":      "guided_flow",
            "products":    [],
            "suggestions": ["Cancel"],
            "session_id":  str(conversation.id),
            "metadata": {
                "flow_state":       FlowState.AWAITING_ORDER_FOR_EMAIL.value,
                "response_time_ms": elapsed,
            },
            "flow_state":  FlowState.AWAITING_ORDER_FOR_EMAIL.value,
            "pagination":  default_pagination(page),
        }), 200

    email = email_match.group(0)

    # ── Look up customer by email ──
    call   = endpoints.search_customers_by_email(
        email=email,
        per_page=1,
        description=f"Order-for email lookup: '{email}'",
    )
    result = woo_client.execute(call)

    if not result.get("success"):
        logger.warning(
            f"handle_order_for_email_reply | API error | "
            f"email='{email}' | error={result.get('error')}"
        )
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success":     False,
            "bot_message": "Sorry, I couldn't search customers right now. Please try again.",
            "intent":      "guided_flow",
            "products":    [],
            "suggestions": ["Try again", "Cancel"],
            "session_id":  str(conversation.id),
            "metadata": {
                "flow_state":       FlowState.AWAITING_ORDER_FOR_EMAIL.value,
                "response_time_ms": elapsed,
            },
            "flow_state":  FlowState.AWAITING_ORDER_FOR_EMAIL.value,
            "pagination":  default_pagination(page),
        }), 200

    customers = result.get("data", [])
    if not isinstance(customers, list) or not customers:
        elapsed = round((time.time() - start_time) * 1000)
        return jsonify({
            "success":     True,
            "bot_message": (
                f"I couldn't find a customer with email **{email}**. "
                "Please check the address and try again."
            ),
            "intent":      "guided_flow",
            "products":    [],
            "suggestions": ["Try a different email", "Cancel"],
            "session_id":  str(conversation.id),
            "metadata": {
                "flow_state":       FlowState.AWAITING_ORDER_FOR_EMAIL.value,
                "response_time_ms": elapsed,
            },
            "flow_state":  FlowState.AWAITING_ORDER_FOR_EMAIL.value,
            "pagination":  default_pagination(page),
        }), 200

    return _fetch_and_show_last_5_orders(
        customers[0], conversation, user_context, page, start_time
    )


# ══════════════════════════════════════════════════════════════
# ── Function 3: _fetch_and_show_last_5_orders (private) ──
# ══════════════════════════════════════════════════════════════

def _fetch_and_show_last_5_orders(customer_data, conversation, user_context, page, start_time):
    """
    Fetches the last 5 orders for the resolved customer via the rep endpoint,
    annotates them with reorder hints, and returns a Flask response.
    """
    customer_id_target = str(customer_data["id"])
    display_name = (
        customer_data.get("display")
        or customer_data.get("billing", {}).get("company")
        or f"{customer_data.get('first_name', '')} {customer_data.get('last_name', '')}".strip()
        or customer_data.get("email", "")
        or f"Customer #{customer_id_target}"
    )

    call   = endpoints.list_rep_orders(
        body={"customer_id": customer_id_target, "per_page": 5},
        description=f"Fetch last 5 orders for {display_name}",
    )
    result = woo_client.execute(call)

    orders = []
    if result.get("success"):
        data = result.get("data", [])
        if isinstance(data, list):
            orders = data
        elif isinstance(data, dict):
            orders = data.get("orders", data.get("data", []))

    patterns = analyse_reorder_patterns(orders)

    user_context["order_for_customer_id"]  = customer_id_target
    user_context["order_for_display_name"] = display_name
    user_context.pop("order_for_candidates", None)

    conversation.context_data = user_context
    flag_modified(conversation, "context_data")
    conversation.flow_state = FlowState.IDLE.value

    if not orders:
        bot_message = (
            f"I couldn't find any recent orders for **{display_name}**. "
            "You can place a new order for them now."
        )
        suggestions = ["Place new order", "Order for someone else", "Cancel"]
    else:
        bot_message = f"Here are the last {len(orders)} orders for **{display_name}**:\n\n"
        for order in orders:
            order_num  = order.get("number") or order.get("id")
            order_date = (order.get("date_created") or "")[:10]
            line_items = order.get("line_items", [])
            items_text = ", ".join(
                f"{item.get('name', '?')} ×{item.get('quantity', 1)}"
                for item in line_items[:3]
            )
            if len(line_items) > 3:
                items_text += f" +{len(line_items) - 3} more"
            bot_message += f"**#{order_num}** — {order_date}\n{items_text}\n"
            for item in line_items:
                pid     = item.get("product_id")
                pattern = patterns.get(pid)
                if pattern and pattern.overdue:
                    bot_message += f"  ⚠️ {pattern.hint}\n"
            bot_message += "\n"
        suggestions = ["Reorder last order", "Place new order", "Order for someone else"]

    elapsed = round((time.time() - start_time) * 1000)
    return jsonify({
        "success":     True,
        "bot_message": bot_message,
        "intent":      "guided_flow",
        "products":    [],
        "suggestions": suggestions,
        "session_id":  str(conversation.id),
        "metadata": {
            "flow_state":               FlowState.IDLE.value,
            "response_time_ms":         elapsed,
            "order_for_customer_id":    customer_id_target,
            "order_for_display_name":   display_name,
        },
        "flow_state":  FlowState.IDLE.value,
        "pagination":  default_pagination(page),
    }), 200